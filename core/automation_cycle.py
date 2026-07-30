"""One bounded, serial end-to-end Jobops automation cycle."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from .private_home import PrivateHome, PrivateHomeError
from .selective_batch_execution import (
    SELECTIVE_BATCH_EXECUTION_CONTRACT_VERSION,
    SelectiveBatchExecutionCommand,
    SelectiveBatchExecutionResult,
    SelectiveBatchExecutionStatus,
)
from .selective_batch_plan_creation import (
    SELECTIVE_BATCH_PLAN_CREATION_CONTRACT_VERSION,
    SelectiveBatchPlanCreationCommand,
    SelectiveBatchPlanCreationResult,
    SelectiveBatchPlanCreationStatus,
)
from .selective_batch_preparation import (
    SELECTIVE_BATCH_PREPARATION_CONTRACT_VERSION,
    SelectiveBatchPreparationCommand,
    SelectiveBatchPreparationResult,
    SelectiveBatchPreparationStatus,
)
from .selective_reprioritization import (
    SelectiveBatchOverallStatus,
    SelectiveBatchReprioritizationCommand,
    SelectiveBatchReprioritizationResult,
)


AUTOMATION_CYCLE_CONTRACT_VERSION = "end-to-end-automation-cycle-v1"
PRIORITY_REFRESH_PUBLIC_CONTRACT = "selective-batch-reprioritization-v1"
_ID_RE = re.compile(r"automation-cycle-[0-9a-f]{64}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _clean(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the automation cycle contract")
    return cleaned


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("persisted cycle timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware("persisted cycle timestamp", parsed)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _subject_key(subject_id: str) -> str:
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


class AutomationCycleStage(StrEnum):
    PRIORITY_REFRESH = "PRIORITY_REFRESH"
    APPLICATION_PLAN_CREATION = "APPLICATION_PLAN_CREATION"
    APPLICATION_PREPARATION = "APPLICATION_PREPARATION"
    APPLICATION_EXECUTION = "APPLICATION_EXECUTION"


class AutomationCycleStageStatus(StrEnum):
    SKIPPED_BUDGET_ZERO = "SKIPPED_BUDGET_ZERO"
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class AutomationCycleRunStatus(StrEnum):
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class AutomationCycleOperationStatus(StrEnum):
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class AutomationCycleFailureReason(StrEnum):
    REPOSITORY_FAILURE = "REPOSITORY_FAILURE"
    REPLAY_INTEGRITY_FAILURE = "REPLAY_INTEGRITY_FAILURE"
    STAGE_RESULT_INVALID = "STAGE_RESULT_INVALID"


class AutomationCycleReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class AutomationCycleWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RunAutomationCycleCommand:
    subject_id: str
    invocation_id: str
    now: datetime
    max_reprioritizations: int
    max_plan_creations: int
    max_preparations: int
    max_executions: int
    composition_binding: str = "jobops-default-composition-v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean("subject_id", self.subject_id, 160)
        )
        object.__setattr__(
            self, "invocation_id", _clean("invocation_id", self.invocation_id)
        )
        object.__setattr__(
            self,
            "composition_binding",
            _clean("composition_binding", self.composition_binding),
        )
        _aware("now", self.now)
        budgets = (
            self.max_reprioritizations,
            self.max_plan_creations,
            self.max_preparations,
            self.max_executions,
        )
        if any(type(value) is not int or value < 0 for value in budgets):
            raise ValueError("cycle budgets must be non-negative integers")
        if not any(budgets):
            raise ValueError("at least one cycle budget must be positive")

    @property
    def budgets(self) -> tuple[int, int, int, int]:
        return (
            self.max_reprioritizations,
            self.max_plan_creations,
            self.max_preparations,
            self.max_executions,
        )


@dataclass(frozen=True, slots=True)
class AutomationCycleStageResult:
    stage: AutomationCycleStage
    budget: int
    status: AutomationCycleStageStatus
    public_status: str
    actual_processed: int
    completed: int
    deferred: int
    failed: int
    uncertain: int
    safely_skipped: int
    summary: tuple[tuple[str, int], ...]
    stage_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage", AutomationCycleStage(self.stage))
        object.__setattr__(
            self, "status", AutomationCycleStageStatus(self.status)
        )
        _clean("public_status", self.public_status, 120)
        for name in (
            "budget",
            "actual_processed",
            "completed",
            "deferred",
            "failed",
            "uncertain",
            "safely_skipped",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be non-negative")
        if not isinstance(self.summary, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or type(item[1]) is not int
            or item[1] < 0
            for item in self.summary
        ):
            raise TypeError("stage summary must be typed counts")
        if tuple(sorted(self.summary)) != self.summary:
            raise ValueError("stage summary must be canonically ordered")
        if _HASH_RE.fullmatch(self.stage_hash) is None:
            raise ValueError("stage_hash is invalid")
        if self.stage_hash != _hash(self.identity_dict()):
            raise ValueError("stage_hash does not match stage content")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "actual_processed": self.actual_processed,
            "budget": self.budget,
            "completed": self.completed,
            "deferred": self.deferred,
            "failed": self.failed,
            "public_status": self.public_status,
            "safely_skipped": self.safely_skipped,
            "stage": self.stage.value,
            "status": self.status.value,
            "summary": dict(self.summary),
            "uncertain": self.uncertain,
        }

    @classmethod
    def create(
        cls,
        *,
        stage: AutomationCycleStage,
        budget: int,
        status: AutomationCycleStageStatus,
        public_status: str,
        actual_processed: int,
        completed: int,
        deferred: int,
        failed: int,
        uncertain: int,
        safely_skipped: int,
        summary: Mapping[str, int],
    ) -> "AutomationCycleStageResult":
        ordered = tuple(sorted(summary.items()))
        values = {
            "stage": stage,
            "budget": budget,
            "status": status,
            "public_status": public_status,
            "actual_processed": actual_processed,
            "completed": completed,
            "deferred": deferred,
            "failed": failed,
            "uncertain": uncertain,
            "safely_skipped": safely_skipped,
            "summary": ordered,
        }
        payload = {
            "actual_processed": actual_processed,
            "budget": budget,
            "completed": completed,
            "deferred": deferred,
            "failed": failed,
            "public_status": public_status,
            "safely_skipped": safely_skipped,
            "stage": stage.value,
            "status": status.value,
            "summary": dict(ordered),
            "uncertain": uncertain,
        }
        return cls(**values, stage_hash=_hash(payload))


@dataclass(frozen=True, slots=True)
class AutomationCycleSummary:
    actual_processed: int
    completed: int
    deferred: int
    failed: int
    uncertain: int
    safely_skipped: int

    def __post_init__(self) -> None:
        for name in (
            "actual_processed",
            "completed",
            "deferred",
            "failed",
            "uncertain",
            "safely_skipped",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")


def _cycle_binding(command: RunAutomationCycleCommand) -> dict[str, Any]:
    return {
        "budgets": list(command.budgets),
        "composition_binding": command.composition_binding,
        "contract_version": AUTOMATION_CYCLE_CONTRACT_VERSION,
        "invocation_id": command.invocation_id,
        "service_contracts": {
            "application_execution": SELECTIVE_BATCH_EXECUTION_CONTRACT_VERSION,
            "application_plan_creation": (
                SELECTIVE_BATCH_PLAN_CREATION_CONTRACT_VERSION
            ),
            "application_preparation": (
                SELECTIVE_BATCH_PREPARATION_CONTRACT_VERSION
            ),
            "priority_refresh": PRIORITY_REFRESH_PUBLIC_CONTRACT,
        },
        "subject_id": command.subject_id,
    }


@dataclass(frozen=True, slots=True)
class AutomationCycleRun:
    cycle_id: str
    contract_version: str
    cycle_binding_hash: str
    invocation_id: str
    composition_binding: str
    subject_id: str
    budgets: tuple[int, int, int, int]
    stage_results: tuple[AutomationCycleStageResult, ...]
    summary: AutomationCycleSummary
    overall_status: AutomationCycleRunStatus
    run_hash: str
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        if _ID_RE.fullmatch(self.cycle_id) is None:
            raise ValueError("cycle_id is invalid")
        if self.contract_version != AUTOMATION_CYCLE_CONTRACT_VERSION:
            raise ValueError("cycle contract version is unsupported")
        if _HASH_RE.fullmatch(self.cycle_binding_hash) is None:
            raise ValueError("cycle binding hash is invalid")
        if self.cycle_id != f"automation-cycle-{self.cycle_binding_hash}":
            raise ValueError("cycle ID does not match its binding")
        _clean("invocation_id", self.invocation_id)
        _clean("composition_binding", self.composition_binding)
        _clean("subject_id", self.subject_id, 160)
        if (
            not isinstance(self.budgets, tuple)
            or len(self.budgets) != 4
            or any(type(value) is not int or value < 0 for value in self.budgets)
            or not any(self.budgets)
        ):
            raise ValueError("cycle budgets are invalid")
        expected_binding = _hash(
            {
                "budgets": list(self.budgets),
                "composition_binding": self.composition_binding,
                "contract_version": self.contract_version,
                "invocation_id": self.invocation_id,
                "service_contracts": {
                    "application_execution": (
                        SELECTIVE_BATCH_EXECUTION_CONTRACT_VERSION
                    ),
                    "application_plan_creation": (
                        SELECTIVE_BATCH_PLAN_CREATION_CONTRACT_VERSION
                    ),
                    "application_preparation": (
                        SELECTIVE_BATCH_PREPARATION_CONTRACT_VERSION
                    ),
                    "priority_refresh": PRIORITY_REFRESH_PUBLIC_CONTRACT,
                },
                "subject_id": self.subject_id,
            }
        )
        if self.cycle_binding_hash != expected_binding:
            raise ValueError("cycle binding hash is inconsistent")
        expected_stages = tuple(AutomationCycleStage)
        if (
            not isinstance(self.stage_results, tuple)
            or tuple(item.stage for item in self.stage_results)
            != expected_stages
        ):
            raise ValueError("cycle stage lineage is invalid")
        if tuple(item.budget for item in self.stage_results) != self.budgets:
            raise ValueError("cycle stage budgets are inconsistent")
        if not isinstance(self.summary, AutomationCycleSummary):
            raise TypeError("cycle summary must be typed")
        object.__setattr__(
            self, "overall_status", AutomationCycleRunStatus(self.overall_status)
        )
        _aware("started_at", self.started_at)
        _aware("completed_at", self.completed_at)
        if self.completed_at < self.started_at:
            raise ValueError("cycle completion precedes its start")
        if _HASH_RE.fullmatch(self.run_hash) is None:
            raise ValueError("run_hash is invalid")
        if self.run_hash != _hash(self.content_dict(include_hash=False)):
            raise ValueError("run_hash does not match cycle content")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "budgets": list(self.budgets),
            "composition_binding": self.composition_binding,
            "contract_version": self.contract_version,
            "cycle_binding_hash": self.cycle_binding_hash,
            "cycle_id": self.cycle_id,
            "invocation_id": self.invocation_id,
            "subject_id": self.subject_id,
        }

    def content_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            **self.identity_dict(),
            "completed_at": _time(self.completed_at),
            "overall_status": self.overall_status.value,
            "stage_results": [
                {**item.identity_dict(), "stage_hash": item.stage_hash}
                for item in self.stage_results
            ],
            "started_at": _time(self.started_at),
            "summary": {
                "actual_processed": self.summary.actual_processed,
                "completed": self.summary.completed,
                "deferred": self.summary.deferred,
                "failed": self.summary.failed,
                "safely_skipped": self.summary.safely_skipped,
                "uncertain": self.summary.uncertain,
            },
        }
        if include_hash:
            value["run_hash"] = self.run_hash
        return value

    @classmethod
    def create(
        cls,
        *,
        command: RunAutomationCycleCommand,
        stage_results: tuple[AutomationCycleStageResult, ...],
        summary: AutomationCycleSummary,
        overall_status: AutomationCycleRunStatus,
    ) -> "AutomationCycleRun":
        binding_hash = _hash(_cycle_binding(command))
        cycle_id = f"automation-cycle-{binding_hash}"
        values = {
            "cycle_id": cycle_id,
            "contract_version": AUTOMATION_CYCLE_CONTRACT_VERSION,
            "cycle_binding_hash": binding_hash,
            "invocation_id": command.invocation_id,
            "composition_binding": command.composition_binding,
            "subject_id": command.subject_id,
            "budgets": command.budgets,
            "stage_results": stage_results,
            "summary": summary,
            "overall_status": overall_status,
            "started_at": command.now,
            "completed_at": command.now,
        }
        content = {
            "budgets": list(command.budgets),
            "completed_at": _time(command.now),
            "composition_binding": command.composition_binding,
            "contract_version": AUTOMATION_CYCLE_CONTRACT_VERSION,
            "cycle_binding_hash": binding_hash,
            "cycle_id": cycle_id,
            "invocation_id": command.invocation_id,
            "overall_status": overall_status.value,
            "stage_results": [
                {**item.identity_dict(), "stage_hash": item.stage_hash}
                for item in stage_results
            ],
            "started_at": _time(command.now),
            "subject_id": command.subject_id,
            "summary": {
                "actual_processed": summary.actual_processed,
                "completed": summary.completed,
                "deferred": summary.deferred,
                "failed": summary.failed,
                "safely_skipped": summary.safely_skipped,
                "uncertain": summary.uncertain,
            },
        }
        return cls(**values, run_hash=_hash(content))


@dataclass(frozen=True, slots=True)
class AutomationCycleReadResult:
    status: AutomationCycleReadStatus
    run: AutomationCycleRun | None


@dataclass(frozen=True, slots=True)
class AutomationCycleWriteResult:
    status: AutomationCycleWriteStatus
    run: AutomationCycleRun | None


@dataclass(frozen=True, slots=True)
class RunAutomationCycleResult:
    status: AutomationCycleOperationStatus
    run: AutomationCycleRun | None
    reason: AutomationCycleFailureReason | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", AutomationCycleOperationStatus(self.status)
        )
        if self.reason is not None:
            object.__setattr__(
                self, "reason", AutomationCycleFailureReason(self.reason)
            )
        if self.status in {
            AutomationCycleOperationStatus.NOOP,
            AutomationCycleOperationStatus.COMPLETED,
            AutomationCycleOperationStatus.UNCHANGED,
            AutomationCycleOperationStatus.PARTIAL_FAILURE,
        }:
            if self.run is None or self.reason is not None:
                raise ValueError("successful cycle result is malformed")
        elif (self.run is None) == (self.reason is None):
            raise ValueError("failed cycle result is malformed")


class AutomationCycleRunRepository(Protocol):
    def get(
        self, *, subject_id: str, cycle_id: str
    ) -> AutomationCycleReadResult: ...

    def save(self, run: AutomationCycleRun) -> AutomationCycleWriteResult: ...


def _stage_from_dict(value: Any) -> AutomationCycleStageResult:
    if not isinstance(value, Mapping):
        raise TypeError("persisted cycle stage must be an object")
    expected = {
        "actual_processed",
        "budget",
        "completed",
        "deferred",
        "failed",
        "public_status",
        "safely_skipped",
        "stage",
        "stage_hash",
        "status",
        "summary",
        "uncertain",
    }
    if set(value) != expected:
        raise ValueError("persisted cycle stage fields are invalid")
    summary = value["summary"]
    if not isinstance(summary, Mapping):
        raise TypeError("persisted cycle stage summary must be an object")
    return AutomationCycleStageResult(
        stage=AutomationCycleStage(value["stage"]),
        budget=value["budget"],
        status=AutomationCycleStageStatus(value["status"]),
        public_status=value["public_status"],
        actual_processed=value["actual_processed"],
        completed=value["completed"],
        deferred=value["deferred"],
        failed=value["failed"],
        uncertain=value["uncertain"],
        safely_skipped=value["safely_skipped"],
        summary=tuple(sorted(summary.items())),
        stage_hash=value["stage_hash"],
    )


def _run_from_dict(value: Any) -> AutomationCycleRun:
    if not isinstance(value, Mapping):
        raise TypeError("persisted cycle run must be an object")
    expected = {
        "budgets",
        "completed_at",
        "composition_binding",
        "contract_version",
        "cycle_binding_hash",
        "cycle_id",
        "invocation_id",
        "overall_status",
        "run_hash",
        "stage_results",
        "started_at",
        "subject_id",
        "summary",
    }
    if set(value) != expected:
        raise ValueError("persisted cycle run fields are invalid")
    summary = value["summary"]
    if not isinstance(summary, Mapping):
        raise TypeError("persisted cycle summary must be an object")
    if set(summary) != {
        "actual_processed",
        "completed",
        "deferred",
        "failed",
        "safely_skipped",
        "uncertain",
    }:
        raise ValueError("persisted cycle summary fields are invalid")
    return AutomationCycleRun(
        cycle_id=value["cycle_id"],
        contract_version=value["contract_version"],
        cycle_binding_hash=value["cycle_binding_hash"],
        invocation_id=value["invocation_id"],
        composition_binding=value["composition_binding"],
        subject_id=value["subject_id"],
        budgets=tuple(value["budgets"]),
        stage_results=tuple(
            _stage_from_dict(item) for item in value["stage_results"]
        ),
        summary=AutomationCycleSummary(**dict(summary)),
        overall_status=AutomationCycleRunStatus(value["overall_status"]),
        run_hash=value["run_hash"],
        started_at=_parse_time(value["started_at"]),
        completed_at=_parse_time(value["completed_at"]),
    )


class PrivateHomeAutomationCycleRunRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        subject = _clean("subject_id", subject_id, 160)
        return (
            self._home.root
            / "state"
            / "automation"
            / "automation-cycle-runs"
            / _subject_key(subject)
        )

    def _path(self, subject_id: str, cycle_id: str) -> Path:
        if not isinstance(cycle_id, str) or _ID_RE.fullmatch(cycle_id) is None:
            raise ValueError("cycle_id is invalid")
        return self._directory(subject_id) / f"{cycle_id}.json"

    def get(
        self, *, subject_id: str, cycle_id: str
    ) -> AutomationCycleReadResult:
        path = self._path(subject_id, cycle_id)
        with self._lock:
            if not path.exists():
                return AutomationCycleReadResult(
                    AutomationCycleReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return AutomationCycleReadResult(
                    AutomationCycleReadStatus.INTEGRITY_FAILURE, None
                )
            try:
                run = _run_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return AutomationCycleReadResult(
                    AutomationCycleReadStatus.INTEGRITY_FAILURE, None
                )
            if run.subject_id != subject_id.strip() or run.cycle_id != cycle_id:
                return AutomationCycleReadResult(
                    AutomationCycleReadStatus.INTEGRITY_FAILURE, None
                )
            return AutomationCycleReadResult(
                AutomationCycleReadStatus.FOUND, run
            )

    def save(self, run: AutomationCycleRun) -> AutomationCycleWriteResult:
        if not isinstance(run, AutomationCycleRun):
            raise TypeError("run must be typed")
        path = self._path(run.subject_id, run.cycle_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            run.content_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return AutomationCycleWriteResult(
                    AutomationCycleWriteStatus.FAILED, None
                )
            if created:
                return AutomationCycleWriteResult(
                    AutomationCycleWriteStatus.CREATED, run
                )
            existing = self.get(
                subject_id=run.subject_id, cycle_id=run.cycle_id
            )
            if (
                existing.status is AutomationCycleReadStatus.FOUND
                and existing.run is not None
                and existing.run.content_dict() == run.content_dict()
            ):
                return AutomationCycleWriteResult(
                    AutomationCycleWriteStatus.UNCHANGED, existing.run
                )
            return AutomationCycleWriteResult(
                AutomationCycleWriteStatus.FAILED, None
            )


class PriorityRefreshCallable(Protocol):
    def __call__(
        self, command: SelectiveBatchReprioritizationCommand
    ) -> (
        SelectiveBatchReprioritizationResult
        | Awaitable[SelectiveBatchReprioritizationResult]
    ): ...


class PlanCreationCallable(Protocol):
    def __call__(
        self, command: SelectiveBatchPlanCreationCommand
    ) -> (
        SelectiveBatchPlanCreationResult
        | Awaitable[SelectiveBatchPlanCreationResult]
    ): ...


class PreparationCallable(Protocol):
    def __call__(
        self, command: SelectiveBatchPreparationCommand
    ) -> (
        SelectiveBatchPreparationResult
        | Awaitable[SelectiveBatchPreparationResult]
    ): ...


class ExecutionCallable(Protocol):
    def __call__(
        self, command: SelectiveBatchExecutionCommand
    ) -> (
        SelectiveBatchExecutionResult
        | Awaitable[SelectiveBatchExecutionResult]
    ): ...


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _skipped(
    stage: AutomationCycleStage, budget: int
) -> AutomationCycleStageResult:
    return AutomationCycleStageResult.create(
        stage=stage,
        budget=budget,
        status=AutomationCycleStageStatus.SKIPPED_BUDGET_ZERO,
        public_status="SKIPPED_BUDGET_ZERO",
        actual_processed=0,
        completed=0,
        deferred=0,
        failed=0,
        uncertain=0,
        safely_skipped=0,
        summary={},
    )


def _failed_stage(
    stage: AutomationCycleStage, budget: int, public_status: str
) -> AutomationCycleStageResult:
    return AutomationCycleStageResult.create(
        stage=stage,
        budget=budget,
        status=AutomationCycleStageStatus.FAILED,
        public_status=public_status,
        actual_processed=0,
        completed=0,
        deferred=0,
        failed=1,
        uncertain=0,
        safely_skipped=0,
        summary={},
    )


def _stage_status(value: str) -> AutomationCycleStageStatus:
    return {
        "NOOP": AutomationCycleStageStatus.NOOP,
        "COMPLETED": AutomationCycleStageStatus.COMPLETED,
        "PARTIAL_FAILURE": AutomationCycleStageStatus.PARTIAL_FAILURE,
        "FAILED": AutomationCycleStageStatus.FAILED,
    }[value]


def _counts(value: Any) -> dict[str, int]:
    return {
        field.name: getattr(value, field.name)
        for field in fields(value)
        if field.name != "status"
        for item in (getattr(value, field.name),)
        if type(item) is int and item >= 0
    }


def _priority_stage(
    budget: int, result: Any
) -> AutomationCycleStageResult:
    if not isinstance(result, SelectiveBatchReprioritizationResult):
        raise TypeError("P1d3 returned an invalid result")
    summary = _counts(result.summary)
    public = SelectiveBatchOverallStatus(result.overall_status).value
    return AutomationCycleStageResult.create(
        stage=AutomationCycleStage.PRIORITY_REFRESH,
        budget=budget,
        status=_stage_status(public),
        public_status=public,
        actual_processed=summary["selected"],
        completed=summary["created"] + summary["unchanged"],
        deferred=0,
        failed=summary["failed"],
        uncertain=0,
        safely_skipped=(
            summary["skipped_current"]
            + summary["skipped_incomplete"]
            + summary["not_found"]
        ),
        summary=summary,
    )


def _plan_stage(budget: int, result: Any) -> AutomationCycleStageResult:
    if not isinstance(result, SelectiveBatchPlanCreationResult):
        raise TypeError("P2a1b returned an invalid result")
    summary = _counts(result.summary)
    public = SelectiveBatchPlanCreationStatus(result.status).value
    return AutomationCycleStageResult.create(
        stage=AutomationCycleStage.APPLICATION_PLAN_CREATION,
        budget=budget,
        status=_stage_status(public),
        public_status=public,
        actual_processed=summary["selected"],
        completed=summary["created"] + summary["unchanged"],
        deferred=0,
        failed=summary["failed"],
        uncertain=0,
        safely_skipped=(
            summary["skipped_not_runnable"] + summary["not_found"]
        ),
        summary=summary,
    )


def _preparation_stage(
    budget: int, result: Any
) -> AutomationCycleStageResult:
    if not isinstance(result, SelectiveBatchPreparationResult):
        raise TypeError("P2b6 returned an invalid result")
    summary = _counts(result.summary)
    public = SelectiveBatchPreparationStatus(result.status).value
    return AutomationCycleStageResult.create(
        stage=AutomationCycleStage.APPLICATION_PREPARATION,
        budget=budget,
        status=_stage_status(public),
        public_status=public,
        actual_processed=summary["selected"],
        completed=summary["completed"] + summary["unchanged"],
        deferred=summary["deferred"],
        failed=summary["failed"],
        uncertain=0,
        safely_skipped=(
            summary["skipped_human_attention"] + summary["not_found"]
        ),
        summary=summary,
    )


def _execution_stage(
    budget: int, result: Any
) -> AutomationCycleStageResult:
    if not isinstance(result, SelectiveBatchExecutionResult):
        raise TypeError("P2c9 returned an invalid result")
    summary = _counts(result.summary)
    public = SelectiveBatchExecutionStatus(result.status).value
    return AutomationCycleStageResult.create(
        stage=AutomationCycleStage.APPLICATION_EXECUTION,
        budget=budget,
        status=_stage_status(public),
        public_status=public,
        actual_processed=summary["selected"],
        completed=summary["completed"] + summary["unchanged"],
        deferred=summary["deferred"],
        failed=summary["failed"],
        uncertain=summary["uncertain"] + summary["skipped_uncertain"],
        safely_skipped=(
            summary["skipped_not_ready"]
            + summary["skipped_submitted"]
            + summary["not_found"]
        ),
        summary=summary,
    )


def _summarize(
    stages: tuple[AutomationCycleStageResult, ...],
) -> AutomationCycleSummary:
    return AutomationCycleSummary(
        actual_processed=sum(item.actual_processed for item in stages),
        completed=sum(item.completed for item in stages),
        deferred=sum(item.deferred for item in stages),
        failed=sum(item.failed for item in stages),
        uncertain=sum(item.uncertain for item in stages),
        safely_skipped=sum(item.safely_skipped for item in stages),
    )


def _overall(
    stages: tuple[AutomationCycleStageResult, ...],
    summary: AutomationCycleSummary,
) -> AutomationCycleRunStatus:
    enabled = tuple(
        item
        for item in stages
        if item.status is not AutomationCycleStageStatus.SKIPPED_BUDGET_ZERO
    )
    if enabled and all(
        item.status is AutomationCycleStageStatus.FAILED for item in enabled
    ):
        return AutomationCycleRunStatus.FAILED
    if any(
        item.status is AutomationCycleStageStatus.FAILED for item in enabled
    ) and summary.actual_processed == 0:
        return AutomationCycleRunStatus.PARTIAL_FAILURE
    if summary.actual_processed == 0:
        return AutomationCycleRunStatus.NOOP
    adverse = summary.deferred + summary.failed + summary.uncertain
    if adverse or any(
        item.status is AutomationCycleStageStatus.PARTIAL_FAILURE
        for item in enabled
    ):
        return AutomationCycleRunStatus.PARTIAL_FAILURE
    return AutomationCycleRunStatus.COMPLETED


async def run_automation_cycle(
    command: RunAutomationCycleCommand,
    *,
    priority_refresh: PriorityRefreshCallable,
    plan_creation: PlanCreationCallable,
    preparation: PreparationCallable,
    execution: ExecutionCallable,
    repository: AutomationCycleRunRepository,
) -> RunAutomationCycleResult:
    """Run each enabled public batch stage once in fixed serial order."""

    if not isinstance(command, RunAutomationCycleCommand):
        raise TypeError("command must be a RunAutomationCycleCommand")
    binding_hash = _hash(_cycle_binding(command))
    cycle_id = f"automation-cycle-{binding_hash}"
    try:
        existing = repository.get(
            subject_id=command.subject_id, cycle_id=cycle_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return RunAutomationCycleResult(
            AutomationCycleOperationStatus.FAILED,
            None,
            AutomationCycleFailureReason.REPOSITORY_FAILURE,
        )
    if not isinstance(existing, AutomationCycleReadResult):
        return RunAutomationCycleResult(
            AutomationCycleOperationStatus.FAILED,
            None,
            AutomationCycleFailureReason.REPLAY_INTEGRITY_FAILURE,
        )
    if existing.status is AutomationCycleReadStatus.FOUND:
        if (
            existing.run is None
            or existing.run.cycle_binding_hash != binding_hash
            or existing.run.subject_id != command.subject_id
        ):
            return RunAutomationCycleResult(
                AutomationCycleOperationStatus.FAILED,
                None,
                AutomationCycleFailureReason.REPLAY_INTEGRITY_FAILURE,
            )
        return RunAutomationCycleResult(
            AutomationCycleOperationStatus.UNCHANGED, existing.run, None
        )
    if existing.status is AutomationCycleReadStatus.INTEGRITY_FAILURE:
        return RunAutomationCycleResult(
            AutomationCycleOperationStatus.FAILED,
            None,
            AutomationCycleFailureReason.REPLAY_INTEGRITY_FAILURE,
        )

    stages: list[AutomationCycleStageResult] = []
    calls = (
        (
            AutomationCycleStage.PRIORITY_REFRESH,
            command.max_reprioritizations,
            priority_refresh,
            SelectiveBatchReprioritizationCommand(
                subject_id=command.subject_id,
                now=command.now,
                max_jobs=command.max_reprioritizations or None,
            ),
            _priority_stage,
        ),
        (
            AutomationCycleStage.APPLICATION_PLAN_CREATION,
            command.max_plan_creations,
            plan_creation,
            SelectiveBatchPlanCreationCommand(
                subject_id=command.subject_id,
                now=command.now,
                max_jobs=command.max_plan_creations or None,
                job_ids=() if command.max_plan_creations == 0 else None,
            )
            if command.max_plan_creations
            else None,
            _plan_stage,
        ),
        (
            AutomationCycleStage.APPLICATION_PREPARATION,
            command.max_preparations,
            preparation,
            SelectiveBatchPreparationCommand(
                subject_id=command.subject_id,
                now=command.now,
                max_plans=command.max_preparations or None,
                application_plan_ids=()
                if command.max_preparations == 0
                else None,
            )
            if command.max_preparations
            else None,
            _preparation_stage,
        ),
        (
            AutomationCycleStage.APPLICATION_EXECUTION,
            command.max_executions,
            execution,
            SelectiveBatchExecutionCommand(
                subject_id=command.subject_id,
                now=command.now,
                max_plans=command.max_executions or None,
                application_plan_ids=()
                if command.max_executions == 0
                else None,
            )
            if command.max_executions
            else None,
            _execution_stage,
        ),
    )
    for stage, budget, callable_, stage_command, projector in calls:
        if budget == 0:
            stages.append(_skipped(stage, budget))
            continue
        try:
            public_result = await _resolve(callable_(stage_command))
            stages.append(projector(budget, public_result))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            stages.append(_failed_stage(stage, budget, "PUBLIC_BATCH_FAILED"))

    typed_stages = tuple(stages)
    summary = _summarize(typed_stages)
    run = AutomationCycleRun.create(
        command=command,
        stage_results=typed_stages,
        summary=summary,
        overall_status=_overall(typed_stages, summary),
    )
    try:
        written = repository.save(run)
    except (OSError, RuntimeError, TypeError, ValueError):
        return RunAutomationCycleResult(
            AutomationCycleOperationStatus.FAILED,
            None,
            AutomationCycleFailureReason.REPOSITORY_FAILURE,
        )
    if (
        not isinstance(written, AutomationCycleWriteResult)
        or written.status is AutomationCycleWriteStatus.FAILED
        or written.run is None
        or written.run.run_hash != run.run_hash
    ):
        return RunAutomationCycleResult(
            AutomationCycleOperationStatus.FAILED,
            None,
            AutomationCycleFailureReason.REPOSITORY_FAILURE,
        )
    operation = (
        AutomationCycleOperationStatus.UNCHANGED
        if written.status is AutomationCycleWriteStatus.UNCHANGED
        else AutomationCycleOperationStatus(run.overall_status.value)
    )
    return RunAutomationCycleResult(operation, written.run, None)


__all__ = [
    "AUTOMATION_CYCLE_CONTRACT_VERSION",
    "AutomationCycleFailureReason",
    "AutomationCycleOperationStatus",
    "AutomationCycleReadResult",
    "AutomationCycleReadStatus",
    "AutomationCycleRun",
    "AutomationCycleRunRepository",
    "AutomationCycleRunStatus",
    "AutomationCycleStage",
    "AutomationCycleStageResult",
    "AutomationCycleStageStatus",
    "AutomationCycleSummary",
    "AutomationCycleWriteResult",
    "AutomationCycleWriteStatus",
    "ExecutionCallable",
    "PlanCreationCallable",
    "PreparationCallable",
    "PriorityRefreshCallable",
    "PrivateHomeAutomationCycleRunRepository",
    "RunAutomationCycleCommand",
    "RunAutomationCycleResult",
    "run_automation_cycle",
]
