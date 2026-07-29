from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import core.resume_visual_qa as visual_qa_module
from core.latex_compiler import (
    LATEX_COMPILE_POLICY_VERSION,
    LATEX_SANDBOX_POLICY_VERSION,
    LatexCompileOutcome,
    LatexCompileRequest,
    LatexCompileStatus,
    LatexCompilerDescription,
    normalized_compile_flags,
)
from core.pdf_page_renderer import (
    DEFAULT_RENDER_DPI,
    RENDER_IMAGE_FORMAT,
    PdfiumPageRenderer,
    PdfRendererDescription,
    PdfRendererUnavailableError,
    RenderedPage,
)
from core.private_home import PrivateHome
from core.resume_compilation import (
    CompileResumeLatexCommand,
    PrivateHomeResumeCompilationRepository,
    ResumeCompilationStatus,
    compile_resume_latex,
)
from core.resume_latex_construction import (
    PrivateHomeResumeLatexConstructionRecordRepository,
    RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION,
    ResumeLatexConstructionMethod,
    ResumeLatexConstructionPath,
    ResumeLatexConstructionRecord,
)
from core.resume_latex_markers import (
    JOBOPS_CONTENT_BEGIN,
    JOBOPS_CONTENT_END,
    MARKER_MACRO_DEFINITIONS,
)
from core.resume_latex_versions import (
    PrivateHomeResumeLatexVersionRepository,
    RegisterResumeLatexVersionCommand,
    ResumeLatexSourceKind,
    register_resume_latex_version,
)
from core.resume_visual_qa import (
    RESUME_VISUAL_QA_AGENT_POLICY,
    RESUME_VISUAL_QA_CONTRACT_VERSION,
    RESUME_VISUAL_QA_POLICY_VERSION,
    PrivateHomeResumeVisualQARepository,
    ResumeVisualQAAgentFinding,
    ResumeVisualQAAgentMetadata,
    ResumeVisualQAAgentOutput,
    ResumeVisualQAAgentUnavailableError,
    ResumeVisualQAAgentVerdict,
    ResumeVisualQAContext,
    ResumeVisualQAFailureReason,
    ResumeVisualQAFindingSeverity,
    ResumeVisualQAFindingSource,
    ResumeVisualQAFindingType,
    ResumeVisualQAPolicy,
    ResumeVisualQAReadStatus,
    ResumeVisualQAStatus,
    ResumeVisualQAVerdict,
    ResumeVisualQAWriteStatus,
    ReviewResumeVisualQACommand,
    VisualBoundingBox,
    review_resume_visual_qa,
)
from core.resume_tailoring import (
    PrivateHomeTailoredResumeDraftRepository,
    RESUME_TAILORING_CONTRACT_VERSION,
    RESUME_TAILORING_POLICY_VERSION,
    TailoredBulletChangeType,
    TailoredResumeBullet,
    TailoredResumeDraft,
    TailoredResumeSection,
)


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
METADATA = ResumeVisualQAAgentMetadata(
    agent_version="resume-visual-qa-agent-v1",
    prompt_version="resume-visual-qa-prompt-v1",
    model_id="synthetic-visual-model",
)
SECTION_TITLE = "Experience"
BULLET_TEXT = "Built deterministic geospatial pipelines in Python."
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
    b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _hashed(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _latex(*, pages: int = 1, tiny: bool = False, text: str = BULLET_TEXT) -> str:
    body = [
        f"\\JobopsSection{{resume-section-a}}{{{SECTION_TITLE}}}",
        "\\begin{itemize}",
        f"\\JobopsBullet{{resume-block-a}}{{{text}}}",
        "\\end{itemize}",
    ]
    for extra in range(pages - 1):
        body.append("\\newpage")
        body.append(f"Continuation page {extra + 2} content for the resume.")
    size = "\\tiny\n" if tiny else ""
    return (
        "\\documentclass[11pt]{article}\n"
        "\\usepackage[margin=0.75in]{geometry}\n"
        "\\pagestyle{empty}\n"
        f"{MARKER_MACRO_DEFINITIONS}"
        "\\begin{document}\n"
        f"{size}"
        f"{JOBOPS_CONTENT_BEGIN}\n"
        + "\n".join(body)
        + f"\n{JOBOPS_CONTENT_END}\n"
        "\\end{document}\n"
    )


class _FakePdfCompiler:
    """Produces a deterministic synthetic PDF carrying the Draft text."""

    def __init__(self, *, pages: int = 1, font_size: float = 11.0,
                 text: str = BULLET_TEXT, title: str = SECTION_TITLE,
                 clipped: bool = False, blank_last: bool = False) -> None:
        self.pages = pages
        self.font_size = font_size
        self.text = text
        self.title = title
        self.clipped = clipped
        self.blank_last = blank_last

    def describe(self) -> LatexCompilerDescription:
        return LatexCompilerDescription(
            engine="pdflatex",
            compiler_version="pdfTeX 3.141592653 (synthetic)",
            normalized_flags=normalized_compile_flags(),
            compile_policy_version=LATEX_COMPILE_POLICY_VERSION,
            sandbox_policy_version=LATEX_SANDBOX_POLICY_VERSION,
        )

    def compile(self, request: LatexCompileRequest) -> LatexCompileOutcome:
        return LatexCompileOutcome(
            status=LatexCompileStatus.SUCCEEDED,
            pdf_bytes=synthetic_pdf(
                pages=self.pages,
                font_size=self.font_size,
                text=self.text,
                title=self.title,
                clipped=self.clipped,
                blank_last=self.blank_last,
            ),
            diagnostics="",
            exit_code=0,
            compiler_started=True,
        )


def synthetic_pdf(
    *,
    pages: int = 1,
    font_size: float = 11.0,
    text: str = BULLET_TEXT,
    title: str = SECTION_TITLE,
    clipped: bool = False,
    blank_last: bool = False,
) -> bytes:
    """A real, parseable PDF whose text and glyph boxes drive the checks."""

    width, height = 612, 792
    objects: list[bytes] = []

    def escape(value: str) -> str:
        return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    kids = " ".join(f"{4 + index * 2} 0 R" for index in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode("ascii")
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )
    for index in range(pages):
        is_blank = blank_last and index == pages - 1
        if is_blank:
            stream = b""
        else:
            x = -60 if clipped else 72
            lines = [
                f"BT /F1 {font_size} Tf {x} {height - 100} Td "
                f"({escape(title)}) Tj ET",
                f"BT /F1 {font_size} Tf {x} {height - 130} Td "
                f"({escape(text)}) Tj ET",
            ]
            if index > 0:
                lines.append(
                    f"BT /F1 {font_size} Tf 72 {height - 160} Td "
                    f"(Continuation page {index + 1} content for the resume.) Tj ET"
                )
            stream = "\n".join(lines).encode("ascii")
        content_index = 5 + index * 2
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_index} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


