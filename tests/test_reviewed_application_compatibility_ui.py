import csv
from datetime import datetime, timedelta, timezone

import pytest

from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.bundles import JobSpec, JobTier
from core.event_ledger import EventLedger
from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    OutcomeStatus,
)
from core.private_home import PrivateHome
from adapters.protocol import REVIEW_BINDING_VERSION
from dashboard.reviewed_application_compatibility import (
    ReviewedApplicationCompatibilityReadStatus,
    ReviewedApplicationCompatibilityReviewStatus,
    ReviewedApplicationCompatibilitySubmissionStatus,
    ReviewedApplicationCompatibilityUIController,
)


NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
SUBJECT = "synthetic-compatibility-subject"
RUN_ID = "run-synthetic-reviewed-123456"
URL = "https://boards.greenhouse.io/example/jobs/123456"


def _context(subject_id: str = SUBJECT) -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="synthetic-session-id-123456",
        subject_id=subject_id,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )


def _home_with_review(
    tmp_path,
    *,
    current_state: OutcomeStatus = OutcomeStatus.REVIEW_READY,
) -> tuple[PrivateHome, str]:
    home = PrivateHome(tmp_path / "private-home")
    paths = home.ensure()
    fields = (
        "company",
        "job_title",
        "job_url",
        "priority",
        "status",
        "resume_variant",
        "location",
    )
    with paths.job_queue.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "company": "Synthetic Systems",
                "job_title": "Backend Engineer",
                "job_url": URL,
                "priority": "Medium",
                "status": (
                    "Needs user"
                    if current_state
                    in {
                        OutcomeStatus.AWAITING_GATE_B,
                        OutcomeStatus.NEEDS_USER,
                        OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
                    }
                    else "Ready for review"
                ),
                "resume_variant": "synthetic-resume.pdf",
                "location": "Alberta",
            }
        )
    job = JobSpec(
        url=URL,
        company="Synthetic Systems",
        title="Backend Engineer",
        tier=JobTier.MEDIUM,
    )
    review = ApplicationOutcome.review_ready(
        run_id=RUN_ID,
        job_id=job.job_id,
        adapter="greenhouse",
        checkpoint="a" * 64,
        details={
            "review": {
                "adapter": "greenhouse",
                "filled_fields": ["first_name", "email", "location"],
                "fingerprint": "a" * 64,
                "ready": True,
                "submit_control_present": True,
                "unresolved_required": [],
                "uploaded_files": [
                    {"field": "resume", "sha256": "b" * 64},
                    {"field": "cover_letter", "sha256": "c" * 64},
                ],
                "validation_errors": [],
            }
        },
    )
    ledger = EventLedger(paths.event_ledger)
    run = ledger.create_run(
        run_id=RUN_ID,
        job_id=job.job_id,
        metadata={
            "answer_hash": "d" * 64,
            "cover_letter_strategy": "narrative",
            "material_hash": "e" * 64,
            "policy_hash": "f" * 64,
            "resume_sha256": "b" * 64,
        },
    )
    ledger.compare_and_set_state(
        run_id=RUN_ID,
        expected_version=run.state_version,
        new_state=current_state.value,
        outcome=review,
    )
    return home, job.job_id


def _controller(home: PrivateHome, submitter, refresher=None):
    return ReviewedApplicationCompatibilityUIController(
        home=home,
        subject_id=SUBJECT,
        submit_reviewed=submitter,
        refresh_reviewed=refresher or submitter,
        headless=True,
        lease_ttl_seconds=1800,
    )


def test_list_projects_current_review_without_candidate_values(tmp_path) -> None:
    home, job_id = _home_with_review(tmp_path)
    controller = _controller(home, lambda _args: None)

    result = controller.list(context=_context())
    public = result.to_dict()

    assert result.status is ReviewedApplicationCompatibilityReadStatus.SUCCEEDED
    assert len(public["items"]) == 1
    item = public["items"][0]
    assert item["review_run_id"] == RUN_ID
    assert item["job_id"] == job_id
    assert item["company"] == "Synthetic Systems"
    assert item["product_status"] == "READY"
    assert item["prepared_answer_count"] == 3
    assert item["unresolved_control_count"] == 0
    assert item["uploaded_file_count"] == 2
    assert "review_token" not in item
    assert SUBJECT not in str(public)


def test_review_load_returns_exact_action_time_token(tmp_path) -> None:
    home, _ = _home_with_review(tmp_path)
    controller = _controller(home, lambda _args: None)

    result = controller.load(context=_context(), run_id=RUN_ID)

    assert result.status is ReviewedApplicationCompatibilityReviewStatus.READY
    assert result.item is not None
    assert result.item.review_fingerprint == "a" * 64
    assert result.review_token is not None
    assert len(result.review_token) == 64


