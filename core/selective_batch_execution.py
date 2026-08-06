"""Bounded serial execution of READY items from one fixed P2c8 snapshot."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from .application_execution_orchestrator import (
    ApplicationExecutionStage,
    ApplicationExecutionStatus,
    RunApplicationExecutionCommand,
    RunApplicationExecutionResult,
)
from .current_application_execution_queue import (
    CurrentApplicationExecutionQueueItem,
    CurrentApplicationExecutionQueueResult,
    CurrentApplicationExecutionQueueStatus,
    CurrentApplicationExecutionStatus,
)
from .submission_authorization import ExplicitSubmissionAuthorization


SELECTIVE_BATCH_EXECUTION_CONTRACT_VERSION = (
    "selective-batch-application-execution-v1"
)


def _clean(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the batch execution contract")
    return cleaned


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


class SelectiveBatchExecutionStatus(StrEnum):
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class BatchApplicationExecutionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    SKIPPED_NOT_READY = "SKIPPED_NOT_READY"
    SKIPPED_SUBMITTED = "SKIPPED_SUBMITTED"
    SKIPPED_UNCERTAIN = "SKIPPED_UNCERTAIN"
    NOT_FOUND = "NOT_FOUND"


class BatchApplicationExecutionReason(StrEnum):
    QUEUE_DEFERRED = "QUEUE_DEFERRED"
    QUEUE_FAILED = "QUEUE_FAILED"
    ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    PLAN_NOT_IN_SNAPSHOT = "PLAN_NOT_IN_SNAPSHOT"
    SINGLE_JOB_DEFERRED = "SINGLE_JOB_DEFERRED"
    SINGLE_JOB_FAILED = "SINGLE_JOB_FAILED"
    SINGLE_JOB_UNCERTAIN = "SINGLE_JOB_UNCERTAIN"
    SINGLE_JOB_EXCEPTION = "SINGLE_JOB_EXCEPTION"
    SINGLE_JOB_RESULT_INVALID = "SINGLE_JOB_RESULT_INVALID"


class SelectiveBatchExecutionFailureReason(StrEnum):
    QUEUE_READER_FAILED = "QUEUE_READER_FAILED"
    QUEUE_RESULT_INVALID = "QUEUE_RESULT_INVALID"


@dataclass(frozen=True, slots=True)
class BatchExecutionPlanInput:
    application_plan_id: str
    approve_gate_a: bool = False
    explicit_user_authorization: ExplicitSubmissionAuthorization | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "application_plan_id",
            _clean("application_plan_id", self.application_plan_id, 180),
        )
        if type(self.approve_gate_a) is not bool:
            raise TypeError("approve_gate_a must be boolean")
        if self.explicit_user_authorization is not None and not isinstance(
            self.explicit_user_authorization,
            ExplicitSubmissionAuthorization,
        ):
            raise TypeError("explicit user authorization must be typed")


@dataclass(frozen=True, slots=True)
class SelectiveBatchExecutionCommand:
    subject_id: str
    now: datetime
    application_plan_ids: tuple[str, ...] | None = None
    max_plans: int | None = None
    plan_inputs: tuple[BatchExecutionPlanInput, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean("subject_id", self.subject_id, 160)
        )
        _aware("now", self.now)
        if self.application_plan_ids is not None:
            if not isinstance(self.application_plan_ids, tuple):
                raise TypeError("application_plan_ids must be a tuple")
            object.__setattr__(
                self,
                "application_plan_ids",
                tuple(
                    _clean("application_plan_id", item, 180)
                    for item in self.application_plan_ids
                ),
            )
        if self.max_plans is not None and (
            type(self.max_plans) is not int or self.max_plans < 1
        ):
            raise ValueError("max_plans must be a positive integer")
        if not self.application_plan_ids and self.max_plans is None:
            raise ValueError(
                "a non-empty application_plan_ids or max_plans is required"
            )
        if not isinstance(self.plan_inputs, tuple) or any(
            not isinstance(item, BatchExecutionPlanInput)
            for item in self.plan_inputs
        ):
            raise TypeError("plan_inputs must contain typed entries")
        if len(
            {item.application_plan_id for item in self.plan_inputs}
        ) != len(self.plan_inputs):
            raise ValueError("plan_inputs must contain unique plans")


@dataclass(frozen=True, slots=True)
class SelectiveBatchExecutionPlanResult:
    application_plan_id: str
    job_id: str | None
    queue_status: CurrentApplicationExecutionStatus | None
    execution_status: BatchApplicationExecutionStatus
    execution_run_id: str | None
    reason: BatchApplicationExecutionReason | None
    source_reason: str | None

    def __post_init__(self) -> None:
        _clean("application_plan_id", self.application_plan_id, 180)
        if self.job_id is not None:
            _clean("job_id", self.job_id, 160)
        if self.queue_status is not None:
            object.__setattr__(
                self,
                "queue_status",
                CurrentApplicationExecutionStatus(self.queue_status),
            )
        object.__setattr__(
            self,
            "execution_status",
            BatchApplicationExecutionStatus(self.execution_status),
        )
        if self.execution_run_id is not None:
            _clean("execution_run_id", self.execution_run_id)
        if self.reason is not None:
            object.__setattr__(
                self, "reason", BatchApplicationExecutionReason(self.reason)
            )
        if self.source_reason is not None:
            _clean("source_reason", self.source_reason)
        if self.execution_status in {
            BatchApplicationExecutionStatus.COMPLETED,
            BatchApplicationExecutionStatus.UNCHANGED,
        }:
            if (
                self.queue_status is not CurrentApplicationExecutionStatus.READY
                or self.execution_run_id is None
                or self.reason is not None
                or self.source_reason is not None
            ):
                raise ValueError("successful batch item is malformed")
        elif self.execution_status is BatchApplicationExecutionStatus.NOT_FOUND:
            if (
                self.job_id is not None
                or self.queue_status is not None
                or self.execution_run_id is not None
                or self.reason
                is not BatchApplicationExecutionReason.PLAN_NOT_IN_SNAPSHOT
            ):
                raise ValueError("not-found batch item is malformed")
        elif self.execution_status in {
            BatchApplicationExecutionStatus.SKIPPED_NOT_READY,
            BatchApplicationExecutionStatus.SKIPPED_SUBMITTED,
            BatchApplicationExecutionStatus.SKIPPED_UNCERTAIN,
        }:
            if self.queue_status is None or self.execution_run_id is not None:
                raise ValueError("skipped batch item is malformed")
            expected = {
                BatchApplicationExecutionStatus.SKIPPED_NOT_READY: {
                    CurrentApplicationExecutionStatus.DEFERRED,
                    CurrentApplicationExecutionStatus.FAILED,
                },
                BatchApplicationExecutionStatus.SKIPPED_SUBMITTED: {
                    CurrentApplicationExecutionStatus.SUBMITTED
                },
                BatchApplicationExecutionStatus.SKIPPED_UNCERTAIN: {
                    CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN
                },
            }[self.execution_status]
            if self.queue_status not in expected or self.reason is None:
                raise ValueError("queue skip reason is malformed")
        elif (
            self.queue_status is not CurrentApplicationExecutionStatus.READY
            or self.reason is None
        ):
            raise ValueError("stopped execution item is malformed")


@dataclass(frozen=True, slots=True)
class SelectiveBatchExecutionSummary:
    requested: int
    selected: int
    completed: int
    unchanged: int
    deferred: int
    failed: int
    uncertain: int
    skipped_not_ready: int
    skipped_submitted: int
    skipped_uncertain: int
    not_found: int

    def __post_init__(self) -> None:
        for name in (
            "requested",
            "selected",
            "completed",
            "unchanged",
            "deferred",
            "failed",
            "uncertain",
            "skipped_not_ready",
            "skipped_submitted",
            "skipped_uncertain",
            "not_found",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.selected != (
            self.completed
            + self.unchanged
            + self.deferred
            + self.failed
            + self.uncertain
        ):
            raise ValueError("selected execution count is inconsistent")


@dataclass(frozen=True, slots=True)
class SelectiveBatchExecutionResult:
    status: SelectiveBatchExecutionStatus
    subject_id: str
    evaluated_at: datetime
    queue_snapshot_hash: str | None
    items: tuple[SelectiveBatchExecutionPlanResult, ...]
    summary: SelectiveBatchExecutionSummary
    failure_reason: SelectiveBatchExecutionFailureReason | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", SelectiveBatchExecutionStatus(self.status)
        )
        _clean("subject_id", self.subject_id, 160)
        _aware("evaluated_at", self.evaluated_at)
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, SelectiveBatchExecutionPlanResult)
            for item in self.items
        ):
            raise TypeError("batch execution items must be typed")
        if len(
            {item.application_plan_id for item in self.items}
        ) != len(self.items):
            raise ValueError("batch execution items must be unique")
        if not isinstance(self.summary, SelectiveBatchExecutionSummary):
            raise TypeError("batch execution summary must be typed")
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                SelectiveBatchExecutionFailureReason(self.failure_reason),
            )
        if self.status is SelectiveBatchExecutionStatus.FAILED and (
            self.failure_reason is not None
        ):
            if self.items or self.queue_snapshot_hash is not None:
                raise ValueError("fatal queue failure is malformed")
        elif (
            self.failure_reason is not None
            or self.queue_snapshot_hash is None
            or self.summary != _summarize(
                self.summary.requested, self.items
            )
            or self.status is not _overall(self.summary)
        ):
            raise ValueError("batch execution result is inconsistent")


class CurrentExecutionQueueReader(Protocol):
    def __call__(
        self, *, subject_id: str, now: datetime
    ) -> (
        CurrentApplicationExecutionQueueResult
        | Awaitable[CurrentApplicationExecutionQueueResult]
    ): ...


class SingleJobExecutionCallable(Protocol):
    def __call__(
        self, command: RunApplicationExecutionCommand
    ) -> (
        RunApplicationExecutionResult
        | Awaitable[RunApplicationExecutionResult]
    ): ...


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _summary_empty(requested: int) -> SelectiveBatchExecutionSummary:
    return SelectiveBatchExecutionSummary(
        requested=requested,
        selected=0,
        completed=0,
        unchanged=0,
        deferred=0,
        failed=0,
        uncertain=0,
        skipped_not_ready=0,
        skipped_submitted=0,
        skipped_uncertain=0,
        not_found=0,
    )


def _fatal(
    command: SelectiveBatchExecutionCommand,
    reason: SelectiveBatchExecutionFailureReason,
    *,
    requested: int,
) -> SelectiveBatchExecutionResult:
    return SelectiveBatchExecutionResult(
        status=SelectiveBatchExecutionStatus.FAILED,
        subject_id=command.subject_id,
        evaluated_at=command.now,
        queue_snapshot_hash=None,
        items=(),
        summary=_summary_empty(requested),
        failure_reason=reason,
    )


def _not_found(plan_id: str) -> SelectiveBatchExecutionPlanResult:
    return SelectiveBatchExecutionPlanResult(
        application_plan_id=plan_id,
        job_id=None,
        queue_status=None,
        execution_status=BatchApplicationExecutionStatus.NOT_FOUND,
        execution_run_id=None,
        reason=BatchApplicationExecutionReason.PLAN_NOT_IN_SNAPSHOT,
        source_reason=None,
    )


def _skipped(
    item: CurrentApplicationExecutionQueueItem,
) -> SelectiveBatchExecutionPlanResult:
    execution, reason = {
        CurrentApplicationExecutionStatus.DEFERRED: (
            BatchApplicationExecutionStatus.SKIPPED_NOT_READY,
            BatchApplicationExecutionReason.QUEUE_DEFERRED,
        ),
        CurrentApplicationExecutionStatus.FAILED: (
            BatchApplicationExecutionStatus.SKIPPED_NOT_READY,
            BatchApplicationExecutionReason.QUEUE_FAILED,
        ),
        CurrentApplicationExecutionStatus.SUBMITTED: (
            BatchApplicationExecutionStatus.SKIPPED_SUBMITTED,
            BatchApplicationExecutionReason.ALREADY_SUBMITTED,
        ),
        CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN: (
            BatchApplicationExecutionStatus.SKIPPED_UNCERTAIN,
            BatchApplicationExecutionReason.SUBMISSION_UNCERTAIN,
        ),
    }[item.execution_status]
    return SelectiveBatchExecutionPlanResult(
        application_plan_id=item.application_plan_id,
        job_id=item.job_id,
        queue_status=item.execution_status,
        execution_status=execution,
        execution_run_id=None,
        reason=reason,
        source_reason=(
            item.deferred_reason
            or item.failed_reason
            or item.execution_status.value
        ),
    )


def _invalid(
    item: CurrentApplicationExecutionQueueItem,
    *,
    queue_status: CurrentApplicationExecutionStatus | None = None,
) -> SelectiveBatchExecutionPlanResult:
    return SelectiveBatchExecutionPlanResult(
        application_plan_id=item.application_plan_id,
        job_id=item.job_id,
        queue_status=queue_status or item.execution_status,
        execution_status=BatchApplicationExecutionStatus.FAILED,
        execution_run_id=None,
        reason=BatchApplicationExecutionReason.SINGLE_JOB_RESULT_INVALID,
        source_reason="PUBLIC_P2C7_RESULT_INVALID",
    )


def _exception(
    item: CurrentApplicationExecutionQueueItem,
    *,
    queue_status: CurrentApplicationExecutionStatus | None = None,
) -> SelectiveBatchExecutionPlanResult:
    return SelectiveBatchExecutionPlanResult(
        application_plan_id=item.application_plan_id,
        job_id=item.job_id,
        queue_status=queue_status or item.execution_status,
        execution_status=BatchApplicationExecutionStatus.FAILED,
        execution_run_id=None,
        reason=BatchApplicationExecutionReason.SINGLE_JOB_EXCEPTION,
        source_reason="PUBLIC_P2C7_EXCEPTION",
    )


def _from_execution(
    item: CurrentApplicationExecutionQueueItem,
    result: Any,
    *,
    queue_status: CurrentApplicationExecutionStatus | None = None,
) -> SelectiveBatchExecutionPlanResult:
    effective_queue_status = queue_status or item.execution_status
    if not isinstance(result, RunApplicationExecutionResult):
        return _invalid(item, queue_status=effective_queue_status)
    try:
        status = ApplicationExecutionStatus(result.status)
        run_id = result.run.run_id if result.run is not None else None
        source_reason = (
            result.run.deferred_reason
            if status is ApplicationExecutionStatus.DEFERRED
            and result.run is not None
            else result.run.failed_reason
            if status is ApplicationExecutionStatus.FAILED
            and result.run is not None
            else result.reason.value
            if result.reason is not None
            else status.value
        )
    except (AttributeError, TypeError, ValueError):
        return _invalid(item, queue_status=effective_queue_status)
    if status in {
        ApplicationExecutionStatus.COMPLETED,
        ApplicationExecutionStatus.UNCHANGED,
    }:
        if run_id is None:
            return _invalid(item, queue_status=effective_queue_status)
        return SelectiveBatchExecutionPlanResult(
            application_plan_id=item.application_plan_id,
            job_id=item.job_id,
            queue_status=effective_queue_status,
            execution_status=BatchApplicationExecutionStatus(status.value),
            execution_run_id=run_id,
            reason=None,
            source_reason=None,
        )
    reason = {
        ApplicationExecutionStatus.DEFERRED: (
            BatchApplicationExecutionReason.SINGLE_JOB_DEFERRED
        ),
        ApplicationExecutionStatus.FAILED: (
            BatchApplicationExecutionReason.SINGLE_JOB_FAILED
        ),
        ApplicationExecutionStatus.SUBMISSION_UNCERTAIN: (
            BatchApplicationExecutionReason.SINGLE_JOB_UNCERTAIN
        ),
    }[status]
    return SelectiveBatchExecutionPlanResult(
        application_plan_id=item.application_plan_id,
        job_id=item.job_id,
        queue_status=effective_queue_status,
        execution_status=BatchApplicationExecutionStatus(status.value),
        execution_run_id=run_id,
        reason=reason,
        source_reason=source_reason,
    )


def _summarize(
    requested: int,
    items: tuple[SelectiveBatchExecutionPlanResult, ...],
) -> SelectiveBatchExecutionSummary:
    return SelectiveBatchExecutionSummary(
        requested=requested,
        selected=sum(
            item.execution_status
            in {
                BatchApplicationExecutionStatus.COMPLETED,
                BatchApplicationExecutionStatus.UNCHANGED,
                BatchApplicationExecutionStatus.DEFERRED,
                BatchApplicationExecutionStatus.FAILED,
                BatchApplicationExecutionStatus.SUBMISSION_UNCERTAIN,
            }
            for item in items
        ),
        completed=sum(
            item.execution_status is BatchApplicationExecutionStatus.COMPLETED
            for item in items
        ),
        unchanged=sum(
            item.execution_status is BatchApplicationExecutionStatus.UNCHANGED
            for item in items
        ),
        deferred=sum(
            item.execution_status is BatchApplicationExecutionStatus.DEFERRED
            for item in items
        ),
        failed=sum(
            item.execution_status is BatchApplicationExecutionStatus.FAILED
            for item in items
        ),
        uncertain=sum(
            item.execution_status
            is BatchApplicationExecutionStatus.SUBMISSION_UNCERTAIN
            for item in items
        ),
        skipped_not_ready=sum(
            item.execution_status
            is BatchApplicationExecutionStatus.SKIPPED_NOT_READY
            for item in items
        ),
        skipped_submitted=sum(
            item.execution_status
            is BatchApplicationExecutionStatus.SKIPPED_SUBMITTED
            for item in items
        ),
        skipped_uncertain=sum(
            item.execution_status
            is BatchApplicationExecutionStatus.SKIPPED_UNCERTAIN
            for item in items
        ),
        not_found=sum(
            item.execution_status is BatchApplicationExecutionStatus.NOT_FOUND
            for item in items
        ),
    )


def _overall(
    summary: SelectiveBatchExecutionSummary,
) -> SelectiveBatchExecutionStatus:
    if summary.selected == 0:
        return SelectiveBatchExecutionStatus.NOOP
    succeeded = summary.completed + summary.unchanged
    stopped = summary.deferred + summary.failed + summary.uncertain
    if stopped == 0:
        return SelectiveBatchExecutionStatus.COMPLETED
    if succeeded:
        return SelectiveBatchExecutionStatus.PARTIAL_FAILURE
    return SelectiveBatchExecutionStatus.FAILED


async def run_selective_batch_execution(
    command: SelectiveBatchExecutionCommand,
    *,
    execution_queue_reader: CurrentExecutionQueueReader,
    single_job_execution: SingleJobExecutionCallable,
) -> SelectiveBatchExecutionResult:
    """Read one queue snapshot and invoke P2c7 for READY items serially."""

    if not isinstance(command, SelectiveBatchExecutionCommand):
        raise TypeError("command must be a SelectiveBatchExecutionCommand")
    requested_ids = (
        _deduplicate(command.application_plan_ids)
        if command.application_plan_ids
        else None
    )
    requested_count = len(requested_ids) if requested_ids is not None else 0
    try:
        queue = await _resolve(
            execution_queue_reader(
                subject_id=command.subject_id,
                now=command.now,
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _fatal(
            command,
            SelectiveBatchExecutionFailureReason.QUEUE_READER_FAILED,
            requested=requested_count,
        )
    if not isinstance(queue, CurrentApplicationExecutionQueueResult):
        return _fatal(
            command,
            SelectiveBatchExecutionFailureReason.QUEUE_RESULT_INVALID,
            requested=requested_count,
        )
    if (
        queue.status is not CurrentApplicationExecutionQueueStatus.SUCCEEDED
        or queue.evaluated_at != command.now
        or any(item.subject_id != command.subject_id for item in queue.items)
    ):
        return _fatal(
            command,
            SelectiveBatchExecutionFailureReason.QUEUE_READER_FAILED,
            requested=requested_count,
        )
    by_plan = {item.application_plan_id: item for item in queue.items}
    explicit = requested_ids is not None
    if explicit:
        candidate_ids = requested_ids
    else:
        ready_ids = tuple(
            item.application_plan_id for item in queue.ready_items
        )
        requested_count = len(ready_ids)
        candidate_ids = ready_ids
    inputs = {
        item.application_plan_id: item for item in command.plan_inputs
    }
    results: list[SelectiveBatchExecutionPlanResult] = []
    selected = 0
    for plan_id in candidate_ids:
        if command.max_plans is not None and selected >= command.max_plans:
            break
        item = by_plan.get(plan_id)
        if item is None:
            results.append(_not_found(plan_id))
            continue
        plan_input = inputs.get(
            plan_id, BatchExecutionPlanInput(application_plan_id=plan_id)
        )
        resumes_gate_a = bool(
            explicit
            and plan_input.approve_gate_a
            and item.execution_status
            is CurrentApplicationExecutionStatus.DEFERRED
            and item.deferred_stage
            is ApplicationExecutionStage.NON_SUBMIT_EXECUTION
            and item.deferred_reason
            in {
                "DEFERRED_GATE_A_REQUIRED",
                "DEFERRED_RUNTIME_INPUT_REQUIRED",
                "RUNTIME_INPUT_REQUIRED",
            }
        )
        if (
            item.execution_status
            is not CurrentApplicationExecutionStatus.READY
            and not resumes_gate_a
        ):
            results.append(_skipped(item))
            continue
        selected += 1
        effective_queue_status = CurrentApplicationExecutionStatus.READY
        execution_command = RunApplicationExecutionCommand(
            subject_id=command.subject_id,
            application_bundle_assembly_record_id=item.assembly_record_id,
            now=command.now,
            approve_gate_a=plan_input.approve_gate_a,
            explicit_user_authorization=(
                plan_input.explicit_user_authorization
            ),
        )
        try:
            public_result = await _resolve(
                single_job_execution(execution_command)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            results.append(
                _exception(item, queue_status=effective_queue_status)
            )
            continue
        results.append(
            _from_execution(
                item,
                public_result,
                queue_status=effective_queue_status,
            )
        )
    typed_items = tuple(results)
    summary = _summarize(requested_count, typed_items)
    return SelectiveBatchExecutionResult(
        status=_overall(summary),
        subject_id=command.subject_id,
        evaluated_at=command.now,
        queue_snapshot_hash=queue.snapshot_hash,
        items=typed_items,
        summary=summary,
        failure_reason=None,
    )


__all__ = [
    "SELECTIVE_BATCH_EXECUTION_CONTRACT_VERSION",
    "BatchApplicationExecutionReason",
    "BatchApplicationExecutionStatus",
    "BatchExecutionPlanInput",
    "CurrentExecutionQueueReader",
    "SelectiveBatchExecutionCommand",
    "SelectiveBatchExecutionFailureReason",
    "SelectiveBatchExecutionPlanResult",
    "SelectiveBatchExecutionResult",
    "SelectiveBatchExecutionStatus",
    "SelectiveBatchExecutionSummary",
    "SingleJobExecutionCallable",
    "run_selective_batch_execution",
]
