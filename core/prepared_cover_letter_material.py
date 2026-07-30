"""Deterministically publish one fact-QA-passed cover letter as a PDF."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from io import BytesIO
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError

from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .application_preparation_orchestrator import (
    COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    CoverLetterPublicationStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PublicPreparationStageResult,
)
from .cover_letter_draft import (
    CoverLetterDraft,
    CoverLetterDraftReadStatus,
    CoverLetterDraftRepository,
)
from .cover_letter_fact_qa import (
    COVER_LETTER_FACT_QA_CONTRACT_VERSION,
    CoverLetterFactQAReadStatus,
    CoverLetterFactQAFindingSeverity,
    CoverLetterFactQARepository,
    CoverLetterFactQAResult,
    CoverLetterFactQAVerdict,
)
from .job_discovery import (
    JobPosting,
    JobPostingReadRepository,
    JobPostingRepositoryError,
)
from .latex_compiler import (
    MAX_PDF_BYTES,
    LatexCompileOutcome,
    LatexCompileRequest,
    LatexCompileStatus,
    LatexCompilerDescription,
    LatexCompilerPort,
    LatexCompilerUnavailableError,
)
from .private_home import PrivateHome, PrivateHomeError
from .publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    PublicationStoppedSourceLineage,
    create_publication_stopped_source_lineage,
)


PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION = (
    "prepared-cover-letter-material-v1"
)
COVER_LETTER_PUBLICATION_POLICY_VERSION = (
    "cover-letter-publication-one-page-v1"
)
MANAGED_COVER_LETTER_TEMPLATE_ID = "managed-cover-letter-one-page-v1"
MANAGED_COVER_LETTER_TEMPLATE_VERSION = "1"

_GREETING_TOKEN = "%%JOBOPS_COVER_LETTER_GREETING%%"
_PARAGRAPHS_TOKEN = "%%JOBOPS_COVER_LETTER_PARAGRAPHS%%"
_CLOSING_TOKEN = "%%JOBOPS_COVER_LETTER_CLOSING%%"
_TEMPLATE_TOKENS = (_GREETING_TOKEN, _PARAGRAPHS_TOKEN, _CLOSING_TOKEN)

MANAGED_COVER_LETTER_TEMPLATE_SOURCE = r"""\documentclass[11pt,letterpaper]{article}
\usepackage[T1]{fontenc}
\pagestyle{empty}
\setlength{\topmargin}{-0.55in}
\setlength{\oddsidemargin}{-0.25in}
\setlength{\evensidemargin}{-0.25in}
\setlength{\textwidth}{7.0in}
\setlength{\textheight}{9.6in}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.75em}
\hyphenpenalty=10000
\exhyphenpenalty=10000
\emergencystretch=3em
\begin{document}
% JOBOPS_GREETING_BEGIN
%%JOBOPS_COVER_LETTER_GREETING%%
% JOBOPS_GREETING_END

%%JOBOPS_COVER_LETTER_PARAGRAPHS%%

