"""Focused P2c10a end-to-end automation cycle tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import core.automation_cycle as cycle_module
from core.automation_cycle import (
    LEGACY_AUTOMATION_CYCLE_CONTRACT_VERSION,
    AutomationCycleOperationStatus,
    AutomationCycleReadStatus,
    AutomationCycleRun,
    AutomationCycleRunStatus,
    AutomationCycleStage,
    AutomationCycleStageStatus,
    AutomationCycleSummary,
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
from core.selective_bundle_assembly import (
    SelectiveBundleAssemblyResult,
    SelectiveBundleAssemblyStatus,
    SelectiveBundleAssemblySummary,
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


def _bundles(
    status="COMPLETED",
    *,
    selected=1,
    assembled=1,
    unchanged=0,
    failed=0,
    missing=0,
):
    return _raw(
        SelectiveBundleAssemblyResult,
        status=SelectiveBundleAssemblyStatus(status),
        summary=SelectiveBundleAssemblySummary(
            requested=selected + missing,
            selected=selected,
            assembled=assembled,
            unchanged=unchanged,
            skipped_not_prepared=0,
            skipped_missing_binding=missing,
            context_bound=assembled + unchanged + failed,
            context_not_ready=0,
            context_conflict=0,
            context_integrity_failure=0,
            context_failed=0,
            failed=failed,
        ),
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
        "max_bundle_assemblies": 4,
        "max_executions": 5,
    }
    values.update(changes)
    return RunAutomationCycleCommand(**values)


def _services(results):
    order = []
    active = {"current": 0, "maximum": 0}
    calls = tuple(
        _StageCallable(name, result, order, active)
        for name, result in zip(
            ("priority", "plans", "preparation", "bundle", "execution"),
            results,
        )
    )
    return calls, order, active


@pytest.mark.asyncio
async def test_five_public_batches_run_once_in_fixed_serial_order(tmp_path):
    services, order, active = _services(
        (
            _priority(),
            _plans(),
            _preparations(),
            _bundles(),
            _executions(),
        )
    )
    result = await run_automation_cycle(
        _command(),
        priority_refresh=services[0],
        plan_creation=services[1],
        preparation=services[2],
        bundle_assembly=services[3],
        execution=services[4],
        repository=PrivateHomeAutomationCycleRunRepository(
            PrivateHome(tmp_path)
        ),
    )

    assert result.status is AutomationCycleOperationStatus.COMPLETED
    assert order == [
        "priority",
        "plans",
        "preparation",
        "bundle",
        "execution",
    ]
    assert active["maximum"] == 1
    assert [len(item.calls) for item in services] == [1, 1, 1, 1, 1]
    assert [item.calls[0].subject_id for item in services] == [SUBJECT] * 5
    assert [item.calls[0].now for item in services] == [NOW] * 5
    assert [
        services[0].calls[0].max_jobs,
        services[1].calls[0].max_jobs,
        services[2].calls[0].max_plans,
        services[3].calls[0].max_assemblies,
        services[4].calls[0].max_plans,
    ] == [1, 2, 3, 4, 5]
    assert (
        services[3].calls[0].preparation_result is services[2].result
    )


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
            _bundles(
                "PARTIAL_FAILURE",
                selected=1,
                assembled=0,
                failed=1,
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
        bundle_assembly=services[3],
        execution=services[4],
        repository=PrivateHomeAutomationCycleRunRepository(
            PrivateHome(tmp_path)
        ),
    )

    assert order == [
        "priority",
        "plans",
        "preparation",
        "bundle",
        "execution",
    ]
    assert result.status is AutomationCycleOperationStatus.PARTIAL_FAILURE
    assert result.run.summary.completed == 1
    assert result.run.summary.failed == 2
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
            _bundles("NOOP", selected=0, assembled=0),
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
            max_bundle_assemblies=0,
            max_executions=2,
        ),
        priority_refresh=services[0],
        plan_creation=services[1],
        preparation=services[2],
        bundle_assembly=services[3],
        execution=services[4],
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
    assert result.run.stage_results[3].status is (
        AutomationCycleStageStatus.SKIPPED_BUDGET_ZERO
    )


@pytest.mark.asyncio
async def test_same_invocation_replay_is_zero_call_and_restart_recoverable(
    tmp_path,
):
    services, _order, _active = _services(
        (
            _priority(),
            _plans(),
            _preparations(),
            _bundles(),
            _executions(),
        )
    )
    home = PrivateHome(tmp_path)
    first = await run_automation_cycle(
        _command(invocation_id="automation-tick-replay"),
        priority_refresh=services[0],
        plan_creation=services[1],
        preparation=services[2],
        bundle_assembly=services[3],
        execution=services[4],
        repository=PrivateHomeAutomationCycleRunRepository(home),
    )
    replay_services, replay_order, _replay_active = _services(
        (
            _priority(),
            _plans(),
            _preparations(),
            _bundles(),
            _executions(),
        )
    )
    replay = await run_automation_cycle(
        replace(
            _command(invocation_id="automation-tick-replay"),
            now=NOW + timedelta(hours=1),
        ),
        priority_refresh=replay_services[0],
        plan_creation=replay_services[1],
        preparation=replay_services[2],
        bundle_assembly=replay_services[3],
        execution=replay_services[4],
        repository=PrivateHomeAutomationCycleRunRepository(home),
    )

    assert first.run.overall_status is AutomationCycleRunStatus.COMPLETED
    assert replay.status is AutomationCycleOperationStatus.UNCHANGED
    assert replay.run.run_hash == first.run.run_hash
    assert replay.run.started_at == NOW
    assert replay_order == []

    legacy_stages = (
        first.run.stage_results[0],
        first.run.stage_results[1],
        first.run.stage_results[2],
        first.run.stage_results[4],
    )
    legacy_budgets = tuple(item.budget for item in legacy_stages)
    legacy_summary = AutomationCycleSummary(
        actual_processed=sum(item.actual_processed for item in legacy_stages),
        completed=sum(item.completed for item in legacy_stages),
        deferred=sum(item.deferred for item in legacy_stages),
        failed=sum(item.failed for item in legacy_stages),
        uncertain=sum(item.uncertain for item in legacy_stages),
        safely_skipped=sum(item.safely_skipped for item in legacy_stages),
    )
    legacy_binding = {
        "budgets": list(legacy_budgets),
        "composition_binding": "jobops-default-composition-v1",
        "contract_version": LEGACY_AUTOMATION_CYCLE_CONTRACT_VERSION,
        "invocation_id": "historical-four-stage-cycle",
        "service_contracts": cycle_module._service_contracts(
            LEGACY_AUTOMATION_CYCLE_CONTRACT_VERSION
        ),
        "subject_id": SUBJECT,
    }
    legacy_binding_hash = cycle_module._hash(legacy_binding)
    legacy_without_hash = {
        "budgets": list(legacy_budgets),
        "completed_at": cycle_module._time(NOW),
        "composition_binding": "jobops-default-composition-v1",
        "contract_version": LEGACY_AUTOMATION_CYCLE_CONTRACT_VERSION,
        "cycle_binding_hash": legacy_binding_hash,
        "cycle_id": f"automation-cycle-{legacy_binding_hash}",
        "invocation_id": "historical-four-stage-cycle",
        "overall_status": AutomationCycleRunStatus.COMPLETED.value,
        "stage_results": [
            {**item.identity_dict(), "stage_hash": item.stage_hash}
            for item in legacy_stages
        ],
        "started_at": cycle_module._time(NOW),
        "subject_id": SUBJECT,
        "summary": {
            "actual_processed": legacy_summary.actual_processed,
            "completed": legacy_summary.completed,
            "deferred": legacy_summary.deferred,
            "failed": legacy_summary.failed,
            "safely_skipped": legacy_summary.safely_skipped,
            "uncertain": legacy_summary.uncertain,
        },
    }
    legacy_run = AutomationCycleRun(
        cycle_id=legacy_without_hash["cycle_id"],
        contract_version=LEGACY_AUTOMATION_CYCLE_CONTRACT_VERSION,
        cycle_binding_hash=legacy_binding_hash,
        invocation_id="historical-four-stage-cycle",
        composition_binding="jobops-default-composition-v1",
        subject_id=SUBJECT,
        budgets=legacy_budgets,
        stage_results=legacy_stages,
        summary=legacy_summary,
        overall_status=AutomationCycleRunStatus.COMPLETED,
        run_hash=cycle_module._hash(legacy_without_hash),
        started_at=NOW,
        completed_at=NOW,
    )
    legacy_repository = PrivateHomeAutomationCycleRunRepository(home)
    assert legacy_repository.save(legacy_run).run == legacy_run
    legacy_path = legacy_repository._path(SUBJECT, legacy_run.cycle_id)
    historical_bytes = legacy_path.read_bytes()
    historical = legacy_repository.get(
        subject_id=SUBJECT, cycle_id=legacy_run.cycle_id
    )
    assert historical.status is AutomationCycleReadStatus.FOUND
    assert historical.run == legacy_run
    assert len(historical.run.stage_results) == 4
    assert legacy_path.read_bytes() == historical_bytes

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
