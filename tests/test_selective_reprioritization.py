from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from core.current_priority_queue import (
    CurrentPriorityItemStatus,
    CurrentPriorityQueueCommand,
    CurrentPriorityQueueReason,
    CurrentPriorityQueueResult,
    CurrentPriorityQueueStatus,
    build_current_priority_queue,
)
from core.job_discovery import JobPosting
from core.job_prioritization import (
    CandidateFact,
    CandidateFactCategory,
    CandidateSummary,
    EvidenceRef,
    EvidenceSourceType,
    EligibilityCategory,
    EligibilityFinding,
    EligibilityFindingResult,
    EligibilityImpact,
    HardConstraintFinding,
    HardConstraintFindingResult,
    PriorityAgentMetadata,
    PriorityAgentOutput,
    PriorityContext,
    PriorityRationale,
    ProposalConfidence,
    ProposedPriorityLevel,
    ProposedQualification,
    PrivateHomePriorityDecisionRepository,
    RationaleCategory,
    build_candidate_summary,
)
from core.private_home import PrivateHome
from core.prioritization_policy import (
    HardConstraint,
    HardConstraintType,
    PreferenceImportance,
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
    SoftPreference,
    SoftPreferenceCategory,
    default_preparation_admission_policy,
    policy_content_hash,
)
from core.selective_reprioritization import (
    SelectiveBatchExecutionStatus,
    SelectiveBatchOverallStatus,
    SelectiveBatchReason,
    SelectiveBatchReprioritizationCommand,
    selectively_reprioritize_jobs,
)
from core.single_job_priority import (
    PrivateHomeSingleJobPriorityRepository,
    SingleJobPriorityCommand,
    SingleJobPriorityReason,
    SingleJobPriorityResult,
    SingleJobPriorityStatus,
    build_single_job_priority_binding,
    orchestrate_single_job_priority,
)


NOW = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)
SUBJECT = "synthetic-subject-batch"


class FakeJobRepository:
    def __init__(self, jobs: tuple[JobPosting, ...]) -> None:
        self.jobs = {job.job_id: job for job in jobs}

    def get(self, job_id: str) -> JobPosting | None:
        return self.jobs.get(job_id)

    def list_current(self) -> tuple[JobPosting, ...]:
        return tuple(self.jobs.values())


class FakePolicyProvider:
    def __init__(
        self,
        policies: dict[str, PrioritizationPolicy | None],
    ) -> None:
        self.policies = policies

    def get_active_policy(
        self,
        subject_id: str,
    ) -> PrioritizationPolicy | None:
        return self.policies.get(subject_id)


class FakeSummaryProvider:
    def __init__(self, summaries: dict[str, CandidateSummary]) -> None:
        self.summaries = summaries

    def get_current(
        self,
        subject_id: str,
        *,
        now: datetime,
    ) -> CandidateSummary:
        assert now is NOW
        return self.summaries[subject_id]


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[PriorityContext] = []

    async def evaluate(self, context: PriorityContext) -> PriorityAgentOutput:
        self.calls.append(context)
        hard = context.policy.hard_constraints[0]
        soft = context.policy.soft_preferences[0]
        fact = context.candidate.facts[0]
        return PriorityAgentOutput(
            proposed_qualification=ProposedQualification.QUALIFIED,
            proposed_priority_level=ProposedPriorityLevel.P1,
            confidence=ProposalConfidence.HIGH,
            summary="Synthetic batch prioritization recommendation.",
            positive_signals=(
                PriorityRationale(
                    signal_id=f"signal-{context.job.job_id}",
                    category=RationaleCategory.DOMAIN,
                    explanation="The verified fact supports the role.",
                    evidence_refs=(
                        EvidenceRef(
                            source_type=(
                                EvidenceSourceType.POLICY_SOFT_PREFERENCE
                            ),
                            source_id=soft.preference_id,
                        ),
                        EvidenceRef(
                            source_type=EvidenceSourceType.CANDIDATE_FACT,
                            source_id=fact.fact_id,
                        ),
                    ),
                ),
            ),
            concerns=(),
            hard_constraint_findings=(
                HardConstraintFinding(
                    constraint_id=hard.constraint_id,
                    result=HardConstraintFindingResult.NOT_MATCHED,
                    explanation="The role is not in the excluded country.",
                    evidence_refs=(
                        EvidenceRef(
                            source_type=(
                                EvidenceSourceType.POLICY_HARD_CONSTRAINT
                            ),
                            source_id=hard.constraint_id,
                        ),
                        EvidenceRef(
                            source_type=EvidenceSourceType.JOB_FIELD,
                            source_id=context.job.job_id,
                            field="location",
                        ),
                    ),
                ),
            ),
            eligibility_findings=tuple(
                EligibilityFinding(
                    category=category,
                    result=EligibilityFindingResult.NOT_APPLICABLE,
                    impact=EligibilityImpact.NONE,
                    explanation="No explicit requirement was provided.",
                    evidence_refs=(),
                )
                for category in EligibilityCategory
            ),
            missing_information=(),
            questions_for_user=(),
        )


