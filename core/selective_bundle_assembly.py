"""Bounded serial P2c1 assembly from one fixed P2b6 result."""

from __future__ import annotations

import hashlib
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
from .plan_assembly_execution_context_binding import (
    BindPlanAssemblyExecutionContextCommand,
    BindPlanAssemblyExecutionContextResult,
    BindPlanAssemblyExecutionContextStatus,
    ExecutionPolicyRecordRef,
    PlanAssemblyExecutionContextBinding,
    VerifiedProfileRecordRef,
)
from .selective_batch_preparation import (
    BatchPlanExecutionStatus,
    SelectiveBatchPreparationResult,
)


LEGACY_SELECTIVE_BUNDLE_ASSEMBLY_CONTRACT_VERSION = (
    "selective-application-bundle-assembly-v1"
)
SELECTIVE_BUNDLE_ASSEMBLY_CONTRACT_VERSION = (
    "selective-application-bundle-assembly-v2"
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
    CONTEXT_NOT_READY = "CONTEXT_NOT_READY"
    CONTEXT_CONFLICT = "CONTEXT_CONFLICT"
    CONTEXT_INTEGRITY_FAILURE = "CONTEXT_INTEGRITY_FAILURE"
    CONTEXT_FAILED = "CONTEXT_FAILED"
    FAILED = "FAILED"


class BundleAssemblyFailureReason(StrEnum):
    PREPARATION_RESULT_INVALID = "PREPARATION_RESULT_INVALID"
    CONTEXT_NOT_READY = "CONTEXT_NOT_READY"
    CONTEXT_CONFLICT = "CONTEXT_CONFLICT"
    CONTEXT_INTEGRITY_FAILURE = "CONTEXT_INTEGRITY_FAILURE"
    CONTEXT_FAILED = "CONTEXT_FAILED"
    CONTEXT_EXCEPTION = "CONTEXT_EXCEPTION"
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
    execution_context_binding_id: str | None = None
    verified_profile_ref: VerifiedProfileRecordRef | None = None
    execution_policy_ref: ExecutionPolicyRecordRef | None = None
    application_assembly_context_hash: str | None = None

    def __post_init__(self) -> None:
        _clean("application_plan_id", self.application_plan_id, 180)
        object.__setattr__(self, "status", BundleAssemblyPlanStatus(self.status))
        if self.preparation_run_id is not None:
            _clean("preparation_run_id", self.preparation_run_id)
        if self.assembly_record_id is not None:
            _clean("assembly_record_id", self.assembly_record_id)
        if self.execution_context_binding_id is not None:
            _clean(
                "execution_context_binding_id",
                self.execution_context_binding_id,
            )
        if self.application_assembly_context_hash is not None:
            if (
                len(self.application_assembly_context_hash) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in self.application_assembly_context_hash
                )
            ):
                raise ValueError("execution context hash is invalid")
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
                or self.execution_context_binding_id is None
                or not isinstance(
                    self.verified_profile_ref, VerifiedProfileRecordRef
                )
                or not isinstance(
                    self.execution_policy_ref, ExecutionPolicyRecordRef
                )
                or self.application_assembly_context_hash is None
            ):
                raise ValueError("successful assembly item is malformed")
        elif self.status is BundleAssemblyPlanStatus.SKIPPED_NOT_PREPARED:
            if (
                self.assembly_lineage is not None
                or self.assembly_record_id is not None
                or self.reason is not None
                or self.execution_context_binding_id is not None
                or self.verified_profile_ref is not None
                or self.execution_policy_ref is not None
                or self.application_assembly_context_hash is not None
            ):
                raise ValueError("not-prepared assembly item is malformed")
        elif self.status is BundleAssemblyPlanStatus.SKIPPED_MISSING_BINDING:
            if (
                self.assembly_lineage is not None
                or self.assembly_record_id is not None
                or self.execution_context_binding_id is not None
                or self.verified_profile_ref is not None
                or self.execution_policy_ref is not None
                or self.application_assembly_context_hash is not None
                or self.reason
                is not BundleAssemblyFailureReason.PREPARATION_RESULT_INVALID
            ):
                raise ValueError("missing-binding assembly item is malformed")
        elif self.status in {
            BundleAssemblyPlanStatus.CONTEXT_NOT_READY,
            BundleAssemblyPlanStatus.CONTEXT_CONFLICT,
            BundleAssemblyPlanStatus.CONTEXT_INTEGRITY_FAILURE,
            BundleAssemblyPlanStatus.CONTEXT_FAILED,
        }:
            if (
                not isinstance(
                    self.assembly_lineage, PreparationAssemblyLineage
                )
                or self.preparation_run_id
                != self.assembly_lineage.preparation_run_id
                or self.assembly_record_id is not None
                or self.reason is None
                or self.execution_context_binding_id is not None
                or self.verified_profile_ref is not None
                or self.execution_policy_ref is not None
                or self.application_assembly_context_hash is not None
            ):
                raise ValueError("failed context-binding item is malformed")
        elif (
            not isinstance(self.assembly_lineage, PreparationAssemblyLineage)
            or self.preparation_run_id
            != self.assembly_lineage.preparation_run_id
            or self.assembly_record_id is not None
            or self.reason is None
        ):
            raise ValueError("failed assembly item is malformed")
        elif (
            self.execution_context_binding_id is None
            or not isinstance(
                self.verified_profile_ref, VerifiedProfileRecordRef
            )
            or not isinstance(
                self.execution_policy_ref, ExecutionPolicyRecordRef
            )
            or self.application_assembly_context_hash is None
        ):
            raise ValueError("assembly failure lacks exact context lineage")


