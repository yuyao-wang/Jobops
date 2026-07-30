"""Authenticated, immutable previews for current Cover Letter overflow targets."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .human_attention_queue import HumanAttentionQueueItem
from .latex_compiler import (
    LATEX_COMPILE_POLICY_VERSION,
    LATEX_SANDBOX_POLICY_VERSION,
    LatexCompileRequest,
    LatexCompileStatus,
    LatexCompilerDescription,
    LatexCompilerPort,
    LatexCompilerUnavailableError,
)
from .material_correction_ref import MaterialCorrectionTargetRef
from .material_correction_target import (
    CoverLetterLayoutCorrectionTarget,
    MaterialCorrectionTarget,
    MaterialCorrectionTargetKind,
    MaterialCorrectionTargetProvider,
    MaterialCorrectionTargetReadStatus,
    MaterialCorrectionTargetRepository,
    MaterialCorrectionTargetStatus,
)
from .pdf_page_renderer import (
    PDF_RENDERER_CONTRACT_VERSION,
    PdfPageRendererPort,
    PdfRendererDescription,
    PdfRendererUnavailableError,
    RenderedPage,
)
from .prepared_cover_letter_material import (
    COVER_LETTER_PUBLICATION_POLICY_VERSION,
    MANAGED_COVER_LETTER_TEMPLATE_ID,
    PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION,
    cover_letter_source_reference,
    inspect_cover_letter_pdf,
)
from .private_home import PrivateHome, PrivateHomeError


COVER_LETTER_OVERFLOW_PREVIEW_VERSION = (
    "cover-letter-overflow-correction-preview-v1"
)
COVER_LETTER_OVERFLOW_PREVIEW_REF_VERSION = (
    "cover-letter-overflow-correction-preview-ref-v1"
)
COVER_LETTER_OVERFLOW_SOURCE_VERSION = (
    "cover-letter-overflow-source-provider-v1"
)
COVER_LETTER_OVERFLOW_RENDERED_ARTIFACT_VERSION = (
    "cover-letter-overflow-rendered-pdf-v1"
)
_PAGE_ARTIFACT_VERSION = "cover-letter-overflow-preview-page-v1"
_PREVIEW_ARTIFACT_VERSION = "cover-letter-overflow-preview-pages-v1"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MEDIA_TYPE = "image/png"
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ID_RE = re.compile(r"^cover-letter-overflow-preview-[a-f0-9]{64}$")


class CoverLetterOverflowPreviewStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNCHANGED = "UNCHANGED"
    ITEM_NOT_CURRENT = "ITEM_NOT_CURRENT"
    TARGET_STALE = "TARGET_STALE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    RENDERER_UNAVAILABLE = "RENDERER_UNAVAILABLE"
    PREVIEW_UNSAFE = "PREVIEW_UNSAFE"
    PREVIEW_INTEGRITY_FAILURE = "PREVIEW_INTEGRITY_FAILURE"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    FAILED = "FAILED"


class CoverLetterOverflowPreviewReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CoverLetterOverflowPreviewWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class CoverLetterOverflowSourceStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _text(name: str, value: Any, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is too long")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 hash")
    return value


def _aware(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionPreviewRef:
    preview_id: str
    preview_version: str
    preview_hash: str

    def __post_init__(self) -> None:
        if _ID_RE.fullmatch(self.preview_id) is None:
            raise ValueError("preview ID is invalid")
        if self.preview_version != COVER_LETTER_OVERFLOW_PREVIEW_REF_VERSION:
            raise ValueError("preview reference version is unsupported")
        _hash("preview_hash", self.preview_hash)

    def to_dict(self) -> dict[str, str]:
        return {
            "preview_hash": self.preview_hash,
            "preview_id": self.preview_id,
            "preview_version": self.preview_version,
        }

    def to_opaque(self) -> str:
        raw = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def from_opaque(
        cls, value: str
    ) -> "CoverLetterOverflowCorrectionPreviewRef":
        if not isinstance(value, str) or not value or len(value) > 600:
            raise ValueError("preview reference is invalid")
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(
            base64.urlsafe_b64decode(value + padding).decode()
        )
        if not isinstance(decoded, Mapping) or set(decoded) != {
            "preview_hash",
            "preview_id",
            "preview_version",
        }:
            raise ValueError("preview reference is invalid")
        return cls(**decoded)


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowPreviewPage:
    page_number: int
    artifact_id: str
    artifact_version: str
    content_hash: str
    byte_size: int
    width_px: int
    height_px: int
    media_type: str = _MEDIA_TYPE

    def __post_init__(self) -> None:
        if type(self.page_number) is not int or self.page_number < 1:
            raise ValueError("page number is invalid")
        _text("artifact_id", self.artifact_id)
        if self.artifact_version != _PAGE_ARTIFACT_VERSION:
            raise ValueError("page artifact version is unsupported")
        _hash("content_hash", self.content_hash)
        if type(self.byte_size) is not int or self.byte_size < 1:
            raise ValueError("page byte size is invalid")
        if (
            type(self.width_px) is not int
            or type(self.height_px) is not int
            or self.width_px < 1
            or self.height_px < 1
        ):
            raise ValueError("preview dimensions are invalid")
        if self.media_type != _MEDIA_TYPE:
            raise ValueError("preview media type is unsafe")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_version": self.artifact_version,
            "byte_size": self.byte_size,
            "content_hash": self.content_hash,
            "height_px": self.height_px,
            "media_type": self.media_type,
            "page_number": self.page_number,
            "width_px": self.width_px,
        }


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionPreview:
    preview_id: str
    preview_version: str
    preview_hash: str
    subject_id: str
    application_plan_id: str
    preparation_run_id: str
    attention_item_id: str
    correction_target_ref: MaterialCorrectionTargetRef
    publication_result_id: str
    overflow_evaluation_id: str
    overflow_evaluation_version: str
    source_record_id: str
    source_version: str
    source_content_hash: str
    compiled_artifact_id: str
    compiled_artifact_version: str
    compiled_artifact_content_hash: str
    preview_artifact_id: str
    preview_artifact_version: str
    preview_artifact_hash: str
    compiler_engine: str
    compiler_version: str
    compile_policy_version: str
    sandbox_policy_version: str
    normalized_flags: tuple[str, ...]
    renderer_name: str
    renderer_version: str
    renderer_contract_version: str
    renderer_dpi: int
    pages: tuple[CoverLetterOverflowPreviewPage, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.preview_version != COVER_LETTER_OVERFLOW_PREVIEW_VERSION:
            raise ValueError("preview contract version is unsupported")
        if _ID_RE.fullmatch(self.preview_id) is None:
            raise ValueError("preview ID is invalid")
        _hash("preview_hash", self.preview_hash)
        for name, value in (
            ("subject_id", self.subject_id),
            ("application_plan_id", self.application_plan_id),
            ("preparation_run_id", self.preparation_run_id),
            ("attention_item_id", self.attention_item_id),
            ("publication_result_id", self.publication_result_id),
            ("overflow_evaluation_id", self.overflow_evaluation_id),
            ("source_record_id", self.source_record_id),
            ("source_version", self.source_version),
            ("compiled_artifact_id", self.compiled_artifact_id),
            ("preview_artifact_id", self.preview_artifact_id),
            ("compiler_engine", self.compiler_engine),
            ("compiler_version", self.compiler_version),
            ("renderer_name", self.renderer_name),
            ("renderer_version", self.renderer_version),
        ):
            _text(name, value)
        if (
            self.overflow_evaluation_version
            != PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION
        ):
            raise ValueError("overflow evaluation version is unsupported")
        if (
            self.compiled_artifact_version
            != COVER_LETTER_OVERFLOW_RENDERED_ARTIFACT_VERSION
            or self.preview_artifact_version != _PREVIEW_ARTIFACT_VERSION
        ):
            raise ValueError("derived artifact version is unsupported")
        for name, value in (
            ("source_content_hash", self.source_content_hash),
            (
                "compiled_artifact_content_hash",
                self.compiled_artifact_content_hash,
            ),
            ("preview_artifact_hash", self.preview_artifact_hash),
        ):
            _hash(name, value)
        if self.source_record_id != (
            f"cover-letter-latex-source-{self.source_content_hash}"
        ):
            raise ValueError("source artifact identity is invalid")
        if self.compiled_artifact_id != (
            "cover-letter-overflow-pdf-"
            f"{self.compiled_artifact_content_hash}"
        ):
            raise ValueError("compiled artifact identity is invalid")
        if self.preview_artifact_id != f"{self.preview_id}-pages":
            raise ValueError("preview artifact identity is invalid")
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ):
            raise TypeError("correction target reference must be typed")
        if (
            not isinstance(self.normalized_flags, tuple)
            or any(not isinstance(flag, str) or not flag for flag in self.normalized_flags)
        ):
            raise ValueError("compiler flags are invalid")
        if (
            self.compile_policy_version != LATEX_COMPILE_POLICY_VERSION
            or self.sandbox_policy_version != LATEX_SANDBOX_POLICY_VERSION
        ):
            raise ValueError("compiler contract identity is unsupported")
        if self.renderer_contract_version != PDF_RENDERER_CONTRACT_VERSION:
            raise ValueError("renderer contract version is unsupported")
        if type(self.renderer_dpi) is not int or not 36 <= self.renderer_dpi <= 600:
            raise ValueError("renderer DPI is invalid")
        if (
            not isinstance(self.pages, tuple)
            or len(self.pages) < 2
            or tuple(page.page_number for page in self.pages)
            != tuple(range(1, len(self.pages) + 1))
        ):
            raise ValueError("overflow preview page set is invalid")
        _aware("created_at", self.created_at)
        expected_id = _canonical_hash(self.logical_identity_dict())
        if self.preview_id != f"cover-letter-overflow-preview-{expected_id}":
            raise ValueError("preview logical identity is invalid")
        if self.preview_artifact_hash != _canonical_hash(
            {"pages": [page.to_dict() for page in self.pages]}
        ):
            raise ValueError("preview artifact hash is invalid")
        if self.preview_hash != _canonical_hash(self.content_dict()):
            raise ValueError("preview canonical hash is invalid")

    @property
    def reference(self) -> CoverLetterOverflowCorrectionPreviewRef:
        return CoverLetterOverflowCorrectionPreviewRef(
            self.preview_id,
            COVER_LETTER_OVERFLOW_PREVIEW_REF_VERSION,
            self.preview_hash,
        )

    def logical_identity_dict(self) -> dict[str, Any]:
        return {
            "compiler_engine": self.compiler_engine,
            "compiler_version": self.compiler_version,
            "compile_policy_version": self.compile_policy_version,
            "correction_target_ref": self.correction_target_ref.to_dict(),
            "normalized_flags": list(self.normalized_flags),
            "overflow_evaluation_id": self.overflow_evaluation_id,
            "renderer_contract_version": self.renderer_contract_version,
            "renderer_dpi": self.renderer_dpi,
            "renderer_name": self.renderer_name,
            "renderer_version": self.renderer_version,
            "sandbox_policy_version": self.sandbox_policy_version,
            "source_content_hash": self.source_content_hash,
            "source_record_id": self.source_record_id,
            "source_version": self.source_version,
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.logical_identity_dict(),
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "compiled_artifact_content_hash": (
                self.compiled_artifact_content_hash
            ),
            "compiled_artifact_id": self.compiled_artifact_id,
            "compiled_artifact_version": self.compiled_artifact_version,
            "overflow_evaluation_version": self.overflow_evaluation_version,
            "pages": [page.to_dict() for page in self.pages],
            "preparation_run_id": self.preparation_run_id,
            "preview_artifact_hash": self.preview_artifact_hash,
            "preview_artifact_id": self.preview_artifact_id,
            "preview_artifact_version": self.preview_artifact_version,
            "preview_id": self.preview_id,
            "preview_version": self.preview_version,
            "publication_result_id": self.publication_result_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "created_at": _rfc3339(self.created_at),
            "preview_hash": self.preview_hash,
        }


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowSource:
    subject_id: str
    source_record_id: str
    source_version: str
    source_content_hash: str
    latex_source: str
    provider_contract_version: str = COVER_LETTER_OVERFLOW_SOURCE_VERSION

    def __post_init__(self) -> None:
        _text("subject_id", self.subject_id, 160)
        _text("source_record_id", self.source_record_id)
        _text("source_version", self.source_version, 160)
        _hash("source_content_hash", self.source_content_hash)
        if (
            not isinstance(self.latex_source, str)
            or not self.latex_source.strip()
            or hashlib.sha256(self.latex_source.encode("utf-8")).hexdigest()
            != self.source_content_hash
        ):
            raise ValueError("Cover Letter source integrity failed")
        if self.source_record_id != (
            f"cover-letter-latex-source-{self.source_content_hash}"
        ):
            raise ValueError("Cover Letter source identity is invalid")
        if self.provider_contract_version != COVER_LETTER_OVERFLOW_SOURCE_VERSION:
            raise ValueError("source provider version is unsupported")


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowSourceResult:
    status: CoverLetterOverflowSourceStatus
    source: CoverLetterOverflowSource | None


@runtime_checkable
class CoverLetterOverflowSourceProvider(Protocol):
    def get(
        self,
        *,
        subject_id: str,
        source_record_id: str,
        source_version: str,
        source_content_hash: str,
    ) -> CoverLetterOverflowSourceResult: ...


class PrivateHomeCoverLetterOverflowSourceProvider:
    """Return verified content-addressed source bytes, never its private path."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def get(
        self,
        *,
        subject_id: str,
        source_record_id: str,
        source_version: str,
        source_content_hash: str,
    ) -> CoverLetterOverflowSourceResult:
        try:
            _text("subject_id", subject_id, 160)
            _hash("source_content_hash", source_content_hash)
            if source_record_id != (
                f"cover-letter-latex-source-{source_content_hash}"
            ):
                raise ValueError("source record identity is invalid")
            reference = cover_letter_source_reference(
                subject_id=subject_id, source_sha256=source_content_hash
            )
            managed = self._home.contained_path(reference)
            if not managed.exists():
                return CoverLetterOverflowSourceResult(
                    CoverLetterOverflowSourceStatus.NOT_FOUND, None
                )
            if managed.is_symlink() or not managed.is_file():
                raise ValueError("managed source is unsafe")
            content = managed.read_bytes()
            if hashlib.sha256(content).hexdigest() != source_content_hash:
                raise ValueError("managed source hash drifted")
            source = CoverLetterOverflowSource(
                subject_id=subject_id,
                source_record_id=source_record_id,
                source_version=source_version,
                source_content_hash=source_content_hash,
                latex_source=content.decode("utf-8"),
            )
            return CoverLetterOverflowSourceResult(
                CoverLetterOverflowSourceStatus.AVAILABLE, source
            )
        except (OSError, PrivateHomeError, TypeError, UnicodeError, ValueError):
            return CoverLetterOverflowSourceResult(
                CoverLetterOverflowSourceStatus.INTEGRITY_FAILURE, None
            )


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowPreviewReadResult:
    status: CoverLetterOverflowPreviewReadStatus
    preview: CoverLetterOverflowCorrectionPreview | None


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowPreviewWriteResult:
    status: CoverLetterOverflowPreviewWriteStatus
    preview: CoverLetterOverflowCorrectionPreview | None


