"""Focused P2a1b selective batch ApplicationPlan creation tests."""

from __future__ import annotations

import asyncio

import pytest

from core.application_plan import (
    CreateApplicationPlanCommand,
    PrivateHomeApplicationPlanRepository,
    create_application_plan,
)
from core.current_priority_queue import CurrentPriorityItemStatus
from core.private_home import PrivateHome
from core.runnable_application_queue import (
    RunnableApplicationQueueCommand,
    RunnableApplicationQueueItem,
    RunnableApplicationQueueResult,
    RunnableApplicationReason,
    RunnableApplicationStatus,
)
from core.selective_batch_plan_creation import (
    BatchJobPreparationInstructions,
    BatchPlanCreationStatus,
    SelectiveBatchPlanCreationCommand,
    SelectiveBatchPlanCreationStatus,
    run_selective_batch_plan_creation,
)
from tests.test_application_plan import NOW, SUBJECT
from tests.test_runnable_application_queue import (
    _intent,
    _policy,
    _queue,
    _queue_item,
)


def _snapshot(
    entries: tuple[tuple[str, RunnableApplicationStatus], ...],
) -> RunnableApplicationQueueResult:
    policy = _policy()
    current_items = tuple(
        _queue_item(job_id, policy) for job_id, _status in entries
    )
    projected = []
    for current, (_job_id, status) in zip(current_items, entries):
        runnable = status is RunnableApplicationStatus.RUNNABLE
        projected.append(
            RunnableApplicationQueueItem(
                subject_id=SUBJECT,
                job=current.job,
                priority_queue_status=CurrentPriorityItemStatus.CURRENT,
                runnable_status=status,
                priority_decision=current.decision,
                application_intent=(
                    _intent(current.job.job_id) if runnable else None
                ),
                reasons=(
                    ()
                    if runnable
                    else (
                        RunnableApplicationReason.NO_APPLICATION_INTENT,
                    )
                ),
            )
        )
    from core.runnable_application_queue import (
        RunnableApplicationQueueStatus,
    )

    return RunnableApplicationQueueResult(
        status=RunnableApplicationQueueStatus.SUCCEEDED,
        reason_code=None,
        retryable=False,
        subject_id=SUBJECT,
        now=NOW,
        policy_snapshot=policy,
        priority_queue_result=_queue(policy, current_items),
        items=tuple(projected),
        message="Synthetic fixed runnable snapshot.",
    )


class _SnapshotReader:
    def __init__(self, snapshot: RunnableApplicationQueueResult) -> None:
        self.snapshot = snapshot
        self.calls: list[RunnableApplicationQueueCommand] = []

    async def __call__(
        self, command: RunnableApplicationQueueCommand
    ) -> RunnableApplicationQueueResult:
        self.calls.append(command)
        return self.snapshot


class _RealCreator:
    def __init__(
        self,
        snapshot: RunnableApplicationQueueResult,
        repository: PrivateHomeApplicationPlanRepository,
        *,
        fail_job: str | None = None,
    ) -> None:
        self.reader = _SnapshotReader(snapshot)
        self.repository = repository
        self.fail_job = fail_job
        self.calls: list[CreateApplicationPlanCommand] = []
        self.active = 0
        self.max_active = 0

    async def __call__(self, command: CreateApplicationPlanCommand):
        self.calls.append(command)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)
        try:
            if command.job_id == self.fail_job:
                raise RuntimeError("synthetic P2a1 failure")
            return await create_application_plan(
                command,
                runnable_queue_reader=self.reader,
                repository=self.repository,
            )
        finally:
            self.active -= 1


def _repository(tmp_path) -> PrivateHomeApplicationPlanRepository:
    return PrivateHomeApplicationPlanRepository(PrivateHome(tmp_path))


@pytest.mark.asyncio
async def test_implicit_selection_is_runnable_only_ordered_and_serial(
    tmp_path,
) -> None:
    snapshot = _snapshot(
        (
            ("job-a", RunnableApplicationStatus.RUNNABLE),
            (
                "job-blocked",
                RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT,
            ),
            ("job-b", RunnableApplicationStatus.RUNNABLE),
        )
    )
    reader = _SnapshotReader(snapshot)
    creator = _RealCreator(snapshot, _repository(tmp_path))

    result = await run_selective_batch_plan_creation(
        SelectiveBatchPlanCreationCommand(
            subject_id=SUBJECT,
            now=NOW,
            max_jobs=2,
        ),
        runnable_queue_reader=reader,
        single_job_plan_creator=creator,
    )

    assert result.status is SelectiveBatchPlanCreationStatus.COMPLETED
    assert [item.job_id for item in result.items] == ["job-a", "job-b"]
    assert [call.job_id for call in creator.calls] == ["job-a", "job-b"]
    assert all(
        call.subject_id == SUBJECT and call.now == NOW
        for call in creator.calls
    )
    assert creator.max_active == 1
    assert len(reader.calls) == 1


