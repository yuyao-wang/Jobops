"""Deterministic, source-bound projections of candidate information sources."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import sqlite3
import ssl
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import pdfplumber
from PIL import Image, UnidentifiedImageError
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError

from .candidate_information_sources import (
    CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION,
    CANDIDATE_SOURCE_VERSION,
    CandidateFileDetectedFormat,
    CandidateFileSourceDescriptor,
    CandidateInformationSource,
    CandidateInformationSourceKind,
    CandidateInformationSourcePayloadReadStatus,
    CandidateInformationSourceReadStatus,
    CandidateInformationSourceRepository,
    CandidateURLSourceDescriptor,
    GetCandidateInformationSourceCommand,
    canonicalize_candidate_source_url,
    get_candidate_information_source,
    read_candidate_information_source_payload,
)
from .pdf_page_renderer import (
    PDF_RENDERER_CONTRACT_VERSION,
    PdfPageRendererPort,
    PdfRendererUnavailableError,
)
from .private_home import PRIVATE_FILE_MODE, PrivateHome


CANDIDATE_SOURCE_PROJECTION_CONTRACT_VERSION = "candidate-source-projection-v1"
CANDIDATE_SOURCE_PROJECTION_REPOSITORY_SCHEMA_VERSION = 1
CANDIDATE_SOURCE_PARSER_POLICY_VERSION = "candidate-source-parser-v1"
CANDIDATE_SOURCE_ASSET_POLICY_VERSION = "candidate-source-asset-v1"
CANDIDATE_SOURCE_LOCATOR_CONTRACT_VERSION = "candidate-source-locator-v1"
CANDIDATE_URL_CAPTURE_CONTRACT_VERSION = "candidate-url-capture-v1"
CANDIDATE_URL_FETCH_POLICY_VERSION = "candidate-url-fetch-v1"
CANDIDATE_SOURCE_PROJECTION_LIMITS_VERSION = "candidate-source-projection-limits-v1"

MAX_PROJECTION_TEXT_BYTES = 512 * 1024
MAX_PROJECTION_BLOCKS = 4_000
MAX_PROJECTION_ASSETS = 40
MAX_PROJECTION_ASSET_BYTES = 8 * 1024 * 1024
MAX_URL_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_URL_REDIRECTS = 4
MAX_URL_TIMEOUT_SECONDS = 15
MAX_URL_HTML_BLOCKS = 2_000
TEXT_CHUNK_BYTES = 16 * 1024

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,239}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ROLE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
_LIMITATION_RE = re.compile(
    r"(?:TEXT_TRUNCATED|BLOCK_LIMIT_REACHED|ASSET_LIMIT_REACHED|"
    r"EMBEDDED_IMAGE_SKIPPED|PDF_RENDER_UNAVAILABLE|"
    r"PPTX_READING_ORDER_APPROXIMATE|IMAGE_ONLY_PAGE:[1-9][0-9]*)"
)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


class CandidateSourceProjectionKind(StrEnum):
    DOCUMENT = "DOCUMENT"
    IMAGE = "IMAGE"
    WEB_SNAPSHOT = "WEB_SNAPSHOT"
    USER_STATEMENT = "USER_STATEMENT"


class CandidateSourceProjectionCompleteness(StrEnum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_LIMITS = "COMPLETED_WITH_LIMITS"


class ProjectCandidateSourceStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_READABLE = "NOT_READABLE"
    FETCH_FAILED = "FETCH_FAILED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class CandidateSourceProjectionReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CandidateSourceProjectionListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CandidateProjectionBlockType(StrEnum):
    TITLE = "TITLE"
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    TABLE_CELL = "TABLE_CELL"
    METADATA = "METADATA"
    LINK = "LINK"
    SLIDE_TEXT = "SLIDE_TEXT"
    SPEAKER_NOTE = "SPEAKER_NOTE"
    USER_STATEMENT = "USER_STATEMENT"


class CandidateProjectionAssetKind(StrEnum):
    SOURCE_IMAGE = "SOURCE_IMAGE"
    RENDERED_PAGE = "RENDERED_PAGE"
    RENDERED_SLIDE = "RENDERED_SLIDE"
    EMBEDDED_IMAGE = "EMBEDDED_IMAGE"


class CandidateSourceLocatorContainerKind(StrEnum):
    PDF = "PDF"
    DOCX = "DOCX"
    PPTX = "PPTX"
    IMAGE = "IMAGE"
    TEXT = "TEXT"
    HTML = "HTML"
    URL_CAPTURE = "URL_CAPTURE"


class CandidateURLFetchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"
    TOO_LARGE = "TOO_LARGE"
    UNSUPPORTED = "UNSUPPORTED"


class _ProjectionIntegrityError(RuntimeError):
    pass


class _ProjectionUnsupported(ValueError):
    pass


class _ProjectionUnreadable(ValueError):
    pass


class _ProjectionLimited(RuntimeError):
    pass


def _clean_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _clean_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(parsed)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _hash_mapping(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("projection text must be a string")
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    if "\x00" in normalized or any(
        ord(char) < 32 and char not in "\n\t" for char in normalized
    ):
        raise ValueError("projection text contains unsupported controls")
    return normalized


@dataclass(frozen=True, slots=True)
class CandidateSourceLocator:
    source_id: str
    source_version: str
    container_kind: CandidateSourceLocatorContainerKind
    page_number: int | None = None
    slide_number: int | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row_index: int | None = None
    column_index: int | None = None
    block_index: int | None = None
    character_start: int | None = None
    character_end: int | None = None
    element_path: str | None = None
    locator_contract_version: str = CANDIDATE_SOURCE_LOCATOR_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _clean_id("source_id", self.source_id)
        if self.source_version != CANDIDATE_SOURCE_VERSION:
            raise ValueError("source version is unsupported")
        object.__setattr__(
            self, "container_kind", CandidateSourceLocatorContainerKind(self.container_kind)
        )
        if self.locator_contract_version != CANDIDATE_SOURCE_LOCATOR_CONTRACT_VERSION:
            raise ValueError("locator contract version is unsupported")
        for name in (
            "page_number", "slide_number", "paragraph_index", "table_index",
            "row_index", "column_index", "block_index", "character_start",
            "character_end",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} is invalid")
        if self.page_number == 0 or self.slide_number == 0:
            raise ValueError("page and slide numbers are one-based")
        if self.element_path is not None and (
            not self.element_path
            or len(self.element_path) > 300
            or self.element_path.startswith("/")
            or "\\" in self.element_path
        ):
            raise ValueError("element path is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_index": self.block_index,
            "character_end": self.character_end,
            "character_start": self.character_start,
            "column_index": self.column_index,
            "container_kind": self.container_kind.value,
            "element_path": self.element_path,
            "locator_contract_version": self.locator_contract_version,
            "page_number": self.page_number,
            "paragraph_index": self.paragraph_index,
            "row_index": self.row_index,
            "slide_number": self.slide_number,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "table_index": self.table_index,
        }


@dataclass(frozen=True, slots=True)
class CandidateProjectionBlock:
    block_id: str
    block_type: CandidateProjectionBlockType
    ordinal: int
    text: str = field(repr=False)
    source_locator: CandidateSourceLocator
    structural_role: str
    parent_block_id: str | None
    block_hash: str
    parser_policy_version: str = CANDIDATE_SOURCE_PARSER_POLICY_VERSION

    def __post_init__(self) -> None:
        _clean_id("block_id", self.block_id)
        object.__setattr__(self, "block_type", CandidateProjectionBlockType(self.block_type))
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("block ordinal is invalid")
        object.__setattr__(self, "text", _text(self.text))
        if not self.text.strip():
            raise ValueError("block text is empty")
        if (
            not isinstance(self.structural_role, str)
            or _ROLE_RE.fullmatch(self.structural_role) is None
        ):
            raise ValueError("structural role is invalid")
        if self.parent_block_id is not None:
            _clean_id("parent_block_id", self.parent_block_id)
        if self.parser_policy_version != CANDIDATE_SOURCE_PARSER_POLICY_VERSION:
            raise ValueError("parser policy version is unsupported")
        expected = _hash_mapping(self.binding_dict())
        if self.block_hash != expected or self.block_id != f"candidate-block-{expected[:32]}":
            raise ValueError("block identity is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "block_type": self.block_type.value,
            "locator": self.source_locator.to_dict(),
            "ordinal": self.ordinal,
            "parent_block_id": self.parent_block_id,
            "parser_policy_version": self.parser_policy_version,
            "structural_role": self.structural_role,
            "text_sha256": _sha256(self.text.encode("utf-8")),
        }

    def metadata_dict(self) -> dict[str, Any]:
        return {
            "block_hash": self.block_hash,
            "block_id": self.block_id,
            "block_type": self.block_type.value,
            "ordinal": self.ordinal,
            "source_locator": self.source_locator.to_dict(),
            "structural_role": self.structural_role,
        }


@dataclass(frozen=True, slots=True)
class CandidateProjectionAsset:
    asset_id: str
    asset_kind: CandidateProjectionAssetKind
    ordinal: int
    media_type: str
    byte_size: int
    content_hash: str
    managed_asset_ref: str
    source_locator: CandidateSourceLocator
    width: int
    height: int
    asset_policy_version: str = CANDIDATE_SOURCE_ASSET_POLICY_VERSION

    def __post_init__(self) -> None:
        _clean_id("asset_id", self.asset_id)
        object.__setattr__(self, "asset_kind", CandidateProjectionAssetKind(self.asset_kind))
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ValueError("asset ordinal is invalid")
        if self.media_type not in {"image/png", "image/jpeg"}:
            raise ValueError("asset media type is unsupported")
        if type(self.byte_size) is not int or not 0 < self.byte_size <= MAX_PROJECTION_ASSET_BYTES:
            raise ValueError("asset byte size is invalid")
        _clean_hash("content_hash", self.content_hash)
        _clean_id("managed_asset_ref", self.managed_asset_ref)
        if any(type(v) is not int or v <= 0 or v > 20_000 for v in (self.width, self.height)):
            raise ValueError("asset dimensions are invalid")
        if self.asset_policy_version != CANDIDATE_SOURCE_ASSET_POLICY_VERSION:
            raise ValueError("asset policy version is unsupported")
        expected = _hash_mapping(self.binding_dict())
        if self.asset_id != f"candidate-asset-{expected[:32]}":
            raise ValueError("asset identity is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "asset_kind": self.asset_kind.value,
            "asset_policy_version": self.asset_policy_version,
            "byte_size": self.byte_size,
            "content_hash": self.content_hash,
            "height": self.height,
            "media_type": self.media_type,
            "ordinal": self.ordinal,
            "source_locator": self.source_locator.to_dict(),
            "width": self.width,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.binding_dict(), "asset_id": self.asset_id, "managed_asset_ref": self.managed_asset_ref}


@dataclass(frozen=True, slots=True)
class CandidateURLCapture:
    capture_id: str
    subject_id: str
    source_id: str
    canonical_url: str = field(repr=False)
    final_url: str = field(repr=False)
    response_status: int
    detected_content_type: str
    content_byte_size: int
    content_hash: str
    managed_payload_ref: str
    redirect_chain: tuple[str, ...] = field(repr=False)
    fetched_at: datetime = field(repr=False)
    capture_hash: str = ""
    capture_contract_version: str = CANDIDATE_URL_CAPTURE_CONTRACT_VERSION
    fetch_policy_version: str = CANDIDATE_URL_FETCH_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in ("capture_id", "subject_id", "source_id", "managed_payload_ref"):
            _clean_id(name, getattr(self, name))
        if self.capture_contract_version != CANDIDATE_URL_CAPTURE_CONTRACT_VERSION:
            raise ValueError("capture contract version is unsupported")
        if self.fetch_policy_version != CANDIDATE_URL_FETCH_POLICY_VERSION:
            raise ValueError("fetch policy version is unsupported")
        canonical, _ = canonicalize_candidate_source_url(self.canonical_url)
        final, _ = canonicalize_candidate_source_url(self.final_url)
        redirects = tuple(
            canonicalize_candidate_source_url(item)[0]
            for item in self.redirect_chain
        )
        if (
            canonical != self.canonical_url
            or final != self.final_url
            or redirects != self.redirect_chain
        ):
            raise ValueError("capture URLs are not canonical")
        if type(self.response_status) is not int or not 200 <= self.response_status < 300:
            raise ValueError("capture response status is invalid")
        if self.detected_content_type not in {
            "text/html", "text/plain", "application/pdf", "image/png", "image/jpeg"
        }:
            raise ValueError("capture content type is unsupported")
        if type(self.content_byte_size) is not int or not 0 < self.content_byte_size <= MAX_URL_RESPONSE_BYTES:
            raise ValueError("capture byte size is invalid")
        _clean_hash("content_hash", self.content_hash)
        object.__setattr__(self, "fetched_at", _utc(self.fetched_at))
        expected = _hash_mapping(self.binding_dict())
        if self.capture_hash != expected or self.capture_id != f"candidate-capture-{expected[:32]}":
            raise ValueError("capture identity is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "canonical_url_hash": _sha256(self.canonical_url.encode()),
            "capture_contract_version": self.capture_contract_version,
            "content_hash": self.content_hash,
            "detected_content_type": self.detected_content_type,
            "fetch_policy_version": self.fetch_policy_version,
            "final_url_hash": _sha256(self.final_url.encode()),
            "redirect_chain_hash": _sha256("\n".join(self.redirect_chain).encode()),
            "response_status": self.response_status,
            "source_id": self.source_id,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_url": self.canonical_url,
            "capture_hash": self.capture_hash,
            "capture_id": self.capture_id,
            "capture_contract_version": self.capture_contract_version,
            "content_byte_size": self.content_byte_size,
            "content_hash": self.content_hash,
            "detected_content_type": self.detected_content_type,
            "fetch_policy_version": self.fetch_policy_version,
            "fetched_at": _time_text(self.fetched_at),
            "final_url": self.final_url,
            "managed_payload_ref": self.managed_payload_ref,
            "redirect_chain": list(self.redirect_chain),
            "response_status": self.response_status,
            "source_id": self.source_id,
            "subject_id": self.subject_id,
        }


@dataclass(frozen=True, slots=True)
class CandidateSourceProjection:
    projection_id: str
    subject_id: str
    source_id: str
    source_kind: CandidateInformationSourceKind
    source_version: str
    source_payload_hash: str
    source_identity_hash: str
    projection_kind: CandidateSourceProjectionKind
    completeness: CandidateSourceProjectionCompleteness
    block_ids: tuple[str, ...]
    asset_ids: tuple[str, ...]
    capture_id: str | None
    capture_hash: str | None
    limitation_codes: tuple[str, ...]
    parser_policy_version: str
    limits_policy_version: str
    renderer_policy_version: str | None
    projection_hash: str
    created_at: datetime
    invocation_id: str
    projection_contract_version: str = CANDIDATE_SOURCE_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("projection_id", "subject_id", "source_id", "invocation_id"):
            _clean_id(name, getattr(self, name))
        object.__setattr__(self, "source_kind", CandidateInformationSourceKind(self.source_kind))
        object.__setattr__(self, "projection_kind", CandidateSourceProjectionKind(self.projection_kind))
        object.__setattr__(self, "completeness", CandidateSourceProjectionCompleteness(self.completeness))
        if self.source_version != CANDIDATE_SOURCE_VERSION:
            raise ValueError("source version is unsupported")
        for name in ("source_payload_hash", "source_identity_hash", "projection_hash"):
            _clean_hash(name, getattr(self, name))
        if self.projection_contract_version != CANDIDATE_SOURCE_PROJECTION_CONTRACT_VERSION:
            raise ValueError("projection contract version is unsupported")
        if self.parser_policy_version != CANDIDATE_SOURCE_PARSER_POLICY_VERSION:
            raise ValueError("parser policy version is unsupported")
        if self.limits_policy_version != CANDIDATE_SOURCE_PROJECTION_LIMITS_VERSION:
            raise ValueError("projection limits policy version is unsupported")
        if (self.capture_id is None) != (self.capture_hash is None):
            raise ValueError("capture binding is incomplete")
        if self.capture_id is not None:
            _clean_id("capture_id", self.capture_id)
            _clean_hash("capture_hash", self.capture_hash)
        if len(set(self.block_ids)) != len(self.block_ids) or len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("projection children are duplicated")
        if (
            tuple(sorted(set(self.limitation_codes))) != self.limitation_codes
            or any(_LIMITATION_RE.fullmatch(code) is None for code in self.limitation_codes)
        ):
            raise ValueError("projection limitation codes are invalid")
        for value in (*self.block_ids, *self.asset_ids):
            _clean_id("projection child ID", value)
        object.__setattr__(self, "created_at", _utc(self.created_at))
        expected = _hash_mapping(self.binding_dict())
        if self.projection_hash != expected or self.projection_id != f"candidate-projection-{expected[:32]}":
            raise ValueError("projection identity is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "asset_ids": list(self.asset_ids),
            "block_ids": list(self.block_ids),
            "capture_hash": self.capture_hash,
            "capture_id": self.capture_id,
            "completeness": self.completeness.value,
            "limitation_codes": list(self.limitation_codes),
            "limits_policy_version": self.limits_policy_version,
            "parser_policy_version": self.parser_policy_version,
            "projection_contract_version": self.projection_contract_version,
            "projection_kind": self.projection_kind.value,
            "renderer_policy_version": self.renderer_policy_version,
            "source_id": self.source_id,
            "source_identity_hash": self.source_identity_hash,
            "source_kind": self.source_kind.value,
            "source_payload_hash": self.source_payload_hash,
            "source_version": self.source_version,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.binding_dict(),
            "created_at": _time_text(self.created_at),
            "invocation_id": self.invocation_id,
            "projection_hash": self.projection_hash,
            "projection_id": self.projection_id,
        }


@dataclass(frozen=True, slots=True)
class CandidateProjectionAssetPayload:
    projection_id: str
    asset: CandidateProjectionAsset
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class CandidateURLCapturePayload:
    projection_id: str
    capture: CandidateURLCapture
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProjectCandidateInformationSourceCommand:
    subject_id: str
    source_id: str
    source_version: str
    source_identity_hash: str
    invocation_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ProjectCandidateInformationSourceResult:
    status: ProjectCandidateSourceStatus
    projection: CandidateSourceProjection | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateSourceProjectionReadResult:
    status: CandidateSourceProjectionReadStatus
    projection: CandidateSourceProjection | None = None
    block: CandidateProjectionBlock | None = None
    asset_payload: CandidateProjectionAssetPayload | None = None
    capture_payload: CandidateURLCapturePayload | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateSourceProjectionListResult:
    status: CandidateSourceProjectionListStatus
    projections: tuple[CandidateSourceProjection, ...]
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateURLFetchRequest:
    canonical_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CandidateURLFetchResponse:
    status: CandidateURLFetchStatus
    final_url: str | None = field(default=None, repr=False)
    response_status: int | None = None
    content_type: str | None = None
    content: bytes | None = field(default=None, repr=False)
    redirect_chain: tuple[str, ...] = field(default=(), repr=False)
    failure_code: str | None = None


@runtime_checkable
class CandidateURLFetchPort(Protocol):
    def fetch(self, request: CandidateURLFetchRequest) -> CandidateURLFetchResponse:
        """Fetch once with pinned-address SSRF validation and bounded bytes."""


def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise _ProjectionUnreadable("URL DNS resolution failed") from exc
    addresses = sorted({row[4][0] for row in rows})
    if not addresses:
        raise _ProjectionUnreadable("URL DNS returned no address")
    for raw in addresses:
        address = ipaddress.ip_address(raw)
        if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_unspecified or address.is_multicast
        ):
            raise _ProjectionUnsupported("URL address is forbidden")
    return tuple(addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str, port: int, timeout: float) -> None:
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class PinnedHTTPSCandidateURLFetcher:
    """One-resource HTTPS fetcher; every redirect is re-resolved and pinned."""

    _USER_AGENT = "Jobops-CandidateSourceProjection/1"

    def fetch(self, request: CandidateURLFetchRequest) -> CandidateURLFetchResponse:
        current = request.canonical_url
        chain: list[str] = []
        try:
            for _ in range(MAX_URL_REDIRECTS + 1):
                current, _ = canonicalize_candidate_source_url(current)
                parsed = urlsplit(current)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
                    raise _ProjectionUnsupported("URL transport is unsupported")
                port = parsed.port or 443
                address = _public_addresses(parsed.hostname, port)[0]
                connection = _PinnedHTTPSConnection(
                    parsed.hostname, address, port, MAX_URL_TIMEOUT_SECONDS
                )
                try:
                    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                    connection.request(
                        "GET",
                        target,
                        headers={
                            "Accept": "text/html,text/plain,application/pdf,image/png,image/jpeg",
                            "Accept-Encoding": "identity",
                            "User-Agent": self._USER_AGENT,
                        },
                    )
                    response = connection.getresponse()
                    if response.getheader("Content-Encoding", "identity").casefold() not in {"", "identity"}:
                        raise _ProjectionUnsupported("encoded response is unsupported")
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        if not location:
                            raise _ProjectionUnreadable("redirect has no location")
                        if len(chain) >= MAX_URL_REDIRECTS:
                            raise _ProjectionUnsupported("redirect limit exceeded")
                        chain.append(current)
                        current = urljoin(current, location)
                        continue
                    if not 200 <= response.status < 300:
                        return CandidateURLFetchResponse(
                            status=CandidateURLFetchStatus.FAILED,
                            response_status=response.status,
                            redirect_chain=tuple(chain),
                            failure_code="URL_HTTP_STATUS",
                        )
                    content_length = response.getheader("Content-Length")
                    if content_length and int(content_length) > MAX_URL_RESPONSE_BYTES:
                        raise _ProjectionLimited("URL response is too large")
                    content = response.read(MAX_URL_RESPONSE_BYTES + 1)
                    if len(content) > MAX_URL_RESPONSE_BYTES:
                        raise _ProjectionLimited("URL response is too large")
                    content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().casefold()
                    return CandidateURLFetchResponse(
                        status=CandidateURLFetchStatus.SUCCEEDED,
                        final_url=current,
                        response_status=response.status,
                        content_type=content_type,
                        content=content,
                        redirect_chain=tuple(chain),
                    )
                finally:
                    connection.close()
        except socket.timeout:
            return CandidateURLFetchResponse(status=CandidateURLFetchStatus.TIMEOUT, failure_code="URL_TIMEOUT")
        except _ProjectionLimited:
            return CandidateURLFetchResponse(status=CandidateURLFetchStatus.TOO_LARGE, failure_code="URL_TOO_LARGE")
        except _ProjectionUnsupported:
            return CandidateURLFetchResponse(status=CandidateURLFetchStatus.BLOCKED, failure_code="URL_BLOCKED")
        except (OSError, ValueError, http.client.HTTPException, _ProjectionUnreadable):
            return CandidateURLFetchResponse(status=CandidateURLFetchStatus.FAILED, failure_code="URL_FETCH_FAILED")
        return CandidateURLFetchResponse(status=CandidateURLFetchStatus.FAILED, failure_code="URL_FETCH_FAILED")


def _make_block(
    *,
    block_type: CandidateProjectionBlockType,
    ordinal: int,
    text: str,
    locator: CandidateSourceLocator,
    structural_role: str,
    parent_block_id: str | None = None,
) -> CandidateProjectionBlock:
    candidate = {
        "block_type": block_type.value,
        "locator": locator.to_dict(),
        "ordinal": ordinal,
        "parent_block_id": parent_block_id,
        "parser_policy_version": CANDIDATE_SOURCE_PARSER_POLICY_VERSION,
        "structural_role": structural_role,
        "text_sha256": _sha256(_text(text).encode()),
    }
    digest = _hash_mapping(candidate)
    return CandidateProjectionBlock(
        block_id=f"candidate-block-{digest[:32]}",
        block_type=block_type,
        ordinal=ordinal,
        text=text,
        source_locator=locator,
        structural_role=structural_role,
        parent_block_id=parent_block_id,
        block_hash=digest,
    )


def _locator_from_dict(value: Mapping[str, Any]) -> CandidateSourceLocator:
    payload = dict(value)
    payload["container_kind"] = CandidateSourceLocatorContainerKind(
        payload["container_kind"]
    )
    return CandidateSourceLocator(**payload)


def _image_info(content: bytes) -> tuple[str, int, int]:
    try:
        with Image.open(BytesIO(content)) as image:
            media = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(str(image.format))
            if media is None:
                raise _ProjectionUnsupported("image format is unsupported")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > 40_000_000:
                raise _ProjectionUnsupported("image dimensions are unsupported")
            image.verify()
            return media, width, height
    except _ProjectionUnsupported:
        raise
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise _ProjectionUnreadable("image cannot be read") from exc


def _make_asset(
    *, kind: CandidateProjectionAssetKind, ordinal: int, content: bytes,
    locator: CandidateSourceLocator,
) -> tuple[CandidateProjectionAsset, bytes]:
    if len(content) > MAX_PROJECTION_ASSET_BYTES:
        raise _ProjectionLimited("asset exceeded size policy")
    media, width, height = _image_info(content)
    content_hash = _sha256(content)
    binding = {
        "asset_kind": kind.value,
        "asset_policy_version": CANDIDATE_SOURCE_ASSET_POLICY_VERSION,
        "byte_size": len(content),
        "content_hash": content_hash,
        "height": height,
        "media_type": media,
        "ordinal": ordinal,
        "source_locator": locator.to_dict(),
        "width": width,
    }
    digest = _hash_mapping(binding)
    return (
        CandidateProjectionAsset(
            asset_id=f"candidate-asset-{digest[:32]}",
            asset_kind=kind,
            ordinal=ordinal,
            media_type=media,
            byte_size=len(content),
            content_hash=content_hash,
            managed_asset_ref=f"projection-asset-{content_hash}",
            source_locator=locator,
            width=width,
            height=height,
        ),
        content,
    )


def _chunk_text(
    source: CandidateInformationSource, text: str, *,
    container: CandidateSourceLocatorContainerKind,
    block_type: CandidateProjectionBlockType,
) -> tuple[list[CandidateProjectionBlock], list[str]]:
    blocks: list[CandidateProjectionBlock] = []
    limits: list[str] = []
    used = 0
    offset = 0
    paragraphs = re.split(r"\n[ \t]*\n", text)
    for index, paragraph in enumerate(paragraphs):
        normalized = paragraph.strip()
        if not normalized:
            offset += len(paragraph) + 2
            continue
        cursor = 0
        while cursor < len(normalized):
            end = min(len(normalized), cursor + TEXT_CHUNK_BYTES)
            while len(normalized[cursor:end].encode()) > TEXT_CHUNK_BYTES:
                end -= 1
            chunk = normalized[cursor:end]
            encoded = chunk.encode()
            if (
                used + len(encoded) > MAX_PROJECTION_TEXT_BYTES
                or len(blocks) >= MAX_PROJECTION_BLOCKS
            ):
                limits.append("TEXT_TRUNCATED")
                break
            locator = CandidateSourceLocator(
                source_id=source.source_id,
                source_version=source.source_version,
                container_kind=container,
                paragraph_index=index,
                block_index=len(blocks),
                character_start=offset + cursor,
                character_end=offset + end,
            )
            blocks.append(_make_block(
                block_type=block_type,
                ordinal=len(blocks),
                text=chunk,
                locator=locator,
                structural_role="paragraph",
            ))
            used += len(encoded)
            cursor = end
        if limits and limits[-1] == "TEXT_TRUNCATED":
            break
        offset += len(paragraph) + 2
    return blocks, limits


def _parse_pdf(
    source: CandidateInformationSource, content: bytes,
    renderer: PdfPageRendererPort | None,
) -> tuple[list[CandidateProjectionBlock], list[tuple[CandidateProjectionAsset, bytes]], list[str], str | None]:
    blocks: list[CandidateProjectionBlock] = []
    limits: list[str] = []
    try:
        with pdfplumber.open(BytesIO(content)) as document:
            for page_number, page in enumerate(document.pages, 1):
                extracted = page.extract_text(x_tolerance=3, y_tolerance=3, layout=False)
                if not extracted or not extracted.strip():
                    limits.append(f"IMAGE_ONLY_PAGE:{page_number}")
                    continue
                for line_index, raw in enumerate(extracted.splitlines()):
                    value = raw.strip()
                    if not value:
                        continue
                    if len(blocks) >= MAX_PROJECTION_BLOCKS:
                        limits.append("BLOCK_LIMIT_REACHED")
                        break
                    kind = CandidateProjectionBlockType.HEADING if len(value) <= 100 and value == value.upper() else CandidateProjectionBlockType.PARAGRAPH
                    blocks.append(_make_block(
                        block_type=kind,
                        ordinal=len(blocks),
                        text=value,
                        locator=CandidateSourceLocator(
                            source_id=source.source_id,
                            source_version=source.source_version,
                            container_kind=CandidateSourceLocatorContainerKind.PDF,
                            page_number=page_number,
                            block_index=line_index,
                        ),
                        structural_role="pdf-line",
                    ))
    except PDFPasswordIncorrect as exc:
        raise _ProjectionUnsupported("encrypted PDF is unsupported") from exc
    except (PDFSyntaxError, OSError, ValueError) as exc:
        raise _ProjectionUnreadable("PDF cannot be parsed") from exc
    assets: list[tuple[CandidateProjectionAsset, bytes]] = []
    renderer_version: str | None = None
    if renderer is not None:
        try:
            description = renderer.describe()
            renderer_version = (
                f"{PDF_RENDERER_CONTRACT_VERSION}:{description.renderer_name}:"
                f"{description.renderer_version}:{description.dpi}"
            )
            rendered = renderer.render(content)
            for page in rendered[:MAX_PROJECTION_ASSETS]:
                locator = CandidateSourceLocator(
                    source_id=source.source_id,
                    source_version=source.source_version,
                    container_kind=CandidateSourceLocatorContainerKind.PDF,
                    page_number=page.page_number,
                )
                assets.append(_make_asset(
                    kind=CandidateProjectionAssetKind.RENDERED_PAGE,
                    ordinal=len(assets), content=page.image_bytes, locator=locator
                ))
            if len(rendered) > MAX_PROJECTION_ASSETS:
                limits.append("ASSET_LIMIT_REACHED")
        except (PdfRendererUnavailableError, ValueError, _ProjectionLimited):
            limits.append("PDF_RENDER_UNAVAILABLE")
    return blocks, assets, limits, renderer_version


def _xml_text(element: ElementTree.Element, namespace: str) -> str:
    return "".join(node.text or "" for node in element.iter(f"{namespace}t")).strip()


def _parse_docx(
    source: CandidateInformationSource, content: bytes
) -> tuple[list[CandidateProjectionBlock], list[tuple[CandidateProjectionAsset, bytes]], list[str]]:
    blocks: list[CandidateProjectionBlock] = []
    assets: list[tuple[CandidateProjectionAsset, bytes]] = []
    limits: list[str] = []
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
            body = root.find(f"{_W}body")
            if body is None:
                raise _ProjectionUnreadable("DOCX body is missing")
            paragraph_index = table_index = 0
            for child in body:
                if child.tag == f"{_W}p":
                    value = _xml_text(child, _W)
                    if value:
                        style_node = child.find(f"{_W}pPr/{_W}pStyle")
                        style = style_node.get(f"{_W}val", "") if style_node is not None else ""
                        num = child.find(f"{_W}pPr/{_W}numPr") is not None
                        kind = CandidateProjectionBlockType.HEADING if style.casefold().startswith("heading") else CandidateProjectionBlockType.LIST_ITEM if num else CandidateProjectionBlockType.PARAGRAPH
                        blocks.append(_make_block(
                            block_type=kind, ordinal=len(blocks), text=value,
                            locator=CandidateSourceLocator(
                                source_id=source.source_id, source_version=source.source_version,
                                container_kind=CandidateSourceLocatorContainerKind.DOCX,
                                paragraph_index=paragraph_index,
                            ),
                            structural_role=(
                                "heading" if kind is CandidateProjectionBlockType.HEADING
                                else "list-item" if num else "paragraph"
                            ),
                        ))
                        for link_index, link in enumerate(
                            child.findall(f".//{_W}hyperlink")
                        ):
                            link_text = _xml_text(link, _W)
                            if link_text:
                                blocks.append(_make_block(
                                    block_type=CandidateProjectionBlockType.LINK,
                                    ordinal=len(blocks),
                                    text=link_text,
                                    locator=CandidateSourceLocator(
                                        source_id=source.source_id,
                                        source_version=source.source_version,
                                        container_kind=CandidateSourceLocatorContainerKind.DOCX,
                                        paragraph_index=paragraph_index,
                                        element_path=f"hyperlink/{link_index}",
                                    ),
                                    structural_role="hyperlink-text",
                                ))
                    paragraph_index += 1
                elif child.tag == f"{_W}tbl":
                    for row_index, row in enumerate(child.findall(f"{_W}tr")):
                        for column_index, cell in enumerate(row.findall(f"{_W}tc")):
                            value = " ".join(filter(None, (_xml_text(p, _W) for p in cell.findall(f"{_W}p"))))
                            if value:
                                blocks.append(_make_block(
                                    block_type=CandidateProjectionBlockType.TABLE_CELL,
                                    ordinal=len(blocks), text=value,
                                    locator=CandidateSourceLocator(
                                        source_id=source.source_id, source_version=source.source_version,
                                        container_kind=CandidateSourceLocatorContainerKind.DOCX,
                                        table_index=table_index, row_index=row_index,
                                        column_index=column_index,
                                    ),
                                    structural_role="table-cell",
                                ))
                    table_index += 1
            media_names = sorted(
                name for name in archive.namelist()
                if name.startswith("word/media/") and name.casefold().endswith((".png", ".jpg", ".jpeg"))
            )
            for index, name in enumerate(media_names[:MAX_PROJECTION_ASSETS]):
                content_bytes = archive.read(name)
                locator = CandidateSourceLocator(
                    source_id=source.source_id, source_version=source.source_version,
                    container_kind=CandidateSourceLocatorContainerKind.DOCX,
                    element_path=f"word/media/{index}",
                )
                try:
                    assets.append(_make_asset(
                        kind=CandidateProjectionAssetKind.EMBEDDED_IMAGE,
                        ordinal=len(assets), content=content_bytes, locator=locator
                    ))
                except (_ProjectionLimited, _ProjectionUnreadable, _ProjectionUnsupported):
                    limits.append("EMBEDDED_IMAGE_SKIPPED")
            if len(media_names) > MAX_PROJECTION_ASSETS:
                limits.append("ASSET_LIMIT_REACHED")
    except (_ProjectionUnreadable, _ProjectionUnsupported):
        raise
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError, OSError) as exc:
        raise _ProjectionUnreadable("DOCX cannot be parsed") from exc
    return blocks, assets, limits


def _parse_pptx(
    source: CandidateInformationSource, content: bytes
) -> tuple[list[CandidateProjectionBlock], list[tuple[CandidateProjectionAsset, bytes]], list[str]]:
    blocks: list[CandidateProjectionBlock] = []
    assets: list[tuple[CandidateProjectionAsset, bytes]] = []
    limits: list[str] = ["PPTX_READING_ORDER_APPROXIMATE"]
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            slide_names = sorted(
                (n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", n)),
                key=lambda n: int(re.search(r"([0-9]+)", n.rsplit("/", 1)[-1]).group(1)),
            )
            for slide_number, name in enumerate(slide_names, 1):
                root = ElementTree.fromstring(archive.read(name))
                shape_index = 0
                for shape in root.iter(f"{_P}sp"):
                    value = " ".join(filter(None, (node.text for node in shape.iter(f"{_A}t")))).strip()
                    if not value:
                        shape_index += 1
                        continue
                    placeholder = shape.find(f".//{_P}ph")
                    raw_role = placeholder.get("type", "text") if placeholder is not None else "text"
                    role = "title" if raw_role in {"title", "ctrTitle"} else "text"
                    kind = CandidateProjectionBlockType.TITLE if role == "title" else CandidateProjectionBlockType.SLIDE_TEXT
                    blocks.append(_make_block(
                        block_type=kind, ordinal=len(blocks), text=value,
                        locator=CandidateSourceLocator(
                            source_id=source.source_id, source_version=source.source_version,
                            container_kind=CandidateSourceLocatorContainerKind.PPTX,
                            slide_number=slide_number, block_index=shape_index,
                        ),
                        structural_role=f"slide-{role}",
                    ))
                    shape_index += 1
                for table_index, table in enumerate(root.iter(f"{_A}tbl")):
                    for row_index, row in enumerate(table.findall(f"{_A}tr")):
                        for column_index, cell in enumerate(row.findall(f"{_A}tc")):
                            value = " ".join(
                                filter(
                                    None,
                                    (node.text for node in cell.iter(f"{_A}t")),
                                )
                            ).strip()
                            if value:
                                blocks.append(_make_block(
                                    block_type=CandidateProjectionBlockType.TABLE_CELL,
                                    ordinal=len(blocks),
                                    text=value,
                                    locator=CandidateSourceLocator(
                                        source_id=source.source_id,
                                        source_version=source.source_version,
                                        container_kind=CandidateSourceLocatorContainerKind.PPTX,
                                        slide_number=slide_number,
                                        table_index=table_index,
                                        row_index=row_index,
                                        column_index=column_index,
                                    ),
                                    structural_role="slide-table-cell",
                                ))
            note_names = sorted(
                n for n in archive.namelist()
                if re.fullmatch(r"ppt/notesSlides/notesSlide[0-9]+\.xml", n)
            )
            for note_index, name in enumerate(note_names, 1):
                root = ElementTree.fromstring(archive.read(name))
                value = " ".join(filter(None, (node.text for node in root.iter(f"{_A}t")))).strip()
                if value:
                    blocks.append(_make_block(
                        block_type=CandidateProjectionBlockType.SPEAKER_NOTE,
                        ordinal=len(blocks), text=value,
                        locator=CandidateSourceLocator(
                            source_id=source.source_id, source_version=source.source_version,
                            container_kind=CandidateSourceLocatorContainerKind.PPTX,
                            slide_number=note_index, element_path="notes",
                        ),
                        structural_role="speaker-note",
                    ))
            media_names = sorted(n for n in archive.namelist() if n.startswith("ppt/media/") and n.casefold().endswith((".png", ".jpg", ".jpeg")))
            for index, name in enumerate(media_names[:MAX_PROJECTION_ASSETS]):
                try:
                    assets.append(_make_asset(
                        kind=CandidateProjectionAssetKind.EMBEDDED_IMAGE,
                        ordinal=len(assets), content=archive.read(name),
                        locator=CandidateSourceLocator(
                            source_id=source.source_id, source_version=source.source_version,
                            container_kind=CandidateSourceLocatorContainerKind.PPTX,
                            element_path=f"ppt/media/{index}",
                        ),
                    ))
                except (_ProjectionLimited, _ProjectionUnreadable, _ProjectionUnsupported):
                    limits.append("EMBEDDED_IMAGE_SKIPPED")
            if len(media_names) > MAX_PROJECTION_ASSETS:
                limits.append("ASSET_LIMIT_REACHED")
    except (KeyError, zipfile.BadZipFile, ElementTree.ParseError, OSError) as exc:
        raise _ProjectionUnreadable("PPTX cannot be parsed") from exc
    return blocks, assets, limits


class _DeterministicHTMLParser(HTMLParser):
    _BLOCK_TAGS = {
        "title", "h1", "h2", "h3", "h4", "h5", "h6",
        "p", "li", "td", "th", "a",
    }
    _SKIP_TAGS = {"script", "style", "iframe", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.active: tuple[str, list[str], int] | None = None
        self.items: list[tuple[str, str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        self.stack.append(tag)
        if tag in self._BLOCK_TAGS and not any(item in self._SKIP_TAGS for item in self.stack):
            self.active = (tag, [], len(self.items))

    def handle_data(self, data: str) -> None:
        if self.active is not None and not any(item in self._SKIP_TAGS for item in self.stack):
            self.active[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.active is not None and self.active[0] == tag:
            value = " ".join("".join(self.active[1]).split())
            if value:
                self.items.append((tag, value, self.active[2]))
            self.active = None
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]


def _parse_html(source: CandidateInformationSource, content: bytes) -> tuple[list[CandidateProjectionBlock], list[str]]:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _ProjectionUnreadable("HTML is not UTF-8") from exc
    parser = _DeterministicHTMLParser()
    try:
        parser.feed(text)
    except Exception as exc:
        raise _ProjectionUnreadable("HTML cannot be parsed") from exc
    blocks: list[CandidateProjectionBlock] = []
    limits: list[str] = []
    for tag, value, element_index in parser.items[:MAX_URL_HTML_BLOCKS]:
        kind = CandidateProjectionBlockType.TITLE if tag == "title" else CandidateProjectionBlockType.HEADING if tag.startswith("h") else CandidateProjectionBlockType.LIST_ITEM if tag == "li" else CandidateProjectionBlockType.TABLE_CELL if tag in {"td", "th"} else CandidateProjectionBlockType.LINK if tag == "a" else CandidateProjectionBlockType.PARAGRAPH
        blocks.append(_make_block(
            block_type=kind, ordinal=len(blocks), text=value,
            locator=CandidateSourceLocator(
                source_id=source.source_id, source_version=source.source_version,
                container_kind=CandidateSourceLocatorContainerKind.HTML,
                block_index=element_index, element_path=f"html/{tag}/{element_index}",
            ),
            structural_role=f"html-{tag}",
        ))
    if len(parser.items) > MAX_URL_HTML_BLOCKS:
        limits.append("BLOCK_LIMIT_REACHED")
    return blocks, limits


def _sniff_capture(content: bytes, declared: str | None) -> str:
    if content.startswith(b"%PDF-"):
        return "application/pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    prefix = content[:1024].decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n").casefold()
    if prefix.startswith("<!doctype html") or prefix.startswith("<html"):
        return "text/html"
    try:
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _ProjectionUnsupported("URL response type is unsupported") from exc
    if declared in {"text/html", "text/plain"}:
        return declared
    return "text/plain"


@runtime_checkable
class CandidateSourceProjectionRepository(Protocol):
    def save(
        self, *, projection: CandidateSourceProjection,
        blocks: Sequence[CandidateProjectionBlock],
        assets: Sequence[tuple[CandidateProjectionAsset, bytes]],
        capture: CandidateURLCapture | None,
        capture_content: bytes | None,
        request_hash: str,
    ) -> ProjectCandidateInformationSourceResult: ...
    def get(self, subject_id: str, projection_id: str) -> CandidateSourceProjectionReadResult: ...
    def list_for_subject(self, subject_id: str) -> CandidateSourceProjectionListResult: ...
    def read_block(self, subject_id: str, projection_id: str, block_id: str) -> CandidateSourceProjectionReadResult: ...
    def read_asset(self, subject_id: str, projection_id: str, asset_id: str) -> CandidateSourceProjectionReadResult: ...
    def read_capture(self, subject_id: str, projection_id: str, capture_id: str) -> CandidateSourceProjectionReadResult: ...
    def replay_invocation(self, subject_id: str, invocation_id: str, request_hash: str) -> ProjectCandidateInformationSourceResult | None: ...


class PrivateHomeCandidateSourceProjectionRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    @property
    def path(self) -> Path:
        return self._home.paths.candidate_source_projections

    def _connect(self) -> sqlite3.Connection:
        self._home.ensure()
        self._home.ensure_private_file(self.path)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projections(
          projection_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,source_id TEXT NOT NULL,
          projection_hash TEXT NOT NULL,created_at TEXT NOT NULL,record_hash TEXT NOT NULL,
          record_json TEXT NOT NULL,UNIQUE(subject_id,projection_hash));
        CREATE TABLE IF NOT EXISTS blocks(
          block_id TEXT NOT NULL,projection_id TEXT NOT NULL,subject_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,record_hash TEXT NOT NULL,record_json TEXT NOT NULL,
          text_value TEXT NOT NULL,PRIMARY KEY(projection_id,block_id),
          FOREIGN KEY(projection_id) REFERENCES projections(projection_id));
        CREATE TABLE IF NOT EXISTS assets(
          asset_id TEXT NOT NULL,projection_id TEXT NOT NULL,subject_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,record_hash TEXT NOT NULL,record_json TEXT NOT NULL,
          content_bytes BLOB NOT NULL,PRIMARY KEY(projection_id,asset_id),
          FOREIGN KEY(projection_id) REFERENCES projections(projection_id));
        CREATE TABLE IF NOT EXISTS captures(
          capture_id TEXT PRIMARY KEY,projection_id TEXT NOT NULL,subject_id TEXT NOT NULL,
          record_hash TEXT NOT NULL,record_json TEXT NOT NULL,content_bytes BLOB NOT NULL,
          FOREIGN KEY(projection_id) REFERENCES projections(projection_id));
        CREATE TABLE IF NOT EXISTS invocations(
          invocation_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,request_hash TEXT NOT NULL,
          projection_id TEXT NOT NULL,FOREIGN KEY(projection_id) REFERENCES projections(projection_id));
        """)
        expected = str(CANDIDATE_SOURCE_PROJECTION_REPOSITORY_SCHEMA_VERSION)
        connection.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version',?)", (expected,))
        connection.commit()
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None or row["value"] != expected:
            connection.close()
            raise _ProjectionIntegrityError("projection schema is unsupported")
        os.chmod(self.path, PRIVATE_FILE_MODE)
        return connection

    @staticmethod
    def _projection_from_row(row: sqlite3.Row) -> CandidateSourceProjection:
        raw = row["record_json"]
        if row["record_hash"] != _sha256(raw.encode()):
            raise _ProjectionIntegrityError("projection record drift")
        value = json.loads(raw)
        value["source_kind"] = CandidateInformationSourceKind(value["source_kind"])
        value["projection_kind"] = CandidateSourceProjectionKind(value["projection_kind"])
        value["completeness"] = CandidateSourceProjectionCompleteness(value["completeness"])
        value["block_ids"] = tuple(value["block_ids"])
        value["asset_ids"] = tuple(value["asset_ids"])
        value["limitation_codes"] = tuple(value["limitation_codes"])
        value["created_at"] = _parse_time(value["created_at"])
        projection = CandidateSourceProjection(**value)
        if (
            projection.projection_id != row["projection_id"]
            or projection.subject_id != row["subject_id"]
            or projection.projection_hash != row["projection_hash"]
            or _time_text(projection.created_at) != row["created_at"]
        ):
            raise _ProjectionIntegrityError("projection row binding drift")
        return projection

    @staticmethod
    def _validate_children(
        connection: sqlite3.Connection,
        projection: CandidateSourceProjection,
    ) -> None:
        block_rows = connection.execute(
            "SELECT block_id FROM blocks WHERE projection_id=? AND subject_id=? ORDER BY ordinal",
            (projection.projection_id, projection.subject_id),
        ).fetchall()
        asset_rows = connection.execute(
            "SELECT asset_id FROM assets WHERE projection_id=? AND subject_id=? ORDER BY ordinal",
            (projection.projection_id, projection.subject_id),
        ).fetchall()
        if (
            tuple(row["block_id"] for row in block_rows) != projection.block_ids
            or tuple(row["asset_id"] for row in asset_rows) != projection.asset_ids
        ):
            raise _ProjectionIntegrityError("projection child binding drift")
        capture_rows = connection.execute(
            "SELECT capture_id FROM captures WHERE projection_id=? AND subject_id=?",
            (projection.projection_id, projection.subject_id),
        ).fetchall()
        expected = () if projection.capture_id is None else (projection.capture_id,)
        if tuple(row["capture_id"] for row in capture_rows) != expected:
            raise _ProjectionIntegrityError("projection capture binding drift")
        for row in connection.execute(
            "SELECT record_hash,record_json,text_value FROM blocks "
            "WHERE projection_id=? AND subject_id=?",
            (projection.projection_id, projection.subject_id),
        ):
            raw = row["record_json"]
            if row["record_hash"] != _sha256(raw.encode()):
                raise _ProjectionIntegrityError("block record drift")
            value = json.loads(raw)
            locator = _locator_from_dict(value.pop("source_locator"))
            value["block_type"] = CandidateProjectionBlockType(value["block_type"])
            value["source_locator"] = locator
            value["text"] = row["text_value"]
            CandidateProjectionBlock(**value)
        for row in connection.execute(
            "SELECT record_hash,record_json,content_bytes FROM assets "
            "WHERE projection_id=? AND subject_id=?",
            (projection.projection_id, projection.subject_id),
        ):
            raw = row["record_json"]
            content = bytes(row["content_bytes"])
            if row["record_hash"] != _sha256(raw.encode()):
                raise _ProjectionIntegrityError("asset record drift")
            value = json.loads(raw)
            value["source_locator"] = _locator_from_dict(
                value.pop("source_locator")
            )
            value["asset_kind"] = CandidateProjectionAssetKind(value["asset_kind"])
            asset = CandidateProjectionAsset(**value)
            if _sha256(content) != asset.content_hash:
                raise _ProjectionIntegrityError("asset payload drift")
        for row in connection.execute(
            "SELECT record_hash,record_json,content_bytes FROM captures "
            "WHERE projection_id=? AND subject_id=?",
            (projection.projection_id, projection.subject_id),
        ):
            raw = row["record_json"]
            content = bytes(row["content_bytes"])
            if row["record_hash"] != _sha256(raw.encode()):
                raise _ProjectionIntegrityError("capture record drift")
            value = json.loads(raw)
            value["redirect_chain"] = tuple(value["redirect_chain"])
            value["fetched_at"] = _parse_time(value["fetched_at"])
            capture = CandidateURLCapture(**value)
            if _sha256(content) != capture.content_hash:
                raise _ProjectionIntegrityError("capture payload drift")

    def replay_invocation(self, subject_id: str, invocation_id: str, request_hash: str) -> ProjectCandidateInformationSourceResult | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)
                ).fetchone()
                if row is None:
                    return None
                if row["subject_id"] != subject_id or row["request_hash"] != request_hash:
                    return ProjectCandidateInformationSourceResult(
                        ProjectCandidateSourceStatus.INTEGRITY_FAILURE,
                        failure_code="PROJECTION_INVOCATION_CONFLICT",
                    )
                projection_row = connection.execute(
                    "SELECT * FROM projections WHERE projection_id=? AND subject_id=?",
                    (row["projection_id"], subject_id),
                ).fetchone()
                if projection_row is None:
                    raise _ProjectionIntegrityError("invocation target missing")
                return ProjectCandidateInformationSourceResult(
                    ProjectCandidateSourceStatus.UNCHANGED,
                    self._projection_from_row(projection_row),
                )
        except (sqlite3.Error, ValueError, json.JSONDecodeError, _ProjectionIntegrityError):
            return ProjectCandidateInformationSourceResult(
                ProjectCandidateSourceStatus.INTEGRITY_FAILURE,
                failure_code="PROJECTION_REPOSITORY_INTEGRITY",
            )

    def save(self, *, projection: CandidateSourceProjection, blocks: Sequence[CandidateProjectionBlock], assets: Sequence[tuple[CandidateProjectionAsset, bytes]], capture: CandidateURLCapture | None, capture_content: bytes | None, request_hash: str) -> ProjectCandidateInformationSourceResult:
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior_invocation = connection.execute(
                    "SELECT * FROM invocations WHERE invocation_id=?", (projection.invocation_id,)
                ).fetchone()
                if prior_invocation is not None:
                    connection.rollback()
                    return self.replay_invocation(projection.subject_id, projection.invocation_id, request_hash) or ProjectCandidateInformationSourceResult(ProjectCandidateSourceStatus.INTEGRITY_FAILURE, failure_code="PROJECTION_INVOCATION_CONFLICT")
                prior = connection.execute(
                    "SELECT * FROM projections WHERE subject_id=? AND projection_hash=?",
                    (projection.subject_id, projection.projection_hash),
                ).fetchone()
                if prior is not None:
                    existing = self._projection_from_row(prior)
                    self._validate_children(connection, existing)
                    connection.execute(
                        "INSERT INTO invocations VALUES(?,?,?,?)",
                        (projection.invocation_id, projection.subject_id, request_hash, existing.projection_id),
                    )
                    connection.commit()
                    return ProjectCandidateInformationSourceResult(ProjectCandidateSourceStatus.UNCHANGED, existing)
                record = json.dumps(projection.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                connection.execute(
                    "INSERT INTO projections VALUES(?,?,?,?,?,?,?)",
                    (projection.projection_id, projection.subject_id, projection.source_id, projection.projection_hash, _time_text(projection.created_at), _sha256(record.encode()), record),
                )
                for block in blocks:
                    block_record = json.dumps(
                        {**block.metadata_dict(), "parent_block_id": block.parent_block_id, "parser_policy_version": block.parser_policy_version},
                        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                    )
                    connection.execute(
                        "INSERT INTO blocks VALUES(?,?,?,?,?,?,?)",
                        (block.block_id, projection.projection_id, projection.subject_id, block.ordinal, _sha256(block_record.encode()), block_record, block.text),
                    )
                for asset, content in assets:
                    if _sha256(content) != asset.content_hash or len(content) != asset.byte_size:
                        raise _ProjectionIntegrityError("asset payload drift")
                    asset_record = json.dumps(asset.to_dict(), separators=(",", ":"), sort_keys=True)
                    connection.execute(
                        "INSERT INTO assets VALUES(?,?,?,?,?,?,?)",
                        (asset.asset_id, projection.projection_id, projection.subject_id, asset.ordinal, _sha256(asset_record.encode()), asset_record, content),
                    )
                if capture is not None:
                    if capture_content is None or _sha256(capture_content) != capture.content_hash:
                        raise _ProjectionIntegrityError("capture payload drift")
                    capture_record = json.dumps(capture.to_dict(), separators=(",", ":"), sort_keys=True)
                    connection.execute(
                        "INSERT INTO captures VALUES(?,?,?,?,?,?)",
                        (capture.capture_id, projection.projection_id, projection.subject_id, _sha256(capture_record.encode()), capture_record, capture_content),
                    )
                connection.execute(
                    "INSERT INTO invocations VALUES(?,?,?,?)",
                    (projection.invocation_id, projection.subject_id, request_hash, projection.projection_id),
                )
                connection.commit()
                return ProjectCandidateInformationSourceResult(ProjectCandidateSourceStatus.CREATED, projection)
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except (sqlite3.Error, ValueError, TypeError, _ProjectionIntegrityError):
            return ProjectCandidateInformationSourceResult(
                ProjectCandidateSourceStatus.INTEGRITY_FAILURE,
                failure_code="PROJECTION_REPOSITORY_INTEGRITY",
            )

    def get(self, subject_id: str, projection_id: str) -> CandidateSourceProjectionReadResult:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM projections WHERE projection_id=? AND subject_id=?",
                    (projection_id, subject_id),
                ).fetchone()
                if row is None:
                    return CandidateSourceProjectionReadResult(CandidateSourceProjectionReadStatus.NOT_FOUND)
                projection = self._projection_from_row(row)
                self._validate_children(connection, projection)
                return CandidateSourceProjectionReadResult(
                    CandidateSourceProjectionReadStatus.FOUND,
                    projection=projection,
                )
        except (sqlite3.Error, ValueError, json.JSONDecodeError, _ProjectionIntegrityError):
            return CandidateSourceProjectionReadResult(CandidateSourceProjectionReadStatus.INTEGRITY_FAILURE, failure_code="PROJECTION_REPOSITORY_INTEGRITY")

    def list_for_subject(self, subject_id: str) -> CandidateSourceProjectionListResult:
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM projections WHERE subject_id=? ORDER BY created_at,projection_id",
                    (subject_id,),
                ).fetchall()
                projections = tuple(self._projection_from_row(row) for row in rows)
                for projection in projections:
                    self._validate_children(connection, projection)
                return CandidateSourceProjectionListResult(
                    CandidateSourceProjectionListStatus.SUCCEEDED, projections
                )
        except (sqlite3.Error, ValueError, json.JSONDecodeError, _ProjectionIntegrityError):
            return CandidateSourceProjectionListResult(CandidateSourceProjectionListStatus.INTEGRITY_FAILURE, (), "PROJECTION_REPOSITORY_INTEGRITY")

    def read_block(self, subject_id: str, projection_id: str, block_id: str) -> CandidateSourceProjectionReadResult:
        try:
            with self._connect() as connection:
                projection_row = connection.execute(
                    "SELECT * FROM projections WHERE projection_id=? AND subject_id=?",
                    (projection_id, subject_id),
                ).fetchone()
                if projection_row is None:
                    return CandidateSourceProjectionReadResult(
                        CandidateSourceProjectionReadStatus.NOT_FOUND
                    )
                projection = self._projection_from_row(projection_row)
                self._validate_children(connection, projection)
                if block_id not in projection.block_ids:
                    return CandidateSourceProjectionReadResult(
                        CandidateSourceProjectionReadStatus.NOT_FOUND
                    )
                row = connection.execute(
                    "SELECT * FROM blocks WHERE projection_id=? AND block_id=? AND subject_id=?",
                    (projection_id, block_id, subject_id),
                ).fetchone()
                if row is None:
                    return CandidateSourceProjectionReadResult(CandidateSourceProjectionReadStatus.NOT_FOUND)
                raw = row["record_json"]
                if row["record_hash"] != _sha256(raw.encode()):
                    raise _ProjectionIntegrityError("block record drift")
                value = json.loads(raw)
                locator = _locator_from_dict(value.pop("source_locator"))
                value["block_type"] = CandidateProjectionBlockType(
                    value["block_type"]
                )
                value["source_locator"] = locator
                value["text"] = row["text_value"]
                block = CandidateProjectionBlock(**value)
                if block.block_id != row["block_id"] or block.ordinal != row["ordinal"]:
                    raise _ProjectionIntegrityError("block row binding drift")
                return CandidateSourceProjectionReadResult(
                    CandidateSourceProjectionReadStatus.FOUND,
                    block=block,
                )
        except (sqlite3.Error, ValueError, json.JSONDecodeError, _ProjectionIntegrityError):
            return CandidateSourceProjectionReadResult(CandidateSourceProjectionReadStatus.INTEGRITY_FAILURE, failure_code="PROJECTION_REPOSITORY_INTEGRITY")

    def read_asset(self, subject_id: str, projection_id: str, asset_id: str) -> CandidateSourceProjectionReadResult:
        try:
            with self._connect() as connection:
                projection_row = connection.execute(
                    "SELECT * FROM projections WHERE projection_id=? AND subject_id=?",
                    (projection_id, subject_id),
                ).fetchone()
                if projection_row is None:
                    return CandidateSourceProjectionReadResult(
                        CandidateSourceProjectionReadStatus.NOT_FOUND
                    )
                projection = self._projection_from_row(projection_row)
                self._validate_children(connection, projection)
                if asset_id not in projection.asset_ids:
                    return CandidateSourceProjectionReadResult(
                        CandidateSourceProjectionReadStatus.NOT_FOUND
                    )
                row = connection.execute(
                    "SELECT * FROM assets WHERE projection_id=? AND asset_id=? AND subject_id=?",
                    (projection_id, asset_id, subject_id),
                ).fetchone()
                if row is None:
                    return CandidateSourceProjectionReadResult(CandidateSourceProjectionReadStatus.NOT_FOUND)
                raw = row["record_json"]
                content = bytes(row["content_bytes"])
                if row["record_hash"] != _sha256(raw.encode()):
                    raise _ProjectionIntegrityError("asset record drift")
                value = json.loads(raw)
                locator = _locator_from_dict(value.pop("source_locator"))
                value["asset_kind"] = CandidateProjectionAssetKind(value["asset_kind"])
                value["source_locator"] = locator
                asset = CandidateProjectionAsset(**value)
                if _sha256(content) != asset.content_hash or len(content) != asset.byte_size:
                    raise _ProjectionIntegrityError("asset payload drift")
                return CandidateSourceProjectionReadResult(
                    CandidateSourceProjectionReadStatus.FOUND,
                    asset_payload=CandidateProjectionAssetPayload(projection_id, asset, content),
                )
        except (sqlite3.Error, ValueError, json.JSONDecodeError, _ProjectionIntegrityError):
            return CandidateSourceProjectionReadResult(CandidateSourceProjectionReadStatus.INTEGRITY_FAILURE, failure_code="PROJECTION_REPOSITORY_INTEGRITY")

    def read_capture(
        self,
        subject_id: str,
        projection_id: str,
        capture_id: str,
    ) -> CandidateSourceProjectionReadResult:
        try:
            with self._connect() as connection:
                projection_row = connection.execute(
                    "SELECT * FROM projections WHERE projection_id=? AND subject_id=?",
                    (projection_id, subject_id),
                ).fetchone()
                if projection_row is None:
                    return CandidateSourceProjectionReadResult(
                        CandidateSourceProjectionReadStatus.NOT_FOUND
                    )
                projection = self._projection_from_row(projection_row)
                self._validate_children(connection, projection)
                if capture_id != projection.capture_id:
                    return CandidateSourceProjectionReadResult(
                        CandidateSourceProjectionReadStatus.NOT_FOUND
                    )
                row = connection.execute(
                    "SELECT * FROM captures WHERE projection_id=? AND capture_id=? AND subject_id=?",
                    (projection_id, capture_id, subject_id),
                ).fetchone()
                if row is None:
                    return CandidateSourceProjectionReadResult(
                        CandidateSourceProjectionReadStatus.NOT_FOUND
                    )
                raw = row["record_json"]
                content = bytes(row["content_bytes"])
                if row["record_hash"] != _sha256(raw.encode()):
                    raise _ProjectionIntegrityError("capture record drift")
                value = json.loads(raw)
                value["redirect_chain"] = tuple(value["redirect_chain"])
                value["fetched_at"] = _parse_time(value["fetched_at"])
                capture = CandidateURLCapture(**value)
                if (
                    capture.capture_id != row["capture_id"]
                    or capture.subject_id != projection.subject_id
                    or capture.source_id != projection.source_id
                    or capture.capture_hash != projection.capture_hash
                    or _sha256(content) != capture.content_hash
                    or len(content) != capture.content_byte_size
                ):
                    raise _ProjectionIntegrityError("capture payload drift")
                return CandidateSourceProjectionReadResult(
                    CandidateSourceProjectionReadStatus.FOUND,
                    capture_payload=CandidateURLCapturePayload(
                        projection_id, capture, content
                    ),
                )
        except (
            sqlite3.Error,
            ValueError,
            json.JSONDecodeError,
            _ProjectionIntegrityError,
        ):
            return CandidateSourceProjectionReadResult(
                CandidateSourceProjectionReadStatus.INTEGRITY_FAILURE,
                failure_code="PROJECTION_REPOSITORY_INTEGRITY",
            )


