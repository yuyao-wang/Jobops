from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

import core.single_job_priority as orchestration_module
from core.job_discovery import (
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobIntakeIntent,
    JobIntakeProposal,
    JobPosting,
    PrivateHomeJobPostingRepository,
    ProposalResolution,
    ResolvedJobCandidate,
    run_discovery,
)
from core.job_prioritization import (
    PRIORITY_VALIDATION_VERSION,
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
    PriorityAgentUnavailableError,
    PriorityContext,
    PriorityDecisionFailureReason,
    PriorityDecisionRepositoryError,
    PriorityDecisionResult,
    PriorityDecisionStatus,
    PriorityRationale,
    ProposalConfidence,
    ProposedPriorityLevel,
    ProposedQualification,
    RationaleCategory,
    build_candidate_summary,
    candidate_summary_content_hash,
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
from core.profile_store import (
    CandidateSummaryProviderError,
    PrivateHomeCandidateSummaryProvider,
)
from core.single_job_priority import (
    OrchestrationRecordStatus,
    PriorityArtifactWriteOutcome,
    PrivateHomeSingleJobPriorityRepository,
    SingleJobPriorityChange,
    SingleJobPriorityCommand,
    SingleJobPriorityReason,
    SingleJobPriorityStatus,
    build_single_job_priority_binding,
    orchestrate_single_job_priority,
)


NOW = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)
SUBJECT = "synthetic-subject-1"


class FakeJobRepository:
    def __init__(self, jobs: dict[str, JobPosting]) -> None:
        self.jobs = jobs
        self.calls: list[str] = []

    def get(self, job_id: str) -> JobPosting | None:
        self.calls.append(job_id)
        return self.jobs.get(job_id)


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
        error: bool = False,
    ) -> None:
        self.summaries = summaries
        self.error = error
        self.calls: list[tuple[str, datetime]] = []

    def get_current(
        self,
        subject_id: str,
        *,
        now: datetime,
    ) -> CandidateSummary:
        self.calls.append((subject_id, now))
        if self.error:
            raise CandidateSummaryProviderError(
                "synthetic provider failure"
            )
        return self.summaries[subject_id]


class FakePriorityAgent:
    def __init__(
        self,
        behavior: Callable[[PriorityContext], Any] | None = None,
    ) -> None:
        self.behavior = behavior or _qualified_output
        self.calls: list[PriorityContext] = []

    async def evaluate(self, context: PriorityContext) -> PriorityAgentOutput:
        self.calls.append(context)
        value = self.behavior(context)
        if isinstance(value, BaseException):
            raise value
        return value


def _metadata() -> PriorityAgentMetadata:
    return PriorityAgentMetadata(
        agent_version="priority-agent-v1",
        prompt_version="priority-agent-prompt-v1",
        model_id="synthetic-model",
    )


def _job(
    *,
    job_id: str = "job-synthetic-priority",
    revision: int = 1,
    content_hash: str = "a" * 64,
) -> JobPosting:
    return JobPosting(
        schema_version="1.0",
        job_id=job_id,
        revision=revision,
        source_platform="greenhouse",
        source_job_id="source-123",
        source_url="https://boards.greenhouse.io/example/jobs/123",
        company="Synthetic Earth",
        title="Machine Learning Engineer",
        location="Vancouver, Canada",
        work_mode="HYBRID",
        posted_at="2026-07-25T18:00:00Z",
        observed_at="2026-07-27T17:30:00Z",
        application_url=None,
        ats_type="greenhouse",
        description=(
            "Build geospatial machine-learning systems for environmental "
            "monitoring."
        ),
        content_hash=content_hash,
        status="NORMALIZED",
    )


def _hard_constraint() -> HardConstraint:
    return HardConstraint(
        constraint_type=HardConstraintType.EXCLUDED_COUNTRY,
        normalized_value="united states",
        source_excerpt="Do not apply in the United States.",
        user_confirmed=True,
    )


