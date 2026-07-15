#!/usr/bin/env python3
"""Migrate a private ApplyPilot workflow into the repository-external vault.

This command prints counts and paths only.  It never prints candidate values,
document contents, or credentials.  Re-running it is deterministic and makes a
timestamped backup of existing private JSON files before replacing them.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.private_home import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    PrivateHome,
    PrivateHomeError,
    containing_git_worktree,
)
from scripts.import_applypilot import build_profile


SCHEMA_VERSION = 1

PERSONAL_FIELDS = frozenset(
    {
        "first_name",
        "last_name",
        "preferred_name",
        "email",
        "phone",
        "location",
        "linkedin",
        "github",
        "portfolio",
    }
)
PREFERENCE_FIELDS = frozenset(
    {
        "roles",
        "keywords",
        "min_match_score",
        "remote_only",
        "locations",
        "exclude_companies",
        "roles_to_avoid",
    }
)
RESUME_METADATA_FIELDS = frozenset({"role_family", "use_when", "version"})
FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "cookie",
        "cookies",
        "secret",
        "api_key",
        "apikey",
        "recovery_code",
        "mailbox_secret",
        "storage_state",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _safe_filename(path: Path, used: set[str]) -> str:
    stem = "".join(char if char.isalnum() or char in "-_" else "-" for char in path.stem)
    stem = stem.strip("-") or "document"
    suffix = path.suffix.casefold()
    candidate = f"{stem}{suffix}"
    if candidate in used:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
        candidate = f"{stem}-{digest}{suffix}"
    used.add(candidate)
    return candidate


def _private_copy(source: Path, destination: Path) -> None:
    """Atomically copy one regular file without following a leaf symlink."""

    if source.is_symlink() or not source.is_file():
        raise ValueError("migration sources must be regular, non-symlink files")
    if destination.is_symlink():
        raise ValueError("migration destinations cannot be symlinks")
    destination.parent.mkdir(
        parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE
    )
    if destination.parent.is_symlink():
        raise ValueError("migration destination parents cannot be symlinks")
    destination.parent.chmod(PRIVATE_DIRECTORY_MODE)

    source_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
    source_fd = os.open(source, source_flags)
    try:
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", dir=destination.parent
        )
    except BaseException:
        os.close(source_fd)
        raise
    temporary = Path(temp_name)
    try:
        os.fchmod(temp_fd, PRIVATE_FILE_MODE)
        with (
            os.fdopen(source_fd, "rb", closefd=True) as source_handle,
            os.fdopen(temp_fd, "wb", closefd=True) as destination_handle,
        ):
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        os.replace(temporary, destination)
        destination.chmod(PRIVATE_FILE_MODE)
    except BaseException:
        for descriptor in (source_fd, temp_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass
        temporary.unlink(missing_ok=True)
        raise


def _backup_existing(path: Path, backup_dir: Path) -> None:
    if path.is_symlink():
        raise ValueError("existing migration destinations cannot be symlinks")
    if not path.exists():
        return
    if not path.is_file():
        raise ValueError("existing migration destinations must be regular files")
    _private_copy(path, backup_dir)


def _allowlisted_mapping(value: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: copy.deepcopy(value[key])
        for key in sorted(allowed)
        if key in value and value[key] is not None
    }


def _assert_no_secret_keys(value: Any, *, path: str = "root") -> None:
    """Fail closed if a persisted JSON projection grows a credential field."""

    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold().replace("-", "_")
            if (
                key in FORBIDDEN_SECRET_KEYS
                or key.endswith(("_password", "_token", "_secret"))
                or key.startswith("cookie")
            ):
                raise ValueError(f"credential-like field is forbidden at {path}")
            _assert_no_secret_keys(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_secret_keys(item, path=f"{path}[{index}]")


@dataclass(frozen=True)
class _StagedFile:
    source: Path
    destination: Path


def _commit_staged_files(
    private_home: PrivateHome,
    staged_files: list[_StagedFile],
    backup_dir: Path,
) -> None:
    """Commit staged files with complete backups and best-effort rollback."""

    destinations: list[Path] = []
    backups: dict[Path, Path] = {}
    for item in staged_files:
        if item.source.is_symlink() or not item.source.is_file():
            raise ValueError("migration staging contains an unsafe file")
        destination = private_home.contained_path(item.destination)
        if destination in destinations:
            raise ValueError("migration attempted to write a destination twice")
        destinations.append(destination)
        if destination.exists() or destination.is_symlink():
            relative = destination.relative_to(private_home.paths.root)
            backup = backup_dir / relative
            _backup_existing(destination, backup)
            backups[destination] = backup

    installed: list[Path] = []
    try:
        for item, destination in zip(staged_files, destinations, strict=True):
            _private_copy(item.source, destination)
            installed.append(destination)
    except BaseException:
        for destination in reversed(installed):
            backup = backups.get(destination)
            try:
                if backup is not None:
                    _private_copy(backup, destination)
                else:
                    destination.unlink(missing_ok=True)
            except OSError:
                # The original exception is more useful. Backups remain under
                # Private Home for manual recovery if the filesystem failed.
                pass
        raise


def _load_legacy(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return value if isinstance(value, dict) else {}


def _record(value: Any, *, sensitivity: str, source: str, scope: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "value": value,
        "verified": True,
        "source": source,
        "sensitivity": sensitivity,
        "scope": dict(scope or {}),
        "confirmed_at": _utc_now(),
        "expires_at": None,
    }


def _answer_records(
    candidate_data: Mapping[str, Any],
    compatibility_profile: Mapping[str, Any],
    legacy_profile: Mapping[str, Any],
) -> dict[str, Any]:
    common = dict(compatibility_profile.get("common_answers") or {})
    common.update(dict(legacy_profile.get("common_answers") or {}))
    candidate = dict(candidate_data.get("candidate") or {})
    current = dict(candidate_data.get("current_status") or {})
    education = dict(candidate_data.get("education") or {})
    accommodations = dict(candidate_data.get("application_accommodations") or {})

    definitions = {
        "preferred_name": (candidate.get("preferred_name"), "personal"),
        "work_authorization": (common.get("authorized_to_work"), "legal"),
        "sponsorship": (common.get("require_sponsorship"), "legal"),
        "relocation": (common.get("willing_to_relocate"), "personal"),
        "salary": (common.get("salary_expectation"), "compensation"),
        "start_date": (common.get("earliest_start_date"), "personal"),
        "gender": (common.get("gender"), "voluntary_self_id"),
        "race_ethnicity": (common.get("race_ethnicity"), "voluntary_self_id"),
        "veteran_status": (common.get("veteran_status"), "voluntary_self_id"),
        "disability_status": (common.get("disability_status"), "voluntary_self_id"),
        "employment_status": (current.get("employment_status_answer"), "personal"),
        "graduation_date": (education.get("default_application_graduation_answer"), "personal"),
        "accommodation": (accommodations.get("answer_exactly_as"), "health"),
    }
    answers: dict[str, Any] = {}
    for key, (value, sensitivity) in definitions.items():
        if value is not None and str(value).strip():
            answers[key] = _record(
                value,
                sensitivity=sensitivity,
                source="user_confirmed_import",
            )
    return answers


def _copy_resume_variants(
    workflow_dir: Path,
    candidate_data: Mapping[str, Any],
    staging_dir: Path,
    destination_dir: Path,
) -> tuple[list[dict[str, Any]], str, list[_StagedFile]]:
    used: set[str] = set()
    variants: list[dict[str, Any]] = []
    staged_files: list[_StagedFile] = []
    default_resume = ""
    for raw in candidate_data.get("resume_files", []):
        if not isinstance(raw, Mapping):
            continue
        source = Path(str(raw.get("file_path") or "")).expanduser()
        if not source.is_absolute():
            source = workflow_dir / source
        if source.is_symlink():
            raise ValueError("resume sources cannot be symlinks")
        source = source.resolve()
        if containing_git_worktree(source) is not None:
            raise ValueError("resume migration sources must be outside Git")
        if not source.is_file():
            continue
        filename = _safe_filename(source, used)
        staged = staging_dir / filename
        destination = destination_dir / filename
        _private_copy(source, staged)
        entry = {
            "artifact_id": hashlib.sha256(staged.read_bytes()).hexdigest(),
            **_allowlisted_mapping(raw, RESUME_METADATA_FIELDS),
            "file_path": str(destination),
        }
        variants.append(entry)
        staged_files.append(_StagedFile(staged, destination))
        if not default_resume or "general" in " ".join(
            str(raw.get(key) or "") for key in ("role_family", "use_when", "file_path")
        ).casefold():
            default_resume = str(destination)
    if not variants:
        raise FileNotFoundError("no referenced ApplyPilot resume could be copied")
    return variants, default_resume, staged_files


def _copy_material_tree(
    source: Path,
    staging: Path,
    destination: Path,
) -> tuple[list[dict[str, Any]], list[_StagedFile]]:
    """Copy authored material formats while excluding build logs and auxiliaries."""

    allowed_suffixes = {".pdf", ".tex", ".docx", ".md", ".txt"}
    copied: list[dict[str, Any]] = []
    staged_files: list[_StagedFile] = []
    if not source.is_dir():
        return copied, staged_files
    for item in sorted(source.rglob("*")):
        if item.is_symlink() or not item.is_file() or item.suffix.casefold() not in allowed_suffixes:
            continue
        relative = item.relative_to(source)
        staged = staging / relative
        target = destination / relative
        _private_copy(item, staged)
        copied.append(
            {
                "path": str(target),
                "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
                "kind": item.suffix.casefold().lstrip("."),
            }
        )
        staged_files.append(_StagedFile(staged, target))
    return copied, staged_files


def migrate(
    *,
    workflow_dir: Path,
    private_home: PrivateHome,
    legacy_profile_path: Path | None = None,
) -> dict[str, Any]:
    requested_workflow = workflow_dir.expanduser()
    if requested_workflow.is_symlink():
        raise ValueError("migration workflow directory cannot be a symlink")
    workflow_dir = requested_workflow.resolve()
    if containing_git_worktree(workflow_dir) is not None:
        raise ValueError(
            "migration source must be outside a Git worktree; move the private "
            "workflow to a repository-external directory first"
        )
    candidate_source = workflow_dir / "candidate_profile.json"
    if candidate_source.is_symlink() or not candidate_source.is_file():
        raise ValueError(
            "candidate_profile.json must be a regular, non-symlink file"
        )
    candidate_data = json.loads(candidate_source.read_text(encoding="utf-8"))
    if not isinstance(candidate_data, dict):
        raise ValueError("candidate_profile.json must contain an object")

    if legacy_profile_path is not None:
        requested_legacy = legacy_profile_path.expanduser()
        if requested_legacy.is_symlink():
            raise ValueError("legacy profile cannot be a symlink")
        resolved_legacy = requested_legacy.resolve()
        if containing_git_worktree(resolved_legacy) is not None:
            raise ValueError("legacy profile migration source must be outside Git")
        legacy_profile_path = resolved_legacy

    paths = private_home.ensure()
    compatibility = build_profile(workflow_dir)
    legacy = _load_legacy(legacy_profile_path)
    staging_root = Path(
        tempfile.mkdtemp(prefix=".migration-staging-", dir=paths.root)
    )
    staging_root.chmod(PRIVATE_DIRECTORY_MODE)
    try:
        staged_files: list[_StagedFile] = []
        variants, default_resume, resume_files = _copy_resume_variants(
            workflow_dir,
            candidate_data,
            staging_root / "documents" / "master",
            paths.master_documents,
        )
        staged_files.extend(resume_files)

        authored_materials: list[dict[str, Any]] = []
        material_specs = (
            (
                workflow_dir / "resumes" / "source",
                staging_root / "documents" / "master" / "source",
                paths.master_documents / "source",
            ),
            (
                workflow_dir / "cover_letters" / "pdf",
                staging_root / "documents" / "generated" / "cover_letters" / "pdf",
                paths.generated_documents / "cover_letters" / "pdf",
            ),
            (
                workflow_dir / "cover_letters" / "source",
                staging_root / "documents" / "generated" / "cover_letters" / "source",
                paths.generated_documents / "cover_letters" / "source",
            ),
        )
        for source, staged, destination in material_specs:
            records, files = _copy_material_tree(source, staged, destination)
            authored_materials.extend(records)
            staged_files.extend(files)

        personal = _allowlisted_mapping(
            compatibility.get("personal"), PERSONAL_FIELDS
        )
        legacy_personal = _allowlisted_mapping(
            legacy.get("personal"), PERSONAL_FIELDS
        )
        personal.update(
            {
                key: value
                for key, value in legacy_personal.items()
                if str(value).strip()
            }
        )
        candidate_section = candidate_data.get("candidate")
        preferred = (
            candidate_section.get("preferred_name")
            if isinstance(candidate_section, Mapping)
            else None
        )
        if preferred is not None and str(preferred).strip():
            personal["preferred_name"] = preferred

        normalized = {
            "personal": personal,
            "default_resume": default_resume,
            "resume_variants": variants,
            "preferences": _allowlisted_mapping(
                compatibility.get("preferences"), PREFERENCE_FIELDS
            ),
            # Browser state is always rooted by CandidateVault/PrivateHome.
            # No legacy path, cookie, or storage-state field is imported.
            "browser": {
                "preferred_handoff_browser": "safari",
                "user_data_dir": str(paths.chromium_profile),
                "chromium_user_data_dir": str(paths.chromium_profile),
            },
            # Only policy booleans are migrated; credentials remain Keychain-only.
            "workday": {"auto_login": True, "auto_register": True},
            "ai": {
                "default_backend": "codex_cli",
                "backends": {"codex_cli": {"timeout": 180}},
                "components": {"form_analysis": "codex_cli"},
            },
        }
        facts = {
            "schema_version": SCHEMA_VERSION,
            "imported_at": _utc_now(),
            "source": "applypilot_allowlisted",
            "normalized": normalized,
        }
        answers = {
            "schema_version": SCHEMA_VERSION,
            "imported_at": _utc_now(),
            "answers": _answer_records(candidate_data, compatibility, legacy),
        }
        policy = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _utc_now(),
            "autonomy": {
                "mode": "LOW_RISK_AUTOPILOT",
                "allow_keychain_login": True,
                "allow_account_registration": True,
                "email_verification_agent_enabled": False,
            },
            "mailbox": {
                "enabled": False,
                "provider": "imap",
                "host": "",
                "port": 993,
                "mailbox": "INBOX",
                "keychain_service": "com.jobops.mailbox.imap",
            },
            "tiers": {
                "HIGH": {
                    "materials": "BESPOKE",
                    "cover_letter": "NARRATIVE",
                    "gate_a": "HUMAN",
                    "gate_b": "HUMAN",
                },
                "MEDIUM": {
                    "materials": "TARGETED",
                    "cover_letter": "TARGETED",
                    "gate_a": "CODEX",
                    "gate_b": "HUMAN",
                },
                "LOW": {
                    "materials": "ROUTE_EXISTING",
                    "cover_letter": "IF_REQUIRED",
                    "gate_a": "CODEX",
                    "gate_b": "CODEX",
                },
            },
            "hard_stops": [
                "CAPTCHA",
                "MFA",
                "EMAIL_VERIFICATION",
                "ACCOUNT_LOCKED",
                "UNKNOWN_SENSITIVE_REQUIRED_QUESTION",
            ],
            "extensions": {"outreach": False, "follow_up": False},
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _utc_now(),
            "resume_variants": variants,
            "authored_materials": authored_materials,
        }
        for projection in (facts, answers, policy, manifest):
            _assert_no_secret_keys(projection)

        json_destinations = (
            ("facts.json", facts, paths.profile_facts),
            ("verified-answers.json", answers, paths.verified_answers),
            ("policy.json", policy, paths.policy),
            ("documents-manifest.json", manifest, paths.documents / "manifest.json"),
        )
        for name, value, destination in json_destinations:
            staged = staging_root / "projections" / name
            private_home.write_bytes(staged, _json_bytes(value))
            staged_files.append(_StagedFile(staged, destination))

        queue_source = workflow_dir / "dashboard" / "job_pool.csv"
        queue_imported = False
        if queue_source.is_symlink():
            raise ValueError("queue migration source cannot be a symlink")
        if queue_source.is_file():
            staged_queue = staging_root / "queue" / "job_pool.csv"
            _private_copy(queue_source, staged_queue)
            staged_files.append(_StagedFile(staged_queue, paths.job_queue))
            queue_imported = True

        copied_references = 0
        for name in ("answer_bank.md", "application_rules.md", "resume_routing.md"):
            source = workflow_dir / name
            if source.is_symlink():
                raise ValueError("reference migration sources cannot be symlinks")
            if source.is_file():
                staged = staging_root / "references" / name
                _private_copy(source, staged)
                staged_files.append(
                    _StagedFile(staged, paths.profile / "source" / name)
                )
                copied_references += 1

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_dir = paths.profile / "backups" / f"{stamp}-{uuid4().hex[:8]}"
        _commit_staged_files(private_home, staged_files, backup_dir)

        return {
            "private_home": str(paths.root),
            "resume_variants": len(variants),
            "verified_answers": len(answers["answers"]),
            "authored_materials": len(authored_materials),
            "reference_files": copied_references,
            "queue_imported": queue_imported,
        }
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_dir", type=Path)
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--legacy-profile", type=Path, default=None)
    args = parser.parse_args()
    home = PrivateHome(args.home.expanduser().resolve()) if args.home else PrivateHome.discover()
    result = migrate(
        workflow_dir=args.workflow_dir,
        private_home=home,
        legacy_profile_path=args.legacy_profile,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