% JOBOPS_CLOSING_BEGIN
%%JOBOPS_COVER_LETTER_CLOSING%%
% JOBOPS_CLOSING_END
\end{document}
"""

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_MATERIAL_ID_PATTERN = re.compile(
    r"^prepared-cover-letter-material-[a-f0-9]{64}$"
)
_FORBIDDEN_LATEX_CAPABILITY = re.compile(
    r"\\(?:"
    r"write18|openin|openout|read|write|input|include|includeonly|"
    r"includegraphics|lstinputlisting|subfile|includepdf|bibliography|"
    r"addbibresource|special|directlua|"
    r"catcode|csname|newread|newwrite"
    r")(?=[^A-Za-z@]|$)",
    re.IGNORECASE,
)
_DOCUMENT_CLASS_PATTERN = re.compile(
    r"\\documentclass(?:\[[^\]]*\])?\{([^{}]+)\}"
)
_PACKAGE_COMMAND_PATTERN = re.compile(
    r"\\(?:usepackage|RequirePackage)\b"
)
_PACKAGE_DECLARATION_PATTERN = re.compile(
    r"\\(?:usepackage|RequirePackage)(?:\[([^\]]*)\])?\{([^{}]+)\}"
)
_PLACEHOLDER_PATTERN = re.compile(
    r"\[[^\]\n]{1,80}\]|\{[^}\n]{1,80}\}|\bTBD\b|\bT\.B\.D\.\b"
    r"|\bhiring manager'?s? name\b|\bcompany name\b|\bxxx+\b"
    r"|JOBOPS_COVER_LETTER_(?:GREETING|PARAGRAPHS|CLOSING)",
    re.IGNORECASE,
)
_LIGATURES = str.maketrans(
    {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "st",
        "\ufb06": "st",
    }
)


class PreparedCoverLetterMaterialRole(str, Enum):
    COVER_LETTER = "COVER_LETTER"


class PreparedCoverLetterMaterialStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_READY = "NOT_READY"
    DEFERRED_COMPILER_UNAVAILABLE = "DEFERRED_COMPILER_UNAVAILABLE"
    DEFERRED_COMPILATION_ERROR = "DEFERRED_COMPILATION_ERROR"
    DEFERRED_LAYOUT_OVERFLOW = "DEFERRED_LAYOUT_OVERFLOW"
    FAILED = "FAILED"


class PreparedCoverLetterMaterialWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class PreparedCoverLetterMaterialReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class PreparedCoverLetterMaterialNotReadyReason(str, Enum):
    FACT_QA_NOT_PASSED = "FACT_QA_NOT_PASSED"
    JOB_BINDING_MISMATCH = "JOB_BINDING_MISMATCH"
    DRAFT_BINDING_MISMATCH = "DRAFT_BINDING_MISMATCH"
    FACT_QA_BINDING_MISMATCH = "FACT_QA_BINDING_MISMATCH"


class PreparedCoverLetterMaterialFailureReason(str, Enum):
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
    COMPILER_UNAVAILABLE = "COMPILER_UNAVAILABLE"
    COMPILATION_ERROR = "COMPILATION_ERROR"
    LAYOUT_OVERFLOW = "LAYOUT_OVERFLOW"
    PDF_INVALID = "PDF_INVALID"
    PDF_TEXT_MISMATCH = "PDF_TEXT_MISMATCH"
    ARTIFACT_PERSISTENCE_FAILED = "ARTIFACT_PERSISTENCE_FAILED"
    MATERIAL_PERSISTENCE_FAILED = "MATERIAL_PERSISTENCE_FAILED"
    MATERIAL_INTEGRITY_FAILURE = "MATERIAL_INTEGRITY_FAILURE"


class CoverLetterOverflowCorrectionConstraintStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionConstraint:
    directive_id: str
    directive_version: int
    directive_hash: str
    subject_id: str
    application_plan_id: str
    correction_target_id: str
    correction_target_hash: str
    safe_preview_id: str
    safe_preview_hash: str
    publication_result_id: str
    overflow_evaluation_id: str
    overflow_evaluation_version: str
    source_record_id: str
    source_version: str
    source_content_hash: str
    correction_mode: str

    def __post_init__(self) -> None:
        for name, value in (
            ("directive_id", self.directive_id),
            ("subject_id", self.subject_id),
            ("application_plan_id", self.application_plan_id),
            ("correction_target_id", self.correction_target_id),
            ("safe_preview_id", self.safe_preview_id),
            ("publication_result_id", self.publication_result_id),
            ("overflow_evaluation_id", self.overflow_evaluation_id),
            ("source_record_id", self.source_record_id),
            ("source_version", self.source_version),
        ):
            _clean_text(name, value, maximum=300)
        if type(self.directive_version) is not int or not 1 <= self.directive_version <= 3:
            raise ValueError("Cover Letter format profile is outside its bound")
        for name in (
            "directive_hash",
            "correction_target_hash",
            "safe_preview_hash",
            "source_content_hash",
        ):
            _require_hash(name, getattr(self, name))
        if (
            self.overflow_evaluation_version
            != PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION
            or self.correction_mode != "REFORMAT_EXISTING_CONTENT"
            or self.source_record_id
            != f"cover-letter-latex-source-{self.source_content_hash}"
        ):
            raise ValueError("Cover Letter correction constraint is invalid")

    def identity_dict(self) -> dict[str, str]:
        return {
            "application_plan_id": self.application_plan_id,
            "correction_mode": self.correction_mode,
            "correction_target_hash": self.correction_target_hash,
            "correction_target_id": self.correction_target_id,
            "directive_hash": self.directive_hash,
            "directive_id": self.directive_id,
            "directive_version": str(self.directive_version),
            "overflow_evaluation_id": self.overflow_evaluation_id,
            "overflow_evaluation_version": self.overflow_evaluation_version,
            "publication_result_id": self.publication_result_id,
            "safe_preview_hash": self.safe_preview_hash,
            "safe_preview_id": self.safe_preview_id,
            "source_content_hash": self.source_content_hash,
            "source_record_id": self.source_record_id,
            "source_version": self.source_version,
            "subject_id": self.subject_id,
        }


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionConstraintReadResult:
    status: CoverLetterOverflowCorrectionConstraintStatus
    constraint: CoverLetterOverflowCorrectionConstraint | None


@runtime_checkable
class CoverLetterOverflowCorrectionDirectiveProvider(Protocol):
    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> CoverLetterOverflowCorrectionConstraintReadResult: ...


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the publication contract")
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
        raise ValueError("published_at is invalid")
    return _require_aware(
        "published_at",
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


def cover_letter_source_reference(
    *, subject_id: str, source_sha256: str
) -> str:
    _clean_text("subject_id", subject_id, maximum=160)
    _require_hash("source_sha256", source_sha256)
    return str(
        PurePosixPath("state")
        / "preparation"
        / "cover-letter-latex-sources"
        / _subject_storage_key(subject_id)
        / f"{source_sha256}.tex"
    )


def cover_letter_pdf_reference(
    *, subject_id: str, pdf_sha256: str
) -> str:
    _clean_text("subject_id", subject_id, maximum=160)
    _require_hash("pdf_sha256", pdf_sha256)
    return str(
        PurePosixPath("state")
        / "preparation"
        / "compiled-cover-letters"
        / _subject_storage_key(subject_id)
        / f"{pdf_sha256}.pdf"
    )


@dataclass(frozen=True, slots=True)
class ManagedCoverLetterTemplate:
    template_id: str
    template_version: str
    template_source: str
    template_sha256: str

    def __post_init__(self) -> None:
        template_id = _clean_text(
            "template_id", self.template_id, maximum=120
        )
        if template_id != MANAGED_COVER_LETTER_TEMPLATE_ID:
            raise ValueError("the managed cover-letter template is unsupported")
        version = _clean_text(
            "template_version", self.template_version, maximum=40
        )
        if version != MANAGED_COVER_LETTER_TEMPLATE_VERSION:
            raise ValueError("the managed template version is unsupported")
        if not isinstance(self.template_source, str):
            raise TypeError("template_source must be text")
        source_bytes = self.template_source.encode("utf-8")
        source_hash = _require_hash(
            "template_sha256", self.template_sha256
        )
        if hashlib.sha256(source_bytes).hexdigest() != source_hash:
            raise ValueError("template_sha256 does not match the UTF-8 bytes")
        validate_managed_cover_letter_template(self.template_source)


@runtime_checkable
class ManagedCoverLetterTemplateProvider(Protocol):
    def get(self) -> ManagedCoverLetterTemplate:
        """Return the one managed P2b2d template."""


class DefaultManagedCoverLetterTemplateProvider:
    """Return the single source-controlled V1 cover-letter template."""

    def get(self) -> ManagedCoverLetterTemplate:
        source = MANAGED_COVER_LETTER_TEMPLATE_SOURCE
        return ManagedCoverLetterTemplate(
            template_id=MANAGED_COVER_LETTER_TEMPLATE_ID,
            template_version=MANAGED_COVER_LETTER_TEMPLATE_VERSION,
            template_source=source,
            template_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )


def validate_managed_cover_letter_template(source: str) -> None:
    """Fail closed on external dependencies and active TeX capabilities."""

    if not isinstance(source, str) or not source.strip():
        raise ValueError("managed template must be non-empty text")
    if "\x00" in source or _FORBIDDEN_LATEX_CAPABILITY.search(source):
        raise ValueError("managed template contains a forbidden capability")
    document_classes = _DOCUMENT_CLASS_PATTERN.findall(source)
    if document_classes != ["article"]:
        raise ValueError("managed template must use exactly the article class")
    if _PACKAGE_COMMAND_PATTERN.findall(source) != [r"\usepackage"]:
        raise ValueError("managed template package cardinality is invalid")
    if _PACKAGE_DECLARATION_PATTERN.findall(source) != [("T1", "fontenc")]:
        raise ValueError("managed template has an unallowlisted package")
    if source.count(r"\begin{document}") != 1:
        raise ValueError("managed template must begin one document")
    if source.count(r"\end{document}") != 1:
        raise ValueError("managed template must end one document")
    for token in _TEMPLATE_TOKENS:
        if source.count(token) != 1:
            raise ValueError("managed template token cardinality is invalid")


_LATEX_ESCAPE_MAP = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "%": r"\%",
    "_": r"\_",
    "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
}


def escape_cover_letter_latex_text(text: str) -> str:
    """Escape once, character by character, without a second replacement pass."""

    if not isinstance(text, str):
        raise TypeError("cover-letter text must be a string")
    return "".join(_LATEX_ESCAPE_MAP.get(character, character) for character in text)


def render_cover_letter_latex(
    draft: CoverLetterDraft,
    template: ManagedCoverLetterTemplate,
) -> str:
    """Render only the Draft greeting, ordered paragraphs and closing."""

    if not isinstance(draft, CoverLetterDraft):
        raise TypeError("draft must be a CoverLetterDraft")
    if not isinstance(template, ManagedCoverLetterTemplate):
        raise TypeError("template must be a ManagedCoverLetterTemplate")

    paragraph_blocks = []
    for paragraph in draft.paragraphs:
        paragraph_blocks.append(
            "\n".join(
                (
                    f"% JOBOPS_PARAGRAPH {paragraph.paragraph_id}",
                    escape_cover_letter_latex_text(paragraph.text),
                    "% JOBOPS_PARAGRAPH_END",
                )
            )
        )
    replacements = {
        _GREETING_TOKEN: escape_cover_letter_latex_text(draft.greeting),
        _PARAGRAPHS_TOKEN: "\n\n".join(paragraph_blocks),
        _CLOSING_TOKEN: escape_cover_letter_latex_text(draft.closing),
    }
    source = template.template_source
    for token, replacement in replacements.items():
        source = source.replace(token, replacement)
    validate_rendered_cover_letter_latex(source, draft)
    return source


def validate_rendered_cover_letter_latex(
    source: str, draft: CoverLetterDraft
) -> None:
    """Verify exact, ordered paragraph blocks and a capability-safe source."""

    if not isinstance(source, str) or not isinstance(draft, CoverLetterDraft):
        raise TypeError("source and draft are required")
    if "\x00" in source or _FORBIDDEN_LATEX_CAPABILITY.search(source):
        raise ValueError("rendered source contains a forbidden capability")
    if any(token in source for token in _TEMPLATE_TOKENS):
        raise ValueError("rendered source contains an unresolved token")
    if _DOCUMENT_CLASS_PATTERN.findall(source) != ["article"]:
        raise ValueError("rendered source changed the document class")
    if _PACKAGE_COMMAND_PATTERN.findall(source) != [r"\usepackage"]:
        raise ValueError("rendered source package cardinality is invalid")
    if _PACKAGE_DECLARATION_PATTERN.findall(source) != [("T1", "fontenc")]:
        raise ValueError("rendered source has an unallowlisted package")
    if source.count(r"\begin{document}") != 1:
        raise ValueError("rendered source must begin one document")
    if source.count(r"\end{document}") != 1:
        raise ValueError("rendered source must end one document")

    greeting_block = "\n".join(
        (
            "% JOBOPS_GREETING_BEGIN",
            escape_cover_letter_latex_text(draft.greeting),
            "% JOBOPS_GREETING_END",
        )
    )
    closing_block = "\n".join(
        (
            "% JOBOPS_CLOSING_BEGIN",
            escape_cover_letter_latex_text(draft.closing),
            "% JOBOPS_CLOSING_END",
        )
    )
    if source.count(greeting_block) != 1 or source.count(closing_block) != 1:
        raise ValueError("greeting or closing fidelity failed")

    positions: list[int] = []
    for paragraph in draft.paragraphs:
        block = "\n".join(
            (
                f"% JOBOPS_PARAGRAPH {paragraph.paragraph_id}",
                escape_cover_letter_latex_text(paragraph.text),
                "% JOBOPS_PARAGRAPH_END",
            )
        )
        if source.count(block) != 1:
            raise ValueError("a paragraph is missing or duplicated")
        if source.count(paragraph.paragraph_id) != 1:
            raise ValueError("paragraph identity cardinality is invalid")
        positions.append(source.index(block))
    if positions != sorted(positions):
        raise ValueError("paragraph order changed during rendering")


_FORMAT_ONLY_PROFILES = (
    (
        (r"\setlength{\topmargin}{-0.55in}", r"\setlength{\topmargin}{-0.70in}"),
        (r"\setlength{\textheight}{9.6in}", r"\setlength{\textheight}{9.9in}"),
        (r"\setlength{\parskip}{0.75em}", r"\setlength{\parskip}{0.45em}"),
    ),
    (
        (r"\setlength{\topmargin}{-0.70in}", r"\setlength{\topmargin}{-0.80in}"),
        (r"\setlength{\textheight}{9.9in}", r"\setlength{\textheight}{10.1in}"),
        (r"\setlength{\parskip}{0.45em}", r"\setlength{\parskip}{0.30em}"),
    ),
    (
        (r"\setlength{\topmargin}{-0.80in}", r"\setlength{\topmargin}{-0.85in}"),
        (r"\setlength{\textheight}{10.1in}", r"\setlength{\textheight}{10.2in}"),
        (r"\setlength{\parskip}{0.30em}", r"\setlength{\parskip}{0.20em}"),
    ),
)


def reformat_cover_letter_latex(
    source: str,
    draft: CoverLetterDraft,
    constraint: CoverLetterOverflowCorrectionConstraint,
) -> str:
    """Apply one closed format profile while preserving the document body."""

    if (
        not isinstance(source, str)
        or not isinstance(draft, CoverLetterDraft)
        or not isinstance(
            constraint, CoverLetterOverflowCorrectionConstraint
        )
    ):
        raise TypeError("format-only correction inputs must be typed")
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    if (
        source_hash != constraint.source_content_hash
        or constraint.source_record_id
        != f"cover-letter-latex-source-{source_hash}"
    ):
        raise ValueError("format-only correction source binding drifted")
    begin = r"\begin{document}"
    if source.count(begin) != 1:
        raise ValueError("Cover Letter document body is ambiguous")
    preamble, body = source.split(begin, 1)
    reformatted_preamble = preamble
    profile = _FORMAT_ONLY_PROFILES[constraint.directive_version - 1]
    for current, replacement in profile:
        if reformatted_preamble.count(current) != 1:
            raise ValueError("managed layout parameter is unavailable")
        reformatted_preamble = reformatted_preamble.replace(
            current, replacement
        )
    directive_marker = (
        "% JOBOPS_FORMAT_DIRECTIVE "
        f"{constraint.directive_id} {constraint.directive_hash}\n"
    )
    corrected = reformatted_preamble + directive_marker + begin + body
    if corrected.split(begin, 1)[1] != body:
        raise ValueError("Cover Letter text region changed")
    validate_rendered_cover_letter_latex(corrected, draft)
    if expected_cover_letter_text_projection(draft) != (
        normalize_cover_letter_text_projection(
            " ".join(
                (
                    draft.greeting,
                    *(paragraph.text for paragraph in draft.paragraphs),
                    draft.closing,
                )
            )
        )
    ):
        raise ValueError("Cover Letter canonical text identity changed")
    return corrected


def normalize_cover_letter_text_projection(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("text projection must be a string")
    normalized = unicodedata.normalize("NFKC", text.translate(_LIGATURES))
    normalized = normalized.replace("\u00ad", "")
    return re.sub(r"\s+", " ", normalized).strip()


def expected_cover_letter_text_projection(draft: CoverLetterDraft) -> str:
    if not isinstance(draft, CoverLetterDraft):
        raise TypeError("draft must be a CoverLetterDraft")
    return normalize_cover_letter_text_projection(
        " ".join(
            (
                draft.greeting,
                *(paragraph.text for paragraph in draft.paragraphs),
                draft.closing,
            )
        )
    )


def inspect_cover_letter_pdf(content: bytes) -> tuple[int, str] | None:
    """Parse a PDF once and return page count plus normalized visible text."""

    if not isinstance(content, bytes):
        raise TypeError("content must be bytes")
    try:
        with pdfplumber.open(BytesIO(content)) as document:
            pages = len(document.pages)
            text = "\n".join(
                page.extract_text() or "" for page in document.pages
            )
    except (PDFSyntaxError, OSError, TypeError, ValueError):
        return None
    except Exception:
        return None
    return pages, normalize_cover_letter_text_projection(text)


def cover_letter_pdf_text_is_faithful(
    content: bytes, draft: CoverLetterDraft
) -> bool:
    inspected = inspect_cover_letter_pdf(content)
    if inspected is None:
        return False
    _, projection = inspected
    expected = expected_cover_letter_text_projection(draft)
    return (
        bool(projection)
        and projection == expected
        and _PLACEHOLDER_PATTERN.search(projection) is None
    )


def _publication_identity(
    *,
    contract_version: str,
    publication_policy_version: str,
    subject_id: str,
    application_plan_id: str,
    plan_user_instructions_hash: str,
    job_id: str,
    job_revision: int,
    job_content_hash: str,
    cover_letter_draft_id: str,
    draft_content_hash: str,
    evidence_snapshot_id: str,
    evidence_snapshot_hash: str,
    fact_qa_result_id: str,
    fact_qa_result_hash: str,
    template_id: str,
    template_version: str,
    template_sha256: str,
    latex_source_sha256: str,
    compiler_engine: str,
    compiler_version: str,
    compile_policy_version: str,
    sandbox_policy_version: str,
    normalized_flags: tuple[str, ...],
    material_role: PreparedCoverLetterMaterialRole,
) -> dict[str, Any]:
    return {
        "application_plan_id": application_plan_id,
        "compile_policy_version": compile_policy_version,
        "compiler_engine": compiler_engine,
        "compiler_version": compiler_version,
        "contract_version": contract_version,
        "cover_letter_draft_id": cover_letter_draft_id,
        "draft_content_hash": draft_content_hash,
        "evidence_snapshot_hash": evidence_snapshot_hash,
        "evidence_snapshot_id": evidence_snapshot_id,
        "fact_qa_result_hash": fact_qa_result_hash,
        "fact_qa_result_id": fact_qa_result_id,
        "job_content_hash": job_content_hash,
        "job_id": job_id,
        "job_revision": job_revision,
        "latex_source_sha256": latex_source_sha256,
        "material_role": material_role.value,
        "normalized_flags": list(normalized_flags),
        "plan_user_instructions_hash": plan_user_instructions_hash,
        "publication_policy_version": publication_policy_version,
        "sandbox_policy_version": sandbox_policy_version,
        "subject_id": subject_id,
        "template_id": template_id,
        "template_sha256": template_sha256,
        "template_version": template_version,
    }


def cover_letter_publication_binding(**values: Any) -> str:
    return _canonical_hash(_publication_identity(**values))


def prepared_cover_letter_material_id(**values: Any) -> str:
    return (
        "prepared-cover-letter-material-"
        + cover_letter_publication_binding(**values)
    )


@dataclass(frozen=True, slots=True)
class PreparedCoverLetterMaterial:
    material_id: str
    contract_version: str
    publication_policy_version: str
    publication_binding: str
    subject_id: str
    application_plan_id: str
    plan_user_instructions_hash: str
    job_id: str
    job_revision: int
    job_content_hash: str
    cover_letter_draft_id: str
    draft_content_hash: str
    evidence_snapshot_id: str
    evidence_snapshot_hash: str
    fact_qa_result_id: str
    fact_qa_result_hash: str
    template_id: str
    template_version: str
    template_sha256: str
    latex_source_reference: str
    latex_source_sha256: str
    latex_source_byte_size: int
    compiler_engine: str
    compiler_version: str
    compile_policy_version: str
    sandbox_policy_version: str
    normalized_flags: tuple[str, ...]
    pdf_reference: str
    pdf_sha256: str
    pdf_byte_size: int
    page_count: int
    pdf_text_projection_hash: str
    material_role: PreparedCoverLetterMaterialRole
    material_content_hash: str
    published_at: datetime

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION:
            raise ValueError("publication contract is unsupported")
        policy = _clean_text(
            "publication_policy_version",
            self.publication_policy_version,
            maximum=80,
        )
        if policy != COVER_LETTER_PUBLICATION_POLICY_VERSION:
            raise ValueError("publication policy is unsupported")
        subject = _clean_text("subject_id", self.subject_id, maximum=160)
        plan_id = _clean_text(
            "application_plan_id", self.application_plan_id, maximum=160
        )
        plan_hash = _require_hash(
            "plan_user_instructions_hash",
            self.plan_user_instructions_hash,
        )
        job_id = _clean_text("job_id", self.job_id, maximum=160)
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be positive")
        job_hash = _require_hash("job_content_hash", self.job_content_hash)
        draft_id = _clean_text(
            "cover_letter_draft_id",
            self.cover_letter_draft_id,
            maximum=160,
        )
        draft_hash = _require_hash(
            "draft_content_hash", self.draft_content_hash
        )
        snapshot_id = _clean_text(
            "evidence_snapshot_id", self.evidence_snapshot_id, maximum=160
        )
        snapshot_hash = _require_hash(
            "evidence_snapshot_hash", self.evidence_snapshot_hash
        )
        qa_id = _clean_text(
            "fact_qa_result_id", self.fact_qa_result_id, maximum=160
        )
        qa_hash = _require_hash(
            "fact_qa_result_hash", self.fact_qa_result_hash
        )
        template_id = _clean_text(
            "template_id", self.template_id, maximum=120
        )
        template_version = _clean_text(
            "template_version", self.template_version, maximum=40
        )
        template_hash = _require_hash(
            "template_sha256", self.template_sha256
        )
        source_hash = _require_hash(
            "latex_source_sha256", self.latex_source_sha256
        )
        if self.latex_source_reference != cover_letter_source_reference(
            subject_id=subject, source_sha256=source_hash
        ):
            raise ValueError("LaTeX source reference does not match its binding")
        if (
            type(self.latex_source_byte_size) is not int
            or self.latex_source_byte_size <= 0
        ):
            raise ValueError("latex_source_byte_size must be positive")
        engine = _clean_text(
            "compiler_engine", self.compiler_engine, maximum=80
        )
        compiler_version = _clean_text(
            "compiler_version", self.compiler_version, maximum=200
        )
        compile_policy = _clean_text(
            "compile_policy_version",
            self.compile_policy_version,
            maximum=80,
        )
        sandbox_policy = _clean_text(
            "sandbox_policy_version",
            self.sandbox_policy_version,
            maximum=80,
        )
        if not isinstance(self.normalized_flags, tuple) or any(
            not isinstance(item, str) or not item
            for item in self.normalized_flags
        ):
            raise TypeError("normalized_flags must be a tuple of strings")
        pdf_hash = _require_hash("pdf_sha256", self.pdf_sha256)
        if self.pdf_reference != cover_letter_pdf_reference(
            subject_id=subject, pdf_sha256=pdf_hash
        ):
            raise ValueError("PDF reference does not match its binding")
        if type(self.pdf_byte_size) is not int or self.pdf_byte_size <= 0:
            raise ValueError("pdf_byte_size must be positive")
        if self.page_count != 1:
            raise ValueError("a prepared cover letter must have exactly one page")
        _require_hash(
            "pdf_text_projection_hash", self.pdf_text_projection_hash
        )
        role = PreparedCoverLetterMaterialRole(self.material_role)
        object.__setattr__(self, "material_role", role)
        published = _require_aware("published_at", self.published_at)

        identity = {
            "contract_version": contract,
            "publication_policy_version": policy,
            "subject_id": subject,
            "application_plan_id": plan_id,
            "plan_user_instructions_hash": plan_hash,
            "job_id": job_id,
            "job_revision": self.job_revision,
            "job_content_hash": job_hash,
            "cover_letter_draft_id": draft_id,
            "draft_content_hash": draft_hash,
            "evidence_snapshot_id": snapshot_id,
            "evidence_snapshot_hash": snapshot_hash,
            "fact_qa_result_id": qa_id,
            "fact_qa_result_hash": qa_hash,
            "template_id": template_id,
            "template_version": template_version,
            "template_sha256": template_hash,
            "latex_source_sha256": source_hash,
            "compiler_engine": engine,
            "compiler_version": compiler_version,
            "compile_policy_version": compile_policy,
            "sandbox_policy_version": sandbox_policy,
            "normalized_flags": self.normalized_flags,
            "material_role": role,
        }
        expected_binding = cover_letter_publication_binding(**identity)
        if self.publication_binding != expected_binding:
            raise ValueError("publication_binding is invalid")
        expected_id = prepared_cover_letter_material_id(**identity)
        if (
            not isinstance(self.material_id, str)
            or _MATERIAL_ID_PATTERN.fullmatch(self.material_id) is None
            or self.material_id != expected_id
        ):
            raise ValueError("material_id does not match its binding")
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "publication_policy_version", policy)
        object.__setattr__(self, "published_at", published)
        content_hash = _require_hash(
            "material_content_hash", self.material_content_hash
        )
        if content_hash != _canonical_hash(self.canonical_dict()):
            raise ValueError("material_content_hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return _publication_identity(
            contract_version=self.contract_version,
            publication_policy_version=self.publication_policy_version,
            subject_id=self.subject_id,
            application_plan_id=self.application_plan_id,
            plan_user_instructions_hash=self.plan_user_instructions_hash,
            job_id=self.job_id,
            job_revision=self.job_revision,
            job_content_hash=self.job_content_hash,
            cover_letter_draft_id=self.cover_letter_draft_id,
            draft_content_hash=self.draft_content_hash,
            evidence_snapshot_id=self.evidence_snapshot_id,
            evidence_snapshot_hash=self.evidence_snapshot_hash,
            fact_qa_result_id=self.fact_qa_result_id,
            fact_qa_result_hash=self.fact_qa_result_hash,
            template_id=self.template_id,
            template_version=self.template_version,
            template_sha256=self.template_sha256,
            latex_source_sha256=self.latex_source_sha256,
            compiler_engine=self.compiler_engine,
            compiler_version=self.compiler_version,
            compile_policy_version=self.compile_policy_version,
            sandbox_policy_version=self.sandbox_policy_version,
            normalized_flags=self.normalized_flags,
            material_role=self.material_role,
        )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "publication_binding": self.publication_binding,
            **self.identity_dict(),
            "latex_source_byte_size": self.latex_source_byte_size,
            "latex_source_reference": self.latex_source_reference,
            "page_count": self.page_count,
            "pdf_byte_size": self.pdf_byte_size,
            "pdf_reference": self.pdf_reference,
            "pdf_sha256": self.pdf_sha256,
            "pdf_text_projection_hash": self.pdf_text_projection_hash,
            "published_at": _rfc3339(self.published_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.canonical_dict(),
            "material_content_hash": self.material_content_hash,
        }


@dataclass(frozen=True, slots=True)
class PreparedCoverLetterMaterialWriteResult:
    status: PreparedCoverLetterMaterialWriteStatus
    material: PreparedCoverLetterMaterial | None
    reason_code: PreparedCoverLetterMaterialFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = PreparedCoverLetterMaterialWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PreparedCoverLetterMaterialFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            PreparedCoverLetterMaterialWriteStatus.CREATED,
            PreparedCoverLetterMaterialWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.material, PreparedCoverLetterMaterial)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful material write is invalid")
        elif self.material is not None or self.reason_code is None:
            raise ValueError("failed material write is invalid")


@dataclass(frozen=True, slots=True)
class PreparedCoverLetterMaterialReadResult:
    status: PreparedCoverLetterMaterialReadStatus
    material: PreparedCoverLetterMaterial | None
    reason_code: PreparedCoverLetterMaterialFailureReason | None = None

    def __post_init__(self) -> None:
        status = PreparedCoverLetterMaterialReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PreparedCoverLetterMaterialFailureReason(self.reason_code),
            )
        if status is PreparedCoverLetterMaterialReadStatus.FOUND:
            if (
                not isinstance(self.material, PreparedCoverLetterMaterial)
                or self.reason_code is not None
            ):
                raise ValueError("found material read is invalid")
        elif status is PreparedCoverLetterMaterialReadStatus.NOT_FOUND:
            if self.material is not None or self.reason_code is not None:
                raise ValueError("not-found material read is invalid")
        elif (
            self.material is not None
            or self.reason_code
            is not PreparedCoverLetterMaterialFailureReason
            .MATERIAL_INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure material read is invalid")


@runtime_checkable
class PreparedCoverLetterMaterialRepository(Protocol):
    def save(
        self, material: PreparedCoverLetterMaterial
    ) -> PreparedCoverLetterMaterialWriteResult:
        """Persist one immutable prepared cover-letter material."""

    def get(
        self, *, subject_id: str, material_id: str
    ) -> PreparedCoverLetterMaterialReadResult:
        """Read one subject-owned prepared cover-letter material."""


def _material_from_dict(value: Any) -> PreparedCoverLetterMaterial:
    expected = {
        "application_plan_id",
        "compile_policy_version",
        "compiler_engine",
        "compiler_version",
        "contract_version",
        "cover_letter_draft_id",
        "draft_content_hash",
        "evidence_snapshot_hash",
        "evidence_snapshot_id",
        "fact_qa_result_hash",
        "fact_qa_result_id",
        "job_content_hash",
        "job_id",
        "job_revision",
        "latex_source_byte_size",
        "latex_source_reference",
        "latex_source_sha256",
        "material_content_hash",
        "material_id",
        "material_role",
        "normalized_flags",
        "page_count",
        "pdf_byte_size",
        "pdf_reference",
        "pdf_sha256",
        "pdf_text_projection_hash",
        "plan_user_instructions_hash",
        "publication_binding",
        "publication_policy_version",
        "published_at",
        "sandbox_policy_version",
        "subject_id",
        "template_id",
        "template_sha256",
        "template_version",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["normalized_flags"], list)
    ):
        raise ValueError("persisted PreparedCoverLetterMaterial is invalid")
    return PreparedCoverLetterMaterial(
        material_id=value["material_id"],
        contract_version=value["contract_version"],
        publication_policy_version=value["publication_policy_version"],
        publication_binding=value["publication_binding"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        plan_user_instructions_hash=value["plan_user_instructions_hash"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        cover_letter_draft_id=value["cover_letter_draft_id"],
        draft_content_hash=value["draft_content_hash"],
        evidence_snapshot_id=value["evidence_snapshot_id"],
        evidence_snapshot_hash=value["evidence_snapshot_hash"],
        fact_qa_result_id=value["fact_qa_result_id"],
        fact_qa_result_hash=value["fact_qa_result_hash"],
        template_id=value["template_id"],
        template_version=value["template_version"],
        template_sha256=value["template_sha256"],
        latex_source_reference=value["latex_source_reference"],
        latex_source_sha256=value["latex_source_sha256"],
        latex_source_byte_size=value["latex_source_byte_size"],
        compiler_engine=value["compiler_engine"],
        compiler_version=value["compiler_version"],
        compile_policy_version=value["compile_policy_version"],
        sandbox_policy_version=value["sandbox_policy_version"],
        normalized_flags=tuple(value["normalized_flags"]),
        pdf_reference=value["pdf_reference"],
        pdf_sha256=value["pdf_sha256"],
        pdf_byte_size=value["pdf_byte_size"],
        page_count=value["page_count"],
        pdf_text_projection_hash=value["pdf_text_projection_hash"],
        material_role=PreparedCoverLetterMaterialRole(
            value["material_role"]
        ),
        material_content_hash=value["material_content_hash"],
        published_at=_parse_timestamp(value["published_at"]),
    )


class PrivateHomePreparedCoverLetterMaterialRepository:
    """Immutable records that revalidate both managed artifacts on every read."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _subject_directory(self, subject_id: str) -> Path:
        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        return (
            self._home.paths.prepared_cover_letter_materials
            / _subject_storage_key(cleaned)
        )

    def _path(self, subject_id: str, material_id: str) -> Path:
        if (
            not isinstance(material_id, str)
            or _MATERIAL_ID_PATTERN.fullmatch(material_id) is None
        ):
            raise ValueError("material_id is invalid")
        return self._subject_directory(subject_id) / f"{material_id}.json"

    def _artifacts_are_valid(
        self, material: PreparedCoverLetterMaterial
    ) -> bool:
        try:
            source_path = self._home.contained_path(
                material.latex_source_reference
            )
            pdf_path = self._home.contained_path(material.pdf_reference)
            if (
                source_path.is_symlink()
                or not source_path.is_file()
                or pdf_path.is_symlink()
                or not pdf_path.is_file()
            ):
                return False
            source_bytes = source_path.read_bytes()
            pdf_bytes = pdf_path.read_bytes()
            source_size = source_path.stat(follow_symlinks=False).st_size
            pdf_size = pdf_path.stat(follow_symlinks=False).st_size
        except (OSError, PrivateHomeError):
            return False
        inspected = inspect_cover_letter_pdf(pdf_bytes)
        if inspected is None:
            return False
        pages, projection = inspected
        try:
            source = source_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return (
            source_size == material.latex_source_byte_size
            and hashlib.sha256(source_bytes).hexdigest()
            == material.latex_source_sha256
            and not any(token in source for token in _TEMPLATE_TOKENS)
            and _FORBIDDEN_LATEX_CAPABILITY.search(source) is None
            and _DOCUMENT_CLASS_PATTERN.findall(source) == ["article"]
            and _PACKAGE_COMMAND_PATTERN.findall(source)
            == [r"\usepackage"]
            and _PACKAGE_DECLARATION_PATTERN.findall(source)
            == [("T1", "fontenc")]
            and pdf_size == material.pdf_byte_size
            and len(pdf_bytes) == material.pdf_byte_size
            and pdf_bytes.startswith(b"%PDF-")
            and hashlib.sha256(pdf_bytes).hexdigest() == material.pdf_sha256
            and pages == material.page_count == 1
            and hashlib.sha256(projection.encode("utf-8")).hexdigest()
            == material.pdf_text_projection_hash
            and _PLACEHOLDER_PATTERN.search(projection) is None
        )

    def get(
        self, *, subject_id: str, material_id: str
    ) -> PreparedCoverLetterMaterialReadResult:
        path = self._path(subject_id, material_id)
        with self._lock:
            if not path.exists() and not path.is_symlink():
                return PreparedCoverLetterMaterialReadResult(
                    status=PreparedCoverLetterMaterialReadStatus.NOT_FOUND,
                    material=None,
                )
            if path.is_symlink() or not path.is_file():
                return self._integrity_failure()
            try:
                material = _material_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return self._integrity_failure()
            if (
                material.subject_id != subject_id.strip()
                or material.material_id != material_id
                or path.name != f"{material.material_id}.json"
                or not self._artifacts_are_valid(material)
            ):
                return self._integrity_failure()
            return PreparedCoverLetterMaterialReadResult(
                status=PreparedCoverLetterMaterialReadStatus.FOUND,
                material=material,
            )

    @staticmethod
    def _integrity_failure() -> PreparedCoverLetterMaterialReadResult:
        return PreparedCoverLetterMaterialReadResult(
            status=PreparedCoverLetterMaterialReadStatus.INTEGRITY_FAILURE,
            material=None,
            reason_code=(
                PreparedCoverLetterMaterialFailureReason
                .MATERIAL_INTEGRITY_FAILURE
            ),
        )

    def save(
        self, material: PreparedCoverLetterMaterial
    ) -> PreparedCoverLetterMaterialWriteResult:
        if not isinstance(material, PreparedCoverLetterMaterial):
            raise TypeError("material must be a PreparedCoverLetterMaterial")
        path = self._path(material.subject_id, material.material_id)
        with self._lock:
            if not self._artifacts_are_valid(material):
                return PreparedCoverLetterMaterialWriteResult(
                    status=PreparedCoverLetterMaterialWriteStatus.FAILED,
                    material=None,
                    reason_code=(
                        PreparedCoverLetterMaterialFailureReason
                        .MATERIAL_INTEGRITY_FAILURE
                    ),
                    retryable=False,
                )
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            material.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return PreparedCoverLetterMaterialWriteResult(
                    status=PreparedCoverLetterMaterialWriteStatus.FAILED,
                    material=None,
                    reason_code=(
                        PreparedCoverLetterMaterialFailureReason
                        .MATERIAL_PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return PreparedCoverLetterMaterialWriteResult(
                    status=PreparedCoverLetterMaterialWriteStatus.CREATED,
                    material=material,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=material.subject_id,
                material_id=material.material_id,
            )
            if (
                existing.status
                is PreparedCoverLetterMaterialReadStatus.FOUND
                and existing.material is not None
                and existing.material.to_dict() == material.to_dict()
            ):
                return PreparedCoverLetterMaterialWriteResult(
                    status=PreparedCoverLetterMaterialWriteStatus.UNCHANGED,
                    material=existing.material,
                    reason_code=None,
                    retryable=False,
                )
            return PreparedCoverLetterMaterialWriteResult(
                status=PreparedCoverLetterMaterialWriteStatus.FAILED,
                material=None,
                reason_code=(
                    PreparedCoverLetterMaterialFailureReason
                    .MATERIAL_INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class PublishPreparedCoverLetterCommand:
    subject_id: str
    application_plan_id: str
    cover_letter_fact_qa_result_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class PublishPreparedCoverLetterResult:
    status: PreparedCoverLetterMaterialStatus
    subject_id: str
    application_plan_id: str
    material: PreparedCoverLetterMaterial | None
    write_result: PreparedCoverLetterMaterialWriteResult | None
    reason_code: PreparedCoverLetterMaterialFailureReason | None
    not_ready_reason: PreparedCoverLetterMaterialNotReadyReason | None
    compiler_started: bool
    retryable: bool
    message: str
    stopped_source_lineage: PublicationStoppedSourceLineage | None = None

    def __post_init__(self) -> None:
        status = PreparedCoverLetterMaterialStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PreparedCoverLetterMaterialFailureReason(self.reason_code),
            )
        if self.not_ready_reason is not None:
            object.__setattr__(
                self,
                "not_ready_reason",
                PreparedCoverLetterMaterialNotReadyReason(
                    self.not_ready_reason
                ),
            )
        if type(self.compiler_started) is not bool:
            raise TypeError("compiler_started must be a boolean")
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if (
            self.stopped_source_lineage is not None
            and (
                not isinstance(
                    self.stopped_source_lineage,
                    PublicationStoppedSourceLineage,
                )
                or self.stopped_source_lineage.subject_id != self.subject_id
                or self.stopped_source_lineage.application_plan_id
                != self.application_plan_id
                or self.stopped_source_lineage.publication_stage
                is not ApplicationPreparationStage.COVER_LETTER_PUBLICATION
                or self.stopped_source_lineage.material_kind
                is not PublicationMaterialKind.COVER_LETTER
            )
        ):
            raise ValueError("stopped source lineage does not match publication")
        if status in {
            PreparedCoverLetterMaterialStatus.CREATED,
            PreparedCoverLetterMaterialStatus.UNCHANGED,
        }:
            expected = PreparedCoverLetterMaterialWriteStatus(status.value)
            if (
                not isinstance(self.material, PreparedCoverLetterMaterial)
                or not isinstance(
                    self.write_result,
                    PreparedCoverLetterMaterialWriteResult,
                )
                or self.write_result.status is not expected
                or self.write_result.material != self.material
                or self.reason_code is not None
                or self.not_ready_reason is not None
                or self.retryable
                or self.stopped_source_lineage is not None
            ):
                raise ValueError("successful publication result is invalid")
        elif status is PreparedCoverLetterMaterialStatus.NOT_READY:
            if (
                self.material is not None
                or self.write_result is not None
                or self.reason_code is not None
                or self.not_ready_reason is None
                or self.compiler_started
                or self.retryable
            ):
                raise ValueError("not-ready publication result is invalid")
        elif (
            self.material is not None
            or self.write_result is not None
            or self.reason_code is None
            or self.not_ready_reason is not None
        ):
            raise ValueError("deferred or failed publication result is invalid")


def _failure(
    command: PublishPreparedCoverLetterCommand,
    reason: PreparedCoverLetterMaterialFailureReason,
    *,
    status: PreparedCoverLetterMaterialStatus = (
        PreparedCoverLetterMaterialStatus.FAILED
    ),
    compiler_started: bool = False,
    retryable: bool = False,
    detail: str | None = None,
    stopped_source_lineage: PublicationStoppedSourceLineage | None = None,
) -> PublishPreparedCoverLetterResult:
    return PublishPreparedCoverLetterResult(
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
        material=None,
        write_result=None,
        reason_code=reason,
        not_ready_reason=None,
        compiler_started=compiler_started,
        retryable=retryable,
        message=detail or f"Cover-letter publication failed: {reason.value}.",
        stopped_source_lineage=stopped_source_lineage,
    )


def _not_ready(
    command: PublishPreparedCoverLetterCommand,
    reason: PreparedCoverLetterMaterialNotReadyReason,
    *,
    detail: str,
    stopped_source_lineage: PublicationStoppedSourceLineage | None = None,
) -> PublishPreparedCoverLetterResult:
    return PublishPreparedCoverLetterResult(
        status=PreparedCoverLetterMaterialStatus.NOT_READY,
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        material=None,
        write_result=None,
        reason_code=None,
        not_ready_reason=reason,
        compiler_started=False,
        retryable=False,
        message=f"The cover letter is not ready: {detail}",
        stopped_source_lineage=stopped_source_lineage,
    )


def _identity_values(
    *,
    plan: ApplicationPlan,
    draft: CoverLetterDraft,
    fact_qa: CoverLetterFactQAResult,
    template: ManagedCoverLetterTemplate,
    source_hash: str,
    description: LatexCompilerDescription,
) -> dict[str, Any]:
    return {
        "contract_version": (
            PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION
        ),
        "publication_policy_version": (
            COVER_LETTER_PUBLICATION_POLICY_VERSION
        ),
        "subject_id": plan.subject_id,
        "application_plan_id": plan.plan_id,
        "plan_user_instructions_hash": (
            plan.user_preparation_instructions_hash
        ),
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "cover_letter_draft_id": draft.draft_id,
        "draft_content_hash": draft.draft_content_hash,
        "evidence_snapshot_id": draft.evidence_snapshot_id,
        "evidence_snapshot_hash": draft.evidence_snapshot_hash,
        "fact_qa_result_id": fact_qa.result_id,
        "fact_qa_result_hash": fact_qa.result_content_hash,
        "template_id": template.template_id,
        "template_version": template.template_version,
        "template_sha256": template.template_sha256,
        "latex_source_sha256": source_hash,
        "compiler_engine": description.engine,
        "compiler_version": description.compiler_version,
        "compile_policy_version": description.compile_policy_version,
        "sandbox_policy_version": description.sandbox_policy_version,
        "normalized_flags": description.normalized_flags,
        "material_role": PreparedCoverLetterMaterialRole.COVER_LETTER,
    }


def _existing_material_result(
    command: PublishPreparedCoverLetterCommand,
    material: PreparedCoverLetterMaterial,
) -> PublishPreparedCoverLetterResult:
    write = PreparedCoverLetterMaterialWriteResult(
        status=PreparedCoverLetterMaterialWriteStatus.UNCHANGED,
        material=material,
        reason_code=None,
        retryable=False,
    )
    return PublishPreparedCoverLetterResult(
        status=PreparedCoverLetterMaterialStatus.UNCHANGED,
        subject_id=command.subject_id,
        application_plan_id=command.application_plan_id,
        material=material,
        write_result=write,
        reason_code=None,
        not_ready_reason=None,
        compiler_started=False,
        retryable=False,
        message="The existing prepared cover letter is unchanged.",
    )


def _persist_bytes_if_absent(
    *,
    home: PrivateHome,
    reference: str,
    content: bytes,
) -> bool:
    target = home.contained_path(reference)
    created = home.write_bytes_if_absent(target, content)
    if created:
        return True
    return (
        not target.is_symlink()
        and target.is_file()
        and target.read_bytes() == content
    )


def publish_prepared_cover_letter(
    command: PublishPreparedCoverLetterCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    job_repository: JobPostingReadRepository,
    draft_repository: CoverLetterDraftRepository,
    fact_qa_repository: CoverLetterFactQARepository,
    template_provider: ManagedCoverLetterTemplateProvider,
    compiler: LatexCompilerPort,
    material_repository: PreparedCoverLetterMaterialRepository,
    home: PrivateHome | None = None,
    correction_provider: (
        CoverLetterOverflowCorrectionDirectiveProvider | None
    ) = None,
) -> PublishPreparedCoverLetterResult:
    """Render, compile, validate and publish exactly one cover-letter binding."""

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
        qa_result_id = _clean_text(
            "cover_letter_fact_qa_result_id",
            command.cover_letter_fact_qa_result_id,
            maximum=160,
        )
        now = _require_aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.INVALID_REQUEST,
        )

    try:
        plan_read = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .APPLICATION_PLAN_NOT_FOUND,
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_read.plan, ApplicationPlan)
    ):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
        )
    plan = plan_read.plan
    if plan.subject_id != subject_id:
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
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
            command,
            PreparedCoverLetterMaterialFailureReason.JOB_READ_FAILED,
        )
    if job is None:
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.JOB_NOT_FOUND,
        )
    if not isinstance(job, JobPosting):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.JOB_READ_FAILED,
        )
    if (
        job.job_id != plan.job_id
        or job.revision != plan.job_revision
        or job.content_hash != plan.job_content_hash
    ):
        return _not_ready(
            command,
            PreparedCoverLetterMaterialNotReadyReason.JOB_BINDING_MISMATCH,
            detail="the current JobPosting no longer matches the plan.",
        )

    try:
        qa_read = fact_qa_repository.get(
            subject_id=subject_id, result_id=qa_result_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .FACT_QA_INTEGRITY_FAILURE,
        )
    if qa_read.status is CoverLetterFactQAReadStatus.NOT_FOUND:
        missing_hash = _canonical_hash(
            {
                "application_plan_id": plan.plan_id,
                "expected_fact_qa_result_id": qa_result_id,
                "subject_id": subject_id,
            }
        )
        lineage = create_publication_stopped_source_lineage(
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            publication_stage=(
                ApplicationPreparationStage.COVER_LETTER_PUBLICATION
            ),
            material_kind=PublicationMaterialKind.COVER_LETTER,
            source_kind=PublicationStoppedSourceKind.FACT_QA_BLOCKER,
            source_stage=ApplicationPreparationStage.COVER_LETTER_FACT_QA,
            source_result_id=qa_result_id,
            source_outcome=PreparationStageOutcome.DEFERRED,
            source_contract_version=COVER_LETTER_FACT_QA_CONTRACT_VERSION,
            source_result_content_hash=missing_hash,
            source_directive=(
                PublicationBlockingDirective.FACT_QA_RESULT_MISSING
            ),
        )
        return _not_ready(
            command,
            PreparedCoverLetterMaterialNotReadyReason.FACT_QA_NOT_PASSED,
            detail="no completed Fact QA result is available.",
            stopped_source_lineage=lineage,
        )
    if (
        qa_read.status is not CoverLetterFactQAReadStatus.FOUND
        or not isinstance(qa_read.result, CoverLetterFactQAResult)
    ):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .FACT_QA_INTEGRITY_FAILURE,
        )
    fact_qa = qa_read.result
    if (
        fact_qa.result_id != qa_result_id
        or fact_qa.subject_id != subject_id
        or fact_qa.application_plan_id != plan.plan_id
        or fact_qa.job_id != job.job_id
        or fact_qa.job_revision != job.revision
        or fact_qa.job_content_hash != job.content_hash
    ):
        return _not_ready(
            command,
            PreparedCoverLetterMaterialNotReadyReason
            .FACT_QA_BINDING_MISMATCH,
            detail="Fact QA does not belong to the current plan and job.",
        )
    if fact_qa.verdict is not CoverLetterFactQAVerdict.PASSED:
        lineage = create_publication_stopped_source_lineage(
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            publication_stage=(
                ApplicationPreparationStage.COVER_LETTER_PUBLICATION
            ),
            material_kind=PublicationMaterialKind.COVER_LETTER,
            source_kind=PublicationStoppedSourceKind.FACT_QA_BLOCKER,
            source_stage=ApplicationPreparationStage.COVER_LETTER_FACT_QA,
            source_result_id=fact_qa.result_id,
            source_outcome=PreparationStageOutcome.COMPLETED,
            source_contract_version=COVER_LETTER_FACT_QA_CONTRACT_VERSION,
            source_result_content_hash=fact_qa.result_content_hash,
            source_directive=PublicationBlockingDirective.FACT_QA_BLOCKED,
            source_artifact_id=fact_qa.cover_letter_draft_id,
            source_artifact_content_hash=fact_qa.draft_content_hash,
            blocking_lineage_ids=tuple(
                finding.finding_id
                for finding in fact_qa.findings
                if finding.severity
                is CoverLetterFactQAFindingSeverity.BLOCKING
            ),
        )
        return _not_ready(
            command,
            PreparedCoverLetterMaterialNotReadyReason.FACT_QA_NOT_PASSED,
            detail=(
                f"Fact QA returned {fact_qa.verdict.value} rather than PASSED."
            ),
            stopped_source_lineage=lineage,
        )

    try:
        draft_read = draft_repository.get(
            subject_id=subject_id,
            draft_id=fact_qa.cover_letter_draft_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    if draft_read.status is CoverLetterDraftReadStatus.NOT_FOUND:
        return _not_ready(
            command,
            PreparedCoverLetterMaterialNotReadyReason.DRAFT_BINDING_MISMATCH,
            detail="the Fact-QA-bound Draft is not available.",
        )
    if (
        draft_read.status is not CoverLetterDraftReadStatus.FOUND
        or not isinstance(draft_read.draft, CoverLetterDraft)
    ):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.DRAFT_INTEGRITY_FAILURE,
        )
    draft = draft_read.draft
    if (
        draft.subject_id != subject_id
        or draft.application_plan_id != plan.plan_id
        or draft.job_id != job.job_id
        or draft.job_revision != job.revision
        or draft.job_content_hash != job.content_hash
        or draft.user_preparation_instructions_hash
        != plan.user_preparation_instructions_hash
        or fact_qa.cover_letter_draft_id != draft.draft_id
        or fact_qa.draft_content_hash != draft.draft_content_hash
        or fact_qa.evidence_snapshot_id != draft.evidence_snapshot_id
        or fact_qa.evidence_snapshot_hash != draft.evidence_snapshot_hash
    ):
        return _not_ready(
            command,
            PreparedCoverLetterMaterialNotReadyReason
            .DRAFT_BINDING_MISMATCH,
            detail="Draft, evidence, Fact QA and plan bindings differ.",
        )

    correction_constraint = None
    if correction_provider is not None:
        try:
            correction_read = correction_provider.get_current(
                subject_id=subject_id,
                application_plan_id=plan.plan_id,
            )
            if not isinstance(
                correction_read,
                CoverLetterOverflowCorrectionConstraintReadResult,
            ):
                raise TypeError("correction provider returned an invalid value")
            if (
                correction_read.status
                is CoverLetterOverflowCorrectionConstraintStatus
                .INTEGRITY_FAILURE
            ):
                raise ValueError("correction directive integrity failed")
            if (
                correction_read.status
                is CoverLetterOverflowCorrectionConstraintStatus.FOUND
            ):
                if (
                    not isinstance(
                        correction_read.constraint,
                        CoverLetterOverflowCorrectionConstraint,
                    )
                    or correction_read.constraint.subject_id != subject_id
                    or correction_read.constraint.application_plan_id
                    != plan.plan_id
                ):
                    raise ValueError("correction directive binding failed")
                correction_constraint = correction_read.constraint
            elif (
                correction_read.status
                is not CoverLetterOverflowCorrectionConstraintStatus.NOT_FOUND
                or correction_read.constraint is not None
            ):
                raise ValueError("correction directive result is invalid")
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                command,
                PreparedCoverLetterMaterialFailureReason.TEMPLATE_INVALID,
                detail="The format-only correction directive is invalid.",
            )

    try:
        template = template_provider.get()
        if not isinstance(template, ManagedCoverLetterTemplate):
            raise TypeError("template provider returned an invalid value")
        base_source = render_cover_letter_latex(draft, template)
        source = base_source
        if correction_constraint is not None:
            if (
                correction_constraint.source_version
                != template.template_version
            ):
                raise ValueError("correction source version drifted")
            source_reference = cover_letter_source_reference(
                subject_id=subject_id,
                source_sha256=correction_constraint.source_content_hash,
            )
            source_path = active_home.contained_path(source_reference)
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError("correction source is unavailable")
            source_bytes = source_path.read_bytes()
            if (
                hashlib.sha256(source_bytes).hexdigest()
                != correction_constraint.source_content_hash
            ):
                raise ValueError("correction source integrity drifted")
            source = source_bytes.decode("utf-8")
            validate_rendered_cover_letter_latex(source, draft)
            begin = r"\begin{document}"
            if (
                source.count(begin) != 1
                or base_source.count(begin) != 1
                or source.split(begin, 1)[1]
                != base_source.split(begin, 1)[1]
            ):
                raise ValueError(
                    "correction source changed approved Cover Letter text"
                )
            source = reformat_cover_letter_latex(
                source, draft, correction_constraint
            )
        source_bytes = source.encode("utf-8")
        source_hash = hashlib.sha256(source_bytes).hexdigest()
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.TEMPLATE_INVALID,
        )

    try:
        description = compiler.describe()
        if not isinstance(description, LatexCompilerDescription):
            raise TypeError("compiler returned an invalid description")
    except LatexCompilerUnavailableError:
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.COMPILER_UNAVAILABLE,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILER_UNAVAILABLE
            ),
            retryable=True,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.COMPILER_UNAVAILABLE,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILER_UNAVAILABLE
            ),
            retryable=True,
        )

    identity = _identity_values(
        plan=plan,
        draft=draft,
        fact_qa=fact_qa,
        template=template,
        source_hash=source_hash,
        description=description,
    )
    material_id = prepared_cover_letter_material_id(**identity)
    publication_binding = cover_letter_publication_binding(**identity)
    try:
        existing = material_repository.get(
            subject_id=subject_id, material_id=material_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .MATERIAL_INTEGRITY_FAILURE,
        )
    if (
        existing.status
        is PreparedCoverLetterMaterialReadStatus.INTEGRITY_FAILURE
    ):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .MATERIAL_INTEGRITY_FAILURE,
        )
    if existing.status is PreparedCoverLetterMaterialReadStatus.FOUND:
        if (
            not isinstance(existing.material, PreparedCoverLetterMaterial)
            or existing.material.material_id != material_id
            or existing.material.publication_binding != publication_binding
            or existing.material.identity_dict()
            != _publication_identity(**identity)
        ):
            return _failure(
                command,
                PreparedCoverLetterMaterialFailureReason
                .MATERIAL_INTEGRITY_FAILURE,
            )
        return _existing_material_result(command, existing.material)

    source_reference = cover_letter_source_reference(
        subject_id=subject_id, source_sha256=source_hash
    )
    try:
        active_home.ensure()
        if not _persist_bytes_if_absent(
            home=active_home,
            reference=source_reference,
            content=source_bytes,
        ):
            raise ValueError("managed source conflicts with existing bytes")
        source_path = active_home.contained_path(source_reference)
        if (
            source_path.is_symlink()
            or not source_path.is_file()
            or source_path.read_bytes() != source_bytes
        ):
            raise ValueError("managed source validation failed")
    except (OSError, PrivateHomeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .SOURCE_PERSISTENCE_FAILED,
            retryable=True,
        )

    try:
        outcome = compiler.compile(
            LatexCompileRequest(latex_source=source)
        )
    except LatexCompilerUnavailableError:
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.COMPILER_UNAVAILABLE,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILER_UNAVAILABLE
            ),
            retryable=True,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.COMPILER_UNAVAILABLE,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILER_UNAVAILABLE
            ),
            retryable=True,
        )
    if not isinstance(outcome, LatexCompileOutcome):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.COMPILATION_ERROR,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILATION_ERROR
            ),
            detail="The LaTeX compiler returned an invalid outcome.",
        )
    if outcome.status is LatexCompileStatus.UNAVAILABLE:
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.COMPILER_UNAVAILABLE,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILER_UNAVAILABLE
            ),
            compiler_started=outcome.compiler_started,
            retryable=True,
        )
    if outcome.status is not LatexCompileStatus.SUCCEEDED:
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.COMPILATION_ERROR,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILATION_ERROR
            ),
            compiler_started=outcome.compiler_started,
            detail="The managed cover-letter source did not compile.",
        )
    if not isinstance(outcome.pdf_bytes, bytes):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.PDF_INVALID,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILATION_ERROR
            ),
            compiler_started=True,
        )

    pdf_bytes = outcome.pdf_bytes
    inspected = inspect_cover_letter_pdf(pdf_bytes)
    if (
        not pdf_bytes.startswith(b"%PDF-")
        or not 0 < len(pdf_bytes) <= MAX_PDF_BYTES
        or inspected is None
        or inspected[0] < 1
    ):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.PDF_INVALID,
            status=(
                PreparedCoverLetterMaterialStatus
                .DEFERRED_COMPILATION_ERROR
            ),
            compiler_started=True,
        )
    page_count, projection = inspected
    if page_count > 1:
        overflow_content = {
            "application_plan_id": plan.plan_id,
            "compiler_engine": description.engine,
            "compiler_version": description.compiler_version,
            "page_count": page_count,
            "policy_version": COVER_LETTER_PUBLICATION_POLICY_VERSION,
            "source_sha256": source_hash,
            "subject_id": subject_id,
            "template_id": template.template_id,
            "template_version": template.template_version,
        }
        overflow_hash = _canonical_hash(overflow_content)
        lineage = create_publication_stopped_source_lineage(
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            publication_stage=(
                ApplicationPreparationStage.COVER_LETTER_PUBLICATION
            ),
            material_kind=PublicationMaterialKind.COVER_LETTER,
            source_kind=(
                PublicationStoppedSourceKind
                .COVER_LETTER_LAYOUT_OVERFLOW
            ),
            source_stage=(
                ApplicationPreparationStage.COVER_LETTER_PUBLICATION
            ),
            source_result_id=(
                f"cover-letter-overflow-evaluation-{overflow_hash}"
            ),
            source_outcome=PreparationStageOutcome.DEFERRED,
            source_contract_version=(
                PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION
            ),
            source_result_content_hash=overflow_hash,
            source_directive=(
                PublicationBlockingDirective
                .COVER_LETTER_LAYOUT_OVERFLOW
            ),
            source_artifact_id=f"cover-letter-latex-source-{source_hash}",
            source_artifact_version=template.template_version,
            source_artifact_content_hash=source_hash,
        )
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.LAYOUT_OVERFLOW,
            status=(
                PreparedCoverLetterMaterialStatus.DEFERRED_LAYOUT_OVERFLOW
            ),
            compiler_started=True,
            detail=(
                "The cover letter exceeds the fixed one-page publication "
                "policy; its text and layout were not modified."
            ),
            stopped_source_lineage=lineage,
        )
    expected_projection = expected_cover_letter_text_projection(draft)
    if (
        not projection
        or projection != expected_projection
        or _PLACEHOLDER_PATTERN.search(projection) is not None
    ):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason.PDF_TEXT_MISMATCH,
            compiler_started=True,
        )

    pdf_hash = hashlib.sha256(pdf_bytes).hexdigest()
    pdf_reference = cover_letter_pdf_reference(
        subject_id=subject_id, pdf_sha256=pdf_hash
    )
    projection_hash = hashlib.sha256(projection.encode("utf-8")).hexdigest()
    try:
        if not _persist_bytes_if_absent(
            home=active_home,
            reference=pdf_reference,
            content=pdf_bytes,
        ):
            raise ValueError("managed PDF conflicts with existing bytes")
        pdf_path = active_home.contained_path(pdf_reference)
        stored_bytes = pdf_path.read_bytes()
        stored_inspection = inspect_cover_letter_pdf(stored_bytes)
        if (
            pdf_path.is_symlink()
            or not pdf_path.is_file()
            or stored_bytes != pdf_bytes
            or hashlib.sha256(stored_bytes).hexdigest() != pdf_hash
            or stored_inspection != (1, projection)
        ):
            raise ValueError("managed PDF validation failed")
    except (OSError, PrivateHomeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .ARTIFACT_PERSISTENCE_FAILED,
            compiler_started=True,
            retryable=True,
        )

    canonical = {
        "material_id": material_id,
        "publication_binding": publication_binding,
        **_publication_identity(**identity),
        "latex_source_byte_size": len(source_bytes),
        "latex_source_reference": source_reference,
        "page_count": page_count,
        "pdf_byte_size": len(pdf_bytes),
        "pdf_reference": pdf_reference,
        "pdf_sha256": pdf_hash,
        "pdf_text_projection_hash": projection_hash,
        "published_at": _rfc3339(now),
    }
    try:
        material = PreparedCoverLetterMaterial(
            material_id=material_id,
            publication_binding=publication_binding,
            latex_source_reference=source_reference,
            latex_source_byte_size=len(source_bytes),
            pdf_reference=pdf_reference,
            pdf_sha256=pdf_hash,
            pdf_byte_size=len(pdf_bytes),
            page_count=page_count,
            pdf_text_projection_hash=projection_hash,
            material_content_hash=_canonical_hash(canonical),
            published_at=now,
            **identity,
        )
    except (TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .MATERIAL_INTEGRITY_FAILURE,
            compiler_started=True,
        )

    try:
        write_result = material_repository.save(material)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            command,
            PreparedCoverLetterMaterialFailureReason
            .MATERIAL_PERSISTENCE_FAILED,
            compiler_started=True,
            retryable=True,
        )
    if write_result.status is PreparedCoverLetterMaterialWriteStatus.FAILED:
        return _failure(
            command,
            write_result.reason_code
            or PreparedCoverLetterMaterialFailureReason
            .MATERIAL_PERSISTENCE_FAILED,
            compiler_started=True,
            retryable=write_result.retryable,
        )
    status = PreparedCoverLetterMaterialStatus(write_result.status.value)
    return PublishPreparedCoverLetterResult(
        status=status,
        subject_id=subject_id,
        application_plan_id=plan_id,
        material=write_result.material,
        write_result=write_result,
        reason_code=None,
        not_ready_reason=None,
        compiler_started=True,
        retryable=False,
        message=(
            "The fact-checked cover letter was published as a managed PDF."
            if status is PreparedCoverLetterMaterialStatus.CREATED
            else "The existing prepared cover letter is unchanged."
        ),
    )


