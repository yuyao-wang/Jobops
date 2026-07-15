from __future__ import annotations

import json
from pathlib import Path

from core.policy import AutonomyMode
from core.private_home import PrivateHome
from core.profile_store import CandidateVault


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _record(value, *, verified: bool = True, scope: dict | None = None, expires_at=None):
    return {
        "value": value,
        "verified": verified,
        "source": "synthetic_user_confirmation",
        "sensitivity": "legal",
        "scope": scope or {},
        "confirmed_at": "2026-01-01T00:00:00Z",
        "expires_at": expires_at,
    }


def test_private_vault_projects_only_verified_answers(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    paths = home.ensure()
    _write(
        paths.profile_facts,
        {
            "schema_version": 1,
            "normalized": {
                "personal": {
                    "first_name": "Synthetic",
                    "last_name": "Candidate",
                    "email": "candidate@example.test",
                    "preferred_name": "Test",
                },
                "default_resume": str(paths.master_documents / "resume.pdf"),
                "browser": {
                    "user_data_dir": str(tmp_path / "escaped-browser"),
                    "chromium_user_data_dir": str(tmp_path / "escaped-chromium"),
                },
                "workday": {},
            },
        },
    )
    _write(
        paths.verified_answers,
        {
            "schema_version": 1,
            "answers": {
                "sponsorship": _record("No"),
                "salary": _record("invented", verified=False),
            },
        },
    )
    _write(
        paths.policy,
        {
            "schema_version": 1,
            "autonomy": {"mode": "LOW_RISK_AUTOPILOT"},
        },
    )

    vault = CandidateVault.load(home)
    profile = vault.application_profile()

    assert profile["common_answers"] == {"require_sponsorship": "No"}
    assert "salary" not in profile["canonical_answers"]
    assert profile["browser"]["user_data_dir"] == str(paths.chromium_profile)
    assert profile["browser"]["chromium_user_data_dir"] == str(
        paths.chromium_profile
    )
    assert vault.policy_config.mode is AutonomyMode.LOW_RISK_AUTOPILOT


def test_verified_answer_requires_provenance_scope_and_unexpired_confirmation(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    paths = home.ensure()
    _write(
        paths.profile_facts,
        {
            "schema_version": 1,
            "normalized": {
                "personal": {
                    "first_name": "Synthetic",
                    "last_name": "Candidate",
                    "email": "candidate@example.test",
                }
            },
        },
    )
    _write(
        paths.verified_answers,
        {
            "schema_version": 1,
            "answers": {
                "global": _record("Yes"),
                "scoped": _record("No", scope={"job_id": "job-allowed"}),
                "expired": _record("No", expires_at="2025-01-01T00:00:00Z"),
                "missing_source": {
                    "value": "No",
                    "verified": True,
                    "sensitivity": "legal",
                    "scope": {},
                    "confirmed_at": "2026-01-01T00:00:00Z",
                    "expires_at": None,
                },
            },
        },
    )
    _write(paths.policy, {"schema_version": 1, "autonomy": {}})

    vault = CandidateVault.load(home)
    report = vault.answer_trust_report(job_id="job-other")

    assert report.values == {"global": "Yes"}
    assert report.invalid_verified_keys == ("expired", "missing_source")
    assert report.all_projected_answers_verified is False
    assert vault.answer_trust_report(job_id="job-allowed").values["scoped"] == "No"


def test_private_vault_contains_no_credentials_field(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    paths = home.ensure()
    _write(
        paths.profile_facts,
        {
            "schema_version": 1,
            "normalized": {
                "personal": {
                    "first_name": "Synthetic",
                    "last_name": "Candidate",
                    "email": "candidate@example.test",
                }
            },
        },
    )
    _write(paths.verified_answers, {"schema_version": 1, "answers": {}})
    _write(paths.policy, {"schema_version": 1, "autonomy": {}})

    profile = CandidateVault.load(home).application_profile()

    assert "password" not in json.dumps(profile).casefold()
    assert "credentials" not in profile


def test_workday_auth_autonomy_cannot_exceed_private_policy(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    paths = home.ensure()
    _write(
        paths.profile_facts,
        {
            "schema_version": 1,
            "normalized": {
                "personal": {
                    "first_name": "Synthetic",
                    "last_name": "Candidate",
                    "email": "candidate@example.test",
                },
                "workday": {
                    "auto_login": True,
                    "auto_register": True,
                },
            },
        },
    )
    _write(paths.verified_answers, {"schema_version": 1, "answers": {}})
    _write(
        paths.policy,
        {
            "schema_version": 1,
            "autonomy": {
                "allow_keychain_login": False,
                "allow_account_registration": False,
            },
        },
    )

    profile = CandidateVault.load(home).application_profile()

    assert profile["workday"]["auto_login"] is False
    assert profile["workday"]["auto_register"] is False
