from pathlib import Path

import pytest

from core.event_ledger import EventLedger, hash_job_url
from core.permits import (
    PermitBindingError,
    PermitBindings,
    PermitConsumedError,
    PermitExpiredError,
    PermitGate,
    PermitPrerequisiteError,
    PermitService,
    PermitSignatureError,
)


def bindings(*, review: str = "review-a", material: str = "material-a"):
    return PermitBindings(
        run_id="run-1",
        job_id="job-1",
        job_url_hash=hash_job_url("https://example.invalid/jobs/1"),
        material_hash=material,
        answer_hash="answers-a",
        review_hash=review,
        policy_hash="policy-a",
    )


def service(tmp_path: Path, now: list[float]):
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.create_run(run_id="run-1", job_id="job-1")
    return PermitService(
        secret=b"x" * 32, ledger=ledger, clock=lambda: now[0]
    ), ledger


def test_two_gate_permits_are_bound_expiring_and_one_time(tmp_path: Path) -> None:
    now = [1_000.0]
    permits, ledger = service(tmp_path, now)
    gate_a_bindings = bindings()
    gate_a_token = permits.issue_gate_a(gate_a_bindings, ttl_seconds=30)
    gate_a = permits.consume(
        gate_a_token,
        expected_gate=PermitGate.GATE_A,
        expected_bindings=gate_a_bindings,
    )
    with pytest.raises(PermitConsumedError):
        permits.consume(
            gate_a_token,
            expected_gate=PermitGate.GATE_A,
            expected_bindings=gate_a_bindings,
        )

    final_bindings = bindings(review="final-readback")
    gate_b_token = permits.issue_gate_b(
        final_bindings, gate_a_jti=gate_a.jti, ttl_seconds=10
    )
    gate_b = permits.consume(
        gate_b_token,
        expected_gate=PermitGate.GATE_B,
        expected_bindings=final_bindings,
    )
    assert gate_b.prior_gate_jti == gate_a.jti
    assert ledger.get_permit_consumption(gate_b.jti)["gate"] == "GATE_B"


def test_gate_b_requires_consumed_gate_a_and_unchanged_stable_inputs(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    permits, _ = service(tmp_path, now)
    with pytest.raises(PermitPrerequisiteError):
        permits.issue_gate_b(bindings(), gate_a_jti="not-consumed")

    token = permits.issue_gate_a(bindings())
    gate_a = permits.consume(
        token, expected_gate=PermitGate.GATE_A, expected_bindings=bindings()
    )
    with pytest.raises(PermitBindingError):
        permits.issue_gate_b(
            bindings(review="final", material="changed"), gate_a_jti=gate_a.jti
        )


def test_permit_rejects_tampering_binding_changes_and_expiration(
    tmp_path: Path,
) -> None:
    now = [1_000.0]
    permits, _ = service(tmp_path, now)
    expected = bindings()
    token = permits.issue_gate_a(expected, ttl_seconds=5)

    with pytest.raises(PermitBindingError):
        permits.verify(token, expected_bindings=bindings(material="changed"))
    alphabet = (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    )
    signature = token.rsplit(".", 1)[1]
    final_index = alphabet.index(signature[-1])
    assert final_index % 4 == 0
    noncanonical_signature = (
        signature[:-1] + alphabet[final_index + 1]
    )
    with pytest.raises(PermitSignatureError):
        permits.verify(token.rsplit(".", 1)[0] + "." + noncanonical_signature)

    now[0] = 1_005.0
    with pytest.raises(PermitExpiredError):
        permits.verify(token)
