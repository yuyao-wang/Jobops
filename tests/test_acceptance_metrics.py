"""V1 acceptance metrics over sanitized fixtures, never live ATS sites."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median

import pytest

from adapters.ashby import AshbyAdapter
from adapters.greenhouse import GreenhouseAdapter
from adapters.jobvite import JobviteAdapter
from adapters.lever import LeverAdapter
from adapters.protocol import ApplicationContext
from adapters.workday import WorkdayAdapter, WorkdayApplicationContext
from core.outcomes import OutcomeStatus
from tests.support.workday_fsm import FixtureWorkdayFsmPage


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_five_supported_ats_fixture_review_rate_and_model_budget(tmp_path: Path) -> None:
    playwright_module = pytest.importorskip("playwright.async_api")
    async with playwright_module.async_playwright() as playwright:
        # This is the release acceptance gate, not an optional browser smoke
        # test. Missing Chromium must fail CI instead of turning the only >=95%
        # assertion into a skip.
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            resume = tmp_path / "synthetic-resume.pdf"
            resume.write_bytes(b"%PDF-1.4\n% synthetic\n")
            profile = {
                "personal": {
                    "first_name": "Ada",
                    "last_name": "Example",
                    "email": "ada@example.test",
                    "phone": "+1 555 0100",
                }
            }
            results = []
            for name, adapter in (
                ("greenhouse", GreenhouseAdapter()),
                ("lever", LeverAdapter()),
                ("ashby", AshbyAdapter()),
                ("jobvite", JobviteAdapter()),
            ):
                await page.set_content(
                    (FIXTURES / "ats" / f"{name}.html").read_text(encoding="utf-8")
                )
                results.append(
                    await adapter.run(
                        ApplicationContext(
                            page=page,
                            job_url=f"https://fixture.{name}.example/jobs/1",
                            job_id=f"job-{name}",
                            run_id=f"run-{name}",
                            profile=profile,
                            resume_path=resume,
                            answers={"work_authorization": "Yes"},
                            navigate=False,
                            settle_timeout_ms=0,
                        )
                    )
                )

            workday_fixture = json.loads(
                (FIXTURES / "workday" / "multi_stage_fsm.json").read_text(
                    encoding="utf-8"
                )
            )
            workday_page = FixtureWorkdayFsmPage(workday_fixture)
            question = "Why are you interested in this synthetic role?"
            workday_outcome = await WorkdayAdapter().run(
                WorkdayApplicationContext(
                    page=workday_page,
                    job_url=workday_fixture["posting_url"],
                    job_id="job-workday",
                    run_id="run-workday",
                    profile=profile,
                    resume_path=str(resume),
                    answers={
                        question: "A verified synthetic answer.",
                        "gender": "Prefer not to disclose",
                        "disability_status": "Prefer not to disclose",
                    },
                    navigate=False,
                )
            )
            results.append(workday_outcome)

            assert workday_outcome.checkpoint == "workday.review"
            assert workday_outcome.details["exact_readback_bindings"] is True
            assert workday_outcome.details["resumed_at_review"] is False
            assert workday_page.uploaded_file_count == 1
            assert workday_page.next_clicks == len(workday_fixture["stages"]) - 1
        finally:
            await browser.close()

    reached_review = sum(item.status is OutcomeStatus.REVIEW_READY for item in results)
    # Missing telemetry is a contract failure, never implicit proof of zero.
    model_calls = [int(item.details["model_calls"]) for item in results]

    assert reached_review / len(results) >= 0.95
    assert median(model_calls) == 0
    assert model_calls == [0] * len(results)
    assert reached_review == 5
