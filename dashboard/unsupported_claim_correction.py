"""Authenticated UI adapter for typed unsupported-claim correction."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime

from core.authenticated_subject import AuthenticatedSubjectContext
from core.unsupported_claim_correction import (
    UnsupportedClaimCorrectionAction,
    UnsupportedClaimCorrectionCommand,
    UnsupportedClaimCorrectionResult,
)


UnsupportedClaimCorrectionCallable = Callable[
    ...,
    UnsupportedClaimCorrectionResult
    | Awaitable[UnsupportedClaimCorrectionResult],
]


class UnsupportedClaimCorrectionUIController:
    def __init__(
        self,
        *,
        correction_callable: UnsupportedClaimCorrectionCallable,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(correction_callable) or not callable(clock):
            raise TypeError("correction_callable and clock must be callable")
        self._correction_callable = correction_callable
        self._clock = clock

    async def correct(
        self,
        *,
        context: AuthenticatedSubjectContext,
        attention_item_id: str,
        action: UnsupportedClaimCorrectionAction,
        instruction: str | None,
    ) -> UnsupportedClaimCorrectionResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        value = self._correction_callable(
            UnsupportedClaimCorrectionCommand(
                subject_id=context.subject_id,
                attention_item_id=attention_item_id,
                action=UnsupportedClaimCorrectionAction(action),
                instruction=instruction,
                now=now,
            )
        )
        result = await value if inspect.isawaitable(value) else value
        if not isinstance(result, UnsupportedClaimCorrectionResult):
            raise ValueError("correction service returned an invalid result")
        return result


__all__ = [
    "UnsupportedClaimCorrectionCallable",
    "UnsupportedClaimCorrectionUIController",
]
