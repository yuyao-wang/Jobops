"""Focused tests for the pollable serial automatic-application supervisor."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from starlette.requests import Request

from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.automation_cycle import (
    AutomationCycleOperationStatus,
    AutomationCycleRun,
    AutomationCycleRunStatus,
    AutomationCycleStage,
    AutomationCycleStageResult,
    AutomationCycleStageStatus,
    AutomationCycleSummary,
    RunAutomationCycleCommand,
    RunAutomationCycleResult,
)
from dashboard.automation_cycle import (
    AUTOMATION_CYCLE_UI_CONFIG_VERSION,
    AutomationCycleUIBudgetConfig,
    AutomationPreflightProgress,
    AutomationPreflightResult,
    AutomationPreflightStatus,
    ContinueAutomationUICommand,
    ContinueAutomationUIController,
    ContinueAutomationUIStatus,
    StopAutomationUICommand,
    map_automation_cycle_result,
)
from dashboard.server import (
    app,
    automatic_application_status_ui,
    continue_automatic_application_ui,
    stop_automatic_application_ui,
)
from tests.test_application_plan import NOW, SUBJECT


def _context() -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="session_reference_0123456789abcdef",
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def test_v2_budget_config_migrates_to_current_supervisor_contract() -> None:
    config = AutomationCycleUIBudgetConfig(
        contract_version="automation-cycle-ui-budgets-v2"
    )

    assert config.contract_version == AUTOMATION_CYCLE_UI_CONFIG_VERSION


async def _preflight(**_kwargs) -> AutomationPreflightResult:
    return AutomationPreflightResult(AutomationPreflightStatus.COMPLETED)


def _work(*job_ids: str):
    async def snapshot(**_kwargs) -> tuple[str, ...]:
        return tuple(job_ids)

    return snapshot


def _stage(
    stage: AutomationCycleStage,
    budget: int,
    *,
    status: AutomationCycleStageStatus,
    completed: int = 0,
    deferred: int = 0,
    failed: int = 0,
    uncertain: int = 0,
    safely_skipped: int = 0,
    summary: dict[str, int] | None = None,
) -> AutomationCycleStageResult:
    return AutomationCycleStageResult.create(
        stage=stage,
        budget=budget,
        status=status,
        public_status=status.value,
        actual_processed=completed + deferred + failed + uncertain,
        completed=completed,
        deferred=deferred,
        failed=failed,
        uncertain=uncertain,
        safely_skipped=safely_skipped,
        summary=summary or {},
    )


def _result(
    command: RunAutomationCycleCommand,
    *,
    operation: AutomationCycleOperationStatus = (
        AutomationCycleOperationStatus.COMPLETED
    ),
    human_checkpoint: bool = False,
    preparation_failure: bool = False,
    preparation_deferred: bool = False,
    replayed_not_ready_checkpoint: bool = False,
    preparation_failure_blocks_execution: bool = False,
    noop: bool = False,
) -> RunAutomationCycleResult:
    normal = (
        AutomationCycleStageStatus.NOOP
        if noop
        else AutomationCycleStageStatus.COMPLETED
    )
    stages = (
        _stage(
            AutomationCycleStage.PRIORITY_REFRESH,
            command.max_reprioritizations,
            status=normal,
            completed=0 if noop else 1,
        ),
        _stage(
            AutomationCycleStage.APPLICATION_PLAN_CREATION,
            command.max_plan_creations,
            status=normal,
            completed=0 if noop else 1,
            summary={"created": 0 if noop else 1, "unchanged": 0},
        ),
        _stage(
            AutomationCycleStage.APPLICATION_PREPARATION,
            command.max_preparations,
            status=(
                AutomationCycleStageStatus.PARTIAL_FAILURE
                if (
                    human_checkpoint
                    or preparation_failure
                    or preparation_deferred
                )
                else normal
            ),
            completed=(
                0
                if noop
                or human_checkpoint
                or preparation_failure
                or preparation_deferred
                else 1
            ),
            deferred=1 if human_checkpoint or preparation_deferred else 0,
            failed=1 if preparation_failure else 0,
            safely_skipped=(
                1 if human_checkpoint or preparation_deferred else 0
            ),
            summary={
                "skipped_human_attention": (
                    1 if human_checkpoint or preparation_deferred else 0
                )
            },
        ),
        _stage(
            AutomationCycleStage.BUNDLE_ASSEMBLY,
            command.max_bundle_assemblies,
            status=(
                AutomationCycleStageStatus.NOOP
                if preparation_deferred or preparation_failure_blocks_execution
                else normal
            ),
            completed=(
                0
                if noop
                or preparation_deferred
                or preparation_failure_blocks_execution
                else 1
            ),
            summary={
                "assembled": (
                    0
                    if noop
                    or preparation_deferred
                    or preparation_failure_blocks_execution
                    else 1
                ),
                "unchanged": 0,
            },
        ),
        _stage(
            AutomationCycleStage.APPLICATION_EXECUTION,
            command.max_executions,
            status=(
                AutomationCycleStageStatus.PARTIAL_FAILURE
                if human_checkpoint
                else AutomationCycleStageStatus.PARTIAL_FAILURE
                if preparation_failure_blocks_execution
                else AutomationCycleStageStatus.NOOP
                if preparation_deferred
                else AutomationCycleStageStatus.NOOP
                if replayed_not_ready_checkpoint
                else normal
            ),
            completed=(
                0
                if noop
                or human_checkpoint
                or preparation_failure_blocks_execution
                or preparation_deferred
                or replayed_not_ready_checkpoint
                else 1
            ),
            deferred=(
                1
                if preparation_failure_blocks_execution
                or preparation_deferred
                else 0
            ),
            uncertain=1 if human_checkpoint else 0,
            safely_skipped=1 if replayed_not_ready_checkpoint else 0,
            summary={
                "skipped_not_ready": (
                    1 if replayed_not_ready_checkpoint else 0
                )
            },
        ),
    )
    summary = AutomationCycleSummary(
        actual_processed=sum(item.actual_processed for item in stages),
        completed=sum(item.completed for item in stages),
        deferred=sum(item.deferred for item in stages),
        failed=sum(item.failed for item in stages),
        uncertain=sum(item.uncertain for item in stages),
        safely_skipped=sum(item.safely_skipped for item in stages),
    )
    run = AutomationCycleRun.create(
        command=command,
        stage_results=stages,
        summary=summary,
        overall_status=(
            AutomationCycleRunStatus.PARTIAL_FAILURE
            if human_checkpoint or preparation_failure or preparation_deferred
            else AutomationCycleRunStatus.NOOP
            if noop
            else AutomationCycleRunStatus.COMPLETED
        ),
    )
    return RunAutomationCycleResult(operation, run, None)


def _priority_failure_result(
    command: RunAutomationCycleCommand,
    *,
    continuable: bool,
) -> RunAutomationCycleResult:
    stages = (
        _stage(
            AutomationCycleStage.PRIORITY_REFRESH,
            command.max_reprioritizations,
            status=AutomationCycleStageStatus.FAILED,
            failed=1,
            summary={
                "continuable_system_failures": 1 if continuable else 0,
            },
        ),
        _stage(
            AutomationCycleStage.APPLICATION_PLAN_CREATION,
            command.max_plan_creations,
            status=AutomationCycleStageStatus.NOOP,
        ),
        _stage(
            AutomationCycleStage.APPLICATION_PREPARATION,
            command.max_preparations,
            status=AutomationCycleStageStatus.NOOP,
        ),
        _stage(
            AutomationCycleStage.BUNDLE_ASSEMBLY,
            command.max_bundle_assemblies,
            status=AutomationCycleStageStatus.NOOP,
        ),
        _stage(
            AutomationCycleStage.APPLICATION_EXECUTION,
            command.max_executions,
            status=AutomationCycleStageStatus.NOOP,
        ),
    )
    summary = AutomationCycleSummary(
        actual_processed=1,
        completed=0,
        deferred=0,
        failed=1,
        uncertain=0,
        safely_skipped=0,
    )
    run = AutomationCycleRun.create(
        command=command,
        stage_results=stages,
        summary=summary,
        overall_status=AutomationCycleRunStatus.PARTIAL_FAILURE,
    )
    return RunAutomationCycleResult(
        AutomationCycleOperationStatus.PARTIAL_FAILURE,
        run,
        None,
    )


async def _wait_terminal(
    controller: ContinueAutomationUIController,
) -> object:
    for _ in range(200):
        result = await controller.status(context=_context())
        if result.status not in {
            ContinueAutomationUIStatus.RUNNING,
            ContinueAutomationUIStatus.STOPPING,
        }:
            return result
        await asyncio.sleep(0)
    raise AssertionError("automation session did not reach a terminal state")


@pytest.mark.asyncio
async def test_start_returns_immediately_and_runs_jobs_serially_in_order() -> None:
    calls: list[RunAutomationCycleCommand] = []
    active = 0
    max_active = 0

    async def cycle(
        command: RunAutomationCycleCommand,
    ) -> RunAutomationCycleResult:
        nonlocal active, max_active
        calls.append(command)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0)
        active -= 1
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-a", "job-b", "job-c"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(
            max_reprioritizations=3,
            max_plan_creations=4,
            max_preparations=5,
            max_bundle_assemblies=6,
            max_executions=7,
        ),
    )

    started = await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand(
            "automation-ui-serial", approve_gate_a=True
        ),
    )
    terminal = await _wait_terminal(controller)

    assert started.status is ContinueAutomationUIStatus.RUNNING
    assert terminal.status is ContinueAutomationUIStatus.COMPLETED
    assert terminal.cycles_completed == terminal.total_jobs == 3
    assert max_active == 1
    assert [call.target_job_ids for call in calls] == [
        ("job-a",),
        ("job-b",),
        ("job-c",),
    ]
    assert all(call.budgets == (1, 1, 1, 1, 1) for call in calls)
    assert all(call.subject_id == SUBJECT for call in calls)
    assert all(call.approve_gate_a is True for call in calls)


@pytest.mark.asyncio
async def test_stop_waits_for_current_job_and_prevents_the_next_cycle() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(command: RunAutomationCycleCommand):
        calls.append(command)
        entered.set()
        await release.wait()
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-a", "job-b", "job-c"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-ui-stop"),
    )
    await entered.wait()

    stopping = await controller.stop(
        context=_context(),
        command=StopAutomationUICommand("automation-ui-stop"),
    )
    release.set()
    terminal = await _wait_terminal(controller)

    assert stopping.status is ContinueAutomationUIStatus.STOPPING
    assert stopping.stop_requested is True
    assert terminal.status is ContinueAutomationUIStatus.STOPPED
    assert terminal.cycles_completed == 1
    assert len(calls) == 1

    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-ui-resume-after-stop"),
    )
    resumed = await _wait_terminal(controller)

    assert resumed.status is ContinueAutomationUIStatus.COMPLETED
    assert [call.target_job_ids for call in calls] == [
        ("job-a",),
        ("job-b",),
        ("job-c",),
    ]


@pytest.mark.asyncio
async def test_duplicate_and_competing_starts_share_one_subject_session() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(command: RunAutomationCycleCommand):
        calls.append(command)
        entered.set()
        await release.wait()
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-a"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    first = await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-first"),
    )
    await entered.wait()
    duplicate = await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-first"),
    )
    competing = await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-competing"),
    )
    release.set()
    await _wait_terminal(controller)

    assert first.invocation_id == "automation-first"
    assert duplicate.invocation_id == "automation-first"
    assert competing.invocation_id == "automation-first"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stop_during_preflight_prevents_queue_and_cycle_work() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    work_calls = 0
    cycle_calls = 0

    async def preflight(**_kwargs):
        entered.set()
        await release.wait()
        return AutomationPreflightResult(AutomationPreflightStatus.COMPLETED)

    async def work(**_kwargs):
        nonlocal work_calls
        work_calls += 1
        return ("job-a",)

    async def cycle(command: RunAutomationCycleCommand):
        nonlocal cycle_calls
        cycle_calls += 1
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=preflight,
        work_snapshot=work,
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-stop-preflight"),
    )
    await entered.wait()
    await controller.stop(
        context=_context(),
        command=StopAutomationUICommand("automation-stop-preflight"),
    )
    release.set()

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.STOPPED
    assert work_calls == cycle_calls == 0


@pytest.mark.asyncio
async def test_cooperative_preflight_observes_stop_without_external_release() -> None:
    entered = asyncio.Event()
    work_calls = 0

    async def preflight(*, stop_requested, **_kwargs):
        entered.set()
        while not stop_requested():
            await asyncio.sleep(0)
        return AutomationPreflightResult(AutomationPreflightStatus.NOOP)

    async def work(**_kwargs):
        nonlocal work_calls
        work_calls += 1
        return ("job-a",)

    controller = ContinueAutomationUIController(
        automation_cycle=lambda _command: None,
        preflight=preflight,
        work_snapshot=work,
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-cooperative-stop"),
    )
    await entered.wait()
    await controller.stop(
        context=_context(),
        command=StopAutomationUICommand("automation-cooperative-stop"),
    )

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.STOPPED
    assert work_calls == 0


@pytest.mark.asyncio
async def test_preflight_progress_is_pollable_before_refresh_finishes() -> None:
    progress_reported = asyncio.Event()
    release = asyncio.Event()

    async def preflight(*, progress_observer, **_kwargs):
        await progress_observer(
            AutomationPreflightProgress(
                "Searching configured sources: 1 of 3 completed."
            )
        )
        progress_reported.set()
        await release.wait()
        return AutomationPreflightResult(AutomationPreflightStatus.COMPLETED)

    controller = ContinueAutomationUIController(
        automation_cycle=lambda _command: None,
        preflight=preflight,
        work_snapshot=_work(),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-preflight-progress"),
    )
    await progress_reported.wait()

    running = await controller.status(context=_context())

    assert running.status is ContinueAutomationUIStatus.RUNNING
    assert running.phase == "PREFLIGHT"
    assert running.message == "Searching configured sources: 1 of 3 completed."

    release.set()
    terminal = await _wait_terminal(controller)
    assert terminal.status is ContinueAutomationUIStatus.NOOP


@pytest.mark.asyncio
async def test_stop_during_queue_snapshot_wins_over_empty_noop() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cycle_calls = 0

    async def work(**_kwargs):
        entered.set()
        await release.wait()
        return ()

    async def cycle(_command):
        nonlocal cycle_calls
        cycle_calls += 1

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=work,
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-stop-snapshot"),
    )
    await entered.wait()
    await controller.stop(
        context=_context(),
        command=StopAutomationUICommand("automation-stop-snapshot"),
    )
    release.set()

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.STOPPED
    assert cycle_calls == 0


@pytest.mark.asyncio
async def test_operational_clock_failure_stops_before_queue_or_cycle() -> None:
    clock_calls = 0
    work_calls = 0
    cycle_calls = 0

    def clock():
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 1:
            return NOW
        raise OSError("synthetic clock failure")

    async def work(**_kwargs):
        nonlocal work_calls
        work_calls += 1
        return ("job-a",)

    async def cycle(_command):
        nonlocal cycle_calls
        cycle_calls += 1

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=work,
        clock=clock,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-clock-failure"),
    )

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.FAILED
    assert work_calls == cycle_calls == 0


@pytest.mark.asyncio
async def test_next_session_resumes_after_bounded_queue_prefix() -> None:
    calls: list[str] = []

    async def cycle(command: RunAutomationCycleCommand):
        calls.append(command.target_job_ids[0])
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-a", "job-b", "job-c"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(max_continuous_cycles=2),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-batch-one"),
    )
    first = await _wait_terminal(controller)
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-batch-two"),
    )
    second = await _wait_terminal(controller)

    assert first.status is ContinueAutomationUIStatus.COMPLETED
    assert "1 ordered jobs remain" in first.message
    assert second.status is ContinueAutomationUIStatus.COMPLETED
    assert calls == ["job-a", "job-b", "job-c"]


@pytest.mark.asyncio
async def test_completed_snapshot_does_not_hide_newly_prepended_job() -> None:
    calls: list[str] = []
    snapshots = iter(
        (
            ("job-a", "job-b"),
            ("job-new", "job-a", "job-b"),
        )
    )

    async def work(**_kwargs) -> tuple[str, ...]:
        return next(snapshots)

    async def cycle(command: RunAutomationCycleCommand):
        calls.append(command.target_job_ids[0])
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=work,
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-complete-snapshot-one"),
    )
    first = await _wait_terminal(controller)
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-complete-snapshot-two"),
    )
    second = await _wait_terminal(controller)

    assert first.status is ContinueAutomationUIStatus.COMPLETED
    assert second.status is ContinueAutomationUIStatus.COMPLETED
    assert calls[:2] == ["job-a", "job-b"]
    assert calls[2] == "job-new"


@pytest.mark.asyncio
async def test_human_checkpoint_and_unknown_outcome_stop_without_retry() -> None:
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(command: RunAutomationCycleCommand):
        calls.append(command)
        return _result(
            command,
            operation=AutomationCycleOperationStatus.PARTIAL_FAILURE,
            human_checkpoint=True,
        )

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-a", "job-b"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-ui-attention"),
    )

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.PARTIAL_FAILURE
    assert terminal.phase == "NEEDS_ATTENTION"
    assert terminal.summary.execution_uncertain == 1
    assert "automatic retry is blocked" in terminal.message
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_priority_system_failure_is_recorded_and_later_job_runs() -> None:
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(
        command: RunAutomationCycleCommand,
    ) -> RunAutomationCycleResult:
        calls.append(command)
        if command.target_job_ids == ("job-priority-failed",):
            return _priority_failure_result(command, continuable=True)
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-priority-failed", "job-next"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand(
            "automation-priority-system-failure"
        ),
    )

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.PARTIAL_FAILURE
    assert terminal.phase == "COMPLETED"
    assert terminal.cycles_completed == 2
    assert terminal.summary.priority_failed == 1
    assert terminal.summary.priority_system_failures == 1
    assert terminal.stage_failures == (
        "Priority refresh did not complete.",
    )
    assert [call.target_job_ids for call in calls] == [
        ("job-priority-failed",),
        ("job-next",),
    ]


@pytest.mark.asyncio
async def test_pre_submit_material_failure_is_recorded_and_next_job_runs() -> None:
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(
        command: RunAutomationCycleCommand,
    ) -> RunAutomationCycleResult:
        calls.append(command)
        if command.target_job_ids == ("job-material-failed",):
            return _result(
                command,
                operation=AutomationCycleOperationStatus.PARTIAL_FAILURE,
                preparation_failure=True,
                preparation_failure_blocks_execution=True,
            )
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-material-failed", "job-next"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand(
            "automation-material-failure"
        ),
    )

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.PARTIAL_FAILURE
    assert terminal.phase == "COMPLETED"
    assert terminal.cycles_completed == 2
    assert terminal.summary.preparation_failed == 1
    assert terminal.summary.execution_deferred == 1
    assert [call.target_job_ids for call in calls] == [
        ("job-material-failed",),
        ("job-next",),
    ]


@pytest.mark.asyncio
async def test_material_review_is_kept_per_job_and_next_job_runs() -> None:
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(
        command: RunAutomationCycleCommand,
    ) -> RunAutomationCycleResult:
        calls.append(command)
        if command.target_job_ids == ("job-material-review",):
            return _result(
                command,
                operation=AutomationCycleOperationStatus.PARTIAL_FAILURE,
                preparation_deferred=True,
            )
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-material-review", "job-next"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand(
            "automation-material-review"
        ),
    )

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.PARTIAL_FAILURE
    assert terminal.phase == "COMPLETED"
    assert terminal.cycles_completed == 2
    assert terminal.summary.preparation_deferred == 1
    assert terminal.summary.execution_deferred == 1
    assert [call.target_job_ids for call in calls] == [
        ("job-material-review",),
        ("job-next",),
    ]


@pytest.mark.asyncio
async def test_unclassified_priority_failure_still_pauses_batch() -> None:
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(
        command: RunAutomationCycleCommand,
    ) -> RunAutomationCycleResult:
        calls.append(command)
        return _priority_failure_result(command, continuable=False)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-persistence-failed", "job-next"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand(
            "automation-priority-unclassified-failure"
        ),
    )

    terminal = await _wait_terminal(controller)

    assert terminal.status is ContinueAutomationUIStatus.PARTIAL_FAILURE
    assert terminal.phase == "NEEDS_ATTENTION"
    assert terminal.summary.priority_failed == 1
    assert terminal.summary.priority_system_failures == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_replayed_not_ready_checkpoint_does_not_advance_queue_cursor() -> None:
    calls: list[str] = []

    async def cycle(command: RunAutomationCycleCommand):
        calls.append(command.target_job_ids[0])
        return _result(command, replayed_not_ready_checkpoint=True)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-review", "job-next"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-replayed-review-one"),
    )
    first = await _wait_terminal(controller)
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-replayed-review-two"),
    )
    second = await _wait_terminal(controller)

    assert first.status is ContinueAutomationUIStatus.PARTIAL_FAILURE
    assert second.status is ContinueAutomationUIStatus.PARTIAL_FAILURE
    assert first.phase == second.phase == "NEEDS_ATTENTION"
    assert first.summary.execution_not_ready_skipped == 1
    assert "did not move past it" in first.message
    assert calls == ["job-review", "job-review"]


@pytest.mark.asyncio
async def test_done_callback_releases_session_without_a_status_poll() -> None:
    calls: list[RunAutomationCycleCommand] = []
    completed = asyncio.Event()
    snapshot_calls = 0

    async def cycle(command: RunAutomationCycleCommand):
        calls.append(command)
        completed.set()
        return _result(command)

    async def work(**_kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return (f"job-{snapshot_calls}",)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=work,
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-disconnected-request"),
    )
    await completed.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    second = await controller.start(
        context=_context(),
        command=ContinueAutomationUICommand("automation-new-request"),
    )
    terminal = await _wait_terminal(controller)

    assert second.invocation_id == "automation-new-request"
    assert terminal.status is ContinueAutomationUIStatus.COMPLETED
    assert len(calls) == 2


def test_cycle_mapping_preserves_safe_summary_and_hides_internal_data() -> None:
    command = RunAutomationCycleCommand(
        subject_id=SUBJECT,
        invocation_id="automation-ui-map",
        now=NOW,
        max_reprioritizations=1,
        max_plan_creations=1,
        max_preparations=1,
        max_bundle_assemblies=1,
        max_executions=1,
        target_job_ids=("job-a",),
    )
    partial = map_automation_cycle_result(
        _result(
            command,
            operation=AutomationCycleOperationStatus.PARTIAL_FAILURE,
            human_checkpoint=True,
        ),
        invocation_id="public-parent-id",
    )

    assert partial.summary.human_attention_skipped == 1
    assert partial.summary.execution_uncertain == 1
    assert partial.stage_failures == (
        "Application preparation did not complete.",
        "Application execution did not complete.",
    )
    serialized = repr(partial.to_dict()).casefold()
    assert "permit" not in serialized
    assert "/private/" not in serialized


@pytest.mark.asyncio
async def test_routes_start_poll_and_stop_the_subject_scoped_session() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cycle(command: RunAutomationCycleCommand):
        entered.set()
        await release.wait()
        return _result(command)

    controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        preflight=_preflight,
        work_snapshot=_work("job-a", "job-b"),
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(),
    )
    app.state.automation_cycle_controller = controller
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/automation-cycle/run",
            "headers": [],
            "query_string": b"subject_id=subject-attacker",
        }
    )
    body = {
        "subject_id": "subject-attacker",
        "invocation_id": "automation-route-session",
        "max_executions": 999,
    }

    started = await continue_automatic_application_ui(
        body, request, _context()
    )
    await entered.wait()
    polled = await automatic_application_status_ui(request, _context())
    stopped = await stop_automatic_application_ui(
        {"invocation_id": "automation-route-session"},
        request,
        _context(),
    )
    release.set()
    terminal = await _wait_terminal(controller)

    assert started["status"] == "RUNNING"
    assert polled["status"] == "RUNNING"
    assert stopped["status"] == "STOPPING"
    assert terminal.status is ContinueAutomationUIStatus.STOPPED
    assert terminal.invocation_id == "automation-route-session"
