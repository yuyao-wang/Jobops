"""Focused P2c10b1 exact execution-context binding tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

import core.application_preparation_orchestrator as preparation_module
from core.application_assembly_execution_context import (
    LoadApplicationAssemblyExecutionContextResult,
    LoadApplicationAssemblyExecutionContextStatus,
)
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    PREPARATION_ASSEMBLY_LINEAGE_CONTRACT_VERSION,
    PreparationAssemblyLineage,
)
from core.plan_assembly_execution_context_binding import (
    BindPlanAssemblyExecutionContextCommand,
    BindPlanAssemblyExecutionContextResult,
    BindPlanAssemblyExecutionContextStatus,
    PrivateHomePlanAssemblyExecutionContextBindingRepository,
    bind_plan_assembly_execution_context,
)
from core.plan_execution_policy import (
    DecidePlanExecutionPolicyResult,
    DecidePlanExecutionPolicyStatus,
)
from core.application_bundle_assembly import ApplicationBundleAssemblyStatus
from core.selective_bundle_assembly import (
    BundleAssemblyPlanStatus,
    SelectiveBundleAssemblyCommand,
    run_selective_bundle_assembly,
)
from core.verified_application_execution_profile import (
    ProjectVerifiedApplicationExecutionProfileResult,
    ProjectVerifiedApplicationExecutionProfileStatus,
)
from tests.test_application_bundle_assembly import NOW, _setup
from tests.test_selective_bundle_assembly import (
    NOW as SELECTIVE_NOW,
    SUBJECT,
    _assembly_result,
    _binding,
    _preparation,
    _successful,
)


def _lineage(
    subject_id: str, plan_id: str, *, run_suffix: str = ""
) -> PreparationAssemblyLineage:
    values = {
        "subject_id": subject_id,
        "application_plan_id": plan_id,
        "preparation_run_id": f"preparation-run-{plan_id}{run_suffix}",
        "preparation_run_contract_version": (
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        "plan_material_manifest_id": f"manifest-{plan_id}",
        "prepared_application_answer_set_id": f"answers-{plan_id}",
        "preparation_completion_hash": preparation_module._canonical_hash(
            {"plan": plan_id, "run_suffix": run_suffix}
        ),
        "contract_version": PREPARATION_ASSEMBLY_LINEAGE_CONTRACT_VERSION,
    }
    return PreparationAssemblyLineage(
        **values,
        lineage_hash=preparation_module._canonical_hash(values),
    )


def _command(parts, invocation_id="p2c10b1-test"):
    return BindPlanAssemblyExecutionContextCommand(
        subject_id=parts["plan"].subject_id,
        application_plan_id=parts["plan"].plan_id,
        preparation_lineage=_lineage(
            parts["plan"].subject_id, parts["plan"].plan_id
        ),
        invocation_id=invocation_id,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_exact_profile_policy_context_order_and_zero_call_replay(tmp_path):
    parts = _setup(tmp_path)
    repository = PrivateHomePlanAssemblyExecutionContextBindingRepository(
        parts["home"]
    )
    order = []

    def profile(command):
        order.append(("profile", command.invocation_id))
        return ProjectVerifiedApplicationExecutionProfileResult(
            ProjectVerifiedApplicationExecutionProfileStatus.UNCHANGED,
            snapshot=parts["profile"],
        )

    def policy(command):
        order.append(("policy", command.invocation_id))
        return DecidePlanExecutionPolicyResult(
            DecidePlanExecutionPolicyStatus.UNCHANGED,
            record=parts["execution_policy"],
        )

    def context(command):
        order.append(("context", command.verified_profile_id))
        return LoadApplicationAssemblyExecutionContextResult(
            LoadApplicationAssemblyExecutionContextStatus.READY,
            context=parts["execution_context"],
        )

    first = await bind_plan_assembly_execution_context(
        _command(parts),
        plan_provider=parts["plan_repository"],
        verified_profile_projector=profile,
        execution_policy_decider=policy,
        execution_context_loader=context,
        repository=repository,
    )
    replay = await bind_plan_assembly_execution_context(
        _command(parts),
        plan_provider=parts["plan_repository"],
        verified_profile_projector=lambda _command: pytest.fail(
            "profile projector called during replay"
        ),
        execution_policy_decider=lambda _command: pytest.fail(
            "policy decider called during replay"
        ),
        execution_context_loader=lambda _command: pytest.fail(
            "context loader called during replay"
        ),
        repository=repository,
    )

    assert [item[0] for item in order] == ["profile", "policy", "context"]
    assert first.status is BindPlanAssemblyExecutionContextStatus.CREATED
    assert replay.status is BindPlanAssemblyExecutionContextStatus.UNCHANGED
    assert replay.binding == first.binding
    assert first.binding.verified_profile_ref.record_id == (
        parts["profile"].profile_snapshot_id
    )
    assert first.binding.execution_policy_ref.record_id == (
        parts["execution_policy"].record_id
    )
    assert first.binding.application_assembly_context_hash == (
        parts["execution_context"].context_binding_hash
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_stage", "expected_calls"),
    [
        ("profile", ["profile"]),
        ("policy", ["profile", "policy"]),
        ("context", ["profile", "policy", "context"]),
    ],
)
async def test_fail_closed_ordered_prefix(tmp_path, failure_stage, expected_calls):
    parts = _setup(tmp_path)
    calls = []

    def profile(_command):
        calls.append("profile")
        if failure_stage == "profile":
            return ProjectVerifiedApplicationExecutionProfileResult(
                ProjectVerifiedApplicationExecutionProfileStatus.NOT_READY,
                failure_code="SYNTHETIC_MISSING",
            )
        return ProjectVerifiedApplicationExecutionProfileResult(
            ProjectVerifiedApplicationExecutionProfileStatus.UNCHANGED,
            snapshot=parts["profile"],
        )

    def policy(_command):
        calls.append("policy")
        if failure_stage == "policy":
            return DecidePlanExecutionPolicyResult(
                DecidePlanExecutionPolicyStatus.NOT_READY,
                reason="AUTHORITY_CONFIGURATION_REQUIRED",
            )
        return DecidePlanExecutionPolicyResult(
            DecidePlanExecutionPolicyStatus.UNCHANGED,
            record=parts["execution_policy"],
        )

    def context(_command):
        calls.append("context")
        if failure_stage == "context":
            return LoadApplicationAssemblyExecutionContextResult(
                LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
                failure_reason="CROSS_BINDING_MISMATCH",
            )
        raise AssertionError("unexpected context success")

    result = await bind_plan_assembly_execution_context(
        _command(parts, f"failure-{failure_stage}"),
        plan_provider=parts["plan_repository"],
        verified_profile_projector=profile,
        execution_policy_decider=policy,
        execution_context_loader=context,
        repository=PrivateHomePlanAssemblyExecutionContextBindingRepository(
            parts["home"]
        ),
    )

    assert calls == expected_calls
    assert result.binding is None
    assert result.status in {
        BindPlanAssemblyExecutionContextStatus.NOT_READY,
        BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
    }


@pytest.mark.asyncio
async def test_invocation_conflict_and_binding_receipt_contain_no_payload(
    tmp_path,
):
    parts = _setup(tmp_path)
    repository = PrivateHomePlanAssemblyExecutionContextBindingRepository(
        parts["home"]
    )

    async def run(command):
        return await bind_plan_assembly_execution_context(
            command,
            plan_provider=parts["plan_repository"],
            verified_profile_projector=lambda _command: (
                ProjectVerifiedApplicationExecutionProfileResult(
                    ProjectVerifiedApplicationExecutionProfileStatus.UNCHANGED,
                    snapshot=parts["profile"],
                )
            ),
            execution_policy_decider=lambda _command: (
                DecidePlanExecutionPolicyResult(
                    DecidePlanExecutionPolicyStatus.UNCHANGED,
                    record=parts["execution_policy"],
                )
            ),
            execution_context_loader=lambda _command: (
                LoadApplicationAssemblyExecutionContextResult(
                    LoadApplicationAssemblyExecutionContextStatus.READY,
                    context=parts["execution_context"],
                )
            ),
            repository=repository,
        )

    first = await run(_command(parts, "same-invocation"))
    changed = replace(
        _command(parts, "same-invocation"),
        preparation_lineage=_lineage(
            parts["plan"].subject_id,
            parts["plan"].plan_id,
            run_suffix="-changed",
        ),
    )
    conflict = await run(changed)

    assert first.status is BindPlanAssemblyExecutionContextStatus.CREATED
    assert (
        conflict.status
        is BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE
    )
    serialized = first.binding.to_dict()
    assert set(serialized["verified_profile_ref"]) == {
        "record_id",
        "record_version",
        "record_hash",
    }
    text = str(serialized)
    assert "Synthetic" not in text
    assert "@" not in text
    assert str(tmp_path) not in text


@pytest.mark.asyncio
async def test_selective_budget_counts_context_attempts_and_forwards_exact_refs():
    preparation = _preparation(
        (
            _successful("plan-context-fails"),
            _successful("plan-assembles"),
            _successful("plan-outside-budget"),
        )
    )
    bound = []
    assembly_commands = []

    def binder(command):
        bound.append(command.application_plan_id)
        if command.application_plan_id == "plan-context-fails":
            return BindPlanAssemblyExecutionContextResult(
                BindPlanAssemblyExecutionContextStatus.NOT_READY,
                failure_reason="PROFILE_NOT_READY",
            )
        return BindPlanAssemblyExecutionContextResult(
            BindPlanAssemblyExecutionContextStatus.CREATED,
            binding=_binding(command),
        )

    def assemble(command):
        assembly_commands.append(command)
        return _assembly_result(
            command, ApplicationBundleAssemblyStatus.CREATED
        )

    result = await run_selective_bundle_assembly(
        SelectiveBundleAssemblyCommand(
            subject_id=SUBJECT,
            now=SELECTIVE_NOW,
            invocation_id="selective-context-budget",
            preparation_result=preparation,
            max_assemblies=2,
        ),
        plan_execution_context_binder=binder,
        assemble_application_bundle=assemble,
    )

    assert bound == ["plan-context-fails", "plan-assembles"]
    assert len(assembly_commands) == 1
    command = assembly_commands[0]
    successful = result.items[1]
    assert successful.status is BundleAssemblyPlanStatus.ASSEMBLED
    assert command.verified_profile_id == successful.verified_profile_ref.record_id
    assert command.execution_policy_record_id == (
        successful.execution_policy_ref.record_id
    )
    assert command.execution_context_binding_hash == (
        successful.application_assembly_context_hash
    )
    assert result.summary.selected == 2
    assert result.summary.context_bound == 1
