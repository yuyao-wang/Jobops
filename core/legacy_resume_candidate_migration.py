"""Project hash-attested legacy resume variants into ResumeCandidates.

This compatibility bridge copies no candidate data into the Git checkout and
makes no claims from resume content.  It accepts only the artifact hash and
routing text already persisted by the private-home migration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .private_home import PrivateHome, PrivateHomeError
from .resume_candidates import (
    MAX_SELECTION_SUMMARY_CHARS,
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
    RegisterResumeCandidateStatus,
    ResumeArtifactType,
    ResumeCandidateRepository,
    ResumeSummarySource,
    ResumeSummaryTrust,
    detect_resume_artifact_type,
    register_resume_candidate,
)


LEGACY_RESUME_CANDIDATE_MIGRATION_CONTRACT_VERSION = (
    "legacy-resume-candidate-migration-v1"
)
MAX_LEGACY_PROFILE_BYTES = 5 * 1024 * 1024
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class LegacyResumeCandidateMigrationFailure(StrEnum):
    PROFILE_UNREADABLE = "PROFILE_UNREADABLE"
    PROFILE_INVALID = "PROFILE_INVALID"
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    VARIANT_INTEGRITY_FAILURE = "VARIANT_INTEGRITY_FAILURE"
    REGISTRATION_FAILED = "REGISTRATION_FAILED"


class LegacyResumeCandidateMigrationError(RuntimeError):
    """Sanitized, typed failure that never carries profile values or paths."""

    def __init__(self, failure: LegacyResumeCandidateMigrationFailure) -> None:
        self.failure = LegacyResumeCandidateMigrationFailure(failure)
        super().__init__(self.failure.value)


@dataclass(frozen=True, slots=True)
class LegacyResumeCandidateMigrationResult:
    discovered_count: int
    eligible_count: int
    created_count: int
    unchanged_count: int
    ignored_unattested_count: int
    contract_version: str = LEGACY_RESUME_CANDIDATE_MIGRATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        counts = (
            self.discovered_count,
            self.eligible_count,
            self.created_count,
            self.unchanged_count,
            self.ignored_unattested_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("migration counts must be non-negative integers")
        if self.eligible_count + self.ignored_unattested_count != self.discovered_count:
            raise ValueError("migration discovery counts are inconsistent")
        if self.created_count + self.unchanged_count != self.eligible_count:
            raise ValueError("migration registration counts are inconsistent")
        if self.contract_version != LEGACY_RESUME_CANDIDATE_MIGRATION_CONTRACT_VERSION:
            raise ValueError("migration contract version is unsupported")

    def safe_dict(self) -> dict[str, int | str]:
        """Return count-only diagnostics safe for logs and UI projections."""

        return {
            "contract_version": self.contract_version,
            "discovered_count": self.discovered_count,
            "eligible_count": self.eligible_count,
            "created_count": self.created_count,
            "unchanged_count": self.unchanged_count,
            "ignored_unattested_count": self.ignored_unattested_count,
        }


@dataclass(frozen=True, slots=True)
class _ValidatedLegacyVariant:
    artifact_path: Path
    artifact_sha256: str
    display_name: str
    selection_safe_summary: str


def _empty_result() -> LegacyResumeCandidateMigrationResult:
    return LegacyResumeCandidateMigrationResult(
        discovered_count=0,
        eligible_count=0,
        created_count=0,
        unchanged_count=0,
        ignored_unattested_count=0,
    )


def _read_legacy_variants(
    home: PrivateHome,
    *,
    subject_id: str,
) -> tuple[int, list[Mapping[str, Any]]]:
    try:
        path = home.contained_path(home.paths.profile_facts)
    except (OSError, PrivateHomeError):
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.PROFILE_INVALID
        ) from None
    if not path.exists():
        return 0, []
    if path.is_symlink() or not path.is_file():
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.PROFILE_INVALID
        )
    try:
        size = path.stat(follow_symlinks=False).st_size
        if size <= 0 or size > MAX_LEGACY_PROFILE_BYTES:
            raise LegacyResumeCandidateMigrationError(
                LegacyResumeCandidateMigrationFailure.PROFILE_INVALID
            )
        profile = json.loads(path.read_text(encoding="utf-8"))
    except LegacyResumeCandidateMigrationError:
        raise
    except OSError:
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.PROFILE_UNREADABLE
        ) from None
    except (UnicodeError, json.JSONDecodeError):
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.PROFILE_INVALID
        ) from None
    if not isinstance(profile, Mapping):
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.PROFILE_INVALID
        )
    recorded_subject = profile.get("subject_id")
    if recorded_subject is not None and (
        not isinstance(recorded_subject, str)
        or not recorded_subject.strip()
        or recorded_subject.strip() != subject_id
    ):
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.SUBJECT_MISMATCH
        )
    normalized = profile.get("normalized")
    if normalized is None:
        return 0, []
    if not isinstance(normalized, Mapping):
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.PROFILE_INVALID
        )
    raw_variants = normalized.get("resume_variants")
    if raw_variants is None:
        return 0, []
    if not isinstance(raw_variants, list):
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.PROFILE_INVALID
        )
    return len(raw_variants), [
        variant for variant in raw_variants if isinstance(variant, Mapping)
    ]


def _text(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        return None
    return cleaned


def _validate_variant(
    home: PrivateHome,
    raw: Mapping[str, Any],
) -> _ValidatedLegacyVariant | None:
    artifact_hash = raw.get("artifact_id")
    artifact_path = _text(raw.get("file_path"), maximum=4096)
    display_name = _text(raw.get("role_family"), maximum=240)
    summary = _text(
        raw.get("use_when"),
        maximum=MAX_SELECTION_SUMMARY_CHARS,
    )
    if (
        not isinstance(artifact_hash, str)
        or _SHA256.fullmatch(artifact_hash) is None
        or artifact_path is None
        or display_name is None
        or summary is None
    ):
        return None
    try:
        path = home.contained_path(artifact_path)
        path.relative_to(home.paths.master_documents)
        if path.is_symlink() or not path.is_file():
            raise ValueError
        content = path.read_bytes()
        detected = detect_resume_artifact_type(content)
        expected = {
            ".pdf": ResumeArtifactType.PDF,
            ".docx": ResumeArtifactType.DOCX,
        }.get(path.suffix.casefold())
        if expected is None or detected is not expected:
            raise ValueError
        if hashlib.sha256(content).hexdigest() != artifact_hash:
            raise ValueError
    except (OSError, PrivateHomeError, TypeError, ValueError):
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.VARIANT_INTEGRITY_FAILURE
        ) from None
    return _ValidatedLegacyVariant(
        artifact_path=path,
        artifact_sha256=artifact_hash,
        display_name=display_name,
        selection_safe_summary=summary,
    )


def migrate_hash_attested_legacy_resume_candidates(
    *,
    home: PrivateHome,
    subject_id: str,
    now: datetime,
    repository: ResumeCandidateRepository | None = None,
) -> LegacyResumeCandidateMigrationResult:
    """Idempotently register trusted projections of legacy master variants.

    All eligible variants are integrity-checked before registration starts, so
    malformed profile input cannot leave a partially migrated candidate set.
    """

    if not isinstance(subject_id, str) or not subject_id.strip():
        raise LegacyResumeCandidateMigrationError(
            LegacyResumeCandidateMigrationFailure.SUBJECT_MISMATCH
        )
    clean_subject = subject_id.strip()
    discovered_count, raw_variants = _read_legacy_variants(
        home,
        subject_id=clean_subject,
    )
    if discovered_count == 0:
        return _empty_result()

    validated: list[_ValidatedLegacyVariant] = []
    for raw in raw_variants:
        variant = _validate_variant(home, raw)
        if variant is not None:
            validated.append(variant)
    ignored_count = discovered_count - len(validated)

    active_repository = repository or PrivateHomeResumeCandidateRepository(home)
    created = 0
    unchanged = 0
    for variant in validated:
        result = register_resume_candidate(
            RegisterResumeCandidateCommand(
                subject_id=clean_subject,
                artifact_path=variant.artifact_path,
                display_name=variant.display_name,
                selection_safe_summary=variant.selection_safe_summary,
                summary_source=ResumeSummarySource.AUTHENTICATED_CALLER,
                summary_trust=ResumeSummaryTrust.USER_CONFIRMED,
                now=now,
                claimed_artifact_sha256=variant.artifact_sha256,
            ),
            home=home,
            repository=active_repository,
        )
        if result.status is RegisterResumeCandidateStatus.CREATED:
            created += 1
        elif result.status is RegisterResumeCandidateStatus.UNCHANGED:
            unchanged += 1
        else:
            raise LegacyResumeCandidateMigrationError(
                LegacyResumeCandidateMigrationFailure.REGISTRATION_FAILED
            )
    return LegacyResumeCandidateMigrationResult(
        discovered_count=discovered_count,
        eligible_count=len(validated),
        created_count=created,
        unchanged_count=unchanged,
        ignored_unattested_count=ignored_count,
    )


__all__ = [
    "LEGACY_RESUME_CANDIDATE_MIGRATION_CONTRACT_VERSION",
    "LegacyResumeCandidateMigrationError",
    "LegacyResumeCandidateMigrationFailure",
    "LegacyResumeCandidateMigrationResult",
    "migrate_hash_attested_legacy_resume_candidates",
]
