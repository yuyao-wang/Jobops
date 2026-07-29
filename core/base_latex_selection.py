"""Automatic base LaTeX version selection for one fact-QA-passed resume draft."""

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
from .resume_fact_qa import (
    ResumeFactQAReadStatus,
    ResumeFactQARepository,
    ResumeFactQAResult,
    ResumeFactQAVerdict,
)
from .resume_latex_versions import (
    ResumeLatexSourceKind,
    ResumeLatexVersion,
    ResumeLatexVersionListStatus,
    ResumeLatexVersionProvider,
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


BASE_LATEX_SELECTION_CONTRACT_VERSION = "base-latex-selection-v1"
MAX_BASE_LATEX_RATIONALE_CHARS = 4_000

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DECISION_ID_PATTERN = re.compile(r"^base-latex-selection-[a-f0-9]{64}$")
_VERSION_ID_PATTERN = re.compile(r"^resume-latex-version-[a-f0-9]{64}$")
_FAMILY_ID_PATTERN = re.compile(r"^resume-latex-family-[a-f0-9]{64}$")
_VERSION_TOKEN = re.compile(r"resume-latex-version-[a-f0-9]{64}")
_FAMILY_TOKEN = re.compile(r"resume-latex-family-[a-f0-9]{64}")


class BaseLatexSelectionKind(str, Enum):
    EXISTING_VERSION = "EXISTING_VERSION"
    MANAGED_TEMPLATE_FALLBACK = "MANAGED_TEMPLATE_FALLBACK"


class BaseLatexSelectionMethod(str, Enum):
    ONLY_CANDIDATE = "ONLY_CANDIDATE"
    EXACT_SOURCE_RESUME_MATCH = "EXACT_SOURCE_RESUME_MATCH"
    USER_REQUIRED_VERSION = "USER_REQUIRED_VERSION"
    AGENT_SELECTED = "AGENT_SELECTED"
    MANAGED_TEMPLATE_FALLBACK = "MANAGED_TEMPLATE_FALLBACK"


class BaseLatexSelectionAgentDisposition(str, Enum):
    SELECTED = "SELECTED"
    USE_MANAGED_TEMPLATE = "USE_MANAGED_TEMPLATE"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class BaseLatexSelectionWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class BaseLatexSelectionReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class BaseLatexSelectionStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class BaseLatexSelectionFailureReason(str, Enum):
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
    RESUME_SELECTION_NOT_FOUND = "RESUME_SELECTION_NOT_FOUND"
    RESUME_SELECTION_INTEGRITY_FAILURE = (
        "RESUME_SELECTION_INTEGRITY_FAILURE"
    )
    RESUME_SELECTION_BINDING_MISMATCH = (
        "RESUME_SELECTION_BINDING_MISMATCH"
    )
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_READ_FAILED = "JOB_READ_FAILED"
    JOB_BINDING_MISMATCH = "JOB_BINDING_MISMATCH"
    LATEX_PROVIDER_FAILED = "LATEX_PROVIDER_FAILED"
    LATEX_PROVENANCE_INVALID = "LATEX_PROVENANCE_INVALID"
    AGENT_TIMEOUT = "AGENT_TIMEOUT"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    USER_REQUIREMENT_UNSATISFIABLE = "USER_REQUIREMENT_UNSATISFIABLE"
    DECISION_PERSISTENCE_FAILED = "DECISION_PERSISTENCE_FAILED"
    DECISION_INTEGRITY_FAILURE = "DECISION_INTEGRITY_FAILURE"


class BaseLatexSelectionAgentUnavailableError(RuntimeError):
    """Raised when the bounded base-LaTeX selection Agent cannot answer."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the base-LaTeX contract")
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
class BaseLatexSelectionAgentMetadata:
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
class BaseLatexCandidateView:
    """Version metadata only: the LaTeX source itself is never exposed."""

    latex_version_id: str
    source_kind: ResumeLatexSourceKind
    source_sha256: str
    root_family_id: str
    parent_version_id: str | None
    template_id: str | None
    source_resume_id: str | None
    labels: tuple[str, ...]
    has_passed_fact_qa: bool

    @classmethod
    def from_version(
        cls, version: ResumeLatexVersion, *, has_passed_fact_qa: bool
    ) -> "BaseLatexCandidateView":
        if not isinstance(version, ResumeLatexVersion):
            raise TypeError("version must be a ResumeLatexVersion")
        return cls(
            latex_version_id=version.latex_version_id,
            source_kind=version.source_kind,
            source_sha256=version.source_sha256,
            root_family_id=version.root_family_id,
            parent_version_id=version.parent_version_id,
            template_id=version.template_id,
            source_resume_id=version.source_resume_id,
            labels=version.labels,
            has_passed_fact_qa=has_passed_fact_qa,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_passed_fact_qa": self.has_passed_fact_qa,
            "labels": list(self.labels),
            "latex_version_id": self.latex_version_id,
            "parent_version_id": self.parent_version_id,
            "root_family_id": self.root_family_id,
            "source_kind": self.source_kind.value,
            "source_resume_id": self.source_resume_id,
            "source_sha256": self.source_sha256,
            "template_id": self.template_id,
        }


@dataclass(frozen=True, slots=True)
class BaseLatexSelectionJobContext:
    job_id: str
    revision: int
    content_hash: str
    company: str
    title: str
    description: str

    @classmethod
    def from_job(cls, job: JobPosting) -> "BaseLatexSelectionJobContext":
        if not isinstance(job, JobPosting):
            raise TypeError("job must be a JobPosting")
        return cls(
            job_id=job.job_id,
            revision=job.revision,
            content_hash=job.content_hash,
            company=job.company,
            title=job.title,
            description=job.description,
        )


@dataclass(frozen=True, slots=True)
class BaseLatexSelectionContext:
    subject_id: str
    application_plan_id: str
    job: BaseLatexSelectionJobContext
    candidates: tuple[BaseLatexCandidateView, ...]
    user_preparation_instructions: str | None


@dataclass(frozen=True, slots=True)
class BaseLatexSelectionAgentOutput:
    disposition: BaseLatexSelectionAgentDisposition
    selected_latex_version_id: str | None
    rationale: str

    def __post_init__(self) -> None:
        disposition = BaseLatexSelectionAgentDisposition(self.disposition)
        object.__setattr__(self, "disposition", disposition)
        _clean_text(
            "rationale",
            self.rationale,
            maximum=MAX_BASE_LATEX_RATIONALE_CHARS,
        )
        if disposition is BaseLatexSelectionAgentDisposition.SELECTED:
            _clean_text(
                "selected_latex_version_id",
                self.selected_latex_version_id,
                maximum=160,
            )
        elif self.selected_latex_version_id is not None:
            raise ValueError(
                "only a SELECTED disposition may name a LaTeX version"
            )


@runtime_checkable
class BaseLatexSelectionAgentPort(Protocol):
    async def evaluate(
        self,
        context: BaseLatexSelectionContext,
    ) -> BaseLatexSelectionAgentOutput:
        """Choose one supplied candidate or the managed template, tool-free."""


def base_latex_candidate_set_hash(
    candidates: tuple[BaseLatexCandidateView, ...],
) -> str:
    ordered = tuple(
        sorted(candidates, key=lambda item: item.latex_version_id)
    )
    return _canonical_hash(
        {"candidates": [item.to_dict() for item in ordered]}
    )


def _selection_binding(
    *,
    plan: ApplicationPlan,
    draft: TailoredResumeDraft,
    qa_result: ResumeFactQAResult,
    job: JobPosting,
    selection: ResumeSelectionDecision,
    candidate_set_hash: str,
    metadata: BaseLatexSelectionAgentMetadata,
) -> str:
    return _canonical_hash(
        {
            "application_plan_id": plan.plan_id,
            "base_latex_selection_agent_version": metadata.agent_version,
            "base_latex_selection_contract_version": (
                BASE_LATEX_SELECTION_CONTRACT_VERSION
            ),
            "base_latex_selection_model_id": metadata.model_id,
            "base_latex_selection_prompt_version": metadata.prompt_version,
            "candidate_set_hash": candidate_set_hash,
            "fact_qa_result_hash": qa_result.qa_content_hash,
            "fact_qa_result_id": qa_result.qa_result_id,
            "job_content_hash": job.content_hash,
            "job_id": job.job_id,
            "job_revision": job.revision,
            "resume_selection_decision_id": selection.decision_id,
            "source_resume_id": selection.source_resume_id,
            "subject_id": plan.subject_id,
            "tailored_resume_draft_hash": draft.draft_content_hash,
            "tailored_resume_draft_id": draft.draft_id,
            "user_preparation_instructions_hash": (
                plan.user_preparation_instructions_hash
            ),
        }
    )


@dataclass(frozen=True, slots=True)
class BaseLatexSelectionDecision:
    decision_id: str
    contract_version: str
    selection_binding: str
    subject_id: str
    application_plan_id: str
    tailored_resume_draft_id: str
    tailored_resume_draft_hash: str
    fact_qa_result_id: str
    fact_qa_result_hash: str
    job_id: str
    job_revision: int
    job_content_hash: str
    resume_selection_decision_id: str
    source_resume_id: str
    candidate_set_hash: str
    selection_kind: BaseLatexSelectionKind
    selection_method: BaseLatexSelectionMethod
    selected_latex_version_id: str | None
    selected_latex_source_sha256: str | None
    selected_root_family_id: str | None
    rationale: str
    agent_invoked: bool
    agent_version: str
    prompt_version: str
    model_id: str
    selected_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != BASE_LATEX_SELECTION_CONTRACT_VERSION:
            raise ValueError("base-LaTeX selection contract is unsupported")
        binding = _require_hash("selection_binding", self.selection_binding)
        if (
            not isinstance(self.decision_id, str)
            or _DECISION_ID_PATTERN.fullmatch(self.decision_id) is None
            or self.decision_id != f"base-latex-selection-{binding}"
        ):
            raise ValueError("decision_id does not match its binding")
        _clean_text("subject_id", self.subject_id, maximum=160)
        _clean_text(
            "application_plan_id", self.application_plan_id, maximum=160
        )
        _clean_text(
            "tailored_resume_draft_id",
            self.tailored_resume_draft_id,
            maximum=160,
        )
        _require_hash(
            "tailored_resume_draft_hash", self.tailored_resume_draft_hash
        )
        _clean_text(
            "fact_qa_result_id", self.fact_qa_result_id, maximum=160
        )
        _require_hash("fact_qa_result_hash", self.fact_qa_result_hash)
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
        _require_hash("candidate_set_hash", self.candidate_set_hash)
        kind = BaseLatexSelectionKind(self.selection_kind)
        method = BaseLatexSelectionMethod(self.selection_method)
        object.__setattr__(self, "selection_kind", kind)
        object.__setattr__(self, "selection_method", method)
        version_id = _optional_text(
            "selected_latex_version_id",
            self.selected_latex_version_id,
            maximum=160,
        )
        source_hash = _optional_hash(
            "selected_latex_source_sha256",
            self.selected_latex_source_sha256,
        )
        family_id = _optional_text(
            "selected_root_family_id",
            self.selected_root_family_id,
            maximum=160,
        )
        if kind is BaseLatexSelectionKind.EXISTING_VERSION:
            if (
                version_id is None
                or _VERSION_ID_PATTERN.fullmatch(version_id) is None
                or source_hash is None
                or family_id is None
                or _FAMILY_ID_PATTERN.fullmatch(family_id) is None
                or method is BaseLatexSelectionMethod.MANAGED_TEMPLATE_FALLBACK
            ):
                raise ValueError("existing-version selection is invalid")
        else:
            if (
                version_id is not None
                or source_hash is not None
                or family_id is not None
                or method
                is not BaseLatexSelectionMethod.MANAGED_TEMPLATE_FALLBACK
            ):
                raise ValueError("managed-template selection is invalid")
        _clean_text(
            "rationale",
            self.rationale,
            maximum=MAX_BASE_LATEX_RATIONALE_CHARS,
        )
        if type(self.agent_invoked) is not bool:
            raise TypeError("agent_invoked must be a boolean")
        if (
            method is BaseLatexSelectionMethod.AGENT_SELECTED
            and not self.agent_invoked
        ):
            raise ValueError("an Agent-selected decision requires an Agent call")
        _clean_text("agent_version", self.agent_version, maximum=80)
        _clean_text("prompt_version", self.prompt_version, maximum=80)
        _clean_text("model_id", self.model_id, maximum=160)
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "selected_latex_version_id", version_id)
        object.__setattr__(
            self, "selected_latex_source_sha256", source_hash
        )
        object.__setattr__(self, "selected_root_family_id", family_id)
        _require_aware("selected_at", self.selected_at)

    def content_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "contract_version": self.contract_version,
            "selection_binding": self.selection_binding,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "tailored_resume_draft_id": self.tailored_resume_draft_id,
            "tailored_resume_draft_hash": self.tailored_resume_draft_hash,
            "fact_qa_result_id": self.fact_qa_result_id,
            "fact_qa_result_hash": self.fact_qa_result_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "resume_selection_decision_id": (
                self.resume_selection_decision_id
            ),
            "source_resume_id": self.source_resume_id,
            "candidate_set_hash": self.candidate_set_hash,
            "selection_kind": self.selection_kind.value,
            "selection_method": self.selection_method.value,
            "selected_latex_version_id": self.selected_latex_version_id,
            "selected_latex_source_sha256": (
                self.selected_latex_source_sha256
            ),
            "selected_root_family_id": self.selected_root_family_id,
            "rationale": self.rationale,
            "agent_invoked": self.agent_invoked,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "selected_at": _rfc3339(self.selected_at),
        }


@dataclass(frozen=True, slots=True)
class BaseLatexSelectionWriteResult:
    status: BaseLatexSelectionWriteStatus
    decision: BaseLatexSelectionDecision | None
    reason_code: BaseLatexSelectionFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = BaseLatexSelectionWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                BaseLatexSelectionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            BaseLatexSelectionWriteStatus.CREATED,
            BaseLatexSelectionWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.decision, BaseLatexSelectionDecision)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful selection write is invalid")
        elif self.decision is not None or self.reason_code is None:
            raise ValueError("failed selection write is invalid")


@dataclass(frozen=True, slots=True)
class BaseLatexSelectionReadResult:
    status: BaseLatexSelectionReadStatus
    decision: BaseLatexSelectionDecision | None
    reason_code: BaseLatexSelectionFailureReason | None = None

    def __post_init__(self) -> None:
        status = BaseLatexSelectionReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                BaseLatexSelectionFailureReason(self.reason_code),
            )
        if status is BaseLatexSelectionReadStatus.FOUND:
            if (
                not isinstance(self.decision, BaseLatexSelectionDecision)
                or self.reason_code is not None
            ):
                raise ValueError("found selection read is invalid")
        elif status is BaseLatexSelectionReadStatus.NOT_FOUND:
            if self.decision is not None or self.reason_code is not None:
                raise ValueError("not-found selection read is invalid")
        elif (
            self.decision is not None
            or self.reason_code
            is not BaseLatexSelectionFailureReason.DECISION_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure selection read is invalid")


@runtime_checkable
class BaseLatexSelectionDecisionRepository(Protocol):
    def save(
        self, decision: BaseLatexSelectionDecision
    ) -> BaseLatexSelectionWriteResult:
        """Persist one immutable base-LaTeX selection decision."""

    def get(
        self, *, subject_id: str, decision_id: str
    ) -> BaseLatexSelectionReadResult:
        """Read one subject-owned base-LaTeX selection decision."""


def _decision_from_dict(value: Any) -> BaseLatexSelectionDecision:
    expected = {
        "decision_id",
        "contract_version",
        "selection_binding",
        "subject_id",
        "application_plan_id",
        "tailored_resume_draft_id",
        "tailored_resume_draft_hash",
        "fact_qa_result_id",
        "fact_qa_result_hash",
        "job_id",
        "job_revision",
        "job_content_hash",
        "resume_selection_decision_id",
        "source_resume_id",
        "candidate_set_hash",
        "selection_kind",
        "selection_method",
        "selected_latex_version_id",
        "selected_latex_source_sha256",
        "selected_root_family_id",
        "rationale",
        "agent_invoked",
        "agent_version",
        "prompt_version",
        "model_id",
        "selected_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted BaseLatexSelectionDecision is invalid")
    return BaseLatexSelectionDecision(
        decision_id=value["decision_id"],
        contract_version=value["contract_version"],
        selection_binding=value["selection_binding"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        tailored_resume_draft_id=value["tailored_resume_draft_id"],
        tailored_resume_draft_hash=value["tailored_resume_draft_hash"],
        fact_qa_result_id=value["fact_qa_result_id"],
        fact_qa_result_hash=value["fact_qa_result_hash"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        resume_selection_decision_id=value[
            "resume_selection_decision_id"
        ],
        source_resume_id=value["source_resume_id"],
        candidate_set_hash=value["candidate_set_hash"],
        selection_kind=BaseLatexSelectionKind(value["selection_kind"]),
        selection_method=BaseLatexSelectionMethod(
            value["selection_method"]
        ),
        selected_latex_version_id=value["selected_latex_version_id"],
        selected_latex_source_sha256=value[
            "selected_latex_source_sha256"
        ],
        selected_root_family_id=value["selected_root_family_id"],
        rationale=value["rationale"],
        agent_invoked=value["agent_invoked"],
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        selected_at=_parse_timestamp(value["selected_at"]),
    )


class PrivateHomeBaseLatexSelectionDecisionRepository:
    """Immutable, subject-scoped base-LaTeX selection decisions."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, decision_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(decision_id, str)
            or _DECISION_ID_PATTERN.fullmatch(decision_id) is None
        ):
            raise ValueError("decision_id is invalid")
        return (
            self._home.paths.base_latex_selections
            / _subject_storage_key(subject)
            / f"{decision_id}.json"
        )

    def get(
        self, *, subject_id: str, decision_id: str
    ) -> BaseLatexSelectionReadResult:
        path = self._path(subject_id, decision_id)
        with self._lock:
            if not path.exists():
                return BaseLatexSelectionReadResult(
                    status=BaseLatexSelectionReadStatus.NOT_FOUND,
                    decision=None,
                )
            if path.is_symlink() or not path.is_file():
                return BaseLatexSelectionReadResult(
                    status=BaseLatexSelectionReadStatus.INTEGRITY_FAILURE,
                    decision=None,
                    reason_code=(
                        BaseLatexSelectionFailureReason
                        .DECISION_INTEGRITY_FAILURE
                    ),
                )
            try:
                decision = _decision_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return BaseLatexSelectionReadResult(
                    status=BaseLatexSelectionReadStatus.INTEGRITY_FAILURE,
                    decision=None,
                    reason_code=(
                        BaseLatexSelectionFailureReason
                        .DECISION_INTEGRITY_FAILURE
                    ),
                )
            if (
                decision.subject_id != subject_id.strip()
                or decision.decision_id != decision_id
                or path.name != f"{decision.decision_id}.json"
            ):
                return BaseLatexSelectionReadResult(
                    status=BaseLatexSelectionReadStatus.INTEGRITY_FAILURE,
                    decision=None,
                    reason_code=(
                        BaseLatexSelectionFailureReason
                        .DECISION_INTEGRITY_FAILURE
                    ),
                )
            return BaseLatexSelectionReadResult(
                status=BaseLatexSelectionReadStatus.FOUND,
                decision=decision,
            )

    def save(
        self, decision: BaseLatexSelectionDecision
    ) -> BaseLatexSelectionWriteResult:
        if not isinstance(decision, BaseLatexSelectionDecision):
            raise TypeError("decision must be a BaseLatexSelectionDecision")
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
                return BaseLatexSelectionWriteResult(
                    status=BaseLatexSelectionWriteStatus.FAILED,
                    decision=None,
                    reason_code=(
                        BaseLatexSelectionFailureReason
                        .DECISION_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return BaseLatexSelectionWriteResult(
                    status=BaseLatexSelectionWriteStatus.CREATED,
                    decision=decision,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=decision.subject_id,
                decision_id=decision.decision_id,
            )
            if (
                existing.status is BaseLatexSelectionReadStatus.FOUND
                and existing.decision is not None
                and existing.decision.content_dict()
                == decision.content_dict()
            ):
                return BaseLatexSelectionWriteResult(
                    status=BaseLatexSelectionWriteStatus.UNCHANGED,
                    decision=existing.decision,
                    reason_code=None,
                    retryable=False,
                )
            return BaseLatexSelectionWriteResult(
                status=BaseLatexSelectionWriteStatus.FAILED,
                decision=None,
                reason_code=(
                    BaseLatexSelectionFailureReason
                    .DECISION_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class SelectBaseLatexVersionCommand:
    subject_id: str
    application_plan_id: str
    fact_qa_result_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class SelectBaseLatexVersionResult:
    status: BaseLatexSelectionStatus
    subject_id: str
    application_plan_id: str
    selection_binding: str
    candidate_set_hash: str
    decision: BaseLatexSelectionDecision | None
    write_result: BaseLatexSelectionWriteResult | None
    reason_code: BaseLatexSelectionFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = BaseLatexSelectionStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                BaseLatexSelectionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            BaseLatexSelectionStatus.CREATED,
            BaseLatexSelectionStatus.UNCHANGED,
        }:
            expected = BaseLatexSelectionWriteStatus(status.value)
            if (
                not isinstance(self.decision, BaseLatexSelectionDecision)
                or not isinstance(
                    self.write_result, BaseLatexSelectionWriteResult
                )
                or self.write_result.status is not expected
                or self.write_result.decision != self.decision
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful selection result is invalid")
        elif status is BaseLatexSelectionStatus.DEFERRED_NEEDS_HUMAN:
            if (
                self.decision is not None
                or self.write_result is not None
                or self.reason_code
                is not BaseLatexSelectionFailureReason
                .USER_REQUIREMENT_UNSATISFIABLE
                or self.retryable
            ):
                raise ValueError("deferred selection result is invalid")
        elif self.decision is not None or self.reason_code is None:
            raise ValueError("failed selection result is invalid")


def _failure(
    command: SelectBaseLatexVersionCommand,
    reason: BaseLatexSelectionFailureReason,
    *,
    retryable: bool = False,
    selection_binding: str = "",
    candidate_set_hash: str = "",
    write_result: BaseLatexSelectionWriteResult | None = None,
) -> SelectBaseLatexVersionResult:
    return SelectBaseLatexVersionResult(
        status=BaseLatexSelectionStatus.FAILED,
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
        selection_binding=selection_binding,
        candidate_set_hash=candidate_set_hash,
        decision=None,
        write_result=write_result,
        reason_code=reason,
        retryable=retryable,
        message=f"Base LaTeX selection failed: {reason.value}.",
    )


def _required_identifiers(
    instructions: str | None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Read literal version/family IDs only; no natural-language parsing."""

    if not instructions:
        return frozenset(), frozenset()
    return (
        frozenset(_VERSION_TOKEN.findall(instructions)),
        frozenset(_FAMILY_TOKEN.findall(instructions)),
    )


async def select_base_latex_version(
    command: SelectBaseLatexVersionCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    fact_qa_repository: ResumeFactQARepository,
    draft_repository: TailoredResumeDraftRepository,
    selection_repository: ResumeSelectionDecisionRepository,
    job_repository: JobPostingReadRepository,
    latex_version_provider: ResumeLatexVersionProvider,
    agent: BaseLatexSelectionAgentPort,
    metadata: BaseLatexSelectionAgentMetadata,
    decision_repository: BaseLatexSelectionDecisionRepository,
) -> SelectBaseLatexVersionResult:
    """Pick one trusted historical LaTeX version, or the managed template."""

    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        plan_id = _clean_text(
            "application_plan_id",
            command.application_plan_id,
            maximum=160,
        )
        qa_id = _clean_text(
            "fact_qa_result_id", command.fact_qa_result_id, maximum=160
        )
        now = _require_aware("now", command.now)
        if not isinstance(metadata, BaseLatexSelectionAgentMetadata):
            raise TypeError("metadata must be BaseLatexSelectionAgentMetadata")
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, BaseLatexSelectionFailureReason.INVALID_REQUEST
        )

    try:
        plan_result = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            BaseLatexSelectionFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_result.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command,
            BaseLatexSelectionFailureReason.APPLICATION_PLAN_NOT_FOUND,
        )
    if (
        plan_result.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_result.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            BaseLatexSelectionFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_result.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            BaseLatexSelectionFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
        )

    try:
        qa_read = fact_qa_repository.get(
            subject_id=subject_id, qa_result_id=qa_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.FACT_QA_INTEGRITY_FAILURE,
        )
    if qa_read.status is ResumeFactQAReadStatus.NOT_FOUND:
        return _failure(
            command, BaseLatexSelectionFailureReason.FACT_QA_NOT_FOUND
        )
    if (
        qa_read.status is not ResumeFactQAReadStatus.FOUND
        or not isinstance(qa_read.qa_result, ResumeFactQAResult)
    ):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.FACT_QA_INTEGRITY_FAILURE,
        )
    qa_result = qa_read.qa_result
    if (
        qa_result.subject_id != subject_id
        or qa_result.application_plan_id != plan.plan_id
        or qa_result.job_id != plan.job_id
        or qa_result.job_revision != plan.job_revision
        or qa_result.job_content_hash != plan.job_content_hash
    ):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.FACT_QA_BINDING_MISMATCH,
        )
    if qa_result.verdict is not ResumeFactQAVerdict.PASSED:
        return _failure(
            command, BaseLatexSelectionFailureReason.FACT_QA_NOT_PASSED
        )

    try:
        draft_read = draft_repository.get(
            subject_id=subject_id,
            draft_id=qa_result.tailored_resume_draft_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    if draft_read.status is TailoredResumeDraftReadStatus.NOT_FOUND:
        return _failure(
            command, BaseLatexSelectionFailureReason.DRAFT_NOT_FOUND
        )
    if (
        draft_read.status is not TailoredResumeDraftReadStatus.FOUND
        or not isinstance(draft_read.draft, TailoredResumeDraft)
    ):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    draft = draft_read.draft
    if (
        draft.subject_id != subject_id
        or draft.draft_id != qa_result.tailored_resume_draft_id
        or draft.draft_content_hash
        != qa_result.tailored_resume_draft_hash
        or draft.application_plan_id != plan.plan_id
        or draft.job_id != plan.job_id
        or draft.job_revision != plan.job_revision
        or draft.job_content_hash != plan.job_content_hash
    ):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.DRAFT_BINDING_MISMATCH,
        )

    try:
        selection_read = selection_repository.get(
            subject_id=subject_id,
            decision_id=draft.resume_selection_decision_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            BaseLatexSelectionFailureReason
            .RESUME_SELECTION_INTEGRITY_FAILURE,
        )
    if selection_read.status is ResumeSelectionDecisionReadStatus.NOT_FOUND:
        return _failure(
            command,
            BaseLatexSelectionFailureReason.RESUME_SELECTION_NOT_FOUND,
        )
    if (
        selection_read.status is not ResumeSelectionDecisionReadStatus.FOUND
        or not isinstance(
            selection_read.decision, ResumeSelectionDecision
        )
    ):
        return _failure(
            command,
            BaseLatexSelectionFailureReason
            .RESUME_SELECTION_INTEGRITY_FAILURE,
        )
    selection = selection_read.decision
    if (
        selection.subject_id != subject_id
        or selection.decision_id != draft.resume_selection_decision_id
        or selection.application_plan_id != plan.plan_id
        or selection.source_resume_id != draft.source_resume_id
        or selection.source_artifact_sha256
        != draft.source_artifact_sha256
    ):
        return _failure(
            command,
            BaseLatexSelectionFailureReason
            .RESUME_SELECTION_BINDING_MISMATCH,
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
            command, BaseLatexSelectionFailureReason.JOB_READ_FAILED
        )
    if job is None:
        return _failure(
            command, BaseLatexSelectionFailureReason.JOB_NOT_FOUND
        )
    if (
        not isinstance(job, JobPosting)
        or job.job_id != plan.job_id
        or job.revision != plan.job_revision
        or job.content_hash != plan.job_content_hash
    ):
        return _failure(
            command, BaseLatexSelectionFailureReason.JOB_BINDING_MISMATCH
        )

    try:
        listed = latex_version_provider.list_selectable(subject_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command, BaseLatexSelectionFailureReason.LATEX_PROVIDER_FAILED
        )
    if (
        listed.status is not ResumeLatexVersionListStatus.SUCCEEDED
        or listed.subject_id != subject_id
    ):
        return _failure(
            command, BaseLatexSelectionFailureReason.LATEX_PROVIDER_FAILED
        )

    versions: list[ResumeLatexVersion] = []
    views: list[BaseLatexCandidateView] = []
    for version in listed.versions:
        if not isinstance(version, ResumeLatexVersion):
            return _failure(
                command,
                BaseLatexSelectionFailureReason.LATEX_PROVIDER_FAILED,
            )
        if version.subject_id != subject_id:
            return _failure(
                command,
                BaseLatexSelectionFailureReason.LATEX_PROVIDER_FAILED,
            )
        passed_provenance = False
        if version.fact_qa_result_id is not None:
            try:
                provenance = fact_qa_repository.get(
                    subject_id=subject_id,
                    qa_result_id=version.fact_qa_result_id,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                return _failure(
                    command,
                    BaseLatexSelectionFailureReason
                    .LATEX_PROVENANCE_INVALID,
                )
            if (
                provenance.status is not ResumeFactQAReadStatus.FOUND
                or not isinstance(
                    provenance.qa_result, ResumeFactQAResult
                )
                or provenance.qa_result.qa_content_hash
                != version.fact_qa_result_hash
                or provenance.qa_result.verdict
                is not ResumeFactQAVerdict.PASSED
            ):
                return _failure(
                    command,
                    BaseLatexSelectionFailureReason
                    .LATEX_PROVENANCE_INVALID,
                )
            passed_provenance = True
        versions.append(version)
        views.append(
            BaseLatexCandidateView.from_version(
                version, has_passed_fact_qa=passed_provenance
            )
        )

    candidates = tuple(views)
    by_id = {item.latex_version_id: item for item in versions}
    candidate_hash = base_latex_candidate_set_hash(candidates)
    binding = _selection_binding(
        plan=plan,
        draft=draft,
        qa_result=qa_result,
        job=job,
        selection=selection,
        candidate_set_hash=candidate_hash,
        metadata=metadata,
    )
    decision_id = f"base-latex-selection-{binding}"
    try:
        existing = decision_repository.get(
            subject_id=subject_id, decision_id=decision_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.DECISION_INTEGRITY_FAILURE,
            selection_binding=binding,
            candidate_set_hash=candidate_hash,
        )
    if existing.status is BaseLatexSelectionReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            BaseLatexSelectionFailureReason.DECISION_INTEGRITY_FAILURE,
            selection_binding=binding,
            candidate_set_hash=candidate_hash,
        )
    if (
        existing.status is BaseLatexSelectionReadStatus.FOUND
        and existing.decision is not None
    ):
        return SelectBaseLatexVersionResult(
            status=BaseLatexSelectionStatus.UNCHANGED,
            subject_id=subject_id,
            application_plan_id=plan_id,
            selection_binding=binding,
            candidate_set_hash=candidate_hash,
            decision=existing.decision,
            write_result=BaseLatexSelectionWriteResult(
                status=BaseLatexSelectionWriteStatus.UNCHANGED,
                decision=existing.decision,
                reason_code=None,
                retryable=False,
            ),
            reason_code=None,
            retryable=False,
            message="The existing base LaTeX selection is unchanged.",
        )

    required_versions, required_families = _required_identifiers(
        plan.user_preparation_instructions
    )
    has_requirement = bool(required_versions or required_families)

    def _deferred(detail: str) -> SelectBaseLatexVersionResult:
        return SelectBaseLatexVersionResult(
            status=BaseLatexSelectionStatus.DEFERRED_NEEDS_HUMAN,
            subject_id=subject_id,
            application_plan_id=plan_id,
            selection_binding=binding,
            candidate_set_hash=candidate_hash,
            decision=None,
            write_result=None,
            reason_code=(
                BaseLatexSelectionFailureReason
                .USER_REQUIREMENT_UNSATISFIABLE
            ),
            retryable=False,
            message=f"The requested LaTeX version needs human review: {detail}",
        )

    pool = candidates
    agent_invoked = False
    if has_requirement:
        if not required_versions.issubset(set(by_id)):
            return _deferred(
                "an explicitly requested version is not a selectable candidate."
            )
        pool = tuple(
            item
            for item in candidates
            if (
                not required_versions
                or item.latex_version_id in required_versions
            )
            and (
                not required_families
                or item.root_family_id in required_families
            )
        )
        if not pool:
            return _deferred(
                "no selectable candidate satisfies the requested version "
                "or family."
            )

    selected: BaseLatexCandidateView | None
    method: BaseLatexSelectionMethod
    rationale: str
    if not pool:
        selected = None
        method = BaseLatexSelectionMethod.MANAGED_TEMPLATE_FALLBACK
        rationale = (
            "No selectable LaTeX version exists, so the managed default "
            "template applies."
        )
    elif has_requirement and len(pool) == 1:
        selected = pool[0]
        method = BaseLatexSelectionMethod.USER_REQUIRED_VERSION
        rationale = (
            "The plan instructions explicitly require this LaTeX version."
        )
    elif len(pool) == 1:
        selected = pool[0]
        method = BaseLatexSelectionMethod.ONLY_CANDIDATE
        rationale = "It is the only selectable LaTeX version."
    else:
        matched = tuple(
            item
            for item in pool
            if item.source_resume_id == selection.source_resume_id
        )
        if len(matched) == 1:
            selected = matched[0]
            method = BaseLatexSelectionMethod.EXACT_SOURCE_RESUME_MATCH
            rationale = (
                "It is the only LaTeX version bound to the selected source "
                "resume."
            )
        else:
            context = BaseLatexSelectionContext(
                subject_id=subject_id,
                application_plan_id=plan_id,
                job=BaseLatexSelectionJobContext.from_job(job),
                candidates=pool,
                user_preparation_instructions=(
                    plan.user_preparation_instructions
                ),
            )
            try:
                output = await agent.evaluate(context)
            except TimeoutError:
                return _failure(
                    command,
                    BaseLatexSelectionFailureReason.AGENT_TIMEOUT,
                    retryable=True,
                    selection_binding=binding,
                    candidate_set_hash=candidate_hash,
                )
            except BaseLatexSelectionAgentUnavailableError:
                return _failure(
                    command,
                    BaseLatexSelectionFailureReason.AGENT_UNAVAILABLE,
                    retryable=True,
                    selection_binding=binding,
                    candidate_set_hash=candidate_hash,
                )
            except Exception:
                return _failure(
                    command,
                    BaseLatexSelectionFailureReason.AGENT_UNAVAILABLE,
                    retryable=True,
                    selection_binding=binding,
                    candidate_set_hash=candidate_hash,
                )
            agent_invoked = True
            allowed = {item.latex_version_id: item for item in pool}
            chosen = (
                allowed.get(output.selected_latex_version_id or "")
                if isinstance(output, BaseLatexSelectionAgentOutput)
                else None
            )
            if not isinstance(output, BaseLatexSelectionAgentOutput):
                if has_requirement:
                    return _deferred(
                        "the Agent returned no usable answer for an "
                        "explicitly requested version."
                    )
                selected = None
                method = BaseLatexSelectionMethod.MANAGED_TEMPLATE_FALLBACK
                rationale = (
                    "The Agent output was unusable, so the managed default "
                    "template applies."
                )
            elif (
                output.disposition
                is BaseLatexSelectionAgentDisposition.NEEDS_HUMAN
            ):
                if has_requirement:
                    return _deferred(
                        "the Agent could not satisfy the requested version."
                    )
                selected = None
                method = BaseLatexSelectionMethod.MANAGED_TEMPLATE_FALLBACK
                rationale = (
                    "The Agent asked for human review, so the managed "
                    "default template applies."
                )
            elif (
                output.disposition
                is BaseLatexSelectionAgentDisposition.SELECTED
                and chosen is not None
            ):
                selected = chosen
                method = BaseLatexSelectionMethod.AGENT_SELECTED
                rationale = output.rationale
            elif (
                output.disposition
                is BaseLatexSelectionAgentDisposition.SELECTED
            ):
                if has_requirement:
                    return _deferred(
                        "the Agent named a version outside the requested set."
                    )
                selected = None
                method = BaseLatexSelectionMethod.MANAGED_TEMPLATE_FALLBACK
                rationale = (
                    "The Agent named an unknown version, so the managed "
                    "default template applies."
                )
            else:
                if has_requirement:
                    return _deferred(
                        "the Agent chose the managed template despite an "
                        "explicitly requested version."
                    )
                selected = None
                method = BaseLatexSelectionMethod.MANAGED_TEMPLATE_FALLBACK
                rationale = output.rationale

    kind = (
        BaseLatexSelectionKind.EXISTING_VERSION
        if selected is not None
        else BaseLatexSelectionKind.MANAGED_TEMPLATE_FALLBACK
    )
    try:
        decision = BaseLatexSelectionDecision(
            decision_id=decision_id,
            contract_version=BASE_LATEX_SELECTION_CONTRACT_VERSION,
            selection_binding=binding,
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            tailored_resume_draft_id=draft.draft_id,
            tailored_resume_draft_hash=draft.draft_content_hash,
            fact_qa_result_id=qa_result.qa_result_id,
            fact_qa_result_hash=qa_result.qa_content_hash,
            job_id=job.job_id,
            job_revision=job.revision,
            job_content_hash=job.content_hash,
            resume_selection_decision_id=selection.decision_id,
            source_resume_id=selection.source_resume_id,
            candidate_set_hash=candidate_hash,
            selection_kind=kind,
            selection_method=method,
            selected_latex_version_id=(
                selected.latex_version_id if selected else None
            ),
            selected_latex_source_sha256=(
                selected.source_sha256 if selected else None
            ),
            selected_root_family_id=(
                selected.root_family_id if selected else None
            ),
            rationale=rationale,
            agent_invoked=agent_invoked,
            agent_version=metadata.agent_version,
            prompt_version=metadata.prompt_version,
            model_id=metadata.model_id,
            selected_at=now,
        )
    except (TypeError, ValueError):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.DECISION_INTEGRITY_FAILURE,
            selection_binding=binding,
            candidate_set_hash=candidate_hash,
        )

    try:
        write_result = decision_repository.save(decision)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            BaseLatexSelectionFailureReason.DECISION_PERSISTENCE_FAILED,
            retryable=True,
            selection_binding=binding,
            candidate_set_hash=candidate_hash,
        )
    if write_result.status is BaseLatexSelectionWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or BaseLatexSelectionFailureReason.DECISION_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            selection_binding=binding,
            candidate_set_hash=candidate_hash,
            write_result=write_result,
        )
    status = BaseLatexSelectionStatus(write_result.status.value)
    return SelectBaseLatexVersionResult(
        status=status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        selection_binding=binding,
        candidate_set_hash=candidate_hash,
        decision=write_result.decision,
        write_result=write_result,
        reason_code=None,
        retryable=False,
        message=(
            "The base LaTeX selection was created."
            if status is BaseLatexSelectionStatus.CREATED
            else "The existing base LaTeX selection is unchanged."
        ),
    )


__all__ = [
    "BASE_LATEX_SELECTION_CONTRACT_VERSION",
    "BaseLatexCandidateView",
    "BaseLatexSelectionAgentDisposition",
    "BaseLatexSelectionAgentMetadata",
    "BaseLatexSelectionAgentOutput",
    "BaseLatexSelectionAgentPort",
    "BaseLatexSelectionAgentUnavailableError",
    "BaseLatexSelectionContext",
    "BaseLatexSelectionDecision",
    "BaseLatexSelectionDecisionRepository",
    "BaseLatexSelectionFailureReason",
    "BaseLatexSelectionJobContext",
    "BaseLatexSelectionKind",
    "BaseLatexSelectionMethod",
    "BaseLatexSelectionReadResult",
    "BaseLatexSelectionReadStatus",
    "BaseLatexSelectionStatus",
    "BaseLatexSelectionWriteResult",
    "BaseLatexSelectionWriteStatus",
    "MAX_BASE_LATEX_RATIONALE_CHARS",
    "PrivateHomeBaseLatexSelectionDecisionRepository",
    "SelectBaseLatexVersionCommand",
    "SelectBaseLatexVersionResult",
    "base_latex_candidate_set_hash",
    "select_base_latex_version",
]