class _FakeRenderer:
    def __init__(
        self,
        *,
        pages: int = 1,
        describe_error: Exception | None = None,
        render_error: Exception | None = None,
        out_of_order: bool = False,
    ) -> None:
        self.pages = pages
        self.describe_error = describe_error
        self.render_error = render_error
        self.out_of_order = out_of_order
        self.render_calls = 0
        self.describe_calls = 0

    def describe(self) -> PdfRendererDescription:
        self.describe_calls += 1
        if self.describe_error is not None:
            raise self.describe_error
        return PdfRendererDescription(
            renderer_name="fake-renderer",
            renderer_version="1.0.0",
            dpi=DEFAULT_RENDER_DPI,
            image_format=RENDER_IMAGE_FORMAT,
        )

    def render(self, pdf_bytes: bytes) -> tuple[RenderedPage, ...]:
        self.render_calls += 1
        if self.render_error is not None:
            raise self.render_error
        numbers = list(range(1, self.pages + 1))
        if self.out_of_order:
            numbers.reverse()
        return tuple(
            RenderedPage(
                page_number=number,
                width_px=1275,
                height_px=1650,
                image_format=RENDER_IMAGE_FORMAT,
                image_bytes=PNG_BYTES,
            )
            for number in numbers
        )


class _FakeVisualAgent:
    def __init__(self, output=None) -> None:
        self.output = output
        self.contexts: list[ResumeVisualQAContext] = []

    async def review(self, context: ResumeVisualQAContext):
        self.contexts.append(context)
        if isinstance(self.output, Exception):
            raise self.output
        if callable(self.output):
            return self.output(context)
        if self.output is None:
            return ResumeVisualQAAgentOutput(
                verdict=ResumeVisualQAAgentVerdict.CLEAN,
                findings=(),
            )
        return self.output


