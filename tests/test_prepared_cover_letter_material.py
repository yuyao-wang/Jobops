from __future__ import annotations

import ast
import hashlib
import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.prepared_cover_letter_material as material_module
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.application_preparation_orchestrator import (
    PreparationStageOutcome,
)
from core.cover_letter_draft import (
    COVER_LETTER_DRAFT_CONTRACT_VERSION,
    COVER_LETTER_DRAFT_POLICY_VERSION,
    CoverLetterDraft,
    CoverLetterDraftReadResult,
    CoverLetterDraftReadStatus,
    CoverLetterParagraph,
    CoverLetterParagraphPurpose,
    PrivateHomeCoverLetterDraftRepository,
)
from core.cover_letter_fact_qa import (
    COVER_LETTER_FACT_QA_CONTRACT_VERSION,
    COVER_LETTER_FACT_QA_POLICY_VERSION,
    CoverLetterFactQAFinding,
    CoverLetterFactQAFindingSeverity,
    CoverLetterFactQAFindingSource,
    CoverLetterFactQAResult,
    CoverLetterFactQAReadResult,
    CoverLetterFactQAReadStatus,
    CoverLetterFactQAVerdict,
    PrivateHomeCoverLetterFactQARepository,
)
from core.job_discovery import JobPosting
from core.job_prioritization import ProposedPriorityLevel
from core.latex_compiler import (
    LATEX_COMPILE_POLICY_VERSION,
    LATEX_SANDBOX_POLICY_VERSION,
    LatexCompileOutcome,
    LatexCompileRequest,
    LatexCompileStatus,
    LatexCompilerDescription,
    LatexCompilerUnavailableError,
    SandboxedPdfLatexCompiler,
    normalized_compile_flags,
)
from core.prepared_cover_letter_material import (
    COVER_LETTER_PUBLICATION_POLICY_VERSION,
    DefaultManagedCoverLetterTemplateProvider,
    MANAGED_COVER_LETTER_TEMPLATE_ID,
    MANAGED_COVER_LETTER_TEMPLATE_SOURCE,
    MANAGED_COVER_LETTER_TEMPLATE_VERSION,
    ManagedCoverLetterTemplate,
    PreparedCoverLetterMaterialFailureReason,
    PreparedCoverLetterMaterialNotReadyReason,
    PreparedCoverLetterMaterialReadStatus,
    PreparedCoverLetterMaterialRole,
    PreparedCoverLetterMaterialStatus,
    PrivateHomePreparedCoverLetterMaterialRepository,
    PublishPreparedCoverLetterCommand,
    cover_letter_pdf_text_is_faithful,
    escape_cover_letter_latex_text,
    expected_cover_letter_text_projection,
    publish_prepared_cover_letter,
    prepared_cover_letter_publication_public_result,
    render_cover_letter_latex,
    validate_managed_cover_letter_template,
)
from core.private_home import PrivateHome


NOW = datetime(2026, 8, 1, 15, 0, tzinfo=timezone.utc)
SUBJECT_ID = "synthetic-subject-a"


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _paragraph(
    order: int,
    purpose: CoverLetterParagraphPurpose,
    text: str,
) -> CoverLetterParagraph:
    content = {
        "evidence_ids": [],
        "jd_alignment": [],
        "order": order,
        "purpose": purpose.value,
        "text": text,
    }
    return CoverLetterParagraph(
        paragraph_id="cover-letter-paragraph-" + _hash(content),
        order=order,
        purpose=purpose,
        text=text,
        evidence_ids=(),
        jd_alignment=(),
    )