def _soft_preference(
    *,
    preference_id: str = "preference-earth-ai",
    statement: str = "Prioritize AI for Earth roles.",
) -> SoftPreference:
    return SoftPreference(
        preference_id=preference_id,
        category=SoftPreferenceCategory.DOMAIN,
        statement=statement,
        source_excerpt=statement,
        importance=PreferenceImportance.HIGH,
    )


def _policy(
    *,
    subject_id: str = SUBJECT,
    version: int = 1,
    statement: str = "Prioritize AI for Earth roles.",
) -> PrioritizationPolicy:
    hard = (_hard_constraint(),)
    soft = (
        _soft_preference(
            preference_id=f"preference-earth-ai-v{version}",
            statement=statement,
        ),
    )
    raw = f"{statement} Do not apply in the United States."
    admission = default_preparation_admission_policy()
    return PrioritizationPolicy(
        policy_id=f"prioritization-policy-synthetic-v{version:06d}",
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


def _fact(
    *,
    fact_id: str = "fact-earth-observation",
    statement: str = "Has verified environmental monitoring experience.",
) -> CandidateFact:
    return CandidateFact(
        fact_id=fact_id,
        category=CandidateFactCategory.DOMAIN,
        statement=statement,
        source="synthetic-user-confirmation",
        verified=True,
        prioritization_safe=True,
        scope="global",
        confirmed_at=NOW - timedelta(days=3),
    )


def _summary(
    *,
    subject_id: str = SUBJECT,
    fact: CandidateFact | None = None,
) -> CandidateSummary:
    selected = fact or _fact()
    provisional = build_candidate_summary(
        subject_id=subject_id,
        candidate_summary_version="candidate-summary-current",
        facts=(selected,),
        created_at=NOW,
    )
    return replace(
        provisional,
        candidate_summary_version=(
            "candidate-summary-"
            f"{provisional.candidate_summary_content_hash[:24]}"
        ),
    )


def _not_applicable_eligibility() -> tuple[EligibilityFinding, ...]:
    return tuple(
        EligibilityFinding(
            category=category,
            result=EligibilityFindingResult.NOT_APPLICABLE,
            impact=EligibilityImpact.NONE,
            explanation="No explicit requirement was provided.",
            evidence_refs=(),
        )
        for category in EligibilityCategory
    )


def _qualified_output(context: PriorityContext) -> PriorityAgentOutput:
    hard = context.policy.hard_constraints[0]
    soft = context.policy.soft_preferences[0]
    fact = context.candidate.facts[0]
    return PriorityAgentOutput(
        proposed_qualification=ProposedQualification.QUALIFIED,
        proposed_priority_level=ProposedPriorityLevel.P1,
        confidence=ProposalConfidence.HIGH,
        summary="The role aligns with the approved strategy and facts.",
        positive_signals=(
            PriorityRationale(
                signal_id="signal-domain-fit",
                category=RationaleCategory.DOMAIN,
                explanation="The verified domain fact supports the role.",
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
                    EvidenceRef(
                        source_type=EvidenceSourceType.JOB_FIELD,
                        source_id=context.job.job_id,
                        field="title",
                    ),
                ),
            ),
        ),
        concerns=(),
        hard_constraint_findings=(
            HardConstraintFinding(
                constraint_id=hard.constraint_id,
                result=HardConstraintFindingResult.NOT_MATCHED,
                explanation="The job is not identified as United States.",
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
        eligibility_findings=_not_applicable_eligibility(),
        missing_information=(),
        questions_for_user=(),
    )


def _write_vault(
    home: PrivateHome,
    *,
    subject_id: str = SUBJECT,
    records: list[dict[str, Any]] | None = None,
) -> None:
    paths = home.ensure()
    facts = records if records is not None else [
        {
            "fact_id": "fact-earth-observation",
            "category": "DOMAIN",
            "statement": "Has verified environmental monitoring experience.",
            "source": "synthetic-user-confirmation",
            "verified": True,
            "prioritization_safe": True,
            "scope": "global",
            "confirmed_at": "2026-07-24T18:00:00Z",
            "expires_at": None,
        }
    ]
    home.write_bytes(
        paths.profile_facts,
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "subject_id": subject_id,
                    "normalized": {},
                    "prioritization_facts": facts,
                },
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )
    home.write_bytes(
        paths.verified_answers,
        b'{"schema_version":1,"answers":{}}\n',
    )
    home.write_bytes(
        paths.policy,
        b'{"schema_version":1,"autonomy":{}}\n',
    )


def _services(
    tmp_path: Path,
    *,
    job: JobPosting | None = None,
    policy: PrioritizationPolicy | None = None,
    summary: CandidateSummary | None = None,
    agent: FakePriorityAgent | None = None,
) -> dict[str, Any]:
    selected_job = job or _job()
    selected_policy = policy or _policy()
    selected_summary = summary or _summary()
    home = PrivateHome(tmp_path / "private")
    return {
        "job_repository": FakeJobRepository(
            {selected_job.job_id: selected_job}
        ),
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
            orchestration_module.PrivateHomePriorityDecisionRepository(home)
        ),
        "agent": agent or FakePriorityAgent(),
        "metadata": _metadata(),
        "home": home,
    }


