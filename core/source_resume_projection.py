"""Deterministic, hash-bound projections of managed resume artifacts."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable
from xml.etree import ElementTree

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

from .private_home import PrivateHome, PrivateHomeError
from .application_preparation_orchestrator import (
    SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PublicPreparationStageResult,
    SourceResumeProjectionStopReason,
)
from .resume_candidates import (
    MAX_RESUME_ARTIFACT_BYTES,
    ResumeArtifactType,
    ResumeCandidate,
    ResumeCandidateReadStatus,
    ResumeCandidateRepository,
)


SOURCE_RESUME_PROJECTION_CONTRACT_VERSION = "source-resume-projection-v1"
SOURCE_RESUME_PARSER_VERSION = "source-resume-parser-v1"
MAX_DOCX_DOCUMENT_XML_BYTES = 10 * 1024 * 1024
MAX_SOURCE_BLOCK_CHARS = 20_000
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_PROJECTION_ID_PATTERN = re.compile(
    r"^source-resume-projection-[a-f0-9]{64}$"
)
_SECTION_ID_PATTERN = re.compile(r"^resume-section-[a-f0-9]{64}$")
_BLOCK_ID_PATTERN = re.compile(r"^resume-block-[a-f0-9]{64}$")
_BULLET_ID_PATTERN = re.compile(r"^resume-bullet-[a-f0-9]{64}$")
_BULLET_PREFIX = re.compile(
    r"^(?:[•◦▪‣⁃]\s*|[-*]\s+|\(?\d{1,3}[.)]\s+)"
)
_KNOWN_SECTION_TITLES = frozenset(
    {
        "awards",
        "certifications",
        "education",
        "experience",
        "professional experience",
        "professional summary",
        "projects",
        "publications",
        "research experience",
        "skills",
        "summary",
        "technical skills",
        "volunteer experience",
        "work experience",
    }
)
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_WORD_NS}}}"


class SourceResumeBlockKind(str, Enum):
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    BULLET = "BULLET"
    TABLE_CELL = "TABLE_CELL"


class SourceResumeLocatorKind(str, Enum):
    PDF_LINE = "PDF_LINE"
    DOCX_PARAGRAPH = "DOCX_PARAGRAPH"
    DOCX_TABLE_CELL_PARAGRAPH = "DOCX_TABLE_CELL_PARAGRAPH"


class SourceResumeProjectionWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class SourceResumeProjectionReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class SourceResumeProjectionStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    UNSUPPORTED = "UNSUPPORTED"
    UNREADABLE = "UNREADABLE"
    FAILED = "FAILED"


class SourceResumeProjectionFailureReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    RESUME_NOT_FOUND = "RESUME_NOT_FOUND"
    RESUME_INTEGRITY_FAILURE = "RESUME_INTEGRITY_FAILURE"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"
    ARTIFACT_UNREADABLE = "ARTIFACT_UNREADABLE"
    PROJECTION_PERSISTENCE_FAILED = "PROJECTION_PERSISTENCE_FAILED"
    PROJECTION_INTEGRITY_FAILURE = "PROJECTION_INTEGRITY_FAILURE"


class SourceResumeUnsupportedError(RuntimeError):
    """The managed artifact is valid but unsupported by the deterministic parser."""


class SourceResumeUnreadableError(RuntimeError):
    """The managed artifact cannot be read completely and safely."""


class SourceResumeArtifactIntegrityError(RuntimeError):
    """The managed artifact no longer matches its immutable candidate binding."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the source-resume contract")
    return cleaned


def _source_text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip() or len(value) > MAX_SOURCE_BLOCK_CHARS:
        raise ValueError(f"{name} is outside the source-resume contract")
    return value


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
        raise ValueError("projected_at is invalid")
    return _require_aware(
        "projected_at",
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


@dataclass(frozen=True, slots=True)
class SourceResumeLocator:
    kind: SourceResumeLocatorKind
    page_number: int | None = None
    line_index: int | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row_index: int | None = None
    cell_index: int | None = None
    cell_paragraph_index: int | None = None

    def __post_init__(self) -> None:
        kind = SourceResumeLocatorKind(self.kind)
        object.__setattr__(self, "kind", kind)
        values = {
            "page_number": self.page_number,
            "line_index": self.line_index,
            "paragraph_index": self.paragraph_index,
            "table_index": self.table_index,
            "row_index": self.row_index,
            "cell_index": self.cell_index,
            "cell_paragraph_index": self.cell_paragraph_index,
        }
        if any(
            item is not None and (type(item) is not int or item < 0)
            for item in values.values()
        ):
            raise ValueError("source locator indices must be non-negative integers")
        if kind is SourceResumeLocatorKind.PDF_LINE:
            if (
                self.page_number is None
                or self.page_number < 1
                or self.line_index is None
                or any(
                    values[name] is not None
                    for name in (
                        "paragraph_index",
                        "table_index",
                        "row_index",
                        "cell_index",
                        "cell_paragraph_index",
                    )
                )
            ):
                raise ValueError("PDF source locator is invalid")
        elif kind is SourceResumeLocatorKind.DOCX_PARAGRAPH:
            if self.paragraph_index is None or any(
                values[name] is not None
                for name in (
                    "page_number",
                    "line_index",
                    "table_index",
                    "row_index",
                    "cell_index",
                    "cell_paragraph_index",
                )
            ):
                raise ValueError("DOCX paragraph locator is invalid")
        elif any(
            values[name] is None
            for name in (
                "table_index",
                "row_index",
                "cell_index",
                "cell_paragraph_index",
            )
        ) or any(
            values[name] is not None
            for name in ("page_number", "line_index", "paragraph_index")
        ):
            raise ValueError("DOCX table-cell locator is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "page_number": self.page_number,
            "line_index": self.line_index,
            "paragraph_index": self.paragraph_index,
            "table_index": self.table_index,
            "row_index": self.row_index,
            "cell_index": self.cell_index,
            "cell_paragraph_index": self.cell_paragraph_index,
        }


def _stable_source_id(
    prefix: str,
    *,
    artifact_sha256: str,
    parser_version: str,
    locator: Mapping[str, Any],
    kind: str,
) -> str:
    digest = _canonical_hash(
        {
            "artifact_sha256": artifact_sha256,
            "contract_version": SOURCE_RESUME_PROJECTION_CONTRACT_VERSION,
            "kind": kind,
            "locator": dict(locator),
            "parser_version": parser_version,
        }
    )
    return f"{prefix}-{digest}"


@dataclass(frozen=True, slots=True)
class SourceResumeBlock:
    block_id: str
    section_id: str
    order: int
    kind: SourceResumeBlockKind
    text: str
    locator: SourceResumeLocator
    bullet_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.block_id, str)
            or _BLOCK_ID_PATTERN.fullmatch(self.block_id) is None
        ):
            raise ValueError("block_id is invalid")
        if (
            not isinstance(self.section_id, str)
            or _SECTION_ID_PATTERN.fullmatch(self.section_id) is None
        ):
            raise ValueError("section_id is invalid")
        if type(self.order) is not int or self.order < 0:
            raise ValueError("block order must be a non-negative integer")
        kind = SourceResumeBlockKind(self.kind)
        object.__setattr__(self, "kind", kind)
        _source_text("block text", self.text)
        if not isinstance(self.locator, SourceResumeLocator):
            raise TypeError("locator must be a SourceResumeLocator")
        if kind is SourceResumeBlockKind.BULLET:
            if (
                not isinstance(self.bullet_id, str)
                or _BULLET_ID_PATTERN.fullmatch(self.bullet_id) is None
            ):
                raise ValueError("bullet block requires a stable bullet_id")
        elif self.bullet_id is not None:
            raise ValueError("non-bullet block cannot carry bullet_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "section_id": self.section_id,
            "order": self.order,
            "kind": self.kind.value,
            "text": self.text,
            "locator": self.locator.to_dict(),
            "bullet_id": self.bullet_id,
        }


