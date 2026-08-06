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
    "RESOLVED": "The answer was saved safely.",
    "RESOLVED_AND_PREPARATION_COMPLETED": "The answer was saved and application preparation completed.",
    "RESOLVED_AND_PREPARATION_DEFERRED": "The answer was saved; other application items still need attention.",
    "UNCHANGED": "This answer was already resolved.",
    "ITEM_NOT_CURRENT": "This attention item is no longer current.",
    "DEFERRED_AMBIGUOUS_INPUT": "The response is ambiguous. Provide the fact, choice, or your decision directly.",
    "UNSUPPORTED_ITEM": "This item requires a different resolution path.",
    "FAILED": "The answer could not be saved safely.",
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