_COVER_LETTER_PUBLICATION_NOT_READY_REASON_MAP = {
    reason: CoverLetterPublicationStopReason[reason.name]
    for reason in PreparedCoverLetterMaterialNotReadyReason
}
_COVER_LETTER_PUBLICATION_FAILURE_REASON_MAP = {
    reason: CoverLetterPublicationStopReason[reason.name]
    for reason in PreparedCoverLetterMaterialFailureReason
}
_COVER_LETTER_DEFERRED_PUBLICATION_STATUSES = {
    PreparedCoverLetterMaterialStatus.DEFERRED_COMPILER_UNAVAILABLE,
    PreparedCoverLetterMaterialStatus.DEFERRED_COMPILATION_ERROR,
    PreparedCoverLetterMaterialStatus.DEFERRED_LAYOUT_OVERFLOW,
}


def prepared_cover_letter_publication_public_result(
    result: PublishPreparedCoverLetterResult,
) -> PublicPreparationStageResult:
    """Adapt every authoritative P2b2d outcome to stage-result v2."""

    if not isinstance(result, PublishPreparedCoverLetterResult):
        raise TypeError(
            "result must be a prepared-cover-letter publication result"
        )
    stage = ApplicationPreparationStage.COVER_LETTER_PUBLICATION
    if result.status in {
        PreparedCoverLetterMaterialStatus.CREATED,
        PreparedCoverLetterMaterialStatus.UNCHANGED,
    }:
        if result.material is None:
            raise ValueError("successful publication has no material")
        constructor = (
            PublicPreparationStageResult.completed
            if result.status is PreparedCoverLetterMaterialStatus.CREATED
            else PublicPreparationStageResult.unchanged
        )
        return constructor(
            stage=stage,
            result_id=result.material.material_id,
            result_content_hash=result.material.material_content_hash,
            outputs={
                "prepared_cover_letter_material_id": (
                    result.material.material_id
                )
            },
        )
    if result.status is PreparedCoverLetterMaterialStatus.NOT_READY:
        if result.not_ready_reason is None:
            raise ValueError("not-ready publication has no reason")
        try:
            reason = _COVER_LETTER_PUBLICATION_NOT_READY_REASON_MAP[
                result.not_ready_reason
            ]
        except KeyError as error:
            raise ValueError(
                "unmapped cover-letter not-ready reason"
            ) from error
        outcome = PreparationStageOutcome.DEFERRED
    else:
        if result.reason_code is None:
            raise ValueError("stopped publication has no reason")
        try:
            reason = _COVER_LETTER_PUBLICATION_FAILURE_REASON_MAP[
                result.reason_code
            ]
        except KeyError as error:
            raise ValueError(
                "unmapped cover-letter publication reason"
            ) from error
        outcome = (
            PreparationStageOutcome.DEFERRED
            if result.status in _COVER_LETTER_DEFERRED_PUBLICATION_STATUSES
            else PreparationStageOutcome.FAILED
        )
    stop_reason = PreparationStopReasonEnvelope(
        stage=stage,
        code=reason,
        contract_version=(
            COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION
        ),
        outcome=outcome,
    )
    constructor = (
        PublicPreparationStageResult.deferred
        if outcome is PreparationStageOutcome.DEFERRED
        else PublicPreparationStageResult.failed
    )
    lineage = result.stopped_source_lineage
    targeted_stop = (
        result.not_ready_reason
        is PreparedCoverLetterMaterialNotReadyReason.FACT_QA_NOT_PASSED
        or result.reason_code
        is PreparedCoverLetterMaterialFailureReason.LAYOUT_OVERFLOW
    )
    if targeted_stop and lineage is None:
        raise ValueError(
            "targeted cover-letter publication stop lacks source lineage"
        )
    return constructor(
        stage=stage,
        stop_reason=stop_reason,
        retryable=result.retryable,
        result_id=(
            lineage.publication_result_id if lineage is not None else None
        ),
        result_content_hash=(
            lineage.lineage_content_hash if lineage is not None else None
        ),
        outputs=(
            lineage.output_references() if lineage is not None else {}
        ),
    )


