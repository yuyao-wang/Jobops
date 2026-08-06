from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from core.job_leads import (
    JobLead,
    JobLeadListStatus,
    JobLeadOrigin,
    JobLeadReadStatus,
    JobLeadSource,
    JobLeadStatus,
    JobLeadWriteStatus,
    PrivateHomeJobLeadRepository,
)
from core.private_home import PRIVATE_FILE_MODE, PrivateHome


NOW = datetime(2026, 8, 4, 18, 30, tzinfo=timezone.utc)
SUBJECT = "subject:synthetic-job-leads"


def _web_lead(**overrides: object) -> JobLead:
    values: dict[str, object] = {
        "subject_id": SUBJECT,
        "source": JobLeadSource.AUTHORIZED_WEB_SEARCH,
        "origin": JobLeadOrigin.LINKEDIN_SEARCH_INDEX,
        "source_url": "https://www.linkedin.com/jobs/view/123?trk=search#details",
        "title_hint": "Machine Learning Engineer",
        "company_hint": "Example Robotics",
        "location_hint": "Vancouver, BC",
        "snippet_hint": "Synthetic result excerpt that is not a verified job fact.",
        "discovered_at": NOW,
        "query_id": "web-query-001",
        "confidence": 0.72,
    }
    values.update(overrides)
    return JobLead.discover(**values)  # type: ignore[arg-type]


def test_job_lead_replay_is_stable_and_repr_redacts_source_evidence() -> None:
    first = _web_lead()
    replay = _web_lead()

    assert replay == first
    assert first.source_url == "https://www.linkedin.com/jobs/view/123"
    assert len(first.content_hash) == 64
    assert first.to_dict()["snippet_hint"].startswith("Synthetic result")

    rendered = repr(first)
    assert "Synthetic result excerpt" not in rendered
    assert "linkedin.com/jobs/view" not in rendered

    message_digest = hashlib.sha256(b"synthetic-alert-message").hexdigest()
    email = JobLead.discover(
        subject_id=SUBJECT,
        source=JobLeadSource.LINKEDIN_ALERT_EMAIL,
        origin=JobLeadOrigin.LINKEDIN_SEARCH_INDEX,
        source_url="https://www.linkedin.com/jobs/view/999",
        title_hint="Data Engineer",
        discovered_at=NOW,
        confidence=0.8,
        source_message_digest=message_digest,
    )
    assert message_digest not in repr(email)


@pytest.mark.parametrize(
    ("source_url", "expected"),
    (
        (
            "https://ca.indeed.com./viewjob?utm_source=alert&jk=synthetic-123&session_token=do-not-store",
            "https://www.indeed.com/viewjob?jk=synthetic-123",
        ),
        (
            "https://jobs.linkedin.com./jobs/view/synthetic-role-1234567890?trk=search&authToken=do-not-store",
            "https://www.linkedin.com/jobs/view/1234567890",
        ),
        (
            "https://jobs.glassdoor.ca./job-listing/synthetic.htm?jl=987654&utm_campaign=mail&cookie=do-not-store",
            "https://www.glassdoor.ca/job-listing/synthetic.htm?jl=987654",
        ),
        (
            "https://careers.example.test/jobs/opening?utm_source=mail&id=job-123&session=do-not-store",
            "https://careers.example.test/jobs/opening?id=job-123",
        ),
        (
            "https://example.wd5.myworkdayjobs.com/jobs?jobReqId=R-123&utm_source=mail&token=do-not-store",
            "https://example.wd5.myworkdayjobs.com/jobs?jobReqId=R-123",
        ),
        (
            "https://career5.successfactors.eu/career?company=example"
            "&career_ns=JOB_LISTING&career_job_req_id=R-456"
            "&rcm_site_locale=en_US&utm_source=mail&token=do-not-store",
            "https://career5.successfactors.eu/career?company=example"
            "&career_job_req_id=R-456&career_ns=job_listing",
        ),
    ),
)
def test_job_lead_url_keeps_only_stable_job_identity_query(
    source_url: str,
    expected: str,
) -> None:
    lead = _web_lead(source_url=source_url)

    assert lead.source_url == expected
    assert "do-not-store" not in repr(lead)


