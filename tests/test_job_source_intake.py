from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.job_discovery import (
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryReason,
    JobDiscoveryResponse,
    JobIntakeIntent,
)
from core.job_leads import (
    JobLead,
    JobLeadOrigin,
    JobLeadSource,
    JobLeadStatus,
    PrivateHomeJobLeadRepository,
)
from core.private_home import PrivateHome
from core.search_profile import (
    PrivateHomeSearchProfileRepository,
    SearchProfileSourceKind,
    SearchProfileSourceReference,
)
from core.subject_job_discovery import (
    SubjectJobDiscoveryResult,
    SubjectJobDiscoveryStatus,
)
from core.subject_job_library import SubjectJobMembershipSourceKind
from dashboard.job_source_intake import (
    AssistedDiscoveryPlatform,
    AssistedJobImportCommand,
    AssistedJobImportController,
    CurrentPageJobCaptureCommand,
    ResolveJobLeadCommand,
    SaveSearchProfileUICommand,
    SearchProfileUIController,
)
from source_connectors.contract import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    ReadJobResult,
    ReadJobReason,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)
SOURCE = SearchProfileSourceReference(
    SearchProfileSourceKind.KNOWN_ASHBY_BOARD,
    "example",
)


def _context() -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="synthetic-session-id-0001",
        subject_id="subject-synthetic",
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def test_search_profile_ui_only_accepts_composed_sources(tmp_path) -> None:
    repository = PrivateHomeSearchProfileRepository(PrivateHome(tmp_path))
    controller = SearchProfileUIController(
        repository=repository,
        available_sources=(SOURCE,),
        source_companies={SOURCE: "Example Ashby"},
        clock=lambda: NOW,
    )
    command = SaveSearchProfileUICommand(
        display_name="Example engineering",
        company="Example Ashby",
        title="Software Engineer",
        location="Calgary",
        source=SOURCE,
        enabled=True,
    )

    saved = controller.save(_context(), command)
    read = controller.read(_context())

    assert saved["status"] == "CREATED"
    assert read["available_sources"] == [
        {
            "kind": "KNOWN_ASHBY_BOARD",
            "source_id": "example",
            "canonical_company": "Example Ashby",
        }
    ]
    assert read["profiles"][0]["profile_id"] == saved["profile_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "platform"),
    (
        (
            "https://www.linkedin.com/jobs/view/123",
            AssistedDiscoveryPlatform.LINKEDIN,
        ),
        (
            "https://ca.indeed.com/viewjob?jk=synthetic",
            AssistedDiscoveryPlatform.INDEED,
        ),
        (
            "https://www.glassdoor.ca/job-listing/synthetic-role-JV_IC123.htm",
            AssistedDiscoveryPlatform.GLASSDOOR,
        ),
    ),
)
async def test_aggregator_url_stops_for_local_browser_handoff(
    url: str,
    platform: AssistedDiscoveryPlatform,
) -> None:
    calls = 0

    async def unexpected_reader(request):
        nonlocal calls
        calls += 1
        raise AssertionError("authenticated aggregator pages must not be fetched")

    controller = AssistedJobImportController(
        public_job_reader=unexpected_reader,
        discovery=lambda command: None,
        clock=lambda: NOW,
    )

    result = await controller.import_job(
        _context(),
        AssistedJobImportCommand(
            platform=platform,
            job_url=url,
            invocation_id="assisted-import-synthetic",
        ),
    )

    assert result["status"] == "HUMAN_INTERVENTION_REQUIRED"
    assert result["reason"] == "EMPLOYER_JOB_URL_REQUIRED"
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "platform", "expected_origin"),
    (
        (
            "https://www.linkedin.com/jobs/view/123",
            AssistedDiscoveryPlatform.LINKEDIN,
            "LINKEDIN_SEARCH_INDEX",
        ),
        (
            "https://ca.indeed.com/viewjob?jk=synthetic",
            AssistedDiscoveryPlatform.INDEED,
            "INDEED_SEARCH_INDEX",
        ),
        (
            "https://www.glassdoor.ca/job-listing/synthetic-role-JV_IC123.htm",
            AssistedDiscoveryPlatform.GLASSDOOR,
            "GLASSDOOR_SEARCH_INDEX",
        ),
    ),
)
async def test_aggregator_url_is_persisted_only_as_needs_user_lead(
    tmp_path,
    url: str,
    platform: AssistedDiscoveryPlatform,
    expected_origin: str,
) -> None:
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    reader_calls = 0

    async def unexpected_reader(request):
        nonlocal reader_calls
        reader_calls += 1
        raise AssertionError("platform lead must not be fetched")

    controller = AssistedJobImportController(
        public_job_reader=unexpected_reader,
        discovery=lambda command: (_ for _ in ()).throw(
            AssertionError("unverified lead must not enter discovery")
        ),
        clock=lambda: NOW,
        lead_repository=repository,
    )

    result = await controller.import_job(
        _context(),
        AssistedJobImportCommand(
            platform=platform,
            job_url=url,
            invocation_id="assisted-lead-synthetic",
        ),
    )
    listed = repository.list_current(_context().subject_id)

    assert result["status"] == "HUMAN_INTERVENTION_REQUIRED"
    assert result["lead_status"] == "NEEDS_USER"
    assert reader_calls == 0
    assert len(listed.leads) == 1
    lead = listed.leads[0]
    assert lead.status is JobLeadStatus.NEEDS_USER
    assert lead.source is JobLeadSource.PASTED_URL
    assert lead.origin.value == expected_origin
    assert lead.canonical_url is None


