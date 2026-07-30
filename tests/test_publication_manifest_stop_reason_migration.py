from __future__ import annotations

import inspect

import pytest

import core.plan_material_manifest as resume_manifest_module
import core.plan_material_manifest_cover_letter as cover_manifest_module
import core.prepared_cover_letter_material as cover_publication_module
import core.prepared_resume_material as resume_publication_module
from core.application_preparation_orchestrator import (
    COVER_LETTER_MANIFEST_ENTRY_STOP_REASON_CONTRACT_VERSION,
    PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    CoverLetterManifestEntryStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PreparedResumePublicationStopReason,
)
from core.plan_material_manifest import (
    AssemblePlanMaterialManifestResult,
    PlanMaterialManifestFailureReason,
    PlanMaterialManifestNotReadyReason,
    PlanMaterialManifestStatus,
    _RESUME_MANIFEST_FAILURE_REASON_MAP,
    _RESUME_MANIFEST_NOT_READY_REASON_MAP,
    resume_manifest_entry_public_result,
)
from core.plan_material_manifest_cover_letter import (
    IncludeCoverLetterInPlanMaterialManifestResult,
    _COVER_LETTER_MANIFEST_FAILURE_REASON_MAP,
    _COVER_LETTER_MANIFEST_NOT_READY_REASON_MAP,
    cover_letter_manifest_entry_public_result,
)
from core.prepared_cover_letter_material import (
    PreparedCoverLetterMaterialFailureReason,
    PreparedCoverLetterMaterialNotReadyReason,
    PreparedCoverLetterMaterialStatus,
    PublishPreparedCoverLetterResult,
    _COVER_LETTER_DEFERRED_PUBLICATION_STATUSES,
    _COVER_LETTER_PUBLICATION_FAILURE_REASON_MAP,
    _COVER_LETTER_PUBLICATION_NOT_READY_REASON_MAP,
    prepared_cover_letter_publication_public_result,
)
from core.prepared_resume_material import (
    PreparedResumeMaterialFailureReason,
    PreparedResumeMaterialNotReadyReason,
    PreparedResumeMaterialStatus,
    PublishPreparedResumeResult,
    _PREPARED_RESUME_FAILURE_REASON_MAP,
    _PREPARED_RESUME_NOT_READY_REASON_MAP,
    prepared_resume_publication_public_result,
)


def test_resume_publication_all_stops_are_typed() -> None:
    authoritative_not_ready = set(PreparedResumeMaterialNotReadyReason) - {
        PreparedResumeMaterialNotReadyReason.PLAN_BINDING_MISMATCH
    }
    assert set(_PREPARED_RESUME_NOT_READY_REASON_MAP) == (
        authoritative_not_ready
    )
    assert set(_PREPARED_RESUME_FAILURE_REASON_MAP) == set(
        PreparedResumeMaterialFailureReason
    )
    lineage_required = {
        PreparedResumeMaterialNotReadyReason.FACT_QA_NOT_PASSED,
        PreparedResumeMaterialNotReadyReason.VISUAL_QA_NOT_PASSED,
        PreparedResumeMaterialNotReadyReason.REVISION_RUN_NOT_SUCCESSFUL,
    }
    for reason in authoritative_not_ready - lineage_required:
        public = prepared_resume_publication_public_result(
            PublishPreparedResumeResult(
                status=PreparedResumeMaterialStatus.NOT_READY,
                subject_id="subject-1",
                application_plan_id="plan-1",
                material=None,
                write_result=None,
                reason_code=None,
                not_ready_reason=reason,
                retryable=False,
                message="Not ready.",
            )
        )
        assert public.outcome is PreparationStageOutcome.DEFERRED
        assert public.stop_reason is not None
        assert public.stop_reason.code.value == reason.value
    for reason in lineage_required:
        with pytest.raises(ValueError, match="lacks source lineage"):
            prepared_resume_publication_public_result(
                PublishPreparedResumeResult(
                    status=PreparedResumeMaterialStatus.NOT_READY,
                    subject_id="subject-1",
                    application_plan_id="plan-1",
                    material=None,
                    write_result=None,
                    reason_code=None,
                    not_ready_reason=reason,
                    retryable=False,
                    message="Not ready.",
                )
            )
    for reason in PreparedResumeMaterialFailureReason:
        public = prepared_resume_publication_public_result(
            PublishPreparedResumeResult(
                status=PreparedResumeMaterialStatus.FAILED,
                subject_id="subject-1",
                application_plan_id="plan-1",
                material=None,
                write_result=None,
                reason_code=reason,
                not_ready_reason=None,
                retryable=False,
                message="Failed.",
            )
        )
        assert public.outcome is PreparationStageOutcome.FAILED
        assert public.stop_reason is not None
        assert public.stop_reason.code.value == reason.value
    with pytest.raises(ValueError, match="unmapped"):
        prepared_resume_publication_public_result(
            PublishPreparedResumeResult(
                status=PreparedResumeMaterialStatus.NOT_READY,
                subject_id="subject-1",
                application_plan_id="plan-1",
                material=None,
                write_result=None,
                reason_code=None,
                not_ready_reason=(
                    PreparedResumeMaterialNotReadyReason
                    .PLAN_BINDING_MISMATCH
                ),
                retryable=False,
                message="Unused legacy enum member.",
            )
        )