def _request_hash(command: ProjectCandidateInformationSourceCommand) -> str:
    return _hash_mapping({
        "invocation_id": command.invocation_id,
        "projection_contract_version": CANDIDATE_SOURCE_PROJECTION_CONTRACT_VERSION,
        "source_id": command.source_id,
        "source_identity_hash": command.source_identity_hash,
        "source_version": command.source_version,
        "subject_id": command.subject_id,
    })


def _capture(
    source: CandidateInformationSource,
    response: CandidateURLFetchResponse,
    now: datetime,
) -> tuple[CandidateURLCapture, bytes]:
    if response.status is not CandidateURLFetchStatus.SUCCEEDED or response.content is None or response.final_url is None or response.response_status is None:
        raise _ProjectionUnreadable("URL capture failed")
    detected = _sniff_capture(response.content, response.content_type)
    content_hash = _sha256(response.content)
    binding = {
        "canonical_url_hash": _sha256(cast_url(source).encode()),
        "capture_contract_version": CANDIDATE_URL_CAPTURE_CONTRACT_VERSION,
        "content_hash": content_hash,
        "detected_content_type": detected,
        "fetch_policy_version": CANDIDATE_URL_FETCH_POLICY_VERSION,
        "final_url_hash": _sha256(response.final_url.encode()),
        "redirect_chain_hash": _sha256("\n".join(response.redirect_chain).encode()),
        "response_status": response.response_status,
        "source_id": source.source_id,
        "subject_id": source.subject_id,
    }
    digest = _hash_mapping(binding)
    capture = CandidateURLCapture(
        capture_id=f"candidate-capture-{digest[:32]}",
        subject_id=source.subject_id,
        source_id=source.source_id,
        canonical_url=cast_url(source),
        final_url=response.final_url,
        response_status=response.response_status,
        detected_content_type=detected,
        content_byte_size=len(response.content),
        content_hash=content_hash,
        managed_payload_ref=f"capture-payload-{content_hash}",
        redirect_chain=response.redirect_chain,
        fetched_at=now,
        capture_hash=digest,
    )
    return capture, response.content


