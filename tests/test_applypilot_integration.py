import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.stagehand_adapter import _fill_form_step, _handle_navigation_step
from scripts.import_applypilot import build_profile


def test_import_applypilot_builds_private_codex_profile(tmp_path: Path):
    resume = tmp_path / "general.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    candidate = {
        "candidate": {
            "legal_name": "Test Candidate",
            "email": "test@example.com",
            "phone": "555-0100",
            "current_location": "Calgary, AB",
            "linkedin_url": "",
            "github_url": "",
            "portfolio_url": "",
        },
        "current_status": {
            "current_role_or_framing": "Machine Learning Engineer",
            "available_start_date": "Immediately",
        },
        "work_authorization": {
            "requires_sponsorship_now": False,
            "requires_sponsorship_in_future": False,
            "answer_exactly_as": "Authorized to work in Canada",
        },
        "targets": {
            "primary_role_families": ["Machine Learning Engineer"],
            "secondary_role_families": [],
            "roles_to_avoid": [],
            "target_locations": ["Calgary", "Remote - Canada"],
            "relocation_policy": "Open to discussion",
        },
        "compensation": {"answer_strategy": "Discuss based on total compensation"},
        "resume_files": [{
            "role_family": "General ML",
            "file_path": str(resume),
            "source_path": "",
            "version": "test",
            "pages": 1,
            "use_when": "General applications",
        }],
        "voluntary_self_identification": {"fill_automatically": False},
    }
    (tmp_path / "candidate_profile.json").write_text(json.dumps(candidate))

    profile = build_profile(tmp_path)

    assert profile["ai"]["default_backend"] == "codex_cli"
    assert profile["auto_submission"]["low_risk_only"] is True
    assert profile["auto_submission"]["allow_ai_custom_answers"] is False
    assert profile["schedule"]["enabled"] is False
    assert Path(profile["resume_path"]).is_file()


@pytest.mark.asyncio
async def test_low_risk_live_mode_blocks_unconfirmed_custom_question():
    page = MagicMock()
    brain = MagicMock()
    profile = {
        "personal": {},
        "common_answers": {},
        "auto_submission": {"low_risk_only": True},
    }
    analysis = {
        "fields": [{
            "field_purpose": "custom",
            "role": "textbox",
            "name": "Describe a project",
            "custom_question": "Describe a project not covered by your resume",
            "required": True,
        }]
    }

    with patch(
        "adapters.stagehand_adapter._resolve_selector",
        new=AsyncMock(return_value="#custom"),
    ):
        filled, blockers = await _fill_form_step(
            page, "https://example.com", profile, brain, "", analysis, []
        )

    assert filled == 0
    assert blockers
    brain.answer_question.assert_not_called()


@pytest.mark.asyncio
async def test_submit_without_confirmation_is_not_success():
    page = MagicMock()
    form_analysis = {
        "navigation": {
            "has_submit": True,
            "submit_button_text": "Submit",
            "submit_button_selector": "#submit",
        }
    }
    with (
        patch("adapters.stagehand_adapter._click_element", new=AsyncMock(return_value=True)),
        patch("adapters.stagehand_adapter._detect_page_state", new=AsyncMock(return_value="form")),
        patch("adapters.stagehand_adapter.asyncio.sleep", new=AsyncMock()),
    ):
        result = await _handle_navigation_step(page, form_analysis, dry_run=False)

    assert result == "pending_confirmation"


@pytest.mark.asyncio
async def test_non_application_button_is_never_treated_as_submit():
    page = MagicMock()
    form_analysis = {
        "navigation": {
            "has_submit": True,
            "submit_button_text": "Search Jobs",
            "submit_button_selector": "#search",
        }
    }
    with patch(
        "adapters.stagehand_adapter._click_element",
        new=AsyncMock(return_value=True),
    ) as click:
        result = await _handle_navigation_step(page, form_analysis, dry_run=False)

    assert result == "failed"
    click.assert_not_called()
