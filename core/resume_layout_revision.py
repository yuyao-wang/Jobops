"""Bounded automatic layout revision: typography only, never resume content."""

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
from .pdf_page_renderer import (
    PdfPageRendererPort,
    PdfRendererUnavailableError,
    RenderedPage,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_compilation import (
    CompileResumeLatexResult,
    ResumeCompilationReadStatus,
    ResumeCompilationRecord,
    ResumeCompilationRepository,
    ResumeCompilationStatus,
    unmanaged_file_dependencies,
)
from .resume_latex_construction import (
    LatexBuildProvenance,
    ResumeLatexConstructionReadResult,
    ResumeLatexConstructionReadStatus,
    ResumeLatexConstructionRecordRepository,
)
from .resume_latex_markers import (
    ResumeLatexMarkerError,
    parse_markers,
    split_controlled_region,
    uses_controlled_markers,
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
    TailoredResumeDraft,
    TailoredResumeDraftReadStatus,
    TailoredResumeDraftRepository,
)
from .resume_visual_qa import (
    ResumeVisualQAFindingSeverity,
    ResumeVisualQAReadStatus,
    ResumeVisualQARepository,
    ResumeVisualQAResult,
    ResumeVisualQAStatus,
    ResumeVisualQAVerdict,
    ReviewResumeVisualQAResult,
)


RESUME_LAYOUT_REVISION_CONTRACT_VERSION = "resume-layout-revision-v1"
RESUME_LAYOUT_REVISION_POLICY_VERSION = "resume-layout-revision-policy-v1"

RESUME_LAYOUT_REVISION_AGENT_POLICY = """Layout Revision Agent policy (static, non-negotiable):

You adjust typography so an already-approved resume fits and reads well. You
are not a writer, an editor or a fact checker.

You may change only presentation:
- Page margins and geometry.
- Section, bullet, list and paragraph spacing.
- Line spacing and leading.
- Font size within the allowed range.
- Header and title spacing.
- Alignment, and existing safe macro definitions.

You must never:
- Change anything between the JOBOPS-CONTENT sentinels. That region is
  copied byte for byte and any edit is rejected.
- Reword, shorten, delete or reorder a bullet or section.
- Add any candidate fact.
- Hide text, push it off the page, make it transparent or invisible, or use
  zero, negative or clipped sizes.
- Use a font size below the allowed minimum to pass a page check.
- Emit shell escape, file reads or writes, external programs, or any
  dependency the registry does not manage.

Return the complete LaTeX document as typed structured output.
"""

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RUN_ID_PATTERN = re.compile(r"^resume-layout-revision-run-[a-f0-9]{64}$")
_RECORD_ID_PATTERN = re.compile(r"^resume-layout-revision-[a-f0-9]{64}$")
_VERSION_ID_PATTERN = re.compile(r"^resume-latex-version-[a-f0-9]{64}$")

_FONTSIZE_PATTERN = re.compile(
    r"\\fontsize\s*\{\s*([0-9]*\.?[0-9]+)\s*(?:pt)?\s*\}"
)
_MARGIN_PATTERN = re.compile(
    r"margin\s*=\s*([0-9]*\.?[0-9]+)\s*(in|cm|mm|pt)", re.IGNORECASE
)
#: Size macros whose nominal point size sits below any sane resume minimum.
_UNSAFE_SIZE_MACROS = ("\\tiny", "\\scriptsize")
#: Constructs that hide content instead of fitting it.
_HIDING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("white text", re.compile(r"\\(?:text)?color\s*(?:\[[^\]]*\])?\s*\{\s*white\s*\}")),
    ("transparency", re.compile(r"\\(?:pdfliteral|transparent|opacity)\b")),
    ("clipping", re.compile(r"\\clip\b|\bclip\s*=\s*true|\\trimbox\b")),
    ("off-page shifting", re.compile(r"\\hoffset\s*=?\s*-|\\voffset\s*=?\s*-")),
    ("zero-size boxes", re.compile(r"\\resizebox\s*\{\s*0+(?:\.0+)?\s*\w*\s*\}")),
    ("phantom content", re.compile(r"\\phantom\b|\\hphantom\b|\\vphantom\b")),
)
_UNIT_TO_INCHES = {"in": 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4, "pt": 1 / 72.27}


class ResumeLayoutRevisionStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_REQUIRED = "NOT_REQUIRED"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    DEFERRED_ATTEMPTS_EXHAUSTED = "DEFERRED_ATTEMPTS_EXHAUSTED"
    FAILED = "FAILED"


class ResumeLayoutAttemptOutcome(str, Enum):
    PASSED = "PASSED"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    AGENT_OUTPUT_REJECTED = "AGENT_OUTPUT_REJECTED"
    RENDER_UNAVAILABLE = "RENDER_UNAVAILABLE"
    VERSION_REGISTRATION_FAILED = "VERSION_REGISTRATION_FAILED"
    COMPILATION_STOPPED = "COMPILATION_STOPPED"
    VISUAL_QA_DEFERRED = "VISUAL_QA_DEFERRED"
    VISUAL_QA_FAILED = "VISUAL_QA_FAILED"


class ResumeLayoutRevisionWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeLayoutRevisionReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeLayoutRevisionFailureReason(str, Enum):
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


