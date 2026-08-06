"""Independent fact QA for one evidence-bound CoverLetterDraft.

This module never imports the private validators inside
:mod:`core.cover_letter_draft`. It re-derives every deterministic check from
the typed ``CoverLetterDraft``, the ``CoverLetterEvidenceSnapshot`` and the
current ``JobPosting`` so that a bug in the Draft-generation validator cannot
silently pass a QA gate that is supposed to be a separate line of defense.
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
from .application_preparation_orchestrator import (
    COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    CoverLetterFactQAStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PublicPreparationStageResult,
)
from .cover_letter_draft import (
    CoverLetterDraft,
    CoverLetterDraftReadStatus,
    CoverLetterDraftRepository,
    CoverLetterJobContext,
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


COVER_LETTER_FACT_QA_CONTRACT_VERSION = "cover-letter-fact-qa-v1"
COVER_LETTER_FACT_QA_POLICY_VERSION = "cover-letter-fact-qa-policy-v2"

COVER_LETTER_FACT_QA_AGENT_POLICY = """Cover Letter Fact QA Agent policy (static, non-negotiable):

You review one already-written cover letter paragraph set. You never rewrite,
extend, or supply new evidence. You return only typed findings and a verdict.

You judge exactly these semantic questions, and nothing else:
1. Does the cited evidence support the degree of responsibility claimed
   (e.g. "participated in" rewritten as "led", "owned" or "single-handedly
   built" when the evidence does not say that)?
2. Is an experiment, prototype or research effort described as a shipped
   production deployment when the evidence does not say that?
3. Is an unproven business impact, scale, maturity or causal outcome
   attached to a piece of work the evidence does not attribute it to?
4. Does a motivation paragraph invent a personal connection to the
   company's mission, product, culture or industry that the evidence does
   not support?
5. Does a paragraph, taken as a whole, exceed the reasonable semantic scope
   of its cited evidence and the job description?

Every finding must cite the exact paragraph_id it concerns, and every
finding about a candidate fact must cite the evidence_id(s) that fail to
support it. If nothing rises to one of the five questions above, return an
empty findings list and a PASSED verdict. If a paragraph is ambiguous rather
than clearly wrong, return the UNCERTAIN verdict instead of guessing.
"""

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RESULT_ID_PATTERN = re.compile(r"^cover-letter-fact-qa-[a-f0-9]{64}$")
_FINDING_ID_PATTERN = re.compile(
    r"^cover-letter-fact-qa-finding-[a-f0-9]{64}$"
)
_WORD_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#/]*")
_NUMBER_PATTERN = re.compile(r"\d")
_PLACEHOLDER_PATTERN = re.compile(
    r"\[[^\]\n]{1,80}\]|\{[^}\n]{1,80}\}|\bTBD\b|\bT\.B\.D\.\b"
    r"|\bhiring manager'?s? name\b|\bcompany name\b|\bxxx+\b",
    re.IGNORECASE,
)
_GENERIC_GREETING_WORDS = frozenset(
    {
        "dear",
        "hello",
        "hi",
        "greetings",
        "hiring",
        "manager",
        "team",
        "recruiter",
        "recruiting",
        "committee",
        "sir",
        "madam",
        "there",
    }
)
MAX_CLAIM_TEXT_CHARS = 4_000
MAX_EXPLANATION_CHARS = 1_000
QUALIFICATION_LIKE_PURPOSES = frozenset({"QUALIFICATION", "MOTIVATION"})
AGENT_ELIGIBLE_FINDING_TYPES = frozenset(
    {
        "RESPONSIBILITY_LEVEL_EXAGGERATION",
        "DEPLOYMENT_STAGE_EXAGGERATION",
        "UNSUPPORTED_IMPACT_OR_CAUSALITY",
        "FABRICATED_COMPANY_CONNECTION",
        "SEMANTIC_SCOPE_OVERREACH",
    }
)


class CoverLetterFactQAFindingType(str, Enum):
    UNKNOWN_EVIDENCE_REFERENCE = "UNKNOWN_EVIDENCE_REFERENCE"
    EVIDENCE_SCOPE_INVALID = "EVIDENCE_SCOPE_INVALID"
    UNKNOWN_JD_REFERENCE = "UNKNOWN_JD_REFERENCE"
    MISSING_EVIDENCE_FOR_PURPOSE = "MISSING_EVIDENCE_FOR_PURPOSE"
    UNSUPPORTED_CANDIDATE_CLAIM = "UNSUPPORTED_CANDIDATE_CLAIM"
    JD_REQUIREMENT_PRESENTED_AS_FACT = "JD_REQUIREMENT_PRESENTED_AS_FACT"
    UNVERIFIED_GREETING_NAME = "UNVERIFIED_GREETING_NAME"
    PLACEHOLDER_PRESENT = "PLACEHOLDER_PRESENT"
    DUPLICATE_OR_MISSING_PARAGRAPH_ID = (
        "DUPLICATE_OR_MISSING_PARAGRAPH_ID"
    )
    RESPONSIBILITY_LEVEL_EXAGGERATION = (
        "RESPONSIBILITY_LEVEL_EXAGGERATION"
    )
    DEPLOYMENT_STAGE_EXAGGERATION = "DEPLOYMENT_STAGE_EXAGGERATION"
    UNSUPPORTED_IMPACT_OR_CAUSALITY = "UNSUPPORTED_IMPACT_OR_CAUSALITY"
    FABRICATED_COMPANY_CONNECTION = "FABRICATED_COMPANY_CONNECTION"
    SEMANTIC_SCOPE_OVERREACH = "SEMANTIC_SCOPE_OVERREACH"


class CoverLetterFactQAFindingSeverity(str, Enum):
    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"


class CoverLetterFactQAFindingSource(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    AGENT = "AGENT"


class CoverLetterFactQAVerdict(str, Enum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    DEFERRED = "DEFERRED"


class CoverLetterFactQAAgentVerdict(str, Enum):
    PASSED = "PASSED"
    BLOCKED = "BLOCKED"
    UNCERTAIN = "UNCERTAIN"


class CoverLetterFactQAWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class CoverLetterFactQAReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CoverLetterFactQAStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    BLOCKED_UNSUPPORTED_CLAIM = "BLOCKED_UNSUPPORTED_CLAIM"
    BLOCKED_BINDING_MISMATCH = "BLOCKED_BINDING_MISMATCH"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class CoverLetterFactQAFailureReason(str, Enum):
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


class CoverLetterFactQAAgentUnavailableError(RuntimeError):
    """Raised when the bounded Fact QA Agent cannot return an output."""


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
        raise ValueError("validated_at is invalid")
    return _require_aware(
        "validated_at", datetime.fromisoformat(value.replace("Z", "+00:00"))
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


def _words(text: str) -> tuple[str, ...]:
    return tuple(_WORD_PATTERN.findall(text))


def _casefold_word_set(text: str) -> frozenset[str]:
    return frozenset(word.casefold() for word in _words(text))


def _checkable_tokens(text: str) -> tuple[str, ...]:
    """Return mechanically verifiable numeric tokens only.

    Capitalized words require semantic context and are reviewed by the
    independent Agent below; treating capitalization as a fact signal creates
    false blocking findings for role titles, acronyms and technology names.
    """

    return tuple(
        token for token in _words(text) if _NUMBER_PATTERN.search(token)
    )


def _contains_placeholder(text: str) -> bool:
    return _PLACEHOLDER_PATTERN.search(text) is not None


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAAgentMetadata:
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
class CoverLetterFactQAParagraphView:
    paragraph_id: str
    order: int
    purpose: str
    text: str
    evidence_ids: tuple[str, ...]
    jd_alignment: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAEvidenceView:
    evidence_id: str
    evidence_text: str


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAAgentContext:
    subject_id: str
    application_plan_id: str
    job: CoverLetterJobContext
    greeting: str
    paragraphs: tuple[CoverLetterFactQAParagraphView, ...]
    closing: str
    evidence_items: tuple[CoverLetterFactQAEvidenceView, ...]
    qa_policy: str
    qa_policy_version: str


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAFindingProposal:
    paragraph_id: str
    finding_type: str
    severity: CoverLetterFactQAFindingSeverity
    claim_text: str
    evidence_ids: tuple[str, ...]
    jd_references: tuple[str, ...]
    explanation: str

    def __post_init__(self) -> None:
        finding_type = str(self.finding_type)
        if finding_type not in AGENT_ELIGIBLE_FINDING_TYPES:
            raise ValueError("Agent finding_type is not agent-eligible")
        object.__setattr__(self, "finding_type", finding_type)
        severity = CoverLetterFactQAFindingSeverity(self.severity)
        object.__setattr__(self, "severity", severity)
        _clean_text(
            "claim_text", self.claim_text, maximum=MAX_CLAIM_TEXT_CHARS
        )
        _clean_text(
            "explanation", self.explanation, maximum=MAX_EXPLANATION_CHARS
        )
        if not isinstance(self.evidence_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_ids
        ):
            raise TypeError("evidence_ids must be a tuple of identifiers")
        if not isinstance(self.jd_references, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.jd_references
        ):
            raise TypeError("jd_references must be a tuple of JD excerpts")


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAAgentOutput:
    verdict: CoverLetterFactQAAgentVerdict
    findings: tuple[CoverLetterFactQAFindingProposal, ...]

    def __post_init__(self) -> None:
        verdict = CoverLetterFactQAAgentVerdict(self.verdict)
        object.__setattr__(self, "verdict", verdict)
        if not isinstance(self.findings, tuple) or any(
            not isinstance(item, CoverLetterFactQAFindingProposal)
            for item in self.findings
        ):
            raise TypeError("findings must be a typed tuple")
        blocking = any(
            item.severity is CoverLetterFactQAFindingSeverity.BLOCKING
            for item in self.findings
        )
        if verdict is CoverLetterFactQAAgentVerdict.BLOCKED and not blocking:
            raise ValueError(
                "a BLOCKED verdict requires at least one blocking finding"
            )
        if verdict is CoverLetterFactQAAgentVerdict.PASSED and blocking:
            raise ValueError(
                "a PASSED verdict cannot carry a blocking finding"
            )


@runtime_checkable
class CoverLetterFactQAAgentPort(Protocol):
    async def review(
        self, context: CoverLetterFactQAAgentContext
    ) -> CoverLetterFactQAAgentOutput:
        """Judge semantic exaggeration in one draft, tool-free."""


def _finding_content(
    *,
    paragraph_id: str,
    finding_type: str,
    severity: CoverLetterFactQAFindingSeverity,
    claim_text: str,
    evidence_ids: tuple[str, ...],
    jd_references: tuple[str, ...],
    explanation: str,
    source: CoverLetterFactQAFindingSource,
) -> dict[str, Any]:
    return {
        "claim_text": claim_text,
        "evidence_ids": list(evidence_ids),
        "explanation": explanation,
        "finding_type": finding_type,
        "jd_references": list(jd_references),
        "paragraph_id": paragraph_id,
        "severity": severity.value,
        "source": source.value,
    }


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAFinding:
    finding_id: str
    paragraph_id: str
    finding_type: str
    severity: CoverLetterFactQAFindingSeverity
    claim_text: str
    evidence_ids: tuple[str, ...]
    jd_references: tuple[str, ...]
    explanation: str
    source: CoverLetterFactQAFindingSource

    def __post_init__(self) -> None:
        _clean_text("paragraph_id", self.paragraph_id, maximum=160)
        _clean_text("finding_type", self.finding_type, maximum=80)
        severity = CoverLetterFactQAFindingSeverity(self.severity)
        object.__setattr__(self, "severity", severity)
        _clean_text(
            "claim_text", self.claim_text, maximum=MAX_CLAIM_TEXT_CHARS
        )
        _clean_text(
            "explanation", self.explanation, maximum=MAX_EXPLANATION_CHARS
        )
        source = CoverLetterFactQAFindingSource(self.source)
        object.__setattr__(self, "source", source)
        if not isinstance(self.evidence_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.evidence_ids
        ):
            raise TypeError("evidence_ids must be a tuple of identifiers")
        if not isinstance(self.jd_references, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.jd_references
        ):
            raise TypeError("jd_references must be a tuple of JD excerpts")
        expected_id = "cover-letter-fact-qa-finding-" + _canonical_hash(
            self.content_dict()
        )
        if (
            not isinstance(self.finding_id, str)
            or _FINDING_ID_PATTERN.fullmatch(self.finding_id) is None
            or self.finding_id != expected_id
        ):
            raise ValueError("finding_id does not match its content")

    def content_dict(self) -> dict[str, Any]:
        return _finding_content(
            paragraph_id=self.paragraph_id,
            finding_type=self.finding_type,
            severity=self.severity,
            claim_text=self.claim_text,
            evidence_ids=self.evidence_ids,
            jd_references=self.jd_references,
            explanation=self.explanation,
            source=self.source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"finding_id": self.finding_id, **self.content_dict()}


def _build_finding(
    *,
    paragraph_id: str,
    finding_type: CoverLetterFactQAFindingType,
    severity: CoverLetterFactQAFindingSeverity,
    claim_text: str,
    evidence_ids: tuple[str, ...],
    jd_references: tuple[str, ...],
    explanation: str,
    source: CoverLetterFactQAFindingSource,
) -> CoverLetterFactQAFinding:
    content = _finding_content(
        paragraph_id=paragraph_id,
        finding_type=finding_type.value,
        severity=severity,
        claim_text=claim_text,
        evidence_ids=evidence_ids,
        jd_references=jd_references,
        explanation=explanation,
        source=source,
    )
    return CoverLetterFactQAFinding(
        finding_id="cover-letter-fact-qa-finding-" + _canonical_hash(content),
        paragraph_id=paragraph_id,
        finding_type=finding_type.value,
        severity=severity,
        claim_text=claim_text,
        evidence_ids=evidence_ids,
        jd_references=jd_references,
        explanation=explanation,
        source=source,
    )


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAResult:
    result_id: str
    contract_version: str
    result_binding: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    evidence_snapshot_id: str
    evidence_snapshot_hash: str
    cover_letter_draft_id: str
    draft_content_hash: str
    agent_version: str
    prompt_version: str
    model_id: str
    qa_policy_version: str
    verdict: CoverLetterFactQAVerdict
    findings: tuple[CoverLetterFactQAFinding, ...]
    result_content_hash: str
    validated_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != COVER_LETTER_FACT_QA_CONTRACT_VERSION:
            raise ValueError("cover-letter fact QA contract is unsupported")
        binding = _require_hash("result_binding", self.result_binding)
        if (
            not isinstance(self.result_id, str)
            or _RESULT_ID_PATTERN.fullmatch(self.result_id) is None
            or self.result_id != f"cover-letter-fact-qa-{binding}"
        ):
            raise ValueError("result_id does not match its binding")
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
        _clean_text(
            "cover_letter_draft_id", self.cover_letter_draft_id, maximum=160
        )
        _require_hash("draft_content_hash", self.draft_content_hash)
        _clean_text("agent_version", self.agent_version, maximum=80)
        _clean_text("prompt_version", self.prompt_version, maximum=80)
        _clean_text("model_id", self.model_id, maximum=160)
        policy_version = _clean_text(
            "qa_policy_version", self.qa_policy_version, maximum=80
        )
        if policy_version != COVER_LETTER_FACT_QA_POLICY_VERSION:
            raise ValueError("cover-letter QA policy version is unsupported")
        verdict = CoverLetterFactQAVerdict(self.verdict)
        object.__setattr__(self, "verdict", verdict)
        if not isinstance(self.findings, tuple) or any(
            not isinstance(item, CoverLetterFactQAFinding)
            for item in self.findings
        ):
            raise TypeError("findings must be a typed tuple")
        finding_ids = [item.finding_id for item in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("finding identities must be unique")
        has_blocking = any(
            item.severity is CoverLetterFactQAFindingSeverity.BLOCKING
            for item in self.findings
        )
        if verdict is CoverLetterFactQAVerdict.PASSED and has_blocking:
            raise ValueError("a PASSED result cannot carry a blocking finding")
        if verdict is CoverLetterFactQAVerdict.BLOCKED and not has_blocking:
            raise ValueError("a BLOCKED result requires a blocking finding")
        if verdict is CoverLetterFactQAVerdict.DEFERRED:
            raise ValueError("a persisted result cannot carry DEFERRED")
        object.__setattr__(self, "contract_version", contract)
        _require_aware("validated_at", self.validated_at)
        content_hash = _require_hash(
            "result_content_hash", self.result_content_hash
        )
        if content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("result content hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "agent_version": self.agent_version,
            "application_plan_id": self.application_plan_id,
            "contract_version": self.contract_version,
            "cover_letter_draft_id": self.cover_letter_draft_id,
            "draft_content_hash": self.draft_content_hash,
            "evidence_snapshot_hash": self.evidence_snapshot_hash,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "findings": [item.to_dict() for item in self.findings],
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "qa_policy_version": self.qa_policy_version,
            "result_binding": self.result_binding,
            "result_id": self.result_id,
            "subject_id": self.subject_id,
            "verdict": self.verdict.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "result_content_hash": self.result_content_hash,
            "validated_at": _rfc3339(self.validated_at),
        }


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAWriteResult:
    status: CoverLetterFactQAWriteStatus
    result: CoverLetterFactQAResult | None
    reason_code: CoverLetterFactQAFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = CoverLetterFactQAWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CoverLetterFactQAFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            CoverLetterFactQAWriteStatus.CREATED,
            CoverLetterFactQAWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.result, CoverLetterFactQAResult)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful QA write result is invalid")
        elif self.result is not None or self.reason_code is None:
            raise ValueError("failed QA write result is invalid")


@dataclass(frozen=True, slots=True)
class CoverLetterFactQAReadResult:
    status: CoverLetterFactQAReadStatus
    result: CoverLetterFactQAResult | None
    reason_code: CoverLetterFactQAFailureReason | None = None

    def __post_init__(self) -> None:
        status = CoverLetterFactQAReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CoverLetterFactQAFailureReason(self.reason_code),
            )
        if status is CoverLetterFactQAReadStatus.FOUND:
            if (
                not isinstance(self.result, CoverLetterFactQAResult)
                or self.reason_code is not None
            ):
                raise ValueError("found QA read result is invalid")
        elif status is CoverLetterFactQAReadStatus.NOT_FOUND:
            if self.result is not None or self.reason_code is not None:
                raise ValueError("not-found QA read result is invalid")
        elif (
            self.result is not None
            or self.reason_code
            is not CoverLetterFactQAFailureReason.RESULT_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure QA read result is invalid")


@runtime_checkable
class CoverLetterFactQARepository(Protocol):
    def save(
        self, result: CoverLetterFactQAResult
    ) -> CoverLetterFactQAWriteResult:
        """Persist one immutable cover-letter Fact QA result."""

    def get(
        self, *, subject_id: str, result_id: str
    ) -> CoverLetterFactQAReadResult:
        """Read one subject-owned cover-letter Fact QA result."""


def _finding_from_dict(value: Any) -> CoverLetterFactQAFinding:
    expected = {
        "finding_id",
        "paragraph_id",
        "finding_type",
        "severity",
        "claim_text",
        "evidence_ids",
        "jd_references",
        "explanation",
        "source",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["evidence_ids"], list)
        or not isinstance(value["jd_references"], list)
    ):
        raise ValueError("persisted CoverLetterFactQAFinding is invalid")
    return CoverLetterFactQAFinding(
        finding_id=value["finding_id"],
        paragraph_id=value["paragraph_id"],
        finding_type=value["finding_type"],
        severity=CoverLetterFactQAFindingSeverity(value["severity"]),
        claim_text=value["claim_text"],
        evidence_ids=tuple(value["evidence_ids"]),
        jd_references=tuple(value["jd_references"]),
        explanation=value["explanation"],
        source=CoverLetterFactQAFindingSource(value["source"]),
    )


def _result_from_dict(value: Any) -> CoverLetterFactQAResult:
    expected = {
        "result_id",
        "contract_version",
        "result_binding",
        "subject_id",
        "application_plan_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "evidence_snapshot_id",
        "evidence_snapshot_hash",
        "cover_letter_draft_id",
        "draft_content_hash",
        "agent_version",
        "prompt_version",
        "model_id",
        "qa_policy_version",
        "verdict",
        "findings",
        "result_content_hash",
        "validated_at",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["findings"], list)
    ):
        raise ValueError("persisted CoverLetterFactQAResult is invalid")
    return CoverLetterFactQAResult(
        result_id=value["result_id"],
        contract_version=value["contract_version"],
        result_binding=value["result_binding"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        evidence_snapshot_id=value["evidence_snapshot_id"],
        evidence_snapshot_hash=value["evidence_snapshot_hash"],
        cover_letter_draft_id=value["cover_letter_draft_id"],
        draft_content_hash=value["draft_content_hash"],
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        qa_policy_version=value["qa_policy_version"],
        verdict=CoverLetterFactQAVerdict(value["verdict"]),
        findings=tuple(
            _finding_from_dict(item) for item in value["findings"]
        ),
        result_content_hash=value["result_content_hash"],
        validated_at=_parse_timestamp(value["validated_at"]),
    )


class PrivateHomeCoverLetterFactQARepository:
    """Immutable, subject-scoped cover-letter Fact QA results in Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, result_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(result_id, str)
            or _RESULT_ID_PATTERN.fullmatch(result_id) is None
        ):
            raise ValueError("result_id is invalid")
        return (
            self._home.paths.cover_letter_fact_qa_results
            / _subject_storage_key(subject)
            / f"{result_id}.json"
        )

    def get(
        self, *, subject_id: str, result_id: str
    ) -> CoverLetterFactQAReadResult:
        path = self._path(subject_id, result_id)
        with self._lock:
            if not path.exists():
                return CoverLetterFactQAReadResult(
                    status=CoverLetterFactQAReadStatus.NOT_FOUND,
                    result=None,
                )
            if path.is_symlink() or not path.is_file():
                return CoverLetterFactQAReadResult(
                    status=CoverLetterFactQAReadStatus.INTEGRITY_FAILURE,
                    result=None,
                    reason_code=(
                        CoverLetterFactQAFailureReason
                        .RESULT_INTEGRITY_FAILURE
                    ),
                )
            try:
                result = _result_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return CoverLetterFactQAReadResult(
                    status=CoverLetterFactQAReadStatus.INTEGRITY_FAILURE,
                    result=None,
                    reason_code=(
                        CoverLetterFactQAFailureReason
                        .RESULT_INTEGRITY_FAILURE
                    ),
                )
            if (
                result.subject_id != subject_id.strip()
                or result.result_id != result_id
                or path.name != f"{result.result_id}.json"
            ):
                return CoverLetterFactQAReadResult(
                    status=CoverLetterFactQAReadStatus.INTEGRITY_FAILURE,
                    result=None,
                    reason_code=(
                        CoverLetterFactQAFailureReason
                        .RESULT_INTEGRITY_FAILURE
                    ),
                )
            return CoverLetterFactQAReadResult(
                status=CoverLetterFactQAReadStatus.FOUND,
                result=result,
            )

    def save(
        self, result: CoverLetterFactQAResult
    ) -> CoverLetterFactQAWriteResult:
        if not isinstance(result, CoverLetterFactQAResult):
            raise TypeError("result must be a CoverLetterFactQAResult")
        path = self._path(result.subject_id, result.result_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            result.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return CoverLetterFactQAWriteResult(
                    status=CoverLetterFactQAWriteStatus.FAILED,
                    result=None,
                    reason_code=(
                        CoverLetterFactQAFailureReason
                        .RESULT_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return CoverLetterFactQAWriteResult(
                    status=CoverLetterFactQAWriteStatus.CREATED,
                    result=result,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=result.subject_id, result_id=result.result_id
            )
            if (
                existing.status is CoverLetterFactQAReadStatus.FOUND
                and existing.result is not None
                and existing.result.content_dict() == result.content_dict()
            ):
                return CoverLetterFactQAWriteResult(
                    status=CoverLetterFactQAWriteStatus.UNCHANGED,
                    result=existing.result,
                    reason_code=None,
                    retryable=False,
                )
            return CoverLetterFactQAWriteResult(
                status=CoverLetterFactQAWriteStatus.FAILED,
                result=None,
                reason_code=(
                    CoverLetterFactQAFailureReason.RESULT_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class RunCoverLetterFactQACommand:
    subject_id: str
    application_plan_id: str
    cover_letter_evidence_snapshot_id: str
    cover_letter_draft_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class RunCoverLetterFactQAResult:
    status: CoverLetterFactQAStatus
    subject_id: str
    application_plan_id: str
    result_binding: str
    result: CoverLetterFactQAResult | None
    write_result: CoverLetterFactQAWriteResult | None
    reason_code: CoverLetterFactQAFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = CoverLetterFactQAStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CoverLetterFactQAFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            CoverLetterFactQAStatus.CREATED,
            CoverLetterFactQAStatus.UNCHANGED,
            CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM,
        }:
            if (
                not isinstance(self.result, CoverLetterFactQAResult)
                or not isinstance(
                    self.write_result, CoverLetterFactQAWriteResult
                )
                or self.write_result.result != self.result
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful QA result is invalid")
        elif status is CoverLetterFactQAStatus.DEFERRED_NEEDS_HUMAN:
            if (
                self.result is not None
                or self.write_result is not None
                or self.reason_code
                is not CoverLetterFactQAFailureReason.AGENT_OUTPUT_UNSAFE
                or self.retryable
            ):
                raise ValueError("needs-human QA result is invalid")
        elif self.result is not None or self.reason_code is None:
            raise ValueError("failed QA result is invalid")


def _failure(
    command: RunCoverLetterFactQACommand,
    reason: CoverLetterFactQAFailureReason,
    *,
    status: CoverLetterFactQAStatus = CoverLetterFactQAStatus.FAILED,
    retryable: bool = False,
    result_binding: str = "",
    write_result: CoverLetterFactQAWriteResult | None = None,
) -> RunCoverLetterFactQAResult:
    return RunCoverLetterFactQAResult(
        status=status,
        subject_id=(
            command.subject_id if isinstance(command.subject_id, str) else ""
        ),
        application_plan_id=(
            command.application_plan_id
            if isinstance(command.application_plan_id, str)
            else ""
        ),
        result_binding=result_binding,
        result=None,
        write_result=write_result,
        reason_code=reason,
        retryable=retryable,
        message=f"Cover letter Fact QA failed: {reason.value}.",
    )


def _needs_human(
    command: RunCoverLetterFactQACommand, *, result_binding: str, detail: str
) -> RunCoverLetterFactQAResult:
    return RunCoverLetterFactQAResult(
        status=CoverLetterFactQAStatus.DEFERRED_NEEDS_HUMAN,
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        result_binding=result_binding,
        result=None,
        write_result=None,
        reason_code=CoverLetterFactQAFailureReason.AGENT_OUTPUT_UNSAFE,
        retryable=False,
        message=f"The cover letter Fact QA result needs human review: {detail}",
    )


def _qa_binding(
    *,
    draft: CoverLetterDraft,
    job: JobPosting,
    snapshot: CoverLetterEvidenceSnapshot,
    metadata: CoverLetterFactQAAgentMetadata,
) -> str:
    return _canonical_hash(
        {
            "cover_letter_draft_id": draft.draft_id,
            "cover_letter_fact_qa_agent_version": metadata.agent_version,
            "cover_letter_fact_qa_contract_version": (
                COVER_LETTER_FACT_QA_CONTRACT_VERSION
            ),
            "cover_letter_fact_qa_model_id": metadata.model_id,
            "cover_letter_fact_qa_policy_version": (
                COVER_LETTER_FACT_QA_POLICY_VERSION
            ),
            "cover_letter_fact_qa_prompt_version": metadata.prompt_version,
            "draft_content_hash": draft.draft_content_hash,
            "evidence_snapshot_hash": snapshot.snapshot_content_hash,
            "evidence_snapshot_id": snapshot.snapshot_id,
            "job_content_hash": job.content_hash,
            "job_id": job.job_id,
            "job_revision": job.revision,
        }
    )


def _deterministic_findings(
    *,
    draft: CoverLetterDraft,
    snapshot: CoverLetterEvidenceSnapshot,
    job: JobPosting,
) -> tuple[CoverLetterFactQAFinding, ...]:
    """Re-derive every mechanically checkable rule directly from the Draft."""

    evidence_by_id = {
        item.evidence_id: item for item in snapshot.evidence_items
    }
    jd_words = _casefold_word_set(job.description)
    job_context_words = (
        jd_words
        | _casefold_word_set(job.title)
        | _casefold_word_set(job.company)
    )
    findings: list[CoverLetterFactQAFinding] = []

    all_text = draft.greeting + " " + draft.closing
    for paragraph in draft.paragraphs:
        all_text += " " + paragraph.text
    if _contains_placeholder(all_text):
        findings.append(
            _build_finding(
                paragraph_id=draft.draft_id,
                finding_type=CoverLetterFactQAFindingType.PLACEHOLDER_PRESENT,
                severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                claim_text=all_text[:MAX_CLAIM_TEXT_CHARS],
                evidence_ids=(),
                jd_references=(),
                explanation=(
                    "the draft contains an unresolved placeholder token"
                ),
                source=CoverLetterFactQAFindingSource.DETERMINISTIC,
            )
        )

    for greeting_word in _words(draft.greeting):
        folded = greeting_word.casefold()
        if folded in _GENERIC_GREETING_WORDS:
            continue
        if not any(char.isupper() for char in greeting_word):
            continue
        if folded in jd_words:
            continue
        findings.append(
            _build_finding(
                paragraph_id=draft.draft_id,
                finding_type=(
                    CoverLetterFactQAFindingType.UNVERIFIED_GREETING_NAME
                ),
                severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                claim_text=draft.greeting,
                evidence_ids=(),
                jd_references=(),
                explanation=(
                    f"the greeting names '{greeting_word}', which is not "
                    "present in the trusted job description"
                ),
                source=CoverLetterFactQAFindingSource.DETERMINISTIC,
            )
        )

    seen_order: set[int] = set()
    seen_ids: set[str] = set()
    for paragraph in draft.paragraphs:
        if (
            paragraph.order in seen_order
            or paragraph.paragraph_id in seen_ids
        ):
            findings.append(
                _build_finding(
                    paragraph_id=paragraph.paragraph_id,
                    finding_type=(
                        CoverLetterFactQAFindingType
                        .DUPLICATE_OR_MISSING_PARAGRAPH_ID
                    ),
                    severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                    claim_text=paragraph.text,
                    evidence_ids=(),
                    jd_references=(),
                    explanation="paragraph order or identity is duplicated",
                    source=CoverLetterFactQAFindingSource.DETERMINISTIC,
                )
            )
        seen_order.add(paragraph.order)
        seen_ids.add(paragraph.paragraph_id)

        for evidence_id in paragraph.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                findings.append(
                    _build_finding(
                        paragraph_id=paragraph.paragraph_id,
                        finding_type=(
                            CoverLetterFactQAFindingType
                            .UNKNOWN_EVIDENCE_REFERENCE
                        ),
                        severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                        claim_text=paragraph.text,
                        evidence_ids=(evidence_id,),
                        jd_references=(),
                        explanation=(
                            f"evidence_id {evidence_id} is not in the "
                            "current evidence snapshot"
                        ),
                        source=CoverLetterFactQAFindingSource.DETERMINISTIC,
                    )
                )
            elif (
                CoverLetterEvidenceScope.COVER_LETTER
                not in evidence.allowed_scopes
            ):
                findings.append(
                    _build_finding(
                        paragraph_id=paragraph.paragraph_id,
                        finding_type=(
                            CoverLetterFactQAFindingType
                            .EVIDENCE_SCOPE_INVALID
                        ),
                        severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                        claim_text=paragraph.text,
                        evidence_ids=(evidence_id,),
                        jd_references=(),
                        explanation=(
                            f"evidence_id {evidence_id} is not scoped for "
                            "cover-letter use"
                        ),
                        source=CoverLetterFactQAFindingSource.DETERMINISTIC,
                    )
                )

        for reference in paragraph.jd_alignment:
            if reference not in job.description:
                findings.append(
                    _build_finding(
                        paragraph_id=paragraph.paragraph_id,
                        finding_type=(
                            CoverLetterFactQAFindingType.UNKNOWN_JD_REFERENCE
                        ),
                        severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                        claim_text=paragraph.text,
                        evidence_ids=(),
                        jd_references=(reference,),
                        explanation=(
                            "the JD alignment reference is not an exact "
                            "excerpt of the current job description"
                        ),
                        source=CoverLetterFactQAFindingSource.DETERMINISTIC,
                    )
                )

        if (
            paragraph.purpose.value in QUALIFICATION_LIKE_PURPOSES
            and not paragraph.evidence_ids
        ):
            findings.append(
                _build_finding(
                    paragraph_id=paragraph.paragraph_id,
                    finding_type=(
                        CoverLetterFactQAFindingType
                        .MISSING_EVIDENCE_FOR_PURPOSE
                    ),
                    severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                    claim_text=paragraph.text,
                    evidence_ids=(),
                    jd_references=(),
                    explanation=(
                        "a qualification or motivation paragraph has no "
                        "cited evidence"
                    ),
                    source=CoverLetterFactQAFindingSource.DETERMINISTIC,
                )
            )

        cited_words: set[str] = set()
        for evidence_id in paragraph.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is not None:
                cited_words |= _casefold_word_set(evidence.evidence_text)

        if paragraph.purpose.value in QUALIFICATION_LIKE_PURPOSES:
            if paragraph.purpose == "QUALIFICATION":
                paragraph_text = paragraph.text.casefold()
                cited_evidence_text = tuple(
                    evidence_by_id[evidence_id].evidence_text.casefold()
                    for evidence_id in paragraph.evidence_ids
                    if evidence_id in evidence_by_id
                )
                if any(
                    reference.casefold() in paragraph_text
                    and not any(
                        reference.casefold() in evidence_text
                        for evidence_text in cited_evidence_text
                    )
                    for reference in paragraph.jd_alignment
                ):
                    findings.append(
                        _build_finding(
                            paragraph_id=paragraph.paragraph_id,
                            finding_type=(
                                CoverLetterFactQAFindingType
                                .JD_REQUIREMENT_PRESENTED_AS_FACT
                            ),
                            severity=(
                                CoverLetterFactQAFindingSeverity.BLOCKING
                            ),
                            claim_text=paragraph.text,
                            evidence_ids=paragraph.evidence_ids,
                            jd_references=(),
                            explanation=(
                                "the paragraph repeats a JD-derived "
                                "requirement as a candidate fact"
                            ),
                            source=(
                                CoverLetterFactQAFindingSource.DETERMINISTIC
                            ),
                        )
                    )
            for token in _checkable_tokens(paragraph.text):
                folded = token.casefold()
                if folded not in cited_words and folded not in jd_words:
                    findings.append(
                        _build_finding(
                            paragraph_id=paragraph.paragraph_id,
                            finding_type=(
                                CoverLetterFactQAFindingType
                                .UNSUPPORTED_CANDIDATE_CLAIM
                            ),
                            severity=(
                                CoverLetterFactQAFindingSeverity.BLOCKING
                            ),
                            claim_text=paragraph.text,
                            evidence_ids=paragraph.evidence_ids,
                            jd_references=(),
                            explanation=(
                                f"the paragraph asserts {token} without "
                                "supporting evidence"
                            ),
                            source=(
                                CoverLetterFactQAFindingSource.DETERMINISTIC
                            ),
                        )
                    )
                elif folded in jd_words and folded not in cited_words:
                    findings.append(
                        _build_finding(
                            paragraph_id=paragraph.paragraph_id,
                            finding_type=(
                                CoverLetterFactQAFindingType
                                .JD_REQUIREMENT_PRESENTED_AS_FACT
                            ),
                            severity=(
                                CoverLetterFactQAFindingSeverity.BLOCKING
                            ),
                            claim_text=paragraph.text,
                            evidence_ids=paragraph.evidence_ids,
                            jd_references=(),
                            explanation=(
                                f"'{token}' is a job-description detail "
                                "presented as a candidate fact without "
                                "evidence"
                            ),
                            source=(
                                CoverLetterFactQAFindingSource.DETERMINISTIC
                            ),
                        )
                    )
        else:
            allowed_words = job_context_words | cited_words
            for token in _checkable_tokens(paragraph.text):
                folded = token.casefold()
                if folded not in allowed_words:
                    findings.append(
                        _build_finding(
                            paragraph_id=paragraph.paragraph_id,
                            finding_type=(
                                CoverLetterFactQAFindingType
                                .UNSUPPORTED_CANDIDATE_CLAIM
                            ),
                            severity=(
                                CoverLetterFactQAFindingSeverity.BLOCKING
                            ),
                            claim_text=paragraph.text,
                            evidence_ids=paragraph.evidence_ids,
                            jd_references=paragraph.jd_alignment,
                            explanation=(
                                f"the paragraph states {token} without "
                                "support from the job description or cited "
                                "evidence"
                            ),
                            source=(
                                CoverLetterFactQAFindingSource.DETERMINISTIC
                            ),
                        )
                    )

    return tuple(findings)


class _AgentOutputRejected(ValueError):
    """The Fact QA Agent output failed a deterministic safety check."""


def _agent_findings(
    *,
    output: CoverLetterFactQAAgentOutput,
    draft: CoverLetterDraft,
    snapshot: CoverLetterEvidenceSnapshot,
    job: JobPosting,
) -> tuple[CoverLetterFactQAFinding, ...]:
    """Verify every Agent finding references known, current objects."""

    paragraph_ids = {item.paragraph_id for item in draft.paragraphs}
    evidence_by_id = {
        item.evidence_id: item for item in snapshot.evidence_items
    }
    findings: list[CoverLetterFactQAFinding] = []
    for proposal in output.findings:
        if proposal.paragraph_id not in paragraph_ids:
            raise _AgentOutputRejected(
                "a finding references an unknown paragraph"
            )
        for evidence_id in proposal.evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                raise _AgentOutputRejected(
                    "a finding references an unknown evidence item"
                )
            if (
                CoverLetterEvidenceScope.COVER_LETTER
                not in evidence.allowed_scopes
            ):
                raise _AgentOutputRejected(
                    "a finding references out-of-scope evidence"
                )
        for reference in proposal.jd_references:
            if reference not in job.description:
                raise _AgentOutputRejected(
                    "a finding references JD text that is not verbatim"
                )
        findings.append(
            _build_finding(
                paragraph_id=proposal.paragraph_id,
                finding_type=CoverLetterFactQAFindingType(
                    proposal.finding_type
                ),
                severity=proposal.severity,
                claim_text=proposal.claim_text,
                evidence_ids=proposal.evidence_ids,
                jd_references=proposal.jd_references,
                explanation=proposal.explanation,
                source=CoverLetterFactQAFindingSource.AGENT,
            )
        )
    return tuple(findings)


async def review_cover_letter_fact_qa(
    command: RunCoverLetterFactQACommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    job_repository: JobPostingReadRepository,
    evidence_snapshot_repository: CoverLetterEvidenceSnapshotRepository,
    draft_repository: CoverLetterDraftRepository,
    agent: CoverLetterFactQAAgentPort,
    metadata: CoverLetterFactQAAgentMetadata,
    result_repository: CoverLetterFactQARepository,
) -> RunCoverLetterFactQAResult:
    """Independently verify one CoverLetterDraft, at most one Agent call."""

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
        draft_id = _clean_text(
            "cover_letter_draft_id",
            command.cover_letter_draft_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
        if not isinstance(metadata, CoverLetterFactQAAgentMetadata):
            raise TypeError(
                "metadata must be CoverLetterFactQAAgentMetadata"
            )
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, CoverLetterFactQAFailureReason.INVALID_REQUEST
        )

    try:
        plan_result = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CoverLetterFactQAFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_result.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command, CoverLetterFactQAFailureReason.APPLICATION_PLAN_NOT_FOUND
        )
    if (
        plan_result.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_result.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            CoverLetterFactQAFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_result.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            CoverLetterFactQAFailureReason.APPLICATION_PLAN_SUBJECT_MISMATCH,
            status=CoverLetterFactQAStatus.BLOCKED_BINDING_MISMATCH,
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
            command, CoverLetterFactQAFailureReason.JOB_READ_FAILED
        )
    if job is None:
        return _failure(
            command, CoverLetterFactQAFailureReason.JOB_NOT_FOUND
        )
    if (
        not isinstance(job, JobPosting)
        or job.job_id != plan.job_id
        or job.revision != plan.job_revision
        or job.content_hash != plan.job_content_hash
    ):
        return _failure(
            command,
            CoverLetterFactQAFailureReason.JOB_BINDING_MISMATCH,
            status=CoverLetterFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    try:
        snapshot_result = evidence_snapshot_repository.get(
            subject_id=subject_id, snapshot_id=snapshot_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CoverLetterFactQAFailureReason
            .EVIDENCE_SNAPSHOT_INTEGRITY_FAILURE,
        )
    if (
        snapshot_result.status
        is CoverLetterEvidenceSnapshotReadStatus.NOT_FOUND
    ):
        return _failure(
            command,
            CoverLetterFactQAFailureReason.EVIDENCE_SNAPSHOT_NOT_FOUND,
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
            CoverLetterFactQAFailureReason
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
            CoverLetterFactQAFailureReason
            .EVIDENCE_SNAPSHOT_BINDING_MISMATCH,
            status=CoverLetterFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    try:
        draft_result = draft_repository.get(
            subject_id=subject_id, draft_id=draft_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command, CoverLetterFactQAFailureReason.DRAFT_INTEGRITY_FAILURE
        )
    if draft_result.status is CoverLetterDraftReadStatus.NOT_FOUND:
        return _failure(
            command, CoverLetterFactQAFailureReason.DRAFT_NOT_FOUND
        )
    if (
        draft_result.status is not CoverLetterDraftReadStatus.FOUND
        or not isinstance(draft_result.draft, CoverLetterDraft)
    ):
        return _failure(
            command, CoverLetterFactQAFailureReason.DRAFT_INTEGRITY_FAILURE
        )
    draft = draft_result.draft
    if (
        draft.subject_id != subject_id
        or draft.application_plan_id != plan.plan_id
        or draft.job_id != job.job_id
        or draft.job_revision != job.revision
        or draft.job_content_hash != job.content_hash
        or draft.evidence_snapshot_id != snapshot.snapshot_id
        or draft.evidence_snapshot_hash != snapshot.snapshot_content_hash
    ):
        return _failure(
            command,
            CoverLetterFactQAFailureReason.DRAFT_BINDING_MISMATCH,
            status=CoverLetterFactQAStatus.BLOCKED_BINDING_MISMATCH,
        )

    binding = _qa_binding(
        draft=draft, job=job, snapshot=snapshot, metadata=metadata
    )
    result_id = f"cover-letter-fact-qa-{binding}"
    try:
        existing = result_repository.get(
            subject_id=subject_id, result_id=result_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            CoverLetterFactQAFailureReason.RESULT_INTEGRITY_FAILURE,
            result_binding=binding,
        )
    if existing.status is CoverLetterFactQAReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            CoverLetterFactQAFailureReason.RESULT_INTEGRITY_FAILURE,
            result_binding=binding,
        )
    if (
        existing.status is CoverLetterFactQAReadStatus.FOUND
        and existing.result is not None
    ):
        write_result = CoverLetterFactQAWriteResult(
            status=CoverLetterFactQAWriteStatus.UNCHANGED,
            result=existing.result,
            reason_code=None,
            retryable=False,
        )
        return RunCoverLetterFactQAResult(
            status=CoverLetterFactQAStatus.UNCHANGED,
            subject_id=subject_id,
            application_plan_id=plan_id,
            result_binding=binding,
            result=existing.result,
            write_result=write_result,
            reason_code=None,
            retryable=False,
            message="The existing cover letter Fact QA result is unchanged.",
        )

    deterministic = _deterministic_findings(
        draft=draft, snapshot=snapshot, job=job
    )
    deterministic_blocking = tuple(
        item
        for item in deterministic
        if item.severity is CoverLetterFactQAFindingSeverity.BLOCKING
    )

    async def _persist(
        *, verdict: CoverLetterFactQAVerdict, findings: tuple[
            CoverLetterFactQAFinding, ...
        ]
    ) -> RunCoverLetterFactQAResult:
        content = {
            "agent_version": metadata.agent_version,
            "application_plan_id": plan.plan_id,
            "contract_version": COVER_LETTER_FACT_QA_CONTRACT_VERSION,
            "cover_letter_draft_id": draft.draft_id,
            "draft_content_hash": draft.draft_content_hash,
            "evidence_snapshot_hash": snapshot.snapshot_content_hash,
            "evidence_snapshot_id": snapshot.snapshot_id,
            "findings": [item.to_dict() for item in findings],
            "job_content_hash": job.content_hash,
            "job_id": job.job_id,
            "job_revision": job.revision,
            "model_id": metadata.model_id,
            "prompt_version": metadata.prompt_version,
            "qa_policy_version": COVER_LETTER_FACT_QA_POLICY_VERSION,
            "result_binding": binding,
            "result_id": result_id,
            "subject_id": subject_id,
            "verdict": verdict.value,
        }
        result = CoverLetterFactQAResult(
            result_id=result_id,
            contract_version=COVER_LETTER_FACT_QA_CONTRACT_VERSION,
            result_binding=binding,
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            job_id=job.job_id,
            job_revision=job.revision,
            job_content_hash=job.content_hash,
            evidence_snapshot_id=snapshot.snapshot_id,
            evidence_snapshot_hash=snapshot.snapshot_content_hash,
            cover_letter_draft_id=draft.draft_id,
            draft_content_hash=draft.draft_content_hash,
            agent_version=metadata.agent_version,
            prompt_version=metadata.prompt_version,
            model_id=metadata.model_id,
            qa_policy_version=COVER_LETTER_FACT_QA_POLICY_VERSION,
            verdict=verdict,
            findings=findings,
            result_content_hash=_canonical_hash(content),
            validated_at=now,
        )
        try:
            write_result = result_repository.save(result)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                CoverLetterFactQAFailureReason.RESULT_PERSISTENCE_FAILED,
                retryable=True,
                result_binding=binding,
            )
        if write_result.status is CoverLetterFactQAWriteStatus.FAILED:
            return _failure(
                command,
                write_result.reason_code
                or CoverLetterFactQAFailureReason.RESULT_PERSISTENCE_FAILED,
                retryable=write_result.retryable,
                result_binding=binding,
                write_result=write_result,
            )
        top_status = (
            CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
            if verdict is CoverLetterFactQAVerdict.BLOCKED
            else CoverLetterFactQAStatus(write_result.status.value)
        )
        return RunCoverLetterFactQAResult(
            status=top_status,
            subject_id=subject_id,
            application_plan_id=plan_id,
            result_binding=binding,
            result=write_result.result,
            write_result=write_result,
            reason_code=None,
            retryable=False,
            message=(
                "The cover letter Fact QA result was blocked."
                if verdict is CoverLetterFactQAVerdict.BLOCKED
                else "The cover letter Fact QA result passed."
            ),
        )

    if deterministic_blocking:
        return await _persist(
            verdict=CoverLetterFactQAVerdict.BLOCKED,
            findings=deterministic,
        )

    context = CoverLetterFactQAAgentContext(
        subject_id=subject_id,
        application_plan_id=plan_id,
        job=CoverLetterJobContext.from_job(job),
        greeting=draft.greeting,
        paragraphs=tuple(
            CoverLetterFactQAParagraphView(
                paragraph_id=item.paragraph_id,
                order=item.order,
                purpose=item.purpose.value,
                text=item.text,
                evidence_ids=item.evidence_ids,
                jd_alignment=item.jd_alignment,
            )
            for item in draft.paragraphs
        ),
        closing=draft.closing,
        evidence_items=tuple(
            CoverLetterFactQAEvidenceView(
                evidence_id=item.evidence_id,
                evidence_text=item.evidence_text,
            )
            for item in snapshot.evidence_items
            if CoverLetterEvidenceScope.COVER_LETTER in item.allowed_scopes
        ),
        qa_policy=COVER_LETTER_FACT_QA_AGENT_POLICY,
        qa_policy_version=COVER_LETTER_FACT_QA_POLICY_VERSION,
    )
    try:
        output = await agent.review(context)
    except TimeoutError:
        return _failure(
            command,
            CoverLetterFactQAFailureReason.AGENT_TIMEOUT,
            retryable=True,
            result_binding=binding,
        )
    except CoverLetterFactQAAgentUnavailableError:
        return _failure(
            command,
            CoverLetterFactQAFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            result_binding=binding,
        )
    except Exception:
        return _failure(
            command,
            CoverLetterFactQAFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            result_binding=binding,
        )
    if not isinstance(output, CoverLetterFactQAAgentOutput):
        return _needs_human(
            command,
            result_binding=binding,
            detail="the Agent did not return a typed structured result.",
        )
    if output.verdict is CoverLetterFactQAAgentVerdict.UNCERTAIN:
        return _needs_human(
            command,
            result_binding=binding,
            detail="the Agent could not reach a reliable verdict.",
        )

    try:
        agent_findings = _agent_findings(
            output=output, draft=draft, snapshot=snapshot, job=job
        )
    except _AgentOutputRejected as rejection:
        return _needs_human(
            command, result_binding=binding, detail=f"{rejection}."
        )
    except (AttributeError, TypeError, ValueError):
        return _needs_human(
            command,
            result_binding=binding,
            detail="the Agent output could not be safely validated.",
        )

    final_verdict = (
        CoverLetterFactQAVerdict.BLOCKED
        if output.verdict is CoverLetterFactQAAgentVerdict.BLOCKED
        else CoverLetterFactQAVerdict.PASSED
    )
    return await _persist(
        verdict=final_verdict, findings=deterministic + agent_findings
    )


_COVER_LETTER_FACT_QA_FAILURE_REASON_MAP = {
    reason: CoverLetterFactQAStopReason[reason.name]
    for reason in CoverLetterFactQAFailureReason
}


def cover_letter_fact_qa_public_result(
    result: RunCoverLetterFactQAResult,
) -> PublicPreparationStageResult:
    """Adapt every authoritative P2b2c outcome to stage-result v2."""

    if not isinstance(result, RunCoverLetterFactQAResult):
        raise TypeError("result must be a cover-letter Fact QA result")
    stage = ApplicationPreparationStage.COVER_LETTER_FACT_QA
    if result.status in {
        CoverLetterFactQAStatus.CREATED,
        CoverLetterFactQAStatus.UNCHANGED,
    }:
        if result.result is None:
            raise ValueError("successful Fact QA has no result")
        constructor = (
            PublicPreparationStageResult.completed
            if result.status is CoverLetterFactQAStatus.CREATED
            else PublicPreparationStageResult.unchanged
        )
        return constructor(
            stage=stage,
            result_id=result.result.result_id,
            result_content_hash=result.result.result_content_hash,
            outputs={
                "cover_letter_fact_qa_result_id": result.result.result_id
            },
        )
    if result.status is CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM:
        reason = CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM
        outcome = PreparationStageOutcome.DEFERRED
    else:
        if result.reason_code is None:
            raise ValueError("stopped Fact QA has no authoritative reason")
        try:
            reason = _COVER_LETTER_FACT_QA_FAILURE_REASON_MAP[
                result.reason_code
            ]
        except KeyError as error:
            raise ValueError(
                "unmapped cover-letter Fact QA stop reason"
            ) from error
        outcome = (
            PreparationStageOutcome.DEFERRED
            if result.status
            is CoverLetterFactQAStatus.DEFERRED_NEEDS_HUMAN
            else PreparationStageOutcome.FAILED
        )
    stop_reason = PreparationStopReasonEnvelope(
        stage=stage,
        code=reason,
        contract_version=COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        outcome=outcome,
        upstream_lineage_id=result.result_binding or None,
    )
    constructor = (
        PublicPreparationStageResult.deferred
        if outcome is PreparationStageOutcome.DEFERRED
        else PublicPreparationStageResult.failed
    )
    return constructor(
        stage=stage,
        stop_reason=stop_reason,
        result_id=result.result.result_id if result.result is not None else None,
        result_content_hash=(
            result.result.result_content_hash
            if result.result is not None
            else None
        ),
        outputs=(
            {"cover_letter_fact_qa_result_id": result.result.result_id}
            if result.result is not None
            else None
        ),
        retryable=result.retryable,
        human_attention_required=(
            result.status
            in {
                CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM,
                CoverLetterFactQAStatus.DEFERRED_NEEDS_HUMAN,
            }
        ),
    )


__all__ = [
    "AGENT_ELIGIBLE_FINDING_TYPES",
    "COVER_LETTER_FACT_QA_AGENT_POLICY",
    "COVER_LETTER_FACT_QA_CONTRACT_VERSION",
    "COVER_LETTER_FACT_QA_POLICY_VERSION",
    "CoverLetterFactQAAgentContext",
    "CoverLetterFactQAAgentMetadata",
    "CoverLetterFactQAAgentOutput",
    "CoverLetterFactQAAgentPort",
    "CoverLetterFactQAAgentUnavailableError",
    "CoverLetterFactQAAgentVerdict",
    "CoverLetterFactQAEvidenceView",
    "CoverLetterFactQAFailureReason",
    "CoverLetterFactQAFinding",
    "CoverLetterFactQAFindingProposal",
    "CoverLetterFactQAFindingSeverity",
    "CoverLetterFactQAFindingSource",
    "CoverLetterFactQAFindingType",
    "CoverLetterFactQAParagraphView",
    "CoverLetterFactQAReadResult",
    "CoverLetterFactQAReadStatus",
    "CoverLetterFactQARepository",
    "CoverLetterFactQAResult",
    "CoverLetterFactQAStatus",
    "CoverLetterFactQAVerdict",
    "CoverLetterFactQAWriteResult",
    "CoverLetterFactQAWriteStatus",
    "MAX_CLAIM_TEXT_CHARS",
    "MAX_EXPLANATION_CHARS",
    "PrivateHomeCoverLetterFactQARepository",
    "QUALIFICATION_LIKE_PURPOSES",
    "RunCoverLetterFactQACommand",
    "RunCoverLetterFactQAResult",
    "review_cover_letter_fact_qa",
    "cover_letter_fact_qa_public_result",
]
