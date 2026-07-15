#!/usr/bin/env python3
"""Generate an ignored MR.Jobs profile from a private ApplyPilot workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def _name_parts(legal_name: str) -> tuple[str, str]:
    parts = legal_name.strip().split()
    if len(parts) < 2:
        raise ValueError("candidate.legal_name must contain at least two name parts")
    return parts[0], " ".join(parts[1:])


def _resolve_resume(workflow_dir: Path, entry: dict) -> Path:
    path = Path(entry.get("file_path", ""))
    if not path.is_absolute():
        path = workflow_dir / path
    return path.resolve()


def _choose_resume(workflow_dir: Path, resumes: list[dict]) -> Path:
    existing = [(entry, _resolve_resume(workflow_dir, entry)) for entry in resumes]
    existing = [(entry, path) for entry, path in existing if path.is_file()]
    if not existing:
        raise FileNotFoundError("No resume listed in candidate_profile.json exists")
    for entry, path in existing:
        haystack = " ".join(
            str(entry.get(key, "")) for key in ("role_family", "use_when", "file_path")
        ).lower()
        if "general" in haystack:
            return path
    return existing[0][1]


def build_profile(workflow_dir: Path) -> dict:
    source = workflow_dir / "candidate_profile.json"
    data = json.loads(source.read_text())

    candidate = data["candidate"]
    targets = data["targets"]
    current = data["current_status"]
    work_auth = data["work_authorization"]
    compensation = data["compensation"]
    self_id = data.get("voluntary_self_identification", {})
    first_name, last_name = _name_parts(candidate["legal_name"])
    resume_path = _choose_resume(workflow_dir, data.get("resume_files", []))

    sponsorship = bool(
        work_auth.get("requires_sponsorship_now")
        or work_auth.get("requires_sponsorship_in_future")
    )
    prefer_not = not self_id.get("fill_automatically", False)
    roles = list(dict.fromkeys(
        targets.get("primary_role_families", [])
        + targets.get("secondary_role_families", [])
    ))
    locations = targets.get("target_locations", [])

    resume_variants = []
    for entry in data.get("resume_files", []):
        path = _resolve_resume(workflow_dir, entry)
        if path.is_file():
            resume_variants.append({**entry, "file_path": str(path)})

    return {
        "personal": {
            "first_name": first_name,
            "last_name": last_name,
            "email": candidate.get("email", ""),
            "phone": candidate.get("phone", ""),
            "location": candidate.get("current_location", ""),
            "linkedin": candidate.get("linkedin_url", ""),
            "github": candidate.get("github_url", ""),
            "portfolio": candidate.get("portfolio_url", ""),
        },
        "resume_path": str(resume_path),
        "resume_variants": resume_variants,
        "preferences": {
            "roles": roles,
            "keywords": [],
            "min_match_score": 70,
            "remote_only": False,
            "locations": locations,
            "exclude_companies": [],
            "roles_to_avoid": targets.get("roles_to_avoid", []),
        },
        "common_answers": {
            "authorized_to_work": work_auth.get("answer_exactly_as", ""),
            "require_sponsorship": "Yes" if sponsorship else "No",
            "willing_to_relocate": targets.get("relocation_policy", ""),
            "salary_expectation": compensation.get("answer_strategy", ""),
            "earliest_start_date": current.get("available_start_date", ""),
            "gender": "Prefer not to say" if prefer_not else self_id.get("gender", ""),
            "race_ethnicity": "Prefer not to say" if prefer_not else self_id.get("race_ethnicity", ""),
            "veteran_status": "Prefer not to say" if prefer_not else self_id.get("veteran_status", ""),
            "disability_status": "Prefer not to say" if prefer_not else self_id.get("disability_status", ""),
        },
        "search": {
            "enabled": True,
            "queries": roles,
            "locations": locations,
            "distance_miles": 50,
            "results_per_query": 25,
            "salary_min": 0,
        },
        "skills": {"primary": [], "secondary": []},
        "ideal_job_description": current.get("current_role_or_framing", ""),
        "favorite_companies": [],
        "target_boards": {"greenhouse": [], "lever": []},
        "custom_career_pages": [],
        "rate_limits": {
            "max_applications_per_day": 5,
            "min_delay_seconds": 60,
            "max_delay_seconds": 180,
        },
        "schedule": {
            "discover_interval_hours": 6,
            "score_interval_minutes": 30,
            "enabled": False,
        },
        "email": {"enabled": False},
        "ai": {
            "default_backend": "codex_cli",
            "backends": {"codex_cli": {"timeout": 180}},
            "components": {
                "scoring": "codex_cli",
                "cover_letter": "codex_cli",
                "resume_tailoring": "codex_cli",
                "form_analysis": "codex_cli",
                "email_classification": "codex_cli",
                "profile_analysis": "codex_cli",
            },
        },
        "auto_submission": {
            "enabled": True,
            "low_risk_only": True,
            "allow_ai_custom_answers": False,
            "require_explicit_confirmation_evidence": True,
            "stop_for": [
                "CAPTCHA or anti-bot",
                "login or 2FA",
                "unclear legal, identity, work authorization, sponsorship, or compensation question",
                "missing material or failed resume upload",
                "payment or permission prompt",
            ],
        },
        "applypilot": {
            "workflow_dir": str(workflow_dir.resolve()),
            "candidate_profile": str(source.resolve()),
            "application_rules": str((workflow_dir / "application_rules.md").resolve()),
            "answer_bank": str((workflow_dir / "answer_bank.md").resolve()),
            "resume_routing": str((workflow_dir / "resume_routing.md").resolve()),
            "dashboard": str((workflow_dir / "dashboard").resolve()),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow_dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("profile.yaml"))
    args = parser.parse_args()
    profile = build_profile(args.workflow_dir.resolve())
    args.output.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True))
    print(f"Generated private profile: {args.output}")


if __name__ == "__main__":
    main()
