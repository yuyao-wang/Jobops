"""Focused P2b5e2 Cover Letter overflow safe-preview tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from core.application_preparation_orchestrator import (
    COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    CoverLetterPublicationStopReason,
    PreparationStageOutcome,
    PrivateHomeApplicationPreparationRunRepository,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.cover_letter_overflow_preview import (
    CoverLetterOverflowPreviewProvider,
    CoverLetterOverflowPreviewStatus,
    CoverLetterOverflowSource,
    CoverLetterOverflowSourceResult,
    CoverLetterOverflowSourceStatus,
    PrivateHomeCoverLetterOverflowPreviewRepository,
    PrivateHomeCoverLetterOverflowSourceProvider,
)
from core.latex_compiler import (
    LATEX_COMPILE_POLICY_VERSION,
    LATEX_SANDBOX_POLICY_VERSION,
    LatexCompileOutcome,
    LatexCompileStatus,
    LatexCompilerDescription,
)
from core.pdf_page_renderer import (
    PdfRendererDescription,
    PdfRendererUnavailableError,
    RenderedPage,
)
from core.prepared_cover_letter_material import (
    COVER_LETTER_PUBLICATION_POLICY_VERSION,
    MANAGED_COVER_LETTER_TEMPLATE_ID,
    PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION,
    cover_letter_source_reference,
)
from core.private_home import PrivateHome
from core.publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    create_publication_stopped_source_lineage,
)
from dashboard.cover_letter_overflow_preview import (
    CoverLetterOverflowPreviewUIController,
)
from dashboard.server import (
    app,
    cover_letter_overflow_preview_page_ui,
    cover_letter_overflow_preview_ui,
)
from starlette.requests import Request
from tests.test_human_attention_queue import NOW, _plan
from tests.test_material_correction_target import (
    _MissingFindingProvider,
    _deferred_run,
    _queue,
    _target_provider,
)


PNG = b"\x89PNG\r\n\x1a\ncover-letter-preview"
PDF = b"%PDF-synthetic-cover-letter-overflow"
SOURCE = "\\documentclass{article}\\begin{document}Synthetic\\end{document}"
SOURCE_HASH = hashlib.sha256(SOURCE.encode()).hexdigest()


def _canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class _Compiler:
    def __init__(self, *, version="synthetic-compiler-1"):
        self.description = LatexCompilerDescription(
            engine="pdflatex",
            compiler_version=version,
            normalized_flags=("-no-shell-escape",),
            compile_policy_version=LATEX_COMPILE_POLICY_VERSION,
            sandbox_policy_version=LATEX_SANDBOX_POLICY_VERSION,
        )
        self.calls = 0

    def describe(self):
        return self.description

    def compile(self, request):
        self.calls += 1
        assert request.latex_source == SOURCE
        return LatexCompileOutcome(
            LatexCompileStatus.SUCCEEDED,
            PDF,
            "",
            0,
            True,
        )


class _Renderer:
    def __init__(self, *, unsafe=False, unavailable=False):
        self.unsafe = unsafe
        self.unavailable = unavailable
        self.calls = 0

    def describe(self):
        if self.unavailable:
            raise PdfRendererUnavailableError("synthetic")
        return PdfRendererDescription("synthetic-renderer", "1", 150, "PNG")

    def render(self, pdf_bytes):
        assert pdf_bytes == PDF
        self.calls += 1
        return tuple(
            RenderedPage(
                page,
                100,
                200,
                "SVG" if self.unsafe else "PNG",
                PNG + bytes([page]),
            )
            for page in (1, 2)
        )


class _SourceProvider:
    def __init__(self, source, *, status=CoverLetterOverflowSourceStatus.AVAILABLE):
        self.source = source
        self.status = status

    def get(self, **kwargs):
        if (
            self.status is not CoverLetterOverflowSourceStatus.AVAILABLE
            or kwargs["subject_id"] != self.source.subject_id
            or kwargs["source_record_id"] != self.source.source_record_id
            or kwargs["source_version"] != self.source.source_version
            or kwargs["source_content_hash"]
            != self.source.source_content_hash
        ):
            return CoverLetterOverflowSourceResult(self.status, None)
        return CoverLetterOverflowSourceResult(self.status, self.source)


def _overflow_target(tmp_path):
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-cover-letter-overflow-preview")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    compiler = _Compiler()
    overflow_hash = _canonical_hash(
        {
            "application_plan_id": plan.plan_id,
            "compiler_engine": compiler.description.engine,
            "compiler_version": compiler.description.compiler_version,
            "page_count": 2,
            "policy_version": COVER_LETTER_PUBLICATION_POLICY_VERSION,
            "source_sha256": SOURCE_HASH,
            "subject_id": plan.subject_id,
            "template_id": MANAGED_COVER_LETTER_TEMPLATE_ID,
            "template_version": "1",
        }
    )
    lineage = create_publication_stopped_source_lineage(
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        publication_stage=(
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION
        ),
        material_kind=PublicationMaterialKind.COVER_LETTER,
        source_kind=(
            PublicationStoppedSourceKind.COVER_LETTER_LAYOUT_OVERFLOW
        ),
        source_stage=ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        source_result_id=(
            f"cover-letter-overflow-evaluation-{overflow_hash}"
        ),
        source_outcome=PreparationStageOutcome.DEFERRED,
        source_contract_version=(
            PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION
        ),
        source_result_content_hash=overflow_hash,
        source_directive=(
            PublicationBlockingDirective.COVER_LETTER_LAYOUT_OVERFLOW
        ),
        source_artifact_id=f"cover-letter-latex-source-{SOURCE_HASH}",
        source_artifact_version="1",
        source_artifact_content_hash=SOURCE_HASH,
    )
    _deferred_run(
        plan=plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        reason=CoverLetterPublicationStopReason.LAYOUT_OVERFLOW,
        reason_version=(
            COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION
        ),
        result_id=lineage.publication_result_id,
        result_hash=lineage.lineage_content_hash,
        outputs=lineage.output_references(),
    )
    target_provider = _target_provider(home)
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
    source = CoverLetterOverflowSource(
        subject_id=item.subject_id,
        source_record_id=f"cover-letter-latex-source-{SOURCE_HASH}",
        source_version="1",
        source_content_hash=SOURCE_HASH,
        latex_source=SOURCE,
    )
    home.ensure()
    home.write_bytes_if_absent(
        cover_letter_source_reference(
            subject_id=item.subject_id, source_sha256=SOURCE_HASH
        ),
        SOURCE.encode(),
    )
    return home, item, target, target_provider, source, compiler


def _provider(
    home,
    item,
    target_provider,
    source,
    compiler,
    renderer,
    *,
    source_status=CoverLetterOverflowSourceStatus.AVAILABLE,
    source_provider=None,
):
    return CoverLetterOverflowPreviewProvider(
        target_repository=target_provider.repository,
        target_provider=target_provider,
        current_item_reader=target_provider.current_item_reader,
        source_provider=(
            source_provider
            or _SourceProvider(source, status=source_status)
        ),
        compiler=compiler,
        renderer=renderer,
        repository=PrivateHomeCoverLetterOverflowPreviewRepository(home),
        clock=lambda: NOW,
        pdf_inspector=lambda _pdf: (2, "synthetic"),
    )


def test_exact_overflow_evaluation_creates_and_reuses_immutable_preview(
    tmp_path,
) -> None:
    home, item, target, target_provider, source, compiler = _overflow_target(
        tmp_path
    )
    renderer = _Renderer()
    provider = _provider(
        home,
        item,
        target_provider,
        source,
        compiler,
        renderer,
        source_provider=PrivateHomeCoverLetterOverflowSourceProvider(home),
    )
    created = provider.get_or_create_cover_letter_overflow_preview(
        subject_id=item.subject_id,
        correction_target_ref=target.reference,
    )
    replay = provider.get_or_create_cover_letter_overflow_preview(
        subject_id=item.subject_id,
        correction_target_ref=target.reference,
    )
    assert created.status is CoverLetterOverflowPreviewStatus.AVAILABLE
    assert replay.status is CoverLetterOverflowPreviewStatus.UNCHANGED
    assert replay.preview_ref == created.preview_ref
    assert compiler.calls == renderer.calls == 1
    assert created.page_count == 2
    assert provider.read_current_preview_page(
        subject_id=item.subject_id,
        preview_ref=created.preview_ref,
        page_number=2,
    ) == PNG + b"\x02"
    record = provider.repository.get(
        subject_id=item.subject_id,
        preview_id=created.preview_ref.preview_id,
    ).preview
    assert record.overflow_evaluation_id == (
        target.payload.overflow_evaluation_id
    )
    assert record.source_record_id == target.payload.latex_source_id
    assert record.compiled_artifact_id.startswith(
        "cover-letter-overflow-pdf-"
    )


def test_stale_cross_subject_source_renderer_and_unsafe_media_fail_closed(
    tmp_path,
) -> None:
    home, item, target, target_provider, source, compiler = _overflow_target(
        tmp_path
    )
    assert (
        _provider(
            home,
            item,
            target_provider,
            source,
            _Compiler(version="drifted"),
            _Renderer(),
        ).get_or_create_cover_letter_overflow_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        ).status
        is CoverLetterOverflowPreviewStatus.TARGET_STALE
    )
    assert (
        _provider(
            home,
            item,
            target_provider,
            source,
            compiler,
            _Renderer(unsafe=True),
        ).get_or_create_cover_letter_overflow_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        ).status
        is CoverLetterOverflowPreviewStatus.PREVIEW_UNSAFE
    )
    assert (
        _provider(
            home,
            item,
            target_provider,
            source,
            compiler,
            _Renderer(unavailable=True),
        ).get_or_create_cover_letter_overflow_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        ).status
        is CoverLetterOverflowPreviewStatus.RENDERER_UNAVAILABLE
    )
    assert (
        _provider(
            home,
            item,
            target_provider,
            source,
            compiler,
            _Renderer(),
            source_status=CoverLetterOverflowSourceStatus.NOT_FOUND,
        ).get_or_create_cover_letter_overflow_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        ).status
        is CoverLetterOverflowPreviewStatus.SOURCE_UNAVAILABLE
    )
    assert (
        _provider(
            home, item, target_provider, source, compiler, _Renderer()
        ).get_or_create_cover_letter_overflow_preview(
            subject_id="subject-other",
            correction_target_ref=target.reference,
        ).status
        is CoverLetterOverflowPreviewStatus.TARGET_STALE
    )
    integrity_provider = _provider(
        home, item, target_provider, source, compiler, _Renderer()
    )
    created = integrity_provider.get_or_create_cover_letter_overflow_preview(
        subject_id=item.subject_id,
        correction_target_ref=target.reference,
    )
    page = (
        integrity_provider.repository._directory(
            item.subject_id, created.preview_ref.preview_id
        )
        / "page-1.png"
    )
    page.write_bytes(b"corrupted")
    assert (
        integrity_provider.get_or_create_cover_letter_overflow_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        ).status
        is CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE
    )


def test_authenticated_route_returns_only_opaque_png_preview(
    tmp_path,
) -> None:
    home, item, target, target_provider, source, compiler = _overflow_target(
        tmp_path
    )
    provider = _provider(
        home, item, target_provider, source, compiler, _Renderer()
    )
    controller = CoverLetterOverflowPreviewUIController(
        target_repository=target_provider.repository,
        preview_provider=provider,
    )
    context = AuthenticatedSubjectContext(
        session_id="session_cover_letter_preview_0123456789",
        subject_id=item.subject_id,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW,
        expires_at=NOW.replace(year=NOW.year + 1),
    )
    app.state.cover_letter_overflow_preview_controller = controller
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/cover-letter-overflow-previews",
            "headers": [],
            "app": app,
        }
    )
    payload = asyncio.run(
        cover_letter_overflow_preview_ui(
            target.target_id, request, context
        )
    )
    assert payload["status"] == "AVAILABLE"
    assert set(payload["preview"]) == {
        "media_type",
        "page_count",
        "preview_reference",
    }
    opaque = payload["preview"]["preview_reference"]
    assert "/" not in opaque
    response = asyncio.run(
        cover_letter_overflow_preview_page_ui(
            opaque, 1, request, context
        )
    )
    assert response.body == PNG + b"\x01"
    assert response.media_type == "image/png"
    rendered = json.dumps(payload)
    assert "path" not in rendered
    assert "hash" not in rendered
    assert "stderr" not in rendered
    template = (
        Path(__file__).parents[1] / "dashboard" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert "查看当前 Cover Letter" in template
    assert "预览不代表溢出已解决" in template