def cast_url(source: CandidateInformationSource) -> str:
    descriptor = source.source_descriptor
    if not isinstance(descriptor, CandidateURLSourceDescriptor):
        raise _ProjectionIntegrityError("URL descriptor binding is invalid")
    return descriptor.canonical_url


def _bounded_blocks(
    blocks: Sequence[CandidateProjectionBlock],
    limits: list[str],
) -> list[CandidateProjectionBlock]:
    selected: list[CandidateProjectionBlock] = []
    total_bytes = 0
    for block in blocks:
        block_bytes = len(block.text.encode("utf-8"))
        if len(selected) >= MAX_PROJECTION_BLOCKS:
            limits.append("BLOCK_LIMIT_REACHED")
            break
        if total_bytes + block_bytes > MAX_PROJECTION_TEXT_BYTES:
            limits.append("TEXT_TRUNCATED")
            break
        selected.append(block)
        total_bytes += block_bytes
    return selected


def _build_projection(
    *, command: ProjectCandidateInformationSourceCommand,
    source: CandidateInformationSource,
    kind: CandidateSourceProjectionKind,
    blocks: Sequence[CandidateProjectionBlock],
    assets: Sequence[tuple[CandidateProjectionAsset, bytes]],
    limits: Sequence[str],
    capture: CandidateURLCapture | None,
    renderer_policy_version: str | None,
) -> CandidateSourceProjection:
    completeness = CandidateSourceProjectionCompleteness.COMPLETED_WITH_LIMITS if limits else CandidateSourceProjectionCompleteness.COMPLETED
    binding = {
        "asset_ids": [asset.asset_id for asset, _ in assets],
        "block_ids": [block.block_id for block in blocks],
        "capture_hash": capture.capture_hash if capture else None,
        "capture_id": capture.capture_id if capture else None,
        "completeness": completeness.value,
        "limitation_codes": sorted(set(limits)),
        "limits_policy_version": CANDIDATE_SOURCE_PROJECTION_LIMITS_VERSION,
        "parser_policy_version": CANDIDATE_SOURCE_PARSER_POLICY_VERSION,
        "projection_contract_version": CANDIDATE_SOURCE_PROJECTION_CONTRACT_VERSION,
        "projection_kind": kind.value,
        "renderer_policy_version": renderer_policy_version,
        "source_id": source.source_id,
        "source_identity_hash": source.source_identity_hash,
        "source_kind": source.source_kind.value,
        "source_payload_hash": source.source_payload_hash,
        "source_version": source.source_version,
        "subject_id": source.subject_id,
    }
    digest = _hash_mapping(binding)
    return CandidateSourceProjection(
        projection_id=f"candidate-projection-{digest[:32]}",
        subject_id=source.subject_id,
        source_id=source.source_id,
        source_kind=source.source_kind,
        source_version=source.source_version,
        source_payload_hash=source.source_payload_hash,
        source_identity_hash=source.source_identity_hash,
        projection_kind=kind,
        completeness=completeness,
        block_ids=tuple(binding["block_ids"]),
        asset_ids=tuple(binding["asset_ids"]),
        capture_id=binding["capture_id"],
        capture_hash=binding["capture_hash"],
        limitation_codes=tuple(binding["limitation_codes"]),
        parser_policy_version=CANDIDATE_SOURCE_PARSER_POLICY_VERSION,
        limits_policy_version=CANDIDATE_SOURCE_PROJECTION_LIMITS_VERSION,
        renderer_policy_version=renderer_policy_version,
        projection_hash=digest,
        created_at=command.now,
        invocation_id=command.invocation_id,
    )