def _setup(
    tmp_path: Path,
    *,
    subject_id: str = "subject-a",
    compiler: _FakePdfCompiler | None = None,
    latex: str | None = None,
):
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    source = latex or _latex()
    latex_repository = PrivateHomeResumeLatexVersionRepository(home)

    draft_section = TailoredResumeSection(
        order=0,
        source_section_id="resume-section-a",
        title=SECTION_TITLE,
        bullets=(
            TailoredResumeBullet(
                order=0,
                change_type=TailoredBulletChangeType.REWRITTEN,
                text=BULLET_TEXT,
                source_section_id="resume-section-a",
                source_block_id="resume-block-a",
                source_bullet_id=None,
                evidence_ids=("candidate-evidence-" + "1" * 64,),
                jd_alignment=("geospatial data pipelines",),
            ),
        ),
    )
    draft_binding = _hashed({"draft": subject_id})
    draft_content = {
        "draft_id": f"tailored-resume-draft-{draft_binding}",
        "contract_version": RESUME_TAILORING_CONTRACT_VERSION,
        "tailoring_binding": draft_binding,
        "subject_id": subject_id,
        "application_plan_id": "application-plan-" + "2" * 64,
        "job_id": "job-one",
        "job_revision": 1,
        "job_content_hash": "3" * 64,
        "resume_selection_decision_id": "resume-selection-" + "4" * 64,
        "source_resume_id": "resume-candidate-" + "5" * 64,
        "source_artifact_sha256": "6" * 64,
        "source_projection_id": "source-resume-projection-" + "7" * 64,
        "source_projection_hash": "8" * 64,
        "evidence_snapshot_id": "candidate-evidence-snapshot-" + "9" * 64,
        "evidence_snapshot_hash": "a" * 64,
        "user_preparation_instructions_hash": "b" * 64,
        "agent_version": "resume-tailoring-agent-v1",
        "prompt_version": "resume-tailoring-prompt-v1",
        "model_id": "synthetic-tailoring-model",
        "agent_policy_version": RESUME_TAILORING_POLICY_VERSION,
        "rationale": "Synthetic draft for visual QA coverage.",
        "sections": [draft_section.to_dict()],
    }
    draft = TailoredResumeDraft(
        draft_content_hash=_hashed(draft_content),
        created_at=NOW,
        sections=(draft_section,),
        **{
            key: value
            for key, value in draft_content.items()
            if key != "sections"
        },
    )
    draft_repository = PrivateHomeTailoredResumeDraftRepository(home)
    assert draft_repository.save(draft).draft is not None

    version = register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id=subject_id,
            source_kind=ResumeLatexSourceKind.SYSTEM_TEMPLATE_DERIVED,
            now=NOW,
            latex_source=source,
            template_id="managed-resume-one-page-v1",
            template_sha256="c" * 64,
            tailored_resume_draft_id=draft.draft_id,
            tailored_resume_draft_hash=draft.draft_content_hash,
            fact_qa_result_id="resume-fact-qa-" + "d" * 64,
            fact_qa_result_hash="e" * 64,
        ),
        home=home,
        repository=latex_repository,
    ).version
    assert version is not None

    construction_binding = _hashed({"construction": version.latex_version_id})
    construction = ResumeLatexConstructionRecord(
        record_id=f"resume-latex-construction-{construction_binding}",
        contract_version=RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION,
        construction_binding=construction_binding,
        subject_id=subject_id,
        application_plan_id=draft.application_plan_id,
        tailored_resume_draft_id=draft.draft_id,
        tailored_resume_draft_hash=draft.draft_content_hash,
        fact_qa_result_id=version.fact_qa_result_id,
        fact_qa_result_hash=version.fact_qa_result_hash,
        base_latex_selection_decision_id="base-latex-selection-" + "f" * 64,
        construction_path=ResumeLatexConstructionPath.MANAGED_TEMPLATE,
        construction_method=(
            ResumeLatexConstructionMethod.DETERMINISTIC_TEMPLATE_RENDER
        ),
        latex_version_id=version.latex_version_id,
        latex_source_sha256=version.source_sha256,
        root_family_id=version.root_family_id,
        parent_version_id=None,
        template_id=version.template_id,
        template_sha256=version.template_sha256,
        agent_invoked=False,
        agent_version="resume-latex-construction-agent-v1",
        prompt_version="resume-latex-construction-prompt-v1",
        model_id="synthetic-construction-model",
        constructed_at=NOW,
    )
    construction_repository = (
        PrivateHomeResumeLatexConstructionRecordRepository(home)
    )
    assert construction_repository.save(construction).record is not None

    compilation_repository = PrivateHomeResumeCompilationRepository(home)
    compiled = compile_resume_latex(
        CompileResumeLatexCommand(
            subject_id=subject_id,
            resume_latex_construction_record_id=construction.record_id,
            resume_latex_version_id=version.latex_version_id,
            now=NOW,
        ),
        construction_repository=construction_repository,
        latex_version_repository=latex_repository,
        compiler=compiler or _FakePdfCompiler(),
        compilation_repository=compilation_repository,
        home=home,
    )
    assert compiled.status is ResumeCompilationStatus.CREATED, (
        compiled.reason_code,
        compiled.diagnostics,
    )

    return {
        "home": home,
        "draft": draft,
        "draft_repository": draft_repository,
        "version": version,
        "latex_repository": latex_repository,
        "construction": construction,
        "construction_repository": construction_repository,
        "compilation": compiled.record,
        "compilation_repository": compilation_repository,
        "visual_qa_repository": PrivateHomeResumeVisualQARepository(home),
    }


