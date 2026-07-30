from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.resume_layout_revision as revision_module
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.job_prioritization import ProposedPriorityLevel
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
    PdfRendererDescription,
    PdfRendererUnavailableError,
    RenderedPage,
)
from core.private_home import PrivateHome
from core.resume_compilation import (
    CompileResumeLatexCommand,
    ResumeCompilationFailureReason,
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
from core.resume_layout_revision import (
    RESUME_LAYOUT_REVISION_AGENT_POLICY,
    RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
    CompositeLatexBuildProvenanceRepository,
    PrivateHomeResumeLayoutRevisionRecordRepository,
    PrivateHomeResumeLayoutRevisionRepository,
    ResumeLayoutAttemptOutcome,
    ResumeLayoutRevisionAgentMetadata,
    ResumeLayoutRevisionAgentOutput,
    ResumeLayoutRevisionAgentUnavailableError,
    ResumeLayoutRevisionContext,
    ResumeLayoutRevisionFailureReason,
    ResumeLayoutRevisionPolicy,
    ResumeLayoutRevisionReadStatus,
    ResumeLayoutRevisionStatus,
    ReviseResumeLayoutCommand,
    revise_resume_layout,
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
from core.resume_visual_qa import (
    PrivateHomeResumeVisualQARepository,
    ResumeVisualQAAgentMetadata,
    ResumeVisualQAAgentOutput,
    ResumeVisualQAAgentVerdict,
    ResumeVisualQAPolicy,
    ResumeVisualQAStatus,
    ResumeVisualQAVerdict,
    ReviewResumeVisualQACommand,
    review_resume_visual_qa,
)

from test_resume_visual_qa import PNG_BYTES, synthetic_pdf


NOW = datetime(2026, 7, 29, 21, 0, tzinfo=timezone.utc)
METADATA = ResumeLayoutRevisionAgentMetadata(
    agent_version="resume-layout-revision-agent-v1",
    prompt_version="resume-layout-revision-prompt-v1",
    model_id="synthetic-revision-model",
)
QA_METADATA = ResumeVisualQAAgentMetadata(
    agent_version="resume-visual-qa-agent-v1",
    prompt_version="resume-visual-qa-prompt-v1",
    model_id="synthetic-visual-model",
)
SECTION_TITLE = "Experience"
BULLET_TEXT = "Built deterministic geospatial pipelines in Python."
CONTENT_REGION = (
    f"{JOBOPS_CONTENT_BEGIN}\n"
    "\\JobopsSection{resume-section-a}{Experience}\n"
    "\\begin{itemize}\n"
    f"\\JobopsBullet{{resume-block-a}}{{{BULLET_TEXT}}}\n"
    "\\end{itemize}\n"
    f"{JOBOPS_CONTENT_END}\n"
)


def _latex(*, margin: str = "0.75in", extra: str = "") -> str:
    return (
        "\\documentclass[11pt]{article}\n"
        f"\\usepackage[margin={margin}]{{geometry}}\n"
        "\\pagestyle{empty}\n"
        f"{extra}"
        f"{MARKER_MACRO_DEFINITIONS}"
        "\\begin{document}\n"
        f"{CONTENT_REGION}"
        "\\end{document}\n"
    )


def _hashed(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


class _ScriptedCompiler:
    """Returns a synthetic PDF whose page count follows a script."""

    def __init__(self, page_counts: list[int]) -> None:
        self.page_counts = page_counts
        self.calls = 0

    def describe(self) -> LatexCompilerDescription:
        return LatexCompilerDescription(
            engine="pdflatex",
            compiler_version="pdfTeX 3.141592653 (synthetic)",
            normalized_flags=normalized_compile_flags(),
            compile_policy_version=LATEX_COMPILE_POLICY_VERSION,
            sandbox_policy_version=LATEX_SANDBOX_POLICY_VERSION,
        )

    def compile(self, request: LatexCompileRequest) -> LatexCompileOutcome:
        index = min(self.calls, len(self.page_counts) - 1)
        pages = self.page_counts[index]
        self.calls += 1
        return LatexCompileOutcome(
            status=LatexCompileStatus.SUCCEEDED,
            pdf_bytes=synthetic_pdf(
                pages=pages, text=BULLET_TEXT, title=SECTION_TITLE
            ),
            diagnostics="",
            exit_code=0,
            compiler_started=True,
        )


class _StoppingCompiler(_ScriptedCompiler):
    """Compiles the base resume, then fails on the first revised version."""

    def compile(self, request: LatexCompileRequest) -> LatexCompileOutcome:
        if self.calls == 0:
            return super().compile(request)
        self.calls += 1
        return LatexCompileOutcome(
            status=LatexCompileStatus.COMPILATION_ERROR,
            pdf_bytes=None,
            diagnostics="! Undefined control sequence.",
            exit_code=1,
            compiler_started=True,
        )


class _InfrastructureStoppingCompiler(_ScriptedCompiler):
    def __init__(self, page_counts, stop_status: LatexCompileStatus) -> None:
        super().__init__(page_counts)
        self.stop_status = stop_status

    def compile(self, request: LatexCompileRequest) -> LatexCompileOutcome:
        if self.calls == 0:
            return super().compile(request)
        self.calls += 1
        return LatexCompileOutcome(
            status=self.stop_status,
            pdf_bytes=None,
            diagnostics="bounded synthetic diagnostic",
            exit_code=None,
            compiler_started=(
                self.stop_status is LatexCompileStatus.TIMEOUT
            ),
        )


class _FakeRenderer:
    def __init__(self, *, pages: int = 1, error: Exception | None = None) -> None:
        self.pages = pages
        self.error = error
        self.render_calls = 0

    def describe(self) -> PdfRendererDescription:
        return PdfRendererDescription(
            renderer_name="fake-renderer",
            renderer_version="1.0.0",
            dpi=DEFAULT_RENDER_DPI,
            image_format=RENDER_IMAGE_FORMAT,
        )

    def render(self, pdf_bytes: bytes) -> tuple[RenderedPage, ...]:
        self.render_calls += 1
        if self.error is not None:
            raise self.error
        from core.resume_compilation import pdf_page_count

        count = pdf_page_count(pdf_bytes) or self.pages
        return tuple(
            RenderedPage(
                page_number=number,
                width_px=1275,
                height_px=1650,
                image_format=RENDER_IMAGE_FORMAT,
                image_bytes=PNG_BYTES,
            )
            for number in range(1, count + 1)
        )


class _CleanVisualAgent:
    async def review(self, _context):
        return ResumeVisualQAAgentOutput(
            verdict=ResumeVisualQAAgentVerdict.CLEAN, findings=()
        )


class _FakeRevisionAgent:
    def __init__(self, transform=None) -> None:
        self.transform = transform
        self.contexts: list[ResumeLayoutRevisionContext] = []

    async def revise(self, context: ResumeLayoutRevisionContext):
        self.contexts.append(context)
        if isinstance(self.transform, Exception):
            raise self.transform
        if self.transform is None:
            revised = context.latex_source.replace(
                "margin=0.75in", "margin=0.5in"
            )
        else:
            revised = self.transform(context)
        if not isinstance(revised, str):
            return revised
        return ResumeLayoutRevisionAgentOutput(latex_source=revised)


async def _setup(
    tmp_path: Path,
    *,
    subject_id: str = "subject-a",
    compiler: _ScriptedCompiler | None = None,
    visual_policy: ResumeVisualQAPolicy | None = None,
):
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id="job-one",
        job_revision=1,
        job_content_hash=_hashed({"job": "one"}),
        priority_decision_id="priority-decision-one",
        policy_id="policy-one",
        policy_version=1,
        policy_content_hash=_hashed({"policy": "one"}),
        accepted_job_intent_id="accepted-intent-one",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
        user_preparation_instructions="Keep the layout conservative.",
    )
    plan_repository = PrivateHomeApplicationPlanRepository(home)
    plan_repository.save(plan)

    section = TailoredResumeSection(
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
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "resume_selection_decision_id": "resume-selection-" + "4" * 64,
        "source_resume_id": "resume-candidate-" + "5" * 64,
        "source_artifact_sha256": "6" * 64,
        "source_projection_id": "source-resume-projection-" + "7" * 64,
        "source_projection_hash": "8" * 64,
        "evidence_snapshot_id": "candidate-evidence-snapshot-" + "9" * 64,
        "evidence_snapshot_hash": "a" * 64,
        "user_preparation_instructions_hash": (
            plan.user_preparation_instructions_hash
        ),
        "agent_version": "resume-tailoring-agent-v1",
        "prompt_version": "resume-tailoring-prompt-v1",
        "model_id": "synthetic-tailoring-model",
        "agent_policy_version": RESUME_TAILORING_POLICY_VERSION,
        "rationale": "Synthetic draft for layout revision coverage.",
        "sections": [section.to_dict()],
    }
    draft = TailoredResumeDraft(
        draft_content_hash=_hashed(draft_content),
        created_at=NOW,
        sections=(section,),
        **{k: v for k, v in draft_content.items() if k != "sections"},
    )
    draft_repository = PrivateHomeTailoredResumeDraftRepository(home)
    draft_repository.save(draft)

    latex_repository = PrivateHomeResumeLatexVersionRepository(home)
    version = register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id=subject_id,
            source_kind=ResumeLatexSourceKind.SYSTEM_TEMPLATE_DERIVED,
            now=NOW,
            latex_source=_latex(),
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
        application_plan_id=plan.plan_id,
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
    construction_repository.save(construction)

    revision_records = PrivateHomeResumeLayoutRevisionRecordRepository(home)
    provenance = CompositeLatexBuildProvenanceRepository(
        construction_repository, revision_records
    )
    compilation_repository = PrivateHomeResumeCompilationRepository(home)
    active_compiler = compiler or _ScriptedCompiler([2, 1])

    def compile_step(*, subject_id, provenance_record_id, latex_version_id, now):
        return compile_resume_latex(
            CompileResumeLatexCommand(
                subject_id=subject_id,
                resume_latex_construction_record_id=provenance_record_id,
                resume_latex_version_id=latex_version_id,
                now=now,
            ),
            construction_repository=provenance,
            latex_version_repository=latex_repository,
            compiler=active_compiler,
            compilation_repository=compilation_repository,
            home=home,
        )

    visual_qa_repository = PrivateHomeResumeVisualQARepository(home)
    renderer = _FakeRenderer()
    active_visual_policy = visual_policy or ResumeVisualQAPolicy()

    async def review_step(*, subject_id, compilation_record_id, now):
        return await review_resume_visual_qa(
            ReviewResumeVisualQACommand(
                subject_id=subject_id,
                resume_compilation_record_id=compilation_record_id,
                now=now,
            ),
            compilation_repository=compilation_repository,
            latex_version_repository=latex_repository,
            construction_repository=provenance,
            draft_repository=draft_repository,
            renderer=renderer,
            agent=_CleanVisualAgent(),
            metadata=QA_METADATA,
            visual_qa_repository=visual_qa_repository,
            policy=active_visual_policy,
            home=home,
        )

    initial = compile_step(
        subject_id=subject_id,
        provenance_record_id=construction.record_id,
        latex_version_id=version.latex_version_id,
        now=NOW,
    )
    assert initial.status is ResumeCompilationStatus.CREATED, initial.reason_code

    initial_qa = await review_step(
        subject_id=subject_id,
        compilation_record_id=initial.record.record_id,
        now=NOW,
    )
    assert initial_qa.result is not None

    return {
        "home": home,
        "plan_repository": plan_repository,
        "draft": draft,
        "draft_repository": draft_repository,
        "version": version,
        "latex_repository": latex_repository,
        "construction_repository": construction_repository,
        "provenance": provenance,
        "revision_records": revision_records,
        "compilation_repository": compilation_repository,
        "visual_qa_repository": visual_qa_repository,
        "revision_repository": PrivateHomeResumeLayoutRevisionRepository(home),
        "renderer": renderer,
        "compiler": active_compiler,
        "compile_step": compile_step,
        "review_step": review_step,
        "initial_qa": initial_qa.result,
    }


async def _revise(
    parts,
    agent=None,
    *,
    subject_id: str = "subject-a",
    policy: ResumeLayoutRevisionPolicy | None = None,
    now: datetime = NOW,
    visual_qa_result_id: str | None = None,
):
    return await revise_resume_layout(
        ReviseResumeLayoutCommand(
            subject_id=subject_id,
            resume_visual_qa_result_id=(
                visual_qa_result_id or parts["initial_qa"].result_id
            ),
            now=now,
        ),
        visual_qa_repository=parts["visual_qa_repository"],
        compilation_repository=parts["compilation_repository"],
        latex_version_repository=parts["latex_repository"],
        provenance_repository=parts["provenance"],
        revision_record_repository=parts["revision_records"],
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        renderer=parts["renderer"],
        agent=agent if agent is not None else _FakeRevisionAgent(),
        metadata=METADATA,
        compile_step=parts["compile_step"],
        review_step=parts["review_step"],
        revision_repository=parts["revision_repository"],
        policy=policy,
        home=parts["home"],
    )


def _runs(parts) -> tuple[Path, ...]:
    return tuple(
        parts["home"].paths.resume_layout_revision_runs.rglob("*.json")
    )


@pytest.mark.asyncio
async def test_passed_visual_qa_needs_no_revision(tmp_path: Path) -> None:
    parts = await _setup(tmp_path, compiler=_ScriptedCompiler([1]))
    agent = _FakeRevisionAgent()
    assert parts["initial_qa"].verdict is ResumeVisualQAVerdict.PASSED
    compiles_before = parts["compiler"].calls

    result = await _revise(parts, agent)

    assert result.status is ResumeLayoutRevisionStatus.NOT_REQUIRED
    assert result.run is None
    assert agent.contexts == []
    assert parts["compiler"].calls == compiles_before
    assert not _runs(parts)


@pytest.mark.asyncio
async def test_first_revision_that_passes_stops_immediately(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeRevisionAgent()
    assert parts["initial_qa"].verdict is (
        ResumeVisualQAVerdict.REVISION_REQUIRED
    )
    compiles_before = parts["compiler"].calls

    result = await _revise(parts, agent)

    assert result.status is ResumeLayoutRevisionStatus.CREATED
    run = result.run
    assert len(run.attempts) == 1
    assert run.attempts[0].outcome is ResumeLayoutAttemptOutcome.PASSED
    assert len(agent.contexts) == 1
    assert parts["compiler"].calls == compiles_before + 1
    assert run.contract_version == RESUME_LAYOUT_REVISION_CONTRACT_VERSION
    assert run.final_status is ResumeLayoutRevisionStatus.CREATED
    assert run.max_attempts == 3


@pytest.mark.asyncio
async def test_revision_creates_a_child_version_with_lineage(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)

    result = await _revise(parts)

    child_id = result.run.attempts[0].output_latex_version_id
    child = parts["latex_repository"].get(
        subject_id="subject-a", latex_version_id=child_id
    ).version
    assert child.source_kind is ResumeLatexSourceKind.AI_REVISED
    assert child.parent_version_id == parts["version"].latex_version_id
    assert child.root_family_id == parts["version"].root_family_id
    assert child.latex_version_id != parts["version"].latex_version_id
    assert result.run.final_latex_version_id == child_id


@pytest.mark.asyncio
async def test_content_region_is_byte_identical_after_revision(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)

    result = await _revise(parts)

    child_id = result.run.attempts[0].output_latex_version_id
    child = parts["latex_repository"].get(
        subject_id="subject-a", latex_version_id=child_id
    ).version
    revised = parts["home"].contained_path(
        child.source_reference
    ).read_text(encoding="utf-8")
    original = parts["home"].contained_path(
        parts["version"].source_reference
    ).read_text(encoding="utf-8")
    assert CONTENT_REGION in revised
    assert BULLET_TEXT in revised
    assert "margin=0.5in" in revised
    assert "margin=0.75in" in original
    from core.resume_latex_markers import parse_markers, split_controlled_region

    assert split_controlled_region(revised)[1] == (
        split_controlled_region(original)[1]
    )
    assert parse_markers(revised) == parse_markers(original)


@pytest.mark.asyncio
async def test_agent_sees_source_pages_findings_policy_and_instructions(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeRevisionAgent()

    await _revise(parts, agent)

    context = agent.contexts[0]
    assert context.attempt_number == 1
    assert "margin=0.75in" in context.latex_source
    assert context.pages and context.pages[0].image_bytes == PNG_BYTES
    assert context.findings
    assert context.visual_qa_policy["max_pages"] == 1
    assert context.layout_revision_policy["max_attempts"] == 3
    assert (
        context.user_preparation_instructions
        == "Keep the layout conservative."
    )
    assert context.agent_policy == RESUME_LAYOUT_REVISION_AGENT_POLICY
    assert not hasattr(context, "draft")
    assert not hasattr(context, "evidence_items")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transform", "fragment"),
    [
        (
            lambda c: c.latex_source.replace(
                BULLET_TEXT, "Led deterministic geospatial pipelines."
            ),
            "content region",
        ),
        (
            lambda c: c.latex_source.replace(
                "\\JobopsBullet{resume-block-a}", "\\JobopsBullet{other}"
            ),
            "content region",
        ),
        (
            lambda c: c.latex_source.replace(
                "\\begin{document}", "\\tiny\n\\begin{document}"
            ),
            "tiny",
        ),
        (
            lambda c: c.latex_source.replace(
                "\\begin{document}",
                "\\fontsize{4}{5}\\selectfont\n\\begin{document}",
            ),
            "font size",
        ),
        (
            lambda c: c.latex_source.replace("margin=0.75in", "margin=0.1in"),
            "margin",
        ),
        (
            lambda c: c.latex_source.replace(
                "\\begin{document}",
                "\\textcolor{white}{hidden}\n\\begin{document}",
            ),
            "white text",
        ),
        (
            lambda c: c.latex_source.replace(
                "\\begin{document}",
                "\\immediate\\write18{id}\n\\begin{document}",
            ),
            "SHELL_ESCAPE",
        ),
        (
            lambda c: c.latex_source.replace(
                "\\end{document}", "\\input{extra.tex}\n\\end{document}"
            ),
            "unmanaged file dependency",
        ),
    ],
)
async def test_unsafe_revisions_are_rejected_and_defer(
    tmp_path: Path, transform, fragment: str
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeRevisionAgent(transform)
    compiles_before = parts["compiler"].calls

    result = await _revise(parts, agent)

    assert result.status is ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is ResumeLayoutRevisionFailureReason.REVISION_OUTPUT_UNSAFE
    )
    assert result.run.attempts[0].outcome is (
        ResumeLayoutAttemptOutcome.AGENT_OUTPUT_REJECTED
    )
    assert fragment.lower() in result.run.attempts[0].detail.lower()
    assert parts["compiler"].calls == compiles_before
    assert result.run.final_latex_version_id == (
        parts["version"].latex_version_id
    )


@pytest.mark.asyncio
async def test_untyped_agent_output_defers(tmp_path: Path) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeRevisionAgent(lambda _c: {"latex": "free text"})

    result = await _revise(parts, agent)

    assert result.status is ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
    assert result.run.attempts[0].outcome is (
        ResumeLayoutAttemptOutcome.AGENT_OUTPUT_REJECTED
    )


@pytest.mark.asyncio
async def test_attempts_are_bounded_and_serial(tmp_path: Path) -> None:
    parts = await _setup(tmp_path, compiler=_ScriptedCompiler([2, 2, 2, 2]))
    sizes = iter(["0.7in", "0.65in", "0.6in", "0.55in"])
    agent = _FakeRevisionAgent(
        lambda c: c.latex_source.replace(
            "margin=0.75in", f"margin={next(sizes)}"
        )
        if "margin=0.75in" in c.latex_source
        else c.latex_source.replace("in]{geometry}", "in]{geometry} % pass")
    )

    result = await _revise(parts, agent)

    assert result.status is (
        ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED
    )
    assert (
        result.reason_code
        is ResumeLayoutRevisionFailureReason.ATTEMPTS_EXHAUSTED
    )
    assert len(result.run.attempts) == 3
    assert len(agent.contexts) == 3
    assert [item.attempt_number for item in result.run.attempts] == [1, 2, 3]
    assert all(
        item.outcome is ResumeLayoutAttemptOutcome.REVISION_REQUIRED
        for item in result.run.attempts
    )
    assert result.run.attempts[1].input_latex_version_id == (
        result.run.attempts[0].output_latex_version_id
    )


@pytest.mark.asyncio
async def test_compilation_stop_halts_the_run(tmp_path: Path) -> None:
    parts = await _setup(tmp_path, compiler=_StoppingCompiler([2, 1]))
    agent = _FakeRevisionAgent()

    result = await _revise(parts, agent)

    assert result.status is ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is ResumeLayoutRevisionFailureReason.COMPILATION_STOPPED
    )
    assert len(result.run.attempts) == 1
    assert result.run.attempts[0].outcome is (
        ResumeLayoutAttemptOutcome.COMPILATION_STOPPED
    )
    lineage = result.run.attempts[0].downstream_stop_lineage
    assert lineage is not None
    assert lineage.parent_attempt_id.startswith("resume-layout-revision-")
    assert lineage.child_stage_result_id.startswith(
        "resume-compilation-stop-"
    )
    assert lineage.child_stop_reason.code.value == "COMPILATION_ERROR"
    assert lineage.child_outcome.value == "DEFERRED"
    public = revision_module.resume_layout_revision_public_result(result)
    public_outputs = {
        item.key: item.value for item in public.outputs
    }
    assert (
        public_outputs["downstream_lineage_id"]
        == lineage.lineage_id
    )
    assert (
        public_outputs["downstream_child_reason_code"]
        == "COMPILATION_ERROR"
    )
    for drift in (
        {"subject_id": "subject-b"},
        {"application_plan_id": "plan-drift"},
        {"parent_attempt_id": "resume-layout-revision-" + "0" * 64},
        {
            "child_stage": (
                revision_module.ApplicationPreparationStage
                .RESUME_LAYOUT_REVISION
            )
        },
        {
            "child_outcome": (
                revision_module.PreparationStageOutcome.FAILED
            )
        },
        {"contract_version": "downstream-lineage-v999"},
    ):
        with pytest.raises(ValueError):
            replace(lineage, **drift)
    assert len(agent.contexts) == 1
    compile_calls = parts["compiler"].calls

    replay = await _revise(parts, agent)

    assert replay.status is ResumeLayoutRevisionStatus.UNCHANGED
    assert replay.run.run_id == result.run.run_id
    assert (
        replay.run.attempts[0].downstream_stop_lineage
        == lineage
    )
    assert parts["compiler"].calls == compile_calls


@pytest.mark.asyncio
async def test_compilation_infrastructure_lineage_stays_distinct(
    tmp_path: Path,
) -> None:
    parts = await _setup(
        tmp_path,
        compiler=_InfrastructureStoppingCompiler(
            [2], LatexCompileStatus.UNAVAILABLE
        ),
    )

    result = await _revise(parts)

    lineage = result.run.attempts[0].downstream_stop_lineage
    assert lineage is not None
    assert (
        lineage.child_stop_reason.code.value
        == ResumeCompilationFailureReason.COMPILER_UNAVAILABLE.value
    )
    assert lineage.child_stage_result_id.startswith(
        "resume-compilation-stop-"
    )


@pytest.mark.asyncio
async def test_compilation_lineage_binding_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path, compiler=_StoppingCompiler([2, 1]))
    authoritative_compile = parts["compile_step"]

    def mismatched_compile(**kwargs):
        return replace(
            authoritative_compile(**kwargs),
            source_construction_record_id="resume-layout-revision-" + "0" * 64,
        )

    parts["compile_step"] = mismatched_compile

    result = await _revise(parts)

    assert result.status is ResumeLayoutRevisionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLayoutRevisionFailureReason.RECORD_INTEGRITY_FAILURE
    )
    assert result.run is None


