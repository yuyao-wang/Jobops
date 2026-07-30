"""Authenticated, read-only UI adapter for Material Correction Targets."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.material_correction_target import (
    MaterialCorrectionTargetResult,
    MaterialCorrectionTargetStatus,
)


MaterialCorrectionTargetCallable = Callable[
    ...,
    MaterialCorrectionTargetResult
    | Awaitable[MaterialCorrectionTargetResult],
]


@dataclass(frozen=True, slots=True)
class MaterialCorrectionTargetUIResult:
    status: MaterialCorrectionTargetStatus
    target: dict[str, Any] | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "status": self.status.value,
            "target": self.target,
        }


_MESSAGES = {
    MaterialCorrectionTargetStatus.AVAILABLE: "修正目标已加载。",
    MaterialCorrectionTargetStatus.ITEM_NOT_CURRENT: "该待处理事项已不再是当前事项。",
    MaterialCorrectionTargetStatus.NOT_CORRECTABLE: "该事项不支持材料修正。",
    MaterialCorrectionTargetStatus.TARGET_STALE: "材料版本已变化，请刷新待处理事项。",
    MaterialCorrectionTargetStatus.TARGET_INCOMPLETE: "当前材料缺少安全修正所需的正式标识。",
    MaterialCorrectionTargetStatus.PREVIEW_UNAVAILABLE: "当前材料预览不可用，不能进行盲目修改。",
    MaterialCorrectionTargetStatus.FAILED: "暂时无法安全读取修正目标。",
}


class MaterialCorrectionTargetUIController:
    """Resolve one target using only the authenticated subject context."""

    def __init__(
        self, *, target_reader: MaterialCorrectionTargetCallable
    ) -> None:
        if not callable(target_reader):
            raise TypeError("target_reader must be callable")
        self._target_reader = target_reader

    async def get(
        self,
        *,
        context: AuthenticatedSubjectContext,
        attention_item_id: str,
    ) -> MaterialCorrectionTargetUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if (
            not isinstance(attention_item_id, str)
            or not attention_item_id.strip()
            or len(attention_item_id) > 240
        ):
            raise ValueError("attention_item_id is outside the UI contract")
        try:
            value = self._target_reader(
                subject_id=context.subject_id,
                attention_item_id=attention_item_id.strip(),
            )
            result = await value if inspect.isawaitable(value) else value
        except (OSError, RuntimeError, TypeError, ValueError):
            result = MaterialCorrectionTargetResult(
                MaterialCorrectionTargetStatus.FAILED, None
            )
        if not isinstance(result, MaterialCorrectionTargetResult):
            result = MaterialCorrectionTargetResult(
                MaterialCorrectionTargetStatus.FAILED, None
            )
        target = None
        if result.safe_target is not None:
            safe = result.safe_target
            target = {
                "attempt_count": safe.attempt_count,
                "attempt_limit": safe.attempt_limit,
                "material_kind": safe.material_kind,
                "preview_reference": safe.preview_reference,
                "required_action": safe.required_action,
                "summary": safe.summary,
                "target_id": safe.target_id,
                "target_kind": safe.target_kind.value,
                "title": safe.title,
            }
        return MaterialCorrectionTargetUIResult(
            status=result.status,
            target=target,
            message=_MESSAGES[result.status],
        )


__all__ = [
    "MaterialCorrectionTargetCallable",
    "MaterialCorrectionTargetUIController",
    "MaterialCorrectionTargetUIResult",
]
