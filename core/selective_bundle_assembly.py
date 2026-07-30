"""Bounded serial P2c1 assembly from one fixed P2b6 result."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from .application_bundle_assembly import (
    ApplicationBundleAssemblyStatus,
    AssembleApplicationBundleCommand,
    AssembleApplicationBundleResult,
)
from .application_preparation_orchestrator import (
    PreparationAssemblyLineage,
)
from .selective_batch_preparation import (
    BatchPlanExecutionStatus,
    SelectiveBatchPreparationResult,
)


SELECTIVE_BUNDLE_ASSEMBLY_CONTRACT_VERSION = (
    "selective-application-bundle-assembly-v1"
)


def _clean(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the bundle batch contract")
    return cleaned


def _aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value


class SelectiveBundleAssemblyStatus(StrEnum):
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class BundleAssemblyPlanStatus(StrEnum):
    ASSEMBLED = "ASSEMBLED"
    UNCHANGED = "UNCHANGED"
    SKIPPED_NOT_PREPARED = "SKIPPED_NOT_PREPARED"
    SKIPPED_MISSING_BINDING = "SKIPPED_MISSING_BINDING"
    FAILED = "FAILED"


class BundleAssemblyFailureReason(StrEnum):
    PREPARATION_RESULT_INVALID = "PREPARATION_RESULT_INVALID"
    P2C1_RESULT_INVALID = "P2C1_RESULT_INVALID"
    P2C1_NOT_READY = "P2C1_NOT_READY"
    P2C1_FAILED = "P2C1_FAILED"
    P2C1_EXCEPTION = "P2C1_EXCEPTION"


@dataclass(frozen=True, slots=True)
class SelectiveBundleAssemblyCommand:
    subject_id: str
    now: datetime
    invocation_id: str
    preparation_result: SelectiveBatchPreparationResult
    max_assemblies: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean("subject_id", self.subject_id, 160)
        )
        object.__setattr__(
            self, "invocation_id", _clean("invocation_id", self.invocation_id)
        )
        _aware(self.now)
        if not isinstance(
            self.preparation_result, SelectiveBatchPreparationResult
        ):
            raise TypeError("preparation_result must be a P2b6 result")
        if type(self.max_assemblies) is not int or self.max_assemblies < 0:
            raise ValueError("max_assemblies must be non-negative")


@dataclass(frozen=True, slots=True)
class SelectiveBundleAssemblyPlanResult:
    application_plan_id: str
    preparation_run_id: str | None
    assembly_lineage: PreparationAssemblyLineage | None
    status: BundleAssemblyPlanStatus
    assembly_record_id: str | None
    reason: BundleAssemblyFailureReason | None

    def __post_init__(self) -> None:
        _clean("application_plan_id", self.application_plan_id, 180)
        object.__setattr__(self, "status", BundleAssemblyPlanStatus(self.status))
        if self.preparation_run_id is not None:
            _clean("preparation_run_id", self.preparation_run_id)
        if self.assembly_record_id is not None:
            _clean("assembly_record_id", self.assembly_record_id)
        if self.reason is not None:
            object.__setattr__(
                self, "reason", BundleAssemblyFailureReason(self.reason)
            )
        if self.status in {
            BundleAssemblyPlanStatus.ASSEMBLED,
            BundleAssemblyPlanStatus.UNCHANGED,
        }:
            if (
                not isinstance(
                    self.assembly_lineage, PreparationAssemblyLineage
                )
                or self.preparation_run_id
                != self.assembly_lineage.preparation_run_id
                or self.assembly_record_id is None
                or self.reason is not None
            ):
                raise ValueError("successful assembly item is malformed")
        elif self.status is BundleAssemblyPlanStatus.SKIPPED_NOT_PREPARED:
            if (
                self.assembly_lineage is not None
                or self.assembly_record_id is not None
                or self.reason is not None
            ):
                raise ValueError("not-prepared assembly item is malformed")
        elif self.status is BundleAssemblyPlanStatus.SKIPPED_MISSING_BINDING:
            if (
                self.assembly_lineage is not None
                or self.assembly_record_id is not None
                or self.reason
                is not BundleAssemblyFailureReason.PREPARATION_RESULT_INVALID
            ):
                raise ValueError("missing-binding assembly item is malformed")
        elif (
            not isinstance(self.assembly_lineage, PreparationAssemblyLineage)
            or self.preparation_run_id
            != self.assembly_lineage.preparation_run_id
            or self.assembly_record_id is not None
            or self.reason is None
        ):
            raise ValueError("failed assembly item is malformed")


@dataclass(frozen=True, slots=True)
class SelectiveBundleAssemblySummary:
    requested: int
    selected: int
    assembled: int
    unchanged: int
    skipped_not_prepared: int
    skipped_missing_binding: int
    failed: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError("bundle assembly counts must be non-negative")
        if self.selected != self.assembled + self.unchanged + self.failed:
            raise ValueError("bundle assembly selected count is inconsistent")


@dataclass(frozen=True, slots=True)
class SelectiveBundleAssemblyResult:
    status: SelectiveBundleAssemblyStatus
    subject_id: str
    evaluated_at: datetime
    invocation_id: str
    preparation_queue_snapshot_hash: str | None
    items: tuple[SelectiveBundleAssemblyPlanResult, ...]
    summary: SelectiveBundleAssemblySummary
    failure_reason: BundleAssemblyFailureReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", SelectiveBundleAssemblyStatus(self.status)
        )
        _clean("subject_id", self.subject_id, 160)
        _clean("invocation_id", self.invocation_id)
        _aware(self.evaluated_at)
        if not isinstance(self.items, tuple) or any(
            not isinstance(item, SelectiveBundleAssemblyPlanResult)
            for item in self.items
        ):
            raise TypeError("bundle assembly items must be typed")
        if len({item.application_plan_id for item in self.items}) != len(
            self.items
        ):
            raise ValueError("bundle assembly items must contain unique plans")
        if not isinstance(self.summary, SelectiveBundleAssemblySummary):
            raise TypeError("bundle assembly summary must be typed")
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                BundleAssemblyFailureReason(self.failure_reason),
            )
            if (
                self.status is not SelectiveBundleAssemblyStatus.FAILED
                or self.items
                or self.preparation_queue_snapshot_hash is not None
            ):
                raise ValueError("fatal bundle assembly result is malformed")
        elif (
            self.summary != _summarize(self.summary.requested, self.items)
            or self.status is not _overall(self.summary)
        ):
            raise ValueError("bundle assembly result is inconsistent")


class ApplicationBundleAssembler(Protocol):
    def __call__(
        self, command: AssembleApplicationBundleCommand
    ) -> AssembleApplicationBundleResult | Awaitable[
        AssembleApplicationBundleResult
    ]: ...


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _lineage_is_valid(
    *,
    subject_id: str,
    plan_id: str,
    run_id: str | None,
    lineage: object,
) -> bool:
    return (
        isinstance(lineage, PreparationAssemblyLineage)
        and lineage.subject_id == subject_id
        and lineage.application_plan_id == plan_id
        and lineage.preparation_run_id == run_id
    )


def _summarize(
    requested: int, items: tuple[SelectiveBundleAssemblyPlanResult, ...]
) -> SelectiveBundleAssemblySummary:
    return SelectiveBundleAssemblySummary(
        requested=requested,
        selected=sum(
            item.status
            in {
                BundleAssemblyPlanStatus.ASSEMBLED,
                BundleAssemblyPlanStatus.UNCHANGED,
                BundleAssemblyPlanStatus.FAILED,
            }
            for item in items
        ),
        assembled=sum(
            item.status is BundleAssemblyPlanStatus.ASSEMBLED for item in items
        ),
        unchanged=sum(
            item.status is BundleAssemblyPlanStatus.UNCHANGED for item in items
        ),
        skipped_not_prepared=sum(
            item.status is BundleAssemblyPlanStatus.SKIPPED_NOT_PREPARED
            for item in items
        ),
        skipped_missing_binding=sum(
            item.status is BundleAssemblyPlanStatus.SKIPPED_MISSING_BINDING
            for item in items
        ),
        failed=sum(
            item.status is BundleAssemblyPlanStatus.FAILED for item in items
        ),
    )


def _overall(
    summary: SelectiveBundleAssemblySummary,
) -> SelectiveBundleAssemblyStatus:
    successes = summary.assembled + summary.unchanged
    invalid = summary.skipped_missing_binding
    if summary.selected == 0:
        return (
            SelectiveBundleAssemblyStatus.FAILED
            if invalid
            else SelectiveBundleAssemblyStatus.NOOP
        )
    if summary.failed or invalid:
        return (
            SelectiveBundleAssemblyStatus.PARTIAL_FAILURE
            if successes
            else SelectiveBundleAssemblyStatus.FAILED
        )
    return SelectiveBundleAssemblyStatus.COMPLETED


async def run_selective_bundle_assembly(
    command: SelectiveBundleAssemblyCommand,
    *,
    assemble_application_bundle: ApplicationBundleAssembler,
) -> SelectiveBundleAssemblyResult:
    """Call P2c1 serially using only one fixed P2b6 public result."""

    if not isinstance(command, SelectiveBundleAssemblyCommand):
        raise TypeError("command must be typed")
    preparation = command.preparation_result
    if (
        preparation.subject_id != command.subject_id
        or preparation.evaluated_at != command.now
    ):
        return SelectiveBundleAssemblyResult(
            status=SelectiveBundleAssemblyStatus.FAILED,
            subject_id=command.subject_id,
            evaluated_at=command.now,
            invocation_id=command.invocation_id,
            preparation_queue_snapshot_hash=None,
            items=(),
            summary=SelectiveBundleAssemblySummary(0, 0, 0, 0, 0, 0, 0),
            failure_reason=(
                BundleAssemblyFailureReason.PREPARATION_RESULT_INVALID
            ),
        )

    if command.max_assemblies == 0:
        summary = SelectiveBundleAssemblySummary(
            requested=len(preparation.items),
            selected=0,
            assembled=0,
            unchanged=0,
            skipped_not_prepared=0,
            skipped_missing_binding=0,
            failed=0,
        )
        return SelectiveBundleAssemblyResult(
            status=SelectiveBundleAssemblyStatus.NOOP,
            subject_id=command.subject_id,
            evaluated_at=command.now,
            invocation_id=command.invocation_id,
            preparation_queue_snapshot_hash=preparation.queue_snapshot_hash,
            items=(),
            summary=summary,
        )

    items: list[SelectiveBundleAssemblyPlanResult] = []
    seen: set[str] = set()
    calls = 0
    for item in preparation.items:
        if item.application_plan_id in seen:
            continue
        seen.add(item.application_plan_id)
        successful = item.execution_status in {
            BatchPlanExecutionStatus.COMPLETED,
            BatchPlanExecutionStatus.UNCHANGED,
        }
        if not successful:
            items.append(
                SelectiveBundleAssemblyPlanResult(
                    application_plan_id=item.application_plan_id,
                    preparation_run_id=item.preparation_run_id,
                    assembly_lineage=None,
                    status=BundleAssemblyPlanStatus.SKIPPED_NOT_PREPARED,
                    assembly_record_id=None,
                    reason=None,
                )
            )
            continue
        lineage = item.assembly_lineage
        if not _lineage_is_valid(
            subject_id=command.subject_id,
            plan_id=item.application_plan_id,
            run_id=item.preparation_run_id,
            lineage=lineage,
        ):
            items.append(
                SelectiveBundleAssemblyPlanResult(
                    application_plan_id=item.application_plan_id,
                    preparation_run_id=item.preparation_run_id,
                    assembly_lineage=None,
                    status=(
                        BundleAssemblyPlanStatus.SKIPPED_MISSING_BINDING
                    ),
                    assembly_record_id=None,
                    reason=(
                        BundleAssemblyFailureReason
                        .PREPARATION_RESULT_INVALID
                    ),
                )
            )
            continue
        if calls >= command.max_assemblies:
            continue
        calls += 1
        assembly_command = AssembleApplicationBundleCommand(
            subject_id=command.subject_id,
            application_plan_id=item.application_plan_id,
            plan_material_manifest_id=lineage.plan_material_manifest_id,
            prepared_application_answer_set_id=(
                lineage.prepared_application_answer_set_id
            ),
            now=command.now,
        )
        try:
            result = await _resolve(
                assemble_application_bundle(assembly_command)
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            result = None
            reason = BundleAssemblyFailureReason.P2C1_EXCEPTION
        else:
            reason = BundleAssemblyFailureReason.P2C1_RESULT_INVALID
        if isinstance(result, AssembleApplicationBundleResult):
            if (
                result.status
                in {
                    ApplicationBundleAssemblyStatus.CREATED,
                    ApplicationBundleAssemblyStatus.UNCHANGED,
                }
                and result.record is not None
                and result.record.subject_id == command.subject_id
                and result.record.application_plan_id
                == item.application_plan_id
                and result.record.manifest_id
                == lineage.plan_material_manifest_id
                and result.record.answer_set_id
                == lineage.prepared_application_answer_set_id
            ):
                items.append(
                    SelectiveBundleAssemblyPlanResult(
                        application_plan_id=item.application_plan_id,
                        preparation_run_id=item.preparation_run_id,
                        assembly_lineage=lineage,
                        status=(
                            BundleAssemblyPlanStatus.ASSEMBLED
                            if result.status
                            is ApplicationBundleAssemblyStatus.CREATED
                            else BundleAssemblyPlanStatus.UNCHANGED
                        ),
                        assembly_record_id=result.record.record_id,
                        reason=None,
                    )
                )
                continue
            reason = (
                BundleAssemblyFailureReason.P2C1_NOT_READY
                if result.status is ApplicationBundleAssemblyStatus.NOT_READY
                else BundleAssemblyFailureReason.P2C1_FAILED
                if result.status is ApplicationBundleAssemblyStatus.FAILED
                else BundleAssemblyFailureReason.P2C1_RESULT_INVALID
            )
        items.append(
            SelectiveBundleAssemblyPlanResult(
                application_plan_id=item.application_plan_id,
                preparation_run_id=item.preparation_run_id,
                assembly_lineage=lineage,
                status=BundleAssemblyPlanStatus.FAILED,
                assembly_record_id=None,
                reason=reason,
            )
        )

    typed_items = tuple(items)
    summary = _summarize(len(preparation.items), typed_items)
    return SelectiveBundleAssemblyResult(
        status=_overall(summary),
        subject_id=command.subject_id,
        evaluated_at=command.now,
        invocation_id=command.invocation_id,
        preparation_queue_snapshot_hash=preparation.queue_snapshot_hash,
        items=typed_items,
        summary=summary,
    )


__all__ = [
    "SELECTIVE_BUNDLE_ASSEMBLY_CONTRACT_VERSION",
    "ApplicationBundleAssembler",
    "BundleAssemblyFailureReason",
    "BundleAssemblyPlanStatus",
    "SelectiveBundleAssemblyCommand",
    "SelectiveBundleAssemblyPlanResult",
    "SelectiveBundleAssemblyResult",
    "SelectiveBundleAssemblyStatus",
    "SelectiveBundleAssemblySummary",
    "run_selective_bundle_assembly",
]
