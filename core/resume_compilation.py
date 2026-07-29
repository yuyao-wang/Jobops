"""Compile one constructed ResumeLatexVersion into a managed PDF artifact."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from io import BytesIO
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError

from .latex_compiler import (
    LATEX_COMPILE_POLICY_VERSION,
    LATEX_SANDBOX_POLICY_VERSION,
    MAX_DIAGNOSTIC_CHARS,
    MAX_PDF_BYTES,
    LatexCompileRequest,
    LatexCompileStatus,
    LatexCompilerDescription,
    LatexCompilerPort,
    LatexCompilerUnavailableError,
    redact_diagnostics,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_latex_construction import (
    LatexBuildProvenance,
    ResumeLatexConstructionReadStatus,
    ResumeLatexConstructionRecordRepository,
)
from .resume_latex_versions import (
    ResumeLatexCapabilityError,
    ResumeLatexVersion,
    ResumeLatexVersionReadStatus,
    ResumeLatexVersionRepository,
    validate_latex_source,
)


RESUME_COMPILATION_CONTRACT_VERSION = "resume-compilation-v1"

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RECORD_ID_PATTERN = re.compile(r"^resume-compilation-[a-f0-9]{64}$")

#: P2a6a manages exactly one .tex per version, so any file dependency the
#: source pulls in cannot be resolved from the registry.
_FILE_DEPENDENCY_PATTERN = re.compile(
    r"\\(?:input|include|includegraphics|lstinputlisting|subfile"
    r"|includepdf|bibliography|addbibresource)\s*(?:\[[^\]]*\])?\s*\{"
)


class ResumeCompilationStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_COMPILER_UNAVAILABLE = "DEFERRED_COMPILER_UNAVAILABLE"
    DEFERRED_SOURCE_INCOMPLETE = "DEFERRED_SOURCE_INCOMPLETE"
    DEFERRED_COMPILATION_ERROR = "DEFERRED_COMPILATION_ERROR"
    FAILED = "FAILED"


class ResumeCompilationWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeCompilationReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeCompilationFailureReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    CONSTRUCTION_RECORD_NOT_FOUND = "CONSTRUCTION_RECORD_NOT_FOUND"
    CONSTRUCTION_RECORD_INTEGRITY_FAILURE = (
        "CONSTRUCTION_RECORD_INTEGRITY_FAILURE"
    )
    CONSTRUCTION_BINDING_MISMATCH = "CONSTRUCTION_BINDING_MISMATCH"
    LATEX_VERSION_NOT_FOUND = "LATEX_VERSION_NOT_FOUND"
    LATEX_VERSION_INTEGRITY_FAILURE = "LATEX_VERSION_INTEGRITY_FAILURE"
    LATEX_VERSION_BINDING_MISMATCH = "LATEX_VERSION_BINDING_MISMATCH"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    SOURCE_HASH_DRIFT = "SOURCE_HASH_DRIFT"
    SOURCE_CAPABILITY_REJECTED = "SOURCE_CAPABILITY_REJECTED"
    UNMANAGED_DEPENDENCY = "UNMANAGED_DEPENDENCY"
    COMPILER_UNAVAILABLE = "COMPILER_UNAVAILABLE"
    COMPILATION_ERROR = "COMPILATION_ERROR"
    COMPILATION_TIMEOUT = "COMPILATION_TIMEOUT"
    PDF_INVALID = "PDF_INVALID"
    ARTIFACT_PERSISTENCE_FAILED = "ARTIFACT_PERSISTENCE_FAILED"
    RECORD_PERSISTENCE_FAILED = "RECORD_PERSISTENCE_FAILED"
    RECORD_INTEGRITY_FAILURE = "RECORD_INTEGRITY_FAILURE"


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the compilation contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
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


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("compiled_at is invalid")
    return _require_aware(
        "compiled_at",
        datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _subject_storage_key(subject_id: str) -> str:
    return f"subject-{hashlib.sha256(subject_id.encode('utf-8')).hexdigest()}"


def compiled_pdf_reference(*, subject_id: str, pdf_sha256: str) -> str:
    return str(
        PurePosixPath("state")
        / "preparation"
        / "compiled-resumes"
        / _subject_storage_key(subject_id)
        / f"{pdf_sha256}.pdf"
    )


def pdf_page_count(content: bytes) -> int:
    """Count pages by parsing the PDF; layout judgment belongs to P2a8.

    A real engine compresses page objects into object streams, so scanning the
    raw bytes for ``/Type /Page`` undercounts. Parsing uses the pdfplumber
    dependency the source-resume projection already relies on. An unparseable
    document counts zero pages and therefore fails validation.
    """

    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    try:
        with pdfplumber.open(BytesIO(content)) as document:
            return len(document.pages)
    except (PDFSyntaxError, OSError, TypeError, ValueError):
        return 0
    except Exception:
        return 0


def unmanaged_file_dependencies(source: str) -> tuple[str, ...]:
    """Report LaTeX macros that would pull in files the registry does not hold."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    found: list[str] = []
    for match in _FILE_DEPENDENCY_PATTERN.finditer(source):
        macro = match.group(0)
        name = macro.strip()[1:].split("[")[0].split("{")[0].strip()
        if name and name not in found:
            found.append(name)
    return tuple(found)


