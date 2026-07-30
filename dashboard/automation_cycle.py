"""UI-safe adapter for one authenticated P2c10a automation cycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.automation_cycle import (
    AutomationCycleOperationStatus,
    AutomationCycleStage,
    AutomationCycleStageStatus,
    RunAutomationCycleCommand,
    RunAutomationCycleResult,
)


AUTOMATION_CYCLE_UI_CONFIG_VERSION = "automation-cycle-ui-budgets-v2"


class ContinueAutomationUIStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    NOOP = "NOOP"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class AutomationCycleUIBudgetConfig:
    max_reprioritizations: int = 10
    max_plan_creations: int = 10
    max_preparations: int = 5
    max_executions: int = 2
    max_bundle_assemblies: int = 5
    composition_binding: str = "jobops-dashboard-automation-v2"
    contract_version: str = AUTOMATION_CYCLE_UI_CONFIG_VERSION

    def __post_init__(self) -> None:
        budgets = (
            self.max_reprioritizations,
            self.max_plan_creations,
            self.max_preparations,
            self.max_executions,
            self.max_bundle_assemblies,
        )
        if any(type(value) is not int or value < 0 for value in budgets):
            raise ValueError("automation budgets must be non-negative")
        if not any(budgets):
            raise ValueError("at least one automation budget must be positive")
        if self.contract_version != AUTOMATION_CYCLE_UI_CONFIG_VERSION:
            raise ValueError("automation budget config version is unsupported")
        binding = self.composition_binding.strip()
        if not binding or len(binding) > 240:
            raise ValueError("composition_binding is required")
        object.__setattr__(self, "composition_binding", binding)


@dataclass(frozen=True, slots=True)
class ContinueAutomationUICommand:
    invocation_id: str

    def __post_init__(self) -> None:
        invocation_id = self.invocation_id.strip()
        if not invocation_id or len(invocation_id) > 240:
            raise ValueError("invocation_id is required")
        object.__setattr__(self, "invocation_id", invocation_id)


@dataclass(frozen=True, slots=True)
class AutomationCycleUIStage:
    stage: str
    status: str
    budget: int
    actual_processed: int
    completed: int
    deferred: int
    failed: int
    uncertain: int
    safely_skipped: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class AutomationCycleUISummary:
    plans_created: int = 0
    plans_reused: int = 0
    preparation_completed: int = 0
    preparation_deferred: int = 0
    preparation_failed: int = 0
    bundles_assembled: int = 0
    bundles_reused: int = 0
    execution_completed: int = 0
    execution_deferred: int = 0
    execution_failed: int = 0
    execution_uncertain: int = 0
    human_attention_skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class ContinueAutomationUIResult:
    status: ContinueAutomationUIStatus
    invocation_id: str
    stages: tuple[AutomationCycleUIStage, ...]
    summary: AutomationCycleUISummary
    stage_failures: tuple[str, ...] = ()
    cycle_run_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "cycle_run_id": self.cycle_run_id,
            "invocation_id": self.invocation_id,
            "message": self.message,
            "stage_failures": list(self.stage_failures),
            "stages": [stage.to_dict() for stage in self.stages],
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "status": self.status.value,
            "summary": self.summary.to_dict(),
        }


AutomationCycleCallable = Callable[
    [RunAutomationCycleCommand],
    Awaitable[RunAutomationCycleResult],
]


_STAGE_LABELS = {
    AutomationCycleStage.PRIORITY_REFRESH: "优先级刷新",
    AutomationCycleStage.APPLICATION_PLAN_CREATION: "申请计划创建",
    AutomationCycleStage.APPLICATION_PREPARATION: "申请材料准备",
    AutomationCycleStage.BUNDLE_ASSEMBLY: "申请包组装",
    AutomationCycleStage.APPLICATION_EXECUTION: "申请执行",
}


def _count(stage: object, name: str) -> int:
    return dict(getattr(stage, "summary", ())).get(name, 0)


def map_automation_cycle_result(
    result: RunAutomationCycleResult,
    *,
    invocation_id: str,
) -> ContinueAutomationUIResult:
    """Project a P2c10a result without exposing internal bindings or errors."""

    if not isinstance(result, RunAutomationCycleResult):
        return ContinueAutomationUIResult(
            ContinueAutomationUIStatus.FAILED,
            invocation_id,
            (),
            AutomationCycleUISummary(),
            message="自动处理服务返回了无效结果。",
        )
    run = result.run
    if run is None:
        return ContinueAutomationUIResult(
            ContinueAutomationUIStatus.FAILED,
            invocation_id,
            (),
            AutomationCycleUISummary(),
            message="自动处理暂时无法完成。",
        )

    by_stage = {item.stage: item for item in run.stage_results}
    plan = by_stage[AutomationCycleStage.APPLICATION_PLAN_CREATION]
    preparation = by_stage[AutomationCycleStage.APPLICATION_PREPARATION]
    bundle = by_stage.get(AutomationCycleStage.BUNDLE_ASSEMBLY)
    execution = by_stage[AutomationCycleStage.APPLICATION_EXECUTION]
    stages = tuple(
        AutomationCycleUIStage(
            stage=item.stage.value,
            status=item.status.value,
            budget=item.budget,
            actual_processed=item.actual_processed,
            completed=item.completed,
            deferred=item.deferred,
            failed=item.failed,
            uncertain=item.uncertain,
            safely_skipped=item.safely_skipped,
        )
        for item in run.stage_results
    )
    summary = AutomationCycleUISummary(
        plans_created=_count(plan, "created"),
        plans_reused=_count(plan, "unchanged"),
        preparation_completed=preparation.completed,
        preparation_deferred=preparation.deferred,
        preparation_failed=preparation.failed,
        bundles_assembled=(
            _count(bundle, "assembled") if bundle is not None else 0
        ),
        bundles_reused=(
            _count(bundle, "unchanged") if bundle is not None else 0
        ),
        execution_completed=execution.completed,
        execution_deferred=execution.deferred,
        execution_failed=execution.failed,
        execution_uncertain=execution.uncertain,
        human_attention_skipped=_count(
            preparation, "skipped_human_attention"
        ),
    )
    failures = tuple(
        f"{_STAGE_LABELS[item.stage]}未完全完成。"
        for item in run.stage_results
        if item.status
        in {
            AutomationCycleStageStatus.PARTIAL_FAILURE,
            AutomationCycleStageStatus.FAILED,
        }
    )
    status = ContinueAutomationUIStatus(result.status.value)
    message = None
    if status is ContinueAutomationUIStatus.NOOP:
        message = "当前没有可自动处理的职位。"
    elif summary.execution_uncertain:
        message = "存在提交结果不确定的申请；系统不会自动重试。"
    elif summary.human_attention_skipped:
        message = "需要人工处理的职位已跳过；其他职位继续处理。"
    elif summary.preparation_deferred or summary.execution_deferred:
        message = "延后项不会阻塞其他职位。"
    return ContinueAutomationUIResult(
        status=status,
        invocation_id=invocation_id,
        stages=stages,
        summary=summary,
        stage_failures=failures,
        cycle_run_id=run.cycle_id,
        started_at=run.started_at,
        completed_at=run.completed_at,
        message=message,
    )


class ContinueAutomationUIController:
    """One authenticated UI action backed only by P2c10a."""

    def __init__(
        self,
        *,
        automation_cycle: AutomationCycleCallable,
        clock: Callable[[], datetime],
        budgets: AutomationCycleUIBudgetConfig,
    ) -> None:
        if not callable(automation_cycle) or not callable(clock):
            raise TypeError("automation_cycle and clock must be callable")
        if not isinstance(budgets, AutomationCycleUIBudgetConfig):
            raise TypeError("budgets must be typed")
        self._automation_cycle = automation_cycle
        self._clock = clock
        self._budgets = budgets
        self._active: dict[
            str, tuple[str, asyncio.Task[ContinueAutomationUIResult]]
        ] = {}

    async def run(
        self,
        *,
        context: AuthenticatedSubjectContext,
        command: ContinueAutomationUICommand,
    ) -> ContinueAutomationUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if not isinstance(command, ContinueAutomationUICommand):
            raise TypeError("command must be typed")

        active = self._active.get(context.subject_id)
        if active is not None:
            active_invocation, task = active
            if active_invocation == command.invocation_id:
                return await asyncio.shield(task)
            return ContinueAutomationUIResult(
                ContinueAutomationUIStatus.RUNNING,
                active_invocation,
                (),
                AutomationCycleUISummary(),
                message="自动处理正在进行中。",
            )

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        task = asyncio.create_task(
            self._invoke(
                subject_id=context.subject_id,
                invocation_id=command.invocation_id,
                now=now,
            )
        )
        self._active[context.subject_id] = (command.invocation_id, task)
        try:
            return await asyncio.shield(task)
        finally:
            current = self._active.get(context.subject_id)
            if current is not None and current[1] is task and task.done():
                self._active.pop(context.subject_id, None)

    async def _invoke(
        self,
        *,
        subject_id: str,
        invocation_id: str,
        now: datetime,
    ) -> ContinueAutomationUIResult:
        command = RunAutomationCycleCommand(
            subject_id=subject_id,
            invocation_id=invocation_id,
            now=now,
            max_reprioritizations=self._budgets.max_reprioritizations,
            max_plan_creations=self._budgets.max_plan_creations,
            max_preparations=self._budgets.max_preparations,
            max_bundle_assemblies=self._budgets.max_bundle_assemblies,
            max_executions=self._budgets.max_executions,
            composition_binding=self._budgets.composition_binding,
        )
        try:
            result = await self._automation_cycle(command)
        except (OSError, RuntimeError, TypeError, ValueError):
            return ContinueAutomationUIResult(
                ContinueAutomationUIStatus.FAILED,
                invocation_id,
                (),
                AutomationCycleUISummary(),
                message="自动处理服务暂时不可用。",
            )
        return map_automation_cycle_result(
            result, invocation_id=invocation_id
        )


__all__ = [
    "AUTOMATION_CYCLE_UI_CONFIG_VERSION",
    "AutomationCycleCallable",
    "AutomationCycleUIBudgetConfig",
    "AutomationCycleUIStage",
    "AutomationCycleUISummary",
    "ContinueAutomationUICommand",
    "ContinueAutomationUIController",
    "ContinueAutomationUIResult",
    "ContinueAutomationUIStatus",
    "map_automation_cycle_result",
]
