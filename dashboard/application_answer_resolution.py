"""Authenticated UI adapter for S3g1 application-answer resolution."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from core.application_answer_resolution import (
    ApplicationAnswerResolutionCommand,
    ApplicationAnswerResolutionResult,
    ApplicationAnswerResolutionStatus,
)
from core.authenticated_subject import AuthenticatedSubjectContext


ResolutionCallable = Callable[
    ...,
    ApplicationAnswerResolutionResult
    | Awaitable[ApplicationAnswerResolutionResult],
]


@dataclass(frozen=True, slots=True)
class ApplicationAnswerResolutionUIResult:
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


_SAFE_MESSAGES = {
    "RESOLVED": "回答已安全保存。",
    "RESOLVED_AND_PREPARATION_COMPLETED": "回答已保存，申请准备已继续完成。",
    "RESOLVED_AND_PREPARATION_DEFERRED": "回答已保存；申请仍有其他待处理事项。",
    "UNCHANGED": "该回答已处理，无需重复保存。",
    "ITEM_NOT_CURRENT": "该待处理事项已不是当前状态。",
    "DEFERRED_AMBIGUOUS_INPUT": "回复不够明确，请直接给出事实、选择或本人决定。",
    "UNSUPPORTED_ITEM": "该事项需要其他处理方式。",
    "FAILED": "回答暂时无法安全保存。",
}


class ApplicationAnswerResolutionUIController:
    def __init__(
        self,
        *,
        resolution_callable: ResolutionCallable,
        clock: Callable[[], datetime],
    ) -> None:
        self._callable = resolution_callable
        self._clock = clock
        self._active: dict[str, asyncio.Task[Any]] = {}

    async def resolve(
        self,
        *,
        context: AuthenticatedSubjectContext,
        attention_item_id: str,
        user_message: str,
    ) -> ApplicationAnswerResolutionUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if attention_item_id in self._active:
            result = await asyncio.shield(self._active[attention_item_id])
        else:
            now = self._clock()
            if now.tzinfo is None:
                raise ValueError("clock must be timezone-aware")
            task = asyncio.create_task(
                self._invoke(
                    ApplicationAnswerResolutionCommand(
                        subject_id=context.subject_id,
                        attention_item_id=attention_item_id,
                        user_message=user_message,
                        now=now,
                    )
                )
            )
            self._active[attention_item_id] = task
            try:
                result = await asyncio.shield(task)
            finally:
                if task.done():
                    self._active.pop(attention_item_id, None)
        receipt = result.receipt
        return ApplicationAnswerResolutionUIResult(
            status=result.status.value,
            receipt_id=receipt.receipt_id if receipt else None,
            preparation_run_id=(
                receipt.preparation_run_id if receipt else None
            ),
            message=_SAFE_MESSAGES[result.status.value],
        )

    async def _invoke(
        self, command: ApplicationAnswerResolutionCommand
    ) -> ApplicationAnswerResolutionResult:
        try:
            value = self._callable(command)
            result = await value if inspect.isawaitable(value) else value
            if not isinstance(result, ApplicationAnswerResolutionResult):
                raise ValueError(
                    "resolution service returned an invalid result"
                )
            return result
        except (OSError, RuntimeError, TypeError, ValueError):
            return ApplicationAnswerResolutionResult(
                ApplicationAnswerResolutionStatus.FAILED,
                None,
                "RESOLUTION_FAILED",
                "The application answer could not be resolved safely.",
            )


__all__ = [
    "ApplicationAnswerResolutionUIController",
    "ApplicationAnswerResolutionUIResult",
    "ResolutionCallable",
]
