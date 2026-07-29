"""Include one published cover letter in an immutable plan manifest."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .plan_material_manifest import (
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
    RESUME_MEDIA_TYPE,
    PlanMaterialAssemblyState,
    PlanMaterialEntry,
    PlanMaterialManifest,
    PlanMaterialManifestFailureReason,
    PlanMaterialManifestNotReadyReason,
    PlanMaterialManifestReadStatus,
    PlanMaterialManifestRepository,
    PlanMaterialManifestStatus,
    PlanMaterialManifestWriteResult,
    PlanMaterialManifestWriteStatus,
    PlanMaterialProvenanceType,
    PlanMaterialRole,
    plan_material_entry_id,
    plan_material_manifest_content_hash,
    plan_material_manifest_id,
)
from .prepared_cover_letter_material import (
    PreparedCoverLetterMaterial,
    PreparedCoverLetterMaterialReadStatus,
    PreparedCoverLetterMaterialRepository,
    PreparedCoverLetterMaterialRole,
    cover_letter_pdf_reference,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_compilation import pdf_page_count


COVER_LETTER_MEDIA_TYPE = "application/pdf"


@dataclass(frozen=True, slots=True)
class IncludeCoverLetterInPlanMaterialManifestCommand:
    subject_id: str
    application_plan_id: str
    plan_material_manifest_id: str
    prepared_cover_letter_material_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class IncludeCoverLetterInPlanMaterialManifestResult:
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
                raise ValueError("successful inclusion result is invalid")
        elif status is PlanMaterialManifestStatus.NOT_READY:
            if (
                self.manifest is not None
                or self.write_result is not None
                or self.reason_code is not None
                or self.not_ready_reason is None
                or self.retryable
            ):
                raise ValueError("not-ready inclusion result is invalid")
        elif (
            self.manifest is not None
            or self.reason_code is None
            or self.not_ready_reason is not None
        ):
            raise ValueError("failed inclusion result is invalid")


def _clean_text(name: str, value: object, *, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the manifest contract")
    return cleaned


def _failure(
    command: IncludeCoverLetterInPlanMaterialManifestCommand,
    reason: PlanMaterialManifestFailureReason,
    *,
    retryable: bool = False,
) -> IncludeCoverLetterInPlanMaterialManifestResult:
    return IncludeCoverLetterInPlanMaterialManifestResult(
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
        message=f"Cover-letter manifest inclusion failed: {reason.value}.",
    )


def _not_ready(
    command: IncludeCoverLetterInPlanMaterialManifestCommand,
    reason: PlanMaterialManifestNotReadyReason,
    detail: str,
) -> IncludeCoverLetterInPlanMaterialManifestResult:
    return IncludeCoverLetterInPlanMaterialManifestResult(
        status=PlanMaterialManifestStatus.NOT_READY,
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        manifest=None,
        write_result=None,
        reason_code=None,
        not_ready_reason=reason,
        retryable=False,
        message=f"Cover-letter manifest inclusion is not ready: {detail}",
    )


def _manifest_matches_plan(
    manifest: PlanMaterialManifest,
    plan: ApplicationPlan,
    subject_id: str,
) -> bool:
    return (
        manifest.subject_id == subject_id
        and manifest.application_plan_id == plan.plan_id
        and manifest.job_id == plan.job_id
        and manifest.job_revision == plan.job_revision
        and manifest.job_content_hash == plan.job_content_hash
    )


def _cover_letter_matches_plan(
    material: PreparedCoverLetterMaterial,
    plan: ApplicationPlan,
    subject_id: str,
) -> bool:
    return (
        material.subject_id == subject_id
        and material.application_plan_id == plan.plan_id
        and material.plan_user_instructions_hash
        == plan.user_preparation_instructions_hash
        and material.job_id == plan.job_id
        and material.job_revision == plan.job_revision
        and material.job_content_hash == plan.job_content_hash
    )


def _cover_letter_provenance_is_complete(
    material: PreparedCoverLetterMaterial,
) -> bool:
    return all(
        (
            material.publication_binding,
            material.cover_letter_draft_id,
            material.draft_content_hash,
            material.evidence_snapshot_id,
            material.evidence_snapshot_hash,
            material.fact_qa_result_id,
            material.fact_qa_result_hash,
            material.template_id,
            material.template_version,
            material.template_sha256,
            material.latex_source_reference,
            material.latex_source_sha256,
            material.compiler_engine,
            material.compiler_version,
            material.compile_policy_version,
            material.sandbox_policy_version,
            material.pdf_reference,
            material.pdf_sha256,
            material.material_content_hash,
        )
    )


def _cover_letter_entry(
    material: PreparedCoverLetterMaterial,
) -> PlanMaterialEntry:
    content = {
        "artifact_byte_size": material.pdf_byte_size,
        "artifact_reference": material.pdf_reference,
        "artifact_sha256": material.pdf_sha256,
        "material_role": PlanMaterialRole.COVER_LETTER.value,
        "media_type": COVER_LETTER_MEDIA_TYPE,
        "order": 1,
        "page_count": material.page_count,
        "prepared_material_id": material.material_id,
        "provenance_type": (
            PlanMaterialProvenanceType
            .PREPARED_COVER_LETTER_MATERIAL.value
        ),
        "source_record_hash": material.material_content_hash,
        "source_record_id": material.material_id,
    }
    return PlanMaterialEntry(
        entry_id=plan_material_entry_id(content),
        order=1,
        material_role=PlanMaterialRole.COVER_LETTER,
        prepared_material_id=material.material_id,
        artifact_reference=material.pdf_reference,
        artifact_sha256=material.pdf_sha256,
        media_type=COVER_LETTER_MEDIA_TYPE,
        page_count=material.page_count,
        provenance_type=(
            PlanMaterialProvenanceType.PREPARED_COVER_LETTER_MATERIAL
        ),
        source_record_id=material.material_id,
        source_record_hash=material.material_content_hash,
        artifact_byte_size=material.pdf_byte_size,
        contract_version=PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
    )


def include_cover_letter_in_plan_material_manifest(
    command: IncludeCoverLetterInPlanMaterialManifestCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    manifest_repository: PlanMaterialManifestRepository,
    prepared_cover_letter_repository: (
        PreparedCoverLetterMaterialRepository
    ),
    home: PrivateHome | None = None,
) -> IncludeCoverLetterInPlanMaterialManifestResult:
    """Create a new manifest preserving RESUME and adding COVER_LETTER."""

    active_home = home or PrivateHome.discover()
    try:
        subject_id = _clean_text("subject_id", command.subject_id)
        plan_id = _clean_text(
            "application_plan_id", command.application_plan_id
        )
        prior_manifest_id = _clean_text(
            "plan_material_manifest_id",
            command.plan_material_manifest_id,
        )
        material_id = _clean_text(
            "prepared_cover_letter_material_id",
            command.prepared_cover_letter_material_id,
        )
        if not isinstance(command.now, datetime) or command.now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = command.now
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
        manifest_read = manifest_repository.get(
            subject_id=subject_id, manifest_id=prior_manifest_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PlanMaterialManifestFailureReason.MANIFEST_INTEGRITY_FAILURE,
        )
    if manifest_read.status is PlanMaterialManifestReadStatus.NOT_FOUND:
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PLAN_MATERIAL_MANIFEST_NOT_READY,
            "the explicitly selected prior manifest was not found.",
        )
    if (
        manifest_read.status is not PlanMaterialManifestReadStatus.FOUND
        or not isinstance(manifest_read.manifest, PlanMaterialManifest)
    ):
        return _failure(
            command,
            PlanMaterialManifestFailureReason.MANIFEST_INTEGRITY_FAILURE,
        )
    prior = manifest_read.manifest
    if prior.contract_version != PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION:
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE,
            "the prior v1 manifest requires an explicit new v2 assembly.",
        )
    if not _manifest_matches_plan(prior, plan, subject_id):
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PLAN_MATERIAL_MANIFEST_NOT_READY,
            "the prior manifest belongs to a different plan binding.",
        )
    if prior.included_roles not in (
        (PlanMaterialRole.RESUME,),
        (PlanMaterialRole.RESUME, PlanMaterialRole.COVER_LETTER),
    ):
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PLAN_MATERIAL_MANIFEST_NOT_READY,
            "the prior manifest is not a supported ordered material set.",
        )
    resume = prior.entries[0]
    if (
        resume.material_role is not PlanMaterialRole.RESUME
        or resume.provenance_type
        is not PlanMaterialProvenanceType.PREPARED_RESUME_MATERIAL
        or resume.media_type != RESUME_MEDIA_TYPE
    ):
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PLAN_MATERIAL_MANIFEST_NOT_READY,
            "the prior manifest lacks one valid preserved resume.",
        )

    try:
        material_read = prepared_cover_letter_repository.get(
            subject_id=subject_id, material_id=material_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PlanMaterialManifestFailureReason
            .PREPARED_COVER_LETTER_INTEGRITY_FAILURE,
        )
    if material_read.status is PreparedCoverLetterMaterialReadStatus.NOT_FOUND:
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PREPARED_COVER_LETTER_NOT_PUBLISHED,
            "the explicitly selected cover letter was not published.",
        )
    if (
        material_read.status
        is not PreparedCoverLetterMaterialReadStatus.FOUND
        or not isinstance(
            material_read.material, PreparedCoverLetterMaterial
        )
    ):
        return _failure(
            command,
            PlanMaterialManifestFailureReason
            .PREPARED_COVER_LETTER_INTEGRITY_FAILURE,
        )
    material = material_read.material
    if (
        material.material_role
        is not PreparedCoverLetterMaterialRole.COVER_LETTER
    ):
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PREPARED_COVER_LETTER_ROLE_MISMATCH,
            "the selected prepared material is not a cover letter.",
        )
    if (
        not _cover_letter_matches_plan(material, plan, subject_id)
        or not _cover_letter_provenance_is_complete(material)
    ):
        return _not_ready(
            command,
            PlanMaterialManifestNotReadyReason
            .PREPARED_COVER_LETTER_PLAN_MISMATCH,
            "the cover letter provenance does not match the current plan.",
        )

    try:
        if material.pdf_reference != cover_letter_pdf_reference(
            subject_id=subject_id, pdf_sha256=material.pdf_sha256
        ):
            raise ValueError("the PDF is outside its managed subject path")
        pdf_path = active_home.contained_path(material.pdf_reference)
        if pdf_path.is_symlink() or not pdf_path.is_file():
            raise ValueError("the managed PDF is not a regular file")
        pdf_bytes = pdf_path.read_bytes()
        pdf_size = pdf_path.stat(follow_symlinks=False).st_size
    except (OSError, PrivateHomeError, TypeError, ValueError):
        return _failure(
            command, PlanMaterialManifestFailureReason.ARTIFACT_UNREADABLE
        )
    if hashlib.sha256(pdf_bytes).hexdigest() != material.pdf_sha256:
        return _failure(
            command, PlanMaterialManifestFailureReason.ARTIFACT_HASH_DRIFT
        )
    if (
        not pdf_bytes.startswith(b"%PDF-")
        or pdf_size != len(pdf_bytes)
        or len(pdf_bytes) != material.pdf_byte_size
        or pdf_page_count(pdf_bytes) != material.page_count
    ):
        return _failure(
            command, PlanMaterialManifestFailureReason.ARTIFACT_INVALID
        )

    try:
        cover_letter = _cover_letter_entry(material)
    except (TypeError, ValueError):
        return _failure(
            command,
            PlanMaterialManifestFailureReason.MANIFEST_INTEGRITY_FAILURE,
        )

    existing_cover_letter = prior.entry_for_role(
        PlanMaterialRole.COVER_LETTER
    )
    if existing_cover_letter is not None:
        if existing_cover_letter != cover_letter:
            if (
                existing_cover_letter.prepared_material_id
                == material.material_id
            ):
                return _failure(
                    command,
                    PlanMaterialManifestFailureReason
                    .MANIFEST_INTEGRITY_FAILURE,
                )
        else:
            write_result = PlanMaterialManifestWriteResult(
                status=PlanMaterialManifestWriteStatus.UNCHANGED,
                manifest=prior,
                reason_code=None,
                retryable=False,
            )
            return IncludeCoverLetterInPlanMaterialManifestResult(
                status=PlanMaterialManifestStatus.UNCHANGED,
                subject_id=subject_id,
                application_plan_id=plan_id,
                manifest=prior,
                write_result=write_result,
                reason_code=None,
                not_ready_reason=None,
                retryable=False,
                message="The selected cover letter is already included.",
            )

    identity = {
        "contract_version": PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
        "subject_id": subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "prepared_resume_material_id": (
            prior.prepared_resume_material_id
        ),
        "prepared_resume_material_hash": (
            prior.prepared_resume_material_hash
        ),
        "resume_artifact_sha256": prior.resume_artifact_sha256,
        "entry_hashes": (resume.entry_id, cover_letter.entry_id),
        "artifact_byte_sizes": (
            resume.artifact_byte_size,
            cover_letter.artifact_byte_size,
        ),
        "assembly_state": (
            PlanMaterialAssemblyState.RESUME_AND_COVER_LETTER
        ),
        "prior_manifest_id": prior.manifest_id,
        "prior_manifest_content_hash": prior.manifest_content_hash,
        "prepared_cover_letter_material_id": material.material_id,
        "prepared_cover_letter_material_hash": (
            material.material_content_hash
        ),
        "cover_letter_artifact_sha256": material.pdf_sha256,
        "preserved_resume_entry_hash": resume.entry_id,
    }
    try:
        manifest_id = plan_material_manifest_id(**identity)
        content = {
            "manifest_id": manifest_id,
            "contract_version": PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
            "subject_id": subject_id,
            "application_plan_id": plan.plan_id,
            "job_id": plan.job_id,
            "job_revision": plan.job_revision,
            "job_content_hash": plan.job_content_hash,
            "prepared_resume_material_id": (
                prior.prepared_resume_material_id
            ),
            "prepared_resume_material_hash": (
                prior.prepared_resume_material_hash
            ),
            "resume_artifact_sha256": prior.resume_artifact_sha256,
            "assembly_state": (
                PlanMaterialAssemblyState.RESUME_AND_COVER_LETTER.value
            ),
            "included_roles": [
                PlanMaterialRole.RESUME.value,
                PlanMaterialRole.COVER_LETTER.value,
            ],
            "entries": [resume.to_dict(), cover_letter.to_dict()],
            "prior_manifest_id": prior.manifest_id,
            "prior_manifest_content_hash": prior.manifest_content_hash,
            "prepared_cover_letter_material_id": material.material_id,
            "prepared_cover_letter_material_hash": (
                material.material_content_hash
            ),
            "cover_letter_artifact_sha256": material.pdf_sha256,
            "preserved_resume_entry_hash": resume.entry_id,
        }
        manifest = PlanMaterialManifest(
            manifest_id=manifest_id,
            contract_version=PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            job_id=plan.job_id,
            job_revision=plan.job_revision,
            job_content_hash=plan.job_content_hash,
            prepared_resume_material_id=(
                prior.prepared_resume_material_id
            ),
            prepared_resume_material_hash=(
                prior.prepared_resume_material_hash
            ),
            resume_artifact_sha256=prior.resume_artifact_sha256,
            assembly_state=(
                PlanMaterialAssemblyState.RESUME_AND_COVER_LETTER
            ),
            included_roles=(
                PlanMaterialRole.RESUME,
                PlanMaterialRole.COVER_LETTER,
            ),
            entries=(resume, cover_letter),
            manifest_content_hash=plan_material_manifest_content_hash(
                content
            ),
            assembled_at=now,
            prior_manifest_id=prior.manifest_id,
            prior_manifest_content_hash=prior.manifest_content_hash,
            prepared_cover_letter_material_id=material.material_id,
            prepared_cover_letter_material_hash=(
                material.material_content_hash
            ),
            cover_letter_artifact_sha256=material.pdf_sha256,
            preserved_resume_entry_hash=resume.entry_id,
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
    return IncludeCoverLetterInPlanMaterialManifestResult(
        status=status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        manifest=write_result.manifest,
        write_result=write_result,
        reason_code=None,
        not_ready_reason=None,
        retryable=False,
        message=(
            "A new plan manifest includes the published cover letter."
            if status is PlanMaterialManifestStatus.CREATED
            else "The cover-letter plan manifest is unchanged."
        ),
    )


__all__ = [
    "COVER_LETTER_MEDIA_TYPE",
    "IncludeCoverLetterInPlanMaterialManifestCommand",
    "IncludeCoverLetterInPlanMaterialManifestResult",
    "include_cover_letter_in_plan_material_manifest",
]
