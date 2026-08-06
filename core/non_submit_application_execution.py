"""Plan-scoped Gate A and one-shot non-submit Application Engine integration."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, AsyncContextManager, Mapping, Protocol, runtime_checkable

from .application_answer_taxonomy import CanonicalApplicationAnswers
from .application_bundle_assembly import (
    ApplicationBundleAssemblyReadStatus,
    ApplicationBundleAssemblyRepository,
    ApplicationBundleAssemblyRecord,
)
from .application_plan import (
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .bundles import (
    ApplicationBundle,
    application_bundle_canonical_hash,
    file_sha256,
    normalized_job_url,
)
from .job_discovery import (
    JobPostingReadRepository,
    JobPostingRepositoryError,
)
from .outcomes import (
    SUBMISSION_EVIDENCE_KINDS,
    ApplicationOutcome,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from .policy import ApprovalActor, JobTier
from .permits import GateAConsumptionReference
from .private_home import PrivateHome, PrivateHomeError
from .recoverable_application_bundle import (
    RecoverableApplicationBundleEnvelopeReadStatus,
    RecoverableApplicationBundleEnvelopeRepository,
)


NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION_V1 = (
    "plan-scoped-non-submit-application-execution-v1"
)
NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION = (
    "plan-scoped-non-submit-application-execution-v2"
)
NON_SUBMIT_EXECUTION_POLICY_VERSION = "non-submit-only-v5"
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ASSEMBLY_ID_RE = re.compile(
    r"^application-bundle-assembly-[a-f0-9]{64}$"
)
_RECORD_ID_RE = re.compile(
    r"^non-submit-application-execution-[a-f0-9]{64}$"
)


class _EngineExecutionError(RuntimeError):
    pass


class NonSubmitApplicationExecutionStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_GATE_A_REQUIRED = "DEFERRED_GATE_A_REQUIRED"
    DEFERRED_BROWSER_UNAVAILABLE = "DEFERRED_BROWSER_UNAVAILABLE"
    DEFERRED_RUNTIME_INPUT_REQUIRED = (
        "DEFERRED_RUNTIME_INPUT_REQUIRED"
    )
    FAILED = "FAILED"


class NonSubmitExecutionRecordState(StrEnum):
    REVIEW_READY = "REVIEW_READY"
    RUNTIME_INPUT_REQUIRED = "RUNTIME_INPUT_REQUIRED"


class GateAOutcome(StrEnum):
    POLICY_AUTHORIZED = "POLICY_AUTHORIZED"
    HUMAN_AUTHORIZED = "HUMAN_AUTHORIZED"


class NonSubmitApplicationExecutionFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    ASSEMBLY_NOT_FOUND = "ASSEMBLY_NOT_FOUND"
    ASSEMBLY_INTEGRITY_FAILURE = "ASSEMBLY_INTEGRITY_FAILURE"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    JOB_POSTING_NOT_FOUND = "JOB_POSTING_NOT_FOUND"
    JOB_POSTING_INTEGRITY_FAILURE = "JOB_POSTING_INTEGRITY_FAILURE"
    BUNDLE_ENVELOPE_NOT_FOUND = "BUNDLE_ENVELOPE_NOT_FOUND"
    BUNDLE_ENVELOPE_INTEGRITY_FAILURE = (
        "BUNDLE_ENVELOPE_INTEGRITY_FAILURE"
    )
    BINDING_MISMATCH = "BINDING_MISMATCH"
    MATERIAL_INTEGRITY_FAILURE = "MATERIAL_INTEGRITY_FAILURE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    ENGINE_CONTRACT_FAILURE = "ENGINE_CONTRACT_FAILURE"
    SUBMISSION_BOUNDARY_VIOLATION = "SUBMISSION_BOUNDARY_VIOLATION"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    RECORD_INTEGRITY_FAILURE = "RECORD_INTEGRITY_FAILURE"


class NonSubmitApplicationExecutionReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class NonSubmitApplicationExecutionWriteStatus(StrEnum):
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
class NonSubmitExecutionMetadata:
    gate_a_contract_version: str
    browser_broker_contract_version: str
    engine_contract_version: str
    adapter_contract_version: str
    non_submit_policy_version: str = NON_SUBMIT_EXECUTION_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "gate_a_contract_version",
            "browser_broker_contract_version",
            "engine_contract_version",
            "adapter_contract_version",
            "non_submit_policy_version",
        ):
            _clean(name, getattr(self, name), maximum=120)

    @classmethod
    def default(cls) -> "NonSubmitExecutionMetadata":
        return cls(
            gate_a_contract_version="permit-gate-a-v1",
            browser_broker_contract_version="leased-browser-v1",
            engine_contract_version="job-application-engine-v1",
            adapter_contract_version="jobops.adapter/v2",
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "adapter_contract_version": self.adapter_contract_version,
            "browser_broker_contract_version": (
                self.browser_broker_contract_version
            ),
            "engine_contract_version": self.engine_contract_version,
            "gate_a_contract_version": self.gate_a_contract_version,
            "non_submit_policy_version": self.non_submit_policy_version,
        }


@dataclass(frozen=True, slots=True)
class ExecuteNonSubmitApplicationCommand:
    subject_id: str
    application_bundle_assembly_record_id: str
    now: datetime
    approve_gate_a: bool = False


@dataclass(frozen=True, slots=True)
class NonSubmitApplicationExecutionRecord:
    record_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    assembly_record_id: str
    assembly_record_content_hash: str
    bundle_canonical_hash: str
    gate_a_outcome: GateAOutcome
    gate_a_binding_hash: str
    gate_a_consumption_reference: GateAConsumptionReference | None
    routed_adapter: str
    outcome_status: str
    outcome_phase: str
    outcome_reason_code: str
    outcome_checkpoint: str
    outcome_reference_hash: str
    runtime_unresolved_controls: tuple[str, ...]
    execution_state: NonSubmitExecutionRecordState
    submission_attempted: bool
    execution_metadata: NonSubmitExecutionMetadata
    record_content_hash: str
    executed_at: datetime

    def __post_init__(self) -> None:
        if self.contract_version not in {
            NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION_V1,
            NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION,
        }:
            raise ValueError("non-submit execution contract is unsupported")
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "assembly_record_id",
            "routed_adapter",
            "outcome_status",
            "outcome_phase",
            "outcome_reason_code",
        ):
            _clean(name, getattr(self, name))
        if _ASSEMBLY_ID_RE.fullmatch(self.assembly_record_id) is None:
            raise ValueError("assembly_record_id is invalid")
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be positive")
        for name in (
            "job_content_hash",
            "assembly_record_content_hash",
            "bundle_canonical_hash",
            "gate_a_binding_hash",
            "outcome_reference_hash",
            "record_content_hash",
        ):
            _require_hash(name, getattr(self, name))
        object.__setattr__(self, "gate_a_outcome", GateAOutcome(self.gate_a_outcome))
        if (
            self.contract_version
            == NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION_V1
        ):
            if self.gate_a_consumption_reference is not None:
                raise ValueError(
                    "v1 non-submit execution cannot contain Gate A provenance"
                )
        else:
            reference = self.gate_a_consumption_reference
            if not isinstance(reference, GateAConsumptionReference):
                raise ValueError(
                    "v2 non-submit execution requires Gate A provenance"
                )
            if (
                reference.job_id != self.job_id
                or reference.consumer != "P2C3_NON_SUBMIT_EXECUTION"
                or reference.action != "PREPARE_REVIEW"
            ):
                raise ValueError("Gate A consumption provenance is invalid")
        object.__setattr__(
            self,
            "execution_state",
            NonSubmitExecutionRecordState(self.execution_state),
        )
        if type(self.submission_attempted) is not bool:
            raise TypeError("submission_attempted must be a boolean")
        if self.submission_attempted:
            raise ValueError("non-submit execution cannot attempt submission")
        if not isinstance(self.runtime_unresolved_controls, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.runtime_unresolved_controls
        ):
            raise ValueError("runtime unresolved controls are invalid")
        if not isinstance(self.execution_metadata, NonSubmitExecutionMetadata):
            raise TypeError("execution metadata is invalid")
        expected_id = "non-submit-application-execution-" + _hash(
            self.identity_dict()
        )
        if (
            _RECORD_ID_RE.fullmatch(self.record_id) is None
            or self.record_id != expected_id
        ):
            raise ValueError("non-submit execution identity is invalid")
        _aware("executed_at", self.executed_at)
        if self.record_content_hash != _hash(self.content_dict()):
            raise ValueError("non-submit execution content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        value = {
            "application_plan_id": self.application_plan_id,
            "assembly_record_content_hash": self.assembly_record_content_hash,
            "assembly_record_id": self.assembly_record_id,
            "bundle_canonical_hash": self.bundle_canonical_hash,
            "contract_version": self.contract_version,
            "execution_metadata": self.execution_metadata.to_dict(),
            "gate_a_binding_hash": self.gate_a_binding_hash,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "subject_id": self.subject_id,
        }
        if (
            self.contract_version
            == NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION
        ):
            assert self.gate_a_consumption_reference is not None
            value["gate_a_consumption_reference_hash"] = (
                self.gate_a_consumption_reference.reference_hash
            )
            value["gate_a_permit_jti"] = (
                self.gate_a_consumption_reference.permit_jti
            )
        return value

    def content_dict(self) -> dict[str, Any]:
        value = {
            **self.identity_dict(),
            "executed_at": _rfc3339(self.executed_at),
            "execution_state": self.execution_state.value,
            "gate_a_outcome": self.gate_a_outcome.value,
            "outcome_checkpoint": self.outcome_checkpoint,
            "outcome_phase": self.outcome_phase,
            "outcome_reason_code": self.outcome_reason_code,
            "outcome_reference_hash": self.outcome_reference_hash,
            "outcome_status": self.outcome_status,
            "record_id": self.record_id,
            "routed_adapter": self.routed_adapter,
            "runtime_unresolved_controls": list(
                self.runtime_unresolved_controls
            ),
            "submission_attempted": self.submission_attempted,
        }
        if (
            self.contract_version
            == NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION
        ):
            assert self.gate_a_consumption_reference is not None
            value["gate_a_consumption_reference"] = (
                self.gate_a_consumption_reference.to_dict()
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "record_content_hash": self.record_content_hash,
        }


@dataclass(frozen=True, slots=True)
class NonSubmitApplicationExecutionReadResult:
    status: NonSubmitApplicationExecutionReadStatus
    record: NonSubmitApplicationExecutionRecord | None


@dataclass(frozen=True, slots=True)
class NonSubmitApplicationExecutionWriteResult:
    status: NonSubmitApplicationExecutionWriteStatus
    record: NonSubmitApplicationExecutionRecord | None
    reason: NonSubmitApplicationExecutionFailureReason | None = None


@runtime_checkable
class NonSubmitApplicationExecutionRepository(Protocol):
    def get(
        self, *, subject_id: str, record_id: str
    ) -> NonSubmitApplicationExecutionReadResult: ...

    def save(
        self, record: NonSubmitApplicationExecutionRecord
    ) -> NonSubmitApplicationExecutionWriteResult: ...


@runtime_checkable
class BrowserLeaseProvider(Protocol):
    def lease(
        self, *, owner: str
    ) -> AsyncContextManager[Any]: ...


@runtime_checkable
class NonSubmitApplicationEngine(Protocol):
    async def execute(self, **kwargs: Any) -> ApplicationOutcome: ...

    def gate_a_consumption_reference(
        self, run_id: str
    ) -> GateAConsumptionReference | None: ...


@dataclass(frozen=True, slots=True)
class ExecuteNonSubmitApplicationResult:
    status: NonSubmitApplicationExecutionStatus
    record: NonSubmitApplicationExecutionRecord | None
    failure_reason: NonSubmitApplicationExecutionFailureReason | None
    outcome_status: str
    retryable: bool
    message: str


def _failed(
    reason: NonSubmitApplicationExecutionFailureReason,
    message: str,
    *,
    retryable: bool = False,
) -> ExecuteNonSubmitApplicationResult:
    return ExecuteNonSubmitApplicationResult(
        NonSubmitApplicationExecutionStatus.FAILED,
        None,
        reason,
        "",
        retryable,
        message,
    )


def _deferred(
    status: NonSubmitApplicationExecutionStatus,
    message: str,
    *,
    outcome_status: str = "",
) -> ExecuteNonSubmitApplicationResult:
    return ExecuteNonSubmitApplicationResult(
        status, None, None, outcome_status, False, message
    )


def _record_from_dict(value: Any) -> NonSubmitApplicationExecutionRecord:
    common = {
        "application_plan_id",
        "assembly_record_content_hash",
        "assembly_record_id",
        "bundle_canonical_hash",
        "contract_version",
        "executed_at",
        "execution_metadata",
        "execution_state",
        "gate_a_binding_hash",
        "gate_a_outcome",
        "job_content_hash",
        "job_id",
        "job_revision",
        "outcome_checkpoint",
        "outcome_phase",
        "outcome_reason_code",
        "outcome_reference_hash",
        "outcome_status",
        "record_content_hash",
        "record_id",
        "routed_adapter",
        "runtime_unresolved_controls",
        "submission_attempted",
        "subject_id",
    }
    if not isinstance(value, Mapping):
        raise ValueError("persisted non-submit execution fields are invalid")
    contract_version = value.get("contract_version")
    if contract_version == NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION_V1:
        required = common
        gate_a_reference = None
    elif contract_version == NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION:
        required = common | {
            "gate_a_consumption_reference",
            "gate_a_consumption_reference_hash",
            "gate_a_permit_jti",
        }
        raw_reference = value.get("gate_a_consumption_reference")
        if not isinstance(raw_reference, Mapping):
            raise ValueError("persisted Gate A provenance is invalid")
        gate_a_reference = GateAConsumptionReference.from_dict(raw_reference)
    else:
        raise ValueError("persisted non-submit execution version is invalid")
    if set(value) != required:
        raise ValueError("persisted non-submit execution fields are invalid")
    metadata = value["execution_metadata"]
    if not isinstance(metadata, Mapping):
        raise ValueError("persisted execution metadata is invalid")
    persisted = dict(value)
    persisted.pop("gate_a_consumption_reference", None)
    persisted.pop("gate_a_consumption_reference_hash", None)
    persisted.pop("gate_a_permit_jti", None)
    return NonSubmitApplicationExecutionRecord(
        **{
            **persisted,
            "executed_at": _parse_time(value["executed_at"]),
            "execution_metadata": NonSubmitExecutionMetadata(**dict(metadata)),
            "gate_a_consumption_reference": gate_a_reference,
            "runtime_unresolved_controls": tuple(
                value["runtime_unresolved_controls"]
            ),
        }
    )


class PrivateHomeNonSubmitApplicationExecutionRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.non_submit_application_executions
            / _subject_key(_clean("subject_id", subject_id, maximum=160))
        )

    def _path(self, subject_id: str, record_id: str) -> Path:
        if not isinstance(record_id, str) or _RECORD_ID_RE.fullmatch(
            record_id
        ) is None:
            raise ValueError("record_id is invalid")
        return self._directory(subject_id) / f"{record_id}.json"

    def get(
        self, *, subject_id: str, record_id: str
    ) -> NonSubmitApplicationExecutionReadResult:
        try:
            path = self._path(subject_id, record_id)
        except (TypeError, ValueError):
            return NonSubmitApplicationExecutionReadResult(
                NonSubmitApplicationExecutionReadStatus.INTEGRITY_FAILURE,
                None,
            )
        with self._lock:
            if not path.exists():
                return NonSubmitApplicationExecutionReadResult(
                    NonSubmitApplicationExecutionReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return NonSubmitApplicationExecutionReadResult(
                    NonSubmitApplicationExecutionReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            try:
                record = _record_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return NonSubmitApplicationExecutionReadResult(
                    NonSubmitApplicationExecutionReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            if (
                record.subject_id != subject_id.strip()
                or record.record_id != record_id
            ):
                return NonSubmitApplicationExecutionReadResult(
                    NonSubmitApplicationExecutionReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            return NonSubmitApplicationExecutionReadResult(
                NonSubmitApplicationExecutionReadStatus.FOUND, record
            )

    def save(
        self, record: NonSubmitApplicationExecutionRecord
    ) -> NonSubmitApplicationExecutionWriteResult:
        if not isinstance(record, NonSubmitApplicationExecutionRecord):
            raise TypeError(
                "record must be a NonSubmitApplicationExecutionRecord"
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
                return NonSubmitApplicationExecutionWriteResult(
                    NonSubmitApplicationExecutionWriteStatus.FAILED,
                    None,
                    NonSubmitApplicationExecutionFailureReason
                    .PERSISTENCE_FAILED,
                )
            if created:
                return NonSubmitApplicationExecutionWriteResult(
                    NonSubmitApplicationExecutionWriteStatus.CREATED, record
                )
            existing = self.get(
                subject_id=record.subject_id, record_id=record.record_id
            )
            if (
                existing.status
                is NonSubmitApplicationExecutionReadStatus.FOUND
                and existing.record is not None
                and existing.record.record_content_hash
                == record.record_content_hash
            ):
                return NonSubmitApplicationExecutionWriteResult(
                    NonSubmitApplicationExecutionWriteStatus.UNCHANGED,
                    existing.record,
                )
            return NonSubmitApplicationExecutionWriteResult(
                NonSubmitApplicationExecutionWriteStatus.FAILED,
                None,
                NonSubmitApplicationExecutionFailureReason
                .RECORD_INTEGRITY_FAILURE,
            )


def _gate_a_binding_hash(
    bundle: ApplicationBundle,
    metadata: NonSubmitExecutionMetadata,
    outcome: GateAOutcome,
) -> str:
    return _hash(
        {
            "gate_a_actor": bundle.policy.gate_a_actor.value,
            "gate_a_contract_version": metadata.gate_a_contract_version,
            "gate_a_outcome": outcome.value,
            "policy_hash": bundle.policy.policy_hash,
        }
    )


def _execution_identity(
    *,
    assembly: ApplicationBundleAssemblyRecord,
    metadata: NonSubmitExecutionMetadata,
    gate_a_binding_hash: str,
    gate_a_consumption_reference: GateAConsumptionReference,
) -> dict[str, Any]:
    return {
        "application_plan_id": assembly.application_plan_id,
        "assembly_record_content_hash": assembly.record_content_hash,
        "assembly_record_id": assembly.record_id,
        "bundle_canonical_hash": assembly.application_bundle_canonical_hash,
        "contract_version": NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION,
        "execution_metadata": metadata.to_dict(),
        "gate_a_binding_hash": gate_a_binding_hash,
        "gate_a_consumption_reference_hash": (
            gate_a_consumption_reference.reference_hash
        ),
        "gate_a_permit_jti": gate_a_consumption_reference.permit_jti,
        "job_content_hash": assembly.job_content_hash,
        "job_id": assembly.job_id,
        "job_revision": assembly.job_revision,
        "subject_id": assembly.subject_id,
    }


def _validate_materials(
    bundle: ApplicationBundle, *, home: PrivateHome, subject_id: str
) -> bool:
    subject_key = _subject_key(subject_id)
    try:
        resume = bundle.materials.resume_path
        if resume.is_symlink() or not resume.is_file():
            return False
        relative_resume = resume.resolve().relative_to(
            home.root.expanduser().resolve()
        )
        if (
            subject_key not in relative_resume.parts
            or relative_resume.parts[:2] != ("state", "preparation")
            or file_sha256(resume) != bundle.materials.resume_sha256
        ):
            return False
        cover = bundle.materials.cover_letter_pdf
        if cover is None:
            return False
        if subject_key not in PurePosixPath(cover.reference).parts:
            return False
        cover_path = home.contained_path(cover.reference)
        if cover_path.is_symlink() or not cover_path.is_file():
            return False
        content = cover_path.read_bytes()
        return (
            len(content) == cover.byte_size
            and hashlib.sha256(content).hexdigest() == cover.sha256
            and content.startswith(b"%PDF-")
        )
    except (OSError, ValueError, PrivateHomeError):
        return False


def _outcome_reference_hash(outcome: ApplicationOutcome) -> str:
    value = outcome.to_dict()
    value.pop("created_at", None)
    return _hash(value)


def _runtime_unresolved(outcome: ApplicationOutcome) -> tuple[str, ...]:
    review = outcome.details.get("review")
    if not isinstance(review, Mapping):
        return ()
    values: list[Any] = []
    for key in ("unresolved_required", "validation_errors"):
        raw = review.get(key)
        if isinstance(raw, (list, tuple)):
            values.extend(raw)
    return tuple(
        sorted(
            {
                str(item).strip()
                for item in values
                if isinstance(item, str) and item.strip()
            }
        )
    )


def _submission_boundary_violated(outcome: ApplicationOutcome) -> bool:
    if outcome.status in {
        OutcomeStatus.SUBMITTED_VERIFIED,
        OutcomeStatus.SUBMIT_UNKNOWN,
        OutcomeStatus.SUBMITTING,
    }:
        return True
    if outcome.phase in {
        OutcomePhase.SUBMIT,
        OutcomePhase.VERIFY,
        OutcomePhase.COMPLETE,
    }:
        return True
    return any(
        item.kind in SUBMISSION_EVIDENCE_KINDS
        for item in outcome.evidence_refs
    )


def _is_runtime_input_required(outcome: ApplicationOutcome) -> bool:
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
    assembly: ApplicationBundleAssemblyRecord,
    gate_a_outcome: GateAOutcome,
    gate_a_binding_hash: str,
    gate_a_consumption_reference: GateAConsumptionReference,
    metadata: NonSubmitExecutionMetadata,
    outcome: ApplicationOutcome,
    execution_state: NonSubmitExecutionRecordState,
    now: datetime,
) -> NonSubmitApplicationExecutionRecord:
    identity = _execution_identity(
        assembly=assembly,
        metadata=metadata,
        gate_a_binding_hash=gate_a_binding_hash,
        gate_a_consumption_reference=gate_a_consumption_reference,
    )
    record_id = "non-submit-application-execution-" + _hash(identity)
    reason = (
        outcome.reason_code.value
        if isinstance(outcome.reason_code, ReasonCode)
        else str(outcome.reason_code)
    )
    values = {
        **identity,
        "executed_at": _rfc3339(now),
        "execution_state": execution_state.value,
        "gate_a_outcome": gate_a_outcome.value,
        "gate_a_consumption_reference": (
            gate_a_consumption_reference.to_dict()
        ),
        "outcome_checkpoint": str(outcome.checkpoint or ""),
        "outcome_phase": outcome.phase.value,
        "outcome_reason_code": reason,
        "outcome_reference_hash": _outcome_reference_hash(outcome),
        "outcome_status": outcome.status.value,
        "record_id": record_id,
        "routed_adapter": str(outcome.adapter or "unknown"),
        "runtime_unresolved_controls": list(_runtime_unresolved(outcome)),
        "submission_attempted": False,
    }
    return NonSubmitApplicationExecutionRecord(
        record_id=record_id,
        contract_version=NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION,
        subject_id=assembly.subject_id,
        application_plan_id=assembly.application_plan_id,
        job_id=assembly.job_id,
        job_revision=assembly.job_revision,
        job_content_hash=assembly.job_content_hash,
        assembly_record_id=assembly.record_id,
        assembly_record_content_hash=assembly.record_content_hash,
        bundle_canonical_hash=assembly.application_bundle_canonical_hash,
        gate_a_outcome=gate_a_outcome,
        gate_a_binding_hash=gate_a_binding_hash,
        gate_a_consumption_reference=gate_a_consumption_reference,
        routed_adapter=str(outcome.adapter or "unknown"),
        outcome_status=outcome.status.value,
        outcome_phase=outcome.phase.value,
        outcome_reason_code=reason,
        outcome_checkpoint=str(outcome.checkpoint or ""),
        outcome_reference_hash=_outcome_reference_hash(outcome),
        runtime_unresolved_controls=_runtime_unresolved(outcome),
        execution_state=execution_state,
        submission_attempted=False,
        execution_metadata=metadata,
        record_content_hash=_hash(values),
        executed_at=now,
    )


async def execute_non_submit_application(
    command: ExecuteNonSubmitApplicationCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    assembly_repository: ApplicationBundleAssemblyRepository,
    bundle_envelope_repository: RecoverableApplicationBundleEnvelopeRepository,
    job_posting_repository: JobPostingReadRepository,
    browser_lease_provider: BrowserLeaseProvider,
    application_engine: NonSubmitApplicationEngine,
    execution_repository: NonSubmitApplicationExecutionRepository,
    private_home: PrivateHome,
    execution_metadata: NonSubmitExecutionMetadata,
) -> ExecuteNonSubmitApplicationResult:
    """Run one recovered P2c1 bundle to Review without submit authority."""

    try:
        subject_id = _clean("subject_id", command.subject_id, maximum=160)
        assembly_id = _clean(
            "application_bundle_assembly_record_id",
            command.application_bundle_assembly_record_id,
        )
        if _ASSEMBLY_ID_RE.fullmatch(assembly_id) is None:
            raise ValueError("assembly record ID is invalid")
        now = _aware("now", command.now)
        if type(command.approve_gate_a) is not bool:
            raise TypeError("approve_gate_a must be a boolean")
        if not isinstance(execution_metadata, NonSubmitExecutionMetadata):
            raise TypeError("execution metadata is invalid")
    except (AttributeError, TypeError, ValueError) as exc:
        return _failed(
            NonSubmitApplicationExecutionFailureReason.INVALID_REQUEST,
            str(exc),
        )

    try:
        assembly_read = assembly_repository.get(
            subject_id=subject_id, record_id=assembly_id
        )
    except Exception:
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .ASSEMBLY_INTEGRITY_FAILURE,
            "ApplicationBundleAssemblyRecord could not be read safely",
        )
    if assembly_read.status is ApplicationBundleAssemblyReadStatus.NOT_FOUND:
        return _failed(
            NonSubmitApplicationExecutionFailureReason.ASSEMBLY_NOT_FOUND,
            "ApplicationBundleAssemblyRecord was not found",
        )
    if (
        assembly_read.status is not ApplicationBundleAssemblyReadStatus.FOUND
        or assembly_read.record is None
    ):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .ASSEMBLY_INTEGRITY_FAILURE,
            "ApplicationBundleAssemblyRecord failed integrity validation",
        )
    assembly = assembly_read.record

    try:
        plan_read = application_plan_repository.get(
            assembly.application_plan_id
        )
    except Exception:
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
            "ApplicationPlan could not be read safely",
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .APPLICATION_PLAN_NOT_FOUND,
            "ApplicationPlan was not found",
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or plan_read.plan is None
    ):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
            "ApplicationPlan failed integrity validation",
        )
    plan = plan_read.plan

    try:
        posting = job_posting_repository.get(assembly.job_id)
    except (JobPostingRepositoryError, OSError, TypeError, ValueError):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .JOB_POSTING_INTEGRITY_FAILURE,
            "JobPosting could not be read safely",
        )
    if posting is None:
        return _failed(
            NonSubmitApplicationExecutionFailureReason.JOB_POSTING_NOT_FOUND,
            "JobPosting was not found",
        )

    try:
        envelope_read = bundle_envelope_repository.get_for_assembly(
            subject_id=subject_id, assembly_record_id=assembly_id
        )
    except Exception:
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .BUNDLE_ENVELOPE_INTEGRITY_FAILURE,
            "recoverable ApplicationBundle envelope could not be read safely",
        )
    if (
        envelope_read.status
        is RecoverableApplicationBundleEnvelopeReadStatus.NOT_FOUND
    ):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .BUNDLE_ENVELOPE_NOT_FOUND,
            "recoverable ApplicationBundle envelope was not found",
        )
    if (
        envelope_read.status
        is not RecoverableApplicationBundleEnvelopeReadStatus.FOUND
        or envelope_read.envelope is None
    ):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .BUNDLE_ENVELOPE_INTEGRITY_FAILURE,
            "recoverable ApplicationBundle envelope failed integrity validation",
        )
    envelope = envelope_read.envelope
    bundle = envelope.bundle

    try:
        expected_url = normalized_job_url(
            posting.application_url or posting.source_url
        )
        actual_bundle_hash = application_bundle_canonical_hash(bundle)
    except (TypeError, ValueError):
        return _failed(
            NonSubmitApplicationExecutionFailureReason.BINDING_MISMATCH,
            "ApplicationBundle binding could not be validated",
        )
    if (
        assembly.subject_id != subject_id
        or plan.subject_id != subject_id
        or plan.plan_id != assembly.application_plan_id
        or plan.job_id != assembly.job_id
        or plan.job_revision != assembly.job_revision
        or plan.job_content_hash != assembly.job_content_hash
        or posting.job_id != assembly.job_id
        or posting.revision != assembly.job_revision
        or posting.content_hash != assembly.job_content_hash
        or envelope.subject_id != subject_id
        or envelope.application_plan_id != assembly.application_plan_id
        or envelope.assembly_record_id != assembly.record_id
        or envelope.assembly_record_content_hash
        != assembly.record_content_hash
        or envelope.bundle_canonical_hash
        != assembly.application_bundle_canonical_hash
        or actual_bundle_hash != assembly.application_bundle_canonical_hash
        or bundle.job.job_id != assembly.job_id
        or bundle.job.url != expected_url
        or not isinstance(bundle.answers, CanonicalApplicationAnswers)
        or bundle.answers.taxonomy_version != assembly.taxonomy_version
    ):
        return _failed(
            NonSubmitApplicationExecutionFailureReason.BINDING_MISMATCH,
            "Plan, JobPosting, AssemblyRecord and bundle envelope do not agree",
        )
    if not _validate_materials(
        bundle, home=private_home, subject_id=subject_id
    ):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .MATERIAL_INTEGRITY_FAILURE,
            "managed Resume or Cover Letter failed execution-boundary validation",
        )
    if bundle.policy.blockers:
        return _failed(
            NonSubmitApplicationExecutionFailureReason.POLICY_BLOCKED,
            "ApplicationBundle policy contains blocking signals",
        )

    if bundle.policy.gate_a_actor is ApprovalActor.HUMAN:
        if (
            not command.approve_gate_a
            or bundle.policy.tier is not JobTier.LOW
        ):
            return _deferred(
                NonSubmitApplicationExecutionStatus
                .DEFERRED_GATE_A_REQUIRED,
                "Gate A requires explicit human approval",
            )
        gate_a_outcome = GateAOutcome.HUMAN_AUTHORIZED
    else:
        gate_a_outcome = GateAOutcome.POLICY_AUTHORIZED
    gate_binding = _gate_a_binding_hash(
        bundle, execution_metadata, gate_a_outcome
    )
    try:
        prior_gate_reference = (
            application_engine.gate_a_consumption_reference(bundle.run_id)
        )
    except Exception:
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .ENGINE_CONTRACT_FAILURE,
            "Gate A consumption provenance could not be read safely",
        )
    if prior_gate_reference is not None:
        if (
            not isinstance(prior_gate_reference, GateAConsumptionReference)
            or prior_gate_reference.run_id != bundle.run_id
            or prior_gate_reference.job_id != assembly.job_id
        ):
            return _failed(
                NonSubmitApplicationExecutionFailureReason
                .ENGINE_CONTRACT_FAILURE,
                "Gate A consumption provenance binding is invalid",
            )
        identity = _execution_identity(
            assembly=assembly,
            metadata=execution_metadata,
            gate_a_binding_hash=gate_binding,
            gate_a_consumption_reference=prior_gate_reference,
        )
        record_id = "non-submit-application-execution-" + _hash(identity)
        existing = execution_repository.get(
            subject_id=subject_id, record_id=record_id
        )
        if (
            existing.status
            is NonSubmitApplicationExecutionReadStatus.FOUND
            and existing.record is not None
        ):
            return ExecuteNonSubmitApplicationResult(
                NonSubmitApplicationExecutionStatus.UNCHANGED,
                existing.record,
                None,
                existing.record.outcome_status,
                False,
                "identical non-submit execution already exists",
            )
        if (
            existing.status
            is NonSubmitApplicationExecutionReadStatus.INTEGRITY_FAILURE
        ):
            return _failed(
                NonSubmitApplicationExecutionFailureReason
                .RECORD_INTEGRITY_FAILURE,
                "existing non-submit execution failed integrity validation",
            )

    try:
        lease_context = browser_lease_provider.lease(owner=bundle.run_id)
        async with lease_context as leased:
            page = getattr(leased, "page", None)
            if page is None and getattr(leased, "session", None) is not None:
                page = leased.session.page
            browser_lease = getattr(leased, "lease", None)
            if page is None or browser_lease is None:
                raise RuntimeError("browser lease did not expose page and lease")
            try:
                outcome = await application_engine.execute(
                    page=page,
                    bundle=bundle,
                    request_submit=False,
                    approve_gate_a=command.approve_gate_a,
                    approved_review_hash="",
                    browser_lease=browser_lease,
                    private_home=private_home,
                    platform_hint=posting.ats_type
                    or posting.source_platform,
                )
            except Exception as exc:
                raise _EngineExecutionError from exc
    except _EngineExecutionError:
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .ENGINE_CONTRACT_FAILURE,
            "Application Engine execution failed",
        )
    except RuntimeError:
        return _deferred(
            NonSubmitApplicationExecutionStatus
            .DEFERRED_BROWSER_UNAVAILABLE,
            "Browser lease was unavailable",
        )
    except Exception:
        return _deferred(
            NonSubmitApplicationExecutionStatus
            .DEFERRED_BROWSER_UNAVAILABLE,
            "Browser lease or non-submit Engine execution was unavailable",
        )
    if not isinstance(outcome, ApplicationOutcome):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .ENGINE_CONTRACT_FAILURE,
            "Application Engine returned an invalid outcome",
        )
    if (
        outcome.run_id != bundle.run_id
        or outcome.job_id != bundle.job.job_id
    ):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .ENGINE_CONTRACT_FAILURE,
            "Application Engine outcome binding is invalid",
        )
    if _submission_boundary_violated(outcome):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .SUBMISSION_BOUNDARY_VIOLATION,
            "Application Engine crossed the non-submit boundary",
        )

    if outcome.status is OutcomeStatus.REVIEW_READY:
        result_status = NonSubmitApplicationExecutionStatus.CREATED
        record_state = NonSubmitExecutionRecordState.REVIEW_READY
    elif _is_runtime_input_required(outcome):
        result_status = (
            NonSubmitApplicationExecutionStatus
            .DEFERRED_RUNTIME_INPUT_REQUIRED
        )
        record_state = NonSubmitExecutionRecordState.RUNTIME_INPUT_REQUIRED
    elif (
        outcome.status is OutcomeStatus.FAILED_RETRYABLE
        and outcome.reason_code is ReasonCode.RETRYABLE_BROWSER_ERROR
    ):
        return _deferred(
            NonSubmitApplicationExecutionStatus
            .DEFERRED_BROWSER_UNAVAILABLE,
            "Application Engine reported a retryable Browser failure",
            outcome_status=outcome.status.value,
        )
    else:
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .ENGINE_CONTRACT_FAILURE,
            "Application Engine did not reach Review or a typed runtime handoff",
            retryable=outcome.retryable,
        )

    try:
        gate_a_reference = application_engine.gate_a_consumption_reference(
            bundle.run_id
        )
    except Exception:
        gate_a_reference = None
    if (
        not isinstance(gate_a_reference, GateAConsumptionReference)
        or gate_a_reference.run_id != bundle.run_id
        or gate_a_reference.job_id != assembly.job_id
    ):
        return _failed(
            NonSubmitApplicationExecutionFailureReason
            .ENGINE_CONTRACT_FAILURE,
            "Engine did not expose verifiable consumed Gate A provenance",
        )

    record = _make_record(
        assembly=assembly,
        gate_a_outcome=gate_a_outcome,
        gate_a_binding_hash=gate_binding,
        gate_a_consumption_reference=gate_a_reference,
        metadata=execution_metadata,
        outcome=outcome,
        execution_state=record_state,
        now=now,
    )
    try:
        write = execution_repository.save(record)
    except Exception:
        return _failed(
            NonSubmitApplicationExecutionFailureReason.PERSISTENCE_FAILED,
            "non-submit execution record could not be persisted",
            retryable=True,
        )
    if (
        write.status is NonSubmitApplicationExecutionWriteStatus.CREATED
        and write.record is not None
    ):
        return ExecuteNonSubmitApplicationResult(
            result_status,
            write.record,
            None,
            outcome.status.value,
            False,
            (
                "non-submit execution reached Review"
                if result_status
                is NonSubmitApplicationExecutionStatus.CREATED
                else "runtime input is required before Review can complete"
            ),
        )
    if (
        write.status is NonSubmitApplicationExecutionWriteStatus.UNCHANGED
        and write.record is not None
    ):
        return ExecuteNonSubmitApplicationResult(
            NonSubmitApplicationExecutionStatus.UNCHANGED,
            write.record,
            None,
            write.record.outcome_status,
            False,
            "identical non-submit execution already exists",
        )
    return _failed(
        write.reason
        or NonSubmitApplicationExecutionFailureReason.PERSISTENCE_FAILED,
        "non-submit execution persistence failed closed",
        retryable=(
            write.reason
            is NonSubmitApplicationExecutionFailureReason.PERSISTENCE_FAILED
        ),
    )


__all__ = [
    "NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION",
    "NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION_V1",
    "NON_SUBMIT_EXECUTION_POLICY_VERSION",
    "BrowserLeaseProvider",
    "ExecuteNonSubmitApplicationCommand",
    "ExecuteNonSubmitApplicationResult",
    "GateAOutcome",
    "NonSubmitApplicationEngine",
    "NonSubmitApplicationExecutionFailureReason",
    "NonSubmitApplicationExecutionReadResult",
    "NonSubmitApplicationExecutionReadStatus",
    "NonSubmitApplicationExecutionRecord",
    "NonSubmitApplicationExecutionRepository",
    "NonSubmitApplicationExecutionStatus",
    "NonSubmitApplicationExecutionWriteResult",
    "NonSubmitApplicationExecutionWriteStatus",
    "NonSubmitExecutionMetadata",
    "NonSubmitExecutionRecordState",
    "PrivateHomeNonSubmitApplicationExecutionRepository",
    "execute_non_submit_application",
]
