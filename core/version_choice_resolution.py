"""Conversational resolution of plan-scoped Resume and LaTeX choices."""

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
from typing import Any, Mapping, Protocol

from .application_preparation_orchestrator import (
    ApplicationPreparationStage,
    ApplicationPreparationStatus,
    RunApplicationPreparationCommand,
    RunApplicationPreparationResult,
)
from .human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionKind,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
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


VERSION_CHOICE_RESOLUTION_CONTRACT_VERSION = (
    "version-choice-resolution-v1"
)


def _json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class VersionChoiceResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    RESOLVED_AND_PREPARATION_COMPLETED = (
        "RESOLVED_AND_PREPARATION_COMPLETED"
    )
    RESOLVED_AND_PREPARATION_DEFERRED = (
        "RESOLVED_AND_PREPARATION_DEFERRED"
    )
    UNCHANGED = "UNCHANGED"
    ITEM_NOT_CURRENT = "ITEM_NOT_CURRENT"
    DEFERRED_AMBIGUOUS_INPUT = "DEFERRED_AMBIGUOUS_INPUT"
    OPTION_NOT_SELECTABLE = "OPTION_NOT_SELECTABLE"
    UNSUPPORTED_ITEM = "UNSUPPORTED_ITEM"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class VersionChoiceResolutionCommand:
    subject_id: str
    attention_item_id: str
    user_message: str
    now: datetime


@dataclass(frozen=True, slots=True)
class SelectableVersionChoice:
    option_id: str
    display_labels: tuple[str, ...]
    override_kind: PlanScopedVersionOverrideKind

    def parser_dict(self) -> dict[str, Any]:
        return {
            "display_labels": list(self.display_labels),
            "option_id": self.option_id,
            "option_kind": self.override_kind.value,
        }


@dataclass(frozen=True, slots=True)
class VersionChoiceResolutionParserRequest:
    user_message: str
    attention_kind: HumanAttentionKind
    required_action: str
    options: tuple[SelectableVersionChoice, ...]


@dataclass(frozen=True, slots=True)
class VersionChoiceResolutionParserProposal:
    selected_option_id: str | None
    unambiguous: bool


class VersionChoiceResolutionParserPort(Protocol):
    def parse(
        self, request: VersionChoiceResolutionParserRequest
    ) -> (
        VersionChoiceResolutionParserProposal
        | Awaitable[VersionChoiceResolutionParserProposal]
    ): ...


