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
    "RESOLVED": "The choice was saved safely.",
    "RESOLVED_AND_PREPARATION_COMPLETED": "The choice was saved and application preparation completed.",
    "RESOLVED_AND_PREPARATION_DEFERRED": "The choice was saved; other application items still need attention.",
    "UNCHANGED": "This choice was already resolved.",
    "ITEM_NOT_CURRENT": "This attention item is no longer current.",
    "DEFERRED_AMBIGUOUS_INPUT": "Select one resume or LaTeX version explicitly.",
    "OPTION_NOT_SELECTABLE": "The selected version is not currently available.",
    "UNSUPPORTED_ITEM": "This item requires a different resolution path.",
    "FAILED": "The choice could not be saved safely.",
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
