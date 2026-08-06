from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from core.profile_store import CandidateVault
from core.private_home import PrivateHome
from core.policy import PolicyBlocker
from jobctl import _build_application_bundle
from scripts import migrate_private_home as migration
from utils.csv_apply import CSVApplication


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


def test_migrated_sensitive_answers_reach_canonical_bundle_without_losing_sensitivity(
    tmp_path: Path,
) -> None:
    workflow = _synthetic_workflow(tmp_path / "source")
    candidate_path = workflow / "candidate_profile.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["current_status"]["employment_status_answer"] = (
        "Synthetic employed status"
    )
    candidate["education"] = {
        "default_application_graduation_answer": "May 2030"
    }
    candidate["application_accommodations"] = {
        "answer_exactly_as": "No accommodation requested"
    }
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    home = PrivateHome(tmp_path / "private-home")

    migration.migrate(workflow_dir=workflow, private_home=home)
    vault = CandidateVault.load(home)
    records = vault.answers["answers"]
    application = CSVApplication(
        row_index=0,
        row={
            "company": "Synthetic Company",
            "job_title": "Synthetic Test Engineer",
            "job_url": "https://jobs.example.test/openings/answer-taxonomy",
            "priority": "Low",
            "status": "Pending",
            "resume_variant": "synthetic-resume.pdf",
        },
        resume_path=home.paths.master_documents / "synthetic-resume.pdf",
    )

    bundle, profile = _build_application_bundle(
        application=application,
        vault=vault,
        home=home,
        run_id="run-synthetic-answer-taxonomy",
    )

    assert set(bundle.profile) == {"personal"}
    assert bundle.identity_profile.email == (
        profile["personal"]["email"]
    )
    relative_resume = bundle.materials.resume_path.relative_to(home.root)
    assert relative_resume.parts[:2] == ("state", "preparation")
    assert profile["personal"]["email"] not in str(relative_resume)

    assert records["employment_status"]["sensitivity"] == "employment"
    assert records["graduation_date"]["sensitivity"] == "education"
    assert records["accommodation"]["sensitivity"] == "health"
    assert {
        key: bundle.answers[key]
        for key in (
            "employment_status",
            "graduation_date",
            "accommodation",
        )
    } == {
        "employment_status": "Synthetic employed status",
        "graduation_date": "May 2030",
        "accommodation": "No accommodation requested",
    }
    assert set(profile["canonical_answers"]) >= {
        "employment_status",
        "graduation_date",
        "accommodation",
    }

    persisted_answers = json.loads(
        home.paths.verified_answers.read_text(encoding="utf-8")
    )
    persisted_answers["answers"]["future_answer_key"] = {
        **persisted_answers["answers"]["employment_status"],
        "value": "must not reach an application bundle",
    }
    home.write_text(
        home.paths.verified_answers,
        json.dumps(persisted_answers, sort_keys=True),
    )
    blocked_vault = CandidateVault.load(home)

    blocked_bundle, _ = _build_application_bundle(
        application=application,
        vault=blocked_vault,
        home=home,
        run_id="run-synthetic-unknown-answer",
    )

    assert "future_answer_key" not in blocked_bundle.answers.to_dict()
    assert blocked_vault.answer_trust_report().invalid_verified_keys == (
        "future_answer_key",
    )
    assert PolicyBlocker.UNVERIFIED_ANSWERS in blocked_bundle.policy.blockers
