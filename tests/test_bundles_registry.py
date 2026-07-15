from __future__ import annotations

from pathlib import Path

import pytest

from adapters.registry import AdapterRegistry
from core.bundles import (
    ApplicationBundle,
    JobSpec,
    MaterialBundle,
    priority_to_tier,
    stable_job_id,
)
from core.event_ledger import hash_job_url
from core.policy import AutonomyMode, JobTier, PolicyConfig, PolicyEngine, RiskSignals


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://jobs.lever.co/acme/1", "lever"),
        ("https://jobs.ashbyhq.com/acme/1", "ashby"),
        ("https://jobs.jobvite.com/acme/job/1", "jobvite"),
        ("https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/1", "workday"),
        ("https://careers.example.test/jobs/1", "generic_ai"),
    ],
)
def test_registry_routes_without_model(url: str, expected: str) -> None:
    assert AdapterRegistry.route_name(url) == expected


@pytest.mark.parametrize(
    ("url", "hint"),
    [
        ("https://workday.evil.example/jobs/1", "workday"),
        ("https://greenhouse.evil.example/jobs/1", "greenhouse"),
        ("https://careers.example.test/jobs/1", "lever"),
    ],
)
def test_untrusted_platform_hint_cannot_override_hostname(url: str, hint: str) -> None:
    assert AdapterRegistry.route_name(url, hint) == "generic_ai"


def test_stable_job_id_ignores_fragment() -> None:
    first = stable_job_id(url="https://example.test/job?id=1#top", company="A", title="B")
    second = stable_job_id(url="https://example.test/job?id=1#apply", company="A", title="B")
    assert first == second


def test_stable_job_id_ignores_tracking_company_and_title_variants() -> None:
    first = stable_job_id(
        url="https://www.example.test/jobs/42?utm_source=mail&ref=friend",
        company="Old Company Label",
        title="Old Title",
    )
    second = stable_job_id(
        url="https://example.test/jobs/42?source=career-page",
        company="Renamed Company",
        title="Renamed Role",
    )
    assert first == second


def test_stable_job_id_uses_canonical_url_identity() -> None:
    first = stable_job_id(
        url="https://example.test/job/?id=1&utm_source=mail", company="A", title="B"
    )
    second = stable_job_id(
        url="https://EXAMPLE.test/job?id=1&utm_campaign=queue", company="A", title="B"
    )
    assert first == second


def test_material_bundle_hashes_content_not_filename(tmp_path: Path) -> None:
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(b"synthetic resume")
    second.write_bytes(b"synthetic resume")
    assert MaterialBundle.build(resume_path=first).digest == MaterialBundle.build(
        resume_path=second
    ).digest


def test_permit_and_ledger_share_one_canonical_job_url_hash(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic resume")
    job = JobSpec(
        url="https://careers.example.test/jobs/1/?utm_source=mail&ref=queue#apply",
        company="Example",
        title="Engineer",
        tier=JobTier.LOW,
    )
    bundle = ApplicationBundle(
        run_id="run-url-identity",
        job=job,
        materials=MaterialBundle.build(resume_path=resume),
        profile={},
        answers={},
        policy=PolicyEngine(
            PolicyConfig(mode=AutonomyMode.LOW_RISK_AUTOPILOT)
        ).decide(JobTier.LOW, RiskSignals()),
    )

    assert (
        bundle.permit_bindings(review_hash="review").job_url_hash
        == hash_job_url(job.url)
        == hash_job_url("https://careers.example.test/jobs/1?ref=queue")
    )


def test_priority_treatment() -> None:
    assert priority_to_tier("High") is JobTier.HIGH
    assert priority_to_tier("Medium") is JobTier.MEDIUM
    assert priority_to_tier("Low") is JobTier.LOW


def test_job_spec_rejects_non_http_url() -> None:
    with pytest.raises(ValueError):
        JobSpec(url="file:///tmp/job", company="A", title="B", tier=JobTier.LOW)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.test/jobs/1",
        "https://example.test:99999/jobs/1",
    ],
)
def test_job_spec_rejects_credentialed_or_invalid_authority(url: str) -> None:
    with pytest.raises(ValueError):
        JobSpec(url=url, company="A", title="B", tier=JobTier.LOW)
