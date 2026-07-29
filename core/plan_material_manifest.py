"""Plan-scoped manifest of finished application materials.

This is deliberately separate from the legacy job-directory ``MaterialManifest``
in :mod:`core.materials`, which remains untouched. That contract is tier and
job-directory centric; this one is bound to an immutable ``ApplicationPlan``
and references only materials the preparation chain already published.
"""

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
from .prepared_resume_material import (
    PreparedMaterialRole,
    PreparedResumeMaterial,
    PreparedResumeMaterialReadStatus,
    PreparedResumeMaterialRepository,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_compilation import pdf_page_count


PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1 = "plan-material-manifest-v1"
PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2 = "plan-material-manifest-v2"
PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION = (
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2
)
_SUPPORTED_MANIFEST_CONTRACT_VERSIONS = {
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1,
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2,
}
RESUME_MEDIA_TYPE = "application/pdf"

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_MANIFEST_ID_PATTERN = re.compile(
    r"^plan-material-manifest-[a-f0-9]{64}$"
)
_ENTRY_ID_PATTERN = re.compile(r"^plan-material-entry-[a-f0-9]{64}$")


class PlanMaterialRole(str, Enum):
    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"


class PlanMaterialProvenanceType(str, Enum):
    PREPARED_RESUME_MATERIAL = "PREPARED_RESUME_MATERIAL"
    PREPARED_COVER_LETTER_MATERIAL = "PREPARED_COVER_LETTER_MATERIAL"


class PlanMaterialAssemblyState(str, Enum):
    """What this manifest actually contains — never a single ambiguous flag."""

    RESUME_ONLY = "RESUME_ONLY"
    RESUME_AND_COVER_LETTER = "RESUME_AND_COVER_LETTER"


class PlanMaterialManifestStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_READY = "NOT_READY"
    FAILED = "FAILED"


class PlanMaterialManifestWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class PlanMaterialManifestReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class PlanMaterialManifestNotReadyReason(str, Enum):
    PREPARED_RESUME_NOT_PUBLISHED = "PREPARED_RESUME_NOT_PUBLISHED"
    PREPARED_RESUME_PLAN_MISMATCH = "PREPARED_RESUME_PLAN_MISMATCH"
    PREPARED_RESUME_ROLE_MISMATCH = "PREPARED_RESUME_ROLE_MISMATCH"
    PLAN_MATERIAL_MANIFEST_NOT_READY = "PLAN_MATERIAL_MANIFEST_NOT_READY"
    PREPARED_COVER_LETTER_NOT_PUBLISHED = (
        "PREPARED_COVER_LETTER_NOT_PUBLISHED"
    )
    PREPARED_COVER_LETTER_PLAN_MISMATCH = (
        "PREPARED_COVER_LETTER_PLAN_MISMATCH"
    )
    PREPARED_COVER_LETTER_ROLE_MISMATCH = (
        "PREPARED_COVER_LETTER_ROLE_MISMATCH"
    )
    PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE = (
        "PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE"
    )


class PlanMaterialManifestFailureReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    PREPARED_RESUME_INTEGRITY_FAILURE = (
        "PREPARED_RESUME_INTEGRITY_FAILURE"
    )
    PREPARED_COVER_LETTER_INTEGRITY_FAILURE = (
        "PREPARED_COVER_LETTER_INTEGRITY_FAILURE"
    )
    ARTIFACT_UNREADABLE = "ARTIFACT_UNREADABLE"
    ARTIFACT_HASH_DRIFT = "ARTIFACT_HASH_DRIFT"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    MANIFEST_PERSISTENCE_FAILED = "MANIFEST_PERSISTENCE_FAILED"
    MANIFEST_INTEGRITY_FAILURE = "MANIFEST_INTEGRITY_FAILURE"


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the manifest contract")
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
        raise ValueError("assembled_at is invalid")
    return _require_aware(
        "assembled_at",
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


def prepared_material_content_hash(
    material: PreparedResumeMaterial,
) -> str:
    """Hash the published material's own content, without altering P2a9."""

    if not isinstance(material, PreparedResumeMaterial):
        raise TypeError("material must be a PreparedResumeMaterial")
    return _canonical_hash(material.content_dict())


@dataclass(frozen=True, slots=True)
class PlanMaterialEntry:
    entry_id: str
    order: int
    material_role: PlanMaterialRole
    prepared_material_id: str
    artifact_reference: str
    artifact_sha256: str
    media_type: str
    page_count: int
    provenance_type: PlanMaterialProvenanceType
    source_record_id: str
    source_record_hash: str
    artifact_byte_size: int | None = None
    contract_version: str = PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract not in _SUPPORTED_MANIFEST_CONTRACT_VERSIONS:
            raise ValueError("material entry contract is unsupported")
        object.__setattr__(self, "contract_version", contract)
        if type(self.order) is not int or self.order < 0:
            raise ValueError("entry order must be a non-negative integer")
        role = PlanMaterialRole(self.material_role)
        provenance = PlanMaterialProvenanceType(self.provenance_type)
        object.__setattr__(self, "material_role", role)
        object.__setattr__(self, "provenance_type", provenance)
        _clean_text(
            "prepared_material_id", self.prepared_material_id, maximum=160
        )
        _clean_text(
            "artifact_reference", self.artifact_reference, maximum=400
        )
        _require_hash("artifact_sha256", self.artifact_sha256)
        media = _clean_text("media_type", self.media_type, maximum=120)
        if role in {
            PlanMaterialRole.RESUME,
            PlanMaterialRole.COVER_LETTER,
        } and media != RESUME_MEDIA_TYPE:
            raise ValueError("document material entries must be PDFs")
        if type(self.page_count) is not int or self.page_count < 1:
            raise ValueError("page_count must be at least one")
        if contract == PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1:
            if self.artifact_byte_size is not None:
                raise ValueError(
                    "v1 material entries cannot carry artifact byte size"
                )
        elif (
            type(self.artifact_byte_size) is not int
            or self.artifact_byte_size < 1
        ):
            raise ValueError(
                "v2 material entries require positive artifact byte size"
            )
        _clean_text(
            "source_record_id", self.source_record_id, maximum=160
        )
        _require_hash("source_record_hash", self.source_record_hash)
        expected = plan_material_entry_id(self.content_dict())
        if (
            not isinstance(self.entry_id, str)
            or _ENTRY_ID_PATTERN.fullmatch(self.entry_id) is None
            or self.entry_id != expected
        ):
            raise ValueError("entry_id does not match its content")

    def content_dict(self) -> dict[str, Any]:
        content = {
            "artifact_reference": self.artifact_reference,
            "artifact_sha256": self.artifact_sha256,
            "material_role": self.material_role.value,
            "media_type": self.media_type,
            "order": self.order,
            "page_count": self.page_count,
            "prepared_material_id": self.prepared_material_id,
            "provenance_type": self.provenance_type.value,
            "source_record_hash": self.source_record_hash,
            "source_record_id": self.source_record_id,
        }
        if (
            self.contract_version
            == PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2
        ):
            content["artifact_byte_size"] = self.artifact_byte_size
        return content

    def to_dict(self) -> dict[str, Any]:
        return {"entry_id": self.entry_id, **self.content_dict()}

    @property
    def artifact_byte_size_available(self) -> bool:
        """Whether the persisted entry explicitly binds artifact size."""

        return self.artifact_byte_size is not None


def plan_material_entry_id(content: Mapping[str, Any]) -> str:
    return "plan-material-entry-" + _canonical_hash(content)


def _identity_payload(
    *,
    contract_version: str,
    subject_id: str,
    application_plan_id: str,
    job_id: str,
    job_revision: int,
    job_content_hash: str,
    prepared_resume_material_id: str,
    prepared_resume_material_hash: str,
    resume_artifact_sha256: str,
    entry_hashes: tuple[str, ...],
    assembly_state: PlanMaterialAssemblyState,
    artifact_byte_sizes: tuple[int, ...] | None = None,
    prior_manifest_id: str | None = None,
    prior_manifest_content_hash: str | None = None,
    prepared_cover_letter_material_id: str | None = None,
    prepared_cover_letter_material_hash: str | None = None,
    cover_letter_artifact_sha256: str | None = None,
    preserved_resume_entry_hash: str | None = None,
) -> dict[str, Any]:
    payload = {
        "application_plan_id": application_plan_id,
        "assembly_state": assembly_state.value,
        "contract_version": contract_version,
        "entry_hashes": list(entry_hashes),
        "job_content_hash": job_content_hash,
        "job_id": job_id,
        "job_revision": job_revision,
        "prepared_resume_material_hash": prepared_resume_material_hash,
        "prepared_resume_material_id": prepared_resume_material_id,
        "resume_artifact_sha256": resume_artifact_sha256,
        "subject_id": subject_id,
    }
    if contract_version == PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2:
        if artifact_byte_sizes is None:
            raise ValueError("v2 manifest identity requires artifact sizes")
        payload["artifact_byte_sizes"] = list(artifact_byte_sizes)
    elif artifact_byte_sizes is not None:
        raise ValueError("v1 manifest identity cannot carry artifact sizes")
    if assembly_state is PlanMaterialAssemblyState.RESUME_AND_COVER_LETTER:
        payload.update(
            {
                "cover_letter_artifact_sha256": (
                    cover_letter_artifact_sha256
                ),
                "prepared_cover_letter_material_hash": (
                    prepared_cover_letter_material_hash
                ),
                "prepared_cover_letter_material_id": (
                    prepared_cover_letter_material_id
                ),
                "preserved_resume_entry_hash": (
                    preserved_resume_entry_hash
                ),
                "prior_manifest_content_hash": (
                    prior_manifest_content_hash
                ),
                "prior_manifest_id": prior_manifest_id,
            }
        )
    return payload


def plan_material_manifest_id(**values: Any) -> str:
    return "plan-material-manifest-" + _canonical_hash(
        _identity_payload(**values)
    )


def plan_material_manifest_content_hash(
    content: Mapping[str, Any],
) -> str:
    """Return the canonical content hash used by persisted manifests."""

    return _canonical_hash(content)


@dataclass(frozen=True, slots=True)
class PlanMaterialManifest:
    manifest_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    prepared_resume_material_id: str
    prepared_resume_material_hash: str
    resume_artifact_sha256: str
    assembly_state: PlanMaterialAssemblyState
    included_roles: tuple[PlanMaterialRole, ...]
    entries: tuple[PlanMaterialEntry, ...]
    manifest_content_hash: str
    assembled_at: datetime
    prior_manifest_id: str | None = None
    prior_manifest_content_hash: str | None = None
    prepared_cover_letter_material_id: str | None = None
    prepared_cover_letter_material_hash: str | None = None
    cover_letter_artifact_sha256: str | None = None
    preserved_resume_entry_hash: str | None = None

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract not in _SUPPORTED_MANIFEST_CONTRACT_VERSIONS:
            raise ValueError("manifest contract is unsupported")
        subject = _clean_text("subject_id", self.subject_id, maximum=160)
        plan_id = _clean_text(
            "application_plan_id", self.application_plan_id, maximum=160
        )
        job_id = _clean_text("job_id", self.job_id, maximum=160)
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be a positive integer")
        job_hash = _require_hash("job_content_hash", self.job_content_hash)
        material_id = _clean_text(
            "prepared_resume_material_id",
            self.prepared_resume_material_id,
            maximum=160,
        )
        material_hash = _require_hash(
            "prepared_resume_material_hash",
            self.prepared_resume_material_hash,
        )
        artifact_hash = _require_hash(
            "resume_artifact_sha256", self.resume_artifact_sha256
        )
        state = PlanMaterialAssemblyState(self.assembly_state)
        object.__setattr__(self, "assembly_state", state)
        if (
            not isinstance(self.entries, tuple)
            or not self.entries
            or any(
                not isinstance(item, PlanMaterialEntry)
                for item in self.entries
            )
        ):
            raise TypeError("entries must be a non-empty typed tuple")
        if any(
            item.contract_version != contract for item in self.entries
        ):
            raise ValueError(
                "manifest and material entry contract versions differ"
            )
        if tuple(item.order for item in self.entries) != tuple(
            range(len(self.entries))
        ):
            raise ValueError("entries must have contiguous order")
        roles = tuple(item.material_role for item in self.entries)
        if len(roles) != len(set(roles)):
            raise ValueError("each material role may appear only once")
        declared = tuple(
            PlanMaterialRole(item) for item in self.included_roles
        )
        if declared != roles:
            raise ValueError("included_roles must match the entries")
        object.__setattr__(self, "included_roles", declared)
        if state is PlanMaterialAssemblyState.RESUME_ONLY and roles != (
            PlanMaterialRole.RESUME,
        ):
            raise ValueError(
                "a resume-only manifest carries exactly one resume entry"
            )
        resume = self.entries[0]
        if (
            resume.material_role is not PlanMaterialRole.RESUME
            or resume.provenance_type
            is not PlanMaterialProvenanceType.PREPARED_RESUME_MATERIAL
        ):
            raise ValueError("the first entry must be a prepared resume")
        if (
            resume.prepared_material_id != material_id
            or resume.source_record_hash != material_hash
            or resume.artifact_sha256 != artifact_hash
        ):
            raise ValueError("the resume entry does not match its binding")
        extension_values = (
            self.prior_manifest_id,
            self.prior_manifest_content_hash,
            self.prepared_cover_letter_material_id,
            self.prepared_cover_letter_material_hash,
            self.cover_letter_artifact_sha256,
            self.preserved_resume_entry_hash,
        )
        if state is PlanMaterialAssemblyState.RESUME_ONLY:
            if any(item is not None for item in extension_values):
                raise ValueError(
                    "resume-only serialization cannot carry "
                    "cover-letter fields"
                )
        else:
            if roles != (
                PlanMaterialRole.RESUME,
                PlanMaterialRole.COVER_LETTER,
            ):
                raise ValueError(
                    "the cover-letter assembly has exactly two ordered entries"
                )
            if any(item is None for item in extension_values):
                raise ValueError(
                    "the cover-letter assembly lineage is incomplete"
                )
            if (
                not isinstance(self.prior_manifest_id, str)
                or _MANIFEST_ID_PATTERN.fullmatch(self.prior_manifest_id)
                is None
            ):
                raise ValueError("prior_manifest_id is invalid")
            _require_hash(
                "prior_manifest_content_hash",
                self.prior_manifest_content_hash,
            )
            _clean_text(
                "prepared_cover_letter_material_id",
                self.prepared_cover_letter_material_id,
                maximum=160,
            )
            _require_hash(
                "prepared_cover_letter_material_hash",
                self.prepared_cover_letter_material_hash,
            )
            _require_hash(
                "cover_letter_artifact_sha256",
                self.cover_letter_artifact_sha256,
            )
            if self.preserved_resume_entry_hash != resume.entry_id:
                raise ValueError("the preserved resume entry hash is invalid")
            cover_letter = self.entries[1]
            if (
                cover_letter.provenance_type
                is not PlanMaterialProvenanceType
                .PREPARED_COVER_LETTER_MATERIAL
                or cover_letter.prepared_material_id
                != self.prepared_cover_letter_material_id
                or cover_letter.source_record_id
                != self.prepared_cover_letter_material_id
                or cover_letter.source_record_hash
                != self.prepared_cover_letter_material_hash
                or cover_letter.artifact_sha256
                != self.cover_letter_artifact_sha256
            ):
                raise ValueError(
                    "the cover-letter entry does not match its binding"
                )
        expected = plan_material_manifest_id(
            contract_version=contract,
            subject_id=subject,
            application_plan_id=plan_id,
            job_id=job_id,
            job_revision=self.job_revision,
            job_content_hash=job_hash,
            prepared_resume_material_id=material_id,
            prepared_resume_material_hash=material_hash,
            resume_artifact_sha256=artifact_hash,
            entry_hashes=tuple(item.entry_id for item in self.entries),
            artifact_byte_sizes=(
                tuple(
                    item.artifact_byte_size for item in self.entries
                )
                if contract
                == PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2
                else None
            ),
            assembly_state=state,
            prior_manifest_id=self.prior_manifest_id,
            prior_manifest_content_hash=self.prior_manifest_content_hash,
            prepared_cover_letter_material_id=(
                self.prepared_cover_letter_material_id
            ),
            prepared_cover_letter_material_hash=(
                self.prepared_cover_letter_material_hash
            ),
            cover_letter_artifact_sha256=(
                self.cover_letter_artifact_sha256
            ),
            preserved_resume_entry_hash=self.preserved_resume_entry_hash,
        )
        if (
            not isinstance(self.manifest_id, str)
            or _MANIFEST_ID_PATTERN.fullmatch(self.manifest_id) is None
            or self.manifest_id != expected
        ):
            raise ValueError("manifest_id does not match its binding")
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "subject_id", subject)
        _require_aware("assembled_at", self.assembled_at)
        content_hash = _require_hash(
            "manifest_content_hash", self.manifest_content_hash
        )
        if content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("manifest content hash is invalid")

    @property
    def resume_prepared(self) -> bool:
        """One formal resume material is assembled for this plan."""

        return PlanMaterialRole.RESUME in self.included_roles

    @property
    def complete_application_material_prepared(self) -> bool:
        """Whether every material a full application needs is assembled.

        A resume-only manifest is explicitly incomplete: cover letters and
        application answers are separate Slices and are never faked here.
        Approval Gate A is not represented by this contract at all.
        """

        return False

    def entry_for_role(
        self, role: PlanMaterialRole
    ) -> PlanMaterialEntry | None:
        wanted = PlanMaterialRole(role)
        for item in self.entries:
            if item.material_role is wanted:
                return item
        return None

    def content_dict(self) -> dict[str, Any]:
        content = {
            "manifest_id": self.manifest_id,
            "contract_version": self.contract_version,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "prepared_resume_material_id": (
                self.prepared_resume_material_id
            ),
            "prepared_resume_material_hash": (
                self.prepared_resume_material_hash
            ),
            "resume_artifact_sha256": self.resume_artifact_sha256,
            "assembly_state": self.assembly_state.value,
            "included_roles": [item.value for item in self.included_roles],
            "entries": [item.to_dict() for item in self.entries],
        }
        if (
            self.assembly_state
            is PlanMaterialAssemblyState.RESUME_AND_COVER_LETTER
        ):
            content.update(
                {
                    "prior_manifest_id": self.prior_manifest_id,
                    "prior_manifest_content_hash": (
                        self.prior_manifest_content_hash
                    ),
                    "prepared_cover_letter_material_id": (
                        self.prepared_cover_letter_material_id
                    ),
                    "prepared_cover_letter_material_hash": (
                        self.prepared_cover_letter_material_hash
                    ),
                    "cover_letter_artifact_sha256": (
                        self.cover_letter_artifact_sha256
                    ),
                    "preserved_resume_entry_hash": (
                        self.preserved_resume_entry_hash
                    ),
                }
            )
        return content

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "manifest_content_hash": self.manifest_content_hash,
            "assembled_at": _rfc3339(self.assembled_at),
        }


