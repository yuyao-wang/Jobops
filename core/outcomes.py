"""Versioned outcomes shared by every Jobops adapter and command.

JSON is the source of truth for detailed results.  Process exit codes deliberately
remain coarse so shell callers do not become coupled to every ATS-specific state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum, StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


OUTCOME_SCHEMA_VERSION = 1


def utc_now() -> str:
    """Return a stable RFC 3339 timestamp in UTC."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OutcomeStatus(StrEnum):
    """Machine-readable application states; values are part of the public API."""

    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    MATERIALS_REQUIRED = "MATERIALS_REQUIRED"
    REVIEW_READY = "REVIEW_READY"
    AWAITING_GATE_A = "AWAITING_GATE_A"
    AWAITING_GATE_B = "AWAITING_GATE_B"
    NEEDS_USER = "NEEDS_USER"
    NEEDS_USER_LOGIN = "NEEDS_USER_LOGIN"
    NEEDS_USER_2FA = "NEEDS_USER_2FA"
    NEEDS_USER_CAPTCHA = "NEEDS_USER_CAPTCHA"
    NEEDS_USER_EMAIL_VERIFICATION = "NEEDS_USER_EMAIL_VERIFICATION"
    NEEDS_USER_ACCOUNT_LOCKED = "NEEDS_USER_ACCOUNT_LOCKED"
    NEEDS_USER_SENSITIVE_ANSWER = "NEEDS_USER_SENSITIVE_ANSWER"
    SUBMITTING = "SUBMITTING"
    SUBMITTED_VERIFIED = "SUBMITTED_VERIFIED"
    SUBMIT_UNKNOWN = "SUBMIT_UNKNOWN"
    SKIPPED_POLICY = "SKIPPED_POLICY"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_UNSUPPORTED = "FAILED_UNSUPPORTED"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class OutcomePhase(StrEnum):
    QUEUE = "QUEUE"
    MATERIALS = "MATERIALS"
    AUTHENTICATE = "AUTHENTICATE"
    INSPECT = "INSPECT"
    FILL = "FILL"
    VALIDATE = "VALIDATE"
    REVIEW = "REVIEW"
    SUBMIT = "SUBMIT"
    VERIFY = "VERIFY"
    COMPLETE = "COMPLETE"


class ReasonCode(StrEnum):
    NONE = "NONE"
    REVIEW_COMPLETE = "REVIEW_COMPLETE"
    SUBMISSION_CONFIRMED = "SUBMISSION_CONFIRMED"
    GATE_A_REQUIRED = "GATE_A_REQUIRED"
    GATE_B_REQUIRED = "GATE_B_REQUIRED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    TWO_FACTOR_AUTH = "TWO_FACTOR_AUTH"
    CAPTCHA = "CAPTCHA"
    EMAIL_VERIFICATION = "EMAIL_VERIFICATION"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    UNKNOWN_REQUIRED_QUESTION = "UNKNOWN_REQUIRED_QUESTION"
    SENSITIVE_ANSWER_REQUIRED = "SENSITIVE_ANSWER_REQUIRED"
    MISSING_MATERIAL = "MISSING_MATERIAL"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    UNSUPPORTED_ATS = "UNSUPPORTED_ATS"
    POLICY_DENIED = "POLICY_DENIED"
    RETRYABLE_BROWSER_ERROR = "RETRYABLE_BROWSER_ERROR"
    SUBMISSION_CONFIRMATION_MISSING = "SUBMISSION_CONFIRMATION_MISSING"
    DUPLICATE_SUBMISSION = "DUPLICATE_SUBMISSION"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class EvidenceKind(StrEnum):
    CONFIRMATION_TEXT = "CONFIRMATION_TEXT"
    CONFIRMATION_URL = "CONFIRMATION_URL"
    SCREENSHOT = "SCREENSHOT"
    ATS_APPLICATION_ID = "ATS_APPLICATION_ID"
    EMAIL = "EMAIL"
    NETWORK_RESPONSE = "NETWORK_RESPONSE"
    FORM_SNAPSHOT = "FORM_SNAPSHOT"


SUBMISSION_EVIDENCE_KINDS = frozenset(
    {
        EvidenceKind.CONFIRMATION_TEXT,
        EvidenceKind.CONFIRMATION_URL,
        EvidenceKind.SCREENSHOT,
        EvidenceKind.ATS_APPLICATION_ID,
        EvidenceKind.EMAIL,
        EvidenceKind.NETWORK_RESPONSE,
    }
)


