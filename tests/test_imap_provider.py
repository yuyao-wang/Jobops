from __future__ import annotations

import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP

import pytest

from auth import (
    IMAPMailboxProvider,
    IMAPProviderConfig,
    IMAPProviderDisabled,
    IMAPProviderError,
    InMemoryCredentialStore,
    MailAuthenticationResult,
)


NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
ACCOUNT = "candidate@example.test"
SERVICE = "jobops.test.mailbox"
SECRET = "synthetic-password-never-report"


def _message(
    *,
    subject: str = "Verify your ExampleCo account",
    text: str = "ExampleCo verification code: 123456",
    recipient: str = ACCOUNT,
    authentication_results: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = "Workday <noreply@myworkday.com>"
    message["To"] = recipient
    message["Date"] = "Tue, 14 Jul 2026 17:58:00 +0000"
    message["Subject"] = subject
    message["Message-ID"] = "<raw-id-must-not-be-exposed@example.test>"
    if authentication_results is not None:
        message["Authentication-Results"] = authentication_results
    message.set_content(text)
    message.add_alternative(
        "<p>ExampleCo verification code: <strong>123456</strong></p>",
        subtype="html",
    )
    return message.as_bytes(policy=SMTP)


class FakeIMAPClient:
    def __init__(self, messages: dict[str, bytes] | None = None, login_error=None):
        self.messages = messages or {"41": _message()}
        self.login_error = login_error
        self.calls: list[tuple] = []
        self.logged_out = False
        self.unselected = False

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.login_error is not None:
            raise self.login_error(password)
        return "OK", [b"authenticated"]

    def select(self, mailbox="INBOX", readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode("ascii")]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(self.messages).encode("ascii")]
        if command == "FETCH":
            uid = str(args[0])
            raw = self.messages[uid]
            metadata = (
                f'{uid} (RFC822.SIZE {len(raw)} '
                'INTERNALDATE "14-Jul-2026 17:58:00 +0000" BODY[] '
                f'{{{len(raw)}}}'
            ).encode("ascii")
            return "OK", [(metadata, raw), b")"]
        raise AssertionError(f"unexpected command: {command}")

    def unselect(self):
        self.calls.append(("unselect",))
        self.unselected = True
        return "OK", [b""]

    def logout(self):
        self.calls.append(("logout",))
        self.logged_out = True
        return "BYE", [b""]


class FakeFactory:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def __call__(self, host, port, timeout, context):
        self.calls.append((host, port, timeout, context))
        return self.client


def _provider(
    client: FakeIMAPClient,
    *,
    enabled: bool = True,
    store: InMemoryCredentialStore | None = None,
    **config_overrides,
) -> tuple[IMAPMailboxProvider, FakeFactory, InMemoryCredentialStore]:
    credential_store = store or InMemoryCredentialStore()
    if store is None:
        credential_store.set(SERVICE, ACCOUNT, SECRET)
    factory = FakeFactory(client)
    config = IMAPProviderConfig(
        enabled=enabled,
        host="imap.example.test",
        account=ACCOUNT,
        keychain_service=SERVICE,
        trusted_authserv_ids=("mx.example.test",),
        **config_overrides,
    )
    provider = IMAPMailboxProvider(
        config,
        credential_store,
        client_factory=factory,
        now=lambda: NOW,
    )
    return provider, factory, credential_store


