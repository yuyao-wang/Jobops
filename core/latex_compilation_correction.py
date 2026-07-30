"""Plan-scoped correction directives for correctable LaTeX compilation stops."""

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
    LatexCompilationStopReason,
    ResolvedCompilationSourceLineage,
    ResumeCompilationStoppedSourceRef,
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
    LatexCompilationCorrectionTarget,
    MaterialCorrectionTarget,
    MaterialCorrectionTargetProvider,
    MaterialCorrectionTargetStatus,
)
from .preparation_invocation import PreparationInvocationBindingRef
from .private_home import PrivateHome
from .resume_compilation_stopped_source import (
    ResumeCompilationStoppedSourceReadStatus,
)


LATEX_COMPILATION_CORRECTION_DIRECTIVE_VERSION = (
    "latex-compilation-correction-directive-v1"
)
LATEX_COMPILATION_CORRECTION_RECEIPT_VERSION = (
    "latex-compilation-correction-receipt-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_DIRECTIVE_ID_RE = re.compile(
    r"^latex-compilation-correction-[a-f0-9]{64}$"
)


class LatexCompilationCorrectionAction(StrEnum):
    REGENERATE_AND_RETRY = "REGENERATE_AND_RETRY"


class LatexCompilationCorrectionMode(StrEnum):
    REGENERATE_WITH_MANAGED_DEPENDENCIES = (
        "REGENERATE_WITH_MANAGED_DEPENDENCIES"
    )
    REGENERATE_COMPILABLE_LATEX = "REGENERATE_COMPILABLE_LATEX"


class LatexCompilationCorrectionStatus(StrEnum):
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
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    INVALID_ACTION = "INVALID_ACTION"
    FAILED = "FAILED"


class LatexCompilationCorrectionWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


_MODE_BY_REASON = {
    LatexCompilationStopReason.UNMANAGED_DEPENDENCY: (
        LatexCompilationCorrectionMode
        .REGENERATE_WITH_MANAGED_DEPENDENCIES
    ),
    LatexCompilationStopReason.COMPILATION_ERROR: (
        LatexCompilationCorrectionMode.REGENERATE_COMPILABLE_LATEX
    ),
}


@dataclass(frozen=True, slots=True)
class LatexCompilationCorrectionCommand:
    subject_id: str
    attention_item_id: str
    action: LatexCompilationCorrectionAction
    now: datetime


@dataclass(frozen=True, slots=True)
class LatexCompilationCorrectionConstraint:
    directive_id: str
    directive_hash: str
    mode: LatexCompilationCorrectionMode
    failed_construction_result_id: str
    failed_latex_version_id: str
    failed_source_content_hash: str
    compilation_attempt_id: str
    original_reason: LatexCompilationStopReason

    def __post_init__(self) -> None:
        _text("directive_id", self.directive_id, 240)
        _hash("directive_hash", self.directive_hash)
        object.__setattr__(
            self, "mode", LatexCompilationCorrectionMode(self.mode)
        )
        object.__setattr__(
            self, "original_reason", LatexCompilationStopReason(
                self.original_reason
            )
        )
        for name, value in (
            (
                "failed_construction_result_id",
                self.failed_construction_result_id,
            ),
            ("failed_latex_version_id", self.failed_latex_version_id),
            ("compilation_attempt_id", self.compilation_attempt_id),
        ):
            _text(name, value, 240)
        _hash(
            "failed_source_content_hash", self.failed_source_content_hash
        )
        if _MODE_BY_REASON.get(self.original_reason) is not self.mode:
            raise ValueError("Compilation correction mode is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "compilation_attempt_id": self.compilation_attempt_id,
            "directive_hash": self.directive_hash,
            "directive_id": self.directive_id,
            "failed_construction_result_id": (
                self.failed_construction_result_id
            ),
            "failed_latex_version_id": self.failed_latex_version_id,
            "failed_source_content_hash": (
                self.failed_source_content_hash
            ),
            "mode": self.mode.value,
            "original_reason": self.original_reason.value,
        }


