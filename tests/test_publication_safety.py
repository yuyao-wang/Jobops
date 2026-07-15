"""Regression checks for repository and container publication boundaries."""

from __future__ import annotations

from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = (
    "job-apply",
    "job-materials",
    "job-orchestrate",
    "job-profile",
    "job-status",
)
PRIVATE_BASENAMES = frozenset(
    {
        "profile.yaml",
        "candidate_profile.json",
        "answer_bank.md",
        "application_rules.md",
        "resume_routing.md",
        "job_pool.csv",
        "facts.json",
        "verified-answers.json",
        "policy.json",
    }
)
PRIVATE_DIRECTORY_NAMES = frozenset(
    {
        "private",
        ".private",
        "resumes",
        "documents",
        "queue",
        "state",
        "evidence",
        "browser",
        "browser-data",
        "auth-state",
    }
)
PRIVATE_SUFFIXES = (
    ".pdf",
    ".docx",
    ".pages",
    ".rtf",
    ".har",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite3",
)


def _rules(name: str) -> set[str]:
    return {
        line.strip()
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_docker_context_excludes_private_and_local_runtime_trees() -> None:
    rules = _rules(".dockerignore")

    assert {
        ".venv/",
        "mr-jobs/",
        "private/",
        ".private/",
        "profile.yaml",
        "job_pool.csv",
        "resumes/",
        "documents/",
        "queue/",
        "state/",
        "evidence/",
        "browser/",
        "browser-data/",
        "auth-state/",
        ".env",
        ".env.*",
        "*.db",
        "*.sqlite3",
        "*.pdf",
        "*.har",
        "*storage-state*.json",
        "*storage_state*.json",
        "cookies*.json",
        "*.pem",
        "*.key",
        "candidate_profile.json",
        "answer_bank.md",
        "application_rules.md",
        "resume_routing.md",
    } <= rules


def test_git_excludes_nested_upstream_and_legacy_private_profile() -> None:
    rules = _rules(".gitignore")

    assert "/mr-jobs/" in rules
    assert ".agents/*" in rules
    assert "!.agents/skills/" in rules
    assert "!.agents/skills/**" in rules
    assert ".codex/" in rules
    assert "profile.yaml" in rules
    assert "private/" in rules or "/private/" in rules
    assert "/documents/" in rules
    assert "/evidence/" in rules
    assert "*.pdf" in rules
    assert "*.har" in rules
    assert "*storage-state*.json" in rules
    assert "*storage_state*.json" in rules
    assert "cookies*.json" in rules
    assert "*.pem" in rules
    assert "*.key" in rules
    assert "candidate_profile.json" in rules
    assert "answer_bank.md" in rules
    assert "application_rules.md" in rules
    assert "resume_routing.md" in rules


def test_git_index_contains_no_private_runtime_artifact() -> None:
    """Ignore rules are insufficient once a sensitive file is already tracked."""

    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    tracked = [Path(item) for item in result.stdout.split("\0") if item]
    violations: list[str] = []
    for path in tracked:
        lowered = path.name.casefold()
        if lowered in PRIVATE_BASENAMES:
            violations.append(path.as_posix())
            continue
        if any(part.casefold() in PRIVATE_DIRECTORY_NAMES for part in path.parts):
            violations.append(path.as_posix())
            continue
        if lowered.endswith(PRIVATE_SUFFIXES):
            violations.append(path.as_posix())
            continue
        if lowered.startswith("cookies") and lowered.endswith(".json"):
            violations.append(path.as_posix())
        if "storage-state" in lowered or "storage_state" in lowered:
            violations.append(path.as_posix())

    assert not violations, "private artifacts are tracked: " + ", ".join(violations)


def test_legacy_profile_example_has_no_prefilled_application_answers() -> None:
    profile = yaml.safe_load((ROOT / "profile.yaml.example").read_text(encoding="utf-8"))

    answers = profile.get("common_answers") or {}
    assert answers
    assert all(value in {None, ""} for value in answers.values())


def test_public_skills_use_the_repository_virtual_environment() -> None:
    for name in EXPECTED_SKILLS:
        skill_path = ROOT / ".agents" / "skills" / name / "SKILL.md"
        agent_path = ROOT / ".agents" / "skills" / name / "agents" / "openai.yaml"
        body = skill_path.read_text(encoding="utf-8")

        assert f"name: {name}" in body
        assert ".venv/bin/python jobctl.py" in body
        assert "`python jobctl.py" not in body
        assert agent_path.is_file()