@dataclass(frozen=True, slots=True)
class SourceResumeSection:
    section_id: str
    order: int
    title: str | None
    blocks: tuple[SourceResumeBlock, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.section_id, str)
            or _SECTION_ID_PATTERN.fullmatch(self.section_id) is None
        ):
            raise ValueError("section_id is invalid")
        if type(self.order) is not int or self.order < 0:
            raise ValueError("section order must be a non-negative integer")
        if self.title is not None:
            _source_text("section title", self.title)
        if (
            not isinstance(self.blocks, tuple)
            or not self.blocks
            or any(not isinstance(item, SourceResumeBlock) for item in self.blocks)
        ):
            raise TypeError("section blocks must be a non-empty typed tuple")
        if tuple(item.order for item in self.blocks) != tuple(
            range(len(self.blocks))
        ):
            raise ValueError("section blocks must have contiguous stable order")
        if any(item.section_id != self.section_id for item in self.blocks):
            raise ValueError("section block binding is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "order": self.order,
            "title": self.title,
            "blocks": [item.to_dict() for item in self.blocks],
        }


def _projection_identity_payload(
    *,
    subject_id: str,
    resume_id: str,
    artifact_sha256: str,
    artifact_type: ResumeArtifactType,
    parser_version: str,
) -> dict[str, Any]:
    return {
        "artifact_sha256": artifact_sha256,
        "artifact_type": artifact_type.value,
        "contract_version": SOURCE_RESUME_PROJECTION_CONTRACT_VERSION,
        "parser_version": parser_version,
        "resume_id": resume_id,
        "subject_id": subject_id,
    }


def source_resume_projection_id(
    *,
    subject_id: str,
    resume_id: str,
    artifact_sha256: str,
    artifact_type: ResumeArtifactType,
    parser_version: str,
) -> str:
    return (
        "source-resume-projection-"
        + _canonical_hash(
            _projection_identity_payload(
                subject_id=subject_id,
                resume_id=resume_id,
                artifact_sha256=artifact_sha256,
                artifact_type=artifact_type,
                parser_version=parser_version,
            )
        )
    )


@dataclass(frozen=True, slots=True)
class SourceResumeProjection:
    projection_id: str
    contract_version: str
    parser_version: str
    subject_id: str
    resume_id: str
    artifact_sha256: str
    artifact_type: ResumeArtifactType
    sections: tuple[SourceResumeSection, ...]
    projection_content_hash: str
    projected_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != SOURCE_RESUME_PROJECTION_CONTRACT_VERSION:
            raise ValueError("source-resume projection contract is unsupported")
        parser = _clean_text("parser_version", self.parser_version, maximum=80)
        subject = _clean_text("subject_id", self.subject_id, maximum=160)
        resume = _clean_text("resume_id", self.resume_id, maximum=160)
        artifact_hash = _require_hash("artifact_sha256", self.artifact_sha256)
        artifact_type = ResumeArtifactType(self.artifact_type)
        _require_aware("projected_at", self.projected_at)
        if not isinstance(self.sections, tuple) or any(
            not isinstance(item, SourceResumeSection)
            for item in self.sections
        ):
            raise TypeError("sections must be a typed tuple")
        if tuple(item.order for item in self.sections) != tuple(
            range(len(self.sections))
        ):
            raise ValueError("sections must have contiguous stable order")
        section_ids = [item.section_id for item in self.sections]
        block_ids = [
            block.block_id
            for section in self.sections
            for block in section.blocks
        ]
        bullet_ids = [
            block.bullet_id
            for section in self.sections
            for block in section.blocks
            if block.bullet_id is not None
        ]
        if (
            len(section_ids) != len(set(section_ids))
            or len(block_ids) != len(set(block_ids))
            or len(bullet_ids) != len(set(bullet_ids))
        ):
            raise ValueError("projection source identities must be unique")
        expected_id = source_resume_projection_id(
            subject_id=subject,
            resume_id=resume,
            artifact_sha256=artifact_hash,
            artifact_type=artifact_type,
            parser_version=parser,
        )
        if (
            not isinstance(self.projection_id, str)
            or _PROJECTION_ID_PATTERN.fullmatch(self.projection_id) is None
            or self.projection_id != expected_id
        ):
            raise ValueError("projection_id does not match its binding")
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "parser_version", parser)
        object.__setattr__(self, "subject_id", subject)
        object.__setattr__(self, "resume_id", resume)
        object.__setattr__(self, "artifact_sha256", artifact_hash)
        object.__setattr__(self, "artifact_type", artifact_type)
        expected_hash = _canonical_hash(self.content_dict())
        content_hash = _require_hash(
            "projection_content_hash", self.projection_content_hash
        )
        if content_hash != expected_hash:
            raise ValueError("projection content hash is invalid")
        object.__setattr__(
            self, "projection_content_hash", content_hash
        )

    def content_dict(self) -> dict[str, Any]:
        return {
            **_projection_identity_payload(
                subject_id=self.subject_id,
                resume_id=self.resume_id,
                artifact_sha256=self.artifact_sha256,
                artifact_type=self.artifact_type,
                parser_version=self.parser_version,
            ),
            "projection_id": self.projection_id,
            "sections": [item.to_dict() for item in self.sections],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "projection_content_hash": self.projection_content_hash,
            "projected_at": _rfc3339(self.projected_at),
        }


