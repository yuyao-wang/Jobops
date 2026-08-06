"""Publish one fact-checked, visually approved PDF as a plan's prepared resume."""

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
from .job_prioritization import ProposedPriorityLevel
from .application_preparation_orchestrator import (
    PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PreparedResumePublicationStopReason,
    PublicPreparationStageResult,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_candidates import (
    ResumeArtifactType,
    ResumeCandidate,
    ResumeCandidateReadStatus,
    ResumeCandidateRepository,
    ResumeCandidateStatus,
)
from .publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    PublicationStoppedSourceLineage,
    create_publication_stopped_source_lineage,
)
from .resume_compilation import (
    ResumeCompilationReadStatus,
    ResumeCompilationRecord,
    ResumeCompilationRepository,
    pdf_page_count,
)
from .resume_fact_qa import (
    RESUME_FACT_QA_CONTRACT_VERSION,
    ResumeFactQAReadStatus,
    ResumeFactQAFindingSeverity,
    ResumeFactQARepository,
    ResumeFactQAResult,
    ResumeFactQAVerdict,
)
from .resume_latex_versions import (
    ResumeLatexVersion,
    ResumeLatexVersionReadStatus,
    ResumeLatexVersionRepository,
)
from .resume_layout_revision import (
    RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
    ResumeLayoutAttemptOutcome,
    ResumeLayoutRevisionReadStatus,
    ResumeLayoutRevisionRepository,
    ResumeLayoutRevisionRun,
    ResumeLayoutRevisionStatus,
)
from .resume_selection import (
    ResumeSelectionDecision,
    ResumeSelectionDecisionReadStatus,
    ResumeSelectionDecisionRepository,
)
from .resume_tailoring import (
    TailoredResumeDraft,
    TailoredResumeDraftReadStatus,
    TailoredResumeDraftRepository,
)
from .resume_visual_qa import (
    RESUME_VISUAL_QA_CONTRACT_VERSION,
    ResumeVisualQAReadStatus,
    ResumeVisualQAFindingSeverity,
    ResumeVisualQARepository,
    ResumeVisualQAResult,
    ResumeVisualQAVerdict,
)


PREPARED_RESUME_MATERIAL_CONTRACT_VERSION = "prepared-resume-material-v1"
APPROVED_RESUME_REUSE_MATERIAL_CONTRACT_VERSION = (
    "approved-resume-reuse-material-v1"
)

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_MATERIAL_ID_PATTERN = re.compile(
    r"^prepared-resume-material-[a-f0-9]{64}$"
)
_REUSE_MATERIAL_ID_PATTERN = re.compile(
    r"^approved-resume-reuse-material-[a-f0-9]{64}$"
)


class PreparedMaterialRole(str, Enum):
    RESUME = "RESUME"


class PreparedResumeMaterialStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_READY = "NOT_READY"
    FAILED = "FAILED"


class PreparedResumeMaterialWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class PreparedResumeMaterialReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class PreparedResumeMaterialNotReadyReason(str, Enum):
    VISUAL_QA_NOT_PASSED = "VISUAL_QA_NOT_PASSED"
    REVISION_RUN_NOT_SUCCESSFUL = "REVISION_RUN_NOT_SUCCESSFUL"
    FACT_QA_NOT_PASSED = "FACT_QA_NOT_PASSED"
    PLAN_BINDING_MISMATCH = "PLAN_BINDING_MISMATCH"
    DRAFT_BINDING_MISMATCH = "DRAFT_BINDING_MISMATCH"
    FACT_QA_BINDING_MISMATCH = "FACT_QA_BINDING_MISMATCH"
    LATEX_VERSION_BINDING_MISMATCH = "LATEX_VERSION_BINDING_MISMATCH"
    COMPILATION_BINDING_MISMATCH = "COMPILATION_BINDING_MISMATCH"
    REVISION_BINDING_MISMATCH = "REVISION_BINDING_MISMATCH"


class PreparedResumeMaterialFailureReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    SOURCE_SELECTION_AMBIGUOUS = "SOURCE_SELECTION_AMBIGUOUS"
    SOURCE_SELECTION_MISSING = "SOURCE_SELECTION_MISSING"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    REVISION_RUN_NOT_FOUND = "REVISION_RUN_NOT_FOUND"
    REVISION_RUN_INTEGRITY_FAILURE = "REVISION_RUN_INTEGRITY_FAILURE"
    VISUAL_QA_NOT_FOUND = "VISUAL_QA_NOT_FOUND"
    VISUAL_QA_INTEGRITY_FAILURE = "VISUAL_QA_INTEGRITY_FAILURE"
    COMPILATION_NOT_FOUND = "COMPILATION_NOT_FOUND"
    COMPILATION_INTEGRITY_FAILURE = "COMPILATION_INTEGRITY_FAILURE"
    LATEX_VERSION_NOT_FOUND = "LATEX_VERSION_NOT_FOUND"
    LATEX_VERSION_INTEGRITY_FAILURE = "LATEX_VERSION_INTEGRITY_FAILURE"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"
    FACT_QA_NOT_FOUND = "FACT_QA_NOT_FOUND"
    FACT_QA_INTEGRITY_FAILURE = "FACT_QA_INTEGRITY_FAILURE"
    PDF_UNREADABLE = "PDF_UNREADABLE"
    PDF_HASH_DRIFT = "PDF_HASH_DRIFT"
    PDF_INVALID = "PDF_INVALID"
    MATERIAL_PERSISTENCE_FAILED = "MATERIAL_PERSISTENCE_FAILED"
    MATERIAL_INTEGRITY_FAILURE = "MATERIAL_INTEGRITY_FAILURE"


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the publication contract")
    return cleaned


