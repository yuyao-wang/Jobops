"""Bounded, shell-free LaTeX compilation inside a disposable sandbox."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable


LATEX_COMPILE_POLICY_VERSION = "latex-compile-policy-v1"
LATEX_SANDBOX_POLICY_VERSION = "latex-sandbox-policy-v1"

#: V1 supports exactly one engine. There is no recommendation or fallback.
ALLOWED_LATEX_ENGINES = ("pdflatex",)
DEFAULT_LATEX_ENGINE = "pdflatex"

SANDBOX_SOURCE_STEM = "resume"
DEFAULT_COMPILE_TIMEOUT_SECONDS = 60
MAX_COMPILER_PASSES = 1
MAX_DIAGNOSTIC_CHARS = 8_000
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_SANDBOX_FILES = 64
MAX_SANDBOX_FILE_BYTES = 32 * 1024 * 1024
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_CPU_SECONDS = 120
MAX_ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024

_ABSOLUTE_PATH_PATTERN = re.compile(r"(?:/[A-Za-z0-9._+\-]+){2,}/?")
_VERSION_LINE_PATTERN = re.compile(r"^[^\n]{0,200}")


class LatexCompileStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    COMPILATION_ERROR = "COMPILATION_ERROR"
    TIMEOUT = "TIMEOUT"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    UNAVAILABLE = "UNAVAILABLE"


class LatexCompilerUnavailableError(RuntimeError):
    """The allowlisted compiler cannot be located or described."""


@dataclass(frozen=True, slots=True)
class LatexCompilerDescription:
    """Compiler identity used for the compilation binding, before any run."""

    engine: str
    compiler_version: str
    normalized_flags: tuple[str, ...]
    compile_policy_version: str
    sandbox_policy_version: str

    def __post_init__(self) -> None:
        if self.engine not in ALLOWED_LATEX_ENGINES:
            raise ValueError("engine is not allowlisted")
        if (
            not isinstance(self.compiler_version, str)
            or not self.compiler_version.strip()
            or len(self.compiler_version) > 200
        ):
            raise ValueError("compiler_version is invalid")
        if not isinstance(self.normalized_flags, tuple) or any(
            not isinstance(flag, str) or not flag.strip()
            for flag in self.normalized_flags
        ):
            raise TypeError("normalized_flags must be a tuple of strings")
        if self.compile_policy_version != LATEX_COMPILE_POLICY_VERSION:
            raise ValueError("compile policy version is unsupported")
        if self.sandbox_policy_version != LATEX_SANDBOX_POLICY_VERSION:
            raise ValueError("sandbox policy version is unsupported")


@dataclass(frozen=True, slots=True)
class LatexCompileRequest:
    latex_source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.latex_source, str)
            or not self.latex_source.strip()
        ):
            raise ValueError("latex_source must be non-empty text")


@dataclass(frozen=True, slots=True)
class LatexCompileOutcome:
    status: LatexCompileStatus
    pdf_bytes: bytes | None
    diagnostics: str
    exit_code: int | None
    compiler_started: bool

    def __post_init__(self) -> None:
        status = LatexCompileStatus(self.status)
        object.__setattr__(self, "status", status)
        if not isinstance(self.diagnostics, str):
            raise TypeError("diagnostics must be a string")
        if len(self.diagnostics) > MAX_DIAGNOSTIC_CHARS:
            raise ValueError("diagnostics exceed the bounded size")
        if type(self.compiler_started) is not bool:
            raise TypeError("compiler_started must be a boolean")
        if status is LatexCompileStatus.SUCCEEDED:
            if not isinstance(self.pdf_bytes, bytes) or not self.pdf_bytes:
                raise ValueError("a successful compile must return PDF bytes")
            if not self.compiler_started:
                raise ValueError("a successful compile must have started")
        elif self.pdf_bytes is not None:
            raise ValueError("an unsuccessful compile cannot return a PDF")


@runtime_checkable
class LatexCompilerPort(Protocol):
    def describe(self) -> LatexCompilerDescription:
        """Return compiler identity without compiling anything."""

    def compile(self, request: LatexCompileRequest) -> LatexCompileOutcome:
        """Compile one source inside a disposable, bounded sandbox."""


def redact_diagnostics(text: str, *, sandbox: str | None = None) -> str:
    """Bound diagnostics and strip absolute paths, home and sandbox locations."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    cleaned = text
    if sandbox:
        # The OS may hand the child a resolved path (macOS /var -> /private/var),
        # so redact both spellings before the generic path pass.
        candidates = {sandbox}
        try:
            candidates.add(str(Path(sandbox).resolve()))
        except OSError:
            pass
        for candidate in sorted(candidates, key=len, reverse=True):
            cleaned = cleaned.replace(candidate, "<sandbox>")
    home = os.path.expanduser("~")
    if home and home != "/":
        cleaned = cleaned.replace(home, "<home>")
    cleaned = _ABSOLUTE_PATH_PATTERN.sub("<path>", cleaned)
    if len(cleaned) > MAX_DIAGNOSTIC_CHARS:
        cleaned = cleaned[:MAX_DIAGNOSTIC_CHARS]
    return cleaned


