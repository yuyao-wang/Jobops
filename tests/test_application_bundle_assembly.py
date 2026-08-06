"""Synthetic contract tests for P2c1 plan-scoped bundle assembly."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.application_bundle_assembly as assembly_module
from core.application_answer_taxonomy import (
    CanonicalApplicationAnswerKey,
    CanonicalApplicationAnswers,
)
from core.application_answers import (
    ApplicationAnswerPolicy,
    PrepareApplicationAnswersCommand,
    PreparedApplicationAnswerSetReadResult,
    PreparedApplicationAnswerSetReadStatus,
    PreparedApplicationAnswerSetStatus,
    PrivateHomeApplicationFactProvider,
    PrivateHomePreparedApplicationAnswerSetRepository,
    prepare_application_answers,
)
from core.application_bundle_assembly import (
    ApplicationBundleAssemblyFailureReason,
    ApplicationBundleAssemblyNotReadyReason,
    ApplicationBundleAssemblyReadStatus,
    ApplicationBundleAssemblyStatus,
    ApplicationBundleFactoryRequest,
    AssembleApplicationBundleCommand,
    PrivateHomeApplicationBundleAssemblyRepository,
    assemble_application_bundle,
)
from core.application_assembly_execution_context import (
    LoadApplicationAssemblyExecutionContextCommand,
    LoadApplicationAssemblyExecutionContextStatus,
    load_application_assembly_execution_context,
)
from core.application_execution_profile import (
    ApplicationExecutionIdentityFieldKey,
)
from core.candidate_identity_facts import (
    CandidateIdentityFactSourceKind,
    CandidateIdentityFactSourceRef,
    CandidateIdentityFactVerificationStatus,
    PrivateHomeCandidateIdentityFactRepository,
    WriteCandidateIdentityFactCommand,
)
from core.bundles import (
    ApplicationBundle,
    JobSpec,
    ManagedArtifactReference,
)
from core.plan_material_manifest import (
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1,
    PlanMaterialManifestReadResult,
    PlanMaterialManifestReadStatus,
)
from core.policy import (
    AutonomyMode,
    JobTier,
    PolicyConfig,
    PolicyEngine,
    RiskSignals,
)
from core.plan_execution_policy import (
    PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION,
    PlanExecutionPolicyDecisionRecord,
    PrivateHomePlanExecutionPolicyDecisionRepository,
    plan_execution_policy_plan_binding_hash,
    plan_execution_policy_record_hash,
    policy_decision_to_dict,
)
from core.private_home import PrivateHome
from core.recoverable_application_bundle import (
    PrivateHomeRecoverableApplicationBundleEnvelopeRepository,
)
from core.verified_application_execution_profile import (
    PrivateHomeVerifiedApplicationExecutionProfileRepository,
    ProjectVerifiedApplicationExecutionProfileCommand,
    ProjectVerifiedApplicationExecutionProfileStatus,
    project_verified_application_execution_profile,
)

from test_plan_material_manifest_cover_letter import (
    SUBJECT_ID,
    _include,
    _setup as _manifest_setup,
)


NOW = datetime(2026, 8, 5, 16, 0, tzinfo=timezone.utc)


class _Factory:
    def __init__(self) -> None:
        self.requests: list[ApplicationBundleFactoryRequest] = []

    def create(
        self, request: ApplicationBundleFactoryRequest
    ) -> ApplicationBundle:
        self.requests.append(request)
        posting = request.job_posting
        profile = request.identity_profile.to_application_bundle_profile()
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
            profile={"personal": dict(profile["personal"])},
            answers=request.answers,
            policy=request.policy_decision,
        )


def _mapping_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _execution_context(
    home: PrivateHome,
    plan,
    plan_repository,
    *,
    mode: AutonomyMode = AutonomyMode.SUPERVISED,
    tier: JobTier = JobTier.MEDIUM,
):
    facts = PrivateHomeCandidateIdentityFactRepository(home)
    for key, value in (
        (ApplicationExecutionIdentityFieldKey.FIRST_NAME, "Synthetic"),
        (ApplicationExecutionIdentityFieldKey.LAST_NAME, "Candidate"),
        (
            ApplicationExecutionIdentityFieldKey.EMAIL,
            "synthetic@example.test",
        ),
    ):
        source = CandidateIdentityFactSourceRef(
            source_kind=CandidateIdentityFactSourceKind.USER_CONFIRMATION,
            source_id=f"confirmation-{key.value}",
            source_version="v1",
            source_hash=hashlib.sha256(
                f"source-{key.value}".encode()
            ).hexdigest(),
            source_locator=f"review:{key.value}",
            source_subject_id=plan.subject_id,
        )
        facts.write(
            WriteCandidateIdentityFactCommand(
                subject_id=plan.subject_id,
                field_key=key,
                submitted_value=value,
                verification_status=(
                    CandidateIdentityFactVerificationStatus.USER_CONFIRMED
                ),
                source_ref=source,
                expected_current_fact_id=None,
                invocation_id=f"fact-{key.value}",
                now=NOW,
            )
        )
    profiles = PrivateHomeVerifiedApplicationExecutionProfileRepository(home)
    projected = project_verified_application_execution_profile(
        ProjectVerifiedApplicationExecutionProfileCommand(
            subject_id=plan.subject_id,
            application_plan_id=plan.plan_id,
            invocation_id="bundle-test-profile",
            now=NOW,
        ),
        plan_repository=plan_repository,
        fact_repository=facts,
        repository=profiles,
    )
    assert (
        projected.status
        is ProjectVerifiedApplicationExecutionProfileStatus.CREATED
    )
    profile = projected.snapshot
    assert profile is not None

    decision = PolicyEngine(
        PolicyConfig(mode=mode)
    ).decide(tier, RiskSignals())
    decision_hash = _mapping_hash(policy_decision_to_dict(decision))
    input_hash = hashlib.sha256(b"synthetic-policy-input").hexdigest()
    record_id = "plan-execution-policy-" + _mapping_hash(
        {
            "input_binding_hash": input_hash,
            "policy_decision_hash": decision_hash,
            "record_contract_version": (
                PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
            ),
        }
    )
    policy = PlanExecutionPolicyDecisionRecord(
        record_id=record_id,
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        accepted_intent_id=plan.accepted_job_intent_id,
        accepted_intent_hash=hashlib.sha256(b"intent").hexdigest(),
        priority_decision_id=plan.priority_decision_id,
        priority_decision_hash=hashlib.sha256(b"priority").hexdigest(),
        prioritization_policy_id=plan.policy_id,
        prioritization_policy_version=plan.policy_version,
        prioritization_policy_hash=plan.policy_content_hash,
        plan_binding_hash=plan_execution_policy_plan_binding_hash(plan),
        execution_rules_version="plan-execution-policy-rules-v1",
        execution_configuration_id="synthetic-execution-policy",
        execution_configuration_version=1,
        execution_configuration_hash=hashlib.sha256(
            b"configuration"
        ).hexdigest(),
        policy_decision=decision,
        policy_decision_hash=decision_hash,
        input_binding_hash=input_hash,
        created_at=NOW,
        invocation_id="bundle-test-policy",
    )
    policies = PrivateHomePlanExecutionPolicyDecisionRepository(home)
    policies.save(policy)
    policy_hash = plan_execution_policy_record_hash(policy)
    loaded = load_application_assembly_execution_context(
        LoadApplicationAssemblyExecutionContextCommand(
            subject_id=plan.subject_id,
            application_plan=plan,
            job_id=plan.job_id,
            verified_profile_id=profile.profile_snapshot_id,
            verified_profile_hash=profile.profile_snapshot_hash,
            execution_policy_record_id=policy.record_id,
            execution_policy_record_hash=policy_hash,
        ),
        verified_profile_provider=profiles,
        execution_policy_provider=policies,
    )
    assert (
        loaded.status
        is LoadApplicationAssemblyExecutionContextStatus.READY
    )
    assert loaded.context is not None
    return profiles, policies, profile, policy, policy_hash, loaded.context


def _write_facts(
    home: PrivateHome,
    subject_id: str,
    *,
    email: str = "synthetic@example.test",
) -> None:
    paths = home.ensure()
    paths.profile_facts.write_text(
        json.dumps(
            {
                "normalized": {},
                "schema_version": 1,
                "subject_id": subject_id,
            }
        ),
        encoding="utf-8",
    )
    paths.verified_answers.write_text(
        json.dumps(
            {
                "answers": {
                    "email": {
                        "confirmed_at": NOW.isoformat(),
                        "expires_at": None,
                        "fact_id": "fact-email",
                        "recorded_at": (
                            NOW - timedelta(days=1)
                        ).isoformat(),
                        "scope": {},
                        "sensitivity": "BASIC",
                        "source": "synthetic_candidate_confirmation",
                        "source_classification": "VERIFIED_FACT",
                        "source_record_id": "record-fact-email",
                        "value": email,
                        "verified": True,
                    }
                },
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    paths.policy.write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )


def _setup(
    tmp_path: Path,
    *,
    blocking: bool = False,
    execution_mode: AutonomyMode = AutonomyMode.SUPERVISED,
    execution_tier: JobTier = JobTier.MEDIUM,
):
    parts = _manifest_setup(tmp_path)
    manifest_result = _include(parts)
    manifest = manifest_result.manifest
    assert manifest is not None
    home = parts["resume"]["home"]
    plan = parts["resume"]["plan"]
    _write_facts(home, SUBJECT_ID)
    policy = (
        ApplicationAnswerPolicy.default()
        if blocking
        else ApplicationAnswerPolicy.create(
            policy_id="application-answer-policy-bundle-test-v1",
            tracked_keys=(
                CanonicalApplicationAnswerKey.EMAIL,
                CanonicalApplicationAnswerKey.LOCATION,
            ),
        )
    )
    answer_repository = PrivateHomePreparedApplicationAnswerSetRepository(
        home
    )
    prepared = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT_ID,
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=parts["resume"]["plan_repository"],
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=policy,
        answer_set_repository=answer_repository,
    )
    assert prepared.status is PreparedApplicationAnswerSetStatus.CREATED
    factory = _Factory()
    repository = PrivateHomeApplicationBundleAssemblyRepository(home)
    envelope_repository = (
        PrivateHomeRecoverableApplicationBundleEnvelopeRepository(home)
    )
    (
        profile_repository,
        policy_repository,
        profile,
        execution_policy,
        execution_policy_hash,
        execution_context,
    ) = _execution_context(
        home,
        plan,
        parts["resume"]["plan_repository"],
        mode=execution_mode,
        tier=execution_tier,
    )
    return {
        "answer_repository": answer_repository,
        "answer_set": prepared.answer_set,
        "assembly_repository": repository,
        "envelope_repository": envelope_repository,
        "factory": factory,
        "home": home,
        "job_repository": parts["cover"]["job_repository"],
        "manifest": manifest,
        "manifest_repository": parts["resume"]["manifest_repository"],
        "profile_repository": profile_repository,
        "policy_repository": policy_repository,
        "profile": profile,
        "execution_policy": execution_policy,
        "execution_policy_hash": execution_policy_hash,
        "execution_context": execution_context,
        "parts": parts,
        "plan": plan,
        "plan_repository": parts["resume"]["plan_repository"],
    }


def _run(parts, **overrides):
    command = overrides.pop(
        "command",
        AssembleApplicationBundleCommand(
            subject_id=SUBJECT_ID,
            application_plan_id=parts["plan"].plan_id,
            plan_material_manifest_id=parts["manifest"].manifest_id,
            prepared_application_answer_set_id=(
                parts["answer_set"].answer_set_id
            ),
            now=NOW,
            verified_profile_id=parts["profile"].profile_snapshot_id,
            verified_profile_version=(
                parts["profile"].profile_contract_version
            ),
            verified_profile_hash=parts["profile"].profile_snapshot_hash,
            execution_policy_record_id=(
                parts["execution_policy"].record_id
            ),
            execution_policy_record_version=(
                parts["execution_policy"].record_contract_version
            ),
            execution_policy_record_hash=parts["execution_policy_hash"],
            execution_context_binding_hash=(
                parts["execution_context"].context_binding_hash
            ),
        ),
    )
    if command.verified_profile_id is None:
        command = replace(
            command,
            verified_profile_id=parts["profile"].profile_snapshot_id,
            verified_profile_version=(
                parts["profile"].profile_contract_version
            ),
            verified_profile_hash=parts["profile"].profile_snapshot_hash,
            execution_policy_record_id=(
                parts["execution_policy"].record_id
            ),
            execution_policy_record_version=(
                parts["execution_policy"].record_contract_version
            ),
            execution_policy_record_hash=parts["execution_policy_hash"],
            execution_context_binding_hash=(
                parts["execution_context"].context_binding_hash
            ),
        )
    values = {
        "application_plan_repository": parts["plan_repository"],
        "job_posting_repository": parts["job_repository"],
        "plan_material_manifest_repository": parts["manifest_repository"],
        "answer_set_repository": parts["answer_repository"],
        "verified_execution_profile_provider": (
            parts["profile_repository"]
        ),
        "plan_execution_policy_provider": parts["policy_repository"],
        "bundle_factory": parts["factory"],
        "assembly_repository": parts["assembly_repository"],
        "bundle_envelope_repository": parts["envelope_repository"],
        "private_home": parts["home"],
    }
    values.update(overrides)
    return assemble_application_bundle(command, **values)


def test_complete_preparation_creates_existing_application_bundle(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    result = _run(parts)

    assert result.status is ApplicationBundleAssemblyStatus.CREATED
    assert isinstance(result.bundle, ApplicationBundle)
    assert isinstance(result.bundle.answers, CanonicalApplicationAnswers)
    assert result.bundle.answers.to_dict() == {
        "email": "synthetic@example.test"
    }
    assert result.bundle.materials.resume_sha256 == (
        parts["manifest"].entries[0].artifact_sha256
    )
    assert result.bundle.materials.cover_letter == ""
    assert result.bundle.materials.cover_letter_pdf == ManagedArtifactReference(
        reference=parts["manifest"].entries[1].artifact_reference,
        sha256=parts["manifest"].entries[1].artifact_sha256,
        byte_size=parts["manifest"].entries[1].artifact_byte_size,
        media_type="application/pdf",
    )
    assert result.record.manifest_id == parts["manifest"].manifest_id
    assert result.record.answer_set_id == parts["answer_set"].answer_set_id
    assert result.record.prepared_resume_material_id == (
        parts["manifest"].prepared_resume_material_id
    )
    assert result.record.prepared_cover_letter_material_id == (
        parts["manifest"].prepared_cover_letter_material_id
    )
    with pytest.raises(FrozenInstanceError):
        result.record.job_id = "changed"


def test_factory_receives_only_exact_prepared_inputs(tmp_path: Path) -> None:
    parts = _setup(tmp_path)

    result = _run(parts)

    request = parts["factory"].requests[0]
    assert request.application_plan == parts["plan"]
    assert request.subject_id == SUBJECT_ID
    assert request.materials == result.bundle.materials
    assert request.answers == result.bundle.answers
    assert request.job_posting.job_id == parts["plan"].job_id


def test_runtime_conditional_attestations_do_not_block_bundle_assembly(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path, blocking=True)

    result = _run(parts)

    assert result.status is ApplicationBundleAssemblyStatus.CREATED
    assert len(parts["factory"].requests) == 1


def test_nonblocking_optional_unresolved_does_not_block(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    answer_set = parts["answer_set"]
    # P2b3b already guarantees all safe answers survive unresolved items.
    assert answer_set.unresolved_items
    assert not any(item.blocking for item in answer_set.unresolved_items)

    result = _run(parts)

    assert result.status is ApplicationBundleAssemblyStatus.CREATED


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        (
            "subject_id",
            ApplicationBundleAssemblyFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
        ),
        (
            "job_revision",
            ApplicationBundleAssemblyFailureReason
            .JOB_POSTING_BINDING_MISMATCH,
        ),
    ],
)
def test_subject_and_job_bindings_fail_closed(
    tmp_path: Path, field: str, reason
) -> None:
    parts = _setup(tmp_path)
    if field == "subject_id":
        command = replace(
            AssembleApplicationBundleCommand(
                SUBJECT_ID,
                parts["plan"].plan_id,
                parts["manifest"].manifest_id,
                parts["answer_set"].answer_set_id,
                NOW,
            ),
            subject_id="another-subject",
        )
        result = _run(parts, command=command)
    else:
        posting = parts["parts"]["cover"]["job"]
        changed = replace(posting, revision=posting.revision + 1)

        class _Jobs:
            def get(self, _job_id):
                return changed

        result = _run(parts, job_posting_repository=_Jobs())

    assert result.status is ApplicationBundleAssemblyStatus.FAILED
    assert result.failure_reason is reason
    assert parts["factory"].requests == []


def test_manifest_and_answer_set_binding_mismatches_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    manifest = parts["manifest"]
    object.__setattr__(manifest, "application_plan_id", "wrong-plan")

    class _ManifestRepository:
        def get(self, **_kwargs):
            return PlanMaterialManifestReadResult(
                PlanMaterialManifestReadStatus.FOUND, manifest
            )

    manifest_result = _run(
        parts, plan_material_manifest_repository=_ManifestRepository()
    )
    object.__setattr__(
        parts["answer_set"], "application_plan_id", "wrong-plan"
    )
    answer_read = PreparedApplicationAnswerSetReadResult(
        PreparedApplicationAnswerSetReadStatus.FOUND, parts["answer_set"]
    )

    class _Answers:
        def get(self, **_kwargs):
            return answer_read

    # Restore only the manifest field so the second assertion reaches answers.
    object.__setattr__(manifest, "application_plan_id", parts["plan"].plan_id)
    answer_result = _run(parts, answer_set_repository=_Answers())

    assert manifest_result.failure_reason is (
        ApplicationBundleAssemblyFailureReason.MANIFEST_BINDING_MISMATCH
    )
    assert answer_result.failure_reason is (
        ApplicationBundleAssemblyFailureReason
        .ANSWER_SET_BINDING_MISMATCH
    )


def test_incomplete_or_v1_manifest_cannot_be_execution_ready(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    manifest = parts["manifest"]

    class _ManifestRepository:
        def get(self, **_kwargs):
            return PlanMaterialManifestReadResult(
                PlanMaterialManifestReadStatus.FOUND, manifest
            )

    object.__setattr__(manifest, "entries", (manifest.entries[0],))
    incomplete = _run(
        parts, plan_material_manifest_repository=_ManifestRepository()
    )
    # Reload the pristine immutable record before checking explicit v1 routing.
    pristine = parts["manifest_repository"].get(
        subject_id=SUBJECT_ID,
        manifest_id=manifest.manifest_id,
    ).manifest
    object.__setattr__(
        pristine,
        "contract_version",
        PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1,
    )
    manifest = pristine
    legacy = _run(
        parts, plan_material_manifest_repository=_ManifestRepository()
    )

    assert incomplete.status is ApplicationBundleAssemblyStatus.NOT_READY
    assert incomplete.not_ready_reason is (
        ApplicationBundleAssemblyNotReadyReason
        .REQUIRED_MATERIALS_INCOMPLETE
    )
    assert legacy.failure_reason is (
        ApplicationBundleAssemblyFailureReason
        .MANIFEST_VERSION_INCOMPATIBLE
    )


@pytest.mark.parametrize(
    "drift", ["missing", "hash", "size", "signature", "page_count"]
)
def test_managed_pdf_anomalies_fail_closed(
    tmp_path: Path, drift: str
) -> None:
    parts = _setup(tmp_path)
    entry = parts["manifest"].entries[1]
    path = parts["home"].contained_path(entry.artifact_reference)
    if drift == "missing":
        path.unlink()
    elif drift == "signature":
        path.write_bytes(b"not-a-pdf")
    elif drift == "hash":
        object.__setattr__(entry, "artifact_sha256", "0" * 64)
    elif drift == "page_count":
        object.__setattr__(entry, "page_count", entry.page_count + 1)
    else:
        object.__setattr__(
            entry, "artifact_byte_size", entry.artifact_byte_size + 1
        )

    class _ManifestRepository:
        def get(self, **_kwargs):
            return PlanMaterialManifestReadResult(
                PlanMaterialManifestReadStatus.FOUND, parts["manifest"]
            )

    result = _run(
        parts, plan_material_manifest_repository=_ManifestRepository()
    )

    assert result.status is ApplicationBundleAssemblyStatus.FAILED
    assert result.failure_reason is (
        ApplicationBundleAssemblyFailureReason.ARTIFACT_INTEGRITY_FAILURE
    )
    assert parts["factory"].requests == []


def test_symlink_and_subject_escape_are_rejected(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    entry = parts["manifest"].entries[1]
    path = parts["home"].contained_path(entry.artifact_reference)
    target = path.with_name("target.pdf")
    path.rename(target)
    path.symlink_to(target)

    class _ManifestRepository:
        def get(self, **_kwargs):
            return PlanMaterialManifestReadResult(
                PlanMaterialManifestReadStatus.FOUND, parts["manifest"]
            )

    result = _run(
        parts, plan_material_manifest_repository=_ManifestRepository()
    )

    assert result.failure_reason is (
        ApplicationBundleAssemblyFailureReason.ARTIFACT_INTEGRITY_FAILURE
    )
    assert parts["factory"].requests == []


def test_cross_subject_artifact_reference_is_rejected(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    entry = parts["manifest"].entries[1]
    source = parts["home"].contained_path(entry.artifact_reference)
    other_key = "subject-" + "f" * 64
    target = (
        parts["home"].paths.compiled_cover_letters
        / other_key
        / source.name
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    object.__setattr__(
        entry,
        "artifact_reference",
        str(target.relative_to(parts["home"].paths.root)),
    )

    class _ManifestRepository:
        def get(self, **_kwargs):
            return PlanMaterialManifestReadResult(
                PlanMaterialManifestReadStatus.FOUND, parts["manifest"]
            )

    result = _run(
        parts, plan_material_manifest_repository=_ManifestRepository()
    )

    assert result.failure_reason is (
        ApplicationBundleAssemblyFailureReason.ARTIFACT_INTEGRITY_FAILURE
    )
    assert parts["factory"].requests == []


def test_replay_is_unchanged_and_preserves_original_time(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _run(parts)
    command = AssembleApplicationBundleCommand(
        SUBJECT_ID,
        parts["plan"].plan_id,
        parts["manifest"].manifest_id,
        parts["answer_set"].answer_set_id,
        NOW + timedelta(days=3),
    )

    replay = _run(parts, command=command)

    assert replay.status is ApplicationBundleAssemblyStatus.UNCHANGED
    assert replay.record == first.record
    assert replay.record.assembled_at == NOW
    assert len(
        tuple(parts["home"].paths.application_bundle_assemblies.rglob("*.json"))
    ) == 1


def test_changed_answer_set_creates_immutable_history(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _run(parts)
    _write_facts(
        parts["home"],
        SUBJECT_ID,
        email="changed-synthetic@example.test",
    )
    policy = ApplicationAnswerPolicy.create(
        policy_id="application-answer-policy-bundle-test-v1",
        tracked_keys=(
            CanonicalApplicationAnswerKey.EMAIL,
            CanonicalApplicationAnswerKey.LOCATION,
        ),
    )
    changed_answers = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT_ID,
            application_plan_id=parts["plan"].plan_id,
            now=NOW + timedelta(minutes=1),
        ),
        application_plan_repository=parts["plan_repository"],
        fact_provider=PrivateHomeApplicationFactProvider(parts["home"]),
        answer_policy=policy,
        answer_set_repository=parts["answer_repository"],
    ).answer_set
    command = AssembleApplicationBundleCommand(
        SUBJECT_ID,
        parts["plan"].plan_id,
        parts["manifest"].manifest_id,
        changed_answers.answer_set_id,
        NOW + timedelta(minutes=1),
    )

    changed = _run(parts, command=command)

    assert changed.status is ApplicationBundleAssemblyStatus.CREATED
    assert changed.record.record_id != first.record.record_id
    assert len(
        tuple(parts["home"].paths.application_bundle_assemblies.rglob("*.json"))
    ) == 2


def test_restart_current_lookup_is_stable_and_ignores_mtime(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    created = _run(parts)
    path = next(
        parts["home"].paths.application_bundle_assemblies.rglob("*.json")
    )
    os.utime(path, (1, 1))
    restarted = PrivateHomeApplicationBundleAssemblyRepository(parts["home"])

    read = restarted.get(
        subject_id=SUBJECT_ID, record_id=created.record.record_id
    )
    current = restarted.find_current_for_plan(
        subject_id=SUBJECT_ID,
        application_plan_id=parts["plan"].plan_id,
    )

    assert read.status is ApplicationBundleAssemblyReadStatus.FOUND
    assert read.record == created.record
    assert current.record == created.record


def test_corrupt_record_and_factory_input_change_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    created = _run(parts)
    path = next(
        parts["home"].paths.application_bundle_assemblies.rglob("*.json")
    )
    path.write_text("{}", encoding="utf-8")
    read = parts["assembly_repository"].get(
        subject_id=SUBJECT_ID, record_id=created.record.record_id
    )

    class _ChangingFactory(_Factory):
        def create(self, request):
            bundle = super().create(request)
            return replace(bundle, materials=replace(bundle.materials, metadata={}))

    changed = _run(parts, bundle_factory=_ChangingFactory())

    assert read.status is (
        ApplicationBundleAssemblyReadStatus.INTEGRITY_FAILURE
    )
    assert changed.failure_reason is (
        ApplicationBundleAssemblyFailureReason.RECORD_INTEGRITY_FAILURE
    )


def test_source_keeps_execution_and_preparation_boundaries() -> None:
    tree = ast.parse(
        Path(assembly_module.__file__).read_text(encoding="utf-8")
    )
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(alias.name for alias in node.names)

    assert not any(
        marker in name
        for name in imported
        for marker in (
            "application_engine",
            "semantic_mapper",
            "adapters",
            "browser",
            "application_preparation_orchestrator",
            "selective_batch",
        )
    )
