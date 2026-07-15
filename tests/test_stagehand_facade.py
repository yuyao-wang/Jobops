"""Focused tests for the production Stagehand compatibility façade."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import adapters.stagehand_adapter as facade
from adapters.registry import AdapterRegistry
from core.outcomes import ApplicationOutcome, EvidenceKind, EvidenceRef


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://jobs.lever.co/acme/1", "lever"),
        ("https://jobs.ashbyhq.com/acme/1", "ashby"),
        ("https://jobs.jobvite.com/acme/job/1", "jobvite"),
        ("https://acme.wd5.myworkdayjobs.com/job/1", "workday"),
        ("https://careers.example.test/jobs/1", "generic_ai"),
    ],
)
def test_facade_uses_registry_route_table(url, expected):
    assert AdapterRegistry.route_name(url) == expected


class CapturingRegistry:
    def __init__(self, outcome_factory):
        self.outcome_factory = outcome_factory
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return self.outcome_factory(request)


@pytest.mark.asyncio
async def test_apply_smart_dry_run_maps_review_to_true(monkeypatch):
    registry = CapturingRegistry(
        lambda request: ApplicationOutcome.review_ready(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter=AdapterRegistry.route_name(request.job_url),
        )
    )
    monkeypatch.setattr(facade, "AdapterRegistry", lambda: registry)

    result = await facade.apply_smart(
        SimpleNamespace(),
        "https://jobs.lever.co/acme/1",
        {"resume_path": "/private/resume.pdf"},
        brain=None,
        dry_run=True,
        gate_b_permit="must-not-reach-adapter",
        gate_b_validator=AsyncMock(return_value=True),
        permit_service=object(),
        permit_bindings=object(),
    )

    assert result is True
    assert registry.requests[0].request_submit is False
    assert registry.requests[0].gate_b_permit is None
    assert registry.requests[0].gate_b_validator is None
    assert not hasattr(registry.requests[0], "permit_service")
    assert not hasattr(registry.requests[0], "permit_bindings")
    assert not hasattr(registry.requests[0], "ledger")
    assert registry.requests[0].resume_path == "/private/resume.pdf"


@pytest.mark.asyncio
async def test_apply_stagehand_routes_long_tail_to_registry(monkeypatch):
    registry = CapturingRegistry(
        lambda request: ApplicationOutcome.review_ready(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="generic_ai",
        )
    )
    monkeypatch.setattr(facade, "AdapterRegistry", lambda: registry)

    result = await facade.apply_stagehand(
        SimpleNamespace(),
        "https://careers.example.test/jobs/1",
        {},
        brain=object(),
        dry_run=True,
    )

    assert result is True
    assert AdapterRegistry.route_name(registry.requests[0].job_url) == "generic_ai"


@pytest.mark.asyncio
async def test_live_review_is_not_reported_as_submission(monkeypatch):
    registry = CapturingRegistry(
        lambda request: ApplicationOutcome.review_ready(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="greenhouse",
        )
    )
    monkeypatch.setattr(facade, "AdapterRegistry", lambda: registry)

    result = await facade.apply_smart(
        SimpleNamespace(),
        "https://boards.greenhouse.io/acme/jobs/1",
        {},
        brain=None,
        dry_run=False,
    )

    assert result is False
    request = registry.requests[0]
    assert request.request_submit is True
    assert request.gate_b_permit is None


@pytest.mark.asyncio
async def test_live_true_requires_verified_submission_evidence(monkeypatch):
    registry = CapturingRegistry(
        lambda request: ApplicationOutcome.submitted_verified(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="jobvite",
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    sha256="0" * 64,
                ),
            ),
        )
    )
    monkeypatch.setattr(facade, "AdapterRegistry", lambda: registry)

    result = await facade.apply_smart(
        SimpleNamespace(),
        "https://jobs.jobvite.com/acme/job/1",
        {},
        brain=None,
        dry_run=False,
        gate_b_permit="opaque-token",
        gate_b_validator=AsyncMock(return_value=True),
    )

    assert result is True


@pytest.mark.asyncio
async def test_page_state_rejects_bare_thank_you_as_submission():
    page = SimpleNamespace(
        url="https://careers.example.test/jobs/1",
        inner_text=AsyncMock(return_value="Thank you for visiting our careers page"),
        evaluate=AsyncMock(return_value={"formControls": 1, "invalidControls": 0}),
    )

    assert await facade._detect_page_state(page) == "form"
