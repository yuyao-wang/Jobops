"""Execute one plan-scoped submission permit through the existing Engine."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, AsyncContextManager, Mapping, Protocol, runtime_checkable

from .non_submit_application_execution import (
    NonSubmitApplicationExecutionReadStatus,
    NonSubmitApplicationExecutionRepository,
)
from .outcomes import (
    SUBMISSION_EVIDENCE_KINDS,
    ApplicationOutcome,
    EvidenceRef,
    OutcomeStatus,
    ReasonCode,
)
from .permits import (
    OpaquePermitTokenStore,
    PermitError,
    PermitExpiredError,
    PermitGate,
    PermitPrerequisiteError,
    PermitService,
    SubmissionPermitAction,
    SubmissionPermitConsumptionReference,
)
from .private_home import PrivateHome, PrivateHomeError
from .recoverable_application_bundle import (
    RecoverableApplicationBundleEnvelopeReadStatus,
    RecoverableApplicationBundleEnvelopeRepository,
)
from .submission_authorization import (
    SubmissionAuthorizationReadStatus,
    SubmissionAuthorizationRepository,
    SubmissionAuthorizationVerdict,
)
from .submission_permit import (
    SubmissionPermitReadStatus,
    SubmissionPermitRepository,
)


AUTHORIZED_SUBMISSION_EXECUTION_CONTRACT_VERSION = (
    "authorized-submission-execution-v1"
)
AUTHORIZED_SUBMISSION_POLICY_VERSION = "submit-once-with-evidence-v1"
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_PERMIT_ID_RE = re.compile(r"^submission-permit-[a-f0-9]{64}$")
_RECORD_ID_RE = re.compile(
    r"^authorized-submission-execution-[a-f0-9]{64}$"
)


class AuthorizedSubmissionExecutionStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    DEFERRED_BROWSER_UNAVAILABLE = "DEFERRED_BROWSER_UNAVAILABLE"
    DEFERRED_REVIEW_CHANGED = "DEFERRED_REVIEW_CHANGED"
    DEFERRED_RUNTIME_INPUT_REQUIRED = (
        "DEFERRED_RUNTIME_INPUT_REQUIRED"
    )
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    FAILED = "FAILED"


class AuthorizedSubmissionOutcome(StrEnum):
    SUBMITTED_VERIFIED = "SUBMITTED_VERIFIED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"


class AuthorizedSubmissionFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PERMIT_NOT_FOUND = "PERMIT_NOT_FOUND"
    PERMIT_INTEGRITY_FAILURE = "PERMIT_INTEGRITY_FAILURE"
    AUTHORIZATION_NOT_FOUND = "AUTHORIZATION_NOT_FOUND"
    AUTHORIZATION_INTEGRITY_FAILURE = "AUTHORIZATION_INTEGRITY_FAILURE"
    EXECUTION_NOT_FOUND = "EXECUTION_NOT_FOUND"
    EXECUTION_INTEGRITY_FAILURE = "EXECUTION_INTEGRITY_FAILURE"
    ENVELOPE_NOT_FOUND = "ENVELOPE_NOT_FOUND"
    ENVELOPE_INTEGRITY_FAILURE = "ENVELOPE_INTEGRITY_FAILURE"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    TOKEN_REFERENCE_FAILURE = "TOKEN_REFERENCE_FAILURE"
    PERMIT_VALIDATION_FAILURE = "PERMIT_VALIDATION_FAILURE"
    PERMIT_ALREADY_CONSUMED = "PERMIT_ALREADY_CONSUMED"
    ENGINE_CONTRACT_FAILURE = "ENGINE_CONTRACT_FAILURE"
    EVIDENCE_VALIDATION_FAILURE = "EVIDENCE_VALIDATION_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    RECORD_INTEGRITY_FAILURE = "RECORD_INTEGRITY_FAILURE"


class AuthorizedSubmissionReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class AuthorizedSubmissionWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean(name: str, value: Any, *, maximum: int = 220) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the execution contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return (
        _aware("timestamp", value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("persisted execution timestamp is invalid")
    return _aware(
        "executed_at", datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuthorizedSubmissionExecutionMetadata:
    browser_broker_contract_version: str
    engine_contract_version: str
    adapter_contract_version: str
    submission_policy_version: str = AUTHORIZED_SUBMISSION_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "browser_broker_contract_version",
            "engine_contract_version",
            "adapter_contract_version",
            "submission_policy_version",
        ):
            _clean(name, getattr(self, name), maximum=120)

    @classmethod
    def default(cls) -> "AuthorizedSubmissionExecutionMetadata":
        return cls(
            browser_broker_contract_version="leased-browser-v1",
            engine_contract_version="job-application-engine-v1",
            adapter_contract_version="jobops.adapter/v1",
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "adapter_contract_version": self.adapter_contract_version,
            "browser_broker_contract_version": (
                self.browser_broker_contract_version
            ),
            "engine_contract_version": self.engine_contract_version,
            "submission_policy_version": self.submission_policy_version,
        }


@dataclass(frozen=True, slots=True)
class BoundedSubmissionEvidence:
    kind: str
    artifact_sha256: str | None
    uri_hash: str | None
    metadata_hash: str
    observed_at: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _clean("evidence kind", self.kind, maximum=80)
        if self.artifact_sha256 is not None:
            _require_hash("artifact_sha256", self.artifact_sha256)
        if self.uri_hash is not None:
            _require_hash("uri_hash", self.uri_hash)
        _require_hash("metadata_hash", self.metadata_hash)
        _clean("observed_at", self.observed_at, maximum=80)
        if self.evidence_hash != _hash(self.identity_dict()):
            raise ValueError("bounded submission evidence hash is invalid")

    @classmethod
    def from_evidence(cls, evidence: EvidenceRef) -> "BoundedSubmissionEvidence":
        values = {
            "artifact_sha256": evidence.sha256,
            "kind": evidence.kind.value,
            "metadata_hash": _hash(dict(evidence.metadata)),
            "observed_at": evidence.observed_at,
            "uri_hash": _hash_text(evidence.uri) if evidence.uri else None,
        }
        return cls(evidence_hash=_hash(values), **values)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BoundedSubmissionEvidence":
        expected = {
            "artifact_sha256",
            "evidence_hash",
            "kind",
            "metadata_hash",
            "observed_at",
            "uri_hash",
        }
        if set(value) != expected:
            raise ValueError("bounded evidence fields are invalid")
        return cls(**dict(value))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "kind": self.kind,
            "metadata_hash": self.metadata_hash,
            "observed_at": self.observed_at,
            "uri_hash": self.uri_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "evidence_hash": self.evidence_hash}


@dataclass(frozen=True, slots=True)
class AuthorizedSubmissionExecutionRecord:
    record_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    submission_permit_record_id: str
    submission_permit_record_hash: str
    permit_jti: str
    authorization_decision_id: str
    authorization_decision_hash: str
    non_submit_execution_record_id: str
    non_submit_execution_record_hash: str
    bundle_canonical_hash: str
    approved_review_hash: str
    actual_pre_submit_review_hash: str
    adapter_platform: str
    submission_intent_id: str | None
    permit_consumption_reference: SubmissionPermitConsumptionReference
    outcome: AuthorizedSubmissionOutcome
    outcome_reference_hash: str
    evidence: tuple[BoundedSubmissionEvidence, ...]
    execution_metadata: AuthorizedSubmissionExecutionMetadata
    record_canonical_hash: str
    executed_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != AUTHORIZED_SUBMISSION_EXECUTION_CONTRACT_VERSION
        ):
            raise ValueError(
                "authorized submission execution version is unsupported"
            )
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "submission_permit_record_id",
            "permit_jti",
            "authorization_decision_id",
            "non_submit_execution_record_id",
            "adapter_platform",
        ):
            _clean(name, getattr(self, name))
        for name in (
            "submission_permit_record_hash",
            "authorization_decision_hash",
            "non_submit_execution_record_hash",
            "bundle_canonical_hash",
            "approved_review_hash",
            "actual_pre_submit_review_hash",
            "outcome_reference_hash",
            "record_canonical_hash",
        ):
            _require_hash(name, getattr(self, name))
        object.__setattr__(
            self, "outcome", AuthorizedSubmissionOutcome(self.outcome)
        )
        if self.approved_review_hash != self.actual_pre_submit_review_hash:
            raise ValueError("submitted Review differs from approved Review")
        if self.submission_intent_id is not None:
            _clean("submission_intent_id", self.submission_intent_id)
        if not isinstance(
            self.permit_consumption_reference,
            SubmissionPermitConsumptionReference,
        ):
            raise TypeError("permit consumption reference is invalid")
        if (
            self.permit_consumption_reference.permit_jti != self.permit_jti
            or self.permit_consumption_reference.job_id != self.job_id
            or self.permit_consumption_reference.action
            is not SubmissionPermitAction.SUBMIT_APPLICATION
        ):
            raise ValueError("permit consumption binding is invalid")
        object.__setattr__(
            self,
            "evidence",
            tuple(
                item
                if isinstance(item, BoundedSubmissionEvidence)
                else BoundedSubmissionEvidence.from_dict(item)
                for item in self.evidence
            ),
        )
        if (
            self.outcome is AuthorizedSubmissionOutcome.SUBMITTED_VERIFIED
            and not self.evidence
        ):
            raise ValueError("verified submission requires evidence")
        if not isinstance(
            self.execution_metadata, AuthorizedSubmissionExecutionMetadata
        ):
            raise TypeError("execution metadata is invalid")
        expected_id = "authorized-submission-execution-" + _hash(
            self.identity_dict()
        )
        if (
            _RECORD_ID_RE.fullmatch(self.record_id) is None
            or self.record_id != expected_id
        ):
            raise ValueError("authorized submission identity is invalid")
        _aware("executed_at", self.executed_at)
        if self.record_canonical_hash != _hash(self.content_dict()):
            raise ValueError("authorized submission record hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "adapter_platform": self.adapter_platform,
            "application_plan_id": self.application_plan_id,
            "approved_review_hash": self.approved_review_hash,
            "authorization_decision_hash": self.authorization_decision_hash,
            "authorization_decision_id": self.authorization_decision_id,
            "bundle_canonical_hash": self.bundle_canonical_hash,
            "contract_version": self.contract_version,
            "execution_metadata": self.execution_metadata.to_dict(),
            "job_id": self.job_id,
            "non_submit_execution_record_hash": (
                self.non_submit_execution_record_hash
            ),
            "non_submit_execution_record_id": (
                self.non_submit_execution_record_id
            ),
            "permit_jti": self.permit_jti,
            "submission_permit_record_hash": (
                self.submission_permit_record_hash
            ),
            "submission_permit_record_id": (
                self.submission_permit_record_id
            ),
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "actual_pre_submit_review_hash": (
                self.actual_pre_submit_review_hash
            ),
            "evidence": [item.to_dict() for item in self.evidence],
            "executed_at": _rfc3339(self.executed_at),
            "outcome": self.outcome.value,
            "outcome_reference_hash": self.outcome_reference_hash,
            "permit_consumption_reference": (
                self.permit_consumption_reference.to_dict()
            ),
            "record_id": self.record_id,
            "submission_intent_id": self.submission_intent_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "record_canonical_hash": self.record_canonical_hash,
        }


@dataclass(frozen=True, slots=True)
class AuthorizedSubmissionReadResult:
    status: AuthorizedSubmissionReadStatus
    record: AuthorizedSubmissionExecutionRecord | None


@dataclass(frozen=True, slots=True)
class AuthorizedSubmissionWriteResult:
    status: AuthorizedSubmissionWriteStatus
    record: AuthorizedSubmissionExecutionRecord | None
    failure_reason: AuthorizedSubmissionFailureReason | None = None


@runtime_checkable
class AuthorizedSubmissionExecutionRepository(Protocol):
    def get(
        self, *, subject_id: str, record_id: str
    ) -> AuthorizedSubmissionReadResult: ...

    def save(
        self, record: AuthorizedSubmissionExecutionRecord
    ) -> AuthorizedSubmissionWriteResult: ...


def _record_from_dict(value: Any) -> AuthorizedSubmissionExecutionRecord:
    required = {
        "actual_pre_submit_review_hash",
        "adapter_platform",
        "application_plan_id",
        "approved_review_hash",
        "authorization_decision_hash",
        "authorization_decision_id",
        "bundle_canonical_hash",
        "contract_version",
        "evidence",
        "executed_at",
        "execution_metadata",
        "job_id",
        "non_submit_execution_record_hash",
        "non_submit_execution_record_id",
        "outcome",
        "outcome_reference_hash",
        "permit_consumption_reference",
        "permit_jti",
        "record_canonical_hash",
        "record_id",
        "submission_intent_id",
        "submission_permit_record_hash",
        "submission_permit_record_id",
        "subject_id",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("persisted authorized submission fields are invalid")
    metadata = value["execution_metadata"]
    consumption = value["permit_consumption_reference"]
    evidence = value["evidence"]
    if (
        not isinstance(metadata, Mapping)
        or not isinstance(consumption, Mapping)
        or not isinstance(evidence, list)
    ):
        raise ValueError("persisted authorized submission components are invalid")
    return AuthorizedSubmissionExecutionRecord(
        **{
            **value,
            "evidence": tuple(
                BoundedSubmissionEvidence.from_dict(item)
                for item in evidence
            ),
            "executed_at": _parse_time(value["executed_at"]),
            "execution_metadata": AuthorizedSubmissionExecutionMetadata(
                **dict(metadata)
            ),
            "permit_consumption_reference": (
                SubmissionPermitConsumptionReference.from_dict(consumption)
            ),
        }
    )


class PrivateHomeAuthorizedSubmissionExecutionRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.authorized_submission_executions
            / _subject_key(_clean("subject_id", subject_id, maximum=160))
        )

    def _path(self, subject_id: str, record_id: str) -> Path:
        if not isinstance(record_id, str) or _RECORD_ID_RE.fullmatch(
            record_id
        ) is None:
            raise ValueError("authorized submission record ID is invalid")
        return self._directory(subject_id) / f"{record_id}.json"

    def get(
        self, *, subject_id: str, record_id: str
    ) -> AuthorizedSubmissionReadResult:
        try:
            path = self._path(subject_id, record_id)
        except (TypeError, ValueError):
            return AuthorizedSubmissionReadResult(
                AuthorizedSubmissionReadStatus.INTEGRITY_FAILURE, None
            )
        with self._lock:
            if not path.exists():
                return AuthorizedSubmissionReadResult(
                    AuthorizedSubmissionReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return AuthorizedSubmissionReadResult(
                    AuthorizedSubmissionReadStatus.INTEGRITY_FAILURE, None
                )
            try:
                record = _record_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return AuthorizedSubmissionReadResult(
                    AuthorizedSubmissionReadStatus.INTEGRITY_FAILURE, None
                )
            if (
                record.subject_id != subject_id.strip()
                or record.record_id != record_id
            ):
                return AuthorizedSubmissionReadResult(
                    AuthorizedSubmissionReadStatus.INTEGRITY_FAILURE, None
                )
            return AuthorizedSubmissionReadResult(
                AuthorizedSubmissionReadStatus.FOUND, record
            )

    def save(
        self, record: AuthorizedSubmissionExecutionRecord
    ) -> AuthorizedSubmissionWriteResult:
        if not isinstance(record, AuthorizedSubmissionExecutionRecord):
            raise TypeError(
                "record must be an AuthorizedSubmissionExecutionRecord"
            )
        path = self._path(record.subject_id, record.record_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            record.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return AuthorizedSubmissionWriteResult(
                    AuthorizedSubmissionWriteStatus.FAILED,
                    None,
                    AuthorizedSubmissionFailureReason.PERSISTENCE_FAILURE,
                )
            if created:
                return AuthorizedSubmissionWriteResult(
                    AuthorizedSubmissionWriteStatus.CREATED, record
                )
            existing = self.get(
                subject_id=record.subject_id, record_id=record.record_id
            )
            if (
                existing.status is AuthorizedSubmissionReadStatus.FOUND
                and existing.record is not None
                and existing.record.record_canonical_hash
                == record.record_canonical_hash
            ):
                return AuthorizedSubmissionWriteResult(
                    AuthorizedSubmissionWriteStatus.UNCHANGED,
                    existing.record,
                )
            return AuthorizedSubmissionWriteResult(
                AuthorizedSubmissionWriteStatus.FAILED,
                None,
                AuthorizedSubmissionFailureReason.RECORD_INTEGRITY_FAILURE,
            )


@runtime_checkable
class BrowserLeaseProvider(Protocol):
    def lease(self, *, owner: str) -> AsyncContextManager[Any]: ...


@runtime_checkable
class AuthorizedSubmissionEngine(Protocol):
    async def execute(self, **kwargs: Any) -> ApplicationOutcome: ...


@dataclass(frozen=True, slots=True)
class ExecuteAuthorizedSubmissionCommand:
    subject_id: str
    submission_permit_record_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ExecuteAuthorizedSubmissionResult:
    status: AuthorizedSubmissionExecutionStatus
    record: AuthorizedSubmissionExecutionRecord | None
    failure_reason: AuthorizedSubmissionFailureReason | None
    message: str


def _result(
    status: AuthorizedSubmissionExecutionStatus,
    message: str,
    *,
    record: AuthorizedSubmissionExecutionRecord | None = None,
    failure_reason: AuthorizedSubmissionFailureReason | None = None,
) -> ExecuteAuthorizedSubmissionResult:
    return ExecuteAuthorizedSubmissionResult(
        status=status,
        record=record,
        failure_reason=failure_reason,
        message=message,
    )


def _identity(permit: Any, metadata: AuthorizedSubmissionExecutionMetadata):
    return {
        "adapter_platform": permit.adapter_platform,
        "application_plan_id": permit.application_plan_id,
        "approved_review_hash": permit.review_digest_hash,
        "authorization_decision_hash": permit.authorization_decision_hash,
        "authorization_decision_id": permit.authorization_decision_id,
        "bundle_canonical_hash": permit.bundle_canonical_hash,
        "contract_version": AUTHORIZED_SUBMISSION_EXECUTION_CONTRACT_VERSION,
        "execution_metadata": metadata.to_dict(),
        "job_id": permit.job_id,
        "non_submit_execution_record_hash": (
            permit.non_submit_execution_record_hash
        ),
        "non_submit_execution_record_id": (
            permit.non_submit_execution_record_id
        ),
        "permit_jti": permit.permit_jti,
        "submission_permit_record_hash": permit.record_canonical_hash,
        "submission_permit_record_id": permit.record_id,
        "subject_id": permit.subject_id,
    }


def _outcome_hash(outcome: ApplicationOutcome) -> str:
    value = outcome.to_dict()
    value.pop("created_at", None)
    return _hash(value)


def _runtime_input_required(outcome: ApplicationOutcome) -> bool:
    return outcome.status in {
        OutcomeStatus.NEEDS_USER,
        OutcomeStatus.NEEDS_USER_LOGIN,
        OutcomeStatus.NEEDS_USER_2FA,
        OutcomeStatus.NEEDS_USER_CAPTCHA,
        OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
        OutcomeStatus.NEEDS_USER_ACCOUNT_LOCKED,
        OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
    } or outcome.reason_code in {
        ReasonCode.UNKNOWN_REQUIRED_QUESTION,
        ReasonCode.SENSITIVE_ANSWER_REQUIRED,
    }


def _make_record(
    *,
    permit: Any,
    consumption: SubmissionPermitConsumptionReference,
    outcome: AuthorizedSubmissionOutcome,
    outcome_reference_hash: str,
    actual_review_hash: str,
    intent_id: str | None,
    evidence: tuple[BoundedSubmissionEvidence, ...],
    metadata: AuthorizedSubmissionExecutionMetadata,
    now: datetime,
) -> AuthorizedSubmissionExecutionRecord:
    identity = _identity(permit, metadata)
    record_id = "authorized-submission-execution-" + _hash(identity)
    content = {
        **identity,
        "actual_pre_submit_review_hash": actual_review_hash,
        "evidence": [item.to_dict() for item in evidence],
        "executed_at": _rfc3339(now),
        "outcome": outcome.value,
        "outcome_reference_hash": outcome_reference_hash,
        "permit_consumption_reference": consumption.to_dict(),
        "record_id": record_id,
        "submission_intent_id": intent_id,
    }
    return AuthorizedSubmissionExecutionRecord(
        record_id=record_id,
        contract_version=AUTHORIZED_SUBMISSION_EXECUTION_CONTRACT_VERSION,
        subject_id=permit.subject_id,
        application_plan_id=permit.application_plan_id,
        job_id=permit.job_id,
        submission_permit_record_id=permit.record_id,
        submission_permit_record_hash=permit.record_canonical_hash,
        permit_jti=permit.permit_jti,
        authorization_decision_id=permit.authorization_decision_id,
        authorization_decision_hash=permit.authorization_decision_hash,
        non_submit_execution_record_id=permit.non_submit_execution_record_id,
        non_submit_execution_record_hash=(
            permit.non_submit_execution_record_hash
        ),
        bundle_canonical_hash=permit.bundle_canonical_hash,
        approved_review_hash=permit.review_digest_hash,
        actual_pre_submit_review_hash=actual_review_hash,
        adapter_platform=permit.adapter_platform,
        submission_intent_id=intent_id,
        permit_consumption_reference=consumption,
        outcome=outcome,
        outcome_reference_hash=outcome_reference_hash,
        evidence=evidence,
        execution_metadata=metadata,
        record_canonical_hash=_hash(content),
        executed_at=now,
    )


def _persist(
    record: AuthorizedSubmissionExecutionRecord,
    repository: AuthorizedSubmissionExecutionRepository,
    *,
    result_status: AuthorizedSubmissionExecutionStatus,
) -> ExecuteAuthorizedSubmissionResult:
    try:
        write = repository.save(record)
    except Exception:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "authorized submission record could not be persisted",
            failure_reason=AuthorizedSubmissionFailureReason.PERSISTENCE_FAILURE,
        )
    if (
        write.status is AuthorizedSubmissionWriteStatus.CREATED
        and write.record is not None
    ):
        return _result(
            result_status,
            "authorized submission execution recorded",
            record=write.record,
        )
    if (
        write.status is AuthorizedSubmissionWriteStatus.UNCHANGED
        and write.record is not None
    ):
        status = (
            AuthorizedSubmissionExecutionStatus.UNCHANGED
            if write.record.outcome
            is AuthorizedSubmissionOutcome.SUBMITTED_VERIFIED
            else AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN
        )
        return _result(
            status,
            "authorized submission execution already exists",
            record=write.record,
        )
    return _result(
        AuthorizedSubmissionExecutionStatus.FAILED,
        "authorized submission persistence failed closed",
        failure_reason=write.failure_reason
        or AuthorizedSubmissionFailureReason.PERSISTENCE_FAILURE,
    )


async def execute_authorized_submission(
    command: ExecuteAuthorizedSubmissionCommand,
    *,
    submission_permit_repository: SubmissionPermitRepository,
    submission_authorization_repository: SubmissionAuthorizationRepository,
    non_submit_execution_repository: NonSubmitApplicationExecutionRepository,
    bundle_envelope_repository: RecoverableApplicationBundleEnvelopeRepository,
    token_store: OpaquePermitTokenStore,
    permit_service: PermitService,
    browser_lease_provider: BrowserLeaseProvider,
    application_engine: AuthorizedSubmissionEngine,
    execution_repository: AuthorizedSubmissionExecutionRepository,
    private_home: PrivateHome,
    execution_metadata: AuthorizedSubmissionExecutionMetadata,
) -> ExecuteAuthorizedSubmissionResult:
    """Replay Review and let the existing Engine consume/submit exactly once."""

    try:
        subject_id = _clean("subject_id", command.subject_id, maximum=160)
        permit_record_id = _clean(
            "submission_permit_record_id",
            command.submission_permit_record_id,
        )
        if _PERMIT_ID_RE.fullmatch(permit_record_id) is None:
            raise ValueError("submission permit record ID is invalid")
        now = _aware("now", command.now).astimezone(timezone.utc)
        if not isinstance(execution_metadata, AuthorizedSubmissionExecutionMetadata):
            raise TypeError("execution metadata is invalid")
    except (AttributeError, TypeError, ValueError) as exc:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            str(exc),
            failure_reason=AuthorizedSubmissionFailureReason.INVALID_REQUEST,
        )

    try:
        permit_read = submission_permit_repository.get(
            subject_id=subject_id, record_id=permit_record_id
        )
    except Exception:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "submission permit record could not be read safely",
            failure_reason=(
                AuthorizedSubmissionFailureReason.PERMIT_INTEGRITY_FAILURE
            ),
        )
    if permit_read.status is SubmissionPermitReadStatus.NOT_FOUND:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "submission permit record was not found",
            failure_reason=AuthorizedSubmissionFailureReason.PERMIT_NOT_FOUND,
        )
    if (
        permit_read.status is not SubmissionPermitReadStatus.FOUND
        or permit_read.record is None
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "submission permit record failed integrity validation",
            failure_reason=(
                AuthorizedSubmissionFailureReason.PERMIT_INTEGRITY_FAILURE
            ),
        )
    permit = permit_read.record
    identity = _identity(permit, execution_metadata)
    record_id = "authorized-submission-execution-" + _hash(identity)
    existing = execution_repository.get(
        subject_id=subject_id, record_id=record_id
    )
    if (
        existing.status is AuthorizedSubmissionReadStatus.FOUND
        and existing.record is not None
    ):
        status = (
            AuthorizedSubmissionExecutionStatus.UNCHANGED
            if existing.record.outcome
            is AuthorizedSubmissionOutcome.SUBMITTED_VERIFIED
            else AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN
        )
        return _result(
            status,
            "authorized submission execution already exists",
            record=existing.record,
        )
    if existing.status is AuthorizedSubmissionReadStatus.INTEGRITY_FAILURE:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "existing authorized submission record failed integrity validation",
            failure_reason=(
                AuthorizedSubmissionFailureReason.RECORD_INTEGRITY_FAILURE
            ),
        )

    if (
        permit.subject_id != subject_id
        or permit.permit_action
        is not SubmissionPermitAction.SUBMIT_APPLICATION
        or now >= permit.expires_at
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "submission permit is outside its authorized scope or expired",
        )

    try:
        decision_read = submission_authorization_repository.get(
            subject_id=subject_id,
            decision_id=permit.authorization_decision_id,
        )
    except Exception:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "submission authorization could not be read safely",
            failure_reason=(
                AuthorizedSubmissionFailureReason
                .AUTHORIZATION_INTEGRITY_FAILURE
            ),
        )

    if (
        decision_read.status is SubmissionAuthorizationReadStatus.NOT_FOUND
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "submission authorization was not found",
            failure_reason=(
                AuthorizedSubmissionFailureReason.AUTHORIZATION_NOT_FOUND
            ),
        )
    if (
        decision_read.status is not SubmissionAuthorizationReadStatus.FOUND
        or decision_read.decision is None
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "submission authorization failed integrity validation",
            failure_reason=(
                AuthorizedSubmissionFailureReason
                .AUTHORIZATION_INTEGRITY_FAILURE
            ),
        )
    decision = decision_read.decision
    try:
        execution_read = non_submit_execution_repository.get(
            subject_id=subject_id,
            record_id=permit.non_submit_execution_record_id,
        )
    except Exception:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "non-submit execution could not be read safely",
            failure_reason=(
                AuthorizedSubmissionFailureReason
                .EXECUTION_INTEGRITY_FAILURE
            ),
        )
    if (
        execution_read.status
        is NonSubmitApplicationExecutionReadStatus.NOT_FOUND
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "non-submit execution was not found",
            failure_reason=AuthorizedSubmissionFailureReason.EXECUTION_NOT_FOUND,
        )
    if (
        execution_read.status
        is not NonSubmitApplicationExecutionReadStatus.FOUND
        or execution_read.record is None
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "non-submit execution failed integrity validation",
            failure_reason=(
                AuthorizedSubmissionFailureReason.EXECUTION_INTEGRITY_FAILURE
            ),
        )
    execution = execution_read.record

    try:
        envelope_read = bundle_envelope_repository.get_for_assembly(
            subject_id=subject_id,
            assembly_record_id=execution.assembly_record_id,
        )
    except Exception:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "bundle envelope could not be read safely",
            failure_reason=(
                AuthorizedSubmissionFailureReason.ENVELOPE_INTEGRITY_FAILURE
            ),
        )
    if (
        envelope_read.status
        is RecoverableApplicationBundleEnvelopeReadStatus.NOT_FOUND
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "bundle envelope was not found",
            failure_reason=AuthorizedSubmissionFailureReason.ENVELOPE_NOT_FOUND,
        )
    if (
        envelope_read.status
        is not RecoverableApplicationBundleEnvelopeReadStatus.FOUND
        or envelope_read.envelope is None
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "bundle envelope failed integrity validation",
            failure_reason=(
                AuthorizedSubmissionFailureReason.ENVELOPE_INTEGRITY_FAILURE
            ),
        )
    envelope = envelope_read.envelope
    bundle = envelope.bundle

    if (
        decision.verdict is not SubmissionAuthorizationVerdict.AUTHORIZED
        or decision.subject_id != subject_id
        or decision.application_plan_id != permit.application_plan_id
        or decision.job_id != permit.job_id
        or decision.decision_id != permit.authorization_decision_id
        or decision.decision_canonical_hash
        != permit.authorization_decision_hash
        or decision.non_submit_execution_record_id != execution.record_id
        or decision.non_submit_execution_record_content_hash
        != execution.record_content_hash
        or decision.bundle_canonical_hash != permit.bundle_canonical_hash
        or decision.review_digest_hash != permit.review_digest_hash
        or execution.subject_id != subject_id
        or execution.application_plan_id != permit.application_plan_id
        or execution.job_id != permit.job_id
        or execution.record_id != permit.non_submit_execution_record_id
        or execution.record_content_hash
        != permit.non_submit_execution_record_hash
        or execution.bundle_canonical_hash != permit.bundle_canonical_hash
        or execution.outcome_checkpoint != permit.review_digest_hash
        or execution.routed_adapter != permit.adapter_platform
        or envelope.subject_id != subject_id
        or envelope.application_plan_id != permit.application_plan_id
        or envelope.assembly_record_id != execution.assembly_record_id
        or envelope.bundle_canonical_hash != permit.bundle_canonical_hash
        or bundle.job.job_id != permit.job_id
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "permit, authorization, execution and Bundle bindings disagree",
            failure_reason=AuthorizedSubmissionFailureReason.BINDING_MISMATCH,
        )

    try:
        token = token_store.load(
            subject_id=subject_id, reference=permit.token_reference
        )
    except Exception:
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "opaque submission permit token could not be restored safely",
            failure_reason=(
                AuthorizedSubmissionFailureReason.TOKEN_REFERENCE_FAILURE
            ),
        )
    try:
        claims = permit_service.verify_at(
            token,
            now=int(now.timestamp()),
            expected_gate=PermitGate.GATE_B,
            expected_bindings=permit.permit_bindings,
        )
    except PermitExpiredError:
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "submission permit is expired",
        )
    except PermitError:
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "submission permit failed signature or binding validation",
            failure_reason=(
                AuthorizedSubmissionFailureReason.PERMIT_VALIDATION_FAILURE
            ),
        )
    if (
        claims.jti != permit.permit_jti
        or claims.prior_gate_jti
        != permit.gate_a_consumption_reference_id
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "submission permit claims disagree with its record",
            failure_reason=(
                AuthorizedSubmissionFailureReason.PERMIT_VALIDATION_FAILURE
            ),
        )
    try:
        permit_service.submission_permit_consumption_reference(
            permit_jti=permit.permit_jti,
            consumer="P2C6_AUTHORIZED_SUBMISSION_EXECUTION",
        )
    except PermitPrerequisiteError:
        pass
    except PermitError:
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "submission permit consumption state is invalid",
            failure_reason=(
                AuthorizedSubmissionFailureReason.PERMIT_ALREADY_CONSUMED
            ),
        )
    else:
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "submission permit was already consumed without a matching record",
            failure_reason=(
                AuthorizedSubmissionFailureReason.PERMIT_ALREADY_CONSUMED
            ),
        )

    outcome: ApplicationOutcome | None = None
    engine_failed = False
    try:
        lease_context = browser_lease_provider.lease(owner=bundle.run_id)
        async with lease_context as leased:
            page = getattr(leased, "page", None)
            if page is None and getattr(leased, "session", None) is not None:
                page = leased.session.page
            browser_lease = getattr(leased, "lease", None)
            if page is None or browser_lease is None:
                raise RuntimeError("Browser lease is incomplete")
            try:
                outcome = await application_engine.execute(
                    page=page,
                    bundle=bundle,
                    request_submit=True,
                    approve_gate_a=False,
                    approved_review_hash=permit.review_digest_hash,
                    submission_permit_token=token,
                    submission_permit_bindings=permit.permit_bindings,
                    browser_lease=browser_lease,
                    private_home=private_home,
                    platform_hint=permit.adapter_platform,
                )
            except Exception:
                engine_failed = True
    except Exception:
        engine_failed = True

    try:
        consumption = (
            permit_service.submission_permit_consumption_reference(
                permit_jti=permit.permit_jti,
                consumer="P2C6_AUTHORIZED_SUBMISSION_EXECUTION",
            )
        )
    except PermitPrerequisiteError:
        consumption = None
    except PermitError:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "submission permit consumption could not be verified",
            failure_reason=(
                AuthorizedSubmissionFailureReason.PERMIT_VALIDATION_FAILURE
            ),
        )

    if engine_failed:
        if consumption is None:
            return _result(
                AuthorizedSubmissionExecutionStatus
                .DEFERRED_BROWSER_UNAVAILABLE,
                "Engine failed before the submission boundary",
            )
        record = _make_record(
            permit=permit,
            consumption=consumption,
            outcome=AuthorizedSubmissionOutcome.SUBMISSION_UNCERTAIN,
            outcome_reference_hash=_hash(
                {"outcome": "ENGINE_EXCEPTION_AFTER_PERMIT_CONSUMPTION"}
            ),
            actual_review_hash=permit.review_digest_hash,
            intent_id=None,
            evidence=(),
            metadata=execution_metadata,
            now=now,
        )
        return _persist(
            record,
            execution_repository,
            result_status=(
                AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN
            ),
        )
    if not isinstance(outcome, ApplicationOutcome):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "Application Engine returned an invalid outcome",
            failure_reason=(
                AuthorizedSubmissionFailureReason.ENGINE_CONTRACT_FAILURE
            ),
        )
    if (
        outcome.run_id != bundle.run_id
        or outcome.job_id != permit.job_id
        or (
            outcome.adapter is not None
            and outcome.adapter != permit.adapter_platform
        )
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "Application Engine outcome binding is invalid",
            failure_reason=(
                AuthorizedSubmissionFailureReason.ENGINE_CONTRACT_FAILURE
            ),
        )

    actual_review_hash = str(
        outcome.details.get("actual_pre_submit_review_hash")
        or outcome.details.get("review_fingerprint")
        or (
            outcome.details.get("review", {}).get("fingerprint")
            if isinstance(outcome.details.get("review"), Mapping)
            else ""
        )
        or ""
    )
    if outcome.status is OutcomeStatus.AWAITING_GATE_B:
        if (
            actual_review_hash
            and actual_review_hash != permit.review_digest_hash
        ):
            return _result(
                AuthorizedSubmissionExecutionStatus.DEFERRED_REVIEW_CHANGED,
                "the replayed Review differs from the authorized Review",
            )
        return _result(
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            "the Engine rejected the submission permit at Gate B",
        )
    if _runtime_input_required(outcome) and consumption is None:
        return _result(
            AuthorizedSubmissionExecutionStatus
            .DEFERRED_RUNTIME_INPUT_REQUIRED,
            "runtime input is required before submission",
        )
    if (
        outcome.status is OutcomeStatus.FAILED_RETRYABLE
        and outcome.reason_code is ReasonCode.RETRYABLE_BROWSER_ERROR
        and consumption is None
    ):
        return _result(
            AuthorizedSubmissionExecutionStatus.DEFERRED_BROWSER_UNAVAILABLE,
            "Engine reported a Browser failure before submission",
        )

    eligible = tuple(
        item
        for item in outcome.evidence_refs
        if item.kind in SUBMISSION_EVIDENCE_KINDS
    )
    if eligible and consumption is None:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "submission evidence exists without permit consumption",
            failure_reason=(
                AuthorizedSubmissionFailureReason.EVIDENCE_VALIDATION_FAILURE
            ),
        )
    if consumption is None:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "Engine ended without consuming the authorized permit",
            failure_reason=(
                AuthorizedSubmissionFailureReason.ENGINE_CONTRACT_FAILURE
            ),
        )
    if actual_review_hash != permit.review_digest_hash:
        return _result(
            AuthorizedSubmissionExecutionStatus.FAILED,
            "consumed permit is not bound to the actual submitted Review",
            failure_reason=AuthorizedSubmissionFailureReason.BINDING_MISMATCH,
        )
    intent_id = (
        str(outcome.details.get("submission_intent_id") or "").strip() or None
    )
    bounded_evidence = tuple(
        BoundedSubmissionEvidence.from_evidence(item) for item in eligible
    )
    if (
        outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
        and bounded_evidence
        and intent_id is not None
    ):
        record = _make_record(
            permit=permit,
            consumption=consumption,
            outcome=AuthorizedSubmissionOutcome.SUBMITTED_VERIFIED,
            outcome_reference_hash=_outcome_hash(outcome),
            actual_review_hash=actual_review_hash,
            intent_id=intent_id,
            evidence=bounded_evidence,
            metadata=execution_metadata,
            now=now,
        )
        return _persist(
            record,
            execution_repository,
            result_status=AuthorizedSubmissionExecutionStatus.CREATED,
        )

    record = _make_record(
        permit=permit,
        consumption=consumption,
        outcome=AuthorizedSubmissionOutcome.SUBMISSION_UNCERTAIN,
        outcome_reference_hash=_outcome_hash(outcome),
        actual_review_hash=actual_review_hash,
        intent_id=intent_id,
        evidence=bounded_evidence,
        metadata=execution_metadata,
        now=now,
    )
    return _persist(
        record,
        execution_repository,
        result_status=AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN,
    )


__all__ = [
    "AUTHORIZED_SUBMISSION_EXECUTION_CONTRACT_VERSION",
    "AUTHORIZED_SUBMISSION_POLICY_VERSION",
    "AuthorizedSubmissionExecutionMetadata",
    "AuthorizedSubmissionExecutionRecord",
    "AuthorizedSubmissionExecutionRepository",
    "AuthorizedSubmissionExecutionStatus",
    "AuthorizedSubmissionFailureReason",
    "AuthorizedSubmissionOutcome",
    "AuthorizedSubmissionReadResult",
    "AuthorizedSubmissionReadStatus",
    "AuthorizedSubmissionWriteResult",
    "AuthorizedSubmissionWriteStatus",
    "BoundedSubmissionEvidence",
    "BrowserLeaseProvider",
    "ExecuteAuthorizedSubmissionCommand",
    "ExecuteAuthorizedSubmissionResult",
    "PrivateHomeAuthorizedSubmissionExecutionRepository",
    "execute_authorized_submission",
]