__all__ = [
    "COVER_LETTER_PUBLICATION_POLICY_VERSION",
    "CoverLetterOverflowCorrectionConstraint",
    "CoverLetterOverflowCorrectionConstraintReadResult",
    "CoverLetterOverflowCorrectionConstraintStatus",
    "CoverLetterOverflowCorrectionDirectiveProvider",
    "DefaultManagedCoverLetterTemplateProvider",
    "MANAGED_COVER_LETTER_TEMPLATE_ID",
    "MANAGED_COVER_LETTER_TEMPLATE_SOURCE",
    "MANAGED_COVER_LETTER_TEMPLATE_VERSION",
    "ManagedCoverLetterTemplate",
    "ManagedCoverLetterTemplateProvider",
    "PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION",
    "PreparedCoverLetterMaterial",
    "PreparedCoverLetterMaterialFailureReason",
    "PreparedCoverLetterMaterialNotReadyReason",
    "PreparedCoverLetterMaterialReadResult",
    "PreparedCoverLetterMaterialReadStatus",
    "PreparedCoverLetterMaterialRepository",
    "PreparedCoverLetterMaterialRole",
    "PreparedCoverLetterMaterialStatus",
    "PreparedCoverLetterMaterialWriteResult",
    "PreparedCoverLetterMaterialWriteStatus",
    "PrivateHomePreparedCoverLetterMaterialRepository",
    "PublishPreparedCoverLetterCommand",
    "PublishPreparedCoverLetterResult",
    "cover_letter_pdf_reference",
    "cover_letter_pdf_text_is_faithful",
    "cover_letter_publication_binding",
    "cover_letter_source_reference",
    "escape_cover_letter_latex_text",
    "expected_cover_letter_text_projection",
    "inspect_cover_letter_pdf",
    "normalize_cover_letter_text_projection",
    "prepared_cover_letter_material_id",
    "prepared_cover_letter_publication_public_result",
    "publish_prepared_cover_letter",
    "reformat_cover_letter_latex",
    "render_cover_letter_latex",
    "validate_managed_cover_letter_template",
    "validate_rendered_cover_letter_latex",
]
