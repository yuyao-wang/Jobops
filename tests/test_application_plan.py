"""Synthetic tests for the P2a1 automation-first ApplicationPlan slice."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.application_plan import (
    APPLICATION_PLAN_CONTRACT_VERSION,
    APPLICATION_PLAN_STAGES,
    ApplicationAutomationPolicy,
    ApplicationPlan,
    ApplicationPlanFailureReason,
    ApplicationPlanReadStatus,
    ApplicationPlanWriteResult,
    ApplicationPlanWriteStatus,
    CreateApplicationPlanCommand,
    CreateApplicationPlanReason,
    CreateApplicationPlanStatus,
    HumanAttentionPolicy,
    PrivateHomeApplicationPlanRepository,
    create_application_plan,
)
from core.accepted_job_intent import (
    AcceptedJobIntent,
    AcceptedJobIntentSourceProvenance,
    AcceptedJobIntentSourceType,
)
from core.current_priority_queue import CurrentPriorityItemStatus
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from core.runnable_application_queue import (
    RunnableApplicationQueueCommand,
    RunnableApplicationQueueReason,
    RunnableApplicationQueueResult,
    RunnableApplicationQueueStatus,
    RunnableApplicationQueueItem,
    RunnableApplicationReason,
    RunnableApplicationStatus,
)
from tests.test_runnable_application_queue import (
    NOW,
    SUBJECT,
    _intent,
    _policy,
    _queue,
    _queue_item,
)


INSTRUCTIONS = (
    "  Emphasize the verified remote-sensing project.\n"
    "Do not add unverified metrics.  "
)


class _QueueReader:
    def __init__(self, result: RunnableApplicationQueueResult) -> None:
        self.result = result
        self.calls: list[RunnableApplicationQueueCommand] = []

    async def __call__(
        self,
        command: RunnableApplicationQueueCommand,
    ) -> RunnableApplicationQueueResult:
        self.calls.append(command)
        return self.result


class _ForbiddenPlanRepository:
    def __init__(self) -> None:
        self.save_calls = 0

    def save(self, _plan: ApplicationPlan) -> ApplicationPlanWriteResult:
        self.save_calls += 1
        raise AssertionError("a non-runnable job must not write a plan")


class _FailingPlanRepository:
    def save(self, _plan: ApplicationPlan) -> ApplicationPlanWriteResult:
        return ApplicationPlanWriteResult(
            status=ApplicationPlanWriteStatus.FAILED,
            plan=None,
            reason_code=ApplicationPlanFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )


def _runnable_result(
    *,
    job_id: str = "job-plan",
    level: ProposedPriorityLevel = ProposedPriorityLevel.P1,
) -> RunnableApplicationQueueResult:
    policy = _policy()
    current = _queue_item(job_id, policy, level=level)
    accepted = _intent(job_id)
    projected = RunnableApplicationQueueItem(
        subject_id=SUBJECT,
        job=current.job,
        priority_queue_status=CurrentPriorityItemStatus.CURRENT,
        runnable_status=RunnableApplicationStatus.RUNNABLE,
        priority_decision=current.decision,
        application_intent=accepted,
        reasons=(),
    )
    return RunnableApplicationQueueResult(
        status=RunnableApplicationQueueStatus.SUCCEEDED,
        reason_code=None,
        retryable=False,
        subject_id=SUBJECT,
        now=NOW,
        policy_snapshot=policy,
        priority_queue_result=_queue(policy, (current,)),
        items=(projected,),
        message="Synthetic runnable queue.",
    )


def _blocked_result(
    status: RunnableApplicationStatus = (
        RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT
    ),
) -> RunnableApplicationQueueResult:
    policy = _policy()
    current = _queue_item("job-blocked", policy)
    reason = {
        RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT: (
            RunnableApplicationReason.NO_APPLICATION_INTENT
        ),
        RunnableApplicationStatus.BLOCKED_PRIORITY: (
            RunnableApplicationReason.PRIORITY_NOT_ADMITTED
        ),
    }[status]
    projected = RunnableApplicationQueueItem(
        subject_id=SUBJECT,
        job=current.job,
        priority_queue_status=CurrentPriorityItemStatus.CURRENT,
        runnable_status=status,
        priority_decision=current.decision,
        application_intent=None,
        reasons=(reason,),
    )
    return RunnableApplicationQueueResult(
        status=RunnableApplicationQueueStatus.SUCCEEDED,
        reason_code=None,
        retryable=False,
        subject_id=SUBJECT,
        now=NOW,
        policy_snapshot=policy,
        priority_queue_result=_queue(policy, (current,)),
        items=(projected,),
        message="Synthetic blocked queue.",
    )


def _direct_plan(
    *,
    job_revision: int = 1,
    job_content_hash: str = "a" * 64,
    decision_id: str = "priority-decision-a",
    policy_id: str = "policy-a",
    policy_version: int = 1,
    policy_content_hash: str = "b" * 64,
    intent_id: str = "accepted-job-intent-a",
    instructions: str | None = None,
    created_at: datetime = NOW,
) -> ApplicationPlan:
    return ApplicationPlan.create(
        subject_id=SUBJECT,
        job_id="job-direct-plan",
        job_revision=job_revision,
        job_content_hash=job_content_hash,
        priority_decision_id=decision_id,
        policy_id=policy_id,
        policy_version=policy_version,
        policy_content_hash=policy_content_hash,
        accepted_job_intent_id=intent_id,
        priority_level=ProposedPriorityLevel.P1,
        created_at=created_at,
        user_preparation_instructions=instructions,
    )


@pytest.mark.asyncio
async def test_runnable_job_creates_typed_immutable_bound_plan(
    tmp_path: Path,
) -> None:
    queue = _runnable_result()
    reader = _QueueReader(queue)
    repository = PrivateHomeApplicationPlanRepository(
        PrivateHome(tmp_path / "private")
    )

    result = await create_application_plan(
        CreateApplicationPlanCommand(
            subject_id=SUBJECT,
            job_id="job-plan",
            now=NOW,
            user_preparation_instructions=INSTRUCTIONS,
        ),
        runnable_queue_reader=reader,
        repository=repository,
    )

    assert result.status is CreateApplicationPlanStatus.CREATED
    assert len(reader.calls) == 1
    assert reader.calls[0].now is NOW
    plan = result.plan
    assert plan is not None
    source = queue.items[0]
    assert plan.contract_version == APPLICATION_PLAN_CONTRACT_VERSION
    assert plan.subject_id == SUBJECT
    assert plan.job_id == source.job.job_id
    assert plan.job_revision == source.job.revision
    assert plan.job_content_hash == source.job.content_hash
    assert plan.priority_decision_id == source.priority_decision.decision_id
    assert plan.policy_id == queue.policy_snapshot.policy_id
    assert plan.policy_version == queue.policy_snapshot.policy_version
    assert (
        plan.policy_content_hash
        == queue.policy_snapshot.policy_content_hash
    )
    assert (
        plan.accepted_job_intent_id
        == source.application_intent.accepted_job_intent_id
    )
    assert plan.priority_level is ProposedPriorityLevel.P1
    assert (
        plan.automation_policy
        is ApplicationAutomationPolicy.AUTOMATION_FIRST
    )
    assert (
        plan.human_attention_policy
        is HumanAttentionPolicy.DEFER_ITEM_AND_CONTINUE
    )
    assert plan.planned_stages == APPLICATION_PLAN_STAGES
    assert plan.user_preparation_instructions == INSTRUCTIONS
    with pytest.raises(FrozenInstanceError):
        plan.job_id = "mutated"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_non_runnable_and_missing_jobs_never_write_a_plan() -> None:
    repository = _ForbiddenPlanRepository()
    blocked_reader = _QueueReader(_blocked_result())

    blocked = await create_application_plan(
        CreateApplicationPlanCommand(SUBJECT, "job-blocked", NOW),
        runnable_queue_reader=blocked_reader,
        repository=repository,
    )
    missing = await create_application_plan(
        CreateApplicationPlanCommand(SUBJECT, "job-absent", NOW),
        runnable_queue_reader=blocked_reader,
        repository=repository,
    )

    assert blocked.status is CreateApplicationPlanStatus.NOT_RUNNABLE
    assert (
        blocked.reason_code
        is CreateApplicationPlanReason.JOB_NOT_RUNNABLE
    )
    assert (
        blocked.runnable_status
        is RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT
    )
    assert missing.status is CreateApplicationPlanStatus.NOT_RUNNABLE
    assert missing.reason_code is CreateApplicationPlanReason.JOB_NOT_FOUND
    assert repository.save_calls == 0


@pytest.mark.asyncio
async def test_identical_creator_replay_is_unchanged_without_duplicate_file(
    tmp_path: Path,
) -> None:
    queue = _runnable_result()
    first_reader = _QueueReader(queue)
    home = PrivateHome(tmp_path / "private")
    repository = PrivateHomeApplicationPlanRepository(home)
    first_command = CreateApplicationPlanCommand(
        SUBJECT,
        "job-plan",
        NOW,
        INSTRUCTIONS,
    )
    replay_now = NOW + timedelta(hours=1)
    replay_reader = _QueueReader(replace(queue, now=replay_now))

    first = await create_application_plan(
        first_command,
        runnable_queue_reader=first_reader,
        repository=repository,
    )
    second = await create_application_plan(
        CreateApplicationPlanCommand(
            SUBJECT,
            "job-plan",
            replay_now,
            INSTRUCTIONS,
        ),
        runnable_queue_reader=replay_reader,
        repository=repository,
    )

    assert first.status is CreateApplicationPlanStatus.CREATED
    assert second.status is CreateApplicationPlanStatus.UNCHANGED
    assert first.plan == second.plan
    assert second.plan.created_at == NOW
    assert len(tuple(home.paths.application_plans.glob("*.json"))) == 1
    assert len(first_reader.calls) == len(replay_reader.calls) == 1


@pytest.mark.asyncio
async def test_creator_reuses_preferred_progress_across_equivalent_refresh_intent(
    tmp_path: Path,
) -> None:
    first_queue = _runnable_result(level=ProposedPriorityLevel.P2)
    home = PrivateHome(tmp_path / "private")
    repository = PrivateHomeApplicationPlanRepository(home)
    first = await create_application_plan(
        CreateApplicationPlanCommand(SUBJECT, "job-plan", NOW),
        runnable_queue_reader=_QueueReader(first_queue),
        repository=repository,
    )
    assert first.plan is not None

    refreshed_intent = AcceptedJobIntent.create(
        subject_id=SUBJECT,
        job_id="job-plan",
        intent=first_queue.items[0].application_intent.intent,
        intake_proposal_id="refresh-proposal-two",
        discovery_run_id="refresh-run-two",
        recorded_at=NOW + timedelta(minutes=5),
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType.SEARCH_PROFILE_REFRESH,
            source_id="refresh-source-two",
            source_profile_ids=("search-profile-two",),
        ),
    )
    refreshed_queue = replace(
        first_queue,
        now=NOW + timedelta(minutes=5),
        items=(
            replace(
                first_queue.items[0],
                application_intent=refreshed_intent,
            ),
        ),
    )
    resumed = await create_application_plan(
        CreateApplicationPlanCommand(
            SUBJECT,
            "job-plan",
            refreshed_queue.now,
            preferred_application_plan_id=first.plan.plan_id,
        ),
        runnable_queue_reader=_QueueReader(refreshed_queue),
        repository=repository,
    )

    assert resumed.status is CreateApplicationPlanStatus.UNCHANGED
    assert resumed.plan == first.plan
    assert resumed.plan.accepted_job_intent_id != refreshed_intent.accepted_job_intent_id
    assert len(tuple(home.paths.application_plans.glob("*.json"))) == 1


def test_repository_replay_preserves_original_created_at(
    tmp_path: Path,
) -> None:
    repository = PrivateHomeApplicationPlanRepository(
        PrivateHome(tmp_path / "private")
    )
    first = _direct_plan(created_at=NOW)
    replay = _direct_plan(created_at=NOW + timedelta(hours=1))

    created = repository.save(first)
    unchanged = repository.save(replay)

    assert created.status is ApplicationPlanWriteStatus.CREATED
    assert unchanged.status is ApplicationPlanWriteStatus.UNCHANGED
    assert unchanged.plan is not None
    assert unchanged.plan.created_at == NOW
    assert unchanged.plan.plan_id == first.plan_id == replay.plan_id


@pytest.mark.parametrize(
    "changed",
    (
        {"instructions": "Use only the approved platform resume."},
        {"job_revision": 2},
        {"job_content_hash": "c" * 64},
        {"decision_id": "priority-decision-b"},
        {
            "policy_id": "policy-b",
            "policy_version": 2,
            "policy_content_hash": "d" * 64,
        },
        {"intent_id": "accepted-job-intent-b"},
    ),
)
def test_meaningful_input_changes_create_new_immutable_plan(
    tmp_path: Path,
    changed: dict[str, Any],
) -> None:
    repository = PrivateHomeApplicationPlanRepository(
        PrivateHome(tmp_path / "private")
    )
    original = _direct_plan()
    revised = _direct_plan(**changed)

    assert original.plan_id != revised.plan_id
    assert repository.save(original).status is ApplicationPlanWriteStatus.CREATED
    assert repository.save(revised).status is ApplicationPlanWriteStatus.CREATED


def test_user_instructions_are_preserved_and_hashed_exactly() -> None:
    first = _direct_plan(instructions=INSTRUCTIONS)
    changed = _direct_plan(instructions=INSTRUCTIONS.rstrip())

    assert first.user_preparation_instructions == INSTRUCTIONS
    assert len(first.user_preparation_instructions_hash) == 64
    assert first.user_preparation_instructions_hash != (
        changed.user_preparation_instructions_hash
    )
    assert first.plan_id != changed.plan_id


def test_repository_restarts_and_reads_same_typed_plan(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan = _direct_plan(instructions=INSTRUCTIONS)
    first = PrivateHomeApplicationPlanRepository(home)
    assert first.save(plan).status is ApplicationPlanWriteStatus.CREATED

    restarted = PrivateHomeApplicationPlanRepository(home).get(plan.plan_id)

    assert restarted.status is ApplicationPlanReadStatus.FOUND
    assert restarted.plan == plan


def test_corrupt_or_conflicting_record_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    repository = PrivateHomeApplicationPlanRepository(home)
    plan = _direct_plan()
    assert repository.save(plan).status is ApplicationPlanWriteStatus.CREATED
    path = home.paths.application_plans / f"{plan.plan_id}.json"
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()

    read = repository.get(plan.plan_id)
    repeated = repository.save(plan)

    assert read.status is ApplicationPlanReadStatus.INTEGRITY_FAILURE
    assert repeated.status is ApplicationPlanWriteStatus.FAILED
    assert (
        repeated.reason_code
        is ApplicationPlanFailureReason.INTEGRITY_FAILURE
    )
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_plan_persistence_failure_is_typed_and_not_success() -> None:
    result = await create_application_plan(
        CreateApplicationPlanCommand(SUBJECT, "job-plan", NOW),
        runnable_queue_reader=_QueueReader(_runnable_result()),
        repository=_FailingPlanRepository(),
    )
    assert result.status is CreateApplicationPlanStatus.FAILED
    assert (
        result.reason_code
        is CreateApplicationPlanReason.PLAN_PERSISTENCE_FAILED
    )
    assert result.retryable
    assert result.plan is None


@pytest.mark.asyncio
async def test_invalid_command_and_queue_failure_fail_before_plan_write() -> None:
    queue_failure = RunnableApplicationQueueResult(
        status=RunnableApplicationQueueStatus.FAILED,
        reason_code=RunnableApplicationQueueReason.PRIORITY_QUEUE_FAILED,
        retryable=True,
        subject_id=SUBJECT,
        now=NOW,
        policy_snapshot=None,
        priority_queue_result=None,
        items=(),
        message="Synthetic queue failure.",
    )
    repository = _ForbiddenPlanRepository()
    reader = _QueueReader(queue_failure)

    invalid = await create_application_plan(
        CreateApplicationPlanCommand(
            SUBJECT,
            "job-plan",
            datetime(2026, 7, 28, 18, 0),
        ),
        runnable_queue_reader=reader,
        repository=repository,
    )
    failed = await create_application_plan(
        CreateApplicationPlanCommand(SUBJECT, "job-plan", NOW),
        runnable_queue_reader=reader,
        repository=repository,
    )

    assert invalid.reason_code is CreateApplicationPlanReason.INVALID_REQUEST
    assert len(reader.calls) == 1
    assert failed.reason_code is CreateApplicationPlanReason.RUNNABLE_QUEUE_FAILED
    assert failed.retryable
    assert repository.save_calls == 0


def test_application_plan_module_has_no_agent_material_or_execution_dependency() -> None:
    tree = ast.parse(
        Path("core/application_plan.py").read_text(encoding="utf-8")
    )
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
            "ApplicationEngine",
            "MaterialPackage",
        }
    )
    assert not any(
        fragment in module
        for module in imported_modules
        for fragment in (
            "single_job_priority",
            "selective_reprioritization",
            "application_engine",
            "materials",
            "browser",
            "ats",
            "tracker",
        )
    )
    assert not calls.intersection(
        {
            "evaluate",
            "create_priority_proposal",
            "finalize_priority_proposal",
            "orchestrate_single_job_priority",
            "selectively_reprioritize_jobs",
            "execute",
        }
    )
