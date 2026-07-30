"""Authenticated single-file UI adapter for S3g5b2."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime

from core.authenticated_subject import AuthenticatedSubjectContext
from core.new_base_latex_version_replacement import (
    NewBaseLatexVersionReplacementCommand,
    NewBaseLatexVersionReplacementResult,
)


NewBaseLatexVersionReplacementCallable = Callable[
    ...,
    NewBaseLatexVersionReplacementResult
    | Awaitable[NewBaseLatexVersionReplacementResult],
]


class NewBaseLatexVersionReplacementUIController:
    def __init__(
        self,
        *,
        replacement_callable: NewBaseLatexVersionReplacementCallable,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(replacement_callable) or not callable(clock):
            raise TypeError("replacement_callable and clock must be callable")
        self._replacement_callable = replacement_callable
        self._clock = clock

    async def replace(
        self,
        *,
        context: AuthenticatedSubjectContext,
        attention_item_id: str,
        invocation_id: str,
        uploaded_content: bytes,
        display_label: str,
        version_note: str | None,
    ) -> NewBaseLatexVersionReplacementResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        value = self._replacement_callable(
            NewBaseLatexVersionReplacementCommand(
                subject_id=context.subject_id,
                attention_item_id=attention_item_id,
                invocation_id=invocation_id,
                uploaded_content=uploaded_content,
                display_label=display_label,
                version_note=version_note,
                now=now,
            )
        )
        result = await value if inspect.isawaitable(value) else value
        if not isinstance(result, NewBaseLatexVersionReplacementResult):
            raise ValueError("new Base LaTeX service returned invalid data")
        return result


__all__ = [
    "NewBaseLatexVersionReplacementCallable",
    "NewBaseLatexVersionReplacementUIController",
]
