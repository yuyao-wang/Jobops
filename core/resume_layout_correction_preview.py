"""Safe, immutable previews for current Resume layout correction targets."""

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
from .material_correction_ref import MaterialCorrectionTargetRef
from .material_correction_target import (
    MaterialCorrectionTarget,
    MaterialCorrectionTargetKind,
    MaterialCorrectionTargetProvider,
    MaterialCorrectionTargetReadStatus,
    MaterialCorrectionTargetRepository,
    MaterialCorrectionTargetStatus,
    ResumeVisualLayoutCorrectionTarget,
    ResumeVisualLayoutOriginKind,
)
from .pdf_page_renderer import (
    PDF_RENDERER_CONTRACT_VERSION,
    PdfPageRendererPort,
    PdfRendererDescription,
    PdfRendererUnavailableError,
    RenderedPage,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_compilation import (
    ResumeCompilationReadStatus,
    ResumeCompilationRepository,
)


RESUME_LAYOUT_CORRECTION_PREVIEW_VERSION = (
    "resume-layout-correction-preview-v1"
)
RESUME_LAYOUT_CORRECTION_PREVIEW_REF_VERSION = (
    "resume-layout-correction-preview-ref-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ID_RE = re.compile(r"^resume-layout-preview-[a-f0-9]{64}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MEDIA_TYPE = "image/png"


class ResumeLayoutCorrectionPreviewStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNCHANGED = "UNCHANGED"
    ITEM_NOT_CURRENT = "ITEM_NOT_CURRENT"
    TARGET_STALE = "TARGET_STALE"
    SOURCE_ARTIFACT_MISSING = "SOURCE_ARTIFACT_MISSING"
    RENDERER_UNAVAILABLE = "RENDERER_UNAVAILABLE"
    PREVIEW_UNSAFE = "PREVIEW_UNSAFE"
    PREVIEW_INTEGRITY_FAILURE = "PREVIEW_INTEGRITY_FAILURE"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    FAILED = "FAILED"


class ResumeLayoutCorrectionPreviewReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeLayoutCorrectionPreviewWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeCompilationArtifactStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionPreviewRef:
    preview_id: str
    preview_version: str
    preview_hash: str

    def __post_init__(self) -> None:
        if _ID_RE.fullmatch(self.preview_id) is None:
            raise ValueError("preview ID is invalid")
        if self.preview_version != RESUME_LAYOUT_CORRECTION_PREVIEW_REF_VERSION:
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
    def from_opaque(cls, value: str) -> "ResumeLayoutCorrectionPreviewRef":
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
class ResumeLayoutCorrectionPreviewPage:
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
        _text("artifact_id", self.artifact_id, 180)
        if self.artifact_version != "rendered-preview-page-v1":
            raise ValueError("preview artifact version is unsupported")
        _hash("content_hash", self.content_hash)
        if type(self.byte_size) is not int or self.byte_size < 1:
            raise ValueError("preview byte size is invalid")
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
class ResumeLayoutCorrectionPreview:
    preview_id: str
    preview_version: str
    preview_hash: str
    subject_id: str
    application_plan_id: str
    preparation_run_id: str
    correction_target_ref: MaterialCorrectionTargetRef
    target_origin_kind: ResumeVisualLayoutOriginKind
    source_artifact_id: str
    source_artifact_version: str
    source_artifact_content_hash: str
    latex_source_id: str
    latex_source_content_hash: str
    final_layout_attempt_id: str | None
    renderer_name: str
    renderer_version: str
    renderer_contract_version: str
    renderer_dpi: int
    pages: tuple[ResumeLayoutCorrectionPreviewPage, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if self.preview_version != RESUME_LAYOUT_CORRECTION_PREVIEW_VERSION:
            raise ValueError("preview contract version is unsupported")
        if _ID_RE.fullmatch(self.preview_id) is None:
            raise ValueError("preview ID is invalid")
        _hash("preview_hash", self.preview_hash)
        for name, value in (
            ("subject_id", self.subject_id),
            ("application_plan_id", self.application_plan_id),
            ("preparation_run_id", self.preparation_run_id),
            ("source_artifact_id", self.source_artifact_id),
            ("source_artifact_version", self.source_artifact_version),
            ("latex_source_id", self.latex_source_id),
            ("renderer_name", self.renderer_name),
            ("renderer_version", self.renderer_version),
        ):
            _text(name, value, 240)
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ):
            raise TypeError("correction target reference must be typed")
        object.__setattr__(
            self,
            "target_origin_kind",
            ResumeVisualLayoutOriginKind(self.target_origin_kind),
        )
        _hash("source_artifact_content_hash", self.source_artifact_content_hash)
        _hash("latex_source_content_hash", self.latex_source_content_hash)
        if self.final_layout_attempt_id is not None:
            _text("final_layout_attempt_id", self.final_layout_attempt_id, 300)
        if self.renderer_contract_version != PDF_RENDERER_CONTRACT_VERSION:
            raise ValueError("renderer contract version is unsupported")
        if type(self.renderer_dpi) is not int or not 36 <= self.renderer_dpi <= 600:
            raise ValueError("renderer DPI is invalid")
        if (
            not isinstance(self.pages, tuple)
            or not self.pages
            or any(
                not isinstance(page, ResumeLayoutCorrectionPreviewPage)
                for page in self.pages
            )
            or tuple(page.page_number for page in self.pages)
            != tuple(range(1, len(self.pages) + 1))
        ):
            raise ValueError("preview page set is invalid")
        _aware("created_at", self.created_at)
        expected_id = _canonical_hash(self.logical_identity_dict())
        if self.preview_id != f"resume-layout-preview-{expected_id}":
            raise ValueError("preview logical identity is invalid")
        if self.preview_hash != _canonical_hash(self.content_dict()):
            raise ValueError("preview canonical hash is invalid")

    @property
    def reference(self) -> ResumeLayoutCorrectionPreviewRef:
        return ResumeLayoutCorrectionPreviewRef(
            self.preview_id,
            RESUME_LAYOUT_CORRECTION_PREVIEW_REF_VERSION,
            self.preview_hash,
        )

    def logical_identity_dict(self) -> dict[str, Any]:
        return {
            "correction_target_ref": self.correction_target_ref.to_dict(),
            "renderer_contract_version": self.renderer_contract_version,
            "renderer_dpi": self.renderer_dpi,
            "renderer_name": self.renderer_name,
            "renderer_version": self.renderer_version,
            "source_artifact_content_hash": self.source_artifact_content_hash,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_version": self.source_artifact_version,
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "correction_target_ref": self.correction_target_ref.to_dict(),
            "final_layout_attempt_id": self.final_layout_attempt_id,
            "latex_source_content_hash": self.latex_source_content_hash,
            "latex_source_id": self.latex_source_id,
            "pages": [page.to_dict() for page in self.pages],
            "preparation_run_id": self.preparation_run_id,
            "preview_id": self.preview_id,
            "preview_version": self.preview_version,
            "renderer_contract_version": self.renderer_contract_version,
            "renderer_dpi": self.renderer_dpi,
            "renderer_name": self.renderer_name,
            "renderer_version": self.renderer_version,
            "source_artifact_content_hash": self.source_artifact_content_hash,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_version": self.source_artifact_version,
            "subject_id": self.subject_id,
            "target_origin_kind": self.target_origin_kind.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "created_at": self.created_at.isoformat(),
            "preview_hash": self.preview_hash,
        }


@dataclass(frozen=True, slots=True)
class ResumeCompilationArtifact:
    subject_id: str
    record_id: str
    record_version: str
    latex_version_id: str
    pdf_hash: str
    page_count: int
    content: bytes


@dataclass(frozen=True, slots=True)
class ResumeCompilationArtifactResult:
    status: ResumeCompilationArtifactStatus
    artifact: ResumeCompilationArtifact | None


@runtime_checkable
class ResumeCompilationArtifactProvider(Protocol):
    def get(
        self, *, subject_id: str, compilation_record_id: str
    ) -> ResumeCompilationArtifactResult: ...


class PrivateHomeResumeCompilationArtifactProvider:
    """Resolve a formal Compilation record to verified bytes, never a path."""

    def __init__(
        self,
        *,
        repository: ResumeCompilationRepository,
        home: PrivateHome | None = None,
    ) -> None:
        self._repository = repository
        self._home = home or PrivateHome.discover()

    def get(
        self, *, subject_id: str, compilation_record_id: str
    ) -> ResumeCompilationArtifactResult:
        try:
            read = self._repository.get(
                subject_id=subject_id, record_id=compilation_record_id
            )
            if read.status is ResumeCompilationReadStatus.NOT_FOUND:
                return ResumeCompilationArtifactResult(
                    ResumeCompilationArtifactStatus.NOT_FOUND, None
                )
            if (
                read.status is not ResumeCompilationReadStatus.FOUND
                or read.record is None
            ):
                raise ValueError("Compilation record is invalid")
            record = read.record
            managed = self._home.contained_path(record.pdf_reference)
            if managed.is_symlink() or not managed.is_file():
                raise ValueError("Compilation artifact is invalid")
            content = managed.read_bytes()
            if (
                len(content) != record.pdf_byte_size
                or hashlib.sha256(content).hexdigest() != record.pdf_sha256
                or not content.startswith(b"%PDF-")
            ):
                raise ValueError("Compilation artifact is invalid")
            return ResumeCompilationArtifactResult(
                ResumeCompilationArtifactStatus.AVAILABLE,
                ResumeCompilationArtifact(
                    record.subject_id,
                    record.record_id,
                    record.contract_version,
                    record.latex_version_id,
                    record.pdf_sha256,
                    record.page_count,
                    content,
                ),
            )
        except (OSError, PrivateHomeError, TypeError, ValueError):
            return ResumeCompilationArtifactResult(
                ResumeCompilationArtifactStatus.INTEGRITY_FAILURE, None
            )


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionPreviewReadResult:
    status: ResumeLayoutCorrectionPreviewReadStatus
    preview: ResumeLayoutCorrectionPreview | None


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionPreviewWriteResult:
    status: ResumeLayoutCorrectionPreviewWriteStatus
    preview: ResumeLayoutCorrectionPreview | None


@runtime_checkable
class ResumeLayoutCorrectionPreviewRepository(Protocol):
    def save(
        self,
        preview: ResumeLayoutCorrectionPreview,
        page_bytes: tuple[bytes, ...],
    ) -> ResumeLayoutCorrectionPreviewWriteResult: ...

    def get(
        self, *, subject_id: str, preview_id: str
    ) -> ResumeLayoutCorrectionPreviewReadResult: ...

    def read_page(
        self, *, subject_id: str, preview_id: str, page_number: int
    ) -> bytes | None: ...


class PrivateHomeResumeLayoutCorrectionPreviewRepository:
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
            / "resume-layout-correction-previews"
            / f"subject-{subject_key}"
            / preview_id
        )

    def save(
        self,
        preview: ResumeLayoutCorrectionPreview,
        page_bytes: tuple[bytes, ...],
    ) -> ResumeLayoutCorrectionPreviewWriteResult:
        if (
            not isinstance(preview, ResumeLayoutCorrectionPreview)
            or len(page_bytes) != len(preview.pages)
        ):
            raise TypeError("preview write is invalid")
        directory = self._directory(preview.subject_id, preview.preview_id)
        with self._lock:
            existing = self.get(
                subject_id=preview.subject_id, preview_id=preview.preview_id
            )
            if existing.status is ResumeLayoutCorrectionPreviewReadStatus.FOUND:
                if (
                    existing.preview is not None
                    and existing.preview.preview_hash == preview.preview_hash
                ):
                    return ResumeLayoutCorrectionPreviewWriteResult(
                        ResumeLayoutCorrectionPreviewWriteStatus.UNCHANGED,
                        existing.preview,
                    )
                return ResumeLayoutCorrectionPreviewWriteResult(
                    ResumeLayoutCorrectionPreviewWriteStatus.FAILED, None
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
                return ResumeLayoutCorrectionPreviewWriteResult(
                    ResumeLayoutCorrectionPreviewWriteStatus.FAILED, None
                )
            read = self.get(
                subject_id=preview.subject_id, preview_id=preview.preview_id
            )
            return ResumeLayoutCorrectionPreviewWriteResult(
                ResumeLayoutCorrectionPreviewWriteStatus.CREATED,
                read.preview,
            ) if (
                read.status is ResumeLayoutCorrectionPreviewReadStatus.FOUND
                and read.preview is not None
                and read.preview.preview_hash == preview.preview_hash
            ) else ResumeLayoutCorrectionPreviewWriteResult(
                ResumeLayoutCorrectionPreviewWriteStatus.FAILED, None
            )

    def get(
        self, *, subject_id: str, preview_id: str
    ) -> ResumeLayoutCorrectionPreviewReadResult:
        directory = self._directory(subject_id, preview_id)
        path = directory / "record.json"
        with self._lock:
            if not path.exists():
                return ResumeLayoutCorrectionPreviewReadResult(
                    ResumeLayoutCorrectionPreviewReadStatus.NOT_FOUND, None
                )
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("preview record is invalid")
                preview = _preview_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                if (
                    preview.subject_id != subject_id.strip()
                    or preview.preview_id != preview_id
                ):
                    raise ValueError("preview binding is invalid")
                for page in preview.pages:
                    content = (
                        directory / f"page-{page.page_number}.png"
                    ).read_bytes()
                    if (
                        not content.startswith(_PNG_SIGNATURE)
                        or len(content) != page.byte_size
                        or hashlib.sha256(content).hexdigest()
                        != page.content_hash
                    ):
                        raise ValueError("preview page integrity failed")
                return ResumeLayoutCorrectionPreviewReadResult(
                    ResumeLayoutCorrectionPreviewReadStatus.FOUND, preview
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return ResumeLayoutCorrectionPreviewReadResult(
                    ResumeLayoutCorrectionPreviewReadStatus.INTEGRITY_FAILURE,
                    None,
                )

    def read_page(
        self, *, subject_id: str, preview_id: str, page_number: int
    ) -> bytes | None:
        read = self.get(subject_id=subject_id, preview_id=preview_id)
        if (
            read.status is not ResumeLayoutCorrectionPreviewReadStatus.FOUND
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
class ResumeLayoutCorrectionPreviewResult:
    status: ResumeLayoutCorrectionPreviewStatus
    preview_ref: ResumeLayoutCorrectionPreviewRef | None = None
    page_count: int | None = None
    media_type: str | None = None
    origin_kind: ResumeVisualLayoutOriginKind | None = None


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionTypedPreviewResult:
    status: ResumeLayoutCorrectionPreviewStatus
    preview: ResumeLayoutCorrectionPreview | None


CurrentAttentionItemReader = Callable[
    [str, str], HumanAttentionQueueItem | None
]


@dataclass(slots=True)
class ResumeLayoutCorrectionPreviewProvider:
    target_repository: MaterialCorrectionTargetRepository
    target_provider: MaterialCorrectionTargetProvider
    current_item_reader: CurrentAttentionItemReader
    artifact_provider: ResumeCompilationArtifactProvider
    renderer: PdfPageRendererPort
    repository: ResumeLayoutCorrectionPreviewRepository
    clock: Callable[[], datetime]

    def get_current_resume_layout_correction_preview(
        self,
        *,
        subject_id: str,
        correction_target_ref: MaterialCorrectionTargetRef,
    ) -> ResumeLayoutCorrectionTypedPreviewResult:
        """Read an already-created preview; resolution may not create one."""

        validated = self._current_target(subject_id, correction_target_ref)
        if isinstance(validated, ResumeLayoutCorrectionPreviewResult):
            return ResumeLayoutCorrectionTypedPreviewResult(
                validated.status, None
            )
        target, payload = validated
        try:
            description = self.renderer.describe()
            if not isinstance(description, PdfRendererDescription):
                raise ValueError("renderer description is invalid")
            preview_id = "resume-layout-preview-" + _canonical_hash(
                _logical_identity(
                    target=target, payload=payload, renderer=description
                )
            )
            read = self.repository.get(
                subject_id=subject_id, preview_id=preview_id
            )
            if read.status is ResumeLayoutCorrectionPreviewReadStatus.NOT_FOUND:
                return ResumeLayoutCorrectionTypedPreviewResult(
                    ResumeLayoutCorrectionPreviewStatus.SOURCE_ARTIFACT_MISSING,
                    None,
                )
            if (
                read.status
                is not ResumeLayoutCorrectionPreviewReadStatus.FOUND
                or read.preview is None
            ):
                return ResumeLayoutCorrectionTypedPreviewResult(
                    ResumeLayoutCorrectionPreviewStatus
                    .PREVIEW_INTEGRITY_FAILURE,
                    None,
                )
            preview = read.preview
            if (
                preview.correction_target_ref != correction_target_ref
                or preview.subject_id != target.subject_id
                or preview.application_plan_id != target.application_plan_id
                or preview.preparation_run_id != target.preparation_run_id
                or preview.source_artifact_id != payload.artifact_id
                or preview.source_artifact_version
                != payload.artifact_version
                or preview.source_artifact_content_hash
                != payload.artifact_content_hash
                or preview.latex_source_id != payload.latex_source_id
                or preview.latex_source_content_hash
                != payload.latex_source_content_hash
                or preview.final_layout_attempt_id
                != payload.final_attempt_id
            ):
                return ResumeLayoutCorrectionTypedPreviewResult(
                    ResumeLayoutCorrectionPreviewStatus.TARGET_STALE, None
                )
            return ResumeLayoutCorrectionTypedPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.AVAILABLE, preview
            )
        except PdfRendererUnavailableError:
            return ResumeLayoutCorrectionTypedPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.RENDERER_UNAVAILABLE, None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return ResumeLayoutCorrectionTypedPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.FAILED, None
            )

    def get_or_create_resume_layout_correction_preview(
        self,
        *,
        subject_id: str,
        correction_target_ref: MaterialCorrectionTargetRef,
    ) -> ResumeLayoutCorrectionPreviewResult:
        validated = self._current_target(subject_id, correction_target_ref)
        if isinstance(validated, ResumeLayoutCorrectionPreviewResult):
            return validated
        target, payload = validated
        try:
            description = self.renderer.describe()
            if not isinstance(description, PdfRendererDescription):
                raise PdfRendererUnavailableError("invalid renderer")
        except (PdfRendererUnavailableError, OSError, RuntimeError):
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.RENDERER_UNAVAILABLE
            )
        except (TypeError, ValueError):
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        preview_id = "resume-layout-preview-" + _canonical_hash(
            _logical_identity(
                target=target, payload=payload, renderer=description
            )
        )
        existing = self.repository.get(
            subject_id=subject_id, preview_id=preview_id
        )
        if existing.status is ResumeLayoutCorrectionPreviewReadStatus.FOUND:
            preview = existing.preview
            if (
                preview is None
                or preview.correction_target_ref != correction_target_ref
                or preview.source_artifact_id != payload.artifact_id
                or preview.source_artifact_content_hash
                != payload.artifact_content_hash
            ):
                return ResumeLayoutCorrectionPreviewResult(
                    ResumeLayoutCorrectionPreviewStatus.PREVIEW_INTEGRITY_FAILURE
                )
            return _public_result(
                ResumeLayoutCorrectionPreviewStatus.UNCHANGED, preview
            )
        if (
            existing.status
            is ResumeLayoutCorrectionPreviewReadStatus.INTEGRITY_FAILURE
        ):
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        artifact_read = self.artifact_provider.get(
            subject_id=subject_id,
            compilation_record_id=payload.artifact_id,
        )
        if artifact_read.status is ResumeCompilationArtifactStatus.NOT_FOUND:
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.SOURCE_ARTIFACT_MISSING
            )
        artifact = artifact_read.artifact
        if (
            artifact_read.status is not ResumeCompilationArtifactStatus.AVAILABLE
            or artifact is None
            or artifact.subject_id != target.subject_id
            or artifact.record_id != payload.artifact_id
            or artifact.pdf_hash != payload.artifact_content_hash
            or artifact.latex_version_id != payload.artifact_version
        ):
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.TARGET_STALE
            )
        try:
            rendered = self.renderer.render(artifact.content)
        except PdfRendererUnavailableError:
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.RENDERER_UNAVAILABLE
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.FAILED
            )
        if (
            not isinstance(rendered, tuple)
            or not rendered
            or len(rendered) != artifact.page_count
            or any(not isinstance(page, RenderedPage) for page in rendered)
            or tuple(page.page_number for page in rendered)
            != tuple(range(1, len(rendered) + 1))
        ):
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        if any(
            page.image_format.upper() != "PNG"
            or not page.image_bytes.startswith(_PNG_SIGNATURE)
            for page in rendered
        ):
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.PREVIEW_UNSAFE
            )
        pages = tuple(
            ResumeLayoutCorrectionPreviewPage(
                page_number=page.page_number,
                artifact_id=(
                    f"{preview_id}-page-{page.page_number}"
                ),
                artifact_version="rendered-preview-page-v1",
                content_hash=hashlib.sha256(page.image_bytes).hexdigest(),
                byte_size=len(page.image_bytes),
                width_px=page.width_px,
                height_px=page.height_px,
            )
            for page in rendered
        )
        content = _preview_content(
            preview_id=preview_id,
            target=target,
            payload=payload,
            renderer=description,
            pages=pages,
        )
        preview = ResumeLayoutCorrectionPreview(
            **content,
            preview_hash=_canonical_hash(content),
            created_at=self.clock(),
        )
        write = self.repository.save(
            preview, tuple(page.image_bytes for page in rendered)
        )
        if write.preview is None:
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.PREVIEW_INTEGRITY_FAILURE
            )
        return _public_result(
            (
                ResumeLayoutCorrectionPreviewStatus.AVAILABLE
                if write.status
                is ResumeLayoutCorrectionPreviewWriteStatus.CREATED
                else ResumeLayoutCorrectionPreviewStatus.UNCHANGED
            ),
            write.preview,
        )

    def read_current_preview_page(
        self,
        *,
        subject_id: str,
        preview_ref: ResumeLayoutCorrectionPreviewRef,
        page_number: int,
    ) -> bytes | None:
        read = self.repository.get(
            subject_id=subject_id, preview_id=preview_ref.preview_id
        )
        if (
            read.status is not ResumeLayoutCorrectionPreviewReadStatus.FOUND
            or read.preview is None
            or read.preview.reference != preview_ref
        ):
            return None
        current = self._current_target(
            subject_id, read.preview.correction_target_ref
        )
        if isinstance(current, ResumeLayoutCorrectionPreviewResult):
            return None
        target, payload = current
        if (
            target.reference != read.preview.correction_target_ref
            or payload.artifact_id != read.preview.source_artifact_id
            or payload.artifact_content_hash
            != read.preview.source_artifact_content_hash
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
        tuple[MaterialCorrectionTarget, ResumeVisualLayoutCorrectionTarget]
        | ResumeLayoutCorrectionPreviewResult
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
                return ResumeLayoutCorrectionPreviewResult(
                    ResumeLayoutCorrectionPreviewStatus.TARGET_STALE
                )
            item = self.current_item_reader(
                subject_id, read.target.attention_item_id
            )
            if item is None:
                return ResumeLayoutCorrectionPreviewResult(
                    ResumeLayoutCorrectionPreviewStatus.ITEM_NOT_CURRENT
                )
            typed = self.target_provider.get_current_typed_target(item=item)
            if (
                typed.status is not MaterialCorrectionTargetStatus.AVAILABLE
                or typed.target is None
                or typed.target.reference != reference
            ):
                return ResumeLayoutCorrectionPreviewResult(
                    ResumeLayoutCorrectionPreviewStatus.TARGET_STALE
                )
            target = typed.target
            if (
                target.kind
                is not MaterialCorrectionTargetKind.RESUME_VISUAL_LAYOUT
                or not isinstance(
                    target.payload, ResumeVisualLayoutCorrectionTarget
                )
            ):
                return ResumeLayoutCorrectionPreviewResult(
                    ResumeLayoutCorrectionPreviewStatus.UNSUPPORTED_TARGET
                )
            return target, target.payload
        except (OSError, RuntimeError, TypeError, ValueError):
            return ResumeLayoutCorrectionPreviewResult(
                ResumeLayoutCorrectionPreviewStatus.FAILED
            )


def _logical_identity(
    *,
    target: MaterialCorrectionTarget,
    payload: ResumeVisualLayoutCorrectionTarget,
    renderer: PdfRendererDescription,
) -> dict[str, Any]:
    return {
        "correction_target_ref": target.reference.to_dict(),
        "renderer_contract_version": PDF_RENDERER_CONTRACT_VERSION,
        "renderer_dpi": renderer.dpi,
        "renderer_name": renderer.renderer_name,
        "renderer_version": renderer.renderer_version,
        "source_artifact_content_hash": payload.artifact_content_hash,
        "source_artifact_id": payload.artifact_id,
        "source_artifact_version": payload.artifact_version,
        "subject_id": target.subject_id,
    }


def _preview_content(
    *,
    preview_id: str,
    target: MaterialCorrectionTarget,
    payload: ResumeVisualLayoutCorrectionTarget,
    renderer: PdfRendererDescription,
    pages: tuple[ResumeLayoutCorrectionPreviewPage, ...],
) -> dict[str, Any]:
    return {
        "preview_id": preview_id,
        "preview_version": RESUME_LAYOUT_CORRECTION_PREVIEW_VERSION,
        "subject_id": target.subject_id,
        "application_plan_id": target.application_plan_id,
        "preparation_run_id": target.preparation_run_id,
        "correction_target_ref": target.reference,
        "target_origin_kind": payload.origin_kind,
        "source_artifact_id": payload.artifact_id,
        "source_artifact_version": payload.artifact_version,
        "source_artifact_content_hash": payload.artifact_content_hash,
        "latex_source_id": payload.latex_source_id,
        "latex_source_content_hash": payload.latex_source_content_hash,
        "final_layout_attempt_id": payload.final_attempt_id,
        "renderer_name": renderer.renderer_name,
        "renderer_version": renderer.renderer_version,
        "renderer_contract_version": PDF_RENDERER_CONTRACT_VERSION,
        "renderer_dpi": renderer.dpi,
        "pages": pages,
    }


def _public_result(
    status: ResumeLayoutCorrectionPreviewStatus,
    preview: ResumeLayoutCorrectionPreview,
) -> ResumeLayoutCorrectionPreviewResult:
    return ResumeLayoutCorrectionPreviewResult(
        status=status,
        preview_ref=preview.reference,
        page_count=len(preview.pages),
        media_type=_MEDIA_TYPE,
        origin_kind=preview.target_origin_kind,
    )


def _preview_from_dict(value: Any) -> ResumeLayoutCorrectionPreview:
    if not isinstance(value, Mapping):
        raise ValueError("preview record is invalid")
    return ResumeLayoutCorrectionPreview(
        preview_id=value["preview_id"],
        preview_version=value["preview_version"],
        preview_hash=value["preview_hash"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        preparation_run_id=value["preparation_run_id"],
        correction_target_ref=MaterialCorrectionTargetRef.from_dict(
            value["correction_target_ref"]
        ),
        target_origin_kind=value["target_origin_kind"],
        source_artifact_id=value["source_artifact_id"],
        source_artifact_version=value["source_artifact_version"],
        source_artifact_content_hash=value[
            "source_artifact_content_hash"
        ],
        latex_source_id=value["latex_source_id"],
        latex_source_content_hash=value["latex_source_content_hash"],
        final_layout_attempt_id=value["final_layout_attempt_id"],
        renderer_name=value["renderer_name"],
        renderer_version=value["renderer_version"],
        renderer_contract_version=value["renderer_contract_version"],
        renderer_dpi=value["renderer_dpi"],
        pages=tuple(
            ResumeLayoutCorrectionPreviewPage(**page)
            for page in value["pages"]
        ),
        created_at=datetime.fromisoformat(value["created_at"]),
    )


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=(
                lambda item: (
                    item.to_dict()
                    if hasattr(item, "to_dict")
                    else item.value
                )
            ),
        ).encode()
    ).hexdigest()


def _text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


__all__ = [
    "PrivateHomeResumeCompilationArtifactProvider",
    "PrivateHomeResumeLayoutCorrectionPreviewRepository",
    "RESUME_LAYOUT_CORRECTION_PREVIEW_REF_VERSION",
    "RESUME_LAYOUT_CORRECTION_PREVIEW_VERSION",
    "ResumeCompilationArtifact",
    "ResumeCompilationArtifactProvider",
    "ResumeCompilationArtifactResult",
    "ResumeCompilationArtifactStatus",
    "ResumeLayoutCorrectionPreview",
    "ResumeLayoutCorrectionPreviewPage",
    "ResumeLayoutCorrectionPreviewProvider",
    "ResumeLayoutCorrectionPreviewRef",
    "ResumeLayoutCorrectionPreviewResult",
    "ResumeLayoutCorrectionPreviewStatus",
    "ResumeLayoutCorrectionTypedPreviewResult",
]
