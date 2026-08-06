"""Sanitized Dashboard and isolated-NLP tests for conversational job finding."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from core.accepted_job_intent import (
    AcceptedJobIntent,
    AcceptedJobIntentReadResult,
    AcceptedJobIntentReadStatus,
    AcceptedJobIntentWriteResult,
    AcceptedJobIntentWriteStatus,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.conversational_intake import (
    InMemoryCandidateSelectionStore,
    InMemoryPendingIntakeStore,
    NamedJobClues,
    NamedJobIntentHint,
)
from core.job_search import (
    CandidateSet,
    JobSearchRequest,
    JobSearchResult,
    SearchCandidate,
)
from core.job_leads import (
    JobLeadSource,
    JobLeadStatus,
    PrivateHomeJobLeadRepository,
)
from core.private_home import PrivateHome
from core.job_discovery import (
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryReason,
    JobDiscoveryResponse,
)
from core.model_provider_capabilities import MODEL_EXECUTION_ISOLATION_PROFILES
from core.production_named_job_clue_extractor import (
    NAMED_JOB_CLUE_COMPONENT_ID,
    NAMED_JOB_CLUE_OUTPUT_SCHEMA,
    StructuredBackendNamedJobClueExtractor,
    build_production_named_job_clue_extractor,
)
from dashboard.conversational_job_finder import (
    ConversationalJobFinderUIController,
)
from dashboard.job_source_intake import AssistedJobImportController
from dashboard.server import conversational_job_finder_resolve_ui
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
    ReadJobResult,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)
from utils import llm


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)
SUBJECT = "subject-job-finder-synthetic"


class FakeStructuredBackend:
    capabilities = llm.OpenAIAPIBackend.capabilities
    native_capabilities = llm.OpenAIAPIBackend.native_capabilities
    response = {
        "company": "Acme",
        "title": "Backend Engineer",
        "location": "Calgary",
        "intent_hint": "ADD_JOB",
        "missing_fields": [],
    }
    calls = []

    def __init__(self, config):
        self.model = config.get("model", "")

    async def complete_structured_request(self, request):
        type(self).calls.append(request)
        return copy.deepcopy(type(self).response)


@pytest.mark.asyncio
async def test_production_clue_extractor_is_one_tool_free_bounded_call() -> None:
    FakeStructuredBackend.calls = []
    extractor = build_production_named_job_clue_extractor(
        ai_config={
            "default_backend": "openai_api",
            "backends": {"openai_api": {"model": "synthetic-model"}},
            "components": {"priority_evaluation": "openai_api"},
        },
        backend_registry={"openai_api": FakeStructuredBackend},
        isolation_profile_registry=MODEL_EXECUTION_ISOLATION_PROFILES,
    )

    clues = await extractor.extract(
        "User turn 1: Add the Acme Backend Engineer role in Calgary"
    )

    assert isinstance(extractor, StructuredBackendNamedJobClueExtractor)
    assert clues == NamedJobClues(
        company="Acme",
        title="Backend Engineer",
        location="Calgary",
        intent_hint=NamedJobIntentHint.ADD_JOB,
        missing_fields=(),
    )
    assert len(FakeStructuredBackend.calls) == 1
    request = FakeStructuredBackend.calls[0]
    assert request.component_id == NAMED_JOB_CLUE_COMPONENT_ID
    assert request.output_schema == NAMED_JOB_CLUE_OUTPUT_SCHEMA
    assert request.images == ()
    assert "subject" not in repr(request.input_data).casefold()


class ClarifyingClueExtractor:
    async def extract(self, message: str) -> NamedJobClues:
        if "User turn 2" not in message:
            return NamedJobClues(
                company=None,
                title="Backend Engineer",
                location=None,
                intent_hint=NamedJobIntentHint.UNSPECIFIED,
                missing_fields=("company",),
            )
        return NamedJobClues(
            company="Acme",
            title="Backend Engineer",
            location="Calgary",
            intent_hint=NamedJobIntentHint.ADD_JOB,
            missing_fields=(),
        )


class AlwaysAmbiguousClueExtractor:
    async def extract(self, message: str) -> NamedJobClues:
        return NamedJobClues(
            company=None,
            title="Backend Engineer",
            location=None,
            intent_hint=NamedJobIntentHint.UNSPECIFIED,
            missing_fields=("company",),
        )


class CandidateSearchPort:
    def __init__(self) -> None:
        self.calls: list[JobSearchRequest] = []

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        self.calls.append(request)
        return JobSearchResult.succeeded(
            CandidateSet(
                candidate_set_id=f"candidate-set-{request.request_id}",
                request_id=request.request_id,
                candidates=(
                    SearchCandidate(
                        candidate_id="greenhouse:acme:1001",
                        company="Acme",
                        title="Backend Engineer",
                        location="Calgary",
                        source_platform=SourcePlatform.GREENHOUSE,
                        source_url=(
                            "https://job-boards.greenhouse.io/acme/jobs/1001"
                        ),
                        source_job_id="1001",
                    ),
                ),
                created_at=NOW,
            )
        )


class UnusedAcceptedIntentRepository:
    def save(self, intent):
        raise AssertionError("search must not persist accepted intent")

    def get_current(self, *, subject_id: str, job_id: str):
        raise AssertionError("search must not read accepted intent")


async def _unused_reader(request):
    raise AssertionError("named search must not read a job before selection")


class AcceptedIntentRepository:
    def __init__(self) -> None:
        self.saved: list[AcceptedJobIntent] = []

    def save(self, intent: AcceptedJobIntent) -> AcceptedJobIntentWriteResult:
        self.saved.append(intent)
        return AcceptedJobIntentWriteResult(
            status=AcceptedJobIntentWriteStatus.CREATED,
            intent=intent,
            reason_code=None,
            retryable=False,
        )

    def get_current(
        self, *, subject_id: str, job_id: str
    ) -> AcceptedJobIntentReadResult:
        matches = [
            item
            for item in self.saved
            if item.subject_id == subject_id and item.job_id == job_id
        ]
        return AcceptedJobIntentReadResult(
            status=(
                AcceptedJobIntentReadStatus.FOUND
                if matches
                else AcceptedJobIntentReadStatus.NOT_FOUND
            ),
            intent=matches[-1] if matches else None,
        )


class AcceptingDiscovery:
    def __init__(self) -> None:
        self.calls: list[SubjectJobDiscoveryCommand] = []

    def __call__(
        self, command: SubjectJobDiscoveryCommand
    ) -> SubjectJobDiscoveryResult:
        self.calls.append(command)
        return SubjectJobDiscoveryResult(
            status=SubjectJobDiscoveryStatus.ACCEPTED,
            discovery_response=JobDiscoveryResponse(
                disposition=DiscoveryDisposition.ACCEPTED,
                original_intent=command.request.proposal.intent,
                reason_code=DiscoveryReason.JOB_CREATED,
                run_id="discovery-run-job-finder-synthetic",
                job_id="job-job-finder-synthetic",
                change=DiscoveryChange.CREATED,
            ),
            membership=object(),
            membership_status=RegisterSubjectJobMembershipStatus.CREATED,
        )


def _observation() -> SourceJobObservation:
    return SourceJobObservation(
        source_platform=SourcePlatform.GREENHOUSE,
        source_job_id="1001",
        source_url="https://job-boards.greenhouse.io/acme/jobs/1001",
        application_url=None,
        company="Acme",
        title="Backend Engineer",
        description="Build deterministic backend systems.",
        location="Calgary",
        work_mode=WorkMode.UNKNOWN,
        posted_at=None,
        ats_type=AtsType.GREENHOUSE,
        observed_at="2026-08-04T18:00:00Z",
        provenance=(
            FieldProvenance(
                "source_url", ProvenanceSource.REQUEST, "url"
            ),
        ),
    )


def _context(subject_id: str = SUBJECT) -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="session-job-finder-synthetic-12345",
        subject_id=subject_id,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def _controller(clue_extractor) -> ConversationalJobFinderUIController:
    return ConversationalJobFinderUIController(
        pending_store=InMemoryPendingIntakeStore(ttl=timedelta(minutes=15)),
        candidate_store=InMemoryCandidateSelectionStore(
            ttl=timedelta(minutes=15)
        ),
        clue_extractor=clue_extractor,
        job_search_port=CandidateSearchPort(),
        public_job_reader=_unused_reader,
        accepted_intent_repository=UnusedAcceptedIntentRepository(),
        discovery=lambda command: (_ for _ in ()).throw(
            AssertionError("search must not call discovery")
        ),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "expected_source_platform"),
    (
        (
            "https://www.linkedin.com/jobs/view/123",
            "LINKEDIN_SEARCH_INDEX",
        ),
        (
            "https://ca.indeed.com/viewjob?jk=synthetic",
            "INDEED_SEARCH_INDEX",
        ),
        (
            "https://www.glassdoor.ca/job-listing/synthetic-role-JV_IC123.htm",
            "GLASSDOOR_SEARCH_INDEX",
        ),
    ),
)
async def test_exact_platform_url_becomes_unverified_lead_without_page_read(
    tmp_path,
    url: str,
    expected_source_platform: str,
) -> None:
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    search_port = CandidateSearchPort()

    class UnexpectedClueExtractor:
        async def extract(self, message: str):
            raise AssertionError("an exact platform URL must not invoke AI")

    async def unexpected_reader(request):
        raise AssertionError("a platform page must not be read")

    def unexpected_discovery(command):
        raise AssertionError("an unverified lead must not enter discovery")

    assisted = AssistedJobImportController(
        public_job_reader=unexpected_reader,
        discovery=unexpected_discovery,
        clock=lambda: NOW,
        lead_repository=repository,
    )
    controller = ConversationalJobFinderUIController(
        pending_store=InMemoryPendingIntakeStore(ttl=timedelta(minutes=15)),
        candidate_store=InMemoryCandidateSelectionStore(
            ttl=timedelta(minutes=15)
        ),
        clue_extractor=UnexpectedClueExtractor(),
        job_search_port=search_port,
        public_job_reader=unexpected_reader,
        accepted_intent_repository=UnusedAcceptedIntentRepository(),
        discovery=unexpected_discovery,
        clock=lambda: NOW,
        assisted_import=assisted,
    )

    response = await controller.message(
        _context(),
        conversation_id="platform-url-synthetic",
        messages=(url,),
    )
    listed = repository.list_current(SUBJECT)

    assert response["kind"] == "LEAD"
    assert response["status"] == "NEEDS_USER"
    assert response["reason"] == "EMPLOYER_JOB_URL_REQUIRED"
    assert response["lead_status"] == "NEEDS_USER"
    assert search_port.calls == []
    assert len(listed.leads) == 1
    lead = listed.leads[0]
    assert lead.source is JobLeadSource.PASTED_URL
    assert lead.origin.value == expected_source_platform
    assert lead.status is JobLeadStatus.NEEDS_USER
    assert lead.canonical_url is None


@pytest.mark.asyncio
async def test_dashboard_allows_exactly_one_clarification_then_candidates() -> None:
    controller = _controller(ClarifyingClueExtractor())

    first = await controller.message(
        _context(),
        conversation_id="browser-conversation-synthetic",
        messages=("Backend engineer",),
    )
    second = await controller.message(
        _context(),
        conversation_id="browser-conversation-synthetic",
        messages=("Backend engineer", "Acme in Calgary"),
    )

    assert first["status"] == "NEEDS_USER"
    assert first["missing_fields"] == ["company"]
    assert second["candidate_set_id"] is not None
    assert second["candidates"] == [
        {
            "candidate_id": "greenhouse:acme:1001",
            "company": "Acme",
            "title": "Backend Engineer",
            "location": "Calgary",
            "source_platform": "GREENHOUSE",
            "source_url": (
                "https://job-boards.greenhouse.io/acme/jobs/1001"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_dashboard_closes_after_one_unresolved_clarification() -> None:
    controller = _controller(AlwaysAmbiguousClueExtractor())

    result = await controller.message(
        _context(),
        conversation_id="browser-conversation-ambiguous",
        messages=("Engineering role", "A technology company"),
    )

    assert result["status"] == "FAILED"
    assert result["reason"] == "CLARIFICATION_LIMIT_REACHED"
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_selected_candidate_needs_explicit_add_and_uses_session_subject() -> None:
    accepted = AcceptedIntentRepository()
    discovery = AcceptingDiscovery()

    async def reader(request):
        return ReadJobResult.succeeded(_observation())

    controller = ConversationalJobFinderUIController(
        pending_store=InMemoryPendingIntakeStore(ttl=timedelta(minutes=15)),
        candidate_store=InMemoryCandidateSelectionStore(
            ttl=timedelta(minutes=15)
        ),
        clue_extractor=ClarifyingClueExtractor(),
        job_search_port=CandidateSearchPort(),
        public_job_reader=reader,
        accepted_intent_repository=accepted,
        discovery=discovery,
        clock=lambda: NOW,
    )
    context = _context()
    searched = await controller.message(
        context,
        conversation_id="browser-conversation-add",
        messages=("Backend engineer", "Acme in Calgary"),
    )
    selected = await controller.select_candidate(
        context,
        conversation_id="browser-conversation-add",
        candidate_set_id=searched["candidate_set_id"],
        candidate_id=searched["candidates"][0]["candidate_id"],
    )

    assert selected["status"] == "NEEDS_USER"
    assert selected["actions"] == ["ADD_JOB", "REQUEST_APPLICATION"]
    assert discovery.calls == []
    local_app = FastAPI()
    local_app.state.conversational_job_finder_controller = controller
    request = Request(
        {
            "type": "http",
            "app": local_app,
            "method": "POST",
            "path": "/api/job-finder/resolve",
            "query_string": b"subject_id=attacker",
            "headers": [(b"x-subject-id", b"attacker")],
        }
    )
    resolved = await conversational_job_finder_resolve_ui(
        {
            "subject_id": "attacker",
            "conversation_id": "browser-conversation-add",
            "pending_intake_id": selected["pending_intake_id"],
            "action": "ADD_JOB",
        },
        request,
        context,
    )

    assert resolved["status"] == "COMPLETED"
    assert resolved["change"] == "CREATED"
    assert len(discovery.calls) == 1
    assert discovery.calls[0].subject_id == SUBJECT
    assert accepted.saved[0].subject_id == SUBJECT
    assert "browser-conversation-add" not in (
        discovery.calls[0].source_ref
    )