@dataclass(frozen=True, slots=True)
class SelectiveBundleAssemblySummary:
    requested: int
    selected: int
    assembled: int
    unchanged: int
    skipped_not_prepared: int
    skipped_missing_binding: int
    context_bound: int
    context_not_ready: int
    context_conflict: int
    context_integrity_failure: int
    context_failed: int
    failed: int

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError("bundle assembly counts must be non-negative")
        if self.selected != (
            self.assembled
            + self.unchanged
            + self.context_not_ready
            + self.context_conflict
            + self.context_integrity_failure
            + self.context_failed
            + self.failed
        ):
            raise ValueError("bundle assembly selected count is inconsistent")
        if self.context_bound != self.assembled + self.unchanged + self.failed:
            raise ValueError("bundle assembly context count is inconsistent")


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


class PlanExecutionContextBinder(Protocol):
    def __call__(
        self, command: BindPlanAssemblyExecutionContextCommand
    ) -> BindPlanAssemblyExecutionContextResult | Awaitable[
        BindPlanAssemblyExecutionContextResult
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
                BundleAssemblyPlanStatus.CONTEXT_NOT_READY,
                BundleAssemblyPlanStatus.CONTEXT_CONFLICT,
                BundleAssemblyPlanStatus.CONTEXT_INTEGRITY_FAILURE,
                BundleAssemblyPlanStatus.CONTEXT_FAILED,
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
        context_bound=sum(
            item.execution_context_binding_id is not None for item in items
        ),
        context_not_ready=sum(
            item.status is BundleAssemblyPlanStatus.CONTEXT_NOT_READY
            for item in items
        ),
        context_conflict=sum(
            item.status is BundleAssemblyPlanStatus.CONTEXT_CONFLICT
            for item in items
        ),
        context_integrity_failure=sum(
            item.status
            is BundleAssemblyPlanStatus.CONTEXT_INTEGRITY_FAILURE
            for item in items
        ),
        context_failed=sum(
            item.status is BundleAssemblyPlanStatus.CONTEXT_FAILED
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
    if (
        summary.failed
        or summary.context_not_ready
        or summary.context_conflict
        or summary.context_integrity_failure
        or summary.context_failed
        or invalid
    ):
        return (
            SelectiveBundleAssemblyStatus.PARTIAL_FAILURE
            if successes
            else SelectiveBundleAssemblyStatus.FAILED
        )
    return SelectiveBundleAssemblyStatus.COMPLETED


async def run_selective_bundle_assembly(
    command: SelectiveBundleAssemblyCommand,
    *,
    plan_execution_context_binder: PlanExecutionContextBinder,
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
            summary=SelectiveBundleAssemblySummary(
                0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
            ),
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
            context_bound=0,
            context_not_ready=0,
            context_conflict=0,
            context_integrity_failure=0,
            context_failed=0,
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
        binding_invocation = "p2c10b1-selective-" + hashlib.sha256(
            (
                command.invocation_id
                + "\0"
                + item.application_plan_id
                + "\0"
                + lineage.lineage_hash
            ).encode("utf-8")
        ).hexdigest()
        try:
            binding_result = await _resolve(
                plan_execution_context_binder(
                    BindPlanAssemblyExecutionContextCommand(
                        subject_id=command.subject_id,
                        application_plan_id=item.application_plan_id,
                        preparation_lineage=lineage,
                        invocation_id=binding_invocation,
                        now=command.now,
                    )
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            binding_result = None
        if (
            not isinstance(
                binding_result, BindPlanAssemblyExecutionContextResult
            )
            or binding_result.status
            not in {
                BindPlanAssemblyExecutionContextStatus.CREATED,
                BindPlanAssemblyExecutionContextStatus.UNCHANGED,
            }
            or not isinstance(
                binding_result.binding,
                PlanAssemblyExecutionContextBinding,
            )
        ):
            binding_status = (
                BundleAssemblyPlanStatus.CONTEXT_NOT_READY
                if isinstance(
                    binding_result,
                    BindPlanAssemblyExecutionContextResult,
                )
                and binding_result.status
                is BindPlanAssemblyExecutionContextStatus.NOT_READY
                else BundleAssemblyPlanStatus.CONTEXT_CONFLICT
                if isinstance(
                    binding_result,
                    BindPlanAssemblyExecutionContextResult,
                )
                and binding_result.status
                is BindPlanAssemblyExecutionContextStatus.CONFLICT
                else BundleAssemblyPlanStatus.CONTEXT_INTEGRITY_FAILURE
                if isinstance(
                    binding_result,
                    BindPlanAssemblyExecutionContextResult,
                )
                and binding_result.status
                is BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE
                else BundleAssemblyPlanStatus.CONTEXT_FAILED
            )
            binding_reason = {
                BundleAssemblyPlanStatus.CONTEXT_NOT_READY: (
                    BundleAssemblyFailureReason.CONTEXT_NOT_READY
                ),
                BundleAssemblyPlanStatus.CONTEXT_CONFLICT: (
                    BundleAssemblyFailureReason.CONTEXT_CONFLICT
                ),
                BundleAssemblyPlanStatus.CONTEXT_INTEGRITY_FAILURE: (
                    BundleAssemblyFailureReason.CONTEXT_INTEGRITY_FAILURE
                ),
                BundleAssemblyPlanStatus.CONTEXT_FAILED: (
                    BundleAssemblyFailureReason.CONTEXT_FAILED
                    if isinstance(
                        binding_result,
                        BindPlanAssemblyExecutionContextResult,
                    )
                    else BundleAssemblyFailureReason.CONTEXT_EXCEPTION
                ),
            }[binding_status]
            items.append(
                SelectiveBundleAssemblyPlanResult(
                    application_plan_id=item.application_plan_id,
                    preparation_run_id=item.preparation_run_id,
                    assembly_lineage=lineage,
                    status=binding_status,
                    assembly_record_id=None,
                    reason=binding_reason,
                )
            )
            continue
        binding = binding_result.binding
        if (
            binding.subject_id != command.subject_id
            or binding.application_plan_id != item.application_plan_id
            or binding.preparation_run_id != lineage.preparation_run_id
            or binding.plan_material_manifest_id
            != lineage.plan_material_manifest_id
            or binding.prepared_application_answer_set_id
            != lineage.prepared_application_answer_set_id
            or binding.preparation_lineage_hash != lineage.lineage_hash
        ):
            items.append(
                SelectiveBundleAssemblyPlanResult(
                    application_plan_id=item.application_plan_id,
                    preparation_run_id=item.preparation_run_id,
                    assembly_lineage=lineage,
                    status=(
                        BundleAssemblyPlanStatus.CONTEXT_INTEGRITY_FAILURE
                    ),
                    assembly_record_id=None,
                    reason=(
                        BundleAssemblyFailureReason
                        .CONTEXT_INTEGRITY_FAILURE
                    ),
                )
            )
            continue
        assembly_command = AssembleApplicationBundleCommand(
            subject_id=command.subject_id,
            application_plan_id=item.application_plan_id,
            plan_material_manifest_id=lineage.plan_material_manifest_id,
            prepared_application_answer_set_id=(
                lineage.prepared_application_answer_set_id
            ),
            verified_profile_id=binding.verified_profile_ref.record_id,
            verified_profile_version=(
                binding.verified_profile_ref.record_version
            ),
            verified_profile_hash=binding.verified_profile_ref.record_hash,
            execution_policy_record_id=(
                binding.execution_policy_ref.record_id
            ),
            execution_policy_record_version=(
                binding.execution_policy_ref.record_version
            ),
            execution_policy_record_hash=(
                binding.execution_policy_ref.record_hash
            ),
            execution_context_binding_hash=(
                binding.application_assembly_context_hash
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
                        execution_context_binding_id=binding.binding_id,
                        verified_profile_ref=binding.verified_profile_ref,
                        execution_policy_ref=binding.execution_policy_ref,
                        application_assembly_context_hash=(
                            binding.application_assembly_context_hash
                        ),
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
                execution_context_binding_id=binding.binding_id,
                verified_profile_ref=binding.verified_profile_ref,
                execution_policy_ref=binding.execution_policy_ref,
                application_assembly_context_hash=(
                    binding.application_assembly_context_hash
                ),
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
    "LEGACY_SELECTIVE_BUNDLE_ASSEMBLY_CONTRACT_VERSION",
    "ApplicationBundleAssembler",
    "PlanExecutionContextBinder",
    "BundleAssemblyFailureReason",
    "BundleAssemblyPlanStatus",
    "SelectiveBundleAssemblyCommand",
    "SelectiveBundleAssemblyPlanResult",
    "SelectiveBundleAssemblyResult",
    "SelectiveBundleAssemblyStatus",
    "SelectiveBundleAssemblySummary",
    "run_selective_bundle_assembly",
]
