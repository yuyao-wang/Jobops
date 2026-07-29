"""Independent visual QA over one compiled resume PDF; it never edits anything."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError

from .pdf_page_renderer import (
    PdfPageRendererPort,
    PdfRendererDescription,
    PdfRendererUnavailableError,
    RenderedPage,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_compilation import (
    ResumeCompilationReadStatus,
    ResumeCompilationRecord,
    ResumeCompilationRepository,
)
from .resume_latex_construction import (
    LatexBuildProvenance,
    ResumeLatexConstructionReadStatus,
    ResumeLatexConstructionRecordRepository,
)
from .resume_latex_versions import (
    ResumeLatexVersion,
    ResumeLatexVersionReadStatus,
    ResumeLatexVersionRepository,
)
from .resume_tailoring import (
    TailoredBulletChangeType,
    TailoredResumeDraft,
    TailoredResumeDraftReadStatus,
    TailoredResumeDraftRepository,
)


RESUME_VISUAL_QA_CONTRACT_VERSION = "resume-visual-qa-v1"
RESUME_VISUAL_QA_POLICY_VERSION = "resume-visual-qa-policy-v1"

RESUME_VISUAL_QA_AGENT_POLICY = """Resume Visual QA Agent policy (static, non-negotiable):

You inspect rendered resume pages. You do not write LaTeX, edit the PDF or
propose fixes.

Judge only what ordinary code cannot measure reliably:
- Text or elements that visually overlap.
- Type so small the page is not comfortably readable.
- A page so crowded that structure breaks down.
- Large unexplained blank regions.
- Section alignment that is visibly inconsistent.
- Glyph or rendering corruption.
- An overall visual hierarchy that is plainly broken.

Rules:
- Report ISSUES_FOUND with at least one finding when a page shows any of the
  above, CLEAN with no findings when the pages look sound, and UNCERTAIN when
  you cannot judge reliably.
