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
    PREVIOUS_AUTOMATION_CYCLE_CONTRACT_VERSION,
    TARGETED_AUTOMATION_CYCLE_CONTRACT_VERSION,
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
    BatchPlanCreationStatus,
    SelectiveBatchPlanCreationItem,
    SelectiveBatchPlanCreationResult,
    SelectiveBatchPlanCreationStatus,
    SelectiveBatchPlanCreationSummary,
)
from core.runnable_application_queue import RunnableApplicationStatus
from core.selective_batch_preparation import (
    SelectiveBatchPlanResult,
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
    SelectiveBatchReprioritizationItem,
    SelectiveBatchReprioritizationResult,
    SelectiveBatchSummary,
)
from tests.test_application_plan import NOW, SUBJECT


def _raw(cls, **values):
    instance = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    return instance


def _priority(
    status="COMPLETED",
    *,
    selected=1,
    failed=0,
    requested_job_ids: tuple[str, ...] = (),
):
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
        subject_id=SUBJECT,
        now=NOW,
        requested_job_ids=requested_job_ids,
        items=tuple(
            _raw(SelectiveBatchReprioritizationItem, job_id=job_id)
            for job_id in requested_job_ids[:selected]
        ),
        summary=summary,
    )


def _plans(
    status="COMPLETED",
    *,
    selected=1,
    failed=0,
    successful: tuple[tuple[str, str, BatchPlanCreationStatus], ...]
    | None = None,
):
    if successful is None:
        successful = tuple(
            (
                f"job-{index + 1}",
                f"application-plan-{index + 1}",
                BatchPlanCreationStatus.CREATED,
            )
            for index in range(selected - failed)
        )
    else:
        selected = len(successful) + failed
    items = tuple(
        SelectiveBatchPlanCreationItem(
            job_id=job_id,
            runnable_queue_status=RunnableApplicationStatus.RUNNABLE,
            creation_status=creation_status,
            application_plan_id=application_plan_id,
            creation_attempted=True,
            reason=None,
            source_reason=None,
        )
        for job_id, application_plan_id, creation_status in successful
    )
    created = sum(
        item.creation_status is BatchPlanCreationStatus.CREATED
        for item in items
    )
    unchanged = sum(
        item.creation_status is BatchPlanCreationStatus.UNCHANGED
        for item in items
    )
    summary = SelectiveBatchPlanCreationSummary(
        requested=selected,
        selected=selected,
        created=created,
        unchanged=unchanged,
        skipped_not_runnable=0,
        not_found=0,
        failed=failed,
    )
    return _raw(
        SelectiveBatchPlanCreationResult,
        status=SelectiveBatchPlanCreationStatus(status),
        subject_id=SUBJECT,
        evaluated_at=NOW,
        items=items,
        summary=summary,
        failure_reason=None,
    )


