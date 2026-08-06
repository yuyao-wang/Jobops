"""Serial orchestration of the public plan-scoped execution stages."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .application_bundle_assembly import (
    ApplicationBundleAssemblyReadStatus,
    ApplicationBundleAssemblyRecord,
    ApplicationBundleAssemblyRepository,
)
from .authorized_submission_execution import (
    AUTHORIZED_SUBMISSION_EXECUTION_CONTRACT_VERSION,
    AUTHORIZED_SUBMISSION_POLICY_VERSION,
    AuthorizedSubmissionExecutionStatus,
    AuthorizedSubmissionOutcome,
    ExecuteAuthorizedSubmissionCommand,
)
from .non_submit_application_execution import (
    NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION,
    NON_SUBMIT_EXECUTION_POLICY_VERSION,
    ExecuteNonSubmitApplicationCommand,
    NonSubmitApplicationExecutionStatus,
    NonSubmitExecutionRecordState,
)
from .private_home import PrivateHome, PrivateHomeError
from .submission_authorization import (
    EXPLICIT_SUBMISSION_AUTHORIZATION_CONTRACT_VERSION,
    GATE_B_SUBMISSION_POLICY_VERSION,
    SUBMISSION_AUTHORIZATION_DECISION_CONTRACT_VERSION,
    DecideSubmissionAuthorizationCommand,
    ExplicitSubmissionAuthorization,
    SubmissionAuthorizationResultStatus,
    SubmissionAuthorizationVerdict,
)
from .submission_permit import (
    SUBMISSION_PERMIT_POLICY_VERSION,
    SUBMISSION_PERMIT_RECORD_CONTRACT_VERSION,
    IssueSubmissionPermitCommand,
    SubmissionPermitStatus,
)


APPLICATION_EXECUTION_ORCHESTRATION_CONTRACT_VERSION = (
    "single-job-automated-application-execution-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ASSEMBLY_ID_RE = re.compile(
    r"^application-bundle-assembly-[a-f0-9]{64}$"
)
_RUN_ID_RE = re.compile(r"^application-execution-run-[a-f0-9]{64}$")


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


def _clean(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the execution-run contract")
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


def _parse_time(name: str, value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} is invalid")
    return _aware(
        name, datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


class ApplicationExecutionStage(StrEnum):
    NON_SUBMIT_EXECUTION = "NON_SUBMIT_EXECUTION"
    GATE_B_AUTHORIZATION = "GATE_B_AUTHORIZATION"
    SUBMISSION_PERMIT_ISSUANCE = "SUBMISSION_PERMIT_ISSUANCE"
    AUTHORIZED_SUBMISSION_EXECUTION = "AUTHORIZED_SUBMISSION_EXECUTION"


APPLICATION_EXECUTION_STAGE_ORDER = tuple(ApplicationExecutionStage)


class ApplicationExecutionStageStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED = "DEFERRED"
    BLOCKED = "BLOCKED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    FAILED = "FAILED"


class ApplicationExecutionRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    DEFERRED = "DEFERRED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    FAILED = "FAILED"


class ApplicationExecutionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    DEFERRED = "DEFERRED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    FAILED = "FAILED"


class ApplicationExecutionFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    ASSEMBLY_NOT_FOUND = "ASSEMBLY_NOT_FOUND"
    ASSEMBLY_INTEGRITY_FAILURE = "ASSEMBLY_INTEGRITY_FAILURE"
    ASSEMBLY_SUBJECT_MISMATCH = "ASSEMBLY_SUBJECT_MISMATCH"
    RUN_INTEGRITY_FAILURE = "RUN_INTEGRITY_FAILURE"
    PUBLIC_STAGE_CONTRACT_FAILURE = "PUBLIC_STAGE_CONTRACT_FAILURE"
    PUBLIC_STAGE_EXCEPTION = "PUBLIC_STAGE_EXCEPTION"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


@dataclass(frozen=True, slots=True)
class ApplicationExecutionOrchestrationMetadata:
    non_submit_contract_version: str
    non_submit_policy_version: str
    gate_b_contract_version: str
    gate_b_policy_version: str
    explicit_authorization_contract_version: str
    permit_record_contract_version: str
    permit_policy_version: str
    authorized_submission_contract_version: str
    authorized_submission_policy_version: str
    metadata_hash: str

    def __post_init__(self) -> None:
        for name, value in self.content_dict().items():
            _clean(name, value)
        if self.metadata_hash != _hash(self.content_dict()):
            raise ValueError("execution orchestration metadata hash is invalid")

    def content_dict(self) -> dict[str, str]:
        return {
            "authorized_submission_contract_version": (
                self.authorized_submission_contract_version
            ),
            "authorized_submission_policy_version": (
                self.authorized_submission_policy_version
            ),
            "explicit_authorization_contract_version": (
                self.explicit_authorization_contract_version
            ),
            "gate_b_contract_version": self.gate_b_contract_version,
            "gate_b_policy_version": self.gate_b_policy_version,
            "non_submit_contract_version": self.non_submit_contract_version,
            "non_submit_policy_version": self.non_submit_policy_version,
            "permit_policy_version": self.permit_policy_version,
            "permit_record_contract_version": (
                self.permit_record_contract_version
            ),
        }

    @classmethod
    def current(cls) -> "ApplicationExecutionOrchestrationMetadata":
        content = {
            "authorized_submission_contract_version": (
                AUTHORIZED_SUBMISSION_EXECUTION_CONTRACT_VERSION
            ),
            "authorized_submission_policy_version": (
                AUTHORIZED_SUBMISSION_POLICY_VERSION
            ),
            "explicit_authorization_contract_version": (
                EXPLICIT_SUBMISSION_AUTHORIZATION_CONTRACT_VERSION
            ),
            "gate_b_contract_version": (
                SUBMISSION_AUTHORIZATION_DECISION_CONTRACT_VERSION
            ),
            "gate_b_policy_version": GATE_B_SUBMISSION_POLICY_VERSION,
            "non_submit_contract_version": (
                NON_SUBMIT_APPLICATION_EXECUTION_CONTRACT_VERSION
            ),
            "non_submit_policy_version": (
                NON_SUBMIT_EXECUTION_POLICY_VERSION
            ),
            "permit_policy_version": SUBMISSION_PERMIT_POLICY_VERSION,
            "permit_record_contract_version": (
                SUBMISSION_PERMIT_RECORD_CONTRACT_VERSION
            ),
        }
        return cls(**content, metadata_hash=_hash(content))


@dataclass(frozen=True, slots=True)
class ApplicationExecutionStageResult:
    stage: ApplicationExecutionStage
    status: ApplicationExecutionStageStatus
    public_status: str
    record_id: str | None
    record_hash: str | None
    reason_code: str | None
    stage_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", ApplicationExecutionStage(self.stage))
        object.__setattr__(
            self, "status", ApplicationExecutionStageStatus(self.status)
        )
        _clean("public_status", self.public_status, 120)
        if self.status in {
            ApplicationExecutionStageStatus.CREATED,
            ApplicationExecutionStageStatus.UNCHANGED,
            ApplicationExecutionStageStatus.SUBMISSION_UNCERTAIN,
        }:
            _clean("record_id", self.record_id)
            _require_hash("record_hash", self.record_hash)
        elif self.record_id is not None or self.record_hash is not None:
            raise ValueError("stopped stage cannot expose a success record")
        if self.status in {
            ApplicationExecutionStageStatus.DEFERRED,
            ApplicationExecutionStageStatus.BLOCKED,
            ApplicationExecutionStageStatus.FAILED,
        }:
            _clean("reason_code", self.reason_code, 200)
        elif self.reason_code is not None:
            raise ValueError("successful stage cannot have a reason")
        if self.stage_hash != _hash(self.content_dict()):
            raise ValueError("execution stage hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "public_status": self.public_status,
            "reason_code": self.reason_code,
            "record_hash": self.record_hash,
            "record_id": self.record_id,
            "stage": self.stage.value,
            "status": self.status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_dict(), "stage_hash": self.stage_hash}

    @classmethod
    def create(
        cls,
        *,
        stage: ApplicationExecutionStage,
        status: ApplicationExecutionStageStatus,
        public_status: str,
        record_id: str | None = None,
        record_hash: str | None = None,
        reason_code: str | None = None,
    ) -> "ApplicationExecutionStageResult":
        content = {
            "public_status": public_status,
            "reason_code": reason_code,
            "record_hash": record_hash,
            "record_id": record_id,
            "stage": stage.value,
            "status": status.value,
        }
        return cls(**content, stage_hash=_hash(content))


@dataclass(frozen=True, slots=True)
class ApplicationExecutionRun:
    run_id: str
    contract_version: str
    execution_binding_hash: str
    orchestration_metadata: ApplicationExecutionOrchestrationMetadata
    subject_id: str
    application_plan_id: str
    job_id: str
    assembly_record_id: str
    assembly_record_hash: str
    gate_a_approved: bool
    explicit_user_authorization_id: str | None
    explicit_user_authorization_hash: str | None
    stage_results: tuple[ApplicationExecutionStageResult, ...]
    non_submit_execution_record_id: str | None
    submission_authorization_decision_id: str | None
    submission_permit_record_id: str | None
    authorized_submission_execution_record_id: str | None
    overall_status: ApplicationExecutionRunStatus
    deferred_stage: ApplicationExecutionStage | None
    deferred_reason: str | None
    failed_stage: ApplicationExecutionStage | None
    failed_reason: str | None
    run_hash: str
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != APPLICATION_EXECUTION_ORCHESTRATION_CONTRACT_VERSION
        ):
            raise ValueError("execution orchestration version is unsupported")
        _require_hash("execution_binding_hash", self.execution_binding_hash)
        _require_hash("assembly_record_hash", self.assembly_record_hash)
        if not isinstance(
            self.orchestration_metadata,
            ApplicationExecutionOrchestrationMetadata,
        ):
            raise TypeError("orchestration metadata must be typed")
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "assembly_record_id",
        ):
            _clean(name, getattr(self, name))
        if type(self.gate_a_approved) is not bool:
            raise TypeError("gate_a_approved must be boolean")
        if (self.explicit_user_authorization_id is None) != (
            self.explicit_user_authorization_hash is None
        ):
            raise ValueError("explicit authorization binding is incomplete")
        if self.explicit_user_authorization_id is not None:
            _clean(
                "explicit_user_authorization_id",
                self.explicit_user_authorization_id,
            )
            _require_hash(
                "explicit_user_authorization_hash",
                self.explicit_user_authorization_hash,
            )
        if not self.stage_results or tuple(
            item.stage for item in self.stage_results
        ) != APPLICATION_EXECUTION_STAGE_ORDER[: len(self.stage_results)]:
            raise ValueError("execution stage lineage must be an ordered prefix")
        outputs = {
            ApplicationExecutionStage.NON_SUBMIT_EXECUTION: (
                "non_submit_execution_record_id"
            ),
            ApplicationExecutionStage.GATE_B_AUTHORIZATION: (
                "submission_authorization_decision_id"
            ),
            ApplicationExecutionStage.SUBMISSION_PERMIT_ISSUANCE: (
                "submission_permit_record_id"
            ),
            ApplicationExecutionStage.AUTHORIZED_SUBMISSION_EXECUTION: (
                "authorized_submission_execution_record_id"
            ),
        }
        for stage, field_name in outputs.items():
            result = next(
                (item for item in self.stage_results if item.stage is stage),
                None,
            )
            expected = (
                result.record_id
                if result is not None
                and result.status
                in {
                    ApplicationExecutionStageStatus.CREATED,
                    ApplicationExecutionStageStatus.UNCHANGED,
                    ApplicationExecutionStageStatus.SUBMISSION_UNCERTAIN,
                }
                else None
            )
            if getattr(self, field_name) != expected:
                raise ValueError("execution output conflicts with lineage")
        object.__setattr__(
            self,
            "overall_status",
            ApplicationExecutionRunStatus(self.overall_status),
        )
        if self.overall_status is ApplicationExecutionRunStatus.COMPLETED:
            if (
                len(self.stage_results) != len(APPLICATION_EXECUTION_STAGE_ORDER)
                or self.stage_results[-1].status
                not in {
                    ApplicationExecutionStageStatus.CREATED,
                    ApplicationExecutionStageStatus.UNCHANGED,
                }
                or self.deferred_stage is not None
                or self.failed_stage is not None
            ):
                raise ValueError("completed execution run is malformed")
        elif (
            self.overall_status
            is ApplicationExecutionRunStatus.SUBMISSION_UNCERTAIN
        ):
            if (
                len(self.stage_results) != len(APPLICATION_EXECUTION_STAGE_ORDER)
                or self.stage_results[-1].status
                is not ApplicationExecutionStageStatus.SUBMISSION_UNCERTAIN
                or self.deferred_stage is not None
                or self.failed_stage is not None
            ):
                raise ValueError("uncertain execution run is malformed")
        elif self.overall_status is ApplicationExecutionRunStatus.DEFERRED:
            if (
                self.deferred_stage is None
                or self.deferred_reason is None
                or self.failed_stage is not None
                or self.failed_reason is not None
                or self.stage_results[-1].stage is not self.deferred_stage
                or self.stage_results[-1].status
                is not ApplicationExecutionStageStatus.DEFERRED
            ):
                raise ValueError("deferred execution run is malformed")
        elif (
            self.failed_stage is None
            or self.failed_reason is None
            or self.deferred_stage is not None
            or self.deferred_reason is not None
            or self.stage_results[-1].stage is not self.failed_stage
            or self.stage_results[-1].status
            not in {
                ApplicationExecutionStageStatus.BLOCKED,
                ApplicationExecutionStageStatus.FAILED,
            }
        ):
            raise ValueError("failed execution run is malformed")
        started = _aware("started_at", self.started_at)
        completed = _aware("completed_at", self.completed_at)
        if completed < started:
            raise ValueError("completed_at precedes started_at")
        expected_id = "application-execution-run-" + _hash(
            self.identity_dict()
        )
        if (
            _RUN_ID_RE.fullmatch(self.run_id) is None
            or self.run_id != expected_id
        ):
            raise ValueError("application execution run identity is invalid")
        if self.run_hash != _hash(self.content_dict()):
            raise ValueError("application execution run hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "assembly_record_hash": self.assembly_record_hash,
            "assembly_record_id": self.assembly_record_id,
            "contract_version": self.contract_version,
            "deferred_reason": self.deferred_reason,
            "deferred_stage": (
                self.deferred_stage.value if self.deferred_stage else None
            ),
            "execution_binding_hash": self.execution_binding_hash,
            "explicit_user_authorization_hash": (
                self.explicit_user_authorization_hash
            ),
            "explicit_user_authorization_id": (
                self.explicit_user_authorization_id
            ),
            "failed_reason": self.failed_reason,
            "failed_stage": (
                self.failed_stage.value if self.failed_stage else None
            ),
            "gate_a_approved": self.gate_a_approved,
            "job_id": self.job_id,
            "orchestration_metadata_hash": (
                self.orchestration_metadata.metadata_hash
            ),
            "overall_status": self.overall_status.value,
            "stage_hashes": [item.stage_hash for item in self.stage_results],
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "authorized_submission_execution_record_id": (
                self.authorized_submission_execution_record_id
            ),
            "completed_at": _rfc3339(self.completed_at),
            "non_submit_execution_record_id": (
                self.non_submit_execution_record_id
            ),
            "orchestration_metadata": (
                self.orchestration_metadata.content_dict()
            ),
            "run_id": self.run_id,
            "stage_results": [item.to_dict() for item in self.stage_results],
            "started_at": _rfc3339(self.started_at),
            "submission_authorization_decision_id": (
                self.submission_authorization_decision_id
            ),
            "submission_permit_record_id": (
                self.submission_permit_record_id
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_dict(), "run_hash": self.run_hash}


class ApplicationExecutionRunReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ApplicationExecutionRunWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ApplicationExecutionRunListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class ApplicationExecutionRunReadResult:
    status: ApplicationExecutionRunReadStatus
    run: ApplicationExecutionRun | None


@dataclass(frozen=True, slots=True)
class ApplicationExecutionRunWriteResult:
    status: ApplicationExecutionRunWriteStatus
    run: ApplicationExecutionRun | None
    reason: ApplicationExecutionFailureReason | None


@dataclass(frozen=True, slots=True)
class ApplicationExecutionRunListResult:
    status: ApplicationExecutionRunListStatus
    runs: tuple[ApplicationExecutionRun, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", ApplicationExecutionRunListStatus(self.status)
        )
        if not isinstance(self.runs, tuple) or any(
            not isinstance(item, ApplicationExecutionRun)
            for item in self.runs
        ):
            raise TypeError("listed execution runs must be typed")
        if self.status is ApplicationExecutionRunListStatus.INTEGRITY_FAILURE:
            if self.runs:
                raise ValueError("failed execution-run list cannot expose runs")
            return
        expected = tuple(
            sorted(
                self.runs,
                key=lambda item: (
                    item.application_plan_id,
                    item.assembly_record_id,
                    item.completed_at.astimezone(timezone.utc),
                    item.run_id,
                ),
            )
        )
        if self.runs != expected or len(
            {item.run_id for item in self.runs}
        ) != len(self.runs):
            raise ValueError("execution-run list ordering is invalid")
        if self.runs and len(
            {item.subject_id for item in self.runs}
        ) != 1:
            raise ValueError("execution-run list mixes subjects")


@runtime_checkable
class ApplicationExecutionRunRepository(Protocol):
    def get(
        self, *, subject_id: str, run_id: str
    ) -> ApplicationExecutionRunReadResult: ...

    def save(
        self, run: ApplicationExecutionRun
    ) -> ApplicationExecutionRunWriteResult: ...

    def find_current_for_assembly(
        self, *, subject_id: str, assembly_record_id: str
    ) -> ApplicationExecutionRunReadResult: ...

    def list_for_subject(
        self, *, subject_id: str
    ) -> ApplicationExecutionRunListResult: ...


def _stage_from_dict(value: Mapping[str, Any]) -> ApplicationExecutionStageResult:
    return ApplicationExecutionStageResult(
        stage=ApplicationExecutionStage(value["stage"]),
        status=ApplicationExecutionStageStatus(value["status"]),
        public_status=value["public_status"],
        record_id=value["record_id"],
        record_hash=value["record_hash"],
        reason_code=value["reason_code"],
        stage_hash=value["stage_hash"],
    )


def _run_from_dict(value: Mapping[str, Any]) -> ApplicationExecutionRun:
    expected = {
        "application_plan_id",
        "assembly_record_hash",
        "assembly_record_id",
        "authorized_submission_execution_record_id",
        "completed_at",
        "contract_version",
        "deferred_reason",
        "deferred_stage",
        "execution_binding_hash",
        "explicit_user_authorization_hash",
        "explicit_user_authorization_id",
        "failed_reason",
        "failed_stage",
        "gate_a_approved",
        "job_id",
        "non_submit_execution_record_id",
        "orchestration_metadata",
        "orchestration_metadata_hash",
        "overall_status",
        "run_hash",
        "run_id",
        "stage_hashes",
        "stage_results",
        "started_at",
        "subject_id",
        "submission_authorization_decision_id",
        "submission_permit_record_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted application execution run is malformed")
    metadata_value = value["orchestration_metadata"]
    if not isinstance(metadata_value, Mapping):
        raise ValueError("persisted orchestration metadata is malformed")
    metadata = ApplicationExecutionOrchestrationMetadata(
        **dict(metadata_value),
        metadata_hash=value["orchestration_metadata_hash"],
    )
    stages = tuple(
        _stage_from_dict(item) for item in value["stage_results"]
    )
    if value["stage_hashes"] != [item.stage_hash for item in stages]:
        raise ValueError("persisted execution stage hashes are inconsistent")
    return ApplicationExecutionRun(
        run_id=value["run_id"],
        contract_version=value["contract_version"],
        execution_binding_hash=value["execution_binding_hash"],
        orchestration_metadata=metadata,
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        assembly_record_id=value["assembly_record_id"],
        assembly_record_hash=value["assembly_record_hash"],
        gate_a_approved=value["gate_a_approved"],
        explicit_user_authorization_id=value[
            "explicit_user_authorization_id"
        ],
        explicit_user_authorization_hash=value[
            "explicit_user_authorization_hash"
        ],
        stage_results=stages,
        non_submit_execution_record_id=value[
            "non_submit_execution_record_id"
        ],
        submission_authorization_decision_id=value[
            "submission_authorization_decision_id"
        ],
        submission_permit_record_id=value["submission_permit_record_id"],
        authorized_submission_execution_record_id=value[
            "authorized_submission_execution_record_id"
        ],
        overall_status=ApplicationExecutionRunStatus(
            value["overall_status"]
        ),
        deferred_stage=(
            ApplicationExecutionStage(value["deferred_stage"])
            if value["deferred_stage"]
            else None
        ),
        deferred_reason=value["deferred_reason"],
        failed_stage=(
            ApplicationExecutionStage(value["failed_stage"])
            if value["failed_stage"]
            else None
        ),
        failed_reason=value["failed_reason"],
        run_hash=value["run_hash"],
        started_at=_parse_time("started_at", value["started_at"]),
        completed_at=_parse_time("completed_at", value["completed_at"]),
    )


class PrivateHomeApplicationExecutionRunRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.application_execution_runs
            / _subject_key(_clean("subject_id", subject_id, 160))
        )

    def _path(self, subject_id: str, run_id: str) -> Path:
        if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("run_id is invalid")
        return self._directory(subject_id) / f"{run_id}.json"

    def get(
        self, *, subject_id: str, run_id: str
    ) -> ApplicationExecutionRunReadResult:
        path = self._path(subject_id, run_id)
        with self._lock:
            if not path.exists():
                return ApplicationExecutionRunReadResult(
                    ApplicationExecutionRunReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return ApplicationExecutionRunReadResult(
                    ApplicationExecutionRunReadStatus.INTEGRITY_FAILURE, None
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
                return ApplicationExecutionRunReadResult(
                    ApplicationExecutionRunReadStatus.INTEGRITY_FAILURE, None
                )
            if run.subject_id != subject_id.strip() or run.run_id != run_id:
                return ApplicationExecutionRunReadResult(
                    ApplicationExecutionRunReadStatus.INTEGRITY_FAILURE, None
                )
            return ApplicationExecutionRunReadResult(
                ApplicationExecutionRunReadStatus.FOUND, run
            )

    def save(
        self, run: ApplicationExecutionRun
    ) -> ApplicationExecutionRunWriteResult:
        if not isinstance(run, ApplicationExecutionRun):
            raise TypeError("run must be typed")
        path = self._path(run.subject_id, run.run_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            run.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ApplicationExecutionRunWriteResult(
                    ApplicationExecutionRunWriteStatus.FAILED,
                    None,
                    ApplicationExecutionFailureReason.PERSISTENCE_FAILURE,
                )
            if created:
                return ApplicationExecutionRunWriteResult(
                    ApplicationExecutionRunWriteStatus.CREATED, run, None
                )
            existing = self.get(
                subject_id=run.subject_id, run_id=run.run_id
            )
            if (
                existing.status is ApplicationExecutionRunReadStatus.FOUND
                and existing.run is not None
                and existing.run.identity_dict() == run.identity_dict()
            ):
                return ApplicationExecutionRunWriteResult(
                    ApplicationExecutionRunWriteStatus.UNCHANGED,
                    existing.run,
                    None,
                )
            return ApplicationExecutionRunWriteResult(
                ApplicationExecutionRunWriteStatus.FAILED,
                None,
                ApplicationExecutionFailureReason.RUN_INTEGRITY_FAILURE,
            )

    def find_current_for_assembly(
        self, *, subject_id: str, assembly_record_id: str
    ) -> ApplicationExecutionRunReadResult:
        listed = self.list_for_subject(subject_id=subject_id)
        if listed.status is ApplicationExecutionRunListStatus.INTEGRITY_FAILURE:
            return ApplicationExecutionRunReadResult(
                ApplicationExecutionRunReadStatus.INTEGRITY_FAILURE, None
            )
        runs = [
            item
            for item in listed.runs
            if item.assembly_record_id == assembly_record_id
        ]
        if not runs:
            return ApplicationExecutionRunReadResult(
                ApplicationExecutionRunReadStatus.NOT_FOUND, None
            )
        current = max(
            runs,
            key=lambda item: (
                item.completed_at.astimezone(timezone.utc),
                item.run_id,
            ),
        )
        return ApplicationExecutionRunReadResult(
            ApplicationExecutionRunReadStatus.FOUND, current
        )

    def list_for_subject(
        self, *, subject_id: str
    ) -> ApplicationExecutionRunListResult:
        directory = self._directory(subject_id)
        if not directory.exists():
            return ApplicationExecutionRunListResult(
                ApplicationExecutionRunListStatus.SUCCEEDED, ()
            )
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            return ApplicationExecutionRunListResult(
                ApplicationExecutionRunListStatus.INTEGRITY_FAILURE, ()
            )
        runs: list[ApplicationExecutionRun] = []
        for path in paths:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or _RUN_ID_RE.fullmatch(path.stem) is None
            ):
                return ApplicationExecutionRunListResult(
                    ApplicationExecutionRunListStatus.INTEGRITY_FAILURE, ()
                )
            read = self.get(subject_id=subject_id, run_id=path.stem)
            if (
                read.status is not ApplicationExecutionRunReadStatus.FOUND
                or read.run is None
            ):
                return ApplicationExecutionRunListResult(
                    ApplicationExecutionRunListStatus.INTEGRITY_FAILURE, ()
                )
            runs.append(read.run)
        return ApplicationExecutionRunListResult(
            ApplicationExecutionRunListStatus.SUCCEEDED,
            tuple(
                sorted(
                    runs,
                    key=lambda item: (
                        item.application_plan_id,
                        item.assembly_record_id,
                        item.completed_at.astimezone(timezone.utc),
                        item.run_id,
                    ),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class RunApplicationExecutionCommand:
    subject_id: str
    application_bundle_assembly_record_id: str
    now: datetime
    approve_gate_a: bool = False
    explicit_user_authorization: ExplicitSubmissionAuthorization | None = None


@dataclass(frozen=True, slots=True)
class RunApplicationExecutionResult:
    status: ApplicationExecutionStatus
    run: ApplicationExecutionRun | None
    reason: ApplicationExecutionFailureReason | None
    message: str


async def _invoke(callable_: Callable[[Any], Any], command: Any) -> Any:
    result = callable_(command)
    if inspect.isawaitable(result):
        return await result
    return result


def _public_reason(result: Any, fallback: str) -> str:
    reason = getattr(result, "failure_reason", None)
    if reason is None:
        return fallback
    return str(getattr(reason, "value", reason))


def _binding(
    assembly: ApplicationBundleAssemblyRecord,
    *,
    approve_gate_a: bool,
    explicit: ExplicitSubmissionAuthorization | None,
    metadata: ApplicationExecutionOrchestrationMetadata,
) -> str:
    return _hash(
        {
            "assembly_record_hash": assembly.record_content_hash,
            "assembly_record_id": assembly.record_id,
            "contract_version": (
                APPLICATION_EXECUTION_ORCHESTRATION_CONTRACT_VERSION
            ),
            "explicit_user_authorization_hash": (
                explicit.authorization_content_hash if explicit else None
            ),
            "explicit_user_authorization_id": (
                explicit.authorization_id if explicit else None
            ),
            "gate_a_approved": approve_gate_a,
            "orchestration_metadata_hash": metadata.metadata_hash,
            "subject_id": assembly.subject_id,
        }
    )


def _build_run(
    *,
    assembly: ApplicationBundleAssemblyRecord,
    binding: str,
    metadata: ApplicationExecutionOrchestrationMetadata,
    command: RunApplicationExecutionCommand,
    stages: tuple[ApplicationExecutionStageResult, ...],
    overall_status: ApplicationExecutionRunStatus,
    now: datetime,
) -> ApplicationExecutionRun:
    explicit = command.explicit_user_authorization
    deferred = (
        stages[-1]
        if overall_status is ApplicationExecutionRunStatus.DEFERRED
        else None
    )
    failed = (
        stages[-1]
        if overall_status is ApplicationExecutionRunStatus.FAILED
        else None
    )
    by_stage = {item.stage: item for item in stages}
    identity = {
        "application_plan_id": assembly.application_plan_id,
        "assembly_record_hash": assembly.record_content_hash,
        "assembly_record_id": assembly.record_id,
        "contract_version": APPLICATION_EXECUTION_ORCHESTRATION_CONTRACT_VERSION,
        "deferred_reason": deferred.reason_code if deferred else None,
        "deferred_stage": deferred.stage.value if deferred else None,
        "execution_binding_hash": binding,
        "explicit_user_authorization_hash": (
            explicit.authorization_content_hash if explicit else None
        ),
        "explicit_user_authorization_id": (
            explicit.authorization_id if explicit else None
        ),
        "failed_reason": failed.reason_code if failed else None,
        "failed_stage": failed.stage.value if failed else None,
        "gate_a_approved": command.approve_gate_a,
        "job_id": assembly.job_id,
        "orchestration_metadata_hash": metadata.metadata_hash,
        "overall_status": overall_status.value,
        "stage_hashes": [item.stage_hash for item in stages],
        "subject_id": assembly.subject_id,
    }
    run_id = "application-execution-run-" + _hash(identity)

    def record_id(stage: ApplicationExecutionStage) -> str | None:
        item = by_stage.get(stage)
        return item.record_id if item is not None else None

    content = {
        **identity,
        "authorized_submission_execution_record_id": record_id(
            ApplicationExecutionStage.AUTHORIZED_SUBMISSION_EXECUTION
        ),
        "completed_at": _rfc3339(now),
        "non_submit_execution_record_id": record_id(
            ApplicationExecutionStage.NON_SUBMIT_EXECUTION
        ),
        "orchestration_metadata": metadata.content_dict(),
        "run_id": run_id,
        "stage_results": [item.to_dict() for item in stages],
        "started_at": _rfc3339(now),
        "submission_authorization_decision_id": record_id(
            ApplicationExecutionStage.GATE_B_AUTHORIZATION
        ),
        "submission_permit_record_id": record_id(
            ApplicationExecutionStage.SUBMISSION_PERMIT_ISSUANCE
        ),
    }
    return ApplicationExecutionRun(
        run_id=run_id,
        contract_version=APPLICATION_EXECUTION_ORCHESTRATION_CONTRACT_VERSION,
        execution_binding_hash=binding,
        orchestration_metadata=metadata,
        subject_id=assembly.subject_id,
        application_plan_id=assembly.application_plan_id,
        job_id=assembly.job_id,
        assembly_record_id=assembly.record_id,
        assembly_record_hash=assembly.record_content_hash,
        gate_a_approved=command.approve_gate_a,
        explicit_user_authorization_id=(
            explicit.authorization_id if explicit else None
        ),
        explicit_user_authorization_hash=(
            explicit.authorization_content_hash if explicit else None
        ),
        stage_results=stages,
        non_submit_execution_record_id=content[
            "non_submit_execution_record_id"
        ],
        submission_authorization_decision_id=content[
            "submission_authorization_decision_id"
        ],
        submission_permit_record_id=content[
            "submission_permit_record_id"
        ],
        authorized_submission_execution_record_id=content[
            "authorized_submission_execution_record_id"
        ],
        overall_status=overall_status,
        deferred_stage=deferred.stage if deferred else None,
        deferred_reason=deferred.reason_code if deferred else None,
        failed_stage=failed.stage if failed else None,
        failed_reason=failed.reason_code if failed else None,
        run_hash=_hash(content),
        started_at=now,
        completed_at=now,
    )


def _result(
    status: ApplicationExecutionStatus,
    message: str,
    *,
    run: ApplicationExecutionRun | None = None,
    reason: ApplicationExecutionFailureReason | None = None,
) -> RunApplicationExecutionResult:
    return RunApplicationExecutionResult(status, run, reason, message)


def _persist(
    run: ApplicationExecutionRun,
    repository: ApplicationExecutionRunRepository,
) -> RunApplicationExecutionResult:
    try:
        write = repository.save(run)
    except Exception:
        return _result(
            ApplicationExecutionStatus.FAILED,
            "application execution run persistence failed",
            reason=ApplicationExecutionFailureReason.PERSISTENCE_FAILURE,
        )
    if (
        write.status is ApplicationExecutionRunWriteStatus.FAILED
        or write.run is None
    ):
        return _result(
            ApplicationExecutionStatus.FAILED,
            "application execution run persistence failed closed",
            reason=write.reason
            or ApplicationExecutionFailureReason.PERSISTENCE_FAILURE,
        )
    return _result(
        ApplicationExecutionStatus(run.overall_status.value),
        f"application execution is {run.overall_status.value}",
        run=write.run,
    )


async def run_application_execution(
    command: RunApplicationExecutionCommand,
    *,
    assembly_repository: ApplicationBundleAssemblyRepository,
    non_submit_execution: Callable[[ExecuteNonSubmitApplicationCommand], Any],
    gate_b_authorization: Callable[
        [DecideSubmissionAuthorizationCommand], Any
    ],
    submission_permit_issuance: Callable[
        [IssueSubmissionPermitCommand], Any
    ],
    authorized_submission_execution: Callable[
        [ExecuteAuthorizedSubmissionCommand], Any
    ],
    run_repository: ApplicationExecutionRunRepository,
    orchestration_metadata: (
        ApplicationExecutionOrchestrationMetadata | None
    ) = None,
) -> RunApplicationExecutionResult:
    """Run P2c3 → P2c4 → P2c5b → P2c6 once, strictly serially."""

    try:
        subject = _clean("subject_id", command.subject_id, 160)
        assembly_id = _clean(
            "application_bundle_assembly_record_id",
            command.application_bundle_assembly_record_id,
        )
        if _ASSEMBLY_ID_RE.fullmatch(assembly_id) is None:
            raise ValueError("assembly ID is invalid")
        now = _aware("now", command.now)
        if type(command.approve_gate_a) is not bool:
            raise TypeError("approve_gate_a must be boolean")
        explicit = command.explicit_user_authorization
        if explicit is not None and not isinstance(
            explicit, ExplicitSubmissionAuthorization
        ):
            raise TypeError("explicit user authorization must be typed")
        metadata = (
            orchestration_metadata
            or ApplicationExecutionOrchestrationMetadata.current()
        )
        if not isinstance(
            metadata, ApplicationExecutionOrchestrationMetadata
        ):
            raise TypeError("orchestration metadata must be typed")
    except (AttributeError, TypeError, ValueError):
        return _result(
            ApplicationExecutionStatus.FAILED,
            "application execution command is invalid",
            reason=ApplicationExecutionFailureReason.INVALID_REQUEST,
        )
    try:
        assembly_read = assembly_repository.get(
            subject_id=subject, record_id=assembly_id
        )
    except Exception:
        return _result(
            ApplicationExecutionStatus.FAILED,
            "ApplicationBundle assembly could not be read safely",
            reason=(
                ApplicationExecutionFailureReason
                .ASSEMBLY_INTEGRITY_FAILURE
            ),
        )
    if assembly_read.status is ApplicationBundleAssemblyReadStatus.NOT_FOUND:
        return _result(
            ApplicationExecutionStatus.FAILED,
            "ApplicationBundle assembly was not found",
            reason=ApplicationExecutionFailureReason.ASSEMBLY_NOT_FOUND,
        )
    if (
        assembly_read.status
        is not ApplicationBundleAssemblyReadStatus.FOUND
        or not isinstance(
            assembly_read.record, ApplicationBundleAssemblyRecord
        )
    ):
        return _result(
            ApplicationExecutionStatus.FAILED,
            "ApplicationBundle assembly failed integrity validation",
            reason=(
                ApplicationExecutionFailureReason
                .ASSEMBLY_INTEGRITY_FAILURE
            ),
        )
    assembly = assembly_read.record
    if assembly.subject_id != subject:
        return _result(
            ApplicationExecutionStatus.FAILED,
            "ApplicationBundle assembly belongs to another subject",
            reason=(
                ApplicationExecutionFailureReason.ASSEMBLY_SUBJECT_MISMATCH
            ),
        )
    binding = _binding(
        assembly,
        approve_gate_a=command.approve_gate_a,
        explicit=explicit,
        metadata=metadata,
    )
    try:
        current = run_repository.find_current_for_assembly(
            subject_id=subject, assembly_record_id=assembly.record_id
        )
    except Exception:
        return _result(
            ApplicationExecutionStatus.FAILED,
            "current execution run could not be read safely",
            reason=ApplicationExecutionFailureReason.RUN_INTEGRITY_FAILURE,
        )
    if current.status is ApplicationExecutionRunReadStatus.INTEGRITY_FAILURE:
        return _result(
            ApplicationExecutionStatus.FAILED,
            "current execution run failed integrity validation",
            reason=ApplicationExecutionFailureReason.RUN_INTEGRITY_FAILURE,
        )
    if (
        current.status is ApplicationExecutionRunReadStatus.FOUND
        and current.run is not None
        and current.run.execution_binding_hash == binding
        and current.run.overall_status
        in {
            ApplicationExecutionRunStatus.COMPLETED,
            ApplicationExecutionRunStatus.SUBMISSION_UNCERTAIN,
        }
    ):
        return _result(
            ApplicationExecutionStatus.UNCHANGED,
            "terminal application execution is unchanged",
            run=current.run,
        )
    if (
        current.status is ApplicationExecutionRunReadStatus.FOUND
        and current.run is not None
        and current.run.execution_binding_hash == binding
        and current.run.overall_status
        is ApplicationExecutionRunStatus.DEFERRED
    ):
        return _result(
            ApplicationExecutionStatus.DEFERRED,
            "identical deferred application execution is unchanged",
            run=current.run,
        )

    stages: list[ApplicationExecutionStageResult] = []

    def stop(
        stage: ApplicationExecutionStage,
        *,
        stage_status: ApplicationExecutionStageStatus,
        public_status: str,
        reason_code: str,
        overall: ApplicationExecutionRunStatus,
    ) -> RunApplicationExecutionResult:
        stages.append(
            ApplicationExecutionStageResult.create(
                stage=stage,
                status=stage_status,
                public_status=public_status,
                reason_code=reason_code,
            )
        )
        return _persist(
            _build_run(
                assembly=assembly,
                binding=binding,
                metadata=metadata,
                command=command,
                stages=tuple(stages),
                overall_status=overall,
                now=now,
            ),
            run_repository,
        )

    async def invoke(stage: ApplicationExecutionStage, callable_: Any, value: Any):
        try:
            return await _invoke(callable_, value)
        except Exception:
            return None

    p2c3 = await invoke(
        ApplicationExecutionStage.NON_SUBMIT_EXECUTION,
        non_submit_execution,
        ExecuteNonSubmitApplicationCommand(
            subject_id=subject,
            application_bundle_assembly_record_id=assembly.record_id,
            now=now,
            approve_gate_a=command.approve_gate_a,
        ),
    )
    if p2c3 is None:
        return stop(
            ApplicationExecutionStage.NON_SUBMIT_EXECUTION,
            stage_status=ApplicationExecutionStageStatus.FAILED,
            public_status="PUBLIC_STAGE_EXCEPTION",
            reason_code=ApplicationExecutionFailureReason.PUBLIC_STAGE_EXCEPTION,
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    p2c3_status = getattr(p2c3, "status", None)
    p2c3_record = getattr(p2c3, "record", None)
    if p2c3_status in {
        NonSubmitApplicationExecutionStatus.CREATED,
        NonSubmitApplicationExecutionStatus.UNCHANGED,
    } and (
        p2c3_record is None
        or getattr(p2c3_record, "execution_state", None)
        is not NonSubmitExecutionRecordState.REVIEW_READY
    ):
        return stop(
            ApplicationExecutionStage.NON_SUBMIT_EXECUTION,
            stage_status=ApplicationExecutionStageStatus.DEFERRED,
            public_status=str(getattr(p2c3_status, "value", p2c3_status)),
            reason_code="RUNTIME_INPUT_REQUIRED",
            overall=ApplicationExecutionRunStatus.DEFERRED,
        )
    if p2c3_status not in {
        NonSubmitApplicationExecutionStatus.CREATED,
        NonSubmitApplicationExecutionStatus.UNCHANGED,
    }:
        if p2c3_status in {
            NonSubmitApplicationExecutionStatus.DEFERRED_GATE_A_REQUIRED,
            NonSubmitApplicationExecutionStatus.DEFERRED_BROWSER_UNAVAILABLE,
            NonSubmitApplicationExecutionStatus
            .DEFERRED_RUNTIME_INPUT_REQUIRED,
        }:
            return stop(
                ApplicationExecutionStage.NON_SUBMIT_EXECUTION,
                stage_status=ApplicationExecutionStageStatus.DEFERRED,
                public_status=p2c3_status.value,
                reason_code=_public_reason(p2c3, p2c3_status.value),
                overall=ApplicationExecutionRunStatus.DEFERRED,
            )
        return stop(
            ApplicationExecutionStage.NON_SUBMIT_EXECUTION,
            stage_status=ApplicationExecutionStageStatus.FAILED,
            public_status=str(getattr(p2c3_status, "value", p2c3_status)),
            reason_code=_public_reason(
                p2c3, ApplicationExecutionFailureReason.PUBLIC_STAGE_CONTRACT_FAILURE
            ),
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    try:
        c3_id = _clean("non-submit execution record ID", p2c3_record.record_id)
        c3_hash = _require_hash(
            "non-submit execution record hash",
            p2c3_record.record_content_hash,
        )
    except (AttributeError, TypeError, ValueError):
        return stop(
            ApplicationExecutionStage.NON_SUBMIT_EXECUTION,
            stage_status=ApplicationExecutionStageStatus.FAILED,
            public_status="PUBLIC_STAGE_CONTRACT_FAILURE",
            reason_code=ApplicationExecutionFailureReason.PUBLIC_STAGE_CONTRACT_FAILURE,
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    stages.append(
        ApplicationExecutionStageResult.create(
            stage=ApplicationExecutionStage.NON_SUBMIT_EXECUTION,
            status=ApplicationExecutionStageStatus(p2c3_status.value),
            public_status=p2c3_status.value,
            record_id=c3_id,
            record_hash=c3_hash,
        )
    )

    p2c4 = await invoke(
        ApplicationExecutionStage.GATE_B_AUTHORIZATION,
        gate_b_authorization,
        DecideSubmissionAuthorizationCommand(
            subject_id=subject,
            non_submit_execution_record_id=c3_id,
            now=now,
            explicit_user_authorization=explicit,
        ),
    )
    if p2c4 is None:
        return stop(
            ApplicationExecutionStage.GATE_B_AUTHORIZATION,
            stage_status=ApplicationExecutionStageStatus.FAILED,
            public_status="PUBLIC_STAGE_EXCEPTION",
            reason_code=ApplicationExecutionFailureReason.PUBLIC_STAGE_EXCEPTION,
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    p2c4_status = getattr(p2c4, "status", None)
    decision = getattr(p2c4, "decision", None)
    verdict = getattr(decision, "verdict", None)
    if p2c4_status is SubmissionAuthorizationResultStatus.BLOCKED:
        return stop(
            ApplicationExecutionStage.GATE_B_AUTHORIZATION,
            stage_status=ApplicationExecutionStageStatus.BLOCKED,
            public_status=p2c4_status.value,
            reason_code=_public_reason(p2c4, "GATE_B_BLOCKED"),
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    if verdict is SubmissionAuthorizationVerdict.USER_AUTHORIZATION_REQUIRED:
        return stop(
            ApplicationExecutionStage.GATE_B_AUTHORIZATION,
            stage_status=ApplicationExecutionStageStatus.DEFERRED,
            public_status=str(getattr(p2c4_status, "value", p2c4_status)),
            reason_code="USER_AUTHORIZATION_REQUIRED",
            overall=ApplicationExecutionRunStatus.DEFERRED,
        )
    if verdict is SubmissionAuthorizationVerdict.BLOCKED:
        return stop(
            ApplicationExecutionStage.GATE_B_AUTHORIZATION,
            stage_status=ApplicationExecutionStageStatus.BLOCKED,
            public_status=str(getattr(p2c4_status, "value", p2c4_status)),
            reason_code="GATE_B_BLOCKED",
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    if (
        p2c4_status
        not in {
            SubmissionAuthorizationResultStatus.AUTHORIZED,
            SubmissionAuthorizationResultStatus.UNCHANGED,
        }
        or verdict is not SubmissionAuthorizationVerdict.AUTHORIZED
    ):
        stage_status = (
            ApplicationExecutionStageStatus.DEFERRED
            if p2c4_status
            is SubmissionAuthorizationResultStatus
            .DEFERRED_USER_AUTHORIZATION_REQUIRED
            else ApplicationExecutionStageStatus.FAILED
        )
        return stop(
            ApplicationExecutionStage.GATE_B_AUTHORIZATION,
            stage_status=stage_status,
            public_status=str(getattr(p2c4_status, "value", p2c4_status)),
            reason_code=_public_reason(
                p2c4,
                "USER_AUTHORIZATION_REQUIRED"
                if stage_status is ApplicationExecutionStageStatus.DEFERRED
                else ApplicationExecutionFailureReason.PUBLIC_STAGE_CONTRACT_FAILURE,
            ),
            overall=(
                ApplicationExecutionRunStatus.DEFERRED
                if stage_status is ApplicationExecutionStageStatus.DEFERRED
                else ApplicationExecutionRunStatus.FAILED
            ),
        )
    try:
        c4_id = _clean("authorization decision ID", decision.decision_id)
        c4_hash = _require_hash(
            "authorization decision hash", decision.decision_canonical_hash
        )
    except (AttributeError, TypeError, ValueError):
        return stop(
            ApplicationExecutionStage.GATE_B_AUTHORIZATION,
            stage_status=ApplicationExecutionStageStatus.FAILED,
            public_status="PUBLIC_STAGE_CONTRACT_FAILURE",
            reason_code=ApplicationExecutionFailureReason.PUBLIC_STAGE_CONTRACT_FAILURE,
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    stages.append(
        ApplicationExecutionStageResult.create(
            stage=ApplicationExecutionStage.GATE_B_AUTHORIZATION,
            status=(
                ApplicationExecutionStageStatus.UNCHANGED
                if p2c4_status
                is SubmissionAuthorizationResultStatus.UNCHANGED
                else ApplicationExecutionStageStatus.CREATED
            ),
            public_status=p2c4_status.value,
            record_id=c4_id,
            record_hash=c4_hash,
        )
    )

    p2c5 = await invoke(
        ApplicationExecutionStage.SUBMISSION_PERMIT_ISSUANCE,
        submission_permit_issuance,
        IssueSubmissionPermitCommand(
            subject_id=subject,
            submission_authorization_decision_id=c4_id,
            now=now,
        ),
    )
    if p2c5 is None:
        return stop(
            ApplicationExecutionStage.SUBMISSION_PERMIT_ISSUANCE,
            stage_status=ApplicationExecutionStageStatus.FAILED,
            public_status="PUBLIC_STAGE_EXCEPTION",
            reason_code=ApplicationExecutionFailureReason.PUBLIC_STAGE_EXCEPTION,
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    p2c5_status = getattr(p2c5, "status", None)
    permit = getattr(p2c5, "record", None)
    if p2c5_status not in {
        SubmissionPermitStatus.CREATED,
        SubmissionPermitStatus.UNCHANGED,
    }:
        deferred = p2c5_status in {
            SubmissionPermitStatus.NOT_AUTHORIZED,
            SubmissionPermitStatus.DEFERRED_ISSUER_UNAVAILABLE,
        }
        return stop(
            ApplicationExecutionStage.SUBMISSION_PERMIT_ISSUANCE,
            stage_status=(
                ApplicationExecutionStageStatus.DEFERRED
                if deferred
                else ApplicationExecutionStageStatus.FAILED
            ),
            public_status=str(getattr(p2c5_status, "value", p2c5_status)),
            reason_code=_public_reason(
                p2c5,
                str(getattr(p2c5_status, "value", "PERMIT_STAGE_FAILED")),
            ),
            overall=(
                ApplicationExecutionRunStatus.DEFERRED
                if deferred
                else ApplicationExecutionRunStatus.FAILED
            ),
        )
    try:
        c5_id = _clean("submission permit record ID", permit.record_id)
        c5_hash = _require_hash(
            "submission permit record hash", permit.record_canonical_hash
        )
    except (AttributeError, TypeError, ValueError):
        return stop(
            ApplicationExecutionStage.SUBMISSION_PERMIT_ISSUANCE,
            stage_status=ApplicationExecutionStageStatus.FAILED,
            public_status="PUBLIC_STAGE_CONTRACT_FAILURE",
            reason_code=ApplicationExecutionFailureReason.PUBLIC_STAGE_CONTRACT_FAILURE,
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    stages.append(
        ApplicationExecutionStageResult.create(
            stage=ApplicationExecutionStage.SUBMISSION_PERMIT_ISSUANCE,
            status=ApplicationExecutionStageStatus(p2c5_status.value),
            public_status=p2c5_status.value,
            record_id=c5_id,
            record_hash=c5_hash,
        )
    )

    p2c6 = await invoke(
        ApplicationExecutionStage.AUTHORIZED_SUBMISSION_EXECUTION,
        authorized_submission_execution,
        ExecuteAuthorizedSubmissionCommand(
            subject_id=subject,
            submission_permit_record_id=c5_id,
            now=now,
        ),
    )
    if p2c6 is None:
        return stop(
            ApplicationExecutionStage.AUTHORIZED_SUBMISSION_EXECUTION,
            stage_status=ApplicationExecutionStageStatus.FAILED,
            public_status="PUBLIC_STAGE_EXCEPTION",
            reason_code=ApplicationExecutionFailureReason.PUBLIC_STAGE_EXCEPTION,
            overall=ApplicationExecutionRunStatus.FAILED,
        )
    p2c6_status = getattr(p2c6, "status", None)
    execution = getattr(p2c6, "record", None)
    if p2c6_status in {
        AuthorizedSubmissionExecutionStatus.CREATED,
        AuthorizedSubmissionExecutionStatus.UNCHANGED,
        AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN,
    }:
        try:
            c6_id = _clean(
                "authorized submission execution record ID",
                execution.record_id,
            )
            c6_hash = _require_hash(
                "authorized submission execution record hash",
                execution.record_canonical_hash,
            )
        except (AttributeError, TypeError, ValueError):
            return stop(
                ApplicationExecutionStage.AUTHORIZED_SUBMISSION_EXECUTION,
                stage_status=ApplicationExecutionStageStatus.FAILED,
                public_status="PUBLIC_STAGE_CONTRACT_FAILURE",
                reason_code=ApplicationExecutionFailureReason.PUBLIC_STAGE_CONTRACT_FAILURE,
                overall=ApplicationExecutionRunStatus.FAILED,
            )
        uncertain = (
            p2c6_status
            is AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN
            or getattr(execution, "outcome", None)
            is AuthorizedSubmissionOutcome.SUBMISSION_UNCERTAIN
        )
        stages.append(
            ApplicationExecutionStageResult.create(
                stage=(
                    ApplicationExecutionStage
                    .AUTHORIZED_SUBMISSION_EXECUTION
                ),
                status=(
                    ApplicationExecutionStageStatus.SUBMISSION_UNCERTAIN
                    if uncertain
                    else ApplicationExecutionStageStatus(p2c6_status.value)
                ),
                public_status=p2c6_status.value,
                record_id=c6_id,
                record_hash=c6_hash,
            )
        )
        return _persist(
            _build_run(
                assembly=assembly,
                binding=binding,
                metadata=metadata,
                command=command,
                stages=tuple(stages),
                overall_status=(
                    ApplicationExecutionRunStatus.SUBMISSION_UNCERTAIN
                    if uncertain
                    else ApplicationExecutionRunStatus.COMPLETED
                ),
                now=now,
            ),
            run_repository,
        )
    deferred = p2c6_status in {
        AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
        AuthorizedSubmissionExecutionStatus.DEFERRED_BROWSER_UNAVAILABLE,
        AuthorizedSubmissionExecutionStatus.DEFERRED_REVIEW_CHANGED,
        AuthorizedSubmissionExecutionStatus
        .DEFERRED_RUNTIME_INPUT_REQUIRED,
    }
    return stop(
        ApplicationExecutionStage.AUTHORIZED_SUBMISSION_EXECUTION,
        stage_status=(
            ApplicationExecutionStageStatus.DEFERRED
            if deferred
            else ApplicationExecutionStageStatus.FAILED
        ),
        public_status=str(getattr(p2c6_status, "value", p2c6_status)),
        reason_code=_public_reason(
            p2c6,
            str(getattr(p2c6_status, "value", "SUBMISSION_STAGE_FAILED")),
        ),
        overall=(
            ApplicationExecutionRunStatus.DEFERRED
            if deferred
            else ApplicationExecutionRunStatus.FAILED
        ),
    )


__all__ = [
    "APPLICATION_EXECUTION_ORCHESTRATION_CONTRACT_VERSION",
    "APPLICATION_EXECUTION_STAGE_ORDER",
    "ApplicationExecutionFailureReason",
    "ApplicationExecutionOrchestrationMetadata",
    "ApplicationExecutionRun",
    "ApplicationExecutionRunReadResult",
    "ApplicationExecutionRunReadStatus",
    "ApplicationExecutionRunListResult",
    "ApplicationExecutionRunListStatus",
    "ApplicationExecutionRunRepository",
    "ApplicationExecutionRunStatus",
    "ApplicationExecutionRunWriteResult",
    "ApplicationExecutionRunWriteStatus",
    "ApplicationExecutionStage",
    "ApplicationExecutionStageResult",
    "ApplicationExecutionStageStatus",
    "ApplicationExecutionStatus",
    "PrivateHomeApplicationExecutionRunRepository",
    "RunApplicationExecutionCommand",
    "RunApplicationExecutionResult",
    "run_application_execution",
]
