from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
from pathlib import Path

import pytest
import uvicorn
import yaml
from playwright.async_api import async_playwright, expect

from auth.credentials import InMemoryCredentialStore
from core.job_leads import JobLeadStatus, JobLeadSource
from core.model_provider_capabilities import model_execution_isolation_profiles
from core.production_application_bootstrap import (
    build_production_application_bootstrap,
    production_application_config_from_mapping,
)
from core.production_automation_composition import (
    build_production_automation_composition,
)
from dashboard.server import app as dashboard_app
from dashboard.job_source_intake import AssistedJobImportController
from source_connectors.contract import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    ReadJobResult,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)
from utils.llm import CodexCLIBackend


SUBJECT = "subject-synthetic-web-clipper"


def _port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


async def _start_server(port: int):
    server = uvicorn.Server(
        uvicorn.Config(
            dashboard_app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve()),
        daemon=True,
    )
    thread.start()
    for _ in range(200):
        if server.started:
            return server, thread
        await asyncio.sleep(0.025)
    raise RuntimeError("synthetic Dashboard server did not start")


async def _bootstrap(tmp_path: Path):
    example = (
        Path(__file__).parents[1]
        / "config"
        / "production.application.example.yaml"
    )
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    raw["private_home"]["root"] = str(tmp_path / "private-home")
    raw["authentication"]["local_subject_id"] = SUBJECT
    raw["authentication"]["session_secret_ref"] = {
        "source": "ENV",
        "name": "JOBOPS_SYNTHETIC_CLIPPER_SESSION_SECRET",
    }
    bootstrap = await build_production_application_bootstrap(
        production_application_config_from_mapping(raw),
        credential_store=InMemoryCredentialStore(),
        environ={
            "JOBOPS_SYNTHETIC_CLIPPER_SESSION_SECRET": (
                "synthetic-clipper-session-secret"
            )
        },
        backend_registry={"codex_cli": CodexCLIBackend},
        isolation_profile_registry=model_execution_isolation_profiles(
            isolated_subscription_cli_runner_available=True
        ),
    )
    composition = build_production_automation_composition(bootstrap=bootstrap)
    return bootstrap, composition


def _handoff(payload: dict[str, str]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


@pytest.mark.asyncio
async def test_current_page_handoff_runs_real_dashboard_javascript(
    tmp_path: Path,
) -> None:
    bootstrap, composition = await _bootstrap(tmp_path)
    saved_state = dict(dashboard_app.state._state)
    composition.install_dashboard(dashboard_app)
    official_url = "https://jobs.ashbyhq.com/example/synthetic-posting"
    observation = SourceJobObservation(
        source_platform=SourcePlatform.ASHBY,
        source_job_id="synthetic-posting",
        source_url=official_url,
        application_url=f"{official_url}/application",
        company="Example Robotics",
        title="Synthetic Machine Learning Engineer",
        description="Build reliable synthetic systems.",
        location="Calgary",
        work_mode=WorkMode.HYBRID,
        posted_at=None,
        ats_type=AtsType.ASHBY,
        observed_at="2026-08-04T18:00:00+00:00",
        provenance=(
            FieldProvenance(
                "description",
                ProvenanceSource.SOURCE_API,
                "descriptionPlain",
            ),
        ),
    )
    composed_intake = dashboard_app.state.assisted_job_import_controller
    dashboard_app.state.assisted_job_import_controller = (
        AssistedJobImportController(
            public_job_reader=lambda request: ReadJobResult.succeeded(
                observation
            ),
            discovery=composed_intake.discovery,
            clock=composed_intake.clock,
            lead_repository=composed_intake.lead_repository,
        )
    )
    server = thread = browser = playwright = None
    try:
        port = _port()
        server, thread = await _start_server(port)
        payload = {
            "page_url": "https://www.linkedin.com/jobs/view/123456",
            "page_title": "Synthetic Machine Learning Engineer",
            "selected_text": "Synthetic public job excerpt.",
        }
        url = (
            f"http://127.0.0.1:{port}/#jobops-clip="
            f"{_handoff(payload)}"
        )
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded")

        dialog = page.locator("#job-clipper-dialog")
        await expect(dialog).to_be_visible(timeout=15_000)
        await expect(page.locator("#job-clipper-title")).to_have_text(
            "Synthetic Machine Learning Engineer"
        )
        assert "jobops-clip" not in await page.evaluate("location.href")

        await page.locator("#save-job-clip").click()
        await expect(dialog).not_to_be_visible(timeout=15_000)
        await expect(page.locator("#global-notice")).to_contain_text(
            "saved as an unverified lead",
            timeout=15_000,
        )
        await expect(page.locator("#job-leads-review-list")).to_contain_text(
            "Synthetic Machine Learning Engineer",
            timeout=15_000,
        )

        lead_form = page.locator("[data-lead-resolution-form]")
        await lead_form.locator('input[name="official_job_url"]').fill(
            official_url
        )
        await lead_form.locator('button[type="submit"]').click()
        await expect(page.locator("#global-notice")).to_contain_text(
            "official posting was verified",
            timeout=15_000,
        )
        await expect(page.locator("#job-leads-review")).to_be_hidden(
            timeout=15_000
        )
        jobs_response = await page.request.get(
            f"http://127.0.0.1:{port}/api/dashboard/jobs"
        )
        jobs_payload = await jobs_response.json()
        assert jobs_payload["read_status"] in {"READY", "EMPTY"}, jobs_payload
        await expect(page.locator("#jobs-list")).to_contain_text(
            "Synthetic Machine Learning Engineer",
            timeout=15_000,
        )

        listed = bootstrap.repository_bundle.require(
            "job_leads"
        ).list_current(SUBJECT)
        assert len(listed.leads) == 1
        assert listed.leads[0].source is JobLeadSource.WEB_CLIPPER
        assert listed.leads[0].status is JobLeadStatus.RESOLVED
        assert listed.leads[0].canonical_url == official_url
    finally:
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            await asyncio.to_thread(thread.join, 10)
        dashboard_app.state._state.clear()
        dashboard_app.state._state.update(saved_state)
        await bootstrap.close()