@runtime_checkable
class CoverLetterOverflowPreviewRepository(Protocol):
    def save(
        self,
        preview: CoverLetterOverflowCorrectionPreview,
        page_bytes: tuple[bytes, ...],
    ) -> CoverLetterOverflowPreviewWriteResult: ...

    def get(
        self, *, subject_id: str, preview_id: str
    ) -> CoverLetterOverflowPreviewReadResult: ...

    def read_page(
        self, *, subject_id: str, preview_id: str, page_number: int
    ) -> bytes | None: ...


class PrivateHomeCoverLetterOverflowPreviewRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str, preview_id: str) -> Path:
        _text("subject_id", subject_id, 160)
        if _ID_RE.fullmatch(preview_id) is None:
            raise ValueError("preview ID is invalid")
        subject_key = hashlib.sha256(subject_id.strip().encode()).hexdigest()
        return (
            self._home.paths.preparation
            / "cover-letter-overflow-correction-previews"
            / f"subject-{subject_key}"
            / preview_id
        )

    def save(
        self,
        preview: CoverLetterOverflowCorrectionPreview,
        page_bytes: tuple[bytes, ...],
    ) -> CoverLetterOverflowPreviewWriteResult:
        if (
            not isinstance(preview, CoverLetterOverflowCorrectionPreview)
            or len(page_bytes) != len(preview.pages)
        ):
            raise TypeError("preview write is invalid")
        directory = self._directory(preview.subject_id, preview.preview_id)
        with self._lock:
            existing = self.get(
                subject_id=preview.subject_id, preview_id=preview.preview_id
            )
            if existing.status is CoverLetterOverflowPreviewReadStatus.FOUND:
                if (
                    existing.preview is not None
                    and existing.preview.preview_hash == preview.preview_hash
                ):
                    return CoverLetterOverflowPreviewWriteResult(
                        CoverLetterOverflowPreviewWriteStatus.UNCHANGED,
                        existing.preview,
                    )
                return CoverLetterOverflowPreviewWriteResult(
                    CoverLetterOverflowPreviewWriteStatus.FAILED, None
                )
            try:
                self._home.ensure()
                for page, content in zip(preview.pages, page_bytes):
                    if (
                        not content.startswith(_PNG_SIGNATURE)
                        or hashlib.sha256(content).hexdigest()
                        != page.content_hash
                    ):
                        raise ValueError("preview page bytes are invalid")
                    self._home.write_bytes_if_absent(
                        directory / f"page-{page.page_number}.png", content
                    )
                self._home.write_bytes_if_absent(
                    directory / "record.json",
                    (
                        json.dumps(
                            preview.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode(),
                )
            except (OSError, PrivateHomeError, TypeError, ValueError):
                return CoverLetterOverflowPreviewWriteResult(
                    CoverLetterOverflowPreviewWriteStatus.FAILED, None
                )
            read = self.get(
                subject_id=preview.subject_id, preview_id=preview.preview_id
            )
            if (
                read.status is CoverLetterOverflowPreviewReadStatus.FOUND
                and read.preview is not None
                and read.preview.preview_hash == preview.preview_hash
            ):
                return CoverLetterOverflowPreviewWriteResult(
                    CoverLetterOverflowPreviewWriteStatus.CREATED, read.preview
                )
            return CoverLetterOverflowPreviewWriteResult(
                CoverLetterOverflowPreviewWriteStatus.FAILED, None
            )

    def get(
        self, *, subject_id: str, preview_id: str
    ) -> CoverLetterOverflowPreviewReadResult:
        directory = self._directory(subject_id, preview_id)
        record = directory / "record.json"
        with self._lock:
            if not record.exists():
                return CoverLetterOverflowPreviewReadResult(
                    CoverLetterOverflowPreviewReadStatus.NOT_FOUND, None
                )
            try:
                if record.is_symlink() or not record.is_file():
                    raise ValueError("preview record is unsafe")
                preview = _preview_from_dict(
                    json.loads(record.read_text(encoding="utf-8"))
                )
                if (
                    preview.subject_id != subject_id.strip()
                    or preview.preview_id != preview_id
                ):
                    raise ValueError("preview subject binding failed")
                for page in preview.pages:
                    path = directory / f"page-{page.page_number}.png"
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("preview page is unsafe")
                    content = path.read_bytes()
                    if (
                        not content.startswith(_PNG_SIGNATURE)
                        or len(content) != page.byte_size
                        or hashlib.sha256(content).hexdigest()
                        != page.content_hash
                    ):
                        raise ValueError("preview page integrity failed")
                return CoverLetterOverflowPreviewReadResult(
                    CoverLetterOverflowPreviewReadStatus.FOUND, preview
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return CoverLetterOverflowPreviewReadResult(
                    CoverLetterOverflowPreviewReadStatus.INTEGRITY_FAILURE, None
                )

    def read_page(
        self, *, subject_id: str, preview_id: str, page_number: int
    ) -> bytes | None:
        read = self.get(subject_id=subject_id, preview_id=preview_id)
        if (
            read.status is not CoverLetterOverflowPreviewReadStatus.FOUND
            or read.preview is None
            or type(page_number) is not int
            or not 1 <= page_number <= len(read.preview.pages)
        ):
            return None
        try:
            return (
                self._directory(subject_id, preview_id)
                / f"page-{page_number}.png"
            ).read_bytes()
        except OSError:
            return None


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowPreviewResult:
    status: CoverLetterOverflowPreviewStatus
    preview_ref: CoverLetterOverflowCorrectionPreviewRef | None = None
    page_count: int | None = None
    media_type: str | None = None


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowTypedPreviewResult:
    status: CoverLetterOverflowPreviewStatus
    preview: CoverLetterOverflowCorrectionPreview | None


CurrentAttentionItemReader = Callable[
    [str, str], HumanAttentionQueueItem | None
]
PdfInspector = Callable[[bytes], tuple[int, str] | None]


@dataclass(slots=True)
class CoverLetterOverflowPreviewProvider:
    target_repository: MaterialCorrectionTargetRepository
    target_provider: MaterialCorrectionTargetProvider
    current_item_reader: CurrentAttentionItemReader
    source_provider: CoverLetterOverflowSourceProvider
    compiler: LatexCompilerPort
    renderer: PdfPageRendererPort
    repository: CoverLetterOverflowPreviewRepository
    clock: Callable[[], datetime]
    pdf_inspector: PdfInspector = inspect_cover_letter_pdf

    def get_current_cover_letter_overflow_preview(
        self,
        *,
        subject_id: str,
        correction_target_ref: MaterialCorrectionTargetRef,
    ) -> CoverLetterOverflowTypedPreviewResult:
        """Read an already-created current preview without compiling again."""

        current = self._current_target(subject_id, correction_target_ref)
        if isinstance(current, CoverLetterOverflowPreviewResult):
            return CoverLetterOverflowTypedPreviewResult(
                current.status, None
            )
        target, payload = current
        try:
            compiler = self.compiler.describe()
            renderer = self.renderer.describe()
            if not isinstance(compiler, LatexCompilerDescription) or not isinstance(
                renderer, PdfRendererDescription
            ):
                raise ValueError("preview adapter description is invalid")
            preview_id = "cover-letter-overflow-preview-" + _canonical_hash(
                _logical_identity(
                    target=target,
                    payload=payload,
                    compiler=compiler,
                    renderer=renderer,
                )
            )
            read = self.repository.get(
                subject_id=subject_id, preview_id=preview_id
            )
            if read.status is CoverLetterOverflowPreviewReadStatus.NOT_FOUND:
                return CoverLetterOverflowTypedPreviewResult(
                    CoverLetterOverflowPreviewStatus.SOURCE_UNAVAILABLE, None
                )
            if (
                read.status
                is not CoverLetterOverflowPreviewReadStatus.FOUND
                or read.preview is None
            ):
                return CoverLetterOverflowTypedPreviewResult(
                    CoverLetterOverflowPreviewStatus
                    .PREVIEW_INTEGRITY_FAILURE,
                    None,
                )
            if not _preview_matches(read.preview, target, payload):
                return CoverLetterOverflowTypedPreviewResult(
                    CoverLetterOverflowPreviewStatus.TARGET_STALE, None
                )
            source = self.source_provider.get(
                subject_id=subject_id,
                source_record_id=payload.latex_source_id,
                source_version=payload.source_version,
                source_content_hash=payload.source_content_hash,
            )
            if (
                source.status
                is not CoverLetterOverflowSourceStatus.AVAILABLE
                or source.source is None
            ):
                return CoverLetterOverflowTypedPreviewResult(
                    CoverLetterOverflowPreviewStatus.SOURCE_UNAVAILABLE, None
                )
            return CoverLetterOverflowTypedPreviewResult(
                CoverLetterOverflowPreviewStatus.AVAILABLE, read.preview
            )
        except (
            LatexCompilerUnavailableError,
            PdfRendererUnavailableError,
        ):
            return CoverLetterOverflowTypedPreviewResult(
                CoverLetterOverflowPreviewStatus.RENDERER_UNAVAILABLE, None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return CoverLetterOverflowTypedPreviewResult(
                CoverLetterOverflowPreviewStatus.FAILED, None
            )

    def get_or_create_cover_letter_overflow_preview(
        self,
        *,
        subject_id: str,
        correction_target_ref: MaterialCorrectionTargetRef,
    ) -> CoverLetterOverflowPreviewResult:
        current = self._current_target(subject_id, correction_target_ref)
        if isinstance(current, CoverLetterOverflowPreviewResult):
            return current
        target, payload = current
        source_read = self.source_provider.get(
            subject_id=subject_id,
            source_record_id=payload.latex_source_id,
            source_version=payload.source_version,
            source_content_hash=payload.source_content_hash,
        )
        if source_read.status is CoverLetterOverflowSourceStatus.NOT_FOUND:
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.SOURCE_UNAVAILABLE
            )
        if (
            source_read.status
            is not CoverLetterOverflowSourceStatus.AVAILABLE
            or source_read.source is None
        ):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        source = source_read.source
        try:
            compiler = self.compiler.describe()
            renderer = self.renderer.describe()
            if not isinstance(compiler, LatexCompilerDescription) or not isinstance(
                renderer, PdfRendererDescription
            ):
                raise ValueError("preview adapter description is invalid")
        except (
            LatexCompilerUnavailableError,
            PdfRendererUnavailableError,
            OSError,
            RuntimeError,
        ):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.RENDERER_UNAVAILABLE
            )
        except (TypeError, ValueError):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        preview_id = "cover-letter-overflow-preview-" + _canonical_hash(
            _logical_identity(
                target=target,
                payload=payload,
                compiler=compiler,
                renderer=renderer,
            )
        )
        existing = self.repository.get(
            subject_id=subject_id, preview_id=preview_id
        )
        if existing.status is CoverLetterOverflowPreviewReadStatus.FOUND:
            if (
                existing.preview is None
                or not _preview_matches(
                    existing.preview, target, payload
                )
            ):
                return CoverLetterOverflowPreviewResult(
                    CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE
                )
            return _public_result(
                CoverLetterOverflowPreviewStatus.UNCHANGED, existing.preview
            )
        if (
            existing.status
            is CoverLetterOverflowPreviewReadStatus.INTEGRITY_FAILURE
        ):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        try:
            outcome = self.compiler.compile(
                LatexCompileRequest(source.latex_source)
            )
        except LatexCompilerUnavailableError:
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.RENDERER_UNAVAILABLE
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.FAILED
            )
        if outcome.status is LatexCompileStatus.UNAVAILABLE:
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.RENDERER_UNAVAILABLE
            )
        if (
            outcome.status is not LatexCompileStatus.SUCCEEDED
            or not isinstance(outcome.pdf_bytes, bytes)
        ):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.FAILED
            )
        pdf = outcome.pdf_bytes
        inspected = self.pdf_inspector(pdf)
        if inspected is None or inspected[0] < 2:
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.TARGET_STALE
            )
        page_count = inspected[0]
        expected_evaluation = _overflow_evaluation_id(
            target=target,
            payload=payload,
            compiler=compiler,
            page_count=page_count,
        )
        if expected_evaluation != payload.overflow_evaluation_id:
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.TARGET_STALE
            )
        try:
            rendered = self.renderer.render(pdf)
        except PdfRendererUnavailableError:
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.RENDERER_UNAVAILABLE
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.FAILED
            )
        if (
            not isinstance(rendered, tuple)
            or len(rendered) != page_count
            or any(not isinstance(page, RenderedPage) for page in rendered)
            or tuple(page.page_number for page in rendered)
            != tuple(range(1, page_count + 1))
        ):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        if any(
            page.image_format.upper() != "PNG"
            or not page.image_bytes.startswith(_PNG_SIGNATURE)
            for page in rendered
        ):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.PREVIEW_UNSAFE
            )
        pages = tuple(
            CoverLetterOverflowPreviewPage(
                page_number=page.page_number,
                artifact_id=f"{preview_id}-page-{page.page_number}",
                artifact_version=_PAGE_ARTIFACT_VERSION,
                content_hash=hashlib.sha256(page.image_bytes).hexdigest(),
                byte_size=len(page.image_bytes),
                width_px=page.width_px,
                height_px=page.height_px,
            )
            for page in rendered
        )
        pdf_hash = hashlib.sha256(pdf).hexdigest()
        preview_artifact_hash = _canonical_hash(
            {"pages": [page.to_dict() for page in pages]}
        )
        content = _preview_content(
            preview_id=preview_id,
            target=target,
            payload=payload,
            compiler=compiler,
            renderer=renderer,
            pdf_hash=pdf_hash,
            preview_artifact_hash=preview_artifact_hash,
            pages=pages,
        )
        preview = CoverLetterOverflowCorrectionPreview(
            **content,
            preview_hash=_canonical_hash(
                {
                    **content,
                    "correction_target_ref": (
                        content["correction_target_ref"].to_dict()
                    ),
                    "normalized_flags": list(
                        content["normalized_flags"]
                    ),
                    "pages": [
                        page.to_dict() for page in content["pages"]
                    ],
                }
            ),
            created_at=self.clock(),
        )
        write = self.repository.save(
            preview, tuple(page.image_bytes for page in rendered)
        )
        if write.preview is None:
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        return _public_result(
            (
                CoverLetterOverflowPreviewStatus.AVAILABLE
                if write.status
                is CoverLetterOverflowPreviewWriteStatus.CREATED
                else CoverLetterOverflowPreviewStatus.UNCHANGED
            ),
            write.preview,
        )

    def read_current_preview_page(
        self,
        *,
        subject_id: str,
        preview_ref: CoverLetterOverflowCorrectionPreviewRef,
        page_number: int,
    ) -> bytes | None:
        read = self.repository.get(
            subject_id=subject_id, preview_id=preview_ref.preview_id
        )
        if (
            read.status is not CoverLetterOverflowPreviewReadStatus.FOUND
            or read.preview is None
            or read.preview.reference != preview_ref
        ):
            return None
        current = self._current_target(
            subject_id, read.preview.correction_target_ref
        )
        if isinstance(current, CoverLetterOverflowPreviewResult):
            return None
        target, payload = current
        if not _preview_matches(read.preview, target, payload):
            return None
        source = self.source_provider.get(
            subject_id=subject_id,
            source_record_id=payload.latex_source_id,
            source_version=payload.source_version,
            source_content_hash=payload.source_content_hash,
        )
        if (
            source.status is not CoverLetterOverflowSourceStatus.AVAILABLE
            or source.source is None
        ):
            return None
        return self.repository.read_page(
            subject_id=subject_id,
            preview_id=preview_ref.preview_id,
            page_number=page_number,
        )

    def _current_target(
        self,
        subject_id: str,
        reference: MaterialCorrectionTargetRef,
    ) -> (
        tuple[MaterialCorrectionTarget, CoverLetterLayoutCorrectionTarget]
        | CoverLetterOverflowPreviewResult
    ):
        try:
            read = self.target_repository.get(
                subject_id=subject_id, target_id=reference.target_id
            )
            if (
                read.status is not MaterialCorrectionTargetReadStatus.FOUND
                or read.target is None
                or read.target.reference != reference
            ):
                return CoverLetterOverflowPreviewResult(
                    CoverLetterOverflowPreviewStatus.TARGET_STALE
                )
            item = self.current_item_reader(
                subject_id, read.target.attention_item_id
            )
            if item is None:
                return CoverLetterOverflowPreviewResult(
                    CoverLetterOverflowPreviewStatus.ITEM_NOT_CURRENT
                )
            typed = self.target_provider.get_current_typed_target(item=item)
            if (
                typed.status is not MaterialCorrectionTargetStatus.AVAILABLE
                or typed.target is None
                or typed.target.reference != reference
            ):
                return CoverLetterOverflowPreviewResult(
                    CoverLetterOverflowPreviewStatus.TARGET_STALE
                )
            target = typed.target
            if (
                target.kind
                is not MaterialCorrectionTargetKind.COVER_LETTER_LAYOUT
                or not isinstance(
                    target.payload, CoverLetterLayoutCorrectionTarget
                )
            ):
                return CoverLetterOverflowPreviewResult(
                    CoverLetterOverflowPreviewStatus.UNSUPPORTED_TARGET
                )
            return target, target.payload
        except (OSError, RuntimeError, TypeError, ValueError):
            return CoverLetterOverflowPreviewResult(
                CoverLetterOverflowPreviewStatus.FAILED
            )


