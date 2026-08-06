"""Narrow mailbox verification contracts for account activation.

This module intentionally does not provide broad inbox search or message
summarization.  Providers receive a recipient and a recent time boundary;
correlation and ambiguity checks happen before an artifact can be returned.
CAPTCHA and MFA are outside this interface and always require handoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from html import unescape
from typing import Callable, Protocol, Sequence, runtime_checkable
from urllib.parse import urlparse


class MailboxVerificationStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    UNAVAILABLE = "UNAVAILABLE"
    UNSAFE = "UNSAFE"


class VerificationArtifactKind(StrEnum):
    CODE = "CODE"
    LINK = "LINK"


class MailAuthenticationResult(StrEnum):
    """Sanitized result for one mailbox-projected email-auth mechanism."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MailAuthenticationEvidence:
    """Typed SPF/DKIM/DMARC projection with no raw header material."""

    spf: MailAuthenticationResult = MailAuthenticationResult.UNKNOWN
    dkim: MailAuthenticationResult = MailAuthenticationResult.UNKNOWN
    dmarc: MailAuthenticationResult = MailAuthenticationResult.UNKNOWN

    @property
    def sender_is_authenticated(self) -> bool:
        """Require aligned DMARC plus at least one passing auth mechanism."""

        return self.dmarc is MailAuthenticationResult.PASS and (
            self.spf is MailAuthenticationResult.PASS
            or self.dkim is MailAuthenticationResult.PASS
        )


@dataclass(frozen=True)
class MailboxMessage:
    """Minimal message projection; providers should not return full inbox data."""

    message_id: str
    received_at: datetime
    sender: str
    recipients: tuple[str, ...]
    subject: str
    text: str = ""
    html: str = ""
    authentication: MailAuthenticationEvidence = field(
        default_factory=MailAuthenticationEvidence
    )


@dataclass(frozen=True)
class VerificationRequest:
    recipient: str
    tenant_host: str
    initiated_at: datetime
    correlation_terms: tuple[str, ...] = ()
    allowed_sender_domains: tuple[str, ...] = (
        "workday.com",
        "myworkday.com",
        "myworkdayjobs.com",
    )
    max_age: timedelta = timedelta(minutes=15)
    max_messages: int = 10


@dataclass(frozen=True)
class VerificationArtifact:
    kind: VerificationArtifactKind
    value: str
    message_id: str
    received_at: datetime


@dataclass(frozen=True)
class VerificationResult:
    status: MailboxVerificationStatus
    artifact: VerificationArtifact | None = None
    reason: str = ""
    matched_message_ids: tuple[str, ...] = ()

    @property
    def requires_handoff(self) -> bool:
        return self.status is not MailboxVerificationStatus.FOUND


@runtime_checkable
class MailboxProvider(Protocol):
    async def search_recent(
        self,
        *,
        recipient: str,
        since: datetime,
        limit: int,
    ) -> Sequence[MailboxMessage]:
        """Return only messages for ``recipient`` received at/after ``since``."""


@runtime_checkable
class MailboxVerifier(Protocol):
    async def find_verification(self, request: VerificationRequest) -> VerificationResult:
        """Return one correlated verification artifact or a handoff result."""


