"""Evidence-bound resume tailoring drafts with deterministic output validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import (
    TYPE_CHECKING,
    Any,
    Mapping,
    Protocol,
    runtime_checkable,
)

from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .application_preparation_orchestrator import (
    TAILORED_RESUME_DRAFT_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PublicPreparationStageResult,
    TailoredResumeDraftStopReason,
)
from .candidate_evidence import (
    CandidateEvidenceScope,
    CandidateEvidenceSnapshot,
    CandidateEvidenceSnapshotReadStatus,
    CandidateEvidenceSnapshotRepository,
)
from .job_discovery import (
    JobPosting,
    JobPostingReadRepository,
    JobPostingRepositoryError,
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
    SourceResumeProjection,
    SourceResumeProjectionReadStatus,
    SourceResumeProjectionRepository,
)
if TYPE_CHECKING:
    from .unsupported_claim_correction import (
        UnsupportedClaimCorrectionConstraint,
        UnsupportedClaimCorrectionDirectiveProvider,
    )


RESUME_TAILORING_CONTRACT_VERSION = "resume-tailoring-v1"
RESUME_TAILORING_POLICY_VERSION = "resume-tailoring-policy-v2"

RESUME_TAILORING_AGENT_POLICY = """Resume Tailoring Agent policy (static, non-negotiable):

Skill statement formula:
    Action Verb + Details + Outcome = Skill Statement

Instruction priority (highest first):
    1. Facts and the prohibition on fabrication.
    2. The current ApplicationPlan's user preparation instructions.
    3. Alignment with the trusted job description.
    4. Default writing style.

Correction directives are constraints, never new facts or evidence. REMOVE
means omit the identified unsupported claim. REWRITE may use only the supplied
CandidateEvidence and must not treat the user's wording as evidence.

Writing rules:
- Prefer bullets shaped as action verb + what/how + result.
- Prefer role-specific action verbs from the JD requirements and
  responsibilities, but reuse a JD verb only when the supplied
  CandidateEvidence supports that action.
- Avoid weak verbs such as "helped", "worked on", "responsible for"
  and "participated in".
- Never add numbers, skills, experience, titles, degrees, duties or
  outcomes that the supplied CandidateEvidence does not contain.
- When no quantified result exists, use evidenced deliverables, scope
  or impact; never invent metrics.

Allowed edits, always within the supplied evidence:
- Rewrite, condense or reorder bullets.
- Omit bullets weakly related to the target role.
- Reorder sections.
- Use more accurate, evidence-supported JD verbs.

Forbidden edits:
- Changing identity facts: names, companies, titles, degrees, dates.
- Introducing any fact absent from the supplied CandidateEvidence.
- Returning free text instead of the typed structured result.

Output completeness rules:
- Return exactly one section proposal for every supplied source section and
  assign contiguous section order values starting at zero.
- Within those sections, return exactly one bullet proposal for every supplied
  source block, including non-bullet blocks whose source_bullet_id is null.
- Preserve the exact source_section_id, source_block_id, and source_bullet_id
  bindings. Never invent or omit an identifier.
- To preserve a block, use UNCHANGED with its exact source text and empty
  evidence_ids and jd_alignment arrays. This is the required safe default when
  a rewrite is uncertain.
- To omit a block, use OMITTED with null text. Otherwise text must be non-empty.
- A REWRITTEN block must cite supplied evidence IDs and exact substrings from
  the trusted job description. Do not paraphrase jd_alignment references.
- Every section must contain at least one proposal. Account for all source
  blocks even when several are omitted.
"""

WEAK_LEADING_VERBS = frozenset(
    {"helped", "worked", "responsible", "participated"}
)

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DRAFT_ID_PATTERN = re.compile(r"^tailored-resume-draft-[a-f0-9]{64}$")
_WORD_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./%-]*")
_NUMBER_PATTERN = re.compile(r"\d")
MAX_TAILORED_BULLET_CHARS = 20_000


class TailoredBulletChangeType(str, Enum):
    UNCHANGED = "UNCHANGED"
    REWRITTEN = "REWRITTEN"
    REORDERED = "REORDERED"
    OMITTED = "OMITTED"


class ResumeTailoringAgentDisposition(str, Enum):
    TAILORED = "TAILORED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class TailoredResumeDraftWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class TailoredResumeDraftReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeTailoringStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_INSUFFICIENT_EVIDENCE = "DEFERRED_INSUFFICIENT_EVIDENCE"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class ResumeTailoringFailureReason(str, Enum):
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
    AGENT_OUTPUT_UNSAFE = "AGENT_OUTPUT_UNSAFE"
    DRAFT_PERSISTENCE_FAILED = "DRAFT_PERSISTENCE_FAILED"
    DRAFT_INTEGRITY_FAILURE = "DRAFT_INTEGRITY_FAILURE"


class ResumeTailoringAgentUnavailableError(RuntimeError):
    """Raised when the bounded tailoring Agent cannot return an output."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the resume-tailoring contract")
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
        raise ValueError("created_at is invalid")
    return _require_aware(
        "created_at",
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
class ResumeTailoringAgentMetadata:
    agent_version: str
    prompt_version: str
    model_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "agent_version",
            _clean_text("agent_version", self.agent_version, maximum=80),
        )
        object.__setattr__(
            self,
            "prompt_version",
            _clean_text("prompt_version", self.prompt_version, maximum=80),
        )
        object.__setattr__(
            self,
            "model_id",
            _clean_text("model_id", self.model_id, maximum=160),
        )


@dataclass(frozen=True, slots=True)
class ResumeTailoringJobContext:
    job_id: str
    revision: int
    content_hash: str
    company: str
    title: str
    description: str
    location: str
    work_mode: str

    @classmethod
    def from_job(cls, job: JobPosting) -> "ResumeTailoringJobContext":
        if not isinstance(job, JobPosting):
            raise TypeError("job must be a JobPosting")
        return cls(
            job_id=job.job_id,
            revision=job.revision,
            content_hash=job.content_hash,
            company=job.company,
            title=job.title,
            description=job.description,
            location=job.location,
            work_mode=job.work_mode,
        )


@dataclass(frozen=True, slots=True)
class ResumeTailoringEvidenceView:
    evidence_id: str
    evidence_text: str
    source_section_id: str
    source_block_id: str
    source_bullet_id: str | None


@dataclass(frozen=True, slots=True)
class ResumeTailoringContext:
    subject_id: str
    application_plan_id: str
    job: ResumeTailoringJobContext
    source_projection: SourceResumeProjection
    evidence_items: tuple[ResumeTailoringEvidenceView, ...]
    user_preparation_instructions: str | None
    agent_policy: str
    agent_policy_version: str
    correction_constraints: tuple[
        UnsupportedClaimCorrectionConstraint, ...
    ]


