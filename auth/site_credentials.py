"""Tenant-scoped Keychain credentials for non-Workday ATS accounts."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
from urllib.parse import urlsplit

from .credentials import CredentialStore, MacOSSecurityCredentialStore
from .passwords import generate_strong_password


KEYCHAIN_PREFIX = "jobops.ats"
_TENANT_RE = re.compile(r"[^a-z0-9.-]+")


@dataclass(frozen=True)
class SiteCredential:
    email: str
    password: str = field(repr=False)
    service: str
    created: bool = False


def _public_https_host(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not host:
        raise ValueError("an absolute HTTPS ATS URL is required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ATS URL must not contain userinfo")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None or host == "localhost" or "." not in host:
        raise ValueError("ATS credential host must be a public DNS name")
    return host


def _tenant_slug(tenant: str) -> str:
    slug = _TENANT_RE.sub("-", str(tenant or "").strip().casefold()).strip("-.")
    if not slug:
        raise ValueError("ATS tenant is required")
    return slug[:80]


def site_service(url: str, tenant: str) -> str:
    """Return a host-and-tenant scoped service identifier."""

    return f"{KEYCHAIN_PREFIX}.{_public_https_host(url)}.{_tenant_slug(tenant)}"


def get_site_credential(
    url: str,
    tenant: str,
    email: str,
    *,
    store: CredentialStore | None = None,
) -> SiteCredential | None:
    if not str(email or "").strip():
        raise ValueError("email is required")
    backend = store or MacOSSecurityCredentialStore()
    service = site_service(url, tenant)
    password = backend.get(service, email)
    if password is None:
        return None
    return SiteCredential(email=email, password=password, service=service)


def save_site_credential(
    url: str,
    tenant: str,
    email: str,
    password: str,
    *,
    store: CredentialStore | None = None,
) -> SiteCredential:
    if not str(email or "").strip() or not password:
        raise ValueError("email and password are required")
    backend = store or MacOSSecurityCredentialStore()
    service = site_service(url, tenant)
    backend.set(service, email, password)
    if backend.get(service, email) != password:
        raise RuntimeError("Keychain write verification failed")
    return SiteCredential(email=email, password=password, service=service)


def get_or_create_site_credential(
    url: str,
    tenant: str,
    email: str,
    *,
    store: CredentialStore | None = None,
    password_length: int = 24,
) -> SiteCredential:
    """Load an existing password or persist a generated one before form entry."""

    backend = store or MacOSSecurityCredentialStore()
    existing = get_site_credential(url, tenant, email, store=backend)
    if existing is not None:
        return existing
    password = generate_strong_password(password_length)
    saved = save_site_credential(
        url,
        tenant,
        email,
        password,
        store=backend,
    )
    return SiteCredential(
        email=saved.email,
        password=saved.password,
        service=saved.service,
        created=True,
    )


__all__ = [
    "KEYCHAIN_PREFIX",
    "SiteCredential",
    "get_or_create_site_credential",
    "get_site_credential",
    "save_site_credential",
    "site_service",
]
