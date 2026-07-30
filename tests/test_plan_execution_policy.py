"""Focused P2c1d2 Plan-bound execution policy tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.accepted_job_intent import (
    AcceptedJobIntentReadResult,
    AcceptedJobIntentReadStatus,
    PrivateHomeAcceptedJobIntentRepository,
)
from core.application_plan import (
    ApplicationPlan,
    ApplicationPlanReadResult,
    ApplicationPlanReadStatus,
)
from core.job_discovery import JobIntakeIntent
from core.job_prioritization import ProposedPriorityLevel
from core.plan_execution_policy import (
    DecidePlanExecutionPolicyCommand,
    DecidePlanExecutionPolicyStatus,
    PlanExecutionPolicyConfiguration,
    PlanExecutionPolicyFailureReason,
    PlanExecutionPolicyReadStatus,
    PlanExecutionPolicyRulesV1,
    PrivateHomePlanExecutionPolicyDecisionRepository,
    decide_plan_execution_policy,
    get_current_plan_execution_policy_decision,
)
from core.policy import (
    ApprovalActor,
    AutonomyMode,
    JobTier,
    MaterialStrategy,
    PolicyConfig,
    PolicyDecision,
    SubmitAuthority,
)
from core.private_home import PrivateHome
from tests.test_runnable_application_queue import (
    NOW,
    SUBJECT,
    _artifacts,
    _intent,
    _job,
    _policy,
)


class _PlanProvider:
    def __init__(self, plan: ApplicationPlan | None) -> None:
        self.plan = plan

    def get(self, plan_id: str) -> ApplicationPlanReadResult:
        if self.plan is None:
            return ApplicationPlanReadResult(
                ApplicationPlanReadStatus.NOT_FOUND, None
            )
        return ApplicationPlanReadResult(
            ApplicationPlanReadStatus.FOUND, self.plan
        )


class _JobProvider:
    def __init__(self, job: object) -> None:
        self.job = job

    def get(self, _job_id: str) -> object:
        return self.job


class _IntentProvider:
    def __init__(self, intent: object) -> None:
        self.intent = intent

    def get_by_id(self, **_values: object) -> AcceptedJobIntentReadResult:
        return AcceptedJobIntentReadResult(
            AcceptedJobIntentReadStatus.FOUND, self.intent
        )


class _PriorityProvider:
    def __init__(self, decision: object) -> None:
        self.decision = decision

    def get_decision(self, **_values: object) -> object:
        return self.decision


class _PolicyProvider:
    def __init__(self, policy: object) -> None:
        self.policy = policy

    def get_policy(self, _subject_id: str, _policy_id: str) -> object:
        return self.policy


def _inputs(
    level: ProposedPriorityLevel = ProposedPriorityLevel.P1,
) -> tuple[object, object, object, ApplicationPlan]:
    job = _job("job-execution-policy")
    policy = _policy()
    _, priority = _artifacts(job, policy, level=level)
    intent = _intent(job.job_id)
    plan = ApplicationPlan.create(
        subject_id=SUBJECT,
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        priority_decision_id=priority.decision_id,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_content_hash=policy.policy_content_hash,
        accepted_job_intent_id=intent.accepted_job_intent_id,
        priority_level=level,
        created_at=NOW,
        user_preparation_instructions="Do not weaken safety gates.",
    )
    return job, policy, priority, plan


def _rules(
    *,
    mode: AutonomyMode = AutonomyMode.SUPERVISED,
    configured: bool = True,
    version: int = 1,
) -> PlanExecutionPolicyRulesV1:
    return PlanExecutionPolicyRulesV1(
        PlanExecutionPolicyConfiguration.create(
            configuration_id="production-execution-policy",
            configuration_version=version,
            policy_config=PolicyConfig(mode=mode),
            authority_configured=configured,
        )
    )


def _run(
    tmp_path: Path,
    *,
    level: ProposedPriorityLevel = ProposedPriorityLevel.P1,
    mode: AutonomyMode = AutonomyMode.SUPERVISED,
    configured: bool = True,
    invocation_id: str = "execution-policy-invocation-1",
    priority_override: object | None = None,
    intent_value: JobIntakeIntent = JobIntakeIntent.REQUEST_APPLICATION,
) -> tuple[object, PrivateHomePlanExecutionPolicyDecisionRepository]:
    job, policy, priority, plan = _inputs(level)
    intent = _intent(job.job_id, value=intent_value)
    if intent.accepted_job_intent_id != plan.accepted_job_intent_id:
        plan = ApplicationPlan.create(
            subject_id=plan.subject_id,
            job_id=plan.job_id,
            job_revision=plan.job_revision,
            job_content_hash=plan.job_content_hash,
            priority_decision_id=plan.priority_decision_id,
            policy_id=plan.policy_id,
            policy_version=plan.policy_version,
            policy_content_hash=plan.policy_content_hash,
            accepted_job_intent_id=intent.accepted_job_intent_id,
            priority_level=plan.priority_level,
            created_at=plan.created_at,
            user_preparation_instructions=(
                plan.user_preparation_instructions
            ),
        )
    repository = PrivateHomePlanExecutionPolicyDecisionRepository(
        PrivateHome(tmp_path / "private")
    )
    result = decide_plan_execution_policy(
        DecidePlanExecutionPolicyCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            invocation_id=invocation_id,
            now=NOW,
        ),
        plan_provider=_PlanProvider(plan),
        job_provider=_JobProvider(job),
        accepted_intent_provider=_IntentProvider(intent),
        priority_decision_provider=_PriorityProvider(
            priority if priority_override is None else priority_override
        ),
        prioritization_policy_provider=_PolicyProvider(policy),
        execution_rules=_rules(mode=mode, configured=configured),
        repository=repository,
    )
    return result, repository


def test_exact_plan_bound_decision_is_immutable_and_replays(
    tmp_path: Path,
) -> None:
    result, repository = _run(tmp_path)
    assert result.status is DecidePlanExecutionPolicyStatus.CREATED
    record = result.record
    assert record is not None
    assert isinstance(record.policy_decision, PolicyDecision)
    assert record.policy_decision.tier is JobTier.MEDIUM
    assert record.policy_decision.material_strategy is MaterialStrategy.TARGETED
    assert record.policy_decision.gate_a_actor is ApprovalActor.HUMAN
    assert record.policy_decision.gate_b_actor is ApprovalActor.HUMAN
    assert (
        record.policy_decision.submit_authority
        is SubmitAuthority.HUMAN_WITH_PERMIT
    )
    assert record.application_plan_id
    assert record.accepted_intent_id
    assert record.priority_decision_id
    assert record.prioritization_policy_id
    assert len(record.input_binding_hash) == 64

    replay, _ = _run(tmp_path)
    assert replay.status is DecidePlanExecutionPolicyStatus.UNCHANGED
    assert replay.record == record
    other_invocation, _ = _run(
        tmp_path,
        invocation_id="execution-policy-invocation-2",
    )
    assert (
        other_invocation.status
        is DecidePlanExecutionPolicyStatus.UNCHANGED
    )
    assert other_invocation.record == record
    current = get_current_plan_execution_policy_decision(
        subject_id=SUBJECT,
        application_plan_id=record.application_plan_id,
        repository=repository,
    )
    assert current.status is PlanExecutionPolicyReadStatus.FOUND
    assert current.record == record


def test_authority_requires_explicit_config_and_is_not_inferred_from_intent(
    tmp_path: Path,
) -> None:
    missing, _ = _run(tmp_path / "missing", configured=False)
    assert missing.status is DecidePlanExecutionPolicyStatus.NOT_READY
    assert (
        missing.reason
        is PlanExecutionPolicyFailureReason.AUTHORITY_CONFIGURATION_REQUIRED
    )

    automatic, _ = _run(
        tmp_path / "automatic",
        mode=AutonomyMode.FULL_AUTOPILOT,
    )
    assert automatic.status is DecidePlanExecutionPolicyStatus.CREATED
    assert automatic.record is not None
    assert (
        automatic.record.policy_decision.submit_authority
        is SubmitAuthority.CODEX_WITH_PERMIT
    )
    # Even explicit automatic authority remains permit-bound.
    assert automatic.record.policy_decision.submit_authority.value.endswith(
        "_WITH_PERMIT"
    )

    non_application, _ = _run(
        tmp_path / "intent",
        intent_value=JobIntakeIntent.ADD_JOB,
    )
    assert non_application.status is DecidePlanExecutionPolicyStatus.NOT_READY
    assert (
        non_application.reason
        is PlanExecutionPolicyFailureReason.INTENT_NOT_RUNNABLE
    )


def test_lineage_and_policy_type_fail_closed(tmp_path: Path) -> None:
    job, policy, priority, _ = _inputs()
    drifted = replace(priority, policy_content_hash="f" * 64)
    mismatch, _ = _run(
        tmp_path / "drift",
        priority_override=drifted,
    )
    assert (
        mismatch.status
        is DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE
    )
    assert mismatch.record is None

    unsupported, _ = _run(
        tmp_path / "p3",
        level=ProposedPriorityLevel.P3,
    )
    assert (
        unsupported.status
        is DecidePlanExecutionPolicyStatus.UNSUPPORTED_POLICY
    )
    assert unsupported.record is None

    repository = PrivateHomePlanExecutionPolicyDecisionRepository(
        PrivateHome(tmp_path / "wrong-type")
    )
    _, _, _, plan = _inputs()
    wrong_type = decide_plan_execution_policy(
        DecidePlanExecutionPolicyCommand(
            SUBJECT, plan.plan_id, "wrong-policy-type", NOW
        ),
        plan_provider=_PlanProvider(plan),
        job_provider=_JobProvider(job),
        accepted_intent_provider=_IntentProvider(_intent(job.job_id)),
        priority_decision_provider=_PriorityProvider(priority),
        # A PriorityDecision cannot stand in for PrioritizationPolicy.
        prioritization_policy_provider=_PolicyProvider(priority),
        execution_rules=_rules(),
        repository=repository,
    )
    assert (
        wrong_type.status
        is DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE
    )
    assert wrong_type.record is None


def test_historical_read_invocation_conflict_and_plan_compatibility(
    tmp_path: Path,
) -> None:
    job, policy, priority, plan = _inputs()
    home = PrivateHome(tmp_path / "private")
    repository = PrivateHomePlanExecutionPolicyDecisionRepository(home)
    missing = repository.get_current(
        subject_id=SUBJECT,
        application_plan_id=plan.plan_id,
    )
    assert missing.status is PlanExecutionPolicyReadStatus.NOT_FOUND
    cross_subject = repository.get_current(
        subject_id="another-synthetic-subject",
        application_plan_id=plan.plan_id,
    )
    assert cross_subject.status is PlanExecutionPolicyReadStatus.NOT_FOUND
    original_plan = plan.to_dict()
    intent_repository = PrivateHomeAcceptedJobIntentRepository(home)
    saved_intent = intent_repository.save(_intent(job.job_id))
    assert saved_intent.intent is not None
    exact_intent = intent_repository.get_by_id(
        subject_id=SUBJECT,
        job_id=job.job_id,
        accepted_job_intent_id=saved_intent.intent.accepted_job_intent_id,
    )
    assert exact_intent.status is AcceptedJobIntentReadStatus.FOUND

    command = DecidePlanExecutionPolicyCommand(
        SUBJECT, plan.plan_id, "stable-invocation", NOW
    )
    providers = {
        "plan_provider": _PlanProvider(plan),
        "job_provider": _JobProvider(job),
        "accepted_intent_provider": _IntentProvider(_intent(job.job_id)),
        "priority_decision_provider": _PriorityProvider(priority),
        "prioritization_policy_provider": _PolicyProvider(policy),
        "repository": repository,
    }
    created = decide_plan_execution_policy(
        command, execution_rules=_rules(), **providers
    )
    assert created.status is DecidePlanExecutionPolicyStatus.CREATED
    conflict = decide_plan_execution_policy(
        command,
        execution_rules=_rules(
            mode=AutonomyMode.FULL_AUTOPILOT,
            version=2,
        ),
        **providers,
    )
    assert (
        conflict.status
        is DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE
    )
    assert (
        conflict.reason
        is PlanExecutionPolicyFailureReason.INVOCATION_CONFLICT
    )
    second = decide_plan_execution_policy(
        replace(command, invocation_id="second-rules-invocation"),
        execution_rules=_rules(
            mode=AutonomyMode.FULL_AUTOPILOT,
            version=2,
        ),
        **providers,
    )
    assert second.status is DecidePlanExecutionPolicyStatus.CREATED
    ambiguous = repository.get_current(
        subject_id=SUBJECT,
        application_plan_id=plan.plan_id,
    )
    assert ambiguous.status is PlanExecutionPolicyReadStatus.CONFLICT
    assert plan.to_dict() == original_plan
    assert created.record is not None
    assert isinstance(created.record.policy_decision, PolicyDecision)
    serialized = created.record.to_dict()
    assert "user_preparation_instructions" not in str(serialized)
    assert str(home.root) not in str(serialized)