def _overflow_evaluation_id(
    *,
    target: MaterialCorrectionTarget,
    payload: CoverLetterLayoutCorrectionTarget,
    compiler: LatexCompilerDescription,
    page_count: int,
) -> str:
    evaluation = {
        "application_plan_id": target.application_plan_id,
        "compiler_engine": compiler.engine,
        "compiler_version": compiler.compiler_version,
        "page_count": page_count,
        "policy_version": COVER_LETTER_PUBLICATION_POLICY_VERSION,
        "source_sha256": payload.source_content_hash,
        "subject_id": target.subject_id,
        "template_id": MANAGED_COVER_LETTER_TEMPLATE_ID,
        "template_version": payload.source_version,
    }
    return f"cover-letter-overflow-evaluation-{_canonical_hash(evaluation)}"


def _logical_identity(
    *,
    target: MaterialCorrectionTarget,
    payload: CoverLetterLayoutCorrectionTarget,
    compiler: LatexCompilerDescription,
    renderer: PdfRendererDescription,
) -> dict[str, Any]:
    return {
        "compiler_engine": compiler.engine,
        "compiler_version": compiler.compiler_version,
        "compile_policy_version": compiler.compile_policy_version,
        "correction_target_ref": target.reference.to_dict(),
        "normalized_flags": list(compiler.normalized_flags),
        "overflow_evaluation_id": payload.overflow_evaluation_id,
        "renderer_contract_version": PDF_RENDERER_CONTRACT_VERSION,
        "renderer_dpi": renderer.dpi,
        "renderer_name": renderer.renderer_name,
        "renderer_version": renderer.renderer_version,
        "sandbox_policy_version": compiler.sandbox_policy_version,
        "source_content_hash": payload.source_content_hash,
        "source_record_id": payload.latex_source_id,
        "source_version": payload.source_version,
        "subject_id": target.subject_id,
    }


