"""Explicit, preview-scoped Cover Letter overflow correction resolution."""

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
    ApplicationPreparationStage,
    ApplicationPreparationStatus,
    RunApplicationPreparationCommand,
    RunApplicationPreparationResult,
)
from .cover_letter_overflow_preview import (
    CoverLetterOverflowCorrectionPreview,
    CoverLetterOverflowCorrectionPreviewRef,
    CoverLetterOverflowPreviewProvider,
    CoverLetterOverflowPreviewStatus,
)
from .human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
    HumanAttentionResolutionCapability,
)
from .material_correction_ref import MaterialCorrectionTargetRef
from .material_correction_target import (
    CoverLetterLayoutCorrectionTarget,
    MaterialCorrectionTarget,
    MaterialCorrectionTargetProvider,
    MaterialCorrectionTargetStatus,
)
from .prepared_cover_letter_material import (
    CoverLetterOverflowCorrectionConstraint,
    CoverLetterOverflowCorrectionConstraintReadResult,
    CoverLetterOverflowCorrectionConstraintStatus,
)
from .private_home import PrivateHome


COVER_LETTER_OVERFLOW_CORRECTION_DIRECTIVE_VERSION = (
    "cover-letter-overflow-correction-directive-v1"
)
COVER_LETTER_OVERFLOW_CORRECTION_RECEIPT_VERSION = (
    "cover-letter-overflow-correction-receipt-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_DIRECTIVE_ID_RE = re.compile(
    r"^cover-letter-overflow-correction-[a-f0-9]{64}$"
)


class CoverLetterOverflowCorrectionAction(StrEnum):
    REFORMAT_AND_RETRY = "REFORMAT_AND_RETRY"


class CoverLetterOverflowCorrectionMode(StrEnum):
    REFORMAT_EXISTING_CONTENT = "REFORMAT_EXISTING_CONTENT"


class CoverLetterOverflowCorrectionStatus(StrEnum):
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
    CONTENT_PRESERVATION_FAILED = "CONTENT_PRESERVATION_FAILED"
    FAILED = "FAILED"


class CoverLetterOverflowCorrectionWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is too long")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 hash")
    return value