def _optional_text(name: str, value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _clean_text(name, value, maximum=maximum)


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _optional_hash(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _require_hash(name, value)


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
        raise ValueError("published_at is invalid")
    return _require_aware(
        "published_at",
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


def _identity_payload(
    *,
    contract_version: str,
    subject_id: str,
    application_plan_id: str,
    job_id: str,
    job_revision: int,
    job_content_hash: str,
    tailored_resume_draft_id: str,
    tailored_resume_draft_hash: str,
    fact_qa_result_id: str,
    fact_qa_result_hash: str,
    latex_version_id: str,
    latex_source_sha256: str,
    compilation_record_id: str,
    compilation_binding: str,
    pdf_sha256: str,
    visual_qa_result_id: str,
    visual_qa_result_hash: str,
    layout_revision_run_id: str | None,
    layout_revision_run_binding: str | None,
    material_role: PreparedMaterialRole,
) -> dict[str, Any]:
    return {
        "application_plan_id": application_plan_id,
        "compilation_binding": compilation_binding,
        "compilation_record_id": compilation_record_id,
        "contract_version": contract_version,
        "fact_qa_result_hash": fact_qa_result_hash,
        "fact_qa_result_id": fact_qa_result_id,
        "job_content_hash": job_content_hash,
        "job_id": job_id,
        "job_revision": job_revision,
        "latex_source_sha256": latex_source_sha256,
        "latex_version_id": latex_version_id,
        "layout_revision_run_binding": layout_revision_run_binding,
        "layout_revision_run_id": layout_revision_run_id,
        "material_role": material_role.value,
        "pdf_sha256": pdf_sha256,
        "subject_id": subject_id,
        "tailored_resume_draft_hash": tailored_resume_draft_hash,
        "tailored_resume_draft_id": tailored_resume_draft_id,
        "visual_qa_result_hash": visual_qa_result_hash,
        "visual_qa_result_id": visual_qa_result_id,
    }


def prepared_resume_material_id(**values: Any) -> str:
    return "prepared-resume-material-" + _canonical_hash(
        _identity_payload(**values)
    )


@dataclass(frozen=True, slots=True)
class PreparedResumeMaterial:
    material_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    tailored_resume_draft_id: str
    tailored_resume_draft_hash: str
    fact_qa_result_id: str
    fact_qa_result_hash: str
    latex_version_id: str
    latex_source_sha256: str
    compilation_record_id: str
    compilation_binding: str
    pdf_reference: str
    pdf_sha256: str
    pdf_byte_size: int
    page_count: int
    visual_qa_result_id: str
    visual_qa_result_hash: str
    layout_revision_run_id: str | None
    layout_revision_run_binding: str | None
    material_role: PreparedMaterialRole
    published_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != PREPARED_RESUME_MATERIAL_CONTRACT_VERSION:
            raise ValueError("publication contract is unsupported")
        subject = _clean_text("subject_id", self.subject_id, maximum=160)
        plan_id = _clean_text(
            "application_plan_id", self.application_plan_id, maximum=160
        )
        job_id = _clean_text("job_id", self.job_id, maximum=160)
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be a positive integer")
        job_hash = _require_hash("job_content_hash", self.job_content_hash)
        draft_id = _clean_text(
            "tailored_resume_draft_id",
            self.tailored_resume_draft_id,
            maximum=160,
        )
        draft_hash = _require_hash(
            "tailored_resume_draft_hash", self.tailored_resume_draft_hash
        )
        qa_id = _clean_text(
            "fact_qa_result_id", self.fact_qa_result_id, maximum=160
        )
        qa_hash = _require_hash(
            "fact_qa_result_hash", self.fact_qa_result_hash
        )
        version_id = _clean_text(
            "latex_version_id", self.latex_version_id, maximum=160
        )
        source_hash = _require_hash(
            "latex_source_sha256", self.latex_source_sha256
        )
        compilation_id = _clean_text(
            "compilation_record_id",
            self.compilation_record_id,
            maximum=160,
        )
        compilation_binding = _require_hash(
            "compilation_binding", self.compilation_binding
        )
        _clean_text("pdf_reference", self.pdf_reference, maximum=400)
        pdf_hash = _require_hash("pdf_sha256", self.pdf_sha256)
        if type(self.pdf_byte_size) is not int or self.pdf_byte_size <= 0:
            raise ValueError("pdf_byte_size must be positive")
        if type(self.page_count) is not int or self.page_count < 1:
            raise ValueError("page_count must be at least one")
        visual_id = _clean_text(
            "visual_qa_result_id", self.visual_qa_result_id, maximum=160
        )
        visual_hash = _require_hash(
            "visual_qa_result_hash", self.visual_qa_result_hash
        )
        run_id = _optional_text(
            "layout_revision_run_id",
            self.layout_revision_run_id,
            maximum=160,
        )
        run_binding = _optional_hash(
            "layout_revision_run_binding",
            self.layout_revision_run_binding,
        )
        if (run_id is None) != (run_binding is None):
            raise ValueError(
                "a revision binding must be complete or absent"
            )
        role = PreparedMaterialRole(self.material_role)
        object.__setattr__(self, "material_role", role)
        expected = prepared_resume_material_id(
            contract_version=contract,
            subject_id=subject,
            application_plan_id=plan_id,
            job_id=job_id,
            job_revision=self.job_revision,
            job_content_hash=job_hash,
            tailored_resume_draft_id=draft_id,
            tailored_resume_draft_hash=draft_hash,
            fact_qa_result_id=qa_id,
            fact_qa_result_hash=qa_hash,
            latex_version_id=version_id,
            latex_source_sha256=source_hash,
            compilation_record_id=compilation_id,
            compilation_binding=compilation_binding,
            pdf_sha256=pdf_hash,
            visual_qa_result_id=visual_id,
            visual_qa_result_hash=visual_hash,
            layout_revision_run_id=run_id,
            layout_revision_run_binding=run_binding,
            material_role=role,
        )
        if (
            not isinstance(self.material_id, str)
            or _MATERIAL_ID_PATTERN.fullmatch(self.material_id) is None
            or self.material_id != expected
        ):
            raise ValueError("material_id does not match its binding")
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "subject_id", subject)
        object.__setattr__(self, "layout_revision_run_id", run_id)
        _require_aware("published_at", self.published_at)

    def content_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            **_identity_payload(
                contract_version=self.contract_version,
                subject_id=self.subject_id,
                application_plan_id=self.application_plan_id,
                job_id=self.job_id,
                job_revision=self.job_revision,
                job_content_hash=self.job_content_hash,
                tailored_resume_draft_id=self.tailored_resume_draft_id,
                tailored_resume_draft_hash=(
                    self.tailored_resume_draft_hash
                ),
                fact_qa_result_id=self.fact_qa_result_id,
                fact_qa_result_hash=self.fact_qa_result_hash,
                latex_version_id=self.latex_version_id,
                latex_source_sha256=self.latex_source_sha256,
                compilation_record_id=self.compilation_record_id,
                compilation_binding=self.compilation_binding,
                pdf_sha256=self.pdf_sha256,
                visual_qa_result_id=self.visual_qa_result_id,
                visual_qa_result_hash=self.visual_qa_result_hash,
                layout_revision_run_id=self.layout_revision_run_id,
                layout_revision_run_binding=(
                    self.layout_revision_run_binding
                ),
                material_role=self.material_role,
            ),
            "pdf_byte_size": self.pdf_byte_size,
            "pdf_reference": self.pdf_reference,
            "page_count": self.page_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "published_at": _rfc3339(self.published_at),
        }


@dataclass(frozen=True, slots=True)
class ApprovedResumeReuseMaterial:
    """One P2 material record that preserves an approved PDF byte-for-byte."""

    material_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    source_selection_decision_id: str
    source_selection_decision_hash: str
    source_resume_id: str
    source_candidate_version: str
    source_artifact_sha256: str
    pdf_reference: str
    pdf_sha256: str
    pdf_byte_size: int
    page_count: int
    material_role: PreparedMaterialRole
    published_at: datetime

    def __post_init__(self) -> None:
        if self.contract_version != (
            APPROVED_RESUME_REUSE_MATERIAL_CONTRACT_VERSION
        ):
            raise ValueError("approved-resume reuse contract is unsupported")
        for name, maximum in (
            ("subject_id", 160),
            ("application_plan_id", 160),
            ("job_id", 160),
            ("source_selection_decision_id", 160),
            ("source_resume_id", 160),
            ("source_candidate_version", 80),
            ("pdf_reference", 400),
        ):
            _clean_text(name, getattr(self, name), maximum=maximum)
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be a positive integer")
        for name in (
            "job_content_hash",
            "source_selection_decision_hash",
            "source_artifact_sha256",
            "pdf_sha256",
        ):
            _require_hash(name, getattr(self, name))
        if self.pdf_sha256 != self.source_artifact_sha256:
            raise ValueError("P2 reuse must preserve the selected PDF bytes")
        if type(self.pdf_byte_size) is not int or self.pdf_byte_size <= 0:
            raise ValueError("pdf_byte_size must be positive")
        if type(self.page_count) is not int or self.page_count < 1:
            raise ValueError("page_count must be at least one")
        object.__setattr__(
            self, "material_role", PreparedMaterialRole(self.material_role)
        )
        _require_aware("published_at", self.published_at)
        expected_id = "approved-resume-reuse-material-" + _canonical_hash(
            self.identity_dict()
        )
        if (
            _REUSE_MATERIAL_ID_PATTERN.fullmatch(self.material_id) is None
            or self.material_id != expected_id
        ):
            raise ValueError("approved-resume reuse material ID is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "contract_version": self.contract_version,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "material_role": self.material_role.value,
            "pdf_sha256": self.pdf_sha256,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_candidate_version": self.source_candidate_version,
            "source_resume_id": self.source_resume_id,
            "source_selection_decision_hash": (
                self.source_selection_decision_hash
            ),
            "source_selection_decision_id": (
                self.source_selection_decision_id
            ),
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            **self.identity_dict(),
            "page_count": self.page_count,
            "pdf_byte_size": self.pdf_byte_size,
            "pdf_reference": self.pdf_reference,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "published_at": _rfc3339(self.published_at),
        }


PreparedResumeMaterialRecord = (
    PreparedResumeMaterial | ApprovedResumeReuseMaterial
)


@dataclass(frozen=True, slots=True)
class PreparedResumeMaterialWriteResult:
    status: PreparedResumeMaterialWriteStatus
    material: PreparedResumeMaterialRecord | None
    reason_code: PreparedResumeMaterialFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = PreparedResumeMaterialWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PreparedResumeMaterialFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            PreparedResumeMaterialWriteStatus.CREATED,
            PreparedResumeMaterialWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(
                    self.material,
                    (PreparedResumeMaterial, ApprovedResumeReuseMaterial),
                )
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful publication write is invalid")
        elif self.material is not None or self.reason_code is None:
            raise ValueError("failed publication write is invalid")


@dataclass(frozen=True, slots=True)
class PreparedResumeMaterialReadResult:
    status: PreparedResumeMaterialReadStatus
    material: PreparedResumeMaterialRecord | None
    reason_code: PreparedResumeMaterialFailureReason | None = None

    def __post_init__(self) -> None:
        status = PreparedResumeMaterialReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PreparedResumeMaterialFailureReason(self.reason_code),
            )
        if status is PreparedResumeMaterialReadStatus.FOUND:
            if (
                not isinstance(
                    self.material,
                    (PreparedResumeMaterial, ApprovedResumeReuseMaterial),
                )
                or self.reason_code is not None
            ):
                raise ValueError("found publication read is invalid")
        elif status is PreparedResumeMaterialReadStatus.NOT_FOUND:
            if self.material is not None or self.reason_code is not None:
                raise ValueError("not-found publication read is invalid")
        elif (
            self.material is not None
            or self.reason_code
            is not PreparedResumeMaterialFailureReason
            .MATERIAL_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure publication read is invalid")


@runtime_checkable
class PreparedResumeMaterialRepository(Protocol):
    def save(
        self, material: PreparedResumeMaterialRecord
    ) -> PreparedResumeMaterialWriteResult:
        """Persist one immutable prepared resume material."""

    def get(
        self, *, subject_id: str, material_id: str
    ) -> PreparedResumeMaterialReadResult:
        """Read one subject-owned prepared resume material."""

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> PreparedResumeMaterialReadResult:
        """Resolve the current prepared resume for one plan."""


def _material_from_dict(value: Any) -> PreparedResumeMaterialRecord:
    if (
        isinstance(value, Mapping)
        and value.get("contract_version")
        == APPROVED_RESUME_REUSE_MATERIAL_CONTRACT_VERSION
    ):
        expected = {
            "application_plan_id",
            "contract_version",
            "job_content_hash",
            "job_id",
            "job_revision",
            "material_id",
            "material_role",
            "page_count",
            "pdf_byte_size",
            "pdf_reference",
            "pdf_sha256",
            "published_at",
            "source_artifact_sha256",
            "source_candidate_version",
            "source_resume_id",
            "source_selection_decision_hash",
            "source_selection_decision_id",
            "subject_id",
        }
        if set(value) != expected:
            raise ValueError("persisted approved-resume reuse material is invalid")
        return ApprovedResumeReuseMaterial(
            material_id=value["material_id"],
            contract_version=value["contract_version"],
            subject_id=value["subject_id"],
            application_plan_id=value["application_plan_id"],
            job_id=value["job_id"],
            job_revision=value["job_revision"],
            job_content_hash=value["job_content_hash"],
            source_selection_decision_id=value[
                "source_selection_decision_id"
            ],
            source_selection_decision_hash=value[
                "source_selection_decision_hash"
            ],
            source_resume_id=value["source_resume_id"],
            source_candidate_version=value["source_candidate_version"],
            source_artifact_sha256=value["source_artifact_sha256"],
            pdf_reference=value["pdf_reference"],
            pdf_sha256=value["pdf_sha256"],
            pdf_byte_size=value["pdf_byte_size"],
            page_count=value["page_count"],
            material_role=PreparedMaterialRole(value["material_role"]),
            published_at=_parse_timestamp(value["published_at"]),
        )
    expected = {
        "material_id",
        "contract_version",
        "subject_id",
        "application_plan_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "tailored_resume_draft_id",
        "tailored_resume_draft_hash",
        "fact_qa_result_id",
        "fact_qa_result_hash",
        "latex_version_id",
        "latex_source_sha256",
        "compilation_record_id",
        "compilation_binding",
        "pdf_reference",
        "pdf_sha256",
        "pdf_byte_size",
        "page_count",
        "visual_qa_result_id",
        "visual_qa_result_hash",
        "layout_revision_run_id",
        "layout_revision_run_binding",
        "material_role",
        "published_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted PreparedResumeMaterial is invalid")
    return PreparedResumeMaterial(
        material_id=value["material_id"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        tailored_resume_draft_id=value["tailored_resume_draft_id"],
        tailored_resume_draft_hash=value["tailored_resume_draft_hash"],
        fact_qa_result_id=value["fact_qa_result_id"],
        fact_qa_result_hash=value["fact_qa_result_hash"],
        latex_version_id=value["latex_version_id"],
        latex_source_sha256=value["latex_source_sha256"],
        compilation_record_id=value["compilation_record_id"],
        compilation_binding=value["compilation_binding"],
        pdf_reference=value["pdf_reference"],
        pdf_sha256=value["pdf_sha256"],
        pdf_byte_size=value["pdf_byte_size"],
        page_count=value["page_count"],
        visual_qa_result_id=value["visual_qa_result_id"],
        visual_qa_result_hash=value["visual_qa_result_hash"],
        layout_revision_run_id=value["layout_revision_run_id"],
        layout_revision_run_binding=value["layout_revision_run_binding"],
        material_role=PreparedMaterialRole(value["material_role"]),
        published_at=_parse_timestamp(value["published_at"]),
    )


class PrivateHomePreparedResumeMaterialRepository:
    """Immutable published resume materials with fail-closed PDF verification."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _subject_directory(self, subject_id: str) -> Path:
        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        return (
            self._home.paths.prepared_resume_materials
            / _subject_storage_key(cleaned)
        )

    def _path(self, subject_id: str, material_id: str) -> Path:
        if (
            not isinstance(material_id, str)
            or (
                _MATERIAL_ID_PATTERN.fullmatch(material_id) is None
                and _REUSE_MATERIAL_ID_PATTERN.fullmatch(material_id) is None
            )
        ):
            raise ValueError("material_id is invalid")
        return self._subject_directory(subject_id) / f"{material_id}.json"

    def _artifact_is_valid(
        self, material: PreparedResumeMaterialRecord
    ) -> bool:
        try:
            path = self._home.contained_path(material.pdf_reference)
            if path.is_symlink() or not path.is_file():
                return False
            if path.stat(follow_symlinks=False).st_size != (
                material.pdf_byte_size
            ):
                return False
            content = path.read_bytes()
        except (OSError, PrivateHomeError):
            return False
        return (
            hashlib.sha256(content).hexdigest() == material.pdf_sha256
            and content.startswith(b"%PDF-")
        )

    def get(
        self, *, subject_id: str, material_id: str
    ) -> PreparedResumeMaterialReadResult:
        path = self._path(subject_id, material_id)
        with self._lock:
            if not path.exists():
                return PreparedResumeMaterialReadResult(
                    status=PreparedResumeMaterialReadStatus.NOT_FOUND,
                    material=None,
                )
            if path.is_symlink() or not path.is_file():
                return PreparedResumeMaterialReadResult(
                    status=(
                        PreparedResumeMaterialReadStatus.INTEGRITY_FAILURE
                    ),
                    material=None,
                    reason_code=(
                        PreparedResumeMaterialFailureReason
                        .MATERIAL_INTEGRITY_FAILURE
                    ),
                )
            try:
                material = _material_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return PreparedResumeMaterialReadResult(
                    status=(
                        PreparedResumeMaterialReadStatus.INTEGRITY_FAILURE
                    ),
                    material=None,
                    reason_code=(
                        PreparedResumeMaterialFailureReason
                        .MATERIAL_INTEGRITY_FAILURE
                    ),
                )
            if (
                material.subject_id != subject_id.strip()
                or material.material_id != material_id
                or path.name != f"{material.material_id}.json"
                or not self._artifact_is_valid(material)
            ):
                return PreparedResumeMaterialReadResult(
                    status=(
                        PreparedResumeMaterialReadStatus.INTEGRITY_FAILURE
                    ),
                    material=None,
                    reason_code=(
                        PreparedResumeMaterialFailureReason
                        .MATERIAL_INTEGRITY_FAILURE
                    ),
                )
            return PreparedResumeMaterialReadResult(
                status=PreparedResumeMaterialReadStatus.FOUND,
                material=material,
            )

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> PreparedResumeMaterialReadResult:
        """Pick by stored publication time, then material ID; never by mtime."""

        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        plan_id = _clean_text(
            "application_plan_id", application_plan_id, maximum=160
        )
        directory = self._subject_directory(cleaned)
        if not directory.exists():
            return PreparedResumeMaterialReadResult(
                status=PreparedResumeMaterialReadStatus.NOT_FOUND,
                material=None,
            )
        if directory.is_symlink() or not directory.is_dir():
            return PreparedResumeMaterialReadResult(
                status=PreparedResumeMaterialReadStatus.INTEGRITY_FAILURE,
                material=None,
                reason_code=(
                    PreparedResumeMaterialFailureReason
                    .MATERIAL_INTEGRITY_FAILURE
                ),
            )
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            return PreparedResumeMaterialReadResult(
                status=PreparedResumeMaterialReadStatus.INTEGRITY_FAILURE,
                material=None,
                reason_code=(
                    PreparedResumeMaterialFailureReason
                    .MATERIAL_INTEGRITY_FAILURE
                ),
            )
        matches: list[PreparedResumeMaterial] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or (
                    _MATERIAL_ID_PATTERN.fullmatch(path.stem) is None
                    and _REUSE_MATERIAL_ID_PATTERN.fullmatch(path.stem)
                    is None
                )
            ):
                return PreparedResumeMaterialReadResult(
                    status=(
                        PreparedResumeMaterialReadStatus.INTEGRITY_FAILURE
                    ),
                    material=None,
                    reason_code=(
                        PreparedResumeMaterialFailureReason
                        .MATERIAL_INTEGRITY_FAILURE
                    ),
                )
            result = self.get(subject_id=cleaned, material_id=path.stem)
            if (
                result.status is not PreparedResumeMaterialReadStatus.FOUND
                or result.material is None
            ):
                return PreparedResumeMaterialReadResult(
                    status=(
                        PreparedResumeMaterialReadStatus.INTEGRITY_FAILURE
                    ),
                    material=None,
                    reason_code=(
                        PreparedResumeMaterialFailureReason
                        .MATERIAL_INTEGRITY_FAILURE
                    ),
                )
            if result.material.application_plan_id == plan_id:
                matches.append(result.material)
        if not matches:
            return PreparedResumeMaterialReadResult(
                status=PreparedResumeMaterialReadStatus.NOT_FOUND,
                material=None,
            )
        current = max(
            matches,
            key=lambda item: (
                item.published_at.astimezone(timezone.utc),
                item.material_id,
            ),
        )
        return PreparedResumeMaterialReadResult(
            status=PreparedResumeMaterialReadStatus.FOUND,
            material=current,
        )

    def save(
        self, material: PreparedResumeMaterialRecord
    ) -> PreparedResumeMaterialWriteResult:
        if not isinstance(
            material, (PreparedResumeMaterial, ApprovedResumeReuseMaterial)
        ):
            raise TypeError("material must be a prepared-resume record")
        path = self._path(material.subject_id, material.material_id)
        with self._lock:
            if not self._artifact_is_valid(material):
                return PreparedResumeMaterialWriteResult(
                    status=PreparedResumeMaterialWriteStatus.FAILED,
                    material=None,
                    reason_code=(
                        PreparedResumeMaterialFailureReason
                        .MATERIAL_INTEGRITY_FAILURE
                    ),
                    retryable=False,
                )
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            material.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return PreparedResumeMaterialWriteResult(
                    status=PreparedResumeMaterialWriteStatus.FAILED,
                    material=None,
                    reason_code=(
                        PreparedResumeMaterialFailureReason
                        .MATERIAL_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return PreparedResumeMaterialWriteResult(
                    status=PreparedResumeMaterialWriteStatus.CREATED,
                    material=material,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=material.subject_id,
                material_id=material.material_id,
            )
            if (
                existing.status is PreparedResumeMaterialReadStatus.FOUND
                and existing.material is not None
                and existing.material.content_dict()
                == material.content_dict()
            ):
                return PreparedResumeMaterialWriteResult(
                    status=PreparedResumeMaterialWriteStatus.UNCHANGED,
                    material=existing.material,
                    reason_code=None,
                    retryable=False,
                )
            return PreparedResumeMaterialWriteResult(
                status=PreparedResumeMaterialWriteStatus.FAILED,
                material=None,
                reason_code=(
                    PreparedResumeMaterialFailureReason
                    .MATERIAL_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class PublishPreparedResumeCommand:
    subject_id: str
    application_plan_id: str
    now: datetime
    resume_visual_qa_result_id: str | None = None
    resume_layout_revision_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class PublishApprovedResumeReuseCommand:
    subject_id: str
    application_plan_id: str
    resume_selection_decision_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class PublishPreparedResumeResult:
    status: PreparedResumeMaterialStatus
    subject_id: str
    application_plan_id: str
    material: PreparedResumeMaterialRecord | None
    write_result: PreparedResumeMaterialWriteResult | None
    reason_code: PreparedResumeMaterialFailureReason | None
    not_ready_reason: PreparedResumeMaterialNotReadyReason | None
    retryable: bool
    message: str
    stopped_source_lineage: PublicationStoppedSourceLineage | None = None

    def __post_init__(self) -> None:
        status = PreparedResumeMaterialStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PreparedResumeMaterialFailureReason(self.reason_code),
            )
        if self.not_ready_reason is not None:
            object.__setattr__(
                self,
                "not_ready_reason",
                PreparedResumeMaterialNotReadyReason(
                    self.not_ready_reason
                ),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if (
            self.stopped_source_lineage is not None
            and (
                not isinstance(
                    self.stopped_source_lineage,
                    PublicationStoppedSourceLineage,
                )
                or self.stopped_source_lineage.subject_id != self.subject_id
                or self.stopped_source_lineage.application_plan_id
                != self.application_plan_id
                or self.stopped_source_lineage.publication_stage
                is not ApplicationPreparationStage.RESUME_PUBLICATION
                or self.stopped_source_lineage.material_kind
                is not PublicationMaterialKind.RESUME
            )
        ):
            raise ValueError("stopped source lineage does not match publication")
        if status in {
            PreparedResumeMaterialStatus.CREATED,
            PreparedResumeMaterialStatus.UNCHANGED,
        }:
            expected = PreparedResumeMaterialWriteStatus(status.value)
            if (
                not isinstance(
                    self.material,
                    (PreparedResumeMaterial, ApprovedResumeReuseMaterial),
                )
                or not isinstance(
                    self.write_result, PreparedResumeMaterialWriteResult
                )
                or self.write_result.status is not expected
                or self.write_result.material != self.material
                or self.reason_code is not None
                or self.not_ready_reason is not None
                or self.retryable
                or self.stopped_source_lineage is not None
            ):
                raise ValueError("successful publication result is invalid")
        elif status is PreparedResumeMaterialStatus.NOT_READY:
            if (
                self.material is not None
                or self.write_result is not None
                or self.reason_code is not None
                or self.not_ready_reason is None
                or self.retryable
            ):
                raise ValueError("not-ready publication result is invalid")
        elif (
            self.material is not None
            or self.reason_code is None
            or self.not_ready_reason is not None
        ):
            raise ValueError("failed publication result is invalid")


def _failure(
    command: PublishPreparedResumeCommand | PublishApprovedResumeReuseCommand,
    reason: PreparedResumeMaterialFailureReason,
    *,
    retryable: bool = False,
) -> PublishPreparedResumeResult:
    return PublishPreparedResumeResult(
        status=PreparedResumeMaterialStatus.FAILED,
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
        material=None,
        write_result=None,
        reason_code=reason,
        not_ready_reason=None,
        retryable=retryable,
        message=f"Prepared resume publication failed: {reason.value}.",
    )


def _not_ready(
    command: PublishPreparedResumeCommand | PublishApprovedResumeReuseCommand,
    reason: PreparedResumeMaterialNotReadyReason,
    *,
    detail: str,
    stopped_source_lineage: PublicationStoppedSourceLineage | None = None,
) -> PublishPreparedResumeResult:
    return PublishPreparedResumeResult(
        status=PreparedResumeMaterialStatus.NOT_READY,
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        material=None,
        write_result=None,
        reason_code=None,
        not_ready_reason=reason,
        retryable=False,
        message=f"The prepared resume is not ready: {detail}",
        stopped_source_lineage=stopped_source_lineage,
    )


def publish_approved_resume_reuse(
    command: PublishApprovedResumeReuseCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    selection_repository: ResumeSelectionDecisionRepository,
    candidate_repository: ResumeCandidateRepository,
    material_repository: PreparedResumeMaterialRepository,
    home: PrivateHome | None = None,
) -> PublishPreparedResumeResult:
    """Publish the exact selected PDF for a P2 plan without rewriting it."""

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
        decision_id = _clean_text(
            "resume_selection_decision_id",
            command.resume_selection_decision_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, PreparedResumeMaterialFailureReason.INVALID_REQUEST
        )

    try:
        plan_read = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.APPLICATION_PLAN_NOT_FOUND,
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_read.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_read.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
        )
    if plan.priority_level is not ProposedPriorityLevel.P2:
        return _failure(
            command, PreparedResumeMaterialFailureReason.INVALID_REQUEST
        )

    try:
        selection_read = selection_repository.get(
            subject_id=subject_id, decision_id=decision_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE,
        )
    if selection_read.status is ResumeSelectionDecisionReadStatus.NOT_FOUND:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.SOURCE_SELECTION_MISSING,
        )
    if (
        selection_read.status is not ResumeSelectionDecisionReadStatus.FOUND
        or not isinstance(selection_read.decision, ResumeSelectionDecision)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE,
        )
    selection = selection_read.decision
    if (
        selection.subject_id != subject_id
        or selection.application_plan_id != plan.plan_id
        or selection.job_id != plan.job_id
        or selection.job_revision != plan.job_revision
        or selection.job_content_hash != plan.job_content_hash
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.SOURCE_SELECTION_AMBIGUOUS,
        )

    try:
        candidate_read = candidate_repository.get(
            subject_id=subject_id, resume_id=selection.source_resume_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE,
        )
    if (
        candidate_read.status is not ResumeCandidateReadStatus.FOUND
        or not isinstance(candidate_read.candidate, ResumeCandidate)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE,
        )
    candidate = candidate_read.candidate
    if (
        candidate.subject_id != subject_id
        or candidate.resume_id != selection.source_resume_id
        or candidate.contract_version != selection.source_candidate_version
        or candidate.artifact_sha256
        != selection.source_artifact_sha256
        or candidate.status is not ResumeCandidateStatus.SELECTABLE
        or candidate.artifact_type is not ResumeArtifactType.PDF
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE,
        )

    try:
        pdf_path = active_home.contained_path(candidate.artifact_reference)
        if pdf_path.is_symlink() or not pdf_path.is_file():
            raise ValueError("selected resume PDF is not a regular file")
        content = pdf_path.read_bytes()
        page_count = pdf_page_count(content)
    except (OSError, PrivateHomeError, TypeError, ValueError):
        return _failure(
            command, PreparedResumeMaterialFailureReason.PDF_UNREADABLE
        )
    if hashlib.sha256(content).hexdigest() != candidate.artifact_sha256:
        return _failure(
            command, PreparedResumeMaterialFailureReason.PDF_HASH_DRIFT
        )
    if not content.startswith(b"%PDF-") or page_count < 1:
        return _failure(
            command, PreparedResumeMaterialFailureReason.PDF_INVALID
        )

    identity = {
        "application_plan_id": plan.plan_id,
        "contract_version": APPROVED_RESUME_REUSE_MATERIAL_CONTRACT_VERSION,
        "job_content_hash": plan.job_content_hash,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "material_role": PreparedMaterialRole.RESUME.value,
        "pdf_sha256": candidate.artifact_sha256,
        "source_artifact_sha256": candidate.artifact_sha256,
        "source_candidate_version": candidate.contract_version,
        "source_resume_id": candidate.resume_id,
        "source_selection_decision_hash": selection.decision_content_hash,
        "source_selection_decision_id": selection.decision_id,
        "subject_id": subject_id,
    }
    try:
        material = ApprovedResumeReuseMaterial(
            material_id=(
                "approved-resume-reuse-material-"
                + _canonical_hash(identity)
            ),
            pdf_reference=candidate.artifact_reference,
            pdf_byte_size=len(content),
            page_count=page_count,
            published_at=now,
            **identity,
        )
    except (TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE,
        )
    try:
        write_result = material_repository.save(material)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_PERSISTENCE_FAILED,
            retryable=True,
        )
    if write_result.status is PreparedResumeMaterialWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or PreparedResumeMaterialFailureReason
            .MATERIAL_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
        )
    status = PreparedResumeMaterialStatus(write_result.status.value)
    return PublishPreparedResumeResult(
        status=status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        material=write_result.material,
        write_result=write_result,
        reason_code=None,
        not_ready_reason=None,
        retryable=False,
        message=(
            "The approved P2 resume was reused without byte changes."
        ),
    )


def publish_prepared_resume(
    command: PublishPreparedResumeCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    draft_repository: TailoredResumeDraftRepository,
    fact_qa_repository: ResumeFactQARepository,
    latex_version_repository: ResumeLatexVersionRepository,
    compilation_repository: ResumeCompilationRepository,
    visual_qa_repository: ResumeVisualQARepository,
    layout_revision_repository: ResumeLayoutRevisionRepository,
    material_repository: PreparedResumeMaterialRepository,
    home: PrivateHome | None = None,
) -> PublishPreparedResumeResult:
    """Record an already approved managed PDF as the plan's prepared resume."""

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
        now = _require_aware("now", command.now)
        direct_id = _optional_text(
            "resume_visual_qa_result_id",
            command.resume_visual_qa_result_id,
            maximum=160,
        )
        run_id = _optional_text(
            "resume_layout_revision_run_id",
            command.resume_layout_revision_run_id,
            maximum=160,
        )
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, PreparedResumeMaterialFailureReason.INVALID_REQUEST
        )
    if direct_id is not None and run_id is not None:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.SOURCE_SELECTION_AMBIGUOUS,
        )
    if direct_id is None and run_id is None:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.SOURCE_SELECTION_MISSING,
        )

    try:
        plan_read = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.APPLICATION_PLAN_NOT_FOUND,
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_read.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_read.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
        )

    run: ResumeLayoutRevisionRun | None = None
    visual_qa_id = direct_id
    if run_id is not None:
        try:
            run_read = layout_revision_repository.get(
                subject_id=subject_id, run_id=run_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                PreparedResumeMaterialFailureReason
                .REVISION_RUN_INTEGRITY_FAILURE,
            )
        if run_read.status is ResumeLayoutRevisionReadStatus.NOT_FOUND:
            return _failure(
                command,
                PreparedResumeMaterialFailureReason.REVISION_RUN_NOT_FOUND,
            )
        if (
            run_read.status is not ResumeLayoutRevisionReadStatus.FOUND
            or not isinstance(run_read.run, ResumeLayoutRevisionRun)
        ):
            return _failure(
                command,
                PreparedResumeMaterialFailureReason
                .REVISION_RUN_INTEGRITY_FAILURE,
            )
        run = run_read.run
        if run.subject_id != subject_id or run.application_plan_id != plan_id:
            return _not_ready(
                command,
                PreparedResumeMaterialNotReadyReason
                .REVISION_BINDING_MISMATCH,
                detail=(
                    "the revision run does not belong to this plan chain."
                ),
            )
        if (
            run.final_status is not ResumeLayoutRevisionStatus.CREATED
            or not run.attempts
            or run.attempts[-1].outcome
            is not ResumeLayoutAttemptOutcome.PASSED
        ):
            final_attempt = run.attempts[-1] if run.attempts else None
            source_artifact_id = (
                final_attempt.output_latex_version_id
                or final_attempt.input_latex_version_id
                if final_attempt is not None
                else run.final_latex_version_id
            )
            lineage = create_publication_stopped_source_lineage(
                subject_id=subject_id,
                application_plan_id=plan_id,
                publication_stage=(
                    ApplicationPreparationStage.RESUME_PUBLICATION
                ),
                material_kind=PublicationMaterialKind.RESUME,
                source_kind=(
                    PublicationStoppedSourceKind.LAYOUT_REVISION_STOP
                ),
                source_stage=(
                    ApplicationPreparationStage.RESUME_LAYOUT_REVISION
                ),
                source_result_id=run.run_id,
                source_outcome=PreparationStageOutcome.DEFERRED,
                source_contract_version=(
                    RESUME_LAYOUT_REVISION_CONTRACT_VERSION
                ),
                source_result_content_hash=run.run_content_hash,
                source_directive=(
                    PublicationBlockingDirective
                    .LAYOUT_REVISION_NOT_SUCCESSFUL
                ),
                source_artifact_id=source_artifact_id,
                source_artifact_version=(
                    str(final_attempt.attempt_number)
                    if final_attempt is not None
                    else None
                ),
                blocking_lineage_ids=(
                    final_attempt.blocking_finding_ids
                    if final_attempt is not None
                    else ()
                ),
            )
            return _not_ready(
                command,
                PreparedResumeMaterialNotReadyReason
                .REVISION_RUN_NOT_SUCCESSFUL,
                detail=(
                    "the layout revision run did not end in a passing "
                    f"visual QA ({run.final_status.value})."
                ),
                stopped_source_lineage=lineage,
            )
        visual_qa_id = run.final_visual_qa_result_id

    try:
        qa_read = visual_qa_repository.get(
            subject_id=subject_id, result_id=visual_qa_id or ""
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .VISUAL_QA_INTEGRITY_FAILURE,
        )
    if qa_read.status is ResumeVisualQAReadStatus.NOT_FOUND:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.VISUAL_QA_NOT_FOUND,
        )
    if (
        qa_read.status is not ResumeVisualQAReadStatus.FOUND
        or not isinstance(qa_read.result, ResumeVisualQAResult)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .VISUAL_QA_INTEGRITY_FAILURE,
        )
    visual_qa = qa_read.result
    if visual_qa.subject_id != subject_id:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .VISUAL_QA_INTEGRITY_FAILURE,
        )
    if visual_qa.verdict is not ResumeVisualQAVerdict.PASSED:
        try:
            visual_draft_read = draft_repository.get(
                subject_id=subject_id,
                draft_id=visual_qa.tailored_resume_draft_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                PreparedResumeMaterialFailureReason.DRAFT_INTEGRITY_FAILURE,
            )
        if (
            visual_draft_read.status
            is not TailoredResumeDraftReadStatus.FOUND
            or not isinstance(
                visual_draft_read.draft, TailoredResumeDraft
            )
        ):
            return _failure(
                command,
                PreparedResumeMaterialFailureReason.DRAFT_INTEGRITY_FAILURE,
            )
        visual_draft = visual_draft_read.draft
        if (
            visual_draft.subject_id != subject_id
            or visual_draft.application_plan_id != plan.plan_id
            or visual_draft.job_id != plan.job_id
            or visual_draft.job_revision != plan.job_revision
            or visual_draft.job_content_hash != plan.job_content_hash
            or visual_draft.draft_id
            != visual_qa.tailored_resume_draft_id
            or visual_draft.draft_content_hash
            != visual_qa.tailored_resume_draft_hash
        ):
            return _failure(
                command,
                PreparedResumeMaterialFailureReason
                .VISUAL_QA_INTEGRITY_FAILURE,
            )
        lineage = create_publication_stopped_source_lineage(
            subject_id=subject_id,
            application_plan_id=plan_id,
            publication_stage=ApplicationPreparationStage.RESUME_PUBLICATION,
            material_kind=PublicationMaterialKind.RESUME,
            source_kind=PublicationStoppedSourceKind.VISUAL_QA_DIRECTIVE,
            source_stage=ApplicationPreparationStage.RESUME_VISUAL_QA,
            source_result_id=visual_qa.result_id,
            source_outcome=PreparationStageOutcome.COMPLETED,
            source_contract_version=RESUME_VISUAL_QA_CONTRACT_VERSION,
            source_result_content_hash=visual_qa.result_content_hash,
            source_directive=(
                PublicationBlockingDirective.VISUAL_QA_REVISION_REQUIRED
            ),
            source_artifact_id=visual_qa.compilation_record_id,
            source_artifact_version=visual_qa.latex_version_id,
            source_artifact_content_hash=visual_qa.pdf_sha256,
            blocking_lineage_ids=tuple(
                finding.finding_id
                for finding in visual_qa.findings
                if finding.severity
                is ResumeVisualQAFindingSeverity.BLOCKING
            ),
        )
        return _not_ready(
            command,
            PreparedResumeMaterialNotReadyReason.VISUAL_QA_NOT_PASSED,
            detail=(
                "visual QA returned "
                f"{visual_qa.verdict.value} rather than PASSED."
            ),
            stopped_source_lineage=lineage,
        )
    if run is not None and (
        run.final_visual_qa_result_id != visual_qa.result_id
        or run.final_latex_version_id != visual_qa.latex_version_id
        or run.final_compilation_record_id
        != visual_qa.compilation_record_id
    ):
        return _not_ready(
            command,
            PreparedResumeMaterialNotReadyReason.REVISION_BINDING_MISMATCH,
            detail=(
                "the revision run's final lineage does not match its "
                "visual QA result."
            ),
        )

    try:
        compilation_read = compilation_repository.get(
            subject_id=subject_id,
            record_id=visual_qa.compilation_record_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .COMPILATION_INTEGRITY_FAILURE,
        )
    if compilation_read.status is ResumeCompilationReadStatus.NOT_FOUND:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.COMPILATION_NOT_FOUND,
        )
    if (
        compilation_read.status is not ResumeCompilationReadStatus.FOUND
        or not isinstance(compilation_read.record, ResumeCompilationRecord)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .COMPILATION_INTEGRITY_FAILURE,
        )
    compilation = compilation_read.record
    if (
        compilation.subject_id != subject_id
        or compilation.record_id != visual_qa.compilation_record_id
        or compilation.compilation_binding
        != visual_qa.compilation_binding
        or compilation.pdf_sha256 != visual_qa.pdf_sha256
        or compilation.latex_version_id != visual_qa.latex_version_id
        or compilation.latex_source_sha256
        != visual_qa.latex_source_sha256
    ):
        return _not_ready(
            command,
            PreparedResumeMaterialNotReadyReason
            .COMPILATION_BINDING_MISMATCH,
            detail="the compilation record does not match its visual QA.",
        )

    try:
        version_read = latex_version_repository.get(
            subject_id=subject_id,
            latex_version_id=compilation.latex_version_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .LATEX_VERSION_INTEGRITY_FAILURE,
        )
    if version_read.status is ResumeLatexVersionReadStatus.NOT_FOUND:
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.LATEX_VERSION_NOT_FOUND,
        )
    if (
        version_read.status is not ResumeLatexVersionReadStatus.FOUND
        or not isinstance(version_read.version, ResumeLatexVersion)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason
            .LATEX_VERSION_INTEGRITY_FAILURE,
        )
    version = version_read.version
    if (
        version.subject_id != subject_id
        or version.source_sha256 != compilation.latex_source_sha256
        or version.tailored_resume_draft_id
        != visual_qa.tailored_resume_draft_id
        or version.tailored_resume_draft_hash
        != visual_qa.tailored_resume_draft_hash
        or version.fact_qa_result_id is None
        or version.fact_qa_result_hash is None
    ):
        return _not_ready(
            command,
            PreparedResumeMaterialNotReadyReason
            .LATEX_VERSION_BINDING_MISMATCH,
            detail="the LaTeX version does not carry a complete binding.",
        )

    try:
        draft_read = draft_repository.get(
            subject_id=subject_id,
            draft_id=version.tailored_resume_draft_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    if draft_read.status is TailoredResumeDraftReadStatus.NOT_FOUND:
        return _failure(
            command, PreparedResumeMaterialFailureReason.DRAFT_NOT_FOUND
        )
    if (
        draft_read.status is not TailoredResumeDraftReadStatus.FOUND
        or not isinstance(draft_read.draft, TailoredResumeDraft)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    draft = draft_read.draft
    if (
        draft.subject_id != subject_id
        or draft.draft_content_hash != version.tailored_resume_draft_hash
        or draft.application_plan_id != plan.plan_id
        or draft.job_id != plan.job_id
        or draft.job_revision != plan.job_revision
        or draft.job_content_hash != plan.job_content_hash
    ):
        return _not_ready(
            command,
            PreparedResumeMaterialNotReadyReason.DRAFT_BINDING_MISMATCH,
            detail="the draft does not belong to this plan chain.",
        )

    try:
        fact_qa_read = fact_qa_repository.get(
            subject_id=subject_id,
            qa_result_id=version.fact_qa_result_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.FACT_QA_INTEGRITY_FAILURE,
        )
    if fact_qa_read.status is ResumeFactQAReadStatus.NOT_FOUND:
        return _failure(
            command, PreparedResumeMaterialFailureReason.FACT_QA_NOT_FOUND
        )
    if (
        fact_qa_read.status is not ResumeFactQAReadStatus.FOUND
        or not isinstance(fact_qa_read.qa_result, ResumeFactQAResult)
    ):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.FACT_QA_INTEGRITY_FAILURE,
        )
    fact_qa = fact_qa_read.qa_result
    if (
        fact_qa.subject_id != subject_id
        or fact_qa.qa_content_hash != version.fact_qa_result_hash
        or fact_qa.tailored_resume_draft_id != draft.draft_id
        or fact_qa.tailored_resume_draft_hash != draft.draft_content_hash
        or fact_qa.application_plan_id != plan.plan_id
    ):
        return _not_ready(
            command,
            PreparedResumeMaterialNotReadyReason.FACT_QA_BINDING_MISMATCH,
            detail=(
                "the fact-QA result does not cover this exact draft."
            ),
        )
    if fact_qa.verdict is not ResumeFactQAVerdict.PASSED:
        lineage = create_publication_stopped_source_lineage(
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            publication_stage=ApplicationPreparationStage.RESUME_PUBLICATION,
            material_kind=PublicationMaterialKind.RESUME,
            source_kind=PublicationStoppedSourceKind.FACT_QA_BLOCKER,
            source_stage=ApplicationPreparationStage.RESUME_FACT_QA,
            source_result_id=fact_qa.qa_result_id,
            source_outcome=PreparationStageOutcome.COMPLETED,
            source_contract_version=RESUME_FACT_QA_CONTRACT_VERSION,
            source_result_content_hash=fact_qa.qa_content_hash,
            source_directive=PublicationBlockingDirective.FACT_QA_BLOCKED,
            source_artifact_id=fact_qa.tailored_resume_draft_id,
            source_artifact_content_hash=(
                fact_qa.tailored_resume_draft_hash
            ),
            blocking_lineage_ids=tuple(
                finding.finding_id
                for finding in fact_qa.findings
                if finding.severity is ResumeFactQAFindingSeverity.BLOCKING
            )
            or (
                "resume-fact-qa-blocker-collection-"
                + _canonical_hash(
                    {
                        "qa_content_hash": fact_qa.qa_content_hash,
                        "qa_result_id": fact_qa.qa_result_id,
                    }
                ),
            ),
        )
        return _not_ready(
            command,
            PreparedResumeMaterialNotReadyReason.FACT_QA_NOT_PASSED,
            detail=(
                "fact QA returned "
                f"{fact_qa.verdict.value} rather than PASSED."
            ),
            stopped_source_lineage=lineage,
        )

    try:
        pdf_path = active_home.contained_path(compilation.pdf_reference)
        if pdf_path.is_symlink() or not pdf_path.is_file():
            raise ValueError("the managed PDF is not a regular file")
        content = pdf_path.read_bytes()
    except (OSError, PrivateHomeError, TypeError, ValueError):
        return _failure(
            command, PreparedResumeMaterialFailureReason.PDF_UNREADABLE
        )
    if hashlib.sha256(content).hexdigest() != compilation.pdf_sha256:
        return _failure(
            command, PreparedResumeMaterialFailureReason.PDF_HASH_DRIFT
        )
    if (
        not content.startswith(b"%PDF-")
        or len(content) != compilation.pdf_byte_size
        or pdf_page_count(content) != compilation.page_count
    ):
        return _failure(
            command, PreparedResumeMaterialFailureReason.PDF_INVALID
        )

    identity = {
        "contract_version": PREPARED_RESUME_MATERIAL_CONTRACT_VERSION,
        "subject_id": subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "tailored_resume_draft_id": draft.draft_id,
        "tailored_resume_draft_hash": draft.draft_content_hash,
        "fact_qa_result_id": fact_qa.qa_result_id,
        "fact_qa_result_hash": fact_qa.qa_content_hash,
        "latex_version_id": version.latex_version_id,
        "latex_source_sha256": version.source_sha256,
        "compilation_record_id": compilation.record_id,
        "compilation_binding": compilation.compilation_binding,
        "pdf_sha256": compilation.pdf_sha256,
        "visual_qa_result_id": visual_qa.result_id,
        "visual_qa_result_hash": visual_qa.result_content_hash,
        "layout_revision_run_id": run.run_id if run else None,
        "layout_revision_run_binding": run.run_binding if run else None,
        "material_role": PreparedMaterialRole.RESUME,
    }
    try:
        material = PreparedResumeMaterial(
            material_id=prepared_resume_material_id(**identity),
            pdf_reference=compilation.pdf_reference,
            pdf_byte_size=compilation.pdf_byte_size,
            page_count=compilation.page_count,
            published_at=now,
            **identity,
        )
    except (TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE,
        )

    try:
        write_result = material_repository.save(material)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedResumeMaterialFailureReason.MATERIAL_PERSISTENCE_FAILED,
            retryable=True,
        )
    if write_result.status is PreparedResumeMaterialWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or PreparedResumeMaterialFailureReason
            .MATERIAL_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
        )
    status = PreparedResumeMaterialStatus(write_result.status.value)
    return PublishPreparedResumeResult(
        status=status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        material=write_result.material,
        write_result=write_result,
        reason_code=None,
        not_ready_reason=None,
        retryable=False,
        message=(
            "The prepared resume was published for this plan."
            if status is PreparedResumeMaterialStatus.CREATED
            else "The existing prepared resume is unchanged."
        ),
    )


