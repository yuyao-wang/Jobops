from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.private_home import PrivateHome
from core.resume_candidates import (
    RESUME_CANDIDATE_CONTRACT_VERSION,
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
    RegisterResumeCandidateStatus,
    ResumeArtifactType,
    ResumeCandidate,
    ResumeCandidateFailureReason,
    ResumeCandidateListStatus,
    ResumeCandidateReadStatus,
    ResumeCandidateStatus,
    ResumeSummarySource,
    ResumeSummaryTrust,
    register_resume_candidate,
)


NOW = datetime(2026, 7, 28, 16, 0, tzinfo=timezone.utc)


def _home(tmp_path: Path) -> PrivateHome:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    return home


def _pdf(
    home: PrivateHome,
    *,
    name: str = "base.pdf",
    marker: bytes = b"one",
) -> Path:
    path = home.paths.master_documents / name
    path.write_bytes(b"%PDF-1.7\nsynthetic resume " + marker + b"\n%%EOF\n")
    return path


def _command(
    *,
    subject_id: str,
    artifact_path: Path,
    now: datetime = NOW,
    display_name: str = "Synthetic ML Resume",
    summary: str = "Verified skills: Python, geospatial ML, and remote sensing.",
    claimed_hash: str | None = None,
) -> RegisterResumeCandidateCommand:
    return RegisterResumeCandidateCommand(
        subject_id=subject_id,
        artifact_path=artifact_path,
        display_name=display_name,
        selection_safe_summary=summary,
        summary_source=ResumeSummarySource.AUTHENTICATED_CALLER,
        summary_trust=ResumeSummaryTrust.USER_CONFIRMED,
        now=now,
        claimed_artifact_sha256=claimed_hash,
    )


def _register(
    home: PrivateHome,
    *,
    subject_id: str = "subject-a",
    artifact_path: Path | None = None,
    now: datetime = NOW,
    display_name: str = "Synthetic ML Resume",
    summary: str = "Verified skills: Python, geospatial ML, and remote sensing.",
):
    path = artifact_path or _pdf(home)
    repository = PrivateHomeResumeCandidateRepository(home)
    result = register_resume_candidate(
        _command(
            subject_id=subject_id,
            artifact_path=path,
            now=now,
            display_name=display_name,
            summary=summary,
        ),
        home=home,
        repository=repository,
    )
    return repository, result


