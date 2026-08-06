"""Focused S3d authenticated Refresh Job Library UI tests."""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from starlette.requests import Request

from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.job_library_refresh import (
    DiscoveryRefreshSummary,
    JobLibraryRefreshProgress,
    JobLibraryRefreshProgressPhase,
    JobLibraryRefreshStatus,
    ManualJobLibraryRefreshCommand,
    ManualJobLibraryRefreshResult,
    PriorityRefreshSummary,
    ProfileRefreshFailureReason,
    ProfileRefreshSearchStatus,
    SearchProfileRefreshResult,
    JobLibraryRefreshRun,
)
from core.job_leads import JobLeadSource
from core.search_profile import (
    SearchProfileSourceKind,
    SearchProfileSourceReference,
)
from dashboard.job_library_refresh import (
    LeadRefreshPhase,
    LeadRefreshProgress,
    LeadRefreshResult,
    LeadRefreshSourceResult,
    LeadRefreshSourceStatus,
    LeadRefreshStatus,
    RefreshJobLibraryUICommand,
    RefreshJobLibraryUIController,
    RefreshJobLibraryUIStatus,
    map_manual_refresh_result,
    merge_lead_refresh_result,
)
from dashboard.server import app, refresh_job_library_ui
from tests.test_application_plan import NOW, SUBJECT


def _context() -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="session_reference_0123456789abcdef",
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def _result(
    *,
    invocation_id: str,
    status: JobLibraryRefreshStatus,
    failed_profile: bool = False,
) -> ManualJobLibraryRefreshResult:
    command = ManualJobLibraryRefreshCommand(
        subject_id=SUBJECT,
        invocation_id=invocation_id,
        now=NOW,
        max_reprioritizations=5,
    )
    profile_results = ()
    discovery = DiscoveryRefreshSummary(0, 0, 0, 0, 0, 0)
    priority = None
    if status is not JobLibraryRefreshStatus.NOOP:
        source = SearchProfileSourceReference(
            SearchProfileSourceKind.KNOWN_GREENHOUSE_BOARD, "synthetic"
        )
        profile_results = (
            SearchProfileRefreshResult.create(
                profile_id="search-profile-synthetic",
                profile_version=1,
                source=source,
                search_status=(
                    ProfileRefreshSearchStatus.FAILED
                    if failed_profile
                    else ProfileRefreshSearchStatus.SUCCEEDED
                ),
                candidate_count=0,
                reason=(
                    ProfileRefreshFailureReason.SEARCH_EXCEPTION
                    if failed_profile
                    else None
                ),
                source_reason=(
                    "/private/secret/traceback token=secret"
                    if failed_profile
                    else None
                ),
            ),
        )
        priority = PriorityRefreshSummary(
            status="COMPLETED",
            requested=5,
            selected=2,
            created=1,
            unchanged=1,
            failed=0,
        )
    run = JobLibraryRefreshRun.create(
        command=command,
        profile_snapshot_hash="a" * 64,
        profile_results=profile_results,
        candidate_results=(),
        discovery_summary=discovery,
        priority_summary=priority,
        overall_status=status,
    )
    return ManualJobLibraryRefreshResult(status, run, None)