def test_job_lead_repository_never_persists_url_secrets_or_tracking(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    repository = PrivateHomeJobLeadRepository(home)
    lead = _web_lead(
        source_url=(
            "https://ca.indeed.com/viewjob?jk=synthetic-123"
            "&authToken=synthetic-secret&session_token=synthetic-session"
            "&cookie=synthetic-cookie&utm_source=alert"
        )
    )

    assert repository.save(lead).status is JobLeadWriteStatus.CREATED
    record = next(
        (home.root / "state" / "discovery" / "job-leads").rglob("*.json")
    )
    persisted = record.read_text(encoding="utf-8")

    assert lead.source_url == "https://www.indeed.com/viewjob?jk=synthetic-123"
    assert "synthetic-123" in persisted
    assert "synthetic-secret" not in persisted
    assert "synthetic-session" not in persisted
    assert "synthetic-cookie" not in persisted
    assert "utm_source" not in persisted
    assert "authToken" not in repr(lead)


def test_job_lead_enforces_source_specific_evidence_and_strict_urls() -> None:
    with pytest.raises(ValueError, match="query_id"):
        _web_lead(query_id=None)
    with pytest.raises(ValueError, match="source message digest"):
        JobLead.discover(
            subject_id=SUBJECT,
            source=JobLeadSource.INDEED_ALERT_EMAIL,
            origin=JobLeadOrigin.INDEED_SEARCH_INDEX,
            source_url="https://ca.indeed.com/viewjob?jk=synthetic",
            discovered_at=NOW,
            confidence=0.7,
        )
    with pytest.raises(ValueError, match="HTTPS"):
        _web_lead(source_url="http://example.test/jobs/123")
    with pytest.raises(ValueError, match="credentials"):
        _web_lead(source_url="https://user:password@example.test/jobs/123")
    with pytest.raises(ValueError, match="between zero and one"):
        _web_lead(confidence=1.1)

    local_fixture = _web_lead(source_url="http://127.0.0.1:8000/jobs/123")
    assert local_fixture.source_url.startswith("http://127.0.0.1:8000/")

    for operational_source in (
        JobLeadSource.CANONICAL_RESOLUTION,
        JobLeadSource.JOB_ALERT_INBOX,
    ):
        with pytest.raises(ValueError, match="refresh-only"):
            _web_lead(source=operational_source)


@pytest.mark.parametrize(
    ("source", "origin", "source_url"),
    (
        (
            JobLeadSource.LINKEDIN_ALERT_EMAIL,
            JobLeadOrigin.ATS,
            "https://jobs.ashbyhq.com/example/synthetic-posting",
        ),
        (
            JobLeadSource.INDEED_ALERT_EMAIL,
            JobLeadOrigin.EMPLOYER,
            "https://careers.example.test/jobs/synthetic-posting",
        ),
    ),
)
def test_alert_source_and_link_origin_are_orthogonal_and_persistable(
    tmp_path,
    source: JobLeadSource,
    origin: JobLeadOrigin,
    source_url: str,
) -> None:
    repository = PrivateHomeJobLeadRepository(
        PrivateHome(tmp_path / "private-home")
    )
    lead = JobLead.discover(
        subject_id=SUBJECT,
        source=source,
        origin=origin,
        source_url=source_url,
        discovered_at=NOW,
        confidence=0.8,
        source_message_digest=hashlib.sha256(
            f"synthetic-alert:{source.value}".encode("utf-8")
        ).hexdigest(),
    )

    written = repository.save(lead)

    assert written.status is JobLeadWriteStatus.CREATED
    assert written.lead is not None
    assert written.lead.source is source
    assert written.lead.origin is origin


def test_job_lead_status_transitions_are_immutable_and_fenced() -> None:
    discovered = _web_lead()
    needs_user = discovered.transition(
        JobLeadStatus.NEEDS_USER,
        now=NOW + timedelta(minutes=1),
        reason="No verified employer or ATS posting was found.",
    )
    resolved = needs_user.transition(
        JobLeadStatus.RESOLVED,
        now=NOW + timedelta(minutes=2),
        canonical_url="https://jobs.example.test/openings/ml-engineer#apply",
        reason="Verified against the public employer posting.",
    )

    assert discovered.status is JobLeadStatus.DISCOVERED
    assert needs_user.lead_version == 2
    assert needs_user.previous_content_hash == discovered.content_hash
    assert resolved.lead_version == 3
    assert resolved.previous_content_hash == needs_user.content_hash
    assert resolved.canonical_url == (
        "https://jobs.example.test/openings/ml-engineer"
    )
    assert resolved.is_direct_successor_of(needs_user)

    with pytest.raises(ValueError, match="invalid JobLead transition"):
        resolved.transition(
            JobLeadStatus.NEEDS_USER,
            now=NOW + timedelta(minutes=3),
            reason="Cannot move a verified lead backwards.",
        )
    with pytest.raises(ValueError, match="requires canonical_url"):
        discovered.transition(
            JobLeadStatus.RESOLVED,
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="requires a reason"):
        discovered.transition(
            JobLeadStatus.STALE,
            now=NOW + timedelta(minutes=1),
        )


def test_private_home_repository_replays_and_restores_version_history(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    repository = PrivateHomeJobLeadRepository(home)
    discovered = _web_lead()

    created = repository.save(discovered)
    replayed = repository.save(discovered)
    resolved = discovered.transition(
        JobLeadStatus.RESOLVED,
        now=NOW + timedelta(minutes=1),
        canonical_url="https://careers.example.test/jobs/123",
    )
    transitioned = repository.save(resolved)

    assert created.status is JobLeadWriteStatus.CREATED
    assert replayed.status is JobLeadWriteStatus.UNCHANGED
    assert transitioned.status is JobLeadWriteStatus.CREATED

    restarted = PrivateHomeJobLeadRepository(home)
    read = restarted.get(SUBJECT, discovered.lead_id)
    listed = restarted.list_current(SUBJECT)
    assert read.status is JobLeadReadStatus.FOUND
    assert read.lead == resolved
    assert listed.status is JobLeadListStatus.SUCCEEDED
    assert listed.leads == (resolved,)

    subject_key = hashlib.sha256(SUBJECT.encode("utf-8")).hexdigest()
    record_directory = (
        home.root
        / "state"
        / "discovery"
        / "job-leads"
        / subject_key
        / discovered.lead_id
    )
    assert sorted(path.name for path in record_directory.iterdir()) == [
        "v00000001.json",
        "v00000002.json",
    ]
    assert all(
        path.stat().st_mode & 0o777 == PRIVATE_FILE_MODE
        for path in record_directory.iterdir()
    )


def test_repository_reports_integrity_failure_without_exposing_tampered_data(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    repository = PrivateHomeJobLeadRepository(home)
    lead = _web_lead()
    assert repository.save(lead).status is JobLeadWriteStatus.CREATED

    record = next((home.root / "state" / "discovery" / "job-leads").rglob("*.json"))
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["snippet_hint"] = "Tampered result text"
    record.write_text(json.dumps(payload), encoding="utf-8")

    read = repository.get(SUBJECT, lead.lead_id)
    listed = repository.list_current(SUBJECT)
    replay = repository.save(lead)
    assert read.status is JobLeadReadStatus.INTEGRITY_FAILURE
    assert read.lead is None
    assert listed.status is JobLeadListStatus.INTEGRITY_FAILURE
    assert listed.leads == ()
    assert replay.status is JobLeadWriteStatus.INTEGRITY_FAILURE
    assert replay.lead is None


def test_repository_is_subject_scoped_and_rejects_version_gaps(tmp_path) -> None:
    home = PrivateHome(tmp_path / "private-home")
    repository = PrivateHomeJobLeadRepository(home)
    lead = _web_lead()
    resolved = lead.transition(
        JobLeadStatus.RESOLVED,
        now=NOW + timedelta(minutes=1),
        canonical_url="https://jobs.example.test/jobs/123",
    )

    assert repository.save(resolved).status is JobLeadWriteStatus.CONFLICT
    assert repository.save(lead).status is JobLeadWriteStatus.CREATED

    other_subject = "subject:synthetic-other"
    cross_read = repository.get(other_subject, lead.lead_id)
    cross_list = repository.list_current(other_subject)
    assert cross_read.status is JobLeadReadStatus.NOT_FOUND
    assert cross_read.lead is None
    assert cross_list.status is JobLeadListStatus.SUCCEEDED
    assert cross_list.leads == ()
