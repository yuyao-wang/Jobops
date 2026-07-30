"""Focused S3e Continue Automatic Application UI tests."""

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
from core.automation_cycle import (
    AutomationCycleOperationStatus,
    AutomationCycleRun,
    AutomationCycleRunStatus,
    AutomationCycleStage,
    AutomationCycleStageResult,
    AutomationCycleStageStatus,
    AutomationCycleSummary,
    RunAutomationCycleCommand,
    RunAutomationCycleResult,
)
from dashboard.automation_cycle import (
    AutomationCycleUIBudgetConfig,
    ContinueAutomationUICommand,
    ContinueAutomationUIController,
    ContinueAutomationUIStatus,
    map_automation_cycle_result,
)
from dashboard.server import app, continue_automatic_application_ui
from tests.test_application_plan import NOW, SUBJECT


def _context() -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="session_reference_0123456789abcdef",
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def _stage(
    stage: AutomationCycleStage,
    budget: int,
    *,
    status: AutomationCycleStageStatus,
    completed: int = 0,
    deferred: int = 0,
    failed: int = 0,
    uncertain: int = 0,
    safely_skipped: int = 0,
    summary: dict[str, int] | None = None,
) -> AutomationCycleStageResult:
    return AutomationCycleStageResult.create(
        stage=stage,
        budget=budget,
        status=status,
        public_status=status.value,
        actual_processed=completed + deferred + failed + uncertain,
        completed=completed,
        deferred=deferred,
        failed=failed,
        uncertain=uncertain,
        safely_skipped=safely_skipped,
        summary=summary or {},
    )


def _result(
    command: RunAutomationCycleCommand,
    *,
    operation: AutomationCycleOperationStatus,
    partial: bool = False,
    noop: bool = False,
) -> RunAutomationCycleResult:
    normal = (
        AutomationCycleStageStatus.NOOP
        if noop
        else AutomationCycleStageStatus.COMPLETED
    )
    stages = (
        _stage(
            AutomationCycleStage.PRIORITY_REFRESH,
            command.max_reprioritizations,
            status=normal,
            completed=0 if noop else 1,
            summary={"created": 0 if noop else 1, "unchanged": 0},
        ),
        _stage(
            AutomationCycleStage.APPLICATION_PLAN_CREATION,
            command.max_plan_creations,
            status=normal,
            completed=0 if noop else 2,
            summary={
                "created": 0 if noop else 1,
                "unchanged": 0 if noop else 1,
            },
        ),
        _stage(
            AutomationCycleStage.APPLICATION_PREPARATION,
            command.max_preparations,
            status=(
                AutomationCycleStageStatus.PARTIAL_FAILURE
                if partial
                else normal
            ),
            completed=0 if noop else 1,
            deferred=1 if partial else 0,
            safely_skipped=2 if partial else 0,
            summary={"skipped_human_attention": 2 if partial else 0},
        ),
        _stage(
            AutomationCycleStage.APPLICATION_EXECUTION,
            command.max_executions,
            status=(
                AutomationCycleStageStatus.PARTIAL_FAILURE
                if partial
                else normal
            ),
            completed=0 if noop else 1,
            uncertain=1 if partial else 0,
            summary={"skipped_uncertain": 1 if partial else 0},
        ),
    )
    summary = AutomationCycleSummary(
        actual_processed=sum(item.actual_processed for item in stages),
        completed=sum(item.completed for item in stages),
        deferred=sum(item.deferred for item in stages),
        failed=sum(item.failed for item in stages),
        uncertain=sum(item.uncertain for item in stages),
        safely_skipped=sum(item.safely_skipped for item in stages),
    )
    run_status = {
        AutomationCycleOperationStatus.COMPLETED: (
            AutomationCycleRunStatus.COMPLETED
        ),
        AutomationCycleOperationStatus.UNCHANGED: (
            AutomationCycleRunStatus.COMPLETED
        ),
        AutomationCycleOperationStatus.PARTIAL_FAILURE: (
            AutomationCycleRunStatus.PARTIAL_FAILURE
        ),
        AutomationCycleOperationStatus.NOOP: AutomationCycleRunStatus.NOOP,
    }[operation]
    run = AutomationCycleRun.create(
        command=command,
        stage_results=stages,
        summary=summary,
        overall_status=run_status,
    )
    return RunAutomationCycleResult(operation, run, None)