class ResumeLayoutRevisionAgentUnavailableError(RuntimeError):
    """Raised when the bounded layout revision Agent cannot answer."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the layout revision contract")
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


def _parse_timestamp(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return _require_aware(
        name, datetime.fromisoformat(value.replace("Z", "+00:00"))
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
class ResumeLayoutRevisionPolicy:
    """Bounded retries and the typography floors a revision may not cross."""

    policy_version: str = RESUME_LAYOUT_REVISION_POLICY_VERSION
    max_attempts: int = 3
    min_font_size_pt: float = 9.0
    max_font_size_pt: float = 14.0
    min_margin_inches: float = 0.4

    def __post_init__(self) -> None:
        if self.policy_version != RESUME_LAYOUT_REVISION_POLICY_VERSION:
            raise ValueError("layout revision policy version is unsupported")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts is outside the policy contract")
        if not 1 <= float(self.min_font_size_pt) <= float(
            self.max_font_size_pt
        ) <= 72:
            raise ValueError("font size bounds are invalid")
        if not 0 < float(self.min_margin_inches) <= 3:
            raise ValueError("min_margin_inches is outside the policy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "max_font_size_pt": float(self.max_font_size_pt),
            "min_font_size_pt": float(self.min_font_size_pt),
            "min_margin_inches": float(self.min_margin_inches),
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class ResumeLayoutRevisionAgentMetadata:
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
class LayoutRevisionFindingView:
    finding_id: str
    finding_type: str
    severity: str
    page_number: int
    explanation: str


@dataclass(frozen=True, slots=True)
class LayoutRevisionPageView:
    page_number: int
    width_px: int
    height_px: int
    image_format: str
    image_bytes: bytes


@dataclass(frozen=True, slots=True)
class ResumeLayoutRevisionContext:
    subject_id: str
    attempt_number: int
    latex_source: str
    pages: tuple[LayoutRevisionPageView, ...]
    findings: tuple[LayoutRevisionFindingView, ...]
    visual_qa_policy: Mapping[str, Any]
    layout_revision_policy: Mapping[str, Any]
    user_preparation_instructions: str | None
    agent_policy: str


@dataclass(frozen=True, slots=True)
class ResumeLayoutRevisionAgentOutput:
    latex_source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.latex_source, str)
            or not self.latex_source.strip()
        ):
            raise ValueError("latex_source must be non-empty text")


@runtime_checkable
class ResumeLayoutRevisionAgentPort(Protocol):
    async def revise(
        self, context: ResumeLayoutRevisionContext
    ) -> ResumeLayoutRevisionAgentOutput:
        """Adjust typography only; the content region is copied byte for byte."""


class _RevisionRejected(ValueError):
    """The revised LaTeX failed a deterministic content or safety check."""


def validate_revised_layout(
    revised: str,
    *,
    base_source: str,
    policy: ResumeLayoutRevisionPolicy,
) -> str:
    """Prove the revision changed presentation only, and stayed inside policy."""

    if not isinstance(revised, str) or not revised.strip():
        raise _RevisionRejected("the revised LaTeX is empty")
    try:
        validate_latex_source(revised)
    except ResumeLatexCapabilityError as exc:
        raise _RevisionRejected(
            f"the revised LaTeX requests {exc.capability.value}"
        ) from exc
    except (TypeError, ValueError) as exc:
        raise _RevisionRejected(
            "the revised LaTeX is outside the source contract"
        ) from exc
    for required in (
        "\\documentclass",
        "\\begin{document}",
        "\\end{document}",
    ):
        if required not in revised:
            raise _RevisionRejected(
                f"the revised LaTeX is missing {required}"
            )
    if not uses_controlled_markers(revised):
        raise _RevisionRejected(
            "the revised LaTeX has no single controlled region"
        )
    try:
        _, base_region, _ = split_controlled_region(base_source)
        _, revised_region, _ = split_controlled_region(revised)
    except ResumeLatexMarkerError as exc:
        raise _RevisionRejected(str(exc)) from exc
    if revised_region != base_region:
        raise _RevisionRejected(
            "the controlled content region was modified"
        )
    try:
        if parse_markers(revised) != parse_markers(base_source):
            raise _RevisionRejected(
                "the controlled markers changed"
            )
    except ResumeLatexMarkerError as exc:
        raise _RevisionRejected(str(exc)) from exc
    if unmanaged_file_dependencies(revised) != unmanaged_file_dependencies(
        base_source
    ):
        raise _RevisionRejected(
            "the revision introduced an unmanaged file dependency"
        )
    for label, pattern in _HIDING_PATTERNS:
        if pattern.search(revised) and not pattern.search(base_source):
            raise _RevisionRejected(
                f"the revision introduced {label}"
            )
    for macro in _UNSAFE_SIZE_MACROS:
        if macro in revised and macro not in base_source:
            raise _RevisionRejected(
                f"the revision introduced {macro.strip()}, below the "
                "allowed font size"
            )
    for match in _FONTSIZE_PATTERN.finditer(revised):
        size = float(match.group(1))
        if size < policy.min_font_size_pt or size > policy.max_font_size_pt:
            raise _RevisionRejected(
                f"a font size of {size}pt is outside the allowed range"
            )
    for match in _MARGIN_PATTERN.finditer(revised):
        inches = float(match.group(1)) * _UNIT_TO_INCHES[
            match.group(2).lower()
        ]
        if inches < policy.min_margin_inches:
            raise _RevisionRejected(
                f"a margin of {inches:.2f}in is below the allowed minimum"
            )
    return revised


@dataclass(frozen=True, slots=True)
class ResumeLayoutRevisionRecord:
    """Build provenance for one revised version; satisfies LatexBuildProvenance."""

    record_id: str
    contract_version: str
    revision_binding: str
    subject_id: str
    application_plan_id: str
    tailored_resume_draft_id: str
    tailored_resume_draft_hash: str
    fact_qa_result_id: str
    fact_qa_result_hash: str
    source_visual_qa_result_id: str
    attempt_number: int
    latex_version_id: str
    latex_source_sha256: str
    root_family_id: str
    parent_version_id: str
    template_id: str | None
    template_sha256: str | None
    agent_version: str
    prompt_version: str
    model_id: str
    policy_version: str
    created_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != RESUME_LAYOUT_REVISION_CONTRACT_VERSION:
            raise ValueError("layout revision contract is unsupported")
        binding = _require_hash("revision_binding", self.revision_binding)
        if (
            not isinstance(self.record_id, str)
            or _RECORD_ID_PATTERN.fullmatch(self.record_id) is None
            or self.record_id != f"resume-layout-revision-{binding}"
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
            "source_visual_qa_result_id",
            self.source_visual_qa_result_id,
            maximum=160,
        )
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt_number must be a positive integer")
        _clean_text(
            "latex_version_id", self.latex_version_id, maximum=160
        )
        _require_hash("latex_source_sha256", self.latex_source_sha256)
        _clean_text("root_family_id", self.root_family_id, maximum=160)
        if (
            not isinstance(self.parent_version_id, str)
            or _VERSION_ID_PATTERN.fullmatch(self.parent_version_id) is None
        ):
            raise ValueError("a revision must record its parent version")
        if self.template_id is not None or self.template_sha256 is not None:
            raise ValueError("a revision derives from a parent, not a template")
        _clean_text("agent_version", self.agent_version, maximum=80)
        _clean_text("prompt_version", self.prompt_version, maximum=80)
        _clean_text("model_id", self.model_id, maximum=160)
        if self.policy_version != RESUME_LAYOUT_REVISION_POLICY_VERSION:
            raise ValueError("layout revision policy version is unsupported")
        object.__setattr__(self, "contract_version", contract)
        _require_aware("created_at", self.created_at)

    @property
    def build_provenance_binding(self) -> str:
        return self.revision_binding

    def content_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "contract_version": self.contract_version,
            "revision_binding": self.revision_binding,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "tailored_resume_draft_id": self.tailored_resume_draft_id,
            "tailored_resume_draft_hash": self.tailored_resume_draft_hash,
            "fact_qa_result_id": self.fact_qa_result_id,
            "fact_qa_result_hash": self.fact_qa_result_hash,
            "source_visual_qa_result_id": self.source_visual_qa_result_id,
            "attempt_number": self.attempt_number,
            "latex_version_id": self.latex_version_id,
            "latex_source_sha256": self.latex_source_sha256,
            "root_family_id": self.root_family_id,
            "parent_version_id": self.parent_version_id,
            "template_id": self.template_id,
            "template_sha256": self.template_sha256,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "policy_version": self.policy_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "created_at": _rfc3339(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeLayoutRevisionAttempt:
    attempt_number: int
    input_latex_version_id: str
    input_compilation_record_id: str
    input_visual_qa_result_id: str
    blocking_finding_ids: tuple[str, ...]
    agent_version: str
    prompt_version: str
    model_id: str
    output_latex_version_id: str | None
    output_compilation_record_id: str | None
    output_visual_qa_result_id: str | None
    outcome: ResumeLayoutAttemptOutcome
    detail: str

    def __post_init__(self) -> None:
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("attempt_number must be a positive integer")
        for name in (
            "input_latex_version_id",
            "input_compilation_record_id",
            "input_visual_qa_result_id",
        ):
            _clean_text(name, getattr(self, name), maximum=160)
        if not isinstance(self.blocking_finding_ids, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.blocking_finding_ids
        ):
            raise TypeError("blocking_finding_ids must be identifiers")
        _clean_text("agent_version", self.agent_version, maximum=80)
        _clean_text("prompt_version", self.prompt_version, maximum=80)
        _clean_text("model_id", self.model_id, maximum=160)
        for name in (
            "output_latex_version_id",
            "output_compilation_record_id",
            "output_visual_qa_result_id",
        ):
            _optional_text(name, getattr(self, name), maximum=160)
        outcome = ResumeLayoutAttemptOutcome(self.outcome)
        object.__setattr__(self, "outcome", outcome)
        _clean_text("detail", self.detail, maximum=2_000)
        if outcome in {
            ResumeLayoutAttemptOutcome.PASSED,
            ResumeLayoutAttemptOutcome.REVISION_REQUIRED,
        } and (
            self.output_latex_version_id is None
            or self.output_compilation_record_id is None
            or self.output_visual_qa_result_id is None
        ):
            raise ValueError("a reviewed attempt must record all outputs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_number": self.attempt_number,
            "input_latex_version_id": self.input_latex_version_id,
            "input_compilation_record_id": (
                self.input_compilation_record_id
            ),
            "input_visual_qa_result_id": self.input_visual_qa_result_id,
            "blocking_finding_ids": list(self.blocking_finding_ids),
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "output_latex_version_id": self.output_latex_version_id,
            "output_compilation_record_id": (
                self.output_compilation_record_id
            ),
            "output_visual_qa_result_id": (
                self.output_visual_qa_result_id
            ),
            "outcome": self.outcome.value,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ResumeLayoutRevisionRun:
    run_id: str
    contract_version: str
    run_binding: str
    subject_id: str
    application_plan_id: str
    tailored_resume_draft_id: str
    tailored_resume_draft_hash: str
    initial_visual_qa_result_id: str
    initial_visual_qa_result_hash: str
    initial_latex_version_id: str
    initial_latex_source_sha256: str
    policy_version: str
    max_attempts: int
    attempts: tuple[ResumeLayoutRevisionAttempt, ...]
    final_latex_version_id: str
    final_compilation_record_id: str
    final_visual_qa_result_id: str
    final_status: ResumeLayoutRevisionStatus
    run_content_hash: str
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != RESUME_LAYOUT_REVISION_CONTRACT_VERSION:
            raise ValueError("layout revision contract is unsupported")
        binding = _require_hash("run_binding", self.run_binding)
        if (
            not isinstance(self.run_id, str)
            or _RUN_ID_PATTERN.fullmatch(self.run_id) is None
            or self.run_id != f"resume-layout-revision-run-{binding}"
        ):
            raise ValueError("run_id does not match its binding")
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
            "initial_visual_qa_result_id",
            self.initial_visual_qa_result_id,
            maximum=160,
        )
        _require_hash(
            "initial_visual_qa_result_hash",
            self.initial_visual_qa_result_hash,
        )
        _clean_text(
            "initial_latex_version_id",
            self.initial_latex_version_id,
            maximum=160,
        )
        _require_hash(
            "initial_latex_source_sha256", self.initial_latex_source_sha256
        )
        if self.policy_version != RESUME_LAYOUT_REVISION_POLICY_VERSION:
            raise ValueError("layout revision policy version is unsupported")
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if not isinstance(self.attempts, tuple) or any(
            not isinstance(item, ResumeLayoutRevisionAttempt)
            for item in self.attempts
        ):
            raise TypeError("attempts must be a typed tuple")
        if len(self.attempts) > self.max_attempts:
            raise ValueError("attempts exceed the policy maximum")
        if tuple(item.attempt_number for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("attempts must be numbered contiguously")
        for name in (
            "final_latex_version_id",
            "final_compilation_record_id",
            "final_visual_qa_result_id",
        ):
            _clean_text(name, getattr(self, name), maximum=160)
        status = ResumeLayoutRevisionStatus(self.final_status)
        object.__setattr__(self, "final_status", status)
        if status is ResumeLayoutRevisionStatus.CREATED and (
            not self.attempts
            or self.attempts[-1].outcome
            is not ResumeLayoutAttemptOutcome.PASSED
        ):
            raise ValueError("a created run must end in a passing attempt")
        if status is ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED and (
            len(self.attempts) != self.max_attempts
        ):
            raise ValueError("an exhausted run must use every attempt")
        object.__setattr__(self, "contract_version", contract)
        started = _require_aware("started_at", self.started_at)
        completed = _require_aware("completed_at", self.completed_at)
        if completed < started:
            raise ValueError("completed_at precedes started_at")
        content_hash = _require_hash(
            "run_content_hash", self.run_content_hash
        )
        if content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("run content hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "contract_version": self.contract_version,
            "run_binding": self.run_binding,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "tailored_resume_draft_id": self.tailored_resume_draft_id,
            "tailored_resume_draft_hash": self.tailored_resume_draft_hash,
            "initial_visual_qa_result_id": (
                self.initial_visual_qa_result_id
            ),
            "initial_visual_qa_result_hash": (
                self.initial_visual_qa_result_hash
            ),
            "initial_latex_version_id": self.initial_latex_version_id,
            "initial_latex_source_sha256": (
                self.initial_latex_source_sha256
            ),
            "policy_version": self.policy_version,
            "max_attempts": self.max_attempts,
            "attempts": [item.to_dict() for item in self.attempts],
            "final_latex_version_id": self.final_latex_version_id,
            "final_compilation_record_id": (
                self.final_compilation_record_id
            ),
            "final_visual_qa_result_id": self.final_visual_qa_result_id,
            "final_status": self.final_status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "run_content_hash": self.run_content_hash,
            "started_at": _rfc3339(self.started_at),
            "completed_at": _rfc3339(self.completed_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeLayoutRevisionWriteResult:
    status: ResumeLayoutRevisionWriteStatus
    run: ResumeLayoutRevisionRun | None
    reason_code: ResumeLayoutRevisionFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ResumeLayoutRevisionWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLayoutRevisionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ResumeLayoutRevisionWriteStatus.CREATED,
            ResumeLayoutRevisionWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.run, ResumeLayoutRevisionRun)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful revision write is invalid")
        elif self.run is not None or self.reason_code is None:
            raise ValueError("failed revision write is invalid")


@dataclass(frozen=True, slots=True)
class ResumeLayoutRevisionReadResult:
    status: ResumeLayoutRevisionReadStatus
    run: ResumeLayoutRevisionRun | None
    reason_code: ResumeLayoutRevisionFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeLayoutRevisionReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLayoutRevisionFailureReason(self.reason_code),
            )
        if status is ResumeLayoutRevisionReadStatus.FOUND:
            if (
                not isinstance(self.run, ResumeLayoutRevisionRun)
                or self.reason_code is not None
            ):
                raise ValueError("found revision read is invalid")
        elif status is ResumeLayoutRevisionReadStatus.NOT_FOUND:
            if self.run is not None or self.reason_code is not None:
                raise ValueError("not-found revision read is invalid")
        elif (
            self.run is not None
            or self.reason_code
            is not ResumeLayoutRevisionFailureReason.RECORD_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure revision read is invalid")


@runtime_checkable
class ResumeLayoutRevisionRepository(Protocol):
    def save(
        self, run: ResumeLayoutRevisionRun
    ) -> ResumeLayoutRevisionWriteResult:
        """Persist one immutable revision run."""

    def get(
        self, *, subject_id: str, run_id: str
    ) -> ResumeLayoutRevisionReadResult:
        """Read one subject-owned revision run."""


@runtime_checkable
class ResumeLayoutRevisionRecordRepository(Protocol):
    def save(self, record: ResumeLayoutRevisionRecord) -> bool:
        """Persist one immutable revision provenance record."""

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeLatexConstructionReadResult:
        """Read one revision record as LaTeX build provenance."""


def _attempt_from_dict(value: Any) -> ResumeLayoutRevisionAttempt:
    expected = {
        "attempt_number",
        "input_latex_version_id",
        "input_compilation_record_id",
        "input_visual_qa_result_id",
        "blocking_finding_ids",
        "agent_version",
        "prompt_version",
        "model_id",
        "output_latex_version_id",
        "output_compilation_record_id",
        "output_visual_qa_result_id",
        "outcome",
        "detail",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["blocking_finding_ids"], list)
    ):
        raise ValueError("persisted revision attempt is invalid")
    return ResumeLayoutRevisionAttempt(
        attempt_number=value["attempt_number"],
        input_latex_version_id=value["input_latex_version_id"],
        input_compilation_record_id=value["input_compilation_record_id"],
        input_visual_qa_result_id=value["input_visual_qa_result_id"],
        blocking_finding_ids=tuple(value["blocking_finding_ids"]),
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        output_latex_version_id=value["output_latex_version_id"],
        output_compilation_record_id=value[
            "output_compilation_record_id"
        ],
        output_visual_qa_result_id=value["output_visual_qa_result_id"],
        outcome=ResumeLayoutAttemptOutcome(value["outcome"]),
        detail=value["detail"],
    )


def _run_from_dict(value: Any) -> ResumeLayoutRevisionRun:
    expected = {
        "run_id",
        "contract_version",
        "run_binding",
        "subject_id",
        "application_plan_id",
        "tailored_resume_draft_id",
        "tailored_resume_draft_hash",
        "initial_visual_qa_result_id",
        "initial_visual_qa_result_hash",
        "initial_latex_version_id",
        "initial_latex_source_sha256",
        "policy_version",
        "max_attempts",
        "attempts",
        "final_latex_version_id",
        "final_compilation_record_id",
        "final_visual_qa_result_id",
        "final_status",
        "run_content_hash",
        "started_at",
        "completed_at",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["attempts"], list)
    ):
        raise ValueError("persisted revision run is invalid")
    return ResumeLayoutRevisionRun(
        run_id=value["run_id"],
        contract_version=value["contract_version"],
        run_binding=value["run_binding"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        tailored_resume_draft_id=value["tailored_resume_draft_id"],
        tailored_resume_draft_hash=value["tailored_resume_draft_hash"],
        initial_visual_qa_result_id=value["initial_visual_qa_result_id"],
        initial_visual_qa_result_hash=value[
            "initial_visual_qa_result_hash"
        ],
        initial_latex_version_id=value["initial_latex_version_id"],
        initial_latex_source_sha256=value["initial_latex_source_sha256"],
        policy_version=value["policy_version"],
        max_attempts=value["max_attempts"],
        attempts=tuple(
            _attempt_from_dict(item) for item in value["attempts"]
        ),
        final_latex_version_id=value["final_latex_version_id"],
        final_compilation_record_id=value["final_compilation_record_id"],
        final_visual_qa_result_id=value["final_visual_qa_result_id"],
        final_status=ResumeLayoutRevisionStatus(value["final_status"]),
        run_content_hash=value["run_content_hash"],
        started_at=_parse_timestamp("started_at", value["started_at"]),
        completed_at=_parse_timestamp(
            "completed_at", value["completed_at"]
        ),
    )


def _record_from_dict(value: Any) -> ResumeLayoutRevisionRecord:
    expected = set(
        ResumeLayoutRevisionRecord.__dataclass_fields__
    )
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted revision record is invalid")
    return ResumeLayoutRevisionRecord(
        **{
            key: (
                _parse_timestamp("created_at", value[key])
                if key == "created_at"
                else value[key]
            )
            for key in expected
        }
    )


class PrivateHomeResumeLayoutRevisionRepository:
    """Immutable, subject-scoped revision runs in Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, run_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        if (
            not isinstance(run_id, str)
            or _RUN_ID_PATTERN.fullmatch(run_id) is None
        ):
            raise ValueError("run_id is invalid")
        return (
            self._home.paths.resume_layout_revision_runs
            / _subject_storage_key(subject)
            / f"{run_id}.json"
        )

    def get(
        self, *, subject_id: str, run_id: str
    ) -> ResumeLayoutRevisionReadResult:
        path = self._path(subject_id, run_id)
        with self._lock:
            if not path.exists():
                return ResumeLayoutRevisionReadResult(
                    status=ResumeLayoutRevisionReadStatus.NOT_FOUND,
                    run=None,
                )
            if path.is_symlink() or not path.is_file():
                return ResumeLayoutRevisionReadResult(
                    status=(
                        ResumeLayoutRevisionReadStatus.INTEGRITY_FAILURE
                    ),
                    run=None,
                    reason_code=(
                        ResumeLayoutRevisionFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            try:
                run = _run_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ResumeLayoutRevisionReadResult(
                    status=(
                        ResumeLayoutRevisionReadStatus.INTEGRITY_FAILURE
                    ),
                    run=None,
                    reason_code=(
                        ResumeLayoutRevisionFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            if (
                run.subject_id != subject_id.strip()
                or run.run_id != run_id
                or path.name != f"{run.run_id}.json"
            ):
                return ResumeLayoutRevisionReadResult(
                    status=(
                        ResumeLayoutRevisionReadStatus.INTEGRITY_FAILURE
                    ),
                    run=None,
                    reason_code=(
                        ResumeLayoutRevisionFailureReason
                        .RECORD_INTEGRITY_FAILURE
                    ),
                )
            return ResumeLayoutRevisionReadResult(
                status=ResumeLayoutRevisionReadStatus.FOUND,
                run=run,
            )

    def save(
        self, run: ResumeLayoutRevisionRun
    ) -> ResumeLayoutRevisionWriteResult:
        if not isinstance(run, ResumeLayoutRevisionRun):
            raise TypeError("run must be a ResumeLayoutRevisionRun")
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
                return ResumeLayoutRevisionWriteResult(
                    status=ResumeLayoutRevisionWriteStatus.FAILED,
                    run=None,
                    reason_code=(
                        ResumeLayoutRevisionFailureReason
                        .RECORD_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return ResumeLayoutRevisionWriteResult(
                    status=ResumeLayoutRevisionWriteStatus.CREATED,
                    run=run,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(subject_id=run.subject_id, run_id=run.run_id)
            if (
                existing.status is ResumeLayoutRevisionReadStatus.FOUND
                and existing.run is not None
                and existing.run.content_dict() == run.content_dict()
            ):
                return ResumeLayoutRevisionWriteResult(
                    status=ResumeLayoutRevisionWriteStatus.UNCHANGED,
                    run=existing.run,
                    reason_code=None,
                    retryable=False,
                )
            return ResumeLayoutRevisionWriteResult(
                status=ResumeLayoutRevisionWriteStatus.FAILED,
                run=None,
                reason_code=(
                    ResumeLayoutRevisionFailureReason
                    .RECORD_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


class PrivateHomeResumeLayoutRevisionRecordRepository:
    """Immutable revision provenance, readable by P2a7 and P2a8a."""

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
            self._home.paths.resume_layout_revision_records
            / _subject_storage_key(subject)
            / f"{record_id}.json"
        )

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeLatexConstructionReadResult:
        try:
            path = self._path(subject_id, record_id)
        except ValueError:
            return ResumeLatexConstructionReadResult(
                status=ResumeLatexConstructionReadStatus.NOT_FOUND,
                record=None,
            )
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
                    reason_code=None,
                )
            try:
                record = _record_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                from .resume_latex_construction import (
                    ResumeLatexConstructionFailureReason,
                )

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
            if record.subject_id != subject_id.strip():
                return ResumeLatexConstructionReadResult(
                    status=ResumeLatexConstructionReadStatus.NOT_FOUND,
                    record=None,
                )
            return ResumeLatexConstructionReadResult(
                status=ResumeLatexConstructionReadStatus.FOUND,
                record=record,
            )

    def save(self, record: ResumeLayoutRevisionRecord) -> bool:
        if not isinstance(record, ResumeLayoutRevisionRecord):
            raise TypeError("record must be a ResumeLayoutRevisionRecord")
        path = self._path(record.subject_id, record.record_id)
        with self._lock:
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
            if created:
                return True
            existing = self.get(
                subject_id=record.subject_id, record_id=record.record_id
            )
            return (
                existing.status is ResumeLatexConstructionReadStatus.FOUND
                and existing.record is not None
                and existing.record.content_dict() == record.content_dict()
            )


class CompositeLatexBuildProvenanceRepository:
    """Serve construction records and revision records through one reader."""

    def __init__(
        self,
        construction_repository: ResumeLatexConstructionRecordRepository,
        revision_repository: PrivateHomeResumeLayoutRevisionRecordRepository,
    ) -> None:
        self._construction = construction_repository
        self._revision = revision_repository

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeLatexConstructionReadResult:
        if record_id.startswith("resume-layout-revision-"):
            return self._revision.get(
                subject_id=subject_id, record_id=record_id
            )
        return self._construction.get(
            subject_id=subject_id, record_id=record_id
        )


@runtime_checkable
class LayoutRevisionCompileStep(Protocol):
    def __call__(
        self,
        *,
        subject_id: str,
        provenance_record_id: str,
        latex_version_id: str,
        now: datetime,
    ) -> CompileResumeLatexResult:
        """Invoke the P2a7 public entry point exactly once."""


@runtime_checkable
class LayoutRevisionReviewStep(Protocol):
    async def __call__(
        self,
        *,
        subject_id: str,
        compilation_record_id: str,
        now: datetime,
    ) -> ReviewResumeVisualQAResult:
        """Invoke the P2a8a public entry point exactly once."""


@dataclass(frozen=True, slots=True)
class ReviseResumeLayoutCommand:
    subject_id: str
    resume_visual_qa_result_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ReviseResumeLayoutResult:
    status: ResumeLayoutRevisionStatus
    subject_id: str
    run_binding: str
    run: ResumeLayoutRevisionRun | None
    write_result: ResumeLayoutRevisionWriteResult | None
    reason_code: ResumeLayoutRevisionFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = ResumeLayoutRevisionStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLayoutRevisionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        persisted = {
            ResumeLayoutRevisionStatus.CREATED,
            ResumeLayoutRevisionStatus.UNCHANGED,
            ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN,
            ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED,
        }
        if status in persisted:
            if (
                not isinstance(self.run, ResumeLayoutRevisionRun)
                or not isinstance(
                    self.write_result, ResumeLayoutRevisionWriteResult
                )
                or self.write_result.run != self.run
                or self.write_result.status
                is ResumeLayoutRevisionWriteStatus.FAILED
                or self.retryable
            ):
                raise ValueError("persisted revision result is invalid")
            if status is ResumeLayoutRevisionStatus.UNCHANGED and (
                self.write_result.status
                is not ResumeLayoutRevisionWriteStatus.UNCHANGED
            ):
                raise ValueError("unchanged revision needs a replay write")
        elif status is ResumeLayoutRevisionStatus.NOT_REQUIRED:
            if (
                self.run is not None
                or self.write_result is not None
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("not-required revision result is invalid")
        elif self.run is not None or self.reason_code is None:
            raise ValueError("failed revision result is invalid")


def _failure(
    command: ReviseResumeLayoutCommand,
    reason: ResumeLayoutRevisionFailureReason,
    *,
    retryable: bool = False,
    run_binding: str = "",
    detail: str | None = None,
) -> ReviseResumeLayoutResult:
    return ReviseResumeLayoutResult(
        status=ResumeLayoutRevisionStatus.FAILED,
        subject_id=(
            command.subject_id
            if isinstance(command.subject_id, str)
            else ""
        ),
        run_binding=run_binding,
        run=None,
        write_result=None,
        reason_code=reason,
        retryable=retryable,
        message=detail or f"Layout revision stopped: {reason.value}.",
    )


def _run_binding(
    *,
    visual_qa: ResumeVisualQAResult,
    version: ResumeLatexVersion,
    draft: TailoredResumeDraft,
    policy: ResumeLayoutRevisionPolicy,
    metadata: ResumeLayoutRevisionAgentMetadata,
) -> str:
    return _canonical_hash(
        {
            "initial_latex_source_sha256": version.source_sha256,
            "initial_latex_version_id": version.latex_version_id,
            "initial_visual_qa_result_hash": (
                visual_qa.result_content_hash
            ),
            "initial_visual_qa_result_id": visual_qa.result_id,
            "layout_revision_policy": policy.to_dict(),
            "renderer_dpi": visual_qa.renderer_dpi,
            "renderer_name": visual_qa.renderer_name,
            "renderer_version": visual_qa.renderer_version,
            "resume_layout_revision_agent_version": (
                metadata.agent_version
            ),
            "resume_layout_revision_contract_version": (
                RESUME_LAYOUT_REVISION_CONTRACT_VERSION
            ),
            "resume_layout_revision_model_id": metadata.model_id,
            "resume_layout_revision_prompt_version": (
                metadata.prompt_version
            ),
            "subject_id": visual_qa.subject_id,
            "tailored_resume_draft_hash": draft.draft_content_hash,
            "tailored_resume_draft_id": draft.draft_id,
            "visual_qa_policy_version": visual_qa.policy_version,
        }
    )


async def revise_resume_layout(
    command: ReviseResumeLayoutCommand,
    *,
    visual_qa_repository: ResumeVisualQARepository,
    compilation_repository: ResumeCompilationRepository,
    latex_version_repository: ResumeLatexVersionRepository,
    provenance_repository: ResumeLatexConstructionRecordRepository,
    revision_record_repository: (
        PrivateHomeResumeLayoutRevisionRecordRepository
    ),
    application_plan_repository: ApplicationPlanRepository,
    draft_repository: TailoredResumeDraftRepository,
    renderer: PdfPageRendererPort,
    agent: ResumeLayoutRevisionAgentPort,
    metadata: ResumeLayoutRevisionAgentMetadata,
    compile_step: LayoutRevisionCompileStep,
    review_step: LayoutRevisionReviewStep,
    revision_repository: ResumeLayoutRevisionRepository,
    policy: ResumeLayoutRevisionPolicy | None = None,
    home: PrivateHome | None = None,
) -> ReviseResumeLayoutResult:
    """Retry typography a bounded number of times, serially, then pass or defer."""

    active_home = home or PrivateHome.discover()
    active_policy = policy or ResumeLayoutRevisionPolicy()
    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        visual_qa_id = _clean_text(
            "resume_visual_qa_result_id",
            command.resume_visual_qa_result_id,
            maximum=160,
        )
        started = _require_aware("now", command.now)
        if not isinstance(active_policy, ResumeLayoutRevisionPolicy):
            raise TypeError("policy must be a ResumeLayoutRevisionPolicy")
        if not isinstance(metadata, ResumeLayoutRevisionAgentMetadata):
            raise TypeError("metadata must be revision Agent metadata")
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, ResumeLayoutRevisionFailureReason.INVALID_REQUEST
        )

    try:
        qa_read = visual_qa_repository.get(
            subject_id=subject_id, result_id=visual_qa_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.VISUAL_QA_INTEGRITY_FAILURE,
        )
    if qa_read.status is ResumeVisualQAReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeLayoutRevisionFailureReason.VISUAL_QA_NOT_FOUND
        )
    if (
        qa_read.status is not ResumeVisualQAReadStatus.FOUND
        or not isinstance(qa_read.result, ResumeVisualQAResult)
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.VISUAL_QA_INTEGRITY_FAILURE,
        )
    initial_qa = qa_read.result
    if initial_qa.subject_id != subject_id:
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.VISUAL_QA_BINDING_MISMATCH,
        )

    if initial_qa.verdict is ResumeVisualQAVerdict.PASSED:
        return ReviseResumeLayoutResult(
            status=ResumeLayoutRevisionStatus.NOT_REQUIRED,
            subject_id=subject_id,
            run_binding="",
            run=None,
            write_result=None,
            reason_code=None,
            retryable=False,
            message="Visual QA already passed; no layout revision is needed.",
        )

    try:
        compilation_read = compilation_repository.get(
            subject_id=subject_id,
            record_id=initial_qa.compilation_record_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .COMPILATION_INTEGRITY_FAILURE,
        )
    if compilation_read.status is ResumeCompilationReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.COMPILATION_NOT_FOUND,
        )
    if (
        compilation_read.status is not ResumeCompilationReadStatus.FOUND
        or not isinstance(compilation_read.record, ResumeCompilationRecord)
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .COMPILATION_INTEGRITY_FAILURE,
        )
    compilation = compilation_read.record
    if (
        compilation.subject_id != subject_id
        or compilation.record_id != initial_qa.compilation_record_id
        or compilation.compilation_binding
        != initial_qa.compilation_binding
        or compilation.pdf_sha256 != initial_qa.pdf_sha256
        or compilation.latex_version_id != initial_qa.latex_version_id
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .COMPILATION_BINDING_MISMATCH,
        )

    try:
        version_read = latex_version_repository.get(
            subject_id=subject_id,
            latex_version_id=compilation.latex_version_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .LATEX_VERSION_INTEGRITY_FAILURE,
        )
    if version_read.status is ResumeLatexVersionReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.LATEX_VERSION_NOT_FOUND,
        )
    if (
        version_read.status is not ResumeLatexVersionReadStatus.FOUND
        or not isinstance(version_read.version, ResumeLatexVersion)
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .LATEX_VERSION_INTEGRITY_FAILURE,
        )
    version = version_read.version
    if (
        version.subject_id != subject_id
        or version.source_sha256 != initial_qa.latex_source_sha256
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .LATEX_VERSION_BINDING_MISMATCH,
        )

    try:
        provenance_read = provenance_repository.get(
            subject_id=subject_id,
            record_id=compilation.construction_record_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .PROVENANCE_INTEGRITY_FAILURE,
        )
    if provenance_read.status is ResumeLatexConstructionReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.PROVENANCE_NOT_FOUND,
        )
    if (
        provenance_read.status
        is not ResumeLatexConstructionReadStatus.FOUND
        or not isinstance(provenance_read.record, LatexBuildProvenance)
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .PROVENANCE_INTEGRITY_FAILURE,
        )
    provenance = provenance_read.record
    if (
        provenance.subject_id != subject_id
        or provenance.latex_version_id != version.latex_version_id
        or provenance.latex_source_sha256 != version.source_sha256
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .PROVENANCE_BINDING_MISMATCH,
        )

    try:
        draft_read = draft_repository.get(
            subject_id=subject_id,
            draft_id=provenance.tailored_resume_draft_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    if draft_read.status is TailoredResumeDraftReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeLayoutRevisionFailureReason.DRAFT_NOT_FOUND
        )
    if (
        draft_read.status is not TailoredResumeDraftReadStatus.FOUND
        or not isinstance(draft_read.draft, TailoredResumeDraft)
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    draft = draft_read.draft
    if (
        draft.subject_id != subject_id
        or draft.draft_id != initial_qa.tailored_resume_draft_id
        or draft.draft_content_hash
        != initial_qa.tailored_resume_draft_hash
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.DRAFT_BINDING_MISMATCH,
        )

    try:
        plan_read = application_plan_repository.get(
            draft.application_plan_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.APPLICATION_PLAN_NOT_FOUND,
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_read.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_read.plan

    binding = _run_binding(
        visual_qa=initial_qa,
        version=version,
        draft=draft,
        policy=active_policy,
        metadata=metadata,
    )
    run_id = f"resume-layout-revision-run-{binding}"
    try:
        existing = revision_repository.get(
            subject_id=subject_id, run_id=run_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.RECORD_INTEGRITY_FAILURE,
            run_binding=binding,
        )
    if existing.status is ResumeLayoutRevisionReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.RECORD_INTEGRITY_FAILURE,
            run_binding=binding,
        )
    if (
        existing.status is ResumeLayoutRevisionReadStatus.FOUND
        and existing.run is not None
    ):
        return ReviseResumeLayoutResult(
            status=ResumeLayoutRevisionStatus.UNCHANGED,
            subject_id=subject_id,
            run_binding=binding,
            run=existing.run,
            write_result=ResumeLayoutRevisionWriteResult(
                status=ResumeLayoutRevisionWriteStatus.UNCHANGED,
                run=existing.run,
                reason_code=None,
                retryable=False,
            ),
            reason_code=None,
            retryable=False,
            message=(
                "The existing layout revision run is unchanged with status "
                f"{existing.run.final_status.value}."
            ),
        )

    if initial_qa.verdict is ResumeVisualQAVerdict.DEFERRED:
        return _persist_run(
            command=command,
            repository=revision_repository,
            binding=binding,
            subject_id=subject_id,
            plan=plan,
            draft=draft,
            initial_qa=initial_qa,
            version=version,
            policy=active_policy,
            attempts=(),
            final_version_id=version.latex_version_id,
            final_compilation_id=compilation.record_id,
            final_qa_id=initial_qa.result_id,
            status=ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN,
            reason=ResumeLayoutRevisionFailureReason.VISUAL_QA_DEFERRED,
            message=(
                "Visual QA deferred this resume, so no automatic layout "
                "revision was attempted."
            ),
            started=started,
            completed=started,
        )

    current_version = version
    current_compilation = compilation
    current_qa = initial_qa
    attempts: list[ResumeLayoutRevisionAttempt] = []
    stop_status: ResumeLayoutRevisionStatus | None = None
    stop_reason: ResumeLayoutRevisionFailureReason | None = None
    stop_message = ""

    for attempt_number in range(1, active_policy.max_attempts + 1):
        blocking = tuple(
            item.finding_id
            for item in current_qa.findings
            if item.severity is ResumeVisualQAFindingSeverity.BLOCKING
        )
        try:
            source_path = active_home.contained_path(
                current_version.source_reference
            )
            base_source = source_path.read_text(encoding="utf-8")
        except (OSError, PrivateHomeError, UnicodeDecodeError, ValueError):
            return _failure(
                command,
                ResumeLayoutRevisionFailureReason.SOURCE_UNREADABLE,
                run_binding=binding,
            )
        try:
            pdf_path = active_home.contained_path(
                current_compilation.pdf_reference
            )
            pdf_bytes = pdf_path.read_bytes()
            pages = renderer.render(pdf_bytes)
            if (
                not isinstance(pages, tuple)
                or not pages
                or any(not isinstance(item, RenderedPage) for item in pages)
            ):
                raise PdfRendererUnavailableError("invalid page set")
        except (
            PdfRendererUnavailableError,
            OSError,
            PrivateHomeError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            attempts.append(
                _attempt(
                    attempt_number,
                    current_version,
                    current_compilation,
                    current_qa,
                    blocking,
                    metadata,
                    outcome=ResumeLayoutAttemptOutcome.RENDER_UNAVAILABLE,
                    detail="The current PDF could not be rendered.",
                )
            )
            stop_status = ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
            stop_reason = (
                ResumeLayoutRevisionFailureReason.RENDERER_UNAVAILABLE
            )
            stop_message = (
                "The PDF could not be rendered for layout revision."
            )
            break

        context = ResumeLayoutRevisionContext(
            subject_id=subject_id,
            attempt_number=attempt_number,
            latex_source=base_source,
            pages=tuple(
                LayoutRevisionPageView(
                    page_number=page.page_number,
                    width_px=page.width_px,
                    height_px=page.height_px,
                    image_format=page.image_format,
                    image_bytes=page.image_bytes,
                )
                for page in pages
            ),
            findings=tuple(
                LayoutRevisionFindingView(
                    finding_id=item.finding_id,
                    finding_type=item.finding_type.value,
                    severity=item.severity.value,
                    page_number=item.page_number,
                    explanation=item.explanation,
                )
                for item in current_qa.findings
            ),
            visual_qa_policy={
                "max_pages": current_qa.max_pages,
                "policy_version": current_qa.policy_version,
            },
            layout_revision_policy=active_policy.to_dict(),
            user_preparation_instructions=(
                plan.user_preparation_instructions
            ),
            agent_policy=RESUME_LAYOUT_REVISION_AGENT_POLICY,
        )
        try:
            output = await agent.revise(context)
        except TimeoutError:
            return _failure(
                command,
                ResumeLayoutRevisionFailureReason.AGENT_TIMEOUT,
                retryable=True,
                run_binding=binding,
            )
        except ResumeLayoutRevisionAgentUnavailableError:
            return _failure(
                command,
                ResumeLayoutRevisionFailureReason.AGENT_UNAVAILABLE,
                retryable=True,
                run_binding=binding,
            )
        except Exception:
            return _failure(
                command,
                ResumeLayoutRevisionFailureReason.AGENT_UNAVAILABLE,
                retryable=True,
                run_binding=binding,
            )

        try:
            if not isinstance(output, ResumeLayoutRevisionAgentOutput):
                raise _RevisionRejected(
                    "the Agent did not return a typed structured result"
                )
            revised = validate_revised_layout(
                output.latex_source,
                base_source=base_source,
                policy=active_policy,
            )
        except _RevisionRejected as rejection:
            attempts.append(
                _attempt(
                    attempt_number,
                    current_version,
                    current_compilation,
                    current_qa,
                    blocking,
                    metadata,
                    outcome=(
                        ResumeLayoutAttemptOutcome.AGENT_OUTPUT_REJECTED
                    ),
                    detail=str(rejection),
                )
            )
            stop_status = ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
            stop_reason = (
                ResumeLayoutRevisionFailureReason.REVISION_OUTPUT_UNSAFE
            )
            stop_message = (
                f"The layout revision was rejected: {rejection}."
            )
            break
        except (AttributeError, TypeError, ValueError):
            attempts.append(
                _attempt(
                    attempt_number,
                    current_version,
                    current_compilation,
                    current_qa,
                    blocking,
                    metadata,
                    outcome=(
                        ResumeLayoutAttemptOutcome.AGENT_OUTPUT_REJECTED
                    ),
                    detail="The revision output could not be validated.",
                )
            )
            stop_status = ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
            stop_reason = (
                ResumeLayoutRevisionFailureReason.REVISION_OUTPUT_UNSAFE
            )
            stop_message = "The layout revision could not be validated."
            break

        registration = register_resume_latex_version(
            RegisterResumeLatexVersionCommand(
                subject_id=subject_id,
                source_kind=ResumeLatexSourceKind.AI_REVISED,
                now=started,
                latex_source=revised,
                parent_version_id=current_version.latex_version_id,
                source_resume_id=current_version.source_resume_id,
                tailored_resume_draft_id=draft.draft_id,
                tailored_resume_draft_hash=draft.draft_content_hash,
                fact_qa_result_id=provenance.fact_qa_result_id,
                fact_qa_result_hash=provenance.fact_qa_result_hash,
            ),
            home=active_home,
            repository=latex_version_repository,
        )
        if (
            registration.status is RegisterResumeLatexVersionStatus.FAILED
            or registration.version is None
        ):
            attempts.append(
                _attempt(
                    attempt_number,
                    current_version,
                    current_compilation,
                    current_qa,
                    blocking,
                    metadata,
                    outcome=(
                        ResumeLayoutAttemptOutcome
                        .VERSION_REGISTRATION_FAILED
                    ),
                    detail="The revised version could not be registered.",
                )
            )
            stop_status = ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
            stop_reason = (
                ResumeLayoutRevisionFailureReason
                .VERSION_REGISTRATION_FAILED
            )
            stop_message = "The revised LaTeX version could not be stored."
            break
        revised_version = registration.version

        revision_binding = _canonical_hash(
            {
                "attempt_number": attempt_number,
                "latex_source_sha256": revised_version.source_sha256,
                "latex_version_id": revised_version.latex_version_id,
                "run_binding": binding,
            }
        )
        record = ResumeLayoutRevisionRecord(
            record_id=f"resume-layout-revision-{revision_binding}",
            contract_version=RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
            revision_binding=revision_binding,
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            tailored_resume_draft_id=draft.draft_id,
            tailored_resume_draft_hash=draft.draft_content_hash,
            fact_qa_result_id=provenance.fact_qa_result_id,
            fact_qa_result_hash=provenance.fact_qa_result_hash,
            source_visual_qa_result_id=current_qa.result_id,
            attempt_number=attempt_number,
            latex_version_id=revised_version.latex_version_id,
            latex_source_sha256=revised_version.source_sha256,
            root_family_id=revised_version.root_family_id,
            parent_version_id=current_version.latex_version_id,
            template_id=None,
            template_sha256=None,
            agent_version=metadata.agent_version,
            prompt_version=metadata.prompt_version,
            model_id=metadata.model_id,
            policy_version=active_policy.policy_version,
            created_at=started,
        )
        try:
            if not revision_record_repository.save(record):
                raise ValueError("revision provenance conflict")
        except (OSError, PrivateHomeError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                ResumeLayoutRevisionFailureReason
                .RECORD_PERSISTENCE_FAILED,
                retryable=True,
                run_binding=binding,
            )

        compiled = compile_step(
            subject_id=subject_id,
            provenance_record_id=record.record_id,
            latex_version_id=revised_version.latex_version_id,
            now=started,
        )
        if (
            compiled.status is not ResumeCompilationStatus.CREATED
            and compiled.status is not ResumeCompilationStatus.UNCHANGED
        ):
            attempts.append(
                _attempt(
                    attempt_number,
                    current_version,
                    current_compilation,
                    current_qa,
                    blocking,
                    metadata,
                    outcome=(
                        ResumeLayoutAttemptOutcome.COMPILATION_STOPPED
                    ),
                    detail=(
                        "Compilation stopped: "
                        f"{compiled.status.value}."
                    ),
                    output_version_id=revised_version.latex_version_id,
                )
            )
            stop_status = ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
            stop_reason = (
                ResumeLayoutRevisionFailureReason.COMPILATION_STOPPED
            )
            stop_message = (
                "Compilation did not succeed, so revision stopped without "
                "further changes."
            )
            break
        revised_compilation = compiled.record

        reviewed = await review_step(
            subject_id=subject_id,
            compilation_record_id=revised_compilation.record_id,
            now=started,
        )
        if reviewed.status is ResumeVisualQAStatus.DEFERRED_NEEDS_HUMAN:
            attempts.append(
                _attempt(
                    attempt_number,
                    current_version,
                    current_compilation,
                    current_qa,
                    blocking,
                    metadata,
                    outcome=(
                        ResumeLayoutAttemptOutcome.VISUAL_QA_DEFERRED
                    ),
                    detail="Visual QA deferred the revised resume.",
                    output_version_id=revised_version.latex_version_id,
                    output_compilation_id=revised_compilation.record_id,
                    output_qa_id=(
                        reviewed.result.result_id if reviewed.result else None
                    ),
                )
            )
            stop_status = ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
            stop_reason = (
                ResumeLayoutRevisionFailureReason.VISUAL_QA_DEFERRED
            )
            stop_message = "Visual QA deferred the revised resume."
            current_version = revised_version
            current_compilation = revised_compilation
            break
        if (
            reviewed.status
            not in {
                ResumeVisualQAStatus.CREATED,
                ResumeVisualQAStatus.UNCHANGED,
            }
            or reviewed.result is None
        ):
            attempts.append(
                _attempt(
                    attempt_number,
                    current_version,
                    current_compilation,
                    current_qa,
                    blocking,
                    metadata,
                    outcome=ResumeLayoutAttemptOutcome.VISUAL_QA_FAILED,
                    detail=(
                        f"Visual QA stopped: {reviewed.status.value}."
                    ),
                    output_version_id=revised_version.latex_version_id,
                    output_compilation_id=revised_compilation.record_id,
                )
            )
            stop_status = ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN
            stop_reason = (
                ResumeLayoutRevisionFailureReason.VISUAL_QA_FAILED
            )
            stop_message = "Visual QA could not review the revised resume."
            current_version = revised_version
            current_compilation = revised_compilation
            break

        revised_qa = reviewed.result
        passed = revised_qa.verdict is ResumeVisualQAVerdict.PASSED
        attempts.append(
            _attempt(
                attempt_number,
                current_version,
                current_compilation,
                current_qa,
                blocking,
                metadata,
                outcome=(
                    ResumeLayoutAttemptOutcome.PASSED
                    if passed
                    else ResumeLayoutAttemptOutcome.REVISION_REQUIRED
                ),
                detail=(
                    "Visual QA passed the revised layout."
                    if passed
                    else "Visual QA still requires a revision."
                ),
                output_version_id=revised_version.latex_version_id,
                output_compilation_id=revised_compilation.record_id,
                output_qa_id=revised_qa.result_id,
            )
        )
        current_version = revised_version
        current_compilation = revised_compilation
        current_qa = revised_qa
        if passed:
            stop_status = ResumeLayoutRevisionStatus.CREATED
            stop_message = (
                "The layout revision passed visual QA."
            )
            break

    if stop_status is None:
        stop_status = (
            ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED
        )
        stop_reason = ResumeLayoutRevisionFailureReason.ATTEMPTS_EXHAUSTED
        stop_message = (
            "The bounded layout revision attempts were exhausted without "
            "passing visual QA."
        )

    return _persist_run(
        command=command,
        repository=revision_repository,
        binding=binding,
        subject_id=subject_id,
        plan=plan,
        draft=draft,
        initial_qa=initial_qa,
        version=version,
        policy=active_policy,
        attempts=tuple(attempts),
        final_version_id=current_version.latex_version_id,
        final_compilation_id=current_compilation.record_id,
        final_qa_id=current_qa.result_id,
        status=stop_status,
        reason=stop_reason,
        message=stop_message,
        started=started,
        completed=started,
    )


def _attempt(
    attempt_number: int,
    version: ResumeLatexVersion,
    compilation: ResumeCompilationRecord,
    visual_qa: ResumeVisualQAResult,
    blocking: tuple[str, ...],
    metadata: ResumeLayoutRevisionAgentMetadata,
    *,
    outcome: ResumeLayoutAttemptOutcome,
    detail: str,
    output_version_id: str | None = None,
    output_compilation_id: str | None = None,
    output_qa_id: str | None = None,
) -> ResumeLayoutRevisionAttempt:
    return ResumeLayoutRevisionAttempt(
        attempt_number=attempt_number,
        input_latex_version_id=version.latex_version_id,
        input_compilation_record_id=compilation.record_id,
        input_visual_qa_result_id=visual_qa.result_id,
        blocking_finding_ids=blocking,
        agent_version=metadata.agent_version,
        prompt_version=metadata.prompt_version,
        model_id=metadata.model_id,
        output_latex_version_id=output_version_id,
        output_compilation_record_id=output_compilation_id,
        output_visual_qa_result_id=output_qa_id,
        outcome=outcome,
        detail=detail,
    )


def _persist_run(
    *,
    command: ReviseResumeLayoutCommand,
    repository: ResumeLayoutRevisionRepository,
    binding: str,
    subject_id: str,
    plan: ApplicationPlan,
    draft: TailoredResumeDraft,
    initial_qa: ResumeVisualQAResult,
    version: ResumeLatexVersion,
    policy: ResumeLayoutRevisionPolicy,
    attempts: tuple[ResumeLayoutRevisionAttempt, ...],
    final_version_id: str,
    final_compilation_id: str,
    final_qa_id: str,
    status: ResumeLayoutRevisionStatus,
    reason: ResumeLayoutRevisionFailureReason | None,
    message: str,
    started: datetime,
    completed: datetime,
) -> ReviseResumeLayoutResult:
    run_id = f"resume-layout-revision-run-{binding}"
    content = {
        "run_id": run_id,
        "contract_version": RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
        "run_binding": binding,
        "subject_id": subject_id,
        "application_plan_id": plan.plan_id,
        "tailored_resume_draft_id": draft.draft_id,
        "tailored_resume_draft_hash": draft.draft_content_hash,
        "initial_visual_qa_result_id": initial_qa.result_id,
        "initial_visual_qa_result_hash": initial_qa.result_content_hash,
        "initial_latex_version_id": version.latex_version_id,
        "initial_latex_source_sha256": version.source_sha256,
        "policy_version": policy.policy_version,
        "max_attempts": policy.max_attempts,
        "attempts": [item.to_dict() for item in attempts],
        "final_latex_version_id": final_version_id,
        "final_compilation_record_id": final_compilation_id,
        "final_visual_qa_result_id": final_qa_id,
        "final_status": status.value,
    }
    try:
        run = ResumeLayoutRevisionRun(
            run_content_hash=_canonical_hash(content),
            started_at=started,
            completed_at=completed,
            attempts=attempts,
            final_status=status,
            **{
                key: value
                for key, value in content.items()
                if key
                not in {"attempts", "final_status", "run_content_hash"}
            },
        )
    except (TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.RECORD_INTEGRITY_FAILURE,
            run_binding=binding,
        )
    try:
        write_result = repository.save(run)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeLayoutRevisionFailureReason.RECORD_PERSISTENCE_FAILED,
            retryable=True,
            run_binding=binding,
        )
    if write_result.status is ResumeLayoutRevisionWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or ResumeLayoutRevisionFailureReason.RECORD_PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            run_binding=binding,
        )
    return ReviseResumeLayoutResult(
        status=status,
        subject_id=subject_id,
        run_binding=binding,
        run=write_result.run,
        write_result=write_result,
        reason_code=reason,
        retryable=False,
        message=message,
    )


__all__ = [
    "CompositeLatexBuildProvenanceRepository",
    "LayoutRevisionCompileStep",
    "LayoutRevisionFindingView",
    "LayoutRevisionPageView",
    "LayoutRevisionReviewStep",
    "PrivateHomeResumeLayoutRevisionRecordRepository",
    "PrivateHomeResumeLayoutRevisionRepository",
    "RESUME_LAYOUT_REVISION_AGENT_POLICY",
    "RESUME_LAYOUT_REVISION_CONTRACT_VERSION",
    "RESUME_LAYOUT_REVISION_POLICY_VERSION",
    "ResumeLayoutAttemptOutcome",
    "ResumeLayoutRevisionAgentMetadata",
    "ResumeLayoutRevisionAgentOutput",
    "ResumeLayoutRevisionAgentPort",
    "ResumeLayoutRevisionAgentUnavailableError",
    "ResumeLayoutRevisionAttempt",
    "ResumeLayoutRevisionContext",
    "ResumeLayoutRevisionFailureReason",
    "ResumeLayoutRevisionPolicy",
    "ResumeLayoutRevisionReadResult",
    "ResumeLayoutRevisionReadStatus",
    "ResumeLayoutRevisionRecord",
    "ResumeLayoutRevisionRecordRepository",
    "ResumeLayoutRevisionRepository",
    "ResumeLayoutRevisionRun",
    "ResumeLayoutRevisionStatus",
    "ResumeLayoutRevisionWriteResult",
    "ResumeLayoutRevisionWriteStatus",
    "ReviseResumeLayoutCommand",
    "ReviseResumeLayoutResult",
    "revise_resume_layout",
    "validate_revised_layout",
]
