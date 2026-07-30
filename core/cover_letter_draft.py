"""Evidence-bound cover letter drafts with deterministic output validation."""

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
    COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    CoverLetterDraftStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PublicPreparationStageResult,
)
from .cover_letter_evidence import (
    CoverLetterEvidenceScope,
    CoverLetterEvidenceSnapshot,
    CoverLetterEvidenceSnapshotReadStatus,
    CoverLetterEvidenceSnapshotRepository,
)
from .job_discovery import (
    JobPosting,
    JobPostingReadRepository,
    JobPostingRepositoryError,
)
from .private_home import PrivateHome, PrivateHomeError
if TYPE_CHECKING:
    from .unsupported_claim_correction import (
        UnsupportedClaimCorrectionConstraint,
        UnsupportedClaimCorrectionDirectiveProvider,
    )


COVER_LETTER_DRAFT_CONTRACT_VERSION = "cover-letter-draft-v1"
COVER_LETTER_DRAFT_POLICY_VERSION = "cover-letter-draft-policy-v1"

COVER_LETTER_DRAFT_AGENT_POLICY = """Cover Letter Agent policy (static, non-negotiable):

You write one cover letter for one specific job, from supplied facts only.

Instruction priority (highest first):
    1. Facts and the prohibition on fabrication.
    2. The current ApplicationPlan's user preparation instructions.
    3. Alignment with the trusted job description.
    4. Default writing style.

Correction directives are constraints, never new facts or evidence. REMOVE
means omit the identified unsupported claim. REWRITE may use only the supplied
CandidateEvidence and must not treat the user's wording as evidence.

You must never fabricate:
- A skill, experience, responsibility, number, outcome, degree or personal
  background fact the supplied CandidateEvidence does not state.
- The hiring manager's name. If none is supplied, use a role-based or
  generic greeting instead of guessing.
- Personal experience with the company's culture, mission or product that
  the evidence does not support.

Every candidate-specific statement must be supported by the supplied
CandidateEvidence. The job description may only describe what the role
requires — it is never evidence that the candidate has a given skill or
experience. Every JD alignment reference must be an exact excerpt of the
supplied job description.

Do not stack resume bullets verbatim into paragraph form. Select the few
most relevant pieces of evidence for this JD and build one coherent
narrative. Keep the tone professional, direct and concise; avoid empty
flattery and generic filler phrases.

Return the complete letter as typed structured output: a greeting, ordered
paragraphs (each with a purpose, exact text, cited evidence IDs and JD
alignment references), and a closing.
"""

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DRAFT_ID_PATTERN = re.compile(r"^cover-letter-draft-[a-f0-9]{64}$")
_PARAGRAPH_ID_PATTERN = re.compile(r"^cover-letter-paragraph-[a-f0-9]{64}$")
_WORD_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#/]*")
_NUMBER_PATTERN = re.compile(r"\d")
_PLACEHOLDER_PATTERN = re.compile(
    r"\[[^\]\n]{1,80}\]|\{[^}\n]{1,80}\}|\bTBD\b|\bT\.B\.D\.\b"
    r"|\bhiring manager'?s? name\b|\bcompany name\b|\bxxx+\b",
    re.IGNORECASE,
)
MAX_COVER_LETTER_TEXT_CHARS = 4_000
MAX_GREETING_CHARS = 200
MAX_CLOSING_CHARS = 400
MAX_RATIONALE_CHARS = 2_000


class CoverLetterParagraphPurpose(str, Enum):
    INTRODUCTION = "INTRODUCTION"
    QUALIFICATION = "QUALIFICATION"
    MOTIVATION = "MOTIVATION"
    CLOSING = "CLOSING"


class CoverLetterDraftWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class CoverLetterDraftReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CoverLetterDraftStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_INSUFFICIENT_EVIDENCE = "DEFERRED_INSUFFICIENT_EVIDENCE"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class CoverLetterDraftFailureReason(str, Enum):
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


