"""Finding-scoped correction directives for unsupported application claims."""

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
from .fact_qa_findings import FactQAMaterialKind
from .human_attention_queue import (
    FactQAFindingAttentionRef,
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
    UnsupportedClaimCorrectionTarget,
)
from .private_home import PrivateHome


UNSUPPORTED_CLAIM_CORRECTION_DIRECTIVE_VERSION = (
    "unsupported-claim-correction-directive-v1"
)
UNSUPPORTED_CLAIM_CORRECTION_RECEIPT_VERSION = (
    "unsupported-claim-correction-receipt-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_DIRECTIVE_ID_RE = re.compile(
    r"^unsupported-claim-correction-[a-f0-9]{64}$"
)


class UnsupportedClaimCorrectionAction(StrEnum):
    REMOVE_UNSUPPORTED_CLAIM = "REMOVE_UNSUPPORTED_CLAIM"
    REWRITE_USING_EXISTING_EVIDENCE = (
        "REWRITE_USING_EXISTING_EVIDENCE"
    )


class UnsupportedClaimCorrectionStatus(StrEnum):
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
    INVALID_CORRECTION = "INVALID_CORRECTION"
    FAILED = "FAILED"


class UnsupportedClaimCorrectionWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class UnsupportedClaimCorrectionCommand:
    subject_id: str
    attention_item_id: str
    action: UnsupportedClaimCorrectionAction
    instruction: str | None
    now: datetime


@dataclass(frozen=True, slots=True)
class UnsupportedClaimCorrectionConstraint:
    directive_id: str
    directive_hash: str
    material_kind: FactQAMaterialKind
    finding_id: str
    action: UnsupportedClaimCorrectionAction
    claim_summary: str
    instruction: str | None

    def __post_init__(self) -> None:
        _text("directive_id", self.directive_id, 240)
        _hash_value("directive_hash", self.directive_hash)
        object.__setattr__(
            self, "material_kind", FactQAMaterialKind(self.material_kind)
        )
        _text("finding_id", self.finding_id, 240)
        object.__setattr__(
            self, "action", UnsupportedClaimCorrectionAction(self.action)
        )
        _text("claim_summary", self.claim_summary, 1_200)
        if self.instruction is not None:
            _text("instruction", self.instruction, 800)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "claim_summary": self.claim_summary,
            "directive_hash": self.directive_hash,
            "directive_id": self.directive_id,
            "finding_id": self.finding_id,
            "instruction": self.instruction,
            "material_kind": self.material_kind.value,
        }


