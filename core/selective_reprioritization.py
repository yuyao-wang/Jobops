"""Bounded serial orchestration of existing P1d2 and P1d1 services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .current_priority_queue import (
    CurrentPriorityItemStatus,
    CurrentPriorityQueueCommand,
    CurrentPriorityQueueItem,
    CurrentPriorityQueueResult,
    CurrentPriorityQueueStatus,
)
from .single_job_priority import (
    SingleJobPriorityChange,
    SingleJobPriorityCommand,
    SingleJobPriorityReason,
    SingleJobPriorityResult,
    SingleJobPriorityStatus,
)


_QueueReader = Callable[
    [CurrentPriorityQueueCommand],
    Awaitable[CurrentPriorityQueueResult],
]
_SingleJobOrchestrator = Callable[
    [SingleJobPriorityCommand],
    Awaitable[SingleJobPriorityResult],
]


class SelectiveBatchOverallStatus(str, Enum):
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class SelectiveBatchExecutionStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    SKIPPED_CURRENT = "SKIPPED_CURRENT"
    SKIPPED_INCOMPLETE = "SKIPPED_INCOMPLETE"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"


class SelectiveBatchReason(str, Enum):
    INVALID_COMMAND = "INVALID_COMMAND"
    QUEUE_BUILD_FAILED = "QUEUE_BUILD_FAILED"
    QUEUE_RESULT_INVALID = "QUEUE_RESULT_INVALID"
    ITEM_FAILURE = "ITEM_FAILURE"
    ALL_EXECUTIONS_FAILED = "ALL_EXECUTIONS_FAILED"


class SelectiveBatchItemReason(str, Enum):
    JOB_NOT_IN_QUEUE = "JOB_NOT_IN_QUEUE"
    SINGLE_JOB_FAILED = "SINGLE_JOB_FAILED"
    SINGLE_JOB_RESULT_INVALID = "SINGLE_JOB_RESULT_INVALID"


@dataclass(frozen=True, slots=True)
class SelectiveBatchReprioritizationCommand:
    subject_id: str
    now: datetime
    job_ids: tuple[str, ...] | None = None
    max_jobs: int | None = None


@dataclass(frozen=True, slots=True)
class PriorityQueueSnapshotSummary:
    total: int
    current: int
    stale: int
    missing: int
    incomplete: int

    def __post_init__(self) -> None:
        values = (
            self.total,
            self.current,
            self.stale,
            self.missing,
            self.incomplete,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in values
        ):
            raise ValueError("queue snapshot counts must be non-negative")
        if self.total != sum(values[1:]):
            raise ValueError("queue snapshot counts are inconsistent")


@dataclass(frozen=True, slots=True)
class SelectiveBatchItemFailure:
    reason_code: SelectiveBatchItemReason
    retryable: bool
    message: str
    single_job_reason: SingleJobPriorityReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reason_code",
            SelectiveBatchItemReason(self.reason_code),
        )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("failure message must be non-empty")
        if self.single_job_reason is not None:
            object.__setattr__(
                self,
                "single_job_reason",
                SingleJobPriorityReason(self.single_job_reason),
            )
        if (
            self.reason_code
            is SelectiveBatchItemReason.SINGLE_JOB_FAILED
            and self.single_job_reason is None
        ):
            raise ValueError("single-job failure must retain its reason")
        if (
            self.reason_code
            is not SelectiveBatchItemReason.SINGLE_JOB_FAILED
            and self.single_job_reason is not None
        ):
            raise ValueError("unexpected single-job failure reason")


@dataclass(frozen=True, slots=True)
class SelectiveBatchReprioritizationItem:
    job_id: str
    queue_status: CurrentPriorityItemStatus | None
    execution_status: SelectiveBatchExecutionStatus
    single_job_result: SingleJobPriorityResult | None
    failure: SelectiveBatchItemFailure | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.job_id, str)
            or not self.job_id
            or self.job_id.strip() != self.job_id
        ):
            raise ValueError("batch item job_id must be canonical")
        if self.queue_status is not None:
            object.__setattr__(
                self,
                "queue_status",
                CurrentPriorityItemStatus(self.queue_status),
            )
        object.__setattr__(
            self,
            "execution_status",
            SelectiveBatchExecutionStatus(self.execution_status),
        )

        if self.execution_status is SelectiveBatchExecutionStatus.CREATED:
            self._validate_success(SingleJobPriorityChange.CREATED)
        elif (
            self.execution_status
            is SelectiveBatchExecutionStatus.UNCHANGED
        ):
            self._validate_success(SingleJobPriorityChange.UNCHANGED)
        elif (
            self.execution_status
            is SelectiveBatchExecutionStatus.SKIPPED_CURRENT
        ):
            self._validate_skip(CurrentPriorityItemStatus.CURRENT)
        elif (
            self.execution_status
            is SelectiveBatchExecutionStatus.SKIPPED_INCOMPLETE
        ):
            self._validate_skip(CurrentPriorityItemStatus.INCOMPLETE)
        elif self.execution_status is SelectiveBatchExecutionStatus.NOT_FOUND:
            if (
                self.queue_status is not None
                or self.single_job_result is not None
                or self.failure is None
                or self.failure.reason_code
                is not SelectiveBatchItemReason.JOB_NOT_IN_QUEUE
            ):
                raise ValueError("NOT_FOUND batch item is invalid")
        elif (
            self.queue_status
            not in (
                CurrentPriorityItemStatus.STALE,
                CurrentPriorityItemStatus.MISSING,
            )
            or self.failure is None
        ):
            raise ValueError("FAILED batch item is invalid")

    def _validate_success(
        self,
        expected_change: SingleJobPriorityChange,
    ) -> None:
        result = self.single_job_result
        if (
            self.queue_status
            not in (
                CurrentPriorityItemStatus.STALE,
                CurrentPriorityItemStatus.MISSING,
            )
            or result is None
            or result.status is not SingleJobPriorityStatus.SUCCEEDED
            or result.change is not expected_change
            or result.job_id != self.job_id
            or self.failure is not None
        ):
            raise ValueError("successful batch item is invalid")

    def _validate_skip(
        self,
        expected_status: CurrentPriorityItemStatus,
    ) -> None:
        if (
            self.queue_status is not expected_status
            or self.single_job_result is not None
            or self.failure is not None
        ):
            raise ValueError("skipped batch item is invalid")


@dataclass(frozen=True, slots=True)
class SelectiveBatchSummary:
    requested: int
    selected: int
    created: int
    unchanged: int
    skipped_current: int
    skipped_incomplete: int
    not_found: int
    failed: int

    def __post_init__(self) -> None:
        values = (
            self.requested,
            self.selected,
            self.created,
            self.unchanged,
            self.skipped_current,
            self.skipped_incomplete,
            self.not_found,
            self.failed,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in values
        ):
            raise ValueError("batch summary counts must be non-negative")
        if self.selected != self.created + self.unchanged + self.failed:
            raise ValueError("selected count is inconsistent")
        if self.requested != (
            self.selected
            + self.skipped_current
            + self.skipped_incomplete
            + self.not_found
        ):
            raise ValueError("requested count is inconsistent")


@dataclass(frozen=True, slots=True)
class SelectiveBatchReprioritizationResult:
    overall_status: SelectiveBatchOverallStatus
    reason_code: SelectiveBatchReason | None
    retryable: bool
    subject_id: str
    now: datetime | None
    requested_job_ids: tuple[str, ...]
    max_jobs: int | None
    queue_snapshot: PriorityQueueSnapshotSummary | None
    queue_failure: CurrentPriorityQueueResult | None
    items: tuple[SelectiveBatchReprioritizationItem, ...]
    summary: SelectiveBatchSummary
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "overall_status",
            SelectiveBatchOverallStatus(self.overall_status),
        )
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                SelectiveBatchReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if not isinstance(self.requested_job_ids, tuple) or len(
            self.requested_job_ids
        ) != len(set(self.requested_job_ids)):
            raise ValueError("requested job IDs must be a unique tuple")
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, SelectiveBatchReprioritizationItem)
            for item in self.items
        ):
            raise TypeError("batch items must be typed")
        if self.summary.requested != len(self.items):
            raise ValueError("batch result item count is inconsistent")

        if self.queue_failure is not None:
            if (
                self.queue_failure.status
                is not CurrentPriorityQueueStatus.FAILED
                or self.queue_snapshot is not None
                or self.items
                or self.overall_status
                is not SelectiveBatchOverallStatus.FAILED
                or self.reason_code
                is not SelectiveBatchReason.QUEUE_BUILD_FAILED
            ):
                raise ValueError("queue failure result is invalid")
            return
        if self.reason_code is SelectiveBatchReason.INVALID_COMMAND:
            if (
                self.queue_snapshot is not None
                or self.items
                or self.overall_status
                is not SelectiveBatchOverallStatus.FAILED
            ):
                raise ValueError("invalid-command result is invalid")
            return
        if self.reason_code is SelectiveBatchReason.QUEUE_RESULT_INVALID:
            if (
                self.queue_snapshot is not None
                or self.items
                or self.overall_status
                is not SelectiveBatchOverallStatus.FAILED
            ):
                raise ValueError("invalid queue result is invalid")
            return
        if self.queue_snapshot is None:
            raise ValueError("successful queue read requires a snapshot")

        expected_status, expected_reason = _overall_outcome(self.summary)
        if (
            self.overall_status is not expected_status
            or self.reason_code is not expected_reason
        ):
            raise ValueError("batch overall outcome is inconsistent")


def _empty_summary() -> SelectiveBatchSummary:
    return SelectiveBatchSummary(
        requested=0,
        selected=0,
        created=0,
        unchanged=0,
        skipped_current=0,
        skipped_incomplete=0,
        not_found=0,
        failed=0,
    )


def _clean_id(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 160:
        raise ValueError(f"{name} is outside the batch contract")
    return cleaned


def _require_aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value


def _normalize_command(
    command: SelectiveBatchReprioritizationCommand,
) -> tuple[str, datetime, tuple[str, ...], int | None]:
    subject_id = _clean_id("subject_id", command.subject_id)
    now = _require_aware(command.now)
    if command.max_jobs is not None and (
        isinstance(command.max_jobs, bool)
        or not isinstance(command.max_jobs, int)
        or command.max_jobs < 1
    ):
        raise ValueError("max_jobs must be a positive integer")
    if command.job_ids is not None and not isinstance(
        command.job_ids,
        tuple,
    ):
        raise TypeError("job_ids must be a tuple or None")
    ordered_ids: list[str] = []
    seen: set[str] = set()
    for raw_job_id in command.job_ids or ():
        job_id = _clean_id("job_id", raw_job_id)
        if job_id not in seen:
            ordered_ids.append(job_id)
            seen.add(job_id)
    if not ordered_ids and command.max_jobs is None:
        raise ValueError("non-empty job_ids or max_jobs is required")
    return subject_id, now, tuple(ordered_ids), command.max_jobs


def _snapshot_summary(
    result: CurrentPriorityQueueResult,
) -> PriorityQueueSnapshotSummary:
    counts = {
        status: 0
        for status in CurrentPriorityItemStatus
    }
    for item in result.items:
        counts[item.status] += 1
    return PriorityQueueSnapshotSummary(
        total=len(result.items),
        current=counts[CurrentPriorityItemStatus.CURRENT],
        stale=counts[CurrentPriorityItemStatus.STALE],
        missing=counts[CurrentPriorityItemStatus.MISSING],
        incomplete=counts[CurrentPriorityItemStatus.INCOMPLETE],
    )


def _summary_from_items(
    items: tuple[SelectiveBatchReprioritizationItem, ...],
) -> SelectiveBatchSummary:
    counts = {
        status: 0
        for status in SelectiveBatchExecutionStatus
    }
    for item in items:
        counts[item.execution_status] += 1
    return SelectiveBatchSummary(
        requested=len(items),
        selected=(
            counts[SelectiveBatchExecutionStatus.CREATED]
            + counts[SelectiveBatchExecutionStatus.UNCHANGED]
            + counts[SelectiveBatchExecutionStatus.FAILED]
        ),
        created=counts[SelectiveBatchExecutionStatus.CREATED],
        unchanged=counts[SelectiveBatchExecutionStatus.UNCHANGED],
        skipped_current=counts[
            SelectiveBatchExecutionStatus.SKIPPED_CURRENT
        ],
        skipped_incomplete=counts[
            SelectiveBatchExecutionStatus.SKIPPED_INCOMPLETE
        ],
        not_found=counts[SelectiveBatchExecutionStatus.NOT_FOUND],
        failed=counts[SelectiveBatchExecutionStatus.FAILED],
    )


def _overall_outcome(
    summary: SelectiveBatchSummary,
) -> tuple[SelectiveBatchOverallStatus, SelectiveBatchReason | None]:
    if summary.selected == 0:
        return (SelectiveBatchOverallStatus.NOOP, None)
    succeeded = summary.created + summary.unchanged
    if summary.failed == 0:
        return (SelectiveBatchOverallStatus.COMPLETED, None)
    if succeeded:
        return (
            SelectiveBatchOverallStatus.PARTIAL_FAILURE,
            SelectiveBatchReason.ITEM_FAILURE,
        )
    return (
        SelectiveBatchOverallStatus.FAILED,
        SelectiveBatchReason.ALL_EXECUTIONS_FAILED,
    )


def _invalid_result(
    command: SelectiveBatchReprioritizationCommand,
    message: str,
) -> SelectiveBatchReprioritizationResult:
    raw_ids = (
        command.job_ids
        if isinstance(command.job_ids, tuple)
        and all(isinstance(item, str) for item in command.job_ids)
        else ()
    )
    return SelectiveBatchReprioritizationResult(
        overall_status=SelectiveBatchOverallStatus.FAILED,
        reason_code=SelectiveBatchReason.INVALID_COMMAND,
        retryable=False,
        subject_id=(
            command.subject_id
            if isinstance(command.subject_id, str)
            else ""
        ),
        now=command.now if isinstance(command.now, datetime) else None,
        requested_job_ids=tuple(dict.fromkeys(raw_ids)),
        max_jobs=(
            command.max_jobs
            if isinstance(command.max_jobs, int)
            and not isinstance(command.max_jobs, bool)
            else None
        ),
        queue_snapshot=None,
        queue_failure=None,
        items=(),
        summary=_empty_summary(),
        message=message,
    )


def _not_found_item(job_id: str) -> SelectiveBatchReprioritizationItem:
    return SelectiveBatchReprioritizationItem(
        job_id=job_id,
        queue_status=None,
        execution_status=SelectiveBatchExecutionStatus.NOT_FOUND,
        single_job_result=None,
        failure=SelectiveBatchItemFailure(
            reason_code=SelectiveBatchItemReason.JOB_NOT_IN_QUEUE,
            retryable=False,
            message="The requested job is not in the P1d2 queue snapshot.",
        ),
    )


def _skipped_item(
    *,
    job_id: str,
    queue_status: CurrentPriorityItemStatus,
) -> SelectiveBatchReprioritizationItem:
    execution_status = (
        SelectiveBatchExecutionStatus.SKIPPED_CURRENT
        if queue_status is CurrentPriorityItemStatus.CURRENT
        else SelectiveBatchExecutionStatus.SKIPPED_INCOMPLETE
    )
    return SelectiveBatchReprioritizationItem(
        job_id=job_id,
        queue_status=queue_status,
        execution_status=execution_status,
        single_job_result=None,
        failure=None,
    )


def _executed_item(
    *,
    job_id: str,
    queue_status: CurrentPriorityItemStatus,
    result: SingleJobPriorityResult,
    subject_id: str,
) -> SelectiveBatchReprioritizationItem:
    if (
        result.subject_id != subject_id
        or result.job_id != job_id
    ):
        return SelectiveBatchReprioritizationItem(
            job_id=job_id,
            queue_status=queue_status,
            execution_status=SelectiveBatchExecutionStatus.FAILED,
            single_job_result=result,
            failure=SelectiveBatchItemFailure(
                reason_code=(
                    SelectiveBatchItemReason.SINGLE_JOB_RESULT_INVALID
                ),
                retryable=False,
                message="P1d1 returned a mismatched typed result.",
            ),
        )
    if result.status is SingleJobPriorityStatus.SUCCEEDED:
        if result.change is SingleJobPriorityChange.CREATED:
            execution_status = SelectiveBatchExecutionStatus.CREATED
        elif result.change is SingleJobPriorityChange.UNCHANGED:
            execution_status = SelectiveBatchExecutionStatus.UNCHANGED
        else:
            return SelectiveBatchReprioritizationItem(
                job_id=job_id,
                queue_status=queue_status,
                execution_status=SelectiveBatchExecutionStatus.FAILED,
                single_job_result=result,
                failure=SelectiveBatchItemFailure(
                    reason_code=(
                        SelectiveBatchItemReason.SINGLE_JOB_RESULT_INVALID
                    ),
                    retryable=False,
                    message="P1d1 returned an invalid success outcome.",
                ),
            )
        return SelectiveBatchReprioritizationItem(
            job_id=job_id,
            queue_status=queue_status,
            execution_status=execution_status,
            single_job_result=result,
            failure=None,
        )
    if result.reason_code is None:
        return SelectiveBatchReprioritizationItem(
            job_id=job_id,
            queue_status=queue_status,
            execution_status=SelectiveBatchExecutionStatus.FAILED,
            single_job_result=result,
            failure=SelectiveBatchItemFailure(
                reason_code=(
                    SelectiveBatchItemReason.SINGLE_JOB_RESULT_INVALID
                ),
                retryable=False,
                message="P1d1 returned an invalid failure outcome.",
            ),
        )
    return SelectiveBatchReprioritizationItem(
        job_id=job_id,
        queue_status=queue_status,
        execution_status=SelectiveBatchExecutionStatus.FAILED,
        single_job_result=result,
        failure=SelectiveBatchItemFailure(
            reason_code=SelectiveBatchItemReason.SINGLE_JOB_FAILED,
            retryable=result.retryable,
            message=result.message,
            single_job_reason=result.reason_code,
        ),
    )


async def selectively_reprioritize_jobs(
    command: SelectiveBatchReprioritizationCommand,
    *,
    queue_reader: _QueueReader,
    single_job_orchestrator: _SingleJobOrchestrator,
) -> SelectiveBatchReprioritizationResult:
    """Run P1d1 serially for bounded STALE/MISSING items from one P1d2 read."""

    if not isinstance(command, SelectiveBatchReprioritizationCommand):
        raise TypeError(
            "command must be a SelectiveBatchReprioritizationCommand"
        )
    try:
        subject_id, now, requested_ids, max_jobs = _normalize_command(
            command
        )
    except (TypeError, ValueError) as exc:
        return _invalid_result(command, str(exc))

    queue_result = await queue_reader(
        CurrentPriorityQueueCommand(
            subject_id=subject_id,
            now=now,
        )
    )
    if not isinstance(queue_result, CurrentPriorityQueueResult):
        return SelectiveBatchReprioritizationResult(
            overall_status=SelectiveBatchOverallStatus.FAILED,
            reason_code=SelectiveBatchReason.QUEUE_RESULT_INVALID,
            retryable=False,
            subject_id=subject_id,
            now=now,
            requested_job_ids=requested_ids,
            max_jobs=max_jobs,
            queue_snapshot=None,
            queue_failure=None,
            items=(),
            summary=_empty_summary(),
            message="P1d2 returned an invalid result type.",
        )
    if queue_result.subject_id != subject_id:
        return SelectiveBatchReprioritizationResult(
            overall_status=SelectiveBatchOverallStatus.FAILED,
            reason_code=SelectiveBatchReason.QUEUE_RESULT_INVALID,
            retryable=False,
            subject_id=subject_id,
            now=now,
            requested_job_ids=requested_ids,
            max_jobs=max_jobs,
            queue_snapshot=None,
            queue_failure=None,
            items=(),
            summary=_empty_summary(),
            message="P1d2 returned a mismatched subject.",
        )
    if queue_result.status is CurrentPriorityQueueStatus.FAILED:
        return SelectiveBatchReprioritizationResult(
            overall_status=SelectiveBatchOverallStatus.FAILED,
            reason_code=SelectiveBatchReason.QUEUE_BUILD_FAILED,
            retryable=queue_result.retryable,
            subject_id=subject_id,
            now=now,
            requested_job_ids=requested_ids,
            max_jobs=max_jobs,
            queue_snapshot=None,
            queue_failure=queue_result,
            items=(),
            summary=_empty_summary(),
            message=queue_result.message,
        )

    queue_by_job = {
        item.job.job_id: item
        for item in queue_result.items
    }
    if len(queue_by_job) != len(queue_result.items):
        return SelectiveBatchReprioritizationResult(
            overall_status=SelectiveBatchOverallStatus.FAILED,
            reason_code=SelectiveBatchReason.QUEUE_RESULT_INVALID,
            retryable=False,
            subject_id=subject_id,
            now=now,
            requested_job_ids=requested_ids,
            max_jobs=max_jobs,
            queue_snapshot=None,
            queue_failure=None,
            items=(),
            summary=_empty_summary(),
            message="P1d2 returned duplicate job IDs.",
        )

    targets: list[tuple[str, CurrentPriorityQueueItem | None]]
    explicit_allowlist = bool(requested_ids)
    if explicit_allowlist:
        considered_ids = (
            requested_ids[:max_jobs]
            if max_jobs is not None
            else requested_ids
        )
        targets = [
            (job_id, queue_by_job.get(job_id))
            for job_id in considered_ids
        ]
    else:
        targets = [
            (item.job.job_id, item)
            for item in queue_result.items
            if item.status
            in (
                CurrentPriorityItemStatus.STALE,
                CurrentPriorityItemStatus.MISSING,
            )
        ][:max_jobs]

    batch_items: list[SelectiveBatchReprioritizationItem] = []
    for requested_job_id, queue_item in targets:
        if queue_item is None:
            batch_items.append(_not_found_item(requested_job_id))
            continue
        queue_status = queue_item.status
        job_id = queue_item.job.job_id
        if queue_status in (
            CurrentPriorityItemStatus.CURRENT,
            CurrentPriorityItemStatus.INCOMPLETE,
        ):
            batch_items.append(
                _skipped_item(
                    job_id=job_id,
                    queue_status=queue_status,
                )
            )
            continue

        single_result = await single_job_orchestrator(
            SingleJobPriorityCommand(
                subject_id=subject_id,
                job_id=job_id,
                now=now,
            )
        )
        if not isinstance(single_result, SingleJobPriorityResult):
            raise TypeError("P1d1 returned an invalid result type")
        batch_items.append(
            _executed_item(
                job_id=job_id,
                queue_status=queue_status,
                result=single_result,
                subject_id=subject_id,
            )
        )

    items = tuple(batch_items)
    summary = _summary_from_items(items)
    overall_status, reason_code = _overall_outcome(summary)
    return SelectiveBatchReprioritizationResult(
        overall_status=overall_status,
        reason_code=reason_code,
        retryable=any(
            item.failure is not None and item.failure.retryable
            for item in items
        ),
        subject_id=subject_id,
        now=now,
        requested_job_ids=requested_ids,
        max_jobs=max_jobs,
        queue_snapshot=_snapshot_summary(queue_result),
        queue_failure=None,
        items=items,
        summary=summary,
        message={
            SelectiveBatchOverallStatus.NOOP: (
                "No STALE or MISSING job was executed."
            ),
            SelectiveBatchOverallStatus.COMPLETED: (
                "All selected jobs completed through P1d1."
            ),
            SelectiveBatchOverallStatus.PARTIAL_FAILURE: (
                "Some selected jobs failed; later jobs still ran."
            ),
            SelectiveBatchOverallStatus.FAILED: (
                "All selected job orchestrations failed."
            ),
        }[overall_status],
    )


__all__ = [
    "PriorityQueueSnapshotSummary",
    "SelectiveBatchExecutionStatus",
    "SelectiveBatchItemFailure",
    "SelectiveBatchItemReason",
    "SelectiveBatchOverallStatus",
    "SelectiveBatchReason",
    "SelectiveBatchReprioritizationCommand",
    "SelectiveBatchReprioritizationItem",
    "SelectiveBatchReprioritizationResult",
    "SelectiveBatchSummary",
    "selectively_reprioritize_jobs",
]
