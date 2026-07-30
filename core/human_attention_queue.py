"""Subject-scoped read model for current preparation attention items."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .application_answer_taxonomy import CanonicalApplicationAnswerKey
from .application_answers import (
    PreparedApplicationAnswerSet,
    PreparedApplicationAnswerSetReadStatus,
    PreparedApplicationAnswerSetRepository,
    UnresolvedAnswerReason,
)
from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .application_preparation_orchestrator import (
    ApplicationAnswersStopReason,
    ApplicationPreparationRun,
    ApplicationPreparationRunListStatus,
    ApplicationPreparationRunReadStatus,
    ApplicationPreparationRunRepository,
    ApplicationPreparationRunStatus,
    ApplicationPreparationStage,
    ApplicationPreparationStageResult,
    BaseLatexPreparationStopReason,
    BaseResumeSelectionStopReason,
    CandidateEvidenceSnapshotStopReason,
    CoverLetterDraftStopReason,
    CoverLetterEvidenceStopReason,
    CoverLetterFactQAStopReason,
    CoverLetterManifestEntryStopReason,
    CoverLetterPublicationStopReason,
    DOWNSTREAM_PREPARATION_STOP_LINEAGE_CONTRACT_VERSION,
    DownstreamPreparationStopLineage,
    LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION,
    LatexCompilationStopReason,
    LatexConstructionStopReason,
    PreparedResumePublicationStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    ResumeFactQAStopReason,
    ResumeLayoutRevisionStopReason,
    ResumeManifestEntryStopReason,
    ResumeVisualQAStopReason,
    SourceResumeProjectionStopReason,
    TailoredResumeDraftStopReason,
    _STOP_REASON_CONTRACTS,
)
from .job_prioritization import ProposedPriorityLevel
from .material_correction_ref import MaterialCorrectionTargetRef
from .input_replacement_ref import InputReplacementTargetRef
from .fact_qa_findings import (
    FactQABlockingFinding,
    FactQABlockingFindingProvider,
    FactQABlockingFindingReadStatus,
    FactQAMaterialKind,
)
from .publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    PublicationStoppedSourceLineage,
)


HUMAN_ATTENTION_MAPPING_VERSION = "human-attention-mapping-v3"
HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION = "human-attention-queue-v5"
FACT_QA_FINDING_ATTENTION_REF_CONTRACT_VERSION = (
    "fact-qa-finding-attention-ref-v1"
)

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ITEM_ID_RE = re.compile(r"^human-attention-item-[a-f0-9]{64}$")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _clean_text(name: str, value: Any, maximum: int = 300) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _require_aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return (
        _require_aware("timestamp", value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class HumanAttentionQueueStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class HumanAttentionKind(StrEnum):
    USER_FACT_REQUIRED = "USER_FACT_REQUIRED"
    USER_CHOICE_REQUIRED = "USER_CHOICE_REQUIRED"
    USER_ATTESTATION_REQUIRED = "USER_ATTESTATION_REQUIRED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    MATERIAL_CORRECTION_REQUIRED = "MATERIAL_CORRECTION_REQUIRED"
    INPUT_REPLACEMENT_REQUIRED = "INPUT_REPLACEMENT_REQUIRED"
    SYSTEM_OPERATOR_REQUIRED = "SYSTEM_OPERATOR_REQUIRED"
    UNCLASSIFIED_SYSTEM_BLOCKER = "UNCLASSIFIED_SYSTEM_BLOCKER"


class HumanAttentionAudience(StrEnum):
    USER = "USER"
    OPERATOR = "OPERATOR"


class HumanAttentionResolutionCapability(StrEnum):
    PROVIDE_FACT = "PROVIDE_FACT"
    MAKE_CHOICE = "MAKE_CHOICE"
    ATTEST = "ATTEST"
    APPROVE_REVIEW_TARGET = "APPROVE_REVIEW_TARGET"
    CORRECT_MATERIAL = "CORRECT_MATERIAL"
    REPLACE_INPUT = "REPLACE_INPUT"
    OPERATOR_REPAIR = "OPERATOR_REPAIR"
    NON_OVERRIDABLE = "NON_OVERRIDABLE"


class HumanAttentionReasonCode(StrEnum):
    MISSING_TRUSTED_FACT = "MISSING_TRUSTED_FACT"
    AMBIGUOUS_USER_CHOICE = "AMBIGUOUS_USER_CHOICE"
    PERSONAL_ATTESTATION = "PERSONAL_ATTESTATION"
    MANUAL_PREPARATION_REVIEW = "MANUAL_PREPARATION_REVIEW"
    SYSTEM_DEPENDENCY_UNAVAILABLE = "SYSTEM_DEPENDENCY_UNAVAILABLE"
    SYSTEM_INTEGRITY_OR_CONTRACT_FAILURE = (
        "SYSTEM_INTEGRITY_OR_CONTRACT_FAILURE"
    )
    POLICY_REQUIRES_OPERATOR = "POLICY_REQUIRES_OPERATOR"
    UNSUPPORTED_REQUIRED_ANSWER = "UNSUPPORTED_REQUIRED_ANSWER"
    UNKNOWN_DEFER_REASON = "UNKNOWN_DEFER_REASON"
    MATERIAL_CORRECTION_REQUIRED = "MATERIAL_CORRECTION_REQUIRED"
    INPUT_REPLACEMENT_REQUIRED = "INPUT_REPLACEMENT_REQUIRED"
    SYSTEM_REPAIR_REQUIRED = "SYSTEM_REPAIR_REQUIRED"
    UNCLASSIFIED_SYSTEM_BLOCKER = "UNCLASSIFIED_SYSTEM_BLOCKER"


class HumanAttentionQueueFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    RUN_LIST_INTEGRITY_FAILURE = "RUN_LIST_INTEGRITY_FAILURE"
    CURRENT_RUN_INTEGRITY_FAILURE = "CURRENT_RUN_INTEGRITY_FAILURE"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_BINDING_MISMATCH = (
        "APPLICATION_PLAN_BINDING_MISMATCH"
    )
    ANSWER_SET_NOT_FOUND = "ANSWER_SET_NOT_FOUND"
    ANSWER_SET_INTEGRITY_FAILURE = "ANSWER_SET_INTEGRITY_FAILURE"
    ANSWER_SET_BINDING_MISMATCH = "ANSWER_SET_BINDING_MISMATCH"
    ATTENTION_MAPPING_FAILURE = "ATTENTION_MAPPING_FAILURE"


@dataclass(frozen=True, slots=True)
class _AttentionMapping:
    kind: HumanAttentionKind
    audience: HumanAttentionAudience
    resolution_capability: HumanAttentionResolutionCapability
    reason_code: HumanAttentionReasonCode
    required_action: str


_FACT = _AttentionMapping(
    HumanAttentionKind.USER_FACT_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionResolutionCapability.PROVIDE_FACT,
    HumanAttentionReasonCode.MISSING_TRUSTED_FACT,
    "Provide or verify the missing authoritative application fact.",
)
_CHOICE = _AttentionMapping(
    HumanAttentionKind.USER_CHOICE_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionResolutionCapability.MAKE_CHOICE,
    HumanAttentionReasonCode.AMBIGUOUS_USER_CHOICE,
    "Choose the valid option or resolve the ambiguous preparation input.",
)
_ATTESTATION = _AttentionMapping(
    HumanAttentionKind.USER_ATTESTATION_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionResolutionCapability.ATTEST,
    HumanAttentionReasonCode.PERSONAL_ATTESTATION,
    "Personally review and attest to the application statement.",
)
_CORRECT_MATERIAL = _AttentionMapping(
    HumanAttentionKind.MATERIAL_CORRECTION_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionResolutionCapability.CORRECT_MATERIAL,
    HumanAttentionReasonCode.MATERIAL_CORRECTION_REQUIRED,
    "The current material needs correction before preparation can continue.",
)
_REPLACE_INPUT = _AttentionMapping(
    HumanAttentionKind.INPUT_REPLACEMENT_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionResolutionCapability.REPLACE_INPUT,
    HumanAttentionReasonCode.INPUT_REPLACEMENT_REQUIRED,
    "Provide a supported, readable replacement input.",
)
_OPERATOR = _AttentionMapping(
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
    HumanAttentionAudience.OPERATOR,
    HumanAttentionResolutionCapability.OPERATOR_REPAIR,
    HumanAttentionReasonCode.SYSTEM_REPAIR_REQUIRED,
    "A system operator must repair the preparation dependency or binding.",
)
_CORRECT_LATEX = _AttentionMapping(
    HumanAttentionKind.MATERIAL_CORRECTION_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionResolutionCapability.CORRECT_MATERIAL,
    HumanAttentionReasonCode.MATERIAL_CORRECTION_REQUIRED,
    "Correct the generated LaTeX or layout before preparation continues.",
)
_REPLACE_LATEX_INPUT = _AttentionMapping(
    HumanAttentionKind.INPUT_REPLACEMENT_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionResolutionCapability.REPLACE_INPUT,
    HumanAttentionReasonCode.INPUT_REPLACEMENT_REQUIRED,
    "Choose or provide a readable LaTeX input before preparation continues.",
)
_OPERATOR_COMPILER = _AttentionMapping(
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
    HumanAttentionAudience.OPERATOR,
    HumanAttentionResolutionCapability.OPERATOR_REPAIR,
    HumanAttentionReasonCode.SYSTEM_REPAIR_REQUIRED,
    "A system operator must repair managed compilation or PDF output.",
)
_OPERATOR_RENDERER = _AttentionMapping(
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
    HumanAttentionAudience.OPERATOR,
    HumanAttentionResolutionCapability.OPERATOR_REPAIR,
    HumanAttentionReasonCode.SYSTEM_REPAIR_REQUIRED,
    "A system operator must repair rendering or Visual QA execution.",
)
_OPERATOR_LATEX_PIPELINE = _AttentionMapping(
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
    HumanAttentionAudience.OPERATOR,
    HumanAttentionResolutionCapability.OPERATOR_REPAIR,
    HumanAttentionReasonCode.SYSTEM_REPAIR_REQUIRED,
    "A system operator must repair the managed LaTeX preparation pipeline.",
)
_UNKNOWN = _AttentionMapping(
    HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER,
    HumanAttentionAudience.OPERATOR,
    HumanAttentionResolutionCapability.NON_OVERRIDABLE,
    HumanAttentionReasonCode.UNCLASSIFIED_SYSTEM_BLOCKER,
    "This blocker has no safe resolution path in the current contract.",
)


def _reason_entries(
    stage: ApplicationPreparationStage,
    reasons: tuple[StrEnum, ...],
    mapping: _AttentionMapping,
) -> dict[tuple[ApplicationPreparationStage, StrEnum], _AttentionMapping]:
    return {
        (stage, reason): mapping
        for reason in reasons
    }


_TYPED_DEFERRED_ATTENTION_MAPPINGS = {
    **_reason_entries(
        ApplicationPreparationStage.BASE_RESUME_SELECTION,
        (
            BaseResumeSelectionStopReason.NO_SELECTABLE_RESUME,
            BaseResumeSelectionStopReason.AGENT_SELECTION_UNSAFE,
        ),
        _CHOICE,
    ),
    **_reason_entries(
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
        (
            SourceResumeProjectionStopReason.FORMAT_UNSUPPORTED,
            SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE,
        ),
        _REPLACE_INPUT,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_EVIDENCE,
        (CandidateEvidenceSnapshotStopReason.NO_USABLE_EVIDENCE,),
        _FACT,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_TAILORING,
        (
            TailoredResumeDraftStopReason.INSUFFICIENT_EVIDENCE,
        ),
        _FACT,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_TAILORING,
        (TailoredResumeDraftStopReason.AGENT_OUTPUT_UNSAFE,),
        _OPERATOR,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_FACT_QA,
        (ResumeFactQAStopReason.UNSUPPORTED_CLAIM,),
        _CORRECT_MATERIAL,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_FACT_QA,
        (ResumeFactQAStopReason.AGENT_OUTPUT_UNRELIABLE,),
        _OPERATOR,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_EVIDENCE,
        (CoverLetterEvidenceStopReason.NO_USABLE_EVIDENCE,),
        _FACT,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_DRAFT,
        (CoverLetterDraftStopReason.INSUFFICIENT_EVIDENCE,),
        _FACT,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_DRAFT,
        (CoverLetterDraftStopReason.AGENT_OUTPUT_UNSAFE,),
        _OPERATOR,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_FACT_QA,
        (CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM,),
        _CORRECT_MATERIAL,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_FACT_QA,
        (CoverLetterFactQAStopReason.AGENT_OUTPUT_UNSAFE,),
        _OPERATOR,
    ),
    **_reason_entries(
        ApplicationPreparationStage.APPLICATION_ANSWERS,
        (
            ApplicationAnswersStopReason.NO_TRUSTED_FACTS,
            ApplicationAnswersStopReason.USER_FACT_REQUIRED,
            ApplicationAnswersStopReason.USER_FACT_AND_CHOICE_REQUIRED,
            ApplicationAnswersStopReason
            .USER_FACT_AND_ATTESTATION_REQUIRED,
            ApplicationAnswersStopReason
            .USER_FACT_CHOICE_AND_ATTESTATION_REQUIRED,
        ),
        _FACT,
    ),
    **_reason_entries(
        ApplicationPreparationStage.APPLICATION_ANSWERS,
        (
            ApplicationAnswersStopReason.USER_CHOICE_REQUIRED,
            ApplicationAnswersStopReason
            .USER_CHOICE_AND_ATTESTATION_REQUIRED,
        ),
        _CHOICE,
    ),
    **_reason_entries(
        ApplicationPreparationStage.APPLICATION_ANSWERS,
        (ApplicationAnswersStopReason.USER_ATTESTATION_REQUIRED,),
        _ATTESTATION,
    ),
    **_reason_entries(
        ApplicationPreparationStage.APPLICATION_ANSWERS,
        (ApplicationAnswersStopReason.NO_SAFE_AUTOMATABLE_ANSWER,),
        _UNKNOWN,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_PUBLICATION,
        (
            PreparedResumePublicationStopReason.VISUAL_QA_NOT_PASSED,
            PreparedResumePublicationStopReason
            .REVISION_RUN_NOT_SUCCESSFUL,
            PreparedResumePublicationStopReason.FACT_QA_NOT_PASSED,
        ),
        _CORRECT_MATERIAL,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_PUBLICATION,
        (
            PreparedResumePublicationStopReason.DRAFT_BINDING_MISMATCH,
            PreparedResumePublicationStopReason
            .FACT_QA_BINDING_MISMATCH,
            PreparedResumePublicationStopReason
            .LATEX_VERSION_BINDING_MISMATCH,
            PreparedResumePublicationStopReason
            .COMPILATION_BINDING_MISMATCH,
            PreparedResumePublicationStopReason
            .REVISION_BINDING_MISMATCH,
        ),
        _OPERATOR,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_MANIFEST,
        (
            ResumeManifestEntryStopReason.PREPARED_RESUME_NOT_PUBLISHED,
            ResumeManifestEntryStopReason.PREPARED_RESUME_PLAN_MISMATCH,
            ResumeManifestEntryStopReason.PREPARED_RESUME_ROLE_MISMATCH,
        ),
        _OPERATOR,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        (CoverLetterPublicationStopReason.FACT_QA_NOT_PASSED,),
        _CORRECT_MATERIAL,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        (
            CoverLetterPublicationStopReason.JOB_BINDING_MISMATCH,
            CoverLetterPublicationStopReason.DRAFT_BINDING_MISMATCH,
            CoverLetterPublicationStopReason.FACT_QA_BINDING_MISMATCH,
            CoverLetterPublicationStopReason.COMPILER_UNAVAILABLE,
            CoverLetterPublicationStopReason.COMPILATION_ERROR,
        ),
        _OPERATOR,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        (CoverLetterPublicationStopReason.LAYOUT_OVERFLOW,),
        _CORRECT_MATERIAL,
    ),
    **_reason_entries(
        ApplicationPreparationStage.COVER_LETTER_MANIFEST,
        (
            CoverLetterManifestEntryStopReason
            .PLAN_MATERIAL_MANIFEST_NOT_READY,
            CoverLetterManifestEntryStopReason
            .PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE,
            CoverLetterManifestEntryStopReason
            .PREPARED_COVER_LETTER_NOT_PUBLISHED,
            CoverLetterManifestEntryStopReason
            .PREPARED_COVER_LETTER_PLAN_MISMATCH,
            CoverLetterManifestEntryStopReason
            .PREPARED_COVER_LETTER_ROLE_MISMATCH,
        ),
        _OPERATOR,
    ),
    **_reason_entries(
        ApplicationPreparationStage.BASE_LATEX_SELECTION,
        (
            BaseLatexPreparationStopReason
            .USER_REQUIREMENT_UNSATISFIABLE,
        ),
        _CHOICE,
    ),
    **_reason_entries(
        ApplicationPreparationStage.LATEX_CONSTRUCTION,
        (LatexConstructionStopReason.BASE_VERSION_UNREADABLE,),
        _REPLACE_LATEX_INPUT,
    ),
    **_reason_entries(
        ApplicationPreparationStage.LATEX_CONSTRUCTION,
        (LatexConstructionStopReason.CONSTRUCTION_OUTPUT_UNSAFE,),
        _OPERATOR_LATEX_PIPELINE,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_COMPILATION,
        (
            LatexCompilationStopReason.UNMANAGED_DEPENDENCY,
            LatexCompilationStopReason.COMPILATION_ERROR,
        ),
        _CORRECT_LATEX,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_COMPILATION,
        (
            LatexCompilationStopReason.COMPILER_UNAVAILABLE,
            LatexCompilationStopReason.COMPILATION_TIMEOUT,
            LatexCompilationStopReason.PDF_INVALID,
        ),
        _OPERATOR_COMPILER,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_VISUAL_QA,
        (
            ResumeVisualQAStopReason.RENDERER_UNAVAILABLE,
            ResumeVisualQAStopReason.AGENT_OUTPUT_UNRELIABLE,
        ),
        _OPERATOR_RENDERER,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
        (
            ResumeLayoutRevisionStopReason.RENDERER_UNAVAILABLE,
            ResumeLayoutRevisionStopReason.VISUAL_QA_DEFERRED,
            ResumeLayoutRevisionStopReason.VISUAL_QA_FAILED,
        ),
        _OPERATOR_RENDERER,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
        (
            ResumeLayoutRevisionStopReason.REVISION_OUTPUT_UNSAFE,
            ResumeLayoutRevisionStopReason.VERSION_REGISTRATION_FAILED,
        ),
        _OPERATOR_LATEX_PIPELINE,
    ),
    **_reason_entries(
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
        (ResumeLayoutRevisionStopReason.ATTEMPTS_EXHAUSTED,),
        _CORRECT_LATEX,
    ),
}

_LINEAGE_CLASSIFIED_DEFERRED_REASONS = frozenset(
    {
        (
            ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
            ResumeLayoutRevisionStopReason.COMPILATION_STOPPED,
        )
    }
)

_REGISTERED_TYPED_DEFERRED_REASONS = frozenset(
    (stage, reason)
    for stage, (_version, _reason_type, outcomes) in (
        _STOP_REASON_CONTRACTS.items()
    )
    for reason, outcome in outcomes.items()
    if outcome is PreparationStageOutcome.DEFERRED
)


def _layout_compilation_stop_mapping(
    stage_result: ApplicationPreparationStageResult,
    *,
    subject_id: str,
    application_plan_id: str,
) -> _AttentionMapping:
    outputs = {item.key: item.value for item in stage_result.outputs}
    required = {
        "downstream_application_plan_id",
        "downstream_child_outcome",
        "downstream_child_reason_code",
        "downstream_child_reason_contract_version",
        "downstream_child_stage",
        "downstream_child_stage_result_hash",
        "downstream_child_stage_result_id",
        "downstream_lineage_contract_version",
        "downstream_lineage_id",
        "downstream_parent_attempt_id",
        "downstream_parent_stage",
        "downstream_subject_id",
    }
    if not required.issubset(outputs):
        return _UNKNOWN
    try:
        stop_reason = PreparationStopReasonEnvelope(
            stage=ApplicationPreparationStage(
                outputs["downstream_child_stage"]
            ),
            code=LatexCompilationStopReason(
                outputs["downstream_child_reason_code"]
            ),
            contract_version=(
                outputs["downstream_child_reason_contract_version"]
            ),
            outcome=PreparationStageOutcome(
                outputs["downstream_child_outcome"]
            ),
            diagnostic_code=outputs.get(
                "downstream_child_reason_diagnostic_code"
            ),
            upstream_lineage_id=outputs.get(
                "downstream_child_reason_upstream_lineage_id"
            ),
        )
        lineage = DownstreamPreparationStopLineage(
            lineage_id=outputs["downstream_lineage_id"],
            contract_version=outputs[
                "downstream_lineage_contract_version"
            ],
            parent_stage=ApplicationPreparationStage(
                outputs["downstream_parent_stage"]
            ),
            parent_attempt_id=outputs["downstream_parent_attempt_id"],
            subject_id=outputs["downstream_subject_id"],
            application_plan_id=outputs[
                "downstream_application_plan_id"
            ],
            child_stage=ApplicationPreparationStage(
                outputs["downstream_child_stage"]
            ),
            child_stage_result_id=outputs[
                "downstream_child_stage_result_id"
            ],
            child_stage_result_hash=outputs[
                "downstream_child_stage_result_hash"
            ],
            child_outcome=PreparationStageOutcome(
                outputs["downstream_child_outcome"]
            ),
            child_stop_reason=stop_reason,
            child_result_lineage_id=outputs.get(
                "downstream_child_result_lineage_id"
            ),
        )
    except (KeyError, TypeError, ValueError):
        return _UNKNOWN
    if (
        lineage.contract_version
        != DOWNSTREAM_PREPARATION_STOP_LINEAGE_CONTRACT_VERSION
        or lineage.child_stop_reason.contract_version
        != LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION
        or lineage.subject_id != subject_id
        or lineage.application_plan_id != application_plan_id
    ):
        return _UNKNOWN
    if lineage.child_stop_reason.code in {
        LatexCompilationStopReason.UNMANAGED_DEPENDENCY,
        LatexCompilationStopReason.COMPILATION_ERROR,
    }:
        return _CORRECT_LATEX
    return _OPERATOR_COMPILER


def _typed_deferred_mapping(
    stage_result: ApplicationPreparationStageResult,
    *,
    subject_id: str,
    application_plan_id: str,
) -> _AttentionMapping:
    stop_reason = stage_result.stop_reason
    if stop_reason is None:
        return _UNKNOWN
    key = (stage_result.stage, stop_reason.code)
    if key in _LINEAGE_CLASSIFIED_DEFERRED_REASONS:
        return _layout_compilation_stop_mapping(
            stage_result,
            subject_id=subject_id,
            application_plan_id=application_plan_id,
        )
    return _TYPED_DEFERRED_ATTENTION_MAPPINGS.get(key, _UNKNOWN)


@dataclass(frozen=True, slots=True)
class FactQAFindingAttentionRef:
    subject_id: str
    application_plan_id: str
    material_kind: FactQAMaterialKind
    attention_origin_stage: ApplicationPreparationStage
    attention_origin_stage_result_id: str
    fact_qa_stage: ApplicationPreparationStage
    qa_result_id: str
    qa_result_content_hash: str
    qa_contract_version: str
    finding_id: str
    finding_kind: str
    finding_order: int
    source_material_id: str
    source_material_content_hash: str
    contract_version: str = (
        FACT_QA_FINDING_ATTENTION_REF_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if self.contract_version != (
            FACT_QA_FINDING_ATTENTION_REF_CONTRACT_VERSION
        ):
            raise ValueError("finding-attention reference is unsupported")
        material = FactQAMaterialKind(self.material_kind)
        origin = ApplicationPreparationStage(self.attention_origin_stage)
        qa_stage = ApplicationPreparationStage(self.fact_qa_stage)
        object.__setattr__(self, "material_kind", material)
        object.__setattr__(self, "attention_origin_stage", origin)
        object.__setattr__(self, "fact_qa_stage", qa_stage)
        expected_qa_stage = (
            ApplicationPreparationStage.RESUME_FACT_QA
            if material is FactQAMaterialKind.RESUME
            else ApplicationPreparationStage.COVER_LETTER_FACT_QA
        )
        expected_origins = {
            expected_qa_stage,
            (
                ApplicationPreparationStage.RESUME_PUBLICATION
                if material is FactQAMaterialKind.RESUME
                else ApplicationPreparationStage.COVER_LETTER_PUBLICATION
            ),
        }
        if qa_stage is not expected_qa_stage or origin not in expected_origins:
            raise ValueError("finding reference stage or material is invalid")
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            (
                "attention_origin_stage_result_id",
                self.attention_origin_stage_result_id,
                240,
            ),
            ("qa_result_id", self.qa_result_id, 240),
            ("qa_contract_version", self.qa_contract_version, 100),
            ("finding_id", self.finding_id, 240),
            ("finding_kind", self.finding_kind, 100),
            ("source_material_id", self.source_material_id, 240),
        ):
            _clean_text(name, value, maximum)
        _require_hash(
            "qa_result_content_hash", self.qa_result_content_hash
        )
        _require_hash(
            "source_material_content_hash",
            self.source_material_content_hash,
        )
        if type(self.finding_order) is not int or self.finding_order < 0:
            raise ValueError("finding order must be non-negative")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "attention_origin_stage": self.attention_origin_stage.value,
            "attention_origin_stage_result_id": (
                self.attention_origin_stage_result_id
            ),
            "contract_version": self.contract_version,
            "fact_qa_stage": self.fact_qa_stage.value,
            "finding_id": self.finding_id,
            "finding_kind": self.finding_kind,
            "material_kind": self.material_kind.value,
            "qa_contract_version": self.qa_contract_version,
            "qa_result_content_hash": self.qa_result_content_hash,
            "qa_result_id": self.qa_result_id,
            "source_material_content_hash": (
                self.source_material_content_hash
            ),
            "source_material_id": self.source_material_id,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "finding_order": self.finding_order}

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "FactQAFindingAttentionRef":
        expected = {
            "application_plan_id",
            "attention_origin_stage",
            "attention_origin_stage_result_id",
            "contract_version",
            "fact_qa_stage",
            "finding_id",
            "finding_kind",
            "finding_order",
            "material_kind",
            "qa_contract_version",
            "qa_result_content_hash",
            "qa_result_id",
            "source_material_content_hash",
            "source_material_id",
            "subject_id",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("finding-attention reference is invalid")
        return cls(
            subject_id=value["subject_id"],
            application_plan_id=value["application_plan_id"],
            material_kind=value["material_kind"],
            attention_origin_stage=value["attention_origin_stage"],
            attention_origin_stage_result_id=value[
                "attention_origin_stage_result_id"
            ],
            fact_qa_stage=value["fact_qa_stage"],
            qa_result_id=value["qa_result_id"],
            qa_result_content_hash=value["qa_result_content_hash"],
            qa_contract_version=value["qa_contract_version"],
            finding_id=value["finding_id"],
            finding_kind=value["finding_kind"],
            finding_order=value["finding_order"],
            source_material_id=value["source_material_id"],
            source_material_content_hash=value[
                "source_material_content_hash"
            ],
            contract_version=value["contract_version"],
        )


@dataclass(frozen=True, slots=True)
class HumanAttentionQueueItem:
    item_id: str
    contract_version: str
    mapping_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    priority: ProposedPriorityLevel
    source_preparation_run_id: str
    source_preparation_binding: str
    source_stage: ApplicationPreparationStage
    attention_kind: HumanAttentionKind
    audience: HumanAttentionAudience
    resolution_capability: HumanAttentionResolutionCapability
    reason_code: HumanAttentionReasonCode
    source_reason_code: str
    canonical_answer_key: CanonicalApplicationAnswerKey | None
    blocking: bool
    required_action: str
    source_record_id: str
    source_event_time: datetime
    answer_set_id: str | None
    answer_set_content_hash: str | None
    fact_qa_finding_ref: FactQAFindingAttentionRef | None
    correction_target_ref: MaterialCorrectionTargetRef | None
    item_content_hash: str
    replacement_target_ref: InputReplacementTargetRef | None = None

    def __post_init__(self) -> None:
        if self.contract_version != HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION:
            raise ValueError("attention queue contract is unsupported")
        if self.mapping_version != HUMAN_ATTENTION_MAPPING_VERSION:
            raise ValueError("attention mapping version is unsupported")
        _clean_text("subject_id", self.subject_id, 160)
        _clean_text("application_plan_id", self.application_plan_id, 180)
        _clean_text("job_id", self.job_id, 160)
        object.__setattr__(
            self, "priority", ProposedPriorityLevel(self.priority)
        )
        _clean_text(
            "source_preparation_run_id",
            self.source_preparation_run_id,
            200,
        )
        _require_hash(
            "source_preparation_binding",
            self.source_preparation_binding,
        )
        object.__setattr__(
            self,
            "source_stage",
            ApplicationPreparationStage(self.source_stage),
        )
        kind = HumanAttentionKind(self.attention_kind)
        audience = HumanAttentionAudience(self.audience)
        capability = HumanAttentionResolutionCapability(
            self.resolution_capability
        )
        reason = HumanAttentionReasonCode(self.reason_code)
        object.__setattr__(self, "attention_kind", kind)
        object.__setattr__(self, "audience", audience)
        object.__setattr__(self, "resolution_capability", capability)
        object.__setattr__(self, "reason_code", reason)
        operator_kinds = {
            HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
            HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER,
        }
        if (audience is HumanAttentionAudience.OPERATOR) != (
            kind in operator_kinds
        ):
            raise ValueError("attention kind and audience conflict")
        if (
            capability
            is HumanAttentionResolutionCapability.APPROVE_REVIEW_TARGET
            or kind is HumanAttentionKind.MANUAL_REVIEW_REQUIRED
        ):
            raise ValueError(
                "no current preparation stage has an approvable review target"
            )
        _clean_text("source_reason_code", self.source_reason_code, 200)
        if self.canonical_answer_key is not None:
            object.__setattr__(
                self,
                "canonical_answer_key",
                CanonicalApplicationAnswerKey(
                    self.canonical_answer_key
                ),
            )
        if self.blocking is not True:
            raise ValueError("current attention items must be blocking")
        _clean_text("required_action", self.required_action, 300)
        _clean_text("source_record_id", self.source_record_id, 240)
        _require_aware("source_event_time", self.source_event_time)
        if (self.answer_set_id is None) != (
            self.answer_set_content_hash is None
        ):
            raise ValueError("answer-set item binding is incomplete")
        if self.answer_set_id is not None:
            _clean_text("answer_set_id", self.answer_set_id, 200)
            _require_hash(
                "answer_set_content_hash",
                self.answer_set_content_hash,
            )
        if self.fact_qa_finding_ref is not None:
            reference = self.fact_qa_finding_ref
            if (
                not isinstance(reference, FactQAFindingAttentionRef)
                or capability
                is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
                or kind
                is not HumanAttentionKind.MATERIAL_CORRECTION_REQUIRED
                or audience is not HumanAttentionAudience.USER
                or reference.subject_id != self.subject_id
                or reference.application_plan_id
                != self.application_plan_id
                or reference.attention_origin_stage is not self.source_stage
                or reference.attention_origin_stage_result_id
                != self.source_record_id
            ):
                raise ValueError(
                    "Fact QA finding reference does not match attention item"
                )
        if self.correction_target_ref is not None and (
            not isinstance(
                self.correction_target_ref, MaterialCorrectionTargetRef
            )
            or capability
            is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
            or kind
            is not HumanAttentionKind.MATERIAL_CORRECTION_REQUIRED
            or audience is not HumanAttentionAudience.USER
        ):
            raise ValueError(
                "correction target reference does not match attention item"
            )
        if self.replacement_target_ref is not None and (
            not isinstance(
                self.replacement_target_ref, InputReplacementTargetRef
            )
            or capability
            is not HumanAttentionResolutionCapability.REPLACE_INPUT
            or kind
            is not HumanAttentionKind.INPUT_REPLACEMENT_REQUIRED
            or audience is not HumanAttentionAudience.USER
        ):
            raise ValueError(
                "replacement target reference does not match attention item"
            )
        expected_id = "human-attention-item-" + _canonical_hash(
            self.identity_dict()
        )
        if (
            _ITEM_ID_RE.fullmatch(self.item_id) is None
            or self.item_id != expected_id
        ):
            raise ValueError("attention item ID is invalid")
        if self.item_content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("attention item content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "answer_set_content_hash": self.answer_set_content_hash,
            "answer_set_id": self.answer_set_id,
            "application_plan_id": self.application_plan_id,
            "attention_kind": self.attention_kind.value,
            "canonical_answer_key": (
                self.canonical_answer_key.value
                if self.canonical_answer_key
                else None
            ),
            "contract_version": self.contract_version,
            "correction_target_ref": None,
            "replacement_target_ref": None,
            "fact_qa_finding_ref": (
                self.fact_qa_finding_ref.identity_dict()
                if self.fact_qa_finding_ref is not None
                else None
            ),
            "mapping_version": self.mapping_version,
            "reason_code": self.reason_code.value,
            "resolution_capability": self.resolution_capability.value,
            "source_preparation_binding": (
                self.source_preparation_binding
            ),
            "source_preparation_run_id": self.source_preparation_run_id,
            "source_record_id": self.source_record_id,
            "source_reason_code": self.source_reason_code,
            "source_stage": self.source_stage.value,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            "answer_set_content_hash": self.answer_set_content_hash,
            "answer_set_id": self.answer_set_id,
            "application_plan_id": self.application_plan_id,
            "attention_kind": self.attention_kind.value,
            "audience": self.audience.value,
            "blocking": self.blocking,
            "canonical_answer_key": (
                self.canonical_answer_key.value
                if self.canonical_answer_key
                else None
            ),
            "contract_version": self.contract_version,
            "correction_target_ref": (
                self.correction_target_ref.to_dict()
                if self.correction_target_ref is not None
                else None
            ),
            "replacement_target_ref": (
                self.replacement_target_ref.to_dict()
                if self.replacement_target_ref is not None
                else None
            ),
            "fact_qa_finding_ref": (
                self.fact_qa_finding_ref.to_dict()
                if self.fact_qa_finding_ref is not None
                else None
            ),
            "item_id": self.item_id,
            "job_id": self.job_id,
            "mapping_version": self.mapping_version,
            "priority": self.priority.value,
            "reason_code": self.reason_code.value,
            "resolution_capability": self.resolution_capability.value,
            "required_action": self.required_action,
            "source_event_time": _rfc3339(self.source_event_time),
            "source_preparation_binding": (
                self.source_preparation_binding
            ),
            "source_preparation_run_id": self.source_preparation_run_id,
            "source_reason_code": self.source_reason_code,
            "source_record_id": self.source_record_id,
            "source_stage": self.source_stage.value,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "item_content_hash": self.item_content_hash,
        }


@dataclass(frozen=True, slots=True)
class HumanAttentionQueueResult:
    status: HumanAttentionQueueStatus
    subject_id: str
    items: tuple[HumanAttentionQueueItem, ...]
    item_count: int
    user_item_count: int
    operator_item_count: int
    affected_plan_count: int
    queue_snapshot_hash: str | None
    evaluated_at: datetime
    reason_code: HumanAttentionQueueFailureReason | None
    message: str

    def __post_init__(self) -> None:
        status = HumanAttentionQueueStatus(self.status)
        object.__setattr__(self, "status", status)
        _clean_text("subject_id", self.subject_id, 160)
        _require_aware("evaluated_at", self.evaluated_at)
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, HumanAttentionQueueItem)
            for item in self.items
        ):
            raise TypeError("queue items must be typed")
        if (
            tuple(sorted(self.items, key=_item_sort_key)) != self.items
            or len({item.item_id for item in self.items})
            != len(self.items)
            or any(item.subject_id != self.subject_id for item in self.items)
        ):
            raise ValueError("queue item ordering or ownership is invalid")
        counts = (
            len(self.items),
            sum(
                item.audience is HumanAttentionAudience.USER
                for item in self.items
            ),
            sum(
                item.audience is HumanAttentionAudience.OPERATOR
                for item in self.items
            ),
            len({item.application_plan_id for item in self.items}),
        )
        if counts != (
            self.item_count,
            self.user_item_count,
            self.operator_item_count,
            self.affected_plan_count,
        ):
            raise ValueError("queue counts are invalid")
        if status is HumanAttentionQueueStatus.SUCCEEDED:
            if self.reason_code is not None:
                raise ValueError("successful queue cannot have a reason")
            _require_hash("queue_snapshot_hash", self.queue_snapshot_hash)
            expected_snapshot = _canonical_hash(
                {
                    "affected_plan_count": self.affected_plan_count,
                    "contract_version": (
                        HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION
                    ),
                    "item_count": self.item_count,
                    "item_hashes": [
                        item.item_content_hash for item in self.items
                    ],
                    "mapping_version": HUMAN_ATTENTION_MAPPING_VERSION,
                    "operator_item_count": self.operator_item_count,
                    "subject_id": self.subject_id,
                    "user_item_count": self.user_item_count,
                }
            )
            if self.queue_snapshot_hash != expected_snapshot:
                raise ValueError("queue snapshot hash is invalid")
        elif (
            self.reason_code is None
            or self.items
            or self.queue_snapshot_hash is not None
        ):
            raise ValueError("failed queue result is invalid")
        _clean_text("message", self.message, 300)


_PRIORITY_ORDER = {
    ProposedPriorityLevel.P0: 0,
    ProposedPriorityLevel.P1: 1,
    ProposedPriorityLevel.P2: 2,
    ProposedPriorityLevel.P3: 3,
}
_AUDIENCE_ORDER = {
    HumanAttentionAudience.USER: 0,
    HumanAttentionAudience.OPERATOR: 1,
}
_KIND_ORDER = {
    HumanAttentionKind.USER_ATTESTATION_REQUIRED: 0,
    HumanAttentionKind.USER_FACT_REQUIRED: 1,
    HumanAttentionKind.USER_CHOICE_REQUIRED: 2,
    HumanAttentionKind.MANUAL_REVIEW_REQUIRED: 3,
    HumanAttentionKind.MATERIAL_CORRECTION_REQUIRED: 4,
    HumanAttentionKind.INPUT_REPLACEMENT_REQUIRED: 5,
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED: 6,
    HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER: 7,
}


def _item_sort_key(item: HumanAttentionQueueItem) -> tuple[Any, ...]:
    return (
        _PRIORITY_ORDER[item.priority],
        _AUDIENCE_ORDER[item.audience],
        _KIND_ORDER[item.attention_kind],
        item.source_event_time.astimezone(timezone.utc),
        item.application_plan_id,
        (
            item.fact_qa_finding_ref.finding_order
            if item.fact_qa_finding_ref is not None
            else -1
        ),
        item.item_id,
    )


def _build_item(
    *,
    plan: ApplicationPlan,
    run: ApplicationPreparationRun,
    stage: ApplicationPreparationStage,
    mapping: _AttentionMapping,
    source_reason_code: str,
    required_action: str,
    source_record_id: str,
    source_event_time: datetime,
    canonical_answer_key: CanonicalApplicationAnswerKey | None = None,
    answer_set: PreparedApplicationAnswerSet | None = None,
    fact_qa_finding_ref: FactQAFindingAttentionRef | None = None,
) -> HumanAttentionQueueItem:
    identity = {
        "answer_set_content_hash": (
            answer_set.answer_set_content_hash if answer_set else None
        ),
        "answer_set_id": answer_set.answer_set_id if answer_set else None,
        "application_plan_id": plan.plan_id,
        "attention_kind": mapping.kind.value,
        "canonical_answer_key": (
            canonical_answer_key.value if canonical_answer_key else None
        ),
        "contract_version": HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION,
        "correction_target_ref": None,
        "replacement_target_ref": None,
        "fact_qa_finding_ref": (
            fact_qa_finding_ref.identity_dict()
            if fact_qa_finding_ref is not None
            else None
        ),
        "mapping_version": HUMAN_ATTENTION_MAPPING_VERSION,
        "reason_code": mapping.reason_code.value,
        "resolution_capability": mapping.resolution_capability.value,
        "source_preparation_binding": run.preparation_binding,
        "source_preparation_run_id": run.run_id,
        "source_record_id": source_record_id,
        "source_reason_code": source_reason_code,
        "source_stage": stage.value,
    }
    item_id = "human-attention-item-" + _canonical_hash(identity)
    content = {
        "answer_set_content_hash": (
            answer_set.answer_set_content_hash if answer_set else None
        ),
        "answer_set_id": answer_set.answer_set_id if answer_set else None,
        "application_plan_id": plan.plan_id,
        "attention_kind": mapping.kind.value,
        "audience": mapping.audience.value,
        "blocking": True,
        "canonical_answer_key": (
            canonical_answer_key.value if canonical_answer_key else None
        ),
        "contract_version": HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION,
        "correction_target_ref": None,
        "replacement_target_ref": None,
        "fact_qa_finding_ref": (
            fact_qa_finding_ref.to_dict()
            if fact_qa_finding_ref is not None
            else None
        ),
        "item_id": item_id,
        "job_id": plan.job_id,
        "mapping_version": HUMAN_ATTENTION_MAPPING_VERSION,
        "priority": plan.priority_level.value,
        "reason_code": mapping.reason_code.value,
        "resolution_capability": mapping.resolution_capability.value,
        "required_action": required_action,
        "source_event_time": _rfc3339(source_event_time),
        "source_preparation_binding": run.preparation_binding,
        "source_preparation_run_id": run.run_id,
        "source_reason_code": source_reason_code,
        "source_record_id": source_record_id,
        "source_stage": stage.value,
        "subject_id": plan.subject_id,
    }
    return HumanAttentionQueueItem(
        item_id=item_id,
        contract_version=HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION,
        mapping_version=HUMAN_ATTENTION_MAPPING_VERSION,
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        priority=plan.priority_level,
        source_preparation_run_id=run.run_id,
        source_preparation_binding=run.preparation_binding,
        source_stage=stage,
        attention_kind=mapping.kind,
        audience=mapping.audience,
        resolution_capability=mapping.resolution_capability,
        reason_code=mapping.reason_code,
        source_reason_code=source_reason_code,
        canonical_answer_key=canonical_answer_key,
        blocking=True,
        required_action=required_action,
        source_record_id=source_record_id,
        source_event_time=source_event_time,
        answer_set_id=answer_set.answer_set_id if answer_set else None,
        answer_set_content_hash=(
            answer_set.answer_set_content_hash if answer_set else None
        ),
        fact_qa_finding_ref=fact_qa_finding_ref,
        correction_target_ref=None,
        item_content_hash=_canonical_hash(content),
        replacement_target_ref=None,
    )


class MaterialCorrectionTargetProjector:
    """Structural boundary used by P2b5 without importing target internals."""

    def get_current_ref(
        self,
        *,
        item: HumanAttentionQueueItem,
        run: ApplicationPreparationRun,
        stage_result: ApplicationPreparationStageResult,
        now: datetime,
    ) -> MaterialCorrectionTargetRef | None: ...


class InputReplacementTargetProjector:
    """Structural boundary used by P2b5 without importing target internals."""

    def get_current_ref(
        self,
        *,
        item: HumanAttentionQueueItem,
        run: ApplicationPreparationRun,
        stage_result: ApplicationPreparationStageResult,
        now: datetime,
    ) -> InputReplacementTargetRef | None: ...


def _attach_correction_target(
    item: HumanAttentionQueueItem,
    *,
    run: ApplicationPreparationRun,
    stage_result: ApplicationPreparationStageResult,
    now: datetime,
    projector: MaterialCorrectionTargetProjector | None,
) -> HumanAttentionQueueItem:
    if (
        item.resolution_capability
        is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
        or projector is None
    ):
        return item
    reference = projector.get_current_ref(
        item=item,
        run=run,
        stage_result=stage_result,
        now=now,
    )
    if reference is None:
        return item
    if not isinstance(reference, MaterialCorrectionTargetRef):
        raise ValueError("correction target projection is invalid")
    content = item.content_dict()
    content["correction_target_ref"] = reference.to_dict()
    return replace(
        item,
        correction_target_ref=reference,
        item_content_hash=_canonical_hash(content),
    )


def _attach_replacement_target(
    item: HumanAttentionQueueItem,
    *,
    run: ApplicationPreparationRun,
    stage_result: ApplicationPreparationStageResult,
    now: datetime,
    projector: InputReplacementTargetProjector | None,
) -> HumanAttentionQueueItem:
    if (
        item.resolution_capability
        is not HumanAttentionResolutionCapability.REPLACE_INPUT
        or projector is None
    ):
        return item
    reference = projector.get_current_ref(
        item=item,
        run=run,
        stage_result=stage_result,
        now=now,
    )
    if reference is None:
        return item
    if not isinstance(reference, InputReplacementTargetRef):
        raise ValueError("replacement target projection is invalid")
    content = item.content_dict()
    content["replacement_target_ref"] = reference.to_dict()
    return replace(
        item,
        replacement_target_ref=reference,
        item_content_hash=_canonical_hash(content),
    )


def _answer_mapping(
    reason: UnresolvedAnswerReason,
) -> _AttentionMapping:
    if reason is UnresolvedAnswerReason.MISSING_FACT:
        return _FACT
    if reason is UnresolvedAnswerReason.REQUIRES_USER_CHOICE:
        return _CHOICE
    if reason is UnresolvedAnswerReason.REQUIRES_ATTESTATION:
        return _ATTESTATION
    if reason is UnresolvedAnswerReason.POLICY_FORBIDS_AUTOMATION:
        return _AttentionMapping(
            HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
            HumanAttentionAudience.OPERATOR,
            HumanAttentionResolutionCapability.NON_OVERRIDABLE,
            HumanAttentionReasonCode.POLICY_REQUIRES_OPERATOR,
            "Review the formal policy restriction before continuing.",
        )
    return _AttentionMapping(
        HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER,
        HumanAttentionAudience.OPERATOR,
        HumanAttentionResolutionCapability.NON_OVERRIDABLE,
        HumanAttentionReasonCode.UNSUPPORTED_REQUIRED_ANSWER,
        "Classify the unsupported required answer before continuing.",
    )


@dataclass(frozen=True, slots=True)
class _FactQAProjection:
    material_kind: FactQAMaterialKind
    qa_stage: ApplicationPreparationStage
    qa_result_id: str
    qa_result_content_hash: str
    qa_contract_version: str | None
    blocker_ids: tuple[str, ...] | None
    source_material_id: str | None
    source_material_content_hash: str | None


def _publication_fact_qa_projection(
    stage_result: ApplicationPreparationStageResult,
    *,
    subject_id: str,
    application_plan_id: str,
) -> _FactQAProjection:
    outputs = {item.key: item.value for item in stage_result.outputs}
    required = {
        "publication_stopped_application_plan_id",
        "publication_stopped_material_kind",
        "publication_stopped_source_contract_version",
        "publication_stopped_source_content_hash",
        "publication_stopped_source_directive",
        "publication_stopped_source_lineage_id",
        "publication_stopped_source_kind",
        "publication_stopped_source_outcome",
        "publication_stopped_source_result_id",
        "publication_stopped_source_stage",
        "publication_stopped_subject_id",
    }
    if not required.issubset(outputs) or stage_result.result_id is None:
        raise ValueError("publication Fact QA lineage is incomplete")
    blocker_keys = sorted(
        key
        for key in outputs
        if key.startswith("publication_stopped_blocker_")
    )
    blockers = tuple(outputs[key] for key in blocker_keys)
    lineage_id = outputs["publication_stopped_source_lineage_id"]
    prefix = "publication-stopped-source-"
    if not lineage_id.startswith(prefix):
        raise ValueError("publication lineage ID is invalid")
    lineage = PublicationStoppedSourceLineage(
        lineage_id=lineage_id,
        lineage_content_hash=lineage_id[len(prefix):],
        subject_id=outputs["publication_stopped_subject_id"],
        application_plan_id=outputs[
            "publication_stopped_application_plan_id"
        ],
        publication_stage=stage_result.stage,
        publication_result_id=stage_result.result_id,
        material_kind=PublicationMaterialKind(
            outputs["publication_stopped_material_kind"]
        ),
        source_kind=PublicationStoppedSourceKind(
            outputs["publication_stopped_source_kind"]
        ),
        source_stage=ApplicationPreparationStage(
            outputs["publication_stopped_source_stage"]
        ),
        source_result_id=outputs[
            "publication_stopped_source_result_id"
        ],
        source_outcome=PreparationStageOutcome(
            outputs["publication_stopped_source_outcome"]
        ),
        source_contract_version=outputs[
            "publication_stopped_source_contract_version"
        ],
        source_result_content_hash=outputs[
            "publication_stopped_source_content_hash"
        ],
        source_directive=PublicationBlockingDirective(
            outputs["publication_stopped_source_directive"]
        ),
        source_stop_reason=None,
        source_artifact_id=outputs.get(
            "publication_stopped_source_artifact_id"
        ),
        source_artifact_version=outputs.get(
            "publication_stopped_source_artifact_version"
        ),
        source_artifact_content_hash=outputs.get(
            "publication_stopped_source_artifact_hash"
        ),
        blocking_lineage_ids=blockers,
    )
    if (
        stage_result.result_content_hash != lineage.lineage_content_hash
        or lineage.subject_id != subject_id
        or lineage.application_plan_id != application_plan_id
        or lineage.source_kind
        is not PublicationStoppedSourceKind.FACT_QA_BLOCKER
        or lineage.source_directive
        is not PublicationBlockingDirective.FACT_QA_BLOCKED
        or not lineage.blocking_lineage_ids
        or lineage.source_artifact_id is None
        or lineage.source_artifact_content_hash is None
    ):
        raise ValueError("publication Fact QA lineage binding is invalid")
    material = FactQAMaterialKind(lineage.material_kind.value)
    return _FactQAProjection(
        material_kind=material,
        qa_stage=lineage.source_stage,
        qa_result_id=lineage.source_result_id,
        qa_result_content_hash=lineage.source_result_content_hash,
        qa_contract_version=lineage.source_contract_version,
        blocker_ids=lineage.blocking_lineage_ids,
        source_material_id=lineage.source_artifact_id,
        source_material_content_hash=(
            lineage.source_artifact_content_hash
        ),
    )


def _fact_qa_projection(
    stage_result: ApplicationPreparationStageResult,
    *,
    subject_id: str,
    application_plan_id: str,
) -> _FactQAProjection | None:
    stop_reason = stage_result.stop_reason
    if stop_reason is None:
        return None
    if (
        stage_result.stage is ApplicationPreparationStage.RESUME_FACT_QA
        and stop_reason.code is ResumeFactQAStopReason.UNSUPPORTED_CLAIM
    ):
        if (
            stage_result.result_id is None
            or stage_result.result_content_hash is None
        ):
            raise ValueError("direct Resume Fact QA lineage is incomplete")
        return _FactQAProjection(
            FactQAMaterialKind.RESUME,
            ApplicationPreparationStage.RESUME_FACT_QA,
            stage_result.result_id,
            stage_result.result_content_hash,
            None,
            None,
            None,
            None,
        )
    if (
        stage_result.stage
        is ApplicationPreparationStage.COVER_LETTER_FACT_QA
        and stop_reason.code
        is CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM
    ):
        if (
            stage_result.result_id is None
            or stage_result.result_content_hash is None
        ):
            raise ValueError(
                "direct Cover Letter Fact QA lineage is incomplete"
            )
        return _FactQAProjection(
            FactQAMaterialKind.COVER_LETTER,
            ApplicationPreparationStage.COVER_LETTER_FACT_QA,
            stage_result.result_id,
            stage_result.result_content_hash,
            None,
            None,
            None,
            None,
        )
    if (
        stage_result.stage is ApplicationPreparationStage.RESUME_PUBLICATION
        and stop_reason.code
        is PreparedResumePublicationStopReason.FACT_QA_NOT_PASSED
    ) or (
        stage_result.stage
        is ApplicationPreparationStage.COVER_LETTER_PUBLICATION
        and stop_reason.code
        is CoverLetterPublicationStopReason.FACT_QA_NOT_PASSED
    ):
        return _publication_fact_qa_projection(
            stage_result,
            subject_id=subject_id,
            application_plan_id=application_plan_id,
        )
    return None


def _finding_level_items(
    *,
    plan: ApplicationPlan,
    run: ApplicationPreparationRun,
    stage_result: ApplicationPreparationStageResult,
    projection: _FactQAProjection,
    provider: FactQABlockingFindingProvider,
    target_projector: MaterialCorrectionTargetProjector | None,
    now: datetime,
) -> tuple[HumanAttentionQueueItem, ...]:
    read = provider.list_blocking_findings(
        subject_id=plan.subject_id,
        qa_result_id=projection.qa_result_id,
        material_kind=projection.material_kind,
    )
    if (
        read.status is not FactQABlockingFindingReadStatus.FOUND
        or read.finding_set is None
    ):
        raise ValueError("blocking Fact QA findings are unavailable")
    finding_set = read.finding_set
    if (
        finding_set.subject_id != plan.subject_id
        or finding_set.application_plan_id != plan.plan_id
        or finding_set.material_kind is not projection.material_kind
        or finding_set.qa_result_id != projection.qa_result_id
        or finding_set.qa_result_content_hash
        != projection.qa_result_content_hash
        or (
            projection.qa_contract_version is not None
            and finding_set.qa_contract_version
            != projection.qa_contract_version
        )
        or (
            projection.source_material_id is not None
            and any(
                finding.source_material_id
                != projection.source_material_id
                or finding.source_material_content_hash
                != projection.source_material_content_hash
                for finding in finding_set.findings
            )
        )
    ):
        raise ValueError("blocking Fact QA finding-set binding drifted")
    finding_ids = tuple(item.finding_id for item in finding_set.findings)
    if (
        not finding_ids
        or (
            projection.blocker_ids is not None
            and finding_ids != projection.blocker_ids
        )
    ):
        raise ValueError("blocking Fact QA collection is incomplete")
    origin_result_id = stage_result.result_id
    if origin_result_id is None:
        raise ValueError("attention origin has no stable result ID")
    return tuple(
        _attach_correction_target(
            _build_item(
                plan=plan,
                run=run,
                stage=stage_result.stage,
                mapping=_CORRECT_MATERIAL,
                source_reason_code=(
                    stage_result.stop_reason.code.value
                    if stage_result.stop_reason is not None
                    else "UNSUPPORTED_CLAIM"
                ),
                required_action=(
                    "Delete or rewrite this statement because it lacks "
                    "sufficient evidence support."
                ),
                source_record_id=origin_result_id,
                source_event_time=run.completed_at,
                fact_qa_finding_ref=FactQAFindingAttentionRef(
                    subject_id=plan.subject_id,
                    application_plan_id=plan.plan_id,
                    material_kind=projection.material_kind,
                    attention_origin_stage=stage_result.stage,
                    attention_origin_stage_result_id=origin_result_id,
                    fact_qa_stage=projection.qa_stage,
                    qa_result_id=finding_set.qa_result_id,
                    qa_result_content_hash=(
                        finding_set.qa_result_content_hash
                    ),
                    qa_contract_version=finding_set.qa_contract_version,
                    finding_id=finding.finding_id,
                    finding_kind=finding.finding_kind,
                    finding_order=finding.order,
                    source_material_id=finding.source_material_id,
                    source_material_content_hash=(
                        finding.source_material_content_hash
                    ),
                ),
            ),
            run=run,
            stage_result=stage_result,
            now=now,
            projector=target_projector,
        )
        for finding in finding_set.findings
    )


def _failure(
    *,
    subject_id: str,
    now: datetime,
    reason: HumanAttentionQueueFailureReason,
) -> HumanAttentionQueueResult:
    return HumanAttentionQueueResult(
        status=HumanAttentionQueueStatus.FAILED,
        subject_id=subject_id,
        items=(),
        item_count=0,
        user_item_count=0,
        operator_item_count=0,
        affected_plan_count=0,
        queue_snapshot_hash=None,
        evaluated_at=now,
        reason_code=reason,
        message=f"Human attention queue failed: {reason.value}.",
    )


def build_current_human_attention_queue(
    *,
    subject_id: str,
    now: datetime,
    run_repository: ApplicationPreparationRunRepository,
    application_plan_repository: ApplicationPlanRepository,
    answer_set_repository: PreparedApplicationAnswerSetRepository,
    fact_qa_finding_provider: FactQABlockingFindingProvider | None = None,
    material_correction_target_projector: (
        MaterialCorrectionTargetProjector | None
    ) = None,
    input_replacement_target_projector: (
        InputReplacementTargetProjector | None
    ) = None,
) -> HumanAttentionQueueResult:
    try:
        subject = _clean_text("subject_id", subject_id, 160)
        evaluated = _require_aware("now", now)
    except (TypeError, ValueError):
        return _failure(
            subject_id=(
                subject_id.strip()
                if isinstance(subject_id, str) and subject_id.strip()
                else "invalid-subject"
            ),
            now=(
                now
                if isinstance(now, datetime) and now.tzinfo is not None
                else datetime.min.replace(tzinfo=timezone.utc)
            ),
            reason=HumanAttentionQueueFailureReason.INVALID_REQUEST,
        )
    try:
        listed = run_repository.list_for_subject(subject_id=subject)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            subject_id=subject,
            now=evaluated,
            reason=(
                HumanAttentionQueueFailureReason
                .RUN_LIST_INTEGRITY_FAILURE
            ),
        )
    if (
        listed.status
        is not ApplicationPreparationRunListStatus.SUCCEEDED
    ):
        return _failure(
            subject_id=subject,
            now=evaluated,
            reason=(
                HumanAttentionQueueFailureReason
                .RUN_LIST_INTEGRITY_FAILURE
            ),
        )
    listed_ids = {run.run_id for run in listed.runs}
    current_runs: list[ApplicationPreparationRun] = []
    for plan_id in sorted(
        {run.application_plan_id for run in listed.runs}
    ):
        try:
            current = run_repository.find_current_for_plan(
                subject_id=subject, application_plan_id=plan_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .CURRENT_RUN_INTEGRITY_FAILURE
                ),
            )
        if (
            current.status
            is not ApplicationPreparationRunReadStatus.FOUND
            or current.run is None
            or current.run.run_id not in listed_ids
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .CURRENT_RUN_INTEGRITY_FAILURE
                ),
            )
        current_runs.append(current.run)

    items: list[HumanAttentionQueueItem] = []
    for run in current_runs:
        try:
            plan_read = application_plan_repository.get(
                run.application_plan_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .APPLICATION_PLAN_INTEGRITY_FAILURE
                ),
            )
        if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .APPLICATION_PLAN_NOT_FOUND
                ),
            )
        if (
            plan_read.status is not ApplicationPlanReadStatus.FOUND
            or not isinstance(plan_read.plan, ApplicationPlan)
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .APPLICATION_PLAN_INTEGRITY_FAILURE
                ),
            )
        plan = plan_read.plan
        if (
            plan.subject_id != subject
            or run.subject_id != subject
            or plan.plan_id != run.application_plan_id
            or plan.job_id != run.job_id
            or plan.job_revision != run.job_revision
            or plan.job_content_hash != run.job_content_hash
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .APPLICATION_PLAN_BINDING_MISMATCH
                ),
            )
        if run.overall_status is ApplicationPreparationRunStatus.DEFERRED:
            final_stage = run.stage_results[-1]
            try:
                fact_projection = _fact_qa_projection(
                    final_stage,
                    subject_id=subject,
                    application_plan_id=plan.plan_id,
                )
                if fact_projection is not None:
                    if fact_qa_finding_provider is None:
                        raise ValueError(
                            "blocking-finding provider is unavailable"
                        )
                    items.extend(
                        _finding_level_items(
                            plan=plan,
                            run=run,
                            stage_result=final_stage,
                            projection=fact_projection,
                            provider=fact_qa_finding_provider,
                            target_projector=(
                                material_correction_target_projector
                            ),
                            now=evaluated,
                        )
                    )
                    continue
            except (OSError, RuntimeError, TypeError, ValueError):
                items.append(
                    _build_item(
                        plan=plan,
                        run=run,
                        stage=run.deferred_stage,
                        mapping=_UNKNOWN,
                        source_reason_code=run.deferred_reason,
                        required_action=_UNKNOWN.required_action,
                        source_record_id=(
                            final_stage.result_id or run.run_id
                        ),
                        source_event_time=run.completed_at,
                    )
                )
                continue
            mapping = _typed_deferred_mapping(
                final_stage,
                subject_id=subject,
                application_plan_id=plan.plan_id,
            )
            try:
                projected_item = _attach_replacement_target(
                    _attach_correction_target(
                        _build_item(
                            plan=plan,
                            run=run,
                            stage=run.deferred_stage,
                            mapping=mapping,
                            source_reason_code=run.deferred_reason,
                            required_action=mapping.required_action,
                            source_record_id=(
                                final_stage.result_id or run.run_id
                            ),
                            source_event_time=run.completed_at,
                        ),
                        run=run,
                        stage_result=final_stage,
                        now=evaluated,
                        projector=material_correction_target_projector,
                    ),
                    run=run,
                    stage_result=final_stage,
                    now=evaluated,
                    projector=input_replacement_target_projector,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                projected_item = _build_item(
                    plan=plan,
                    run=run,
                    stage=run.deferred_stage,
                    mapping=_UNKNOWN,
                    source_reason_code=run.deferred_reason,
                    required_action=_UNKNOWN.required_action,
                    source_record_id=final_stage.result_id or run.run_id,
                    source_event_time=run.completed_at,
                )
            items.append(projected_item)
            continue
        if run.overall_status is ApplicationPreparationRunStatus.FAILED:
            final_stage = run.stage_results[-1]
            mapping = _AttentionMapping(
                HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
                HumanAttentionAudience.OPERATOR,
                HumanAttentionResolutionCapability.OPERATOR_REPAIR,
                HumanAttentionReasonCode
                .SYSTEM_INTEGRITY_OR_CONTRACT_FAILURE,
                "Inspect the failed preparation contract or managed state.",
            )
            items.append(
                _build_item(
                    plan=plan,
                    run=run,
                    stage=run.failed_stage,
                    mapping=mapping,
                    source_reason_code=run.failed_reason,
                    required_action=mapping.required_action,
                    source_record_id=(
                        final_stage.result_id or run.run_id
                    ),
                    source_event_time=run.completed_at,
                )
            )
            continue
        if not run.human_attention_required:
            continue
        if run.final_prepared_application_answer_set_id is None:
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_BINDING_MISMATCH
                ),
            )
        try:
            answer_read = answer_set_repository.get(
                subject_id=subject,
                answer_set_id=(
                    run.final_prepared_application_answer_set_id
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_INTEGRITY_FAILURE
                ),
            )
        if (
            answer_read.status
            is PreparedApplicationAnswerSetReadStatus.NOT_FOUND
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason.ANSWER_SET_NOT_FOUND
                ),
            )
        if (
            answer_read.status
            is not PreparedApplicationAnswerSetReadStatus.FOUND
            or not isinstance(
                answer_read.answer_set, PreparedApplicationAnswerSet
            )
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_INTEGRITY_FAILURE
                ),
            )
        answer_set = answer_read.answer_set
        if (
            answer_set.answer_set_id
            != run.final_prepared_application_answer_set_id
            or answer_set.subject_id != subject
            or answer_set.application_plan_id != plan.plan_id
            or answer_set.job_id != plan.job_id
            or answer_set.job_revision != plan.job_revision
            or answer_set.job_content_hash != plan.job_content_hash
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_BINDING_MISMATCH
                ),
            )
        blocking = tuple(
            unresolved
            for unresolved in answer_set.unresolved_items
            if unresolved.blocking
        )
        if not blocking:
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_BINDING_MISMATCH
                ),
            )
        for unresolved in blocking:
            mapping = _answer_mapping(unresolved.reason)
            items.append(
                _build_item(
                    plan=plan,
                    run=run,
                    stage=ApplicationPreparationStage.APPLICATION_ANSWERS,
                    mapping=mapping,
                    source_reason_code=unresolved.reason.value,
                    required_action=unresolved.required_human_action,
                    source_record_id=(
                        unresolved.unresolved_content_hash
                    ),
                    source_event_time=answer_set.prepared_at,
                    canonical_answer_key=unresolved.canonical_key,
                    answer_set=answer_set,
                )
            )

    deduplicated: dict[tuple[str, ...], HumanAttentionQueueItem] = {}
    passthrough: list[HumanAttentionQueueItem] = []
    origin_priority = {
        ApplicationPreparationStage.RESUME_FACT_QA: 0,
        ApplicationPreparationStage.COVER_LETTER_FACT_QA: 0,
        ApplicationPreparationStage.RESUME_PUBLICATION: 1,
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION: 1,
    }
    for item in items:
        reference = item.fact_qa_finding_ref
        if reference is None:
            passthrough.append(item)
            continue
        key = (
            item.subject_id,
            item.application_plan_id,
            reference.qa_result_id,
            reference.finding_id,
        )
        existing = deduplicated.get(key)
        if existing is None or origin_priority[item.source_stage] < (
            origin_priority[existing.source_stage]
        ):
            deduplicated[key] = item
    ordered = tuple(
        sorted(
            [*passthrough, *deduplicated.values()],
            key=_item_sort_key,
        )
    )
    snapshot_content = {
        "affected_plan_count": len(
            {item.application_plan_id for item in ordered}
        ),
        "contract_version": HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION,
        "item_count": len(ordered),
        "item_hashes": [item.item_content_hash for item in ordered],
        "mapping_version": HUMAN_ATTENTION_MAPPING_VERSION,
        "operator_item_count": sum(
            item.audience is HumanAttentionAudience.OPERATOR
            for item in ordered
        ),
        "subject_id": subject,
        "user_item_count": sum(
            item.audience is HumanAttentionAudience.USER
            for item in ordered
        ),
    }
    return HumanAttentionQueueResult(
        status=HumanAttentionQueueStatus.SUCCEEDED,
        subject_id=subject,
        items=ordered,
        item_count=snapshot_content["item_count"],
        user_item_count=snapshot_content["user_item_count"],
        operator_item_count=snapshot_content["operator_item_count"],
        affected_plan_count=snapshot_content["affected_plan_count"],
        queue_snapshot_hash=_canonical_hash(snapshot_content),
        evaluated_at=evaluated,
        reason_code=None,
        message="Current human attention queue was derived.",
    )


__all__ = [
    "HUMAN_ATTENTION_MAPPING_VERSION",
    "HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION",
    "FACT_QA_FINDING_ATTENTION_REF_CONTRACT_VERSION",
    "FactQAFindingAttentionRef",
    "HumanAttentionAudience",
    "HumanAttentionKind",
    "HumanAttentionQueueFailureReason",
    "HumanAttentionQueueItem",
    "HumanAttentionQueueResult",
    "HumanAttentionQueueStatus",
    "HumanAttentionReasonCode",
    "HumanAttentionResolutionCapability",
    "InputReplacementTargetProjector",
    "InputReplacementTargetRef",
    "MaterialCorrectionTargetProjector",
    "MaterialCorrectionTargetRef",
    "build_current_human_attention_queue",
]