class RecordingQueueReader:
    def __init__(self, result: CurrentPriorityQueueResult) -> None:
        self.result = result
        self.calls: list[CurrentPriorityQueueCommand] = []

    async def __call__(
        self,
        command: CurrentPriorityQueueCommand,
    ) -> CurrentPriorityQueueResult:
        self.calls.append(command)
        return self.result


class RecordingSingleOrchestrator:
    def __init__(
        self,
        delegate: Callable[
            [SingleJobPriorityCommand],
            Awaitable[SingleJobPriorityResult],
        ],
    ) -> None:
        self.delegate = delegate
        self.calls: list[SingleJobPriorityCommand] = []
        self.active = 0
        self.max_active = 0

    async def __call__(
        self,
        command: SingleJobPriorityCommand,
    ) -> SingleJobPriorityResult:
        self.calls.append(command)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            return await self.delegate(command)
        finally:
            self.active -= 1


def _metadata() -> PriorityAgentMetadata:
    return PriorityAgentMetadata(
        agent_version="priority-agent-v1",
        prompt_version="priority-agent-prompt-v1",
        model_id="synthetic-model",
    )


def _job(
    job_id: str,
    *,
    revision: int = 1,
    content_hash: str,
) -> JobPosting:
    return JobPosting(
        schema_version="1.0",
        job_id=job_id,
        revision=revision,
        source_platform="greenhouse",
        source_job_id=f"source-{job_id}",
        source_url=f"https://boards.greenhouse.io/example/jobs/{job_id}",
        company="Synthetic Earth",
        title="Machine Learning Engineer",
        location="Vancouver, Canada",
        work_mode="HYBRID",
        posted_at="2026-07-25T18:00:00Z",
        observed_at="2026-07-27T17:30:00Z",
        application_url=None,
        ats_type="greenhouse",
        description="Build synthetic environmental monitoring systems.",
        content_hash=content_hash,
        status="NORMALIZED",
    )


def _policy() -> PrioritizationPolicy:
    hard = (
        HardConstraint(
            constraint_type=HardConstraintType.EXCLUDED_COUNTRY,
            normalized_value="united states",
            source_excerpt="Do not apply in the United States.",
            user_confirmed=True,
        ),
    )
    soft = (
        SoftPreference(
            preference_id="preference-earth",
            category=SoftPreferenceCategory.DOMAIN,
            statement="Prioritize AI for Earth roles.",
            source_excerpt="Prioritize AI for Earth roles.",
            importance=PreferenceImportance.HIGH,
        ),
    )
    raw = "Prioritize AI for Earth. Do not apply in the United States."
    admission = default_preparation_admission_policy()
    return PrioritizationPolicy(
        policy_id="prioritization-policy-batch-v000001",
        subject_id=SUBJECT,
        policy_version=1,
        policy_content_hash=policy_content_hash(
            raw_preference_text=raw,
            hard_constraints=hard,
            soft_preferences=soft,
            preparation_admission=admission,
        ),
        raw_preference_text=raw,
        hard_constraints=hard,
        soft_preferences=soft,
        preparation_admission=admission,
        status=PrioritizationPolicyStatus.ACTIVE,
        created_at=NOW - timedelta(days=2),
        approved_at=NOW - timedelta(days=1),
        interpreter_version="synthetic-interpreter-v1",
    )