def _draft(
    plan: ApplicationPlan,
    *,
    version: str = "v1",
    paragraph_suffix: str = "",
) -> CoverLetterDraft:
    binding = _hash({"draft": version, "plan": plan.plan_id})
    paragraphs = (
        _paragraph(
            0,
            CoverLetterParagraphPurpose.INTRODUCTION,
            "I am applying for the R&D role with C++ experience.",
        ),
        _paragraph(
            1,
            CoverLetterParagraphPurpose.CLOSING,
            (
                "I built Python_pipeline #12 at $0 test cost "
                f"with 100% repeatability{paragraph_suffix}."
            ),
        ),
    )
    values = {
        "draft_id": f"cover-letter-draft-{binding}",
        "contract_version": COVER_LETTER_DRAFT_CONTRACT_VERSION,
        "draft_binding": binding,
        "subject_id": plan.subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "evidence_snapshot_id": (
            "cover-letter-evidence-snapshot-" + _hash({"snapshot": version})
        ),
        "evidence_snapshot_hash": _hash({"evidence": version}),
        "user_preparation_instructions_hash": (
            plan.user_preparation_instructions_hash
        ),
        "agent_version": "cover-letter-agent-v1",
        "prompt_version": "cover-letter-prompt-v1",
        "model_id": "synthetic-model",
        "agent_policy_version": COVER_LETTER_DRAFT_POLICY_VERSION,
        "greeting": "Dear R&D Team, 100% ready.",
        "paragraphs": [item.to_dict() for item in paragraphs],
        "closing": "Sincerely,\nSynthetic_Candidate \\ path ^ ~",
        "rationale": "Synthetic truthful fixture.",
    }
    return CoverLetterDraft(
        draft_content_hash=_hash(values),
        created_at=NOW,
        paragraphs=paragraphs,
        **{key: value for key, value in values.items() if key != "paragraphs"},
    )


def _blocking_finding(draft: CoverLetterDraft) -> CoverLetterFactQAFinding:
    values = {
        "claim_text": draft.paragraphs[0].text,
        "evidence_ids": [],
        "explanation": "Synthetic blocking finding.",
        "finding_type": "UNSUPPORTED_CANDIDATE_CLAIM",
        "jd_references": [],
        "paragraph_id": draft.paragraphs[0].paragraph_id,
        "severity": CoverLetterFactQAFindingSeverity.BLOCKING.value,
        "source": CoverLetterFactQAFindingSource.DETERMINISTIC.value,
    }
    return CoverLetterFactQAFinding(
        finding_id="cover-letter-fact-qa-finding-" + _hash(values),
        paragraph_id=draft.paragraphs[0].paragraph_id,
        finding_type="UNSUPPORTED_CANDIDATE_CLAIM",
        severity=CoverLetterFactQAFindingSeverity.BLOCKING,
        claim_text=draft.paragraphs[0].text,
        evidence_ids=(),
        jd_references=(),
        explanation="Synthetic blocking finding.",
        source=CoverLetterFactQAFindingSource.DETERMINISTIC,
    )


def _fact_qa(
    draft: CoverLetterDraft,
    *,
    version: str = "v1",
    verdict: CoverLetterFactQAVerdict = CoverLetterFactQAVerdict.PASSED,
) -> CoverLetterFactQAResult:
    binding = _hash(
        {
            "draft": draft.draft_id,
            "qa": version,
            "verdict": verdict.value,
        }
    )
    findings = (
        (_blocking_finding(draft),)
        if verdict is CoverLetterFactQAVerdict.BLOCKED
        else ()
    )
    values = {
        "agent_version": f"cover-letter-fact-qa-agent-{version}",
        "application_plan_id": draft.application_plan_id,
        "contract_version": COVER_LETTER_FACT_QA_CONTRACT_VERSION,
        "cover_letter_draft_id": draft.draft_id,
        "draft_content_hash": draft.draft_content_hash,
        "evidence_snapshot_hash": draft.evidence_snapshot_hash,
        "evidence_snapshot_id": draft.evidence_snapshot_id,
        "findings": [item.to_dict() for item in findings],
        "job_content_hash": draft.job_content_hash,
        "job_id": draft.job_id,
        "job_revision": draft.job_revision,
        "model_id": "synthetic-qa-model",
        "prompt_version": "cover-letter-fact-qa-prompt-v1",
        "qa_policy_version": COVER_LETTER_FACT_QA_POLICY_VERSION,
        "result_binding": binding,
        "result_id": f"cover-letter-fact-qa-{binding}",
        "subject_id": draft.subject_id,
        "verdict": verdict.value,
    }
    return CoverLetterFactQAResult(
        result_id=values["result_id"],
        contract_version=values["contract_version"],
        result_binding=binding,
        subject_id=draft.subject_id,
        application_plan_id=draft.application_plan_id,
        job_id=draft.job_id,
        job_revision=draft.job_revision,
        job_content_hash=draft.job_content_hash,
        evidence_snapshot_id=draft.evidence_snapshot_id,
        evidence_snapshot_hash=draft.evidence_snapshot_hash,
        cover_letter_draft_id=draft.draft_id,
        draft_content_hash=draft.draft_content_hash,
        agent_version=values["agent_version"],
        prompt_version=values["prompt_version"],
        model_id=values["model_id"],
        qa_policy_version=values["qa_policy_version"],
        verdict=verdict,
        findings=findings,
        result_content_hash=_hash(values),
        validated_at=NOW,
    )


