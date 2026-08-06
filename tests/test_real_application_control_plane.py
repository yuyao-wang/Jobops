from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.bundles import canonical_hash
from core.event_ledger import EventLedger, SubmissionStatus
from core.outcomes import ApplicationOutcome, EvidenceKind, EvidenceRef
from core.permits import PermitService
from core.real_application_control_plane import (
    RealApplicationConflictError,
    RealApplicationControlPlane,
    RealApplicationExecutorStatus,
    RealApplicationNotAuthorizedError,
    RealApplicationPreparation,
    RealApplicationTaskStatus,
)


WORKDAY_URL = (
    "https://synthetic.wd5.myworkdayjobs.com/"
    "en-US/External/job/Synthetic-Role/JR-123"
)


def digest(value: str) -> str:
    return canonical_hash({"value": value})


def preparation() -> RealApplicationPreparation:
    answers = {
        "sections": [
            {
                "key": "contact",
                "label": "Contact information",
                "items": [
                    {
                        "key": "email",
                        "label": "Email",
                        "value": "candidate@example.test",
                        "source": "verified_private_vault",
                        "certainty": "VERIFIED",
                        "status": "READY",
                    }
                ],
            }
        ]
    }
    return RealApplicationPreparation(
        attempt_id="run-real-acceptance-1",
        subject_id="subject-test",
        application_plan_id="application-plan-real-1",
        assembly_record_id="application-bundle-assembly-" + "a" * 64,
        assembly_record_content_hash=digest("assembly"),
        job_id="job-real-1",
        external_job_id="JR-123",
        company="Synthetic Company",
        title="Synthetic Role",
        provider="workday",
        canonical_job_url=WORKDAY_URL,
        bundle_canonical_hash=digest("bundle"),
        profile_snapshot_hash=digest("profile"),
        answer_hash=digest("answers"),
        answer_bundle_hash=canonical_hash(answers),
        material_hash=digest("materials"),
        resume_sha256=digest("resume"),
        cover_letter_sha256=digest("no-cover-letter"),
        policy_hash=digest("policy"),
        answer_bundle=answers,
    )


@pytest.fixture
def control(tmp_path: Path):
    now = [1_800_000_000.0]
    ledger = EventLedger(tmp_path / "ledger.sqlite3")
    permit_service = PermitService(
        secret=b"p" * 32,
        ledger=ledger,
        clock=lambda: now[0],
    )
    plane = RealApplicationControlPlane(
        ledger=ledger,
        permit_service=permit_service,
        subject_id="subject-test",
        enrollment_secret="synthetic-one-time-enrollment",
        clock=lambda: now[0],
    )
    return plane, now


def test_real_application_control_plane_requires_one_time_worker_enrollment(control):
    plane, _now = control

    with pytest.raises(RealApplicationNotAuthorizedError):
        plane.enroll_worker("wrong-synthetic-token")

    enrolled = plane.enroll_worker("synthetic-one-time-enrollment")
    assert plane.authenticate_worker(enrolled.session_secret) == enrolled.worker_id
    assert plane.executor_status() is RealApplicationExecutorStatus.AVAILABLE

    with pytest.raises(RealApplicationConflictError):
        plane.enroll_worker("synthetic-one-time-enrollment")