def sandbox_environment(sandbox: Path) -> dict[str, str]:
    """A minimal, deterministic environment; nothing sensitive is inherited."""

    path = os.environ.get("PATH", "/usr/bin:/bin")
    return {
        "PATH": path,
        "HOME": str(sandbox),
        "TMPDIR": str(sandbox),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "SOURCE_DATE_EPOCH": "0",
        "TEXMFHOME": str(sandbox / "texmf"),
        "TEXMFVAR": str(sandbox / "texmf-var"),
        "TEXMFCONFIG": str(sandbox / "texmf-config"),
        "openout_any": "p",
        "openin_any": "p",
        "shell_escape": "f",
    }


def _apply_resource_limits() -> None:  # pragma: no cover - child process only
    try:
        import resource
    except ImportError:
        return
    for name, limit in (
        ("RLIMIT_CPU", MAX_CPU_SECONDS),
        ("RLIMIT_FSIZE", MAX_SANDBOX_FILE_BYTES),
        ("RLIMIT_AS", MAX_ADDRESS_SPACE_BYTES),
        ("RLIMIT_NPROC", 64),
        ("RLIMIT_CORE", 0),
    ):
        which = getattr(resource, name, None)
        if which is None:
            continue
        try:
            soft, hard = resource.getrlimit(which)
            ceiling = limit if hard in (resource.RLIM_INFINITY,) else min(limit, hard)
            resource.setrlimit(which, (ceiling, hard))
        except (OSError, ValueError):
            continue


def compile_flags(output_directory: Path) -> tuple[str, ...]:
    """The fixed, safety-bearing flag set recorded in every binding."""

    return (
        "-no-shell-escape",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        f"-output-directory={output_directory}",
    )


def normalized_compile_flags() -> tuple[str, ...]:
    """Flags with the per-run output directory replaced by a stable token."""

    return (
        "-no-shell-escape",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-output-directory=<sandbox>",
    )


