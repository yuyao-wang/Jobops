"""Read-only current PriorityDecision projection for one explicit subject."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .job_discovery import JobPosting
from .job_prioritization import (
    CandidateSummary,
    PriorityAgentMetadata,
    PriorityDecision,
    PriorityDecisionRepositoryError,
    PriorityProposal,
    PriorityQualification,
    ProposedPriorityLevel,
    PrivateHomePriorityDecisionRepository,
)
from .prioritization_policy import (
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
)
from .profile_store import (
    CandidateSummaryProvider,
    CandidateSummaryProviderError,
)
from .single_job_priority import (
    ActivePrioritizationPolicyProvider,
    OrchestrationRecordStatus,
    PrivateHomeSingleJobPriorityRepository,
    SingleJobPriorityBinding,
    SingleJobPriorityRepositoryError,
    StoredSingleJobPriority,
    build_single_job_priority_binding,
    completed_priority_bindings_match,
)
from .subject_job_library import (
    SubjectJobPostingReadStatus,
    SubjectScopedJobPostingReadPort,
)


class CurrentPriorityQueueStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class CurrentPriorityItemStatus(str, Enum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    MISSING = "MISSING"
    INCOMPLETE = "INCOMPLETE"


class CurrentPriorityStaleReason(str, Enum):
    JOB_REVISION_CHANGED = "JOB_REVISION_CHANGED"
    JOB_CONTENT_CHANGED = "JOB_CONTENT_CHANGED"
    POLICY_CHANGED = "POLICY_CHANGED"
    CANDIDATE_SUMMARY_CHANGED = "CANDIDATE_SUMMARY_CHANGED"
    AGENT_VERSION_CHANGED = "AGENT_VERSION_CHANGED"
    PROMPT_VERSION_CHANGED = "PROMPT_VERSION_CHANGED"
    MODEL_VERSION_CHANGED = "MODEL_VERSION_CHANGED"
    EVALUATION_TIME_CHANGED = "EVALUATION_TIME_CHANGED"
    GATE_VERSION_CHANGED = "GATE_VERSION_CHANGED"
    ORCHESTRATION_VERSION_CHANGED = "ORCHESTRATION_VERSION_CHANGED"


class CurrentPriorityQueueReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    JOB_REPOSITORY_FAILED = "JOB_REPOSITORY_FAILED"
    ACTIVE_POLICY_NOT_FOUND = "ACTIVE_POLICY_NOT_FOUND"
    POLICY_READ_FAILED = "POLICY_READ_FAILED"
    CANDIDATE_SUMMARY_UNAVAILABLE = "CANDIDATE_SUMMARY_UNAVAILABLE"
    ORCHESTRATION_REPOSITORY_FAILED = "ORCHESTRATION_REPOSITORY_FAILED"
    DECISION_REPOSITORY_FAILED = "DECISION_REPOSITORY_FAILED"
    PRIORITY_DATA_INTEGRITY_FAILED = "PRIORITY_DATA_INTEGRITY_FAILED"


@dataclass(frozen=True, slots=True)
class CurrentPriorityQueueCommand:
    subject_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class CurrentPriorityQueueItem:
    subject_id: str
    job: JobPosting
    status: CurrentPriorityItemStatus
    expected_binding: SingleJobPriorityBinding
    stored_binding: SingleJobPriorityBinding | None
    proposal: PriorityProposal | None
    decision: PriorityDecision | None
    stale_reasons: tuple[CurrentPriorityStaleReason, ...]
    orchestration_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            CurrentPriorityItemStatus(self.status),
        )
        object.__setattr__(
            self,
            "stale_reasons",
            tuple(
                CurrentPriorityStaleReason(item)
                for item in self.stale_reasons
            ),
        )
        if (
            not isinstance(self.subject_id, str)
            or not self.subject_id
            or not isinstance(self.job, JobPosting)
            or not isinstance(
                self.expected_binding,
                SingleJobPriorityBinding,
            )
            or self.expected_binding.subject_id != self.subject_id
            or self.expected_binding.job_id != self.job.job_id
        ):
            raise ValueError("priority queue item binding is invalid")

        if self.status is CurrentPriorityItemStatus.CURRENT:
            if (
                self.stored_binding != self.expected_binding
                or self.proposal is None
                or self.decision is None
                or self.stale_reasons
                or self.orchestration_id
                != self.expected_binding.input_binding
            ):
                raise ValueError("CURRENT priority queue item is invalid")
        elif self.status is CurrentPriorityItemStatus.STALE:
            if (
                self.stored_binding is None
                or self.proposal is not None
                or self.decision is not None
                or not self.stale_reasons
                or self.orchestration_id
                != self.stored_binding.input_binding
            ):
                raise ValueError("STALE priority queue item is invalid")
        elif self.status is CurrentPriorityItemStatus.MISSING:
            if any(
                value is not None
                for value in (
                    self.stored_binding,
                    self.proposal,
                    self.decision,
                    self.orchestration_id,
                )
            ) or self.stale_reasons:
                raise ValueError("MISSING priority queue item is invalid")
        elif (
            self.stored_binding != self.expected_binding
            or self.proposal is not None
            or self.decision is not None
            or self.stale_reasons
            or self.orchestration_id
            != self.expected_binding.input_binding
        ):
            raise ValueError("INCOMPLETE priority queue item is invalid")


@dataclass(frozen=True, slots=True)
class CurrentPriorityQueueResult:
    status: CurrentPriorityQueueStatus
    reason_code: CurrentPriorityQueueReason | None
    retryable: bool
    subject_id: str
    policy_snapshot: PrioritizationPolicy | None
    items: tuple[CurrentPriorityQueueItem, ...]
    message: str
    membership_snapshot_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            CurrentPriorityQueueStatus(self.status),
        )
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CurrentPriorityQueueReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if self.status is CurrentPriorityQueueStatus.SUCCEEDED:
            if self.reason_code is not None or self.retryable:
                raise ValueError("successful priority queue result is invalid")
            if (
                not isinstance(self.policy_snapshot, PrioritizationPolicy)
                or self.policy_snapshot.status
                is not PrioritizationPolicyStatus.ACTIVE
                or self.policy_snapshot.subject_id != self.subject_id
            ):
                raise ValueError(
                    "successful priority queue policy snapshot is invalid"
                )
            if not all(
                isinstance(item, CurrentPriorityQueueItem)
                and item.subject_id == self.subject_id
                and (
                    item.expected_binding.policy_id,
                    item.expected_binding.policy_version,
                    item.expected_binding.policy_content_hash,
                )
                == (
                    self.policy_snapshot.policy_id,
                    self.policy_snapshot.policy_version,
                    self.policy_snapshot.policy_content_hash,
                )
                for item in self.items
            ):
                raise ValueError("priority queue result items are invalid")
            if self.membership_snapshot_hash and len(
                self.membership_snapshot_hash
            ) != 64:
                raise ValueError("membership snapshot hash is invalid")
        elif (
            self.reason_code is None
            or self.policy_snapshot is not None
            or self.items
            or self.membership_snapshot_hash
        ):
            raise ValueError("failed priority queue result is invalid")

    @property
    def current_items(self) -> tuple[CurrentPriorityQueueItem, ...]:
        return tuple(
            item
            for item in self.items
            if item.status is CurrentPriorityItemStatus.CURRENT
        )


def priority_binding_stale_reasons(
    *,
    expected: SingleJobPriorityBinding,
    stored: SingleJobPriorityBinding,
) -> tuple[CurrentPriorityStaleReason, ...]:
    """Return only stale reasons directly proved by immutable bindings."""

    if not isinstance(expected, SingleJobPriorityBinding) or not isinstance(
        stored,
        SingleJobPriorityBinding,
    ):
        raise TypeError("expected and stored must be typed priority bindings")
    reasons: list[CurrentPriorityStaleReason] = []
    if expected.job_revision != stored.job_revision:
        reasons.append(CurrentPriorityStaleReason.JOB_REVISION_CHANGED)
    if expected.job_content_hash != stored.job_content_hash:
        reasons.append(CurrentPriorityStaleReason.JOB_CONTENT_CHANGED)
    if (
        expected.policy_id,
        expected.policy_version,
        expected.policy_content_hash,
    ) != (
        stored.policy_id,
        stored.policy_version,
        stored.policy_content_hash,
    ):
        reasons.append(CurrentPriorityStaleReason.POLICY_CHANGED)
    if (
        expected.candidate_summary_version,
        expected.candidate_summary_content_hash,
    ) != (
        stored.candidate_summary_version,
        stored.candidate_summary_content_hash,
    ):
        reasons.append(
            CurrentPriorityStaleReason.CANDIDATE_SUMMARY_CHANGED
        )
    if expected.agent_version != stored.agent_version:
        reasons.append(CurrentPriorityStaleReason.AGENT_VERSION_CHANGED)
    if expected.prompt_version != stored.prompt_version:
        reasons.append(CurrentPriorityStaleReason.PROMPT_VERSION_CHANGED)
    if expected.model_id != stored.model_id:
        reasons.append(CurrentPriorityStaleReason.MODEL_VERSION_CHANGED)
    if expected.evaluated_at != stored.evaluated_at:
        reasons.append(CurrentPriorityStaleReason.EVALUATION_TIME_CHANGED)
    if expected.validation_version != stored.validation_version:
        reasons.append(CurrentPriorityStaleReason.GATE_VERSION_CHANGED)
    if expected.orchestration_version != stored.orchestration_version:
        reasons.append(
            CurrentPriorityStaleReason.ORCHESTRATION_VERSION_CHANGED
        )
    return tuple(reasons)


def _failure(
    command: CurrentPriorityQueueCommand,
    reason: CurrentPriorityQueueReason,
    message: str,
    *,
    retryable: bool = False,
) -> CurrentPriorityQueueResult:
    subject_id = (
        command.subject_id
        if isinstance(command.subject_id, str)
        else ""
    )
    return CurrentPriorityQueueResult(
        status=CurrentPriorityQueueStatus.FAILED,
        reason_code=reason,
        retryable=retryable,
        subject_id=subject_id,
        policy_snapshot=None,
        items=(),
        membership_snapshot_hash="",
        message=message,
    )


def _clean_subject_id(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("subject_id must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 160:
        raise ValueError("subject_id is outside the queue contract")
    return cleaned


def _require_aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value


def _historical_sort_key(
    record: StoredSingleJobPriority,
) -> tuple[str, str]:
    return (record.binding.evaluated_at, record.input_binding)


def _current_sort_key(
    item: CurrentPriorityQueueItem,
) -> tuple[int, str, str]:
    decision = item.decision
    if decision is None:
        raise ValueError("CURRENT item requires a decision")
    if decision.qualification is PriorityQualification.QUALIFIED:
        priority_rank = {
            ProposedPriorityLevel.P0: 0,
            ProposedPriorityLevel.P1: 1,
            ProposedPriorityLevel.P2: 2,
            ProposedPriorityLevel.P3: 3,
        }[decision.priority_level]
    elif decision.qualification is PriorityQualification.NEEDS_USER:
        priority_rank = 4
    else:
        priority_rank = 5
    validated_at = (
        decision.validated_at.astimezone(timezone.utc).isoformat()
    )
    return (priority_rank, validated_at, item.job.job_id)


def _queue_sort_key(
    item: CurrentPriorityQueueItem,
) -> tuple[int, int, str, str]:
    status_rank = {
        CurrentPriorityItemStatus.CURRENT: 0,
        CurrentPriorityItemStatus.STALE: 1,
        CurrentPriorityItemStatus.MISSING: 2,
        CurrentPriorityItemStatus.INCOMPLETE: 3,
    }[item.status]
    if item.status is CurrentPriorityItemStatus.CURRENT:
        priority_rank, timestamp, job_id = _current_sort_key(item)
        return (status_rank, priority_rank, timestamp, job_id)
    return (status_rank, 0, "", item.job.job_id)


def _load_completed_decision(
    *,
    record: StoredSingleJobPriority,
    decision_repository: PrivateHomePriorityDecisionRepository,
) -> PriorityDecision:
    if (
        record.status is not OrchestrationRecordStatus.COMPLETED
        or record.proposal is None
        or record.decision_id is None
    ):
        raise ValueError("completed orchestration is incomplete")
    decision = decision_repository.get_decision(
        subject_id=record.binding.subject_id,
        job_id=record.binding.job_id,
        decision_id=record.decision_id,
    )
    if decision is None or not completed_priority_bindings_match(
        binding=record.binding,
        proposal=record.proposal,
        decision=decision,
    ):
        raise ValueError("completed priority bindings are invalid")
    return decision


async def build_current_priority_queue(
    command: CurrentPriorityQueueCommand,
    *,
    subject_job_reader: SubjectScopedJobPostingReadPort,
    policy_provider: ActivePrioritizationPolicyProvider,
    candidate_summary_provider: CandidateSummaryProvider,
    orchestration_repository: PrivateHomeSingleJobPriorityRepository,
    decision_repository: PrivateHomePriorityDecisionRepository,
    metadata: PriorityAgentMetadata,
) -> CurrentPriorityQueueResult:
    """Build a typed current/stale/missing/incomplete queue without writes."""

    if not isinstance(command, CurrentPriorityQueueCommand):
        raise TypeError("command must be a CurrentPriorityQueueCommand")
    try:
        subject_id = _clean_subject_id(command.subject_id)
        _require_aware(command.now)
    except (TypeError, ValueError) as exc:
        return _failure(
            command,
            CurrentPriorityQueueReason.INVALID_REQUEST,
            str(exc),
        )

    try:
        subject_jobs = subject_job_reader.list_current(
            subject_id=subject_id, now=command.now
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CurrentPriorityQueueReason.JOB_REPOSITORY_FAILED,
            "The current typed JobPosting collection could not be read.",
            retryable=True,
        )
    if subject_jobs.status not in {
        SubjectJobPostingReadStatus.READY,
        SubjectJobPostingReadStatus.EMPTY,
    }:
        return _failure(
            command,
            CurrentPriorityQueueReason.JOB_REPOSITORY_FAILED,
            "The subject Job Library could not be read safely.",
        )
    jobs = tuple(item.job_posting for item in subject_jobs.ordered_items)

    try:
        policy = policy_provider.get_active_policy(subject_id)
    except RuntimeError:
        return _failure(
            command,
            CurrentPriorityQueueReason.POLICY_READ_FAILED,
            "The ACTIVE PrioritizationPolicy could not be read.",
            retryable=True,
        )
    if (
        policy is None
        or not isinstance(policy, PrioritizationPolicy)
        or policy.status is not PrioritizationPolicyStatus.ACTIVE
        or policy.subject_id != subject_id
    ):
        return _failure(
            command,
            CurrentPriorityQueueReason.ACTIVE_POLICY_NOT_FOUND,
            "No matching ACTIVE PrioritizationPolicy is available.",
        )

    try:
        candidate_summary = candidate_summary_provider.get_current(
            subject_id,
            now=command.now,
        )
    except CandidateSummaryProviderError:
        return _failure(
            command,
            CurrentPriorityQueueReason.CANDIDATE_SUMMARY_UNAVAILABLE,
            "The authoritative CandidateSummary is unavailable or invalid.",
        )
    if (
        not isinstance(candidate_summary, CandidateSummary)
        or candidate_summary.subject_id != subject_id
    ):
        return _failure(
            command,
            CurrentPriorityQueueReason.CANDIDATE_SUMMARY_UNAVAILABLE,
            "The CandidateSummary subject binding is invalid.",
        )

    try:
        records = orchestration_repository.list_for_subject(subject_id)
    except SingleJobPriorityRepositoryError:
        return _failure(
            command,
            CurrentPriorityQueueReason.ORCHESTRATION_REPOSITORY_FAILED,
            "The priority orchestration history could not be read.",
            retryable=True,
        )
    records_by_job: dict[str, list[StoredSingleJobPriority]] = {}
    for record in records:
        records_by_job.setdefault(record.binding.job_id, []).append(record)

    items: list[CurrentPriorityQueueItem] = []
    for job in jobs:
        try:
            expected = build_single_job_priority_binding(
                subject_id=subject_id,
                job=job,
                policy=policy,
                candidate_summary=candidate_summary,
                metadata=metadata,
                now=command.now,
            )
        except (AttributeError, TypeError, ValueError):
            return _failure(
                command,
                CurrentPriorityQueueReason.PRIORITY_DATA_INTEGRITY_FAILED,
                "A current priority input binding is invalid.",
            )

        history = records_by_job.get(job.job_id, [])
        exact = next(
            (
                record
                for record in history
                if record.input_binding == expected.input_binding
            ),
            None,
        )
        if exact is not None:
            if exact.binding != expected:
                return _failure(
                    command,
                    CurrentPriorityQueueReason.PRIORITY_DATA_INTEGRITY_FAILED,
                    "An orchestration input binding collision was detected.",
                )
            if exact.status is not OrchestrationRecordStatus.COMPLETED:
                items.append(
                    CurrentPriorityQueueItem(
                        subject_id=subject_id,
                        job=job,
                        status=CurrentPriorityItemStatus.INCOMPLETE,
                        expected_binding=expected,
                        stored_binding=exact.binding,
                        proposal=None,
                        decision=None,
                        stale_reasons=(),
                        orchestration_id=exact.input_binding,
                    )
                )
                continue
            try:
                decision = _load_completed_decision(
                    record=exact,
                    decision_repository=decision_repository,
                )
            except PriorityDecisionRepositoryError:
                return _failure(
                    command,
                    CurrentPriorityQueueReason.DECISION_REPOSITORY_FAILED,
                    "A completed PriorityDecision could not be read.",
                    retryable=True,
                )
            except ValueError:
                return _failure(
                    command,
                    CurrentPriorityQueueReason.PRIORITY_DATA_INTEGRITY_FAILED,
                    "A completed priority orchestration is inconsistent.",
                )
            items.append(
                CurrentPriorityQueueItem(
                    subject_id=subject_id,
                    job=job,
                    status=CurrentPriorityItemStatus.CURRENT,
                    expected_binding=expected,
                    stored_binding=exact.binding,
                    proposal=exact.proposal,
                    decision=decision,
                    stale_reasons=(),
                    orchestration_id=exact.input_binding,
                )
            )
            continue

        completed = tuple(
            record
            for record in history
            if record.status is OrchestrationRecordStatus.COMPLETED
        )
        if not completed:
            items.append(
                CurrentPriorityQueueItem(
                    subject_id=subject_id,
                    job=job,
                    status=CurrentPriorityItemStatus.MISSING,
                    expected_binding=expected,
                    stored_binding=None,
                    proposal=None,
                    decision=None,
                    stale_reasons=(),
                    orchestration_id=None,
                )
            )
            continue

        previous = max(completed, key=_historical_sort_key)
        stale_reasons = priority_binding_stale_reasons(
            expected=expected,
            stored=previous.binding,
        )
        if not stale_reasons:
            return _failure(
                command,
                CurrentPriorityQueueReason.PRIORITY_DATA_INTEGRITY_FAILED,
                "A completed orchestration has an inconsistent identity.",
            )
        try:
            _load_completed_decision(
                record=previous,
                decision_repository=decision_repository,
            )
        except PriorityDecisionRepositoryError:
            return _failure(
                command,
                CurrentPriorityQueueReason.DECISION_REPOSITORY_FAILED,
                "A historical PriorityDecision could not be read.",
                retryable=True,
            )
        except ValueError:
            return _failure(
                command,
                CurrentPriorityQueueReason.PRIORITY_DATA_INTEGRITY_FAILED,
                "A historical priority orchestration is inconsistent.",
            )
        items.append(
            CurrentPriorityQueueItem(
                subject_id=subject_id,
                job=job,
                status=CurrentPriorityItemStatus.STALE,
                expected_binding=expected,
                stored_binding=previous.binding,
                proposal=None,
                decision=None,
                stale_reasons=stale_reasons,
                orchestration_id=previous.input_binding,
            )
        )

    ordered = tuple(sorted(items, key=_queue_sort_key))
    return CurrentPriorityQueueResult(
        status=CurrentPriorityQueueStatus.SUCCEEDED,
        reason_code=None,
        retryable=False,
        subject_id=subject_id,
        policy_snapshot=policy,
        items=ordered,
        membership_snapshot_hash=subject_jobs.membership_snapshot_hash,
        message="The current priority queue read model was built.",
    )


__all__ = [
    "CurrentPriorityItemStatus",
    "CurrentPriorityQueueCommand",
    "CurrentPriorityQueueItem",
    "CurrentPriorityQueueReason",
    "CurrentPriorityQueueResult",
    "CurrentPriorityQueueStatus",
    "CurrentPriorityStaleReason",
    "build_current_priority_queue",
    "priority_binding_stale_reasons",
]