@pytest.mark.asyncio
async def test_new_and_repeated_orchestration_use_one_agent_and_gate_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(tmp_path)
    real_create = orchestration_module.create_priority_proposal
    real_finalize = orchestration_module.finalize_priority_proposal
    create_calls: list[Any] = []
    finalize_calls: list[Any] = []

    async def counted_create(*args: Any, **kwargs: Any) -> Any:
        create_calls.append(args[0])
        return await real_create(*args, **kwargs)

    def counted_finalize(*args: Any, **kwargs: Any) -> Any:
        finalize_calls.append(args[0])
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(
        orchestration_module,
        "create_priority_proposal",
        counted_create,
    )
    monkeypatch.setattr(
        orchestration_module,
        "finalize_priority_proposal",
        counted_finalize,
    )
    command = SingleJobPriorityCommand(
        subject_id=SUBJECT,
        job_id=_job().job_id,
        now=NOW,
    )

    created = await orchestrate_single_job_priority(command, **{
        key: value for key, value in services.items() if key != "home"
    })
    unchanged = await orchestrate_single_job_priority(command, **{
        key: value for key, value in services.items() if key != "home"
    })

    assert created.status is SingleJobPriorityStatus.SUCCEEDED
    assert created.change is SingleJobPriorityChange.CREATED
    assert created.proposal_outcome is PriorityArtifactWriteOutcome.CREATED
    assert created.decision_outcome is PriorityArtifactWriteOutcome.CREATED
    assert created.proposal is not None
    assert created.decision is not None
    assert created.decision.source_proposal_id == created.proposal.proposal_id
    assert created.decision.validation_version == PRIORITY_VALIDATION_VERSION
    assert created.decision.job_content_hash == _job().content_hash
    assert created.proposal.policy_content_hash == _policy().policy_content_hash
    assert created.proposal.candidate_summary_content_hash == (
        _summary().candidate_summary_content_hash
    )
    assert unchanged.status is SingleJobPriorityStatus.SUCCEEDED
    assert unchanged.change is SingleJobPriorityChange.UNCHANGED
    assert unchanged.input_binding == created.input_binding
    assert unchanged.proposal == created.proposal
    assert unchanged.decision == created.decision
    assert len(create_calls) == 1
    assert len(finalize_calls) == 1
    assert len(services["agent"].calls) == 1
    assert services["agent"].calls[0].deterministic_facts.evaluated_at == NOW
    assert services["job_repository"].calls == [_job().job_id] * 2
    assert services["policy_provider"].calls == [SUBJECT] * 2
    assert services["candidate_summary_provider"].calls == [
        (SUBJECT, NOW),
        (SUBJECT, NOW),
    ]
    orchestration_files = list(
        services["home"].paths.prioritization.glob(
            "orchestrations/*.json"
        )
    )
    decision_files = list(
        services["home"].paths.priority_decisions.glob("*/*/*.json")
    )
    assert len(orchestration_files) == 1
    assert len(decision_files) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["job", "policy", "summary"])