@pytest.mark.asyncio
async def test_imap_provider_is_disabled_by_default_and_never_connects():
    client = FakeIMAPClient()
    factory = FakeFactory(client)
    provider = IMAPMailboxProvider(
        IMAPProviderConfig(),
        InMemoryCredentialStore(),
        client_factory=factory,
        now=lambda: NOW,
    )

    with pytest.raises(IMAPProviderDisabled, match="disabled"):
        await provider.search_recent(
            recipient=ACCOUNT,
            since=NOW - timedelta(minutes=5),
            limit=1,
        )

    assert factory.calls == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_imap_provider_uses_tls_keychain_secret_and_read_only_search():
    client = FakeIMAPClient()
    provider, factory, _ = _provider(client)

    messages = await provider.search_recent(
        recipient=ACCOUNT,
        since=NOW - timedelta(minutes=5),
        limit=2,
    )

    assert len(messages) == 1
    projected = messages[0]
    assert projected.recipients == (ACCOUNT,)
    assert projected.sender == "noreply@myworkday.com"
    assert projected.received_at == NOW - timedelta(minutes=2)
    assert projected.message_id.startswith("imap:")
    assert "raw-id-must-not-be-exposed" not in projected.message_id
    assert "123456" in projected.text
    assert "123456" in projected.html
    assert projected.authentication.spf is MailAuthenticationResult.UNKNOWN
    assert projected.authentication.dkim is MailAuthenticationResult.UNKNOWN
    assert projected.authentication.dmarc is MailAuthenticationResult.UNKNOWN

    assert client.calls[0] == ("login", ACCOUNT, SECRET)
    assert ("select", "INBOX", True) in client.calls
    search = next(call for call in client.calls if call[:2] == ("uid", "SEARCH"))
    assert search[2:] == (None, "SINCE", "14-Jul-2026", "TO", ACCOUNT)
    fetch = next(call for call in client.calls if call[:2] == ("uid", "FETCH"))
    assert "BODY.PEEK[]" in fetch[-1]
    assert not any(call[:2] == ("uid", "STORE") for call in client.calls)
    assert client.unselected is True
    assert client.logged_out is True

    assert len(factory.calls) == 1
    host, port, timeout, context = factory.calls[0]
    assert (host, port, timeout) == ("imap.example.test", 993, 15.0)
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.mark.asyncio
async def test_imap_provider_projects_sanitized_sender_authentication_evidence():
    raw_marker = "raw-authserv-marker.example.test"
    authenticated = _message(
        authentication_results=(
            "mx.example.test; spf=pass smtp.mailfrom=linkedin.com; "
            "dkim=pass header.d=linkedin.com; "
            f"dmarc=pass header.from=linkedin.com ({raw_marker})"
        )
    )
    provider, _, _ = _provider(FakeIMAPClient({"41": authenticated}))

    messages = await provider.search_recent(
        recipient=ACCOUNT,
        since=NOW - timedelta(minutes=5),
        limit=1,
    )

    evidence = messages[0].authentication
    assert evidence.spf is MailAuthenticationResult.PASS
    assert evidence.dkim is MailAuthenticationResult.PASS
    assert evidence.dmarc is MailAuthenticationResult.PASS
    assert evidence.sender_is_authenticated is True
    assert raw_marker not in repr(messages[0])


@pytest.mark.asyncio
async def test_imap_provider_ignores_forged_untrusted_authentication_results():
    forged = _message(
        authentication_results=(
            "attacker.example.test; spf=pass smtp.mailfrom=linkedin.com; "
            "dkim=pass header.d=linkedin.com; "
            "dmarc=pass header.from=linkedin.com"
        )
    )
    provider, _, _ = _provider(FakeIMAPClient({"41": forged}))

    messages = await provider.search_recent(
        recipient=ACCOUNT,
        since=NOW - timedelta(minutes=5),
        limit=1,
    )

    evidence = messages[0].authentication
    assert evidence.spf is MailAuthenticationResult.UNKNOWN
    assert evidence.dkim is MailAuthenticationResult.UNKNOWN
    assert evidence.dmarc is MailAuthenticationResult.UNKNOWN
    assert evidence.sender_is_authenticated is False


@pytest.mark.asyncio
async def test_imap_provider_treats_malformed_authserv_id_as_unknown():
    malformed = _message(
        authentication_results=(
            "; spf=pass; dkim=pass; dmarc=pass"
        )
    )
    provider, _, _ = _provider(FakeIMAPClient({"41": malformed}))

    messages = await provider.search_recent(
        recipient=ACCOUNT,
        since=NOW - timedelta(minutes=5),
        limit=1,
    )

    assert messages[0].authentication.sender_is_authenticated is False