@dataclass(frozen=True, slots=True)
class UnsupportedClaimCorrectionDirective:
    directive_id: str
    directive_version: int
    directive_hash: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    correction_target_ref: MaterialCorrectionTargetRef
    finding_ref: FactQAFindingAttentionRef
    material_kind: FactQAMaterialKind
    action: UnsupportedClaimCorrectionAction
    instruction: str | None
    instruction_hash: str
    claim_summary: str
    previous_directive_id: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        if self.contract_version != (
            UNSUPPORTED_CLAIM_CORRECTION_DIRECTIVE_VERSION
        ):
            raise ValueError("unsupported-claim directive is unsupported")
        if (
            not isinstance(self.directive_id, str)
            or _DIRECTIVE_ID_RE.fullmatch(self.directive_id) is None
        ):
            raise ValueError("unsupported-claim directive ID is invalid")
        if type(self.directive_version) is not int or self.directive_version < 1:
            raise ValueError("directive_version must be positive")
        _hash_value("directive_hash", self.directive_hash)
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            ("attention_item_id", self.attention_item_id, 240),
            ("claim_summary", self.claim_summary, 1_200),
        ):
            _text(name, value, maximum)
        if not isinstance(
            self.correction_target_ref, MaterialCorrectionTargetRef
        ):
            raise TypeError("correction target reference must be typed")
        if not isinstance(self.finding_ref, FactQAFindingAttentionRef):
            raise TypeError("Fact QA finding reference must be typed")
        material = FactQAMaterialKind(self.material_kind)
        action = UnsupportedClaimCorrectionAction(self.action)
        object.__setattr__(self, "material_kind", material)
        object.__setattr__(self, "action", action)
        if (
            self.finding_ref.subject_id != self.subject_id
            or self.finding_ref.application_plan_id
            != self.application_plan_id
            or self.finding_ref.material_kind is not material
        ):
            raise ValueError("directive finding binding is invalid")
        normalized = _instruction(self.instruction)
        object.__setattr__(self, "instruction", normalized)
        if self.instruction_hash != hashlib.sha256(
            (normalized or "").encode()
        ).hexdigest():
            raise ValueError("directive instruction hash is invalid")
        if self.previous_directive_id is not None:
            _text(
                "previous_directive_id", self.previous_directive_id, 240
            )
        _aware("created_at", self.created_at)
        expected = _canonical_hash(self.identity_dict())
        if (
            self.directive_hash != expected
            or self.directive_id
            != f"unsupported-claim-correction-{expected}"
        ):
            raise ValueError("unsupported-claim directive identity is invalid")

    @property
    def constraint(self) -> UnsupportedClaimCorrectionConstraint:
        return UnsupportedClaimCorrectionConstraint(
            directive_id=self.directive_id,
            directive_hash=self.directive_hash,
            material_kind=self.material_kind,
            finding_id=self.finding_ref.finding_id,
            action=self.action,
            claim_summary=self.claim_summary,
            instruction=self.instruction,
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "claim_summary": self.claim_summary,
            "contract_version": self.contract_version,
            "correction_target_ref": self.correction_target_ref.to_dict(),
            "directive_version": self.directive_version,
            "finding_ref": self.finding_ref.to_dict(),
            "instruction": self.instruction,
            "instruction_hash": self.instruction_hash,
            "material_kind": self.material_kind.value,
            "previous_directive_id": self.previous_directive_id,
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
        action: UnsupportedClaimCorrectionAction,
        instruction: str | None,
        previous: "UnsupportedClaimCorrectionDirective | None",
        created_at: datetime,
    ) -> "UnsupportedClaimCorrectionDirective":
        payload = target.payload
        if (
            not isinstance(payload, UnsupportedClaimCorrectionTarget)
            or target.reference != item.correction_target_ref
            or target.subject_id != item.subject_id
            or target.application_plan_id != item.application_plan_id
            or target.attention_item_id != item.item_id
            or payload.finding_ref != item.fact_qa_finding_ref
        ):
            raise ValueError("unsupported-claim target binding is invalid")
        normalized = _instruction(instruction)
        instruction_hash = hashlib.sha256(
            (normalized or "").encode()
        ).hexdigest()
        version = previous.directive_version + 1 if previous else 1
        identity = {
            "action": UnsupportedClaimCorrectionAction(action).value,
            "application_plan_id": item.application_plan_id,
            "attention_item_id": item.item_id,
            "claim_summary": payload.claim_summary,
            "contract_version": (
                UNSUPPORTED_CLAIM_CORRECTION_DIRECTIVE_VERSION
            ),
            "correction_target_ref": target.reference.to_dict(),
            "directive_version": version,
            "finding_ref": payload.finding_ref.to_dict(),
            "instruction": normalized,
            "instruction_hash": instruction_hash,
            "material_kind": payload.finding_ref.material_kind.value,
            "previous_directive_id": (
                previous.directive_id if previous else None
            ),
            "subject_id": item.subject_id,
        }
        digest = _canonical_hash(identity)
        return cls(
            directive_id=f"unsupported-claim-correction-{digest}",
            directive_version=version,
            directive_hash=digest,
            contract_version=(
                UNSUPPORTED_CLAIM_CORRECTION_DIRECTIVE_VERSION
            ),
            subject_id=item.subject_id,
            application_plan_id=item.application_plan_id,
            attention_item_id=item.item_id,
            correction_target_ref=target.reference,
            finding_ref=payload.finding_ref,
            material_kind=payload.finding_ref.material_kind,
            action=UnsupportedClaimCorrectionAction(action),
            instruction=normalized,
            instruction_hash=instruction_hash,
            claim_summary=payload.claim_summary,
            previous_directive_id=(
                previous.directive_id if previous else None
            ),
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class UnsupportedClaimCorrectionWriteResult:
    status: UnsupportedClaimCorrectionWriteStatus
    directive: UnsupportedClaimCorrectionDirective | None


@runtime_checkable
class UnsupportedClaimCorrectionDirectiveRepository(Protocol):
    def save(
        self, directive: UnsupportedClaimCorrectionDirective
    ) -> UnsupportedClaimCorrectionWriteResult: ...

    def get_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        material_kind: FactQAMaterialKind,
        finding_id: str,
    ) -> UnsupportedClaimCorrectionDirective | None: ...

    def list_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        material_kind: FactQAMaterialKind,
    ) -> tuple[UnsupportedClaimCorrectionDirective, ...]: ...