async def test_changed_effective_binding_creates_one_new_result(
    tmp_path: Path,
    changed_field: str,
) -> None:
    services = _services(tmp_path)
    command = SingleJobPriorityCommand(
        subject_id=SUBJECT,
        job_id=_job().job_id,
        now=NOW,
    )
    first = await orchestrate_single_job_priority(command, **{
        key: value for key, value in services.items() if key != "home"
    })
    assert first.change is SingleJobPriorityChange.CREATED

    if changed_field == "job":
        services["job_repository"].jobs[command.job_id] = _job(
            revision=2,
            content_hash="b" * 64,
        )
    elif changed_field == "policy":
        services["policy_provider"].policies[SUBJECT] = _policy(
            version=2,
            statement="Prioritize climate infrastructure roles.",
        )
    else:
        services["candidate_summary_provider"].summaries[SUBJECT] = _summary(
            fact=_fact(
                fact_id="fact-climate-platform",
                statement="Has verified climate platform experience.",
            )
        )

    second = await orchestrate_single_job_priority(command, **{
        key: value for key, value in services.items() if key != "home"
    })

    assert second.status is SingleJobPriorityStatus.SUCCEEDED
    assert second.change is SingleJobPriorityChange.CREATED
    assert second.input_binding != first.input_binding
    assert len(services["agent"].calls) == 2
    assert second.proposal is not None
    assert second.decision is not None
    if changed_field == "job":
        assert second.proposal.job_revision == 2
        assert second.decision.job_content_hash == "b" * 64
    elif changed_field == "policy":
        assert second.proposal.policy_version == 2
        assert second.decision.policy_version == 2
    else:
        expected = services[
            "candidate_summary_provider"
        ].summaries[SUBJECT]
        assert second.proposal.candidate_summary_content_hash == (
            expected.candidate_summary_content_hash
        )


