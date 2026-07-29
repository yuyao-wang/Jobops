"""Focused P2c1b recoverable ApplicationBundle envelope tests."""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application_bundle_assembly import (
    ApplicationBundleAssemblyFailureReason,
    ApplicationBundleAssemblyStatus,
)
from core.bundles import (
    ApplicationBundle,
    application_bundle_canonical_hash,
)
from core.recoverable_application_bundle import (
    PrivateHomeRecoverableApplicationBundleEnvelopeRepository,
    RecoverableApplicationBundleEnvelopeFailureReason,
    RecoverableApplicationBundleEnvelopeReadStatus,
    RecoverableApplicationBundleEnvelopeWriteStatus,
    create_recoverable_application_bundle_envelope,
)

from test_application_bundle_assembly import NOW, SUBJECT_ID, _run, _setup


def _created(tmp_path: Path):
    parts = _setup(tmp_path)
    result = _run(parts)
    assert result.status is ApplicationBundleAssemblyStatus.CREATED
    assert result.record is not None
    assert result.bundle is not None
    return parts, result


def test_p2c1_saves_and_recovers_complete_typed_bundle(
    tmp_path: Path,
) -> None:
    parts, result = _created(tmp_path)

    read = parts["envelope_repository"].get_for_assembly(
        subject_id=SUBJECT_ID,
        assembly_record_id=result.record.record_id,
    )

    assert read.status is RecoverableApplicationBundleEnvelopeReadStatus.FOUND
    assert read.envelope is not None
    recovered = read.envelope.bundle
    assert isinstance(recovered, ApplicationBundle)
    assert recovered.materials.resume_sha256 == (
        result.bundle.materials.resume_sha256
    )
    assert recovered.materials.cover_letter_pdf == (
        result.bundle.materials.cover_letter_pdf
    )
    assert recovered.materials.cover_letter == result.bundle.materials.cover_letter
    assert recovered.answers == result.bundle.answers
    assert recovered.profile == result.bundle.profile
    assert recovered.policy == result.bundle.policy
    assert application_bundle_canonical_hash(recovered) == (
        result.record.application_bundle_canonical_hash
    )


def test_identical_p2c1_replay_keeps_one_envelope_and_original_time(
    tmp_path: Path,
) -> None:
    parts, first = _created(tmp_path)

    replay = _run(parts)
    read = parts["envelope_repository"].get_for_assembly(
        subject_id=SUBJECT_ID,
        assembly_record_id=first.record.record_id,
    )

    assert replay.status is ApplicationBundleAssemblyStatus.UNCHANGED
    assert read.envelope is not None
    assert read.envelope.created_at == first.record.assembled_at
    assert len(
        tuple(
            parts["home"].paths.recoverable_application_bundle_envelopes.rglob(
                "*.json"
            )
        )
    ) == 1


def test_envelope_creation_rejects_subject_and_bundle_hash_mismatch(
    tmp_path: Path,
) -> None:
    parts, result = _created(tmp_path)

    with pytest.raises(ValueError):
        create_recoverable_application_bundle_envelope(
            subject_id="subject-other",
            application_plan_id=result.record.application_plan_id,
            assembly_record=result.record,
            bundle=result.bundle,
            home=parts["home"],
            created_at=NOW,
        )
    bad_record = SimpleNamespace(
        **{
            name: getattr(result.record, name)
            for name in (
                "record_id",
                "record_content_hash",
                "subject_id",
                "application_plan_id",
                "manifest_id",
                "manifest_content_hash",
                "answer_set_id",
                "answer_set_content_hash",
                "resume_entry_id",
                "cover_letter_entry_id",
                "prepared_resume_material_id",
                "prepared_cover_letter_material_id",
                "taxonomy_version",
                "application_bundle_run_id",
            )
        },
        application_bundle_canonical_hash="0" * 64,
    )
    with pytest.raises(ValueError):
        create_recoverable_application_bundle_envelope(
            subject_id=SUBJECT_ID,
            application_plan_id=result.record.application_plan_id,
            assembly_record=bad_record,
            bundle=result.bundle,
            home=parts["home"],
            created_at=NOW,
        )


