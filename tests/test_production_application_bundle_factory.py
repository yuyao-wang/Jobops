"""Focused P2c1c production ApplicationBundle Factory tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.application_bundle_assembly import (
    ApplicationBundleAssemblyStatus,
)
from core.bundles import (
    ApplicationBundle,
    MaterialBundle,
    application_bundle_canonical_hash,
)
from core.production_application_bundle_factory import (
    PRODUCTION_APPLICATION_BUNDLE_FACTORY_CONTRACT_VERSION,
    ProductionApplicationBundleFactory,
    ProductionApplicationBundleFactoryError,
    ProductionApplicationBundleFactoryFailureReason,
    build_production_application_bundle_factory,
)
from core.recoverable_application_bundle import (
    RecoverableApplicationBundleEnvelopeReadStatus,
)
from test_application_bundle_assembly import _run, _setup


def _captured_request(tmp_path: Path):
    parts = _setup(tmp_path)
    result = _run(parts)
    assert result.status is ApplicationBundleAssemblyStatus.CREATED
    return parts, parts["factory"].requests[0]


def test_complete_exact_request_builds_existing_bundle_without_io(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    factory = build_production_application_bundle_factory()

    result = _run(parts, bundle_factory=factory)

    assert result.status is ApplicationBundleAssemblyStatus.CREATED
    assert isinstance(result.bundle, ApplicationBundle)
    assert factory.contract_version == (
        PRODUCTION_APPLICATION_BUNDLE_FACTORY_CONTRACT_VERSION
    )
    assert result.bundle.job.job_id == parts["plan"].job_id
    assert result.bundle.job.tier == parts["execution_policy"].policy_decision.tier
    assert result.bundle.materials == parts["envelope_repository"].get_for_assembly(
        subject_id=parts["plan"].subject_id,
        assembly_record_id=result.record.record_id,
    ).envelope.bundle.materials
    assert result.bundle.identity_profile.first_name == "Synthetic"
    assert result.bundle.answers.to_dict() == {
        item.canonical_key.value: item.value
        for item in parts["answer_set"].answers
    }
    assert result.bundle.policy == parts["execution_policy"].policy_decision


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda request: replace(request, subject_id="other-subject"),
            ProductionApplicationBundleFactoryFailureReason
            .SUBJECT_BINDING_MISMATCH,
        ),
        (
            lambda request: replace(
                request, execution_context_binding_hash="0" * 64
            ),
            ProductionApplicationBundleFactoryFailureReason
            .EXECUTION_CONTEXT_BINDING_MISMATCH,
        ),
        (
            lambda request: replace(
                request,
                materials=replace(
                    request.materials,
                    resume_path=Path("/synthetic/outside/resume.pdf"),
                ),
            ),
            ProductionApplicationBundleFactoryFailureReason
            .MATERIAL_CONTRACT_INVALID,
        ),
        (
            lambda request: replace(request, answers={}),
            ProductionApplicationBundleFactoryFailureReason
            .ANSWER_CONTRACT_INVALID,
        ),
        (
            lambda request: replace(request, policy_decision="not-a-policy"),
            ProductionApplicationBundleFactoryFailureReason
            .POLICY_CONTRACT_INVALID,
        ),
        (
            lambda request: replace(
                request,
                job_posting=replace(
                    request.job_posting,
                    revision=request.job_posting.revision + 1,
                ),
            ),
            ProductionApplicationBundleFactoryFailureReason
            .PLAN_JOB_BINDING_MISMATCH,
        ),
    ],
)
def test_invalid_or_drifted_inputs_fail_closed_without_partial_bundle(
    tmp_path: Path,
    mutation,
    reason,
) -> None:
    _, request = _captured_request(tmp_path)
    factory = ProductionApplicationBundleFactory()

    with pytest.raises(ProductionApplicationBundleFactoryError) as caught:
        factory.create(mutation(request))

    assert caught.value.reason is reason
    diagnostic = str(caught.value)
    assert "Synthetic" not in diagnostic
    assert "synthetic@example.test" not in diagnostic
    assert "/synthetic/" not in diagnostic


def test_factory_is_deterministic_and_p2c1_replay_calls_it_zero_times(
    tmp_path: Path,
) -> None:
    parts, request = _captured_request(tmp_path)
    production = ProductionApplicationBundleFactory()

    first = production.create(request)
    second = production.create(request)

    assert first == second
    assert application_bundle_canonical_hash(first) == (
        application_bundle_canonical_hash(second)
    )

    class _CountingFactory:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, replay_request):
            self.calls += 1
            return production.create(replay_request)

    counting = _CountingFactory()
    replay = _run(parts, bundle_factory=counting)

    assert replay.status is ApplicationBundleAssemblyStatus.UNCHANGED
    assert counting.calls == 0
    assert replay.record.record_id == parts[
        "assembly_repository"
    ].list_for_subject(
        subject_id=parts["plan"].subject_id
    ).records[0].record_id


def test_factory_bundle_round_trips_through_existing_execution_contract(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    result = _run(
        parts,
        bundle_factory=ProductionApplicationBundleFactory(),
    )
    assert result.status is ApplicationBundleAssemblyStatus.CREATED

    read = parts["envelope_repository"].get_for_assembly(
        subject_id=parts["plan"].subject_id,
        assembly_record_id=result.record.record_id,
    )

    assert (
        read.status
        is RecoverableApplicationBundleEnvelopeReadStatus.FOUND
    )
    recovered = read.envelope.bundle
    assert application_bundle_canonical_hash(recovered) == (
        result.record.application_bundle_canonical_hash
    )
    assert recovered.identity_profile == result.bundle.identity_profile
    assert recovered.policy == result.bundle.policy
    assert recovered.permit_bindings(review_hash="1" * 64).job_id == (
        parts["plan"].job_id
    )
