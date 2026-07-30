"""Subject-scoped registry of trusted, lineage-tracked LaTeX resume versions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .private_home import PrivateHome, PrivateHomeError
from .resume_latex_dependencies import (
    RESUME_LATEX_DEPENDENCY_POLICY_VERSION,
    single_file_external_dependencies,
    unmanaged_latex_packages,
)
from .resume_latex_markers import (
    BULLET_MACRO,
    JOBOPS_CONTENT_BEGIN,
    JOBOPS_CONTENT_END,
    SECTION_MACRO,
    split_controlled_region,
    uses_controlled_markers,
)


RESUME_LATEX_VERSION_CONTRACT_VERSION = "resume-latex-version-v1"
BASE_LATEX_TEMPLATE_CONTRACT_VERSION = "base-latex-template-v1"
RESUME_LATEX_SOURCE_SAFETY_POLICY_VERSION = (
    "resume-latex-source-safety-v1"
)
MAX_LATEX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_LATEX_LABELS = 32
MAX_LATEX_LABEL_CHARS = 80

_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_VERSION_ID_PATTERN = re.compile(r"^resume-latex-version-[a-f0-9]{64}$")
_FAMILY_ID_PATTERN = re.compile(r"^resume-latex-family-[a-f0-9]{64}$")
_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ResumeLatexSourceKind(str, Enum):
    USER_PROVIDED = "USER_PROVIDED"
    IMPORTED_EXISTING = "IMPORTED_EXISTING"
    SYSTEM_TEMPLATE_DERIVED = "SYSTEM_TEMPLATE_DERIVED"
    AI_GENERATED = "AI_GENERATED"
    AI_REVISED = "AI_REVISED"


class LatexSourceProfile(str, Enum):
    GENERAL_SOURCE_V1 = "GENERAL_SOURCE_V1"
    SINGLE_FILE_BASE_TEMPLATE_V1 = "SINGLE_FILE_BASE_TEMPLATE_V1"


class ResumeLatexVersionWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeLatexVersionReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeLatexVersionListStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RegisterResumeLatexVersionStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeLatexVersionFailureReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    SOURCE_MISSING = "SOURCE_MISSING"
    SOURCE_AMBIGUOUS = "SOURCE_AMBIGUOUS"
    SOURCE_UNMANAGED = "SOURCE_UNMANAGED"
    SOURCE_INVALID = "SOURCE_INVALID"
    SOURCE_NOT_UTF8 = "SOURCE_NOT_UTF8"
    SOURCE_CAPABILITY_REJECTED = "SOURCE_CAPABILITY_REJECTED"
    SOURCE_PROFILE_INVALID = "SOURCE_PROFILE_INVALID"
    TEMPLATE_CONTRACT_REJECTED = "TEMPLATE_CONTRACT_REJECTED"
    DEPENDENCY_POLICY_REJECTED = "DEPENDENCY_POLICY_REJECTED"
    PARENT_NOT_FOUND = "PARENT_NOT_FOUND"
    PARENT_INTEGRITY_FAILURE = "PARENT_INTEGRITY_FAILURE"
    ROOT_FAMILY_CONFLICT = "ROOT_FAMILY_CONFLICT"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ResumeLatexCapability(str, Enum):
    SHELL_ESCAPE = "SHELL_ESCAPE"
    EXTERNAL_PROGRAM = "EXTERNAL_PROGRAM"
    FILE_WRITE = "FILE_WRITE"
    FILE_READ = "FILE_READ"
    ABSOLUTE_PATH = "ABSOLUTE_PATH"
    DYNAMIC_CODE_LOADING = "DYNAMIC_CODE_LOADING"


_FORBIDDEN_CAPABILITIES: tuple[tuple[ResumeLatexCapability, re.Pattern[str]], ...] = (
    (
        ResumeLatexCapability.SHELL_ESCAPE,
        re.compile(r"\\write18\b|\\ShellEscape\b|\\immediate\s*\\write18\b"),
    ),
    (
        ResumeLatexCapability.EXTERNAL_PROGRAM,
        re.compile(
            r"\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bshellesc\b[^}]*\}"
            r"|\\directlua\b|\\luaexec\b|\\pdfshellescape\b"
            r"|\\usepackage\s*(?:\[[^\]]*\])?\s*\{[^}]*\bpython\b[^}]*\}"
        ),
    ),
    (
        ResumeLatexCapability.FILE_WRITE,
        re.compile(r"\\openout\b|\\newwrite\b|\\closeout\b"),
    ),
    (
        ResumeLatexCapability.FILE_READ,
        re.compile(r"\\openin\b|\\newread\b|\\closein\b|\\readline\b"),
    ),
    (
        ResumeLatexCapability.ABSOLUTE_PATH,
        re.compile(
            r"\\(?:input|include|includegraphics|usepackage|lstinputlisting)"
            r"\s*(?:\[[^\]]*\])?\s*\{\s*(?:/|~/|[A-Za-z]:[\\/])"
        ),
    ),
)
_STRICT_PROFILE_CAPABILITIES = (
    (
        ResumeLatexCapability.DYNAMIC_CODE_LOADING,
        re.compile(r"\\(?:csname|endcsname|catcode|scantokens)\b"),
    ),
    (
        ResumeLatexCapability.FILE_WRITE,
        re.compile(r"\\begin\s*\{filecontents\*?\}"),
    ),
    (
        ResumeLatexCapability.FILE_READ,
        re.compile(r"\\read\b"),
    ),
)


class ResumeLatexCapabilityError(ValueError):
    """The submitted LaTeX source requests a plainly unsafe capability."""

    def __init__(self, capability: ResumeLatexCapability) -> None:
        super().__init__(f"LaTeX source requests {capability.value}")
        self.capability = capability


class BaseLatexTemplateContractError(ValueError):
    """The source does not expose the closed P2a6c template interface."""


class ResumeLatexDependencyPolicyError(ValueError):
    """The strict single-file source uses a forbidden dependency."""


def _clean_text(name: str, value: Any, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the LaTeX version contract")
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


def _normalize_labels(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        raise TypeError("labels must be an iterable of strings")
    cleaned: set[str] = set()
    for item in value:
        label = _clean_text("label", item, maximum=MAX_LATEX_LABEL_CHARS)
        if _LABEL_PATTERN.fullmatch(label) is None:
            raise ValueError("label is outside the LaTeX version contract")
        cleaned.add(label)
    if len(cleaned) > MAX_LATEX_LABELS:
        raise ValueError("too many labels for one LaTeX version")
    return tuple(sorted(cleaned))


def validate_latex_source(source: str) -> str:
    """Reject plainly unsafe capabilities; full compile safety belongs to P2a7."""

    if not isinstance(source, str):
        raise TypeError("LaTeX source must be a string")
    if not source.strip():
        raise ValueError("LaTeX source is empty")
    if len(source.encode("utf-8")) > MAX_LATEX_SOURCE_BYTES:
        raise ValueError("LaTeX source is too large")
    for capability, pattern in _FORBIDDEN_CAPABILITIES:
        if pattern.search(source) is not None:
            raise ResumeLatexCapabilityError(capability)
    return source


def validate_single_file_base_latex_template(source: str) -> str:
    """Validate the draft-independent P2a6c base-template interface."""

    validate_latex_source(source)
    active = _active_latex(source)
    for capability, pattern in _STRICT_PROFILE_CAPABILITIES:
        if pattern.search(active) is not None:
            raise ResumeLatexCapabilityError(capability)
    if single_file_external_dependencies(active):
        raise ResumeLatexDependencyPolicyError(
            "single-file template has an external dependency"
        )
    if unmanaged_latex_packages(active):
        raise ResumeLatexDependencyPolicyError(
            "single-file template has an unmanaged package"
        )
    required = (
        "\\documentclass",
        "\\begin{document}",
        "\\end{document}",
    )
    if any(
        active.count(token) != 1 or source.count(token) != 1
        for token in required
    ):
        raise BaseLatexTemplateContractError(
            "base template document root is invalid"
        )
    active_document_class = active.index("\\documentclass")
    active_begin_document = active.index("\\begin{document}")
    active_end_document = active.index("\\end{document}")
    if (
        not active_document_class
        < active_begin_document
        < active_end_document
        or active[:active_document_class].strip()
        or active[
            active_end_document + len("\\end{document}") :
        ].strip()
    ):
        raise BaseLatexTemplateContractError(
            "base template document order is invalid"
        )
    begin_document = source.index("\\begin{document}")
    end_document = source.index("\\end{document}")
    if not uses_controlled_markers(source):
        raise BaseLatexTemplateContractError(
            "base template controlled region is invalid"
        )
    begin_anchor = source.index(JOBOPS_CONTENT_BEGIN)
    end_anchor = source.index(JOBOPS_CONTENT_END)
    if not begin_document < begin_anchor < end_anchor < end_document:
        raise BaseLatexTemplateContractError(
            "base template anchors are outside the document"
        )
    _, region, _ = split_controlled_region(source)
    if region.strip():
        raise BaseLatexTemplateContractError(
            "base template controlled region must be empty"
        )
    definitions = {
        SECTION_MACRO: re.compile(
            rf"\\(?:provide|new|renew)command\s*"
            rf"(?:\{{\s*)?\\{SECTION_MACRO}(?:\s*\}})?\s*\[2\]"
        ),
        BULLET_MACRO: re.compile(
            rf"\\(?:provide|new|renew)command\s*"
            rf"(?:\{{\s*)?\\{BULLET_MACRO}(?:\s*\}})?\s*\[2\]"
        ),
    }
    for macro, pattern in definitions.items():
        matches = tuple(pattern.finditer(active))
        if (
            len(matches) != 1
            or source.index(f"\\{macro}") > begin_document
            or active.count(f"\\{macro}") != 1
            or source.count(f"\\{macro}") != 1
        ):
            raise BaseLatexTemplateContractError(
                "base template marker interface is invalid"
            )
    return source


def _active_latex(source: str) -> str:
    """Remove comments while retaining escaped percent characters."""

    lines = []
    for line in source.splitlines(keepends=True):
        end = len(line)
        for index, character in enumerate(line):
            if character != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                end = index
                break
        lines.append(line[:end] + ("\n" if line.endswith("\n") else ""))
    return "".join(lines)


def _source_reference(*, subject_id: str, source_sha256: str) -> str:
    return str(
        PurePosixPath("state")
        / "preparation"
        / "resume-latex-versions"
        / "sources"
        / _subject_storage_key(subject_id)
        / f"{source_sha256}.tex"
    )


def _identity_payload(
    *,
    contract_version: str,
    subject_id: str,
    source_reference: str,
    source_sha256: str,
    source_kind: ResumeLatexSourceKind,
    root_family_id: str,
    parent_version_id: str | None,
    template_id: str | None,
    template_sha256: str | None,
    source_resume_id: str | None,
    tailored_resume_draft_id: str | None,
    tailored_resume_draft_hash: str | None,
    fact_qa_result_id: str | None,
    fact_qa_result_hash: str | None,
    labels: tuple[str, ...],
    source_profile: LatexSourceProfile = LatexSourceProfile.GENERAL_SOURCE_V1,
    template_contract_version: str | None = None,
    dependency_policy_version: str | None = None,
    source_safety_policy_version: str | None = None,
) -> dict[str, Any]:
    profile = LatexSourceProfile(source_profile)
    payload = {
        "contract_version": contract_version,
        "fact_qa_result_hash": fact_qa_result_hash,
        "fact_qa_result_id": fact_qa_result_id,
        "labels": list(labels),
        "parent_version_id": parent_version_id,
        "root_family_id": root_family_id,
        "source_kind": source_kind.value,
        "source_reference": source_reference,
        "source_resume_id": source_resume_id,
        "source_sha256": source_sha256,
        "subject_id": subject_id,
        "tailored_resume_draft_hash": tailored_resume_draft_hash,
        "tailored_resume_draft_id": tailored_resume_draft_id,
        "template_id": template_id,
        "template_sha256": template_sha256,
    }
    if profile is LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1:
        payload.update(
            {
                "dependency_policy_version": dependency_policy_version,
                "source_profile": profile.value,
                "source_safety_policy_version": (
                    source_safety_policy_version
                ),
                "template_contract_version": template_contract_version,
            }
        )
    return payload


def resume_latex_version_id(**values: Any) -> str:
    return "resume-latex-version-" + _canonical_hash(
        _identity_payload(**values)
    )


def resume_latex_root_family_id(**values: Any) -> str:
    """Derive a stable family for a parentless version without using time or paths."""

    payload = _identity_payload(
        **values,
        root_family_id="",
        parent_version_id=None,
    )
    payload.pop("root_family_id")
    payload.pop("parent_version_id")
    return "resume-latex-family-" + _canonical_hash(payload)


@dataclass(frozen=True, slots=True)
class ResumeLatexVersion:
    latex_version_id: str
    contract_version: str
    subject_id: str
    source_reference: str
    source_sha256: str
    source_kind: ResumeLatexSourceKind
    root_family_id: str
    parent_version_id: str | None
    template_id: str | None
    template_sha256: str | None
    source_resume_id: str | None
    tailored_resume_draft_id: str | None
    tailored_resume_draft_hash: str | None
    fact_qa_result_id: str | None
    fact_qa_result_hash: str | None
    labels: tuple[str, ...]
    created_at: datetime
    source_profile: LatexSourceProfile = LatexSourceProfile.GENERAL_SOURCE_V1
    template_contract_version: str | None = None
    dependency_policy_version: str | None = None
    source_safety_policy_version: str | None = None

    def __post_init__(self) -> None:
        contract = _clean_text(
            "contract_version", self.contract_version, maximum=80
        )
        if contract != RESUME_LATEX_VERSION_CONTRACT_VERSION:
            raise ValueError("LaTeX version contract is unsupported")
        subject = _clean_text("subject_id", self.subject_id, maximum=160)
        reference = _clean_text(
            "source_reference", self.source_reference, maximum=400
        )
        source_hash = _require_hash("source_sha256", self.source_sha256)
        kind = ResumeLatexSourceKind(self.source_kind)
        object.__setattr__(self, "source_kind", kind)
        profile = LatexSourceProfile(self.source_profile)
        strict = profile is LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1
        expected_profile_metadata = (
            BASE_LATEX_TEMPLATE_CONTRACT_VERSION,
            RESUME_LATEX_DEPENDENCY_POLICY_VERSION,
            RESUME_LATEX_SOURCE_SAFETY_POLICY_VERSION,
        )
        actual_profile_metadata = (
            self.template_contract_version,
            self.dependency_policy_version,
            self.source_safety_policy_version,
        )
        if (
            strict and actual_profile_metadata != expected_profile_metadata
        ) or (not strict and actual_profile_metadata != (None, None, None)):
            raise ValueError("LaTeX source profile metadata is invalid")
        object.__setattr__(self, "source_profile", profile)
        if (
            not isinstance(self.root_family_id, str)
            or _FAMILY_ID_PATTERN.fullmatch(self.root_family_id) is None
        ):
            raise ValueError("root_family_id is invalid")
        parent = self.parent_version_id
        if parent is not None and (
            not isinstance(parent, str)
            or _VERSION_ID_PATTERN.fullmatch(parent) is None
        ):
            raise ValueError("parent_version_id is invalid")
        template_id = _optional_text(
            "template_id", self.template_id, maximum=160
        )
        template_hash = _optional_hash(
            "template_sha256", self.template_sha256
        )
        if (template_id is None) != (template_hash is None):
            raise ValueError("template binding must be complete or absent")
        resume_id = _optional_text(
            "source_resume_id", self.source_resume_id, maximum=160
        )
        draft_id = _optional_text(
            "tailored_resume_draft_id",
            self.tailored_resume_draft_id,
            maximum=160,
        )
        draft_hash = _optional_hash(
            "tailored_resume_draft_hash", self.tailored_resume_draft_hash
        )
        if (draft_id is None) != (draft_hash is None):
            raise ValueError("draft binding must be complete or absent")
        qa_id = _optional_text(
            "fact_qa_result_id", self.fact_qa_result_id, maximum=160
        )
        qa_hash = _optional_hash(
            "fact_qa_result_hash", self.fact_qa_result_hash
        )
        if (qa_id is None) != (qa_hash is None):
            raise ValueError("fact-QA binding must be complete or absent")
        if qa_id is not None and draft_id is None:
            raise ValueError("a fact-QA binding requires its draft binding")
        labels = _normalize_labels(self.labels)
        object.__setattr__(self, "labels", labels)
        if reference != _source_reference(
            subject_id=subject, source_sha256=source_hash
        ):
            raise ValueError("source_reference does not match its binding")
        expected = resume_latex_version_id(
            contract_version=contract,
            subject_id=subject,
            source_reference=reference,
            source_sha256=source_hash,
            source_kind=kind,
            root_family_id=self.root_family_id,
            parent_version_id=parent,
            template_id=template_id,
            template_sha256=template_hash,
            source_resume_id=resume_id,
            tailored_resume_draft_id=draft_id,
            tailored_resume_draft_hash=draft_hash,
            fact_qa_result_id=qa_id,
            fact_qa_result_hash=qa_hash,
            labels=labels,
            source_profile=profile,
            template_contract_version=self.template_contract_version,
            dependency_policy_version=self.dependency_policy_version,
            source_safety_policy_version=self.source_safety_policy_version,
        )
        if (
            not isinstance(self.latex_version_id, str)
            or _VERSION_ID_PATTERN.fullmatch(self.latex_version_id) is None
            or self.latex_version_id != expected
        ):
            raise ValueError("latex_version_id does not match its binding")
        if self.latex_version_id == parent:
            raise ValueError("a LaTeX version cannot be its own parent")
        object.__setattr__(self, "contract_version", contract)
        object.__setattr__(self, "subject_id", subject)
        object.__setattr__(self, "source_reference", reference)
        object.__setattr__(self, "template_id", template_id)
        object.__setattr__(self, "source_resume_id", resume_id)
        object.__setattr__(self, "tailored_resume_draft_id", draft_id)
        object.__setattr__(self, "fact_qa_result_id", qa_id)
        _require_aware("created_at", self.created_at)

    def content_dict(self) -> dict[str, Any]:
        return {
            "latex_version_id": self.latex_version_id,
            **_identity_payload(
                contract_version=self.contract_version,
                subject_id=self.subject_id,
                source_reference=self.source_reference,
                source_sha256=self.source_sha256,
                source_kind=self.source_kind,
                root_family_id=self.root_family_id,
                parent_version_id=self.parent_version_id,
                template_id=self.template_id,
                template_sha256=self.template_sha256,
                source_resume_id=self.source_resume_id,
                tailored_resume_draft_id=self.tailored_resume_draft_id,
                tailored_resume_draft_hash=self.tailored_resume_draft_hash,
                fact_qa_result_id=self.fact_qa_result_id,
                fact_qa_result_hash=self.fact_qa_result_hash,
                labels=self.labels,
                source_profile=self.source_profile,
                template_contract_version=self.template_contract_version,
                dependency_policy_version=self.dependency_policy_version,
                source_safety_policy_version=(
                    self.source_safety_policy_version
                ),
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "created_at": _rfc3339(self.created_at),
        }


@dataclass(frozen=True, slots=True)
class ResumeLatexVersionWriteResult:
    status: ResumeLatexVersionWriteStatus
    version: ResumeLatexVersion | None
    reason_code: ResumeLatexVersionFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ResumeLatexVersionWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLatexVersionFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ResumeLatexVersionWriteStatus.CREATED,
            ResumeLatexVersionWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.version, ResumeLatexVersion)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful LaTeX write result is invalid")
        elif self.version is not None or self.reason_code is None:
            raise ValueError("failed LaTeX write result is invalid")


@dataclass(frozen=True, slots=True)
class ResumeLatexVersionReadResult:
    status: ResumeLatexVersionReadStatus
    version: ResumeLatexVersion | None
    reason_code: ResumeLatexVersionFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeLatexVersionReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLatexVersionFailureReason(self.reason_code),
            )
        if status is ResumeLatexVersionReadStatus.FOUND:
            if (
                not isinstance(self.version, ResumeLatexVersion)
                or self.reason_code is not None
            ):
                raise ValueError("found LaTeX read result is invalid")
        elif status is ResumeLatexVersionReadStatus.NOT_FOUND:
            if self.version is not None or self.reason_code is not None:
                raise ValueError("not-found LaTeX read result is invalid")
        elif (
            self.version is not None
            or self.reason_code
            is not ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure LaTeX read result is invalid")


@dataclass(frozen=True, slots=True)
class ResumeLatexVersionListResult:
    status: ResumeLatexVersionListStatus
    subject_id: str
    versions: tuple[ResumeLatexVersion, ...]
    reason_code: ResumeLatexVersionFailureReason | None = None

    def __post_init__(self) -> None:
        status = ResumeLatexVersionListStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLatexVersionFailureReason(self.reason_code),
            )
        _clean_text("subject_id", self.subject_id, maximum=160)
        if not isinstance(self.versions, tuple) or any(
            not isinstance(item, ResumeLatexVersion)
            for item in self.versions
        ):
            raise TypeError("versions must be a typed tuple")
        if status is ResumeLatexVersionListStatus.SUCCEEDED:
            if self.reason_code is not None:
                raise ValueError("successful LaTeX list result is invalid")
            identifiers = [item.latex_version_id for item in self.versions]
            if identifiers != sorted(identifiers):
                raise ValueError("LaTeX versions must use a stable order")
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("LaTeX version identities must be unique")
        elif self.versions or self.reason_code is None:
            raise ValueError("failed LaTeX list result is invalid")


@runtime_checkable
class ResumeLatexVersionProvider(Protocol):
    def list_selectable(
        self, subject_id: str
    ) -> ResumeLatexVersionListResult:
        """Return this subject's typed LaTeX versions in stable order."""


