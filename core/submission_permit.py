"""Issue one review-scoped submission permit without Browser or submit access."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .application_plan import (
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .non_submit_application_execution import (
    NonSubmitApplicationExecutionReadStatus,
    NonSubmitApplicationExecutionRepository,
    NonSubmitExecutionRecordState,
)
from .outcomes import OutcomePhase, OutcomeStatus, ReasonCode
from .permits import (
    PLAN_SCOPED_SUBMISSION_BINDING_VERSION,
    GateAConsumptionReference,
    OpaquePermitTokenReference,
    OpaquePermitTokenStore,
    PermitError,
    PermitExpiredError,
    PermitGate,
    PermitIssuerUnavailableError,
    PermitService,
    PermitSignerMetadata,
    PlanScopedSubmissionPermitBindings,
    SubmissionPermitAction,
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


SUBMISSION_PERMIT_RECORD_CONTRACT_VERSION = (
    "plan-scoped-submission-permit-record-v1"
)
SUBMISSION_PERMIT_POLICY_ID = "plan-scoped-submission-permit"
SUBMISSION_PERMIT_POLICY_VERSION = "submission-permit-policy-v1"
SUBMISSION_PERMIT_TTL_SECONDS = 300
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_DECISION_ID_RE = re.compile(
    r"^submission-authorization-decision-[a-f0-9]{64}$"
)
_RECORD_ID_RE = re.compile(r"^submission-permit-[a-f0-9]{64}$")


class SubmissionPermitExpiryRule(StrEnum):
    REQUIRE_NEW_AUTHORIZATION = "REQUIRE_NEW_AUTHORIZATION"


class SubmissionPermitStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    DEFERRED_ISSUER_UNAVAILABLE = "DEFERRED_ISSUER_UNAVAILABLE"
    FAILED = "FAILED"


class SubmissionPermitFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    AUTHORIZATION_NOT_FOUND = "AUTHORIZATION_NOT_FOUND"
    AUTHORIZATION_INTEGRITY_FAILURE = "AUTHORIZATION_INTEGRITY_FAILURE"
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    PLAN_INTEGRITY_FAILURE = "PLAN_INTEGRITY_FAILURE"
    EXECUTION_NOT_FOUND = "EXECUTION_NOT_FOUND"
    EXECUTION_INTEGRITY_FAILURE = "EXECUTION_INTEGRITY_FAILURE"
    ENVELOPE_NOT_FOUND = "ENVELOPE_NOT_FOUND"
    ENVELOPE_INTEGRITY_FAILURE = "ENVELOPE_INTEGRITY_FAILURE"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    GATE_A_REFERENCE_INVALID = "GATE_A_REFERENCE_INVALID"
    EXECUTION_NOT_REVIEW_READY = "EXECUTION_NOT_REVIEW_READY"
    SUBMISSION_BOUNDARY_VIOLATION = "SUBMISSION_BOUNDARY_VIOLATION"
    TOKEN_STORE_FAILURE = "TOKEN_STORE_FAILURE"
    PERMIT_VALIDATION_FAILURE = "PERMIT_VALIDATION_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    RECORD_INTEGRITY_FAILURE = "RECORD_INTEGRITY_FAILURE"


class SubmissionPermitReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class SubmissionPermitWriteStatus(StrEnum):
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


def _clean(name: str, value: Any, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the permit contract")
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
        raise ValueError("persisted permit timestamp is invalid")
    return _aware(
        "permit timestamp",
        datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SubmissionPermitPolicy:
    policy_id: str
    policy_version: str
    ttl_seconds: int
    expired_permit_rule: SubmissionPermitExpiryRule
    policy_hash: str

    def __post_init__(self) -> None:
        if (
            self.policy_id != SUBMISSION_PERMIT_POLICY_ID
            or self.policy_version != SUBMISSION_PERMIT_POLICY_VERSION
            or self.ttl_seconds != SUBMISSION_PERMIT_TTL_SECONDS
        ):
            raise ValueError("submission permit policy is unsupported")
        object.__setattr__(
            self,
            "expired_permit_rule",
            SubmissionPermitExpiryRule(self.expired_permit_rule),
        )
        if (
            self.expired_permit_rule
            is not SubmissionPermitExpiryRule.REQUIRE_NEW_AUTHORIZATION
        ):
            raise ValueError("submission permit expiry rule is unsupported")
        if self.policy_hash != _hash(self.identity_dict()):
            raise ValueError("submission permit policy hash is invalid")

    @classmethod
    def v1(cls) -> "SubmissionPermitPolicy":
        values = {
            "expired_permit_rule": (
                SubmissionPermitExpiryRule.REQUIRE_NEW_AUTHORIZATION.value
            ),
            "policy_id": SUBMISSION_PERMIT_POLICY_ID,
            "policy_version": SUBMISSION_PERMIT_POLICY_VERSION,
            "ttl_seconds": SUBMISSION_PERMIT_TTL_SECONDS,
        }
        return cls(policy_hash=_hash(values), **values)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SubmissionPermitPolicy":
        expected = {
            "expired_permit_rule",
            "policy_hash",
            "policy_id",
            "policy_version",
            "ttl_seconds",
        }
        if set(value) != expected:
            raise ValueError("submission permit policy fields are invalid")
        return cls(**dict(value))

    def identity_dict(self) -> dict[str, Any]:
        return {
            "expired_permit_rule": self.expired_permit_rule.value,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "ttl_seconds": self.ttl_seconds,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "policy_hash": self.policy_hash}


@dataclass(frozen=True, slots=True)
class SubmissionPermitRecord:
    record_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    authorization_decision_id: str
    authorization_decision_hash: str
    non_submit_execution_record_id: str
    non_submit_execution_record_hash: str
    bundle_canonical_hash: str
    review_digest_hash: str
    adapter_platform: str
    gate_a_consumption_reference_id: str
    gate_a_consumption_reference_hash: str
    permit_action: SubmissionPermitAction
    permit_jti: str
    permit_bindings: PlanScopedSubmissionPermitBindings
    permit_bindings_hash: str
    token_reference: OpaquePermitTokenReference
    signer_metadata: PermitSignerMetadata
    permit_policy: SubmissionPermitPolicy
    issuance_binding_hash: str
    issued_at: datetime
    expires_at: datetime
    record_canonical_hash: str

    def __post_init__(self) -> None:
        if self.contract_version != SUBMISSION_PERMIT_RECORD_CONTRACT_VERSION:
            raise ValueError("submission permit record version is unsupported")
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "authorization_decision_id",
            "non_submit_execution_record_id",
            "adapter_platform",
            "gate_a_consumption_reference_id",
            "permit_jti",
        ):
            _clean(name, getattr(self, name))
        for name in (
            "authorization_decision_hash",
            "non_submit_execution_record_hash",
            "bundle_canonical_hash",
            "review_digest_hash",
            "gate_a_consumption_reference_hash",
            "permit_bindings_hash",
            "issuance_binding_hash",
            "record_canonical_hash",
        ):
            _require_hash(name, getattr(self, name))
        object.__setattr__(
            self, "permit_action", SubmissionPermitAction(self.permit_action)
        )
        if self.permit_action is not SubmissionPermitAction.SUBMIT_APPLICATION:
            raise ValueError("submission permit action is invalid")
        if not isinstance(
            self.permit_bindings, PlanScopedSubmissionPermitBindings
        ):
            raise TypeError("submission permit bindings are invalid")
        if not isinstance(self.signer_metadata, PermitSignerMetadata):
            raise TypeError("signer metadata is invalid")
        if not isinstance(self.permit_policy, SubmissionPermitPolicy):
            raise TypeError("submission permit policy is invalid")
        if self.permit_bindings.digest != self.permit_bindings_hash:
            raise ValueError("submission permit bindings hash is invalid")
        if (
            self.permit_bindings.subject_id != self.subject_id
            or self.permit_bindings.application_plan_id
            != self.application_plan_id
            or self.permit_bindings.job_id != self.job_id
            or self.permit_bindings.bundle_canonical_hash
            != self.bundle_canonical_hash
            or self.permit_bindings.review_hash != self.review_digest_hash
            or self.permit_bindings.authorization_decision_id
            != self.authorization_decision_id
            or self.permit_bindings.authorization_decision_hash
            != self.authorization_decision_hash
            or self.permit_bindings.execution_record_id
            != self.non_submit_execution_record_id
            or self.permit_bindings.execution_record_hash
            != self.non_submit_execution_record_hash
            or self.permit_bindings.adapter_platform
            != self.adapter_platform
            or self.permit_bindings.action is not self.permit_action
            or self.permit_bindings.permit_policy_version
            != self.permit_policy.policy_version
        ):
            raise ValueError("submission permit record bindings disagree")
        if not isinstance(self.token_reference, OpaquePermitTokenReference):
            raise TypeError("opaque token reference is invalid")
        if (
            self.token_reference.subject_id != self.subject_id
            or self.token_reference.reference_id
            != f"submission-permit-token-{self.permit_jti}"
        ):
            raise ValueError("opaque token reference binding is invalid")
        issued_at = _aware("issued_at", self.issued_at)
        expires_at = _aware("expires_at", self.expires_at)
        if expires_at != issued_at + timedelta(
            seconds=self.permit_policy.ttl_seconds
        ):
            raise ValueError("submission permit expiration is invalid")
        if self.issuance_binding_hash != _hash(self.binding_dict()):
            raise ValueError("submission permit logical binding is invalid")
        expected_id = "submission-permit-" + _hash(
            {
                "issuance_binding_hash": self.issuance_binding_hash,
                "permit_jti": self.permit_jti,
                "token_reference_hash": self.token_reference.reference_hash,
            }
        )
        if (
            _RECORD_ID_RE.fullmatch(self.record_id) is None
            or self.record_id != expected_id
        ):
            raise ValueError("submission permit record identity is invalid")
        if self.record_canonical_hash != _hash(self.content_dict()):
            raise ValueError("submission permit record hash is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "adapter_platform": self.adapter_platform,
            "application_plan_id": self.application_plan_id,
            "authorization_decision_hash": self.authorization_decision_hash,
            "authorization_decision_id": self.authorization_decision_id,
            "bundle_canonical_hash": self.bundle_canonical_hash,
            "contract_version": self.contract_version,
            "gate_a_consumption_reference_hash": (
                self.gate_a_consumption_reference_hash
            ),
            "gate_a_consumption_reference_id": (
                self.gate_a_consumption_reference_id
            ),
            "job_id": self.job_id,
            "non_submit_execution_record_hash": (
                self.non_submit_execution_record_hash
            ),
            "non_submit_execution_record_id": (
                self.non_submit_execution_record_id
            ),
            "permit_action": self.permit_action.value,
            "permit_bindings_hash": self.permit_bindings_hash,
            "permit_policy_hash": self.permit_policy.policy_hash,
            "review_digest_hash": self.review_digest_hash,
            "signer_metadata": self.signer_metadata.to_dict(),
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.binding_dict(),
            "expires_at": _rfc3339(self.expires_at),
            "issuance_binding_hash": self.issuance_binding_hash,
            "issued_at": _rfc3339(self.issued_at),
            "permit_bindings": self.permit_bindings.to_dict(),
            "permit_jti": self.permit_jti,
            "permit_policy": self.permit_policy.to_dict(),
            "record_id": self.record_id,
            "token_reference": self.token_reference.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "record_canonical_hash": self.record_canonical_hash,
        }


@dataclass(frozen=True, slots=True)
class SubmissionPermitReadResult:
    status: SubmissionPermitReadStatus
    record: SubmissionPermitRecord | None


@dataclass(frozen=True, slots=True)
class SubmissionPermitWriteResult:
    status: SubmissionPermitWriteStatus
    record: SubmissionPermitRecord | None
    failure_reason: SubmissionPermitFailureReason | None = None


@runtime_checkable
class SubmissionPermitRepository(Protocol):
    def get(
        self, *, subject_id: str, record_id: str
    ) -> SubmissionPermitReadResult: ...

    def save(
        self, record: SubmissionPermitRecord
    ) -> SubmissionPermitWriteResult: ...

    def find_current_for_authorization(
        self, *, subject_id: str, authorization_decision_id: str
    ) -> SubmissionPermitReadResult: ...


def _record_from_dict(value: Any) -> SubmissionPermitRecord:
    required = {
        "adapter_platform",
        "application_plan_id",
        "authorization_decision_hash",
        "authorization_decision_id",
        "bundle_canonical_hash",
        "contract_version",
        "expires_at",
        "gate_a_consumption_reference_hash",
        "gate_a_consumption_reference_id",
        "issuance_binding_hash",
        "issued_at",
        "job_id",
        "non_submit_execution_record_hash",
        "non_submit_execution_record_id",
        "permit_action",
        "permit_bindings",
        "permit_bindings_hash",
        "permit_jti",
        "permit_policy",
        "permit_policy_hash",
        "record_canonical_hash",
        "record_id",
        "review_digest_hash",
        "signer_metadata",
        "subject_id",
        "token_reference",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("persisted submission permit fields are invalid")
    bindings = value["permit_bindings"]
    token_reference = value["token_reference"]
    signer_metadata = value["signer_metadata"]
    policy = value["permit_policy"]
    if not all(
        isinstance(item, Mapping)
        for item in (bindings, token_reference, signer_metadata, policy)
    ):
        raise ValueError("persisted submission permit components are invalid")
    if value["permit_policy_hash"] != policy.get("policy_hash"):
        raise ValueError("persisted submission permit policy hash is invalid")
    constructor = dict(value)
    constructor.pop("permit_policy_hash")
    return SubmissionPermitRecord(
        **{
            **constructor,
            "expires_at": _parse_time(value["expires_at"]),
            "issued_at": _parse_time(value["issued_at"]),
            "permit_bindings": (
                PlanScopedSubmissionPermitBindings.from_dict(bindings)
            ),
            "permit_policy": SubmissionPermitPolicy.from_dict(policy),
            "signer_metadata": PermitSignerMetadata.from_dict(
                signer_metadata
            ),
            "token_reference": OpaquePermitTokenReference.from_dict(
                token_reference
            ),
        }
    )


class PrivateHomeSubmissionPermitRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.submission_permits
            / _subject_key(_clean("subject_id", subject_id, maximum=160))
        )

    def _path(self, subject_id: str, record_id: str) -> Path:
        if not isinstance(record_id, str) or _RECORD_ID_RE.fullmatch(
            record_id
        ) is None:
            raise ValueError("submission permit record ID is invalid")
        return self._directory(subject_id) / f"{record_id}.json"

    def get(
        self, *, subject_id: str, record_id: str
    ) -> SubmissionPermitReadResult:
        try:
            path = self._path(subject_id, record_id)
        except (TypeError, ValueError):
            return SubmissionPermitReadResult(
                SubmissionPermitReadStatus.INTEGRITY_FAILURE, None
            )
        with self._lock:
            if not path.exists():
                return SubmissionPermitReadResult(
                    SubmissionPermitReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return SubmissionPermitReadResult(
                    SubmissionPermitReadStatus.INTEGRITY_FAILURE, None
                )
            try:
                record = _record_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return SubmissionPermitReadResult(
                    SubmissionPermitReadStatus.INTEGRITY_FAILURE, None
                )
            if (
                record.subject_id != subject_id.strip()
                or record.record_id != record_id
            ):
                return SubmissionPermitReadResult(
                    SubmissionPermitReadStatus.INTEGRITY_FAILURE, None
                )
            return SubmissionPermitReadResult(
                SubmissionPermitReadStatus.FOUND, record
            )

    def save(
        self, record: SubmissionPermitRecord
    ) -> SubmissionPermitWriteResult:
        if not isinstance(record, SubmissionPermitRecord):
            raise TypeError("record must be a SubmissionPermitRecord")
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
                return SubmissionPermitWriteResult(
                    SubmissionPermitWriteStatus.FAILED,
                    None,
                    SubmissionPermitFailureReason.PERSISTENCE_FAILURE,
                )
            if created:
                return SubmissionPermitWriteResult(
                    SubmissionPermitWriteStatus.CREATED, record
                )
            existing = self.get(
                subject_id=record.subject_id, record_id=record.record_id
            )
            if (
                existing.status is SubmissionPermitReadStatus.FOUND
                and existing.record is not None
                and existing.record.record_canonical_hash
                == record.record_canonical_hash
            ):
                return SubmissionPermitWriteResult(
                    SubmissionPermitWriteStatus.UNCHANGED, existing.record
                )
            return SubmissionPermitWriteResult(
                SubmissionPermitWriteStatus.FAILED,
                None,
                SubmissionPermitFailureReason.RECORD_INTEGRITY_FAILURE,
            )

    def find_current_for_authorization(
        self, *, subject_id: str, authorization_decision_id: str
    ) -> SubmissionPermitReadResult:
        try:
            subject_id = _clean("subject_id", subject_id, maximum=160)
            decision_id = _clean(
                "authorization_decision_id", authorization_decision_id
            )
            if _DECISION_ID_RE.fullmatch(decision_id) is None:
                raise ValueError("authorization decision ID is invalid")
            directory = self._directory(subject_id)
        except (TypeError, ValueError):
            return SubmissionPermitReadResult(
                SubmissionPermitReadStatus.INTEGRITY_FAILURE, None
            )
        if not directory.exists():
            return SubmissionPermitReadResult(
                SubmissionPermitReadStatus.NOT_FOUND, None
            )
        if directory.is_symlink() or not directory.is_dir():
            return SubmissionPermitReadResult(
                SubmissionPermitReadStatus.INTEGRITY_FAILURE, None
            )
        records: list[SubmissionPermitRecord] = []
        for path in sorted(directory.glob("submission-permit-*.json")):
            read = self.get(
                subject_id=subject_id, record_id=path.stem
            )
            if read.status is SubmissionPermitReadStatus.INTEGRITY_FAILURE:
                return read
            if (
                read.record is not None
                and read.record.authorization_decision_id == decision_id
            ):
                records.append(read.record)
        if not records:
            return SubmissionPermitReadResult(
                SubmissionPermitReadStatus.NOT_FOUND, None
            )
        records.sort(
            key=lambda item: (
                item.issued_at.astimezone(timezone.utc),
                item.record_id,
            ),
            reverse=True,
        )
        return SubmissionPermitReadResult(
            SubmissionPermitReadStatus.FOUND, records[0]
        )


@dataclass(frozen=True, slots=True)
class IssueSubmissionPermitCommand:
    subject_id: str
    submission_authorization_decision_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class IssueSubmissionPermitResult:
    status: SubmissionPermitStatus
    record: SubmissionPermitRecord | None
    failure_reason: SubmissionPermitFailureReason | None
    message: str


def _result(
    status: SubmissionPermitStatus,
    message: str,
    *,
    record: SubmissionPermitRecord | None = None,
    failure_reason: SubmissionPermitFailureReason | None = None,
) -> IssueSubmissionPermitResult:
    return IssueSubmissionPermitResult(
        status=status,
        record=record,
        failure_reason=failure_reason,
        message=message,
    )


def _execution_is_review_ready(record: Any) -> bool:
    return (
        record.execution_state is NonSubmitExecutionRecordState.REVIEW_READY
        and record.outcome_status == OutcomeStatus.REVIEW_READY.value
        and record.outcome_phase == OutcomePhase.REVIEW.value
        and record.outcome_reason_code == ReasonCode.REVIEW_COMPLETE.value
        and not record.runtime_unresolved_controls
    )


def _submission_boundary_violated(record: Any) -> bool:
    return record.submission_attempted or record.outcome_status in {
        OutcomeStatus.SUBMITTED_VERIFIED.value,
        OutcomeStatus.SUBMIT_UNKNOWN.value,
        OutcomeStatus.SUBMITTING.value,
    } or record.outcome_phase in {
        OutcomePhase.SUBMIT.value,
        OutcomePhase.VERIFY.value,
        OutcomePhase.COMPLETE.value,
    }


def _make_record(
    *,
    decision: Any,
    execution: Any,
    bindings: PlanScopedSubmissionPermitBindings,
    token_reference: OpaquePermitTokenReference,
    signer_metadata: PermitSignerMetadata,
    policy: SubmissionPermitPolicy,
    permit_jti: str,
    issued_at: datetime,
    expires_at: datetime,
) -> SubmissionPermitRecord:
    binding_values = {
        "adapter_platform": execution.routed_adapter,
        "application_plan_id": execution.application_plan_id,
        "authorization_decision_hash": decision.decision_canonical_hash,
        "authorization_decision_id": decision.decision_id,
        "bundle_canonical_hash": execution.bundle_canonical_hash,
        "contract_version": SUBMISSION_PERMIT_RECORD_CONTRACT_VERSION,
        "gate_a_consumption_reference_hash": (
            execution.gate_a_consumption_reference.reference_hash
        ),
        "gate_a_consumption_reference_id": (
            execution.gate_a_consumption_reference.permit_jti
        ),
        "job_id": execution.job_id,
        "non_submit_execution_record_hash": execution.record_content_hash,
        "non_submit_execution_record_id": execution.record_id,
        "permit_action": SubmissionPermitAction.SUBMIT_APPLICATION.value,
        "permit_bindings_hash": bindings.digest,
        "permit_policy_hash": policy.policy_hash,
        "review_digest_hash": decision.review_digest_hash,
        "signer_metadata": signer_metadata.to_dict(),
        "subject_id": execution.subject_id,
    }
    issuance_binding_hash = _hash(binding_values)
    record_id = "submission-permit-" + _hash(
        {
            "issuance_binding_hash": issuance_binding_hash,
            "permit_jti": permit_jti,
            "token_reference_hash": token_reference.reference_hash,
        }
    )
    content = {
        **binding_values,
        "expires_at": _rfc3339(expires_at),
        "issuance_binding_hash": issuance_binding_hash,
        "issued_at": _rfc3339(issued_at),
        "permit_bindings": bindings.to_dict(),
        "permit_jti": permit_jti,
        "permit_policy": policy.to_dict(),
        "record_id": record_id,
        "token_reference": token_reference.to_dict(),
    }
    return SubmissionPermitRecord(
        record_id=record_id,
        contract_version=SUBMISSION_PERMIT_RECORD_CONTRACT_VERSION,
        subject_id=execution.subject_id,
        application_plan_id=execution.application_plan_id,
        job_id=execution.job_id,
        authorization_decision_id=decision.decision_id,
        authorization_decision_hash=decision.decision_canonical_hash,
        non_submit_execution_record_id=execution.record_id,
        non_submit_execution_record_hash=execution.record_content_hash,
        bundle_canonical_hash=execution.bundle_canonical_hash,
        review_digest_hash=decision.review_digest_hash,
        adapter_platform=execution.routed_adapter,
        gate_a_consumption_reference_id=(
            execution.gate_a_consumption_reference.permit_jti
        ),
        gate_a_consumption_reference_hash=(
            execution.gate_a_consumption_reference.reference_hash
        ),
        permit_action=SubmissionPermitAction.SUBMIT_APPLICATION,
        permit_jti=permit_jti,
        permit_bindings=bindings,
        permit_bindings_hash=bindings.digest,
        token_reference=token_reference,
        signer_metadata=signer_metadata,
        permit_policy=policy,
        issuance_binding_hash=issuance_binding_hash,
        issued_at=issued_at,
        expires_at=expires_at,
        record_canonical_hash=_hash(content),
    )


def issue_submission_permit(
    command: IssueSubmissionPermitCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    submission_authorization_repository: SubmissionAuthorizationRepository,
    non_submit_execution_repository: NonSubmitApplicationExecutionRepository,
    bundle_envelope_repository: RecoverableApplicationBundleEnvelopeRepository,
    permit_service: PermitService,
    token_store: OpaquePermitTokenStore,
    permit_policy: SubmissionPermitPolicy,
    submission_permit_repository: SubmissionPermitRepository,
) -> IssueSubmissionPermitResult:
    """Issue one opaque, short-lived Gate B token for an authorized Review."""

    try:
        subject_id = _clean("subject_id", command.subject_id, maximum=160)
        decision_id = _clean(
            "submission_authorization_decision_id",
            command.submission_authorization_decision_id,
        )
        if _DECISION_ID_RE.fullmatch(decision_id) is None:
            raise ValueError("authorization decision ID is invalid")
        supplied_now = _aware("now", command.now)
        issued_at = supplied_now.astimezone(timezone.utc).replace(
            microsecond=0
        )
        if not isinstance(permit_service, PermitService):
            raise TypeError("permit service is invalid")
        if not isinstance(token_store, OpaquePermitTokenStore):
            raise TypeError("opaque token store is invalid")
        if not isinstance(permit_policy, SubmissionPermitPolicy):
            raise TypeError("submission permit policy is invalid")
    except (AttributeError, TypeError, ValueError) as exc:
        return _result(
            SubmissionPermitStatus.FAILED,
            str(exc),
            failure_reason=SubmissionPermitFailureReason.INVALID_REQUEST,
        )

    try:
        decision_read = submission_authorization_repository.get(
            subject_id=subject_id, decision_id=decision_id
        )
    except Exception:
        return _result(
            SubmissionPermitStatus.FAILED,
            "submission authorization could not be read safely",
            failure_reason=(
                SubmissionPermitFailureReason
                .AUTHORIZATION_INTEGRITY_FAILURE
            ),
        )
    if decision_read.status is SubmissionAuthorizationReadStatus.NOT_FOUND:
        return _result(
            SubmissionPermitStatus.FAILED,
            "submission authorization was not found",
            failure_reason=(
                SubmissionPermitFailureReason.AUTHORIZATION_NOT_FOUND
            ),
        )
    if (
        decision_read.status is not SubmissionAuthorizationReadStatus.FOUND
        or decision_read.decision is None
    ):
        return _result(
            SubmissionPermitStatus.FAILED,
            "submission authorization failed integrity validation",
            failure_reason=(
                SubmissionPermitFailureReason
                .AUTHORIZATION_INTEGRITY_FAILURE
            ),
        )
    decision = decision_read.decision
    if decision.verdict is not SubmissionAuthorizationVerdict.AUTHORIZED:
        return _result(
            SubmissionPermitStatus.NOT_AUTHORIZED,
            "submission authorization verdict is not AUTHORIZED",
        )

    try:
        execution_read = non_submit_execution_repository.get(
            subject_id=subject_id,
            record_id=decision.non_submit_execution_record_id,
        )
    except Exception:
        return _result(
            SubmissionPermitStatus.FAILED,
            "non-submit execution could not be read safely",
            failure_reason=(
                SubmissionPermitFailureReason
                .EXECUTION_INTEGRITY_FAILURE
            ),
        )
    if execution_read.status is NonSubmitApplicationExecutionReadStatus.NOT_FOUND:
        return _result(
            SubmissionPermitStatus.FAILED,
            "non-submit execution was not found",
            failure_reason=SubmissionPermitFailureReason.EXECUTION_NOT_FOUND,
        )
    if (
        execution_read.status
        is not NonSubmitApplicationExecutionReadStatus.FOUND
        or execution_read.record is None
    ):
        return _result(
            SubmissionPermitStatus.FAILED,
            "non-submit execution failed integrity validation",
            failure_reason=(
                SubmissionPermitFailureReason
                .EXECUTION_INTEGRITY_FAILURE
            ),
        )
    execution = execution_read.record

    try:
        plan_read = application_plan_repository.get(
            decision.application_plan_id
        )
    except Exception:
        return _result(
            SubmissionPermitStatus.FAILED,
            "ApplicationPlan could not be read safely",
            failure_reason=SubmissionPermitFailureReason.PLAN_INTEGRITY_FAILURE,
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _result(
            SubmissionPermitStatus.FAILED,
            "ApplicationPlan was not found",
            failure_reason=SubmissionPermitFailureReason.PLAN_NOT_FOUND,
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or plan_read.plan is None
    ):
        return _result(
            SubmissionPermitStatus.FAILED,
            "ApplicationPlan failed integrity validation",
            failure_reason=SubmissionPermitFailureReason.PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_read.plan

    try:
        envelope_read = bundle_envelope_repository.get_for_assembly(
            subject_id=subject_id,
            assembly_record_id=decision.assembly_record_id,
        )
    except Exception:
        return _result(
            SubmissionPermitStatus.FAILED,
            "bundle envelope could not be read safely",
            failure_reason=SubmissionPermitFailureReason.ENVELOPE_INTEGRITY_FAILURE,
        )
    if (
        envelope_read.status
        is RecoverableApplicationBundleEnvelopeReadStatus.NOT_FOUND
    ):
        return _result(
            SubmissionPermitStatus.FAILED,
            "bundle envelope was not found",
            failure_reason=SubmissionPermitFailureReason.ENVELOPE_NOT_FOUND,
        )
    if (
        envelope_read.status
        is not RecoverableApplicationBundleEnvelopeReadStatus.FOUND
        or envelope_read.envelope is None
    ):
        return _result(
            SubmissionPermitStatus.FAILED,
            "bundle envelope failed integrity validation",
            failure_reason=SubmissionPermitFailureReason.ENVELOPE_INTEGRITY_FAILURE,
        )
    envelope = envelope_read.envelope
    bundle = envelope.bundle

    if (
        decision.subject_id != subject_id
        or execution.subject_id != subject_id
        or plan.subject_id != subject_id
        or decision.application_plan_id != execution.application_plan_id
        or plan.plan_id != decision.application_plan_id
        or plan.job_id != decision.job_id
        or plan.job_revision != execution.job_revision
        or plan.job_content_hash != execution.job_content_hash
        or decision.job_id != execution.job_id
        or decision.non_submit_execution_record_id != execution.record_id
        or decision.non_submit_execution_record_content_hash
        != execution.record_content_hash
        or decision.bundle_canonical_hash != execution.bundle_canonical_hash
        or decision.review_digest_hash != execution.outcome_checkpoint
        or decision.fill_validation_outcome_hash
        != execution.outcome_reference_hash
        or envelope.subject_id != subject_id
        or envelope.application_plan_id != plan.plan_id
        or envelope.assembly_record_id != execution.assembly_record_id
        or envelope.bundle_canonical_hash != execution.bundle_canonical_hash
        or bundle.job.job_id != execution.job_id
    ):
        return _result(
            SubmissionPermitStatus.FAILED,
            "Plan, authorization, execution and bundle bindings disagree",
            failure_reason=SubmissionPermitFailureReason.BINDING_MISMATCH,
        )
    if _submission_boundary_violated(execution):
        return _result(
            SubmissionPermitStatus.NOT_AUTHORIZED,
            "execution contains a submission boundary violation",
            failure_reason=(
                SubmissionPermitFailureReason
                .SUBMISSION_BOUNDARY_VIOLATION
            ),
        )
    if not _execution_is_review_ready(execution):
        return _result(
            SubmissionPermitStatus.NOT_AUTHORIZED,
            "execution is not a clean Review-ready result",
            failure_reason=(
                SubmissionPermitFailureReason.EXECUTION_NOT_REVIEW_READY
            ),
        )
    reference = execution.gate_a_consumption_reference
    if (
        not isinstance(reference, GateAConsumptionReference)
        or reference.run_id != bundle.run_id
        or reference.job_id != execution.job_id
        or reference.consumer != "P2C3_NON_SUBMIT_EXECUTION"
        or reference.action != "PREPARE_REVIEW"
    ):
        return _result(
            SubmissionPermitStatus.FAILED,
            "Gate A consumption reference is missing",
            failure_reason=(
                SubmissionPermitFailureReason.GATE_A_REFERENCE_INVALID
            ),
        )

    try:
        legacy = bundle.permit_bindings(
            review_hash=decision.review_digest_hash
        )
        bindings = PlanScopedSubmissionPermitBindings(
            contract_version=PLAN_SCOPED_SUBMISSION_BINDING_VERSION,
            **legacy.to_dict(),
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            bundle_canonical_hash=decision.bundle_canonical_hash,
            authorization_decision_id=decision.decision_id,
            authorization_decision_hash=decision.decision_canonical_hash,
            execution_record_id=execution.record_id,
            execution_record_hash=execution.record_content_hash,
            adapter_platform=execution.routed_adapter,
            action=SubmissionPermitAction.SUBMIT_APPLICATION,
            permit_policy_version=permit_policy.policy_version,
        )
        signer_metadata = permit_service.signer_metadata
        if not isinstance(signer_metadata, PermitSignerMetadata):
            raise TypeError("permit signer metadata is invalid")
    except PermitIssuerUnavailableError:
        return _result(
            SubmissionPermitStatus.DEFERRED_ISSUER_UNAVAILABLE,
            "submission permit signer was unavailable",
        )
    except (PermitError, TypeError, ValueError):
        return _result(
            SubmissionPermitStatus.FAILED,
            "submission permit binding or signer metadata is invalid",
            failure_reason=(
                SubmissionPermitFailureReason.PERMIT_VALIDATION_FAILURE
            ),
        )
    logical_binding = _hash(
        {
            "adapter_platform": execution.routed_adapter,
            "application_plan_id": plan.plan_id,
            "authorization_decision_hash": decision.decision_canonical_hash,
            "authorization_decision_id": decision.decision_id,
            "bundle_canonical_hash": decision.bundle_canonical_hash,
            "contract_version": SUBMISSION_PERMIT_RECORD_CONTRACT_VERSION,
            "gate_a_consumption_reference_hash": reference.reference_hash,
            "gate_a_consumption_reference_id": reference.permit_jti,
            "job_id": decision.job_id,
            "non_submit_execution_record_hash": execution.record_content_hash,
            "non_submit_execution_record_id": execution.record_id,
            "permit_action": SubmissionPermitAction.SUBMIT_APPLICATION.value,
            "permit_bindings_hash": bindings.digest,
            "permit_policy_hash": permit_policy.policy_hash,
            "review_digest_hash": decision.review_digest_hash,
            "signer_metadata": signer_metadata.to_dict(),
            "subject_id": subject_id,
        }
    )
    try:
        permit_service.verify_gate_a_consumption_reference(reference)
    except PermitError:
        return _result(
            SubmissionPermitStatus.FAILED,
            "Gate A consumption reference failed ledger validation",
            failure_reason=(
                SubmissionPermitFailureReason.GATE_A_REFERENCE_INVALID
            ),
        )

    try:
        current = submission_permit_repository.find_current_for_authorization(
            subject_id=subject_id,
            authorization_decision_id=decision_id,
        )
    except Exception:
        return _result(
            SubmissionPermitStatus.FAILED,
            "current submission permit could not be read safely",
            failure_reason=(
                SubmissionPermitFailureReason.RECORD_INTEGRITY_FAILURE
            ),
        )
    if current.status is SubmissionPermitReadStatus.INTEGRITY_FAILURE:
        return _result(
            SubmissionPermitStatus.FAILED,
            "current submission permit failed integrity validation",
            failure_reason=(
                SubmissionPermitFailureReason.RECORD_INTEGRITY_FAILURE
            ),
        )
    if (
        current.status is SubmissionPermitReadStatus.FOUND
        and current.record is not None
        and issued_at >= current.record.expires_at
    ):
        return _result(
            SubmissionPermitStatus.NOT_AUTHORIZED,
            "the prior permit expired and policy requires new authorization",
        )
    if (
        current.status is SubmissionPermitReadStatus.FOUND
        and current.record is not None
        and current.record.issuance_binding_hash == logical_binding
    ):
        existing = current.record
        try:
            token = token_store.load(
                subject_id=subject_id,
                reference=existing.token_reference,
            )
            claims = permit_service.verify_at(
                token,
                now=int(issued_at.timestamp()),
                expected_gate=PermitGate.GATE_B,
                expected_bindings=bindings,
            )
        except (PermitError, TypeError, ValueError):
            return _result(
                SubmissionPermitStatus.FAILED,
                "existing permit or token reference failed validation",
                failure_reason=(
                    SubmissionPermitFailureReason
                    .PERMIT_VALIDATION_FAILURE
                ),
            )
        if (
            claims.jti != existing.permit_jti
            or claims.expires_at != int(existing.expires_at.timestamp())
            or claims.prior_gate_jti != reference.permit_jti
        ):
            return _result(
                SubmissionPermitStatus.FAILED,
                "existing permit claims disagree with the immutable record",
                failure_reason=(
                    SubmissionPermitFailureReason
                    .PERMIT_VALIDATION_FAILURE
                ),
            )
        return _result(
            SubmissionPermitStatus.UNCHANGED,
            "identical unexpired submission permit already exists",
            record=existing,
        )

    try:
        token = permit_service.issue_plan_scoped_submission_permit(
            bindings,
            expected_bindings=bindings,
            gate_a_reference=reference,
            issued_at=int(issued_at.timestamp()),
            ttl_seconds=permit_policy.ttl_seconds,
        )
        claims = permit_service.verify_at(
            token,
            now=int(issued_at.timestamp()),
            expected_gate=PermitGate.GATE_B,
            expected_bindings=bindings,
        )
    except PermitExpiredError:
        return _result(
            SubmissionPermitStatus.NOT_AUTHORIZED,
            "Gate A prerequisite or submission authorization is expired",
        )
    except PermitIssuerUnavailableError:
        return _result(
            SubmissionPermitStatus.DEFERRED_ISSUER_UNAVAILABLE,
            "submission permit issuer was unavailable",
        )
    except PermitError:
        return _result(
            SubmissionPermitStatus.FAILED,
            "submission permit prerequisite or binding validation failed",
            failure_reason=(
                SubmissionPermitFailureReason.PERMIT_VALIDATION_FAILURE
            ),
        )
    except (TypeError, ValueError):
        return _result(
            SubmissionPermitStatus.FAILED,
            "submission permit issuance failed validation",
            failure_reason=(
                SubmissionPermitFailureReason.PERMIT_VALIDATION_FAILURE
            ),
        )
    try:
        token_reference = token_store.save(
            subject_id=subject_id,
            reference_id=f"submission-permit-token-{claims.jti}",
            token=token,
        )
    except Exception:
        return _result(
            SubmissionPermitStatus.FAILED,
            "opaque token storage failed",
            failure_reason=SubmissionPermitFailureReason.TOKEN_STORE_FAILURE,
        )

    expires_at = datetime.fromtimestamp(claims.expires_at, timezone.utc)
    record = _make_record(
        decision=decision,
        execution=execution,
        bindings=bindings,
        token_reference=token_reference,
        signer_metadata=signer_metadata,
        policy=permit_policy,
        permit_jti=claims.jti,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    try:
        write = submission_permit_repository.save(record)
    except Exception:
        return _result(
            SubmissionPermitStatus.FAILED,
            "submission permit record could not be persisted",
            failure_reason=SubmissionPermitFailureReason.PERSISTENCE_FAILURE,
        )
    if (
        write.status is SubmissionPermitWriteStatus.CREATED
        and write.record is not None
    ):
        return _result(
            SubmissionPermitStatus.CREATED,
            "plan-scoped submission permit issued",
            record=write.record,
        )
    if (
        write.status is SubmissionPermitWriteStatus.UNCHANGED
        and write.record is not None
    ):
        return _result(
            SubmissionPermitStatus.UNCHANGED,
            "identical submission permit record already exists",
            record=write.record,
        )
    return _result(
        SubmissionPermitStatus.FAILED,
        "submission permit persistence failed closed",
        failure_reason=write.failure_reason
        or SubmissionPermitFailureReason.PERSISTENCE_FAILURE,
    )


__all__ = [
    "SUBMISSION_PERMIT_POLICY_ID",
    "SUBMISSION_PERMIT_POLICY_VERSION",
    "SUBMISSION_PERMIT_RECORD_CONTRACT_VERSION",
    "SUBMISSION_PERMIT_TTL_SECONDS",
    "IssueSubmissionPermitCommand",
    "IssueSubmissionPermitResult",
    "PrivateHomeSubmissionPermitRepository",
    "SubmissionPermitExpiryRule",
    "SubmissionPermitFailureReason",
    "SubmissionPermitPolicy",
    "SubmissionPermitReadResult",
    "SubmissionPermitReadStatus",
    "SubmissionPermitRecord",
    "SubmissionPermitRepository",
    "SubmissionPermitStatus",
    "SubmissionPermitWriteResult",
    "SubmissionPermitWriteStatus",
    "issue_submission_permit",
]