@dataclass(frozen=True, slots=True)
class PlanMaterialManifestWriteResult:
    status: PlanMaterialManifestWriteStatus
    manifest: PlanMaterialManifest | None
    reason_code: PlanMaterialManifestFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = PlanMaterialManifestWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PlanMaterialManifestFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            PlanMaterialManifestWriteStatus.CREATED,
            PlanMaterialManifestWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.manifest, PlanMaterialManifest)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful manifest write is invalid")
        elif self.manifest is not None or self.reason_code is None:
            raise ValueError("failed manifest write is invalid")


@dataclass(frozen=True, slots=True)
class PlanMaterialManifestReadResult:
    status: PlanMaterialManifestReadStatus
    manifest: PlanMaterialManifest | None
    reason_code: PlanMaterialManifestFailureReason | None = None

    def __post_init__(self) -> None:
        status = PlanMaterialManifestReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PlanMaterialManifestFailureReason(self.reason_code),
            )
        if status is PlanMaterialManifestReadStatus.FOUND:
            if (
                not isinstance(self.manifest, PlanMaterialManifest)
                or self.reason_code is not None
            ):
                raise ValueError("found manifest read is invalid")
        elif status is PlanMaterialManifestReadStatus.NOT_FOUND:
            if self.manifest is not None or self.reason_code is not None:
                raise ValueError("not-found manifest read is invalid")
        elif (
            self.manifest is not None
            or self.reason_code
            is not PlanMaterialManifestFailureReason
            .MANIFEST_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure manifest read is invalid")


@runtime_checkable
class PlanMaterialManifestRepository(Protocol):
    def save(
        self, manifest: PlanMaterialManifest
    ) -> PlanMaterialManifestWriteResult:
        """Persist one immutable plan-scoped manifest."""

    def get(
        self, *, subject_id: str, manifest_id: str
    ) -> PlanMaterialManifestReadResult:
        """Read one subject-owned manifest."""

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> PlanMaterialManifestReadResult:
        """Resolve the current manifest for one plan."""


def _entry_from_dict(
    value: Any, *, contract_version: str
) -> PlanMaterialEntry:
    expected = {
        "entry_id",
        "order",
        "material_role",
        "prepared_material_id",
        "artifact_reference",
        "artifact_sha256",
        "media_type",
        "page_count",
        "provenance_type",
        "source_record_id",
        "source_record_hash",
    }
    if contract_version == PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2:
        expected.add("artifact_byte_size")
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted PlanMaterialEntry is invalid")
    return PlanMaterialEntry(
        entry_id=value["entry_id"],
        order=value["order"],
        material_role=PlanMaterialRole(value["material_role"]),
        prepared_material_id=value["prepared_material_id"],
        artifact_reference=value["artifact_reference"],
        artifact_sha256=value["artifact_sha256"],
        media_type=value["media_type"],
        page_count=value["page_count"],
        provenance_type=PlanMaterialProvenanceType(
            value["provenance_type"]
        ),
        source_record_id=value["source_record_id"],
        source_record_hash=value["source_record_hash"],
        artifact_byte_size=value.get("artifact_byte_size"),
        contract_version=contract_version,
    )


def _manifest_from_dict(value: Any) -> PlanMaterialManifest:
    legacy_expected = {
        "manifest_id",
        "contract_version",
        "subject_id",
        "application_plan_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "prepared_resume_material_id",
        "prepared_resume_material_hash",
        "resume_artifact_sha256",
        "assembly_state",
        "included_roles",
        "entries",
        "manifest_content_hash",
        "assembled_at",
    }
    extension = {
        "prior_manifest_id",
        "prior_manifest_content_hash",
        "prepared_cover_letter_material_id",
        "prepared_cover_letter_material_hash",
        "cover_letter_artifact_sha256",
        "preserved_resume_entry_hash",
    }
    if not isinstance(value, Mapping):
        raise ValueError("persisted PlanMaterialManifest is invalid")
    contract_version = value.get("contract_version")
    if contract_version not in _SUPPORTED_MANIFEST_CONTRACT_VERSIONS:
        raise ValueError("persisted manifest contract is unsupported")
    assembly_state = value.get("assembly_state")
    expected = (
        legacy_expected
        if assembly_state == PlanMaterialAssemblyState.RESUME_ONLY.value
        else legacy_expected | extension
        if assembly_state
        == PlanMaterialAssemblyState.RESUME_AND_COVER_LETTER.value
        else set()
    )
    if (
        set(value) != expected
        or not isinstance(value["entries"], list)
        or not isinstance(value["included_roles"], list)
    ):
        raise ValueError("persisted PlanMaterialManifest is invalid")
    return PlanMaterialManifest(
        manifest_id=value["manifest_id"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        prepared_resume_material_id=value["prepared_resume_material_id"],
        prepared_resume_material_hash=value[
            "prepared_resume_material_hash"
        ],
        resume_artifact_sha256=value["resume_artifact_sha256"],
        assembly_state=PlanMaterialAssemblyState(value["assembly_state"]),
        included_roles=tuple(
            PlanMaterialRole(item) for item in value["included_roles"]
        ),
        entries=tuple(
            _entry_from_dict(item, contract_version=contract_version)
            for item in value["entries"]
        ),
        manifest_content_hash=value["manifest_content_hash"],
        assembled_at=_parse_timestamp(value["assembled_at"]),
        prior_manifest_id=value.get("prior_manifest_id"),
        prior_manifest_content_hash=value.get(
            "prior_manifest_content_hash"
        ),
        prepared_cover_letter_material_id=value.get(
            "prepared_cover_letter_material_id"
        ),
        prepared_cover_letter_material_hash=value.get(
            "prepared_cover_letter_material_hash"
        ),
        cover_letter_artifact_sha256=value.get(
            "cover_letter_artifact_sha256"
        ),
        preserved_resume_entry_hash=value.get(
            "preserved_resume_entry_hash"
        ),
    )


class PrivateHomePlanMaterialManifestRepository:
    """Immutable plan-scoped manifests with fail-closed artifact verification."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _subject_directory(self, subject_id: str) -> Path:
        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        return (
            self._home.paths.plan_material_manifests
            / _subject_storage_key(cleaned)
        )

    def _path(self, subject_id: str, manifest_id: str) -> Path:
        if (
            not isinstance(manifest_id, str)
            or _MANIFEST_ID_PATTERN.fullmatch(manifest_id) is None
        ):
            raise ValueError("manifest_id is invalid")
        return self._subject_directory(subject_id) / f"{manifest_id}.json"

    def _entries_are_valid(self, manifest: PlanMaterialManifest) -> bool:
        for entry in manifest.entries:
            try:
                path = self._home.contained_path(entry.artifact_reference)
                if path.is_symlink() or not path.is_file():
                    return False
                content = path.read_bytes()
            except (OSError, PrivateHomeError):
                return False
            if (
                hashlib.sha256(content).hexdigest() != entry.artifact_sha256
                or not content.startswith(b"%PDF-")
                or (
                    entry.contract_version
                    == PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2
                    and len(content) != entry.artifact_byte_size
                )
            ):
                return False
        return True

    def get(
        self, *, subject_id: str, manifest_id: str
    ) -> PlanMaterialManifestReadResult:
        path = self._path(subject_id, manifest_id)
        with self._lock:
            if not path.exists():
                return PlanMaterialManifestReadResult(
                    status=PlanMaterialManifestReadStatus.NOT_FOUND,
                    manifest=None,
                )
            if path.is_symlink() or not path.is_file():
                return PlanMaterialManifestReadResult(
                    status=(
                        PlanMaterialManifestReadStatus.INTEGRITY_FAILURE
                    ),
                    manifest=None,
                    reason_code=(
                        PlanMaterialManifestFailureReason
                        .MANIFEST_INTEGRITY_FAILURE
                    ),
                )
            try:
                manifest = _manifest_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return PlanMaterialManifestReadResult(
                    status=(
                        PlanMaterialManifestReadStatus.INTEGRITY_FAILURE
                    ),
                    manifest=None,
                    reason_code=(
                        PlanMaterialManifestFailureReason
                        .MANIFEST_INTEGRITY_FAILURE
                    ),
                )
            if (
                manifest.subject_id != subject_id.strip()
                or manifest.manifest_id != manifest_id
                or path.name != f"{manifest.manifest_id}.json"
                or not self._entries_are_valid(manifest)
            ):
                return PlanMaterialManifestReadResult(
                    status=(
                        PlanMaterialManifestReadStatus.INTEGRITY_FAILURE
                    ),
                    manifest=None,
                    reason_code=(
                        PlanMaterialManifestFailureReason
                        .MANIFEST_INTEGRITY_FAILURE
                    ),
                )
            return PlanMaterialManifestReadResult(
                status=PlanMaterialManifestReadStatus.FOUND,
                manifest=manifest,
            )

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> PlanMaterialManifestReadResult:
        """Pick by stored assembly time, then manifest ID; never by mtime."""

        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        plan_id = _clean_text(
            "application_plan_id", application_plan_id, maximum=160
        )
        directory = self._subject_directory(cleaned)
        if not directory.exists():
            return PlanMaterialManifestReadResult(
                status=PlanMaterialManifestReadStatus.NOT_FOUND,
                manifest=None,
            )
        if directory.is_symlink() or not directory.is_dir():
            return PlanMaterialManifestReadResult(
                status=PlanMaterialManifestReadStatus.INTEGRITY_FAILURE,
                manifest=None,
                reason_code=(
                    PlanMaterialManifestFailureReason
                    .MANIFEST_INTEGRITY_FAILURE
                ),
            )
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            return PlanMaterialManifestReadResult(
                status=PlanMaterialManifestReadStatus.INTEGRITY_FAILURE,
                manifest=None,
                reason_code=(
                    PlanMaterialManifestFailureReason
                    .MANIFEST_INTEGRITY_FAILURE
                ),
            )
        matches: list[PlanMaterialManifest] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or _MANIFEST_ID_PATTERN.fullmatch(path.stem) is None
            ):
                return PlanMaterialManifestReadResult(
                    status=(
                        PlanMaterialManifestReadStatus.INTEGRITY_FAILURE
                    ),
                    manifest=None,
                    reason_code=(
                        PlanMaterialManifestFailureReason
                        .MANIFEST_INTEGRITY_FAILURE
                    ),
                )
            result = self.get(subject_id=cleaned, manifest_id=path.stem)
            if (
                result.status is not PlanMaterialManifestReadStatus.FOUND
                or result.manifest is None
            ):
                return PlanMaterialManifestReadResult(
                    status=(
                        PlanMaterialManifestReadStatus.INTEGRITY_FAILURE
                    ),
                    manifest=None,
                    reason_code=(
                        PlanMaterialManifestFailureReason
                        .MANIFEST_INTEGRITY_FAILURE
                    ),
                )
            if result.manifest.application_plan_id == plan_id:
                matches.append(result.manifest)
        if not matches:
            return PlanMaterialManifestReadResult(
                status=PlanMaterialManifestReadStatus.NOT_FOUND,
                manifest=None,
            )
        current = max(
            matches,
            key=lambda item: (
                item.assembled_at.astimezone(timezone.utc),
                item.manifest_id,
            ),
        )
        return PlanMaterialManifestReadResult(
            status=PlanMaterialManifestReadStatus.FOUND,
            manifest=current,
        )

    def save(
        self, manifest: PlanMaterialManifest
    ) -> PlanMaterialManifestWriteResult:
        if not isinstance(manifest, PlanMaterialManifest):
            raise TypeError("manifest must be a PlanMaterialManifest")
        path = self._path(manifest.subject_id, manifest.manifest_id)
        with self._lock:
            if (
                manifest.contract_version
                != PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION
                or not self._entries_are_valid(manifest)
            ):
                return PlanMaterialManifestWriteResult(
                    status=PlanMaterialManifestWriteStatus.FAILED,
                    manifest=None,
                    reason_code=(
                        PlanMaterialManifestFailureReason
                        .MANIFEST_INTEGRITY_FAILURE
                    ),
                    retryable=False,
                )
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            manifest.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return PlanMaterialManifestWriteResult(
                    status=PlanMaterialManifestWriteStatus.FAILED,
                    manifest=None,
                    reason_code=(
                        PlanMaterialManifestFailureReason
                        .MANIFEST_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return PlanMaterialManifestWriteResult(
                    status=PlanMaterialManifestWriteStatus.CREATED,
                    manifest=manifest,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=manifest.subject_id,
                manifest_id=manifest.manifest_id,
            )
            if (
                existing.status is PlanMaterialManifestReadStatus.FOUND
                and existing.manifest is not None
                and existing.manifest.content_dict()
                == manifest.content_dict()
            ):
                return PlanMaterialManifestWriteResult(
                    status=PlanMaterialManifestWriteStatus.UNCHANGED,
                    manifest=existing.manifest,
                    reason_code=None,
                    retryable=False,
                )
            return PlanMaterialManifestWriteResult(
                status=PlanMaterialManifestWriteStatus.FAILED,
                manifest=None,
                reason_code=(
                    PlanMaterialManifestFailureReason
                    .MANIFEST_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class AssemblePlanMaterialManifestCommand:
    subject_id: str
    application_plan_id: str
    prepared_resume_material_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class AssemblePlanMaterialManifestResult:
    status: PlanMaterialManifestStatus
    subject_id: str
    application_plan_id: str
    manifest: PlanMaterialManifest | None
    write_result: PlanMaterialManifestWriteResult | None
    reason_code: PlanMaterialManifestFailureReason | None
    not_ready_reason: PlanMaterialManifestNotReadyReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = PlanMaterialManifestStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PlanMaterialManifestFailureReason(self.reason_code),
            )
        if self.not_ready_reason is not None:
            object.__setattr__(
                self,
                "not_ready_reason",
                PlanMaterialManifestNotReadyReason(self.not_ready_reason),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            PlanMaterialManifestStatus.CREATED,
            PlanMaterialManifestStatus.UNCHANGED,
        }:
            expected = PlanMaterialManifestWriteStatus(status.value)
            if (
                not isinstance(self.manifest, PlanMaterialManifest)
                or not isinstance(
                    self.write_result, PlanMaterialManifestWriteResult
                )
                or self.write_result.status is not expected
                or self.write_result.manifest != self.manifest
                or self.reason_code is not None
                or self.not_ready_reason is not None
                or self.retryable
            ):
                raise ValueError("successful assembly result is invalid")
        elif status is PlanMaterialManifestStatus.NOT_READY:
            if (
                self.manifest is not None
                or self.write_result is not None
                or self.reason_code is not None
                or self.not_ready_reason is None
                or self.retryable
            ):
                raise ValueError("not-ready assembly result is invalid")
        elif (
            self.manifest is not None
            or self.reason_code is None
            or self.not_ready_reason is not None
        ):
            raise ValueError("failed assembly result is invalid")


def _failure(
    command: AssemblePlanMaterialManifestCommand,
    reason: PlanMaterialManifestFailureReason,
    *,
    retryable: bool = False,
) -> AssemblePlanMaterialManifestResult:
    return AssemblePlanMaterialManifestResult(
        status=PlanMaterialManifestStatus.FAILED,
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
        manifest=None,
        write_result=None,
        reason_code=reason,
        not_ready_reason=None,
        retryable=retryable,
        message=f"Material manifest assembly failed: {reason.value}.",
    )


def _not_ready(
    command: AssemblePlanMaterialManifestCommand,
    reason: PlanMaterialManifestNotReadyReason,
    *,
    detail: str,
) -> AssemblePlanMaterialManifestResult:
    return AssemblePlanMaterialManifestResult(
        status=PlanMaterialManifestStatus.NOT_READY,
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        manifest=None,
        write_result=None,
        reason_code=None,
        not_ready_reason=reason,
        retryable=False,
        message=f"The material manifest is not ready: {detail}",
    )


def assemble_plan_material_manifest(
    command: AssemblePlanMaterialManifestCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    prepared_resume_repository: PreparedResumeMaterialRepository,
    manifest_repository: PlanMaterialManifestRepository,
    home: PrivateHome | None = None,
) -> AssemblePlanMaterialManifestResult:
    """Declare one published resume as this plan's formal RESUME material."""

    active_home = home or PrivateHome.discover()
    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        plan_id = _clean_text(
            "application_plan_id",
            command.application_plan_id,
            maximum=160,
        )
        material_id = _clean_text(
            "prepared_resume_material_id",
            command.prepared_resume_material_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, PlanMaterialManifestFailureReason.INVALID_REQUEST
        )

    try:
        plan_read = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PlanMaterialManifestFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command,
            PlanMaterialManifestFailureReason.APPLICATION_PLAN_NOT_FOUND,
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_read.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            PlanMaterialManifestFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_read.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            PlanMaterialManifestFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
        )

    try:
        material_read = prepared_resume_repository.get(
            subject_id=subject_id, material_id=material_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PlanMaterialManifestFailureReason
            .PREPARED_RESUME_INTEGRITY_FAILURE,
        )
    if material_read.status is PreparedResumeMaterialReadStatus.NOT_FOUND:
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PREPARED_RESUME_NOT_PUBLISHED,
            detail=(
                "no published prepared resume was found for this plan; "
                "nothing older is substituted."
            ),
        )
    if (
        material_read.status is not PreparedResumeMaterialReadStatus.FOUND
        or not isinstance(material_read.material, PreparedResumeMaterial)
    ):
        return _failure(
            command,
            PlanMaterialManifestFailureReason
            .PREPARED_RESUME_INTEGRITY_FAILURE,
        )
    material = material_read.material
    if material.material_role is not PreparedMaterialRole.RESUME:
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason.PREPARED_RESUME_ROLE_MISMATCH,
            detail="the published material is not a resume.",
        )
    if (
        material.subject_id != subject_id
        or material.application_plan_id != plan.plan_id
        or material.job_id != plan.job_id
        or material.job_revision != plan.job_revision
        or material.job_content_hash != plan.job_content_hash
    ):
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason.PREPARED_RESUME_PLAN_MISMATCH,
            detail="the published resume belongs to a different plan chain.",
        )
    if not all(
        (
            material.tailored_resume_draft_id,
            material.tailored_resume_draft_hash,
            material.fact_qa_result_id,
            material.fact_qa_result_hash,
            material.latex_version_id,
            material.latex_source_sha256,
            material.compilation_record_id,
            material.compilation_binding,
            material.visual_qa_result_id,
            material.visual_qa_result_hash,
        )
    ):
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason.PREPARED_RESUME_PLAN_MISMATCH,
            detail="the published resume is missing preparation provenance.",
        )

    try:
        pdf_path = active_home.contained_path(material.pdf_reference)
        if pdf_path.is_symlink() or not pdf_path.is_file():
            raise ValueError("the managed PDF is not a regular file")
        content = pdf_path.read_bytes()
    except (OSError, PrivateHomeError, TypeError, ValueError):
        return _failure(
            command, PlanMaterialManifestFailureReason.ARTIFACT_UNREADABLE
        )
    if hashlib.sha256(content).hexdigest() != material.pdf_sha256:
        return _failure(
            command, PlanMaterialManifestFailureReason.ARTIFACT_HASH_DRIFT
        )
    if (
        not content.startswith(b"%PDF-")
        or len(content) != material.pdf_byte_size
        or pdf_page_count(content) != material.page_count
    ):
        return _failure(
            command, PlanMaterialManifestFailureReason.ARTIFACT_INVALID
        )

    material_hash = prepared_material_content_hash(material)
    entry_content = {
        "artifact_byte_size": len(content),
        "artifact_reference": material.pdf_reference,
        "artifact_sha256": material.pdf_sha256,
        "material_role": PlanMaterialRole.RESUME.value,
        "media_type": RESUME_MEDIA_TYPE,
        "order": 0,
        "page_count": material.page_count,
        "prepared_material_id": material.material_id,
        "provenance_type": (
            PlanMaterialProvenanceType.PREPARED_RESUME_MATERIAL.value
        ),
        "source_record_hash": material_hash,
        "source_record_id": material.material_id,
    }
    try:
        entry = PlanMaterialEntry(
            entry_id=plan_material_entry_id(entry_content),
            order=0,
            material_role=PlanMaterialRole.RESUME,
            prepared_material_id=material.material_id,
            artifact_reference=material.pdf_reference,
            artifact_sha256=material.pdf_sha256,
            media_type=RESUME_MEDIA_TYPE,
            page_count=material.page_count,
            provenance_type=(
                PlanMaterialProvenanceType.PREPARED_RESUME_MATERIAL
            ),
            source_record_id=material.material_id,
            source_record_hash=material_hash,
            artifact_byte_size=len(content),
            contract_version=PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
        )
        identity = {
            "contract_version": PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
            "subject_id": subject_id,
            "application_plan_id": plan.plan_id,
            "job_id": plan.job_id,
            "job_revision": plan.job_revision,
            "job_content_hash": plan.job_content_hash,
            "prepared_resume_material_id": material.material_id,
            "prepared_resume_material_hash": material_hash,
            "resume_artifact_sha256": material.pdf_sha256,
            "entry_hashes": (entry.entry_id,),
            "artifact_byte_sizes": (len(content),),
            "assembly_state": PlanMaterialAssemblyState.RESUME_ONLY,
        }
        manifest_id = plan_material_manifest_id(**identity)
        content_values = {
            "manifest_id": manifest_id,
            "contract_version": PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
            "subject_id": subject_id,
            "application_plan_id": plan.plan_id,
            "job_id": plan.job_id,
            "job_revision": plan.job_revision,
            "job_content_hash": plan.job_content_hash,
            "prepared_resume_material_id": material.material_id,
            "prepared_resume_material_hash": material_hash,
            "resume_artifact_sha256": material.pdf_sha256,
            "assembly_state": PlanMaterialAssemblyState.RESUME_ONLY.value,
            "included_roles": [PlanMaterialRole.RESUME.value],
            "entries": [entry.to_dict()],
        }
        manifest = PlanMaterialManifest(
            manifest_id=manifest_id,
            contract_version=PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            job_id=plan.job_id,
            job_revision=plan.job_revision,
            job_content_hash=plan.job_content_hash,
            prepared_resume_material_id=material.material_id,
            prepared_resume_material_hash=material_hash,
            resume_artifact_sha256=material.pdf_sha256,
            assembly_state=PlanMaterialAssemblyState.RESUME_ONLY,
            included_roles=(PlanMaterialRole.RESUME,),
            entries=(entry,),
            manifest_content_hash=_canonical_hash(content_values),
            assembled_at=now,
        )
    except (TypeError, ValueError):
        return _failure(
            command,
            PlanMaterialManifestFailureReason.MANIFEST_INTEGRITY_FAILURE,
        )

    try:
        write_result = manifest_repository.save(manifest)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PlanMaterialManifestFailureReason.MANIFEST_PERSISTENCE_FAILED,
            retryable=True,
        )
    if write_result.status is PlanMaterialManifestWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or PlanMaterialManifestFailureReason.MANIFEST_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
        )
    status = PlanMaterialManifestStatus(write_result.status.value)
    return AssemblePlanMaterialManifestResult(
        status=status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        manifest=write_result.manifest,
        write_result=write_result,
        reason_code=None,
        not_ready_reason=None,
        retryable=False,
        message=(
            "The plan material manifest was assembled with a resume entry."
            if status is PlanMaterialManifestStatus.CREATED
            else "The existing plan material manifest is unchanged."
        ),
    )


__all__ = [
    "AssemblePlanMaterialManifestCommand",
    "AssemblePlanMaterialManifestResult",
    "PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION",
    "PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1",
    "PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2",
    "PlanMaterialAssemblyState",
    "PlanMaterialEntry",
    "PlanMaterialManifest",
    "PlanMaterialManifestFailureReason",
    "PlanMaterialManifestNotReadyReason",
    "PlanMaterialManifestReadResult",
    "PlanMaterialManifestReadStatus",
    "PlanMaterialManifestRepository",
    "PlanMaterialManifestStatus",
    "PlanMaterialManifestWriteResult",
    "PlanMaterialManifestWriteStatus",
    "PlanMaterialProvenanceType",
    "PlanMaterialRole",
    "PrivateHomePlanMaterialManifestRepository",
    "RESUME_MEDIA_TYPE",
    "assemble_plan_material_manifest",
    "plan_material_entry_id",
    "plan_material_manifest_content_hash",
    "plan_material_manifest_id",
    "prepared_material_content_hash",
]