@dataclass(frozen=True, slots=True)
class TailoredBulletProposal:
    source_section_id: str
    source_block_id: str
    source_bullet_id: str | None
    change_type: TailoredBulletChangeType
    text: str | None
    evidence_ids: tuple[str, ...]
    jd_alignment: tuple[str, ...]

    def __post_init__(self) -> None:
        change_type = TailoredBulletChangeType(self.change_type)
        object.__setattr__(self, "change_type", change_type)
        _clean_text(
            "source_section_id", self.source_section_id, maximum=160
        )
        _clean_text("source_block_id", self.source_block_id, maximum=160)
        if self.source_bullet_id is not None:
            _clean_text(
                "source_bullet_id", self.source_bullet_id, maximum=160
            )
        if not isinstance(self.evidence_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_ids
        ):
            raise TypeError("evidence_ids must be a tuple of identifiers")
        if not isinstance(self.jd_alignment, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.jd_alignment
        ):
            raise TypeError("jd_alignment must be a tuple of JD references")
        if change_type is TailoredBulletChangeType.OMITTED:
            if self.text is not None:
                raise ValueError("omitted bullet cannot carry text")
        else:
            if (
                not isinstance(self.text, str)
                or not self.text.strip()
                or len(self.text) > MAX_TAILORED_BULLET_CHARS
            ):
                raise ValueError("bullet text is outside the contract")
        if change_type is TailoredBulletChangeType.REWRITTEN and (
            not self.evidence_ids or not self.jd_alignment
        ):
            raise ValueError(
                "rewritten bullet requires evidence and JD alignment"
            )


@dataclass(frozen=True, slots=True)
class TailoredSectionProposal:
    source_section_id: str
    order: int
    bullets: tuple[TailoredBulletProposal, ...]

    def __post_init__(self) -> None:
        _clean_text(
            "source_section_id", self.source_section_id, maximum=160
        )
        if type(self.order) is not int or self.order < 0:
            raise ValueError("section order must be a non-negative integer")
        if not isinstance(self.bullets, tuple) or any(
            not isinstance(item, TailoredBulletProposal)
            for item in self.bullets
        ):
            raise TypeError("section bullets must be typed proposals")


@dataclass(frozen=True, slots=True)
class ResumeTailoringAgentOutput:
    disposition: ResumeTailoringAgentDisposition
    sections: tuple[TailoredSectionProposal, ...]
    rationale: str

    def __post_init__(self) -> None:
        disposition = ResumeTailoringAgentDisposition(self.disposition)
        object.__setattr__(self, "disposition", disposition)
        _clean_text("rationale", self.rationale, maximum=4_000)
        if not isinstance(self.sections, tuple) or any(
            not isinstance(item, TailoredSectionProposal)
            for item in self.sections
        ):
            raise TypeError("sections must be typed proposals")
        if (
            disposition
            is ResumeTailoringAgentDisposition.INSUFFICIENT_EVIDENCE
            and self.sections
        ):
            raise ValueError(
                "insufficient-evidence output cannot propose sections"
            )
        if (
            disposition is ResumeTailoringAgentDisposition.TAILORED
            and not self.sections
        ):
            raise ValueError("tailored output must propose sections")


@runtime_checkable
class ResumeTailoringAgentPort(Protocol):
    async def tailor(
        self,
        context: ResumeTailoringContext,
    ) -> ResumeTailoringAgentOutput:
        """Rewrite supplied bullets within supplied evidence, without tools."""


