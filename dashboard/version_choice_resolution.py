"""Authenticated UI adapter for S3g2 Resume/LaTeX choice resolution."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.version_choice_resolution import (
    VersionChoiceResolutionCommand,
    VersionChoiceResolutionResult,
    VersionChoiceResolutionStatus,
)


VersionChoiceCallable = Callable[
    ...,
    VersionChoiceResolutionResult
    | Awaitable[VersionChoiceResolutionResult],
]


@dataclass(frozen=True, slots=True)
class VersionChoiceResolutionUIResult:
    status: str
    receipt_id: str | None
    preparation_run_id: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "preparation_run_id": self.preparation_run_id,
            "receipt_id": self.receipt_id,
            "status": self.status,
        }


_MESSAGES = {
    "RESOLVED": "选择已安全保存。",
    "RESOLVED_AND_PREPARATION_COMPLETED": "选择已保存，申请准备已继续完成。",
    "RESOLVED_AND_PREPARATION_DEFERRED": "选择已保存；申请仍有其他待处理事项。",
    "UNCHANGED": "该选择已处理，无需重复保存。",
    "ITEM_NOT_CURRENT": "该待处理事项已不是当前状态。",
    "DEFERRED_AMBIGUOUS_INPUT": "请明确指定一个简历或 LaTeX 版本。",
    "OPTION_NOT_SELECTABLE": "指定版本当前不可选择。",
    "UNSUPPORTED_ITEM": "该事项需要其他处理方式。",
    "FAILED": "选择暂时无法安全保存。",
}


class VersionChoiceResolutionUIController:
    def __init__(
        self,
        *,
        resolution_callable: VersionChoiceCallable,
        clock: Callable[[], datetime],
    ) -> None:
        self._callable = resolution_callable
        self._clock = clock

    async def resolve(
        self,
        *,
        context: AuthenticatedSubjectContext,
        attention_item_id: str,
        user_message: str,
    ) -> VersionChoiceResolutionUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        try:
            value = self._callable(
                VersionChoiceResolutionCommand(
                    subject_id=context.subject_id,
                    attention_item_id=attention_item_id,
                    user_message=user_message,
                    now=now,
                )
            )
            result = await value if inspect.isawaitable(value) else value
            if not isinstance(result, VersionChoiceResolutionResult):
                raise ValueError("invalid version choice result")
        except (OSError, RuntimeError, TypeError, ValueError):
            result = VersionChoiceResolutionResult(
                VersionChoiceResolutionStatus.FAILED,
                None,
                "RESOLUTION_FAILED",
                "The version choice could not be resolved safely.",
            )
        receipt = result.receipt
        return VersionChoiceResolutionUIResult(
            status=result.status.value,
            receipt_id=receipt.receipt_id if receipt else None,
            preparation_run_id=(
                receipt.preparation_run_id if receipt else None
            ),
            message=_MESSAGES[result.status.value],
        )


__all__ = [
    "VersionChoiceCallable",
    "VersionChoiceResolutionUIController",
    "VersionChoiceResolutionUIResult",
]