@pytest.mark.asyncio
async def test_click_calls_p2c10a_once_with_server_budgets_and_no_concurrency(
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(
        command: RunAutomationCycleCommand,
    ) -> RunAutomationCycleResult:
        calls.append(command)
        started.set()
        await release.wait()
        return _result(
            command, operation=AutomationCycleOperationStatus.COMPLETED
        )

    budgets = AutomationCycleUIBudgetConfig(3, 4, 5, 6)
    controller = ContinueAutomationUIController(
        automation_cycle=cycle, clock=lambda: NOW, budgets=budgets
    )
    command = ContinueAutomationUICommand("automation-ui-001")
    first = asyncio.create_task(
        controller.run(context=_context(), command=command)
    )
    await started.wait()
    duplicate = asyncio.create_task(
        controller.run(context=_context(), command=command)
    )
    competing = await controller.run(
        context=_context(),
        command=ContinueAutomationUICommand("automation-ui-002"),
    )
    release.set()

    assert (await first).status is ContinueAutomationUIStatus.COMPLETED
    assert (await duplicate).status is ContinueAutomationUIStatus.COMPLETED
    assert competing.status is ContinueAutomationUIStatus.RUNNING
    assert len(calls) == 1
    assert calls[0].subject_id == SUBJECT
    assert calls[0].now == NOW
    assert calls[0].budgets == (3, 4, 5, 6)


def test_completed_partial_noop_and_unchanged_map_to_safe_stage_summaries(
) -> None:
    command = RunAutomationCycleCommand(
        subject_id=SUBJECT,
        invocation_id="automation-ui-map",
        now=NOW,
        max_reprioritizations=1,
        max_plan_creations=2,
        max_preparations=3,
        max_executions=4,
    )
    mapped = {
        status: map_automation_cycle_result(
            _result(
                command,
                operation=status,
                partial=status
                is AutomationCycleOperationStatus.PARTIAL_FAILURE,
                noop=status is AutomationCycleOperationStatus.NOOP,
            ),
            invocation_id=command.invocation_id,
        )
        for status in (
            AutomationCycleOperationStatus.COMPLETED,
            AutomationCycleOperationStatus.PARTIAL_FAILURE,
            AutomationCycleOperationStatus.NOOP,
            AutomationCycleOperationStatus.UNCHANGED,
        )
    }

    assert mapped[
        AutomationCycleOperationStatus.COMPLETED
    ].summary.plans_created == 1
    partial = mapped[AutomationCycleOperationStatus.PARTIAL_FAILURE]
    assert partial.summary.human_attention_skipped == 2
    assert partial.summary.execution_uncertain == 1
    assert partial.stage_failures == (
        "申请材料准备未完全完成。",
        "申请执行未完全完成。",
    )
    assert "不会自动重试" in partial.message
    assert mapped[
        AutomationCycleOperationStatus.NOOP
    ].status is ContinueAutomationUIStatus.NOOP
    assert mapped[
        AutomationCycleOperationStatus.UNCHANGED
    ].status is ContinueAutomationUIStatus.UNCHANGED
    serialized = repr(partial.to_dict())
    assert "permit" not in serialized.casefold()
    assert "/private/" not in serialized


@pytest.mark.asyncio
async def test_route_replay_uses_same_id_and_ui_is_p2c10a_only() -> None:
    calls: list[RunAutomationCycleCommand] = []

    async def cycle(
        command: RunAutomationCycleCommand,
    ) -> RunAutomationCycleResult:
        calls.append(command)
        operation = (
            AutomationCycleOperationStatus.COMPLETED
            if len(calls) == 1
            else AutomationCycleOperationStatus.UNCHANGED
        )
        return _result(command, operation=operation)

    app.state.automation_cycle_controller = ContinueAutomationUIController(
        automation_cycle=cycle,
        clock=lambda: NOW,
        budgets=AutomationCycleUIBudgetConfig(1, 2, 3, 4),
    )
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/automation-cycle/run",
            "headers": [],
            "query_string": b"subject_id=subject-attacker",
        }
    )
    body = {
        "subject_id": "subject-attacker",
        "invocation_id": "automation-ui-replay",
        "max_executions": 999,
    }
    first = await continue_automatic_application_ui(
        body, request, _context()
    )
    replay = await continue_automatic_application_ui(
        body, request, _context()
    )

    assert first["status"] == "COMPLETED"
    assert replay["status"] == "UNCHANGED"
    assert len(calls) == 2
    assert all(call.subject_id == SUBJECT for call in calls)
    assert all(call.budgets == (1, 2, 3, 4) for call in calls)

    root = Path(__file__).parents[1]
    source = (root / "dashboard/automation_cycle.py").read_text()
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "typing",
        "core.authenticated_subject",
        "core.automation_cycle",
    }
    javascript = (root / "dashboard/static/app.js").read_text()
    template = (root / "dashboard/templates/index.html").read_text()
    assert javascript.count('fetch("/api/automation-cycle/run"') == 1
    assert javascript.count('fetch("/api/job-library/refresh"') == 1
    assert "reuseInvocation: true" in javascript
    assert "继续自动申请" in template
    assert (
        "处理已进入职位库且符合当前策略的岗位；"
        "可能准备材料并提交已获授权的申请。"
    ) in template
    assert "SUBMISSION_UNCERTAIN 不会自动重试。" in template
