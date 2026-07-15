"""Persistent browser sessions for MR.Jobs automation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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

    async def close(self) -> None:
        await self.context.close()


def chromium_user_data_dir(profile: dict) -> Path:
    """Return Chromium state under an owned, repository-external Private Home.

    ``browser.chromium_user_data_dir`` is intentionally ignored. It existed in
    legacy profiles and could redirect cookies/session state into the checkout
    or a shared directory.
    """

    configured_home = str(profile.get("private_home") or "").strip()
    home = (
        PrivateHome(Path(configured_home).expanduser())
        if configured_home
        else PrivateHome.discover()
    )
    paths = home.ensure()
    return home.contained_path(paths.chromium_profile)


async def launch_browser_session(playwright, profile: dict, headless: bool = False) -> BrowserSession:
    """Launch Chromium with a persistent profile so ATS logins survive runs.

    Playwright cannot attach to or reuse Safari's cookie database.  Safari may
    still be configured as the human handoff browser, while automated form
    interaction uses this persistent Chromium context.
    """
    browser_config = profile.get("browser", {})
    user_data_dir = chromium_user_data_dir(profile)
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=headless,
        slow_mo=int(browser_config.get("slow_mo_ms", 100)),
        viewport={"width": 1920, "height": 1080},
        user_agent=browser_config.get("user_agent", DEFAULT_USER_AGENT),
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return BrowserSession(context=context, page=page, user_data_dir=user_data_dir)
