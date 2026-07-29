"""Focused P2c7 single-job automated execution tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.application_execution_orchestrator import (
    APPLICATION_EXECUTION_STAGE_ORDER,
    ApplicationExecutionRunReadStatus,
    ApplicationExecutionRunStatus,
    ApplicationExecutionStatus,
    PrivateHomeApplicationExecutionRunRepository,
    RunApplicationExecutionCommand,
    run_application_execution,
)
from core.authorized_submission_execution import (
    AuthorizedSubmissionExecutionStatus,
    AuthorizedSubmissionFailureReason,
    AuthorizedSubmissionOutcome,
    ExecuteAuthorizedSubmissionResult,
)
from core.non_submit_application_execution import (
    ExecuteNonSubmitApplicationResult,
    NonSubmitApplicationExecutionFailureReason,
    NonSubmitApplicationExecutionStatus,
    NonSubmitExecutionRecordState,
)
from core.submission_authorization import (
    DecideSubmissionAuthorizationResult,
    SubmissionAuthorizationOperationStatus,
    SubmissionAuthorizationResultStatus,
    SubmissionAuthorizationVerdict,
)
from core.submission_permit import (
    IssueSubmissionPermitResult,
    SubmissionPermitFailureReason,
    SubmissionPermitStatus,
)

from test_application_bundle_assembly import SUBJECT_ID
from test_non_submit_application_execution import (
    EXECUTION_NOW,
    _setup as _execution_setup,
)


HASHES = {
    "non_submit": "1" * 64,
    "authorization": "2" * 64,
    "permit": "3" * 64,
    "submission": "4" * 64,
}


@dataclass
class _Calls:
    order: list[str]
    commands: list[object]
    active: int = 0
    maximum_active: int = 0


class _Stages:
    def __init__(
        self,
        *,
        p2c3_status=NonSubmitApplicationExecutionStatus.CREATED,
        p2c4_status=SubmissionAuthorizationResultStatus.AUTHORIZED,
        verdict=SubmissionAuthorizationVerdict.AUTHORIZED,
        p2c5_status=SubmissionPermitStatus.CREATED,
        p2c6_status=AuthorizedSubmissionExecutionStatus.CREATED,
        outcome=AuthorizedSubmissionOutcome.SUBMITTED_VERIFIED,
    ) -> None:
        self.calls = _Calls([], [])
        self.p2c3_status = p2c3_status
        self.p2c4_status = p2c4_status
        self.verdict = verdict
        self.p2c5_status = p2c5_status
        self.p2c6_status = p2c6_status
        self.outcome = outcome

    async def p2c3(self, command):
        self.calls.active += 1
        self.calls.maximum_active = max(
            self.calls.maximum_active, self.calls.active
        )
        self.calls.order.append("P2C3")
        self.calls.commands.append(command)
        self.calls.active -= 1
        record = (
            SimpleNamespace(
                record_id="non-submit-record-1",
                record_content_hash=HASHES["non_submit"],
                execution_state=NonSubmitExecutionRecordState.REVIEW_READY,
            )
            if self.p2c3_status
            in {
                NonSubmitApplicationExecutionStatus.CREATED,
                NonSubmitApplicationExecutionStatus.UNCHANGED,
            }
            else None
        )
        return ExecuteNonSubmitApplicationResult(
            status=self.p2c3_status,
            record=record,
            failure_reason=(
                NonSubmitApplicationExecutionFailureReason
                .ENGINE_CONTRACT_FAILURE
                if self.p2c3_status
                is NonSubmitApplicationExecutionStatus.FAILED
                else None
            ),
            outcome_status="REVIEW_READY",
            retryable=False,
            message="synthetic P2c3",
        )

    def p2c4(self, command):
        self.calls.order.append("P2C4")
        self.calls.commands.append(command)
        decision = SimpleNamespace(
            decision_id="authorization-decision-1",
            decision_canonical_hash=HASHES["authorization"],
            verdict=self.verdict,
        )
        return DecideSubmissionAuthorizationResult(
            operation_status=SubmissionAuthorizationOperationStatus.CREATED,
            status=self.p2c4_status,
            decision=decision,
            failure_reason=None,
            message="synthetic P2c4",
        )

    def p2c5(self, command):
        self.calls.order.append("P2C5")
        self.calls.commands.append(command)
        record = (
            SimpleNamespace(
                record_id="submission-permit-1",
                record_canonical_hash=HASHES["permit"],
            )
            if self.p2c5_status
            in {SubmissionPermitStatus.CREATED, SubmissionPermitStatus.UNCHANGED}
            else None
        )
        return IssueSubmissionPermitResult(
            status=self.p2c5_status,
            record=record,
            failure_reason=(
                SubmissionPermitFailureReason.PERSISTENCE_FAILURE
                if self.p2c5_status is SubmissionPermitStatus.FAILED
                else None
            ),
            message="synthetic P2c5",
        )

    async def p2c6(self, command):
        self.calls.active += 1
        self.calls.maximum_active = max(
            self.calls.maximum_active, self.calls.active
        )
        self.calls.order.append("P2C6")
        self.calls.commands.append(command)
        self.calls.active -= 1
        record = (
            SimpleNamespace(
                record_id="authorized-submission-1",
                record_canonical_hash=HASHES["submission"],
                outcome=self.outcome,
            )
            if self.p2c6_status
            in {
                AuthorizedSubmissionExecutionStatus.CREATED,
                AuthorizedSubmissionExecutionStatus.UNCHANGED,
                AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN,
            }
            else None
        )
        return ExecuteAuthorizedSubmissionResult(
            status=self.p2c6_status,
            record=record,
            failure_reason=(
                AuthorizedSubmissionFailureReason.ENGINE_CONTRACT_FAILURE
                if self.p2c6_status
                is AuthorizedSubmissionExecutionStatus.FAILED
                else None
            ),
            message="synthetic P2c6",
        )


def _parts(tmp_path):
    parts, assembled, _ = _execution_setup(tmp_path)
    repository = PrivateHomeApplicationExecutionRunRepository(parts["home"])
    command = RunApplicationExecutionCommand(
        subject_id=SUBJECT_ID,
        application_bundle_assembly_record_id=assembled.record.record_id,
        now=EXECUTION_NOW + timedelta(minutes=20),
        approve_gate_a=True,
    )
    return parts, assembled, repository, command


async def _run(parts, repository, command, stages):
    return await run_application_execution(
        command,
        assembly_repository=parts["assembly_repository"],
        non_submit_execution=stages.p2c3,
        gate_b_authorization=stages.p2c4,
        submission_permit_issuance=stages.p2c5,
        authorized_submission_execution=stages.p2c6,
        run_repository=repository,
    )


@pytest.mark.asyncio
async def test_happy_path_is_serial_persisted_and_completed_replay_is_zero_call(
    tmp_path,
):
    parts, assembled, repository, command = _parts(tmp_path)
    stages = _Stages()

    result = await _run(parts, repository, command, stages)

    assert result.status is ApplicationExecutionStatus.COMPLETED
    assert result.run is not None
    assert result.run.overall_status is ApplicationExecutionRunStatus.COMPLETED
    assert tuple(item.stage for item in result.run.stage_results) == (
        APPLICATION_EXECUTION_STAGE_ORDER
    )
    assert stages.calls.order == ["P2C3", "P2C4", "P2C5", "P2C6"]
    assert stages.calls.maximum_active == 1
    assert all(item.subject_id == SUBJECT_ID for item in stages.calls.commands)
    assert all(item.now == command.now for item in stages.calls.commands)
    restored = PrivateHomeApplicationExecutionRunRepository(
        parts["home"]
    ).get(subject_id=SUBJECT_ID, run_id=result.run.run_id)
    assert restored.status is ApplicationExecutionRunReadStatus.FOUND
    assert restored.run == result.run

    replay_stages = _Stages()
    replay = await _run(parts, repository, command, replay_stages)
    assert replay.status is ApplicationExecutionStatus.UNCHANGED
    assert replay.run == result.run
    assert replay_stages.calls.order == []


@pytest.mark.asyncio
async def test_p2c3_defer_stops_all_later_stages(tmp_path):
    parts, _, repository, command = _parts(tmp_path)
    stages = _Stages(
        p2c3_status=(
            NonSubmitApplicationExecutionStatus.DEFERRED_GATE_A_REQUIRED
        )
    )

    result = await _run(parts, repository, command, stages)

    assert result.status is ApplicationExecutionStatus.DEFERRED
    assert stages.calls.order == ["P2C3"]
    assert result.run.deferred_stage is APPLICATION_EXECUTION_STAGE_ORDER[0]


@pytest.mark.asyncio
async def test_gate_b_user_authorization_required_stops_before_permit(tmp_path):
    parts, _, repository, command = _parts(tmp_path)
    stages = _Stages(
        p2c4_status=(
            SubmissionAuthorizationResultStatus
            .DEFERRED_USER_AUTHORIZATION_REQUIRED
        ),
        verdict=SubmissionAuthorizationVerdict.USER_AUTHORIZATION_REQUIRED,
    )

    result = await _run(parts, repository, command, stages)

    assert result.status is ApplicationExecutionStatus.DEFERRED
    assert stages.calls.order == ["P2C3", "P2C4"]
    assert result.run.submission_permit_record_id is None


@pytest.mark.asyncio
async def test_submission_uncertain_is_terminal_and_never_retried(tmp_path):
    parts, _, repository, command = _parts(tmp_path)
    stages = _Stages(
        p2c6_status=(
            AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN
        ),
        outcome=AuthorizedSubmissionOutcome.SUBMISSION_UNCERTAIN,
    )

    first = await _run(parts, repository, command, stages)

    assert first.status is ApplicationExecutionStatus.SUBMISSION_UNCERTAIN
    assert (
        first.run.overall_status
        is ApplicationExecutionRunStatus.SUBMISSION_UNCERTAIN
    )
    replay_stages = _Stages()
    replay = await _run(parts, repository, command, replay_stages)
    assert replay.status is ApplicationExecutionStatus.UNCHANGED
    assert replay.run == first.run
    assert replay_stages.calls.order == []


@pytest.mark.asyncio
async def test_stage_failure_preserves_prefix_and_skips_later_stage(tmp_path):
    parts, _, repository, command = _parts(tmp_path)
    stages = _Stages(p2c5_status=SubmissionPermitStatus.FAILED)

    result = await _run(parts, repository, command, stages)

    assert result.status is ApplicationExecutionStatus.FAILED
    assert stages.calls.order == ["P2C3", "P2C4", "P2C5"]
    assert result.run.non_submit_execution_record_id == "non-submit-record-1"
    assert (
        result.run.submission_authorization_decision_id
        == "authorization-decision-1"
    )
    assert result.run.authorized_submission_execution_record_id is None