async def _review(
    parts,
    renderer=None,
    agent=None,
    *,
    subject_id: str = "subject-a",
    policy: ResumeVisualQAPolicy | None = None,
    metadata: ResumeVisualQAAgentMetadata = METADATA,
    now: datetime = NOW,
):
    return await review_resume_visual_qa(
        ReviewResumeVisualQACommand(
            subject_id=subject_id,
            resume_compilation_record_id=parts["compilation"].record_id,
            now=now,
        ),
        compilation_repository=parts["compilation_repository"],
        latex_version_repository=parts["latex_repository"],
        construction_repository=parts["construction_repository"],
        draft_repository=parts["draft_repository"],
        renderer=renderer if renderer is not None else _FakeRenderer(),
        agent=agent if agent is not None else _FakeVisualAgent(),
        metadata=metadata,
        visual_qa_repository=parts["visual_qa_repository"],
        policy=policy,
        home=parts["home"],
    )


def _results(parts) -> tuple[Path, ...]:
    return tuple(parts["home"].paths.resume_visual_qa.rglob("*.json"))


@pytest.mark.asyncio
async def test_sound_single_page_pdf_passes(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    renderer = _FakeRenderer()
    agent = _FakeVisualAgent()

    result = await _review(parts, renderer, agent)

    assert result.status is ResumeVisualQAStatus.CREATED
    record = result.result
    assert record.verdict is ResumeVisualQAVerdict.PASSED
    assert record.findings == ()
    assert record.contract_version == RESUME_VISUAL_QA_CONTRACT_VERSION
    assert record.policy_version == RESUME_VISUAL_QA_POLICY_VERSION
    assert record.page_count == 1
    assert record.max_pages == 1
    assert record.pdf_sha256 == parts["compilation"].pdf_sha256
    assert record.renderer_dpi == DEFAULT_RENDER_DPI
    assert record.agent_invoked is True
    assert record.validated_at == NOW
    assert renderer.render_calls == 1
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (
            "subject",
            ResumeVisualQAFailureReason.COMPILATION_RECORD_NOT_FOUND,
        ),
        (
            "pdf_hash",
            ResumeVisualQAFailureReason.PDF_HASH_DRIFT,
        ),
        (
            "draft_hash",
            ResumeVisualQAFailureReason.DRAFT_BINDING_MISMATCH,
        ),
    ],
)
async def test_binding_mismatch_fails_before_render_or_agent(
    tmp_path: Path, damage: str, reason: ResumeVisualQAFailureReason
) -> None:
    parts = _setup(tmp_path)
    renderer = _FakeRenderer()
    agent = _FakeVisualAgent()
    subject = "subject-a"
    if damage == "subject":
        subject = "subject-b"
    elif damage == "pdf_hash":
        artifact = parts["home"].contained_path(
            parts["compilation"].pdf_reference
        )
        artifact.write_bytes(b"%PDF-1.4\ntampered\n%%EOF\n")

        class _PassThroughCompilation:
            def __init__(self, record) -> None:
                self.record = record

            def get(self, **_kwargs):
                from core.resume_compilation import (
                    ResumeCompilationReadResult,
                    ResumeCompilationReadStatus,
                )

                return ResumeCompilationReadResult(
                    status=ResumeCompilationReadStatus.FOUND,
                    record=self.record,
                )

        parts["compilation_repository"] = _PassThroughCompilation(
            parts["compilation"]
        )
    else:
        object.__setattr__(
            parts["construction"], "tailored_resume_draft_hash", "0" * 64
        )

        class _PassThroughConstruction:
            def __init__(self, record) -> None:
                self.record = record

            def get(self, **_kwargs):
                from core.resume_latex_construction import (
                    ResumeLatexConstructionReadResult,
                    ResumeLatexConstructionReadStatus,
                )

                return ResumeLatexConstructionReadResult(
                    status=ResumeLatexConstructionReadStatus.FOUND,
                    record=self.record,
                )

        parts["construction_repository"] = _PassThroughConstruction(
            parts["construction"]
        )

    result = await _review(parts, renderer, agent, subject_id=subject)

    assert result.status is ResumeVisualQAStatus.FAILED
    assert result.reason_code is reason
    assert renderer.render_calls == 0
    assert agent.contexts == []
    assert not _results(parts)


