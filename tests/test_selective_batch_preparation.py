"""Synthetic acceptance tests for P2b6 selective batch preparation."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.selective_batch_preparation as batch_module
from core.application_answers import (
    PrivateHomePreparedApplicationAnswerSetRepository,
)
from core.application_plan import (
    ApplicationPlan,
    ApplicationPlanListStatus,
    PrivateHomeApplicationPlanRepository,
)
from core.application_preparation_orchestrator import (
    ApplicationPreparationFailureReason,
    ApplicationPreparationStage,
    ApplicationPreparationStatus,
    PrivateHomeApplicationPreparationRunRepository,
    PublicStageStatus,
    RunApplicationPreparationCommand,
    RunApplicationPreparationResult,
    run_application_preparation,
)
from core.human_attention_queue import (
    HumanAttentionQueueFailureReason,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
    build_current_human_attention_queue,
)
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from core.selective_batch_preparation import (
    BatchPlanExecutionStatus,
    SelectiveBatchFailureReason,
    SelectiveBatchPreparationCommand,
    SelectiveBatchPreparationStatus,
    run_selective_batch_preparation,
)
from tests.test_application_preparation_orchestrator import (
    _Recorder,
    _hash,
    _recipe,
)
from tests.test_human_attention_queue import (
    _completed_with_real_answers,
    _deferred_recipe,
    _invoke,
)


NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
SUBJECT = "subject-batch-synthetic"
OTHER_SUBJECT = "subject-batch-other"


def _plan(
    home: PrivateHome,
    *,
    job_id: str,
    priority: ProposedPriorityLevel = ProposedPriorityLevel.P1,
    subject_id: str = SUBJECT,
    created_at: datetime = NOW,
) -> tuple[ApplicationPlan, PrivateHomeApplicationPlanRepository]:
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id=job_id,
        job_revision=1,
        job_content_hash=_hash(job_id),
        priority_decision_id=f"decision-{job_id}",
        policy_id="priority-policy-v1",
        policy_version=1,
        policy_content_hash="a" * 64,
        accepted_job_intent_id=f"intent-{job_id}",
        priority_level=priority,
        created_at=created_at,
    )
    repository = PrivateHomeApplicationPlanRepository(home)
    assert repository.save(plan).plan == plan
    return plan, repository


def _queue(
    home: PrivateHome,
    *,
    subject_id: str = SUBJECT,
    now: datetime = NOW,
) -> HumanAttentionQueueResult:
    return build_current_human_attention_queue(
        subject_id=subject_id,
        now=now,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        application_plan_repository=PrivateHomeApplicationPlanRepository(
            home
        ),
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
    )


class _QueueReader:
    def __init__(self, result=None, *, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = []

    def __call__(self, *, subject_id, now):
        self.calls.append((subject_id, now))
        if self.error is not None:
            raise self.error
        return self.result


class _Preparation:
    def __init__(self, statuses=None, *, errors=None):
        self.statuses = statuses or {}
        self.errors = errors or set()
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def __call__(self, command):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append(command)
            await asyncio.sleep(0)
            if command.application_plan_id in self.errors:
                raise RuntimeError("synthetic P2b4 exception")
            status = self.statuses.get(
                command.application_plan_id,
                ApplicationPreparationStatus.COMPLETED,
            )
            return RunApplicationPreparationResult(
                status=status,
                run=SimpleNamespace(
                    run_id=(
                        "application-preparation-run-"
                        f"{_hash(command.application_plan_id)}"
                    )
                ),
                reason_code=(
                    ApplicationPreparationFailureReason.PUBLIC_STAGE_EXCEPTION
                    if status is ApplicationPreparationStatus.FAILED
                    else None
                ),
                retryable=False,
                message=f"synthetic {status.value}",
            )
        finally:
            self.active -= 1


def _failed_queue(subject_id: str = SUBJECT) -> HumanAttentionQueueResult:
    return HumanAttentionQueueResult(
        status=HumanAttentionQueueStatus.FAILED,
        subject_id=subject_id,
        items=(),
        item_count=0,
        user_item_count=0,
        operator_item_count=0,
        affected_plan_count=0,
        queue_snapshot_hash=None,
        evaluated_at=NOW,
        reason_code=HumanAttentionQueueFailureReason.RUN_LIST_INTEGRITY_FAILURE,
        message="Synthetic queue failure.",
    )


@pytest.mark.asyncio
async def test_implicit_selection_is_domain_ordered_and_serial(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    p2, plans = _plan(
        home,
        job_id="job-p2",
        priority=ProposedPriorityLevel.P2,
        created_at=NOW - timedelta(hours=3),
    )
    p1_late, _ = _plan(
        home,
        job_id="job-p1-late",
        priority=ProposedPriorityLevel.P1,
        created_at=NOW - timedelta(hours=1),
    )
    p0, _ = _plan(
        home,
        job_id="job-p0",
        priority=ProposedPriorityLevel.P0,
        created_at=NOW,
    )
    p1_early, _ = _plan(
        home,
        job_id="job-p1-early",
        priority=ProposedPriorityLevel.P1,
        created_at=NOW - timedelta(hours=2),
    )
    preparation = _Preparation()
    queue_reader = _QueueReader(_queue(home))

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT, now=NOW, max_plans=4
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=queue_reader,
        single_job_preparation=preparation,
    )

    assert result.status is SelectiveBatchPreparationStatus.COMPLETED
    assert [call.application_plan_id for call in preparation.calls] == [
        p0.plan_id,
        p1_early.plan_id,
        p1_late.plan_id,
        p2.plan_id,
    ]
    assert preparation.max_active == 1
    assert queue_reader.calls == [(SUBJECT, NOW)]
    assert all(
        call.subject_id == SUBJECT and call.now == NOW
        for call in preparation.calls
    )


@pytest.mark.asyncio
async def test_attention_plan_is_skipped_once_with_all_item_ids(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, *_ = _completed_with_real_answers(home)
    queue = build_current_human_attention_queue(
        subject_id=plan.subject_id,
        now=NOW,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        application_plan_repository=PrivateHomeApplicationPlanRepository(
            home
        ),
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
    )
    preparation = _Preparation()

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=plan.subject_id,
            now=NOW,
            application_plan_ids=(plan.plan_id,),
        ),
        application_plan_repository=PrivateHomeApplicationPlanRepository(
            home
        ),
        human_attention_queue_reader=_QueueReader(queue),
        single_job_preparation=preparation,
    )

    assert result.status is SelectiveBatchPreparationStatus.NOOP
    assert preparation.calls == []
    assert len(result.items) == 1
    assert result.items[0].execution_status is (
        BatchPlanExecutionStatus.SKIPPED_HUMAN_ATTENTION
    )
    assert result.items[0].attention_item_ids == tuple(
        item.item_id for item in queue.items
    )
    assert result.summary.skipped_human_attention == 1


@pytest.mark.asyncio
async def test_attention_skip_does_not_consume_execution_limit(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    skipped, plans = _plan(home, job_id="job-attention")
    eligible, _ = _plan(home, job_id="job-eligible")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    _recorder, recipe = _deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_EVIDENCE,
        public_status="DEFERRED_NO_EVIDENCE",
        reason_code="NO_TRUSTED_EVIDENCE",
    )
    _invoke(
        plan=skipped,
        plan_repository=plans,
        run_repository=runs,
        recipe=recipe,
        now=NOW,
    )
    preparation = _Preparation()

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW,
            application_plan_ids=(skipped.plan_id, eligible.plan_id),
            max_plans=1,
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=preparation,
    )

    assert [item.execution_status for item in result.items] == [
        BatchPlanExecutionStatus.SKIPPED_HUMAN_ATTENTION,
        BatchPlanExecutionStatus.COMPLETED,
    ]
    assert [call.application_plan_id for call in preparation.calls] == [
        eligible.plan_id
    ]
    assert result.summary.selected == 1


@pytest.mark.asyncio
async def test_explicit_allowlist_deduplicates_and_preserves_order(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    first, plans = _plan(home, job_id="job-first")
    second, _ = _plan(home, job_id="job-second")
    preparation = _Preparation()

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW,
            application_plan_ids=(
                second.plan_id,
                first.plan_id,
                second.plan_id,
            ),
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=preparation,
    )

    assert [call.application_plan_id for call in preparation.calls] == [
        second.plan_id,
        first.plan_id,
    ]
    assert result.summary.requested == 2
    assert result.summary.selected == 2


@pytest.mark.asyncio
async def test_not_found_and_other_subject_do_not_call_p2b4(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    owned, plans = _plan(home, job_id="job-owned")
    other, _ = _plan(
        home, job_id="job-other", subject_id=OTHER_SUBJECT
    )
    missing = "application-plan-" + "f" * 64
    preparation = _Preparation()

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW,
            application_plan_ids=(missing, other.plan_id, owned.plan_id),
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=preparation,
    )

    assert [item.execution_status for item in result.items] == [
        BatchPlanExecutionStatus.NOT_FOUND,
        BatchPlanExecutionStatus.NOT_FOUND,
        BatchPlanExecutionStatus.COMPLETED,
    ]
    assert len(preparation.calls) == 1
    assert result.summary.not_found == 2


@pytest.mark.asyncio
async def test_max_plans_applies_after_deduplication(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    first, plans = _plan(home, job_id="job-first")
    second, _ = _plan(home, job_id="job-second")
    third, _ = _plan(home, job_id="job-third")
    preparation = _Preparation()

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW,
            application_plan_ids=(
                first.plan_id,
                first.plan_id,
                second.plan_id,
                third.plan_id,
            ),
            max_plans=2,
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=preparation,
    )

    assert [call.application_plan_id for call in preparation.calls] == [
        first.plan_id,
        second.plan_id,
    ]
    assert result.summary.requested == 3
    assert result.summary.selected == 2


@pytest.mark.asyncio
async def test_completed_unchanged_deferred_and_failed_are_aggregated(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plans_list = []
    plans = None
    for index in range(4):
        plan, plans = _plan(home, job_id=f"job-{index}")
        plans_list.append(plan)
    preparation = _Preparation(
        {
            plans_list[1].plan_id: ApplicationPreparationStatus.UNCHANGED,
            plans_list[2].plan_id: ApplicationPreparationStatus.DEFERRED,
            plans_list[3].plan_id: ApplicationPreparationStatus.FAILED,
        }
    )

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW,
            application_plan_ids=tuple(
                plan.plan_id for plan in plans_list
            ),
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=preparation,
    )

    assert result.status is SelectiveBatchPreparationStatus.PARTIAL_FAILURE
    assert len(preparation.calls) == 4
    assert (
        result.summary.completed,
        result.summary.unchanged,
        result.summary.deferred,
        result.summary.failed,
    ) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_p2b4_exception_isolated_and_next_plan_continues(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    first, plans = _plan(home, job_id="job-first")
    second, _ = _plan(home, job_id="job-second")
    preparation = _Preparation(errors={first.plan_id})

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW,
            application_plan_ids=(first.plan_id, second.plan_id),
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=preparation,
    )

    assert [item.execution_status for item in result.items] == [
        BatchPlanExecutionStatus.FAILED,
        BatchPlanExecutionStatus.COMPLETED,
    ]
    assert result.status is SelectiveBatchPreparationStatus.PARTIAL_FAILURE
    assert len(preparation.calls) == 2


@pytest.mark.asyncio
async def test_failed_queue_stops_batch_without_p2b4(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job")
    preparation = _Preparation()
    reader = _QueueReader(_failed_queue())

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW,
            application_plan_ids=(plan.plan_id,),
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=reader,
        single_job_preparation=preparation,
    )

    assert result.status is SelectiveBatchPreparationStatus.FAILED
    assert result.failure_reason is (
        SelectiveBatchFailureReason.HUMAN_ATTENTION_QUEUE_FAILED
    )
    assert preparation.calls == []
    assert len(reader.calls) == 1


@pytest.mark.asyncio
async def test_plan_list_failure_stops_implicit_batch(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    _plan(home, job_id="job")
    repository = PrivateHomeApplicationPlanRepository(home)
    repository.list_for_subject = lambda _subject: SimpleNamespace(
        status=ApplicationPlanListStatus.INTEGRITY_FAILURE,
        plans=(),
    )
    preparation = _Preparation()

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT, now=NOW, max_plans=1
        ),
        application_plan_repository=repository,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=preparation,
    )

    assert result.status is SelectiveBatchPreparationStatus.FAILED
    assert result.failure_reason is (
        SelectiveBatchFailureReason.APPLICATION_PLAN_LIST_FAILED
    )
    assert preparation.calls == []


@pytest.mark.asyncio
async def test_real_p2b4_defer_does_not_stop_next_plan(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    first, plans = _plan(home, job_id="job-deferred")
    second, _ = _plan(home, job_id="job-completed")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    _recorder, deferred_recipe = _deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_EVIDENCE,
        public_status="DEFERRED_NO_EVIDENCE",
        reason_code="NO_TRUSTED_EVIDENCE",
        input_binding="batch-deferred",
    )
    completed_recipe = _recipe(
        _Recorder(), input_binding="batch-completed"
    )

    async def real_p2b4(command: RunApplicationPreparationCommand):
        recipe = (
            deferred_recipe
            if command.application_plan_id == first.plan_id
            else completed_recipe
        )
        return run_application_preparation(
            command,
            application_plan_repository=plans,
            recipe=recipe,
            run_repository=runs,
        )

    result = await run_selective_batch_preparation(
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW,
            application_plan_ids=(first.plan_id, second.plan_id),
        ),
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=real_p2b4,
    )

    assert [item.execution_status for item in result.items] == [
        BatchPlanExecutionStatus.DEFERRED,
        BatchPlanExecutionStatus.COMPLETED,
    ]
    assert result.items[0].source_reason_code == "NO_TRUSTED_EVIDENCE"
    assert result.status is SelectiveBatchPreparationStatus.PARTIAL_FAILURE
    refreshed = _queue(home)
    assert {
        item.application_plan_id for item in refreshed.items
    } == {first.plan_id}


@pytest.mark.asyncio
async def test_completed_plan_replay_relies_on_p2b4_unchanged(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-replay")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    recorder = _Recorder()
    recipe = _recipe(recorder, input_binding="batch-replay")

    async def real_p2b4(command):
        return run_application_preparation(
            command,
            application_plan_repository=plans,
            recipe=recipe,
            run_repository=runs,
        )

    command = SelectiveBatchPreparationCommand(
        subject_id=SUBJECT,
        now=NOW,
        application_plan_ids=(plan.plan_id,),
    )
    first = await run_selective_batch_preparation(
        command,
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=real_p2b4,
    )
    call_count = len(recorder.requests)
    second = await run_selective_batch_preparation(
        command,
        application_plan_repository=plans,
        human_attention_queue_reader=_QueueReader(_queue(home)),
        single_job_preparation=real_p2b4,
    )

    assert first.items[0].execution_status is (
        BatchPlanExecutionStatus.COMPLETED
    )
    assert second.items[0].execution_status is (
        BatchPlanExecutionStatus.UNCHANGED
    )
    assert len(recorder.requests) == call_count


def test_application_plan_subject_list_is_stable_and_isolated(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    p1, repository = _plan(
        home,
        job_id="job-p1",
        priority=ProposedPriorityLevel.P1,
        created_at=NOW - timedelta(hours=1),
    )
    p0, _ = _plan(
        home,
        job_id="job-p0",
        priority=ProposedPriorityLevel.P0,
        created_at=NOW,
    )
    _plan(home, job_id="job-other", subject_id=OTHER_SUBJECT)

    first = repository.list_for_subject(SUBJECT)
    second = repository.list_for_subject(SUBJECT)

    assert first.status is ApplicationPlanListStatus.SUCCEEDED
    assert first.plans == second.plans == (p0, p1)


def test_command_requires_bounded_selection() -> None:
    with pytest.raises(ValueError):
        SelectiveBatchPreparationCommand(subject_id=SUBJECT, now=NOW)
    with pytest.raises(ValueError):
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT, now=NOW, max_plans=0
        )
    with pytest.raises(ValueError):
        SelectiveBatchPreparationCommand(
            subject_id=SUBJECT,
            now=NOW.replace(tzinfo=None),
            max_plans=1,
        )


def test_batch_source_imports_only_public_orchestration_contracts() -> None:
    tree = ast.parse(Path(batch_module.__file__).read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "application_preparation_orchestrator" in imports
    assert all(
        forbidden not in imported
        for imported in imports
        for forbidden in (
            "resume_tailoring",
            "cover_letter_drafting",
            "latex_compilation",
            "application_answers",
            "application_engine",
        )
    )
