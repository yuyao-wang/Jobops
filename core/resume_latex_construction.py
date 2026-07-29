"""Construct one immutable ResumeLatexVersion from a fact-QA-passed draft."""

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
from .base_latex_selection import (
    BaseLatexSelectionDecision,
    BaseLatexSelectionDecisionRepository,
    BaseLatexSelectionKind,
    BaseLatexSelectionReadStatus,
)
from .managed_resume_template import (
    ManagedResumeTemplate,
    ManagedResumeTemplateProvider,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_fact_qa import (
    ResumeFactQAReadStatus,
    ResumeFactQARepository,
    ResumeFactQAResult,
    ResumeFactQAVerdict,
)
from .resume_latex_markers import (
    BULLET_MACRO,
    JOBOPS_CONTENT_BEGIN,
    JOBOPS_CONTENT_END,
    RESUME_LATEX_MARKER_CONTRACT_VERSION,
    SECTION_MACRO,
    ResumeLatexMarkerError,
    escape_latex,
    marker_contract_dict,
    normalized_run,
    parse_markers,
    render_marker,
    split_controlled_region,
    uses_controlled_markers,
    visible_text_runs,
)
from .resume_latex_versions import (
    RegisterResumeLatexVersionCommand,
    RegisterResumeLatexVersionStatus,
    ResumeLatexCapabilityError,
    ResumeLatexSourceKind,
    ResumeLatexVersion,
    ResumeLatexVersionReadStatus,
    ResumeLatexVersionRepository,
    register_resume_latex_version,
    validate_latex_source,
)
from .resume_tailoring import (
    TailoredBulletChangeType,
    TailoredResumeDraft,
    TailoredResumeDraftReadStatus,
    TailoredResumeDraftRepository,
)


RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION = "resume-latex-construction-v1"
RESUME_LATEX_CONSTRUCTION_POLICY_VERSION = (
    "resume-latex-construction-policy-v1"
)

RESUME_LATEX_CONSTRUCTION_AGENT_POLICY = """LaTeX Construction Agent policy (static, non-negotiable):

You re-typeset an existing resume layout around new content. You are not a
writer and not a fact checker.

You must:
- Keep the supplied layout, packages, spacing and visual style.
- Place every supplied Draft section and bullet inside one controlled
  region delimited by the supplied begin and end sentinels.
- Emit each section as the section macro and each bullet as the bullet
  macro, using the supplied IDs exactly.
- Reproduce every Draft text exactly as supplied, changing nothing but
  LaTeX escaping.

You must never:
- Reword, summarize, extend, shorten or reorder the supplied text.
- Add a company, project, skill, number, date or outcome.
- Keep any resume content from the old source that is not in the Draft.
- Emit shell escape, file reads or writes, external programs or absolute
  include paths.
- Read files, call tools, or reach a repository or compiler.

Return the complete LaTeX document as typed structured output.
"""

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RECORD_ID_PATTERN = re.compile(
    r"^resume-latex-construction-[a-f0-9]{64}$"
)


class ResumeLatexConstructionPath(str, Enum):
    DERIVED_FROM_EXISTING_VERSION = "DERIVED_FROM_EXISTING_VERSION"
    MANAGED_TEMPLATE = "MANAGED_TEMPLATE"


class ResumeLatexConstructionMethod(str, Enum):
    DETERMINISTIC_TEMPLATE_RENDER = "DETERMINISTIC_TEMPLATE_RENDER"
    DETERMINISTIC_REGION_REPLACEMENT = "DETERMINISTIC_REGION_REPLACEMENT"
    AGENT_RECONSTRUCTED = "AGENT_RECONSTRUCTED"


class ResumeLatexConstructionStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_SOURCE_UNREADABLE = "DEFERRED_SOURCE_UNREADABLE"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class ResumeLatexConstructionWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeLatexConstructionReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeLatexConstructionFailureReason(str, Enum):
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


class ResumeLatexConstructionAgentUnavailableError(RuntimeError):
    """Raised when the bounded construction Agent cannot return an output."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the construction contract")
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
        raise ValueError("constructed_at is invalid")
    return _require_aware(
        "constructed_at",
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
class ResumeLatexConstructionAgentMetadata:
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
class ConstructionDraftBullet:
    bullet_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ConstructionDraftSection:
    section_id: str
    title: str | None
    bullets: tuple[ConstructionDraftBullet, ...]


@dataclass(frozen=True, slots=True)
class ResumeLatexConstructionContext:
    subject_id: str
    tailored_resume_draft_id: str
    base_latex_source: str
    sections: tuple[ConstructionDraftSection, ...]
    user_preparation_instructions: str | None
    marker_contract: Mapping[str, Any]
    agent_policy: str
    agent_policy_version: str


@dataclass(frozen=True, slots=True)
class ResumeLatexConstructionAgentOutput:
    latex_source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.latex_source, str)
            or not self.latex_source.strip()
        ):
            raise ValueError("latex_source must be non-empty text")


@runtime_checkable
class ResumeLatexConstructionAgentPort(Protocol):
    async def construct(
        self,
        context: ResumeLatexConstructionContext,
    ) -> ResumeLatexConstructionAgentOutput:
        """Re-typeset the supplied layout around the supplied Draft content."""


def _draft_sections(
    draft: TailoredResumeDraft,
) -> tuple[ConstructionDraftSection, ...]:
    """Project the Draft's retained content; omitted bullets are dropped."""

    sections: list[ConstructionDraftSection] = []
    for section in draft.sections:
        bullets = tuple(
            ConstructionDraftBullet(
                bullet_id=bullet.source_block_id,
                text=bullet.text or "",
            )
            for bullet in section.bullets
            if bullet.change_type is not TailoredBulletChangeType.OMITTED
        )
        sections.append(
            ConstructionDraftSection(
                section_id=section.source_section_id,
                title=section.title,
                bullets=bullets,
            )
        )
    return tuple(sections)


def render_controlled_region(
    sections: tuple[ConstructionDraftSection, ...],
) -> str:
    """Render the Draft deterministically; wording is never altered."""

    lines: list[str] = []
    for section in sections:
        lines.append(
            render_marker(
                SECTION_MACRO, section.section_id, section.title or ""
            )
        )
        if section.bullets:
            lines.append("\\begin{itemize}")
            lines.extend(
                render_marker(BULLET_MACRO, bullet.bullet_id, bullet.text)
                for bullet in section.bullets
            )
            lines.append("\\end{itemize}")
    return "".join(f"{line}\n" for line in lines)


class _ConstructionRejected(ValueError):
    """The constructed LaTeX failed a deterministic safety check."""


def validate_constructed_source(
    source: str,
    *,
    sections: tuple[ConstructionDraftSection, ...],
    base_source: str | None,
) -> str:
    """Verify structure, marker fidelity, escaping and stale-content removal."""

    if not isinstance(source, str) or not source.strip():
        raise _ConstructionRejected("the constructed LaTeX is empty")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _ConstructionRejected(
            "the constructed LaTeX is not valid UTF-8"
        ) from exc
    try:
        validate_latex_source(source)
    except ResumeLatexCapabilityError as exc:
        raise _ConstructionRejected(
            f"the constructed LaTeX requests {exc.capability.value}"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise _ConstructionRejected(
            "the constructed LaTeX is outside the source contract"
        ) from exc
    for required in (
        "\\documentclass",
        "\\begin{document}",
        "\\end{document}",
    ):
        if required not in source:
            raise _ConstructionRejected(
                f"the constructed LaTeX is missing {required}"
            )
    if not uses_controlled_markers(source):
        raise _ConstructionRejected(
            "the constructed LaTeX has no single controlled region"
        )
    try:
        _, region, _ = split_controlled_region(source)
        markers = parse_markers(region)
    except ResumeLatexMarkerError as exc:
        raise _ConstructionRejected(str(exc)) from exc
    if parse_markers(source) != markers:
        raise _ConstructionRejected(
            "controlled markers appear outside the controlled region"
        )

    expected: list[tuple[str, str, str]] = []
    for section in sections:
        expected.append(
            (SECTION_MACRO, section.section_id, section.title or "")
        )
        expected.extend(
            (BULLET_MACRO, bullet.bullet_id, bullet.text)
            for bullet in section.bullets
        )
    seen = [(item.macro, item.marker_id) for item in markers]
    if len(seen) != len(set(seen)):
        raise _ConstructionRejected("a controlled marker is duplicated")
    expected_keys = {(macro, marker_id) for macro, marker_id, _ in expected}
    if set(seen) - expected_keys:
        raise _ConstructionRejected(
            "the constructed LaTeX carries a marker outside the Draft"
        )
    if expected_keys - set(seen):
        raise _ConstructionRejected(
            "the constructed LaTeX omits Draft content"
        )
    by_key = {
        (item.macro, item.marker_id): item.text for item in markers
    }
    for macro, marker_id, text in expected:
        if by_key[(macro, marker_id)] != escape_latex(text):
            raise _ConstructionRejected(
                "a marker's text does not match the Draft exactly"
            )

    if base_source is not None:
        allowed = {normalized_run(text) for _, _, text in expected}
        stale = {
            run
            for run in visible_text_runs(base_source) & visible_text_runs(source)
            if run not in allowed
            and not any(run in item for item in allowed)
        }
        if stale:
            raise _ConstructionRejected(
                "historical resume content survives in the constructed LaTeX"
            )
    return source


def _construction_binding(
    *,
    plan: ApplicationPlan,
    draft: TailoredResumeDraft,
    qa_result: ResumeFactQAResult,
    decision: BaseLatexSelectionDecision,
    parent_version_id: str | None,
    parent_source_sha256: str | None,
    template_id: str | None,
    template_sha256: str | None,
    metadata: ResumeLatexConstructionAgentMetadata,
) -> str:
    return _canonical_hash(
        {
            "application_plan_id": plan.plan_id,
            "base_latex_selection_decision_id": decision.decision_id,
            "fact_qa_result_hash": qa_result.qa_content_hash,
            "fact_qa_result_id": qa_result.qa_result_id,
            "marker_contract_version": (
                RESUME_LATEX_MARKER_CONTRACT_VERSION
            ),
            "parent_source_sha256": parent_source_sha256,
            "parent_version_id": parent_version_id,
            "resume_latex_construction_agent_version": (
                metadata.agent_version
            ),
            "resume_latex_construction_contract_version": (
                RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION
            ),
            "resume_latex_construction_model_id": metadata.model_id,
            "resume_latex_construction_policy_version": (
                RESUME_LATEX_CONSTRUCTION_POLICY_VERSION
            ),
            "resume_latex_construction_prompt_version": (
                metadata.prompt_version
            ),
            "subject_id": plan.subject_id,
            "tailored_resume_draft_hash": draft.draft_content_hash,
            "tailored_resume_draft_id": draft.draft_id,
            "template_id": template_id,
            "template_sha256": template_sha256,
            "user_preparation_instructions_hash": (
                plan.user_preparation_instructions_hash
            ),
        }
    )


@runtime_checkable
class LatexBuildProvenance(Protocol):
    """What produced one managed LaTeX version.

    Compilation and visual QA only need to know which version a build
    produced and what it was bound to, so a layout revision record is equally
    valid provenance. Construction records satisfy this protocol unchanged.
    """

    record_id: str
    subject_id: str
    latex_version_id: str
    latex_source_sha256: str
    root_family_id: str
    parent_version_id: str | None
    template_id: str | None
    template_sha256: str | None
    tailored_resume_draft_id: str
    tailored_resume_draft_hash: str
    fact_qa_result_id: str
    fact_qa_result_hash: str

    @property
    def build_provenance_binding(self) -> str:
        """The immutable binding identifying this build."""


@dataclass(frozen=True, slots=True)
class ResumeLatexConstructionRecord:
    """Construction provenance owned by this Slice, not by the registry."""

    record_id: str
    contract_version: str
    construction_binding: str
    subject_id: str
    application_plan_id: str
    tailored_resume_draft_id: str
    tailored_resume_draft_hash: str
    fact_qa_result_id: str
    fact_qa_result_hash: str
    base_latex_selection_decision_id: str
    construction_path: ResumeLatexConstructionPath
    construction_method: ResumeLatexConstructionMethod
    latex_version_id: str
    latex_source_sha256: str
    root_family_id: str
    parent_version_id: str | None
    template_id: str | None
    template_sha256: str | None
    agent_invoked: bool
    agent_version: str
    prompt_version: str
    model_id: str
    constructed_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION:
            raise ValueError("construction contract is unsupported")
        binding = _require_hash(
            "construction_binding", self.construction_binding
        )
        if (
            not isinstance(self.record_id, str)
            or _RECORD_ID_PATTERN.fullmatch(self.record_id) is None
            or self.record_id != f"resume-latex-construction-{binding}"
        ):
            raise ValueError("record_id does not match its binding")
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
        _clean_text(
            "base_latex_selection_decision_id",
            self.base_latex_selection_decision_id,
            maximum=160,
        )
        path = ResumeLatexConstructionPath(self.construction_path)
        method = ResumeLatexConstructionMethod(self.construction_method)
        object.__setattr__(self, "construction_path", path)
        object.__setattr__(self, "construction_method", method)
        _clean_text(
            "latex_version_id", self.latex_version_id, maximum=160
        )
        _require_hash("latex_source_sha256", self.latex_source_sha256)
        _clean_text("root_family_id", self.root_family_id, maximum=160)
        parent = _optional_text(
            "parent_version_id", self.parent_version_id, maximum=160
        )
        template_id = _optional_text(
            "template_id", self.template_id, maximum=160
        )
        template_hash = _optional_hash(
            "template_sha256", self.template_sha256
        )
        if (template_id is None) != (template_hash is None):
            raise ValueError("template binding must be complete or absent")
        if path is ResumeLatexConstructionPath.MANAGED_TEMPLATE:
            if (
                parent is not None
                or template_id is None
                or method
                is not ResumeLatexConstructionMethod
                .DETERMINISTIC_TEMPLATE_RENDER
            ):
                raise ValueError("managed-template construction is invalid")
        elif parent is None or template_id is not None:
            raise ValueError("derived construction is invalid")
        if type(self.agent_invoked) is not bool:
            raise TypeError("agent_invoked must be a boolean")
        if (
            method is ResumeLatexConstructionMethod.AGENT_RECONSTRUCTED
        ) is not self.agent_invoked:
            raise ValueError("agent_invoked does not match the method")
        _clean_text("agent_version", self.agent_version, maximum=80)
        _clean_text("prompt_version", self.prompt_version, maximum=80)
        _clean_text("model_id", self.model_id, maximum=160)
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "parent_version_id", parent)
        object.__setattr__(self, "template_id", template_id)
        _require_aware("constructed_at", self.constructed_at)

    @property
    def build_provenance_binding(self) -> str:
        return self.construction_binding

    def content_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "contract_version": self.contract_version,
            "construction_binding": self.construction_binding,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "tailored_resume_draft_id": self.tailored_resume_draft_id,
            "tailored_resume_draft_hash": self.tailored_resume_draft_hash,
            "fact_qa_result_id": self.fact_qa_result_id,
            "fact_qa_result_hash": self.fact_qa_result_hash,
            "base_latex_selection_decision_id": (
                self.base_latex_selection_decision_id
            ),
            "construction_path": self.construction_path.value,
            "construction_method": self.construction_method.value,
            "latex_version_id": self.latex_version_id,
            "latex_source_sha256": self.latex_source_sha256,
            "root_family_id": self.root_family_id,
            "parent_version_id": self.parent_version_id,
            "template_id": self.template_id,
            "template_sha256": self.template_sha256,
            "agent_invoked": self.agent_invoked,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "constructed_at": _rfc3339(self.constructed_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeLatexConstructionWriteResult:
    status: ResumeLatexConstructionWriteStatus
    record: ResumeLatexConstructionRecord | None
    reason_code: ResumeLatexConstructionFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ResumeLatexConstructionWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLatexConstructionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ResumeLatexConstructionWriteStatus.CREATED,
            ResumeLatexConstructionWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.record, LatexBuildProvenance)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful construction write is invalid")
        elif self.record is not None or self.reason_code is None:
            raise ValueError("failed construction write is invalid")


@dataclass(frozen=True, slots=True)
class ResumeLatexConstructionReadResult:
    status: ResumeLatexConstructionReadStatus
    record: ResumeLatexConstructionRecord | None
    reason_code: ResumeLatexConstructionFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeLatexConstructionReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLatexConstructionFailureReason(self.reason_code),
            )
        if status is ResumeLatexConstructionReadStatus.FOUND:
            if (
                not isinstance(self.record, LatexBuildProvenance)
                or self.reason_code is not None
            ):
                raise ValueError("found construction read is invalid")
        elif status is ResumeLatexConstructionReadStatus.NOT_FOUND:
            if self.record is not None or self.reason_code is not None:
                raise ValueError("not-found construction read is invalid")
        elif (
            self.record is not None
            or self.reason_code
            is not ResumeLatexConstructionFailureReason
            .RECORD_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure construction read is invalid")


@runtime_checkable
class ResumeLatexConstructionRecordRepository(Protocol):
    def save(
        self, record: ResumeLatexConstructionRecord
    ) -> ResumeLatexConstructionWriteResult:
        """Persist one immutable construction record."""

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeLatexConstructionReadResult:
        """Read one subject-owned construction record."""


def _record_from_dict(value: Any) -> ResumeLatexConstructionRecord:
    expected = {
        "record_id",
        "contract_version",
        "construction_binding",
        "subject_id",
        "application_plan_id",
        "tailored_resume_draft_id",
        "tailored_resume_draft_hash",
        "fact_qa_result_id",
        "fact_qa_result_hash",
        "base_latex_selection_decision_id",
        "construction_path",
        "construction_method",
        "latex_version_id",
        "latex_source_sha256",
        "root_family_id",
        "parent_version_id",
        "template_id",
        "template_sha256",
        "agent_invoked",
        "agent_version",
        "prompt_version",
        "model_id",
        "constructed_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted construction record is invalid")
    return ResumeLatexConstructionRecord(
        record_id=value["record_id"],
        contract_version=value["contract_version"],
        construction_binding=value["construction_binding"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        tailored_resume_draft_id=value["tailored_resume_draft_id"],
        tailored_resume_draft_hash=value["tailored_resume_draft_hash"],
        fact_qa_result_id=value["fact_qa_result_id"],
        fact_qa_result_hash=value["fact_qa_result_hash"],
        base_latex_selection_decision_id=value[
            "base_latex_selection_decision_id"
        ],
        construction_path=ResumeLatexConstructionPath(
            value["construction_path"]
        ),
        construction_method=ResumeLatexConstructionMethod(
            value["construction_method"]
        ),
        latex_version_id=value["latex_version_id"],
        latex_source_sha256=value["latex_source_sha256"],
        root_family_id=value["root_family_id"],
        parent_version_id=value["parent_version_id"],
        template_id=value["template_id"],
        template_sha256=value["template_sha256"],
        agent_invoked=value["agent_invoked"],
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        constructed_at=_parse_timestamp(value["constructed_at"]),
    )


class PrivateHomeResumeLatexConstructionRecordRepository:
    """Immutable, subject-scoped construction records in Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, record_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(record_id, str)
            or _RECORD_ID_PATTERN.fullmatch(record_id) is None
        ):
            raise ValueError("record_id is invalid")
        return (
            self._home.paths.resume_latex_constructions
            / _subject_storage_key(subject)
            / f"{record_id}.json"
        )

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeLatexConstructionReadResult:
        path = self._path(subject_id, record_id)
        with self._lock:
            if not path.exists():
                return ResumeLatexConstructionReadResult(
                    status=ResumeLatexConstructionReadStatus.NOT_FOUND,
                    record=None,
                )
            if path.is_symlink() or not path.is_file():
                return ResumeLatexConstructionReadResult(
                    status=(
                        ResumeLatexConstructionReadStatus.INTEGRITY_FAILURE
                    ),
                    record=None,
                    reason_code=(
                        ResumeLatexConstructionFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            try:
                record = _record_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ResumeLatexConstructionReadResult(
                    status=(
                        ResumeLatexConstructionReadStatus.INTEGRITY_FAILURE
                    ),
                    record=None,
                    reason_code=(
                        ResumeLatexConstructionFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            if (
                record.subject_id != subject_id.strip()
                or record.record_id != record_id
                or path.name != f"{record.record_id}.json"
            ):
                return ResumeLatexConstructionReadResult(
                    status=(
                        ResumeLatexConstructionReadStatus.INTEGRITY_FAILURE
                    ),
                    record=None,
                    reason_code=(
                        ResumeLatexConstructionFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            return ResumeLatexConstructionReadResult(
                status=ResumeLatexConstructionReadStatus.FOUND,
                record=record,
            )

    def save(
        self, record: ResumeLatexConstructionRecord
    ) -> ResumeLatexConstructionWriteResult:
        if not isinstance(record, ResumeLatexConstructionRecord):
            raise TypeError("record must be a construction record")
        path = self._path(record.subject_id, record.record_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            record.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ResumeLatexConstructionWriteResult(
                    status=ResumeLatexConstructionWriteStatus.FAILED,
                    record=None,
                    reason_code=(
                        ResumeLatexConstructionFailureReason
                        .RECORD_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return ResumeLatexConstructionWriteResult(
                    status=ResumeLatexConstructionWriteStatus.CREATED,
                    record=record,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=record.subject_id, record_id=record.record_id
            )
            if (
                existing.status is ResumeLatexConstructionReadStatus.FOUND
                and existing.record is not None
                and existing.record.content_dict() == record.content_dict()
            ):
                return ResumeLatexConstructionWriteResult(
                    status=ResumeLatexConstructionWriteStatus.UNCHANGED,
                    record=existing.record,
                    reason_code=None,
                    retryable=False,
                )
            return ResumeLatexConstructionWriteResult(
                status=ResumeLatexConstructionWriteStatus.FAILED,
                record=None,
                reason_code=(
                    ResumeLatexConstructionFailureReason
                    .RECORD_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class ConstructResumeLatexCommand:
    subject_id: str
    application_plan_id: str
    base_latex_selection_decision_id: str
    fact_qa_result_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ConstructResumeLatexResult:
    status: ResumeLatexConstructionStatus
    subject_id: str
    application_plan_id: str
    construction_binding: str
    version: ResumeLatexVersion | None
    record: ResumeLatexConstructionRecord | None
    reason_code: ResumeLatexConstructionFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = ResumeLatexConstructionStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLatexConstructionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            ResumeLatexConstructionStatus.CREATED,
            ResumeLatexConstructionStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.version, ResumeLatexVersion)
                or not isinstance(
                    self.record, ResumeLatexConstructionRecord
                )
                or self.record.latex_version_id
                != self.version.latex_version_id
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful construction result is invalid")
        elif status in {
            ResumeLatexConstructionStatus.DEFERRED_NEEDS_HUMAN,
            ResumeLatexConstructionStatus.DEFERRED_SOURCE_UNREADABLE,
        }:
            if (
                self.version is not None
                or self.record is not None
                or self.reason_code is None
                or self.retryable
            ):
                raise ValueError("deferred construction result is invalid")
        elif self.version is not None or self.reason_code is None:
            raise ValueError("failed construction result is invalid")


def _failure(
    command: ConstructResumeLatexCommand,
    reason: ResumeLatexConstructionFailureReason,
    *,
    status: ResumeLatexConstructionStatus = (
        ResumeLatexConstructionStatus.FAILED
    ),
    retryable: bool = False,
    construction_binding: str = "",
    detail: str | None = None,
) -> ConstructResumeLatexResult:
    return ConstructResumeLatexResult(
        status=status,
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
        construction_binding=construction_binding,
        version=None,
        record=None,
        reason_code=reason,
        retryable=retryable,
        message=(
            detail
            if detail
            else f"LaTeX construction stopped: {reason.value}."
        ),
    )


async def construct_resume_latex_version(
    command: ConstructResumeLatexCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    draft_repository: TailoredResumeDraftRepository,
    fact_qa_repository: ResumeFactQARepository,
    base_selection_repository: BaseLatexSelectionDecisionRepository,
    latex_version_repository: ResumeLatexVersionRepository,
    template_provider: ManagedResumeTemplateProvider,
    agent: ResumeLatexConstructionAgentPort,
    metadata: ResumeLatexConstructionAgentMetadata,
    construction_repository: ResumeLatexConstructionRecordRepository,
    home: PrivateHome | None = None,
) -> ConstructResumeLatexResult:
    """Write the Draft into the selected layout as one new immutable version."""

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
            "base_latex_selection_decision_id",
            command.base_latex_selection_decision_id,
            maximum=160,
        )
        qa_id = _clean_text(
            "fact_qa_result_id", command.fact_qa_result_id, maximum=160
        )
        now = _require_aware("now", command.now)
        if not isinstance(
            metadata, ResumeLatexConstructionAgentMetadata
        ):
            raise TypeError("metadata must be construction Agent metadata")
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.INVALID_REQUEST,
        )

    try:
        plan_result = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_result.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .APPLICATION_PLAN_NOT_FOUND,
        )
    if (
        plan_result.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_result.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_result.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
        )

    try:
        qa_read = fact_qa_repository.get(
            subject_id=subject_id, qa_result_id=qa_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .FACT_QA_INTEGRITY_FAILURE,
        )
    if qa_read.status is ResumeFactQAReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.FACT_QA_NOT_FOUND,
        )
    if (
        qa_read.status is not ResumeFactQAReadStatus.FOUND
        or not isinstance(qa_read.qa_result, ResumeFactQAResult)
    ):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .FACT_QA_INTEGRITY_FAILURE,
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
            ResumeLatexConstructionFailureReason
            .FACT_QA_BINDING_MISMATCH,
        )
    if qa_result.verdict is not ResumeFactQAVerdict.PASSED:
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.FACT_QA_NOT_PASSED,
        )

    try:
        draft_read = draft_repository.get(
            subject_id=subject_id,
            draft_id=qa_result.tailored_resume_draft_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    if draft_read.status is TailoredResumeDraftReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.DRAFT_NOT_FOUND,
        )
    if (
        draft_read.status is not TailoredResumeDraftReadStatus.FOUND
        or not isinstance(draft_read.draft, TailoredResumeDraft)
    ):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    draft = draft_read.draft
    if (
        draft.subject_id != subject_id
        or draft.draft_id != qa_result.tailored_resume_draft_id
        or draft.draft_content_hash
        != qa_result.tailored_resume_draft_hash
        or draft.application_plan_id != plan.plan_id
    ):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.DRAFT_BINDING_MISMATCH,
        )

    try:
        decision_read = base_selection_repository.get(
            subject_id=subject_id, decision_id=decision_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .BASE_SELECTION_INTEGRITY_FAILURE,
        )
    if decision_read.status is BaseLatexSelectionReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.BASE_SELECTION_NOT_FOUND,
        )
    if (
        decision_read.status is not BaseLatexSelectionReadStatus.FOUND
        or not isinstance(
            decision_read.decision, BaseLatexSelectionDecision
        )
    ):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .BASE_SELECTION_INTEGRITY_FAILURE,
        )
    decision = decision_read.decision
    if (
        decision.subject_id != subject_id
        or decision.application_plan_id != plan.plan_id
        or decision.tailored_resume_draft_id != draft.draft_id
        or decision.tailored_resume_draft_hash
        != draft.draft_content_hash
        or decision.fact_qa_result_id != qa_result.qa_result_id
        or decision.fact_qa_result_hash != qa_result.qa_content_hash
    ):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .BASE_SELECTION_BINDING_MISMATCH,
        )

    sections = _draft_sections(draft)
    if not any(section.bullets for section in sections):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.DRAFT_HAS_NO_CONTENT,
        )

    parent_version: ResumeLatexVersion | None = None
    parent_source: str | None = None
    template: ManagedResumeTemplate | None = None
    if decision.selection_kind is BaseLatexSelectionKind.EXISTING_VERSION:
        try:
            version_read = latex_version_repository.get(
                subject_id=subject_id,
                latex_version_id=decision.selected_latex_version_id or "",
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                ResumeLatexConstructionFailureReason
                .BASE_VERSION_UNREADABLE,
                status=(
                    ResumeLatexConstructionStatus.DEFERRED_SOURCE_UNREADABLE
                ),
            )
        if version_read.status is ResumeLatexVersionReadStatus.NOT_FOUND:
            return _failure(
                command,
                ResumeLatexConstructionFailureReason
                .BASE_VERSION_NOT_FOUND,
            )
        if (
            version_read.status is not ResumeLatexVersionReadStatus.FOUND
            or not isinstance(version_read.version, ResumeLatexVersion)
        ):
            return _failure(
                command,
                ResumeLatexConstructionFailureReason
                .BASE_VERSION_UNREADABLE,
                status=(
                    ResumeLatexConstructionStatus.DEFERRED_SOURCE_UNREADABLE
                ),
                detail=(
                    "The selected LaTeX version cannot be read safely; "
                    "no other version is substituted."
                ),
            )
        parent_version = version_read.version
        if (
            parent_version.source_sha256
            != decision.selected_latex_source_sha256
        ):
            return _failure(
                command,
                ResumeLatexConstructionFailureReason
                .BASE_VERSION_UNREADABLE,
                status=(
                    ResumeLatexConstructionStatus.DEFERRED_SOURCE_UNREADABLE
                ),
                detail=(
                    "The selected LaTeX source drifted from the selection "
                    "decision; no other version is substituted."
                ),
            )
        try:
            source_path = active_home.contained_path(
                parent_version.source_reference
            )
            raw = source_path.read_bytes()
            if (
                hashlib.sha256(raw).hexdigest()
                != parent_version.source_sha256
            ):
                raise ValueError("source hash drift")
            parent_source = raw.decode("utf-8")
        except (
            OSError,
            PrivateHomeError,
            UnicodeDecodeError,
            ValueError,
        ):
            return _failure(
                command,
                ResumeLatexConstructionFailureReason
                .BASE_VERSION_UNREADABLE,
                status=(
                    ResumeLatexConstructionStatus.DEFERRED_SOURCE_UNREADABLE
                ),
                detail=(
                    "The selected LaTeX source cannot be read; no other "
                    "version is substituted."
                ),
            )
    else:
        try:
            template = template_provider.get()
            if not isinstance(template, ManagedResumeTemplate):
                raise TypeError("template provider returned an invalid value")
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                ResumeLatexConstructionFailureReason.TEMPLATE_UNAVAILABLE,
            )

    binding = _construction_binding(
        plan=plan,
        draft=draft,
        qa_result=qa_result,
        decision=decision,
        parent_version_id=(
            parent_version.latex_version_id if parent_version else None
        ),
        parent_source_sha256=(
            parent_version.source_sha256 if parent_version else None
        ),
        template_id=template.template_id if template else None,
        template_sha256=template.template_sha256 if template else None,
        metadata=metadata,
    )
    record_id = f"resume-latex-construction-{binding}"
    try:
        existing = construction_repository.get(
            subject_id=subject_id, record_id=record_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.RECORD_INTEGRITY_FAILURE,
            construction_binding=binding,
        )
    if existing.status is ResumeLatexConstructionReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.RECORD_INTEGRITY_FAILURE,
            construction_binding=binding,
        )
    if (
        existing.status is ResumeLatexConstructionReadStatus.FOUND
        and existing.record is not None
    ):
        try:
            replayed = latex_version_repository.get(
                subject_id=subject_id,
                latex_version_id=existing.record.latex_version_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                ResumeLatexConstructionFailureReason
                .RECORD_INTEGRITY_FAILURE,
                construction_binding=binding,
            )
        if (
            replayed.status is not ResumeLatexVersionReadStatus.FOUND
            or replayed.version is None
        ):
            return _failure(
                command,
                ResumeLatexConstructionFailureReason
                .RECORD_INTEGRITY_FAILURE,
                construction_binding=binding,
            )
        return ConstructResumeLatexResult(
            status=ResumeLatexConstructionStatus.UNCHANGED,
            subject_id=subject_id,
            application_plan_id=plan_id,
            construction_binding=binding,
            version=replayed.version,
            record=existing.record,
            reason_code=None,
            retryable=False,
            message="The existing constructed LaTeX version is unchanged.",
        )

    region = render_controlled_region(sections)
    agent_invoked = False
    if template is not None:
        constructed = template.wrap(region)
        path = ResumeLatexConstructionPath.MANAGED_TEMPLATE
        method = (
            ResumeLatexConstructionMethod.DETERMINISTIC_TEMPLATE_RENDER
        )
    elif parent_source is not None and uses_controlled_markers(
        parent_source
    ):
        head, _, tail = split_controlled_region(parent_source)
        constructed = (
            f"{head}{JOBOPS_CONTENT_BEGIN}\n{region}"
            f"{JOBOPS_CONTENT_END}{tail}"
        )
        path = ResumeLatexConstructionPath.DERIVED_FROM_EXISTING_VERSION
        method = (
            ResumeLatexConstructionMethod.DETERMINISTIC_REGION_REPLACEMENT
        )
    else:
        context = ResumeLatexConstructionContext(
            subject_id=subject_id,
            tailored_resume_draft_id=draft.draft_id,
            base_latex_source=parent_source or "",
            sections=sections,
            user_preparation_instructions=(
                plan.user_preparation_instructions
            ),
            marker_contract=marker_contract_dict(),
            agent_policy=RESUME_LATEX_CONSTRUCTION_AGENT_POLICY,
            agent_policy_version=(
                RESUME_LATEX_CONSTRUCTION_POLICY_VERSION
            ),
        )
        try:
            output = await agent.construct(context)
        except TimeoutError:
            return _failure(
                command,
                ResumeLatexConstructionFailureReason.AGENT_TIMEOUT,
                retryable=True,
                construction_binding=binding,
            )
        except ResumeLatexConstructionAgentUnavailableError:
            return _failure(
                command,
                ResumeLatexConstructionFailureReason.AGENT_UNAVAILABLE,
                retryable=True,
                construction_binding=binding,
            )
        except Exception:
            return _failure(
                command,
                ResumeLatexConstructionFailureReason.AGENT_UNAVAILABLE,
                retryable=True,
                construction_binding=binding,
            )
        agent_invoked = True
        if not isinstance(output, ResumeLatexConstructionAgentOutput):
            return _failure(
                command,
                ResumeLatexConstructionFailureReason
                .CONSTRUCTION_OUTPUT_UNSAFE,
                status=ResumeLatexConstructionStatus.DEFERRED_NEEDS_HUMAN,
                construction_binding=binding,
                detail=(
                    "The constructed LaTeX needs human review: the Agent "
                    "did not return a typed structured result."
                ),
            )
        constructed = output.latex_source
        path = ResumeLatexConstructionPath.DERIVED_FROM_EXISTING_VERSION
        method = ResumeLatexConstructionMethod.AGENT_RECONSTRUCTED

    try:
        constructed = validate_constructed_source(
            constructed,
            sections=sections,
            base_source=parent_source,
        )
    except _ConstructionRejected as rejection:
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .CONSTRUCTION_OUTPUT_UNSAFE,
            status=ResumeLatexConstructionStatus.DEFERRED_NEEDS_HUMAN,
            construction_binding=binding,
            detail=f"The constructed LaTeX needs human review: {rejection}.",
        )
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .CONSTRUCTION_OUTPUT_UNSAFE,
            status=ResumeLatexConstructionStatus.DEFERRED_NEEDS_HUMAN,
            construction_binding=binding,
            detail=(
                "The constructed LaTeX needs human review: it could not be "
                "validated."
            ),
        )

    registration = register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id=subject_id,
            source_kind=(
                ResumeLatexSourceKind.SYSTEM_TEMPLATE_DERIVED
                if template is not None
                else ResumeLatexSourceKind.AI_REVISED
            ),
            now=now,
            latex_source=constructed,
            parent_version_id=(
                parent_version.latex_version_id if parent_version else None
            ),
            template_id=template.template_id if template else None,
            template_sha256=(
                template.template_sha256 if template else None
            ),
            source_resume_id=draft.source_resume_id,
            tailored_resume_draft_id=draft.draft_id,
            tailored_resume_draft_hash=draft.draft_content_hash,
            fact_qa_result_id=qa_result.qa_result_id,
            fact_qa_result_hash=qa_result.qa_content_hash,
        ),
        home=active_home,
        repository=latex_version_repository,
    )
    if (
        registration.status is RegisterResumeLatexVersionStatus.FAILED
        or registration.version is None
    ):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason
            .VERSION_REGISTRATION_FAILED,
            retryable=registration.retryable,
            construction_binding=binding,
        )
    version = registration.version

    record = ResumeLatexConstructionRecord(
        record_id=record_id,
        contract_version=RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION,
        construction_binding=binding,
        subject_id=subject_id,
        application_plan_id=plan.plan_id,
        tailored_resume_draft_id=draft.draft_id,
        tailored_resume_draft_hash=draft.draft_content_hash,
        fact_qa_result_id=qa_result.qa_result_id,
        fact_qa_result_hash=qa_result.qa_content_hash,
        base_latex_selection_decision_id=decision.decision_id,
        construction_path=path,
        construction_method=method,
        latex_version_id=version.latex_version_id,
        latex_source_sha256=version.source_sha256,
        root_family_id=version.root_family_id,
        parent_version_id=version.parent_version_id,
        template_id=version.template_id,
        template_sha256=version.template_sha256,
        agent_invoked=agent_invoked,
        agent_version=metadata.agent_version,
        prompt_version=metadata.prompt_version,
        model_id=metadata.model_id,
        constructed_at=now,
    )
    try:
        write_result = construction_repository.save(record)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLatexConstructionFailureReason.RECORD_PERSISTENCE_FAILED,
            retryable=True,
            construction_binding=binding,
        )
    if write_result.status is ResumeLatexConstructionWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or ResumeLatexConstructionFailureReason
            .RECORD_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            construction_binding=binding,
        )
    status = (
        ResumeLatexConstructionStatus.CREATED
        if registration.status is RegisterResumeLatexVersionStatus.CREATED
        and write_result.status
        is ResumeLatexConstructionWriteStatus.CREATED
        else ResumeLatexConstructionStatus.UNCHANGED
    )
    return ConstructResumeLatexResult(
        status=status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        construction_binding=binding,
        version=version,
        record=write_result.record,
        reason_code=None,
        retryable=False,
        message=(
            "The tailored resume was written into a new LaTeX version."
            if status is ResumeLatexConstructionStatus.CREATED
            else "The existing constructed LaTeX version is unchanged."
        ),
    )


__all__ = [
    "ConstructResumeLatexCommand",
    "ConstructResumeLatexResult",
    "ConstructionDraftBullet",
    "ConstructionDraftSection",
    "PrivateHomeResumeLatexConstructionRecordRepository",
    "RESUME_LATEX_CONSTRUCTION_AGENT_POLICY",
    "RESUME_LATEX_CONSTRUCTION_CONTRACT_VERSION",
    "RESUME_LATEX_CONSTRUCTION_POLICY_VERSION",
    "ResumeLatexConstructionAgentMetadata",
    "ResumeLatexConstructionAgentOutput",
    "ResumeLatexConstructionAgentPort",
    "ResumeLatexConstructionAgentUnavailableError",
    "ResumeLatexConstructionContext",
    "ResumeLatexConstructionFailureReason",
    "ResumeLatexConstructionMethod",
    "ResumeLatexConstructionPath",
    "ResumeLatexConstructionReadResult",
    "ResumeLatexConstructionReadStatus",
    "ResumeLatexConstructionRecord",
    "ResumeLatexConstructionRecordRepository",
    "ResumeLatexConstructionStatus",
    "ResumeLatexConstructionWriteResult",
    "ResumeLatexConstructionWriteStatus",
    "construct_resume_latex_version",
    "render_controlled_region",
    "validate_constructed_source",
]
