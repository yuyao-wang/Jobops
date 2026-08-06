"""Authenticated, read-only UI adapter for Input Replacement Targets."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.input_replacement_target import (
    InputReplacementTargetResult,
    InputReplacementTargetStatus,
)


InputReplacementTargetCallable = Callable[
    ...,
    InputReplacementTargetResult | Awaitable[InputReplacementTargetResult],
]


@dataclass(frozen=True, slots=True)
class InputReplacementTargetUIResult:
    status: InputReplacementTargetStatus
    target: dict[str, Any] | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "status": self.status.value,
            "target": self.target,
        }


_MESSAGES = {
    InputReplacementTargetStatus.AVAILABLE: "The current input is ready for replacement.",
    InputReplacementTargetStatus.ITEM_NOT_CURRENT: "This attention item is no longer current.",
    InputReplacementTargetStatus.NOT_REPLACEABLE: "This item does not support input replacement.",
    InputReplacementTargetStatus.TARGET_STALE: "The input changed. Refresh the attention item.",
    InputReplacementTargetStatus.TARGET_INCOMPLETE: (
        "The blocker is missing the input identity required for a safe replacement."
    ),
    InputReplacementTargetStatus.FAILED: "The replacement target could not be read safely.",
}


class InputReplacementTargetUIController:
    def __init__(
        self, *, target_reader: InputReplacementTargetCallable
    ) -> None:
        if not callable(target_reader):
            raise TypeError("target_reader must be callable")
        self._target_reader = target_reader

    async def get(
        self,
        *,
        context: AuthenticatedSubjectContext,
        attention_item_id: str,
    ) -> InputReplacementTargetUIResult:
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
            result = InputReplacementTargetResult(
                InputReplacementTargetStatus.FAILED, None
            )
        if not isinstance(result, InputReplacementTargetResult):
            result = InputReplacementTargetResult(
                InputReplacementTargetStatus.FAILED, None
            )
        target = None
        if result.safe_target is not None:
            safe = result.safe_target
            target = {
                "display_name": safe.display_name,
                "input_kind": safe.input_kind,
                "media_type": safe.media_type,
                "replacement_methods": [
                    item.value for item in safe.replacement_methods
                ],
                "required_action": safe.required_action,
                "target_id": safe.target_id,
                "target_kind": safe.target_kind.value,
                "version": safe.version,
            }
        return InputReplacementTargetUIResult(
            status=result.status,
            target=target,
            message=_MESSAGES[result.status],
        )


__all__ = [
    "InputReplacementTargetCallable",
    "InputReplacementTargetUIController",
    "InputReplacementTargetUIResult",
]