_PREPARED_RESUME_NOT_READY_REASON_MAP = {
    reason: PreparedResumePublicationStopReason[reason.name]
    for reason in PreparedResumeMaterialNotReadyReason
    if reason.name in PreparedResumePublicationStopReason.__members__
}
_PREPARED_RESUME_FAILURE_REASON_MAP = {
    reason: PreparedResumePublicationStopReason[reason.name]
    for reason in PreparedResumeMaterialFailureReason
}


def prepared_resume_publication_public_result(
    result: PublishPreparedResumeResult,
) -> PublicPreparationStageResult:
    """Adapt every authoritative P2a9 outcome to stage-result v2."""

    if not isinstance(result, PublishPreparedResumeResult):
        raise TypeError("result must be a prepared-resume publication result")
    stage = ApplicationPreparationStage.RESUME_PUBLICATION
    if result.status in {
        PreparedResumeMaterialStatus.CREATED,
        PreparedResumeMaterialStatus.UNCHANGED,
    }:
        if result.material is None:
            raise ValueError("successful publication has no material")
        constructor = (
            PublicPreparationStageResult.completed
            if result.status is PreparedResumeMaterialStatus.CREATED
            else PublicPreparationStageResult.unchanged
        )
        return constructor(
            stage=stage,
            result_id=result.material.material_id,
            result_content_hash=_canonical_hash(
                result.material.content_dict()
            ),
            outputs={
                "prepared_resume_material_id": result.material.material_id
            },
        )
    if result.status is PreparedResumeMaterialStatus.NOT_READY:
        if result.not_ready_reason is None:
            raise ValueError("not-ready publication has no reason")
        try:
            reason = _PREPARED_RESUME_NOT_READY_REASON_MAP[
                result.not_ready_reason
            ]
        except KeyError as error:
            raise ValueError(
                "unmapped prepared-resume not-ready reason"
            ) from error
        outcome = PreparationStageOutcome.DEFERRED
    else:
        if result.reason_code is None:
            raise ValueError("failed publication has no reason")
        try:
            reason = _PREPARED_RESUME_FAILURE_REASON_MAP[result.reason_code]
        except KeyError as error:
            raise ValueError(
                "unmapped prepared-resume failure reason"
            ) from error
        outcome = PreparationStageOutcome.FAILED
    stop_reason = PreparationStopReasonEnvelope(
        stage=stage,
        code=reason,
        contract_version=(
            PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION
        ),
        outcome=outcome,
    )
    constructor = (
        PublicPreparationStageResult.deferred
        if outcome is PreparationStageOutcome.DEFERRED
        else PublicPreparationStageResult.failed
    )
    lineage = result.stopped_source_lineage
    targeted_reasons = {
        PreparedResumeMaterialNotReadyReason.FACT_QA_NOT_PASSED,
        PreparedResumeMaterialNotReadyReason.VISUAL_QA_NOT_PASSED,
        PreparedResumeMaterialNotReadyReason.REVISION_RUN_NOT_SUCCESSFUL,
    }
    if (
        result.status is PreparedResumeMaterialStatus.NOT_READY
        and result.not_ready_reason in targeted_reasons
        and lineage is None
    ):
        raise ValueError(
            "targeted resume publication stop lacks source lineage"
        )
    return constructor(
        stage=stage,
        stop_reason=stop_reason,
        retryable=result.retryable,
        result_id=(
            lineage.publication_result_id if lineage is not None else None
        ),
        result_content_hash=(
            lineage.lineage_content_hash if lineage is not None else None
        ),
        outputs=(
            lineage.output_references() if lineage is not None else {}
        ),
    )


__all__ = [
    "APPROVED_RESUME_REUSE_MATERIAL_CONTRACT_VERSION",
    "PREPARED_RESUME_MATERIAL_CONTRACT_VERSION",
    "ApprovedResumeReuseMaterial",
    "PreparedMaterialRole",
    "PreparedResumeMaterial",
    "PreparedResumeMaterialFailureReason",
    "PreparedResumeMaterialNotReadyReason",
    "PreparedResumeMaterialReadResult",
    "PreparedResumeMaterialReadStatus",
    "PreparedResumeMaterialRecord",
    "PreparedResumeMaterialRepository",
    "PreparedResumeMaterialStatus",
    "PreparedResumeMaterialWriteResult",
    "PreparedResumeMaterialWriteStatus",
    "PrivateHomePreparedResumeMaterialRepository",
    "PublishApprovedResumeReuseCommand",
    "PublishPreparedResumeCommand",
    "PublishPreparedResumeResult",
    "prepared_resume_material_id",
    "prepared_resume_publication_public_result",
    "publish_approved_resume_reuse",
    "publish_prepared_resume",
]
