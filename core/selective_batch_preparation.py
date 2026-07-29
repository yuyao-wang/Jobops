"""Bounded serial preparation of existing ApplicationPlans."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from .application_plan import (
    ApplicationPlan,
    ApplicationPlanListResult,
    ApplicationPlanListStatus,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .application_preparation_orchestrator import (
    ApplicationPreparationStatus,
    RunApplicationPreparationCommand,
    RunApplicationPreparationResult,
)
from .human_attention_queue import (
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
)


SELECTIVE_BATCH_PREPARATION_CONTRACT_VERSION = (
    "selective-batch-application-preparation-v1"
)


def _clean_text(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the batch contract")
    return cleaned


def _require_aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


class SelectiveBatchPreparationStatus(StrEnum):
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class BatchPlanSelectionStatus(StrEnum):
    SELECTED = "SELECTED"
    SKIPPED_HUMAN_ATTENTION = "SKIPPED_HUMAN_ATTENTION"
    NOT_FOUND = "NOT_FOUND"


class BatchPlanExecutionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"
    SKIPPED_HUMAN_ATTENTION = "SKIPPED_HUMAN_ATTENTION"
    NOT_FOUND = "NOT_FOUND"


class BatchPlanReasonCode(StrEnum):
    CURRENT_HUMAN_ATTENTION = "CURRENT_HUMAN_ATTENTION"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    SINGLE_JOB_DEFERRED = "SINGLE_JOB_DEFERRED"
    SINGLE_JOB_FAILED = "SINGLE_JOB_FAILED"
    SINGLE_JOB_EXCEPTION = "SINGLE_JOB_EXCEPTION"
    SINGLE_JOB_RESULT_INVALID = "SINGLE_JOB_RESULT_INVALID"


class SelectiveBatchFailureReason(StrEnum):
    HUMAN_ATTENTION_QUEUE_FAILED = "HUMAN_ATTENTION_QUEUE_FAILED"
    HUMAN_ATTENTION_QUEUE_RESULT_INVALID = (
        "HUMAN_ATTENTION_QUEUE_RESULT_INVALID"
    )
    APPLICATION_PLAN_LIST_FAILED = "APPLICATION_PLAN_LIST_FAILED"
    APPLICATION_PLAN_REPOSITORY_FAILURE = (
        "APPLICATION_PLAN_REPOSITORY_FAILURE"
    )


@dataclass(frozen=True, slots=True)
class SelectiveBatchPreparationCommand:
    subject_id: str
    now: datetime
    application_plan_ids: tuple[str, ...] | None = None
    max_plans: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean_text("subject_id", self.subject_id, 160)
        )
        _require_aware("now", self.now)
        if self.application_plan_ids is not None:
            if not isinstance(self.application_plan_ids, tuple):
                raise TypeError("application_plan_ids must be a tuple")
            normalized = tuple(
                _clean_text("application_plan_id", value, 180)
                for value in self.application_plan_ids
            )
            object.__setattr__(self, "application_plan_ids", normalized)
        if self.max_plans is not None and (
            type(self.max_plans) is not int or self.max_plans < 1
        ):
            raise ValueError("max_plans must be a positive integer")
        if not self.application_plan_ids and self.max_plans is None:
            raise ValueError(
                "a non-empty application_plan_ids or max_plans is required"
            )


@dataclass(frozen=True, slots=True)
class SelectiveBatchPlanResult:
    application_plan_id: str
    job_id: str | None
    selection_status: BatchPlanSelectionStatus
    execution_status: BatchPlanExecutionStatus
    preparation_run_id: str | None
    attention_item_ids: tuple[str, ...]
    reason_code: BatchPlanReasonCode | None
    source_reason_code: str | None

    def __post_init__(self) -> None:
        _clean_text(
            "application_plan_id", self.application_plan_id, maximum=180
        )
        selection = BatchPlanSelectionStatus(self.selection_status)
        execution = BatchPlanExecutionStatus(self.execution_status)
        object.__setattr__(self, "selection_status", selection)
        object.__setattr__(self, "execution_status", execution)
        if self.job_id is not None:
            _clean_text("job_id", self.job_id, maximum=160)
        if self.preparation_run_id is not None:
            _clean_text(
                "preparation_run_id", self.preparation_run_id, maximum=240
            )
        if not isinstance(self.attention_item_ids, tuple):
            raise TypeError("attention_item_ids must be a tuple")
        if len(set(self.attention_item_ids)) != len(
            self.attention_item_ids
        ):
            raise ValueError("attention item IDs must be unique")
        for item_id in self.attention_item_ids:
            _clean_text("attention_item_id", item_id, maximum=240)
        reason = (
            BatchPlanReasonCode(self.reason_code)
            if self.reason_code is not None
            else None
        )
        object.__setattr__(self, "reason_code", reason)
        if self.source_reason_code is not None:
            _clean_text(
                "source_reason_code", self.source_reason_code, maximum=240
            )
        if execution in {
            BatchPlanExecutionStatus.COMPLETED,
            BatchPlanExecutionStatus.UNCHANGED,
        }:
            if (
                selection is not BatchPlanSelectionStatus.SELECTED
                or self.preparation_run_id is None
                or reason is not None
                or self.attention_item_ids
            ):
                raise ValueError("successful per-plan result is invalid")
        elif execution is BatchPlanExecutionStatus.SKIPPED_HUMAN_ATTENTION:
            if (
                selection
                is not BatchPlanSelectionStatus.SKIPPED_HUMAN_ATTENTION
                or not self.attention_item_ids
                or reason is not BatchPlanReasonCode.CURRENT_HUMAN_ATTENTION
                or self.preparation_run_id is not None
            ):
                raise ValueError("attention-skipped result is invalid")
        elif execution is BatchPlanExecutionStatus.NOT_FOUND:
            if (
                selection is not BatchPlanSelectionStatus.NOT_FOUND
                or self.job_id is not None
                or self.preparation_run_id is not None
                or self.attention_item_ids
                or reason
                is not BatchPlanReasonCode.APPLICATION_PLAN_NOT_FOUND
            ):
                raise ValueError("not-found per-plan result is invalid")
        elif (
            selection is not BatchPlanSelectionStatus.SELECTED
            or reason is None
            or self.attention_item_ids
        ):
            raise ValueError("stopped per-plan result is invalid")


@dataclass(frozen=True, slots=True)
class SelectiveBatchPreparationSummary:
    requested: int
    selected: int
    completed: int
    unchanged: int
    deferred: int
    failed: int
    skipped_human_attention: int
    not_found: int

    def __post_init__(self) -> None:
        for name in (
            "requested",
            "selected",
            "completed",
            "unchanged",
            "deferred",
            "failed",
            "skipped_human_attention",
            "not_found",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.selected != (
            self.completed
            + self.unchanged
            + self.deferred
            + self.failed
        ):
            raise ValueError("selected plan count is inconsistent")


@dataclass(frozen=True, slots=True)
class SelectiveBatchPreparationResult:
    status: SelectiveBatchPreparationStatus
    subject_id: str
    evaluated_at: datetime
    queue_snapshot_hash: str | None
    items: tuple[SelectiveBatchPlanResult, ...]
    summary: SelectiveBatchPreparationSummary
    failure_reason: SelectiveBatchFailureReason | None

    def __post_init__(self) -> None:
        status = SelectiveBatchPreparationStatus(self.status)
        object.__setattr__(self, "status", status)
        _clean_text("subject_id", self.subject_id, 160)
        _require_aware("evaluated_at", self.evaluated_at)
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, SelectiveBatchPlanResult)
            for item in self.items
        ):
            raise TypeError("batch items must be typed")
        if len(
            {item.application_plan_id for item in self.items}
        ) != len(self.items):
            raise ValueError("batch items must contain unique plans")
        if not isinstance(self.summary, SelectiveBatchPreparationSummary):
            raise TypeError("batch summary must be typed")
        reason = (
            SelectiveBatchFailureReason(self.failure_reason)
            if self.failure_reason is not None
            else None
        )
        object.__setattr__(self, "failure_reason", reason)
        if status is SelectiveBatchPreparationStatus.FAILED and reason:
            if self.items or self.queue_snapshot_hash is not None:
                raise ValueError("fatal batch failure result is invalid")
        else:
            if reason is not None or self.queue_snapshot_hash is None:
                raise ValueError("non-fatal batch result is invalid")
            if self.summary != _summarize(
                self.summary.requested, self.items
            ) or status is not _overall(self.summary):
                raise ValueError("batch status or summary is inconsistent")


class HumanAttentionQueueReader(Protocol):
    def __call__(
        self, *, subject_id: str, now: datetime
    ) -> HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult]:
        """Read one fixed current-attention snapshot."""


class SingleJobPreparationCallable(Protocol):
    def __call__(
        self, command: RunApplicationPreparationCommand
    ) -> (
        RunApplicationPreparationResult
        | Awaitable[RunApplicationPreparationResult]
    ):
        """Run P2b4 for one existing plan."""


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _fatal(
    *,
    command: SelectiveBatchPreparationCommand,
    reason: SelectiveBatchFailureReason,
    requested: int = 0,
) -> SelectiveBatchPreparationResult:
    return SelectiveBatchPreparationResult(
        status=SelectiveBatchPreparationStatus.FAILED,
        subject_id=command.subject_id,
        evaluated_at=command.now,
        queue_snapshot_hash=None,
        items=(),
        summary=SelectiveBatchPreparationSummary(
            requested=requested,
            selected=0,
            completed=0,
            unchanged=0,
            deferred=0,
            failed=0,
            skipped_human_attention=0,
            not_found=0,
        ),
        failure_reason=reason,
    )


def _not_found(plan_id: str) -> SelectiveBatchPlanResult:
    return SelectiveBatchPlanResult(
        application_plan_id=plan_id,
        job_id=None,
        selection_status=BatchPlanSelectionStatus.NOT_FOUND,
        execution_status=BatchPlanExecutionStatus.NOT_FOUND,
        preparation_run_id=None,
        attention_item_ids=(),
        reason_code=BatchPlanReasonCode.APPLICATION_PLAN_NOT_FOUND,
        source_reason_code=None,
    )


def _skipped(
    plan: ApplicationPlan, attention_ids: tuple[str, ...]
) -> SelectiveBatchPlanResult:
    return SelectiveBatchPlanResult(
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        selection_status=BatchPlanSelectionStatus.SKIPPED_HUMAN_ATTENTION,
        execution_status=(
            BatchPlanExecutionStatus.SKIPPED_HUMAN_ATTENTION
        ),
        preparation_run_id=None,
        attention_item_ids=attention_ids,
        reason_code=BatchPlanReasonCode.CURRENT_HUMAN_ATTENTION,
        source_reason_code=None,
    )


def _from_preparation(
    plan: ApplicationPlan, value: Any
) -> SelectiveBatchPlanResult:
    if not isinstance(value, RunApplicationPreparationResult):
        return _invalid_preparation_result(plan)
    try:
        status = ApplicationPreparationStatus(value.status)
    except ValueError:
        return _invalid_preparation_result(plan)
    try:
        run_id = value.run.run_id if value.run is not None else None
        result_reason = (
            value.reason_code.value
            if value.reason_code is not None
            else None
        )
    except (AttributeError, TypeError, ValueError):
        return _invalid_preparation_result(plan)
    if status in {
        ApplicationPreparationStatus.COMPLETED,
        ApplicationPreparationStatus.UNCHANGED,
    }:
        if run_id is None:
            return _invalid_preparation_result(plan)
        return SelectiveBatchPlanResult(
            application_plan_id=plan.plan_id,
            job_id=plan.job_id,
            selection_status=BatchPlanSelectionStatus.SELECTED,
            execution_status=BatchPlanExecutionStatus(status.value),
            preparation_run_id=run_id,
            attention_item_ids=(),
            reason_code=None,
            source_reason_code=None,
        )
    return SelectiveBatchPlanResult(
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        selection_status=BatchPlanSelectionStatus.SELECTED,
        execution_status=BatchPlanExecutionStatus(status.value),
        preparation_run_id=run_id,
        attention_item_ids=(),
        reason_code=(
            BatchPlanReasonCode.SINGLE_JOB_DEFERRED
            if status is ApplicationPreparationStatus.DEFERRED
            else BatchPlanReasonCode.SINGLE_JOB_FAILED
        ),
        source_reason_code=(
            getattr(value.run, "deferred_reason", None)
            if status is ApplicationPreparationStatus.DEFERRED
            else getattr(value.run, "failed_reason", None)
        )
        or result_reason
        or status.value,
    )


def _invalid_preparation_result(
    plan: ApplicationPlan,
) -> SelectiveBatchPlanResult:
    return SelectiveBatchPlanResult(
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        selection_status=BatchPlanSelectionStatus.SELECTED,
        execution_status=BatchPlanExecutionStatus.FAILED,
        preparation_run_id=None,
        attention_item_ids=(),
        reason_code=BatchPlanReasonCode.SINGLE_JOB_RESULT_INVALID,
        source_reason_code="PUBLIC_P2B4_RESULT_INVALID",
    )


def _preparation_exception(
    plan: ApplicationPlan,
) -> SelectiveBatchPlanResult:
    return SelectiveBatchPlanResult(
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        selection_status=BatchPlanSelectionStatus.SELECTED,
        execution_status=BatchPlanExecutionStatus.FAILED,
        preparation_run_id=None,
        attention_item_ids=(),
        reason_code=BatchPlanReasonCode.SINGLE_JOB_EXCEPTION,
        source_reason_code="PUBLIC_P2B4_EXCEPTION",
    )


def _summarize(
    requested: int, items: tuple[SelectiveBatchPlanResult, ...]
) -> SelectiveBatchPreparationSummary:
    return SelectiveBatchPreparationSummary(
        requested=requested,
        selected=sum(
            item.selection_status is BatchPlanSelectionStatus.SELECTED
            for item in items
        ),
        completed=sum(
            item.execution_status is BatchPlanExecutionStatus.COMPLETED
            for item in items
        ),
        unchanged=sum(
            item.execution_status is BatchPlanExecutionStatus.UNCHANGED
            for item in items
        ),
        deferred=sum(
            item.execution_status is BatchPlanExecutionStatus.DEFERRED
            for item in items
        ),
        failed=sum(
            item.execution_status is BatchPlanExecutionStatus.FAILED
            for item in items
        ),
        skipped_human_attention=sum(
            item.execution_status
            is BatchPlanExecutionStatus.SKIPPED_HUMAN_ATTENTION
            for item in items
        ),
        not_found=sum(
            item.execution_status is BatchPlanExecutionStatus.NOT_FOUND
            for item in items
        ),
    )


def _overall(
    summary: SelectiveBatchPreparationSummary,
) -> SelectiveBatchPreparationStatus:
    if summary.selected == 0:
        return SelectiveBatchPreparationStatus.NOOP
    successes = summary.completed + summary.unchanged
    stopped = summary.deferred + summary.failed
    if stopped == 0:
        return SelectiveBatchPreparationStatus.COMPLETED
    if successes:
        return SelectiveBatchPreparationStatus.PARTIAL_FAILURE
    return SelectiveBatchPreparationStatus.FAILED


async def run_selective_batch_preparation(
    command: SelectiveBatchPreparationCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    human_attention_queue_reader: HumanAttentionQueueReader,
    single_job_preparation: SingleJobPreparationCallable,
) -> SelectiveBatchPreparationResult:
    """Take one attention snapshot, then run eligible plans serially."""

    if not isinstance(command, SelectiveBatchPreparationCommand):
        raise TypeError("command must be a SelectiveBatchPreparationCommand")
    requested_ids = (
        _deduplicate(command.application_plan_ids)
        if command.application_plan_ids
        else None
    )
    requested_count = len(requested_ids) if requested_ids is not None else 0

    try:
        queue = await _resolve(
            human_attention_queue_reader(
                subject_id=command.subject_id,
                now=command.now,
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _fatal(
            command=command,
            reason=SelectiveBatchFailureReason.HUMAN_ATTENTION_QUEUE_FAILED,
            requested=requested_count,
        )
    if not isinstance(queue, HumanAttentionQueueResult):
        return _fatal(
            command=command,
            reason=(
                SelectiveBatchFailureReason
                .HUMAN_ATTENTION_QUEUE_RESULT_INVALID
            ),
            requested=requested_count,
        )
    if (
        queue.status is not HumanAttentionQueueStatus.SUCCEEDED
        or queue.subject_id != command.subject_id
        or queue.evaluated_at != command.now
    ):
        return _fatal(
            command=command,
            reason=SelectiveBatchFailureReason.HUMAN_ATTENTION_QUEUE_FAILED,
            requested=requested_count,
        )

    explicit = requested_ids is not None
    if explicit:
        candidate_ids = requested_ids
    else:
        try:
            listed = application_plan_repository.list_for_subject(
                command.subject_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _fatal(
                command=command,
                reason=(
                    SelectiveBatchFailureReason
                    .APPLICATION_PLAN_LIST_FAILED
                ),
            )
        if (
            not isinstance(listed, ApplicationPlanListResult)
            or listed.status is not ApplicationPlanListStatus.SUCCEEDED
        ):
            return _fatal(
                command=command,
                reason=(
                    SelectiveBatchFailureReason
                    .APPLICATION_PLAN_LIST_FAILED
                ),
            )
        candidate_ids = tuple(plan.plan_id for plan in listed.plans)
        requested_count = len(candidate_ids)

    attention_by_plan: dict[str, list[str]] = {}
    for item in queue.items:
        attention_by_plan.setdefault(item.application_plan_id, []).append(
            item.item_id
        )

    results: list[SelectiveBatchPlanResult] = []
    selected = 0
    for plan_id in candidate_ids:
        if command.max_plans is not None and selected >= command.max_plans:
            break
        try:
            read = application_plan_repository.get(plan_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _fatal(
                command=command,
                reason=(
                    SelectiveBatchFailureReason
                    .APPLICATION_PLAN_REPOSITORY_FAILURE
                ),
                requested=requested_count,
            )
        if read.status is ApplicationPlanReadStatus.NOT_FOUND:
            results.append(_not_found(plan_id))
            continue
        if (
            read.status is not ApplicationPlanReadStatus.FOUND
            or read.plan is None
        ):
            return _fatal(
                command=command,
                reason=(
                    SelectiveBatchFailureReason
                    .APPLICATION_PLAN_REPOSITORY_FAILURE
                ),
                requested=requested_count,
            )
        plan = read.plan
        if plan.subject_id != command.subject_id:
            results.append(_not_found(plan_id))
            continue
        attention_ids = tuple(attention_by_plan.get(plan.plan_id, ()))
        if attention_ids:
            results.append(_skipped(plan, attention_ids))
            continue

        selected += 1
        preparation_command = RunApplicationPreparationCommand(
            subject_id=command.subject_id,
            application_plan_id=plan.plan_id,
            now=command.now,
        )
        try:
            public_result = await _resolve(
                single_job_preparation(preparation_command)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            results.append(_preparation_exception(plan))
            continue
        results.append(_from_preparation(plan, public_result))

    typed_items = tuple(results)
    summary = _summarize(requested_count, typed_items)
    return SelectiveBatchPreparationResult(
        status=_overall(summary),
        subject_id=command.subject_id,
        evaluated_at=command.now,
        queue_snapshot_hash=queue.queue_snapshot_hash,
        items=typed_items,
        summary=summary,
        failure_reason=None,
    )


__all__ = [
    "SELECTIVE_BATCH_PREPARATION_CONTRACT_VERSION",
    "BatchPlanExecutionStatus",
    "BatchPlanReasonCode",
    "BatchPlanSelectionStatus",
    "HumanAttentionQueueReader",
    "SelectiveBatchFailureReason",
    "SelectiveBatchPlanResult",
    "SelectiveBatchPreparationCommand",
    "SelectiveBatchPreparationResult",
    "SelectiveBatchPreparationStatus",
    "SelectiveBatchPreparationSummary",
    "SingleJobPreparationCallable",
    "run_selective_batch_preparation",
]
