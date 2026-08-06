"""Focused S4a guided Dashboard information-architecture tests."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "dashboard/templates/index.html").read_text(encoding="utf-8")
JS = (ROOT / "dashboard/static/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "dashboard/static/style.css").read_text(encoding="utf-8")


def _css_color(token: str) -> str:
    match = re.search(rf"--{re.escape(token)}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert match is not None
    return match.group(1)


def _luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    bright, dark = sorted(
        (_luminance(first), _luminance(second)), reverse=True
    )
    return (bright + 0.05) / (dark + 0.05)


def test_first_run_is_guided_and_has_one_product_navigation() -> None:
    assert 'class="primary-nav"' in HTML
    for label in ("Home", "Jobs", "Applications", "Profile", "Settings"):
        assert f'data-nav="{label.lower()}"' in HTML
    assert "Three steps to your first job matches" in HTML
    assert "Add your information" in JS
    assert "Set priorities and sources" in JS
    assert "Refresh your job library" in JS
    for legacy_action in (
        "SCORE ALL",
        "> DISCOVER",
        "> APPLY",
        "YOLO",
        "Mission Goals",
        "Skills Matrix",
        "Search Queries",
        "No targets acquired",
    ):
        assert legacy_action not in HTML
        assert legacy_action not in JS
    assert "metric-card" not in HTML
    assert "chart.js" not in HTML.casefold()


def test_attention_precedes_other_actions_and_hides_internal_detail() -> None:
    attention_position = HTML.index('id="attention-list"')
    matches_position = HTML.index('id="top-matches"')
    recent_position = HTML.index('id="recent-applications"')
    assert attention_position < matches_position < recent_position
    assert 'data-attention-id=' in JS
    assert "/api/human-attention-inbox/${encodeURIComponent(item.item_id)}/${endpoint}" in JS
    assert '["PROVIDE_FACT", "MAKE_CHOICE", "ATTEST"]' in JS
    assert "<details" in HTML
    assert "Technical details" in JS
    assert "source_stage" not in HTML
    assert "resolution_capability" not in HTML
    assert "reason_code" not in HTML
    assert "This is a system issue, not an empty result" in JS


def test_gate_b_uses_an_exact_frontend_review_and_submit_action() -> None:
    assert 'id="submission-review-dialog"' in HTML
    assert "Review this application before submitting" in HTML
    assert "Confirm and submit" in HTML
    assert "this reviewed version only" in HTML
    assert "does not retry automatically" in HTML
    assert 'item.next_action === "REVIEW_AND_SUBMIT"' in JS
    assert "data-review-plan" in JS
    assert "data-review-run" in JS
    assert 'reviewSource === "COMPATIBILITY_RUN"' in JS
    assert "`/api/application-reviews/${encodeURIComponent(reviewId)}`" in JS
    assert "`/api/reviewed-applications/${encodeURIComponent(reviewId)}/refresh-review`" in JS
    assert "`/api/reviewed-applications/${encodeURIComponent(reviewId)}/submit`" in JS
    assert "{ review_token: review.review_token, confirmed: true }" in JS
    assert "result.status === \"SUBMISSION_UNCERTAIN\"" in JS
    submission = JS[
        JS.index("async function confirmApplicationSubmission()") :
        JS.index("async function submitAttentionResponse()")
    ]
    assert submission.count("postJson(") == 1
    assert "confirmApplicationSubmission()" not in submission[
        submission.index("{") + 1 :
    ]


def test_automation_and_refresh_remain_separate_single_controller_calls() -> None:
    assert JS.count('rawJson("/api/auth/local-session"') == 1
    assert JS.count('rawJson("/api/auth/session"') == 1
    assert "credentials: \"same-origin\"" in JS
    assert "error.status === 401" in JS
    assert "error.status === 503" in JS
    assert JS.count('postJson("/api/automation-cycle/run"') == 1
    assert JS.count('getJson("/api/automation-cycle/status"') == 2
    assert JS.count('postJson("/api/automation-cycle/stop"') == 1
    assert JS.count('postJson("/api/job-library/refresh"') == 1
    assert JS.count('getJson("/api/job-library/refresh/status"') == 2
    automation = JS[JS.index("async function runAutomation()") :]
    automation = automation[: automation.index("function updateRunningButtons")]
    assert "/api/job-library/refresh" not in automation
    refresh = JS[JS.index("async function refreshJobs()") :]
    refresh = refresh[: refresh.index("async function runAutomation()")]
    assert "/api/automation-cycle/run" not in refresh
    assert "if (state.automating) return" not in refresh
    assert "refresh.disabled = state.refreshing" in JS
    assert "automation.disabled = state.automating" in JS
    assert "|| state.automationStarting" in JS
    assert "|| state.automationReconciling" in JS
    assert "Stop after current application" in HTML
    assert 'id="automation-progress"' in HTML
    assert 'aria-live="polite"' in HTML
    progress_open = HTML.split('id="automation-progress"', 1)[1].split(">", 1)[0]
    assert 'role="status"' not in progress_open
    assert 'id="automation-progress-message" role="status"' in HTML
    run_button = HTML.split('id="run-automation"', 1)[1].split(">", 1)[0]
    assert "disabled" in run_button
    assert "await waitForAutomationCompletion(generation)" in JS
    polling = JS[JS.index("async function waitForAutomationCompletion(") :]
    polling = polling[: polling.index("async function runAutomation()")]
    assert "await loadDashboard();" in polling
    assert polling.index("await loadDashboard();") < polling.index(
        "renderAutomationProgress(observed);"
    )
    assert "resumeAutomationIfRunning()" in JS
    for status in (
        "IDLE",
        "RUNNING",
        "STOPPING",
        "STOPPED",
        "COMPLETED",
        "PARTIAL_FAILURE",
        "FAILED",
        "NOOP",
        "UNCHANGED",
    ):
        assert f'{status}:' in JS
    assert "Stopping takes effect after the current application" in HTML
    assert "Continue automatic applications" in HTML
    assert "every enabled search profile" in HTML
    assert "CAPTCHA, MFA" in HTML
    assert "automationStopIntent" in JS
    assert "automationPollGeneration" in JS
    assert "Status connection interrupted" in JS
    assert "retrying before enabling Start" in JS


def test_automation_progress_key_includes_message_only_updates() -> None:
    progress_key = JS[JS.index("function automationProgressKey(result)") :]
    progress_key = progress_key[: progress_key.index(
        "function automationSnapshotKey(result)"
    )]

    assert 'result.message || ""' in progress_key
    assert "result.stage_failures || []" in progress_key


def test_conversational_job_finder_is_the_only_visible_discovery_input() -> None:
    assert "Exact company-board filters" not in HTML
    assert 'id="search-profile-form"' not in HTML
    assert 'id="search-profile-unavailable"' not in HTML
    assert 'data-open-preferences' in JS
    assert 'getJson("/api/search-profiles"' in JS
    assert 'postJson("/api/search-profiles"' not in JS
    assert 'id="job-finder-dialog"' not in HTML
    assert 'id="job-finder-input"' in HTML
    assert 'id="open-job-finder"' not in HTML
    assert "Find a specific job" in HTML
    assert 'postJson("/api/job-finder/message"' in JS
    assert 'postJson("/api/job-finder/select"' in JS
    assert 'postJson("/api/job-finder/resolve"' in JS
    assert "Paste a job URL or enter a company, title, and location" in HTML
    assert "Add position" in HTML
    assert "Clear" in HTML
    assert 'id="linkedin-search-link"' not in HTML
    assert 'id="assisted-import-form"' not in HTML
    assert "Powered by Glassdoor" not in HTML


def test_priority_preferences_have_one_reviewed_nlp_entry_point() -> None:
    assert "Add a search preference" not in HTML
    assert 'id="preference-dialog"' in HTML
    assert 'id="preference-input"' in HTML
    assert 'id="preference-summary"' in HTML
    assert "roles and seniority" in HTML
    assert "explicit must-not-have constraints" in HTML
    assert 'getJson("/api/prioritization-policy"' in JS
    assert 'postJson("/api/prioritization-policy/draft"' in JS
    assert 'postJson("/api/prioritization-policy/approve"' in JS
    assert 'id="assisted-search-role"' not in HTML
    assert 'id="assisted-search-location"' not in HTML
    assert "Used by discovery and Priority" in JS
    assert 'postJson("/api/prioritization-policy/preferences"' in JS
    assert "Role or title phrase" in JS


def test_preference_editing_lives_only_in_profile_and_jobs_is_read_only() -> None:
    jobs_section = HTML[
        HTML.index('aria-labelledby="jobs-preference-title"'):
        HTML.index('aria-labelledby="job-finder-title"')
    ]
    profile_section = HTML[
        HTML.index('id="profile-preferences"'):
        HTML.index('id="profile-answers"')
    ]
    assert "Current job preferences" in jobs_section
    assert "read-only summary" in jobs_section
    assert "<button" not in jobs_section
    assert "What kind of job do you want?" not in jobs_section
    assert "What kind of job do you want?" in profile_section
    assert "Add preferences with AI" in profile_section

    render_policy = JS[
        JS.index("function renderPrioritizationPolicy()"):
        JS.index("async function savePreferenceItems")
    ]
    profile_form_start = render_policy.rindex("if (profileNode)")
    profile_branch = render_policy[
        profile_form_start:
        render_policy.index("if (jobsNode)", profile_form_start)
    ]
    jobs_branch = render_policy[
        render_policy.rindex("if (jobsNode)"):
    ]
    assert '<form class="preference-editor"' in profile_branch
    assert '<form class="preference-editor"' not in jobs_branch
    assert '<ul class="preference-list">' in jobs_branch


def test_unresolved_leads_are_separate_from_verified_job_rows() -> None:
    assert 'id="job-leads-review"' in HTML
    assert 'id="job-leads-review-list"' in HTML
    assert "Needs your review" in HTML
    assert "These are clues, not verified jobs." in HTML
    assert "function renderNeedsUserJobLeads()" in JS
    assert "state.jobs.needs_user_leads || []" in JS
    assert "Open source" in JS
    assert "Official employer or ATS URL" in JS
    assert "Verify and add" in JS
    assert 'postJson(`/api/job-leads/${encodeURIComponent(leadId)}/resolve`' in JS
    assert "lead.source_url" in JS
    assert "lead.snippet" not in JS
    assert "source_message_digest" not in JS
    assert "query_id" not in JS
    assert "Discovered via ${readableSource(item.discovered_via)}" in JS
    assert "Verified on ${readableDate(item.source_verified_at)}" in JS
    assert ".lead-review-item" in CSS
    assert ".job-provenance" in CSS


def test_refresh_reports_typed_backend_outcomes_and_cannot_be_blocked_by_other_controls() -> None:
    refresh = JS[JS.index("async function refreshJobs()") :]
    refresh = refresh[: refresh.index("async function runAutomation()")]
    assert "await postJson" in refresh
    assert "await waitForRefreshCompletion()" in refresh
    assert "await loadDashboard();\n    showRefreshResult(result);" in refresh
    assert "await loadJobsSnapshot();" in refresh
    assert 'result.status === "PARTIAL_FAILURE"' in refresh
    assert 'result.status === "FAILED"' in refresh
    assert 'result.status === "NOOP"' in refresh
    assert 'result.status === "RUNNING"' in refresh
    assert "summary.jobs_unchanged" in refresh
    assert "Source search and job-library update completed" in refresh
    assert "Priority needs attention" in refresh
    assert "Job search finished with partial failures" not in refresh
    assert 'showNotice(text, tone = "danger")' in JS
    for tone in ("info", "success", "warning", "danger"):
        assert f".notice.is-{tone}" in CSS
    assert 'if (refresh) {' in JS
    assert 'if (automation) {' in JS
    assert "nextStepCopy[next] || nextStepCopy.SYSTEM_ATTENTION" in JS
    assert "if (!title || !description || !button) return" in JS

    assert 'id="job-refresh-progress"' not in HTML
    assert 'id="job-refresh-stage"' not in HTML
    assert 'id="job-refresh-source-results"' not in HTML
    assert "function renderRefreshProgress(result)" not in JS
    assert ".refresh-progress" not in CSS
    assert ".source-result-grid" not in CSS
    polling = JS[JS.index("async function waitForRefreshCompletion()") :]
    polling = polling[: polling.index("async function loadJobsSnapshot()")]
    for progress_field in (
        "summary.lead_requests_completed",
        "summary.lead_public_reads",
        "summary.lead_failures",
        "summary.lead_search_truncated",
        "item.search_hits",
        "item.public_reads",
        "item.lead_failures",
        "item.truncated",
        "sourceProgressKey",
    ):
        assert progress_field in polling


@pytest.mark.asyncio
async def test_real_page_javascript_separates_priority_only_refresh_failure() -> None:
    playwright_module = pytest.importorskip("playwright.async_api")
    async with playwright_module.async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except playwright_module.Error as exc:
            if (
                "MachPortRendezvousServer" in str(exc)
                and "Permission denied" in str(exc)
            ):
                pytest.skip("macOS sandbox denied the Chromium Mach port")
            raise
        try:
            page = await browser.new_page()
            await page.set_content(
                '<div id="global-notice" class="notice" hidden></div>'
                '<div id="header-status" class="header-status">'
                '<span></span><span></span></div>'
                '<div id="jobs-refresh-detail"></div>'
            )
            await page.add_script_tag(path=str(ROOT / "dashboard/static/app.js"))
            await page.evaluate(
                """showRefreshResult({
                    status: "PARTIAL_FAILURE",
                    summary: {
                        completed_profiles: 5,
                        enabled_profiles: 5,
                        searched_profiles: 5,
                        profiles_with_matches: 3,
                        zero_result_profiles: 2,
                        candidates_found: 17,
                        unique_candidates: 17,
                        jobs_created: 0,
                        jobs_updated: 0,
                        jobs_unchanged: 17,
                        priorities_requested: 10,
                        priorities_refreshed: 6,
                        priorities_failed: 4
                    },
                    source_failures: [],
                    priority_failures: [{
                        code: "PROPOSAL_FAILED:AGENT_OUTPUT_INVALID",
                        count: 4,
                        message: "Priority AI output did not satisfy the Priority contract."
                    }]
                })"""
            )
            notice = page.locator("#global-notice")
            text = await notice.text_content()
            assert text is not None
            assert "Source search and job-library update completed" in text
            assert "17 already current" in text
            assert "6 of 10 selected Priority decisions refreshed; 4 failed" in text
            assert "Job search finished with partial failures" not in text
            assert await notice.get_attribute("class") == "notice is-warning"
            assert await page.locator(
                "#header-status span:last-child"
            ).text_content() == "Priority needs attention"
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_real_page_javascript_drives_refresh_and_stoppable_automation() -> None:
    playwright_module = pytest.importorskip("playwright.async_api")
    responses = {
        "/api/auth/session": {"authenticated": True},
        "/api/dashboard/overview": {
            "next_step": "FUTURE_SAFE_STATE",
            "top_matches": [],
            "recent_applications": [],
        },
        "/api/dashboard/profile": {
            "read_status": "READY",
            "profile_state": "READY",
            "verified_required_field_count": 1,
            "required_field_count": 1,
            "missing_required_fields": [],
            "source_summary": {},
            "identity_fields": [],
            "search_preference_summary": {"enabled_profile_count": 1},
            "capabilities": {"review_capability": "UNAVAILABLE"},
        },
        "/api/dashboard/jobs": {
            "read_status": "EMPTY",
            "library_state": "EMPTY",
            "counts": {
                "total": 0,
                "high_match": 0,
                "ready_to_prepare": 0,
                "needs_input": 0,
            },
            "lead_summary": {
                "total": 1,
                "discovered": 0,
                "resolved": 0,
                "needs_user": 1,
                "stale": 0,
            },
            "needs_user_leads": [{
                "lead_id": "job-lead-synthetic",
                "source": "AUTHORIZED_WEB_SEARCH",
                "origin": "LINKEDIN_SEARCH_INDEX",
                "source_url": "https://www.linkedin.com/jobs/view/1001",
                "title_hint": "Unconfirmed Backend Engineer",
                "company_hint": "Example Labs",
                "location_hint": "Calgary",
                "reason": "Confirm the official employer posting.",
                "discovered_at": "2026-07-30T11:00:00Z",
            }],
            "ordered_items": [],
        },
        "/api/dashboard/applications": {
            "read_status": "READY",
            "counts": {"total": 1, "needs_attention": 1},
            "ordered_items": [{
                "application_plan_id": "application-plan-synthetic",
                "job_id": "job-synthetic-attention",
                "title": "Synthetic Review Role",
                "company": "Acme",
                "location": "Calgary",
                "product_status": "NEEDS_ATTENTION",
                "progress_steps": [{
                    "stage": "REVIEW",
                    "state": "BLOCKED",
                }],
                "safe_status_detail": "Waiting for your answer",
                }],
            },
            "/api/reviewed-applications": {
                "status": "SUCCEEDED",
                "items": [],
            },
        "/api/human-attention-inbox": {
            "status": "COMPLETED",
            "user_items": [{
                "item_id": "attention-synthetic",
                "application_plan_id": "application-plan-synthetic",
                "job_id": "job-synthetic-attention",
                "canonical_answer_key": None,
                "required_action": "Choose the verified base resume.",
                "resolution_capability": "MAKE_CHOICE",
                "attention_label": "Choice required",
                "attention_kind": "USER_CHOICE_REQUIRED",
                "source_stage": "BASE_RESUME_SELECTION",
            }],
            "operator_items": [],
        },
        "/api/search-profiles": {
            "status": "SUCCEEDED",
            "available_sources": [],
            "profiles": [],
        },
        "/api/prioritization-policy": {
            "status": "EMPTY",
            "policy": None,
        },
        "/api/automation-cycle/status": {
            "status": "IDLE",
            "invocation_id": "none",
            "phase": None,
            "stop_requested": False,
            "cycles_completed": 0,
            "total_jobs": 0,
            "current_job_index": 0,
            "message": None,
            "stages": [],
            "summary": {},
        },
    }
    refresh_requests = []
    automation_requests = []
    automation_stop_requests = []
    job_finder_requests = []
    refresh_started = False
    refresh_completed = False
    refresh_imported = False
    refresh_poll_count = 0
    automation_started = False
    automation_stop_requested = False
    automation_poll_count = 0
    automation_reconcile_release = asyncio.Event()
    automation_run_release = asyncio.Event()
    finder_added = False

    async with playwright_module.async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except playwright_module.Error as exc:
            if (
                "MachPortRendezvousServer" in str(exc)
                and "Permission denied" in str(exc)
            ):
                pytest.skip("macOS sandbox denied the Chromium Mach port")
            raise
        try:
            page = await browser.new_page()
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))

            async def fulfill_api(route) -> None:
                nonlocal automation_poll_count, automation_started
                nonlocal automation_stop_requested
                nonlocal finder_added, refresh_completed, refresh_imported
                nonlocal refresh_poll_count, refresh_started
                path = route.request.url.removeprefix(
                    "http://jobops.invalid"
                )
                if path == "/api/automation-cycle/run":
                    automation_requests.append(route.request)
                    automation_started = True
                    await automation_run_release.wait()
                    body = {
                        "status": "RUNNING",
                        "invocation_id": "automation-synthetic",
                        "phase": "PREFLIGHT",
                        "stop_requested": False,
                        "cycles_completed": 0,
                        "total_jobs": 2,
                        "current_job_index": 0,
                        "message": "Automatic applications started.",
                        "stages": [],
                        "summary": {},
                    }
                elif path == "/api/automation-cycle/status":
                    if not automation_started:
                        await automation_reconcile_release.wait()
                        body = responses[path]
                    elif automation_stop_requested:
                        automation_poll_count += 1
                        if automation_poll_count == 1:
                            await route.abort("connectionfailed")
                            return
                        stopping = automation_poll_count < 4
                        body = {
                            "status": "STOPPING" if stopping else "STOPPED",
                            "invocation_id": "automation-synthetic",
                            "phase": "STOPPING" if stopping else "STOPPED",
                            "stop_requested": True,
                            "cycles_completed": 0,
                            "total_jobs": 2,
                            "current_job_index": 1 if stopping else 0,
                            "message": None,
                            "stages": [],
                            "summary": {},
                        }
                    else:
                        automation_poll_count += 1
                        body = {
                            "status": "RUNNING",
                            "invocation_id": "automation-synthetic",
                            "phase": "PROCESSING",
                            "stop_requested": False,
                            "cycles_completed": 0,
                            "total_jobs": 2,
                            "current_job_index": 1,
                            "message": None,
                            "stages": [],
                            "summary": {},
                        }
                elif path == "/api/automation-cycle/stop":
                    automation_stop_requests.append(route.request)
                    automation_stop_requested = True
                    body = {
                        "status": "STOPPING",
                        "invocation_id": "automation-synthetic",
                        "phase": "STOPPING",
                        "stop_requested": True,
                        "cycles_completed": 0,
                        "total_jobs": 2,
                        "current_job_index": 1,
                        "message": "Stop requested.",
                        "stages": [],
                        "summary": {},
                    }
                elif path == "/api/job-library/refresh":
                    refresh_requests.append(route.request)
                    refresh_started = True
                    body = {
                        "status": "RUNNING",
                        "invocation_id": "refresh-synthetic",
                        "summary": {},
                    }
                elif path == "/api/job-library/refresh/status":
                    if refresh_started:
                        refresh_poll_count += 1
                        if refresh_poll_count == 1:
                            refresh_imported = True
                            body = {
                                "status": "RUNNING",
                                "phase": "IMPORTING",
                                "message": "2 unique jobs were imported.",
                                "summary": {
                                    "enabled_profiles": 1,
                                    "searched_profiles": 1,
                                    "profiles_with_matches": 1,
                                    "zero_result_profiles": 0,
                                    "candidates_found": 2,
                                    "unique_candidates": 2,
                                    "candidates_processed": 2,
                                    "jobs_created": 2,
                                },
                                "source_results": [{
                                    "profile_id": "search-profile-synthetic",
                                    "provider": "KNOWN_ASHBY_BOARD",
                                    "source_id": "example",
                                    "status": "SUCCEEDED",
                                    "candidate_count": 2,
                                    "message": None,
                                }],
                            }
                        else:
                            refresh_completed = True
                            body = {
                                "status": "COMPLETED",
                                "summary": {
                                    "jobs_created": 2,
                                    "jobs_updated": 1,
                                    "searched_profiles": 1,
                                    "enabled_profiles": 1,
                                    "profiles_with_matches": 1,
                                    "zero_result_profiles": 0,
                                    "candidates_found": 3,
                                    "unique_candidates": 3,
                                    "lead_refresh_ran": True,
                                    "lead_requests": 12,
                                    "lead_requests_completed": 11,
                                    "leads_discovered": 48,
                                    "leads_unique": 39,
                                    "leads_deduplicated": 9,
                                    "lead_public_reads": 21,
                                    "leads_resolved": 20,
                                    "leads_needing_review": 19,
                                    "lead_failures": 1,
                                    "lead_search_truncated": True,
                                },
                                "source_results": [{
                                    "result_type": "JOB_LEAD",
                                    "provider": "LINKEDIN",
                                    "source_id": "LINKEDIN",
                                    "acquisition_source": "AUTHORIZED_WEB_SEARCH",
                                    "status": "PARTIAL_FAILURE",
                                    "requests": 12,
                                    "completed": 11,
                                    "search_hits": 48,
                                    "leads_discovered": 48,
                                    "leads_unique": 39,
                                    "leads_deduplicated": 9,
                                    "leads_resolved": 20,
                                    "leads_needing_review": 19,
                                    "lead_failures": 1,
                                    "public_reads": 21,
                                    "truncated": True,
                                }],
                            }
                    else:
                        body = {"status": "NOOP", "summary": {}}
                elif path == "/api/job-finder/message":
                    job_finder_requests.append(route.request)
                    payload = route.request.post_data_json
                    if len(payload["messages"]) == 1:
                        body = {
                            "kind": "SEARCH",
                            "status": "NEEDS_USER",
                            "reason": "NEEDS_MORE_INFORMATION",
                            "retryable": False,
                            "prompt": "Which company is this role at?",
                            "candidate_set_id": None,
                            "candidates": [],
                            "missing_fields": ["company"],
                        }
                    else:
                        body = {
                            "kind": "SEARCH",
                            "status": "NEEDS_USER",
                            "reason": None,
                            "retryable": False,
                            "prompt": "Found one matching job. Please select it.",
                            "candidate_set_id": "candidate-set-synthetic",
                            "selection_status": "WAITING_FOR_CANDIDATE_SELECTION",
                            "candidates": [
                                {
                                    "candidate_id": "greenhouse:acme:1001",
                                    "company": "Acme",
                                    "title": "Backend Engineer",
                                    "location": "Calgary",
                                    "source_platform": "greenhouse",
                                    "source_url": "https://job-boards.greenhouse.io/acme/jobs/1001",
                                }
                            ],
                            "missing_fields": [],
                        }
                elif path == "/api/job-finder/select":
                    job_finder_requests.append(route.request)
                    body = {
                        "kind": "INTAKE",
                        "status": "NEEDS_USER",
                        "reason": None,
                        "retryable": False,
                        "prompt": "Review this job before adding it.",
                        "pending_intake_id": "pending-synthetic",
                        "pending_status": "WAITING_FOR_ACTION",
                        "summary": {
                            "company": "Acme",
                            "title": "Backend Engineer",
                            "location": "Calgary",
                            "source_platform": "greenhouse",
                        },
                        "actions": ["ADD_JOB", "REQUEST_APPLICATION"],
                    }
                elif path == "/api/job-finder/resolve":
                    job_finder_requests.append(route.request)
                    finder_added = True
                    body = {
                        "kind": "RESOLUTION",
                        "status": "COMPLETED",
                        "reason": "JOB_CREATED",
                        "retryable": False,
                        "prompt": "The job was added to your job list.",
                        "pending_intake_id": "pending-synthetic",
                        "selected_action": "ADD_JOB",
                        "job_id": "job-synthetic",
                        "change": "CREATED",
                    }
                elif path == "/api/dashboard/jobs" and refresh_completed:
                    body = {
                        **responses[path],
                        "read_status": "READY",
                        "library_state": "READY",
                        "counts": {
                            "total": 3,
                            "high_match": 2,
                            "ready_to_prepare": 1,
                            "needs_input": 0,
                        },
                    }
                elif (
                    path == "/api/dashboard/jobs"
                    and finder_added
                    and not refresh_imported
                ):
                    body = {
                        **responses[path],
                        "read_status": "READY",
                        "library_state": "READY",
                        "counts": {
                            "total": 1,
                            "high_match": 0,
                            "ready_to_prepare": 0,
                            "needs_input": 0,
                        },
                    }
                elif path == "/api/dashboard/jobs" and refresh_imported:
                    body = {
                        **responses[path],
                        "read_status": "READY",
                        "library_state": "READY",
                        "counts": {
                            "total": 2,
                            "high_match": 0,
                            "ready_to_prepare": 0,
                            "needs_input": 0,
                        },
                    }
                else:
                    body = responses[path]
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(body),
                )

            await page.route("http://jobops.invalid/api/**", fulfill_api)
            html = HTML.replace(
                "<head>",
                '<head><base href="http://jobops.invalid/">',
            ).replace(
                '<link rel="stylesheet" href="/static/style.css?v=multi-source-leads-v8">',
                "",
            ).replace(
                '<script defer src="/static/app.js?v=attention-context-v9"></script>',
                "",
            )
            await page.set_content(html)
            await page.add_script_tag(path=str(ROOT / "dashboard/static/app.js"))
            await page.evaluate(
                "document.dispatchEvent(new Event('DOMContentLoaded'))"
            )
            await page.wait_for_timeout(100)
            assert page_errors == []
            await page.wait_for_function(
                "document.querySelector('#header-status span:last-child').textContent === 'Up to date'"
            )
            assert await page.locator("#run-automation").is_disabled()
            automation_reconcile_release.set()
            await page.wait_for_function(
                "!document.querySelector('#run-automation').disabled"
            )

            await page.locator('[data-nav="jobs"]').first.click()
            assert await page.get_by_text(
                "Needs your review", exact=True
            ).is_visible()
            assert await page.get_by_text(
                "Unconfirmed Backend Engineer", exact=True
            ).is_visible()
            assert await page.get_by_role(
                "link", name="Open source"
            ).get_attribute("href") == (
                "https://www.linkedin.com/jobs/view/1001"
            )
            assert await page.locator("#search-profile-form").count() == 0
            assert await page.get_by_text(
                "Find a specific job", exact=True
            ).is_visible()

            await page.locator("#job-finder-input").fill("Backend engineer")
            await page.locator("#send-job-finder-message").click()
            await page.locator("#job-finder-status").get_by_text(
                "Which company is this role at?", exact=True
            ).wait_for()
            await page.locator("#job-finder-input").fill("Acme in Calgary")
            await page.locator("#send-job-finder-message").click()
            await page.get_by_text("Backend Engineer", exact=True).click()
            await page.get_by_text("Add to job list", exact=True).click()
            await page.locator("#job-finder-status").get_by_text(
                "The job was added to your job list.", exact=True
            ).wait_for()
            assert len(job_finder_requests) == 4
            assert job_finder_requests[0].post_data_json["messages"] == [
                "Backend engineer"
            ]
            assert job_finder_requests[1].post_data_json["messages"] == [
                "Backend engineer",
                "Acme in Calgary",
            ]
            assert all(
                "subject_id" not in request.post_data_json
                for request in job_finder_requests
            )
            assert job_finder_requests[-1].post_data_json["action"] == "ADD_JOB"
            assert (
                await page.locator("#job-counts .summary-item strong").first.text_content()
                == "1"
            )
            await page.locator("#refresh-jobs").click()
            assert await page.locator("#job-refresh-progress").count() == 0
            assert await page.locator("#job-refresh-source-results").count() == 0
            await page.wait_for_function(
                "document.querySelector('#global-notice').textContent.includes('2 provider-feed jobs added') && document.querySelector('#global-notice').textContent.includes('1 provider-feed jobs updated')"
            )
            await page.wait_for_function(
                "document.querySelector('#job-counts').textContent.includes('3')"
            )
            assert len(refresh_requests) == 1
            assert refresh_requests[0].method == "POST"
            assert set(refresh_requests[0].post_data_json) == {
                "invocation_id"
            }
            assert "max_reprioritizations" not in (
                refresh_requests[0].post_data_json
            )

            await page.locator('[data-nav="applications"]').first.click()
            await page.get_by_role(
                "button", name="Resolve required information"
            ).click()
            await page.locator("#attention-dialog[open]").wait_for()
            assert await page.locator(
                "#attention-dialog-action"
            ).text_content() == "Choose the verified base resume."
            assert await page.locator(
                "#attention-dialog-title"
            ).text_content() == (
                "Choice required: Synthetic Review Role at Acme"
            )
            await page.get_by_role(
                "button", name="Close attention dialog"
            ).click()
            await page.locator("#run-automation").click()
            await page.locator("#automation-progress").wait_for(
                state="visible"
            )
            assert await page.locator("#stop-automation").is_visible()
            await page.locator("#stop-automation").click()
            assert len(automation_stop_requests) == 0
            assert await page.locator("#stop-automation").is_visible()
            assert await page.locator("#stop-automation").text_content() == (
                "Stopping safely…"
            )
            automation_run_release.set()
            await page.wait_for_function(
                "document.querySelector('#automation-progress-message').textContent.includes('Status connection interrupted')"
            )
            assert await page.locator("#stop-automation").is_visible()
            await page.locator("#automation-progress-message").get_by_text(
                "Automatic applications stopped safely. Completed work remains saved, and you can continue later.",
                exact=True,
            ).wait_for()

            assert len(automation_requests) == 1
            assert automation_requests[0].method == "POST"
            assert set(automation_requests[0].post_data_json) == {
                "invocation_id"
            }
            assert len(automation_stop_requests) == 1
            assert automation_stop_requests[0].post_data_json == {
                "invocation_id": "automation-synthetic"
            }
            assert automation_poll_count >= 2
            assert not await page.locator("#stop-automation").is_visible()
            assert await page.evaluate("document.activeElement.id") == (
                "automation-progress-title"
            )

            await page.evaluate(
                "() => { setHeader('Up to date', ''); return resumeAutomationIfRunning(); }"
            )
            assert await page.locator(
                "#header-status span:last-child"
            ).text_content() == "Automation stopped"
        finally:
            await browser.close()


def test_readability_accessibility_and_dangerous_action_location() -> None:
    assert '<meta name="color-scheme" content="light">' in HTML
    assert "color-scheme: light" in CSS
    assert "--bg: #f5f7fb" in CSS
    assert "--surface: #ffffff" in CSS
    assert "--text: #172033" in CSS
    assert "color-scheme: dark" not in CSS
    assert "--bg: #0b1020" not in CSS
    assert _luminance(_css_color("bg")) > 0.8
    assert _luminance(_css_color("surface")) > 0.85
    for foreground in ("text", "muted", "accent-strong"):
        assert _contrast(_css_color(foreground), _css_color("surface")) >= 4.5
    assert _contrast(_css_color("on-accent"), _css_color("accent")) >= 4.5
    assert "font: 16px/" in CSS
    assert ".job-row {" in CSS and "font-size: 14px" in CSS
    assert "min-height: 42px" in CSS
    assert "max-width: 1280px" in CSS
    assert ":focus-visible" in CSS
    assert "prefers-reduced-motion" in CSS
    assert 'aria-label="Main navigation"' in HTML
    assert 'role="status"' in HTML
    assert 'role="tablist"' in HTML
    assert 'class="page" id="page-settings"' in HTML
    assert HTML.count('id="delete-local-data"') == 1
    settings = HTML[HTML.index('id="page-settings"') :]
    assert 'id="delete-local-data"' in settings
    assert 'id="delete-confirmation"' in settings
    assert 'id="activity-content"' in HTML
    assert "/api/purge" not in JS