class PrivateHomeUnsupportedClaimCorrectionDirectiveRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        subject = _text("subject_id", subject_id, 160)
        return (
            self._home.paths.preparation
            / "unsupported-claim-correction-directives"
            / ("subject-" + hashlib.sha256(subject.encode()).hexdigest())
        )

    def save(
        self, directive: UnsupportedClaimCorrectionDirective
    ) -> UnsupportedClaimCorrectionWriteResult:
        if not isinstance(
            directive, UnsupportedClaimCorrectionDirective
        ):
            raise TypeError("directive must be typed")
        path = self._directory(directive.subject_id) / (
            directive.directive_id + ".json"
        )
        content = _json(directive.to_dict())
        try:
            created = self._home.write_bytes_if_absent(path, content)
            if not created:
                existing = _directive_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                if existing.identity_dict() != directive.identity_dict():
                    return UnsupportedClaimCorrectionWriteResult(
                        UnsupportedClaimCorrectionWriteStatus.FAILED, None
                    )
                return UnsupportedClaimCorrectionWriteResult(
                    UnsupportedClaimCorrectionWriteStatus.UNCHANGED,
                    existing,
                )
            return UnsupportedClaimCorrectionWriteResult(
                UnsupportedClaimCorrectionWriteStatus.CREATED, directive
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return UnsupportedClaimCorrectionWriteResult(
                UnsupportedClaimCorrectionWriteStatus.FAILED, None
            )

    def _list(
        self, subject_id: str
    ) -> tuple[UnsupportedClaimCorrectionDirective, ...]:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return ()
        directives = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            contained = self._home.contained_path(path)
            if contained.is_symlink() or not contained.is_file():
                raise ValueError("directive path is unsafe")
            directive = _directive_from_dict(
                json.loads(contained.read_text(encoding="utf-8"))
            )
            if directive.subject_id != subject_id:
                raise ValueError("directive subject binding is invalid")
            directives.append(directive)
        return tuple(directives)

    def get_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        material_kind: FactQAMaterialKind,
        finding_id: str,
    ) -> UnsupportedClaimCorrectionDirective | None:
        matches = tuple(
            item
            for item in self._list(subject_id)
            if item.application_plan_id == application_plan_id
            and item.material_kind is FactQAMaterialKind(material_kind)
            and item.finding_ref.finding_id == finding_id
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
            raise ValueError("directive history is not contiguous")
        return ordered[-1]

    def list_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        material_kind: FactQAMaterialKind,
    ) -> tuple[UnsupportedClaimCorrectionDirective, ...]:
        by_finding: dict[str, UnsupportedClaimCorrectionDirective] = {}
        for item in self._list(subject_id):
            if (
                item.application_plan_id != application_plan_id
                or item.material_kind is not FactQAMaterialKind(material_kind)
            ):
                continue
            prior = by_finding.get(item.finding_ref.finding_id)
            if prior is None or item.directive_version > prior.directive_version:
                by_finding[item.finding_ref.finding_id] = item
        return tuple(
            by_finding[key]
            for key in sorted(by_finding)
        )


