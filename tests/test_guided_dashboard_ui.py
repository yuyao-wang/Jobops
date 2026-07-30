"""Focused S4a guided Dashboard information-architecture tests."""

from __future__ import annotations

import re
from pathlib import Path


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
    assert "Set roles and locations" in JS
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


def test_automation_and_refresh_remain_separate_single_controller_calls() -> None:
    assert JS.count('postJson("/api/automation-cycle/run"') == 1
    assert JS.count('postJson("/api/job-library/refresh"') == 1
    automation = JS[JS.index("async function runAutomation()") :]
    automation = automation[: automation.index("function updateRunningButtons")]
    assert "/api/job-library/refresh" not in automation
    refresh = JS[JS.index("async function refreshJobs()") :]
    refresh = refresh[: refresh.index("async function runAutomation()")]
    assert "/api/automation-cycle/run" not in refresh
    assert "if (state.automating) return" in JS
    assert "automation.disabled = state.automating" in JS
    assert "Continue automatic applications" in HTML


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
