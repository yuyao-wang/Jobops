"""Focused I2c compatibility tests for accepted-intent provenance."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.accepted_job_intent import (
    ACCEPTED_JOB_INTENT_CONTRACT_VERSION,
    ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION,
    AcceptedJobIntent,
    AcceptedJobIntentReadStatus,
    AcceptedJobIntentSourceProvenance,
    AcceptedJobIntentSourceType,
    AcceptedJobIntentWriteStatus,
    PrivateHomeAcceptedJobIntentRepository,
)
from core.job_discovery import JobIntakeIntent
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)
V1_ID = (
    "accepted-job-intent-"
    "576b7a76c0f0cf9d3509e0b190228c30050f93f209c41d2adba2d6b991c27434"
)
V1_BYTES = (
    "{\n"
    f'  "accepted_job_intent_id": "{V1_ID}",\n'
    '  "contract_version": "accepted-job-intent-v1",\n'
    '  "discovery_run_id": "discovery-run-v1",\n'
    '  "intake_proposal_id": "proposal-v1",\n'
    '  "intent": "REQUEST_APPLICATION",\n'
    '  "job_id": "job-v1",\n'
    '  "recorded_at": "2026-07-27T22:00:00Z",\n'
    '  "subject_id": "subject-v1"\n'
    "}\n"
).encode("utf-8")
V1_BYTES_SHA256 = (
    "835dae666ff02ca0052b42502a99594cd"
    "926dd14fc90b145b85922cd2e247f9f"
)


def _v2(
    *,
    intent: JobIntakeIntent,
    proposal_id: str,
    run_id: str,
    provenance: AcceptedJobIntentSourceProvenance,
    recorded_at: datetime = NOW,
) -> AcceptedJobIntent:
    return AcceptedJobIntent.create(
        subject_id="subject-v1",
        job_id="job-v1",
        intent=intent,
        intake_proposal_id=proposal_id,
        discovery_run_id=run_id,
        recorded_at=recorded_at,
        provenance=provenance,
    )


def test_fixed_v1_bytes_identity_and_precedence_remain_unchanged(
    tmp_path: Path,
) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    path = (
        repository._directory(subject_id="subject-v1", job_id="job-v1")
        / f"{V1_ID}.json"
    )
    repository._home.ensure()
    repository._home.write_bytes_if_absent(path, V1_BYTES)

    read = repository.get_current(subject_id="subject-v1", job_id="job-v1")

    assert read.status is AcceptedJobIntentReadStatus.FOUND
    assert read.intent is not None
    assert read.intent.accepted_job_intent_id == V1_ID
    assert read.intent.contract_version == ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION
    assert read.intent.provenance is None
    assert repository.save(read.intent).status is AcceptedJobIntentWriteStatus.UNCHANGED
    assert path.read_bytes() == V1_BYTES
    assert hashlib.sha256(path.read_bytes()).hexdigest() == V1_BYTES_SHA256


def test_v2_conversational_and_search_sources_round_trip_and_bind_identity(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    repository = PrivateHomeAcceptedJobIntentRepository(home)
    conversational = _v2(
        intent=JobIntakeIntent.ADD_JOB,
        proposal_id="proposal-conversation",
        run_id="run-conversation",
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType.CONVERSATIONAL_INTAKE,
            source_id="proposal-conversation",
        ),
    )
    search_source = AcceptedJobIntentSourceProvenance(
        source_type=AcceptedJobIntentSourceType.SEARCH_PROFILE_REFRESH,
        source_id="refresh-1",
        source_version="profile-version-4",
        source_profile_ids=("profile-b", "profile-a", "profile-b"),
    )
    search = _v2(
        intent=JobIntakeIntent.REQUEST_APPLICATION,
        proposal_id="proposal-search",
        run_id="run-search",
        provenance=search_source,
        recorded_at=NOW + timedelta(minutes=1),
    )
    changed_source = _v2(
        intent=JobIntakeIntent.REQUEST_APPLICATION,
        proposal_id="proposal-search",
        run_id="run-search",
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType.SEARCH_PROFILE_REFRESH,
            source_id="refresh-1",
            source_version="profile-version-4",
            source_profile_ids=("profile-a",),
        ),
        recorded_at=NOW + timedelta(minutes=1),
    )

    assert (
        repository.save(conversational).status
        is AcceptedJobIntentWriteStatus.CREATED
    )
    assert repository.save(search).status is AcceptedJobIntentWriteStatus.CREATED
    restarted = PrivateHomeAcceptedJobIntentRepository(home).get_current(
        subject_id="subject-v1",
        job_id="job-v1",
    )

    assert conversational.contract_version == ACCEPTED_JOB_INTENT_CONTRACT_VERSION
    assert search.provenance == search_source
    assert search_source.source_profile_ids == ("profile-a", "profile-b")
    assert search.accepted_job_intent_id != changed_source.accepted_job_intent_id
    assert restarted.intent == search


def test_provenance_keeps_request_precedence_replay_and_conflict_semantics(
    tmp_path: Path,
) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    request = _v2(
        intent=JobIntakeIntent.REQUEST_APPLICATION,
        proposal_id="proposal-request",
        run_id="run-request",
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType.SEARCH_PROFILE_REFRESH,
            source_id="refresh-2",
            source_profile_ids=("profile-auto",),
        ),
    )
    later_add = _v2(
        intent=JobIntakeIntent.ADD_JOB,
        proposal_id="proposal-add",
        run_id="run-add",
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType.CONVERSATIONAL_INTAKE,
            source_id="proposal-add",
        ),
        recorded_at=NOW + timedelta(minutes=2),
    )
    conflict = _v2(
        intent=JobIntakeIntent.REQUEST_APPLICATION,
        proposal_id="proposal-request",
        run_id="run-request",
        provenance=request.provenance,
        recorded_at=NOW + timedelta(minutes=3),
    )

    assert repository.save(request).status is AcceptedJobIntentWriteStatus.CREATED
    assert repository.save(request).status is AcceptedJobIntentWriteStatus.UNCHANGED
    assert repository.save(later_add).status is AcceptedJobIntentWriteStatus.CREATED
    assert repository.save(conflict).status is AcceptedJobIntentWriteStatus.FAILED
    current = repository.get_current(subject_id="subject-v1", job_id="job-v1")
    assert current.intent == request
