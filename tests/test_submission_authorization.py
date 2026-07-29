"""Focused P2c4 offline Gate B submission authorization tests."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from core.application_bundle_assembly import ApplicationBundleFactoryRequest
from core.bundles import ApplicationBundle, JobSpec
from core.non_submit_application_execution import (
    NonSubmitApplicationExecutionReadResult,
    NonSubmitApplicationExecutionReadStatus,
    NonSubmitApplicationExecutionStatus,
    PrivateHomeNonSubmitApplicationExecutionRepository,
)
from core.outcomes import (
    ApplicationOutcome,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.policy import (
    AutonomyMode,
    JobTier,
    PolicyConfig,
    PolicyEngine,
    RiskSignals,
)
from core.submission_authorization import (
    DecideSubmissionAuthorizationCommand,
    PrivateHomeSubmissionAuthorizationRepository,
    SubmissionAuthorizationMode,
    SubmissionAuthorizationOperationStatus,
    SubmissionAuthorizationReason,
    SubmissionAuthorizationResultStatus,
    SubmissionAuthorizationVerdict,
    create_explicit_submission_authorization,
    decide_submission_authorization,
)

from test_application_bundle_assembly import (
    NOW,
    SUBJECT_ID,
    _run as _assemble,
    _setup as _assembly_setup,
)
from test_non_submit_application_execution import (
    _Engine,
    _execute,
    _setup as _execution_setup,
)


DECISION_NOW = NOW + timedelta(days=2)


class _RecordRepository:
    def __init__(self, record) -> None:
        self.record = record

    def get(self, *, subject_id: str, record_id: str):
        return NonSubmitApplicationExecutionReadResult(
            NonSubmitApplicationExecutionReadStatus.FOUND, self.record
        )


class _AutomaticFactory:
    def create(
        self, request: ApplicationBundleFactoryRequest
    ) -> ApplicationBundle:
        posting = request.job_posting
        return ApplicationBundle(
            run_id=request.run_id,
            job=JobSpec(
                url=posting.application_url or posting.source_url,
                company=posting.company,
                title=posting.title,
                tier=JobTier.LOW,
                job_id=posting.job_id,
            ),
            materials=request.materials,
            profile={"source": "synthetic-execution-profile"},
            answers=request.answers,
            policy=PolicyEngine(
                PolicyConfig(mode=AutonomyMode.FULL_AUTOPILOT)
            ).decide(JobTier.LOW, RiskSignals()),
        )


def _decision_repository(parts):
    return PrivateHomeSubmissionAuthorizationRepository(parts["home"])


def _command(record, *, explicit=None):
    return DecideSubmissionAuthorizationCommand(
        subject_id=SUBJECT_ID,
        non_submit_execution_record_id=record.record_id,
        now=DECISION_NOW,
        explicit_user_authorization=explicit,
    )


def _decide(parts, record, repository, *, explicit=None, record_repo=None):
    return decide_submission_authorization(
        _command(record, explicit=explicit),
        application_plan_repository=parts["plan_repository"],
        non_submit_execution_repository=record_repo
        or PrivateHomeNonSubmitApplicationExecutionRepository(parts["home"]),
        bundle_envelope_repository=parts["envelope_repository"],
        submission_authorization_repository=repository,
    )


@pytest.mark.asyncio
async def test_explicit_user_authorization_creates_authorized_decision(
    tmp_path: Path,
) -> None:
    parts, assembled, execution_repository = _execution_setup(tmp_path)
    execution, _, _ = await _execute(
        parts, assembled, execution_repository
    )
    record = execution.record
    assert record is not None
    explicit = create_explicit_submission_authorization(
        subject_id=SUBJECT_ID,
        application_plan_id=record.application_plan_id,
        non_submit_execution_record_id=record.record_id,
        review_digest_hash=record.outcome_checkpoint,
        authorized_at=DECISION_NOW,
    )

    result = _decide(
        parts, record, _decision_repository(parts), explicit=explicit
    )

    assert result.operation_status is (
        SubmissionAuthorizationOperationStatus.CREATED
    )
    assert result.status is SubmissionAuthorizationResultStatus.AUTHORIZED
    assert result.decision is not None
    assert result.decision.verdict is (
        SubmissionAuthorizationVerdict.AUTHORIZED
    )
    assert result.decision.authorization_mode is (
        SubmissionAuthorizationMode.EXPLICIT_USER
    )


@pytest.mark.asyncio
async def test_human_policy_without_authorization_defers(
    tmp_path: Path,
) -> None:
    parts, assembled, execution_repository = _execution_setup(tmp_path)
    execution, _, _ = await _execute(
        parts, assembled, execution_repository
    )

    result = _decide(
        parts,
        execution.record,
        _decision_repository(parts),
    )

    assert result.status is (
        SubmissionAuthorizationResultStatus
        .DEFERRED_USER_AUTHORIZATION_REQUIRED
    )
    assert result.decision is not None
    assert result.decision.reasons == (
        SubmissionAuthorizationReason
        .EXPLICIT_USER_AUTHORIZATION_REQUIRED,
    )


@pytest.mark.asyncio
async def test_formal_autonomous_policy_authorizes_automatically(
    tmp_path: Path,
) -> None:
    parts = _assembly_setup(tmp_path)
    parts["factory"] = _AutomaticFactory()
    assembled = _assemble(parts)
    execution_repository = (
        PrivateHomeNonSubmitApplicationExecutionRepository(parts["home"])
    )
    execution, _, _ = await _execute(
        parts, assembled, execution_repository
    )
    assert execution.status is NonSubmitApplicationExecutionStatus.CREATED

    result = _decide(
        parts,
        execution.record,
        _decision_repository(parts),
    )

    assert result.status is SubmissionAuthorizationResultStatus.AUTHORIZED
    assert result.decision is not None
    assert result.decision.authorization_mode is (
        SubmissionAuthorizationMode.AUTOMATIC
    )
    assert result.decision.reasons == (
        SubmissionAuthorizationReason.AUTOMATIC_POLICY_AUTHORIZED,
    )


@pytest.mark.asyncio
async def test_attestation_consent_and_signature_never_auto_authorize(
    tmp_path: Path,
) -> None:
    parts, assembled, execution_repository = _execution_setup(tmp_path)
    bundle = assembled.bundle
    outcome = ApplicationOutcome.needs_user(
        run_id=bundle.run_id,
        job_id=bundle.job.job_id,
        status=OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
        phase=OutcomePhase.VALIDATE,
        reason_code=ReasonCode.SENSITIVE_ANSWER_REQUIRED,
        message="synthetic user confirmation required",
        adapter="synthetic",
        checkpoint="d" * 64,
        details={
            "review": {
                "unresolved_required": [
                    "legal attestation",
                    "background consent",
                    "electronic signature",
                ]
            }
        },
    )
    execution, _, _ = await _execute(
        parts,
        assembled,
        execution_repository,
        engine=_Engine(outcome),
    )

    result = _decide(
        parts,
        execution.record,
        _decision_repository(parts),
    )

    assert result.status is (
        SubmissionAuthorizationResultStatus
        .DEFERRED_USER_AUTHORIZATION_REQUIRED
    )
    assert result.decision is not None
    assert result.decision.verdict is (
        SubmissionAuthorizationVerdict.USER_AUTHORIZATION_REQUIRED
    )
    assert set(result.decision.reasons) == {
        SubmissionAuthorizationReason.RUNTIME_ATTESTATION_REQUIRED,
        SubmissionAuthorizationReason.RUNTIME_CONSENT_REQUIRED,
        SubmissionAuthorizationReason.RUNTIME_SIGNATURE_REQUIRED,
    }


@pytest.mark.asyncio
async def test_validation_binding_and_submission_boundary_fail_closed(
    tmp_path: Path,
) -> None:
    for index, mutation in enumerate(
        ("validation", "binding", "submission")
    ):
        case = tmp_path / str(index)
        parts, assembled, execution_repository = _execution_setup(case)
        execution, _, _ = await _execute(
            parts, assembled, execution_repository
        )
        record = execution.record
        assert record is not None
        if mutation == "validation":
            object.__setattr__(
                record,
                "outcome_reason_code",
                ReasonCode.VALIDATION_FAILED.value,
            )
        elif mutation == "binding":
            object.__setattr__(record, "bundle_canonical_hash", "0" * 64)
        else:
            object.__setattr__(record, "submission_attempted", True)

        result = _decide(
            parts,
            record,
            _decision_repository(parts),
            record_repo=_RecordRepository(record),
        )

        assert result.status is SubmissionAuthorizationResultStatus.BLOCKED


@pytest.mark.asyncio
async def test_replay_and_changed_user_authorization_are_immutable_and_offline(
    tmp_path: Path,
) -> None:
    parts, assembled, execution_repository = _execution_setup(tmp_path)
    execution, _, _ = await _execute(
        parts, assembled, execution_repository
    )
    record = execution.record
    assert record is not None
    first_auth = create_explicit_submission_authorization(
        subject_id=SUBJECT_ID,
        application_plan_id=record.application_plan_id,
        non_submit_execution_record_id=record.record_id,
        review_digest_hash=record.outcome_checkpoint,
        authorized_at=DECISION_NOW,
    )
    repository = _decision_repository(parts)
    first = _decide(parts, record, repository, explicit=first_auth)
    replay = _decide(parts, record, repository, explicit=first_auth)
    changed_auth = create_explicit_submission_authorization(
        subject_id=SUBJECT_ID,
        application_plan_id=record.application_plan_id,
        non_submit_execution_record_id=record.record_id,
        review_digest_hash=record.outcome_checkpoint,
        authorized_at=DECISION_NOW + timedelta(minutes=1),
    )
    changed = _decide(parts, record, repository, explicit=changed_auth)
    tree = ast.parse(
        (
            Path(__file__).parents[1]
            / "core"
            / "submission_authorization.py"
        ).read_text(encoding="utf-8")
    )
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert first.status is SubmissionAuthorizationResultStatus.AUTHORIZED
    assert replay.operation_status is (
        SubmissionAuthorizationOperationStatus.UNCHANGED
    )
    assert changed.operation_status is (
        SubmissionAuthorizationOperationStatus.CREATED
    )
    assert changed.decision.decision_id != first.decision.decision_id
    assert not any(
        marker in imported
        for imported in imports
        for marker in (
            "application_engine",
            "browser_broker",
            "adapters",
            "event_ledger",
        )
    )
