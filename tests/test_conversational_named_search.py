"""Focused tests for conversational named-job search and selection state."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.conversational_intake import (
    CandidateSelectionReason,
    CandidateSelectionRequest,
    CandidateSelectionStatus,
    ConversationalIntakeRequest,
    InMemoryCandidateSelectionStore,
    InMemoryPendingIntakeStore,
    IntakeAction,
    IntakeReason,
    IntakeResponseStatus,
    NamedJobClues,
    NamedJobIntentHint,
    NamedJobSearchResponse,
    NamedSearchReason,
    PendingIntakeStatus,
    handle_conversational_intake,
    select_search_candidate,
)
from core.job_search import (
    CandidateSet,
    JobSearchReason,
    JobSearchRequest,
    JobSearchResult,
    JobSearchStatus,
    SearchCandidate,
)
from source_connectors import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    ReadJobRequest,
    ReadJobReason,
    ReadJobResult,
    ReadJobStatus,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc)


class FakeNamedJobClueExtractor:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[str] = []

    async def extract(self, message: str) -> NamedJobClues:
        self.calls.append(message)
        return self.result  # type: ignore[return-value]


class FakeJobSearchPort:
    def __init__(self, result: JobSearchResult) -> None:
        self.result = result
        self.calls: list[JobSearchRequest] = []

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        self.calls.append(request)
        return self.result


class FakeCandidateSelectionStore(InMemoryCandidateSelectionStore):
    """Process-local fake retaining the production atomic store behavior."""


class FakePublicJobReader:
    def __init__(
        self,
        result: ReadJobResult | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[ReadJobRequest] = []

    async def __call__(self, request: ReadJobRequest) -> ReadJobResult:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        if self.result is None:
            raise AssertionError("fake reader has no result")
        return self.result


def _candidate(
    source_job_id: str,
    *,
    title: str,
    location: str | None = "Vancouver, BC",
) -> SearchCandidate:
    return SearchCandidate(
        candidate_id=f"greenhouse:acme:{source_job_id}",
        company="Acme",
        title=title,
        location=location,
        source_platform=SourcePlatform.GREENHOUSE,
        source_url=(
            f"https://job-boards.greenhouse.io/acme/jobs/{source_job_id}"
        ),
        source_job_id=source_job_id,
    )


def _search_result(
    candidates: tuple[SearchCandidate, ...],
) -> JobSearchResult:
    return JobSearchResult.succeeded(
        CandidateSet(
            candidate_set_id="candidate-set-named-synthetic",
            request_id="search-request-named-synthetic",
            candidates=candidates,
            created_at=NOW,
        )
    )


def _clues(
    *,
    company: str | None = "Acme",
    title: str | None = "Backend Engineer",
    location: str | None = None,
    intent_hint: NamedJobIntentHint = NamedJobIntentHint.UNSPECIFIED,
    missing_fields: tuple[str, ...] = (),
) -> NamedJobClues:
    return NamedJobClues(
        company=company,
        title=title,
        location=location,
        intent_hint=intent_hint,
        missing_fields=missing_fields,
    )


def _pending_store() -> InMemoryPendingIntakeStore:
    return InMemoryPendingIntakeStore(
        ttl=timedelta(minutes=20),
        id_factory=lambda: "pending-url-synthetic",
    )


def _candidate_store(
    *,
    ttl: timedelta = timedelta(minutes=15),
) -> FakeCandidateSelectionStore:
    return FakeCandidateSelectionStore(ttl=ttl)


def _observation(
    *,
    source_url: str = (
        "https://job-boards.greenhouse.io/acme/jobs/1001"
    ),
) -> SourceJobObservation:
    return SourceJobObservation(
        source_platform=SourcePlatform.GREENHOUSE,
        source_job_id="1001",
        source_url=source_url,
        application_url=None,
        company="Acme",
        title="Backend Engineer",
        description="Build deterministic backend systems.",
        location="Vancouver, BC",
        work_mode=WorkMode.UNKNOWN,
        posted_at=None,
        ats_type=AtsType.GREENHOUSE,
        observed_at="2026-07-27T22:00:00Z",
        provenance=(
            FieldProvenance(
                "source_url",
                ProvenanceSource.REQUEST,
                "url",
            ),
        ),
    )


def _seed_candidate_selection(
    *,
    candidates: tuple[SearchCandidate, ...] | None = None,
    conversation_id: str = "conversation-select",
    intent_hint: NamedJobIntentHint = NamedJobIntentHint.REQUEST_APPLICATION,
    created_at: datetime = NOW,
    ttl: timedelta = timedelta(minutes=15),
) -> FakeCandidateSelectionStore:
    items = candidates or (
        _candidate("1001", title="Backend Engineer"),
        _candidate("1002", title="Senior Backend Engineer"),
    )
    store = _candidate_store(ttl=ttl)
    candidate_set = CandidateSet(
        candidate_set_id="candidate-set-select-synthetic",
        request_id="search-request-select-synthetic",
        candidates=items,
        created_at=created_at,
    )
    store.create(
        conversation_id=conversation_id,
        candidate_set=candidate_set,
        intent_hint=intent_hint,
        search_request=JobSearchRequest(
            request_id="search-request-select-synthetic",
            company="Acme",
            title="Backend Engineer",
        ),
        created_at=created_at,
    )
    return store


async def _unexpected_reader(request: ReadJobRequest) -> ReadJobResult:
    raise AssertionError("named search must not call Public Job Reader")


@pytest.mark.asyncio
async def test_named_message_builds_one_search_request_with_location() -> None:
    extractor = FakeNamedJobClueExtractor(
        _clues(
            company="  Acme  ",
            title="  Backend Engineer ",
            location=" Vancouver ",
            intent_hint=NamedJobIntentHint.ADD_JOB,
        )
    )
    search_port = FakeJobSearchPort(
        _search_result((_candidate("1001", title="Backend Engineer"),))
    )

    response = await handle_conversational_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-named",
            message="帮我找 Acme 在 Vancouver 的 Backend Engineer",
        ),
        pending_store=_pending_store(),
        candidate_store=_candidate_store(),
        clue_extractor=extractor,
        job_search_port=search_port,
        reader=_unexpected_reader,
        clock=lambda: NOW,
        request_id_factory=lambda: "search-request-named-synthetic",
    )

    assert isinstance(response, NamedJobSearchResponse)
    assert len(extractor.calls) == 1
    assert len(search_port.calls) == 1
    assert search_port.calls[0] == JobSearchRequest(
        request_id="search-request-named-synthetic",
        company="Acme",
        title="Backend Engineer",
        location="Vancouver",
    )
    assert response.intent_hint is NamedJobIntentHint.ADD_JOB


@pytest.mark.asyncio
async def test_url_route_preserves_i1_and_never_calls_named_search() -> None:
    class UnexpectedExtractor:
        calls = 0

        async def extract(self, message: str) -> NamedJobClues:
            self.calls += 1
            raise AssertionError("URL route must not extract named clues")

    class UnexpectedSearch:
        calls = 0

        async def search(self, request: JobSearchRequest) -> JobSearchResult:
            self.calls += 1
            raise AssertionError("URL route must not call JobSearchPort")

    async def fake_reader(request: ReadJobRequest) -> ReadJobResult:
        return ReadJobResult.succeeded(
            SourceJobObservation(
                source_platform=SourcePlatform.GREENHOUSE,
                source_job_id="1001",
                source_url=request.url,
                application_url=None,
                company="Acme",
                title="Backend Engineer",
                description="Build synthetic systems.",
                location="Vancouver, BC",
                work_mode=WorkMode.UNKNOWN,
                posted_at=None,
                ats_type=AtsType.GREENHOUSE,
                observed_at="2026-07-27T22:00:00Z",
                provenance=(
                    FieldProvenance(
                        "source_url",
                        ProvenanceSource.REQUEST,
                        "url",
                    ),
                ),
            )
        )

    extractor = UnexpectedExtractor()
    search_port = UnexpectedSearch()
    pending_store = _pending_store()
    candidate_store = _candidate_store()
    response = await handle_conversational_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-url-priority",
            message=(
                "申请 Acme 的 Backend Engineer："
                "https://job-boards.greenhouse.io/acme/jobs/1001"
            ),
        ),
        pending_store=pending_store,
        candidate_store=candidate_store,
        clue_extractor=extractor,
        job_search_port=search_port,
        reader=fake_reader,
        clock=lambda: NOW,
    )

    assert response.pending_status is PendingIntakeStatus.WAITING_FOR_ACTION
    assert pending_store.count == 1
    assert candidate_store.count == 0
    assert extractor.calls == 0
    assert search_port.calls == 0


@pytest.mark.parametrize(
    ("clues", "expected_missing"),
    (
        (
            _clues(
                company=None,
                missing_fields=("company",),
            ),
            ("company",),
        ),
        (
            _clues(
                title=None,
                missing_fields=("title",),
            ),
            ("title",),
        ),
    ),
)
@pytest.mark.asyncio
async def test_missing_required_clue_never_calls_search(
    clues: NamedJobClues,
    expected_missing: tuple[str, ...],
) -> None:
    search_port = FakeJobSearchPort(_search_result(()))
    response = await handle_conversational_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-missing",
            message="帮我找一个岗位",
        ),
        pending_store=_pending_store(),
        candidate_store=_candidate_store(),
        clue_extractor=FakeNamedJobClueExtractor(clues),
        job_search_port=search_port,
        reader=_unexpected_reader,
    )

    assert isinstance(response, NamedJobSearchResponse)
    assert response.status is IntakeResponseStatus.NEEDS_USER
    assert response.reason_code is IntakeReason.NEEDS_MORE_INFORMATION
    assert response.missing_fields == expected_missing
    assert expected_missing[0] in response.prompt
    assert search_port.calls == []


@pytest.mark.asyncio
async def test_invalid_extractor_result_never_calls_search() -> None:
    search_port = FakeJobSearchPort(_search_result(()))
    response = await handle_conversational_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-invalid",
            message="找一下 Acme 的 Backend Engineer",
        ),
        pending_store=_pending_store(),
        candidate_store=_candidate_store(),
        clue_extractor=FakeNamedJobClueExtractor(
            {"company": "Acme", "title": "Backend Engineer"}
        ),
        job_search_port=search_port,
        reader=_unexpected_reader,
    )

    assert isinstance(response, NamedJobSearchResponse)
    assert response.status is IntakeResponseStatus.FAILED
    assert response.reason_code is JobSearchReason.INVALID_REQUEST
    assert search_port.calls == []


@pytest.mark.asyncio
async def test_zero_candidates_is_needs_user_without_selection_state_or_retry() -> None:
    search_port = FakeJobSearchPort(_search_result(()))
    store = _candidate_store()
    response = await handle_conversational_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-zero",
            message="找一下 Acme 的 Backend Engineer",
        ),
        pending_store=_pending_store(),
        candidate_store=store,
        clue_extractor=FakeNamedJobClueExtractor(_clues()),
        job_search_port=search_port,
        reader=_unexpected_reader,
        request_id_factory=lambda: "search-request-named-synthetic",
    )

    assert isinstance(response, NamedJobSearchResponse)
    assert response.status is IntakeResponseStatus.NEEDS_USER
    assert response.reason_code is NamedSearchReason.NO_CANDIDATES
    assert response.retryable is False
    assert response.candidate_set_id is None
    assert response.selection_status is None
    assert response.candidates == ()
    assert store.count == 0
    assert len(search_port.calls) == 1


@pytest.mark.parametrize(
    "candidates",
    (
        (_candidate("1001", title="Backend Engineer"),),
        (
            _candidate("1002", title="Senior Backend Engineer"),
            _candidate("1001", title="Backend Engineer"),
        ),
    ),
)
@pytest.mark.asyncio
async def test_one_or_multiple_candidates_wait_for_explicit_selection(
    candidates: tuple[SearchCandidate, ...],
) -> None:
    store = _candidate_store()
    search_port = FakeJobSearchPort(_search_result(candidates))
    response = await handle_conversational_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-candidates",
            message="申请 Acme 的 Backend Engineer",
        ),
        pending_store=_pending_store(),
        candidate_store=store,
        clue_extractor=FakeNamedJobClueExtractor(
            _clues(intent_hint=NamedJobIntentHint.REQUEST_APPLICATION)
        ),
        job_search_port=search_port,
        reader=_unexpected_reader,
        clock=lambda: NOW,
        request_id_factory=lambda: "search-request-named-synthetic",
    )

    assert isinstance(response, NamedJobSearchResponse)
    assert response.status is IntakeResponseStatus.NEEDS_USER
    assert response.reason_code is None
    assert response.selection_status is (
        CandidateSelectionStatus.WAITING_FOR_CANDIDATE_SELECTION
    )
    assert response.candidate_set_id == "candidate-set-named-synthetic"
    assert response.candidates == candidates
    assert response.intent_hint is NamedJobIntentHint.REQUEST_APPLICATION
    assert store.count == 1
    pending = store.get("candidate-set-named-synthetic", now=NOW)
    assert pending is not None
    assert pending.conversation_id == "conversation-candidates"
    assert pending.candidate_set.candidates == candidates
    assert pending.intent_hint is NamedJobIntentHint.REQUEST_APPLICATION
    assert pending.search_request == search_port.calls[0]
    assert pending.status is (
        CandidateSelectionStatus.WAITING_FOR_CANDIDATE_SELECTION
    )
    assert pending.created_at == NOW


def test_candidate_selection_ttl_is_configurable() -> None:
    store = _candidate_store(ttl=timedelta(minutes=7))
    candidate_set = _search_result(
        (_candidate("1001", title="Backend Engineer"),)
    ).candidate_set
    assert candidate_set is not None
    pending = store.create(
        conversation_id="conversation-ttl",
        candidate_set=candidate_set,
        intent_hint=NamedJobIntentHint.UNSPECIFIED,
        search_request=JobSearchRequest(
            request_id="search-request-named-synthetic",
            company="Acme",
            title="Backend Engineer",
        ),
        created_at=NOW,
    )

    assert pending.expires_at == NOW + timedelta(minutes=7)
    assert store.get(
        pending.candidate_set_id,
        now=NOW + timedelta(minutes=6, seconds=59),
    ) is pending
    assert store.get(
        pending.candidate_set_id,
        now=NOW + timedelta(minutes=7),
    ) is None
    assert store.count == 0


@pytest.mark.parametrize(
    ("result", "expected_status", "reason", "retryable"),
    (
        (
            JobSearchResult.failed(JobSearchReason.SOURCE_TIMEOUT),
            IntakeResponseStatus.FAILED,
            JobSearchReason.SOURCE_TIMEOUT,
            True,
        ),
        (
            JobSearchResult.failed(JobSearchReason.SOURCE_RATE_LIMITED),
            IntakeResponseStatus.FAILED,
            JobSearchReason.SOURCE_RATE_LIMITED,
            True,
        ),
        (
            JobSearchResult.unsupported(),
            IntakeResponseStatus.UNSUPPORTED,
            JobSearchReason.UNSUPPORTED_COMPANY,
            False,
        ),
    ),
)
@pytest.mark.asyncio
async def test_search_failure_preserves_typed_semantics_without_state(
    result: JobSearchResult,
    expected_status: IntakeResponseStatus,
    reason: JobSearchReason,
    retryable: bool,
) -> None:
    store = _candidate_store()
    search_port = FakeJobSearchPort(result)
    response = await handle_conversational_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-failure",
            message="找一下 Acme 的 Backend Engineer",
        ),
        pending_store=_pending_store(),
        candidate_store=store,
        clue_extractor=FakeNamedJobClueExtractor(_clues()),
        job_search_port=search_port,
        reader=_unexpected_reader,
        request_id_factory=lambda: "search-request-named-synthetic",
    )

    assert isinstance(response, NamedJobSearchResponse)
    assert response.status is expected_status
    assert response.reason_code is reason
    assert response.retryable is retryable
    assert response.candidates == ()
    assert response.candidate_set_id is None
    assert store.count == 0
    assert len(search_port.calls) == 1


@pytest.mark.asyncio
async def test_named_search_has_no_discovery_persistence_or_execution_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_home = tmp_path / "synthetic-private-home"
    monkeypatch.setenv("JOBOPS_HOME", str(synthetic_home))
    pending_store = _pending_store()
    candidate_store = _candidate_store()

    response = await handle_conversational_intake(
        ConversationalIntakeRequest(
            conversation_id="conversation-boundary",
            message="申请 Acme 的 Backend Engineer",
        ),
        pending_store=pending_store,
        candidate_store=candidate_store,
        clue_extractor=FakeNamedJobClueExtractor(
            _clues(intent_hint=NamedJobIntentHint.REQUEST_APPLICATION)
        ),
        job_search_port=FakeJobSearchPort(
            _search_result((_candidate("1001", title="Backend Engineer"),))
        ),
        reader=_unexpected_reader,
        clock=lambda: NOW,
        request_id_factory=lambda: "search-request-named-synthetic",
    )

    assert isinstance(response, NamedJobSearchResponse)
    assert response.selection_status is (
        CandidateSelectionStatus.WAITING_FOR_CANDIDATE_SELECTION
    )
    assert pending_store.count == 0
    assert candidate_store.count == 1
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
    assert imported_modules.isdisjoint(
        {
            "adapters",
            "core.private_home",
            "source_connectors.greenhouse_board",
            "utils",
        }
    )
    assert imported_names.isdisjoint(
        {
            "ApplicationPlan",
            "GreenhouseBoardJobSearch",
            "JobPosting",
        }
    )


@pytest.mark.asyncio
async def test_candidate_selection_reads_once_and_creates_existing_pending() -> None:
    candidate_store = _seed_candidate_selection()
    pending_store = _pending_store()
    observation = _observation()
    reader = FakePublicJobReader(ReadJobResult.succeeded(observation))

    response = await select_search_candidate(
        CandidateSelectionRequest(
            conversation_id="conversation-select",
            candidate_set_id="candidate-set-select-synthetic",
            candidate_id="greenhouse:acme:1001",
        ),
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: NOW,
    )

    assert reader.calls == [
        ReadJobRequest(
            url="https://job-boards.greenhouse.io/acme/jobs/1001"
        )
    ]
    assert response.status is IntakeResponseStatus.NEEDS_USER
    assert response.reason_code is None
    assert response.pending_intake_id == "pending-url-synthetic"
    assert response.pending_status is PendingIntakeStatus.WAITING_FOR_ACTION
    assert response.selected_candidate_id == "greenhouse:acme:1001"
    assert response.intent_hint is NamedJobIntentHint.REQUEST_APPLICATION
    assert response.actions == (
        IntakeAction.ADD_JOB,
        IntakeAction.REQUEST_APPLICATION,
    )
    assert response.summary is not None
    assert response.summary.company == "Acme"
    assert response.summary.title == "Backend Engineer"
    assert response.summary.location == "Vancouver, BC"
    assert response.summary.source_platform is SourcePlatform.GREENHOUSE

    pending = pending_store.get("pending-url-synthetic", now=NOW)
    assert pending is not None
    assert pending.status is PendingIntakeStatus.WAITING_FOR_ACTION
    assert pending.observation is observation
    assert pending.conversation_id == "conversation-select"
    assert pending.intent_hint is NamedJobIntentHint.REQUEST_APPLICATION
    assert (
        pending.source_candidate_set_id
        == "candidate-set-select-synthetic"
    )
    assert pending.source_candidate_id == "greenhouse:acme:1001"

    selection = candidate_store.get(
        "candidate-set-select-synthetic",
        now=NOW,
    )
    assert selection is not None
    assert selection.status is CandidateSelectionStatus.COMPLETED
    assert selection.selected_candidate_id == "greenhouse:acme:1001"
    assert selection.pending_intake_id == "pending-url-synthetic"
    assert selection.read_result is not None
    assert selection.read_result.observation is observation


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        (
            "not-found",
            CandidateSelectionReason.CANDIDATE_SET_NOT_FOUND,
        ),
        (
            "conversation-mismatch",
            CandidateSelectionReason.CONVERSATION_MISMATCH,
        ),
        (
            "expired",
            CandidateSelectionReason.CANDIDATE_SET_EXPIRED,
        ),
        (
            "candidate-not-found",
            CandidateSelectionReason.CANDIDATE_NOT_FOUND,
        ),
    ),
)
@pytest.mark.asyncio
async def test_invalid_candidate_selection_never_calls_reader(
    case: str,
    expected_reason: CandidateSelectionReason,
) -> None:
    candidate_store = _seed_candidate_selection()
    conversation_id = "conversation-select"
    candidate_set_id = "candidate-set-select-synthetic"
    candidate_id = "greenhouse:acme:1001"
    now = NOW
    if case == "not-found":
        candidate_set_id = "candidate-set-missing"
    elif case == "conversation-mismatch":
        conversation_id = "conversation-other"
    elif case == "expired":
        now = NOW + timedelta(minutes=15)
    else:
        candidate_id = "greenhouse:acme:missing"
    reader = FakePublicJobReader(
        ReadJobResult.succeeded(_observation())
    )
    pending_store = _pending_store()

    response = await select_search_candidate(
        CandidateSelectionRequest(
            conversation_id=conversation_id,
            candidate_set_id=candidate_set_id,
            candidate_id=candidate_id,
        ),
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: now,
    )

    assert response.status is IntakeResponseStatus.FAILED
    assert response.reason_code is expected_reason
    assert response.pending_intake_id is None
    assert reader.calls == []
    assert pending_store.count == 0


@pytest.mark.asyncio
async def test_invalid_candidate_source_is_rejected_before_reader() -> None:
    invalid_candidate = SearchCandidate(
        candidate_id="candidate-invalid-source",
        company="Acme",
        title="Backend Engineer",
        location=None,
        source_platform=SourcePlatform.GREENHOUSE,
        source_url="not-an-absolute-url",
        source_job_id="1001",
    )
    candidate_store = _seed_candidate_selection(
        candidates=(invalid_candidate,)
    )
    reader = FakePublicJobReader(
        ReadJobResult.succeeded(_observation())
    )

    response = await select_search_candidate(
        CandidateSelectionRequest(
            conversation_id="conversation-select",
            candidate_set_id="candidate-set-select-synthetic",
            candidate_id="candidate-invalid-source",
        ),
        candidate_store=candidate_store,
        pending_store=_pending_store(),
        reader=reader,
        clock=lambda: NOW,
    )

    assert response.reason_code is (
        CandidateSelectionReason.CANDIDATE_SOURCE_INVALID
    )
    assert reader.calls == []
    pending = candidate_store.get(
        "candidate-set-select-synthetic",
        now=NOW,
    )
    assert pending is not None
    assert pending.status is (
        CandidateSelectionStatus.WAITING_FOR_CANDIDATE_SELECTION
    )


@pytest.mark.asyncio
async def test_same_selection_replays_pending_without_second_read() -> None:
    candidate_store = _seed_candidate_selection()
    pending_store = _pending_store()
    reader = FakePublicJobReader(
        ReadJobResult.succeeded(_observation())
    )
    request = CandidateSelectionRequest(
        conversation_id="conversation-select",
        candidate_set_id="candidate-set-select-synthetic",
        candidate_id="greenhouse:acme:1001",
    )

    first = await select_search_candidate(
        request,
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: NOW,
    )
    repeated = await select_search_candidate(
        request,
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: NOW,
    )

    assert len(reader.calls) == 1
    assert repeated == first
    assert repeated.pending_intake_id == "pending-url-synthetic"
    assert pending_store.count == 1


@pytest.mark.asyncio
async def test_different_selection_after_completion_is_conflict() -> None:
    candidate_store = _seed_candidate_selection()
    pending_store = _pending_store()
    reader = FakePublicJobReader(
        ReadJobResult.succeeded(_observation())
    )
    await select_search_candidate(
        CandidateSelectionRequest(
            conversation_id="conversation-select",
            candidate_set_id="candidate-set-select-synthetic",
            candidate_id="greenhouse:acme:1001",
        ),
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: NOW,
    )

    conflict = await select_search_candidate(
        CandidateSelectionRequest(
            conversation_id="conversation-select",
            candidate_set_id="candidate-set-select-synthetic",
            candidate_id="greenhouse:acme:1002",
        ),
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: NOW,
    )

    assert conflict.status is IntakeResponseStatus.FAILED
    assert conflict.reason_code is (
        CandidateSelectionReason.CANDIDATE_SET_ALREADY_RESOLVED
    )
    assert conflict.selected_candidate_id == "greenhouse:acme:1001"
    assert len(reader.calls) == 1
    assert pending_store.count == 1


@pytest.mark.parametrize(
    "failure",
    (
        ReadJobResult.failed(ReadJobReason.SOURCE_TIMEOUT),
        ReadJobResult.failed(ReadJobReason.UNSUPPORTED_URL),
    ),
)
@pytest.mark.asyncio
async def test_typed_reader_failure_releases_selection_without_pending(
    failure: ReadJobResult,
) -> None:
    candidate_store = _seed_candidate_selection()
    pending_store = _pending_store()
    reader = FakePublicJobReader(failure)
    request = CandidateSelectionRequest(
        conversation_id="conversation-select",
        candidate_set_id="candidate-set-select-synthetic",
        candidate_id="greenhouse:acme:1001",
    )

    response = await select_search_candidate(
        request,
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: NOW,
    )

    assert response.reason_code is failure.reason_code
    assert response.retryable is failure.retryable
    assert response.status is (
        IntakeResponseStatus.UNSUPPORTED
        if failure.status is ReadJobStatus.UNSUPPORTED
        else IntakeResponseStatus.FAILED
    )
    assert pending_store.count == 0
    assert len(reader.calls) == 1
    selection = candidate_store.get(
        "candidate-set-select-synthetic",
        now=NOW,
    )
    assert selection is not None
    assert selection.status is (
        CandidateSelectionStatus.WAITING_FOR_CANDIDATE_SELECTION
    )
    assert selection.selected_candidate_id is None

    await select_search_candidate(
        request,
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: NOW,
    )
    assert len(reader.calls) == 2


@pytest.mark.asyncio
async def test_unexpected_reader_error_restores_explicit_retry_state() -> None:
    candidate_store = _seed_candidate_selection()
    pending_store = _pending_store()
    reader = FakePublicJobReader(error=RuntimeError("synthetic outage"))

    response = await select_search_candidate(
        CandidateSelectionRequest(
            conversation_id="conversation-select",
            candidate_set_id="candidate-set-select-synthetic",
            candidate_id="greenhouse:acme:1001",
        ),
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=reader,
        clock=lambda: NOW,
    )

    assert response.status is IntakeResponseStatus.FAILED
    assert response.reason_code is ReadJobReason.SOURCE_UNAVAILABLE
    assert response.retryable is True
    assert response.pending_intake_id is None
    assert pending_store.count == 0
    selection = candidate_store.get(
        "candidate-set-select-synthetic",
        now=NOW,
    )
    assert selection is not None
    assert selection.status is (
        CandidateSelectionStatus.WAITING_FOR_CANDIDATE_SELECTION
    )
    assert selection.selected_candidate_id is None


@pytest.mark.asyncio
async def test_concurrent_selection_claim_prevents_second_read() -> None:
    candidate_store = _seed_candidate_selection()
    pending_store = _pending_store()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[ReadJobRequest] = []

    async def blocking_reader(request: ReadJobRequest) -> ReadJobResult:
        calls.append(request)
        entered.set()
        await release.wait()
        return ReadJobResult.succeeded(_observation())

    first = asyncio.create_task(
        select_search_candidate(
            CandidateSelectionRequest(
                conversation_id="conversation-select",
                candidate_set_id="candidate-set-select-synthetic",
                candidate_id="greenhouse:acme:1001",
            ),
            candidate_store=candidate_store,
            pending_store=pending_store,
            reader=blocking_reader,
            clock=lambda: NOW,
        )
    )
    await entered.wait()
    duplicate = await select_search_candidate(
        CandidateSelectionRequest(
            conversation_id="conversation-select",
            candidate_set_id="candidate-set-select-synthetic",
            candidate_id="greenhouse:acme:1001",
        ),
        candidate_store=candidate_store,
        pending_store=pending_store,
        reader=blocking_reader,
        clock=lambda: NOW,
    )
    release.set()
    completed = await first

    assert duplicate.reason_code is (
        CandidateSelectionReason.CANDIDATE_SET_ALREADY_RESOLVED
    )
    assert completed.pending_intake_id == "pending-url-synthetic"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_candidate_selection_has_no_search_discovery_or_execution_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_home = tmp_path / "synthetic-private-home"
    monkeypatch.setenv("JOBOPS_HOME", str(synthetic_home))
    candidate_store = _seed_candidate_selection(
        intent_hint=NamedJobIntentHint.REQUEST_APPLICATION
    )

    response = await select_search_candidate(
        CandidateSelectionRequest(
            conversation_id="conversation-select",
            candidate_set_id="candidate-set-select-synthetic",
            candidate_id="greenhouse:acme:1001",
        ),
        candidate_store=candidate_store,
        pending_store=_pending_store(),
        reader=FakePublicJobReader(
            ReadJobResult.succeeded(_observation())
        ),
        clock=lambda: NOW,
    )

    assert response.status is IntakeResponseStatus.NEEDS_USER
    assert response.intent_hint is NamedJobIntentHint.REQUEST_APPLICATION
    assert response.actions == (
        IntakeAction.ADD_JOB,
        IntakeAction.REQUEST_APPLICATION,
    )
    assert not synthetic_home.exists()

    source = (
        ROOT / "core" / "conversational_intake.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    selection_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "select_search_candidate"
    )
    called_names = {
        node.func.id
        for node in ast.walk(selection_function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
    }
    assert called_names.isdisjoint(
        {
            "JobIntakeProposal",
            "run_discovery",
            "search_jobs",
        }
    )
