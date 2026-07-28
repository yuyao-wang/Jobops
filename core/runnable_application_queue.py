"""Read-only admission view for jobs allowed into Application Preparation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .accepted_job_intent import (
    AcceptedJobIntent,
    AcceptedJobIntentReadResult,
    AcceptedJobIntentReadStatus,
    AcceptedJobIntentRepository,
)
from .current_priority_queue import (
    CurrentPriorityItemStatus,
    CurrentPriorityQueueCommand,
    CurrentPriorityQueueResult,
    CurrentPriorityQueueStatus,
)
from .job_discovery import (
    JOB_POSTING_STATUSES,
    JOB_POSTING_UNAVAILABLE_STATUSES,
    JobIntakeIntent,
    JobPosting,
)
from .job_prioritization import (
    PriorityDecision,
    PriorityQualification,
)
from .prioritization_policy import (
    PreparationAdmissionPolicy,
    PreparationPriority,
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
)


_PriorityQueueReader = Callable[
    [CurrentPriorityQueueCommand],
    Awaitable[CurrentPriorityQueueResult],
]
_AVAILABLE_JOB_STATUSES = (
    JOB_POSTING_STATUSES - JOB_POSTING_UNAVAILABLE_STATUSES
)


class RunnableApplicationQueueStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RunnableApplicationStatus(str, Enum):
    RUNNABLE = "RUNNABLE"
    BLOCKED_NOT_CURRENT = "BLOCKED_NOT_CURRENT"
    BLOCKED_NO_APPLICATION_INTENT = "BLOCKED_NO_APPLICATION_INTENT"
    BLOCKED_NEEDS_USER = "BLOCKED_NEEDS_USER"
    BLOCKED_EXCLUDED = "BLOCKED_EXCLUDED"
    BLOCKED_PRIORITY = "BLOCKED_PRIORITY"
    BLOCKED_PROMOTION_REQUIRED = "BLOCKED_PROMOTION_REQUIRED"
    BLOCKED_JOB_STATE = "BLOCKED_JOB_STATE"


class RunnableApplicationReason(str, Enum):
    PRIORITY_NOT_CURRENT = "PRIORITY_NOT_CURRENT"
    NO_APPLICATION_INTENT = "NO_APPLICATION_INTENT"
    PRIORITY_NEEDS_USER = "PRIORITY_NEEDS_USER"
    PRIORITY_EXCLUDED = "PRIORITY_EXCLUDED"
    PRIORITY_NOT_ADMITTED = "PRIORITY_NOT_ADMITTED"
    EXPLICIT_PROMOTION_REQUIRED = "EXPLICIT_PROMOTION_REQUIRED"
    JOB_STATE_UNAVAILABLE = "JOB_STATE_UNAVAILABLE"


class RunnableApplicationQueueReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PRIORITY_QUEUE_FAILED = "PRIORITY_QUEUE_FAILED"
    PRIORITY_QUEUE_RESULT_INVALID = "PRIORITY_QUEUE_RESULT_INVALID"
    INTENT_INTEGRITY_FAILURE = "INTENT_INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class RunnableApplicationQueueCommand:
    subject_id: str
    now: datetime


_STATUS_REASON = {
    RunnableApplicationStatus.BLOCKED_NOT_CURRENT: (
        RunnableApplicationReason.PRIORITY_NOT_CURRENT
    ),
    RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT: (
        RunnableApplicationReason.NO_APPLICATION_INTENT
    ),
    RunnableApplicationStatus.BLOCKED_NEEDS_USER: (
        RunnableApplicationReason.PRIORITY_NEEDS_USER
    ),
    RunnableApplicationStatus.BLOCKED_EXCLUDED: (
        RunnableApplicationReason.PRIORITY_EXCLUDED
    ),
    RunnableApplicationStatus.BLOCKED_PRIORITY: (
        RunnableApplicationReason.PRIORITY_NOT_ADMITTED
    ),
    RunnableApplicationStatus.BLOCKED_PROMOTION_REQUIRED: (
        RunnableApplicationReason.EXPLICIT_PROMOTION_REQUIRED
    ),
    RunnableApplicationStatus.BLOCKED_JOB_STATE: (
        RunnableApplicationReason.JOB_STATE_UNAVAILABLE
    ),
}


@dataclass(frozen=True, slots=True)
class RunnableApplicationQueueItem:
    subject_id: str
    job: JobPosting
    priority_queue_status: CurrentPriorityItemStatus
    runnable_status: RunnableApplicationStatus
    priority_decision: PriorityDecision | None
    application_intent: AcceptedJobIntent | None
    reasons: tuple[RunnableApplicationReason, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "priority_queue_status",
            CurrentPriorityItemStatus(self.priority_queue_status),
        )
        object.__setattr__(
            self,
            "runnable_status",
            RunnableApplicationStatus(self.runnable_status),
        )
        object.__setattr__(
            self,
            "reasons",
            tuple(RunnableApplicationReason(item) for item in self.reasons),
        )
        if (
            not isinstance(self.subject_id, str)
            or not self.subject_id
            or not isinstance(self.job, JobPosting)
        ):
            raise ValueError("runnable application item identity is invalid")
        if self.application_intent is not None and (
            not isinstance(self.application_intent, AcceptedJobIntent)
            or self.application_intent.subject_id != self.subject_id
            or self.application_intent.job_id != self.job.job_id
        ):
            raise ValueError("runnable application intent binding is invalid")

        if self.runnable_status is RunnableApplicationStatus.RUNNABLE:
            if (
                self.priority_queue_status
                is not CurrentPriorityItemStatus.CURRENT
                or not isinstance(self.priority_decision, PriorityDecision)
                or self.application_intent is None
                or self.application_intent.intent
                is not JobIntakeIntent.REQUEST_APPLICATION
                or self.reasons
            ):
                raise ValueError("RUNNABLE application item is invalid")
            return

        expected_reason = _STATUS_REASON[self.runnable_status]
        if self.reasons != (expected_reason,):
            raise ValueError("blocked application reasons are invalid")
        if (
            self.priority_queue_status
            is not CurrentPriorityItemStatus.CURRENT
        ):
            if (
                self.runnable_status
                is not RunnableApplicationStatus.BLOCKED_NOT_CURRENT
                or self.priority_decision is not None
            ):
                raise ValueError("non-current application item is invalid")
        elif not isinstance(self.priority_decision, PriorityDecision):
            raise ValueError("current blocked item requires a decision")


@dataclass(frozen=True, slots=True)
class RunnableApplicationQueueResult:
    status: RunnableApplicationQueueStatus
    reason_code: RunnableApplicationQueueReason | None
    retryable: bool
    subject_id: str
    now: datetime | None
    policy_snapshot: PrioritizationPolicy | None
    priority_queue_result: CurrentPriorityQueueResult | None
    items: tuple[RunnableApplicationQueueItem, ...]
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            RunnableApplicationQueueStatus(self.status),
        )
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                RunnableApplicationQueueReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if self.status is RunnableApplicationQueueStatus.SUCCEEDED:
            if (
                self.reason_code is not None
                or self.retryable
                or not isinstance(self.now, datetime)
                or self.now.tzinfo is None
                or not isinstance(self.policy_snapshot, PrioritizationPolicy)
                or self.policy_snapshot.status
                is not PrioritizationPolicyStatus.ACTIVE
                or self.policy_snapshot.subject_id != self.subject_id
                or not isinstance(
                    self.priority_queue_result,
                    CurrentPriorityQueueResult,
                )
                or self.priority_queue_result.status
                is not CurrentPriorityQueueStatus.SUCCEEDED
                or self.priority_queue_result.policy_snapshot
                != self.policy_snapshot
                or not all(
                    isinstance(item, RunnableApplicationQueueItem)
                    and item.subject_id == self.subject_id
                    for item in self.items
                )
            ):
                raise ValueError(
                    "successful runnable application queue is invalid"
                )
        elif (
            self.reason_code is None
            or self.policy_snapshot is not None
            or self.items
        ):
            raise ValueError("failed runnable application queue is invalid")

    @property
    def runnable_items(self) -> tuple[RunnableApplicationQueueItem, ...]:
        return tuple(
            item
            for item in self.items
            if item.runnable_status is RunnableApplicationStatus.RUNNABLE
        )

    @property
    def blocked_items(self) -> tuple[RunnableApplicationQueueItem, ...]:
        return tuple(
            item
            for item in self.items
            if item.runnable_status is not RunnableApplicationStatus.RUNNABLE
        )


def _clean_subject_id(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("subject_id must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 160:
        raise ValueError("subject_id is outside the runnable queue contract")
    return cleaned


def _require_aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value


def _failure(
    *,
    subject_id: str,
    now: datetime | None,
    reason: RunnableApplicationQueueReason,
    message: str,
    retryable: bool = False,
    priority_queue_result: CurrentPriorityQueueResult | None = None,
) -> RunnableApplicationQueueResult:
    return RunnableApplicationQueueResult(
        status=RunnableApplicationQueueStatus.FAILED,
        reason_code=reason,
        retryable=retryable,
        subject_id=subject_id,
        now=now,
        policy_snapshot=None,
        priority_queue_result=priority_queue_result,
        items=(),
        message=message,
    )


def _blocked(
    *,
    subject_id: str,
    job: JobPosting,
    queue_status: CurrentPriorityItemStatus,
    status: RunnableApplicationStatus,
    decision: PriorityDecision | None,
    intent: AcceptedJobIntent | None,
) -> RunnableApplicationQueueItem:
    return RunnableApplicationQueueItem(
        subject_id=subject_id,
        job=job,
        priority_queue_status=queue_status,
        runnable_status=status,
        priority_decision=decision,
        application_intent=intent,
        reasons=(_STATUS_REASON[status],),
    )


def _classify(
    *,
    subject_id: str,
    queue_status: CurrentPriorityItemStatus,
    job: JobPosting,
    decision: PriorityDecision | None,
    intent: AcceptedJobIntent | None,
    admission: PreparationAdmissionPolicy,
) -> RunnableApplicationQueueItem:
    if queue_status is not CurrentPriorityItemStatus.CURRENT:
        return _blocked(
            subject_id=subject_id,
            job=job,
            queue_status=queue_status,
            status=RunnableApplicationStatus.BLOCKED_NOT_CURRENT,
            decision=None,
            intent=intent,
        )
    if not isinstance(decision, PriorityDecision):
        raise ValueError("CURRENT priority item has no decision")
    if decision.qualification is PriorityQualification.NEEDS_USER:
        return _blocked(
            subject_id=subject_id,
            job=job,
            queue_status=queue_status,
            status=RunnableApplicationStatus.BLOCKED_NEEDS_USER,
            decision=decision,
            intent=intent,
        )
    if decision.qualification is PriorityQualification.EXCLUDED:
        return _blocked(
            subject_id=subject_id,
            job=job,
            queue_status=queue_status,
            status=RunnableApplicationStatus.BLOCKED_EXCLUDED,
            decision=decision,
            intent=intent,
        )
    if job.status not in _AVAILABLE_JOB_STATUSES:
        return _blocked(
            subject_id=subject_id,
            job=job,
            queue_status=queue_status,
            status=RunnableApplicationStatus.BLOCKED_JOB_STATE,
            decision=decision,
            intent=intent,
        )
    if (
        intent is None
        or intent.intent is not JobIntakeIntent.REQUEST_APPLICATION
    ):
        return _blocked(
            subject_id=subject_id,
            job=job,
            queue_status=queue_status,
            status=(
                RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT
            ),
            decision=decision,
            intent=intent,
        )

    if decision.priority_level is None:
        raise ValueError("qualified priority item has no priority level")
    priority = PreparationPriority(decision.priority_level.value)
    if priority in admission.preparation_eligible_priorities:
        return RunnableApplicationQueueItem(
            subject_id=subject_id,
            job=job,
            priority_queue_status=queue_status,
            runnable_status=RunnableApplicationStatus.RUNNABLE,
            priority_decision=decision,
            application_intent=intent,
            reasons=(),
        )
    if priority in admission.explicit_promotion_priorities:
        return _blocked(
            subject_id=subject_id,
            job=job,
            queue_status=queue_status,
            status=RunnableApplicationStatus.BLOCKED_PROMOTION_REQUIRED,
            decision=decision,
            intent=intent,
        )
    return _blocked(
        subject_id=subject_id,
        job=job,
        queue_status=queue_status,
        status=RunnableApplicationStatus.BLOCKED_PRIORITY,
        decision=decision,
        intent=intent,
    )


async def build_runnable_application_queue(
    command: RunnableApplicationQueueCommand,
    *,
    priority_queue_reader: _PriorityQueueReader,
    accepted_intent_repository: AcceptedJobIntentRepository,
) -> RunnableApplicationQueueResult:
    """Build one typed read model without claims, writes or reprioritization."""

    if not isinstance(command, RunnableApplicationQueueCommand):
        raise TypeError(
            "command must be a RunnableApplicationQueueCommand"
        )
    try:
        subject_id = _clean_subject_id(command.subject_id)
        now = _require_aware(command.now)
    except (TypeError, ValueError) as exc:
        return _failure(
            subject_id=(
                command.subject_id
                if isinstance(command.subject_id, str)
                else ""
            ),
            now=command.now if isinstance(command.now, datetime) else None,
            reason=RunnableApplicationQueueReason.INVALID_REQUEST,
            message=str(exc),
        )

    try:
        priority_queue = await priority_queue_reader(
            CurrentPriorityQueueCommand(subject_id=subject_id, now=now)
        )
    except RuntimeError:
        return _failure(
            subject_id=subject_id,
            now=now,
            reason=RunnableApplicationQueueReason.PRIORITY_QUEUE_FAILED,
            message="The current Priority Queue could not be read.",
            retryable=True,
        )
    if not isinstance(priority_queue, CurrentPriorityQueueResult):
        return _failure(
            subject_id=subject_id,
            now=now,
            reason=(
                RunnableApplicationQueueReason.PRIORITY_QUEUE_RESULT_INVALID
            ),
            message="The current Priority Queue returned invalid data.",
        )
    if priority_queue.status is CurrentPriorityQueueStatus.FAILED:
        return _failure(
            subject_id=subject_id,
            now=now,
            reason=RunnableApplicationQueueReason.PRIORITY_QUEUE_FAILED,
            message="The current Priority Queue could not be built.",
            retryable=priority_queue.retryable,
            priority_queue_result=priority_queue,
        )
    policy = priority_queue.policy_snapshot
    if (
        priority_queue.subject_id != subject_id
        or not isinstance(policy, PrioritizationPolicy)
        or policy.status is not PrioritizationPolicyStatus.ACTIVE
        or policy.subject_id != subject_id
    ):
        return _failure(
            subject_id=subject_id,
            now=now,
            reason=(
                RunnableApplicationQueueReason.PRIORITY_QUEUE_RESULT_INVALID
            ),
            message="The current Priority Queue snapshot is inconsistent.",
            priority_queue_result=priority_queue,
        )

    items: list[RunnableApplicationQueueItem] = []
    for queue_item in priority_queue.items:
        try:
            intent_result = accepted_intent_repository.get_current(
                subject_id=subject_id,
                job_id=queue_item.job.job_id,
            )
        except (OSError, RuntimeError):
            return _failure(
                subject_id=subject_id,
                now=now,
                reason=(
                    RunnableApplicationQueueReason.INTENT_INTEGRITY_FAILURE
                ),
                message="Accepted application intent could not be read safely.",
                priority_queue_result=priority_queue,
            )
        if not isinstance(intent_result, AcceptedJobIntentReadResult):
            return _failure(
                subject_id=subject_id,
                now=now,
                reason=(
                    RunnableApplicationQueueReason.INTENT_INTEGRITY_FAILURE
                ),
                message="Accepted application intent data is invalid.",
                priority_queue_result=priority_queue,
            )
        if (
            intent_result.status
            is AcceptedJobIntentReadStatus.INTEGRITY_FAILURE
        ):
            return _failure(
                subject_id=subject_id,
                now=now,
                reason=(
                    RunnableApplicationQueueReason.INTENT_INTEGRITY_FAILURE
                ),
                message="Accepted application intent failed integrity checks.",
                priority_queue_result=priority_queue,
            )
        intent = (
            intent_result.intent
            if intent_result.status is AcceptedJobIntentReadStatus.FOUND
            else None
        )
        try:
            items.append(
                _classify(
                    subject_id=subject_id,
                    queue_status=queue_item.status,
                    job=queue_item.job,
                    decision=queue_item.decision,
                    intent=intent,
                    admission=policy.preparation_admission,
                )
            )
        except (AttributeError, TypeError, ValueError):
            return _failure(
                subject_id=subject_id,
                now=now,
                reason=(
                    RunnableApplicationQueueReason.PRIORITY_QUEUE_RESULT_INVALID
                ),
                message="A Priority Queue item is inconsistent.",
                priority_queue_result=priority_queue,
            )

    return RunnableApplicationQueueResult(
        status=RunnableApplicationQueueStatus.SUCCEEDED,
        reason_code=None,
        retryable=False,
        subject_id=subject_id,
        now=now,
        policy_snapshot=policy,
        priority_queue_result=priority_queue,
        items=tuple(items),
        message="The runnable Application Preparation queue was built.",
    )


__all__ = [
    "RunnableApplicationQueueCommand",
    "RunnableApplicationQueueItem",
    "RunnableApplicationQueueReason",
    "RunnableApplicationQueueResult",
    "RunnableApplicationQueueStatus",
    "RunnableApplicationReason",
    "RunnableApplicationStatus",
    "build_runnable_application_queue",
]
