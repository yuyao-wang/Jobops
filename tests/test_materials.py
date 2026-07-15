from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.bundles import JobSpec, file_sha256
from core.materials import MaterialValidationError, build_tier_materials
from core.event_ledger import hash_job_url
from core.policy import AutonomyMode, JobTier, PolicyConfig, PolicyEngine, RiskSignals
from core.private_home import PrivateHome


def _policy(tier: JobTier):
    return PolicyEngine(PolicyConfig(mode=AutonomyMode.LOW_RISK_AUTOPILOT)).decide(
        tier, RiskSignals()
    )


def _manifest(home: PrivateHome, job: JobSpec, *, narrative: bool = True) -> Path:
    directory = home.paths.generated_documents / job.job_id
    directory.mkdir(parents=True)
    resume = directory / "resume.pdf"
    resume.write_bytes(b"synthetic customized resume")
    cover = directory / "cover-letter.md"
    cover.write_text("A true, synthetic narrative about mission alignment.", encoding="utf-8")
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "job_id": job.job_id,
                "job_url_hash": hash_job_url(job.url),
                "tier": job.tier.value,
                "resume_path": resume.name,
                "resume_sha256": file_sha256(resume),
                "cover_letter_path": cover.name,
                "cover_letter_sha256": file_sha256(cover),
                "facts_verified": True,
                "job_specific": True,
                "bespoke": job.tier is JobTier.HIGH,
                "targeted": job.tier is JobTier.MEDIUM,
                "cover_letter_job_specific": True,
                "narrative_alignment": narrative,
                "resume_visual_qa": {
                    "passed": True,
                    "artifact_sha256": file_sha256(resume),
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_high_tier_requires_private_job_specific_manifest(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    home.ensure()
    fallback = home.paths.master_documents / "fallback.pdf"
    fallback.write_bytes(b"synthetic")
    job = JobSpec(
        url="https://boards.example.test/jobs/1",
        company="Synthetic Co",
        title="Engineer",
        tier=JobTier.HIGH,
    )

    with pytest.raises(MaterialValidationError, match="manifest"):
        build_tier_materials(
            home=home,
            job=job,
            policy=_policy(JobTier.HIGH),
            fallback_resume=fallback,
        )


def test_high_tier_binds_visual_qa_and_narrative_cover_letter(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    home.ensure()
    fallback = home.paths.master_documents / "fallback.pdf"
    fallback.write_bytes(b"synthetic")
    job = JobSpec(
        url="https://boards.example.test/jobs/1",
        company="Synthetic Co",
        title="Engineer",
        tier=JobTier.HIGH,
    )
    _manifest(home, job)

    materials = build_tier_materials(
        home=home,
        job=job,
        policy=_policy(JobTier.HIGH),
        fallback_resume=fallback,
    )

    assert materials.resume_path.parent.name == job.job_id
    assert "mission alignment" in materials.cover_letter
    assert materials.metadata["visual_qa"] is True


def test_high_tier_rejects_missing_narrative_attestation(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    home.ensure()
    fallback = home.paths.master_documents / "fallback.pdf"
    fallback.write_bytes(b"synthetic")
    job = JobSpec(
        url="https://boards.example.test/jobs/1",
        company="Synthetic Co",
        title="Engineer",
        tier=JobTier.HIGH,
    )
    _manifest(home, job, narrative=False)

    with pytest.raises(MaterialValidationError, match="narrative"):
        build_tier_materials(
            home=home,
            job=job,
            policy=_policy(JobTier.HIGH),
            fallback_resume=fallback,
        )


def test_low_tier_routes_existing_resume_without_manifest(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    home.ensure()
    fallback = home.paths.master_documents / "fallback.pdf"
    fallback.write_bytes(b"synthetic")
    job = JobSpec(
        url="https://boards.example.test/jobs/1",
        company="Synthetic Co",
        title="Engineer",
        tier=JobTier.LOW,
    )

    materials = build_tier_materials(
        home=home,
        job=job,
        policy=_policy(JobTier.LOW),
        fallback_resume=fallback,
    )

    assert materials.resume_path == fallback.resolve()
    assert not materials.cover_letter
