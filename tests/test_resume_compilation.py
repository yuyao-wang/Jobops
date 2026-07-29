from __future__ import annotations

import ast
import hashlib
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.resume_compilation as compilation_module
from core.latex_compiler import (
    LATEX_COMPILE_POLICY_VERSION,
    LATEX_SANDBOX_POLICY_VERSION,
    MAX_DIAGNOSTIC_CHARS,
    LatexCompileOutcome,
    LatexCompileRequest,
    LatexCompileStatus,
    LatexCompilerDescription,
    LatexCompilerUnavailableError,
    SandboxedPdfLatexCompiler,
    normalized_compile_flags,
    redact_diagnostics,
    sandbox_environment,
)
from core.managed_resume_template import DefaultManagedResumeTemplateProvider
from core.private_home import PrivateHome
from core.resume_compilation import (
    CompileResumeLatexCommand,
    PrivateHomeResumeCompilationRepository,
    ResumeCompilationFailureReason,
    ResumeCompilationReadStatus,
    ResumeCompilationStatus,
    ResumeCompilationWriteStatus,
    compile_resume_latex,
    pdf_page_count,
    unmanaged_file_dependencies,
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


NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
LATEX = (
    "\\documentclass[11pt]{article}\n"
    f"{MARKER_MACRO_DEFINITIONS}"
    "\\begin{document}\n"
    f"{JOBOPS_CONTENT_BEGIN}\n"
    "\\JobopsSection{resume-section-a}{Experience}\n"
    "\\begin{itemize}\n"
    "\\JobopsBullet{resume-block-a}{Built deterministic geospatial pipelines.}\n"
    "\\end{itemize}\n"
    f"{JOBOPS_CONTENT_END}\n"
    "\\end{document}\n"
)


def _pdf(pages: int = 1) -> bytes:
    """A genuinely parseable minimal PDF, so page counting is exercised."""

    kids = " ".join(f"{3 + index} 0 R" for index in range(pages))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>",
        *(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>"
            for _ in range(pages)
        ),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n{body}\nendobj\n".encode("ascii")
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


def _hashed(value: dict) -> str:
    import json

    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


class _FakeCompiler:
    """A fake compiler; describe() is cheap and compile() is the only run."""

    def __init__(
        self,
        outcome: LatexCompileOutcome | Exception | None = None,
        *,
        compiler_version: str = "pdfTeX 3.141592653-2.6-1.40.25 (fake)",
        describe_error: Exception | None = None,
    ) -> None:
        self.outcome = outcome
        self.compiler_version = compiler_version
        self.describe_error = describe_error
        self.compile_calls: list[LatexCompileRequest] = []
        self.describe_calls = 0

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
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.outcome is None:
            return LatexCompileOutcome(
                status=LatexCompileStatus.SUCCEEDED,
                pdf_bytes=_pdf(),
                diagnostics="",
                exit_code=0,
                compiler_started=True,
            )
        return self.outcome


def _setup(tmp_path: Path, *, subject_id: str = "subject-a", source: str = LATEX):
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    latex_repository = PrivateHomeResumeLatexVersionRepository(home)
    version = register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id=subject_id,
            source_kind=ResumeLatexSourceKind.SYSTEM_TEMPLATE_DERIVED,
            now=NOW,
            latex_source=source,
            template_id="managed-resume-one-page-v1",
            template_sha256="a" * 64,
            tailored_resume_draft_id="tailored-resume-draft-" + "b" * 64,
            tailored_resume_draft_hash="c" * 64,
            fact_qa_result_id="resume-fact-qa-" + "d" * 64,
            fact_qa_result_hash="e" * 64,
        ),
        home=home,
        repository=latex_repository,
    ).version
    assert version is not None

    binding = _hashed({"construction": subject_id, "version": version.latex_version_id})
    record = ResumeLatexConstructionRecord(
        record_id=f"resume-latex-construction-{binding}",
        contract_version=RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION,
        construction_binding=binding,
        subject_id=subject_id,
        application_plan_id="application-plan-" + "f" * 64,
        tailored_resume_draft_id=version.tailored_resume_draft_id,
        tailored_resume_draft_hash=version.tailored_resume_draft_hash,
        fact_qa_result_id=version.fact_qa_result_id,
        fact_qa_result_hash=version.fact_qa_result_hash,
        base_latex_selection_decision_id=(
            "base-latex-selection-" + "0" * 64
        ),
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
    assert construction_repository.save(record).record is not None
    return {
        "home": home,
        "version": version,
        "record": record,
        "latex_repository": latex_repository,
        "construction_repository": construction_repository,
        "compilation_repository": PrivateHomeResumeCompilationRepository(home),
    }


def _compile(
    parts,
    compiler,
    *,
    subject_id: str = "subject-a",
    version_id: str | None = None,
    now: datetime = NOW,
):
    return compile_resume_latex(
        CompileResumeLatexCommand(
            subject_id=subject_id,
            resume_latex_construction_record_id=parts["record"].record_id,
            resume_latex_version_id=(
                version_id or parts["version"].latex_version_id
            ),
            now=now,
        ),
        construction_repository=parts["construction_repository"],
        latex_version_repository=parts["latex_repository"],
        compiler=compiler,
        compilation_repository=parts["compilation_repository"],
        home=parts["home"],
    )


def _records(parts) -> tuple[Path, ...]:
    return tuple(parts["home"].paths.resume_compilations.rglob("*.json"))


def _artifacts(parts) -> tuple[Path, ...]:
    return tuple(parts["home"].paths.compiled_resumes.rglob("*.pdf"))


def test_valid_source_compiles_into_an_immutable_record(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    compiler = _FakeCompiler()

    result = _compile(parts, compiler)

    assert result.status is ResumeCompilationStatus.CREATED
    record = result.record
    assert record.subject_id == "subject-a"
    assert record.construction_record_id == parts["record"].record_id
    assert record.construction_binding == parts["record"].construction_binding
    assert record.latex_version_id == parts["version"].latex_version_id
    assert record.latex_source_sha256 == parts["version"].source_sha256
    assert record.compiler_engine == "pdflatex"
    assert record.compile_policy_version == LATEX_COMPILE_POLICY_VERSION
    assert record.sandbox_policy_version == LATEX_SANDBOX_POLICY_VERSION
    assert record.normalized_flags == normalized_compile_flags()
    assert record.page_count == 1
    assert record.pdf_byte_size == len(_pdf())
    assert record.compiled_at == NOW
    assert len(compiler.compile_calls) == 1


def test_pdf_is_stored_managed_and_hashed_from_actual_bytes(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    result = _compile(parts, _FakeCompiler())

    record = result.record
    stored = parts["home"].contained_path(record.pdf_reference)
    content = stored.read_bytes()
    assert stored.is_file()
    assert content == _pdf()
    assert hashlib.sha256(content).hexdigest() == record.pdf_sha256
    assert stored.name == f"{record.pdf_sha256}.pdf"
    assert "compiled-resumes" in record.pdf_reference


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (
            "version_id",
            ResumeCompilationFailureReason.CONSTRUCTION_BINDING_MISMATCH,
        ),
        (
            "source_hash",
            ResumeCompilationFailureReason.LATEX_VERSION_BINDING_MISMATCH,
        ),
        (
            "subject",
            ResumeCompilationFailureReason.CONSTRUCTION_RECORD_NOT_FOUND,
        ),
    ],
)
def test_binding_mismatch_fails_before_the_compiler_runs(
    tmp_path: Path, damage: str, reason: ResumeCompilationFailureReason
) -> None:
    parts = _setup(tmp_path)
    compiler = _FakeCompiler()
    subject = "subject-a"
    version_id = None
    if damage == "version_id":
        version_id = "resume-latex-version-" + "9" * 64
    elif damage == "source_hash":
        object.__setattr__(
            parts["record"], "latex_source_sha256", "f" * 64
        )

        class _DamagedRepository:
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

        parts["construction_repository"] = _DamagedRepository(
            parts["record"]
        )
    else:
        subject = "subject-b"

    result = _compile(
        parts, compiler, subject_id=subject, version_id=version_id
    )

    assert result.status is ResumeCompilationStatus.FAILED
    assert result.reason_code is reason
    assert compiler.compile_calls == []
    assert not result.compiler_started
    assert not _records(parts)
    assert not _artifacts(parts)


def test_source_hash_drift_and_capability_regression_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    source_path = parts["home"].contained_path(
        parts["version"].source_reference
    )
    source_path.write_text(LATEX + "\n% drifted\n", encoding="utf-8")
    compiler = _FakeCompiler()

    drifted = _compile(parts, compiler)

    # The registry re-verifies its own managed source first, so drift is
    # caught one layer earlier; this Slice's hash check is defence in depth.
    assert drifted.reason_code is (
        ResumeCompilationFailureReason.LATEX_VERSION_INTEGRITY_FAILURE
    )
    assert compiler.compile_calls == []

    class _PassThroughVersionsEarly:
        def __init__(self, version) -> None:
            self.version = version

        def get(self, **_kwargs):
            from core.resume_latex_versions import (
                ResumeLatexVersionReadResult,
                ResumeLatexVersionReadStatus,
            )

            return ResumeLatexVersionReadResult(
                status=ResumeLatexVersionReadStatus.FOUND,
                version=self.version,
            )

    lenient = dict(parts)
    lenient["latex_repository"] = _PassThroughVersionsEarly(
        parts["version"]
    )
    own_check = _compile(lenient, compiler)

    assert own_check.reason_code is (
        ResumeCompilationFailureReason.SOURCE_HASH_DRIFT
    )
    assert compiler.compile_calls == []

    dangerous = LATEX.replace(
        "\\begin{document}", "\\immediate\\write18{id}\n\\begin{document}"
    )
    source_path.write_bytes(dangerous.encode("utf-8"))
    object.__setattr__(
        parts["version"],
        "source_sha256",
        hashlib.sha256(dangerous.encode("utf-8")).hexdigest(),
    )
    object.__setattr__(
        parts["record"],
        "latex_source_sha256",
        parts["version"].source_sha256,
    )

    class _PassThroughVersions:
        def __init__(self, version) -> None:
            self.version = version

        def get(self, **_kwargs):
            from core.resume_latex_versions import (
                ResumeLatexVersionReadResult,
                ResumeLatexVersionReadStatus,
            )

            return ResumeLatexVersionReadResult(
                status=ResumeLatexVersionReadStatus.FOUND,
                version=self.version,
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

    parts["latex_repository"] = _PassThroughVersions(parts["version"])
    parts["construction_repository"] = _PassThroughConstruction(
        parts["record"]
    )
    rejected = _compile(parts, compiler)

    assert rejected.reason_code is (
        ResumeCompilationFailureReason.SOURCE_CAPABILITY_REJECTED
    )
    assert compiler.compile_calls == []


def test_compiler_unavailable_defers_without_artifact(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    compiler = _FakeCompiler(
        describe_error=LatexCompilerUnavailableError("no engine")
    )

    result = _compile(parts, compiler)

    assert result.status is (
        ResumeCompilationStatus.DEFERRED_COMPILER_UNAVAILABLE
    )
    assert (
        result.reason_code
        is ResumeCompilationFailureReason.COMPILER_UNAVAILABLE
    )
    assert compiler.compile_calls == []
    assert not _artifacts(parts)
    assert not _records(parts)


def test_unmanaged_dependency_defers_without_running_the_compiler(
    tmp_path: Path,
) -> None:
    parts = _setup(
        tmp_path,
        source=LATEX.replace(
            "\\end{document}",
            "\\input{sections/extra.tex}\n\\end{document}",
        ),
    )
    compiler = _FakeCompiler()

    result = _compile(parts, compiler)

    assert result.status is (
        ResumeCompilationStatus.DEFERRED_SOURCE_INCOMPLETE
    )
    assert (
        result.reason_code
        is ResumeCompilationFailureReason.UNMANAGED_DEPENDENCY
    )
    assert "input" in result.diagnostics
    assert compiler.compile_calls == []
    assert compiler.describe_calls == 0
    assert not _artifacts(parts)


def test_compilation_error_defers_with_bounded_clean_diagnostics(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    noisy = (
        "! Undefined control sequence.\n"
        f"l.12 at {Path.home()}/private/secret/resume.tex\n"
        "/usr/local/texlive/2025/tex/latex/base/article.cls loaded\n"
    ) * 400
    compiler = _FakeCompiler(
        LatexCompileOutcome(
            status=LatexCompileStatus.COMPILATION_ERROR,
            pdf_bytes=None,
            diagnostics=redact_diagnostics(noisy),
            exit_code=1,
            compiler_started=True,
        )
    )

    result = _compile(parts, compiler)

    assert result.status is (
        ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR
    )
    assert (
        result.reason_code
        is ResumeCompilationFailureReason.COMPILATION_ERROR
    )
    assert result.compiler_started
    assert len(result.diagnostics) <= MAX_DIAGNOSTIC_CHARS
    assert str(Path.home()) not in result.diagnostics
    assert "/usr/local/texlive" not in result.diagnostics
    assert "Undefined control sequence" in result.diagnostics
    assert not _artifacts(parts)
    assert not _records(parts)


def test_timeout_defers_and_records_that_the_compiler_started(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    compiler = _FakeCompiler(
        LatexCompileOutcome(
            status=LatexCompileStatus.TIMEOUT,
            pdf_bytes=None,
            diagnostics="The LaTeX engine exceeded the compile timeout.",
            exit_code=None,
            compiler_started=True,
        )
    )

    result = _compile(parts, compiler)

    assert result.status is (
        ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR
    )
    assert (
        result.reason_code
        is ResumeCompilationFailureReason.COMPILATION_TIMEOUT
    )
    assert result.compiler_started
    assert not _records(parts)


@pytest.mark.parametrize(
    "payload",
    [b"", b"not-a-pdf at all", b"%PDF-1.7\ntrailer\n%%EOF\n"],
)
def test_invalid_pdf_never_creates_a_successful_record(
    tmp_path: Path, payload: bytes
) -> None:
    parts = _setup(tmp_path)
    if payload:
        outcome = LatexCompileOutcome(
            status=LatexCompileStatus.SUCCEEDED,
            pdf_bytes=payload,
            diagnostics="",
            exit_code=0,
            compiler_started=True,
        )
    else:
        outcome = LatexCompileOutcome(
            status=LatexCompileStatus.OUTPUT_INVALID,
            pdf_bytes=None,
            diagnostics="The engine exited successfully without a PDF.",
            exit_code=0,
            compiler_started=True,
        )

    result = _compile(parts, _FakeCompiler(outcome))

    assert result.status is (
        ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR
    )
    assert result.record is None
    assert not _artifacts(parts)
    assert not _records(parts)


def test_multi_page_pdf_is_recorded_without_a_one_page_rule(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    compiler = _FakeCompiler(
        LatexCompileOutcome(
            status=LatexCompileStatus.SUCCEEDED,
            pdf_bytes=_pdf(pages=3),
            diagnostics="",
            exit_code=0,
            compiler_started=True,
        )
    )

    result = _compile(parts, compiler)

    assert result.status is ResumeCompilationStatus.CREATED
    assert result.record.page_count == 3


def test_only_the_pdf_reaches_the_managed_artifact_directory(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    _compile(parts, _FakeCompiler())

    stored = tuple(parts["home"].paths.compiled_resumes.rglob("*"))
    files = [item for item in stored if item.is_file()]
    assert len(files) == 1
    assert files[0].suffix == ".pdf"
    for suffix in (".aux", ".log", ".fls", ".out", ".synctex.gz"):
        assert not tuple(
            parts["home"].paths.compiled_resumes.rglob(f"*{suffix}")
        )


def test_replay_returns_unchanged_with_zero_extra_compiler_runs(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    compiler = _FakeCompiler()

    first = _compile(parts, compiler)
    replay = _compile(parts, compiler, now=NOW + timedelta(days=3))

    assert first.status is ResumeCompilationStatus.CREATED
    assert replay.status is ResumeCompilationStatus.UNCHANGED
    assert replay.record == first.record
    assert replay.record.compiled_at == NOW
    assert not replay.compiler_started
    assert len(compiler.compile_calls) == 1
    assert len(_artifacts(parts)) == 1
    assert len(_records(parts)) == 1


def test_changed_compiler_version_creates_a_new_record(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _compile(parts, _FakeCompiler())

    second = _compile(
        parts,
        _FakeCompiler(compiler_version="pdfTeX 3.141592653-2.6-1.40.26"),
        now=NOW + timedelta(minutes=5),
    )

    assert second.status is ResumeCompilationStatus.CREATED
    assert second.record.record_id != first.record.record_id
    assert second.compilation_binding != first.compilation_binding
    assert len(_records(parts)) == 2
    kept = parts["compilation_repository"].get(
        subject_id="subject-a", record_id=first.record.record_id
    )
    assert kept.record == first.record


def test_artifact_drift_and_corrupt_record_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _compile(parts, _FakeCompiler())
    record = first.record
    artifact = parts["home"].contained_path(record.pdf_reference)
    artifact.write_bytes(b"%PDF-1.7\ntampered\n%%EOF\n")

    drifted = parts["compilation_repository"].get(
        subject_id="subject-a", record_id=record.record_id
    )
    conflict = parts["compilation_repository"].save(record)

    assert drifted.status is ResumeCompilationReadStatus.INTEGRITY_FAILURE
    assert conflict.status is ResumeCompilationWriteStatus.FAILED
    assert (
        conflict.reason_code
        is ResumeCompilationFailureReason.RECORD_INTEGRITY_FAILURE
    )

    record_path = next(
        parts["home"].paths.resume_compilations.rglob(
            f"{record.record_id}.json"
        )
    )
    corrupted = b"{broken"
    record_path.write_bytes(corrupted)
    corrupt = parts["compilation_repository"].get(
        subject_id="subject-a", record_id=record.record_id
    )
    assert corrupt.status is ResumeCompilationReadStatus.INTEGRITY_FAILURE
    assert record_path.read_bytes() == corrupted


def test_restart_and_subject_isolation(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    first = _compile(parts, _FakeCompiler())

    restarted = PrivateHomeResumeCompilationRepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(
        subject_id="subject-a", record_id=first.record.record_id
    )
    cross = restarted.get(
        subject_id="subject-b", record_id=first.record.record_id
    )

    assert read.status is ResumeCompilationReadStatus.FOUND
    assert read.record == first.record
    assert read.record.pdf_sha256 == first.record.pdf_sha256
    assert cross.status is ResumeCompilationReadStatus.NOT_FOUND


def test_invalid_command_fails_without_side_effects(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    compiler = _FakeCompiler()

    naive = _compile(parts, compiler, now=datetime(2026, 7, 29, 15, 0))

    assert naive.status is ResumeCompilationStatus.FAILED
    assert (
        naive.reason_code
        is ResumeCompilationFailureReason.INVALID_REQUEST
    )
    assert compiler.compile_calls == []
    assert not _records(parts)


def test_page_count_and_dependency_helpers_are_deterministic() -> None:
    assert pdf_page_count(_pdf(pages=1)) == 1
    assert pdf_page_count(_pdf(pages=4)) == 4
    assert pdf_page_count(b"%PDF-1.7\nnot really a document\n") == 0
    assert pdf_page_count(b"") == 0
    assert unmanaged_file_dependencies(LATEX) == ()
    assert unmanaged_file_dependencies(
        "\\input{a.tex}\n\\includegraphics{b.png}\n"
    ) == ("input", "includegraphics")
    assert unmanaged_file_dependencies("\\usepackage{geometry}") == ()


def test_sandbox_environment_is_minimal_and_deterministic(
    tmp_path: Path,
) -> None:
    env = sandbox_environment(tmp_path)

    assert env["HOME"] == str(tmp_path)
    assert env["TZ"] == "UTC"
    assert env["SOURCE_DATE_EPOCH"] == "0"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["shell_escape"] == "f"
    assert env["openout_any"] == "p"
    assert set(env) <= {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TZ",
        "SOURCE_DATE_EPOCH",
        "TEXMFHOME",
        "TEXMFVAR",
        "TEXMFCONFIG",
        "openout_any",
        "openin_any",
        "shell_escape",
    }


def test_redaction_removes_home_and_absolute_paths() -> None:
    text = (
        f"error at {Path.home()}/Documents/private/resume.tex line 3 "
        "and /usr/local/texlive/2025/tex/latex/base/article.cls"
    )

    cleaned = redact_diagnostics(text, sandbox="/tmp/jobops-latex-xyz")

    assert str(Path.home()) not in cleaned
    assert "/usr/local/texlive" not in cleaned
    assert "line 3" in cleaned


@pytest.mark.skipif(
    not hasattr(os, "fork"), reason="POSIX-only sandbox behaviour"
)
def test_real_subprocess_sandbox_isolates_env_cwd_and_shell(
    tmp_path: Path,
) -> None:
    """Drive the real adapter with a controlled fake executable."""

    fake = tmp_path / "fake-pdflatex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, pathlib\n"
        "out = None\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg.startswith('-output-directory='):\n"
        "        out = arg.split('=', 1)[1]\n"
        "print('cwd=' + os.getcwd())\n"
        "print('secret=' + os.environ.get('JOBOPS_TEST_SECRET', 'absent'))\n"
        "print('shell_escape=' + os.environ.get('shell_escape', 'unset'))\n"
        "print('argv=' + repr(sys.argv[1:]))\n"
        "pathlib.Path(out, 'resume.pdf').write_bytes("
        "b'%PDF-1.7\\n1 0 obj\\n<< /Type /Page >>\\nendobj\\ntrailer\\n%%EOF\\n')\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    os.environ["JOBOPS_TEST_SECRET"] = "must-not-leak"
    try:
        compiler = SandboxedPdfLatexCompiler(executable=fake)
        outcome = compiler.compile(LatexCompileRequest(latex_source=LATEX))
    finally:
        os.environ.pop("JOBOPS_TEST_SECRET", None)

    assert outcome.status is LatexCompileStatus.SUCCEEDED
    assert outcome.pdf_bytes.startswith(b"%PDF-")
    assert "secret=absent" in outcome.diagnostics
    assert "shell_escape=f" in outcome.diagnostics
    assert "-no-shell-escape" in outcome.diagnostics
    assert "-halt-on-error" in outcome.diagnostics
    assert "cwd=<sandbox>" in outcome.diagnostics
    assert str(tmp_path) not in outcome.diagnostics.replace(
        str(fake), ""
    ) or "<path>" in outcome.diagnostics


@pytest.mark.skipif(
    not hasattr(os, "fork"), reason="POSIX-only sandbox behaviour"
)
def test_real_subprocess_failure_is_a_compilation_error(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "failing-pdflatex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('! Undefined control sequence.')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    compiler = SandboxedPdfLatexCompiler(executable=fake)
    outcome = compiler.compile(LatexCompileRequest(latex_source=LATEX))

    assert outcome.status is LatexCompileStatus.COMPILATION_ERROR
    assert outcome.pdf_bytes is None
    assert outcome.compiler_started
    assert "Undefined control sequence" in outcome.diagnostics


@pytest.mark.skipif(
    not hasattr(os, "fork"), reason="POSIX-only sandbox behaviour"
)
def test_real_subprocess_timeout_terminates_the_child(
    tmp_path: Path,
) -> None:
    fake = tmp_path / "hanging-pdflatex"
    fake.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    compiler = SandboxedPdfLatexCompiler(executable=fake, timeout_seconds=1)
    outcome = compiler.compile(LatexCompileRequest(latex_source=LATEX))

    assert outcome.status is LatexCompileStatus.TIMEOUT
    assert outcome.pdf_bytes is None
    assert outcome.compiler_started


def test_missing_executable_reports_unavailable(tmp_path: Path) -> None:
    compiler = SandboxedPdfLatexCompiler(
        executable=tmp_path / "does-not-exist"
    )

    outcome = compiler.compile(LatexCompileRequest(latex_source=LATEX))

    assert outcome.status is LatexCompileStatus.UNAVAILABLE
    assert not outcome.compiler_started
    with pytest.raises(LatexCompilerUnavailableError):
        compiler.describe()


@pytest.mark.skipif(
    shutil.which("pdflatex") is None,
    reason="a real LaTeX engine is not installed",
)
def test_optional_real_pdflatex_end_to_end(tmp_path: Path) -> None:
    """Runs only when a real engine exists; never a routine-suite dependency."""

    parts = _setup(tmp_path)
    compiler = SandboxedPdfLatexCompiler()

    result = _compile(parts, compiler)

    assert result.status is ResumeCompilationStatus.CREATED
    assert result.record.page_count >= 1
    stored = parts["home"].contained_path(result.record.pdf_reference)
    assert stored.read_bytes().startswith(b"%PDF-")


def test_modules_never_use_shell_or_reach_execution_surfaces() -> None:
    for module in (compilation_module, sys.modules["core.latex_compiler"]):
        text = Path(module.__file__).read_text(encoding="utf-8")
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
            "core.materials",
            "core.resume_fact_qa",
            "playwright",
            "requests",
            "urllib",
            "httpx",
        }
        assert not any(
            imported == item or imported.startswith(f"{item}.")
            for imported in imports
            for item in forbidden
        )
        assert "shell=True" not in text
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                keywords = {
                    keyword.arg for keyword in node.keywords
                }
                assert "shell" in keywords
                assert "timeout" in keywords
