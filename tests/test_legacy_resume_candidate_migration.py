from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.legacy_resume_candidate_migration import (
    LEGACY_RESUME_CANDIDATE_MIGRATION_CONTRACT_VERSION,
    LegacyResumeCandidateMigrationError,
    LegacyResumeCandidateMigrationFailure,
    migrate_hash_attested_legacy_resume_candidates,
)
from core.private_home import PrivateHome
from core.resume_candidates import (
    PrivateHomeResumeCandidateRepository,
    ResumeCandidateListStatus,
    ResumeSummarySource,
    ResumeSummaryTrust,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _pdf(home: PrivateHome, name: str, marker: bytes) -> tuple[Path, str]:
    home.ensure()
    path = home.paths.master_documents / name
    content = b"%PDF-1.7\nsynthetic resume " + marker + b"\n%%EOF\n"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _write_profile(
    home: PrivateHome,
    *,
    subject_id: str = "subject-synthetic",
    variants: list[object],
) -> None:
    home.write_bytes(
        home.paths.profile_facts,
        (
            json.dumps(
                {
                    "schema_version": "synthetic-v1",
                    "subject_id": subject_id,
                    "normalized": {"resume_variants": variants},
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _variant(
    path: Path,
    artifact_hash: str,
    *,
    role: str,
    use_when: str,
) -> dict[str, str]:
    return {
        "artifact_id": artifact_hash,
        "file_path": str(path),
        "role_family": role,
        "use_when": use_when,
        "version": "synthetic-v1",
    }


def test_migrates_two_attested_variants_without_inventing_claims(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    first_path, first_hash = _pdf(home, "first.pdf", b"first")
    second_path, second_hash = _pdf(home, "second.pdf", b"second")
    variants = [
        _variant(
            first_path,
            first_hash,
            role="Synthetic Platform",
            use_when="Use for synthetic platform postings.",
        ),
        _variant(
            second_path,
            second_hash,
            role="Synthetic Data",
            use_when="Use for synthetic data postings.",
        ),
    ]
    _write_profile(home, variants=variants)
    repository = PrivateHomeResumeCandidateRepository(home)

    result = migrate_hash_attested_legacy_resume_candidates(
        home=home,
        subject_id="subject-synthetic",
        now=NOW,
        repository=repository,
    )

    assert result.safe_dict() == {
        "contract_version": LEGACY_RESUME_CANDIDATE_MIGRATION_CONTRACT_VERSION,
        "discovered_count": 2,
        "eligible_count": 2,
        "created_count": 2,
        "unchanged_count": 0,
        "ignored_unattested_count": 0,
    }
    listed = repository.list_selectable("subject-synthetic")
    assert listed.status is ResumeCandidateListStatus.SUCCEEDED
    assert {
        (candidate.display_name, candidate.selection_safe_summary)
        for candidate in listed.candidates
    } == {
        ("Synthetic Platform", "Use for synthetic platform postings."),
        ("Synthetic Data", "Use for synthetic data postings."),
    }
    assert all(
        candidate.summary_source is ResumeSummarySource.AUTHENTICATED_CALLER
        and candidate.summary_trust is ResumeSummaryTrust.USER_CONFIRMED
        for candidate in listed.candidates
    )
    assert str(home.root) not in json.dumps(result.safe_dict())


def test_replay_is_idempotent(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private-home")
    path, artifact_hash = _pdf(home, "resume.pdf", b"replay")
    _write_profile(
        home,
        variants=[
            _variant(
                path,
                artifact_hash,
                role="Synthetic General",
                use_when="Use for synthetic general postings.",
            )
        ],
    )
    repository = PrivateHomeResumeCandidateRepository(home)

    first = migrate_hash_attested_legacy_resume_candidates(
        home=home,
        subject_id="subject-synthetic",
        now=NOW,
        repository=repository,
    )
    replay = migrate_hash_attested_legacy_resume_candidates(
        home=home,
        subject_id="subject-synthetic",
        now=datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc),
        repository=repository,
    )

    assert (first.created_count, first.unchanged_count) == (1, 0)
    assert (replay.created_count, replay.unchanged_count) == (0, 1)
    assert len(repository.list_selectable("subject-synthetic").candidates) == 1


def test_integrity_failure_prevalidates_full_batch_before_any_write(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    good_path, good_hash = _pdf(home, "good.pdf", b"good")
    bad_path, _ = _pdf(home, "bad.pdf", b"bad")
    _write_profile(
        home,
        variants=[
            _variant(
                good_path,
                good_hash,
                role="Synthetic Good",
                use_when="Use for synthetic good postings.",
            ),
            _variant(
                bad_path,
                "0" * 64,
                role="Synthetic Bad",
                use_when="Use for synthetic bad postings.",
            ),
        ],
    )
    repository = PrivateHomeResumeCandidateRepository(home)

    with pytest.raises(LegacyResumeCandidateMigrationError) as raised:
        migrate_hash_attested_legacy_resume_candidates(
            home=home,
            subject_id="subject-synthetic",
            now=NOW,
            repository=repository,
        )

    assert raised.value.failure is (
        LegacyResumeCandidateMigrationFailure.VARIANT_INTEGRITY_FAILURE
    )
    assert str(raised.value) == "VARIANT_INTEGRITY_FAILURE"
    assert repository.list_selectable("subject-synthetic").candidates == ()


def test_attested_path_escape_and_subject_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    external = tmp_path / "external.pdf"
    content = b"%PDF-1.7\nsynthetic external\n%%EOF\n"
    external.write_bytes(content)
    _write_profile(
        home,
        variants=[
            _variant(
                external,
                hashlib.sha256(content).hexdigest(),
                role="Synthetic External",
                use_when="Use for synthetic external postings.",
            )
        ],
    )

    with pytest.raises(LegacyResumeCandidateMigrationError) as path_error:
        migrate_hash_attested_legacy_resume_candidates(
            home=home,
            subject_id="subject-synthetic",
            now=NOW,
        )
    assert path_error.value.failure is (
        LegacyResumeCandidateMigrationFailure.VARIANT_INTEGRITY_FAILURE
    )

    _write_profile(
        home,
        subject_id="different-subject",
        variants=[],
    )
    with pytest.raises(LegacyResumeCandidateMigrationError) as subject_error:
        migrate_hash_attested_legacy_resume_candidates(
            home=home,
            subject_id="subject-synthetic",
            now=NOW,
        )
    assert subject_error.value.failure is (
        LegacyResumeCandidateMigrationFailure.SUBJECT_MISMATCH
    )
