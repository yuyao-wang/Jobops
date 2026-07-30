"""Read-only UI projection of the P2b5 Human Attention Queue."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionKind,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
    HumanAttentionResolutionCapability,
)


class HumanAttentionInboxUIStatus(StrEnum):
    LOADING = "LOADING"
    READY = "READY"
    EMPTY = "EMPTY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class HumanAttentionInboxUIItem:
    item_id: str
    application_plan_id: str
    job_id: str
    priority: str
    audience: str
    attention_kind: str
    resolution_capability: str
    attention_label: str
    required_action: str
    blocking: bool
    source_stage: str
    canonical_answer_key: str | None
    correction_target_id: str | None
    replacement_target_id: str | None
    unsupported_claim_correction_supported: bool
    latex_compilation_correction_supported: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class HumanAttentionInboxUIResult:
    status: HumanAttentionInboxUIStatus
    user_items: tuple[HumanAttentionInboxUIItem, ...]
    operator_items: tuple[HumanAttentionInboxUIItem, ...]
    item_count: int
    affected_plan_count: int
    refreshed_at: datetime
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_plan_count": self.affected_plan_count,
            "item_count": self.item_count,
            "message": self.message,
            "operator_items": [
                item.to_dict() for item in self.operator_items
            ],
            "refreshed_at": self.refreshed_at.isoformat(),
            "status": self.status.value,
            "user_items": [item.to_dict() for item in self.user_items],
        }


HumanAttentionQueueCallable = Callable[
    ...,
    HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult],
]


_KIND_LABELS = {
    HumanAttentionKind.USER_FACT_REQUIRED: "需要补充事实",
    HumanAttentionKind.USER_CHOICE_REQUIRED: "需要作出选择",
    HumanAttentionKind.USER_ATTESTATION_REQUIRED: "需要本人确认",
    HumanAttentionKind.MANUAL_REVIEW_REQUIRED: "需要人工检查结果",
    HumanAttentionKind.MATERIAL_CORRECTION_REQUIRED: "材料需要修正",
    HumanAttentionKind.INPUT_REPLACEMENT_REQUIRED: "输入需要替换",
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED: "系统需要处理",
    HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER: "未分类的系统阻断",
}

_SAFE_FAILURE_MESSAGES = {
    "INVALID_REQUEST": "待处理事项请求无效。",
    "RUN_LIST_INTEGRITY_FAILURE": "暂时无法读取准备记录。",
    "CURRENT_RUN_INTEGRITY_FAILURE": "暂时无法确定当前准备状态。",
    "APPLICATION_PLAN_NOT_FOUND": "相关申请计划暂时不可用。",
    "APPLICATION_PLAN_INTEGRITY_FAILURE": "相关申请计划无法安全读取。",
    "APPLICATION_PLAN_BINDING_MISMATCH": "相关申请计划绑定不一致。",
    "ANSWER_SET_NOT_FOUND": "相关申请答案暂时不可用。",
    "ANSWER_SET_INTEGRITY_FAILURE": "相关申请答案无法安全读取。",
    "ANSWER_SET_BINDING_MISMATCH": "相关申请答案绑定不一致。",
    "ATTENTION_MAPPING_FAILURE": "待处理事项暂时无法分类。",
}

_GENERIC_ACTIONS = {
    HumanAttentionKind.USER_FACT_REQUIRED: (
        "请补充或核实缺失的申请事实。"
    ),
    HumanAttentionKind.USER_CHOICE_REQUIRED: "请确认有效选项。",
    HumanAttentionKind.USER_ATTESTATION_REQUIRED: (
        "请本人审阅并确认相关声明。"
    ),
    HumanAttentionKind.MANUAL_REVIEW_REQUIRED: (
        "请人工检查准备结果并作出决定。"
    ),
    HumanAttentionKind.MATERIAL_CORRECTION_REQUIRED: (
        "当前材料需要修正后才能继续。"
    ),
    HumanAttentionKind.INPUT_REPLACEMENT_REQUIRED: (
        "请提供受支持且可读取的替代输入。"
    ),
    HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED: (
        "请由系统运维人员检查相关依赖或完整性状态。"
    ),
    HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER: (
        "当前阻断尚无安全的处理路径。"
    ),
}


def _safe_action(kind: HumanAttentionKind, action: str) -> str:
    lowered = action.casefold()
    unsafe_markers = (
        "/users/",
        "/private/",
        "\\users\\",
        "bearer ",
        "token=",
        "password",
        "credential",
        "private key",
        "traceback",
    )
    if any(marker in lowered for marker in unsafe_markers):
        return _GENERIC_ACTIONS[kind]
    return action


def _safe_capability(item: Any) -> str:
    capability = getattr(item, "resolution_capability", None)
    if isinstance(capability, HumanAttentionResolutionCapability):
        return capability.value
    return {
        HumanAttentionKind.USER_FACT_REQUIRED: (
            HumanAttentionResolutionCapability.PROVIDE_FACT
        ),
        HumanAttentionKind.USER_CHOICE_REQUIRED: (
            HumanAttentionResolutionCapability.MAKE_CHOICE
        ),
        HumanAttentionKind.USER_ATTESTATION_REQUIRED: (
            HumanAttentionResolutionCapability.ATTEST
        ),
        HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED: (
            HumanAttentionResolutionCapability.OPERATOR_REPAIR
        ),
    }.get(
        item.attention_kind,
        HumanAttentionResolutionCapability.NON_OVERRIDABLE,
    ).value


def _failed(
    *,
    now: datetime,
    message: str,
) -> HumanAttentionInboxUIResult:
    return HumanAttentionInboxUIResult(
        HumanAttentionInboxUIStatus.FAILED,
        (),
        (),
        0,
        0,
        now,
        message,
    )


def map_human_attention_queue(
    result: HumanAttentionQueueResult,
    *,
    subject_id: str,
    now: datetime,
) -> HumanAttentionInboxUIResult:
    """Preserve P2b5 order while removing internal bindings and diagnostics."""

    if (
        not isinstance(result, HumanAttentionQueueResult)
        or result.subject_id != subject_id
        or result.evaluated_at != now
    ):
        return _failed(
            now=now, message="待处理事项服务返回了无效结果。"
        )
    if result.status is HumanAttentionQueueStatus.FAILED:
        reason = getattr(result.reason_code, "value", "")
        return _failed(
            now=now,
            message=_SAFE_FAILURE_MESSAGES.get(
                reason, "暂时无法读取待处理事项。"
            ),
        )

    items = tuple(
        HumanAttentionInboxUIItem(
            item_id=item.item_id,
            application_plan_id=item.application_plan_id,
            job_id=item.job_id,
            priority=item.priority.value,
            audience=item.audience.value,
            attention_kind=item.attention_kind.value,
            resolution_capability=_safe_capability(item),
            attention_label=_KIND_LABELS[item.attention_kind],
            required_action=_safe_action(
                item.attention_kind, item.required_action
            ),
            blocking=item.blocking,
            source_stage=item.source_stage.value,
            canonical_answer_key=(
                item.canonical_answer_key.value
                if item.canonical_answer_key
                else None
            ),
            correction_target_id=(
                item.correction_target_ref.target_id
                if getattr(item, "correction_target_ref", None)
                is not None
                else None
            ),
            replacement_target_id=(
                item.replacement_target_ref.target_id
                if getattr(item, "replacement_target_ref", None)
                is not None
                else None
            ),
            unsupported_claim_correction_supported=(
                getattr(item, "fact_qa_finding_ref", None) is not None
                and getattr(item, "correction_target_ref", None) is not None
            ),
            latex_compilation_correction_supported=(
                item.source_stage.value == "RESUME_COMPILATION"
                and item.source_reason_code
                in {
                    "UNMANAGED_DEPENDENCY",
                    "COMPILATION_ERROR",
                }
                and getattr(item, "correction_target_ref", None) is not None
            ),
        )
        for item in result.items
    )
    user_items = tuple(
        item
        for item in items
        if item.audience == HumanAttentionAudience.USER.value
    )
    operator_items = tuple(
        item
        for item in items
        if item.audience == HumanAttentionAudience.OPERATOR.value
    )
    status = (
        HumanAttentionInboxUIStatus.EMPTY
        if not items
        else HumanAttentionInboxUIStatus.READY
    )
    message = (
        "目前没有需要你处理的事项。"
        if status is HumanAttentionInboxUIStatus.EMPTY
        else "待处理事项已刷新。"
    )
    return HumanAttentionInboxUIResult(
        status=status,
        user_items=user_items,
        operator_items=operator_items,
        item_count=result.item_count,
        affected_plan_count=result.affected_plan_count,
        refreshed_at=result.evaluated_at,
        message=message,
    )


class HumanAttentionInboxUIController:
    """Authenticated, read-only adapter over one P2b5 public callable."""

    def __init__(
        self,
        *,
        queue_reader: HumanAttentionQueueCallable,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(queue_reader) or not callable(clock):
            raise TypeError("queue_reader and clock must be callable")
        self._queue_reader = queue_reader
        self._clock = clock
        self._active: dict[
            str, asyncio.Task[HumanAttentionInboxUIResult]
        ] = {}

    async def load(
        self,
        *,
        context: AuthenticatedSubjectContext,
    ) -> HumanAttentionInboxUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        active = self._active.get(context.subject_id)
        if active is not None:
            return await asyncio.shield(active)

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        task = asyncio.create_task(
            self._invoke(subject_id=context.subject_id, now=now)
        )
        self._active[context.subject_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            current = self._active.get(context.subject_id)
            if current is task and task.done():
                self._active.pop(context.subject_id, None)

    async def _invoke(
        self,
        *,
        subject_id: str,
        now: datetime,
    ) -> HumanAttentionInboxUIResult:
        try:
            value = self._queue_reader(subject_id=subject_id, now=now)
            result = await value if inspect.isawaitable(value) else value
        except (OSError, RuntimeError, TypeError, ValueError):
            return _failed(
                now=now, message="待处理事项服务暂时不可用。"
            )
        return map_human_attention_queue(
            result, subject_id=subject_id, now=now
        )


__all__ = [
    "HumanAttentionInboxUIController",
    "HumanAttentionInboxUIItem",
    "HumanAttentionInboxUIResult",
    "HumanAttentionInboxUIStatus",
    "HumanAttentionQueueCallable",
    "map_human_attention_queue",
]