def _summary() -> CandidateSummary:
    fact = CandidateFact(
        fact_id="fact-batch-domain",
        category=CandidateFactCategory.DOMAIN,
        statement="Has verified environmental monitoring experience.",
        source="synthetic-user-confirmation",
        verified=True,
        prioritization_safe=True,
        scope="global",
        confirmed_at=NOW - timedelta(days=3),
    )
    return build_candidate_summary(
        subject_id=SUBJECT,
        candidate_summary_version="candidate-summary-batch-v1",
        facts=(fact,),
        created_at=NOW,
    )


def _services(tmp_path: Path) -> dict[str, Any]:
    jobs = (
        _job("job-current", content_hash="1" * 64),
        _job("job-stale", content_hash="2" * 64),
        _job("job-missing", content_hash="3" * 64),
        _job("job-incomplete", content_hash="4" * 64),
    )
    home = PrivateHome(tmp_path / "private")
    policy = _policy()
    summary = _summary()
    return {
        "home": home,
        "job_repository": FakeJobRepository(jobs),
        "policy_provider": FakePolicyProvider({SUBJECT: policy}),
        "candidate_summary_provider": FakeSummaryProvider(
            {SUBJECT: summary}
        ),
        "orchestration_repository": (
            PrivateHomeSingleJobPriorityRepository(home)
        ),
        "decision_repository": (
            PrivateHomePriorityDecisionRepository(home)
        ),
        "agent": FakeAgent(),
        "metadata": _metadata(),
    }


async def _real_single(
    services: dict[str, Any],
    command: SingleJobPriorityCommand,
) -> SingleJobPriorityResult:
    return await orchestrate_single_job_priority(
        command,
        job_repository=services["job_repository"],
        policy_provider=services["policy_provider"],
        candidate_summary_provider=services[
            "candidate_summary_provider"
        ],
        orchestration_repository=services[
            "orchestration_repository"
        ],
        decision_repository=services["decision_repository"],
        agent=services["agent"],
        metadata=services["metadata"],
    )


async def _real_queue(
    services: dict[str, Any],
    command: CurrentPriorityQueueCommand,
) -> CurrentPriorityQueueResult:
    return await build_current_priority_queue(
        command,
        job_repository=services["job_repository"],
        policy_provider=services["policy_provider"],
        candidate_summary_provider=services[
            "candidate_summary_provider"
        ],
        orchestration_repository=services[
            "orchestration_repository"
        ],
        decision_repository=services["decision_repository"],
        metadata=services["metadata"],
    )


async def _mixed_snapshot(
    services: dict[str, Any],
) -> CurrentPriorityQueueResult:
    await _real_single(
        services,
        SingleJobPriorityCommand(SUBJECT, "job-current", NOW),
    )
    await _real_single(
        services,
        SingleJobPriorityCommand(SUBJECT, "job-stale", NOW),
    )
    services["job_repository"].jobs["job-stale"] = _job(
        "job-stale",
        revision=2,
        content_hash="5" * 64,
    )
    incomplete_job = services["job_repository"].jobs["job-incomplete"]
    binding = build_single_job_priority_binding(
        subject_id=SUBJECT,
        job=incomplete_job,
        policy=services["policy_provider"].policies[SUBJECT],
        candidate_summary=services[
            "candidate_summary_provider"
        ].summaries[SUBJECT],
        metadata=services["metadata"],
        now=NOW,
    )
    services["orchestration_repository"].claim(binding)
    return await _real_queue(
        services,
        CurrentPriorityQueueCommand(SUBJECT, NOW),
    )