def test_review_approval_final_fence_and_confirmation_are_one_golden_path(control):
    plane, _now = control
    enrolled = plane.enroll_worker("synthetic-one-time-enrollment")
    worker_id = enrolled.worker_id
    item = preparation()

    assert plane.prepare(worker_id, item) == "CREATED"
    assert plane.prepare(worker_id, item) == "UNCHANGED"
    claimed = plane.claim_next(worker_id)
    assert claimed is not None
    lease_token = claimed.lease.token
    assert claimed.task["status"] == RealApplicationTaskStatus.CLAIMED.value

    review_hash = digest("browser-review")
    plane.report_review(
        worker_id,
        item.attempt_id,
        lease_token,
        review_hash=review_hash,
        review={
            "submit_control_present": True,
            "legal_declarations": ["I certify that the information is accurate."],
            "page_states": ["myInformation", "myExperience", "review"],
            "review_fields": [],
            "unresolved_required": [],
        },
    )
    assert plane.get_task(item.attempt_id)["status"] == "REVIEW_READY"

    plane.approve(
        item.attempt_id,
        reviewed_hash=review_hash,
        external_side_effect_acknowledged=True,
    )
    permit = plane.load_worker_permit(worker_id, item.attempt_id, lease_token)
    assert permit
    assert permit not in str(plane.get_task(item.attempt_id))

    intent = plane.final_fence(
        worker_id,
        item.attempt_id,
        lease_token,
        permit_token=permit,
        current_url=WORKDAY_URL + "/review",
        external_job_id=item.external_job_id,
        bundle_canonical_hash=item.bundle_canonical_hash,
        profile_snapshot_hash=item.profile_snapshot_hash,
        answer_hash=item.answer_hash,
        answer_bundle_hash=item.answer_bundle_hash,
        material_hash=item.material_hash,
        resume_sha256=item.resume_sha256,
        cover_letter_sha256=item.cover_letter_sha256,
        review_hash=review_hash,
        assembly_record_id=item.assembly_record_id,
        assembly_record_content_hash=item.assembly_record_content_hash,
    )
    assert intent.status is SubmissionStatus.SUBMITTING
    assert plane.ledger.get_submission_intent(intent.intent_id).status is SubmissionStatus.SUBMITTING

    evidence = EvidenceRef(
        kind=EvidenceKind.CONFIRMATION_TEXT,
        sha256=digest("confirmation"),
        metadata={"source": "synthetic_workday_confirmation"},
    )
    outcome = ApplicationOutcome.submitted_verified(
        run_id=item.attempt_id,
        job_id=item.job_id,
        adapter="workday",
        evidence_refs=(evidence,),
    )
    status = plane.report_outcome(
        worker_id,
        item.attempt_id,
        lease_token,
        outcome=outcome,
        confirmation_id="SYNTHETIC-CONFIRMATION-123",
        success_url=WORKDAY_URL + "/confirmation",
    )

    assert status is RealApplicationTaskStatus.CONFIRMED
    task = plane.get_task(item.attempt_id)
    assert task["status"] == "CONFIRMED"
    assert task["confirmation_id"] == "SYNTHETIC-CONFIRMATION-123"
    assert plane.ledger.get_submission_intent(intent.intent_id).status is SubmissionStatus.VERIFIED
    assert plane.ledger.get_run(item.attempt_id).state == "SUBMITTED_VERIFIED"


def test_changed_file_or_page_identity_closes_final_fence_without_intent(control):
    plane, _now = control
    enrolled = plane.enroll_worker("synthetic-one-time-enrollment")
    item = preparation()
    plane.prepare(enrolled.worker_id, item)
    claimed = plane.claim_next(enrolled.worker_id)
    assert claimed is not None
    review_hash = digest("browser-review")
    plane.report_review(
        enrolled.worker_id,
        item.attempt_id,
        claimed.lease.token,
        review_hash=review_hash,
        review={
            "submit_control_present": True,
            "legal_declarations": [],
            "review_fields": [],
            "unresolved_required": [],
        },
    )
    plane.approve(
        item.attempt_id,
        reviewed_hash=review_hash,
        external_side_effect_acknowledged=True,
    )
    permit = plane.load_worker_permit(
        enrolled.worker_id, item.attempt_id, claimed.lease.token
    )

    with pytest.raises(RealApplicationNotAuthorizedError):
        plane.final_fence(
            enrolled.worker_id,
            item.attempt_id,
            claimed.lease.token,
            permit_token=permit,
            current_url=WORKDAY_URL + "/review",
            external_job_id=item.external_job_id,
            bundle_canonical_hash=item.bundle_canonical_hash,
            profile_snapshot_hash=item.profile_snapshot_hash,
            answer_hash=item.answer_hash,
            answer_bundle_hash=item.answer_bundle_hash,
            material_hash=item.material_hash,
            resume_sha256=digest("changed-resume"),
            cover_letter_sha256=item.cover_letter_sha256,
            review_hash=review_hash,
            assembly_record_id=item.assembly_record_id,
            assembly_record_content_hash=item.assembly_record_content_hash,
        )

    assert plane.ledger.find_submission_intent_for_url(
        item.canonical_job_url,
        statuses=(SubmissionStatus.SUBMITTING,),
    ) is None
    assert plane.get_task(item.attempt_id)["status"] == "APPROVED"


