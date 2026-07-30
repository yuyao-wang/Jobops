"""Authenticated UI adapter for explicit Resume layout correction."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime

from core.authenticated_subject import AuthenticatedSubjectContext
from core.resume_layout_correction import (
    ResumeLayoutCorrectionAction,
    ResumeLayoutCorrectionCommand,
    ResumeLayoutCorrectionResult,
    ResumeLayoutVisualIssue,
)


ResumeLayoutCorrectionCallable = Callable[
    ...,
    ResumeLayoutCorrectionResult | Awaitable[ResumeLayoutCorrectionResult],
]


class ResumeLayoutCorrectionUIController:
    def __init__(
        self,
        *,
        correction_callable: ResumeLayoutCorrectionCallable,
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
        action: ResumeLayoutCorrectionAction,
        visual_issues: tuple[ResumeLayoutVisualIssue, ...],
    ) -> ResumeLayoutCorrectionResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return an aware datetime")
        value = self._correction_callable(
            ResumeLayoutCorrectionCommand(
                subject_id=context.subject_id,
                attention_item_id=attention_item_id,
                action=ResumeLayoutCorrectionAction(action),
                visual_issues=tuple(
                    ResumeLayoutVisualIssue(issue)
                    for issue in visual_issues
                ),
                now=now,
            )
        )
        result = await value if inspect.isawaitable(value) else value
        if not isinstance(result, ResumeLayoutCorrectionResult):
            raise ValueError("layout correction returned an invalid result")
        return result


__all__ = [
    "ResumeLayoutCorrectionCallable",
    "ResumeLayoutCorrectionUIController",
]
