"""UI-safe supervisor for authenticated, serial P2c10a automation cycles."""

from __future__ import annotations

import asyncio
import hashlib
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


AUTOMATION_CYCLE_UI_CONFIG_VERSION = "automation-cycle-ui-budgets-v3"
PREVIOUS_AUTOMATION_CYCLE_UI_CONFIG_VERSION = (
    "automation-cycle-ui-budgets-v2"
)


class ContinueAutomationUIStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
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
    max_continuous_cycles: int = 100
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
        if (
            type(self.max_continuous_cycles) is not int
            or not 1 <= self.max_continuous_cycles <= 240
        ):
            raise ValueError("max_continuous_cycles is outside policy")
        if self.contract_version not in {
            PREVIOUS_AUTOMATION_CYCLE_UI_CONFIG_VERSION,
            AUTOMATION_CYCLE_UI_CONFIG_VERSION,
        }:
            raise ValueError("automation budget config version is unsupported")
        object.__setattr__(
            self, "contract_version", AUTOMATION_CYCLE_UI_CONFIG_VERSION
        )
        binding = self.composition_binding.strip()
        if not binding or len(binding) > 240:
            raise ValueError("composition_binding is required")
        object.__setattr__(self, "composition_binding", binding)


@dataclass(frozen=True, slots=True)
class ContinueAutomationUICommand:
    invocation_id: str
    approve_gate_a: bool = False

    def __post_init__(self) -> None:
        invocation_id = self.invocation_id.strip()
        if not invocation_id or len(invocation_id) > 240:
            raise ValueError("invocation_id is required")
        object.__setattr__(self, "invocation_id", invocation_id)
        if type(self.approve_gate_a) is not bool:
            raise TypeError("approve_gate_a must be a boolean")


@dataclass(frozen=True, slots=True)
class StopAutomationUICommand:
    invocation_id: str

    def __post_init__(self) -> None:
        invocation_id = self.invocation_id.strip()
        if not invocation_id or len(invocation_id) > 240:
            raise ValueError("invocation_id is required")
        object.__setattr__(self, "invocation_id", invocation_id)


class AutomationPreflightStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    NOOP = "NOOP"


@dataclass(frozen=True, slots=True)
class AutomationPreflightResult:
    status: AutomationPreflightStatus
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", AutomationPreflightStatus(self.status)
        )
        if self.message is not None:
            message = self.message.strip()
            if not message or len(message) > 1000:
                raise ValueError("preflight message is outside the UI contract")
            object.__setattr__(self, "message", message)


@dataclass(frozen=True, slots=True)
class AutomationPreflightProgress:
    """One public, safe progress heartbeat emitted during preflight."""

    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.message, str):
            raise TypeError("preflight progress message must be a string")
        message = self.message.strip()
        if not message or len(message) > 1000:
            raise ValueError("preflight progress message is outside the UI contract")
        object.__setattr__(self, "message", message)


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
    priority_failed: int = 0
    priority_system_failures: int = 0
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
    execution_not_ready_skipped: int = 0
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
    phase: str | None = None
    stop_requested: bool = False
    cycles_completed: int = 0
    total_jobs: int = 0
    current_job_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "cycle_run_id": self.cycle_run_id,
            "invocation_id": self.invocation_id,
            "message": self.message,
            "phase": self.phase,
            "stage_failures": list(self.stage_failures),
            "stages": [stage.to_dict() for stage in self.stages],
            "started_at": (
                self.started_at.isoformat() if self.started_at else None
            ),
            "status": self.status.value,
            "stop_requested": self.stop_requested,
            "summary": self.summary.to_dict(),
            "cycles_completed": self.cycles_completed,
            "total_jobs": self.total_jobs,
            "current_job_index": self.current_job_index,
        }


AutomationCycleCallable = Callable[
    [RunAutomationCycleCommand],
    Awaitable[RunAutomationCycleResult],
]