class ExitCode(IntEnum):
    """Stable, intentionally broad process exit categories."""

    SUCCESS = 0
    INVALID_INPUT = 2
    NEEDS_USER = 10
    AWAITING_GATE_A = 11
    AWAITING_GATE_B = 12
    RETRYABLE_FAILURE = 20
    TERMINAL_FAILURE = 30
    POLICY_BLOCKED = 40
    INTERNAL_ERROR = 50


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A reference to evidence, never the potentially sensitive artifact itself."""

    kind: EvidenceKind
    uri: str | None = None
    sha256: str | None = None
    observed_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", EvidenceKind(self.kind))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        if not self.uri and not self.sha256 and not self.metadata:
            raise ValueError("evidence must include a uri, sha256, or metadata")
        if self.kind is EvidenceKind.CONFIRMATION_URL and self.uri:
            parsed = urlsplit(self.uri)
            if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
                raise ValueError(
                    "confirmation URL evidence must be an absolute HTTP(S) URL"
                )
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("confirmation URL evidence must not contain userinfo")
            if "?" in self.uri or "#" in self.uri:
                raise ValueError(
                    "confirmation URL evidence must not contain a query or fragment"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "uri": self.uri,
            "sha256": self.sha256,
            "observed_at": self.observed_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceRef":
        return cls(
            kind=EvidenceKind(value["kind"]),
            uri=value.get("uri"),
            sha256=value.get("sha256"),
            observed_at=value.get("observed_at") or utc_now(),
            metadata=value.get("metadata") or {},
        )


_NEEDS_USER_STATUSES = frozenset(
    {
        OutcomeStatus.NEEDS_USER,
        OutcomeStatus.NEEDS_USER_LOGIN,
        OutcomeStatus.NEEDS_USER_2FA,
        OutcomeStatus.NEEDS_USER_CAPTCHA,
        OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
        OutcomeStatus.NEEDS_USER_ACCOUNT_LOCKED,
        OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
    }
)

# A submission whose result cannot be confirmed is intentionally not a generic
# resumable "Needs user" state.  It still exits through the broad NEEDS_USER
# process category, but queue orchestration must keep it out of automatic retry.
_HARD_STOP_STATUSES = frozenset({OutcomeStatus.SUBMIT_UNKNOWN})


@dataclass(frozen=True, slots=True)
class ApplicationOutcome:
    """Structured result returned by adapters and orchestration commands."""

    run_id: str
    job_id: str
    status: OutcomeStatus
    phase: OutcomePhase
    reason_code: ReasonCode | str = ReasonCode.NONE
    message: str = ""
    adapter: str | None = None
    retryable: bool = False
    checkpoint: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = OUTCOME_SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.run_id or not self.job_id:
            raise ValueError("run_id and job_id are required")
        object.__setattr__(self, "status", OutcomeStatus(self.status))
        object.__setattr__(self, "phase", OutcomePhase(self.phase))
        try:
            reason: ReasonCode | str = ReasonCode(self.reason_code)
        except ValueError:
            reason = str(self.reason_code)
        object.__setattr__(self, "reason_code", reason)
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(
                item if isinstance(item, EvidenceRef) else EvidenceRef.from_dict(item)
                for item in self.evidence_refs
            ),
        )
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))
        if self.schema_version != OUTCOME_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported outcome schema version {self.schema_version}; "
                f"expected {OUTCOME_SCHEMA_VERSION}"
            )
        if self.status is OutcomeStatus.SUBMITTED_VERIFIED and not any(
            evidence.kind in SUBMISSION_EVIDENCE_KINDS
            for evidence in self.evidence_refs
        ):
            raise ValueError("SUBMITTED_VERIFIED requires explicit submission evidence")
        if self.status is OutcomeStatus.FAILED_RETRYABLE and not self.retryable:
            object.__setattr__(self, "retryable", True)

    @property
    def exit_code(self) -> ExitCode:
        if self.status is OutcomeStatus.AWAITING_GATE_A:
            return ExitCode.AWAITING_GATE_A
        if self.status is OutcomeStatus.AWAITING_GATE_B:
            return ExitCode.AWAITING_GATE_B
        if self.status in _HARD_STOP_STATUSES:
            return ExitCode.NEEDS_USER
        if self.status in _NEEDS_USER_STATUSES:
            return ExitCode.NEEDS_USER
        if self.status is OutcomeStatus.FAILED_RETRYABLE:
            return ExitCode.RETRYABLE_FAILURE
        if self.status is OutcomeStatus.MATERIALS_REQUIRED:
            return ExitCode.POLICY_BLOCKED
        if self.status in {
            OutcomeStatus.FAILED_UNSUPPORTED,
            OutcomeStatus.FAILED_TERMINAL,
        }:
            return ExitCode.TERMINAL_FAILURE
        if self.status is OutcomeStatus.SKIPPED_POLICY:
            return ExitCode.POLICY_BLOCKED
        if self.status is OutcomeStatus.INTERNAL_ERROR:
            return ExitCode.INTERNAL_ERROR
        return ExitCode.SUCCESS

    @property
    def evidence(self) -> tuple[EvidenceRef, ...]:
        """Compatibility alias for callers that use the shorter name."""

        return self.evidence_refs

    def to_dict(self) -> dict[str, Any]:
        reason = (
            self.reason_code.value
            if isinstance(self.reason_code, ReasonCode)
            else self.reason_code
        )
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "status": self.status.value,
            "phase": self.phase.value,
            "reason_code": reason,
            "message": self.message,
            "adapter": self.adapter,
            "retryable": self.retryable,
            "checkpoint": self.checkpoint,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "details": dict(self.details),
            "created_at": self.created_at,
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(),
            indent=indent,
            sort_keys=True,
            separators=None if indent is not None else (",", ":"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ApplicationOutcome":
        return cls(
            schema_version=int(value.get("schema_version", OUTCOME_SCHEMA_VERSION)),
            run_id=str(value["run_id"]),
            job_id=str(value["job_id"]),
            status=OutcomeStatus(value["status"]),
            phase=OutcomePhase(value["phase"]),
            reason_code=value.get("reason_code", ReasonCode.NONE),
            message=str(value.get("message") or ""),
            adapter=value.get("adapter"),
            retryable=bool(value.get("retryable", False)),
            checkpoint=value.get("checkpoint"),
            evidence_refs=tuple(
                EvidenceRef.from_dict(item)
                for item in value.get("evidence_refs", ())
            ),
            details=value.get("details") or {},
            created_at=value.get("created_at") or utc_now(),
        )

    @classmethod
    def from_json(cls, payload: str | bytes | bytearray) -> "ApplicationOutcome":
        return cls.from_dict(json.loads(payload))

    @classmethod
    def review_ready(
        cls,
        *,
        run_id: str,
        job_id: str,
        adapter: str,
        message: str = "Application is filled and validated for review",
        checkpoint: str | None = None,
        evidence_refs: Iterable[EvidenceRef] = (),
        details: Mapping[str, Any] | None = None,
    ) -> "ApplicationOutcome":
        return cls(
            run_id=run_id,
            job_id=job_id,
            status=OutcomeStatus.REVIEW_READY,
            phase=OutcomePhase.REVIEW,
            reason_code=ReasonCode.REVIEW_COMPLETE,
            message=message,
            adapter=adapter,
            checkpoint=checkpoint,
            evidence_refs=tuple(evidence_refs),
            details=details or {},
        )

    @classmethod
    def needs_user(
        cls,
        *,
        run_id: str,
        job_id: str,
        status: OutcomeStatus = OutcomeStatus.NEEDS_USER,
        phase: OutcomePhase,
        reason_code: ReasonCode | str,
        message: str,
        adapter: str | None = None,
        checkpoint: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> "ApplicationOutcome":
        if status not in _NEEDS_USER_STATUSES | _HARD_STOP_STATUSES:
            raise ValueError(f"{status} is not a needs-user status")
        return cls(
            run_id=run_id,
            job_id=job_id,
            status=status,
            phase=phase,
            reason_code=reason_code,
            message=message,
            adapter=adapter,
            checkpoint=checkpoint,
            details=details or {},
        )

    @classmethod
    def submitted_verified(
        cls,
        *,
        run_id: str,
        job_id: str,
        adapter: str,
        evidence_refs: Iterable[EvidenceRef],
        message: str = "Submission was explicitly confirmed",
        details: Mapping[str, Any] | None = None,
    ) -> "ApplicationOutcome":
        return cls(
            run_id=run_id,
            job_id=job_id,
            status=OutcomeStatus.SUBMITTED_VERIFIED,
            phase=OutcomePhase.COMPLETE,
            reason_code=ReasonCode.SUBMISSION_CONFIRMED,
            message=message,
            adapter=adapter,
            evidence_refs=tuple(evidence_refs),
            details=details or {},
        )
