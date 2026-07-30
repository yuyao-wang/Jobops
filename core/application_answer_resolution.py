"""Resolve current application-answer attention items from explicit user text."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol

from .application_answer_taxonomy import (
    CanonicalAnswerValueType,
    CanonicalApplicationAnswerKey,
    canonical_application_answer_definition,
)
from .application_attestation import (
    ApplicationAttestationDecision,
    PlanScopedApplicationAttestation,
    PlanScopedApplicationAttestationRepository,
)
from .application_fact_writer import (
    ApplicationFactWriteResult,
    ApplicationFactWriteStatus,
    WriteUserConfirmedApplicationFactCommand,
)
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
from .private_home import PrivateHome


APPLICATION_ANSWER_RESOLUTION_CONTRACT_VERSION = (
    "application-answer-resolution-v1"
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


class ApplicationAnswerResolutionStatus(StrEnum):
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
    UNSUPPORTED_ITEM = "UNSUPPORTED_ITEM"
    FAILED = "FAILED"


class ApplicationAnswerResolutionKind(StrEnum):
    FACT = "FACT"
    USER_CHOICE = "USER_CHOICE"
    ATTESTATION = "ATTESTATION"


@dataclass(frozen=True, slots=True)
class ApplicationAnswerResolutionCommand:
    subject_id: str
    attention_item_id: str
    user_message: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ApplicationAnswerResolutionParserRequest:
    user_message: str
    attention_kind: HumanAttentionKind
    canonical_key: CanonicalApplicationAnswerKey
    value_type: CanonicalAnswerValueType
    required_action: str


@dataclass(frozen=True, slots=True)
class ApplicationAnswerResolutionProposal:
    canonical_key: CanonicalApplicationAnswerKey
    resolution_kind: ApplicationAnswerResolutionKind
    value: Any = None
    attestation_decision: ApplicationAttestationDecision | None = None
    evidence_text: str = ""
    unambiguous: bool = False


class ApplicationAnswerResolutionParserPort(Protocol):
    def parse(
        self, request: ApplicationAnswerResolutionParserRequest
    ) -> (
        ApplicationAnswerResolutionProposal
        | Awaitable[ApplicationAnswerResolutionProposal]
    ): ...


@dataclass(frozen=True, slots=True)
class ApplicationAnswerResolutionReceipt:
    receipt_id: str
    subject_id: str
    attention_item_id: str
    application_plan_id: str
    canonical_key: CanonicalApplicationAnswerKey
    resolution_kind: ApplicationAnswerResolutionKind
    user_message_hash: str
    value: Any
    attestation_decision: ApplicationAttestationDecision | None
    authoritative_record_id: str
    preparation_run_id: str | None
    preparation_status: str
    contract_version: str
    resolved_at: datetime
    receipt_content_hash: str

    def content_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "attestation_decision": (
                self.attestation_decision.value
                if self.attestation_decision
                else None
            ),
            "attention_item_id": self.attention_item_id,
            "authoritative_record_id": self.authoritative_record_id,
            "canonical_key": self.canonical_key.value,
            "contract_version": self.contract_version,
            "preparation_run_id": self.preparation_run_id,
            "preparation_status": self.preparation_status,
            "resolution_kind": self.resolution_kind.value,
            "subject_id": self.subject_id,
            "user_message_hash": self.user_message_hash,
            "value": self.value,
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
        canonical_key: CanonicalApplicationAnswerKey,
        resolution_kind: ApplicationAnswerResolutionKind,
        user_message_hash: str,
        value: Any,
        attestation_decision: ApplicationAttestationDecision | None,
        authoritative_record_id: str,
        preparation_run_id: str | None,
        preparation_status: str,
        resolved_at: datetime,
    ) -> "ApplicationAnswerResolutionReceipt":
        identity = {
            "application_plan_id": application_plan_id,
            "attestation_decision": (
                attestation_decision.value
                if attestation_decision
                else None
            ),
            "attention_item_id": attention_item_id,
            "authoritative_record_id": authoritative_record_id,
            "canonical_key": canonical_key.value,
            "contract_version": APPLICATION_ANSWER_RESOLUTION_CONTRACT_VERSION,
            "preparation_run_id": preparation_run_id,
            "preparation_status": preparation_status,
            "resolution_kind": resolution_kind.value,
            "subject_id": subject_id,
            "user_message_hash": user_message_hash,
            "value": value,
        }
        digest = _hash(identity)
        return cls(
            receipt_id="application-answer-resolution-" + digest,
            subject_id=subject_id,
            attention_item_id=attention_item_id,
            application_plan_id=application_plan_id,
            canonical_key=canonical_key,
            resolution_kind=resolution_kind,
            user_message_hash=user_message_hash,
            value=value,
            attestation_decision=attestation_decision,
            authoritative_record_id=authoritative_record_id,
            preparation_run_id=preparation_run_id,
            preparation_status=preparation_status,
            contract_version=APPLICATION_ANSWER_RESOLUTION_CONTRACT_VERSION,
            resolved_at=resolved_at,
            receipt_content_hash=digest,
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "ApplicationAnswerResolutionReceipt":
        decision = payload.get("attestation_decision")
        value = cls(
            receipt_id=str(payload["receipt_id"]),
            subject_id=str(payload["subject_id"]),
            attention_item_id=str(payload["attention_item_id"]),
            application_plan_id=str(payload["application_plan_id"]),
            canonical_key=CanonicalApplicationAnswerKey(
                payload["canonical_key"]
            ),
            resolution_kind=ApplicationAnswerResolutionKind(
                payload["resolution_kind"]
            ),
            user_message_hash=str(payload["user_message_hash"]),
            value=payload.get("value"),
            attestation_decision=(
                ApplicationAttestationDecision(decision)
                if decision
                else None
            ),
            authoritative_record_id=str(
                payload["authoritative_record_id"]
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
            != APPLICATION_ANSWER_RESOLUTION_CONTRACT_VERSION
            or value.receipt_content_hash != _hash(value.content_dict())
            or value.receipt_id
            != "application-answer-resolution-"
            + value.receipt_content_hash
        ):
            raise ValueError("resolution receipt integrity failure")
        return value


class ApplicationAnswerResolutionReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        key = hashlib.sha256(subject_id.encode()).hexdigest()
        return (
            self._home.paths.preparation
            / "application-answer-resolutions"
            / ("subject-" + key)
        )

    def save(self, receipt: ApplicationAnswerResolutionReceipt) -> bool:
        path = self._directory(receipt.subject_id) / (
            receipt.receipt_id + ".json"
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable resolution receipt conflict")
        return created

    def find_replay(
        self,
        *,
        subject_id: str,
        attention_item_id: str,
        user_message_hash: str,
    ) -> ApplicationAnswerResolutionReceipt | None:
        directory = self._home.contained_path(
            self._directory(subject_id)
        )
        if not directory.exists():
            return None
        matches: list[ApplicationAnswerResolutionReceipt] = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            path = self._home.contained_path(path)
            receipt = ApplicationAnswerResolutionReceipt.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if receipt.subject_id != subject_id:
                raise ValueError("resolution receipt subject mismatch")
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
class ApplicationAnswerResolutionResult:
    status: ApplicationAnswerResolutionStatus
    receipt: ApplicationAnswerResolutionReceipt | None
    reason_code: str | None
    message: str


QueueReader = Callable[..., HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult]]
PreparationCallable = Callable[
    ...,
    RunApplicationPreparationResult
    | Awaitable[RunApplicationPreparationResult],
]


def _ambiguous(message: str) -> bool:
    normalized = " ".join(message.casefold().split())
    return any(
        phrase in normalized
        for phrase in (
            "maybe",
            "probably",
            "whatever",
            "you decide",
            "not sure",
            "应该",
            "大概",
            "随便",
            "你决定",
            "不确定",
        )
    )


def _expected_kind(kind: HumanAttentionKind) -> ApplicationAnswerResolutionKind:
    return {
        HumanAttentionKind.USER_FACT_REQUIRED: (
            ApplicationAnswerResolutionKind.FACT
        ),
        HumanAttentionKind.USER_CHOICE_REQUIRED: (
            ApplicationAnswerResolutionKind.USER_CHOICE
        ),
        HumanAttentionKind.USER_ATTESTATION_REQUIRED: (
            ApplicationAnswerResolutionKind.ATTESTATION
        ),
    }[kind]


def _valid_proposal(
    request: ApplicationAnswerResolutionParserRequest,
    proposal: ApplicationAnswerResolutionProposal,
) -> bool:
    if (
        not isinstance(proposal, ApplicationAnswerResolutionProposal)
        or proposal.canonical_key is not request.canonical_key
        or proposal.resolution_kind is not _expected_kind(
            request.attention_kind
        )
        or not proposal.unambiguous
        or not proposal.evidence_text.strip()
        or proposal.evidence_text not in request.user_message
        or _ambiguous(request.user_message)
    ):
        return False
    evidence = proposal.evidence_text.casefold()
    if request.value_type is CanonicalAnswerValueType.ATTESTATION:
        if proposal.value is not None:
            return False
        confirmed = (
            "i confirm",
            "personally confirm",
            "i agree",
            "i consent",
            "本人确认",
            "我确认",
            "我同意",
        )
        declined = (
            "i decline",
            "i do not agree",
            "i do not consent",
            "本人拒绝",
            "我拒绝",
            "我不同意",
        )
        expected = (
            confirmed
            if proposal.attestation_decision
            is ApplicationAttestationDecision.CONFIRMED
            else declined
        )
        return (
            proposal.attestation_decision
            in {
                ApplicationAttestationDecision.CONFIRMED,
                ApplicationAttestationDecision.DECLINED,
            }
            and any(token in evidence for token in expected)
        )
    if proposal.attestation_decision is not None:
        return False
    value = proposal.value
    if request.value_type in {
        CanonicalAnswerValueType.TEXT,
        CanonicalAnswerValueType.ENUM,
    }:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and value.casefold() in request.user_message.casefold()
        )
    if request.value_type is CanonicalAnswerValueType.BOOLEAN:
        if type(value) is not bool:
            return False
        yes = ("yes", "true", "是", "可以", "愿意")
        no = (
            "no",
            "false",
            "not ",
            "do not",
            "don't",
            "否",
            "不可以",
            "不愿意",
            "不需要",
        )
        context = {
            CanonicalApplicationAnswerKey.WORK_AUTHORIZATION: (
                "authorized to work",
                "work authorization",
                "工作许可",
                "合法工作",
            ),
            CanonicalApplicationAnswerKey.SPONSORSHIP: (
                "sponsorship",
                "visa sponsor",
                "签证担保",
            ),
            CanonicalApplicationAnswerKey.RELOCATION: (
                "relocat",
                "搬迁",
            ),
        }.get(request.canonical_key, ())
        return (
            bool(context)
            and any(token in evidence for token in context)
            and any(token in evidence for token in (yes if value else no))
        )
    if request.value_type is CanonicalAnswerValueType.MULTI_SELECT:
        return (
            isinstance(value, tuple)
            and bool(value)
            and len(value) == len(set(value))
            and all(
                isinstance(item, str)
                and item
                and item.casefold() in request.user_message.casefold()
                for item in value
            )
        )
    return False


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def resolve_application_answer(
    command: ApplicationAnswerResolutionCommand,
    *,
    queue_reader: QueueReader,
    parser: ApplicationAnswerResolutionParserPort,
    fact_write_service: Any,
    attestation_repository: PlanScopedApplicationAttestationRepository,
    preparation_callable: PreparationCallable,
    receipt_repository: ApplicationAnswerResolutionReceiptRepository,
) -> ApplicationAnswerResolutionResult:
    try:
        subject = command.subject_id.strip()
        item_id = command.attention_item_id.strip()
        message = command.user_message.strip()
        _time(command.now)
        if not subject or not item_id or not message:
            raise ValueError
        message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        queue = await _resolve(
            queue_reader(subject_id=subject, now=command.now)
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
            return ApplicationAnswerResolutionResult(
                ApplicationAnswerResolutionStatus.ITEM_NOT_CURRENT,
                None,
                "ITEM_NOT_CURRENT",
                "The attention item is no longer current.",
            )
        replay = receipt_repository.find_replay(
            subject_id=subject,
            attention_item_id=item_id,
            user_message_hash=message_hash,
        )
        if replay is not None:
            return ApplicationAnswerResolutionResult(
                ApplicationAnswerResolutionStatus.UNCHANGED,
                replay,
                None,
                "This application-answer resolution is unchanged.",
            )
        if (
            item.audience is not HumanAttentionAudience.USER
            or item.source_stage
            is not ApplicationPreparationStage.APPLICATION_ANSWERS
            or item.attention_kind
            not in {
                HumanAttentionKind.USER_FACT_REQUIRED,
                HumanAttentionKind.USER_CHOICE_REQUIRED,
                HumanAttentionKind.USER_ATTESTATION_REQUIRED,
            }
            or item.canonical_answer_key is None
        ):
            return ApplicationAnswerResolutionResult(
                ApplicationAnswerResolutionStatus.UNSUPPORTED_ITEM,
                None,
                "UNSUPPORTED_ITEM",
                "This attention item requires a different resolution path.",
            )
        definition = canonical_application_answer_definition(
            item.canonical_answer_key
        )
        request = ApplicationAnswerResolutionParserRequest(
            user_message=message,
            attention_kind=item.attention_kind,
            canonical_key=item.canonical_answer_key,
            value_type=definition.value_type,
            required_action=item.required_action,
        )
        proposal = await _resolve(parser.parse(request))
        if not _valid_proposal(request, proposal):
            return ApplicationAnswerResolutionResult(
                ApplicationAnswerResolutionStatus.DEFERRED_AMBIGUOUS_INPUT,
                None,
                "AMBIGUOUS_INPUT",
                "The response is not explicit enough to save safely.",
            )
        if proposal.resolution_kind is ApplicationAnswerResolutionKind.ATTESTATION:
            attestation = PlanScopedApplicationAttestation.create(
                subject_id=subject,
                application_plan_id=item.application_plan_id,
                canonical_key=item.canonical_answer_key,
                statement=item.required_action,
                statement_version=item.source_record_id,
                decision=proposal.attestation_decision,
                source_attention_item_id=item.item_id,
                user_message_hash=message_hash,
                decided_at=command.now,
            )
            attestation_repository.save(attestation)
            authoritative_id = attestation.attestation_id
            value = None
            decision = proposal.attestation_decision
        else:
            write: ApplicationFactWriteResult = (
                fact_write_service.write_user_confirmed(
                    WriteUserConfirmedApplicationFactCommand(
                        subject_id=subject,
                        canonical_key=item.canonical_answer_key,
                        value=proposal.value,
                        source_attention_item_id=item.item_id,
                        user_message_hash=message_hash,
                        recorded_at=command.now,
                        allowed_scope={},
                    )
                )
            )
            if write.status is ApplicationFactWriteStatus.FAILED or not write.record_id:
                raise ValueError("authoritative fact write failed")
            authoritative_id = write.record_id
            value = proposal.value
            decision = None
        try:
            preparation = await _resolve(
                preparation_callable(
                    RunApplicationPreparationCommand(
                        subject_id=subject,
                        application_plan_id=item.application_plan_id,
                        now=command.now,
                    )
                )
            )
            if not isinstance(preparation, RunApplicationPreparationResult):
                raise ValueError("preparation result is invalid")
            preparation_status = preparation.status
            run_id = preparation.run.run_id if preparation.run else None
            preparation_reason = (
                preparation.reason_code.value
                if preparation.reason_code is not None
                else None
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            preparation = None
            preparation_status = ApplicationPreparationStatus.FAILED
            preparation_reason = "PREPARATION_RERUN_FAILED"
            run_id = None
        receipt = ApplicationAnswerResolutionReceipt.create(
            subject_id=subject,
            attention_item_id=item.item_id,
            application_plan_id=item.application_plan_id,
            canonical_key=item.canonical_answer_key,
            resolution_kind=proposal.resolution_kind,
            user_message_hash=message_hash,
            value=value,
            attestation_decision=decision,
            authoritative_record_id=authoritative_id,
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
                ApplicationAnswerResolutionStatus
                .RESOLVED_AND_PREPARATION_COMPLETED
            )
        elif preparation_status is ApplicationPreparationStatus.DEFERRED:
            status = (
                ApplicationAnswerResolutionStatus
                .RESOLVED_AND_PREPARATION_DEFERRED
            )
        else:
            status = ApplicationAnswerResolutionStatus.FAILED
        return ApplicationAnswerResolutionResult(
            status,
            receipt,
            (
                preparation_reason
            ),
            (
                "The answer was saved and preparation was rerun."
                if status is not ApplicationAnswerResolutionStatus.FAILED
                else "The answer was saved, but preparation failed."
            ),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return ApplicationAnswerResolutionResult(
            ApplicationAnswerResolutionStatus.FAILED,
            None,
            "RESOLUTION_FAILED",
            "The application answer could not be resolved safely.",
        )


__all__ = [
    "APPLICATION_ANSWER_RESOLUTION_CONTRACT_VERSION",
    "ApplicationAnswerResolutionCommand",
    "ApplicationAnswerResolutionKind",
    "ApplicationAnswerResolutionParserPort",
    "ApplicationAnswerResolutionParserRequest",
    "ApplicationAnswerResolutionProposal",
    "ApplicationAnswerResolutionReceipt",
    "ApplicationAnswerResolutionReceiptRepository",
    "ApplicationAnswerResolutionResult",
    "ApplicationAnswerResolutionStatus",
    "resolve_application_answer",
]