def _typed_single_failure(job_id: str) -> SingleJobPriorityResult:
    return SingleJobPriorityResult(
        status=SingleJobPriorityStatus.FAILED,
        change=None,
        reason_code=SingleJobPriorityReason.PROPOSAL_FAILED,
        retryable=False,
        subject_id=SUBJECT,
        job_id=job_id,
        input_binding=None,
        proposal_outcome=None,
        decision_outcome=None,
        proposal_result=None,
        decision_result=None,
        message="Synthetic P1d1 failure.",
    )


@pytest.mark.asyncio
async def test_only_stale_and_missing_execute_while_other_states_skip(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    snapshot = await _mixed_snapshot(services)
    queue_reader = RecordingQueueReader(snapshot)
    orchestrator = RecordingSingleOrchestrator(
        lambda command: _real_single(services, command)
    )
    initial_agent_calls = len(services["agent"].calls)

    result = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=(
                "job-current",
                "job-stale",
                "job-missing",
                "job-incomplete",
            ),
        ),
        queue_reader=queue_reader,
        single_job_orchestrator=orchestrator,
    )

    assert [item.execution_status for item in result.items] == [
        SelectiveBatchExecutionStatus.SKIPPED_CURRENT,
        SelectiveBatchExecutionStatus.CREATED,
        SelectiveBatchExecutionStatus.CREATED,
        SelectiveBatchExecutionStatus.SKIPPED_INCOMPLETE,
    ]
    assert [call.job_id for call in orchestrator.calls] == [
        "job-stale",
        "job-missing",
    ]
    assert len(services["agent"].calls) == initial_agent_calls + 2
    assert result.overall_status is SelectiveBatchOverallStatus.COMPLETED
    assert result.summary.selected == 2
    assert result.summary.skipped_current == 1
    assert result.summary.skipped_incomplete == 1


@pytest.mark.asyncio
async def test_explicit_allowlist_preserves_order_deduplicates_and_not_found(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    snapshot = await _mixed_snapshot(services)
    queue_reader = RecordingQueueReader(snapshot)
    orchestrator = RecordingSingleOrchestrator(
        lambda command: _real_single(services, command)
    )

    result = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=(
                "job-missing",
                "job-current",
                "job-missing",
                "job-absent",
                "job-stale",
                "job-incomplete",
            ),
        ),
        queue_reader=queue_reader,
        single_job_orchestrator=orchestrator,
    )

    assert result.requested_job_ids == (
        "job-missing",
        "job-current",
        "job-absent",
        "job-stale",
        "job-incomplete",
    )
    assert [item.job_id for item in result.items] == list(
        result.requested_job_ids
    )
    assert [call.job_id for call in orchestrator.calls] == [
        "job-missing",
        "job-stale",
    ]
    assert result.items[2].execution_status is (
        SelectiveBatchExecutionStatus.NOT_FOUND
    )
    assert result.summary.not_found == 1