def test_cover_letter_publication_preserves_defer_and_failure() -> None:
    assert set(_COVER_LETTER_PUBLICATION_NOT_READY_REASON_MAP) == set(
        PreparedCoverLetterMaterialNotReadyReason
    )
    assert set(_COVER_LETTER_PUBLICATION_FAILURE_REASON_MAP) == set(
        PreparedCoverLetterMaterialFailureReason
    )
    for reason in (
        set(PreparedCoverLetterMaterialNotReadyReason)
        - {PreparedCoverLetterMaterialNotReadyReason.FACT_QA_NOT_PASSED}
    ):
        public = prepared_cover_letter_publication_public_result(
            PublishPreparedCoverLetterResult(
                status=PreparedCoverLetterMaterialStatus.NOT_READY,
                subject_id="subject-1",
                application_plan_id="plan-1",
                material=None,
                write_result=None,
                reason_code=None,
                not_ready_reason=reason,
                compiler_started=False,
                retryable=False,
                message="Not ready.",
            )
        )
        assert public.outcome is PreparationStageOutcome.DEFERRED
    with pytest.raises(ValueError, match="lacks source lineage"):
        prepared_cover_letter_publication_public_result(
            PublishPreparedCoverLetterResult(
                status=PreparedCoverLetterMaterialStatus.NOT_READY,
                subject_id="subject-1",
                application_plan_id="plan-1",
                material=None,
                write_result=None,
                reason_code=None,
                not_ready_reason=(
                    PreparedCoverLetterMaterialNotReadyReason
                    .FACT_QA_NOT_PASSED
                ),
                compiler_started=False,
                retryable=False,
                message="Not ready.",
            )
        )
    deferred = {
        PreparedCoverLetterMaterialFailureReason.COMPILER_UNAVAILABLE: (
            PreparedCoverLetterMaterialStatus
            .DEFERRED_COMPILER_UNAVAILABLE
        ),
        PreparedCoverLetterMaterialFailureReason.COMPILATION_ERROR: (
            PreparedCoverLetterMaterialStatus.DEFERRED_COMPILATION_ERROR
        ),
        PreparedCoverLetterMaterialFailureReason.LAYOUT_OVERFLOW: (
            PreparedCoverLetterMaterialStatus.DEFERRED_LAYOUT_OVERFLOW
        ),
    }
    assert set(deferred.values()) == _COVER_LETTER_DEFERRED_PUBLICATION_STATUSES
    for reason in (
        set(PreparedCoverLetterMaterialFailureReason)
        - {PreparedCoverLetterMaterialFailureReason.LAYOUT_OVERFLOW}
    ):
        status = deferred.get(reason, PreparedCoverLetterMaterialStatus.FAILED)
        public = prepared_cover_letter_publication_public_result(
            PublishPreparedCoverLetterResult(
                status=status,
                subject_id="subject-1",
                application_plan_id="plan-1",
                material=None,
                write_result=None,
                reason_code=reason,
                not_ready_reason=None,
                compiler_started=True,
                retryable=False,
                message="Stopped.",
            )
        )
        expected = (
            PreparationStageOutcome.DEFERRED
            if reason in deferred
            else PreparationStageOutcome.FAILED
        )
        assert public.outcome is expected
        assert public.stop_reason is not None
        assert public.stop_reason.code.value == reason.value
    with pytest.raises(ValueError, match="lacks source lineage"):
        prepared_cover_letter_publication_public_result(
            PublishPreparedCoverLetterResult(
                status=(
                    PreparedCoverLetterMaterialStatus
                    .DEFERRED_LAYOUT_OVERFLOW
                ),
                subject_id="subject-1",
                application_plan_id="plan-1",
                material=None,
                write_result=None,
                reason_code=(
                    PreparedCoverLetterMaterialFailureReason.LAYOUT_OVERFLOW
                ),
                not_ready_reason=None,
                compiler_started=True,
                retryable=False,
                message="Stopped.",
            )
        )


