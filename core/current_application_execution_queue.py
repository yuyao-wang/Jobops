"""Subject-scoped read model for current plan execution eligibility."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .application_bundle_assembly import (
    ApplicationBundleAssemblyListStatus,
    ApplicationBundleAssemblyReadStatus,
    ApplicationBundleAssemblyRecord,
    ApplicationBundleAssemblyRepository,
)
from .application_execution_orchestrator import (
    ApplicationExecutionRun,
    ApplicationExecutionRunListStatus,
    ApplicationExecutionRunReadStatus,
    ApplicationExecutionRunRepository,
    ApplicationExecutionRunStatus,
    ApplicationExecutionStage,
)
from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .job_prioritization import ProposedPriorityLevel


CURRENT_APPLICATION_EXECUTION_QUEUE_CONTRACT_VERSION = (
    "current-application-execution-queue-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ITEM_ID_RE = re.compile(
    r"^current-application-execution-item-[a-f0-9]{64}$"
)


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
        raise ValueError(f"{name} is outside the execution-queue contract")
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


class CurrentApplicationExecutionQueueStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class CurrentApplicationExecutionStatus(StrEnum):
    READY = "READY"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    SUBMITTED = "SUBMITTED"


class CurrentApplicationExecutionQueueFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    ASSEMBLY_LIST_INTEGRITY_FAILURE = "ASSEMBLY_LIST_INTEGRITY_FAILURE"
    ASSEMBLY_CURRENT_INTEGRITY_FAILURE = "ASSEMBLY_CURRENT_INTEGRITY_FAILURE"
    EXECUTION_RUN_LIST_INTEGRITY_FAILURE = (
        "EXECUTION_RUN_LIST_INTEGRITY_FAILURE"
    )
    EXECUTION_RUN_CURRENT_INTEGRITY_FAILURE = (
        "EXECUTION_RUN_CURRENT_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    BINDING_MISMATCH = "BINDING_MISMATCH"


_STATUS_ORDER = {
    CurrentApplicationExecutionStatus.READY: 0,
    CurrentApplicationExecutionStatus.DEFERRED: 1,
    CurrentApplicationExecutionStatus.FAILED: 2,
    CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN: 3,
    CurrentApplicationExecutionStatus.SUBMITTED: 4,
}
_PRIORITY_ORDER = {
    ProposedPriorityLevel.P0: 0,
    ProposedPriorityLevel.P1: 1,
    ProposedPriorityLevel.P2: 2,
    ProposedPriorityLevel.P3: 3,
}


@dataclass(frozen=True, slots=True)
class CurrentApplicationExecutionQueueItem:
    item_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    priority: ProposedPriorityLevel
    plan_created_at: datetime
    assembly_record_id: str
    assembly_record_hash: str
    execution_run_id: str | None
    execution_run_binding_hash: str | None
    execution_status: CurrentApplicationExecutionStatus
    deferred_stage: ApplicationExecutionStage | None
    deferred_reason: str | None
    failed_stage: ApplicationExecutionStage | None
    failed_reason: str | None
    item_hash: str

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != CURRENT_APPLICATION_EXECUTION_QUEUE_CONTRACT_VERSION
        ):
            raise ValueError("execution queue contract is unsupported")
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "assembly_record_id",
        ):
            _clean(name, getattr(self, name))
        _require_hash("assembly_record_hash", self.assembly_record_hash)
        object.__setattr__(
            self, "priority", ProposedPriorityLevel(self.priority)
        )
        _aware("plan_created_at", self.plan_created_at)
        if (self.execution_run_id is None) != (
            self.execution_run_binding_hash is None
        ):
            raise ValueError("execution run binding is incomplete")
        if self.execution_run_id is not None:
            _clean("execution_run_id", self.execution_run_id)
            _require_hash(
                "execution_run_binding_hash",
                self.execution_run_binding_hash,
            )
        object.__setattr__(
            self,
            "execution_status",
            CurrentApplicationExecutionStatus(self.execution_status),
        )
        if self.deferred_stage is not None:
            object.__setattr__(
                self,
                "deferred_stage",
                ApplicationExecutionStage(self.deferred_stage),
            )
        if self.failed_stage is not None:
            object.__setattr__(
                self,
                "failed_stage",
                ApplicationExecutionStage(self.failed_stage),
            )
        if self.execution_status is CurrentApplicationExecutionStatus.DEFERRED:
            if (
                self.deferred_stage is None
                or self.deferred_reason is None
                or self.failed_stage is not None
                or self.failed_reason is not None
            ):
                raise ValueError("deferred queue item is malformed")
            _clean("deferred_reason", self.deferred_reason)
        elif self.execution_status is CurrentApplicationExecutionStatus.FAILED:
            if (
                self.failed_stage is None
                or self.failed_reason is None
                or self.deferred_stage is not None
                or self.deferred_reason is not None
            ):
                raise ValueError("failed queue item is malformed")
            _clean("failed_reason", self.failed_reason)
        elif any(
            value is not None
            for value in (
                self.deferred_stage,
                self.deferred_reason,
                self.failed_stage,
                self.failed_reason,
            )
        ):
            raise ValueError("terminal/ready item cannot carry stop reasons")
        if (
            self.execution_status is CurrentApplicationExecutionStatus.READY
            and self.execution_run_id is not None
        ):
            raise ValueError("ready item cannot bind an execution run")
        expected_id = "current-application-execution-item-" + _hash(
            self.identity_dict()
        )
        if (
            _ITEM_ID_RE.fullmatch(self.item_id) is None
            or self.item_id != expected_id
        ):
            raise ValueError("execution queue item identity is invalid")
        if self.item_hash != _hash(self.content_dict()):
            raise ValueError("execution queue item hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "assembly_record_hash": self.assembly_record_hash,
            "assembly_record_id": self.assembly_record_id,
            "contract_version": self.contract_version,
            "execution_run_binding_hash": self.execution_run_binding_hash,
            "execution_run_id": self.execution_run_id,
            "execution_status": self.execution_status.value,
            "job_id": self.job_id,
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "deferred_reason": self.deferred_reason,
            "deferred_stage": (
                self.deferred_stage.value if self.deferred_stage else None
            ),
            "failed_reason": self.failed_reason,
            "failed_stage": (
                self.failed_stage.value if self.failed_stage else None
            ),
            "item_id": self.item_id,
            "plan_created_at": _rfc3339(self.plan_created_at),
            "priority": self.priority.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_dict(), "item_hash": self.item_hash}


def _item_sort_key(item: CurrentApplicationExecutionQueueItem) -> tuple[Any, ...]:
    return (
        _STATUS_ORDER[item.execution_status],
        _PRIORITY_ORDER[item.priority],
        item.plan_created_at.astimezone(timezone.utc),
        item.job_id,
        item.application_plan_id,
    )


@dataclass(frozen=True, slots=True)
class CurrentApplicationExecutionQueueResult:
    status: CurrentApplicationExecutionQueueStatus
    items: tuple[CurrentApplicationExecutionQueueItem, ...]
    ready_items: tuple[CurrentApplicationExecutionQueueItem, ...]
    non_ready_items: tuple[CurrentApplicationExecutionQueueItem, ...]
    ready_count: int
    deferred_count: int
    failed_count: int
    submission_uncertain_count: int
    submitted_count: int
    affected_plan_count: int
    snapshot_hash: str
    evaluated_at: datetime
    failure_reason: CurrentApplicationExecutionQueueFailureReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            CurrentApplicationExecutionQueueStatus(self.status),
        )
        _aware("evaluated_at", self.evaluated_at)
        if self.status is CurrentApplicationExecutionQueueStatus.FAILED:
            if (
                self.items
                or self.ready_items
                or self.non_ready_items
                or any(
                    (
                        self.ready_count,
                        self.deferred_count,
                        self.failed_count,
                        self.submission_uncertain_count,
                        self.submitted_count,
                        self.affected_plan_count,
                    )
                )
                or self.failure_reason is None
            ):
                raise ValueError("failed execution queue result is malformed")
            return
        if self.failure_reason is not None:
            raise ValueError("successful queue cannot have a failure reason")
        if self.items != tuple(sorted(self.items, key=_item_sort_key)):
            raise ValueError("execution queue ordering is invalid")
        if self.ready_items != tuple(
            item
            for item in self.items
            if item.execution_status is CurrentApplicationExecutionStatus.READY
        ):
            raise ValueError("ready item projection is invalid")
        if self.non_ready_items != tuple(
            item
            for item in self.items
            if item.execution_status is not CurrentApplicationExecutionStatus.READY
        ):
            raise ValueError("non-ready item projection is invalid")
        expected_counts = {
            status: sum(
                item.execution_status is status for item in self.items
            )
            for status in CurrentApplicationExecutionStatus
        }
        if (
            self.ready_count
            != expected_counts[CurrentApplicationExecutionStatus.READY]
            or self.deferred_count
            != expected_counts[CurrentApplicationExecutionStatus.DEFERRED]
            or self.failed_count
            != expected_counts[CurrentApplicationExecutionStatus.FAILED]
            or self.submission_uncertain_count
            != expected_counts[
                CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN
            ]
            or self.submitted_count
            != expected_counts[CurrentApplicationExecutionStatus.SUBMITTED]
            or self.affected_plan_count != len(self.items)
        ):
            raise ValueError("execution queue counts are invalid")
        expected_snapshot = _hash(
            {
                "contract_version": (
                    CURRENT_APPLICATION_EXECUTION_QUEUE_CONTRACT_VERSION
                ),
                "item_hashes": [item.item_hash for item in self.items],
            }
        )
        if self.snapshot_hash != expected_snapshot:
            raise ValueError("execution queue snapshot hash is invalid")


def _failed(
    now: datetime,
    reason: CurrentApplicationExecutionQueueFailureReason,
) -> CurrentApplicationExecutionQueueResult:
    return CurrentApplicationExecutionQueueResult(
        status=CurrentApplicationExecutionQueueStatus.FAILED,
        items=(),
        ready_items=(),
        non_ready_items=(),
        ready_count=0,
        deferred_count=0,
        failed_count=0,
        submission_uncertain_count=0,
        submitted_count=0,
        affected_plan_count=0,
        snapshot_hash="",
        evaluated_at=now,
        failure_reason=reason,
    )


def _make_item(
    *,
    plan: ApplicationPlan,
    assembly: ApplicationBundleAssemblyRecord,
    run: ApplicationExecutionRun | None,
    status: CurrentApplicationExecutionStatus,
) -> CurrentApplicationExecutionQueueItem:
    identity = {
        "application_plan_id": plan.plan_id,
        "assembly_record_hash": assembly.record_content_hash,
        "assembly_record_id": assembly.record_id,
        "contract_version": CURRENT_APPLICATION_EXECUTION_QUEUE_CONTRACT_VERSION,
        "execution_run_binding_hash": (
            run.execution_binding_hash if run else None
        ),
        "execution_run_id": run.run_id if run else None,
        "execution_status": status.value,
        "job_id": plan.job_id,
        "subject_id": plan.subject_id,
    }
    item_id = "current-application-execution-item-" + _hash(identity)
    content = {
        **identity,
        "deferred_reason": run.deferred_reason if run else None,
        "deferred_stage": (
            run.deferred_stage.value
            if run is not None and run.deferred_stage is not None
            else None
        ),
        "failed_reason": run.failed_reason if run else None,
        "failed_stage": (
            run.failed_stage.value
            if run is not None and run.failed_stage is not None
            else None
        ),
        "item_id": item_id,
        "plan_created_at": _rfc3339(plan.created_at),
        "priority": plan.priority_level.value,
    }
    return CurrentApplicationExecutionQueueItem(
        item_id=item_id,
        contract_version=CURRENT_APPLICATION_EXECUTION_QUEUE_CONTRACT_VERSION,
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        priority=plan.priority_level,
        plan_created_at=plan.created_at,
        assembly_record_id=assembly.record_id,
        assembly_record_hash=assembly.record_content_hash,
        execution_run_id=run.run_id if run else None,
        execution_run_binding_hash=(
            run.execution_binding_hash if run else None
        ),
        execution_status=status,
        deferred_stage=run.deferred_stage if run else None,
        deferred_reason=run.deferred_reason if run else None,
        failed_stage=run.failed_stage if run else None,
        failed_reason=run.failed_reason if run else None,
        item_hash=_hash(content),
    )


def _run_binding_is_valid(
    run: ApplicationExecutionRun,
    *,
    subject_id: str,
    plan: ApplicationPlan,
    assemblies_by_id: Mapping[str, ApplicationBundleAssemblyRecord],
) -> bool:
    assembly = assemblies_by_id.get(run.assembly_record_id)
    return bool(
        run.subject_id == subject_id
        and run.application_plan_id == plan.plan_id
        and run.job_id == plan.job_id
        and assembly is not None
        and assembly.subject_id == subject_id
        and assembly.application_plan_id == plan.plan_id
        and assembly.job_id == plan.job_id
        and run.assembly_record_hash == assembly.record_content_hash
    )


def build_current_application_execution_queue(
    *,
    subject_id: str,
    now: datetime,
    assembly_repository: ApplicationBundleAssemblyRepository,
    execution_run_repository: ApplicationExecutionRunRepository,
    application_plan_repository: ApplicationPlanRepository,
) -> CurrentApplicationExecutionQueueResult:
    """Derive current execution eligibility without writes or stage calls."""

    try:
        subject = _clean("subject_id", subject_id, 160)
        evaluated = _aware("now", now)
    except (TypeError, ValueError):
        return _failed(
            (
                now
                if isinstance(now, datetime) and now.tzinfo
                else datetime(1970, 1, 1, tzinfo=timezone.utc)
            ),
            CurrentApplicationExecutionQueueFailureReason.INVALID_REQUEST,
        )
    try:
        assembly_list = assembly_repository.list_for_subject(
            subject_id=subject
        )
    except Exception:
        return _failed(
            evaluated,
            CurrentApplicationExecutionQueueFailureReason
            .ASSEMBLY_LIST_INTEGRITY_FAILURE,
        )
    if (
        assembly_list.status
        is not ApplicationBundleAssemblyListStatus.SUCCEEDED
    ):
        return _failed(
            evaluated,
            CurrentApplicationExecutionQueueFailureReason
            .ASSEMBLY_LIST_INTEGRITY_FAILURE,
        )
    try:
        run_list = execution_run_repository.list_for_subject(
            subject_id=subject
        )
    except Exception:
        return _failed(
            evaluated,
            CurrentApplicationExecutionQueueFailureReason
            .EXECUTION_RUN_LIST_INTEGRITY_FAILURE,
        )
    if run_list.status is not ApplicationExecutionRunListStatus.SUCCEEDED:
        return _failed(
            evaluated,
            CurrentApplicationExecutionQueueFailureReason
            .EXECUTION_RUN_LIST_INTEGRITY_FAILURE,
        )

    assemblies_by_id = {
        item.record_id: item for item in assembly_list.records
    }
    assemblies_by_plan: dict[str, list[ApplicationBundleAssemblyRecord]] = {}
    for assembly in assembly_list.records:
        if assembly.subject_id != subject:
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason.BINDING_MISMATCH,
            )
        assemblies_by_plan.setdefault(
            assembly.application_plan_id, []
        ).append(assembly)
    runs_by_plan: dict[str, list[ApplicationExecutionRun]] = {}
    for run in run_list.runs:
        if (
            run.subject_id != subject
            or run.assembly_record_id not in assemblies_by_id
        ):
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason.BINDING_MISMATCH,
            )
        runs_by_plan.setdefault(run.application_plan_id, []).append(run)

    items: list[CurrentApplicationExecutionQueueItem] = []
    for plan_id in sorted(assemblies_by_plan):
        try:
            plan_read = application_plan_repository.get(plan_id)
        except Exception:
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason
                .APPLICATION_PLAN_INTEGRITY_FAILURE,
            )
        if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason
                .APPLICATION_PLAN_NOT_FOUND,
            )
        if (
            plan_read.status is not ApplicationPlanReadStatus.FOUND
            or not isinstance(plan_read.plan, ApplicationPlan)
        ):
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason
                .APPLICATION_PLAN_INTEGRITY_FAILURE,
            )
        plan = plan_read.plan
        if plan.subject_id != subject or plan.plan_id != plan_id:
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason.BINDING_MISMATCH,
            )
        try:
            current_assembly_read = assembly_repository.find_current_for_plan(
                subject_id=subject, application_plan_id=plan_id
            )
        except Exception:
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason
                .ASSEMBLY_CURRENT_INTEGRITY_FAILURE,
            )
        if (
            current_assembly_read.status
            is not ApplicationBundleAssemblyReadStatus.FOUND
            or current_assembly_read.record is None
            or current_assembly_read.record.record_id
            not in assemblies_by_id
        ):
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason
                .ASSEMBLY_CURRENT_INTEGRITY_FAILURE,
            )
        current_assembly = current_assembly_read.record
        if (
            current_assembly.subject_id != subject
            or current_assembly.application_plan_id != plan.plan_id
            or current_assembly.job_id != plan.job_id
        ):
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason.BINDING_MISMATCH,
            )
        plan_runs = runs_by_plan.get(plan_id, [])
        if any(
            not _run_binding_is_valid(
                run,
                subject_id=subject,
                plan=plan,
                assemblies_by_id=assemblies_by_id,
            )
            for run in plan_runs
        ):
            return _failed(
                evaluated,
                CurrentApplicationExecutionQueueFailureReason.BINDING_MISMATCH,
            )
        completed = [
            run
            for run in plan_runs
            if run.overall_status is ApplicationExecutionRunStatus.COMPLETED
        ]
        uncertain = [
            run
            for run in plan_runs
            if run.overall_status
            is ApplicationExecutionRunStatus.SUBMISSION_UNCERTAIN
        ]
        if completed:
            selected_run = max(
                completed,
                key=lambda item: (
                    item.completed_at.astimezone(timezone.utc),
                    item.run_id,
                ),
            )
            queue_status = CurrentApplicationExecutionStatus.SUBMITTED
        elif uncertain:
            selected_run = max(
                uncertain,
                key=lambda item: (
                    item.completed_at.astimezone(timezone.utc),
                    item.run_id,
                ),
            )
            queue_status = (
                CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN
            )
        else:
            try:
                current_run_read = (
                    execution_run_repository.find_current_for_assembly(
                        subject_id=subject,
                        assembly_record_id=current_assembly.record_id,
                    )
                )
            except Exception:
                return _failed(
                    evaluated,
                    CurrentApplicationExecutionQueueFailureReason
                    .EXECUTION_RUN_CURRENT_INTEGRITY_FAILURE,
                )
            if (
                current_run_read.status
                is ApplicationExecutionRunReadStatus.NOT_FOUND
            ):
                selected_run = None
                queue_status = CurrentApplicationExecutionStatus.READY
            elif (
                current_run_read.status
                is not ApplicationExecutionRunReadStatus.FOUND
                or current_run_read.run is None
                or current_run_read.run not in plan_runs
                or current_run_read.run.assembly_record_id
                != current_assembly.record_id
            ):
                return _failed(
                    evaluated,
                    CurrentApplicationExecutionQueueFailureReason
                    .EXECUTION_RUN_CURRENT_INTEGRITY_FAILURE,
                )
            else:
                selected_run = current_run_read.run
                if (
                    selected_run.overall_status
                    is ApplicationExecutionRunStatus.DEFERRED
                ):
                    queue_status = CurrentApplicationExecutionStatus.DEFERRED
                elif (
                    selected_run.overall_status
                    is ApplicationExecutionRunStatus.FAILED
                ):
                    queue_status = CurrentApplicationExecutionStatus.FAILED
                else:
                    return _failed(
                        evaluated,
                        CurrentApplicationExecutionQueueFailureReason
                        .EXECUTION_RUN_CURRENT_INTEGRITY_FAILURE,
                    )
        items.append(
            _make_item(
                plan=plan,
                assembly=current_assembly,
                run=selected_run,
                status=queue_status,
            )
        )
    ordered = tuple(sorted(items, key=_item_sort_key))
    ready = tuple(
        item
        for item in ordered
        if item.execution_status is CurrentApplicationExecutionStatus.READY
    )
    non_ready = tuple(item for item in ordered if item not in ready)
    counts = {
        status: sum(item.execution_status is status for item in ordered)
        for status in CurrentApplicationExecutionStatus
    }
    snapshot_hash = _hash(
        {
            "contract_version": (
                CURRENT_APPLICATION_EXECUTION_QUEUE_CONTRACT_VERSION
            ),
            "item_hashes": [item.item_hash for item in ordered],
        }
    )
    return CurrentApplicationExecutionQueueResult(
        status=CurrentApplicationExecutionQueueStatus.SUCCEEDED,
        items=ordered,
        ready_items=ready,
        non_ready_items=non_ready,
        ready_count=counts[CurrentApplicationExecutionStatus.READY],
        deferred_count=counts[CurrentApplicationExecutionStatus.DEFERRED],
        failed_count=counts[CurrentApplicationExecutionStatus.FAILED],
        submission_uncertain_count=counts[
            CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN
        ],
        submitted_count=counts[CurrentApplicationExecutionStatus.SUBMITTED],
        affected_plan_count=len(ordered),
        snapshot_hash=snapshot_hash,
        evaluated_at=evaluated,
    )


__all__ = [
    "CURRENT_APPLICATION_EXECUTION_QUEUE_CONTRACT_VERSION",
    "CurrentApplicationExecutionQueueFailureReason",
    "CurrentApplicationExecutionQueueItem",
    "CurrentApplicationExecutionQueueResult",
    "CurrentApplicationExecutionQueueStatus",
    "CurrentApplicationExecutionStatus",
    "build_current_application_execution_queue",
]
