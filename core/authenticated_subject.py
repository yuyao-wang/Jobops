"""Typed authenticated-subject sessions backed by a trusted credential store."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from auth.credentials import (
    CredentialStore,
    CredentialStoreError,
    MacOSSecurityCredentialStore,
)


AUTHENTICATED_SUBJECT_SESSION_CONTRACT_VERSION = (
    "authenticated-subject-session-v1"
)
AUTHENTICATED_SUBJECT_SESSION_SERVICE = "jobops.dashboard.sessions.v1"
AUTHENTICATED_SUBJECT_COOKIE_NAME = "jobops_session"
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,128}")
_CREDENTIAL_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,256}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _clean(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the session contract")
    return cleaned


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("persisted session timestamp must be a string")
    return _aware(
        "persisted session timestamp",
        datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


class AuthenticationMethod(StrEnum):
    LOCAL_KEYCHAIN_SESSION = "LOCAL_KEYCHAIN_SESSION"


class AuthenticatedSubjectStatus(StrEnum):
    AUTHENTICATED = "AUTHENTICATED"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_INVALID = "SESSION_INVALID"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AuthenticatedSubjectContext:
    session_id: str
    subject_id: str
    authentication_method: AuthenticationMethod
    issued_at: datetime
    expires_at: datetime
    contract_version: str = AUTHENTICATED_SUBJECT_SESSION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.session_id, str)
            or _SESSION_ID_RE.fullmatch(self.session_id) is None
        ):
            raise ValueError("session_id is invalid")
        subject_id = _clean("subject_id", self.subject_id, 160)
        object.__setattr__(
            self,
            "authentication_method",
            AuthenticationMethod(self.authentication_method),
        )
        issued_at = _aware("issued_at", self.issued_at)
        expires_at = _aware("expires_at", self.expires_at)
        if expires_at <= issued_at:
            raise ValueError("session expiry must follow issue time")
        if self.contract_version != AUTHENTICATED_SUBJECT_SESSION_CONTRACT_VERSION:
            raise ValueError("session contract version is unsupported")
        object.__setattr__(self, "subject_id", subject_id)


@dataclass(frozen=True, slots=True)
class AuthenticatedSubjectSession:
    context: AuthenticatedSubjectContext
    credential_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.context, AuthenticatedSubjectContext):
            raise TypeError("session context must be typed")
        if (
            not isinstance(self.credential_hash, str)
            or _HASH_RE.fullmatch(self.credential_hash) is None
        ):
            raise ValueError("session credential hash is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "authentication_method": (
                self.context.authentication_method.value
            ),
            "contract_version": self.context.contract_version,
            "credential_hash": self.credential_hash,
            "expires_at": _time(self.context.expires_at),
            "issued_at": _time(self.context.issued_at),
            "session_id": self.context.session_id,
            "subject_id": self.context.subject_id,
        }


@dataclass(frozen=True, slots=True)
class AuthenticatedSessionCredential:
    session_id: str
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.session_id, str)
            or _SESSION_ID_RE.fullmatch(self.session_id) is None
            or not isinstance(self.secret, str)
            or _CREDENTIAL_SECRET_RE.fullmatch(self.secret) is None
        ):
            raise ValueError("session credential is invalid")

    @classmethod
    def parse(cls, value: Any) -> "AuthenticatedSessionCredential":
        if not isinstance(value, str) or value.count(".") != 1:
            raise ValueError("session credential is invalid")
        session_id, secret = value.split(".", 1)
        return cls(session_id=session_id, secret=secret)


@dataclass(frozen=True, slots=True)
class AuthenticatedSubjectResult:
    status: AuthenticatedSubjectStatus
    context: AuthenticatedSubjectContext | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", AuthenticatedSubjectStatus(self.status)
        )
        if self.status is AuthenticatedSubjectStatus.AUTHENTICATED:
            if not isinstance(self.context, AuthenticatedSubjectContext):
                raise ValueError("authenticated result requires context")
        elif self.context is not None:
            raise ValueError("failed authentication cannot expose context")


@runtime_checkable
class AuthenticatedSubjectSessionProvider(Protocol):
    def authenticate(
        self,
        credential: AuthenticatedSessionCredential,
        *,
        now: datetime,
    ) -> AuthenticatedSubjectResult: ...


def _credential_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _session_from_dict(value: Any) -> AuthenticatedSubjectSession:
    expected = {
        "authentication_method",
        "contract_version",
        "credential_hash",
        "expires_at",
        "issued_at",
        "session_id",
        "subject_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("stored authenticated session is malformed")
    context = AuthenticatedSubjectContext(
        session_id=value["session_id"],
        subject_id=value["subject_id"],
        authentication_method=AuthenticationMethod(
            value["authentication_method"]
        ),
        issued_at=_parse_time(value["issued_at"]),
        expires_at=_parse_time(value["expires_at"]),
        contract_version=value["contract_version"],
    )
    return AuthenticatedSubjectSession(
        context=context,
        credential_hash=value["credential_hash"],
    )


class KeychainAuthenticatedSubjectSessionProvider:
    """Validate opaque cookie credentials against Keychain session records."""

    def __init__(self, store: CredentialStore | None = None) -> None:
        self._store = store or MacOSSecurityCredentialStore()

    def save_session(
        self,
        context: AuthenticatedSubjectContext,
        credential: AuthenticatedSessionCredential,
    ) -> None:
        if not isinstance(context, AuthenticatedSubjectContext) or (
            not isinstance(credential, AuthenticatedSessionCredential)
        ):
            raise TypeError("context and credential must be typed")
        if context.session_id != credential.session_id:
            raise ValueError("session credential binding is invalid")
        session = AuthenticatedSubjectSession(
            context=context,
            credential_hash=_credential_hash(credential.secret),
        )
        encoded = json.dumps(
            session.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self._store.set(
            AUTHENTICATED_SUBJECT_SESSION_SERVICE,
            context.session_id,
            encoded,
        )
        if (
            self._store.get(
                AUTHENTICATED_SUBJECT_SESSION_SERVICE,
                context.session_id,
            )
            != encoded
        ):
            raise CredentialStoreError("session store verification failed")

    def authenticate(
        self,
        credential: AuthenticatedSessionCredential,
        *,
        now: datetime,
    ) -> AuthenticatedSubjectResult:
        if not isinstance(credential, AuthenticatedSessionCredential):
            raise TypeError("credential must be typed")
        checked_at = _aware("now", now)
        try:
            encoded = self._store.get(
                AUTHENTICATED_SUBJECT_SESSION_SERVICE,
                credential.session_id,
            )
        except (CredentialStoreError, OSError, RuntimeError):
            return AuthenticatedSubjectResult(
                AuthenticatedSubjectStatus.FAILED, None
            )
        if encoded is None:
            return AuthenticatedSubjectResult(
                AuthenticatedSubjectStatus.SESSION_INVALID, None
            )
        try:
            session = _session_from_dict(json.loads(encoded))
        except (
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return AuthenticatedSubjectResult(
                AuthenticatedSubjectStatus.SESSION_INVALID, None
            )
        if session.context.session_id != credential.session_id or (
            not secrets.compare_digest(
                session.credential_hash,
                _credential_hash(credential.secret),
            )
        ):
            return AuthenticatedSubjectResult(
                AuthenticatedSubjectStatus.SESSION_INVALID, None
            )
        if checked_at < session.context.issued_at:
            return AuthenticatedSubjectResult(
                AuthenticatedSubjectStatus.SESSION_INVALID, None
            )
        if checked_at >= session.context.expires_at:
            return AuthenticatedSubjectResult(
                AuthenticatedSubjectStatus.SESSION_EXPIRED, None
            )
        return AuthenticatedSubjectResult(
            AuthenticatedSubjectStatus.AUTHENTICATED,
            session.context,
        )


def resolve_authenticated_subject(
    request: Any,
    *,
    session_provider: AuthenticatedSubjectSessionProvider,
    now: datetime,
) -> AuthenticatedSubjectResult:
    """Resolve only the fixed secure-cookie credential from one HTTP request."""

    _aware("now", now)
    cookies = getattr(request, "cookies", None)
    if not isinstance(cookies, Mapping):
        return AuthenticatedSubjectResult(
            AuthenticatedSubjectStatus.UNAUTHENTICATED, None
        )
    raw_credential = cookies.get(AUTHENTICATED_SUBJECT_COOKIE_NAME)
    if raw_credential is None:
        return AuthenticatedSubjectResult(
            AuthenticatedSubjectStatus.UNAUTHENTICATED, None
        )
    try:
        credential = AuthenticatedSessionCredential.parse(raw_credential)
    except (TypeError, ValueError):
        return AuthenticatedSubjectResult(
            AuthenticatedSubjectStatus.SESSION_INVALID, None
        )
    try:
        result = session_provider.authenticate(credential, now=now)
    except (OSError, RuntimeError, TypeError, ValueError):
        return AuthenticatedSubjectResult(
            AuthenticatedSubjectStatus.FAILED, None
        )
    if not isinstance(result, AuthenticatedSubjectResult):
        return AuthenticatedSubjectResult(
            AuthenticatedSubjectStatus.FAILED, None
        )
    return result


__all__ = [
    "AUTHENTICATED_SUBJECT_COOKIE_NAME",
    "AUTHENTICATED_SUBJECT_SESSION_CONTRACT_VERSION",
    "AUTHENTICATED_SUBJECT_SESSION_SERVICE",
    "AuthenticatedSessionCredential",
    "AuthenticatedSubjectContext",
    "AuthenticatedSubjectResult",
    "AuthenticatedSubjectSession",
    "AuthenticatedSubjectSessionProvider",
    "AuthenticatedSubjectStatus",
    "AuthenticationMethod",
    "KeychainAuthenticatedSubjectSessionProvider",
    "resolve_authenticated_subject",
]
