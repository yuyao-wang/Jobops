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
    JobLibraryRefreshStatus,
    ManualJobLibraryRefreshCommand,
    ManualJobLibraryRefreshResult,
    PriorityRefreshSummary,
    ProfileRefreshFailureReason,
    ProfileRefreshSearchStatus,
    SearchProfileRefreshResult,
    JobLibraryRefreshRun,
)
from core.search_profile import (
    SearchProfileSourceKind,
    SearchProfileSourceReference,
)
from dashboard.job_library_refresh import (
    RefreshJobLibraryUICommand,
    RefreshJobLibraryUIController,
    RefreshJobLibraryUIStatus,
    map_manual_refresh_result,
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

    assert completed.status is RefreshJobLibraryUIStatus.COMPLETED
    assert completed.summary.priorities_refreshed == 2
    assert partial.status is RefreshJobLibraryUIStatus.PARTIAL_FAILURE
    assert partial.source_failures
    assert "/private/" not in repr(partial.to_dict())
    assert "token=secret" not in repr(partial.to_dict())
    assert noop.status is RefreshJobLibraryUIStatus.NOOP
    assert noop.message == "没有已启用的职位搜索配置。"


@pytest.mark.asyncio
async def test_route_replay_reuses_invocation_and_ui_has_no_business_calls(
) -> None:
    calls: list[ManualJobLibraryRefreshCommand] = []

    async def manual_refresh(
        command: ManualJobLibraryRefreshCommand,
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

    app.state.job_library_refresh_controller = (
        RefreshJobLibraryUIController(
            manual_refresh=manual_refresh, clock=lambda: NOW
        )
    )
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
        "max_reprioritizations": 5,
    }
    first = await refresh_job_library_ui(body, request, _context())
    replay = await refresh_job_library_ui(body, request, _context())

    assert first["status"] == "COMPLETED"
    assert replay["status"] == "COMPLETED"
    assert replay["replayed"] is True
    assert [call.invocation_id for call in calls] == [
        "ui-replay-001",
        "ui-replay-001",
    ]
    assert all(call.subject_id == SUBJECT for call in calls)

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
    assert "Jobs matched to your preferences" in template
    assert 'id="refresh-jobs"' in template
    assert "JobOps" in template
    assert "MR.Jobs" not in template
    assert app.title == "JobOps"
    assert "x-data=" not in template
    assert javascript.count(
        'postJson("/api/job-library/refresh"'
    ) == 1
    assert "if (state.refreshing) return" in javascript