@dataclass
class CorrelatedMailboxVerifier:
    provider: MailboxProvider
    now: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc), repr=False
    )

    async def find_verification(self, request: VerificationRequest) -> VerificationResult:
        if not request.recipient or not request.tenant_host:
            return VerificationResult(
                MailboxVerificationStatus.UNSAFE,
                reason="recipient and tenant correlation are required",
            )

        current = _as_utc(self.now())
        initiated = _as_utc(request.initiated_at)
        lower_bound = max(initiated - timedelta(minutes=1), current - request.max_age)
        try:
            messages = await self.provider.search_recent(
                recipient=request.recipient,
                since=lower_bound,
                limit=min(max(request.max_messages, 1), 25),
            )
        except Exception:
            return VerificationResult(
                MailboxVerificationStatus.UNAVAILABLE,
                reason="mailbox provider is unavailable",
            )

        candidates: list[tuple[MailboxMessage, list[VerificationArtifact]]] = []
        for message in messages:
            if not _message_is_correlated(message, request, lower_bound, current):
                continue
            artifacts = _extract_artifacts(message, request)
            if artifacts:
                candidates.append((message, artifacts))

        if not candidates:
            return VerificationResult(
                MailboxVerificationStatus.NOT_FOUND,
                reason="no recent correlated verification message was found",
            )

        message_ids = tuple(message.message_id for message, _ in candidates)
        flattened = [artifact for _, artifacts in candidates for artifact in artifacts]
        unique_values = {(artifact.kind, artifact.value) for artifact in flattened}
        if len(candidates) != 1 or len(flattened) != 1 or len(unique_values) != 1:
            return VerificationResult(
                MailboxVerificationStatus.AMBIGUOUS,
                reason="multiple verification messages or artifacts matched",
                matched_message_ids=message_ids,
            )
        return VerificationResult(
            MailboxVerificationStatus.FOUND,
            artifact=flattened[0],
            matched_message_ids=message_ids,
        )


def _message_is_correlated(
    message: MailboxMessage,
    request: VerificationRequest,
    lower_bound: datetime,
    current: datetime,
) -> bool:
    received = _as_utc(message.received_at)
    if received < lower_bound or received > current + timedelta(minutes=2):
        return False
    recipient = request.recipient.casefold()
    if recipient not in {item.casefold() for item in message.recipients}:
        return False
    if not _sender_is_allowed(message.sender, request.allowed_sender_domains):
        return False

    haystack = f"{message.subject}\n{message.text}\n{message.html}".casefold()
    verification_language = (
        "verify" in haystack
        or "verification" in haystack
        or "confirm your email" in haystack
        or "activation" in haystack
    )
    if not verification_language:
        return False
    if any(term in haystack for term in ("password reset", "security alert", "new sign-in")):
        return False

    tenant_terms = {
        request.tenant_host.casefold(),
        request.tenant_host.split(".", 1)[0].casefold(),
        *(term.casefold() for term in request.correlation_terms if term.strip()),
    }
    return any(term and term in haystack for term in tenant_terms)


def _sender_is_allowed(sender: str, domains: tuple[str, ...]) -> bool:
    match = re.search(r"@([^>\s]+)", sender.casefold())
    if not match:
        return False
    domain = match.group(1).rstrip(".>")
    return any(domain == allowed or domain.endswith(f".{allowed}") for allowed in domains)


def _extract_artifacts(
    message: MailboxMessage,
    request: VerificationRequest,
) -> list[VerificationArtifact]:
    content = unescape(f"{message.text}\n{message.html}")
    artifacts: list[VerificationArtifact] = []

    codes = set(re.findall(
        r"(?i)(?:verification|verify|confirmation|activation)\s*(?:code)?\s*(?:is\s*)?[:\-]?\s*\b(\d{6})\b",
        content,
    ))
    for code in sorted(codes):
        artifacts.append(VerificationArtifact(
            VerificationArtifactKind.CODE,
            code,
            message.message_id,
            message.received_at,
        ))

    links: set[str] = set()
    for link in re.findall(r"https?://[^\s<>'\"]+", content):
        cleaned = link.rstrip(".,);]")
        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").casefold()
        path_and_query = f"{parsed.path}?{parsed.query}".casefold()
        trusted = (
            host == request.tenant_host.casefold()
            or host.endswith(".myworkday.com")
            or host.endswith(".myworkdayjobs.com")
            or host.endswith(".workday.com")
        )
        if trusted and any(token in path_and_query for token in ("verify", "confirm", "activate")):
            links.add(cleaned)
    for link in sorted(links):
        artifacts.append(VerificationArtifact(
            VerificationArtifactKind.LINK,
            link,
            message.message_id,
            message.received_at,
        ))
    return artifacts


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
