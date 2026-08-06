"""Bounded, read-only IMAP provider for verification-mail correlation.

The provider is deliberately narrower than a general mailbox client:

* it is disabled unless explicitly enabled;
* it can only search the configured account's own recipient address;
* searches have a short time window, result cap, and message-size cap;
* the mailbox is selected read-only and messages are fetched with ``BODY.PEEK``;
* the password is obtained from an injected :class:`CredentialStore` and is
  never accepted as configuration, persisted, logged, or placed in argv;
* protocol and parsing failures are surfaced as sanitized errors.

This module uses only Python's standard-library IMAP and email parsers.  It
does not attempt CAPTCHA, MFA, account recovery, mailbox writes, or broad
inbox summarization.
"""

from __future__ import annotations

import asyncio
import hashlib
import imaplib
import math
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Callable, Protocol, Sequence

from .credentials import CredentialStore
from .mailbox import (
    MailAuthenticationEvidence,
    MailAuthenticationResult,
    MailboxMessage,
)


_MAX_LIMIT = 25
_DEFAULT_MAX_SEARCH_WINDOW = timedelta(hours=1)
_DEFAULT_MAX_MESSAGE_BYTES = 256 * 1024
_DEFAULT_MAX_OUTPUT_CHARS = 64 * 1024
_DEFAULT_MAX_CANDIDATE_UIDS = 100
_MAX_SEARCH_RESPONSE_BYTES = 128 * 1024
_MAX_HEADER_CHARS = 1_024
_MAX_AUTHENTICATION_RESULTS_HEADERS = 8
_MAX_AUTHENTICATION_RESULTS_HEADER_CHARS = 4_096
_MAX_MIME_PARTS = 32
_IMAP_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_UID_PATTERN = re.compile(rb"^[1-9][0-9]{0,19}$")
_EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$",
    re.ASCII,
)
_HOST_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$",
    re.ASCII,
)
_MAILBOX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$", re.ASCII)
_SERVICE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$", re.ASCII)
_SIZE_PATTERN = re.compile(rb"\bRFC822\.SIZE\s+([0-9]{1,12})\b", re.IGNORECASE)
_INTERNAL_DATE_PATTERN = re.compile(
    rb'\bINTERNALDATE\s+"([0-9]{1,2}-[A-Za-z]{3}-[0-9]{4} '
    rb'[0-9]{2}:[0-9]{2}:[0-9]{2} [+-][0-9]{4})"',
    re.IGNORECASE,
)
_AUTHENTICATION_RESULT_PATTERN = re.compile(
    r"(?:^|[;\s])(spf|dkim|dmarc)\s*=\s*([A-Za-z0-9_-]{1,32})",
    re.IGNORECASE,
)


class IMAPProviderError(RuntimeError):
    """Sanitized failure from the optional IMAP provider."""


class IMAPProviderDisabled(IMAPProviderError):
    """Raised when mailbox access has not been explicitly enabled."""


class _IMAPClient(Protocol):
    def login(self, user: str, password: str): ...

    def select(self, mailbox: str = "INBOX", readonly: bool = False): ...

    def uid(self, command: str, *args): ...

    def logout(self): ...


IMAPClientFactory = Callable[[str, int, float, ssl.SSLContext], _IMAPClient]


@dataclass(frozen=True)
class IMAPProviderConfig:
    """Non-secret IMAP configuration.

    ``account`` is also the only recipient this provider may search.  The
    corresponding password must already exist in ``CredentialStore`` under
    ``(keychain_service, account)``.
    """

    enabled: bool = False
    host: str = ""
    account: str = field(default="", repr=False)
    keychain_service: str = field(default="", repr=False)
    port: int = 993
    mailbox: str = "INBOX"
    trusted_authserv_ids: tuple[str, ...] = ()
    timeout_seconds: float = 15.0
    max_search_window: timedelta = _DEFAULT_MAX_SEARCH_WINDOW
    max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS
    max_candidate_uids: int = _DEFAULT_MAX_CANDIDATE_UIDS

    def validate(self) -> None:
        """Validate the complete enabled configuration without connecting."""

        _validate_config(self)


