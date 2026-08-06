from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.application_answers import PreparedApplicationAnswerSetReadStatus
from core.application_bundle_assembly import ApplicationBundleAssemblyReadStatus
from core.application_execution_orchestrator import (
    ApplicationExecutionRunReadStatus,
    ApplicationExecutionStage,
    ApplicationExecutionStatus,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.current_application_execution_queue import (
    CurrentApplicationExecutionQueueStatus,
    CurrentApplicationExecutionStatus,
)
from core.non_submit_application_execution import (
    NonSubmitApplicationExecutionReadStatus,
    NonSubmitExecutionRecordState,
)
from dashboard.application_review_submission import (
    ApplicationReviewSubmissionUIController,
    ApplicationReviewUIStatus,
    ApplicationSubmissionUIStatus,
)


NOW = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
SUBJECT = "synthetic-review-subject"
PLAN = "application-plan-" + "1" * 64
JOB = "job-" + "2" * 20
ASSEMBLY = "application-bundle-assembly-" + "3" * 64
NON_SUBMIT = "non-submit-application-execution-" + "4" * 64
RUN = "application-execution-run-" + "5" * 64


def _context() -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="synthetic-session-id-123456",
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
    )


def _controller(*, execution_status=ApplicationExecutionStatus.COMPLETED):
    item = SimpleNamespace(
        subject_id=SUBJECT,
        application_plan_id=PLAN,
        job_id=JOB,
        assembly_record_id=ASSEMBLY,
        assembly_record_hash="6" * 64,
        execution_run_id=RUN,
        execution_status=CurrentApplicationExecutionStatus.DEFERRED,
        deferred_stage=ApplicationExecutionStage.GATE_B_AUTHORIZATION,
        deferred_reason="USER_AUTHORIZATION_REQUIRED",
        item_hash="7" * 64,
    )
    run = SimpleNamespace(
        subject_id=SUBJECT,
        application_plan_id=PLAN,
        job_id=JOB,
        assembly_record_id=ASSEMBLY,
        assembly_record_hash="6" * 64,
        non_submit_execution_record_id=NON_SUBMIT,
        run_hash="8" * 64,
        gate_a_approved=True,
    )
    assembly = SimpleNamespace(
        subject_id=SUBJECT,
        application_plan_id=PLAN,
        job_id=JOB,
        record_content_hash="6" * 64,
        answer_set_id="prepared-application-answer-set-" + "9" * 64,
        answer_set_content_hash="a" * 64,
    )
    record = SimpleNamespace(
        record_id=NON_SUBMIT,
        subject_id=SUBJECT,
        application_plan_id=PLAN,
        job_id=JOB,
        job_revision=1,
        job_content_hash="b" * 64,
        assembly_record_id=ASSEMBLY,
        assembly_record_content_hash="6" * 64,
        execution_state=NonSubmitExecutionRecordState.REVIEW_READY,
        submission_attempted=False,
        runtime_unresolved_controls=(),
        routed_adapter="greenhouse",
        outcome_checkpoint="c" * 64,
        record_content_hash="d" * 64,
        executed_at=NOW - timedelta(minutes=2),
    )
    answers = SimpleNamespace(
        subject_id=SUBJECT,
        application_plan_id=PLAN,
        job_id=JOB,
        answer_set_id=assembly.answer_set_id,
        answer_set_content_hash="a" * 64,
        answers=(object(), object(), object()),
        unresolved_items=(),
    )
    job = SimpleNamespace(
        job_id=JOB,
        revision=1,
        content_hash="b" * 64,
        title="Synthetic Backend Engineer",
        company="Example Labs",
        location="Alberta",
        ats_type="GREENHOUSE",
    )
    commands = []

    async def execute(command):
        commands.append(command)
        return SimpleNamespace(status=execution_status)

    controller = ApplicationReviewSubmissionUIController(
        execution_queue_reader=lambda **_kwargs: SimpleNamespace(
            status=CurrentApplicationExecutionQueueStatus.SUCCEEDED,
            items=(item,),
        ),
        execution_run_repository=SimpleNamespace(
            get=lambda **_kwargs: SimpleNamespace(
                status=ApplicationExecutionRunReadStatus.FOUND, run=run
            )
        ),
        non_submit_execution_repository=SimpleNamespace(
            get=lambda **_kwargs: SimpleNamespace(
                status=NonSubmitApplicationExecutionReadStatus.FOUND,
                record=record,
            )
        ),
        assembly_repository=SimpleNamespace(
            get=lambda **_kwargs: SimpleNamespace(
                status=ApplicationBundleAssemblyReadStatus.FOUND,
                record=assembly,
            )
        ),
        answer_set_repository=SimpleNamespace(
            get=lambda **_kwargs: SimpleNamespace(
                status=PreparedApplicationAnswerSetReadStatus.FOUND,
                answer_set=answers,
            )
        ),
        job_posting_repository=SimpleNamespace(get=lambda _job_id: job),
        single_job_execution=execute,
        clock=lambda: NOW,
    )
    return controller, commands


