"""Focused P2c10b selective ApplicationBundle assembly tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

import core.application_preparation_orchestrator as preparation_module
from core.application_bundle_assembly import (
    ApplicationBundleAssemblyStatus,
    AssembleApplicationBundleResult,
)
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    PREPARATION_ASSEMBLY_LINEAGE_CONTRACT_VERSION,
    PreparationAssemblyLineage,
)
from core.selective_batch_preparation import (
    BatchPlanExecutionStatus,
    BatchPlanSelectionStatus,
    SelectiveBatchPlanResult,
    SelectiveBatchPreparationResult,
    SelectiveBatchPreparationStatus,
    SelectiveBatchPreparationSummary,
)
from core.selective_bundle_assembly import (
    BundleAssemblyFailureReason,
    BundleAssemblyPlanStatus,
    SelectiveBundleAssemblyCommand,
    SelectiveBundleAssemblyStatus,
    run_selective_bundle_assembly,
)
from core.plan_assembly_execution_context_binding import (
    PLAN_ASSEMBLY_EXECUTION_CONTEXT_BINDING_CONTRACT_VERSION,
    BindPlanAssemblyExecutionContextResult,
    BindPlanAssemblyExecutionContextStatus,
    ExecutionPolicyRecordRef,
    PlanAssemblyExecutionContextBinding,
    VerifiedProfileRecordRef,
)
from tests.test_application_plan import NOW, SUBJECT


def _lineage(plan_id: str) -> PreparationAssemblyLineage:
    values = {
        "subject_id": SUBJECT,
        "application_plan_id": plan_id,
        "preparation_run_id": f"preparation-run-{plan_id}",
        "preparation_run_contract_version": (
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        "plan_material_manifest_id": f"manifest-{plan_id}",
        "prepared_application_answer_set_id": f"answers-{plan_id}",
        "preparation_completion_hash": preparation_module._canonical_hash(
            {"plan": plan_id}
        ),
        "contract_version": PREPARATION_ASSEMBLY_LINEAGE_CONTRACT_VERSION,
    }
    return PreparationAssemblyLineage(
        **values,
        lineage_hash=preparation_module._canonical_hash(values),
    )


def _successful(plan_id: str, *, unchanged: bool = False):
    lineage = _lineage(plan_id)
    return SelectiveBatchPlanResult(
        application_plan_id=plan_id,
        job_id=f"job-{plan_id}",
        selection_status=BatchPlanSelectionStatus.SELECTED,
        execution_status=(
            BatchPlanExecutionStatus.UNCHANGED
            if unchanged
            else BatchPlanExecutionStatus.COMPLETED
        ),
        preparation_run_id=lineage.preparation_run_id,
        attention_item_ids=(),
        reason_code=None,
        source_reason_code=None,
        assembly_lineage=lineage,
    )


def _preparation(items) -> SelectiveBatchPreparationResult:
    typed = tuple(items)
    completed = sum(
        item.execution_status is BatchPlanExecutionStatus.COMPLETED
        for item in typed
    )
    unchanged = sum(
        item.execution_status is BatchPlanExecutionStatus.UNCHANGED
        for item in typed
    )
    deferred = sum(
        item.execution_status is BatchPlanExecutionStatus.DEFERRED
        for item in typed
    )
    failed = sum(
        item.execution_status is BatchPlanExecutionStatus.FAILED
        for item in typed
    )
    return SelectiveBatchPreparationResult(
        status=(
            SelectiveBatchPreparationStatus.COMPLETED
            if not deferred and not failed
            else SelectiveBatchPreparationStatus.PARTIAL_FAILURE
        ),
        subject_id=SUBJECT,
        evaluated_at=NOW,
        queue_snapshot_hash="synthetic-preparation-snapshot",
        items=typed,
        summary=SelectiveBatchPreparationSummary(
            requested=len(typed),
            selected=len(typed),
            completed=completed,
            unchanged=unchanged,
            deferred=deferred,
            failed=failed,
            skipped_human_attention=0,
            not_found=0,
        ),
        failure_reason=None,
    )


def _assembly_result(command, status):
    record = SimpleNamespace(
        record_id=f"assembly-{command.application_plan_id}",
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        manifest_id=command.plan_material_manifest_id,
        answer_set_id=command.prepared_application_answer_set_id,
    )
    return AssembleApplicationBundleResult(
        status=status,
        record=record,
        bundle=SimpleNamespace(),
        not_ready_reason=None,
        failure_reason=None,
        retryable=False,
        message="synthetic assembly",
    )


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _binding(command) -> PlanAssemblyExecutionContextBinding:
    lineage = command.preparation_lineage
    profile = VerifiedProfileRecordRef(
        f"profile-{command.application_plan_id}", "profile-v1", "1" * 64
    )
    policy = ExecutionPolicyRecordRef(
        f"policy-{command.application_plan_id}", "policy-v1", "2" * 64
    )
    identity = {
        "application_assembly_context_hash": "3" * 64,
        "application_plan_id": command.application_plan_id,
        "binding_contract_version": (
            PLAN_ASSEMBLY_EXECUTION_CONTEXT_BINDING_CONTRACT_VERSION
        ),
        "execution_policy_ref": policy.to_dict(),
        "job_id": f"job-{command.application_plan_id}",
        "plan_material_manifest_id": lineage.plan_material_manifest_id,
        "policy_input_lineage_hash": "4" * 64,
        "policy_plan_binding_hash": "5" * 64,
        "preparation_lineage_hash": lineage.lineage_hash,
        "preparation_run_id": lineage.preparation_run_id,
        "prepared_application_answer_set_id": (
            lineage.prepared_application_answer_set_id
        ),
        "profile_input_lineage_hash": "6" * 64,
        "profile_plan_binding_hash": "7" * 64,
        "subject_id": command.subject_id,
        "verified_profile_ref": profile.to_dict(),
    }
    binding_hash = _digest(identity)
    return PlanAssemblyExecutionContextBinding(
        binding_id=f"plan-assembly-context-{binding_hash}",
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        job_id=f"job-{command.application_plan_id}",
        preparation_run_id=lineage.preparation_run_id,
        plan_material_manifest_id=lineage.plan_material_manifest_id,
        prepared_application_answer_set_id=(
            lineage.prepared_application_answer_set_id
        ),
        preparation_lineage_hash=lineage.lineage_hash,
        verified_profile_ref=profile,
        execution_policy_ref=policy,
        application_assembly_context_hash="3" * 64,
        profile_input_lineage_hash="6" * 64,
        policy_input_lineage_hash="4" * 64,
        profile_plan_binding_hash="7" * 64,
        policy_plan_binding_hash="5" * 64,
        created_at=command.now,
        invocation_id=command.invocation_id,
        binding_hash=binding_hash,
    )


def _bind(command):
    return BindPlanAssemblyExecutionContextResult(
        BindPlanAssemblyExecutionContextStatus.CREATED,
        binding=_binding(command),
    )


@pytest.mark.asyncio
async def test_completed_and_unchanged_call_p2c1_once_each_in_order():
    preparation = _preparation(
        (_successful("plan-a"), _successful("plan-b", unchanged=True))
    )
    calls = []
    active = 0
    maximum = 0

    async def assemble(command):
        nonlocal active, maximum
        calls.append(command)
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return _assembly_result(
            command,
            (
                ApplicationBundleAssemblyStatus.CREATED
                if command.application_plan_id == "plan-a"
                else ApplicationBundleAssemblyStatus.UNCHANGED
            ),
        )

    result = await run_selective_bundle_assembly(
        SelectiveBundleAssemblyCommand(
            subject_id=SUBJECT,
            now=NOW,
            invocation_id="cycle-001:bundle",
            preparation_result=preparation,
            max_assemblies=2,
        ),
        plan_execution_context_binder=_bind,
        assemble_application_bundle=assemble,
    )

    assert result.status is SelectiveBundleAssemblyStatus.COMPLETED
    assert [item.status for item in result.items] == [
        BundleAssemblyPlanStatus.ASSEMBLED,
        BundleAssemblyPlanStatus.UNCHANGED,
    ]
    assert [call.application_plan_id for call in calls] == [
        "plan-a",
        "plan-b",
    ]
    assert calls[0].plan_material_manifest_id == "manifest-plan-a"
    assert calls[0].prepared_application_answer_set_id == "answers-plan-a"
    assert maximum == 1


@pytest.mark.asyncio
async def test_invalid_and_deferred_do_not_consume_budget_and_failure_continues():
    missing = object.__new__(SelectiveBatchPlanResult)
    for name, value in {
        "application_plan_id": "plan-missing",
        "job_id": "job-missing",
        "selection_status": BatchPlanSelectionStatus.SELECTED,
        "execution_status": BatchPlanExecutionStatus.COMPLETED,
        "preparation_run_id": "preparation-run-plan-missing",
        "attention_item_ids": (),
        "reason_code": None,
        "source_reason_code": None,
        "assembly_lineage": None,
    }.items():
        object.__setattr__(missing, name, value)
    deferred = SelectiveBatchPlanResult(
        application_plan_id="plan-deferred",
        job_id="job-deferred",
        selection_status=BatchPlanSelectionStatus.SELECTED,
        execution_status=BatchPlanExecutionStatus.DEFERRED,
        preparation_run_id=None,
        attention_item_ids=(),
        reason_code="SINGLE_JOB_DEFERRED",
        source_reason_code="UPSTREAM_DEFERRED",
    )
    preparation = object.__new__(SelectiveBatchPreparationResult)
    for name, value in {
        "status": SelectiveBatchPreparationStatus.PARTIAL_FAILURE,
        "subject_id": SUBJECT,
        "evaluated_at": NOW,
        "queue_snapshot_hash": "snapshot",
        "items": (
            missing,
            deferred,
            _successful("plan-fails"),
            _successful("plan-succeeds"),
        ),
    }.items():
        object.__setattr__(preparation, name, value)
    calls = []

    def assemble(command):
        calls.append(command)
        if command.application_plan_id == "plan-fails":
            return AssembleApplicationBundleResult(
                ApplicationBundleAssemblyStatus.FAILED,
                None,
                None,
                None,
                None,
                False,
                "synthetic failure",
            )
        return _assembly_result(
            command, ApplicationBundleAssemblyStatus.CREATED
        )

    result = await run_selective_bundle_assembly(
        SelectiveBundleAssemblyCommand(
            subject_id=SUBJECT,
            now=NOW,
            invocation_id="cycle-002:bundle",
            preparation_result=preparation,
            max_assemblies=2,
        ),
        plan_execution_context_binder=_bind,
        assemble_application_bundle=assemble,
    )

    assert result.status is SelectiveBundleAssemblyStatus.PARTIAL_FAILURE
    assert [item.status for item in result.items] == [
        BundleAssemblyPlanStatus.SKIPPED_MISSING_BINDING,
        BundleAssemblyPlanStatus.SKIPPED_NOT_PREPARED,
        BundleAssemblyPlanStatus.FAILED,
        BundleAssemblyPlanStatus.ASSEMBLED,
    ]
    assert result.items[0].reason is (
        BundleAssemblyFailureReason.PREPARATION_RESULT_INVALID
    )
    assert [call.application_plan_id for call in calls] == [
        "plan-fails",
        "plan-succeeds",
    ]

    zero_budget = await run_selective_bundle_assembly(
        SelectiveBundleAssemblyCommand(
            subject_id=SUBJECT,
            now=NOW,
            invocation_id="cycle-002:bundle-zero",
            preparation_result=preparation,
            max_assemblies=0,
        ),
        plan_execution_context_binder=_bind,
        assemble_application_bundle=assemble,
    )
    assert zero_budget.status is SelectiveBundleAssemblyStatus.NOOP
    assert zero_budget.items == ()
    assert len(calls) == 2
