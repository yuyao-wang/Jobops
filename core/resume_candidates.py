"""Trusted, subject-scoped resume candidates for later preparation selection."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from io import BytesIO
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .private_home import PrivateHome, PrivateHomeError


RESUME_CANDIDATE_CONTRACT_VERSION = "resume-candidate-v1"
MAX_RESUME_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_SELECTION_SUMMARY_CHARS = 20_000
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RESUME_ID_PATTERN = re.compile(r"^resume-candidate-[a-f0-9]{64}$")


class ResumeArtifactType(str, Enum):
    PDF = "PDF"
    DOCX = "DOCX"


class ResumeSummarySource(str, Enum):
    AUTHENTICATED_CALLER = "AUTHENTICATED_CALLER"


class ResumeSummaryTrust(str, Enum):
    VERIFIED = "VERIFIED"
    USER_CONFIRMED = "USER_CONFIRMED"


class ResumeCandidateStatus(str, Enum):
    SELECTABLE = "SELECTABLE"


class ResumeCandidateWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeCandidateReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeCandidateListStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ResumeCandidateFailureReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    ARTIFACT_UNMANAGED = "ARTIFACT_UNMANAGED"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    SUMMARY_UNTRUSTED = "SUMMARY_UNTRUSTED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class RegisterResumeCandidateStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the ResumeCandidate contract")
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
        raise ValueError("recorded_at is invalid")
    return _require_aware(
        "recorded_at",
        datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _subject_storage_key(subject_id: str) -> str:
    return f"subject-{hashlib.sha256(subject_id.encode('utf-8')).hexdigest()}"


def _summary_hash(summary: str) -> str:
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def _artifact_suffix(artifact_type: ResumeArtifactType) -> str:
    return {
        ResumeArtifactType.PDF: ".pdf",
        ResumeArtifactType.DOCX: ".docx",
    }[artifact_type]


def _artifact_reference(
    *,
    subject_id: str,
    artifact_sha256: str,
    artifact_type: ResumeArtifactType,
) -> str:
    return str(
        PurePosixPath("state")
        / "preparation"
        / "resume-candidates"
        / "artifacts"
        / _subject_storage_key(subject_id)
        / f"{artifact_sha256}{_artifact_suffix(artifact_type)}"
    )


def _identity_payload(
    *,
    contract_version: str,
    subject_id: str,
    artifact_reference: str,
    artifact_sha256: str,
    artifact_type: ResumeArtifactType,
    display_name: str,
    selection_safe_summary: str,
    selection_safe_summary_sha256: str,
    summary_source: ResumeSummarySource,
    summary_trust: ResumeSummaryTrust,
    status: ResumeCandidateStatus,
) -> dict[str, Any]:
    return {
        "artifact_reference": artifact_reference,
        "artifact_sha256": artifact_sha256,
        "artifact_type": artifact_type.value,
        "contract_version": contract_version,
        "display_name": display_name,
        "selection_safe_summary": selection_safe_summary,
        "selection_safe_summary_sha256": selection_safe_summary_sha256,
        "status": status.value,
        "subject_id": subject_id,
        "summary_source": summary_source.value,
        "summary_trust": summary_trust.value,
    }


def _resume_id(**values: Any) -> str:
    digest = hashlib.sha256(_canonical_json(_identity_payload(**values))).hexdigest()
    return f"resume-candidate-{digest}"


@dataclass(frozen=True, slots=True)
class ResumeCandidate:
    resume_id: str
    contract_version: str
    subject_id: str
    artifact_reference: str
    artifact_sha256: str
    artifact_type: ResumeArtifactType
    display_name: str
    selection_safe_summary: str
    selection_safe_summary_sha256: str
    summary_source: ResumeSummarySource
    summary_trust: ResumeSummaryTrust
    status: ResumeCandidateStatus
    recorded_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version",
            self.contract_version,
            maximum=80,
        )
        if contract != RESUME_CANDIDATE_CONTRACT_VERSION:
            raise ValueError("ResumeCandidate contract version is unsupported")
        subject_id = _clean_text("subject_id", self.subject_id, maximum=160)
        artifact_hash = _require_hash("artifact_sha256", self.artifact_sha256)
        artifact_type = ResumeArtifactType(self.artifact_type)
        expected_reference = _artifact_reference(
            subject_id=subject_id,
            artifact_sha256=artifact_hash,
            artifact_type=artifact_type,
        )
        if self.artifact_reference != expected_reference:
            raise ValueError("artifact_reference is outside the managed contract")
        display_name = _clean_text("display_name", self.display_name, maximum=240)
        summary = _clean_text(
            "selection_safe_summary",
            self.selection_safe_summary,
            maximum=MAX_SELECTION_SUMMARY_CHARS,
        )
        summary_hash = _require_hash(
            "selection_safe_summary_sha256",
            self.selection_safe_summary_sha256,
        )
        if summary_hash != _summary_hash(summary):
            raise ValueError("selection-safe summary hash is invalid")
        source = ResumeSummarySource(self.summary_source)
        trust = ResumeSummaryTrust(self.summary_trust)
        status = ResumeCandidateStatus(self.status)
        recorded_at = _require_aware("recorded_at", self.recorded_at)
        values = {
            "contract_version": contract,
            "subject_id": subject_id,
            "artifact_reference": expected_reference,
            "artifact_sha256": artifact_hash,
            "artifact_type": artifact_type,
            "display_name": display_name,
            "selection_safe_summary": summary,
            "selection_safe_summary_sha256": summary_hash,
            "summary_source": source,
            "summary_trust": trust,
            "status": status,
        }
        if (
            not isinstance(self.resume_id, str)
            or _RESUME_ID_PATTERN.fullmatch(self.resume_id) is None
            or self.resume_id != _resume_id(**values)
        ):
            raise ValueError("resume_id does not match candidate content")
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "artifact_sha256", artifact_hash)
        object.__setattr__(self, "artifact_type", artifact_type)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "selection_safe_summary", summary)
        object.__setattr__(self, "summary_source", source)
        object.__setattr__(self, "summary_trust", trust)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "recorded_at", recorded_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resume_id": self.resume_id,
            "contract_version": self.contract_version,
            "subject_id": self.subject_id,
            "artifact_reference": self.artifact_reference,
            "artifact_sha256": self.artifact_sha256,
            "artifact_type": self.artifact_type.value,
            "display_name": self.display_name,
            "selection_safe_summary": self.selection_safe_summary,
            "selection_safe_summary_sha256": (
                self.selection_safe_summary_sha256
            ),
            "summary_source": self.summary_source.value,
            "summary_trust": self.summary_trust.value,
            "status": self.status.value,
            "recorded_at": _rfc3339(self.recorded_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeCandidateWriteResult:
    status: ResumeCandidateWriteStatus
    candidate: ResumeCandidate | None
    reason_code: ResumeCandidateFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ResumeCandidateWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeCandidateFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ResumeCandidateWriteStatus.CREATED,
            ResumeCandidateWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.candidate, ResumeCandidate)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful candidate write result is invalid")
        elif (
            self.candidate is not None
            or self.reason_code is None
        ):
            raise ValueError("failed candidate write result is invalid")


@dataclass(frozen=True, slots=True)
class ResumeCandidateReadResult:
    status: ResumeCandidateReadStatus
    candidate: ResumeCandidate | None
    reason_code: ResumeCandidateFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeCandidateReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeCandidateFailureReason(self.reason_code),
            )
        if status is ResumeCandidateReadStatus.FOUND:
            if (
                not isinstance(self.candidate, ResumeCandidate)
                or self.reason_code is not None
            ):
                raise ValueError("found candidate read result is invalid")
        elif status is ResumeCandidateReadStatus.NOT_FOUND:
            if self.candidate is not None or self.reason_code is not None:
                raise ValueError("not-found candidate read result is invalid")
        elif (
            self.candidate is not None
            or self.reason_code
            is not ResumeCandidateFailureReason.INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure candidate read result is invalid")


@dataclass(frozen=True, slots=True)
class ResumeCandidateListResult:
    status: ResumeCandidateListStatus
    subject_id: str
    candidates: tuple[ResumeCandidate, ...]
    reason_code: ResumeCandidateFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeCandidateListStatus(self.status)
        object.__setattr__(self, "status", status)
        subject_id = _clean_text("subject_id", self.subject_id, maximum=160)
        object.__setattr__(self, "subject_id", subject_id)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeCandidateFailureReason(self.reason_code),
            )
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(item, ResumeCandidate)
            for item in self.candidates
        ):
            raise TypeError("candidates must be typed ResumeCandidates")
        if status is ResumeCandidateListStatus.SUCCEEDED:
            if (
                self.reason_code is not None
                or any(item.subject_id != subject_id for item in self.candidates)
                or any(
                    item.status is not ResumeCandidateStatus.SELECTABLE
                    for item in self.candidates
                )
            ):
                raise ValueError("successful candidate list result is invalid")
        elif self.candidates or (
            self.reason_code
            is not ResumeCandidateFailureReason.INTEGRITY_FAILURE
        ):
            raise ValueError("failed candidate list result is invalid")


@runtime_checkable
class ResumeCandidateProvider(Protocol):
    def list_selectable(self, subject_id: str) -> ResumeCandidateListResult:
        """Return the complete trusted candidate set for one subject."""


@runtime_checkable
class ResumeCandidateRepository(ResumeCandidateProvider, Protocol):
    def save(self, candidate: ResumeCandidate) -> ResumeCandidateWriteResult:
        """Persist one immutable resume candidate."""

    def get(
        self,
        *,
        subject_id: str,
        resume_id: str,
    ) -> ResumeCandidateReadResult:
        """Read one candidate while enforcing subject ownership."""


def _candidate_from_dict(value: Any) -> ResumeCandidate:
    expected = {
        "resume_id",
        "contract_version",
        "subject_id",
        "artifact_reference",
        "artifact_sha256",
        "artifact_type",
        "display_name",
        "selection_safe_summary",
        "selection_safe_summary_sha256",
        "summary_source",
        "summary_trust",
        "status",
        "recorded_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted ResumeCandidate fields are invalid")
    return ResumeCandidate(
        resume_id=value["resume_id"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        artifact_reference=value["artifact_reference"],
        artifact_sha256=value["artifact_sha256"],
        artifact_type=ResumeArtifactType(value["artifact_type"]),
        display_name=value["display_name"],
        selection_safe_summary=value["selection_safe_summary"],
        selection_safe_summary_sha256=value[
            "selection_safe_summary_sha256"
        ],
        summary_source=ResumeSummarySource(value["summary_source"]),
        summary_trust=ResumeSummaryTrust(value["summary_trust"]),
        status=ResumeCandidateStatus(value["status"]),
        recorded_at=_parse_timestamp(value["recorded_at"]),
    )


def _semantic_content(candidate: ResumeCandidate) -> dict[str, Any]:
    value = candidate.to_dict()
    value.pop("recorded_at")
    return value


def _validate_docx(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    return "[Content_Types].xml" in names and "word/document.xml" in names


def _validate_artifact_content(
    content: bytes,
    artifact_type: ResumeArtifactType,
) -> bool:
    if not content or len(content) > MAX_RESUME_ARTIFACT_BYTES:
        return False
    if artifact_type is ResumeArtifactType.PDF:
        return content.startswith(b"%PDF-")
    return _validate_docx(content)


def detect_resume_artifact_type(content: bytes) -> ResumeArtifactType:
    """Detect one supported ResumeCandidate type from bytes, not its name."""

    if not isinstance(content, bytes):
        raise TypeError("resume artifact content must be bytes")
    if _validate_artifact_content(content, ResumeArtifactType.PDF):
        return ResumeArtifactType.PDF
    if _validate_artifact_content(content, ResumeArtifactType.DOCX):
        return ResumeArtifactType.DOCX
    raise ValueError("resume artifact type is unsupported")


def _artifact_type_from_path(path: Path, content: bytes) -> ResumeArtifactType:
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        artifact_type = ResumeArtifactType.PDF
    elif suffix == ".docx":
        artifact_type = ResumeArtifactType.DOCX
    else:
        raise ValueError("resume artifact type is unsupported")
    if not _validate_artifact_content(content, artifact_type):
        raise ValueError("resume artifact bytes are invalid")
    return artifact_type


class PrivateHomeResumeCandidateRepository:
    """Immutable candidate records with fail-closed artifact verification."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _subject_records(self, subject_id: str) -> Path:
        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        return (
            self._home.paths.resume_candidate_records
            / _subject_storage_key(cleaned)
        )

    def _record_path(self, subject_id: str, resume_id: str) -> Path:
        if (
            not isinstance(resume_id, str)
            or _RESUME_ID_PATTERN.fullmatch(resume_id) is None
        ):
            raise ValueError("resume_id is invalid")
        return self._subject_records(subject_id) / f"{resume_id}.json"

    def _artifact_is_valid(self, candidate: ResumeCandidate) -> bool:
        try:
            path = self._home.contained_path(candidate.artifact_reference)
            if path.is_symlink() or not path.is_file():
                return False
            size = path.stat(follow_symlinks=False).st_size
            if size <= 0 or size > MAX_RESUME_ARTIFACT_BYTES:
                return False
            content = path.read_bytes()
        except (OSError, PrivateHomeError):
            return False
        return (
            hashlib.sha256(content).hexdigest()
            == candidate.artifact_sha256
            and _validate_artifact_content(content, candidate.artifact_type)
        )

    def get(
        self,
        *,
        subject_id: str,
        resume_id: str,
    ) -> ResumeCandidateReadResult:
        path = self._record_path(subject_id, resume_id)
        with self._lock:
            if not path.exists():
                return ResumeCandidateReadResult(
                    status=ResumeCandidateReadStatus.NOT_FOUND,
                    candidate=None,
                )
            if path.is_symlink() or not path.is_file():
                return ResumeCandidateReadResult(
                    status=ResumeCandidateReadStatus.INTEGRITY_FAILURE,
                    candidate=None,
                    reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
                )
            try:
                candidate = _candidate_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ResumeCandidateReadResult(
                    status=ResumeCandidateReadStatus.INTEGRITY_FAILURE,
                    candidate=None,
                    reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
                )
            if (
                candidate.subject_id != subject_id.strip()
                or candidate.resume_id != resume_id
                or path.name != f"{candidate.resume_id}.json"
                or not self._artifact_is_valid(candidate)
            ):
                return ResumeCandidateReadResult(
                    status=ResumeCandidateReadStatus.INTEGRITY_FAILURE,
                    candidate=None,
                    reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
                )
            return ResumeCandidateReadResult(
                status=ResumeCandidateReadStatus.FOUND,
                candidate=candidate,
            )

    def list_selectable(self, subject_id: str) -> ResumeCandidateListResult:
        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        directory = self._subject_records(cleaned)
        if not directory.exists():
            return ResumeCandidateListResult(
                status=ResumeCandidateListStatus.SUCCEEDED,
                subject_id=cleaned,
                candidates=(),
            )
        if directory.is_symlink() or not directory.is_dir():
            return ResumeCandidateListResult(
                status=ResumeCandidateListStatus.FAILED,
                subject_id=cleaned,
                candidates=(),
                reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
            )
        try:
            paths = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
        except OSError:
            return ResumeCandidateListResult(
                status=ResumeCandidateListStatus.FAILED,
                subject_id=cleaned,
                candidates=(),
                reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
            )
        candidates: list[ResumeCandidate] = []
        for path in paths:
            if path.suffix != ".json" or _RESUME_ID_PATTERN.fullmatch(path.stem) is None:
                return ResumeCandidateListResult(
                    status=ResumeCandidateListStatus.FAILED,
                    subject_id=cleaned,
                    candidates=(),
                    reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
                )
            result = self.get(subject_id=cleaned, resume_id=path.stem)
            if (
                result.status is not ResumeCandidateReadStatus.FOUND
                or result.candidate is None
            ):
                return ResumeCandidateListResult(
                    status=ResumeCandidateListStatus.FAILED,
                    subject_id=cleaned,
                    candidates=(),
                    reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
                )
            if result.candidate.status is ResumeCandidateStatus.SELECTABLE:
                candidates.append(result.candidate)
        return ResumeCandidateListResult(
            status=ResumeCandidateListStatus.SUCCEEDED,
            subject_id=cleaned,
            candidates=tuple(sorted(candidates, key=lambda item: item.resume_id)),
        )

    def save(self, candidate: ResumeCandidate) -> ResumeCandidateWriteResult:
        if not isinstance(candidate, ResumeCandidate):
            raise TypeError("candidate must be a ResumeCandidate")
        path = self._record_path(candidate.subject_id, candidate.resume_id)
        with self._lock:
            if not self._artifact_is_valid(candidate):
                return ResumeCandidateWriteResult(
                    status=ResumeCandidateWriteStatus.FAILED,
                    candidate=None,
                    reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
                    retryable=False,
                )
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            candidate.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ResumeCandidateWriteResult(
                    status=ResumeCandidateWriteStatus.FAILED,
                    candidate=None,
                    reason_code=ResumeCandidateFailureReason.PERSISTENCE_FAILED,
                    retryable=True,
                )
            if created:
                return ResumeCandidateWriteResult(
                    status=ResumeCandidateWriteStatus.CREATED,
                    candidate=candidate,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=candidate.subject_id,
                resume_id=candidate.resume_id,
            )
            if (
                existing.status is ResumeCandidateReadStatus.FOUND
                and existing.candidate is not None
                and _semantic_content(existing.candidate)
                == _semantic_content(candidate)
            ):
                return ResumeCandidateWriteResult(
                    status=ResumeCandidateWriteStatus.UNCHANGED,
                    candidate=existing.candidate,
                    reason_code=None,
                    retryable=False,
                )
            return ResumeCandidateWriteResult(
                status=ResumeCandidateWriteStatus.FAILED,
                candidate=None,
                reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class RegisterResumeCandidateCommand:
    subject_id: str
    artifact_path: str | Path
    display_name: str
    selection_safe_summary: str
    summary_source: ResumeSummarySource
    summary_trust: ResumeSummaryTrust
    now: datetime
    claimed_artifact_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RegisterResumeCandidateResult:
    status: RegisterResumeCandidateStatus
    candidate: ResumeCandidate | None
    write_result: ResumeCandidateWriteResult | None
    reason_code: ResumeCandidateFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = RegisterResumeCandidateStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeCandidateFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            RegisterResumeCandidateStatus.CREATED,
            RegisterResumeCandidateStatus.UNCHANGED,
        }:
            expected = ResumeCandidateWriteStatus(status.value)
            if (
                not isinstance(self.candidate, ResumeCandidate)
                or not isinstance(self.write_result, ResumeCandidateWriteResult)
                or self.write_result.status is not expected
                or self.write_result.candidate != self.candidate
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful registration result is invalid")
        elif (
            self.candidate is not None
            or self.reason_code is None
        ):
            raise ValueError("failed registration result is invalid")


def _failure(
    reason: ResumeCandidateFailureReason,
    *,
    retryable: bool = False,
    write_result: ResumeCandidateWriteResult | None = None,
) -> RegisterResumeCandidateResult:
    return RegisterResumeCandidateResult(
        status=RegisterResumeCandidateStatus.FAILED,
        candidate=None,
        write_result=write_result,
        reason_code=reason,
        retryable=retryable,
        message=f"Resume candidate registration failed: {reason.value}.",
    )


def register_resume_candidate(
    command: RegisterResumeCandidateCommand,
    *,
    home: PrivateHome | None = None,
    repository: ResumeCandidateRepository | None = None,
) -> RegisterResumeCandidateResult:
    """Register one explicit managed artifact without scanning loose profile paths."""

    active_home = home or PrivateHome.discover()
    active_repository = repository or PrivateHomeResumeCandidateRepository(
        active_home
    )
    try:
        subject_id = _clean_text("subject_id", command.subject_id, maximum=160)
        display_name = _clean_text(
            "display_name",
            command.display_name,
            maximum=240,
        )
        summary = _clean_text(
            "selection_safe_summary",
            command.selection_safe_summary,
            maximum=MAX_SELECTION_SUMMARY_CHARS,
        )
        source = ResumeSummarySource(command.summary_source)
        trust = ResumeSummaryTrust(command.summary_trust)
        now = _require_aware("now", command.now)
        if source is not ResumeSummarySource.AUTHENTICATED_CALLER:
            return _failure(ResumeCandidateFailureReason.SUMMARY_UNTRUSTED)
        if trust not in {
            ResumeSummaryTrust.VERIFIED,
            ResumeSummaryTrust.USER_CONFIRMED,
        }:
            return _failure(ResumeCandidateFailureReason.SUMMARY_UNTRUSTED)
        active_home.ensure()
        source_path = active_home.contained_path(command.artifact_path)
    except (TypeError, ValueError):
        return _failure(ResumeCandidateFailureReason.INVALID_REQUEST)
    except PrivateHomeError:
        return _failure(ResumeCandidateFailureReason.ARTIFACT_UNMANAGED)
    try:
        source_path.relative_to(active_home.paths.master_documents)
    except ValueError:
        return _failure(ResumeCandidateFailureReason.ARTIFACT_UNMANAGED)

    if source_path.is_symlink() or not source_path.is_file():
        return _failure(ResumeCandidateFailureReason.ARTIFACT_UNMANAGED)
    try:
        size = source_path.stat(follow_symlinks=False).st_size
        if size <= 0 or size > MAX_RESUME_ARTIFACT_BYTES:
            return _failure(ResumeCandidateFailureReason.ARTIFACT_INVALID)
        content = source_path.read_bytes()
        artifact_type = _artifact_type_from_path(source_path, content)
    except (OSError, ValueError):
        return _failure(ResumeCandidateFailureReason.ARTIFACT_INVALID)
    artifact_hash = hashlib.sha256(content).hexdigest()
    claimed = command.claimed_artifact_sha256
    if claimed is not None and claimed != artifact_hash:
        return _failure(ResumeCandidateFailureReason.ARTIFACT_HASH_MISMATCH)

    reference = _artifact_reference(
        subject_id=subject_id,
        artifact_sha256=artifact_hash,
        artifact_type=artifact_type,
    )
    artifact_target = active_home.contained_path(reference)
    try:
        created_artifact = active_home.write_bytes_if_absent(
            artifact_target,
            content,
        )
        if not created_artifact:
            if (
                artifact_target.is_symlink()
                or not artifact_target.is_file()
                or artifact_target.read_bytes() != content
            ):
                return _failure(
                    ResumeCandidateFailureReason.INTEGRITY_FAILURE
                )
    except (OSError, PrivateHomeError):
        return _failure(
            ResumeCandidateFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )

    summary_hash = _summary_hash(summary)
    values = {
        "contract_version": RESUME_CANDIDATE_CONTRACT_VERSION,
        "subject_id": subject_id,
        "artifact_reference": reference,
        "artifact_sha256": artifact_hash,
        "artifact_type": artifact_type,
        "display_name": display_name,
        "selection_safe_summary": summary,
        "selection_safe_summary_sha256": summary_hash,
        "summary_source": source,
        "summary_trust": trust,
        "status": ResumeCandidateStatus.SELECTABLE,
    }
    candidate = ResumeCandidate(
        resume_id=_resume_id(**values),
        recorded_at=now,
        **values,
    )
    write_result = active_repository.save(candidate)
    if write_result.status is ResumeCandidateWriteStatus.FAILED:
        return _failure(
            write_result.reason_code
            or ResumeCandidateFailureReason.PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            write_result=write_result,
        )
    return RegisterResumeCandidateResult(
        status=RegisterResumeCandidateStatus(write_result.status.value),
        candidate=write_result.candidate,
        write_result=write_result,
        reason_code=None,
        retryable=False,
        message=(
            "Resume candidate registered."
            if write_result.status is ResumeCandidateWriteStatus.CREATED
            else "Resume candidate registration is unchanged."
        ),
    )


__all__ = [
    "MAX_RESUME_ARTIFACT_BYTES",
    "RESUME_CANDIDATE_CONTRACT_VERSION",
    "PrivateHomeResumeCandidateRepository",
    "RegisterResumeCandidateCommand",
    "RegisterResumeCandidateResult",
    "RegisterResumeCandidateStatus",
    "ResumeArtifactType",
    "ResumeCandidate",
    "ResumeCandidateFailureReason",
    "ResumeCandidateListResult",
    "ResumeCandidateListStatus",
    "ResumeCandidateProvider",
    "ResumeCandidateReadResult",
    "ResumeCandidateReadStatus",
    "ResumeCandidateRepository",
    "ResumeCandidateStatus",
    "ResumeCandidateWriteResult",
    "ResumeCandidateWriteStatus",
    "ResumeSummarySource",
    "ResumeSummaryTrust",
    "register_resume_candidate",
    "detect_resume_artifact_type",
]
