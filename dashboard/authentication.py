"""FastAPI dependency boundary for authenticated Jobops subjects."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable

from fastapi import HTTPException
from starlette.requests import Request

from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticatedSubjectSessionProvider,
    AuthenticatedSubjectStatus,
    IssuedAuthenticatedSubjectSession,
    LocalAuthenticatedSubjectSessionIssuer,
    resolve_authenticated_subject,
)


AuthenticatedSubjectDependency = Callable[
    [Request], Awaitable[AuthenticatedSubjectContext]
]


def _is_loopback_host(value: str | None) -> bool:
    if not value:
        return False
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class LocalDashboardSessionController:
    """Issue a session only to the same-origin loopback Dashboard."""

    issuer: LocalAuthenticatedSubjectSessionIssuer
    clock: Callable[[], datetime]

    def __post_init__(self) -> None:
        if not isinstance(
            self.issuer, LocalAuthenticatedSubjectSessionIssuer
        ):
            raise TypeError("issuer must be a local session issuer")
        if not callable(self.clock):
            raise TypeError("clock must be callable")

    def issue(self, request: Request) -> IssuedAuthenticatedSubjectSession:
        client_host = request.client.host if request.client else None
        if not _is_loopback_host(client_host) or not _is_loopback_host(
            request.url.hostname
        ):
            raise HTTPException(
                status_code=403,
                detail="Local Dashboard authentication is restricted.",
            )
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.headers.get("origin") != expected_origin:
            raise HTTPException(
                status_code=403,
                detail="Local Dashboard origin is invalid.",
            )
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site not in (None, "same-origin"):
            raise HTTPException(
                status_code=403,
                detail="Local Dashboard origin is invalid.",
            )
        try:
            return self.issuer.issue(now=self.clock())
        except (OSError, RuntimeError, TypeError, ValueError):
            raise HTTPException(
                status_code=503,
                detail="Authenticated session issuance failed.",
            ) from None


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
    "LocalDashboardSessionController",
    "make_authenticated_subject_dependency",
    "require_subject_access",
]