@pytest.mark.asyncio
async def test_click_uses_authenticated_subject_and_one_inflight_s3b_call(
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[ManualJobLibraryRefreshCommand] = []

    async def manual_refresh(
        command: ManualJobLibraryRefreshCommand,
        *,
        progress_observer=None,
    ) -> ManualJobLibraryRefreshResult:
        calls.append(command)
        started.set()
        await release.wait()
        return _result(
            invocation_id=command.invocation_id,
            status=JobLibraryRefreshStatus.COMPLETED,
        )

    controller = RefreshJobLibraryUIController(
        manual_refresh=manual_refresh, clock=lambda: NOW
    )
    command = RefreshJobLibraryUICommand("ui-click-001", 5)
    first = asyncio.create_task(
        controller.refresh(context=_context(), command=command)
    )
    await started.wait()
    duplicate = asyncio.create_task(
        controller.refresh(context=_context(), command=command)
    )
    competing = await controller.refresh(
        context=_context(),
        command=RefreshJobLibraryUICommand("ui-click-002", 5),
    )
    release.set()

    assert (await first).status is RefreshJobLibraryUIStatus.COMPLETED
    assert (await duplicate).status is RefreshJobLibraryUIStatus.COMPLETED
    assert competing.status is RefreshJobLibraryUIStatus.RUNNING
    assert len(calls) == 1
    assert calls[0].subject_id == SUBJECT
    assert calls[0].now == NOW
    assert calls[0].max_reprioritizations == 5


@pytest.mark.asyncio
async def test_start_returns_running_and_status_polls_terminal_result() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def manual_refresh(command, *, progress_observer=None):
        started.set()
        await release.wait()
        return _result(
            invocation_id=command.invocation_id,
            status=JobLibraryRefreshStatus.COMPLETED,
        )

    controller = RefreshJobLibraryUIController(
        manual_refresh=manual_refresh,
        clock=lambda: NOW,
    )
    command = RefreshJobLibraryUICommand("ui-poll-001", 5)

    initial = await controller.start(context=_context(), command=command)
    await started.wait()
    running = await controller.status(context=_context())
    release.set()
    await asyncio.sleep(0)
    completed = await controller.status(context=_context())

    assert initial.status is RefreshJobLibraryUIStatus.RUNNING
    assert running.status is RefreshJobLibraryUIStatus.RUNNING
    assert completed.status is RefreshJobLibraryUIStatus.COMPLETED
    assert completed.summary.priorities_refreshed == 2


@pytest.mark.asyncio
async def test_running_status_exposes_per_query_counts_before_priority() -> None:
    reported = asyncio.Event()
    release = asyncio.Event()
    source = SearchProfileSourceReference(
        SearchProfileSourceKind.KNOWN_GREENHOUSE_BOARD, "synthetic"
    )
    profile_result = SearchProfileRefreshResult.create(
        profile_id="search-profile-synthetic",
        profile_version=1,
        source=source,
        search_status=ProfileRefreshSearchStatus.SUCCEEDED,
        candidate_count=0,
        reason=None,
        source_reason=None,
    )

    async def manual_refresh(command, *, progress_observer=None):
        assert progress_observer is not None
        await progress_observer(
            JobLibraryRefreshProgress(
                phase=JobLibraryRefreshProgressPhase.SEARCHING,
                enabled_profiles=2,
                profile_results=(profile_result,),
                unique_candidates=0,
                candidates_processed=0,
                discovery_summary=DiscoveryRefreshSummary(0, 0, 0, 0, 0, 0),
            )
        )
        reported.set()
        await release.wait()
        return _result(
            invocation_id=command.invocation_id,
            status=JobLibraryRefreshStatus.COMPLETED,
        )

    controller = RefreshJobLibraryUIController(
        manual_refresh=manual_refresh,
        clock=lambda: NOW,
    )
    await controller.start(
        context=_context(),
        command=RefreshJobLibraryUICommand("ui-progress-001", 5),
    )
    await reported.wait()

    running = await controller.status(context=_context())
    assert running.phase == "SEARCHING"
    assert running.summary.searched_profiles == 1
    assert running.summary.zero_result_profiles == 1
    assert running.summary.profiles_with_matches == 0
    assert running.source_results[0]["candidate_count"] == 0

    release.set()
    await asyncio.sleep(0)
    await controller.status(context=_context())


def test_typed_results_map_to_safe_completed_partial_and_noop_ui() -> None:
    completed = map_manual_refresh_result(
        _result(
            invocation_id="ui-completed",
            status=JobLibraryRefreshStatus.COMPLETED,
        ),
        invocation_id="ui-completed",
    )
    partial = map_manual_refresh_result(
        _result(
            invocation_id="ui-partial",
            status=JobLibraryRefreshStatus.PARTIAL_FAILURE,
            failed_profile=True,
        ),
        invocation_id="ui-partial",
    )
    noop = map_manual_refresh_result(
        _result(
            invocation_id="ui-noop",
            status=JobLibraryRefreshStatus.NOOP,
        ),
        invocation_id="ui-noop",
    )
    raw_priority_failure = _result(
        invocation_id="ui-priority-timeout",
        status=JobLibraryRefreshStatus.PARTIAL_FAILURE,
    )
    priority_failure = map_manual_refresh_result(
        ManualJobLibraryRefreshResult(
            raw_priority_failure.status,
            raw_priority_failure.run,
            None,
            priority_failure_codes=(
                "PROPOSAL_FAILED:AGENT_TIMEOUT",
                "PROPOSAL_FAILED:AGENT_TIMEOUT",
            ),
        ),
        invocation_id="ui-priority-timeout",
    )

    assert completed.status is RefreshJobLibraryUIStatus.COMPLETED
    assert completed.summary.priorities_refreshed == 2
    assert partial.status is RefreshJobLibraryUIStatus.PARTIAL_FAILURE
    assert partial.source_failures
    assert "/private/" not in repr(partial.to_dict())
    assert "token=secret" not in repr(partial.to_dict())
    assert noop.status is RefreshJobLibraryUIStatus.NOOP
    assert noop.message == "No enabled job-search configuration was found."
    assert priority_failure.priority_failures == (
        {
            "code": "PROPOSAL_FAILED:AGENT_TIMEOUT",
            "count": 2,
            "message": "Priority AI timed out.",
        },
    )
    assert priority_failure.source_failures == ()

    priority_only = merge_lead_refresh_result(
        priority_failure,
        LeadRefreshResult(status=LeadRefreshStatus.NOOP),
        completed_at=NOW,
    )
    assert priority_only.status is RefreshJobLibraryUIStatus.PARTIAL_FAILURE
    assert priority_only.message == (
        "Source search and the job-library update completed, but some "
        "Priority decisions need attention."
    )
    assert priority_only.source_failures == ()


@pytest.mark.asyncio
async def test_optional_lead_refresh_reports_progress_and_keeps_leads_separate(
) -> None:
    progress_reported = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[str, str]] = []
    web_source = LeadRefreshSourceResult(
        source=JobLeadSource.AUTHORIZED_WEB_SEARCH,
        status=LeadRefreshSourceStatus.COMPLETED,
        family="LINKEDIN",
        requests=12,
        completed=12,
        search_hits=48,
        discovered=48,
        unique=39,
        duplicates=9,
        resolved=21,
        needs_user=18,
        public_reads=21,
    )
    email_source = LeadRefreshSourceResult(
        source=JobLeadSource.LINKEDIN_ALERT_EMAIL,
        status=LeadRefreshSourceStatus.COMPLETED,
        requests=1,
        completed=1,
        discovered=4,
        unique=3,
        duplicates=1,
        resolved=2,
        needs_user=1,
    )

    async def manual_refresh(command, *, progress_observer=None):
        return _result(
            invocation_id=command.invocation_id,
            status=JobLibraryRefreshStatus.COMPLETED,
        )

    async def lead_refresh(
        *, subject_id, invocation_id, now, progress_observer=None
    ):
        calls.append((subject_id, invocation_id))
        assert now == NOW
        assert progress_observer is not None
        await progress_observer(
            LeadRefreshProgress(
                phase=LeadRefreshPhase.RESOLVING,
                requests=13,
                completed=13,
                discovered=52,
                unique=42,
                duplicates=10,
                resolved=23,
                needs_user=19,
                public_reads=21,
                priorities_requested=23,
                priorities_refreshed=20,
                priorities_failed=3,
                source_results=(web_source, email_source),
            )
        )
        progress_reported.set()
        await release.wait()
        return LeadRefreshResult(
            status=LeadRefreshStatus.PARTIAL_FAILURE,
            requests=13,
            completed=13,
            discovered=52,
            unique=42,
            duplicates=10,
            resolved=23,
            needs_user=19,
            public_reads=21,
            priorities_requested=23,
            priorities_refreshed=20,
            priorities_failed=3,
            source_results=(web_source, email_source),
        )

    controller = RefreshJobLibraryUIController(
        manual_refresh=manual_refresh,
        lead_refresh=lead_refresh,
        clock=lambda: NOW,
    )
    initial = await controller.start(
        context=_context(),
        command=RefreshJobLibraryUICommand("ui-multi-source-001", 5),
    )
    await progress_reported.wait()
    running = await controller.status(context=_context())

    assert initial.status is RefreshJobLibraryUIStatus.RUNNING
    assert running.status is RefreshJobLibraryUIStatus.RUNNING
    assert running.phase == "RESOLVING"
    assert running.summary.leads_discovered == 52
    assert running.summary.leads_unique == 42
    assert running.summary.leads_deduplicated == 10
    assert running.summary.leads_resolved == 23
    assert running.summary.leads_needing_review == 19
    assert running.summary.lead_public_reads == 21
    assert running.summary.lead_refresh_ran is True
    assert running.summary.priorities_requested == 28
    assert running.summary.priorities_refreshed == 22
    assert running.summary.priorities_failed == 3
    assert running.source_results[-2]["result_type"] == "JOB_LEAD"
    assert running.source_results[-2]["provider"] == "LINKEDIN"
    assert running.source_results[-2]["acquisition_source"] == (
        "AUTHORIZED_WEB_SEARCH"
    )
    assert running.source_results[-2]["requests"] == 12
    assert running.source_results[-2]["completed"] == 12
    assert running.source_results[-2]["search_hits"] == 48
    assert running.source_results[-2]["public_reads"] == 21
    assert running.source_results[-2]["leads_resolved"] == 21
    assert running.source_results[-2]["leads_needing_review"] == 18
    assert running.source_results[-2]["lead_failures"] == 0
    assert running.source_results[-2]["truncated"] is False
    assert running.source_results[-1]["provider"] == "LINKEDIN_ALERT_EMAIL"

    release.set()
    await asyncio.sleep(0)
    terminal = await controller.status(context=_context())
    assert terminal.status is RefreshJobLibraryUIStatus.PARTIAL_FAILURE
    assert terminal.summary.lead_failures == 0
    assert terminal.summary.priorities_failed == 3
    assert not any(
        "lead operation" in failure for failure in terminal.source_failures
    )
    assert calls == [(SUBJECT, "ui-multi-source-001")]