@pytest.mark.asyncio
async def test_imap_provider_enforces_recipient_time_and_limit_before_connecting():
    client = FakeIMAPClient()
    provider, factory, _ = _provider(client)

    requests = (
        {
            "recipient": "other@example.test",
            "since": NOW - timedelta(minutes=5),
            "limit": 1,
        },
        {
            "recipient": ACCOUNT,
            "since": NOW - timedelta(hours=2),
            "limit": 1,
        },
        {
            "recipient": ACCOUNT,
            "since": NOW - timedelta(minutes=5),
            "limit": 26,
        },
    )
    for request in requests:
        with pytest.raises(IMAPProviderError):
            await provider.search_recent(**request)

    assert factory.calls == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_imap_provider_rejects_imap_metacharacters_before_connecting():
    client = FakeIMAPClient()
    provider, factory, _ = _provider(client)
    unsafe_recipient = 'candidate@example.test")OR ALL('

    with pytest.raises(IMAPProviderError, match="invalid"):
        await provider.search_recent(
            recipient=unsafe_recipient,
            since=NOW - timedelta(minutes=5),
            limit=1,
        )

    assert factory.calls == []
    assert client.calls == []


@pytest.mark.asyncio
async def test_imap_provider_caps_results_and_fetches_only_requested_count():
    messages = {
        "40": _message(subject="older"),
        "41": _message(subject="newer"),
    }
    client = FakeIMAPClient(messages)
    provider, _, _ = _provider(client)

    result = await provider.search_recent(
        recipient=ACCOUNT,
        since=NOW - timedelta(minutes=5),
        limit=1,
    )

    assert len(result) == 1
    fetches = [call for call in client.calls if call[:2] == ("uid", "FETCH")]
    assert [call[2] for call in fetches] == ["41"]


@pytest.mark.asyncio
async def test_imap_provider_fails_closed_when_search_response_is_too_broad():
    client = FakeIMAPClient({str(uid): _message() for uid in range(1, 102)})
    provider, _, _ = _provider(client, max_candidate_uids=100)

    with pytest.raises(IMAPProviderError, match="too many"):
        await provider.search_recent(
            recipient=ACCOUNT,
            since=NOW - timedelta(minutes=5),
            limit=1,
        )

    assert not any(call[:2] == ("uid", "FETCH") for call in client.calls)
    assert client.logged_out is True


@pytest.mark.asyncio
async def test_imap_provider_skips_oversize_and_malformed_messages():
    oversize = _message(text="x" * (20 * 1024))
    malformed = b"From: nobody@example.test\r\nBroken Header\r\n\r\nbody"
    client = FakeIMAPClient({"40": malformed, "41": oversize})
    provider, _, _ = _provider(
        client,
        max_message_bytes=16 * 1024,
        max_output_chars=8 * 1024,
    )

    result = await provider.search_recent(
        recipient=ACCOUNT,
        since=NOW - timedelta(minutes=5),
        limit=2,
    )

    assert result == ()


@pytest.mark.asyncio
async def test_imap_provider_sanitizes_authentication_errors_and_repr():
    client = FakeIMAPClient(
        login_error=lambda password: RuntimeError(f"server rejected {password}")
    )
    provider, _, _ = _provider(client)

    with pytest.raises(IMAPProviderError) as captured:
        await provider.search_recent(
            recipient=ACCOUNT,
            since=NOW - timedelta(minutes=5),
            limit=1,
        )

    assert SECRET not in str(captured.value)
    assert SECRET not in repr(provider)
    assert ACCOUNT not in repr(provider)
    assert client.logged_out is True


@pytest.mark.asyncio
async def test_imap_provider_requires_existing_keychain_secret_without_connecting():
    client = FakeIMAPClient()
    empty_store = InMemoryCredentialStore()
    provider, factory, _ = _provider(client, store=empty_store)

    with pytest.raises(IMAPProviderError, match="unavailable"):
        await provider.search_recent(
            recipient=ACCOUNT,
            since=NOW - timedelta(minutes=5),
            limit=1,
        )

    assert factory.calls == []
    assert client.calls == []
