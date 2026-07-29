"""Subject-scoped read model for current preparation attention items."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from .application_answer_taxonomy import CanonicalApplicationAnswerKey
from .application_answers import (
    PreparedApplicationAnswerSet,
    PreparedApplicationAnswerSetReadStatus,
    PreparedApplicationAnswerSetRepository,
    UnresolvedAnswerReason,
)
from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .application_preparation_orchestrator import (
    ApplicationPreparationRun,
    ApplicationPreparationRunListStatus,
    ApplicationPreparationRunReadStatus,
    ApplicationPreparationRunRepository,
    ApplicationPreparationRunStatus,
    ApplicationPreparationStage,
)
from .job_prioritization import ProposedPriorityLevel


HUMAN_ATTENTION_MAPPING_VERSION = "human-attention-mapping-v1"
HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION = "human-attention-queue-v1"

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ITEM_ID_RE = re.compile(r"^human-attention-item-[a-f0-9]{64}$")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _clean_text(name: str, value: Any, maximum: int = 300) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
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


class HumanAttentionQueueStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class HumanAttentionKind(StrEnum):
    USER_FACT_REQUIRED = "USER_FACT_REQUIRED"
    USER_CHOICE_REQUIRED = "USER_CHOICE_REQUIRED"
    USER_ATTESTATION_REQUIRED = "USER_ATTESTATION_REQUIRED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    SYSTEM_OPERATOR_REQUIRED = "SYSTEM_OPERATOR_REQUIRED"


class HumanAttentionAudience(StrEnum):
    USER = "USER"
    OPERATOR = "OPERATOR"


class HumanAttentionReasonCode(StrEnum):
    MISSING_TRUSTED_FACT = "MISSING_TRUSTED_FACT"
    AMBIGUOUS_USER_CHOICE = "AMBIGUOUS_USER_CHOICE"
    PERSONAL_ATTESTATION = "PERSONAL_ATTESTATION"
    MANUAL_PREPARATION_REVIEW = "MANUAL_PREPARATION_REVIEW"
    SYSTEM_DEPENDENCY_UNAVAILABLE = "SYSTEM_DEPENDENCY_UNAVAILABLE"
    SYSTEM_INTEGRITY_OR_CONTRACT_FAILURE = (
        "SYSTEM_INTEGRITY_OR_CONTRACT_FAILURE"
    )
    POLICY_REQUIRES_OPERATOR = "POLICY_REQUIRES_OPERATOR"
    UNSUPPORTED_REQUIRED_ANSWER = "UNSUPPORTED_REQUIRED_ANSWER"
    UNKNOWN_DEFER_REASON = "UNKNOWN_DEFER_REASON"


class HumanAttentionQueueFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    RUN_LIST_INTEGRITY_FAILURE = "RUN_LIST_INTEGRITY_FAILURE"
    CURRENT_RUN_INTEGRITY_FAILURE = "CURRENT_RUN_INTEGRITY_FAILURE"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_BINDING_MISMATCH = (
        "APPLICATION_PLAN_BINDING_MISMATCH"
    )
    ANSWER_SET_NOT_FOUND = "ANSWER_SET_NOT_FOUND"
    ANSWER_SET_INTEGRITY_FAILURE = "ANSWER_SET_INTEGRITY_FAILURE"
    ANSWER_SET_BINDING_MISMATCH = "ANSWER_SET_BINDING_MISMATCH"
    ATTENTION_MAPPING_FAILURE = "ATTENTION_MAPPING_FAILURE"


@dataclass(frozen=True, slots=True)
class _AttentionMapping:
    kind: HumanAttentionKind
    audience: HumanAttentionAudience
    reason_code: HumanAttentionReasonCode
    required_action: str


_FACT = _AttentionMapping(
    HumanAttentionKind.USER_FACT_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionReasonCode.MISSING_TRUSTED_FACT,
    "Provide or verify the missing authoritative application fact.",
)
_CHOICE = _AttentionMapping(
    HumanAttentionKind.USER_CHOICE_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionReasonCode.AMBIGUOUS_USER_CHOICE,
    "Choose the valid option or resolve the ambiguous preparation input.",
)
_MANUAL = _AttentionMapping(
    HumanAttentionKind.MANUAL_REVIEW_REQUIRED,
    HumanAttentionAudience.USER,
    HumanAttentionReasonCode.MANUAL_PREPARATION_REVIEW,
    "Review the prepared content or layout and provide a decision.",
)
_OPERATOR = _AttentionMapping(
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
    HumanAttentionAudience.OPERATOR,
    HumanAttentionReasonCode.SYSTEM_DEPENDENCY_UNAVAILABLE,
    "Inspect the managed artifact, dependency, or preparation environment.",
)
_UNKNOWN = _AttentionMapping(
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
    HumanAttentionAudience.OPERATOR,
    HumanAttentionReasonCode.UNKNOWN_DEFER_REASON,
    "Classify the unmapped typed preparation reason before continuing.",
)


def _mapping_entries(
    stages: tuple[ApplicationPreparationStage, ...],
    statuses: tuple[str, ...],
    mapping: _AttentionMapping,
) -> dict[tuple[ApplicationPreparationStage, str], _AttentionMapping]:
    return {
        (stage, status): mapping
        for stage in stages
        for status in statuses
    }


_RUN_DEFER_MAPPINGS = {
    **_mapping_entries(
        (
            ApplicationPreparationStage.BASE_RESUME_SELECTION,
            ApplicationPreparationStage.RESUME_EVIDENCE,
            ApplicationPreparationStage.RESUME_TAILORING,
            ApplicationPreparationStage.COVER_LETTER_EVIDENCE,
            ApplicationPreparationStage.COVER_LETTER_DRAFT,
            ApplicationPreparationStage.APPLICATION_ANSWERS,
        ),
        (
            "DEFERRED_NO_RESUME",
            "DEFERRED_NO_EVIDENCE",
            "DEFERRED_INSUFFICIENT_EVIDENCE",
            "DEFERRED_NO_TRUSTED_FACTS",
        ),
        _FACT,
    ),
    **_mapping_entries(
        (
            ApplicationPreparationStage.BASE_RESUME_SELECTION,
            ApplicationPreparationStage.BASE_LATEX_SELECTION,
            ApplicationPreparationStage.APPLICATION_ANSWERS,
        ),
        ("DEFERRED_NEEDS_HUMAN",),
        _CHOICE,
    ),
    **_mapping_entries(
        (
            ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
            ApplicationPreparationStage.RESUME_TAILORING,
            ApplicationPreparationStage.RESUME_FACT_QA,
            ApplicationPreparationStage.LATEX_CONSTRUCTION,
            ApplicationPreparationStage.RESUME_VISUAL_QA,
            ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
            ApplicationPreparationStage.COVER_LETTER_DRAFT,
            ApplicationPreparationStage.COVER_LETTER_FACT_QA,
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        ),
        (
            "UNSUPPORTED",
            "UNREADABLE",
            "DEFERRED_NEEDS_HUMAN",
            "BLOCKED_UNSUPPORTED_CLAIM",
            "BLOCKED_BINDING_MISMATCH",
            "DEFERRED_ATTEMPTS_EXHAUSTED",
            "DEFERRED_LAYOUT_OVERFLOW",
        ),
        _MANUAL,
    ),
    **_mapping_entries(
        (
            ApplicationPreparationStage.LATEX_CONSTRUCTION,
            ApplicationPreparationStage.RESUME_COMPILATION,
            ApplicationPreparationStage.RESUME_VISUAL_QA,
            ApplicationPreparationStage.RESUME_PUBLICATION,
            ApplicationPreparationStage.RESUME_MANIFEST,
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
            ApplicationPreparationStage.COVER_LETTER_MANIFEST,
        ),
        (
            "DEFERRED_SOURCE_UNREADABLE",
            "DEFERRED_COMPILER_UNAVAILABLE",
            "DEFERRED_SOURCE_INCOMPLETE",
            "DEFERRED_COMPILATION_ERROR",
            "DEFERRED_RENDERER_UNAVAILABLE",
            "NOT_READY",
        ),
        _OPERATOR,
    ),
}


@dataclass(frozen=True, slots=True)
class HumanAttentionQueueItem:
    item_id: str
    contract_version: str
    mapping_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    priority: ProposedPriorityLevel
    source_preparation_run_id: str
    source_preparation_binding: str
    source_stage: ApplicationPreparationStage
    attention_kind: HumanAttentionKind
    audience: HumanAttentionAudience
    reason_code: HumanAttentionReasonCode
    source_reason_code: str
    canonical_answer_key: CanonicalApplicationAnswerKey | None
    blocking: bool
    required_action: str
    source_record_id: str
    source_event_time: datetime
    answer_set_id: str | None
    answer_set_content_hash: str | None
    item_content_hash: str

    def __post_init__(self) -> None:
        if self.contract_version != HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION:
            raise ValueError("attention queue contract is unsupported")
        if self.mapping_version != HUMAN_ATTENTION_MAPPING_VERSION:
            raise ValueError("attention mapping version is unsupported")
        _clean_text("subject_id", self.subject_id, 160)
        _clean_text("application_plan_id", self.application_plan_id, 180)
        _clean_text("job_id", self.job_id, 160)
        object.__setattr__(
            self, "priority", ProposedPriorityLevel(self.priority)
        )
        _clean_text(
            "source_preparation_run_id",
            self.source_preparation_run_id,
            200,
        )
        _require_hash(
            "source_preparation_binding",
            self.source_preparation_binding,
        )
        object.__setattr__(
            self,
            "source_stage",
            ApplicationPreparationStage(self.source_stage),
        )
        kind = HumanAttentionKind(self.attention_kind)
        audience = HumanAttentionAudience(self.audience)
        reason = HumanAttentionReasonCode(self.reason_code)
        object.__setattr__(self, "attention_kind", kind)
        object.__setattr__(self, "audience", audience)
        object.__setattr__(self, "reason_code", reason)
        if (
            audience is HumanAttentionAudience.OPERATOR
        ) != (
            kind is HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED
        ):
            raise ValueError("attention kind and audience conflict")
        _clean_text("source_reason_code", self.source_reason_code, 200)
        if self.canonical_answer_key is not None:
            object.__setattr__(
                self,
                "canonical_answer_key",
                CanonicalApplicationAnswerKey(
                    self.canonical_answer_key
                ),
            )
        if self.blocking is not True:
            raise ValueError("current attention items must be blocking")
        _clean_text("required_action", self.required_action, 300)
        _clean_text("source_record_id", self.source_record_id, 240)
        _require_aware("source_event_time", self.source_event_time)
        if (self.answer_set_id is None) != (
            self.answer_set_content_hash is None
        ):
            raise ValueError("answer-set item binding is incomplete")
        if self.answer_set_id is not None:
            _clean_text("answer_set_id", self.answer_set_id, 200)
            _require_hash(
                "answer_set_content_hash",
                self.answer_set_content_hash,
            )
        expected_id = "human-attention-item-" + _canonical_hash(
            self.identity_dict()
        )
        if (
            _ITEM_ID_RE.fullmatch(self.item_id) is None
            or self.item_id != expected_id
        ):
            raise ValueError("attention item ID is invalid")
        if self.item_content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("attention item content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "answer_set_content_hash": self.answer_set_content_hash,
            "answer_set_id": self.answer_set_id,
            "application_plan_id": self.application_plan_id,
            "attention_kind": self.attention_kind.value,
            "canonical_answer_key": (
                self.canonical_answer_key.value
                if self.canonical_answer_key
                else None
            ),
            "contract_version": self.contract_version,
            "mapping_version": self.mapping_version,
            "reason_code": self.reason_code.value,
            "source_preparation_binding": (
                self.source_preparation_binding
            ),
            "source_preparation_run_id": self.source_preparation_run_id,
            "source_record_id": self.source_record_id,
            "source_reason_code": self.source_reason_code,
            "source_stage": self.source_stage.value,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            "answer_set_content_hash": self.answer_set_content_hash,
            "answer_set_id": self.answer_set_id,
            "application_plan_id": self.application_plan_id,
            "attention_kind": self.attention_kind.value,
            "audience": self.audience.value,
            "blocking": self.blocking,
            "canonical_answer_key": (
                self.canonical_answer_key.value
                if self.canonical_answer_key
                else None
            ),
            "contract_version": self.contract_version,
            "item_id": self.item_id,
            "job_id": self.job_id,
            "mapping_version": self.mapping_version,
            "priority": self.priority.value,
            "reason_code": self.reason_code.value,
            "required_action": self.required_action,
            "source_event_time": _rfc3339(self.source_event_time),
            "source_preparation_binding": (
                self.source_preparation_binding
            ),
            "source_preparation_run_id": self.source_preparation_run_id,
            "source_reason_code": self.source_reason_code,
            "source_record_id": self.source_record_id,
            "source_stage": self.source_stage.value,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "item_content_hash": self.item_content_hash,
        }


@dataclass(frozen=True, slots=True)
class HumanAttentionQueueResult:
    status: HumanAttentionQueueStatus
    subject_id: str
    items: tuple[HumanAttentionQueueItem, ...]
    item_count: int
    user_item_count: int
    operator_item_count: int
    affected_plan_count: int
    queue_snapshot_hash: str | None
    evaluated_at: datetime
    reason_code: HumanAttentionQueueFailureReason | None
    message: str

    def __post_init__(self) -> None:
        status = HumanAttentionQueueStatus(self.status)
        object.__setattr__(self, "status", status)
        _clean_text("subject_id", self.subject_id, 160)
        _require_aware("evaluated_at", self.evaluated_at)
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, HumanAttentionQueueItem)
            for item in self.items
        ):
            raise TypeError("queue items must be typed")
        if (
            tuple(sorted(self.items, key=_item_sort_key)) != self.items
            or len({item.item_id for item in self.items})
            != len(self.items)
            or any(item.subject_id != self.subject_id for item in self.items)
        ):
            raise ValueError("queue item ordering or ownership is invalid")
        counts = (
            len(self.items),
            sum(
                item.audience is HumanAttentionAudience.USER
                for item in self.items
            ),
            sum(
                item.audience is HumanAttentionAudience.OPERATOR
                for item in self.items
            ),
            len({item.application_plan_id for item in self.items}),
        )
        if counts != (
            self.item_count,
            self.user_item_count,
            self.operator_item_count,
            self.affected_plan_count,
        ):
            raise ValueError("queue counts are invalid")
        if status is HumanAttentionQueueStatus.SUCCEEDED:
            if self.reason_code is not None:
                raise ValueError("successful queue cannot have a reason")
            _require_hash("queue_snapshot_hash", self.queue_snapshot_hash)
            expected_snapshot = _canonical_hash(
                {
                    "affected_plan_count": self.affected_plan_count,
                    "contract_version": (
                        HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION
                    ),
                    "item_count": self.item_count,
                    "item_hashes": [
                        item.item_content_hash for item in self.items
                    ],
                    "mapping_version": HUMAN_ATTENTION_MAPPING_VERSION,
                    "operator_item_count": self.operator_item_count,
                    "subject_id": self.subject_id,
                    "user_item_count": self.user_item_count,
                }
            )
            if self.queue_snapshot_hash != expected_snapshot:
                raise ValueError("queue snapshot hash is invalid")
        elif (
            self.reason_code is None
            or self.items
            or self.queue_snapshot_hash is not None
        ):
            raise ValueError("failed queue result is invalid")
        _clean_text("message", self.message, 300)


_PRIORITY_ORDER = {
    ProposedPriorityLevel.P0: 0,
    ProposedPriorityLevel.P1: 1,
    ProposedPriorityLevel.P2: 2,
    ProposedPriorityLevel.P3: 3,
}
_AUDIENCE_ORDER = {
    HumanAttentionAudience.USER: 0,
    HumanAttentionAudience.OPERATOR: 1,
}
_KIND_ORDER = {
    HumanAttentionKind.USER_ATTESTATION_REQUIRED: 0,
    HumanAttentionKind.USER_FACT_REQUIRED: 1,
    HumanAttentionKind.USER_CHOICE_REQUIRED: 2,
    HumanAttentionKind.MANUAL_REVIEW_REQUIRED: 3,
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED: 4,
}


def _item_sort_key(item: HumanAttentionQueueItem) -> tuple[Any, ...]:
    return (
        _PRIORITY_ORDER[item.priority],
        _AUDIENCE_ORDER[item.audience],
        _KIND_ORDER[item.attention_kind],
        item.source_event_time.astimezone(timezone.utc),
        item.application_plan_id,
        item.item_id,
    )


def _build_item(
    *,
    plan: ApplicationPlan,
    run: ApplicationPreparationRun,
    stage: ApplicationPreparationStage,
    mapping: _AttentionMapping,
    source_reason_code: str,
    required_action: str,
    source_record_id: str,
    source_event_time: datetime,
    canonical_answer_key: CanonicalApplicationAnswerKey | None = None,
    answer_set: PreparedApplicationAnswerSet | None = None,
) -> HumanAttentionQueueItem:
    identity = {
        "answer_set_content_hash": (
            answer_set.answer_set_content_hash if answer_set else None
        ),
        "answer_set_id": answer_set.answer_set_id if answer_set else None,
        "application_plan_id": plan.plan_id,
        "attention_kind": mapping.kind.value,
        "canonical_answer_key": (
            canonical_answer_key.value if canonical_answer_key else None
        ),
        "contract_version": HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION,
        "mapping_version": HUMAN_ATTENTION_MAPPING_VERSION,
        "reason_code": mapping.reason_code.value,
        "source_preparation_binding": run.preparation_binding,
        "source_preparation_run_id": run.run_id,
        "source_record_id": source_record_id,
        "source_reason_code": source_reason_code,
        "source_stage": stage.value,
    }
    item_id = "human-attention-item-" + _canonical_hash(identity)
    content = {
        "answer_set_content_hash": (
            answer_set.answer_set_content_hash if answer_set else None
        ),
        "answer_set_id": answer_set.answer_set_id if answer_set else None,
        "application_plan_id": plan.plan_id,
        "attention_kind": mapping.kind.value,
        "audience": mapping.audience.value,
        "blocking": True,
        "canonical_answer_key": (
            canonical_answer_key.value if canonical_answer_key else None
        ),
        "contract_version": HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION,
        "item_id": item_id,
        "job_id": plan.job_id,
        "mapping_version": HUMAN_ATTENTION_MAPPING_VERSION,
        "priority": plan.priority_level.value,
        "reason_code": mapping.reason_code.value,
        "required_action": required_action,
        "source_event_time": _rfc3339(source_event_time),
        "source_preparation_binding": run.preparation_binding,
        "source_preparation_run_id": run.run_id,
        "source_reason_code": source_reason_code,
        "source_record_id": source_record_id,
        "source_stage": stage.value,
        "subject_id": plan.subject_id,
    }
    return HumanAttentionQueueItem(
        item_id=item_id,
        contract_version=HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION,
        mapping_version=HUMAN_ATTENTION_MAPPING_VERSION,
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        priority=plan.priority_level,
        source_preparation_run_id=run.run_id,
        source_preparation_binding=run.preparation_binding,
        source_stage=stage,
        attention_kind=mapping.kind,
        audience=mapping.audience,
        reason_code=mapping.reason_code,
        source_reason_code=source_reason_code,
        canonical_answer_key=canonical_answer_key,
        blocking=True,
        required_action=required_action,
        source_record_id=source_record_id,
        source_event_time=source_event_time,
        answer_set_id=answer_set.answer_set_id if answer_set else None,
        answer_set_content_hash=(
            answer_set.answer_set_content_hash if answer_set else None
        ),
        item_content_hash=_canonical_hash(content),
    )


def _answer_mapping(
    reason: UnresolvedAnswerReason,
) -> _AttentionMapping:
    if reason is UnresolvedAnswerReason.MISSING_FACT:
        return _FACT
    if reason is UnresolvedAnswerReason.REQUIRES_USER_CHOICE:
        return _CHOICE
    if reason is UnresolvedAnswerReason.REQUIRES_ATTESTATION:
        return _AttentionMapping(
            HumanAttentionKind.USER_ATTESTATION_REQUIRED,
            HumanAttentionAudience.USER,
            HumanAttentionReasonCode.PERSONAL_ATTESTATION,
            "Personally review and attest to the application statement.",
        )
    if reason is UnresolvedAnswerReason.POLICY_FORBIDS_AUTOMATION:
        return _AttentionMapping(
            HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
            HumanAttentionAudience.OPERATOR,
            HumanAttentionReasonCode.POLICY_REQUIRES_OPERATOR,
            "Review the formal policy restriction before continuing.",
        )
    return _AttentionMapping(
        HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
        HumanAttentionAudience.OPERATOR,
        HumanAttentionReasonCode.UNSUPPORTED_REQUIRED_ANSWER,
        "Classify the unsupported required answer before continuing.",
    )


def _failure(
    *,
    subject_id: str,
    now: datetime,
    reason: HumanAttentionQueueFailureReason,
) -> HumanAttentionQueueResult:
    return HumanAttentionQueueResult(
        status=HumanAttentionQueueStatus.FAILED,
        subject_id=subject_id,
        items=(),
        item_count=0,
        user_item_count=0,
        operator_item_count=0,
        affected_plan_count=0,
        queue_snapshot_hash=None,
        evaluated_at=now,
        reason_code=reason,
        message=f"Human attention queue failed: {reason.value}.",
    )


def build_current_human_attention_queue(
    *,
    subject_id: str,
    now: datetime,
    run_repository: ApplicationPreparationRunRepository,
    application_plan_repository: ApplicationPlanRepository,
    answer_set_repository: PreparedApplicationAnswerSetRepository,
) -> HumanAttentionQueueResult:
    try:
        subject = _clean_text("subject_id", subject_id, 160)
        evaluated = _require_aware("now", now)
    except (TypeError, ValueError):
        return _failure(
            subject_id=(
                subject_id.strip()
                if isinstance(subject_id, str) and subject_id.strip()
                else "invalid-subject"
            ),
            now=(
                now
                if isinstance(now, datetime) and now.tzinfo is not None
                else datetime.min.replace(tzinfo=timezone.utc)
            ),
            reason=HumanAttentionQueueFailureReason.INVALID_REQUEST,
        )
    try:
        listed = run_repository.list_for_subject(subject_id=subject)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            subject_id=subject,
            now=evaluated,
            reason=(
                HumanAttentionQueueFailureReason
                .RUN_LIST_INTEGRITY_FAILURE
            ),
        )
    if (
        listed.status
        is not ApplicationPreparationRunListStatus.SUCCEEDED
    ):
        return _failure(
            subject_id=subject,
            now=evaluated,
            reason=(
                HumanAttentionQueueFailureReason
                .RUN_LIST_INTEGRITY_FAILURE
            ),
        )
    listed_ids = {run.run_id for run in listed.runs}
    current_runs: list[ApplicationPreparationRun] = []
    for plan_id in sorted(
        {run.application_plan_id for run in listed.runs}
    ):
        try:
            current = run_repository.find_current_for_plan(
                subject_id=subject, application_plan_id=plan_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .CURRENT_RUN_INTEGRITY_FAILURE
                ),
            )
        if (
            current.status
            is not ApplicationPreparationRunReadStatus.FOUND
            or current.run is None
            or current.run.run_id not in listed_ids
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .CURRENT_RUN_INTEGRITY_FAILURE
                ),
            )
        current_runs.append(current.run)

    items: list[HumanAttentionQueueItem] = []
    for run in current_runs:
        try:
            plan_read = application_plan_repository.get(
                run.application_plan_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .APPLICATION_PLAN_INTEGRITY_FAILURE
                ),
            )
        if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .APPLICATION_PLAN_NOT_FOUND
                ),
            )
        if (
            plan_read.status is not ApplicationPlanReadStatus.FOUND
            or not isinstance(plan_read.plan, ApplicationPlan)
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .APPLICATION_PLAN_INTEGRITY_FAILURE
                ),
            )
        plan = plan_read.plan
        if (
            plan.subject_id != subject
            or run.subject_id != subject
            or plan.plan_id != run.application_plan_id
            or plan.job_id != run.job_id
            or plan.job_revision != run.job_revision
            or plan.job_content_hash != run.job_content_hash
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .APPLICATION_PLAN_BINDING_MISMATCH
                ),
            )
        if run.overall_status is ApplicationPreparationRunStatus.DEFERRED:
            final_stage = run.stage_results[-1]
            mapping = _RUN_DEFER_MAPPINGS.get(
                (run.deferred_stage, final_stage.public_status), _UNKNOWN
            )
            items.append(
                _build_item(
                    plan=plan,
                    run=run,
                    stage=run.deferred_stage,
                    mapping=mapping,
                    source_reason_code=run.deferred_reason,
                    required_action=mapping.required_action,
                    source_record_id=(
                        final_stage.result_id or run.run_id
                    ),
                    source_event_time=run.completed_at,
                )
            )
            continue
        if run.overall_status is ApplicationPreparationRunStatus.FAILED:
            final_stage = run.stage_results[-1]
            mapping = _AttentionMapping(
                HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
                HumanAttentionAudience.OPERATOR,
                HumanAttentionReasonCode
                .SYSTEM_INTEGRITY_OR_CONTRACT_FAILURE,
                "Inspect the failed preparation contract or managed state.",
            )
            items.append(
                _build_item(
                    plan=plan,
                    run=run,
                    stage=run.failed_stage,
                    mapping=mapping,
                    source_reason_code=run.failed_reason,
                    required_action=mapping.required_action,
                    source_record_id=(
                        final_stage.result_id or run.run_id
                    ),
                    source_event_time=run.completed_at,
                )
            )
            continue
        if not run.human_attention_required:
            continue
        if run.final_prepared_application_answer_set_id is None:
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_BINDING_MISMATCH
                ),
            )
        try:
            answer_read = answer_set_repository.get(
                subject_id=subject,
                answer_set_id=(
                    run.final_prepared_application_answer_set_id
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_INTEGRITY_FAILURE
                ),
            )
        if (
            answer_read.status
            is PreparedApplicationAnswerSetReadStatus.NOT_FOUND
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason.ANSWER_SET_NOT_FOUND
                ),
            )
        if (
            answer_read.status
            is not PreparedApplicationAnswerSetReadStatus.FOUND
            or not isinstance(
                answer_read.answer_set, PreparedApplicationAnswerSet
            )
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_INTEGRITY_FAILURE
                ),
            )
        answer_set = answer_read.answer_set
        if (
            answer_set.answer_set_id
            != run.final_prepared_application_answer_set_id
            or answer_set.subject_id != subject
            or answer_set.application_plan_id != plan.plan_id
            or answer_set.job_id != plan.job_id
            or answer_set.job_revision != plan.job_revision
            or answer_set.job_content_hash != plan.job_content_hash
        ):
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_BINDING_MISMATCH
                ),
            )
        blocking = tuple(
            unresolved
            for unresolved in answer_set.unresolved_items
            if unresolved.blocking
        )
        if not blocking:
            return _failure(
                subject_id=subject,
                now=evaluated,
                reason=(
                    HumanAttentionQueueFailureReason
                    .ANSWER_SET_BINDING_MISMATCH
                ),
            )
        for unresolved in blocking:
            mapping = _answer_mapping(unresolved.reason)
            items.append(
                _build_item(
                    plan=plan,
                    run=run,
                    stage=ApplicationPreparationStage.APPLICATION_ANSWERS,
                    mapping=mapping,
                    source_reason_code=unresolved.reason.value,
                    required_action=unresolved.required_human_action,
                    source_record_id=(
                        unresolved.unresolved_content_hash
                    ),
                    source_event_time=answer_set.prepared_at,
                    canonical_answer_key=unresolved.canonical_key,
                    answer_set=answer_set,
                )
            )

    ordered = tuple(sorted(items, key=_item_sort_key))
    snapshot_content = {
        "affected_plan_count": len(
            {item.application_plan_id for item in ordered}
        ),
        "contract_version": HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION,
        "item_count": len(ordered),
        "item_hashes": [item.item_content_hash for item in ordered],
        "mapping_version": HUMAN_ATTENTION_MAPPING_VERSION,
        "operator_item_count": sum(
            item.audience is HumanAttentionAudience.OPERATOR
            for item in ordered
        ),
        "subject_id": subject,
        "user_item_count": sum(
            item.audience is HumanAttentionAudience.USER
            for item in ordered
        ),
    }
    return HumanAttentionQueueResult(
        status=HumanAttentionQueueStatus.SUCCEEDED,
        subject_id=subject,
        items=ordered,
        item_count=snapshot_content["item_count"],
        user_item_count=snapshot_content["user_item_count"],
        operator_item_count=snapshot_content["operator_item_count"],
        affected_plan_count=snapshot_content["affected_plan_count"],
        queue_snapshot_hash=_canonical_hash(snapshot_content),
        evaluated_at=evaluated,
        reason_code=None,
        message="Current human attention queue was derived.",
    )


__all__ = [
    "HUMAN_ATTENTION_MAPPING_VERSION",
    "HUMAN_ATTENTION_QUEUE_CONTRACT_VERSION",
    "HumanAttentionAudience",
    "HumanAttentionKind",
    "HumanAttentionQueueFailureReason",
    "HumanAttentionQueueItem",
    "HumanAttentionQueueResult",
    "HumanAttentionQueueStatus",
    "HumanAttentionReasonCode",
    "build_current_human_attention_queue",
]
