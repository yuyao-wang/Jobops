from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "browser_extension" / "jobops_web_clipper"


def test_web_clipper_has_current_tab_only_permissions() -> None:
    manifest = json.loads(
        (EXTENSION / "manifest.json").read_text(encoding="utf-8")
    )

    assert manifest["manifest_version"] == 3
    assert set(manifest["permissions"]) == {"activeTab", "scripting"}
    assert "host_permissions" not in manifest
    assert manifest["background"]["service_worker"] == "background.js"


def test_web_clipper_handoff_never_scrapes_or_calls_platforms() -> None:
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    dashboard = (ROOT / "dashboard" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert "chrome.action.onClicked" in background
    assert "window.getSelection" in background
    assert "chrome.tabs.create" in background
    assert 'JOBOPS_DASHBOARD = "http://127.0.0.1:8080/"' in background
    assert "#jobops-clip=${handoff}" in background
    assert "fetch(" not in background
    assert "querySelectorAll" not in background
    assert "user_gesture: true" in dashboard
    assert 'postJson("/api/job-leads/capture"' in dashboard
    assert "history.replaceState" in dashboard


def test_dashboard_requires_explicit_clipper_confirmation() -> None:
    template = (ROOT / "dashboard" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    dashboard = (ROOT / "dashboard" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert 'id="job-clipper-dialog"' in template
    assert 'id="save-job-clip"' in template
    assert 'id="cancel-job-clip"' in template
    assert "Save this current page?" in template
    assert "addEventListener(\"click\", saveJobClip)" in dashboard
    assert "addEventListener(\"click\", cancelJobClip)" in dashboard