def _pdf(*, text: str, pages: int = 1) -> bytes:
    def escaped(value: str) -> str:
        return (
            value.replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + " ".join(
                f"{4 + index * 2} 0 R" for index in range(pages)
            ).encode("ascii")
            + f"] /Count {pages} >>".encode("ascii")
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index in range(pages):
        stream = (
            f"BT /F1 10 Tf 36 720 Td ({escaped(text)}) Tj ET"
            if index == 0
            else ""
        ).encode("ascii")
        content_object = 5 + index * 2
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                "/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_object} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    document = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(document))
        document += (
            f"{number} 0 obj\n".encode("ascii")
            + body
            + b"\nendobj\n"
        )
    xref = len(document)
    document += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    document += b"0000000000 65535 f \n"
    for offset in offsets:
        document += f"{offset:010d} 00000 n \n".encode("ascii")
    document += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(document)


class _JobRepository:
    def __init__(self, job: JobPosting | None) -> None:
        self.job = job

    def get(self, job_id: str) -> JobPosting | None:
        if self.job is not None and self.job.job_id == job_id:
            return self.job
        return None

    def list_current(self) -> tuple[JobPosting, ...]:
        return (self.job,) if self.job is not None else ()


class _FakeCompiler:
    def __init__(
        self,
        pdf_bytes: bytes,
        *,
        compiler_version: str = "pdfTeX 3.141592653 (synthetic)",
        status: LatexCompileStatus = LatexCompileStatus.SUCCEEDED,
        describe_error: Exception | None = None,
    ) -> None:
        self.pdf_bytes = pdf_bytes
        self.compiler_version = compiler_version
        self.status = status
        self.describe_error = describe_error
        self.describe_calls = 0
        self.compile_calls: list[LatexCompileRequest] = []

    def describe(self) -> LatexCompilerDescription:
        self.describe_calls += 1
        if self.describe_error is not None:
            raise self.describe_error
        return LatexCompilerDescription(
            engine="pdflatex",
            compiler_version=self.compiler_version,
            normalized_flags=normalized_compile_flags(),
            compile_policy_version=LATEX_COMPILE_POLICY_VERSION,
            sandbox_policy_version=LATEX_SANDBOX_POLICY_VERSION,
        )

    def compile(self, request: LatexCompileRequest) -> LatexCompileOutcome:
        self.compile_calls.append(request)
        if self.status is LatexCompileStatus.SUCCEEDED:
            return LatexCompileOutcome(
                status=self.status,
                pdf_bytes=self.pdf_bytes,
                diagnostics="",
                exit_code=0,
                compiler_started=True,
            )
        return LatexCompileOutcome(
            status=self.status,
            pdf_bytes=None,
            diagnostics="synthetic compiler failure",
            exit_code=1,
            compiler_started=True,
        )


class _TemplateProvider:
    def __init__(self, template: ManagedCoverLetterTemplate) -> None:
        self.template = template
        self.calls = 0

    def get(self) -> ManagedCoverLetterTemplate:
        self.calls += 1
        return self.template


class _DraftRepositoryView:
    def __init__(self, draft: CoverLetterDraft) -> None:
        self.draft = draft

    def get(self, **_kwargs) -> CoverLetterDraftReadResult:
        return CoverLetterDraftReadResult(
            status=CoverLetterDraftReadStatus.FOUND,
            draft=self.draft,
        )

    def save(self, _draft):  # pragma: no cover - publication is read-only
        raise AssertionError("publication must not save a Draft")


class _FactQARepositoryView:
    def __init__(self, result: CoverLetterFactQAResult) -> None:
        self.result = result

    def get(self, **_kwargs) -> CoverLetterFactQAReadResult:
        return CoverLetterFactQAReadResult(
            status=CoverLetterFactQAReadStatus.FOUND,
            result=self.result,
        )

    def save(self, _result):  # pragma: no cover - publication is read-only
        raise AssertionError("publication must not save a Fact QA result")


def _setup(
    tmp_path: Path,
    *,
    subject_id: str = SUBJECT_ID,
    draft_version: str = "v1",
    qa_version: str = "v1",
    verdict: CoverLetterFactQAVerdict = CoverLetterFactQAVerdict.PASSED,
):
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id="job-one",
        job_revision=1,
        job_content_hash=_hash({"job": "one"}),
        priority_decision_id="priority-decision-one",
        policy_id="policy-one",
        policy_version=1,
        policy_content_hash=_hash({"policy": "one"}),
        accepted_job_intent_id="accepted-intent-one",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
    )
    plan_repository = PrivateHomeApplicationPlanRepository(home)
    plan_repository.save(plan)
    job = JobPosting(
        schema_version="job-posting-v1",
        job_id=plan.job_id,
        revision=plan.job_revision,
        source_platform="synthetic",
        source_job_id=None,
        source_url="https://jobs.example.test/one",
        company="Example R&D",
        title="Research Engineer",
        location="Remote",
        work_mode="REMOTE",
        posted_at=None,
        observed_at="2026-08-01T00:00:00Z",
        application_url=None,
        ats_type="unknown",
        description="Synthetic job description.",
        content_hash=plan.job_content_hash,
        status="ACTIVE",
    )
    draft = _draft(plan, version=draft_version)
    draft_repository = PrivateHomeCoverLetterDraftRepository(home)
    draft_repository.save(draft)
    qa = _fact_qa(draft, version=qa_version, verdict=verdict)
    qa_repository = PrivateHomeCoverLetterFactQARepository(home)
    qa_repository.save(qa)
    material_repository = PrivateHomePreparedCoverLetterMaterialRepository(
        home
    )
    compiler = _FakeCompiler(
        _pdf(text=expected_cover_letter_text_projection(draft))
    )
    return {
        "home": home,
        "plan": plan,
        "plan_repository": plan_repository,
        "job": job,
        "job_repository": _JobRepository(job),
        "draft": draft,
        "draft_repository": draft_repository,
        "qa": qa,
        "qa_repository": qa_repository,
        "material_repository": material_repository,
        "template_provider": DefaultManagedCoverLetterTemplateProvider(),
        "compiler": compiler,
    }


