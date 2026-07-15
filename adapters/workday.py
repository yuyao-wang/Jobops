"""Purpose-built Workday authentication and application state adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from utils.keychain import (
    KeychainError,
    generate_strong_password,
    get_workday_credential,
    save_workday_credential,
)


LOGIN_EMAIL_SELECTORS = [
    '[data-automation-id="email"]',
    'input[type="email"]',
    'input[name="username"]',
    'input[name="email"]',
]
PASSWORD_SELECTORS = [
    '[data-automation-id="password"]',
    'input[type="password"]',
]
CONFIRM_EMAIL_SELECTORS = [
    '[data-automation-id="verifyEmail"]',
    'input[name*="confirmEmail" i]',
    'input[name*="verifyEmail" i]',
]
CONFIRM_PASSWORD_SELECTORS = [
    '[data-automation-id="verifyPassword"]',
    'input[name*="confirmPassword" i]',
    'input[name*="verifyPassword" i]',
]


@dataclass(frozen=True)
class WorkdayPageSignals:
    text: str
    url: str
    password_fields: int = 0
    visible_inputs: int = 0
    has_apply_button: bool = False
    has_create_account: bool = False


def classify_workday_state(signals: WorkdayPageSignals) -> str:
    """Classify a Workday page without exposing credential values."""
    text = signals.text.casefold()
    url = signals.url.casefold()

    if any(phrase in text for phrase in (
        "thank you for applying",
        "application submitted",
        "application received",
        "successfully submitted",
        "you have applied",
    )):
        return "confirmation"
    if any(phrase in text for phrase in (
        "account has been locked",
        "account is locked",
        "account may be locked",
        "too many failed",
        "wrong email or password",
        "wrong email/password",
        "password you entered isn't correct",
        "incorrect username or password",
        "unable to sign in",
    )):
        return "locked_or_rejected"
    if "captcha" in text or "recaptcha" in text or "hcaptcha" in text:
        return "captcha"
    if any(phrase in text for phrase in (
        "verification code",
        "verify your email",
        "check your email",
        "email verification",
        "we sent you a code",
    )):
        return "email_verification"
    if signals.password_fields >= 2 or "create an account" in text:
        return "register"
    if signals.password_fields == 1 and any(
        phrase in text for phrase in ("sign in", "log in", "forgot password")
    ):
        return "login"
    if any(token in url for token in ("autofillwithresume", "/apply", "application")):
        return "application"
    if signals.has_apply_button:
        return "job"
    if signals.visible_inputs > 0:
        return "application"
    return "other"


async def inspect_workday_state(page) -> tuple[str, WorkdayPageSignals]:
    """Read the current Workday stage using text and stable automation IDs."""
    try:
        body_text = await page.locator("body").inner_text(timeout=10000)
    except Exception:
        body_text = ""
    try:
        details = await page.evaluate("""() => ({
            passwordFields: Array.from(document.querySelectorAll('input[type="password"]'))
                .filter(el => el.offsetParent !== null).length,
            visibleInputs: Array.from(document.querySelectorAll('input, textarea, select'))
                .filter(el => el.offsetParent !== null && el.type !== 'hidden').length,
            hasApplyButton: !!document.querySelector('[data-automation-id="jobPostingApplyButton"]') ||
                Array.from(document.querySelectorAll('button, a')).some(el =>
                    /^apply( now)?$/i.test((el.innerText || '').trim())),
            hasCreateAccount: Array.from(document.querySelectorAll('button, a')).some(el =>
                /create (an )?account/i.test((el.innerText || '').trim())),
        })""")
    except Exception:
        details = {}
    signals = WorkdayPageSignals(
        text=body_text,
        url=page.url,
        password_fields=int(details.get("passwordFields", 0)),
        visible_inputs=int(details.get("visibleInputs", 0)),
        has_apply_button=bool(details.get("hasApplyButton", False)),
        has_create_account=bool(details.get("hasCreateAccount", False)),
    )
    return classify_workday_state(signals), signals


async def _fill_first(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=800):
                await locator.fill(value)
                return True
        except Exception:
            continue
    return False


async def _click_named(page, names: list[str]) -> bool:
    for name in names:
        try:
            locator = page.get_by_role("button", name=name, exact=False).first
            if await locator.is_visible(timeout=800):
                await locator.click()
                return True
        except Exception:
            pass
        try:
            locator = page.get_by_role("link", name=name, exact=False).first
            if await locator.is_visible(timeout=800):
                await locator.click()
                return True
        except Exception:
            pass
    return False


async def _check_terms(page) -> None:
    selectors = [
        '[data-automation-id="createAccountCheckbox"]',
        'input[type="checkbox"][required]',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=500) and not await locator.is_checked():
                await locator.check()
                return
        except Exception:
            continue


async def _fill_login(page, email: str, password: str) -> bool:
    email_ok = await _fill_first(page, LOGIN_EMAIL_SELECTORS, email)
    password_ok = await _fill_first(page, PASSWORD_SELECTORS, password)
    if not (email_ok and password_ok):
        return False
    return await _click_named(page, ["Sign In", "Log In"])


async def _fill_registration(page, email: str, password: str) -> bool:
    email_ok = await _fill_first(page, LOGIN_EMAIL_SELECTORS, email)
    await _fill_first(page, CONFIRM_EMAIL_SELECTORS, email)
    password_ok = await _fill_first(page, PASSWORD_SELECTORS, password)
    await _fill_first(page, CONFIRM_PASSWORD_SELECTORS, password)
    await _check_terms(page)
    if not (email_ok and password_ok):
        return False
    return await _click_named(page, ["Create Account", "Register"])


async def _click_apply(page) -> bool:
    try:
        button = page.locator('[data-automation-id="jobPostingApplyButton"]').first
        if await button.is_visible(timeout=1000):
            await button.click()
            return True
    except Exception:
        pass
    return await _click_named(page, ["Apply", "Apply Now"])


async def apply_workday(
    page,
    job_url: str,
    profile: dict,
    brain,
    cover_letter: str = "",
    dry_run: bool = True,
) -> bool:
    """Navigate Workday auth stages, then delegate the verified application form."""
    config = profile.get("workday", {})
    auto_login = bool(config.get("auto_login", True))
    auto_register = bool(config.get("auto_register", True))
    email = profile.get("personal", {}).get("email", "").strip()
    generated_password: str | None = None

    print("  [*] Workday adapter: loading job and detecting account state")
    await page.goto(job_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)

    for _ in range(10):
        state, signals = await inspect_workday_state(page)
        print(f"  [*] Workday stage: {state}")

        if state == "confirmation":
            return True
        if state == "captcha":
            print("  [!] Needs user: Workday CAPTCHA detected")
            return False
        if state == "email_verification":
            if generated_password:
                try:
                    save_workday_credential(job_url, email, generated_password)
                    print("  [+] New Workday credential saved to macOS Keychain")
                except (KeychainError, ValueError) as exc:
                    print(f"  [!] Could not save the Workday credential to Keychain: {exc}")
            print("  [!] Needs user: Workday email verification is required")
            return False
        if state == "locked_or_rejected":
            print("  [!] Needs user: Workday rejected the login or the account is locked")
            return False
        if state == "job":
            if not await _click_apply(page):
                print("  [!] Needs user: Workday Apply button could not be activated")
                return False
            await page.wait_for_timeout(2000)
            continue
        if state == "login":
            if not auto_login:
                print("  [!] Needs user: Workday auto-login is disabled")
                return False
            try:
                credential = get_workday_credential(job_url, email)
            except KeychainError as exc:
                print(f"  [!] Needs user: macOS Keychain lookup failed: {exc}")
                return False

            if credential:
                print("  [*] Signing in with the tenant credential from macOS Keychain")
                if not await _fill_login(page, email, credential.password):
                    print("  [!] Needs user: Workday login fields could not be completed")
                    return False
                await page.wait_for_timeout(2500)
                continue

            if auto_register and signals.has_create_account:
                print("  [*] No tenant credential found; opening Workday account registration")
                if not await _click_named(page, ["Create Account", "Create an Account"]):
                    print("  [!] Needs user: Workday account registration could not be opened")
                    return False
                await page.wait_for_timeout(1500)
                continue

            print("  [!] Needs user: no Workday credential is stored in macOS Keychain")
            return False
        if state == "register":
            if not auto_register:
                print("  [!] Needs user: Workday auto-registration is disabled")
                return False
            generated_password = generated_password or generate_strong_password(
                int(config.get("generated_password_length", 24))
            )
            print("  [*] Creating a Workday account with a generated strong password")
            if not await _fill_registration(page, email, generated_password):
                print("  [!] Needs user: Workday registration fields could not be completed")
                return False
            await page.wait_for_timeout(2500)
            new_state, _ = await inspect_workday_state(page)
            if new_state not in {"register", "login", "locked_or_rejected"}:
                try:
                    save_workday_credential(job_url, email, generated_password)
                    print("  [+] New Workday credential saved to macOS Keychain")
                except (KeychainError, ValueError) as exc:
                    print(f"  [!] Could not save the Workday credential to Keychain: {exc}")
            continue
        if state == "application":
            if generated_password:
                try:
                    save_workday_credential(job_url, email, generated_password)
                    print("  [+] New Workday credential saved to macOS Keychain")
                    generated_password = None
                except (KeychainError, ValueError) as exc:
                    print(f"  [!] Could not save the Workday credential to Keychain: {exc}")
            print("  [*] Workday application reached; resuming at autofillWithResume")
            from adapters.stagehand_adapter import apply_stagehand
            return await apply_stagehand(
                page,
                page.url,
                profile,
                brain,
                cover_letter=cover_letter,
                dry_run=dry_run,
            )

        # Workday SPAs occasionally render an empty shell before hydrating.
        await asyncio.sleep(1.5)

    print("  [!] Needs user: Workday stage did not stabilize")
    return False
