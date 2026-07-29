"""Focused P2c5a permit-contract migration tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from auth.credentials import InMemoryCredentialStore
from core.event_ledger import EventLedger, hash_job_url
from core.permits import (
    PERMIT_TOKEN_SERVICE,
    PLAN_SCOPED_SUBMISSION_BINDING_VERSION,
    OpaquePermitTokenStore,
    OpaquePermitTokenReference,
    PermitBindingError,
    PermitBindings,
    PermitGate,
    PermitService,
    PlanScopedSubmissionPermitBindings,
    SubmissionPermitAction,
    hash_value,
)


def _legacy_bindings(*, review_hash: str = "review-a") -> PermitBindings:
    return PermitBindings(
        run_id="run-1",
        job_id="job-1",
        job_url_hash=hash_job_url("https://example.invalid/jobs/1"),
        material_hash="material-a",
        answer_hash="answers-a",
        review_hash=review_hash,
        policy_hash="policy-a",
    )


def _service(tmp_path: Path) -> tuple[PermitService, EventLedger]:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.create_run(run_id="run-1", job_id="job-1")
    return (
        PermitService(
            secret=b"x" * 32,
            ledger=ledger,
            clock=lambda: 1_000.0,
            signer_key_id="keychain:jobops.core.permits:hmac-v1",
        ),
        ledger,
    )


def _submission_bindings() -> PlanScopedSubmissionPermitBindings:
    legacy = _legacy_bindings(review_hash="review-final")
    return PlanScopedSubmissionPermitBindings(
        contract_version=PLAN_SCOPED_SUBMISSION_BINDING_VERSION,
        **legacy.to_dict(),
        subject_id="subject-1",
        application_plan_id="plan-1",
        bundle_canonical_hash="b" * 64,
        authorization_decision_id="authorization-1",
        authorization_decision_hash="a" * 64,
        execution_record_id="execution-1",
        execution_record_hash="e" * 64,
        adapter_platform="greenhouse",
        action=SubmissionPermitAction.SUBMIT_APPLICATION,
        permit_policy_version="submission-permit-policy-v1",
    )


def _consumed_gate_a(
    permits: PermitService,
):
    token = permits.issue_gate_a(_legacy_bindings())
    claims = permits.consume(
        token,
        expected_gate=PermitGate.GATE_A,
        expected_bindings=_legacy_bindings(),
    )
    return permits.gate_a_consumption_reference(
        permit_jti=claims.jti,
        consumer="P2C3_NON_SUBMIT_EXECUTION",
        action="PREPARE_REVIEW",
    )


def test_legacy_permit_payload_and_consumption_remain_unchanged(
    tmp_path: Path,
) -> None:
    permits, ledger = _service(tmp_path)
    bindings = _legacy_bindings()

    token = permits.issue_gate_a(bindings)
    claims = permits.verify(token, expected_bindings=bindings)
    consumed = permits.consume(
        token,
        expected_gate=PermitGate.GATE_A,
        expected_bindings=bindings,
    )

    assert claims.bindings.to_dict() == {
        "run_id": "run-1",
        "job_id": "job-1",
        "job_url_hash": bindings.job_url_hash,
        "material_hash": "material-a",
        "answer_hash": "answers-a",
        "review_hash": "review-a",
        "policy_hash": "policy-a",
    }
    assert "contract_version" not in claims.bindings.to_dict()
    assert ledger.get_permit_consumption(consumed.jti)["claims"] == (
        consumed.to_dict()
    )


def test_plan_scoped_bindings_validate_every_authorized_scope_field(
    tmp_path: Path,
) -> None:
    permits, _ = _service(tmp_path)
    reference = _consumed_gate_a(permits)
    expected = _submission_bindings()

    claims = permits.validate_plan_scoped_submission_bindings(
        expected,
        expected_bindings=expected,
        gate_a_reference=reference,
    )

    assert claims.gate is PermitGate.GATE_A
    for field_name in (
        "subject_id",
        "application_plan_id",
        "bundle_canonical_hash",
        "authorization_decision_id",
        "authorization_decision_hash",
        "execution_record_id",
        "execution_record_hash",
        "adapter_platform",
        "permit_policy_version",
    ):
        tampered = replace(
            expected, **{field_name: getattr(expected, field_name) + "-changed"}
        )
        with pytest.raises(PermitBindingError):
            permits.validate_plan_scoped_submission_bindings(
                tampered,
                expected_bindings=expected,
                gate_a_reference=reference,
            )


def test_gate_a_reference_is_ledger_verifiable_and_signer_metadata_is_safe(
    tmp_path: Path,
) -> None:
    permits, _ = _service(tmp_path)
    reference = _consumed_gate_a(permits)

    claims = permits.verify_gate_a_consumption_reference(reference)
    metadata = permits.signer_metadata.to_dict()

    assert claims.bindings == _legacy_bindings()
    assert metadata == {
        "algorithm": "HMAC-SHA256",
        "key_id": "keychain:jobops.core.permits:hmac-v1",
        "provider_version": "foundation-permit-signer-v1",
    }
    serialized = json.dumps(
        {"reference": reference.to_dict(), "signer": metadata}, sort_keys=True
    )
    assert (b"x" * 32).hex() not in serialized
    assert "." not in reference.permit_jti


def test_opaque_token_reference_is_subject_isolated_and_fails_on_drift() -> None:
    credentials = InMemoryCredentialStore()
    tokens = OpaquePermitTokenStore(credentials)
    token = "synthetic.header.signature"

    reference = tokens.save(
        subject_id="subject-1",
        reference_id="permit-1",
        token=token,
    )

    assert tokens.load(subject_id="subject-1", reference=reference) == token
    assert OpaquePermitTokenReference.from_dict(
        reference.to_dict()
    ) == reference
    assert token not in json.dumps(reference.to_dict(), sort_keys=True)
    with pytest.raises(PermitBindingError):
        tokens.load(subject_id="subject-2", reference=reference)
    account = f"{hash_value('subject-1')}:permit-1"
    credentials.set(PERMIT_TOKEN_SERVICE, account, "corrupted.token")
    with pytest.raises(PermitBindingError):
        tokens.load(subject_id="subject-1", reference=reference)
