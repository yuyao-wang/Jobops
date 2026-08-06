"""Bounded multi-source discovery from search-index leads to verified jobs.

The orchestration in this module deliberately separates two trust levels:

* an authorized web-search hit is persisted as a :class:`JobLead`;
* only one verified employer/ATS :class:`SourceJobObservation` may cross the
  existing subject-aware Discovery boundary.

LinkedIn, Indeed, and Glassdoor are discovery indexes here.  Their pages are
never opened by the public reader.  They can only contribute hints used to
locate an official employer or ATS posting.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from core.job_discovery import (
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobIntakeIntent,
    JobIntakeProposal,
    ProposalResolution,
    ResolvedJobCandidate,
)
from core.job_leads import (
    JobLead,
    JobLeadListResult,
    JobLeadListStatus,
    JobLeadOrigin,
    JobLeadRepository,
    JobLeadSource,
    JobLeadStatus,
    JobLeadWriteResult,
    JobLeadWriteStatus,
    canonicalize_job_lead_url,
)
from core.job_search import (
    JobSearchPort,
    JobSearchRequest,
    JobSearchResult,
    JobSearchStatus,
)
from core.prioritization_policy import (
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
    SoftPreferenceCategory,
)
from core.subject_job_discovery import (
    SubjectJobDiscoveryCommand,
    SubjectJobDiscoveryResult,
    SubjectJobDiscoveryStatus,
)
from core.subject_job_library import SubjectJobMembershipSourceKind
from source_connectors.authorized_web_search import (
    AuthorizedWebSearchHit,
    AuthorizedWebSearchPort,
    AuthorizedWebSearchRequest,
    AuthorizedWebSearchResult,
    AuthorizedWebSearchStatus,
)
from source_connectors.contract import (
    PUBLIC_ATS_JOB_HOST_SUFFIXES,
    ReadJobRequest,
    ReadJobResult,
    ReadJobStatus,
    SourceJobObservation,
    WORKDAY_PUBLIC_JOB_HOST_SUFFIXES,
)


JOB_LEAD_DISCOVERY_CONTRACT_VERSION = "job-lead-discovery-v1"
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DAYS_RE = re.compile(r"\b(\d{1,3})\s*(?:day|days|d)\b", re.IGNORECASE)
_COUNTRY_RE = re.compile(r"[A-Z]{2}")
_LANGUAGE_RE = re.compile(r"[a-z]{2,5}")


class JobLeadDiscoverySource(StrEnum):
    LINKEDIN = "LINKEDIN"
    INDEED = "INDEED"
    GLASSDOOR = "GLASSDOOR"
    GREENHOUSE = "GREENHOUSE"
    LEVER = "LEVER"
    ASHBY = "ASHBY"
    JOBVITE = "JOBVITE"
    WORKDAY = "WORKDAY"
    SMARTRECRUITERS = "SMARTRECRUITERS"
    ICIMS = "ICIMS"
    SUCCESSFACTORS = "SUCCESSFACTORS"
    GENERIC_CAREERS = "GENERIC_CAREERS"
    CONFIGURED_PROVIDER_RESOLUTION = "CONFIGURED_PROVIDER_RESOLUTION"
    CANONICAL_WEB_RESOLUTION = "CANONICAL_WEB_RESOLUTION"


class JobLeadDiscoveryStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class JobLeadSourceRunStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    EMPTY = "EMPTY"
    PARTIAL = "PARTIAL"
    TRUNCATED = "TRUNCATED"
    FAILED = "FAILED"


class JobLeadDiscoveryPhase(StrEnum):
    SEARCHING = "SEARCHING"
    PERSISTING = "PERSISTING"
    RESOLVING = "RESOLVING"
    COMPLETED = "COMPLETED"


class JobLeadDiscoveryReason(StrEnum):
    INVALID_POLICY = "INVALID_POLICY"
    ROLE_PREFERENCE_REQUIRED = "ROLE_PREFERENCE_REQUIRED"
    LEAD_REPOSITORY_FAILURE = "LEAD_REPOSITORY_FAILURE"
    SEARCH_FAILED = "SEARCH_FAILED"
    SEARCH_PARTIAL = "SEARCH_PARTIAL"
    PUBLIC_READ_FAILED = "PUBLIC_READ_FAILED"
    AMBIGUOUS_OFFICIAL_POSTING = "AMBIGUOUS_OFFICIAL_POSTING"
    OFFICIAL_POSTING_NOT_FOUND = "OFFICIAL_POSTING_NOT_FOUND"
    SOURCE_REQUIRES_USER = "SOURCE_REQUIRES_USER"
    DISCOVERY_NOT_ACCEPTED = "DISCOVERY_NOT_ACCEPTED"
    REQUEST_BUDGET_EXHAUSTED = "REQUEST_BUDGET_EXHAUSTED"
    RESULT_BUDGET_EXHAUSTED = "RESULT_BUDGET_EXHAUSTED"
    PUBLIC_READ_BUDGET_EXHAUSTED = "PUBLIC_READ_BUDGET_EXHAUSTED"


_SEARCH_SPECS: tuple[tuple[JobLeadDiscoverySource, str], ...] = (
    (JobLeadDiscoverySource.LINKEDIN, "site:linkedin.com/jobs/view"),
    (JobLeadDiscoverySource.INDEED, "site:indeed.com/viewjob"),
    (
        JobLeadDiscoverySource.GLASSDOOR,
        "(site:glassdoor.com/job-listing OR site:glassdoor.ca/job-listing)",
    ),
    (
        JobLeadDiscoverySource.GREENHOUSE,
        "(site:boards.greenhouse.io OR site:job-boards.greenhouse.io)",
    ),
    (JobLeadDiscoverySource.LEVER, "site:jobs.lever.co"),
    (JobLeadDiscoverySource.ASHBY, "site:jobs.ashbyhq.com"),
    (JobLeadDiscoverySource.JOBVITE, "site:jobs.jobvite.com"),
    (
        JobLeadDiscoverySource.WORKDAY,
        "(site:myworkdayjobs.com OR site:myworkdaysite.com OR "
        "site:workdayjobs.com)",
    ),
    (
        JobLeadDiscoverySource.SMARTRECRUITERS,
        "site:jobs.smartrecruiters.com",
    ),
    (JobLeadDiscoverySource.ICIMS, "site:icims.com/jobs"),
    (
        JobLeadDiscoverySource.SUCCESSFACTORS,
        "(site:successfactors.com OR site:successfactors.eu)",
    ),
    (
        JobLeadDiscoverySource.GENERIC_CAREERS,
        '(inurl:careers OR inurl:jobs OR intitle:"careers")',
    ),
)


@dataclass(frozen=True, slots=True)
class JobLeadDiscoveryCommand:
    subject_id: str
    invocation_id: str
    now: datetime
    count: int = 20
    offsets: tuple[int, ...] = (0,)
    country: str = "CA"
    search_language: str = "en"
    default_lookback_days: int | None = None
    max_requests: int = 240
    max_initial_requests: int | None = None
    max_canonical_searches: int = 20
    max_public_reads: int = 500
    max_hits: int = 5_000
    max_unique_leads: int = 5_000
    max_resolution_candidates: int = 20

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("invocation_id", self.invocation_id, 160),
        ):
            if (
                not isinstance(value, str)
                or value != value.strip()
                or not value
                or len(value) > maximum
            ):
                raise ValueError(f"{name} is outside the discovery contract")
        if not isinstance(self.now, datetime) or self.now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if type(self.count) is not int or not 1 <= self.count <= 20:
            raise ValueError("count must be between 1 and 20")
        if (
            not isinstance(self.offsets, tuple)
            or not self.offsets
            or any(type(offset) is not int or not 0 <= offset <= 9 for offset in self.offsets)
            or len(self.offsets) != len(set(self.offsets))
        ):
            raise ValueError("offsets must be unique page offsets from zero to nine")
        if tuple(sorted(self.offsets)) != self.offsets:
            raise ValueError("offsets must be in ascending order")
        if not isinstance(self.country, str) or _COUNTRY_RE.fullmatch(
            self.country
        ) is None:
            raise ValueError("country must be an uppercase ISO alpha-2 code")
        if not isinstance(
            self.search_language, str
        ) or _LANGUAGE_RE.fullmatch(self.search_language) is None:
            raise ValueError("search_language is invalid")
        if self.default_lookback_days is not None and (
            type(self.default_lookback_days) is not int
            or not 1 <= self.default_lookback_days <= 365
        ):
            raise ValueError("default_lookback_days is invalid")
        for name, value, lower, upper in (
            ("max_requests", self.max_requests, 1, 500),
            ("max_hits", self.max_hits, 1, 10_000),
            ("max_unique_leads", self.max_unique_leads, 1, 10_000),
            (
                "max_resolution_candidates",
                self.max_resolution_candidates,
                1,
                100,
            ),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} is outside the discovery contract")
        if self.max_initial_requests is not None and (
            type(self.max_initial_requests) is not int
            or not 1 <= self.max_initial_requests <= self.max_requests
        ):
            raise ValueError(
                "max_initial_requests must fit within the total request budget"
            )
        if (
            type(self.max_canonical_searches) is not int
            or not 0 <= self.max_canonical_searches <= 500
        ):
            raise ValueError("max_canonical_searches is outside the discovery contract")
        if type(self.max_public_reads) is not int or not 0 <= self.max_public_reads <= 10_000:
            raise ValueError("max_public_reads is outside the discovery contract")


@dataclass(frozen=True, slots=True)
class PlannedWebSearchRequest:
    source: JobLeadDiscoverySource
    request: AuthorizedWebSearchRequest


@dataclass(frozen=True, slots=True)
class JobLeadDiscoveryRequestPlan:
    requests: tuple[PlannedWebSearchRequest, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class JobLeadSourceRunResult:
    source: JobLeadDiscoverySource
    status: JobLeadSourceRunStatus
    requests: int
    completed: int
    hits: int
    unique: int
    duplicates: int
    resolved: int
    needs_user: int
    failed: int
    rejected_hits: int
    public_reads: int
    truncated: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", JobLeadDiscoverySource(self.source))
        object.__setattr__(self, "status", JobLeadSourceRunStatus(self.status))
        for name in (
            "requests",
            "completed",
            "hits",
            "unique",
            "duplicates",
            "resolved",
            "needs_user",
            "failed",
            "rejected_hits",
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
class JobLeadDiscoveryProgress:
    phase: JobLeadDiscoveryPhase
    source: JobLeadDiscoverySource | None
    query_id: str | None
    requests: int
    completed: int
    hits: int
    unique: int
    duplicates: int
    resolved: int
    needs_user: int
    failed: int
    truncated: bool
    canonical_searches: int = 0
    public_reads: int = 0
    source_results: tuple[JobLeadSourceRunResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", JobLeadDiscoveryPhase(self.phase))
        if self.source is not None:
            object.__setattr__(
                self, "source", JobLeadDiscoverySource(self.source)
            )
        for name in (
            "requests",
            "completed",
            "hits",
            "unique",
            "duplicates",
            "resolved",
            "needs_user",
            "failed",
            "canonical_searches",
            "public_reads",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")
        if not isinstance(self.source_results, tuple) or any(
            not isinstance(result, JobLeadSourceRunResult)
            for result in self.source_results
        ):
            raise TypeError("source_results must be typed")


@dataclass(frozen=True, slots=True)
class JobLeadDiscoveryRunSummary:
    status: JobLeadDiscoveryStatus
    subject_id: str
    invocation_id: str
    requests: int
    completed: int
    hits: int
    unique: int
    duplicates: int
    resolved: int
    needs_user: int
    failed: int
    truncated: bool
    canonical_searches: int
    public_reads: int
    resolved_job_ids: tuple[str, ...]
    source_results: tuple[JobLeadSourceRunResult, ...]
    reason_codes: tuple[JobLeadDiscoveryReason, ...] = ()
    contract_version: str = JOB_LEAD_DISCOVERY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", JobLeadDiscoveryStatus(self.status))
        if self.contract_version != JOB_LEAD_DISCOVERY_CONTRACT_VERSION:
            raise ValueError("unsupported JobLead discovery contract")
        for name, value in (
            ("subject_id", self.subject_id),
            ("invocation_id", self.invocation_id),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} is invalid")
        for name in (
            "requests",
            "completed",
            "hits",
            "unique",
            "duplicates",
            "resolved",
            "needs_user",
            "failed",
            "canonical_searches",
            "public_reads",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.completed > self.requests:
            raise ValueError("completed cannot exceed requests")
        if type(self.truncated) is not bool:
            raise TypeError("truncated must be a bool")
        if not isinstance(self.resolved_job_ids, tuple) or any(
            not isinstance(job_id, str) or not job_id
            for job_id in self.resolved_job_ids
        ):
            raise TypeError("resolved_job_ids must be a tuple of IDs")
        if not isinstance(self.source_results, tuple) or any(
            not isinstance(result, JobLeadSourceRunResult)
            for result in self.source_results
        ):
            raise TypeError("source_results must be typed")
        if not isinstance(self.reason_codes, tuple):
            raise TypeError("reason_codes must be a tuple")
        typed_reasons = tuple(
            JobLeadDiscoveryReason(reason) for reason in self.reason_codes
        )
        if len(typed_reasons) != len(set(typed_reasons)):
            raise ValueError("reason_codes must be unique")
        object.__setattr__(self, "reason_codes", typed_reasons)


class PublicJobReaderCallable(Protocol):
    def __call__(
        self, request: ReadJobRequest
    ) -> ReadJobResult | Awaitable[ReadJobResult]: ...


class SubjectJobDiscoveryCallable(Protocol):
    def __call__(
        self, command: SubjectJobDiscoveryCommand
    ) -> SubjectJobDiscoveryResult | Awaitable[SubjectJobDiscoveryResult]: ...


class JobLeadDiscoveryProgressObserver(Protocol):
    def __call__(
        self, progress: JobLeadDiscoveryProgress
    ) -> object | Awaitable[object]: ...


@dataclass(slots=True)
class _Counts:
    requests: int = 0
    completed: int = 0
    hits: int = 0
    unique: int = 0
    duplicates: int = 0
    resolved: int = 0
    needs_user: int = 0
    failed: int = 0
    rejected_hits: int = 0
    initial_requests: int = 0
    canonical_searches: int = 0
    public_reads: int = 0
    truncated: bool = False
    partial: bool = False


@dataclass(frozen=True, slots=True)
class _ReadAttempt:
    observation: SourceJobObservation | None
    attempted: bool
    failed: bool
    budget_exhausted: bool = False


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _literal(value: str, *, max_words: int = 10, max_chars: int = 120) -> str:
    cleaned = " ".join(value.replace('"', " ").split())
    words = cleaned.split()[:max_words]
    return " ".join(words)[:max_chars].strip()


def _category_terms(
    policy: PrioritizationPolicy,
    category: SoftPreferenceCategory,
) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    for preference in policy.soft_preferences:
        if preference.category is not category:
            continue
        value = _literal(preference.statement)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
    return tuple(output)


def _freshness_clause(policy: PrioritizationPolicy, *, now: datetime) -> str:
    values = _category_terms(policy, SoftPreferenceCategory.FRESHNESS)
    if not values:
        return ""
    text = values[0]
    lowered = text.casefold()
    match = _DAYS_RE.search(text)
    days: int | None = int(match.group(1)) if match else None
    if days is None:
        if "today" in lowered:
            days = 1
        elif "week" in lowered:
            days = 7
        elif "month" in lowered:
            days = 30
    if days is not None:
        days = max(1, min(days, 365))
        cutoff = (_utc(now) - timedelta(days=days)).date().isoformat()
        return f"after:{cutoff}"
    return f'"{_literal(text, max_words=6, max_chars=80)}"'


def _or_phrases(values: Iterable[str]) -> str:
    phrases = [f'"{value}"' for value in values if value]
    if len(phrases) == 1:
        return phrases[0]
    return "(" + " OR ".join(phrases) + ")"


def _role_batches(roles: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    # Three bounded literals keep the provider query below its 50-word limit.
    return tuple(tuple(roles[index : index + 3]) for index in range(0, len(roles), 3))


def _query_for(
    *,
    role_batch: tuple[str, ...],
    locations: tuple[str, ...],
    freshness: str,
    source_clause: str,
) -> str:
    pieces = [_or_phrases(role_batch)]
    if locations:
        pieces.append(_or_phrases(locations[:3]))
    if freshness:
        pieces.append(freshness)
    pieces.append(source_clause)
    query = " ".join(pieces)
    # The inputs are bounded above; this final defensive fallback drops only
    # optional clauses and never the role or selected source.
    if len(query) > 400 or len(query.split()) > 50:
        query = " ".join((_or_phrases(role_batch), freshness, source_clause)).strip()
    if len(query) > 400 or len(query.split()) > 50:
        query = " ".join((f'"{_literal(role_batch[0], max_words=6)}"', source_clause))
    return query


def build_authorized_web_search_requests(
    command: JobLeadDiscoveryCommand,
    policy: PrioritizationPolicy,
) -> JobLeadDiscoveryRequestPlan:
    """Build stable, bounded search requests from approved soft preferences."""

    if (
        not isinstance(policy, PrioritizationPolicy)
        or policy.status is not PrioritizationPolicyStatus.ACTIVE
        or policy.subject_id != command.subject_id
    ):
        return JobLeadDiscoveryRequestPlan((), False)
    roles = _category_terms(policy, SoftPreferenceCategory.ROLE)
    if not roles:
        return JobLeadDiscoveryRequestPlan((), False)
    locations = _category_terms(policy, SoftPreferenceCategory.LOCATION)
    freshness = _freshness_clause(policy, now=command.now)
    if not freshness and command.default_lookback_days is not None:
        cutoff = (
            _utc(command.now)
            - timedelta(days=command.default_lookback_days)
        ).date().isoformat()
        freshness = f"after:{cutoff}"
    planned: list[PlannedWebSearchRequest] = []
    all_specs = (
        (source, clause, role_batch, offset)
        for role_batch in _role_batches(roles)
        for offset in command.offsets
        for source, clause in _SEARCH_SPECS
    )
    truncated = False
    initial_request_limit = (
        command.max_initial_requests
        if command.max_initial_requests is not None
        else command.max_requests
    )
    for source, clause, role_batch, offset in all_specs:
        if len(planned) >= initial_request_limit:
            truncated = True
            break
        query = _query_for(
            role_batch=role_batch,
            locations=locations,
            freshness=freshness,
            source_clause=clause,
        )
        identity = "|".join(
            (
                command.invocation_id,
                source.value,
                str(offset),
                query,
            )
        )
        request = AuthorizedWebSearchRequest(
            query_id=f"lead-search-{_digest(identity)[:48]}",
            query=query,
            count=command.count,
            offset=offset,
            country=command.country,
            search_language=command.search_language,
        )
        planned.append(PlannedWebSearchRequest(source, request))
    return JobLeadDiscoveryRequestPlan(tuple(planned), truncated)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").casefold().rstrip(".")


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _is_linkedin(url: str) -> bool:
    return _host_is(_host(url), "linkedin.com")


def _is_indeed(url: str) -> bool:
    return _host_is(_host(url), "indeed.com")


def _is_glassdoor(url: str) -> bool:
    host = _host(url)
    return _host_is(host, "glassdoor.com") or _host_is(host, "glassdoor.ca")


def _is_aggregator(url: str) -> bool:
    return _is_linkedin(url) or _is_indeed(url) or _is_glassdoor(url)


def classify_job_lead_origin(url: str) -> JobLeadOrigin:
    """Classify a search URL without treating an index as authoritative."""

    canonical = canonicalize_job_lead_url("url", url)
    host = _host(canonical)
    if _is_linkedin(canonical):
        return JobLeadOrigin.LINKEDIN_SEARCH_INDEX
    if _is_indeed(canonical):
        return JobLeadOrigin.INDEED_SEARCH_INDEX
    if _is_glassdoor(canonical):
        return JobLeadOrigin.GLASSDOOR_SEARCH_INDEX
    if any(
        _host_is(host, domain) for domain in PUBLIC_ATS_JOB_HOST_SUFFIXES
    ):
        return JobLeadOrigin.ATS
    # A careers-looking path is not proof that the host belongs to the
    # employer.  Search results on every other web host remain unverified
    # until a user explicitly supplies the official destination or a known
    # ATS source verifies it.
    return JobLeadOrigin.UNKNOWN_WEB


def _hints_from_hit(
    hit: AuthorizedWebSearchHit,
    *,
    fallback_location: str | None,
) -> tuple[str | None, str | None, str | None]:
    title = " ".join(hit.title.split())
    title = re.sub(
        r"\s*(?:\||-)\s*(?:LinkedIn|Indeed|Glassdoor)\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    company: str | None = None
    role: str | None = None
    hiring = re.match(r"^(.+?)\s+hiring\s+(.+?)(?:\s+in\s+.+)?$", title, re.I)
    if hiring:
        company, role = hiring.group(1).strip(), hiring.group(2).strip()
    elif " - " in title:
        parts = [part.strip() for part in title.split(" - ") if part.strip()]
        if len(parts) >= 2:
            role, company = parts[0], parts[1]
    elif " at " in title.casefold():
        role, company = re.split(r"\s+at\s+", title, maxsplit=1, flags=re.I)
        role, company = role.strip(), company.strip()
    else:
        role = title or None
    return role, company, fallback_location


def _observation_candidate(observation: SourceJobObservation) -> ResolvedJobCandidate:
    return ResolvedJobCandidate(
        source_platform=observation.source_platform.value,
        source_url=observation.source_url,
        company=observation.company,
        title=observation.title,
        description=observation.description,
        source_job_id=observation.source_job_id,
        application_url=observation.application_url,
        location=observation.location,
        work_mode=observation.work_mode.value,
        posted_at=observation.posted_at,
        ats_type=observation.ats_type.value,
    )


def _discovery_request(
    command: JobLeadDiscoveryCommand,
    lead: JobLead,
    observation: SourceJobObservation,
) -> JobDiscoveryRequest:
    identity = _digest(f"{command.subject_id}|{lead.lead_id}|{observation.source_url}")
    return JobDiscoveryRequest(
        request_id=f"job-lead-discovery-request-{identity}",
        trigger=DiscoveryTrigger.MANUAL_LIBRARY_REFRESH,
        proposal=JobIntakeProposal(
            proposal_id=f"job-lead-discovery-proposal-{identity}",
            intent=JobIntakeIntent.ADD_JOB,
            resolution=ProposalResolution.RESOLVED,
            resolved_candidate=_observation_candidate(observation),
        ),
    )


def _normalized_hint(value: str) -> str:
    return " ".join(_TOKEN_RE.findall(value.casefold()))


def _hint_matches(expected: str, actual: str) -> bool:
    left = _normalized_hint(expected)
    right = _normalized_hint(actual)
    return bool(left and right and (left == right or left in right or right in left))


async def _await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _notify(
    observer: JobLeadDiscoveryProgressObserver | None,
    phase: JobLeadDiscoveryPhase,
    source: JobLeadDiscoverySource | None,
    query_id: str | None,
    total: _Counts,
    buckets: dict[JobLeadDiscoverySource, _Counts],
) -> None:
    if observer is None:
        return
    progress = JobLeadDiscoveryProgress(
        phase=phase,
        source=source,
        query_id=query_id,
        requests=total.requests,
        completed=total.completed,
        hits=total.hits,
        unique=total.unique,
        duplicates=total.duplicates,
        resolved=total.resolved,
        needs_user=total.needs_user,
        failed=total.failed,
        truncated=total.truncated,
        canonical_searches=total.canonical_searches,
        public_reads=total.public_reads,
        source_results=_source_results(buckets),
    )
    try:
        await _await(observer(progress))
    except (OSError, RuntimeError, TypeError, ValueError):
        return


def _bucket(
    buckets: dict[JobLeadDiscoverySource, _Counts],
    source: JobLeadDiscoverySource,
) -> _Counts:
    return buckets.setdefault(source, _Counts())


def _increment(
    total: _Counts,
    source: _Counts,
    name: str,
    amount: int = 1,
) -> None:
    setattr(total, name, getattr(total, name) + amount)
    setattr(source, name, getattr(source, name) + amount)


async def _search(
    *,
    port: AuthorizedWebSearchPort,
    request: AuthorizedWebSearchRequest,
    source: JobLeadDiscoverySource,
    command: JobLeadDiscoveryCommand,
    total: _Counts,
    buckets: dict[JobLeadDiscoverySource, _Counts],
    observer: JobLeadDiscoveryProgressObserver | None,
) -> AuthorizedWebSearchResult | None:
    source_counts = _bucket(buckets, source)
    is_canonical = source is JobLeadDiscoverySource.CANONICAL_WEB_RESOLUTION
    initial_limit = (
        command.max_initial_requests
        if command.max_initial_requests is not None
        else command.max_requests
    )
    budget_exhausted = (
        total.canonical_searches >= command.max_canonical_searches
        if is_canonical
        else total.initial_requests >= initial_limit
    )
    if budget_exhausted:
        total.truncated = True
        source_counts.truncated = True
        return None
    _increment(total, source_counts, "requests")
    if is_canonical:
        total.canonical_searches += 1
    else:
        total.initial_requests += 1
    await _notify(
        observer,
        JobLeadDiscoveryPhase.SEARCHING,
        source,
        request.query_id,
        total,
        buckets,
    )
    try:
        result = await port.search(request)
    except (OSError, RuntimeError, TypeError, ValueError):
        _increment(total, source_counts, "failed")
        total.partial = source_counts.partial = True
        await _notify(
            observer,
            JobLeadDiscoveryPhase.SEARCHING,
            source,
            request.query_id,
            total,
            buckets,
        )
        return None
    if not isinstance(result, AuthorizedWebSearchResult):
        _increment(total, source_counts, "failed")
        total.partial = source_counts.partial = True
        await _notify(
            observer,
            JobLeadDiscoveryPhase.SEARCHING,
            source,
            request.query_id,
            total,
            buckets,
        )
        return None
    _increment(total, source_counts, "completed")
    if result.status is AuthorizedWebSearchStatus.FAILED:
        _increment(total, source_counts, "failed")
        total.partial = source_counts.partial = True
    else:
        _increment(total, source_counts, "hits", len(result.hits))
        if result.status is AuthorizedWebSearchStatus.PARTIAL:
            total.partial = source_counts.partial = True
            _increment(
                total,
                source_counts,
                "rejected_hits",
                result.rejected_hit_count,
            )
        if len(result.hits) >= request.count and request.offset == max(command.offsets):
            total.truncated = True
            source_counts.truncated = True
    await _notify(
        observer,
        JobLeadDiscoveryPhase.SEARCHING,
        source,
        request.query_id,
        total,
        buckets,
    )
    return result


async def _read_observation(
    url: str,
    *,
    reader: PublicJobReaderCallable,
    command: JobLeadDiscoveryCommand,
    total: _Counts,
    counts: _Counts,
) -> _ReadAttempt:
    if _is_aggregator(url):
        return _ReadAttempt(None, False, False)
    origin = classify_job_lead_origin(url)
    if origin is not JobLeadOrigin.ATS:
        return _ReadAttempt(None, False, False)
    if total.public_reads >= command.max_public_reads:
        total.truncated = counts.truncated = True
        return _ReadAttempt(None, False, False, budget_exhausted=True)
    total.public_reads += 1
    counts.public_reads += 1
    try:
        result = await _await(reader(ReadJobRequest(url=url)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return _ReadAttempt(None, True, True)
    if (
        not isinstance(result, ReadJobResult)
        or result.status is not ReadJobStatus.SUCCEEDED
        or not isinstance(result.observation, SourceJobObservation)
    ):
        return _ReadAttempt(None, True, True)
    return _ReadAttempt(result.observation, True, False)


def _record_read_failure(
    *,
    total: _Counts,
    counts: _Counts,
) -> None:
    _increment(total, counts, "failed")
    total.partial = counts.partial = True


def _resolution_query(lead: JobLead) -> str:
    assert lead.title_hint is not None
    assert lead.company_hint is not None
    role = _literal(lead.title_hint, max_words=10)
    company = _literal(lead.company_hint, max_words=8)
    return (
        f'"{role}" "{company}" '
        "(site:greenhouse.io OR site:lever.co OR site:ashbyhq.com OR "
        "site:jobvite.com OR site:myworkdayjobs.com OR "
        "site:myworkdaysite.com OR site:workdayjobs.com OR "
        "site:smartrecruiters.com OR site:icims.com OR "
        "site:successfactors.com OR site:successfactors.eu OR inurl:careers)"
    )


async def _resolve_aggregator(
    lead: JobLead,
    *,
    command: JobLeadDiscoveryCommand,
    web_search: AuthorizedWebSearchPort | None,
    public_job_reader: PublicJobReaderCallable,
    configured_job_search: JobSearchPort | None,
    total: _Counts,
    buckets: dict[JobLeadDiscoverySource, _Counts],
    reasons: set[JobLeadDiscoveryReason],
    observer: JobLeadDiscoveryProgressObserver | None,
) -> tuple[SourceJobObservation, ...]:
    if (
        lead.title_hint is None
        or lead.company_hint is None
        or (web_search is None and configured_job_search is None)
    ):
        return ()
    observations: dict[str, SourceJobObservation] = {}
    public_read_budget_exhausted = False
    if configured_job_search is not None and total.requests < command.max_requests:
        counts = _bucket(
            buckets, JobLeadDiscoverySource.CONFIGURED_PROVIDER_RESOLUTION
        )
        _increment(total, counts, "requests")
        try:
            result = await configured_job_search.search(
                JobSearchRequest(
                    request_id=f"lead-provider-resolve-{_digest(lead.lead_id)[:48]}",
                    company=lead.company_hint,
                    title=lead.title_hint,
                    location=lead.location_hint,
                    result_limit=command.max_resolution_candidates,
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            result = None
        if not isinstance(result, JobSearchResult):
            _increment(total, counts, "failed")
            total.partial = counts.partial = True
        else:
            _increment(total, counts, "completed")
            if result.status is JobSearchStatus.SUCCEEDED and result.candidate_set:
                candidates = result.candidate_set.candidates[
                    : command.max_resolution_candidates
                ]
                _increment(total, counts, "hits", len(candidates))
                for candidate in candidates:
                    observation = candidate.observation
                    if observation is None:
                        attempt = await _read_observation(
                            candidate.source_url,
                            reader=public_job_reader,
                            command=command,
                            total=total,
                            counts=counts,
                        )
                        observation = attempt.observation
                        if attempt.budget_exhausted:
                            public_read_budget_exhausted = True
                            reasons.add(
                                JobLeadDiscoveryReason.PUBLIC_READ_BUDGET_EXHAUSTED
                            )
                            break
                        if attempt.failed:
                            _record_read_failure(total=total, counts=counts)
                            reasons.add(JobLeadDiscoveryReason.PUBLIC_READ_FAILED)
                    if (
                        observation is not None
                        and _hint_matches(lead.company_hint, observation.company)
                        and _hint_matches(lead.title_hint, observation.title)
                    ):
                        observations[observation.source_url] = observation
            elif result.status is not JobSearchStatus.UNSUPPORTED:
                _increment(total, counts, "failed")
                total.partial = counts.partial = True
    elif configured_job_search is not None:
        total.truncated = True
        _bucket(
            buckets, JobLeadDiscoverySource.CONFIGURED_PROVIDER_RESOLUTION
        ).truncated = True

    if web_search is None or command.max_canonical_searches == 0:
        return () if public_read_budget_exhausted else tuple(observations.values())

    query = _resolution_query(lead)
    request = AuthorizedWebSearchRequest(
        query_id=f"lead-canonical-{_digest(lead.lead_id)[:48]}",
        query=query,
        count=min(command.count, command.max_resolution_candidates, 20),
        offset=0,
        country=command.country,
        search_language=command.search_language,
    )
    result = await _search(
        port=web_search,
        request=request,
        source=JobLeadDiscoverySource.CANONICAL_WEB_RESOLUTION,
        command=command,
        total=total,
        buckets=buckets,
        observer=observer,
    )
    if (
        result is None
        and _bucket(
            buckets, JobLeadDiscoverySource.CANONICAL_WEB_RESOLUTION
        ).truncated
    ):
        reasons.add(JobLeadDiscoveryReason.REQUEST_BUDGET_EXHAUSTED)
    if result is not None and result.status is not AuthorizedWebSearchStatus.FAILED:
        counts = _bucket(buckets, JobLeadDiscoverySource.CANONICAL_WEB_RESOLUTION)
        for hit in result.hits[: command.max_resolution_candidates]:
            attempt = await _read_observation(
                hit.url,
                reader=public_job_reader,
                command=command,
                total=total,
                counts=counts,
            )
            observation = attempt.observation
            if attempt.budget_exhausted:
                public_read_budget_exhausted = True
                reasons.add(
                    JobLeadDiscoveryReason.PUBLIC_READ_BUDGET_EXHAUSTED
                )
                break
            if attempt.failed:
                _record_read_failure(total=total, counts=counts)
                reasons.add(JobLeadDiscoveryReason.PUBLIC_READ_FAILED)
            if (
                observation is not None
                and _hint_matches(lead.company_hint, observation.company)
                and _hint_matches(lead.title_hint, observation.title)
            ):
                observations[observation.source_url] = observation
    if public_read_budget_exhausted:
        return ()
    return tuple(observations.values())


def _source_status(counts: _Counts) -> JobLeadSourceRunStatus:
    if counts.completed == 0 and counts.failed:
        return JobLeadSourceRunStatus.FAILED
    if counts.partial or counts.failed:
        return JobLeadSourceRunStatus.PARTIAL
    if counts.truncated:
        return JobLeadSourceRunStatus.TRUNCATED
    if counts.resolved or counts.needs_user or counts.unique:
        return JobLeadSourceRunStatus.SUCCEEDED
    if counts.hits == 0:
        return JobLeadSourceRunStatus.EMPTY
    return JobLeadSourceRunStatus.SUCCEEDED


def _source_results(
    buckets: dict[JobLeadDiscoverySource, _Counts],
) -> tuple[JobLeadSourceRunResult, ...]:
    return tuple(
        JobLeadSourceRunResult(
            source=source,
            status=_source_status(counts),
            requests=counts.requests,
            completed=counts.completed,
            hits=counts.hits,
            unique=counts.unique,
            duplicates=counts.duplicates,
            resolved=counts.resolved,
            needs_user=counts.needs_user,
            failed=counts.failed,
            rejected_hits=counts.rejected_hits,
            public_reads=counts.public_reads,
            truncated=counts.truncated,
        )
        for source, counts in sorted(buckets.items(), key=lambda item: item[0].value)
    )


def _summary(
    command: JobLeadDiscoveryCommand,
    total: _Counts,
    buckets: dict[JobLeadDiscoverySource, _Counts],
    resolved_job_ids: list[str],
    reasons: set[JobLeadDiscoveryReason],
    *,
    force_failed: bool = False,
) -> JobLeadDiscoveryRunSummary:
    if force_failed or (total.completed == 0 and total.failed):
        status = JobLeadDiscoveryStatus.FAILED
    elif total.failed or total.partial or total.truncated:
        status = JobLeadDiscoveryStatus.PARTIAL
    else:
        status = JobLeadDiscoveryStatus.SUCCEEDED
    return JobLeadDiscoveryRunSummary(
        status=status,
        subject_id=command.subject_id,
        invocation_id=command.invocation_id,
        requests=total.requests,
        completed=total.completed,
        hits=total.hits,
        unique=total.unique,
        duplicates=total.duplicates,
        resolved=total.resolved,
        needs_user=total.needs_user,
        failed=total.failed,
        truncated=total.truncated,
        canonical_searches=total.canonical_searches,
        public_reads=total.public_reads,
        resolved_job_ids=tuple(dict.fromkeys(resolved_job_ids)),
        source_results=_source_results(buckets),
        reason_codes=tuple(sorted(reasons, key=lambda reason: reason.value)),
    )


def _source_for_lead(lead: JobLead) -> JobLeadDiscoverySource:
    if lead.origin is JobLeadOrigin.LINKEDIN_SEARCH_INDEX:
        return JobLeadDiscoverySource.LINKEDIN
    if lead.origin is JobLeadOrigin.INDEED_SEARCH_INDEX:
        return JobLeadDiscoverySource.INDEED
    host = _host(lead.source_url)
    if any(
        _host_is(host, domain)
        for domain in WORKDAY_PUBLIC_JOB_HOST_SUFFIXES
    ):
        return JobLeadDiscoverySource.WORKDAY
    domains = (
        ("greenhouse.io", JobLeadDiscoverySource.GREENHOUSE),
        ("lever.co", JobLeadDiscoverySource.LEVER),
        ("ashbyhq.com", JobLeadDiscoverySource.ASHBY),
        ("jobvite.com", JobLeadDiscoverySource.JOBVITE),
        ("smartrecruiters.com", JobLeadDiscoverySource.SMARTRECRUITERS),
        ("icims.com", JobLeadDiscoverySource.ICIMS),
        ("successfactors.com", JobLeadDiscoverySource.SUCCESSFACTORS),
        ("successfactors.eu", JobLeadDiscoverySource.SUCCESSFACTORS),
    )
    for domain, source in domains:
        if _host_is(host, domain):
            return source
    if _is_glassdoor(lead.source_url):
        return JobLeadDiscoverySource.GLASSDOOR
    return JobLeadDiscoverySource.GENERIC_CAREERS


async def _resolve_persisted_lead(
    lead: JobLead,
    *,
    source: JobLeadDiscoverySource,
    command: JobLeadDiscoveryCommand,
    web_search: AuthorizedWebSearchPort | None,
    lead_repository: JobLeadRepository,
    public_job_reader: PublicJobReaderCallable,
    subject_discovery: SubjectJobDiscoveryCallable,
    configured_job_search: JobSearchPort | None,
    total: _Counts,
    buckets: dict[JobLeadDiscoverySource, _Counts],
    reasons: set[JobLeadDiscoveryReason],
    resolved_job_ids: list[str],
    progress_observer: JobLeadDiscoveryProgressObserver | None,
) -> None:
    """Resolve one current DISCOVERED lead through the shared trust gate."""

    source_counts = _bucket(buckets, source)
    observations: tuple[SourceJobObservation, ...]
    requires_canonical_resolution = _is_aggregator(
        lead.source_url
    ) or lead.origin is JobLeadOrigin.UNKNOWN_WEB
    if requires_canonical_resolution:
        observations = await _resolve_aggregator(
            lead,
            command=command,
            web_search=web_search,
            public_job_reader=public_job_reader,
            configured_job_search=configured_job_search,
            total=total,
            buckets=buckets,
            reasons=reasons,
            observer=progress_observer,
        )
    else:
        attempt = await _read_observation(
            lead.source_url,
            reader=public_job_reader,
            command=command,
            total=total,
            counts=source_counts,
        )
        observation = attempt.observation
        if attempt.budget_exhausted:
            reasons.add(JobLeadDiscoveryReason.PUBLIC_READ_BUDGET_EXHAUSTED)
        if attempt.failed:
            _record_read_failure(total=total, counts=source_counts)
            reasons.add(JobLeadDiscoveryReason.PUBLIC_READ_FAILED)
        observations = (observation,) if observation is not None else ()
    await _notify(
        progress_observer,
        JobLeadDiscoveryPhase.RESOLVING,
        source,
        lead.query_id,
        total,
        buckets,
    )

    if len(observations) != 1:
        if (
            total.public_reads >= command.max_public_reads
            and JobLeadDiscoveryReason.PUBLIC_READ_BUDGET_EXHAUSTED
            in reasons
        ):
            reason = JobLeadDiscoveryReason.PUBLIC_READ_BUDGET_EXHAUSTED
        else:
            reason = (
                JobLeadDiscoveryReason.AMBIGUOUS_OFFICIAL_POSTING
                if len(observations) > 1
                else JobLeadDiscoveryReason.OFFICIAL_POSTING_NOT_FOUND
            )
        if requires_canonical_resolution and (
            web_search is None
            or lead.title_hint is None
            or lead.company_hint is None
        ):
            reason = JobLeadDiscoveryReason.SOURCE_REQUIRES_USER
        reasons.add(reason)
        try:
            transitioned = lead.transition(
                JobLeadStatus.NEEDS_USER,
                now=_utc(command.now),
                reason=reason.value,
            )
            saved = lead_repository.save(transitioned)
        except (OSError, RuntimeError, TypeError, ValueError):
            saved = None
        if (
            not isinstance(saved, JobLeadWriteResult)
            or saved.status
            not in {JobLeadWriteStatus.CREATED, JobLeadWriteStatus.UNCHANGED}
        ):
            _increment(total, source_counts, "failed")
            total.partial = source_counts.partial = True
            reasons.add(JobLeadDiscoveryReason.LEAD_REPOSITORY_FAILURE)
        else:
            _increment(total, source_counts, "needs_user")
        return

    observation = observations[0]
    try:
        subject_result = await _await(
            subject_discovery(
                SubjectJobDiscoveryCommand(
                    subject_id=command.subject_id,
                    request=_discovery_request(command, lead, observation),
                    source_kind=(
                        SubjectJobMembershipSourceKind.JOB_LEAD_RESOLUTION
                    ),
                    source_ref=lead.lead_id,
                    invocation_id=f"{command.invocation_id}:lead:{lead.lead_id}",
                    now=_utc(command.now),
                )
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        subject_result = None
    if (
        not isinstance(subject_result, SubjectJobDiscoveryResult)
        or subject_result.status is not SubjectJobDiscoveryStatus.ACCEPTED
        or subject_result.discovery_response.job_id is None
    ):
        reasons.add(JobLeadDiscoveryReason.DISCOVERY_NOT_ACCEPTED)
        try:
            transitioned = lead.transition(
                JobLeadStatus.NEEDS_USER,
                now=_utc(command.now),
                reason=JobLeadDiscoveryReason.DISCOVERY_NOT_ACCEPTED.value,
            )
            saved = lead_repository.save(transitioned)
        except (OSError, RuntimeError, TypeError, ValueError):
            saved = None
        if not isinstance(saved, JobLeadWriteResult) or saved.status not in {
            JobLeadWriteStatus.CREATED,
            JobLeadWriteStatus.UNCHANGED,
        }:
            _increment(total, source_counts, "failed")
            total.partial = source_counts.partial = True
            reasons.add(JobLeadDiscoveryReason.LEAD_REPOSITORY_FAILURE)
        else:
            _increment(total, source_counts, "needs_user")
        return

    try:
        transitioned = lead.transition(
            JobLeadStatus.RESOLVED,
            now=_utc(command.now),
            canonical_url=observation.source_url,
        )
        saved = lead_repository.save(transitioned)
    except (OSError, RuntimeError, TypeError, ValueError):
        saved = None
    if not isinstance(saved, JobLeadWriteResult) or saved.status not in {
        JobLeadWriteStatus.CREATED,
        JobLeadWriteStatus.UNCHANGED,
    }:
        _increment(total, source_counts, "failed")
        total.partial = source_counts.partial = True
        reasons.add(JobLeadDiscoveryReason.LEAD_REPOSITORY_FAILURE)
        return
    _increment(total, source_counts, "resolved")
    resolved_job_ids.append(subject_result.discovery_response.job_id)


async def discover_job_leads(
    command: JobLeadDiscoveryCommand,
    *,
    policy: PrioritizationPolicy,
    web_search: AuthorizedWebSearchPort,
    lead_repository: JobLeadRepository,
    public_job_reader: PublicJobReaderCallable,
    subject_discovery: SubjectJobDiscoveryCallable,
    configured_job_search: JobSearchPort | None = None,
    progress_observer: JobLeadDiscoveryProgressObserver | None = None,
) -> JobLeadDiscoveryRunSummary:
    """Discover, persist, resolve, and formally import one bounded lead run."""

    if not isinstance(command, JobLeadDiscoveryCommand):
        raise TypeError("command must be a JobLeadDiscoveryCommand")
    total = _Counts()
    buckets: dict[JobLeadDiscoverySource, _Counts] = {}
    reasons: set[JobLeadDiscoveryReason] = set()
    resolved_job_ids: list[str] = []
    if (
        not isinstance(policy, PrioritizationPolicy)
        or policy.status is not PrioritizationPolicyStatus.ACTIVE
        or policy.subject_id != command.subject_id
    ):
        reasons.add(JobLeadDiscoveryReason.INVALID_POLICY)
        total.failed = 1
        return _summary(
            command, total, buckets, resolved_job_ids, reasons, force_failed=True
        )
    plan = build_authorized_web_search_requests(command, policy)
    if not plan.requests:
        reasons.add(JobLeadDiscoveryReason.ROLE_PREFERENCE_REQUIRED)
        total.failed = 1
        return _summary(
            command, total, buckets, resolved_job_ids, reasons, force_failed=True
        )
    if plan.truncated:
        total.truncated = True
        reasons.add(JobLeadDiscoveryReason.REQUEST_BUDGET_EXHAUSTED)

    try:
        listed = lead_repository.list_current(command.subject_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        listed = None
    if (
        not isinstance(listed, JobLeadListResult)
        or listed.status is not JobLeadListStatus.SUCCEEDED
    ):
        reasons.add(JobLeadDiscoveryReason.LEAD_REPOSITORY_FAILURE)
        total.failed = 1
        return _summary(
            command, total, buckets, resolved_job_ids, reasons, force_failed=True
        )
    current_by_url = {lead.source_url: lead for lead in listed.leads}
    fallback_locations = _category_terms(policy, SoftPreferenceCategory.LOCATION)
    fallback_location = fallback_locations[0] if len(fallback_locations) == 1 else None

    stop = False
    examined_hits = 0
    for planned in plan.requests:
        source_counts = _bucket(buckets, planned.source)
        result = await _search(
            port=web_search,
            request=planned.request,
            source=planned.source,
            command=command,
            total=total,
            buckets=buckets,
            observer=progress_observer,
        )
        if result is None:
            reasons.add(
                JobLeadDiscoveryReason.REQUEST_BUDGET_EXHAUSTED
                if total.requests >= command.max_requests
                else JobLeadDiscoveryReason.SEARCH_FAILED
            )
            continue
        if result.status is AuthorizedWebSearchStatus.FAILED:
            reasons.add(JobLeadDiscoveryReason.SEARCH_FAILED)
            continue
        if result.status is AuthorizedWebSearchStatus.PARTIAL:
            reasons.add(JobLeadDiscoveryReason.SEARCH_PARTIAL)
        for hit in result.hits:
            if (
                examined_hits >= command.max_hits
                or total.unique >= command.max_unique_leads
            ):
                total.truncated = source_counts.truncated = True
                reasons.add(JobLeadDiscoveryReason.RESULT_BUDGET_EXHAUSTED)
                stop = True
                break
            examined_hits += 1
            try:
                source_url = canonicalize_job_lead_url("source_url", hit.url)
                origin = classify_job_lead_origin(source_url)
            except (TypeError, ValueError):
                _increment(total, source_counts, "failed")
                total.partial = source_counts.partial = True
                continue
            if source_url in current_by_url:
                _increment(total, source_counts, "duplicates")
                continue
            title, company, location = _hints_from_hit(
                hit, fallback_location=fallback_location
            )
            try:
                lead = JobLead.discover(
                    subject_id=command.subject_id,
                    source=JobLeadSource.AUTHORIZED_WEB_SEARCH,
                    origin=origin,
                    source_url=source_url,
                    discovered_at=_utc(command.now),
                    confidence=(
                        0.75
                        if origin is JobLeadOrigin.ATS
                        else 0.60
                        if origin
                        in {
                            JobLeadOrigin.LINKEDIN_SEARCH_INDEX,
                            JobLeadOrigin.INDEED_SEARCH_INDEX,
                            JobLeadOrigin.GLASSDOOR_SEARCH_INDEX,
                        }
                        else 0.40
                    ),
                    title_hint=title,
                    company_hint=company,
                    location_hint=location,
                    snippet_hint=hit.description or None,
                    query_id=planned.request.query_id,
                )
                written = lead_repository.save(lead)
            except (OSError, RuntimeError, TypeError, ValueError):
                written = None
            if (
                not isinstance(written, JobLeadWriteResult)
                or written.status
                not in {JobLeadWriteStatus.CREATED, JobLeadWriteStatus.UNCHANGED}
                or written.lead is None
            ):
                _increment(total, source_counts, "failed")
                total.partial = source_counts.partial = True
                reasons.add(JobLeadDiscoveryReason.LEAD_REPOSITORY_FAILURE)
                continue
            lead = written.lead
            current_by_url[source_url] = lead
            _increment(total, source_counts, "unique")
            await _notify(
                progress_observer,
                JobLeadDiscoveryPhase.PERSISTING,
                planned.source,
                planned.request.query_id,
                total,
                buckets,
            )

            await _resolve_persisted_lead(
                lead,
                source=planned.source,
                command=command,
                web_search=web_search,
                lead_repository=lead_repository,
                public_job_reader=public_job_reader,
                subject_discovery=subject_discovery,
                configured_job_search=configured_job_search,
                total=total,
                buckets=buckets,
                reasons=reasons,
                resolved_job_ids=resolved_job_ids,
                progress_observer=progress_observer,
            )
        if stop:
            break

    await _notify(
        progress_observer,
        JobLeadDiscoveryPhase.COMPLETED,
        None,
        None,
        total,
        buckets,
    )
    return _summary(command, total, buckets, resolved_job_ids, reasons)


async def resolve_persisted_job_leads(
    command: JobLeadDiscoveryCommand,
    *,
    web_search: AuthorizedWebSearchPort | None = None,
    lead_repository: JobLeadRepository,
    public_job_reader: PublicJobReaderCallable,
    subject_discovery: SubjectJobDiscoveryCallable,
    configured_job_search: JobSearchPort | None = None,
    leads: tuple[JobLead, ...] | None = None,
    progress_observer: JobLeadDiscoveryProgressObserver | None = None,
) -> JobLeadDiscoveryRunSummary:
    """Resolve current persisted leads without requiring a repeat search hit.

    With ``leads=None`` every current ``DISCOVERED`` lead for the subject is
    considered.  An explicit tuple is still fenced against the repository's
    current version; stale, foreign-subject, and already-resolved values never
    re-enter Discovery.
    """

    if not isinstance(command, JobLeadDiscoveryCommand):
        raise TypeError("command must be a JobLeadDiscoveryCommand")
    total = _Counts()
    buckets: dict[JobLeadDiscoverySource, _Counts] = {}
    reasons: set[JobLeadDiscoveryReason] = set()
    resolved_job_ids: list[str] = []
    if leads is not None and (
        not isinstance(leads, tuple)
        or any(not isinstance(lead, JobLead) for lead in leads)
    ):
        total.failed = 1
        reasons.add(JobLeadDiscoveryReason.LEAD_REPOSITORY_FAILURE)
        return _summary(
            command, total, buckets, resolved_job_ids, reasons, force_failed=True
        )
    try:
        listed = lead_repository.list_current(command.subject_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        listed = None
    if (
        not isinstance(listed, JobLeadListResult)
        or listed.status is not JobLeadListStatus.SUCCEEDED
    ):
        total.failed = 1
        reasons.add(JobLeadDiscoveryReason.LEAD_REPOSITORY_FAILURE)
        return _summary(
            command, total, buckets, resolved_job_ids, reasons, force_failed=True
        )
    current_by_id = {lead.lead_id: lead for lead in listed.leads}
    if leads is None:
        targets = tuple(
            lead for lead in listed.leads if lead.status is JobLeadStatus.DISCOVERED
        )
    else:
        selected: list[JobLead] = []
        seen: set[str] = set()
        for supplied in leads:
            current = current_by_id.get(supplied.lead_id)
            source = _source_for_lead(supplied)
            counts = _bucket(buckets, source)
            if supplied.lead_id in seen:
                _increment(total, counts, "duplicates")
                continue
            seen.add(supplied.lead_id)
            if (
                supplied.subject_id != command.subject_id
                or current is None
                or current.content_hash != supplied.content_hash
                or current.status is not JobLeadStatus.DISCOVERED
            ):
                _increment(total, counts, "duplicates")
                continue
            selected.append(current)
        targets = tuple(selected)

    for lead in targets:
        source = _source_for_lead(lead)
        counts = _bucket(buckets, source)
        _increment(total, counts, "unique")
        await _resolve_persisted_lead(
            lead,
            source=source,
            command=command,
            web_search=web_search,
            lead_repository=lead_repository,
            public_job_reader=public_job_reader,
            subject_discovery=subject_discovery,
            configured_job_search=configured_job_search,
            total=total,
            buckets=buckets,
            reasons=reasons,
            resolved_job_ids=resolved_job_ids,
            progress_observer=progress_observer,
        )
    await _notify(
        progress_observer,
        JobLeadDiscoveryPhase.COMPLETED,
        None,
        None,
        total,
        buckets,
    )
    return _summary(command, total, buckets, resolved_job_ids, reasons)


__all__ = [
    "JOB_LEAD_DISCOVERY_CONTRACT_VERSION",
    "JobLeadDiscoveryCommand",
    "JobLeadDiscoveryPhase",
    "JobLeadDiscoveryProgress",
    "JobLeadDiscoveryReason",
    "JobLeadDiscoveryRequestPlan",
    "JobLeadDiscoveryRunSummary",
    "JobLeadDiscoverySource",
    "JobLeadDiscoveryStatus",
    "JobLeadSourceRunResult",
    "JobLeadSourceRunStatus",
    "PlannedWebSearchRequest",
    "build_authorized_web_search_requests",
    "classify_job_lead_origin",
    "discover_job_leads",
    "resolve_persisted_job_leads",
]
