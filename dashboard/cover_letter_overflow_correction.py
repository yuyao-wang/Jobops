"""Authenticated UI adapter for explicit Cover Letter overflow correction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from core.authenticated_subject import AuthenticatedSubjectContext
from core.cover_letter_overflow_correction import (
    CoverLetterOverflowCorrectionAction,
    CoverLetterOverflowCorrectionCommand,
    CoverLetterOverflowCorrectionResult,
)


@dataclass(frozen=True, slots=True)
class CoverLetterOverflowCorrectionUICommand:
    attention_item_id: str
    action: CoverLetterOverflowCorrectionAction


class CoverLetterOverflowCorrectionUIController:
    def __init__(
        self,
        *,
        resolver: Callable[..., Any],
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(resolver) or not callable(clock):
            raise TypeError("resolver and clock must be callable")
        self._resolver = resolver
        self._clock = clock

    async def resolve(
        self,
        *,
        context: AuthenticatedSubjectContext,
        command: CoverLetterOverflowCorrectionUICommand,
    ) -> CoverLetterOverflowCorrectionResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if not isinstance(
            command, CoverLetterOverflowCorrectionUICommand
        ):
            raise TypeError("command must be typed")
        result = self._resolver(
            CoverLetterOverflowCorrectionCommand(
                subject_id=context.subject_id,
                attention_item_id=command.attention_item_id,
                action=command.action,
                now=self._clock(),
            )
        )
        if hasattr(result, "__await__"):
            result = await result
        if not isinstance(result, CoverLetterOverflowCorrectionResult):
            raise TypeError("resolver returned an invalid result")
        return result


__all__ = [
    "CoverLetterOverflowCorrectionUICommand",
    "CoverLetterOverflowCorrectionUIController",
]
