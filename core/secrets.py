"""Keychain-backed secrets used by the trusted Jobops core."""

from __future__ import annotations

import base64
import binascii

from auth.credentials import CredentialStore, MacOSSecurityCredentialStore

from .permits import PermitService


PERMIT_SECRET_SERVICE = "jobops.core.permits"
PERMIT_SECRET_ACCOUNT = "hmac-v1"


def load_or_create_permit_secret(store: CredentialStore | None = None) -> bytes:
    """Load the permit HMAC key or create it directly in the credential store."""

    backend = store or MacOSSecurityCredentialStore()
    encoded = backend.get(PERMIT_SECRET_SERVICE, PERMIT_SECRET_ACCOUNT)
    if encoded:
        try:
            secret = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise ValueError("stored permit key has an invalid encoding") from exc
        if len(secret) < 32:
            raise ValueError("stored permit key is too short")
        return secret

    secret = PermitService.generate_secret()
    backend.set(
        PERMIT_SECRET_SERVICE,
        PERMIT_SECRET_ACCOUNT,
        base64.urlsafe_b64encode(secret).decode("ascii"),
    )
    return secret


__all__ = [
    "PERMIT_SECRET_ACCOUNT",
    "PERMIT_SECRET_SERVICE",
    "load_or_create_permit_secret",
]
