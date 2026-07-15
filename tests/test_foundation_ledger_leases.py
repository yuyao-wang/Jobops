from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from core.event_ledger import (
    DuplicateSubmissionError,
    EventLedger,
    StateConflictError,
    SubmissionStateError,
    SubmissionStatus,
    _canonical_url_identity,
    hash_job_url,
)
from core.leases import (
    LeaseManager,
    LeaseOwnershipError,
    LeaseUnavailableError,
)
from core.outcomes import EvidenceKind, EvidenceRef


def test_run_state_uses_compare_and_swap_and_append_only_events(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    run = ledger.create_run(run_id="run-1", job_id="job-1")
    assert run.state_version == 0

    updated = ledger.compare_and_set_state(
        run_id="run-1", expected_version=0, new_state="REVIEW_READY"
    )
    assert updated.state_version == 1
    assert [event.event_type for event in ledger.list_events(run_id="run-1")] == [
        "RUN_CREATED",
        "RUN_STATE_CHANGED",
    ]

    with pytest.raises(StateConflictError):
        ledger.compare_and_set_state(
            run_id="run-1", expected_version=0, new_state="STALE_WRITE"
        )

    with pytest.raises(Exception, match="append-only"):
        with ledger.transaction() as connection:
            connection.execute("UPDATE events SET event_type = 'MUTATED'")


def test_submission_intent_is_idempotent_and_blocks_duplicate_submit(
    tmp_path: Path,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.create_run(run_id="run-1", job_id="job-1")
    arguments = {
        "run_id": "run-1",
        "job_id": "job-1",
        "job_url": "https://example.invalid/jobs/1?utm_source=test",
        "material_hash": "material-v1",
        "answer_hash": "answers-v1",
        "review_hash": "review-v1",
        "policy_hash": "policy-v1",
    }
    intent = ledger.create_submission_intent(**arguments)
    assert ledger.create_submission_intent(**arguments).intent_id == intent.intent_id

    with pytest.raises(DuplicateSubmissionError):
        ledger.create_submission_intent(
            **{**arguments, "material_hash": "different-material"}
        )

    started = ledger.mark_submission_started(intent.intent_id)
    assert started.status is SubmissionStatus.SUBMITTING
    evidence = EvidenceRef(
        kind=EvidenceKind.CONFIRMATION_TEXT,
        sha256="b" * 64,
        metadata={"matched_phrase": "synthetic confirmation"},
    )
    verified = ledger.mark_submission_verified(
        intent_id=intent.intent_id, evidence=evidence
    )
    assert verified.status is SubmissionStatus.VERIFIED
    assert len(ledger.list_submission_evidence(intent.intent_id)) == 1

    ledger.create_run(run_id="run-2", job_id="job-1")
    with pytest.raises(DuplicateSubmissionError):
        ledger.create_submission_intent(
            **{
                **arguments,
                "run_id": "run-2",
                "material_hash": "material-v2",
            }
        )


def test_duplicate_identity_ignores_tracking_and_mutable_job_metadata(
    tmp_path: Path,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.create_run(run_id="run-original", job_id="job-old-title")
    common = {
        "material_hash": "material",
        "answer_hash": "answers",
        "review_hash": "review",
        "policy_hash": "policy",
    }
    ledger.create_submission_intent(
        run_id="run-original",
        job_id="job-old-title",
        job_url="https://www.example.invalid/jobs/42?utm_source=mail&ref=friend",
        **common,
    )

    ledger.create_run(run_id="run-renamed", job_id="job-new-company-and-title")
    with pytest.raises(DuplicateSubmissionError):
        ledger.create_submission_intent(
            run_id="run-renamed",
            job_id="job-new-company-and-title",
            job_url="https://example.invalid/jobs/42?source=career-page",
            **common,
        )


def test_submission_verification_rejects_ineligible_evidence_atomically(
    tmp_path: Path,
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.create_run(run_id="run-ineligible", job_id="job-ineligible")
    intent = ledger.create_submission_intent(
        run_id="run-ineligible",
        job_id="job-ineligible",
        job_url="https://example.invalid/jobs/ineligible",
        material_hash="material",
        answer_hash="answers",
        review_hash="review",
        policy_hash="policy",
    )
    ledger.mark_submission_started(intent.intent_id)

    with pytest.raises(SubmissionStateError, match="ineligible submission evidence"):
        ledger.mark_submission_verified(
            intent_id=intent.intent_id,
            evidence=EvidenceRef(
                kind=EvidenceKind.FORM_SNAPSHOT,
                sha256="c" * 64,
            ),
        )

    assert ledger.get_submission_intent(intent.intent_id).status is SubmissionStatus.SUBMITTING
    assert ledger.list_submission_evidence(intent.intent_id) == []
    assert "SUBMISSION_VERIFIED" not in {
        event.event_type
        for event in ledger.list_events(run_id="run-ineligible")
    }


def test_unknown_intent_lookup_is_read_only_and_privacy_safe(tmp_path: Path) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.create_run(run_id="run-unknown", job_id="job-unknown")
    intent = ledger.create_submission_intent(
        run_id="run-unknown",
        job_id="job-unknown",
        job_url="https://boards.greenhouse.io/acme/jobs/42?utm_source=mail",
        material_hash="private-material-hash",
        answer_hash="private-answer-hash",
        review_hash="private-review-hash",
        policy_hash="private-policy-hash",
    )
    ledger.mark_submission_started(intent.intent_id)
    ledger.mark_submission_unknown(intent.intent_id)

    found = ledger.find_submission_intent_for_url(
        "https://job-boards.greenhouse.io/acme/jobs/42?gh_src=referral"
    )

    assert found is not None
    assert found.intent_id == intent.intent_id
    assert ledger.get_submission_intent(intent.intent_id).status is SubmissionStatus.UNKNOWN
    safe = found.to_safe_dict()
    assert safe["status"] == SubmissionStatus.UNKNOWN.value
    assert "job_url_hash" in safe
    serialized = str(safe)
    assert "boards.greenhouse.io" not in serialized
    assert "private-material-hash" not in serialized
    assert "private-answer-hash" not in serialized
    assert "private-review-hash" not in serialized
    assert "private-policy-hash" not in serialized


@pytest.mark.parametrize(
    ("first", "alias"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/123?utm_source=mail",
            "https://job-boards.greenhouse.io/acme/jobs/123",
        ),
        (
            "https://jobs.lever.co/acme/abc-123?lever-source=mail",
            "https://api.lever.co/v0/postings/acme/abc-123",
        ),
        (
            "https://jobs.ashbyhq.com/acme/abc-123?utm_source=mail",
            "https://jobs.ashbyhq.com/acme/abc-123/application",
        ),
        (
            "https://jobs.jobvite.com/acme/job/JV-123?__jvst=mail",
            "https://apply.jobvite.com/acme/job/JV-123?__jvsd=referral",
        ),
        (
            "https://acme.wd3.myworkdayjobs.com/en-US/External/job/Calgary/Engineer_R123?source=mail",
            "https://acme.wd5.myworkdayjobs.com/Careers/job/Remote/Other-title_R123/apply",
        ),
    ],
)
def test_native_ats_posting_keys_unify_known_aliases(first: str, alias: str) -> None:
    assert hash_job_url(first) == hash_job_url(alias)


@pytest.mark.parametrize(
    ("legacy_url", "current_alias"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/123?utm_source=mail",
            "https://job-boards.greenhouse.io/acme/jobs/123",
        ),
        (
            "https://jobs.lever.co/acme/abc-123?lever-source=mail",
            "https://api.lever.co/v0/postings/acme/abc-123",
        ),
        (
            "https://jobs.ashbyhq.com/acme/abc-123?utm_source=mail",
            "https://jobs.ashbyhq.com/acme/abc-123/application",
        ),
        (
            "https://jobs.jobvite.com/acme/job/JV-123?__jvst=mail",
            "https://apply.jobvite.com/acme/job/JV-123?__jvsd=referral",
        ),
        (
            "https://acme.wd3.myworkdayjobs.com/External/job/Engineer_R123",
            "https://acme.wd5.myworkdayjobs.com/External/job/Engineer_R123/apply",
        ),
    ],
)
def test_pre_native_url_intents_block_known_ats_aliases(
    tmp_path: Path, legacy_url: str, current_alias: str
) -> None:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.create_run(run_id="run-legacy", job_id="job-legacy")
    intent = ledger.create_submission_intent(
        run_id="run-legacy",
        job_id="job-legacy",
        job_url=legacy_url,
        material_hash="material",
        answer_hash="answers",
        review_hash="review",
        policy_hash="policy",
    )
    legacy_hash = hashlib.sha256(
        _canonical_url_identity(legacy_url).encode("utf-8")
    ).hexdigest()
    with ledger.transaction() as connection:
        connection.execute(
            """
            UPDATE submission_intents
            SET application_key = ?, job_url_hash = ?
            WHERE intent_id = ?
            """,
            (legacy_hash, legacy_hash, intent.intent_id),
        )

    found = ledger.find_submission_intent_for_url(
        current_alias,
        statuses=(SubmissionStatus.PENDING,),
    )

    assert found is not None
    assert found.intent_id == intent.intent_id
    ledger.create_run(run_id="run-current", job_id="job-current")
    with pytest.raises(DuplicateSubmissionError):
        ledger.create_submission_intent(
            run_id="run-current",
            job_id="job-current",
            job_url=current_alias,
            material_hash="material-2",
            answer_hash="answers-2",
            review_hash="review-2",
            policy_hash="policy-2",
        )


@pytest.mark.parametrize(
    ("first", "different_tenant"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://boards.greenhouse.io/other/jobs/123",
        ),
        (
            "https://jobs.lever.co/acme/abc-123",
            "https://jobs.lever.co/other/abc-123/apply",
        ),
        (
            "https://jobs.ashbyhq.com/acme/abc-123",
            "https://jobs.ashbyhq.com/other/abc-123/application",
        ),
        (
            "https://jobs.jobvite.com/acme/job/JV-123",
            "https://apply.jobvite.com/other/job/JV-123",
        ),
        (
            "https://acme.wd3.myworkdayjobs.com/External/job/Engineer_R123",
            "https://other.wd3.myworkdayjobs.com/External/job/Engineer_R123",
        ),
    ],
)
def test_native_ats_posting_keys_keep_tenant_scope(
    first: str, different_tenant: str
) -> None:
    assert hash_job_url(first) != hash_job_url(different_tenant)


def test_unknown_custom_hosts_remain_exact_url_scoped() -> None:
    assert hash_job_url("https://careers-a.example/jobs/123") != hash_job_url(
        "https://careers-b.example/jobs/123"
    )


def test_lease_owner_checks_and_expired_takeover(tmp_path: Path) -> None:
    now = [100.0]
    ledger = EventLedger(tmp_path / "events.sqlite3")
    leases = LeaseManager(ledger, clock=lambda: now[0])
    first = leases.acquire("browser:default", owner="worker-a", ttl_seconds=10)

    with pytest.raises(LeaseUnavailableError):
        leases.acquire("browser:default", owner="worker-b", ttl_seconds=10)
    with pytest.raises(LeaseOwnershipError):
        leases.renew(replace(first, owner="worker-b"), ttl_seconds=10)

    now[0] = 111.0
    second = leases.acquire("browser:default", owner="worker-b", ttl_seconds=10)
    assert second.owner == "worker-b"
    assert second.token != first.token
    with pytest.raises(LeaseOwnershipError):
        leases.release(first)

    leases.release(second)
    assert leases.get("browser:default") is None
