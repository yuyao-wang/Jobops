"""Immutable application inputs and privacy-safe content hashes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .event_ledger import hash_job_url
from .application_answer_taxonomy import CanonicalApplicationAnswers
from .permits import PermitBindings, hash_value
from .policy import JobTier, PolicyDecision


_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_SUBJECT_KEY_RE = re.compile(r"^subject-[a-f0-9]{64}$")
APPLICATION_BUNDLE_CONTRACT_VERSION = "application-bundle-v1"


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
class ManagedArtifactReference:
    """Value-only reference to one subject-isolated managed artifact."""

    reference: str
    sha256: str
    byte_size: int
    media_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not self.reference.strip():
            raise ValueError("managed artifact reference is required")
        reference = self.reference.strip()
        path = PurePosixPath(reference)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.parts[:2] != ("state", "preparation")
            or not any(
                _SUBJECT_KEY_RE.fullmatch(part) for part in path.parts
            )
        ):
            raise ValueError(
                "managed artifact reference must be subject-isolated"
            )
        if (
            not isinstance(self.sha256, str)
            or _SHA256_RE.fullmatch(self.sha256) is None
        ):
            raise ValueError("managed artifact hash must be SHA-256")
        if type(self.byte_size) is not int or self.byte_size < 1:
            raise ValueError("managed artifact byte size must be positive")
        if self.media_type != "application/pdf":
            raise ValueError("managed Cover Letter artifact must be a PDF")
        object.__setattr__(self, "reference", reference)

    def to_dict(self) -> dict[str, Any]:
        return {
            "byte_size": self.byte_size,
            "media_type": self.media_type,
            "reference": self.reference,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class MaterialBundle:
    resume_path: Path
    resume_sha256: str
    cover_letter: str = ""
    cover_letter_sha256: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    cover_letter_pdf: ManagedArtifactReference | None = None

    def __post_init__(self) -> None:
        if self.cover_letter_pdf is not None and not isinstance(
            self.cover_letter_pdf, ManagedArtifactReference
        ):
            raise TypeError(
                "cover_letter_pdf must be a ManagedArtifactReference"
            )

    @classmethod
    def build(
        cls,
        *,
        resume_path: str | Path,
        cover_letter: str = "",
        metadata: Mapping[str, Any] | None = None,
        cover_letter_pdf: ManagedArtifactReference | None = None,
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
            cover_letter_pdf=cover_letter_pdf,
        )

    @property
    def digest(self) -> str:
        content = {
            "resume_sha256": self.resume_sha256,
            "cover_letter_sha256": self.cover_letter_sha256,
            "metadata": dict(self.metadata),
        }
        if self.cover_letter_pdf is not None:
            content["cover_letter_pdf"] = self.cover_letter_pdf.to_dict()
        return canonical_hash(content)


@dataclass(frozen=True, slots=True)
class ApplicationBundle:
    run_id: str
    job: JobSpec
    materials: MaterialBundle
    profile: Mapping[str, Any]
    answers: CanonicalApplicationAnswers
    policy: PolicyDecision

    def __post_init__(self) -> None:
        if not isinstance(self.answers, CanonicalApplicationAnswers):
            object.__setattr__(
                self,
                "answers",
                CanonicalApplicationAnswers.from_mapping(self.answers),
            )

    @property
    def answer_hash(self) -> str:
        return canonical_hash(self.answers.to_dict())

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


def application_bundle_canonical_hash(bundle: ApplicationBundle) -> str:
    """Stable identity of the complete execution-relevant bundle contract."""

    if not isinstance(bundle, ApplicationBundle):
        raise TypeError("bundle must be an ApplicationBundle")
    return canonical_hash(
        {
            "answers": bundle.answers.to_dict(),
            "application_bundle_contract_version": (
                APPLICATION_BUNDLE_CONTRACT_VERSION
            ),
            "job": {
                "company": bundle.job.company,
                "job_id": bundle.job.job_id,
                "tier": bundle.job.tier.value,
                "title": bundle.job.title,
                "url": bundle.job.url,
            },
            "materials": bundle.materials.digest,
            "policy_hash": bundle.policy.policy_hash,
            "profile_hash": canonical_hash(dict(bundle.profile)),
            "run_id": bundle.run_id,
            "taxonomy_version": bundle.answers.taxonomy_version,
        }
    )


def priority_to_tier(priority: str) -> JobTier:
    normalized = str(priority or "").strip().casefold()
    if normalized in {"high", "important", "p0", "p1", "1"}:
        return JobTier.HIGH
    if normalized in {"medium", "normal", "p2", "2"}:
        return JobTier.MEDIUM
    return JobTier.LOW


__all__ = [
    "APPLICATION_BUNDLE_CONTRACT_VERSION",
    "ApplicationBundle",
    "JobSpec",
    "ManagedArtifactReference",
    "MaterialBundle",
    "application_bundle_canonical_hash",
    "canonical_hash",
    "file_sha256",
    "normalized_job_url",
    "priority_to_tier",
    "stable_job_id",
]