@pytest.mark.asyncio
async def test_resolved_platform_lead_replay_is_unchanged_without_page_read(
    tmp_path,
) -> None:
    platform_url = "https://www.linkedin.com/jobs/view/1234567890"
    official_url = "https://jobs.ashbyhq.com/example/synthetic-posting"
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    discovered = JobLead.discover(
        subject_id=_context().subject_id,
        source=JobLeadSource.PASTED_URL,
        origin=JobLeadOrigin.LINKEDIN_SEARCH_INDEX,
        source_url=platform_url,
        discovered_at=NOW,
        confidence=0.55,
    )
    repository.save(discovered)
    needs_user = discovered.transition(
        JobLeadStatus.NEEDS_USER,
        now=NOW,
        reason="EMPLOYER_JOB_URL_REQUIRED",
    )
    repository.save(needs_user)
    resolved = needs_user.transition(
        JobLeadStatus.RESOLVED,
        now=NOW,
        canonical_url=official_url,
    )
    repository.save(resolved)
    reader_calls = 0

    async def unexpected_reader(request):
        nonlocal reader_calls
        reader_calls += 1
        raise AssertionError("resolved platform replay must not read the page")

    controller = AssistedJobImportController(
        public_job_reader=unexpected_reader,
        discovery=lambda command: (_ for _ in ()).throw(
            AssertionError("resolved platform replay must not rediscover")
        ),
        clock=lambda: NOW,
        lead_repository=repository,
    )

    result = await controller.import_job(
        _context(),
        AssistedJobImportCommand(
            platform=AssistedDiscoveryPlatform.LINKEDIN,
            job_url=platform_url,
            invocation_id="resolved-platform-replay-synthetic",
        ),
    )

    assert result["status"] == "UNCHANGED"
    assert result["lead_id"] == resolved.lead_id
    assert result["lead_status"] == "RESOLVED"
    assert result["canonical_url"] == official_url
    assert reader_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_url", "page_title", "expected_origin"),
    (
        (
            "https://www.linkedin.com/jobs/view/123#details",
            "Synthetic Backend Engineer | LinkedIn",
            JobLeadOrigin.LINKEDIN_SEARCH_INDEX,
        ),
        (
            "https://www.glassdoor.ca/job-listing/synthetic-role-JV_IC123.htm",
            "Synthetic Backend Engineer | Glassdoor",
            JobLeadOrigin.GLASSDOOR_SEARCH_INDEX,
        ),
    ),
)
async def test_current_page_capture_is_user_gesture_bounded_and_idempotent(
    tmp_path,
    page_url: str,
    page_title: str,
    expected_origin: JobLeadOrigin,
) -> None:
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    reader_calls = 0

    async def unexpected_reader(request):
        nonlocal reader_calls
        reader_calls += 1
        raise AssertionError("current platform page must not be fetched")

    controller = AssistedJobImportController(
        public_job_reader=unexpected_reader,
        discovery=lambda command: (_ for _ in ()).throw(
            AssertionError("unverified lead must not enter discovery")
        ),
        clock=lambda: NOW,
        lead_repository=repository,
    )
    command = CurrentPageJobCaptureCommand(
        page_url=page_url,
        page_title=page_title,
        selected_text="Synthetic public job summary.",
        invocation_id="clipper-synthetic",
        user_gesture=True,
    )

    first = await controller.capture_current_page(_context(), command)
    second = await controller.capture_current_page(_context(), command)
    listed = repository.list_current(_context().subject_id)

    assert first["status"] == "HUMAN_INTERVENTION_REQUIRED"
    assert first["lead_status"] == "NEEDS_USER"
    assert second["lead_id"] == first["lead_id"]
    assert reader_calls == 0
    assert len(listed.leads) == 1
    lead = listed.leads[0]
    assert lead.lead_version == 2
    assert lead.status is JobLeadStatus.NEEDS_USER
    assert lead.source is JobLeadSource.WEB_CLIPPER
    assert lead.origin is expected_origin
    assert lead.title_hint == page_title
    assert lead.snippet_hint == "Synthetic public job summary."


