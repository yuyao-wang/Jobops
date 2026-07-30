"""Serial single-plan orchestration over public preparation Slice callables."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
from uuid import uuid4

from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .private_home import PrivateHome, PrivateHomeError
from .preparation_invocation import (
    PreparationInvocationBinding,
    PreparationInvocationBindingRef,
)


LEGACY_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION = (
    "single-job-application-preparation-orchestration-v1"
)
PREVIOUS_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION = (
    "single-job-application-preparation-orchestration-v2"
)
APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION = (
    "single-job-application-preparation-orchestration-v3"
)
LEGACY_PREPARATION_STAGE_RESULT_SCHEMA_VERSION = (
    "preparation-stage-result-v1"
)
PREVIOUS_PREPARATION_STAGE_RESULT_SCHEMA_VERSION = (
    "preparation-stage-result-v2"
)
PREPARATION_STAGE_RESULT_SCHEMA_VERSION = "preparation-stage-result-v3"
COMPILATION_SOURCE_RESOLUTION_LINEAGE_CONTRACT_VERSION = (
    "compilation-source-resolution-lineage-v1"
)
DOWNSTREAM_PREPARATION_STOP_LINEAGE_CONTRACT_VERSION = (
    "downstream-preparation-stop-lineage-v1"
)
BASE_LATEX_STOP_REASON_CONTRACT_VERSION = (
    "base-latex-selection-stop-reasons-v1"
)
BASE_RESUME_SELECTION_STOP_REASON_CONTRACT_VERSION = (
    "base-resume-selection-stop-reasons-v1"
)
SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION = (
    "source-resume-projection-stop-reasons-v1"
)
CANDIDATE_EVIDENCE_STOP_REASON_CONTRACT_VERSION = (
    "candidate-evidence-stop-reasons-v1"
)
TAILORED_RESUME_DRAFT_STOP_REASON_CONTRACT_VERSION = (
    "tailored-resume-draft-stop-reasons-v1"
)
RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION = (
    "resume-fact-qa-stop-reasons-v1"
)
COVER_LETTER_EVIDENCE_STOP_REASON_CONTRACT_VERSION = (
    "cover-letter-evidence-stop-reasons-v1"
)
COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION = (
    "cover-letter-draft-stop-reasons-v1"
)
COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION = (
    "cover-letter-fact-qa-stop-reasons-v1"
)
APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION = (
    "application-answers-stop-reasons-v1"
)
PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION = (
    "prepared-resume-publication-stop-reasons-v1"
)
RESUME_MANIFEST_ENTRY_STOP_REASON_CONTRACT_VERSION = (
    "resume-manifest-entry-stop-reasons-v1"
)
COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION = (
    "cover-letter-publication-stop-reasons-v1"
)
COVER_LETTER_MANIFEST_ENTRY_STOP_REASON_CONTRACT_VERSION = (
    "cover-letter-manifest-entry-stop-reasons-v1"
)
LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION = (
    "latex-construction-stop-reasons-v1"
)
LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION = (
    "latex-compilation-stop-reasons-v1"
)
RESUME_VISUAL_QA_STOP_REASON_CONTRACT_VERSION = (
    "resume-visual-qa-stop-reasons-v1"
)
RESUME_LAYOUT_REVISION_STOP_REASON_CONTRACT_VERSION = (
    "resume-layout-revision-stop-reasons-v1"
)
REQUIRED_MATERIAL_POLICY_ID = "required-application-materials-v1"
REQUIRED_MATERIAL_POLICY_VERSION = "required-application-materials-v1"

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_DOWNSTREAM_LINEAGE_ID_RE = re.compile(
    r"^downstream-preparation-stop-lineage-[a-f0-9]{64}$"
)
_RUN_ID_RE = re.compile(r"^application-preparation-run-[a-f0-9]{64}$")
_COMPILATION_ATTEMPT_ID_RE = re.compile(
    r"^resume-compilation-attempt-[a-f0-9]{64}$"
)
_COMPILATION_STOPPED_SOURCE_ID_RE = re.compile(
    r"^resume-compilation-stopped-source-[a-f0-9]{64}$"
)
RESUME_COMPILATION_STOPPED_SOURCE_CONTRACT_VERSION = (
    "resume-compilation-stopped-source-v1"
)
RESUME_COMPILATION_STOPPED_SOURCE_REF_VERSION = (
    "resume-compilation-stopped-source-ref-v1"
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


def _clean_text(name: str, value: Any, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
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


def _parse_time(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return _require_aware(
        name, datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


class ApplicationPreparationStage(StrEnum):
    BASE_RESUME_SELECTION = "BASE_RESUME_SELECTION"
    SOURCE_RESUME_PROJECTION = "SOURCE_RESUME_PROJECTION"
    RESUME_EVIDENCE = "RESUME_EVIDENCE"
    RESUME_TAILORING = "RESUME_TAILORING"
    RESUME_FACT_QA = "RESUME_FACT_QA"
    BASE_LATEX_SELECTION = "BASE_LATEX_SELECTION"
    LATEX_CONSTRUCTION = "LATEX_CONSTRUCTION"
    RESUME_COMPILATION = "RESUME_COMPILATION"
    RESUME_VISUAL_QA = "RESUME_VISUAL_QA"
    RESUME_LAYOUT_REVISION = "RESUME_LAYOUT_REVISION"
    RESUME_PUBLICATION = "RESUME_PUBLICATION"
    RESUME_MANIFEST = "RESUME_MANIFEST"
    COVER_LETTER_EVIDENCE = "COVER_LETTER_EVIDENCE"
    COVER_LETTER_DRAFT = "COVER_LETTER_DRAFT"
    COVER_LETTER_FACT_QA = "COVER_LETTER_FACT_QA"
    COVER_LETTER_PUBLICATION = "COVER_LETTER_PUBLICATION"
    COVER_LETTER_MANIFEST = "COVER_LETTER_MANIFEST"
    APPLICATION_ANSWERS = "APPLICATION_ANSWERS"


APPLICATION_PREPARATION_STAGE_ORDER = tuple(ApplicationPreparationStage)
PREPARATION_ASSEMBLY_LINEAGE_CONTRACT_VERSION = (
    "preparation-assembly-lineage-v1"
)


class PublicStageStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class PublicStageDirective(StrEnum):
    CONTINUE = "CONTINUE"
    PASSED = "PASSED"
    REVISION_REQUIRED = "REVISION_REQUIRED"


class PreparationStageExecutionStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    SKIPPED = "SKIPPED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class PreparationStageOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    SKIPPED = "SKIPPED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"
    LEGACY_UNTYPED = "LEGACY_UNTYPED"


class BaseLatexPreparationStopReason(StrEnum):
    USER_REQUIREMENT_UNSATISFIABLE = "USER_REQUIREMENT_UNSATISFIABLE"
    DECISION_INTEGRITY_FAILURE = "DECISION_INTEGRITY_FAILURE"


class LatexConstructionStopReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    FACT_QA_NOT_FOUND = "FACT_QA_NOT_FOUND"
    FACT_QA_INTEGRITY_FAILURE = "FACT_QA_INTEGRITY_FAILURE"
    FACT_QA_BINDING_MISMATCH = "FACT_QA_BINDING_MISMATCH"
    FACT_QA_NOT_PASSED = "FACT_QA_NOT_PASSED"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"
    DRAFT_BINDING_MISMATCH = "DRAFT_BINDING_MISMATCH"
    BASE_SELECTION_NOT_FOUND = "BASE_SELECTION_NOT_FOUND"
    BASE_SELECTION_INTEGRITY_FAILURE = (
        "BASE_SELECTION_INTEGRITY_FAILURE"
    )
    BASE_SELECTION_BINDING_MISMATCH = "BASE_SELECTION_BINDING_MISMATCH"
    BASE_VERSION_NOT_FOUND = "BASE_VERSION_NOT_FOUND"
    BASE_VERSION_UNREADABLE = "BASE_VERSION_UNREADABLE"
    TEMPLATE_UNAVAILABLE = "TEMPLATE_UNAVAILABLE"
    DRAFT_HAS_NO_CONTENT = "DRAFT_HAS_NO_CONTENT"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    CONSTRUCTION_OUTPUT_UNSAFE = "CONSTRUCTION_OUTPUT_UNSAFE"
    VERSION_REGISTRATION_FAILED = "VERSION_REGISTRATION_FAILED"
    RECORD_PERSISTENCE_FAILED = "RECORD_PERSISTENCE_FAILED"
    RECORD_INTEGRITY_FAILURE = "RECORD_INTEGRITY_FAILURE"


class LatexCompilationStopReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    CONSTRUCTION_RECORD_NOT_FOUND = "CONSTRUCTION_RECORD_NOT_FOUND"
    CONSTRUCTION_RECORD_INTEGRITY_FAILURE = (
        "CONSTRUCTION_RECORD_INTEGRITY_FAILURE"
    )
    CONSTRUCTION_BINDING_MISMATCH = "CONSTRUCTION_BINDING_MISMATCH"
    LATEX_VERSION_NOT_FOUND = "LATEX_VERSION_NOT_FOUND"
    LATEX_VERSION_INTEGRITY_FAILURE = "LATEX_VERSION_INTEGRITY_FAILURE"
    LATEX_VERSION_BINDING_MISMATCH = "LATEX_VERSION_BINDING_MISMATCH"
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    SOURCE_HASH_DRIFT = "SOURCE_HASH_DRIFT"
    SOURCE_CAPABILITY_REJECTED = "SOURCE_CAPABILITY_REJECTED"
    UNMANAGED_DEPENDENCY = "UNMANAGED_DEPENDENCY"
    COMPILER_UNAVAILABLE = "COMPILER_UNAVAILABLE"
    COMPILATION_ERROR = "COMPILATION_ERROR"
    COMPILATION_TIMEOUT = "COMPILATION_TIMEOUT"
    PDF_INVALID = "PDF_INVALID"
    ARTIFACT_PERSISTENCE_FAILED = "ARTIFACT_PERSISTENCE_FAILED"
    RECORD_PERSISTENCE_FAILED = "RECORD_PERSISTENCE_FAILED"
    RECORD_INTEGRITY_FAILURE = "RECORD_INTEGRITY_FAILURE"


class ResumeVisualQAStopReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    COMPILATION_RECORD_NOT_FOUND = "COMPILATION_RECORD_NOT_FOUND"
    COMPILATION_RECORD_INTEGRITY_FAILURE = (
        "COMPILATION_RECORD_INTEGRITY_FAILURE"
    )
    LATEX_VERSION_NOT_FOUND = "LATEX_VERSION_NOT_FOUND"
    LATEX_VERSION_INTEGRITY_FAILURE = "LATEX_VERSION_INTEGRITY_FAILURE"
    LATEX_VERSION_BINDING_MISMATCH = "LATEX_VERSION_BINDING_MISMATCH"
    CONSTRUCTION_RECORD_NOT_FOUND = "CONSTRUCTION_RECORD_NOT_FOUND"
    CONSTRUCTION_RECORD_INTEGRITY_FAILURE = (
        "CONSTRUCTION_RECORD_INTEGRITY_FAILURE"
    )
    CONSTRUCTION_BINDING_MISMATCH = "CONSTRUCTION_BINDING_MISMATCH"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"
    DRAFT_BINDING_MISMATCH = "DRAFT_BINDING_MISMATCH"
    PDF_UNREADABLE = "PDF_UNREADABLE"
    PDF_HASH_DRIFT = "PDF_HASH_DRIFT"
    PDF_PAGE_COUNT_MISMATCH = "PDF_PAGE_COUNT_MISMATCH"
    RENDERER_UNAVAILABLE = "RENDERER_UNAVAILABLE"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    AGENT_OUTPUT_UNRELIABLE = "AGENT_OUTPUT_UNRELIABLE"
    RESULT_PERSISTENCE_FAILED = "RESULT_PERSISTENCE_FAILED"
    RESULT_INTEGRITY_FAILURE = "RESULT_INTEGRITY_FAILURE"


class ResumeLayoutRevisionStopReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    VISUAL_QA_NOT_FOUND = "VISUAL_QA_NOT_FOUND"
    VISUAL_QA_INTEGRITY_FAILURE = "VISUAL_QA_INTEGRITY_FAILURE"
    VISUAL_QA_BINDING_MISMATCH = "VISUAL_QA_BINDING_MISMATCH"
    COMPILATION_NOT_FOUND = "COMPILATION_NOT_FOUND"
    COMPILATION_INTEGRITY_FAILURE = "COMPILATION_INTEGRITY_FAILURE"
    COMPILATION_BINDING_MISMATCH = "COMPILATION_BINDING_MISMATCH"
    LATEX_VERSION_NOT_FOUND = "LATEX_VERSION_NOT_FOUND"
    LATEX_VERSION_INTEGRITY_FAILURE = "LATEX_VERSION_INTEGRITY_FAILURE"
    LATEX_VERSION_BINDING_MISMATCH = "LATEX_VERSION_BINDING_MISMATCH"
    PROVENANCE_NOT_FOUND = "PROVENANCE_NOT_FOUND"
    PROVENANCE_INTEGRITY_FAILURE = "PROVENANCE_INTEGRITY_FAILURE"
    PROVENANCE_BINDING_MISMATCH = "PROVENANCE_BINDING_MISMATCH"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"
    DRAFT_BINDING_MISMATCH = "DRAFT_BINDING_MISMATCH"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    RENDERER_UNAVAILABLE = "RENDERER_UNAVAILABLE"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    REVISION_OUTPUT_UNSAFE = "REVISION_OUTPUT_UNSAFE"
    VERSION_REGISTRATION_FAILED = "VERSION_REGISTRATION_FAILED"
    COMPILATION_STOPPED = "COMPILATION_STOPPED"
    VISUAL_QA_DEFERRED = "VISUAL_QA_DEFERRED"
    VISUAL_QA_FAILED = "VISUAL_QA_FAILED"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    RECORD_PERSISTENCE_FAILED = "RECORD_PERSISTENCE_FAILED"
    RECORD_INTEGRITY_FAILURE = "RECORD_INTEGRITY_FAILURE"


class BaseResumeSelectionStopReason(StrEnum):
    NO_SELECTABLE_RESUME = "NO_SELECTABLE_RESUME"
    AGENT_SELECTION_UNSAFE = "AGENT_SELECTION_UNSAFE"
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_READ_FAILED = "JOB_READ_FAILED"
    JOB_BINDING_MISMATCH = "JOB_BINDING_MISMATCH"
    CANDIDATE_PROVIDER_FAILED = "CANDIDATE_PROVIDER_FAILED"
    OVERRIDE_INVALID = "OVERRIDE_INVALID"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    DECISION_PERSISTENCE_FAILED = "DECISION_PERSISTENCE_FAILED"
    DECISION_INTEGRITY_FAILURE = "DECISION_INTEGRITY_FAILURE"


class SourceResumeProjectionStopReason(StrEnum):
    FORMAT_UNSUPPORTED = "FORMAT_UNSUPPORTED"
    ARTIFACT_UNREADABLE = "ARTIFACT_UNREADABLE"
    INVALID_REQUEST = "INVALID_REQUEST"
    RESUME_NOT_FOUND = "RESUME_NOT_FOUND"
    RESUME_INTEGRITY_FAILURE = "RESUME_INTEGRITY_FAILURE"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    PROJECTION_PERSISTENCE_FAILED = "PROJECTION_PERSISTENCE_FAILED"
    PROJECTION_INTEGRITY_FAILURE = "PROJECTION_INTEGRITY_FAILURE"


class CandidateEvidenceSnapshotStopReason(StrEnum):
    NO_USABLE_EVIDENCE = "NO_USABLE_EVIDENCE"
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


class TailoredResumeDraftStopReason(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    AGENT_OUTPUT_UNSAFE = "AGENT_OUTPUT_UNSAFE"
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_READ_FAILED = "JOB_READ_FAILED"
    JOB_BINDING_MISMATCH = "JOB_BINDING_MISMATCH"
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
    EVIDENCE_SNAPSHOT_NOT_FOUND = "EVIDENCE_SNAPSHOT_NOT_FOUND"
    EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE = (
        "EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE"
    )
    EVIDENCE_SNAPSHOT_BINDING_MISMATCH = (
        "EVIDENCE_SNAPSHOT_BINDING_MISMATCH"
    )
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    DRAFT_PERSISTENCE_FAILED = "DRAFT_PERSISTENCE_FAILED"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"


class ResumeFactQAStopReason(StrEnum):
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    AGENT_OUTPUT_UNRELIABLE = "AGENT_OUTPUT_UNRELIABLE"
    INVALID_REQUEST = "INVALID_REQUEST"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"
    DRAFT_SUBJECT_MISMATCH = "DRAFT_SUBJECT_MISMATCH"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_BINDING_MISMATCH = (
        "APPLICATION_PLAN_BINDING_MISMATCH"
    )
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_READ_FAILED = "JOB_READ_FAILED"
    JOB_BINDING_MISMATCH = "JOB_BINDING_MISMATCH"
    RESUME_SELECTION_NOT_FOUND = "RESUME_SELECTION_NOT_FOUND"
    RESUME_SELECTION_INTEGRITY_FAILURE = (
        "RESUME_SELECTION_INTEGRITY_FAILURE"
    )
    RESUME_SELECTION_BINDING_MISMATCH = (
        "RESUME_SELECTION_BINDING_MISMATCH"
    )
    SOURCE_PROJECTION_NOT_FOUND = "SOURCE_PROJECTION_NOT_FOUND"
    SOURCE_PROJECTION_INTEGRITY_FAILURE = (
        "SOURCE_PROJECTION_INTEGRITY_FAILURE"
    )
    SOURCE_PROJECTION_BINDING_MISMATCH = (
        "SOURCE_PROJECTION_BINDING_MISMATCH"
    )
    EVIDENCE_SNAPSHOT_NOT_FOUND = "EVIDENCE_SNAPSHOT_NOT_FOUND"
    EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE = (
        "EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE"
    )
    EVIDENCE_SNAPSHOT_BINDING_MISMATCH = (
        "EVIDENCE_SNAPSHOT_BINDING_MISMATCH"
    )
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    QA_RESULT_PERSISTENCE_FAILED = "QA_RESULT_PERSISTENCE_FAILED"
    QA_RESULT_INTEGRITY_FAILURE = "QA_RESULT_INTEGRITY_FAILURE"


class CoverLetterEvidenceStopReason(StrEnum):
    NO_USABLE_EVIDENCE = "NO_USABLE_EVIDENCE"
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


class CoverLetterDraftStopReason(StrEnum):
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_READ_FAILED = "JOB_READ_FAILED"
    JOB_BINDING_MISMATCH = "JOB_BINDING_MISMATCH"
    EVIDENCE_SNAPSHOT_NOT_FOUND = "EVIDENCE_SNAPSHOT_NOT_FOUND"
    EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE = (
        "EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE"
    )
    EVIDENCE_SNAPSHOT_BINDING_MISMATCH = (
        "EVIDENCE_SNAPSHOT_BINDING_MISMATCH"
    )
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    AGENT_OUTPUT_UNSAFE = "AGENT_OUTPUT_UNSAFE"
    DRAFT_PERSISTENCE_FAILED = "DRAFT_PERSISTENCE_FAILED"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"


class CoverLetterFactQAStopReason(StrEnum):
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_READ_FAILED = "JOB_READ_FAILED"
    JOB_BINDING_MISMATCH = "JOB_BINDING_MISMATCH"
    EVIDENCE_SNAPSHOT_NOT_FOUND = "EVIDENCE_SNAPSHOT_NOT_FOUND"
    EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE = (
        "EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE"
    )
    EVIDENCE_SNAPSHOT_BINDING_MISMATCH = (
        "EVIDENCE_SNAPSHOT_BINDING_MISMATCH"
    )
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"
    DRAFT_BINDING_MISMATCH = "DRAFT_BINDING_MISMATCH"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    AGENT_OUTPUT_UNSAFE = "AGENT_OUTPUT_UNSAFE"
    RESULT_PERSISTENCE_FAILED = "RESULT_PERSISTENCE_FAILED"
    RESULT_INTEGRITY_FAILURE = "RESULT_INTEGRITY_FAILURE"


class ApplicationAnswersStopReason(StrEnum):
    NO_TRUSTED_FACTS = "NO_TRUSTED_FACTS"
    USER_FACT_REQUIRED = "USER_FACT_REQUIRED"
    USER_CHOICE_REQUIRED = "USER_CHOICE_REQUIRED"
    USER_ATTESTATION_REQUIRED = "USER_ATTESTATION_REQUIRED"
    USER_FACT_AND_CHOICE_REQUIRED = "USER_FACT_AND_CHOICE_REQUIRED"
    USER_FACT_AND_ATTESTATION_REQUIRED = (
        "USER_FACT_AND_ATTESTATION_REQUIRED"
    )
    USER_CHOICE_AND_ATTESTATION_REQUIRED = (
        "USER_CHOICE_AND_ATTESTATION_REQUIRED"
    )
    USER_FACT_CHOICE_AND_ATTESTATION_REQUIRED = (
        "USER_FACT_CHOICE_AND_ATTESTATION_REQUIRED"
    )
    NO_SAFE_AUTOMATABLE_ANSWER = "NO_SAFE_AUTOMATABLE_ANSWER"
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    FACT_SNAPSHOT_INTEGRITY_FAILURE = "FACT_SNAPSHOT_INTEGRITY_FAILURE"
    FACT_SNAPSHOT_SUBJECT_MISMATCH = "FACT_SNAPSHOT_SUBJECT_MISMATCH"
    FACT_VALUE_TYPE_MISMATCH = "FACT_VALUE_TYPE_MISMATCH"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    ANSWER_SET_INTEGRITY_FAILURE = "ANSWER_SET_INTEGRITY_FAILURE"


class PreparedResumePublicationStopReason(StrEnum):
    VISUAL_QA_NOT_PASSED = "VISUAL_QA_NOT_PASSED"
    REVISION_RUN_NOT_SUCCESSFUL = "REVISION_RUN_NOT_SUCCESSFUL"
    FACT_QA_NOT_PASSED = "FACT_QA_NOT_PASSED"
    DRAFT_BINDING_MISMATCH = "DRAFT_BINDING_MISMATCH"
    FACT_QA_BINDING_MISMATCH = "FACT_QA_BINDING_MISMATCH"
    LATEX_VERSION_BINDING_MISMATCH = "LATEX_VERSION_BINDING_MISMATCH"
    COMPILATION_BINDING_MISMATCH = "COMPILATION_BINDING_MISMATCH"
    REVISION_BINDING_MISMATCH = "REVISION_BINDING_MISMATCH"
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


class ResumeManifestEntryStopReason(StrEnum):
    PREPARED_RESUME_NOT_PUBLISHED = "PREPARED_RESUME_NOT_PUBLISHED"
    PREPARED_RESUME_PLAN_MISMATCH = "PREPARED_RESUME_PLAN_MISMATCH"
    PREPARED_RESUME_ROLE_MISMATCH = "PREPARED_RESUME_ROLE_MISMATCH"
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
    ARTIFACT_UNREADABLE = "ARTIFACT_UNREADABLE"
    ARTIFACT_HASH_DRIFT = "ARTIFACT_HASH_DRIFT"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    MANIFEST_PERSISTENCE_FAILED = "MANIFEST_PERSISTENCE_FAILED"
    MANIFEST_INTEGRITY_FAILURE = "MANIFEST_INTEGRITY_FAILURE"


class CoverLetterPublicationStopReason(StrEnum):
    FACT_QA_NOT_PASSED = "FACT_QA_NOT_PASSED"
    JOB_BINDING_MISMATCH = "JOB_BINDING_MISMATCH"
    DRAFT_BINDING_MISMATCH = "DRAFT_BINDING_MISMATCH"
    FACT_QA_BINDING_MISMATCH = "FACT_QA_BINDING_MISMATCH"
    COMPILER_UNAVAILABLE = "COMPILER_UNAVAILABLE"
    COMPILATION_ERROR = "COMPILATION_ERROR"
    LAYOUT_OVERFLOW = "LAYOUT_OVERFLOW"
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_READ_FAILED = "JOB_READ_FAILED"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"
    FACT_QA_INTEGRITY_FAILURE = "FACT_QA_INTEGRITY_FAILURE"
    TEMPLATE_INVALID = "TEMPLATE_INVALID"
    SOURCE_PERSISTENCE_FAILED = "SOURCE_PERSISTENCE_FAILED"
    PDF_INVALID = "PDF_INVALID"
    PDF_TEXT_MISMATCH = "PDF_TEXT_MISMATCH"
    ARTIFACT_PERSISTENCE_FAILED = "ARTIFACT_PERSISTENCE_FAILED"
    MATERIAL_PERSISTENCE_FAILED = "MATERIAL_PERSISTENCE_FAILED"
    MATERIAL_INTEGRITY_FAILURE = "MATERIAL_INTEGRITY_FAILURE"


class CoverLetterManifestEntryStopReason(StrEnum):
    PLAN_MATERIAL_MANIFEST_NOT_READY = "PLAN_MATERIAL_MANIFEST_NOT_READY"
    PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE = (
        "PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE"
    )
    PREPARED_COVER_LETTER_NOT_PUBLISHED = (
        "PREPARED_COVER_LETTER_NOT_PUBLISHED"
    )
    PREPARED_COVER_LETTER_PLAN_MISMATCH = (
        "PREPARED_COVER_LETTER_PLAN_MISMATCH"
    )
    PREPARED_COVER_LETTER_ROLE_MISMATCH = (
        "PREPARED_COVER_LETTER_ROLE_MISMATCH"
    )
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    PREPARED_COVER_LETTER_INTEGRITY_FAILURE = (
        "PREPARED_COVER_LETTER_INTEGRITY_FAILURE"
    )
    ARTIFACT_UNREADABLE = "ARTIFACT_UNREADABLE"
    ARTIFACT_HASH_DRIFT = "ARTIFACT_HASH_DRIFT"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    MANIFEST_PERSISTENCE_FAILED = "MANIFEST_PERSISTENCE_FAILED"
    MANIFEST_INTEGRITY_FAILURE = "MANIFEST_INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class PreparationStopReasonEnvelope:
    stage: ApplicationPreparationStage
    code: StrEnum
    contract_version: str
    outcome: PreparationStageOutcome
    diagnostic_code: str | None = None
    upstream_lineage_id: str | None = None

    def __post_init__(self) -> None:
        stage = ApplicationPreparationStage(self.stage)
        outcome = PreparationStageOutcome(self.outcome)
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "outcome", outcome)
        if outcome not in {
            PreparationStageOutcome.DEFERRED,
            PreparationStageOutcome.FAILED,
        }:
            raise ValueError("stop reason outcome must stop the stage")
        if not isinstance(self.code, StrEnum):
            raise TypeError("stop reason code must be a typed enum member")
        contract = _STOP_REASON_CONTRACTS.get(stage)
        if contract is None:
            raise ValueError("stage has no registered stop reason contract")
        version, reason_type, outcomes = contract
        if (
            self.contract_version != version
            or type(self.code) is not reason_type
            or outcomes.get(self.code) is not outcome
        ):
            raise ValueError("stop reason does not match its stage contract")
        if self.diagnostic_code is not None:
            _clean_text("diagnostic_code", self.diagnostic_code, 120)
        if self.upstream_lineage_id is not None:
            _clean_text(
                "upstream_lineage_id", self.upstream_lineage_id, 240
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "contract_version": self.contract_version,
            "diagnostic_code": self.diagnostic_code,
            "outcome": self.outcome.value,
            "stage": self.stage.value,
            "upstream_lineage_id": self.upstream_lineage_id,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "PreparationStopReasonEnvelope":
        return _stop_reason_from_dict(value)


class UnresolvedCompilationSourceState(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    CONSTRUCTION_NOT_FOUND = "CONSTRUCTION_NOT_FOUND"
    CONSTRUCTION_INTEGRITY_FAILURE = "CONSTRUCTION_INTEGRITY_FAILURE"
    LATEX_VERSION_NOT_FOUND = "LATEX_VERSION_NOT_FOUND"
    LATEX_VERSION_INTEGRITY_FAILURE = "LATEX_VERSION_INTEGRITY_FAILURE"
    SOURCE_BINDING_REJECTED = "SOURCE_BINDING_REJECTED"


@dataclass(frozen=True, slots=True)
class ResolvedCompilationSourceLineage:
    contract_version: str
    invocation_binding_ref: PreparationInvocationBindingRef
    compilation_attempt_id: str
    subject_id: str
    application_plan_id: str
    construction_result_id: str
    latex_version_id: str
    source_content_hash: str
    source_contract_version: str

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != COMPILATION_SOURCE_RESOLUTION_LINEAGE_CONTRACT_VERSION
        ):
            raise ValueError(
                "compilation source-resolution contract is unsupported"
            )
        if not isinstance(
            self.invocation_binding_ref, PreparationInvocationBindingRef
        ):
            raise TypeError("compilation invocation reference must be typed")
        for name, maximum in (
            ("compilation_attempt_id", 240),
            ("subject_id", 160),
            ("application_plan_id", 180),
            ("construction_result_id", 240),
            ("latex_version_id", 240),
            ("source_contract_version", 120),
        ):
            _clean_text(name, getattr(self, name), maximum)
        if (
            _COMPILATION_ATTEMPT_ID_RE.fullmatch(
                self.compilation_attempt_id
            )
            is None
        ):
            raise ValueError("compilation attempt ID is invalid")
        _require_hash("source_content_hash", self.source_content_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "compilation_attempt_id": self.compilation_attempt_id,
            "construction_result_id": self.construction_result_id,
            "contract_version": self.contract_version,
            "invocation_binding_ref": self.invocation_binding_ref.to_dict(),
            "kind": "RESOLVED",
            "latex_version_id": self.latex_version_id,
            "source_content_hash": self.source_content_hash,
            "source_contract_version": self.source_contract_version,
            "subject_id": self.subject_id,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "ResolvedCompilationSourceLineage":
        parsed = _compilation_source_lineage_from_dict(value)
        if not isinstance(parsed, cls):
            raise ValueError("compilation source lineage is not resolved")
        return parsed


@dataclass(frozen=True, slots=True)
class UnresolvedCompilationSourceLineage:
    contract_version: str
    invocation_binding_ref: PreparationInvocationBindingRef
    compilation_attempt_id: str
    subject_id: str
    application_plan_id: str
    resolution_state: UnresolvedCompilationSourceState
    requested_construction_id: str | None = None
    requested_latex_version_id: str | None = None

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != COMPILATION_SOURCE_RESOLUTION_LINEAGE_CONTRACT_VERSION
        ):
            raise ValueError(
                "compilation source-resolution contract is unsupported"
            )
        if not isinstance(
            self.invocation_binding_ref, PreparationInvocationBindingRef
        ):
            raise TypeError("compilation invocation reference must be typed")
        object.__setattr__(
            self,
            "resolution_state",
            UnresolvedCompilationSourceState(self.resolution_state),
        )
        for name, maximum in (
            ("compilation_attempt_id", 240),
            ("subject_id", 160),
            ("application_plan_id", 180),
        ):
            _clean_text(name, getattr(self, name), maximum)
        if (
            _COMPILATION_ATTEMPT_ID_RE.fullmatch(
                self.compilation_attempt_id
            )
            is None
        ):
            raise ValueError("compilation attempt ID is invalid")
        for name in (
            "requested_construction_id",
            "requested_latex_version_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _clean_text(name, value, 240)

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "compilation_attempt_id": self.compilation_attempt_id,
            "contract_version": self.contract_version,
            "invocation_binding_ref": self.invocation_binding_ref.to_dict(),
            "kind": "UNRESOLVED",
            "requested_construction_id": self.requested_construction_id,
            "requested_latex_version_id": self.requested_latex_version_id,
            "resolution_state": self.resolution_state.value,
            "subject_id": self.subject_id,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "UnresolvedCompilationSourceLineage":
        parsed = _compilation_source_lineage_from_dict(value)
        if not isinstance(parsed, cls):
            raise ValueError("compilation source lineage is not unresolved")
        return parsed


CompilationSourceResolutionLineage = (
    ResolvedCompilationSourceLineage | UnresolvedCompilationSourceLineage
)


@dataclass(frozen=True, slots=True)
class ResumeCompilationStoppedSourceRef:
    record_id: str
    record_version: str
    record_hash: str
    reference_version: str = (
        RESUME_COMPILATION_STOPPED_SOURCE_REF_VERSION
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.record_id, str)
            or _COMPILATION_STOPPED_SOURCE_ID_RE.fullmatch(
                self.record_id
            )
            is None
        ):
            raise ValueError("compilation stopped-source ID is invalid")
        if (
            self.record_version
            != RESUME_COMPILATION_STOPPED_SOURCE_CONTRACT_VERSION
        ):
            raise ValueError(
                "compilation stopped-source contract is unsupported"
            )
        _require_hash("record_hash", self.record_hash)
        if self.record_id != (
            f"resume-compilation-stopped-source-{self.record_hash}"
        ):
            raise ValueError(
                "compilation stopped-source reference is inconsistent"
            )
        if (
            self.reference_version
            != RESUME_COMPILATION_STOPPED_SOURCE_REF_VERSION
        ):
            raise ValueError(
                "compilation stopped-source reference is unsupported"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "record_hash": self.record_hash,
            "record_id": self.record_id,
            "record_version": self.record_version,
            "reference_version": self.reference_version,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "ResumeCompilationStoppedSourceRef":
        expected = {
            "record_hash",
            "record_id",
            "record_version",
            "reference_version",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError(
                "compilation stopped-source reference is invalid"
            )
        return cls(
            record_id=value["record_id"],
            record_version=value["record_version"],
            record_hash=value["record_hash"],
            reference_version=value["reference_version"],
        )


def _compilation_source_lineage_from_dict(
    value: Mapping[str, Any],
) -> CompilationSourceResolutionLineage:
    if not isinstance(value, Mapping):
        raise TypeError("compilation source lineage must be a mapping")
    kind = value.get("kind")
    if kind == "RESOLVED":
        expected = {
            "application_plan_id",
            "compilation_attempt_id",
            "construction_result_id",
            "contract_version",
            "invocation_binding_ref",
            "kind",
            "latex_version_id",
            "source_content_hash",
            "source_contract_version",
            "subject_id",
        }
        if set(value) != expected:
            raise ValueError("resolved compilation lineage is invalid")
        return ResolvedCompilationSourceLineage(
            contract_version=value["contract_version"],
            invocation_binding_ref=PreparationInvocationBindingRef.from_dict(
                value["invocation_binding_ref"]
            ),
            compilation_attempt_id=value["compilation_attempt_id"],
            subject_id=value["subject_id"],
            application_plan_id=value["application_plan_id"],
            construction_result_id=value["construction_result_id"],
            latex_version_id=value["latex_version_id"],
            source_content_hash=value["source_content_hash"],
            source_contract_version=value["source_contract_version"],
        )
    if kind == "UNRESOLVED":
        expected = {
            "application_plan_id",
            "compilation_attempt_id",
            "contract_version",
            "invocation_binding_ref",
            "kind",
            "requested_construction_id",
            "requested_latex_version_id",
            "resolution_state",
            "subject_id",
        }
        if set(value) != expected:
            raise ValueError("unresolved compilation lineage is invalid")
        return UnresolvedCompilationSourceLineage(
            contract_version=value["contract_version"],
            invocation_binding_ref=PreparationInvocationBindingRef.from_dict(
                value["invocation_binding_ref"]
            ),
            compilation_attempt_id=value["compilation_attempt_id"],
            subject_id=value["subject_id"],
            application_plan_id=value["application_plan_id"],
            resolution_state=UnresolvedCompilationSourceState(
                value["resolution_state"]
            ),
            requested_construction_id=value["requested_construction_id"],
            requested_latex_version_id=value[
                "requested_latex_version_id"
            ],
        )
    raise ValueError("compilation source lineage kind is invalid")


@dataclass(frozen=True, slots=True)
class DownstreamPreparationStopLineage:
    lineage_id: str
    contract_version: str
    parent_stage: ApplicationPreparationStage
    parent_attempt_id: str
    subject_id: str
    application_plan_id: str
    child_stage: ApplicationPreparationStage
    child_stage_result_id: str
    child_stage_result_hash: str
    child_outcome: PreparationStageOutcome
    child_stop_reason: PreparationStopReasonEnvelope
    child_result_lineage_id: str | None

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != DOWNSTREAM_PREPARATION_STOP_LINEAGE_CONTRACT_VERSION
        ):
            raise ValueError("downstream stop lineage contract is unsupported")
        parent_stage = ApplicationPreparationStage(self.parent_stage)
        child_stage = ApplicationPreparationStage(self.child_stage)
        child_outcome = PreparationStageOutcome(self.child_outcome)
        object.__setattr__(self, "parent_stage", parent_stage)
        object.__setattr__(self, "child_stage", child_stage)
        object.__setattr__(self, "child_outcome", child_outcome)
        if child_outcome not in {
            PreparationStageOutcome.DEFERRED,
            PreparationStageOutcome.FAILED,
        }:
            raise ValueError("downstream lineage child did not stop")
        for name in (
            "parent_attempt_id",
            "subject_id",
            "application_plan_id",
            "child_stage_result_id",
        ):
            _clean_text(name, getattr(self, name), 240)
        _require_hash(
            "child_stage_result_hash", self.child_stage_result_hash
        )
        if (
            not isinstance(
                self.child_stop_reason, PreparationStopReasonEnvelope
            )
            or self.child_stop_reason.stage is not child_stage
            or self.child_stop_reason.outcome is not child_outcome
        ):
            raise ValueError("downstream child stop reason is invalid")
        if self.child_result_lineage_id is not None:
            _clean_text(
                "child_result_lineage_id",
                self.child_result_lineage_id,
                240,
            )
        content_hash = _canonical_hash(self.content_dict())
        if (
            _DOWNSTREAM_LINEAGE_ID_RE.fullmatch(self.lineage_id) is None
            or self.lineage_id
            != f"downstream-preparation-stop-lineage-{content_hash}"
        ):
            raise ValueError("downstream stop lineage ID is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "child_outcome": self.child_outcome.value,
            "child_result_lineage_id": self.child_result_lineage_id,
            "child_stage": self.child_stage.value,
            "child_stage_result_hash": self.child_stage_result_hash,
            "child_stage_result_id": self.child_stage_result_id,
            "child_stop_reason": self.child_stop_reason.to_dict(),
            "contract_version": self.contract_version,
            "parent_attempt_id": self.parent_attempt_id,
            "parent_stage": self.parent_stage.value,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"lineage_id": self.lineage_id, **self.content_dict()}

    def stage_output_references(self) -> dict[str, str]:
        values = {
            "downstream_application_plan_id": self.application_plan_id,
            "downstream_child_outcome": self.child_outcome.value,
            "downstream_child_reason_code": (
                self.child_stop_reason.code.value
            ),
            "downstream_child_reason_contract_version": (
                self.child_stop_reason.contract_version
            ),
            "downstream_child_stage": self.child_stage.value,
            "downstream_child_stage_result_hash": (
                self.child_stage_result_hash
            ),
            "downstream_child_stage_result_id": (
                self.child_stage_result_id
            ),
            "downstream_lineage_contract_version": self.contract_version,
            "downstream_lineage_id": self.lineage_id,
            "downstream_parent_attempt_id": self.parent_attempt_id,
            "downstream_parent_stage": self.parent_stage.value,
            "downstream_subject_id": self.subject_id,
        }
        if self.child_result_lineage_id is not None:
            values["downstream_child_result_lineage_id"] = (
                self.child_result_lineage_id
            )
        if self.child_stop_reason.diagnostic_code is not None:
            values["downstream_child_reason_diagnostic_code"] = (
                self.child_stop_reason.diagnostic_code
            )
        if self.child_stop_reason.upstream_lineage_id is not None:
            values["downstream_child_reason_upstream_lineage_id"] = (
                self.child_stop_reason.upstream_lineage_id
            )
        return values


_STOP_REASON_CONTRACTS: dict[
    ApplicationPreparationStage,
    tuple[
        str,
        type[StrEnum],
        Mapping[StrEnum, PreparationStageOutcome],
    ],
] = {
    ApplicationPreparationStage.BASE_RESUME_SELECTION: (
        BASE_RESUME_SELECTION_STOP_REASON_CONTRACT_VERSION,
        BaseResumeSelectionStopReason,
        {
            BaseResumeSelectionStopReason.NO_SELECTABLE_RESUME: (
                PreparationStageOutcome.DEFERRED
            ),
            BaseResumeSelectionStopReason.AGENT_SELECTION_UNSAFE: (
                PreparationStageOutcome.DEFERRED
            ),
            **{
                reason: PreparationStageOutcome.FAILED
                for reason in BaseResumeSelectionStopReason
                if reason
                not in {
                    BaseResumeSelectionStopReason.NO_SELECTABLE_RESUME,
                    BaseResumeSelectionStopReason.AGENT_SELECTION_UNSAFE,
                }
            },
        },
    ),
    ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: (
        SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION,
        SourceResumeProjectionStopReason,
        {
            SourceResumeProjectionStopReason.FORMAT_UNSUPPORTED: (
                PreparationStageOutcome.DEFERRED
            ),
            SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE: (
                PreparationStageOutcome.DEFERRED
            ),
            **{
                reason: PreparationStageOutcome.FAILED
                for reason in SourceResumeProjectionStopReason
                if reason
                not in {
                    SourceResumeProjectionStopReason.FORMAT_UNSUPPORTED,
                    SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE,
                }
            },
        },
    ),
    ApplicationPreparationStage.RESUME_EVIDENCE: (
        CANDIDATE_EVIDENCE_STOP_REASON_CONTRACT_VERSION,
        CandidateEvidenceSnapshotStopReason,
        {
            CandidateEvidenceSnapshotStopReason.NO_USABLE_EVIDENCE: (
                PreparationStageOutcome.DEFERRED
            ),
            **{
                reason: PreparationStageOutcome.FAILED
                for reason in CandidateEvidenceSnapshotStopReason
                if reason
                is not CandidateEvidenceSnapshotStopReason.NO_USABLE_EVIDENCE
            },
        },
    ),
    ApplicationPreparationStage.RESUME_TAILORING: (
        TAILORED_RESUME_DRAFT_STOP_REASON_CONTRACT_VERSION,
        TailoredResumeDraftStopReason,
        {
            TailoredResumeDraftStopReason.INSUFFICIENT_EVIDENCE: (
                PreparationStageOutcome.DEFERRED
            ),
            TailoredResumeDraftStopReason.AGENT_OUTPUT_UNSAFE: (
                PreparationStageOutcome.DEFERRED
            ),
            **{
                reason: PreparationStageOutcome.FAILED
                for reason in TailoredResumeDraftStopReason
                if reason
                not in {
                    TailoredResumeDraftStopReason.INSUFFICIENT_EVIDENCE,
                    TailoredResumeDraftStopReason.AGENT_OUTPUT_UNSAFE,
                }
            },
        },
    ),
    ApplicationPreparationStage.RESUME_FACT_QA: (
        RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        ResumeFactQAStopReason,
        {
            ResumeFactQAStopReason.UNSUPPORTED_CLAIM: (
                PreparationStageOutcome.DEFERRED
            ),
            ResumeFactQAStopReason.AGENT_OUTPUT_UNRELIABLE: (
                PreparationStageOutcome.DEFERRED
            ),
            **{
                reason: PreparationStageOutcome.FAILED
                for reason in ResumeFactQAStopReason
                if reason
                not in {
                    ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
                    ResumeFactQAStopReason.AGENT_OUTPUT_UNRELIABLE,
                }
            },
        },
    ),
    ApplicationPreparationStage.COVER_LETTER_EVIDENCE: (
        COVER_LETTER_EVIDENCE_STOP_REASON_CONTRACT_VERSION,
        CoverLetterEvidenceStopReason,
        {
            CoverLetterEvidenceStopReason.NO_USABLE_EVIDENCE: (
                PreparationStageOutcome.DEFERRED
            ),
            **{
                reason: PreparationStageOutcome.FAILED
                for reason in CoverLetterEvidenceStopReason
                if reason
                is not CoverLetterEvidenceStopReason.NO_USABLE_EVIDENCE
            },
        },
    ),
    ApplicationPreparationStage.COVER_LETTER_DRAFT: (
        COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION,
        CoverLetterDraftStopReason,
        {
            CoverLetterDraftStopReason.INSUFFICIENT_EVIDENCE: (
                PreparationStageOutcome.DEFERRED
            ),
            CoverLetterDraftStopReason.AGENT_OUTPUT_UNSAFE: (
                PreparationStageOutcome.DEFERRED
            ),
            **{
                reason: PreparationStageOutcome.FAILED
                for reason in CoverLetterDraftStopReason
                if reason
                not in {
                    CoverLetterDraftStopReason.INSUFFICIENT_EVIDENCE,
                    CoverLetterDraftStopReason.AGENT_OUTPUT_UNSAFE,
                }
            },
        },
    ),
    ApplicationPreparationStage.COVER_LETTER_FACT_QA: (
        COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        CoverLetterFactQAStopReason,
        {
            CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM: (
                PreparationStageOutcome.DEFERRED
            ),
            CoverLetterFactQAStopReason.AGENT_OUTPUT_UNSAFE: (
                PreparationStageOutcome.DEFERRED
            ),
            **{
                reason: PreparationStageOutcome.FAILED
                for reason in CoverLetterFactQAStopReason
                if reason
                not in {
                    CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM,
                    CoverLetterFactQAStopReason.AGENT_OUTPUT_UNSAFE,
                }
            },
        },
    ),
    ApplicationPreparationStage.APPLICATION_ANSWERS: (
        APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION,
        ApplicationAnswersStopReason,
        {
            reason: (
                PreparationStageOutcome.FAILED
                if reason
                in {
                    ApplicationAnswersStopReason.INVALID_REQUEST,
                    ApplicationAnswersStopReason.APPLICATION_PLAN_NOT_FOUND,
                    ApplicationAnswersStopReason
                    .APPLICATION_PLAN_INTEGRITY_FAILURE,
                    ApplicationAnswersStopReason
                    .APPLICATION_PLAN_SUBJECT_MISMATCH,
                    ApplicationAnswersStopReason
                    .FACT_SNAPSHOT_INTEGRITY_FAILURE,
                    ApplicationAnswersStopReason
                    .FACT_SNAPSHOT_SUBJECT_MISMATCH,
                    ApplicationAnswersStopReason.FACT_VALUE_TYPE_MISMATCH,
                    ApplicationAnswersStopReason.PERSISTENCE_FAILED,
                    ApplicationAnswersStopReason
                    .ANSWER_SET_INTEGRITY_FAILURE,
                }
                else PreparationStageOutcome.DEFERRED
            )
            for reason in ApplicationAnswersStopReason
        },
    ),
    ApplicationPreparationStage.RESUME_PUBLICATION: (
        PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
        PreparedResumePublicationStopReason,
        {
            reason: (
                PreparationStageOutcome.DEFERRED
                if reason
                in {
                    PreparedResumePublicationStopReason
                    .VISUAL_QA_NOT_PASSED,
                    PreparedResumePublicationStopReason
                    .REVISION_RUN_NOT_SUCCESSFUL,
                    PreparedResumePublicationStopReason.FACT_QA_NOT_PASSED,
                    PreparedResumePublicationStopReason
                    .DRAFT_BINDING_MISMATCH,
                    PreparedResumePublicationStopReason
                    .FACT_QA_BINDING_MISMATCH,
                    PreparedResumePublicationStopReason
                    .LATEX_VERSION_BINDING_MISMATCH,
                    PreparedResumePublicationStopReason
                    .COMPILATION_BINDING_MISMATCH,
                    PreparedResumePublicationStopReason
                    .REVISION_BINDING_MISMATCH,
                }
                else PreparationStageOutcome.FAILED
            )
            for reason in PreparedResumePublicationStopReason
        },
    ),
    ApplicationPreparationStage.RESUME_MANIFEST: (
        RESUME_MANIFEST_ENTRY_STOP_REASON_CONTRACT_VERSION,
        ResumeManifestEntryStopReason,
        {
            reason: (
                PreparationStageOutcome.DEFERRED
                if reason
                in {
                    ResumeManifestEntryStopReason
                    .PREPARED_RESUME_NOT_PUBLISHED,
                    ResumeManifestEntryStopReason
                    .PREPARED_RESUME_PLAN_MISMATCH,
                    ResumeManifestEntryStopReason
                    .PREPARED_RESUME_ROLE_MISMATCH,
                }
                else PreparationStageOutcome.FAILED
            )
            for reason in ResumeManifestEntryStopReason
        },
    ),
    ApplicationPreparationStage.COVER_LETTER_PUBLICATION: (
        COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
        CoverLetterPublicationStopReason,
        {
            reason: (
                PreparationStageOutcome.DEFERRED
                if reason
                in {
                    CoverLetterPublicationStopReason.FACT_QA_NOT_PASSED,
                    CoverLetterPublicationStopReason.JOB_BINDING_MISMATCH,
                    CoverLetterPublicationStopReason
                    .DRAFT_BINDING_MISMATCH,
                    CoverLetterPublicationStopReason
                    .FACT_QA_BINDING_MISMATCH,
                    CoverLetterPublicationStopReason.COMPILER_UNAVAILABLE,
                    CoverLetterPublicationStopReason.COMPILATION_ERROR,
                    CoverLetterPublicationStopReason.LAYOUT_OVERFLOW,
                }
                else PreparationStageOutcome.FAILED
            )
            for reason in CoverLetterPublicationStopReason
        },
    ),
    ApplicationPreparationStage.COVER_LETTER_MANIFEST: (
        COVER_LETTER_MANIFEST_ENTRY_STOP_REASON_CONTRACT_VERSION,
        CoverLetterManifestEntryStopReason,
        {
            reason: (
                PreparationStageOutcome.DEFERRED
                if reason
                in {
                    CoverLetterManifestEntryStopReason
                    .PLAN_MATERIAL_MANIFEST_NOT_READY,
                    CoverLetterManifestEntryStopReason
                    .PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE,
                    CoverLetterManifestEntryStopReason
                    .PREPARED_COVER_LETTER_NOT_PUBLISHED,
                    CoverLetterManifestEntryStopReason
                    .PREPARED_COVER_LETTER_PLAN_MISMATCH,
                    CoverLetterManifestEntryStopReason
                    .PREPARED_COVER_LETTER_ROLE_MISMATCH,
                }
                else PreparationStageOutcome.FAILED
            )
            for reason in CoverLetterManifestEntryStopReason
        },
    ),
    ApplicationPreparationStage.BASE_LATEX_SELECTION: (
        BASE_LATEX_STOP_REASON_CONTRACT_VERSION,
        BaseLatexPreparationStopReason,
        {
            BaseLatexPreparationStopReason
            .USER_REQUIREMENT_UNSATISFIABLE: (
                PreparationStageOutcome.DEFERRED
            ),
            BaseLatexPreparationStopReason.DECISION_INTEGRITY_FAILURE: (
                PreparationStageOutcome.FAILED
            ),
        },
    ),
    ApplicationPreparationStage.LATEX_CONSTRUCTION: (
        LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION,
        LatexConstructionStopReason,
        {
            reason: (
                PreparationStageOutcome.DEFERRED
                if reason
                in {
                    LatexConstructionStopReason.BASE_VERSION_UNREADABLE,
                    LatexConstructionStopReason.CONSTRUCTION_OUTPUT_UNSAFE,
                }
                else PreparationStageOutcome.FAILED
            )
            for reason in LatexConstructionStopReason
        },
    ),
    ApplicationPreparationStage.RESUME_COMPILATION: (
        LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION,
        LatexCompilationStopReason,
        {
            reason: (
                PreparationStageOutcome.DEFERRED
                if reason
                in {
                    LatexCompilationStopReason.UNMANAGED_DEPENDENCY,
                    LatexCompilationStopReason.COMPILER_UNAVAILABLE,
                    LatexCompilationStopReason.COMPILATION_ERROR,
                    LatexCompilationStopReason.COMPILATION_TIMEOUT,
                    LatexCompilationStopReason.PDF_INVALID,
                }
                else PreparationStageOutcome.FAILED
            )
            for reason in LatexCompilationStopReason
        },
    ),
    ApplicationPreparationStage.RESUME_VISUAL_QA: (
        RESUME_VISUAL_QA_STOP_REASON_CONTRACT_VERSION,
        ResumeVisualQAStopReason,
        {
            reason: (
                PreparationStageOutcome.DEFERRED
                if reason
                in {
                    ResumeVisualQAStopReason.RENDERER_UNAVAILABLE,
                    ResumeVisualQAStopReason.AGENT_OUTPUT_UNRELIABLE,
                }
                else PreparationStageOutcome.FAILED
            )
            for reason in ResumeVisualQAStopReason
        },
    ),
    ApplicationPreparationStage.RESUME_LAYOUT_REVISION: (
        RESUME_LAYOUT_REVISION_STOP_REASON_CONTRACT_VERSION,
        ResumeLayoutRevisionStopReason,
        {
            reason: (
                PreparationStageOutcome.DEFERRED
                if reason
                in {
                    ResumeLayoutRevisionStopReason.RENDERER_UNAVAILABLE,
                    ResumeLayoutRevisionStopReason.REVISION_OUTPUT_UNSAFE,
                    ResumeLayoutRevisionStopReason
                    .VERSION_REGISTRATION_FAILED,
                    ResumeLayoutRevisionStopReason.COMPILATION_STOPPED,
                    ResumeLayoutRevisionStopReason.VISUAL_QA_DEFERRED,
                    ResumeLayoutRevisionStopReason.VISUAL_QA_FAILED,
                    ResumeLayoutRevisionStopReason.ATTEMPTS_EXHAUSTED,
                }
                else PreparationStageOutcome.FAILED
            )
            for reason in ResumeLayoutRevisionStopReason
        },
    ),
}

_STAGE_CONTRACT_FAILURE_REASONS: Mapping[
    ApplicationPreparationStage, StrEnum
] = {
    ApplicationPreparationStage.BASE_RESUME_SELECTION: (
        BaseResumeSelectionStopReason.DECISION_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: (
        SourceResumeProjectionStopReason.PROJECTION_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.RESUME_EVIDENCE: (
        CandidateEvidenceSnapshotStopReason.SNAPSHOT_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.RESUME_TAILORING: (
        TailoredResumeDraftStopReason.DRAFT_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.RESUME_FACT_QA: (
        ResumeFactQAStopReason.QA_RESULT_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.BASE_LATEX_SELECTION: (
        BaseLatexPreparationStopReason.DECISION_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.LATEX_CONSTRUCTION: (
        LatexConstructionStopReason.RECORD_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.RESUME_COMPILATION: (
        LatexCompilationStopReason.RECORD_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.RESUME_VISUAL_QA: (
        ResumeVisualQAStopReason.RESULT_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.RESUME_LAYOUT_REVISION: (
        ResumeLayoutRevisionStopReason.RECORD_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.RESUME_PUBLICATION: (
        PreparedResumePublicationStopReason.MATERIAL_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.RESUME_MANIFEST: (
        ResumeManifestEntryStopReason.MANIFEST_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.COVER_LETTER_EVIDENCE: (
        CoverLetterEvidenceStopReason.SNAPSHOT_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.COVER_LETTER_DRAFT: (
        CoverLetterDraftStopReason.DRAFT_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.COVER_LETTER_FACT_QA: (
        CoverLetterFactQAStopReason.RESULT_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.COVER_LETTER_PUBLICATION: (
        CoverLetterPublicationStopReason.MATERIAL_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.COVER_LETTER_MANIFEST: (
        CoverLetterManifestEntryStopReason.MANIFEST_INTEGRITY_FAILURE
    ),
    ApplicationPreparationStage.APPLICATION_ANSWERS: (
        ApplicationAnswersStopReason.ANSWER_SET_INTEGRITY_FAILURE
    ),
}


def _stop_reason_from_dict(
    value: Mapping[str, Any],
) -> PreparationStopReasonEnvelope:
    if not isinstance(value, Mapping) or set(value) != {
        "code",
        "contract_version",
        "diagnostic_code",
        "outcome",
        "stage",
        "upstream_lineage_id",
    }:
        raise ValueError("persisted stop reason is invalid")
    stage = ApplicationPreparationStage(value["stage"])
    contract = _STOP_REASON_CONTRACTS.get(stage)
    if contract is None:
        raise ValueError("persisted stop reason stage is unregistered")
    _version, reason_type, _outcomes = contract
    return PreparationStopReasonEnvelope(
        stage=stage,
        code=reason_type(value["code"]),
        contract_version=value["contract_version"],
        outcome=PreparationStageOutcome(value["outcome"]),
        diagnostic_code=value["diagnostic_code"],
        upstream_lineage_id=value["upstream_lineage_id"],
    )


class ApplicationPreparationRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class ApplicationPreparationStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class ApplicationPreparationCompletedRole(StrEnum):
    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"
    APPLICATION_ANSWERS = "APPLICATION_ANSWERS"


class ApplicationPreparationFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    ORCHESTRATION_RECIPE_INVALID = "ORCHESTRATION_RECIPE_INVALID"
    PUBLIC_STAGE_CONTRACT_FAILURE = "PUBLIC_STAGE_CONTRACT_FAILURE"
    PUBLIC_STAGE_EXCEPTION = "PUBLIC_STAGE_EXCEPTION"
    RUN_INTEGRITY_FAILURE = "RUN_INTEGRITY_FAILURE"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class RequiredApplicationMaterialPolicy:
    policy_id: str
    policy_version: str
    cover_letter_required: bool
    policy_content_hash: str

    def __post_init__(self) -> None:
        if self.policy_id != REQUIRED_MATERIAL_POLICY_ID:
            raise ValueError("required-material policy ID is unsupported")
        if self.policy_version != REQUIRED_MATERIAL_POLICY_VERSION:
            raise ValueError("required-material policy version is unsupported")
        if self.cover_letter_required is not True:
            raise ValueError("V1 requires a cover letter")
        if self.policy_content_hash != _canonical_hash(
            self.content_dict()
        ):
            raise ValueError("required-material policy hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "cover_letter_required": self.cover_letter_required,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }

    @classmethod
    def v1(cls) -> "RequiredApplicationMaterialPolicy":
        content = {
            "cover_letter_required": True,
            "policy_id": REQUIRED_MATERIAL_POLICY_ID,
            "policy_version": REQUIRED_MATERIAL_POLICY_VERSION,
        }
        return cls(
            policy_id=REQUIRED_MATERIAL_POLICY_ID,
            policy_version=REQUIRED_MATERIAL_POLICY_VERSION,
            cover_letter_required=True,
            policy_content_hash=_canonical_hash(content),
        )


@dataclass(frozen=True, slots=True)
class ApplicationPreparationOutputReference:
    key: str
    value: str

    def __post_init__(self) -> None:
        _clean_text("output key", self.key, 100)
        _clean_text("output value", self.value, 240)

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "value": self.value}


def _ordered_outputs(
    values: Mapping[str, str],
) -> tuple[ApplicationPreparationOutputReference, ...]:
    if not isinstance(values, Mapping):
        raise TypeError("stage outputs must be a mapping")
    return tuple(
        ApplicationPreparationOutputReference(key=key, value=value)
        for key, value in sorted(values.items())
    )


@dataclass(frozen=True, slots=True)
class PublicPreparationStageResult:
    stage: ApplicationPreparationStage
    status: PublicStageStatus
    result_id: str | None
    result_content_hash: str | None
    outputs: tuple[ApplicationPreparationOutputReference, ...]
    schema_version: str
    outcome: PreparationStageOutcome
    stop_reason: PreparationStopReasonEnvelope | None = None
    compilation_source_lineage: (
        CompilationSourceResolutionLineage | None
    ) = None
    stopped_source_ref: ResumeCompilationStoppedSourceRef | None = None
    legacy_public_status: str | None = None
    legacy_reason_code: str | None = None
    retryable: bool = False
    human_attention_required: bool = False
    directive: PublicStageDirective = PublicStageDirective.CONTINUE

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stage", ApplicationPreparationStage(self.stage)
        )
        status = PublicStageStatus(self.status)
        object.__setattr__(self, "status", status)
        outcome = PreparationStageOutcome(self.outcome)
        object.__setattr__(self, "outcome", outcome)
        if self.schema_version != PREPARATION_STAGE_RESULT_SCHEMA_VERSION:
            raise ValueError("public stage-result schema is unsupported")
        if (
            not isinstance(self.outputs, tuple)
            or any(
                not isinstance(item, ApplicationPreparationOutputReference)
                for item in self.outputs
            )
            or tuple(sorted(self.outputs, key=lambda item: item.key))
            != self.outputs
            or len({item.key for item in self.outputs}) != len(self.outputs)
        ):
            raise ValueError("stage outputs must be unique and ordered")
        if type(self.retryable) is not bool or type(
            self.human_attention_required
        ) is not bool:
            raise TypeError("stage flags must be boolean")
        directive = PublicStageDirective(self.directive)
        object.__setattr__(self, "directive", directive)
        expected_status = {
            PreparationStageOutcome.COMPLETED: PublicStageStatus.CREATED,
            PreparationStageOutcome.UNCHANGED: PublicStageStatus.UNCHANGED,
            PreparationStageOutcome.DEFERRED: PublicStageStatus.DEFERRED,
            PreparationStageOutcome.FAILED: PublicStageStatus.FAILED,
        }.get(outcome)
        if outcome is PreparationStageOutcome.LEGACY_UNTYPED:
            _clean_text(
                "legacy_public_status", self.legacy_public_status, 120
            )
            if status in {
                PublicStageStatus.DEFERRED,
                PublicStageStatus.FAILED,
            }:
                _clean_text(
                    "legacy_reason_code", self.legacy_reason_code, 200
                )
            elif self.legacy_reason_code is not None:
                raise ValueError("legacy success cannot carry a reason")
            if self.stop_reason is not None:
                raise ValueError("legacy result cannot carry a typed reason")
        elif (
            expected_status is not status
            or self.legacy_public_status is not None
            or self.legacy_reason_code is not None
        ):
            raise ValueError("typed outcome conflicts with public status")
        if status in {PublicStageStatus.CREATED, PublicStageStatus.UNCHANGED}:
            _clean_text("result_id", self.result_id, 240)
            _require_hash("result_content_hash", self.result_content_hash)
            if self.stop_reason is not None or self.retryable:
                raise ValueError("successful public stage is malformed")
        else:
            if outcome is not PreparationStageOutcome.LEGACY_UNTYPED and (
                not isinstance(
                    self.stop_reason, PreparationStopReasonEnvelope
                )
                or self.stop_reason.stage is not self.stage
                or self.stop_reason.outcome is not outcome
            ):
                raise ValueError(
                    "stopped public stage needs a matching typed reason"
                )
            if (self.result_id is None) != (self.result_content_hash is None):
                raise ValueError("stopped result lineage must be complete")
            if self.result_id is not None:
                _clean_text("result_id", self.result_id, 240)
            if self.result_content_hash is not None:
                _require_hash(
                    "result_content_hash", self.result_content_hash
                )
            if self.outputs and self.result_id is None:
                raise ValueError("stopped outputs require result lineage")
        if (
            self.stage is not ApplicationPreparationStage.RESUME_VISUAL_QA
            and directive is not PublicStageDirective.CONTINUE
        ):
            raise ValueError("only Visual QA may direct revision")
        if self.compilation_source_lineage is not None and (
            self.stage is not ApplicationPreparationStage.RESUME_COMPILATION
            or status
            not in {PublicStageStatus.DEFERRED, PublicStageStatus.FAILED}
            or not isinstance(
                self.compilation_source_lineage,
                (
                    ResolvedCompilationSourceLineage,
                    UnresolvedCompilationSourceLineage,
                ),
            )
        ):
            raise ValueError(
                "compilation source lineage is attached to the wrong result"
            )
        if self.stopped_source_ref is not None and (
            self.stage is not ApplicationPreparationStage.RESUME_COMPILATION
            or status
            not in {PublicStageStatus.DEFERRED, PublicStageStatus.FAILED}
            or self.compilation_source_lineage is None
            or not isinstance(
                self.stopped_source_ref,
                ResumeCompilationStoppedSourceRef,
            )
        ):
            raise ValueError(
                "stopped-source reference is attached to the wrong result"
            )

    @property
    def public_status(self) -> str:
        if self.outcome is PreparationStageOutcome.LEGACY_UNTYPED:
            assert self.legacy_public_status is not None
            return self.legacy_public_status
        return self.outcome.value

    @property
    def reason_code(self) -> str | None:
        if self.stop_reason is not None:
            return self.stop_reason.code.value
        return self.legacy_reason_code

    @classmethod
    def completed(
        cls,
        *,
        stage: ApplicationPreparationStage,
        result_id: str,
        result_content_hash: str,
        outputs: Mapping[str, str],
        human_attention_required: bool = False,
        directive: PublicStageDirective = PublicStageDirective.CONTINUE,
    ) -> "PublicPreparationStageResult":
        return cls(
            stage=stage,
            status=PublicStageStatus.CREATED,
            result_id=result_id,
            result_content_hash=result_content_hash,
            outputs=_ordered_outputs(outputs),
            schema_version=PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            outcome=PreparationStageOutcome.COMPLETED,
            human_attention_required=human_attention_required,
            directive=directive,
        )

    @classmethod
    def unchanged(
        cls,
        *,
        stage: ApplicationPreparationStage,
        result_id: str,
        result_content_hash: str,
        outputs: Mapping[str, str],
        human_attention_required: bool = False,
        directive: PublicStageDirective = PublicStageDirective.CONTINUE,
    ) -> "PublicPreparationStageResult":
        return cls(
            stage=stage,
            status=PublicStageStatus.UNCHANGED,
            result_id=result_id,
            result_content_hash=result_content_hash,
            outputs=_ordered_outputs(outputs),
            schema_version=PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            outcome=PreparationStageOutcome.UNCHANGED,
            human_attention_required=human_attention_required,
            directive=directive,
        )

    @classmethod
    def deferred(
        cls,
        *,
        stage: ApplicationPreparationStage,
        stop_reason: PreparationStopReasonEnvelope,
        result_id: str | None = None,
        result_content_hash: str | None = None,
        outputs: Mapping[str, str] | None = None,
        retryable: bool = False,
        human_attention_required: bool = False,
        compilation_source_lineage: (
            CompilationSourceResolutionLineage | None
        ) = None,
        stopped_source_ref: ResumeCompilationStoppedSourceRef | None = None,
    ) -> "PublicPreparationStageResult":
        return cls(
            stage=stage,
            status=PublicStageStatus.DEFERRED,
            result_id=result_id,
            result_content_hash=result_content_hash,
            outputs=_ordered_outputs(outputs or {}),
            schema_version=PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            outcome=PreparationStageOutcome.DEFERRED,
            stop_reason=stop_reason,
            retryable=retryable,
            human_attention_required=human_attention_required,
            compilation_source_lineage=compilation_source_lineage,
            stopped_source_ref=stopped_source_ref,
        )

    @classmethod
    def failed(
        cls,
        *,
        stage: ApplicationPreparationStage,
        stop_reason: PreparationStopReasonEnvelope,
        result_id: str | None = None,
        result_content_hash: str | None = None,
        outputs: Mapping[str, str] | None = None,
        retryable: bool = False,
        human_attention_required: bool = False,
        compilation_source_lineage: (
            CompilationSourceResolutionLineage | None
        ) = None,
        stopped_source_ref: ResumeCompilationStoppedSourceRef | None = None,
    ) -> "PublicPreparationStageResult":
        return cls(
            stage=stage,
            status=PublicStageStatus.FAILED,
            result_id=result_id,
            result_content_hash=result_content_hash,
            outputs=_ordered_outputs(outputs or {}),
            schema_version=PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            outcome=PreparationStageOutcome.FAILED,
            stop_reason=stop_reason,
            retryable=retryable,
            human_attention_required=human_attention_required,
            compilation_source_lineage=compilation_source_lineage,
            stopped_source_ref=stopped_source_ref,
        )

    @classmethod
    def legacy_success(
        cls,
        *,
        stage: ApplicationPreparationStage,
        status: PublicStageStatus,
        public_status: str,
        result_id: str,
        result_content_hash: str,
        outputs: Mapping[str, str],
        human_attention_required: bool = False,
        directive: PublicStageDirective = PublicStageDirective.CONTINUE,
    ) -> "PublicPreparationStageResult":
        if status not in {
            PublicStageStatus.CREATED,
            PublicStageStatus.UNCHANGED,
        }:
            raise ValueError("legacy success requires a success status")
        return cls(
            stage=stage,
            status=status,
            result_id=result_id,
            result_content_hash=result_content_hash,
            outputs=_ordered_outputs(outputs),
            schema_version=PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            outcome=PreparationStageOutcome.LEGACY_UNTYPED,
            legacy_public_status=public_status,
            human_attention_required=human_attention_required,
            directive=directive,
        )

    @classmethod
    def legacy_stopped(
        cls,
        *,
        stage: ApplicationPreparationStage,
        status: PublicStageStatus,
        public_status: str,
        reason_code: str,
        retryable: bool = False,
        human_attention_required: bool = False,
    ) -> "PublicPreparationStageResult":
        if status not in {
            PublicStageStatus.DEFERRED,
            PublicStageStatus.FAILED,
        }:
            raise ValueError("legacy stop must defer or fail")
        return cls(
            stage=stage,
            status=status,
            result_id=None,
            result_content_hash=None,
            outputs=(),
            schema_version=PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            outcome=PreparationStageOutcome.LEGACY_UNTYPED,
            legacy_public_status=public_status,
            legacy_reason_code=reason_code,
            retryable=retryable,
            human_attention_required=human_attention_required,
        )


@dataclass(frozen=True, slots=True)
class ApplicationPreparationStageResult:
    stage: ApplicationPreparationStage
    execution_status: PreparationStageExecutionStatus
    result_id: str | None
    result_content_hash: str | None
    outputs: tuple[ApplicationPreparationOutputReference, ...]
    retryable: bool
    human_attention_required: bool
    stage_content_hash: str
    schema_version: str
    outcome: PreparationStageOutcome
    stop_reason: PreparationStopReasonEnvelope | None = None
    preparation_invocation_ref: PreparationInvocationBindingRef | None = None
    compilation_source_lineage: (
        CompilationSourceResolutionLineage | None
    ) = None
    stopped_source_ref: ResumeCompilationStoppedSourceRef | None = None
    legacy_public_status: str | None = None
    legacy_reason_code: str | None = None
    historical_legacy_serialization: bool = False
    historical_missing_stopped_source_ref: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stage", ApplicationPreparationStage(self.stage)
        )
        status = PreparationStageExecutionStatus(self.execution_status)
        object.__setattr__(self, "execution_status", status)
        outcome = PreparationStageOutcome(self.outcome)
        object.__setattr__(self, "outcome", outcome)
        if self.schema_version not in {
            LEGACY_PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            PREVIOUS_PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
        }:
            raise ValueError("stage-result schema is unsupported")
        if not isinstance(self.outputs, tuple) or tuple(
            sorted(self.outputs, key=lambda item: item.key)
        ) != self.outputs:
            raise ValueError("stage-result outputs are invalid")
        if type(self.retryable) is not bool or type(
            self.human_attention_required
        ) is not bool:
            raise TypeError("stage-result flags must be boolean")
        if type(self.historical_legacy_serialization) is not bool:
            raise TypeError("legacy serialization flag must be boolean")
        if type(self.historical_missing_stopped_source_ref) is not bool:
            raise TypeError("stopped-source compatibility flag is invalid")
        if self.historical_legacy_serialization != (
            self.schema_version
            == LEGACY_PREPARATION_STAGE_RESULT_SCHEMA_VERSION
        ):
            raise ValueError("legacy serialization marker is invalid")
        if self.schema_version == PREPARATION_STAGE_RESULT_SCHEMA_VERSION:
            if not isinstance(
                self.preparation_invocation_ref,
                PreparationInvocationBindingRef,
            ):
                raise ValueError(
                    "new stage result needs a preparation invocation"
                )
        elif (
            self.preparation_invocation_ref is not None
            or self.compilation_source_lineage is not None
            or self.stopped_source_ref is not None
        ):
            raise ValueError(
                "historical stage result cannot gain invocation lineage"
            )
        expected_execution = {
            PreparationStageOutcome.COMPLETED: (
                PreparationStageExecutionStatus.CREATED
            ),
            PreparationStageOutcome.UNCHANGED: (
                PreparationStageExecutionStatus.UNCHANGED
            ),
            PreparationStageOutcome.SKIPPED: (
                PreparationStageExecutionStatus.SKIPPED
            ),
            PreparationStageOutcome.DEFERRED: (
                PreparationStageExecutionStatus.DEFERRED
            ),
            PreparationStageOutcome.FAILED: (
                PreparationStageExecutionStatus.FAILED
            ),
        }.get(outcome)
        if outcome is PreparationStageOutcome.LEGACY_UNTYPED:
            _clean_text(
                "legacy_public_status", self.legacy_public_status, 120
            )
            if status in {
                PreparationStageExecutionStatus.DEFERRED,
                PreparationStageExecutionStatus.FAILED,
                PreparationStageExecutionStatus.SKIPPED,
            }:
                _clean_text(
                    "legacy_reason_code", self.legacy_reason_code, 200
                )
            elif self.legacy_reason_code is not None:
                raise ValueError("legacy success cannot carry a reason")
            if self.stop_reason is not None:
                raise ValueError("legacy result cannot carry typed reason")
        elif (
            expected_execution is not status
            or self.legacy_public_status is not None
            or self.legacy_reason_code is not None
        ):
            raise ValueError("typed outcome conflicts with execution status")
        if status in {
            PreparationStageExecutionStatus.CREATED,
            PreparationStageExecutionStatus.UNCHANGED,
        }:
            _clean_text("result_id", self.result_id, 240)
            _require_hash("result_content_hash", self.result_content_hash)
            if self.stop_reason is not None:
                raise ValueError("successful stage cannot have a reason")
        elif outcome in {
            PreparationStageOutcome.DEFERRED,
            PreparationStageOutcome.FAILED,
        } and (
            not isinstance(self.stop_reason, PreparationStopReasonEnvelope)
            or self.stop_reason.stage is not self.stage
            or self.stop_reason.outcome is not outcome
        ):
            raise ValueError("typed stopped stage needs matching reason")
        if self.compilation_source_lineage is not None:
            if (
                self.stage
                is not ApplicationPreparationStage.RESUME_COMPILATION
                or outcome
                not in {
                    PreparationStageOutcome.DEFERRED,
                    PreparationStageOutcome.FAILED,
                }
                or not isinstance(
                    self.compilation_source_lineage,
                    (
                        ResolvedCompilationSourceLineage,
                        UnresolvedCompilationSourceLineage,
                    ),
                )
                or self.compilation_source_lineage.invocation_binding_ref
                != self.preparation_invocation_ref
            ):
                raise ValueError("compilation source lineage is invalid")
        if self.stopped_source_ref is not None and (
            self.stage is not ApplicationPreparationStage.RESUME_COMPILATION
            or outcome
            not in {
                PreparationStageOutcome.DEFERRED,
                PreparationStageOutcome.FAILED,
            }
            or self.compilation_source_lineage is None
            or not isinstance(
                self.stopped_source_ref,
                ResumeCompilationStoppedSourceRef,
            )
        ):
            raise ValueError("compilation stopped-source reference is invalid")
        if (
            self.historical_missing_stopped_source_ref
            and self.schema_version
            != PREPARATION_STAGE_RESULT_SCHEMA_VERSION
        ):
            raise ValueError(
                "stopped-source compatibility marker is invalid"
            )
        if (
            self.stage is ApplicationPreparationStage.RESUME_COMPILATION
            and outcome
            in {
                PreparationStageOutcome.DEFERRED,
                PreparationStageOutcome.FAILED,
            }
            and self.schema_version
            == PREPARATION_STAGE_RESULT_SCHEMA_VERSION
            and self.compilation_source_lineage is None
            and (
                self.stop_reason is None
                or self.stop_reason.diagnostic_code is None
            )
        ):
            raise ValueError(
                "new stopped compilation needs source-resolution lineage"
            )
        if (
            self.stage is ApplicationPreparationStage.RESUME_COMPILATION
            and outcome
            in {
                PreparationStageOutcome.DEFERRED,
                PreparationStageOutcome.FAILED,
            }
            and self.schema_version
            == PREPARATION_STAGE_RESULT_SCHEMA_VERSION
            and self.compilation_source_lineage is not None
            and self.stopped_source_ref is None
            and not self.historical_missing_stopped_source_ref
            and (
                self.stop_reason is None
                or self.stop_reason.code
                not in {
                    LatexCompilationStopReason.RECORD_PERSISTENCE_FAILED,
                    LatexCompilationStopReason.RECORD_INTEGRITY_FAILURE,
                }
            )
        ):
            raise ValueError(
                "new stopped compilation needs a stopped-source reference"
            )
        if self.stage_content_hash != _canonical_hash(
            self.content_dict()
        ):
            raise ValueError("stage-result hash is invalid")

    @property
    def is_legacy_untyped(self) -> bool:
        return self.outcome is PreparationStageOutcome.LEGACY_UNTYPED

    @property
    def public_status(self) -> str:
        if self.is_legacy_untyped:
            assert self.legacy_public_status is not None
            return self.legacy_public_status
        return self.outcome.value

    @property
    def reason_code(self) -> str | None:
        if self.stop_reason is not None:
            return self.stop_reason.code.value
        return self.legacy_reason_code

    def content_dict(self) -> dict[str, Any]:
        if self.historical_legacy_serialization:
            return {
                "execution_status": self.execution_status.value,
                "human_attention_required": self.human_attention_required,
                "outputs": [item.to_dict() for item in self.outputs],
                "public_status": self.public_status,
                "reason_code": self.reason_code,
                "result_content_hash": self.result_content_hash,
                "result_id": self.result_id,
                "retryable": self.retryable,
                "stage": self.stage.value,
            }
        if (
            self.schema_version
            == PREVIOUS_PREPARATION_STAGE_RESULT_SCHEMA_VERSION
        ):
            return {
                "execution_status": self.execution_status.value,
                "human_attention_required": self.human_attention_required,
                "legacy_public_status": self.legacy_public_status,
                "legacy_reason_code": self.legacy_reason_code,
                "outcome": self.outcome.value,
                "outputs": [item.to_dict() for item in self.outputs],
                "result_content_hash": self.result_content_hash,
                "result_id": self.result_id,
                "retryable": self.retryable,
                "schema_version": self.schema_version,
                "stage": self.stage.value,
                "stop_reason": (
                    self.stop_reason.to_dict()
                    if self.stop_reason
                    else None
                ),
            }
        content = {
            "compilation_source_lineage": (
                self.compilation_source_lineage.to_dict()
                if self.compilation_source_lineage is not None
                else None
            ),
            "execution_status": self.execution_status.value,
            "human_attention_required": self.human_attention_required,
            "legacy_public_status": self.legacy_public_status,
            "legacy_reason_code": self.legacy_reason_code,
            "outcome": self.outcome.value,
            "outputs": [item.to_dict() for item in self.outputs],
            "preparation_invocation_ref": (
                self.preparation_invocation_ref.to_dict()
                if self.preparation_invocation_ref is not None
                else None
            ),
            "result_content_hash": self.result_content_hash,
            "result_id": self.result_id,
            "retryable": self.retryable,
            "schema_version": self.schema_version,
            "stage": self.stage.value,
            "stop_reason": (
                self.stop_reason.to_dict() if self.stop_reason else None
            ),
        }
        if not self.historical_missing_stopped_source_ref:
            content["stopped_source_ref"] = (
                self.stopped_source_ref.to_dict()
                if self.stopped_source_ref is not None
                else None
            )
        return content

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "stage_content_hash": self.stage_content_hash,
        }

    @classmethod
    def from_public(
        cls,
        result: PublicPreparationStageResult,
        *,
        preparation_invocation_ref: PreparationInvocationBindingRef,
    ) -> "ApplicationPreparationStageResult":
        if not isinstance(
            preparation_invocation_ref, PreparationInvocationBindingRef
        ):
            raise TypeError("preparation invocation reference must be typed")
        execution = PreparationStageExecutionStatus(result.status.value)
        content = {
            "compilation_source_lineage": (
                result.compilation_source_lineage.to_dict()
                if result.compilation_source_lineage is not None
                else None
            ),
            "execution_status": execution.value,
            "human_attention_required": result.human_attention_required,
            "legacy_public_status": result.legacy_public_status,
            "legacy_reason_code": result.legacy_reason_code,
            "outcome": result.outcome.value,
            "outputs": [item.to_dict() for item in result.outputs],
            "preparation_invocation_ref": (
                preparation_invocation_ref.to_dict()
            ),
            "result_content_hash": result.result_content_hash,
            "result_id": result.result_id,
            "retryable": result.retryable,
            "schema_version": PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            "stage": result.stage.value,
            "stop_reason": (
                result.stop_reason.to_dict()
                if result.stop_reason
                else None
            ),
            "stopped_source_ref": (
                result.stopped_source_ref.to_dict()
                if result.stopped_source_ref is not None
                else None
            ),
        }
        return cls(
            stage=result.stage,
            execution_status=execution,
            result_id=result.result_id,
            result_content_hash=result.result_content_hash,
            outputs=result.outputs,
            retryable=result.retryable,
            human_attention_required=result.human_attention_required,
            stage_content_hash=_canonical_hash(content),
            schema_version=PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            outcome=result.outcome,
            stop_reason=result.stop_reason,
            preparation_invocation_ref=preparation_invocation_ref,
            compilation_source_lineage=result.compilation_source_lineage,
            stopped_source_ref=result.stopped_source_ref,
            legacy_public_status=result.legacy_public_status,
            legacy_reason_code=result.legacy_reason_code,
        )

    @classmethod
    def skipped_layout(
        cls,
        *,
        preparation_invocation_ref: PreparationInvocationBindingRef,
    ) -> "ApplicationPreparationStageResult":
        content = {
            "compilation_source_lineage": None,
            "execution_status": PreparationStageExecutionStatus.SKIPPED.value,
            "human_attention_required": False,
            "legacy_public_status": None,
            "legacy_reason_code": None,
            "outcome": PreparationStageOutcome.SKIPPED.value,
            "outputs": [],
            "preparation_invocation_ref": (
                preparation_invocation_ref.to_dict()
            ),
            "result_content_hash": None,
            "result_id": None,
            "retryable": False,
            "schema_version": PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            "stage": ApplicationPreparationStage.RESUME_LAYOUT_REVISION.value,
            "stop_reason": None,
            "stopped_source_ref": None,
        }
        return cls(
            stage=ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
            execution_status=PreparationStageExecutionStatus.SKIPPED,
            result_id=None,
            result_content_hash=None,
            outputs=(),
            retryable=False,
            human_attention_required=False,
            stage_content_hash=_canonical_hash(content),
            schema_version=PREPARATION_STAGE_RESULT_SCHEMA_VERSION,
            outcome=PreparationStageOutcome.SKIPPED,
            preparation_invocation_ref=preparation_invocation_ref,
        )


@dataclass(frozen=True, slots=True)
class ApplicationPreparationStageRequest:
    stage: ApplicationPreparationStage
    subject_id: str
    application_plan_id: str
    job_id: str
    now: datetime
    outputs: tuple[ApplicationPreparationOutputReference, ...]
    prior_stage_results: tuple[ApplicationPreparationStageResult, ...]
    preparation_invocation_binding: PreparationInvocationBinding

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stage", ApplicationPreparationStage(self.stage)
        )
        _clean_text("subject_id", self.subject_id, 160)
        _clean_text("application_plan_id", self.application_plan_id, 180)
        _clean_text("job_id", self.job_id, 160)
        _require_aware("now", self.now)
        if tuple(sorted(self.outputs, key=lambda item: item.key)) != self.outputs:
            raise ValueError("request outputs must be ordered")
        if not isinstance(self.prior_stage_results, tuple):
            raise TypeError("prior_stage_results must be a tuple")
        if (
            not isinstance(
                self.preparation_invocation_binding,
                PreparationInvocationBinding,
            )
            or self.preparation_invocation_binding.subject_id
            != self.subject_id
            or self.preparation_invocation_binding.application_plan_id
            != self.application_plan_id
        ):
            raise ValueError("stage request invocation binding is invalid")

    def output(self, key: str) -> str:
        for item in self.outputs:
            if item.key == key:
                return item.value
        raise KeyError(key)


@runtime_checkable
class ApplicationPreparationPublicCallable(Protocol):
    def __call__(
        self, request: ApplicationPreparationStageRequest
    ) -> (
        PublicPreparationStageResult
        | Awaitable[PublicPreparationStageResult]
    ): ...


@dataclass(frozen=True, slots=True)
class ApplicationPreparationStageDefinition:
    stage: ApplicationPreparationStage
    public_callable_name: str
    slice_contract_version: str
    slice_policy_version: str
    configuration_hash: str
    invoke: Callable[
        [ApplicationPreparationStageRequest],
        PublicPreparationStageResult
        | Awaitable[PublicPreparationStageResult],
    ] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stage", ApplicationPreparationStage(self.stage)
        )
        _clean_text("public_callable_name", self.public_callable_name, 120)
        _clean_text("slice_contract_version", self.slice_contract_version)
        _clean_text("slice_policy_version", self.slice_policy_version)
        _require_hash("configuration_hash", self.configuration_hash)
        if not callable(self.invoke):
            raise TypeError("stage invoke must be callable")

    def identity_dict(self) -> dict[str, str]:
        return {
            "configuration_hash": self.configuration_hash,
            "public_callable_name": self.public_callable_name,
            "slice_contract_version": self.slice_contract_version,
            "slice_policy_version": self.slice_policy_version,
            "stage": self.stage.value,
        }


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRecipe:
    input_binding_hash: str
    stages: tuple[ApplicationPreparationStageDefinition, ...]
    required_material_policy: RequiredApplicationMaterialPolicy

    def __post_init__(self) -> None:
        _require_hash("input_binding_hash", self.input_binding_hash)
        if not isinstance(self.stages, tuple) or tuple(
            item.stage for item in self.stages
        ) != APPLICATION_PREPARATION_STAGE_ORDER:
            raise ValueError("recipe must define every stage in order")
        if not isinstance(
            self.required_material_policy,
            RequiredApplicationMaterialPolicy,
        ):
            raise TypeError("required material policy must be typed")

    @property
    def metadata_hash(self) -> str:
        return _canonical_hash(
            {
                "input_binding_hash": self.input_binding_hash,
                "required_material_policy_hash": (
                    self.required_material_policy.policy_content_hash
                ),
                "stages": [
                    item.identity_dict() for item in self.stages
                ],
            }
        )


_REQUIRED_OUTPUTS: dict[ApplicationPreparationStage, frozenset[str]] = {
    ApplicationPreparationStage.BASE_RESUME_SELECTION: frozenset(
        {"resume_selection_decision_id", "resume_id"}
    ),
    ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: frozenset(
        {"source_resume_projection_id"}
    ),
    ApplicationPreparationStage.RESUME_EVIDENCE: frozenset(
        {"resume_evidence_snapshot_id"}
    ),
    ApplicationPreparationStage.RESUME_TAILORING: frozenset(
        {"tailored_resume_draft_id"}
    ),
    ApplicationPreparationStage.RESUME_FACT_QA: frozenset(
        {"resume_fact_qa_result_id"}
    ),
    ApplicationPreparationStage.BASE_LATEX_SELECTION: frozenset(
        {"base_latex_selection_id"}
    ),
    ApplicationPreparationStage.LATEX_CONSTRUCTION: frozenset(
        {"latex_version_id", "latex_construction_record_id"}
    ),
    ApplicationPreparationStage.RESUME_COMPILATION: frozenset(
        {"compilation_record_id"}
    ),
    ApplicationPreparationStage.RESUME_VISUAL_QA: frozenset(
        {"visual_qa_result_id"}
    ),
    ApplicationPreparationStage.RESUME_LAYOUT_REVISION: frozenset(
        {
            "layout_revision_run_id",
            "latex_version_id",
            "compilation_record_id",
            "visual_qa_result_id",
        }
    ),
    ApplicationPreparationStage.RESUME_PUBLICATION: frozenset(
        {"prepared_resume_material_id"}
    ),
    ApplicationPreparationStage.RESUME_MANIFEST: frozenset(
        {"plan_material_manifest_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_EVIDENCE: frozenset(
        {"cover_letter_evidence_snapshot_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_DRAFT: frozenset(
        {"cover_letter_draft_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_FACT_QA: frozenset(
        {"cover_letter_fact_qa_result_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_PUBLICATION: frozenset(
        {"prepared_cover_letter_material_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_MANIFEST: frozenset(
        {"plan_material_manifest_id"}
    ),
    ApplicationPreparationStage.APPLICATION_ANSWERS: frozenset(
        {"prepared_application_answer_set_id"}
    ),
}


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRun:
    run_id: str
    contract_version: str
    preparation_binding: str
    recipe_metadata_hash: str
    required_material_policy_id: str
    required_material_policy_version: str
    required_material_policy_hash: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    stage_results: tuple[ApplicationPreparationStageResult, ...]
    final_plan_material_manifest_id: str | None
    final_prepared_application_answer_set_id: str | None
    completed_roles: tuple[ApplicationPreparationCompletedRole, ...]
    human_attention_required: bool
    deferred_stage: ApplicationPreparationStage | None
    deferred_reason: str | None
    failed_stage: ApplicationPreparationStage | None
    failed_reason: str | None
    overall_status: ApplicationPreparationRunStatus
    run_content_hash: str
    started_at: datetime
    completed_at: datetime
    preparation_invocation_binding: PreparationInvocationBinding | None = None

    def __post_init__(self) -> None:
        if self.contract_version not in {
            LEGACY_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
            PREVIOUS_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
        }:
            raise ValueError("orchestration contract is unsupported")
        for name, value in (
            ("preparation_binding", self.preparation_binding),
            ("recipe_metadata_hash", self.recipe_metadata_hash),
            (
                "required_material_policy_hash",
                self.required_material_policy_hash,
            ),
            ("job_content_hash", self.job_content_hash),
        ):
            _require_hash(name, value)
        expected_id = "application-preparation-run-" + _canonical_hash(
            self.identity_dict()
        )
        if (
            _RUN_ID_RE.fullmatch(self.run_id) is None
            or self.run_id != expected_id
        ):
            raise ValueError("preparation run ID is invalid")
        _clean_text("subject_id", self.subject_id, 160)
        _clean_text("application_plan_id", self.application_plan_id, 180)
        _clean_text("job_id", self.job_id, 160)
        if (
            self.contract_version
            == APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ):
            if (
                not isinstance(
                    self.preparation_invocation_binding,
                    PreparationInvocationBinding,
                )
                or self.preparation_invocation_binding.subject_id
                != self.subject_id
                or self.preparation_invocation_binding.application_plan_id
                != self.application_plan_id
                or any(
                    item.preparation_invocation_ref
                    != self.preparation_invocation_binding.reference
                    for item in self.stage_results
                )
            ):
                raise ValueError(
                    "run preparation invocation binding is invalid"
                )
            for item in self.stage_results:
                lineage = item.compilation_source_lineage
                if lineage is not None and (
                    lineage.subject_id != self.subject_id
                    or lineage.application_plan_id
                    != self.application_plan_id
                ):
                    raise ValueError(
                        "compilation source lineage is cross-boundary"
                    )
        elif self.preparation_invocation_binding is not None:
            raise ValueError(
                "historical run cannot gain an invocation binding"
            )
        if (
            self.required_material_policy_id
            != REQUIRED_MATERIAL_POLICY_ID
            or self.required_material_policy_version
            != REQUIRED_MATERIAL_POLICY_VERSION
        ):
            raise ValueError("run required-material policy is unsupported")
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job revision is invalid")
        if not isinstance(self.stage_results, tuple) or any(
            not isinstance(item, ApplicationPreparationStageResult)
            for item in self.stage_results
        ):
            raise TypeError("stage results must be typed")
        if (
            self.contract_version
            == LEGACY_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
            and any(
                not item.historical_legacy_serialization
                for item in self.stage_results
            )
        ) or (
            self.contract_version
            in {
                PREVIOUS_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
                APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
            }
            and any(
                item.historical_legacy_serialization
                for item in self.stage_results
            )
        ):
            raise ValueError(
                "run contract and stage-result schemas are inconsistent"
            )
        if tuple(item.stage for item in self.stage_results) != (
            APPLICATION_PREPARATION_STAGE_ORDER[: len(self.stage_results)]
        ):
            raise ValueError("stage lineage must be an exact ordered prefix")
        roles = tuple(
            ApplicationPreparationCompletedRole(item)
            for item in self.completed_roles
        )
        if roles != tuple(
            sorted(
                set(roles),
                key=lambda item: list(
                    ApplicationPreparationCompletedRole
                ).index(item),
            )
        ):
            raise ValueError("completed roles are invalid")
        object.__setattr__(self, "completed_roles", roles)
        successful_stages = {
            item.stage
            for item in self.stage_results
            if item.execution_status
            in {
                PreparationStageExecutionStatus.CREATED,
                PreparationStageExecutionStatus.UNCHANGED,
            }
        }
        expected_roles = tuple(
            role
            for role, stage in (
                (
                    ApplicationPreparationCompletedRole.RESUME,
                    ApplicationPreparationStage.RESUME_MANIFEST,
                ),
                (
                    ApplicationPreparationCompletedRole.COVER_LETTER,
                    ApplicationPreparationStage.COVER_LETTER_MANIFEST,
                ),
                (
                    ApplicationPreparationCompletedRole.APPLICATION_ANSWERS,
                    ApplicationPreparationStage.APPLICATION_ANSWERS,
                ),
            )
            if stage in successful_stages
        )
        if roles != expected_roles:
            raise ValueError("completed roles do not match stage lineage")
        if type(self.human_attention_required) is not bool:
            raise TypeError("human_attention_required must be boolean")
        if self.human_attention_required != any(
            item.human_attention_required for item in self.stage_results
        ):
            raise ValueError("human-attention summary conflicts with lineage")
        lineage_outputs: dict[str, str] = {}
        for item in self.stage_results:
            lineage_outputs.update(
                {output.key: output.value for output in item.outputs}
            )
        if self.final_plan_material_manifest_id != lineage_outputs.get(
            "plan_material_manifest_id"
        ) or self.final_prepared_application_answer_set_id != (
            lineage_outputs.get("prepared_application_answer_set_id")
        ):
            raise ValueError("final output IDs conflict with stage lineage")
        status = ApplicationPreparationRunStatus(self.overall_status)
        object.__setattr__(self, "overall_status", status)
        if status is ApplicationPreparationRunStatus.COMPLETED:
            if (
                len(self.stage_results)
                != len(APPLICATION_PREPARATION_STAGE_ORDER)
                or not self.final_plan_material_manifest_id
                or not self.final_prepared_application_answer_set_id
                or roles
                != tuple(ApplicationPreparationCompletedRole)
                or self.deferred_stage is not None
                or self.deferred_reason is not None
                or self.failed_stage is not None
                or self.failed_reason is not None
            ):
                raise ValueError("completed preparation run is incomplete")
        elif status is ApplicationPreparationRunStatus.DEFERRED:
            if (
                self.deferred_stage is None
                or self.deferred_reason is None
                or self.failed_stage is not None
                or self.failed_reason is not None
            ):
                raise ValueError("deferred preparation run is malformed")
        elif (
            self.failed_stage is None
            or self.failed_reason is None
            or self.deferred_stage is not None
            or self.deferred_reason is not None
        ):
            raise ValueError("failed preparation run is malformed")
        if status is not ApplicationPreparationRunStatus.COMPLETED:
            final_stage = self.stage_results[-1]
            expected_stage = (
                self.deferred_stage
                if status is ApplicationPreparationRunStatus.DEFERRED
                else self.failed_stage
            )
            expected_execution = (
                PreparationStageExecutionStatus.DEFERRED
                if status is ApplicationPreparationRunStatus.DEFERRED
                else PreparationStageExecutionStatus.FAILED
            )
            if (
                final_stage.stage is not expected_stage
                or final_stage.execution_status is not expected_execution
                or final_stage.reason_code
                != (
                    self.deferred_reason
                    if status is ApplicationPreparationRunStatus.DEFERRED
                    else self.failed_reason
                )
            ):
                raise ValueError("stopped outcome conflicts with stage lineage")
        started = _require_aware("started_at", self.started_at)
        completed = _require_aware("completed_at", self.completed_at)
        if completed < started:
            raise ValueError("completed_at precedes started_at")
        if self.run_content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("preparation run content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "completed_roles": [item.value for item in self.completed_roles],
            "contract_version": self.contract_version,
            "deferred_reason": self.deferred_reason,
            "deferred_stage": (
                self.deferred_stage.value if self.deferred_stage else None
            ),
            "failed_reason": self.failed_reason,
            "failed_stage": (
                self.failed_stage.value if self.failed_stage else None
            ),
            "final_plan_material_manifest_id": (
                self.final_plan_material_manifest_id
            ),
            "final_prepared_application_answer_set_id": (
                self.final_prepared_application_answer_set_id
            ),
            "human_attention_required": self.human_attention_required,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "overall_status": self.overall_status.value,
            "preparation_binding": self.preparation_binding,
            "recipe_metadata_hash": self.recipe_metadata_hash,
            "required_material_policy_hash": (
                self.required_material_policy_hash
            ),
            "required_material_policy_id": (
                self.required_material_policy_id
            ),
            "required_material_policy_version": (
                self.required_material_policy_version
            ),
            "stage_hashes": [
                item.stage_content_hash for item in self.stage_results
            ],
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        content = {
            "application_plan_id": self.application_plan_id,
            "completed_at": _rfc3339(self.completed_at),
            "completed_roles": [item.value for item in self.completed_roles],
            "contract_version": self.contract_version,
            "deferred_reason": self.deferred_reason,
            "deferred_stage": (
                self.deferred_stage.value if self.deferred_stage else None
            ),
            "failed_reason": self.failed_reason,
            "failed_stage": (
                self.failed_stage.value if self.failed_stage else None
            ),
            "final_plan_material_manifest_id": (
                self.final_plan_material_manifest_id
            ),
            "final_prepared_application_answer_set_id": (
                self.final_prepared_application_answer_set_id
            ),
            "human_attention_required": self.human_attention_required,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "overall_status": self.overall_status.value,
            "preparation_binding": self.preparation_binding,
            "recipe_metadata_hash": self.recipe_metadata_hash,
            "required_material_policy_hash": (
                self.required_material_policy_hash
            ),
            "required_material_policy_id": (
                self.required_material_policy_id
            ),
            "required_material_policy_version": (
                self.required_material_policy_version
            ),
            "run_id": self.run_id,
            "stage_results": [
                item.to_dict() for item in self.stage_results
            ],
            "started_at": _rfc3339(self.started_at),
            "subject_id": self.subject_id,
        }
        if (
            self.contract_version
            == APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ):
            content["preparation_invocation_binding"] = (
                self.preparation_invocation_binding.to_dict()
                if self.preparation_invocation_binding is not None
                else None
            )
        return content

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "run_content_hash": self.run_content_hash,
        }


class ApplicationPreparationRunReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ApplicationPreparationRunWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ApplicationPreparationRunListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRunReadResult:
    status: ApplicationPreparationRunReadStatus
    run: ApplicationPreparationRun | None


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRunWriteResult:
    status: ApplicationPreparationRunWriteStatus
    run: ApplicationPreparationRun | None
    reason_code: ApplicationPreparationFailureReason | None
    retryable: bool


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRunListResult:
    status: ApplicationPreparationRunListStatus
    runs: tuple[ApplicationPreparationRun, ...]


@runtime_checkable
class ApplicationPreparationRunRepository(Protocol):
    def get(
        self, *, subject_id: str, run_id: str
    ) -> ApplicationPreparationRunReadResult: ...

    def save(
        self, run: ApplicationPreparationRun
    ) -> ApplicationPreparationRunWriteResult: ...

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> ApplicationPreparationRunReadResult: ...

    def list_for_subject(
        self, *, subject_id: str
    ) -> ApplicationPreparationRunListResult: ...


def _stage_result_from_dict(
    value: Mapping[str, Any],
    *,
    run_contract_version: str,
) -> ApplicationPreparationStageResult:
    if (
        run_contract_version
        == LEGACY_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
    ):
        expected = {
            "execution_status",
            "human_attention_required",
            "outputs",
            "public_status",
            "reason_code",
            "result_content_hash",
            "result_id",
            "retryable",
            "stage",
            "stage_content_hash",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("legacy stage result is invalid")
        return ApplicationPreparationStageResult(
            stage=ApplicationPreparationStage(value["stage"]),
            execution_status=PreparationStageExecutionStatus(
                value["execution_status"]
            ),
            result_id=value["result_id"],
            result_content_hash=value["result_content_hash"],
            outputs=tuple(
                ApplicationPreparationOutputReference(
                    key=item["key"], value=item["value"]
                )
                for item in value["outputs"]
            ),
            retryable=value["retryable"],
            human_attention_required=value["human_attention_required"],
            stage_content_hash=value["stage_content_hash"],
            schema_version=(
                LEGACY_PREPARATION_STAGE_RESULT_SCHEMA_VERSION
            ),
            outcome=PreparationStageOutcome.LEGACY_UNTYPED,
            legacy_public_status=value["public_status"],
            legacy_reason_code=value["reason_code"],
            historical_legacy_serialization=True,
        )
    expected = {
        "execution_status",
        "human_attention_required",
        "legacy_public_status",
        "legacy_reason_code",
        "outcome",
        "outputs",
        "result_content_hash",
        "result_id",
        "retryable",
        "schema_version",
        "stage",
        "stage_content_hash",
        "stop_reason",
    }
    if (
        run_contract_version
        == APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
    ):
        current_expected = {
            *expected,
            "compilation_source_lineage",
            "preparation_invocation_ref",
            "stopped_source_ref",
        }
        historical_current_expected = current_expected - {
            "stopped_source_ref"
        }
        if not isinstance(value, Mapping) or frozenset(value) not in {
            frozenset(current_expected),
            frozenset(historical_current_expected),
        }:
            raise ValueError("typed stage result is invalid")
    elif not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("typed stage result is invalid")
    return ApplicationPreparationStageResult(
        stage=ApplicationPreparationStage(value["stage"]),
        execution_status=PreparationStageExecutionStatus(
            value["execution_status"]
        ),
        result_id=value["result_id"],
        result_content_hash=value["result_content_hash"],
        outputs=tuple(
            ApplicationPreparationOutputReference(
                key=item["key"], value=item["value"]
            )
            for item in value["outputs"]
        ),
        retryable=value["retryable"],
        human_attention_required=value["human_attention_required"],
        stage_content_hash=value["stage_content_hash"],
        schema_version=value["schema_version"],
        outcome=PreparationStageOutcome(value["outcome"]),
        stop_reason=(
            _stop_reason_from_dict(value["stop_reason"])
            if value["stop_reason"] is not None
            else None
        ),
        legacy_public_status=value["legacy_public_status"],
        legacy_reason_code=value["legacy_reason_code"],
        preparation_invocation_ref=(
            PreparationInvocationBindingRef.from_dict(
                value["preparation_invocation_ref"]
            )
            if "preparation_invocation_ref" in value
            else None
        ),
        compilation_source_lineage=(
            _compilation_source_lineage_from_dict(
                value["compilation_source_lineage"]
            )
            if value.get("compilation_source_lineage") is not None
            else None
        ),
        stopped_source_ref=(
            ResumeCompilationStoppedSourceRef.from_dict(
                value["stopped_source_ref"]
            )
            if value.get("stopped_source_ref") is not None
            else None
        ),
        historical_missing_stopped_source_ref=(
            run_contract_version
            == APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
            and "stopped_source_ref" not in value
        ),
    )


def _run_from_dict(value: Mapping[str, Any]) -> ApplicationPreparationRun:
    expected = {
        "application_plan_id",
        "completed_at",
        "completed_roles",
        "contract_version",
        "deferred_reason",
        "deferred_stage",
        "failed_reason",
        "failed_stage",
        "final_plan_material_manifest_id",
        "final_prepared_application_answer_set_id",
        "human_attention_required",
        "job_content_hash",
        "job_id",
        "job_revision",
        "overall_status",
        "preparation_binding",
        "recipe_metadata_hash",
        "required_material_policy_hash",
        "required_material_policy_id",
        "required_material_policy_version",
        "run_content_hash",
        "run_id",
        "stage_results",
        "started_at",
        "subject_id",
    }
    if (
        isinstance(value, Mapping)
        and value.get("contract_version")
        == APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
    ):
        expected = {*expected, "preparation_invocation_binding"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["stage_results"], list)
        or not isinstance(value["completed_roles"], list)
    ):
        raise ValueError("persisted preparation run is invalid")
    return ApplicationPreparationRun(
        run_id=value["run_id"],
        contract_version=value["contract_version"],
        preparation_binding=value["preparation_binding"],
        preparation_invocation_binding=(
            PreparationInvocationBinding.from_dict(
                value["preparation_invocation_binding"]
            )
            if "preparation_invocation_binding" in value
            else None
        ),
        recipe_metadata_hash=value["recipe_metadata_hash"],
        required_material_policy_id=value[
            "required_material_policy_id"
        ],
        required_material_policy_version=value[
            "required_material_policy_version"
        ],
        required_material_policy_hash=value[
            "required_material_policy_hash"
        ],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        stage_results=tuple(
            _stage_result_from_dict(
                item, run_contract_version=value["contract_version"]
            )
            for item in value["stage_results"]
        ),
        final_plan_material_manifest_id=value[
            "final_plan_material_manifest_id"
        ],
        final_prepared_application_answer_set_id=value[
            "final_prepared_application_answer_set_id"
        ],
        completed_roles=tuple(
            ApplicationPreparationCompletedRole(item)
            for item in value["completed_roles"]
        ),
        human_attention_required=value["human_attention_required"],
        deferred_stage=(
            ApplicationPreparationStage(value["deferred_stage"])
            if value["deferred_stage"]
            else None
        ),
        deferred_reason=value["deferred_reason"],
        failed_stage=(
            ApplicationPreparationStage(value["failed_stage"])
            if value["failed_stage"]
            else None
        ),
        failed_reason=value["failed_reason"],
        overall_status=ApplicationPreparationRunStatus(
            value["overall_status"]
        ),
        run_content_hash=value["run_content_hash"],
        started_at=_parse_time("started_at", value["started_at"]),
        completed_at=_parse_time("completed_at", value["completed_at"]),
    )


class PrivateHomeApplicationPreparationRunRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, 160)
        return (
            self._home.paths.application_preparation_runs
            / _subject_key(subject)
        )

    def _path(self, subject_id: str, run_id: str) -> Path:
        if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("run_id is invalid")
        return self._directory(subject_id) / f"{run_id}.json"

    def get(
        self, *, subject_id: str, run_id: str
    ) -> ApplicationPreparationRunReadResult:
        path = self._path(subject_id, run_id)
        with self._lock:
            if not path.exists():
                return ApplicationPreparationRunReadResult(
                    ApplicationPreparationRunReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return ApplicationPreparationRunReadResult(
                    ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            try:
                run = _run_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return ApplicationPreparationRunReadResult(
                    ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            if run.subject_id != subject_id.strip() or run.run_id != run_id:
                return ApplicationPreparationRunReadResult(
                    ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            return ApplicationPreparationRunReadResult(
                ApplicationPreparationRunReadStatus.FOUND, run
            )

    def save(
        self, run: ApplicationPreparationRun
    ) -> ApplicationPreparationRunWriteResult:
        if not isinstance(run, ApplicationPreparationRun):
            raise TypeError("run must be typed")
        path = self._path(run.subject_id, run.run_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            run.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ApplicationPreparationRunWriteResult(
                    ApplicationPreparationRunWriteStatus.FAILED,
                    None,
                    ApplicationPreparationFailureReason.PERSISTENCE_FAILED,
                    True,
                )
            if created:
                return ApplicationPreparationRunWriteResult(
                    ApplicationPreparationRunWriteStatus.CREATED,
                    run,
                    None,
                    False,
                )
            existing = self.get(
                subject_id=run.subject_id, run_id=run.run_id
            )
            if (
                existing.status is ApplicationPreparationRunReadStatus.FOUND
                and existing.run is not None
                and existing.run.identity_dict() == run.identity_dict()
            ):
                return ApplicationPreparationRunWriteResult(
                    ApplicationPreparationRunWriteStatus.UNCHANGED,
                    existing.run,
                    None,
                    False,
                )
            return ApplicationPreparationRunWriteResult(
                ApplicationPreparationRunWriteStatus.FAILED,
                None,
                ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE,
                False,
            )

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> ApplicationPreparationRunReadResult:
        listed = self.list_for_subject(subject_id=subject_id)
        if (
            listed.status
            is ApplicationPreparationRunListStatus.INTEGRITY_FAILURE
        ):
            return ApplicationPreparationRunReadResult(
                ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE,
                None,
            )
        matches = tuple(
            run
            for run in listed.runs
            if run.application_plan_id == application_plan_id
        )
        if not matches:
            return ApplicationPreparationRunReadResult(
                ApplicationPreparationRunReadStatus.NOT_FOUND, None
            )
        current = max(
            matches,
            key=lambda item: (
                item.completed_at.astimezone(timezone.utc),
                item.run_id,
            ),
        )
        return ApplicationPreparationRunReadResult(
            ApplicationPreparationRunReadStatus.FOUND, current
        )

    def list_for_subject(
        self, *, subject_id: str
    ) -> ApplicationPreparationRunListResult:
        directory = self._directory(subject_id)
        if not directory.exists():
            return ApplicationPreparationRunListResult(
                ApplicationPreparationRunListStatus.SUCCEEDED, ()
            )
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            return ApplicationPreparationRunListResult(
                ApplicationPreparationRunListStatus.INTEGRITY_FAILURE, ()
            )
        runs: list[ApplicationPreparationRun] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or _RUN_ID_RE.fullmatch(path.stem) is None
            ):
                return ApplicationPreparationRunListResult(
                    ApplicationPreparationRunListStatus.INTEGRITY_FAILURE,
                    (),
                )
            read = self.get(subject_id=subject_id, run_id=path.stem)
            if (
                read.status is not ApplicationPreparationRunReadStatus.FOUND
                or read.run is None
            ):
                return ApplicationPreparationRunListResult(
                    ApplicationPreparationRunListStatus.INTEGRITY_FAILURE,
                    (),
                )
            runs.append(read.run)
        ordered = tuple(
            sorted(
                runs,
                key=lambda item: (
                    item.application_plan_id,
                    item.completed_at.astimezone(timezone.utc),
                    item.run_id,
                ),
            ),
        )
        return ApplicationPreparationRunListResult(
            ApplicationPreparationRunListStatus.SUCCEEDED, ordered
        )


@dataclass(frozen=True, slots=True)
class RunApplicationPreparationCommand:
    subject_id: str
    application_plan_id: str
    now: datetime
    invocation_id: str = field(
        default_factory=lambda: f"preparation-call-{uuid4().hex}"
    )


@dataclass(frozen=True, slots=True)
class PreparationAssemblyLineage:
    subject_id: str
    application_plan_id: str
    preparation_run_id: str
    preparation_run_contract_version: str
    plan_material_manifest_id: str
    prepared_application_answer_set_id: str
    preparation_completion_hash: str
    contract_version: str
    lineage_hash: str

    def __post_init__(self) -> None:
        _clean_text("subject_id", self.subject_id, 160)
        _clean_text("application_plan_id", self.application_plan_id, 180)
        _clean_text("preparation_run_id", self.preparation_run_id, 240)
        _clean_text(
            "plan_material_manifest_id",
            self.plan_material_manifest_id,
            240,
        )
        _clean_text(
            "prepared_application_answer_set_id",
            self.prepared_application_answer_set_id,
            240,
        )
        if (
            self.preparation_run_contract_version
            != APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ):
            raise ValueError("preparation run contract is unsupported")
        if (
            self.contract_version
            != PREPARATION_ASSEMBLY_LINEAGE_CONTRACT_VERSION
        ):
            raise ValueError("assembly lineage contract is unsupported")
        _require_hash(
            "preparation_completion_hash",
            self.preparation_completion_hash,
        )
        _require_hash("lineage_hash", self.lineage_hash)
        if self.lineage_hash != _canonical_hash(self.identity_dict()):
            raise ValueError("assembly lineage hash is invalid")

    def identity_dict(self) -> dict[str, str]:
        return {
            "application_plan_id": self.application_plan_id,
            "contract_version": self.contract_version,
            "plan_material_manifest_id": self.plan_material_manifest_id,
            "preparation_completion_hash": self.preparation_completion_hash,
            "preparation_run_contract_version": (
                self.preparation_run_contract_version
            ),
            "preparation_run_id": self.preparation_run_id,
            "prepared_application_answer_set_id": (
                self.prepared_application_answer_set_id
            ),
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, str]:
        return {**self.identity_dict(), "lineage_hash": self.lineage_hash}

    @classmethod
    def from_run(
        cls, run: ApplicationPreparationRun
    ) -> PreparationAssemblyLineage:
        if (
            not isinstance(run, ApplicationPreparationRun)
            or run.overall_status is not ApplicationPreparationRunStatus.COMPLETED
            or not run.final_plan_material_manifest_id
            or not run.final_prepared_application_answer_set_id
        ):
            raise ValueError("completed preparation lineage is unavailable")
        values = {
            "application_plan_id": run.application_plan_id,
            "contract_version": (
                PREPARATION_ASSEMBLY_LINEAGE_CONTRACT_VERSION
            ),
            "plan_material_manifest_id": (
                run.final_plan_material_manifest_id
            ),
            "preparation_completion_hash": run.run_content_hash,
            "preparation_run_contract_version": run.contract_version,
            "preparation_run_id": run.run_id,
            "prepared_application_answer_set_id": (
                run.final_prepared_application_answer_set_id
            ),
            "subject_id": run.subject_id,
        }
        return cls(**values, lineage_hash=_canonical_hash(values))


@dataclass(frozen=True, slots=True)
class RunApplicationPreparationResult:
    status: ApplicationPreparationStatus
    run: ApplicationPreparationRun | None
    reason_code: ApplicationPreparationFailureReason | None
    retryable: bool
    message: str
    assembly_lineage: PreparationAssemblyLineage | None = None


def _preparation_binding(
    plan: ApplicationPlan, recipe: ApplicationPreparationRecipe
) -> str:
    return _canonical_hash(
        {
            "application_plan_id": plan.plan_id,
            "contract_version": (
                APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
            ),
            "job_content_hash": plan.job_content_hash,
            "job_id": plan.job_id,
            "job_revision": plan.job_revision,
            "recipe_metadata_hash": recipe.metadata_hash,
            "required_material_policy_hash": (
                recipe.required_material_policy.policy_content_hash
            ),
            "subject_id": plan.subject_id,
        }
    )


def _run_result_failure(
    reason: ApplicationPreparationFailureReason,
    *,
    retryable: bool = False,
) -> RunApplicationPreparationResult:
    return RunApplicationPreparationResult(
        status=ApplicationPreparationStatus.FAILED,
        run=None,
        reason_code=reason,
        retryable=retryable,
        message=f"Application preparation failed: {reason.value}.",
    )


def _build_run(
    *,
    plan: ApplicationPlan,
    recipe: ApplicationPreparationRecipe,
    preparation_binding: str,
    preparation_invocation_binding: PreparationInvocationBinding,
    stages: tuple[ApplicationPreparationStageResult, ...],
    outputs: Mapping[str, str],
    roles: tuple[ApplicationPreparationCompletedRole, ...],
    human_attention_required: bool,
    status: ApplicationPreparationRunStatus,
    stopped_stage: ApplicationPreparationStage | None,
    stopped_reason: str | None,
    now: datetime,
) -> ApplicationPreparationRun:
    policy = recipe.required_material_policy
    deferred_stage = (
        stopped_stage
        if status is ApplicationPreparationRunStatus.DEFERRED
        else None
    )
    failed_stage = (
        stopped_stage
        if status is ApplicationPreparationRunStatus.FAILED
        else None
    )
    values = {
        "application_plan_id": plan.plan_id,
        "completed_roles": [item.value for item in roles],
        "contract_version": (
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        "deferred_reason": (
            stopped_reason
            if status is ApplicationPreparationRunStatus.DEFERRED
            else None
        ),
        "deferred_stage": (
            deferred_stage.value if deferred_stage else None
        ),
        "failed_reason": (
            stopped_reason
            if status is ApplicationPreparationRunStatus.FAILED
            else None
        ),
        "failed_stage": failed_stage.value if failed_stage else None,
        "final_plan_material_manifest_id": outputs.get(
            "plan_material_manifest_id"
        ),
        "final_prepared_application_answer_set_id": outputs.get(
            "prepared_application_answer_set_id"
        ),
        "human_attention_required": human_attention_required,
        "job_content_hash": plan.job_content_hash,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "overall_status": status.value,
        "preparation_binding": preparation_binding,
        "recipe_metadata_hash": recipe.metadata_hash,
        "required_material_policy_hash": policy.policy_content_hash,
        "required_material_policy_id": policy.policy_id,
        "required_material_policy_version": policy.policy_version,
        "stage_hashes": [item.stage_content_hash for item in stages],
        "subject_id": plan.subject_id,
    }
    run_id = "application-preparation-run-" + _canonical_hash(values)
    content = {
        "application_plan_id": plan.plan_id,
        "completed_at": _rfc3339(now),
        "completed_roles": [item.value for item in roles],
        "contract_version": (
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        "deferred_reason": values["deferred_reason"],
        "deferred_stage": values["deferred_stage"],
        "failed_reason": values["failed_reason"],
        "failed_stage": values["failed_stage"],
        "final_plan_material_manifest_id": values[
            "final_plan_material_manifest_id"
        ],
        "final_prepared_application_answer_set_id": values[
            "final_prepared_application_answer_set_id"
        ],
        "human_attention_required": human_attention_required,
        "job_content_hash": plan.job_content_hash,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "overall_status": status.value,
        "preparation_binding": preparation_binding,
        "preparation_invocation_binding": (
            preparation_invocation_binding.to_dict()
        ),
        "recipe_metadata_hash": recipe.metadata_hash,
        "required_material_policy_hash": policy.policy_content_hash,
        "required_material_policy_id": policy.policy_id,
        "required_material_policy_version": policy.policy_version,
        "run_id": run_id,
        "stage_results": [item.to_dict() for item in stages],
        "started_at": _rfc3339(now),
        "subject_id": plan.subject_id,
    }
    return ApplicationPreparationRun(
        run_id=run_id,
        contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        preparation_binding=preparation_binding,
        preparation_invocation_binding=preparation_invocation_binding,
        recipe_metadata_hash=recipe.metadata_hash,
        required_material_policy_id=policy.policy_id,
        required_material_policy_version=policy.policy_version,
        required_material_policy_hash=policy.policy_content_hash,
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        job_revision=plan.job_revision,
        job_content_hash=plan.job_content_hash,
        stage_results=stages,
        final_plan_material_manifest_id=values[
            "final_plan_material_manifest_id"
        ],
        final_prepared_application_answer_set_id=values[
            "final_prepared_application_answer_set_id"
        ],
        completed_roles=roles,
        human_attention_required=human_attention_required,
        deferred_stage=deferred_stage,
        deferred_reason=values["deferred_reason"],
        failed_stage=failed_stage,
        failed_reason=values["failed_reason"],
        overall_status=status,
        run_content_hash=_canonical_hash(content),
        started_at=now,
        completed_at=now,
    )


def _persist_outcome(
    run: ApplicationPreparationRun,
    repository: ApplicationPreparationRunRepository,
    *,
    operation_reason: ApplicationPreparationFailureReason | None = None,
) -> RunApplicationPreparationResult:
    try:
        write = repository.save(run)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )
    if (
        write.status is ApplicationPreparationRunWriteStatus.FAILED
        or write.run is None
    ):
        return _run_result_failure(
            write.reason_code
            or ApplicationPreparationFailureReason.PERSISTENCE_FAILED,
            retryable=write.retryable,
        )
    if (
        run.overall_status is ApplicationPreparationRunStatus.COMPLETED
        and write.status is ApplicationPreparationRunWriteStatus.UNCHANGED
    ):
        status = ApplicationPreparationStatus.UNCHANGED
    else:
        status = ApplicationPreparationStatus(run.overall_status.value)
    try:
        assembly_lineage = (
            PreparationAssemblyLineage.from_run(write.run)
            if status
            in {
                ApplicationPreparationStatus.COMPLETED,
                ApplicationPreparationStatus.UNCHANGED,
            }
            else None
        )
    except (TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE
        )
    return RunApplicationPreparationResult(
        status=status,
        run=write.run,
        reason_code=operation_reason,
        retryable=False,
        message=f"Application preparation is {status.value}.",
        assembly_lineage=assembly_lineage,
    )


async def _invoke_preparation_stage(
    definition: ApplicationPreparationStageDefinition,
    request: ApplicationPreparationStageRequest,
) -> PublicPreparationStageResult:
    """Invoke one stage once and normalize sync/async implementations."""

    value = definition.invoke(request)
    if inspect.isawaitable(value):
        value = await value
    return value


async def run_application_preparation(
    command: RunApplicationPreparationCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    recipe: ApplicationPreparationRecipe,
    run_repository: ApplicationPreparationRunRepository,
) -> RunApplicationPreparationResult:
    try:
        subject = _clean_text("subject_id", command.subject_id, 160)
        plan_id = _clean_text(
            "application_plan_id", command.application_plan_id, 180
        )
        invocation_id = _clean_text(
            "invocation_id", command.invocation_id, 200
        )
        now = _require_aware("now", command.now)
        if not isinstance(recipe, ApplicationPreparationRecipe):
            raise TypeError("recipe must be typed")
    except (AttributeError, TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason.INVALID_REQUEST
        )
    try:
        plan_read = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _run_result_failure(
            ApplicationPreparationFailureReason.APPLICATION_PLAN_NOT_FOUND
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_read.plan, ApplicationPlan)
    ):
        return _run_result_failure(
            ApplicationPreparationFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE
        )
    plan = plan_read.plan
    if plan.subject_id != subject:
        return _run_result_failure(
            ApplicationPreparationFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH
        )
    try:
        invocation_binding = PreparationInvocationBinding.create(
            subject_id=subject,
            application_plan_id=plan.plan_id,
            invocation_id=invocation_id,
            orchestration_contract_version=(
                APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
            ),
            created_at=now,
        )
    except (TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason.INVALID_REQUEST
        )
    binding = _preparation_binding(plan, recipe)
    try:
        current = run_repository.find_current_for_plan(
            subject_id=subject, application_plan_id=plan.plan_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE
        )
    if (
        current.status
        is ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE
    ):
        return _run_result_failure(
            ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE
        )
    if (
        current.status is ApplicationPreparationRunReadStatus.FOUND
        and current.run is not None
        and current.run.overall_status
        is ApplicationPreparationRunStatus.COMPLETED
        and current.run.preparation_binding == binding
    ):
        try:
            assembly_lineage = PreparationAssemblyLineage.from_run(
                current.run
            )
        except (TypeError, ValueError):
            return _run_result_failure(
                ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE
            )
        return RunApplicationPreparationResult(
            status=ApplicationPreparationStatus.UNCHANGED,
            run=current.run,
            reason_code=None,
            retryable=False,
            message="Completed application preparation is unchanged.",
            assembly_lineage=assembly_lineage,
        )

    outputs: dict[str, str] = {}
    stage_results: list[ApplicationPreparationStageResult] = []
    roles: list[ApplicationPreparationCompletedRole] = []
    human_attention_required = False
    visual_directive: PublicStageDirective | None = None

    def typed_contract_failure(
        stage: ApplicationPreparationStage,
    ) -> PublicPreparationStageResult:
        version, reason_type, _outcomes = _STOP_REASON_CONTRACTS[stage]
        reason = _STAGE_CONTRACT_FAILURE_REASONS[stage]
        if type(reason) is not reason_type:
            raise ValueError("stage contract-failure reason is invalid")
        return PublicPreparationStageResult.failed(
            stage=stage,
            stop_reason=PreparationStopReasonEnvelope(
                stage=stage,
                code=reason,
                contract_version=version,
                outcome=PreparationStageOutcome.FAILED,
                diagnostic_code=(
                    ApplicationPreparationFailureReason
                    .PUBLIC_STAGE_CONTRACT_FAILURE.value
                ),
            ),
        )

    def persist_contract_failure(
        stage: ApplicationPreparationStage,
    ) -> RunApplicationPreparationResult:
        failed = ApplicationPreparationStageResult.from_public(
            typed_contract_failure(stage),
            preparation_invocation_ref=invocation_binding.reference,
        )
        stage_results.append(failed)
        run = _build_run(
            plan=plan,
            recipe=recipe,
            preparation_binding=binding,
            preparation_invocation_binding=invocation_binding,
            stages=tuple(stage_results),
            outputs=outputs,
            roles=tuple(roles),
            human_attention_required=human_attention_required,
            status=ApplicationPreparationRunStatus.FAILED,
            stopped_stage=stage,
            stopped_reason=failed.reason_code,
            now=now,
        )
        return _persist_outcome(
            run,
            run_repository,
            operation_reason=(
                ApplicationPreparationFailureReason
                .PUBLIC_STAGE_CONTRACT_FAILURE
            ),
        )

    for definition in recipe.stages:
        stage = definition.stage
        if stage is ApplicationPreparationStage.RESUME_LAYOUT_REVISION:
            if visual_directive is PublicStageDirective.PASSED:
                stage_results.append(
                    ApplicationPreparationStageResult.skipped_layout(
                        preparation_invocation_ref=(
                            invocation_binding.reference
                        )
                    )
                )
                continue
            if visual_directive is not PublicStageDirective.REVISION_REQUIRED:
                return persist_contract_failure(
                    ApplicationPreparationStage.RESUME_LAYOUT_REVISION
                )
        request = ApplicationPreparationStageRequest(
            stage=stage,
            subject_id=subject,
            application_plan_id=plan.plan_id,
            job_id=plan.job_id,
            now=now,
            outputs=_ordered_outputs(outputs),
            prior_stage_results=tuple(stage_results),
            preparation_invocation_binding=invocation_binding,
        )
        try:
            public_result = await _invoke_preparation_stage(
                definition, request
            )
        except Exception:
            failed = ApplicationPreparationStageResult.from_public(
                PublicPreparationStageResult.failed(
                    stage=stage,
                    stop_reason=PreparationStopReasonEnvelope(
                        stage=stage,
                        code=_STAGE_CONTRACT_FAILURE_REASONS[stage],
                        contract_version=_STOP_REASON_CONTRACTS[stage][0],
                        outcome=PreparationStageOutcome.FAILED,
                        diagnostic_code=(
                            ApplicationPreparationFailureReason
                            .PUBLIC_STAGE_EXCEPTION.value
                        ),
                    ),
                ),
                preparation_invocation_ref=invocation_binding.reference,
            )
            stage_results.append(failed)
            run = _build_run(
                plan=plan,
                recipe=recipe,
                preparation_binding=binding,
                preparation_invocation_binding=invocation_binding,
                stages=tuple(stage_results),
                outputs=outputs,
                roles=tuple(roles),
                human_attention_required=human_attention_required,
                status=ApplicationPreparationRunStatus.FAILED,
                stopped_stage=stage,
                stopped_reason=failed.reason_code,
                now=now,
            )
            return _persist_outcome(
                run,
                run_repository,
                operation_reason=(
                    ApplicationPreparationFailureReason
                    .PUBLIC_STAGE_EXCEPTION
                ),
            )
        if (
            not isinstance(public_result, PublicPreparationStageResult)
            or public_result.stage is not stage
        ):
            return persist_contract_failure(stage)
        try:
            stage_record = ApplicationPreparationStageResult.from_public(
                public_result,
                preparation_invocation_ref=invocation_binding.reference,
            )
        except (TypeError, ValueError):
            return persist_contract_failure(stage)
        if public_result.status in {
            PublicStageStatus.CREATED,
            PublicStageStatus.UNCHANGED,
        }:
            output_map = {
                item.key: item.value for item in public_result.outputs
            }
            if not _REQUIRED_OUTPUTS[stage].issubset(output_map):
                return persist_contract_failure(stage)
            if stage is ApplicationPreparationStage.RESUME_VISUAL_QA:
                visual_directive = public_result.directive
                if visual_directive not in {
                    PublicStageDirective.PASSED,
                    PublicStageDirective.REVISION_REQUIRED,
                }:
                    return persist_contract_failure(stage)
            stage_results.append(stage_record)
            human_attention_required = (
                human_attention_required
                or public_result.human_attention_required
            )
            outputs.update(output_map)
            if stage is ApplicationPreparationStage.RESUME_MANIFEST:
                roles.append(ApplicationPreparationCompletedRole.RESUME)
            elif stage is ApplicationPreparationStage.COVER_LETTER_MANIFEST:
                roles.append(
                    ApplicationPreparationCompletedRole.COVER_LETTER
                )
            elif stage is ApplicationPreparationStage.APPLICATION_ANSWERS:
                roles.append(
                    ApplicationPreparationCompletedRole.APPLICATION_ANSWERS
                )
            continue
        stage_results.append(stage_record)
        human_attention_required = (
            human_attention_required
            or public_result.human_attention_required
        )
        stopped_status = (
            ApplicationPreparationRunStatus.DEFERRED
            if public_result.status is PublicStageStatus.DEFERRED
            else ApplicationPreparationRunStatus.FAILED
        )
        run = _build_run(
            plan=plan,
            recipe=recipe,
            preparation_binding=binding,
            preparation_invocation_binding=invocation_binding,
            stages=tuple(stage_results),
            outputs=outputs,
            roles=tuple(roles),
            human_attention_required=human_attention_required,
            status=stopped_status,
            stopped_stage=stage,
            stopped_reason=public_result.reason_code,
            now=now,
        )
        return _persist_outcome(run, run_repository)

    run = _build_run(
        plan=plan,
        recipe=recipe,
        preparation_binding=binding,
        preparation_invocation_binding=invocation_binding,
        stages=tuple(stage_results),
        outputs=outputs,
        roles=tuple(roles),
        human_attention_required=human_attention_required,
        status=ApplicationPreparationRunStatus.COMPLETED,
        stopped_stage=None,
        stopped_reason=None,
        now=now,
    )
    return _persist_outcome(run, run_repository)


__all__ = [
    "APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION",
    "APPLICATION_PREPARATION_STAGE_ORDER",
    "APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION",
    "BASE_LATEX_STOP_REASON_CONTRACT_VERSION",
    "BASE_RESUME_SELECTION_STOP_REASON_CONTRACT_VERSION",
    "CANDIDATE_EVIDENCE_STOP_REASON_CONTRACT_VERSION",
    "COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION",
    "COVER_LETTER_EVIDENCE_STOP_REASON_CONTRACT_VERSION",
    "COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION",
    "COVER_LETTER_MANIFEST_ENTRY_STOP_REASON_CONTRACT_VERSION",
    "COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION",
    "COMPILATION_SOURCE_RESOLUTION_LINEAGE_CONTRACT_VERSION",
    "DOWNSTREAM_PREPARATION_STOP_LINEAGE_CONTRACT_VERSION",
    "LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION",
    "LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION",
    "PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION",
    "RESUME_LAYOUT_REVISION_STOP_REASON_CONTRACT_VERSION",
    "RESUME_COMPILATION_STOPPED_SOURCE_CONTRACT_VERSION",
    "RESUME_COMPILATION_STOPPED_SOURCE_REF_VERSION",
    "RESUME_MANIFEST_ENTRY_STOP_REASON_CONTRACT_VERSION",
    "RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION",
    "RESUME_VISUAL_QA_STOP_REASON_CONTRACT_VERSION",
    "SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION",
    "TAILORED_RESUME_DRAFT_STOP_REASON_CONTRACT_VERSION",
    "ApplicationPreparationCompletedRole",
    "ApplicationAnswersStopReason",
    "ApplicationPreparationFailureReason",
    "ApplicationPreparationOutputReference",
    "ApplicationPreparationPublicCallable",
    "ApplicationPreparationRecipe",
    "ApplicationPreparationRun",
    "ApplicationPreparationRunReadResult",
    "ApplicationPreparationRunReadStatus",
    "ApplicationPreparationRunListResult",
    "ApplicationPreparationRunListStatus",
    "ApplicationPreparationRunRepository",
    "ApplicationPreparationRunStatus",
    "ApplicationPreparationRunWriteResult",
    "ApplicationPreparationRunWriteStatus",
    "ApplicationPreparationStage",
    "ApplicationPreparationStageDefinition",
    "ApplicationPreparationStageRequest",
    "ApplicationPreparationStageResult",
    "ApplicationPreparationStatus",
    "BaseLatexPreparationStopReason",
    "BaseResumeSelectionStopReason",
    "CandidateEvidenceSnapshotStopReason",
    "CoverLetterDraftStopReason",
    "CoverLetterEvidenceStopReason",
    "CoverLetterFactQAStopReason",
    "CoverLetterManifestEntryStopReason",
    "CoverLetterPublicationStopReason",
    "DownstreamPreparationStopLineage",
    "CompilationSourceResolutionLineage",
    "LatexCompilationStopReason",
    "LatexConstructionStopReason",
    "LEGACY_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION",
    "LEGACY_PREPARATION_STAGE_RESULT_SCHEMA_VERSION",
    "PREVIOUS_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION",
    "PREVIOUS_PREPARATION_STAGE_RESULT_SCHEMA_VERSION",
    "PREPARATION_STAGE_RESULT_SCHEMA_VERSION",
    "PREPARATION_ASSEMBLY_LINEAGE_CONTRACT_VERSION",
    "PreparationAssemblyLineage",
    "PreparationStageOutcome",
    "PreparationStageExecutionStatus",
    "PreparationStopReasonEnvelope",
    "ResolvedCompilationSourceLineage",
    "PreparedResumePublicationStopReason",
    "PrivateHomeApplicationPreparationRunRepository",
    "PublicPreparationStageResult",
    "PublicStageDirective",
    "PublicStageStatus",
    "ResumeFactQAStopReason",
    "ResumeCompilationStoppedSourceRef",
    "ResumeLayoutRevisionStopReason",
    "ResumeManifestEntryStopReason",
    "ResumeVisualQAStopReason",
    "RequiredApplicationMaterialPolicy",
    "RunApplicationPreparationCommand",
    "RunApplicationPreparationResult",
    "SourceResumeProjectionStopReason",
    "TailoredResumeDraftStopReason",
    "UnresolvedCompilationSourceLineage",
    "UnresolvedCompilationSourceState",
    "run_application_preparation",
]