def test_legacy_compilation_stop_attempt_is_not_reconstructed_from_detail() -> None:
    legacy = {
        "attempt_number": 1,
        "input_latex_version_id": "latex-1",
        "input_compilation_record_id": "compilation-1",
        "input_visual_qa_result_id": "visual-1",
        "blocking_finding_ids": ["finding-1"],
        "agent_version": "v1",
        "prompt_version": "v1",
        "model_id": "synthetic",
        "output_latex_version_id": "latex-2",
        "output_compilation_record_id": None,
        "output_visual_qa_result_id": None,
        "outcome": ResumeLayoutAttemptOutcome.COMPILATION_STOPPED.value,
        "detail": "Compilation stopped: DEFERRED_COMPILATION_ERROR.",
    }

    attempt = revision_module._attempt_from_dict(legacy)

    assert attempt.downstream_stop_lineage is None
    assert attempt.legacy_incomplete_downstream_lineage is True
    assert attempt.to_dict() == legacy


@pytest.mark.asyncio
async def test_renderer_failure_defers_for_human(tmp_path: Path) -> None:
    parts = await _setup(tmp_path)
    parts["renderer"].error = PdfRendererUnavailableError("no renderer")
    agent = _FakeRevisionAgent()

    result = await _revise(parts, agent)

    assert result.status is ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is ResumeLayoutRevisionFailureReason.RENDERER_UNAVAILABLE
    )
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_agent_unavailable_fails_retryable(tmp_path: Path) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeRevisionAgent(
        ResumeLayoutRevisionAgentUnavailableError("offline")
    )

    result = await _revise(parts, agent)

    assert result.status is ResumeLayoutRevisionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLayoutRevisionFailureReason.AGENT_UNAVAILABLE
    )
    assert result.retryable
    assert not _runs(parts)