def compilation_binding(
    *,
    construction_record: LatexBuildProvenance,
    version: ResumeLatexVersion,
    description: LatexCompilerDescription,
) -> str:
    return _canonical_hash(
        {
            "compile_policy_version": description.compile_policy_version,
            "compiler_engine": description.engine,
            "compiler_version": description.compiler_version,
            "construction_binding": (
                construction_record.build_provenance_binding
            ),
            "construction_record_id": construction_record.record_id,
            "latex_source_sha256": version.source_sha256,
            "latex_version_id": version.latex_version_id,
            "normalized_flags": list(description.normalized_flags),
            "resume_compilation_contract_version": (
                RESUME_COMPILATION_CONTRACT_VERSION
            ),
            "sandbox_policy_version": description.sandbox_policy_version,
            "subject_id": version.subject_id,
        }
    )


@dataclass(frozen=True, slots=True)
class ResumeCompilationRecord:
    record_id: str
    contract_version: str
    compilation_binding: str
    subject_id: str
    construction_record_id: str
    construction_binding: str
    latex_version_id: str
    latex_source_sha256: str
    compiler_engine: str
    compiler_version: str
    compile_policy_version: str
    sandbox_policy_version: str
    normalized_flags: tuple[str, ...]
    pdf_reference: str
    pdf_sha256: str
    pdf_byte_size: int
    page_count: int
    diagnostics: str
    compiled_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != RESUME_COMPILATION_CONTRACT_VERSION:
            raise ValueError("compilation contract is unsupported")
        binding = _require_hash(
            "compilation_binding", self.compilation_binding
        )
        if (
            not isinstance(self.record_id, str)
            or _RECORD_ID_PATTERN.fullmatch(self.record_id) is None
            or self.record_id != f"resume-compilation-{binding}"
        ):
            raise ValueError("record_id does not match its binding")
        subject = _clean_text("subject_id", self.subject_id, maximum=160)
        _clean_text(
            "construction_record_id",
            self.construction_record_id,
            maximum=160,
        )
        _require_hash("construction_binding", self.construction_binding)
        _clean_text(
            "latex_version_id", self.latex_version_id, maximum=160
        )
        _require_hash("latex_source_sha256", self.latex_source_sha256)
        _clean_text("compiler_engine", self.compiler_engine, maximum=80)
        _clean_text("compiler_version", self.compiler_version, maximum=200)
        if self.compile_policy_version != LATEX_COMPILE_POLICY_VERSION:
            raise ValueError("compile policy version is unsupported")
        if self.sandbox_policy_version != LATEX_SANDBOX_POLICY_VERSION:
            raise ValueError("sandbox policy version is unsupported")
        if not isinstance(self.normalized_flags, tuple) or any(
            not isinstance(flag, str) or not flag.strip()
            for flag in self.normalized_flags
        ):
            raise TypeError("normalized_flags must be a tuple of strings")
        pdf_hash = _require_hash("pdf_sha256", self.pdf_sha256)
        if self.pdf_reference != compiled_pdf_reference(
            subject_id=subject, pdf_sha256=pdf_hash
        ):
            raise ValueError("pdf_reference does not match its binding")
        if (
            type(self.pdf_byte_size) is not int
            or not 0 < self.pdf_byte_size <= MAX_PDF_BYTES
        ):
            raise ValueError("pdf_byte_size is outside the contract")
        if type(self.page_count) is not int or self.page_count < 1:
            raise ValueError("page_count must be at least one")
        if (
            not isinstance(self.diagnostics, str)
            or len(self.diagnostics) > MAX_DIAGNOSTIC_CHARS
        ):
            raise ValueError("diagnostics are outside the bounded contract")
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "subject_id", subject)
        _require_aware("compiled_at", self.compiled_at)

    def content_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "contract_version": self.contract_version,
            "compilation_binding": self.compilation_binding,
            "subject_id": self.subject_id,
            "construction_record_id": self.construction_record_id,
            "construction_binding": self.construction_binding,
            "latex_version_id": self.latex_version_id,
            "latex_source_sha256": self.latex_source_sha256,
            "compiler_engine": self.compiler_engine,
            "compiler_version": self.compiler_version,
            "compile_policy_version": self.compile_policy_version,
            "sandbox_policy_version": self.sandbox_policy_version,
            "normalized_flags": list(self.normalized_flags),
            "pdf_reference": self.pdf_reference,
            "pdf_sha256": self.pdf_sha256,
            "pdf_byte_size": self.pdf_byte_size,
            "page_count": self.page_count,
            "diagnostics": self.diagnostics,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "compiled_at": _rfc3339(self.compiled_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeCompilationWriteResult:
    status: ResumeCompilationWriteStatus
    record: ResumeCompilationRecord | None
    reason_code: ResumeCompilationFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ResumeCompilationWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeCompilationFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ResumeCompilationWriteStatus.CREATED,
            ResumeCompilationWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.record, ResumeCompilationRecord)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful compilation write is invalid")
        elif self.record is not None or self.reason_code is None:
            raise ValueError("failed compilation write is invalid")