def test_awaiting_gate_b_refresh_remains_visible_as_ready(tmp_path) -> None:
    home, _ = _home_with_review(
        tmp_path, current_state=OutcomeStatus.AWAITING_GATE_B
    )
    controller = _controller(home, lambda _args: None)

    listed = controller.list(context=_context())
    review = controller.load(context=_context(), run_id=RUN_ID)

    assert listed.status is ReviewedApplicationCompatibilityReadStatus.SUCCEEDED
    assert len(listed.items) == 1
    assert listed.items[0].product_status == "READY"
    assert review.status is ReviewedApplicationCompatibilityReviewStatus.READY
    assert review.review_token is not None


@pytest.mark.parametrize(
    "current_state",
    [OutcomeStatus.NEEDS_USER, OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER],
)
def test_pre_submit_validation_stop_keeps_last_review_visible(
    tmp_path, current_state: OutcomeStatus
) -> None:
    home, _ = _home_with_review(tmp_path, current_state=current_state)
    controller = _controller(home, lambda _args: None)

    listed = controller.list(context=_context())

    assert listed.status is ReviewedApplicationCompatibilityReadStatus.SUCCEEDED
    assert len(listed.items) == 1
    assert listed.items[0].product_status == "READY"


@pytest.mark.asyncio
async def test_prepare_current_review_refreshes_without_submit(tmp_path) -> None:
    home, job_id = _home_with_review(
        tmp_path, current_state=OutcomeStatus.AWAITING_GATE_B
    )
    calls = []

    async def refresher(args):
        calls.append(args)
        return ApplicationOutcome.review_ready(
            run_id=RUN_ID,
            job_id=job_id,
            adapter="greenhouse",
            checkpoint="b" * 64,
            details={"review": {"binding_version": REVIEW_BINDING_VERSION}},
        )

    controller = _controller(home, lambda _args: None, refresher)
    result = await controller.prepare_current_review(
        context=_context(), run_id=RUN_ID
    )

    # The injected refresher owns persistence in production. This focused
    # contract proves the UI path requests a non-submit refresh exactly once.
    assert len(calls) == 1
    assert calls[0].run_id == RUN_ID
    assert result.status is ReviewedApplicationCompatibilityReviewStatus.READY


@pytest.mark.asyncio
async def test_confirm_invokes_shared_submitter_once(tmp_path) -> None:
    home, job_id = _home_with_review(tmp_path)
    calls = []

    async def submitter(args):
        calls.append(args)
        return ApplicationOutcome.submitted_verified(
            run_id=RUN_ID,
            job_id=job_id,
            adapter="greenhouse",
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    metadata={"matched": True},
                ),
            ),
        )

    controller = _controller(home, submitter)
    review = controller.load(context=_context(), run_id=RUN_ID)

    result = await controller.submit(
        context=_context(),
        run_id=RUN_ID,
        review_token=review.review_token,
        confirmed=True,
    )

    assert result.status is ReviewedApplicationCompatibilitySubmissionStatus.SUBMITTED
    assert result.retry_allowed is False
    assert len(calls) == 1
    assert calls[0].run_id == RUN_ID
    assert calls[0].approve is True
    assert calls[0].semantic_mapper is False


@pytest.mark.asyncio
async def test_stale_review_token_never_invokes_submitter(tmp_path) -> None:
    home, _ = _home_with_review(tmp_path)
    calls = []
    controller = _controller(home, lambda args: calls.append(args))

    result = await controller.submit(
        context=_context(),
        run_id=RUN_ID,
        review_token="0" * 64,
        confirmed=True,
    )

    assert result.status is ReviewedApplicationCompatibilitySubmissionStatus.STALE_REVIEW
    assert calls == []


@pytest.mark.asyncio
async def test_submit_unknown_is_terminal_and_never_retried(tmp_path) -> None:
    home, job_id = _home_with_review(tmp_path)
    calls = []

    async def submitter(args):
        calls.append(args)
        return ApplicationOutcome.needs_user(
            run_id=RUN_ID,
            job_id=job_id,
            status=OutcomeStatus.SUBMIT_UNKNOWN,
            phase="VERIFY",
            reason_code="SUBMISSION_CONFIRMATION_MISSING",
            message="Synthetic confirmation was unavailable.",
        )

    controller = _controller(home, submitter)
    review = controller.load(context=_context(), run_id=RUN_ID)

    result = await controller.submit(
        context=_context(),
        run_id=RUN_ID,
        review_token=review.review_token,
        confirmed=True,
    )

    assert result.status is ReviewedApplicationCompatibilitySubmissionStatus.SUBMISSION_UNCERTAIN
    assert result.retry_allowed is False
    assert len(calls) == 1


def test_subject_mismatch_cannot_read_private_queue(tmp_path) -> None:
    home, _ = _home_with_review(tmp_path)
    controller = _controller(home, lambda _args: None)

    with pytest.raises(ValueError, match="does not own"):
        controller.list(context=_context("another-synthetic-subject"))