@dataclass
class IMAPMailboxProvider:
    """TLS-only, read-only implementation of ``MailboxProvider``."""

    config: IMAPProviderConfig
    credential_store: CredentialStore = field(repr=False)
    client_factory: IMAPClientFactory = field(
        default=lambda host, port, timeout, context: imaplib.IMAP4_SSL(
            host=host,
            port=port,
            ssl_context=context,
            timeout=timeout,
        ),
        repr=False,
    )
    now: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc),
        repr=False,
    )

    async def search_recent(
        self,
        *,
        recipient: str,
        since: datetime,
        limit: int,
    ) -> Sequence[MailboxMessage]:
        """Return a bounded projection of recent messages for one recipient.

        Synchronous ``imaplib`` work runs in a worker thread so the adapter's
        async orchestration loop is not blocked.
        """

        try:
            normalized_since = self._validate_request(recipient, since, limit)
            return await asyncio.to_thread(
                self._search_sync,
                recipient,
                normalized_since,
                limit,
            )
        except IMAPProviderDisabled:
            raise
        except IMAPProviderError:
            raise
        except Exception:
            raise IMAPProviderError("IMAP mailbox search failed") from None

    def _validate_request(
        self,
        recipient: str,
        since: datetime,
        limit: int,
    ) -> datetime:
        config = self.config
        if not config.enabled:
            raise IMAPProviderDisabled("IMAP mailbox access is disabled")

        _validate_config(config)
        if not isinstance(recipient, str) or not _EMAIL_PATTERN.fullmatch(recipient):
            raise IMAPProviderError("IMAP mailbox request is invalid")
        if recipient.casefold() != config.account.casefold():
            raise IMAPProviderError("IMAP mailbox request is outside the configured account")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
            raise IMAPProviderError("IMAP mailbox request limit is invalid")
        if not isinstance(since, datetime):
            raise IMAPProviderError("IMAP mailbox request time is invalid")

        normalized_since = _as_utc(since)
        current = _as_utc(self.now())
        if normalized_since > current + timedelta(minutes=2):
            raise IMAPProviderError("IMAP mailbox request time is invalid")
        if current - normalized_since > config.max_search_window:
            raise IMAPProviderError("IMAP mailbox request exceeds the allowed time window")
        return normalized_since

    def _search_sync(
        self,
        recipient: str,
        since: datetime,
        limit: int,
    ) -> tuple[MailboxMessage, ...]:
        config = self.config
        secret = self.credential_store.get(config.keychain_service, config.account)
        if not secret:
            raise IMAPProviderError("IMAP mailbox credential is unavailable")

        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        client: _IMAPClient | None = None
        selected = False
        try:
            client = self.client_factory(
                config.host,
                config.port,
                config.timeout_seconds,
                context,
            )
            status, _ = client.login(config.account, secret)
            del secret
            _require_ok(status)

            status, _ = client.select(config.mailbox, readonly=True)
            _require_ok(status)
            selected = True

            # IMAP dates always use English month abbreviations regardless of
            # the process locale.
            search_date = (
                f"{since.day:02d}-{_IMAP_MONTHS[since.month - 1]}-{since.year:04d}"
            )
            status, data = client.uid(
                "SEARCH",
                None,
                "SINCE",
                search_date,
                "TO",
                recipient,
            )
            _require_ok(status)
            uids = _parse_search_uids(data, config.max_candidate_uids)

            messages: list[MailboxMessage] = []
            for uid in reversed(uids):
                status, fetch_data = client.uid(
                    "FETCH",
                    uid.decode("ascii"),
                    f"(RFC822.SIZE INTERNALDATE BODY.PEEK[]<0.{config.max_message_bytes + 1}>)",
                )
                if not _is_ok(status):
                    continue
                parsed = _parse_fetch_response(
                    fetch_data,
                    uid=uid,
                    config=config,
                    recipient=recipient,
                    since=since,
                    now=_as_utc(self.now()),
                )
                if parsed is not None:
                    messages.append(parsed)
                if len(messages) >= limit:
                    break
            messages.sort(key=lambda message: message.received_at, reverse=True)
            return tuple(messages)
        except IMAPProviderError:
            raise
        except Exception:
            raise IMAPProviderError("IMAP mailbox search failed") from None
        finally:
            # ``secret`` is deliberately never retained on the provider.
            if "secret" in locals():
                del secret
            if client is not None:
                if selected:
                    try:
                        unselect = getattr(client, "unselect", None)
                        if callable(unselect):
                            unselect()
                    except Exception:
                        pass
                try:
                    client.logout()
                except Exception:
                    pass


