"""Immutable application inputs and privacy-safe content hashes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .event_ledger import hash_job_url
from .permits import PermitBindings, hash_value
from .policy import JobTier, PolicyDecision


def canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).expanduser().open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalized_job_url(url: str) -> str:
    """Remove fragments while preserving query parameters used as job IDs."""

    parts = urlsplit(str(url).strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("job URL must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("job URL must not contain userinfo")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("job URL contains an invalid port") from exc
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, parts.query, ""))


def stable_job_id(*, url: str, company: str = "", title: str = "") -> str:
    """Identify a posting independently of editable company/title metadata."""

    del company, title  # retained only for source compatibility with MR.Jobs
    return f"job-{hash_job_url(normalized_job_url(url))[:24]}"


@dataclass(frozen=True, slots=True)
class JobSpec:
    url: str
    company: str
    title: str
    tier: JobTier
    job_id: str = ""

    def __post_init__(self) -> None:
        normalized = normalized_job_url(self.url)
        object.__setattr__(self, "url", normalized)
        object.__setattr__(self, "tier", JobTier(self.tier))
        if not self.job_id:
            object.__setattr__(
                self,
                "job_id",
                stable_job_id(url=normalized, company=self.company, title=self.title),
            )


@dataclass(frozen=True, slots=True)
class MaterialBundle:
    resume_path: Path
    resume_sha256: str
    cover_letter: str = ""
    cover_letter_sha256: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        resume_path: str | Path,
        cover_letter: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "MaterialBundle":
        path = Path(resume_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"resume file not found: {path.name}")
        return cls(
            resume_path=path,
            resume_sha256=file_sha256(path),
            cover_letter=cover_letter,
            cover_letter_sha256=hash_value(cover_letter),
            metadata=dict(metadata or {}),
        )

    @property
    def digest(self) -> str:
        return canonical_hash(
            {
                "resume_sha256": self.resume_sha256,
                "cover_letter_sha256": self.cover_letter_sha256,
                "metadata": dict(self.metadata),
            }
        )


@dataclass(frozen=True, slots=True)
class ApplicationBundle:
    run_id: str
    job: JobSpec
    materials: MaterialBundle
    profile: Mapping[str, Any]
    answers: Mapping[str, Any]
    policy: PolicyDecision

    @property
    def answer_hash(self) -> str:
        return canonical_hash(dict(self.answers))

    def permit_bindings(self, *, review_hash: str) -> PermitBindings:
        return PermitBindings(
            run_id=self.run_id,
            job_id=self.job.job_id,
            # Permit checks and the submission ledger must use exactly the
            # same URL identity.  In particular, tracking parameters and a
            # trailing slash cannot make the browser worker disagree with the
            # orchestrator about the active job.
            job_url_hash=hash_job_url(self.job.url),
            material_hash=self.materials.digest,
            answer_hash=self.answer_hash,
            review_hash=review_hash,
            policy_hash=self.policy.policy_hash,
        )

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "job_id": self.job.job_id,
            "company": self.job.company,
            "title": self.job.title,
            "tier": self.job.tier.value,
            "resume_sha256": self.materials.resume_sha256,
            "material_hash": self.materials.digest,
            "answer_hash": self.answer_hash,
            "policy_hash": self.policy.policy_hash,
            "material_strategy": self.policy.material_strategy.value,
            "cover_letter_strategy": self.policy.cover_letter_strategy.value,
        }


def priority_to_tier(priority: str) -> JobTier:
    normalized = str(priority or "").strip().casefold()
    if normalized in {"high", "important", "p0", "p1", "1"}:
        return JobTier.HIGH
    if normalized in {"medium", "normal", "p2", "2"}:
        return JobTier.MEDIUM
    return JobTier.LOW


__all__ = [
    "ApplicationBundle",
    "JobSpec",
    "MaterialBundle",
    "canonical_hash",
    "file_sha256",
    "normalized_job_url",
    "priority_to_tier",
    "stable_job_id",
]