def test_current_page_capture_rejects_missing_user_gesture() -> None:
    with pytest.raises(ValueError, match="user gesture"):
        CurrentPageJobCaptureCommand(
            page_url="https://jobs.example.invalid/job/123",
            page_title="Synthetic job",
            invocation_id="clipper-without-gesture",
            user_gesture=False,
        )


@pytest.mark.asyncio
async def test_current_public_ats_page_enters_formal_discovery_and_resolves_lead(
    tmp_path,
) -> None:
    entry_url = "https://careers.example.test/jobs/software-engineer"
    url = "https://jobs.ashbyhq.com/example/synthetic-posting"
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    observation = SourceJobObservation(
        source_platform=SourcePlatform.ASHBY,
        source_job_id="synthetic-posting",
        source_url=url,
        application_url=f"{url}/application",
        company="Example Ashby",
        title="Software Engineer",
        description="Build reliable systems.",
        location="Calgary",
        work_mode=WorkMode.HYBRID,
        posted_at=None,
        ats_type=AtsType.ASHBY,
        observed_at=NOW.isoformat(),
        provenance=(
            FieldProvenance(
                "description",
                ProvenanceSource.SOURCE_API,
                "descriptionPlain",
            ),
        ),
    )
    read_requests = []
    discovery_calls = []

    def reader(request):
        read_requests.append(request)
        if len(read_requests) == 1:
            return ReadJobResult.failed(ReadJobReason.SOURCE_UNAVAILABLE)
        return ReadJobResult.succeeded(observation)

    def discovery(command):
        discovery_calls.append(command)
        return SubjectJobDiscoveryResult(
            SubjectJobDiscoveryStatus.ACCEPTED,
            JobDiscoveryResponse(
                disposition=DiscoveryDisposition.ACCEPTED,
                original_intent=JobIntakeIntent.ADD_JOB,
                reason_code=DiscoveryReason.JOB_CREATED,
                run_id="clipper-discovery-synthetic",
                run_hash="b" * 64,
                job_id="job-clipper-synthetic",
                change=DiscoveryChange.CREATED,
            ),
            None,
            None,
        )

    controller = AssistedJobImportController(
        public_job_reader=reader,
        discovery=discovery,
        clock=lambda: NOW,
        lead_repository=repository,
    )

    first = await controller.capture_current_page(
        _context(),
        CurrentPageJobCaptureCommand(
            page_url=entry_url,
            page_title="Software Engineer | Example Ashby",
            selected_text=None,
            invocation_id="clipper-official-synthetic",
            user_gesture=True,
        ),
    )
    result = await controller.capture_current_page(
        _context(),
        CurrentPageJobCaptureCommand(
            page_url=entry_url,
            page_title="Software Engineer | Example Ashby",
            selected_text=None,
            invocation_id="clipper-official-retry-synthetic",
            user_gesture=True,
        ),
    )
    listed = repository.list_current(_context().subject_id)

    assert first["status"] == "HUMAN_INTERVENTION_REQUIRED"
    assert first["lead_status"] == "NEEDS_USER"
    assert result["status"] == "IMPORTED"
    assert result["job_id"] == "job-clipper-synthetic"
    assert result["lead_status"] == "RESOLVED"
    assert len(read_requests) == 2
    assert all(request.url == entry_url for request in read_requests)
    assert len(discovery_calls) == 1
    assert discovery_calls[0].subject_id == _context().subject_id
    assert len(listed.leads) == 1
    lead = listed.leads[0]
    assert lead.source is JobLeadSource.WEB_CLIPPER
    assert lead.status is JobLeadStatus.RESOLVED
    assert lead.canonical_url == url
    assert discovery_calls[0].source_ref == lead.lead_id