@pytest.mark.parametrize(
    ("provider_status", "lead_status", "expected"),
    (
        (
            JobLibraryRefreshStatus.NOOP,
            LeadRefreshStatus.COMPLETED,
            RefreshJobLibraryUIStatus.COMPLETED,
        ),
        (
            JobLibraryRefreshStatus.COMPLETED,
            LeadRefreshStatus.FAILED,
            RefreshJobLibraryUIStatus.PARTIAL_FAILURE,
        ),
        (
            JobLibraryRefreshStatus.FAILED,
            LeadRefreshStatus.FAILED,
            RefreshJobLibraryUIStatus.FAILED,
        ),
        (
            JobLibraryRefreshStatus.NOOP,
            LeadRefreshStatus.NOOP,
            RefreshJobLibraryUIStatus.NOOP,
        ),
    ),
)
def test_provider_and_lead_terminal_statuses_merge_without_promoting_leads(
    provider_status: JobLibraryRefreshStatus,
    lead_status: LeadRefreshStatus,
    expected: RefreshJobLibraryUIStatus,
) -> None:
    provider = map_manual_refresh_result(
        _result(invocation_id="ui-merge", status=provider_status),
        invocation_id="ui-merge",
    )
    lead = LeadRefreshResult(
        status=lead_status,
        requests=1 if lead_status is not LeadRefreshStatus.NOOP else 0,
        completed=(
            1
            if lead_status
            in {LeadRefreshStatus.COMPLETED, LeadRefreshStatus.PARTIAL_FAILURE}
            else 0
        ),
        discovered=6 if lead_status is LeadRefreshStatus.COMPLETED else 0,
        unique=5 if lead_status is LeadRefreshStatus.COMPLETED else 0,
        duplicates=1 if lead_status is LeadRefreshStatus.COMPLETED else 0,
        resolved=2 if lead_status is LeadRefreshStatus.COMPLETED else 0,
        needs_user=3 if lead_status is LeadRefreshStatus.COMPLETED else 0,
        failed=1 if lead_status is LeadRefreshStatus.FAILED else 0,
    )

    merged = merge_lead_refresh_result(provider, lead, completed_at=NOW)

    assert merged.status is expected
    assert merged.summary.leads_discovered == lead.discovered
    assert merged.summary.leads_resolved == lead.resolved
    assert merged.summary.jobs_created == provider.summary.jobs_created
    assert merged.summary.lead_refresh_ran is True


