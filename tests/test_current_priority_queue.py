from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.current_priority_queue import (
    CurrentPriorityItemStatus,
    CurrentPriorityQueueCommand,
    CurrentPriorityQueueReason,
    CurrentPriorityQueueStatus,
    CurrentPriorityStaleReason,
    build_current_priority_queue,
    priority_binding_stale_reasons,
)
from core.job_discovery import (
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobIntakeIntent,
    JobIntakeProposal,
    JobPosting,
    JobPostingRepositoryError,
    PrivateHomeJobPostingRepository,
    ProposalResolution,
    ResolvedJobCandidate,
    run_discovery,
)
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
from core.profile_store import CandidateSummaryProviderError
from core.single_job_priority import (
    PrivateHomeSingleJobPriorityRepository,
    SingleJobPriorityBinding,
    SingleJobPriorityCommand,
    build_single_job_priority_binding,
    orchestrate_single_job_priority,
)
from core.subject_job_library import (
    SubjectJobPostingItem,
    SubjectJobPostingListResult,
    SubjectJobPostingReadStatus,
)
from core.job_prioritization import PrivateHomePriorityDecisionRepository


NOW = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)
SUBJECT = "synthetic-subject-queue"


def _raw(cls, **values):
    instance = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    return instance


class FakeJobRepository:
    def __init__(self, jobs: list[JobPosting]) -> None:
        self.jobs = jobs
        self.get_calls: list[str] = []
        self.list_calls = 0

    def get(self, job_id: str) -> JobPosting | None:
        self.get_calls.append(job_id)
        return next(
            (job for job in self.jobs if job.job_id == job_id),
            None,
        )

    def list_current(self) -> tuple[JobPosting, ...]:
        self.list_calls += 1
        return tuple(self.jobs)


class FakeSubjectJobReader:
    def __init__(self, repository: FakeJobRepository) -> None:
        self.repository = repository
        self.calls: list[str] = []

    def list_current(self, *, subject_id: str, now: datetime):
        self.calls.append(subject_id)
        items = tuple(
            _raw(
                SubjectJobPostingItem,
                membership=None,
                job_posting=job,
                current_job_revision_ref=f"{job.job_id}:{job.revision}",
                item_hash=job.content_hash,
            )
            for job in self.repository.jobs
        )
        return SubjectJobPostingListResult(
            SubjectJobPostingReadStatus.READY,
            subject_id,
            "a" * 64,
            "b" * 64,
            items,
            now,
        )


class FakePolicyProvider:
    def __init__(
        self,
        policies: dict[str, PrioritizationPolicy | None],
    ) -> None:
        self.policies = policies
        self.calls: list[str] = []

    def get_active_policy(
        self,
        subject_id: str,
    ) -> PrioritizationPolicy | None:
        self.calls.append(subject_id)
        return self.policies.get(subject_id)