@dataclass(frozen=True, slots=True)
class SourceResumeProjectionWriteResult:
    status: SourceResumeProjectionWriteStatus
    projection: SourceResumeProjection | None
    reason_code: SourceResumeProjectionFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = SourceResumeProjectionWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                SourceResumeProjectionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            SourceResumeProjectionWriteStatus.CREATED,
            SourceResumeProjectionWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.projection, SourceResumeProjection)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful projection write result is invalid")
        elif self.projection is not None or self.reason_code is None:
            raise ValueError("failed projection write result is invalid")


@dataclass(frozen=True, slots=True)
class SourceResumeProjectionReadResult:
    status: SourceResumeProjectionReadStatus
    projection: SourceResumeProjection | None
    reason_code: SourceResumeProjectionFailureReason | None = None

    def __post_init__(self) -> None:
        status = SourceResumeProjectionReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                SourceResumeProjectionFailureReason(self.reason_code),
            )
        if status is SourceResumeProjectionReadStatus.FOUND:
            if self.projection is None or self.reason_code is not None:
                raise ValueError("found projection read result is invalid")
        elif status is SourceResumeProjectionReadStatus.NOT_FOUND:
            if self.projection is not None or self.reason_code is not None:
                raise ValueError("not-found projection read result is invalid")
        elif (
            self.projection is not None
            or self.reason_code
            is not SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure projection read result is invalid")


@runtime_checkable
class SourceResumeProjectionRepository(Protocol):
    def find_current_for_resume(
        self,
        *,
        subject_id: str,
        resume_id: str,
        artifact_sha256: str,
    ) -> SourceResumeProjectionReadResult:
        """Return the latest immutable projection for one artifact binding."""

    def save(
        self, projection: SourceResumeProjection
    ) -> SourceResumeProjectionWriteResult:
        """Persist one immutable projection."""

    def get(
        self, *, subject_id: str, projection_id: str
    ) -> SourceResumeProjectionReadResult:
        """Read one projection with subject ownership enforcement."""


def _locator_from_dict(value: Any) -> SourceResumeLocator:
    expected = {
        "kind",
        "page_number",
        "line_index",
        "paragraph_index",
        "table_index",
        "row_index",
        "cell_index",
        "cell_paragraph_index",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted source locator is invalid")
    return SourceResumeLocator(
        kind=SourceResumeLocatorKind(value["kind"]),
        page_number=value["page_number"],
        line_index=value["line_index"],
        paragraph_index=value["paragraph_index"],
        table_index=value["table_index"],
        row_index=value["row_index"],
        cell_index=value["cell_index"],
        cell_paragraph_index=value["cell_paragraph_index"],
    )


def _projection_from_dict(value: Any) -> SourceResumeProjection:
    expected = {
        "projection_id",
        "contract_version",
        "parser_version",
        "subject_id",
        "resume_id",
        "artifact_sha256",
        "artifact_type",
        "sections",
        "projection_content_hash",
        "projected_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted SourceResumeProjection fields are invalid")
    raw_sections = value["sections"]
    if not isinstance(raw_sections, list):
        raise ValueError("persisted projection sections are invalid")
    sections: list[SourceResumeSection] = []
    for raw_section in raw_sections:
        if (
            not isinstance(raw_section, Mapping)
            or set(raw_section) != {"section_id", "order", "title", "blocks"}
            or not isinstance(raw_section["blocks"], list)
        ):
            raise ValueError("persisted projection section is invalid")
        blocks: list[SourceResumeBlock] = []
        for raw_block in raw_section["blocks"]:
            if (
                not isinstance(raw_block, Mapping)
                or set(raw_block)
                != {
                    "block_id",
                    "section_id",
                    "order",
                    "kind",
                    "text",
                    "locator",
                    "bullet_id",
                }
            ):
                raise ValueError("persisted projection block is invalid")
            blocks.append(
                SourceResumeBlock(
                    block_id=raw_block["block_id"],
                    section_id=raw_block["section_id"],
                    order=raw_block["order"],
                    kind=SourceResumeBlockKind(raw_block["kind"]),
                    text=raw_block["text"],
                    locator=_locator_from_dict(raw_block["locator"]),
                    bullet_id=raw_block["bullet_id"],
                )
            )
        sections.append(
            SourceResumeSection(
                section_id=raw_section["section_id"],
                order=raw_section["order"],
                title=raw_section["title"],
                blocks=tuple(blocks),
            )
        )
    return SourceResumeProjection(
        projection_id=value["projection_id"],
        contract_version=value["contract_version"],
        parser_version=value["parser_version"],
        subject_id=value["subject_id"],
        resume_id=value["resume_id"],
        artifact_sha256=value["artifact_sha256"],
        artifact_type=ResumeArtifactType(value["artifact_type"]),
        sections=tuple(sections),
        projection_content_hash=value["projection_content_hash"],
        projected_at=_parse_timestamp(value["projected_at"]),
    )


class PrivateHomeSourceResumeProjectionRepository:
    """Immutable Private Home storage for hash-bound resume projections."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _record_path(self, subject_id: str, projection_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(projection_id, str)
            or _PROJECTION_ID_PATTERN.fullmatch(projection_id) is None
        ):
            raise ValueError("projection_id is invalid")
        return (
            self._home.paths.source_resume_projections
            / _subject_storage_key(subject)
            / f"{projection_id}.json"
        )

    def get(
        self, *, subject_id: str, projection_id: str
    ) -> SourceResumeProjectionReadResult:
        path = self._record_path(subject_id, projection_id)
        with self._lock:
            if not path.exists():
                return SourceResumeProjectionReadResult(
                    status=SourceResumeProjectionReadStatus.NOT_FOUND,
                    projection=None,
                )
            if path.is_symlink() or not path.is_file():
                return SourceResumeProjectionReadResult(
                    status=SourceResumeProjectionReadStatus.INTEGRITY_FAILURE,
                    projection=None,
                    reason_code=(
                        SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE
                    ),
                )
            try:
                projection = _projection_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return SourceResumeProjectionReadResult(
                    status=SourceResumeProjectionReadStatus.INTEGRITY_FAILURE,
                    projection=None,
                    reason_code=(
                        SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE
                    ),
                )
            if (
                projection.subject_id != subject_id.strip()
                or projection.projection_id != projection_id
                or path.name != f"{projection.projection_id}.json"
            ):
                return SourceResumeProjectionReadResult(
                    status=SourceResumeProjectionReadStatus.INTEGRITY_FAILURE,
                    projection=None,
                    reason_code=(
                        SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE
                    ),
                )
            return SourceResumeProjectionReadResult(
                status=SourceResumeProjectionReadStatus.FOUND,
                projection=projection,
            )

    def find_current_for_resume(
        self,
        *,
        subject_id: str,
        resume_id: str,
        artifact_sha256: str,
    ) -> SourceResumeProjectionReadResult:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        resume = _clean_text("resume_id", resume_id, maximum=160)
        artifact_hash = _require_hash(
            "artifact_sha256", artifact_sha256
        )
        directory = (
            self._home.paths.source_resume_projections
            / _subject_storage_key(subject)
        )
        if not directory.exists():
            return SourceResumeProjectionReadResult(
                status=SourceResumeProjectionReadStatus.NOT_FOUND,
                projection=None,
            )
        if directory.is_symlink() or not directory.is_dir():
            return SourceResumeProjectionReadResult(
                status=SourceResumeProjectionReadStatus.INTEGRITY_FAILURE,
                projection=None,
                reason_code=(
                    SourceResumeProjectionFailureReason
                    .PROJECTION_INTEGRITY_FAILURE
                ),
            )
        try:
            paths = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
        except OSError:
            return SourceResumeProjectionReadResult(
                status=SourceResumeProjectionReadStatus.INTEGRITY_FAILURE,
                projection=None,
                reason_code=(
                    SourceResumeProjectionFailureReason
                    .PROJECTION_INTEGRITY_FAILURE
                ),
            )
        matches: list[SourceResumeProjection] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or _PROJECTION_ID_PATTERN.fullmatch(path.stem) is None
            ):
                return SourceResumeProjectionReadResult(
                    status=SourceResumeProjectionReadStatus.INTEGRITY_FAILURE,
                    projection=None,
                    reason_code=(
                        SourceResumeProjectionFailureReason
                        .PROJECTION_INTEGRITY_FAILURE
                    ),
                )
            result = self.get(
                subject_id=subject,
                projection_id=path.stem,
            )
            if (
                result.status is not SourceResumeProjectionReadStatus.FOUND
                or result.projection is None
            ):
                return SourceResumeProjectionReadResult(
                    status=SourceResumeProjectionReadStatus.INTEGRITY_FAILURE,
                    projection=None,
                    reason_code=(
                        SourceResumeProjectionFailureReason
                        .PROJECTION_INTEGRITY_FAILURE
                    ),
                )
            projection = result.projection
            if (
                projection.resume_id == resume
                and projection.artifact_sha256 == artifact_hash
            ):
                matches.append(projection)
        if not matches:
            return SourceResumeProjectionReadResult(
                status=SourceResumeProjectionReadStatus.NOT_FOUND,
                projection=None,
            )
        current = max(
            matches,
            key=lambda item: (
                item.projected_at.astimezone(timezone.utc),
                item.projection_id,
            ),
        )
        return SourceResumeProjectionReadResult(
            status=SourceResumeProjectionReadStatus.FOUND,
            projection=current,
        )

    def save(
        self, projection: SourceResumeProjection
    ) -> SourceResumeProjectionWriteResult:
        if not isinstance(projection, SourceResumeProjection):
            raise TypeError("projection must be a SourceResumeProjection")
        path = self._record_path(
            projection.subject_id, projection.projection_id
        )
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            projection.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return SourceResumeProjectionWriteResult(
                    status=SourceResumeProjectionWriteStatus.FAILED,
                    projection=None,
                    reason_code=(
                        SourceResumeProjectionFailureReason
                        .PROJECTION_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return SourceResumeProjectionWriteResult(
                    status=SourceResumeProjectionWriteStatus.CREATED,
                    projection=projection,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=projection.subject_id,
                projection_id=projection.projection_id,
            )
            if (
                existing.status is SourceResumeProjectionReadStatus.FOUND
                and existing.projection is not None
                and existing.projection.content_dict()
                == projection.content_dict()
            ):
                return SourceResumeProjectionWriteResult(
                    status=SourceResumeProjectionWriteStatus.UNCHANGED,
                    projection=existing.projection,
                    reason_code=None,
                    retryable=False,
                )
            return SourceResumeProjectionWriteResult(
                status=SourceResumeProjectionWriteStatus.FAILED,
                projection=None,
                reason_code=(
                    SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@runtime_checkable
class SourceResumeArtifactReader(Protocol):
    def read(self, candidate: ResumeCandidate) -> bytes:
        """Read and revalidate one managed artifact."""


class PrivateHomeSourceResumeArtifactReader:
    """Read managed artifact bytes without exposing a path to downstream code."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def read(self, candidate: ResumeCandidate) -> bytes:
        if not isinstance(candidate, ResumeCandidate):
            raise TypeError("candidate must be a ResumeCandidate")
        try:
            path = self._home.contained_path(candidate.artifact_reference)
            if path.is_symlink() or not path.is_file():
                raise SourceResumeArtifactIntegrityError(
                    "managed resume artifact is unavailable"
                )
            size = path.stat(follow_symlinks=False).st_size
            if size <= 0 or size > MAX_RESUME_ARTIFACT_BYTES:
                raise SourceResumeArtifactIntegrityError(
                    "managed resume artifact size is invalid"
                )
            content = path.read_bytes()
        except (OSError, PrivateHomeError) as exc:
            raise SourceResumeArtifactIntegrityError(
                "managed resume artifact cannot be read"
            ) from exc
        if hashlib.sha256(content).hexdigest() != candidate.artifact_sha256:
            raise SourceResumeArtifactIntegrityError(
                "managed resume artifact hash changed"
            )
        if candidate.artifact_type is ResumeArtifactType.PDF:
            valid = content.startswith(b"%PDF-")
        else:
            try:
                with zipfile.ZipFile(BytesIO(content)) as archive:
                    names = set(archive.namelist())
            except (OSError, zipfile.BadZipFile):
                valid = False
            else:
                valid = (
                    "[Content_Types].xml" in names
                    and "word/document.xml" in names
                )
        if not valid:
            raise SourceResumeArtifactIntegrityError(
                "managed resume artifact type is invalid"
            )
        return content


@dataclass(frozen=True, slots=True)
class _ParsedSourceBlock:
    kind: SourceResumeBlockKind
    text: str
    locator: SourceResumeLocator
    is_heading: bool


def _is_pdf_heading(text: str) -> bool:
    normalized = " ".join(text.split()).casefold()
    if normalized in _KNOWN_SECTION_TITLES:
        return True
    letters = [character for character in text if character.isalpha()]
    return (
        bool(letters)
        and len(text) <= 80
        and not text.rstrip().endswith((".", ";", ":"))
        and all(not character.islower() for character in letters)
    )


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
    return "".join(parts).strip()


def _docx_paragraph_kind(
    paragraph: ElementTree.Element,
    text: str,
    *,
    allow_heading: bool,
    table_cell: bool,
) -> tuple[SourceResumeBlockKind, bool]:
    properties = paragraph.find(f"{_W}pPr")
    style_value = ""
    numbered = False
    if properties is not None:
        style = properties.find(f"{_W}pStyle")
        if style is not None:
            style_value = str(style.attrib.get(f"{_W}val") or "")
        numbered = properties.find(f"{_W}numPr") is not None
    heading = allow_heading and (
        style_value.casefold().startswith("heading")
        or style_value.casefold() == "title"
    )
    if heading:
        return SourceResumeBlockKind.HEADING, True
    if numbered or _BULLET_PREFIX.match(text):
        return SourceResumeBlockKind.BULLET, False
    if table_cell:
        return SourceResumeBlockKind.TABLE_CELL, False
    return SourceResumeBlockKind.PARAGRAPH, False


@runtime_checkable
class SourceResumeParser(Protocol):
    @property
    def parser_version(self) -> str:
        """Return the version binding deterministic parsing behavior."""

    def parse(
        self,
        *,
        artifact_type: ResumeArtifactType,
        content: bytes,
    ) -> tuple[_ParsedSourceBlock, ...]:
        """Return all extracted source blocks in deterministic document order."""


class DeterministicSourceResumeParser:
    """Versioned PDF-line and DOCX-structure extraction without inference."""

    def __init__(self, parser_version: str = SOURCE_RESUME_PARSER_VERSION) -> None:
        self._parser_version = _clean_text(
            "parser_version", parser_version, maximum=80
        )

    @property
    def parser_version(self) -> str:
        return self._parser_version

    def _parse_pdf(self, content: bytes) -> tuple[_ParsedSourceBlock, ...]:
        blocks: list[_ParsedSourceBlock] = []
        try:
            with pdfplumber.open(BytesIO(content)) as document:
                for page_number, page in enumerate(document.pages, start=1):
                    extracted = page.extract_text(
                        x_tolerance=3,
                        y_tolerance=3,
                        layout=False,
                    )
                    if extracted is None:
                        continue
                    for line_index, raw_line in enumerate(
                        extracted.splitlines()
                    ):
                        text = raw_line.strip()
                        if not text:
                            continue
                        heading = _is_pdf_heading(text)
                        kind = (
                            SourceResumeBlockKind.HEADING
                            if heading
                            else (
                                SourceResumeBlockKind.BULLET
                                if _BULLET_PREFIX.match(text)
                                else SourceResumeBlockKind.PARAGRAPH
                            )
                        )
                        blocks.append(
                            _ParsedSourceBlock(
                                kind=kind,
                                text=text,
                                locator=SourceResumeLocator(
                                    kind=SourceResumeLocatorKind.PDF_LINE,
                                    page_number=page_number,
                                    line_index=line_index,
                                ),
                                is_heading=heading,
                            )
                        )
        except PDFPasswordIncorrect as exc:
            raise SourceResumeUnsupportedError(
                "encrypted PDF is unsupported"
            ) from exc
        except (PDFSyntaxError, OSError, ValueError) as exc:
            raise SourceResumeUnreadableError("PDF cannot be parsed") from exc
        except Exception as exc:
            raise SourceResumeUnreadableError("PDF extraction failed") from exc
        if not blocks:
            raise SourceResumeUnsupportedError(
                "PDF contains no deterministically readable text"
            )
        return tuple(blocks)

    def _parse_docx(self, content: bytes) -> tuple[_ParsedSourceBlock, ...]:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                info = archive.getinfo("word/document.xml")
                if (
                    info.file_size <= 0
                    or info.file_size > MAX_DOCX_DOCUMENT_XML_BYTES
                ):
                    raise SourceResumeUnreadableError(
                        "DOCX document XML size is invalid"
                    )
                document_xml = archive.read(info)
        except SourceResumeUnreadableError:
            raise
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise SourceResumeUnreadableError("DOCX cannot be opened") from exc
        try:
            root = ElementTree.fromstring(document_xml)
        except ElementTree.ParseError as exc:
            raise SourceResumeUnreadableError(
                "DOCX document XML is invalid"
            ) from exc
        body = root.find(f"{_W}body")
        if body is None:
            raise SourceResumeUnreadableError("DOCX body is missing")
        blocks: list[_ParsedSourceBlock] = []
        paragraph_index = 0
        table_index = 0
        for child in body:
            if child.tag == f"{_W}p":
                text = _paragraph_text(child)
                if text:
                    kind, heading = _docx_paragraph_kind(
                        child,
                        text,
                        allow_heading=True,
                        table_cell=False,
                    )
                    blocks.append(
                        _ParsedSourceBlock(
                            kind=kind,
                            text=text,
                            locator=SourceResumeLocator(
                                kind=(
                                    SourceResumeLocatorKind.DOCX_PARAGRAPH
                                ),
                                paragraph_index=paragraph_index,
                            ),
                            is_heading=heading,
                        )
                    )
                paragraph_index += 1
            elif child.tag == f"{_W}tbl":
                for row_index, row in enumerate(child.findall(f"{_W}tr")):
                    for cell_index, cell in enumerate(
                        row.findall(f"{_W}tc")
                    ):
                        for cell_paragraph_index, paragraph in enumerate(
                            cell.findall(f"{_W}p")
                        ):
                            text = _paragraph_text(paragraph)
                            if not text:
                                continue
                            kind, _ = _docx_paragraph_kind(
                                paragraph,
                                text,
                                allow_heading=False,
                                table_cell=True,
                            )
                            blocks.append(
                                _ParsedSourceBlock(
                                    kind=kind,
                                    text=text,
                                    locator=SourceResumeLocator(
                                        kind=(
                                            SourceResumeLocatorKind
                                            .DOCX_TABLE_CELL_PARAGRAPH
                                        ),
                                        table_index=table_index,
                                        row_index=row_index,
                                        cell_index=cell_index,
                                        cell_paragraph_index=(
                                            cell_paragraph_index
                                        ),
                                    ),
                                    is_heading=False,
                                )
                            )
                table_index += 1
        if not blocks:
            raise SourceResumeUnreadableError(
                "DOCX contains no readable paragraphs or table cells"
            )
        return tuple(blocks)

    def parse(
        self,
        *,
        artifact_type: ResumeArtifactType,
        content: bytes,
    ) -> tuple[_ParsedSourceBlock, ...]:
        artifact_type = ResumeArtifactType(artifact_type)
        if artifact_type is ResumeArtifactType.PDF:
            return self._parse_pdf(content)
        return self._parse_docx(content)


def _build_sections(
    *,
    artifact_sha256: str,
    parser_version: str,
    parsed: tuple[_ParsedSourceBlock, ...],
) -> tuple[SourceResumeSection, ...]:
    grouped: list[tuple[str | None, Mapping[str, Any], list[_ParsedSourceBlock]]] = []
    for item in parsed:
        if item.is_heading:
            grouped.append((item.text, item.locator.to_dict(), [item]))
        elif not grouped:
            grouped.append((None, {"root": True}, [item]))
        else:
            grouped[-1][2].append(item)
    sections: list[SourceResumeSection] = []
    for section_order, (title, anchor, parsed_blocks) in enumerate(grouped):
        section_id = _stable_source_id(
            "resume-section",
            artifact_sha256=artifact_sha256,
            parser_version=parser_version,
            locator=anchor,
            kind="section",
        )
        blocks: list[SourceResumeBlock] = []
        for block_order, item in enumerate(parsed_blocks):
            locator = item.locator.to_dict()
            block_id = _stable_source_id(
                "resume-block",
                artifact_sha256=artifact_sha256,
                parser_version=parser_version,
                locator=locator,
                kind=item.kind.value,
            )
            bullet_id = (
                _stable_source_id(
                    "resume-bullet",
                    artifact_sha256=artifact_sha256,
                    parser_version=parser_version,
                    locator=locator,
                    kind="bullet",
                )
                if item.kind is SourceResumeBlockKind.BULLET
                else None
            )
            blocks.append(
                SourceResumeBlock(
                    block_id=block_id,
                    section_id=section_id,
                    order=block_order,
                    kind=item.kind,
                    text=item.text,
                    locator=item.locator,
                    bullet_id=bullet_id,
                )
            )
        sections.append(
            SourceResumeSection(
                section_id=section_id,
                order=section_order,
                title=title,
                blocks=tuple(blocks),
            )
        )
    return tuple(sections)


@dataclass(frozen=True, slots=True)
class CreateSourceResumeProjectionCommand:
    subject_id: str
    resume_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class CreateSourceResumeProjectionResult:
    status: SourceResumeProjectionStatus
    subject_id: str
    resume_id: str
    projection: SourceResumeProjection | None
    write_result: SourceResumeProjectionWriteResult | None
    reason_code: SourceResumeProjectionFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = SourceResumeProjectionStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                SourceResumeProjectionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            SourceResumeProjectionStatus.CREATED,
            SourceResumeProjectionStatus.UNCHANGED,
        }:
            expected = SourceResumeProjectionWriteStatus(status.value)
            if (
                not isinstance(self.projection, SourceResumeProjection)
                or not isinstance(
                    self.write_result, SourceResumeProjectionWriteResult
                )
                or self.write_result.status is not expected
                or self.write_result.projection != self.projection
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful projection result is invalid")
        elif status in {
            SourceResumeProjectionStatus.UNSUPPORTED,
            SourceResumeProjectionStatus.UNREADABLE,
        }:
            expected_reason = (
                SourceResumeProjectionFailureReason.FORMAT_UNSUPPORTED
                if status is SourceResumeProjectionStatus.UNSUPPORTED
                else SourceResumeProjectionFailureReason.ARTIFACT_UNREADABLE
            )
            if (
                self.projection is not None
                or self.write_result is not None
                or self.reason_code is not expected_reason
                or self.retryable
            ):
                raise ValueError("deferred projection result is invalid")
        elif self.projection is not None or self.reason_code is None:
            raise ValueError("failed projection result is invalid")


def _failure(
    command: CreateSourceResumeProjectionCommand,
    reason: SourceResumeProjectionFailureReason,
    *,
    status: SourceResumeProjectionStatus = SourceResumeProjectionStatus.FAILED,
    retryable: bool = False,
    write_result: SourceResumeProjectionWriteResult | None = None,
) -> CreateSourceResumeProjectionResult:
    return CreateSourceResumeProjectionResult(
        status=status,
        subject_id=(
            command.subject_id
            if isinstance(command.subject_id, str)
            else ""
        ),
        resume_id=(
            command.resume_id if isinstance(command.resume_id, str) else ""
        ),
        projection=None,
        write_result=write_result,
        reason_code=reason,
        retryable=retryable,
        message=f"Source resume projection failed: {reason.value}.",
    )


def create_source_resume_projection(
    command: CreateSourceResumeProjectionCommand,
    *,
    candidate_repository: ResumeCandidateRepository,
    artifact_reader: SourceResumeArtifactReader,
    parser: SourceResumeParser,
    projection_repository: SourceResumeProjectionRepository,
) -> CreateSourceResumeProjectionResult:
    """Create or reuse one immutable, faithful resume source projection."""

    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        resume_id = _clean_text("resume_id", command.resume_id, maximum=160)
        now = _require_aware("now", command.now)
        parser_version = _clean_text(
            "parser_version", parser.parser_version, maximum=80
        )
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, SourceResumeProjectionFailureReason.INVALID_REQUEST
        )
    try:
        candidate_result = candidate_repository.get(
            subject_id=subject_id,
            resume_id=resume_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            SourceResumeProjectionFailureReason.RESUME_INTEGRITY_FAILURE,
        )
    if candidate_result.status is ResumeCandidateReadStatus.NOT_FOUND:
        return _failure(
            command, SourceResumeProjectionFailureReason.RESUME_NOT_FOUND
        )
    if (
        candidate_result.status is not ResumeCandidateReadStatus.FOUND
        or candidate_result.candidate is None
        or candidate_result.candidate.subject_id != subject_id
        or candidate_result.candidate.resume_id != resume_id
    ):
        return _failure(
            command,
            SourceResumeProjectionFailureReason.RESUME_INTEGRITY_FAILURE,
        )
    candidate = candidate_result.candidate
    try:
        content = artifact_reader.read(candidate)
    except SourceResumeArtifactIntegrityError:
        return _failure(
            command,
            SourceResumeProjectionFailureReason.ARTIFACT_HASH_MISMATCH,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            SourceResumeProjectionFailureReason.ARTIFACT_UNREADABLE,
            status=SourceResumeProjectionStatus.UNREADABLE,
        )
    if hashlib.sha256(content).hexdigest() != candidate.artifact_sha256:
        return _failure(
            command,
            SourceResumeProjectionFailureReason.ARTIFACT_HASH_MISMATCH,
        )
    projection_id = source_resume_projection_id(
        subject_id=subject_id,
        resume_id=resume_id,
        artifact_sha256=candidate.artifact_sha256,
        artifact_type=candidate.artifact_type,
        parser_version=parser_version,
    )
    try:
        existing = projection_repository.get(
            subject_id=subject_id,
            projection_id=projection_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE,
        )
    if existing.status is SourceResumeProjectionReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE,
        )
    if (
        existing.status is SourceResumeProjectionReadStatus.FOUND
        and existing.projection is not None
    ):
        write_result = SourceResumeProjectionWriteResult(
            status=SourceResumeProjectionWriteStatus.UNCHANGED,
            projection=existing.projection,
            reason_code=None,
            retryable=False,
        )
        return CreateSourceResumeProjectionResult(
            status=SourceResumeProjectionStatus.UNCHANGED,
            subject_id=subject_id,
            resume_id=resume_id,
            projection=existing.projection,
            write_result=write_result,
            reason_code=None,
            retryable=False,
            message="The existing source resume projection is unchanged.",
        )
    try:
        parsed = parser.parse(
            artifact_type=candidate.artifact_type,
            content=content,
        )
        if (
            not isinstance(parsed, tuple)
            or not parsed
            or any(not isinstance(item, _ParsedSourceBlock) for item in parsed)
        ):
            raise SourceResumeUnreadableError(
                "parser returned an invalid source projection"
            )
        sections = _build_sections(
            artifact_sha256=candidate.artifact_sha256,
            parser_version=parser_version,
            parsed=parsed,
        )
    except SourceResumeUnsupportedError:
        return _failure(
            command,
            SourceResumeProjectionFailureReason.FORMAT_UNSUPPORTED,
            status=SourceResumeProjectionStatus.UNSUPPORTED,
        )
    except SourceResumeUnreadableError:
        return _failure(
            command,
            SourceResumeProjectionFailureReason.ARTIFACT_UNREADABLE,
            status=SourceResumeProjectionStatus.UNREADABLE,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            SourceResumeProjectionFailureReason.ARTIFACT_UNREADABLE,
            status=SourceResumeProjectionStatus.UNREADABLE,
        )
    content_values = {
        **_projection_identity_payload(
            subject_id=subject_id,
            resume_id=resume_id,
            artifact_sha256=candidate.artifact_sha256,
            artifact_type=candidate.artifact_type,
            parser_version=parser_version,
        ),
        "projection_id": projection_id,
        "sections": [item.to_dict() for item in sections],
    }
    projection = SourceResumeProjection(
        projection_id=projection_id,
        contract_version=SOURCE_RESUME_PROJECTION_CONTRACT_VERSION,
        parser_version=parser_version,
        subject_id=subject_id,
        resume_id=resume_id,
        artifact_sha256=candidate.artifact_sha256,
        artifact_type=candidate.artifact_type,
        sections=sections,
        projection_content_hash=_canonical_hash(content_values),
        projected_at=now,
    )
    try:
        write_result = projection_repository.save(projection)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            SourceResumeProjectionFailureReason.PROJECTION_PERSISTENCE_FAILED,
            retryable=True,
        )
    if write_result.status is SourceResumeProjectionWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or SourceResumeProjectionFailureReason.PROJECTION_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            write_result=write_result,
        )
    result_status = SourceResumeProjectionStatus(write_result.status.value)
    return CreateSourceResumeProjectionResult(
        status=result_status,
        subject_id=subject_id,
        resume_id=resume_id,
        projection=write_result.projection,
        write_result=write_result,
        reason_code=None,
        retryable=False,
        message=(
            "The source resume projection was created."
            if result_status is SourceResumeProjectionStatus.CREATED
            else "The existing source resume projection is unchanged."
        ),
    )