def _validate_config(config: IMAPProviderConfig) -> None:
    if not isinstance(config.enabled, bool):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        not isinstance(config.host, str)
        or len(config.host) > 253
        or not _HOST_PATTERN.fullmatch(config.host)
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if not isinstance(config.account, str) or not _EMAIL_PATTERN.fullmatch(config.account):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        not isinstance(config.keychain_service, str)
        or not _SERVICE_PATTERN.fullmatch(config.keychain_service)
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        isinstance(config.port, bool)
        or not isinstance(config.port, int)
        or not 1 <= config.port <= 65_535
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        not isinstance(config.mailbox, str)
        or not _MAILBOX_PATTERN.fullmatch(config.mailbox)
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if not isinstance(config.trusted_authserv_ids, tuple) or any(
        not isinstance(value, str)
        or value != value.casefold()
        or len(value) > 253
        or not _HOST_PATTERN.fullmatch(value)
        for value in config.trusted_authserv_ids
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        isinstance(config.timeout_seconds, bool)
        or not isinstance(config.timeout_seconds, (int, float))
        or not math.isfinite(config.timeout_seconds)
        or not 1 <= config.timeout_seconds <= 60
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        not isinstance(config.max_search_window, timedelta)
        or not timedelta(minutes=1)
        <= config.max_search_window
        <= timedelta(hours=24)
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        isinstance(config.max_message_bytes, bool)
        or not isinstance(config.max_message_bytes, int)
        or not 16 * 1024 <= config.max_message_bytes <= 1024 * 1024
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        isinstance(config.max_output_chars, bool)
        or not isinstance(config.max_output_chars, int)
        or not 1_024 <= config.max_output_chars <= config.max_message_bytes
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")
    if (
        isinstance(config.max_candidate_uids, bool)
        or not isinstance(config.max_candidate_uids, int)
        or not 1 <= config.max_candidate_uids <= 250
    ):
        raise IMAPProviderError("IMAP provider configuration is invalid")


def _parse_search_uids(data: object, maximum: int) -> tuple[bytes, ...]:
    if not isinstance(data, (list, tuple)):
        raise IMAPProviderError("IMAP mailbox search returned an invalid response")
    chunks = [item for item in data if isinstance(item, bytes)]
    if sum(len(chunk) for chunk in chunks) > _MAX_SEARCH_RESPONSE_BYTES:
        raise IMAPProviderError("IMAP mailbox search returned too many results")
    raw_uids = b" ".join(chunks).split()
    if len(raw_uids) > maximum:
        raise IMAPProviderError("IMAP mailbox search returned too many results")
    if any(not _UID_PATTERN.fullmatch(uid) for uid in raw_uids):
        raise IMAPProviderError("IMAP mailbox search returned an invalid response")
    return tuple(dict.fromkeys(raw_uids))


def _parse_fetch_response(
    data: object,
    *,
    uid: bytes,
    config: IMAPProviderConfig,
    recipient: str,
    since: datetime,
    now: datetime,
) -> MailboxMessage | None:
    metadata, raw = _fetch_metadata_and_body(data)
    if raw is None or metadata is None:
        return None
    size_match = _SIZE_PATTERN.search(metadata)
    if size_match is None:
        return None
    declared_size = int(size_match.group(1))
    if declared_size > config.max_message_bytes or len(raw) > config.max_message_bytes:
        return None

    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return None
    if message.defects:
        return None

    received_at = _received_at(metadata, message)
    if received_at is None or received_at < since or received_at > now + timedelta(minutes=2):
        return None

    recipients = _recipient_addresses(message)
    if recipient.casefold() not in {value.casefold() for value in recipients}:
        return None

    text, html = _safe_bodies(message, config.max_output_chars)
    subject = _safe_header(message.get("Subject", ""), _MAX_HEADER_CHARS)
    sender_addresses = _addresses(
        tuple(str(value) for value in message.get_all("From", []))
    )
    sender = sender_addresses[0] if sender_addresses else ""
    if not sender:
        return None

    digest = hashlib.sha256(
        b"\0".join((config.host.casefold().encode("utf-8"), config.mailbox.encode("utf-8"), uid))
    ).hexdigest()[:24]
    return MailboxMessage(
        message_id=f"imap:{digest}",
        received_at=received_at,
        sender=sender,
        recipients=recipients,
        subject=subject,
        text=text,
        html=html,
        authentication=_authentication_evidence(
            message,
            trusted_authserv_ids=config.trusted_authserv_ids,
        ),
    )


def _authentication_evidence(
    message: Message,
    *,
    trusted_authserv_ids: tuple[str, ...],
) -> MailAuthenticationEvidence:
    """Reduce Authentication-Results to typed outcomes, never raw headers.

    Only headers created by a configured, trusted receiving authentication
    service are considered.  This prevents an untrusted sender from adding a
    forged ``Authentication-Results`` header that claims PASS.  Among trusted
    headers, any explicit non-pass result wins for that mechanism so ambiguous
    or conflicting evidence fails closed.  Missing, oversized, malformed, or
    untrusted evidence remains ``UNKNOWN``.
    """

    raw_headers = tuple(message.get_all("Authentication-Results", ()))
    if (
        not trusted_authserv_ids
        or not raw_headers
        or len(raw_headers) > _MAX_AUTHENTICATION_RESULTS_HEADERS
    ):
        return MailAuthenticationEvidence()

    trusted = frozenset(trusted_authserv_ids)
    results: dict[str, list[MailAuthenticationResult]] = {
        "spf": [],
        "dkim": [],
        "dmarc": [],
    }
    for raw_header in raw_headers:
        header = str(raw_header)
        if len(header) > _MAX_AUTHENTICATION_RESULTS_HEADER_CHARS:
            return MailAuthenticationEvidence()
        authserv_segment, separator, authenticated_results = header.partition(";")
        if not separator:
            continue
        authserv_tokens = authserv_segment.strip().split(None, 1)
        if not authserv_tokens:
            continue
        authserv_token = authserv_tokens[0]
        authserv_id = authserv_token.split("/", 1)[0].casefold()
        if authserv_id not in trusted:
            continue
        for match in _AUTHENTICATION_RESULT_PATTERN.finditer(
            authenticated_results
        ):
            mechanism = match.group(1).casefold()
            value = match.group(2).casefold()
            results[mechanism].append(
                MailAuthenticationResult.PASS
                if value == "pass"
                else MailAuthenticationResult.FAIL
            )

    def reduced(mechanism: str) -> MailAuthenticationResult:
        values = results[mechanism]
        if not values:
            return MailAuthenticationResult.UNKNOWN
        if all(value is MailAuthenticationResult.PASS for value in values):
            return MailAuthenticationResult.PASS
        return MailAuthenticationResult.FAIL

    return MailAuthenticationEvidence(
        spf=reduced("spf"),
        dkim=reduced("dkim"),
        dmarc=reduced("dmarc"),
    )


def _fetch_metadata_and_body(data: object) -> tuple[bytes | None, bytes | None]:
    if not isinstance(data, (list, tuple)):
        return None, None
    for item in data:
        if (
            isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            return item[0], item[1]
    return None, None


def _received_at(metadata: bytes, message: Message) -> datetime | None:
    match = _INTERNAL_DATE_PATTERN.search(metadata)
    if match is not None:
        try:
            value = datetime.strptime(
                match.group(1).decode("ascii"),
                "%d-%b-%Y %H:%M:%S %z",
            )
            return _as_utc(value)
        except (UnicodeDecodeError, ValueError):
            return None
    try:
        value = parsedate_to_datetime(str(message.get("Date", "")))
    except (TypeError, ValueError, OverflowError):
        return None
    return _as_utc(value) if value is not None else None


def _recipient_addresses(message: Message) -> tuple[str, ...]:
    headers: list[str] = []
    for name in ("To", "Cc", "Delivered-To", "X-Original-To"):
        headers.extend(str(value) for value in message.get_all(name, []))
    return _addresses(headers)


def _addresses(headers: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for _, address in getaddresses(headers):
        normalized = _safe_header(address, 320).casefold()
        if _EMAIL_PATTERN.fullmatch(normalized) and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _safe_bodies(message: Message, maximum: int) -> tuple[str, str]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    total = 0
    parts = message.walk() if message.is_multipart() else (message,)
    for index, part in enumerate(parts):
        if index >= _MAX_MIME_PARTS:
            break
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type().casefold()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not isinstance(payload, bytes):
            continue
        remaining = maximum - total
        if remaining <= 0:
            break
        payload = payload[:remaining]
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeError):
            decoded = payload.decode("utf-8", errors="replace")
        decoded = _safe_text(decoded, remaining)
        total += len(decoded)
        if content_type == "text/plain":
            text_parts.append(decoded)
        else:
            html_parts.append(decoded)
    return "\n".join(text_parts), "\n".join(html_parts)


def _safe_header(value: object, maximum: int) -> str:
    return _safe_text(str(value), maximum).strip()


def _safe_text(value: str, maximum: int) -> str:
    # Keep line structure needed by artifact extraction while removing NUL and
    # other control characters that are unsafe in downstream diagnostics.
    cleaned = "".join(
        character
        for character in value[:maximum]
        if character in "\n\t" or ord(character) >= 32
    )
    return cleaned


def _is_ok(status: object) -> bool:
    if isinstance(status, bytes):
        status = status.decode("ascii", errors="ignore")
    return isinstance(status, str) and status.upper() == "OK"


def _require_ok(status: object) -> None:
    if not _is_ok(status):
        raise IMAPProviderError("IMAP mailbox operation was not accepted")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
