"""Focused acceptance tests for P2b4e technical-stage typed reasons."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import core.application_preparation_orchestrator as preparation
import core.resume_layout_revision as layout
import core.resume_visual_qa as visual
from core.application_preparation_orchestrator import (
    LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION,
    LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION,
    RESUME_LAYOUT_REVISION_STOP_REASON_CONTRACT_VERSION,
    RESUME_VISUAL_QA_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    LatexCompilationStopReason,
    LatexConstructionStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    ResumeLayoutRevisionStopReason,
    ResumeVisualQAStopReason,
)
from core.resume_compilation import (
    CompileResumeLatexResult,
    ResumeCompilationFailureReason,
    ResumeCompilationStatus,
    resume_compilation_public_result,
)
from core.application_preparation_orchestrator import (
    UnresolvedCompilationSourceState,
)
from core.resume_latex_versions import (
    RESUME_LATEX_VERSION_CONTRACT_VERSION,
)
from core.resume_latex_construction import (
    ConstructResumeLatexResult,
    ResumeLatexConstructionFailureReason,
    ResumeLatexConstructionStatus,
    resume_latex_construction_public_result,
)
from core.resume_layout_revision import (
    RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
    RESUME_LAYOUT_REVISION_POLICY_VERSION,
    ResumeLayoutAttemptOutcome,
    ResumeLayoutRevisionAttempt,
    ResumeLayoutRevisionFailureReason,
    ResumeLayoutRevisionRun,
    ResumeLayoutRevisionStatus,
    ResumeLayoutRevisionWriteResult,
    ResumeLayoutRevisionWriteStatus,
    ReviseResumeLayoutResult,
    resume_layout_revision_public_result,
)
from core.resume_visual_qa import (
    RESUME_VISUAL_QA_CONTRACT_VERSION,
    RESUME_VISUAL_QA_POLICY_VERSION,
    ResumeVisualQAFailureReason,
    ResumeVisualQAFindingSource,
    ResumeVisualQAFindingType,
    ResumeVisualQAResult,
    ResumeVisualQAStatus,
    ResumeVisualQAVerdict,
    ResumeVisualQAWriteResult,
    ResumeVisualQAWriteStatus,
    ReviewResumeVisualQAResult,
    resume_visual_qa_public_result,
)


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
SUBJECT = "subject-technical-stage-synthetic"
HASH = "a" * 64


def test_four_closed_reason_contracts_are_exhaustive_and_registered() -> None:
    pairs = (
        (
            ResumeLatexConstructionFailureReason,
            LatexConstructionStopReason,
            ApplicationPreparationStage.LATEX_CONSTRUCTION,
            LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION,
        ),
        (
            ResumeCompilationFailureReason,
            LatexCompilationStopReason,
            ApplicationPreparationStage.RESUME_COMPILATION,
            LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION,
        ),
        (
            ResumeVisualQAFailureReason,
            ResumeVisualQAStopReason,
            ApplicationPreparationStage.RESUME_VISUAL_QA,
            RESUME_VISUAL_QA_STOP_REASON_CONTRACT_VERSION,
        ),
        (
            ResumeLayoutRevisionFailureReason,
            ResumeLayoutRevisionStopReason,
            ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
            RESUME_LAYOUT_REVISION_STOP_REASON_CONTRACT_VERSION,
        ),
    )
    for source_type, typed_type, stage, version in pairs:
        assert {item.name for item in source_type} == {
            item.name for item in typed_type
        }
        registered_version, registered_type, outcomes = (
            preparation._STOP_REASON_CONTRACTS[stage]
        )
        assert (registered_version, registered_type) == (
            version,
            typed_type,
        )
        assert set(outcomes) == set(typed_type)

    with pytest.raises(TypeError):
        PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage.RESUME_VISUAL_QA,
            code="RENDERER_UNAVAILABLE",  # type: ignore[arg-type]
            contract_version=RESUME_VISUAL_QA_STOP_REASON_CONTRACT_VERSION,
            outcome=PreparationStageOutcome.DEFERRED,
        )


def test_construction_and_compilation_adapters_preserve_every_outcome() -> None:
    construction_deferred = {
        ResumeLatexConstructionFailureReason.BASE_VERSION_UNREADABLE,
        ResumeLatexConstructionFailureReason.CONSTRUCTION_OUTPUT_UNSAFE,
    }
    for reason in ResumeLatexConstructionFailureReason:
        deferred = reason in construction_deferred
        status = (
            ResumeLatexConstructionStatus.DEFERRED_SOURCE_UNREADABLE
            if reason
            is ResumeLatexConstructionFailureReason.BASE_VERSION_UNREADABLE
            else (
                ResumeLatexConstructionStatus.DEFERRED_NEEDS_HUMAN
                if deferred
                else ResumeLatexConstructionStatus.FAILED
            )
        )
        public = resume_latex_construction_public_result(
            ConstructResumeLatexResult(
                status=status,
                subject_id=SUBJECT,
                application_plan_id="plan-1",
                construction_binding=HASH,
                version=None,
                record=None,
                reason_code=reason,
                retryable=False,
                message="Synthetic stopped construction.",
            )
        )
        assert public.stop_reason is not None
        assert public.stop_reason.code.name == reason.name
        assert public.outcome is (
            PreparationStageOutcome.DEFERRED
            if deferred
            else PreparationStageOutcome.FAILED
        )
        assert public.outcome is not PreparationStageOutcome.LEGACY_UNTYPED

    compilation_deferred = {
        ResumeCompilationFailureReason.UNMANAGED_DEPENDENCY,
        ResumeCompilationFailureReason.COMPILER_UNAVAILABLE,
        ResumeCompilationFailureReason.COMPILATION_ERROR,
        ResumeCompilationFailureReason.COMPILATION_TIMEOUT,
        ResumeCompilationFailureReason.PDF_INVALID,
    }
    unresolved_states = {
        ResumeCompilationFailureReason.INVALID_REQUEST: (
            UnresolvedCompilationSourceState.INVALID_REQUEST
        ),
        ResumeCompilationFailureReason.CONSTRUCTION_RECORD_NOT_FOUND: (
            UnresolvedCompilationSourceState.CONSTRUCTION_NOT_FOUND
        ),
        ResumeCompilationFailureReason
        .CONSTRUCTION_RECORD_INTEGRITY_FAILURE: (
            UnresolvedCompilationSourceState
            .CONSTRUCTION_INTEGRITY_FAILURE
        ),
        ResumeCompilationFailureReason.CONSTRUCTION_BINDING_MISMATCH: (
            UnresolvedCompilationSourceState.SOURCE_BINDING_REJECTED
        ),
        ResumeCompilationFailureReason.LATEX_VERSION_NOT_FOUND: (
            UnresolvedCompilationSourceState.LATEX_VERSION_NOT_FOUND
        ),
        ResumeCompilationFailureReason.LATEX_VERSION_INTEGRITY_FAILURE: (
            UnresolvedCompilationSourceState
            .LATEX_VERSION_INTEGRITY_FAILURE
        ),
        ResumeCompilationFailureReason.LATEX_VERSION_BINDING_MISMATCH: (
            UnresolvedCompilationSourceState.SOURCE_BINDING_REJECTED
        ),
        ResumeCompilationFailureReason.SOURCE_HASH_DRIFT: (
            UnresolvedCompilationSourceState.SOURCE_BINDING_REJECTED
        ),
    }
    for reason in ResumeCompilationFailureReason:
        if reason is ResumeCompilationFailureReason.UNMANAGED_DEPENDENCY:
            status = ResumeCompilationStatus.DEFERRED_SOURCE_INCOMPLETE
        elif reason is ResumeCompilationFailureReason.COMPILER_UNAVAILABLE:
            status = ResumeCompilationStatus.DEFERRED_COMPILER_UNAVAILABLE
        elif reason in compilation_deferred:
            status = ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR
        else:
            status = ResumeCompilationStatus.FAILED
        source_kwargs = (
            {
                "unresolved_source_state": unresolved_states[reason],
            }
            if reason in unresolved_states
            else {
                "source_application_plan_id": "plan-synthetic",
                "source_latex_sha256": HASH,
                "source_contract_version": (
                    RESUME_LATEX_VERSION_CONTRACT_VERSION
                ),
            }
        )
        if reason is ResumeCompilationFailureReason.SOURCE_UNREADABLE:
            source_kwargs = {
                "source_application_plan_id": "plan-synthetic",
                "source_latex_sha256": HASH,
                "source_contract_version": (
                    RESUME_LATEX_VERSION_CONTRACT_VERSION
                ),
            }
        public = resume_compilation_public_result(
            CompileResumeLatexResult(
                status=status,
                subject_id=SUBJECT,
                compilation_binding=HASH,
                record=None,
                write_result=None,
                reason_code=reason,
                compiler_started=False,
                diagnostics="",
                retryable=False,
                message="Synthetic stopped compilation.",
                source_construction_record_id="construction-synthetic",
                source_latex_version_id="latex-version-synthetic",
                **source_kwargs,
            )
        )
        assert public.stop_reason is not None
        assert public.stop_reason.code.name == reason.name
        assert public.outcome is (
            PreparationStageOutcome.DEFERRED
            if reason in compilation_deferred
            else PreparationStageOutcome.FAILED
        )
        assert public.outcome is not PreparationStageOutcome.LEGACY_UNTYPED


def test_visual_and_layout_boundaries_remain_distinct_and_typed() -> None:
    visual_deferred = {
        ResumeVisualQAStopReason.RENDERER_UNAVAILABLE,
        ResumeVisualQAStopReason.AGENT_OUTPUT_UNRELIABLE,
    }
    visual_outcomes = preparation._STOP_REASON_CONTRACTS[
        ApplicationPreparationStage.RESUME_VISUAL_QA
    ][2]
    assert {
        reason
        for reason, outcome in visual_outcomes.items()
        if outcome is PreparationStageOutcome.DEFERRED
    } == visual_deferred
    renderer = resume_visual_qa_public_result(
        ReviewResumeVisualQAResult(
            status=ResumeVisualQAStatus.DEFERRED_RENDERER_UNAVAILABLE,
            subject_id=SUBJECT,
            visual_qa_binding=HASH,
            result=None,
            write_result=None,
            reason_code=ResumeVisualQAFailureReason.RENDERER_UNAVAILABLE,
            retryable=False,
            message="Synthetic renderer defer.",
        )
    )
    assert renderer.stop_reason is not None
    assert (
        renderer.stop_reason.code
        is ResumeVisualQAStopReason.RENDERER_UNAVAILABLE
    )

    finding = visual._build_finding(
        order=0,
        finding_type=ResumeVisualQAFindingType.AGENT_OUTPUT_UNRELIABLE,
        source=ResumeVisualQAFindingSource.DETERMINISTIC,
        page_number=0,
        explanation="The bounded result was unreliable.",
    )
    content = {
        "result_id": f"resume-visual-qa-{HASH}",
        "contract_version": RESUME_VISUAL_QA_CONTRACT_VERSION,
        "visual_qa_binding": HASH,
        "subject_id": SUBJECT,
        "compilation_record_id": "compilation-1",
        "compilation_binding": HASH,
        "pdf_sha256": HASH,
        "latex_version_id": "latex-1",
        "latex_source_sha256": HASH,
        "tailored_resume_draft_id": "draft-1",
        "tailored_resume_draft_hash": HASH,
        "renderer_name": "synthetic",
        "renderer_version": "v1",
        "renderer_dpi": 144,
        "policy_version": RESUME_VISUAL_QA_POLICY_VERSION,
        "max_pages": 1,
        "page_count": 1,
        "agent_invoked": True,
        "agent_version": "v1",
        "prompt_version": "v1",
        "model_id": "synthetic",
        "verdict": ResumeVisualQAVerdict.DEFERRED,
        "findings": (finding,),
    }
    qa = ResumeVisualQAResult(
        **content,
        result_content_hash=visual._canonical_hash(
            {
                **content,
                "verdict": ResumeVisualQAVerdict.DEFERRED.value,
                "findings": [finding.to_dict()],
            }
        ),
        validated_at=NOW,
    )
    unreliable = resume_visual_qa_public_result(
        ReviewResumeVisualQAResult(
            status=ResumeVisualQAStatus.DEFERRED_NEEDS_HUMAN,
            subject_id=SUBJECT,
            visual_qa_binding=HASH,
            result=qa,
            write_result=ResumeVisualQAWriteResult(
                status=ResumeVisualQAWriteStatus.CREATED,
                result=qa,
                reason_code=None,
                retryable=False,
            ),
            reason_code=ResumeVisualQAFailureReason.AGENT_OUTPUT_UNRELIABLE,
            retryable=False,
            message="Synthetic unreliable Agent output.",
        )
    )
    assert unreliable.result_id == qa.result_id
    assert unreliable.stop_reason is not None
    assert (
        unreliable.stop_reason.code
        is ResumeVisualQAStopReason.AGENT_OUTPUT_UNRELIABLE
    )
    visual_replay = resume_visual_qa_public_result(
        ReviewResumeVisualQAResult(
            status=ResumeVisualQAStatus.UNCHANGED,
            subject_id=SUBJECT,
            visual_qa_binding=HASH,
            result=qa,
            write_result=ResumeVisualQAWriteResult(
                status=ResumeVisualQAWriteStatus.UNCHANGED,
                result=qa,
                reason_code=None,
                retryable=False,
            ),
            reason_code=None,
            retryable=False,
            message="Synthetic Visual QA replay.",
        )
    )
    assert visual_replay.outcome is PreparationStageOutcome.DEFERRED
    assert visual_replay.result_id == qa.result_id

    layout_deferred = {
        ResumeLayoutRevisionStopReason.RENDERER_UNAVAILABLE,
        ResumeLayoutRevisionStopReason.REVISION_OUTPUT_UNSAFE,
        ResumeLayoutRevisionStopReason.VERSION_REGISTRATION_FAILED,
        ResumeLayoutRevisionStopReason.COMPILATION_STOPPED,
        ResumeLayoutRevisionStopReason.VISUAL_QA_DEFERRED,
        ResumeLayoutRevisionStopReason.VISUAL_QA_FAILED,
        ResumeLayoutRevisionStopReason.ATTEMPTS_EXHAUSTED,
    }
    layout_outcomes = preparation._STOP_REASON_CONTRACTS[
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION
    ][2]
    assert {
        reason
        for reason, outcome in layout_outcomes.items()
        if outcome is PreparationStageOutcome.DEFERRED
    } == layout_deferred
    assert "NO_PROGRESS" not in ResumeLayoutRevisionStopReason.__members__
    assert (
        "DUPLICATE_REVISION"
        not in ResumeLayoutRevisionStopReason.__members__
    )

    attempt = ResumeLayoutRevisionAttempt(
        attempt_number=1,
        input_latex_version_id="latex-1",
        input_compilation_record_id="compilation-1",
        input_visual_qa_result_id="visual-1",
        blocking_finding_ids=("finding-1",),
        agent_version="v1",
        prompt_version="v1",
        model_id="synthetic",
        output_latex_version_id="latex-2",
        output_compilation_record_id="compilation-2",
        output_visual_qa_result_id="visual-2",
        outcome=ResumeLayoutAttemptOutcome.REVISION_REQUIRED,
        detail="Visual QA still requires revision.",
    )
    run_content = {
        "run_id": f"resume-layout-revision-run-{HASH}",
        "contract_version": RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
        "run_binding": HASH,
        "subject_id": SUBJECT,
        "application_plan_id": "plan-1",
        "tailored_resume_draft_id": "draft-1",
        "tailored_resume_draft_hash": HASH,
        "initial_visual_qa_result_id": "visual-1",
        "initial_visual_qa_result_hash": HASH,
        "initial_latex_version_id": "latex-1",
        "initial_latex_source_sha256": HASH,
        "policy_version": RESUME_LAYOUT_REVISION_POLICY_VERSION,
        "max_attempts": 1,
        "attempts": (attempt,),
        "final_latex_version_id": "latex-2",
        "final_compilation_record_id": "compilation-2",
        "final_visual_qa_result_id": "visual-2",
        "final_status": ResumeLayoutRevisionStatus
        .DEFERRED_ATTEMPTS_EXHAUSTED,
    }
    run = ResumeLayoutRevisionRun(
        **run_content,
        run_content_hash=layout._canonical_hash(
            {
                **run_content,
                "attempts": [attempt.to_dict()],
                "final_status": (
                    ResumeLayoutRevisionStatus
                    .DEFERRED_ATTEMPTS_EXHAUSTED.value
                ),
            }
        ),
        started_at=NOW,
        completed_at=NOW,
    )
    exhausted = resume_layout_revision_public_result(
        ReviseResumeLayoutResult(
            status=ResumeLayoutRevisionStatus
            .DEFERRED_ATTEMPTS_EXHAUSTED,
            subject_id=SUBJECT,
            run_binding=HASH,
            run=run,
            write_result=ResumeLayoutRevisionWriteResult(
                status=ResumeLayoutRevisionWriteStatus.CREATED,
                run=run,
                reason_code=None,
                retryable=False,
            ),
            reason_code=ResumeLayoutRevisionFailureReason
            .ATTEMPTS_EXHAUSTED,
            retryable=False,
            message="Synthetic bounded exhaustion.",
        )
    )
    assert exhausted.stop_reason is not None
    assert (
        exhausted.stop_reason.code
        is ResumeLayoutRevisionStopReason.ATTEMPTS_EXHAUSTED
    )
    assert exhausted.result_id == run.run_id
    layout_replay = resume_layout_revision_public_result(
        ReviseResumeLayoutResult(
            status=ResumeLayoutRevisionStatus.UNCHANGED,
            subject_id=SUBJECT,
            run_binding=HASH,
            run=run,
            write_result=ResumeLayoutRevisionWriteResult(
                status=ResumeLayoutRevisionWriteStatus.UNCHANGED,
                run=run,
                reason_code=None,
                retryable=False,
            ),
            reason_code=None,
            retryable=False,
            message="Synthetic layout replay.",
        )
    )
    assert layout_replay.outcome is PreparationStageOutcome.DEFERRED
    assert layout_replay.stop_reason is not None
    assert (
        layout_replay.stop_reason.code
        is ResumeLayoutRevisionStopReason.ATTEMPTS_EXHAUSTED
    )