@pytest.mark.asyncio
async def test_max_jobs_uses_p1d2_order_and_truncates_deduplicated_allowlist(
    tmp_path: Path,
) -> None:
    auto_services = _services(tmp_path / "auto")
    auto_snapshot = await _mixed_snapshot(auto_services)
    auto_orchestrator = RecordingSingleOrchestrator(
        lambda command: _real_single(auto_services, command)
    )
    automatic = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            max_jobs=1,
        ),
        queue_reader=RecordingQueueReader(auto_snapshot),
        single_job_orchestrator=auto_orchestrator,
    )
    assert [item.job_id for item in automatic.items] == ["job-stale"]
    assert [call.job_id for call in auto_orchestrator.calls] == [
        "job-stale"
    ]

    explicit_services = _services(tmp_path / "explicit")
    explicit_snapshot = await _mixed_snapshot(explicit_services)
    explicit_orchestrator = RecordingSingleOrchestrator(
        lambda command: _real_single(explicit_services, command)
    )
    explicit = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=(
                "job-missing",
                "job-missing",
                "job-stale",
                "job-absent",
            ),
            max_jobs=2,
        ),
        queue_reader=RecordingQueueReader(explicit_snapshot),
        single_job_orchestrator=explicit_orchestrator,
    )
    assert explicit.requested_job_ids == (
        "job-missing",
        "job-stale",
        "job-absent",
    )
    assert [item.job_id for item in explicit.items] == [
        "job-missing",
        "job-stale",
    ]
    assert [call.job_id for call in explicit_orchestrator.calls] == [
        "job-missing",
        "job-stale",
    ]


@pytest.mark.asyncio
async def test_execution_is_serial_and_forwards_identical_now(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    snapshot = await _mixed_snapshot(services)
    queue_reader = RecordingQueueReader(snapshot)
    orchestrator = RecordingSingleOrchestrator(
        lambda command: _real_single(services, command)
    )

    await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=("job-stale", "job-missing"),
        ),
        queue_reader=queue_reader,
        single_job_orchestrator=orchestrator,
    )

    assert len(queue_reader.calls) == 1
    assert queue_reader.calls[0].now is NOW
    assert orchestrator.max_active == 1
    assert all(call.now is NOW for call in orchestrator.calls)


@pytest.mark.asyncio
async def test_created_and_unchanged_are_aggregated_without_changing_result(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    snapshot = await _mixed_snapshot(services)
    await _real_single(
        services,
        SingleJobPriorityCommand(SUBJECT, "job-missing", NOW),
    )
    orchestrator = RecordingSingleOrchestrator(
        lambda command: _real_single(services, command)
    )

    result = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=("job-stale", "job-missing"),
        ),
        queue_reader=RecordingQueueReader(snapshot),
        single_job_orchestrator=orchestrator,
    )

    assert [item.execution_status for item in result.items] == [
        SelectiveBatchExecutionStatus.CREATED,
        SelectiveBatchExecutionStatus.UNCHANGED,
    ]
    assert result.summary.created == 1
    assert result.summary.unchanged == 1
    assert result.items[1].single_job_result is not None
    assert result.items[1].single_job_result.change.value == "UNCHANGED"


@pytest.mark.asyncio
async def test_single_item_failure_isolated_and_later_job_still_runs(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    snapshot = await _mixed_snapshot(services)

    async def delegate(
        command: SingleJobPriorityCommand,
    ) -> SingleJobPriorityResult:
        if command.job_id == "job-missing":
            return _typed_single_failure(command.job_id)
        return await _real_single(services, command)

    orchestrator = RecordingSingleOrchestrator(delegate)
    result = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=("job-stale", "job-missing", "job-stale"),
        ),
        queue_reader=RecordingQueueReader(snapshot),
        single_job_orchestrator=orchestrator,
    )

    assert [call.job_id for call in orchestrator.calls] == [
        "job-stale",
        "job-missing",
    ]
    assert [item.execution_status for item in result.items] == [
        SelectiveBatchExecutionStatus.CREATED,
        SelectiveBatchExecutionStatus.FAILED,
    ]
    assert result.overall_status is (
        SelectiveBatchOverallStatus.PARTIAL_FAILURE
    )
    assert result.reason_code is SelectiveBatchReason.ITEM_FAILURE
    assert result.items[1].failure is not None
    assert result.items[1].failure.single_job_reason is (
        SingleJobPriorityReason.PROPOSAL_FAILED
    )


