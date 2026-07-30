from __future__ import annotations

import inspect
from datetime import datetime, timezone

import core.application_answers as answers_module
import core.application_preparation_orchestrator as preparation_module
import core.cover_letter_draft as draft_module
import core.cover_letter_evidence as evidence_module
import core.cover_letter_fact_qa as fact_qa_module
from core.application_answers import (
    PrepareApplicationAnswersResult,
    PreparedApplicationAnswerSetFailureReason,
    PreparedApplicationAnswerSetStatus,
    UnresolvedAnswerReason,
    _APPLICATION_ANSWERS_FAILURE_REASON_MAP,
    application_answers_public_result,
)
from core.application_preparation_orchestrator import (
    APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION,
    COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    ApplicationAnswersStopReason,
    ApplicationPreparationRecipe,
    ApplicationPreparationStage,
    ApplicationPreparationStageDefinition,
    ApplicationPreparationStatus,
    CoverLetterDraftStopReason,
    CoverLetterEvidenceStopReason,
    CoverLetterFactQAStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PrivateHomeApplicationPreparationRunRepository,
    PublicPreparationStageResult,
    PublicStageDirective,
    PublicStageStatus,
    RequiredApplicationMaterialPolicy,
    RunApplicationPreparationCommand,
    run_application_preparation,
)
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.cover_letter_draft import (
    CoverLetterDraftFailureReason,
    CoverLetterDraftStatus,
    DraftCoverLetterResult,
    _COVER_LETTER_DRAFT_FAILURE_REASON_MAP,
    cover_letter_draft_public_result,
)
from core.cover_letter_evidence import (
    CoverLetterEvidenceFailureReason,
    CoverLetterEvidenceSnapshotStatus,
    CreateCoverLetterEvidenceSnapshotResult,
    _COVER_LETTER_EVIDENCE_FAILURE_REASON_MAP,
    cover_letter_evidence_public_result,
)
from core.cover_letter_fact_qa import (
    CoverLetterFactQAFailureReason,
    CoverLetterFactQAStatus,
    RunCoverLetterFactQAResult,
    _COVER_LETTER_FACT_QA_FAILURE_REASON_MAP,
    cover_letter_fact_qa_public_result,
)
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)


def _hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def test_all_four_stage_mappings_and_mixed_p2b4_run(
    tmp_path,
) -> None:
    mappings = (
        (
            CoverLetterEvidenceFailureReason,
            _COVER_LETTER_EVIDENCE_FAILURE_REASON_MAP,
        ),
        (
            CoverLetterDraftFailureReason,
            _COVER_LETTER_DRAFT_FAILURE_REASON_MAP,
        ),
        (
            CoverLetterFactQAFailureReason,
            _COVER_LETTER_FACT_QA_FAILURE_REASON_MAP,
        ),
        (
            PreparedApplicationAnswerSetFailureReason,
            _APPLICATION_ANSWERS_FAILURE_REASON_MAP,
        ),
    )
    for reason_type, mapping in mappings:
        assert set(mapping) == set(reason_type)
        assert all(
            type(value).__name__.endswith("StopReason")
            for value in mapping.values()
        )
    for module in (
        evidence_module,
        draft_module,
        fact_qa_module,
        answers_module,
    ):
        assert "legacy_stopped" not in inspect.getsource(module)

    home = PrivateHome(tmp_path / "private")
    plan = ApplicationPlan.create(
        subject_id="subject-1",
        job_id="job-1",
        job_revision=1,
        job_content_hash=_hash("job"),
        priority_decision_id="priority-1",
        policy_id="priority-policy-v1",
        policy_version=1,
        policy_content_hash="a" * 64,
        accepted_job_intent_id="intent-1",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
    )
    plans = PrivateHomeApplicationPlanRepository(home)
    assert plans.save(plan).plan == plan
    typed_stages = {
        ApplicationPreparationStage.BASE_RESUME_SELECTION,
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
        ApplicationPreparationStage.RESUME_EVIDENCE,
        ApplicationPreparationStage.RESUME_TAILORING,
        ApplicationPreparationStage.RESUME_FACT_QA,
        ApplicationPreparationStage.BASE_LATEX_SELECTION,
        ApplicationPreparationStage.RESUME_PUBLICATION,
        ApplicationPreparationStage.RESUME_MANIFEST,
        ApplicationPreparationStage.COVER_LETTER_EVIDENCE,
        ApplicationPreparationStage.COVER_LETTER_DRAFT,
        ApplicationPreparationStage.COVER_LETTER_FACT_QA,
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        ApplicationPreparationStage.COVER_LETTER_MANIFEST,
        ApplicationPreparationStage.APPLICATION_ANSWERS,
    }

    def invoke(request):
        outputs = {
            key: f"{key}-1"
            for key in preparation_module._REQUIRED_OUTPUTS[request.stage]
        }
        directive = (
            PublicStageDirective.PASSED
            if request.stage
            is ApplicationPreparationStage.RESUME_VISUAL_QA
            else PublicStageDirective.CONTINUE
        )
        if request.stage in typed_stages:
            return PublicPreparationStageResult.completed(
                stage=request.stage,
                result_id=f"typed-{request.stage.value.lower()}",
                result_content_hash=_hash(request.stage.value),
                outputs=outputs,
            )
        return PublicPreparationStageResult.legacy_success(
            stage=request.stage,
            status=PublicStageStatus.CREATED,
            public_status="CREATED",
            result_id=f"legacy-{request.stage.value.lower()}",
            result_content_hash=_hash(request.stage.value),
            outputs=outputs,
            directive=directive,
        )

    recipe = ApplicationPreparationRecipe(
        input_binding_hash=_hash("mixed-p2b4c"),
        stages=tuple(
            ApplicationPreparationStageDefinition(
                stage=stage,
                public_callable_name=f"public-{stage.value.lower()}",
                slice_contract_version="synthetic-v1",
                slice_policy_version="synthetic-policy-v1",
                configuration_hash=_hash(f"config:{stage.value}"),
                invoke=invoke,
            )
            for stage in preparation_module.APPLICATION_PREPARATION_STAGE_ORDER
        ),
        required_material_policy=RequiredApplicationMaterialPolicy.v1(),
    )
    run = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=plan.subject_id,
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=plans,
        recipe=recipe,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
    )
    assert run.status is ApplicationPreparationStatus.COMPLETED
    assert run.run is not None
    by_stage = {item.stage: item for item in run.run.stage_results}
    assert all(not by_stage[stage].is_legacy_untyped for stage in typed_stages)
    assert by_stage[
        ApplicationPreparationStage.RESUME_COMPILATION
    ].is_legacy_untyped