@pytest.mark.asyncio
async def test_allowlist_deduplicates_and_skips_without_using_call_limit(
    tmp_path,
) -> None:
    snapshot = _snapshot(
        (
            (
                "job-blocked",
                RunnableApplicationStatus.BLOCKED_NO_APPLICATION_INTENT,
            ),
            ("job-ready", RunnableApplicationStatus.RUNNABLE),
            ("job-later", RunnableApplicationStatus.RUNNABLE),
        )
    )
    reader = _SnapshotReader(snapshot)
    creator = _RealCreator(snapshot, _repository(tmp_path))

    result = await run_selective_batch_plan_creation(
        SelectiveBatchPlanCreationCommand(
            subject_id=SUBJECT,
            now=NOW,
            job_ids=(
                "missing",
                "job-blocked",
                "job-ready",
                "job-ready",
                "job-later",
            ),
            max_jobs=1,
        ),
        runnable_queue_reader=reader,
        single_job_plan_creator=creator,
    )

    assert [item.job_id for item in result.items] == [
        "missing",
        "job-blocked",
        "job-ready",
    ]
    assert [item.creation_status for item in result.items] == [
        BatchPlanCreationStatus.NOT_FOUND,
        BatchPlanCreationStatus.SKIPPED_NOT_RUNNABLE,
        BatchPlanCreationStatus.CREATED,
    ]
    assert result.summary.requested == 4
    assert result.summary.selected == 1
    assert [call.job_id for call in creator.calls] == ["job-ready"]


@pytest.mark.asyncio
async def test_single_job_failure_is_isolated_and_summary_is_partial(
    tmp_path,
) -> None:
    snapshot = _snapshot(
        (
            ("job-fail", RunnableApplicationStatus.RUNNABLE),
            ("job-ok", RunnableApplicationStatus.RUNNABLE),
        )
    )
    creator = _RealCreator(
        snapshot,
        _repository(tmp_path),
        fail_job="job-fail",
    )

    result = await run_selective_batch_plan_creation(
        SelectiveBatchPlanCreationCommand(
            subject_id=SUBJECT,
            now=NOW,
            max_jobs=2,
        ),
        runnable_queue_reader=_SnapshotReader(snapshot),
        single_job_plan_creator=creator,
    )

    assert result.status is SelectiveBatchPlanCreationStatus.PARTIAL_FAILURE
    assert [item.creation_status for item in result.items] == [
        BatchPlanCreationStatus.FAILED,
        BatchPlanCreationStatus.CREATED,
    ]
    assert result.summary.created == 1
    assert result.summary.failed == 1


@pytest.mark.asyncio
async def test_replay_reuses_p2a1_and_instructions_are_only_explicit(
    tmp_path,
) -> None:
    snapshot = _snapshot(
        (
            ("job-instructed", RunnableApplicationStatus.RUNNABLE),
            ("job-default", RunnableApplicationStatus.RUNNABLE),
        )
    )
    repository = _repository(tmp_path)
    command = SelectiveBatchPlanCreationCommand(
        subject_id=SUBJECT,
        now=NOW,
        max_jobs=2,
        job_instructions=(
            BatchJobPreparationInstructions(
                job_id="job-instructed",
                user_preparation_instructions="Use only verified project facts.",
            ),
        ),
    )
    first_creator = _RealCreator(snapshot, repository)
    first = await run_selective_batch_plan_creation(
        command,
        runnable_queue_reader=_SnapshotReader(snapshot),
        single_job_plan_creator=first_creator,
    )
    replay_creator = _RealCreator(snapshot, repository)
    replay = await run_selective_batch_plan_creation(
        command,
        runnable_queue_reader=_SnapshotReader(snapshot),
        single_job_plan_creator=replay_creator,
    )

    assert first.summary.created == 2
    assert replay.status is SelectiveBatchPlanCreationStatus.COMPLETED
    assert replay.summary.unchanged == 2
    assert replay_creator.calls[0].user_preparation_instructions == (
        "Use only verified project facts."
    )
    assert replay_creator.calls[1].user_preparation_instructions is None