- Every finding must name a supplied page number.
- A bounding box, when given, must lie inside that page's pixel bounds.
- Never emit LaTeX, a patch, replacement text or a recompile instruction.
- Never call tools or request files.
"""

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RESULT_ID_PATTERN = re.compile(r"^resume-visual-qa-[a-f0-9]{64}$")
_FINDING_ID_PATTERN = re.compile(
    r"^resume-visual-qa-finding-[a-f0-9]{64}$"
)
_NON_TEXT_PATTERN = re.compile(r"[^0-9a-z]+")
MAX_VISUAL_QA_EXPLANATION_CHARS = 2_000
MAX_VISUAL_QA_FINDINGS = 200


class ResumeVisualQAVerdict(str, Enum):
    PASSED = "PASSED"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    DEFERRED = "DEFERRED"


class ResumeVisualQAFindingSource(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    AGENT = "AGENT"


class ResumeVisualQAFindingSeverity(str, Enum):
    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"


class ResumeVisualQAFindingType(str, Enum):
    PAGE_COUNT_MISMATCH = "PAGE_COUNT_MISMATCH"
    UNEXPECTED_PAGE_COUNT = "UNEXPECTED_PAGE_COUNT"
    BLANK_PAGE = "BLANK_PAGE"
    CONTENT_MISSING = "CONTENT_MISSING"
    CONTENT_CLIPPED = "CONTENT_CLIPPED"
    ELEMENT_OVERLAP = "ELEMENT_OVERLAP"
    TEXT_TOO_SMALL = "TEXT_TOO_SMALL"
    EXCESSIVE_DENSITY = "EXCESSIVE_DENSITY"
    EXCESSIVE_WHITESPACE = "EXCESSIVE_WHITESPACE"
    BROKEN_GLYPH = "BROKEN_GLYPH"
    INCONSISTENT_ALIGNMENT = "INCONSISTENT_ALIGNMENT"
    UNREADABLE_LAYOUT = "UNREADABLE_LAYOUT"
    AGENT_OUTPUT_UNRELIABLE = "AGENT_OUTPUT_UNRELIABLE"


#: Only these may come from the Agent; the rest are measured by ordinary code.
AGENT_FINDING_TYPES = frozenset(
    {
        ResumeVisualQAFindingType.ELEMENT_OVERLAP,
        ResumeVisualQAFindingType.TEXT_TOO_SMALL,
        ResumeVisualQAFindingType.EXCESSIVE_DENSITY,
        ResumeVisualQAFindingType.EXCESSIVE_WHITESPACE,
        ResumeVisualQAFindingType.BROKEN_GLYPH,
        ResumeVisualQAFindingType.INCONSISTENT_ALIGNMENT,
        ResumeVisualQAFindingType.UNREADABLE_LAYOUT,
        ResumeVisualQAFindingType.PAGE_COUNT_MISMATCH,
    }
)

#: Severity is derived from the type, so an Agent cannot downgrade a defect.
ADVISORY_FINDING_TYPES = frozenset(
    {
        ResumeVisualQAFindingType.EXCESSIVE_DENSITY,
        ResumeVisualQAFindingType.EXCESSIVE_WHITESPACE,
        ResumeVisualQAFindingType.INCONSISTENT_ALIGNMENT,
    }
)


class ResumeVisualQAAgentVerdict(str, Enum):
    CLEAN = "CLEAN"
    ISSUES_FOUND = "ISSUES_FOUND"
    UNCERTAIN = "UNCERTAIN"


class ResumeVisualQAWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeVisualQAReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeVisualQAStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_RENDERER_UNAVAILABLE = "DEFERRED_RENDERER_UNAVAILABLE"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class ResumeVisualQAFailureReason(str, Enum):
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


class ResumeVisualQAAgentUnavailableError(RuntimeError):
    """Raised when the bounded visual QA Agent cannot return an output."""


@dataclass(frozen=True, slots=True)
class ResumeVisualQAPolicy:
    """The versioned layout expectation; no page rule is inferred from prose."""

    policy_version: str = RESUME_VISUAL_QA_POLICY_VERSION
    max_pages: int = 1
    min_font_size_pt: float = 7.5
    page_margin_tolerance_pt: float = 2.0
    min_text_characters_per_page: int = 40

    def __post_init__(self) -> None:
        if self.policy_version != RESUME_VISUAL_QA_POLICY_VERSION:
            raise ValueError("visual QA policy version is unsupported")
        if type(self.max_pages) is not int or not 1 <= self.max_pages <= 20:
            raise ValueError("max_pages is outside the policy contract")
        if (
            not isinstance(self.min_font_size_pt, (int, float))
            or not 1 <= float(self.min_font_size_pt) <= 72
        ):
            raise ValueError("min_font_size_pt is outside the policy")
        if (
            not isinstance(self.page_margin_tolerance_pt, (int, float))
            or not 0 <= float(self.page_margin_tolerance_pt) <= 72
        ):
            raise ValueError("page_margin_tolerance_pt is outside the policy")
        if (
            type(self.min_text_characters_per_page) is not int
            or self.min_text_characters_per_page < 0
        ):
            raise ValueError("min_text_characters_per_page is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_pages": self.max_pages,
            "min_font_size_pt": float(self.min_font_size_pt),
            "min_text_characters_per_page": (
                self.min_text_characters_per_page
            ),
            "page_margin_tolerance_pt": float(
                self.page_margin_tolerance_pt
            ),
            "policy_version": self.policy_version,
        }


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the visual QA contract")
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


def _normalized(value: str) -> str:
    return " ".join(
        token
        for token in _NON_TEXT_PATTERN.split(value.casefold())
        if token
    )


@dataclass(frozen=True, slots=True)
class ResumeVisualQAAgentMetadata:
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
class VisualBoundingBox:
    x0: float
    top: float
    x1: float
    bottom: float

    def __post_init__(self) -> None:
        for name in ("x0", "top", "x1", "bottom"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value != value:
                raise ValueError(f"{name} must be a real number")
            object.__setattr__(self, name, float(value))
        if self.x1 <= self.x0 or self.bottom <= self.top:
            raise ValueError("bounding box must have positive area")

    def within(self, *, width: float, height: float) -> bool:
        return (
            self.x0 >= 0
            and self.top >= 0
            and self.x1 <= width
            and self.bottom <= height
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bottom": self.bottom,
            "top": self.top,
            "x0": self.x0,
            "x1": self.x1,
        }


@dataclass(frozen=True, slots=True)
class ResumeVisualQAFinding:
    finding_id: str
    order: int
    finding_type: ResumeVisualQAFindingType
    severity: ResumeVisualQAFindingSeverity
    source: ResumeVisualQAFindingSource
    page_number: int
    bounding_box: VisualBoundingBox | None
    explanation: str

    def __post_init__(self) -> None:
        if type(self.order) is not int or self.order < 0:
            raise ValueError("finding order must be a non-negative integer")
        finding_type = ResumeVisualQAFindingType(self.finding_type)
        severity = ResumeVisualQAFindingSeverity(self.severity)
        source = ResumeVisualQAFindingSource(self.source)
        object.__setattr__(self, "finding_type", finding_type)
        object.__setattr__(self, "severity", severity)
        object.__setattr__(self, "source", source)
        if (
            source is ResumeVisualQAFindingSource.AGENT
            and finding_type not in AGENT_FINDING_TYPES
        ):
            raise ValueError("the Agent cannot report this finding type")
        expected = (
            ResumeVisualQAFindingSeverity.ADVISORY
            if finding_type in ADVISORY_FINDING_TYPES
            else ResumeVisualQAFindingSeverity.BLOCKING
        )
        if severity is not expected:
            raise ValueError("finding severity does not match its type")
        if type(self.page_number) is not int or self.page_number < 0:
            raise ValueError("page_number must be a non-negative integer")
        if self.bounding_box is not None and not isinstance(
            self.bounding_box, VisualBoundingBox
        ):
            raise TypeError("bounding_box must be typed")
        _clean_text(
            "explanation",
            self.explanation,
            maximum=MAX_VISUAL_QA_EXPLANATION_CHARS,
        )
        expected_id = resume_visual_qa_finding_id(self.content_dict())
        if (
            not isinstance(self.finding_id, str)
            or _FINDING_ID_PATTERN.fullmatch(self.finding_id) is None
            or self.finding_id != expected_id
        ):
            raise ValueError("finding_id does not match its content")

    def content_dict(self) -> dict[str, Any]:
        return {
            "bounding_box": (
                self.bounding_box.to_dict() if self.bounding_box else None
            ),
            "explanation": self.explanation,
            "finding_type": self.finding_type.value,
            "order": self.order,
            "page_number": self.page_number,
            "severity": self.severity.value,
            "source": self.source.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"finding_id": self.finding_id, **self.content_dict()}


def resume_visual_qa_finding_id(content: Mapping[str, Any]) -> str:
    return "resume-visual-qa-finding-" + _canonical_hash(content)


def _build_finding(
    *,
    order: int,
    finding_type: ResumeVisualQAFindingType,
    source: ResumeVisualQAFindingSource,
    page_number: int,
    explanation: str,
    bounding_box: VisualBoundingBox | None = None,
) -> ResumeVisualQAFinding:
    severity = (
        ResumeVisualQAFindingSeverity.ADVISORY
        if finding_type in ADVISORY_FINDING_TYPES
        else ResumeVisualQAFindingSeverity.BLOCKING
    )
    content = {
        "bounding_box": bounding_box.to_dict() if bounding_box else None,
        "explanation": explanation,
        "finding_type": finding_type.value,
        "order": order,
        "page_number": page_number,
        "severity": severity.value,
        "source": source.value,
    }
    return ResumeVisualQAFinding(
        finding_id=resume_visual_qa_finding_id(content),
        order=order,
        finding_type=finding_type,
        severity=severity,
        source=source,
        page_number=page_number,
        bounding_box=bounding_box,
        explanation=explanation,
    )


@dataclass(frozen=True, slots=True)
class ResumeVisualQAPageView:
    page_number: int
    width_px: int
    height_px: int
    image_format: str
    image_bytes: bytes


@dataclass(frozen=True, slots=True)
class ResumeVisualQAFindingView:
    finding_type: ResumeVisualQAFindingType
    severity: ResumeVisualQAFindingSeverity
    page_number: int
    explanation: str


@dataclass(frozen=True, slots=True)
class ResumeVisualQAContext:
    subject_id: str
    pages: tuple[ResumeVisualQAPageView, ...]
    deterministic_findings: tuple[ResumeVisualQAFindingView, ...]
    policy: Mapping[str, Any]
    policy_version: str
    agent_policy: str


@dataclass(frozen=True, slots=True)
class ResumeVisualQAAgentFinding:
    finding_type: ResumeVisualQAFindingType
    page_number: int
    explanation: str
    bounding_box: VisualBoundingBox | None = None

    def __post_init__(self) -> None:
        finding_type = ResumeVisualQAFindingType(self.finding_type)
        object.__setattr__(self, "finding_type", finding_type)
        if finding_type not in AGENT_FINDING_TYPES:
            raise ValueError("the Agent cannot report this finding type")
        if type(self.page_number) is not int or self.page_number < 1:
            raise ValueError("page_number must be a positive integer")
        _clean_text(
            "explanation",
            self.explanation,
            maximum=MAX_VISUAL_QA_EXPLANATION_CHARS,
        )
        if self.bounding_box is not None and not isinstance(
            self.bounding_box, VisualBoundingBox
        ):
            raise TypeError("bounding_box must be typed")


@dataclass(frozen=True, slots=True)
class ResumeVisualQAAgentOutput:
    verdict: ResumeVisualQAAgentVerdict
    findings: tuple[ResumeVisualQAAgentFinding, ...]

    def __post_init__(self) -> None:
        verdict = ResumeVisualQAAgentVerdict(self.verdict)
        object.__setattr__(self, "verdict", verdict)
        if not isinstance(self.findings, tuple) or any(
            not isinstance(item, ResumeVisualQAAgentFinding)
            for item in self.findings
        ):
            raise TypeError("findings must be typed Agent findings")
        if verdict is ResumeVisualQAAgentVerdict.ISSUES_FOUND:
            if not self.findings:
                raise ValueError("an issues verdict requires findings")
        elif self.findings:
            raise ValueError("only an issues verdict may carry findings")


@runtime_checkable
class ResumeVisualQAAgentPort(Protocol):
    async def review(
        self, context: ResumeVisualQAContext
    ) -> ResumeVisualQAAgentOutput:
        """Judge rendered pages only; never edit, patch or recompile."""


def _visual_qa_binding(
    *,
    compilation: ResumeCompilationRecord,
    version: ResumeLatexVersion,
    draft: TailoredResumeDraft,
    renderer: PdfRendererDescription,
    policy: ResumeVisualQAPolicy,
    metadata: ResumeVisualQAAgentMetadata,
) -> str:
    return _canonical_hash(
        {
            "compilation_binding": compilation.compilation_binding,
            "compilation_record_id": compilation.record_id,
            "latex_source_sha256": version.source_sha256,
            "latex_version_id": version.latex_version_id,
            "pdf_sha256": compilation.pdf_sha256,
            "renderer_dpi": renderer.dpi,
            "renderer_image_format": renderer.image_format,
            "renderer_name": renderer.renderer_name,
            "renderer_version": renderer.renderer_version,
            "resume_visual_qa_agent_version": metadata.agent_version,
            "resume_visual_qa_contract_version": (
                RESUME_VISUAL_QA_CONTRACT_VERSION
            ),
            "resume_visual_qa_model_id": metadata.model_id,
            "resume_visual_qa_policy": policy.to_dict(),
            "resume_visual_qa_prompt_version": metadata.prompt_version,
            "subject_id": compilation.subject_id,
            "tailored_resume_draft_hash": draft.draft_content_hash,
            "tailored_resume_draft_id": draft.draft_id,
        }
    )


@dataclass(frozen=True, slots=True)
class ResumeVisualQAResult:
    result_id: str
    contract_version: str
    visual_qa_binding: str
    subject_id: str
    compilation_record_id: str
    compilation_binding: str
    pdf_sha256: str
    latex_version_id: str
    latex_source_sha256: str
    tailored_resume_draft_id: str
    tailored_resume_draft_hash: str
    renderer_name: str
    renderer_version: str
    renderer_dpi: int
    policy_version: str
    max_pages: int
    page_count: int
    agent_invoked: bool
    agent_version: str
    prompt_version: str
    model_id: str
    verdict: ResumeVisualQAVerdict
    findings: tuple[ResumeVisualQAFinding, ...]
    result_content_hash: str
    validated_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != RESUME_VISUAL_QA_CONTRACT_VERSION:
            raise ValueError("visual QA contract is unsupported")
        binding = _require_hash("visual_qa_binding", self.visual_qa_binding)
        if (
            not isinstance(self.result_id, str)
            or _RESULT_ID_PATTERN.fullmatch(self.result_id) is None
            or self.result_id != f"resume-visual-qa-{binding}"
        ):
            raise ValueError("result_id does not match its binding")
        _clean_text("subject_id", self.subject_id, maximum=160)
        _clean_text(
            "compilation_record_id",
            self.compilation_record_id,
            maximum=160,
        )
        _require_hash("compilation_binding", self.compilation_binding)
        _require_hash("pdf_sha256", self.pdf_sha256)
        _clean_text(
            "latex_version_id", self.latex_version_id, maximum=160
        )
        _require_hash("latex_source_sha256", self.latex_source_sha256)
        _clean_text(
            "tailored_resume_draft_id",
            self.tailored_resume_draft_id,
            maximum=160,
        )
        _require_hash(
            "tailored_resume_draft_hash", self.tailored_resume_draft_hash
        )
        _clean_text("renderer_name", self.renderer_name, maximum=120)
        _clean_text("renderer_version", self.renderer_version, maximum=120)
        if type(self.renderer_dpi) is not int or not 36 <= self.renderer_dpi <= 600:
            raise ValueError("renderer_dpi is outside the contract")
        if self.policy_version != RESUME_VISUAL_QA_POLICY_VERSION:
            raise ValueError("visual QA policy version is unsupported")
        if type(self.max_pages) is not int or self.max_pages < 1:
            raise ValueError("max_pages must be at least one")
        if type(self.page_count) is not int or self.page_count < 1:
            raise ValueError("page_count must be at least one")
        if type(self.agent_invoked) is not bool:
            raise TypeError("agent_invoked must be a boolean")
        _clean_text("agent_version", self.agent_version, maximum=80)
        _clean_text("prompt_version", self.prompt_version, maximum=80)
        _clean_text("model_id", self.model_id, maximum=160)
        verdict = ResumeVisualQAVerdict(self.verdict)
        object.__setattr__(self, "verdict", verdict)
        if (
            not isinstance(self.findings, tuple)
            or len(self.findings) > MAX_VISUAL_QA_FINDINGS
            or any(
                not isinstance(item, ResumeVisualQAFinding)
                for item in self.findings
            )
        ):
            raise TypeError("findings must be a bounded typed tuple")
        if tuple(item.order for item in self.findings) != tuple(
            range(len(self.findings))
        ):
            raise ValueError("findings must have contiguous order")
        identifiers = [item.finding_id for item in self.findings]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("finding identities must be unique")
        blocking = tuple(
            item
            for item in self.findings
            if item.severity is ResumeVisualQAFindingSeverity.BLOCKING
        )
        if verdict is ResumeVisualQAVerdict.PASSED and blocking:
            raise ValueError("a passed result cannot carry blocking findings")
        if verdict is ResumeVisualQAVerdict.REVISION_REQUIRED and not blocking:
            raise ValueError(
                "a revision-required result needs a blocking finding"
            )
        if verdict is ResumeVisualQAVerdict.DEFERRED and not any(
            item.finding_type
            is ResumeVisualQAFindingType.AGENT_OUTPUT_UNRELIABLE
            for item in self.findings
        ):
            raise ValueError("a deferred result must record why")
        if (
            any(
                item.source is ResumeVisualQAFindingSource.AGENT
                for item in self.findings
            )
            and not self.agent_invoked
        ):
            raise ValueError("Agent findings require an actual Agent call")
        object.__setattr__(self, "contract_version", contract)
        _require_aware("validated_at", self.validated_at)
        content_hash = _require_hash(
            "result_content_hash", self.result_content_hash
        )
        if content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("visual QA content hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "contract_version": self.contract_version,
            "visual_qa_binding": self.visual_qa_binding,
            "subject_id": self.subject_id,
            "compilation_record_id": self.compilation_record_id,
            "compilation_binding": self.compilation_binding,
            "pdf_sha256": self.pdf_sha256,
            "latex_version_id": self.latex_version_id,
            "latex_source_sha256": self.latex_source_sha256,
            "tailored_resume_draft_id": self.tailored_resume_draft_id,
            "tailored_resume_draft_hash": self.tailored_resume_draft_hash,
            "renderer_name": self.renderer_name,
            "renderer_version": self.renderer_version,
            "renderer_dpi": self.renderer_dpi,
            "policy_version": self.policy_version,
            "max_pages": self.max_pages,
            "page_count": self.page_count,
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
            "result_content_hash": self.result_content_hash,
            "validated_at": _rfc3339(self.validated_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeVisualQAWriteResult:
    status: ResumeVisualQAWriteStatus
    result: ResumeVisualQAResult | None
    reason_code: ResumeVisualQAFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ResumeVisualQAWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeVisualQAFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ResumeVisualQAWriteStatus.CREATED,
            ResumeVisualQAWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.result, ResumeVisualQAResult)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful visual QA write is invalid")
        elif self.result is not None or self.reason_code is None:
            raise ValueError("failed visual QA write is invalid")


@dataclass(frozen=True, slots=True)
class ResumeVisualQAReadResult:
    status: ResumeVisualQAReadStatus
    result: ResumeVisualQAResult | None
    reason_code: ResumeVisualQAFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeVisualQAReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeVisualQAFailureReason(self.reason_code),
            )
        if status is ResumeVisualQAReadStatus.FOUND:
            if (
                not isinstance(self.result, ResumeVisualQAResult)
                or self.reason_code is not None
            ):
                raise ValueError("found visual QA read is invalid")
        elif status is ResumeVisualQAReadStatus.NOT_FOUND:
            if self.result is not None or self.reason_code is not None:
                raise ValueError("not-found visual QA read is invalid")
        elif (
            self.result is not None
            or self.reason_code
            is not ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure visual QA read is invalid")


@runtime_checkable
class ResumeVisualQARepository(Protocol):
    def save(
        self, result: ResumeVisualQAResult
    ) -> ResumeVisualQAWriteResult:
        """Persist one immutable visual QA result."""

    def get(
        self, *, subject_id: str, result_id: str
    ) -> ResumeVisualQAReadResult:
        """Read one subject-owned visual QA result."""


def _bounding_box_from_dict(value: Any) -> VisualBoundingBox | None:
    if value is None:
        return None
    expected = {"bottom", "top", "x0", "x1"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted bounding box is invalid")
    return VisualBoundingBox(
        x0=value["x0"],
        top=value["top"],
        x1=value["x1"],
        bottom=value["bottom"],
    )


def _finding_from_dict(value: Any) -> ResumeVisualQAFinding:
    expected = {
        "finding_id",
        "order",
        "finding_type",
        "severity",
        "source",
        "page_number",
        "bounding_box",
        "explanation",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted visual QA finding is invalid")
    return ResumeVisualQAFinding(
        finding_id=value["finding_id"],
        order=value["order"],
        finding_type=ResumeVisualQAFindingType(value["finding_type"]),
        severity=ResumeVisualQAFindingSeverity(value["severity"]),
        source=ResumeVisualQAFindingSource(value["source"]),
        page_number=value["page_number"],
        bounding_box=_bounding_box_from_dict(value["bounding_box"]),
        explanation=value["explanation"],
    )


def _result_from_dict(value: Any) -> ResumeVisualQAResult:
    expected = {
        "result_id",
        "contract_version",
        "visual_qa_binding",
        "subject_id",
        "compilation_record_id",
        "compilation_binding",
        "pdf_sha256",
        "latex_version_id",
        "latex_source_sha256",
        "tailored_resume_draft_id",
        "tailored_resume_draft_hash",
        "renderer_name",
        "renderer_version",
        "renderer_dpi",
        "policy_version",
        "max_pages",
        "page_count",
        "agent_invoked",
        "agent_version",
        "prompt_version",
        "model_id",
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
        raise ValueError("persisted visual QA result is invalid")
    return ResumeVisualQAResult(
        result_id=value["result_id"],
        contract_version=value["contract_version"],
        visual_qa_binding=value["visual_qa_binding"],
        subject_id=value["subject_id"],
        compilation_record_id=value["compilation_record_id"],
        compilation_binding=value["compilation_binding"],
        pdf_sha256=value["pdf_sha256"],
        latex_version_id=value["latex_version_id"],
        latex_source_sha256=value["latex_source_sha256"],
        tailored_resume_draft_id=value["tailored_resume_draft_id"],
        tailored_resume_draft_hash=value["tailored_resume_draft_hash"],
        renderer_name=value["renderer_name"],
        renderer_version=value["renderer_version"],
        renderer_dpi=value["renderer_dpi"],
        policy_version=value["policy_version"],
        max_pages=value["max_pages"],
        page_count=value["page_count"],
        agent_invoked=value["agent_invoked"],
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        verdict=ResumeVisualQAVerdict(value["verdict"]),
        findings=tuple(
            _finding_from_dict(item) for item in value["findings"]
        ),
        result_content_hash=value["result_content_hash"],
        validated_at=_parse_timestamp(value["validated_at"]),
    )


class PrivateHomeResumeVisualQARepository:
    """Immutable, subject-scoped visual QA results in Private Home."""

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
            self._home.paths.resume_visual_qa
            / _subject_storage_key(subject)
            / f"{result_id}.json"
        )

    def get(
        self, *, subject_id: str, result_id: str
    ) -> ResumeVisualQAReadResult:
        path = self._path(subject_id, result_id)
        with self._lock:
            if not path.exists():
                return ResumeVisualQAReadResult(
                    status=ResumeVisualQAReadStatus.NOT_FOUND,
                    result=None,
                )
            if path.is_symlink() or not path.is_file():
                return ResumeVisualQAReadResult(
                    status=ResumeVisualQAReadStatus.INTEGRITY_FAILURE,
                    result=None,
                    reason_code=(
                        ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE
                    ),
                )
            try:
                result = _result_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ResumeVisualQAReadResult(
                    status=ResumeVisualQAReadStatus.INTEGRITY_FAILURE,
                    result=None,
                    reason_code=(
                        ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE
                    ),
                )
            if (
                result.subject_id != subject_id.strip()
                or result.result_id != result_id
                or path.name != f"{result.result_id}.json"
            ):
                return ResumeVisualQAReadResult(
                    status=ResumeVisualQAReadStatus.INTEGRITY_FAILURE,
                    result=None,
                    reason_code=(
                        ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE
                    ),
                )
            return ResumeVisualQAReadResult(
                status=ResumeVisualQAReadStatus.FOUND,
                result=result,
            )

    def save(
        self, result: ResumeVisualQAResult
    ) -> ResumeVisualQAWriteResult:
        if not isinstance(result, ResumeVisualQAResult):
            raise TypeError("result must be a ResumeVisualQAResult")
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
                return ResumeVisualQAWriteResult(
                    status=ResumeVisualQAWriteStatus.FAILED,
                    result=None,
                    reason_code=(
                        ResumeVisualQAFailureReason.RESULT_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return ResumeVisualQAWriteResult(
                    status=ResumeVisualQAWriteStatus.CREATED,
                    result=result,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=result.subject_id, result_id=result.result_id
            )
            if (
                existing.status is ResumeVisualQAReadStatus.FOUND
                and existing.result is not None
                and existing.result.content_dict() == result.content_dict()
            ):
                return ResumeVisualQAWriteResult(
                    status=ResumeVisualQAWriteStatus.UNCHANGED,
                    result=existing.result,
                    reason_code=None,
                    retryable=False,
                )
            return ResumeVisualQAWriteResult(
                status=ResumeVisualQAWriteStatus.FAILED,
                result=None,
                reason_code=(
                    ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class _PdfProjection:
    page_count: int
    pages: tuple[dict[str, Any], ...]


def _project_pdf(content: bytes) -> _PdfProjection | None:
    """Extract page geometry, text and character boxes; no rendering involved."""

    try:
        with pdfplumber.open(BytesIO(content)) as document:
            pages: list[dict[str, Any]] = []
            for index, page in enumerate(document.pages, start=1):
                chars = page.chars or []
                pages.append(
                    {
                        "page_number": index,
                        "width": float(page.width),
                        "height": float(page.height),
                        "text": page.extract_text() or "",
                        "chars": [
                            {
                                "x0": float(item.get("x0", 0.0)),
                                "x1": float(item.get("x1", 0.0)),
                                "top": float(item.get("top", 0.0)),
                                "bottom": float(item.get("bottom", 0.0)),
                                "size": float(item.get("size", 0.0)),
                            }
                            for item in chars
                        ],
                    }
                )
            return _PdfProjection(
                page_count=len(pages), pages=tuple(pages)
            )
    except (PDFSyntaxError, OSError, TypeError, ValueError):
        return None
    except Exception:
        return None


def _draft_expected_texts(draft: TailoredResumeDraft) -> tuple[str, ...]:
    texts: list[str] = []
    for section in draft.sections:
        if section.title:
            texts.append(section.title)
        texts.extend(
            bullet.text
            for bullet in section.bullets
            if bullet.change_type is not TailoredBulletChangeType.OMITTED
            and bullet.text
        )
    return tuple(texts)


def deterministic_visual_findings(
    *,
    projection: _PdfProjection,
    draft: TailoredResumeDraft,
    policy: ResumeVisualQAPolicy,
) -> tuple[ResumeVisualQAFinding, ...]:
    """Measure everything ordinary code can measure, before any rendering."""

    raw: list[tuple[ResumeVisualQAFindingType, int, str, VisualBoundingBox | None]] = []
    if projection.page_count > policy.max_pages:
        raw.append(
            (
                ResumeVisualQAFindingType.UNEXPECTED_PAGE_COUNT,
                0,
                (
                    f"The PDF has {projection.page_count} pages but the "
                    f"policy allows {policy.max_pages}."
                ),
                None,
            )
        )
    combined = " ".join(page["text"] for page in projection.pages)
    normalized_document = _normalized(combined)
    for page in projection.pages:
        number = page["page_number"]
        width = page["width"]
        height = page["height"]
        text = page["text"]
        chars = page["chars"]
        if width <= 0 or height <= 0:
            raw.append(
                (
                    ResumeVisualQAFindingType.UNREADABLE_LAYOUT,
                    number,
                    "The page has no usable dimensions.",
                    None,
                )
            )
            continue
        if not text.strip() or not chars:
            raw.append(
                (
                    ResumeVisualQAFindingType.BLANK_PAGE,
                    number,
                    "The page contains no extractable text.",
                    None,
                )
            )
            continue
        if len(text.strip()) < policy.min_text_characters_per_page:
            raw.append(
                (
                    ResumeVisualQAFindingType.BLANK_PAGE,
                    number,
                    (
                        "The page carries less text than the policy considers "
                        "a real page."
                    ),
                    None,
                )
            )
        tolerance = policy.page_margin_tolerance_pt
        clipped = [
            item
            for item in chars
            if item["x0"] < -tolerance
            or item["top"] < -tolerance
            or item["x1"] > width + tolerance
            or item["bottom"] > height + tolerance
        ]
        if clipped:
            first = clipped[0]
            raw.append(
                (
                    ResumeVisualQAFindingType.CONTENT_CLIPPED,
                    number,
                    (
                        f"{len(clipped)} characters fall outside the page "
                        "boundary."
                    ),
                    VisualBoundingBox(
                        x0=max(first["x0"], 0.0),
                        top=max(first["top"], 0.0),
                        x1=max(first["x1"], max(first["x0"], 0.0) + 0.1),
                        bottom=max(
                            first["bottom"], max(first["top"], 0.0) + 0.1
                        ),
                    ),
                )
            )
        sizes = [item["size"] for item in chars if item["size"] > 0]
        if sizes and min(sizes) < policy.min_font_size_pt:
            raw.append(
                (
                    ResumeVisualQAFindingType.TEXT_TOO_SMALL,
                    number,
                    (
                        f"The smallest glyph measures {min(sizes):.1f}pt, "
                        f"below the {policy.min_font_size_pt}pt minimum."
                    ),
                    None,
                )
            )
    for expected in _draft_expected_texts(draft):
        normalized = _normalized(expected)
        if normalized and normalized not in normalized_document:
            raw.append(
                (
                    ResumeVisualQAFindingType.CONTENT_MISSING,
                    0,
                    "Draft content is not present in the compiled PDF text.",
                    None,
                )
            )
    return tuple(
        _build_finding(
            order=order,
            finding_type=item[0],
            source=ResumeVisualQAFindingSource.DETERMINISTIC,
            page_number=item[1],
            explanation=item[2],
            bounding_box=item[3],
        )
        for order, item in enumerate(raw)
    )


class _AgentOutputRejected(ValueError):
    """The Agent output failed deterministic reference validation."""


def _agent_findings(
    *,
    output: ResumeVisualQAAgentOutput,
    pages: tuple[RenderedPage, ...],
    start_order: int,
) -> tuple[ResumeVisualQAFinding, ...]:
    by_number = {page.page_number: page for page in pages}
    accepted: list[ResumeVisualQAFinding] = []
    for offset, finding in enumerate(output.findings):
        page = by_number.get(finding.page_number)
        if page is None:
            raise _AgentOutputRejected(
                "an Agent finding references an unknown page"
            )
        if finding.bounding_box is not None and not finding.bounding_box.within(
            width=float(page.width_px), height=float(page.height_px)
        ):
            raise _AgentOutputRejected(
                "an Agent bounding box falls outside its page"
            )
        accepted.append(
            _build_finding(
                order=start_order + offset,
                finding_type=finding.finding_type,
                source=ResumeVisualQAFindingSource.AGENT,
                page_number=finding.page_number,
                explanation=finding.explanation,
                bounding_box=finding.bounding_box,
            )
        )
    return tuple(accepted)


@dataclass(frozen=True, slots=True)
class ReviewResumeVisualQACommand:
    subject_id: str
    resume_compilation_record_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ReviewResumeVisualQAResult:
    status: ResumeVisualQAStatus
    subject_id: str
    visual_qa_binding: str
    result: ResumeVisualQAResult | None
    write_result: ResumeVisualQAWriteResult | None
    reason_code: ResumeVisualQAFailureReason | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = ResumeVisualQAStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeVisualQAFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            ResumeVisualQAStatus.CREATED,
            ResumeVisualQAStatus.UNCHANGED,
            ResumeVisualQAStatus.DEFERRED_NEEDS_HUMAN,
        }:
            if (
                not isinstance(self.result, ResumeVisualQAResult)
                or not isinstance(
                    self.write_result, ResumeVisualQAWriteResult
                )
                or self.write_result.result != self.result
                or self.write_result.status
                is ResumeVisualQAWriteStatus.FAILED
                or self.retryable
            ):
                raise ValueError("persisted visual QA result is invalid")
            if status is ResumeVisualQAStatus.UNCHANGED and (
                self.write_result.status
                is not ResumeVisualQAWriteStatus.UNCHANGED
            ):
                raise ValueError("unchanged visual QA needs a replay write")
            if status is ResumeVisualQAStatus.DEFERRED_NEEDS_HUMAN:
                if (
                    self.result.verdict is not ResumeVisualQAVerdict.DEFERRED
                    or self.reason_code
                    is not ResumeVisualQAFailureReason.AGENT_OUTPUT_UNRELIABLE
                ):
                    raise ValueError("deferred visual QA result is invalid")
            elif self.reason_code is not None:
                raise ValueError("successful visual QA cannot carry a reason")
        elif self.result is not None or self.reason_code is None:
            raise ValueError("unsuccessful visual QA result is invalid")


def _failure(
    command: ReviewResumeVisualQACommand,
    reason: ResumeVisualQAFailureReason,
    *,
    status: ResumeVisualQAStatus = ResumeVisualQAStatus.FAILED,
    retryable: bool = False,
    visual_qa_binding: str = "",
    detail: str | None = None,
) -> ReviewResumeVisualQAResult:
    return ReviewResumeVisualQAResult(
        status=status,
        subject_id=(
            command.subject_id
            if isinstance(command.subject_id, str)
            else ""
        ),
        visual_qa_binding=visual_qa_binding,
        result=None,
        write_result=None,
        reason_code=reason,
        retryable=retryable,
        message=detail or f"Resume visual QA stopped: {reason.value}.",
    )


def _build_result(
    *,
    compilation: ResumeCompilationRecord,
    version: ResumeLatexVersion,
    draft: TailoredResumeDraft,
    renderer: PdfRendererDescription,
    policy: ResumeVisualQAPolicy,
    metadata: ResumeVisualQAAgentMetadata,
    binding: str,
    page_count: int,
    verdict: ResumeVisualQAVerdict,
    findings: tuple[ResumeVisualQAFinding, ...],
    agent_invoked: bool,
    now: datetime,
) -> ResumeVisualQAResult:
    result_id = f"resume-visual-qa-{binding}"
    content = {
        "result_id": result_id,
        "contract_version": RESUME_VISUAL_QA_CONTRACT_VERSION,
        "visual_qa_binding": binding,
        "subject_id": compilation.subject_id,
        "compilation_record_id": compilation.record_id,
        "compilation_binding": compilation.compilation_binding,
        "pdf_sha256": compilation.pdf_sha256,
        "latex_version_id": version.latex_version_id,
        "latex_source_sha256": version.source_sha256,
        "tailored_resume_draft_id": draft.draft_id,
        "tailored_resume_draft_hash": draft.draft_content_hash,
        "renderer_name": renderer.renderer_name,
        "renderer_version": renderer.renderer_version,
        "renderer_dpi": renderer.dpi,
        "policy_version": policy.policy_version,
        "max_pages": policy.max_pages,
        "page_count": page_count,
        "agent_invoked": agent_invoked,
        "agent_version": metadata.agent_version,
        "prompt_version": metadata.prompt_version,
        "model_id": metadata.model_id,
        "verdict": verdict.value,
        "findings": [item.to_dict() for item in findings],
    }
    return ResumeVisualQAResult(
        result_id=result_id,
        contract_version=RESUME_VISUAL_QA_CONTRACT_VERSION,
        visual_qa_binding=binding,
        subject_id=compilation.subject_id,
        compilation_record_id=compilation.record_id,
        compilation_binding=compilation.compilation_binding,
        pdf_sha256=compilation.pdf_sha256,
        latex_version_id=version.latex_version_id,
        latex_source_sha256=version.source_sha256,
        tailored_resume_draft_id=draft.draft_id,
        tailored_resume_draft_hash=draft.draft_content_hash,
        renderer_name=renderer.renderer_name,
        renderer_version=renderer.renderer_version,
        renderer_dpi=renderer.dpi,
        policy_version=policy.policy_version,
        max_pages=policy.max_pages,
        page_count=page_count,
        agent_invoked=agent_invoked,
        agent_version=metadata.agent_version,
        prompt_version=metadata.prompt_version,
        model_id=metadata.model_id,
        verdict=verdict,
        findings=findings,
        result_content_hash=_canonical_hash(content),
        validated_at=now,
    )


async def review_resume_visual_qa(
    command: ReviewResumeVisualQACommand,
    *,
    compilation_repository: ResumeCompilationRepository,
    latex_version_repository: ResumeLatexVersionRepository,
    construction_repository: ResumeLatexConstructionRecordRepository,
    draft_repository: TailoredResumeDraftRepository,
    renderer: PdfPageRendererPort,
    agent: ResumeVisualQAAgentPort,
    metadata: ResumeVisualQAAgentMetadata,
    visual_qa_repository: ResumeVisualQARepository,
    policy: ResumeVisualQAPolicy | None = None,
    home: PrivateHome | None = None,
) -> ReviewResumeVisualQAResult:
    """Inspect one compiled PDF and report; never edit, patch or recompile."""

    active_home = home or PrivateHome.discover()
    active_policy = policy or ResumeVisualQAPolicy()
    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        compilation_id = _clean_text(
            "resume_compilation_record_id",
            command.resume_compilation_record_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
        if not isinstance(active_policy, ResumeVisualQAPolicy):
            raise TypeError("policy must be a ResumeVisualQAPolicy")
        if not isinstance(metadata, ResumeVisualQAAgentMetadata):
            raise TypeError("metadata must be visual QA Agent metadata")
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command, ResumeVisualQAFailureReason.INVALID_REQUEST
        )

    try:
        compilation_read = compilation_repository.get(
            subject_id=subject_id, record_id=compilation_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeVisualQAFailureReason
            .COMPILATION_RECORD_INTEGRITY_FAILURE,
        )
    if compilation_read.status is ResumeCompilationReadStatus.NOT_FOUND:
        return _failure(
            command,
            ResumeVisualQAFailureReason.COMPILATION_RECORD_NOT_FOUND,
        )
    if (
        compilation_read.status is not ResumeCompilationReadStatus.FOUND
        or not isinstance(compilation_read.record, ResumeCompilationRecord)
    ):
        return _failure(
            command,
            ResumeVisualQAFailureReason
            .COMPILATION_RECORD_INTEGRITY_FAILURE,
        )
    compilation = compilation_read.record
    if compilation.subject_id != subject_id:
        return _failure(
            command,
            ResumeVisualQAFailureReason.COMPILATION_RECORD_NOT_FOUND,
        )

    try:
        version_read = latex_version_repository.get(
            subject_id=subject_id,
            latex_version_id=compilation.latex_version_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeVisualQAFailureReason.LATEX_VERSION_INTEGRITY_FAILURE,
        )
    if version_read.status is ResumeLatexVersionReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeVisualQAFailureReason.LATEX_VERSION_NOT_FOUND
        )
    if (
        version_read.status is not ResumeLatexVersionReadStatus.FOUND
        or not isinstance(version_read.version, ResumeLatexVersion)
    ):
        return _failure(
            command,
            ResumeVisualQAFailureReason.LATEX_VERSION_INTEGRITY_FAILURE,
        )
    version = version_read.version
    if (
        version.subject_id != subject_id
        or version.latex_version_id != compilation.latex_version_id
        or version.source_sha256 != compilation.latex_source_sha256
    ):
        return _failure(
            command,
            ResumeVisualQAFailureReason.LATEX_VERSION_BINDING_MISMATCH,
        )

    try:
        construction_read = construction_repository.get(
            subject_id=subject_id,
            record_id=compilation.construction_record_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeVisualQAFailureReason
            .CONSTRUCTION_RECORD_INTEGRITY_FAILURE,
        )
    if (
        construction_read.status
        is ResumeLatexConstructionReadStatus.NOT_FOUND
    ):
        return _failure(
            command,
            ResumeVisualQAFailureReason.CONSTRUCTION_RECORD_NOT_FOUND,
        )
    if (
        construction_read.status
        is not ResumeLatexConstructionReadStatus.FOUND
        or not isinstance(construction_read.record, LatexBuildProvenance)
    ):
        return _failure(
            command,
            ResumeVisualQAFailureReason
            .CONSTRUCTION_RECORD_INTEGRITY_FAILURE,
        )
    construction = construction_read.record
    if (
        construction.subject_id != subject_id
        or construction.record_id != compilation.construction_record_id
        or construction.build_provenance_binding
        != compilation.construction_binding
        or construction.latex_version_id != version.latex_version_id
        or construction.latex_source_sha256 != version.source_sha256
    ):
        return _failure(
            command,
            ResumeVisualQAFailureReason.CONSTRUCTION_BINDING_MISMATCH,
        )

    try:
        draft_read = draft_repository.get(
            subject_id=subject_id,
            draft_id=construction.tailored_resume_draft_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command, ResumeVisualQAFailureReason.DRAFT_INTEGRITY_FAILURE
        )
    if draft_read.status is TailoredResumeDraftReadStatus.NOT_FOUND:
        return _failure(
            command, ResumeVisualQAFailureReason.DRAFT_NOT_FOUND
        )
    if (
        draft_read.status is not TailoredResumeDraftReadStatus.FOUND
        or not isinstance(draft_read.draft, TailoredResumeDraft)
    ):
        return _failure(
            command, ResumeVisualQAFailureReason.DRAFT_INTEGRITY_FAILURE
        )
    draft = draft_read.draft
    if (
        draft.subject_id != subject_id
        or draft.draft_id != construction.tailored_resume_draft_id
        or draft.draft_content_hash
        != construction.tailored_resume_draft_hash
    ):
        return _failure(
            command, ResumeVisualQAFailureReason.DRAFT_BINDING_MISMATCH
        )

    try:
        pdf_path = active_home.contained_path(compilation.pdf_reference)
        if pdf_path.is_symlink() or not pdf_path.is_file():
            raise ValueError("the managed PDF is not a regular file")
        content = pdf_path.read_bytes()
    except (OSError, PrivateHomeError, TypeError, ValueError):
        return _failure(
            command, ResumeVisualQAFailureReason.PDF_UNREADABLE
        )
    if hashlib.sha256(content).hexdigest() != compilation.pdf_sha256:
        return _failure(
            command, ResumeVisualQAFailureReason.PDF_HASH_DRIFT
        )
    if not content.startswith(b"%PDF-"):
        return _failure(
            command, ResumeVisualQAFailureReason.PDF_UNREADABLE
        )
    projection = _project_pdf(content)
    if projection is None or projection.page_count < 1:
        return _failure(
            command, ResumeVisualQAFailureReason.PDF_UNREADABLE
        )
    if projection.page_count != compilation.page_count:
        return _failure(
            command,
            ResumeVisualQAFailureReason.PDF_PAGE_COUNT_MISMATCH,
            detail=(
                "The stored PDF page count no longer matches its "
                "compilation record."
            ),
        )

    try:
        renderer_description = renderer.describe()
        if not isinstance(renderer_description, PdfRendererDescription):
            raise PdfRendererUnavailableError(
                "the renderer description is invalid"
            )
    except PdfRendererUnavailableError:
        return _failure(
            command,
            ResumeVisualQAFailureReason.RENDERER_UNAVAILABLE,
            status=ResumeVisualQAStatus.DEFERRED_RENDERER_UNAVAILABLE,
            detail="No local PDF renderer is available for visual QA.",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeVisualQAFailureReason.RENDERER_UNAVAILABLE,
            status=ResumeVisualQAStatus.DEFERRED_RENDERER_UNAVAILABLE,
            detail="The PDF renderer could not be described.",
        )

    binding = _visual_qa_binding(
        compilation=compilation,
        version=version,
        draft=draft,
        renderer=renderer_description,
        policy=active_policy,
        metadata=metadata,
    )
    result_id = f"resume-visual-qa-{binding}"
    try:
        existing = visual_qa_repository.get(
            subject_id=subject_id, result_id=result_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE,
            visual_qa_binding=binding,
        )
    if existing.status is ResumeVisualQAReadStatus.INTEGRITY_FAILURE:
        return _failure(
            command,
            ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE,
            visual_qa_binding=binding,
        )
    if (
        existing.status is ResumeVisualQAReadStatus.FOUND
        and existing.result is not None
    ):
        return ReviewResumeVisualQAResult(
            status=ResumeVisualQAStatus.UNCHANGED,
            subject_id=subject_id,
            visual_qa_binding=binding,
            result=existing.result,
            write_result=ResumeVisualQAWriteResult(
                status=ResumeVisualQAWriteStatus.UNCHANGED,
                result=existing.result,
                reason_code=None,
                retryable=False,
            ),
            reason_code=None,
            retryable=False,
            message=(
                "The existing visual QA result is unchanged with verdict "
                f"{existing.result.verdict.value}."
            ),
        )

    deterministic = deterministic_visual_findings(
        projection=projection,
        draft=draft,
        policy=active_policy,
    )

    def _persist(
        *,
        verdict: ResumeVisualQAVerdict,
        findings: tuple[ResumeVisualQAFinding, ...],
        agent_invoked: bool,
        status: ResumeVisualQAStatus,
        reason: ResumeVisualQAFailureReason | None,
        message: str,
    ) -> ReviewResumeVisualQAResult:
        try:
            built = _build_result(
                compilation=compilation,
                version=version,
                draft=draft,
                renderer=renderer_description,
                policy=active_policy,
                metadata=metadata,
                binding=binding,
                page_count=projection.page_count,
                verdict=verdict,
                findings=findings,
                agent_invoked=agent_invoked,
                now=now,
            )
        except (TypeError, ValueError):
            return _failure(
                command,
                ResumeVisualQAFailureReason.RESULT_INTEGRITY_FAILURE,
                visual_qa_binding=binding,
            )
        try:
            write_result = visual_qa_repository.save(built)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                ResumeVisualQAFailureReason.RESULT_PERSISTENCE_FAILED,
                retryable=True,
                visual_qa_binding=binding,
            )
        if write_result.status is ResumeVisualQAWriteStatus.FAILED:
            return _failure(
                command,
                write_result.reason_code
                or ResumeVisualQAFailureReason.RESULT_PERSISTENCE_FAILED,
                retryable=write_result.retryable,
                visual_qa_binding=binding,
            )
        return ReviewResumeVisualQAResult(
            status=status,
            subject_id=subject_id,
            visual_qa_binding=binding,
            result=write_result.result,
            write_result=write_result,
            reason_code=reason,
            retryable=False,
            message=message,
        )

    if any(
        item.severity is ResumeVisualQAFindingSeverity.BLOCKING
        for item in deterministic
    ):
        return _persist(
            verdict=ResumeVisualQAVerdict.REVISION_REQUIRED,
            findings=deterministic,
            agent_invoked=False,
            status=ResumeVisualQAStatus.CREATED,
            reason=None,
            message=(
                "Deterministic visual QA requires a layout revision; nothing "
                "was modified."
            ),
        )

    try:
        pages = renderer.render(content)
        if (
            not isinstance(pages, tuple)
            or not pages
            or any(not isinstance(item, RenderedPage) for item in pages)
        ):
            raise PdfRendererUnavailableError(
                "the renderer returned an invalid page set"
            )
    except PdfRendererUnavailableError:
        return _failure(
            command,
            ResumeVisualQAFailureReason.RENDERER_UNAVAILABLE,
            status=ResumeVisualQAStatus.DEFERRED_RENDERER_UNAVAILABLE,
            visual_qa_binding=binding,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            ResumeVisualQAFailureReason.RENDERER_UNAVAILABLE,
            status=ResumeVisualQAStatus.DEFERRED_RENDERER_UNAVAILABLE,
            visual_qa_binding=binding,
        )

    rendered_numbers = tuple(page.page_number for page in pages)
    if rendered_numbers != tuple(range(1, len(pages) + 1)):
        return _failure(
            command,
            ResumeVisualQAFailureReason.RENDERER_UNAVAILABLE,
            status=ResumeVisualQAStatus.DEFERRED_RENDERER_UNAVAILABLE,
            visual_qa_binding=binding,
            detail="The renderer did not return pages in stable order.",
        )
    if len(pages) != projection.page_count:
        mismatch = _build_finding(
            order=len(deterministic),
            finding_type=ResumeVisualQAFindingType.PAGE_COUNT_MISMATCH,
            source=ResumeVisualQAFindingSource.DETERMINISTIC,
            page_number=0,
            explanation=(
                f"The renderer produced {len(pages)} pages while the PDF "
                f"parses as {projection.page_count}."
            ),
        )
        return _persist(
            verdict=ResumeVisualQAVerdict.REVISION_REQUIRED,
            findings=deterministic + (mismatch,),
            agent_invoked=False,
            status=ResumeVisualQAStatus.CREATED,
            reason=None,
            message="The renderer and the PDF disagree on page count.",
        )

    context = ResumeVisualQAContext(
        subject_id=subject_id,
        pages=tuple(
            ResumeVisualQAPageView(
                page_number=page.page_number,
                width_px=page.width_px,
                height_px=page.height_px,
                image_format=page.image_format,
                image_bytes=page.image_bytes,
            )
            for page in pages
        ),
        deterministic_findings=tuple(
            ResumeVisualQAFindingView(
                finding_type=item.finding_type,
                severity=item.severity,
                page_number=item.page_number,
                explanation=item.explanation,
            )
            for item in deterministic
        ),
        policy=active_policy.to_dict(),
        policy_version=active_policy.policy_version,
        agent_policy=RESUME_VISUAL_QA_AGENT_POLICY,
    )
    try:
        output = await agent.review(context)
    except TimeoutError:
        return _failure(
            command,
            ResumeVisualQAFailureReason.AGENT_TIMEOUT,
            retryable=True,
            visual_qa_binding=binding,
        )
    except ResumeVisualQAAgentUnavailableError:
        return _failure(
            command,
            ResumeVisualQAFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            visual_qa_binding=binding,
        )
    except Exception:
        return _failure(
            command,
            ResumeVisualQAFailureReason.AGENT_UNAVAILABLE,
            retryable=True,
            visual_qa_binding=binding,
        )

    deferred_detail: str | None = None
    semantic: tuple[ResumeVisualQAFinding, ...] = ()
    if not isinstance(output, ResumeVisualQAAgentOutput):
        deferred_detail = "the Agent did not return a typed structured result"
    elif output.verdict is ResumeVisualQAAgentVerdict.UNCERTAIN:
        deferred_detail = "the Agent could not reach a reliable verdict"
    else:
        try:
            semantic = _agent_findings(
                output=output,
                pages=pages,
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
                    ResumeVisualQAFindingType.AGENT_OUTPUT_UNRELIABLE
                ),
                source=ResumeVisualQAFindingSource.DETERMINISTIC,
                page_number=0,
                explanation=(
                    f"Visual QA needs human review because {deferred_detail}."
                ),
            ),
        )
        return _persist(
            verdict=ResumeVisualQAVerdict.DEFERRED,
            findings=findings,
            agent_invoked=True,
            status=ResumeVisualQAStatus.DEFERRED_NEEDS_HUMAN,
            reason=ResumeVisualQAFailureReason.AGENT_OUTPUT_UNRELIABLE,
            message=f"Visual QA needs human review because {deferred_detail}.",
        )

    findings = deterministic + semantic
    blocking = any(
        item.severity is ResumeVisualQAFindingSeverity.BLOCKING
        for item in findings
    )
    return _persist(
        verdict=(
            ResumeVisualQAVerdict.REVISION_REQUIRED
            if blocking
            else ResumeVisualQAVerdict.PASSED
        ),
        findings=findings,
        agent_invoked=True,
        status=ResumeVisualQAStatus.CREATED,
        reason=None,
        message=(
            "Visual QA requires a layout revision; nothing was modified."
            if blocking
            else "Visual QA passed: the compiled resume looks sound."
        ),
    )


__all__ = [
    "ADVISORY_FINDING_TYPES",
    "AGENT_FINDING_TYPES",
    "MAX_VISUAL_QA_EXPLANATION_CHARS",
    "MAX_VISUAL_QA_FINDINGS",
    "PrivateHomeResumeVisualQARepository",
    "RESUME_VISUAL_QA_AGENT_POLICY",
    "RESUME_VISUAL_QA_CONTRACT_VERSION",
    "RESUME_VISUAL_QA_POLICY_VERSION",
    "ResumeVisualQAAgentFinding",
    "ResumeVisualQAAgentMetadata",
    "ResumeVisualQAAgentOutput",
    "ResumeVisualQAAgentPort",
    "ResumeVisualQAAgentUnavailableError",
    "ResumeVisualQAAgentVerdict",
    "ResumeVisualQAContext",
    "ResumeVisualQAFailureReason",
    "ResumeVisualQAFinding",
    "ResumeVisualQAFindingSeverity",
    "ResumeVisualQAFindingSource",
    "ResumeVisualQAFindingType",
    "ResumeVisualQAFindingView",
    "ResumeVisualQAPageView",
    "ResumeVisualQAPolicy",
    "ResumeVisualQAReadResult",
    "ResumeVisualQAReadStatus",
    "ResumeVisualQARepository",
    "ResumeVisualQAResult",
    "ResumeVisualQAStatus",
    "ResumeVisualQAVerdict",
    "ResumeVisualQAWriteResult",
    "ResumeVisualQAWriteStatus",
    "ReviewResumeVisualQACommand",
    "ReviewResumeVisualQAResult",
    "VisualBoundingBox",
    "deterministic_visual_findings",
    "resume_visual_qa_finding_id",
    "review_resume_visual_qa",
]
