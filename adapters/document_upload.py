"""Deterministic mapping from canonical file controls to prepared PDFs."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from core.application_answer_taxonomy import CanonicalApplicationAnswerKey
from core.bundles import MaterialBundle
from core.private_home import PrivateHome, PrivateHomeError

if TYPE_CHECKING:
    from .protocol import FieldIR, FormIR


_MATERIAL_KEYS = (
    CanonicalApplicationAnswerKey.RESUME,
    CanonicalApplicationAnswerKey.COVER_LETTER_FILE,
)
_SUBJECT_KEY_RE = re.compile(r"^subject-[a-f0-9]{64}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class ApplicationDocumentUploadPlanStatus(StrEnum):
    READY = "READY"
    FAILED = "FAILED"


class ApplicationDocumentUploadFailure(StrEnum):
    MATERIAL_MISSING = "MATERIAL_MISSING"
    AMBIGUOUS_ROLE = "AMBIGUOUS_ROLE"
    ARTIFACT_INTEGRITY_FAILURE = "ARTIFACT_INTEGRITY_FAILURE"
    UNSUPPORTED_FILE_CONTROL = "UNSUPPORTED_FILE_CONTROL"


@dataclass(frozen=True, slots=True)
class ApplicationDocumentUploadItem:
    control_id: str
    canonical_material_key: CanonicalApplicationAnswerKey
    managed_artifact_reference: str
    artifact_sha256: str
    byte_size: int
    media_type: str
    resolved_path: Path

    def __post_init__(self) -> None:
        key = CanonicalApplicationAnswerKey(self.canonical_material_key)
        if key not in _MATERIAL_KEYS:
            raise ValueError("upload item key is not a document material")
        object.__setattr__(self, "canonical_material_key", key)
        if not self.control_id:
            raise ValueError("upload control ID is required")
        if (
            _SHA256_RE.fullmatch(self.artifact_sha256) is None
            or self.byte_size < 1
            or self.media_type != "application/pdf"
        ):
            raise ValueError("upload item must reference a non-empty PDF")
        reference = PurePosixPath(self.managed_artifact_reference)
        if (
            reference.is_absolute()
            or ".." in reference.parts
            or reference.parts[:2] != ("state", "preparation")
            or len(_subject_keys(self.managed_artifact_reference)) != 1
        ):
            raise ValueError("upload item artifact reference is not managed")


@dataclass(frozen=True, slots=True)
class ApplicationDocumentUploadPlan:
    items: tuple[ApplicationDocumentUploadItem, ...]
    skipped_control_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.items, tuple)
            or any(
                not isinstance(item, ApplicationDocumentUploadItem)
                for item in self.items
            )
            or not isinstance(self.skipped_control_ids, tuple)
        ):
            raise TypeError("document upload plan fields must be typed tuples")
        control_ids = [item.control_id for item in self.items]
        roles = [item.canonical_material_key for item in self.items]
        if (
            len(control_ids) != len(set(control_ids))
            or len(roles) != len(set(roles))
            or set(control_ids) & set(self.skipped_control_ids)
        ):
            raise ValueError("document upload plan is ambiguous")


@dataclass(frozen=True, slots=True)
class ApplicationDocumentUploadPlanResult:
    status: ApplicationDocumentUploadPlanStatus
    plan: ApplicationDocumentUploadPlan | None
    failure: ApplicationDocumentUploadFailure | None = None
    control_id: str | None = None

    def __post_init__(self) -> None:
        status = ApplicationDocumentUploadPlanStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.failure is not None:
            object.__setattr__(
                self,
                "failure",
                ApplicationDocumentUploadFailure(self.failure),
            )
        if status is ApplicationDocumentUploadPlanStatus.READY:
            if self.plan is None or self.failure is not None:
                raise ValueError("ready upload-plan result is invalid")
        elif self.plan is not None or self.failure is None:
            raise ValueError("failed upload-plan result is invalid")


def document_control_id(field: FieldIR) -> str:
    """Return the stable FormIR identity used to join plan and fill."""

    return field.element_id or field.name or field.selectors[0]


def _failed(
    failure: ApplicationDocumentUploadFailure,
    control_id: str | None = None,
) -> ApplicationDocumentUploadPlanResult:
    return ApplicationDocumentUploadPlanResult(
        status=ApplicationDocumentUploadPlanStatus.FAILED,
        plan=None,
        failure=failure,
        control_id=control_id,
    )


def _subject_keys(reference: str) -> tuple[str, ...]:
    return tuple(
        part
        for part in PurePosixPath(reference).parts
        if _SUBJECT_KEY_RE.fullmatch(part)
    )


def _verified_pdf(
    *,
    home: PrivateHome,
    reference: str,
    expected_sha256: str,
    expected_size: int | None,
) -> tuple[Path, int] | None:
    path_parts = PurePosixPath(reference).parts
    if (
        path_parts[:2] != ("state", "preparation")
        or len(_subject_keys(reference)) != 1
    ):
        return None
    try:
        path = home.contained_path(reference)
        if path.is_symlink() or not path.is_file():
            return None
        content = path.read_bytes()
    except (OSError, PrivateHomeError):
        return None
    if (
        not content.startswith(b"%PDF-")
        or hashlib.sha256(content).hexdigest() != expected_sha256
        or (expected_size is not None and len(content) != expected_size)
        or len(content) < 1
    ):
        return None
    return path, len(content)


def _resume_reference(
    materials: MaterialBundle, home: PrivateHome
) -> str | None:
    try:
        path = home.contained_path(materials.resume_path)
        return path.relative_to(home.paths.root).as_posix()
    except (ValueError, PrivateHomeError):
        return None


def plan_application_document_uploads(
    *,
    form: FormIR,
    materials: MaterialBundle,
    private_home: PrivateHome,
) -> ApplicationDocumentUploadPlanResult:
    """Create a fail-closed, at-most-once upload plan for file controls."""

    file_fields = tuple(field for field in form.fields if field.kind.value == "file")
    by_role: dict[CanonicalApplicationAnswerKey, list[FieldIR]] = {
        key: [] for key in _MATERIAL_KEYS
    }
    skipped: list[str] = []
    seen_control_ids: set[str] = set()
    for field in file_fields:
        control_id = document_control_id(field)
        if control_id in seen_control_ids:
            return _failed(
                ApplicationDocumentUploadFailure.AMBIGUOUS_ROLE, control_id
            )
        seen_control_ids.add(control_id)
        if field.canonical_key not in _MATERIAL_KEYS:
            if field.required:
                return _failed(
                    ApplicationDocumentUploadFailure
                    .UNSUPPORTED_FILE_CONTROL,
                    control_id,
                )
            skipped.append(control_id)
            continue
        by_role[field.canonical_key].append(field)

    for fields in by_role.values():
        if len(fields) > 1 and any(field.required for field in fields):
            return _failed(
                ApplicationDocumentUploadFailure.AMBIGUOUS_ROLE,
                document_control_id(fields[0]),
            )

    items: list[ApplicationDocumentUploadItem] = []
    subject_key: str | None = None
    for key in _MATERIAL_KEYS:
        fields = by_role[key]
        if not fields:
            continue
        selected = fields[0]
        skipped.extend(document_control_id(field) for field in fields[1:])
        if key is CanonicalApplicationAnswerKey.RESUME:
            reference = _resume_reference(materials, private_home)
            if reference is None:
                return _failed(
                    ApplicationDocumentUploadFailure
                    .ARTIFACT_INTEGRITY_FAILURE,
                    document_control_id(selected),
                )
            expected_hash = materials.resume_sha256
            expected_size = None
        else:
            cover = materials.cover_letter_pdf
            if cover is None:
                if selected.required:
                    return _failed(
                        ApplicationDocumentUploadFailure.MATERIAL_MISSING,
                        document_control_id(selected),
                    )
                skipped.append(document_control_id(selected))
                continue
            reference = cover.reference
            expected_hash = cover.sha256
            expected_size = cover.byte_size

        verified = _verified_pdf(
            home=private_home,
            reference=reference,
            expected_sha256=expected_hash,
            expected_size=expected_size,
        )
        if verified is None:
            return _failed(
                ApplicationDocumentUploadFailure.ARTIFACT_INTEGRITY_FAILURE,
                document_control_id(selected),
            )
        path, actual_size = verified
        keys = _subject_keys(reference)
        if subject_key is None:
            subject_key = keys[0]
        elif keys[0] != subject_key:
            return _failed(
                ApplicationDocumentUploadFailure.ARTIFACT_INTEGRITY_FAILURE,
                document_control_id(selected),
            )
        items.append(
            ApplicationDocumentUploadItem(
                control_id=document_control_id(selected),
                canonical_material_key=key,
                managed_artifact_reference=reference,
                artifact_sha256=expected_hash,
                byte_size=actual_size,
                media_type="application/pdf",
                resolved_path=path,
            )
        )

    return ApplicationDocumentUploadPlanResult(
        status=ApplicationDocumentUploadPlanStatus.READY,
        plan=ApplicationDocumentUploadPlan(
            items=tuple(items),
            skipped_control_ids=tuple(dict.fromkeys(skipped)),
        ),
    )


__all__ = [
    "ApplicationDocumentUploadFailure",
    "ApplicationDocumentUploadItem",
    "ApplicationDocumentUploadPlan",
    "ApplicationDocumentUploadPlanResult",
    "ApplicationDocumentUploadPlanStatus",
    "document_control_id",
    "plan_application_document_uploads",
]
