"""Focused P2c8 current application execution queue tests."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest

from core.application_bundle_assembly import (
    ApplicationBundleAssemblyRecord,
)
from core.application_execution_orchestrator import (
    RunApplicationExecutionCommand,
)
from core.application_plan import ApplicationPlan
from core.current_application_execution_queue import (
    CurrentApplicationExecutionQueueStatus,
    CurrentApplicationExecutionStatus,
    build_current_application_execution_queue,
)
from core.job_prioritization import ProposedPriorityLevel
from core.non_submit_application_execution import (
    NonSubmitApplicationExecutionStatus,
)
from core.authorized_submission_execution import (
    AuthorizedSubmissionExecutionStatus,
    AuthorizedSubmissionOutcome,
)

from test_application_bundle_assembly import SUBJECT_ID
from test_application_execution_orchestrator import (
    _Stages,
    _parts,
    _run,
)


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _new_assembly(
    base: ApplicationBundleAssemblyRecord,
    *,
    assembled_at,
    plan: ApplicationPlan | None = None,
    suffix: str = "new",
) -> ApplicationBundleAssemblyRecord:
    identity = base.identity_dict()
    identity.update(
        {
            "answer_set_content_hash": _hash(f"answers-{suffix}"),
            "answer_set_id": f"answer-set-{suffix}",
            "application_bundle_canonical_hash": _hash(f"bundle-{suffix}"),
            "application_bundle_run_id": f"bundle-run-{suffix}",
        }
    )
    if plan is not None:
        identity.update(
            {
                "application_plan_id": plan.plan_id,
                "job_content_hash": plan.job_content_hash,
                "job_id": plan.job_id,
                "job_revision": plan.job_revision,
                "subject_id": plan.subject_id,
            }
        )
    record_id = "application-bundle-assembly-" + _hash(identity)
    content = {
        **identity,
        "assembled_at": (
            assembled_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "record_id": record_id,
    }
    return ApplicationBundleAssemblyRecord(
        **identity,
        record_id=record_id,
        record_content_hash=_hash(content),
        assembled_at=assembled_at,
    )


def _queue(parts, run_repository, now, **overrides):
    values = {
        "subject_id": SUBJECT_ID,
        "now": now,
        "assembly_repository": parts["assembly_repository"],
        "execution_run_repository": run_repository,
        "application_plan_repository": parts["plan_repository"],
    }
    values.update(overrides)
    return build_current_application_execution_queue(**values)


@pytest.mark.asyncio
async def test_current_assembly_without_run_is_ready(tmp_path):
    parts, _, repository, command = _parts(tmp_path)

    result = _queue(parts, repository, command.now)

    assert result.status is CurrentApplicationExecutionQueueStatus.SUCCEEDED
    assert result.ready_count == 1
    assert result.items[0].execution_status is (
        CurrentApplicationExecutionStatus.READY
    )
    assert result.items[0].execution_run_id is None


@pytest.mark.asyncio
async def test_verified_submission_remains_submitted_after_new_assembly(
    tmp_path,
):
    parts, assembled, repository, command = _parts(tmp_path)
    completed = await _run(parts, repository, command, _Stages())
    assert completed.run is not None
    newer = _new_assembly(
        assembled.record,
        assembled_at=command.now + timedelta(minutes=1),
    )
    assert parts["assembly_repository"].save(newer).record == newer

    result = _queue(parts, repository, command.now + timedelta(minutes=2))

    assert result.submitted_count == 1
    item = result.items[0]
    assert item.execution_status is CurrentApplicationExecutionStatus.SUBMITTED
    assert item.assembly_record_id == newer.record_id
    assert item.execution_run_id == completed.run.run_id


@pytest.mark.asyncio
async def test_uncertain_terminal_precedes_later_nonterminal_run(tmp_path):
    parts, _, repository, command = _parts(tmp_path)
    uncertain = await _run(
        parts,
        repository,
        command,
        _Stages(
            p2c6_status=(
                AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN
            ),
            outcome=AuthorizedSubmissionOutcome.SUBMISSION_UNCERTAIN,
        ),
    )
    assert uncertain.run is not None
    later_command = replace(
        command,
        now=command.now + timedelta(minutes=1),
        approve_gate_a=False,
    )
    deferred = await _run(
        parts,
        repository,
        later_command,
        _Stages(
            p2c3_status=(
                NonSubmitApplicationExecutionStatus.DEFERRED_GATE_A_REQUIRED
            )
        ),
    )
    assert deferred.run is not None

    result = _queue(parts, repository, later_command.now)

    assert result.submission_uncertain_count == 1
    item = result.items[0]
    assert item.execution_status is (
        CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN
    )
    assert item.execution_run_id == uncertain.run.run_id


@pytest.mark.asyncio
async def test_old_defer_does_not_block_new_current_assembly(tmp_path):
    parts, assembled, repository, command = _parts(tmp_path)
    deferred = await _run(
        parts,
        repository,
        command,
        _Stages(
            p2c3_status=(
                NonSubmitApplicationExecutionStatus.DEFERRED_GATE_A_REQUIRED
            )
        ),
    )
    assert deferred.run is not None
    newer = _new_assembly(
        assembled.record,
        assembled_at=command.now + timedelta(minutes=1),
    )
    assert parts["assembly_repository"].save(newer).record == newer

    result = _queue(parts, repository, command.now + timedelta(minutes=2))

    item = result.items[0]
    assert item.execution_status is CurrentApplicationExecutionStatus.READY
    assert item.assembly_record_id == newer.record_id
    assert item.execution_run_id is None


@pytest.mark.asyncio
async def test_identity_sort_snapshot_and_zero_write_ignore_mtime_and_list_order(
    tmp_path,
):
    parts, assembled, repository, command = _parts(tmp_path)
    deferred = await _run(
        parts,
        repository,
        command,
        _Stages(
            p2c3_status=(
                NonSubmitApplicationExecutionStatus.DEFERRED_GATE_A_REQUIRED
            )
        ),
    )
    assert deferred.run is not None
    second_plan = ApplicationPlan.create(
        subject_id=SUBJECT_ID,
        job_id="job-execution-queue-second",
        job_revision=1,
        job_content_hash=_hash("job-second"),
        priority_decision_id="priority-second",
        policy_id="priority-policy-v1",
        policy_version=1,
        policy_content_hash="9" * 64,
        accepted_job_intent_id="intent-second",
        priority_level=ProposedPriorityLevel.P0,
        created_at=command.now - timedelta(days=1),
    )
    assert parts["plan_repository"].save(second_plan).plan == second_plan
    second_assembly = _new_assembly(
        assembled.record,
        assembled_at=command.now + timedelta(minutes=1),
        plan=second_plan,
        suffix="second",
    )
    assert (
        parts["assembly_repository"].save(second_assembly).record
        == second_assembly
    )
    first = _queue(parts, repository, command.now + timedelta(minutes=2))
    paths = tuple(
        parts["home"].paths.application_bundle_assemblies.rglob("*.json")
    ) + tuple(
        parts["home"].paths.application_execution_runs.rglob("*.json")
    )
    for index, path in enumerate(paths):
        os.utime(path, (10_000 + index, 10_000 + index))
    mtimes = {path: path.stat().st_mtime_ns for path in paths}

    class _ReverseAssemblies:
        def list_for_subject(self, **kwargs):
            listed = parts["assembly_repository"].list_for_subject(**kwargs)
            return SimpleNamespace(
                status=listed.status,
                records=tuple(reversed(listed.records)),
            )

        def find_current_for_plan(self, **kwargs):
            return parts["assembly_repository"].find_current_for_plan(**kwargs)

    class _ReverseRuns:
        def list_for_subject(self, **kwargs):
            listed = repository.list_for_subject(**kwargs)
            return SimpleNamespace(
                status=listed.status,
                runs=tuple(reversed(listed.runs)),
            )

        def find_current_for_assembly(self, **kwargs):
            return repository.find_current_for_assembly(**kwargs)

    second = _queue(
        parts,
        repository,
        command.now + timedelta(hours=1),
        assembly_repository=_ReverseAssemblies(),
        execution_run_repository=_ReverseRuns(),
    )

    assert [item.execution_status for item in first.items] == [
        CurrentApplicationExecutionStatus.READY,
        CurrentApplicationExecutionStatus.DEFERRED,
    ]
    assert [item.item_id for item in second.items] == [
        item.item_id for item in first.items
    ]
    assert second.snapshot_hash == first.snapshot_hash
    assert {path: path.stat().st_mtime_ns for path in paths} == mtimes