class FakeSummaryProvider:
    def __init__(
        self,
        summaries: dict[str, CandidateSummary],
        *,
        fail: bool = False,
    ) -> None:
        self.summaries = summaries
        self.fail = fail
        self.calls: list[tuple[str, datetime]] = []

    def get_current(
        self,
        subject_id: str,
        *,
        now: datetime,
    ) -> CandidateSummary:
        self.calls.append((subject_id, now))
        if self.fail:
            raise CandidateSummaryProviderError("synthetic unavailable")
        return self.summaries[subject_id]


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[PriorityContext] = []

    async def evaluate(self, context: PriorityContext) -> PriorityAgentOutput:
        self.calls.append(context)
        hard = context.policy.hard_constraints[0]
        soft = context.policy.soft_preferences[0]
        fact = context.candidate.facts[0]
        level = {
            "job-queue-p0": ProposedPriorityLevel.P0,
            "job-queue-p2": ProposedPriorityLevel.P2,
        }.get(context.job.job_id, ProposedPriorityLevel.P1)
        return PriorityAgentOutput(
            proposed_qualification=ProposedQualification.QUALIFIED,
            proposed_priority_level=level,
            confidence=ProposalConfidence.HIGH,
            summary="Synthetic current priority recommendation.",
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
                    explanation="The job is not in the excluded country.",
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


class ReadOnlyOrchestrationRepository:
    def __init__(
        self,
        repository: PrivateHomeSingleJobPriorityRepository,
    ) -> None:
        self.repository = repository
        self.list_calls: list[str] = []

    def list_for_subject(self, subject_id: str) -> Any:
        self.list_calls.append(subject_id)
        return self.repository.list_for_subject(subject_id)

    def claim(self, _binding: Any) -> Any:
        raise AssertionError("read model must not claim a binding")

    def complete(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("read model must not complete an orchestration")


class ReadOnlyDecisionRepository:
    def __init__(
        self,
        repository: PrivateHomePriorityDecisionRepository,
    ) -> None:
        self.repository = repository
        self.get_calls: list[tuple[str, str, str]] = []

    def get_decision(
        self,
        *,
        subject_id: str,
        job_id: str,
        decision_id: str,
    ) -> Any:
        self.get_calls.append((subject_id, job_id, decision_id))
        return self.repository.get_decision(
            subject_id=subject_id,
            job_id=job_id,
            decision_id=decision_id,
        )

    def save(self, _decision: Any) -> Any:
        raise AssertionError("read model must not save a decision")


def _metadata(
    *,
    agent: str = "priority-agent-v1",
    prompt: str = "priority-agent-prompt-v1",
    model: str = "synthetic-model",
) -> PriorityAgentMetadata:
    return PriorityAgentMetadata(
        agent_version=agent,
        prompt_version=prompt,
        model_id=model,
    )


def _job(
    job_id: str = "job-queue-p1",
    *,
    revision: int = 1,
    content_hash: str = "a" * 64,
    status: str = "NORMALIZED",
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
        status=status,
    )


def _policy(
    *,
    subject_id: str = SUBJECT,
    version: int = 1,
    statement: str = "Prioritize AI for Earth roles.",
) -> PrioritizationPolicy:
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
            preference_id=f"preference-earth-v{version}",
            category=SoftPreferenceCategory.DOMAIN,
            statement=statement,
            source_excerpt=statement,
            importance=PreferenceImportance.HIGH,
        ),
    )
    raw = f"{statement} Do not apply in the United States."
    admission = default_preparation_admission_policy()
    return PrioritizationPolicy(
        policy_id=f"prioritization-policy-queue-v{version:06d}",
        subject_id=subject_id,
        policy_version=version,
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


def _summary(
    *,
    subject_id: str = SUBJECT,
    version: str = "candidate-summary-v1",
    statement: str = "Has verified environmental monitoring experience.",
) -> CandidateSummary:
    fact = CandidateFact(
        fact_id=f"fact-{version}",
        category=CandidateFactCategory.DOMAIN,
        statement=statement,
        source="synthetic-user-confirmation",
        verified=True,
        prioritization_safe=True,
        scope="global",
        confirmed_at=NOW - timedelta(days=3),
    )
    return build_candidate_summary(
        subject_id=subject_id,
        candidate_summary_version=version,
        facts=(fact,),
        created_at=NOW,
    )


def _services(
    tmp_path: Path,
    *,
    jobs: list[JobPosting] | None = None,
    policy: PrioritizationPolicy | None = None,
    summary: CandidateSummary | None = None,
) -> dict[str, Any]:
    home = PrivateHome(tmp_path / "private")
    selected_policy = policy or _policy()
    selected_summary = summary or _summary()
    return {
        "home": home,
        "job_repository": FakeJobRepository(jobs or [_job()]),
        "policy_provider": FakePolicyProvider(
            {selected_policy.subject_id: selected_policy}
        ),
        "candidate_summary_provider": FakeSummaryProvider(
            {selected_summary.subject_id: selected_summary}
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


async def _seed_completed(
    services: dict[str, Any],
    *,
    subject_id: str = SUBJECT,
    job_id: str = "job-queue-p1",
    now: datetime = NOW,
) -> Any:
    return await orchestrate_single_job_priority(
        SingleJobPriorityCommand(subject_id, job_id, now),
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


async def _build(
    services: dict[str, Any],
    *,
    subject_id: str = SUBJECT,
    now: datetime = NOW,
    metadata: PriorityAgentMetadata | None = None,
    read_only: bool = False,
) -> Any:
    orchestration_repository: Any = services[
        "orchestration_repository"
    ]
    decision_repository: Any = services["decision_repository"]
    if read_only:
        orchestration_repository = ReadOnlyOrchestrationRepository(
            orchestration_repository
        )
        decision_repository = ReadOnlyDecisionRepository(
            decision_repository
        )
    result = await build_current_priority_queue(
        CurrentPriorityQueueCommand(subject_id=subject_id, now=now),
        subject_job_reader=FakeSubjectJobReader(
            services["job_repository"]
        ),
        policy_provider=services["policy_provider"],
        candidate_summary_provider=services[
            "candidate_summary_provider"
        ],
        orchestration_repository=orchestration_repository,
        decision_repository=decision_repository,
        metadata=metadata or services["metadata"],
    )
    return result


@pytest.mark.asyncio
async def test_current_item_returns_existing_artifacts_without_agent_or_writes(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    seeded = await _seed_completed(services)
    assert seeded.proposal is not None and seeded.decision is not None
    agent_calls = len(services["agent"].calls)
    before = {
        path: path.read_bytes()
        for path in services["home"].root.rglob("*.json")
    }

    result = await _build(services, read_only=True)

    assert result.status is CurrentPriorityQueueStatus.SUCCEEDED
    assert (
        result.policy_snapshot
        is services["policy_provider"].policies[SUBJECT]
    )
    assert len(result.items) == 1
    item = result.items[0]
    assert item.status is CurrentPriorityItemStatus.CURRENT
    assert item.proposal == seeded.proposal
    assert item.decision == seeded.decision
    assert item.expected_binding == item.stored_binding
    assert result.current_items == (item,)
    assert len(services["agent"].calls) == agent_calls
    assert before == {
        path: path.read_bytes()
        for path in services["home"].root.rglob("*.json")
    }


@pytest.mark.asyncio
async def test_same_utc_day_clock_advance_keeps_completed_decision_current(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    seeded = await _seed_completed(services)
    agent_calls = len(services["agent"].calls)
    before = {
        path: path.read_bytes()
        for path in services["home"].root.rglob("*.json")
    }

    result = await _build(
        services,
        now=NOW + timedelta(hours=1),
        read_only=True,
    )

    item = result.items[0]
    assert item.status is CurrentPriorityItemStatus.CURRENT
    assert item.expected_binding == item.stored_binding
    assert item.expected_binding.evaluated_at == "2026-07-27T18:00:00Z"
    assert item.proposal == seeded.proposal
    assert item.decision == seeded.decision
    assert len(services["agent"].calls) == agent_calls
    assert before == {
        path: path.read_bytes()
        for path in services["home"].root.rglob("*.json")
    }


@pytest.mark.asyncio
async def test_next_utc_day_makes_completed_decision_stale(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    await _seed_completed(services)

    result = await _build(services, now=NOW + timedelta(days=1))

    item = result.items[0]
    assert item.status is CurrentPriorityItemStatus.STALE
    assert item.stale_reasons == (
        CurrentPriorityStaleReason.EVALUATION_TIME_CHANGED,
    )
    assert item.proposal is None
    assert item.decision is None


@pytest.mark.asyncio
async def test_missing_and_exact_incomplete_are_distinct(
    tmp_path: Path,
) -> None:
    missing_services = _services(tmp_path / "missing")
    missing = await _build(missing_services)
    assert missing.items[0].status is CurrentPriorityItemStatus.MISSING
    assert missing.items[0].proposal is None
    assert missing.items[0].decision is None

    incomplete_services = _services(tmp_path / "incomplete")
    job = incomplete_services["job_repository"].jobs[0]
    binding = build_single_job_priority_binding(
        subject_id=SUBJECT,
        job=job,
        policy=incomplete_services["policy_provider"].policies[SUBJECT],
        candidate_summary=incomplete_services[
            "candidate_summary_provider"
        ].summaries[SUBJECT],
        metadata=incomplete_services["metadata"],
        now=NOW,
    )
    incomplete_services["orchestration_repository"].claim(binding)
    incomplete = await _build(incomplete_services, read_only=True)
    assert incomplete.items[0].status is CurrentPriorityItemStatus.INCOMPLETE
    assert incomplete.items[0].orchestration_id == binding.input_binding
    assert incomplete.items[0].proposal is None
    assert incomplete.items[0].decision is None
    assert not incomplete_services["agent"].calls

    incomplete_services["orchestration_repository"].fail(
        binding,
        reason="synthetic-interrupted",
    )
    failed = await _build(incomplete_services, read_only=True)
    assert failed.items[0].status is CurrentPriorityItemStatus.INCOMPLETE
    assert failed.items[0].orchestration_id == binding.input_binding
    assert failed.items[0].latest_failure_reason == "synthetic-interrupted"
    assert not incomplete_services["agent"].calls

    retryable_view = await _build(
        incomplete_services,
        now=NOW + timedelta(minutes=1),
        read_only=True,
    )
    assert retryable_view.items[0].status is CurrentPriorityItemStatus.MISSING
    assert retryable_view.items[0].orchestration_id is None
    assert retryable_view.items[0].latest_failure_reason == (
        "synthetic-interrupted"
    )

    incomplete_services["job_repository"].jobs[0] = _job(
        revision=2,
        content_hash="9" * 64,
    )
    changed_inputs = await _build(
        incomplete_services,
        now=NOW + timedelta(minutes=2),
        read_only=True,
    )
    assert changed_inputs.items[0].status is CurrentPriorityItemStatus.MISSING
    assert changed_inputs.items[0].latest_failure_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_job", "expected_reason"),
    [
        (
            _job(revision=2),
            CurrentPriorityStaleReason.JOB_REVISION_CHANGED,
        ),
        (
            _job(content_hash="b" * 64),
            CurrentPriorityStaleReason.JOB_CONTENT_CHANGED,
        ),
    ],
)
async def test_job_binding_changes_make_previous_decision_stale(
    tmp_path: Path,
    changed_job: JobPosting,
    expected_reason: CurrentPriorityStaleReason,
) -> None:
    services = _services(tmp_path)
    seeded = await _seed_completed(services)
    services["job_repository"].jobs = [changed_job]

    result = await _build(services)

    item = result.items[0]
    assert item.status is CurrentPriorityItemStatus.STALE
    assert expected_reason in item.stale_reasons
    assert item.proposal is None
    assert item.decision is None
    assert item.orchestration_id == seeded.input_binding


@pytest.mark.asyncio
async def test_policy_and_candidate_changes_are_reported_as_stale(
    tmp_path: Path,
) -> None:
    policy_services = _services(tmp_path / "policy")
    await _seed_completed(policy_services)
    policy_services["policy_provider"].policies[SUBJECT] = _policy(
        version=2,
        statement="Prioritize climate platform roles.",
    )
    policy_result = await _build(policy_services)
    assert (
        CurrentPriorityStaleReason.POLICY_CHANGED
        in policy_result.items[0].stale_reasons
    )

    summary_services = _services(tmp_path / "summary")
    await _seed_completed(summary_services)
    summary_services[
        "candidate_summary_provider"
    ].summaries[SUBJECT] = _summary(
        version="candidate-summary-v2",
        statement="Has verified climate platform experience.",
    )
    summary_result = await _build(summary_services)
    assert (
        CurrentPriorityStaleReason.CANDIDATE_SUMMARY_CHANGED
        in summary_result.items[0].stale_reasons
    )


def test_all_version_stale_reasons_come_from_binding_fields() -> None:
    job = _job()
    policy = _policy()
    summary = _summary()
    stored = build_single_job_priority_binding(
        subject_id=SUBJECT,
        job=job,
        policy=policy,
        candidate_summary=summary,
        metadata=_metadata(
            agent="agent-old",
            prompt="prompt-old",
            model="model-old",
        ),
        now=NOW - timedelta(days=1),
    )
    expected = replace(
        stored,
        agent_version="agent-new",
        prompt_version="prompt-new",
        model_id="model-new",
        evaluated_at="2026-07-27T18:00:00Z",
        validation_version="priority-gate-new",
        orchestration_version="single-job-priority-new",
    )

    assert priority_binding_stale_reasons(
        expected=expected,
        stored=stored,
    ) == (
        CurrentPriorityStaleReason.AGENT_VERSION_CHANGED,
        CurrentPriorityStaleReason.PROMPT_VERSION_CHANGED,
        CurrentPriorityStaleReason.MODEL_VERSION_CHANGED,
        CurrentPriorityStaleReason.EVALUATION_TIME_CHANGED,
        CurrentPriorityStaleReason.GATE_VERSION_CHANGED,
        CurrentPriorityStaleReason.ORCHESTRATION_VERSION_CHANGED,
    )


@pytest.mark.asyncio
async def test_changed_agent_prompt_and_model_are_stale_within_same_utc_day(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    await _seed_completed(services)

    result = await _build(
        services,
        now=NOW + timedelta(hours=1),
        metadata=_metadata(
            agent="priority-agent-v2",
            prompt="priority-agent-prompt-v2",
            model="synthetic-model-v2",
        ),
    )

    assert result.items[0].stale_reasons == (
        CurrentPriorityStaleReason.AGENT_VERSION_CHANGED,
        CurrentPriorityStaleReason.PROMPT_VERSION_CHANGED,
        CurrentPriorityStaleReason.MODEL_VERSION_CHANGED,
    )


@pytest.mark.asyncio
async def test_subject_isolation_never_reuses_other_subject_decision(
    tmp_path: Path,
) -> None:
    other = "synthetic-subject-other"
    services = _services(tmp_path)
    services["policy_provider"].policies[other] = _policy(
        subject_id=other
    )
    services["candidate_summary_provider"].summaries[other] = _summary(
        subject_id=other
    )
    await _seed_completed(services, subject_id=SUBJECT)

    result = await _build(services, subject_id=other)

    assert result.items[0].status is CurrentPriorityItemStatus.MISSING
    assert (
        result.items[0].expected_binding.subject_id == other
    )


@pytest.mark.asyncio
async def test_current_sorting_is_priority_then_timestamp_then_job_id(
    tmp_path: Path,
) -> None:
    jobs = [
        _job("job-queue-p2", content_hash="2" * 64),
        _job("job-queue-p1-b", content_hash="3" * 64),
        _job("job-queue-p0", content_hash="1" * 64),
        _job("job-queue-p1-a", content_hash="4" * 64),
    ]
    services = _services(tmp_path, jobs=jobs)
    for job in jobs:
        await _seed_completed(services, job_id=job.job_id)
    agent_calls = len(services["agent"].calls)
    services["job_repository"].jobs = list(reversed(jobs))

    result = await _build(services, read_only=True)

    assert [item.job.job_id for item in result.current_items] == [
        "job-queue-p0",
        "job-queue-p1-a",
        "job-queue-p1-b",
        "job-queue-p2",
    ]
    assert all(
        item.status is CurrentPriorityItemStatus.CURRENT
        for item in result.items
    )
    assert len(services["agent"].calls) == agent_calls


@pytest.mark.asyncio
async def test_status_groups_are_current_stale_missing_incomplete(
    tmp_path: Path,
) -> None:
    jobs = [
        _job("job-current", content_hash="1" * 64),
        _job("job-stale", content_hash="2" * 64),
        _job("job-missing", content_hash="3" * 64),
        _job("job-incomplete", content_hash="4" * 64),
    ]
    services = _services(tmp_path, jobs=jobs)
    await _seed_completed(services, job_id="job-current")
    await _seed_completed(services, job_id="job-stale")
    jobs[1] = _job(
        "job-stale",
        revision=2,
        content_hash="5" * 64,
    )
    binding = build_single_job_priority_binding(
        subject_id=SUBJECT,
        job=jobs[3],
        policy=services["policy_provider"].policies[SUBJECT],
        candidate_summary=services[
            "candidate_summary_provider"
        ].summaries[SUBJECT],
        metadata=services["metadata"],
        now=NOW,
    )
    services["orchestration_repository"].claim(binding)

    result = await _build(services)

    assert [item.status for item in result.items] == [
        CurrentPriorityItemStatus.CURRENT,
        CurrentPriorityItemStatus.STALE,
        CurrentPriorityItemStatus.MISSING,
        CurrentPriorityItemStatus.INCOMPLETE,
    ]


@pytest.mark.asyncio
async def test_invalid_time_missing_policy_and_summary_failure_fail_closed(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    naive = await _build(
        services,
        now=datetime(2026, 7, 27, 18, 0),
    )
    assert naive.reason_code is CurrentPriorityQueueReason.INVALID_REQUEST
    assert naive.policy_snapshot is None
    assert services["job_repository"].list_calls == 0

    services["policy_provider"].policies[SUBJECT] = None
    missing_policy = await _build(services)
    assert (
        missing_policy.reason_code
        is CurrentPriorityQueueReason.ACTIVE_POLICY_NOT_FOUND
    )
    assert not services["candidate_summary_provider"].calls

    services["policy_provider"].policies[SUBJECT] = _policy()
    services["candidate_summary_provider"].fail = True
    failed_summary = await _build(services)
    assert (
        failed_summary.reason_code
        is CurrentPriorityQueueReason.CANDIDATE_SUMMARY_UNAVAILABLE
    )
    assert not services["agent"].calls
    assert not list(
        services["home"].paths.prioritization.glob(
            "orchestrations/*.json"
        )
    )


@pytest.mark.asyncio
async def test_corrupt_orchestration_record_fails_closed(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    directory = (
        services["home"].paths.prioritization / "orchestrations"
    )
    directory.mkdir(parents=True, exist_ok=True)
    corrupt_path = directory / f"priority-input-{'a' * 64}.json"
    corrupt_path.write_text("{not-json", encoding="utf-8")

    result = await _build(services)

    assert result.status is CurrentPriorityQueueStatus.FAILED
    assert (
        result.reason_code
        is CurrentPriorityQueueReason.ORCHESTRATION_REPOSITORY_FAILED
    )
    assert not result.items
    assert not services["agent"].calls


def test_job_repository_list_current_is_typed_stable_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = PrivateHome(tmp_path / "private")
    monkeypatch.setenv("JOBOPS_HOME", str(home.root))
    repository = PrivateHomeJobPostingRepository(home)
    created_ids: list[str] = []
    for index in (1, 2):
        response = run_discovery(
            JobDiscoveryRequest(
                request_id=f"request-queue-list-{index}",
                trigger=DiscoveryTrigger.CONVERSATIONAL,
                proposal=JobIntakeProposal(
                    proposal_id=f"proposal-queue-list-{index}",
                    intent=JobIntakeIntent.ADD_JOB,
                    resolution=ProposalResolution.RESOLVED,
                    resolved_candidate=ResolvedJobCandidate(
                        source_platform="greenhouse",
                        source_url=(
                            "https://boards.greenhouse.io/example/jobs/"
                            f"queue-list-{index}"
                        ),
                        company="Synthetic Earth",
                        title=f"Synthetic Role {index}",
                        description="Synthetic public job description.",
                        location="Vancouver, Canada",
                        work_mode="HYBRID",
                        ats_type="greenhouse",
                    ),
                ),
            )
        )
        assert response.job_id is not None
        created_ids.append(response.job_id)
    excluded_path = home.paths.job_postings / f"{created_ids[0]}.json"
    excluded = json.loads(excluded_path.read_text(encoding="utf-8"))
    excluded["status"] = "EXCLUDED"
    home.write_bytes(
        excluded_path,
        (json.dumps(excluded, sort_keys=True) + "\n").encode(),
    )

    listed = repository.list_current()
    assert [job.job_id for job in listed] == [created_ids[1]]
    assert all(isinstance(job, JobPosting) for job in listed)

    home.write_bytes(
        home.paths.job_postings / "job-corrupt.json",
        b"{not-json",
    )
    with pytest.raises(
        JobPostingRepositoryError,
        match="could not be loaded",
    ):
        repository.list_current()


def test_read_model_imports_no_execution_or_agent_services() -> None:
    source = Path("core/current_priority_queue.py").read_text(
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
        "dashboard",
        "tracker",
        "csv",
        "browser",
        "ats",
        "application",
    }
    assert not imported.intersection(banned)
