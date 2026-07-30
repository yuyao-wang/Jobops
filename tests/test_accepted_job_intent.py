"""Focused synthetic tests for accepted I2 job-intent persistence."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.accepted_job_intent import (
    ACCEPTED_JOB_INTENT_CONTRACT_VERSION,
    ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION,
    AcceptedJobIntent,
    AcceptedJobIntentFailureReason,
    AcceptedJobIntentReadResult,
    AcceptedJobIntentReadStatus,
    AcceptedJobIntentSourceProvenance,
    AcceptedJobIntentSourceType,
    AcceptedJobIntentWriteResult,
    AcceptedJobIntentWriteStatus,
    PrivateHomeAcceptedJobIntentRepository,
)
from core.conversational_intake import (
    InMemoryPendingIntakeStore,
    PendingIntakeStatus,
    ResolveIntakeReason,
    ResolveIntakeStatus,
    ResolvePendingIntakeRequest,
    resolve_pending_intake,
)
from core.job_discovery import (
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryReason,
    JobDiscoveryRequest,
    JobDiscoveryResponse,
    JobIntakeIntent,
)
from core.private_home import PrivateHome
from core.subject_job_discovery import (
    SubjectJobDiscoveryCommand,
    SubjectJobDiscoveryResult,
    SubjectJobDiscoveryStatus,
)
from core.subject_job_library import RegisterSubjectJobMembershipStatus
from source_connectors import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)


def _record(
    *,
    subject_id: str = "subject-a",
    job_id: str = "job-a",
    intent: JobIntakeIntent = JobIntakeIntent.ADD_JOB,
    proposal_id: str = "proposal-a",
    run_id: str = "discovery-run-a",
    recorded_at: datetime = NOW,
) -> AcceptedJobIntent:
    return AcceptedJobIntent.create(
        subject_id=subject_id,
        job_id=job_id,
        intent=intent,
        intake_proposal_id=proposal_id,
        discovery_run_id=run_id,
        recorded_at=recorded_at,
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType.CONVERSATIONAL_INTAKE,
            source_id=proposal_id,
        ),
    )


def _observation() -> SourceJobObservation:
    return SourceJobObservation(
        source_platform=SourcePlatform.GREENHOUSE,
        source_job_id="source-job-a",
        source_url="https://boards.greenhouse.io/acme/jobs/123",
        application_url=None,
        company="Acme",
        title="Backend Engineer",
        description="Build reliable services.",
        location="Vancouver, Canada",
        work_mode=WorkMode.HYBRID,
        posted_at=None,
        ats_type=AtsType.GREENHOUSE,
        observed_at="2026-07-27T21:00:00Z",
        provenance=(
            FieldProvenance(
                field="source_url",
                source=ProvenanceSource.REQUEST,
                source_field="url",
            ),
        ),
    )


def _pending_store(pending_id: str) -> InMemoryPendingIntakeStore:
    store = InMemoryPendingIntakeStore(
        ttl=timedelta(minutes=30),
        id_factory=lambda: pending_id,
    )
    store.create(
        conversation_id="conversation-a",
        observation=_observation(),
        created_at=NOW,
    )
    return store


class _DiscoveryPort:
    def __init__(
        self,
        *,
        job_id: str | None = "job-a",
        run_id: str | None = "discovery-run-a",
        disposition: DiscoveryDisposition = DiscoveryDisposition.ACCEPTED,
    ) -> None:
        self.job_id = job_id
        self.run_id = run_id
        self.disposition = disposition
        self.calls: list[SubjectJobDiscoveryCommand] = []

    def __call__(
        self,
        request: SubjectJobDiscoveryCommand,
    ) -> SubjectJobDiscoveryResult:
        self.calls.append(request)
        accepted = self.disposition is DiscoveryDisposition.ACCEPTED
        response = JobDiscoveryResponse(
            disposition=self.disposition,
            original_intent=request.request.proposal.intent,
            reason_code=(
                DiscoveryReason.JOB_CREATED
                if accepted
                else DiscoveryReason.PROPOSAL_UNSUPPORTED
            ),
            run_id=self.run_id,
            job_id=self.job_id if accepted else None,
            change=DiscoveryChange.CREATED if accepted else None,
        )
        return SubjectJobDiscoveryResult(
            (
                SubjectJobDiscoveryStatus.ACCEPTED
                if accepted
                else SubjectJobDiscoveryStatus.NOT_ACCEPTED
            ),
            response,
            object() if accepted else None,
            (
                RegisterSubjectJobMembershipStatus.CREATED
                if accepted
                else None
            ),
        )


class _FailingIntentRepository:
    def __init__(self, *, recover: bool = False) -> None:
        self.recover = recover
        self.calls: list[AcceptedJobIntent] = []

    def save(
        self,
        intent: AcceptedJobIntent,
    ) -> AcceptedJobIntentWriteResult:
        self.calls.append(intent)
        if self.recover and len(self.calls) > 1:
            return AcceptedJobIntentWriteResult(
                status=AcceptedJobIntentWriteStatus.CREATED,
                intent=intent,
                reason_code=None,
                retryable=False,
            )
        return AcceptedJobIntentWriteResult(
            status=AcceptedJobIntentWriteStatus.FAILED,
            intent=None,
            reason_code=AcceptedJobIntentFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )

    def get_current(
        self,
        *,
        subject_id: str,
        job_id: str,
    ) -> AcceptedJobIntentReadResult:
        return AcceptedJobIntentReadResult(
            status=AcceptedJobIntentReadStatus.NOT_FOUND,
            intent=None,
        )


def _resolve(
    *,
    store: InMemoryPendingIntakeStore,
    pending_id: str,
    action: JobIntakeIntent,
    repository,
    discovery,
    subject_id: str = "subject-a",
):
    return resolve_pending_intake(
        ResolvePendingIntakeRequest(
            subject_id=subject_id,
            conversation_id="conversation-a",
            pending_intake_id=pending_id,
            action=action.value,
        ),
        pending_store=store,
        accepted_intent_repository=repository,
        discovery_port=discovery,
        clock=lambda: NOW,
    )


@pytest.mark.parametrize(
    "intent",
    (JobIntakeIntent.ADD_JOB, JobIntakeIntent.REQUEST_APPLICATION),
)
def test_i2_persists_typed_intent_after_accepted_discovery(
    tmp_path: Path,
    intent: JobIntakeIntent,
) -> None:
    home = PrivateHome(tmp_path / "private")
    repository = PrivateHomeAcceptedJobIntentRepository(home)
    pending_id = f"pending-{intent.value.casefold()}"
    discovery = _DiscoveryPort()

    response = _resolve(
        store=_pending_store(pending_id),
        pending_id=pending_id,
        action=intent,
        repository=repository,
        discovery=discovery,
    )

    assert response.status is ResolveIntakeStatus.COMPLETED
    assert response.accepted_intent_write_result is not None
    assert (
        response.accepted_intent_write_result.status
        is AcceptedJobIntentWriteStatus.CREATED
    )
    record = response.accepted_intent_write_result.intent
    assert record is not None
    assert record.subject_id == "subject-a"
    assert record.job_id == "job-a"
    assert record.intent is intent
    assert record.intake_proposal_id == f"proposal-{pending_id}"
    assert record.discovery_run_id == "discovery-run-a"
    assert record.recorded_at == NOW
    assert (
        record.contract_version
        == ACCEPTED_JOB_INTENT_CONTRACT_VERSION
    )
    restarted = PrivateHomeAcceptedJobIntentRepository(home).get_current(
        subject_id="subject-a",
        job_id="job-a",
    )
    assert restarted.status is AcceptedJobIntentReadStatus.FOUND
    assert restarted.intent == record


def test_discovery_rejection_never_writes_accepted_intent(
    tmp_path: Path,
) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    discovery = _DiscoveryPort(
        disposition=DiscoveryDisposition.REJECTED
    )

    response = _resolve(
        store=_pending_store("pending-rejected"),
        pending_id="pending-rejected",
        action=JobIntakeIntent.REQUEST_APPLICATION,
        repository=repository,
        discovery=discovery,
    )

    assert response.status is ResolveIntakeStatus.FAILED
    assert response.accepted_intent_write_result is None
    assert (
        repository.get_current(subject_id="subject-a", job_id="job-a").status
        is AcceptedJobIntentReadStatus.NOT_FOUND
    )


def test_persistence_failure_is_typed_and_replay_skips_discovery() -> None:
    store = _pending_store("pending-retry")
    discovery = _DiscoveryPort()
    repository = _FailingIntentRepository(recover=True)

    failed = _resolve(
        store=store,
        pending_id="pending-retry",
        action=JobIntakeIntent.REQUEST_APPLICATION,
        repository=repository,
        discovery=discovery,
    )
    recovered = _resolve(
        store=store,
        pending_id="pending-retry",
        action=JobIntakeIntent.REQUEST_APPLICATION,
        repository=repository,
        discovery=discovery,
    )

    assert failed.status is ResolveIntakeStatus.FAILED
    assert failed.reason_code is (
        ResolveIntakeReason.ACCEPTED_INTENT_PERSISTENCE_FAILED
    )
    assert failed.retryable is True
    assert recovered.status is ResolveIntakeStatus.COMPLETED
    assert len(discovery.calls) == 1
    assert len(repository.calls) == 2
    assert repository.calls[0].recorded_at == repository.calls[1].recorded_at
    assert (
        repository.calls[0].accepted_job_intent_id
        == repository.calls[1].accepted_job_intent_id
    )


def test_completed_i2_replay_writes_nothing_again(
    tmp_path: Path,
) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    store = _pending_store("pending-replay")
    discovery = _DiscoveryPort()

    first = _resolve(
        store=store,
        pending_id="pending-replay",
        action=JobIntakeIntent.REQUEST_APPLICATION,
        repository=repository,
        discovery=discovery,
    )
    path = next(
        repository._home.paths.accepted_job_intents.rglob("*.json")
    )
    first_bytes = path.read_bytes()
    repeated = _resolve(
        store=store,
        pending_id="pending-replay",
        action=JobIntakeIntent.REQUEST_APPLICATION,
        repository=repository,
        discovery=discovery,
    )

    assert repeated == first
    assert len(discovery.calls) == 1
    assert path.read_bytes() == first_bytes
    assert len(tuple(path.parent.glob("*.json"))) == 1


def test_missing_discovery_identity_fails_without_repository_write() -> None:
    store = _pending_store("pending-invalid-discovery")
    repository = _FailingIntentRepository()
    discovery = _DiscoveryPort(run_id=None)

    response = _resolve(
        store=store,
        pending_id="pending-invalid-discovery",
        action=JobIntakeIntent.ADD_JOB,
        repository=repository,
        discovery=discovery,
    )

    assert response.status is ResolveIntakeStatus.FAILED
    assert response.reason_code is ResolveIntakeReason.DISCOVERY_RESPONSE_INVALID
    assert response.retryable is False
    assert repository.calls == []
    assert len(discovery.calls) == 1


def test_current_intent_precedence_never_treats_add_as_cancellation(
    tmp_path: Path,
) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    add_first = _record(
        proposal_id="proposal-add-first",
        run_id="run-add-first",
        recorded_at=NOW,
    )
    request = _record(
        intent=JobIntakeIntent.REQUEST_APPLICATION,
        proposal_id="proposal-request",
        run_id="run-request",
        recorded_at=NOW + timedelta(minutes=1),
    )
    add_later = _record(
        proposal_id="proposal-add-later",
        run_id="run-add-later",
        recorded_at=NOW + timedelta(minutes=2),
    )

    assert repository.save(add_first).status is AcceptedJobIntentWriteStatus.CREATED
    assert repository.save(request).status is AcceptedJobIntentWriteStatus.CREATED
    assert repository.save(add_later).status is AcceptedJobIntentWriteStatus.CREATED
    current = repository.get_current(subject_id="subject-a", job_id="job-a")

    assert current.status is AcceptedJobIntentReadStatus.FOUND
    assert current.intent == request


@pytest.mark.parametrize(
    ("first_action", "second_action"),
    (
        (
            JobIntakeIntent.ADD_JOB,
            JobIntakeIntent.REQUEST_APPLICATION,
        ),
        (
            JobIntakeIntent.REQUEST_APPLICATION,
            JobIntakeIntent.ADD_JOB,
        ),
    ),
)
def test_i2_action_history_preserves_request_application_precedence(
    tmp_path: Path,
    first_action: JobIntakeIntent,
    second_action: JobIntakeIntent,
) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    first_discovery = _DiscoveryPort(run_id="discovery-run-first")
    second_discovery = _DiscoveryPort(run_id="discovery-run-second")

    first = _resolve(
        store=_pending_store("pending-first"),
        pending_id="pending-first",
        action=first_action,
        repository=repository,
        discovery=first_discovery,
    )
    second = _resolve(
        store=_pending_store("pending-second"),
        pending_id="pending-second",
        action=second_action,
        repository=repository,
        discovery=second_discovery,
    )
    current = repository.get_current(
        subject_id="subject-a",
        job_id="job-a",
    )

    assert first.status is ResolveIntakeStatus.COMPLETED
    assert second.status is ResolveIntakeStatus.COMPLETED
    assert current.status is AcceptedJobIntentReadStatus.FOUND
    assert current.intent is not None
    assert current.intent.intent is JobIntakeIntent.REQUEST_APPLICATION


def test_repository_isolates_subjects_and_jobs(tmp_path: Path) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    subject_a = _record(
        intent=JobIntakeIntent.REQUEST_APPLICATION,
    )
    subject_b = _record(
        subject_id="subject-b",
        proposal_id="proposal-b",
        run_id="run-b",
    )
    job_b = _record(
        job_id="job-b",
        proposal_id="proposal-job-b",
        run_id="run-job-b",
    )
    for item in (subject_a, subject_b, job_b):
        assert repository.save(item).status is AcceptedJobIntentWriteStatus.CREATED

    assert repository.get_current(
        subject_id="subject-a", job_id="job-a"
    ).intent == subject_a
    assert repository.get_current(
        subject_id="subject-b", job_id="job-a"
    ).intent == subject_b
    assert repository.get_current(
        subject_id="subject-a", job_id="job-b"
    ).intent == job_b


def test_not_found_is_distinct_from_corrupted_record(tmp_path: Path) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    assert repository.get_current(
        subject_id="subject-a",
        job_id="job-a",
    ).status is AcceptedJobIntentReadStatus.NOT_FOUND

    record = _record()
    assert repository.save(record).status is AcceptedJobIntentWriteStatus.CREATED
    path = repository._path(record)
    path.write_text("{not-json", encoding="utf-8")
    corrupted = repository.get_current(
        subject_id="subject-a",
        job_id="job-a",
    )

    assert corrupted.status is AcceptedJobIntentReadStatus.INTEGRITY_FAILURE
    assert (
        corrupted.reason_code
        is AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
    )


def test_same_identity_different_content_is_immutable_conflict(
    tmp_path: Path,
) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    first = _record()
    conflicting = _record(recorded_at=NOW + timedelta(minutes=1))

    assert first.accepted_job_intent_id == conflicting.accepted_job_intent_id
    assert repository.save(first).status is AcceptedJobIntentWriteStatus.CREATED
    result = repository.save(conflicting)

    assert result.status is AcceptedJobIntentWriteStatus.FAILED
    assert (
        result.reason_code
        is AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
    )
    assert result.retryable is False
    assert repository.get_current(
        subject_id="subject-a",
        job_id="job-a",
    ).intent == first


def test_exact_repository_replay_returns_unchanged(tmp_path: Path) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    record = _record()

    created = repository.save(record)
    unchanged = repository.save(record)

    assert created.status is AcceptedJobIntentWriteStatus.CREATED
    assert unchanged.status is AcceptedJobIntentWriteStatus.UNCHANGED
    assert unchanged.intent == record
    assert len(tuple(repository._path(record).parent.glob("*.json"))) == 1


def test_invalid_subject_fails_before_discovery() -> None:
    discovery = _DiscoveryPort()
    with pytest.raises(ValueError):
        ResolvePendingIntakeRequest(
            subject_id=" ",
            conversation_id="conversation-a",
            pending_intake_id="pending-a",
            action="ADD_JOB",
        )
    assert discovery.calls == []


def test_completed_pending_intake_cannot_replay_for_another_subject(
    tmp_path: Path,
) -> None:
    repository = PrivateHomeAcceptedJobIntentRepository(
        PrivateHome(tmp_path / "private")
    )
    store = _pending_store("pending-subject-bound")
    discovery = _DiscoveryPort()
    first = _resolve(
        store=store,
        pending_id="pending-subject-bound",
        action=JobIntakeIntent.REQUEST_APPLICATION,
        repository=repository,
        discovery=discovery,
        subject_id="subject-a",
    )
    mismatched = _resolve(
        store=store,
        pending_id="pending-subject-bound",
        action=JobIntakeIntent.REQUEST_APPLICATION,
        repository=repository,
        discovery=discovery,
        subject_id="subject-b",
    )

    assert first.status is ResolveIntakeStatus.COMPLETED
    assert mismatched.status is ResolveIntakeStatus.FAILED
    assert mismatched.reason_code is ResolveIntakeReason.SUBJECT_MISMATCH
    assert len(discovery.calls) == 1
    assert repository.get_current(
        subject_id="subject-b",
        job_id="job-a",
    ).status is AcceptedJobIntentReadStatus.NOT_FOUND


def test_i2b_has_no_priority_or_execution_dependencies() -> None:
    module_paths = (
        ROOT / "core" / "accepted_job_intent.py",
        ROOT / "core" / "conversational_intake.py",
    )
    forbidden = {
        "application_engine",
        "current_priority_queue",
        "job_prioritization",
        "materials",
        "selective_reprioritization",
        "single_job_priority",
        "adapters",
        "utils.tracker",
    }
    for module_path in module_paths:
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert all(
            not any(
                name == blocked or name.startswith(f"{blocked}.")
                for blocked in forbidden
            )
            for name in imported
        )