@dataclass(frozen=True, slots=True)
class UnsupportedClaimCorrectionDirectiveSetResult:
    succeeded: bool
    directives: tuple[UnsupportedClaimCorrectionDirective, ...]


class UnsupportedClaimCorrectionDirectiveProvider:
    def __init__(
        self, repository: UnsupportedClaimCorrectionDirectiveRepository
    ) -> None:
        self._repository = repository

    def list_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        material_kind: FactQAMaterialKind,
    ) -> UnsupportedClaimCorrectionDirectiveSetResult:
        try:
            material = FactQAMaterialKind(material_kind)
            directives = self._repository.list_current(
                subject_id=subject_id,
                application_plan_id=application_plan_id,
                material_kind=material,
            )
            if any(
                item.subject_id != subject_id
                or item.application_plan_id != application_plan_id
                or item.material_kind is not material
                for item in directives
            ):
                raise ValueError("directive provider binding drifted")
            return UnsupportedClaimCorrectionDirectiveSetResult(
                True, directives
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return UnsupportedClaimCorrectionDirectiveSetResult(False, ())


@dataclass(frozen=True, slots=True)
class UnsupportedClaimCorrectionReceipt:
    receipt_id: str
    receipt_hash: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    directive_id: str
    directive_version: int
    finding_id: str
    action: UnsupportedClaimCorrectionAction
    preparation_run_id: str | None
    preparation_status: str
    failure_reason: str | None
    completed_at: datetime

    def identity_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "contract_version": self.contract_version,
            "directive_id": self.directive_id,
            "directive_version": self.directive_version,
            "failure_reason": self.failure_reason,
            "finding_id": self.finding_id,
            "preparation_run_id": self.preparation_run_id,
            "preparation_status": self.preparation_status,
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
        directive: UnsupportedClaimCorrectionDirective,
        preparation_run_id: str | None,
        preparation_status: str,
        failure_reason: str | None,
        completed_at: datetime,
    ) -> "UnsupportedClaimCorrectionReceipt":
        identity = {
            "action": directive.action.value,
            "application_plan_id": directive.application_plan_id,
            "attention_item_id": directive.attention_item_id,
            "contract_version": (
                UNSUPPORTED_CLAIM_CORRECTION_RECEIPT_VERSION
            ),
            "directive_id": directive.directive_id,
            "directive_version": directive.directive_version,
            "failure_reason": failure_reason,
            "finding_id": directive.finding_ref.finding_id,
            "preparation_run_id": preparation_run_id,
            "preparation_status": preparation_status,
            "subject_id": directive.subject_id,
        }
        digest = _canonical_hash(identity)
        return cls(
            receipt_id="unsupported-claim-correction-receipt-" + digest,
            receipt_hash=digest,
            contract_version=(
                UNSUPPORTED_CLAIM_CORRECTION_RECEIPT_VERSION
            ),
            subject_id=directive.subject_id,
            application_plan_id=directive.application_plan_id,
            attention_item_id=directive.attention_item_id,
            directive_id=directive.directive_id,
            directive_version=directive.directive_version,
            finding_id=directive.finding_ref.finding_id,
            action=directive.action,
            preparation_run_id=preparation_run_id,
            preparation_status=preparation_status,
            failure_reason=failure_reason,
            completed_at=completed_at,
        )


class UnsupportedClaimCorrectionReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def save(self, receipt: UnsupportedClaimCorrectionReceipt) -> bool:
        key = hashlib.sha256(receipt.subject_id.encode()).hexdigest()
        path = (
            self._home.paths.preparation
            / "unsupported-claim-correction-receipts"
            / ("subject-" + key)
            / (receipt.receipt_id + ".json")
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable correction receipt conflict")
        return created


@dataclass(frozen=True, slots=True)
class UnsupportedClaimCorrectionResult:
    status: UnsupportedClaimCorrectionStatus
    receipt: UnsupportedClaimCorrectionReceipt | None
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


async def resolve_unsupported_claim_correction(
    command: UnsupportedClaimCorrectionCommand,
    *,
    queue_reader: QueueReader,
    target_provider: MaterialCorrectionTargetProvider,
    directive_repository: UnsupportedClaimCorrectionDirectiveRepository,
    preparation_callable: PreparationCallable,
    receipt_repository: UnsupportedClaimCorrectionReceiptRepository,
) -> UnsupportedClaimCorrectionResult:
    try:
        subject = _text("subject_id", command.subject_id, 160)
        item_id = _text(
            "attention_item_id", command.attention_item_id, 240
        )
        action = UnsupportedClaimCorrectionAction(command.action)
        instruction = _instruction(command.instruction)
        now = _aware("now", command.now)
    except (AttributeError, TypeError, ValueError):
        return _result(
            UnsupportedClaimCorrectionStatus.INVALID_CORRECTION,
            "INVALID_CORRECTION",
            "The correction command is invalid.",
        )
    try:
        queue = await _resolve(
            queue_reader(subject_id=subject, now=now)
        )
        if (
            not isinstance(queue, HumanAttentionQueueResult)
            or queue.status is not HumanAttentionQueueStatus.SUCCEEDED
            or queue.subject_id != subject
        ):
            raise ValueError("current attention queue is unavailable")
        item = next(
            (candidate for candidate in queue.items if candidate.item_id == item_id),
            None,
        )
        if item is None:
            return _result(
                UnsupportedClaimCorrectionStatus.ITEM_NOT_CURRENT,
                "ITEM_NOT_CURRENT",
                "The unsupported-claim item is no longer current.",
            )
        if (
            item.subject_id != subject
            or item.audience is not HumanAttentionAudience.USER
            or item.resolution_capability
            is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
            or item.correction_target_ref is None
        ):
            return _result(
                UnsupportedClaimCorrectionStatus.UNSUPPORTED_TARGET,
                "UNSUPPORTED_TARGET",
                "This item requires a different correction path.",
            )
        typed = target_provider.get_current_typed_target(item=item)
        if typed.status is MaterialCorrectionTargetStatus.TARGET_STALE:
            return _result(
                UnsupportedClaimCorrectionStatus.TARGET_STALE,
                "TARGET_STALE",
                "The correction target is stale.",
            )
        if (
            typed.status is not MaterialCorrectionTargetStatus.AVAILABLE
            or typed.target is None
        ):
            return _result(
                UnsupportedClaimCorrectionStatus.TARGET_UNAVAILABLE,
                typed.status.value,
                "The correction target is unavailable.",
            )
        target = typed.target
        if not isinstance(
            target.payload, UnsupportedClaimCorrectionTarget
        ):
            return _result(
                UnsupportedClaimCorrectionStatus.UNSUPPORTED_TARGET,
                "UNSUPPORTED_TARGET",
                "This target is not an unsupported claim.",
            )
        finding = target.payload.finding_ref
        if (
            item.fact_qa_finding_ref != finding
            or finding.subject_id != subject
            or finding.application_plan_id != item.application_plan_id
            or target.source_record_id != finding.source_material_id
            or target.source_content_hash
            != finding.source_material_content_hash
        ):
            return _result(
                UnsupportedClaimCorrectionStatus.TARGET_STALE,
                "TARGET_STALE",
                "The finding or source identity has changed.",
            )
        previous = directive_repository.get_current(
            subject_id=subject,
            application_plan_id=item.application_plan_id,
            material_kind=finding.material_kind,
            finding_id=finding.finding_id,
        )
        if (
            previous is not None
            and previous.correction_target_ref == target.reference
            and previous.action is action
            and previous.instruction == instruction
        ):
            return _result(
                UnsupportedClaimCorrectionStatus.UNCHANGED,
                None,
                "This correction directive is unchanged.",
            )
        directive = UnsupportedClaimCorrectionDirective.create(
            item=item,
            target=target,
            action=action,
            instruction=instruction,
            previous=previous,
            created_at=now,
        )
        write = directive_repository.save(directive)
        if (
            write.status is UnsupportedClaimCorrectionWriteStatus.FAILED
            or write.directive is None
        ):
            raise ValueError("correction directive persistence failed")
        if write.status is UnsupportedClaimCorrectionWriteStatus.UNCHANGED:
            return _result(
                UnsupportedClaimCorrectionStatus.UNCHANGED,
                None,
                "This correction directive is unchanged.",
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
            failure_reason = (
                preparation.reason_code.value
                if preparation.reason_code is not None
                else None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            preparation_status = ApplicationPreparationStatus.FAILED
            run_id = None
            failure_reason = "PREPARATION_RERUN_FAILED"
        receipt = UnsupportedClaimCorrectionReceipt.create(
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
                UnsupportedClaimCorrectionStatus
                .CORRECTED_AND_PREPARATION_COMPLETED
            )
        elif preparation_status is ApplicationPreparationStatus.DEFERRED:
            status = (
                UnsupportedClaimCorrectionStatus
                .CORRECTED_AND_PREPARATION_DEFERRED
            )
        else:
            status = (
                UnsupportedClaimCorrectionStatus
                .CORRECTION_RECORDED_PREPARATION_FAILED
            )
        return UnsupportedClaimCorrectionResult(
            status,
            receipt,
            failure_reason,
            "The correction directive was recorded and preparation reran.",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _result(
            UnsupportedClaimCorrectionStatus.FAILED,
            "CORRECTION_FAILED",
            "The unsupported claim could not be corrected safely.",
        )


def _result(status, reason, message):
    return UnsupportedClaimCorrectionResult(status, None, reason, message)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _directive_from_dict(
    value: Mapping[str, Any],
) -> UnsupportedClaimCorrectionDirective:
    return UnsupportedClaimCorrectionDirective(
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
        finding_ref=FactQAFindingAttentionRef.from_dict(
            value["finding_ref"]
        ),
        material_kind=value["material_kind"],
        action=value["action"],
        instruction=value["instruction"],
        instruction_hash=value["instruction_hash"],
        claim_summary=value["claim_summary"],
        previous_directive_id=value["previous_directive_id"],
        created_at=datetime.fromisoformat(
            value["created_at"].replace("Z", "+00:00")
        ),
    )


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


def _instruction(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("instruction must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > 800:
        raise ValueError("instruction is outside the bounded contract")
    return normalized


def _hash_value(name: str, value: Any) -> str:
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
    "PrivateHomeUnsupportedClaimCorrectionDirectiveRepository",
    "UNSUPPORTED_CLAIM_CORRECTION_DIRECTIVE_VERSION",
    "UNSUPPORTED_CLAIM_CORRECTION_RECEIPT_VERSION",
    "UnsupportedClaimCorrectionAction",
    "UnsupportedClaimCorrectionCommand",
    "UnsupportedClaimCorrectionConstraint",
    "UnsupportedClaimCorrectionDirective",
    "UnsupportedClaimCorrectionDirectiveProvider",
    "UnsupportedClaimCorrectionDirectiveRepository",
    "UnsupportedClaimCorrectionDirectiveSetResult",
    "UnsupportedClaimCorrectionReceipt",
    "UnsupportedClaimCorrectionReceiptRepository",
    "UnsupportedClaimCorrectionResult",
    "UnsupportedClaimCorrectionStatus",
    "UnsupportedClaimCorrectionWriteResult",
    "UnsupportedClaimCorrectionWriteStatus",
    "resolve_unsupported_claim_correction",
]
