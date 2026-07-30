from __future__ import annotations

import ast
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.plan_material_manifest as manifest_module
from core.application_preparation_orchestrator import (
    PreparationStageOutcome,
)
from core.plan_material_manifest import (
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
    RESUME_MEDIA_TYPE,
    AssemblePlanMaterialManifestCommand,
    PlanMaterialAssemblyState,
    PlanMaterialManifestFailureReason,
    PlanMaterialManifestNotReadyReason,
    PlanMaterialManifestReadStatus,
    PlanMaterialManifestStatus,
    PlanMaterialManifestWriteStatus,
    PlanMaterialProvenanceType,
    PlanMaterialRole,
    PrivateHomePlanMaterialManifestRepository,
    assemble_plan_material_manifest,
    prepared_material_content_hash,
    resume_manifest_entry_public_result,
)
from core.prepared_resume_material import (
    PreparedResumeMaterialStatus,
    PrivateHomePreparedResumeMaterialRepository,
    PublishPreparedResumeCommand,
    publish_prepared_resume,
)
from core.private_home import PrivateHome

from test_prepared_resume_material import _setup as _resume_setup


NOW = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)


def _setup(tmp_path: Path, *, subject_id: str = "subject-a", **kwargs):
    parts = _resume_setup(tmp_path, subject_id=subject_id, **kwargs)
    published = publish_prepared_resume(
        PublishPreparedResumeCommand(
            subject_id=subject_id,
            application_plan_id=parts["plan"].plan_id,
            now=NOW,
            resume_visual_qa_result_id=parts["visual_qa"].result_id,
        ),
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        fact_qa_repository=parts["fact_qa_repository"],
        latex_version_repository=parts["latex_repository"],
        compilation_repository=parts["compilation_repository"],
        visual_qa_repository=parts["visual_repository"],
        layout_revision_repository=parts["revision_repository"],
        material_repository=parts["material_repository"],
        home=parts["home"],
    )
    assert published.status is PreparedResumeMaterialStatus.CREATED
    parts["prepared"] = published.material
    parts["manifest_repository"] = (
        PrivateHomePlanMaterialManifestRepository(parts["home"])
    )
    return parts


def _assemble(
    parts,
    *,
    subject_id: str = "subject-a",
    material_id: str | None = None,
    now: datetime = NOW,
):
    return assemble_plan_material_manifest(
        AssemblePlanMaterialManifestCommand(
            subject_id=subject_id,
            application_plan_id=parts["plan"].plan_id,
            prepared_resume_material_id=(
                material_id or parts["prepared"].material_id
            ),
            now=now,
        ),
        application_plan_repository=parts["plan_repository"],
        prepared_resume_repository=parts["material_repository"],
        manifest_repository=parts["manifest_repository"],
        home=parts["home"],
    )


def _manifests(parts) -> tuple[Path, ...]:
    return tuple(
        parts["home"].paths.plan_material_manifests.rglob("*.json")
    )


