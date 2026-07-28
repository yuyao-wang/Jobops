"""Provider-neutral contract for one public job-source observation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit


class SourcePlatform(str, Enum):
    GREENHOUSE = "GREENHOUSE"
    LEVER = "LEVER"
    GENERIC_WEB = "GENERIC_WEB"


class AtsType(str, Enum):
    GREENHOUSE = "GREENHOUSE"
    LEVER = "LEVER"
    UNKNOWN = "UNKNOWN"


class WorkMode(str, Enum):
    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"
    UNKNOWN = "UNKNOWN"


class ProvenanceSource(str, Enum):
    REQUEST = "REQUEST"
    SOURCE_API = "SOURCE_API"
    SYSTEM = "SYSTEM"


class ReadJobStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class ReadJobReason(str, Enum):
    INVALID_URL = "INVALID_URL"
    UNSAFE_URL = "UNSAFE_URL"
    UNSUPPORTED_URL = "UNSUPPORTED_URL"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_CLOSED = "JOB_CLOSED"
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    SOURCE_RATE_LIMITED = "SOURCE_RATE_LIMITED"
    SOURCE_RESPONSE_INVALID = "SOURCE_RESPONSE_INVALID"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


_ALWAYS_RETRYABLE_REASONS = frozenset(
    {
        ReadJobReason.SOURCE_TIMEOUT,
        ReadJobReason.SOURCE_RATE_LIMITED,
    }
)
_NEVER_RETRYABLE_REASONS = frozenset(
    {
        ReadJobReason.INVALID_URL,
        ReadJobReason.UNSAFE_URL,
        ReadJobReason.UNSUPPORTED_URL,
        ReadJobReason.JOB_NOT_FOUND,
        ReadJobReason.JOB_CLOSED,
        ReadJobReason.SOURCE_RESPONSE_INVALID,
    }
)


def _validate_absolute_http_url(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError(f"{name} must be a non-empty absolute HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid absolute HTTP(S) URL") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        raise ValueError(f"{name} must be a valid absolute HTTP(S) URL")


def _validate_timestamp(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class ReadJobRequest:
    url: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str):
            raise TypeError("url must be a string")


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    field: str
    source: ProvenanceSource
    source_field: str

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not self.field:
            raise ValueError("provenance field must be non-empty")
        object.__setattr__(self, "source", ProvenanceSource(self.source))
        if not isinstance(self.source_field, str) or not self.source_field:
            raise ValueError("provenance source_field must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceJobObservation:
    source_platform: SourcePlatform
    source_job_id: str | None
    source_url: str
    application_url: str | None
    company: str
    title: str
    description: str
    location: str
    work_mode: WorkMode
    posted_at: str | None
    ats_type: AtsType
    observed_at: str
    provenance: tuple[FieldProvenance, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_platform", SourcePlatform(self.source_platform)
        )
        object.__setattr__(self, "work_mode", WorkMode(self.work_mode))
        object.__setattr__(self, "ats_type", AtsType(self.ats_type))
        if self.source_job_id is not None and (
            not isinstance(self.source_job_id, str)
            or not self.source_job_id
            or len(self.source_job_id) > 240
        ):
            raise ValueError("source_job_id is outside the observation contract")
        _validate_absolute_http_url("source_url", self.source_url)
        if self.application_url is not None:
            _validate_absolute_http_url("application_url", self.application_url)
        for name, value, maximum in (
            ("company", self.company, 240),
            ("title", self.title, 240),
            ("description", self.description, 100_000),
            ("location", self.location, 320),
        ):
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"{name} is outside the observation contract")
        if not self.company or not self.title or not self.description:
            raise ValueError("company, title, and description must be non-empty")
        if self.posted_at is not None:
            _validate_timestamp("posted_at", self.posted_at)
        _validate_timestamp("observed_at", self.observed_at)
        if not isinstance(self.provenance, tuple) or not self.provenance:
            raise ValueError("provenance must be a non-empty tuple")
        if not all(isinstance(item, FieldProvenance) for item in self.provenance):
            raise TypeError("provenance contains an invalid item")
        fields = [item.field for item in self.provenance]
        if len(fields) != len(set(fields)):
            raise ValueError("provenance fields must be unique")


@dataclass(frozen=True, slots=True)
class ReadJobResult:
    status: ReadJobStatus
    reason_code: ReadJobReason | None
    retryable: bool
    observation: SourceJobObservation | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ReadJobStatus(self.status))
        if self.reason_code is not None:
            object.__setattr__(
                self, "reason_code", ReadJobReason(self.reason_code)
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if self.status is ReadJobStatus.SUCCEEDED:
            if (
                self.reason_code is not None
                or self.observation is None
                or self.retryable
            ):
                raise ValueError("successful read result has conflicting fields")
            return
        if self.observation is not None or self.reason_code is None:
            raise ValueError("unsuccessful read result cannot contain an observation")
        if self.status is ReadJobStatus.UNSUPPORTED:
            if (
                self.reason_code is not ReadJobReason.UNSUPPORTED_URL
                or self.retryable
            ):
                raise ValueError("unsupported result has conflicting fields")
            return
        if self.reason_code is ReadJobReason.UNSUPPORTED_URL:
            raise ValueError("UNSUPPORTED_URL requires UNSUPPORTED status")
        if (
            self.reason_code in _ALWAYS_RETRYABLE_REASONS
            and not self.retryable
        ):
            raise ValueError("retryable conflicts with the reason policy")
        if self.reason_code in _NEVER_RETRYABLE_REASONS and self.retryable:
            raise ValueError("retryable conflicts with the reason policy")

    @classmethod
    def succeeded(cls, observation: SourceJobObservation) -> "ReadJobResult":
        return cls(
            status=ReadJobStatus.SUCCEEDED,
            reason_code=None,
            retryable=False,
            observation=observation,
        )

    @classmethod
    def failed(
        cls,
        reason_code: ReadJobReason,
        *,
        retryable: bool | None = None,
    ) -> "ReadJobResult":
        reason = ReadJobReason(reason_code)
        status = (
            ReadJobStatus.UNSUPPORTED
            if reason is ReadJobReason.UNSUPPORTED_URL
            else ReadJobStatus.FAILED
        )
        if retryable is None:
            retryable = (
                reason in _ALWAYS_RETRYABLE_REASONS
                or reason is ReadJobReason.SOURCE_UNAVAILABLE
            )
        return cls(
            status=status,
            reason_code=reason,
            retryable=retryable,
            observation=None,
        )


@runtime_checkable
class SourceJobReader(Protocol):
    async def read_job(self, request: ReadJobRequest) -> ReadJobResult:
        """Read one allowlisted public job URL without creating durable state."""


__all__ = [
    "AtsType",
    "FieldProvenance",
    "ProvenanceSource",
    "ReadJobReason",
    "ReadJobRequest",
    "ReadJobResult",
    "ReadJobStatus",
    "SourceJobObservation",
    "SourceJobReader",
    "SourcePlatform",
    "WorkMode",
]