def _publish(parts, **overrides):
    command = overrides.pop(
        "command",
        PublishPreparedCoverLetterCommand(
            subject_id=parts["plan"].subject_id,
            application_plan_id=parts["plan"].plan_id,
            cover_letter_fact_qa_result_id=parts["qa"].result_id,
            now=NOW,
        ),
    )
    values = {
        "application_plan_repository": parts["plan_repository"],
        "job_repository": parts["job_repository"],
        "draft_repository": parts["draft_repository"],
        "fact_qa_repository": parts["qa_repository"],
        "template_provider": parts["template_provider"],
        "compiler": parts["compiler"],
        "material_repository": parts["material_repository"],
        "home": parts["home"],
    }
    values.update(overrides)
    return publish_prepared_cover_letter(command, **values)


def _material_records(parts) -> tuple[Path, ...]:
    return tuple(
        parts["home"].paths.prepared_cover_letter_materials.rglob("*.json")
    )


def _pdf_artifacts(parts) -> tuple[Path, ...]:
    return tuple(parts["home"].paths.compiled_cover_letters.rglob("*.pdf"))


def test_passed_draft_publishes_typed_immutable_material(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    result = _publish(parts)

    assert result.status is PreparedCoverLetterMaterialStatus.CREATED
    assert result.material is not None
    material = result.material
    assert material.material_role is PreparedCoverLetterMaterialRole.COVER_LETTER
    assert material.publication_policy_version == (
        COVER_LETTER_PUBLICATION_POLICY_VERSION
    )
    assert material.page_count == 1
    assert material.cover_letter_draft_id == parts["draft"].draft_id
    assert material.draft_content_hash == parts["draft"].draft_content_hash
    assert material.fact_qa_result_id == parts["qa"].result_id
    assert material.fact_qa_result_hash == parts["qa"].result_content_hash
    assert material.published_at == NOW
    source = parts["home"].contained_path(material.latex_source_reference)
    pdf = parts["home"].contained_path(material.pdf_reference)
    assert source.is_relative_to(
        parts["home"].paths.cover_letter_latex_sources
    )
    assert pdf.is_relative_to(parts["home"].paths.compiled_cover_letters)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        material.latex_source_sha256
    )
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == material.pdf_sha256
    assert cover_letter_pdf_text_is_faithful(
        pdf.read_bytes(), parts["draft"]
    )
    assert len(parts["compiler"].compile_calls) == 1


