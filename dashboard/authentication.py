"""FastAPI dependency boundary for authenticated Jobops subjects."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Awaitable

from fastapi import HTTPException
from starlette.requests import Request

from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticatedSubjectSessionProvider,
    AuthenticatedSubjectStatus,
    resolve_authenticated_subject,
)


AuthenticatedSubjectDependency = Callable[
    [Request], Awaitable[AuthenticatedSubjectContext]
]


def make_authenticated_subject_dependency(
    *,
    session_provider: AuthenticatedSubjectSessionProvider,
    clock: Callable[[], datetime],
) -> AuthenticatedSubjectDependency:
    """Build one dependency with explicit provider and clock composition."""

    async def authenticated_subject(
        request: Request,
    ) -> AuthenticatedSubjectContext:
        result = resolve_authenticated_subject(
            request,
            session_provider=session_provider,
            now=clock(),
        )
        if result.status is AuthenticatedSubjectStatus.AUTHENTICATED:
            assert result.context is not None
            return result.context
        detail = {
            AuthenticatedSubjectStatus.UNAUTHENTICATED: (
                "Authentication required."
            ),
            AuthenticatedSubjectStatus.SESSION_EXPIRED: "Session expired.",
            AuthenticatedSubjectStatus.SESSION_INVALID: "Session invalid.",
            AuthenticatedSubjectStatus.FAILED: "Session invalid.",
        }[result.status]
        raise HTTPException(status_code=401, detail=detail)

    return authenticated_subject


def require_subject_access(
    context: AuthenticatedSubjectContext,
    *,
    subject_id: str,
) -> None:
    """Fail closed when a route resource belongs to another subject."""

    if not isinstance(context, AuthenticatedSubjectContext):
        raise TypeError("context must be authenticated")
    if context.subject_id != subject_id:
        raise HTTPException(status_code=403, detail="Access denied.")


__all__ = [
    "AuthenticatedSubjectDependency",
    "make_authenticated_subject_dependency",
    "require_subject_access",
]
