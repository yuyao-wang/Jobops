"""Focused P2c1d1 verified application execution profile tests."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY,
    ApplicationExecutionIdentityFieldKey,
)
from core.application_plan import ApplicationPlan, PrivateHomeApplicationPlanRepository
from core.candidate_identity_facts import (
    CandidateIdentityFactSourceKind,
    CandidateIdentityFactSourceRef,
    CandidateIdentityFactVerificationStatus,
    PrivateHomeCandidateIdentityFactRepository,
    WriteCandidateIdentityFactCommand,
    WriteCandidateIdentityFactStatus,
)
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from core.verified_application_execution_profile import (
    PrivateHomeVerifiedApplicationExecutionProfileRepository,
    ProjectVerifiedApplicationExecutionProfileCommand,
    ProjectVerifiedApplicationExecutionProfileStatus,
    VerifiedApplicationExecutionProfileReadStatus,
    project_verified_application_execution_profile,
    to_application_bundle_profile,
)


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
SUBJECT = "subject-profile-synthetic"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plan(
    home: PrivateHome, *, subject_id: str = SUBJECT
) -> tuple[ApplicationPlan, PrivateHomeApplicationPlanRepository]:
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id="job-profile-synthetic",
        job_revision=1,
        job_content_hash=_hash("job"),
        priority_decision_id="priority-decision-synthetic",
        policy_id="priority-policy-synthetic",
        policy_version=1,
        policy_content_hash=_hash("policy"),
        accepted_job_intent_id="accepted-intent-synthetic",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
    )
    repository = PrivateHomeApplicationPlanRepository(home)
    assert repository.save(plan).plan == plan
    return plan, repository


def _source(
    field_key: ApplicationExecutionIdentityFieldKey,
    *,
    subject_id: str = SUBJECT,
    suffix: str = "one",
) -> CandidateIdentityFactSourceRef:
    return CandidateIdentityFactSourceRef(
        source_kind=CandidateIdentityFactSourceKind.USER_CONFIRMATION,
        source_id=f"confirmation-{field_key.value}-{suffix}",
        source_version="v1",
        source_hash=_hash(f"{field_key.value}-{suffix}"),
        source_locator=f"review:{field_key.value}",
        source_subject_id=subject_id,
    )


def _write(
    repository: PrivateHomeCandidateIdentityFactRepository,
    field_key: ApplicationExecutionIdentityFieldKey,
    value: str,
    *,
    suffix: str = "one",
    expected: str | None = None,
):
    result = repository.write(
        WriteCandidateIdentityFactCommand(
            subject_id=SUBJECT,
            field_key=field_key,
            submitted_value=value,
            verification_status=(
                CandidateIdentityFactVerificationStatus.USER_CONFIRMED
            ),
            source_ref=_source(field_key, suffix=suffix),
            expected_current_fact_id=expected,
            invocation_id=f"fact-{field_key.value}-{suffix}",
            now=NOW,
        )
    )
    assert result.status in {
        WriteCandidateIdentityFactStatus.CREATED,
        WriteCandidateIdentityFactStatus.UNCHANGED,
    }
    assert result.fact is not None
    return result.fact


def _project(
    *,
    plan: ApplicationPlan,
    plans: PrivateHomeApplicationPlanRepository,
    facts: PrivateHomeCandidateIdentityFactRepository,
    profiles: PrivateHomeVerifiedApplicationExecutionProfileRepository,
    invocation: str,
):
    return project_verified_application_execution_profile(
        ProjectVerifiedApplicationExecutionProfileCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            invocation_id=invocation,
            now=NOW,
        ),
        plan_repository=plans,
        fact_repository=facts,
        repository=profiles,
    )


def test_verified_snapshot_has_exact_per_field_lineage_and_bundle_projection(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home)
    facts = PrivateHomeCandidateIdentityFactRepository(home)
    first = _write(
        facts, ApplicationExecutionIdentityFieldKey.FIRST_NAME, "Synthetic"
    )
    last = _write(
        facts, ApplicationExecutionIdentityFieldKey.LAST_NAME, "Candidate"
    )
    email = _write(
        facts,
        ApplicationExecutionIdentityFieldKey.EMAIL,
        "Synthetic.Candidate@EXAMPLE.TEST",
    )
    phone = _write(
        facts, ApplicationExecutionIdentityFieldKey.PHONE, "+1 555 010 2026"
    )
    profiles = PrivateHomeVerifiedApplicationExecutionProfileRepository(home)

    result = _project(
        plan=plan,
        plans=plans,
        facts=facts,
        profiles=profiles,
        invocation="profile-project-one",
    )

    assert result.status is ProjectVerifiedApplicationExecutionProfileStatus.CREATED
    assert result.snapshot is not None
    snapshot = result.snapshot
    assert snapshot.subject_id == SUBJECT
    assert snapshot.application_plan_id == plan.plan_id
    assert snapshot.job_id == plan.job_id
    assert tuple(item.source_fact_id for item in snapshot.ordered_fields) == (
        first.fact_id,
        last.fact_id,
        email.fact_id,
        phone.fact_id,
    )
    assert snapshot.source_fact_bindings[-1] == (
        phone.fact_id,
        phone.field_version,
        phone.content_hash,
    )
    assert all(
        item.normalization_policy_version
        == APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY[
            item.field_key
        ].normalization_policy_version
        for item in snapshot.ordered_fields
    )
    assert to_application_bundle_profile(snapshot) == {
        "personal": {
            "first_name": "Synthetic",
            "last_name": "Candidate",
            "email": "Synthetic.Candidate@example.test",
            "phone": "+1 555 010 2026",
        }
    }
    assert "Synthetic.Candidate@example.test" not in repr(result)


def test_missing_unverified_cross_subject_and_drift_fail_closed(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home)
    facts = PrivateHomeCandidateIdentityFactRepository(home)
    _write(facts, ApplicationExecutionIdentityFieldKey.FIRST_NAME, "Synthetic")
    _write(facts, ApplicationExecutionIdentityFieldKey.LAST_NAME, "Candidate")
    profiles = PrivateHomeVerifiedApplicationExecutionProfileRepository(home)

    missing = _project(
        plan=plan,
        plans=plans,
        facts=facts,
        profiles=profiles,
        invocation="profile-missing",
    )
    assert missing.status is ProjectVerifiedApplicationExecutionProfileStatus.NOT_READY
    assert missing.snapshot is None
    assert missing.missing_field_keys == (
        ApplicationExecutionIdentityFieldKey.EMAIL,
    )

    wrong_subject = project_verified_application_execution_profile(
        ProjectVerifiedApplicationExecutionProfileCommand(
            subject_id="subject-profile-other",
            application_plan_id=plan.plan_id,
            invocation_id="profile-cross-subject",
            now=NOW,
        ),
        plan_repository=plans,
        fact_repository=facts,
        repository=profiles,
    )
    assert (
        wrong_subject.status
        is ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE
    )
    assert wrong_subject.snapshot is None

    _write(
        facts,
        ApplicationExecutionIdentityFieldKey.EMAIL,
        "synthetic@example.test",
    )
    with sqlite3.connect(facts.path) as connection:
        connection.execute(
            """
            UPDATE current_heads SET current_fact_hash = ?
            WHERE subject_id = ? AND field_key = ?
            """,
            (
                "0" * 64,
                SUBJECT,
                ApplicationExecutionIdentityFieldKey.EMAIL.value,
            ),
        )
    drift = _project(
        plan=plan,
        plans=plans,
        facts=facts,
        profiles=profiles,
        invocation="profile-drift",
    )
    assert drift.status is ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE
    assert drift.snapshot is None


def test_replay_is_unchanged_and_new_current_fact_creates_new_snapshot(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home)
    facts = PrivateHomeCandidateIdentityFactRepository(home)
    _write(facts, ApplicationExecutionIdentityFieldKey.FIRST_NAME, "Synthetic")
    _write(facts, ApplicationExecutionIdentityFieldKey.LAST_NAME, "Candidate")
    email = _write(
        facts,
        ApplicationExecutionIdentityFieldKey.EMAIL,
        "first@example.test",
    )
    profiles = PrivateHomeVerifiedApplicationExecutionProfileRepository(home)
    created = _project(
        plan=plan,
        plans=plans,
        facts=facts,
        profiles=profiles,
        invocation="profile-created",
    )
    replay = _project(
        plan=plan,
        plans=plans,
        facts=facts,
        profiles=profiles,
        invocation="profile-replay",
    )
    assert created.status is ProjectVerifiedApplicationExecutionProfileStatus.CREATED
    assert replay.status is ProjectVerifiedApplicationExecutionProfileStatus.UNCHANGED
    assert replay.snapshot == created.snapshot

    updated = _write(
        facts,
        ApplicationExecutionIdentityFieldKey.EMAIL,
        "second@example.test",
        suffix="two",
        expected=email.fact_id,
    )
    conflicting_invocation = _project(
        plan=plan,
        plans=plans,
        facts=facts,
        profiles=profiles,
        invocation="profile-created",
    )
    assert (
        conflicting_invocation.status
        is ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE
    )
    assert conflicting_invocation.failure_code == "INVOCATION_PAYLOAD_MISMATCH"
    changed = _project(
        plan=plan,
        plans=plans,
        facts=facts,
        profiles=profiles,
        invocation="profile-changed",
    )
    assert changed.status is ProjectVerifiedApplicationExecutionProfileStatus.CREATED
    assert changed.snapshot is not None and created.snapshot is not None
    assert changed.snapshot.profile_snapshot_id != created.snapshot.profile_snapshot_id
    assert changed.snapshot.source_fact_bindings[-1][0] == updated.fact_id
    historical = profiles.get(SUBJECT, created.snapshot.profile_snapshot_id)
    assert historical.status is VerifiedApplicationExecutionProfileReadStatus.FOUND
    assert historical.snapshot == created.snapshot


def test_identity_excludes_time_and_records_are_immutable_and_subject_isolated(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home)
    facts = PrivateHomeCandidateIdentityFactRepository(home)
    _write(facts, ApplicationExecutionIdentityFieldKey.FIRST_NAME, "Synthetic")
    _write(facts, ApplicationExecutionIdentityFieldKey.LAST_NAME, "Candidate")
    _write(
        facts,
        ApplicationExecutionIdentityFieldKey.EMAIL,
        "synthetic@example.test",
    )
    profiles = PrivateHomeVerifiedApplicationExecutionProfileRepository(home)
    created = _project(
        plan=plan,
        plans=plans,
        facts=facts,
        profiles=profiles,
        invocation="profile-time-one",
    )
    assert created.snapshot is not None
    later = project_verified_application_execution_profile(
        replace(
            ProjectVerifiedApplicationExecutionProfileCommand(
                subject_id=SUBJECT,
                application_plan_id=plan.plan_id,
                invocation_id="profile-time-two",
                now=NOW,
            ),
            now=NOW + timedelta(days=1),
        ),
        plan_repository=plans,
        fact_repository=facts,
        repository=profiles,
    )
    assert later.status is ProjectVerifiedApplicationExecutionProfileStatus.UNCHANGED
    assert later.snapshot == created.snapshot
    with pytest.raises(FrozenInstanceError):
        created.snapshot.job_id = "changed"  # type: ignore[misc]
    isolated = profiles.get(
        "subject-profile-other", created.snapshot.profile_snapshot_id
    )
    assert (
        isolated.status
        is VerifiedApplicationExecutionProfileReadStatus.INTEGRITY_FAILURE
    )
    assert isolated.snapshot is None
    assert str(home.root.resolve()) not in repr(isolated)
