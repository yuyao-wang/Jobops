"""Persistent browser sessions for MR.Jobs automation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.private_home import PrivateHome


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class BrowserSession:
    context: object
    page: object
    user_data_dir: Path
    private_home: PrivateHome | None = None
    session_state_path: Path | None = None

    async def close(self) -> None:
        try:
            if self.private_home is not None and self.session_state_path is not None:
                cookies = await self.context.cookies()
                payload = json.dumps(
                    {"schema_version": 1, "cookies": cookies},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.private_home.write_text(self.session_state_path, payload)
        finally:
            await self.context.close()


def _private_home(profile: dict[str, Any]) -> PrivateHome:
    configured_home = str(profile.get("private_home") or "").strip()
    return (
        PrivateHome(Path(configured_home).expanduser())
        if configured_home
        else PrivateHome.discover()
    )


def _session_state_path(home: PrivateHome) -> Path:
    paths = home.ensure()
    return home.contained_path(paths.browser / "chromium-session-cookies.json")


def _load_session_cookies(home: PrivateHome, path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return []
    now = time.time()
    cookies: list[dict[str, Any]] = []
    for cookie in document.get("cookies") or []:
        if not isinstance(cookie, dict):
            continue
        expires = cookie.get("expires", -1)
        if isinstance(expires, (int, float)) and expires >= 0 and expires <= now:
            continue
        if not str(cookie.get("name") or "") or not str(cookie.get("domain") or ""):
            continue
        cookies.append(dict(cookie))
    return cookies


def chromium_user_data_dir(profile: dict) -> Path:
    """Return Chromium state under an owned, repository-external Private Home.

    ``browser.chromium_user_data_dir`` is intentionally ignored. It existed in
    legacy profiles and could redirect cookies/session state into the checkout
    or a shared directory.
    """

    home = _private_home(profile)
    paths = home.ensure()
    return home.contained_path(paths.chromium_profile)


async def launch_browser_session(playwright, profile: dict, headless: bool = False) -> BrowserSession:
    """Launch Chromium with a persistent profile so ATS logins survive runs.

    Playwright cannot attach to or reuse Safari's cookie database.  Safari may
    still be configured as the human handoff browser, while automated form
    interaction uses this persistent Chromium context.
    """
    browser_config = profile.get("browser", {})
    home = _private_home(profile)
    user_data_dir = chromium_user_data_dir(profile)
    session_state_path = _session_state_path(home)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=headless,
        slow_mo=int(browser_config.get("slow_mo_ms", 100)),
        viewport={"width": 1920, "height": 1080},
        user_agent=browser_config.get("user_agent", DEFAULT_USER_AGENT),
    )
    cookies = _load_session_cookies(home, session_state_path)
    if cookies:
        await context.add_cookies(cookies)
    page = context.pages[0] if context.pages else await context.new_page()
    return BrowserSession(
        context=context,
        page=page,
        user_data_dir=user_data_dir,
        private_home=home,
        session_state_path=session_state_path,
    )
