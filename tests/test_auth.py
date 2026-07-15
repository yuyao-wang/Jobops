from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from auth.credentials import InMemoryCredentialStore
from auth.mailbox import (
    CorrelatedMailboxVerifier,
    MailboxMessage,
    MailboxVerificationStatus,
    VerificationArtifactKind,
    VerificationRequest,
)
from utils.keychain import (
    KeychainError,
    get_workday_credential,
    legacy_workday_services,
    save_workday_credential,
    workday_service,
)


WORKDAY_URL = "https://exampleco.wd5.myworkdayjobs.com/External/job/example"
NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)


def test_in_memory_credential_store_round_trip():
    store = InMemoryCredentialStore()
    store.set("service", "account", "secret")
    assert store.get("service", "account") == "secret"
    assert store.delete("service", "account") is True
    assert store.get("service", "account") is None


def test_workday_credential_round_trip_uses_injected_store():
    store = InMemoryCredentialStore()
    save_workday_credential(
        WORKDAY_URL, "candidate@example.test", "secret", store=store
    )
    result = get_workday_credential(
        WORKDAY_URL, "candidate@example.test", store=store
    )
    assert result is not None
    assert result.password == "secret"
    assert result.service == workday_service(WORKDAY_URL)


def test_credential_representations_never_include_secrets():
    secret = "synthetic-secret-never-print"
    store = InMemoryCredentialStore()
    store.set("service", "account", secret)
    assert secret not in repr(store)

    save_workday_credential(
        WORKDAY_URL, "candidate@example.test", secret, store=store
    )
    credential = get_workday_credential(
        WORKDAY_URL, "candidate@example.test", store=store
    )
    assert credential is not None
    assert secret not in repr(credential)


class SilentWriteCredentialStore:
    def get(self, _service, _account):
        return None

    def set(self, _service, _account, _secret):
        return None

    def delete(self, _service, _account):
        return False


def test_keychain_save_requires_secret_readback_verification():
    with pytest.raises(KeychainError, match="verification failed"):
        save_workday_credential(
            WORKDAY_URL,
            "candidate@example.test",
            "synthetic-secret",
            store=SilentWriteCredentialStore(),
        )


def test_legacy_workday_credential_is_migrated_after_verified_write():
    store = InMemoryCredentialStore()
    legacy = legacy_workday_services(WORKDAY_URL)[0]
    store.set(legacy, "candidate@example.test", "legacy-secret")

    result = get_workday_credential(
        WORKDAY_URL, "candidate@example.test", store=store
    )

    assert result is not None
    assert result.migrated_from == legacy
    assert store.get(workday_service(WORKDAY_URL), "candidate@example.test") == "legacy-secret"
    assert store.get(legacy, "candidate@example.test") is None


def test_keychain_implementation_does_not_shell_out_with_passwords():
    source = (Path(__file__).parents[1] / "auth" / "credentials.py").read_text()
    compatibility = (Path(__file__).parents[1] / "utils" / "keychain.py").read_text()
    assert "import subprocess" not in source
    assert "import subprocess" not in compatibility
    assert "security add-generic-password" not in source
    assert '"-w"' not in compatibility


class RecordingMailbox:
    def __init__(self, messages=(), error=None):
        self.messages = list(messages)
        self.error = error
        self.calls = []

    async def search_recent(self, *, recipient, since, limit):
        self.calls.append({"recipient": recipient, "since": since, "limit": limit})
        if self.error:
            raise self.error
        return self.messages


def verification_message(message_id="m1", code="123456", minutes_ago=2):
    return MailboxMessage(
        message_id=message_id,
        received_at=NOW - timedelta(minutes=minutes_ago),
        sender="Workday <noreply@myworkday.com>",
        recipients=("candidate@example.test",),
        subject="Verify your ExampleCo Workday account",
        text=f"ExampleCo verification code: {code}",
    )


def verification_request():
    return VerificationRequest(
        recipient="candidate@example.test",
        tenant_host="exampleco.wd5.myworkdayjobs.com",
        initiated_at=NOW - timedelta(minutes=3),
        correlation_terms=("ExampleCo",),
    )


@pytest.mark.asyncio
async def test_mailbox_verifier_queries_only_recent_correlated_mail():
    provider = RecordingMailbox([verification_message()])
    verifier = CorrelatedMailboxVerifier(provider, now=lambda: NOW)

    result = await verifier.find_verification(verification_request())

    assert result.status is MailboxVerificationStatus.FOUND
    assert result.artifact.kind is VerificationArtifactKind.CODE
    assert result.artifact.value == "123456"
    assert provider.calls[0]["recipient"] == "candidate@example.test"
    assert provider.calls[0]["since"] >= NOW - timedelta(minutes=15)
    assert provider.calls[0]["limit"] <= 25


@pytest.mark.asyncio
async def test_mailbox_verifier_hands_off_ambiguous_matches():
    provider = RecordingMailbox([
        verification_message("m1", "123456", 2),
        verification_message("m2", "654321", 1),
    ])
    result = await CorrelatedMailboxVerifier(provider, now=lambda: NOW).find_verification(
        verification_request()
    )
    assert result.status is MailboxVerificationStatus.AMBIGUOUS
    assert result.requires_handoff is True
    assert result.artifact is None


@pytest.mark.asyncio
async def test_mailbox_verifier_hands_off_when_provider_unavailable():
    provider = RecordingMailbox(error=RuntimeError("offline"))
    result = await CorrelatedMailboxVerifier(provider, now=lambda: NOW).find_verification(
        verification_request()
    )
    assert result.status is MailboxVerificationStatus.UNAVAILABLE
    assert "offline" not in result.reason


@pytest.mark.asyncio
async def test_mailbox_verifier_ignores_untrusted_or_unrelated_messages():
    message = MailboxMessage(
        message_id="m1",
        received_at=NOW - timedelta(minutes=1),
        sender="Unknown <attacker@example.invalid>",
        recipients=("candidate@example.test",),
        subject="Verify now",
        text="ExampleCo verification code: 123456",
    )
    provider = RecordingMailbox([message])
    result = await CorrelatedMailboxVerifier(provider, now=lambda: NOW).find_verification(
        verification_request()
    )
    assert result.status is MailboxVerificationStatus.NOT_FOUND
    assert result.artifact is None
