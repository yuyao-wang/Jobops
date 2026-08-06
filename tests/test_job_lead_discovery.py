"""Sanitized vertical tests for authorized multi-source JobLead discovery."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from core.job_discovery import (
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryReason,
    JobDiscoveryResponse,
    JobIntakeIntent,
)
from core.job_lead_discovery import (
    JobLeadDiscoveryCommand,
    JobLeadDiscoveryPhase,
    JobLeadDiscoveryReason,
    JobLeadDiscoverySource,
    JobLeadDiscoveryStatus,
    JobLeadSourceRunStatus,
    build_authorized_web_search_requests,
    classify_job_lead_origin,
    discover_job_leads,
    resolve_persisted_job_leads,
)
from core.job_leads import (
    JobLead,
    JobLeadListResult,
    JobLeadListStatus,
    JobLeadOrigin,
    JobLeadReadResult,
    JobLeadReadStatus,
    JobLeadSource,
    JobLeadStatus,
    JobLeadWriteResult,
    JobLeadWriteStatus,
)
from core.job_search import (
    CandidateSet,
    JobSearchResult,
    SearchCandidate,
)
from core.prioritization_policy import (
    PreferenceImportance,
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
    SoftPreference,
    SoftPreferenceCategory,
    default_preparation_admission_policy,
    policy_content_hash,
)
from core.subject_job_discovery import (
    SubjectJobDiscoveryResult,
    SubjectJobDiscoveryStatus,
)
from core.subject_job_library import SubjectJobMembershipSourceKind
from source_connectors.authorized_web_search import (
    AuthorizedWebSearchHit,
    AuthorizedWebSearchResult,
)
from source_connectors.contract import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    ReadJobReason,
    ReadJobResult,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)
SUBJECT = "subject:synthetic-lead-discovery"


def _policy(*, roles: tuple[str, ...] = ("Machine Learning Engineer",)):
    preferences = tuple(
        SoftPreference(
            preference_id=f"role-{index}",
            category=SoftPreferenceCategory.ROLE,
            statement=role,
            source_excerpt=role,
            importance=PreferenceImportance.HIGH,
        )
        for index, role in enumerate(roles)
    ) + (
        SoftPreference(
            preference_id="location-vancouver",
            category=SoftPreferenceCategory.LOCATION,
            statement="Vancouver or Remote Canada",
            source_excerpt="Vancouver or Remote Canada",
        ),
        SoftPreference(
            preference_id="freshness-seven-days",
            category=SoftPreferenceCategory.FRESHNESS,
            statement="Posted within 7 days",
            source_excerpt="within 7 days",
        ),
    )
    raw = "Synthetic search preferences"
    admission = default_preparation_admission_policy()
    return PrioritizationPolicy(
        policy_id="policy-synthetic-lead-discovery",
        subject_id=SUBJECT,
        policy_version=1,
        policy_content_hash=policy_content_hash(
            raw_preference_text=raw,
            hard_constraints=(),
            soft_preferences=preferences,
            preparation_admission=admission,
        ),
        raw_preference_text=raw,
        hard_constraints=(),
        soft_preferences=preferences,
        preparation_admission=admission,
        status=PrioritizationPolicyStatus.ACTIVE,
        created_at=NOW,
        approved_at=NOW,
        interpreter_version="synthetic-v1",
    )


def _command(**changes: object) -> JobLeadDiscoveryCommand:
    values: dict[str, object] = {
        "subject_id": SUBJECT,
        "invocation_id": "lead-discovery-run-001",
        "now": NOW,
        "count": 20,
        "offsets": (0,),
        "max_requests": 100,
    }
    values.update(changes)
    return JobLeadDiscoveryCommand(**values)  # type: ignore[arg-type]


class InMemoryLeads:
    def __init__(self) -> None:
        self.current: dict[str, JobLead] = {}

    def save(self, lead: JobLead) -> JobLeadWriteResult:
        existing = self.current.get(lead.lead_id)
        if existing == lead:
            return JobLeadWriteResult(JobLeadWriteStatus.UNCHANGED, lead)
        if existing is not None and not lead.is_direct_successor_of(existing):
            return JobLeadWriteResult(JobLeadWriteStatus.CONFLICT, None)
        self.current[lead.lead_id] = lead
        return JobLeadWriteResult(JobLeadWriteStatus.CREATED, lead)

    def get(self, subject_id: str, lead_id: str) -> JobLeadReadResult:
        lead = self.current.get(lead_id)
        if lead is None or lead.subject_id != subject_id:
            return JobLeadReadResult(JobLeadReadStatus.NOT_FOUND, None)
        return JobLeadReadResult(JobLeadReadStatus.FOUND, lead)

    def list_current(self, subject_id: str) -> JobLeadListResult:
        return JobLeadListResult(
            JobLeadListStatus.SUCCEEDED,
            tuple(
                sorted(
                    (
                        lead
                        for lead in self.current.values()
                        if lead.subject_id == subject_id
                    ),
                    key=lambda lead: lead.lead_id,
                )
            ),
        )


class FakeWebSearch:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests = []

    async def search(self, request):
        self.requests.append(request)
        return self.handler(request)


class FakeConfiguredSearch:
    def __init__(self, result: JobSearchResult) -> None:
        self.result = result
        self.requests = []

    async def search(self, request):
        self.requests.append(request)
        return self.result


class FakeReader:
    def __init__(self, observations: dict[str, SourceJobObservation]) -> None:
        self.observations = observations
        self.calls: list[str] = []

    async def __call__(self, request):
        self.calls.append(request.url)
        observation = self.observations.get(request.url)
        if observation is None:
            return ReadJobResult.failed(ReadJobReason.JOB_NOT_FOUND)
        return ReadJobResult.succeeded(observation)


class AcceptingDiscovery:
    def __init__(self) -> None:
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        job_id = f"job-synthetic-{len(self.commands)}"
        response = JobDiscoveryResponse(
            disposition=DiscoveryDisposition.ACCEPTED,
            original_intent=JobIntakeIntent.ADD_JOB,
            reason_code=DiscoveryReason.JOB_CREATED,
            run_id=f"run-{len(self.commands)}",
            job_id=job_id,
            change=DiscoveryChange.CREATED,
            run_hash="a" * 64,
        )
        return SubjectJobDiscoveryResult(
            status=SubjectJobDiscoveryStatus.ACCEPTED,
            discovery_response=response,
            membership=None,
            membership_status=None,
        )


def _observation(
    url: str,
    *,
    company: str = "Example Robotics",
    title: str = "Machine Learning Engineer",
) -> SourceJobObservation:
    return SourceJobObservation(
        source_platform=SourcePlatform.GREENHOUSE,
        source_job_id=url.rsplit("/", 1)[-1],
        source_url=url,
        application_url=url,
        company=company,
        title=title,
        description="A complete synthetic public job description.",
        location="Vancouver, BC",
        work_mode=WorkMode.HYBRID,
        posted_at="2026-08-02T12:00:00Z",
        ats_type=AtsType.GREENHOUSE,
        observed_at="2026-08-04T18:00:00Z",
        provenance=(
            FieldProvenance(
                field="title",
                source=ProvenanceSource.SOURCE_API,
                source_field="title",
            ),
        ),
    )


def _linkedin_alert_lead(
    *,
    url: str,
    with_hints: bool = True,
) -> JobLead:
    return JobLead.discover(
        subject_id=SUBJECT,
        source=JobLeadSource.LINKEDIN_ALERT_EMAIL,
        origin=JobLeadOrigin.LINKEDIN_SEARCH_INDEX,
        source_url=url,
        discovered_at=NOW,
        confidence=0.72,
        title_hint="Machine Learning Engineer" if with_hints else None,
        company_hint="Example Robotics" if with_hints else None,
        location_hint="Vancouver, BC" if with_hints else None,
        source_message_digest=hashlib.sha256(
            f"synthetic-message:{url}".encode("utf-8")
        ).hexdigest(),
    )


def _initial_source(request) -> JobLeadDiscoverySource | None:
    query = request.query
    mapping = {
        "site:linkedin.com/jobs/view": JobLeadDiscoverySource.LINKEDIN,
        "site:indeed.com/viewjob": JobLeadDiscoverySource.INDEED,
        "glassdoor.com/job-listing": JobLeadDiscoverySource.GLASSDOOR,
        "boards.greenhouse.io": JobLeadDiscoverySource.GREENHOUSE,
        "site:jobs.lever.co": JobLeadDiscoverySource.LEVER,
        "site:jobs.ashbyhq.com": JobLeadDiscoverySource.ASHBY,
        "site:jobs.jobvite.com": JobLeadDiscoverySource.JOBVITE,
        "site:myworkdayjobs.com": JobLeadDiscoverySource.WORKDAY,
        "site:jobs.smartrecruiters.com": JobLeadDiscoverySource.SMARTRECRUITERS,
        "site:icims.com/jobs": JobLeadDiscoverySource.ICIMS,
        "site:successfactors.com": JobLeadDiscoverySource.SUCCESSFACTORS,
    }
    for marker, source in mapping.items():
        if marker in query:
            return source
    if "inurl:careers" in query and not request.query_id.startswith("lead-canonical-"):
        return JobLeadDiscoverySource.GENERIC_CAREERS
    return None


def test_plan_covers_all_source_families_and_uses_bounded_pagination() -> None:
    all_sources = {
        JobLeadDiscoverySource.LINKEDIN,
        JobLeadDiscoverySource.INDEED,
        JobLeadDiscoverySource.GLASSDOOR,
        JobLeadDiscoverySource.GREENHOUSE,
        JobLeadDiscoverySource.LEVER,
        JobLeadDiscoverySource.ASHBY,
        JobLeadDiscoverySource.JOBVITE,
        JobLeadDiscoverySource.WORKDAY,
        JobLeadDiscoverySource.SMARTRECRUITERS,
        JobLeadDiscoverySource.ICIMS,
        JobLeadDiscoverySource.SUCCESSFACTORS,
        JobLeadDiscoverySource.GENERIC_CAREERS,
    }
    command = _command(offsets=(0, 1, 2), max_requests=500, count=17)
    plan = build_authorized_web_search_requests(
        command,
        _policy(
            roles=(
                "Machine Learning Engineer",
                "Applied Scientist",
                "Backend Engineer",
                "Data Engineer",
            )
        ),
    )

    assert plan.truncated is False
    assert {item.source for item in plan.requests} == all_sources
    assert {item.request.offset for item in plan.requests} == {0, 1, 2}
    assert {item.request.count for item in plan.requests} == {17}
    assert all("Vancouver or Remote Canada" in item.request.query for item in plan.requests)
    assert all("after:2026-07-28" in item.request.query for item in plan.requests)
    assert all(len(item.request.query.split()) <= 50 for item in plan.requests)
    workday_query = next(
        item.request.query
        for item in plan.requests
        if item.source is JobLeadDiscoverySource.WORKDAY
    )
    successfactors_query = next(
        item.request.query
        for item in plan.requests
        if item.source is JobLeadDiscoverySource.SUCCESSFACTORS
    )
    assert "site:myworkdayjobs.com" in workday_query
    assert "site:myworkdaysite.com" in workday_query
    assert "site:workdayjobs.com" in workday_query
    assert "site:successfactors.com" in successfactors_query
    assert "site:successfactors.eu" in successfactors_query

    clipped = build_authorized_web_search_requests(
        _command(
            offsets=(0, 1),
            max_requests=40,
            max_initial_requests=13,
        ),
        _policy(roles=("Machine Learning Engineer", "Applied Scientist")),
    )
    assert clipped.truncated is True
    assert len(clipped.requests) == 13
    assert {item.source for item in clipped.requests[:12]} == all_sources
    assert {item.request.offset for item in clipped.requests[:12]} == {0}
    assert clipped.requests[12].request.offset == 1


@pytest.mark.parametrize(
    "url",
    (
        "https://tenant.myworkdayjobs.com/jobs/synthetic-1",
        "https://tenant.myworkdaysite.com/jobs/synthetic-2",
        "https://tenant.workdayjobs.com/jobs/synthetic-3",
        "https://tenant.workday.com/jobs/synthetic-4",
    ),
)
def test_all_public_workday_host_families_are_recognized_ats_urls(
    url: str,
) -> None:
    assert classify_job_lead_origin(url) is JobLeadOrigin.ATS


@pytest.mark.asyncio
async def test_linkedin_is_persisted_as_a_lead_and_only_official_url_is_read() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/123"
    official = "https://boards.greenhouse.io/example/jobs/123"

    def handler(request):
        if request.query_id.startswith("lead-canonical-"):
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (AuthorizedWebSearchHit("Official job", official),),
            )
        if _initial_source(request) is JobLeadDiscoverySource.LINKEDIN:
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit(
                        "Machine Learning Engineer - Example Robotics | LinkedIn",
                        linkedin,
                        "A non-authoritative indexed excerpt.",
                    ),
                ),
            )
        return AuthorizedWebSearchResult.succeeded(request.query_id, ())

    repository = InMemoryLeads()
    reader = FakeReader({official: _observation(official)})
    discovery = AcceptingDiscovery()
    summary = await discover_job_leads(
        _command(),
        policy=_policy(),
        web_search=FakeWebSearch(handler),
        lead_repository=repository,
        public_job_reader=reader,
        subject_discovery=discovery,
    )

    assert summary.resolved == 1
    assert summary.needs_user == 0
    assert summary.resolved_job_ids == ("job-synthetic-1",)
    assert reader.calls == [official]
    assert linkedin not in reader.calls
    assert len(discovery.commands) == 1
    command = discovery.commands[0]
    assert command.source_kind is SubjectJobMembershipSourceKind.JOB_LEAD_RESOLUTION
    assert command.source_ref.startswith("job-lead-")
    current = repository.list_current(SUBJECT).leads
    assert len(current) == 1
    assert current[0].source_url == linkedin
    assert current[0].canonical_url == official
    assert current[0].status is JobLeadStatus.RESOLVED


@pytest.mark.asyncio
async def test_multiple_official_matches_stop_at_needs_user_without_discovery() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/456"
    first = "https://boards.greenhouse.io/example/jobs/456"
    second = "https://jobs.lever.co/example/456"

    def handler(request):
        if request.query_id.startswith("lead-canonical-"):
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit("Official one", first),
                    AuthorizedWebSearchHit("Official two", second),
                ),
            )
        if _initial_source(request) is JobLeadDiscoverySource.LINKEDIN:
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit(
                        "Machine Learning Engineer - Example Robotics | LinkedIn",
                        linkedin,
                    ),
                ),
            )
        return AuthorizedWebSearchResult.succeeded(request.query_id, ())

    repository = InMemoryLeads()
    discovery = AcceptingDiscovery()
    summary = await discover_job_leads(
        _command(),
        policy=_policy(),
        web_search=FakeWebSearch(handler),
        lead_repository=repository,
        public_job_reader=FakeReader(
            {first: _observation(first), second: _observation(second)}
        ),
        subject_discovery=discovery,
    )

    assert summary.resolved == 0
    assert summary.needs_user == 1
    assert summary.reason_codes == (
        JobLeadDiscoveryReason.AMBIGUOUS_OFFICIAL_POSTING,
    )
    assert discovery.commands == []
    lead = repository.list_current(SUBJECT).leads[0]
    assert lead.status is JobLeadStatus.NEEDS_USER
    assert lead.reason == "AMBIGUOUS_OFFICIAL_POSTING"


@pytest.mark.asyncio
async def test_same_subject_and_source_url_is_deduplicated_across_query_ids() -> None:
    official = "https://boards.greenhouse.io/example/jobs/789"

    def handler(request):
        if _initial_source(request) in {
            JobLeadDiscoverySource.LINKEDIN,
            JobLeadDiscoverySource.INDEED,
        }:
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit(
                        "Machine Learning Engineer", official
                    ),
                ),
            )
        return AuthorizedWebSearchResult.succeeded(request.query_id, ())

    repository = InMemoryLeads()
    reader = FakeReader({official: _observation(official)})
    discovery = AcceptingDiscovery()
    summary = await discover_job_leads(
        _command(),
        policy=_policy(),
        web_search=FakeWebSearch(handler),
        lead_repository=repository,
        public_job_reader=reader,
        subject_discovery=discovery,
    )

    assert summary.unique == 1
    assert summary.duplicates == 1
    assert summary.resolved == 1
    assert len(repository.list_current(SUBJECT).leads) == 1
    assert reader.calls == [official]
    assert len(discovery.commands) == 1


@pytest.mark.asyncio
async def test_partial_search_and_full_page_are_typed_and_progress_is_advisory() -> None:
    unknown = "https://news.example.test/article/role"

    def handler(request):
        if _initial_source(request) is JobLeadDiscoverySource.LINKEDIN:
            return AuthorizedWebSearchResult.partial(
                request.query_id,
                (AuthorizedWebSearchHit("Unverified role", unknown),),
                rejected_hit_count=2,
            )
        return AuthorizedWebSearchResult.succeeded(request.query_id, ())

    progress = []

    def observer(value):
        progress.append(value)
        if value.phase is JobLeadDiscoveryPhase.PERSISTING:
            raise RuntimeError("synthetic UI observer failure")

    repository = InMemoryLeads()
    summary = await discover_job_leads(
        _command(count=1),
        policy=_policy(),
        web_search=FakeWebSearch(handler),
        lead_repository=repository,
        public_job_reader=FakeReader({}),
        subject_discovery=AcceptingDiscovery(),
        progress_observer=observer,
    )

    assert summary.status is JobLeadDiscoveryStatus.PARTIAL
    assert summary.truncated is True
    assert summary.unique == 1
    assert summary.needs_user == 1
    linkedin = next(
        result
        for result in summary.source_results
        if result.source is JobLeadDiscoverySource.LINKEDIN
    )
    assert linkedin.status is JobLeadSourceRunStatus.PARTIAL
    assert linkedin.rejected_hits == 2
    assert linkedin.truncated is True
    assert progress[-1].phase is JobLeadDiscoveryPhase.COMPLETED


@pytest.mark.asyncio
async def test_persisted_alert_lead_resolves_without_repeat_discovery_search() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/alert-123"
    official = "https://boards.greenhouse.io/example/jobs/alert-123"
    repository = InMemoryLeads()
    discovered = _linkedin_alert_lead(url=linkedin)
    assert repository.save(discovered).status is JobLeadWriteStatus.CREATED

    def handler(request):
        assert request.query_id.startswith("lead-canonical-")
        return AuthorizedWebSearchResult.succeeded(
            request.query_id,
            (AuthorizedWebSearchHit("Official posting", official),),
        )

    web = FakeWebSearch(handler)
    reader = FakeReader({official: _observation(official)})
    discovery = AcceptingDiscovery()
    summary = await resolve_persisted_job_leads(
        _command(),
        web_search=web,
        lead_repository=repository,
        public_job_reader=reader,
        subject_discovery=discovery,
    )

    assert summary.status is JobLeadDiscoveryStatus.SUCCEEDED
    assert summary.unique == 1
    assert summary.resolved == 1
    assert summary.resolved_job_ids == ("job-synthetic-1",)
    assert len(web.requests) == 1
    assert reader.calls == [official]
    assert linkedin not in reader.calls
    current = repository.list_current(SUBJECT).leads[0]
    assert current.status is JobLeadStatus.RESOLVED
    assert current.canonical_url == official


@pytest.mark.asyncio
async def test_persisted_platform_lead_without_hints_never_opens_platform_page() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/alert-needs-user"
    repository = InMemoryLeads()
    discovered = _linkedin_alert_lead(url=linkedin, with_hints=False)
    repository.save(discovered)

    def unexpected_search(request):
        raise AssertionError(f"unexpected search: {request.query_id}")

    web = FakeWebSearch(unexpected_search)
    reader = FakeReader({})
    discovery = AcceptingDiscovery()
    summary = await resolve_persisted_job_leads(
        _command(),
        web_search=web,
        lead_repository=repository,
        public_job_reader=reader,
        subject_discovery=discovery,
        leads=(discovered,),
    )

    assert summary.resolved == 0
    assert summary.needs_user == 1
    assert summary.reason_codes == (
        JobLeadDiscoveryReason.SOURCE_REQUIRES_USER,
    )
    assert web.requests == []
    assert reader.calls == []
    assert discovery.commands == []
    current = repository.list_current(SUBJECT).leads[0]
    assert current.status is JobLeadStatus.NEEDS_USER


@pytest.mark.asyncio
async def test_official_clipper_lead_resolves_without_web_search_configuration() -> None:
    official = "https://boards.greenhouse.io/example/jobs/clipper-123"
    repository = InMemoryLeads()
    discovered = JobLead.discover(
        subject_id=SUBJECT,
        source=JobLeadSource.WEB_CLIPPER,
        origin=JobLeadOrigin.ATS,
        source_url=official,
        discovered_at=NOW,
        confidence=0.90,
        title_hint="Machine Learning Engineer",
        company_hint="Example Robotics",
    )
    repository.save(discovered)
    reader = FakeReader({official: _observation(official)})
    discovery = AcceptingDiscovery()

    summary = await resolve_persisted_job_leads(
        _command(),
        web_search=None,
        lead_repository=repository,
        public_job_reader=reader,
        subject_discovery=discovery,
    )

    assert summary.status is JobLeadDiscoveryStatus.SUCCEEDED
    assert summary.resolved == 1
    assert summary.requests == 0
    assert reader.calls == [official]
    assert discovery.commands[0].source_kind is (
        SubjectJobMembershipSourceKind.JOB_LEAD_RESOLUTION
    )


@pytest.mark.asyncio
async def test_unknown_careers_path_is_not_read_as_an_authoritative_job() -> None:
    unverified = "https://aggregator.example.test/jobs/synthetic-123"
    repository = InMemoryLeads()
    discovered = JobLead.discover(
        subject_id=SUBJECT,
        source=JobLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL,
        origin=JobLeadOrigin.UNKNOWN_WEB,
        source_url=unverified,
        discovered_at=NOW,
        confidence=0.70,
        title_hint="Machine Learning Engineer",
        company_hint="Example Robotics",
        source_message_digest=hashlib.sha256(
            b"synthetic-unknown-employer-alert"
        ).hexdigest(),
    )
    repository.save(discovered)
    reader = FakeReader({unverified: _observation(unverified)})
    discovery = AcceptingDiscovery()

    summary = await resolve_persisted_job_leads(
        _command(),
        web_search=None,
        lead_repository=repository,
        public_job_reader=reader,
        subject_discovery=discovery,
        leads=(discovered,),
    )

    assert summary.resolved == 0
    assert summary.needs_user == 1
    assert summary.reason_codes == (
        JobLeadDiscoveryReason.SOURCE_REQUIRES_USER,
    )
    assert reader.calls == []
    assert discovery.commands == []
    current = repository.list_current(SUBJECT).leads[0]
    assert current.status is JobLeadStatus.NEEDS_USER


@pytest.mark.asyncio
async def test_platform_lead_with_hints_needs_user_when_web_search_is_unavailable() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/no-web-search"
    repository = InMemoryLeads()
    discovered = _linkedin_alert_lead(url=linkedin, with_hints=True)
    repository.save(discovered)
    reader = FakeReader({})
    discovery = AcceptingDiscovery()

    summary = await resolve_persisted_job_leads(
        _command(),
        web_search=None,
        lead_repository=repository,
        public_job_reader=reader,
        subject_discovery=discovery,
    )

    assert summary.resolved == 0
    assert summary.needs_user == 1
    assert summary.reason_codes == (
        JobLeadDiscoveryReason.SOURCE_REQUIRES_USER,
    )
    assert summary.requests == 0
    assert reader.calls == []
    assert discovery.commands == []


@pytest.mark.asyncio
async def test_platform_lead_resolves_from_configured_feed_without_web_search() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/configured-feed"
    official = "https://boards.greenhouse.io/example/jobs/configured-feed"
    observation = _observation(official)
    repository = InMemoryLeads()
    discovered = _linkedin_alert_lead(url=linkedin, with_hints=True)
    repository.save(discovered)
    configured = FakeConfiguredSearch(
        JobSearchResult.succeeded(
            CandidateSet(
                candidate_set_id="configured-resolution-set",
                request_id="configured-resolution-request",
                candidates=(
                    SearchCandidate(
                        candidate_id="configured-resolution-candidate",
                        company=observation.company,
                        title=observation.title,
                        location=observation.location or None,
                        source_platform=observation.source_platform,
                        source_url=observation.source_url,
                        source_job_id=observation.source_job_id,
                        observation=observation,
                    ),
                ),
                created_at=NOW,
            )
        )
    )
    discovery = AcceptingDiscovery()

    summary = await resolve_persisted_job_leads(
        _command(
            max_requests=1,
            max_initial_requests=1,
            max_canonical_searches=0,
            max_public_reads=0,
        ),
        web_search=None,
        lead_repository=repository,
        public_job_reader=FakeReader({}),
        subject_discovery=discovery,
        configured_job_search=configured,
    )

    assert len(configured.requests) == 1
    assert summary.canonical_searches == 0
    assert summary.resolved == 1
    assert summary.needs_user == 0
    assert repository.list_current(SUBJECT).leads[0].status is (
        JobLeadStatus.RESOLVED
    )
    assert len(discovery.commands) == 1


@pytest.mark.asyncio
async def test_zero_canonical_budget_never_borrows_initial_search_budget() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/budget-zero"

    def handler(request):
        if request.query_id.startswith("lead-canonical-"):
            raise AssertionError("canonical search must be disabled")
        if _initial_source(request) is JobLeadDiscoverySource.LINKEDIN:
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit(
                        "Machine Learning Engineer - Example Robotics | LinkedIn",
                        linkedin,
                    ),
                ),
            )
        return AuthorizedWebSearchResult.succeeded(request.query_id, ())

    web = FakeWebSearch(handler)
    progress = []
    summary = await discover_job_leads(
        _command(
            max_requests=2,
            max_initial_requests=2,
            max_canonical_searches=0,
        ),
        policy=_policy(),
        web_search=web,
        lead_repository=InMemoryLeads(),
        public_job_reader=FakeReader({}),
        subject_discovery=AcceptingDiscovery(),
        progress_observer=progress.append,
    )

    assert len(web.requests) == 2
    assert all(
        not request.query_id.startswith("lead-canonical-")
        for request in web.requests
    )
    assert summary.requests == 2
    assert summary.canonical_searches == 0
    assert summary.needs_user == 1
    assert progress[-1].canonical_searches == 0
    assert {
        item.source for item in progress[-1].source_results
    } >= {
        JobLeadDiscoverySource.LINKEDIN,
        JobLeadDiscoverySource.INDEED,
    }


@pytest.mark.asyncio
async def test_canonical_and_initial_search_budgets_are_counted_independently() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/budget-one"
    indeed = "https://ca.indeed.com/viewjob?jk=budget-one"
    official = "https://boards.greenhouse.io/example/jobs/budget-one"

    def handler(request):
        if request.query_id.startswith("lead-canonical-"):
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (AuthorizedWebSearchHit("Official posting", official),),
            )
        source = _initial_source(request)
        if source is JobLeadDiscoverySource.LINKEDIN:
            url = linkedin
            suffix = "LinkedIn"
        elif source is JobLeadDiscoverySource.INDEED:
            url = indeed
            suffix = "Indeed"
        else:
            return AuthorizedWebSearchResult.succeeded(request.query_id, ())
        return AuthorizedWebSearchResult.succeeded(
            request.query_id,
            (
                AuthorizedWebSearchHit(
                    f"Machine Learning Engineer - Example Robotics | {suffix}",
                    url,
                ),
            ),
        )

    web = FakeWebSearch(handler)
    summary = await discover_job_leads(
        _command(
            max_requests=2,
            max_initial_requests=2,
            max_canonical_searches=1,
        ),
        policy=_policy(),
        web_search=web,
        lead_repository=InMemoryLeads(),
        public_job_reader=FakeReader({official: _observation(official)}),
        subject_discovery=AcceptingDiscovery(),
    )

    initial_requests = tuple(
        request
        for request in web.requests
        if not request.query_id.startswith("lead-canonical-")
    )
    canonical_requests = tuple(
        request
        for request in web.requests
        if request.query_id.startswith("lead-canonical-")
    )
    assert len(initial_requests) == 2
    assert len(canonical_requests) == 1
    assert summary.requests == 3
    assert summary.canonical_searches == 1
    assert summary.truncated is True
    canonical = next(
        item
        for item in summary.source_results
        if item.source is JobLeadDiscoverySource.CANONICAL_WEB_RESOLUTION
    )
    assert canonical.requests == 1


@pytest.mark.asyncio
async def test_public_read_budget_stops_before_ambiguous_partial_verification() -> None:
    linkedin = "https://www.linkedin.com/jobs/view/read-budget"
    first = "https://boards.greenhouse.io/example/jobs/read-budget-1"
    second = "https://boards.greenhouse.io/example/jobs/read-budget-2"

    def handler(request):
        if request.query_id.startswith("lead-canonical-"):
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit("Official one", first),
                    AuthorizedWebSearchHit("Official two", second),
                ),
            )
        if _initial_source(request) is JobLeadDiscoverySource.LINKEDIN:
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit(
                        "Machine Learning Engineer - Example Robotics | LinkedIn",
                        linkedin,
                    ),
                ),
            )
        return AuthorizedWebSearchResult.succeeded(request.query_id, ())

    reader = FakeReader(
        {first: _observation(first), second: _observation(second)}
    )
    discovery = AcceptingDiscovery()
    repository = InMemoryLeads()
    progress = []
    summary = await discover_job_leads(
        _command(
            max_requests=1,
            max_initial_requests=1,
            max_canonical_searches=1,
            max_public_reads=1,
        ),
        policy=_policy(),
        web_search=FakeWebSearch(handler),
        lead_repository=repository,
        public_job_reader=reader,
        subject_discovery=discovery,
        progress_observer=progress.append,
    )

    assert reader.calls == [first]
    assert discovery.commands == []
    assert summary.public_reads == 1
    assert summary.resolved == 0
    assert summary.needs_user == 1
    assert summary.truncated is True
    assert JobLeadDiscoveryReason.PUBLIC_READ_BUDGET_EXHAUSTED in (
        summary.reason_codes
    )
    assert progress[-1].public_reads == 1
    stored = repository.list_current(SUBJECT).leads[0]
    assert stored.reason == "PUBLIC_READ_BUDGET_EXHAUSTED"
    canonical = next(
        item
        for item in summary.source_results
        if item.source is JobLeadDiscoverySource.CANONICAL_WEB_RESOLUTION
    )
    assert canonical.public_reads == 1
    assert canonical.truncated is True
