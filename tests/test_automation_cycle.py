"""Focused P2c10a end-to-end automation cycle tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from core.automation_cycle import (
    AutomationCycleOperationStatus,
    AutomationCycleRunStatus,
    AutomationCycleStage,
    AutomationCycleStageStatus,
    PrivateHomeAutomationCycleRunRepository,
    RunAutomationCycleCommand,
    run_automation_cycle,
)
from core.private_home import PrivateHome
from core.selective_batch_execution import (
    SelectiveBatchExecutionResult,
    SelectiveBatchExecutionStatus,
    SelectiveBatchExecutionSummary,
)
from core.selective_batch_plan_creation import (
    SelectiveBatchPlanCreationResult,
    SelectiveBatchPlanCreationStatus,
    SelectiveBatchPlanCreationSummary,
)
from core.selective_batch_preparation import (
    SelectiveBatchPreparationResult,
    SelectiveBatchPreparationStatus,
    SelectiveBatchPreparationSummary,
)
from core.selective_reprioritization import (
    SelectiveBatchOverallStatus,
    SelectiveBatchReprioritizationResult,
    SelectiveBatchSummary,
)
from tests.test_application_plan import NOW, SUBJECT


def _raw(cls, **values):
    instance = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    return instance


def _priority(status="COMPLETED", *, selected=1, failed=0):
    summary = SelectiveBatchSummary(
        requested=selected,
        selected=selected,
        created=selected - failed,
        unchanged=0,
        skipped_current=0,
        skipped_incomplete=0,
        not_found=0,
        failed=failed,
    )
    return _raw(
        SelectiveBatchReprioritizationResult,
        overall_status=SelectiveBatchOverallStatus(status),
        summary=summary,
    )


def _plans(status="COMPLETED", *, selected=1, failed=0):
    summary = SelectiveBatchPlanCreationSummary(
        requested=selected,
        selected=selected,
        created=selected - failed,
        unchanged=0,
        skipped_not_runnable=0,
        not_found=0,
        failed=failed,
    )
    return _raw(
        SelectiveBatchPlanCreationResult,
        status=SelectiveBatchPlanCreationStatus(status),
        summary=summary,
    )


def _preparations(
    status="COMPLETED",
    *,
    selected=1,
    completed=1,
    deferred=0,
    failed=0,
    attention=0,
):
    summary = SelectiveBatchPreparationSummary(
        requested=selected + attention,
        selected=selected,
        completed=completed,
        unchanged=0,
        deferred=deferred,
        failed=failed,
        skipped_human_attention=attention,
        not_found=0,
    )
    return _raw(
        SelectiveBatchPreparationResult,
        status=SelectiveBatchPreparationStatus(status),
        summary=summary,
    )


def _executions(
    status="COMPLETED",
    *,
    selected=1,
    completed=1,
    deferred=0,
    failed=0,
    uncertain=0,
    skipped_uncertain=0,
):
    summary = SelectiveBatchExecutionSummary(
        requested=selected + skipped_uncertain,
        selected=selected,
        completed=completed,
        unchanged=0,
        deferred=deferred,
        failed=failed,
        uncertain=uncertain,
        skipped_not_ready=0,
        skipped_submitted=0,
        skipped_uncertain=skipped_uncertain,
        not_found=0,
    )
    return _raw(
        SelectiveBatchExecutionResult,
        status=SelectiveBatchExecutionStatus(status),
        summary=summary,
    )


class _StageCallable:
    def __init__(self, name, result, order, active) -> None:
        self.name = name
        self.result = result
        self.order = order
        self.active = active
        self.calls = []

    async def __call__(self, command):
        self.calls.append(command)
        self.order.append(self.name)
        self.active["current"] += 1
        self.active["maximum"] = max(
            self.active["maximum"], self.active["current"]
        )
        await asyncio.sleep(0)
        self.active["current"] -= 1
        return self.result


def _command(**changes) -> RunAutomationCycleCommand:
    values = {
        "subject_id": SUBJECT,
        "invocation_id": "automation-tick-001",
        "now": NOW,
        "max_reprioritizations": 1,
        "max_plan_creations": 2,
        "max_preparations": 3,
        "max_executions": 4,
    }
    values.update(changes)
    return RunAutomationCycleCommand(**values)


def _services(results):
    order = []
    active = {"current": 0, "maximum": 0}
    calls = tuple(
        _StageCallable(name, result, order, active)
        for name, result in zip(
            ("priority", "plans", "preparation", "execution"), results
        )
    )
    return calls, order, active


@pytest.mark.asyncio
async def test_four_public_batches_run_once_in_fixed_serial_order(tmp_path):
    services, order, active = _services(
        (_priority(), _plans(), _preparations(), _executions())
    )
    result = await run_automation_cycle(
        _command(),
        priority_refresh=services[0],
        plan_creation=services[1],
        preparation=services[2],
        execution=services[3],
        repository=PrivateHomeAutomationCycleRunRepository(
            PrivateHome(tmp_path)
        ),
    )

    assert result.status is AutomationCycleOperationStatus.COMPLETED
    assert order == ["priority", "plans", "preparation", "execution"]
    assert active["maximum"] == 1
    assert [len(item.calls) for item in services] == [1, 1, 1, 1]
    assert [item.calls[0].subject_id for item in services] == [SUBJECT] * 4
    assert [item.calls[0].now for item in services] == [NOW] * 4
    assert [
        services[0].calls[0].max_jobs,
        services[1].calls[0].max_jobs,
        services[2].calls[0].max_plans,
        services[3].calls[0].max_plans,
    ] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_stage_failure_defer_and_uncertain_do_not_stop_later_stages(
    tmp_path,
):
    services, order, _active = _services(
        (
            _priority("FAILED", selected=1, failed=1),
            _plans(),
            _preparations(
                "PARTIAL_FAILURE",
                selected=1,
                completed=0,
                deferred=1,
            ),
            _executions(
                "PARTIAL_FAILURE",
                selected=1,
                completed=0,
                uncertain=1,
            ),
        )
    )
    result = await run_automation_cycle(
        _command(invocation_id="automation-tick-partial"),
        priority_refresh=services[0],
        plan_creation=services[1],
        preparation=services[2],
        execution=services[3],
        repository=PrivateHomeAutomationCycleRunRepository(
            PrivateHome(tmp_path)
        ),
    )

    assert order == ["priority", "plans", "preparation", "execution"]
    assert result.status is AutomationCycleOperationStatus.PARTIAL_FAILURE
    assert result.run.summary.completed == 1
    assert result.run.summary.failed == 1
    assert result.run.summary.deferred == 1
    assert result.run.summary.uncertain == 1


@pytest.mark.asyncio
async def test_zero_budgets_and_only_attention_uncertain_skips_are_noop(
    tmp_path,
):
    services, order, _active = _services(
        (
            _priority(),
            _plans(),
            _preparations(
                "NOOP",
                selected=0,
                completed=0,
                attention=2,
            ),
            _executions(
                "NOOP",
                selected=0,
                completed=0,
                skipped_uncertain=1,
            ),
        )
    )
    result = await run_automation_cycle(
        _command(
            invocation_id="automation-tick-noop",
            max_reprioritizations=0,
            max_plan_creations=0,
            max_preparations=2,
            max_executions=2,
        ),
        priority_refresh=services[0],
        plan_creation=services[1],
        preparation=services[2],
        execution=services[3],
        repository=PrivateHomeAutomationCycleRunRepository(
            PrivateHome(tmp_path)
        ),
    )

    assert result.status is AutomationCycleOperationStatus.NOOP
    assert order == ["preparation", "execution"]
    assert result.run.summary.safely_skipped == 2
    assert result.run.summary.uncertain == 1
    assert result.run.stage_results[0].status is (
        AutomationCycleStageStatus.SKIPPED_BUDGET_ZERO
    )
    assert result.run.stage_results[1].status is (
        AutomationCycleStageStatus.SKIPPED_BUDGET_ZERO
    )


@pytest.mark.asyncio
async def test_same_invocation_replay_is_zero_call_and_restart_recoverable(
    tmp_path,
):
    services, _order, _active = _services(
        (_priority(), _plans(), _preparations(), _executions())
    )
    home = PrivateHome(tmp_path)
    first = await run_automation_cycle(
        _command(invocation_id="automation-tick-replay"),
        priority_refresh=services[0],
        plan_creation=services[1],
        preparation=services[2],
        execution=services[3],
        repository=PrivateHomeAutomationCycleRunRepository(home),
    )
    replay_services, replay_order, _replay_active = _services(
        (_priority(), _plans(), _preparations(), _executions())
    )
    replay = await run_automation_cycle(
        replace(
            _command(invocation_id="automation-tick-replay"),
            now=NOW + timedelta(hours=1),
        ),
        priority_refresh=replay_services[0],
        plan_creation=replay_services[1],
        preparation=replay_services[2],
        execution=replay_services[3],
        repository=PrivateHomeAutomationCycleRunRepository(home),
    )

    assert first.run.overall_status is AutomationCycleRunStatus.COMPLETED
    assert replay.status is AutomationCycleOperationStatus.UNCHANGED
    assert replay.run.run_hash == first.run.run_hash
    assert replay.run.started_at == NOW
    assert replay_order == []
    source = Path("core/automation_cycle.py").read_text(encoding="utf-8")
    for forbidden in (
        "single_job_priority",
        "create_application_plan",
        "application_preparation_orchestrator",
        "application_execution_orchestrator",
        "PermitService",
        "Browser",
        "JobApplicationEngine",
    ):
        assert forbidden not in source
