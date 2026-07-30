"""Authenticated UI adapter for selecting an existing replacement input."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime

from core.authenticated_subject import AuthenticatedSubjectContext
from core.input_replacement_resolution import (
    InputReplacementAction,
    InputReplacementResolutionCommand,
    InputReplacementResolutionResult,
    SelectableInputReplacement,
)


InputReplacementResolutionCallable = Callable[
    ...,
    InputReplacementResolutionResult
    | Awaitable[InputReplacementResolutionResult],
]
InputReplacementOptionsCallable = Callable[
    ...,
    tuple[SelectableInputReplacement, ...]
    | Awaitable[tuple[SelectableInputReplacement, ...]],
]


class InputReplacementResolutionUIController:
    def __init__(
        self,
        *,
        resolution_callable: InputReplacementResolutionCallable,
        options_callable: InputReplacementOptionsCallable,
        clock: Callable[[], datetime],
    ) -> None:
        if not all(
            callable(value)
            for value in (resolution_callable, options_callable, clock)
        ):
            raise TypeError("replacement UI dependencies must be callable")
        self._resolution_callable = resolution_callable
        self._options_callable = options_callable
        self._clock = clock

    async def options(
        self,
        *,
        context: AuthenticatedSubjectContext,
        attention_item_id: str,
    ) -> dict:
        _context(context)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        value = self._options_callable(
            subject_id=context.subject_id,
            attention_item_id=attention_item_id,
            now=now,
        )
        options = await value if inspect.isawaitable(value) else value
        if not isinstance(options, tuple) or any(
            not isinstance(item, SelectableInputReplacement)
            for item in options
        ):
            raise ValueError("replacement options are invalid")
        return {
            "options": [
                {
                    "display_name": item.display_name,
                    "option_id": item.option_id,
                    "option_version": item.option_version,
                    "target_kind": item.target_kind.value,
                }
                for item in options
            ],
            "status": (
                "AVAILABLE" if options else "NO_EXISTING_REPLACEMENT"
            ),
        }

    async def resolve(
        self,
        *,
        context: AuthenticatedSubjectContext,
        attention_item_id: str,
        action: InputReplacementAction,
        replacement_option_id: str,
    ) -> InputReplacementResolutionResult:
        _context(context)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        value = self._resolution_callable(
            InputReplacementResolutionCommand(
                subject_id=context.subject_id,
                attention_item_id=attention_item_id,
                action=InputReplacementAction(action),
                replacement_option_id=replacement_option_id,
                now=now,
            )
        )
        result = await value if inspect.isawaitable(value) else value
        if not isinstance(result, InputReplacementResolutionResult):
            raise ValueError("replacement service returned an invalid result")
        return result


def _context(value: AuthenticatedSubjectContext) -> None:
    if not isinstance(value, AuthenticatedSubjectContext):
        raise TypeError("context must be authenticated")


__all__ = [
    "InputReplacementOptionsCallable",
    "InputReplacementResolutionCallable",
    "InputReplacementResolutionUIController",
]