@pytest.mark.asyncio
async def test_page_count_drift_from_the_record_fails_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    record = parts["compilation"]
    object.__setattr__(record, "page_count", 3)

    class _PassThrough:
        def get(self, **_kwargs):
            from core.resume_compilation import (
                ResumeCompilationReadResult,
                ResumeCompilationReadStatus,
            )

            return ResumeCompilationReadResult(
                status=ResumeCompilationReadStatus.FOUND,
                record=record,
            )

    parts["compilation_repository"] = _PassThrough()
    renderer = _FakeRenderer()

    result = await _review(parts, renderer)

    assert result.status is ResumeVisualQAStatus.FAILED
    assert (
        result.reason_code
        is ResumeVisualQAFailureReason.PDF_PAGE_COUNT_MISMATCH
    )
    assert renderer.render_calls == 0


@pytest.mark.asyncio
async def test_page_policy_violation_requires_revision_without_editing(
    tmp_path: Path,
) -> None:
    parts = _setup(
        tmp_path,
        compiler=_FakePdfCompiler(pages=2),
        latex=_latex(pages=2),
    )
    renderer = _FakeRenderer(pages=2)
    agent = _FakeVisualAgent()
    source_before = parts["home"].contained_path(
        parts["version"].source_reference
    ).read_bytes()
    pdf_before = parts["home"].contained_path(
        parts["compilation"].pdf_reference
    ).read_bytes()

    result = await _review(parts, renderer, agent)

    assert result.status is ResumeVisualQAStatus.CREATED
    assert result.result.verdict is ResumeVisualQAVerdict.REVISION_REQUIRED
    types = {item.finding_type for item in result.result.findings}
    assert ResumeVisualQAFindingType.UNEXPECTED_PAGE_COUNT in types
    assert renderer.render_calls == 0
    assert agent.contexts == []
    assert (
        parts["home"]
        .contained_path(parts["version"].source_reference)
        .read_bytes()
        == source_before
    )
    assert (
        parts["home"]
        .contained_path(parts["compilation"].pdf_reference)
        .read_bytes()
        == pdf_before
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("compiler", "finding_type"),
    [
        (
            _FakePdfCompiler(pages=2, blank_last=True),
            ResumeVisualQAFindingType.BLANK_PAGE,
        ),
        (
            _FakePdfCompiler(clipped=True),
            ResumeVisualQAFindingType.CONTENT_CLIPPED,
        ),
        (
            _FakePdfCompiler(font_size=4.0),
            ResumeVisualQAFindingType.TEXT_TOO_SMALL,
        ),
        (
            _FakePdfCompiler(text="Totally different content in the PDF."),
            ResumeVisualQAFindingType.CONTENT_MISSING,
        ),
    ],
)
async def test_deterministic_defects_are_found_without_the_agent(
    tmp_path: Path, compiler: _FakePdfCompiler, finding_type
) -> None:
    parts = _setup(
        tmp_path,
        compiler=compiler,
        latex=_latex(pages=compiler.pages),
    )
    renderer = _FakeRenderer(pages=compiler.pages)
    agent = _FakeVisualAgent()
    policy = ResumeVisualQAPolicy(max_pages=compiler.pages)

    result = await _review(parts, renderer, agent, policy=policy)

    assert result.result.verdict is ResumeVisualQAVerdict.REVISION_REQUIRED
    types = {item.finding_type for item in result.result.findings}
    assert finding_type in types
    assert all(
        item.source is ResumeVisualQAFindingSource.DETERMINISTIC
        for item in result.result.findings
    )
    assert renderer.render_calls == 0
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_renderer_unavailable_defers_with_zero_agent_calls(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    renderer = _FakeRenderer(
        describe_error=PdfRendererUnavailableError("no renderer")
    )
    agent = _FakeVisualAgent()

    result = await _review(parts, renderer, agent)

    assert result.status is (
        ResumeVisualQAStatus.DEFERRED_RENDERER_UNAVAILABLE
    )
    assert (
        result.reason_code
        is ResumeVisualQAFailureReason.RENDERER_UNAVAILABLE
    )
    assert agent.contexts == []
    assert not _results(parts)


@pytest.mark.asyncio
async def test_renderer_out_of_order_pages_defer(tmp_path: Path) -> None:
    parts = _setup(
        tmp_path, compiler=_FakePdfCompiler(pages=2), latex=_latex(pages=2)
    )
    renderer = _FakeRenderer(pages=2, out_of_order=True)
    agent = _FakeVisualAgent()

    result = await _review(
        parts, renderer, agent, policy=ResumeVisualQAPolicy(max_pages=2)
    )

    assert result.status is (
        ResumeVisualQAStatus.DEFERRED_RENDERER_UNAVAILABLE
    )
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_agent_receives_only_images_findings_and_policy(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeVisualAgent()

    await _review(parts, _FakeRenderer(), agent)

    context = agent.contexts[0]
    assert context.subject_id == "subject-a"
    assert len(context.pages) == 1
    assert context.pages[0].image_bytes == PNG_BYTES
    assert context.pages[0].image_format == RENDER_IMAGE_FORMAT
    assert context.policy["max_pages"] == 1
    assert context.policy_version == RESUME_VISUAL_QA_POLICY_VERSION
    assert context.agent_policy == RESUME_VISUAL_QA_AGENT_POLICY
    assert "never edit" in context.agent_policy.lower() or (
        "do not write" in context.agent_policy.lower()
    )
    assert not hasattr(context, "pdf_bytes")
    assert not hasattr(context, "latex_source")
    assert not hasattr(context, "home")
    rendered = json.dumps(
        [
            {
                "page_number": page.page_number,
                "width_px": page.width_px,
                "height_px": page.height_px,
            }
            for page in context.pages
        ]
    )
    assert "documentclass" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finding_type", "blocking"),
    [
        (ResumeVisualQAFindingType.ELEMENT_OVERLAP, True),
        (ResumeVisualQAFindingType.TEXT_TOO_SMALL, True),
        (ResumeVisualQAFindingType.BROKEN_GLYPH, True),
        (ResumeVisualQAFindingType.UNREADABLE_LAYOUT, True),
        (ResumeVisualQAFindingType.EXCESSIVE_DENSITY, False),
        (ResumeVisualQAFindingType.EXCESSIVE_WHITESPACE, False),
        (ResumeVisualQAFindingType.INCONSISTENT_ALIGNMENT, False),
    ],
)
async def test_agent_findings_drive_verdict_by_derived_severity(
    tmp_path: Path, finding_type, blocking: bool
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeVisualAgent(
        ResumeVisualQAAgentOutput(
            verdict=ResumeVisualQAAgentVerdict.ISSUES_FOUND,
            findings=(
                ResumeVisualQAAgentFinding(
                    finding_type=finding_type,
                    page_number=1,
                    explanation="Observed on the rendered page.",
                    bounding_box=VisualBoundingBox(
                        x0=10.0, top=10.0, x1=200.0, bottom=60.0
                    ),
                ),
            ),
        )
    )

    result = await _review(parts, _FakeRenderer(), agent)

    record = result.result
    recorded = record.findings[0]
    assert recorded.source is ResumeVisualQAFindingSource.AGENT
    assert recorded.finding_type is finding_type
    if blocking:
        assert recorded.severity is ResumeVisualQAFindingSeverity.BLOCKING
        assert record.verdict is ResumeVisualQAVerdict.REVISION_REQUIRED
    else:
        assert recorded.severity is ResumeVisualQAFindingSeverity.ADVISORY
        assert record.verdict is ResumeVisualQAVerdict.PASSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["unknown_page", "outside_box", "untyped"]
)
async def test_invalid_agent_output_defers_for_human(
    tmp_path: Path, corruption: str
) -> None:
    parts = _setup(tmp_path)
    if corruption == "untyped":
        agent = _FakeVisualAgent(lambda _context: {"verdict": "CLEAN"})
    else:
        page_number = 9 if corruption == "unknown_page" else 1
        box = (
            VisualBoundingBox(x0=10.0, top=10.0, x1=99_999.0, bottom=99_999.0)
            if corruption == "outside_box"
            else None
        )
        agent = _FakeVisualAgent(
            ResumeVisualQAAgentOutput(
                verdict=ResumeVisualQAAgentVerdict.ISSUES_FOUND,
                findings=(
                    ResumeVisualQAAgentFinding(
                        finding_type=(
                            ResumeVisualQAFindingType.ELEMENT_OVERLAP
                        ),
                        page_number=page_number,
                        explanation="Synthetic invalid reference.",
                        bounding_box=box,
                    ),
                ),
            )
        )

    result = await _review(parts, _FakeRenderer(), agent)

    assert result.status is ResumeVisualQAStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is ResumeVisualQAFailureReason.AGENT_OUTPUT_UNRELIABLE
    )
    assert result.result.verdict is ResumeVisualQAVerdict.DEFERRED
    assert any(
        item.finding_type
        is ResumeVisualQAFindingType.AGENT_OUTPUT_UNRELIABLE
        for item in result.result.findings
    )
    assert not result.retryable