@dataclass(frozen=True, slots=True)
class ResumeCompilationReadResult:
    status: ResumeCompilationReadStatus
    record: ResumeCompilationRecord | None
    reason_code: ResumeCompilationFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeCompilationReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeCompilationFailureReason(self.reason_code),
            )
        if status is ResumeCompilationReadStatus.FOUND:
            if (
                not isinstance(self.record, ResumeCompilationRecord)
                or self.reason_code is not None
            ):
                raise ValueError("found compilation read is invalid")
        elif status is ResumeCompilationReadStatus.NOT_FOUND:
            if self.record is not None or self.reason_code is not None:
                raise ValueError("not-found compilation read is invalid")
        elif (
            self.record is not None
            or self.reason_code
            is not ResumeCompilationFailureReason.RECORD_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure compilation read is invalid")


@runtime_checkable
class ResumeCompilationRepository(Protocol):
    def save(
        self, record: ResumeCompilationRecord
    ) -> ResumeCompilationWriteResult:
        """Persist one immutable compilation record."""

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeCompilationReadResult:
        """Read one subject-owned compilation record."""


def _record_from_dict(value: Any) -> ResumeCompilationRecord:
    expected = {
        "record_id",
        "contract_version",
        "compilation_binding",
        "subject_id",
        "construction_record_id",
        "construction_binding",
        "latex_version_id",
        "latex_source_sha256",
        "compiler_engine",
        "compiler_version",
        "compile_policy_version",
        "sandbox_policy_version",
        "normalized_flags",
        "pdf_reference",
        "pdf_sha256",
        "pdf_byte_size",
        "page_count",
        "diagnostics",
        "compiled_at",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["normalized_flags"], list)
    ):
        raise ValueError("persisted compilation record is invalid")
    return ResumeCompilationRecord(
        record_id=value["record_id"],
        contract_version=value["contract_version"],
        compilation_binding=value["compilation_binding"],
        subject_id=value["subject_id"],
        construction_record_id=value["construction_record_id"],
        construction_binding=value["construction_binding"],
        latex_version_id=value["latex_version_id"],
        latex_source_sha256=value["latex_source_sha256"],
        compiler_engine=value["compiler_engine"],
        compiler_version=value["compiler_version"],
        compile_policy_version=value["compile_policy_version"],
        sandbox_policy_version=value["sandbox_policy_version"],
        normalized_flags=tuple(value["normalized_flags"]),
        pdf_reference=value["pdf_reference"],
        pdf_sha256=value["pdf_sha256"],
        pdf_byte_size=value["pdf_byte_size"],
        page_count=value["page_count"],
        diagnostics=value["diagnostics"],
        compiled_at=_parse_timestamp(value["compiled_at"]),
    )


