"""Compatibility helpers for tenant-scoped ATS credentials.

Secrets are stored through :mod:`auth.credentials`, which calls Apple's
Security.framework directly.  No password is ever passed to a subprocess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from auth.credentials import (
    CredentialStore,
    CredentialStoreError,
    MacOSSecurityCredentialStore,
)
from auth.workday_hosts import is_trusted_workday_host
from auth.passwords import generate_strong_password


KEYCHAIN_PREFIX = "jobops.workday"
LEGACY_KEYCHAIN_PREFIXES = ("mr-jobs.workday",)

# Retain the public exception name used by the original MR.Jobs CLI.
KeychainError = CredentialStoreError


@dataclass(frozen=True)
class WorkdayCredential:
    email: str
    password: str = field(repr=False)
    service: str
    migrated_from: str | None = None


_default_store: CredentialStore | None = None


def default_credential_store() -> CredentialStore:
    """Return the process-wide credential store, created lazily."""
    global _default_store
    if _default_store is None:
        _default_store = MacOSSecurityCredentialStore()
    return _default_store


def set_default_credential_store(store: CredentialStore | None) -> None:
    """Override the store for embedding/tests; ``None`` restores production."""
    global _default_store
    _default_store = store


def workday_service(job_url: str) -> str:
    """Return the Jobops tenant-specific service name for a Workday URL."""
    host = _validated_workday_host(job_url)
    return f"{KEYCHAIN_PREFIX}.{host}"


def legacy_workday_services(job_url: str) -> tuple[str, ...]:
    """Return service identifiers used by earlier local MR.Jobs versions."""
    host = _validated_workday_host(job_url)
    return tuple(f"{prefix}.{host}" for prefix in LEGACY_KEYCHAIN_PREFIXES)


def get_workday_credential(
    job_url: str,
    email: str,
    *,
    store: CredentialStore | None = None,
    migrate_legacy: bool = True,
) -> WorkdayCredential | None:
    """Read a credential and safely migrate a legacy service when present.

    Migration writes the new item first.  The legacy item is deleted only
    after the new value can be read back, so an interrupted migration cannot
    lose the user's credential.
    """
    if not email:
        raise ValueError("email is required")
    backend = store or default_credential_store()
    service = workday_service(job_url)
    password = backend.get(service, email)
    if password is not None:
        return WorkdayCredential(email=email, password=password, service=service)

    for legacy_service in legacy_workday_services(job_url):
        password = backend.get(legacy_service, email)
        if password is None:
            continue
        if not migrate_legacy:
            return WorkdayCredential(
                email=email,
                password=password,
                service=legacy_service,
                migrated_from=legacy_service,
            )

        backend.set(service, email, password)
        if backend.get(service, email) != password:
            raise KeychainError("Keychain migration verification failed")
        try:
            backend.delete(legacy_service, email)
        except CredentialStoreError:
            # The new, verified item is authoritative.  Leaving the old item is
            # safer than failing authentication after a successful migration.
            pass
        return WorkdayCredential(
            email=email,
            password=password,
            service=service,
            migrated_from=legacy_service,
        )
    return None


def save_workday_credential(
    job_url: str,
    email: str,
    password: str,
    *,
    store: CredentialStore | None = None,
) -> str:
    """Create or update a tenant credential without exposing the password."""
    if not email or not password:
        raise ValueError("email and password are required")
    backend = store or default_credential_store()
    service = workday_service(job_url)
    backend.set(service, email, password)
    if backend.get(service, email) != password:
        raise KeychainError("Keychain write verification failed")
    return service


def delete_workday_credential(
    job_url: str,
    email: str,
    *,
    store: CredentialStore | None = None,
) -> bool:
    backend = store or default_credential_store()
    return backend.delete(workday_service(job_url), email)


def _validated_workday_host(job_url: str) -> str:
    parsed = urlparse(job_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or not is_trusted_workday_host(host)
    ):
        raise ValueError("a valid Workday URL is required")
    return host
