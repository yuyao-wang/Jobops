"""macOS Keychain access for ATS credentials.

Passwords are stored as generic-password items and are never written to the
profile, CSV, logs, or command-line arguments.
"""

from __future__ import annotations

import platform
import secrets
import string
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse


KEYCHAIN_PREFIX = "mr-jobs.workday"


class KeychainError(RuntimeError):
    """Raised when the local Keychain cannot complete an operation."""


@dataclass(frozen=True)
class WorkdayCredential:
    email: str
    password: str
    service: str


def workday_service(job_url: str) -> str:
    """Return a tenant-specific Keychain service name for a Workday URL."""
    host = (urlparse(job_url).hostname or "workday").lower()
    return f"{KEYCHAIN_PREFIX}.{host}"


def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise KeychainError("macOS Keychain is only available on macOS")


def get_workday_credential(job_url: str, email: str) -> WorkdayCredential | None:
    """Read a Workday password from Keychain, returning None when absent."""
    _require_macos()
    service = workday_service(job_url)
    result = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", email, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 44:  # errSecItemNotFound
        return None
    if result.returncode != 0:
        message = result.stderr.strip() or "Keychain lookup failed"
        raise KeychainError(message)
    return WorkdayCredential(email=email, password=result.stdout.rstrip("\n"), service=service)


def save_workday_credential(job_url: str, email: str, password: str) -> str:
    """Create or update a tenant-specific Workday Keychain item."""
    _require_macos()
    if not email or not password:
        raise ValueError("email and password are required")
    service = workday_service(job_url)
    result = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            service,
            "-a",
            email,
            "-w",
            password,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "Keychain update failed"
        raise KeychainError(message)
    return service


def generate_strong_password(length: int = 24) -> str:
    """Generate a password with all common Workday character classes."""
    if length < 16:
        raise ValueError("password length must be at least 16")
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*_-+="),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-+="
    remaining = [secrets.choice(alphabet) for _ in range(length - len(required))]
    chars = required + remaining
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