def project_candidate_information_source(
    command: ProjectCandidateInformationSourceCommand,
    *,
    source_repository: CandidateInformationSourceRepository,
    projection_repository: CandidateSourceProjectionRepository,
    url_fetcher: CandidateURLFetchPort | None = None,
    pdf_renderer: PdfPageRendererPort | None = None,
) -> ProjectCandidateInformationSourceResult:
    try:
        request_hash = _request_hash(command)
        replay = projection_repository.replay_invocation(command.subject_id, command.invocation_id, request_hash)
        if replay is not None:
            return replay
        source_result = get_candidate_information_source(
            GetCandidateInformationSourceCommand(command.subject_id, command.source_id),
            repository=source_repository,
        )
        if source_result.status is not CandidateInformationSourceReadStatus.FOUND or source_result.source is None:
            return ProjectCandidateInformationSourceResult(
                ProjectCandidateSourceStatus.INTEGRITY_FAILURE,
                failure_code="SOURCE_BINDING_UNAVAILABLE",
            )
        source = source_result.source
        if (
            source.source_version != command.source_version
            or source.source_identity_hash != command.source_identity_hash
            or source.source_contract_version != CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION
        ):
            return ProjectCandidateInformationSourceResult(
                ProjectCandidateSourceStatus.INTEGRITY_FAILURE,
                failure_code="SOURCE_BINDING_MISMATCH",
            )
        payload_result = read_candidate_information_source_payload(
            GetCandidateInformationSourceCommand(command.subject_id, command.source_id),
            repository=source_repository,
        )
        if payload_result.status is not CandidateInformationSourcePayloadReadStatus.FOUND or payload_result.payload is None:
            return ProjectCandidateInformationSourceResult(
                ProjectCandidateSourceStatus.INTEGRITY_FAILURE,
                failure_code="SOURCE_PAYLOAD_INTEGRITY",
            )
        payload = payload_result.payload
        blocks: list[CandidateProjectionBlock] = []
        assets: list[tuple[CandidateProjectionAsset, bytes]] = []
        limits: list[str] = []
        capture: CandidateURLCapture | None = None
        capture_content: bytes | None = None
        renderer_version: str | None = None
        if source.source_kind is CandidateInformationSourceKind.USER_STATEMENT:
            if payload.statement_text is None:
                raise _ProjectionIntegrityError("statement payload binding is invalid")
            blocks, limits = _chunk_text(
                source, payload.statement_text,
                container=CandidateSourceLocatorContainerKind.TEXT,
                block_type=CandidateProjectionBlockType.USER_STATEMENT,
            )
            projection_kind = CandidateSourceProjectionKind.USER_STATEMENT
        elif source.source_kind is CandidateInformationSourceKind.URL:
            if url_fetcher is None:
                return ProjectCandidateInformationSourceResult(ProjectCandidateSourceStatus.FETCH_FAILED, failure_code="URL_FETCH_PORT_UNAVAILABLE")
            response = url_fetcher.fetch(CandidateURLFetchRequest(cast_url(source)))
            if response.status is not CandidateURLFetchStatus.SUCCEEDED:
                status = ProjectCandidateSourceStatus.UNSUPPORTED if response.status in {CandidateURLFetchStatus.BLOCKED, CandidateURLFetchStatus.UNSUPPORTED, CandidateURLFetchStatus.TOO_LARGE} else ProjectCandidateSourceStatus.FETCH_FAILED
                return ProjectCandidateInformationSourceResult(status, failure_code=response.failure_code or "URL_FETCH_FAILED")
            capture, capture_content = _capture(source, response, command.now)
            if capture.detected_content_type == "text/html":
                blocks, limits = _parse_html(source, capture_content)
            elif capture.detected_content_type == "text/plain":
                text_value = capture_content.decode("utf-8", errors="strict")
                blocks, limits = _chunk_text(source, text_value, container=CandidateSourceLocatorContainerKind.URL_CAPTURE, block_type=CandidateProjectionBlockType.PARAGRAPH)
            elif capture.detected_content_type == "application/pdf":
                blocks, assets, limits, renderer_version = _parse_pdf(source, capture_content, pdf_renderer)
            else:
                locator = CandidateSourceLocator(source.source_id, source.source_version, CandidateSourceLocatorContainerKind.URL_CAPTURE)
                assets = [_make_asset(kind=CandidateProjectionAssetKind.SOURCE_IMAGE, ordinal=0, content=capture_content, locator=locator)]
            projection_kind = CandidateSourceProjectionKind.WEB_SNAPSHOT
        else:
            if payload.file_bytes is None or not isinstance(source.source_descriptor, CandidateFileSourceDescriptor):
                raise _ProjectionIntegrityError("file payload binding is invalid")
            content = payload.file_bytes
            if _sha256(content) != source.source_payload_hash or len(content) != source.source_descriptor.byte_size:
                raise _ProjectionIntegrityError("file payload drift")
            detected = source.source_descriptor.detected_format
            if detected is CandidateFileDetectedFormat.PDF:
                blocks, assets, limits, renderer_version = _parse_pdf(source, content, pdf_renderer)
                projection_kind = CandidateSourceProjectionKind.DOCUMENT
            elif detected is CandidateFileDetectedFormat.DOCX:
                blocks, assets, limits = _parse_docx(source, content)
                projection_kind = CandidateSourceProjectionKind.DOCUMENT
            elif detected is CandidateFileDetectedFormat.PPTX:
                blocks, assets, limits = _parse_pptx(source, content)
                projection_kind = CandidateSourceProjectionKind.DOCUMENT
            elif detected is CandidateFileDetectedFormat.UTF8_TEXT:
                blocks, limits = _chunk_text(source, content.decode("utf-8"), container=CandidateSourceLocatorContainerKind.TEXT, block_type=CandidateProjectionBlockType.PARAGRAPH)
                projection_kind = CandidateSourceProjectionKind.DOCUMENT
            else:
                locator = CandidateSourceLocator(source.source_id, source.source_version, CandidateSourceLocatorContainerKind.IMAGE)
                assets = [_make_asset(kind=CandidateProjectionAssetKind.SOURCE_IMAGE, ordinal=0, content=content, locator=locator)]
                projection_kind = CandidateSourceProjectionKind.IMAGE
        blocks = _bounded_blocks(blocks, limits)
        projection = _build_projection(
            command=command, source=source, kind=projection_kind, blocks=blocks,
            assets=assets, limits=limits, capture=capture,
            renderer_policy_version=renderer_version,
        )
        return projection_repository.save(
            projection=projection, blocks=blocks, assets=assets, capture=capture,
            capture_content=capture_content, request_hash=request_hash,
        )
    except _ProjectionUnsupported:
        return ProjectCandidateInformationSourceResult(ProjectCandidateSourceStatus.UNSUPPORTED, failure_code="PROJECTION_UNSUPPORTED")
    except _ProjectionUnreadable:
        return ProjectCandidateInformationSourceResult(ProjectCandidateSourceStatus.NOT_READABLE, failure_code="PROJECTION_NOT_READABLE")
    except _ProjectionLimited:
        return ProjectCandidateInformationSourceResult(
            ProjectCandidateSourceStatus.UNSUPPORTED,
            failure_code="PROJECTION_LIMIT_EXCEEDED",
        )
    except (TypeError, ValueError, UnicodeError, _ProjectionIntegrityError):
        return ProjectCandidateInformationSourceResult(ProjectCandidateSourceStatus.INTEGRITY_FAILURE, failure_code="PROJECTION_INTEGRITY")
    except Exception:
        return ProjectCandidateInformationSourceResult(ProjectCandidateSourceStatus.FAILED, failure_code="PROJECTION_FAILED")