def _preview_content(
    *,
    preview_id: str,
    target: MaterialCorrectionTarget,
    payload: CoverLetterLayoutCorrectionTarget,
    compiler: LatexCompilerDescription,
    renderer: PdfRendererDescription,
    pdf_hash: str,
    preview_artifact_hash: str,
    pages: tuple[CoverLetterOverflowPreviewPage, ...],
) -> dict[str, Any]:
    return {
        "preview_id": preview_id,
        "preview_version": COVER_LETTER_OVERFLOW_PREVIEW_VERSION,
        "subject_id": target.subject_id,
        "application_plan_id": target.application_plan_id,
        "preparation_run_id": target.preparation_run_id,
        "attention_item_id": target.attention_item_id,
        "correction_target_ref": target.reference,
        "publication_result_id": payload.publication_result_id,
        "overflow_evaluation_id": payload.overflow_evaluation_id,
        "overflow_evaluation_version": (
            PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION
        ),
        "source_record_id": payload.latex_source_id,
        "source_version": payload.source_version,
        "source_content_hash": payload.source_content_hash,
        "compiled_artifact_id": f"cover-letter-overflow-pdf-{pdf_hash}",
        "compiled_artifact_version": (
            COVER_LETTER_OVERFLOW_RENDERED_ARTIFACT_VERSION
        ),
        "compiled_artifact_content_hash": pdf_hash,
        "preview_artifact_id": f"{preview_id}-pages",
        "preview_artifact_version": _PREVIEW_ARTIFACT_VERSION,
        "preview_artifact_hash": preview_artifact_hash,
        "compiler_engine": compiler.engine,
        "compiler_version": compiler.compiler_version,
        "compile_policy_version": compiler.compile_policy_version,
        "sandbox_policy_version": compiler.sandbox_policy_version,
        "normalized_flags": compiler.normalized_flags,
        "renderer_name": renderer.renderer_name,
        "renderer_version": renderer.renderer_version,
        "renderer_contract_version": PDF_RENDERER_CONTRACT_VERSION,
        "renderer_dpi": renderer.dpi,
        "pages": pages,
    }


