"""Automatic, bounded selection of one trusted base resume for one plan."""

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
from .job_discovery import (
    JobPosting,
    JobPostingReadRepository,
    JobPostingRepositoryError,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_candidates import (
    ResumeCandidate,
    ResumeCandidateListStatus,
    ResumeCandidateProvider,
)


RESUME_SELECTION_CONTRACT_VERSION = "resume-selection-v1"
MAX_SELECTION_RATIONALE_CHARS = 2_000
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DECISION_ID_PATTERN = re.compile(r"^resume-selection-[a-f0-9]{64}$")


class ResumeSelectionMethod(str, Enum):
    ONLY_CANDIDATE = "ONLY_CANDIDATE"
    AGENT_SELECTED = "AGENT_SELECTED"


class ResumeSelectionAgentDisposition(str, Enum):
    SELECTED = "SELECTED"
    DEFERRED = "DEFERRED"


class ResumeSelectionDecisionWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeSelectionDecisionReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeSelectionStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_NO_RESUME = "DEFERRED_NO_RESUME"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class ResumeSelectionFailureReason(str, Enum):
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
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    AGENT_SELECTION_UNSAFE = "AGENT_SELECTION_UNSAFE"
    DECISION_PERSISTENCE_FAILED = "DECISION_PERSISTENCE_FAILED"
    DECISION_INTEGRITY_FAILURE = "DECISION_INTEGRITY_FAILURE"


class ResumeSelectionAgentUnavailableError(RuntimeError):
    """Raised when the bounded selection Agent cannot return an output."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the resume-selection contract")
    return cleaned


def _optional_text(name: str, value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _clean_text(name, value, maximum=maximum)


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
        raise ValueError("selected_at is invalid")
    return _require_aware(
        "selected_at",
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
class ResumeSelectionAgentMetadata:
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
class ResumeSelectionCandidateProjection:
    resume_id: str
    candidate_version: str
    artifact_sha256: str
    display_name: str
    selection_safe_summary: str
    selection_safe_summary_sha256: str

    @classmethod
    def from_candidate(
        cls,
        candidate: ResumeCandidate,
    ) -> "ResumeSelectionCandidateProjection":
        if not isinstance(candidate, ResumeCandidate):
            raise TypeError("candidate must be a ResumeCandidate")
        return cls(
            resume_id=candidate.resume_id,
            candidate_version=candidate.contract_version,
            artifact_sha256=candidate.artifact_sha256,
            display_name=candidate.display_name,
            selection_safe_summary=candidate.selection_safe_summary,
            selection_safe_summary_sha256=(
                candidate.selection_safe_summary_sha256
            ),
        )

    def __post_init__(self) -> None:
        _clean_text("resume_id", self.resume_id, maximum=160)
        _clean_text(
            "candidate_version",
            self.candidate_version,
            maximum=80,
        )
        _require_hash("artifact_sha256", self.artifact_sha256)
        _clean_text("display_name", self.display_name, maximum=240)
        _clean_text(
            "selection_safe_summary",
            self.selection_safe_summary,
            maximum=20_000,
        )
        _require_hash(
            "selection_safe_summary_sha256",
            self.selection_safe_summary_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resume_id": self.resume_id,
            "candidate_version": self.candidate_version,
            "artifact_sha256": self.artifact_sha256,
            "display_name": self.display_name,
            "selection_safe_summary": self.selection_safe_summary,
            "selection_safe_summary_sha256": (
                self.selection_safe_summary_sha256
            ),
        }


@dataclass(frozen=True, slots=True)
class ResumeSelectionJobContext:
    job_id: str
    revision: int
    content_hash: str
    company: str
    title: str
    description: str
    location: str
    work_mode: str
    posted_at: str | None
    source_platform: str

    @classmethod
    def from_job(cls, job: JobPosting) -> "ResumeSelectionJobContext":
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
            posted_at=job.posted_at,
            source_platform=job.source_platform,
        )


@dataclass(frozen=True, slots=True)
class ResumeSelectionContext:
    subject_id: str
    application_plan_id: str
    job: ResumeSelectionJobContext
    candidates: tuple[ResumeSelectionCandidateProjection, ...]
    user_preparation_instructions: str | None


@dataclass(frozen=True, slots=True)
class ResumeSelectionAgentOutput:
    disposition: ResumeSelectionAgentDisposition
    selected_resume_id: str | None
    selected_candidate_version: str | None
    selected_artifact_sha256: str | None
    rationale: str

    def __post_init__(self) -> None:
        disposition = ResumeSelectionAgentDisposition(self.disposition)
        object.__setattr__(self, "disposition", disposition)
        rationale = _clean_text(
            "rationale",
            self.rationale,
            maximum=MAX_SELECTION_RATIONALE_CHARS,
        )
        object.__setattr__(self, "rationale", rationale)
        if disposition is ResumeSelectionAgentDisposition.SELECTED:
            _clean_text(
                "selected_resume_id",
                self.selected_resume_id,
                maximum=160,
            )
            _clean_text(
                "selected_candidate_version",
                self.selected_candidate_version,
                maximum=80,
            )
            _require_hash(
                "selected_artifact_sha256",
                self.selected_artifact_sha256,
            )
        elif any(
            value is not None
            for value in (
                self.selected_resume_id,
                self.selected_candidate_version,
                self.selected_artifact_sha256,
            )
        ):
            raise ValueError("deferred Agent output cannot select a resume")


@runtime_checkable
class ResumeSelectionAgentPort(Protocol):
    async def evaluate(
        self,
        context: ResumeSelectionContext,
    ) -> ResumeSelectionAgentOutput:
        """Select at most one supplied candidate without tools or file access."""


def resume_candidate_set_hash(
    candidates: tuple[ResumeCandidate, ...],
) -> str:
    projections = tuple(
        sorted(
            (
                ResumeSelectionCandidateProjection.from_candidate(item)
                for item in candidates
            ),
            key=lambda item: item.resume_id,
        )
    )
    return _canonical_hash(
        {"candidates": [item.to_dict() for item in projections]}
    )


def _selection_binding(
    *,
    plan: ApplicationPlan,
    job: JobPosting,
    candidate_set_hash: str,
    metadata: ResumeSelectionAgentMetadata,
) -> str:
    return _canonical_hash(
        {
            "application_plan_contract_version": plan.contract_version,
            "application_plan_id": plan.plan_id,
            "candidate_set_hash": candidate_set_hash,
            "job_content_hash": job.content_hash,
            "job_id": job.job_id,
            "job_revision": job.revision,
            "resume_selection_agent_version": metadata.agent_version,
            "resume_selection_contract_version": (
                RESUME_SELECTION_CONTRACT_VERSION
            ),
            "resume_selection_model_id": metadata.model_id,
            "resume_selection_prompt_version": metadata.prompt_version,
            "subject_id": plan.subject_id,
            "user_preparation_instructions_hash": (
                plan.user_preparation_instructions_hash
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class ResumeSelectionDecision:
    decision_id: str
    contract_version: str
    selection_binding: str
    decision_content_hash: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    source_resume_id: str
    source_candidate_version: str
    source_artifact_sha256: str
    candidate_set_hash: str
    selection_method: ResumeSelectionMethod
    rationale: str
    agent_version: str
    prompt_version: str
    model_id: str
    selected_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version",
            self.contract_version,
            maximum=80,
        )
        if contract != RESUME_SELECTION_CONTRACT_VERSION:
            raise ValueError("ResumeSelectionDecision version is unsupported")
        binding = _require_hash("selection_binding", self.selection_binding)
        if (
            not isinstance(self.decision_id, str)
            or _DECISION_ID_PATTERN.fullmatch(self.decision_id) is None
            or self.decision_id != f"resume-selection-{binding}"
        ):
            raise ValueError("ResumeSelectionDecision identity is invalid")
        content_hash = _require_hash(
            "decision_content_hash",
            self.decision_content_hash,
        )
        _clean_text("subject_id", self.subject_id, maximum=160)
        _clean_text(
            "application_plan_id",
            self.application_plan_id,
            maximum=160,
        )
        _clean_text("job_id", self.job_id, maximum=160)
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be a positive integer")
        _require_hash("job_content_hash", self.job_content_hash)
        _clean_text("source_resume_id", self.source_resume_id, maximum=160)
        _clean_text(
            "source_candidate_version",
            self.source_candidate_version,
            maximum=80,
        )
        _require_hash("source_artifact_sha256", self.source_artifact_sha256)
        _require_hash("candidate_set_hash", self.candidate_set_hash)
        object.__setattr__(
            self,
            "selection_method",
            ResumeSelectionMethod(self.selection_method),
        )
        object.__setattr__(
            self,
            "rationale",
            _clean_text(
                "rationale",
                self.rationale,
                maximum=MAX_SELECTION_RATIONALE_CHARS,
            ),
        )
        _clean_text("agent_version", self.agent_version, maximum=80)
        _clean_text("prompt_version", self.prompt_version, maximum=80)
        _clean_text("model_id", self.model_id, maximum=160)
        object.__setattr__(
            self,
            "selected_at",
            _require_aware("selected_at", self.selected_at),
        )
        if content_hash != _canonical_hash(self._content_payload()):
            raise ValueError("ResumeSelectionDecision content hash is invalid")

    def _content_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "contract_version": self.contract_version,
            "selection_binding": self.selection_binding,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "source_resume_id": self.source_resume_id,
            "source_candidate_version": self.source_candidate_version,
            "source_artifact_sha256": self.source_artifact_sha256,
            "candidate_set_hash": self.candidate_set_hash,
            "selection_method": self.selection_method.value,
            "rationale": self.rationale,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "contract_version": self.contract_version,
            "selection_binding": self.selection_binding,
            "decision_content_hash": self.decision_content_hash,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "source_resume_id": self.source_resume_id,
            "source_candidate_version": self.source_candidate_version,
            "source_artifact_sha256": self.source_artifact_sha256,
            "candidate_set_hash": self.candidate_set_hash,
            "selection_method": self.selection_method.value,
            "rationale": self.rationale,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "selected_at": _rfc3339(self.selected_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeSelectionDecisionWriteResult:
    status: ResumeSelectionDecisionWriteStatus
    decision: ResumeSelectionDecision | None
    reason_code: ResumeSelectionFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ResumeSelectionDecisionWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeSelectionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ResumeSelectionDecisionWriteStatus.CREATED,
            ResumeSelectionDecisionWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.decision, ResumeSelectionDecision)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful selection write is invalid")
        elif self.decision is not None or self.reason_code is None:
            raise ValueError("failed selection write is invalid")


@dataclass(frozen=True, slots=True)
class ResumeSelectionDecisionReadResult:
    status: ResumeSelectionDecisionReadStatus
    decision: ResumeSelectionDecision | None
    reason_code: ResumeSelectionFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeSelectionDecisionReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeSelectionFailureReason(self.reason_code),
            )
        if status is ResumeSelectionDecisionReadStatus.FOUND:
            if (
                not isinstance(self.decision, ResumeSelectionDecision)
                or self.reason_code is not None
            ):
                raise ValueError("found selection decision is invalid")
        elif status is ResumeSelectionDecisionReadStatus.NOT_FOUND:
            if self.decision is not None or self.reason_code is not None:
                raise ValueError("not-found selection decision is invalid")
        elif (
            self.decision is not None
            or self.reason_code
            is not ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure selection read is invalid")


@runtime_checkable
class ResumeSelectionDecisionRepository(Protocol):
    def find_current_for_plan(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
    ) -> ResumeSelectionDecisionReadResult:
        """Return the latest immutable selection recorded for one plan."""

    def find_completed_by_binding(
        self,
        *,
        subject_id: str,
        selection_binding: str,
    ) -> ResumeSelectionDecisionReadResult:
        """Find a completed immutable selection before any Agent call."""

    def get(
        self,
        *,
        subject_id: str,
        decision_id: str,
    ) -> ResumeSelectionDecisionReadResult:
        """Read one subject-owned immutable selection decision."""

    def save(
        self,
        decision: ResumeSelectionDecision,
    ) -> ResumeSelectionDecisionWriteResult:
        """Persist one immutable selection decision."""


def _decision_from_dict(value: Any) -> ResumeSelectionDecision:
    expected = {
        "decision_id",
        "contract_version",
        "selection_binding",
        "decision_content_hash",
        "subject_id",
        "application_plan_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "source_resume_id",
        "source_candidate_version",
        "source_artifact_sha256",
        "candidate_set_hash",
        "selection_method",
        "rationale",
        "agent_version",
        "prompt_version",
        "model_id",
        "selected_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted ResumeSelectionDecision fields are invalid")
    return ResumeSelectionDecision(
        decision_id=value["decision_id"],
        contract_version=value["contract_version"],
        selection_binding=value["selection_binding"],
        decision_content_hash=value["decision_content_hash"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        source_resume_id=value["source_resume_id"],
        source_candidate_version=value["source_candidate_version"],
        source_artifact_sha256=value["source_artifact_sha256"],
        candidate_set_hash=value["candidate_set_hash"],
        selection_method=ResumeSelectionMethod(value["selection_method"]),
        rationale=value["rationale"],
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        selected_at=_parse_timestamp(value["selected_at"]),
    )


def _semantic_content(decision: ResumeSelectionDecision) -> dict[str, Any]:
    value = decision.to_dict()
    value.pop("selected_at")
    return value


class PrivateHomeResumeSelectionDecisionRepository:
    """Subject-scoped immutable decisions stored below Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _subject_directory(self, subject_id: str) -> Path:
        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        return (
            self._home.paths.resume_selection_decisions
            / _subject_storage_key(cleaned)
        )

    def _path(self, subject_id: str, decision_id: str) -> Path:
        if (
            not isinstance(decision_id, str)
            or _DECISION_ID_PATTERN.fullmatch(decision_id) is None
        ):
            raise ValueError("decision_id is invalid")
        return self._subject_directory(subject_id) / f"{decision_id}.json"

    def get(
        self,
        *,
        subject_id: str,
        decision_id: str,
    ) -> ResumeSelectionDecisionReadResult:
        path = self._path(subject_id, decision_id)
        with self._lock:
            if not path.exists():
                return ResumeSelectionDecisionReadResult(
                    status=ResumeSelectionDecisionReadStatus.NOT_FOUND,
                    decision=None,
                )
            if path.is_symlink() or not path.is_file():
                return ResumeSelectionDecisionReadResult(
                    status=(
                        ResumeSelectionDecisionReadStatus.INTEGRITY_FAILURE
                    ),
                    decision=None,
                    reason_code=(
                        ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
                    ),
                )
            try:
                decision = _decision_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ResumeSelectionDecisionReadResult(
                    status=(
                        ResumeSelectionDecisionReadStatus.INTEGRITY_FAILURE
                    ),
                    decision=None,
                    reason_code=(
                        ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
                    ),
                )
            if (
                decision.subject_id != subject_id.strip()
                or decision.decision_id != decision_id
                or path.name != f"{decision.decision_id}.json"
            ):
                return ResumeSelectionDecisionReadResult(
                    status=(
                        ResumeSelectionDecisionReadStatus.INTEGRITY_FAILURE
                    ),
                    decision=None,
                    reason_code=(
                        ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
                    ),
                )
            return ResumeSelectionDecisionReadResult(
                status=ResumeSelectionDecisionReadStatus.FOUND,
                decision=decision,
            )

    def find_completed_by_binding(
        self,
        *,
        subject_id: str,
        selection_binding: str,
    ) -> ResumeSelectionDecisionReadResult:
        binding = _require_hash("selection_binding", selection_binding)
        return self.get(
            subject_id=subject_id,
            decision_id=f"resume-selection-{binding}",
        )

    def find_current_for_plan(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
    ) -> ResumeSelectionDecisionReadResult:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        plan_id = _clean_text(
            "application_plan_id",
            application_plan_id,
            maximum=160,
        )
        directory = self._subject_directory(subject)
        if not directory.exists():
            return ResumeSelectionDecisionReadResult(
                status=ResumeSelectionDecisionReadStatus.NOT_FOUND,
                decision=None,
            )
        if directory.is_symlink() or not directory.is_dir():
            return ResumeSelectionDecisionReadResult(
                status=ResumeSelectionDecisionReadStatus.INTEGRITY_FAILURE,
                decision=None,
                reason_code=(
                    ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
                ),
            )
        try:
            paths = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
        except OSError:
            return ResumeSelectionDecisionReadResult(
                status=ResumeSelectionDecisionReadStatus.INTEGRITY_FAILURE,
                decision=None,
                reason_code=(
                    ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
                ),
            )
        matches: list[ResumeSelectionDecision] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or _DECISION_ID_PATTERN.fullmatch(path.stem) is None
            ):
                return ResumeSelectionDecisionReadResult(
                    status=ResumeSelectionDecisionReadStatus.INTEGRITY_FAILURE,
                    decision=None,
                    reason_code=(
                        ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
                    ),
                )
            result = self.get(subject_id=subject, decision_id=path.stem)
            if (
                result.status is not ResumeSelectionDecisionReadStatus.FOUND
                or result.decision is None
            ):
                return ResumeSelectionDecisionReadResult(
                    status=ResumeSelectionDecisionReadStatus.INTEGRITY_FAILURE,
                    decision=None,
                    reason_code=(
                        ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
                    ),
                )
            if result.decision.application_plan_id == plan_id:
                matches.append(result.decision)
        if not matches:
            return ResumeSelectionDecisionReadResult(
                status=ResumeSelectionDecisionReadStatus.NOT_FOUND,
                decision=None,
            )
        current = max(
            matches,
            key=lambda item: (
                item.selected_at.astimezone(timezone.utc),
                item.decision_id,
            ),
        )
        return ResumeSelectionDecisionReadResult(
            status=ResumeSelectionDecisionReadStatus.FOUND,
            decision=current,
        )

    def save(
        self,
        decision: ResumeSelectionDecision,
    ) -> ResumeSelectionDecisionWriteResult:
        if not isinstance(decision, ResumeSelectionDecision):
            raise TypeError("decision must be a ResumeSelectionDecision")
        path = self._path(decision.subject_id, decision.decision_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            decision.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ResumeSelectionDecisionWriteResult(
                    status=ResumeSelectionDecisionWriteStatus.FAILED,
                    decision=None,
                    reason_code=(
                        ResumeSelectionFailureReason.DECISION_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return ResumeSelectionDecisionWriteResult(
                    status=ResumeSelectionDecisionWriteStatus.CREATED,
                    decision=decision,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=decision.subject_id,
                decision_id=decision.decision_id,
            )
            if (
                existing.status is ResumeSelectionDecisionReadStatus.FOUND
                and existing.decision is not None
                and _semantic_content(existing.decision)
                == _semantic_content(decision)
            ):
                return ResumeSelectionDecisionWriteResult(
                    status=ResumeSelectionDecisionWriteStatus.UNCHANGED,
                    decision=existing.decision,
                    reason_code=None,
                    retryable=False,
                )
            return ResumeSelectionDecisionWriteResult(
                status=ResumeSelectionDecisionWriteStatus.FAILED,
                decision=None,
                reason_code=(
                    ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class SelectBaseResumeCommand:
    subject_id: str
    application_plan_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class SelectBaseResumeResult:
    status: ResumeSelectionStatus
    reason_code: ResumeSelectionFailureReason | None
    retryable: bool
    subject_id: str
    application_plan_id: str
    job_id: str | None
    candidate_set_hash: str | None
    selection_binding: str | None
    decision: ResumeSelectionDecision | None
    write_result: ResumeSelectionDecisionWriteResult | None
    message: str

    def __post_init__(self) -> None:
        status = ResumeSelectionStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeSelectionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            ResumeSelectionStatus.CREATED,
            ResumeSelectionStatus.UNCHANGED,
        }:
            expected = ResumeSelectionDecisionWriteStatus(status.value)
            if (
                self.reason_code is not None
                or self.retryable
                or not isinstance(self.decision, ResumeSelectionDecision)
                or not isinstance(
                    self.write_result,
                    ResumeSelectionDecisionWriteResult,
                )
                or self.write_result.status is not expected
                or self.write_result.decision != self.decision
                or not self.job_id
                or not self.candidate_set_hash
                or not self.selection_binding
            ):
                raise ValueError("successful resume selection result is invalid")
        elif status in {
            ResumeSelectionStatus.DEFERRED_NO_RESUME,
            ResumeSelectionStatus.DEFERRED_NEEDS_HUMAN,
        }:
            expected_reason = (
                None
                if status is ResumeSelectionStatus.DEFERRED_NO_RESUME
                else ResumeSelectionFailureReason.AGENT_SELECTION_UNSAFE
            )
            if (
                self.reason_code is not expected_reason
                or self.retryable
                or self.decision is not None
                or self.write_result is not None
                or not self.job_id
                or not self.candidate_set_hash
                or not self.selection_binding
            ):
                raise ValueError("deferred resume selection result is invalid")
        elif self.decision is not None:
            raise ValueError("failed resume selection cannot include a decision")


def _failed(
    *,
    command: SelectBaseResumeCommand,
    reason: ResumeSelectionFailureReason,
    job_id: str | None = None,
    candidate_set_hash: str | None = None,
    selection_binding: str | None = None,
    retryable: bool = False,
    write_result: ResumeSelectionDecisionWriteResult | None = None,
) -> SelectBaseResumeResult:
    return SelectBaseResumeResult(
        status=ResumeSelectionStatus.FAILED,
        reason_code=reason,
        retryable=retryable,
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
        job_id=job_id,
        candidate_set_hash=candidate_set_hash,
        selection_binding=selection_binding,
        decision=None,
        write_result=write_result,
        message=f"Base resume selection failed: {reason.value}.",
    )


async def select_base_resume(
    command: SelectBaseResumeCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    job_repository: JobPostingReadRepository,
    candidate_provider: ResumeCandidateProvider,
    agent: ResumeSelectionAgentPort,
    metadata: ResumeSelectionAgentMetadata,
    decision_repository: ResumeSelectionDecisionRepository,
) -> SelectBaseResumeResult:
    """Select one trusted base resume without altering any artifact."""

    try:
        subject_id = _clean_text(
            "subject_id",
            command.subject_id,
            maximum=160,
        )
        plan_id = _clean_text(
            "application_plan_id",
            command.application_plan_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
        if not isinstance(metadata, ResumeSelectionAgentMetadata):
            raise TypeError("metadata must be ResumeSelectionAgentMetadata")
    except (AttributeError, TypeError, ValueError):
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.INVALID_REQUEST,
        )

    try:
        plan_result = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failed(
            command=command,
            reason=(
                ResumeSelectionFailureReason.APPLICATION_PLAN_INTEGRITY_FAILURE
            ),
        )
    if plan_result.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.APPLICATION_PLAN_NOT_FOUND,
        )
    if (
        plan_result.status is not ApplicationPlanReadStatus.FOUND
        or plan_result.plan is None
    ):
        return _failed(
            command=command,
            reason=(
                ResumeSelectionFailureReason.APPLICATION_PLAN_INTEGRITY_FAILURE
            ),
        )
    plan = plan_result.plan
    if plan.subject_id != subject_id:
        return _failed(
            command=command,
            reason=(
                ResumeSelectionFailureReason.APPLICATION_PLAN_SUBJECT_MISMATCH
            ),
            job_id=plan.job_id,
        )

    try:
        job = job_repository.get(plan.job_id)
    except (JobPostingRepositoryError, OSError, RuntimeError, TypeError, ValueError):
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.JOB_READ_FAILED,
            job_id=plan.job_id,
        )
    if job is None:
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.JOB_NOT_FOUND,
            job_id=plan.job_id,
        )
    if (
        not isinstance(job, JobPosting)
        or job.job_id != plan.job_id
        or job.revision != plan.job_revision
        or job.content_hash != plan.job_content_hash
    ):
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.JOB_BINDING_MISMATCH,
            job_id=plan.job_id,
        )

    try:
        candidate_result = candidate_provider.list_selectable(subject_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.CANDIDATE_PROVIDER_FAILED,
            job_id=plan.job_id,
        )
    if (
        candidate_result.status is not ResumeCandidateListStatus.SUCCEEDED
        or candidate_result.subject_id != subject_id
    ):
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.CANDIDATE_PROVIDER_FAILED,
            job_id=plan.job_id,
        )
    candidates = candidate_result.candidates
    candidate_hash = resume_candidate_set_hash(candidates)
    binding = _selection_binding(
        plan=plan,
        job=job,
        candidate_set_hash=candidate_hash,
        metadata=metadata,
    )
    try:
        existing = decision_repository.find_completed_by_binding(
            subject_id=subject_id,
            selection_binding=binding,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failed(
            command=command,
            reason=(
                ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
            ),
            job_id=plan.job_id,
            candidate_set_hash=candidate_hash,
            selection_binding=binding,
        )
    if existing.status is ResumeSelectionDecisionReadStatus.INTEGRITY_FAILURE:
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE,
            job_id=plan.job_id,
            candidate_set_hash=candidate_hash,
            selection_binding=binding,
        )
    if (
        existing.status is ResumeSelectionDecisionReadStatus.FOUND
        and existing.decision is not None
    ):
        write_result = ResumeSelectionDecisionWriteResult(
            status=ResumeSelectionDecisionWriteStatus.UNCHANGED,
            decision=existing.decision,
            reason_code=None,
            retryable=False,
        )
        return SelectBaseResumeResult(
            status=ResumeSelectionStatus.UNCHANGED,
            reason_code=None,
            retryable=False,
            subject_id=subject_id,
            application_plan_id=plan_id,
            job_id=plan.job_id,
            candidate_set_hash=candidate_hash,
            selection_binding=binding,
            decision=existing.decision,
            write_result=write_result,
            message="The existing base resume selection is unchanged.",
        )

    if not candidates:
        return SelectBaseResumeResult(
            status=ResumeSelectionStatus.DEFERRED_NO_RESUME,
            reason_code=None,
            retryable=False,
            subject_id=subject_id,
            application_plan_id=plan_id,
            job_id=plan.job_id,
            candidate_set_hash=candidate_hash,
            selection_binding=binding,
            decision=None,
            write_result=None,
            message="No trusted selectable resume is registered.",
        )

    selected: ResumeCandidate
    method: ResumeSelectionMethod
    rationale: str
    if len(candidates) == 1:
        selected = candidates[0]
        method = ResumeSelectionMethod.ONLY_CANDIDATE
        rationale = "The only trusted selectable resume candidate was selected."
    else:
        projections = tuple(
            sorted(
                (
                    ResumeSelectionCandidateProjection.from_candidate(item)
                    for item in candidates
                ),
                key=lambda item: item.resume_id,
            )
        )
        context = ResumeSelectionContext(
            subject_id=subject_id,
            application_plan_id=plan_id,
            job=ResumeSelectionJobContext.from_job(job),
            candidates=projections,
            user_preparation_instructions=(
                plan.user_preparation_instructions
            ),
        )
        try:
            output = await agent.evaluate(context)
        except TimeoutError:
            return _failed(
                command=command,
                reason=ResumeSelectionFailureReason.AGENT_TIMEOUT,
                retryable=True,
                job_id=plan.job_id,
                candidate_set_hash=candidate_hash,
                selection_binding=binding,
            )
        except ResumeSelectionAgentUnavailableError:
            return _failed(
                command=command,
                reason=ResumeSelectionFailureReason.AGENT_UNAVAILABLE,
                retryable=True,
                job_id=plan.job_id,
                candidate_set_hash=candidate_hash,
                selection_binding=binding,
            )
        except Exception:
            return _failed(
                command=command,
                reason=ResumeSelectionFailureReason.AGENT_UNAVAILABLE,
                retryable=True,
                job_id=plan.job_id,
                candidate_set_hash=candidate_hash,
                selection_binding=binding,
            )
        if not isinstance(output, ResumeSelectionAgentOutput):
            return SelectBaseResumeResult(
                status=ResumeSelectionStatus.DEFERRED_NEEDS_HUMAN,
                reason_code=(
                    ResumeSelectionFailureReason.AGENT_SELECTION_UNSAFE
                ),
                retryable=False,
                subject_id=subject_id,
                application_plan_id=plan_id,
                job_id=plan.job_id,
                candidate_set_hash=candidate_hash,
                selection_binding=binding,
                decision=None,
                write_result=None,
                message="The resume choice needs human review.",
            )
        selected_by_id = {
            item.resume_id: item for item in candidates
        }.get(output.selected_resume_id or "")
        if (
            output.disposition
            is not ResumeSelectionAgentDisposition.SELECTED
            or selected_by_id is None
            or output.selected_candidate_version
            != selected_by_id.contract_version
            or output.selected_artifact_sha256
            != selected_by_id.artifact_sha256
        ):
            return SelectBaseResumeResult(
                status=ResumeSelectionStatus.DEFERRED_NEEDS_HUMAN,
                reason_code=(
                    ResumeSelectionFailureReason.AGENT_SELECTION_UNSAFE
                ),
                retryable=False,
                subject_id=subject_id,
                application_plan_id=plan_id,
                job_id=plan.job_id,
                candidate_set_hash=candidate_hash,
                selection_binding=binding,
                decision=None,
                write_result=None,
                message="The resume choice needs human review.",
            )
        selected = selected_by_id
        method = ResumeSelectionMethod.AGENT_SELECTED
        rationale = output.rationale

    decision_values = {
        "decision_id": f"resume-selection-{binding}",
        "contract_version": RESUME_SELECTION_CONTRACT_VERSION,
        "selection_binding": binding,
        "subject_id": subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": job.job_id,
        "job_revision": job.revision,
        "job_content_hash": job.content_hash,
        "source_resume_id": selected.resume_id,
        "source_candidate_version": selected.contract_version,
        "source_artifact_sha256": selected.artifact_sha256,
        "candidate_set_hash": candidate_hash,
        "selection_method": method.value,
        "rationale": rationale,
        "agent_version": metadata.agent_version,
        "prompt_version": metadata.prompt_version,
        "model_id": metadata.model_id,
    }
    decision = ResumeSelectionDecision(
        decision_content_hash=_canonical_hash(decision_values),
        selected_at=now,
        **decision_values,
    )
    try:
        write_result = decision_repository.save(decision)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failed(
            command=command,
            reason=ResumeSelectionFailureReason.DECISION_PERSISTENCE_FAILED,
            retryable=True,
            job_id=plan.job_id,
            candidate_set_hash=candidate_hash,
            selection_binding=binding,
        )
    if write_result.status is ResumeSelectionDecisionWriteStatus.FAILED:
        return _failed(
            command=command,
            reason=(
                write_result.reason_code
                or ResumeSelectionFailureReason.DECISION_PERSISTENCE_FAILED
            ),
            retryable=write_result.retryable,
            job_id=plan.job_id,
            candidate_set_hash=candidate_hash,
            selection_binding=binding,
            write_result=write_result,
        )
    result_status = ResumeSelectionStatus(write_result.status.value)
    return SelectBaseResumeResult(
        status=result_status,
        reason_code=None,
        retryable=False,
        subject_id=subject_id,
        application_plan_id=plan.plan_id,
        job_id=job.job_id,
        candidate_set_hash=candidate_hash,
        selection_binding=binding,
        decision=write_result.decision,
        write_result=write_result,
        message=(
            "The base resume selection was created."
            if result_status is ResumeSelectionStatus.CREATED
            else "The base resume selection is unchanged."
        ),
    )


__all__ = [
    "RESUME_SELECTION_CONTRACT_VERSION",
    "PrivateHomeResumeSelectionDecisionRepository",
    "ResumeSelectionAgentDisposition",
    "ResumeSelectionAgentMetadata",
    "ResumeSelectionAgentOutput",
    "ResumeSelectionAgentPort",
    "ResumeSelectionAgentUnavailableError",
    "ResumeSelectionCandidateProjection",
    "ResumeSelectionContext",
    "ResumeSelectionDecision",
    "ResumeSelectionDecisionReadResult",
    "ResumeSelectionDecisionReadStatus",
    "ResumeSelectionDecisionRepository",
    "ResumeSelectionDecisionWriteResult",
    "ResumeSelectionDecisionWriteStatus",
    "ResumeSelectionFailureReason",
    "ResumeSelectionJobContext",
    "ResumeSelectionMethod",
    "ResumeSelectionStatus",
    "SelectBaseResumeCommand",
    "SelectBaseResumeResult",
    "resume_candidate_set_hash",
    "select_base_resume",
]
