from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from adapters.stagehand_adapter import apply_smart
from adapters.workday import WorkdayPageSignals, classify_workday_state
from utils.browser_session import launch_browser_session
from utils.keychain import (
    generate_strong_password,
    get_workday_credential,
    save_workday_credential,
    workday_service,
)


WORKDAY_URL = "https://bmo.wd3.myworkdayjobs.com/External/job/example"


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (WorkdayPageSignals("Application submitted", WORKDAY_URL), "confirmation"),
        (WorkdayPageSignals("Your account is locked", WORKDAY_URL), "locked_or_rejected"),
        (
            WorkdayPageSignals("The password you entered isn't correct, or your account may be locked.", WORKDAY_URL),
            "locked_or_rejected",
        ),
        (WorkdayPageSignals("Verify your email", WORKDAY_URL), "email_verification"),
        (WorkdayPageSignals("reCAPTCHA", WORKDAY_URL), "captcha"),
        (WorkdayPageSignals("Create an Account", WORKDAY_URL, password_fields=2), "register"),
        (WorkdayPageSignals("Sign In", WORKDAY_URL, password_fields=1), "login"),
        (WorkdayPageSignals("Job details", WORKDAY_URL, has_apply_button=True), "job"),
        (
            WorkdayPageSignals("Application", WORKDAY_URL + "/autofillWithResume"),
            "application",
        ),
    ],
)
def test_classify_workday_state(signals, expected):
    assert classify_workday_state(signals) == expected


def test_generate_strong_password_has_required_classes():
    password = generate_strong_password(24)
    assert len(password) == 24
    assert any(char.isupper() for char in password)
    assert any(char.islower() for char in password)
    assert any(char.isdigit() for char in password)
    assert any(char in "!@#$%^&*_-+=" for char in password)


def test_workday_keychain_round_trip_commands(monkeypatch):
    monkeypatch.setattr("utils.keychain.platform.system", lambda: "Darwin")
    run = Mock()
    run.side_effect = [
        SimpleNamespace(returncode=0, stdout="secret-value\n", stderr=""),
        SimpleNamespace(returncode=0, stdout="", stderr=""),
    ]
    monkeypatch.setattr("utils.keychain.subprocess.run", run)

    credential = get_workday_credential(WORKDAY_URL, "person@example.com")
    service = save_workday_credential(WORKDAY_URL, "person@example.com", "secret-value")

    assert credential.password == "secret-value"
    assert credential.service == workday_service(WORKDAY_URL)
    assert service == workday_service(WORKDAY_URL)
    lookup_args = run.call_args_list[0].args[0]
    save_args = run.call_args_list[1].args[0]
    assert lookup_args[:2] == ["security", "find-generic-password"]
    assert save_args[:2] == ["security", "add-generic-password"]
    assert "secret-value" not in lookup_args
    assert save_args[-1] == "secret-value"


def test_workday_keychain_missing_item(monkeypatch):
    monkeypatch.setattr("utils.keychain.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "utils.keychain.subprocess.run",
        Mock(return_value=SimpleNamespace(returncode=44, stdout="", stderr="not found")),
    )
    assert get_workday_credential(WORKDAY_URL, "person@example.com") is None


@pytest.mark.asyncio
async def test_apply_smart_routes_workday_without_generic_fallback(monkeypatch):
    apply_workday = AsyncMock(return_value=False)
    monkeypatch.setattr("adapters.workday.apply_workday", apply_workday)
    page = SimpleNamespace()
    profile = {"personal": {"email": "person@example.com"}}

    result = await apply_smart(page, WORKDAY_URL, profile, brain=object(), dry_run=False)

    assert result is False
    apply_workday.assert_awaited_once()


@pytest.mark.asyncio
async def test_launch_browser_session_uses_persistent_context(tmp_path):
    page = object()
    context = SimpleNamespace(pages=[page], close=AsyncMock())
    launch = AsyncMock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    profile = {
        "browser": {
            "chromium_user_data_dir": str(tmp_path / "chromium"),
            "slow_mo_ms": 50,
        }
    }

    session = await launch_browser_session(playwright, profile)

    assert session.page is page
    assert session.user_data_dir == (tmp_path / "chromium").resolve()
    assert launch.await_args.kwargs["user_data_dir"] == str((tmp_path / "chromium").resolve())
    await session.close()
    context.close.assert_awaited_once()
