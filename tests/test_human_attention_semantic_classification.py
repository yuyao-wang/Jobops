from __future__ import annotations

from core.application_answers import UnresolvedAnswerReason
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION,
    RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    RESUME_LAYOUT_REVISION_STOP_REASON_CONTRACT_VERSION,
    RESUME_VISUAL_QA_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStageResult,
    ApplicationPreparationStage,
    LatexCompilationStopReason,
    LatexConstructionStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PublicPreparationStageResult,
    PublicStageDirective,
    PublicStageStatus,
    ResumeFactQAStopReason,
    ResumeLayoutRevisionStopReason,
    ResumeVisualQAStopReason,
    SourceResumeProjectionStopReason,
    _STOP_REASON_CONTRACTS,
)
from core.preparation_invocation import PreparationInvocationBinding
from core.resume_latex_versions import (
    RESUME_LATEX_VERSION_CONTRACT_VERSION,
)
from core.human_attention_queue import (
    HUMAN_ATTENTION_MAPPING_VERSION,
    HumanAttentionAudience,
    HumanAttentionKind,
    HumanAttentionReasonCode,
    HumanAttentionResolutionCapability,
    _LINEAGE_CLASSIFIED_DEFERRED_REASONS,
    _REGISTERED_TYPED_DEFERRED_REASONS,
    _TYPED_DEFERRED_ATTENTION_MAPPINGS,
    _answer_mapping,
    _typed_deferred_mapping,
)
from core.resume_compilation import (
    CompileResumeLatexResult,
    ResumeCompilationFailureReason,
    ResumeCompilationStatus,
)
from core.resume_layout_revision import (
    _downstream_compilation_stop_lineage,
)
from core.private_home import PrivateHome
from tests.test_human_attention_queue import (
    NOW,
    OUTPUTS,
    _Recorder,
    _deferred_recipe,
    _hash,
    _invoke,
    _plan,
    _queue,
    _recipe,
    _typed_deferred_recipe,
)
from core.application_preparation_orchestrator import (
    PrivateHomeApplicationPreparationRunRepository,
)


def test_all_technical_deferred_reasons_have_explicit_classification() -> None:
    registered = {
        (stage, reason)
        for stage, (_version, _reason_type, outcomes) in (
            _STOP_REASON_CONTRACTS.items()
        )
        for reason, outcome in outcomes.items()
        if outcome is PreparationStageOutcome.DEFERRED
    }
    assert registered == set(_REGISTERED_TYPED_DEFERRED_REASONS)
    technical_stages = {
        ApplicationPreparationStage.LATEX_CONSTRUCTION,
        ApplicationPreparationStage.RESUME_COMPILATION,
        ApplicationPreparationStage.RESUME_VISUAL_QA,
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
    }
    technical = {
        key for key in registered if key[0] in technical_stages
    }
    assert len(technical) == 16
    classified = (
        set(_TYPED_DEFERRED_ATTENTION_MAPPINGS)
        | set(_LINEAGE_CLASSIFIED_DEFERRED_REASONS)
    )
    assert classified == registered
    assert HUMAN_ATTENTION_MAPPING_VERSION == "human-attention-mapping-v3"
    assert all(
        mapping.kind is not HumanAttentionKind.MANUAL_REVIEW_REQUIRED
        and mapping.resolution_capability
        is not HumanAttentionResolutionCapability.APPROVE_REVIEW_TARGET
        for mapping in _TYPED_DEFERRED_ATTENTION_MAPPINGS.values()
    )
    assert _TYPED_DEFERRED_ATTENTION_MAPPINGS[
        (
            ApplicationPreparationStage.LATEX_CONSTRUCTION,
            LatexConstructionStopReason.BASE_VERSION_UNREADABLE,
        )
    ].resolution_capability is HumanAttentionResolutionCapability.REPLACE_INPUT
    assert _TYPED_DEFERRED_ATTENTION_MAPPINGS[
        (
            ApplicationPreparationStage.RESUME_COMPILATION,
            LatexCompilationStopReason.COMPILATION_ERROR,
        )
    ].resolution_capability is (
        HumanAttentionResolutionCapability.CORRECT_MATERIAL
    )
    assert _TYPED_DEFERRED_ATTENTION_MAPPINGS[
        (
            ApplicationPreparationStage.RESUME_VISUAL_QA,
            ResumeVisualQAStopReason.RENDERER_UNAVAILABLE,
        )
    ].resolution_capability is (
        HumanAttentionResolutionCapability.OPERATOR_REPAIR
    )
    assert _TYPED_DEFERRED_ATTENTION_MAPPINGS[
        (
            ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
            ResumeLayoutRevisionStopReason.ATTEMPTS_EXHAUSTED,
        )
    ].resolution_capability is (
        HumanAttentionResolutionCapability.CORRECT_MATERIAL
    )


