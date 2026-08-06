from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auth.mailbox import (
    MailAuthenticationEvidence,
    MailAuthenticationResult,
    MailboxMessage,
)
from core.job_alerts import (
    JobAlertInboxConfig,
    JobAlertInboxIngestor,
    JobAlertIngestionStatus,
    JobAlertIssueCode,
    JobAlertLeadSource,
    JobAlertLeadOrigin,
    JobAlertLeadUrlKind,
    JobAlertPersistenceStatus,
    ingest_job_alerts_for_subject,
)
from core.job_leads import (
    JobLead,
    JobLeadOrigin,
    JobLeadSource,
    PrivateHomeJobLeadRepository,
)
from core.private_home import PrivateHome


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)
RECIPIENT = "candidate@example.test"
AUTHENTICATED = MailAuthenticationEvidence(
    spf=MailAuthenticationResult.PASS,
    dkim=MailAuthenticationResult.PASS,
    dmarc=MailAuthenticationResult.PASS,
)


class RecordingMailbox:
    def __init__(self, messages=(), error: Exception | None = None):
        self.messages = tuple(messages)
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def search_recent(self, *, recipient, since, limit):
        self.calls.append({"recipient": recipient, "since": since, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.messages


def config(**overrides) -> JobAlertInboxConfig:
    values = {
        "enabled": True,
        "recipient": RECIPIENT,
        "max_age": timedelta(hours=2),
        "max_messages": 10,
    }
    values.update(overrides)
    return JobAlertInboxConfig(**values)


def message(
    *,
    message_id: str = "message-raw-id@example.test",
    sender: str = "LinkedIn Job Alerts <jobs-noreply@linkedin.com>",
    recipients: tuple[str, ...] = (RECIPIENT,),
    received_at: datetime = NOW - timedelta(minutes=10),
    subject: str = "New jobs for you",
    text: str = "",
    html: str = "",
    authentication: MailAuthenticationEvidence = AUTHENTICATED,
) -> MailboxMessage:
    return MailboxMessage(
        message_id=message_id,
        received_at=received_at,
        sender=sender,
        recipients=recipients,
        subject=subject,
        text=text,
        html=html,
        authentication=authentication,
    )


@pytest.mark.asyncio
async def test_disabled_inbox_never_calls_provider_even_without_recipient():
    provider = RecordingMailbox(error=AssertionError("must not be called"))

    result = await JobAlertInboxIngestor(
        provider,
        JobAlertInboxConfig(enabled=False),
        now=lambda: NOW,
    ).ingest()

    assert result.status is JobAlertIngestionStatus.DISABLED
    assert result.provider_called is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_ingestion_is_bounded_and_canonicalizes_platform_job_links():
    linkedin = message(
        html=(
            '<a href="https://www.linkedin.com/jobs/view/1234567890?trk=alert-secret">'
            "Machine Learning Engineer</a>"
            '<a href="https://www.linkedin.com/login">Login</a>'
            '<a href="https://www.linkedin.com/unsubscribe?token=mail-secret">Unsubscribe</a>'
        )
    )
    indeed = message(
        message_id="indeed-raw-id@example.test",
        sender="Indeed <alerts@alerts.indeed.com>",
        text=(
            "https://ca.indeed.com/viewjob?jk=abc123def456&utm_source=alert&from=mail "
            "https://ca.indeed.com/account/security"
        ),
    )
    provider = RecordingMailbox((linkedin, indeed))

    result = await JobAlertInboxIngestor(provider, config(), now=lambda: NOW).ingest()

    assert result.status is JobAlertIngestionStatus.SUCCEEDED
    assert provider.calls == [
        {
            "recipient": RECIPIENT,
            "since": NOW - timedelta(hours=2),
            "limit": 10,
        }
    ]
    assert [lead.source_url for lead in result.leads] == [
        "https://www.linkedin.com/jobs/view/1234567890",
        "https://www.indeed.com/viewjob?jk=abc123def456",
    ]
    assert result.leads[0].source is JobAlertLeadSource.LINKEDIN_ALERT_EMAIL
    assert result.leads[1].source is JobAlertLeadSource.INDEED_ALERT_EMAIL
    assert all(lead.url_kind is JobAlertLeadUrlKind.PLATFORM_LEAD for lead in result.leads)
    assert [lead.origin for lead in result.leads] == [
        JobAlertLeadOrigin.LINKEDIN_SEARCH_INDEX,
        JobAlertLeadOrigin.INDEED_SEARCH_INDEX,
    ]
    assert result.leads[0].title_hint == "Machine Learning Engineer"


@pytest.mark.asyncio
async def test_alert_anchor_preserves_explicit_company_and_title_hints():
    alert = message(
        html=(
            '<a href="https://www.linkedin.com/jobs/view/4234567890">'
            "Machine Learning Engineer at Example Robotics | LinkedIn</a>"
        ),
    )

    result = await JobAlertInboxIngestor(
        RecordingMailbox((alert,)),
        config(),
        now=lambda: NOW,
    ).ingest()

    assert result.status is JobAlertIngestionStatus.SUCCEEDED
    assert result.leads[0].title_hint == "Machine Learning Engineer"
    assert result.leads[0].company_hint == "Example Robotics"
    assert all(lead.is_authoritative is False for lead in result.leads)

    projection = repr(result)
    assert "message-raw-id" not in projection
    assert "candidate@example.test" not in projection
    assert "alert-secret" not in projection
    assert "mail-secret" not in projection


@pytest.mark.asyncio
async def test_sender_authentication_fails_closed_and_passed_evidence_is_accepted():
    forged_from = message(
        text="https://www.linkedin.com/jobs/view/1234567890",
        authentication=MailAuthenticationEvidence(
            spf=MailAuthenticationResult.PASS,
            dkim=MailAuthenticationResult.PASS,
            dmarc=MailAuthenticationResult.FAIL,
        ),
    )
    unknown = message(
        message_id="unknown-auth",
        text="https://www.linkedin.com/jobs/view/2234567890",
        authentication=MailAuthenticationEvidence(),
    )

    rejected = await JobAlertInboxIngestor(
        RecordingMailbox((forged_from, unknown)),
        config(),
        now=lambda: NOW,
    ).ingest()

    assert rejected.leads == ()
    assert {issue.code for issue in rejected.issues} == {
        JobAlertIssueCode.SENDER_NOT_AUTHENTICATED
    }
    assert "Authentication-Results" not in repr(rejected)

    accepted = await JobAlertInboxIngestor(
        RecordingMailbox(
            (
                message(
                    message_id="authenticated-alert",
                    text="https://www.linkedin.com/jobs/view/3234567890",
                ),
            )
        ),
        config(),
        now=lambda: NOW,
    ).ingest()

    assert accepted.status is JobAlertIngestionStatus.SUCCEEDED
    assert [lead.source_url for lead in accepted.leads] == [
        "https://www.linkedin.com/jobs/view/3234567890"
    ]


@pytest.mark.asyncio
async def test_platform_wrappers_are_canonicalized_and_official_ats_link_is_retained():
    alert = message(
        html=(
            '<a href="https://www.linkedin.com/comm/jobs/view/4234567890?trk=mail">'
            "ML Engineer</a>"
            '<a href="https://ca.indeed.com/rc/clk?jk=wrapper12345&amp;from=alert">'
            "Data Engineer</a>"
            '<a href="https://boards.greenhouse.io/example/jobs/456?gh_jid=456&amp;utm_source=linkedin">'
            "Platform Engineer</a>"
        ),
    )

    result = await JobAlertInboxIngestor(
        RecordingMailbox((alert,)),
        config(),
        now=lambda: NOW,
    ).ingest()

    assert result.status is JobAlertIngestionStatus.SUCCEEDED
    assert [lead.source_url for lead in result.leads] == [
        "https://www.linkedin.com/jobs/view/4234567890",
        "https://www.indeed.com/viewjob?jk=wrapper12345",
        "https://boards.greenhouse.io/example/jobs/456?gh_jid=456",
    ]
    assert all(
        lead.source is JobAlertLeadSource.LINKEDIN_ALERT_EMAIL
        for lead in result.leads
    )
    assert result.leads[-1].url_kind is JobAlertLeadUrlKind.OFFICIAL_CANDIDATE
    assert result.leads[-1].origin is JobAlertLeadOrigin.ATS


@pytest.mark.asyncio
async def test_employer_and_ats_links_are_only_unverified_official_candidates():
    alert = message(
        sender="Example Careers <jobs@careers.example.com>",
        html=(
            '<a href="https://boards.greenhouse.io/example/jobs/456?gh_jid=456&utm_campaign=mail">'
            "Platform Engineer</a>"
            '<a href="https://www.example.com/careers/openings/789?utm_source=email">'
            "Data Engineer</a>"
            '<a href="https://career5.successfactors.eu/career?company=example&amp;career_ns=JOB_LISTING&amp;career_job_req_id=sf-123&amp;rcm_site_locale=en_US&amp;utm_source=email">'
            "Applied Scientist</a>"
            '<a href="https://example.workdayjobs.com/jobs/wd-456?jobReqId=R-789&amp;utm_source=email&amp;token=secret">'
            "ML Engineer</a>"
        ),
    )
    provider = RecordingMailbox((alert,))

    result = await JobAlertInboxIngestor(
        provider,
        config(allowed_sender_domains=("linkedin.com", "indeed.com", "example.com")),
        now=lambda: NOW,
    ).ingest()

    assert result.status is JobAlertIngestionStatus.SUCCEEDED
    assert [lead.source_url for lead in result.leads] == [
        "https://boards.greenhouse.io/example/jobs/456?gh_jid=456",
        "https://www.example.com/careers/openings/789",
        "https://career5.successfactors.eu/career?company=example&career_job_req_id=sf-123&career_ns=job_listing",
        "https://example.workdayjobs.com/jobs/wd-456?jobReqId=R-789",
    ]
    assert all(
        lead.source is JobAlertLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL
        for lead in result.leads
    )
    assert all(
        lead.url_kind is JobAlertLeadUrlKind.OFFICIAL_CANDIDATE
        and lead.is_authoritative is False
        for lead in result.leads
    )
    assert [lead.origin for lead in result.leads] == [
        JobAlertLeadOrigin.ATS,
        JobAlertLeadOrigin.UNKNOWN_WEB,
        JobAlertLeadOrigin.ATS,
        JobAlertLeadOrigin.ATS,
    ]


@pytest.mark.asyncio
async def test_myworkdaysite_alert_is_classified_as_an_ats_candidate():
    alert = message(
        sender="Example Careers <jobs@careers.example.com>",
        html=(
            '<a href="https://tenant.myworkdaysite.com/jobs/synthetic-123">'
            "Platform Engineer at Example Careers</a>"
        ),
    )

    result = await JobAlertInboxIngestor(
        RecordingMailbox((alert,)),
        config(allowed_sender_domains=("example.com",)),
        now=lambda: NOW,
    ).ingest()

    assert len(result.leads) == 1
    assert result.leads[0].origin is JobAlertLeadOrigin.ATS
    assert result.leads[0].url_kind is JobAlertLeadUrlKind.OFFICIAL_CANDIDATE


@pytest.mark.asyncio
async def test_spoofed_sender_is_rejected_without_exposing_identity():
    spoofed = message(
        sender="LinkedIn jobs-noreply@linkedin.com <attacker@example.invalid>",
        text="https://www.linkedin.com/jobs/view/1234567890",
    )
    result = await JobAlertInboxIngestor(
        RecordingMailbox((spoofed,)),
        config(),
        now=lambda: NOW,
    ).ingest()

    assert result.status is JobAlertIngestionStatus.PARTIAL_FAILURE
    assert result.leads == ()
    assert result.issues[0].code is JobAlertIssueCode.SENDER_NOT_ALLOWED
    assert "attacker" not in repr(result)
    assert "message-raw-id" not in repr(result)

    multiple_addresses = message(
        sender="alerts@linkedin.com, attacker@example.invalid",
        text="https://www.linkedin.com/jobs/view/1234567890",
    )
    multiple_result = await JobAlertInboxIngestor(
        RecordingMailbox((multiple_addresses,)),
        config(),
        now=lambda: NOW,
    ).ingest()
    assert multiple_result.leads == ()
    assert multiple_result.issues[0].code is JobAlertIssueCode.SENDER_NOT_ALLOWED


@pytest.mark.asyncio
async def test_userinfo_abnormal_ports_and_account_links_are_rejected():
    unsafe = message(
        text=" ".join(
            (
                "https://user:password@www.linkedin.com/jobs/view/1234567890",
                "https://www.linkedin.com:8443/jobs/view/1234567890",
                "https://www.linkedin.com/jobs/login/1234567890",
                "https://www.linkedin.com/jobs/view/not-a-job-id",
                "https://www.indeed.com/viewjob?jk=abc123&unsubscribe=yes",
            )
        )
    )

    result = await JobAlertInboxIngestor(
        RecordingMailbox((unsafe,)),
        config(),
        now=lambda: NOW,
    ).ingest()

    assert result.leads == ()
    assert result.issues[0].code is JobAlertIssueCode.NO_JOB_LINKS


@pytest.mark.asyncio
async def test_recipient_time_and_security_subject_checks_are_typed():
    messages = (
        message(message_id="wrong-recipient", recipients=("other@example.test",)),
        message(message_id="old", received_at=NOW - timedelta(days=1)),
        message(
            message_id="security",
            subject="Security alert: reset your password",
            text="https://www.linkedin.com/jobs/view/1234567890",
        ),
    )

    result = await JobAlertInboxIngestor(
        RecordingMailbox(messages),
        config(),
        now=lambda: NOW,
    ).ingest()

    assert result.leads == ()
    assert {issue.code for issue in result.issues} == {
        JobAlertIssueCode.RECIPIENT_MISMATCH,
        JobAlertIssueCode.OUTSIDE_TIME_WINDOW,
        JobAlertIssueCode.UNSAFE_MESSAGE,
    }


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_messages_report_partial_failure():
    valid = message(text="https://www.linkedin.com/jobs/view/1234567890")
    invalid = message(
        message_id="spoof",
        sender="Attacker <alerts@example.invalid>",
        text="https://www.linkedin.com/jobs/view/9999999999",
    )

    result = await JobAlertInboxIngestor(
        RecordingMailbox((valid, invalid)),
        config(),
        now=lambda: NOW,
    ).ingest()

    assert result.status is JobAlertIngestionStatus.PARTIAL_FAILURE
    assert len(result.leads) == 1
    assert result.messages_examined == 2
    assert result.messages_accepted == 1


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized_and_invalid_config_fails_closed():
    unavailable = await JobAlertInboxIngestor(
        RecordingMailbox(error=RuntimeError("mailbox-secret-host failed")),
        config(),
        now=lambda: NOW,
    ).ingest()
    assert unavailable.status is JobAlertIngestionStatus.PROVIDER_UNAVAILABLE
    assert unavailable.provider_called is True
    assert "mailbox-secret-host" not in repr(unavailable)

    provider = RecordingMailbox()
    invalid = await JobAlertInboxIngestor(
        provider,
        config(max_messages=26),
        now=lambda: NOW,
    ).ingest()
    assert invalid.status is JobAlertIngestionStatus.INVALID_CONFIG
    assert invalid.issues[0].code is JobAlertIssueCode.INVALID_CONFIG
    assert invalid.provider_called is False
    assert provider.calls == []


@pytest.mark.asyncio
async def test_provider_cannot_expand_the_requested_message_bound():
    alerts = tuple(
        message(
            message_id=f"message-{index}",
            text=f"https://www.linkedin.com/jobs/view/{1234500000 + index}",
        )
        for index in range(4)
    )
    result = await JobAlertInboxIngestor(
        RecordingMailbox(alerts),
        config(max_messages=2),
        now=lambda: NOW,
    ).ingest()

    assert len(result.leads) == 2
    assert result.messages_examined == 2
    assert result.status is JobAlertIngestionStatus.PARTIAL_FAILURE
    assert result.issues[0].code is JobAlertIssueCode.PROVIDER_LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_sanitized_alert_drafts_persist_as_subject_scoped_job_leads(
    tmp_path,
):
    alert = message(
        sender="Example Careers <jobs@careers.example.com>",
        html=(
            '<a href="https://boards.greenhouse.io/example/jobs/456?gh_jid=456">'
            "Platform Engineer</a>"
        ),
    )
    provider = RecordingMailbox((alert,))
    ingestor = JobAlertInboxIngestor(
        provider,
        config(allowed_sender_domains=("example.com",)),
        now=lambda: NOW,
    )
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))

    first = await ingest_job_alerts_for_subject(
        subject_id="subject-alert-synthetic",
        ingestor=ingestor,
        repository=repository,
    )
    replay = await ingest_job_alerts_for_subject(
        subject_id="subject-alert-synthetic",
        ingestor=ingestor,
        repository=repository,
    )

    assert first.status is JobAlertPersistenceStatus.SUCCEEDED
    assert first.created == 1
    assert first.duplicates == 0
    assert first.leads[0].source is (
        JobLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL
    )
    assert first.acquisition_sources == (
        JobLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL,
    )
    assert first.leads[0].origin is JobLeadOrigin.ATS
    assert replay.created == 0
    assert replay.duplicates == 1
    assert len(repository.list_current("subject-alert-synthetic").leads) == 1
    assert RECIPIENT not in repr(first)
    assert "message-raw-id" not in repr(first)


@pytest.mark.asyncio
async def test_alert_duplicate_retains_this_run_acquisition_source(tmp_path):
    url = "https://www.linkedin.com/jobs/view/1234567890"
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    existing = JobLead.discover(
        subject_id="subject-alert-synthetic",
        source=JobLeadSource.AUTHORIZED_WEB_SEARCH,
        origin=JobLeadOrigin.LINKEDIN_SEARCH_INDEX,
        source_url=url,
        discovered_at=NOW - timedelta(minutes=20),
        confidence=0.70,
        query_id="web-query-synthetic",
    )
    repository.save(existing)
    ingestor = JobAlertInboxIngestor(
        RecordingMailbox((message(text=url),)),
        config(),
        now=lambda: NOW,
    )

    result = await ingest_job_alerts_for_subject(
        subject_id="subject-alert-synthetic",
        ingestor=ingestor,
        repository=repository,
    )

    assert result.duplicates == 1
    assert result.leads[0].source is JobLeadSource.AUTHORIZED_WEB_SEARCH
    assert result.acquisition_sources == (
        JobLeadSource.LINKEDIN_ALERT_EMAIL,
    )
