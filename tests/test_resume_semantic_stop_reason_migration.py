from __future__ import annotations

from core.application_preparation_orchestrator import (
    BASE_RESUME_SELECTION_STOP_REASON_CONTRACT_VERSION,
    CANDIDATE_EVIDENCE_STOP_REASON_CONTRACT_VERSION,
    RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION,
    TAILORED_RESUME_DRAFT_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    BaseResumeSelectionStopReason,
    CandidateEvidenceSnapshotStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    ResumeFactQAStopReason,
    SourceResumeProjectionStopReason,
    TailoredResumeDraftStopReason,
)
from core.candidate_evidence import (
    CandidateEvidenceFailureReason,
    CandidateEvidenceSnapshotStatus,
    CreateCandidateEvidenceSnapshotResult,
    _CANDIDATE_EVIDENCE_FAILURE_REASON_MAP,
    candidate_evidence_snapshot_public_result,
)
from core.resume_fact_qa import (
    ResumeFactQAFailureReason,
    ResumeFactQAStatus,
    RunResumeFactQAResult,
    _RESUME_FACT_QA_FAILURE_REASON_MAP,
    resume_fact_qa_public_result,
)
from core.resume_selection import (
    ResumeSelectionFailureReason,
    ResumeSelectionStatus,
    SelectBaseResumeResult,
    _BASE_RESUME_FAILURE_REASON_MAP,
    base_resume_selection_public_result,
)
from core.resume_tailoring import (
    ResumeTailoringFailureReason,
    ResumeTailoringStatus,
    TailorResumeResult,
    _TAILORED_RESUME_FAILURE_REASON_MAP,
    tailored_resume_draft_public_result,
)
from core.source_resume_projection import (
    CreateSourceResumeProjectionResult,
    SourceResumeProjectionFailureReason,
    SourceResumeProjectionStatus,
    _SOURCE_PROJECTION_FAILURE_REASON_MAP,
    source_resume_projection_public_result,
)


def test_all_five_stage_failure_enums_have_closed_typed_mappings() -> None:
    mappings = (
        (ResumeSelectionFailureReason, _BASE_RESUME_FAILURE_REASON_MAP),
        (
            SourceResumeProjectionFailureReason,
            _SOURCE_PROJECTION_FAILURE_REASON_MAP,
        ),
        (CandidateEvidenceFailureReason, _CANDIDATE_EVIDENCE_FAILURE_REASON_MAP),
        (ResumeTailoringFailureReason, _TAILORED_RESUME_FAILURE_REASON_MAP),
        (ResumeFactQAFailureReason, _RESUME_FACT_QA_FAILURE_REASON_MAP),
    )
    for reason_type, mapping in mappings:
        assert set(mapping) == set(reason_type)
        assert all(type(value).__name__.endswith("StopReason") for value in mapping.values())


def test_missing_input_and_unsafe_output_defer_with_distinct_reasons() -> None:
    no_resume = base_resume_selection_public_result(
        SelectBaseResumeResult(
            status=ResumeSelectionStatus.DEFERRED_NO_RESUME,
            reason_code=None,
            retryable=False,
            subject_id="subject-1",
            application_plan_id="plan-1",
            job_id="job-1",
            candidate_set_hash="a" * 64,
            selection_binding="b" * 64,
            decision=None,
            write_result=None,
            message="No selectable resume.",
        )
    )
    unsafe_draft = tailored_resume_draft_public_result(
        TailorResumeResult(
            status=ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN,
            subject_id="subject-1",
            application_plan_id="plan-1",
            tailoring_binding="c" * 64,
            draft=None,
            write_result=None,
            reason_code=ResumeTailoringFailureReason.AGENT_OUTPUT_UNSAFE,
            retryable=False,
            message="Unsafe output.",
        )
    )
    assert no_resume.outcome is PreparationStageOutcome.DEFERRED
    assert no_resume.stop_reason.code is BaseResumeSelectionStopReason.NO_SELECTABLE_RESUME
    assert unsafe_draft.outcome is PreparationStageOutcome.DEFERRED
    assert unsafe_draft.stop_reason.code is TailoredResumeDraftStopReason.AGENT_OUTPUT_UNSAFE


def test_projection_and_evidence_defer_do_not_use_legacy_results() -> None:
    unsupported = source_resume_projection_public_result(
        CreateSourceResumeProjectionResult(
            status=SourceResumeProjectionStatus.UNSUPPORTED,
            subject_id="subject-1",
            resume_id="resume-1",
            projection=None,
            write_result=None,
            reason_code=SourceResumeProjectionFailureReason.FORMAT_UNSUPPORTED,
            retryable=False,
            message="Unsupported.",
        )
    )
    no_evidence = candidate_evidence_snapshot_public_result(
        CreateCandidateEvidenceSnapshotResult(
            status=CandidateEvidenceSnapshotStatus.DEFERRED_NO_EVIDENCE,
            subject_id="subject-1",
            application_plan_id="plan-1",
            snapshot=None,
            write_result=None,
            reason_code=None,
            retryable=False,
            message="No evidence.",
        )
    )
    assert unsupported.stop_reason.code is SourceResumeProjectionStopReason.FORMAT_UNSUPPORTED
    assert no_evidence.stop_reason.code is CandidateEvidenceSnapshotStopReason.NO_USABLE_EVIDENCE
    assert unsupported.legacy_reason_code is None
    assert no_evidence.legacy_reason_code is None


def test_fact_safety_block_and_integrity_failure_are_separate_contracts() -> None:
    unsupported = PreparationStopReasonEnvelope(
        stage=ApplicationPreparationStage.RESUME_FACT_QA,
        code=ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        contract_version=RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        outcome=PreparationStageOutcome.DEFERRED,
    )
    integrity_result = resume_fact_qa_public_result(
        RunResumeFactQAResult(
            status=ResumeFactQAStatus.FAILED,
            subject_id="subject-1",
            tailored_resume_draft_id="draft-1",
            qa_binding="",
            qa_result=None,
            write_result=None,
            reason_code=ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE,
            retryable=False,
            message="Integrity failure.",
        )
    )
    assert unsupported.code is ResumeFactQAStopReason.UNSUPPORTED_CLAIM
    assert unsupported.outcome is PreparationStageOutcome.DEFERRED
    assert integrity_result.stop_reason.code is ResumeFactQAStopReason.QA_RESULT_INTEGRITY_FAILURE
    assert integrity_result.outcome is PreparationStageOutcome.FAILED
    assert {
        BASE_RESUME_SELECTION_STOP_REASON_CONTRACT_VERSION,
        SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION,
        CANDIDATE_EVIDENCE_STOP_REASON_CONTRACT_VERSION,
        TAILORED_RESUME_DRAFT_STOP_REASON_CONTRACT_VERSION,
        RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    } == {
        "base-resume-selection-stop-reasons-v1",
        "source-resume-projection-stop-reasons-v1",
        "candidate-evidence-stop-reasons-v1",
        "tailored-resume-draft-stop-reasons-v1",
        "resume-fact-qa-stop-reasons-v1",
    }
