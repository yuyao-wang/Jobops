"""Focused P2c5b plan-scoped submission permit issuance tests."""

from __future__ import annotations

import json
import hashlib
import ast
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from auth.credentials import InMemoryCredentialStore
from core.event_ledger import EventLedger
from core.non_submit_application_execution import (
    NonSubmitApplicationExecutionReadResult,
    NonSubmitApplicationExecutionReadStatus,
    PrivateHomeNonSubmitApplicationExecutionRepository,
)
from core.permits import (
    OpaquePermitTokenStore,
    PermitBindingError,
    PermitGate,
    PermitIssuerUnavailableError,
    PermitService,
    SubmissionPermitAction,
)
from core.submission_authorization import (
    SubmissionAuthorizationResultStatus,
    create_explicit_submission_authorization,
)
from core.submission_permit import (
    IssueSubmissionPermitCommand,
    PrivateHomeSubmissionPermitRepository,
    SubmissionPermitFailureReason,
    SubmissionPermitPolicy,
    SubmissionPermitReadResult,
    SubmissionPermitReadStatus,
    SubmissionPermitStatus,
    SubmissionPermitWriteResult,
    SubmissionPermitWriteStatus,
    issue_submission_permit,
)

from test_application_bundle_assembly import SUBJECT_ID
from test_non_submit_application_execution import (
    _Engine,
    _execute,
    _setup as _execution_setup,
)
from test_submission_authorization import (
    DECISION_NOW,
    _decide,
    _decision_repository,
)


PERMIT_NOW = DECISION_NOW + timedelta(minutes=1)


class _LedgerEngine(_Engine):
    def __init__(self, reference) -> None:
        super().__init__()
        self.reference = reference

    def gate_a_consumption_reference(self, run_id: str):
        assert self.reference.run_id == run_id
        return self.reference


class _CountingPermitService(PermitService):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.issue_calls = 0

    def issue_plan_scoped_submission_permit(self, *args, **kwargs):
        self.issue_calls += 1
        return super().issue_plan_scoped_submission_permit(*args, **kwargs)


class _CountingTokenStore(OpaquePermitTokenStore):
    def __init__(self, credentials) -> None:
        super().__init__(credentials)
        self.save_calls = 0

    def save(self, **kwargs):
        self.save_calls += 1
        return super().save(**kwargs)


