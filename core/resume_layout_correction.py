"""Explicit, preview-scoped Resume layout correction resolution."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .application_preparation_orchestrator import (
    ApplicationPreparationStatus,
    RunApplicationPreparationCommand,
    RunApplicationPreparationResult,
)
from .human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionQueueItem,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
    HumanAttentionResolutionCapability,
)
from .material_correction_ref import MaterialCorrectionTargetRef
from .material_correction_target import (
    MaterialCorrectionTarget,
    MaterialCorrectionTargetProvider,
    MaterialCorrectionTargetStatus,
    ResumeVisualLayoutCorrectionTarget,
    ResumeVisualLayoutOriginKind,
)
from .private_home import PrivateHome
from .resume_layout_correction_preview import (
    ResumeLayoutCorrectionPreview,
    ResumeLayoutCorrectionPreviewProvider,
    ResumeLayoutCorrectionPreviewRef,
    ResumeLayoutCorrectionPreviewStatus,
)
from .resume_layout_revision import (
    ResumeLayoutCorrectionConstraint,
    ResumeLayoutCorrectionConstraintReadResult,
)


RESUME_LAYOUT_CORRECTION_DIRECTIVE_VERSION = (
    "resume-layout-correction-directive-v1"
)
RESUME_LAYOUT_CORRECTION_RECEIPT_VERSION = (
    "resume-layout-correction-receipt-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_DIRECTIVE_ID_RE = re.compile(r"^resume-layout-correction-[a-f0-9]{64}$")


class ResumeLayoutCorrectionAction(StrEnum):
    REVISE_LAYOUT_AND_RETRY = "REVISE_LAYOUT_AND_RETRY"


class ResumeLayoutVisualIssue(StrEnum):
    OVERFLOW_OR_CLIPPING = "OVERFLOW_OR_CLIPPING"
    ELEMENT_OVERLAP = "ELEMENT_OVERLAP"
    POOR_READABILITY = "POOR_READABILITY"
    EXCESS_WHITESPACE = "EXCESS_WHITESPACE"
    PAGE_COUNT_MISMATCH = "PAGE_COUNT_MISMATCH"


class ResumeLayoutCorrectionMode(StrEnum):
    REVISE_FROM_VISUAL_QA_DIRECTIVE = (
        "REVISE_FROM_VISUAL_QA_DIRECTIVE"
    )
    RESTART_BOUNDED_LAYOUT_REVISION = (
        "RESTART_BOUNDED_LAYOUT_REVISION"
    )


class ResumeLayoutCorrectionStatus(StrEnum):
    CORRECTED_AND_PREPARATION_COMPLETED = (
        "CORRECTED_AND_PREPARATION_COMPLETED"
    )
    CORRECTED_AND_PREPARATION_DEFERRED = (
        "CORRECTED_AND_PREPARATION_DEFERRED"
    )
    CORRECTION_RECORDED_PREPARATION_FAILED = (
        "CORRECTION_RECORDED_PREPARATION_FAILED"
    )
    UNCHANGED = "UNCHANGED"
    ITEM_NOT_CURRENT = "ITEM_NOT_CURRENT"
    TARGET_STALE = "TARGET_STALE"
    PREVIEW_UNAVAILABLE = "PREVIEW_UNAVAILABLE"
    PREVIEW_STALE = "PREVIEW_STALE"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    INVALID_ACTION = "INVALID_ACTION"
    FAILED = "FAILED"


class ResumeLayoutCorrectionWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


_MODE_BY_ORIGIN = {
    ResumeVisualLayoutOriginKind.PUBLICATION_VISUAL_QA_DIRECTIVE: (
        ResumeLayoutCorrectionMode.REVISE_FROM_VISUAL_QA_DIRECTIVE
    ),
    ResumeVisualLayoutOriginKind.PUBLICATION_LAYOUT_REVISION_STOP: (
        ResumeLayoutCorrectionMode.RESTART_BOUNDED_LAYOUT_REVISION
    ),
    ResumeVisualLayoutOriginKind.DIRECT_LAYOUT_ATTEMPTS_EXHAUSTED: (
        ResumeLayoutCorrectionMode.RESTART_BOUNDED_LAYOUT_REVISION
    ),
}


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionCommand:
    subject_id: str
    attention_item_id: str
    action: ResumeLayoutCorrectionAction
    visual_issues: tuple[ResumeLayoutVisualIssue, ...]
    now: datetime


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionDirective:
    directive_id: str
    directive_version: int
    directive_hash: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    correction_target_ref: MaterialCorrectionTargetRef
    safe_preview_ref: ResumeLayoutCorrectionPreviewRef
    origin_kind: ResumeVisualLayoutOriginKind
    source_visual_qa_result_id: str
    source_artifact_id: str
    source_artifact_content_hash: str
    source_latex_version_id: str
    source_latex_content_hash: str
    previous_layout_run_id: str | None
    previous_final_attempt_id: str | None
    action: ResumeLayoutCorrectionAction
    visual_issues: tuple[ResumeLayoutVisualIssue, ...]
    mode: ResumeLayoutCorrectionMode
    previous_directive_id: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        if self.contract_version != RESUME_LAYOUT_CORRECTION_DIRECTIVE_VERSION:
            raise ValueError("Resume layout correction contract is unsupported")
        if (
            not isinstance(self.directive_id, str)
            or _DIRECTIVE_ID_RE.fullmatch(self.directive_id) is None
            or type(self.directive_version) is not int
            or self.directive_version < 1
        ):
            raise ValueError("Resume layout directive identity is invalid")
        _hash("directive_hash", self.directive_hash)
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            ("attention_item_id", self.attention_item_id, 240),
            (
                "source_visual_qa_result_id",
                self.source_visual_qa_result_id,
                240,
            ),
            ("source_artifact_id", self.source_artifact_id, 240),
            ("source_latex_version_id", self.source_latex_version_id, 240),
        ):
            _text(name, value, maximum)
        for name in (
            "source_artifact_content_hash",
            "source_latex_content_hash",
        ):
            _hash(name, getattr(self, name))
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ) or not isinstance(
            self.safe_preview_ref, ResumeLayoutCorrectionPreviewRef
        ):
            raise TypeError("layout correction references must be typed")
        origin = ResumeVisualLayoutOriginKind(self.origin_kind)
        action = ResumeLayoutCorrectionAction(self.action)
        mode = ResumeLayoutCorrectionMode(self.mode)
        issues = tuple(
            sorted(
                {
                    ResumeLayoutVisualIssue(issue)
                    for issue in self.visual_issues
                },
                key=lambda issue: issue.value,
            )
        )
        object.__setattr__(self, "origin_kind", origin)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "visual_issues", issues)
        if (
            action is not ResumeLayoutCorrectionAction.REVISE_LAYOUT_AND_RETRY
            or _MODE_BY_ORIGIN[origin] is not mode
        ):
            raise ValueError("layout correction action or mode is invalid")
        if (self.previous_layout_run_id is None) != (
            self.previous_final_attempt_id is None
        ):
            raise ValueError("previous Layout lineage is incomplete")
        if self.previous_layout_run_id is not None:
            _text("previous_layout_run_id", self.previous_layout_run_id, 240)
            _text(
                "previous_final_attempt_id",
                self.previous_final_attempt_id,
                300,
            )
        if self.previous_directive_id is not None:
            _text("previous_directive_id", self.previous_directive_id, 240)
        _aware("created_at", self.created_at)
        expected = _canonical_hash(self.identity_dict())
        if (
            self.directive_hash != expected
            or self.directive_id != f"resume-layout-correction-{expected}"
        ):
            raise ValueError("Resume layout correction hash is invalid")

    @property
    def constraint(self) -> ResumeLayoutCorrectionConstraint:
        return ResumeLayoutCorrectionConstraint(
            directive_id=self.directive_id,
            directive_hash=self.directive_hash,
            subject_id=self.subject_id,
            application_plan_id=self.application_plan_id,
            source_visual_qa_result_id=self.source_visual_qa_result_id,
            source_artifact_id=self.source_artifact_id,
            source_artifact_content_hash=self.source_artifact_content_hash,
            source_latex_version_id=self.source_latex_version_id,
            source_latex_content_hash=self.source_latex_content_hash,
            preview_id=self.safe_preview_ref.preview_id,
            preview_hash=self.safe_preview_ref.preview_hash,
            correction_mode=self.mode.value,
            visual_issue_selections=tuple(
                issue.value for issue in self.visual_issues
            ),
            previous_layout_run_id=self.previous_layout_run_id,
            previous_final_attempt_id=self.previous_final_attempt_id,
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "contract_version": self.contract_version,
            "correction_target_ref": self.correction_target_ref.to_dict(),
            "directive_version": self.directive_version,
            "mode": self.mode.value,
            "origin_kind": self.origin_kind.value,
            "previous_directive_id": self.previous_directive_id,
            "previous_final_attempt_id": self.previous_final_attempt_id,
            "previous_layout_run_id": self.previous_layout_run_id,
            "safe_preview_ref": self.safe_preview_ref.to_dict(),
            "source_artifact_content_hash": (
                self.source_artifact_content_hash
            ),
            "source_artifact_id": self.source_artifact_id,
            "source_latex_content_hash": self.source_latex_content_hash,
            "source_latex_version_id": self.source_latex_version_id,
            "source_visual_qa_result_id": (
                self.source_visual_qa_result_id
            ),
            "subject_id": self.subject_id,
            "visual_issues": [issue.value for issue in self.visual_issues],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "created_at": _rfc3339(self.created_at),
            "directive_hash": self.directive_hash,
            "directive_id": self.directive_id,
        }

    @classmethod
    def create(
        cls,
        *,
        item: HumanAttentionQueueItem,
        target: MaterialCorrectionTarget,
        preview: ResumeLayoutCorrectionPreview,
        source_visual_qa_result_id: str,
        previous_layout_run_id: str | None,
        previous_final_attempt_id: str | None,
        action: ResumeLayoutCorrectionAction,
        visual_issues: tuple[ResumeLayoutVisualIssue, ...],
        previous: "ResumeLayoutCorrectionDirective | None",
        created_at: datetime,
    ) -> "ResumeLayoutCorrectionDirective":
        payload = target.payload
        if (
            not isinstance(payload, ResumeVisualLayoutCorrectionTarget)
            or target.reference != item.correction_target_ref
            or preview.correction_target_ref != target.reference
            or preview.subject_id != target.subject_id
            or preview.application_plan_id != target.application_plan_id
            or preview.preparation_run_id != target.preparation_run_id
            or preview.source_artifact_id != payload.artifact_id
            or preview.source_artifact_content_hash
            != payload.artifact_content_hash
            or preview.latex_source_id != payload.latex_source_id
            or preview.latex_source_content_hash
            != payload.latex_source_content_hash
            or preview.final_layout_attempt_id != payload.final_attempt_id
        ):
            raise ValueError("Resume layout target/preview binding is invalid")
        mode = _MODE_BY_ORIGIN[payload.origin_kind]
        version = previous.directive_version + 1 if previous else 1
        prototype = {
            "action": action.value,
            "application_plan_id": item.application_plan_id,
            "attention_item_id": item.item_id,
            "contract_version": RESUME_LAYOUT_CORRECTION_DIRECTIVE_VERSION,
            "correction_target_ref": target.reference.to_dict(),
            "directive_version": version,
            "mode": mode.value,
            "origin_kind": payload.origin_kind.value,
            "previous_directive_id": (
                previous.directive_id if previous else None
            ),
            "previous_final_attempt_id": previous_final_attempt_id,
            "previous_layout_run_id": previous_layout_run_id,
            "safe_preview_ref": preview.reference.to_dict(),
            "source_artifact_content_hash": payload.artifact_content_hash,
            "source_artifact_id": payload.artifact_id,
            "source_latex_content_hash": (
                payload.latex_source_content_hash
            ),
            "source_latex_version_id": payload.latex_source_id,
            "source_visual_qa_result_id": source_visual_qa_result_id,
            "subject_id": item.subject_id,
            "visual_issues": [issue.value for issue in visual_issues],
        }
        digest = _canonical_hash(prototype)
        return cls(
            directive_id=f"resume-layout-correction-{digest}",
            directive_version=version,
            directive_hash=digest,
            contract_version=RESUME_LAYOUT_CORRECTION_DIRECTIVE_VERSION,
            subject_id=item.subject_id,
            application_plan_id=item.application_plan_id,
            attention_item_id=item.item_id,
            correction_target_ref=target.reference,
            safe_preview_ref=preview.reference,
            origin_kind=payload.origin_kind,
            source_visual_qa_result_id=source_visual_qa_result_id,
            source_artifact_id=payload.artifact_id,
            source_artifact_content_hash=payload.artifact_content_hash,
            source_latex_version_id=payload.latex_source_id,
            source_latex_content_hash=payload.latex_source_content_hash,
            previous_layout_run_id=previous_layout_run_id,
            previous_final_attempt_id=previous_final_attempt_id,
            action=action,
            visual_issues=visual_issues,
            mode=mode,
            previous_directive_id=(
                previous.directive_id if previous else None
            ),
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionWriteResult:
    status: ResumeLayoutCorrectionWriteStatus
    directive: ResumeLayoutCorrectionDirective | None


@runtime_checkable
class ResumeLayoutCorrectionDirectiveRepository(Protocol):
    def save(
        self, directive: ResumeLayoutCorrectionDirective
    ) -> ResumeLayoutCorrectionWriteResult: ...

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> ResumeLayoutCorrectionDirective | None: ...


class PrivateHomeResumeLayoutCorrectionDirectiveRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        subject = _text("subject_id", subject_id, 160)
        return (
            self._home.paths.preparation
            / "resume-layout-correction-directives"
            / ("subject-" + hashlib.sha256(subject.encode()).hexdigest())
        )

    def save(
        self, directive: ResumeLayoutCorrectionDirective
    ) -> ResumeLayoutCorrectionWriteResult:
        if not isinstance(directive, ResumeLayoutCorrectionDirective):
            raise TypeError("directive must be typed")
        path = self._directory(directive.subject_id) / (
            directive.directive_id + ".json"
        )
        content = _json(directive.to_dict())
        try:
            created = self._home.write_bytes_if_absent(path, content)
            if created:
                return ResumeLayoutCorrectionWriteResult(
                    ResumeLayoutCorrectionWriteStatus.CREATED, directive
                )
            existing = _directive_from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if existing.identity_dict() == directive.identity_dict():
                return ResumeLayoutCorrectionWriteResult(
                    ResumeLayoutCorrectionWriteStatus.UNCHANGED, existing
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return ResumeLayoutCorrectionWriteResult(
            ResumeLayoutCorrectionWriteStatus.FAILED, None
        )

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> ResumeLayoutCorrectionDirective | None:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return None
        values = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            contained = self._home.contained_path(path)
            if contained.is_symlink() or not contained.is_file():
                raise ValueError("layout directive path is unsafe")
            value = _directive_from_dict(
                json.loads(contained.read_text(encoding="utf-8"))
            )
            if value.subject_id != subject_id:
                raise ValueError("layout directive subject drifted")
            if value.application_plan_id == application_plan_id:
                values.append(value)
        if not values:
            return None
        ordered = tuple(
            sorted(values, key=lambda item: item.directive_version)
        )
        if tuple(item.directive_version for item in ordered) != tuple(
            range(1, len(ordered) + 1)
        ) or any(
            item.previous_directive_id
            != (ordered[index - 1].directive_id if index else None)
            for index, item in enumerate(ordered)
        ):
            raise ValueError("layout directive history is invalid")
        return ordered[-1]


class ResumeLayoutCorrectionDirectiveProvider:
    def __init__(
        self, repository: ResumeLayoutCorrectionDirectiveRepository
    ) -> None:
        self._repository = repository

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> ResumeLayoutCorrectionConstraintReadResult:
        try:
            directive = self._repository.get_current(
                subject_id=subject_id,
                application_plan_id=application_plan_id,
            )
            if directive is not None and (
                directive.subject_id != subject_id
                or directive.application_plan_id != application_plan_id
            ):
                raise ValueError("layout directive binding drifted")
            return ResumeLayoutCorrectionConstraintReadResult(
                True, directive.constraint if directive else None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return ResumeLayoutCorrectionConstraintReadResult(False, None)


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionReceipt:
    receipt_id: str
    receipt_hash: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    directive_id: str
    directive_version: int
    correction_target_ref: MaterialCorrectionTargetRef
    safe_preview_ref: ResumeLayoutCorrectionPreviewRef
    mode: ResumeLayoutCorrectionMode
    preparation_run_id: str | None
    preparation_status: str
    failure_reason: str | None
    completed_at: datetime

    def __post_init__(self) -> None:
        if self.contract_version != RESUME_LAYOUT_CORRECTION_RECEIPT_VERSION:
            raise ValueError("Resume layout receipt contract is unsupported")
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            ("attention_item_id", self.attention_item_id, 240),
            ("directive_id", self.directive_id, 240),
            ("preparation_status", self.preparation_status, 80),
        ):
            _text(name, value, maximum)
        if type(self.directive_version) is not int or self.directive_version < 1:
            raise ValueError("receipt directive version is invalid")
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ) or not isinstance(
            self.safe_preview_ref, ResumeLayoutCorrectionPreviewRef
        ):
            raise TypeError("receipt references must be typed")
        object.__setattr__(
            self, "mode", ResumeLayoutCorrectionMode(self.mode)
        )
        if self.preparation_run_id is not None:
            _text("preparation_run_id", self.preparation_run_id, 240)
        if self.failure_reason is not None:
            _text("failure_reason", self.failure_reason, 200)
        _aware("completed_at", self.completed_at)
        expected = _canonical_hash(self.identity_dict())
        if (
            self.receipt_hash != expected
            or self.receipt_id
            != f"resume-layout-correction-receipt-{expected}"
        ):
            raise ValueError("Resume layout receipt identity is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "contract_version": self.contract_version,
            "correction_target_ref": self.correction_target_ref.to_dict(),
            "directive_id": self.directive_id,
            "directive_version": self.directive_version,
            "failure_reason": self.failure_reason,
            "mode": self.mode.value,
            "preparation_run_id": self.preparation_run_id,
            "preparation_status": self.preparation_status,
            "safe_preview_ref": self.safe_preview_ref.to_dict(),
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "completed_at": _rfc3339(self.completed_at),
            "receipt_hash": self.receipt_hash,
            "receipt_id": self.receipt_id,
        }

    @classmethod
    def create(
        cls,
        *,
        directive: ResumeLayoutCorrectionDirective,
        preparation_run_id: str | None,
        preparation_status: str,
        failure_reason: str | None,
        completed_at: datetime,
    ) -> "ResumeLayoutCorrectionReceipt":
        prototype = {
            "application_plan_id": directive.application_plan_id,
            "attention_item_id": directive.attention_item_id,
            "contract_version": RESUME_LAYOUT_CORRECTION_RECEIPT_VERSION,
            "correction_target_ref": directive.correction_target_ref.to_dict(),
            "directive_id": directive.directive_id,
            "directive_version": directive.directive_version,
            "failure_reason": failure_reason,
            "mode": directive.mode.value,
            "preparation_run_id": preparation_run_id,
            "preparation_status": preparation_status,
            "safe_preview_ref": directive.safe_preview_ref.to_dict(),
            "subject_id": directive.subject_id,
        }
        digest = _canonical_hash(prototype)
        return cls(
            receipt_id=f"resume-layout-correction-receipt-{digest}",
            receipt_hash=digest,
            contract_version=RESUME_LAYOUT_CORRECTION_RECEIPT_VERSION,
            subject_id=directive.subject_id,
            application_plan_id=directive.application_plan_id,
            attention_item_id=directive.attention_item_id,
            directive_id=directive.directive_id,
            directive_version=directive.directive_version,
            correction_target_ref=directive.correction_target_ref,
            safe_preview_ref=directive.safe_preview_ref,
            mode=directive.mode,
            preparation_run_id=preparation_run_id,
            preparation_status=preparation_status,
            failure_reason=failure_reason,
            completed_at=completed_at,
        )


class ResumeLayoutCorrectionReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.preparation
            / "resume-layout-correction-receipts"
            / (
                "subject-"
                + hashlib.sha256(
                    _text("subject_id", subject_id, 160).encode()
                ).hexdigest()
            )
        )

    def save(self, receipt: ResumeLayoutCorrectionReceipt) -> None:
        path = self._directory(receipt.subject_id) / (
            receipt.receipt_id + ".json"
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable layout correction receipt conflict")

    def has_successful_replay(
        self, *, subject_id: str, attention_item_id: str
    ) -> bool:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return False
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            receipt = _receipt_from_dict(
                json.loads(
                    self._home.contained_path(path).read_text(encoding="utf-8")
                )
            )
            if (
                receipt.attention_item_id == attention_item_id
                and receipt.subject_id == subject_id
                and receipt.preparation_status
                in {
                    ApplicationPreparationStatus.COMPLETED.value,
                    ApplicationPreparationStatus.DEFERRED.value,
                    ApplicationPreparationStatus.UNCHANGED.value,
                }
            ):
                return True
        return False


@dataclass(frozen=True, slots=True)
class ResumeLayoutCorrectionResult:
    status: ResumeLayoutCorrectionStatus
    receipt: ResumeLayoutCorrectionReceipt | None
    reason_code: str | None
    message: str


QueueReader = Callable[
    ...,
    HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult],
]
PreparationCallable = Callable[
    ...,
    Awaitable[RunApplicationPreparationResult],
]


async def resolve_resume_layout_correction(
    command: ResumeLayoutCorrectionCommand,
    *,
    queue_reader: QueueReader,
    target_provider: MaterialCorrectionTargetProvider,
    preview_provider: ResumeLayoutCorrectionPreviewProvider,
    directive_repository: ResumeLayoutCorrectionDirectiveRepository,
    receipt_repository: ResumeLayoutCorrectionReceiptRepository,
    preparation_callable: PreparationCallable,
) -> ResumeLayoutCorrectionResult:
    try:
        subject = _text("subject_id", command.subject_id, 160)
        item_id = _text("attention_item_id", command.attention_item_id, 240)
        action = ResumeLayoutCorrectionAction(command.action)
        issues = tuple(
            sorted(
                {
                    ResumeLayoutVisualIssue(issue)
                    for issue in command.visual_issues
                },
                key=lambda issue: issue.value,
            )
        )
        now = _aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _result(
            ResumeLayoutCorrectionStatus.INVALID_ACTION,
            "INVALID_ACTION",
            "The Resume layout correction action is invalid.",
        )
    if action is not ResumeLayoutCorrectionAction.REVISE_LAYOUT_AND_RETRY:
        return _result(
            ResumeLayoutCorrectionStatus.INVALID_ACTION,
            "INVALID_ACTION",
            "The Resume layout correction action is invalid.",
        )
    try:
        queue = await _resolve(queue_reader(subject_id=subject, now=now))
        if (
            not isinstance(queue, HumanAttentionQueueResult)
            or queue.status is not HumanAttentionQueueStatus.SUCCEEDED
            or queue.subject_id != subject
        ):
            raise ValueError("current attention queue is unavailable")
        item = next(
            (entry for entry in queue.items if entry.item_id == item_id),
            None,
        )
        if item is None:
            if receipt_repository.has_successful_replay(
                subject_id=subject, attention_item_id=item_id
            ):
                return _result(
                    ResumeLayoutCorrectionStatus.UNCHANGED,
                    None,
                    "This Resume layout correction is unchanged.",
                )
            return _result(
                ResumeLayoutCorrectionStatus.ITEM_NOT_CURRENT,
                "ITEM_NOT_CURRENT",
                "The Resume layout item is no longer current.",
            )
        if (
            item.subject_id != subject
            or item.audience is not HumanAttentionAudience.USER
            or item.resolution_capability
            is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
            or item.correction_target_ref is None
        ):
            return _result(
                ResumeLayoutCorrectionStatus.UNSUPPORTED_TARGET,
                "UNSUPPORTED_TARGET",
                "This item requires a different correction path.",
            )
        typed = target_provider.get_current_typed_target(item=item)
        if typed.status is MaterialCorrectionTargetStatus.TARGET_STALE:
            return _result(
                ResumeLayoutCorrectionStatus.TARGET_STALE,
                "TARGET_STALE",
                "The Resume layout target is stale.",
            )
        if (
            typed.status is not MaterialCorrectionTargetStatus.AVAILABLE
            or typed.target is None
            or not isinstance(
                typed.target.payload, ResumeVisualLayoutCorrectionTarget
            )
        ):
            return _result(
                ResumeLayoutCorrectionStatus.UNSUPPORTED_TARGET,
                "UNSUPPORTED_TARGET",
                "This target is not a Resume layout target.",
            )
        target = typed.target
        preview_read = (
            preview_provider.get_current_resume_layout_correction_preview(
                subject_id=subject,
                correction_target_ref=target.reference,
            )
        )
        if (
            preview_read.status
            is ResumeLayoutCorrectionPreviewStatus.SOURCE_ARTIFACT_MISSING
        ):
            return _result(
                ResumeLayoutCorrectionStatus.PREVIEW_UNAVAILABLE,
                "PREVIEW_UNAVAILABLE",
                "A current safe preview is required.",
            )
        if (
            preview_read.status
            is not ResumeLayoutCorrectionPreviewStatus.AVAILABLE
            or preview_read.preview is None
        ):
            return _result(
                ResumeLayoutCorrectionStatus.PREVIEW_STALE,
                preview_read.status.value,
                "The Resume layout preview is stale or invalid.",
            )
        preview = preview_read.preview
        payload = target.payload
        if (
            preview.correction_target_ref != target.reference
            or preview.subject_id != item.subject_id
            or preview.application_plan_id != item.application_plan_id
            or preview.preparation_run_id != item.source_preparation_run_id
            or preview.source_artifact_id != payload.artifact_id
            or preview.source_artifact_content_hash
            != payload.artifact_content_hash
            or preview.latex_source_id != payload.latex_source_id
            or preview.latex_source_content_hash
            != payload.latex_source_content_hash
            or preview.final_layout_attempt_id != payload.final_attempt_id
        ):
            return _result(
                ResumeLayoutCorrectionStatus.PREVIEW_STALE,
                "PREVIEW_STALE",
                "The Resume layout preview no longer matches the target.",
            )
        previous_layout_run_id = None
        previous_final_attempt_id = None
        source_visual_qa_result_id = payload.source_result_id
        if payload.origin_kind is not (
            ResumeVisualLayoutOriginKind
            .PUBLICATION_VISUAL_QA_DIRECTIVE
        ):
            run = target_provider.get_layout_run_for_target(target=target)
            if run is None:
                raise ValueError("Layout run is unavailable")
            previous_layout_run_id = run.run_id
            previous_final_attempt_id = payload.final_attempt_id
            source_visual_qa_result_id = run.final_visual_qa_result_id
        previous = directive_repository.get_current(
            subject_id=subject,
            application_plan_id=item.application_plan_id,
        )
        mode = _MODE_BY_ORIGIN[payload.origin_kind]
        if (
            previous is not None
            and previous.correction_target_ref == target.reference
            and previous.safe_preview_ref == preview.reference
            and previous.action is action
            and previous.mode is mode
            and previous.visual_issues == issues
        ):
            return _result(
                ResumeLayoutCorrectionStatus.UNCHANGED,
                None,
                "This Resume layout correction is unchanged.",
            )
        directive = ResumeLayoutCorrectionDirective.create(
            item=item,
            target=target,
            preview=preview,
            source_visual_qa_result_id=source_visual_qa_result_id,
            previous_layout_run_id=previous_layout_run_id,
            previous_final_attempt_id=previous_final_attempt_id,
            action=action,
            visual_issues=issues,
            previous=previous,
            created_at=now,
        )
        write = directive_repository.save(directive)
        if (
            write.status is ResumeLayoutCorrectionWriteStatus.FAILED
            or write.directive is None
        ):
            raise ValueError("layout correction persistence failed")
        if write.status is ResumeLayoutCorrectionWriteStatus.UNCHANGED:
            return _result(
                ResumeLayoutCorrectionStatus.UNCHANGED,
                None,
                "This Resume layout correction is unchanged.",
            )
        try:
            preparation = await preparation_callable(
                RunApplicationPreparationCommand(
                    subject_id=subject,
                    application_plan_id=item.application_plan_id,
                    now=now,
                )
            )
            if not isinstance(preparation, RunApplicationPreparationResult):
                raise ValueError("preparation result is invalid")
            preparation_status = preparation.status
            run_id = preparation.run.run_id if preparation.run else None
            failure = (
                preparation.reason_code.value
                if preparation.reason_code is not None
                else None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            preparation_status = ApplicationPreparationStatus.FAILED
            run_id = None
            failure = "PREPARATION_RERUN_FAILED"
        receipt = ResumeLayoutCorrectionReceipt.create(
            directive=write.directive,
            preparation_run_id=run_id,
            preparation_status=preparation_status.value,
            failure_reason=failure,
            completed_at=now,
        )
        receipt_repository.save(receipt)
        status = {
            ApplicationPreparationStatus.COMPLETED: (
                ResumeLayoutCorrectionStatus
                .CORRECTED_AND_PREPARATION_COMPLETED
            ),
            ApplicationPreparationStatus.UNCHANGED: (
                ResumeLayoutCorrectionStatus
                .CORRECTED_AND_PREPARATION_COMPLETED
            ),
            ApplicationPreparationStatus.DEFERRED: (
                ResumeLayoutCorrectionStatus
                .CORRECTED_AND_PREPARATION_DEFERRED
            ),
            ApplicationPreparationStatus.FAILED: (
                ResumeLayoutCorrectionStatus
                .CORRECTION_RECORDED_PREPARATION_FAILED
            ),
        }[preparation_status]
        return ResumeLayoutCorrectionResult(
            status,
            receipt,
            failure,
            "The Resume layout correction was recorded and preparation reran.",
        )
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return _result(
            ResumeLayoutCorrectionStatus.FAILED,
            "CORRECTION_FAILED",
            "The Resume layout correction could not be recorded safely.",
        )


def _directive_from_dict(
    value: Mapping[str, Any],
) -> ResumeLayoutCorrectionDirective:
    return ResumeLayoutCorrectionDirective(
        directive_id=value["directive_id"],
        directive_version=value["directive_version"],
        directive_hash=value["directive_hash"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        attention_item_id=value["attention_item_id"],
        correction_target_ref=MaterialCorrectionTargetRef.from_dict(
            value["correction_target_ref"]
        ),
        safe_preview_ref=ResumeLayoutCorrectionPreviewRef(
            **value["safe_preview_ref"]
        ),
        origin_kind=value["origin_kind"],
        source_visual_qa_result_id=value["source_visual_qa_result_id"],
        source_artifact_id=value["source_artifact_id"],
        source_artifact_content_hash=value[
            "source_artifact_content_hash"
        ],
        source_latex_version_id=value["source_latex_version_id"],
        source_latex_content_hash=value["source_latex_content_hash"],
        previous_layout_run_id=value["previous_layout_run_id"],
        previous_final_attempt_id=value["previous_final_attempt_id"],
        action=value["action"],
        visual_issues=tuple(value["visual_issues"]),
        mode=value["mode"],
        previous_directive_id=value["previous_directive_id"],
        created_at=datetime.fromisoformat(
            value["created_at"].replace("Z", "+00:00")
        ),
    )


def _receipt_from_dict(
    value: Mapping[str, Any],
) -> ResumeLayoutCorrectionReceipt:
    return ResumeLayoutCorrectionReceipt(
        receipt_id=value["receipt_id"],
        receipt_hash=value["receipt_hash"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        attention_item_id=value["attention_item_id"],
        directive_id=value["directive_id"],
        directive_version=value["directive_version"],
        correction_target_ref=MaterialCorrectionTargetRef.from_dict(
            value["correction_target_ref"]
        ),
        safe_preview_ref=ResumeLayoutCorrectionPreviewRef(
            **value["safe_preview_ref"]
        ),
        mode=ResumeLayoutCorrectionMode(value["mode"]),
        preparation_run_id=value["preparation_run_id"],
        preparation_status=value["preparation_status"],
        failure_reason=value["failure_reason"],
        completed_at=datetime.fromisoformat(
            value["completed_at"].replace("Z", "+00:00")
        ),
    )


def _result(status, reason, message):
    return ResumeLayoutCorrectionResult(status, None, reason, message)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "PrivateHomeResumeLayoutCorrectionDirectiveRepository",
    "RESUME_LAYOUT_CORRECTION_DIRECTIVE_VERSION",
    "RESUME_LAYOUT_CORRECTION_RECEIPT_VERSION",
    "ResumeLayoutCorrectionAction",
    "ResumeLayoutCorrectionCommand",
    "ResumeLayoutCorrectionDirective",
    "ResumeLayoutCorrectionDirectiveProvider",
    "ResumeLayoutCorrectionDirectiveRepository",
    "ResumeLayoutCorrectionMode",
    "ResumeLayoutCorrectionReceipt",
    "ResumeLayoutCorrectionReceiptRepository",
    "ResumeLayoutCorrectionResult",
    "ResumeLayoutCorrectionStatus",
    "ResumeLayoutVisualIssue",
    "resolve_resume_layout_correction",
]