class CoverLetterAgentUnavailableError(RuntimeError):
    """Raised when the bounded cover-letter Agent cannot return an output."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the cover-letter contract")
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
        "created_at", datetime.fromisoformat(value.replace("Z", "+00:00"))
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
class CoverLetterAgentMetadata:
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
class CoverLetterJobContext:
    job_id: str
    revision: int
    content_hash: str
    company: str
    title: str
    description: str
    location: str
    work_mode: str

    @classmethod
    def from_job(cls, job: JobPosting) -> "CoverLetterJobContext":
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
class CoverLetterEvidenceView:
    evidence_id: str
    evidence_text: str


@dataclass(frozen=True, slots=True)
class CoverLetterAgentContext:
    subject_id: str
    application_plan_id: str
    job: CoverLetterJobContext
    evidence_items: tuple[CoverLetterEvidenceView, ...]
    user_preparation_instructions: str | None
    agent_policy: str
    agent_policy_version: str
    correction_constraints: tuple[
        UnsupportedClaimCorrectionConstraint, ...
    ]


@dataclass(frozen=True, slots=True)
class CoverLetterParagraphProposal:
    purpose: CoverLetterParagraphPurpose
    text: str
    evidence_ids: tuple[str, ...]
    jd_alignment: tuple[str, ...]

    def __post_init__(self) -> None:
        purpose = CoverLetterParagraphPurpose(self.purpose)
        object.__setattr__(self, "purpose", purpose)
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > MAX_COVER_LETTER_TEXT_CHARS
        ):
            raise ValueError("paragraph text is outside the contract")
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
        if (
            purpose
            in {
                CoverLetterParagraphPurpose.QUALIFICATION,
                CoverLetterParagraphPurpose.MOTIVATION,
            }
            and not self.evidence_ids
        ):
            raise ValueError(
                "a qualification or motivation paragraph requires evidence"
            )


@dataclass(frozen=True, slots=True)
class CoverLetterAgentOutput:
    greeting: str
    paragraphs: tuple[CoverLetterParagraphProposal, ...]
    closing: str
    rationale: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.greeting, str)
            or not self.greeting.strip()
            or len(self.greeting) > MAX_GREETING_CHARS
        ):
            raise ValueError("greeting is outside the contract")
        if (
            not isinstance(self.closing, str)
            or not self.closing.strip()
            or len(self.closing) > MAX_CLOSING_CHARS
        ):
            raise ValueError("closing is outside the contract")
        _clean_text("rationale", self.rationale, maximum=MAX_RATIONALE_CHARS)
        if not isinstance(self.paragraphs, tuple) or not self.paragraphs or any(
            not isinstance(item, CoverLetterParagraphProposal)
            for item in self.paragraphs
        ):
            raise TypeError(
                "paragraphs must be a non-empty tuple of typed proposals"
            )


@runtime_checkable
class CoverLetterAgentPort(Protocol):
    async def generate(
        self, context: CoverLetterAgentContext
    ) -> CoverLetterAgentOutput:
        """Draft one cover letter from supplied evidence, tool-free."""


def _draft_binding(
    *,
    plan: ApplicationPlan,
    job: JobPosting,
    snapshot: CoverLetterEvidenceSnapshot,
    metadata: CoverLetterAgentMetadata,
    correction_constraints: tuple[
        UnsupportedClaimCorrectionConstraint, ...
    ] = (),
) -> str:
    return _canonical_hash(
        {
            "application_plan_id": plan.plan_id,
            "cover_letter_draft_agent_version": metadata.agent_version,
            "cover_letter_draft_contract_version": (
                COVER_LETTER_DRAFT_CONTRACT_VERSION
            ),
            "cover_letter_draft_model_id": metadata.model_id,
            "cover_letter_draft_policy_version": (
                COVER_LETTER_DRAFT_POLICY_VERSION
            ),
            "cover_letter_draft_prompt_version": metadata.prompt_version,
            "evidence_snapshot_hash": snapshot.snapshot_content_hash,
            "evidence_snapshot_id": snapshot.snapshot_id,
            "job_content_hash": job.content_hash,
            "job_id": job.job_id,
            "job_revision": job.revision,
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


def _paragraph_content(
    *,
    order: int,
    purpose: CoverLetterParagraphPurpose,
    text: str,
    evidence_ids: tuple[str, ...],
    jd_alignment: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "evidence_ids": list(evidence_ids),
        "jd_alignment": list(jd_alignment),
        "order": order,
        "purpose": purpose.value,
        "text": text,
    }


@dataclass(frozen=True, slots=True)
class CoverLetterParagraph:
    paragraph_id: str
    order: int
    purpose: CoverLetterParagraphPurpose
    text: str
    evidence_ids: tuple[str, ...]
    jd_alignment: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.order) is not int or self.order < 0:
            raise ValueError("paragraph order must be a non-negative integer")
        purpose = CoverLetterParagraphPurpose(self.purpose)
        object.__setattr__(self, "purpose", purpose)
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > MAX_COVER_LETTER_TEXT_CHARS
        ):
            raise ValueError("paragraph text is outside the contract")
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
        expected_id = "cover-letter-paragraph-" + _canonical_hash(
            self.content_dict()
        )
        if (
            not isinstance(self.paragraph_id, str)
            or _PARAGRAPH_ID_PATTERN.fullmatch(self.paragraph_id) is None
            or self.paragraph_id != expected_id
        ):
            raise ValueError("paragraph_id does not match its content")

    def content_dict(self) -> dict[str, Any]:
        return _paragraph_content(
            order=self.order,
            purpose=self.purpose,
            text=self.text,
            evidence_ids=self.evidence_ids,
            jd_alignment=self.jd_alignment,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"paragraph_id": self.paragraph_id, **self.content_dict()}


@dataclass(frozen=True, slots=True)
class CoverLetterDraft:
    draft_id: str
    contract_version: str
    draft_binding: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    evidence_snapshot_id: str
    evidence_snapshot_hash: str
    user_preparation_instructions_hash: str
    agent_version: str
    prompt_version: str
    model_id: str
    agent_policy_version: str
    greeting: str
    paragraphs: tuple[CoverLetterParagraph, ...]
    closing: str
    rationale: str
    draft_content_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != COVER_LETTER_DRAFT_CONTRACT_VERSION:
            raise ValueError("cover-letter draft contract is unsupported")
        binding = _require_hash("draft_binding", self.draft_binding)
        if (
            not isinstance(self.draft_id, str)
            or _DRAFT_ID_PATTERN.fullmatch(self.draft_id) is None
            or self.draft_id != f"cover-letter-draft-{binding}"
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
        if policy_version != COVER_LETTER_DRAFT_POLICY_VERSION:
            raise ValueError("cover-letter policy version is unsupported")
        if (
            not isinstance(self.greeting, str)
            or not self.greeting.strip()
            or len(self.greeting) > MAX_GREETING_CHARS
        ):
            raise ValueError("greeting is outside the contract")
        if (
            not isinstance(self.closing, str)
            or not self.closing.strip()
            or len(self.closing) > MAX_CLOSING_CHARS
        ):
            raise ValueError("closing is outside the contract")
        _clean_text("rationale", self.rationale, maximum=MAX_RATIONALE_CHARS)
        if (
            not isinstance(self.paragraphs, tuple)
            or not self.paragraphs
            or any(
                not isinstance(item, CoverLetterParagraph)
                for item in self.paragraphs
            )
        ):
            raise TypeError("paragraphs must be a non-empty typed tuple")
        if tuple(item.order for item in self.paragraphs) != tuple(
            range(len(self.paragraphs))
        ):
            raise ValueError("paragraphs must have contiguous order")
        paragraph_ids = [item.paragraph_id for item in self.paragraphs]
        if len(paragraph_ids) != len(set(paragraph_ids)):
            raise ValueError("paragraph identities must be unique")
        object.__setattr__(self, "contract_version", contract)
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
            "draft_binding": self.draft_binding,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "evidence_snapshot_hash": self.evidence_snapshot_hash,
            "user_preparation_instructions_hash": (
                self.user_preparation_instructions_hash
            ),
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "agent_policy_version": self.agent_policy_version,
            "greeting": self.greeting,
            "paragraphs": [item.to_dict() for item in self.paragraphs],
            "closing": self.closing,
            "rationale": self.rationale,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "draft_content_hash": self.draft_content_hash,
            "created_at": _rfc3339(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class CoverLetterDraftWriteResult:
    status: CoverLetterDraftWriteStatus
    draft: CoverLetterDraft | None
    reason_code: CoverLetterDraftFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = CoverLetterDraftWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CoverLetterDraftFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            CoverLetterDraftWriteStatus.CREATED,
            CoverLetterDraftWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.draft, CoverLetterDraft)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful draft write result is invalid")
        elif self.draft is not None or self.reason_code is None:
            raise ValueError("failed draft write result is invalid")


@dataclass(frozen=True, slots=True)
class CoverLetterDraftReadResult:
    status: CoverLetterDraftReadStatus
    draft: CoverLetterDraft | None
    reason_code: CoverLetterDraftFailureReason | None = None

    def __post_init__(self) -> None:
        status = CoverLetterDraftReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CoverLetterDraftFailureReason(self.reason_code),
            )
        if status is CoverLetterDraftReadStatus.FOUND:
            if (
                not isinstance(self.draft, CoverLetterDraft)
                or self.reason_code is not None
            ):
                raise ValueError("found draft read result is invalid")
        elif status is CoverLetterDraftReadStatus.NOT_FOUND:
            if self.draft is not None or self.reason_code is not None:
                raise ValueError("not-found draft read result is invalid")
        elif (
            self.draft is not None
            or self.reason_code
            is not CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure draft read result is invalid")


@runtime_checkable
class CoverLetterDraftRepository(Protocol):
    def save(
        self, draft: CoverLetterDraft
    ) -> CoverLetterDraftWriteResult:
        """Persist one immutable cover-letter draft."""

    def get(
        self, *, subject_id: str, draft_id: str
    ) -> CoverLetterDraftReadResult:
        """Read one subject-owned cover-letter draft."""


def _paragraph_from_dict(value: Any) -> CoverLetterParagraph:
    expected = {
        "paragraph_id",
        "order",
        "purpose",
        "text",
        "evidence_ids",
        "jd_alignment",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["evidence_ids"], list)
        or not isinstance(value["jd_alignment"], list)
    ):
        raise ValueError("persisted CoverLetterParagraph is invalid")
    return CoverLetterParagraph(
        paragraph_id=value["paragraph_id"],
        order=value["order"],
        purpose=CoverLetterParagraphPurpose(value["purpose"]),
        text=value["text"],
        evidence_ids=tuple(value["evidence_ids"]),
        jd_alignment=tuple(value["jd_alignment"]),
    )


def _draft_from_dict(value: Any) -> CoverLetterDraft:
    expected = {
        "draft_id",
        "contract_version",
        "draft_binding",
        "subject_id",
        "application_plan_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "evidence_snapshot_id",
        "evidence_snapshot_hash",
        "user_preparation_instructions_hash",
        "agent_version",
        "prompt_version",
        "model_id",
        "agent_policy_version",
        "greeting",
        "paragraphs",
        "closing",
        "rationale",
        "draft_content_hash",
        "created_at",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["paragraphs"], list)
    ):
        raise ValueError("persisted CoverLetterDraft is invalid")
    return CoverLetterDraft(
        draft_id=value["draft_id"],
        contract_version=value["contract_version"],
        draft_binding=value["draft_binding"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        evidence_snapshot_id=value["evidence_snapshot_id"],
        evidence_snapshot_hash=value["evidence_snapshot_hash"],
        user_preparation_instructions_hash=value[
            "user_preparation_instructions_hash"
        ],
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        agent_policy_version=value["agent_policy_version"],
        greeting=value["greeting"],
        paragraphs=tuple(
            _paragraph_from_dict(item) for item in value["paragraphs"]
        ),
        closing=value["closing"],
        rationale=value["rationale"],
        draft_content_hash=value["draft_content_hash"],
        created_at=_parse_timestamp(value["created_at"]),
    )


class PrivateHomeCoverLetterDraftRepository:
    """Immutable, subject-scoped cover-letter drafts in Private Home."""

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
            self._home.paths.cover_letter_drafts
            / _subject_storage_key(subject)
            / f"{draft_id}.json"
        )

    def get(
        self, *, subject_id: str, draft_id: str
    ) -> CoverLetterDraftReadResult:
        path = self._path(subject_id, draft_id)
        with self._lock:
            if not path.exists():
                return CoverLetterDraftReadResult(
                    status=CoverLetterDraftReadStatus.NOT_FOUND,
                    draft=None,
                )
            if path.is_symlink() or not path.is_file():
                return CoverLetterDraftReadResult(
                    status=CoverLetterDraftReadStatus.INTEGRITY_FAILURE,
                    draft=None,
                    reason_code=(
                        CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE
                    ),
                )
            try:
                draft = _draft_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return CoverLetterDraftReadResult(
                    status=CoverLetterDraftReadStatus.INTEGRITY_FAILURE,
                    draft=None,
                    reason_code=(
                        CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE
                    ),
                )
            if (
                draft.subject_id != subject_id.strip()
                or draft.draft_id != draft_id
                or path.name != f"{draft.draft_id}.json"
            ):
                return CoverLetterDraftReadResult(
                    status=CoverLetterDraftReadStatus.INTEGRITY_FAILURE,
                    draft=None,
                    reason_code=(
                        CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE
                    ),
                )
            return CoverLetterDraftReadResult(
                status=CoverLetterDraftReadStatus.FOUND,
                draft=draft,
            )

    def save(
        self, draft: CoverLetterDraft
    ) -> CoverLetterDraftWriteResult:
        if not isinstance(draft, CoverLetterDraft):
            raise TypeError("draft must be a CoverLetterDraft")
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
                return CoverLetterDraftWriteResult(
                    status=CoverLetterDraftWriteStatus.FAILED,
                    draft=None,
                    reason_code=(
                        CoverLetterDraftFailureReason.DRAFT_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return CoverLetterDraftWriteResult(
                    status=CoverLetterDraftWriteStatus.CREATED,
                    draft=draft,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=draft.subject_id, draft_id=draft.draft_id
            )
            if (
                existing.status is CoverLetterDraftReadStatus.FOUND
                and existing.draft is not None
                and existing.draft.content_dict() == draft.content_dict()
            ):
                return CoverLetterDraftWriteResult(
                    status=CoverLetterDraftWriteStatus.UNCHANGED,
                    draft=existing.draft,
                    reason_code=None,
                    retryable=False,
                )
            return CoverLetterDraftWriteResult(
                status=CoverLetterDraftWriteStatus.FAILED,
                draft=None,
                reason_code=(
                    CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class DraftCoverLetterCommand:
    subject_id: str
    application_plan_id: str
    cover_letter_evidence_snapshot_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class DraftCoverLetterResult:
    status: CoverLetterDraftStatus
    subject_id: str
    application_plan_id: str
    draft_binding: str
    draft: CoverLetterDraft | None
    write_result: CoverLetterDraftWriteResult | None
    reason_code: CoverLetterDraftFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = CoverLetterDraftStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CoverLetterDraftFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            CoverLetterDraftStatus.CREATED,
            CoverLetterDraftStatus.UNCHANGED,
        }:
            expected = CoverLetterDraftWriteStatus(status.value)
            if (
                not isinstance(self.draft, CoverLetterDraft)
                or not isinstance(
                    self.write_result, CoverLetterDraftWriteResult
                )
                or self.write_result.status is not expected
                or self.write_result.draft != self.draft
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful draft result is invalid")
        elif status is CoverLetterDraftStatus.DEFERRED_INSUFFICIENT_EVIDENCE:
            if (
                self.draft is not None
                or self.write_result is not None
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("deferred draft result is invalid")
        elif status is CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN:
            if (
                self.draft is not None
                or self.write_result is not None
                or self.reason_code
                is not CoverLetterDraftFailureReason.AGENT_OUTPUT_UNSAFE
                or self.retryable
            ):
                raise ValueError("needs-human draft result is invalid")
        elif self.draft is not None or self.reason_code is None:
            raise ValueError("failed draft result is invalid")


def _failure(
    command: DraftCoverLetterCommand,
    reason: CoverLetterDraftFailureReason,
    *,
    retryable: bool = False,
    draft_binding: str = "",
    write_result: CoverLetterDraftWriteResult | None = None,
) -> DraftCoverLetterResult:
    return DraftCoverLetterResult(
        status=CoverLetterDraftStatus.FAILED,
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
        draft_binding=draft_binding,
        draft=None,
        write_result=write_result,
        reason_code=reason,
        retryable=retryable,
        message=f"Cover letter drafting failed: {reason.value}.",
    )


def _needs_human(
    command: DraftCoverLetterCommand, *, draft_binding: str, detail: str
) -> DraftCoverLetterResult:
    return DraftCoverLetterResult(
        status=CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN,
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        draft_binding=draft_binding,
        draft=None,
        write_result=None,
        reason_code=CoverLetterDraftFailureReason.AGENT_OUTPUT_UNSAFE,
        retryable=False,
        message=f"The cover letter draft needs human review: {detail}",
    )


class _OutputRejected(ValueError):
    """The Agent output failed a deterministic safety check."""


def _words(text: str) -> tuple[str, ...]:
    return tuple(_WORD_PATTERN.findall(text))


def _casefold_word_set(text: str) -> frozenset[str]:
    return frozenset(word.casefold() for word in _words(text))


def _checkable_tokens(text: str) -> tuple[str, ...]:
    """Numbers and proper-noun-like words that must trace to a legitimate source.

    ``_WORD_PATTERN`` deliberately excludes trailing punctuation characters
    (unlike the resume-tailoring tokenizer), so a sentence-final word is
    compared without a trailing period ever being part of the token.
    """

    tokens = _words(text)
    checked: list[str] = []
    for index, token in enumerate(tokens):
        if _NUMBER_PATTERN.search(token):
            checked.append(token)
        elif index > 0 and any(char.isupper() for char in token):
            checked.append(token)
    return tuple(checked)


def _contains_placeholder(text: str) -> bool:
    return _PLACEHOLDER_PATTERN.search(text) is not None


def _validate_agent_output(
    *,
    output: CoverLetterAgentOutput,
    snapshot: CoverLetterEvidenceSnapshot,
    job: JobPosting,
) -> tuple[CoverLetterParagraph, ...]:
    """Deterministically verify every claim, reference and structural rule."""

    evidence_by_id = {
        item.evidence_id: item for item in snapshot.evidence_items
    }
    jd_words = _casefold_word_set(job.description)
    all_text = output.greeting + " " + output.closing
    for paragraph in output.paragraphs:
        all_text += " " + paragraph.text
    if _contains_placeholder(all_text):
        raise _OutputRejected(
            "the draft contains an unresolved placeholder"
        )

    seen_orders: set[int] = set()
    paragraphs: list[CoverLetterParagraph] = []
    for order, proposal in enumerate(output.paragraphs):
        for evidence_id in proposal.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                raise _OutputRejected("unknown evidence reference")
            if (
                CoverLetterEvidenceScope.COVER_LETTER
                not in evidence.allowed_scopes
            ):
                raise _OutputRejected("evidence scope is not permitted")
        for reference in proposal.jd_alignment:
            if reference not in job.description:
                raise _OutputRejected(
                    "JD alignment reference is not in the job description"
                )

        if proposal.purpose in {
            CoverLetterParagraphPurpose.QUALIFICATION,
            CoverLetterParagraphPurpose.MOTIVATION,
        }:
            allowed_words: set[str] = set()
            for evidence_id in proposal.evidence_ids:
                allowed_words |= _casefold_word_set(
                    evidence_by_id[evidence_id].evidence_text
                )
            tokens = _words(proposal.text)
            if tokens:
                leading = tokens[0].casefold()
                if leading in jd_words and leading not in allowed_words:
                    raise _OutputRejected(
                        "a JD-derived claim about the role is presented as "
                        "a fact about the candidate without evidence"
                    )
            for token in _checkable_tokens(proposal.text):
                folded = token.casefold()
                if folded not in allowed_words and folded not in jd_words:
                    raise _OutputRejected(
                        f"the paragraph asserts {token} without supporting "
                        "evidence or JD context"
                    )
                if folded in jd_words and folded not in allowed_words:
                    raise _OutputRejected(
                        "a job-description detail is presented as a "
                        "candidate fact without evidence"
                    )

        paragraphs.append(
            CoverLetterParagraph(
                paragraph_id=(
                    "cover-letter-paragraph-"
                    + _canonical_hash(
                        _paragraph_content(
                            order=order,
                            purpose=proposal.purpose,
                            text=proposal.text,
                            evidence_ids=proposal.evidence_ids,
                            jd_alignment=proposal.jd_alignment,
                        )
                    )
                ),
                order=order,
                purpose=proposal.purpose,
                text=proposal.text,
                evidence_ids=proposal.evidence_ids,
                jd_alignment=proposal.jd_alignment,
            )
        )
        seen_orders.add(order)

    if seen_orders != set(range(len(output.paragraphs))):
        raise _OutputRejected("paragraph order is not contiguous")
    return tuple(paragraphs)


async def draft_cover_letter(
    command: DraftCoverLetterCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    job_repository: JobPostingReadRepository,
    evidence_snapshot_repository: CoverLetterEvidenceSnapshotRepository,
    agent: CoverLetterAgentPort,
    metadata: CoverLetterAgentMetadata,
    draft_repository: CoverLetterDraftRepository,
    correction_provider: (
        UnsupportedClaimCorrectionDirectiveProvider | None
    ) = None,
) -> DraftCoverLetterResult:
    """Create one evidence-bound cover letter draft, at most one Agent call."""

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
            "cover_letter_evidence_snapshot_id",
            command.cover_letter_evidence_snapshot_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
        if not isinstance(metadata, CoverLetterAgentMetadata):
            raise TypeError("metadata must be CoverLetterAgentMetadata")
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, CoverLetterDraftFailureReason.INVALID_REQUEST
        )

    try:
        plan_result = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CoverLetterDraftFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_result.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command, CoverLetterDraftFailureReason.APPLICATION_PLAN_NOT_FOUND
        )
    if (
        plan_result.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_result.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            CoverLetterDraftFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_result.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            CoverLetterDraftFailureReason.APPLICATION_PLAN_SUBJECT_MISMATCH,
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
            command, CoverLetterDraftFailureReason.JOB_READ_FAILED
        )
    if job is None:
        return _failure(
            command, CoverLetterDraftFailureReason.JOB_NOT_FOUND
        )
    if (
        not isinstance(job, JobPosting)
        or job.job_id != plan.job_id
        or job.revision != plan.job_revision
        or job.content_hash != plan.job_content_hash
    ):
        return _failure(
            command, CoverLetterDraftFailureReason.JOB_BINDING_MISMATCH
        )

    try:
        snapshot_result = evidence_snapshot_repository.get(
            subject_id=subject_id, snapshot_id=snapshot_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CoverLetterDraftFailureReason
            .EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE,
        )
    if (
        snapshot_result.status
        is CoverLetterEvidenceSnapshotReadStatus.NOT_FOUND
    ):
        return _failure(
            command,
            CoverLetterDraftFailureReason.EVIDENCE_SNAPSHOT_NOT_FOUND,
        )
    if (
        snapshot_result.status
        is not CoverLetterEvidenceSnapshotReadStatus.FOUND
        or not isinstance(
            snapshot_result.snapshot, CoverLetterEvidenceSnapshot
        )
    ):
        return _failure(
            command,
            CoverLetterDraftFailureReason
            .EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE,
        )
    snapshot = snapshot_result.snapshot
    if (
        snapshot.subject_id != subject_id
        or snapshot.application_plan_id != plan.plan_id
        or snapshot.job_id != plan.job_id
    ):
        return _failure(
            command,
            CoverLetterDraftFailureReason
            .EVIDENCE_SNAPSHOT_BINDING_MISMATCH,
        )

    correction_constraints: tuple[
        UnsupportedClaimCorrectionConstraint, ...
    ] = ()
    if correction_provider is not None:
        correction_result = correction_provider.list_current(
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            material_kind="COVER_LETTER",
        )
        if not correction_result.succeeded:
            return _failure(
                command,
                CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE,
            )
        correction_constraints = tuple(
            item.constraint for item in correction_result.directives
        )
    binding = _draft_binding(
        plan=plan,
        job=job,
        snapshot=snapshot,
        metadata=metadata,
        correction_constraints=correction_constraints,
    )
    draft_id = f"cover-letter-draft-{binding}"
    try:
        existing = draft_repository.get(
            subject_id=subject_id, draft_id=draft_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE,
            draft_binding=binding,
        )
    if existing.status is CoverLetterDraftReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE,
            draft_binding=binding,
        )
    if (
        existing.status is CoverLetterDraftReadStatus.FOUND
        and existing.draft is not None
    ):
        write_result = CoverLetterDraftWriteResult(
            status=CoverLetterDraftWriteStatus.UNCHANGED,
            draft=existing.draft,
            reason_code=None,
            retryable=False,
        )
        return DraftCoverLetterResult(
            status=CoverLetterDraftStatus.UNCHANGED,
            subject_id=subject_id,
            application_plan_id=plan_id,
            draft_binding=binding,
            draft=existing.draft,
            write_result=write_result,
            reason_code=None,
            retryable=False,
            message="The existing cover letter draft is unchanged.",
        )

    cover_letter_evidence = tuple(
        CoverLetterEvidenceView(
            evidence_id=item.evidence_id, evidence_text=item.evidence_text
        )
        for item in snapshot.evidence_items
        if CoverLetterEvidenceScope.COVER_LETTER in item.allowed_scopes
    )
    if not cover_letter_evidence:
        return DraftCoverLetterResult(
            status=CoverLetterDraftStatus.DEFERRED_INSUFFICIENT_EVIDENCE,
            subject_id=subject_id,
            application_plan_id=plan_id,
            draft_binding=binding,
            draft=None,
            write_result=None,
            reason_code=None,
            retryable=False,
            message=(
                "No COVER_LETTER evidence is available for drafting."
            ),
        )

    context = CoverLetterAgentContext(
        subject_id=subject_id,
        application_plan_id=plan_id,
        job=CoverLetterJobContext.from_job(job),
        evidence_items=cover_letter_evidence,
        user_preparation_instructions=plan.user_preparation_instructions,
        agent_policy=COVER_LETTER_DRAFT_AGENT_POLICY,
        agent_policy_version=COVER_LETTER_DRAFT_POLICY_VERSION,
        correction_constraints=correction_constraints,
    )
    try:
        output = await agent.generate(context)
    except TimeoutError:
        return _failure(
            command,
            CoverLetterDraftFailureReason.AGENT_TIMEOUT,
            retryable=True,
            draft_binding=binding,
        )
    except CoverLetterAgentUnavailableError:
        return _failure(
            command,
            CoverLetterDraftFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            draft_binding=binding,
        )
    except Exception:
        return _failure(
            command,
            CoverLetterDraftFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            draft_binding=binding,
        )
    if not isinstance(output, CoverLetterAgentOutput):
        return _needs_human(
            command,
            draft_binding=binding,
            detail="the Agent did not return a typed structured result.",
        )

    try:
        paragraphs = _validate_agent_output(
            output=output,
            snapshot=snapshot,
            job=job,
        )
    except _OutputRejected as rejection:
        return _needs_human(
            command, draft_binding=binding, detail=f"{rejection}."
        )
    except (AttributeError, TypeError, ValueError):
        return _needs_human(
            command,
            draft_binding=binding,
            detail="the Agent output could not be safely validated.",
        )

    if _contains_placeholder(output.greeting) or _contains_placeholder(
        output.closing
    ):
        return _needs_human(
            command,
            draft_binding=binding,
            detail="the greeting or closing contains an unresolved placeholder.",
        )

    content = {
        "draft_id": draft_id,
        "contract_version": COVER_LETTER_DRAFT_CONTRACT_VERSION,
        "draft_binding": binding,
        "subject_id": subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": job.job_id,
        "job_revision": job.revision,
        "job_content_hash": job.content_hash,
        "evidence_snapshot_id": snapshot.snapshot_id,
        "evidence_snapshot_hash": snapshot.snapshot_content_hash,
        "user_preparation_instructions_hash": (
            plan.user_preparation_instructions_hash
        ),
        "agent_version": metadata.agent_version,
        "prompt_version": metadata.prompt_version,
        "model_id": metadata.model_id,
        "agent_policy_version": COVER_LETTER_DRAFT_POLICY_VERSION,
        "greeting": output.greeting,
        "paragraphs": [item.to_dict() for item in paragraphs],
        "closing": output.closing,
        "rationale": output.rationale,
    }
    draft = CoverLetterDraft(
        draft_id=draft_id,
        contract_version=COVER_LETTER_DRAFT_CONTRACT_VERSION,
        draft_binding=binding,
        subject_id=subject_id,
        application_plan_id=plan.plan_id,
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        evidence_snapshot_id=snapshot.snapshot_id,
        evidence_snapshot_hash=snapshot.snapshot_content_hash,
        user_preparation_instructions_hash=(
            plan.user_preparation_instructions_hash
        ),
        agent_version=metadata.agent_version,
        prompt_version=metadata.prompt_version,
        model_id=metadata.model_id,
        agent_policy_version=COVER_LETTER_DRAFT_POLICY_VERSION,
        greeting=output.greeting,
        paragraphs=paragraphs,
        closing=output.closing,
        rationale=output.rationale,
        draft_content_hash=_canonical_hash(content),
        created_at=now,
    )
    try:
        write_result = draft_repository.save(draft)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CoverLetterDraftFailureReason.DRAFT_PERSISTENCE_FAILED,
            retryable=True,
            draft_binding=binding,
        )
    if write_result.status is CoverLetterDraftWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or CoverLetterDraftFailureReason.DRAFT_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            draft_binding=binding,
            write_result=write_result,
        )
    result_status = CoverLetterDraftStatus(write_result.status.value)
    return DraftCoverLetterResult(
        status=result_status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        draft_binding=binding,
        draft=write_result.draft,
        write_result=write_result,
        reason_code=None,
        retryable=False,
        message=(
            "The cover letter draft was created."
            if result_status is CoverLetterDraftStatus.CREATED
            else "The existing cover letter draft is unchanged."
        ),
    )


_COVER_LETTER_DRAFT_FAILURE_REASON_MAP = {
    reason: CoverLetterDraftStopReason[reason.name]
    for reason in CoverLetterDraftFailureReason
}


def cover_letter_draft_public_result(
    result: DraftCoverLetterResult,
) -> PublicPreparationStageResult:
    """Adapt every authoritative P2b2b outcome to stage-result v2."""

    if not isinstance(result, DraftCoverLetterResult):
        raise TypeError("result must be a cover-letter draft result")
    stage = ApplicationPreparationStage.COVER_LETTER_DRAFT
    if result.status in {
        CoverLetterDraftStatus.CREATED,
        CoverLetterDraftStatus.UNCHANGED,
    }:
        if result.draft is None:
            raise ValueError("successful drafting has no draft")
        constructor = (
            PublicPreparationStageResult.completed
            if result.status is CoverLetterDraftStatus.CREATED
            else PublicPreparationStageResult.unchanged
        )
        return constructor(
            stage=stage,
            result_id=result.draft.draft_id,
            result_content_hash=result.draft.draft_content_hash,
            outputs={"cover_letter_draft_id": result.draft.draft_id},
        )
    if result.status is CoverLetterDraftStatus.DEFERRED_INSUFFICIENT_EVIDENCE:
        reason = CoverLetterDraftStopReason.INSUFFICIENT_EVIDENCE
        outcome = PreparationStageOutcome.DEFERRED
    else:
        if result.reason_code is None:
            raise ValueError("stopped drafting has no authoritative reason")
        try:
            reason = _COVER_LETTER_DRAFT_FAILURE_REASON_MAP[
                result.reason_code
            ]
        except KeyError as error:
            raise ValueError(
                "unmapped cover-letter draft stop reason"
            ) from error
        outcome = (
            PreparationStageOutcome.DEFERRED
            if result.status is CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN
            else PreparationStageOutcome.FAILED
        )
    stop_reason = PreparationStopReasonEnvelope(
        stage=stage,
        code=reason,
        contract_version=COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION,
        outcome=outcome,
        upstream_lineage_id=result.draft_binding or None,
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
            result.status is CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN
        ),
    )


__all__ = [
    "COVER_LETTER_DRAFT_AGENT_POLICY",
    "COVER_LETTER_DRAFT_CONTRACT_VERSION",
    "COVER_LETTER_DRAFT_POLICY_VERSION",
    "CoverLetterAgentContext",
    "CoverLetterAgentMetadata",
    "CoverLetterAgentOutput",
    "CoverLetterAgentPort",
    "CoverLetterAgentUnavailableError",
    "CoverLetterDraft",
    "CoverLetterDraftFailureReason",
    "CoverLetterDraftReadResult",
    "CoverLetterDraftReadStatus",
    "CoverLetterDraftRepository",
    "CoverLetterDraftStatus",
    "CoverLetterDraftWriteResult",
    "CoverLetterDraftWriteStatus",
    "CoverLetterEvidenceView",
    "CoverLetterJobContext",
    "CoverLetterParagraph",
    "CoverLetterParagraphProposal",
    "CoverLetterParagraphPurpose",
    "DraftCoverLetterCommand",
    "DraftCoverLetterResult",
    "MAX_CLOSING_CHARS",
    "MAX_COVER_LETTER_TEXT_CHARS",
    "MAX_GREETING_CHARS",
    "MAX_RATIONALE_CHARS",
    "PrivateHomeCoverLetterDraftRepository",
    "draft_cover_letter",
    "cover_letter_draft_public_result",
]