@runtime_checkable
class ResumeLatexVersionRepository(ResumeLatexVersionProvider, Protocol):
    def save(
        self, version: ResumeLatexVersion
    ) -> ResumeLatexVersionWriteResult:
        """Persist one immutable LaTeX version record."""

    def get(
        self, *, subject_id: str, latex_version_id: str
    ) -> ResumeLatexVersionReadResult:
        """Read one subject-owned LaTeX version."""


def _version_from_dict(value: Any) -> ResumeLatexVersion:
    legacy_expected = {
        "latex_version_id",
        "contract_version",
        "subject_id",
        "source_reference",
        "source_sha256",
        "source_kind",
        "root_family_id",
        "parent_version_id",
        "template_id",
        "template_sha256",
        "source_resume_id",
        "tailored_resume_draft_id",
        "tailored_resume_draft_hash",
        "fact_qa_result_id",
        "fact_qa_result_hash",
        "labels",
        "created_at",
    }
    strict_expected = legacy_expected | {
        "source_profile",
        "template_contract_version",
        "dependency_policy_version",
        "source_safety_policy_version",
    }
    keys = frozenset(value) if isinstance(value, Mapping) else frozenset()
    if (
        not isinstance(value, Mapping)
        or keys not in {frozenset(legacy_expected), frozenset(strict_expected)}
        or not isinstance(value["labels"], list)
    ):
        raise ValueError("persisted ResumeLatexVersion is invalid")
    is_strict_record = keys == frozenset(strict_expected)
    return ResumeLatexVersion(
        latex_version_id=value["latex_version_id"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        source_reference=value["source_reference"],
        source_sha256=value["source_sha256"],
        source_kind=ResumeLatexSourceKind(value["source_kind"]),
        root_family_id=value["root_family_id"],
        parent_version_id=value["parent_version_id"],
        template_id=value["template_id"],
        template_sha256=value["template_sha256"],
        source_resume_id=value["source_resume_id"],
        tailored_resume_draft_id=value["tailored_resume_draft_id"],
        tailored_resume_draft_hash=value["tailored_resume_draft_hash"],
        fact_qa_result_id=value["fact_qa_result_id"],
        fact_qa_result_hash=value["fact_qa_result_hash"],
        labels=tuple(value["labels"]),
        created_at=_parse_timestamp(value["created_at"]),
        source_profile=(
            LatexSourceProfile(value["source_profile"])
            if is_strict_record
            else LatexSourceProfile.GENERAL_SOURCE_V1
        ),
        template_contract_version=(
            value["template_contract_version"] if is_strict_record else None
        ),
        dependency_policy_version=(
            value["dependency_policy_version"] if is_strict_record else None
        ),
        source_safety_policy_version=(
            value["source_safety_policy_version"]
            if is_strict_record
            else None
        ),
    )


class PrivateHomeResumeLatexVersionRepository:
    """Immutable LaTeX version records with fail-closed source verification."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _subject_records(self, subject_id: str) -> Path:
        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        return (
            self._home.paths.resume_latex_version_records
            / _subject_storage_key(cleaned)
        )

    def _record_path(self, subject_id: str, latex_version_id: str) -> Path:
        if (
            not isinstance(latex_version_id, str)
            or _VERSION_ID_PATTERN.fullmatch(latex_version_id) is None
        ):
            raise ValueError("latex_version_id is invalid")
        return self._subject_records(subject_id) / f"{latex_version_id}.json"

    def _source_is_valid(self, version: ResumeLatexVersion) -> bool:
        try:
            path = self._home.contained_path(version.source_reference)
            if path.is_symlink() or not path.is_file():
                return False
            size = path.stat(follow_symlinks=False).st_size
            if size <= 0 or size > MAX_LATEX_SOURCE_BYTES:
                return False
            content = path.read_bytes()
        except (OSError, PrivateHomeError):
            return False
        if hashlib.sha256(content).hexdigest() != version.source_sha256:
            return False
        try:
            decoded = content.decode("utf-8")
            if (
                version.source_profile
                is LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1
            ):
                validate_single_file_base_latex_template(decoded)
            else:
                validate_latex_source(decoded)
        except (UnicodeDecodeError, ValueError):
            return False
        return True

    def get(
        self, *, subject_id: str, latex_version_id: str
    ) -> ResumeLatexVersionReadResult:
        path = self._record_path(subject_id, latex_version_id)
        with self._lock:
            if not path.exists():
                return ResumeLatexVersionReadResult(
                    status=ResumeLatexVersionReadStatus.NOT_FOUND,
                    version=None,
                )
            if path.is_symlink() or not path.is_file():
                return ResumeLatexVersionReadResult(
                    status=ResumeLatexVersionReadStatus.INTEGRITY_FAILURE,
                    version=None,
                    reason_code=(
                        ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                    ),
                )
            try:
                version = _version_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ResumeLatexVersionReadResult(
                    status=ResumeLatexVersionReadStatus.INTEGRITY_FAILURE,
                    version=None,
                    reason_code=(
                        ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                    ),
                )
            if (
                version.subject_id != subject_id.strip()
                or version.latex_version_id != latex_version_id
                or path.name != f"{version.latex_version_id}.json"
                or not self._source_is_valid(version)
            ):
                return ResumeLatexVersionReadResult(
                    status=ResumeLatexVersionReadStatus.INTEGRITY_FAILURE,
                    version=None,
                    reason_code=(
                        ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                    ),
                )
            return ResumeLatexVersionReadResult(
                status=ResumeLatexVersionReadStatus.FOUND,
                version=version,
            )

    def list_selectable(
        self, subject_id: str
    ) -> ResumeLatexVersionListResult:
        cleaned = _clean_text("subject_id", subject_id, maximum=160)
        directory = self._subject_records(cleaned)
        if not directory.exists():
            return ResumeLatexVersionListResult(
                status=ResumeLatexVersionListStatus.SUCCEEDED,
                subject_id=cleaned,
                versions=(),
            )
        if directory.is_symlink() or not directory.is_dir():
            return ResumeLatexVersionListResult(
                status=ResumeLatexVersionListStatus.FAILED,
                subject_id=cleaned,
                versions=(),
                reason_code=(
                    ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                ),
            )
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            return ResumeLatexVersionListResult(
                status=ResumeLatexVersionListStatus.FAILED,
                subject_id=cleaned,
                versions=(),
                reason_code=(
                    ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                ),
            )
        versions: list[ResumeLatexVersion] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or _VERSION_ID_PATTERN.fullmatch(path.stem) is None
            ):
                return ResumeLatexVersionListResult(
                    status=ResumeLatexVersionListStatus.FAILED,
                    subject_id=cleaned,
                    versions=(),
                    reason_code=(
                        ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                    ),
                )
            result = self.get(
                subject_id=cleaned, latex_version_id=path.stem
            )
            if (
                result.status is not ResumeLatexVersionReadStatus.FOUND
                or result.version is None
            ):
                return ResumeLatexVersionListResult(
                    status=ResumeLatexVersionListStatus.FAILED,
                    subject_id=cleaned,
                    versions=(),
                    reason_code=(
                        ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                    ),
                )
            versions.append(result.version)
        return ResumeLatexVersionListResult(
            status=ResumeLatexVersionListStatus.SUCCEEDED,
            subject_id=cleaned,
            versions=tuple(
                sorted(versions, key=lambda item: item.latex_version_id)
            ),
        )

    def save(
        self, version: ResumeLatexVersion
    ) -> ResumeLatexVersionWriteResult:
        if not isinstance(version, ResumeLatexVersion):
            raise TypeError("version must be a ResumeLatexVersion")
        path = self._record_path(version.subject_id, version.latex_version_id)
        with self._lock:
            if not self._source_is_valid(version):
                return ResumeLatexVersionWriteResult(
                    status=ResumeLatexVersionWriteStatus.FAILED,
                    version=None,
                    reason_code=(
                        ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                    ),
                    retryable=False,
                )
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            version.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ResumeLatexVersionWriteResult(
                    status=ResumeLatexVersionWriteStatus.FAILED,
                    version=None,
                    reason_code=(
                        ResumeLatexVersionFailureReason.PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return ResumeLatexVersionWriteResult(
                    status=ResumeLatexVersionWriteStatus.CREATED,
                    version=version,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(
                subject_id=version.subject_id,
                latex_version_id=version.latex_version_id,
            )
            if (
                existing.status is ResumeLatexVersionReadStatus.FOUND
                and existing.version is not None
                and existing.version.content_dict() == version.content_dict()
            ):
                return ResumeLatexVersionWriteResult(
                    status=ResumeLatexVersionWriteStatus.UNCHANGED,
                    version=existing.version,
                    reason_code=None,
                    retryable=False,
                )
            return ResumeLatexVersionWriteResult(
                status=ResumeLatexVersionWriteStatus.FAILED,
                version=None,
                reason_code=(
                    ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
                ),
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class RegisterResumeLatexVersionCommand:
    subject_id: str
    source_kind: ResumeLatexSourceKind
    now: datetime
    latex_source: str | None = None
    source_path: str | Path | None = None
    parent_version_id: str | None = None
    root_family_id: str | None = None
    template_id: str | None = None
    template_sha256: str | None = None
    source_resume_id: str | None = None
    tailored_resume_draft_id: str | None = None
    tailored_resume_draft_hash: str | None = None
    fact_qa_result_id: str | None = None
    fact_qa_result_hash: str | None = None
    labels: tuple[str, ...] = ()
    source_profile: LatexSourceProfile = LatexSourceProfile.GENERAL_SOURCE_V1


@dataclass(frozen=True, slots=True)
class RegisterResumeLatexVersionResult:
    status: RegisterResumeLatexVersionStatus
    version: ResumeLatexVersion | None
    write_result: ResumeLatexVersionWriteResult | None
    reason_code: ResumeLatexVersionFailureReason | None
    rejected_capability: ResumeLatexCapability | None
    retryable: bool
    message: str

    def __post_init__(self) -> None:
        status = RegisterResumeLatexVersionStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ResumeLatexVersionFailureReason(self.reason_code),
            )
        if self.rejected_capability is not None:
            object.__setattr__(
                self,
                "rejected_capability",
                ResumeLatexCapability(self.rejected_capability),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            RegisterResumeLatexVersionStatus.CREATED,
            RegisterResumeLatexVersionStatus.UNCHANGED,
        }:
            expected = ResumeLatexVersionWriteStatus(status.value)
            if (
                not isinstance(self.version, ResumeLatexVersion)
                or not isinstance(
                    self.write_result, ResumeLatexVersionWriteResult
                )
                or self.write_result.status is not expected
                or self.write_result.version != self.version
                or self.reason_code is not None
                or self.rejected_capability is not None
                or self.retryable
            ):
                raise ValueError("successful registration result is invalid")
        elif self.version is not None or self.reason_code is None:
            raise ValueError("failed registration result is invalid")
        elif (
            self.rejected_capability is not None
            and self.reason_code
            is not ResumeLatexVersionFailureReason.SOURCE_CAPABILITY_REJECTED
        ):
            raise ValueError("rejected capability requires its reason code")


def _failure(
    reason: ResumeLatexVersionFailureReason,
    *,
    retryable: bool = False,
    capability: ResumeLatexCapability | None = None,
    write_result: ResumeLatexVersionWriteResult | None = None,
) -> RegisterResumeLatexVersionResult:
    return RegisterResumeLatexVersionResult(
        status=RegisterResumeLatexVersionStatus.FAILED,
        version=None,
        write_result=write_result,
        reason_code=reason,
        rejected_capability=capability,
        retryable=retryable,
        message=f"LaTeX version registration failed: {reason.value}.",
    )


def register_resume_latex_version(
    command: RegisterResumeLatexVersionCommand,
    *,
    home: PrivateHome | None = None,
    repository: ResumeLatexVersionRepository | None = None,
) -> RegisterResumeLatexVersionResult:
    """Register one explicitly supplied LaTeX source without scanning any tree."""

    active_home = home or PrivateHome.discover()
    active_repository = repository or PrivateHomeResumeLatexVersionRepository(
        active_home
    )
    if not isinstance(command, RegisterResumeLatexVersionCommand):
        return _failure(ResumeLatexVersionFailureReason.INVALID_REQUEST)
    try:
        profile = LatexSourceProfile(command.source_profile)
    except (TypeError, ValueError):
        return _failure(ResumeLatexVersionFailureReason.SOURCE_PROFILE_INVALID)
    try:
        subject_id = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        kind = ResumeLatexSourceKind(command.source_kind)
        now = _require_aware("now", command.now)
        labels = _normalize_labels(command.labels)
        parent_id = _optional_text(
            "parent_version_id", command.parent_version_id, maximum=160
        )
        if parent_id is not None and (
            _VERSION_ID_PATTERN.fullmatch(parent_id) is None
        ):
            raise ValueError("parent_version_id is invalid")
        requested_family = _optional_text(
            "root_family_id", command.root_family_id, maximum=160
        )
        if requested_family is not None and (
            _FAMILY_ID_PATTERN.fullmatch(requested_family) is None
        ):
            raise ValueError("root_family_id is invalid")
        template_id = _optional_text(
            "template_id", command.template_id, maximum=160
        )
        template_hash = _optional_hash(
            "template_sha256", command.template_sha256
        )
        if (template_id is None) != (template_hash is None):
            raise ValueError("template binding must be complete or absent")
        resume_id = _optional_text(
            "source_resume_id", command.source_resume_id, maximum=160
        )
        draft_id = _optional_text(
            "tailored_resume_draft_id",
            command.tailored_resume_draft_id,
            maximum=160,
        )
        draft_hash = _optional_hash(
            "tailored_resume_draft_hash",
            command.tailored_resume_draft_hash,
        )
        if (draft_id is None) != (draft_hash is None):
            raise ValueError("draft binding must be complete or absent")
        qa_id = _optional_text(
            "fact_qa_result_id", command.fact_qa_result_id, maximum=160
        )
        qa_hash = _optional_hash(
            "fact_qa_result_hash", command.fact_qa_result_hash
        )
        if (qa_id is None) != (qa_hash is None):
            raise ValueError("fact-QA binding must be complete or absent")
        if qa_id is not None and draft_id is None:
            raise ValueError("a fact-QA binding requires its draft binding")
    except (AttributeError, TypeError, ValueError):
        return _failure(ResumeLatexVersionFailureReason.INVALID_REQUEST)

    if command.latex_source is not None and command.source_path is not None:
        return _failure(ResumeLatexVersionFailureReason.SOURCE_AMBIGUOUS)
    if command.latex_source is None and command.source_path is None:
        return _failure(ResumeLatexVersionFailureReason.SOURCE_MISSING)
    if (
        profile is LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1
        and command.source_path is not None
    ):
        return _failure(ResumeLatexVersionFailureReason.SOURCE_UNMANAGED)

    try:
        active_home.ensure()
    except (OSError, PrivateHomeError):
        return _failure(
            ResumeLatexVersionFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )

    if command.latex_source is not None:
        if not isinstance(command.latex_source, str):
            return _failure(ResumeLatexVersionFailureReason.INVALID_REQUEST)
        try:
            content = command.latex_source.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            return _failure(ResumeLatexVersionFailureReason.SOURCE_NOT_UTF8)
    else:
        try:
            source_path = active_home.contained_path(command.source_path)
        except (TypeError, ValueError):
            return _failure(ResumeLatexVersionFailureReason.INVALID_REQUEST)
        except PrivateHomeError:
            return _failure(ResumeLatexVersionFailureReason.SOURCE_UNMANAGED)
        if (
            source_path.suffix != ".tex"
            or source_path.is_symlink()
            or not source_path.is_file()
        ):
            return _failure(ResumeLatexVersionFailureReason.SOURCE_UNMANAGED)
        try:
            size = source_path.stat(follow_symlinks=False).st_size
            if size <= 0 or size > MAX_LATEX_SOURCE_BYTES:
                return _failure(
                    ResumeLatexVersionFailureReason.SOURCE_INVALID
                )
            content = source_path.read_bytes()
        except OSError:
            return _failure(ResumeLatexVersionFailureReason.SOURCE_INVALID)

    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        return _failure(ResumeLatexVersionFailureReason.SOURCE_NOT_UTF8)
    try:
        validate_latex_source(decoded)
    except ResumeLatexCapabilityError as rejection:
        return _failure(
            ResumeLatexVersionFailureReason.SOURCE_CAPABILITY_REJECTED,
            capability=rejection.capability,
        )
    except (TypeError, ValueError):
        return _failure(ResumeLatexVersionFailureReason.SOURCE_INVALID)
    if profile is LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1:
        try:
            validate_single_file_base_latex_template(decoded)
        except ResumeLatexCapabilityError as rejection:
            return _failure(
                ResumeLatexVersionFailureReason.SOURCE_CAPABILITY_REJECTED,
                capability=rejection.capability,
            )
        except ResumeLatexDependencyPolicyError:
            return _failure(
                ResumeLatexVersionFailureReason.DEPENDENCY_POLICY_REJECTED
            )
        except BaseLatexTemplateContractError:
            return _failure(
                ResumeLatexVersionFailureReason.TEMPLATE_CONTRACT_REJECTED
            )
        except (TypeError, ValueError):
            return _failure(ResumeLatexVersionFailureReason.SOURCE_INVALID)

    source_hash = hashlib.sha256(content).hexdigest()
    reference = _source_reference(
        subject_id=subject_id, source_sha256=source_hash
    )

    if parent_id is not None:
        try:
            parent_result = active_repository.get(
                subject_id=subject_id, latex_version_id=parent_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                ResumeLatexVersionFailureReason.PARENT_INTEGRITY_FAILURE
            )
        if parent_result.status is ResumeLatexVersionReadStatus.NOT_FOUND:
            return _failure(
                ResumeLatexVersionFailureReason.PARENT_NOT_FOUND
            )
        if (
            parent_result.status is not ResumeLatexVersionReadStatus.FOUND
            or not isinstance(parent_result.version, ResumeLatexVersion)
        ):
            return _failure(
                ResumeLatexVersionFailureReason.PARENT_INTEGRITY_FAILURE
            )
        parent = parent_result.version
        if parent.subject_id != subject_id:
            return _failure(
                ResumeLatexVersionFailureReason.PARENT_NOT_FOUND
            )
        family_id = parent.root_family_id
        if requested_family is not None and requested_family != family_id:
            return _failure(
                ResumeLatexVersionFailureReason.ROOT_FAMILY_CONFLICT
            )
    elif requested_family is not None:
        family_id = requested_family
    else:
        family_values: dict[str, Any] = {
            "source_profile": profile,
        }
        if profile is LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1:
            family_values.update(
                {
                    "template_contract_version": (
                        BASE_LATEX_TEMPLATE_CONTRACT_VERSION
                    ),
                    "dependency_policy_version": (
                        RESUME_LATEX_DEPENDENCY_POLICY_VERSION
                    ),
                    "source_safety_policy_version": (
                        RESUME_LATEX_SOURCE_SAFETY_POLICY_VERSION
                    ),
                }
            )
        family_id = resume_latex_root_family_id(
            contract_version=RESUME_LATEX_VERSION_CONTRACT_VERSION,
            subject_id=subject_id,
            source_reference=reference,
            source_sha256=source_hash,
            source_kind=kind,
            template_id=template_id,
            template_sha256=template_hash,
            source_resume_id=resume_id,
            tailored_resume_draft_id=draft_id,
            tailored_resume_draft_hash=draft_hash,
            fact_qa_result_id=qa_id,
            fact_qa_result_hash=qa_hash,
            labels=labels,
            **family_values,
        )

    target = active_home.contained_path(reference)
    try:
        created_source = active_home.write_bytes_if_absent(target, content)
        if not created_source and (
            target.is_symlink()
            or not target.is_file()
            or target.read_bytes() != content
        ):
            return _failure(
                ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
            )
    except (OSError, PrivateHomeError):
        return _failure(
            ResumeLatexVersionFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )

    values = {
        "contract_version": RESUME_LATEX_VERSION_CONTRACT_VERSION,
        "subject_id": subject_id,
        "source_reference": reference,
        "source_sha256": source_hash,
        "source_kind": kind,
        "root_family_id": family_id,
        "parent_version_id": parent_id,
        "template_id": template_id,
        "template_sha256": template_hash,
        "source_resume_id": resume_id,
        "tailored_resume_draft_id": draft_id,
        "tailored_resume_draft_hash": draft_hash,
        "fact_qa_result_id": qa_id,
        "fact_qa_result_hash": qa_hash,
        "labels": labels,
    }
    if profile is LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1:
        values.update(
            {
                "source_profile": profile,
                "template_contract_version": (
                    BASE_LATEX_TEMPLATE_CONTRACT_VERSION
                ),
                "dependency_policy_version": (
                    RESUME_LATEX_DEPENDENCY_POLICY_VERSION
                ),
                "source_safety_policy_version": (
                    RESUME_LATEX_SOURCE_SAFETY_POLICY_VERSION
                ),
            }
        )
    try:
        version = ResumeLatexVersion(
            latex_version_id=resume_latex_version_id(**values),
            created_at=now,
            **values,
        )
    except (TypeError, ValueError):
        return _failure(ResumeLatexVersionFailureReason.INVALID_REQUEST)

    try:
        write_result = active_repository.save(version)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            ResumeLatexVersionFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )
    if write_result.status is ResumeLatexVersionWriteStatus.FAILED:
        return _failure(
            write_result.reason_code
            or ResumeLatexVersionFailureReason.PERSISTENCE_FAILED,
            retryable=write_result.retryable,
            write_result=write_result,
        )
    status = RegisterResumeLatexVersionStatus(write_result.status.value)
    return RegisterResumeLatexVersionResult(
        status=status,
        version=write_result.version,
        write_result=write_result,
        reason_code=None,
        rejected_capability=None,
        retryable=False,
        message=(
            "The LaTeX resume version was registered."
            if status is RegisterResumeLatexVersionStatus.CREATED
            else "The existing LaTeX resume version is unchanged."
        ),
    )


__all__ = [
    "BASE_LATEX_TEMPLATE_CONTRACT_VERSION",
    "BaseLatexTemplateContractError",
    "LatexSourceProfile",
    "MAX_LATEX_LABELS",
    "MAX_LATEX_LABEL_CHARS",
    "MAX_LATEX_SOURCE_BYTES",
    "PrivateHomeResumeLatexVersionRepository",
    "RESUME_LATEX_VERSION_CONTRACT_VERSION",
    "RESUME_LATEX_SOURCE_SAFETY_POLICY_VERSION",
    "RegisterResumeLatexVersionCommand",
    "RegisterResumeLatexVersionResult",
    "RegisterResumeLatexVersionStatus",
    "ResumeLatexCapability",
    "ResumeLatexCapabilityError",
    "ResumeLatexDependencyPolicyError",
    "ResumeLatexSourceKind",
    "ResumeLatexVersion",
    "ResumeLatexVersionFailureReason",
    "ResumeLatexVersionListResult",
    "ResumeLatexVersionListStatus",
    "ResumeLatexVersionProvider",
    "ResumeLatexVersionReadResult",
    "ResumeLatexVersionReadStatus",
    "ResumeLatexVersionRepository",
    "ResumeLatexVersionWriteResult",
    "ResumeLatexVersionWriteStatus",
    "register_resume_latex_version",
    "resume_latex_root_family_id",
    "resume_latex_version_id",
    "validate_single_file_base_latex_template",
]
