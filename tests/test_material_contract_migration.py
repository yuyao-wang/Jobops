"""P2c0 compatibility tests for manifest v2 and managed Cover Letter PDFs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.bundles import (
    ApplicationBundle,
    JobSpec,
    ManagedArtifactReference,
    MaterialBundle,
    canonical_hash,
)
from core.plan_material_manifest import (
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1,
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2,
    PlanMaterialAssemblyState,
    PlanMaterialEntry,
    PlanMaterialManifest,
    PlanMaterialManifestNotReadyReason,
    PlanMaterialManifestReadStatus,
    PlanMaterialManifestStatus,
    PlanMaterialProvenanceType,
    PlanMaterialRole,
    PrivateHomePlanMaterialManifestRepository,
    plan_material_entry_id,
    plan_material_manifest_content_hash,
    plan_material_manifest_id,
)
from core.plan_material_manifest_cover_letter import (
    IncludeCoverLetterInPlanMaterialManifestCommand,
)
from core.policy import (
    AutonomyMode,
    JobTier,
    PolicyConfig,
    PolicyEngine,
    RiskSignals,
)
from core.private_home import PrivateHome
from test_plan_material_manifest_cover_letter import (
    NOW,
    _include,
    _setup,
)


FIXTURES = Path(__file__).parent / "fixtures"
V1_SUBJECT = "subject-v1-fixture"
V1_RESUME_BYTES = b"%PDF-1.4\n% fixed resume v1 fixture\n%%EOF\n"
V1_COVER_BYTES = b"%PDF-1.4\n% fixed cover v1 fixture\n%%EOF\n"


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode()).hexdigest()


def _install_v1_fixture(
    home: PrivateHome, fixture_name: str
) -> tuple[bytes, dict]:
    raw = (FIXTURES / fixture_name).read_bytes()
    value = json.loads(raw)
    entries = value["entries"]
    artifacts = [V1_RESUME_BYTES]
    if len(entries) == 2:
        artifacts.append(V1_COVER_BYTES)
    home.ensure()
    for entry, content in zip(entries, artifacts, strict=True):
        home.write_bytes(entry["artifact_reference"], content)
    record = (
        home.paths.plan_material_manifests
        / _subject_key(value["subject_id"])
        / f"{value['manifest_id']}.json"
    )
    home.write_bytes(record, raw)
    return raw, value


def _v1_projection_from_v2(
    manifest: PlanMaterialManifest,
) -> PlanMaterialManifest:
    resume_v2 = manifest.entries[0]
    entry_content = resume_v2.content_dict()
    entry_content.pop("artifact_byte_size")
    resume_v1 = PlanMaterialEntry(
        entry_id=plan_material_entry_id(entry_content),
        order=resume_v2.order,
        material_role=resume_v2.material_role,
        prepared_material_id=resume_v2.prepared_material_id,
        artifact_reference=resume_v2.artifact_reference,
        artifact_sha256=resume_v2.artifact_sha256,
        media_type=resume_v2.media_type,
        page_count=resume_v2.page_count,
        provenance_type=resume_v2.provenance_type,
        source_record_id=resume_v2.source_record_id,
        source_record_hash=resume_v2.source_record_hash,
        artifact_byte_size=None,
        contract_version=PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1,
    )
    identity = {
        "contract_version": PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1,
        "subject_id": manifest.subject_id,
        "application_plan_id": manifest.application_plan_id,
        "job_id": manifest.job_id,
        "job_revision": manifest.job_revision,
        "job_content_hash": manifest.job_content_hash,
        "prepared_resume_material_id": (
            manifest.prepared_resume_material_id
        ),
        "prepared_resume_material_hash": (
            manifest.prepared_resume_material_hash
        ),
        "resume_artifact_sha256": manifest.resume_artifact_sha256,
        "entry_hashes": (resume_v1.entry_id,),
        "artifact_byte_sizes": None,
        "assembly_state": PlanMaterialAssemblyState.RESUME_ONLY,
    }
    manifest_id = plan_material_manifest_id(**identity)
    content = {
        "application_plan_id": manifest.application_plan_id,
        "assembly_state": PlanMaterialAssemblyState.RESUME_ONLY.value,
        "contract_version": PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1,
        "entries": [resume_v1.to_dict()],
        "included_roles": [PlanMaterialRole.RESUME.value],
        "job_content_hash": manifest.job_content_hash,
        "job_id": manifest.job_id,
        "job_revision": manifest.job_revision,
        "manifest_id": manifest_id,
        "prepared_resume_material_hash": (
            manifest.prepared_resume_material_hash
        ),
        "prepared_resume_material_id": (
            manifest.prepared_resume_material_id
        ),
        "resume_artifact_sha256": manifest.resume_artifact_sha256,
        "subject_id": manifest.subject_id,
    }
    return PlanMaterialManifest(
        manifest_id=manifest_id,
        contract_version=PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V1,
        subject_id=manifest.subject_id,
        application_plan_id=manifest.application_plan_id,
        job_id=manifest.job_id,
        job_revision=manifest.job_revision,
        job_content_hash=manifest.job_content_hash,
        prepared_resume_material_id=manifest.prepared_resume_material_id,
        prepared_resume_material_hash=(
            manifest.prepared_resume_material_hash
        ),
        resume_artifact_sha256=manifest.resume_artifact_sha256,
        assembly_state=PlanMaterialAssemblyState.RESUME_ONLY,
        included_roles=(PlanMaterialRole.RESUME,),
        entries=(resume_v1,),
        manifest_content_hash=plan_material_manifest_content_hash(content),
        assembled_at=manifest.assembled_at,
    )


@pytest.mark.parametrize(
    "fixture_name",
    (
        "plan_material_manifest_v1_resume_only.json",
        "plan_material_manifest_v1_resume_cover.json",
    ),
)
def test_fixed_v1_records_read_without_byte_or_identity_change(
    tmp_path: Path, fixture_name: str
) -> None:
    home = PrivateHome(tmp_path / "private")
    raw, value = _install_v1_fixture(home, fixture_name)
    repository = PrivateHomePlanMaterialManifestRepository(home)
    record_path = (
        home.paths.plan_material_manifests
        / _subject_key(V1_SUBJECT)
        / f"{value['manifest_id']}.json"
    )

    first = repository.get(
        subject_id=V1_SUBJECT, manifest_id=value["manifest_id"]
    )
    restarted = PrivateHomePlanMaterialManifestRepository(home).get(
        subject_id=V1_SUBJECT, manifest_id=value["manifest_id"]
    )

    assert first.status is restarted.status is (
        PlanMaterialManifestReadStatus.FOUND
    )
    assert first.manifest == restarted.manifest
    assert first.manifest.manifest_id == value["manifest_id"]
    assert (
        first.manifest.manifest_content_hash
        == value["manifest_content_hash"]
    )
    assert all(
        entry.artifact_byte_size is None
        and entry.artifact_byte_size_available is False
        and "artifact_byte_size" not in entry.to_dict()
        for entry in first.manifest.entries
    )
    assert record_path.read_bytes() == raw


def test_p2b1_and_p2b2e_write_v2_actual_byte_sizes(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    resume_manifest = parts["prior"]
    full_manifest = _include(parts).manifest

    assert PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION == (
        PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2
    )
    assert resume_manifest.contract_version == (
        PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2
    )
    assert full_manifest.contract_version == (
        PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION_V2
    )
    assert full_manifest.entries[0] == resume_manifest.entries[0]
    for entry in full_manifest.entries:
        artifact = parts["resume"]["home"].contained_path(
            entry.artifact_reference
        )
        assert entry.artifact_byte_size == len(artifact.read_bytes())
        assert entry.to_dict()["artifact_byte_size"] == artifact.stat().st_size


def test_v2_size_changes_entry_manifest_identity_and_content_hash(
    tmp_path: Path,
) -> None:
    manifest = _setup(tmp_path)["prior"]
    entry = manifest.entries[0]
    changed_content = {
        **entry.content_dict(),
        "artifact_byte_size": entry.artifact_byte_size + 1,
    }

    assert plan_material_entry_id(changed_content) != entry.entry_id
    changed_manifest_id = plan_material_manifest_id(
        contract_version=manifest.contract_version,
        subject_id=manifest.subject_id,
        application_plan_id=manifest.application_plan_id,
        job_id=manifest.job_id,
        job_revision=manifest.job_revision,
        job_content_hash=manifest.job_content_hash,
        prepared_resume_material_id=manifest.prepared_resume_material_id,
        prepared_resume_material_hash=(
            manifest.prepared_resume_material_hash
        ),
        resume_artifact_sha256=manifest.resume_artifact_sha256,
        entry_hashes=(entry.entry_id,),
        artifact_byte_sizes=(entry.artifact_byte_size + 1,),
        assembly_state=PlanMaterialAssemblyState.RESUME_ONLY,
    )
    assert changed_manifest_id != manifest.manifest_id
    changed_manifest_content = manifest.content_dict()
    changed_manifest_content["entries"] = [
        {"entry_id": entry.entry_id, **changed_content}
    ]
    assert plan_material_manifest_content_hash(
        changed_manifest_content
    ) != manifest.manifest_content_hash


@pytest.mark.parametrize("invalid_size", (None, 0, -1, "100", True))
def test_v2_entry_rejects_missing_or_invalid_byte_size(
    tmp_path: Path, invalid_size
) -> None:
    entry = _setup(tmp_path)["prior"].entries[0]
    with pytest.raises((TypeError, ValueError)):
        replace(entry, artifact_byte_size=invalid_size)


def test_persisted_v2_missing_byte_size_fails_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    manifest = parts["prior"]
    path = next(
        parts["resume"]["home"].paths.plan_material_manifests.rglob(
            f"{manifest.manifest_id}.json"
        )
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    value["entries"][0].pop("artifact_byte_size")
    path.write_text(json.dumps(value), encoding="utf-8")

    read = parts["resume"]["manifest_repository"].get(
        subject_id=manifest.subject_id,
        manifest_id=manifest.manifest_id,
    )

    assert read.status is PlanMaterialManifestReadStatus.INTEGRITY_FAILURE


def test_p2b2e_rejects_v1_prior_without_rewriting_it(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    v1 = _v1_projection_from_v2(parts["prior"])
    path = (
        parts["resume"]["home"].paths.plan_material_manifests
        / _subject_key(v1.subject_id)
        / f"{v1.manifest_id}.json"
    )
    raw = (
        json.dumps(
            v1.to_dict(), sort_keys=True, ensure_ascii=False, indent=2
        )
        + "\n"
    ).encode()
    parts["resume"]["home"].write_bytes(path, raw)
    command = IncludeCoverLetterInPlanMaterialManifestCommand(
        subject_id=v1.subject_id,
        application_plan_id=v1.application_plan_id,
        plan_material_manifest_id=v1.manifest_id,
        prepared_cover_letter_material_id=parts["material"].material_id,
        now=NOW,
    )

    result = _include(parts, command=command)

    assert result.status is PlanMaterialManifestStatus.NOT_READY
    assert result.not_ready_reason is (
        PlanMaterialManifestNotReadyReason
        .PLAN_MATERIAL_MANIFEST_VERSION_INCOMPATIBLE
    )
    assert path.read_bytes() == raw


def test_material_bundle_legacy_digest_and_text_are_unchanged(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic resume")
    bundle = MaterialBundle.build(
        resume_path=resume,
        cover_letter="Legacy cover letter text.",
        metadata={"source": "synthetic"},
    )

    assert bundle.cover_letter_pdf is None
    assert bundle.cover_letter == "Legacy cover letter text."
    assert bundle.digest == canonical_hash(
        {
            "resume_sha256": bundle.resume_sha256,
            "cover_letter_sha256": bundle.cover_letter_sha256,
            "metadata": {"source": "synthetic"},
        }
    )


def test_material_and_application_bundle_carry_managed_cover_pdf(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic resume")
    reference = ManagedArtifactReference(
        reference=(
            "state/preparation/compiled-cover-letters/"
            f"subject-{'a' * 64}/cover.pdf"
        ),
        sha256="b" * 64,
        byte_size=123,
        media_type="application/pdf",
    )
    materials = MaterialBundle.build(
        resume_path=resume,
        cover_letter="Legacy text remains separate.",
        cover_letter_pdf=reference,
    )
    application = ApplicationBundle(
        run_id="run-material-contract",
        job=JobSpec(
            url="https://jobs.example.test/roles/1",
            company="Synthetic",
            title="Engineer",
            tier=JobTier.LOW,
        ),
        materials=materials,
        profile={},
        answers={},
        policy=PolicyEngine(
            PolicyConfig(mode=AutonomyMode.SUPERVISED)
        ).decide(JobTier.LOW, RiskSignals()),
    )

    assert application.materials.cover_letter_pdf == reference
    assert application.materials.cover_letter == (
        "Legacy text remains separate."
    )
    assert application.materials.digest != MaterialBundle.build(
        resume_path=resume,
        cover_letter="Legacy text remains separate.",
    ).digest


def test_managed_cover_pdf_reference_is_strictly_typed() -> None:
    with pytest.raises(ValueError):
        ManagedArtifactReference(
            reference="../cover.pdf",
            sha256="a" * 64,
            byte_size=1,
            media_type="application/pdf",
        )
    with pytest.raises(ValueError):
        ManagedArtifactReference(
            reference=(
                "state/preparation/compiled-cover-letters/"
                f"subject-{'a' * 64}/cover.pdf"
            ),
            sha256="a" * 64,
            byte_size=0,
            media_type="application/pdf",
        )


def test_only_shared_p2c2_path_selects_cover_pdf() -> None:
    root = Path(__file__).parents[1]
    leaf_sources = (
        root / "core" / "application_engine.py",
        *(
            path
            for path in (root / "adapters").glob("*.py")
            if path.name
            not in {"document_upload.py", "protocol.py"}
        ),
    )
    assert all(
        "cover_letter_pdf" not in path.read_text(encoding="utf-8")
        for path in leaf_sources
    )
    assert "cover_letter_pdf" in (
        root / "adapters" / "document_upload.py"
    ).read_text(encoding="utf-8")