def test_technical_safety_and_legacy_reasons_are_not_reviews(
    tmp_path,
    monkeypatch,
) -> None:
    unsupported = _TYPED_DEFERRED_ATTENTION_MAPPINGS[
        (
            ApplicationPreparationStage.RESUME_FACT_QA,
            ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        )
    ]
    unreadable = _TYPED_DEFERRED_ATTENTION_MAPPINGS[
        (
            ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
            SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE,
        )
    ]
    assert unsupported.resolution_capability is (
        HumanAttentionResolutionCapability.CORRECT_MATERIAL
    )
    assert unreadable.resolution_capability is (
        HumanAttentionResolutionCapability.REPLACE_INPUT
    )

    home = PrivateHome(tmp_path / "private")
    run_repository = PrivateHomeApplicationPreparationRunRepository(home)
    for index, (stage, status) in enumerate(
        (
            (
                ApplicationPreparationStage.RESUME_VISUAL_QA,
                "DEFERRED_NEEDS_HUMAN",
            ),
            (
                ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
                "DEFERRED_ATTEMPTS_EXHAUSTED",
            ),
        )
    ):
        plan, plan_repository = _plan(home, job_id=f"job-legacy-{index}")
        if stage is ApplicationPreparationStage.RESUME_LAYOUT_REVISION:
            recorder = _Recorder()

            def visual_qa(request):
                return PublicPreparationStageResult.legacy_success(
                    stage=request.stage,
                    status=PublicStageStatus.CREATED,
                    public_status="REVISION_REQUIRED",
                    result_id="visual-result-1",
                    result_content_hash=_hash("visual-result-1"),
                    outputs=OUTPUTS[request.stage],
                    directive=PublicStageDirective.REVISION_REQUIRED,
                )

            def layout_revision(request):
                return PublicPreparationStageResult.legacy_stopped(
                    stage=request.stage,
                    status=PublicStageStatus.DEFERRED,
                    public_status=status,
                    reason_code=status,
                    human_attention_required=True,
                )

            recipe = _recipe(
                recorder,
                input_binding=f"legacy-{index}",
                overrides={
                    ApplicationPreparationStage.RESUME_VISUAL_QA: visual_qa,
                    stage: layout_revision,
                },
            )
        else:
            _recorder, recipe = _deferred_recipe(
                stage=stage,
                public_status=status,
                reason_code=status,
                input_binding=f"legacy-{index}",
            )
        _invoke(
            plan=plan,
            plan_repository=plan_repository,
            run_repository=run_repository,
            recipe=recipe,
            now=NOW,
        )
    plan, plan_repository = _plan(home, job_id="job-typed-visual")
    _recorder, recipe = _typed_deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_VISUAL_QA,
        reason=ResumeVisualQAStopReason.RENDERER_UNAVAILABLE,
        contract_version=RESUME_VISUAL_QA_STOP_REASON_CONTRACT_VERSION,
        input_binding="typed-visual",
    )
    _invoke(
        plan=plan,
        plan_repository=plan_repository,
        run_repository=run_repository,
        recipe=recipe,
        now=NOW,
    )
    result = _queue(home)
    assert {item.source_stage for item in result.items} == {
        ApplicationPreparationStage.RESUME_VISUAL_QA,
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
    }
    typed_visual = next(
        item for item in result.items if item.job_id == plan.job_id
    )
    assert typed_visual.audience is HumanAttentionAudience.OPERATOR
    assert typed_visual.attention_kind is (
        HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED
    )
    assert typed_visual.resolution_capability is (
        HumanAttentionResolutionCapability.OPERATOR_REPAIR
    )
    legacy_items = [
        item for item in result.items if item.job_id != plan.job_id
    ]
    assert all(
        item.attention_kind
        is HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER
        and item.resolution_capability
        is HumanAttentionResolutionCapability.NON_OVERRIDABLE
        for item in legacy_items
    )

    plan, plan_repository = _plan(home, job_id="job-unmapped-typed")
    _recorder, recipe = _typed_deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_FACT_QA,
        reason=ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        contract_version=RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        input_binding="unmapped-typed",
    )
    monkeypatch.delitem(
        _TYPED_DEFERRED_ATTENTION_MAPPINGS,
        (
            ApplicationPreparationStage.RESUME_FACT_QA,
            ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        ),
    )
    _invoke(
        plan=plan,
        plan_repository=plan_repository,
        run_repository=run_repository,
        recipe=recipe,
        now=NOW,
    )
    unknown = next(
        item for item in _queue(home).items if item.job_id == plan.job_id
    )
    assert unknown.attention_kind is (
        HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER
    )
    assert unknown.audience is HumanAttentionAudience.OPERATOR
    assert unknown.resolution_capability is (
        HumanAttentionResolutionCapability.NON_OVERRIDABLE
    )