def test_renderer_is_deterministic_exactly_once_and_single_pass_escaped(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    template = DefaultManagedCoverLetterTemplateProvider().get()

    first = render_cover_letter_latex(parts["draft"], template)
    second = render_cover_letter_latex(parts["draft"], template)

    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")
    for paragraph in parts["draft"].paragraphs:
        block = "\n".join(
            (
                f"% JOBOPS_PARAGRAPH {paragraph.paragraph_id}",
                escape_cover_letter_latex_text(paragraph.text),
                "% JOBOPS_PARAGRAPH_END",
            )
        )
        assert first.count(block) == 1
        assert first.count(paragraph.paragraph_id) == 1
    assert r"R\&D" in first
    assert r"100\%" in first
    assert r"Python\_pipeline" in first
    assert r"\textbackslash{} path" in first
    assert r"\textasciicircum{}" in first
    assert r"\textasciitilde{}" in first
    assert r"\textbackslash\{\}" not in first
    assert escape_cover_letter_latex_text("{QA}") == r"\{QA\}"
    assert not any(
        token in first
        for token in (
            "%%JOBOPS_COVER_LETTER_GREETING%%",
            "%%JOBOPS_COVER_LETTER_PARAGRAPHS%%",
            "%%JOBOPS_COVER_LETTER_CLOSING%%",
        )
    )


def test_template_and_rendered_source_have_no_active_dependencies(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    source = render_cover_letter_latex(
        parts["draft"], DefaultManagedCoverLetterTemplateProvider().get()
    )

    for forbidden in (
        r"\write18",
        r"\input",
        r"\include",
        r"\openin",
        r"\openout",
        r"\special",
    ):
        assert forbidden not in MANAGED_COVER_LETTER_TEMPLATE_SOURCE
        assert forbidden not in source
    assert MANAGED_COVER_LETTER_TEMPLATE_SOURCE.count(
        r"\usepackage[T1]{fontenc}"
    ) == 1
    assert source.count(r"\usepackage[T1]{fontenc}") == 1


@pytest.mark.parametrize(
    "injection",
    (
        r"\write18{curl https://example.test}",
        r"\input{unmanaged-relative-file}",
        r"\openout1=unmanaged.txt",
        r"\usepackage{unmanaged-package}",
        r"\RequirePackage[T1]{fontenc}",
    ),
)
def test_template_rejects_dangerous_or_unallowlisted_capabilities(
    injection: str,
) -> None:
    unsafe = MANAGED_COVER_LETTER_TEMPLATE_SOURCE.replace(
        r"\begin{document}",
        f"{injection}\n" + r"\begin{document}",
    )

    with pytest.raises(ValueError):
        validate_managed_cover_letter_template(unsafe)


@pytest.mark.parametrize(
    "mode", ["blocked", "missing", "qa_mismatch", "draft_mismatch"]
)
def test_not_ready_bindings_never_compile_or_publish(
    tmp_path: Path, mode: str
) -> None:
    parts = _setup(
        tmp_path,
        verdict=(
            CoverLetterFactQAVerdict.BLOCKED
            if mode == "blocked"
            else CoverLetterFactQAVerdict.PASSED
        ),
    )
    command = None
    if mode == "missing":
        command = PublishPreparedCoverLetterCommand(
            subject_id=parts["plan"].subject_id,
            application_plan_id=parts["plan"].plan_id,
            cover_letter_fact_qa_result_id=(
                "cover-letter-fact-qa-" + "0" * 64
            ),
            now=NOW,
        )
    elif mode == "qa_mismatch":
        object.__setattr__(parts["qa"], "draft_content_hash", "f" * 64)
        parts["qa_repository"] = _FactQARepositoryView(parts["qa"])
    elif mode == "draft_mismatch":
        object.__setattr__(
            parts["draft"], "evidence_snapshot_hash", "f" * 64
        )
        parts["draft_repository"] = _DraftRepositoryView(parts["draft"])

    result = _publish(
        parts,
        **({"command": command} if command is not None else {}),
    )

    assert result.status is PreparedCoverLetterMaterialStatus.NOT_READY
    assert result.not_ready_reason in {
        PreparedCoverLetterMaterialNotReadyReason.FACT_QA_NOT_PASSED,
        PreparedCoverLetterMaterialNotReadyReason.DRAFT_BINDING_MISMATCH,
    }
    if mode in {"blocked", "missing"}:
        assert result.stopped_source_lineage is not None
        assert (
            result.stopped_source_lineage.source_result_id
            == (
                command.cover_letter_fact_qa_result_id
                if command is not None
                else parts["qa"].result_id
            )
        )
    else:
        assert result.stopped_source_lineage is None
    assert parts["compiler"].describe_calls == 0
    assert parts["compiler"].compile_calls == []
    assert not _material_records(parts)
    assert not _pdf_artifacts(parts)


def test_compiler_unavailable_defers_without_compile_or_material(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    parts["compiler"] = _FakeCompiler(
        b"unused",
        describe_error=LatexCompilerUnavailableError("missing"),
    )

    result = _publish(parts)

    assert result.status is (
        PreparedCoverLetterMaterialStatus.DEFERRED_COMPILER_UNAVAILABLE
    )
    assert result.reason_code is (
        PreparedCoverLetterMaterialFailureReason.COMPILER_UNAVAILABLE
    )
    assert result.retryable
    assert parts["compiler"].compile_calls == []
    assert not _material_records(parts)
    assert not _pdf_artifacts(parts)


@pytest.mark.parametrize(
    "compile_status",
    (
        LatexCompileStatus.COMPILATION_ERROR,
        LatexCompileStatus.TIMEOUT,
        LatexCompileStatus.OUTPUT_INVALID,
    ),
)
def test_compilation_errors_defer_without_draft_mutation(
    tmp_path: Path, compile_status: LatexCompileStatus
) -> None:
    parts = _setup(tmp_path)
    original = parts["draft"].to_dict()
    parts["compiler"] = _FakeCompiler(b"unused", status=compile_status)

    result = _publish(parts)

    assert result.status is (
        PreparedCoverLetterMaterialStatus.DEFERRED_COMPILATION_ERROR
    )
    assert result.reason_code is (
        PreparedCoverLetterMaterialFailureReason.COMPILATION_ERROR
    )
    assert parts["draft"].to_dict() == original
    assert len(parts["compiler"].compile_calls) == 1
    assert not _material_records(parts)
    assert not _pdf_artifacts(parts)


def test_multi_page_pdf_defers_overflow_without_publishing_pdf(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    parts["compiler"] = _FakeCompiler(
        _pdf(
            text=expected_cover_letter_text_projection(parts["draft"]),
            pages=2,
        )
    )

    result = _publish(parts)

    assert result.status is (
        PreparedCoverLetterMaterialStatus.DEFERRED_LAYOUT_OVERFLOW
    )
    assert result.reason_code is (
        PreparedCoverLetterMaterialFailureReason.LAYOUT_OVERFLOW
    )
    assert result.stopped_source_lineage is not None
    assert (
        result.stopped_source_lineage.source_artifact_content_hash
        is not None
    )
    assert result.compiler_started
    assert not _material_records(parts)
    assert not _pdf_artifacts(parts)


@pytest.mark.parametrize(
    "text_transform",
    (
        lambda value: value.replace("C++ experience.", ""),
        lambda value: f"{value} {value}",
        lambda value: f"{value} UNKNOWN VISIBLE CONTENT",
        lambda value: value.replace("Synthetic_Candidate", "[Candidate]"),
    ),
)
def test_pdf_text_mismatch_or_placeholder_fails_closed(
    tmp_path: Path, text_transform
) -> None:
    parts = _setup(tmp_path)
    expected = expected_cover_letter_text_projection(parts["draft"])
    parts["compiler"] = _FakeCompiler(_pdf(text=text_transform(expected)))

    result = _publish(parts)

    assert result.status is PreparedCoverLetterMaterialStatus.FAILED
    assert result.reason_code is (
        PreparedCoverLetterMaterialFailureReason.PDF_TEXT_MISMATCH
    )
    assert not _material_records(parts)
    assert not _pdf_artifacts(parts)


def test_pdf_extractor_word_boundary_loss_preserves_content_fidelity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = _setup(tmp_path)
    expected = expected_cover_letter_text_projection(parts["draft"])
    extracted = expected.replace(" the R&D role ", "the R&D role")
    assert extracted != expected
    real_inspector = material_module.inspect_cover_letter_pdf

    def extractor_with_lost_word_boundaries(content: bytes):
        inspected = real_inspector(content)
        assert inspected is not None
        return inspected[0], extracted

    monkeypatch.setattr(
        material_module,
        "inspect_cover_letter_pdf",
        extractor_with_lost_word_boundaries,
    )
    parts["compiler"] = _FakeCompiler(_pdf(text=expected))

    result = _publish(parts)

    assert result.status is PreparedCoverLetterMaterialStatus.CREATED
    assert result.material is not None
    assert result.material.page_count == 1


def test_invalid_pdf_signature_fails_closed(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    parts["compiler"] = _FakeCompiler(b"not-a-pdf")

    result = _publish(parts)

    assert result.status is (
        PreparedCoverLetterMaterialStatus.DEFERRED_COMPILATION_ERROR
    )
    assert result.reason_code is (
        PreparedCoverLetterMaterialFailureReason.PDF_INVALID
    )
    assert not _material_records(parts)
    assert not _pdf_artifacts(parts)


def test_completed_binding_replays_without_compiling_and_preserves_time(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _publish(parts)
    assert first.material is not None
    later = replace(
        PublishPreparedCoverLetterCommand(
            subject_id=parts["plan"].subject_id,
            application_plan_id=parts["plan"].plan_id,
            cover_letter_fact_qa_result_id=parts["qa"].result_id,
            now=NOW,
        ),
        now=NOW + timedelta(days=1),
    )

    replay = _publish(parts, command=later)

    assert replay.status is PreparedCoverLetterMaterialStatus.UNCHANGED
    assert replay.material is not None
    assert replay.material.material_id == first.material.material_id
    assert replay.material.published_at == NOW
    assert (
        prepared_cover_letter_publication_public_result(first).outcome
        is PreparationStageOutcome.COMPLETED
    )
    assert (
        prepared_cover_letter_publication_public_result(replay).outcome
        is PreparationStageOutcome.UNCHANGED
    )
    assert len(parts["compiler"].compile_calls) == 1
    assert len(_material_records(parts)) == 1
    assert len(_pdf_artifacts(parts)) == 1


def test_changed_compiler_or_template_creates_new_material(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _publish(parts)
    assert first.material is not None

    parts["compiler"] = _FakeCompiler(
        _pdf(text=expected_cover_letter_text_projection(parts["draft"])),
        compiler_version="pdfTeX 3.141592653 (synthetic-v2)",
    )
    second = _publish(parts)
    assert second.status is PreparedCoverLetterMaterialStatus.CREATED
    assert second.material is not None
    assert second.material.material_id != first.material.material_id

    variant_source = (
        "% managed deterministic template revision\n"
        + MANAGED_COVER_LETTER_TEMPLATE_SOURCE
    )
    variant = ManagedCoverLetterTemplate(
        template_id=MANAGED_COVER_LETTER_TEMPLATE_ID,
        template_version=MANAGED_COVER_LETTER_TEMPLATE_VERSION,
        template_source=variant_source,
        template_sha256=hashlib.sha256(
            variant_source.encode("utf-8")
        ).hexdigest(),
    )
    parts["template_provider"] = _TemplateProvider(variant)
    third = _publish(parts)

    assert third.status is PreparedCoverLetterMaterialStatus.CREATED
    assert third.material is not None
    assert third.material.material_id not in {
        first.material.material_id,
        second.material.material_id,
    }
    assert len(parts["compiler"].compile_calls) == 2
    assert len(_material_records(parts)) == 3


def test_changed_draft_and_fact_qa_create_new_material(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _publish(parts)
    changed_draft = _draft(
        parts["plan"], version="v2", paragraph_suffix=" exactly"
    )
    parts["draft_repository"].save(changed_draft)
    changed_qa = _fact_qa(changed_draft, version="v2")
    parts["qa_repository"].save(changed_qa)
    parts["draft"] = changed_draft
    parts["qa"] = changed_qa
    parts["compiler"] = _FakeCompiler(
        _pdf(text=expected_cover_letter_text_projection(changed_draft))
    )

    second = _publish(parts)

    assert first.material is not None
    assert second.status is PreparedCoverLetterMaterialStatus.CREATED
    assert second.material is not None
    assert second.material.material_id != first.material.material_id
    assert len(_material_records(parts)) == 2


def test_restart_reads_same_hashes_and_canonical_hash(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    created = _publish(parts)
    assert created.material is not None

    restarted = PrivateHomePreparedCoverLetterMaterialRepository(
        PrivateHome(parts["home"].root)
    ).get(
        subject_id=SUBJECT_ID,
        material_id=created.material.material_id,
    )

    assert restarted.status is PreparedCoverLetterMaterialReadStatus.FOUND
    assert restarted.material is not None
    assert restarted.material.latex_source_sha256 == (
        created.material.latex_source_sha256
    )
    assert restarted.material.pdf_sha256 == created.material.pdf_sha256
    assert restarted.material.material_content_hash == (
        created.material.material_content_hash
    )


def test_artifact_drift_fails_replay_without_overwrite(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    created = _publish(parts)
    assert created.material is not None
    pdf_path = parts["home"].contained_path(created.material.pdf_reference)
    original = pdf_path.read_bytes()
    parts["home"].write_bytes(pdf_path, original + b"\n% drift")

    replay = _publish(parts)

    assert replay.status is PreparedCoverLetterMaterialStatus.FAILED
    assert replay.reason_code is (
        PreparedCoverLetterMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE
    )
    assert len(parts["compiler"].compile_calls) == 1
    assert pdf_path.read_bytes() == original + b"\n% drift"


def test_record_corruption_fails_replay_without_overwrite(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    created = _publish(parts)
    assert created.material is not None
    record = _material_records(parts)[0]
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["material_content_hash"] = "0" * 64
    parts["home"].write_bytes(
        record,
        (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
    )

    replay = _publish(parts)

    assert replay.status is PreparedCoverLetterMaterialStatus.FAILED
    assert replay.reason_code is (
        PreparedCoverLetterMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE
    )
    assert len(parts["compiler"].compile_calls) == 1


def test_subject_isolation_rejects_cross_subject_material_reads(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    created = _publish(parts)
    assert created.material is not None

    cross = parts["material_repository"].get(
        subject_id="synthetic-subject-b",
        material_id=created.material.material_id,
    )

    assert cross.status is PreparedCoverLetterMaterialReadStatus.NOT_FOUND


@pytest.mark.skipif(
    shutil.which("pdflatex") is None,
    reason="the optional real pdflatex engine is not installed",
)
def test_real_sandboxed_pdflatex_preserves_escaped_visible_text(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    parts["compiler"] = SandboxedPdfLatexCompiler()

    result = _publish(parts)

    assert result.status is PreparedCoverLetterMaterialStatus.CREATED
    assert result.material is not None
    pdf = parts["home"].contained_path(result.material.pdf_reference)
    assert cover_letter_pdf_text_is_faithful(
        pdf.read_bytes(), parts["draft"]
    )


def test_p2b2d_has_no_agent_manifest_browser_or_subprocess_dependency() -> None:
    path = (
        Path(__file__).parents[1]
        / "core"
        / "prepared_cover_letter_material.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    text = path.read_text(encoding="utf-8")

    assert not {
        "subprocess",
        "core.application_engine",
        "core.plan_material_manifest",
        "core.browser_broker",
    } & imports
    assert "AgentPort" not in text
    assert "PlanMaterialManifest" not in text
    assert "ApplicationEngine" not in text
    assert ".compile(" in text
    assert "LatexCompilerPort" in text