@pytest.mark.asyncio
async def test_final_public_ats_url_enters_formal_subject_discovery(
    tmp_path,
) -> None:
    url = "https://jobs.ashbyhq.com/example/synthetic-posting"
    observation = SourceJobObservation(
        source_platform=SourcePlatform.ASHBY,
        source_job_id="synthetic-posting",
        source_url=url,
        application_url=f"{url}/application",
        company="Example Ashby",
        title="Software Engineer",
        description="Build reliable systems.",
        location="Calgary",
        work_mode=WorkMode.HYBRID,
        posted_at=None,
        ats_type=AtsType.ASHBY,
        observed_at=NOW.isoformat(),
        provenance=(
            FieldProvenance(
                "description",
                ProvenanceSource.SOURCE_API,
                "descriptionPlain",
            ),
        ),
    )
    discovery_calls = []

    def discovery(command):
        discovery_calls.append(command)
        return SubjectJobDiscoveryResult(
            SubjectJobDiscoveryStatus.ACCEPTED,
            JobDiscoveryResponse(
                disposition=DiscoveryDisposition.ACCEPTED,
                original_intent=JobIntakeIntent.ADD_JOB,
                reason_code=DiscoveryReason.JOB_CREATED,
                run_id="discovery-synthetic",
                run_hash="a" * 64,
                job_id="job-synthetic",
                change=DiscoveryChange.CREATED,
            ),
            None,
            None,
        )

    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    controller = AssistedJobImportController(
        public_job_reader=lambda request: ReadJobResult.succeeded(observation),
        discovery=discovery,
        clock=lambda: NOW,
        lead_repository=repository,
    )

    result = await controller.import_job(
        _context(),
        AssistedJobImportCommand(
            platform=AssistedDiscoveryPlatform.LINKEDIN,
            job_url=url,
            invocation_id="assisted-import-synthetic",
        ),
    )

    assert result["status"] == "IMPORTED"
    assert result["reason"] is None
    assert result["job_id"] == "job-synthetic"
    assert result["canonical_url"] == url
    assert result["lead_status"] == "RESOLVED"
    assert discovery_calls[0].subject_id == "subject-synthetic"
    assert discovery_calls[0].source_kind is (
        SubjectJobMembershipSourceKind.JOB_LEAD_RESOLUTION
    )
    assert discovery_calls[0].source_ref == result["lead_id"]
    persisted = repository.list_current(_context().subject_id).leads
    assert len(persisted) == 1
    assert persisted[0].source is JobLeadSource.PASTED_URL
    assert persisted[0].status is JobLeadStatus.RESOLVED
    assert persisted[0].canonical_url == url
    candidate = discovery_calls[0].request.proposal.resolved_candidate
    assert candidate.ats_type == "ASHBY"
    assert candidate.source_platform == "ASHBY"


