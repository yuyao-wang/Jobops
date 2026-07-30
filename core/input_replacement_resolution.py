"""Resolve one current unreadable input by selecting a registered replacement."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .application_preparation_orchestrator import (
    ApplicationPreparationStatus,
    RunApplicationPreparationCommand,
    RunApplicationPreparationResult,
)
from .human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
    HumanAttentionResolutionCapability,
)
from .input_replacement_target import (
    BaseLatexVersionReplacementTarget,
    InputReplacementTarget,
    InputReplacementTargetKind,
    InputReplacementTargetProvider,
    SourceResumeReplacementTarget,
)
from .plan_scoped_version_override import (
    PlanScopedVersionOverride,
    PlanScopedVersionOverrideKind,
    PlanScopedVersionOverrideRepository,
)
from .private_home import PrivateHome
from .resume_candidates import (
    ResumeCandidate,
    ResumeCandidateListStatus,
    ResumeCandidateProvider,
)
from .resume_latex_versions import (
    ResumeLatexVersion,
    ResumeLatexVersionListStatus,
    ResumeLatexVersionProvider,
)


INPUT_REPLACEMENT_RESOLUTION_CONTRACT_VERSION = (
    "input-replacement-resolution-v1"
)


class InputReplacementAction(StrEnum):
    SELECT_EXISTING_REPLACEMENT = "SELECT_EXISTING_REPLACEMENT"


class InputReplacementResolutionStatus(StrEnum):
    REPLACED_AND_PREPARATION_COMPLETED = (
        "REPLACED_AND_PREPARATION_COMPLETED"
    )
    REPLACED_AND_PREPARATION_DEFERRED = (
        "REPLACED_AND_PREPARATION_DEFERRED"
    )
    REPLACEMENT_RECORDED_PREPARATION_FAILED = (
        "REPLACEMENT_RECORDED_PREPARATION_FAILED"
    )
    UNCHANGED = "UNCHANGED"
    ITEM_NOT_CURRENT = "ITEM_NOT_CURRENT"
    TARGET_STALE = "TARGET_STALE"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    OPTION_NOT_SELECTABLE = "OPTION_NOT_SELECTABLE"
    SAME_INPUT_SELECTED = "SAME_INPUT_SELECTED"
    NO_EXISTING_REPLACEMENT = "NO_EXISTING_REPLACEMENT"
    NEW_INPUT_REGISTRATION_REQUIRED = "NEW_INPUT_REGISTRATION_REQUIRED"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class InputReplacementResolutionCommand:
    subject_id: str
    attention_item_id: str
    action: InputReplacementAction
    replacement_option_id: str
    now: datetime
    invocation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SelectableInputReplacement:
    option_id: str
    option_version: str
    content_hash: str
    display_name: str
    target_kind: InputReplacementTargetKind


@dataclass(frozen=True, slots=True)
class InputReplacementResolutionReceipt:
    receipt_id: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    replacement_target_id: str
    replacement_target_version: str
    replacement_target_hash: str
    target_kind: InputReplacementTargetKind
    replaced_input_id: str
    replaced_input_version: str
    replaced_input_content_hash: str
    selected_replacement_id: str
    selected_replacement_version: str
    selected_replacement_content_hash: str
    override_id: str
    override_version: str
    replacement_reason: str
    previous_receipt_id: str | None
    previous_override_id: str | None
    preparation_run_id: str | None
    preparation_status: str
    contract_version: str
    resolved_at: datetime
    receipt_hash: str

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "contract_version": self.contract_version,
            "override_id": self.override_id,
            "override_version": self.override_version,
            "preparation_run_id": self.preparation_run_id,
            "preparation_status": self.preparation_status,
            "previous_override_id": self.previous_override_id,
            "previous_receipt_id": self.previous_receipt_id,
            "replaced_input_content_hash": self.replaced_input_content_hash,
            "replaced_input_id": self.replaced_input_id,
            "replaced_input_version": self.replaced_input_version,
            "replacement_reason": self.replacement_reason,
            "replacement_target_hash": self.replacement_target_hash,
            "replacement_target_id": self.replacement_target_id,
            "replacement_target_version": self.replacement_target_version,
            "selected_replacement_content_hash": (
                self.selected_replacement_content_hash
            ),
            "selected_replacement_id": self.selected_replacement_id,
            "selected_replacement_version": self.selected_replacement_version,
            "subject_id": self.subject_id,
            "target_kind": self.target_kind.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "receipt_hash": self.receipt_hash,
            "receipt_id": self.receipt_id,
            "resolved_at": _time(self.resolved_at),
        }

    @classmethod
    def create(
        cls,
        *,
        target: InputReplacementTarget,
        selected: SelectableInputReplacement,
        override: PlanScopedVersionOverride,
        previous_receipt_id: str | None,
        preparation_run_id: str | None,
        preparation_status: str,
        resolved_at: datetime,
    ) -> "InputReplacementResolutionReceipt":
        values = {
            "application_plan_id": target.application_plan_id,
            "attention_item_id": target.attention_item_id,
            "contract_version": INPUT_REPLACEMENT_RESOLUTION_CONTRACT_VERSION,
            "override_id": override.override_id,
            "override_version": override.contract_version,
            "preparation_run_id": preparation_run_id,
            "preparation_status": preparation_status,
            "previous_override_id": override.previous_override_id,
            "previous_receipt_id": previous_receipt_id,
            "replaced_input_content_hash": target.current_input_content_hash,
            "replaced_input_id": target.current_input_record_id,
            "replaced_input_version": target.current_input_version,
            "replacement_reason": target.origin_stop_reason.code.value,
            "replacement_target_hash": target.target_hash,
            "replacement_target_id": target.target_id,
            "replacement_target_version": target.target_version,
            "selected_replacement_content_hash": selected.content_hash,
            "selected_replacement_id": selected.option_id,
            "selected_replacement_version": selected.option_version,
            "subject_id": target.subject_id,
            "target_kind": target.target_kind.value,
        }
        digest = _hash(values)
        return cls(
            receipt_id="input-replacement-resolution-" + digest,
            subject_id=target.subject_id,
            application_plan_id=target.application_plan_id,
            attention_item_id=target.attention_item_id,
            replacement_target_id=target.target_id,
            replacement_target_version=target.target_version,
            replacement_target_hash=target.target_hash,
            target_kind=target.target_kind,
            replaced_input_id=target.current_input_record_id,
            replaced_input_version=target.current_input_version,
            replaced_input_content_hash=target.current_input_content_hash,
            selected_replacement_id=selected.option_id,
            selected_replacement_version=selected.option_version,
            selected_replacement_content_hash=selected.content_hash,
            override_id=override.override_id,
            override_version=override.contract_version,
            replacement_reason=target.origin_stop_reason.code.value,
            previous_receipt_id=previous_receipt_id,
            previous_override_id=override.previous_override_id,
            preparation_run_id=preparation_run_id,
            preparation_status=preparation_status,
            contract_version=INPUT_REPLACEMENT_RESOLUTION_CONTRACT_VERSION,
            resolved_at=resolved_at,
            receipt_hash=digest,
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "InputReplacementResolutionReceipt":
        receipt = cls(
            receipt_id=str(value["receipt_id"]),
            subject_id=str(value["subject_id"]),
            application_plan_id=str(value["application_plan_id"]),
            attention_item_id=str(value["attention_item_id"]),
            replacement_target_id=str(value["replacement_target_id"]),
            replacement_target_version=str(value["replacement_target_version"]),
            replacement_target_hash=str(value["replacement_target_hash"]),
            target_kind=InputReplacementTargetKind(value["target_kind"]),
            replaced_input_id=str(value["replaced_input_id"]),
            replaced_input_version=str(value["replaced_input_version"]),
            replaced_input_content_hash=str(
                value["replaced_input_content_hash"]
            ),
            selected_replacement_id=str(value["selected_replacement_id"]),
            selected_replacement_version=str(
                value["selected_replacement_version"]
            ),
            selected_replacement_content_hash=str(
                value["selected_replacement_content_hash"]
            ),
            override_id=str(value["override_id"]),
            override_version=str(value["override_version"]),
            replacement_reason=str(value["replacement_reason"]),
            previous_receipt_id=value.get("previous_receipt_id"),
            previous_override_id=value.get("previous_override_id"),
            preparation_run_id=value.get("preparation_run_id"),
            preparation_status=str(value["preparation_status"]),
            contract_version=str(value["contract_version"]),
            resolved_at=datetime.fromisoformat(
                str(value["resolved_at"]).replace("Z", "+00:00")
            ),
            receipt_hash=str(value["receipt_hash"]),
        )
        if (
            receipt.contract_version
            != INPUT_REPLACEMENT_RESOLUTION_CONTRACT_VERSION
            or receipt.receipt_hash != _hash(receipt.identity_dict())
            or receipt.receipt_id
            != "input-replacement-resolution-" + receipt.receipt_hash
        ):
            raise ValueError("input replacement receipt integrity failure")
        _time(receipt.resolved_at)
        return receipt


class InputReplacementResolutionReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        key = hashlib.sha256(subject_id.encode()).hexdigest()
        return (
            self._home.paths.preparation
            / "input-replacement-resolutions"
            / ("subject-" + key)
        )

    def save(self, receipt: InputReplacementResolutionReceipt) -> bool:
        path = self._directory(receipt.subject_id) / (
            receipt.receipt_id + ".json"
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable input replacement receipt conflict")
        return created

    def find_replay(
        self,
        *,
        subject_id: str,
        attention_item_id: str,
        replacement_target_id: str | None,
        selected_replacement_id: str,
    ) -> InputReplacementResolutionReceipt | None:
        matches = [
            receipt
            for receipt in self._read_subject(subject_id)
            if receipt.attention_item_id == attention_item_id
            and receipt.selected_replacement_id == selected_replacement_id
            and (
                replacement_target_id is None
                or receipt.replacement_target_id == replacement_target_id
            )
        ]
        return max(matches, key=lambda item: item.receipt_id) if matches else None

    def get_latest(
        self, *, subject_id: str, application_plan_id: str
    ) -> InputReplacementResolutionReceipt | None:
        matches = [
            item
            for item in self._read_subject(subject_id)
            if item.application_plan_id == application_plan_id
        ]
        return (
            max(
                matches,
                key=lambda item: (
                    item.resolved_at.astimezone(timezone.utc),
                    item.receipt_id,
                ),
            )
            if matches
            else None
        )

    def _read_subject(
        self, subject_id: str
    ) -> tuple[InputReplacementResolutionReceipt, ...]:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return ()
        values = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            path = self._home.contained_path(path)
            value = InputReplacementResolutionReceipt.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if value.subject_id != subject_id:
                raise ValueError("input replacement receipt subject mismatch")
            values.append(value)
        return tuple(values)


@dataclass(frozen=True, slots=True)
class InputReplacementResolutionResult:
    status: InputReplacementResolutionStatus
    receipt: InputReplacementResolutionReceipt | None
    reason_code: str | None
    message: str


QueueReader = Callable[..., HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult]]
PreparationCallable = Callable[
    ...,
    RunApplicationPreparationResult
    | Awaitable[RunApplicationPreparationResult],
]


async def resolve_input_replacement(
    command: InputReplacementResolutionCommand,
    *,
    queue_reader: QueueReader,
    target_provider: InputReplacementTargetProvider,
    resume_candidate_provider: ResumeCandidateProvider,
    latex_version_provider: ResumeLatexVersionProvider,
    override_repository: PlanScopedVersionOverrideRepository,
    preparation_callable: PreparationCallable,
    receipt_repository: InputReplacementResolutionReceiptRepository,
) -> InputReplacementResolutionResult:
    try:
        subject = _text(command.subject_id)
        item_id = _text(command.attention_item_id)
        option_id = _text(command.replacement_option_id)
        action = InputReplacementAction(command.action)
        _time(command.now)
        if command.invocation_id is not None:
            _text(command.invocation_id)
        if action is not InputReplacementAction.SELECT_EXISTING_REPLACEMENT:
            raise ValueError("input replacement action is unsupported")

        queue = await _resolve(queue_reader(subject_id=subject, now=command.now))
        if (
            not isinstance(queue, HumanAttentionQueueResult)
            or queue.status is not HumanAttentionQueueStatus.SUCCEEDED
            or queue.subject_id != subject
        ):
            raise ValueError("current attention queue is unavailable")
        item = next(
            (value for value in queue.items if value.item_id == item_id), None
        )
        if item is None:
            replay = receipt_repository.find_replay(
                subject_id=subject,
                attention_item_id=item_id,
                replacement_target_id=None,
                selected_replacement_id=option_id,
            )
            if replay is not None and replay.preparation_status != "FAILED":
                return _result(
                    InputReplacementResolutionStatus.UNCHANGED,
                    replay,
                    None,
                    "This replacement is unchanged.",
                )
            return _result(
                InputReplacementResolutionStatus.ITEM_NOT_CURRENT,
                None,
                "ITEM_NOT_CURRENT",
                "The replacement item is no longer current.",
            )
        if (
            item.subject_id != subject
            or item.audience is not HumanAttentionAudience.USER
            or item.resolution_capability
            is not HumanAttentionResolutionCapability.REPLACE_INPUT
            or item.replacement_target_ref is None
        ):
            return _result(
                InputReplacementResolutionStatus.UNSUPPORTED_TARGET,
                None,
                "UNSUPPORTED_TARGET",
                "This item is not an existing-input replacement.",
            )
        target = target_provider.get_current_typed_target(item=item)
        if target is None:
            return _result(
                InputReplacementResolutionStatus.TARGET_UNAVAILABLE,
                None,
                "TARGET_UNAVAILABLE",
                "The current replacement target is unavailable.",
            )
        replay = receipt_repository.find_replay(
            subject_id=subject,
            attention_item_id=item_id,
            replacement_target_id=target.target_id,
            selected_replacement_id=option_id,
        )
        if replay is not None:
            return _result(
                InputReplacementResolutionStatus.UNCHANGED,
                replay,
                None,
                "This replacement is unchanged.",
            )
        options, old_is_current, kind = _selectable_options(
            target=target,
            resume_candidate_provider=resume_candidate_provider,
            latex_version_provider=latex_version_provider,
        )
        if not old_is_current:
            return _result(
                InputReplacementResolutionStatus.TARGET_STALE,
                None,
                "TARGET_STALE",
                "The input bound to this replacement target has changed.",
            )
        if option_id == target.current_input_record_id:
            return _result(
                InputReplacementResolutionStatus.SAME_INPUT_SELECTED,
                None,
                "SAME_INPUT_SELECTED",
                "Select a different registered input.",
            )
        replacements = tuple(
            value
            for value in options
            if value.option_id != target.current_input_record_id
        )
        if not replacements:
            return _result(
                InputReplacementResolutionStatus.NO_EXISTING_REPLACEMENT,
                None,
                "NEW_INPUT_REGISTRATION_REQUIRED",
                "No other registered input is currently selectable.",
            )
        selected = next(
            (value for value in replacements if value.option_id == option_id),
            None,
        )
        if selected is None:
            return _result(
                InputReplacementResolutionStatus.OPTION_NOT_SELECTABLE,
                None,
                "OPTION_NOT_SELECTABLE",
                "The selected replacement is not currently selectable.",
            )
        previous = override_repository.get_current(
            subject_id=subject,
            application_plan_id=target.application_plan_id,
            override_kind=kind,
        )
        action_hash = hashlib.sha256(
            f"{action.value}\0{option_id}".encode()
        ).hexdigest()
        override = PlanScopedVersionOverride.create(
            subject_id=subject,
            application_plan_id=target.application_plan_id,
            override_kind=kind,
            selected_option_id=selected.option_id,
            source_attention_item_id=item.item_id,
            source_stage=item.source_stage.value,
            source_record_id=item.source_record_id,
            user_message_hash=action_hash,
            previous_override_id=(
                previous.override_id if previous is not None else None
            ),
            replacement_target_id=target.target_id,
            replacement_target_version=target.target_version,
            replacement_target_hash=target.target_hash,
            replacement_reason=target.origin_stop_reason.code.value,
            replaced_option_id=target.current_input_record_id,
            replaced_option_version=target.current_input_version,
            replaced_option_content_hash=target.current_input_content_hash,
            created_at=command.now,
        )
        override_repository.save(override)
        preparation_status, run_id = await _run_preparation(
            preparation_callable,
            subject=subject,
            application_plan_id=target.application_plan_id,
            now=command.now,
        )
        latest = receipt_repository.get_latest(
            subject_id=subject,
            application_plan_id=target.application_plan_id,
        )
        receipt = InputReplacementResolutionReceipt.create(
            target=target,
            selected=selected,
            override=override,
            previous_receipt_id=(
                latest.receipt_id if latest is not None else None
            ),
            preparation_run_id=run_id,
            preparation_status=preparation_status.value,
            resolved_at=command.now,
        )
        receipt_repository.save(receipt)
        if preparation_status in {
            ApplicationPreparationStatus.COMPLETED,
            ApplicationPreparationStatus.UNCHANGED,
        }:
            status = (
                InputReplacementResolutionStatus
                .REPLACED_AND_PREPARATION_COMPLETED
            )
        elif preparation_status is ApplicationPreparationStatus.DEFERRED:
            status = (
                InputReplacementResolutionStatus
                .REPLACED_AND_PREPARATION_DEFERRED
            )
        else:
            status = (
                InputReplacementResolutionStatus
                .REPLACEMENT_RECORDED_PREPARATION_FAILED
            )
        return _result(status, receipt, None, "The replacement was recorded.")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _result(
            InputReplacementResolutionStatus.FAILED,
            None,
            "FAILED",
            "The replacement could not be recorded safely.",
        )


def list_selectable_input_replacements(
    *,
    subject_id: str,
    item: Any,
    target_provider: InputReplacementTargetProvider,
    resume_candidate_provider: ResumeCandidateProvider,
    latex_version_provider: ResumeLatexVersionProvider,
) -> tuple[SelectableInputReplacement, ...]:
    """Return safe exact-ID options for an already authenticated current item."""

    if item.subject_id != subject_id:
        raise ValueError("replacement item subject mismatch")
    target = target_provider.get_current_typed_target(item=item)
    if target is None:
        return ()
    options, old_is_current, _ = _selectable_options(
        target=target,
        resume_candidate_provider=resume_candidate_provider,
        latex_version_provider=latex_version_provider,
    )
    if not old_is_current:
        return ()
    return tuple(
        value
        for value in options
        if value.option_id != target.current_input_record_id
    )


async def get_selectable_input_replacements(
    *,
    subject_id: str,
    attention_item_id: str,
    now: datetime,
    queue_reader: QueueReader,
    target_provider: InputReplacementTargetProvider,
    resume_candidate_provider: ResumeCandidateProvider,
    latex_version_provider: ResumeLatexVersionProvider,
) -> tuple[SelectableInputReplacement, ...]:
    """Read one current Queue snapshot and return exact safe alternatives."""

    subject = _text(subject_id)
    item_id = _text(attention_item_id)
    _time(now)
    queue = await _resolve(queue_reader(subject_id=subject, now=now))
    if (
        not isinstance(queue, HumanAttentionQueueResult)
        or queue.status is not HumanAttentionQueueStatus.SUCCEEDED
        or queue.subject_id != subject
    ):
        raise ValueError("current attention queue is unavailable")
    item = next(
        (value for value in queue.items if value.item_id == item_id), None
    )
    if item is None:
        return ()
    return list_selectable_input_replacements(
        subject_id=subject,
        item=item,
        target_provider=target_provider,
        resume_candidate_provider=resume_candidate_provider,
        latex_version_provider=latex_version_provider,
    )


def _selectable_options(
    *,
    target: InputReplacementTarget,
    resume_candidate_provider: ResumeCandidateProvider,
    latex_version_provider: ResumeLatexVersionProvider,
) -> tuple[
    tuple[SelectableInputReplacement, ...],
    bool,
    PlanScopedVersionOverrideKind,
]:
    if isinstance(target.payload, SourceResumeReplacementTarget):
        listed = resume_candidate_provider.list_selectable(target.subject_id)
        if (
            listed.status is not ResumeCandidateListStatus.SUCCEEDED
            or listed.subject_id != target.subject_id
        ):
            raise ValueError("ResumeCandidate options are unavailable")
        options = tuple(
            _resume_option(value)
            for value in listed.candidates
            if isinstance(value, ResumeCandidate)
            and value.subject_id == target.subject_id
        )
        old = next(
            (
                value
                for value in options
                if value.option_id == target.current_input_record_id
            ),
            None,
        )
        return (
            options,
            old is not None
            and old.option_version == target.current_input_version
            and old.content_hash == target.current_input_content_hash,
            PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE,
        )
    if not isinstance(target.payload, BaseLatexVersionReplacementTarget):
        raise ValueError("replacement target kind is unsupported")
    listed = latex_version_provider.list_selectable(target.subject_id)
    if (
        listed.status is not ResumeLatexVersionListStatus.SUCCEEDED
        or listed.subject_id != target.subject_id
    ):
        raise ValueError("LaTeX Version options are unavailable")
    options = tuple(
        _latex_option(value)
        for value in listed.versions
        if isinstance(value, ResumeLatexVersion)
        and value.subject_id == target.subject_id
    )
    old = next(
        (
            value
            for value in options
            if value.option_id == target.current_input_record_id
        ),
        None,
    )
    return (
        options,
        old is not None
        and old.content_hash == target.current_input_content_hash,
        PlanScopedVersionOverrideKind.LATEX_VERSION_OVERRIDE,
    )


def _resume_option(value: ResumeCandidate) -> SelectableInputReplacement:
    return SelectableInputReplacement(
        value.resume_id,
        value.contract_version,
        value.artifact_sha256,
        value.display_name,
        InputReplacementTargetKind.SOURCE_RESUME,
    )


def _latex_option(value: ResumeLatexVersion) -> SelectableInputReplacement:
    return SelectableInputReplacement(
        value.latex_version_id,
        value.contract_version,
        value.source_sha256,
        next(iter(value.labels), "Registered LaTeX Version"),
        InputReplacementTargetKind.BASE_LATEX_VERSION,
    )


async def _run_preparation(
    callable_: PreparationCallable,
    *,
    subject: str,
    application_plan_id: str,
    now: datetime,
) -> tuple[ApplicationPreparationStatus, str | None]:
    try:
        value = await _resolve(
            callable_(
                RunApplicationPreparationCommand(
                    subject_id=subject,
                    application_plan_id=application_plan_id,
                    now=now,
                )
            )
        )
        if not isinstance(value, RunApplicationPreparationResult):
            raise ValueError("preparation result is invalid")
        return value.status, value.run.run_id if value.run is not None else None
    except (OSError, RuntimeError, TypeError, ValueError):
        return ApplicationPreparationStatus.FAILED, None


def _result(status, receipt, reason, message):
    return InputReplacementResolutionResult(status, receipt, reason, message)


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise ValueError("text value is invalid")
    return value.strip()


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


__all__ = [
    "INPUT_REPLACEMENT_RESOLUTION_CONTRACT_VERSION",
    "InputReplacementAction",
    "InputReplacementResolutionCommand",
    "InputReplacementResolutionReceipt",
    "InputReplacementResolutionReceiptRepository",
    "InputReplacementResolutionResult",
    "InputReplacementResolutionStatus",
    "SelectableInputReplacement",
    "get_selectable_input_replacements",
    "list_selectable_input_replacements",
    "resolve_input_replacement",
]
