"""Manual, subject-scoped refresh of all enabled job search profiles."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from source_connectors.contract import (
    ReadJobRequest,
    ReadJobResult,
    ReadJobStatus,
    SourceJobObservation,
)

from .accepted_job_intent import (
    AcceptedJobIntent,
    AcceptedJobIntentRepository,
    AcceptedJobIntentSourceProvenance,
    AcceptedJobIntentSourceType,
    AcceptedJobIntentWriteResult,
    AcceptedJobIntentWriteStatus,
)
from .bundles import normalized_job_url
from .job_discovery import (
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobDiscoveryResponse,
    JobIntakeIntent,
    JobIntakeProposal,
    ProposalResolution,
    ResolvedJobCandidate,
)
from .job_search import (
    JobSearchPort,
    JobSearchReason,
    JobSearchResult,
    JobSearchStatus,
    SearchCandidate,
    search_jobs,
)
from .private_home import PrivateHome, PrivateHomeError
from .search_profile import (
    SearchProfile,
    SearchProfileListResult,
    SearchProfileListStatus,
    SearchProfileProvider,
    SearchProfileSourceReference,
)
from .search_profile_intent_policy import (
    SearchProfileIntentDecisionStatus,
    SearchProfileIntentPolicyProvider,
    decide_search_profile_intent,
)
from .selective_reprioritization import (
    SelectiveBatchOverallStatus,
    SelectiveBatchReprioritizationCommand,
    SelectiveBatchReprioritizationResult,
)


JOB_LIBRARY_REFRESH_CONTRACT_VERSION = "manual-job-library-refresh-v2"
_RUN_ID_RE = re.compile(r"job-library-refresh-[0-9a-f]{64}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _clean(name: str, value: Any, maximum: int = 320) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the refresh contract")
    return cleaned


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("persisted refresh time must be a string")
    return _aware(
        "persisted refresh time",
        datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _subject_key(subject_id: str) -> str:
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def _run_id(subject_id: str, invocation_id: str) -> str:
    digest = _hash(
        {
            "contract_version": JOB_LIBRARY_REFRESH_CONTRACT_VERSION,
            "invocation_id": invocation_id,
            "subject_id": subject_id,
        }
    )
    return f"job-library-refresh-{digest}"


class JobLibraryRefreshStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    NOOP = "NOOP"


class ProfileRefreshSearchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class ProfileRefreshFailureReason(StrEnum):
    SEARCH_FAILED = "SEARCH_FAILED"
    SEARCH_UNSUPPORTED = "SEARCH_UNSUPPORTED"
    SEARCH_RESULT_INVALID = "SEARCH_RESULT_INVALID"
    SEARCH_EXCEPTION = "SEARCH_EXCEPTION"


class CandidateDiscoveryStatus(StrEnum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class CandidateRefreshReason(StrEnum):
    INVALID_CANDIDATE_URL = "INVALID_CANDIDATE_URL"
    PUBLIC_READ_FAILED = "PUBLIC_READ_FAILED"
    PUBLIC_READ_RESULT_INVALID = "PUBLIC_READ_RESULT_INVALID"
    PUBLIC_READ_EXCEPTION = "PUBLIC_READ_EXCEPTION"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"
    DISCOVERY_RESULT_INVALID = "DISCOVERY_RESULT_INVALID"
    DISCOVERY_EXCEPTION = "DISCOVERY_EXCEPTION"


class CandidateIntentStatus(StrEnum):
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ADD_JOB_ONLY = "ADD_JOB_ONLY"
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class CandidateIntentReason(StrEnum):
    POLICY_DECISION_FAILED = "POLICY_DECISION_FAILED"
    INTENT_REPOSITORY_UNAVAILABLE = "INTENT_REPOSITORY_UNAVAILABLE"
    INTENT_WRITE_FAILED = "INTENT_WRITE_FAILED"


class JobLibraryRefreshFailureReason(StrEnum):
    PROFILE_SNAPSHOT_FAILED = "PROFILE_SNAPSHOT_FAILED"
    PROFILE_SNAPSHOT_INVALID = "PROFILE_SNAPSHOT_INVALID"
    PRIORITY_REFRESH_FAILED = "PRIORITY_REFRESH_FAILED"
    REPOSITORY_FAILURE = "REPOSITORY_FAILURE"
    REPLAY_INTEGRITY_FAILURE = "REPLAY_INTEGRITY_FAILURE"


class JobLibraryRefreshReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class JobLibraryRefreshWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ManualJobLibraryRefreshCommand:
    subject_id: str
    invocation_id: str
    now: datetime
    max_reprioritizations: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean("subject_id", self.subject_id, 160)
        )
        object.__setattr__(
            self, "invocation_id", _clean("invocation_id", self.invocation_id)
        )
        _aware("now", self.now)
        if (
            type(self.max_reprioritizations) is not int
            or self.max_reprioritizations < 1
        ):
            raise ValueError("max_reprioritizations must be positive")


@dataclass(frozen=True, slots=True)
class SearchProfileRefreshResult:
    profile_id: str
    profile_version: int
    source: SearchProfileSourceReference
    search_status: ProfileRefreshSearchStatus
    candidate_count: int
    reason: ProfileRefreshFailureReason | None
    source_reason: str | None
    result_hash: str

    def __post_init__(self) -> None:
        _clean("profile_id", self.profile_id)
        if type(self.profile_version) is not int or self.profile_version < 1:
            raise ValueError("profile_version must be positive")
        if not isinstance(self.source, SearchProfileSourceReference):
            raise TypeError("profile source must be typed")
        object.__setattr__(
            self, "search_status", ProfileRefreshSearchStatus(self.search_status)
        )
        if type(self.candidate_count) is not int or self.candidate_count < 0:
            raise ValueError("candidate_count must be non-negative")
        if self.reason is not None:
            object.__setattr__(
                self, "reason", ProfileRefreshFailureReason(self.reason)
            )
        if self.source_reason is not None:
            _clean("source_reason", self.source_reason)
        if self.search_status is ProfileRefreshSearchStatus.SUCCEEDED:
            if self.reason is not None or self.source_reason is not None:
                raise ValueError("successful profile search is malformed")
        elif self.reason is None:
            raise ValueError("failed profile search requires a reason")
        if (
            _HASH_RE.fullmatch(self.result_hash) is None
            or self.result_hash != _hash(self.identity_dict())
        ):
            raise ValueError("profile refresh hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "candidate_count": self.candidate_count,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "reason": self.reason.value if self.reason else None,
            "search_status": self.search_status.value,
            "source": self.source.to_dict(),
            "source_reason": self.source_reason,
        }

    @classmethod
    def create(cls, **values: Any) -> "SearchProfileRefreshResult":
        payload = {
            "candidate_count": values["candidate_count"],
            "profile_id": values["profile_id"],
            "profile_version": values["profile_version"],
            "reason": (
                values["reason"].value if values["reason"] else None
            ),
            "search_status": values["search_status"].value,
            "source": values["source"].to_dict(),
            "source_reason": values["source_reason"],
        }
        return cls(**values, result_hash=_hash(payload))


@dataclass(frozen=True, slots=True)
class JobCandidateRefreshResult:
    source_profile_ids: tuple[str, ...]
    candidate_id: str
    candidate_url: str
    reader_status: str
    discovery_status: CandidateDiscoveryStatus
    job_id: str | None
    reason: CandidateRefreshReason | None
    source_reason: str | None
    intent_status: CandidateIntentStatus
    intent_reason: CandidateIntentReason | None
    accepted_job_intent_id: str | None
    result_hash: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source_profile_ids, tuple)
            or not self.source_profile_ids
            or len(set(self.source_profile_ids)) != len(
                self.source_profile_ids
            )
        ):
            raise ValueError("source_profile_ids must be a non-empty unique tuple")
        for profile_id in self.source_profile_ids:
            _clean("profile_id", profile_id)
        _clean("candidate_id", self.candidate_id)
        _clean("candidate_url", self.candidate_url, 2048)
        _clean("reader_status", self.reader_status, 120)
        object.__setattr__(
            self,
            "discovery_status",
            CandidateDiscoveryStatus(self.discovery_status),
        )
        if self.job_id is not None:
            _clean("job_id", self.job_id, 160)
        if self.reason is not None:
            object.__setattr__(
                self, "reason", CandidateRefreshReason(self.reason)
            )
        if self.source_reason is not None:
            _clean("source_reason", self.source_reason)
        object.__setattr__(
            self, "intent_status", CandidateIntentStatus(self.intent_status)
        )
        if self.intent_reason is not None:
            object.__setattr__(
                self, "intent_reason", CandidateIntentReason(self.intent_reason)
            )
        if self.accepted_job_intent_id is not None:
            _clean(
                "accepted_job_intent_id",
                self.accepted_job_intent_id,
                160,
            )
        if self.discovery_status in {
            CandidateDiscoveryStatus.CREATED,
            CandidateDiscoveryStatus.UPDATED,
            CandidateDiscoveryStatus.UNCHANGED,
        }:
            if (
                self.reader_status != ReadJobStatus.SUCCEEDED.value
                or self.job_id is None
                or self.reason is not None
            ):
                raise ValueError("successful candidate refresh is malformed")
        elif self.reason is None or self.job_id is not None:
            raise ValueError("stopped candidate refresh is malformed")
        if self.intent_status in {
            CandidateIntentStatus.CREATED,
            CandidateIntentStatus.UNCHANGED,
        }:
            if (
                self.accepted_job_intent_id is None
                or self.intent_reason is not None
                or self.discovery_status
                not in {
                    CandidateDiscoveryStatus.CREATED,
                    CandidateDiscoveryStatus.UPDATED,
                    CandidateDiscoveryStatus.UNCHANGED,
                }
            ):
                raise ValueError("successful candidate intent is malformed")
        elif self.intent_status is CandidateIntentStatus.FAILED:
            if (
                self.intent_reason is None
                or self.accepted_job_intent_id is not None
            ):
                raise ValueError("failed candidate intent is malformed")
        elif (
            self.intent_reason is not None
            or self.accepted_job_intent_id is not None
        ):
            raise ValueError("non-writing candidate intent is malformed")
        if (
            _HASH_RE.fullmatch(self.result_hash) is None
            or self.result_hash != _hash(self.identity_dict())
        ):
            raise ValueError("candidate refresh hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_url": self.candidate_url,
            "discovery_status": self.discovery_status.value,
            "job_id": self.job_id,
            "accepted_job_intent_id": self.accepted_job_intent_id,
            "intent_reason": (
                self.intent_reason.value if self.intent_reason else None
            ),
            "intent_status": self.intent_status.value,
            "reader_status": self.reader_status,
            "reason": self.reason.value if self.reason else None,
            "source_profile_ids": list(self.source_profile_ids),
            "source_reason": self.source_reason,
        }

    @classmethod
    def create(cls, **values: Any) -> "JobCandidateRefreshResult":
        payload = {
            "candidate_id": values["candidate_id"],
            "candidate_url": values["candidate_url"],
            "discovery_status": values["discovery_status"].value,
            "job_id": values["job_id"],
            "accepted_job_intent_id": values["accepted_job_intent_id"],
            "intent_reason": (
                values["intent_reason"].value
                if values["intent_reason"]
                else None
            ),
            "intent_status": values["intent_status"].value,
            "reader_status": values["reader_status"],
            "reason": values["reason"].value if values["reason"] else None,
            "source_profile_ids": list(values["source_profile_ids"]),
            "source_reason": values["source_reason"],
        }
        return cls(**values, result_hash=_hash(payload))


@dataclass(frozen=True, slots=True)
class DiscoveryRefreshSummary:
    unique_candidates: int
    created: int
    updated: int
    unchanged: int
    skipped: int
    failed: int

    def __post_init__(self) -> None:
        for name in (
            "unique_candidates",
            "created",
            "updated",
            "unchanged",
            "skipped",
            "failed",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.unique_candidates != (
            self.created
            + self.updated
            + self.unchanged
            + self.skipped
            + self.failed
        ):
            raise ValueError("Discovery summary is inconsistent")


@dataclass(frozen=True, slots=True)
class PriorityRefreshSummary:
    status: str
    requested: int
    selected: int
    created: int
    unchanged: int
    failed: int

    def __post_init__(self) -> None:
        _clean("priority status", self.status, 120)
        for name in (
            "requested",
            "selected",
            "created",
            "unchanged",
            "failed",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class JobLibraryRefreshRun:
    run_id: str
    contract_version: str
    subject_id: str
    invocation_id: str
    profile_snapshot_hash: str
    profile_results: tuple[SearchProfileRefreshResult, ...]
    candidate_results: tuple[JobCandidateRefreshResult, ...]
    discovery_summary: DiscoveryRefreshSummary
    priority_summary: PriorityRefreshSummary | None
    overall_status: JobLibraryRefreshStatus
    run_hash: str
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        if _RUN_ID_RE.fullmatch(self.run_id) is None:
            raise ValueError("refresh run_id is invalid")
        if self.contract_version != JOB_LIBRARY_REFRESH_CONTRACT_VERSION:
            raise ValueError("refresh contract version is unsupported")
        subject_id = _clean("subject_id", self.subject_id, 160)
        invocation_id = _clean("invocation_id", self.invocation_id)
        if self.run_id != _run_id(subject_id, invocation_id):
            raise ValueError("refresh run identity is invalid")
        if _HASH_RE.fullmatch(self.profile_snapshot_hash) is None:
            raise ValueError("profile_snapshot_hash is invalid")
        if not isinstance(self.profile_results, tuple) or any(
            not isinstance(item, SearchProfileRefreshResult)
            for item in self.profile_results
        ):
            raise TypeError("profile refresh results must be typed")
        if not isinstance(self.candidate_results, tuple) or any(
            not isinstance(item, JobCandidateRefreshResult)
            for item in self.candidate_results
        ):
            raise TypeError("candidate refresh results must be typed")
        if not isinstance(self.discovery_summary, DiscoveryRefreshSummary):
            raise TypeError("discovery_summary must be typed")
        if self.priority_summary is not None and not isinstance(
            self.priority_summary, PriorityRefreshSummary
        ):
            raise TypeError("priority_summary must be typed")
        object.__setattr__(
            self, "overall_status", JobLibraryRefreshStatus(self.overall_status)
        )
        _aware("started_at", self.started_at)
        _aware("completed_at", self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("refresh completed before it started")
        if (
            _HASH_RE.fullmatch(self.run_hash) is None
            or self.run_hash != _hash(self.content_dict(include_hash=False))
        ):
            raise ValueError("refresh run_hash is invalid")

    def content_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "candidate_results": [
                {**item.identity_dict(), "result_hash": item.result_hash}
                for item in self.candidate_results
            ],
            "completed_at": _time(self.completed_at),
            "contract_version": self.contract_version,
            "discovery_summary": {
                name: getattr(self.discovery_summary, name)
                for name in self.discovery_summary.__dataclass_fields__
            },
            "invocation_id": self.invocation_id,
            "overall_status": self.overall_status.value,
            "priority_summary": (
                {
                    name: getattr(self.priority_summary, name)
                    for name in self.priority_summary.__dataclass_fields__
                }
                if self.priority_summary
                else None
            ),
            "profile_results": [
                {**item.identity_dict(), "result_hash": item.result_hash}
                for item in self.profile_results
            ],
            "profile_snapshot_hash": self.profile_snapshot_hash,
            "run_id": self.run_id,
            "started_at": _time(self.started_at),
            "subject_id": self.subject_id,
        }
        if include_hash:
            value["run_hash"] = self.run_hash
        return value

    @classmethod
    def create(
        cls,
        *,
        command: ManualJobLibraryRefreshCommand,
        profile_snapshot_hash: str,
        profile_results: tuple[SearchProfileRefreshResult, ...],
        candidate_results: tuple[JobCandidateRefreshResult, ...],
        discovery_summary: DiscoveryRefreshSummary,
        priority_summary: PriorityRefreshSummary | None,
        overall_status: JobLibraryRefreshStatus,
    ) -> "JobLibraryRefreshRun":
        values = {
            "run_id": _run_id(command.subject_id, command.invocation_id),
            "contract_version": JOB_LIBRARY_REFRESH_CONTRACT_VERSION,
            "subject_id": command.subject_id,
            "invocation_id": command.invocation_id,
            "profile_snapshot_hash": profile_snapshot_hash,
            "profile_results": profile_results,
            "candidate_results": candidate_results,
            "discovery_summary": discovery_summary,
            "priority_summary": priority_summary,
            "overall_status": overall_status,
            "started_at": command.now,
            "completed_at": command.now,
        }
        content = {
            "candidate_results": [
                {**item.identity_dict(), "result_hash": item.result_hash}
                for item in candidate_results
            ],
            "completed_at": _time(command.now),
            "contract_version": JOB_LIBRARY_REFRESH_CONTRACT_VERSION,
            "discovery_summary": {
                name: getattr(discovery_summary, name)
                for name in discovery_summary.__dataclass_fields__
            },
            "invocation_id": command.invocation_id,
            "overall_status": overall_status.value,
            "priority_summary": (
                {
                    name: getattr(priority_summary, name)
                    for name in priority_summary.__dataclass_fields__
                }
                if priority_summary
                else None
            ),
            "profile_results": [
                {**item.identity_dict(), "result_hash": item.result_hash}
                for item in profile_results
            ],
            "profile_snapshot_hash": profile_snapshot_hash,
            "run_id": values["run_id"],
            "started_at": _time(command.now),
            "subject_id": command.subject_id,
        }
        return cls(**values, run_hash=_hash(content))


@dataclass(frozen=True, slots=True)
class JobLibraryRefreshReadResult:
    status: JobLibraryRefreshReadStatus
    run: JobLibraryRefreshRun | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", JobLibraryRefreshReadStatus(self.status)
        )
        if self.status is JobLibraryRefreshReadStatus.FOUND:
            if not isinstance(self.run, JobLibraryRefreshRun):
                raise ValueError("FOUND refresh read requires a run")
        elif self.run is not None:
            raise ValueError("failed refresh read cannot expose a run")


@dataclass(frozen=True, slots=True)
class JobLibraryRefreshWriteResult:
    status: JobLibraryRefreshWriteStatus
    run: JobLibraryRefreshRun | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", JobLibraryRefreshWriteStatus(self.status)
        )
        if self.status in {
            JobLibraryRefreshWriteStatus.CREATED,
            JobLibraryRefreshWriteStatus.UNCHANGED,
        }:
            if not isinstance(self.run, JobLibraryRefreshRun):
                raise ValueError("successful refresh write requires a run")
        elif self.run is not None:
            raise ValueError("failed refresh write cannot expose a run")


@dataclass(frozen=True, slots=True)
class ManualJobLibraryRefreshResult:
    status: JobLibraryRefreshStatus
    run: JobLibraryRefreshRun | None
    reason: JobLibraryRefreshFailureReason | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", JobLibraryRefreshStatus(self.status)
        )
        if self.reason is not None:
            object.__setattr__(
                self, "reason", JobLibraryRefreshFailureReason(self.reason)
            )
        if self.status is JobLibraryRefreshStatus.UNCHANGED:
            if self.run is None or self.reason is not None:
                raise ValueError("UNCHANGED refresh result is malformed")
        elif self.run is not None:
            if self.reason is not None:
                raise ValueError("persisted refresh result cannot have a reason")
        elif self.reason is None:
            raise ValueError("unpersisted refresh failure requires a reason")


class JobLibraryRefreshRunRepository(Protocol):
    def get_by_invocation(
        self, subject_id: str, invocation_id: str
    ) -> JobLibraryRefreshReadResult: ...

    def save(
        self, run: JobLibraryRefreshRun
    ) -> JobLibraryRefreshWriteResult: ...


class SearchProfileSearchExecutor(Protocol):
    def search(
        self, profile: SearchProfile
    ) -> JobSearchResult | Awaitable[JobSearchResult]: ...


class PublicJobReaderCallable(Protocol):
    def __call__(
        self, request: ReadJobRequest
    ) -> ReadJobResult | Awaitable[ReadJobResult]: ...


class JobDiscoveryCallable(Protocol):
    def __call__(
        self, request: JobDiscoveryRequest
    ) -> JobDiscoveryResponse | Awaitable[JobDiscoveryResponse]: ...


class PriorityRefreshCallable(Protocol):
    def __call__(
        self, command: SelectiveBatchReprioritizationCommand
    ) -> (
        SelectiveBatchReprioritizationResult
        | Awaitable[SelectiveBatchReprioritizationResult]
    ): ...


class ConfiguredSearchProfileExecutor:
    """Resolve a typed source reference to an injected JobSearchPort."""

    def __init__(
        self,
        ports: Mapping[SearchProfileSourceReference, JobSearchPort],
    ) -> None:
        self._ports = dict(ports)

    async def search(self, profile: SearchProfile) -> JobSearchResult:
        if not isinstance(profile, SearchProfile):
            raise TypeError("profile must be typed")
        port = self._ports.get(profile.source)
        if port is None:
            return JobSearchResult.failed(JobSearchReason.INVALID_REQUEST)
        return await search_jobs(profile.search_request, port=port)


def _profile_snapshot(profiles: tuple[SearchProfile, ...]) -> str:
    return _hash(
        {
            "contract_version": JOB_LIBRARY_REFRESH_CONTRACT_VERSION,
            "profiles": [
                {
                    "content_hash": profile.content_hash,
                    "profile_id": profile.profile_id,
                    "profile_version": profile.profile_version,
                    "source": profile.source.to_dict(),
                }
                for profile in profiles
            ],
        }
    )


def _empty_discovery() -> DiscoveryRefreshSummary:
    return DiscoveryRefreshSummary(0, 0, 0, 0, 0, 0)


def _discovery_summary(
    results: tuple[JobCandidateRefreshResult, ...],
) -> DiscoveryRefreshSummary:
    return DiscoveryRefreshSummary(
        unique_candidates=len(results),
        created=sum(
            item.discovery_status is CandidateDiscoveryStatus.CREATED
            for item in results
        ),
        updated=sum(
            item.discovery_status is CandidateDiscoveryStatus.UPDATED
            for item in results
        ),
        unchanged=sum(
            item.discovery_status is CandidateDiscoveryStatus.UNCHANGED
            for item in results
        ),
        skipped=sum(
            item.discovery_status is CandidateDiscoveryStatus.SKIPPED
            for item in results
        ),
        failed=sum(
            item.discovery_status is CandidateDiscoveryStatus.FAILED
            for item in results
        ),
    )


def _priority_summary(
    result: SelectiveBatchReprioritizationResult,
    command: ManualJobLibraryRefreshCommand,
) -> PriorityRefreshSummary:
    if result.subject_id != command.subject_id or result.now != command.now:
        raise ValueError("Priority refresh binding is invalid")
    summary = result.summary
    return PriorityRefreshSummary(
        status=SelectiveBatchOverallStatus(result.overall_status).value,
        requested=summary.requested,
        selected=summary.selected,
        created=summary.created,
        unchanged=summary.unchanged,
        failed=summary.failed,
    )


def _overall(
    *,
    profiles: tuple[SearchProfileRefreshResult, ...],
    candidates: tuple[JobCandidateRefreshResult, ...],
    priority: PriorityRefreshSummary | None,
) -> JobLibraryRefreshStatus:
    search_successes = sum(
        item.search_status is ProfileRefreshSearchStatus.SUCCEEDED
        for item in profiles
    )
    search_failures = len(profiles) - search_successes
    if profiles and search_successes == 0:
        return JobLibraryRefreshStatus.FAILED
    candidate_successes = sum(
        item.discovery_status
        in {
            CandidateDiscoveryStatus.CREATED,
            CandidateDiscoveryStatus.UPDATED,
            CandidateDiscoveryStatus.UNCHANGED,
        }
        for item in candidates
    )
    candidate_failures = sum(
        item.discovery_status
        in {CandidateDiscoveryStatus.SKIPPED, CandidateDiscoveryStatus.FAILED}
        for item in candidates
    )
    intent_failures = sum(
        item.intent_status is CandidateIntentStatus.FAILED
        for item in candidates
    )
    priority_failed = (
        priority is None
        or priority.status in {"FAILED", "PARTIAL_FAILURE"}
        or priority.failed > 0
    )
    if (
        search_failures
        or candidate_failures
        or intent_failures
        or priority_failed
    ):
        if search_successes or candidate_successes:
            return JobLibraryRefreshStatus.PARTIAL_FAILURE
        return JobLibraryRefreshStatus.FAILED
    return JobLibraryRefreshStatus.COMPLETED


def _with_intent(
    candidate: JobCandidateRefreshResult,
    *,
    status: CandidateIntentStatus,
    reason: CandidateIntentReason | None,
    accepted_job_intent_id: str | None,
) -> JobCandidateRefreshResult:
    return JobCandidateRefreshResult.create(
        source_profile_ids=candidate.source_profile_ids,
        candidate_id=candidate.candidate_id,
        candidate_url=candidate.candidate_url,
        reader_status=candidate.reader_status,
        discovery_status=candidate.discovery_status,
        job_id=candidate.job_id,
        reason=candidate.reason,
        source_reason=candidate.source_reason,
        intent_status=status,
        intent_reason=reason,
        accepted_job_intent_id=accepted_job_intent_id,
    )


def _apply_intent_policy(
    *,
    command: ManualJobLibraryRefreshCommand,
    profiles_by_id: Mapping[str, SearchProfile],
    profile_ids: tuple[str, ...],
    discovery_request: JobDiscoveryRequest,
    discovery_response: JobDiscoveryResponse,
    candidate_result: JobCandidateRefreshResult,
    policy_provider: SearchProfileIntentPolicyProvider | None,
    accepted_intent_repository: AcceptedJobIntentRepository | None,
) -> JobCandidateRefreshResult:
    if policy_provider is None:
        return candidate_result
    decisions = []
    for profile_id in profile_ids:
        profile = profiles_by_id.get(profile_id)
        if profile is None:
            return _with_intent(
                candidate_result,
                status=CandidateIntentStatus.FAILED,
                reason=CandidateIntentReason.POLICY_DECISION_FAILED,
                accepted_job_intent_id=None,
            )
        decision = decide_search_profile_intent(
            profile,
            discovery_response,
            policy_provider=policy_provider,
        )
        if decision.status is not SearchProfileIntentDecisionStatus.DECIDED:
            return _with_intent(
                candidate_result,
                status=CandidateIntentStatus.FAILED,
                reason=CandidateIntentReason.POLICY_DECISION_FAILED,
                accepted_job_intent_id=None,
            )
        decisions.append(decision)
    if not any(
        decision.action is JobIntakeIntent.REQUEST_APPLICATION
        for decision in decisions
    ):
        return candidate_result
    if accepted_intent_repository is None:
        return _with_intent(
            candidate_result,
            status=CandidateIntentStatus.FAILED,
            reason=CandidateIntentReason.INTENT_REPOSITORY_UNAVAILABLE,
            accepted_job_intent_id=None,
        )
    assert candidate_result.job_id is not None
    assert discovery_response.run_id is not None
    record = AcceptedJobIntent.create(
        subject_id=command.subject_id,
        job_id=candidate_result.job_id,
        intent=JobIntakeIntent.REQUEST_APPLICATION,
        intake_proposal_id=discovery_request.proposal.proposal_id,
        discovery_run_id=discovery_response.run_id,
        recorded_at=command.now,
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType.SEARCH_PROFILE_REFRESH,
            source_id=command.invocation_id,
            source_profile_ids=profile_ids,
        ),
    )
    try:
        written = accepted_intent_repository.save(record)
    except (OSError, RuntimeError, TypeError, ValueError):
        written = None
    if (
        not isinstance(written, AcceptedJobIntentWriteResult)
        or written.status is AcceptedJobIntentWriteStatus.FAILED
        or written.intent != record
    ):
        return _with_intent(
            candidate_result,
            status=CandidateIntentStatus.FAILED,
            reason=CandidateIntentReason.INTENT_WRITE_FAILED,
            accepted_job_intent_id=None,
        )
    return _with_intent(
        candidate_result,
        status=CandidateIntentStatus(written.status.value),
        reason=None,
        accepted_job_intent_id=record.accepted_job_intent_id,
    )


def _observation_candidate(
    observation: SourceJobObservation,
) -> ResolvedJobCandidate:
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
    *,
    command: ManualJobLibraryRefreshCommand,
    candidate_url: str,
    observation: SourceJobObservation,
) -> JobDiscoveryRequest:
    identity = _hash(
        {
            "candidate_url": candidate_url,
            "invocation_id": command.invocation_id,
            "subject_id": command.subject_id,
        }
    )
    return JobDiscoveryRequest(
        request_id=f"manual-library-refresh-request-{identity}",
        trigger=DiscoveryTrigger.MANUAL_LIBRARY_REFRESH,
        proposal=JobIntakeProposal(
            proposal_id=f"manual-library-refresh-proposal-{identity}",
            intent=JobIntakeIntent.ADD_JOB,
            resolution=ProposalResolution.RESOLVED,
            resolved_candidate=_observation_candidate(observation),
        ),
    )


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _profile_result(
    profile: SearchProfile,
    result: Any,
) -> tuple[SearchProfileRefreshResult, tuple[SearchCandidate, ...]]:
    if not isinstance(result, JobSearchResult):
        return (
            SearchProfileRefreshResult.create(
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                source=profile.source,
                search_status=ProfileRefreshSearchStatus.FAILED,
                candidate_count=0,
                reason=ProfileRefreshFailureReason.SEARCH_RESULT_INVALID,
                source_reason="JOB_SEARCH_RESULT_INVALID",
            ),
            (),
        )
    if result.status is JobSearchStatus.SUCCEEDED:
        if (
            result.candidate_set is None
            or result.candidate_set.request_id
            != profile.search_request.request_id
        ):
            return (
                SearchProfileRefreshResult.create(
                    profile_id=profile.profile_id,
                    profile_version=profile.profile_version,
                    source=profile.source,
                    search_status=ProfileRefreshSearchStatus.FAILED,
                    candidate_count=0,
                    reason=ProfileRefreshFailureReason.SEARCH_RESULT_INVALID,
                    source_reason="CANDIDATE_SET_BINDING_INVALID",
                ),
                (),
            )
        candidates = result.candidate_set.candidates
        return (
            SearchProfileRefreshResult.create(
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                source=profile.source,
                search_status=ProfileRefreshSearchStatus.SUCCEEDED,
                candidate_count=len(candidates),
                reason=None,
                source_reason=None,
            ),
            candidates,
        )
    unsupported = result.status is JobSearchStatus.UNSUPPORTED
    return (
        SearchProfileRefreshResult.create(
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            source=profile.source,
            search_status=(
                ProfileRefreshSearchStatus.UNSUPPORTED
                if unsupported
                else ProfileRefreshSearchStatus.FAILED
            ),
            candidate_count=0,
            reason=(
                ProfileRefreshFailureReason.SEARCH_UNSUPPORTED
                if unsupported
                else ProfileRefreshFailureReason.SEARCH_FAILED
            ),
            source_reason=result.reason_code.value,
        ),
        (),
    )


def _stopped_candidate(
    *,
    profile_ids: tuple[str, ...],
    candidate: SearchCandidate,
    candidate_url: str,
    reader_status: str,
    discovery_status: CandidateDiscoveryStatus,
    reason: CandidateRefreshReason,
    source_reason: str,
) -> JobCandidateRefreshResult:
    return JobCandidateRefreshResult.create(
        source_profile_ids=profile_ids,
        candidate_id=candidate.candidate_id,
        candidate_url=candidate_url,
        reader_status=reader_status,
        discovery_status=discovery_status,
        job_id=None,
        reason=reason,
        source_reason=source_reason,
        intent_status=CandidateIntentStatus.NOT_APPLICABLE,
        intent_reason=None,
        accepted_job_intent_id=None,
    )


def _candidate_from_discovery(
    *,
    profile_ids: tuple[str, ...],
    candidate: SearchCandidate,
    candidate_url: str,
    response: Any,
) -> JobCandidateRefreshResult:
    if not isinstance(response, JobDiscoveryResponse):
        return _stopped_candidate(
            profile_ids=profile_ids,
            candidate=candidate,
            candidate_url=candidate_url,
            reader_status=ReadJobStatus.SUCCEEDED.value,
            discovery_status=CandidateDiscoveryStatus.FAILED,
            reason=CandidateRefreshReason.DISCOVERY_RESULT_INVALID,
            source_reason="DISCOVERY_RESULT_INVALID",
        )
    if (
        response.disposition is DiscoveryDisposition.ACCEPTED
        and response.original_intent is JobIntakeIntent.ADD_JOB
        and response.change is not None
        and response.job_id is not None
        and response.run_id is not None
    ):
        return JobCandidateRefreshResult.create(
            source_profile_ids=profile_ids,
            candidate_id=candidate.candidate_id,
            candidate_url=candidate_url,
            reader_status=ReadJobStatus.SUCCEEDED.value,
            discovery_status=CandidateDiscoveryStatus(response.change.value),
            job_id=response.job_id,
            reason=None,
            source_reason=None,
            intent_status=CandidateIntentStatus.ADD_JOB_ONLY,
            intent_reason=None,
            accepted_job_intent_id=None,
        )
    return _stopped_candidate(
        profile_ids=profile_ids,
        candidate=candidate,
        candidate_url=candidate_url,
        reader_status=ReadJobStatus.SUCCEEDED.value,
        discovery_status=CandidateDiscoveryStatus.FAILED,
        reason=CandidateRefreshReason.DISCOVERY_FAILED,
        source_reason=response.reason_code.value,
    )


def _profile_result_from_dict(value: Any) -> SearchProfileRefreshResult:
    source = value["source"]
    return SearchProfileRefreshResult(
        profile_id=value["profile_id"],
        profile_version=value["profile_version"],
        source=SearchProfileSourceReference(
            kind=source["kind"], source_id=source["source_id"]
        ),
        search_status=ProfileRefreshSearchStatus(value["search_status"]),
        candidate_count=value["candidate_count"],
        reason=(
            ProfileRefreshFailureReason(value["reason"])
            if value["reason"]
            else None
        ),
        source_reason=value["source_reason"],
        intent_status=CandidateIntentStatus(value["intent_status"]),
        intent_reason=(
            CandidateIntentReason(value["intent_reason"])
            if value["intent_reason"]
            else None
        ),
        accepted_job_intent_id=value["accepted_job_intent_id"],
        result_hash=value["result_hash"],
    )


def _candidate_result_from_dict(value: Any) -> JobCandidateRefreshResult:
    return JobCandidateRefreshResult(
        source_profile_ids=tuple(value["source_profile_ids"]),
        candidate_id=value["candidate_id"],
        candidate_url=value["candidate_url"],
        reader_status=value["reader_status"],
        discovery_status=CandidateDiscoveryStatus(value["discovery_status"]),
        job_id=value["job_id"],
        reason=(
            CandidateRefreshReason(value["reason"])
            if value["reason"]
            else None
        ),
        source_reason=value["source_reason"],
        result_hash=value["result_hash"],
    )


def _run_from_dict(value: Any) -> JobLibraryRefreshRun:
    expected = {
        "candidate_results",
        "completed_at",
        "contract_version",
        "discovery_summary",
        "invocation_id",
        "overall_status",
        "priority_summary",
        "profile_results",
        "profile_snapshot_hash",
        "run_hash",
        "run_id",
        "started_at",
        "subject_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted refresh run is malformed")
    priority = value["priority_summary"]
    return JobLibraryRefreshRun(
        run_id=value["run_id"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        invocation_id=value["invocation_id"],
        profile_snapshot_hash=value["profile_snapshot_hash"],
        profile_results=tuple(
            _profile_result_from_dict(item)
            for item in value["profile_results"]
        ),
        candidate_results=tuple(
            _candidate_result_from_dict(item)
            for item in value["candidate_results"]
        ),
        discovery_summary=DiscoveryRefreshSummary(
            **dict(value["discovery_summary"])
        ),
        priority_summary=(
            PriorityRefreshSummary(**dict(priority))
            if priority is not None
            else None
        ),
        overall_status=JobLibraryRefreshStatus(value["overall_status"]),
        run_hash=value["run_hash"],
        started_at=_parse_time(value["started_at"]),
        completed_at=_parse_time(value["completed_at"]),
    )


class PrivateHomeJobLibraryRefreshRunRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        subject = _clean("subject_id", subject_id, 160)
        return (
            self._home.root
            / "state"
            / "discovery"
            / "job-library-refresh-runs"
            / _subject_key(subject)
        )

    def _path(self, subject_id: str, invocation_id: str) -> Path:
        return self._directory(subject_id) / (
            f"{_run_id(subject_id, invocation_id)}.json"
        )

    def get_by_invocation(
        self, subject_id: str, invocation_id: str
    ) -> JobLibraryRefreshReadResult:
        path = self._path(subject_id, invocation_id)
        with self._lock:
            if not path.exists():
                return JobLibraryRefreshReadResult(
                    JobLibraryRefreshReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return JobLibraryRefreshReadResult(
                    JobLibraryRefreshReadStatus.INTEGRITY_FAILURE, None
                )
            try:
                run = _run_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return JobLibraryRefreshReadResult(
                    JobLibraryRefreshReadStatus.INTEGRITY_FAILURE, None
                )
            if (
                run.subject_id != subject_id.strip()
                or run.invocation_id != invocation_id.strip()
            ):
                return JobLibraryRefreshReadResult(
                    JobLibraryRefreshReadStatus.INTEGRITY_FAILURE, None
                )
            return JobLibraryRefreshReadResult(
                JobLibraryRefreshReadStatus.FOUND, run
            )

    def save(
        self, run: JobLibraryRefreshRun
    ) -> JobLibraryRefreshWriteResult:
        if not isinstance(run, JobLibraryRefreshRun):
            raise TypeError("run must be typed")
        path = self._path(run.subject_id, run.invocation_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            run.content_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return JobLibraryRefreshWriteResult(
                    JobLibraryRefreshWriteStatus.FAILED, None
                )
            if created:
                return JobLibraryRefreshWriteResult(
                    JobLibraryRefreshWriteStatus.CREATED, run
                )
            existing = self.get_by_invocation(
                run.subject_id, run.invocation_id
            )
            if (
                existing.status is JobLibraryRefreshReadStatus.FOUND
                and existing.run is not None
                and existing.run.content_dict() == run.content_dict()
            ):
                return JobLibraryRefreshWriteResult(
                    JobLibraryRefreshWriteStatus.UNCHANGED, existing.run
                )
            return JobLibraryRefreshWriteResult(
                JobLibraryRefreshWriteStatus.FAILED, None
            )


def _persist(
    *,
    run: JobLibraryRefreshRun,
    repository: JobLibraryRefreshRunRepository,
) -> ManualJobLibraryRefreshResult:
    try:
        written = repository.save(run)
    except (OSError, RuntimeError, TypeError, ValueError):
        return ManualJobLibraryRefreshResult(
            JobLibraryRefreshStatus.FAILED,
            None,
            JobLibraryRefreshFailureReason.REPOSITORY_FAILURE,
        )
    if (
        not isinstance(written, JobLibraryRefreshWriteResult)
        or written.status is JobLibraryRefreshWriteStatus.FAILED
        or written.run is None
        or written.run.run_hash != run.run_hash
    ):
        return ManualJobLibraryRefreshResult(
            JobLibraryRefreshStatus.FAILED,
            None,
            JobLibraryRefreshFailureReason.REPOSITORY_FAILURE,
        )
    status = (
        JobLibraryRefreshStatus.UNCHANGED
        if written.status is JobLibraryRefreshWriteStatus.UNCHANGED
        else run.overall_status
    )
    return ManualJobLibraryRefreshResult(status, written.run, None)


async def refresh_job_library(
    command: ManualJobLibraryRefreshCommand,
    *,
    profile_provider: SearchProfileProvider,
    search_executor: SearchProfileSearchExecutor,
    public_job_reader: PublicJobReaderCallable,
    discovery: JobDiscoveryCallable,
    priority_refresh: PriorityRefreshCallable,
    repository: JobLibraryRefreshRunRepository,
    intent_policy_provider: SearchProfileIntentPolicyProvider | None = None,
    accepted_intent_repository: AcceptedJobIntentRepository | None = None,
) -> ManualJobLibraryRefreshResult:
    """Run one manual refresh and apply only explicit profile intent policy."""

    if not isinstance(command, ManualJobLibraryRefreshCommand):
        raise TypeError("command must be a ManualJobLibraryRefreshCommand")
    try:
        existing = repository.get_by_invocation(
            command.subject_id, command.invocation_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return ManualJobLibraryRefreshResult(
            JobLibraryRefreshStatus.FAILED,
            None,
            JobLibraryRefreshFailureReason.REPOSITORY_FAILURE,
        )
    if not isinstance(existing, JobLibraryRefreshReadResult):
        return ManualJobLibraryRefreshResult(
            JobLibraryRefreshStatus.FAILED,
            None,
            JobLibraryRefreshFailureReason.REPLAY_INTEGRITY_FAILURE,
        )
    if existing.status is JobLibraryRefreshReadStatus.FOUND:
        if (
            existing.run is None
            or existing.run.subject_id != command.subject_id
            or existing.run.invocation_id != command.invocation_id
        ):
            return ManualJobLibraryRefreshResult(
                JobLibraryRefreshStatus.FAILED,
                None,
                JobLibraryRefreshFailureReason.REPLAY_INTEGRITY_FAILURE,
            )
        return ManualJobLibraryRefreshResult(
            JobLibraryRefreshStatus.UNCHANGED, existing.run, None
        )
    if existing.status is JobLibraryRefreshReadStatus.INTEGRITY_FAILURE:
        return ManualJobLibraryRefreshResult(
            JobLibraryRefreshStatus.FAILED,
            None,
            JobLibraryRefreshFailureReason.REPLAY_INTEGRITY_FAILURE,
        )

    try:
        listed = profile_provider.list_enabled(command.subject_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        listed = None
    if (
        not isinstance(listed, SearchProfileListResult)
        or listed.status is not SearchProfileListStatus.SUCCEEDED
        or any(
            profile.subject_id != command.subject_id or not profile.enabled
            for profile in listed.profiles
        )
        or len({profile.profile_id for profile in listed.profiles})
        != len(listed.profiles)
    ):
        run = JobLibraryRefreshRun.create(
            command=command,
            profile_snapshot_hash=_hash({"profile_snapshot": "FAILED"}),
            profile_results=(),
            candidate_results=(),
            discovery_summary=_empty_discovery(),
            priority_summary=None,
            overall_status=JobLibraryRefreshStatus.FAILED,
        )
        return _persist(run=run, repository=repository)
    profiles = listed.profiles
    profiles_by_id = {profile.profile_id: profile for profile in profiles}
    snapshot_hash = _profile_snapshot(profiles)
    if not profiles:
        run = JobLibraryRefreshRun.create(
            command=command,
            profile_snapshot_hash=snapshot_hash,
            profile_results=(),
            candidate_results=(),
            discovery_summary=_empty_discovery(),
            priority_summary=None,
            overall_status=JobLibraryRefreshStatus.NOOP,
        )
        return _persist(run=run, repository=repository)

    profile_results: list[SearchProfileRefreshResult] = []
    candidates_by_url: dict[
        str, tuple[SearchCandidate, list[str]]
    ] = {}
    for profile in profiles:
        try:
            search_result = await _resolve(search_executor.search(profile))
            profile_result, candidates = _profile_result(
                profile, search_result
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            profile_result = SearchProfileRefreshResult.create(
                profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                source=profile.source,
                search_status=ProfileRefreshSearchStatus.FAILED,
                candidate_count=0,
                reason=ProfileRefreshFailureReason.SEARCH_EXCEPTION,
                source_reason="SEARCH_EXECUTOR_EXCEPTION",
            )
            candidates = ()
        profile_results.append(profile_result)
        for candidate in candidates:
            try:
                canonical_url = normalized_job_url(candidate.source_url)
            except (TypeError, ValueError):
                canonical_url = candidate.source_url.strip()
            existing_candidate = candidates_by_url.get(canonical_url)
            if existing_candidate is None:
                candidates_by_url[canonical_url] = (
                    candidate,
                    [profile.profile_id],
                )
            elif profile.profile_id not in existing_candidate[1]:
                existing_candidate[1].append(profile.profile_id)

    candidate_results: list[JobCandidateRefreshResult] = []
    for candidate_url, (candidate, source_ids) in candidates_by_url.items():
        profile_ids = tuple(source_ids)
        try:
            canonical_url = normalized_job_url(candidate.source_url)
        except (TypeError, ValueError):
            candidate_results.append(
                _stopped_candidate(
                    profile_ids=profile_ids,
                    candidate=candidate,
                    candidate_url=candidate_url,
                    reader_status="SKIPPED",
                    discovery_status=CandidateDiscoveryStatus.SKIPPED,
                    reason=CandidateRefreshReason.INVALID_CANDIDATE_URL,
                    source_reason="INVALID_CANDIDATE_URL",
                )
            )
            continue
        try:
            read_result = await _resolve(
                public_job_reader(ReadJobRequest(canonical_url))
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            candidate_results.append(
                _stopped_candidate(
                    profile_ids=profile_ids,
                    candidate=candidate,
                    candidate_url=canonical_url,
                    reader_status=ReadJobStatus.FAILED.value,
                    discovery_status=CandidateDiscoveryStatus.FAILED,
                    reason=CandidateRefreshReason.PUBLIC_READ_EXCEPTION,
                    source_reason="PUBLIC_READER_EXCEPTION",
                )
            )
            continue
        if not isinstance(read_result, ReadJobResult):
            candidate_results.append(
                _stopped_candidate(
                    profile_ids=profile_ids,
                    candidate=candidate,
                    candidate_url=canonical_url,
                    reader_status="INVALID",
                    discovery_status=CandidateDiscoveryStatus.FAILED,
                    reason=CandidateRefreshReason.PUBLIC_READ_RESULT_INVALID,
                    source_reason="PUBLIC_READER_RESULT_INVALID",
                )
            )
            continue
        if (
            read_result.status is not ReadJobStatus.SUCCEEDED
            or read_result.observation is None
        ):
            candidate_results.append(
                _stopped_candidate(
                    profile_ids=profile_ids,
                    candidate=candidate,
                    candidate_url=canonical_url,
                    reader_status=read_result.status.value,
                    discovery_status=CandidateDiscoveryStatus.FAILED,
                    reason=CandidateRefreshReason.PUBLIC_READ_FAILED,
                    source_reason=read_result.reason_code.value,
                )
            )
            continue
        request = _discovery_request(
            command=command,
            candidate_url=canonical_url,
            observation=read_result.observation,
        )
        try:
            response = await _resolve(discovery(request))
        except (OSError, RuntimeError, TypeError, ValueError):
            candidate_results.append(
                _stopped_candidate(
                    profile_ids=profile_ids,
                    candidate=candidate,
                    candidate_url=canonical_url,
                    reader_status=ReadJobStatus.SUCCEEDED.value,
                    discovery_status=CandidateDiscoveryStatus.FAILED,
                    reason=CandidateRefreshReason.DISCOVERY_EXCEPTION,
                    source_reason="DISCOVERY_EXCEPTION",
                )
            )
            continue
        candidate_result = _candidate_from_discovery(
            profile_ids=profile_ids,
            candidate=candidate,
            candidate_url=canonical_url,
            response=response,
        )
        if (
            isinstance(response, JobDiscoveryResponse)
            and candidate_result.discovery_status
            in {
                CandidateDiscoveryStatus.CREATED,
                CandidateDiscoveryStatus.UPDATED,
                CandidateDiscoveryStatus.UNCHANGED,
            }
        ):
            candidate_result = _apply_intent_policy(
                command=command,
                profiles_by_id=profiles_by_id,
                profile_ids=profile_ids,
                discovery_request=request,
                discovery_response=response,
                candidate_result=candidate_result,
                policy_provider=intent_policy_provider,
                accepted_intent_repository=accepted_intent_repository,
            )
        candidate_results.append(candidate_result)

    try:
        priority_result = await _resolve(
            priority_refresh(
                SelectiveBatchReprioritizationCommand(
                    subject_id=command.subject_id,
                    now=command.now,
                    max_jobs=command.max_reprioritizations,
                )
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        priority_result = None
    try:
        priority_summary = (
            _priority_summary(priority_result, command)
            if isinstance(
                priority_result, SelectiveBatchReprioritizationResult
            )
            else None
        )
    except (AttributeError, TypeError, ValueError):
        priority_summary = None
    typed_profiles = tuple(profile_results)
    typed_candidates = tuple(candidate_results)
    discovery_summary = _discovery_summary(typed_candidates)
    run = JobLibraryRefreshRun.create(
        command=command,
        profile_snapshot_hash=snapshot_hash,
        profile_results=typed_profiles,
        candidate_results=typed_candidates,
        discovery_summary=discovery_summary,
        priority_summary=priority_summary,
        overall_status=_overall(
            profiles=typed_profiles,
            candidates=typed_candidates,
            priority=priority_summary,
        ),
    )
    return _persist(run=run, repository=repository)


__all__ = [
    "JOB_LIBRARY_REFRESH_CONTRACT_VERSION",
    "CandidateDiscoveryStatus",
    "CandidateIntentReason",
    "CandidateIntentStatus",
    "CandidateRefreshReason",
    "ConfiguredSearchProfileExecutor",
    "DiscoveryRefreshSummary",
    "JobCandidateRefreshResult",
    "JobLibraryRefreshFailureReason",
    "JobLibraryRefreshReadResult",
    "JobLibraryRefreshReadStatus",
    "JobLibraryRefreshRun",
    "JobLibraryRefreshRunRepository",
    "JobLibraryRefreshStatus",
    "JobLibraryRefreshWriteResult",
    "JobLibraryRefreshWriteStatus",
    "ManualJobLibraryRefreshCommand",
    "ManualJobLibraryRefreshResult",
    "PriorityRefreshSummary",
    "ProfileRefreshFailureReason",
    "ProfileRefreshSearchStatus",
    "PrivateHomeJobLibraryRefreshRunRepository",
    "SearchProfileRefreshResult",
    "SearchProfileSearchExecutor",
    "refresh_job_library",
]