@pytest.mark.asyncio
async def test_needs_user_lead_accepts_one_verified_official_url(
    tmp_path,
) -> None:
    source_url = "https://www.linkedin.com/jobs/view/1234567890"
    official_url = "https://jobs.ashbyhq.com/example/synthetic-posting"
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    discovered = JobLead.discover(
        subject_id=_context().subject_id,
        source=JobLeadSource.AUTHORIZED_WEB_SEARCH,
        origin=JobLeadOrigin.LINKEDIN_SEARCH_INDEX,
        source_url=source_url,
        discovered_at=NOW,
        confidence=0.7,
        title_hint="Software Engineer",
        company_hint="Example Ashby",
        query_id="query-synthetic",
    )
    assert repository.save(discovered).status.value == "CREATED"
    needs_user = discovered.transition(
        JobLeadStatus.NEEDS_USER,
        now=NOW,
        reason="EMPLOYER_JOB_URL_REQUIRED",
    )
    assert repository.save(needs_user).status.value == "CREATED"
    observation = SourceJobObservation(
        source_platform=SourcePlatform.ASHBY,
        source_job_id="synthetic-posting",
        source_url=official_url,
        application_url=f"{official_url}/application",
        company="Example Ashby",
        title="Software Engineer",
        description="Build reliable systems.",
        location="Calgary",
        work_mode=WorkMode.HYBRID,
        posted_at=None,
        ats_type=AtsType.ASHBY,
        observed_at=NOW.isoformat(),
        provenance=(
            FieldProvenance(
                "description",
                ProvenanceSource.SOURCE_API,
                "descriptionPlain",
            ),
        ),
    )
    discovery_calls = []

    def discovery(command):
        discovery_calls.append(command)
        return SubjectJobDiscoveryResult(
            SubjectJobDiscoveryStatus.ACCEPTED,
            JobDiscoveryResponse(
                disposition=DiscoveryDisposition.ACCEPTED,
                original_intent=JobIntakeIntent.ADD_JOB,
                reason_code=DiscoveryReason.JOB_CREATED,
                run_id="lead-resolution-synthetic",
                run_hash="c" * 64,
                job_id="job-resolved-synthetic",
                change=DiscoveryChange.CREATED,
            ),
            None,
            None,
        )

    controller = AssistedJobImportController(
        public_job_reader=lambda request: ReadJobResult.succeeded(observation),
        discovery=discovery,
        clock=lambda: NOW,
        lead_repository=repository,
    )

    result = await controller.resolve_lead(
        _context(),
        ResolveJobLeadCommand(
            lead_id=needs_user.lead_id,
            official_job_url=official_url,
            invocation_id="resolve-lead-synthetic",
        ),
    )
    current = repository.get(_context().subject_id, needs_user.lead_id)

    assert result["status"] == "IMPORTED"
    assert result["lead_status"] == "RESOLVED"
    assert result["canonical_url"] == official_url
    assert current.lead is not None
    assert current.lead.status is JobLeadStatus.RESOLVED
    assert current.lead.canonical_url == official_url
    assert len(discovery_calls) == 1
    assert discovery_calls[0].source_kind is (
        SubjectJobMembershipSourceKind.JOB_LEAD_RESOLUTION
    )
    assert discovery_calls[0].source_ref == needs_user.lead_id


@pytest.mark.asyncio
async def test_job_lead_resolution_is_subject_scoped_and_never_fetches_platform(
    tmp_path,
) -> None:
    repository = PrivateHomeJobLeadRepository(PrivateHome(tmp_path))
    discovered = JobLead.discover(
        subject_id=_context().subject_id,
        source=JobLeadSource.PASTED_URL,
        origin=JobLeadOrigin.INDEED_SEARCH_INDEX,
        source_url="https://ca.indeed.com/viewjob?jk=synthetic",
        discovered_at=NOW,
        confidence=0.5,
    )
    repository.save(discovered)
    needs_user = discovered.transition(
        JobLeadStatus.NEEDS_USER,
        now=NOW,
        reason="EMPLOYER_JOB_URL_REQUIRED",
    )
    repository.save(needs_user)
    calls = 0

    async def unexpected_reader(request):
        nonlocal calls
        calls += 1
        raise AssertionError("platform pages must never be fetched")

    controller = AssistedJobImportController(
        public_job_reader=unexpected_reader,
        discovery=lambda command: None,
        clock=lambda: NOW,
        lead_repository=repository,
    )
    other_subject = AuthenticatedSubjectContext(
        session_id="synthetic-session-id-0002",
        subject_id="subject-other-synthetic",
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )

    cross_subject = await controller.resolve_lead(
        other_subject,
        ResolveJobLeadCommand(
            lead_id=needs_user.lead_id,
            official_job_url="https://jobs.ashbyhq.com/example/job",
            invocation_id="cross-subject-synthetic",
        ),
    )
    platform_target = await controller.resolve_lead(
        _context(),
        ResolveJobLeadCommand(
            lead_id=needs_user.lead_id,
            official_job_url="https://www.linkedin.com./jobs/view/1234567890",
            invocation_id="platform-target-synthetic",
        ),
    )

    assert cross_subject["status"] == "FAILED"
    assert cross_subject["reason"] == "JOB_LEAD_NOT_FOUND"
    assert platform_target["status"] == "HUMAN_INTERVENTION_REQUIRED"
    assert platform_target["reason"] == "EMPLOYER_JOB_URL_REQUIRED"
    assert platform_target["lead_status"] == "NEEDS_USER"
    assert calls == 0