def test_published_resume_assembles_a_typed_manifest(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    result = _assemble(parts)

    assert result.status is PlanMaterialManifestStatus.CREATED
    manifest = result.manifest
    assert manifest.contract_version == (
        PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION
    )
    assert manifest.subject_id == "subject-a"
    assert manifest.application_plan_id == parts["plan"].plan_id
    assert manifest.job_id == parts["plan"].job_id
    assert manifest.job_revision == parts["plan"].job_revision
    assert manifest.job_content_hash == parts["plan"].job_content_hash
    assert manifest.assembly_state is PlanMaterialAssemblyState.RESUME_ONLY
    assert manifest.assembled_at == NOW


def test_manifest_contains_exactly_one_resume_entry(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    manifest = _assemble(parts).manifest

    assert manifest.included_roles == (PlanMaterialRole.RESUME,)
    assert len(manifest.entries) == 1
    assert manifest.entries[0].material_role is PlanMaterialRole.RESUME
    assert manifest.entry_for_role(PlanMaterialRole.RESUME) is (
        manifest.entries[0]
    )
    roles = [item.material_role for item in manifest.entries]
    assert len(roles) == len(set(roles))


def test_resume_entry_binds_material_artifact_and_provenance(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    prepared = parts["prepared"]

    entry = _assemble(parts).manifest.entries[0]

    assert entry.prepared_material_id == prepared.material_id
    assert entry.artifact_reference == prepared.pdf_reference
    assert entry.artifact_sha256 == prepared.pdf_sha256
    assert entry.media_type == RESUME_MEDIA_TYPE
    assert entry.page_count == prepared.page_count
    assert entry.provenance_type is (
        PlanMaterialProvenanceType.PREPARED_RESUME_MATERIAL
    )
    assert entry.source_record_id == prepared.material_id
    assert entry.source_record_hash == prepared_material_content_hash(
        prepared
    )
    assert entry.entry_id.startswith("plan-material-entry-")


def test_manifest_does_not_claim_completeness_or_gate_a(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    manifest = _assemble(parts).manifest

    assert manifest.resume_prepared is True
    assert manifest.complete_application_material_prepared is False
    assert PlanMaterialRole.RESUME in PlanMaterialRole
    assert manifest.manifest_id == (
        "plan-material-manifest-"
        "b7131d74f73c70058fa5574078a78e46b31ba0d5cdc13c7ac223ba3ad654d828"
    )
    assert manifest.manifest_content_hash == (
        "86417c7b10f53a56640cdb7a9059827dff606d5b487c700dddd84c7b2a3066a4"
    )
    rendered = manifest.to_dict()
    assert "cover_letter" not in rendered
    assert "prior_manifest_id" not in rendered
    assert "prepared_cover_letter_material_id" not in rendered
    assert "application_answers" not in rendered
    assert not any("gate" in key.lower() for key in rendered)
    assert not any(
        "approv" in key.lower() or "submit" in key.lower()
        for key in rendered
    )


def test_missing_cover_letter_and_answers_create_no_placeholder(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    documents_before = tuple(
        sorted(parts["home"].paths.documents.rglob("*"))
    )

    manifest = _assemble(parts).manifest

    assert len(manifest.entries) == 1
    assert all(
        item.material_role is PlanMaterialRole.RESUME
        for item in manifest.entries
    )
    assert tuple(
        sorted(parts["home"].paths.documents.rglob("*"))
    ) == documents_before


def test_plan_binding_mismatch_is_not_ready(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    prepared = parts["prepared"]
    object.__setattr__(prepared, "job_id", "job-other")

    class _DriftedRepository:
        def get(self, **_kwargs):
            from core.prepared_resume_material import (
                PreparedResumeMaterialReadResult,
                PreparedResumeMaterialReadStatus,
            )

            return PreparedResumeMaterialReadResult(
                status=PreparedResumeMaterialReadStatus.FOUND,
                material=prepared,
            )

    parts["material_repository"] = _DriftedRepository()

    result = _assemble(parts)

    assert result.status is PlanMaterialManifestStatus.NOT_READY
    assert (
        result.not_ready_reason
        is PlanMaterialManifestNotReadyReason.PREPARED_RESUME_PLAN_MISMATCH
    )
    assert not _manifests(parts)


def test_unknown_prepared_material_is_not_ready(tmp_path: Path) -> None:
    parts = _setup(tmp_path)

    result = _assemble(
        parts, material_id="prepared-resume-material-" + "0" * 64
    )

    assert result.status is PlanMaterialManifestStatus.NOT_READY
    assert (
        result.not_ready_reason
        is PlanMaterialManifestNotReadyReason.PREPARED_RESUME_NOT_PUBLISHED
    )
    assert not _manifests(parts)


def test_subject_mismatch_on_the_plan_fails_closed(tmp_path: Path) -> None:
    parts = _setup(tmp_path)

    result = _assemble(parts, subject_id="subject-b")

    assert result.status is PlanMaterialManifestStatus.FAILED
    assert (
        result.reason_code
        is PlanMaterialManifestFailureReason
        .APPLICATION_PLAN_SUBJECT_MISMATCH
    )
    assert not _manifests(parts)


def test_pdf_drift_removal_and_page_drift_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    prepared = parts["prepared"]
    artifact = parts["home"].contained_path(prepared.pdf_reference)

    class _PassThrough:
        def __init__(self, material) -> None:
            self.material = material

        def get(self, **_kwargs):
            from core.prepared_resume_material import (
                PreparedResumeMaterialReadResult,
                PreparedResumeMaterialReadStatus,
            )

            return PreparedResumeMaterialReadResult(
                status=PreparedResumeMaterialReadStatus.FOUND,
                material=self.material,
            )

    parts["material_repository"] = _PassThrough(prepared)
    artifact.write_bytes(b"%PDF-1.4\ntampered\n%%EOF\n")
    drifted = _assemble(parts)

    object.__setattr__(prepared, "page_count", 9)
    artifact.write_bytes(parts["pdf"])
    page_drift = _assemble(parts)

    object.__setattr__(prepared, "page_count", 1)
    artifact.unlink()
    missing = _assemble(parts)

    assert (
        drifted.reason_code
        is PlanMaterialManifestFailureReason.ARTIFACT_HASH_DRIFT
    )
    assert (
        page_drift.reason_code
        is PlanMaterialManifestFailureReason.ARTIFACT_INVALID
    )
    assert (
        missing.reason_code
        is PlanMaterialManifestFailureReason.ARTIFACT_UNREADABLE
    )
    assert not _manifests(parts)


def test_assembly_never_copies_or_modifies_artifacts(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    compiled_before = {
        path: path.read_bytes()
        for path in parts["home"].paths.compiled_resumes.rglob("*.pdf")
    }
    prepared_before = tuple(
        sorted(parts["home"].paths.prepared_resume_materials.rglob("*"))
    )

    manifest = _assemble(parts).manifest

    compiled_after = {
        path: path.read_bytes()
        for path in parts["home"].paths.compiled_resumes.rglob("*.pdf")
    }
    assert compiled_after == compiled_before
    assert len(compiled_after) == 1
    assert tuple(
        sorted(parts["home"].paths.prepared_resume_materials.rglob("*"))
    ) == prepared_before
    assert manifest.entries[0].artifact_reference == (
        parts["prepared"].pdf_reference
    )


def test_no_fallback_to_legacy_directories_or_source_resume(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    legacy = parts["home"].paths.documents / "generated" / "job-one"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "resume.pdf").write_bytes(b"%PDF-1.4\nlegacy\n%%EOF\n")
    (legacy / "manifest.json").write_text("{}", encoding="utf-8")

    result = _assemble(
        parts, material_id="prepared-resume-material-" + "1" * 64
    )

    assert result.status is PlanMaterialManifestStatus.NOT_READY
    assert result.manifest is None
    assert not _manifests(parts)


def test_replay_returns_unchanged_without_duplicates(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    first = _assemble(parts)
    replay = _assemble(parts, now=NOW + timedelta(days=2))

    assert first.status is PlanMaterialManifestStatus.CREATED
    assert replay.status is PlanMaterialManifestStatus.UNCHANGED
    assert replay.manifest == first.manifest
    assert replay.manifest.assembled_at == NOW
    assert (
        resume_manifest_entry_public_result(first).outcome
        is PreparationStageOutcome.COMPLETED
    )
    assert (
        resume_manifest_entry_public_result(replay).outcome
        is PreparationStageOutcome.UNCHANGED
    )
    assert len(_manifests(parts)) == 1


def test_changed_prepared_material_creates_a_new_manifest(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _assemble(parts)
    other = _setup(tmp_path / "other", latex_marker=" % variant")

    second = assemble_plan_material_manifest(
        AssemblePlanMaterialManifestCommand(
            subject_id="subject-a",
            application_plan_id=other["plan"].plan_id,
            prepared_resume_material_id=other["prepared"].material_id,
            now=NOW + timedelta(minutes=1),
        ),
        application_plan_repository=other["plan_repository"],
        prepared_resume_repository=other["material_repository"],
        manifest_repository=other["manifest_repository"],
        home=other["home"],
    )

    assert second.status is PlanMaterialManifestStatus.CREATED
    assert second.manifest.manifest_id != first.manifest.manifest_id
    assert second.manifest.prepared_resume_material_id != (
        first.manifest.prepared_resume_material_id
    )
    assert second.manifest.prepared_resume_material_hash != (
        first.manifest.prepared_resume_material_hash
    )
    assert second.manifest.entries[0].entry_id != (
        first.manifest.entries[0].entry_id
    )
    kept = parts["manifest_repository"].get(
        subject_id="subject-a",
        manifest_id=first.manifest.manifest_id,
    )
    assert kept.manifest == first.manifest


def test_find_current_for_plan_is_deterministic(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    assembled = _assemble(parts)

    current = parts["manifest_repository"].find_current_for_plan(
        subject_id="subject-a",
        application_plan_id=parts["plan"].plan_id,
    )
    for path in _manifests(parts):
        path.touch()
    again = PrivateHomePlanMaterialManifestRepository(
        PrivateHome(parts["home"].root)
    ).find_current_for_plan(
        subject_id="subject-a",
        application_plan_id=parts["plan"].plan_id,
    )
    other_plan = parts["manifest_repository"].find_current_for_plan(
        subject_id="subject-a",
        application_plan_id="application-plan-" + "0" * 64,
    )

    assert current.manifest == assembled.manifest
    assert again.manifest == assembled.manifest
    assert other_plan.status is PlanMaterialManifestReadStatus.NOT_FOUND


def test_conflict_corruption_and_restart(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    assembled = _assemble(parts)
    manifest = assembled.manifest

    restarted = PrivateHomePlanMaterialManifestRepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(
        subject_id="subject-a", manifest_id=manifest.manifest_id
    )
    assert read.status is PlanMaterialManifestReadStatus.FOUND
    assert read.manifest == manifest
    assert read.manifest.manifest_content_hash == (
        manifest.manifest_content_hash
    )

    path = next(
        parts["home"].paths.plan_material_manifests.rglob(
            f"{manifest.manifest_id}.json"
        )
    )
    corrupted = b"{broken"
    path.write_bytes(corrupted)
    conflict = parts["manifest_repository"].save(manifest)
    corrupt = parts["manifest_repository"].get(
        subject_id="subject-a", manifest_id=manifest.manifest_id
    )

    assert conflict.status is PlanMaterialManifestWriteStatus.FAILED
    assert (
        conflict.reason_code
        is PlanMaterialManifestFailureReason.MANIFEST_INTEGRITY_FAILURE
    )
    assert path.read_bytes() == corrupted
    assert corrupt.status is PlanMaterialManifestReadStatus.INTEGRITY_FAILURE


def test_subject_isolation(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    assembled = _assemble(parts)

    cross = parts["manifest_repository"].get(
        subject_id="subject-b",
        manifest_id=assembled.manifest.manifest_id,
    )
    listed = parts["manifest_repository"].find_current_for_plan(
        subject_id="subject-b",
        application_plan_id=parts["plan"].plan_id,
    )

    assert cross.status is PlanMaterialManifestReadStatus.NOT_FOUND
    assert listed.status is PlanMaterialManifestReadStatus.NOT_FOUND


def test_invalid_command_and_missing_plan_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    naive = _assemble(parts, now=datetime(2026, 7, 30, 14, 0))
    missing = assemble_plan_material_manifest(
        AssemblePlanMaterialManifestCommand(
            subject_id="subject-a",
            application_plan_id="application-plan-" + "0" * 64,
            prepared_resume_material_id=parts["prepared"].material_id,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        prepared_resume_repository=parts["material_repository"],
        manifest_repository=parts["manifest_repository"],
        home=parts["home"],
    )

    assert (
        naive.reason_code
        is PlanMaterialManifestFailureReason.INVALID_REQUEST
    )
    assert (
        missing.reason_code
        is PlanMaterialManifestFailureReason.APPLICATION_PLAN_NOT_FOUND
    )
    assert not _manifests(parts)


def test_new_contract_is_separate_from_the_legacy_manifest() -> None:
    import core.materials as legacy

    assert hasattr(legacy, "MaterialManifest")
    assert hasattr(legacy, "load_material_manifest")
    assert hasattr(legacy, "build_tier_materials")
    assert not hasattr(legacy, "PlanMaterialManifest")
    assert not hasattr(manifest_module, "MaterialManifest")
    assert not hasattr(manifest_module, "load_material_manifest")
    assert manifest_module.PlanMaterialManifest is not (
        legacy.MaterialManifest
    )


def test_module_never_generates_material_or_reaches_execution() -> None:
    module_path = Path(manifest_module.__file__)
    text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = {
        "core.application_engine",
        "core.browser_broker",
        "core.latex_compiler",
        "core.materials",
        "core.pdf_page_renderer",
        "core.resume_candidates",
        "core.resume_layout_revision",
        "core.resume_tailoring",
        "core.resume_visual_qa",
        "playwright",
        "subprocess",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
    for name in (
        "publish_prepared_resume",
        "compile_resume_latex",
        "review_resume_visual_qa",
        "revise_resume_layout",
        "load_material_manifest",
        "build_tier_materials",
    ):
        assert name not in text
    assert "compiled_pdf_reference" not in text
    assert text.count("write_bytes_if_absent") == 1
