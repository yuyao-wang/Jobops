"""Offline Gate B decision for one reviewed, plan-scoped ApplicationBundle."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    NonSubmitApplicationExecutionRecord,
    NonSubmitApplicationExecutionRepository,
    NonSubmitExecutionRecordState,
)
from .outcomes import OutcomePhase, OutcomeStatus, ReasonCode
from .policy import ApprovalActor, SubmitAuthority
from .private_home import PrivateHome, PrivateHomeError
from .recoverable_application_bundle import (
    RecoverableApplicationBundleEnvelopeReadStatus,
    RecoverableApplicationBundleEnvelopeRepository,
)


SUBMISSION_AUTHORIZATION_DECISION_CONTRACT_VERSION = (
    "plan-scoped-submission-authorization-decision-v1"
)
GATE_B_SUBMISSION_POLICY_ID = "existing-policy-decision-gate-b"
GATE_B_SUBMISSION_POLICY_VERSION = "gate-b-submission-policy-v1"
EXPLICIT_SUBMISSION_AUTHORIZATION_CONTRACT_VERSION = (
    "explicit-submission-authorization-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_EXECUTION_ID_RE = re.compile(
    r"^non-submit-application-execution-[a-f0-9]{64}$"
)
_DECISION_ID_RE = re.compile(
    r"^submission-authorization-decision-[a-f0-9]{64}$"
)
_EXPLICIT_ID_RE = re.compile(
    r"^explicit-submission-authorization-[a-f0-9]{64}$"
)


class SubmissionAuthorizationOperationStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class SubmissionAuthorizationResultStatus(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_USER_AUTHORIZATION_REQUIRED = (
        "DEFERRED_USER_AUTHORIZATION_REQUIRED"
    )
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class SubmissionAuthorizationMode(StrEnum):
    AUTOMATIC = "AUTOMATIC"
    EXPLICIT_USER = "EXPLICIT_USER"


class SubmissionAuthorizationVerdict(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    USER_AUTHORIZATION_REQUIRED = "USER_AUTHORIZATION_REQUIRED"
    BLOCKED = "BLOCKED"


class SubmissionAuthorizationScope(StrEnum):
    CURRENT_PLAN_BUNDLE_REVIEW_SUBMISSION = (
        "CURRENT_PLAN_BUNDLE_REVIEW_SUBMISSION"
    )


class SubmissionAuthorizationReason(StrEnum):
    AUTOMATIC_POLICY_AUTHORIZED = "AUTOMATIC_POLICY_AUTHORIZED"
    EXPLICIT_USER_AUTHORIZATION_VALID = (
        "EXPLICIT_USER_AUTHORIZATION_VALID"
    )
    EXPLICIT_USER_AUTHORIZATION_REQUIRED = (
        "EXPLICIT_USER_AUTHORIZATION_REQUIRED"
    )
    EXPLICIT_USER_AUTHORIZATION_MISMATCH = (
        "EXPLICIT_USER_AUTHORIZATION_MISMATCH"
    )
    POLICY_BLOCKED = "POLICY_BLOCKED"
    REVIEW_NOT_READY = "REVIEW_NOT_READY"
    REVIEW_DIGEST_INVALID = "REVIEW_DIGEST_INVALID"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    REQUIRED_FIELD_UNRESOLVED = "REQUIRED_FIELD_UNRESOLVED"
    MATERIAL_UPLOAD_FAILED = "MATERIAL_UPLOAD_FAILED"
    RUNTIME_ATTESTATION_REQUIRED = "RUNTIME_ATTESTATION_REQUIRED"
    RUNTIME_CONSENT_REQUIRED = "RUNTIME_CONSENT_REQUIRED"
    RUNTIME_SIGNATURE_REQUIRED = "RUNTIME_SIGNATURE_REQUIRED"
    RUNTIME_USER_INPUT_REQUIRED = "RUNTIME_USER_INPUT_REQUIRED"
    SUBMISSION_BOUNDARY_VIOLATION = "SUBMISSION_BOUNDARY_VIOLATION"
    BINDING_MISMATCH = "BINDING_MISMATCH"


_RUNTIME_CONFIRMATION_REASONS = {
    "attestation": (
        SubmissionAuthorizationReason.RUNTIME_ATTESTATION_REQUIRED
    ),
    "legal attestation": (
        SubmissionAuthorizationReason.RUNTIME_ATTESTATION_REQUIRED
    ),
    "truthfulness declaration": (
        SubmissionAuthorizationReason.RUNTIME_ATTESTATION_REQUIRED
    ),
    "consent": SubmissionAuthorizationReason.RUNTIME_CONSENT_REQUIRED,
    "background consent": (
        SubmissionAuthorizationReason.RUNTIME_CONSENT_REQUIRED
    ),
    "signature": SubmissionAuthorizationReason.RUNTIME_SIGNATURE_REQUIRED,
    "electronic signature": (
        SubmissionAuthorizationReason.RUNTIME_SIGNATURE_REQUIRED
    ),
}


class SubmissionAuthorizationFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    EXECUTION_RECORD_NOT_FOUND = "EXECUTION_RECORD_NOT_FOUND"
    EXECUTION_RECORD_INTEGRITY_FAILURE = (
        "EXECUTION_RECORD_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    BUNDLE_ENVELOPE_NOT_FOUND = "BUNDLE_ENVELOPE_NOT_FOUND"
    BUNDLE_ENVELOPE_INTEGRITY_FAILURE = (
        "BUNDLE_ENVELOPE_INTEGRITY_FAILURE"
    )
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    DECISION_INTEGRITY_FAILURE = "DECISION_INTEGRITY_FAILURE"


class SubmissionAuthorizationReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class SubmissionAuthorizationWriteStatus(StrEnum):
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
        raise ValueError(f"{name} is outside the authorization contract")
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
        raise ValueError("persisted authorization timestamp is invalid")
    return _aware(
        "decided_at", datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExplicitSubmissionAuthorization:
    authorization_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    non_submit_execution_record_id: str
    review_digest_hash: str
    authorization_scope: SubmissionAuthorizationScope
    authorization_content_hash: str
    authorized_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != EXPLICIT_SUBMISSION_AUTHORIZATION_CONTRACT_VERSION
        ):
            raise ValueError("explicit authorization contract is unsupported")
        _clean("subject_id", self.subject_id, maximum=160)
        _clean("application_plan_id", self.application_plan_id, maximum=180)
        if (
            _EXECUTION_ID_RE.fullmatch(
                self.non_submit_execution_record_id
            )
            is None
        ):
            raise ValueError("execution record ID is invalid")
        _require_hash("review_digest_hash", self.review_digest_hash)
        object.__setattr__(
            self,
            "authorization_scope",
            SubmissionAuthorizationScope(self.authorization_scope),
        )
        expected_id = "explicit-submission-authorization-" + _hash(
            self.identity_dict()
        )
        if (
            _EXPLICIT_ID_RE.fullmatch(self.authorization_id) is None
            or self.authorization_id != expected_id
        ):
            raise ValueError("explicit authorization identity is invalid")
        _aware("authorized_at", self.authorized_at)
        if self.authorization_content_hash != _hash(self.content_dict()):
            raise ValueError("explicit authorization content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "authorization_scope": self.authorization_scope.value,
            "contract_version": self.contract_version,
            "non_submit_execution_record_id": (
                self.non_submit_execution_record_id
            ),
            "review_digest_hash": self.review_digest_hash,
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "authorization_id": self.authorization_id,
            "authorized_at": _rfc3339(self.authorized_at),
        }


def create_explicit_submission_authorization(
    *,
    subject_id: str,
    application_plan_id: str,
    non_submit_execution_record_id: str,
    review_digest_hash: str,
    authorized_at: datetime,
) -> ExplicitSubmissionAuthorization:
    identity = {
        "application_plan_id": _clean(
            "application_plan_id", application_plan_id, maximum=180
        ),
        "authorization_scope": (
            SubmissionAuthorizationScope
            .CURRENT_PLAN_BUNDLE_REVIEW_SUBMISSION.value
        ),
        "contract_version": (
            EXPLICIT_SUBMISSION_AUTHORIZATION_CONTRACT_VERSION
        ),
        "non_submit_execution_record_id": _clean(
            "non_submit_execution_record_id",
            non_submit_execution_record_id,
        ),
        "review_digest_hash": _require_hash(
            "review_digest_hash", review_digest_hash
        ),
        "subject_id": _clean("subject_id", subject_id, maximum=160),
    }
    if (
        _EXECUTION_ID_RE.fullmatch(
            identity["non_submit_execution_record_id"]
        )
        is None
    ):
        raise ValueError("execution record ID is invalid")
    authorization_id = "explicit-submission-authorization-" + _hash(identity)
    content = {
        **identity,
        "authorization_id": authorization_id,
        "authorized_at": _rfc3339(authorized_at),
    }
    return ExplicitSubmissionAuthorization(
        authorization_id=authorization_id,
        authorization_content_hash=_hash(content),
        authorized_at=authorized_at,
        **identity,
    )


@dataclass(frozen=True, slots=True)
class GateBSubmissionPolicyBinding:
    policy_id: str
    policy_version: str
    policy_hash: str
    source_policy_hash: str
    gate_b_actor: ApprovalActor
    submit_authority: SubmitAuthority

    def __post_init__(self) -> None:
        if self.policy_id != GATE_B_SUBMISSION_POLICY_ID:
            raise ValueError("Gate B policy ID is unsupported")
        if self.policy_version != GATE_B_SUBMISSION_POLICY_VERSION:
            raise ValueError("Gate B policy version is unsupported")
        _require_hash("policy_hash", self.policy_hash)
        _require_hash("source_policy_hash", self.source_policy_hash)
        object.__setattr__(
            self, "gate_b_actor", ApprovalActor(self.gate_b_actor)
        )
        object.__setattr__(
            self,
            "submit_authority",
            SubmitAuthority(self.submit_authority),
        )
        if self.policy_hash != _hash(self.identity_dict()):
            raise ValueError("Gate B policy hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "gate_b_actor": self.gate_b_actor.value,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "source_policy_hash": self.source_policy_hash,
            "submit_authority": self.submit_authority.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "policy_hash": self.policy_hash}


def _policy_binding(bundle: Any) -> GateBSubmissionPolicyBinding:
    values = {
        "gate_b_actor": bundle.policy.gate_b_actor.value,
        "policy_id": GATE_B_SUBMISSION_POLICY_ID,
        "policy_version": GATE_B_SUBMISSION_POLICY_VERSION,
        "source_policy_hash": bundle.policy.policy_hash,
        "submit_authority": bundle.policy.submit_authority.value,
    }
    return GateBSubmissionPolicyBinding(
        policy_hash=_hash(values),
        source_policy_hash=bundle.policy.policy_hash,
        gate_b_actor=bundle.policy.gate_b_actor,
        submit_authority=bundle.policy.submit_authority,
        policy_id=GATE_B_SUBMISSION_POLICY_ID,
        policy_version=GATE_B_SUBMISSION_POLICY_VERSION,
    )


@dataclass(frozen=True, slots=True)
class SubmissionAuthorizationDecision:
    decision_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    non_submit_execution_record_id: str
    non_submit_execution_record_content_hash: str
    assembly_record_id: str
    bundle_canonical_hash: str
    review_digest_id: str
    review_digest_hash: str
    fill_validation_outcome_hash: str
    gate_b_policy: GateBSubmissionPolicyBinding
    explicit_user_authorization_id: str | None
    explicit_user_authorization_hash: str | None
    authorization_mode: SubmissionAuthorizationMode
    verdict: SubmissionAuthorizationVerdict
    reasons: tuple[SubmissionAuthorizationReason, ...]
    authorization_scope: SubmissionAuthorizationScope
    decision_canonical_hash: str
    decided_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != SUBMISSION_AUTHORIZATION_DECISION_CONTRACT_VERSION
        ):
            raise ValueError("submission authorization contract is unsupported")
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "non_submit_execution_record_id",
            "assembly_record_id",
            "review_digest_id",
        ):
            _clean(name, getattr(self, name))
        if (
            _EXECUTION_ID_RE.fullmatch(
                self.non_submit_execution_record_id
            )
            is None
        ):
            raise ValueError("execution record ID is invalid")
        for name in (
            "non_submit_execution_record_content_hash",
            "bundle_canonical_hash",
            "review_digest_hash",
            "fill_validation_outcome_hash",
            "decision_canonical_hash",
        ):
            _require_hash(name, getattr(self, name))
        if not isinstance(self.gate_b_policy, GateBSubmissionPolicyBinding):
            raise TypeError("Gate B policy binding is invalid")
        object.__setattr__(
            self,
            "authorization_mode",
            SubmissionAuthorizationMode(self.authorization_mode),
        )
        object.__setattr__(
            self, "verdict", SubmissionAuthorizationVerdict(self.verdict)
        )
        object.__setattr__(
            self,
            "authorization_scope",
            SubmissionAuthorizationScope(self.authorization_scope),
        )
        if not self.reasons:
            raise ValueError("authorization reasons are required")
        object.__setattr__(
            self,
            "reasons",
            tuple(SubmissionAuthorizationReason(item) for item in self.reasons),
        )
        if (self.explicit_user_authorization_id is None) != (
            self.explicit_user_authorization_hash is None
        ):
            raise ValueError("explicit authorization binding is incomplete")
        if self.explicit_user_authorization_id is not None:
            if (
                _EXPLICIT_ID_RE.fullmatch(
                    self.explicit_user_authorization_id
                )
                is None
            ):
                raise ValueError("explicit authorization ID is invalid")
            _require_hash(
                "explicit_user_authorization_hash",
                self.explicit_user_authorization_hash,
            )
        expected_id = "submission-authorization-decision-" + _hash(
            self.identity_dict()
        )
        if (
            _DECISION_ID_RE.fullmatch(self.decision_id) is None
            or self.decision_id != expected_id
        ):
            raise ValueError("submission authorization identity is invalid")
        _aware("decided_at", self.decided_at)
        if self.decision_canonical_hash != _hash(self.content_dict()):
            raise ValueError("submission authorization hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "assembly_record_id": self.assembly_record_id,
            "authorization_scope": self.authorization_scope.value,
            "bundle_canonical_hash": self.bundle_canonical_hash,
            "contract_version": self.contract_version,
            "explicit_user_authorization_hash": (
                self.explicit_user_authorization_hash
            ),
            "explicit_user_authorization_id": (
                self.explicit_user_authorization_id
            ),
            "gate_b_policy_hash": self.gate_b_policy.policy_hash,
            "job_id": self.job_id,
            "non_submit_execution_record_content_hash": (
                self.non_submit_execution_record_content_hash
            ),
            "non_submit_execution_record_id": (
                self.non_submit_execution_record_id
            ),
            "review_digest_hash": self.review_digest_hash,
            "review_digest_id": self.review_digest_id,
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "authorization_mode": self.authorization_mode.value,
            "decided_at": _rfc3339(self.decided_at),
            "decision_id": self.decision_id,
            "fill_validation_outcome_hash": (
                self.fill_validation_outcome_hash
            ),
            "gate_b_policy": self.gate_b_policy.to_dict(),
            "reasons": [item.value for item in self.reasons],
            "verdict": self.verdict.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "decision_canonical_hash": self.decision_canonical_hash,
        }


@dataclass(frozen=True, slots=True)
class SubmissionAuthorizationReadResult:
    status: SubmissionAuthorizationReadStatus
    decision: SubmissionAuthorizationDecision | None


@dataclass(frozen=True, slots=True)
class SubmissionAuthorizationWriteResult:
    status: SubmissionAuthorizationWriteStatus
    decision: SubmissionAuthorizationDecision | None
    failure_reason: SubmissionAuthorizationFailureReason | None = None


@runtime_checkable
class SubmissionAuthorizationRepository(Protocol):
    def get(
        self, *, subject_id: str, decision_id: str
    ) -> SubmissionAuthorizationReadResult: ...

    def save(
        self, decision: SubmissionAuthorizationDecision
    ) -> SubmissionAuthorizationWriteResult: ...


@dataclass(frozen=True, slots=True)
class DecideSubmissionAuthorizationCommand:
    subject_id: str
    non_submit_execution_record_id: str
    now: datetime
    explicit_user_authorization: ExplicitSubmissionAuthorization | None = None


@dataclass(frozen=True, slots=True)
class DecideSubmissionAuthorizationResult:
    operation_status: SubmissionAuthorizationOperationStatus
    status: SubmissionAuthorizationResultStatus
    decision: SubmissionAuthorizationDecision | None
    failure_reason: SubmissionAuthorizationFailureReason | None
    message: str


def _result_without_decision(
    status: SubmissionAuthorizationResultStatus,
    message: str,
    *,
    failure_reason: SubmissionAuthorizationFailureReason | None = None,
) -> DecideSubmissionAuthorizationResult:
    return DecideSubmissionAuthorizationResult(
        SubmissionAuthorizationOperationStatus.FAILED,
        status,
        None,
        failure_reason,
        message,
    )


def _decision_from_dict(value: Any) -> SubmissionAuthorizationDecision:
    required = {
        "application_plan_id",
        "assembly_record_id",
        "authorization_mode",
        "authorization_scope",
        "bundle_canonical_hash",
        "contract_version",
        "decided_at",
        "decision_canonical_hash",
        "decision_id",
        "explicit_user_authorization_hash",
        "explicit_user_authorization_id",
        "fill_validation_outcome_hash",
        "gate_b_policy",
        "gate_b_policy_hash",
        "job_id",
        "non_submit_execution_record_content_hash",
        "non_submit_execution_record_id",
        "reasons",
        "review_digest_hash",
        "review_digest_id",
        "subject_id",
        "verdict",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("persisted authorization decision fields are invalid")
    policy = value["gate_b_policy"]
    if (
        not isinstance(policy, Mapping)
        or value["gate_b_policy_hash"] != policy.get("policy_hash")
    ):
        raise ValueError("persisted Gate B policy binding is invalid")
    constructor_values = dict(value)
    constructor_values.pop("gate_b_policy_hash")
    return SubmissionAuthorizationDecision(
        **{
            **constructor_values,
            "decided_at": _parse_time(value["decided_at"]),
            "gate_b_policy": GateBSubmissionPolicyBinding(**dict(policy)),
            "reasons": tuple(value["reasons"]),
        }
    )


class PrivateHomeSubmissionAuthorizationRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.submission_authorization_decisions
            / _subject_key(_clean("subject_id", subject_id, maximum=160))
        )

    def _path(self, subject_id: str, decision_id: str) -> Path:
        if (
            not isinstance(decision_id, str)
            or _DECISION_ID_RE.fullmatch(decision_id) is None
        ):
            raise ValueError("decision_id is invalid")
        return self._directory(subject_id) / f"{decision_id}.json"

    def get(
        self, *, subject_id: str, decision_id: str
    ) -> SubmissionAuthorizationReadResult:
        try:
            path = self._path(subject_id, decision_id)
        except (TypeError, ValueError):
            return SubmissionAuthorizationReadResult(
                SubmissionAuthorizationReadStatus.INTEGRITY_FAILURE, None
            )
        with self._lock:
            if not path.exists():
                return SubmissionAuthorizationReadResult(
                    SubmissionAuthorizationReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return SubmissionAuthorizationReadResult(
                    SubmissionAuthorizationReadStatus.INTEGRITY_FAILURE, None
                )
            try:
                decision = _decision_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return SubmissionAuthorizationReadResult(
                    SubmissionAuthorizationReadStatus.INTEGRITY_FAILURE, None
                )
            if (
                decision.subject_id != subject_id.strip()
                or decision.decision_id != decision_id
            ):
                return SubmissionAuthorizationReadResult(
                    SubmissionAuthorizationReadStatus.INTEGRITY_FAILURE, None
                )
            return SubmissionAuthorizationReadResult(
                SubmissionAuthorizationReadStatus.FOUND, decision
            )

    def save(
        self, decision: SubmissionAuthorizationDecision
    ) -> SubmissionAuthorizationWriteResult:
        if not isinstance(decision, SubmissionAuthorizationDecision):
            raise TypeError("decision must be a SubmissionAuthorizationDecision")
        path = self._path(decision.subject_id, decision.decision_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            decision.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return SubmissionAuthorizationWriteResult(
                    SubmissionAuthorizationWriteStatus.FAILED,
                    None,
                    SubmissionAuthorizationFailureReason.PERSISTENCE_FAILED,
                )
            if created:
                return SubmissionAuthorizationWriteResult(
                    SubmissionAuthorizationWriteStatus.CREATED, decision
                )
            existing = self.get(
                subject_id=decision.subject_id,
                decision_id=decision.decision_id,
            )
            if (
                existing.status is SubmissionAuthorizationReadStatus.FOUND
                and existing.decision is not None
                and existing.decision.decision_canonical_hash
                == decision.decision_canonical_hash
            ):
                return SubmissionAuthorizationWriteResult(
                    SubmissionAuthorizationWriteStatus.UNCHANGED,
                    existing.decision,
                )
            return SubmissionAuthorizationWriteResult(
                SubmissionAuthorizationWriteStatus.FAILED,
                None,
                SubmissionAuthorizationFailureReason
                .DECISION_INTEGRITY_FAILURE,
            )


def _review_reasons(
    record: NonSubmitApplicationExecutionRecord,
) -> tuple[
    SubmissionAuthorizationVerdict,
    tuple[SubmissionAuthorizationReason, ...],
]:
    if record.submission_attempted or record.outcome_status in {
        OutcomeStatus.SUBMITTED_VERIFIED.value,
        OutcomeStatus.SUBMIT_UNKNOWN.value,
        OutcomeStatus.SUBMITTING.value,
    } or record.outcome_phase in {
        OutcomePhase.SUBMIT.value,
        OutcomePhase.VERIFY.value,
        OutcomePhase.COMPLETE.value,
    }:
        return (
            SubmissionAuthorizationVerdict.BLOCKED,
            (
                SubmissionAuthorizationReason
                .SUBMISSION_BOUNDARY_VIOLATION,
            ),
        )
    if (
        record.execution_state
        is NonSubmitExecutionRecordState.RUNTIME_INPUT_REQUIRED
        or record.runtime_unresolved_controls
    ):
        reasons: list[SubmissionAuthorizationReason] = []
        lowered = tuple(
            item.strip().casefold()
            for item in record.runtime_unresolved_controls
        )
        for item in lowered:
            reason = _RUNTIME_CONFIRMATION_REASONS.get(item)
            if reason is not None and reason not in reasons:
                reasons.append(reason)
        if not reasons:
            reasons.append(
                SubmissionAuthorizationReason
                .RUNTIME_USER_INPUT_REQUIRED
            )
        return (
            SubmissionAuthorizationVerdict.USER_AUTHORIZATION_REQUIRED,
            tuple(reasons),
        )
    if record.outcome_reason_code == ReasonCode.VALIDATION_FAILED.value:
        return (
            SubmissionAuthorizationVerdict.BLOCKED,
            (SubmissionAuthorizationReason.VALIDATION_FAILED,),
        )
    if record.outcome_reason_code == ReasonCode.MISSING_MATERIAL.value:
        return (
            SubmissionAuthorizationVerdict.BLOCKED,
            (SubmissionAuthorizationReason.MATERIAL_UPLOAD_FAILED,),
        )
    if (
        record.execution_state is not NonSubmitExecutionRecordState.REVIEW_READY
        or record.outcome_status != OutcomeStatus.REVIEW_READY.value
        or record.outcome_phase != OutcomePhase.REVIEW.value
        or record.outcome_reason_code != ReasonCode.REVIEW_COMPLETE.value
    ):
        return (
            SubmissionAuthorizationVerdict.BLOCKED,
            (SubmissionAuthorizationReason.REVIEW_NOT_READY,),
        )
    if _HASH_RE.fullmatch(record.outcome_checkpoint) is None:
        return (
            SubmissionAuthorizationVerdict.BLOCKED,
            (SubmissionAuthorizationReason.REVIEW_DIGEST_INVALID,),
        )
    return SubmissionAuthorizationVerdict.AUTHORIZED, ()


def _make_decision(
    *,
    record: NonSubmitApplicationExecutionRecord,
    policy: GateBSubmissionPolicyBinding,
    explicit: ExplicitSubmissionAuthorization | None,
    mode: SubmissionAuthorizationMode,
    verdict: SubmissionAuthorizationVerdict,
    reasons: tuple[SubmissionAuthorizationReason, ...],
    now: datetime,
) -> SubmissionAuthorizationDecision:
    explicit_id = explicit.authorization_id if explicit else None
    explicit_hash = explicit.authorization_content_hash if explicit else None
    review_hash = record.outcome_checkpoint
    identity = {
        "application_plan_id": record.application_plan_id,
        "assembly_record_id": record.assembly_record_id,
        "authorization_scope": (
            SubmissionAuthorizationScope
            .CURRENT_PLAN_BUNDLE_REVIEW_SUBMISSION.value
        ),
        "bundle_canonical_hash": record.bundle_canonical_hash,
        "contract_version": (
            SUBMISSION_AUTHORIZATION_DECISION_CONTRACT_VERSION
        ),
        "explicit_user_authorization_hash": explicit_hash,
        "explicit_user_authorization_id": explicit_id,
        "gate_b_policy_hash": policy.policy_hash,
        "job_id": record.job_id,
        "non_submit_execution_record_content_hash": (
            record.record_content_hash
        ),
        "non_submit_execution_record_id": record.record_id,
        "review_digest_hash": review_hash,
        "review_digest_id": "review-digest-" + review_hash,
        "subject_id": record.subject_id,
    }
    decision_id = "submission-authorization-decision-" + _hash(identity)
    content = {
        **identity,
        "authorization_mode": mode.value,
        "decided_at": _rfc3339(now),
        "decision_id": decision_id,
        "fill_validation_outcome_hash": record.outcome_reference_hash,
        "gate_b_policy": policy.to_dict(),
        "reasons": [item.value for item in reasons],
        "verdict": verdict.value,
    }
    return SubmissionAuthorizationDecision(
        decision_id=decision_id,
        contract_version=(
            SUBMISSION_AUTHORIZATION_DECISION_CONTRACT_VERSION
        ),
        subject_id=record.subject_id,
        application_plan_id=record.application_plan_id,
        job_id=record.job_id,
        non_submit_execution_record_id=record.record_id,
        non_submit_execution_record_content_hash=record.record_content_hash,
        assembly_record_id=record.assembly_record_id,
        bundle_canonical_hash=record.bundle_canonical_hash,
        review_digest_id="review-digest-" + review_hash,
        review_digest_hash=review_hash,
        fill_validation_outcome_hash=record.outcome_reference_hash,
        gate_b_policy=policy,
        explicit_user_authorization_id=explicit_id,
        explicit_user_authorization_hash=explicit_hash,
        authorization_mode=mode,
        verdict=verdict,
        reasons=reasons,
        authorization_scope=(
            SubmissionAuthorizationScope
            .CURRENT_PLAN_BUNDLE_REVIEW_SUBMISSION
        ),
        decision_canonical_hash=_hash(content),
        decided_at=now,
    )


def _persist(
    decision: SubmissionAuthorizationDecision,
    repository: SubmissionAuthorizationRepository,
) -> DecideSubmissionAuthorizationResult:
    try:
        write = repository.save(decision)
    except Exception:
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "submission authorization decision could not be persisted",
            failure_reason=(
                SubmissionAuthorizationFailureReason.PERSISTENCE_FAILED
            ),
        )
    if (
        write.status is SubmissionAuthorizationWriteStatus.UNCHANGED
        and write.decision is not None
    ):
        return DecideSubmissionAuthorizationResult(
            SubmissionAuthorizationOperationStatus.UNCHANGED,
            SubmissionAuthorizationResultStatus.UNCHANGED,
            write.decision,
            None,
            "identical Gate B decision already exists",
        )
    if (
        write.status is not SubmissionAuthorizationWriteStatus.CREATED
        or write.decision is None
    ):
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "submission authorization persistence failed closed",
            failure_reason=write.failure_reason
            or SubmissionAuthorizationFailureReason.PERSISTENCE_FAILED,
        )
    result_status = {
        SubmissionAuthorizationVerdict.AUTHORIZED: (
            SubmissionAuthorizationResultStatus.AUTHORIZED
        ),
        SubmissionAuthorizationVerdict.USER_AUTHORIZATION_REQUIRED: (
            SubmissionAuthorizationResultStatus
            .DEFERRED_USER_AUTHORIZATION_REQUIRED
        ),
        SubmissionAuthorizationVerdict.BLOCKED: (
            SubmissionAuthorizationResultStatus.BLOCKED
        ),
    }[decision.verdict]
    return DecideSubmissionAuthorizationResult(
        SubmissionAuthorizationOperationStatus.CREATED,
        result_status,
        write.decision,
        None,
        "Gate B submission authorization decision created",
    )


def decide_submission_authorization(
    command: DecideSubmissionAuthorizationCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    non_submit_execution_repository: (
        NonSubmitApplicationExecutionRepository
    ),
    bundle_envelope_repository: (
        RecoverableApplicationBundleEnvelopeRepository
    ),
    submission_authorization_repository: (
        SubmissionAuthorizationRepository
    ),
) -> DecideSubmissionAuthorizationResult:
    """Evaluate existing Gate B policy without Browser, Engine, ATS or submit."""

    try:
        subject_id = _clean("subject_id", command.subject_id, maximum=160)
        execution_id = _clean(
            "non_submit_execution_record_id",
            command.non_submit_execution_record_id,
        )
        if _EXECUTION_ID_RE.fullmatch(execution_id) is None:
            raise ValueError("execution record ID is invalid")
        now = _aware("now", command.now)
        explicit = command.explicit_user_authorization
        if explicit is not None and not isinstance(
            explicit, ExplicitSubmissionAuthorization
        ):
            raise TypeError("explicit user authorization is invalid")
    except (AttributeError, TypeError, ValueError) as exc:
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            str(exc),
            failure_reason=SubmissionAuthorizationFailureReason.INVALID_REQUEST,
        )

    try:
        execution_read = non_submit_execution_repository.get(
            subject_id=subject_id, record_id=execution_id
        )
    except Exception:
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "non-submit execution record could not be read safely",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .EXECUTION_RECORD_INTEGRITY_FAILURE
            ),
        )
    if (
        execution_read.status
        is NonSubmitApplicationExecutionReadStatus.NOT_FOUND
    ):
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "non-submit execution record was not found",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .EXECUTION_RECORD_NOT_FOUND
            ),
        )
    if (
        execution_read.status
        is not NonSubmitApplicationExecutionReadStatus.FOUND
        or execution_read.record is None
    ):
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "non-submit execution record failed integrity validation",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .EXECUTION_RECORD_INTEGRITY_FAILURE
            ),
        )
    record = execution_read.record

    try:
        plan_read = application_plan_repository.get(
            record.application_plan_id
        )
    except Exception:
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "ApplicationPlan could not be read safely",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .APPLICATION_PLAN_INTEGRITY_FAILURE
            ),
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "ApplicationPlan was not found",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .APPLICATION_PLAN_NOT_FOUND
            ),
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or plan_read.plan is None
    ):
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "ApplicationPlan failed integrity validation",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .APPLICATION_PLAN_INTEGRITY_FAILURE
            ),
        )
    plan = plan_read.plan

    try:
        envelope_read = bundle_envelope_repository.get_for_assembly(
            subject_id=subject_id,
            assembly_record_id=record.assembly_record_id,
        )
    except Exception:
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "recoverable ApplicationBundle envelope could not be read safely",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .BUNDLE_ENVELOPE_INTEGRITY_FAILURE
            ),
        )
    if (
        envelope_read.status
        is RecoverableApplicationBundleEnvelopeReadStatus.NOT_FOUND
    ):
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "recoverable ApplicationBundle envelope was not found",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .BUNDLE_ENVELOPE_NOT_FOUND
            ),
        )
    if (
        envelope_read.status
        is not RecoverableApplicationBundleEnvelopeReadStatus.FOUND
        or envelope_read.envelope is None
    ):
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.FAILED,
            "recoverable ApplicationBundle envelope failed integrity validation",
            failure_reason=(
                SubmissionAuthorizationFailureReason
                .BUNDLE_ENVELOPE_INTEGRITY_FAILURE
            ),
        )
    envelope = envelope_read.envelope
    bundle = envelope.bundle

    if (
        record.subject_id != subject_id
        or plan.subject_id != subject_id
        or plan.plan_id != record.application_plan_id
        or plan.job_id != record.job_id
        or envelope.subject_id != subject_id
        or envelope.application_plan_id != record.application_plan_id
        or envelope.assembly_record_id != record.assembly_record_id
        or envelope.bundle_canonical_hash != record.bundle_canonical_hash
        or bundle.job.job_id != record.job_id
    ):
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.BLOCKED,
            "Plan, execution record and bundle envelope binding mismatch",
        )

    policy = _policy_binding(bundle)
    review_verdict, review_reasons = _review_reasons(record)
    mode = SubmissionAuthorizationMode.EXPLICIT_USER
    verdict = review_verdict
    reasons = review_reasons

    if review_verdict is SubmissionAuthorizationVerdict.AUTHORIZED:
        if bundle.policy.blockers or (
            bundle.policy.submit_authority is SubmitAuthority.BLOCKED
        ):
            verdict = SubmissionAuthorizationVerdict.BLOCKED
            reasons = (SubmissionAuthorizationReason.POLICY_BLOCKED,)
        elif (
            bundle.policy.gate_b_actor is ApprovalActor.CODEX
            and bundle.policy.submit_authority
            is SubmitAuthority.CODEX_WITH_PERMIT
        ):
            mode = SubmissionAuthorizationMode.AUTOMATIC
            verdict = SubmissionAuthorizationVerdict.AUTHORIZED
            reasons = (
                SubmissionAuthorizationReason
                .AUTOMATIC_POLICY_AUTHORIZED,
            )
        else:
            explicit_valid = explicit is not None and (
                explicit.subject_id == subject_id
                and explicit.application_plan_id == record.application_plan_id
                and explicit.non_submit_execution_record_id == record.record_id
                and explicit.review_digest_hash == record.outcome_checkpoint
                and explicit.authorization_scope
                is SubmissionAuthorizationScope
                .CURRENT_PLAN_BUNDLE_REVIEW_SUBMISSION
            )
            if explicit_valid:
                verdict = SubmissionAuthorizationVerdict.AUTHORIZED
                reasons = (
                    SubmissionAuthorizationReason
                    .EXPLICIT_USER_AUTHORIZATION_VALID,
                )
            elif explicit is not None:
                verdict = SubmissionAuthorizationVerdict.BLOCKED
                reasons = (
                    SubmissionAuthorizationReason
                    .EXPLICIT_USER_AUTHORIZATION_MISMATCH,
                )
            else:
                verdict = (
                    SubmissionAuthorizationVerdict
                    .USER_AUTHORIZATION_REQUIRED
                )
                reasons = (
                    SubmissionAuthorizationReason
                    .EXPLICIT_USER_AUTHORIZATION_REQUIRED,
                )

    if _HASH_RE.fullmatch(record.outcome_checkpoint) is None:
        # A stable digest is required even for a blocked/deferred Decision ID.
        return _result_without_decision(
            SubmissionAuthorizationResultStatus.BLOCKED,
            "review digest is unavailable or invalid",
        )
    decision = _make_decision(
        record=record,
        policy=policy,
        explicit=explicit,
        mode=mode,
        verdict=verdict,
        reasons=reasons,
        now=now,
    )
    return _persist(decision, submission_authorization_repository)


__all__ = [
    "EXPLICIT_SUBMISSION_AUTHORIZATION_CONTRACT_VERSION",
    "GATE_B_SUBMISSION_POLICY_ID",
    "GATE_B_SUBMISSION_POLICY_VERSION",
    "SUBMISSION_AUTHORIZATION_DECISION_CONTRACT_VERSION",
    "DecideSubmissionAuthorizationCommand",
    "DecideSubmissionAuthorizationResult",
    "ExplicitSubmissionAuthorization",
    "GateBSubmissionPolicyBinding",
    "PrivateHomeSubmissionAuthorizationRepository",
    "SubmissionAuthorizationDecision",
    "SubmissionAuthorizationFailureReason",
    "SubmissionAuthorizationMode",
    "SubmissionAuthorizationOperationStatus",
    "SubmissionAuthorizationReadResult",
    "SubmissionAuthorizationReadStatus",
    "SubmissionAuthorizationReason",
    "SubmissionAuthorizationRepository",
    "SubmissionAuthorizationResultStatus",
    "SubmissionAuthorizationScope",
    "SubmissionAuthorizationVerdict",
    "SubmissionAuthorizationWriteResult",
    "SubmissionAuthorizationWriteStatus",
    "create_explicit_submission_authorization",
    "decide_submission_authorization",
]
