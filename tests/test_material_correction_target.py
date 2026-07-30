"""Focused P2b5c Material Correction Target tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from core.application_answers import (
    PrivateHomePreparedApplicationAnswerSetRepository,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.application_plan import PrivateHomeApplicationPlanRepository
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    RESUME_LAYOUT_REVISION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    CoverLetterPublicationStopReason,
    PreparedResumePublicationStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PrivateHomeApplicationPreparationRunRepository,
    PublicPreparationStageResult,
    ResumeLayoutRevisionStopReason,
)
from core.fact_qa_findings import (
    FactQABlockingFindingReadStatus,
    FactQABlockingFindingSetResult,
    FactQAMaterialKind,
)
from core.human_attention_queue import (
    HumanAttentionResolutionCapability,
    build_current_human_attention_queue,
)
from core.material_correction_target import (
    CORRECT_MATERIAL_TARGET_KIND_REGISTRY,
    CoverLetterLayoutCorrectionTarget,
    LatexCompilationCorrectionTarget,
    MaterialCorrectionTargetKind,
    MaterialCorrectionTargetProvider,
    MaterialCorrectionTargetReadStatus,
    MaterialCorrectionTargetStatus,
    PrivateHomeMaterialCorrectionTargetRepository,
    ResumeVisualLayoutCorrectionTarget,
    UnsupportedClaimCorrectionTarget,
)
from core.preparation_invocation import PreparationInvocationBinding
from core.private_home import PrivateHome
from core.publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    create_publication_stopped_source_lineage,
)
from core.resume_compilation import (
    ResumeCompilationReadStatus,
    resume_compilation_public_result,
)
from core.resume_compilation_stopped_source import (
    PrivateHomeResumeCompilationStoppedSourceRepository,
    RepositoryResumeCompilationStoppedSourceProvider,
)
from core.resume_layout_revision import (
    ResumeLayoutAttemptOutcome,
    ResumeLayoutRevisionReadStatus,
    ResumeLayoutRevisionStatus,
)
from dashboard.material_correction_target import (
    MaterialCorrectionTargetUIController,
)
from tests.test_application_preparation_orchestrator import (
    _Recorder,
    _recipe,
)
from tests.test_compilation_source_resolution_lineage import _binding
from tests.test_fact_qa_finding_attention_projection import (
    _FindingProvider,
    _deferred_run,
    _finding_set,
)
from tests.test_human_attention_queue import NOW, _invoke, _plan
from tests.test_resume_compilation import (
    LATEX,
    _FakeCompiler,
    _compile,
    _setup,
)


class _MissingFindingProvider:
    def list_blocking_findings(self, **_kwargs):
        return FactQABlockingFindingSetResult(
            FactQABlockingFindingReadStatus.NOT_FOUND, None
        )


class _MissingStoppedProvider:
    def get(self, **_kwargs):
        return SimpleNamespace(status="NOT_FOUND", record=None)


class _CompilationRepository:
    def __init__(self, records=()):
        self.records = {item.record_id: item for item in records}

    def get(self, *, subject_id, record_id):
        record = self.records.get(record_id)
        return SimpleNamespace(
            status=(
                ResumeCompilationReadStatus.FOUND
                if record is not None and record.subject_id == subject_id
                else ResumeCompilationReadStatus.NOT_FOUND
            ),
            record=record,
        )


class _LayoutRepository:
    def __init__(self, runs=()):
        self.runs = {item.run_id: item for item in runs}

    def get(self, *, subject_id, run_id):
        run = self.runs.get(run_id)
        return SimpleNamespace(
            status=(
                ResumeLayoutRevisionReadStatus.FOUND
                if run is not None and run.subject_id == subject_id
                else ResumeLayoutRevisionReadStatus.NOT_FOUND
            ),
            run=run,
        )


def _target_provider(
    home,
    *,
    finding_provider=None,
    stopped_provider=None,
    compilations=(),
    layouts=(),
    current_item_reader=None,
):
    return MaterialCorrectionTargetProvider(
        repository=PrivateHomeMaterialCorrectionTargetRepository(home),
        finding_provider=finding_provider or _MissingFindingProvider(),
        compilation_stopped_provider=(
            stopped_provider or _MissingStoppedProvider()
        ),
        compilation_repository=_CompilationRepository(compilations),
        layout_repository=_LayoutRepository(layouts),
        current_item_reader=current_item_reader,
    )


def _queue(home, finding_provider, projector):
    return build_current_human_attention_queue(
        subject_id="subject-attention-synthetic",
        now=NOW,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        application_plan_repository=PrivateHomeApplicationPlanRepository(
            home
        ),
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
        fact_qa_finding_provider=finding_provider,
        material_correction_target_projector=projector,
    )


def test_registry_is_10_of_10_and_fact_targets_bind_exact_findings(
    tmp_path,
) -> None:
    assert len(CORRECT_MATERIAL_TARGET_KIND_REGISTRY) == 10
    assert {
        kind: tuple(CORRECT_MATERIAL_TARGET_KIND_REGISTRY.values()).count(
            kind
        )
        for kind in MaterialCorrectionTargetKind
    } == {
        MaterialCorrectionTargetKind.UNSUPPORTED_CLAIM: 4,
        MaterialCorrectionTargetKind.LATEX_COMPILATION: 2,
        MaterialCorrectionTargetKind.RESUME_VISUAL_LAYOUT: 3,
        MaterialCorrectionTargetKind.COVER_LETTER_LAYOUT: 1,
    }
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-correction-finding")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    qa_id, qa_hash = "resume-qa-target", "a" * 64
    _deferred_run(
        plan=plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.RESUME_FACT_QA,
        reason=__import__(
            "core.application_preparation_orchestrator",
            fromlist=["ResumeFactQAStopReason"],
        ).ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        reason_version=__import__(
            "core.application_preparation_orchestrator",
            fromlist=["RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION"],
        ).RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        result_id=qa_id,
        result_hash=qa_hash,
    )
    finding_set = _finding_set(
        plan,
        FactQAMaterialKind.RESUME,
        qa_id,
        qa_hash,
        names=("only",),
    )
    findings = _FindingProvider(
        {
            (
                finding_set.subject_id,
                finding_set.qa_result_id,
                finding_set.material_kind,
            ): finding_set
        }
    )
    projector = _target_provider(home, finding_provider=findings)

    queue = _queue(home, findings, projector)

    assert queue.item_count == 1
    item = queue.items[0]
    assert item.correction_target_ref is not None
    read = projector.repository.get(
        subject_id=item.subject_id,
        target_id=item.correction_target_ref.target_id,
    )
    assert read.status is MaterialCorrectionTargetReadStatus.FOUND
    assert isinstance(read.target.payload, UnsupportedClaimCorrectionTarget)
    assert read.target.payload.finding_ref.finding_id == "finding-only"
    assert read.target.payload.claim_summary == (
        "Synthetic unsupported claim only."
    )
    replay = _queue(home, findings, projector)
    assert replay.items[0].correction_target_ref == item.correction_target_ref


def test_publication_visual_layout_and_overflow_use_formal_lineage(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-publication-targets")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    compilation = SimpleNamespace(
        record_id="resume-compilation-" + "1" * 64,
        subject_id=plan.subject_id,
        contract_version="resume-compilation-v1",
        latex_version_id="resume-latex-version-" + "2" * 64,
        latex_source_sha256="3" * 64,
        pdf_sha256="4" * 64,
    )
    visual_lineage = create_publication_stopped_source_lineage(
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        publication_stage=ApplicationPreparationStage.RESUME_PUBLICATION,
        material_kind=PublicationMaterialKind.RESUME,
        source_kind=PublicationStoppedSourceKind.VISUAL_QA_DIRECTIVE,
        source_stage=ApplicationPreparationStage.RESUME_VISUAL_QA,
        source_result_id="resume-visual-qa-" + "5" * 64,
        source_outcome=PreparationStageOutcome.COMPLETED,
        source_contract_version="resume-visual-qa-v1",
        source_result_content_hash="6" * 64,
        source_directive=(
            PublicationBlockingDirective.VISUAL_QA_REVISION_REQUIRED
        ),
        source_artifact_id=compilation.record_id,
        source_artifact_version=compilation.latex_version_id,
        source_artifact_content_hash=compilation.pdf_sha256,
        blocking_lineage_ids=("visual-finding-1",),
    )
    _deferred_run(
        plan=plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.RESUME_PUBLICATION,
        reason=PreparedResumePublicationStopReason.VISUAL_QA_NOT_PASSED,
        reason_version=(
            PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION
        ),
        result_id=visual_lineage.publication_result_id,
        result_hash=visual_lineage.lineage_content_hash,
        outputs=visual_lineage.output_references(),
    )
    projector = _target_provider(home, compilations=(compilation,))
    queue = _queue(home, _MissingFindingProvider(), projector)
    target = projector.repository.get(
        subject_id=plan.subject_id,
        target_id=queue.items[0].correction_target_ref.target_id,
    ).target
    assert isinstance(target.payload, ResumeVisualLayoutCorrectionTarget)
    assert target.payload.source_result_id == visual_lineage.source_result_id
    assert target.payload.artifact_id == compilation.record_id

    cover_plan, _ = _plan(home, job_id="job-cover-overflow-target")
    overflow = create_publication_stopped_source_lineage(
        subject_id=cover_plan.subject_id,
        application_plan_id=cover_plan.plan_id,
        publication_stage=(
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION
        ),
        material_kind=PublicationMaterialKind.COVER_LETTER,
        source_kind=(
            PublicationStoppedSourceKind.COVER_LETTER_LAYOUT_OVERFLOW
        ),
        source_stage=(
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION
        ),
        source_result_id="cover-letter-overflow-evaluation-" + "7" * 64,
        source_outcome=PreparationStageOutcome.DEFERRED,
        source_contract_version="prepared-cover-letter-material-v1",
        source_result_content_hash="8" * 64,
        source_directive=(
            PublicationBlockingDirective.COVER_LETTER_LAYOUT_OVERFLOW
        ),
        source_artifact_id="cover-letter-latex-source-" + "9" * 64,
        source_artifact_version="cover-letter-template-v1",
        source_artifact_content_hash="9" * 64,
    )
    _deferred_run(
        plan=cover_plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        reason=CoverLetterPublicationStopReason.LAYOUT_OVERFLOW,
        reason_version=(
            COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION
        ),
        result_id=overflow.publication_result_id,
        result_hash=overflow.lineage_content_hash,
        outputs=overflow.output_references(),
    )
    layout_plan, _ = _plan(home, job_id="job-layout-target")
    layout_compilation = SimpleNamespace(
        record_id="resume-compilation-" + "a" * 64,
        subject_id=layout_plan.subject_id,
        contract_version="resume-compilation-v1",
        latex_version_id="resume-latex-version-" + "b" * 64,
        latex_source_sha256="c" * 64,
        pdf_sha256="d" * 64,
    )
    layout = SimpleNamespace(
        run_id="resume-layout-revision-" + "e" * 64,
        subject_id=layout_plan.subject_id,
        application_plan_id=layout_plan.plan_id,
        run_content_hash="f" * 64,
        contract_version="resume-layout-revision-v1",
        max_attempts=2,
        final_status=(
            ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED
        ),
        attempts=(
            SimpleNamespace(
                attempt_number=2,
                input_compilation_record_id=layout_compilation.record_id,
                output_compilation_record_id=None,
                input_latex_version_id=layout_compilation.latex_version_id,
                output_latex_version_id=None,
            ),
        ),
    )
    layout_lineage = create_publication_stopped_source_lineage(
        subject_id=layout_plan.subject_id,
        application_plan_id=layout_plan.plan_id,
        publication_stage=ApplicationPreparationStage.RESUME_PUBLICATION,
        material_kind=PublicationMaterialKind.RESUME,
        source_kind=PublicationStoppedSourceKind.LAYOUT_REVISION_STOP,
        source_stage=ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
        source_result_id=layout.run_id,
        source_outcome=PreparationStageOutcome.DEFERRED,
        source_contract_version=layout.contract_version,
        source_result_content_hash=layout.run_content_hash,
        source_directive=(
            PublicationBlockingDirective.LAYOUT_REVISION_NOT_SUCCESSFUL
        ),
        source_artifact_id=layout_compilation.record_id,
        source_artifact_version=layout_compilation.latex_version_id,
        source_artifact_content_hash=layout_compilation.pdf_sha256,
    )
    _deferred_run(
        plan=layout_plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.RESUME_PUBLICATION,
        reason=(
            PreparedResumePublicationStopReason
            .REVISION_RUN_NOT_SUCCESSFUL
        ),
        reason_version=(
            PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION
        ),
        result_id=layout_lineage.publication_result_id,
        result_hash=layout_lineage.lineage_content_hash,
        outputs=layout_lineage.output_references(),
    )
    projector = _target_provider(
        home,
        compilations=(compilation, layout_compilation),
        layouts=(layout,),
    )
    queue = _queue(home, _MissingFindingProvider(), projector)
    targets = {
        item.application_plan_id: projector.repository.get(
            subject_id=item.subject_id,
            target_id=item.correction_target_ref.target_id,
        ).target
        for item in queue.items
    }
    cover_target = targets[cover_plan.plan_id]
    assert isinstance(
        cover_target.payload, CoverLetterLayoutCorrectionTarget
    )
    assert (
        cover_target.payload.overflow_evaluation_id
        == overflow.source_result_id
    )
    layout_target = targets[layout_plan.plan_id]
    assert isinstance(
        layout_target.payload, ResumeVisualLayoutCorrectionTarget
    )
    assert layout_target.payload.final_attempt_id.endswith(":attempt:2")
    assert layout_target.payload.attempt_limit == 2
    visual_item = next(
        item
        for item in queue.items
        if item.application_plan_id == plan.plan_id
    )
    projector.current_item_reader = lambda _subject, _item: visual_item
    assert (
        projector.get_current_material_correction_target(
            subject_id=plan.subject_id,
            attention_item_id=visual_item.item_id,
        ).status
        is MaterialCorrectionTargetStatus.PREVIEW_UNAVAILABLE
    )


def test_compilation_target_binds_stopped_source_and_drift_fails_closed(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-compilation-target")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    parts = _setup(
        tmp_path / "compile",
        subject_id=plan.subject_id,
        source=LATEX.replace(
            "\\end{document}",
            "\\input{extra.tex}\n\\end{document}",
        ),
    )
    raw = replace(
        _compile(
            parts,
            _FakeCompiler(),
            subject_id=plan.subject_id,
        ),
        source_application_plan_id=plan.plan_id,
    )
    stopped_repository = (
        PrivateHomeResumeCompilationStoppedSourceRepository(home)
    )
    recorder = _Recorder()

    def stopped(request):
        return resume_compilation_public_result(
            raw,
            preparation_invocation_binding=(
                request.preparation_invocation_binding
            ),
            application_plan_id=plan.plan_id,
            stopped_source_repository=stopped_repository,
        )

    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=_recipe(
            recorder,
            input_binding="compilation-target",
            overrides={
                ApplicationPreparationStage.RESUME_COMPILATION: stopped
            },
        ),
        now=NOW,
    )
    projector = _target_provider(
        home,
        stopped_provider=(
            RepositoryResumeCompilationStoppedSourceProvider(
                stopped_repository
            )
        ),
    )
    queue = _queue(home, _MissingFindingProvider(), projector)
    item = queue.items[0]
    target = projector.repository.get(
        subject_id=plan.subject_id,
        target_id=item.correction_target_ref.target_id,
    ).target
    assert isinstance(target.payload, LatexCompilationCorrectionTarget)
    assert (
        target.payload.construction_result_id
        == raw.source_construction_record_id
    )
    assert target.payload.source_content_hash == raw.source_latex_sha256

    projector.current_item_reader = lambda _subject, _item: item
    persisted_repository = projector.repository

    class _MissingTargetRepository:
        def get(self, *, subject_id, target_id):
            return SimpleNamespace(
                status=MaterialCorrectionTargetReadStatus.NOT_FOUND,
                target=None,
            )

        def save(self, target):
            return persisted_repository.save(target)

    projector.repository = _MissingTargetRepository()
    public = projector.get_current_material_correction_target(
        subject_id=plan.subject_id, attention_item_id=item.item_id
    )
    assert public.status is MaterialCorrectionTargetStatus.TARGET_STALE


def test_current_safe_result_hides_internal_identity_and_preview_absence(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-safe-correction-target")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    qa_id, qa_hash = "resume-qa-safe", "b" * 64
    from core.application_preparation_orchestrator import (
        RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        ResumeFactQAStopReason,
    )

    _deferred_run(
        plan=plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.RESUME_FACT_QA,
        reason=ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        reason_version=RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        result_id=qa_id,
        result_hash=qa_hash,
    )
    finding_set = _finding_set(
        plan,
        FactQAMaterialKind.RESUME,
        qa_id,
        qa_hash,
        names=("safe",),
    )
    findings = _FindingProvider(
        {
            (
                plan.subject_id,
                qa_id,
                FactQAMaterialKind.RESUME,
            ): finding_set
        }
    )
    projector = _target_provider(home, finding_provider=findings)
    queue = _queue(home, findings, projector)
    item = queue.items[0]
    projector.current_item_reader = (
        lambda subject, item_id: (
            item
            if subject == item.subject_id and item_id == item.item_id
            else None
        )
    )

    result = projector.get_current_material_correction_target(
        subject_id=plan.subject_id, attention_item_id=item.item_id
    )

    assert result.status is MaterialCorrectionTargetStatus.AVAILABLE
    safe = result.safe_target
    assert safe.summary == "Synthetic unsupported claim safe."
    serialized = repr(safe).casefold()
    assert "/private/" not in serialized
    assert "stderr" not in serialized
    assert "credential" not in serialized
    assert "permit" not in serialized
    assert "target_hash" not in serialized

    incomplete_item = _queue(home, findings, None).items[0]
    projector.current_item_reader = (
        lambda _subject, _item: incomplete_item
    )
    assert (
        projector.get_current_material_correction_target(
            subject_id=plan.subject_id,
            attention_item_id=incomplete_item.item_id,
        ).status
        is MaterialCorrectionTargetStatus.TARGET_INCOMPLETE
    )
    projector.current_item_reader = (
        lambda subject, item_id: (
            item
            if subject == item.subject_id and item_id == item.item_id
            else None
        )
    )

    ui = asyncio.run(
        MaterialCorrectionTargetUIController(
            target_reader=(
                projector.get_current_material_correction_target
            )
        ).get(
            context=AuthenticatedSubjectContext(
                session_id="session_reference_0123456789abcdef",
                subject_id=plan.subject_id,
                authentication_method=(
                    AuthenticationMethod.LOCAL_KEYCHAIN_SESSION
                ),
                issued_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=10),
            ),
            attention_item_id=item.item_id,
        )
    ).to_dict()
    assert ui["status"] == "AVAILABLE"
    assert set(ui["target"]) == {
        "attempt_count",
        "attempt_limit",
        "material_kind",
        "preview_reference",
        "required_action",
        "summary",
        "target_id",
        "target_kind",
        "title",
    }