async def _authorized_setup(tmp_path: Path):
    parts, assembled, execution_repository = _execution_setup(tmp_path)
    bundle = assembled.bundle
    ledger = EventLedger(tmp_path / "permit-events.sqlite3")
    ledger.create_run(run_id=bundle.run_id, job_id=bundle.job.job_id)
    permit_service = _CountingPermitService(
        secret=b"p" * 32,
        ledger=ledger,
        clock=lambda: PERMIT_NOW.timestamp(),
        signer_key_id="keychain:jobops.core.permits:hmac-v1",
    )
    gate_a_bindings = bundle.permit_bindings(review_hash="gate-a-plan")
    gate_a_token = permit_service.issue_gate_a(gate_a_bindings)
    gate_a_claims = permit_service.consume(
        gate_a_token,
        expected_gate=PermitGate.GATE_A,
        expected_bindings=gate_a_bindings,
    )
    reference = permit_service.gate_a_consumption_reference(
        permit_jti=gate_a_claims.jti,
        consumer="P2C3_NON_SUBMIT_EXECUTION",
        action="PREPARE_REVIEW",
    )
    execution, _, _ = await _execute(
        parts,
        assembled,
        execution_repository,
        engine=_LedgerEngine(reference),
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
    decision_result = _decide(
        parts,
        record,
        _decision_repository(parts),
        explicit=explicit,
    )
    assert decision_result.status is SubmissionAuthorizationResultStatus.AUTHORIZED
    assert decision_result.decision is not None
    credentials = InMemoryCredentialStore()
    token_store = _CountingTokenStore(credentials)
    permit_repository = PrivateHomeSubmissionPermitRepository(parts["home"])
    return {
        "assembled": assembled,
        "credentials": credentials,
        "decision": decision_result.decision,
        "execution": record,
        "execution_repository": execution_repository,
        "parts": parts,
        "permit_repository": permit_repository,
        "permit_service": permit_service,
        "token_store": token_store,
    }


def _issue(state, *, now=PERMIT_NOW, **overrides):
    values = {
        "application_plan_repository": state["parts"]["plan_repository"],
        "submission_authorization_repository": _decision_repository(
            state["parts"]
        ),
        "non_submit_execution_repository": state["execution_repository"],
        "bundle_envelope_repository": state["parts"]["envelope_repository"],
        "permit_service": state["permit_service"],
        "token_store": state["token_store"],
        "permit_policy": SubmissionPermitPolicy.v1(),
        "submission_permit_repository": state["permit_repository"],
    }
    values.update(overrides)
    return issue_submission_permit(
        IssueSubmissionPermitCommand(
            subject_id=SUBJECT_ID,
            submission_authorization_decision_id=state["decision"].decision_id,
            now=now,
        ),
        **values,
    )


@pytest.mark.asyncio
async def test_authorized_review_issues_only_an_opaque_plan_scoped_record(
    tmp_path: Path,
) -> None:
    state = await _authorized_setup(tmp_path)

    result = _issue(state)

    assert result.status is SubmissionPermitStatus.CREATED
    assert result.record is not None
    record = result.record
    token = state["token_store"].load(
        subject_id=SUBJECT_ID, reference=record.token_reference
    )
    claims = state["permit_service"].verify_at(
        token,
        now=int(PERMIT_NOW.timestamp()),
        expected_gate=PermitGate.GATE_B,
        expected_bindings=record.permit_bindings,
    )
    assert claims.jti == record.permit_jti
    assert claims.bindings.action is SubmissionPermitAction.SUBMIT_APPLICATION
    persisted = (
        state["parts"]["home"].paths.submission_permits
        / ("subject-" + hashlib.sha256(SUBJECT_ID.encode()).hexdigest())
        / f"{record.record_id}.json"
    ).read_text(encoding="utf-8")
    assert token not in persisted
    assert "synthetic.header.signature" not in json.dumps(record.to_dict())
    tree = ast.parse(
        (
            Path(__file__).parents[1] / "core" / "submission_permit.py"
        ).read_text(encoding="utf-8")
    )
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        marker in imported
        for imported in imports
        for marker in (
            "application_engine",
            "browser",
            "adapters",
        )
    )


@pytest.mark.asyncio
async def test_unauthorized_mismatch_invalid_gate_a_and_submit_state_never_issue(
    tmp_path: Path,
) -> None:
    state = await _authorized_setup(tmp_path)
    execution_repository = state["execution_repository"]
    human_decision = _decide(
        state["parts"],
        state["execution"],
        _decision_repository(state["parts"]),
    )
    assert human_decision.decision is not None
    unauthorized = issue_submission_permit(
        IssueSubmissionPermitCommand(
            subject_id=SUBJECT_ID,
            submission_authorization_decision_id=(
                human_decision.decision.decision_id
            ),
            now=PERMIT_NOW,
        ),
        application_plan_repository=state["parts"]["plan_repository"],
        submission_authorization_repository=_decision_repository(
            state["parts"]
        ),
        non_submit_execution_repository=execution_repository,
        bundle_envelope_repository=state["parts"]["envelope_repository"],
        permit_service=state["permit_service"],
        token_store=state["token_store"],
        permit_policy=SubmissionPermitPolicy.v1(),
        submission_permit_repository=state["permit_repository"],
    )
    assert unauthorized.status is SubmissionPermitStatus.NOT_AUTHORIZED

    other_ledger = EventLedger(tmp_path / "other-permits.sqlite3")
    bundle = state["assembled"].bundle
    other_ledger.create_run(run_id=bundle.run_id, job_id=bundle.job.job_id)
    invalid_reference_service = _CountingPermitService(
        secret=b"p" * 32,
        ledger=other_ledger,
        clock=lambda: PERMIT_NOW.timestamp(),
    )
    invalid_reference = _issue(
        state, permit_service=invalid_reference_service
    )
    assert invalid_reference.status is SubmissionPermitStatus.FAILED

    submitted = state["execution"]
    object.__setattr__(submitted, "submission_attempted", True)

    class _SubmittedRepository:
        def get(self, **kwargs):
            return NonSubmitApplicationExecutionReadResult(
                NonSubmitApplicationExecutionReadStatus.FOUND, submitted
            )

    submit_state = _issue(
        state, non_submit_execution_repository=_SubmittedRepository()
    )
    assert submit_state.status in {
        SubmissionPermitStatus.FAILED,
        SubmissionPermitStatus.NOT_AUTHORIZED,
    }
    assert state["permit_service"].issue_calls == 0
    assert state["token_store"].save_calls == 0


