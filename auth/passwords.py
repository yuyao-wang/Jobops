"""Cryptographically strong passwords for ATS account registration."""

from __future__ import annotations

import secrets
import string


_SYMBOLS = "!@#$%^&*_-+="


def generate_strong_password(length: int = 24) -> str:
    """Generate a password with the character classes common ATS sites require."""

    if length < 16:
        raise ValueError("password length must be at least 16")
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(_SYMBOLS),
    ]
    alphabet = string.ascii_letters + string.digits + _SYMBOLS
    chars = required + [
        secrets.choice(alphabet) for _ in range(length - len(required))
    ]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


__all__ = ["generate_strong_password"]