def _aware(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    ).encode()


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionCommand:
    subject_id: str
    attention_item_id: str
    action: CoverLetterOverflowCorrectionAction
    now: datetime


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionDirective:
    directive_id: str
    directive_version: int
    directive_hash: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    correction_target_ref: MaterialCorrectionTargetRef
    safe_preview_ref: CoverLetterOverflowCorrectionPreviewRef
    publication_result_id: str
    overflow_evaluation_id: str
    overflow_evaluation_version: str
    source_record_id: str
    source_version: str
    source_content_hash: str
    action: CoverLetterOverflowCorrectionAction
    mode: CoverLetterOverflowCorrectionMode
    previous_directive_id: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != COVER_LETTER_OVERFLOW_CORRECTION_DIRECTIVE_VERSION
        ):
            raise ValueError("Cover Letter correction contract is unsupported")
        if (
            _DIRECTIVE_ID_RE.fullmatch(self.directive_id) is None
            or type(self.directive_version) is not int
            or self.directive_version < 1
        ):
            raise ValueError("Cover Letter directive identity is invalid")
        _hash("directive_hash", self.directive_hash)
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            ("attention_item_id", self.attention_item_id, 240),
            ("publication_result_id", self.publication_result_id, 300),
            ("overflow_evaluation_id", self.overflow_evaluation_id, 300),
            ("overflow_evaluation_version", self.overflow_evaluation_version, 120),
            ("source_record_id", self.source_record_id, 300),
            ("source_version", self.source_version, 160),
        ):
            _text(name, value, maximum)
        _hash("source_content_hash", self.source_content_hash)
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ) or not isinstance(
            self.safe_preview_ref,
            CoverLetterOverflowCorrectionPreviewRef,
        ):
            raise TypeError("Cover Letter correction references must be typed")
        object.__setattr__(
            self, "action", CoverLetterOverflowCorrectionAction(self.action)
        )
        object.__setattr__(
            self, "mode", CoverLetterOverflowCorrectionMode(self.mode)
        )
        if (
            self.action
            is not CoverLetterOverflowCorrectionAction.REFORMAT_AND_RETRY
            or self.mode
            is not CoverLetterOverflowCorrectionMode
            .REFORMAT_EXISTING_CONTENT
            or self.source_record_id
            != f"cover-letter-latex-source-{self.source_content_hash}"
        ):
            raise ValueError("Cover Letter correction directive is invalid")
        if self.previous_directive_id is not None:
            _text("previous_directive_id", self.previous_directive_id, 240)
        _aware("created_at", self.created_at)
        digest = _canonical_hash(self.identity_dict())
        if (
            self.directive_hash != digest
            or self.directive_id
            != f"cover-letter-overflow-correction-{digest}"
        ):
            raise ValueError("Cover Letter directive hash is invalid")

    @property
    def constraint(self) -> CoverLetterOverflowCorrectionConstraint:
        return CoverLetterOverflowCorrectionConstraint(
            directive_id=self.directive_id,
            directive_version=self.directive_version,
            directive_hash=self.directive_hash,
            subject_id=self.subject_id,
            application_plan_id=self.application_plan_id,
            correction_target_id=self.correction_target_ref.target_id,
            correction_target_hash=self.correction_target_ref.target_hash,
            safe_preview_id=self.safe_preview_ref.preview_id,
            safe_preview_hash=self.safe_preview_ref.preview_hash,
            publication_result_id=self.publication_result_id,
            overflow_evaluation_id=self.overflow_evaluation_id,
            overflow_evaluation_version=self.overflow_evaluation_version,
            source_record_id=self.source_record_id,
            source_version=self.source_version,
            source_content_hash=self.source_content_hash,
            correction_mode=self.mode.value,
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
            "overflow_evaluation_id": self.overflow_evaluation_id,
            "overflow_evaluation_version": self.overflow_evaluation_version,
            "previous_directive_id": self.previous_directive_id,
            "publication_result_id": self.publication_result_id,
            "safe_preview_ref": self.safe_preview_ref.to_dict(),
            "source_content_hash": self.source_content_hash,
            "source_record_id": self.source_record_id,
            "source_version": self.source_version,
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
        item: Any,
        target: MaterialCorrectionTarget,
        preview: CoverLetterOverflowCorrectionPreview,
        action: CoverLetterOverflowCorrectionAction,
        previous: "CoverLetterOverflowCorrectionDirective | None",
        created_at: datetime,
    ) -> "CoverLetterOverflowCorrectionDirective":
        payload = target.payload
        if (
            not isinstance(payload, CoverLetterLayoutCorrectionTarget)
            or target.reference != item.correction_target_ref
            or preview.correction_target_ref != target.reference
            or preview.subject_id != target.subject_id
            or preview.application_plan_id != target.application_plan_id
            or preview.preparation_run_id != target.preparation_run_id
            or preview.attention_item_id != item.item_id
            or preview.publication_result_id != payload.publication_result_id
            or preview.overflow_evaluation_id
            != payload.overflow_evaluation_id
            or preview.source_record_id != payload.latex_source_id
            or preview.source_version != payload.source_version
            or preview.source_content_hash != payload.source_content_hash
        ):
            raise ValueError("Cover Letter target/preview binding is invalid")
        version = previous.directive_version + 1 if previous else 1
        prototype = {
            "action": action.value,
            "application_plan_id": item.application_plan_id,
            "attention_item_id": item.item_id,
            "contract_version": (
                COVER_LETTER_OVERFLOW_CORRECTION_DIRECTIVE_VERSION
            ),
            "correction_target_ref": target.reference.to_dict(),
            "directive_version": version,
            "mode": (
                CoverLetterOverflowCorrectionMode
                .REFORMAT_EXISTING_CONTENT.value
            ),
            "overflow_evaluation_id": payload.overflow_evaluation_id,
            "overflow_evaluation_version": (
                preview.overflow_evaluation_version
            ),
            "previous_directive_id": (
                previous.directive_id if previous else None
            ),
            "publication_result_id": payload.publication_result_id,
            "safe_preview_ref": preview.reference.to_dict(),
            "source_content_hash": payload.source_content_hash,
            "source_record_id": payload.latex_source_id,
            "source_version": payload.source_version,
            "subject_id": item.subject_id,
        }
        digest = _canonical_hash(prototype)
        return cls(
            directive_id=f"cover-letter-overflow-correction-{digest}",
            directive_version=version,
            directive_hash=digest,
            contract_version=(
                COVER_LETTER_OVERFLOW_CORRECTION_DIRECTIVE_VERSION
            ),
            subject_id=item.subject_id,
            application_plan_id=item.application_plan_id,
            attention_item_id=item.item_id,
            correction_target_ref=target.reference,
            safe_preview_ref=preview.reference,
            publication_result_id=payload.publication_result_id,
            overflow_evaluation_id=payload.overflow_evaluation_id,
            overflow_evaluation_version=preview.overflow_evaluation_version,
            source_record_id=payload.latex_source_id,
            source_version=payload.source_version,
            source_content_hash=payload.source_content_hash,
            action=action,
            mode=(
                CoverLetterOverflowCorrectionMode
                .REFORMAT_EXISTING_CONTENT
            ),
            previous_directive_id=(
                previous.directive_id if previous else None
            ),
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionWriteResult:
    status: CoverLetterOverflowCorrectionWriteStatus
    directive: CoverLetterOverflowCorrectionDirective | None


@runtime_checkable
class CoverLetterOverflowCorrectionDirectiveRepository(Protocol):
    def save(
        self, directive: CoverLetterOverflowCorrectionDirective
    ) -> CoverLetterOverflowCorrectionWriteResult: ...

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> CoverLetterOverflowCorrectionDirective | None: ...


class PrivateHomeCoverLetterOverflowCorrectionDirectiveRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        subject = _text("subject_id", subject_id, 160)
        return (
            self._home.paths.preparation
            / "cover-letter-overflow-correction-directives"
            / ("subject-" + hashlib.sha256(subject.encode()).hexdigest())
        )

    def save(
        self, directive: CoverLetterOverflowCorrectionDirective
    ) -> CoverLetterOverflowCorrectionWriteResult:
        if not isinstance(
            directive, CoverLetterOverflowCorrectionDirective
        ):
            raise TypeError("directive must be typed")
        path = self._directory(directive.subject_id) / (
            directive.directive_id + ".json"
        )
        content = _json(directive.to_dict())
        try:
            created = self._home.write_bytes_if_absent(path, content)
            if created:
                return CoverLetterOverflowCorrectionWriteResult(
                    CoverLetterOverflowCorrectionWriteStatus.CREATED,
                    directive,
                )
            existing = _directive_from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if existing.identity_dict() == directive.identity_dict():
                return CoverLetterOverflowCorrectionWriteResult(
                    CoverLetterOverflowCorrectionWriteStatus.UNCHANGED,
                    existing,
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return CoverLetterOverflowCorrectionWriteResult(
            CoverLetterOverflowCorrectionWriteStatus.FAILED, None
        )

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> CoverLetterOverflowCorrectionDirective | None:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return None
        values = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            contained = self._home.contained_path(path)
            if contained.is_symlink() or not contained.is_file():
                raise ValueError("Cover Letter directive path is unsafe")
            directive = _directive_from_dict(
                json.loads(contained.read_text(encoding="utf-8"))
            )
            if directive.subject_id != subject_id:
                raise ValueError("Cover Letter directive subject drifted")
            if directive.application_plan_id == application_plan_id:
                values.append(directive)
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
            raise ValueError("Cover Letter directive history is invalid")
        return ordered[-1]


class CoverLetterOverflowCorrectionDirectiveProvider:
    def __init__(
        self,
        repository: CoverLetterOverflowCorrectionDirectiveRepository,
    ) -> None:
        self._repository = repository

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> CoverLetterOverflowCorrectionConstraintReadResult:
        try:
            directive = self._repository.get_current(
                subject_id=subject_id,
                application_plan_id=application_plan_id,
            )
            if directive is None:
                return CoverLetterOverflowCorrectionConstraintReadResult(
                    CoverLetterOverflowCorrectionConstraintStatus.NOT_FOUND,
                    None,
                )
            if (
                directive.subject_id != subject_id
                or directive.application_plan_id != application_plan_id
            ):
                raise ValueError("Cover Letter directive binding drifted")
            return CoverLetterOverflowCorrectionConstraintReadResult(
                CoverLetterOverflowCorrectionConstraintStatus.FOUND,
                directive.constraint,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return CoverLetterOverflowCorrectionConstraintReadResult(
                CoverLetterOverflowCorrectionConstraintStatus
                .INTEGRITY_FAILURE,
                None,
            )


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionReceipt:
    receipt_id: str
    receipt_hash: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    directive_id: str
    directive_version: int
    correction_target_ref: MaterialCorrectionTargetRef
    safe_preview_ref: CoverLetterOverflowCorrectionPreviewRef
    mode: CoverLetterOverflowCorrectionMode
    source_record_id: str
    source_content_hash: str
    preparation_run_id: str | None
    preparation_status: str
    failure_reason: str | None
    completed_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != COVER_LETTER_OVERFLOW_CORRECTION_RECEIPT_VERSION
        ):
            raise ValueError("Cover Letter receipt contract is unsupported")
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            ("attention_item_id", self.attention_item_id, 240),
            ("directive_id", self.directive_id, 240),
            ("source_record_id", self.source_record_id, 300),
            ("preparation_status", self.preparation_status, 80),
        ):
            _text(name, value, maximum)
        if type(self.directive_version) is not int or self.directive_version < 1:
            raise ValueError("receipt directive version is invalid")
        _hash("source_content_hash", self.source_content_hash)
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ) or not isinstance(
            self.safe_preview_ref,
            CoverLetterOverflowCorrectionPreviewRef,
        ):
            raise TypeError("receipt references must be typed")
        object.__setattr__(
            self, "mode", CoverLetterOverflowCorrectionMode(self.mode)
        )
        if self.preparation_run_id is not None:
            _text("preparation_run_id", self.preparation_run_id, 240)
        if self.failure_reason is not None:
            _text("failure_reason", self.failure_reason, 200)
        _aware("completed_at", self.completed_at)
        digest = _canonical_hash(self.identity_dict())
        if (
            self.receipt_hash != digest
            or self.receipt_id
            != f"cover-letter-overflow-correction-receipt-{digest}"
        ):
            raise ValueError("Cover Letter receipt identity is invalid")

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
            "source_content_hash": self.source_content_hash,
            "source_record_id": self.source_record_id,
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
        directive: CoverLetterOverflowCorrectionDirective,
        preparation_run_id: str | None,
        preparation_status: str,
        failure_reason: str | None,
        completed_at: datetime,
    ) -> "CoverLetterOverflowCorrectionReceipt":
        prototype = {
            "application_plan_id": directive.application_plan_id,
            "attention_item_id": directive.attention_item_id,
            "contract_version": (
                COVER_LETTER_OVERFLOW_CORRECTION_RECEIPT_VERSION
            ),
            "correction_target_ref": directive.correction_target_ref.to_dict(),
            "directive_id": directive.directive_id,
            "directive_version": directive.directive_version,
            "failure_reason": failure_reason,
            "mode": directive.mode.value,
            "preparation_run_id": preparation_run_id,
            "preparation_status": preparation_status,
            "safe_preview_ref": directive.safe_preview_ref.to_dict(),
            "source_content_hash": directive.source_content_hash,
            "source_record_id": directive.source_record_id,
            "subject_id": directive.subject_id,
        }
        digest = _canonical_hash(prototype)
        return cls(
            receipt_id=(
                f"cover-letter-overflow-correction-receipt-{digest}"
            ),
            receipt_hash=digest,
            contract_version=(
                COVER_LETTER_OVERFLOW_CORRECTION_RECEIPT_VERSION
            ),
            subject_id=directive.subject_id,
            application_plan_id=directive.application_plan_id,
            attention_item_id=directive.attention_item_id,
            directive_id=directive.directive_id,
            directive_version=directive.directive_version,
            correction_target_ref=directive.correction_target_ref,
            safe_preview_ref=directive.safe_preview_ref,
            mode=directive.mode,
            source_record_id=directive.source_record_id,
            source_content_hash=directive.source_content_hash,
            preparation_run_id=preparation_run_id,
            preparation_status=preparation_status,
            failure_reason=failure_reason,
            completed_at=completed_at,
        )


class CoverLetterOverflowCorrectionReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        subject = _text("subject_id", subject_id, 160)
        return (
            self._home.paths.preparation
            / "cover-letter-overflow-correction-receipts"
            / ("subject-" + hashlib.sha256(subject.encode()).hexdigest())
        )

    def save(self, receipt: CoverLetterOverflowCorrectionReceipt) -> None:
        path = self._directory(receipt.subject_id) / (
            receipt.receipt_id + ".json"
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable Cover Letter receipt conflict")

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
class CoverLetterOverflowCorrectionResult:
    status: CoverLetterOverflowCorrectionStatus
    receipt: CoverLetterOverflowCorrectionReceipt | None
    reason_code: str | None
    message: str


QueueReader = Callable[..., Any | Awaitable[Any]]
PreparationCallable = Callable[..., Any | Awaitable[Any]]


async def resolve_cover_letter_overflow_correction(
    command: CoverLetterOverflowCorrectionCommand,
    *,
    queue_reader: QueueReader,
    target_provider: MaterialCorrectionTargetProvider,
    preview_provider: CoverLetterOverflowPreviewProvider,
    directive_repository: CoverLetterOverflowCorrectionDirectiveRepository,
    receipt_repository: CoverLetterOverflowCorrectionReceiptRepository,
    preparation_callable: PreparationCallable,
) -> CoverLetterOverflowCorrectionResult:
    try:
        subject = _text("subject_id", command.subject_id, 160)
        item_id = _text("attention_item_id", command.attention_item_id, 240)
        action = CoverLetterOverflowCorrectionAction(command.action)
        now = _aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _result(
            CoverLetterOverflowCorrectionStatus.INVALID_ACTION,
            "INVALID_ACTION",
            "The Cover Letter correction action is invalid.",
        )
    if action is not CoverLetterOverflowCorrectionAction.REFORMAT_AND_RETRY:
        return _result(
            CoverLetterOverflowCorrectionStatus.INVALID_ACTION,
            "INVALID_ACTION",
            "The Cover Letter correction action is invalid.",
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
                    CoverLetterOverflowCorrectionStatus.UNCHANGED,
                    None,
                    "This Cover Letter correction is unchanged.",
                )
            return _result(
                CoverLetterOverflowCorrectionStatus.ITEM_NOT_CURRENT,
                "ITEM_NOT_CURRENT",
                "The Cover Letter item is no longer current.",
            )
        if (
            item.subject_id != subject
            or item.audience is not HumanAttentionAudience.USER
            or item.resolution_capability
            is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
            or item.correction_target_ref is None
        ):
            return _result(
                CoverLetterOverflowCorrectionStatus.UNSUPPORTED_TARGET,
                "UNSUPPORTED_TARGET",
                "This item requires a different correction path.",
            )
        typed = target_provider.get_current_typed_target(item=item)
        if typed.status is MaterialCorrectionTargetStatus.TARGET_STALE:
            return _result(
                CoverLetterOverflowCorrectionStatus.TARGET_STALE,
                "TARGET_STALE",
                "The Cover Letter target is stale.",
            )
        if (
            typed.status is not MaterialCorrectionTargetStatus.AVAILABLE
            or typed.target is None
            or not isinstance(
                typed.target.payload, CoverLetterLayoutCorrectionTarget
            )
        ):
            return _result(
                CoverLetterOverflowCorrectionStatus.UNSUPPORTED_TARGET,
                "UNSUPPORTED_TARGET",
                "This target is not a Cover Letter layout target.",
            )
        target = typed.target
        preview_read = (
            preview_provider.get_current_cover_letter_overflow_preview(
                subject_id=subject,
                correction_target_ref=target.reference,
            )
        )
        if preview_read.status is CoverLetterOverflowPreviewStatus.SOURCE_UNAVAILABLE:
            return _result(
                CoverLetterOverflowCorrectionStatus.PREVIEW_UNAVAILABLE,
                "PREVIEW_UNAVAILABLE",
                "A current safe Cover Letter preview is required.",
            )
        if (
            preview_read.status is not CoverLetterOverflowPreviewStatus.AVAILABLE
            or preview_read.preview is None
        ):
            return _result(
                CoverLetterOverflowCorrectionStatus.PREVIEW_STALE,
                preview_read.status.value,
                "The Cover Letter preview is stale or invalid.",
            )
        preview = preview_read.preview
        payload = target.payload
        if (
            preview.correction_target_ref != target.reference
            or preview.subject_id != item.subject_id
            or preview.application_plan_id != item.application_plan_id
            or preview.preparation_run_id != item.source_preparation_run_id
            or preview.attention_item_id != item.item_id
            or preview.publication_result_id != payload.publication_result_id
            or preview.overflow_evaluation_id
            != payload.overflow_evaluation_id
            or preview.source_record_id != payload.latex_source_id
            or preview.source_version != payload.source_version
            or preview.source_content_hash != payload.source_content_hash
        ):
            return _result(
                CoverLetterOverflowCorrectionStatus.PREVIEW_STALE,
                "PREVIEW_STALE",
                "The Cover Letter preview no longer matches the target.",
            )
        previous = directive_repository.get_current(
            subject_id=subject,
            application_plan_id=item.application_plan_id,
        )
        if (
            previous is not None
            and previous.correction_target_ref == target.reference
            and previous.safe_preview_ref == preview.reference
            and previous.action is action
        ):
            return _result(
                CoverLetterOverflowCorrectionStatus.UNCHANGED,
                None,
                "This Cover Letter correction is unchanged.",
            )
        directive = CoverLetterOverflowCorrectionDirective.create(
            item=item,
            target=target,
            preview=preview,
            action=action,
            previous=previous,
            created_at=now,
        )
        write = directive_repository.save(directive)
        if (
            write.status is CoverLetterOverflowCorrectionWriteStatus.FAILED
            or write.directive is None
        ):
            raise ValueError("Cover Letter correction persistence failed")
        if write.status is CoverLetterOverflowCorrectionWriteStatus.UNCHANGED:
            return _result(
                CoverLetterOverflowCorrectionStatus.UNCHANGED,
                None,
                "This Cover Letter correction is unchanged.",
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
            failure = (
                preparation.reason_code.value
                if preparation.reason_code is not None
                else None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            preparation = None
            preparation_status = ApplicationPreparationStatus.FAILED
            run_id = None
            failure = "PREPARATION_RERUN_FAILED"
        receipt = CoverLetterOverflowCorrectionReceipt.create(
            directive=write.directive,
            preparation_run_id=run_id,
            preparation_status=preparation_status.value,
            failure_reason=failure,
            completed_at=now,
        )
        receipt_repository.save(receipt)
        if _content_preservation_failed(preparation):
            status = (
                CoverLetterOverflowCorrectionStatus
                .CONTENT_PRESERVATION_FAILED
            )
        else:
            status = {
                ApplicationPreparationStatus.COMPLETED: (
                    CoverLetterOverflowCorrectionStatus
                    .CORRECTED_AND_PREPARATION_COMPLETED
                ),
                ApplicationPreparationStatus.UNCHANGED: (
                    CoverLetterOverflowCorrectionStatus
                    .CORRECTED_AND_PREPARATION_COMPLETED
                ),
                ApplicationPreparationStatus.DEFERRED: (
                    CoverLetterOverflowCorrectionStatus
                    .CORRECTED_AND_PREPARATION_DEFERRED
                ),
                ApplicationPreparationStatus.FAILED: (
                    CoverLetterOverflowCorrectionStatus
                    .CORRECTION_RECORDED_PREPARATION_FAILED
                ),
            }[preparation_status]
        return CoverLetterOverflowCorrectionResult(
            status,
            receipt,
            failure,
            "The Cover Letter correction was recorded and preparation reran.",
        )
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
        return _result(
            CoverLetterOverflowCorrectionStatus.FAILED,
            "CORRECTION_FAILED",
            "The Cover Letter correction could not be recorded safely.",
        )


def _content_preservation_failed(
    preparation: RunApplicationPreparationResult | None,
) -> bool:
    if preparation is None or preparation.run is None:
        return False
    for result in preparation.run.stage_results:
        if (
            result.stage is ApplicationPreparationStage.COVER_LETTER_PUBLICATION
            and result.stop_reason is not None
            and result.stop_reason.code.value == "TEMPLATE_INVALID"
        ):
            return True
    return False


def _result(
    status: CoverLetterOverflowCorrectionStatus,
    reason: str | None,
    message: str,
) -> CoverLetterOverflowCorrectionResult:
    return CoverLetterOverflowCorrectionResult(status, None, reason, message)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _directive_from_dict(
    value: Mapping[str, Any],
) -> CoverLetterOverflowCorrectionDirective:
    return CoverLetterOverflowCorrectionDirective(
        directive_id=value["directive_id"],
        directive_version=value["directive_version"],
        directive_hash=value["directive_hash"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        attention_item_id=value["attention_item_id"],
        correction_target_ref=MaterialCorrectionTargetRef(
            **value["correction_target_ref"]
        ),
        safe_preview_ref=CoverLetterOverflowCorrectionPreviewRef(
            **value["safe_preview_ref"]
        ),
        publication_result_id=value["publication_result_id"],
        overflow_evaluation_id=value["overflow_evaluation_id"],
        overflow_evaluation_version=value["overflow_evaluation_version"],
        source_record_id=value["source_record_id"],
        source_version=value["source_version"],
        source_content_hash=value["source_content_hash"],
        action=CoverLetterOverflowCorrectionAction(value["action"]),
        mode=CoverLetterOverflowCorrectionMode(value["mode"]),
        previous_directive_id=value["previous_directive_id"],
        created_at=datetime.fromisoformat(
            value["created_at"].replace("Z", "+00:00")
        ),
    )


def _receipt_from_dict(
    value: Mapping[str, Any],
) -> CoverLetterOverflowCorrectionReceipt:
    return CoverLetterOverflowCorrectionReceipt(
        receipt_id=value["receipt_id"],
        receipt_hash=value["receipt_hash"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        attention_item_id=value["attention_item_id"],
        directive_id=value["directive_id"],
        directive_version=value["directive_version"],
        correction_target_ref=MaterialCorrectionTargetRef(
            **value["correction_target_ref"]
        ),
        safe_preview_ref=CoverLetterOverflowCorrectionPreviewRef(
            **value["safe_preview_ref"]
        ),
        mode=CoverLetterOverflowCorrectionMode(value["mode"]),
        source_record_id=value["source_record_id"],
        source_content_hash=value["source_content_hash"],
        preparation_run_id=value["preparation_run_id"],
        preparation_status=value["preparation_status"],
        failure_reason=value["failure_reason"],
        completed_at=datetime.fromisoformat(
            value["completed_at"].replace("Z", "+00:00")
        ),
    )


__all__ = [
    "CoverLetterOverflowCorrectionAction",
    "CoverLetterOverflowCorrectionCommand",
    "CoverLetterOverflowCorrectionDirective",
    "CoverLetterOverflowCorrectionDirectiveProvider",
    "CoverLetterOverflowCorrectionDirectiveRepository",
    "CoverLetterOverflowCorrectionMode",
    "CoverLetterOverflowCorrectionReceipt",
    "CoverLetterOverflowCorrectionReceiptRepository",
    "CoverLetterOverflowCorrectionResult",
    "CoverLetterOverflowCorrectionStatus",
    "PrivateHomeCoverLetterOverflowCorrectionDirectiveRepository",
    "resolve_cover_letter_overflow_correction",
]