def test_lead_source_contract_rejects_duplicate_channels_and_bad_counts() -> None:
    source = LeadRefreshSourceResult(
        source=JobLeadSource.WEB_CLIPPER,
        status=LeadRefreshSourceStatus.COMPLETED,
        discovered=1,
        unique=1,
        needs_user=1,
    )
    with pytest.raises(ValueError, match="unique by source"):
        LeadRefreshResult(
            status=LeadRefreshStatus.COMPLETED,
            source_results=(source, source),
        )
    with pytest.raises(ValueError, match="completed cannot exceed requests"):
        LeadRefreshSourceResult(
            source=JobLeadSource.PASTED_URL,
            status=LeadRefreshSourceStatus.COMPLETED,
            requests=0,
            completed=1,
        )


@pytest.mark.asyncio
async def test_route_replay_reuses_invocation_and_ui_has_no_business_calls(
) -> None:
    calls: list[ManualJobLibraryRefreshCommand] = []

    async def manual_refresh(
        command: ManualJobLibraryRefreshCommand,
        *,
        progress_observer=None,
    ) -> ManualJobLibraryRefreshResult:
        calls.append(command)
        original = _result(
            invocation_id=command.invocation_id,
            status=JobLibraryRefreshStatus.COMPLETED,
        )
        if len(calls) == 1:
            return original
        return ManualJobLibraryRefreshResult(
            JobLibraryRefreshStatus.UNCHANGED, original.run, None
        )

    controller = RefreshJobLibraryUIController(
        manual_refresh=manual_refresh, clock=lambda: NOW
    )
    app.state.job_library_refresh_controller = controller
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/job-library/refresh",
            "headers": [],
            "query_string": b"subject_id=subject-attacker",
        }
    )
    body = {
        "subject_id": "subject-attacker",
        "invocation_id": "ui-replay-001",
    }
    first = await refresh_job_library_ui(body, request, _context())
    duplicate = await refresh_job_library_ui(body, request, _context())
    await asyncio.sleep(0)
    completed = await controller.status(context=_context())
    replay_started = await refresh_job_library_ui(body, request, _context())
    await asyncio.sleep(0)
    replay = await controller.status(context=_context())

    assert first["status"] == "RUNNING"
    assert duplicate["status"] == "RUNNING"
    assert completed.status is RefreshJobLibraryUIStatus.COMPLETED
    assert replay_started["status"] == "RUNNING"
    assert replay.status is RefreshJobLibraryUIStatus.COMPLETED
    assert replay.replayed is True
    assert [call.invocation_id for call in calls] == [
        "ui-replay-001",
        "ui-replay-001",
    ]
    assert all(call.subject_id == SUBJECT for call in calls)
    assert all(call.max_reprioritizations == 10 for call in calls)

    root = Path(__file__).parents[1]
    source = (root / "dashboard/job_library_refresh.py").read_text()
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imports.intersection(
        {
            "core.discovery",
            "core.prioritization",
            "core.automation_cycle",
            "core.application_engine",
        }
    )
    template = (root / "dashboard/templates/index.html").read_text()
    javascript = (root / "dashboard/static/app.js").read_text()
    assert "Refresh job library" in template
    assert 'src="/static/app.js?v=multi-source-leads-v8"' in template
    assert 'href="/static/style.css?v=multi-source-leads-v8"' in template
    assert "Jobs matched to your preferences" in template
    assert 'id="refresh-jobs"' in template
    assert "JobOps" in template
    assert "MR.Jobs" not in template
    assert app.title == "JobOps"
    assert "x-data=" not in template
    assert javascript.count('postJson("/api/job-library/refresh"') == 1
    assert 'getJson("/api/job-library/refresh/status"' in javascript
    assert "if (state.refreshing) return" in javascript
    for source in (
        "AUTHORIZED_WEB_SEARCH",
        "LINKEDIN_ALERT_EMAIL",
        "INDEED_ALERT_EMAIL",
        "EMPLOYER_OR_ATS_ALERT_EMAIL",
        "WEB_CLIPPER",
        "PASTED_URL",
    ):
        assert source in javascript
    assert "resolved to verified jobs" in javascript
    assert "need review" in javascript
