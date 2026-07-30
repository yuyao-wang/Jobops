"""Focused P2c1d3 execution-context handoff tests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from core.application_bundle_assembly import (
    APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION,
    APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION_V1,
    ApplicationBundleAssemblyFailureReason,
    ApplicationBundleAssemblyNotReadyReason,
    ApplicationBundleAssemblyRecord,
    ApplicationBundleAssemblyStatus,
    ApplicationBundleAssemblyWriteStatus,
    AssembleApplicationBundleCommand,
)
from core.application_assembly_execution_context import (
    LoadApplicationAssemblyExecutionContextCommand,
    LoadApplicationAssemblyExecutionContextStatus,
    load_application_assembly_execution_context,
)
from core.plan_execution_policy import (
    PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION,
    PlanExecutionPolicyDecisionRecord,
    PlanExecutionPolicyReadResult,
    PlanExecutionPolicyReadStatus,
    plan_execution_policy_record_hash,
    policy_decision_to_dict,
)
from core.policy import AutonomyMode, JobTier, PolicyConfig, PolicyEngine, RiskSignals
from core.verified_application_execution_profile import (
    VerifiedApplicationExecutionProfileReadResult,
    VerifiedApplicationExecutionProfileReadStatus,
)
from test_application_bundle_assembly import NOW, _run, _setup


def _hash(value) -> str:
    if not isinstance(value, dict):
        value = {"value": value}
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


class _CountingProfileProvider:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.calls = 0

    def get(self, subject_id, profile_snapshot_id):
        self.calls += 1
        return self.provider.get(subject_id, profile_snapshot_id)


class _CountingPolicyProvider:
    def __init__(self, provider) -> None:
        self.provider = provider
        self.calls = 0

    def get(self, **values):
        self.calls += 1
        return self.provider.get(**values)


def test_complete_context_reaches_recording_factory_with_exact_refs(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    profiles = _CountingProfileProvider(parts["profile_repository"])
    policies = _CountingPolicyProvider(parts["policy_repository"])

    result = _run(
        parts,
        verified_execution_profile_provider=profiles,
        plan_execution_policy_provider=policies,
    )

    assert result.status is ApplicationBundleAssemblyStatus.CREATED
    assert profiles.calls == policies.calls == 1
    request = parts["factory"].requests[0]
    assert request.verified_profile_ref == parts["profile"]
    assert request.execution_policy_ref == parts["execution_policy"]
    assert request.identity_profile.first_name == "Synthetic"
    assert request.policy_decision == parts["execution_policy"].policy_decision
    assert (
        request.execution_context_binding_hash
        == parts["execution_context"].context_binding_hash
    )
    assert result.record.contract_version == (
        APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION
    )
    assert result.record.verified_profile_id == parts["profile"].profile_snapshot_id
    assert (
        result.record.execution_policy_record_id
        == parts["execution_policy"].record_id
    )


@pytest.mark.parametrize(
    ("source", "status", "expected"),
    [
        (
            "profile",
            VerifiedApplicationExecutionProfileReadStatus.NOT_FOUND,
            ApplicationBundleAssemblyNotReadyReason.VERIFIED_PROFILE_NOT_READY,
        ),
        (
            "profile",
            VerifiedApplicationExecutionProfileReadStatus.CONFLICT,
            ApplicationBundleAssemblyFailureReason.VERIFIED_PROFILE_CONFLICT,
        ),
        (
            "profile",
            VerifiedApplicationExecutionProfileReadStatus.INTEGRITY_FAILURE,
            ApplicationBundleAssemblyFailureReason
            .VERIFIED_PROFILE_INTEGRITY_FAILURE,
        ),
        (
            "policy",
            PlanExecutionPolicyReadStatus.NOT_FOUND,
            ApplicationBundleAssemblyNotReadyReason.EXECUTION_POLICY_NOT_READY,
        ),
        (
            "policy",
            PlanExecutionPolicyReadStatus.CONFLICT,
            ApplicationBundleAssemblyFailureReason.EXECUTION_POLICY_CONFLICT,
        ),
        (
            "policy",
            PlanExecutionPolicyReadStatus.INTEGRITY_FAILURE,
            ApplicationBundleAssemblyFailureReason
            .EXECUTION_POLICY_INTEGRITY_FAILURE,
        ),
        (
            "cross",
            None,
            ApplicationBundleAssemblyFailureReason
            .EXECUTION_CONTEXT_BINDING_MISMATCH,
        ),
    ],
)
def test_context_failures_never_call_factory_or_persist(
    tmp_path: Path,
    source,
    status,
    expected,
) -> None:
    parts = _setup(tmp_path)

    class _Profile:
        def get(self, subject_id, profile_id):
            if source != "profile":
                read = parts["profile_repository"].get(
                    subject_id, profile_id
                )
                if source == "cross":
                    object.__setattr__(
                        read.snapshot, "job_id", "other-synthetic-job"
                    )
                return read
            return VerifiedApplicationExecutionProfileReadResult(
                status,
                failure_code=(
                    None
                    if status
                    is VerifiedApplicationExecutionProfileReadStatus.NOT_FOUND
                    else "SYNTHETIC_PROFILE_FAILURE"
                ),
            )

    class _Policy:
        def get(self, **values):
            if source != "policy":
                return parts["policy_repository"].get(**values)
            return PlanExecutionPolicyReadResult(status)

    result = _run(
        parts,
        verified_execution_profile_provider=_Profile(),
        plan_execution_policy_provider=_Policy(),
    )

    if isinstance(expected, ApplicationBundleAssemblyNotReadyReason):
        assert result.status is ApplicationBundleAssemblyStatus.NOT_READY
        assert result.not_ready_reason is expected
    else:
        assert result.status is ApplicationBundleAssemblyStatus.FAILED
        assert result.failure_reason is expected
    assert parts["factory"].requests == []
    listed = parts["assembly_repository"].list_for_subject(
        subject_id=parts["plan"].subject_id
    )
    assert listed.records == ()


def _alternate_policy(parts) -> tuple[
    PlanExecutionPolicyDecisionRecord, str
]:
    base = parts["execution_policy"]
    decision = PolicyEngine(
        PolicyConfig(mode=AutonomyMode.FULL_AUTOPILOT)
    ).decide(JobTier.MEDIUM, RiskSignals())
    decision_hash = _hash(policy_decision_to_dict(decision))
    input_hash = _hash("second-policy-input")
    record_id = "plan-execution-policy-" + _hash(
        {
            "input_binding_hash": input_hash,
            "policy_decision_hash": decision_hash,
            "record_contract_version": (
                PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
            ),
        }
    )
    record = replace(
        base,
        record_id=record_id,
        execution_configuration_version=2,
        execution_configuration_hash=_hash("second-configuration"),
        policy_decision=decision,
        policy_decision_hash=decision_hash,
        input_binding_hash=input_hash,
        created_at=NOW + timedelta(minutes=1),
        invocation_id="second-policy",
    )
    return record, plan_execution_policy_record_hash(record)


def test_context_identity_changes_and_completed_replay_is_zero_call(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _run(parts)
    assert first.status is ApplicationBundleAssemblyStatus.CREATED
    factory_calls = len(parts["factory"].requests)

    class _Forbidden:
        def get(self, *_args, **_kwargs):
            raise AssertionError("completed replay must not read context")

    replay = _run(
        parts,
        verified_execution_profile_provider=_Forbidden(),
        plan_execution_policy_provider=_Forbidden(),
    )
    assert replay.status is ApplicationBundleAssemblyStatus.UNCHANGED
    assert replay.record == first.record
    assert len(parts["factory"].requests) == factory_calls

    second_policy, second_policy_hash = _alternate_policy(parts)
    assert parts["policy_repository"].save(second_policy)
    loaded = load_application_assembly_execution_context(
        LoadApplicationAssemblyExecutionContextCommand(
            subject_id=parts["plan"].subject_id,
            application_plan=parts["plan"],
            job_id=parts["plan"].job_id,
            verified_profile_id=parts["profile"].profile_snapshot_id,
            verified_profile_hash=parts["profile"].profile_snapshot_hash,
            execution_policy_record_id=second_policy.record_id,
            execution_policy_record_hash=second_policy_hash,
        ),
        verified_profile_provider=parts["profile_repository"],
        execution_policy_provider=parts["policy_repository"],
    )
    assert loaded.status is LoadApplicationAssemblyExecutionContextStatus.READY
    changed_command = AssembleApplicationBundleCommand(
        subject_id=parts["plan"].subject_id,
        application_plan_id=parts["plan"].plan_id,
        plan_material_manifest_id=parts["manifest"].manifest_id,
        prepared_application_answer_set_id=parts["answer_set"].answer_set_id,
        now=NOW + timedelta(minutes=1),
        verified_profile_id=parts["profile"].profile_snapshot_id,
        verified_profile_version=parts["profile"].profile_contract_version,
        verified_profile_hash=parts["profile"].profile_snapshot_hash,
        execution_policy_record_id=second_policy.record_id,
        execution_policy_record_version=second_policy.record_contract_version,
        execution_policy_record_hash=second_policy_hash,
        execution_context_binding_hash=loaded.context.context_binding_hash,
    )
    changed = _run(parts, command=changed_command)
    assert changed.status is ApplicationBundleAssemblyStatus.CREATED
    assert changed.record.record_id != first.record.record_id


def test_legacy_record_remains_readable_but_cannot_satisfy_v2_replay(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    manifest = parts["manifest"]
    answers = parts["answer_set"]
    resume, cover = manifest.entries
    identity = {
        "contract_version": APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION_V1,
        "subject_id": parts["plan"].subject_id,
        "application_plan_id": parts["plan"].plan_id,
        "job_id": parts["plan"].job_id,
        "job_revision": parts["plan"].job_revision,
        "job_content_hash": parts["plan"].job_content_hash,
        "manifest_id": manifest.manifest_id,
        "manifest_content_hash": manifest.manifest_content_hash,
        "answer_set_id": answers.answer_set_id,
        "answer_set_content_hash": answers.answer_set_content_hash,
        "resume_entry_id": resume.entry_id,
        "resume_entry_hash": _hash(resume.to_dict()),
        "cover_letter_entry_id": cover.entry_id,
        "cover_letter_entry_hash": _hash(cover.to_dict()),
        "prepared_resume_material_id": manifest.prepared_resume_material_id,
        "prepared_resume_material_hash": manifest.prepared_resume_material_hash,
        "prepared_cover_letter_material_id": (
            manifest.prepared_cover_letter_material_id
        ),
        "prepared_cover_letter_material_hash": (
            manifest.prepared_cover_letter_material_hash
        ),
        "taxonomy_version": answers.taxonomy_version,
        "taxonomy_hash": answers.taxonomy_hash,
        "application_bundle_contract_version": "application-bundle-v1",
        "application_bundle_run_id": "legacy-synthetic-bundle",
        "application_bundle_canonical_hash": _hash("legacy-bundle"),
    }
    record_id = "application-bundle-assembly-" + _hash(identity)
    content = {
        **identity,
        "record_id": record_id,
        "assembled_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    legacy = ApplicationBundleAssemblyRecord(
        **identity,
        record_id=record_id,
        record_content_hash=_hash(content),
        assembled_at=NOW,
    )
    write = parts["assembly_repository"].save(legacy)
    assert write.status is ApplicationBundleAssemblyWriteStatus.CREATED
    read = parts["assembly_repository"].get(
        subject_id=parts["plan"].subject_id,
        record_id=legacy.record_id,
    )
    assert read.record == legacy
    assert read.record.execution_context_binding_hash is None

    created = _run(parts)
    assert created.status is ApplicationBundleAssemblyStatus.CREATED
    assert created.record.contract_version == (
        APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION
    )
    assert created.record.execution_context_binding_hash is not None
    assert "Synthetic" not in json.dumps(created.record.to_dict())