def _preparations(
    status="COMPLETED",
    *,
    selected=1,
    completed=1,
    deferred=0,
    failed=0,
    attention=0,
    plan_ids: tuple[str, ...] | None = None,
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
        subject_id=SUBJECT,
        evaluated_at=NOW,
        items=tuple(
            _raw(
                SelectiveBatchPlanResult,
                application_plan_id=plan_id,
            )
            for plan_id in (
                plan_ids
                if plan_ids is not None
                else tuple(
                    f"application-plan-{index + 1}"
                    for index in range(selected + attention)
                )
            )
        ),
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


def _historical_run(
    *,
    contract_version: str,
    invocation_id: str,
    stages: tuple,
    target_job_ids: tuple[str, ...] = (),
) -> AutomationCycleRun:
    budgets = tuple(item.budget for item in stages)
    summary = AutomationCycleSummary(
        actual_processed=sum(item.actual_processed for item in stages),
        completed=sum(item.completed for item in stages),
        deferred=sum(item.deferred for item in stages),
        failed=sum(item.failed for item in stages),
        uncertain=sum(item.uncertain for item in stages),
        safely_skipped=sum(item.safely_skipped for item in stages),
    )
    binding = {
        "budgets": list(budgets),
        "composition_binding": "jobops-default-composition-v1",
        "contract_version": contract_version,
        "invocation_id": invocation_id,
        "service_contracts": cycle_module._service_contracts(
            contract_version
        ),
        "subject_id": SUBJECT,
    }
    if contract_version == TARGETED_AUTOMATION_CYCLE_CONTRACT_VERSION:
        binding["target_job_ids"] = list(target_job_ids)
    binding_hash = cycle_module._hash(binding)
    content = {
        "budgets": list(budgets),
        "completed_at": cycle_module._time(NOW),
        "composition_binding": "jobops-default-composition-v1",
        "contract_version": contract_version,
        "cycle_binding_hash": binding_hash,
        "cycle_id": f"automation-cycle-{binding_hash}",
        "invocation_id": invocation_id,
        "overall_status": AutomationCycleRunStatus.COMPLETED.value,
        "stage_results": [
            {**item.identity_dict(), "stage_hash": item.stage_hash}
            for item in stages
        ],
        "started_at": cycle_module._time(NOW),
        "subject_id": SUBJECT,
        "summary": {
            "actual_processed": summary.actual_processed,
            "completed": summary.completed,
            "deferred": summary.deferred,
            "failed": summary.failed,
            "safely_skipped": summary.safely_skipped,
            "uncertain": summary.uncertain,
        },
    }
    if contract_version == TARGETED_AUTOMATION_CYCLE_CONTRACT_VERSION:
        content["target_job_ids"] = list(target_job_ids)
    return AutomationCycleRun(
        cycle_id=content["cycle_id"],
        contract_version=contract_version,
        cycle_binding_hash=binding_hash,
        invocation_id=invocation_id,
        composition_binding="jobops-default-composition-v1",
        subject_id=SUBJECT,
        budgets=budgets,
        stage_results=stages,
        summary=summary,
        overall_status=AutomationCycleRunStatus.COMPLETED,
        run_hash=cycle_module._hash(content),
        started_at=NOW,
        completed_at=NOW,
        target_job_ids=target_job_ids,
    )


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
    assert services[0].calls[0].job_ids is None
    assert services[1].calls[0].job_ids is None
    assert services[2].calls[0].application_plan_ids == (
        "application-plan-1",
    )
    assert services[4].calls[0].application_plan_ids == (
        "application-plan-1",
    )
    assert (
        services[3].calls[0].preparation_result is services[2].result
    )


@pytest.mark.asyncio
async def test_job_targets_flow_to_priority_and_plan_then_exact_plans_prepare(
    tmp_path,
):
    target_job_ids = ("job-target-b", "job-target-c")
    successful_plans = (
        (
            target_job_ids[0],
            "application-plan-target-b",
            BatchPlanCreationStatus.CREATED,
        ),
        (
            target_job_ids[1],
            "application-plan-target-c",
            BatchPlanCreationStatus.UNCHANGED,
        ),
    )
    services, order, active = _services(
        (
            _priority(selected=2, requested_job_ids=target_job_ids),
            _plans(successful=successful_plans),
            _preparations(
                selected=2,
                completed=2,
                plan_ids=(
                    "application-plan-target-b",
                    "application-plan-target-c",
                ),
            ),
            _bundles(selected=2, assembled=2),
            _executions(selected=2, completed=2),
        )
    )
    repository = PrivateHomeAutomationCycleRunRepository(
        PrivateHome(tmp_path)
    )

    result = await run_automation_cycle(
        _command(
            invocation_id="automation-target-lineage",
            target_job_ids=target_job_ids,
            approve_gate_a=True,
            max_reprioritizations=2,
            max_plan_creations=2,
            max_preparations=2,
            max_bundle_assemblies=2,
            max_executions=2,
        ),
        priority_refresh=services[0],
        plan_creation=services[1],
        preparation=services[2],
        bundle_assembly=services[3],
        execution=services[4],
        repository=repository,
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
    assert services[0].calls[0].job_ids == target_job_ids
    assert services[1].calls[0].job_ids == target_job_ids
    assert services[2].calls[0].application_plan_ids == (
        "application-plan-target-b",
        "application-plan-target-c",
    )
    assert services[4].calls[0].application_plan_ids == (
        "application-plan-target-b",
        "application-plan-target-c",
    )
    assert [
        (item.application_plan_id, item.approve_gate_a)
        for item in services[4].calls[0].plan_inputs
    ] == [
        ("application-plan-target-b", True),
        ("application-plan-target-c", True),
    ]
    assert result.run.target_job_ids == target_job_ids
    assert result.run.approve_gate_a is True
    assert result.run.content_dict()["approve_gate_a"] is True
    assert result.run.content_dict()["target_job_ids"] == list(
        target_job_ids
    )
    persisted = repository.get(
        subject_id=SUBJECT, cycle_id=result.run.cycle_id
    )
    assert persisted.status is AutomationCycleReadStatus.FOUND
    assert persisted.run.target_job_ids == target_job_ids
    assert persisted.run.approve_gate_a is True


@pytest.mark.asyncio
async def test_no_successful_plan_does_not_fall_back_to_all_preparations(
    tmp_path,
):
    services, order, _active = _services(
        (
            _priority(),
            _plans("NOOP", selected=0),
            _preparations(),
            _bundles(),
            _executions(),
        )
    )

    result = await run_automation_cycle(
        _command(
            invocation_id="automation-no-plan-lineage",
            max_preparations=1,
            max_bundle_assemblies=1,
            max_executions=1,
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

    assert result.status is AutomationCycleOperationStatus.COMPLETED
    assert order == ["priority", "plans"]
    assert services[2].calls == []
    assert services[3].calls == []
    assert services[4].calls == []
    assert result.run.stage_results[2].public_status == (
        "NO_SUCCESSFUL_PLANS"
    )
    assert result.run.stage_results[3].public_status == "NO_PREPARED_PLANS"
    assert result.run.stage_results[4].public_status == (
        "NO_SUCCESSFUL_PLANS"
    )


@pytest.mark.asyncio
async def test_targeted_cycle_never_falls_back_when_plan_stage_is_disabled(
    tmp_path,
):
    services, order, _active = _services(
        (
            _priority(requested_job_ids=("job-target",)),
            _plans(),
            _preparations(),
            _bundles(),
            _executions(),
        )
    )

    result = await run_automation_cycle(
        _command(
            invocation_id="automation-target-plan-disabled",
            target_job_ids=("job-target",),
            max_plan_creations=0,
            max_preparations=1,
            max_bundle_assemblies=1,
            max_executions=1,
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

    assert result.status is AutomationCycleOperationStatus.PARTIAL_FAILURE
    assert order == ["priority"]
    assert all(service.calls == [] for service in services[1:])
    assert result.run.stage_results[2].public_status == (
        "PLAN_SNAPSHOT_UNAVAILABLE"
    )
    assert result.run.stage_results[3].public_status == (
        "PREPARATION_SNAPSHOT_UNAVAILABLE"
    )
    assert result.run.stage_results[4].public_status == (
        "PLAN_SNAPSHOT_UNAVAILABLE"
    )


@pytest.mark.asyncio
async def test_targeted_cycle_rejects_unrelated_plan_result_lineage(tmp_path):
    services, order, _active = _services(
        (
            _priority(requested_job_ids=("job-target",)),
            _plans(
                successful=((
                    "job-unrelated",
                    "application-plan-unrelated",
                    BatchPlanCreationStatus.CREATED,
                ),)
            ),
            _preparations(),
            _bundles(),
            _executions(),
        )
    )

    result = await run_automation_cycle(
        _command(
            invocation_id="automation-target-invalid-plan",
            target_job_ids=("job-target",),
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

    assert result.status is AutomationCycleOperationStatus.PARTIAL_FAILURE
    assert order == ["priority", "plans"]
    assert all(service.calls == [] for service in services[2:])
    assert result.run.stage_results[1].status is (
        AutomationCycleStageStatus.FAILED
    )


@pytest.mark.asyncio
async def test_preparation_result_cannot_switch_to_an_unrelated_plan(tmp_path):
    services, order, _active = _services(
        (
            _priority(requested_job_ids=("job-target",)),
            _plans(
                successful=((
                    "job-target",
                    "application-plan-target",
                    BatchPlanCreationStatus.CREATED,
                ),)
            ),
            _preparations(
                plan_ids=("application-plan-unrelated",),
            ),
            _bundles(),
            _executions(),
        )
    )

    result = await run_automation_cycle(
        _command(
            invocation_id="automation-invalid-preparation-lineage",
            target_job_ids=("job-target",),
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

    assert result.status is AutomationCycleOperationStatus.PARTIAL_FAILURE
    assert order == ["priority", "plans", "preparation"]
    assert services[3].calls == []
    assert services[4].calls == []
    assert result.run.stage_results[2].public_status == (
        "PREPARATION_RESULT_INVALID"
    )
    assert result.run.stage_results[3].public_status == (
        "PREPARATION_SNAPSHOT_UNAVAILABLE"
    )
    assert result.run.stage_results[4].public_status == (
        "PREPARATION_SNAPSHOT_UNAVAILABLE"
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
    legacy_run = _historical_run(
        contract_version=LEGACY_AUTOMATION_CYCLE_CONTRACT_VERSION,
        invocation_id="historical-four-stage-cycle",
        stages=legacy_stages,
    )
    previous_run = _historical_run(
        contract_version=PREVIOUS_AUTOMATION_CYCLE_CONTRACT_VERSION,
        invocation_id="historical-five-stage-cycle",
        stages=first.run.stage_results,
    )
    targeted_run = _historical_run(
        contract_version=TARGETED_AUTOMATION_CYCLE_CONTRACT_VERSION,
        invocation_id="historical-targeted-five-stage-cycle",
        stages=first.run.stage_results,
        target_job_ids=("job-historical-target",),
    )
    legacy_repository = PrivateHomeAutomationCycleRunRepository(home)
    for historical_run, expected_stage_count in (
        (legacy_run, 4),
        (previous_run, 5),
        (targeted_run, 5),
    ):
        assert legacy_repository.save(historical_run).run == historical_run
        path = legacy_repository._path(
            SUBJECT, historical_run.cycle_id
        )
        historical_bytes = path.read_bytes()
        historical = legacy_repository.get(
            subject_id=SUBJECT, cycle_id=historical_run.cycle_id
        )
        assert historical.status is AutomationCycleReadStatus.FOUND
        assert historical.run == historical_run
        assert historical.run.target_job_ids == (
            ("job-historical-target",)
            if historical_run is targeted_run
            else ()
        )
        assert historical.run.approve_gate_a is False
        assert len(historical.run.stage_results) == expected_stage_count
        assert path.read_bytes() == historical_bytes

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