@pytest.mark.asyncio
async def test_uncertain_agent_verdict_defers(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    agent = _FakeVisualAgent(
        ResumeVisualQAAgentOutput(
            verdict=ResumeVisualQAAgentVerdict.UNCERTAIN,
            findings=(),
        )
    )

    result = await _review(parts, _FakeRenderer(), agent)

    assert result.status is ResumeVisualQAStatus.DEFERRED_NEEDS_HUMAN
    assert result.result.verdict is ResumeVisualQAVerdict.DEFERRED


@pytest.mark.asyncio
async def test_agent_unavailable_fails_retryable(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    agent = _FakeVisualAgent(
        ResumeVisualQAAgentUnavailableError("provider offline")
    )

    result = await _review(parts, _FakeRenderer(), agent)

    assert result.status is ResumeVisualQAStatus.FAILED
    assert (
        result.reason_code
        is ResumeVisualQAFailureReason.AGENT_UNAVAILABLE
    )
    assert result.retryable
    assert not _results(parts)


@pytest.mark.asyncio
async def test_replay_returns_unchanged_without_render_or_agent(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    renderer = _FakeRenderer()
    agent = _FakeVisualAgent()

    first = await _review(parts, renderer, agent)
    replay = await _review(
        parts, renderer, agent, now=NOW + timedelta(days=2)
    )

    assert first.status is ResumeVisualQAStatus.CREATED
    assert replay.status is ResumeVisualQAStatus.UNCHANGED
    assert replay.result == first.result
    assert replay.result.validated_at == NOW
    assert renderer.render_calls == 1
    assert len(agent.contexts) == 1
    assert len(_results(parts)) == 1


@pytest.mark.asyncio
async def test_changed_policy_or_agent_version_creates_a_new_result(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = await _review(parts)

    policy_changed = await _review(
        parts,
        policy=ResumeVisualQAPolicy(max_pages=2),
        now=NOW + timedelta(minutes=1),
    )
    agent_changed = await _review(
        parts,
        metadata=ResumeVisualQAAgentMetadata(
            agent_version="resume-visual-qa-agent-v2",
            prompt_version="resume-visual-qa-prompt-v2",
            model_id="synthetic-visual-model",
        ),
        now=NOW + timedelta(minutes=2),
    )

    identifiers = {
        first.result.result_id,
        policy_changed.result.result_id,
        agent_changed.result.result_id,
    }
    assert len(identifiers) == 3
    assert len(_results(parts)) == 3
    kept = parts["visual_qa_repository"].get(
        subject_id="subject-a", result_id=first.result.result_id
    )
    assert kept.result == first.result


@pytest.mark.asyncio
async def test_conflict_corruption_and_restart(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    first = await _review(parts)
    record = first.result

    restarted = PrivateHomeResumeVisualQARepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(
        subject_id="subject-a", result_id=record.result_id
    )
    cross = restarted.get(
        subject_id="subject-b", result_id=record.result_id
    )
    assert read.status is ResumeVisualQAReadStatus.FOUND
    assert read.result == record
    assert read.result.result_content_hash == record.result_content_hash
    assert cross.status is ResumeVisualQAReadStatus.NOT_FOUND

    path = next(
        parts["home"].paths.resume_visual_qa.rglob(
            f"{record.result_id}.json"
        )
    )
    corrupted = b"{broken"
    path.write_bytes(corrupted)
    conflict = parts["visual_qa_repository"].save(record)
    corrupt = parts["visual_qa_repository"].get(
        subject_id="subject-a", result_id=record.result_id
    )

    assert conflict.status is ResumeVisualQAWriteStatus.FAILED
    assert (
        conflict.reason_code
        is ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE
    )
    assert path.read_bytes() == corrupted
    assert corrupt.status is ResumeVisualQAReadStatus.INTEGRITY_FAILURE


@pytest.mark.asyncio
async def test_invalid_command_fails_without_side_effects(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    renderer = _FakeRenderer()
    agent = _FakeVisualAgent()

    naive = await _review(
        parts, renderer, agent, now=datetime(2026, 7, 29, 18, 0)
    )

    assert naive.status is ResumeVisualQAStatus.FAILED
    assert (
        naive.reason_code
        is ResumeVisualQAFailureReason.INVALID_REQUEST
    )
    assert renderer.render_calls == 0
    assert agent.contexts == []
    assert not _results(parts)


def test_real_renderer_produces_stable_ordered_pages() -> None:
    renderer = PdfiumPageRenderer()
    description = renderer.describe()
    pdf = synthetic_pdf(pages=2)

    pages = renderer.render(pdf)

    assert description.dpi == DEFAULT_RENDER_DPI
    assert description.image_format == RENDER_IMAGE_FORMAT
    assert description.renderer_name == "pypdfium2"
    assert tuple(page.page_number for page in pages) == (1, 2)
    assert all(page.image_bytes.startswith(b"\x89PNG") for page in pages)
    assert all(page.width_px > 0 and page.height_px > 0 for page in pages)
    # 612pt at 150 DPI is 1275px wide.
    assert pages[0].width_px == 1275


def test_real_renderer_rejects_unreadable_input() -> None:
    renderer = PdfiumPageRenderer()

    with pytest.raises(PdfRendererUnavailableError):
        renderer.render(b"not a pdf at all")


def test_module_never_edits_documents_or_reaches_execution() -> None:
    module_path = Path(visual_qa_module.__file__)
    text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = {
        "core.application_engine",
        "core.browser_broker",
        "core.latex_compiler",
        "core.materials",
        "core.resume_latex_markers",
        "playwright",
        "subprocess",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
    assert "register_resume_latex_version" not in text
    assert "compile_resume_latex" not in text
    assert "write_bytes_if_absent" not in text.split("class PrivateHome")[0]