@dataclass(frozen=True, slots=True)
class LatexCompilationCorrectionDirective:
    directive_id: str
    directive_version: int
    directive_hash: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    correction_target_ref: MaterialCorrectionTargetRef
    stopped_source_ref: ResumeCompilationStoppedSourceRef
    failed_construction_result_id: str
    failed_latex_version_id: str
    failed_source_content_hash: str
    compilation_attempt_id: str
    original_reason: LatexCompilationStopReason
    action: LatexCompilationCorrectionAction
    mode: LatexCompilationCorrectionMode
    preparation_invocation_ref: PreparationInvocationBindingRef
    previous_directive_id: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != LATEX_COMPILATION_CORRECTION_DIRECTIVE_VERSION
        ):
            raise ValueError("Compilation correction contract is unsupported")
        if (
            not isinstance(self.directive_id, str)
            or _DIRECTIVE_ID_RE.fullmatch(self.directive_id) is None
        ):
            raise ValueError("Compilation correction directive ID is invalid")
        if type(self.directive_version) is not int or self.directive_version < 1:
            raise ValueError("directive_version must be positive")
        _hash("directive_hash", self.directive_hash)
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            ("attention_item_id", self.attention_item_id, 240),
            (
                "failed_construction_result_id",
                self.failed_construction_result_id,
                240,
            ),
            ("failed_latex_version_id", self.failed_latex_version_id, 240),
            ("compilation_attempt_id", self.compilation_attempt_id, 240),
        ):
            _text(name, value, maximum)
        _hash(
            "failed_source_content_hash", self.failed_source_content_hash
        )
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ):
            raise TypeError("correction target reference must be typed")
        if not isinstance(
            self.stopped_source_ref, ResumeCompilationStoppedSourceRef
        ):
            raise TypeError("stopped-source reference must be typed")
        if not isinstance(
            self.preparation_invocation_ref,
            PreparationInvocationBindingRef,
        ):
            raise TypeError("preparation invocation reference must be typed")
        reason = LatexCompilationStopReason(self.original_reason)
        action = LatexCompilationCorrectionAction(self.action)
        mode = LatexCompilationCorrectionMode(self.mode)
        object.__setattr__(self, "original_reason", reason)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "mode", mode)
        if action is not LatexCompilationCorrectionAction.REGENERATE_AND_RETRY:
            raise ValueError("Compilation correction action is invalid")
        if _MODE_BY_REASON.get(reason) is not mode:
            raise ValueError("Compilation correction mode does not match reason")
        if self.previous_directive_id is not None:
            _text("previous_directive_id", self.previous_directive_id, 240)
        _aware("created_at", self.created_at)
        expected = _canonical_hash(self.identity_dict())
        if (
            self.directive_hash != expected
            or self.directive_id
            != f"latex-compilation-correction-{expected}"
        ):
            raise ValueError("Compilation correction identity is invalid")

    @property
    def constraint(self) -> LatexCompilationCorrectionConstraint:
        return LatexCompilationCorrectionConstraint(
            directive_id=self.directive_id,
            directive_hash=self.directive_hash,
            mode=self.mode,
            failed_construction_result_id=(
                self.failed_construction_result_id
            ),
            failed_latex_version_id=self.failed_latex_version_id,
            failed_source_content_hash=self.failed_source_content_hash,
            compilation_attempt_id=self.compilation_attempt_id,
            original_reason=self.original_reason,
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "compilation_attempt_id": self.compilation_attempt_id,
            "contract_version": self.contract_version,
            "correction_target_ref": self.correction_target_ref.to_dict(),
            "directive_version": self.directive_version,
            "failed_construction_result_id": (
                self.failed_construction_result_id
            ),
            "failed_latex_version_id": self.failed_latex_version_id,
            "failed_source_content_hash": (
                self.failed_source_content_hash
            ),
            "mode": self.mode.value,
            "original_reason": self.original_reason.value,
            "preparation_invocation_ref": (
                self.preparation_invocation_ref.to_dict()
            ),
            "previous_directive_id": self.previous_directive_id,
            "stopped_source_ref": self.stopped_source_ref.to_dict(),
            "subject_id": self.subject_id,
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
        preparation_invocation_ref: PreparationInvocationBindingRef,
        previous: "LatexCompilationCorrectionDirective | None",
        created_at: datetime,
    ) -> "LatexCompilationCorrectionDirective":
        payload = target.payload
        if (
            not isinstance(payload, LatexCompilationCorrectionTarget)
            or target.reference != item.correction_target_ref
            or target.subject_id != item.subject_id
            or target.application_plan_id != item.application_plan_id
            or target.attention_item_id != item.item_id
        ):
            raise ValueError("Compilation correction target binding is invalid")
        mode = _MODE_BY_REASON.get(payload.compilation_reason)
        if mode is None:
            raise ValueError("Compilation target is not correctable")
        version = previous.directive_version + 1 if previous else 1
        identity = {
            "action": (
                LatexCompilationCorrectionAction
                .REGENERATE_AND_RETRY.value
            ),
            "application_plan_id": item.application_plan_id,
            "attention_item_id": item.item_id,
            "compilation_attempt_id": payload.compilation_attempt_id,
            "contract_version": (
                LATEX_COMPILATION_CORRECTION_DIRECTIVE_VERSION
            ),
            "correction_target_ref": target.reference.to_dict(),
            "directive_version": version,
            "failed_construction_result_id": (
                payload.construction_result_id
            ),
            "failed_latex_version_id": payload.latex_version_id,
            "failed_source_content_hash": payload.source_content_hash,
            "mode": mode.value,
            "original_reason": payload.compilation_reason.value,
            "preparation_invocation_ref": (
                preparation_invocation_ref.to_dict()
            ),
            "previous_directive_id": (
                previous.directive_id if previous else None
            ),
            "stopped_source_ref": payload.stopped_source_ref.to_dict(),
            "subject_id": item.subject_id,
        }
        digest = _canonical_hash(identity)
        return cls(
            directive_id=f"latex-compilation-correction-{digest}",
            directive_version=version,
            directive_hash=digest,
            contract_version=(
                LATEX_COMPILATION_CORRECTION_DIRECTIVE_VERSION
            ),
            subject_id=item.subject_id,
            application_plan_id=item.application_plan_id,
            attention_item_id=item.item_id,
            correction_target_ref=target.reference,
            stopped_source_ref=payload.stopped_source_ref,
            failed_construction_result_id=payload.construction_result_id,
            failed_latex_version_id=payload.latex_version_id,
            failed_source_content_hash=payload.source_content_hash,
            compilation_attempt_id=payload.compilation_attempt_id,
            original_reason=payload.compilation_reason,
            action=(
                LatexCompilationCorrectionAction.REGENERATE_AND_RETRY
            ),
            mode=mode,
            preparation_invocation_ref=preparation_invocation_ref,
            previous_directive_id=(
                previous.directive_id if previous else None
            ),
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class LatexCompilationCorrectionWriteResult:
    status: LatexCompilationCorrectionWriteStatus
    directive: LatexCompilationCorrectionDirective | None


@runtime_checkable
class LatexCompilationCorrectionDirectiveRepository(Protocol):
    def save(
        self, directive: LatexCompilationCorrectionDirective
    ) -> LatexCompilationCorrectionWriteResult: ...

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> LatexCompilationCorrectionDirective | None: ...


class PrivateHomeLatexCompilationCorrectionDirectiveRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        subject = _text("subject_id", subject_id, 160)
        return (
            self._home.paths.preparation
            / "latex-compilation-correction-directives"
            / ("subject-" + hashlib.sha256(subject.encode()).hexdigest())
        )

    def save(
        self, directive: LatexCompilationCorrectionDirective
    ) -> LatexCompilationCorrectionWriteResult:
        if not isinstance(directive, LatexCompilationCorrectionDirective):
            raise TypeError("directive must be typed")
        path = self._directory(directive.subject_id) / (
            directive.directive_id + ".json"
        )
        content = _json(directive.to_dict())
        try:
            created = self._home.write_bytes_if_absent(path, content)
            if created:
                return LatexCompilationCorrectionWriteResult(
                    LatexCompilationCorrectionWriteStatus.CREATED, directive
                )
            existing = _directive_from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if existing.identity_dict() == directive.identity_dict():
                return LatexCompilationCorrectionWriteResult(
                    LatexCompilationCorrectionWriteStatus.UNCHANGED,
                    existing,
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return LatexCompilationCorrectionWriteResult(
            LatexCompilationCorrectionWriteStatus.FAILED, None
        )

    def _list(
        self, subject_id: str
    ) -> tuple[LatexCompilationCorrectionDirective, ...]:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return ()
        values = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            contained = self._home.contained_path(path)
            if contained.is_symlink() or not contained.is_file():
                raise ValueError("directive path is unsafe")
            directive = _directive_from_dict(
                json.loads(contained.read_text(encoding="utf-8"))
            )
            if directive.subject_id != subject_id:
                raise ValueError("directive subject binding is invalid")
            values.append(directive)
        return tuple(values)

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> LatexCompilationCorrectionDirective | None:
        matches = tuple(
            item
            for item in self._list(subject_id)
            if item.application_plan_id == application_plan_id
        )
        if not matches:
            return None
        ordered = tuple(
            sorted(matches, key=lambda item: item.directive_version)
        )
        if tuple(item.directive_version for item in ordered) != tuple(
            range(1, len(ordered) + 1)
        ) or any(
            item.previous_directive_id
            != (ordered[index - 1].directive_id if index else None)
            for index, item in enumerate(ordered)
        ):
            raise ValueError("Compilation correction history is invalid")
        return ordered[-1]


@dataclass(frozen=True, slots=True)
class LatexCompilationCorrectionDirectiveReadResult:
    succeeded: bool
    directive: LatexCompilationCorrectionDirective | None


class LatexCompilationCorrectionDirectiveProvider:
    def __init__(
        self, repository: LatexCompilationCorrectionDirectiveRepository
    ) -> None:
        self._repository = repository

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> LatexCompilationCorrectionDirectiveReadResult:
        try:
            directive = self._repository.get_current(
                subject_id=subject_id,
                application_plan_id=application_plan_id,
            )
            if directive is not None and (
                directive.subject_id != subject_id
                or directive.application_plan_id != application_plan_id
            ):
                raise ValueError("Compilation correction binding drifted")
            return LatexCompilationCorrectionDirectiveReadResult(
                True, directive
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return LatexCompilationCorrectionDirectiveReadResult(False, None)


@dataclass(frozen=True, slots=True)
class LatexCompilationCorrectionReceipt:
    receipt_id: str
    receipt_hash: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    directive_id: str
    directive_version: int
    correction_target_ref: MaterialCorrectionTargetRef
    stopped_source_ref: ResumeCompilationStoppedSourceRef
    mode: LatexCompilationCorrectionMode
    preparation_run_id: str | None
    preparation_status: str
    failure_reason: str | None
    completed_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != LATEX_COMPILATION_CORRECTION_RECEIPT_VERSION
        ):
            raise ValueError("Compilation correction receipt is unsupported")
        _hash("receipt_hash", self.receipt_hash)
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            ("attention_item_id", self.attention_item_id, 240),
            ("directive_id", self.directive_id, 240),
            ("preparation_status", self.preparation_status, 80),
        ):
            _text(name, value, maximum)
        if type(self.directive_version) is not int or self.directive_version < 1:
            raise ValueError("receipt directive_version must be positive")
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ) or not isinstance(
            self.stopped_source_ref, ResumeCompilationStoppedSourceRef
        ):
            raise TypeError("receipt source references must be typed")
        if self.preparation_run_id is not None:
            _text("preparation_run_id", self.preparation_run_id, 240)
        if self.failure_reason is not None:
            _text("failure_reason", self.failure_reason, 200)
        _aware("completed_at", self.completed_at)
        object.__setattr__(
            self, "mode", LatexCompilationCorrectionMode(self.mode)
        )
        expected = _canonical_hash(self.identity_dict())
        if (
            self.receipt_hash != expected
            or self.receipt_id
            != f"latex-compilation-correction-receipt-{expected}"
        ):
            raise ValueError("Compilation correction receipt is invalid")

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
            "stopped_source_ref": self.stopped_source_ref.to_dict(),
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
        directive: LatexCompilationCorrectionDirective,
        preparation_run_id: str | None,
        preparation_status: str,
        failure_reason: str | None,
        completed_at: datetime,
    ) -> "LatexCompilationCorrectionReceipt":
        identity = {
            "application_plan_id": directive.application_plan_id,
            "attention_item_id": directive.attention_item_id,
            "contract_version": (
                LATEX_COMPILATION_CORRECTION_RECEIPT_VERSION
            ),
            "correction_target_ref": (
                directive.correction_target_ref.to_dict()
            ),
            "directive_id": directive.directive_id,
            "directive_version": directive.directive_version,
            "failure_reason": failure_reason,
            "mode": directive.mode.value,
            "preparation_run_id": preparation_run_id,
            "preparation_status": preparation_status,
            "stopped_source_ref": directive.stopped_source_ref.to_dict(),
            "subject_id": directive.subject_id,
        }
        digest = _canonical_hash(identity)
        return cls(
            receipt_id=f"latex-compilation-correction-receipt-{digest}",
            receipt_hash=digest,
            contract_version=(
                LATEX_COMPILATION_CORRECTION_RECEIPT_VERSION
            ),
            subject_id=directive.subject_id,
            application_plan_id=directive.application_plan_id,
            attention_item_id=directive.attention_item_id,
            directive_id=directive.directive_id,
            directive_version=directive.directive_version,
            correction_target_ref=directive.correction_target_ref,
            stopped_source_ref=directive.stopped_source_ref,
            mode=directive.mode,
            preparation_run_id=preparation_run_id,
            preparation_status=preparation_status,
            failure_reason=failure_reason,
            completed_at=completed_at,
        )


class LatexCompilationCorrectionReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        subject = _text("subject_id", subject_id, 160)
        return (
            self._home.paths.preparation
            / "latex-compilation-correction-receipts"
            / ("subject-" + hashlib.sha256(subject.encode()).hexdigest())
        )

    def save(self, receipt: LatexCompilationCorrectionReceipt) -> None:
        path = self._directory(receipt.subject_id) / (
            receipt.receipt_id + ".json"
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable Compilation correction receipt conflict")

    def has_successful_replay(
        self, *, subject_id: str, attention_item_id: str
    ) -> bool:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return False
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            contained = self._home.contained_path(path)
            if contained.is_symlink() or not contained.is_file():
                raise ValueError("Compilation correction receipt path is unsafe")
            receipt = _receipt_from_dict(
                json.loads(contained.read_text(encoding="utf-8"))
            )
            if (
                receipt.subject_id == subject_id
                and receipt.attention_item_id == attention_item_id
                and receipt.preparation_status
                in {
                    ApplicationPreparationStatus.COMPLETED.value,
                    ApplicationPreparationStatus.UNCHANGED.value,
                    ApplicationPreparationStatus.DEFERRED.value,
                }
            ):
                return True
        return False


@dataclass(frozen=True, slots=True)
class LatexCompilationCorrectionResult:
    status: LatexCompilationCorrectionStatus
    receipt: LatexCompilationCorrectionReceipt | None
    reason_code: str | None
    message: str


QueueReader = Callable[
    ...,
    HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult],
]
PreparationCallable = Callable[
    ...,
    RunApplicationPreparationResult
    | Awaitable[RunApplicationPreparationResult],
]