AutomationPreflightCallable = Callable[
    ...,
    Awaitable[AutomationPreflightResult],
]
AutomationPreflightProgressObserver = Callable[
    [AutomationPreflightProgress],
    Awaitable[None],
]
AutomationWorkSnapshotCallable = Callable[..., Awaitable[tuple[str, ...]]]


_STAGE_LABELS = {
    AutomationCycleStage.PRIORITY_REFRESH: "Priority refresh",
    AutomationCycleStage.APPLICATION_PLAN_CREATION: "Application plan creation",
    AutomationCycleStage.APPLICATION_PREPARATION: "Application preparation",
    AutomationCycleStage.BUNDLE_ASSEMBLY: "Application bundle assembly",
    AutomationCycleStage.APPLICATION_EXECUTION: "Application execution",
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
            message="The automation service returned an invalid result.",
        )
    run = result.run
    if run is None:
        return ContinueAutomationUIResult(
            ContinueAutomationUIStatus.FAILED,
            invocation_id,
            (),
            AutomationCycleUISummary(),
            message="Automation could not complete safely.",
        )

    by_stage = {item.stage: item for item in run.stage_results}
    priority = by_stage[AutomationCycleStage.PRIORITY_REFRESH]
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
        priority_failed=priority.failed,
        priority_system_failures=_count(
            priority, "continuable_system_failures"
        ),
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
        execution_not_ready_skipped=_count(
            execution, "skipped_not_ready"
        ),
        human_attention_skipped=_count(
            preparation, "skipped_human_attention"
        ),
    )
    failures = tuple(
        f"{_STAGE_LABELS[item.stage]} did not complete."
        for item in run.stage_results
        if item.status
        in {
            AutomationCycleStageStatus.PARTIAL_FAILURE,
            AutomationCycleStageStatus.FAILED,
        }
    )
    status = ContinueAutomationUIStatus(result.status.value)
    message = None
    preparation_blocked_execution = bool(
        (summary.preparation_failed or summary.preparation_deferred)
        and not summary.preparation_completed
        and not summary.bundles_assembled
        and summary.execution_deferred
        and not summary.execution_uncertain
        and not summary.execution_not_ready_skipped
    )
    if summary.execution_uncertain:
        message = "An application has an unknown submission outcome; automatic retry is blocked."
    elif preparation_blocked_execution and summary.preparation_failed:
        message = (
            "Application preparation hit a system or contract issue; this "
            "job was recorded for retry and automatic processing continued."
        )
    elif summary.execution_not_ready_skipped:
        message = (
            "This application is still paused at a required review or system "
            "checkpoint; automatic processing did not move past it."
        )
    elif summary.human_attention_skipped:
        message = "This application needs human attention; automatic processing paused."
    elif summary.preparation_deferred or summary.execution_deferred:
        message = "This application was deferred; automatic processing paused for review."
    elif summary.priority_system_failures:
        message = (
            "Priority evaluation hit a system or contract issue; this job "
            "was recorded for retry and automatic processing continued."
        )
    elif status is ContinueAutomationUIStatus.NOOP:
        message = "There are no jobs ready for automation."
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


@dataclass(slots=True)
class _ActiveAutomationSession:
    invocation_id: str
    started_at: datetime
    stop_event: asyncio.Event
    progress: ContinueAutomationUIResult
    approve_gate_a: bool = False
    task: asyncio.Task[ContinueAutomationUIResult] | None = None


def _add_summaries(
    left: AutomationCycleUISummary,
    right: AutomationCycleUISummary,
) -> AutomationCycleUISummary:
    return AutomationCycleUISummary(
        **{
            name: getattr(left, name) + getattr(right, name)
            for name in left.__dataclass_fields__
        }
    )


def _child_invocation_id(
    *, parent_invocation_id: str, index: int, job_id: str
) -> str:
    digest = hashlib.sha256(
        f"{parent_invocation_id}\0{index}\0{job_id}".encode("utf-8")
    ).hexdigest()
    return f"automation-job-{digest}"


