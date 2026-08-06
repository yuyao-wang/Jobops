"""Deterministic real-browser E2E for the Workday Golden Path."""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import async_playwright, expect

from auth.credentials import InMemoryCredentialStore
from core.application_bundle_assembly import ApplicationBundleAssemblyStatus
from core.authenticated_subject import (
    KeychainAuthenticatedSubjectSessionProvider,
    LocalAuthenticatedSubjectSessionIssuer,
)
from core.event_ledger import EventLedger, SubmissionStatus
from core.permits import PermitService
from core.real_application_control_plane import RealApplicationControlPlane
from dashboard.authentication import (
    LocalDashboardSessionController,
    make_authenticated_subject_dependency,
)
from dashboard.server import app as dashboard_app
from dashboard.server import configure_real_application_control_plane
from jobops.browser_executor import LocalWorkdayBrowserExecutor
from jobops.control_client import RealApplicationControlClient
from jobops.real_application import load_formal_real_application
from utils.browser_session import BrowserSession

sys.path.insert(0, str(Path(__file__).parent))
from test_application_bundle_assembly import (  # noqa: E402
    SUBJECT_ID,
    _run as assemble_bundle,
    _setup as setup_bundle,
)
from test_prepared_cover_letter_material import _JobRepository  # noqa: E402


def _port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


async def _start_server(application: FastAPI, port: int):
    server = uvicorn.Server(
        uvicorn.Config(
            application,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve()), daemon=True
    )
    thread.start()
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.025)
    else:
        raise RuntimeError("test HTTP server did not start")
    return server, thread


async def _stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    await asyncio.to_thread(thread.join, 10)


def _fake_workday() -> tuple[FastAPI, dict[str, int]]:
    application = FastAPI()
    state = {"submits": 0}
    html = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Synthetic Workday</title></head>
