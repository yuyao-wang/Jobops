from __future__ import annotations

import pytest
from playwright.async_api import async_playwright

from adapters.generic_ai.observer import observe_form


@pytest.mark.asyncio
async def test_apply_entry_ignores_careers_site_search_controls() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                """
                <form id="job-search">
                  <label>Search jobs <input name="keyword" required></label>
                  <button type="submit">Search Jobs</button>
                </form>
                <a role="button"
                   href="https://careers.example.invalid/jobs/42/apply">
                  Apply now
                </a>
                """
            )

            form = await observe_form(page, platform="generic", tenant="example")

            assert form.controls == ()
            assert form.next_text == "Apply now"
            assert form.next_selector
            assert form.submit_selector == ""
            assert form.submit_text == ""
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_text_only_sign_in_button_gets_unique_playwright_selector() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(
                """
                <header><button>Search</button></header>
                <main>
                  <input id="username" name="username" aria-label="Email Address:">
                  <input id="password" name="password" type="password">
                  <button>Sign In</button>
                </main>
                <footer><button>Privacy options</button></footer>
                """
            )

            form = await observe_form(page, platform="successfactors", tenant="example")

            selector = form.metadata["auth_submit_selector"]
            assert selector == 'button:has-text("Sign In")'
            assert await page.locator(selector).count() == 1
        finally:
            await browser.close()
