"""Authenticated search-profile setup and user-assisted job URL intake."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable
from urllib.parse import urlsplit

from core.authenticated_subject import AuthenticatedSubjectContext
from core.job_discovery import (
    DiscoveryDisposition,
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobIntakeIntent,
    JobIntakeProposal,
    ProposalResolution,
    ResolvedJobCandidate,
)
from core.job_leads import (
    JobLead,
    JobLeadListStatus,
    JobLeadOrigin,
    JobLeadReadStatus,
    JobLeadRepository,
    JobLeadSource,
    JobLeadStatus,
    JobLeadWriteStatus,
    canonicalize_job_lead_url,
)
from core.search_profile import (
    SaveSearchProfileCommand,
    SearchProfileListStatus,
    SearchProfileRepository,
    SearchProfileSourceReference,
    save_search_profile,
)
from core.subject_job_discovery import (
    SubjectJobDiscoveryCommand,
    SubjectJobDiscoveryResult,
    SubjectJobDiscoveryStatus,
)
from core.subject_job_library import SubjectJobMembershipSourceKind
from source_connectors.contract import (
    PUBLIC_ATS_JOB_HOST_SUFFIXES,
    ReadJobRequest,
    ReadJobResult,
    ReadJobStatus,
)


class AssistedDiscoveryPlatform(StrEnum):
    LINKEDIN = "LINKEDIN"
    INDEED = "INDEED"
    GLASSDOOR = "GLASSDOOR"
    WEB_CLIPPER = "WEB_CLIPPER"


class AssistedJobImportStatus(StrEnum):
    IMPORTED = "IMPORTED"
    UNCHANGED = "UNCHANGED"
    HUMAN_INTERVENTION_REQUIRED = "HUMAN_INTERVENTION_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SaveSearchProfileUICommand:
    display_name: str
    company: str
    title: str
    source: SearchProfileSourceReference
    enabled: bool
    location: str | None = None
    profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class AssistedJobImportCommand:
    platform: AssistedDiscoveryPlatform
    job_url: str
    invocation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "platform", AssistedDiscoveryPlatform(self.platform)
        )
        if (
            not isinstance(self.job_url, str)
            or not self.job_url.strip()
            or len(self.job_url) > 2048
        ):
            raise ValueError("job_url is invalid")
        if (
            not isinstance(self.invocation_id, str)
            or not self.invocation_id.strip()
            or len(self.invocation_id) > 160
        ):
            raise ValueError("invocation_id is invalid")


@dataclass(frozen=True, slots=True)
class CurrentPageJobCaptureCommand:
    page_url: str
    page_title: str
    invocation_id: str
    user_gesture: bool
    selected_text: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.page_url, str)
            or not self.page_url.strip()
            or len(self.page_url) > 2048
        ):
            raise ValueError("page_url is invalid")
        if (
            not isinstance(self.page_title, str)
            or not self.page_title.strip()
            or len(self.page_title) > 500
        ):
            raise ValueError("page_title is invalid")
        if (
            not isinstance(self.invocation_id, str)
            or not self.invocation_id.strip()
            or len(self.invocation_id) > 160
        ):
            raise ValueError("invocation_id is invalid")
        if self.user_gesture is not True:
            raise ValueError("current-page capture requires a user gesture")
        if self.selected_text is not None and (
            not isinstance(self.selected_text, str)
            or not self.selected_text.strip()
            or len(self.selected_text) > 2000
        ):
            raise ValueError("selected_text is invalid")


@dataclass(frozen=True, slots=True)
class ResolveJobLeadCommand:
    lead_id: str
    official_job_url: str
    invocation_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.lead_id, str)
            or re.fullmatch(r"job-lead-[0-9a-f]{64}", self.lead_id) is None
        ):
            raise ValueError("lead_id is invalid")
        if (
            not isinstance(self.official_job_url, str)
            or not self.official_job_url.strip()
            or len(self.official_job_url) > 2048
        ):
            raise ValueError("official_job_url is invalid")
        if (
            not isinstance(self.invocation_id, str)
            or not self.invocation_id.strip()
            or len(self.invocation_id) > 160
        ):
            raise ValueError("invocation_id is invalid")


class SearchProfileUIController:
    def __init__(
        self,
        *,
        repository: SearchProfileRepository,
        available_sources: tuple[SearchProfileSourceReference, ...],
        source_companies: Mapping[SearchProfileSourceReference, str | None],
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(repository, SearchProfileRepository):
            raise TypeError("repository must implement SearchProfileRepository")
        self.repository = repository
        self.available_sources = tuple(available_sources)
        self.source_companies = dict(source_companies)
        if set(self.source_companies) != set(self.available_sources):
            raise ValueError("source company metadata is incomplete")
        self.clock = clock

    def read(self, context: AuthenticatedSubjectContext) -> dict[str, Any]:
        listed = self.repository.list_current(context.subject_id)
        if listed.status is not SearchProfileListStatus.SUCCEEDED:
            return {"status": "INTEGRITY_FAILURE", "profiles": []}
        return {
            "status": "SUCCEEDED",
            "available_sources": [
                {
                    **source.to_dict(),
                    "canonical_company": self.source_companies[source],
                }
                for source in self.available_sources
            ],
            "profiles": [
                {
                    "display_name": profile.display_name,
                    "enabled": profile.enabled,
                    "location": profile.search_request.location,
                    "profile_id": profile.profile_id,
                    "profile_version": profile.profile_version,
                    "source": profile.source.to_dict(),
                    "company": profile.search_request.company,
                    "title": profile.search_request.title,
                }
                for profile in listed.profiles
            ],
        }

    def save(
        self,
        context: AuthenticatedSubjectContext,
        command: SaveSearchProfileUICommand,
    ) -> dict[str, Any]:
        if command.source not in self.available_sources:
            return {"status": "FAILED", "reason": "SOURCE_NOT_CONFIGURED"}
        result = save_search_profile(
            SaveSearchProfileCommand(
                subject_id=context.subject_id,
                display_name=command.display_name,
                company=command.company,
                title=command.title,
                location=command.location,
                source=command.source,
                enabled=command.enabled,
                profile_id=command.profile_id,
                now=self.clock(),
            ),
            repository=self.repository,
        )
        return {
            "status": result.status.value,
            "reason": result.reason.value if result.reason else None,
            "profile_id": (
                result.profile.profile_id if result.profile is not None else None
            ),
            "profile_version": (
                result.profile.profile_version
                if result.profile is not None
                else None
            ),
        }


class AssistedJobImportController:
    def __init__(
        self,
        *,
        public_job_reader: Callable[[ReadJobRequest], Any],
        discovery: Callable[[SubjectJobDiscoveryCommand], Any],
        clock: Callable[[], datetime],
        lead_repository: JobLeadRepository | None = None,
    ) -> None:
        self.public_job_reader = public_job_reader
        self.discovery = discovery
        self.clock = clock
        if lead_repository is not None and not isinstance(
            lead_repository, JobLeadRepository
        ):
            raise TypeError("lead_repository must implement JobLeadRepository")
        self.lead_repository = lead_repository

    async def capture_current_page(
        self,
        context: AuthenticatedSubjectContext,
        command: CurrentPageJobCaptureCommand,
    ) -> dict[str, Any]:
        """Capture only the page a user explicitly opened; never navigate it."""

        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("authenticated context is required")
        if not isinstance(command, CurrentPageJobCaptureCommand):
            raise TypeError("capture command must be typed")
        if self.lead_repository is None:
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="JOB_LEAD_STORAGE_UNAVAILABLE",
            )
        try:
            canonical_url = canonicalize_job_lead_url(
                "page_url", command.page_url
            )
        except (TypeError, ValueError):
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="INVALID_JOB_URL",
            )
        listed = self.lead_repository.list_current(context.subject_id)
        if listed.status is not JobLeadListStatus.SUCCEEDED:
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="JOB_LEAD_STORAGE_UNAVAILABLE",
            )
        existing = next(
            (lead for lead in listed.leads if lead.source_url == canonical_url),
            None,
        )
        if existing is not None and existing.status is JobLeadStatus.RESOLVED:
            return {
                **_import_result(
                    AssistedJobImportStatus.IMPORTED,
                    reason=existing.reason,
                    canonical_url=existing.canonical_url,
                ),
                "lead_id": existing.lead_id,
                "lead_status": existing.status.value,
            }
        if existing is None:
            origin = _lead_origin(canonical_url)
            try:
                lead = JobLead.discover(
                    subject_id=context.subject_id,
                    source=JobLeadSource.WEB_CLIPPER,
                    origin=origin,
                    source_url=canonical_url,
                    discovered_at=self.clock(),
                    confidence=0.65,
                    title_hint=" ".join(command.page_title.split())[:320],
                    snippet_hint=(
                        " ".join(command.selected_text.split())[:2000]
                        if command.selected_text is not None
                        else None
                    ),
                )
                written = self.lead_repository.save(lead)
            except (OSError, RuntimeError, TypeError, ValueError):
                written = None
            if (
                written is None
                or written.status
                not in {JobLeadWriteStatus.CREATED, JobLeadWriteStatus.UNCHANGED}
                or written.lead is None
            ):
                return _import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="JOB_LEAD_STORAGE_UNAVAILABLE",
                )
            lead = written.lead
        else:
            lead = existing
        imported = await self.import_job(
            context,
            AssistedJobImportCommand(
                platform=AssistedDiscoveryPlatform.WEB_CLIPPER,
                job_url=canonical_url,
                invocation_id=command.invocation_id,
            ),
            membership_source_ref=lead.lead_id,
        )
        if imported["status"] in {
            AssistedJobImportStatus.IMPORTED.value,
            AssistedJobImportStatus.UNCHANGED.value,
        }:
            transition = lead.transition(
                JobLeadStatus.RESOLVED,
                now=self.clock(),
                canonical_url=(
                    imported.get("canonical_url") or canonical_url
                ),
            )
        elif lead.status is JobLeadStatus.DISCOVERED:
            transition = lead.transition(
                JobLeadStatus.NEEDS_USER,
                now=self.clock(),
                reason=(
                    imported.get("reason")
                    or "OFFICIAL_POSTING_NOT_VERIFIED"
                ),
            )
        else:
            return {
                **imported,
                "lead_id": lead.lead_id,
                "lead_status": lead.status.value,
            }
        saved = self.lead_repository.save(transition)
        if saved.status not in {
            JobLeadWriteStatus.CREATED,
            JobLeadWriteStatus.UNCHANGED,
        }:
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="JOB_LEAD_STORAGE_UNAVAILABLE",
            )
        return {
            **imported,
            "lead_id": transition.lead_id,
            "lead_status": transition.status.value,
        }

    async def resolve_lead(
        self,
        context: AuthenticatedSubjectContext,
        command: ResolveJobLeadCommand,
    ) -> dict[str, Any]:
        """Verify one user-supplied employer/ATS URL for an existing lead."""

        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("authenticated context is required")
        if not isinstance(command, ResolveJobLeadCommand):
            raise TypeError("resolve command must be typed")
        if self.lead_repository is None:
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="JOB_LEAD_STORAGE_UNAVAILABLE",
            )
        current = self.lead_repository.get(
            context.subject_id, command.lead_id
        )
        if current.status is JobLeadReadStatus.INTEGRITY_FAILURE:
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="JOB_LEAD_INTEGRITY_FAILURE",
            )
        if current.status is not JobLeadReadStatus.FOUND or current.lead is None:
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="JOB_LEAD_NOT_FOUND",
            )
        lead = current.lead
        if lead.status is JobLeadStatus.RESOLVED:
            return {
                **_import_result(
                    AssistedJobImportStatus.UNCHANGED,
                    reason=None,
                    canonical_url=lead.canonical_url,
                ),
                "lead_id": lead.lead_id,
                "lead_status": lead.status.value,
            }
        if lead.status not in {
            JobLeadStatus.DISCOVERED,
            JobLeadStatus.NEEDS_USER,
        }:
            return {
                **_import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="JOB_LEAD_NOT_RESOLVABLE",
                ),
                "lead_id": lead.lead_id,
                "lead_status": lead.status.value,
            }

        imported = await self.import_job(
            context,
            AssistedJobImportCommand(
                platform=_assisted_platform_for_lead(lead),
                job_url=command.official_job_url,
                invocation_id=command.invocation_id,
            ),
            membership_source_ref=lead.lead_id,
        )
        if imported["status"] not in {
            AssistedJobImportStatus.IMPORTED.value,
            AssistedJobImportStatus.UNCHANGED.value,
        }:
            if lead.status is JobLeadStatus.DISCOVERED:
                reason = imported.get("reason") or "OFFICIAL_POSTING_NOT_VERIFIED"
                transition = lead.transition(
                    JobLeadStatus.NEEDS_USER,
                    now=self.clock(),
                    reason=reason,
                )
                saved = self.lead_repository.save(transition)
                if saved.status not in {
                    JobLeadWriteStatus.CREATED,
                    JobLeadWriteStatus.UNCHANGED,
                }:
                    return _import_result(
                        AssistedJobImportStatus.FAILED,
                        reason="JOB_LEAD_STORAGE_UNAVAILABLE",
                    )
                lead = transition
            return {
                **imported,
                "lead_id": lead.lead_id,
                "lead_status": lead.status.value,
            }

        canonical_url = imported.get("canonical_url")
        if not isinstance(canonical_url, str) or not canonical_url:
            return {
                **_import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="VERIFIED_CANONICAL_URL_MISSING",
                ),
                "lead_id": lead.lead_id,
                "lead_status": lead.status.value,
            }
        transition = lead.transition(
            JobLeadStatus.RESOLVED,
            now=self.clock(),
            canonical_url=canonical_url,
        )
        saved = self.lead_repository.save(transition)
        if saved.status not in {
            JobLeadWriteStatus.CREATED,
            JobLeadWriteStatus.UNCHANGED,
        }:
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="JOB_LEAD_STORAGE_UNAVAILABLE",
            )
        return {
            **imported,
            "lead_id": transition.lead_id,
            "lead_status": transition.status.value,
        }

    async def import_job(
        self,
        context: AuthenticatedSubjectContext,
        command: AssistedJobImportCommand,
        *,
        membership_source_ref: str | None = None,
    ) -> dict[str, Any]:
        try:
            canonical_url = canonicalize_job_lead_url(
                "job_url", command.job_url
            )
            host = (urlsplit(canonical_url).hostname or "").casefold()
        except (TypeError, ValueError):
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="INVALID_JOB_URL",
            )

        # A user-pasted URL is a first-class Lead before it can become a job
        # fact.  Captures and existing Lead resolutions pass their own bound
        # source ref; the plain import endpoint creates/reuses a PASTED_URL
        # Lead so JOB_LEAD_RESOLUTION never points at a synthetic label.
        direct_lead: JobLead | None = None
        if membership_source_ref is None and self.lead_repository is not None:
            try:
                listed = self.lead_repository.list_current(context.subject_id)
            except (OSError, RuntimeError, TypeError, ValueError):
                listed = None
            if (
                listed is None
                or listed.status is not JobLeadListStatus.SUCCEEDED
            ):
                return _import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="JOB_LEAD_STORAGE_UNAVAILABLE",
                )
            direct_lead = next(
                (
                    lead
                    for lead in listed.leads
                    if lead.source_url == canonical_url
                ),
                None,
            )
            if direct_lead is None:
                try:
                    draft = JobLead.discover(
                        subject_id=context.subject_id,
                        source=JobLeadSource.PASTED_URL,
                        origin=_lead_origin(canonical_url),
                        source_url=canonical_url,
                        discovered_at=self.clock(),
                        confidence=0.55,
                    )
                    written = self.lead_repository.save(draft)
                except (OSError, RuntimeError, TypeError, ValueError):
                    written = None
                if (
                    written is None
                    or written.status
                    not in {
                        JobLeadWriteStatus.CREATED,
                        JobLeadWriteStatus.UNCHANGED,
                    }
                    or written.lead is None
                ):
                    return _import_result(
                        AssistedJobImportStatus.FAILED,
                        reason="JOB_LEAD_STORAGE_UNAVAILABLE",
                    )
                direct_lead = written.lead
            membership_source_ref = direct_lead.lead_id

        if direct_lead is not None and direct_lead.status not in {
            JobLeadStatus.DISCOVERED,
            JobLeadStatus.NEEDS_USER,
            JobLeadStatus.RESOLVED,
        }:
            return {
                **_import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="JOB_LEAD_NOT_RESOLVABLE",
                ),
                "lead_id": direct_lead.lead_id,
                "lead_status": direct_lead.status.value,
            }

        if _aggregator_host(host):
            result = _import_result(
                AssistedJobImportStatus.HUMAN_INTERVENTION_REQUIRED,
                reason="EMPLOYER_JOB_URL_REQUIRED",
                message=(
                    "Open the result in your local browser, complete any login "
                    "or human verification yourself, then paste the employer or "
                    "ATS job URL. JobOps does not fetch authenticated job-"
                    "platform pages."
                ),
            )
            if direct_lead is None or self.lead_repository is None:
                return result
            if direct_lead.status is JobLeadStatus.RESOLVED:
                return {
                    **_import_result(
                        AssistedJobImportStatus.UNCHANGED,
                        reason=None,
                        canonical_url=direct_lead.canonical_url,
                    ),
                    "lead_id": direct_lead.lead_id,
                    "lead_status": direct_lead.status.value,
                }
            if direct_lead.status is JobLeadStatus.NEEDS_USER:
                return {
                    **result,
                    "lead_id": direct_lead.lead_id,
                    "lead_status": direct_lead.status.value,
                }
            try:
                needs_user = direct_lead.transition(
                    JobLeadStatus.NEEDS_USER,
                    now=self.clock(),
                    reason="EMPLOYER_JOB_URL_REQUIRED",
                )
                written = self.lead_repository.save(needs_user)
            except (OSError, RuntimeError, TypeError, ValueError):
                return _import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="JOB_LEAD_STORAGE_UNAVAILABLE",
                )
            if written.status not in {
                JobLeadWriteStatus.CREATED,
                JobLeadWriteStatus.UNCHANGED,
            }:
                return _import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="JOB_LEAD_STORAGE_UNAVAILABLE",
                )
            return {
                **result,
                "lead_id": needs_user.lead_id,
                "lead_status": needs_user.status.value,
            }
        read_result = await _resolve(
            self.public_job_reader(ReadJobRequest(canonical_url))
        )
        if (
            not isinstance(read_result, ReadJobResult)
            or read_result.status is not ReadJobStatus.SUCCEEDED
            or read_result.observation is None
        ):
            reason = (
                read_result.reason_code.value
                if isinstance(read_result, ReadJobResult)
                and read_result.reason_code is not None
                else "PUBLIC_JOB_READ_FAILED"
            )
            result = _import_result(
                AssistedJobImportStatus.HUMAN_INTERVENTION_REQUIRED,
                reason=reason,
                message=(
                    "The pasted destination could not be read as a public job. "
                    "Use the local browser to open it and paste its final public "
                    "employer or ATS URL; do not bypass a login or anti-bot page."
                ),
            )
            if direct_lead is None or self.lead_repository is None:
                return result
            if direct_lead.status is not JobLeadStatus.DISCOVERED:
                return {
                    **result,
                    "lead_id": direct_lead.lead_id,
                    "lead_status": direct_lead.status.value,
                }
            try:
                needs_user = direct_lead.transition(
                    JobLeadStatus.NEEDS_USER,
                    now=self.clock(),
                    reason=reason,
                )
                written = self.lead_repository.save(needs_user)
            except (OSError, RuntimeError, TypeError, ValueError):
                written = None
            if (
                written is None
                or written.status
                not in {
                    JobLeadWriteStatus.CREATED,
                    JobLeadWriteStatus.UNCHANGED,
                }
            ):
                return _import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="JOB_LEAD_STORAGE_UNAVAILABLE",
                )
            return {
                **result,
                "lead_id": needs_user.lead_id,
                "lead_status": needs_user.status.value,
            }
        observation = read_result.observation
        identity = hashlib.sha256(
            (
                f"{context.subject_id}\n{command.platform.value}\n"
                f"{canonical_url}\n{command.invocation_id}"
            ).encode("utf-8")
        ).hexdigest()
        request = JobDiscoveryRequest(
            request_id=f"assisted-job-import-{identity[:32]}",
            trigger=DiscoveryTrigger.CONVERSATIONAL,
            proposal=JobIntakeProposal(
                proposal_id=f"assisted-job-import-proposal-{identity[:32]}",
                intent=JobIntakeIntent.ADD_JOB,
                resolution=ProposalResolution.RESOLVED,
                resolved_candidate=ResolvedJobCandidate(
                    source_platform=observation.source_platform.value,
                    source_job_id=observation.source_job_id,
                    source_url=observation.source_url,
                    application_url=observation.application_url,
                    company=observation.company,
                    title=observation.title,
                    description=observation.description,
                    location=observation.location,
                    work_mode=observation.work_mode.value,
                    posted_at=observation.posted_at,
                    ats_type=observation.ats_type.value,
                ),
            ),
        )
        discovered = await _resolve(
            self.discovery(
                SubjectJobDiscoveryCommand(
                    subject_id=context.subject_id,
                    request=request,
                    source_kind=(
                        SubjectJobMembershipSourceKind.JOB_LEAD_RESOLUTION
                        if membership_source_ref is not None
                        else SubjectJobMembershipSourceKind.CONVERSATIONAL_ADD
                    ),
                    source_ref=(
                        membership_source_ref
                        or canonical_url
                    ),
                    invocation_id=command.invocation_id,
                    now=self.clock(),
                )
            )
        )
        if (
            not isinstance(discovered, SubjectJobDiscoveryResult)
            or discovered.status is not SubjectJobDiscoveryStatus.ACCEPTED
            or discovered.discovery_response.disposition
            is not DiscoveryDisposition.ACCEPTED
        ):
            result = _import_result(
                AssistedJobImportStatus.FAILED,
                reason="DISCOVERY_NOT_ACCEPTED",
            )
            if direct_lead is None or self.lead_repository is None:
                return result
            if direct_lead.status is not JobLeadStatus.DISCOVERED:
                return {
                    **result,
                    "lead_id": direct_lead.lead_id,
                    "lead_status": direct_lead.status.value,
                }
            try:
                needs_user = direct_lead.transition(
                    JobLeadStatus.NEEDS_USER,
                    now=self.clock(),
                    reason="DISCOVERY_NOT_ACCEPTED",
                )
                written = self.lead_repository.save(needs_user)
            except (OSError, RuntimeError, TypeError, ValueError):
                written = None
            if (
                written is None
                or written.status
                not in {
                    JobLeadWriteStatus.CREATED,
                    JobLeadWriteStatus.UNCHANGED,
                }
            ):
                return _import_result(
                    AssistedJobImportStatus.FAILED,
                    reason="JOB_LEAD_STORAGE_UNAVAILABLE",
                )
            return {
                **result,
                "lead_id": needs_user.lead_id,
                "lead_status": needs_user.status.value,
            }
        change = discovered.discovery_response.change
        result = _import_result(
            (
                AssistedJobImportStatus.UNCHANGED
                if change is not None and change.value == "UNCHANGED"
                else AssistedJobImportStatus.IMPORTED
            ),
            reason=None,
            job_id=discovered.discovery_response.job_id,
            canonical_url=observation.source_url,
        )
        if direct_lead is None or self.lead_repository is None:
            return result
        if direct_lead.status is JobLeadStatus.RESOLVED:
            return {
                **result,
                "lead_id": direct_lead.lead_id,
                "lead_status": direct_lead.status.value,
            }
        try:
            resolved_lead = direct_lead.transition(
                JobLeadStatus.RESOLVED,
                now=self.clock(),
                canonical_url=observation.source_url,
            )
            written = self.lead_repository.save(resolved_lead)
        except (OSError, RuntimeError, TypeError, ValueError):
            written = None
        if (
            written is None
            or written.status
            not in {
                JobLeadWriteStatus.CREATED,
                JobLeadWriteStatus.UNCHANGED,
            }
        ):
            return _import_result(
                AssistedJobImportStatus.FAILED,
                reason="JOB_LEAD_STORAGE_UNAVAILABLE",
            )
        return {
            **result,
            "lead_id": resolved_lead.lead_id,
            "lead_status": resolved_lead.status.value,
        }


async def _resolve(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _aggregator_host(host: str) -> bool:
    host = host.casefold().rstrip(".")
    return (
        host == "linkedin.com"
        or host.endswith(".linkedin.com")
        or host == "indeed.com"
        or host.endswith(".indeed.com")
        or host == "glassdoor.com"
        or host.endswith(".glassdoor.com")
        or host == "glassdoor.ca"
        or host.endswith(".glassdoor.ca")
    )


def _assisted_platform_for_lead(lead: JobLead) -> AssistedDiscoveryPlatform:
    if lead.origin is JobLeadOrigin.LINKEDIN_SEARCH_INDEX:
        return AssistedDiscoveryPlatform.LINKEDIN
    if lead.origin is JobLeadOrigin.INDEED_SEARCH_INDEX:
        return AssistedDiscoveryPlatform.INDEED
    if lead.origin is JobLeadOrigin.GLASSDOOR_SEARCH_INDEX:
        return AssistedDiscoveryPlatform.GLASSDOOR
    return AssistedDiscoveryPlatform.WEB_CLIPPER


def _lead_origin(url: str) -> JobLeadOrigin:
    host = (urlsplit(url).hostname or "").casefold().rstrip(".")
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return JobLeadOrigin.LINKEDIN_SEARCH_INDEX
    if host == "indeed.com" or host.endswith(".indeed.com"):
        return JobLeadOrigin.INDEED_SEARCH_INDEX
    if (
        host == "glassdoor.com"
        or host.endswith(".glassdoor.com")
        or host == "glassdoor.ca"
        or host.endswith(".glassdoor.ca")
    ):
        return JobLeadOrigin.GLASSDOOR_SEARCH_INDEX
    if any(
        host == domain or host.endswith(f".{domain}")
        for domain in PUBLIC_ATS_JOB_HOST_SUFFIXES
    ):
        return JobLeadOrigin.ATS
    return JobLeadOrigin.UNKNOWN_WEB


def _import_result(
    status: AssistedJobImportStatus,
    *,
    reason: str | None,
    message: str | None = None,
    job_id: str | None = None,
    canonical_url: str | None = None,
) -> dict[str, Any]:
    result = {
        "status": status.value,
        "reason": reason,
        "message": message,
        "job_id": job_id,
    }
    if canonical_url is not None:
        result["canonical_url"] = canonical_url
    return result


__all__ = [
    "AssistedDiscoveryPlatform",
    "AssistedJobImportCommand",
    "AssistedJobImportController",
    "AssistedJobImportStatus",
    "CurrentPageJobCaptureCommand",
    "SaveSearchProfileUICommand",
    "SearchProfileUIController",
]