def test_same_identity_different_snapshot_conflicts_without_overwrite(
    tmp_path: Path,
) -> None:
    parts, result = _created(tmp_path)
    repository = parts["envelope_repository"]
    original = repository.get_for_assembly(
        subject_id=SUBJECT_ID,
        assembly_record_id=result.record.record_id,
    ).envelope
    assert original is not None
    conflicting = create_recoverable_application_bundle_envelope(
        subject_id=SUBJECT_ID,
        application_plan_id=result.record.application_plan_id,
        assembly_record=result.record,
        bundle=result.bundle,
        home=parts["home"],
        created_at=NOW + timedelta(minutes=1),
    )

    write = repository.save(conflicting)
    after = repository.get_for_assembly(
        subject_id=SUBJECT_ID,
        assembly_record_id=result.record.record_id,
    )

    assert write.status is RecoverableApplicationBundleEnvelopeWriteStatus.FAILED
    assert write.reason is (
        RecoverableApplicationBundleEnvelopeFailureReason.IMMUTABLE_CONFLICT
    )
    assert after.envelope == original


def test_corrupt_persisted_envelope_fails_closed(
    tmp_path: Path,
) -> None:
    parts, result = _created(tmp_path)
    path = next(
        parts["home"].paths.recoverable_application_bundle_envelopes.rglob(
            "*.json"
        )
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["bundle_payload"]["profile"]["items"][0][1]["value"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    read = parts["envelope_repository"].get_for_assembly(
        subject_id=SUBJECT_ID,
        assembly_record_id=result.record.record_id,
    )

    assert read.status is RecoverableApplicationBundleEnvelopeReadStatus.FAILED
    assert read.reason is (
        RecoverableApplicationBundleEnvelopeFailureReason.INTEGRITY_FAILURE
    )


def test_restart_recovers_same_envelope_and_bundle_hash(
    tmp_path: Path,
) -> None:
    parts, result = _created(tmp_path)
    restarted = PrivateHomeRecoverableApplicationBundleEnvelopeRepository(
        parts["home"]
    )

    read = restarted.get_for_assembly(
        subject_id=SUBJECT_ID,
        assembly_record_id=result.record.record_id,
    )

    assert read.status is RecoverableApplicationBundleEnvelopeReadStatus.FOUND
    assert read.envelope is not None
    assert read.envelope.bundle_canonical_hash == (
        result.record.application_bundle_canonical_hash
    )
    assert read.envelope.assembly_record_content_hash == (
        result.record.record_content_hash
    )


def test_old_assembly_without_envelope_is_not_found_and_has_no_forbidden_calls(
    tmp_path: Path,
) -> None:
    parts, created = _created(tmp_path)
    path = next(
        parts["home"].paths.recoverable_application_bundle_envelopes.rglob(
            "*.json"
        )
    )
    path.unlink()
    missing = parts["envelope_repository"].get_for_assembly(
        subject_id=SUBJECT_ID,
        assembly_record_id=created.record.record_id,
    )
    replay = _run(parts)
    source_path = (
        Path(__file__).parents[1]
        / "core"
        / "recoverable_application_bundle.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
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

    assert missing.status is (
        RecoverableApplicationBundleEnvelopeReadStatus.NOT_FOUND
    )
    assert replay.status is ApplicationBundleAssemblyStatus.FAILED
    assert replay.failure_reason is (
        ApplicationBundleAssemblyFailureReason
        .BUNDLE_ENVELOPE_PERSISTENCE_FAILED
    )
    assert not path.exists()
    assert not any(
        marker in imported
        for imported in imports
        for marker in (
            "application_answers",
            "plan_material_manifest",
            "application_engine",
            "browser",
            "adapters",
        )
    )