def _tailoring_binding(
    *,
    plan: ApplicationPlan,
    job: JobPosting,
    selection: ResumeSelectionDecision,
    projection: SourceResumeProjection,
    snapshot: CandidateEvidenceSnapshot,
    metadata: ResumeTailoringAgentMetadata,
    correction_constraints: tuple[
        UnsupportedClaimCorrectionConstraint, ...
    ] = (),
) -> str:
    return _canonical_hash(
        {
            "application_plan_id": plan.plan_id,
            "evidence_snapshot_hash": snapshot.snapshot_content_hash,
            "evidence_snapshot_id": snapshot.snapshot_id,
            "job_content_hash": job.content_hash,
            "job_id": job.job_id,
            "job_revision": job.revision,
            "resume_selection_decision_id": selection.decision_id,
            "resume_tailoring_agent_version": metadata.agent_version,
            "resume_tailoring_contract_version": (
                RESUME_TAILORING_CONTRACT_VERSION
            ),
            "resume_tailoring_model_id": metadata.model_id,
            "resume_tailoring_policy_version": (
                RESUME_TAILORING_POLICY_VERSION
            ),
            "resume_tailoring_prompt_version": metadata.prompt_version,
            "source_artifact_sha256": selection.source_artifact_sha256,
            "source_projection_hash": projection.projection_content_hash,
            "source_projection_id": projection.projection_id,
            "source_resume_id": selection.source_resume_id,
            "subject_id": plan.subject_id,
            "unsupported_claim_corrections": [
                {
                    "directive_hash": item.directive_hash,
                    "directive_id": item.directive_id,
                    "finding_id": item.finding_id,
                }
                for item in correction_constraints
            ],
            "user_preparation_instructions_hash": (
                plan.user_preparation_instructions_hash
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class TailoredResumeBullet:
    order: int
    change_type: TailoredBulletChangeType
    text: str | None
    source_section_id: str
    source_block_id: str
    source_bullet_id: str | None
    evidence_ids: tuple[str, ...]
    jd_alignment: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.order) is not int or self.order < 0:
            raise ValueError("bullet order must be a non-negative integer")
        change_type = TailoredBulletChangeType(self.change_type)
        object.__setattr__(self, "change_type", change_type)
        _clean_text(
            "source_section_id", self.source_section_id, maximum=160
        )
        _clean_text("source_block_id", self.source_block_id, maximum=160)
        if self.source_bullet_id is not None:
            _clean_text(
                "source_bullet_id", self.source_bullet_id, maximum=160
            )
        if not isinstance(self.evidence_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_ids
        ):
            raise TypeError("evidence_ids must be a tuple of identifiers")
        if not isinstance(self.jd_alignment, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.jd_alignment
        ):
            raise TypeError("jd_alignment must be a tuple of JD references")
        if change_type is TailoredBulletChangeType.OMITTED:
            if self.text is not None:
                raise ValueError("omitted bullet cannot carry text")
        elif (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > MAX_TAILORED_BULLET_CHARS
        ):
            raise ValueError("bullet text is outside the contract")
        if change_type is TailoredBulletChangeType.REWRITTEN and (
            not self.evidence_ids or not self.jd_alignment
        ):
            raise ValueError(
                "rewritten bullet requires evidence and JD alignment"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "change_type": self.change_type.value,
            "text": self.text,
            "source_section_id": self.source_section_id,
            "source_block_id": self.source_block_id,
            "source_bullet_id": self.source_bullet_id,
            "evidence_ids": list(self.evidence_ids),
            "jd_alignment": list(self.jd_alignment),
        }


@dataclass(frozen=True, slots=True)
class TailoredResumeSection:
    order: int
    source_section_id: str
    title: str | None
    bullets: tuple[TailoredResumeBullet, ...]

    def __post_init__(self) -> None:
        if type(self.order) is not int or self.order < 0:
            raise ValueError("section order must be a non-negative integer")
        _clean_text(
            "source_section_id", self.source_section_id, maximum=160
        )
        if self.title is not None:
            _clean_text("title", self.title, maximum=20_000)
        if (
            not isinstance(self.bullets, tuple)
            or not self.bullets
            or any(
                not isinstance(item, TailoredResumeBullet)
                for item in self.bullets
            )
        ):
            raise TypeError("section bullets must be a non-empty typed tuple")
        if tuple(item.order for item in self.bullets) != tuple(
            range(len(self.bullets))
        ):
            raise ValueError("section bullets must have contiguous order")
        if any(
            item.source_section_id != self.source_section_id
            for item in self.bullets
        ):
            raise ValueError("section bullet binding is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "source_section_id": self.source_section_id,
            "title": self.title,
            "bullets": [item.to_dict() for item in self.bullets],
        }


@dataclass(frozen=True, slots=True)
class TailoredResumeDraft:
    draft_id: str
    contract_version: str
    tailoring_binding: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    resume_selection_decision_id: str
    source_resume_id: str
    source_artifact_sha256: str
    source_projection_id: str
    source_projection_hash: str
    evidence_snapshot_id: str
    evidence_snapshot_hash: str
    user_preparation_instructions_hash: str
    agent_version: str
    prompt_version: str
    model_id: str
    agent_policy_version: str
    rationale: str
    sections: tuple[TailoredResumeSection, ...]
    draft_content_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != RESUME_TAILORING_CONTRACT_VERSION:
            raise ValueError(
                "TailoredResumeDraft contract version is unsupported"
            )
        binding = _require_hash("tailoring_binding", self.tailoring_binding)
        if (
            not isinstance(self.draft_id, str)
            or _DRAFT_ID_PATTERN.fullmatch(self.draft_id) is None
            or self.draft_id != f"tailored-resume-draft-{binding}"
        ):
            raise ValueError("draft_id does not match its binding")
        _clean_text("subject_id", self.subject_id, maximum=160)
        _clean_text(
            "application_plan_id", self.application_plan_id, maximum=160
        )
        _clean_text("job_id", self.job_id, maximum=160)
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be a positive integer")
        _require_hash("job_content_hash", self.job_content_hash)
        _clean_text(
            "resume_selection_decision_id",
            self.resume_selection_decision_id,
            maximum=160,
        )
        _clean_text("source_resume_id", self.source_resume_id, maximum=160)
        _require_hash(
            "source_artifact_sha256", self.source_artifact_sha256
        )
        _clean_text(
            "source_projection_id", self.source_projection_id, maximum=160
        )
        _require_hash(
            "source_projection_hash", self.source_projection_hash
        )
        _clean_text(
            "evidence_snapshot_id", self.evidence_snapshot_id, maximum=160
        )
        _require_hash(
            "evidence_snapshot_hash", self.evidence_snapshot_hash
        )
        _require_hash(
            "user_preparation_instructions_hash",
            self.user_preparation_instructions_hash,
        )
        _clean_text("agent_version", self.agent_version, maximum=80)
        _clean_text("prompt_version", self.prompt_version, maximum=80)
        _clean_text("model_id", self.model_id, maximum=160)
        policy_version = _clean_text(
            "agent_policy_version", self.agent_policy_version, maximum=80
        )
        if policy_version != RESUME_TAILORING_POLICY_VERSION:
            raise ValueError("tailoring policy version is unsupported")
        _clean_text("rationale", self.rationale, maximum=4_000)
        if (
            not isinstance(self.sections, tuple)
            or not self.sections
            or any(
                not isinstance(item, TailoredResumeSection)
                for item in self.sections
            )
        ):
            raise TypeError("sections must be a non-empty typed tuple")
        if tuple(item.order for item in self.sections) != tuple(
            range(len(self.sections))
        ):
            raise ValueError("sections must have contiguous order")
        section_ids = [item.source_section_id for item in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("section source identities must be unique")
        _require_aware("created_at", self.created_at)
        content_hash = _require_hash(
            "draft_content_hash", self.draft_content_hash
        )
        if content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("draft content hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "contract_version": self.contract_version,
            "tailoring_binding": self.tailoring_binding,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "resume_selection_decision_id": (
                self.resume_selection_decision_id
            ),
            "source_resume_id": self.source_resume_id,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_projection_id": self.source_projection_id,
            "source_projection_hash": self.source_projection_hash,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "evidence_snapshot_hash": self.evidence_snapshot_hash,
            "user_preparation_instructions_hash": (
                self.user_preparation_instructions_hash
            ),
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "agent_policy_version": self.agent_policy_version,
            "rationale": self.rationale,
            "sections": [item.to_dict() for item in self.sections],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "draft_content_hash": self.draft_content_hash,
            "created_at": _rfc3339(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class TailoredResumeDraftWriteResult:
    status: TailoredResumeDraftWriteStatus
    draft: TailoredResumeDraft | None
    reason_code: ResumeTailoringFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = TailoredResumeDraftWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeTailoringFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            TailoredResumeDraftWriteStatus.CREATED,
            TailoredResumeDraftWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.draft, TailoredResumeDraft)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful draft write result is invalid")
        elif self.draft is not None or self.reason_code is None:
            raise ValueError("failed draft write result is invalid")


@dataclass(frozen=True, slots=True)
class TailoredResumeDraftReadResult:
    status: TailoredResumeDraftReadStatus
    draft: TailoredResumeDraft | None
    reason_code: ResumeTailoringFailureReason | None = None

    def __post_init__(self) -> None:
        status = TailoredResumeDraftReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeTailoringFailureReason(self.reason_code),
            )
        if status is TailoredResumeDraftReadStatus.FOUND:
            if (
                not isinstance(self.draft, TailoredResumeDraft)
                or self.reason_code is not None
            ):
                raise ValueError("found draft read result is invalid")
        elif status is TailoredResumeDraftReadStatus.NOT_FOUND:
            if self.draft is not None or self.reason_code is not None:
                raise ValueError("not-found draft read result is invalid")
        elif (
            self.draft is not None
            or self.reason_code
            is not ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure draft read result is invalid")


@runtime_checkable
class TailoredResumeDraftRepository(Protocol):
    def save(
        self, draft: TailoredResumeDraft
    ) -> TailoredResumeDraftWriteResult:
        """Persist one immutable tailored resume draft."""

    def get(
        self, *, subject_id: str, draft_id: str
    ) -> TailoredResumeDraftReadResult:
        """Read one subject-owned tailored resume draft."""


def _bullet_from_dict(value: Any, *, section_id: str) -> TailoredResumeBullet:
    expected = {
        "order",
        "change_type",
        "text",
        "source_section_id",
        "source_block_id",
        "source_bullet_id",
        "evidence_ids",
        "jd_alignment",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["evidence_ids"], list)
        or not isinstance(value["jd_alignment"], list)
        or value["source_section_id"] != section_id
    ):
        raise ValueError("persisted TailoredResumeBullet is invalid")
    return TailoredResumeBullet(
        order=value["order"],
        change_type=TailoredBulletChangeType(value["change_type"]),
        text=value["text"],
        source_section_id=value["source_section_id"],
        source_block_id=value["source_block_id"],
        source_bullet_id=value["source_bullet_id"],
        evidence_ids=tuple(value["evidence_ids"]),
        jd_alignment=tuple(value["jd_alignment"]),
    )


def _draft_from_dict(value: Any) -> TailoredResumeDraft:
    expected = {
        "draft_id",
        "contract_version",
        "tailoring_binding",
        "subject_id",
        "application_plan_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "resume_selection_decision_id",
        "source_resume_id",
        "source_artifact_sha256",
        "source_projection_id",
        "source_projection_hash",
        "evidence_snapshot_id",
        "evidence_snapshot_hash",
        "user_preparation_instructions_hash",
        "agent_version",
        "prompt_version",
        "model_id",
        "agent_policy_version",
        "rationale",
        "sections",
        "draft_content_hash",
        "created_at",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["sections"], list)
    ):
        raise ValueError("persisted TailoredResumeDraft is invalid")
    sections: list[TailoredResumeSection] = []
    for raw_section in value["sections"]:
        if (
            not isinstance(raw_section, Mapping)
            or set(raw_section)
            != {"order", "source_section_id", "title", "bullets"}
            or not isinstance(raw_section["bullets"], list)
        ):
            raise ValueError("persisted TailoredResumeSection is invalid")
        sections.append(
            TailoredResumeSection(
                order=raw_section["order"],
                source_section_id=raw_section["source_section_id"],
                title=raw_section["title"],
                bullets=tuple(
                    _bullet_from_dict(
                        item,
                        section_id=raw_section["source_section_id"],
                    )
                    for item in raw_section["bullets"]
                ),
            )
        )
    return TailoredResumeDraft(
        draft_id=value["draft_id"],
        contract_version=value["contract_version"],
        tailoring_binding=value["tailoring_binding"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        resume_selection_decision_id=value[
            "resume_selection_decision_id"
        ],
        source_resume_id=value["source_resume_id"],
        source_artifact_sha256=value["source_artifact_sha256"],
        source_projection_id=value["source_projection_id"],
        source_projection_hash=value["source_projection_hash"],
        evidence_snapshot_id=value["evidence_snapshot_id"],
        evidence_snapshot_hash=value["evidence_snapshot_hash"],
        user_preparation_instructions_hash=value[
            "user_preparation_instructions_hash"
        ],
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        agent_policy_version=value["agent_policy_version"],
        rationale=value["rationale"],
        sections=tuple(sections),
        draft_content_hash=value["draft_content_hash"],
        created_at=_parse_timestamp(value["created_at"]),
    )


class PrivateHomeTailoredResumeDraftRepository:
    """Immutable, subject-scoped tailored resume drafts in Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, draft_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(draft_id, str)
            or _DRAFT_ID_PATTERN.fullmatch(draft_id) is None
        ):
            raise ValueError("draft_id is invalid")
        return (
            self._home.paths.tailored_resume_drafts
            / _subject_storage_key(subject)
            / f"{draft_id}.json"
        )

    def get(
        self, *, subject_id: str, draft_id: str
    ) -> TailoredResumeDraftReadResult:
        path = self._path(subject_id, draft_id)
        with self._lock:
            if not path.exists():
                return TailoredResumeDraftReadResult(
                    status=TailoredResumeDraftReadStatus.NOT_FOUND,
                    draft=None,
                )
            if path.is_symlink() or not path.is_file():
                return TailoredResumeDraftReadResult(
                    status=TailoredResumeDraftReadStatus.INTEGRITY_FAILURE,
                    draft=None,
                    reason_code=(
                        ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE
                    ),
                )
            try:
                draft = _draft_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return TailoredResumeDraftReadResult(
                    status=TailoredResumeDraftReadStatus.INTEGRITY_FAILURE,
                    draft=None,
                    reason_code=(
                        ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE
                    ),
                )
            if (
                draft.subject_id != subject_id.strip()
                or draft.draft_id != draft_id
                or path.name != f"{draft.draft_id}.json"
            ):
                return TailoredResumeDraftReadResult(
                    status=TailoredResumeDraftReadStatus.INTEGRITY_FAILURE,
                    draft=None,
                    reason_code=(
                        ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE
                    ),
                )
            return TailoredResumeDraftReadResult(
                status=TailoredResumeDraftReadStatus.FOUND,
                draft=draft,
            )

    def save(
        self, draft: TailoredResumeDraft
    ) -> TailoredResumeDraftWriteResult:
        if not isinstance(draft, TailoredResumeDraft):
            raise TypeError("draft must be a TailoredResumeDraft")
        path = self._path(draft.subject_id, draft.draft_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            draft.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return TailoredResumeDraftWriteResult(
                    status=TailoredResumeDraftWriteStatus.FAILED,
                    draft=None,
                    reason_code=(
                        ResumeTailoringFailureReason.DRAFT_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return TailoredResumeDraftWriteResult(
                    status=TailoredResumeDraftWriteStatus.CREATED,
                    draft=draft,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=draft.subject_id,
                draft_id=draft.draft_id,
            )
            if (
                existing.status is TailoredResumeDraftReadStatus.FOUND
                and existing.draft is not None
                and existing.draft.content_dict() == draft.content_dict()
            ):
                return TailoredResumeDraftWriteResult(
                    status=TailoredResumeDraftWriteStatus.UNCHANGED,
                    draft=existing.draft,
                    reason_code=None,
                    retryable=False,
                )
            return TailoredResumeDraftWriteResult(
                status=TailoredResumeDraftWriteStatus.FAILED,
                draft=None,
                reason_code=(
                    ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class TailorResumeCommand:
    subject_id: str
    application_plan_id: str
    evidence_snapshot_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class TailorResumeResult:
    status: ResumeTailoringStatus
    subject_id: str
    application_plan_id: str
    tailoring_binding: str
    draft: TailoredResumeDraft | None
    write_result: TailoredResumeDraftWriteResult | None
    reason_code: ResumeTailoringFailureReason | None
    retryable: bool
    message: str
    diagnostic_code: str | None = None

    def __post_init__(self) -> None:
        status = ResumeTailoringStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeTailoringFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if self.diagnostic_code is not None:
            if (
                not isinstance(self.diagnostic_code, str)
                or not self.diagnostic_code
                or len(self.diagnostic_code) > 120
            ):
                raise ValueError("diagnostic_code must be a short non-empty string")
        if status in {
            ResumeTailoringStatus.CREATED,
            ResumeTailoringStatus.UNCHANGED,
        }:
            expected = TailoredResumeDraftWriteStatus(status.value)
            if (
                not isinstance(self.draft, TailoredResumeDraft)
                or not isinstance(
                    self.write_result, TailoredResumeDraftWriteResult
                )
                or self.write_result.status is not expected
                or self.write_result.draft != self.draft
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful tailoring result is invalid")
        elif status is ResumeTailoringStatus.DEFERRED_INSUFFICIENT_EVIDENCE:
            if (
                self.draft is not None
                or self.write_result is not None
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("deferred tailoring result is invalid")
        elif status is ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN:
            if (
                self.draft is not None
                or self.write_result is not None
                or self.reason_code
                is not ResumeTailoringFailureReason.AGENT_OUTPUT_UNSAFE
                or self.retryable
            ):
                raise ValueError("needs-human tailoring result is invalid")
        elif self.draft is not None or self.reason_code is None:
            raise ValueError("failed tailoring result is invalid")


def _failure(
    command: TailorResumeCommand,
    reason: ResumeTailoringFailureReason,
    *,
    retryable: bool = False,
    tailoring_binding: str = "",
    write_result: TailoredResumeDraftWriteResult | None = None,
    diagnostic_code: str | None = None,
) -> TailorResumeResult:
    return TailorResumeResult(
        status=ResumeTailoringStatus.FAILED,
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
        tailoring_binding=tailoring_binding,
        draft=None,
        write_result=write_result,
        reason_code=reason,
        retryable=retryable,
        message=f"Resume tailoring failed: {reason.value}.",
        diagnostic_code=diagnostic_code,
    )


def _needs_human(
    command: TailorResumeCommand,
    *,
    tailoring_binding: str,
    detail: str,
    diagnostic_code: str | None = None,
) -> TailorResumeResult:
    return TailorResumeResult(
        status=ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN,
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        tailoring_binding=tailoring_binding,
        draft=None,
        write_result=None,
        reason_code=ResumeTailoringFailureReason.AGENT_OUTPUT_UNSAFE,
        retryable=False,
        message=f"The tailored resume needs human review: {detail}",
        diagnostic_code=diagnostic_code,
    )


class _OutputRejected(ValueError):
    """The Agent output failed a deterministic safety check."""


def _output_rejection_diagnostic(rejection: _OutputRejected) -> str:
    return re.sub(
        r"[^A-Z0-9]+", "_", str(rejection).upper()
    ).strip("_")[:120]


def _words(text: str) -> tuple[str, ...]:
    return tuple(_WORD_PATTERN.findall(text))


def _casefold_word_set(text: str) -> frozenset[str]:
    return frozenset(word.casefold() for word in _words(text))


def _checkable_tokens(text: str) -> tuple[str, ...]:
    """Tokens that must be evidenced when new: numbers plus proper-noun-like words.

    The first token is exempt from the capitalization test because policy
    bullets start with a capitalized action verb; verbs are checked separately.
    """
    tokens = _words(text)
    checked: list[str] = []
    for index, token in enumerate(tokens):
        if _NUMBER_PATTERN.search(token):
            checked.append(token)
        elif index > 0 and any(char.isupper() for char in token):
            checked.append(token)
    return tuple(checked)


def _validate_agent_sections(
    *,
    output: ResumeTailoringAgentOutput,
    projection: SourceResumeProjection,
    snapshot: CandidateEvidenceSnapshot,
    job: JobPosting,
    user_instructions: str | None,
) -> tuple[TailoredResumeSection, ...]:
    """Deterministically verify every Agent proposal against bound inputs."""

    source_sections = {
        section.section_id: section for section in projection.sections
    }
    source_blocks = {
        block.block_id: (section, block)
        for section in projection.sections
        for block in section.blocks
    }
    evidence_by_id = {
        item.evidence_id: item for item in snapshot.evidence_items
    }
    jd_words = _casefold_word_set(job.description)
    instructions = user_instructions or ""

    proposed_section_ids = [
        section.source_section_id for section in output.sections
    ]
    if len(proposed_section_ids) != len(set(proposed_section_ids)):
        raise _OutputRejected("duplicate section in Agent output")
    if set(proposed_section_ids) != set(source_sections):
        raise _OutputRejected("Agent output does not cover source sections")
    if tuple(section.order for section in output.sections) != tuple(
        range(len(output.sections))
    ):
        raise _OutputRejected("Agent section order is not contiguous")

    seen_blocks: set[str] = set()
    sections: list[TailoredResumeSection] = []
    for section_proposal in output.sections:
        source_section = source_sections[section_proposal.source_section_id]
        bullets: list[TailoredResumeBullet] = []
        for bullet_order, proposal in enumerate(section_proposal.bullets):
            located = source_blocks.get(proposal.source_block_id)
            if located is None:
                raise _OutputRejected("unknown source block in Agent output")
            block_section, block = located
            if (
                proposal.source_section_id != block_section.section_id
                or proposal.source_bullet_id != block.bullet_id
            ):
                raise _OutputRejected("source reference binding is invalid")
            if block.block_id in seen_blocks:
                raise _OutputRejected("duplicate source block in Agent output")
            seen_blocks.add(block.block_id)

            for evidence_id in proposal.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    raise _OutputRejected("unknown evidence reference")
                if (
                    CandidateEvidenceScope.RESUME_TAILORING
                    not in evidence.allowed_scopes
                ):
                    raise _OutputRejected("evidence scope is not permitted")
            for reference in proposal.jd_alignment:
                if reference not in job.description:
                    raise _OutputRejected(
                        "JD alignment reference is not in the job description"
                    )

            if proposal.change_type is TailoredBulletChangeType.OMITTED:
                if block.text and block.text in instructions:
                    raise _OutputRejected(
                        "user-required content cannot be omitted"
                    )
            elif proposal.change_type in {
                TailoredBulletChangeType.UNCHANGED,
                TailoredBulletChangeType.REORDERED,
            }:
                if proposal.text != block.text:
                    raise _OutputRejected(
                        "unchanged bullet text differs from the source"
                    )
            else:
                allowed_words = _casefold_word_set(block.text)
                for evidence_id in proposal.evidence_ids:
                    allowed_words |= _casefold_word_set(
                        evidence_by_id[evidence_id].evidence_text
                    )
                text = proposal.text or ""
                tokens = _words(text)
                if tokens:
                    leading = tokens[0].casefold()
                    if leading in WEAK_LEADING_VERBS:
                        raise _OutputRejected(
                            "rewritten bullet starts with a weak verb"
                        )
                    if leading in jd_words and leading not in allowed_words:
                        raise _OutputRejected(
                            "JD verb is not supported by cited evidence"
                        )
                for token in _checkable_tokens(text):
                    if token.casefold() not in allowed_words:
                        raise _OutputRejected(
                            "rewritten bullet adds an unevidenced fact"
                        )
            bullets.append(
                TailoredResumeBullet(
                    order=bullet_order,
                    change_type=proposal.change_type,
                    text=proposal.text,
                    source_section_id=proposal.source_section_id,
                    source_block_id=proposal.source_block_id,
                    source_bullet_id=proposal.source_bullet_id,
                    evidence_ids=proposal.evidence_ids,
                    jd_alignment=proposal.jd_alignment,
                )
            )
        if not bullets:
            raise _OutputRejected("Agent section proposes no bullets")
        sections.append(
            TailoredResumeSection(
                order=section_proposal.order,
                source_section_id=section_proposal.source_section_id,
                title=source_section.title,
                bullets=tuple(bullets),
            )
        )
    if seen_blocks != set(source_blocks):
        raise _OutputRejected(
            "Agent output does not account for every source block"
        )
    return tuple(sections)


async def tailor_resume(
    command: TailorResumeCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    job_repository: JobPostingReadRepository,
    selection_repository: ResumeSelectionDecisionRepository,
    candidate_repository: ResumeCandidateRepository,
    projection_repository: SourceResumeProjectionRepository,
    evidence_snapshot_repository: CandidateEvidenceSnapshotRepository,
    agent: ResumeTailoringAgentPort,
    metadata: ResumeTailoringAgentMetadata,
    draft_repository: TailoredResumeDraftRepository,
    correction_provider: (
        UnsupportedClaimCorrectionDirectiveProvider | None
    ) = None,
) -> TailorResumeResult:
    """Create one evidence-bound tailored resume draft, at most one Agent call."""

    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        plan_id = _clean_text(
            "application_plan_id",
            command.application_plan_id,
            maximum=160,
        )
        snapshot_id = _clean_text(
            "evidence_snapshot_id",
            command.evidence_snapshot_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
        if not isinstance(metadata, ResumeTailoringAgentMetadata):
            raise TypeError("metadata must be ResumeTailoringAgentMetadata")
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, ResumeTailoringFailureReason.INVALID_REQUEST
        )

    try:
        plan_result = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeTailoringFailureReason.APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_result.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeTailoringFailureReason.APPLICATION_PLAN_NOT_FOUND
        )
    if (
        plan_result.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_result.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            ResumeTailoringFailureReason.APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_result.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            ResumeTailoringFailureReason.APPLICATION_PLAN_SUBJECT_MISMATCH,
        )

    try:
        job = job_repository.get(plan.job_id)
    except (
        JobPostingRepositoryError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return _failure(
            command, ResumeTailoringFailureReason.JOB_READ_FAILED
        )
    if job is None:
        return _failure(
            command, ResumeTailoringFailureReason.JOB_NOT_FOUND
        )
    if (
        not isinstance(job, JobPosting)
        or job.job_id != plan.job_id
        or job.revision != plan.job_revision
        or job.content_hash != plan.job_content_hash
    ):
        return _failure(
            command, ResumeTailoringFailureReason.JOB_BINDING_MISMATCH
        )

    try:
        selection_result = selection_repository.find_current_for_plan(
            subject_id=subject_id,
            application_plan_id=plan_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeTailoringFailureReason.RESUME_SELECTION_INTEGRITY_FAILURE,
        )
    if selection_result.status is ResumeSelectionDecisionReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeTailoringFailureReason.RESUME_SELECTION_NOT_FOUND,
        )
    if (
        selection_result.status is not ResumeSelectionDecisionReadStatus.FOUND
        or not isinstance(
            selection_result.decision, ResumeSelectionDecision
        )
    ):
        return _failure(
            command,
            ResumeTailoringFailureReason.RESUME_SELECTION_INTEGRITY_FAILURE,
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
            ResumeTailoringFailureReason.RESUME_SELECTION_BINDING_MISMATCH,
        )

    try:
        candidate_result = candidate_repository.get(
            subject_id=subject_id,
            resume_id=selection.source_resume_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeTailoringFailureReason.RESUME_CANDIDATE_INTEGRITY_FAILURE,
        )
    if candidate_result.status is ResumeCandidateReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeTailoringFailureReason.RESUME_CANDIDATE_NOT_FOUND,
        )
    if (
        candidate_result.status is not ResumeCandidateReadStatus.FOUND
        or not isinstance(candidate_result.candidate, ResumeCandidate)
    ):
        return _failure(
            command,
            ResumeTailoringFailureReason.RESUME_CANDIDATE_INTEGRITY_FAILURE,
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
            ResumeTailoringFailureReason.RESUME_CANDIDATE_BINDING_MISMATCH,
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
            ResumeTailoringFailureReason.SOURCE_PROJECTION_INTEGRITY_FAILURE,
        )
    if projection_result.status is SourceResumeProjectionReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeTailoringFailureReason.SOURCE_PROJECTION_NOT_FOUND,
        )
    if (
        projection_result.status is not SourceResumeProjectionReadStatus.FOUND
        or not isinstance(
            projection_result.projection, SourceResumeProjection
        )
    ):
        return _failure(
            command,
            ResumeTailoringFailureReason.SOURCE_PROJECTION_INTEGRITY_FAILURE,
        )
    projection = projection_result.projection
    if (
        projection.subject_id != subject_id
        or projection.resume_id != candidate.resume_id
        or projection.artifact_sha256 != candidate.artifact_sha256
    ):
        return _failure(
            command,
            ResumeTailoringFailureReason.SOURCE_PROJECTION_BINDING_MISMATCH,
        )

    try:
        snapshot_result = evidence_snapshot_repository.get(
            subject_id=subject_id,
            snapshot_id=snapshot_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeTailoringFailureReason.EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE,
        )
    if snapshot_result.status is CandidateEvidenceSnapshotReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeTailoringFailureReason.EVIDENCE_SNAPSHOT_NOT_FOUND,
        )
    if (
        snapshot_result.status
        is not CandidateEvidenceSnapshotReadStatus.FOUND
        or not isinstance(
            snapshot_result.snapshot, CandidateEvidenceSnapshot
        )
    ):
        return _failure(
            command,
            ResumeTailoringFailureReason.EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE,
        )
    snapshot = snapshot_result.snapshot
    if (
        snapshot.subject_id != subject_id
        or snapshot.application_plan_id != plan.plan_id
        or snapshot.job_id != plan.job_id
        or snapshot.resume_selection_decision_id != selection.decision_id
        or snapshot.source_resume_id != candidate.resume_id
        or snapshot.source_artifact_sha256 != candidate.artifact_sha256
        or snapshot.source_projection_id != projection.projection_id
        or snapshot.source_projection_hash
        != projection.projection_content_hash
    ):
        return _failure(
            command,
            ResumeTailoringFailureReason.EVIDENCE_SNAPSHOT_BINDING_MISMATCH,
        )

    correction_constraints: tuple[
        UnsupportedClaimCorrectionConstraint, ...
    ] = ()
    if correction_provider is not None:
        correction_result = correction_provider.list_current(
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            material_kind="RESUME",
        )
        if not correction_result.succeeded:
            return _failure(
                command,
                ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE,
            )
        correction_constraints = tuple(
            item.constraint for item in correction_result.directives
        )

    binding = _tailoring_binding(
        plan=plan,
        job=job,
        selection=selection,
        projection=projection,
        snapshot=snapshot,
        metadata=metadata,
        correction_constraints=correction_constraints,
    )
    draft_id = f"tailored-resume-draft-{binding}"
    try:
        existing = draft_repository.get(
            subject_id=subject_id,
            draft_id=draft_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE,
            tailoring_binding=binding,
        )
    if existing.status is TailoredResumeDraftReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE,
            tailoring_binding=binding,
        )
    if (
        existing.status is TailoredResumeDraftReadStatus.FOUND
        and existing.draft is not None
    ):
        write_result = TailoredResumeDraftWriteResult(
            status=TailoredResumeDraftWriteStatus.UNCHANGED,
            draft=existing.draft,
            reason_code=None,
            retryable=False,
        )
        return TailorResumeResult(
            status=ResumeTailoringStatus.UNCHANGED,
            subject_id=subject_id,
            application_plan_id=plan_id,
            tailoring_binding=binding,
            draft=existing.draft,
            write_result=write_result,
            reason_code=None,
            retryable=False,
            message="The existing tailored resume draft is unchanged.",
        )

    tailoring_evidence = tuple(
        ResumeTailoringEvidenceView(
            evidence_id=item.evidence_id,
            evidence_text=item.evidence_text,
            source_section_id=item.source_section_id,
            source_block_id=item.source_block_id,
            source_bullet_id=item.source_bullet_id,
        )
        for item in snapshot.evidence_items
        if CandidateEvidenceScope.RESUME_TAILORING in item.allowed_scopes
    )
    if not tailoring_evidence:
        return TailorResumeResult(
            status=ResumeTailoringStatus.DEFERRED_INSUFFICIENT_EVIDENCE,
            subject_id=subject_id,
            application_plan_id=plan_id,
            tailoring_binding=binding,
            draft=None,
            write_result=None,
            reason_code=None,
            retryable=False,
            message="No RESUME_TAILORING evidence is available for tailoring.",
        )

    context = ResumeTailoringContext(
        subject_id=subject_id,
        application_plan_id=plan_id,
        job=ResumeTailoringJobContext.from_job(job),
        source_projection=projection,
        evidence_items=tailoring_evidence,
        user_preparation_instructions=plan.user_preparation_instructions,
        agent_policy=RESUME_TAILORING_AGENT_POLICY,
        agent_policy_version=RESUME_TAILORING_POLICY_VERSION,
        correction_constraints=correction_constraints,
    )
    try:
        output = await agent.tailor(context)
    except TimeoutError:
        return _failure(
            command,
            ResumeTailoringFailureReason.AGENT_TIMEOUT,
            retryable=True,
            tailoring_binding=binding,
        )
    except ResumeTailoringAgentUnavailableError as error:
        return _failure(
            command,
            ResumeTailoringFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            tailoring_binding=binding,
            diagnostic_code=(str(error) or None),
        )
    except Exception as error:
        return _failure(
            command,
            ResumeTailoringFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            tailoring_binding=binding,
            diagnostic_code=type(error).__name__,
        )
    if not isinstance(output, ResumeTailoringAgentOutput):
        return _needs_human(
            command,
            tailoring_binding=binding,
            detail="the Agent did not return a typed structured result.",
            diagnostic_code="UNTYPED_STRUCTURED_RESULT",
        )
    if (
        output.disposition
        is ResumeTailoringAgentDisposition.INSUFFICIENT_EVIDENCE
    ):
        return TailorResumeResult(
            status=ResumeTailoringStatus.DEFERRED_INSUFFICIENT_EVIDENCE,
            subject_id=subject_id,
            application_plan_id=plan_id,
            tailoring_binding=binding,
            draft=None,
            write_result=None,
            reason_code=None,
            retryable=False,
            message=(
                "The Agent reported insufficient evidence for safe tailoring."
            ),
        )
    try:
        sections = _validate_agent_sections(
            output=output,
            projection=projection,
            snapshot=snapshot,
            job=job,
            user_instructions=plan.user_preparation_instructions,
        )
    except _OutputRejected as rejection:
        return _needs_human(
            command,
            tailoring_binding=binding,
            detail=f"{rejection}.",
            diagnostic_code=_output_rejection_diagnostic(rejection),
        )
    except (AttributeError, TypeError, ValueError) as error:
        return _needs_human(
            command,
            tailoring_binding=binding,
            detail="the Agent output could not be safely validated.",
            diagnostic_code=(
                "OUTPUT_VALIDATION_" + type(error).__name__.upper()
            ),
        )

    content = {
        "draft_id": draft_id,
        "contract_version": RESUME_TAILORING_CONTRACT_VERSION,
        "tailoring_binding": binding,
        "subject_id": subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": job.job_id,
        "job_revision": job.revision,
        "job_content_hash": job.content_hash,
        "resume_selection_decision_id": selection.decision_id,
        "source_resume_id": candidate.resume_id,
        "source_artifact_sha256": candidate.artifact_sha256,
        "source_projection_id": projection.projection_id,
        "source_projection_hash": projection.projection_content_hash,
        "evidence_snapshot_id": snapshot.snapshot_id,
        "evidence_snapshot_hash": snapshot.snapshot_content_hash,
        "user_preparation_instructions_hash": (
            plan.user_preparation_instructions_hash
        ),
        "agent_version": metadata.agent_version,
        "prompt_version": metadata.prompt_version,
        "model_id": metadata.model_id,
        "agent_policy_version": RESUME_TAILORING_POLICY_VERSION,
        "rationale": output.rationale,
        "sections": [item.to_dict() for item in sections],
    }
    draft = TailoredResumeDraft(
        draft_id=draft_id,
        contract_version=RESUME_TAILORING_CONTRACT_VERSION,
        tailoring_binding=binding,
        subject_id=subject_id,
        application_plan_id=plan.plan_id,
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        resume_selection_decision_id=selection.decision_id,
        source_resume_id=candidate.resume_id,
        source_artifact_sha256=candidate.artifact_sha256,
        source_projection_id=projection.projection_id,
        source_projection_hash=projection.projection_content_hash,
        evidence_snapshot_id=snapshot.snapshot_id,
        evidence_snapshot_hash=snapshot.snapshot_content_hash,
        user_preparation_instructions_hash=(
            plan.user_preparation_instructions_hash
        ),
        agent_version=metadata.agent_version,
        prompt_version=metadata.prompt_version,
        model_id=metadata.model_id,
        agent_policy_version=RESUME_TAILORING_POLICY_VERSION,
        rationale=output.rationale,
        sections=sections,
        draft_content_hash=_canonical_hash(content),
        created_at=now,
    )
    try:
        write_result = draft_repository.save(draft)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeTailoringFailureReason.DRAFT_PERSISTENCE_FAILED,
            retryable=True,
            tailoring_binding=binding,
        )
    if write_result.status is TailoredResumeDraftWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or ResumeTailoringFailureReason.DRAFT_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            tailoring_binding=binding,
            write_result=write_result,
        )
    result_status = ResumeTailoringStatus(write_result.status.value)
    return TailorResumeResult(
        status=result_status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        tailoring_binding=binding,
        draft=write_result.draft,
        write_result=write_result,
        reason_code=None,
        retryable=False,
        message=(
            "The tailored resume draft was created."
            if result_status is ResumeTailoringStatus.CREATED
            else "The existing tailored resume draft is unchanged."
        ),
    )


_TAILORED_RESUME_FAILURE_REASON_MAP = {
    reason: TailoredResumeDraftStopReason[reason.name]
    for reason in ResumeTailoringFailureReason
}


def tailored_resume_draft_public_result(
    result: TailorResumeResult,
) -> PublicPreparationStageResult:
    """Adapt every authoritative P2a4c outcome to stage-result v2."""

    if not isinstance(result, TailorResumeResult):
        raise TypeError("result must be a tailored-resume result")
    stage = ApplicationPreparationStage.RESUME_TAILORING
    if result.status in {
        ResumeTailoringStatus.CREATED,
        ResumeTailoringStatus.UNCHANGED,
    }:
        if result.draft is None:
            raise ValueError("successful tailoring has no draft")
        constructor = (
            PublicPreparationStageResult.completed
            if result.status is ResumeTailoringStatus.CREATED
            else PublicPreparationStageResult.unchanged
        )
        return constructor(
            stage=stage,
            result_id=result.draft.draft_id,
            result_content_hash=result.draft.draft_content_hash,
            outputs={"tailored_resume_draft_id": result.draft.draft_id},
        )
    if (
        result.status
        is ResumeTailoringStatus.DEFERRED_INSUFFICIENT_EVIDENCE
    ):
        reason = TailoredResumeDraftStopReason.INSUFFICIENT_EVIDENCE
        outcome = PreparationStageOutcome.DEFERRED
    else:
        if result.reason_code is None:
            raise ValueError("stopped tailoring has no authoritative reason")
        try:
            reason = _TAILORED_RESUME_FAILURE_REASON_MAP[result.reason_code]
        except KeyError as error:
            raise ValueError("unmapped tailoring stop reason") from error
        outcome = (
            PreparationStageOutcome.DEFERRED
            if result.status is ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN
            else PreparationStageOutcome.FAILED
        )
    stop_reason = PreparationStopReasonEnvelope(
        stage=stage,
        code=reason,
        contract_version=TAILORED_RESUME_DRAFT_STOP_REASON_CONTRACT_VERSION,
        outcome=outcome,
        diagnostic_code=result.diagnostic_code,
        upstream_lineage_id=result.tailoring_binding or None,
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
        human_attention_required=(
            result.status is ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN
        ),
    )


__all__ = [
    "MAX_TAILORED_BULLET_CHARS",
    "PrivateHomeTailoredResumeDraftRepository",
    "RESUME_TAILORING_AGENT_POLICY",
    "RESUME_TAILORING_CONTRACT_VERSION",
    "RESUME_TAILORING_POLICY_VERSION",
    "ResumeTailoringAgentDisposition",
    "ResumeTailoringAgentMetadata",
    "ResumeTailoringAgentOutput",
    "ResumeTailoringAgentPort",
    "ResumeTailoringAgentUnavailableError",
    "ResumeTailoringContext",
    "ResumeTailoringEvidenceView",
    "ResumeTailoringFailureReason",
    "ResumeTailoringJobContext",
    "ResumeTailoringStatus",
    "TailorResumeCommand",
    "TailorResumeResult",
    "TailoredBulletChangeType",
    "TailoredBulletProposal",
    "TailoredResumeBullet",
    "TailoredResumeDraft",
    "TailoredResumeDraftReadResult",
    "TailoredResumeDraftReadStatus",
    "TailoredResumeDraftRepository",
    "TailoredResumeDraftWriteResult",
    "TailoredResumeDraftWriteStatus",
    "TailoredResumeSection",
    "TailoredSectionProposal",
    "WEAK_LEADING_VERBS",
    "tailor_resume",
    "tailored_resume_draft_public_result",
]