@pytest.mark.asyncio
async def test_subject_isolation_never_reuses_another_subject_result(
    tmp_path: Path,
) -> None:
    other = "synthetic-subject-2"
    job = _job()
    home = PrivateHome(tmp_path / "private")
    agent = FakePriorityAgent()
    services = {
        "job_repository": FakeJobRepository({job.job_id: job}),
        "policy_provider": FakePolicyProvider(
            {
                SUBJECT: _policy(subject_id=SUBJECT),
                other: _policy(subject_id=other),
            }
        ),
        "candidate_summary_provider": FakeSummaryProvider(
            {
                SUBJECT: _summary(subject_id=SUBJECT),
                other: _summary(subject_id=other),
            }
        ),
        "orchestration_repository": (
            PrivateHomeSingleJobPriorityRepository(home)
        ),
        "decision_repository": (
            orchestration_module.PrivateHomePriorityDecisionRepository(home)
        ),
        "agent": agent,
        "metadata": _metadata(),
    }

    first = await orchestrate_single_job_priority(
        SingleJobPriorityCommand(SUBJECT, job.job_id, NOW),
        **services,
    )
    second = await orchestrate_single_job_priority(
        SingleJobPriorityCommand(other, job.job_id, NOW),
        **services,
    )

    assert first.change is SingleJobPriorityChange.CREATED
    assert second.change is SingleJobPriorityChange.CREATED
    assert first.input_binding != second.input_binding
    assert second.proposal is not None
    assert second.proposal.subject_id == other
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_missing_inputs_stop_before_agent_or_writes(
    tmp_path: Path,
) -> None:
    job = _job()
    home = PrivateHome(tmp_path / "private")
    job_repository = FakeJobRepository({})
    policy_provider = FakePolicyProvider({SUBJECT: _policy()})
    summary_provider = FakeSummaryProvider({SUBJECT: _summary()})
    agent = FakePriorityAgent()
    repositories = {
        "orchestration_repository": (
            PrivateHomeSingleJobPriorityRepository(home)
        ),
        "decision_repository": (
            orchestration_module.PrivateHomePriorityDecisionRepository(home)
        ),
    }
    command = SingleJobPriorityCommand(SUBJECT, job.job_id, NOW)

    missing_job = await orchestrate_single_job_priority(
        command,
        job_repository=job_repository,
        policy_provider=policy_provider,
        candidate_summary_provider=summary_provider,
        agent=agent,
        metadata=_metadata(),
        **repositories,
    )
    assert missing_job.reason_code is SingleJobPriorityReason.JOB_NOT_FOUND
    assert not policy_provider.calls
    assert not summary_provider.calls
    assert not agent.calls

    job_repository.jobs[job.job_id] = job
    policy_provider.policies[SUBJECT] = None
    missing_policy = await orchestrate_single_job_priority(
        command,
        job_repository=job_repository,
        policy_provider=policy_provider,
        candidate_summary_provider=summary_provider,
        agent=agent,
        metadata=_metadata(),
        **repositories,
    )
    assert (
        missing_policy.reason_code
        is SingleJobPriorityReason.ACTIVE_POLICY_NOT_FOUND
    )
    assert not summary_provider.calls
    assert not agent.calls

    policy_provider.policies[SUBJECT] = _policy()
    failed_summary_provider = FakeSummaryProvider(
        {SUBJECT: _summary()},
        error=True,
    )
    failed_summary = await orchestrate_single_job_priority(
        command,
        job_repository=job_repository,
        policy_provider=policy_provider,
        candidate_summary_provider=failed_summary_provider,
        agent=agent,
        metadata=_metadata(),
        **repositories,
    )
    assert (
        failed_summary.reason_code
        is SingleJobPriorityReason.CANDIDATE_SUMMARY_UNAVAILABLE
    )
    assert not agent.calls
    assert not list(
        home.paths.prioritization.glob("orchestrations/*.json")
    )
    assert not list(home.paths.priority_decisions.glob("*/*/*.json"))


@pytest.mark.asyncio
async def test_proposal_failure_releases_empty_claim_without_auto_retry(
    tmp_path: Path,
) -> None:
    agent = FakePriorityAgent(
        lambda _context: PriorityAgentUnavailableError(
            "synthetic unavailable"
        )
    )
    services = _services(tmp_path, agent=agent)
    command = SingleJobPriorityCommand(SUBJECT, _job().job_id, NOW)

    first = await orchestrate_single_job_priority(command, **{
        key: value for key, value in services.items() if key != "home"
    })

    assert first.status is SingleJobPriorityStatus.FAILED
    assert first.reason_code is SingleJobPriorityReason.PROPOSAL_FAILED
    assert first.retryable
    assert len(agent.calls) == 1
    assert not list(
        services["home"].paths.prioritization.glob(
            "orchestrations/*.json"
        )
    )
    assert not list(
        services["home"].paths.priority_decisions.glob("*/*/*.json")
    )


@pytest.mark.asyncio
async def test_finalization_failure_is_not_bypassed_or_reported_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(tmp_path)
    finalize_calls: list[Any] = []

    def failed_finalize(request: Any, **_kwargs: Any) -> PriorityDecisionResult:
        finalize_calls.append(request)
        return PriorityDecisionResult(
            status=PriorityDecisionStatus.FAILED,
            reason_code=(
                PriorityDecisionFailureReason.DECISION_SCHEMA_INVALID
            ),
            retryable=False,
            decision=None,
            message="Synthetic Gate failure.",
        )

    monkeypatch.setattr(
        orchestration_module,
        "finalize_priority_proposal",
        failed_finalize,
    )
    result = await orchestrate_single_job_priority(
        SingleJobPriorityCommand(SUBJECT, _job().job_id, NOW),
        **{
            key: value for key, value in services.items() if key != "home"
        },
    )

    assert result.status is SingleJobPriorityStatus.FAILED
    assert result.reason_code is SingleJobPriorityReason.FINALIZATION_FAILED
    assert result.decision is None
    assert len(finalize_calls) == 1
    assert len(services["agent"].calls) == 1
    assert not list(
        services["home"].paths.priority_decisions.glob("*/*/*.json")
    )


