"""Focused synthetic tests for the runnable Application Preparation queue."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.accepted_job_intent import (
    AcceptedJobIntent,
    AcceptedJobIntentFailureReason,
    AcceptedJobIntentReadResult,
    AcceptedJobIntentReadStatus,
    AcceptedJobIntentSourceProvenance,
    AcceptedJobIntentSourceType,
)
from core.current_priority_queue import (
    CurrentPriorityItemStatus,
    CurrentPriorityQueueItem,
    CurrentPriorityQueueReason,
    CurrentPriorityQueueResult,
    CurrentPriorityQueueStatus,
    CurrentPriorityStaleReason,
)
from core.job_discovery import JobIntakeIntent, JobPosting
from core.job_prioritization import (
    PRIORITY_DECISION_SCHEMA_VERSION,
    PRIORITY_VALIDATION_VERSION,
    ConstraintValidationSource,
    DecisionOrigin,
    EligibilityCategory,
    EligibilityFinding,
    EligibilityFindingResult,
    EligibilityImpact,
    EvidenceRef,
    EvidenceSourceType,
    FinalHardConstraintFinding,
    HardConstraintFinding,
    HardConstraintFindingResult,
    PriorityDecision,
    PriorityProposal,
    PriorityQualification,
    PriorityRationale,
    ProposalConfidence,
    ProposedPriorityLevel,
    ProposedQualification,
    RationaleCategory,
    priority_decision_id,
    priority_proposal_content_hash,
)
from core.prioritization_policy import (
    HardConstraintType,
    PreparationAdmissionPolicy,
    PreparationPriority,
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
    policy_content_hash,
)
from core.runnable_application_queue import (
    RunnableApplicationQueueCommand,
    RunnableApplicationQueueReason,
    RunnableApplicationQueueStatus,
    RunnableApplicationStatus,
    build_runnable_application_queue,
)
from core.single_job_priority import SingleJobPriorityBinding


NOW = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
SUBJECT = "synthetic-runnable-subject"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _admission(
    *,
    direct: tuple[PreparationPriority, ...] = (
        PreparationPriority.P0,
        PreparationPriority.P1,
        PreparationPriority.P2,
    ),
    promotion: tuple[PreparationPriority, ...] = (
        PreparationPriority.P3,
    ),
) -> PreparationAdmissionPolicy:
    return PreparationAdmissionPolicy(
        preparation_eligible_priorities=direct,
        explicit_promotion_priorities=promotion,
    )


def _policy(
    *,
    subject_id: str = SUBJECT,
    admission: PreparationAdmissionPolicy | None = None,
) -> PrioritizationPolicy:
    selected = admission or _admission()
    raw = "Synthetic approved preparation policy."
    return PrioritizationPolicy(
        policy_id=f"policy-{subject_id}",
        subject_id=subject_id,
        policy_version=1,
        policy_content_hash=policy_content_hash(
            raw_preference_text=raw,
            hard_constraints=(),
            soft_preferences=(),
            preparation_admission=selected,
        ),
        raw_preference_text=raw,
        hard_constraints=(),
        soft_preferences=(),
        preparation_admission=selected,
        status=PrioritizationPolicyStatus.ACTIVE,
        created_at=NOW - timedelta(days=2),
        approved_at=NOW - timedelta(days=1),
        interpreter_version="synthetic-interpreter-v1",
    )


def _job(
    job_id: str,
    *,
    status: str = "NORMALIZED",
) -> JobPosting:
    return JobPosting(
        schema_version="1.0",
        job_id=job_id,
        revision=1,
        source_platform="greenhouse",
        source_job_id=f"source-{job_id}",
        source_url=f"https://boards.greenhouse.io/example/jobs/{job_id}",
        company="Synthetic Earth",
        title="Machine Learning Engineer",
        location="Vancouver, Canada",
        work_mode="HYBRID",
        posted_at="2026-07-27T18:00:00Z",
        observed_at="2026-07-28T17:00:00Z",
        application_url=None,
        ats_type="greenhouse",
        description="Build synthetic environmental systems.",
        content_hash=_digest(f"job:{job_id}"),
        status=status,
    )


def _eligibility() -> tuple[EligibilityFinding, ...]:
    return tuple(
        EligibilityFinding(
            category=category,
            result=EligibilityFindingResult.NOT_APPLICABLE,
            impact=EligibilityImpact.NONE,
            explanation="No explicit eligibility requirement is present.",
            evidence_refs=(),
        )
        for category in EligibilityCategory
    )


def _artifacts(
    job: JobPosting,
    policy: PrioritizationPolicy,
    *,
    qualification: PriorityQualification = PriorityQualification.QUALIFIED,
    level: ProposedPriorityLevel | None = ProposedPriorityLevel.P1,
) -> tuple[PriorityProposal, PriorityDecision]:
    evidence = (
        EvidenceRef(
            source_type=EvidenceSourceType.JOB_FIELD,
            source_id=job.job_id,
            field="title",
        ),
    )
    signal = PriorityRationale(
        signal_id=f"signal-{job.job_id}",
        category=RationaleCategory.ROLE,
        explanation="The role is a synthetic positive signal.",
        evidence_refs=evidence,
    )
    proposal_id = f"proposal-{job.job_id}"
    matched = qualification is PriorityQualification.EXCLUDED
    needs_user = qualification is PriorityQualification.NEEDS_USER
    proposal_finding = (
        HardConstraintFinding(
            constraint_id="constraint-synthetic",
            result=HardConstraintFindingResult.MATCHED,
            explanation="The synthetic hard constraint is violated.",
            evidence_refs=evidence,
        ),
    ) if matched else ()
    proposal = PriorityProposal(
        proposal_id=proposal_id,
        request_id=f"proposal-request-{job.job_id}",
        subject_id=policy.subject_id,
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_content_hash=policy.policy_content_hash,
        candidate_summary_version="candidate-summary-v1",
        candidate_summary_content_hash=_digest("candidate-summary"),
        agent_version="priority-agent-v1",
        prompt_version="priority-prompt-v1",
        model_id="synthetic-model",
        created_at=NOW - timedelta(minutes=1),
        proposed_qualification=ProposedQualification(qualification.value),
        proposed_priority_level=None if matched or needs_user else level,
        confidence=ProposalConfidence.HIGH,
        summary="Synthetic priority recommendation.",
        positive_signals=() if matched or needs_user else (signal,),
        concerns=(),
        hard_constraint_findings=proposal_finding,
        eligibility_findings=_eligibility(),
        missing_information=(
            ("Confirm a material job fact.",) if needs_user else ()
        ),
        questions_for_user=(),
    )
    proposal_hash = priority_proposal_content_hash(proposal)
    final_finding = (
        FinalHardConstraintFinding(
            constraint_id="constraint-synthetic",
            constraint_type=HardConstraintType.EXCLUDED_COMPANY,
            agent_result=HardConstraintFindingResult.MATCHED,
            deterministic_result=HardConstraintFindingResult.MATCHED,
            final_result=HardConstraintFindingResult.MATCHED,
            validation_source=ConstraintValidationSource.DETERMINISTIC,
            explanation="The synthetic hard constraint is violated.",
            evidence_refs=evidence,
        ),
    ) if matched else ()
    origin = (
        DecisionOrigin.HARD_CONSTRAINT_OVERRIDE
        if matched
        else DecisionOrigin.ACCEPTED_PROPOSAL
    )
    decision = PriorityDecision(
        schema_version=PRIORITY_DECISION_SCHEMA_VERSION,
        decision_id=priority_decision_id(
            source_proposal_id=proposal_id,
            source_proposal_content_hash=proposal_hash,
        ),
        request_id=f"decision-request-{job.job_id}",
        subject_id=policy.subject_id,
        source_proposal_id=proposal_id,
        source_proposal_content_hash=proposal_hash,
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_content_hash=policy.policy_content_hash,
        candidate_summary_version="candidate-summary-v1",
        candidate_summary_content_hash=_digest("candidate-summary"),
        agent_version="priority-agent-v1",
        prompt_version="priority-prompt-v1",
        model_id="synthetic-model",
        validation_version=PRIORITY_VALIDATION_VERSION,
        validated_at=NOW,
        decision_origin=origin,
        qualification=qualification,
        priority_level=None if matched or needs_user else level,
        confidence=ProposalConfidence.HIGH,
        summary="Synthetic formal priority decision.",
        positive_signals=() if matched or needs_user else (signal,),
        concerns=(),
        hard_constraint_findings=final_finding,
        eligibility_findings=_eligibility(),
        missing_information=(
            ("Confirm a material job fact.",) if needs_user else ()
        ),
        questions_for_user=(),
        reason_codes=(origin.value,),
    )
    return proposal, decision


def _binding(job: JobPosting, policy: PrioritizationPolicy) -> SingleJobPriorityBinding:
    return SingleJobPriorityBinding(
        subject_id=policy.subject_id,
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_content_hash=policy.policy_content_hash,
        candidate_summary_version="candidate-summary-v1",
        candidate_summary_content_hash=_digest("candidate-summary"),
        agent_version="priority-agent-v1",
        prompt_version="priority-prompt-v1",
        model_id="synthetic-model",
        validation_version=PRIORITY_VALIDATION_VERSION,
        evaluated_at="2026-07-28T18:00:00Z",
    )


def _queue_item(
    job_id: str,
    policy: PrioritizationPolicy,
    *,
    status: CurrentPriorityItemStatus = CurrentPriorityItemStatus.CURRENT,
    qualification: PriorityQualification = PriorityQualification.QUALIFIED,
    level: ProposedPriorityLevel | None = ProposedPriorityLevel.P1,
    job_status: str = "NORMALIZED",
) -> CurrentPriorityQueueItem:
    job = _job(job_id, status=job_status)
    expected = _binding(job, policy)
    if status is CurrentPriorityItemStatus.CURRENT:
        proposal, decision = _artifacts(
            job,
            policy,
            qualification=qualification,
            level=level,
        )
        return CurrentPriorityQueueItem(
            policy.subject_id,
            job,
            status,
            expected,
            expected,
            proposal,
            decision,
            (),
            expected.input_binding,
        )
    if status is CurrentPriorityItemStatus.STALE:
        stored = replace(expected, job_content_hash=_digest("old-job"))
        return CurrentPriorityQueueItem(
            policy.subject_id,
            job,
            status,
            expected,
            stored,
            None,
            None,
            (CurrentPriorityStaleReason.JOB_CONTENT_CHANGED,),
            stored.input_binding,
        )
    if status is CurrentPriorityItemStatus.MISSING:
        return CurrentPriorityQueueItem(
            policy.subject_id,
            job,
            status,
            expected,
            None,
            None,
            None,
            (),
            None,
        )
    return CurrentPriorityQueueItem(
        policy.subject_id,
        job,
        status,
        expected,
        expected,
        None,
        None,
        (),
        expected.input_binding,
    )


def _queue(
    policy: PrioritizationPolicy,
    items: tuple[CurrentPriorityQueueItem, ...],
) -> CurrentPriorityQueueResult:
    return CurrentPriorityQueueResult(
        status=CurrentPriorityQueueStatus.SUCCEEDED,
        reason_code=None,
        retryable=False,
        subject_id=policy.subject_id,
        policy_snapshot=policy,
        items=items,
        message="Synthetic current queue.",
    )


def _intent(
    job_id: str,
    *,
    subject_id: str = SUBJECT,
    value: JobIntakeIntent = JobIntakeIntent.REQUEST_APPLICATION,
) -> AcceptedJobIntent:
    proposal_id = f"intake-{subject_id}-{job_id}-{value.value}"
    return AcceptedJobIntent.create(
        subject_id=subject_id,
        job_id=job_id,
        intent=value,
        intake_proposal_id=proposal_id,
        discovery_run_id=f"run-{subject_id}-{job_id}-{value.value}",
        recorded_at=NOW,
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType.CONVERSATIONAL_INTAKE,
            source_id=proposal_id,
        ),
    )


class _QueueReader:
    def __init__(self, result: CurrentPriorityQueueResult) -> None:
        self.result = result
        self.calls: list[Any] = []

    async def __call__(self, command: Any) -> CurrentPriorityQueueResult:
        self.calls.append(command)
        return self.result


class _IntentRepository:
    def __init__(
        self,
        records: tuple[AcceptedJobIntent, ...] = (),
        *,
        integrity_jobs: tuple[str, ...] = (),
    ) -> None:
        self.records = {
            (record.subject_id, record.job_id): record for record in records
        }
        self.integrity_jobs = set(integrity_jobs)
        self.calls: list[tuple[str, str]] = []

    def get_current(
        self,
        *,
        subject_id: str,
        job_id: str,
    ) -> AcceptedJobIntentReadResult:
        self.calls.append((subject_id, job_id))
        if job_id in self.integrity_jobs:
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.INTEGRITY_FAILURE,
                intent=None,
                reason_code=AcceptedJobIntentFailureReason.INTEGRITY_FAILURE,
            )
        record = self.records.get((subject_id, job_id))
        return AcceptedJobIntentReadResult(
            status=(
                AcceptedJobIntentReadStatus.FOUND
                if record
                else AcceptedJobIntentReadStatus.NOT_FOUND
            ),
            intent=record,
        )

    def save(self, _intent: AcceptedJobIntent) -> Any:
        raise AssertionError("runnable queue must never save intent")


async def _build(
    policy: PrioritizationPolicy,
    items: tuple[CurrentPriorityQueueItem, ...],
    repository: _IntentRepository,
    *,
    now: datetime = NOW,
) -> tuple[Any, _QueueReader]:
    reader = _QueueReader(_queue(policy, items))
    result = await build_runnable_application_queue(
        RunnableApplicationQueueCommand(policy.subject_id, now),
        priority_queue_reader=reader,
        accepted_intent_repository=repository,
    )
    return result, reader


@pytest.mark.asyncio
async def test_current_request_application_and_direct_priority_is_runnable() -> None:
    policy = _policy()
    item = _queue_item("job-runnable", policy, level=ProposedPriorityLevel.P1)
    result, _ = await _build(
        policy,
        (item,),
        _IntentRepository((_intent(item.job.job_id),)),
    )

    assert result.status is RunnableApplicationQueueStatus.SUCCEEDED
    assert result.items[0].runnable_status is RunnableApplicationStatus.RUNNABLE
    assert result.runnable_items == result.items
    assert result.items[0].priority_decision is item.decision


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [JobIntakeIntent.ADD_JOB, None])
async def test_high_priority_without_request_application_is_blocked(
    accepted: JobIntakeIntent | None,
) -> None:
    policy = _policy()
    item = _queue_item("job-no-request", policy, level=ProposedPriorityLevel.P0)
    records = () if accepted is None else (
        _intent(item.job.job_id, value=accepted),
    )
    result, _ = await _build(policy, (item,), _IntentRepository(records))

    assert (
        result.items[0].runnable_status
        is RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT
    )
    assert not result.runnable_items


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "queue_status",
    (
        CurrentPriorityItemStatus.STALE,
        CurrentPriorityItemStatus.MISSING,
        CurrentPriorityItemStatus.INCOMPLETE,
    ),
)
async def test_non_current_priority_states_are_blocked_without_recalculation(
    queue_status: CurrentPriorityItemStatus,
) -> None:
    policy = _policy()
    item = _queue_item("job-not-current", policy, status=queue_status)
    result, _ = await _build(
        policy,
        (item,),
        _IntentRepository((_intent(item.job.job_id),)),
    )

    projected = result.items[0]
    assert (
        projected.runnable_status
        is RunnableApplicationStatus.BLOCKED_NOT_CURRENT
    )
    assert projected.priority_decision is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("qualification", "status"),
    (
        (
            PriorityQualification.NEEDS_USER,
            RunnableApplicationStatus.BLOCKED_NEEDS_USER,
        ),
        (
            PriorityQualification.EXCLUDED,
            RunnableApplicationStatus.BLOCKED_EXCLUDED,
        ),
    ),
)
async def test_needs_user_and_excluded_are_typed_blocks(
    qualification: PriorityQualification,
    status: RunnableApplicationStatus,
) -> None:
    policy = _policy()
    item = _queue_item(
        f"job-{qualification.value.casefold()}",
        policy,
        qualification=qualification,
        level=None,
    )
    result, _ = await _build(
        policy,
        (item,),
        _IntentRepository((_intent(item.job.job_id),)),
    )
    assert result.items[0].runnable_status is status


@pytest.mark.asyncio
async def test_promotion_and_unadmitted_priorities_remain_distinct() -> None:
    policy = _policy(
        admission=_admission(
            direct=(PreparationPriority.P0,),
            promotion=(PreparationPriority.P3,),
        )
    )
    promotion = _queue_item(
        "job-promote", policy, level=ProposedPriorityLevel.P3
    )
    blocked = _queue_item(
        "job-not-admitted", policy, level=ProposedPriorityLevel.P2
    )
    result, _ = await _build(
        policy,
        (promotion, blocked),
        _IntentRepository(
            (_intent(promotion.job.job_id), _intent(blocked.job.job_id))
        ),
    )

    assert [item.runnable_status for item in result.items] == [
        RunnableApplicationStatus.BLOCKED_PROMOTION_REQUIRED,
        RunnableApplicationStatus.BLOCKED_PRIORITY,
    ]


@pytest.mark.asyncio
async def test_unavailable_job_lifecycle_is_blocked() -> None:
    policy = _policy()
    item = _queue_item(
        "job-expired",
        policy,
        level=ProposedPriorityLevel.P0,
        job_status="EXPIRED",
    )
    result, _ = await _build(
        policy,
        (item,),
        _IntentRepository((_intent(item.job.job_id),)),
    )
    assert (
        result.items[0].runnable_status
        is RunnableApplicationStatus.BLOCKED_JOB_STATE
    )


@pytest.mark.asyncio
async def test_intent_integrity_failure_fails_entire_read_model() -> None:
    policy = _policy()
    item = _queue_item("job-corrupt-intent", policy)
    result, _ = await _build(
        policy,
        (item,),
        _IntentRepository(integrity_jobs=(item.job.job_id,)),
    )

    assert result.status is RunnableApplicationQueueStatus.FAILED
    assert (
        result.reason_code
        is RunnableApplicationQueueReason.INTENT_INTEGRITY_FAILURE
    )
    assert not result.items


@pytest.mark.asyncio
async def test_subject_intents_are_isolated() -> None:
    policy = _policy()
    item = _queue_item("job-shared", policy)
    other_intent = _intent(item.job.job_id, subject_id="other-subject")
    repository = _IntentRepository((other_intent,))
    result, _ = await _build(policy, (item,), repository)

    assert (
        result.items[0].runnable_status
        is RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT
    )
    assert repository.calls == [(SUBJECT, item.job.job_id)]


@pytest.mark.asyncio
async def test_snapshot_order_policy_identity_reader_count_and_now_are_preserved() -> None:
    policy = _policy()
    items = (
        _queue_item("job-p0", policy, level=ProposedPriorityLevel.P0),
        _queue_item("job-p1", policy, level=ProposedPriorityLevel.P1),
        _queue_item(
            "job-stale",
            policy,
            status=CurrentPriorityItemStatus.STALE,
        ),
    )
    repository = _IntentRepository(
        tuple(_intent(item.job.job_id) for item in items)
    )
    result, reader = await _build(policy, items, repository)

    assert [item.job.job_id for item in result.items] == [
        item.job.job_id for item in items
    ]
    assert len(reader.calls) == 1
    assert reader.calls[0].now is NOW
    assert result.now is NOW
    assert result.policy_snapshot is policy
    assert result.priority_queue_result is reader.result
    assert reader.result.policy_snapshot is policy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    (
        RunnableApplicationQueueCommand("", NOW),
        RunnableApplicationQueueCommand(
            SUBJECT, datetime(2026, 7, 28, 18, 0)
        ),
    ),
)
async def test_invalid_command_fails_before_queue_or_intent_read(
    command: RunnableApplicationQueueCommand,
) -> None:
    policy = _policy()
    reader = _QueueReader(_queue(policy, ()))
    repository = _IntentRepository()
    result = await build_runnable_application_queue(
        command,
        priority_queue_reader=reader,
        accepted_intent_repository=repository,
    )

    assert result.status is RunnableApplicationQueueStatus.FAILED
    assert result.reason_code is RunnableApplicationQueueReason.INVALID_REQUEST
    assert not reader.calls
    assert not repository.calls


@pytest.mark.asyncio
async def test_priority_queue_failure_is_not_downgraded_to_blocked_items() -> None:
    failure = CurrentPriorityQueueResult(
        status=CurrentPriorityQueueStatus.FAILED,
        reason_code=CurrentPriorityQueueReason.ACTIVE_POLICY_NOT_FOUND,
        retryable=False,
        subject_id=SUBJECT,
        policy_snapshot=None,
        items=(),
        message="Synthetic missing policy.",
    )
    reader = _QueueReader(failure)
    repository = _IntentRepository()
    result = await build_runnable_application_queue(
        RunnableApplicationQueueCommand(SUBJECT, NOW),
        priority_queue_reader=reader,
        accepted_intent_repository=repository,
    )
    assert (
        result.reason_code
        is RunnableApplicationQueueReason.PRIORITY_QUEUE_FAILED
    )
    assert not result.items
    assert not repository.calls


def test_runnable_read_model_imports_no_write_or_execution_layers() -> None:
    path = Path("core/runnable_application_queue.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_symbols = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert not imported_symbols.intersection(
        {
            "PriorityAgentPort",
            "create_priority_proposal",
            "finalize_priority_proposal",
            "orchestrate_single_job_priority",
            "selectively_reprioritize_jobs",
            "ApplicationPlan",
        }
    )
    assert not any(
        fragment in module
        for module in imported_modules
        for fragment in (
            "single_job_priority",
            "selective_reprioritization",
            "browser",
            "ats",
            "dashboard",
            "application_engine",
        )
    )
    assert not calls.intersection(
        {
            "save",
            "claim",
            "complete",
            "evaluate",
            "create_priority_proposal",
            "finalize_priority_proposal",
            "orchestrate_single_job_priority",
            "selectively_reprioritize_jobs",
        }
    )