def _preview_matches(
    preview: CoverLetterOverflowCorrectionPreview,
    target: MaterialCorrectionTarget,
    payload: CoverLetterLayoutCorrectionTarget,
) -> bool:
    return (
        preview.correction_target_ref == target.reference
        and preview.subject_id == target.subject_id
        and preview.application_plan_id == target.application_plan_id
        and preview.preparation_run_id == target.preparation_run_id
        and preview.attention_item_id == target.attention_item_id
        and preview.publication_result_id == payload.publication_result_id
        and preview.overflow_evaluation_id == payload.overflow_evaluation_id
        and preview.source_record_id == payload.latex_source_id
        and preview.source_version == payload.source_version
        and preview.source_content_hash == payload.source_content_hash
    )


def _public_result(
    status: CoverLetterOverflowPreviewStatus,
    preview: CoverLetterOverflowCorrectionPreview,
) -> CoverLetterOverflowPreviewResult:
    return CoverLetterOverflowPreviewResult(
        status=status,
        preview_ref=preview.reference,
        page_count=len(preview.pages),
        media_type=_MEDIA_TYPE,
    )


def _preview_from_dict(
    value: Mapping[str, Any],
) -> CoverLetterOverflowCorrectionPreview:
    return CoverLetterOverflowCorrectionPreview(
        preview_id=value["preview_id"],
        preview_version=value["preview_version"],
        preview_hash=value["preview_hash"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        preparation_run_id=value["preparation_run_id"],
        attention_item_id=value["attention_item_id"],
        correction_target_ref=MaterialCorrectionTargetRef(
            **value["correction_target_ref"]
        ),
        publication_result_id=value["publication_result_id"],
        overflow_evaluation_id=value["overflow_evaluation_id"],
        overflow_evaluation_version=value["overflow_evaluation_version"],
        source_record_id=value["source_record_id"],
        source_version=value["source_version"],
        source_content_hash=value["source_content_hash"],
        compiled_artifact_id=value["compiled_artifact_id"],
        compiled_artifact_version=value["compiled_artifact_version"],
        compiled_artifact_content_hash=value[
            "compiled_artifact_content_hash"
        ],
        preview_artifact_id=value["preview_artifact_id"],
        preview_artifact_version=value["preview_artifact_version"],
        preview_artifact_hash=value["preview_artifact_hash"],
        compiler_engine=value["compiler_engine"],
        compiler_version=value["compiler_version"],
        compile_policy_version=value["compile_policy_version"],
        sandbox_policy_version=value["sandbox_policy_version"],
        normalized_flags=tuple(value["normalized_flags"]),
        renderer_name=value["renderer_name"],
        renderer_version=value["renderer_version"],
        renderer_contract_version=value["renderer_contract_version"],
        renderer_dpi=value["renderer_dpi"],
        pages=tuple(
            CoverLetterOverflowPreviewPage(**page)
            for page in value["pages"]
        ),
        created_at=datetime.fromisoformat(
            value["created_at"].replace("Z", "+00:00")
        ),
    )


__all__ = [
    "COVER_LETTER_OVERFLOW_PREVIEW_REF_VERSION",
    "COVER_LETTER_OVERFLOW_PREVIEW_VERSION",
    "CoverLetterOverflowCorrectionPreview",
    "CoverLetterOverflowCorrectionPreviewRef",
    "CoverLetterOverflowPreviewProvider",
    "CoverLetterOverflowPreviewRepository",
    "CoverLetterOverflowPreviewResult",
    "CoverLetterOverflowPreviewStatus",
    "CoverLetterOverflowTypedPreviewResult",
    "CoverLetterOverflowSource",
    "CoverLetterOverflowSourceProvider",
    "CoverLetterOverflowSourceResult",
    "CoverLetterOverflowSourceStatus",
    "PrivateHomeCoverLetterOverflowPreviewRepository",
    "PrivateHomeCoverLetterOverflowSourceProvider",
]