@pytest.mark.asyncio
async def test_replay_returns_unchanged_with_zero_extra_work(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeRevisionAgent()

    first = await _revise(parts, agent)
    compiles = parts["compiler"].calls
    renders = parts["renderer"].render_calls

    replay = await _revise(parts, agent, now=NOW + timedelta(days=1))

    assert first.status is ResumeLayoutRevisionStatus.CREATED
    assert replay.status is ResumeLayoutRevisionStatus.UNCHANGED
    assert replay.run == first.run
    assert replay.run.started_at == NOW
    assert len(agent.contexts) == 1
    assert parts["compiler"].calls == compiles
    assert parts["renderer"].render_calls == renders
    assert len(_runs(parts)) == 1


@pytest.mark.asyncio
async def test_changed_policy_creates_a_new_run(tmp_path: Path) -> None:
    parts = await _setup(tmp_path, compiler=_ScriptedCompiler([2, 1, 1, 1]))
    first = await _revise(parts)

    second = await _revise(
        parts,
        policy=ResumeLayoutRevisionPolicy(max_attempts=2),
        now=NOW + timedelta(minutes=1),
    )

    assert second.run.run_id != first.run.run_id
    assert len(_runs(parts)) == 2
    kept = parts["revision_repository"].get(
        subject_id="subject-a", run_id=first.run.run_id
    )
    assert kept.run == first.run


@pytest.mark.asyncio
async def test_conflict_corruption_and_restart(tmp_path: Path) -> None:
    parts = await _setup(tmp_path)
    first = await _revise(parts)
    run = first.run

    restarted = PrivateHomeResumeLayoutRevisionRepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(subject_id="subject-a", run_id=run.run_id)
    cross = restarted.get(subject_id="subject-b", run_id=run.run_id)
    assert read.status is ResumeLayoutRevisionReadStatus.FOUND
    assert read.run == run
    assert read.run.attempts == run.attempts
    assert cross.status is ResumeLayoutRevisionReadStatus.NOT_FOUND

    path = next(
        parts["home"].paths.resume_layout_revision_runs.rglob(
            f"{run.run_id}.json"
        )
    )
    corrupted = b"{broken"
    path.write_bytes(corrupted)
    conflict = parts["revision_repository"].save(run)
    assert conflict.status.value == "FAILED"
    assert path.read_bytes() == corrupted


@pytest.mark.asyncio
async def test_invalid_command_and_missing_qa_fail_closed(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeRevisionAgent()

    naive = await _revise(
        parts, agent, now=datetime(2026, 7, 29, 21, 0)
    )
    missing = await _revise(
        parts,
        agent,
        visual_qa_result_id="resume-visual-qa-" + "0" * 64,
    )

    assert (
        naive.reason_code
        is ResumeLayoutRevisionFailureReason.INVALID_REQUEST
    )
    assert (
        missing.reason_code
        is ResumeLayoutRevisionFailureReason.VISUAL_QA_NOT_FOUND
    )
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_real_p2a7_and_p2a8a_entry_points_are_used(
    tmp_path: Path,
) -> None:
    """The steps wired above call the real public functions, not copies."""

    parts = await _setup(tmp_path)
    result = await _revise(parts)

    child_id = result.run.attempts[0].output_latex_version_id
    compilation = parts["compilation_repository"].get(
        subject_id="subject-a",
        record_id=result.run.attempts[0].output_compilation_record_id,
    )
    qa = parts["visual_qa_repository"].get(
        subject_id="subject-a",
        result_id=result.run.attempts[0].output_visual_qa_result_id,
    )

    assert compilation.record is not None
    assert compilation.record.latex_version_id == child_id
    assert compilation.record.compiler_engine == "pdflatex"
    assert qa.result is not None
    assert qa.result.verdict is ResumeVisualQAVerdict.PASSED
    assert qa.result.latex_version_id == child_id
    stored_pdf = parts["home"].contained_path(
        compilation.record.pdf_reference
    )
    assert stored_pdf.read_bytes().startswith(b"%PDF-")


def test_module_never_edits_content_or_reaches_execution() -> None:
    module_path = Path(revision_module.__file__)
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
        "core.candidate_evidence",
        "core.materials",
        "core.resume_fact_qa",
        "core.latex_compiler",
        "playwright",
        "subprocess",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
    # Compilation and visual QA are reached only through injected steps.
    assert "compile_resume_latex" not in text
    assert "review_resume_visual_qa" not in text
