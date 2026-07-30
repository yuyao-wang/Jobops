from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import core.plan_material_manifest_cover_letter as inclusion_module
from core.application_preparation_orchestrator import (
    PreparationStageOutcome,
)
from core.plan_material_manifest import (
    PlanMaterialAssemblyState,
    PlanMaterialManifestFailureReason,
    PlanMaterialManifestNotReadyReason,
    PlanMaterialManifestReadStatus,
    PlanMaterialManifestStatus,
    PlanMaterialProvenanceType,
    PlanMaterialRole,
    PrivateHomePlanMaterialManifestRepository,
)
from core.plan_material_manifest_cover_letter import (
    IncludeCoverLetterInPlanMaterialManifestCommand,
    cover_letter_manifest_entry_public_result,
    include_cover_letter_in_plan_material_manifest,
)
from core.prepared_cover_letter_material import (
    PreparedCoverLetterMaterialReadResult,
    PreparedCoverLetterMaterialReadStatus,
    cover_letter_pdf_reference,
)
from core.private_home import PrivateHome

from test_plan_material_manifest import _assemble, _setup as _resume_setup
from test_prepared_cover_letter_material import (
    SUBJECT_ID,
    _publish as _publish_cover_letter,
    _setup as _cover_letter_setup,
)


NOW = datetime(2026, 8, 2, 15, 0, tzinfo=timezone.utc)


def _setup(tmp_path: Path):
    resume = _resume_setup(tmp_path, subject_id=SUBJECT_ID)
    prior_result = _assemble(
        resume, subject_id=SUBJECT_ID, now=NOW - timedelta(days=1)
    )
    assert prior_result.status is PlanMaterialManifestStatus.CREATED
    cover = _cover_letter_setup(tmp_path, subject_id=SUBJECT_ID)
    published = _publish_cover_letter(cover)
    assert published.status.value == "CREATED"
    return {
        "resume": resume,
        "cover": cover,
        "prior": prior_result.manifest,
        "material": published.material,
    }


def _include(parts, **overrides):
    command = overrides.pop(
        "command",
        IncludeCoverLetterInPlanMaterialManifestCommand(
            subject_id=SUBJECT_ID,
            application_plan_id=parts["resume"]["plan"].plan_id,
            plan_material_manifest_id=parts["prior"].manifest_id,
            prepared_cover_letter_material_id=parts["material"].material_id,
            now=NOW,
        ),
    )
    values = {
        "application_plan_repository": parts["resume"]["plan_repository"],
        "manifest_repository": parts["resume"]["manifest_repository"],
        "prepared_cover_letter_repository": (
            parts["cover"]["material_repository"]
        ),
        "home": parts["resume"]["home"],
    }
    values.update(overrides)
    return include_cover_letter_in_plan_material_manifest(command, **values)


def _records(parts) -> tuple[Path, ...]:
    return tuple(
        parts["resume"]["home"].paths.plan_material_manifests.rglob(
            "*.json"
        )
    )


