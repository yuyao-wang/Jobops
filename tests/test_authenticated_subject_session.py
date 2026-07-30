"""Focused S3d0 authenticated-subject session tests."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from auth.credentials import InMemoryCredentialStore
from core.authenticated_subject import (
    AUTHENTICATED_SUBJECT_COOKIE_NAME,
    AUTHENTICATED_SUBJECT_SESSION_SERVICE,
    AuthenticatedSessionCredential,
    AuthenticatedSubjectContext,
    AuthenticatedSubjectStatus,
    AuthenticationMethod,
    KeychainAuthenticatedSubjectSessionProvider,
    resolve_authenticated_subject,
)
from dashboard.authentication import (
    make_authenticated_subject_dependency,
    require_subject_access,
)
from dashboard.server import health
from tests.test_application_plan import NOW, SUBJECT


SESSION_ID = "session_reference_0123456789abcdef"
SESSION_SECRET = "synthetic_session_secret_0123456789abcdef"


def _credential() -> AuthenticatedSessionCredential:
    return AuthenticatedSessionCredential(
        session_id=SESSION_ID,
        secret=SESSION_SECRET,
    )


def _context(*, expires_at=None) -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id=SESSION_ID,
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=expires_at or NOW + timedelta(minutes=10),
    )


def _request(cookie: str | None, *, claimed_subject: str = "subject-other"):
    headers = [(b"x-subject-id", claimed_subject.encode("utf-8"))]
    if cookie is not None:
        headers.append(
            (
                b"cookie",
                (
                    f"{AUTHENTICATED_SUBJECT_COOKIE_NAME}={cookie}"
                ).encode("ascii"),
            )
        )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/synthetic",
            "raw_path": b"/synthetic",
            "query_string": (
                f"subject_id={claimed_subject}".encode("ascii")
            ),
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8080),
        }
    )


@pytest.mark.asyncio
async def test_valid_keychain_session_resolves_typed_subject_only_from_cookie(
) -> None:
    store = InMemoryCredentialStore()
    provider = KeychainAuthenticatedSubjectSessionProvider(store)
    credential = _credential()
    context = _context()
    provider.save_session(context, credential)
    request = _request(
        f"{credential.session_id}.{credential.secret}",
        claimed_subject="subject-attacker",
    )

    result = resolve_authenticated_subject(
        request,
        session_provider=provider,
        now=NOW,
    )
    dependency = make_authenticated_subject_dependency(
        session_provider=provider,
        clock=lambda: NOW,
    )

    assert result.status is AuthenticatedSubjectStatus.AUTHENTICATED
    assert result.context == context
    assert (await dependency(request)).subject_id == SUBJECT
    with pytest.raises(HTTPException) as forbidden:
        require_subject_access(context, subject_id="subject-other")
    assert forbidden.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_expired_and_corrupt_sessions_fail_closed_safely(
) -> None:
    store = InMemoryCredentialStore()
    provider = KeychainAuthenticatedSubjectSessionProvider(store)
    expired = _context(expires_at=NOW)
    provider.save_session(expired, _credential())
    dependency = make_authenticated_subject_dependency(
        session_provider=provider,
        clock=lambda: NOW,
    )
    cases = (
        (
            _request(None),
            AuthenticatedSubjectStatus.UNAUTHENTICATED,
            "Authentication required.",
        ),
        (
            _request(f"{SESSION_ID}.{SESSION_SECRET}"),
            AuthenticatedSubjectStatus.SESSION_EXPIRED,
            "Session expired.",
        ),
        (
            _request(f"{SESSION_ID}.{'x' * 40}"),
            AuthenticatedSubjectStatus.SESSION_INVALID,
            "Session invalid.",
        ),
    )

    for request, status, safe_detail in cases:
        result = resolve_authenticated_subject(
            request,
            session_provider=provider,
            now=NOW,
        )
        assert result.status is status
        with pytest.raises(HTTPException) as raised:
            await dependency(request)
        assert raised.value.status_code == 401
        assert raised.value.detail == safe_detail
        assert SESSION_SECRET not in raised.value.detail


@pytest.mark.asyncio
async def test_credentials_stay_secret_and_boundary_has_no_business_calls(
) -> None:
    store = InMemoryCredentialStore()
    provider = KeychainAuthenticatedSubjectSessionProvider(store)
    credential = _credential()
    provider.save_session(_context(), credential)
    stored = store.get(
        AUTHENTICATED_SUBJECT_SESSION_SERVICE,
        credential.session_id,
    )

    assert stored is not None
    assert credential.secret not in stored
    assert credential.secret not in repr(credential)
    assert await health() == {"status": "ok", "profile": Path("profile.yaml").exists()}

    forbidden = {
        "job_library_refresh",
        "job_search",
        "job_discovery",
        "selective_reprioritization",
        "automation_cycle",
        "browser_broker",
        "application_engine",
        "adapters",
    }
    for module in (
        Path("core/authenticated_subject.py"),
        Path("dashboard/authentication.py"),
    ):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert all(
            not any(
                name == item or name.startswith(f"{item}.")
                for item in forbidden
            )
            for name in imports
        )
