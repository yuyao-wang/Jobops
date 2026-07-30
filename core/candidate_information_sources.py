"""Subject-scoped immutable registry for raw candidate information sources."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import unicodedata
import zipfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from xml.etree import ElementTree

from PIL import Image, UnidentifiedImageError

from .candidate_identity_facts import (
    CandidateIdentityFactSourceKind,
    CandidateIdentityFactSourceRef,
)
from .private_home import PRIVATE_FILE_MODE, PrivateHome, PrivateHomeError


CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION = (
    "candidate-information-source-v1"
)
CANDIDATE_INFORMATION_SOURCE_REPOSITORY_SCHEMA_VERSION = 1
CANDIDATE_FILE_MEDIA_CONTRACT_VERSION = "candidate-file-media-v1"
CANDIDATE_URL_CANONICALIZATION_POLICY_VERSION = "candidate-url-canonicalization-v1"
CANDIDATE_USER_STATEMENT_CONTRACT_VERSION = "candidate-user-statement-v1"
CANDIDATE_SOURCE_LIMITS_POLICY_VERSION = "candidate-source-limits-v1"
CANDIDATE_SOURCE_VERSION = CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION

MAX_CANDIDATE_FILE_BYTES = 25 * 1024 * 1024
MAX_CANDIDATE_STATEMENT_BYTES = 64 * 1024
MAX_CANDIDATE_IMAGE_DIMENSION = 12_000
MAX_CANDIDATE_IMAGE_PIXELS = 40_000_000
MAX_OFFICE_ENTRIES = 4_096
MAX_OFFICE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_DISPLAY_NAME_CHARS = 160

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_SECRET_QUERY_PARTS = (
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "secret",
    "signature",
)
_PDF_SIGNATURE = b"%PDF-"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE = b"\xff\xd8\xff"
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class CandidateInformationSourceKind(StrEnum):
    FILE = "FILE"
    URL = "URL"
    USER_STATEMENT = "USER_STATEMENT"


class CandidateFileDetectedFormat(StrEnum):
    PDF = "PDF"
    DOCX = "DOCX"
    PPTX = "PPTX"
    PNG = "PNG"
    JPEG = "JPEG"
    UTF8_TEXT = "UTF8_TEXT"


class CandidateInformationSourceRegistrationStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"
    TOO_LARGE = "TOO_LARGE"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class CandidateInformationSourceReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CandidateInformationSourceListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CandidateInformationSourcePayloadReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class _InvalidSource(ValueError):
    pass


class _UnsupportedSource(ValueError):
    pass


class _SourceTooLarge(ValueError):
    pass


class _SourceIntegrityError(RuntimeError):
    pass


def _clean_id(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if cleaned != value or _ID_RE.fullmatch(cleaned) is None:
        raise ValueError(f"{name} is invalid")
    return cleaned


def _clean_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _hash_mapping(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _format_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("registered_at is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("registered_at is invalid")
    return parsed.astimezone(timezone.utc)


def _safe_display_name(value: str | None, *, fallback: str) -> str:
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise _InvalidSource("display name is invalid")
    cleaned = unicodedata.normalize("NFC", value).strip()
    if (
        not cleaned
        or len(cleaned) > MAX_DISPLAY_NAME_CHARS
        or any(ord(char) < 32 for char in cleaned)
        or any(char in cleaned for char in "<>\\/")
    ):
        raise _InvalidSource("display name is invalid")
    return cleaned


def _canonical_text(content: bytes, *, maximum: int) -> bytes:
    if not isinstance(content, bytes):
        raise _InvalidSource("text payload must be bytes")
    if not content:
        raise _InvalidSource("text payload is empty")
    if len(content) > maximum:
        raise _SourceTooLarge("text payload exceeds the server policy")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _InvalidSource("text payload is not UTF-8") from exc
    if "\x00" in text or any(
        ord(char) < 32 and char not in "\n\r\t" for char in text
    ):
        raise _InvalidSource("text payload contains unsupported controls")
    normalized = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    encoded = normalized.encode("utf-8")
    if not normalized.strip():
        raise _InvalidSource("text payload is empty")
    if len(encoded) > maximum:
        raise _SourceTooLarge("canonical text exceeds the server policy")
    return encoded


def _looks_like_active_text(content: bytes) -> bool:
    text = (
        content.decode("utf-8", errors="strict")
        .lstrip("\ufeff \t\r\n")
        .casefold()
    )
    return (
        text.startswith("#!")
        or text.startswith("<!doctype html")
        or text.startswith("<html")
        or text.startswith("<script")
    )


def _validate_pdf(content: bytes) -> None:
    if not content.startswith(_PDF_SIGNATURE):
        raise _InvalidSource("PDF signature is invalid")
    try:
        import pdfplumber

        with pdfplumber.open(BytesIO(content)) as document:
            if not document.pages:
                raise _InvalidSource("PDF has no pages")
    except _InvalidSource:
        raise
    except Exception as exc:
        raise _InvalidSource("PDF structure is invalid") from exc


def _validate_image(
    content: bytes,
    *,
    expected_format: CandidateFileDetectedFormat,
) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(content)) as image:
            actual = str(image.format or "").upper()
            required = "JPEG" if expected_format is CandidateFileDetectedFormat.JPEG else "PNG"
            if actual != required:
                raise _InvalidSource("image format does not match its bytes")
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_CANDIDATE_IMAGE_DIMENSION
                or height > MAX_CANDIDATE_IMAGE_DIMENSION
                or width * height > MAX_CANDIDATE_IMAGE_PIXELS
            ):
                raise _InvalidSource("image dimensions exceed the server policy")
            image.verify()
    except _InvalidSource:
        raise
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        raise _InvalidSource("image payload is invalid") from exc
    return width, height


def _safe_office_member(info: zipfile.ZipInfo) -> bool:
    name = info.filename
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or PurePosixPath(name).is_absolute()
        or ".." in PurePosixPath(name).parts
        or info.flag_bits & 0x1
    ):
        return False
    mode = (info.external_attr >> 16) & 0o170000
    return mode not in {0o120000, 0o060000}


def _validate_office_container(
    content: bytes,
) -> CandidateFileDetectedFormat:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if (
                not infos
                or len(infos) > MAX_OFFICE_ENTRIES
                or len(names) != len(set(names))
                or any(not _safe_office_member(item) for item in infos)
                or sum(item.file_size for item in infos)
                > MAX_OFFICE_UNCOMPRESSED_BYTES
            ):
                raise _InvalidSource("Office container structure is invalid")
            lowered = tuple(name.casefold() for name in names)
            if any(
                name.endswith(".bin")
                or "/embeddings/" in name
                or "/activex/" in name
                or name.startswith("customui/")
                for name in lowered
            ):
                raise _UnsupportedSource("active Office payload is unsupported")
            required_common = {"[Content_Types].xml", "_rels/.rels"}
            if not required_common.issubset(names):
                raise _UnsupportedSource("generic ZIP is unsupported")
            content_types = archive.read("[Content_Types].xml")
            if len(content_types) > 2 * 1024 * 1024:
                raise _InvalidSource("Office content types are oversized")
            try:
                ElementTree.fromstring(content_types)
            except ElementTree.ParseError as exc:
                raise _InvalidSource("Office content types are invalid") from exc
            is_docx = (
                "word/document.xml" in names
                and b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
                in content_types
            )
            is_pptx = (
                "ppt/presentation.xml" in names
                and b"application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
                in content_types
            )
            if is_docx == is_pptx:
                raise _UnsupportedSource("generic ZIP is unsupported")
            main_name = "word/document.xml" if is_docx else "ppt/presentation.xml"
            main = archive.read(main_name)
            if not main or len(main) > 32 * 1024 * 1024:
                raise _InvalidSource("Office main document is invalid")
            try:
                root = ElementTree.fromstring(main)
            except ElementTree.ParseError as exc:
                raise _InvalidSource("Office main document is invalid") from exc
            expected_root = "document" if is_docx else "presentation"
            if root.tag.rsplit("}", 1)[-1] != expected_root:
                raise _InvalidSource("Office main document root is invalid")
            return (
                CandidateFileDetectedFormat.DOCX
                if is_docx
                else CandidateFileDetectedFormat.PPTX
            )
    except (_InvalidSource, _UnsupportedSource):
        raise
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise _UnsupportedSource("generic ZIP is unsupported") from exc


def _detect_candidate_file(
    content: bytes,
) -> tuple[CandidateFileDetectedFormat, bytes, tuple[int, int] | None]:
    if not isinstance(content, bytes):
        raise _InvalidSource("file payload must be bytes")
    if not content:
        raise _InvalidSource("file payload is empty")
    if len(content) > MAX_CANDIDATE_FILE_BYTES:
        raise _SourceTooLarge("file payload exceeds the server policy")
    if content.startswith(_PDF_SIGNATURE):
        _validate_pdf(content)
        return CandidateFileDetectedFormat.PDF, content, None
    if content.startswith(_PNG_SIGNATURE):
        dimensions = _validate_image(
            content, expected_format=CandidateFileDetectedFormat.PNG
        )
        return CandidateFileDetectedFormat.PNG, content, dimensions
    if content.startswith(_JPEG_SIGNATURE):
        dimensions = _validate_image(
            content, expected_format=CandidateFileDetectedFormat.JPEG
        )
        return CandidateFileDetectedFormat.JPEG, content, dimensions
    if content.startswith(_ZIP_SIGNATURES):
        detected = _validate_office_container(content)
        return detected, content, None
    try:
        canonical = _canonical_text(content, maximum=MAX_CANDIDATE_FILE_BYTES)
    except _SourceTooLarge:
        raise
    except _InvalidSource as exc:
        raise _UnsupportedSource("binary file format is unsupported") from exc
    if _looks_like_active_text(canonical):
        raise _UnsupportedSource("active text format is unsupported")
    return CandidateFileDetectedFormat.UTF8_TEXT, canonical, None


def _canonicalize_url(value: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise _InvalidSource("URL must be a string")
    submitted = value.strip()
    if (
        not submitted
        or len(submitted) > 4_096
        or "\\" in submitted
        or any(char.isspace() for char in submitted)
        or any(ord(char) < 32 for char in submitted)
    ):
        raise _InvalidSource("URL is invalid")
    try:
        parsed = urlsplit(submitted)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise _InvalidSource("URL scheme is unsupported")
        if parsed.username is not None or parsed.password is not None:
            raise _InvalidSource("URL userinfo is forbidden")
        host = parsed.hostname.encode("idna").decode("ascii").casefold()
        if host == "localhost" or host.endswith(".localhost"):
            raise _InvalidSource("local URL is forbidden")
        try:
            address = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            address = None
        if address is not None and (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
        ):
            raise _InvalidSource("local URL is forbidden")
        port = parsed.port
        authority_host = (
            f"[{host}]"
            if address is not None and address.version == 6
            else host
        )
        authority = (
            authority_host
            if port in {None, 443}
            else f"{authority_host}:{port}"
        )
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = key.casefold().replace("-", "_")
            if any(part in normalized_key for part in _SECRET_QUERY_PARTS):
                raise _InvalidSource("URL query contains forbidden credentials")
        canonical = urlunsplit(
            (
                "https",
                authority,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
    except _InvalidSource:
        raise
    except (UnicodeError, ValueError) as exc:
        raise _InvalidSource("URL is invalid") from exc
    return canonical, host


def canonicalize_candidate_source_url(value: str) -> tuple[str, str]:
    """Return the authoritative C1a canonical HTTPS identity."""
    return _canonicalize_url(value)


@dataclass(frozen=True, slots=True)
class CandidateFileSourceDescriptor:
    detected_format: CandidateFileDetectedFormat
    byte_size: int
    content_sha256: str
    managed_payload_ref: str
    original_display_name: str
    image_width: int | None = None
    image_height: int | None = None
    media_contract_version: str = CANDIDATE_FILE_MEDIA_CONTRACT_VERSION
    limits_policy_version: str = CANDIDATE_SOURCE_LIMITS_POLICY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "detected_format", CandidateFileDetectedFormat(self.detected_format)
        )
        if self.media_contract_version != CANDIDATE_FILE_MEDIA_CONTRACT_VERSION:
            raise ValueError("file media contract version is unsupported")
        if self.limits_policy_version != CANDIDATE_SOURCE_LIMITS_POLICY_VERSION:
            raise ValueError("source limits policy version is unsupported")
        if type(self.byte_size) is not int or not 0 < self.byte_size <= MAX_CANDIDATE_FILE_BYTES:
            raise ValueError("file byte size is invalid")
        object.__setattr__(
            self, "content_sha256", _clean_hash("content_sha256", self.content_sha256)
        )
        object.__setattr__(
            self,
            "managed_payload_ref",
            _clean_id("managed_payload_ref", self.managed_payload_ref),
        )
        if (
            _safe_display_name(
                self.original_display_name, fallback="Candidate file"
            )
            != self.original_display_name
        ):
            raise ValueError("original display name is invalid")
        dimensions = (self.image_width, self.image_height)
        if self.detected_format in {
            CandidateFileDetectedFormat.PNG,
            CandidateFileDetectedFormat.JPEG,
        }:
            if any(type(item) is not int or item <= 0 for item in dimensions):
                raise ValueError("image dimensions are invalid")
        elif dimensions != (None, None):
            raise ValueError("non-image descriptor cannot carry dimensions")

    def to_dict(self) -> dict[str, Any]:
        return {
            "byte_size": self.byte_size,
            "content_sha256": self.content_sha256,
            "detected_format": self.detected_format.value,
            "image_height": self.image_height,
            "image_width": self.image_width,
            "limits_policy_version": self.limits_policy_version,
            "managed_payload_ref": self.managed_payload_ref,
            "media_contract_version": self.media_contract_version,
            "original_display_name": self.original_display_name,
        }


@dataclass(frozen=True, slots=True)
class CandidateURLSourceDescriptor:
    canonical_url: str = field(repr=False)
    url_sha256: str
    submitted_host: str
    canonicalization_policy_version: str = (
        CANDIDATE_URL_CANONICALIZATION_POLICY_VERSION
    )

    def __post_init__(self) -> None:
        if (
            self.canonicalization_policy_version
            != CANDIDATE_URL_CANONICALIZATION_POLICY_VERSION
        ):
            raise ValueError("URL canonicalization policy version is unsupported")
        canonical, host = _canonicalize_url(self.canonical_url)
        if canonical != self.canonical_url or host != self.submitted_host:
            raise ValueError("URL descriptor is not canonical")
        if self.url_sha256 != _sha256(canonical.encode("utf-8")):
            raise ValueError("URL hash is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_url": self.canonical_url,
            "canonicalization_policy_version": self.canonicalization_policy_version,
            "submitted_host": self.submitted_host,
            "url_sha256": self.url_sha256,
        }


@dataclass(frozen=True, slots=True)
class CandidateUserStatementSourceDescriptor:
    statement_byte_size: int
    statement_sha256: str
    managed_payload_ref: str
    statement_contract_version: str = CANDIDATE_USER_STATEMENT_CONTRACT_VERSION
    limits_policy_version: str = CANDIDATE_SOURCE_LIMITS_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.statement_contract_version != CANDIDATE_USER_STATEMENT_CONTRACT_VERSION:
            raise ValueError("statement contract version is unsupported")
        if self.limits_policy_version != CANDIDATE_SOURCE_LIMITS_POLICY_VERSION:
            raise ValueError("source limits policy version is unsupported")
        if (
            type(self.statement_byte_size) is not int
            or not 0 < self.statement_byte_size <= MAX_CANDIDATE_STATEMENT_BYTES
        ):
            raise ValueError("statement byte size is invalid")
        object.__setattr__(
            self,
            "statement_sha256",
            _clean_hash("statement_sha256", self.statement_sha256),
        )
        object.__setattr__(
            self,
            "managed_payload_ref",
            _clean_id("managed_payload_ref", self.managed_payload_ref),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits_policy_version": self.limits_policy_version,
            "managed_payload_ref": self.managed_payload_ref,
            "statement_byte_size": self.statement_byte_size,
            "statement_contract_version": self.statement_contract_version,
            "statement_sha256": self.statement_sha256,
        }


CandidateInformationSourceDescriptor = (
    CandidateFileSourceDescriptor
    | CandidateURLSourceDescriptor
    | CandidateUserStatementSourceDescriptor
)


def _descriptor_from_dict(
    kind: CandidateInformationSourceKind,
    value: Mapping[str, Any],
) -> CandidateInformationSourceDescriptor:
    if not isinstance(value, Mapping):
        raise ValueError("source descriptor is invalid")
    constructors = {
        CandidateInformationSourceKind.FILE: CandidateFileSourceDescriptor,
        CandidateInformationSourceKind.URL: CandidateURLSourceDescriptor,
        CandidateInformationSourceKind.USER_STATEMENT: (
            CandidateUserStatementSourceDescriptor
        ),
    }
    return constructors[kind](**dict(value))


@dataclass(frozen=True, slots=True)
class CandidateInformationSource:
    source_id: str
    subject_id: str
    source_kind: CandidateInformationSourceKind
    source_version: str
    source_payload_hash: str
    source_identity_hash: str
    display_name: str
    registered_at: datetime
    registration_invocation_id: str
    source_descriptor: CandidateInformationSourceDescriptor
    source_contract_version: str = CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _clean_id("source_id", self.source_id))
        object.__setattr__(
            self, "subject_id", _clean_id("subject_id", self.subject_id)
        )
        object.__setattr__(
            self, "source_kind", CandidateInformationSourceKind(self.source_kind)
        )
        if self.source_version != CANDIDATE_SOURCE_VERSION:
            raise ValueError("source version is unsupported")
        object.__setattr__(
            self,
            "source_payload_hash",
            _clean_hash("source_payload_hash", self.source_payload_hash),
        )
        object.__setattr__(
            self,
            "source_identity_hash",
            _clean_hash("source_identity_hash", self.source_identity_hash),
        )
        if self.source_contract_version != CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION:
            raise ValueError("source contract version is unsupported")
        if not isinstance(self.registered_at, datetime) or self.registered_at.tzinfo is None:
            raise ValueError("registered_at must be timezone-aware")
        object.__setattr__(
            self, "registered_at", self.registered_at.astimezone(timezone.utc)
        )
        object.__setattr__(
            self,
            "registration_invocation_id",
            _clean_id(
                "registration_invocation_id", self.registration_invocation_id
            ),
        )
        if (
            _safe_display_name(self.display_name, fallback="Candidate source")
            != self.display_name
        ):
            raise ValueError("display name is invalid")
        expected_descriptor = {
            CandidateInformationSourceKind.FILE: CandidateFileSourceDescriptor,
            CandidateInformationSourceKind.URL: CandidateURLSourceDescriptor,
            CandidateInformationSourceKind.USER_STATEMENT: (
                CandidateUserStatementSourceDescriptor
            ),
        }[self.source_kind]
        if not isinstance(self.source_descriptor, expected_descriptor):
            raise TypeError("source descriptor kind is invalid")
        if self.source_payload_hash != _descriptor_payload_hash(
            self.source_kind, self.source_descriptor
        ):
            raise ValueError("source payload hash binding is invalid")
        identity_hash = _hash_mapping(self.identity_dict())
        if self.source_identity_hash != identity_hash:
            raise ValueError("source identity hash is invalid")
        expected_id = f"candidate-source-{identity_hash[:32]}"
        if self.source_id != expected_id:
            raise ValueError("source ID is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "source_contract_version": self.source_contract_version,
            "source_kind": self.source_kind.value,
            "source_payload_hash": self.source_payload_hash,
            "source_version": self.source_version,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "display_name": self.display_name,
            "registered_at": _format_time(self.registered_at),
            "registration_invocation_id": self.registration_invocation_id,
            "source_descriptor": self.source_descriptor.to_dict(),
            "source_id": self.source_id,
            "source_identity_hash": self.source_identity_hash,
        }

    def to_candidate_identity_fact_source_ref(
        self,
    ) -> CandidateIdentityFactSourceRef:
        kind = {
            CandidateInformationSourceKind.FILE: (
                CandidateIdentityFactSourceKind.DOCUMENT_EXTRACTION
            ),
            CandidateInformationSourceKind.URL: (
                CandidateIdentityFactSourceKind.URL_EXTRACTION
            ),
            CandidateInformationSourceKind.USER_STATEMENT: (
                CandidateIdentityFactSourceKind.USER_STATEMENT
            ),
        }[self.source_kind]
        return CandidateIdentityFactSourceRef(
            source_kind=kind,
            source_id=self.source_id,
            source_version=self.source_version,
            source_hash=self.source_identity_hash,
            source_locator="source:root",
            source_subject_id=self.subject_id,
        )


def _descriptor_payload_hash(
    kind: CandidateInformationSourceKind,
    descriptor: CandidateInformationSourceDescriptor,
) -> str:
    if kind is CandidateInformationSourceKind.FILE:
        if not isinstance(descriptor, CandidateFileSourceDescriptor):
            raise ValueError("file source descriptor is invalid")
        return descriptor.content_sha256
    if kind is CandidateInformationSourceKind.URL:
        if not isinstance(descriptor, CandidateURLSourceDescriptor):
            raise ValueError("URL source descriptor is invalid")
        return descriptor.url_sha256
    if not isinstance(descriptor, CandidateUserStatementSourceDescriptor):
        raise ValueError("statement source descriptor is invalid")
    return descriptor.statement_sha256


def _source_from_dict(value: Mapping[str, Any]) -> CandidateInformationSource:
    expected = {
        "display_name",
        "registered_at",
        "registration_invocation_id",
        "source_contract_version",
        "source_descriptor",
        "source_id",
        "source_identity_hash",
        "source_kind",
        "source_payload_hash",
        "source_version",
        "subject_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted source fields are invalid")
    payload = dict(value)
    kind = CandidateInformationSourceKind(payload["source_kind"])
    payload["source_kind"] = kind
    payload["registered_at"] = _parse_time(payload["registered_at"])
    payload["source_descriptor"] = _descriptor_from_dict(
        kind, payload["source_descriptor"]
    )
    return CandidateInformationSource(**payload)


@dataclass(frozen=True, slots=True)
class RegisterCandidateFileSourceCommand:
    subject_id: str
    invocation_id: str
    now: datetime
    content: bytes = field(repr=False)
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class RegisterCandidateURLSourceCommand:
    subject_id: str
    invocation_id: str
    now: datetime
    url: str = field(repr=False)
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class RegisterCandidateUserStatementSourceCommand:
    subject_id: str
    invocation_id: str
    now: datetime
    statement_utf8: bytes = field(repr=False)
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateInformationSourceRegistrationResult:
    status: CandidateInformationSourceRegistrationStatus
    source: CandidateInformationSource | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class GetCandidateInformationSourceCommand:
    subject_id: str
    source_id: str


@dataclass(frozen=True, slots=True)
class CandidateInformationSourceReadResult:
    status: CandidateInformationSourceReadStatus
    source: CandidateInformationSource | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateInformationSourceListResult:
    status: CandidateInformationSourceListStatus
    sources: tuple[CandidateInformationSource, ...]
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateInformationSourcePayload:
    source_id: str
    source_kind: CandidateInformationSourceKind
    payload_hash: str
    file_bytes: bytes | None = field(default=None, repr=False)
    canonical_url: str | None = field(default=None, repr=False)
    statement_text: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_id", _clean_id("source_id", self.source_id)
        )
        object.__setattr__(
            self, "source_kind", CandidateInformationSourceKind(self.source_kind)
        )
        object.__setattr__(
            self, "payload_hash", _clean_hash("payload_hash", self.payload_hash)
        )
        populated = tuple(
            value is not None
            for value in (self.file_bytes, self.canonical_url, self.statement_text)
        )
        if sum(populated) != 1:
            raise ValueError("source payload is invalid")
        expected_index = {
            CandidateInformationSourceKind.FILE: 0,
            CandidateInformationSourceKind.URL: 1,
            CandidateInformationSourceKind.USER_STATEMENT: 2,
        }[self.source_kind]
        if not populated[expected_index]:
            raise ValueError("source payload kind is invalid")


@dataclass(frozen=True, slots=True)
class CandidateInformationSourcePayloadReadResult:
    status: CandidateInformationSourcePayloadReadStatus
    payload: CandidateInformationSourcePayload | None = None
    failure_code: str | None = None


@runtime_checkable
class CandidateInformationSourceRepository(Protocol):
    def register(
        self,
        *,
        subject_id: str,
        invocation_id: str,
        now: datetime,
        kind: CandidateInformationSourceKind,
        canonical_payload: bytes,
        display_name: str,
        descriptor: CandidateInformationSourceDescriptor,
    ) -> CandidateInformationSourceRegistrationResult:
        """Atomically register metadata and any managed payload."""

    def get(
        self, command: GetCandidateInformationSourceCommand
    ) -> CandidateInformationSourceReadResult:
        """Read exact source metadata within one subject."""

    def list_for_subject(
        self, subject_id: str
    ) -> CandidateInformationSourceListResult:
        """List source metadata without loading payloads."""

    def read_payload(
        self, command: GetCandidateInformationSourceCommand
    ) -> CandidateInformationSourcePayloadReadResult:
        """Read exact managed bytes/text or canonical URL without a path."""


class PrivateHomeCandidateInformationSourceRepository:
    """One transactional metadata and content-addressed payload registry."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    @property
    def path(self) -> Path:
        return self._home.paths.candidate_information_sources

    def _connect(self) -> sqlite3.Connection:
        self._home.ensure()
        self._home.ensure_private_file(self.path)
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = DELETE")
        self._initialize(connection)
        os.chmod(self.path, PRIVATE_FILE_MODE)
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payloads (
                subject_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                payload_bytes BLOB NOT NULL,
                PRIMARY KEY(subject_id, payload_hash)
            );
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_payload_hash TEXT NOT NULL,
                source_identity_hash TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                payload_managed INTEGER NOT NULL,
                record_hash TEXT NOT NULL,
                record_json TEXT NOT NULL,
                UNIQUE(subject_id, source_kind, source_payload_hash)
            );
            CREATE TABLE IF NOT EXISTS invocations (
                invocation_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                source_id TEXT NOT NULL,
                FOREIGN KEY(source_id) REFERENCES sources(source_id)
            );
            """
        )
        expected = str(CANDIDATE_INFORMATION_SOURCE_REPOSITORY_SCHEMA_VERSION)
        connection.execute(
            """
            INSERT OR IGNORE INTO metadata(key, value)
            VALUES('schema_version', ?)
            """,
            (expected,),
        )
        connection.commit()
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row["value"] != expected:
            raise _SourceIntegrityError("registry schema version is unsupported")

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> CandidateInformationSource:
        record_json = row["record_json"]
        if (
            not isinstance(record_json, str)
            or row["record_hash"]
            != _sha256(record_json.encode("utf-8"))
        ):
            raise _SourceIntegrityError("source record hash is invalid")
        try:
            source = _source_from_dict(json.loads(record_json))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _SourceIntegrityError("source record is invalid") from exc
        if (
            row["source_id"] != source.source_id
            or row["subject_id"] != source.subject_id
            or row["source_kind"] != source.source_kind.value
            or row["source_payload_hash"] != source.source_payload_hash
            or row["source_identity_hash"] != source.source_identity_hash
            or row["registered_at"] != _format_time(source.registered_at)
        ):
            raise _SourceIntegrityError("source record binding is invalid")
        managed_expected = int(
            source.source_kind
            in {
                CandidateInformationSourceKind.FILE,
                CandidateInformationSourceKind.USER_STATEMENT,
            }
        )
        if row["payload_managed"] != managed_expected:
            raise _SourceIntegrityError("source payload binding is invalid")
        return source

    @staticmethod
    def _payload_bytes_tx(
        connection: sqlite3.Connection,
        *,
        source: CandidateInformationSource,
    ) -> bytes:
        row = connection.execute(
            """
            SELECT byte_size, payload_bytes FROM payloads
            WHERE subject_id = ? AND payload_hash = ?
            """,
            (source.subject_id, source.source_payload_hash),
        ).fetchone()
        if row is None:
            raise _SourceIntegrityError("managed payload is missing")
        content = bytes(row["payload_bytes"])
        if (
            row["byte_size"] != len(content)
            or _sha256(content) != source.source_payload_hash
        ):
            raise _SourceIntegrityError("managed payload integrity failed")
        return content

    @classmethod
    def _verify_source_payload_tx(
        cls,
        connection: sqlite3.Connection,
        source: CandidateInformationSource,
    ) -> None:
        if source.source_kind is CandidateInformationSourceKind.URL:
            descriptor = source.source_descriptor
            assert isinstance(descriptor, CandidateURLSourceDescriptor)
            if _sha256(descriptor.canonical_url.encode("utf-8")) != (
                source.source_payload_hash
            ):
                raise _SourceIntegrityError("URL payload integrity failed")
            return
        content = cls._payload_bytes_tx(connection, source=source)
        descriptor = source.source_descriptor
        if isinstance(descriptor, CandidateFileSourceDescriptor):
            if (
                descriptor.byte_size != len(content)
                or descriptor.managed_payload_ref
                != f"payload-{source.source_payload_hash}"
            ):
                raise _SourceIntegrityError("file descriptor integrity failed")
        elif isinstance(descriptor, CandidateUserStatementSourceDescriptor):
            if (
                descriptor.statement_byte_size != len(content)
                or descriptor.managed_payload_ref
                != f"payload-{source.source_payload_hash}"
            ):
                raise _SourceIntegrityError("statement descriptor integrity failed")

    @staticmethod
    def _insert_payload(
        connection: sqlite3.Connection,
        *,
        subject_id: str,
        payload_hash: str,
        payload: bytes,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO payloads(
                subject_id, payload_hash, byte_size, payload_bytes
            ) VALUES (?, ?, ?, ?)
            """,
            (subject_id, payload_hash, len(payload), payload),
        )
        row = connection.execute(
            """
            SELECT byte_size, payload_bytes FROM payloads
            WHERE subject_id = ? AND payload_hash = ?
            """,
            (subject_id, payload_hash),
        ).fetchone()
        if (
            row is None
            or row["byte_size"] != len(payload)
            or bytes(row["payload_bytes"]) != payload
            or _sha256(bytes(row["payload_bytes"])) != payload_hash
        ):
            raise _SourceIntegrityError("content-addressed payload conflict")

    @staticmethod
    def _insert_source(
        connection: sqlite3.Connection,
        *,
        source: CandidateInformationSource,
        payload_managed: bool,
    ) -> None:
        record_json = json.dumps(
            source.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO sources(
                source_id, subject_id, source_kind, source_payload_hash,
                source_identity_hash, registered_at, payload_managed,
                record_hash, record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.source_id,
                source.subject_id,
                source.source_kind.value,
                source.source_payload_hash,
                source.source_identity_hash,
                _format_time(source.registered_at),
                int(payload_managed),
                _sha256(record_json.encode("utf-8")),
                record_json,
            ),
        )

    def register(
        self,
        *,
        subject_id: str,
        invocation_id: str,
        now: datetime,
        kind: CandidateInformationSourceKind,
        canonical_payload: bytes,
        display_name: str,
        descriptor: CandidateInformationSourceDescriptor,
    ) -> CandidateInformationSourceRegistrationResult:
        try:
            subject = _clean_id("subject_id", subject_id)
            invocation = _clean_id("invocation_id", invocation_id)
            registered_at = _parse_time(_format_time(now))
            source_kind = CandidateInformationSourceKind(kind)
            if not isinstance(canonical_payload, bytes) or not canonical_payload:
                raise ValueError("canonical payload is invalid")
            payload_hash = _sha256(canonical_payload)
            if payload_hash != _descriptor_payload_hash(source_kind, descriptor):
                raise ValueError("descriptor payload hash is invalid")
            identity = {
                "source_contract_version": CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION,
                "source_kind": source_kind.value,
                "source_payload_hash": payload_hash,
                "source_version": CANDIDATE_SOURCE_VERSION,
                "subject_id": subject,
            }
            identity_hash = _hash_mapping(identity)
            source = CandidateInformationSource(
                source_id=f"candidate-source-{identity_hash[:32]}",
                subject_id=subject,
                source_kind=source_kind,
                source_version=CANDIDATE_SOURCE_VERSION,
                source_payload_hash=payload_hash,
                source_identity_hash=identity_hash,
                display_name=display_name,
                registered_at=registered_at,
                registration_invocation_id=invocation,
                source_descriptor=descriptor,
            )
            request_hash = identity_hash
            payload_managed = source_kind in {
                CandidateInformationSourceKind.FILE,
                CandidateInformationSourceKind.USER_STATEMENT,
            }
        except (TypeError, ValueError):
            return CandidateInformationSourceRegistrationResult(
                CandidateInformationSourceRegistrationStatus.INVALID,
                failure_code="CANDIDATE_SOURCE_INVALID",
            )

        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = connection.execute(
                    """
                    SELECT subject_id, request_hash, source_id FROM invocations
                    WHERE invocation_id = ?
                    """,
                    (invocation,),
                ).fetchone()
                if replay is not None:
                    if (
                        replay["subject_id"] != subject
                        or replay["request_hash"] != request_hash
                    ):
                        connection.rollback()
                        return CandidateInformationSourceRegistrationResult(
                            CandidateInformationSourceRegistrationStatus.INTEGRITY_FAILURE,
                            failure_code="INVOCATION_PAYLOAD_MISMATCH",
                        )
                    existing = self._get_tx(
                        connection,
                        subject_id=subject,
                        source_id=replay["source_id"],
                    )
                    self._verify_source_payload_tx(connection, existing)
                    connection.rollback()
                    return CandidateInformationSourceRegistrationResult(
                        CandidateInformationSourceRegistrationStatus.UNCHANGED,
                        source=existing,
                    )

                duplicate_row = connection.execute(
                    """
                    SELECT * FROM sources
                    WHERE subject_id = ? AND source_kind = ?
                      AND source_payload_hash = ?
                    """,
                    (subject, source_kind.value, payload_hash),
                ).fetchone()
                if duplicate_row is not None:
                    existing = self._record_from_row(duplicate_row)
                    self._verify_source_payload_tx(connection, existing)
                    connection.execute(
                        """
                        INSERT INTO invocations(
                            invocation_id, subject_id, request_hash, source_id
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (invocation, subject, request_hash, existing.source_id),
                    )
                    connection.commit()
                    return CandidateInformationSourceRegistrationResult(
                        CandidateInformationSourceRegistrationStatus.UNCHANGED,
                        source=existing,
                    )

                if payload_managed:
                    self._insert_payload(
                        connection,
                        subject_id=subject,
                        payload_hash=payload_hash,
                        payload=canonical_payload,
                    )
                self._insert_source(
                    connection,
                    source=source,
                    payload_managed=payload_managed,
                )
                self._verify_source_payload_tx(connection, source)
                connection.execute(
                    """
                    INSERT INTO invocations(
                        invocation_id, subject_id, request_hash, source_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (invocation, subject, request_hash, source.source_id),
                )
                connection.commit()
                return CandidateInformationSourceRegistrationResult(
                    CandidateInformationSourceRegistrationStatus.CREATED,
                    source=source,
                )
        except _SourceIntegrityError:
            return CandidateInformationSourceRegistrationResult(
                CandidateInformationSourceRegistrationStatus.INTEGRITY_FAILURE,
                failure_code="CANDIDATE_SOURCE_INTEGRITY_FAILURE",
            )
        except (
            OSError,
            PrivateHomeError,
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return CandidateInformationSourceRegistrationResult(
                CandidateInformationSourceRegistrationStatus.FAILED,
                failure_code="CANDIDATE_SOURCE_REGISTRATION_FAILED",
            )

    @classmethod
    def _get_tx(
        cls,
        connection: sqlite3.Connection,
        *,
        subject_id: str,
        source_id: str,
    ) -> CandidateInformationSource:
        row = connection.execute(
            """
            SELECT * FROM sources WHERE subject_id = ? AND source_id = ?
            """,
            (subject_id, source_id),
        ).fetchone()
        if row is None:
            raise LookupError
        return cls._record_from_row(row)

    def get(
        self, command: GetCandidateInformationSourceCommand
    ) -> CandidateInformationSourceReadResult:
        try:
            subject = _clean_id("subject_id", command.subject_id)
            source_id = _clean_id("source_id", command.source_id)
            with closing(self._connect()) as connection:
                try:
                    source = self._get_tx(
                        connection, subject_id=subject, source_id=source_id
                    )
                except LookupError:
                    return CandidateInformationSourceReadResult(
                        CandidateInformationSourceReadStatus.NOT_FOUND
                    )
            return CandidateInformationSourceReadResult(
                CandidateInformationSourceReadStatus.FOUND, source=source
            )
        except (
            _SourceIntegrityError,
            OSError,
            PrivateHomeError,
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return CandidateInformationSourceReadResult(
                CandidateInformationSourceReadStatus.INTEGRITY_FAILURE,
                failure_code="CANDIDATE_SOURCE_INTEGRITY_FAILURE",
            )

    def list_for_subject(
        self, subject_id: str
    ) -> CandidateInformationSourceListResult:
        try:
            subject = _clean_id("subject_id", subject_id)
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    """
                    SELECT * FROM sources WHERE subject_id = ?
                    ORDER BY registered_at, source_kind, source_id
                    """,
                    (subject,),
                ).fetchall()
                sources = tuple(self._record_from_row(row) for row in rows)
                connection.rollback()
            return CandidateInformationSourceListResult(
                CandidateInformationSourceListStatus.SUCCEEDED,
                sources=sources,
            )
        except (
            _SourceIntegrityError,
            OSError,
            PrivateHomeError,
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return CandidateInformationSourceListResult(
                CandidateInformationSourceListStatus.INTEGRITY_FAILURE,
                sources=(),
                failure_code="CANDIDATE_SOURCE_INTEGRITY_FAILURE",
            )

    def read_payload(
        self, command: GetCandidateInformationSourceCommand
    ) -> CandidateInformationSourcePayloadReadResult:
        try:
            subject = _clean_id("subject_id", command.subject_id)
            source_id = _clean_id("source_id", command.source_id)
            with closing(self._connect()) as connection:
                try:
                    source = self._get_tx(
                        connection, subject_id=subject, source_id=source_id
                    )
                except LookupError:
                    return CandidateInformationSourcePayloadReadResult(
                        CandidateInformationSourcePayloadReadStatus.NOT_FOUND
                    )
                self._verify_source_payload_tx(connection, source)
                if source.source_kind is CandidateInformationSourceKind.URL:
                    descriptor = source.source_descriptor
                    assert isinstance(descriptor, CandidateURLSourceDescriptor)
                    payload = CandidateInformationSourcePayload(
                        source_id=source.source_id,
                        source_kind=source.source_kind,
                        payload_hash=source.source_payload_hash,
                        canonical_url=descriptor.canonical_url,
                    )
                else:
                    content = self._payload_bytes_tx(
                        connection, source=source
                    )
                    if source.source_kind is CandidateInformationSourceKind.FILE:
                        payload = CandidateInformationSourcePayload(
                            source_id=source.source_id,
                            source_kind=source.source_kind,
                            payload_hash=source.source_payload_hash,
                            file_bytes=content,
                        )
                    else:
                        payload = CandidateInformationSourcePayload(
                            source_id=source.source_id,
                            source_kind=source.source_kind,
                            payload_hash=source.source_payload_hash,
                            statement_text=content.decode("utf-8", errors="strict"),
                        )
            return CandidateInformationSourcePayloadReadResult(
                CandidateInformationSourcePayloadReadStatus.FOUND,
                payload=payload,
            )
        except (
            _SourceIntegrityError,
            OSError,
            PrivateHomeError,
            sqlite3.DatabaseError,
            UnicodeDecodeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return CandidateInformationSourcePayloadReadResult(
                CandidateInformationSourcePayloadReadStatus.INTEGRITY_FAILURE,
                failure_code="CANDIDATE_SOURCE_PAYLOAD_INTEGRITY_FAILURE",
            )


def _invalid_registration(
    status: CandidateInformationSourceRegistrationStatus,
    failure_code: str,
) -> CandidateInformationSourceRegistrationResult:
    return CandidateInformationSourceRegistrationResult(
        status=status, failure_code=failure_code
    )


def register_candidate_file_source(
    command: RegisterCandidateFileSourceCommand,
    *,
    repository: CandidateInformationSourceRepository,
) -> CandidateInformationSourceRegistrationResult:
    try:
        detected, canonical, dimensions = _detect_candidate_file(command.content)
        display = _safe_display_name(
            command.display_name, fallback="Candidate file"
        )
        payload_hash = _sha256(canonical)
        descriptor = CandidateFileSourceDescriptor(
            detected_format=detected,
            byte_size=len(canonical),
            content_sha256=payload_hash,
            managed_payload_ref=f"payload-{payload_hash}",
            original_display_name=display,
            image_width=dimensions[0] if dimensions is not None else None,
            image_height=dimensions[1] if dimensions is not None else None,
        )
    except _SourceTooLarge:
        return _invalid_registration(
            CandidateInformationSourceRegistrationStatus.TOO_LARGE,
            "CANDIDATE_FILE_TOO_LARGE",
        )
    except _UnsupportedSource:
        return _invalid_registration(
            CandidateInformationSourceRegistrationStatus.UNSUPPORTED,
            "CANDIDATE_FILE_UNSUPPORTED",
        )
    except (_InvalidSource, TypeError, ValueError):
        return _invalid_registration(
            CandidateInformationSourceRegistrationStatus.INVALID,
            "CANDIDATE_FILE_INVALID",
        )
    return repository.register(
        subject_id=command.subject_id,
        invocation_id=command.invocation_id,
        now=command.now,
        kind=CandidateInformationSourceKind.FILE,
        canonical_payload=canonical,
        display_name=display,
        descriptor=descriptor,
    )


def register_candidate_url_source(
    command: RegisterCandidateURLSourceCommand,
    *,
    repository: CandidateInformationSourceRepository,
) -> CandidateInformationSourceRegistrationResult:
    try:
        canonical, host = _canonicalize_url(command.url)
        display = _safe_display_name(
            command.display_name, fallback="Candidate URL"
        )
        canonical_bytes = canonical.encode("utf-8")
        descriptor = CandidateURLSourceDescriptor(
            canonical_url=canonical,
            url_sha256=_sha256(canonical_bytes),
            submitted_host=host,
        )
    except (_InvalidSource, TypeError, ValueError):
        return _invalid_registration(
            CandidateInformationSourceRegistrationStatus.INVALID,
            "CANDIDATE_URL_INVALID",
        )
    return repository.register(
        subject_id=command.subject_id,
        invocation_id=command.invocation_id,
        now=command.now,
        kind=CandidateInformationSourceKind.URL,
        canonical_payload=canonical_bytes,
        display_name=display,
        descriptor=descriptor,
    )


def register_candidate_user_statement_source(
    command: RegisterCandidateUserStatementSourceCommand,
    *,
    repository: CandidateInformationSourceRepository,
) -> CandidateInformationSourceRegistrationResult:
    try:
        canonical = _canonical_text(
            command.statement_utf8, maximum=MAX_CANDIDATE_STATEMENT_BYTES
        )
        display = _safe_display_name(
            command.display_name, fallback="Candidate statement"
        )
        payload_hash = _sha256(canonical)
        descriptor = CandidateUserStatementSourceDescriptor(
            statement_byte_size=len(canonical),
            statement_sha256=payload_hash,
            managed_payload_ref=f"payload-{payload_hash}",
        )
    except _SourceTooLarge:
        return _invalid_registration(
            CandidateInformationSourceRegistrationStatus.TOO_LARGE,
            "CANDIDATE_STATEMENT_TOO_LARGE",
        )
    except (_InvalidSource, TypeError, ValueError):
        return _invalid_registration(
            CandidateInformationSourceRegistrationStatus.INVALID,
            "CANDIDATE_STATEMENT_INVALID",
        )
    return repository.register(
        subject_id=command.subject_id,
        invocation_id=command.invocation_id,
        now=command.now,
        kind=CandidateInformationSourceKind.USER_STATEMENT,
        canonical_payload=canonical,
        display_name=display,
        descriptor=descriptor,
    )


def get_candidate_information_source(
    command: GetCandidateInformationSourceCommand,
    *,
    repository: CandidateInformationSourceRepository,
) -> CandidateInformationSourceReadResult:
    return repository.get(command)


def list_candidate_information_sources(
    subject_id: str,
    *,
    repository: CandidateInformationSourceRepository,
) -> CandidateInformationSourceListResult:
    return repository.list_for_subject(subject_id)


def read_candidate_information_source_payload(
    command: GetCandidateInformationSourceCommand,
    *,
    repository: CandidateInformationSourceRepository,
) -> CandidateInformationSourcePayloadReadResult:
    return repository.read_payload(command)


__all__ = [
    "CANDIDATE_FILE_MEDIA_CONTRACT_VERSION",
    "CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION",
    "CANDIDATE_INFORMATION_SOURCE_REPOSITORY_SCHEMA_VERSION",
    "CANDIDATE_SOURCE_LIMITS_POLICY_VERSION",
    "CANDIDATE_SOURCE_VERSION",
    "CANDIDATE_URL_CANONICALIZATION_POLICY_VERSION",
    "CANDIDATE_USER_STATEMENT_CONTRACT_VERSION",
    "MAX_CANDIDATE_FILE_BYTES",
    "MAX_CANDIDATE_IMAGE_DIMENSION",
    "MAX_CANDIDATE_IMAGE_PIXELS",
    "MAX_CANDIDATE_STATEMENT_BYTES",
    "CandidateFileDetectedFormat",
    "CandidateFileSourceDescriptor",
    "CandidateInformationSource",
    "CandidateInformationSourceKind",
    "CandidateInformationSourceListResult",
    "CandidateInformationSourceListStatus",
    "CandidateInformationSourcePayload",
    "CandidateInformationSourcePayloadReadResult",
    "CandidateInformationSourcePayloadReadStatus",
    "CandidateInformationSourceReadResult",
    "CandidateInformationSourceReadStatus",
    "CandidateInformationSourceRegistrationResult",
    "CandidateInformationSourceRegistrationStatus",
    "CandidateURLSourceDescriptor",
    "CandidateUserStatementSourceDescriptor",
    "GetCandidateInformationSourceCommand",
    "PrivateHomeCandidateInformationSourceRepository",
    "RegisterCandidateFileSourceCommand",
    "RegisterCandidateURLSourceCommand",
    "RegisterCandidateUserStatementSourceCommand",
    "get_candidate_information_source",
    "list_candidate_information_sources",
    "read_candidate_information_source_payload",
    "register_candidate_file_source",
    "register_candidate_url_source",
    "register_candidate_user_statement_source",
    "canonicalize_candidate_source_url",
]