def test_inclusion_creates_ordered_resume_and_cover_letter_manifest(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    result = _include(parts)

    assert result.status is PlanMaterialManifestStatus.CREATED
    manifest = result.manifest
    assert manifest.assembly_state is (
        PlanMaterialAssemblyState.RESUME_AND_COVER_LETTER
    )
    assert manifest.included_roles == (
        PlanMaterialRole.RESUME,
        PlanMaterialRole.COVER_LETTER,
    )
    assert tuple(item.order for item in manifest.entries) == (0, 1)
    assert manifest.complete_application_material_prepared is False


def test_resume_entry_is_preserved_field_for_field(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    before = parts["prior"].entries[0]

    manifest = _include(parts).manifest

    assert manifest.entries[0] == before
    assert manifest.entries[0].to_dict() == before.to_dict()
    assert manifest.preserved_resume_entry_hash == before.entry_id
    assert parts["prior"].to_dict()["entries"][0] == before.to_dict()


def test_cover_letter_entry_binds_material_artifact_and_provenance(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    material = parts["material"]

    entry = _include(parts).manifest.entries[1]

    assert entry.material_role is PlanMaterialRole.COVER_LETTER
    assert entry.prepared_material_id == material.material_id
    assert entry.artifact_reference == material.pdf_reference
    assert entry.artifact_sha256 == material.pdf_sha256
    assert entry.page_count == material.page_count
    assert entry.provenance_type is (
        PlanMaterialProvenanceType.PREPARED_COVER_LETTER_MATERIAL
    )
    assert entry.source_record_id == material.material_id
    assert entry.source_record_hash == material.material_content_hash


def test_manifest_identity_binds_prior_and_cover_letter(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    manifest = _include(parts).manifest

    assert manifest.prior_manifest_id == parts["prior"].manifest_id
    assert manifest.prior_manifest_content_hash == (
        parts["prior"].manifest_content_hash
    )
    assert manifest.prepared_cover_letter_material_id == (
        parts["material"].material_id
    )
    assert manifest.prepared_cover_letter_material_hash == (
        parts["material"].material_content_hash
    )
    assert manifest.cover_letter_artifact_sha256 == (
        parts["material"].pdf_sha256
    )


def test_replay_is_unchanged_and_preserves_assembled_at(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _include(parts)

    replay = _include(
        parts,
        command=replace(
            IncludeCoverLetterInPlanMaterialManifestCommand(
                subject_id=SUBJECT_ID,
                application_plan_id=parts["resume"]["plan"].plan_id,
                plan_material_manifest_id=parts["prior"].manifest_id,
                prepared_cover_letter_material_id=(
                    parts["material"].material_id
                ),
                now=NOW,
            ),
            now=NOW + timedelta(days=4),
        ),
    )

    assert replay.status is PlanMaterialManifestStatus.UNCHANGED
    assert replay.manifest == first.manifest
    assert replay.manifest.assembled_at == NOW
    assert (
        cover_letter_manifest_entry_public_result(first).outcome
        is PreparationStageOutcome.COMPLETED
    )
    assert (
        cover_letter_manifest_entry_public_result(replay).outcome
        is PreparationStageOutcome.UNCHANGED
    )
    assert len(_records(parts)) == 2


def test_already_included_same_cover_letter_is_unchanged(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _include(parts)
    command = IncludeCoverLetterInPlanMaterialManifestCommand(
        subject_id=SUBJECT_ID,
        application_plan_id=parts["resume"]["plan"].plan_id,
        plan_material_manifest_id=first.manifest.manifest_id,
        prepared_cover_letter_material_id=parts["material"].material_id,
        now=NOW + timedelta(minutes=1),
    )

    replay = _include(parts, command=command)

    assert replay.status is PlanMaterialManifestStatus.UNCHANGED
    assert replay.manifest == first.manifest
    assert len(_records(parts)) == 2


def test_different_cover_letter_creates_new_history_version(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _include(parts)
    second_cover = _cover_letter_setup(
        tmp_path, subject_id=SUBJECT_ID, draft_version="v2"
    )
    second_published = _publish_cover_letter(second_cover)
    command = IncludeCoverLetterInPlanMaterialManifestCommand(
        subject_id=SUBJECT_ID,
        application_plan_id=parts["resume"]["plan"].plan_id,
        plan_material_manifest_id=first.manifest.manifest_id,
        prepared_cover_letter_material_id=(
            second_published.material.material_id
        ),
        now=NOW + timedelta(minutes=1),
    )

    second = _include(
        parts,
        command=command,
        prepared_cover_letter_repository=(
            second_cover["material_repository"]
        ),
    )

    assert second.status is PlanMaterialManifestStatus.CREATED
    assert second.manifest.manifest_id != first.manifest.manifest_id
    assert second.manifest.prior_manifest_id == first.manifest.manifest_id
    assert second.manifest.entries[0] == first.manifest.entries[0]
    assert second.manifest.entries[1] != first.manifest.entries[1]
    kept = parts["resume"]["manifest_repository"].get(
        subject_id=SUBJECT_ID, manifest_id=first.manifest.manifest_id
    )
    assert kept.manifest == first.manifest


def test_plan_or_material_binding_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    drifted = parts["material"]
    object.__setattr__(drifted, "job_revision", 2)

    class _MaterialView:
        def get(self, **_kwargs):
            return PreparedCoverLetterMaterialReadResult(
                status=PreparedCoverLetterMaterialReadStatus.FOUND,
                material=drifted,
            )

    result = _include(
        parts, prepared_cover_letter_repository=_MaterialView()
    )

    assert result.status is PlanMaterialManifestStatus.NOT_READY
    assert result.not_ready_reason is (
        PlanMaterialManifestNotReadyReason
        .PREPARED_COVER_LETTER_PLAN_MISMATCH
    )
    assert len(_records(parts)) == 1


def test_missing_or_corrupt_prior_manifest_does_not_create_record(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    missing_command = IncludeCoverLetterInPlanMaterialManifestCommand(
        subject_id=SUBJECT_ID,
        application_plan_id=parts["resume"]["plan"].plan_id,
        plan_material_manifest_id="plan-material-manifest-" + "0" * 64,
        prepared_cover_letter_material_id=parts["material"].material_id,
        now=NOW,
    )
    missing = _include(parts, command=missing_command)
    record = next(
        path
        for path in _records(parts)
        if path.stem == parts["prior"].manifest_id
    )
    record.write_text("{broken", encoding="utf-8")
    corrupt = _include(parts)

    assert missing.status is PlanMaterialManifestStatus.NOT_READY
    assert corrupt.reason_code is (
        PlanMaterialManifestFailureReason.MANIFEST_INTEGRITY_FAILURE
    )
    assert len(_records(parts)) == 1


def test_pdf_missing_hash_signature_size_and_page_drift_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    material = parts["material"]
    artifact = parts["resume"]["home"].contained_path(
        material.pdf_reference
    )

    class _MaterialView:
        def get(self, **_kwargs):
            return PreparedCoverLetterMaterialReadResult(
                status=PreparedCoverLetterMaterialReadStatus.FOUND,
                material=material,
            )

    repository = _MaterialView()
    original = artifact.read_bytes()
    artifact.write_bytes(b"%PDF-1.4\ndrift\n%%EOF\n")
    hash_drift = _include(
        parts, prepared_cover_letter_repository=repository
    )
    artifact.write_bytes(original)
    object.__setattr__(material, "pdf_byte_size", len(original) + 1)
    size_drift = _include(
        parts, prepared_cover_letter_repository=repository
    )
    object.__setattr__(material, "pdf_byte_size", len(original))
    object.__setattr__(material, "page_count", 2)
    page_drift = _include(
        parts, prepared_cover_letter_repository=repository
    )
    object.__setattr__(material, "page_count", 1)
    signature = original.replace(b"%PDF-", b"%BAD-", 1)
    signature_hash = hashlib.sha256(signature).hexdigest()
    signature_reference = cover_letter_pdf_reference(
        subject_id=SUBJECT_ID, pdf_sha256=signature_hash
    )
    signature_path = parts["resume"]["home"].contained_path(
        signature_reference
    )
    parts["resume"]["home"].write_bytes_if_absent(
        signature_path, signature
    )
    object.__setattr__(material, "pdf_sha256", signature_hash)
    object.__setattr__(material, "pdf_reference", signature_reference)
    invalid = _include(parts, prepared_cover_letter_repository=repository)
    signature_path.unlink()
    missing = _include(parts, prepared_cover_letter_repository=repository)

    assert hash_drift.reason_code is (
        PlanMaterialManifestFailureReason.ARTIFACT_HASH_DRIFT
    )
    assert size_drift.reason_code is (
        PlanMaterialManifestFailureReason.ARTIFACT_INVALID
    )
    assert page_drift.reason_code is (
        PlanMaterialManifestFailureReason.ARTIFACT_INVALID
    )
    assert invalid.reason_code is (
        PlanMaterialManifestFailureReason.ARTIFACT_INVALID
    )
    assert missing.reason_code is (
        PlanMaterialManifestFailureReason.ARTIFACT_UNREADABLE
    )
    assert len(_records(parts)) == 1


def test_restart_current_selection_and_subject_isolation(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    created = _include(parts)
    restarted = PrivateHomePlanMaterialManifestRepository(
        PrivateHome(parts["resume"]["home"].root)
    )
    for path in _records(parts):
        if path.stem == parts["prior"].manifest_id:
            path.touch()

    read = restarted.get(
        subject_id=SUBJECT_ID,
        manifest_id=created.manifest.manifest_id,
    )
    current = restarted.find_current_for_plan(
        subject_id=SUBJECT_ID,
        application_plan_id=parts["resume"]["plan"].plan_id,
    )
    cross = restarted.get(
        subject_id="other-subject",
        manifest_id=created.manifest.manifest_id,
    )

    assert read.status is PlanMaterialManifestReadStatus.FOUND
    assert read.manifest == created.manifest
    assert current.manifest == created.manifest
    assert cross.status is PlanMaterialManifestReadStatus.NOT_FOUND


def test_repository_conflict_never_overwrites_history(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    created = _include(parts)
    path = next(
        item
        for item in _records(parts)
        if item.stem == created.manifest.manifest_id
    )
    corrupted = b"{corrupt"
    path.write_bytes(corrupted)

    replay = _include(parts)

    assert replay.status is PlanMaterialManifestStatus.FAILED
    assert replay.reason_code is (
        PlanMaterialManifestFailureReason.MANIFEST_INTEGRITY_FAILURE
    )
    assert path.read_bytes() == corrupted


def test_serialization_has_no_gate_answers_submission_or_ats_state(
    tmp_path: Path,
) -> None:
    rendered = _include(_setup(tmp_path)).manifest.to_dict()
    keys = " ".join(rendered).lower()

    assert "application_answers" not in rendered
    assert "gate" not in keys
    assert "approv" not in keys
    assert "submit" not in keys
    assert "ats" not in keys


def test_inclusion_does_not_copy_or_modify_pdf_artifacts(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    before = {
        path: path.read_bytes()
        for path in parts["resume"]["home"].paths.documents.rglob("*.pdf")
    }

    _include(parts)

    after = {
        path: path.read_bytes()
        for path in parts["resume"]["home"].paths.documents.rglob("*.pdf")
    }
    assert after == before


def test_service_has_no_generation_compilation_or_execution_dependency(
) -> None:
    text = Path(inclusion_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = {
        "core.application_engine",
        "core.browser_broker",
        "core.cover_letter_draft",
        "core.cover_letter_fact_qa",
        "core.latex_compiler",
        "core.materials",
        "subprocess",
    }

    assert not any(
        item == imported or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
    assert "compile(" not in text
    assert "write_bytes_if_absent" not in text


def test_legacy_material_manifest_remains_separate() -> None:
    import core.materials as legacy

    assert hasattr(legacy, "MaterialManifest")
    assert not hasattr(legacy, "PlanMaterialRole")
    assert not hasattr(inclusion_module, "MaterialManifest")
