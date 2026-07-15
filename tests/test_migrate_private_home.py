from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from core.private_home import PrivateHome
from scripts import migrate_private_home as migration


def _synthetic_workflow(root: Path) -> Path:
    workflow = root / "workflow"
    workflow.mkdir(parents=True)
    resume = workflow / "synthetic-resume.pdf"
    resume.write_bytes(b"%PDF-1.4\nsynthetic\n")
    candidate = {
        "candidate": {
            "legal_name": "Synthetic Candidate",
            "preferred_name": "Synth",
            "email": "candidate@example.test",
            "phone": "+1 555 0100",
            "current_location": "Test City",
            "linkedin_url": "https://example.test/profile",
            "password": "must-not-migrate",
        },
        "targets": {
            "primary_role_families": ["Test Engineer"],
            "secondary_role_families": [],
            "roles_to_avoid": [],
            "target_locations": ["Test City"],
            "relocation_policy": "No",
        },
        "current_status": {
            "current_role_or_framing": "Test Engineer",
            "available_start_date": "2026-08-01",
        },
        "work_authorization": {
            "requires_sponsorship_now": False,
            "requires_sponsorship_in_future": False,
            "answer_exactly_as": "Yes",
        },
        "compensation": {"answer_strategy": "Discuss with recruiter"},
        "resume_files": [
            {
                "role_family": "General Test",
                "use_when": "General applications",
                "version": "synthetic-v1",
                "file_path": str(resume),
                "source_path": "/private/source/that/must/not/migrate",
                "password": "must-not-migrate",
            }
        ],
        "voluntary_self_identification": {"fill_automatically": False},
        "browser": {"cookies": ["must-not-migrate"]},
        "access_token": "must-not-migrate",
    }
    (workflow / "candidate_profile.json").write_text(
        json.dumps(candidate), encoding="utf-8"
    )
    return workflow


def test_migration_persists_only_allowlisted_profile_projection(tmp_path: Path) -> None:
    workflow = _synthetic_workflow(tmp_path / "source")
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        yaml.safe_dump(
            {
                "personal": {
                    "phone": "+1 555 0199",
                    "password": "must-not-migrate",
                },
                "browser": {
                    "chromium_user_data_dir": str(tmp_path / "escaped"),
                    "storage_state": {"cookies": ["must-not-migrate"]},
                },
                "workday": {"password": "must-not-migrate"},
            }
        ),
        encoding="utf-8",
    )
    home = PrivateHome(tmp_path / "private-home")

    result = migration.migrate(
        workflow_dir=workflow,
        private_home=home,
        legacy_profile_path=legacy,
    )

    facts = json.loads(home.paths.profile_facts.read_text(encoding="utf-8"))
    serialized = json.dumps(facts, sort_keys=True)
    assert "candidate_profile" not in facts
    assert "must-not-migrate" not in serialized
    assert "source_path" not in serialized
    assert facts["normalized"]["personal"]["phone"] == "+1 555 0199"
    assert facts["normalized"]["browser"]["chromium_user_data_dir"] == str(
        home.paths.chromium_profile
    )
    assert result["resume_variants"] == 1


def test_migration_rejects_any_source_inside_git_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "public-repository"
    (repository / ".git").mkdir(parents=True)
    workflow = repository / "private-workflow"
    workflow.mkdir()

    with pytest.raises(ValueError, match="outside a Git worktree"):
        migration.migrate(
            workflow_dir=workflow,
            private_home=PrivateHome(tmp_path / "private-home"),
        )


def test_migration_rejects_symlinked_resume_source(tmp_path: Path) -> None:
    workflow = _synthetic_workflow(tmp_path / "source")
    candidate_path = workflow / "candidate_profile.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    actual = workflow / "synthetic-resume.pdf"
    linked = workflow / "linked-resume.pdf"
    linked.symlink_to(actual)
    candidate["resume_files"][0]["file_path"] = str(linked)
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    with pytest.raises(ValueError, match="resume sources cannot be symlinks"):
        migration.migrate(
            workflow_dir=workflow,
            private_home=PrivateHome(tmp_path / "private-home"),
        )


def test_migration_rolls_back_overwritten_files_after_commit_failure(
    tmp_path: Path, monkeypatch
) -> None:
    workflow = _synthetic_workflow(tmp_path / "source")
    home = PrivateHome(tmp_path / "private-home")
    paths = home.ensure()
    original_facts = b'{"synthetic_old":true}\n'
    home.write_bytes(paths.profile_facts, original_facts)

    real_copy = migration._private_copy
    failed = False

    def fail_once(source: Path, destination: Path) -> None:
        nonlocal failed
        if destination == paths.verified_answers and not failed:
            failed = True
            raise OSError("synthetic commit failure")
        real_copy(source, destination)

    monkeypatch.setattr(migration, "_private_copy", fail_once)

    with pytest.raises(OSError, match="synthetic commit failure"):
        migration.migrate(workflow_dir=workflow, private_home=home)

    assert paths.profile_facts.read_bytes() == original_facts
    assert not paths.verified_answers.exists()


def test_migration_backs_up_every_overwritten_destination(tmp_path: Path) -> None:
    workflow = _synthetic_workflow(tmp_path / "source")
    home = PrivateHome(tmp_path / "private-home")
    migration.migrate(workflow_dir=workflow, private_home=home)
    migration.migrate(workflow_dir=workflow, private_home=home)

    backups = sorted((home.paths.profile / "backups").iterdir())
    assert backups
    latest = backups[-1]
    assert (latest / "profile" / "facts.json").is_file()
    assert (latest / "documents" / "master" / "synthetic-resume.pdf").is_file()
