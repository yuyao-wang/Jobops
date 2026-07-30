"""Focused P2c9 selective batch execution tests."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.application_execution_orchestrator import (
    ApplicationExecutionFailureReason,
    ApplicationExecutionStatus,
    RunApplicationExecutionCommand,
    RunApplicationExecutionResult,
)
from core.application_plan import ApplicationPlan
from core.authorized_submission_execution import (
    AuthorizedSubmissionExecutionStatus,
    AuthorizedSubmissionOutcome,
)
from core.current_application_execution_queue import (
    CurrentApplicationExecutionStatus,
    build_current_application_execution_queue,
)
from core.job_prioritization import ProposedPriorityLevel
from core.non_submit_application_execution import (
    NonSubmitApplicationExecutionStatus,
)
from core.selective_batch_execution import (
    BatchApplicationExecutionStatus,
    BatchExecutionPlanInput,
    SelectiveBatchExecutionCommand,
    SelectiveBatchExecutionStatus,
    run_selective_batch_execution,
)
from core.submission_permit import SubmissionPermitStatus

from test_application_execution_orchestrator import (
    _Stages,
    _parts,
    _run,
)
from test_current_application_execution_queue import _new_assembly


class _QueueReader:
    def __init__(self, queue):
        self.queue = queue
        self.calls = []

    def __call__(self, *, subject_id, now):
        self.calls.append((subject_id, now))
        return self.queue


class _Execution:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.calls = []
        self.active = 0
        self.maximum_active = 0

    async def __call__(self, command):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            self.calls.append(command)
            await asyncio.sleep(0)
            status = self.statuses.get(
                command.application_bundle_assembly_record_id,
                ApplicationExecutionStatus.COMPLETED,
            )
            run = SimpleNamespace(
                run_id=(
                    "application-execution-run-"
                    + hashlib.sha256(
                        command.application_bundle_assembly_record_id.encode()
                    ).hexdigest()
                ),
                deferred_reason=(
                    "SYNTHETIC_DEFER"
                    if status is ApplicationExecutionStatus.DEFERRED
                    else None
                ),
                failed_reason=(
                    "SYNTHETIC_FAILURE"
                    if status is ApplicationExecutionStatus.FAILED
                    else None
                ),
            )
            return RunApplicationExecutionResult(
                status=status,
                run=run,
                reason=(
                    ApplicationExecutionFailureReason.PUBLIC_STAGE_EXCEPTION
                    if status is ApplicationExecutionStatus.FAILED
                    else None
                ),
                message=f"synthetic {status.value}",
            )
        finally:
            self.active -= 1


async def _environment(tmp_path, definitions):
    parts, assembled, repository, base_command = _parts(tmp_path)
    base_plan = parts["plan"]
    plans = {}
    assemblies = {}
    for index, (label, queue_status, priority) in enumerate(definitions):
        if index == 0:
            plan = base_plan
            assembly = assembled.record
        else:
            plan = ApplicationPlan.create(
                subject_id=base_plan.subject_id,
                job_id=f"job-batch-execution-{label}",
                job_revision=1,
                job_content_hash=hashlib.sha256(
                    f"job-{label}".encode()
                ).hexdigest(),
                priority_decision_id=f"priority-{label}",
                policy_id="priority-policy-v1",
                policy_version=1,
                policy_content_hash="8" * 64,
                accepted_job_intent_id=f"intent-{label}",
                priority_level=priority,
                created_at=base_command.now + timedelta(minutes=index),
            )
            assert parts["plan_repository"].save(plan).plan == plan
            assembly = _new_assembly(
                assembled.record,
                assembled_at=base_command.now + timedelta(minutes=index),
                plan=plan,
                suffix=label,
            )
            assert parts["assembly_repository"].save(assembly).record == assembly
        plans[label] = plan
        assemblies[label] = assembly
        command = RunApplicationExecutionCommand(
            subject_id=base_plan.subject_id,
            application_bundle_assembly_record_id=assembly.record_id,
            now=base_command.now + timedelta(minutes=30 + index),
            approve_gate_a=True,
        )
        if queue_status is CurrentApplicationExecutionStatus.DEFERRED:
            result = await _run(
                parts,
                repository,
                command,
                _Stages(
                    p2c3_status=(
                        NonSubmitApplicationExecutionStatus
                        .DEFERRED_GATE_A_REQUIRED
                    )
                ),
            )
            assert result.status is ApplicationExecutionStatus.DEFERRED
        elif queue_status is CurrentApplicationExecutionStatus.FAILED:
            result = await _run(
                parts,
                repository,
                command,
                _Stages(p2c5_status=SubmissionPermitStatus.FAILED),
            )
            assert result.status is ApplicationExecutionStatus.FAILED
        elif (
            queue_status
            is CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN
        ):
            result = await _run(
                parts,
                repository,
                command,
                _Stages(
                    p2c6_status=(
                        AuthorizedSubmissionExecutionStatus
                        .SUBMISSION_UNCERTAIN
                    ),
                    outcome=AuthorizedSubmissionOutcome.SUBMISSION_UNCERTAIN,
                ),
            )
            assert (
                result.status
                is ApplicationExecutionStatus.SUBMISSION_UNCERTAIN
            )
        elif queue_status is CurrentApplicationExecutionStatus.SUBMITTED:
            result = await _run(parts, repository, command, _Stages())
            assert result.status is ApplicationExecutionStatus.COMPLETED
    evaluated_at = base_command.now + timedelta(hours=2)
    queue = build_current_application_execution_queue(
        subject_id=base_plan.subject_id,
        now=evaluated_at,
        assembly_repository=parts["assembly_repository"],
        execution_run_repository=repository,
        application_plan_repository=parts["plan_repository"],
    )
    by_plan = {item.application_plan_id: item for item in queue.items}
    items = {label: by_plan[plan.plan_id] for label, plan in plans.items()}
    return base_plan.subject_id, evaluated_at, queue, items, assemblies


@pytest.mark.asyncio
async def test_ready_items_follow_snapshot_order_and_execute_serially(tmp_path):
    subject, now, queue, items, _ = await _environment(
        tmp_path,
        (
            ("p2", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P2),
            ("p0", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P0),
            ("p1", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P1),
        ),
    )
    reader = _QueueReader(queue)
    execution = _Execution()

    result = await run_selective_batch_execution(
        SelectiveBatchExecutionCommand(
            subject_id=subject,
            now=now,
            max_plans=3,
        ),
        execution_queue_reader=reader,
        single_job_execution=execution,
    )

    assert result.status is SelectiveBatchExecutionStatus.COMPLETED
    assert [
        call.application_bundle_assembly_record_id
        for call in execution.calls
    ] == [item.assembly_record_id for item in queue.ready_items]
    assert execution.maximum_active == 1
    assert reader.calls == [(subject, now)]
    assert all(
        call.subject_id == subject and call.now == now
        for call in execution.calls
    )


@pytest.mark.asyncio
async def test_nonready_terminal_and_uncertain_items_are_never_executed(
    tmp_path,
):
    subject, now, queue, items, _ = await _environment(
        tmp_path,
        (
            ("ready", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P0),
            ("deferred", CurrentApplicationExecutionStatus.DEFERRED, ProposedPriorityLevel.P1),
            ("failed", CurrentApplicationExecutionStatus.FAILED, ProposedPriorityLevel.P1),
            ("uncertain", CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN, ProposedPriorityLevel.P2),
            ("submitted", CurrentApplicationExecutionStatus.SUBMITTED, ProposedPriorityLevel.P3),
        ),
    )
    execution = _Execution()
    allowlist = tuple(items[label].application_plan_id for label in items)

    result = await run_selective_batch_execution(
        SelectiveBatchExecutionCommand(
            subject_id=subject,
            now=now,
            application_plan_ids=allowlist,
        ),
        execution_queue_reader=_QueueReader(queue),
        single_job_execution=execution,
    )

    assert len(execution.calls) == 1
    statuses = {
        item.application_plan_id: item.execution_status
        for item in result.items
    }
    assert statuses[items["deferred"].application_plan_id] is (
        BatchApplicationExecutionStatus.SKIPPED_NOT_READY
    )
    assert statuses[items["failed"].application_plan_id] is (
        BatchApplicationExecutionStatus.SKIPPED_NOT_READY
    )
    assert statuses[items["submitted"].application_plan_id] is (
        BatchApplicationExecutionStatus.SKIPPED_SUBMITTED
    )
    assert statuses[items["uncertain"].application_plan_id] is (
        BatchApplicationExecutionStatus.SKIPPED_UNCERTAIN
    )


@pytest.mark.asyncio
async def test_defer_failure_and_uncertain_do_not_stop_later_ready_plans(
    tmp_path,
):
    subject, now, queue, items, _ = await _environment(
        tmp_path,
        (
            ("complete", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P0),
            ("defer", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P1),
            ("fail", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P2),
            ("uncertain", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P3),
        ),
    )
    statuses = {
        items["defer"].assembly_record_id: ApplicationExecutionStatus.DEFERRED,
        items["fail"].assembly_record_id: ApplicationExecutionStatus.FAILED,
        items["uncertain"].assembly_record_id: (
            ApplicationExecutionStatus.SUBMISSION_UNCERTAIN
        ),
    }
    execution = _Execution(statuses)

    result = await run_selective_batch_execution(
        SelectiveBatchExecutionCommand(
            subject_id=subject, now=now, max_plans=4
        ),
        execution_queue_reader=_QueueReader(queue),
        single_job_execution=execution,
    )

    assert len(execution.calls) == 4
    assert result.status is SelectiveBatchExecutionStatus.PARTIAL_FAILURE
    assert result.summary.completed == 1
    assert result.summary.deferred == 1
    assert result.summary.failed == 1
    assert result.summary.uncertain == 1


@pytest.mark.asyncio
async def test_allowlist_dedup_bound_notfound_and_repeat_use_one_snapshot(
    tmp_path,
):
    subject, now, queue, items, _ = await _environment(
        tmp_path,
        (
            ("submitted", CurrentApplicationExecutionStatus.SUBMITTED, ProposedPriorityLevel.P0),
            ("first", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P1),
            ("second", CurrentApplicationExecutionStatus.READY, ProposedPriorityLevel.P2),
        ),
    )
    missing = "application-plan-" + "f" * 64
    allowlist = (
        items["submitted"].application_plan_id,
        missing,
        items["first"].application_plan_id,
        items["first"].application_plan_id,
        items["second"].application_plan_id,
    )
    reader = _QueueReader(queue)
    execution = _Execution()
    command = SelectiveBatchExecutionCommand(
        subject_id=subject,
        now=now,
        application_plan_ids=allowlist,
        max_plans=1,
        plan_inputs=(
            BatchExecutionPlanInput(
                application_plan_id=items["first"].application_plan_id,
                approve_gate_a=True,
            ),
        ),
    )

    first = await run_selective_batch_execution(
        command,
        execution_queue_reader=reader,
        single_job_execution=execution,
    )
    replay_execution = _Execution(
        {
            items["first"].assembly_record_id: (
                ApplicationExecutionStatus.UNCHANGED
            )
        }
    )
    second = await run_selective_batch_execution(
        command,
        execution_queue_reader=reader,
        single_job_execution=replay_execution,
    )

    assert first.summary.requested == 4
    assert first.summary.selected == 1
    assert first.summary.skipped_submitted == 1
    assert first.summary.not_found == 1
    assert len(first.items) == 3
    assert len(execution.calls) == 1
    assert execution.calls[0].approve_gate_a is True
    assert len(replay_execution.calls) == 1
    assert second.summary.unchanged == 1
    assert reader.calls == [(subject, now), (subject, now)]