class PrivateHomeResumeCompilationRepository:
    """Immutable compilation records with fail-closed PDF verification."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, record_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(record_id, str)
            or _RECORD_ID_PATTERN.fullmatch(record_id) is None
        ):
            raise ValueError("record_id is invalid")
        return (
            self._home.paths.resume_compilations
            / _subject_storage_key(subject)
            / f"{record_id}.json"
        )

    def _artifact_is_valid(self, record: ResumeCompilationRecord) -> bool:
        try:
            path = self._home.contained_path(record.pdf_reference)
            if path.is_symlink() or not path.is_file():
                return False
            size = path.stat(follow_symlinks=False).st_size
            if size != record.pdf_byte_size:
                return False
            content = path.read_bytes()
        except (OSError, PrivateHomeError):
            return False
        return (
            hashlib.sha256(content).hexdigest() == record.pdf_sha256
            and content.startswith(b"%PDF-")
        )

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeCompilationReadResult:
        path = self._path(subject_id, record_id)
        with self._lock:
            if not path.exists():
                return ResumeCompilationReadResult(
                    status=ResumeCompilationReadStatus.NOT_FOUND,
                    record=None,
                )
            if path.is_symlink() or not path.is_file():
                return ResumeCompilationReadResult(
                    status=ResumeCompilationReadStatus.INTEGRITY_FAILURE,
                    record=None,
                    reason_code=(
                        ResumeCompilationFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            try:
                record = _record_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ResumeCompilationReadResult(
                    status=ResumeCompilationReadStatus.INTEGRITY_FAILURE,
                    record=None,
                    reason_code=(
                        ResumeCompilationFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            if (
                record.subject_id != subject_id.strip()
                or record.record_id != record_id
                or path.name != f"{record.record_id}.json"
                or not self._artifact_is_valid(record)
            ):
                return ResumeCompilationReadResult(
                    status=ResumeCompilationReadStatus.INTEGRITY_FAILURE,
                    record=None,
                    reason_code=(
                        ResumeCompilationFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            return ResumeCompilationReadResult(
                status=ResumeCompilationReadStatus.FOUND,
                record=record,
            )

    def save(
        self, record: ResumeCompilationRecord
    ) -> ResumeCompilationWriteResult:
        if not isinstance(record, ResumeCompilationRecord):
            raise TypeError("record must be a ResumeCompilationRecord")
        path = self._path(record.subject_id, record.record_id)
        with self._lock:
            if not self._artifact_is_valid(record):
                return ResumeCompilationWriteResult(
                    status=ResumeCompilationWriteStatus.FAILED,
                    record=None,
                    reason_code=(
                        ResumeCompilationFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                    retryable=False,
                )
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            record.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ResumeCompilationWriteResult(
                    status=ResumeCompilationWriteStatus.FAILED,
                    record=None,
                    reason_code=(
                        ResumeCompilationFailureReason
                        .RECORD_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return ResumeCompilationWriteResult(
                    status=ResumeCompilationWriteStatus.CREATED,
                    record=record,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=record.subject_id, record_id=record.record_id
            )
            if (
                existing.status is ResumeCompilationReadStatus.FOUND
                and existing.record is not None
                and existing.record.content_dict() == record.content_dict()
            ):
                return ResumeCompilationWriteResult(
                    status=ResumeCompilationWriteStatus.UNCHANGED,
                    record=existing.record,
                    reason_code=None,
                    retryable=False,
                )
            return ResumeCompilationWriteResult(
                status=ResumeCompilationWriteStatus.FAILED,
                record=None,
                reason_code=(
                    ResumeCompilationFailureReason.RECORD_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class CompileResumeLatexCommand:
    subject_id: str
    resume_latex_construction_record_id: str
    resume_latex_version_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class CompileResumeLatexResult:
    status: ResumeCompilationStatus
    subject_id: str
    compilation_binding: str
    record: ResumeCompilationRecord | None
    write_result: ResumeCompilationWriteResult | None
    reason_code: ResumeCompilationFailureReason | None
    compiler_started: bool
    diagnostics: str
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = ResumeCompilationStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeCompilationFailureReason(self.reason_code),
            )
        if type(self.compiler_started) is not bool:
            raise TypeError("compiler_started must be a boolean")
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if (
            not isinstance(self.diagnostics, str)
            or len(self.diagnostics) > MAX_DIAGNOSTIC_CHARS
        ):
            raise ValueError("diagnostics are outside the bounded contract")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            ResumeCompilationStatus.CREATED,
            ResumeCompilationStatus.UNCHANGED,
        }:
            expected = ResumeCompilationWriteStatus(status.value)
            if (
                not isinstance(self.record, ResumeCompilationRecord)
                or not isinstance(
                    self.write_result, ResumeCompilationWriteResult
                )
                or self.write_result.status is not expected
                or self.write_result.record != self.record
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful compilation result is invalid")
        elif self.record is not None or self.reason_code is None:
            raise ValueError("unsuccessful compilation result is invalid")


def _failure(
    command: CompileResumeLatexCommand,
    reason: ResumeCompilationFailureReason,
    *,
    status: ResumeCompilationStatus = ResumeCompilationStatus.FAILED,
    retryable: bool = False,
    compiler_started: bool = False,
    diagnostics: str = "",
    compilation_binding: str = "",
    detail: str | None = None,
) -> CompileResumeLatexResult:
    return CompileResumeLatexResult(
        status=status,
        subject_id=(
            command.subject_id
            if isinstance(command.subject_id, str)
            else ""
        ),
        compilation_binding=compilation_binding,
        record=None,
        write_result=None,
        reason_code=reason,
        compiler_started=compiler_started,
        diagnostics=diagnostics,
        retryable=retryable,
        message=detail or f"LaTeX compilation stopped: {reason.value}.",
    )


def compile_resume_latex(
    command: CompileResumeLatexCommand,
    *,
    construction_repository: ResumeLatexConstructionRecordRepository,
    latex_version_repository: ResumeLatexVersionRepository,
    compiler: LatexCompilerPort,
    compilation_repository: ResumeCompilationRepository,
    home: PrivateHome | None = None,
) -> CompileResumeLatexResult:
    """Compile one constructed version inside a bounded sandbox, at most once."""

    active_home = home or PrivateHome.discover()
    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        construction_id = _clean_text(
            "resume_latex_construction_record_id",
            command.resume_latex_construction_record_id,
            maximum=160,
        )
        version_id = _clean_text(
            "resume_latex_version_id",
            command.resume_latex_version_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, ResumeCompilationFailureReason.INVALID_REQUEST
        )

    try:
        construction_read = construction_repository.get(
            subject_id=subject_id, record_id=construction_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeCompilationFailureReason
            .CONSTRUCTION_RECORD_INTEGRITY_FAILURE,
        )
    if (
        construction_read.status
        is ResumeLatexConstructionReadStatus.NOT_FOUND
    ):
        return _failure(
            command,
            ResumeCompilationFailureReason.CONSTRUCTION_RECORD_NOT_FOUND,
        )
    if (
        construction_read.status
        is not ResumeLatexConstructionReadStatus.FOUND
        or not isinstance(construction_read.record, LatexBuildProvenance)
    ):
        return _failure(
            command,
            ResumeCompilationFailureReason
            .CONSTRUCTION_RECORD_INTEGRITY_FAILURE,
        )
    construction = construction_read.record
    if (
        construction.subject_id != subject_id
        or construction.latex_version_id != version_id
    ):
        return _failure(
            command,
            ResumeCompilationFailureReason.CONSTRUCTION_BINDING_MISMATCH,
        )

    try:
        version_read = latex_version_repository.get(
            subject_id=subject_id, latex_version_id=version_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeCompilationFailureReason
            .LATEX_VERSION_INTEGRITY_FAILURE,
        )
    if version_read.status is ResumeLatexVersionReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeCompilationFailureReason.LATEX_VERSION_NOT_FOUND,
        )
    if (
        version_read.status is not ResumeLatexVersionReadStatus.FOUND
        or not isinstance(version_read.version, ResumeLatexVersion)
    ):
        return _failure(
            command,
            ResumeCompilationFailureReason
            .LATEX_VERSION_INTEGRITY_FAILURE,
        )
    version = version_read.version
    if (
        version.subject_id != subject_id
        or version.latex_version_id != construction.latex_version_id
        or version.source_sha256 != construction.latex_source_sha256
        or version.root_family_id != construction.root_family_id
        or version.parent_version_id != construction.parent_version_id
        or version.template_id != construction.template_id
        or version.tailored_resume_draft_id
        != construction.tailored_resume_draft_id
        or version.tailored_resume_draft_hash
        != construction.tailored_resume_draft_hash
        or version.fact_qa_result_id != construction.fact_qa_result_id
        or version.fact_qa_result_hash != construction.fact_qa_result_hash
    ):
        return _failure(
            command,
            ResumeCompilationFailureReason.LATEX_VERSION_BINDING_MISMATCH,
        )

    try:
        source_path = active_home.contained_path(version.source_reference)
        if source_path.is_symlink() or not source_path.is_file():
            raise ValueError("managed source is not a regular file")
        raw = source_path.read_bytes()
    except (OSError, PrivateHomeError, TypeError, ValueError):
        return _failure(
            command, ResumeCompilationFailureReason.SOURCE_UNREADABLE
        )
    if hashlib.sha256(raw).hexdigest() != version.source_sha256:
        return _failure(
            command, ResumeCompilationFailureReason.SOURCE_HASH_DRIFT
        )
    try:
        latex_source = raw.decode("utf-8")
        validate_latex_source(latex_source)
    except ResumeLatexCapabilityError as exc:
        return _failure(
            command,
            ResumeCompilationFailureReason.SOURCE_CAPABILITY_REJECTED,
            detail=(
                "The managed LaTeX source requests "
                f"{exc.capability.value}; compilation did not start."
            ),
        )
    except (TypeError, UnicodeDecodeError, ValueError):
        return _failure(
            command, ResumeCompilationFailureReason.SOURCE_UNREADABLE
        )

    dependencies = unmanaged_file_dependencies(latex_source)
    if dependencies:
        return _failure(
            command,
            ResumeCompilationFailureReason.UNMANAGED_DEPENDENCY,
            status=ResumeCompilationStatus.DEFERRED_SOURCE_INCOMPLETE,
            diagnostics=(
                "The source pulls in files the registry does not manage: "
                + ", ".join(dependencies)
            ),
            detail=(
                "The LaTeX source needs files that are not registered; "
                "nothing was scanned or downloaded."
            ),
        )

    try:
        description = compiler.describe()
        if not isinstance(description, LatexCompilerDescription):
            raise LatexCompilerUnavailableError(
                "the compiler description is invalid"
            )
    except LatexCompilerUnavailableError:
        return _failure(
            command,
            ResumeCompilationFailureReason.COMPILER_UNAVAILABLE,
            status=ResumeCompilationStatus.DEFERRED_COMPILER_UNAVAILABLE,
            detail="No allowlisted LaTeX engine is available to compile.",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeCompilationFailureReason.COMPILER_UNAVAILABLE,
            status=ResumeCompilationStatus.DEFERRED_COMPILER_UNAVAILABLE,
            detail="The LaTeX engine could not be described.",
        )

    binding = compilation_binding(
        construction_record=construction,
        version=version,
        description=description,
    )
    record_id = f"resume-compilation-{binding}"
    try:
        existing = compilation_repository.get(
            subject_id=subject_id, record_id=record_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeCompilationFailureReason.RECORD_INTEGRITY_FAILURE,
            compilation_binding=binding,
        )
    if existing.status is ResumeCompilationReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            ResumeCompilationFailureReason.RECORD_INTEGRITY_FAILURE,
            compilation_binding=binding,
        )
    if (
        existing.status is ResumeCompilationReadStatus.FOUND
        and existing.record is not None
    ):
        return CompileResumeLatexResult(
            status=ResumeCompilationStatus.UNCHANGED,
            subject_id=subject_id,
            compilation_binding=binding,
            record=existing.record,
            write_result=ResumeCompilationWriteResult(
                status=ResumeCompilationWriteStatus.UNCHANGED,
                record=existing.record,
                reason_code=None,
                retryable=False,
            ),
            reason_code=None,
            compiler_started=False,
            diagnostics=existing.record.diagnostics,
            retryable=False,
            message="The existing compiled resume is unchanged.",
        )

    try:
        outcome = compiler.compile(
            LatexCompileRequest(latex_source=latex_source)
        )
    except LatexCompilerUnavailableError:
        return _failure(
            command,
            ResumeCompilationFailureReason.COMPILER_UNAVAILABLE,
            status=ResumeCompilationStatus.DEFERRED_COMPILER_UNAVAILABLE,
            compilation_binding=binding,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeCompilationFailureReason.COMPILER_UNAVAILABLE,
            status=ResumeCompilationStatus.DEFERRED_COMPILER_UNAVAILABLE,
            compilation_binding=binding,
            detail="The LaTeX engine failed before producing an outcome.",
        )
    diagnostics = redact_diagnostics(
        outcome.diagnostics if hasattr(outcome, "diagnostics") else ""
    )
    if outcome.status is LatexCompileStatus.UNAVAILABLE:
        return _failure(
            command,
            ResumeCompilationFailureReason.COMPILER_UNAVAILABLE,
            status=ResumeCompilationStatus.DEFERRED_COMPILER_UNAVAILABLE,
            compiler_started=outcome.compiler_started,
            diagnostics=diagnostics,
            compilation_binding=binding,
        )
    if outcome.status is LatexCompileStatus.TIMEOUT:
        return _failure(
            command,
            ResumeCompilationFailureReason.COMPILATION_TIMEOUT,
            status=ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR,
            compiler_started=True,
            diagnostics=diagnostics,
            compilation_binding=binding,
            detail="The LaTeX engine timed out and was terminated.",
        )
    if outcome.status is LatexCompileStatus.COMPILATION_ERROR:
        return _failure(
            command,
            ResumeCompilationFailureReason.COMPILATION_ERROR,
            status=ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR,
            compiler_started=True,
            diagnostics=diagnostics,
            compilation_binding=binding,
            detail="The LaTeX source did not compile; it was not modified.",
        )
    if (
        outcome.status is not LatexCompileStatus.SUCCEEDED
        or not isinstance(outcome.pdf_bytes, bytes)
    ):
        return _failure(
            command,
            ResumeCompilationFailureReason.PDF_INVALID,
            status=ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR,
            compiler_started=outcome.compiler_started,
            diagnostics=diagnostics,
            compilation_binding=binding,
            detail="The LaTeX engine did not produce a usable PDF.",
        )

    content = outcome.pdf_bytes
    pages = pdf_page_count(content)
    if (
        not content.startswith(b"%PDF-")
        or not 0 < len(content) <= MAX_PDF_BYTES
        or pages < 1
    ):
        return _failure(
            command,
            ResumeCompilationFailureReason.PDF_INVALID,
            status=ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR,
            compiler_started=True,
            diagnostics=diagnostics,
            compilation_binding=binding,
            detail=(
                "The compiler reported success but the PDF failed "
                "validation."
            ),
        )

    pdf_hash = hashlib.sha256(content).hexdigest()
    reference = compiled_pdf_reference(
        subject_id=subject_id, pdf_sha256=pdf_hash
    )
    try:
        active_home.ensure()
        target = active_home.contained_path(reference)
        created_artifact = active_home.write_bytes_if_absent(target, content)
        if not created_artifact and (
            target.is_symlink()
            or not target.is_file()
            or target.read_bytes() != content
        ):
            return _failure(
                command,
                ResumeCompilationFailureReason.ARTIFACT_PERSISTENCE_FAILED,
                compilation_binding=binding,
                compiler_started=True,
                diagnostics=diagnostics,
            )
    except (OSError, PrivateHomeError):
        return _failure(
            command,
            ResumeCompilationFailureReason.ARTIFACT_PERSISTENCE_FAILED,
            retryable=True,
            compilation_binding=binding,
            compiler_started=True,
            diagnostics=diagnostics,
        )

    try:
        record = ResumeCompilationRecord(
            record_id=record_id,
            contract_version=RESUME_COMPILATION_CONTRACT_VERSION,
            compilation_binding=binding,
            subject_id=subject_id,
            construction_record_id=construction.record_id,
            construction_binding=construction.build_provenance_binding,
            latex_version_id=version.latex_version_id,
            latex_source_sha256=version.source_sha256,
            compiler_engine=description.engine,
            compiler_version=description.compiler_version,
            compile_policy_version=description.compile_policy_version,
            sandbox_policy_version=description.sandbox_policy_version,
            normalized_flags=description.normalized_flags,
            pdf_reference=reference,
            pdf_sha256=pdf_hash,
            pdf_byte_size=len(content),
            page_count=pages,
            diagnostics=diagnostics,
            compiled_at=now,
        )
    except (TypeError, ValueError):
        return _failure(
            command,
            ResumeCompilationFailureReason.PDF_INVALID,
            status=ResumeCompilationStatus.DEFERRED_COMPILATION_ERROR,
            compiler_started=True,
            diagnostics=diagnostics,
            compilation_binding=binding,
        )

    try:
        write_result = compilation_repository.save(record)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeCompilationFailureReason.RECORD_PERSISTENCE_FAILED,
            retryable=True,
            compiler_started=True,
            diagnostics=diagnostics,
            compilation_binding=binding,
        )
    if write_result.status is ResumeCompilationWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or ResumeCompilationFailureReason.RECORD_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            compiler_started=True,
            diagnostics=diagnostics,
            compilation_binding=binding,
        )
    status = ResumeCompilationStatus(write_result.status.value)
    return CompileResumeLatexResult(
        status=status,
        subject_id=subject_id,
        compilation_binding=binding,
        record=write_result.record,
        write_result=write_result,
        reason_code=None,
        compiler_started=True,
        diagnostics=diagnostics,
        retryable=False,
        message=(
            "The resume LaTeX compiled into a managed PDF."
            if status is ResumeCompilationStatus.CREATED
            else "The existing compiled resume is unchanged."
        ),
    )


__all__ = [
    "CompileResumeLatexCommand",
    "CompileResumeLatexResult",
    "PrivateHomeResumeCompilationRepository",
    "RESUME_COMPILATION_CONTRACT_VERSION",
    "ResumeCompilationFailureReason",
    "ResumeCompilationReadResult",
    "ResumeCompilationReadStatus",
    "ResumeCompilationRecord",
    "ResumeCompilationRepository",
    "ResumeCompilationStatus",
    "ResumeCompilationWriteResult",
    "ResumeCompilationWriteStatus",
    "compilation_binding",
    "compile_resume_latex",
    "compiled_pdf_reference",
    "pdf_page_count",
    "unmanaged_file_dependencies",
]
