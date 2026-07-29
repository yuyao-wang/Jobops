"""Focused P2c6 authorized submission and evidence tests."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application_engine import JobApplicationEngine
from core.authorized_submission_execution import (
    AuthorizedSubmissionExecutionMetadata,
    AuthorizedSubmissionExecutionStatus,
    AuthorizedSubmissionOutcome,
    ExecuteAuthorizedSubmissionCommand,
    PrivateHomeAuthorizedSubmissionExecutionRepository,
    execute_authorized_submission,
)
from core.leases import LeaseManager
from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.permits import (
    PERMIT_TOKEN_SERVICE,
    PermitGate,
    hash_value,
)
from core.submission_permit import (
    SubmissionPermitReadResult,
    SubmissionPermitReadStatus,
)

from test_application_bundle_assembly import SUBJECT_ID
from test_submission_permit import (
    PERMIT_NOW,
    _authorized_setup,
    _decision_repository,
    _issue,
)


EXECUTE_NOW = PERMIT_NOW + timedelta(seconds=10)


class _BrowserProvider:
    def __init__(self, engine: JobApplicationEngine) -> None:
        self.engine = engine
        self.calls = 0

    @asynccontextmanager
    async def lease(self, *, owner: str):
        self.calls += 1
        async with self.engine.leases.hold_renewing(
            "browser:chromium", owner=owner, ttl_seconds=300
        ) as guard:
            yield SimpleNamespace(page=object(), lease=guard.lease)


class _SuccessRegistry:
    def __init__(self, permit_service, permit_jti: str) -> None:
        self.permit_service = permit_service
        self.permit_jti = permit_jti
        self.review_calls = 0
        self.submit_calls = 0
        self.consumed_during_review = False

    async def run(self, request):
        review_hash = "b" * 64
        if not request.request_submit:
            self.review_calls += 1
            self.consumed_during_review = (
                self.permit_service.ledger.get_permit_consumption(
                    self.permit_jti
                )
                is not None
            )
            return ApplicationOutcome.review_ready(
                run_id=request.run_id,
                job_id=request.job_id,
                adapter="synthetic",
                details={"review": {"fingerprint": review_hash}},
            )
        allowed = await request.gate_b_validator(
            request.gate_b_permit,
            job_id=request.job_id,
            run_id=request.run_id,
            review_fingerprint=review_hash,
        )
        assert allowed is True
        self.submit_calls += 1
        return ApplicationOutcome.submitted_verified(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="synthetic",
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    sha256="e" * 64,
                ),
            ),
        )


class _ChangedReviewRegistry:
    def __init__(self) -> None:
        self.submit_calls = 0

    async def run(self, request):
        if request.request_submit:
            self.submit_calls += 1
            raise AssertionError("changed Review must not reach submit")
        return ApplicationOutcome.review_ready(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="synthetic",
            details={"review": {"fingerprint": "c" * 64}},
        )


class _RuntimeInputRegistry:
    def __init__(self) -> None:
        self.submit_calls = 0

    async def run(self, request):
        if request.request_submit:
            self.submit_calls += 1
            raise AssertionError("runtime blocker must not reach submit")
        return ApplicationOutcome.needs_user(
            run_id=request.run_id,
            job_id=request.job_id,
            status=OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
            phase=OutcomePhase.VALIDATE,
            reason_code=ReasonCode.SENSITIVE_ANSWER_REQUIRED,
            message="synthetic attestation required",
            adapter="synthetic",
        )


class _UncertainRegistry:
    def __init__(self) -> None:
        self.review_calls = 0
        self.submit_calls = 0

    async def run(self, request):
        review_hash = "b" * 64
        if not request.request_submit:
            self.review_calls += 1
            return ApplicationOutcome.review_ready(
                run_id=request.run_id,
                job_id=request.job_id,
                adapter="synthetic",
                details={"review": {"fingerprint": review_hash}},
            )
        allowed = await request.gate_b_validator(
            request.gate_b_permit,
            job_id=request.job_id,
            run_id=request.run_id,
            review_fingerprint=review_hash,
        )
        assert allowed is True
        self.submit_calls += 1
        return ApplicationOutcome(
            run_id=request.run_id,
            job_id=request.job_id,
            status=OutcomeStatus.FAILED_RETRYABLE,
            phase=OutcomePhase.SUBMIT,
            reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
            message="synthetic post-consumption uncertainty",
            adapter="synthetic",
            retryable=True,
        )


async def _execution_state(tmp_path: Path, registry_factory):
    state = await _authorized_setup(tmp_path)
    permit_result = _issue(state)
    assert permit_result.record is not None
    permit_record = permit_result.record
    registry = registry_factory(state, permit_record)
    engine = JobApplicationEngine(
        ledger=state["permit_service"].ledger,
        leases=LeaseManager(state["permit_service"].ledger),
        permits=state["permit_service"],
        registry=registry,
    )
    engine.record_outcome(
        ApplicationOutcome.review_ready(
            run_id=state["assembled"].bundle.run_id,
            job_id=permit_record.job_id,
            adapter="synthetic",
            checkpoint=permit_record.review_digest_hash,
            details={
                "review": {
                    "fingerprint": permit_record.review_digest_hash
                }
            },
        )
    )
    browser = _BrowserProvider(engine)
    repository = PrivateHomeAuthorizedSubmissionExecutionRepository(
        state["parts"]["home"]
    )
    return state, permit_record, registry, engine, browser, repository


async def _execute(
    state,
    permit_record,
    engine,
    browser,
    repository,
    *,
    now=EXECUTE_NOW,
):
    return await execute_authorized_submission(
        ExecuteAuthorizedSubmissionCommand(
            subject_id=SUBJECT_ID,
            submission_permit_record_id=permit_record.record_id,
            now=now,
        ),
        submission_permit_repository=state["permit_repository"],
        submission_authorization_repository=_decision_repository(
            state["parts"]
        ),
        non_submit_execution_repository=state["execution_repository"],
        bundle_envelope_repository=state["parts"]["envelope_repository"],
        token_store=state["token_store"],
        permit_service=state["permit_service"],
        browser_lease_provider=browser,
        application_engine=engine,
        execution_repository=repository,
        private_home=state["parts"]["home"],
        execution_metadata=AuthorizedSubmissionExecutionMetadata.default(),
    )


@pytest.mark.asyncio
async def test_valid_permit_submits_once_and_persists_verified_evidence(
    tmp_path: Path,
) -> None:
    state, permit, registry, engine, browser, repository = (
        await _execution_state(
            tmp_path,
            lambda state, permit: _SuccessRegistry(
                state["permit_service"], permit.permit_jti
            ),
        )
    )

    result = await _execute(
        state, permit, engine, browser, repository
    )

    assert result.status is AuthorizedSubmissionExecutionStatus.CREATED
    assert result.record is not None
    assert result.record.outcome is (
        AuthorizedSubmissionOutcome.SUBMITTED_VERIFIED
    )
    assert result.record.submission_intent_id
    assert len(result.record.evidence) == 1
    assert registry.submit_calls == 1
    assert browser.calls == 1
    token = state["token_store"].load(
        subject_id=SUBJECT_ID, reference=permit.token_reference
    )
    persisted = (
        state["parts"]["home"].paths.authorized_submission_executions
        / (
            "subject-"
            + hashlib.sha256(SUBJECT_ID.encode("utf-8")).hexdigest()
        )
        / f"{result.record.record_id}.json"
    ).read_text(encoding="utf-8")
    assert token not in persisted
    assert "submission_permit_token" not in json.dumps(
        result.record.to_dict()
    )


@pytest.mark.asyncio
async def test_invalid_permits_stop_before_browser_and_engine(
    tmp_path: Path,
) -> None:
    for name in ("expired", "consumed", "binding", "token"):
        state, permit, registry, engine, browser, repository = (
            await _execution_state(
                tmp_path / name,
                lambda state, permit: _SuccessRegistry(
                    state["permit_service"], permit.permit_jti
                ),
            )
        )
        now = EXECUTE_NOW
        if name == "expired":
            now = permit.expires_at
        elif name == "consumed":
            token = state["token_store"].load(
                subject_id=SUBJECT_ID, reference=permit.token_reference
            )
            state["permit_service"].consume(
                token,
                expected_gate=PermitGate.GATE_B,
                expected_bindings=permit.permit_bindings,
            )
        elif name == "binding":
            object.__setattr__(permit, "bundle_canonical_hash", "0" * 64)

            class _MutatedPermitRepository:
                def get(self, **kwargs):
                    return SubmissionPermitReadResult(
                        SubmissionPermitReadStatus.FOUND, permit
                    )

            state["permit_repository"] = _MutatedPermitRepository()
        else:
            account = (
                f"{hash_value(SUBJECT_ID)}:"
                f"{permit.token_reference.reference_id}"
            )
            state["credentials"].set(
                PERMIT_TOKEN_SERVICE, account, "synthetic.token.drift"
            )

        result = await _execute(
            state, permit, engine, browser, repository, now=now
        )

        assert result.status in {
            AuthorizedSubmissionExecutionStatus.NOT_AUTHORIZED,
            AuthorizedSubmissionExecutionStatus.FAILED,
        }
        assert browser.calls == 0
        assert registry.review_calls == 0
        assert registry.submit_calls == 0


@pytest.mark.asyncio
async def test_changed_review_and_runtime_input_defer_without_consumption(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "changed",
            lambda state, permit: _ChangedReviewRegistry(),
            AuthorizedSubmissionExecutionStatus.DEFERRED_REVIEW_CHANGED,
        ),
        (
            "runtime",
            lambda state, permit: _RuntimeInputRegistry(),
            (
                AuthorizedSubmissionExecutionStatus
                .DEFERRED_RUNTIME_INPUT_REQUIRED
            ),
        ),
    )
    for name, factory, expected in cases:
        state, permit, registry, engine, browser, repository = (
            await _execution_state(tmp_path / name, factory)
        )

        result = await _execute(
            state, permit, engine, browser, repository
        )

        assert result.status is expected
        assert registry.submit_calls == 0
        assert (
            state["permit_service"].ledger.get_permit_consumption(
                permit.permit_jti
            )
            is None
        )


@pytest.mark.asyncio
async def test_permit_is_consumed_at_submit_boundary_and_success_replays_zero_call(
    tmp_path: Path,
) -> None:
    state, permit, registry, engine, browser, repository = (
        await _execution_state(
            tmp_path,
            lambda state, permit: _SuccessRegistry(
                state["permit_service"], permit.permit_jti
            ),
        )
    )

    first = await _execute(
        state, permit, engine, browser, repository
    )
    calls = (browser.calls, registry.review_calls, registry.submit_calls)
    replay = await _execute(
        state, permit, engine, browser, repository
    )

    assert registry.consumed_during_review is False
    assert first.record is not None
    assert (
        first.record.permit_consumption_reference.permit_jti
        == permit.permit_jti
    )
    assert replay.status is AuthorizedSubmissionExecutionStatus.UNCHANGED
    assert replay.record == first.record
    assert (browser.calls, registry.review_calls, registry.submit_calls) == calls


@pytest.mark.asyncio
async def test_consumed_but_unverified_submission_is_uncertain_and_never_retried(
    tmp_path: Path,
) -> None:
    state, permit, registry, engine, browser, repository = (
        await _execution_state(
            tmp_path,
            lambda state, permit: _UncertainRegistry(),
        )
    )

    first = await _execute(
        state, permit, engine, browser, repository
    )
    calls = (browser.calls, registry.review_calls, registry.submit_calls)
    replay = await _execute(
        state, permit, engine, browser, repository
    )

    assert first.status is (
        AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN
    )
    assert first.record is not None
    assert first.record.outcome is (
        AuthorizedSubmissionOutcome.SUBMISSION_UNCERTAIN
    )
    assert replay.status is (
        AuthorizedSubmissionExecutionStatus.SUBMISSION_UNCERTAIN
    )
    assert replay.record == first.record
    assert (browser.calls, registry.review_calls, registry.submit_calls) == calls
    token = state["token_store"].load(
        subject_id=SUBJECT_ID, reference=permit.token_reference
    )
    assert token not in json.dumps(first.record.to_dict(), sort_keys=True)