@pytest.mark.asyncio
async def test_all_executions_failed_returns_failed_overall_status(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    snapshot = await _mixed_snapshot(services)

    async def failed(
        command: SingleJobPriorityCommand,
    ) -> SingleJobPriorityResult:
        return _typed_single_failure(command.job_id)

    orchestrator = RecordingSingleOrchestrator(failed)
    result = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=("job-stale", "job-missing"),
        ),
        queue_reader=RecordingQueueReader(snapshot),
        single_job_orchestrator=orchestrator,
    )

    assert result.overall_status is SelectiveBatchOverallStatus.FAILED
    assert result.reason_code is (
        SelectiveBatchReason.ALL_EXECUTIONS_FAILED
    )
    assert result.summary.failed == 2
    assert [call.job_id for call in orchestrator.calls] == [
        "job-stale",
        "job-missing",
    ]


@pytest.mark.asyncio
async def test_failure_between_two_successes_continues_to_third_job(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    snapshot = await _mixed_snapshot(services)
    snapshot_items = tuple(
        item
        for item in snapshot.items
        if item.status in (
            CurrentPriorityItemStatus.STALE,
            CurrentPriorityItemStatus.MISSING,
        )
    )
    repeated_missing = snapshot_items[1]
    synthetic_third = replace_queue_job(
        repeated_missing,
        _job("job-third", content_hash="6" * 64),
    )
    services["job_repository"].jobs["job-third"] = synthetic_third.job
    extended = CurrentPriorityQueueResult(
        status=CurrentPriorityQueueStatus.SUCCEEDED,
        reason_code=None,
        retryable=False,
        subject_id=SUBJECT,
        policy_snapshot=snapshot.policy_snapshot,
        items=snapshot.items + (synthetic_third,),
        message="Synthetic extended queue.",
    )

    async def delegate(
        command: SingleJobPriorityCommand,
    ) -> SingleJobPriorityResult:
        if command.job_id == "job-missing":
            return _typed_single_failure(command.job_id)
        return await _real_single(services, command)

    orchestrator = RecordingSingleOrchestrator(delegate)
    result = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=("job-stale", "job-missing", "job-third"),
        ),
        queue_reader=RecordingQueueReader(extended),
        single_job_orchestrator=orchestrator,
    )

    assert [call.job_id for call in orchestrator.calls] == [
        "job-stale",
        "job-missing",
        "job-third",
    ]
    assert [item.execution_status for item in result.items] == [
        SelectiveBatchExecutionStatus.CREATED,
        SelectiveBatchExecutionStatus.FAILED,
        SelectiveBatchExecutionStatus.CREATED,
    ]
    assert result.overall_status is (
        SelectiveBatchOverallStatus.PARTIAL_FAILURE
    )


def replace_queue_job(item: Any, job: JobPosting) -> Any:
    expected = replace(
        item.expected_binding,
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
    )
    return replace(
        item,
        job=job,
        expected_binding=expected,
    )


@pytest.mark.asyncio
async def test_queue_failure_stops_before_any_single_job_call() -> None:
    queue_failure = CurrentPriorityQueueResult(
        status=CurrentPriorityQueueStatus.FAILED,
        reason_code=CurrentPriorityQueueReason.ACTIVE_POLICY_NOT_FOUND,
        retryable=False,
        subject_id=SUBJECT,
        policy_snapshot=None,
        items=(),
        message="No ACTIVE policy.",
    )
    queue_reader = RecordingQueueReader(queue_failure)

    async def forbidden(_command: SingleJobPriorityCommand) -> Any:
        raise AssertionError("P1d1 must not run")

    result = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            max_jobs=1,
        ),
        queue_reader=queue_reader,
        single_job_orchestrator=forbidden,
    )

    assert result.overall_status is SelectiveBatchOverallStatus.FAILED
    assert result.reason_code is SelectiveBatchReason.QUEUE_BUILD_FAILED
    assert result.queue_failure is queue_failure
    assert result.summary.selected == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=datetime(2026, 7, 27, 18, 0),
            max_jobs=1,
        ),
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
        ),
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=(),
        ),
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            max_jobs=0,
        ),
    ],
)
async def test_invalid_command_fails_before_queue_or_single_job(
    command: SelectiveBatchReprioritizationCommand,
) -> None:
    calls = {"queue": 0, "single": 0}

    async def queue(_command: CurrentPriorityQueueCommand) -> Any:
        calls["queue"] += 1
        raise AssertionError("queue must not run")

    async def single(_command: SingleJobPriorityCommand) -> Any:
        calls["single"] += 1
        raise AssertionError("single job must not run")

    result = await selectively_reprioritize_jobs(
        command,
        queue_reader=queue,
        single_job_orchestrator=single,
    )

    assert result.overall_status is SelectiveBatchOverallStatus.FAILED
    assert result.reason_code is SelectiveBatchReason.INVALID_COMMAND
    assert calls == {"queue": 0, "single": 0}