def _validate_work_snapshot(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError("automation work snapshot must be a tuple")
    cleaned = tuple(item.strip() for item in value if isinstance(item, str))
    if (
        len(cleaned) != len(value)
        or any(not item or len(item) > 160 for item in cleaned)
        or len(cleaned) != len(set(cleaned))
    ):
        raise ValueError("automation work snapshot is invalid")
    return cleaned


class ContinueAutomationUIController:
    """Run one pollable, subject-scoped session as bounded serial cycles."""

    def __init__(
        self,
        *,
        automation_cycle: AutomationCycleCallable,
        preflight: AutomationPreflightCallable,
        work_snapshot: AutomationWorkSnapshotCallable,
        clock: Callable[[], datetime],
        budgets: AutomationCycleUIBudgetConfig,
    ) -> None:
        if not all(
            callable(item)
            for item in (automation_cycle, preflight, work_snapshot, clock)
        ):
            raise TypeError("automation dependencies must be callable")
        if not isinstance(budgets, AutomationCycleUIBudgetConfig):
            raise TypeError("budgets must be typed")
        self._automation_cycle = automation_cycle
        self._preflight = preflight
        self._work_snapshot = work_snapshot
        self._clock = clock
        self._budgets = budgets
        self._active: dict[str, _ActiveAutomationSession] = {}
        self._last_result: dict[str, ContinueAutomationUIResult] = {}
        self._resume_after_job_id: dict[str, str] = {}

    async def start(
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
            return active.progress
        previous = self._last_result.get(context.subject_id)
        if previous is not None and previous.invocation_id == command.invocation_id:
            return previous

        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("clock must return a timezone-aware datetime")
        initial = ContinueAutomationUIResult(
            ContinueAutomationUIStatus.RUNNING,
            command.invocation_id,
            (),
            AutomationCycleUISummary(),
            started_at=now,
            message=(
                "Automatic application started. Preparing job-search intent "
                "and refreshing the job library."
            ),
            phase="PREFLIGHT",
        )
        session = _ActiveAutomationSession(
            invocation_id=command.invocation_id,
            started_at=now,
            stop_event=asyncio.Event(),
            progress=initial,
            approve_gate_a=command.approve_gate_a,
        )
        self._active[context.subject_id] = session
        task = asyncio.create_task(
            self._run_session(context=context, session=session)
        )
        session.task = task
        task.add_done_callback(
            lambda completed: self._finalize(
                subject_id=context.subject_id,
                session=session,
                task=completed,
            )
        )
        return initial

    async def run(
        self,
        *,
        context: AuthenticatedSubjectContext,
        command: ContinueAutomationUICommand,
    ) -> ContinueAutomationUIResult:
        """Compatibility alias for the non-blocking start boundary."""

        return await self.start(context=context, command=command)

    async def status(
        self, *, context: AuthenticatedSubjectContext
    ) -> ContinueAutomationUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        active = self._active.get(context.subject_id)
        if active is not None:
            if active.task is not None and active.task.done():
                self._finalize(
                    subject_id=context.subject_id,
                    session=active,
                    task=active.task,
                )
                return self._last_result[context.subject_id]
            return active.progress
        return self._last_result.get(
            context.subject_id,
            ContinueAutomationUIResult(
                ContinueAutomationUIStatus.IDLE,
                "none",
                (),
                AutomationCycleUISummary(),
                message="No automatic application session has run in this server process.",
                phase="IDLE",
            ),
        )

    async def stop(
        self,
        *,
        context: AuthenticatedSubjectContext,
        command: StopAutomationUICommand,
    ) -> ContinueAutomationUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if not isinstance(command, StopAutomationUICommand):
            raise TypeError("command must be typed")
        session = self._active.get(context.subject_id)
        if session is None:
            return await self.status(context=context)
        if session.invocation_id != command.invocation_id:
            return ContinueAutomationUIResult(
                session.progress.status,
                session.invocation_id,
                session.progress.stages,
                session.progress.summary,
                stage_failures=session.progress.stage_failures,
                cycle_run_id=session.progress.cycle_run_id,
                started_at=session.started_at,
                message="A different automatic application session is active.",
                phase=session.progress.phase,
                stop_requested=False,
                cycles_completed=session.progress.cycles_completed,
                total_jobs=session.progress.total_jobs,
                current_job_index=session.progress.current_job_index,
            )
        session.stop_event.set()
        progress = session.progress
        session.progress = ContinueAutomationUIResult(
            ContinueAutomationUIStatus.STOPPING,
            session.invocation_id,
            progress.stages,
            progress.summary,
            stage_failures=progress.stage_failures,
            cycle_run_id=progress.cycle_run_id,
            started_at=session.started_at,
            message=(
                "Stopping after the current application reaches a safe checkpoint."
            ),
            phase="STOPPING",
            stop_requested=True,
            cycles_completed=progress.cycles_completed,
            total_jobs=progress.total_jobs,
            current_job_index=progress.current_job_index,
        )
        return session.progress

    def _finalize(
        self,
        *,
        subject_id: str,
        session: _ActiveAutomationSession,
        task: asyncio.Task[ContinueAutomationUIResult],
    ) -> None:
        current = self._active.get(subject_id)
        if current is not session:
            return
        try:
            result = task.result()
        except asyncio.CancelledError:
            result = ContinueAutomationUIResult(
                ContinueAutomationUIStatus.STOPPED,
                session.invocation_id,
                session.progress.stages,
                session.progress.summary,
                stage_failures=session.progress.stage_failures,
                cycle_run_id=session.progress.cycle_run_id,
                started_at=session.started_at,
                completed_at=self._safe_now(session.started_at),
                message="Automatic application stopped.",
                phase="STOPPED",
                stop_requested=True,
                cycles_completed=session.progress.cycles_completed,
                total_jobs=session.progress.total_jobs,
                current_job_index=session.progress.current_job_index,
            )
        except Exception:
            result = self._terminal_failure(
                session,
                message="The automation service is currently unavailable.",
            )
        self._last_result[subject_id] = result
        self._active.pop(subject_id, None)

    def _safe_now(self, fallback: datetime) -> datetime:
        try:
            value = self._clock()
        except Exception:
            return fallback
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            return fallback
        return value

    def _current_now(self, *, not_before: datetime | None = None) -> datetime:
        """Return operational time or fail closed before downstream work."""

        try:
            value = self._clock()
        except Exception as exc:
            raise RuntimeError("operational clock is unavailable") from exc
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ValueError("clock must return a timezone-aware datetime")
        if not_before is not None and value < not_before:
            raise ValueError("operational clock moved backwards")
        return value

    def _bounded_work(
        self,
        *,
        subject_id: str,
        work: tuple[str, ...],
    ) -> tuple[tuple[str, ...], int]:
        """Resume after the last completed job without replaying one prefix."""

        cursor = self._resume_after_job_id.get(subject_id)
        start = 0
        if cursor is not None:
            try:
                start = work.index(cursor) + 1
            except ValueError:
                self._resume_after_job_id.pop(subject_id, None)
            else:
                if start >= len(work):
                    # The prior bounded suffix is exhausted.  This invocation
                    # has a fresh post-preflight queue snapshot, so scan it as
                    # a new batch instead of hiding jobs inserted before the
                    # old cursor behind an empty slice.
                    self._resume_after_job_id.pop(subject_id, None)
                    start = 0
        end = min(start + self._budgets.max_continuous_cycles, len(work))
        return work[start:end], max(len(work) - end, 0)

    def _terminal_failure(
        self,
        session: _ActiveAutomationSession,
        *,
        message: str,
    ) -> ContinueAutomationUIResult:
        progress = session.progress
        return ContinueAutomationUIResult(
            ContinueAutomationUIStatus.FAILED,
            session.invocation_id,
            progress.stages,
            progress.summary,
            stage_failures=progress.stage_failures,
            cycle_run_id=progress.cycle_run_id,
            started_at=session.started_at,
            completed_at=self._safe_now(session.started_at),
            message=message,
            phase="FAILED",
            stop_requested=session.stop_event.is_set(),
            cycles_completed=progress.cycles_completed,
            total_jobs=progress.total_jobs,
            current_job_index=progress.current_job_index,
        )

    async def _run_session(
        self,
        *,
        context: AuthenticatedSubjectContext,
        session: _ActiveAutomationSession,
    ) -> ContinueAutomationUIResult:
        async def observe_preflight(
            progress: AutomationPreflightProgress,
        ) -> None:
            if not isinstance(progress, AutomationPreflightProgress):
                raise TypeError("preflight progress must be typed")
            if session.stop_event.is_set():
                return
            current = session.progress
            session.progress = ContinueAutomationUIResult(
                ContinueAutomationUIStatus.RUNNING,
                session.invocation_id,
                current.stages,
                current.summary,
                stage_failures=current.stage_failures,
                cycle_run_id=current.cycle_run_id,
                started_at=session.started_at,
                message=progress.message,
                phase="PREFLIGHT",
                cycles_completed=current.cycles_completed,
                total_jobs=current.total_jobs,
                current_job_index=current.current_job_index,
            )

        try:
            preflight = await self._preflight(
                context=context,
                invocation_id=session.invocation_id,
                stop_requested=session.stop_event.is_set,
                progress_observer=observe_preflight,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return self._terminal_failure(
                session,
                message="Automatic application preflight could not complete safely.",
            )
        if not isinstance(preflight, AutomationPreflightResult):
            return self._terminal_failure(
                session,
                message="Automatic application preflight returned an invalid result.",
            )
        if preflight.status is AutomationPreflightStatus.FAILED:
            return self._terminal_failure(
                session,
                message=preflight.message
                or "Automatic application preflight failed.",
            )
        if session.stop_event.is_set():
            return self._stopped_result(session)

        session.progress = ContinueAutomationUIResult(
            ContinueAutomationUIStatus.RUNNING,
            session.invocation_id,
            (),
            AutomationCycleUISummary(),
            started_at=session.started_at,
            message="Loading the ordered job queue.",
            phase="LOADING_QUEUE",
        )
        try:
            work = _validate_work_snapshot(
                await self._work_snapshot(
                    subject_id=context.subject_id,
                    now=self._current_now(not_before=session.started_at),
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return self._terminal_failure(
                session,
                message="The ordered job queue could not be loaded safely.",
            )
        if session.stop_event.is_set():
            return self._stopped_result(session)
        work, jobs_remaining = self._bounded_work(
            subject_id=context.subject_id,
            work=work,
        )
        if not work:
            return ContinueAutomationUIResult(
                ContinueAutomationUIStatus.NOOP,
                session.invocation_id,
                (),
                AutomationCycleUISummary(),
                started_at=session.started_at,
                completed_at=self._safe_now(session.started_at),
                message="There are no jobs ready for automatic application.",
                phase="COMPLETED",
            )

        cumulative = AutomationCycleUISummary()
        failures: list[str] = []
        latest_stages: tuple[AutomationCycleUIStage, ...] = ()
        latest_cycle_id: str | None = None
        any_changed = False
        partial = (
            preflight.status is AutomationPreflightStatus.PARTIAL_FAILURE
        )
        for offset, job_id in enumerate(work):
            index = offset + 1
            if session.stop_event.is_set():
                return self._stopped_result(session)
            session.progress = ContinueAutomationUIResult(
                ContinueAutomationUIStatus.RUNNING,
                session.invocation_id,
                latest_stages,
                cumulative,
                stage_failures=tuple(failures),
                cycle_run_id=latest_cycle_id,
                started_at=session.started_at,
                message=f"Processing job {index} of {len(work)}.",
                phase="PROCESSING",
                cycles_completed=offset,
                total_jobs=len(work),
                current_job_index=index,
            )
            try:
                command = RunAutomationCycleCommand(
                    subject_id=context.subject_id,
                    invocation_id=_child_invocation_id(
                        parent_invocation_id=session.invocation_id,
                        index=index,
                        job_id=job_id,
                    ),
                    now=self._current_now(not_before=session.started_at),
                    max_reprioritizations=min(
                        self._budgets.max_reprioritizations, 1
                    ),
                    max_plan_creations=min(
                        self._budgets.max_plan_creations, 1
                    ),
                    max_preparations=min(
                        self._budgets.max_preparations, 1
                    ),
                    max_bundle_assemblies=min(
                        self._budgets.max_bundle_assemblies, 1
                    ),
                    max_executions=min(
                        self._budgets.max_executions, 1
                    ),
                    composition_binding=self._budgets.composition_binding,
                    target_job_ids=(job_id,),
                    approve_gate_a=session.approve_gate_a,
                )
                cycle_result = await self._automation_cycle(command)
            except (OSError, RuntimeError, TypeError, ValueError):
                return self._terminal_failure(
                    session,
                    message="The current application cycle could not complete safely.",
                )
            mapped = map_automation_cycle_result(
                cycle_result, invocation_id=session.invocation_id
            )
            latest_stages = mapped.stages
            latest_cycle_id = mapped.cycle_run_id
            cumulative = _add_summaries(cumulative, mapped.summary)
            failures.extend(
                item for item in mapped.stage_failures if item not in failures
            )
            any_changed = any_changed or mapped.status not in {
                ContinueAutomationUIStatus.NOOP,
                ContinueAutomationUIStatus.UNCHANGED,
            }
            partial = partial or mapped.status is ContinueAutomationUIStatus.PARTIAL_FAILURE
            session.progress = ContinueAutomationUIResult(
                ContinueAutomationUIStatus.RUNNING,
                session.invocation_id,
                latest_stages,
                cumulative,
                stage_failures=tuple(failures),
                cycle_run_id=latest_cycle_id,
                started_at=session.started_at,
                message=mapped.message or f"Finished job {index} of {len(work)}.",
                phase="PROCESSING",
                stop_requested=session.stop_event.is_set(),
                cycles_completed=index,
                total_jobs=len(work),
                current_job_index=index,
            )
            if mapped.status is ContinueAutomationUIStatus.FAILED:
                return self._terminal_failure(
                    session,
                    message=mapped.message or "The current application failed safely.",
                )
            if self._requires_human_checkpoint(mapped):
                return ContinueAutomationUIResult(
                    ContinueAutomationUIStatus.PARTIAL_FAILURE,
                    session.invocation_id,
                    latest_stages,
                    cumulative,
                    stage_failures=tuple(failures),
                    cycle_run_id=latest_cycle_id,
                    started_at=session.started_at,
                    completed_at=self._safe_now(session.started_at),
                    message=mapped.message
                    or "Automatic application paused for human attention.",
                    phase="NEEDS_ATTENTION",
                    stop_requested=session.stop_event.is_set(),
                    cycles_completed=index,
                    total_jobs=len(work),
                    current_job_index=index,
                )
            self._resume_after_job_id[context.subject_id] = job_id
            if session.stop_event.is_set():
                return self._stopped_result(session)

        if not jobs_remaining:
            # A cursor only carries an unfinished bounded suffix across an
            # explicit Continue action.  Retaining the final job would make a
            # later refreshed queue silently ignore newly inserted prefix jobs.
            self._resume_after_job_id.pop(context.subject_id, None)

        status = (
            ContinueAutomationUIStatus.PARTIAL_FAILURE
            if partial
            else ContinueAutomationUIStatus.COMPLETED
            if any_changed
            else ContinueAutomationUIStatus.NOOP
        )
        return ContinueAutomationUIResult(
            status,
            session.invocation_id,
            latest_stages,
            cumulative,
            stage_failures=tuple(failures),
            cycle_run_id=latest_cycle_id,
            started_at=session.started_at,
            completed_at=self._safe_now(session.started_at),
            message=(
                preflight.message
                if partial and preflight.message
                else (
                    "Automatic application finished the current bounded "
                    f"batch; {jobs_remaining} ordered jobs remain. Continue "
                    "again to resume after this batch."
                    if jobs_remaining
                    else f"Automatic application finished after {len(work)} serial job cycles."
                )
            ),
            phase="COMPLETED",
            cycles_completed=len(work),
            total_jobs=len(work),
            current_job_index=len(work),
        )

    @staticmethod
    def _requires_human_checkpoint(
        result: ContinueAutomationUIResult,
    ) -> bool:
        summary = result.summary
        only_continuable_priority_failure = bool(
            summary.priority_failed
            and summary.priority_failed == summary.priority_system_failures
            and result.stage_failures
            == ("Priority refresh did not complete.",)
        )
        blocking_priority_failure = bool(
            summary.priority_failed and not only_continuable_priority_failure
        )
        preparation_blocked_execution = bool(
            (summary.preparation_failed or summary.preparation_deferred)
            and not summary.preparation_completed
            and not summary.bundles_assembled
            and summary.execution_deferred
            and not summary.execution_uncertain
            and not summary.execution_not_ready_skipped
        )
        material_only_attention = bool(
            summary.preparation_deferred
            and (
                not summary.execution_deferred
                or preparation_blocked_execution
            )
            and not summary.execution_uncertain
            and not summary.execution_not_ready_skipped
        )
        blocking_human_attention = bool(
            summary.human_attention_skipped
            and not material_only_attention
        )
        blocking_execution_deferred = bool(
            summary.execution_deferred
            and not preparation_blocked_execution
        )
        return bool(
            blocking_priority_failure
            or blocking_execution_deferred
            or summary.execution_uncertain
            or summary.execution_not_ready_skipped
            or blocking_human_attention
        )

    def _stopped_result(
        self, session: _ActiveAutomationSession
    ) -> ContinueAutomationUIResult:
        progress = session.progress
        return ContinueAutomationUIResult(
            ContinueAutomationUIStatus.STOPPED,
            session.invocation_id,
            progress.stages,
            progress.summary,
            stage_failures=progress.stage_failures,
            cycle_run_id=progress.cycle_run_id,
            started_at=session.started_at,
            completed_at=self._safe_now(session.started_at),
            message="Automatic application stopped at a safe checkpoint.",
            phase="STOPPED",
            stop_requested=True,
            cycles_completed=progress.cycles_completed,
            total_jobs=progress.total_jobs,
            current_job_index=progress.current_job_index,
        )


__all__ = [
    "AUTOMATION_CYCLE_UI_CONFIG_VERSION",
    "PREVIOUS_AUTOMATION_CYCLE_UI_CONFIG_VERSION",
    "AutomationCycleCallable",
    "AutomationPreflightCallable",
    "AutomationPreflightProgress",
    "AutomationPreflightProgressObserver",
    "AutomationPreflightResult",
    "AutomationPreflightStatus",
    "AutomationWorkSnapshotCallable",
    "AutomationCycleUIBudgetConfig",
    "AutomationCycleUIStage",
    "AutomationCycleUISummary",
    "ContinueAutomationUICommand",
    "ContinueAutomationUIController",
    "ContinueAutomationUIResult",
    "ContinueAutomationUIStatus",
    "StopAutomationUICommand",
    "map_automation_cycle_result",
]