def test_registers_explicit_managed_artifact_as_typed_candidate(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = _pdf(home)

    repository, result = _register(home, artifact_path=source)

    assert result.status is RegisterResumeCandidateStatus.CREATED
    assert isinstance(result.candidate, ResumeCandidate)
    candidate = result.candidate
    assert candidate.contract_version == RESUME_CANDIDATE_CONTRACT_VERSION
    assert candidate.subject_id == "subject-a"
    assert candidate.artifact_type is ResumeArtifactType.PDF
    assert candidate.status is ResumeCandidateStatus.SELECTABLE
    assert not Path(candidate.artifact_reference).is_absolute()
    managed = home.contained_path(candidate.artifact_reference)
    assert managed.is_file()
    assert managed != source
    assert repository.get(
        subject_id="subject-a",
        resume_id=candidate.resume_id,
    ).candidate == candidate


def test_claimed_hash_is_never_trusted(tmp_path: Path) -> None:
    home = _home(tmp_path)
    source = _pdf(home)
    repository = PrivateHomeResumeCandidateRepository(home)

    result = register_resume_candidate(
        _command(
            subject_id="subject-a",
            artifact_path=source,
            claimed_hash="0" * 64,
        ),
        home=home,
        repository=repository,
    )

    assert result.status is RegisterResumeCandidateStatus.FAILED
    assert (
        result.reason_code
        is ResumeCandidateFailureReason.ARTIFACT_HASH_MISMATCH
    )
    assert repository.list_selectable(
        "subject-a"
    ).candidates == ()


def test_identical_registration_is_unchanged_and_preserves_time(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = _pdf(home)
    repository, first = _register(home, artifact_path=source)

    second = register_resume_candidate(
        _command(
            subject_id="subject-a",
            artifact_path=source,
            now=NOW + timedelta(days=1),
        ),
        home=home,
        repository=repository,
    )

    assert second.status is RegisterResumeCandidateStatus.UNCHANGED
    assert second.candidate == first.candidate
    assert second.candidate is not None
    assert second.candidate.recorded_at == NOW
    assert len(tuple(home.paths.resume_candidate_records.rglob("*.json"))) == 1


def test_subject_ownership_isolated_even_for_same_artifact(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = _pdf(home)
    repository, first = _register(
        home,
        subject_id="subject-a",
        artifact_path=source,
    )
    _, second = _register(
        home,
        subject_id="subject-b",
        artifact_path=source,
    )

    assert first.candidate is not None and second.candidate is not None
    assert first.candidate.resume_id != second.candidate.resume_id
    assert (
        first.candidate.artifact_reference
        != second.candidate.artifact_reference
    )
    assert [item.resume_id for item in repository.list_selectable(
        "subject-a"
    ).candidates] == [first.candidate.resume_id]
    assert [item.resume_id for item in repository.list_selectable(
        "subject-b"
    ).candidates] == [second.candidate.resume_id]
    assert repository.get(
        subject_id="subject-b",
        resume_id=first.candidate.resume_id,
    ).status is ResumeCandidateReadStatus.NOT_FOUND


def test_unregistered_profile_paths_never_enter_provider(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    loose = _pdf(home)
    home.paths.profile_facts.write_text(
        json.dumps(
            {
                "normalized": {
                    "default_resume": str(loose),
                    "resume_variants": [str(loose)],
                }
            }
        ),
        encoding="utf-8",
    )

    result = PrivateHomeResumeCandidateRepository(home).list_selectable(
        "subject-a"
    )

    assert result.status is ResumeCandidateListStatus.SUCCEEDED
    assert result.candidates == ()


def test_summary_or_artifact_change_creates_new_candidate(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = _pdf(home)
    repository, first = _register(home, artifact_path=source)
    changed_summary = register_resume_candidate(
        _command(
            subject_id="subject-a",
            artifact_path=source,
            summary="User-confirmed summary for an environmental ML resume.",
        ),
        home=home,
        repository=repository,
    )
    source.write_bytes(b"%PDF-1.7\nsynthetic resume two\n%%EOF\n")
    changed_artifact = register_resume_candidate(
        _command(subject_id="subject-a", artifact_path=source),
        home=home,
        repository=repository,
    )

    assert first.candidate is not None
    assert changed_summary.status is RegisterResumeCandidateStatus.CREATED
    assert changed_artifact.status is RegisterResumeCandidateStatus.CREATED
    ids = {
        first.candidate.resume_id,
        changed_summary.candidate.resume_id,
        changed_artifact.candidate.resume_id,
    }
    assert len(ids) == 3
    assert len(repository.list_selectable("subject-a").candidates) == 3


def test_unmanaged_external_or_invalid_artifact_fails_closed(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    external = tmp_path / "external.pdf"
    external.write_bytes(b"%PDF-1.7\nexternal\n")
    invalid = home.paths.master_documents / "invalid.pdf"
    invalid.write_bytes(b"not a pdf")
    repository = PrivateHomeResumeCandidateRepository(home)

    unmanaged = register_resume_candidate(
        _command(subject_id="subject-a", artifact_path=external),
        home=home,
        repository=repository,
    )
    malformed = register_resume_candidate(
        _command(subject_id="subject-a", artifact_path=invalid),
        home=home,
        repository=repository,
    )

    assert unmanaged.reason_code is ResumeCandidateFailureReason.ARTIFACT_UNMANAGED
    assert malformed.reason_code is ResumeCandidateFailureReason.ARTIFACT_INVALID
    assert repository.list_selectable("subject-a").candidates == ()


@pytest.mark.parametrize("damage", ["missing", "changed", "record"])
def test_missing_changed_or_corrupt_storage_fails_closed(
    tmp_path: Path,
    damage: str,
) -> None:
    home = _home(tmp_path)
    repository, registered = _register(home)
    candidate = registered.candidate
    assert candidate is not None
    artifact = home.contained_path(candidate.artifact_reference)
    record = next(
        home.paths.resume_candidate_records.rglob(
            f"{candidate.resume_id}.json"
        )
    )
    if damage == "missing":
        artifact.unlink()
    elif damage == "changed":
        artifact.write_bytes(b"%PDF-1.7\nchanged bytes\n")
    else:
        record.write_text("{broken", encoding="utf-8")

    read = repository.get(
        subject_id="subject-a",
        resume_id=candidate.resume_id,
    )
    listed = repository.list_selectable("subject-a")

    assert read.status is ResumeCandidateReadStatus.INTEGRITY_FAILURE
    assert listed.status is ResumeCandidateListStatus.FAILED
    assert listed.candidates == ()


def test_immutable_record_conflict_does_not_overwrite(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = _pdf(home)
    repository, registered = _register(home, artifact_path=source)
    candidate = registered.candidate
    assert candidate is not None
    record = next(
        home.paths.resume_candidate_records.rglob(
            f"{candidate.resume_id}.json"
        )
    )
    conflicting = candidate.to_dict()
    conflicting["display_name"] = "Conflicting content"
    record.write_text(json.dumps(conflicting), encoding="utf-8")
    before = record.read_bytes()

    replay = register_resume_candidate(
        _command(subject_id="subject-a", artifact_path=source),
        home=home,
        repository=repository,
    )

    assert replay.status is RegisterResumeCandidateStatus.FAILED
    assert replay.reason_code is ResumeCandidateFailureReason.INTEGRITY_FAILURE
    assert record.read_bytes() == before


def test_restart_reads_same_candidate_and_stable_order(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source_b = _pdf(home, name="b.pdf", marker=b"b")
    source_a = _pdf(home, name="a.pdf", marker=b"a")
    _, candidate_b = _register(home, artifact_path=source_b)
    _, candidate_a = _register(home, artifact_path=source_a)
    restarted = PrivateHomeResumeCandidateRepository(
        PrivateHome(home.root)
    )

    result = restarted.list_selectable("subject-a")

    assert result.status is ResumeCandidateListStatus.SUCCEEDED
    assert tuple(item.resume_id for item in result.candidates) == tuple(
        sorted(
            (
                candidate_a.candidate.resume_id,
                candidate_b.candidate.resume_id,
            )
        )
    )
    assert restarted.get(
        subject_id="subject-a",
        resume_id=candidate_a.candidate.resume_id,
    ).candidate == candidate_a.candidate


def test_contract_version_is_validated_not_silently_migrated(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    _, registered = _register(home)
    candidate = registered.candidate
    assert candidate is not None
    values = candidate.to_dict()
    values["contract_version"] = "resume-candidate-v2"

    with pytest.raises(ValueError, match="unsupported"):
        ResumeCandidate(
            resume_id=values["resume_id"],
            contract_version=values["contract_version"],
            subject_id=values["subject_id"],
            artifact_reference=values["artifact_reference"],
            artifact_sha256=values["artifact_sha256"],
            artifact_type=ResumeArtifactType(values["artifact_type"]),
            display_name=values["display_name"],
            selection_safe_summary=values["selection_safe_summary"],
            selection_safe_summary_sha256=values[
                "selection_safe_summary_sha256"
            ],
            summary_source=ResumeSummarySource(values["summary_source"]),
            summary_trust=ResumeSummaryTrust(values["summary_trust"]),
            status=ResumeCandidateStatus(values["status"]),
            recorded_at=candidate.recorded_at,
        )


def test_naive_time_and_untrusted_summary_fail_without_registration(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = _pdf(home)
    repository = PrivateHomeResumeCandidateRepository(home)
    naive = register_resume_candidate(
        _command(
            subject_id="subject-a",
            artifact_path=source,
            now=datetime(2026, 7, 28, 16, 0),
        ),
        home=home,
        repository=repository,
    )
    bad_summary = register_resume_candidate(
        RegisterResumeCandidateCommand(
            subject_id="subject-a",
            artifact_path=source,
            display_name="Resume",
            selection_safe_summary="Unverified inferred claims.",
            summary_source="MODEL_INFERRED",  # type: ignore[arg-type]
            summary_trust=ResumeSummaryTrust.VERIFIED,
            now=NOW,
        ),
        home=home,
        repository=repository,
    )

    assert naive.reason_code is ResumeCandidateFailureReason.INVALID_REQUEST
    assert bad_summary.reason_code is ResumeCandidateFailureReason.INVALID_REQUEST
    assert repository.list_selectable("subject-a").candidates == ()


def test_module_has_no_selection_job_model_or_execution_dependencies() -> None:
    source = Path("core/resume_candidates.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = {
        "core.application_plan",
        "core.job_discovery",
        "core.job_prioritization",
        "core.application_engine",
        "core.materials",
        "adapters",
        "browser",
    }
    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