def test_subject_provider_external_job_id_is_the_duplicate_submit_key(control):
    plane, _now = control
    enrolled = plane.enroll_worker("synthetic-one-time-enrollment")
    first = preparation()
    plane.prepare(enrolled.worker_id, first)
    claimed = plane.claim_next(enrolled.worker_id)
    assert claimed is not None
    first_review = digest("first-review")
    plane.report_review(
        enrolled.worker_id,
        first.attempt_id,
        claimed.lease.token,
        review_hash=first_review,
        review={
            "submit_control_present": True,
            "legal_declarations": [],
            "review_fields": [],
            "unresolved_required": [],
        },
    )
    plane.approve(
        first.attempt_id,
        reviewed_hash=first_review,
        external_side_effect_acknowledged=True,
    )
    first_permit = plane.load_worker_permit(
        enrolled.worker_id, first.attempt_id, claimed.lease.token
    )
    plane.final_fence(
        enrolled.worker_id,
        first.attempt_id,
        claimed.lease.token,
        permit_token=first_permit,
        current_url=WORKDAY_URL + "/review",
        external_job_id=first.external_job_id,
        bundle_canonical_hash=first.bundle_canonical_hash,
        profile_snapshot_hash=first.profile_snapshot_hash,
        answer_hash=first.answer_hash,
        answer_bundle_hash=first.answer_bundle_hash,
        material_hash=first.material_hash,
        resume_sha256=first.resume_sha256,
        cover_letter_sha256=first.cover_letter_sha256,
        review_hash=first_review,
        assembly_record_id=first.assembly_record_id,
        assembly_record_content_hash=first.assembly_record_content_hash,
    )

    second = replace(
        first,
        attempt_id="run-real-acceptance-2",
        application_plan_id="application-plan-real-2",
        assembly_record_id="application-bundle-assembly-" + "b" * 64,
        assembly_record_content_hash=digest("assembly-2"),
        job_id="job-real-2",
        canonical_job_url=(
            "https://another.wd3.myworkdayjobs.com/"
            "en-US/External/job/Other-Title/JR-123"
        ),
        bundle_canonical_hash=digest("bundle-2"),
    )
    plane.prepare(enrolled.worker_id, second)
    claimed_second = plane.claim_next(enrolled.worker_id)
    assert claimed_second is not None
    second_review = digest("second-review")
    plane.report_review(
        enrolled.worker_id,
        second.attempt_id,
        claimed_second.lease.token,
        review_hash=second_review,
        review={
            "submit_control_present": True,
            "legal_declarations": [],
            "review_fields": [],
            "unresolved_required": [],
        },
    )
    plane.approve(
        second.attempt_id,
        reviewed_hash=second_review,
        external_side_effect_acknowledged=True,
    )
    second_permit = plane.load_worker_permit(
        enrolled.worker_id,
        second.attempt_id,
        claimed_second.lease.token,
    )
    with pytest.raises(RealApplicationNotAuthorizedError):
        plane.final_fence(
            enrolled.worker_id,
            second.attempt_id,
            claimed_second.lease.token,
            permit_token=second_permit,
            current_url=second.canonical_job_url + "/review",
            external_job_id=second.external_job_id,
            bundle_canonical_hash=second.bundle_canonical_hash,
            profile_snapshot_hash=second.profile_snapshot_hash,
            answer_hash=second.answer_hash,
            answer_bundle_hash=second.answer_bundle_hash,
            material_hash=second.material_hash,
            resume_sha256=second.resume_sha256,
            cover_letter_sha256=second.cover_letter_sha256,
            review_hash=second_review,
            assembly_record_id=second.assembly_record_id,
            assembly_record_content_hash=second.assembly_record_content_hash,
        )
