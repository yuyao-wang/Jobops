"""Focused tests for the single-URL conversational intake boundary."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import pytest

from core.accepted_job_intent import (
    AcceptedJobIntent,
    AcceptedJobIntentReadResult,
    AcceptedJobIntentReadStatus,
    AcceptedJobIntentWriteResult,
    AcceptedJobIntentWriteStatus,
)
from core.conversational_intake import (
    ConversationalIntakeRequest,
    InMemoryPendingIntakeStore,
    IntakeAction,
    IntakeReason,
    IntakeResponseStatus,
    PendingIntakeStatus,
    ResolveIntakeReason,
    ResolveIntakeStatus,
    ResolvePendingIntakeRequest,
    handle_conversational_url_intake,
    resolve_pending_intake,
)
from core.job_discovery import (
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryReason,
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobDiscoveryResponse,
    JobIntakeIntent,
    ProposalResolution,
)
from source_connectors import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    ReadJobReason,
    ReadJobRequest,
    ReadJobResult,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 27, 21, 30, tzinfo=timezone.utc)


class FakePendingIntakeStore(InMemoryPendingIntakeStore):
    """Process-local fake used only by conversational boundary tests."""


class FakeJobDiscoveryPort:
    def __init__(
        self,
        *,
        change: DiscoveryChange = DiscoveryChange.CREATED,
        disposition: DiscoveryDisposition = DiscoveryDisposition.ACCEPTED,
        error: Exception | None = None,
    ) -> None:
        self.change = change
        self.disposition = disposition
        self.error = error
        self.calls: list[JobDiscoveryRequest] = []

    def __call__(
        self,
        request: JobDiscoveryRequest,
    ) -> JobDiscoveryResponse:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        accepted = self.disposition is DiscoveryDisposition.ACCEPTED
        reasons = {
            DiscoveryChange.CREATED: DiscoveryReason.JOB_CREATED,
            DiscoveryChange.UPDATED: DiscoveryReason.JOB_UPDATED,
            DiscoveryChange.UNCHANGED: DiscoveryReason.JOB_UNCHANGED,
        }
        return JobDiscoveryResponse(
            disposition=self.disposition,
            original_intent=request.proposal.intent,
            reason_code=(
                reasons[self.change]
                if accepted
                else DiscoveryReason.PROPOSAL_UNSUPPORTED
            ),
            run_id="discovery-run-synthetic-123",
            job_id="job-synthetic-123" if accepted else None,
            change=self.change if accepted else None,
        )


class FakeAcceptedJobIntentRepository:
    def __init__(self) -> None:
        self.calls: list[AcceptedJobIntent] = []
        self._records: dict[str, AcceptedJobIntent] = {}

    def save(
        self,
        intent: AcceptedJobIntent,
    ) -> AcceptedJobIntentWriteResult:
        self.calls.append(intent)
        existing = self._records.get(intent.accepted_job_intent_id)
        if existing is None:
            self._records[intent.accepted_job_intent_id] = intent
            return AcceptedJobIntentWriteResult(
                status=AcceptedJobIntentWriteStatus.CREATED,
                intent=intent,
                reason_code=None,
                retryable=False,
            )
        return AcceptedJobIntentWriteResult(
            status=AcceptedJobIntentWriteStatus.UNCHANGED,
            intent=existing,
            reason_code=None,
            retryable=False,
        )

    def get_current(
        self,
        *,
        subject_id: str,
        job_id: str,
    ) -> AcceptedJobIntentReadResult:
        matches = [
            item
            for item in self._records.values()
            if item.subject_id == subject_id and item.job_id == job_id
        ]
        if not matches:
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.NOT_FOUND,
                intent=None,
            )
        return AcceptedJobIntentReadResult(
            status=AcceptedJobIntentReadStatus.FOUND,
            intent=matches[-1],
        )


def _observation(
    platform: SourcePlatform,
    *,
    url: str,
    location: str = "Vancouver, BC",
) -> SourceJobObservation:
    ats_type = {
        SourcePlatform.GREENHOUSE: AtsType.GREENHOUSE,
        SourcePlatform.LEVER: AtsType.LEVER,
        SourcePlatform.GENERIC_WEB: AtsType.UNKNOWN,
    }[platform]
    return SourceJobObservation(
        source_platform=platform,
        source_job_id="synthetic-123",
        source_url=url,
        application_url=None,
        company="Example Labs",
        title="Synthetic Platform Engineer",
        description="Build deterministic job application systems.",
        location=location,
        work_mode=WorkMode.UNKNOWN,
        posted_at=None,
        ats_type=ats_type,
        observed_at="2026-07-27T21:00:00Z",
        provenance=(
            FieldProvenance(
                "source_url",
                ProvenanceSource.REQUEST,
                "url",
            ),
        ),
    )


def _store() -> FakePendingIntakeStore:
    return FakePendingIntakeStore(
        ttl=timedelta(minutes=20),
        id_factory=lambda: "pending-synthetic-123",
    )


def _seed_pending(
    *,
    conversation_id: str = "conversation-resolve",
    platform: SourcePlatform = SourcePlatform.LEVER,
) -> tuple[FakePendingIntakeStore, SourceJobObservation]:
    store = _store()
    observation = _observation(
        platform,
        url="https://jobs.lever.co/example/abc-123",
    )
    store.create(
        conversation_id=conversation_id,
        observation=observation,
        created_at=NOW,
    )
    return store, observation


@pytest.mark.parametrize(
    ("url", "platform"),
    (
        (
            "https://job-boards.greenhouse.io/example/jobs/123",
            SourcePlatform.GREENHOUSE,
        ),
        (
            "https://jobs.lever.co/example/abc-123",
            SourcePlatform.LEVER,
        ),
        (
            "https://careers.example.com/jobs/jsonld-123",
            SourcePlatform.GENERIC_WEB,
        ),
    ),
)
@pytest.mark.asyncio
async def test_bare_url_uses_one_provider_neutral_reader(
    url: str,
    platform: SourcePlatform,
) -> None:
    calls: list[ReadJobRequest] = []

    async def fake_reader(request: ReadJobRequest) -> ReadJobResult:
        calls.append(request)
        return ReadJobResult.succeeded(
            _observation(platform, url=request.url)
        )

    store = _store()
    response = await handle_conversational_url_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-1",
            message=url,
        ),
        pending_store=store,
        reader=fake_reader,
        clock=lambda: NOW,
    )

    assert calls == [ReadJobRequest(url=url)]
    assert not hasattr(calls[0], "platform")
    assert response.status is IntakeResponseStatus.NEEDS_USER
    assert response.reason_code is None
    assert response.pending_intake_id == "pending-synthetic-123"
    assert response.pending_status is PendingIntakeStatus.WAITING_FOR_ACTION
    assert response.summary is not None
    assert response.summary.company == "Example Labs"
    assert response.summary.title == "Synthetic Platform Engineer"
    assert response.summary.location == "Vancouver, BC"
    assert response.summary.source_platform is platform
    assert response.actions == (
        IntakeAction.ADD_JOB,
        IntakeAction.REQUEST_APPLICATION,
    )
    assert "Example Labs" in response.prompt
    assert "Synthetic Platform Engineer" in response.prompt

    pending = store.get("pending-synthetic-123", now=NOW)
    assert pending is not None
    assert pending.conversation_id == "conversation-1"
    assert pending.observation.source_platform is platform
    assert pending.status is PendingIntakeStatus.WAITING_FOR_ACTION
    assert pending.created_at == NOW
    assert pending.expires_at == NOW + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_one_url_inside_sentence_is_extracted_without_punctuation() -> None:
    calls: list[str] = []

    async def fake_reader(request: ReadJobRequest) -> ReadJobResult:
        calls.append(request.url)
        return ReadJobResult.succeeded(
            _observation(SourcePlatform.LEVER, url=request.url, location="")
        )

    response = await handle_conversational_url_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-2",
            message=(
                "帮我看看这个岗位："
                "https://jobs.lever.co/example/abc-123。"
            ),
        ),
        pending_store=_store(),
        reader=fake_reader,
        clock=lambda: NOW,
    )

    assert calls == ["https://jobs.lever.co/example/abc-123"]
    assert response.summary is not None
    assert response.summary.location == ""
    assert "located in" not in response.prompt


@pytest.mark.asyncio
async def test_no_url_needs_more_information_without_reader_call() -> None:
    called = False

    async def unexpected_reader(request: ReadJobRequest) -> ReadJobResult:
        nonlocal called
        called = True
        raise AssertionError("reader must not be called")

    store = _store()
    response = await handle_conversational_url_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-3",
            message="帮我看看昨天看到的岗位",
        ),
        pending_store=store,
        reader=unexpected_reader,
    )

    assert response.status is IntakeResponseStatus.NEEDS_USER
    assert response.reason_code is IntakeReason.NEEDS_MORE_INFORMATION
    assert response.pending_intake_id is None
    assert response.actions == ()
    assert store.count == 0
    assert called is False


@pytest.mark.asyncio
async def test_multiple_urls_need_selection_without_reader_call() -> None:
    called = False

    async def unexpected_reader(request: ReadJobRequest) -> ReadJobResult:
        nonlocal called
        called = True
        raise AssertionError("reader must not be called")

    store = _store()
    response = await handle_conversational_url_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-4",
            message=(
                "比较 https://jobs.lever.co/example/one 和 "
                "https://careers.example.com/jobs/two"
            ),
        ),
        pending_store=store,
        reader=unexpected_reader,
    )

    assert response.status is IntakeResponseStatus.NEEDS_USER
    assert response.reason_code is IntakeReason.NEEDS_USER_SELECTION
    assert response.pending_intake_id is None
    assert response.actions == ()
    assert store.count == 0
    assert called is False


@pytest.mark.parametrize(
    ("reason", "expected_status", "retryable"),
    (
        (
            ReadJobReason.INVALID_URL,
            IntakeResponseStatus.FAILED,
            False,
        ),
        (
            ReadJobReason.UNSAFE_URL,
            IntakeResponseStatus.FAILED,
            False,
        ),
        (
            ReadJobReason.UNSUPPORTED_URL,
            IntakeResponseStatus.UNSUPPORTED,
            False,
        ),
        (
            ReadJobReason.SOURCE_TIMEOUT,
            IntakeResponseStatus.FAILED,
            True,
        ),
        (
            ReadJobReason.SOURCE_RATE_LIMITED,
            IntakeResponseStatus.FAILED,
            True,
        ),
    ),
)
@pytest.mark.asyncio
async def test_reader_failure_preserves_reason_without_pending_state(
    reason: ReadJobReason,
    expected_status: IntakeResponseStatus,
    retryable: bool,
) -> None:
    async def fake_reader(request: ReadJobRequest) -> ReadJobResult:
        return ReadJobResult.failed(reason)

    store = _store()
    response = await handle_conversational_url_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-5",
            message="https://careers.example.com/jobs/failed",
        ),
        pending_store=store,
        reader=fake_reader,
    )

    assert response.status is expected_status
    assert response.reason_code is reason
    assert response.retryable is retryable
    assert response.pending_intake_id is None
    assert response.pending_status is None
    assert response.summary is None
    assert response.actions == ()
    assert store.count == 0


def test_pending_intake_expires_without_becoming_a_job() -> None:
    store = _store()
    pending = store.create(
        conversation_id="conversation-6",
        observation=_observation(
            SourcePlatform.GENERIC_WEB,
            url="https://careers.example.com/jobs/jsonld-123",
        ),
        created_at=NOW,
    )

    assert store.get(
        pending.pending_intake_id,
        now=NOW + timedelta(minutes=19),
    ) is pending
    assert (
        store.get(
            pending.pending_intake_id,
            now=NOW + timedelta(minutes=20),
        )
        is None
    )
    assert store.count == 0


@pytest.mark.asyncio
async def test_intake_depends_only_on_public_reader_and_has_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_home = tmp_path / "synthetic-private-home"
    monkeypatch.setenv("JOBOPS_HOME", str(synthetic_home))

    async def fake_reader(request: ReadJobRequest) -> ReadJobResult:
        return ReadJobResult.succeeded(
            _observation(
                SourcePlatform.GREENHOUSE,
                url=request.url,
            )
        )

    response = await handle_conversational_url_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-7",
            message="https://job-boards.greenhouse.io/example/jobs/123",
        ),
        pending_store=_store(),
        reader=fake_reader,
        clock=lambda: NOW,
    )

    assert response.pending_status is PendingIntakeStatus.WAITING_FOR_ACTION
    assert not synthetic_home.exists()

    source = (
        ROOT / "core" / "conversational_intake.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "source_connectors" in imported_modules
    assert imported_modules.isdisjoint(
        {
            "adapters",
            "core.private_home",
            "utils",
        }
    )
    assert imported_names.isdisjoint(
        {
            "GreenhousePublicJobReader",
            "LeverPublicJobReader",
            "GenericJsonLdJobReader",
            "JobPosting",
        }
    )


@pytest.mark.parametrize(
    ("action", "expected_intent"),
    (
        (IntakeAction.ADD_JOB, JobIntakeIntent.ADD_JOB),
        (
            IntakeAction.REQUEST_APPLICATION,
            JobIntakeIntent.REQUEST_APPLICATION,
        ),
    ),
)
def test_resolve_action_builds_one_typed_discovery_request(
    action: IntakeAction,
    expected_intent: JobIntakeIntent,
) -> None:
    store, observation = _seed_pending()
    discovery = FakeJobDiscoveryPort()
    intents = FakeAcceptedJobIntentRepository()

    response = resolve_pending_intake(
        ResolvePendingIntakeRequest(
            subject_id="subject-synthetic",
            conversation_id="conversation-resolve",
            pending_intake_id="pending-synthetic-123",
            action=action,
        ),
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )

    assert response.status is ResolveIntakeStatus.COMPLETED
    assert response.selected_action is action
    assert response.job_id == "job-synthetic-123"
    assert response.change is DiscoveryChange.CREATED
    assert len(discovery.calls) == 1
    assert len(intents.calls) == 1
    assert intents.calls[0].subject_id == "subject-synthetic"
    assert intents.calls[0].intent is expected_intent

    request = discovery.calls[0]
    assert request.request_id == "intake-pending-synthetic-123"
    assert request.trigger is DiscoveryTrigger.CONVERSATIONAL
    assert request.proposal.proposal_id == (
        "proposal-pending-synthetic-123"
    )
    assert request.proposal.intent is expected_intent
    assert request.proposal.resolution is ProposalResolution.RESOLVED
    assert request.proposal.missing_fields == ()
    assert request.proposal.alternatives == ()

    candidate = request.proposal.resolved_candidate
    assert candidate is not None
    assert candidate.source_platform == observation.source_platform.value
    assert candidate.source_job_id == observation.source_job_id
    assert candidate.source_url == observation.source_url
    assert candidate.application_url == observation.application_url
    assert candidate.company == observation.company
    assert candidate.title == observation.title
    assert candidate.description == observation.description
    assert candidate.location == observation.location
    assert candidate.work_mode == observation.work_mode.value
    assert candidate.posted_at == observation.posted_at
    assert candidate.ats_type == observation.ats_type.value

    completed = store.get("pending-synthetic-123", now=NOW)
    assert completed is not None
    assert completed.status is PendingIntakeStatus.COMPLETED
    assert completed.selected_action is action
    assert completed.discovery_response is response.discovery_response
    assert completed.resolved_at == NOW
    assert completed.observation is observation
    assert completed.observation.provenance is observation.provenance


@pytest.mark.parametrize(
    "change",
    (
        DiscoveryChange.CREATED,
        DiscoveryChange.UPDATED,
        DiscoveryChange.UNCHANGED,
    ),
)
def test_resolve_preserves_each_accepted_discovery_change(
    change: DiscoveryChange,
) -> None:
    store, _ = _seed_pending()
    discovery = FakeJobDiscoveryPort(change=change)
    intents = FakeAcceptedJobIntentRepository()

    response = resolve_pending_intake(
        ResolvePendingIntakeRequest(
            subject_id="subject-synthetic",
            conversation_id="conversation-resolve",
            pending_intake_id="pending-synthetic-123",
            action="ADD_JOB",
        ),
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )

    assert response.status is ResolveIntakeStatus.COMPLETED
    assert response.change is change
    assert response.discovery_response is not None
    assert response.discovery_response.change is change
    assert len(discovery.calls) == 1


def test_rejected_discovery_result_is_not_presented_as_success() -> None:
    store, _ = _seed_pending()
    discovery = FakeJobDiscoveryPort(
        disposition=DiscoveryDisposition.REJECTED
    )
    intents = FakeAcceptedJobIntentRepository()

    response = resolve_pending_intake(
        ResolvePendingIntakeRequest(
            subject_id="subject-synthetic",
            conversation_id="conversation-resolve",
            pending_intake_id="pending-synthetic-123",
            action="ADD_JOB",
        ),
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )

    assert response.status is ResolveIntakeStatus.FAILED
    assert response.reason_code is DiscoveryReason.PROPOSAL_UNSUPPORTED
    assert response.job_id is None
    assert response.change is None
    assert response.discovery_response is not None
    assert (
        response.discovery_response.disposition
        is DiscoveryDisposition.REJECTED
    )
    assert "rejected" in response.prompt
    assert intents.calls == []


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("not-found", ResolveIntakeReason.PENDING_INTAKE_NOT_FOUND),
        ("mismatch", ResolveIntakeReason.CONVERSATION_MISMATCH),
        ("expired", ResolveIntakeReason.PENDING_INTAKE_EXPIRED),
        ("invalid-action", ResolveIntakeReason.INVALID_ACTION),
    ),
)
def test_invalid_pending_resolution_never_calls_discovery(
    case: str,
    reason: ResolveIntakeReason,
) -> None:
    store, _ = _seed_pending()
    discovery = FakeJobDiscoveryPort()
    intents = FakeAcceptedJobIntentRepository()
    conversation_id = "conversation-resolve"
    pending_intake_id = "pending-synthetic-123"
    action = "ADD_JOB"
    now = NOW
    if case == "not-found":
        pending_intake_id = "pending-missing"
    elif case == "mismatch":
        conversation_id = "conversation-other"
    elif case == "expired":
        now = NOW + timedelta(minutes=20)
    else:
        action = "DELETE_JOB"

    response = resolve_pending_intake(
        ResolvePendingIntakeRequest(
            subject_id="subject-synthetic",
            conversation_id=conversation_id,
            pending_intake_id=pending_intake_id,
            action=action,
        ),
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: now,
    )

    assert response.status is ResolveIntakeStatus.FAILED
    assert response.reason_code is reason
    assert response.discovery_response is None
    assert response.job_id is None
    assert discovery.calls == []


def test_invalid_pending_observation_never_calls_discovery() -> None:
    store, _ = _seed_pending()
    pending = store.get("pending-synthetic-123", now=NOW)
    assert pending is not None
    store._items[pending.pending_intake_id] = replace(
        pending,
        observation=None,
    )
    discovery = FakeJobDiscoveryPort()
    intents = FakeAcceptedJobIntentRepository()

    response = resolve_pending_intake(
        ResolvePendingIntakeRequest(
            subject_id="subject-synthetic",
            conversation_id="conversation-resolve",
            pending_intake_id="pending-synthetic-123",
            action="ADD_JOB",
        ),
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )

    assert response.reason_code is (
        ResolveIntakeReason.PENDING_OBSERVATION_INVALID
    )
    assert discovery.calls == []
    pending = store.get("pending-synthetic-123", now=NOW)
    assert pending is not None
    assert pending.status is PendingIntakeStatus.WAITING_FOR_ACTION


def test_same_action_replays_first_result_without_second_discovery_call() -> None:
    store, _ = _seed_pending()
    discovery = FakeJobDiscoveryPort(change=DiscoveryChange.UPDATED)
    intents = FakeAcceptedJobIntentRepository()
    request = ResolvePendingIntakeRequest(
        subject_id="subject-synthetic",
        conversation_id="conversation-resolve",
        pending_intake_id="pending-synthetic-123",
        action="REQUEST_APPLICATION",
    )

    first = resolve_pending_intake(
        request,
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )
    repeated = resolve_pending_intake(
        request,
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )

    assert len(discovery.calls) == 1
    assert len(intents.calls) == 1
    assert repeated == first
    assert repeated.selected_action is IntakeAction.REQUEST_APPLICATION
    assert "application intent was recorded" in repeated.prompt
    assert "have not started" in repeated.prompt


def test_different_action_after_completion_returns_conflict() -> None:
    store, _ = _seed_pending()
    discovery = FakeJobDiscoveryPort()
    intents = FakeAcceptedJobIntentRepository()
    first_request = ResolvePendingIntakeRequest(
        subject_id="subject-synthetic",
        conversation_id="conversation-resolve",
        pending_intake_id="pending-synthetic-123",
        action="ADD_JOB",
    )
    resolve_pending_intake(
        first_request,
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )

    response = resolve_pending_intake(
        replace(first_request, action="REQUEST_APPLICATION"),
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )

    assert response.status is ResolveIntakeStatus.FAILED
    assert response.reason_code is (
        ResolveIntakeReason.PENDING_INTAKE_ALREADY_RESOLVED
    )
    assert response.selected_action is IntakeAction.ADD_JOB
    assert response.discovery_response is None
    assert len(discovery.calls) == 1


def test_concurrent_duplicate_cannot_make_a_second_discovery_call() -> None:
    store, _ = _seed_pending()
    intents = FakeAcceptedJobIntentRepository()
    entered = Event()
    release = Event()
    calls: list[JobDiscoveryRequest] = []
    first_result = []

    def blocking_discovery(
        request: JobDiscoveryRequest,
    ) -> JobDiscoveryResponse:
        calls.append(request)
        entered.set()
        assert release.wait(timeout=2)
        return FakeJobDiscoveryPort()(request)

    request = ResolvePendingIntakeRequest(
        subject_id="subject-synthetic",
        conversation_id="conversation-resolve",
        pending_intake_id="pending-synthetic-123",
        action="ADD_JOB",
    )
    first = Thread(
        target=lambda: first_result.append(
            resolve_pending_intake(
                request,
                pending_store=store,
                accepted_intent_repository=intents,
                discovery_port=blocking_discovery,
                clock=lambda: NOW,
            )
        )
    )
    first.start()
    assert entered.wait(timeout=2)

    duplicate = resolve_pending_intake(
        request,
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=blocking_discovery,
        clock=lambda: NOW,
    )
    release.set()
    first.join(timeout=2)

    assert not first.is_alive()
    assert len(calls) == 1
    assert duplicate.reason_code is (
        ResolveIntakeReason.PENDING_INTAKE_ALREADY_RESOLVED
    )
    assert len(first_result) == 1
    assert first_result[0].status is ResolveIntakeStatus.COMPLETED


def test_discovery_exception_releases_claim_for_safe_manual_retry() -> None:
    store, _ = _seed_pending()
    failing_discovery = FakeJobDiscoveryPort(
        error=RuntimeError("synthetic infrastructure failure")
    )
    intents = FakeAcceptedJobIntentRepository()
    request = ResolvePendingIntakeRequest(
        subject_id="subject-synthetic",
        conversation_id="conversation-resolve",
        pending_intake_id="pending-synthetic-123",
        action="ADD_JOB",
    )

    failed = resolve_pending_intake(
        request,
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=failing_discovery,
        clock=lambda: NOW,
    )

    assert failed.status is ResolveIntakeStatus.FAILED
    assert failed.reason_code is (
        ResolveIntakeReason.DISCOVERY_TEMPORARILY_UNAVAILABLE
    )
    assert failed.retryable is True
    assert failed.discovery_response is None
    pending = store.get("pending-synthetic-123", now=NOW)
    assert pending is not None
    assert pending.status is PendingIntakeStatus.WAITING_FOR_ACTION
    assert pending.selected_action is None

    succeeding_discovery = FakeJobDiscoveryPort()
    retried = resolve_pending_intake(
        request,
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=succeeding_discovery,
        clock=lambda: NOW,
    )
    assert retried.status is ResolveIntakeStatus.COMPLETED
    assert len(failing_discovery.calls) == 1
    assert len(succeeding_discovery.calls) == 1


def test_i2_uses_only_injected_discovery_port_and_writes_no_private_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_home = tmp_path / "synthetic-private-home"
    monkeypatch.setenv("JOBOPS_HOME", str(synthetic_home))
    store, _ = _seed_pending()
    discovery = FakeJobDiscoveryPort()
    intents = FakeAcceptedJobIntentRepository()

    response = resolve_pending_intake(
        ResolvePendingIntakeRequest(
            subject_id="subject-synthetic",
            conversation_id="conversation-resolve",
            pending_intake_id="pending-synthetic-123",
            action="REQUEST_APPLICATION",
        ),
        pending_store=store,
        accepted_intent_repository=intents,
        discovery_port=discovery,
        clock=lambda: NOW,
    )

    assert response.status is ResolveIntakeStatus.COMPLETED
    assert not synthetic_home.exists()

    source = (
        ROOT / "core" / "conversational_intake.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported_modules.isdisjoint(
        {
            "adapters",
            "core.private_home",
            "utils",
        }
    )
    resolve_node = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "resolve_pending_intake"
    )
    called_names = {
        node.func.id
        for node in ast.walk(resolve_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
    }
    assert "discovery_port" in called_names
    assert "run_discovery" not in called_names
    assert "read_public_job" not in called_names