def get_candidate_source_projection(subject_id: str, projection_id: str, *, repository: CandidateSourceProjectionRepository) -> CandidateSourceProjectionReadResult:
    return repository.get(subject_id, projection_id)


def list_candidate_source_projections(subject_id: str, *, repository: CandidateSourceProjectionRepository) -> CandidateSourceProjectionListResult:
    return repository.list_for_subject(subject_id)


def read_candidate_projection_block(subject_id: str, projection_id: str, block_id: str, *, repository: CandidateSourceProjectionRepository) -> CandidateSourceProjectionReadResult:
    return repository.read_block(subject_id, projection_id, block_id)


def read_candidate_projection_asset(subject_id: str, projection_id: str, asset_id: str, *, repository: CandidateSourceProjectionRepository) -> CandidateSourceProjectionReadResult:
    return repository.read_asset(subject_id, projection_id, asset_id)


def read_candidate_url_capture(subject_id: str, projection_id: str, capture_id: str, *, repository: CandidateSourceProjectionRepository) -> CandidateSourceProjectionReadResult:
    return repository.read_capture(subject_id, projection_id, capture_id)


__all__ = [
    "CANDIDATE_SOURCE_PROJECTION_CONTRACT_VERSION",
    "CANDIDATE_SOURCE_PARSER_POLICY_VERSION",
    "CANDIDATE_URL_CAPTURE_CONTRACT_VERSION",
    "CANDIDATE_URL_FETCH_POLICY_VERSION",
    "CandidateProjectionAsset",
    "CandidateProjectionAssetKind",
    "CandidateProjectionAssetPayload",
    "CandidateProjectionBlock",
    "CandidateProjectionBlockType",
    "CandidateSourceLocator",
    "CandidateSourceLocatorContainerKind",
    "CandidateSourceProjection",
    "CandidateSourceProjectionCompleteness",
    "CandidateSourceProjectionKind",
    "CandidateSourceProjectionListResult",
    "CandidateSourceProjectionReadResult",
    "CandidateSourceProjectionReadStatus",
    "CandidateSourceProjectionRepository",
    "CandidateURLCapture",
    "CandidateURLCapturePayload",
    "CandidateURLFetchPort",
    "CandidateURLFetchRequest",
    "CandidateURLFetchResponse",
    "CandidateURLFetchStatus",
    "PinnedHTTPSCandidateURLFetcher",
    "PrivateHomeCandidateSourceProjectionRepository",
    "ProjectCandidateInformationSourceCommand",
    "ProjectCandidateInformationSourceResult",
    "ProjectCandidateSourceStatus",
    "get_candidate_source_projection",
    "list_candidate_source_projections",
    "project_candidate_information_source",
    "read_candidate_projection_asset",
    "read_candidate_projection_block",
    "read_candidate_url_capture",
]