@pytest.mark.asyncio
async def test_review_exposes_exact_safe_summary_and_current_token() -> None:
    controller, _ = _controller()

    result = await controller.load(
        context=_context(), application_plan_id=PLAN
    )
    public = result.to_dict()

    assert result.status is ApplicationReviewUIStatus.READY
    assert public["company"] == "Example Labs"
    assert public["title"] == "Synthetic Backend Engineer"
    assert public["resume_included"] is True
    assert public["cover_letter_included"] is True
    assert public["prepared_answer_count"] == 3
    assert public["unresolved_control_count"] == 0
    assert len(public["review_token"]) == 64
    assert public["review_fingerprint"] == "c" * 12
    assert SUBJECT not in str(public)


@pytest.mark.asyncio
async def test_confirm_submits_once_with_review_bound_explicit_authorization() -> None:
    controller, commands = _controller()
    review = await controller.load(
        context=_context(), application_plan_id=PLAN
    )

    result = await controller.submit(
        context=_context(),
        application_plan_id=PLAN,
        review_token=review.review_token,
        confirmed=True,
    )

    assert result.status is ApplicationSubmissionUIStatus.SUBMITTED
    assert result.retry_allowed is False
    assert len(commands) == 1
    command = commands[0]
    assert command.application_bundle_assembly_record_id == ASSEMBLY
    assert command.approve_gate_a is True
    assert command.explicit_user_authorization.application_plan_id == PLAN
    assert command.explicit_user_authorization.review_digest_hash == "c" * 64
    assert command.explicit_user_authorization.authorized_at == NOW


@pytest.mark.asyncio
async def test_stale_review_never_calls_submission() -> None:
    controller, commands = _controller()

    result = await controller.submit(
        context=_context(),
        application_plan_id=PLAN,
        review_token="0" * 64,
        confirmed=True,
    )

    assert result.status is ApplicationSubmissionUIStatus.STALE_REVIEW
    assert commands == []


@pytest.mark.asyncio
async def test_submission_uncertain_is_terminal_and_not_retryable() -> None:
    controller, commands = _controller(
        execution_status=ApplicationExecutionStatus.SUBMISSION_UNCERTAIN
    )
    review = await controller.load(
        context=_context(), application_plan_id=PLAN
    )

    result = await controller.submit(
        context=_context(),
        application_plan_id=PLAN,
        review_token=review.review_token,
        confirmed=True,
    )

    assert result.status is ApplicationSubmissionUIStatus.SUBMISSION_UNCERTAIN
    assert result.retry_allowed is False
    assert len(commands) == 1


@pytest.mark.asyncio
async def test_confirmation_boolean_is_mandatory() -> None:
    controller, commands = _controller()

    with pytest.raises(ValueError, match="explicit confirmation"):
        await controller.submit(
            context=_context(),
            application_plan_id=PLAN,
            review_token="0" * 64,
            confirmed=False,
        )

    assert commands == []
