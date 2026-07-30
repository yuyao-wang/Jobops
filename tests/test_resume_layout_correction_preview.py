"""Focused P2b5e1 safe Resume layout preview tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from core.application_preparation_orchestrator import (
    PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    PreparedResumePublicationStopReason,
    PreparationStageOutcome,
    PrivateHomeApplicationPreparationRunRepository,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.material_correction_target import (
    PrivateHomeMaterialCorrectionTargetRepository,
)
from core.pdf_page_renderer import (
    PdfRendererDescription,
    RenderedPage,
)
from core.private_home import PrivateHome
from core.publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    create_publication_stopped_source_lineage,
)
from core.resume_layout_correction_preview import (
    PrivateHomeResumeLayoutCorrectionPreviewRepository,
    ResumeCompilationArtifact,
    ResumeCompilationArtifactResult,
    ResumeCompilationArtifactStatus,
    ResumeLayoutCorrectionPreviewProvider,
    ResumeLayoutCorrectionPreviewStatus,
)
from dashboard.resume_layout_correction_preview import (
    ResumeLayoutCorrectionPreviewUIController,
)
from dashboard.server import (
    app,
    resume_layout_correction_preview_page_ui,
    resume_layout_correction_preview_ui,
)
from starlette.requests import Request
from tests.test_human_attention_queue import NOW, _plan
from tests.test_material_correction_target import (
    _MissingFindingProvider,
    _deferred_run,
    _queue,
    _target_provider,
)


PNG = b"\x89PNG\r\n\x1a\nsynthetic-preview"


class _ArtifactProvider:
    def __init__(self, artifact):
        self.artifact = artifact

    def get(self, *, subject_id, compilation_record_id):
        if (
            self.artifact is None
            or self.artifact.subject_id != subject_id
            or self.artifact.record_id != compilation_record_id
        ):
            return ResumeCompilationArtifactResult(
                ResumeCompilationArtifactStatus.NOT_FOUND, None
            )
        return ResumeCompilationArtifactResult(
            ResumeCompilationArtifactStatus.AVAILABLE, self.artifact
        )


class _Renderer:
    def __init__(self, *, unsafe=False, unavailable=False):
        self.unsafe = unsafe
        self.unavailable = unavailable
        self.calls = 0

    def describe(self):
        if self.unavailable:
            from core.pdf_page_renderer import PdfRendererUnavailableError

            raise PdfRendererUnavailableError("synthetic")
        return PdfRendererDescription("synthetic", "1", 150, "PNG")

    def render(self, pdf_bytes):
        self.calls += 1
        return (
            RenderedPage(
                1,
                100,
                200,
                "SVG" if self.unsafe else "PNG",
                PNG,
            ),
        )


def _visual_target(tmp_path):
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-layout-preview")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    compilation = SimpleNamespace(
        record_id="resume-compilation-" + "1" * 64,
        subject_id=plan.subject_id,
        contract_version="resume-compilation-v1",
        latex_version_id="resume-latex-version-" + "2" * 64,
        latex_source_sha256="3" * 64,
        pdf_sha256="4" * 64,
    )
    lineage = create_publication_stopped_source_lineage(
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
        result_id=lineage.publication_result_id,
        result_hash=lineage.lineage_content_hash,
        outputs=lineage.output_references(),
    )
    target_provider = _target_provider(
        home, compilations=(compilation,)
    )
    queue = _queue(home, _MissingFindingProvider(), target_provider)
    item = queue.items[0]
    target_provider.current_item_reader = (
        lambda subject, item_id: item
        if subject == item.subject_id and item_id == item.item_id
        else None
    )
    target = target_provider.repository.get(
        subject_id=item.subject_id,
        target_id=item.correction_target_ref.target_id,
    ).target
    artifact = ResumeCompilationArtifact(
        subject_id=item.subject_id,
        record_id=compilation.record_id,
        record_version=compilation.contract_version,
        latex_version_id=compilation.latex_version_id,
        pdf_hash=compilation.pdf_sha256,
        page_count=1,
        content=b"%PDF-synthetic",
    )
    return home, item, target, target_provider, artifact


def _preview_provider(
    home, item, target_provider, artifact, renderer
):
    return ResumeLayoutCorrectionPreviewProvider(
        target_repository=target_provider.repository,
        target_provider=target_provider,
        current_item_reader=target_provider.current_item_reader,
        artifact_provider=_ArtifactProvider(artifact),
        renderer=renderer,
        repository=PrivateHomeResumeLayoutCorrectionPreviewRepository(home),
        clock=lambda: NOW,
    )


def test_exact_visual_target_creates_and_reuses_immutable_preview(
    tmp_path,
) -> None:
    home, item, target, target_provider, artifact = _visual_target(tmp_path)
    renderer = _Renderer()
    provider = _preview_provider(
        home, item, target_provider, artifact, renderer
    )
    created = provider.get_or_create_resume_layout_correction_preview(
        subject_id=item.subject_id,
        correction_target_ref=target.reference,
    )
    replay = provider.get_or_create_resume_layout_correction_preview(
        subject_id=item.subject_id,
        correction_target_ref=target.reference,
    )
    assert created.status is ResumeLayoutCorrectionPreviewStatus.AVAILABLE
    assert replay.status is ResumeLayoutCorrectionPreviewStatus.UNCHANGED
    assert replay.preview_ref == created.preview_ref
    assert renderer.calls == 1
    assert provider.read_current_preview_page(
        subject_id=item.subject_id,
        preview_ref=created.preview_ref,
        page_number=1,
    ) == PNG


def test_stale_cross_subject_renderer_and_unsafe_output_fail_closed(
    tmp_path,
) -> None:
    home, item, target, target_provider, artifact = _visual_target(tmp_path)
    stale = replace(artifact, pdf_hash="9" * 64)
    assert (
        _preview_provider(
            home, item, target_provider, stale, _Renderer()
        ).get_or_create_resume_layout_correction_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        ).status
        is ResumeLayoutCorrectionPreviewStatus.TARGET_STALE
    )
    assert (
        _preview_provider(
            home, item, target_provider, artifact, _Renderer(unsafe=True)
        ).get_or_create_resume_layout_correction_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        ).status
        is ResumeLayoutCorrectionPreviewStatus.PREVIEW_UNSAFE
    )
    assert (
        _preview_provider(
            home, item, target_provider, artifact, _Renderer(unavailable=True)
        ).get_or_create_resume_layout_correction_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        ).status
        is ResumeLayoutCorrectionPreviewStatus.RENDERER_UNAVAILABLE
    )
    assert (
        _preview_provider(
            home, item, target_provider, artifact, _Renderer()
        ).get_or_create_resume_layout_correction_preview(
            subject_id="subject-other",
            correction_target_ref=target.reference,
        ).status
        is ResumeLayoutCorrectionPreviewStatus.TARGET_STALE
    )


def test_authenticated_ui_returns_opaque_safe_reference_and_png_only(
    tmp_path,
) -> None:
    home, item, target, target_provider, artifact = _visual_target(tmp_path)
    provider = _preview_provider(
        home, item, target_provider, artifact, _Renderer()
    )
    controller = ResumeLayoutCorrectionPreviewUIController(
        target_repository=target_provider.repository,
        preview_provider=provider,
    )
    context = AuthenticatedSubjectContext(
        session_id="session_reference_preview_0123456789",
        subject_id=item.subject_id,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW,
        expires_at=NOW.replace(year=NOW.year + 1),
    )
    app.state.resume_layout_correction_preview_controller = controller
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/resume-layout-correction-previews",
            "headers": [],
            "app": app,
        }
    )
    payload = asyncio.run(
        resume_layout_correction_preview_ui(
            target.target_id, request, context
        )
    )
    assert payload["status"] == "AVAILABLE"
    assert set(payload["preview"]) == {
        "media_type",
        "origin_kind",
        "page_count",
        "preview_reference",
    }
    opaque = payload["preview"]["preview_reference"]
    assert "/" not in opaque
    response = asyncio.run(
        resume_layout_correction_preview_page_ui(
            opaque, 1, request, context
        )
    )
    assert response.body == PNG
    assert response.media_type == "image/png"
    assert "path" not in payload["preview"]
    assert "hash" not in payload["preview"]
