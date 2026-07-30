"""Independent, evidence-bound fact QA over one immutable TailoredResumeDraft."""

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
from .application_preparation_orchestrator import (
    RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PublicPreparationStageResult,
    ResumeFactQAStopReason,
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
from .resume_selection import (
    ResumeSelectionDecision,
    ResumeSelectionDecisionReadStatus,
    ResumeSelectionDecisionRepository,
)
from .resume_tailoring import (
    TailoredBulletChangeType,
    TailoredResumeDraft,
    TailoredResumeDraftReadStatus,
    TailoredResumeDraftRepository,
)
from .source_resume_projection import (
    SourceResumeProjection,
    SourceResumeProjectionReadStatus,
    SourceResumeProjectionRepository,
)


RESUME_FACT_QA_CONTRACT_VERSION = "resume-fact-qa-v1"
RESUME_FACT_QA_POLICY_VERSION = "resume-fact-qa-policy-v1"

RESUME_FACT_QA_AGENT_POLICY = """Resume Fact QA Agent policy (static, non-negotiable):

You are an independent fact reviewer. You do not write or repair resume
content. You judge only whether each supplied bullet stays inside the
supplied CandidateEvidence.

Judge exactly these questions:
- Does the evidence support the action verb the bullet uses?
- Does the bullet overstate responsibility, ownership, maturity or impact?
- Does the bullet present participation, research, a prototype or an
  experiment as leadership, deployment or a production outcome?
- Does the bullet assert a cause-and-effect link the evidence does not state?
- Does the bullet as a whole exceed the reasonable semantic range of the
  evidence it cites?

Rules:
- Report UNSUPPORTED with at least one finding when any answer shows the
  bullet exceeds its evidence.
- Report SUPPORTED with no findings only when every bullet stays inside its
  evidence.
- Report UNCERTAIN when you cannot reach a reliable verdict.
- Every finding must reference a supplied bullet and evidence IDs from the
  supplied snapshot.
- Never propose replacement text, never edit the draft, never call tools.
"""

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RESULT_ID_PATTERN = re.compile(r"^resume-fact-qa-[a-f0-9]{64}$")
_FINDING_ID_PATTERN = re.compile(r"^resume-fact-qa-finding-[a-f0-9]{64}$")
_WORD_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#./%-]*")
_NUMBER_PATTERN = re.compile(r"\d")
MAX_QA_CLAIM_CHARS = 20_000
MAX_QA_EXPLANATION_CHARS = 2_000


class ResumeFactQAVerdict(str, Enum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"


class ResumeFactQAFindingSource(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    AGENT = "AGENT"


class ResumeFactQAFindingSeverity(str, Enum):
    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"


class ResumeFactQAFindingType(str, Enum):
    MISSING_EVIDENCE_REFERENCE = "MISSING_EVIDENCE_REFERENCE"
    UNKNOWN_EVIDENCE_REFERENCE = "UNKNOWN_EVIDENCE_REFERENCE"
    EVIDENCE_SCOPE_NOT_PERMITTED = "EVIDENCE_SCOPE_NOT_PERMITTED"
    UNKNOWN_SOURCE_REFERENCE = "UNKNOWN_SOURCE_REFERENCE"
    DUPLICATE_SOURCE_REFERENCE = "DUPLICATE_SOURCE_REFERENCE"
    MISSING_SOURCE_COVERAGE = "MISSING_SOURCE_COVERAGE"
    SOURCE_TEXT_ALTERED = "SOURCE_TEXT_ALTERED"
    UNSUPPORTED_FACT_TOKEN = "UNSUPPORTED_FACT_TOKEN"
    UNKNOWN_JD_REFERENCE = "UNKNOWN_JD_REFERENCE"
    UNSUPPORTED_ACTION_VERB = "UNSUPPORTED_ACTION_VERB"
    OVERSTATED_OWNERSHIP = "OVERSTATED_OWNERSHIP"
    OVERSTATED_MATURITY = "OVERSTATED_MATURITY"
    UNSUPPORTED_IMPACT = "UNSUPPORTED_IMPACT"
    UNSUPPORTED_CAUSALITY = "UNSUPPORTED_CAUSALITY"
    OUT_OF_SCOPE_CLAIM = "OUT_OF_SCOPE_CLAIM"
    AGENT_OUTPUT_UNRELIABLE = "AGENT_OUTPUT_UNRELIABLE"


AGENT_FINDING_TYPES = frozenset(
    {
        ResumeFactQAFindingType.UNSUPPORTED_ACTION_VERB,
        ResumeFactQAFindingType.OVERSTATED_OWNERSHIP,
        ResumeFactQAFindingType.OVERSTATED_MATURITY,
        ResumeFactQAFindingType.UNSUPPORTED_IMPACT,
        ResumeFactQAFindingType.UNSUPPORTED_CAUSALITY,
        ResumeFactQAFindingType.OUT_OF_SCOPE_CLAIM,
    }
)

ADVISORY_FINDING_TYPES = frozenset(
    {ResumeFactQAFindingType.UNKNOWN_JD_REFERENCE}
)


class ResumeFactQAAgentVerdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNCERTAIN = "UNCERTAIN"


class ResumeFactQAWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeFactQAReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeFactQAStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    BLOCKED_UNSUPPORTED_CLAIM = "BLOCKED_UNSUPPORTED_CLAIM"
    BLOCKED_BINDING_MISMATCH = "BLOCKED_BINDING_MISMATCH"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class ResumeFactQAFailureReason(str, Enum):
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
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    AGENT_OUTPUT_UNRELIABLE = "AGENT_OUTPUT_UNRELIABLE"
    QA_RESULT_PERSISTENCE_FAILED = "QA_RESULT_PERSISTENCE_FAILED"
    QA_RESULT_INTEGRITY_FAILURE = "QA_RESULT_INTEGRITY_FAILURE"


class ResumeFactQAAgentUnavailableError(RuntimeError):
    """Raised when the bounded fact-QA Agent cannot return an output."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the resume-fact-QA contract")
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
        raise ValueError("validated_at is invalid")
    return _require_aware(
        "validated_at",
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
class ResumeFactQAAgentMetadata:
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
class ResumeFactQABulletView:
    source_section_id: str
    source_block_id: str
    source_bullet_id: str | None
    change_type: TailoredBulletChangeType
    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResumeFactQAEvidenceView:
    evidence_id: str
    evidence_text: str


@dataclass(frozen=True, slots=True)
class ResumeFactQAContext:
    subject_id: str
    tailored_resume_draft_id: str
    bullets: tuple[ResumeFactQABulletView, ...]
    evidence_items: tuple[ResumeFactQAEvidenceView, ...]
    agent_policy: str
    agent_policy_version: str


@dataclass(frozen=True, slots=True)
class ResumeFactQAAgentFinding:
    source_section_id: str
    source_block_id: str
    source_bullet_id: str | None
    finding_type: ResumeFactQAFindingType
    claim_text: str
    cited_evidence_ids: tuple[str, ...]
    explanation: str

    def __post_init__(self) -> None:
        finding_type = ResumeFactQAFindingType(self.finding_type)
        object.__setattr__(self, "finding_type", finding_type)
        if finding_type not in AGENT_FINDING_TYPES:
            raise ValueError("Agent cannot report a deterministic finding type")
        _clean_text(
            "source_section_id", self.source_section_id, maximum=160
        )
        _clean_text("source_block_id", self.source_block_id, maximum=160)
        if self.source_bullet_id is not None:
            _clean_text(
                "source_bullet_id", self.source_bullet_id, maximum=160
            )
        _clean_text("claim_text", self.claim_text, maximum=MAX_QA_CLAIM_CHARS)
        _clean_text(
            "explanation",
            self.explanation,
            maximum=MAX_QA_EXPLANATION_CHARS,
        )
        if not isinstance(self.cited_evidence_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.cited_evidence_ids
        ):
            raise TypeError("cited_evidence_ids must be a tuple of identifiers")
        if not self.cited_evidence_ids:
            raise ValueError("an Agent finding must cite evidence")


@dataclass(frozen=True, slots=True)
class ResumeFactQAAgentOutput:
    verdict: ResumeFactQAAgentVerdict
    findings: tuple[ResumeFactQAAgentFinding, ...]

    def __post_init__(self) -> None:
        verdict = ResumeFactQAAgentVerdict(self.verdict)
        object.__setattr__(self, "verdict", verdict)
        if not isinstance(self.findings, tuple) or any(
            not isinstance(item, ResumeFactQAAgentFinding)
            for item in self.findings
        ):
            raise TypeError("findings must be typed Agent findings")
        if verdict is ResumeFactQAAgentVerdict.UNSUPPORTED:
            if not self.findings:
                raise ValueError("an unsupported verdict requires findings")
        elif self.findings:
            raise ValueError(
                "only an unsupported verdict may carry findings"
            )


@runtime_checkable
class ResumeFactQAAgentPort(Protocol):
    async def review(
        self,
        context: ResumeFactQAContext,
    ) -> ResumeFactQAAgentOutput:
        """Judge evidence support for supplied bullets, without tools or edits."""


@dataclass(frozen=True, slots=True)
class ResumeFactQAFinding:
    finding_id: str
    order: int
    finding_type: ResumeFactQAFindingType
    severity: ResumeFactQAFindingSeverity
    source: ResumeFactQAFindingSource
    source_section_id: str
    source_block_id: str | None
    source_bullet_id: str | None
    claim_text: str
    cited_evidence_ids: tuple[str, ...]
    explanation: str

    def __post_init__(self) -> None:
        if type(self.order) is not int or self.order < 0:
            raise ValueError("finding order must be a non-negative integer")
        finding_type = ResumeFactQAFindingType(self.finding_type)
        severity = ResumeFactQAFindingSeverity(self.severity)
        source = ResumeFactQAFindingSource(self.source)
        object.__setattr__(self, "finding_type", finding_type)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "source", source)
        if (
            source is ResumeFactQAFindingSource.AGENT
            and finding_type not in AGENT_FINDING_TYPES
        ):
            raise ValueError("Agent findings must use a semantic finding type")
        if (
            source is ResumeFactQAFindingSource.DETERMINISTIC
            and finding_type in AGENT_FINDING_TYPES
        ):
            raise ValueError(
                "deterministic findings cannot use a semantic finding type"
            )
        expected_severity = (
            ResumeFactQAFindingSeverity.ADVISORY
            if finding_type in ADVISORY_FINDING_TYPES
            else ResumeFactQAFindingSeverity.BLOCKING
        )
        if severity is not expected_severity:
            raise ValueError("finding severity does not match its type")
        _clean_text(
            "source_section_id", self.source_section_id, maximum=160
        )
        if self.source_block_id is not None:
            _clean_text(
                "source_block_id", self.source_block_id, maximum=160
            )
        if self.source_bullet_id is not None:
            _clean_text(
                "source_bullet_id", self.source_bullet_id, maximum=160
            )
        _clean_text("claim_text", self.claim_text, maximum=MAX_QA_CLAIM_CHARS)
        _clean_text(
            "explanation",
            self.explanation,
            maximum=MAX_QA_EXPLANATION_CHARS,
        )
        if not isinstance(self.cited_evidence_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.cited_evidence_ids
        ):
            raise TypeError("cited_evidence_ids must be a tuple of identifiers")
        expected_id = resume_fact_qa_finding_id(self.content_dict())
        if (
            not isinstance(self.finding_id, str)
            or _FINDING_ID_PATTERN.fullmatch(self.finding_id) is None
            or self.finding_id != expected_id
        ):
            raise ValueError("finding_id does not match its content")

    def content_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "finding_type": self.finding_type.value,
            "severity": self.severity.value,
            "source": self.source.value,
            "source_section_id": self.source_section_id,
            "source_block_id": self.source_block_id,
            "source_bullet_id": self.source_bullet_id,
            "claim_text": self.claim_text,
            "cited_evidence_ids": list(self.cited_evidence_ids),
            "explanation": self.explanation,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"finding_id": self.finding_id, **self.content_dict()}


def resume_fact_qa_finding_id(content: Mapping[str, Any]) -> str:
    return "resume-fact-qa-finding-" + _canonical_hash(content)


def _build_finding(
    *,
    order: int,
    finding_type: ResumeFactQAFindingType,
    source: ResumeFactQAFindingSource,
    source_section_id: str,
    source_block_id: str | None,
    source_bullet_id: str | None,
    claim_text: str,
    cited_evidence_ids: tuple[str, ...],
    explanation: str,
) -> ResumeFactQAFinding:
    severity = (
        ResumeFactQAFindingSeverity.ADVISORY
        if finding_type in ADVISORY_FINDING_TYPES
        else ResumeFactQAFindingSeverity.BLOCKING
    )
    content = {
        "order": order,
        "finding_type": finding_type.value,
        "severity": severity.value,
        "source": source.value,
        "source_section_id": source_section_id,
        "source_block_id": source_block_id,
        "source_bullet_id": source_bullet_id,
        "claim_text": claim_text,
        "cited_evidence_ids": list(cited_evidence_ids),
        "explanation": explanation,
    }
    return ResumeFactQAFinding(
        finding_id=resume_fact_qa_finding_id(content),
        order=order,
        finding_type=finding_type,
        severity=severity,
        source=source,
        source_section_id=source_section_id,
        source_block_id=source_block_id,
        source_bullet_id=source_bullet_id,
        claim_text=claim_text,
        cited_evidence_ids=cited_evidence_ids,
        explanation=explanation,
    )


def _qa_binding(
    *,
    draft: TailoredResumeDraft,
    projection: SourceResumeProjection,
    snapshot: CandidateEvidenceSnapshot,
    metadata: ResumeFactQAAgentMetadata,
) -> str:
    return _canonical_hash(
        {
            "application_plan_id": draft.application_plan_id,
            "evidence_snapshot_hash": snapshot.snapshot_content_hash,
            "evidence_snapshot_id": snapshot.snapshot_id,
            "job_content_hash": draft.job_content_hash,
            "job_id": draft.job_id,
            "job_revision": draft.job_revision,
            "resume_fact_qa_agent_version": metadata.agent_version,
            "resume_fact_qa_contract_version": (
                RESUME_FACT_QA_CONTRACT_VERSION
            ),
            "resume_fact_qa_model_id": metadata.model_id,
            "resume_fact_qa_policy_version": RESUME_FACT_QA_POLICY_VERSION,
            "resume_fact_qa_prompt_version": metadata.prompt_version,
            "resume_selection_decision_id": (
                draft.resume_selection_decision_id
            ),
            "source_projection_hash": projection.projection_content_hash,
            "source_projection_id": projection.projection_id,
            "subject_id": draft.subject_id,
            "tailored_resume_draft_hash": draft.draft_content_hash,
            "tailored_resume_draft_id": draft.draft_id,
        }
    )


@dataclass(frozen=True, slots=True)
class ResumeFactQAResult:
    qa_result_id: str
    contract_version: str
    qa_binding: str
    subject_id: str
    tailored_resume_draft_id: str
    tailored_resume_draft_hash: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    resume_selection_decision_id: str
    source_projection_id: str
    source_projection_hash: str
    evidence_snapshot_id: str
    evidence_snapshot_hash: str
    agent_policy_version: str
    agent_invoked: bool
    agent_version: str | None
    prompt_version: str | None
    model_id: str | None
    verdict: ResumeFactQAVerdict
    findings: tuple[ResumeFactQAFinding, ...]
    qa_content_hash: str
    validated_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != RESUME_FACT_QA_CONTRACT_VERSION:
            raise ValueError("ResumeFactQAResult contract is unsupported")
        binding = _require_hash("qa_binding", self.qa_binding)
        if (
            not isinstance(self.qa_result_id, str)
            or _RESULT_ID_PATTERN.fullmatch(self.qa_result_id) is None
            or self.qa_result_id != f"resume-fact-qa-{binding}"
        ):
            raise ValueError("qa_result_id does not match its binding")
        _clean_text("subject_id", self.subject_id, maximum=160)
        _clean_text(
            "tailored_resume_draft_id",
            self.tailored_resume_draft_id,
            maximum=160,
        )
        _require_hash(
            "tailored_resume_draft_hash", self.tailored_resume_draft_hash
        )
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
        policy_version = _clean_text(
            "agent_policy_version", self.agent_policy_version, maximum=80
        )
        if policy_version != RESUME_FACT_QA_POLICY_VERSION:
            raise ValueError("fact-QA policy version is unsupported")
        if type(self.agent_invoked) is not bool:
            raise TypeError("agent_invoked must be a boolean")
        agent_fields = (
            self.agent_version,
            self.prompt_version,
            self.model_id,
        )
        if self.agent_invoked:
            if any(item is None for item in agent_fields):
                raise ValueError(
                    "an invoked Agent must record its full version metadata"
                )
            _clean_text("agent_version", self.agent_version, maximum=80)
            _clean_text("prompt_version", self.prompt_version, maximum=80)
            _clean_text("model_id", self.model_id, maximum=160)
        elif any(item is not None for item in agent_fields):
            raise ValueError(
                "Agent version metadata requires an actual Agent call"
            )
        verdict = ResumeFactQAVerdict(self.verdict)
        object.__setattr__(self, "verdict", verdict)
        if not isinstance(self.findings, tuple) or any(
            not isinstance(item, ResumeFactQAFinding)
            for item in self.findings
        ):
            raise TypeError("findings must be a typed tuple")
        if tuple(item.order for item in self.findings) != tuple(
            range(len(self.findings))
        ):
            raise ValueError("findings must have contiguous order")
        finding_ids = [item.finding_id for item in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding identities must be unique")
        blocking = tuple(
            item
            for item in self.findings
            if item.severity is ResumeFactQAFindingSeverity.BLOCKING
        )
        if verdict is ResumeFactQAVerdict.PASSED and blocking:
            raise ValueError("a passed result cannot carry blocking findings")
        if verdict is ResumeFactQAVerdict.BLOCKED and not blocking:
            raise ValueError("a blocked result requires a blocking finding")
        if verdict is ResumeFactQAVerdict.DEFERRED and not any(
            item.finding_type
            is ResumeFactQAFindingType.AGENT_OUTPUT_UNRELIABLE
            for item in self.findings
        ):
            raise ValueError(
                "a deferred result must record why review was deferred"
            )
        _require_aware("validated_at", self.validated_at)
        content_hash = _require_hash("qa_content_hash", self.qa_content_hash)
        if content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("QA content hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "qa_result_id": self.qa_result_id,
            "contract_version": self.contract_version,
            "qa_binding": self.qa_binding,
            "subject_id": self.subject_id,
            "tailored_resume_draft_id": self.tailored_resume_draft_id,
            "tailored_resume_draft_hash": self.tailored_resume_draft_hash,
            "application_plan_id": self.application_plan_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "resume_selection_decision_id": (
                self.resume_selection_decision_id
            ),
            "source_projection_id": self.source_projection_id,
            "source_projection_hash": self.source_projection_hash,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "evidence_snapshot_hash": self.evidence_snapshot_hash,
            "agent_policy_version": self.agent_policy_version,
            "agent_invoked": self.agent_invoked,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "verdict": self.verdict.value,
            "findings": [item.to_dict() for item in self.findings],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "qa_content_hash": self.qa_content_hash,
            "validated_at": _rfc3339(self.validated_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeFactQAWriteResult:
    status: ResumeFactQAWriteStatus
    qa_result: ResumeFactQAResult | None
    reason_code: ResumeFactQAFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ResumeFactQAWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeFactQAFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ResumeFactQAWriteStatus.CREATED,
            ResumeFactQAWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.qa_result, ResumeFactQAResult)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful QA write result is invalid")
        elif self.qa_result is not None or self.reason_code is None:
            raise ValueError("failed QA write result is invalid")


@dataclass(frozen=True, slots=True)
class ResumeFactQAReadResult:
    status: ResumeFactQAReadStatus
    qa_result: ResumeFactQAResult | None
    reason_code: ResumeFactQAFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeFactQAReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeFactQAFailureReason(self.reason_code),
            )
        if status is ResumeFactQAReadStatus.FOUND:
            if (
                not isinstance(self.qa_result, ResumeFactQAResult)
                or self.reason_code is not None
            ):
                raise ValueError("found QA read result is invalid")
        elif status is ResumeFactQAReadStatus.NOT_FOUND:
            if self.qa_result is not None or self.reason_code is not None:
                raise ValueError("not-found QA read result is invalid")
        elif (
            self.qa_result is not None
            or self.reason_code
            is not ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure QA read result is invalid")


@runtime_checkable
class ResumeFactQARepository(Protocol):
    def save(self, qa_result: ResumeFactQAResult) -> ResumeFactQAWriteResult:
        """Persist one immutable fact-QA result."""

    def get(
        self, *, subject_id: str, qa_result_id: str
    ) -> ResumeFactQAReadResult:
        """Read one subject-owned fact-QA result."""


def _finding_from_dict(value: Any) -> ResumeFactQAFinding:
    expected = {
        "finding_id",
        "order",
        "finding_type",
        "severity",
        "source",
        "source_section_id",
        "source_block_id",
        "source_bullet_id",
        "claim_text",
        "cited_evidence_ids",
        "explanation",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["cited_evidence_ids"], list)
    ):
        raise ValueError("persisted ResumeFactQAFinding is invalid")
    return ResumeFactQAFinding(
        finding_id=value["finding_id"],
        order=value["order"],
        finding_type=ResumeFactQAFindingType(value["finding_type"]),
        severity=ResumeFactQAFindingSeverity(value["severity"]),
        source=ResumeFactQAFindingSource(value["source"]),
        source_section_id=value["source_section_id"],
        source_block_id=value["source_block_id"],
        source_bullet_id=value["source_bullet_id"],
        claim_text=value["claim_text"],
        cited_evidence_ids=tuple(value["cited_evidence_ids"]),
        explanation=value["explanation"],
    )


def _qa_result_from_dict(value: Any) -> ResumeFactQAResult:
    expected = {
        "qa_result_id",
        "contract_version",
        "qa_binding",
        "subject_id",
        "tailored_resume_draft_id",
        "tailored_resume_draft_hash",
        "application_plan_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "resume_selection_decision_id",
        "source_projection_id",
        "source_projection_hash",
        "evidence_snapshot_id",
        "evidence_snapshot_hash",
        "agent_policy_version",
        "agent_invoked",
        "agent_version",
        "prompt_version",
        "model_id",
        "verdict",
        "findings",
        "qa_content_hash",
        "validated_at",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["findings"], list)
    ):
        raise ValueError("persisted ResumeFactQAResult is invalid")
    return ResumeFactQAResult(
        qa_result_id=value["qa_result_id"],
        contract_version=value["contract_version"],
        qa_binding=value["qa_binding"],
        subject_id=value["subject_id"],
        tailored_resume_draft_id=value["tailored_resume_draft_id"],
        tailored_resume_draft_hash=value["tailored_resume_draft_hash"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        resume_selection_decision_id=value[
            "resume_selection_decision_id"
        ],
        source_projection_id=value["source_projection_id"],
        source_projection_hash=value["source_projection_hash"],
        evidence_snapshot_id=value["evidence_snapshot_id"],
        evidence_snapshot_hash=value["evidence_snapshot_hash"],
        agent_policy_version=value["agent_policy_version"],
        agent_invoked=value["agent_invoked"],
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        verdict=ResumeFactQAVerdict(value["verdict"]),
        findings=tuple(
            _finding_from_dict(item) for item in value["findings"]
        ),
        qa_content_hash=value["qa_content_hash"],
        validated_at=_parse_timestamp(value["validated_at"]),
    )


class PrivateHomeResumeFactQARepository:
    """Immutable, subject-scoped fact-QA results in Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, qa_result_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(qa_result_id, str)
            or _RESULT_ID_PATTERN.fullmatch(qa_result_id) is None
        ):
            raise ValueError("qa_result_id is invalid")
        return (
            self._home.paths.resume_fact_qa_results
            / _subject_storage_key(subject)
            / f"{qa_result_id}.json"
        )

    def get(
        self, *, subject_id: str, qa_result_id: str
    ) -> ResumeFactQAReadResult:
        path = self._path(subject_id, qa_result_id)
        with self._lock:
            if not path.exists():
                return ResumeFactQAReadResult(
                    status=ResumeFactQAReadStatus.NOT_FOUND,
                    qa_result=None,
                )
            if path.is_symlink() or not path.is_file():
                return ResumeFactQAReadResult(
                    status=ResumeFactQAReadStatus.INTEGRITY_FAILURE,
                    qa_result=None,
                    reason_code=(
                        ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE
                    ),
                )
            try:
                qa_result = _qa_result_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ResumeFactQAReadResult(
                    status=ResumeFactQAReadStatus.INTEGRITY_FAILURE,
                    qa_result=None,
                    reason_code=(
                        ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE
                    ),
                )
            if (
                qa_result.subject_id != subject_id.strip()
                or qa_result.qa_result_id != qa_result_id
                or path.name != f"{qa_result.qa_result_id}.json"
            ):
                return ResumeFactQAReadResult(
                    status=ResumeFactQAReadStatus.INTEGRITY_FAILURE,
                    qa_result=None,
                    reason_code=(
                        ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE
                    ),
                )
            return ResumeFactQAReadResult(
                status=ResumeFactQAReadStatus.FOUND,
                qa_result=qa_result,
            )

    def save(self, qa_result: ResumeFactQAResult) -> ResumeFactQAWriteResult:
        if not isinstance(qa_result, ResumeFactQAResult):
            raise TypeError("qa_result must be a ResumeFactQAResult")
        path = self._path(qa_result.subject_id, qa_result.qa_result_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            qa_result.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ResumeFactQAWriteResult(
                    status=ResumeFactQAWriteStatus.FAILED,
                    qa_result=None,
                    reason_code=(
                        ResumeFactQAFailureReason.QA_RESULT_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return ResumeFactQAWriteResult(
                    status=ResumeFactQAWriteStatus.CREATED,
                    qa_result=qa_result,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=qa_result.subject_id,
                qa_result_id=qa_result.qa_result_id,
            )
            if (
                existing.status is ResumeFactQAReadStatus.FOUND
                and existing.qa_result is not None
                and existing.qa_result.content_dict()
                == qa_result.content_dict()
            ):
                return ResumeFactQAWriteResult(
                    status=ResumeFactQAWriteStatus.UNCHANGED,
                    qa_result=existing.qa_result,
                    reason_code=None,
                    retryable=False,
                )
            return ResumeFactQAWriteResult(
                status=ResumeFactQAWriteStatus.FAILED,
                qa_result=None,
                reason_code=(
                    ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class RunResumeFactQACommand:
    subject_id: str
    tailored_resume_draft_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class RunResumeFactQAResult:
    status: ResumeFactQAStatus
    subject_id: str
    tailored_resume_draft_id: str
    qa_binding: str
    qa_result: ResumeFactQAResult | None
    write_result: ResumeFactQAWriteResult | None
    reason_code: ResumeFactQAFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = ResumeFactQAStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeFactQAFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            ResumeFactQAStatus.CREATED,
            ResumeFactQAStatus.UNCHANGED,
            ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM,
            ResumeFactQAStatus.DEFERRED_NEEDS_HUMAN,
        }:
            if (
                not isinstance(self.qa_result, ResumeFactQAResult)
                or not isinstance(
                    self.write_result, ResumeFactQAWriteResult
                )
                or self.write_result.qa_result != self.qa_result
                or self.write_result.status
                is ResumeFactQAWriteStatus.FAILED
                or self.retryable
            ):
                raise ValueError("persisted QA operation result is invalid")
            expected_verdict = {
                ResumeFactQAStatus.CREATED: ResumeFactQAVerdict.PASSED,
                ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM: (
                    ResumeFactQAVerdict.BLOCKED
                ),
                ResumeFactQAStatus.DEFERRED_NEEDS_HUMAN: (
                    ResumeFactQAVerdict.DEFERRED
                ),
            }.get(status)
            if (
                expected_verdict is not None
                and self.qa_result.verdict is not expected_verdict
            ):
                raise ValueError("QA verdict does not match the status")
            if status is ResumeFactQAStatus.UNCHANGED and (
                self.write_result.status
                is not ResumeFactQAWriteStatus.UNCHANGED
            ):
                raise ValueError("unchanged QA result requires a replay write")
            if status is ResumeFactQAStatus.DEFERRED_NEEDS_HUMAN and (
                self.reason_code
                is not ResumeFactQAFailureReason.AGENT_OUTPUT_UNRELIABLE
            ):
                raise ValueError("deferred QA result must record its reason")
            if (
                status
                is ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
                and self.reason_code
                is not ResumeFactQAFailureReason.UNSUPPORTED_CLAIM
            ):
                raise ValueError("blocked QA result must record its reason")
            if (
                status
                in {ResumeFactQAStatus.CREATED, ResumeFactQAStatus.UNCHANGED}
                and self.reason_code is not None
            ):
                raise ValueError("successful QA result cannot carry a reason")
        elif status is ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH:
            if (
                self.qa_result is not None
                or self.write_result is not None
                or self.reason_code is None
                or self.retryable
            ):
                raise ValueError("binding-mismatch QA result is invalid")
        elif self.qa_result is not None or self.reason_code is None:
            raise ValueError("failed QA result is invalid")


def _failure(
    command: RunResumeFactQACommand,
    reason: ResumeFactQAFailureReason,
    *,
    status: ResumeFactQAStatus = ResumeFactQAStatus.FAILED,
    retryable: bool = False,
    qa_binding: str = "",
) -> RunResumeFactQAResult:
    return RunResumeFactQAResult(
        status=status,
        subject_id=(
            command.subject_id
            if isinstance(command.subject_id, str)
            else ""
        ),
        tailored_resume_draft_id=(
            command.tailored_resume_draft_id
            if isinstance(command.tailored_resume_draft_id, str)
            else ""
        ),
        qa_binding=qa_binding,
        qa_result=None,
        write_result=None,
        reason_code=reason,
        retryable=retryable,
        message=(
            f"Resume fact QA stopped: {reason.value}."
            if status is ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH
            else f"Resume fact QA failed: {reason.value}."
        ),
    )


def _words(text: str) -> tuple[str, ...]:
    return tuple(_WORD_PATTERN.findall(text))


def _casefold_word_set(text: str) -> frozenset[str]:
    return frozenset(word.casefold() for word in _words(text))


def _factual_tokens(text: str) -> tuple[str, ...]:
    """Tokens asserting a checkable fact: numbers, and proper-noun-like words.

    The leading token is exempt from the capitalization test because a bullet
    opens with a capitalized action verb; verbs are judged by the QA Agent.
    """
    checked: list[str] = []
    for index, token in enumerate(_words(text)):
        if _NUMBER_PATTERN.search(token):
            checked.append(token)
        elif index > 0 and any(character.isupper() for character in token):
            checked.append(token)
    return tuple(checked)


def _deterministic_findings(
    *,
    draft: TailoredResumeDraft,
    projection: SourceResumeProjection,
    snapshot: CandidateEvidenceSnapshot,
    job: JobPosting,
) -> tuple[ResumeFactQAFinding, ...]:
    """Re-derive every checkable fact independently of the tailoring validator."""

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
    findings: list[tuple[Any, ...]] = []
    seen_blocks: set[str] = set()

    for section in draft.sections:
        known_section = section.source_section_id in source_sections
        if not known_section:
            findings.append(
                (
                    ResumeFactQAFindingType.UNKNOWN_SOURCE_REFERENCE,
                    section.source_section_id,
                    None,
                    None,
                    section.title or section.source_section_id,
                    (),
                    "The draft section is not present in the bound source projection.",
                )
            )
        for bullet in section.bullets:
            claim = bullet.text or ""
            located = source_blocks.get(bullet.source_block_id)
            if located is None:
                findings.append(
                    (
                        ResumeFactQAFindingType.UNKNOWN_SOURCE_REFERENCE,
                        bullet.source_section_id,
                        bullet.source_block_id,
                        bullet.source_bullet_id,
                        claim or bullet.source_block_id,
                        tuple(bullet.evidence_ids),
                        "The draft bullet references a source block that does not exist.",
                    )
                )
                continue
            block_section, block = located
            if bullet.source_block_id in seen_blocks:
                findings.append(
                    (
                        ResumeFactQAFindingType.DUPLICATE_SOURCE_REFERENCE,
                        bullet.source_section_id,
                        bullet.source_block_id,
                        bullet.source_bullet_id,
                        claim or block.text,
                        tuple(bullet.evidence_ids),
                        "The draft uses one source block more than once.",
                    )
                )
                continue
            seen_blocks.add(bullet.source_block_id)
            if (
                bullet.source_section_id != block_section.section_id
                or bullet.source_bullet_id != block.bullet_id
            ):
                findings.append(
                    (
                        ResumeFactQAFindingType.UNKNOWN_SOURCE_REFERENCE,
                        bullet.source_section_id,
                        bullet.source_block_id,
                        bullet.source_bullet_id,
                        claim or block.text,
                        tuple(bullet.evidence_ids),
                        "The draft bullet source binding does not match the projection.",
                    )
                )
                continue

            usable_evidence: list[str] = []
            for evidence_id in bullet.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    findings.append(
                        (
                            ResumeFactQAFindingType.UNKNOWN_EVIDENCE_REFERENCE,
                            bullet.source_section_id,
                            bullet.source_block_id,
                            bullet.source_bullet_id,
                            claim or block.text,
                            (evidence_id,),
                            "The cited evidence is not in the bound snapshot.",
                        )
                    )
                elif (
                    CandidateEvidenceScope.RESUME_TAILORING
                    not in evidence.allowed_scopes
                ):
                    findings.append(
                        (
                            ResumeFactQAFindingType.EVIDENCE_SCOPE_NOT_PERMITTED,
                            bullet.source_section_id,
                            bullet.source_block_id,
                            bullet.source_bullet_id,
                            claim or block.text,
                            (evidence_id,),
                            "The cited evidence is not authorized for resume tailoring.",
                        )
                    )
                else:
                    usable_evidence.append(evidence_id)
            for reference in bullet.jd_alignment:
                if reference not in job.description:
                    findings.append(
                        (
                            ResumeFactQAFindingType.UNKNOWN_JD_REFERENCE,
                            bullet.source_section_id,
                            bullet.source_block_id,
                            bullet.source_bullet_id,
                            reference,
                            tuple(bullet.evidence_ids),
                            "The JD alignment reference is not in the bound job description.",
                        )
                    )

            if bullet.change_type is TailoredBulletChangeType.OMITTED:
                continue
            if bullet.change_type in {
                TailoredBulletChangeType.UNCHANGED,
                TailoredBulletChangeType.REORDERED,
            }:
                if bullet.text != block.text:
                    findings.append(
                        (
                            ResumeFactQAFindingType.SOURCE_TEXT_ALTERED,
                            bullet.source_section_id,
                            bullet.source_block_id,
                            bullet.source_bullet_id,
                            claim,
                            tuple(bullet.evidence_ids),
                            "The bullet is marked unrewritten but differs from the source text.",
                        )
                    )
                continue

            if not usable_evidence:
                findings.append(
                    (
                        ResumeFactQAFindingType.MISSING_EVIDENCE_REFERENCE,
                        bullet.source_section_id,
                        bullet.source_block_id,
                        bullet.source_bullet_id,
                        claim,
                        tuple(bullet.evidence_ids),
                        "The rewritten bullet cites no usable CandidateEvidence.",
                    )
                )
                continue
            supported = _casefold_word_set(block.text)
            for evidence_id in usable_evidence:
                supported |= _casefold_word_set(
                    evidence_by_id[evidence_id].evidence_text
                )
            unsupported = tuple(
                token
                for token in _factual_tokens(claim)
                if token.casefold() not in supported
            )
            if unsupported:
                findings.append(
                    (
                        ResumeFactQAFindingType.UNSUPPORTED_FACT_TOKEN,
                        bullet.source_section_id,
                        bullet.source_block_id,
                        bullet.source_bullet_id,
                        claim,
                        tuple(bullet.evidence_ids),
                        (
                            "The rewritten bullet asserts "
                            f"{', '.join(unsupported)} without supporting evidence."
                        ),
                    )
                )

    for block_id, (section, block) in source_blocks.items():
        if block_id not in seen_blocks:
            findings.append(
                (
                    ResumeFactQAFindingType.MISSING_SOURCE_COVERAGE,
                    section.section_id,
                    block_id,
                    block.bullet_id,
                    block.text,
                    (),
                    "The draft does not account for this source block.",
                )
            )

    return tuple(
        _build_finding(
            order=order,
            finding_type=item[0],
            source=ResumeFactQAFindingSource.DETERMINISTIC,
            source_section_id=item[1],
            source_block_id=item[2],
            source_bullet_id=item[3],
            claim_text=item[4],
            cited_evidence_ids=item[5],
            explanation=item[6],
        )
        for order, item in enumerate(findings)
    )


def _agent_findings(
    *,
    output: ResumeFactQAAgentOutput,
    reviewed: tuple[ResumeFactQABulletView, ...],
    snapshot: CandidateEvidenceSnapshot,
    start_order: int,
) -> tuple[ResumeFactQAFinding, ...]:
    """Accept Agent findings only when every reference is independently valid."""

    reviewed_by_block = {item.source_block_id: item for item in reviewed}
    evidence_by_id = {
        item.evidence_id: item for item in snapshot.evidence_items
    }
    accepted: list[ResumeFactQAFinding] = []
    for offset, finding in enumerate(output.findings):
        bullet = reviewed_by_block.get(finding.source_block_id)
        if (
            bullet is None
            or finding.source_section_id != bullet.source_section_id
            or finding.source_bullet_id != bullet.source_bullet_id
        ):
            raise _AgentOutputRejected(
                "an Agent finding references an unknown draft bullet"
            )
        for evidence_id in finding.cited_evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                raise _AgentOutputRejected(
                    "an Agent finding references unknown evidence"
                )
            if (
                CandidateEvidenceScope.RESUME_TAILORING
                not in evidence.allowed_scopes
            ):
                raise _AgentOutputRejected(
                    "an Agent finding cites evidence outside the tailoring scope"
                )
        accepted.append(
            _build_finding(
                order=start_order + offset,
                finding_type=finding.finding_type,
                source=ResumeFactQAFindingSource.AGENT,
                source_section_id=finding.source_section_id,
                source_block_id=finding.source_block_id,
                source_bullet_id=finding.source_bullet_id,
                claim_text=finding.claim_text,
                cited_evidence_ids=finding.cited_evidence_ids,
                explanation=finding.explanation,
            )
        )
    return tuple(accepted)


class _AgentOutputRejected(ValueError):
    """The QA Agent output failed deterministic reference validation."""


def _build_qa_result(
    *,
    draft: TailoredResumeDraft,
    projection: SourceResumeProjection,
    snapshot: CandidateEvidenceSnapshot,
    binding: str,
    verdict: ResumeFactQAVerdict,
    findings: tuple[ResumeFactQAFinding, ...],
    metadata: ResumeFactQAAgentMetadata,
    agent_invoked: bool,
    now: datetime,
) -> ResumeFactQAResult:
    qa_result_id = f"resume-fact-qa-{binding}"
    content = {
        "qa_result_id": qa_result_id,
        "contract_version": RESUME_FACT_QA_CONTRACT_VERSION,
        "qa_binding": binding,
        "subject_id": draft.subject_id,
        "tailored_resume_draft_id": draft.draft_id,
        "tailored_resume_draft_hash": draft.draft_content_hash,
        "application_plan_id": draft.application_plan_id,
        "job_id": draft.job_id,
        "job_revision": draft.job_revision,
        "job_content_hash": draft.job_content_hash,
        "resume_selection_decision_id": draft.resume_selection_decision_id,
        "source_projection_id": projection.projection_id,
        "source_projection_hash": projection.projection_content_hash,
        "evidence_snapshot_id": snapshot.snapshot_id,
        "evidence_snapshot_hash": snapshot.snapshot_content_hash,
        "agent_policy_version": RESUME_FACT_QA_POLICY_VERSION,
        "agent_invoked": agent_invoked,
        "agent_version": metadata.agent_version if agent_invoked else None,
        "prompt_version": metadata.prompt_version if agent_invoked else None,
        "model_id": metadata.model_id if agent_invoked else None,
        "verdict": verdict.value,
        "findings": [item.to_dict() for item in findings],
    }
    return ResumeFactQAResult(
        qa_result_id=qa_result_id,
        contract_version=RESUME_FACT_QA_CONTRACT_VERSION,
        qa_binding=binding,
        subject_id=draft.subject_id,
        tailored_resume_draft_id=draft.draft_id,
        tailored_resume_draft_hash=draft.draft_content_hash,
        application_plan_id=draft.application_plan_id,
        job_id=draft.job_id,
        job_revision=draft.job_revision,
        job_content_hash=draft.job_content_hash,
        resume_selection_decision_id=draft.resume_selection_decision_id,
        source_projection_id=projection.projection_id,
        source_projection_hash=projection.projection_content_hash,
        evidence_snapshot_id=snapshot.snapshot_id,
        evidence_snapshot_hash=snapshot.snapshot_content_hash,
        agent_policy_version=RESUME_FACT_QA_POLICY_VERSION,
        agent_invoked=agent_invoked,
        agent_version=metadata.agent_version if agent_invoked else None,
        prompt_version=metadata.prompt_version if agent_invoked else None,
        model_id=metadata.model_id if agent_invoked else None,
        verdict=verdict,
        findings=findings,
        qa_content_hash=_canonical_hash(content),
        validated_at=now,
    )


def _persisted(
    command: RunResumeFactQACommand,
    *,
    binding: str,
    qa_result: ResumeFactQAResult,
    repository: ResumeFactQARepository,
    status: ResumeFactQAStatus,
    reason_code: ResumeFactQAFailureReason | None,
    message: str,
) -> RunResumeFactQAResult:
    try:
        write_result = repository.save(qa_result)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeFactQAFailureReason.QA_RESULT_PERSISTENCE_FAILED,
            retryable=True,
            qa_binding=binding,
        )
    if write_result.status is ResumeFactQAWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or ResumeFactQAFailureReason.QA_RESULT_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            qa_binding=binding,
        )
    return RunResumeFactQAResult(
        status=status,
        subject_id=qa_result.subject_id,
        tailored_resume_draft_id=qa_result.tailored_resume_draft_id,
        qa_binding=binding,
        qa_result=write_result.qa_result,
        write_result=write_result,
        reason_code=reason_code,
        retryable=False,
        message=message,
    )


async def run_resume_fact_qa(
    command: RunResumeFactQACommand,
    *,
    draft_repository: TailoredResumeDraftRepository,
    application_plan_repository: ApplicationPlanRepository,
    job_repository: JobPostingReadRepository,
    selection_repository: ResumeSelectionDecisionRepository,
    projection_repository: SourceResumeProjectionRepository,
    evidence_snapshot_repository: CandidateEvidenceSnapshotRepository,
    agent: ResumeFactQAAgentPort,
    metadata: ResumeFactQAAgentMetadata,
    qa_repository: ResumeFactQARepository,
) -> RunResumeFactQAResult:
    """Judge one draft against its evidence, with at most one Agent call."""

    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        draft_id = _clean_text(
            "tailored_resume_draft_id",
            command.tailored_resume_draft_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
        if not isinstance(metadata, ResumeFactQAAgentMetadata):
            raise TypeError("metadata must be ResumeFactQAAgentMetadata")
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, ResumeFactQAFailureReason.INVALID_REQUEST
        )

    try:
        draft_result = draft_repository.get(
            subject_id=subject_id, draft_id=draft_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command, ResumeFactQAFailureReason.DRAFT_INTEGRITY_FAILURE
        )
    if draft_result.status is TailoredResumeDraftReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeFactQAFailureReason.DRAFT_NOT_FOUND
        )
    if (
        draft_result.status is not TailoredResumeDraftReadStatus.FOUND
        or not isinstance(draft_result.draft, TailoredResumeDraft)
    ):
        return _failure(
            command, ResumeFactQAFailureReason.DRAFT_INTEGRITY_FAILURE
        )
    draft = draft_result.draft
    if draft.subject_id != subject_id:
        return _failure(
            command,
            ResumeFactQAFailureReason.DRAFT_SUBJECT_MISMATCH,
            status=ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    try:
        plan_result = application_plan_repository.get(
            draft.application_plan_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeFactQAFailureReason.APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_result.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeFactQAFailureReason.APPLICATION_PLAN_NOT_FOUND
        )
    if (
        plan_result.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_result.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            ResumeFactQAFailureReason.APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_result.plan
    if (
        plan.subject_id != subject_id
        or plan.plan_id != draft.application_plan_id
        or plan.job_id != draft.job_id
        or plan.job_revision != draft.job_revision
        or plan.job_content_hash != draft.job_content_hash
        or plan.user_preparation_instructions_hash
        != draft.user_preparation_instructions_hash
    ):
        return _failure(
            command,
            ResumeFactQAFailureReason.APPLICATION_PLAN_BINDING_MISMATCH,
            status=ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    try:
        job = job_repository.get(draft.job_id)
    except (
        JobPostingRepositoryError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return _failure(command, ResumeFactQAFailureReason.JOB_READ_FAILED)
    if job is None:
        return _failure(command, ResumeFactQAFailureReason.JOB_NOT_FOUND)
    if (
        not isinstance(job, JobPosting)
        or job.job_id != draft.job_id
        or job.revision != draft.job_revision
        or job.content_hash != draft.job_content_hash
    ):
        return _failure(
            command,
            ResumeFactQAFailureReason.JOB_BINDING_MISMATCH,
            status=ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    try:
        selection_result = selection_repository.get(
            subject_id=subject_id,
            decision_id=draft.resume_selection_decision_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeFactQAFailureReason.RESUME_SELECTION_INTEGRITY_FAILURE,
        )
    if selection_result.status is ResumeSelectionDecisionReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeFactQAFailureReason.RESUME_SELECTION_NOT_FOUND
        )
    if (
        selection_result.status is not ResumeSelectionDecisionReadStatus.FOUND
        or not isinstance(
            selection_result.decision, ResumeSelectionDecision
        )
    ):
        return _failure(
            command,
            ResumeFactQAFailureReason.RESUME_SELECTION_INTEGRITY_FAILURE,
        )
    selection = selection_result.decision
    if (
        selection.subject_id != subject_id
        or selection.decision_id != draft.resume_selection_decision_id
        or selection.application_plan_id != draft.application_plan_id
        or selection.job_id != draft.job_id
        or selection.job_revision != draft.job_revision
        or selection.job_content_hash != draft.job_content_hash
        or selection.source_resume_id != draft.source_resume_id
        or selection.source_artifact_sha256
        != draft.source_artifact_sha256
    ):
        return _failure(
            command,
            ResumeFactQAFailureReason.RESUME_SELECTION_BINDING_MISMATCH,
            status=ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    try:
        projection_result = projection_repository.get(
            subject_id=subject_id,
            projection_id=draft.source_projection_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeFactQAFailureReason.SOURCE_PROJECTION_INTEGRITY_FAILURE,
        )
    if projection_result.status is SourceResumeProjectionReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeFactQAFailureReason.SOURCE_PROJECTION_NOT_FOUND
        )
    if (
        projection_result.status is not SourceResumeProjectionReadStatus.FOUND
        or not isinstance(
            projection_result.projection, SourceResumeProjection
        )
    ):
        return _failure(
            command,
            ResumeFactQAFailureReason.SOURCE_PROJECTION_INTEGRITY_FAILURE,
        )
    projection = projection_result.projection
    if (
        projection.subject_id != subject_id
        or projection.projection_id != draft.source_projection_id
        or projection.projection_content_hash
        != draft.source_projection_hash
        or projection.resume_id != draft.source_resume_id
        or projection.artifact_sha256 != draft.source_artifact_sha256
    ):
        return _failure(
            command,
            ResumeFactQAFailureReason.SOURCE_PROJECTION_BINDING_MISMATCH,
            status=ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    try:
        snapshot_result = evidence_snapshot_repository.get(
            subject_id=subject_id,
            snapshot_id=draft.evidence_snapshot_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeFactQAFailureReason.EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE,
        )
    if snapshot_result.status is CandidateEvidenceSnapshotReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeFactQAFailureReason.EVIDENCE_SNAPSHOT_NOT_FOUND
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
            ResumeFactQAFailureReason.EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE,
        )
    snapshot = snapshot_result.snapshot
    if (
        snapshot.subject_id != subject_id
        or snapshot.snapshot_id != draft.evidence_snapshot_id
        or snapshot.snapshot_content_hash != draft.evidence_snapshot_hash
        or snapshot.application_plan_id != draft.application_plan_id
        or snapshot.job_id != draft.job_id
        or snapshot.resume_selection_decision_id
        != draft.resume_selection_decision_id
        or snapshot.source_projection_id != draft.source_projection_id
        or snapshot.source_projection_hash != draft.source_projection_hash
    ):
        return _failure(
            command,
            ResumeFactQAFailureReason.EVIDENCE_SNAPSHOT_BINDING_MISMATCH,
            status=ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    binding = _qa_binding(
        draft=draft,
        projection=projection,
        snapshot=snapshot,
        metadata=metadata,
    )
    qa_result_id = f"resume-fact-qa-{binding}"
    try:
        existing = qa_repository.get(
            subject_id=subject_id, qa_result_id=qa_result_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE,
            qa_binding=binding,
        )
    if existing.status is ResumeFactQAReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE,
            qa_binding=binding,
        )
    if (
        existing.status is ResumeFactQAReadStatus.FOUND
        and existing.qa_result is not None
    ):
        return RunResumeFactQAResult(
            status=ResumeFactQAStatus.UNCHANGED,
            subject_id=subject_id,
            tailored_resume_draft_id=draft_id,
            qa_binding=binding,
            qa_result=existing.qa_result,
            write_result=ResumeFactQAWriteResult(
                status=ResumeFactQAWriteStatus.UNCHANGED,
                qa_result=existing.qa_result,
                reason_code=None,
                retryable=False,
            ),
            reason_code=None,
            retryable=False,
            message=(
                "The existing fact-QA result is unchanged with verdict "
                f"{existing.qa_result.verdict.value}."
            ),
        )

    deterministic = _deterministic_findings(
        draft=draft,
        projection=projection,
        snapshot=snapshot,
        job=job,
    )
    if any(
        item.severity is ResumeFactQAFindingSeverity.BLOCKING
        for item in deterministic
    ):
        return _persisted(
            command,
            binding=binding,
            qa_result=_build_qa_result(
                draft=draft,
                projection=projection,
                snapshot=snapshot,
                binding=binding,
                verdict=ResumeFactQAVerdict.BLOCKED,
                findings=deterministic,
                metadata=metadata,
                agent_invoked=False,
                now=now,
            ),
            repository=qa_repository,
            status=ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM,
            reason_code=ResumeFactQAFailureReason.UNSUPPORTED_CLAIM,
            message=(
                "Deterministic fact QA blocked the draft without an Agent call."
            ),
        )

    reviewed = tuple(
        ResumeFactQABulletView(
            source_section_id=bullet.source_section_id,
            source_block_id=bullet.source_block_id,
            source_bullet_id=bullet.source_bullet_id,
            change_type=bullet.change_type,
            text=bullet.text or "",
            evidence_ids=bullet.evidence_ids,
        )
        for section in draft.sections
        for bullet in section.bullets
        if bullet.change_type is TailoredBulletChangeType.REWRITTEN
    )
    if not reviewed:
        return _persisted(
            command,
            binding=binding,
            qa_result=_build_qa_result(
                draft=draft,
                projection=projection,
                snapshot=snapshot,
                binding=binding,
                verdict=ResumeFactQAVerdict.PASSED,
                findings=deterministic,
                metadata=metadata,
                agent_invoked=False,
                now=now,
            ),
            repository=qa_repository,
            status=ResumeFactQAStatus.CREATED,
            reason_code=None,
            message=(
                "The draft makes no rewritten claim, so fact QA passed "
                "without an Agent call."
            ),
        )

    context = ResumeFactQAContext(
        subject_id=subject_id,
        tailored_resume_draft_id=draft.draft_id,
        bullets=reviewed,
        evidence_items=tuple(
            ResumeFactQAEvidenceView(
                evidence_id=item.evidence_id,
                evidence_text=item.evidence_text,
            )
            for item in snapshot.evidence_items
            if CandidateEvidenceScope.RESUME_TAILORING in item.allowed_scopes
        ),
        agent_policy=RESUME_FACT_QA_AGENT_POLICY,
        agent_policy_version=RESUME_FACT_QA_POLICY_VERSION,
    )
    try:
        output = await agent.review(context)
    except TimeoutError:
        return _failure(
            command,
            ResumeFactQAFailureReason.AGENT_TIMEOUT,
            retryable=True,
            qa_binding=binding,
        )
    except ResumeFactQAAgentUnavailableError:
        return _failure(
            command,
            ResumeFactQAFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            qa_binding=binding,
        )
    except Exception:
        return _failure(
            command,
            ResumeFactQAFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            qa_binding=binding,
        )

    deferred_detail: str | None = None
    semantic: tuple[ResumeFactQAFinding, ...] = ()
    if not isinstance(output, ResumeFactQAAgentOutput):
        deferred_detail = "the Agent did not return a typed structured result"
    elif output.verdict is ResumeFactQAAgentVerdict.UNCERTAIN:
        deferred_detail = "the Agent could not reach a reliable verdict"
    else:
        try:
            semantic = _agent_findings(
                output=output,
                reviewed=reviewed,
                snapshot=snapshot,
                start_order=len(deterministic),
            )
        except _AgentOutputRejected as rejection:
            deferred_detail = str(rejection)
        except (AttributeError, TypeError, ValueError):
            deferred_detail = "the Agent output could not be validated"

    if deferred_detail is not None:
        findings = deterministic + (
            _build_finding(
                order=len(deterministic),
                finding_type=(
                    ResumeFactQAFindingType.AGENT_OUTPUT_UNRELIABLE
                ),
                source=ResumeFactQAFindingSource.DETERMINISTIC,
                source_section_id=draft.sections[0].source_section_id,
                source_block_id=None,
                source_bullet_id=None,
                claim_text=draft.draft_id,
                cited_evidence_ids=(),
                explanation=(
                    f"Fact QA needs human review because {deferred_detail}."
                ),
            ),
        )
        return _persisted(
            command,
            binding=binding,
            qa_result=_build_qa_result(
                draft=draft,
                projection=projection,
                snapshot=snapshot,
                binding=binding,
                verdict=ResumeFactQAVerdict.DEFERRED,
                findings=findings,
                metadata=metadata,
                agent_invoked=True,
                now=now,
            ),
            repository=qa_repository,
            status=ResumeFactQAStatus.DEFERRED_NEEDS_HUMAN,
            reason_code=ResumeFactQAFailureReason.AGENT_OUTPUT_UNRELIABLE,
            message=f"Fact QA needs human review because {deferred_detail}.",
        )

    findings = deterministic + semantic
    blocked = bool(semantic)
    return _persisted(
        command,
        binding=binding,
        qa_result=_build_qa_result(
            draft=draft,
            projection=projection,
            snapshot=snapshot,
            binding=binding,
            verdict=(
                ResumeFactQAVerdict.BLOCKED
                if blocked
                else ResumeFactQAVerdict.PASSED
            ),
            findings=findings,
            metadata=metadata,
            agent_invoked=True,
            now=now,
        ),
        repository=qa_repository,
        status=(
            ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
            if blocked
            else ResumeFactQAStatus.CREATED
        ),
        reason_code=(
            ResumeFactQAFailureReason.UNSUPPORTED_CLAIM if blocked else None
        ),
        message=(
            "Fact QA blocked the draft on unsupported claims."
            if blocked
            else "Fact QA passed: every claim is supported by evidence."
        ),
    )


_RESUME_FACT_QA_FAILURE_REASON_MAP = {
    reason: ResumeFactQAStopReason[reason.name]
    for reason in ResumeFactQAFailureReason
}


def resume_fact_qa_public_result(
    result: RunResumeFactQAResult,
) -> PublicPreparationStageResult:
    """Adapt every authoritative P2a5 outcome to stage-result v2."""

    if not isinstance(result, RunResumeFactQAResult):
        raise TypeError("result must be a Resume Fact QA result")
    stage = ApplicationPreparationStage.RESUME_FACT_QA
    if result.status in {
        ResumeFactQAStatus.CREATED,
        ResumeFactQAStatus.UNCHANGED,
    }:
        if result.qa_result is None:
            raise ValueError("successful fact QA has no result")
        constructor = (
            PublicPreparationStageResult.completed
            if result.status is ResumeFactQAStatus.CREATED
            else PublicPreparationStageResult.unchanged
        )
        return constructor(
            stage=stage,
            result_id=result.qa_result.qa_result_id,
            result_content_hash=result.qa_result.qa_content_hash,
            outputs={
                "resume_fact_qa_result_id": result.qa_result.qa_result_id
            },
        )
    if result.reason_code is None:
        raise ValueError("stopped fact QA has no authoritative reason")
    try:
        reason = _RESUME_FACT_QA_FAILURE_REASON_MAP[result.reason_code]
    except KeyError as error:
        raise ValueError("unmapped Resume Fact QA stop reason") from error
    outcome = (
        PreparationStageOutcome.DEFERRED
        if result.status
        in {
            ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM,
            ResumeFactQAStatus.DEFERRED_NEEDS_HUMAN,
        }
        else PreparationStageOutcome.FAILED
    )
    stop_reason = PreparationStopReasonEnvelope(
        stage=stage,
        code=reason,
        contract_version=RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        outcome=outcome,
        upstream_lineage_id=result.qa_binding or None,
    )
    constructor = (
        PublicPreparationStageResult.deferred
        if outcome is PreparationStageOutcome.DEFERRED
        else PublicPreparationStageResult.failed
    )
    return constructor(
        stage=stage,
        stop_reason=stop_reason,
        result_id=(
            result.qa_result.qa_result_id
            if result.qa_result is not None
            else None
        ),
        result_content_hash=(
            result.qa_result.qa_content_hash
            if result.qa_result is not None
            else None
        ),
        outputs=(
            {
                "resume_fact_qa_result_id": (
                    result.qa_result.qa_result_id
                )
            }
            if result.qa_result is not None
            else None
        ),
        retryable=result.retryable,
        human_attention_required=(
            result.status
            in {
                ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM,
                ResumeFactQAStatus.DEFERRED_NEEDS_HUMAN,
            }
        ),
    )


__all__ = [
    "ADVISORY_FINDING_TYPES",
    "AGENT_FINDING_TYPES",
    "MAX_QA_CLAIM_CHARS",
    "MAX_QA_EXPLANATION_CHARS",
    "PrivateHomeResumeFactQARepository",
    "RESUME_FACT_QA_AGENT_POLICY",
    "RESUME_FACT_QA_CONTRACT_VERSION",
    "RESUME_FACT_QA_POLICY_VERSION",
    "ResumeFactQAAgentFinding",
    "ResumeFactQAAgentMetadata",
    "ResumeFactQAAgentOutput",
    "ResumeFactQAAgentPort",
    "ResumeFactQAAgentUnavailableError",
    "ResumeFactQAAgentVerdict",
    "ResumeFactQABulletView",
    "ResumeFactQAContext",
    "ResumeFactQAEvidenceView",
    "ResumeFactQAFailureReason",
    "ResumeFactQAFinding",
    "ResumeFactQAFindingSeverity",
    "ResumeFactQAFindingSource",
    "ResumeFactQAFindingType",
    "ResumeFactQAReadResult",
    "ResumeFactQAReadStatus",
    "ResumeFactQARepository",
    "ResumeFactQAResult",
    "ResumeFactQAStatus",
    "ResumeFactQAVerdict",
    "ResumeFactQAWriteResult",
    "ResumeFactQAWriteStatus",
    "RunResumeFactQACommand",
    "RunResumeFactQAResult",
    "resume_fact_qa_finding_id",
    "run_resume_fact_qa",
    "resume_fact_qa_public_result",
]
