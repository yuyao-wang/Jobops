"""Subject-scoped evidence snapshots derived only from faithful resume source."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_candidates import (
    ResumeCandidate,
    ResumeCandidateReadStatus,
    ResumeCandidateRepository,
)
from .resume_selection import (
    ResumeSelectionDecision,
    ResumeSelectionDecisionReadStatus,
    ResumeSelectionDecisionRepository,
)
from .source_resume_projection import (
    SourceResumeBlock,
    SourceResumeLocator,
    SourceResumeLocatorKind,
    SourceResumeProjection,
    SourceResumeProjectionReadStatus,
    SourceResumeProjectionRepository,
)


CANDIDATE_EVIDENCE_CONTRACT_VERSION = "candidate-evidence-v1"
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_EVIDENCE_ID_PATTERN = re.compile(r"^candidate-evidence-[a-f0-9]{64}$")
_SNAPSHOT_ID_PATTERN = re.compile(
    r"^candidate-evidence-snapshot-[a-f0-9]{64}$"
)
_SECTION_ID_PATTERN = re.compile(r"^resume-section-[a-f0-9]{64}$")
_BLOCK_ID_PATTERN = re.compile(r"^resume-block-[a-f0-9]{64}$")
_BULLET_ID_PATTERN = re.compile(r"^resume-bullet-[a-f0-9]{64}$")


class CandidateEvidenceSourceType(str, Enum):
    SOURCE_RESUME_PROJECTION = "SOURCE_RESUME_PROJECTION"


class CandidateEvidenceSensitivity(str, Enum):
    PERSONAL = "PERSONAL"


class CandidateEvidenceScope(str, Enum):
    RESUME_TAILORING = "RESUME_TAILORING"


class CandidateEvidenceVerificationStatus(str, Enum):
    USER_PROVIDED_DOCUMENT_STATEMENT = "USER_PROVIDED_DOCUMENT_STATEMENT"


class CandidateEvidenceSnapshotWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class CandidateEvidenceSnapshotReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CandidateEvidenceSnapshotStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_NO_EVIDENCE = "DEFERRED_NO_EVIDENCE"
    FAILED = "FAILED"


class CandidateEvidenceFailureReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    RESUME_SELECTION_NOT_FOUND = "RESUME_SELECTION_NOT_FOUND"
    RESUME_SELECTION_INTEGRITY_FAILURE = (
        "RESUME_SELECTION_INTEGRITY_FAILURE"
    )
    RESUME_SELECTION_BINDING_MISMATCH = (
        "RESUME_SELECTION_BINDING_MISMATCH"
    )
    RESUME_CANDIDATE_NOT_FOUND = "RESUME_CANDIDATE_NOT_FOUND"
    RESUME_CANDIDATE_INTEGRITY_FAILURE = (
        "RESUME_CANDIDATE_INTEGRITY_FAILURE"
    )
    RESUME_CANDIDATE_BINDING_MISMATCH = (
        "RESUME_CANDIDATE_BINDING_MISMATCH"
    )
    SOURCE_PROJECTION_NOT_FOUND = "SOURCE_PROJECTION_NOT_FOUND"
    SOURCE_PROJECTION_INTEGRITY_FAILURE = (
        "SOURCE_PROJECTION_INTEGRITY_FAILURE"
    )
    SOURCE_PROJECTION_BINDING_MISMATCH = (
        "SOURCE_PROJECTION_BINDING_MISMATCH"
    )
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    SNAPSHOT_PERSISTENCE_FAILED = "SNAPSHOT_PERSISTENCE_FAILED"
    SNAPSHOT_INTEGRITY_FAILURE = "SNAPSHOT_INTEGRITY_FAILURE"


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the CandidateEvidence contract")
    return cleaned


def _evidence_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 20_000:
        raise ValueError("evidence text is outside the contract")
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


def _parse_optional_timestamp(value: Any, *, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return _require_aware(
        name,
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


def _evidence_identity_payload(
    *,
    subject_id: str,
    source_projection_id: str,
    source_projection_hash: str,
    source_anchor_id: str,
    contract_version: str,
) -> dict[str, Any]:
    return {
        "contract_version": contract_version,
        "source_anchor_id": source_anchor_id,
        "source_projection_hash": source_projection_hash,
        "source_projection_id": source_projection_id,
        "subject_id": subject_id,
    }


def candidate_evidence_id(
    *,
    subject_id: str,
    source_projection_id: str,
    source_projection_hash: str,
    source_anchor_id: str,
    contract_version: str = CANDIDATE_EVIDENCE_CONTRACT_VERSION,
) -> str:
    return (
        "candidate-evidence-"
        + _canonical_hash(
            _evidence_identity_payload(
                subject_id=subject_id,
                source_projection_id=source_projection_id,
                source_projection_hash=source_projection_hash,
                source_anchor_id=source_anchor_id,
                contract_version=contract_version,
            )
        )
    )


@dataclass(frozen=True, slots=True)
class CandidateEvidenceItem:
    evidence_id: str
    subject_id: str
    order: int
    evidence_text: str
    source_type: CandidateEvidenceSourceType
    source_projection_id: str
    source_projection_hash: str
    source_section_id: str
    source_block_id: str
    source_bullet_id: str | None
    source_locator: SourceResumeLocator
    sensitivity: CandidateEvidenceSensitivity
    allowed_scopes: tuple[CandidateEvidenceScope, ...]
    verification_status: CandidateEvidenceVerificationStatus
    recorded_at: datetime
    verified_at: datetime | None
    expires_at: datetime | None
    item_content_hash: str

    def __post_init__(self) -> None:
        subject_id = _clean_text(
            "subject_id", self.subject_id, maximum=160
        )
        if type(self.order) is not int or self.order < 0:
            raise ValueError("evidence order must be a non-negative integer")
        text = _evidence_text(self.evidence_text)
        source_type = CandidateEvidenceSourceType(self.source_type)
        projection_id = _clean_text(
            "source_projection_id",
            self.source_projection_id,
            maximum=160,
        )
        projection_hash = _require_hash(
            "source_projection_hash", self.source_projection_hash
        )
        section_id = _clean_text(
            "source_section_id", self.source_section_id, maximum=160
        )
        block_id = _clean_text(
            "source_block_id", self.source_block_id, maximum=160
        )
        if _SECTION_ID_PATTERN.fullmatch(section_id) is None:
            raise ValueError("source_section_id is invalid")
        if _BLOCK_ID_PATTERN.fullmatch(block_id) is None:
            raise ValueError("source_block_id is invalid")
        bullet_id = self.source_bullet_id
        if bullet_id is not None and (
            not isinstance(bullet_id, str)
            or _BULLET_ID_PATTERN.fullmatch(bullet_id) is None
        ):
            raise ValueError("source_bullet_id is invalid")
        if not isinstance(self.source_locator, SourceResumeLocator):
            raise TypeError("source_locator must be typed")
        sensitivity = CandidateEvidenceSensitivity(self.sensitivity)
        if (
            not isinstance(self.allowed_scopes, tuple)
            or tuple(
                CandidateEvidenceScope(item) for item in self.allowed_scopes
            )
            != (CandidateEvidenceScope.RESUME_TAILORING,)
        ):
            raise ValueError("source-resume evidence scope is invalid")
        verification = CandidateEvidenceVerificationStatus(
            self.verification_status
        )
        if (
            verification
            is not CandidateEvidenceVerificationStatus
            .USER_PROVIDED_DOCUMENT_STATEMENT
        ):
            raise ValueError("source-resume verification status is invalid")
        recorded_at = _require_aware("recorded_at", self.recorded_at)
        if self.verified_at is not None:
            raise ValueError(
                "user-provided document statements are not independently verified"
            )
        if self.expires_at is not None:
            _require_aware("expires_at", self.expires_at)
        anchor = bullet_id or block_id
        expected_id = candidate_evidence_id(
            subject_id=subject_id,
            source_projection_id=projection_id,
            source_projection_hash=projection_hash,
            source_anchor_id=anchor,
        )
        if (
            not isinstance(self.evidence_id, str)
            or _EVIDENCE_ID_PATTERN.fullmatch(self.evidence_id) is None
            or self.evidence_id != expected_id
        ):
            raise ValueError("evidence_id does not match source binding")
        object.__setattr__(self, "evidence_text", text)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_projection_id", projection_id)
        object.__setattr__(self, "source_projection_hash", projection_hash)
        object.__setattr__(self, "source_section_id", section_id)
        object.__setattr__(self, "source_block_id", block_id)
        object.__setattr__(self, "sensitivity", sensitivity)
        object.__setattr__(
            self,
            "allowed_scopes",
            (CandidateEvidenceScope.RESUME_TAILORING,),
        )
        object.__setattr__(self, "verification_status", verification)
        object.__setattr__(self, "recorded_at", recorded_at)
        item_hash = _require_hash(
            "item_content_hash", self.item_content_hash
        )
        if item_hash != _canonical_hash(self.content_dict()):
            raise ValueError("evidence item content hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "subject_id": self.subject_id,
            "order": self.order,
            "evidence_text": self.evidence_text,
            "source_type": self.source_type.value,
            "source_projection_id": self.source_projection_id,
            "source_projection_hash": self.source_projection_hash,
            "source_section_id": self.source_section_id,
            "source_block_id": self.source_block_id,
            "source_bullet_id": self.source_bullet_id,
            "source_locator": self.source_locator.to_dict(),
            "sensitivity": self.sensitivity.value,
            "allowed_scopes": [
                item.value for item in self.allowed_scopes
            ],
            "verification_status": self.verification_status.value,
            "verified_at": (
                _rfc3339(self.verified_at) if self.verified_at else None
            ),
            "expires_at": (
                _rfc3339(self.expires_at) if self.expires_at else None
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "recorded_at": _rfc3339(self.recorded_at),
            "item_content_hash": self.item_content_hash,
        }


def _create_evidence_item(
    *,
    subject_id: str,
    projection: SourceResumeProjection,
    section_id: str,
    block: SourceResumeBlock,
    order: int,
) -> CandidateEvidenceItem:
    anchor = block.bullet_id or block.block_id
    evidence_id = candidate_evidence_id(
        subject_id=subject_id,
        source_projection_id=projection.projection_id,
        source_projection_hash=projection.projection_content_hash,
        source_anchor_id=anchor,
    )
    content = {
        "evidence_id": evidence_id,
        "subject_id": subject_id,
        "order": order,
        "evidence_text": block.text,
        "source_type": (
            CandidateEvidenceSourceType.SOURCE_RESUME_PROJECTION.value
        ),
        "source_projection_id": projection.projection_id,
        "source_projection_hash": projection.projection_content_hash,
        "source_section_id": section_id,
        "source_block_id": block.block_id,
        "source_bullet_id": block.bullet_id,
        "source_locator": block.locator.to_dict(),
        "sensitivity": CandidateEvidenceSensitivity.PERSONAL.value,
        "allowed_scopes": [
            CandidateEvidenceScope.RESUME_TAILORING.value
        ],
        "verification_status": (
            CandidateEvidenceVerificationStatus
            .USER_PROVIDED_DOCUMENT_STATEMENT.value
        ),
        "verified_at": None,
        "expires_at": None,
    }
    return CandidateEvidenceItem(
        evidence_id=evidence_id,
        subject_id=subject_id,
        order=order,
        evidence_text=block.text,
        source_type=(
            CandidateEvidenceSourceType.SOURCE_RESUME_PROJECTION
        ),
        source_projection_id=projection.projection_id,
        source_projection_hash=projection.projection_content_hash,
        source_section_id=section_id,
        source_block_id=block.block_id,
        source_bullet_id=block.bullet_id,
        source_locator=block.locator,
        sensitivity=CandidateEvidenceSensitivity.PERSONAL,
        allowed_scopes=(
            CandidateEvidenceScope.RESUME_TAILORING,
        ),
        verification_status=(
            CandidateEvidenceVerificationStatus
            .USER_PROVIDED_DOCUMENT_STATEMENT
        ),
        recorded_at=projection.projected_at,
        verified_at=None,
        expires_at=None,
        item_content_hash=_canonical_hash(content),
    )


def _snapshot_identity_payload(
    *,
    contract_version: str,
    subject_id: str,
    application_plan_id: str,
    job_id: str,
    resume_selection_decision_id: str,
    source_resume_id: str,
    source_artifact_sha256: str,
    source_projection_id: str,
    source_projection_hash: str,
    evidence_item_hashes: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "application_plan_id": application_plan_id,
        "contract_version": contract_version,
        "evidence_item_hashes": list(evidence_item_hashes),
        "job_id": job_id,
        "resume_selection_decision_id": resume_selection_decision_id,
        "source_artifact_sha256": source_artifact_sha256,
        "source_projection_hash": source_projection_hash,
        "source_projection_id": source_projection_id,
        "source_resume_id": source_resume_id,
        "subject_id": subject_id,
    }


def candidate_evidence_snapshot_id(**values: Any) -> str:
    return "candidate-evidence-snapshot-" + _canonical_hash(
        _snapshot_identity_payload(**values)
    )


@dataclass(frozen=True, slots=True)
class CandidateEvidenceSnapshot:
    snapshot_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    resume_selection_decision_id: str
    source_resume_id: str
    source_artifact_sha256: str
    source_projection_id: str
    source_projection_hash: str
    evidence_items: tuple[CandidateEvidenceItem, ...]
    snapshot_content_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != CANDIDATE_EVIDENCE_CONTRACT_VERSION:
            raise ValueError("CandidateEvidence contract version is unsupported")
        subject = _clean_text("subject_id", self.subject_id, maximum=160)
        plan_id = _clean_text(
            "application_plan_id", self.application_plan_id, maximum=160
        )
        job_id = _clean_text("job_id", self.job_id, maximum=160)
        decision_id = _clean_text(
            "resume_selection_decision_id",
            self.resume_selection_decision_id,
            maximum=160,
        )
        resume_id = _clean_text(
            "source_resume_id", self.source_resume_id, maximum=160
        )
        artifact_hash = _require_hash(
            "source_artifact_sha256", self.source_artifact_sha256
        )
        projection_id = _clean_text(
            "source_projection_id", self.source_projection_id, maximum=160
        )
        projection_hash = _require_hash(
            "source_projection_hash", self.source_projection_hash
        )
        if (
            not isinstance(self.evidence_items, tuple)
            or not self.evidence_items
            or any(
            not isinstance(item, CandidateEvidenceItem)
            for item in self.evidence_items
            )
        ):
            raise TypeError("evidence_items must be a non-empty typed tuple")
        if tuple(item.order for item in self.evidence_items) != tuple(
            range(len(self.evidence_items))
        ):
            raise ValueError("evidence items must have contiguous order")
        evidence_ids = [item.evidence_id for item in self.evidence_items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        if any(
            item.subject_id != subject
            or item.source_projection_id != projection_id
            or item.source_projection_hash != projection_hash
            for item in self.evidence_items
        ):
            raise ValueError("evidence item snapshot binding is invalid")
        expected_id = candidate_evidence_snapshot_id(
            contract_version=contract,
            subject_id=subject,
            application_plan_id=plan_id,
            job_id=job_id,
            resume_selection_decision_id=decision_id,
            source_resume_id=resume_id,
            source_artifact_sha256=artifact_hash,
            source_projection_id=projection_id,
            source_projection_hash=projection_hash,
            evidence_item_hashes=tuple(
                item.item_content_hash for item in self.evidence_items
            ),
        )
        if (
            not isinstance(self.snapshot_id, str)
            or _SNAPSHOT_ID_PATTERN.fullmatch(self.snapshot_id) is None
            or self.snapshot_id != expected_id
        ):
            raise ValueError("snapshot_id does not match its binding")
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "subject_id", subject)
        object.__setattr__(self, "application_plan_id", plan_id)
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(
            self, "resume_selection_decision_id", decision_id
        )
        object.__setattr__(self, "source_resume_id", resume_id)
        object.__setattr__(
            self, "source_artifact_sha256", artifact_hash
        )
        object.__setattr__(self, "source_projection_id", projection_id)
        object.__setattr__(
            self, "source_projection_hash", projection_hash
        )
        _require_aware("created_at", self.created_at)
        content_hash = _require_hash(
            "snapshot_content_hash", self.snapshot_content_hash
        )
        if content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("snapshot content hash is invalid")
        object.__setattr__(self, "snapshot_content_hash", content_hash)

    def content_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "contract_version": self.contract_version,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "job_id": self.job_id,
            "resume_selection_decision_id": (
                self.resume_selection_decision_id
            ),
            "source_resume_id": self.source_resume_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_projection_id": self.source_projection_id,
            "source_projection_hash": self.source_projection_hash,
            "evidence_items": [
                item.to_dict() for item in self.evidence_items
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "snapshot_content_hash": self.snapshot_content_hash,
            "created_at": _rfc3339(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class CandidateEvidenceSnapshotWriteResult:
    status: CandidateEvidenceSnapshotWriteStatus
    snapshot: CandidateEvidenceSnapshot | None
    reason_code: CandidateEvidenceFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = CandidateEvidenceSnapshotWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CandidateEvidenceFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            CandidateEvidenceSnapshotWriteStatus.CREATED,
            CandidateEvidenceSnapshotWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.snapshot, CandidateEvidenceSnapshot)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful snapshot write result is invalid")
        elif self.snapshot is not None or self.reason_code is None:
            raise ValueError("failed snapshot write result is invalid")


@dataclass(frozen=True, slots=True)
class CandidateEvidenceSnapshotReadResult:
    status: CandidateEvidenceSnapshotReadStatus
    snapshot: CandidateEvidenceSnapshot | None
    reason_code: CandidateEvidenceFailureReason | None = None

    def __post_init__(self) -> None:
        status = CandidateEvidenceSnapshotReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CandidateEvidenceFailureReason(self.reason_code),
            )
        if status is CandidateEvidenceSnapshotReadStatus.FOUND:
            if (
                not isinstance(self.snapshot, CandidateEvidenceSnapshot)
                or self.reason_code is not None
            ):
                raise ValueError("found snapshot read result is invalid")
        elif status is CandidateEvidenceSnapshotReadStatus.NOT_FOUND:
            if self.snapshot is not None or self.reason_code is not None:
                raise ValueError("not-found snapshot read result is invalid")
        elif (
            self.snapshot is not None
            or self.reason_code
            is not CandidateEvidenceFailureReason.SNAPSHOT_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure snapshot read is invalid")


@runtime_checkable
class CandidateEvidenceSnapshotRepository(Protocol):
    def save(
        self, snapshot: CandidateEvidenceSnapshot
    ) -> CandidateEvidenceSnapshotWriteResult:
        """Persist one immutable evidence snapshot."""

    def get(
        self, *, subject_id: str, snapshot_id: str
    ) -> CandidateEvidenceSnapshotReadResult:
        """Read one subject-owned evidence snapshot."""


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
        raise ValueError("persisted evidence locator is invalid")
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


def _item_from_dict(value: Any, *, subject_id: str) -> CandidateEvidenceItem:
    expected = {
        "evidence_id",
        "subject_id",
        "order",
        "evidence_text",
        "source_type",
        "source_projection_id",
        "source_projection_hash",
        "source_section_id",
        "source_block_id",
        "source_bullet_id",
        "source_locator",
        "sensitivity",
        "allowed_scopes",
        "verification_status",
        "verified_at",
        "expires_at",
        "recorded_at",
        "item_content_hash",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["allowed_scopes"], list)
    ):
        raise ValueError("persisted CandidateEvidenceItem is invalid")
    if value["subject_id"] != subject_id:
        raise ValueError("persisted evidence subject is invalid")
    return CandidateEvidenceItem(
        evidence_id=value["evidence_id"],
        subject_id=value["subject_id"],
        order=value["order"],
        evidence_text=value["evidence_text"],
        source_type=CandidateEvidenceSourceType(value["source_type"]),
        source_projection_id=value["source_projection_id"],
        source_projection_hash=value["source_projection_hash"],
        source_section_id=value["source_section_id"],
        source_block_id=value["source_block_id"],
        source_bullet_id=value["source_bullet_id"],
        source_locator=_locator_from_dict(value["source_locator"]),
        sensitivity=CandidateEvidenceSensitivity(value["sensitivity"]),
        allowed_scopes=tuple(
            CandidateEvidenceScope(scope)
            for scope in value["allowed_scopes"]
        ),
        verification_status=CandidateEvidenceVerificationStatus(
            value["verification_status"]
        ),
        recorded_at=_parse_optional_timestamp(
            value["recorded_at"], name="recorded_at"
        ),
        verified_at=_parse_optional_timestamp(
            value["verified_at"], name="verified_at"
        ),
        expires_at=_parse_optional_timestamp(
            value["expires_at"], name="expires_at"
        ),
        item_content_hash=value["item_content_hash"],
    )


def _snapshot_from_dict(value: Any) -> CandidateEvidenceSnapshot:
    expected = {
        "snapshot_id",
        "contract_version",
        "subject_id",
        "application_plan_id",
        "job_id",
        "resume_selection_decision_id",
        "source_resume_id",
        "source_artifact_sha256",
        "source_projection_id",
        "source_projection_hash",
        "evidence_items",
        "snapshot_content_hash",
        "created_at",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["evidence_items"], list)
    ):
        raise ValueError("persisted CandidateEvidenceSnapshot is invalid")
    return CandidateEvidenceSnapshot(
        snapshot_id=value["snapshot_id"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        resume_selection_decision_id=value[
            "resume_selection_decision_id"
        ],
        source_resume_id=value["source_resume_id"],
        source_artifact_sha256=value["source_artifact_sha256"],
        source_projection_id=value["source_projection_id"],
        source_projection_hash=value["source_projection_hash"],
        evidence_items=tuple(
            _item_from_dict(item, subject_id=value["subject_id"])
            for item in value["evidence_items"]
        ),
        snapshot_content_hash=value["snapshot_content_hash"],
        created_at=_parse_optional_timestamp(
            value["created_at"], name="created_at"
        ),
    )


class PrivateHomeCandidateEvidenceSnapshotRepository:
    """Immutable, subject-scoped evidence snapshots in Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, snapshot_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(snapshot_id, str)
            or _SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None
        ):
            raise ValueError("snapshot_id is invalid")
        return (
            self._home.paths.candidate_evidence_snapshots
            / _subject_storage_key(subject)
            / f"{snapshot_id}.json"
        )

    def get(
        self, *, subject_id: str, snapshot_id: str
    ) -> CandidateEvidenceSnapshotReadResult:
        path = self._path(subject_id, snapshot_id)
        with self._lock:
            if not path.exists():
                return CandidateEvidenceSnapshotReadResult(
                    status=CandidateEvidenceSnapshotReadStatus.NOT_FOUND,
                    snapshot=None,
                )
            if path.is_symlink() or not path.is_file():
                return CandidateEvidenceSnapshotReadResult(
                    status=(
                        CandidateEvidenceSnapshotReadStatus.INTEGRITY_FAILURE
                    ),
                    snapshot=None,
                    reason_code=(
                        CandidateEvidenceFailureReason
                        .SNAPSHOT_INTEGRITY_FAILURE
                    ),
                )
            try:
                snapshot = _snapshot_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return CandidateEvidenceSnapshotReadResult(
                    status=(
                        CandidateEvidenceSnapshotReadStatus.INTEGRITY_FAILURE
                    ),
                    snapshot=None,
                    reason_code=(
                        CandidateEvidenceFailureReason
                        .SNAPSHOT_INTEGRITY_FAILURE
                    ),
                )
            if (
                snapshot.subject_id != subject_id.strip()
                or snapshot.snapshot_id != snapshot_id
                or path.name != f"{snapshot.snapshot_id}.json"
            ):
                return CandidateEvidenceSnapshotReadResult(
                    status=(
                        CandidateEvidenceSnapshotReadStatus.INTEGRITY_FAILURE
                    ),
                    snapshot=None,
                    reason_code=(
                        CandidateEvidenceFailureReason
                        .SNAPSHOT_INTEGRITY_FAILURE
                    ),
                )
            return CandidateEvidenceSnapshotReadResult(
                status=CandidateEvidenceSnapshotReadStatus.FOUND,
                snapshot=snapshot,
            )

    def save(
        self, snapshot: CandidateEvidenceSnapshot
    ) -> CandidateEvidenceSnapshotWriteResult:
        if not isinstance(snapshot, CandidateEvidenceSnapshot):
            raise TypeError("snapshot must be a CandidateEvidenceSnapshot")
        path = self._path(snapshot.subject_id, snapshot.snapshot_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            snapshot.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return CandidateEvidenceSnapshotWriteResult(
                    status=CandidateEvidenceSnapshotWriteStatus.FAILED,
                    snapshot=None,
                    reason_code=(
                        CandidateEvidenceFailureReason
                        .SNAPSHOT_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return CandidateEvidenceSnapshotWriteResult(
                    status=CandidateEvidenceSnapshotWriteStatus.CREATED,
                    snapshot=snapshot,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=snapshot.subject_id,
                snapshot_id=snapshot.snapshot_id,
            )
            if (
                existing.status is CandidateEvidenceSnapshotReadStatus.FOUND
                and existing.snapshot is not None
                and existing.snapshot.content_dict()
                == snapshot.content_dict()
            ):
                return CandidateEvidenceSnapshotWriteResult(
                    status=CandidateEvidenceSnapshotWriteStatus.UNCHANGED,
                    snapshot=existing.snapshot,
                    reason_code=None,
                    retryable=False,
                )
            return CandidateEvidenceSnapshotWriteResult(
                status=CandidateEvidenceSnapshotWriteStatus.FAILED,
                snapshot=None,
                reason_code=(
                    CandidateEvidenceFailureReason
                    .SNAPSHOT_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class CreateCandidateEvidenceSnapshotCommand:
    subject_id: str
    application_plan_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class CreateCandidateEvidenceSnapshotResult:
    status: CandidateEvidenceSnapshotStatus
    subject_id: str
    application_plan_id: str
    snapshot: CandidateEvidenceSnapshot | None
    write_result: CandidateEvidenceSnapshotWriteResult | None
    reason_code: CandidateEvidenceFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = CandidateEvidenceSnapshotStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CandidateEvidenceFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            CandidateEvidenceSnapshotStatus.CREATED,
            CandidateEvidenceSnapshotStatus.UNCHANGED,
        }:
            expected = CandidateEvidenceSnapshotWriteStatus(status.value)
            if (
                not isinstance(self.snapshot, CandidateEvidenceSnapshot)
                or not isinstance(
                    self.write_result,
                    CandidateEvidenceSnapshotWriteResult,
                )
                or self.write_result.status is not expected
                or self.write_result.snapshot != self.snapshot
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful evidence result is invalid")
        elif status is CandidateEvidenceSnapshotStatus.DEFERRED_NO_EVIDENCE:
            if (
                self.snapshot is not None
                or self.write_result is not None
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("deferred evidence result is invalid")
        elif self.snapshot is not None or self.reason_code is None:
            raise ValueError("failed evidence result is invalid")


def _failure(
    command: CreateCandidateEvidenceSnapshotCommand,
    reason: CandidateEvidenceFailureReason,
    *,
    retryable: bool = False,
    write_result: CandidateEvidenceSnapshotWriteResult | None = None,
) -> CreateCandidateEvidenceSnapshotResult:
    return CreateCandidateEvidenceSnapshotResult(
        status=CandidateEvidenceSnapshotStatus.FAILED,
        subject_id=(
            command.subject_id
            if isinstance(command.subject_id, str)
            else ""
        ),
        application_plan_id=(
            command.application_plan_id
            if isinstance(command.application_plan_id, str)
            else ""
        ),
        snapshot=None,
        write_result=write_result,
        reason_code=reason,
        retryable=retryable,
        message=f"Candidate evidence snapshot failed: {reason.value}.",
    )


def _projection_content_is_valid(
    projection: SourceResumeProjection,
) -> bool:
    try:
        return (
            _canonical_hash(projection.content_dict())
            == projection.projection_content_hash
        )
    except (AttributeError, TypeError, ValueError):
        return False


def create_candidate_evidence_snapshot(
    command: CreateCandidateEvidenceSnapshotCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    selection_repository: ResumeSelectionDecisionRepository,
    candidate_repository: ResumeCandidateRepository,
    projection_repository: SourceResumeProjectionRepository,
    snapshot_repository: CandidateEvidenceSnapshotRepository,
) -> CreateCandidateEvidenceSnapshotResult:
    """Build one immutable evidence snapshot without inferring candidate facts."""

    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        plan_id = _clean_text(
            "application_plan_id",
            command.application_plan_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, CandidateEvidenceFailureReason.INVALID_REQUEST
        )
    try:
        plan_result = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_result.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command, CandidateEvidenceFailureReason.APPLICATION_PLAN_NOT_FOUND
        )
    if (
        plan_result.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_result.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_result.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
        )
    try:
        selection_result = selection_repository.find_current_for_plan(
            subject_id=subject_id,
            application_plan_id=plan_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .RESUME_SELECTION_INTEGRITY_FAILURE,
        )
    if selection_result.status is ResumeSelectionDecisionReadStatus.NOT_FOUND:
        return _failure(
            command,
            CandidateEvidenceFailureReason.RESUME_SELECTION_NOT_FOUND,
        )
    if (
        selection_result.status is not ResumeSelectionDecisionReadStatus.FOUND
        or not isinstance(
            selection_result.decision, ResumeSelectionDecision
        )
    ):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .RESUME_SELECTION_INTEGRITY_FAILURE,
        )
    selection = selection_result.decision
    if (
        selection.subject_id != subject_id
        or selection.application_plan_id != plan.plan_id
        or selection.job_id != plan.job_id
        or selection.job_revision != plan.job_revision
        or selection.job_content_hash != plan.job_content_hash
    ):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .RESUME_SELECTION_BINDING_MISMATCH,
        )
    try:
        candidate_result = candidate_repository.get(
            subject_id=subject_id,
            resume_id=selection.source_resume_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .RESUME_CANDIDATE_INTEGRITY_FAILURE,
        )
    if candidate_result.status is ResumeCandidateReadStatus.NOT_FOUND:
        return _failure(
            command,
            CandidateEvidenceFailureReason.RESUME_CANDIDATE_NOT_FOUND,
        )
    if (
        candidate_result.status is not ResumeCandidateReadStatus.FOUND
        or not isinstance(candidate_result.candidate, ResumeCandidate)
    ):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .RESUME_CANDIDATE_INTEGRITY_FAILURE,
        )
    candidate = candidate_result.candidate
    if (
        candidate.subject_id != subject_id
        or candidate.resume_id != selection.source_resume_id
        or candidate.contract_version != selection.source_candidate_version
        or candidate.artifact_sha256 != selection.source_artifact_sha256
    ):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .RESUME_CANDIDATE_BINDING_MISMATCH,
        )
    try:
        projection_result = projection_repository.find_current_for_resume(
            subject_id=subject_id,
            resume_id=candidate.resume_id,
            artifact_sha256=candidate.artifact_sha256,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .SOURCE_PROJECTION_INTEGRITY_FAILURE,
        )
    if projection_result.status is SourceResumeProjectionReadStatus.NOT_FOUND:
        return _failure(
            command,
            CandidateEvidenceFailureReason.SOURCE_PROJECTION_NOT_FOUND,
        )
    if (
        projection_result.status is not SourceResumeProjectionReadStatus.FOUND
        or not isinstance(
            projection_result.projection, SourceResumeProjection
        )
    ):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .SOURCE_PROJECTION_INTEGRITY_FAILURE,
        )
    projection = projection_result.projection
    if (
        projection.subject_id != subject_id
        or projection.resume_id != candidate.resume_id
        or projection.artifact_sha256 != candidate.artifact_sha256
        or projection.artifact_type is not candidate.artifact_type
        or not _projection_content_is_valid(projection)
    ):
        return _failure(
            command,
            CandidateEvidenceFailureReason
            .SOURCE_PROJECTION_BINDING_MISMATCH,
        )
    try:
        items = tuple(
            _create_evidence_item(
                subject_id=subject_id,
                projection=projection,
                section_id=section.section_id,
                block=block,
                order=order,
            )
            for order, (section, block) in enumerate(
                (section, block)
                for section in projection.sections
                for block in section.blocks
                if block.text.strip()
            )
        )
        evidence_ids = [item.evidence_id for item in items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("duplicate evidence ID")
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, CandidateEvidenceFailureReason.EVIDENCE_INVALID
        )
    if not items:
        return CreateCandidateEvidenceSnapshotResult(
            status=CandidateEvidenceSnapshotStatus.DEFERRED_NO_EVIDENCE,
            subject_id=subject_id,
            application_plan_id=plan_id,
            snapshot=None,
            write_result=None,
            reason_code=None,
            retryable=False,
            message="The source resume projection contains no usable evidence.",
        )
    identity = {
        "contract_version": CANDIDATE_EVIDENCE_CONTRACT_VERSION,
        "subject_id": subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "resume_selection_decision_id": selection.decision_id,
        "source_resume_id": candidate.resume_id,
        "source_artifact_sha256": candidate.artifact_sha256,
        "source_projection_id": projection.projection_id,
        "source_projection_hash": projection.projection_content_hash,
        "evidence_item_hashes": tuple(
            item.item_content_hash for item in items
        ),
    }
    snapshot_id = candidate_evidence_snapshot_id(**identity)
    try:
        existing = snapshot_repository.get(
            subject_id=subject_id,
            snapshot_id=snapshot_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CandidateEvidenceFailureReason.SNAPSHOT_INTEGRITY_FAILURE,
        )
    if existing.status is CandidateEvidenceSnapshotReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            CandidateEvidenceFailureReason.SNAPSHOT_INTEGRITY_FAILURE,
        )
    if (
        existing.status is CandidateEvidenceSnapshotReadStatus.FOUND
        and existing.snapshot is not None
    ):
        write_result = CandidateEvidenceSnapshotWriteResult(
            status=CandidateEvidenceSnapshotWriteStatus.UNCHANGED,
            snapshot=existing.snapshot,
            reason_code=None,
            retryable=False,
        )
        return CreateCandidateEvidenceSnapshotResult(
            status=CandidateEvidenceSnapshotStatus.UNCHANGED,
            subject_id=subject_id,
            application_plan_id=plan_id,
            snapshot=existing.snapshot,
            write_result=write_result,
            reason_code=None,
            retryable=False,
            message="The existing CandidateEvidence snapshot is unchanged.",
        )
    snapshot_content = {
        "snapshot_id": snapshot_id,
        **{
            key: value
            for key, value in identity.items()
            if key != "evidence_item_hashes"
        },
        "evidence_items": [item.to_dict() for item in items],
    }
    snapshot = CandidateEvidenceSnapshot(
        snapshot_id=snapshot_id,
        contract_version=CANDIDATE_EVIDENCE_CONTRACT_VERSION,
        subject_id=subject_id,
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        resume_selection_decision_id=selection.decision_id,
        source_resume_id=candidate.resume_id,
        source_artifact_sha256=candidate.artifact_sha256,
        source_projection_id=projection.projection_id,
        source_projection_hash=projection.projection_content_hash,
        evidence_items=items,
        snapshot_content_hash=_canonical_hash(snapshot_content),
        created_at=now,
    )
    try:
        write_result = snapshot_repository.save(snapshot)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CandidateEvidenceFailureReason.SNAPSHOT_PERSISTENCE_FAILED,
            retryable=True,
        )
    if write_result.status is CandidateEvidenceSnapshotWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or CandidateEvidenceFailureReason.SNAPSHOT_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            write_result=write_result,
        )
    result_status = CandidateEvidenceSnapshotStatus(
        write_result.status.value
    )
    return CreateCandidateEvidenceSnapshotResult(
        status=result_status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        snapshot=write_result.snapshot,
        write_result=write_result,
        reason_code=None,
        retryable=False,
        message=(
            "The CandidateEvidence snapshot was created."
            if result_status is CandidateEvidenceSnapshotStatus.CREATED
            else "The existing CandidateEvidence snapshot is unchanged."
        ),
    )


__all__ = [
    "CANDIDATE_EVIDENCE_CONTRACT_VERSION",
    "CandidateEvidenceFailureReason",
    "CandidateEvidenceItem",
    "CandidateEvidenceScope",
    "CandidateEvidenceSensitivity",
    "CandidateEvidenceSnapshot",
    "CandidateEvidenceSnapshotReadResult",
    "CandidateEvidenceSnapshotReadStatus",
    "CandidateEvidenceSnapshotRepository",
    "CandidateEvidenceSnapshotStatus",
    "CandidateEvidenceSnapshotWriteResult",
    "CandidateEvidenceSnapshotWriteStatus",
    "CandidateEvidenceSourceType",
    "CandidateEvidenceVerificationStatus",
    "CreateCandidateEvidenceSnapshotCommand",
    "CreateCandidateEvidenceSnapshotResult",
    "PrivateHomeCandidateEvidenceSnapshotRepository",
    "candidate_evidence_id",
    "candidate_evidence_snapshot_id",
    "create_candidate_evidence_snapshot",
]