@pytest.mark.asyncio
async def test_decision_persistence_failure_never_reports_success(
    tmp_path: Path,
) -> None:
    class FailingDecisionRepository(
        orchestration_module.PrivateHomePriorityDecisionRepository
    ):
        def save(self, _decision: Any) -> Any:
            raise PriorityDecisionRepositoryError(
                "synthetic persistence failure"
            )

    services = _services(tmp_path)
    services["decision_repository"] = FailingDecisionRepository(
        services["home"]
    )

    result = await orchestrate_single_job_priority(
        SingleJobPriorityCommand(SUBJECT, _job().job_id, NOW),
        **{
            key: value for key, value in services.items() if key != "home"
        },
    )

    assert result.status is SingleJobPriorityStatus.FAILED
    assert result.reason_code is SingleJobPriorityReason.FINALIZATION_FAILED
    assert result.retryable
    assert result.decision is None
    assert len(services["agent"].calls) == 1
    assert not list(
        services["home"].paths.priority_decisions.glob("*/*/*.json")
    )


@pytest.mark.asyncio
async def test_existing_in_progress_claim_returns_typed_incomplete(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    job = services["job_repository"].jobs[_job().job_id]
    policy = services["policy_provider"].policies[SUBJECT]
    summary = services["candidate_summary_provider"].summaries[SUBJECT]
    binding = build_single_job_priority_binding(
        subject_id=SUBJECT,
        job=job,
        policy=policy,
        candidate_summary=summary,
        metadata=services["metadata"],
        now=NOW,
    )
    claim = services["orchestration_repository"].claim(binding)
    assert claim.status is OrchestrationRecordStatus.IN_PROGRESS
    assert claim.claim_acquired
    competing_repository = PrivateHomeSingleJobPriorityRepository(
        services["home"]
    )

    result = await orchestrate_single_job_priority(
        SingleJobPriorityCommand(SUBJECT, job.job_id, NOW),
        **{
            key: (
                competing_repository
                if key == "orchestration_repository"
                else value
            )
            for key, value in services.items()
            if key != "home"
        },
    )

    assert result.status is SingleJobPriorityStatus.FAILED
    assert (
        result.reason_code
        is SingleJobPriorityReason.ORCHESTRATION_INCOMPLETE
    )
    assert result.retryable
    assert not services["agent"].calls
    assert not list(
        services["home"].paths.priority_decisions.glob("*/*/*.json")
    )


@pytest.mark.asyncio
async def test_naive_time_is_rejected_before_any_read_or_agent_call(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    result = await orchestrate_single_job_priority(
        SingleJobPriorityCommand(
            SUBJECT,
            _job().job_id,
            datetime(2026, 7, 27, 18, 0),
        ),
        **{
            key: value for key, value in services.items() if key != "home"
        },
    )

    assert result.status is SingleJobPriorityStatus.FAILED
    assert result.reason_code is SingleJobPriorityReason.INVALID_REQUEST
    assert not services["job_repository"].calls
    assert not services["policy_provider"].calls
    assert not services["candidate_summary_provider"].calls
    assert not services["agent"].calls


def test_private_home_job_read_returns_existing_typed_posting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = PrivateHome(tmp_path / "private")
    monkeypatch.setenv("JOBOPS_HOME", str(home.root))
    candidate = ResolvedJobCandidate(
        source_platform="greenhouse",
        source_url="https://boards.greenhouse.io/example/jobs/123",
        company="Synthetic Earth",
        title="Machine Learning Engineer",
        description="A synthetic public job description.",
        location="Vancouver, Canada",
        work_mode="HYBRID",
        ats_type="greenhouse",
    )
    response = run_discovery(
        JobDiscoveryRequest(
            request_id="request-synthetic-read",
            trigger=DiscoveryTrigger.CONVERSATIONAL,
            proposal=JobIntakeProposal(
                proposal_id="proposal-synthetic-read",
                intent=JobIntakeIntent.ADD_JOB,
                resolution=ProposalResolution.RESOLVED,
                resolved_candidate=candidate,
            ),
        )
    )
    assert response.disposition is DiscoveryDisposition.ACCEPTED
    assert response.change is DiscoveryChange.CREATED
    assert response.job_id is not None

    repository = PrivateHomeJobPostingRepository(home)
    posting = repository.get(response.job_id)

    assert isinstance(posting, JobPosting)
    assert posting.job_id == response.job_id
    assert posting.company == candidate.company
    assert repository.get("job-missing") is None


def test_candidate_summary_provider_projects_only_explicit_trusted_facts(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    _write_vault(
        home,
        records=[
            {
                "fact_id": "fact-current",
                "category": "DOMAIN",
                "statement": "Has verified climate domain experience.",
                "source": "synthetic-user-confirmation",
                "verified": True,
                "prioritization_safe": True,
                "scope": "global",
                "confirmed_at": "2026-07-24T18:00:00Z",
                "expires_at": None,
            },
            {
                "fact_id": "fact-untrusted",
                "category": "SKILL",
                "statement": "Unverified synthetic claim.",
                "source": "synthetic-model",
                "verified": False,
                "prioritization_safe": True,
                "scope": "global",
                "confirmed_at": "2026-07-24T18:00:00Z",
                "expires_at": None,
            },
            {
                "fact_id": "fact-expired",
                "category": "LOCATION",
                "statement": "Expired synthetic location.",
                "source": "synthetic-user-confirmation",
                "verified": True,
                "prioritization_safe": True,
                "scope": "global",
                "confirmed_at": "2026-07-20T18:00:00Z",
                "expires_at": "2026-07-26T18:00:00Z",
            },
        ],
    )
    provider = PrivateHomeCandidateSummaryProvider(home)

    summary = provider.get_current(SUBJECT, now=NOW)
    repeated = provider.get_current(SUBJECT, now=NOW)

    assert isinstance(summary, CandidateSummary)
    assert [fact.fact_id for fact in summary.facts] == ["fact-current"]
    assert summary.candidate_summary_content_hash == (
        candidate_summary_content_hash(summary.facts)
    )
    assert summary.candidate_summary_version == (
        repeated.candidate_summary_version
    )
    assert summary.candidate_summary_content_hash == (
        repeated.candidate_summary_content_hash
    )


def test_candidate_summary_provider_has_no_legacy_or_guessed_fallback(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    _write_vault(home, subject_id="another-subject")
    provider = PrivateHomeCandidateSummaryProvider(home)

    with pytest.raises(CandidateSummaryProviderError):
        provider.get_current(SUBJECT, now=NOW)

    home.write_bytes(
        home.paths.profile_facts,
        b'{"schema_version":1,"subject_id":"synthetic-subject-1","normalized":{}}\n',
    )
    with pytest.raises(CandidateSummaryProviderError):
        provider.get_current(SUBJECT, now=NOW)


def test_orchestrator_has_no_execution_legacy_or_direct_model_dependencies() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "core"
        / "single_job_priority.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    banned = (
        "adapters",
        "browser",
        "dashboard",
        "tracker",
        "csv",
        "application",
        "source_connectors",
        "claude",
        "openai",
    )
    assert not any(
        any(token in name.casefold() for token in banned)
        for name in imported
    )