@pytest.mark.asyncio
async def test_no_executable_items_returns_noop_without_single_job_call(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    snapshot = await _mixed_snapshot(services)
    queue_reader = RecordingQueueReader(snapshot)

    async def forbidden(_command: SingleJobPriorityCommand) -> Any:
        raise AssertionError("P1d1 must not run")

    result = await selectively_reprioritize_jobs(
        SelectiveBatchReprioritizationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=("job-current", "job-incomplete", "job-absent"),
        ),
        queue_reader=queue_reader,
        single_job_orchestrator=forbidden,
    )

    assert result.overall_status is SelectiveBatchOverallStatus.NOOP
    assert result.summary.selected == 0
    assert result.summary.skipped_current == 1
    assert result.summary.skipped_incomplete == 1
    assert result.summary.not_found == 1


@pytest.mark.asyncio
async def test_repeated_real_batch_creates_no_duplicate_or_extra_agent_call(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)

    async def queue(command: CurrentPriorityQueueCommand) -> Any:
        return await _real_queue(services, command)

    async def single(command: SingleJobPriorityCommand) -> Any:
        return await _real_single(services, command)

    command = SelectiveBatchReprioritizationCommand(
        subject_id=SUBJECT,
        now=NOW,
        job_ids=("job-missing",),
    )
    first = await selectively_reprioritize_jobs(
        command,
        queue_reader=queue,
        single_job_orchestrator=single,
    )
    file_counts = (
        len(
            list(
                services["home"].paths.prioritization.glob(
                    "orchestrations/*.json"
                )
            )
        ),
        len(
            list(
                services["home"].paths.priority_decisions.glob(
                    "*/*/*.json"
                )
            )
        ),
    )
    agent_calls = len(services["agent"].calls)
    second = await selectively_reprioritize_jobs(
        command,
        queue_reader=queue,
        single_job_orchestrator=single,
    )

    assert first.items[0].execution_status is (
        SelectiveBatchExecutionStatus.CREATED
    )
    assert second.items[0].execution_status is (
        SelectiveBatchExecutionStatus.SKIPPED_CURRENT
    )
    assert second.overall_status is SelectiveBatchOverallStatus.NOOP
    assert len(services["agent"].calls) == agent_calls
    assert file_counts == (
        len(
            list(
                services["home"].paths.prioritization.glob(
                    "orchestrations/*.json"
                )
            )
        ),
        len(
            list(
                services["home"].paths.priority_decisions.glob(
                    "*/*/*.json"
                )
            )
        ),
    )


def test_batch_module_imports_only_p1d2_and_p1d1_contracts() -> None:
    source = Path("core/selective_reprioritization.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    banned = {
        "PriorityAgentPort",
        "create_priority_proposal",
        "finalize_priority_proposal",
        "build_single_job_priority_binding",
        "PrivateHomeSingleJobPriorityRepository",
        "PrivateHomePriorityDecisionRepository",
        "browser",
        "ats",
        "application",
        "dashboard",
        "tracker",
        "csv",
    }
    assert not imported.intersection(banned)