async def test_cover_letter_evidence_and_draft_deferrals_are_typed() -> None:
    evidence = cover_letter_evidence_public_result(
        CreateCoverLetterEvidenceSnapshotResult(
            status=CoverLetterEvidenceSnapshotStatus.DEFERRED_NO_EVIDENCE,
            subject_id="subject-1",
            application_plan_id="plan-1",
            snapshot=None,
            write_result=None,
            reason_code=None,
            retryable=False,
            message="No evidence.",
        )
    )
    draft = cover_letter_draft_public_result(
        DraftCoverLetterResult(
            status=CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN,
            subject_id="subject-1",
            application_plan_id="plan-1",
            draft_binding="a" * 64,
            draft=None,
            write_result=None,
            reason_code=CoverLetterDraftFailureReason.AGENT_OUTPUT_UNSAFE,
            retryable=False,
            message="Unsafe output.",
        )
    )
    assert (
        evidence.stop_reason.code
        is CoverLetterEvidenceStopReason.NO_USABLE_EVIDENCE
    )
    assert (
        draft.stop_reason.code
        is CoverLetterDraftStopReason.AGENT_OUTPUT_UNSAFE
    )
    assert evidence.outcome is PreparationStageOutcome.DEFERRED
    assert draft.outcome is PreparationStageOutcome.DEFERRED


async def test_application_answer_fact_choice_and_attestation_stay_distinct() -> None:
    cases = (
        (
            (UnresolvedAnswerReason.MISSING_FACT,),
            ApplicationAnswersStopReason.USER_FACT_REQUIRED,
        ),
        (
            (UnresolvedAnswerReason.REQUIRES_USER_CHOICE,),
            ApplicationAnswersStopReason.USER_CHOICE_REQUIRED,
        ),
        (
            (UnresolvedAnswerReason.REQUIRES_ATTESTATION,),
            ApplicationAnswersStopReason.USER_ATTESTATION_REQUIRED,
        ),
        (
            (
                UnresolvedAnswerReason.REQUIRES_ATTESTATION,
                UnresolvedAnswerReason.REQUIRES_USER_CHOICE,
            ),
            (
                ApplicationAnswersStopReason
                .USER_CHOICE_AND_ATTESTATION_REQUIRED
            ),
        ),
    )
    for unresolved_reasons, expected in cases:
        public = application_answers_public_result(
            PrepareApplicationAnswersResult(
                status=(
                    PreparedApplicationAnswerSetStatus.DEFERRED_NEEDS_HUMAN
                ),
                answer_set=None,
                reason_code=None,
                retryable=False,
                message="User input is required.",
                unresolved_reasons=unresolved_reasons,
            )
        )
        assert public.stop_reason.code is expected
        assert public.outcome is PreparationStageOutcome.DEFERRED


async def test_unsupported_claim_and_internal_failure_remain_separate() -> None:
    unsupported = PreparationStopReasonEnvelope(
        stage=ApplicationPreparationStage.COVER_LETTER_FACT_QA,
        code=CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM,
        contract_version=COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        outcome=PreparationStageOutcome.DEFERRED,
    )
    failed = cover_letter_fact_qa_public_result(
        RunCoverLetterFactQAResult(
            status=CoverLetterFactQAStatus.FAILED,
            subject_id="subject-1",
            application_plan_id="plan-1",
            result_binding="",
            result=None,
            write_result=None,
            reason_code=(
                CoverLetterFactQAFailureReason.RESULT_INTEGRITY_FAILURE
            ),
            retryable=False,
            message="Integrity failure.",
        )
    )
    assert unsupported.code is CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM
    assert unsupported.outcome is PreparationStageOutcome.DEFERRED
    assert (
        failed.stop_reason.code
        is CoverLetterFactQAStopReason.RESULT_INTEGRITY_FAILURE
    )
    assert failed.outcome is PreparationStageOutcome.FAILED
    assert APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION == (
        "application-answers-stop-reasons-v1"
    )
