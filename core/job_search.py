"""Provider-neutral contract and entry point for bounded job candidate search."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from source_connectors.contract import SourcePlatform


class JobSearchStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class JobSearchReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    UNSUPPORTED_COMPANY = "UNSUPPORTED_COMPANY"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    SOURCE_RATE_LIMITED = "SOURCE_RATE_LIMITED"
    SOURCE_RESPONSE_INVALID = "SOURCE_RESPONSE_INVALID"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


_ALWAYS_RETRYABLE_REASONS = frozenset(
    {
        JobSearchReason.SOURCE_TIMEOUT,
        JobSearchReason.SOURCE_RATE_LIMITED,
    }
)
_NEVER_RETRYABLE_REASONS = frozenset(
    {
        JobSearchReason.INVALID_REQUEST,
        JobSearchReason.UNSUPPORTED_COMPANY,
        JobSearchReason.SOURCE_RESPONSE_INVALID,
    }
)


def _validate_timestamp(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _valid_request(request: "JobSearchRequest") -> bool:
    if not isinstance(request, JobSearchRequest):
        raise TypeError("request must be a JobSearchRequest")
    if (
        not request.request_id.strip()
        or len(request.request_id.strip()) > 240
        or not request.company.strip()
        or len(request.company.strip()) > 240
        or not request.title.strip()
        or len(request.title.strip()) > 240
    ):
        return False
    if request.location is not None and (
        not request.location.strip() or len(request.location.strip()) > 320
    ):
        return False
    return True


@dataclass(frozen=True, slots=True)
class JobSearchRequest:
    request_id: str
    company: str
    title: str
    location: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("company", self.company),
            ("title", self.title),
        ):
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a string")
        if self.location is not None and not isinstance(self.location, str):
            raise TypeError("location must be a string or None")


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    candidate_id: str
    company: str
    title: str
    location: str | None
    source_platform: SourcePlatform
    source_url: str
    source_job_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_platform",
            SourcePlatform(self.source_platform),
        )
        for name, value, maximum in (
            ("candidate_id", self.candidate_id, 320),
            ("company", self.company, 240),
            ("title", self.title, 240),
            ("source_url", self.source_url, 2048),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > maximum
            ):
                raise ValueError(f"{name} is outside the search contract")
        if self.location is not None and (
            not isinstance(self.location, str)
            or not self.location.strip()
            or len(self.location) > 320
        ):
            raise ValueError("location is outside the search contract")
        if self.source_job_id is not None and (
            not isinstance(self.source_job_id, str)
            or not self.source_job_id.strip()
            or len(self.source_job_id) > 240
        ):
            raise ValueError("source_job_id is outside the search contract")


@dataclass(frozen=True, slots=True)
class CandidateSet:
    candidate_set_id: str
    request_id: str
    candidates: tuple[SearchCandidate, ...]
    created_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate_set_id", self.candidate_set_id),
            ("request_id", self.request_id),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 320
            ):
                raise ValueError(f"{name} is outside the search contract")
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(candidate, SearchCandidate)
            for candidate in self.candidates
        ):
            raise TypeError("candidates must be a tuple of SearchCandidate")
        if len(self.candidates) > 10:
            raise ValueError("CandidateSet cannot contain more than 10 candidates")
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        _validate_timestamp("created_at", self.created_at)
        if self.expires_at is not None:
            _validate_timestamp("expires_at", self.expires_at)
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must be later than created_at")


@dataclass(frozen=True, slots=True)
class JobSearchResult:
    status: JobSearchStatus
    reason_code: JobSearchReason | None
    retryable: bool
    candidate_set: CandidateSet | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", JobSearchStatus(self.status))
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                JobSearchReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")

        if self.status is JobSearchStatus.SUCCEEDED:
            if (
                self.reason_code is not None
                or self.candidate_set is None
                or self.retryable
            ):
                raise ValueError("successful search result has conflicting fields")
            return

        if self.candidate_set is not None or self.reason_code is None:
            raise ValueError("unsuccessful search cannot contain a CandidateSet")
        if self.status is JobSearchStatus.UNSUPPORTED:
            if (
                self.reason_code is not JobSearchReason.UNSUPPORTED_COMPANY
                or self.retryable
            ):
                raise ValueError("unsupported search result has conflicting fields")
            return
        if self.reason_code is JobSearchReason.UNSUPPORTED_COMPANY:
            raise ValueError("UNSUPPORTED_COMPANY requires UNSUPPORTED status")
        if (
            self.reason_code in _ALWAYS_RETRYABLE_REASONS
            and not self.retryable
        ):
            raise ValueError("retryable conflicts with the search reason policy")
        if self.reason_code in _NEVER_RETRYABLE_REASONS and self.retryable:
            raise ValueError("retryable conflicts with the search reason policy")

    @classmethod
    def succeeded(cls, candidate_set: CandidateSet) -> "JobSearchResult":
        return cls(
            status=JobSearchStatus.SUCCEEDED,
            reason_code=None,
            retryable=False,
            candidate_set=candidate_set,
        )

    @classmethod
    def failed(
        cls,
        reason_code: JobSearchReason,
        *,
        retryable: bool | None = None,
    ) -> "JobSearchResult":
        reason = JobSearchReason(reason_code)
        if reason is JobSearchReason.UNSUPPORTED_COMPANY:
            return cls.unsupported()
        if retryable is None:
            retryable = (
                reason in _ALWAYS_RETRYABLE_REASONS
                or reason is JobSearchReason.SOURCE_UNAVAILABLE
            )
        return cls(
            status=JobSearchStatus.FAILED,
            reason_code=reason,
            retryable=retryable,
            candidate_set=None,
        )

    @classmethod
    def unsupported(cls) -> "JobSearchResult":
        return cls(
            status=JobSearchStatus.UNSUPPORTED,
            reason_code=JobSearchReason.UNSUPPORTED_COMPANY,
            retryable=False,
            candidate_set=None,
        )


@runtime_checkable
class JobSearchPort(Protocol):
    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        """Search one bounded source without creating durable state."""


async def search_jobs(
    request: JobSearchRequest,
    *,
    port: JobSearchPort,
) -> JobSearchResult:
    """Run one validated provider-neutral candidate search."""
    if not _valid_request(request):
        return JobSearchResult.failed(JobSearchReason.INVALID_REQUEST)
    if not isinstance(port, JobSearchPort):
        raise TypeError("port must implement JobSearchPort")
    result = await port.search(request)
    if not isinstance(result, JobSearchResult):
        raise TypeError("JobSearchPort must return JobSearchResult")
    return result


__all__ = [
    "CandidateSet",
    "JobSearchPort",
    "JobSearchReason",
    "JobSearchRequest",
    "JobSearchResult",
    "JobSearchStatus",
    "SearchCandidate",
    "search_jobs",
]