class SandboxedPdfLatexCompiler:
    """Run one allowlisted engine with no shell, fixed cwd and hard bounds."""

    def __init__(
        self,
        *,
        engine: str = DEFAULT_LATEX_ENGINE,
        executable: str | Path | None = None,
        timeout_seconds: int = DEFAULT_COMPILE_TIMEOUT_SECONDS,
    ) -> None:
        if engine not in ALLOWED_LATEX_ENGINES:
            raise ValueError("engine is not allowlisted")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
            raise ValueError("timeout_seconds is outside the compile policy")
        self._engine = engine
        self._timeout = timeout_seconds
        resolved = (
            Path(executable) if executable is not None else _which(engine)
        )
        self._executable = resolved

    @property
    def engine(self) -> str:
        return self._engine

    def describe(self) -> LatexCompilerDescription:
        if self._executable is None or not Path(self._executable).is_file():
            raise LatexCompilerUnavailableError(
                "the allowlisted LaTeX engine is not available"
            )
        try:
            completed = subprocess.run(
                [str(self._executable), "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(self._timeout, 30),
                check=False,
                shell=False,
                cwd=tempfile.gettempdir(),
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise LatexCompilerUnavailableError(
                "the LaTeX engine could not be described"
            ) from exc
        if completed.returncode != 0:
            raise LatexCompilerUnavailableError(
                "the LaTeX engine did not report a version"
            )
        first = _VERSION_LINE_PATTERN.match(completed.stdout.strip())
        version = (first.group(0) if first else "").strip()
        if not version:
            raise LatexCompilerUnavailableError(
                "the LaTeX engine reported an empty version"
            )
        return LatexCompilerDescription(
            engine=self._engine,
            compiler_version=version,
            normalized_flags=normalized_compile_flags(),
            compile_policy_version=LATEX_COMPILE_POLICY_VERSION,
            sandbox_policy_version=LATEX_SANDBOX_POLICY_VERSION,
        )

    def compile(self, request: LatexCompileRequest) -> LatexCompileOutcome:
        if not isinstance(request, LatexCompileRequest):
            raise TypeError("request must be a LatexCompileRequest")
        if self._executable is None or not Path(self._executable).is_file():
            return LatexCompileOutcome(
                status=LatexCompileStatus.UNAVAILABLE,
                pdf_bytes=None,
                diagnostics="The allowlisted LaTeX engine is not available.",
                exit_code=None,
                compiler_started=False,
            )
        sandbox = Path(tempfile.mkdtemp(prefix="jobops-latex-"))
        try:
            return self._run(request, sandbox)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

    def _run(
        self, request: LatexCompileRequest, sandbox: Path
    ) -> LatexCompileOutcome:
        source = sandbox / f"{SANDBOX_SOURCE_STEM}.tex"
        source.write_text(request.latex_source, encoding="utf-8")
        stdout_path = sandbox / "compiler-stdout.txt"
        stderr_path = sandbox / "compiler-stderr.txt"
        command = [
            str(self._executable),
            *compile_flags(sandbox),
            source.name,
        ]
        started = False
        try:
            with (
                stdout_path.open("wb") as stdout_file,
                stderr_path.open("wb") as stderr_file,
            ):
                started = True
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=self._timeout,
                    check=False,
                    shell=False,
                    cwd=str(sandbox),
                    env=sandbox_environment(sandbox),
                    preexec_fn=(
                        _apply_resource_limits
                        if hasattr(os, "fork")
                        else None
                    ),
                )
        except subprocess.TimeoutExpired:
            return LatexCompileOutcome(
                status=LatexCompileStatus.TIMEOUT,
                pdf_bytes=None,
                diagnostics=(
                    "The LaTeX engine exceeded the compile timeout and was "
                    "terminated."
                ),
                exit_code=None,
                compiler_started=True,
            )
        except (OSError, subprocess.SubprocessError):
            return LatexCompileOutcome(
                status=LatexCompileStatus.UNAVAILABLE,
                pdf_bytes=None,
                diagnostics="The LaTeX engine could not be executed.",
                exit_code=None,
                compiler_started=started,
            )

        diagnostics = redact_diagnostics(
            _read_capped(stdout_path) + _read_capped(stderr_path),
            sandbox=str(sandbox),
        )
        oversize = _sandbox_is_oversized(sandbox)
        if oversize is not None:
            return LatexCompileOutcome(
                status=LatexCompileStatus.OUTPUT_INVALID,
                pdf_bytes=None,
                diagnostics=redact_diagnostics(oversize, sandbox=str(sandbox)),
                exit_code=completed.returncode,
                compiler_started=True,
            )
        if completed.returncode != 0:
            return LatexCompileOutcome(
                status=LatexCompileStatus.COMPILATION_ERROR,
                pdf_bytes=None,
                diagnostics=diagnostics
                or "The LaTeX engine reported a compilation error.",
                exit_code=completed.returncode,
                compiler_started=True,
            )
        pdf_path = sandbox / f"{SANDBOX_SOURCE_STEM}.pdf"
        try:
            resolved = pdf_path.resolve(strict=True)
            resolved.relative_to(sandbox.resolve())
            if pdf_path.is_symlink() or not pdf_path.is_file():
                raise ValueError("the compiler output is not a regular file")
            size = pdf_path.stat(follow_symlinks=False).st_size
            if size <= 0 or size > MAX_PDF_BYTES:
                raise ValueError("the compiler output size is invalid")
            content = pdf_path.read_bytes()
        except (OSError, ValueError):
            return LatexCompileOutcome(
                status=LatexCompileStatus.OUTPUT_INVALID,
                pdf_bytes=None,
                diagnostics=(
                    diagnostics
                    or "The LaTeX engine exited successfully without a PDF."
                ),
                exit_code=completed.returncode,
                compiler_started=True,
            )
        return LatexCompileOutcome(
            status=LatexCompileStatus.SUCCEEDED,
            pdf_bytes=content,
            diagnostics=diagnostics,
            exit_code=completed.returncode,
            compiler_started=True,
        )


def _which(engine: str) -> Path | None:
    found = shutil.which(engine)
    return Path(found) if found else None


def _read_capped(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            raw = handle.read(MAX_LOG_BYTES)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def _sandbox_is_oversized(sandbox: Path) -> str | None:
    count = 0
    try:
        for item in sandbox.rglob("*"):
            if item.is_symlink() or not item.is_file():
                continue
            count += 1
            if count > MAX_SANDBOX_FILES:
                return "The LaTeX engine produced too many output files."
            if item.stat(follow_symlinks=False).st_size > (
                MAX_SANDBOX_FILE_BYTES
            ):
                return "The LaTeX engine produced an oversized output file."
    except OSError:
        return "The LaTeX sandbox could not be inspected."
    return None


__all__ = [
    "ALLOWED_LATEX_ENGINES",
    "DEFAULT_COMPILE_TIMEOUT_SECONDS",
    "DEFAULT_LATEX_ENGINE",
    "LATEX_COMPILE_POLICY_VERSION",
    "LATEX_SANDBOX_POLICY_VERSION",
    "MAX_COMPILER_PASSES",
    "MAX_DIAGNOSTIC_CHARS",
    "MAX_PDF_BYTES",
    "MAX_SANDBOX_FILES",
    "MAX_SANDBOX_FILE_BYTES",
    "LatexCompileOutcome",
    "LatexCompileRequest",
    "LatexCompileStatus",
    "LatexCompilerDescription",
    "LatexCompilerPort",
    "LatexCompilerUnavailableError",
    "SANDBOX_SOURCE_STEM",
    "SandboxedPdfLatexCompiler",
    "compile_flags",
    "normalized_compile_flags",
    "redact_diagnostics",
    "sandbox_environment",
]