def test_resume_and_cover_manifest_stops_are_stage_specific() -> None:
    resume_not_ready = {
        PlanMaterialManifestNotReadyReason.PREPARED_RESUME_NOT_PUBLISHED,
        PlanMaterialManifestNotReadyReason.PREPARED_RESUME_PLAN_MISMATCH,
        PlanMaterialManifestNotReadyReason.PREPARED_RESUME_ROLE_MISMATCH,
    }
    cover_not_ready = {
        PlanMaterialManifestNotReadyReason.PLAN_MATERIAL_MANIFEST_NOT_READY,
        PlanMaterialManifestNotReadyReason
        .PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE,
        PlanMaterialManifestNotReadyReason
        .PREPARED_COVER_LETTER_NOT_PUBLISHED,
        PlanMaterialManifestNotReadyReason
        .PREPARED_COVER_LETTER_PLAN_MISMATCH,
        PlanMaterialManifestNotReadyReason
        .PREPARED_COVER_LETTER_ROLE_MISMATCH,
    }
    assert set(_RESUME_MANIFEST_NOT_READY_REASON_MAP) == resume_not_ready
    assert set(_COVER_LETTER_MANIFEST_NOT_READY_REASON_MAP) == cover_not_ready
    assert {
        reason.value for reason in _RESUME_MANIFEST_FAILURE_REASON_MAP
    } == {
        reason.value
        for reason in PlanMaterialManifestFailureReason
        if "COVER_LETTER" not in reason.value
    }
    assert {
        reason.value for reason in _COVER_LETTER_MANIFEST_FAILURE_REASON_MAP
    } == {
        reason.value
        for reason in PlanMaterialManifestFailureReason
        if "PREPARED_RESUME" not in reason.value
    }
    resume_public = resume_manifest_entry_public_result(
        AssemblePlanMaterialManifestResult(
            status=PlanMaterialManifestStatus.NOT_READY,
            subject_id="subject-1",
            application_plan_id="plan-1",
            manifest=None,
            write_result=None,
            reason_code=None,
            not_ready_reason=(
                PlanMaterialManifestNotReadyReason
                .PREPARED_RESUME_NOT_PUBLISHED
            ),
            retryable=False,
            message="Not ready.",
        )
    )
    cover_public = cover_letter_manifest_entry_public_result(
        IncludeCoverLetterInPlanMaterialManifestResult(
            status=PlanMaterialManifestStatus.FAILED,
            subject_id="subject-1",
            application_plan_id="plan-1",
            manifest=None,
            write_result=None,
            reason_code=(
                PlanMaterialManifestFailureReason.ARTIFACT_HASH_DRIFT
            ),
            not_ready_reason=None,
            retryable=False,
            message="Failed.",
        )
    )
    assert resume_public.outcome is PreparationStageOutcome.DEFERRED
    assert cover_public.outcome is PreparationStageOutcome.FAILED
    assert type(resume_public.stop_reason.code).__name__ == (
        "ResumeManifestEntryStopReason"
    )
    assert type(cover_public.stop_reason.code).__name__ == (
        "CoverLetterManifestEntryStopReason"
    )


def test_closed_registry_and_target_modules_have_no_legacy_adapter() -> None:
    for module in (
        resume_publication_module,
        resume_manifest_module,
        cover_publication_module,
        cover_manifest_module,
    ):
        assert "legacy_stopped" not in inspect.getsource(module)
    with pytest.raises(TypeError):
        PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage.RESUME_PUBLICATION,
            code="PDF_INVALID",  # type: ignore[arg-type]
            contract_version=(
                PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION
            ),
            outcome=PreparationStageOutcome.FAILED,
        )
    with pytest.raises(ValueError):
        PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage.COVER_LETTER_MANIFEST,
            code=CoverLetterManifestEntryStopReason.ARTIFACT_HASH_DRIFT,
            contract_version=(
                COVER_LETTER_MANIFEST_ENTRY_STOP_REASON_CONTRACT_VERSION
            ),
            outcome=PreparationStageOutcome.DEFERRED,
        )
    with pytest.raises(ValueError):
        PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage.RESUME_PUBLICATION,
            code=PreparedResumePublicationStopReason.PDF_INVALID,
            contract_version="wrong-version",
            outcome=PreparationStageOutcome.FAILED,
        )
