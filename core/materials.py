"""Fail-closed loading of tier-specific application materials from Private Home."""

from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from .bundles import (
    JobSpec,
    ManagedArtifactReference,
    MaterialBundle,
    file_sha256,
)
from .event_ledger import hash_job_url
from .latex_compiler import (
    LatexCompileRequest,
    LatexCompileStatus,
    SandboxedPdfLatexCompiler,
)
from .policy import CoverLetterStrategy, JobTier, MaterialStrategy, PolicyDecision
from .prepared_cover_letter_material import (
    escape_cover_letter_latex_text,
    inspect_cover_letter_pdf,
    normalize_cover_letter_text_projection,
)
from .private_home import PrivateHome, PrivateHomeError


MATERIAL_MANIFEST_SCHEMA_VERSION = 1
_SUBJECT_KEY_RE = re.compile(r"^subject-[a-f0-9]{64}$")
_JOB_ID_RE = re.compile(r"^job-[a-f0-9]{24}$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


class MaterialValidationError(RuntimeError):
    """Raised before browser launch when required material attestations are absent."""


@dataclass(frozen=True, slots=True)
class MaterialManifest:
    path: Path
    document: Mapping[str, Any]
    resume_path: Path
    cover_letter_path: Path | None


def _nonempty_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _private_artifact(home: PrivateHome, job_dir: Path, value: Any) -> Path:
    raw = Path(str(value or ""))
    if not str(raw):
        raise MaterialValidationError("material manifest contains an empty artifact path")
    candidate = raw if raw.is_absolute() else job_dir / raw
    try:
        resolved = home.contained_path(candidate)
    except PrivateHomeError as exc:
        raise MaterialValidationError("material artifact must remain inside Private Home") from exc
    try:
        resolved.relative_to(job_dir.resolve())
    except ValueError as exc:
        raise MaterialValidationError(
            "customized material must live in its job-specific generated directory"
        ) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise MaterialValidationError("material artifact is missing or unsafe")
    return resolved


def load_material_manifest(home: PrivateHome, job: JobSpec) -> MaterialManifest:
    job_dir = home.paths.generated_documents / job.job_id
    path = home.contained_path(job_dir / "manifest.json")
    if not path.is_file() or path.is_symlink():
        raise MaterialValidationError(
            f"tier {job.tier.value} requires a private material manifest for {job.job_id}"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaterialValidationError("private material manifest is invalid") from exc
    if not isinstance(document, Mapping):
        raise MaterialValidationError("private material manifest must be an object")
    if int(document.get("schema_version", 0)) != MATERIAL_MANIFEST_SCHEMA_VERSION:
        raise MaterialValidationError("unsupported private material manifest schema")
    if document.get("job_id") != job.job_id or document.get("tier") != job.tier.value:
        raise MaterialValidationError("material manifest is bound to another job or tier")
    if document.get("job_url_hash") != hash_job_url(job.url):
        raise MaterialValidationError("material manifest URL binding does not match this job")

    resume_path = _private_artifact(home, job_dir, document.get("resume_path"))
    if document.get("resume_sha256") != file_sha256(resume_path):
        raise MaterialValidationError("resume content differs from its material attestation")
    cover_path: Path | None = None
    if document.get("cover_letter_path"):
        cover_path = _private_artifact(home, job_dir, document["cover_letter_path"])
        if document.get("cover_letter_sha256") != file_sha256(cover_path):
            raise MaterialValidationError(
                "cover letter content differs from its material attestation"
            )
    return MaterialManifest(path, document, resume_path, cover_path)


def build_tier_materials(
    *,
    home: PrivateHome,
    job: JobSpec,
    policy: PolicyDecision,
    fallback_resume: Path,
) -> MaterialBundle:
    """Load policy-compliant artifacts, blocking before browser use on any gap."""

    if policy.material_strategy is MaterialStrategy.ROUTE_EXISTING:
        return MaterialBundle.build(
            resume_path=fallback_resume,
            metadata={
                "source": "private_csv_queue",
                "tier": job.tier.value,
                "quality_attestation": "route_existing",
            },
        )

    manifest = load_material_manifest(home, job)
    document = manifest.document
    if document.get("facts_verified") is not True:
        raise MaterialValidationError("customized materials lack a verified-facts attestation")
    if document.get("job_specific") is not True:
        raise MaterialValidationError("customized resume is not attested as job-specific")

    qa = document.get("resume_visual_qa")
    if (
        not isinstance(qa, Mapping)
        or qa.get("passed") is not True
        or qa.get("artifact_sha256") != file_sha256(manifest.resume_path)
        or not _nonempty_timestamp(qa.get("checked_at"))
    ):
        raise MaterialValidationError("customized resume has no valid visual-QA attestation")

    if policy.material_strategy is MaterialStrategy.BESPOKE and document.get("bespoke") is not True:
        raise MaterialValidationError("High-tier resume is not attested as bespoke")
    if policy.material_strategy is MaterialStrategy.TARGETED and document.get("targeted") is not True:
        raise MaterialValidationError("Medium-tier resume is not attested as targeted")

    cover_letter = ""
    if policy.cover_letter_strategy in {
        CoverLetterStrategy.NARRATIVE,
        CoverLetterStrategy.TARGETED,
    }:
        if manifest.cover_letter_path is None:
            raise MaterialValidationError("this tier requires a job-specific cover letter")
        cover_letter = manifest.cover_letter_path.read_text(encoding="utf-8").strip()
        if not cover_letter:
            raise MaterialValidationError("job-specific cover letter is empty")
        if document.get("cover_letter_job_specific") is not True:
            raise MaterialValidationError("cover letter is not attested as job-specific")
    if (
        policy.cover_letter_strategy is CoverLetterStrategy.NARRATIVE
        and document.get("narrative_alignment") is not True
    ):
        raise MaterialValidationError(
            "High-tier cover letter lacks a narrative company/role alignment attestation"
        )

    return MaterialBundle.build(
        resume_path=manifest.resume_path,
        cover_letter=cover_letter,
        metadata={
            "source": "private_material_manifest",
            "tier": job.tier.value,
            "manifest_sha256": file_sha256(manifest.path),
            "visual_qa": True,
            "facts_verified": True,
        },
    )


def project_materials_for_legacy_execution(
    *,
    home: PrivateHome,
    subject_id: str,
    job_id: str,
    materials: MaterialBundle,
) -> MaterialBundle:
    """Project a verified legacy resume into the managed execution tree.

    The deterministic upload boundary accepts bytes only from the
    subject-scoped ``state/preparation`` tree.  Legacy material manifests live
    under ``documents/generated``; this one-way, hash-addressed projection
    preserves their verified bytes without widening the uploader's trust root.
    """

    subject = subject_id.strip()
    if not subject or len(subject) > 160:
        raise MaterialValidationError("candidate subject binding is invalid")
    if _JOB_ID_RE.fullmatch(job_id) is None:
        raise MaterialValidationError("job binding is invalid")
    try:
        source = home.contained_path(materials.resume_path)
        if source.is_symlink() or not source.is_file():
            raise MaterialValidationError("verified resume is unavailable")
        content = source.read_bytes()
    except (OSError, PrivateHomeError) as exc:
        raise MaterialValidationError(
            "verified resume must remain inside Private Home"
        ) from exc
    digest = file_sha256(source)
    if digest != materials.resume_sha256 or not content.startswith(b"%PDF-"):
        raise MaterialValidationError("verified resume bytes failed integrity")
    subject_key = "subject-" + hashlib.sha256(
        subject.encode("utf-8")
    ).hexdigest()
    if _SUBJECT_KEY_RE.fullmatch(subject_key) is None:
        raise MaterialValidationError("candidate subject binding is invalid")
    reference = (
        "state/preparation/legacy-execution-materials/"
        f"{subject_key}/{job_id}/resume-{digest}.pdf"
    )
    target = home.contained_path(reference)
    if not home.write_bytes_if_absent(target, content):
        try:
            if target.is_symlink() or target.read_bytes() != content:
                raise MaterialValidationError(
                    "managed resume projection conflicts with existing bytes"
                )
        except OSError as exc:
            raise MaterialValidationError(
                "managed resume projection could not be verified"
            ) from exc
    cover_reference = materials.cover_letter_pdf
    if materials.cover_letter and cover_reference is None:
        plain_cover = _legacy_cover_letter_plain_text(
            materials.cover_letter
        )
        source = _legacy_cover_letter_latex(plain_cover)
        source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        cover_reference_path = (
            "state/preparation/legacy-execution-materials/"
            f"{subject_key}/{job_id}/cover-letter-source-{source_digest}.pdf"
        )
        cover_target = home.contained_path(cover_reference_path)
        try:
            canonical_pdf = (
                cover_target.read_bytes()
                if cover_target.is_file() and not cover_target.is_symlink()
                else None
            )
        except OSError as exc:
            raise MaterialValidationError(
                "managed cover-letter projection could not be read"
            ) from exc
        if canonical_pdf is None:
            outcome = SandboxedPdfLatexCompiler().compile(
                LatexCompileRequest(latex_source=source)
            )
            if (
                outcome.status is not LatexCompileStatus.SUCCEEDED
                or outcome.pdf_bytes is None
            ):
                raise MaterialValidationError(
                    "targeted cover letter could not be compiled safely"
                )
            if home.write_bytes_if_absent(cover_target, outcome.pdf_bytes):
                canonical_pdf = outcome.pdf_bytes
            else:
                try:
                    canonical_pdf = cover_target.read_bytes()
                except OSError as exc:
                    raise MaterialValidationError(
                        "managed cover-letter projection could not be verified"
                    ) from exc
        inspected = inspect_cover_letter_pdf(canonical_pdf)
        if (
            inspected is None
            or not 1 <= inspected[0] <= 2
            or _normalize_legacy_cover_projection(inspected[1])
            != _normalize_legacy_cover_projection(plain_cover)
        ):
            raise MaterialValidationError(
                "targeted cover letter PDF failed text-fidelity validation"
            )
        cover_digest = hashlib.sha256(canonical_pdf).hexdigest()
        cover_reference = ManagedArtifactReference(
            reference=cover_reference_path,
            sha256=cover_digest,
            byte_size=len(canonical_pdf),
            media_type="application/pdf",
        )
    return MaterialBundle.build(
        resume_path=target,
        cover_letter=materials.cover_letter,
        metadata={
            **dict(materials.metadata),
            "execution_projection": "legacy-manifest-to-managed-v1",
            "source_resume_sha256": digest,
        },
        cover_letter_pdf=cover_reference,
    )


def _legacy_cover_letter_plain_text(value: str) -> str:
    lines: list[str] = []
    for raw in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"^\s*#{1,6}\s+", "", raw.strip())
        line = _MARKDOWN_LINK_RE.sub(r"\1 (\2)", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        lines.append(line)
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if lines and normalize_cover_letter_text_projection(lines[0]) == "cover letter":
        lines.pop(0)
        while lines and not lines[0]:
            lines.pop(0)
    plain = "\n".join(lines).strip()
    plain = (
        plain.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u00a0", " ")
    )
    if not plain:
        raise MaterialValidationError("targeted cover letter is empty")
    try:
        plain.encode("ascii")
    except UnicodeEncodeError as exc:
        raise MaterialValidationError(
            "targeted cover letter contains unsupported PDF text"
        ) from exc
    return plain


def _legacy_cover_letter_latex(plain: str) -> str:
    paragraphs = [
        " ".join(item.split())
        for item in re.split(r"\n\s*\n", plain)
        if item.strip()
    ]
    body = "\n\n\\par\n\n".join(
        escape_cover_letter_latex_text(item) for item in paragraphs
    )
    return "\n".join(
        (
            r"\documentclass[11pt,letterpaper]{article}",
            r"\usepackage[T1]{fontenc}",
            r"\pagestyle{empty}",
            r"\setlength{\topmargin}{-0.55in}",
            r"\setlength{\oddsidemargin}{-0.25in}",
            r"\setlength{\evensidemargin}{-0.25in}",
            r"\setlength{\textwidth}{7.0in}",
            r"\setlength{\textheight}{9.6in}",
            r"\setlength{\parindent}{0pt}",
            r"\setlength{\parskip}{0.8em}",
            r"\raggedright",
            r"\begin{document}",
            body,
            r"\end{document}",
            "",
        )
    )


def _normalize_legacy_cover_projection(value: str) -> str:
    punctuation_normalized = (
        value.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    return normalize_cover_letter_text_projection(punctuation_normalized)


__all__ = [
    "MATERIAL_MANIFEST_SCHEMA_VERSION",
    "MaterialManifest",
    "MaterialValidationError",
    "build_tier_materials",
    "load_material_manifest",
    "project_materials_for_legacy_execution",
]