def test_layout_compilation_lineage_selects_content_or_operator_action() -> None:
    def stage_result(
        *,
        status: ResumeCompilationStatus,
        reason: ResumeCompilationFailureReason,
    ) -> ApplicationPreparationStageResult:
        compiled = CompileResumeLatexResult(
            status=status,
            subject_id="subject-a",
            compilation_binding="a" * 64,
            record=None,
            write_result=None,
            reason_code=reason,
            compiler_started=(
                status
                is not ResumeCompilationStatus
                .DEFERRED_COMPILER_UNAVAILABLE
            ),
            diagnostics="",
            retryable=False,
            message="Synthetic typed compilation stop.",
            source_construction_record_id=(
                "resume-layout-revision-" + "b" * 64
            ),
            source_latex_version_id=(
                "resume-latex-version-" + "c" * 64
            ),
            source_application_plan_id="plan-a",
            source_latex_sha256="f" * 64,
            source_contract_version=(
                RESUME_LATEX_VERSION_CONTRACT_VERSION
            ),
        )
        lineage = _downstream_compilation_stop_lineage(
            subject_id="subject-a",
            application_plan_id="plan-a",
            parent_attempt_id=(
                "resume-layout-revision-" + "b" * 64
            ),
            expected_latex_version_id=(
                "resume-latex-version-" + "c" * 64
            ),
            compiled=compiled,
        )
        parent_reason = PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
            code=ResumeLayoutRevisionStopReason.COMPILATION_STOPPED,
            contract_version=(
                RESUME_LAYOUT_REVISION_STOP_REASON_CONTRACT_VERSION
            ),
            outcome=PreparationStageOutcome.DEFERRED,
        )
        invocation = PreparationInvocationBinding.create(
            subject_id="subject-a",
            application_plan_id="plan-a",
            invocation_id="classification-layout-compilation",
            orchestration_contract_version=(
                APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
            ),
            created_at=NOW,
        )
        return ApplicationPreparationStageResult.from_public(
            PublicPreparationStageResult.deferred(
                stage=(
                    ApplicationPreparationStage.RESUME_LAYOUT_REVISION
                ),
                stop_reason=parent_reason,
                result_id="resume-layout-revision-run-" + "d" * 64,
                result_content_hash="e" * 64,
                outputs=lineage.stage_output_references(),
                human_attention_required=True,
            ),
            preparation_invocation_ref=invocation.reference,
        )

    content = _typed_deferred_mapping(
        stage_result(
            status=ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR,
            reason=ResumeCompilationFailureReason.COMPILATION_ERROR,
        ),
        subject_id="subject-a",
        application_plan_id="plan-a",
    )
    infrastructure = _typed_deferred_mapping(
        stage_result(
            status=ResumeCompilationStatus.DEFERRED_COMPILER_UNAVAILABLE,
            reason=ResumeCompilationFailureReason.COMPILER_UNAVAILABLE,
        ),
        subject_id="subject-a",
        application_plan_id="plan-a",
    )
    assert content.resolution_capability is (
        HumanAttentionResolutionCapability.CORRECT_MATERIAL
    )
    assert infrastructure.resolution_capability is (
        HumanAttentionResolutionCapability.OPERATOR_REPAIR
    )


def test_answer_resolution_capabilities_remain_distinct() -> None:
    expected = {
        UnresolvedAnswerReason.MISSING_FACT: (
            HumanAttentionKind.USER_FACT_REQUIRED,
            HumanAttentionResolutionCapability.PROVIDE_FACT,
        ),
        UnresolvedAnswerReason.REQUIRES_USER_CHOICE: (
            HumanAttentionKind.USER_CHOICE_REQUIRED,
            HumanAttentionResolutionCapability.MAKE_CHOICE,
        ),
        UnresolvedAnswerReason.REQUIRES_ATTESTATION: (
            HumanAttentionKind.USER_ATTESTATION_REQUIRED,
            HumanAttentionResolutionCapability.ATTEST,
        ),
    }
    for reason, (kind, capability) in expected.items():
        mapping = _answer_mapping(reason)
        assert mapping.kind is kind
        assert mapping.audience is HumanAttentionAudience.USER
        assert mapping.resolution_capability is capability
    _LINEAGE_CLASSIFIED_DEFERRED_REASONS,
