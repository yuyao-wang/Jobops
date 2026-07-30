"""Production adapters and the canonical Application Preparation recipe."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping

from adapters.preparation_agents import (
    PRODUCTION_PREPARATION_AGENT_FACTORY_CONTRACT_VERSION,
    ProductionBaseLatexSelectionAgent,
    ProductionCoverLetterAgent,
    ProductionCoverLetterFactQAAgent,
    ProductionPreparationAgentAdapters,
    ProductionResumeFactQAAgent,
    ProductionResumeLatexConstructionAgent,
    ProductionResumeLayoutRevisionAgent,
    ProductionResumeSelectionAgent,
    ProductionResumeTailoringAgent,
    ProductionResumeVisualQAAgent,
)

from .application_answers import (
    APPLICATION_ANSWER_POLICY_VERSION,
    PREPARED_APPLICATION_ANSWER_SET_CONTRACT_VERSION,
    PrepareApplicationAnswersCommand,
    application_answers_public_result,
    prepare_application_answers,
)
from .application_preparation_orchestrator import (
    APPLICATION_PREPARATION_STAGE_ORDER,
    ApplicationPreparationRecipe,
    ApplicationPreparationStage,
    ApplicationPreparationStageDefinition,
    ApplicationPreparationStageRequest,
    PublicPreparationStageResult,
    RequiredApplicationMaterialPolicy,
)
from .base_latex_selection import (
    BASE_LATEX_SELECTION_CONTRACT_VERSION,
    SelectBaseLatexVersionCommand,
    base_latex_selection_public_result,
    select_base_latex_version,
)
from .candidate_evidence import (
    CANDIDATE_EVIDENCE_CONTRACT_VERSION,
    CreateCandidateEvidenceSnapshotCommand,
    candidate_evidence_snapshot_public_result,
    create_candidate_evidence_snapshot,
)
from .cover_letter_draft import (
    COVER_LETTER_DRAFT_CONTRACT_VERSION,
    COVER_LETTER_DRAFT_POLICY_VERSION,
    DraftCoverLetterCommand,
    cover_letter_draft_public_result,
    draft_cover_letter,
)
from .cover_letter_evidence import (
    COVER_LETTER_EVIDENCE_CONTRACT_VERSION,
    CreateCoverLetterEvidenceSnapshotCommand,
    cover_letter_evidence_public_result,
    create_cover_letter_evidence_snapshot,
)
from .cover_letter_fact_qa import (
    COVER_LETTER_FACT_QA_CONTRACT_VERSION,
    COVER_LETTER_FACT_QA_POLICY_VERSION,
    RunCoverLetterFactQACommand,
    cover_letter_fact_qa_public_result,
    review_cover_letter_fact_qa,
)
from .plan_material_manifest import (
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
    AssemblePlanMaterialManifestCommand,
    assemble_plan_material_manifest,
    resume_manifest_entry_public_result,
)
from .plan_material_manifest_cover_letter import (
    IncludeCoverLetterInPlanMaterialManifestCommand,
    cover_letter_manifest_entry_public_result,
    include_cover_letter_in_plan_material_manifest,
)
from .prepared_cover_letter_material import (
    COVER_LETTER_PUBLICATION_POLICY_VERSION,
    PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION,
    PublishPreparedCoverLetterCommand,
    prepared_cover_letter_publication_public_result,
    publish_prepared_cover_letter,
)
from .prepared_resume_material import (
    PREPARED_RESUME_MATERIAL_CONTRACT_VERSION,
    PublishPreparedResumeCommand,
    prepared_resume_publication_public_result,
    publish_prepared_resume,
)
from .resume_compilation import (
    RESUME_COMPILATION_CONTRACT_VERSION,
    CompileResumeLatexCommand,
    compile_resume_latex,
    resume_compilation_public_result,
)
from .resume_fact_qa import (
    RESUME_FACT_QA_CONTRACT_VERSION,
    RESUME_FACT_QA_POLICY_VERSION,
    RunResumeFactQACommand,
    resume_fact_qa_public_result,
    run_resume_fact_qa,
)
from .resume_latex_construction import (
    RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION,
    RESUME_LATEX_CONSTRUCTION_POLICY_VERSION,
    ConstructResumeLatexCommand,
    construct_resume_latex_version,
    resume_latex_construction_public_result,
)
from .resume_layout_revision import (
    RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
    RESUME_LAYOUT_REVISION_POLICY_VERSION,
    ReviseResumeLayoutCommand,
    resume_layout_revision_public_result,
    revise_resume_layout,
)
from .resume_selection import (
    RESUME_SELECTION_CONTRACT_VERSION,
    SelectBaseResumeCommand,
    base_resume_selection_public_result,
    select_base_resume,
)
from .resume_tailoring import (
    RESUME_TAILORING_CONTRACT_VERSION,
    RESUME_TAILORING_POLICY_VERSION,
    TailorResumeCommand,
    tailor_resume,
    tailored_resume_draft_public_result,
)
from .resume_visual_qa import (
    RESUME_VISUAL_QA_CONTRACT_VERSION,
    RESUME_VISUAL_QA_POLICY_VERSION,
    ReviewResumeVisualQACommand,
    resume_visual_qa_public_result,
    review_resume_visual_qa,
)
from .source_resume_projection import (
    SOURCE_RESUME_PROJECTION_CONTRACT_VERSION,
    CreateSourceResumeProjectionCommand,
    create_source_resume_projection,
    source_resume_projection_public_result,
)


PRODUCTION_PREPARATION_STAGE_ADAPTER_CONTRACT_VERSION = (
    "production-preparation-stage-adapter-v1"
)
PRODUCTION_APPLICATION_PREPARATION_RECIPE_CONTRACT_VERSION = (
    "production-application-preparation-recipe-v1"
)
DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION = (
    "deterministic-preparation-stage-policy-v1"
)

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


class ProductionPreparationRecipeErrorCategory(StrEnum):
    MISSING_DEPENDENCY = "MISSING_DEPENDENCY"
    INVALID_AGENT_BUNDLE = "INVALID_AGENT_BUNDLE"
    MISSING_AGENT_ADAPTER = "MISSING_AGENT_ADAPTER"
    MISSING_CONVERTER = "MISSING_CONVERTER"
    INVALID_CANONICAL_ORDER = "INVALID_CANONICAL_ORDER"
    UNSUPPORTED_CONTRACT_VERSION = "UNSUPPORTED_CONTRACT_VERSION"


class ProductionPreparationRecipeConfigurationError(RuntimeError):
    """A bounded, non-sensitive startup configuration failure."""

    def __init__(
        self,
        category: ProductionPreparationRecipeErrorCategory,
        *,
        stage: ApplicationPreparationStage | None = None,
        dependency: str | None = None,
    ) -> None:
        self.category = ProductionPreparationRecipeErrorCategory(category)
        self.stage = stage
        self.dependency = dependency
        parts = [self.category.value]
        if stage is not None:
            parts.append(stage.value)
        if dependency is not None:
            parts.append(dependency)
        super().__init__(":".join(parts))


@dataclass(frozen=True, slots=True)
class ProductionPreparationStageDependencies:
    """Injected production ports; contains no subject or run state."""

    application_plan_repository: object
    job_repository: object
    resume_candidate_repository: object
    source_resume_artifact_reader: object
    source_resume_parser: object
    resume_selection_decision_repository: object
    source_resume_projection_repository: object
    candidate_evidence_snapshot_repository: object
    tailored_resume_draft_repository: object
    resume_fact_qa_repository: object
    latex_version_repository: object
    base_latex_selection_decision_repository: object
    managed_resume_template_provider: object
    resume_latex_construction_repository: object
    latex_compiler: object
    resume_compilation_repository: object
    resume_compilation_stopped_source_repository: object
    pdf_renderer: object
    resume_visual_qa_repository: object
    resume_layout_revision_record_repository: object
    resume_layout_revision_repository: object
    layout_revision_compile_step: object
    layout_revision_review_step: object
    prepared_resume_material_repository: object
    plan_material_manifest_repository: object
    cover_letter_evidence_snapshot_repository: object
    cover_letter_draft_repository: object
    cover_letter_fact_qa_repository: object
    managed_cover_letter_template_provider: object
    prepared_cover_letter_material_repository: object
    application_fact_provider: object
    application_answer_policy: object
    prepared_application_answer_set_repository: object
    private_home: object
    agents: ProductionPreparationAgentAdapters
    dependency_configuration_hash: str
    resume_selection_override_provider: object | None = None
    resume_tailoring_correction_provider: object | None = None
    base_latex_override_provider: object | None = None
    latex_construction_correction_provider: object | None = None
    resume_visual_qa_policy: object | None = None
    resume_layout_revision_policy: object | None = None
    resume_layout_correction_provider: object | None = None
    cover_letter_correction_provider: object | None = None
    cover_letter_overflow_correction_provider: object | None = None
    application_attestation_provider: object | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.dependency_configuration_hash, str)
            or _HASH_RE.fullmatch(self.dependency_configuration_hash) is None
        ):
            raise ProductionPreparationRecipeConfigurationError(
                ProductionPreparationRecipeErrorCategory
                .UNSUPPORTED_CONTRACT_VERSION,
                dependency="dependency_configuration_hash",
            )


_REQUIRED_DEPENDENCIES = tuple(
    name
    for name in ProductionPreparationStageDependencies.__dataclass_fields__
    if name
    not in {
        "resume_selection_override_provider",
        "resume_tailoring_correction_provider",
        "base_latex_override_provider",
        "latex_construction_correction_provider",
        "resume_visual_qa_policy",
        "resume_layout_revision_policy",
        "resume_layout_correction_provider",
        "cover_letter_correction_provider",
        "cover_letter_overflow_correction_provider",
        "application_attestation_provider",
    }
)


def _require_stage(
    request: ApplicationPreparationStageRequest,
    expected: ApplicationPreparationStage,
) -> None:
    if not isinstance(request, ApplicationPreparationStageRequest):
        raise TypeError("request must be ApplicationPreparationStageRequest")
    if request.stage is not expected:
        raise ProductionPreparationRecipeConfigurationError(
            ProductionPreparationRecipeErrorCategory
            .UNSUPPORTED_CONTRACT_VERSION,
            stage=expected,
            dependency="stage_request",
        )


def _optional_output(
    request: ApplicationPreparationStageRequest, key: str
) -> str | None:
    try:
        return request.output(key)
    except KeyError:
        return None


class _SyncProductionStageAdapter:
    __slots__ = ("stage", "_invoke")

    def __init__(
        self,
        stage: ApplicationPreparationStage,
        invoke: Callable[
            [ApplicationPreparationStageRequest],
            PublicPreparationStageResult,
        ],
    ) -> None:
        self.stage = stage
        self._invoke = invoke

    def __call__(
        self, request: ApplicationPreparationStageRequest
    ) -> PublicPreparationStageResult:
        _require_stage(request, self.stage)
        return self._invoke(request)


class _AsyncProductionStageAdapter:
    __slots__ = ("stage", "_invoke")

    def __init__(
        self,
        stage: ApplicationPreparationStage,
        invoke: Callable[..., Any],
    ) -> None:
        self.stage = stage
        self._invoke = invoke

    async def __call__(
        self, request: ApplicationPreparationStageRequest
    ) -> PublicPreparationStageResult:
        _require_stage(request, self.stage)
        return await self._invoke(request)


@dataclass(frozen=True, slots=True)
class _StageContract:
    stage: ApplicationPreparationStage
    callable_name: str
    slice_contract_version: str
    slice_policy_version: str
    asynchronous: bool
    agent_attribute: str | None


_STAGE_CONTRACTS = (
    _StageContract(
        ApplicationPreparationStage.BASE_RESUME_SELECTION,
        "production_base_resume_selection",
        RESUME_SELECTION_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        True,
        "resume_selection",
    ),
    _StageContract(
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
        "production_source_resume_projection",
        SOURCE_RESUME_PROJECTION_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        False,
        None,
    ),
    _StageContract(
        ApplicationPreparationStage.RESUME_EVIDENCE,
        "production_resume_evidence",
        CANDIDATE_EVIDENCE_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        False,
        None,
    ),
    _StageContract(
        ApplicationPreparationStage.RESUME_TAILORING,
        "production_resume_tailoring",
        RESUME_TAILORING_CONTRACT_VERSION,
        RESUME_TAILORING_POLICY_VERSION,
        True,
        "resume_tailoring",
    ),
    _StageContract(
        ApplicationPreparationStage.RESUME_FACT_QA,
        "production_resume_fact_qa",
        RESUME_FACT_QA_CONTRACT_VERSION,
        RESUME_FACT_QA_POLICY_VERSION,
        True,
        "resume_fact_qa",
    ),
    _StageContract(
        ApplicationPreparationStage.BASE_LATEX_SELECTION,
        "production_base_latex_selection",
        BASE_LATEX_SELECTION_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        True,
        "base_latex_selection",
    ),
    _StageContract(
        ApplicationPreparationStage.LATEX_CONSTRUCTION,
        "production_latex_construction",
        RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION,
        RESUME_LATEX_CONSTRUCTION_POLICY_VERSION,
        True,
        "resume_latex_construction",
    ),
    _StageContract(
        ApplicationPreparationStage.RESUME_COMPILATION,
        "production_resume_compilation",
        RESUME_COMPILATION_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        False,
        None,
    ),
    _StageContract(
        ApplicationPreparationStage.RESUME_VISUAL_QA,
        "production_resume_visual_qa",
        RESUME_VISUAL_QA_CONTRACT_VERSION,
        RESUME_VISUAL_QA_POLICY_VERSION,
        True,
        "resume_visual_qa",
    ),
    _StageContract(
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
        "production_resume_layout_revision",
        RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
        RESUME_LAYOUT_REVISION_POLICY_VERSION,
        True,
        "resume_layout_revision",
    ),
    _StageContract(
        ApplicationPreparationStage.RESUME_PUBLICATION,
        "production_resume_publication",
        PREPARED_RESUME_MATERIAL_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        False,
        None,
    ),
    _StageContract(
        ApplicationPreparationStage.RESUME_MANIFEST,
        "production_resume_manifest",
        PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        False,
        None,
    ),
    _StageContract(
        ApplicationPreparationStage.COVER_LETTER_EVIDENCE,
        "production_cover_letter_evidence",
        COVER_LETTER_EVIDENCE_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        False,
        None,
    ),
    _StageContract(
        ApplicationPreparationStage.COVER_LETTER_DRAFT,
        "production_cover_letter_draft",
        COVER_LETTER_DRAFT_CONTRACT_VERSION,
        COVER_LETTER_DRAFT_POLICY_VERSION,
        True,
        "cover_letter",
    ),
    _StageContract(
        ApplicationPreparationStage.COVER_LETTER_FACT_QA,
        "production_cover_letter_fact_qa",
        COVER_LETTER_FACT_QA_CONTRACT_VERSION,
        COVER_LETTER_FACT_QA_POLICY_VERSION,
        True,
        "cover_letter_fact_qa",
    ),
    _StageContract(
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        "production_cover_letter_publication",
        PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION,
        COVER_LETTER_PUBLICATION_POLICY_VERSION,
        False,
        None,
    ),
    _StageContract(
        ApplicationPreparationStage.COVER_LETTER_MANIFEST,
        "production_cover_letter_manifest",
        PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
        DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION,
        False,
        None,
    ),
    _StageContract(
        ApplicationPreparationStage.APPLICATION_ANSWERS,
        "production_application_answers",
        PREPARED_APPLICATION_ANSWER_SET_CONTRACT_VERSION,
        APPLICATION_ANSWER_POLICY_VERSION,
        False,
        None,
    ),
)

_STAGE_CONVERTER_NAMES = MappingProxyType(
    {
        ApplicationPreparationStage.BASE_RESUME_SELECTION: (
            "base_resume_selection_public_result"
        ),
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: (
            "source_resume_projection_public_result"
        ),
        ApplicationPreparationStage.RESUME_EVIDENCE: (
            "candidate_evidence_snapshot_public_result"
        ),
        ApplicationPreparationStage.RESUME_TAILORING: (
            "tailored_resume_draft_public_result"
        ),
        ApplicationPreparationStage.RESUME_FACT_QA: "resume_fact_qa_public_result",
        ApplicationPreparationStage.BASE_LATEX_SELECTION: (
            "base_latex_selection_public_result"
        ),
        ApplicationPreparationStage.LATEX_CONSTRUCTION: (
            "resume_latex_construction_public_result"
        ),
        ApplicationPreparationStage.RESUME_COMPILATION: (
            "resume_compilation_public_result"
        ),
        ApplicationPreparationStage.RESUME_VISUAL_QA: "resume_visual_qa_public_result",
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION: (
            "resume_layout_revision_public_result"
        ),
        ApplicationPreparationStage.RESUME_PUBLICATION: (
            "prepared_resume_publication_public_result"
        ),
        ApplicationPreparationStage.RESUME_MANIFEST: (
            "resume_manifest_entry_public_result"
        ),
        ApplicationPreparationStage.COVER_LETTER_EVIDENCE: (
            "cover_letter_evidence_public_result"
        ),
        ApplicationPreparationStage.COVER_LETTER_DRAFT: (
            "cover_letter_draft_public_result"
        ),
        ApplicationPreparationStage.COVER_LETTER_FACT_QA: (
            "cover_letter_fact_qa_public_result"
        ),
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION: (
            "prepared_cover_letter_publication_public_result"
        ),
        ApplicationPreparationStage.COVER_LETTER_MANIFEST: (
            "cover_letter_manifest_entry_public_result"
        ),
        ApplicationPreparationStage.APPLICATION_ANSWERS: (
            "application_answers_public_result"
        ),
    }
)

_AGENT_METHODS = MappingProxyType(
    {
        "resume_selection": "evaluate",
        "resume_tailoring": "tailor",
        "resume_fact_qa": "review",
        "base_latex_selection": "evaluate",
        "resume_latex_construction": "construct",
        "resume_visual_qa": "review",
        "resume_layout_revision": "revise",
        "cover_letter": "generate",
        "cover_letter_fact_qa": "review",
    }
)

_AGENT_TYPES = MappingProxyType(
    {
        "resume_selection": ProductionResumeSelectionAgent,
        "resume_tailoring": ProductionResumeTailoringAgent,
        "resume_fact_qa": ProductionResumeFactQAAgent,
        "base_latex_selection": ProductionBaseLatexSelectionAgent,
        "resume_latex_construction": (
            ProductionResumeLatexConstructionAgent
        ),
        "resume_visual_qa": ProductionResumeVisualQAAgent,
        "resume_layout_revision": ProductionResumeLayoutRevisionAgent,
        "cover_letter": ProductionCoverLetterAgent,
        "cover_letter_fact_qa": ProductionCoverLetterFactQAAgent,
    }
)


def _build_invokers(
    d: ProductionPreparationStageDependencies,
) -> Mapping[ApplicationPreparationStage, Callable[..., Any]]:
    a = d.agents

    async def base_resume(r):
        return base_resume_selection_public_result(
            await select_base_resume(
                SelectBaseResumeCommand(
                    r.subject_id, r.application_plan_id, r.now
                ),
                application_plan_repository=d.application_plan_repository,
                job_repository=d.job_repository,
                candidate_provider=d.resume_candidate_repository,
                agent=a.resume_selection,
                metadata=a.resume_selection.metadata,
                decision_repository=d.resume_selection_decision_repository,
                override_provider=d.resume_selection_override_provider,
            )
        )

    def source_projection(r):
        return source_resume_projection_public_result(
            create_source_resume_projection(
                CreateSourceResumeProjectionCommand(
                    r.subject_id, r.output("resume_id"), r.now
                ),
                candidate_repository=d.resume_candidate_repository,
                artifact_reader=d.source_resume_artifact_reader,
                parser=d.source_resume_parser,
                projection_repository=d.source_resume_projection_repository,
            )
        )

    def resume_evidence(r):
        return candidate_evidence_snapshot_public_result(
            create_candidate_evidence_snapshot(
                CreateCandidateEvidenceSnapshotCommand(
                    r.subject_id, r.application_plan_id, r.now
                ),
                application_plan_repository=d.application_plan_repository,
                selection_repository=d.resume_selection_decision_repository,
                candidate_repository=d.resume_candidate_repository,
                projection_repository=d.source_resume_projection_repository,
                snapshot_repository=d.candidate_evidence_snapshot_repository,
            )
        )

    async def tailoring(r):
        return tailored_resume_draft_public_result(
            await tailor_resume(
                TailorResumeCommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.output("resume_evidence_snapshot_id"),
                    r.now,
                ),
                application_plan_repository=d.application_plan_repository,
                job_repository=d.job_repository,
                selection_repository=d.resume_selection_decision_repository,
                candidate_repository=d.resume_candidate_repository,
                projection_repository=d.source_resume_projection_repository,
                evidence_snapshot_repository=d.candidate_evidence_snapshot_repository,
                agent=a.resume_tailoring,
                metadata=a.resume_tailoring.metadata,
                draft_repository=d.tailored_resume_draft_repository,
                correction_provider=d.resume_tailoring_correction_provider,
            )
        )

    async def resume_qa(r):
        return resume_fact_qa_public_result(
            await run_resume_fact_qa(
                RunResumeFactQACommand(
                    r.subject_id,
                    r.output("tailored_resume_draft_id"),
                    r.now,
                ),
                draft_repository=d.tailored_resume_draft_repository,
                application_plan_repository=d.application_plan_repository,
                job_repository=d.job_repository,
                selection_repository=d.resume_selection_decision_repository,
                projection_repository=d.source_resume_projection_repository,
                evidence_snapshot_repository=d.candidate_evidence_snapshot_repository,
                agent=a.resume_fact_qa,
                metadata=a.resume_fact_qa.metadata,
                qa_repository=d.resume_fact_qa_repository,
            )
        )

    async def base_latex(r):
        return base_latex_selection_public_result(
            await select_base_latex_version(
                SelectBaseLatexVersionCommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.output("resume_fact_qa_result_id"),
                    r.now,
                ),
                application_plan_repository=d.application_plan_repository,
                fact_qa_repository=d.resume_fact_qa_repository,
                draft_repository=d.tailored_resume_draft_repository,
                selection_repository=d.resume_selection_decision_repository,
                job_repository=d.job_repository,
                latex_version_provider=d.latex_version_repository,
                agent=a.base_latex_selection,
                metadata=a.base_latex_selection.metadata,
                decision_repository=d.base_latex_selection_decision_repository,
                override_provider=d.base_latex_override_provider,
            )
        )

    async def latex_construction(r):
        return resume_latex_construction_public_result(
            await construct_resume_latex_version(
                ConstructResumeLatexCommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.output("base_latex_selection_id"),
                    r.output("resume_fact_qa_result_id"),
                    r.now,
                ),
                application_plan_repository=d.application_plan_repository,
                draft_repository=d.tailored_resume_draft_repository,
                fact_qa_repository=d.resume_fact_qa_repository,
                base_selection_repository=d.base_latex_selection_decision_repository,
                latex_version_repository=d.latex_version_repository,
                template_provider=d.managed_resume_template_provider,
                agent=a.resume_latex_construction,
                metadata=a.resume_latex_construction.metadata,
                construction_repository=d.resume_latex_construction_repository,
                correction_provider=d.latex_construction_correction_provider,
                home=d.private_home,
            )
        )

    def compilation(r):
        return resume_compilation_public_result(
            compile_resume_latex(
                CompileResumeLatexCommand(
                    r.subject_id,
                    r.output("latex_construction_record_id"),
                    r.output("latex_version_id"),
                    r.now,
                ),
                construction_repository=d.resume_latex_construction_repository,
                latex_version_repository=d.latex_version_repository,
                compiler=d.latex_compiler,
                compilation_repository=d.resume_compilation_repository,
                home=d.private_home,
            ),
            preparation_invocation_binding=(
                r.preparation_invocation_binding
            ),
            application_plan_id=r.application_plan_id,
            stopped_source_repository=(
                d.resume_compilation_stopped_source_repository
            ),
        )

    async def visual_qa(r):
        return resume_visual_qa_public_result(
            await review_resume_visual_qa(
                ReviewResumeVisualQACommand(
                    r.subject_id,
                    r.output("compilation_record_id"),
                    r.now,
                ),
                compilation_repository=d.resume_compilation_repository,
                latex_version_repository=d.latex_version_repository,
                construction_repository=d.resume_latex_construction_repository,
                draft_repository=d.tailored_resume_draft_repository,
                renderer=d.pdf_renderer,
                agent=a.resume_visual_qa,
                metadata=a.resume_visual_qa.metadata,
                visual_qa_repository=d.resume_visual_qa_repository,
                policy=d.resume_visual_qa_policy,
                home=d.private_home,
            )
        )

    async def layout_revision(r):
        return resume_layout_revision_public_result(
            await revise_resume_layout(
                ReviseResumeLayoutCommand(
                    r.subject_id,
                    r.output("visual_qa_result_id"),
                    r.now,
                    r.application_plan_id,
                ),
                visual_qa_repository=d.resume_visual_qa_repository,
                compilation_repository=d.resume_compilation_repository,
                latex_version_repository=d.latex_version_repository,
                provenance_repository=d.resume_latex_construction_repository,
                revision_record_repository=d.resume_layout_revision_record_repository,
                application_plan_repository=d.application_plan_repository,
                draft_repository=d.tailored_resume_draft_repository,
                renderer=d.pdf_renderer,
                agent=a.resume_layout_revision,
                metadata=a.resume_layout_revision.metadata,
                compile_step=d.layout_revision_compile_step,
                review_step=d.layout_revision_review_step,
                revision_repository=d.resume_layout_revision_repository,
                policy=d.resume_layout_revision_policy,
                home=d.private_home,
                correction_provider=d.resume_layout_correction_provider,
            )
        )

    def resume_publication(r):
        layout_id = _optional_output(r, "layout_revision_run_id")
        return prepared_resume_publication_public_result(
            publish_prepared_resume(
                PublishPreparedResumeCommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.now,
                    None if layout_id else r.output("visual_qa_result_id"),
                    layout_id,
                ),
                application_plan_repository=d.application_plan_repository,
                draft_repository=d.tailored_resume_draft_repository,
                fact_qa_repository=d.resume_fact_qa_repository,
                latex_version_repository=d.latex_version_repository,
                compilation_repository=d.resume_compilation_repository,
                visual_qa_repository=d.resume_visual_qa_repository,
                layout_revision_repository=d.resume_layout_revision_repository,
                material_repository=d.prepared_resume_material_repository,
                home=d.private_home,
            )
        )

    def resume_manifest(r):
        return resume_manifest_entry_public_result(
            assemble_plan_material_manifest(
                AssemblePlanMaterialManifestCommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.output("prepared_resume_material_id"),
                    r.now,
                ),
                application_plan_repository=d.application_plan_repository,
                prepared_resume_repository=d.prepared_resume_material_repository,
                manifest_repository=d.plan_material_manifest_repository,
                home=d.private_home,
            )
        )

    def cover_evidence(r):
        return cover_letter_evidence_public_result(
            create_cover_letter_evidence_snapshot(
                CreateCoverLetterEvidenceSnapshotCommand(
                    r.subject_id, r.application_plan_id, r.now
                ),
                application_plan_repository=d.application_plan_repository,
                selection_repository=d.resume_selection_decision_repository,
                candidate_repository=d.resume_candidate_repository,
                projection_repository=d.source_resume_projection_repository,
                snapshot_repository=d.cover_letter_evidence_snapshot_repository,
            )
        )

    async def cover_draft(r):
        return cover_letter_draft_public_result(
            await draft_cover_letter(
                DraftCoverLetterCommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.output("cover_letter_evidence_snapshot_id"),
                    r.now,
                ),
                application_plan_repository=d.application_plan_repository,
                job_repository=d.job_repository,
                evidence_snapshot_repository=(
                    d.cover_letter_evidence_snapshot_repository
                ),
                agent=a.cover_letter,
                metadata=a.cover_letter.metadata,
                draft_repository=d.cover_letter_draft_repository,
                correction_provider=d.cover_letter_correction_provider,
            )
        )

    async def cover_qa(r):
        return cover_letter_fact_qa_public_result(
            await review_cover_letter_fact_qa(
                RunCoverLetterFactQACommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.output("cover_letter_evidence_snapshot_id"),
                    r.output("cover_letter_draft_id"),
                    r.now,
                ),
                application_plan_repository=d.application_plan_repository,
                job_repository=d.job_repository,
                evidence_snapshot_repository=(
                    d.cover_letter_evidence_snapshot_repository
                ),
                draft_repository=d.cover_letter_draft_repository,
                agent=a.cover_letter_fact_qa,
                metadata=a.cover_letter_fact_qa.metadata,
                result_repository=d.cover_letter_fact_qa_repository,
            )
        )

    def cover_publication(r):
        return prepared_cover_letter_publication_public_result(
            publish_prepared_cover_letter(
                PublishPreparedCoverLetterCommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.output("cover_letter_fact_qa_result_id"),
                    r.now,
                ),
                application_plan_repository=d.application_plan_repository,
                job_repository=d.job_repository,
                draft_repository=d.cover_letter_draft_repository,
                fact_qa_repository=d.cover_letter_fact_qa_repository,
                template_provider=d.managed_cover_letter_template_provider,
                compiler=d.latex_compiler,
                material_repository=d.prepared_cover_letter_material_repository,
                home=d.private_home,
                correction_provider=d.cover_letter_overflow_correction_provider,
            )
        )

    def cover_manifest(r):
        return cover_letter_manifest_entry_public_result(
            include_cover_letter_in_plan_material_manifest(
                IncludeCoverLetterInPlanMaterialManifestCommand(
                    r.subject_id,
                    r.application_plan_id,
                    r.output("plan_material_manifest_id"),
                    r.output("prepared_cover_letter_material_id"),
                    r.now,
                ),
                application_plan_repository=d.application_plan_repository,
                manifest_repository=d.plan_material_manifest_repository,
                prepared_cover_letter_repository=(
                    d.prepared_cover_letter_material_repository
                ),
                home=d.private_home,
            )
        )

    def answers(r):
        return application_answers_public_result(
            prepare_application_answers(
                PrepareApplicationAnswersCommand(
                    r.subject_id, r.application_plan_id, r.now
                ),
                application_plan_repository=d.application_plan_repository,
                fact_provider=d.application_fact_provider,
                answer_policy=d.application_answer_policy,
                answer_set_repository=d.prepared_application_answer_set_repository,
                attestation_provider=d.application_attestation_provider,
            )
        )

    return MappingProxyType(
        {
            ApplicationPreparationStage.BASE_RESUME_SELECTION: base_resume,
            ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: source_projection,
            ApplicationPreparationStage.RESUME_EVIDENCE: resume_evidence,
            ApplicationPreparationStage.RESUME_TAILORING: tailoring,
            ApplicationPreparationStage.RESUME_FACT_QA: resume_qa,
            ApplicationPreparationStage.BASE_LATEX_SELECTION: base_latex,
            ApplicationPreparationStage.LATEX_CONSTRUCTION: latex_construction,
            ApplicationPreparationStage.RESUME_COMPILATION: compilation,
            ApplicationPreparationStage.RESUME_VISUAL_QA: visual_qa,
            ApplicationPreparationStage.RESUME_LAYOUT_REVISION: layout_revision,
            ApplicationPreparationStage.RESUME_PUBLICATION: resume_publication,
            ApplicationPreparationStage.RESUME_MANIFEST: resume_manifest,
            ApplicationPreparationStage.COVER_LETTER_EVIDENCE: cover_evidence,
            ApplicationPreparationStage.COVER_LETTER_DRAFT: cover_draft,
            ApplicationPreparationStage.COVER_LETTER_FACT_QA: cover_qa,
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION: cover_publication,
            ApplicationPreparationStage.COVER_LETTER_MANIFEST: cover_manifest,
            ApplicationPreparationStage.APPLICATION_ANSWERS: answers,
        }
    )


def _validate_dependencies(
    dependencies: ProductionPreparationStageDependencies,
) -> None:
    if not isinstance(dependencies, ProductionPreparationStageDependencies):
        raise TypeError(
            "dependencies must be ProductionPreparationStageDependencies"
        )
    for name in _REQUIRED_DEPENDENCIES:
        if getattr(dependencies, name) is None:
            raise ProductionPreparationRecipeConfigurationError(
                ProductionPreparationRecipeErrorCategory.MISSING_DEPENDENCY,
                dependency=name,
            )
    agents = dependencies.agents
    if not isinstance(agents, ProductionPreparationAgentAdapters):
        raise ProductionPreparationRecipeConfigurationError(
            ProductionPreparationRecipeErrorCategory.INVALID_AGENT_BUNDLE
        )
    if (
        agents.contract_version
        != PRODUCTION_PREPARATION_AGENT_FACTORY_CONTRACT_VERSION
    ):
        raise ProductionPreparationRecipeConfigurationError(
            ProductionPreparationRecipeErrorCategory
            .UNSUPPORTED_CONTRACT_VERSION,
            dependency="agents",
        )
    for contract in _STAGE_CONTRACTS:
        if contract.agent_attribute is None:
            continue
        agent = getattr(agents, contract.agent_attribute, None)
        method_name = _AGENT_METHODS[contract.agent_attribute]
        if (
            not isinstance(agent, _AGENT_TYPES[contract.agent_attribute])
            or getattr(agent, "metadata", None) is None
            or not callable(getattr(agent, method_name, None))
        ):
            raise ProductionPreparationRecipeConfigurationError(
                ProductionPreparationRecipeErrorCategory
                .MISSING_AGENT_ADAPTER,
                stage=contract.stage,
                dependency=contract.agent_attribute,
            )
    for stage, converter_name in _STAGE_CONVERTER_NAMES.items():
        if not callable(globals().get(converter_name)):
            raise ProductionPreparationRecipeConfigurationError(
                ProductionPreparationRecipeErrorCategory.MISSING_CONVERTER,
                stage=stage,
                dependency=converter_name,
            )


def build_production_application_preparation_recipe(
    dependencies: ProductionPreparationStageDependencies,
) -> ApplicationPreparationRecipe:
    """Build all canonical stages, or fail without returning a partial recipe."""

    _validate_dependencies(dependencies)
    if tuple(item.stage for item in _STAGE_CONTRACTS) != (
        APPLICATION_PREPARATION_STAGE_ORDER
    ):
        raise ProductionPreparationRecipeConfigurationError(
            ProductionPreparationRecipeErrorCategory.INVALID_CANONICAL_ORDER
        )
    invokers = _build_invokers(dependencies)
    if set(invokers) != set(APPLICATION_PREPARATION_STAGE_ORDER):
        raise ProductionPreparationRecipeConfigurationError(
            ProductionPreparationRecipeErrorCategory.INVALID_CANONICAL_ORDER
        )
    definitions: list[ApplicationPreparationStageDefinition] = []
    for contract in _STAGE_CONTRACTS:
        raw = invokers.get(contract.stage)
        if not callable(raw):
            raise ProductionPreparationRecipeConfigurationError(
                ProductionPreparationRecipeErrorCategory.MISSING_CONVERTER,
                stage=contract.stage,
            )
        adapter = (
            _AsyncProductionStageAdapter(contract.stage, raw)
            if contract.asynchronous
            else _SyncProductionStageAdapter(contract.stage, raw)
        )
        configuration_hash = _canonical_hash(
            {
                "adapter_contract_version": (
                    PRODUCTION_PREPARATION_STAGE_ADAPTER_CONTRACT_VERSION
                ),
                "dependency_configuration_hash": (
                    dependencies.dependency_configuration_hash
                ),
                "slice_contract_version": contract.slice_contract_version,
                "slice_policy_version": contract.slice_policy_version,
                "stage": contract.stage.value,
            }
        )
        definitions.append(
            ApplicationPreparationStageDefinition(
                stage=contract.stage,
                public_callable_name=contract.callable_name,
                slice_contract_version=contract.slice_contract_version,
                slice_policy_version=contract.slice_policy_version,
                configuration_hash=configuration_hash,
                invoke=adapter,
            )
        )
    input_binding_hash = _canonical_hash(
        {
            "agent_factory_contract_version": (
                dependencies.agents.contract_version
            ),
            "dependency_configuration_hash": (
                dependencies.dependency_configuration_hash
            ),
            "recipe_contract_version": (
                PRODUCTION_APPLICATION_PREPARATION_RECIPE_CONTRACT_VERSION
            ),
            "stages": [
                {
                    "adapter_contract_version": (
                        PRODUCTION_PREPARATION_STAGE_ADAPTER_CONTRACT_VERSION
                    ),
                    "slice_contract_version": item.slice_contract_version,
                    "slice_policy_version": item.slice_policy_version,
                    "stage": item.stage.value,
                }
                for item in _STAGE_CONTRACTS
            ],
        }
    )
    return ApplicationPreparationRecipe(
        input_binding_hash=input_binding_hash,
        stages=tuple(definitions),
        required_material_policy=RequiredApplicationMaterialPolicy.v1(),
    )


__all__ = [
    "DETERMINISTIC_PREPARATION_STAGE_POLICY_VERSION",
    "PRODUCTION_APPLICATION_PREPARATION_RECIPE_CONTRACT_VERSION",
    "PRODUCTION_PREPARATION_STAGE_ADAPTER_CONTRACT_VERSION",
    "ProductionPreparationRecipeConfigurationError",
    "ProductionPreparationRecipeErrorCategory",
    "ProductionPreparationStageDependencies",
    "build_production_application_preparation_recipe",
]
