"""Bounded serial ApplicationPlan creation from one runnable queue snapshot."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from .application_plan import (
    CreateApplicationPlanCommand,
    CreateApplicationPlanResult,
    CreateApplicationPlanStatus,
)
from .runnable_application_queue import (
    RunnableApplicationQueueCommand,
    RunnableApplicationQueueResult,
    RunnableApplicationQueueStatus,
    RunnableApplicationStatus,
)


SELECTIVE_BATCH_PLAN_CREATION_CONTRACT_VERSION = (
    "selective-batch-application-plan-creation-v1"
)


def _clean(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the batch contract")
    return cleaned


def _aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value


class SelectiveBatchPlanCreationStatus(StrEnum):
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class BatchPlanCreationStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    SKIPPED_NOT_RUNNABLE = "SKIPPED_NOT_RUNNABLE"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"


class BatchPlanCreationReason(StrEnum):
    QUEUE_NOT_RUNNABLE = "QUEUE_NOT_RUNNABLE"
    JOB_NOT_IN_SNAPSHOT = "JOB_NOT_IN_SNAPSHOT"
    SINGLE_JOB_NOT_RUNNABLE = "SINGLE_JOB_NOT_RUNNABLE"
    SINGLE_JOB_FAILED = "SINGLE_JOB_FAILED"
    SINGLE_JOB_EXCEPTION = "SINGLE_JOB_EXCEPTION"
    SINGLE_JOB_RESULT_INVALID = "SINGLE_JOB_RESULT_INVALID"


class SelectiveBatchPlanCreationFailureReason(StrEnum):
    QUEUE_READER_FAILED = "QUEUE_READER_FAILED"
    QUEUE_RESULT_INVALID = "QUEUE_RESULT_INVALID"


@dataclass(frozen=True, slots=True)
class BatchJobPreparationInstructions:
    job_id: str
    user_preparation_instructions: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _clean("job_id", self.job_id, 160))
        if not isinstance(self.user_preparation_instructions, str):
            raise TypeError("user_preparation_instructions must be a string")


@dataclass(frozen=True, slots=True)
class SelectiveBatchPlanCreationCommand:
    subject_id: str
    now: datetime
    job_ids: tuple[str, ...] | None = None
    max_jobs: int | None = None
    job_instructions: tuple[BatchJobPreparationInstructions, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean("subject_id", self.subject_id, 160)
        )
        _aware(self.now)
        if self.job_ids is not None:
            if not isinstance(self.job_ids, tuple):
                raise TypeError("job_ids must be a tuple")
            object.__setattr__(
                self,
                "job_ids",
                tuple(_clean("job_id", item, 160) for item in self.job_ids),
            )
        if self.max_jobs is not None and (
            type(self.max_jobs) is not int or self.max_jobs < 1
        ):
            raise ValueError("max_jobs must be a positive integer")
        if not self.job_ids and self.max_jobs is None:
            raise ValueError("a non-empty job_ids or max_jobs is required")
        if not isinstance(self.job_instructions, tuple) or any(
            not isinstance(item, BatchJobPreparationInstructions)
            for item in self.job_instructions
        ):
            raise TypeError("job_instructions must contain typed entries")
        if len({item.job_id for item in self.job_instructions}) != len(
            self.job_instructions
        ):
            raise ValueError("job_instructions must contain unique jobs")


@dataclass(frozen=True, slots=True)
class SelectiveBatchPlanCreationItem:
    job_id: str
    runnable_queue_status: RunnableApplicationStatus | None
    creation_status: BatchPlanCreationStatus
    application_plan_id: str | None
    creation_attempted: bool
    reason: BatchPlanCreationReason | None
    source_reason: str | None

    def __post_init__(self) -> None:
        _clean("job_id", self.job_id, 160)
        if self.runnable_queue_status is not None:
            object.__setattr__(
                self,
                "runnable_queue_status",
                RunnableApplicationStatus(self.runnable_queue_status),
            )
        object.__setattr__(
            self, "creation_status", BatchPlanCreationStatus(self.creation_status)
        )
        if self.application_plan_id is not None:
            _clean("application_plan_id", self.application_plan_id, 180)
        if type(self.creation_attempted) is not bool:
            raise TypeError("creation_attempted must be boolean")
        if self.reason is not None:
            object.__setattr__(
                self, "reason", BatchPlanCreationReason(self.reason)
            )
        if self.source_reason is not None:
            _clean("source_reason", self.source_reason)
        if self.creation_status in {
            BatchPlanCreationStatus.CREATED,
            BatchPlanCreationStatus.UNCHANGED,
        }:
            if (
                self.runnable_queue_status is not RunnableApplicationStatus.RUNNABLE
                or not self.creation_attempted
                or self.application_plan_id is None
                or self.reason is not None
            ):
                raise ValueError("successful batch plan item is malformed")
        elif self.creation_status is BatchPlanCreationStatus.NOT_FOUND:
            if (
                self.runnable_queue_status is not None
                or self.creation_attempted
                or self.application_plan_id is not None
                or self.reason is not BatchPlanCreationReason.JOB_NOT_IN_SNAPSHOT
            ):
                raise ValueError("not-found batch plan item is malformed")
        elif self.creation_status is BatchPlanCreationStatus.SKIPPED_NOT_RUNNABLE:
            if (
                self.application_plan_id is not None
                or self.reason
                not in {
                    BatchPlanCreationReason.QUEUE_NOT_RUNNABLE,
                    BatchPlanCreationReason.SINGLE_JOB_NOT_RUNNABLE,
                }
                or (
                    not self.creation_attempted
                    and (
                        self.runnable_queue_status is None
                        or self.runnable_queue_status
                        is RunnableApplicationStatus.RUNNABLE
                    )
                )
            ):
                raise ValueError("not-runnable batch plan item is malformed")
        elif (
            not self.creation_attempted
            or self.runnable_queue_status is not RunnableApplicationStatus.RUNNABLE
            or self.application_plan_id is not None
            or self.reason is None
        ):
            raise ValueError("failed batch plan item is malformed")


@dataclass(frozen=True, slots=True)
class SelectiveBatchPlanCreationSummary:
    requested: int
    selected: int
    created: int
    unchanged: int
    skipped_not_runnable: int
    not_found: int
    failed: int

    def __post_init__(self) -> None:
        for name in (
            "requested",
            "selected",
            "created",
            "unchanged",
            "skipped_not_runnable",
            "not_found",
            "failed",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class SelectiveBatchPlanCreationResult:
    status: SelectiveBatchPlanCreationStatus
    subject_id: str
    evaluated_at: datetime
    items: tuple[SelectiveBatchPlanCreationItem, ...]
    summary: SelectiveBatchPlanCreationSummary
    failure_reason: SelectiveBatchPlanCreationFailureReason | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", SelectiveBatchPlanCreationStatus(self.status)
        )
        _clean("subject_id", self.subject_id, 160)
        _aware(self.evaluated_at)
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, SelectiveBatchPlanCreationItem)
            for item in self.items
        ):
            raise TypeError("batch plan items must be typed")
        if len({item.job_id for item in self.items}) != len(self.items):
            raise ValueError("batch plan items must be unique")
        if not isinstance(self.summary, SelectiveBatchPlanCreationSummary):
            raise TypeError("batch summary must be typed")
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                SelectiveBatchPlanCreationFailureReason(self.failure_reason),
            )
        if self.status is SelectiveBatchPlanCreationStatus.FAILED and (
            self.failure_reason is not None
        ):
            if self.items:
                raise ValueError("fatal batch plan result is malformed")
        elif (
            self.failure_reason is not None
            or self.summary != _summarize(self.summary.requested, self.items)
            or self.status is not _overall(self.summary)
        ):
            raise ValueError("batch plan result is inconsistent")


class RunnableQueueReader(Protocol):
    def __call__(
        self, command: RunnableApplicationQueueCommand
    ) -> (
        RunnableApplicationQueueResult
        | Awaitable[RunnableApplicationQueueResult]
    ): ...


class SingleJobPlanCreator(Protocol):
    def __call__(
        self, command: CreateApplicationPlanCommand
    ) -> CreateApplicationPlanResult | Awaitable[CreateApplicationPlanResult]: ...


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _summary_empty(requested: int) -> SelectiveBatchPlanCreationSummary:
    return SelectiveBatchPlanCreationSummary(
        requested=requested,
        selected=0,
        created=0,
        unchanged=0,
        skipped_not_runnable=0,
        not_found=0,
        failed=0,
    )


def _fatal(
    command: SelectiveBatchPlanCreationCommand,
    reason: SelectiveBatchPlanCreationFailureReason,
    requested: int,
) -> SelectiveBatchPlanCreationResult:
    return SelectiveBatchPlanCreationResult(
        status=SelectiveBatchPlanCreationStatus.FAILED,
        subject_id=command.subject_id,
        evaluated_at=command.now,
        items=(),
        summary=_summary_empty(requested),
        failure_reason=reason,
    )


def _not_found(job_id: str) -> SelectiveBatchPlanCreationItem:
    return SelectiveBatchPlanCreationItem(
        job_id=job_id,
        runnable_queue_status=None,
        creation_status=BatchPlanCreationStatus.NOT_FOUND,
        application_plan_id=None,
        creation_attempted=False,
        reason=BatchPlanCreationReason.JOB_NOT_IN_SNAPSHOT,
        source_reason=None,
    )


def _queue_skip(item: Any) -> SelectiveBatchPlanCreationItem:
    return SelectiveBatchPlanCreationItem(
        job_id=item.job.job_id,
        runnable_queue_status=item.runnable_status,
        creation_status=BatchPlanCreationStatus.SKIPPED_NOT_RUNNABLE,
        application_plan_id=None,
        creation_attempted=False,
        reason=BatchPlanCreationReason.QUEUE_NOT_RUNNABLE,
        source_reason=item.reasons[0].value,
    )


def _failed(
    item: Any,
    reason: BatchPlanCreationReason,
) -> SelectiveBatchPlanCreationItem:
    return SelectiveBatchPlanCreationItem(
        job_id=item.job.job_id,
        runnable_queue_status=item.runnable_status,
        creation_status=BatchPlanCreationStatus.FAILED,
        application_plan_id=None,
        creation_attempted=True,
        reason=reason,
        source_reason=reason.value,
    )


def _from_creator(item: Any, result: Any) -> SelectiveBatchPlanCreationItem:
    if not isinstance(result, CreateApplicationPlanResult):
        return _failed(item, BatchPlanCreationReason.SINGLE_JOB_RESULT_INVALID)
    try:
        status = CreateApplicationPlanStatus(result.status)
        if result.subject_id != item.subject_id or result.job_id != item.job.job_id:
            return _failed(item, BatchPlanCreationReason.SINGLE_JOB_RESULT_INVALID)
        if status in {
            CreateApplicationPlanStatus.CREATED,
            CreateApplicationPlanStatus.UNCHANGED,
        }:
            if result.plan is None:
                return _failed(
                    item, BatchPlanCreationReason.SINGLE_JOB_RESULT_INVALID
                )
            return SelectiveBatchPlanCreationItem(
                job_id=item.job.job_id,
                runnable_queue_status=item.runnable_status,
                creation_status=BatchPlanCreationStatus(status.value),
                application_plan_id=result.plan.plan_id,
                creation_attempted=True,
                reason=None,
                source_reason=None,
            )
        if status is CreateApplicationPlanStatus.NOT_RUNNABLE:
            return SelectiveBatchPlanCreationItem(
                job_id=item.job.job_id,
                runnable_queue_status=item.runnable_status,
                creation_status=BatchPlanCreationStatus.SKIPPED_NOT_RUNNABLE,
                application_plan_id=None,
                creation_attempted=True,
                reason=BatchPlanCreationReason.SINGLE_JOB_NOT_RUNNABLE,
                source_reason=(
                    result.reason_code.value
                    if result.reason_code is not None
                    else status.value
                ),
            )
        return SelectiveBatchPlanCreationItem(
            job_id=item.job.job_id,
            runnable_queue_status=item.runnable_status,
            creation_status=BatchPlanCreationStatus.FAILED,
            application_plan_id=None,
            creation_attempted=True,
            reason=BatchPlanCreationReason.SINGLE_JOB_FAILED,
            source_reason=(
                result.reason_code.value
                if result.reason_code is not None
                else status.value
            ),
        )
    except (AttributeError, TypeError, ValueError):
        return _failed(item, BatchPlanCreationReason.SINGLE_JOB_RESULT_INVALID)


def _summarize(
    requested: int,
    items: tuple[SelectiveBatchPlanCreationItem, ...],
) -> SelectiveBatchPlanCreationSummary:
    return SelectiveBatchPlanCreationSummary(
        requested=requested,
        selected=sum(item.creation_attempted for item in items),
        created=sum(
            item.creation_status is BatchPlanCreationStatus.CREATED
            for item in items
        ),
        unchanged=sum(
            item.creation_status is BatchPlanCreationStatus.UNCHANGED
            for item in items
        ),
        skipped_not_runnable=sum(
            item.creation_status
            is BatchPlanCreationStatus.SKIPPED_NOT_RUNNABLE
            for item in items
        ),
        not_found=sum(
            item.creation_status is BatchPlanCreationStatus.NOT_FOUND
            for item in items
        ),
        failed=sum(
            item.creation_status is BatchPlanCreationStatus.FAILED
            for item in items
        ),
    )


def _overall(
    summary: SelectiveBatchPlanCreationSummary,
) -> SelectiveBatchPlanCreationStatus:
    if summary.selected == 0:
        return SelectiveBatchPlanCreationStatus.NOOP
    succeeded = summary.created + summary.unchanged
    stopped = summary.selected - succeeded
    if stopped == 0:
        return SelectiveBatchPlanCreationStatus.COMPLETED
    if succeeded:
        return SelectiveBatchPlanCreationStatus.PARTIAL_FAILURE
    return SelectiveBatchPlanCreationStatus.FAILED


async def run_selective_batch_plan_creation(
    command: SelectiveBatchPlanCreationCommand,
    *,
    runnable_queue_reader: RunnableQueueReader,
    single_job_plan_creator: SingleJobPlanCreator,
) -> SelectiveBatchPlanCreationResult:
    """Read P1d4 once and invoke P2a1 serially for RUNNABLE jobs."""

    if not isinstance(command, SelectiveBatchPlanCreationCommand):
        raise TypeError(
            "command must be a SelectiveBatchPlanCreationCommand"
        )
    requested_ids = (
        _deduplicate(command.job_ids) if command.job_ids else None
    )
    requested = len(requested_ids) if requested_ids is not None else 0
    try:
        queue = await _resolve(
            runnable_queue_reader(
                RunnableApplicationQueueCommand(
                    subject_id=command.subject_id,
                    now=command.now,
                )
            )
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _fatal(
            command,
            SelectiveBatchPlanCreationFailureReason.QUEUE_READER_FAILED,
            requested,
        )
    if not isinstance(queue, RunnableApplicationQueueResult):
        return _fatal(
            command,
            SelectiveBatchPlanCreationFailureReason.QUEUE_RESULT_INVALID,
            requested,
        )
    if (
        queue.status is not RunnableApplicationQueueStatus.SUCCEEDED
        or queue.subject_id != command.subject_id
        or queue.now != command.now
        or len({item.job.job_id for item in queue.items}) != len(queue.items)
    ):
        return _fatal(
            command,
            SelectiveBatchPlanCreationFailureReason.QUEUE_RESULT_INVALID,
            requested,
        )
    by_job = {item.job.job_id: item for item in queue.items}
    explicit = requested_ids is not None
    if explicit:
        candidate_ids = requested_ids
    else:
        candidate_ids = tuple(
            item.job.job_id for item in queue.runnable_items
        )
        requested = len(candidate_ids)
    instructions = {
        item.job_id: item.user_preparation_instructions
        for item in command.job_instructions
    }
    results: list[SelectiveBatchPlanCreationItem] = []
    selected = 0
    for job_id in candidate_ids:
        if command.max_jobs is not None and selected >= command.max_jobs:
            break
        item = by_job.get(job_id)
        if item is None:
            results.append(_not_found(job_id))
            continue
        if item.runnable_status is not RunnableApplicationStatus.RUNNABLE:
            results.append(_queue_skip(item))
            continue
        selected += 1
        create_command = CreateApplicationPlanCommand(
            subject_id=command.subject_id,
            job_id=job_id,
            now=command.now,
            user_preparation_instructions=instructions.get(job_id),
        )
        try:
            public_result = await _resolve(
                single_job_plan_creator(create_command)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            results.append(
                _failed(item, BatchPlanCreationReason.SINGLE_JOB_EXCEPTION)
            )
            continue
        results.append(_from_creator(item, public_result))
    typed_items = tuple(results)
    summary = _summarize(requested, typed_items)
    return SelectiveBatchPlanCreationResult(
        status=_overall(summary),
        subject_id=command.subject_id,
        evaluated_at=command.now,
        items=typed_items,
        summary=summary,
        failure_reason=None,
    )


__all__ = [
    "SELECTIVE_BATCH_PLAN_CREATION_CONTRACT_VERSION",
    "BatchJobPreparationInstructions",
    "BatchPlanCreationReason",
    "BatchPlanCreationStatus",
    "RunnableQueueReader",
    "SelectiveBatchPlanCreationCommand",
    "SelectiveBatchPlanCreationFailureReason",
    "SelectiveBatchPlanCreationItem",
    "SelectiveBatchPlanCreationResult",
    "SelectiveBatchPlanCreationStatus",
    "SelectiveBatchPlanCreationSummary",
    "SingleJobPlanCreator",
    "run_selective_batch_plan_creation",
]