<body><div id="retained" hidden></div><main id="root"></main><script>
const root = document.querySelector('#root');
const retained = document.querySelector('#retained');
const postingPath = location.pathname;
window.__jobopsVisitedStates = ['job'];
let stage = 'job';
const stages = ['autofillWithResume', 'myInformation', 'myExperience', 'applicationQuestions', 'review'];
function cleanBase() {
  const lowered = location.pathname.toLowerCase();
  for (const token of stages.map(value => '/' + value.toLowerCase())) {
    const index = lowered.indexOf(token);
    if (index >= 0) return location.pathname.slice(0, index);
  }
  return postingPath;
}
function go(next) {
  stage = next;
  window.__jobopsVisitedStates.push(next);
  history.pushState({}, '', cleanBase() + '/' + next);
  render();
}
function nextButton(next) {
  return `<button data-automation-id="bottom-navigation-next-button" onclick="go('${next}')">Next</button>`;
}
function render() {
  for (const control of Array.from(root.querySelectorAll('input, textarea, select'))) {
    retained.appendChild(control);
  }
  if (stage === 'job') {
    root.innerHTML = `<h1>Synthetic Research Engineer</h1><button data-automation-id="jobPostingApplyButton" onclick="go('autofillWithResume')">Apply</button>`;
  } else if (stage === 'autofillWithResume') {
    root.innerHTML = `<h1>Autofill with Resume</h1><label>Resume Upload<input required type="file" data-automation-id="resumeUpload"></label>${nextButton('myInformation')}`;
  } else if (stage === 'myInformation') {
    root.innerHTML = `<h1 data-automation-id="myInformation">My Information</h1><label>Email<input required type="email" data-automation-id="email"></label>${nextButton('myExperience')}`;
  } else if (stage === 'myExperience') {
    root.innerHTML = `<h1 data-automation-id="myExperience">My Experience</h1><section data-automation-id="workExperienceSection"><h2>Work Experience 1</h2><p>Resume autofill retained synthetic employment.</p></section><section><h2>Education 1</h2><p>Resume autofill retained synthetic education.</p></section>${nextButton('applicationQuestions')}`;
  } else if (stage === 'applicationQuestions') {
    root.innerHTML = `<h1 data-automation-id="applicationQuestions">Application Questions</h1><p>No additional required question for this fixture.</p>${nextButton('review')}`;
  } else if (stage === 'review') {
    root.innerHTML = `<section data-automation-id="reviewPage"><h1>Review</h1><dl><dt>Email</dt><dd>synthetic@example.test</dd><dt>Employment</dt><dd>Resume autofill retained synthetic employment.</dd><dt>Education</dt><dd>Resume autofill retained synthetic education.</dd></dl><p>I certify that the information in this application is accurate.</p><button data-automation-id="submitApplicationButton" onclick="submitOnce()">Submit</button></section>`;
  } else {
    root.innerHTML = `<section data-automation-id="applicationSubmitted"><h1>Application submitted</h1><p>Confirmation ID: SYNTHETIC-12345</p><p>Thank you for applying.</p></section>`;
  }
}
async function submitOnce() {
  const response = await fetch('/record-submit', {method: 'POST'});
  if (!response.ok) return;
  stage = 'confirmation';
  window.__jobopsVisitedStates.push(stage);
  history.pushState({}, '', cleanBase() + '/confirmation');
  render();
}
render();
</script></body></html>"""

    @application.post("/record-submit")
    async def record_submit(request: Request):
        del request
        state["submits"] += 1
        return JSONResponse({"submitted": True})

    @application.get("/{path:path}")
    async def page(path: str):
        del path
        return HTMLResponse(html)

    return application, state


@pytest.mark.asyncio
async def test_fake_workday_http_browser_dashboard_and_atomic_submit_e2e(
    tmp_path: Path,
) -> None:
    fake_port = _port()
    fake_host = "synthetic.wd5.myworkdayjobs.com"
    workday_url = (
        f"http://{fake_host}:{fake_port}/en-US/External/job/"
        "Synthetic-Research-Engineer/JR-12345"
    )
    fake_app, fake_state = _fake_workday()
    fake_server, fake_thread = await _start_server(fake_app, fake_port)

    parts = setup_bundle(tmp_path)
    workday_job = replace(
        parts["parts"]["cover"]["job"],
        source_url=workday_url,
        application_url=workday_url,
        ats_type="workday",
    )
    parts["job_repository"] = _JobRepository(workday_job)
    assembled = assemble_bundle(parts)
    assert assembled.status is ApplicationBundleAssemblyStatus.CREATED
    preparation, _bundle = load_formal_real_application(
        subject_id=SUBJECT_ID,
        assembly_record_id=assembled.record.record_id,
        home=parts["home"],
    )

    control_port = _port()
    ledger = EventLedger(tmp_path / "control" / "events.sqlite3")
    control = RealApplicationControlPlane(
        ledger=ledger,
        permit_service=PermitService(secret=b"e" * 32, ledger=ledger),
        subject_id=SUBJECT_ID,
        enrollment_secret="synthetic-one-time-enrollment",
    )
    dashboard_credentials = InMemoryCredentialStore()
    dashboard_provider = KeychainAuthenticatedSubjectSessionProvider(
        dashboard_credentials
    )
    issuer = LocalAuthenticatedSubjectSessionIssuer(
        session_writer=dashboard_provider,
        subject_id=SUBJECT_ID,
        master_secret="synthetic-dashboard-master-secret",
        ttl_seconds=600,
    )
    saved_state = dict(dashboard_app.state._state)
    configure_real_application_control_plane(
        application=dashboard_app,
        control_plane=control,
        local_session_controller=LocalDashboardSessionController(
            issuer=issuer, clock=lambda: datetime.now(timezone.utc)
        ),
        authenticated_subject=make_authenticated_subject_dependency(
            session_provider=dashboard_provider,
            clock=lambda: datetime.now(timezone.utc),
        ),
    )
    control_server = control_thread = None
    dashboard_browser = None
    dashboard_playwright = None
    execution = None
    try:
        control_server, control_thread = await _start_server(
            dashboard_app, control_port
        )
        server = f"http://127.0.0.1:{control_port}"
        worker_credentials = InMemoryCredentialStore()
        client = RealApplicationControlClient(
            server, credential_store=worker_credentials
        )
        await client.enroll("synthetic-one-time-enrollment")
        prepared = await client.prepare(preparation.to_dict())
        assert prepared["status"] == "CREATED"

        async with httpx.AsyncClient(base_url=server) as http:
            rejected = await http.post(
                "/api/auth/local-session",
                headers={
                    "Origin": "http://attacker.example",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
        assert rejected.status_code == 403

        config = SimpleNamespace(
            authentication=SimpleNamespace(local_subject_id=SUBJECT_ID),
            browser=SimpleNamespace(
                slow_mo_ms=0,
                lease_ttl_seconds=120,
                navigation_timeout_seconds=20,
            ),
            execution_policy=SimpleNamespace(
                allow_keychain_login=False,
                allow_account_registration=False,
            ),
        )

        async def launch_test_browser(playwright, profile, *, headless=False):
            del profile, headless
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(tmp_path / "executor-chromium"),
                headless=True,
                args=[
                    f"--host-resolver-rules=MAP {fake_host} 127.0.0.1",
                    "--no-proxy-server",
                ],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            return BrowserSession(
                context=context,
                page=page,
                user_data_dir=tmp_path / "executor-chromium",
            )

        executor = LocalWorkdayBrowserExecutor(
            client=client,
            config=config,
            home=parts["home"],
            credential_store=InMemoryCredentialStore(),
            launch=launch_test_browser,
            poll_seconds=0.05,
        )
        execution = asyncio.create_task(executor.run_once())

        dashboard_playwright = await async_playwright().start()
        dashboard_browser = await dashboard_playwright.chromium.launch(
            headless=True
        )
        dashboard_context = await dashboard_browser.new_context()
        dashboard_page = await dashboard_context.new_page()
        await dashboard_page.goto(server, wait_until="domcontentloaded")
        forged_issue = await dashboard_page.evaluate(
            """async () => {
                const response = await fetch('/api/auth/local-session?subject_id=subject-forged', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-Subject-ID': 'subject-forged'},
                    body: JSON.stringify({subject_id: 'subject-forged'})
                });
                return response.status;
            }"""
        )
        assert forged_issue == 200
        await dashboard_page.locator("[data-attempt]").first.click()
        await expect(dashboard_page.locator("#attempt-status")).to_have_text(
            "REVIEW_READY", timeout=30_000
        )
        legal = dashboard_page.locator("#legal-summary")
        await expect(legal).to_contain_text("I certify")

        forged = await dashboard_page.evaluate(
            """async () => {
                const response = await fetch('/api/real-applications?subject_id=subject-forged', {
                    headers: {'X-Subject-ID': 'subject-forged'}
                });
                return {status: response.status, body: await response.json()};
            }"""
        )
        assert forged["status"] == 200
        assert len(forged["body"]["applications"]) == 1

        await dashboard_page.locator("#approval-confirmation").check()
        await dashboard_page.locator("#approve-application").click()
        assert await asyncio.wait_for(execution, timeout=45) == "CONFIRMED"
        assert fake_state["submits"] == 1

        await dashboard_page.locator("#refresh-attempts").click()
        await expect(dashboard_page.locator("#attempt-status")).to_have_text(
            "CONFIRMED", timeout=10_000
        )
        await expect(dashboard_page.locator("#attempt-timeline")).to_contain_text(
            "REAL_APPLICATION_APPROVED"
        )
        await expect(dashboard_page.locator("#job-summary")).to_contain_text(
            "SYNTHETIC-12345"
        )

        task = control.get_task(preparation.attempt_id)
        assert task["confirmation_id"] == "SYNTHETIC-12345"
        intent = ledger.get_submission_intent(task["submission_intent_id"])
        assert intent.status is SubmissionStatus.VERIFIED
        assert ledger.get_run(preparation.attempt_id).state == "SUBMITTED_VERIFIED"
    finally:
        if execution is not None and not execution.done():
            execution.cancel()
            with pytest.raises(asyncio.CancelledError):
                await execution
        if dashboard_browser is not None:
            await dashboard_browser.close()
        if dashboard_playwright is not None:
            await dashboard_playwright.stop()
        if control_server is not None and control_thread is not None:
            await _stop_server(control_server, control_thread)
        dashboard_app.state._state.clear()
        dashboard_app.state._state.update(saved_state)
        await _stop_server(fake_server, fake_thread)
