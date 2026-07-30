"""Focused acceptance tests for P2b4a typed preparation stop reasons."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

import core.application_preparation_orchestrator as preparation
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    APPLICATION_PREPARATION_STAGE_ORDER,
    BASE_LATEX_STOP_REASON_CONTRACT_VERSION,
    LEGACY_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    ApplicationPreparationRecipe,
    ApplicationPreparationRun,
    ApplicationPreparationRunReadStatus,
    ApplicationPreparationRunStatus,
    ApplicationPreparationStage,
    ApplicationPreparationStageDefinition,
    ApplicationPreparationStageResult,
    ApplicationPreparationStatus,
    BaseLatexPreparationStopReason,
    PreparationStageExecutionStatus,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PrivateHomeApplicationPreparationRunRepository,
    PublicPreparationStageResult,
    PublicStageStatus,
    RequiredApplicationMaterialPolicy,
    RunApplicationPreparationCommand,
    run_application_preparation,
)
from core.base_latex_selection import (
    BaseLatexSelectionFailureReason,
    BaseLatexSelectionStatus,
    SelectBaseLatexVersionResult,
    base_latex_selection_public_result,
)
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from core.preparation_invocation import PreparationInvocationBinding


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
SUBJECT = "subject-stop-reason-synthetic"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reason(
    code: BaseLatexPreparationStopReason,
    outcome: PreparationStageOutcome,
) -> PreparationStopReasonEnvelope:
    return PreparationStopReasonEnvelope(
        stage=ApplicationPreparationStage.BASE_LATEX_SELECTION,
        code=code,
        contract_version=BASE_LATEX_STOP_REASON_CONTRACT_VERSION,
        outcome=outcome,
    )


def _invocation_ref():
    return PreparationInvocationBinding.create(
        subject_id=SUBJECT,
        application_plan_id="application-plan-synthetic",
        invocation_id="invocation-stop-reason-test",
        orchestration_contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        created_at=NOW,
    ).reference


async def test_typed_completed_deferred_and_failed_results_round_trip() -> None:
    public_results = (
        PublicPreparationStageResult.completed(
            stage=ApplicationPreparationStage.BASE_LATEX_SELECTION,
            result_id="base-latex-selection-created",
            result_content_hash="a" * 64,
            outputs={
                "base_latex_selection_id": "base-latex-selection-created"
            },
        ),
        PublicPreparationStageResult.deferred(
            stage=ApplicationPreparationStage.BASE_LATEX_SELECTION,
            stop_reason=_reason(
                BaseLatexPreparationStopReason
                .USER_REQUIREMENT_UNSATISFIABLE,
                PreparationStageOutcome.DEFERRED,
            ),
        ),
        PublicPreparationStageResult.failed(
            stage=ApplicationPreparationStage.BASE_LATEX_SELECTION,
            stop_reason=_reason(
                BaseLatexPreparationStopReason
                .DECISION_INTEGRITY_FAILURE,
                PreparationStageOutcome.FAILED,
            ),
        ),
    )

    for public_result in public_results:
        persisted = ApplicationPreparationStageResult.from_public(
            public_result,
            preparation_invocation_ref=_invocation_ref(),
        )
        restored = preparation._stage_result_from_dict(
            persisted.to_dict(),
            run_contract_version=(
                APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
            ),
        )
        assert restored == persisted
        assert not restored.is_legacy_untyped
    assert public_results[1].stop_reason is not None
    assert public_results[2].stop_reason is not None
    adapted = base_latex_selection_public_result(
        SelectBaseLatexVersionResult(
            status=BaseLatexSelectionStatus.DEFERRED_NEEDS_HUMAN,
            subject_id=SUBJECT,
            application_plan_id="application-plan-synthetic",
            selection_binding="f" * 64,
            candidate_set_hash="e" * 64,
            decision=None,
            write_result=None,
            reason_code=(
                BaseLatexSelectionFailureReason
                .USER_REQUIREMENT_UNSATISFIABLE
            ),
            retryable=False,
            message="Synthetic user choice is required.",
        )
    )
    assert adapted.outcome is PreparationStageOutcome.DEFERRED
    assert adapted.stop_reason is not None
    assert (
        adapted.stop_reason.code
        is BaseLatexPreparationStopReason.USER_REQUIREMENT_UNSATISFIABLE
    )


async def test_typed_reason_registry_fails_closed() -> None:
    with pytest.raises(TypeError):
        PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage.BASE_LATEX_SELECTION,
            code="USER_REQUIREMENT_UNSATISFIABLE",  # type: ignore[arg-type]
            contract_version=BASE_LATEX_STOP_REASON_CONTRACT_VERSION,
            outcome=PreparationStageOutcome.DEFERRED,
        )
    with pytest.raises(ValueError):
        PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage.RESUME_VISUAL_QA,
            code=(
                BaseLatexPreparationStopReason
                .USER_REQUIREMENT_UNSATISFIABLE
            ),
            contract_version=BASE_LATEX_STOP_REASON_CONTRACT_VERSION,
            outcome=PreparationStageOutcome.DEFERRED,
        )
    with pytest.raises(ValueError):
        _reason(
            BaseLatexPreparationStopReason
            .USER_REQUIREMENT_UNSATISFIABLE,
            PreparationStageOutcome.FAILED,
        )
    with pytest.raises(ValueError):
        PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage.BASE_LATEX_SELECTION,
            code=(
                BaseLatexPreparationStopReason
                .USER_REQUIREMENT_UNSATISFIABLE
            ),
            contract_version="unregistered-reason-contract-v2",
            outcome=PreparationStageOutcome.DEFERRED,
        )


def _legacy_run() -> ApplicationPreparationRun:
    stage_content = {
        "execution_status": "DEFERRED",
        "human_attention_required": True,
        "outputs": [],
        "public_status": "DEFERRED_NO_RESUME",
        "reason_code": "NO_RESUME_AVAILABLE",
        "result_content_hash": None,
        "result_id": None,
        "retryable": False,
        "stage": "BASE_RESUME_SELECTION",
    }
    stage_value = {
        **stage_content,
        "stage_content_hash": preparation._canonical_hash(stage_content),
    }
    stage = preparation._stage_result_from_dict(
        stage_value,
        run_contract_version=(
            LEGACY_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
    )
    identity = {
        "application_plan_id": "application-plan-legacy",
        "completed_roles": [],
        "contract_version": (
            LEGACY_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        "deferred_reason": "NO_RESUME_AVAILABLE",
        "deferred_stage": "BASE_RESUME_SELECTION",
        "failed_reason": None,
        "failed_stage": None,
        "final_plan_material_manifest_id": None,
        "final_prepared_application_answer_set_id": None,
        "human_attention_required": True,
        "job_content_hash": "d" * 64,
        "job_id": "job-legacy",
        "job_revision": 1,
        "overall_status": "DEFERRED",
        "preparation_binding": "a" * 64,
        "recipe_metadata_hash": "b" * 64,
        "required_material_policy_hash": "c" * 64,
        "required_material_policy_id": "required-application-materials-v1",
        "required_material_policy_version": (
            "required-application-materials-v1"
        ),
        "stage_hashes": [stage.stage_content_hash],
        "subject_id": SUBJECT,
    }
    run_id = "application-preparation-run-" + preparation._canonical_hash(
        identity
    )
    content = {
        "application_plan_id": identity["application_plan_id"],
        "completed_at": preparation._rfc3339(NOW),
        "completed_roles": [],
        "contract_version": identity["contract_version"],
        "deferred_reason": identity["deferred_reason"],
        "deferred_stage": identity["deferred_stage"],
        "failed_reason": None,
        "failed_stage": None,
        "final_plan_material_manifest_id": None,
        "final_prepared_application_answer_set_id": None,
        "human_attention_required": True,
        "job_content_hash": identity["job_content_hash"],
        "job_id": identity["job_id"],
        "job_revision": 1,
        "overall_status": "DEFERRED",
        "preparation_binding": identity["preparation_binding"],
        "recipe_metadata_hash": identity["recipe_metadata_hash"],
        "required_material_policy_hash": (
            identity["required_material_policy_hash"]
        ),
        "required_material_policy_id": (
            identity["required_material_policy_id"]
        ),
        "required_material_policy_version": (
            identity["required_material_policy_version"]
        ),
        "run_id": run_id,
        "stage_results": [stage.to_dict()],
        "started_at": preparation._rfc3339(NOW),
        "subject_id": SUBJECT,
    }
    return ApplicationPreparationRun(
        run_id=run_id,
        contract_version=identity["contract_version"],
        preparation_binding=identity["preparation_binding"],
        recipe_metadata_hash=identity["recipe_metadata_hash"],
        required_material_policy_id=identity[
            "required_material_policy_id"
        ],
        required_material_policy_version=identity[
            "required_material_policy_version"
        ],
        required_material_policy_hash=identity[
            "required_material_policy_hash"
        ],
        subject_id=SUBJECT,
        application_plan_id=identity["application_plan_id"],
        job_id=identity["job_id"],
        job_revision=1,
        job_content_hash=identity["job_content_hash"],
        stage_results=(stage,),
        final_plan_material_manifest_id=None,
        final_prepared_application_answer_set_id=None,
        completed_roles=(),
        human_attention_required=True,
        deferred_stage=ApplicationPreparationStage.BASE_RESUME_SELECTION,
        deferred_reason="NO_RESUME_AVAILABLE",
        failed_stage=None,
        failed_reason=None,
        overall_status=ApplicationPreparationRunStatus.DEFERRED,
        run_content_hash=preparation._canonical_hash(content),
        started_at=NOW,
        completed_at=NOW,
    )


async def test_v1_run_bytes_and_untyped_reason_survive_restart(tmp_path) -> None:
    home = PrivateHome(tmp_path / "private")
    repository = PrivateHomeApplicationPreparationRunRepository(home)
    legacy = _legacy_run()
    assert repository.save(legacy).run == legacy
    path = repository._path(legacy.subject_id, legacy.run_id)
    original = path.read_bytes()

    restarted = PrivateHomeApplicationPreparationRunRepository(home)
    read = restarted.get(
        subject_id=legacy.subject_id, run_id=legacy.run_id
    )

    assert read.status is ApplicationPreparationRunReadStatus.FOUND
    assert read.run is not None
    assert read.run.to_dict() == legacy.to_dict()
    assert read.run.stage_results[0].is_legacy_untyped
    assert read.run.stage_results[0].stop_reason is None
    assert path.read_bytes() == original


_OUTPUTS = {
    ApplicationPreparationStage.BASE_RESUME_SELECTION: {
        "resume_selection_decision_id": "resume-selection-1",
        "resume_id": "resume-1",
    },
    ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: {
        "source_resume_projection_id": "source-projection-1"
    },
    ApplicationPreparationStage.RESUME_EVIDENCE: {
        "resume_evidence_snapshot_id": "resume-evidence-1"
    },
    ApplicationPreparationStage.RESUME_TAILORING: {
        "tailored_resume_draft_id": "resume-draft-1"
    },
    ApplicationPreparationStage.RESUME_FACT_QA: {
        "resume_fact_qa_result_id": "resume-fact-qa-1"
    },
}


async def test_orchestrator_consumes_typed_and_explicit_legacy_results(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan = ApplicationPlan.create(
        subject_id=SUBJECT,
        job_id="job-mixed",
        job_revision=1,
        job_content_hash=_hash("job-mixed"),
        priority_decision_id="priority-mixed",
        policy_id="priority-policy-v1",
        policy_version=1,
        policy_content_hash="e" * 64,
        accepted_job_intent_id="intent-mixed",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
    )
    plans = PrivateHomeApplicationPlanRepository(home)
    assert plans.save(plan).plan == plan
    calls: list[ApplicationPreparationStage] = []

    def invoke(request):
        calls.append(request.stage)
        if (
            request.stage
            is ApplicationPreparationStage.BASE_LATEX_SELECTION
        ):
            return PublicPreparationStageResult.deferred(
                stage=request.stage,
                stop_reason=_reason(
                    BaseLatexPreparationStopReason
                    .USER_REQUIREMENT_UNSATISFIABLE,
                    PreparationStageOutcome.DEFERRED,
                ),
                human_attention_required=True,
            )
        return PublicPreparationStageResult.legacy_success(
            stage=request.stage,
            status=PublicStageStatus.CREATED,
            public_status="CREATED",
            result_id=f"legacy-result-{request.stage.value.lower()}",
            result_content_hash=_hash(request.stage.value),
            outputs=_OUTPUTS[request.stage],
        )

    recipe = ApplicationPreparationRecipe(
        input_binding_hash=_hash("mixed-recipe"),
        stages=tuple(
            ApplicationPreparationStageDefinition(
                stage=stage,
                public_callable_name=f"public-{stage.value.lower()}",
                slice_contract_version="synthetic-v1",
                slice_policy_version="synthetic-policy-v1",
                configuration_hash=_hash(stage.value),
                invoke=invoke,
            )
            for stage in APPLICATION_PREPARATION_STAGE_ORDER
        ),
        required_material_policy=RequiredApplicationMaterialPolicy.v1(),
    )
    result = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=plans,
        recipe=recipe,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
    )

    assert result.status is ApplicationPreparationStatus.DEFERRED
    assert result.run is not None
    assert calls == list(APPLICATION_PREPARATION_STAGE_ORDER[:6])
    assert all(
        item.is_legacy_untyped for item in result.run.stage_results[:5]
    )
    final = result.run.stage_results[-1]
    assert final.outcome is PreparationStageOutcome.DEFERRED
    assert final.stop_reason is not None
    assert (
        final.stop_reason.code
        is BaseLatexPreparationStopReason.USER_REQUIREMENT_UNSATISFIABLE
    )