async def resolve_latex_compilation_correction(
    command: LatexCompilationCorrectionCommand,
    *,
    queue_reader: QueueReader,
    target_provider: MaterialCorrectionTargetProvider,
    directive_repository: LatexCompilationCorrectionDirectiveRepository,
    preparation_callable: PreparationCallable,
    receipt_repository: LatexCompilationCorrectionReceiptRepository,
) -> LatexCompilationCorrectionResult:
    try:
        subject = _text("subject_id", command.subject_id, 160)
        item_id = _text("attention_item_id", command.attention_item_id, 240)
        action = LatexCompilationCorrectionAction(command.action)
        now = _aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _result(
            LatexCompilationCorrectionStatus.INVALID_ACTION,
            "INVALID_ACTION",
            "The Compilation correction action is invalid.",
        )
    if action is not LatexCompilationCorrectionAction.REGENERATE_AND_RETRY:
        return _result(
            LatexCompilationCorrectionStatus.INVALID_ACTION,
            "INVALID_ACTION",
            "The Compilation correction action is invalid.",
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
            (
                candidate
                for candidate in queue.items
                if candidate.item_id == item_id
            ),
            None,
        )
        if item is None:
            if receipt_repository.has_successful_replay(
                subject_id=subject, attention_item_id=item_id
            ):
                return _result(
                    LatexCompilationCorrectionStatus.UNCHANGED,
                    None,
                    "This Compilation correction is unchanged.",
                )
            return _result(
                LatexCompilationCorrectionStatus.ITEM_NOT_CURRENT,
                "ITEM_NOT_CURRENT",
                "The Compilation correction item is no longer current.",
            )
        if (
            item.subject_id != subject
            or item.audience is not HumanAttentionAudience.USER
            or item.resolution_capability
            is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
            or item.correction_target_ref is None
        ):
            return _result(
                LatexCompilationCorrectionStatus.UNSUPPORTED_TARGET,
                "UNSUPPORTED_TARGET",
                "This item requires a different correction path.",
            )
        typed = target_provider.get_current_typed_target(item=item)
        if typed.status is MaterialCorrectionTargetStatus.TARGET_STALE:
            return _result(
                LatexCompilationCorrectionStatus.TARGET_STALE,
                "TARGET_STALE",
                "The Compilation correction target is stale.",
            )
        if (
            typed.status is not MaterialCorrectionTargetStatus.AVAILABLE
            or typed.target is None
        ):
            return _result(
                LatexCompilationCorrectionStatus.TARGET_UNAVAILABLE,
                typed.status.value,
                "The Compilation correction target is unavailable.",
            )
        target = typed.target
        payload = target.payload
        if not isinstance(payload, LatexCompilationCorrectionTarget):
            return _result(
                LatexCompilationCorrectionStatus.UNSUPPORTED_TARGET,
                "UNSUPPORTED_TARGET",
                "This target is not a Compilation source.",
            )
        try:
            read = target_provider.get_compilation_stopped_source_for_target(
                target=target
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _result(
                LatexCompilationCorrectionStatus.TARGET_STALE,
                "TARGET_STALE",
                "The stopped Compilation source has changed.",
            )
        if (
            read.status
            is not ResumeCompilationStoppedSourceReadStatus.FOUND
            or read.record is None
            or not isinstance(
                read.record.source_resolution_lineage,
                ResolvedCompilationSourceLineage,
            )
        ):
            return _result(
                LatexCompilationCorrectionStatus.TARGET_STALE,
                "TARGET_STALE",
                "The stopped Compilation source has changed.",
            )
        record = read.record
        lineage = record.source_resolution_lineage
        if (
            record.subject_id != subject
            or record.application_plan_id != item.application_plan_id
            or record.reference != payload.stopped_source_ref
            or record.compilation_attempt_id
            != payload.compilation_attempt_id
            or lineage.construction_result_id
            != payload.construction_result_id
            or lineage.latex_version_id != payload.latex_version_id
            or lineage.source_content_hash != payload.source_content_hash
            or record.stop_reason.code is not payload.compilation_reason
            or payload.compilation_reason not in _MODE_BY_REASON
        ):
            return _result(
                LatexCompilationCorrectionStatus.TARGET_STALE,
                "TARGET_STALE",
                "The Compilation source identity has changed.",
            )
        previous = directive_repository.get_current(
            subject_id=subject,
            application_plan_id=item.application_plan_id,
        )
        mode = _MODE_BY_REASON[payload.compilation_reason]
        if (
            previous is not None
            and previous.correction_target_ref == target.reference
            and previous.action is action
            and previous.mode is mode
        ):
            return _result(
                LatexCompilationCorrectionStatus.UNCHANGED,
                None,
                "This Compilation correction is unchanged.",
            )
        directive = LatexCompilationCorrectionDirective.create(
            item=item,
            target=target,
            preparation_invocation_ref=record.preparation_invocation_ref,
            previous=previous,
            created_at=now,
        )
        write = directive_repository.save(directive)
        if (
            write.status is LatexCompilationCorrectionWriteStatus.FAILED
            or write.directive is None
        ):
            raise ValueError("Compilation correction persistence failed")
        if write.status is LatexCompilationCorrectionWriteStatus.UNCHANGED:
            return _result(
                LatexCompilationCorrectionStatus.UNCHANGED,
                None,
                "This Compilation correction is unchanged.",
            )
        try:
            preparation = await _resolve(
                preparation_callable(
                    RunApplicationPreparationCommand(
                        subject_id=subject,
                        application_plan_id=item.application_plan_id,
                        now=now,
                    )
                )
            )
            if not isinstance(preparation, RunApplicationPreparationResult):
                raise ValueError("preparation result is invalid")
            preparation_status = preparation.status
            run_id = preparation.run.run_id if preparation.run else None
            failure_reason = (
                preparation.reason_code.value
                if preparation.reason_code is not None
                else None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            preparation_status = ApplicationPreparationStatus.FAILED
            run_id = None
            failure_reason = "PREPARATION_RERUN_FAILED"
        receipt = LatexCompilationCorrectionReceipt.create(
            directive=write.directive,
            preparation_run_id=run_id,
            preparation_status=preparation_status.value,
            failure_reason=failure_reason,
            completed_at=now,
        )
        receipt_repository.save(receipt)
        if preparation_status in {
            ApplicationPreparationStatus.COMPLETED,
            ApplicationPreparationStatus.UNCHANGED,
        }:
            status = (
                LatexCompilationCorrectionStatus
                .CORRECTED_AND_PREPARATION_COMPLETED
            )
        elif preparation_status is ApplicationPreparationStatus.DEFERRED:
            status = (
                LatexCompilationCorrectionStatus
                .CORRECTED_AND_PREPARATION_DEFERRED
            )
        else:
            status = (
                LatexCompilationCorrectionStatus
                .CORRECTION_RECORDED_PREPARATION_FAILED
            )
        return LatexCompilationCorrectionResult(
            status,
            receipt,
            failure_reason,
            "The Compilation correction was recorded and preparation reran.",
        )
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return _result(
            LatexCompilationCorrectionStatus.FAILED,
            "CORRECTION_FAILED",
            "The Compilation correction could not be recorded safely.",
        )


def _directive_from_dict(
    value: Mapping[str, Any],
) -> LatexCompilationCorrectionDirective:
    return LatexCompilationCorrectionDirective(
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
        stopped_source_ref=ResumeCompilationStoppedSourceRef.from_dict(
            value["stopped_source_ref"]
        ),
        failed_construction_result_id=value[
            "failed_construction_result_id"
        ],
        failed_latex_version_id=value["failed_latex_version_id"],
        failed_source_content_hash=value["failed_source_content_hash"],
        compilation_attempt_id=value["compilation_attempt_id"],
        original_reason=value["original_reason"],
        action=value["action"],
        mode=value["mode"],
        preparation_invocation_ref=(
            PreparationInvocationBindingRef.from_dict(
                value["preparation_invocation_ref"]
            )
        ),
        previous_directive_id=value["previous_directive_id"],
        created_at=datetime.fromisoformat(
            value["created_at"].replace("Z", "+00:00")
        ),
    )


def _receipt_from_dict(
    value: Mapping[str, Any],
) -> LatexCompilationCorrectionReceipt:
    return LatexCompilationCorrectionReceipt(
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
        stopped_source_ref=ResumeCompilationStoppedSourceRef.from_dict(
            value["stopped_source_ref"]
        ),
        mode=value["mode"],
        preparation_run_id=value["preparation_run_id"],
        preparation_status=value["preparation_status"],
        failure_reason=value["failure_reason"],
        completed_at=datetime.fromisoformat(
            value["completed_at"].replace("Z", "+00:00")
        ),
    )


def _result(status, reason, message):
    return LatexCompilationCorrectionResult(status, None, reason, message)


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
    "LATEX_COMPILATION_CORRECTION_DIRECTIVE_VERSION",
    "LATEX_COMPILATION_CORRECTION_RECEIPT_VERSION",
    "LatexCompilationCorrectionAction",
    "LatexCompilationCorrectionCommand",
    "LatexCompilationCorrectionConstraint",
    "LatexCompilationCorrectionDirective",
    "LatexCompilationCorrectionDirectiveProvider",
    "LatexCompilationCorrectionDirectiveReadResult",
    "LatexCompilationCorrectionDirectiveRepository",
    "LatexCompilationCorrectionMode",
    "LatexCompilationCorrectionReceipt",
    "LatexCompilationCorrectionReceiptRepository",
    "LatexCompilationCorrectionResult",
    "LatexCompilationCorrectionStatus",
    "LatexCompilationCorrectionWriteResult",
    "LatexCompilationCorrectionWriteStatus",
    "PrivateHomeLatexCompilationCorrectionDirectiveRepository",
    "resolve_latex_compilation_correction",
]
