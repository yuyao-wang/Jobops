"""UI-safe adapter for the S3b manual job-library refresh service."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from core.authenticated_subject import AuthenticatedSubjectContext
from core.job_library_refresh import (
    CandidateDiscoveryStatus,
    JobLibraryRefreshProgress,
    JobLibraryRefreshProgressObserver,
    JobLibraryRefreshProgressPhase,
    JobLibraryRefreshPriorityScope,
    JobLibraryRefreshStatus,
    ManualJobLibraryRefreshCommand,
    ManualJobLibraryRefreshResult,
    ProfileRefreshSearchStatus,
)
from core.job_leads import JobLeadSource


class RefreshJobLibraryUIStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    NOOP = "NOOP"


class LeadRefreshStatus(StrEnum):
    """Terminal state for the optional pre-normalization discovery pass."""

    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    NOOP = "NOOP"


class LeadRefreshSourceStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    NOOP = "NOOP"


class LeadRefreshPhase(StrEnum):
    INGESTING_ALERTS = "INGESTING_ALERTS"
    DISCOVERING = "DISCOVERING"
    RESOLVING = "RESOLVING"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True, slots=True)
class LeadRefreshSourceResult:
    """Credential-free counters for one JobLead acquisition channel."""

    source: JobLeadSource
    status: LeadRefreshSourceStatus
    family: str | None = None
    requests: int = 0
    completed: int = 0
    search_hits: int = 0
    discovered: int = 0
    unique: int = 0
    duplicates: int = 0
    resolved: int = 0
    needs_user: int = 0
    failed: int = 0
    public_reads: int = 0
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", JobLeadSource(self.source))
        object.__setattr__(self, "status", LeadRefreshSourceStatus(self.status))
        if self.family is not None and (
            not isinstance(self.family, str)
            or self.family != self.family.strip()
            or not self.family
            or len(self.family) > 80
            or not self.family.replace("_", "").replace("-", "").isalnum()
        ):
            raise ValueError("family is outside the lead refresh contract")
        for name in (
            "requests",
            "completed",
            "search_hits",
            "discovered",
            "unique",
            "duplicates",
            "resolved",
            "needs_user",
            "failed",
            "public_reads",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.completed > self.requests:
            raise ValueError("completed cannot exceed requests")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")


@dataclass(frozen=True, slots=True)
class LeadRefreshProgress:
    phase: LeadRefreshPhase
    requests: int = 0
    completed: int = 0
    discovered: int = 0
    unique: int = 0
    duplicates: int = 0
    resolved: int = 0
    needs_user: int = 0
    failed: int = 0
    public_reads: int = 0
    priorities_requested: int = 0
    priorities_refreshed: int = 0
    priorities_failed: int = 0
    truncated: bool = False
    source_results: tuple[LeadRefreshSourceResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", LeadRefreshPhase(self.phase))
        _validate_lead_counts(self)
        _validate_lead_source_results(self.source_results)


@dataclass(frozen=True, slots=True)
class LeadRefreshResult:
    status: LeadRefreshStatus
    requests: int = 0
    completed: int = 0
    discovered: int = 0
    unique: int = 0
    duplicates: int = 0
    resolved: int = 0
    needs_user: int = 0
    failed: int = 0
    public_reads: int = 0
    priorities_requested: int = 0
    priorities_refreshed: int = 0
    priorities_failed: int = 0
    truncated: bool = False
    source_results: tuple[LeadRefreshSourceResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", LeadRefreshStatus(self.status))
        _validate_lead_counts(self)
        _validate_lead_source_results(self.source_results)


def _validate_lead_counts(value: object) -> None:
    for name in (
        "requests",
        "completed",
        "discovered",
        "unique",
        "duplicates",
        "resolved",
        "needs_user",
        "failed",
        "public_reads",
        "priorities_requested",
        "priorities_refreshed",
        "priorities_failed",
    ):
        count = getattr(value, name)
        if type(count) is not int or count < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if getattr(value, "completed") > getattr(value, "requests"):
        raise ValueError("completed cannot exceed requests")
    if (
        getattr(value, "priorities_refreshed")
        + getattr(value, "priorities_failed")
        > getattr(value, "priorities_requested")
    ):
        raise ValueError("priority outcomes cannot exceed requested priorities")
    if type(getattr(value, "truncated")) is not bool:
        raise TypeError("truncated must be a bool")


def _validate_lead_source_results(
    source_results: tuple[LeadRefreshSourceResult, ...],
) -> None:
    if not isinstance(source_results, tuple) or any(
        not isinstance(item, LeadRefreshSourceResult) for item in source_results
    ):
        raise TypeError("source_results must contain typed lead source results")
    sources = tuple((item.source, item.family) for item in source_results)
    if len(sources) != len(set(sources)):
        raise ValueError("lead source results must be unique by source and family")


@dataclass(frozen=True, slots=True)
class RefreshJobLibraryUICommand:
    invocation_id: str
    max_reprioritizations: int | None = None

    def __post_init__(self) -> None:
        invocation_id = self.invocation_id.strip()
        if not invocation_id or len(invocation_id) > 240:
            raise ValueError("invocation_id is required")
        if self.max_reprioritizations is not None and (
            type(self.max_reprioritizations) is not int
            or self.max_reprioritizations < 1
        ):
            raise ValueError("max_reprioritizations must be positive or None")
        object.__setattr__(self, "invocation_id", invocation_id)


@dataclass(frozen=True, slots=True)
class RefreshJobLibraryUISummary:
    enabled_profiles: int = 0
    completed_profiles: int = 0
    searched_profiles: int = 0
    profiles_with_matches: int = 0
    zero_result_profiles: int = 0
    candidates_found: int = 0
    unique_candidates: int = 0
    candidates_processed: int = 0
    jobs_created: int = 0
    jobs_updated: int = 0
    jobs_unchanged: int = 0
    jobs_failed: int = 0
    jobs_skipped: int = 0
    priorities_requested: int = 0
    priorities_refreshed: int = 0
    priorities_failed: int = 0
    lead_requests: int = 0
    lead_requests_completed: int = 0
    leads_discovered: int = 0
    leads_unique: int = 0
    leads_deduplicated: int = 0
    leads_resolved: int = 0
    leads_needing_review: int = 0
    lead_failures: int = 0
    lead_public_reads: int = 0
    lead_search_truncated: bool = False
    lead_refresh_ran: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class RefreshJobLibraryUIResult:
    status: RefreshJobLibraryUIStatus
    invocation_id: str
    summary: RefreshJobLibraryUISummary
    phase: str | None = None
    source_results: tuple[dict[str, Any], ...] = ()
    priority_failures: tuple[dict[str, Any], ...] = ()
    source_failures: tuple[str, ...] = ()
    refresh_run_id: str | None = None
    last_completed_refresh_time: datetime | None = None
    replayed: bool = False
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "last_completed_refresh_time": (
                self.last_completed_refresh_time.isoformat()
                if self.last_completed_refresh_time
                else None
            ),
            "message": self.message,
            "phase": self.phase,
            "priority_failures": [
                dict(item) for item in self.priority_failures
            ],
            "refresh_run_id": self.refresh_run_id,
            "replayed": self.replayed,
            "source_results": [dict(item) for item in self.source_results],
            "source_failures": list(self.source_failures),
            "status": self.status.value,
            "summary": self.summary.to_dict(),
        }


class ManualRefreshCallable(Protocol):
    def __call__(
        self,
        command: ManualJobLibraryRefreshCommand,
        *,
        progress_observer: JobLibraryRefreshProgressObserver | None = None,
    ) -> Awaitable[ManualJobLibraryRefreshResult]: ...


class LeadRefreshProgressObserver(Protocol):
    def __call__(
        self, progress: LeadRefreshProgress
    ) -> object | Awaitable[object]: ...


class LeadRefreshCallable(Protocol):
    def __call__(
        self,
        *,
        subject_id: str,
        invocation_id: str,
        now: datetime,
        progress_observer: LeadRefreshProgressObserver | None = None,
    ) -> Awaitable[LeadRefreshResult]: ...


_SAFE_REASON_MESSAGES = {
    "PROFILE_SNAPSHOT_FAILED": "Enabled job-search configuration could not be read.",
    "PROFILE_SNAPSHOT_INVALID": "Job-search configuration is invalid.",
    "PRIORITY_REFRESH_FAILED": "Jobs were refreshed, but Priority could not be updated.",
    "REPOSITORY_FAILURE": "The refresh audit record is temporarily unavailable.",
    "REPLAY_INTEGRITY_FAILURE": "This refresh could not be resumed safely.",
    "SEARCH_FAILED": "This job source search failed.",
    "SEARCH_UNSUPPORTED": "This job source is not supported.",
    "SEARCH_RESULT_INVALID": "This job source returned an invalid result.",
    "SEARCH_EXCEPTION": "This job source is temporarily unavailable.",
    "INVALID_CANDIDATE_URL": "A discovered job has no readable URL.",
    "PUBLIC_READ_FAILED": "The public job record could not be read.",
    "PUBLIC_READ_RESULT_INVALID": "The public job record is invalid.",
    "PUBLIC_READ_EXCEPTION": "Reading the public job record failed.",
    "DISCOVERY_FAILED": "The job could not be added to the library.",
    "DISCOVERY_RESULT_INVALID": "The job library returned an invalid result.",
    "DISCOVERY_EXCEPTION": "Updating the job library failed.",
}

_SAFE_PRIORITY_FAILURE_MESSAGES = {
    "PRIORITY_REFRESH_FAILED": "Priority could not be updated.",
    "PROPOSAL_FAILED:AGENT_TIMEOUT": "Priority AI timed out.",
    "PROPOSAL_FAILED:AGENT_UNAVAILABLE": "Priority AI is unavailable.",
    "PROPOSAL_FAILED:AGENT_OUTPUT_INVALID": (
        "Priority AI output did not satisfy the Priority contract."
    ),
    "CANDIDATE_SUMMARY_UNAVAILABLE": "Priority has no usable candidate summary.",
    "ACTIVE_POLICY_NOT_FOUND": "Priority has no active approved preference policy.",
}

_LEAD_SOURCE_LABELS = {
    JobLeadSource.AUTHORIZED_WEB_SEARCH: "Authorized web search",
    JobLeadSource.LINKEDIN_ALERT_EMAIL: "LinkedIn job alerts",
    JobLeadSource.INDEED_ALERT_EMAIL: "Indeed job alerts",
    JobLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL: "Employer or ATS job alerts",
    JobLeadSource.WEB_CLIPPER: "JobOps Web Clipper",
    JobLeadSource.PASTED_URL: "Pasted job URLs",
}


def _safe_reason(reason: object | None) -> str:
    value = getattr(reason, "value", None)
    return _SAFE_REASON_MESSAGES.get(
        value, "The refresh did not complete. Try again later."
    )


def _ui_status(
    result: ManualJobLibraryRefreshResult,
) -> RefreshJobLibraryUIStatus:
    status = result.status
    if status is JobLibraryRefreshStatus.UNCHANGED and result.run is not None:
        status = result.run.overall_status
    return RefreshJobLibraryUIStatus(status.value)


def _source_results(
    profile_results: tuple,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "candidate_count": item.candidate_count,
            "profile_id": item.profile_id,
            "provider": item.source.kind.value,
            "source_id": item.source.source_id,
            "status": item.search_status.value,
            "message": (
                None
                if item.search_status is ProfileRefreshSearchStatus.SUCCEEDED
                else _safe_reason(item.reason)
            ),
        }
        for item in profile_results
    )


def _lead_source_results(
    source_results: tuple[LeadRefreshSourceResult, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "result_type": "JOB_LEAD",
            "provider": item.family or item.source.value,
            "source_id": item.family or item.source.value,
            "acquisition_source": item.source.value,
            "status": item.status.value,
            "requests": item.requests,
            "completed": item.completed,
            "search_hits": item.search_hits,
            "leads_discovered": item.discovered,
            "leads_unique": item.unique,
            "leads_deduplicated": item.duplicates,
            "leads_resolved": item.resolved,
            "leads_needing_review": item.needs_user,
            "lead_failures": item.failed,
            "public_reads": item.public_reads,
            "truncated": item.truncated,
        }
        for item in source_results
    )


def _summary_with_leads(
    base: RefreshJobLibraryUISummary,
    lead: LeadRefreshProgress | LeadRefreshResult,
) -> RefreshJobLibraryUISummary:
    values = {
        name: getattr(base, name) for name in base.__dataclass_fields__
    }
    values.update(
        {
            "lead_requests": lead.requests,
            "lead_requests_completed": lead.completed,
            "leads_discovered": lead.discovered,
            "leads_unique": lead.unique,
            "leads_deduplicated": lead.duplicates,
            "leads_resolved": lead.resolved,
            "leads_needing_review": lead.needs_user,
            "lead_failures": lead.failed,
            "lead_public_reads": lead.public_reads,
            "lead_search_truncated": lead.truncated,
            "lead_refresh_ran": True,
            "priorities_requested": (
                base.priorities_requested + lead.priorities_requested
            ),
            "priorities_refreshed": (
                base.priorities_refreshed + lead.priorities_refreshed
            ),
            "priorities_failed": (
                base.priorities_failed + lead.priorities_failed
            ),
        }
    )
    return RefreshJobLibraryUISummary(**values)


def _lead_progress_message(progress: LeadRefreshProgress) -> str:
    if progress.phase is LeadRefreshPhase.INGESTING_ALERTS:
        return (
            "Reading the explicitly enabled job-alert inbox and storing "
            "sanitized leads. Email content is not treated as a verified job."
        )
    if progress.phase is LeadRefreshPhase.DISCOVERING:
        return (
            f"Discovery channels found {progress.discovered} leads; "
            f"{progress.unique} remain after URL deduplication."
        )
    if progress.phase is LeadRefreshPhase.RESOLVING:
        return (
            f"Checking official employer or ATS postings: {progress.resolved} "
            f"verified and {progress.needs_user} still need review."
        )
    return (
        f"Lead discovery completed: {progress.resolved} verified official "
        f"postings and {progress.needs_user} leads need review."
    )


def map_lead_refresh_progress(
    progress: LeadRefreshProgress,
    *,
    invocation_id: str,
    provider_result: RefreshJobLibraryUIResult,
) -> RefreshJobLibraryUIResult:
    """Overlay lead progress without promoting unresolved leads to jobs."""

    if not isinstance(progress, LeadRefreshProgress):
        raise TypeError("progress must be typed")
    return RefreshJobLibraryUIResult(
        status=RefreshJobLibraryUIStatus.RUNNING,
        invocation_id=invocation_id,
        summary=_summary_with_leads(provider_result.summary, progress),
        phase=progress.phase.value,
        source_results=(
            provider_result.source_results
            + _lead_source_results(progress.source_results)
        ),
        priority_failures=provider_result.priority_failures,
        source_failures=provider_result.source_failures,
        refresh_run_id=provider_result.refresh_run_id,
        last_completed_refresh_time=provider_result.last_completed_refresh_time,
        replayed=provider_result.replayed,
        message=_lead_progress_message(progress),
    )


def _summary_from_progress(
    progress: JobLibraryRefreshProgress,
) -> RefreshJobLibraryUISummary:
    profiles = progress.profile_results
    discovery = progress.discovery_summary
    succeeded = tuple(
        item
        for item in profiles
        if item.search_status is ProfileRefreshSearchStatus.SUCCEEDED
    )
    return RefreshJobLibraryUISummary(
        enabled_profiles=progress.enabled_profiles,
        completed_profiles=len(profiles),
        searched_profiles=len(succeeded),
        profiles_with_matches=sum(item.candidate_count > 0 for item in succeeded),
        zero_result_profiles=sum(item.candidate_count == 0 for item in succeeded),
        candidates_found=sum(item.candidate_count for item in succeeded),
        unique_candidates=progress.unique_candidates,
        candidates_processed=progress.candidates_processed,
        jobs_created=discovery.created,
        jobs_updated=discovery.updated,
        jobs_unchanged=discovery.unchanged,
        jobs_failed=discovery.failed,
        jobs_skipped=discovery.skipped,
        priorities_requested=progress.priority_requested,
    )


def map_refresh_progress(
    progress: JobLibraryRefreshProgress,
    *,
    invocation_id: str,
) -> RefreshJobLibraryUIResult:
    summary = _summary_from_progress(progress)
    if progress.phase is JobLibraryRefreshProgressPhase.SEARCHING:
        message = (
            "Searching configured job sources: "
            f"{len(progress.profile_results)}/{progress.enabled_profiles} returned; "
            f"{summary.profiles_with_matches} found matches and "
            f"{summary.zero_result_profiles} returned zero."
        )
    elif progress.phase is JobLibraryRefreshProgressPhase.IMPORTING:
        message = (
            f"The configured sources returned {summary.unique_candidates} unique job URLs; "
            f"{summary.candidates_processed}/{summary.unique_candidates} "
            "have been added or checked."
        )
    else:
        scope = (
            "new or changed jobs"
            if progress.priority_scope
            is JobLibraryRefreshPriorityScope.NEW_OR_CHANGED
            else "existing jobs that need reevaluation"
        )
        message = (
            f"Job import is complete. Priority AI is evaluating up to "
            f"{progress.priority_requested} {scope} one at a time. "
            "This can take several minutes; the refresh is still running."
        )
    return RefreshJobLibraryUIResult(
        status=RefreshJobLibraryUIStatus.RUNNING,
        invocation_id=invocation_id,
        summary=summary,
        phase=progress.phase.value,
        source_results=_source_results(progress.profile_results),
        message=message,
    )


def map_manual_refresh_result(
    result: ManualJobLibraryRefreshResult,
    *,
    invocation_id: str,
) -> RefreshJobLibraryUIResult:
    """Project S3b output to a bounded, credential-safe UI result."""

    if not isinstance(result, ManualJobLibraryRefreshResult):
        return RefreshJobLibraryUIResult(
            RefreshJobLibraryUIStatus.FAILED,
            invocation_id,
            RefreshJobLibraryUISummary(),
            message="The refresh service returned an invalid result.",
        )
    run = result.run
    if run is None:
        return RefreshJobLibraryUIResult(
            RefreshJobLibraryUIStatus.FAILED,
            invocation_id,
            RefreshJobLibraryUISummary(),
            message=_safe_reason(result.reason),
        )

    discovery = run.discovery_summary
    priority = run.priority_summary
    summary = RefreshJobLibraryUISummary(
        enabled_profiles=len(run.profile_results),
        completed_profiles=len(run.profile_results),
        searched_profiles=sum(
            item.search_status is ProfileRefreshSearchStatus.SUCCEEDED
            for item in run.profile_results
        ),
        profiles_with_matches=sum(
            item.search_status is ProfileRefreshSearchStatus.SUCCEEDED
            and item.candidate_count > 0
            for item in run.profile_results
        ),
        zero_result_profiles=sum(
            item.search_status is ProfileRefreshSearchStatus.SUCCEEDED
            and item.candidate_count == 0
            for item in run.profile_results
        ),
        candidates_found=sum(
            item.candidate_count for item in run.profile_results
        ),
        unique_candidates=discovery.unique_candidates,
        candidates_processed=discovery.unique_candidates,
        jobs_created=discovery.created,
        jobs_updated=discovery.updated,
        jobs_unchanged=discovery.unchanged,
        jobs_failed=discovery.failed,
        jobs_skipped=discovery.skipped,
        priorities_requested=priority.requested if priority else 0,
        priorities_refreshed=(
            priority.created + priority.unchanged if priority else 0
        ),
        priorities_failed=priority.failed if priority else 0,
    )
    failures: list[str] = []
    for profile in run.profile_results:
        if profile.search_status is ProfileRefreshSearchStatus.SUCCEEDED:
            continue
        failures.append(
            f"{profile.source.kind.value}/{profile.source.source_id}: "
            f"{_safe_reason(profile.reason)}"
        )
    for candidate in run.candidate_results:
        if candidate.discovery_status not in {
            CandidateDiscoveryStatus.FAILED,
            CandidateDiscoveryStatus.SKIPPED,
        }:
            continue
        source_label = ", ".join(candidate.source_profile_ids)
        failures.append(
            f"Search configuration {source_label}: {_safe_reason(candidate.reason)}"
        )
    priority_counts = Counter(result.priority_failure_codes)
    if (
        priority is None
        and run.overall_status
        in {
            JobLibraryRefreshStatus.PARTIAL_FAILURE,
            JobLibraryRefreshStatus.FAILED,
        }
        and not priority_counts
    ):
        priority_counts["PRIORITY_REFRESH_FAILED"] = 1
    priority_failures = tuple(
        {
            "code": code,
            "count": count,
            "message": _SAFE_PRIORITY_FAILURE_MESSAGES.get(
                code, "Priority evaluation failed."
            ),
        }
        for code, count in sorted(priority_counts.items())
    )
    status = _ui_status(result)
    message = (
        "No enabled job-search configuration was found."
        if status is RefreshJobLibraryUIStatus.NOOP
        else None
    )
    return RefreshJobLibraryUIResult(
        status=status,
        invocation_id=invocation_id,
        summary=summary,
        source_results=_source_results(run.profile_results),
        priority_failures=priority_failures,
        source_failures=tuple(failures),
        refresh_run_id=run.run_id,
        last_completed_refresh_time=run.completed_at,
        replayed=result.status is JobLibraryRefreshStatus.UNCHANGED,
        message=message,
    )


def _combined_terminal_status(
    provider: RefreshJobLibraryUIStatus,
    lead: LeadRefreshStatus,
) -> RefreshJobLibraryUIStatus:
    if lead is LeadRefreshStatus.NOOP:
        return provider
    if provider is RefreshJobLibraryUIStatus.NOOP:
        return RefreshJobLibraryUIStatus(lead.value)
    if (
        provider is RefreshJobLibraryUIStatus.COMPLETED
        and lead is LeadRefreshStatus.COMPLETED
    ):
        return RefreshJobLibraryUIStatus.COMPLETED
    if (
        provider is RefreshJobLibraryUIStatus.FAILED
        and lead is LeadRefreshStatus.FAILED
    ):
        return RefreshJobLibraryUIStatus.FAILED
    if provider is RefreshJobLibraryUIStatus.IDLE:
        return RefreshJobLibraryUIStatus(lead.value)
    return RefreshJobLibraryUIStatus.PARTIAL_FAILURE


def merge_lead_refresh_result(
    provider_result: RefreshJobLibraryUIResult,
    lead_result: LeadRefreshResult,
    *,
    completed_at: datetime,
) -> RefreshJobLibraryUIResult:
    """Merge two source families while preserving their separate semantics."""

    if not isinstance(provider_result, RefreshJobLibraryUIResult):
        raise TypeError("provider_result must be typed")
    if not isinstance(lead_result, LeadRefreshResult):
        raise TypeError("lead_result must be typed")
    if completed_at.tzinfo is None or completed_at.utcoffset() is None:
        raise ValueError("completed_at must be timezone-aware")
    status = _combined_terminal_status(provider_result.status, lead_result.status)
    lead_failures = tuple(
        (
            f"{_LEAD_SOURCE_LABELS.get(item.source, 'Job lead source')}: "
            f"{item.failed} lead operation"
            f"{'s' if item.failed != 1 else ''} failed."
        )
        for item in lead_result.source_results
        if item.failed
    )
    if lead_result.failed and not lead_failures:
        lead_failures = (
            f"Job-lead discovery: {lead_result.failed} operation"
            f"{'s' if lead_result.failed != 1 else ''} failed.",
        )
    message: str | None = None
    if status is RefreshJobLibraryUIStatus.NOOP:
        message = "No enabled provider search or lead discovery source was found."
    elif status is RefreshJobLibraryUIStatus.FAILED:
        message = "Provider search and job-lead discovery did not complete."
    elif status is RefreshJobLibraryUIStatus.PARTIAL_FAILURE:
        source_failed = bool(provider_result.source_failures or lead_failures)
        priority_failed = bool(
            provider_result.priority_failures
            or provider_result.summary.priorities_failed
            or lead_result.priorities_failed
        )
        if priority_failed and not source_failed:
            message = (
                "Source search and the job-library update completed, but "
                "some Priority decisions need attention."
            )
        elif source_failed and priority_failed:
            message = (
                "The job-library refresh completed only in part; some source "
                "operations and Priority decisions need attention."
            )
        else:
            message = (
                "The job-library refresh completed only in part. Failed "
                "sources did not create verified jobs."
            )
    elif lead_result.truncated:
        message = (
            "Job-lead discovery reached its configured request or result "
            "limit; the completed results were kept."
        )
    return RefreshJobLibraryUIResult(
        status=status,
        invocation_id=provider_result.invocation_id,
        summary=_summary_with_leads(provider_result.summary, lead_result),
        phase=LeadRefreshPhase.COMPLETED.value,
        source_results=(
            provider_result.source_results
            + _lead_source_results(lead_result.source_results)
        ),
        priority_failures=provider_result.priority_failures,
        source_failures=provider_result.source_failures + lead_failures,
        refresh_run_id=provider_result.refresh_run_id,
        last_completed_refresh_time=(
            provider_result.last_completed_refresh_time
            if lead_result.status
            in {LeadRefreshStatus.NOOP, LeadRefreshStatus.FAILED}
            else completed_at
        ),
        replayed=provider_result.replayed,
        message=message,
    )


class RefreshJobLibraryUIController:
    """One authenticated action combining provider jobs and unverified leads."""

    def __init__(
        self,
        *,
        manual_refresh: ManualRefreshCallable,
        clock: Callable[[], datetime],
        max_reprioritizations: int | None = None,
        lead_refresh: LeadRefreshCallable | None = None,
    ) -> None:
        if max_reprioritizations is not None and (
            type(max_reprioritizations) is not int
            or max_reprioritizations < 0
        ):
            raise ValueError("max_reprioritizations must be non-negative")
        self._manual_refresh = manual_refresh
        self._lead_refresh = lead_refresh
        self._clock = clock
        self._max_reprioritizations = max_reprioritizations
        self._active: dict[
            str, tuple[str, asyncio.Task[RefreshJobLibraryUIResult]]
        ] = {}
        self._last_result: dict[str, RefreshJobLibraryUIResult] = {}
        self._progress: dict[str, RefreshJobLibraryUIResult] = {}

    async def start(
        self,
        *,
        context: AuthenticatedSubjectContext,
        command: RefreshJobLibraryUICommand,
    ) -> RefreshJobLibraryUIResult:
        """Start one refresh and return immediately with a pollable state."""

        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if not isinstance(command, RefreshJobLibraryUICommand):
            raise TypeError("command must be typed")

        active = self._active.get(context.subject_id)
        if active is not None:
            active_invocation, task = active
            if task.done():
                result = await asyncio.shield(task)
                self._last_result[context.subject_id] = result
                self._active.pop(context.subject_id, None)
            else:
                return self._progress.get(
                    context.subject_id,
                    RefreshJobLibraryUIResult(
                        RefreshJobLibraryUIStatus.RUNNING,
                        active_invocation,
                        RefreshJobLibraryUISummary(),
                        message="A job-library refresh is already running.",
                    ),
                )

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        task = asyncio.create_task(
            self._invoke(
                subject_id=context.subject_id,
                command=command,
                now=now,
            )
        )
        self._active[context.subject_id] = (command.invocation_id, task)
        return RefreshJobLibraryUIResult(
            RefreshJobLibraryUIStatus.RUNNING,
            command.invocation_id,
            RefreshJobLibraryUISummary(),
            message="Job-library refresh started.",
        )

    async def status(
        self,
        *,
        context: AuthenticatedSubjectContext,
    ) -> RefreshJobLibraryUIResult:
        """Return the current or most recent subject-scoped refresh state."""

        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        active = self._active.get(context.subject_id)
        if active is None:
            return self._last_result.get(
                context.subject_id,
                RefreshJobLibraryUIResult(
                    RefreshJobLibraryUIStatus.IDLE,
                    "none",
                    RefreshJobLibraryUISummary(),
                    message="No job-library refresh has run in this server process.",
                ),
            )
        invocation_id, task = active
        if not task.done():
            return self._progress.get(
                context.subject_id,
                RefreshJobLibraryUIResult(
                    RefreshJobLibraryUIStatus.RUNNING,
                    invocation_id,
                    RefreshJobLibraryUISummary(),
                    message="Searching configured sources and updating the job library.",
                ),
            )
        result = await asyncio.shield(task)
        self._active.pop(context.subject_id, None)
        self._last_result[context.subject_id] = result
        return result

    async def refresh(
        self,
        *,
        context: AuthenticatedSubjectContext,
        command: RefreshJobLibraryUICommand,
    ) -> RefreshJobLibraryUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if not isinstance(command, RefreshJobLibraryUICommand):
            raise TypeError("command must be typed")

        active = self._active.get(context.subject_id)
        if active is not None:
            active_invocation, task = active
            if active_invocation == command.invocation_id:
                return await asyncio.shield(task)
            return RefreshJobLibraryUIResult(
                RefreshJobLibraryUIStatus.RUNNING,
                active_invocation,
                RefreshJobLibraryUISummary(),
                message="A job-library refresh is already running.",
            )

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        task = asyncio.create_task(
            self._invoke(
                subject_id=context.subject_id,
                command=command,
                now=now,
            )
        )
        self._active[context.subject_id] = (command.invocation_id, task)
        try:
            result = await asyncio.shield(task)
            self._last_result[context.subject_id] = result
            return result
        finally:
            current = self._active.get(context.subject_id)
            if current is not None and current[1] is task and task.done():
                self._active.pop(context.subject_id, None)

    async def _invoke(
        self,
        *,
        subject_id: str,
        command: RefreshJobLibraryUICommand,
        now: datetime,
    ) -> RefreshJobLibraryUIResult:
        s3b_command = ManualJobLibraryRefreshCommand(
            subject_id=subject_id,
            invocation_id=command.invocation_id,
            now=now,
            max_reprioritizations=(
                min(
                    command.max_reprioritizations,
                    self._max_reprioritizations,
                )
                if command.max_reprioritizations is not None
                and self._max_reprioritizations is not None
                else self._max_reprioritizations
                if self._max_reprioritizations is not None
                else command.max_reprioritizations
                if command.max_reprioritizations is not None
                else 10
            ),
        )
        try:
            async def observe(progress: JobLibraryRefreshProgress) -> None:
                self._progress[subject_id] = map_refresh_progress(
                    progress,
                    invocation_id=command.invocation_id,
                )

            result = await self._manual_refresh(
                s3b_command,
                progress_observer=observe,
            )
            provider_result = map_manual_refresh_result(
                result, invocation_id=command.invocation_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            provider_result = RefreshJobLibraryUIResult(
                RefreshJobLibraryUIStatus.FAILED,
                command.invocation_id,
                RefreshJobLibraryUISummary(),
                message="The refresh service is temporarily unavailable.",
            )
        if self._lead_refresh is None:
            self._progress.pop(subject_id, None)
            return provider_result

        self._progress[subject_id] = RefreshJobLibraryUIResult(
            RefreshJobLibraryUIStatus.RUNNING,
            command.invocation_id,
            provider_result.summary,
            phase=LeadRefreshPhase.DISCOVERING.value,
            source_results=provider_result.source_results,
            priority_failures=provider_result.priority_failures,
            source_failures=provider_result.source_failures,
            refresh_run_id=provider_result.refresh_run_id,
            last_completed_refresh_time=provider_result.last_completed_refresh_time,
            replayed=provider_result.replayed,
            message="Provider refresh finished. Discovering additional job leads.",
        )
        try:
            async def observe_leads(progress: LeadRefreshProgress) -> None:
                self._progress[subject_id] = map_lead_refresh_progress(
                    progress,
                    invocation_id=command.invocation_id,
                    provider_result=provider_result,
                )

            lead_result = await self._lead_refresh(
                subject_id=subject_id,
                invocation_id=command.invocation_id,
                now=now,
                progress_observer=observe_leads,
            )
            if not isinstance(lead_result, LeadRefreshResult):
                raise TypeError("lead refresh returned an invalid result")
        except (OSError, RuntimeError, TypeError, ValueError):
            lead_result = LeadRefreshResult(
                status=LeadRefreshStatus.FAILED,
                failed=1,
            )
        self._progress.pop(subject_id, None)
        return merge_lead_refresh_result(
            provider_result,
            lead_result,
            completed_at=self._clock(),
        )


__all__ = [
    "LeadRefreshCallable",
    "LeadRefreshPhase",
    "LeadRefreshProgress",
    "LeadRefreshProgressObserver",
    "LeadRefreshResult",
    "LeadRefreshSourceResult",
    "LeadRefreshSourceStatus",
    "LeadRefreshStatus",
    "ManualRefreshCallable",
    "RefreshJobLibraryUICommand",
    "RefreshJobLibraryUIController",
    "RefreshJobLibraryUIResult",
    "RefreshJobLibraryUIStatus",
    "RefreshJobLibraryUISummary",
    "map_manual_refresh_result",
    "map_lead_refresh_progress",
    "map_refresh_progress",
    "merge_lead_refresh_result",
]