@dataclass(frozen=True, slots=True)
class VersionChoiceResolutionReceipt:
    receipt_id: str
    subject_id: str
    attention_item_id: str
    application_plan_id: str
    override_kind: PlanScopedVersionOverrideKind
    selected_option_id: str
    previous_automatic_decision_id: str | None
    source_stage: ApplicationPreparationStage
    source_record_id: str
    source_preparation_run_id: str
    user_message_hash: str
    override_record_id: str
    override_contract_version: str
    preparation_run_id: str | None
    preparation_status: str
    contract_version: str
    resolved_at: datetime
    receipt_content_hash: str

    def content_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "contract_version": self.contract_version,
            "override_contract_version": self.override_contract_version,
            "override_kind": self.override_kind.value,
            "override_record_id": self.override_record_id,
            "preparation_run_id": self.preparation_run_id,
            "preparation_status": self.preparation_status,
            "previous_automatic_decision_id": (
                self.previous_automatic_decision_id
            ),
            "selected_option_id": self.selected_option_id,
            "source_preparation_run_id": self.source_preparation_run_id,
            "source_record_id": self.source_record_id,
            "source_stage": self.source_stage.value,
            "subject_id": self.subject_id,
            "user_message_hash": self.user_message_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "receipt_content_hash": self.receipt_content_hash,
            "receipt_id": self.receipt_id,
            "resolved_at": _time(self.resolved_at),
        }

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        attention_item_id: str,
        application_plan_id: str,
        override_kind: PlanScopedVersionOverrideKind,
        selected_option_id: str,
        previous_automatic_decision_id: str | None,
        source_stage: ApplicationPreparationStage,
        source_record_id: str,
        source_preparation_run_id: str,
        user_message_hash: str,
        override: PlanScopedVersionOverride,
        preparation_run_id: str | None,
        preparation_status: str,
        resolved_at: datetime,
    ) -> "VersionChoiceResolutionReceipt":
        content = {
            "application_plan_id": application_plan_id,
            "attention_item_id": attention_item_id,
            "contract_version": VERSION_CHOICE_RESOLUTION_CONTRACT_VERSION,
            "override_contract_version": override.contract_version,
            "override_kind": override_kind.value,
            "override_record_id": override.override_id,
            "preparation_run_id": preparation_run_id,
            "preparation_status": preparation_status,
            "previous_automatic_decision_id": (
                previous_automatic_decision_id
            ),
            "selected_option_id": selected_option_id,
            "source_preparation_run_id": source_preparation_run_id,
            "source_record_id": source_record_id,
            "source_stage": source_stage.value,
            "subject_id": subject_id,
            "user_message_hash": user_message_hash,
        }
        digest = _hash(content)
        return cls(
            receipt_id="version-choice-resolution-" + digest,
            subject_id=subject_id,
            attention_item_id=attention_item_id,
            application_plan_id=application_plan_id,
            override_kind=override_kind,
            selected_option_id=selected_option_id,
            previous_automatic_decision_id=previous_automatic_decision_id,
            source_stage=source_stage,
            source_record_id=source_record_id,
            source_preparation_run_id=source_preparation_run_id,
            user_message_hash=user_message_hash,
            override_record_id=override.override_id,
            override_contract_version=override.contract_version,
            preparation_run_id=preparation_run_id,
            preparation_status=preparation_status,
            contract_version=VERSION_CHOICE_RESOLUTION_CONTRACT_VERSION,
            resolved_at=resolved_at,
            receipt_content_hash=digest,
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "VersionChoiceResolutionReceipt":
        value = cls(
            receipt_id=str(payload["receipt_id"]),
            subject_id=str(payload["subject_id"]),
            attention_item_id=str(payload["attention_item_id"]),
            application_plan_id=str(payload["application_plan_id"]),
            override_kind=PlanScopedVersionOverrideKind(
                payload["override_kind"]
            ),
            selected_option_id=str(payload["selected_option_id"]),
            previous_automatic_decision_id=payload.get(
                "previous_automatic_decision_id"
            ),
            source_stage=ApplicationPreparationStage(
                payload["source_stage"]
            ),
            source_record_id=str(payload["source_record_id"]),
            source_preparation_run_id=str(
                payload["source_preparation_run_id"]
            ),
            user_message_hash=str(payload["user_message_hash"]),
            override_record_id=str(payload["override_record_id"]),
            override_contract_version=str(
                payload["override_contract_version"]
            ),
            preparation_run_id=payload.get("preparation_run_id"),
            preparation_status=str(payload["preparation_status"]),
            contract_version=str(payload["contract_version"]),
            resolved_at=datetime.fromisoformat(
                str(payload["resolved_at"]).replace("Z", "+00:00")
            ),
            receipt_content_hash=str(payload["receipt_content_hash"]),
        )
        if (
            value.contract_version
            != VERSION_CHOICE_RESOLUTION_CONTRACT_VERSION
            or value.receipt_content_hash != _hash(value.content_dict())
            or value.receipt_id
            != "version-choice-resolution-" + value.receipt_content_hash
        ):
            raise ValueError("version choice receipt integrity failure")
        _time(value.resolved_at)
        return value


class VersionChoiceResolutionReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        key = hashlib.sha256(subject_id.encode()).hexdigest()
        return (
            self._home.paths.preparation
            / "version-choice-resolutions"
            / ("subject-" + key)
        )

    def save(self, receipt: VersionChoiceResolutionReceipt) -> bool:
        path = self._directory(receipt.subject_id) / (
            receipt.receipt_id + ".json"
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable version choice receipt conflict")
        return created

    def find_replay(
        self,
        *,
        subject_id: str,
        attention_item_id: str,
        user_message_hash: str,
    ) -> VersionChoiceResolutionReceipt | None:
        directory = self._home.contained_path(
            self._directory(subject_id)
        )
        if not directory.exists():
            return None
        matches: list[VersionChoiceResolutionReceipt] = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            path = self._home.contained_path(path)
            receipt = VersionChoiceResolutionReceipt.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if receipt.subject_id != subject_id:
                raise ValueError("version choice receipt subject mismatch")
            if (
                receipt.attention_item_id == attention_item_id
                and receipt.user_message_hash == user_message_hash
            ):
                matches.append(receipt)
        return (
            max(matches, key=lambda item: item.receipt_id)
            if matches
            else None
        )


@dataclass(frozen=True, slots=True)
class VersionChoiceResolutionResult:
    status: VersionChoiceResolutionStatus
    receipt: VersionChoiceResolutionReceipt | None
    reason_code: str | None
    message: str


QueueReader = Callable[..., HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult]]
PreparationCallable = Callable[
    ...,
    Awaitable[RunApplicationPreparationResult],
]


def _ambiguous(message: str) -> bool:
    text = " ".join(message.casefold().split())
    return any(
        phrase in text
        for phrase in (
            "whatever",
            "you decide",
            "use the best",
            "probably",
            "随便",
            "你决定",
            "用最好的",
            "大概",
        )
    )


def _kind_for_stage(
    stage: ApplicationPreparationStage,
) -> PlanScopedVersionOverrideKind:
    return {
        ApplicationPreparationStage.BASE_RESUME_SELECTION: (
            PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE
        ),
        ApplicationPreparationStage.BASE_LATEX_SELECTION: (
            PlanScopedVersionOverrideKind.LATEX_VERSION_OVERRIDE
        ),
    }[stage]


def _safe_label(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    label = " ".join(value.split())
    lowered = label.casefold()
    if (
        not label
        or len(label) > 120
        or label.startswith(("/", "\\", "~"))
        or "/users/" in lowered
        or "\\users\\" in lowered
        or "://" in label
    ):
        return None
    return label


def _resume_options(
    provider: ResumeCandidateProvider, subject_id: str
) -> tuple[SelectableVersionChoice, ...]:
    listed = provider.list_selectable(subject_id)
    if (
        listed.status is not ResumeCandidateListStatus.SUCCEEDED
        or listed.subject_id != subject_id
    ):
        raise ValueError("resume candidate provider failed")
    options = []
    for item in listed.candidates:
        if (
            not isinstance(item, ResumeCandidate)
            or item.subject_id != subject_id
        ):
            raise ValueError("resume candidate binding mismatch")
        options.append(
            SelectableVersionChoice(
                item.resume_id,
                tuple(
                    label
                    for label in (_safe_label(item.display_name),)
                    if label is not None
                ),
                PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE,
            )
        )
    return tuple(sorted(options, key=lambda item: item.option_id))


def _latex_options(
    provider: ResumeLatexVersionProvider, subject_id: str
) -> tuple[SelectableVersionChoice, ...]:
    listed = provider.list_selectable(subject_id)
    if (
        listed.status is not ResumeLatexVersionListStatus.SUCCEEDED
        or listed.subject_id != subject_id
    ):
        raise ValueError("LaTeX version provider failed")
    options = []
    for item in listed.versions:
        if (
            not isinstance(item, ResumeLatexVersion)
            or item.subject_id != subject_id
        ):
            raise ValueError("LaTeX version binding mismatch")
        labels = tuple(
            sorted(
                {
                    *(
                        label
                        for raw in item.labels
                        if (label := _safe_label(raw)) is not None
                    ),
                },
                key=str.casefold,
            )
        )
        options.append(
            SelectableVersionChoice(
                item.latex_version_id,
                labels,
                PlanScopedVersionOverrideKind.LATEX_VERSION_OVERRIDE,
            )
        )
    return tuple(sorted(options, key=lambda item: item.option_id))


def _deterministic_match(
    message: str, options: tuple[SelectableVersionChoice, ...]
) -> SelectableVersionChoice | None:
    folded = message.casefold()

    def contains_label(label: str) -> bool:
        normalized = " ".join(label.casefold().split())
        return bool(normalized) and (
            folded.strip() == normalized
            or re.search(
                rf"(?<!\w){re.escape(normalized)}(?!\w)", folded
            )
            is not None
        )

    matches = {
        option.option_id: option
        for option in options
        if option.option_id.casefold() in folded
        or any(
            contains_label(label)
            for label in option.display_labels
            if label.strip()
        )
    }
    return next(iter(matches.values())) if len(matches) == 1 else None


def _previous_decision_id(source_record_id: str) -> str | None:
    if source_record_id.startswith(
        ("resume-selection-", "base-latex-selection-")
    ):
        return source_record_id
    return None


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def resolve_version_choice(
    command: VersionChoiceResolutionCommand,
    *,
    queue_reader: QueueReader,
    resume_candidate_provider: ResumeCandidateProvider,
    latex_version_provider: ResumeLatexVersionProvider,
    parser: VersionChoiceResolutionParserPort | None,
    override_repository: PlanScopedVersionOverrideRepository,
    preparation_callable: PreparationCallable,
    receipt_repository: VersionChoiceResolutionReceiptRepository,
) -> VersionChoiceResolutionResult:
    try:
        subject = command.subject_id.strip()
        item_id = command.attention_item_id.strip()
        message = command.user_message.strip()
        _time(command.now)
        if not subject or not item_id or not message:
            raise ValueError("resolution input is incomplete")
        message_hash = hashlib.sha256(message.encode()).hexdigest()
        queue = await _resolve(
            queue_reader(subject_id=subject, now=command.now)
        )
        if (
            not isinstance(queue, HumanAttentionQueueResult)
            or queue.status is not HumanAttentionQueueStatus.SUCCEEDED
            or queue.subject_id != subject
        ):
            raise ValueError("current attention queue is unavailable")
        replay = receipt_repository.find_replay(
            subject_id=subject,
            attention_item_id=item_id,
            user_message_hash=message_hash,
        )
        item = next(
            (candidate for candidate in queue.items if candidate.item_id == item_id),
            None,
        )
        if item is None:
            if replay is not None and replay.preparation_status != "FAILED":
                return VersionChoiceResolutionResult(
                    VersionChoiceResolutionStatus.UNCHANGED,
                    replay,
                    None,
                    "This version choice is unchanged.",
                )
            return VersionChoiceResolutionResult(
                VersionChoiceResolutionStatus.ITEM_NOT_CURRENT,
                None,
                "ITEM_NOT_CURRENT",
                "The attention item is no longer current.",
            )
        if replay is not None:
            return VersionChoiceResolutionResult(
                VersionChoiceResolutionStatus.UNCHANGED,
                replay,
                None,
                "This version choice is unchanged.",
            )
        if (
            item.audience is not HumanAttentionAudience.USER
            or item.attention_kind
            is not HumanAttentionKind.USER_CHOICE_REQUIRED
            or item.source_stage
            not in {
                ApplicationPreparationStage.BASE_RESUME_SELECTION,
                ApplicationPreparationStage.BASE_LATEX_SELECTION,
            }
        ):
            return VersionChoiceResolutionResult(
                VersionChoiceResolutionStatus.UNSUPPORTED_ITEM,
                None,
                "UNSUPPORTED_ITEM",
                "This attention item requires a different resolution path.",
            )
        if _ambiguous(message):
            return VersionChoiceResolutionResult(
                VersionChoiceResolutionStatus.DEFERRED_AMBIGUOUS_INPUT,
                None,
                "AMBIGUOUS_INPUT",
                "The response does not identify one selectable option.",
            )
        kind = _kind_for_stage(item.source_stage)
        options = (
            _resume_options(resume_candidate_provider, subject)
            if kind
            is PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE
            else _latex_options(latex_version_provider, subject)
        )
        if not options:
            return VersionChoiceResolutionResult(
                VersionChoiceResolutionStatus.OPTION_NOT_SELECTABLE,
                None,
                "NO_SELECTABLE_OPTIONS",
                "No current selectable option is available.",
            )
        selected = _deterministic_match(message, options)
        if selected is None and parser is not None:
            proposal = await _resolve(
                parser.parse(
                    VersionChoiceResolutionParserRequest(
                        user_message=message,
                        attention_kind=item.attention_kind,
                        required_action=item.required_action,
                        options=options,
                    )
                )
            )
            if not isinstance(
                proposal, VersionChoiceResolutionParserProposal
            ) or not proposal.unambiguous:
                return VersionChoiceResolutionResult(
                    VersionChoiceResolutionStatus
                    .DEFERRED_AMBIGUOUS_INPUT,
                    None,
                    "AMBIGUOUS_INPUT",
                    "The response does not identify one selectable option.",
                )
            selected = next(
                (
                    option
                    for option in options
                    if option.option_id == proposal.selected_option_id
                ),
                None,
            )
            if selected is None:
                return VersionChoiceResolutionResult(
                    VersionChoiceResolutionStatus.OPTION_NOT_SELECTABLE,
                    None,
                    "OPTION_NOT_SELECTABLE",
                    "The selected option is not currently selectable.",
                )
        if selected is None:
            return VersionChoiceResolutionResult(
                VersionChoiceResolutionStatus.DEFERRED_AMBIGUOUS_INPUT,
                None,
                "AMBIGUOUS_INPUT",
                "The response does not identify one selectable option.",
            )
        previous = override_repository.get_current(
            subject_id=subject,
            application_plan_id=item.application_plan_id,
            override_kind=kind,
        )
        override = PlanScopedVersionOverride.create(
            subject_id=subject,
            application_plan_id=item.application_plan_id,
            override_kind=kind,
            selected_option_id=selected.option_id,
            source_attention_item_id=item.item_id,
            source_stage=item.source_stage.value,
            source_record_id=item.source_record_id,
            user_message_hash=message_hash,
            previous_override_id=(
                previous.override_id if previous is not None else None
            ),
            created_at=command.now,
        )
        override_repository.save(override)
        try:
            preparation = await preparation_callable(
                RunApplicationPreparationCommand(
                    subject_id=subject,
                    application_plan_id=item.application_plan_id,
                    now=command.now,
                )
            )
            if not isinstance(preparation, RunApplicationPreparationResult):
                raise ValueError("preparation result is invalid")
            preparation_status = preparation.status
            run_id = preparation.run.run_id if preparation.run else None
            reason = (
                preparation.reason_code.value
                if preparation.reason_code is not None
                else None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            preparation_status = ApplicationPreparationStatus.FAILED
            run_id = None
            reason = "PREPARATION_RERUN_FAILED"
        receipt = VersionChoiceResolutionReceipt.create(
            subject_id=subject,
            attention_item_id=item.item_id,
            application_plan_id=item.application_plan_id,
            override_kind=kind,
            selected_option_id=selected.option_id,
            previous_automatic_decision_id=_previous_decision_id(
                item.source_record_id
            ),
            source_stage=item.source_stage,
            source_record_id=item.source_record_id,
            source_preparation_run_id=item.source_preparation_run_id,
            user_message_hash=message_hash,
            override=override,
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
                VersionChoiceResolutionStatus
                .RESOLVED_AND_PREPARATION_COMPLETED
            )
        elif preparation_status is ApplicationPreparationStatus.DEFERRED:
            status = (
                VersionChoiceResolutionStatus
                .RESOLVED_AND_PREPARATION_DEFERRED
            )
        else:
            status = VersionChoiceResolutionStatus.FAILED
        return VersionChoiceResolutionResult(
            status,
            receipt,
            reason,
            (
                "The version choice was saved and preparation was rerun."
                if status is not VersionChoiceResolutionStatus.FAILED
                else "The version choice was saved, but preparation failed."
            ),
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return VersionChoiceResolutionResult(
            VersionChoiceResolutionStatus.FAILED,
            None,
            "RESOLUTION_FAILED",
            "The version choice could not be resolved safely.",
        )


__all__ = [
    "SelectableVersionChoice",
    "VERSION_CHOICE_RESOLUTION_CONTRACT_VERSION",
    "VersionChoiceResolutionCommand",
    "VersionChoiceResolutionParserPort",
    "VersionChoiceResolutionParserProposal",
    "VersionChoiceResolutionParserRequest",
    "VersionChoiceResolutionReceipt",
    "VersionChoiceResolutionReceiptRepository",
    "VersionChoiceResolutionResult",
    "VersionChoiceResolutionStatus",
    "resolve_version_choice",
]