_SOURCE_PROJECTION_FAILURE_REASON_MAP = {
    SourceResumeProjectionFailureReason.INVALID_REQUEST: (
        SourceResumeProjectionStopReason.INVALID_REQUEST
    ),
    SourceResumeProjectionFailureReason.RESUME_NOT_FOUND: (
        SourceResumeProjectionStopReason.RESUME_NOT_FOUND
    ),
    SourceResumeProjectionFailureReason.RESUME_INTEGRITY_FAILURE: (
        SourceResumeProjectionStopReason.RESUME_INTEGRITY_FAILURE
    ),
    SourceResumeProjectionFailureReason.ARTIFACT_HASH_MISMATCH: (
        SourceResumeProjectionStopReason.ARTIFACT_HASH_MISMATCH
    ),
    SourceResumeProjectionFailureReason.FORMAT_UNSUPPORTED: (
        SourceResumeProjectionStopReason.FORMAT_UNSUPPORTED
    ),
    SourceResumeProjectionFailureReason.ARTIFACT_UNREADABLE: (
        SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE
    ),
    SourceResumeProjectionFailureReason.PROJECTION_PERSISTENCE_FAILED: (
        SourceResumeProjectionStopReason.PROJECTION_PERSISTENCE_FAILED
    ),
    SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE: (
        SourceResumeProjectionStopReason.PROJECTION_INTEGRITY_FAILURE
    ),
}