@pytest.mark.asyncio
async def test_any_plan_scoped_binding_tamper_is_rejected(
    tmp_path: Path,
) -> None:
    state = await _authorized_setup(tmp_path)
    created = _issue(state)
    assert created.record is not None
    record = created.record
    token = state["token_store"].load(
        subject_id=SUBJECT_ID, reference=record.token_reference
    )
    for field_name in (
        "subject_id",
        "application_plan_id",
        "bundle_canonical_hash",
        "review_hash",
        "authorization_decision_id",
        "execution_record_id",
        "adapter_platform",
    ):
        tampered = replace(
            record.permit_bindings,
            **{
                field_name: (
                    getattr(record.permit_bindings, field_name) + "-changed"
                )
            },
        )
        with pytest.raises(PermitBindingError):
            state["permit_service"].verify_at(
                token,
                now=int(PERMIT_NOW.timestamp()),
                expected_bindings=tampered,
            )
    with pytest.raises(ValueError):
        replace(record.permit_bindings, action="OTHER_ACTION")


@pytest.mark.asyncio
async def test_replay_is_zero_issue_and_failures_never_return_token(
    tmp_path: Path,
) -> None:
    state = await _authorized_setup(tmp_path)
    first = _issue(state)
    replay = _issue(state, now=PERMIT_NOW + timedelta(seconds=1))
    expired = _issue(
        state,
        now=PERMIT_NOW
        + timedelta(seconds=SubmissionPermitPolicy.v1().ttl_seconds),
    )

    assert first.status is SubmissionPermitStatus.CREATED
    assert replay.status is SubmissionPermitStatus.UNCHANGED
    assert replay.record == first.record
    assert expired.status is SubmissionPermitStatus.NOT_AUTHORIZED
    assert state["permit_service"].issue_calls == 1
    assert state["token_store"].save_calls == 1

    class _FailingTokenStore(_CountingTokenStore):
        def save(self, **kwargs):
            raise RuntimeError("synthetic opaque store unavailable")

    isolated = await _authorized_setup(tmp_path / "store-failure")
    failed_store = _issue(
        isolated,
        token_store=_FailingTokenStore(InMemoryCredentialStore()),
    )
    assert failed_store.status is SubmissionPermitStatus.FAILED
    assert failed_store.record is None
    assert failed_store.failure_reason is (
        SubmissionPermitFailureReason.TOKEN_STORE_FAILURE
    )

    class _UnavailableIssuer(_CountingPermitService):
        def issue_plan_scoped_submission_permit(self, *args, **kwargs):
            raise PermitIssuerUnavailableError("synthetic signer unavailable")

    unavailable = _UnavailableIssuer(
        secret=b"p" * 32,
        ledger=isolated["permit_service"].ledger,
        clock=lambda: PERMIT_NOW.timestamp(),
    )
    deferred = _issue(isolated, permit_service=unavailable)
    assert deferred.status is (
        SubmissionPermitStatus.DEFERRED_ISSUER_UNAVAILABLE
    )
    assert deferred.record is None

    class _FailingRepository:
        def find_current_for_authorization(self, **kwargs):
            return SubmissionPermitReadResult(
                SubmissionPermitReadStatus.NOT_FOUND, None
            )

        def save(self, record):
            return SubmissionPermitWriteResult(
                SubmissionPermitWriteStatus.FAILED,
                None,
                SubmissionPermitFailureReason.PERSISTENCE_FAILURE,
            )

    record_failure = await _authorized_setup(tmp_path / "record-failure")
    failed_record = _issue(
        record_failure,
        submission_permit_repository=_FailingRepository(),
    )
    assert failed_record.status is SubmissionPermitStatus.FAILED
    assert failed_record.record is None