def source_resume_projection_public_result(
    result: CreateSourceResumeProjectionResult,
) -> PublicPreparationStageResult:
    """Adapt every authoritative P2a4a outcome to stage-result v2."""

    if not isinstance(result, CreateSourceResumeProjectionResult):
        raise TypeError("result must be a source-resume projection result")
    stage = ApplicationPreparationStage.SOURCE_RESUME_PROJECTION
    if result.status in {
        SourceResumeProjectionStatus.CREATED,
        SourceResumeProjectionStatus.UNCHANGED,
    }:
        if result.projection is None:
            raise ValueError("successful projection has no record")
        constructor = (
            PublicPreparationStageResult.completed
            if result.status is SourceResumeProjectionStatus.CREATED
            else PublicPreparationStageResult.unchanged
        )
        return constructor(
            stage=stage,
            result_id=result.projection.projection_id,
            result_content_hash=result.projection.projection_content_hash,
            outputs={
                "source_resume_projection_id": (
                    result.projection.projection_id
                )
            },
        )
    if result.reason_code is None:
        raise ValueError("stopped projection has no authoritative reason")
    try:
        reason = _SOURCE_PROJECTION_FAILURE_REASON_MAP[result.reason_code]
    except KeyError as error:
        raise ValueError("unmapped source-projection stop reason") from error
    outcome = (
        PreparationStageOutcome.DEFERRED
        if result.status
        in {
            SourceResumeProjectionStatus.UNSUPPORTED,
            SourceResumeProjectionStatus.UNREADABLE,
        }
        else PreparationStageOutcome.FAILED
    )
    stop_reason = PreparationStopReasonEnvelope(
        stage=stage,
        code=reason,
        contract_version=SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION,
        outcome=outcome,
    )
    constructor = (
        PublicPreparationStageResult.deferred
        if outcome is PreparationStageOutcome.DEFERRED
        else PublicPreparationStageResult.failed
    )
    return constructor(
        stage=stage,
        stop_reason=stop_reason,
        retryable=result.retryable,
    )


__all__ = [
    "CreateSourceResumeProjectionCommand",
    "CreateSourceResumeProjectionResult",
    "DeterministicSourceResumeParser",
    "PrivateHomeSourceResumeArtifactReader",
    "PrivateHomeSourceResumeProjectionRepository",
    "SOURCE_RESUME_PARSER_VERSION",
    "SOURCE_RESUME_PROJECTION_CONTRACT_VERSION",
    "SourceResumeArtifactIntegrityError",
    "SourceResumeArtifactReader",
    "SourceResumeBlock",
    "SourceResumeBlockKind",
    "SourceResumeLocator",
    "SourceResumeLocatorKind",
    "SourceResumeParser",
    "SourceResumeProjection",
    "SourceResumeProjectionFailureReason",
    "SourceResumeProjectionReadResult",
    "SourceResumeProjectionReadStatus",
    "SourceResumeProjectionRepository",
    "SourceResumeProjectionStatus",
    "SourceResumeProjectionWriteResult",
    "SourceResumeProjectionWriteStatus",
    "source_resume_projection_public_result",
    "SourceResumeSection",
    "create_source_resume_projection",
    "source_resume_projection_id",
]
