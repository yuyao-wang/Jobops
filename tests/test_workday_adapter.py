import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.stagehand_adapter import apply_smart
from adapters.workday import (
    WorkdayAdapter,
    WorkdayApplicationContext,
    WorkdayExpectedReadback,
    WorkdayField,
    WorkdayPageSignals,
    WorkdayStage,
    RegistrationFillResult,
    _check_registration_terms,
    _same_workday_session_url,
    _sanitize_confirmation_url,
    _validate_expected_readbacks,
    _workday_readback_matches,
    classify_workday_state,
    fill_workday_fields,
    inspect_workday_signals,
)
from adapters.protocol import ApplicationContext
from auth.credentials import CredentialStoreError, InMemoryCredentialStore
from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from utils.browser_session import launch_browser_session
from utils.keychain import generate_strong_password, workday_service
from tests.support.workday_fsm import FixtureWorkdayFsmPage


WORKDAY_URL = "https://exampleco.wd5.myworkdayjobs.com/External/job/example"
FIXTURES = Path(__file__).parent / "fixtures" / "workday"


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (WorkdayPageSignals("Application submitted", WORKDAY_URL), "confirmation"),
        (WorkdayPageSignals("Your account is locked", WORKDAY_URL), "account_locked"),
        (WorkdayPageSignals("Verify your email", WORKDAY_URL), "email_verification"),
        (WorkdayPageSignals("Use your authenticator app", WORKDAY_URL), "mfa"),
        (WorkdayPageSignals("reCAPTCHA", WORKDAY_URL), "captcha"),
        (
            WorkdayPageSignals(
                "Sign In. This site is protected by reCAPTCHA and the Google Privacy Policy applies.",
                WORKDAY_URL,
                password_fields=1,
            ),
            "login",
        ),
        (WorkdayPageSignals("Create an Account", WORKDAY_URL, password_fields=2), "register"),
        (WorkdayPageSignals("Sign In", WORKDAY_URL, password_fields=1), "login"),
        (WorkdayPageSignals("Job details", WORKDAY_URL, has_apply_button=True), "job"),
        (
            WorkdayPageSignals("Application", WORKDAY_URL + "/autofillWithResume"),
            "autofillWithResume",
        ),
        (WorkdayPageSignals("", WORKDAY_URL + "/myInformation"), "myInformation"),
        (WorkdayPageSignals("", WORKDAY_URL + "/myExperience"), "myExperience"),
        (WorkdayPageSignals("", WORKDAY_URL + "/applicationQuestions"), "applicationQuestions"),
        (WorkdayPageSignals("", WORKDAY_URL + "/voluntaryDisclosures"), "voluntaryDisclosures"),
        (WorkdayPageSignals("", WORKDAY_URL + "/selfIdentify"), "selfIdentify"),
        (WorkdayPageSignals("", WORKDAY_URL + "/review"), "review"),
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


def test_workday_service_is_tenant_scoped_and_uses_jobops_namespace():
    assert workday_service(WORKDAY_URL) == "jobops.workday.exampleco.wd5.myworkdayjobs.com"


def test_workday_keychain_service_rejects_lookalike_host():
    with pytest.raises(ValueError, match="valid Workday URL"):
        workday_service("https://workday.evil.example/jobs/1")


class FakeInput:
    def __init__(self):
        self.first = self
        self.value = None

    async def fill(self, value):
        self.value = value

    async def evaluate(self, _script):
        return {"value": self.value or ""}


class FakeFormPage:
    def __init__(self):
        self.url = WORKDAY_URL + "/myInformation"
        self.inputs = {}

    def locator(self, selector):
        return self.inputs.setdefault(selector, FakeInput())


@pytest.mark.asyncio
async def test_fill_workday_fields_uses_confirmed_profile_without_model():
    page = FakeFormPage()
    context = WorkdayApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile={"personal": {"first_name": "Test"}},
        job_id="job-1",
        run_id="run-1",
    )
    fields = (
        WorkdayField(
            selector='[data-automation-id="legalNameSection_firstName"]',
            automation_id="legalNameSection_firstName",
            name="",
            label="First Name",
            kind="text",
            required=True,
        ),
    )

    report = await fill_workday_fields(context, WorkdayStage.MY_INFORMATION, fields)

    assert report.filled == 1
    assert not report.unresolved
    assert page.inputs[fields[0].selector].value == "Test"


@pytest.mark.asyncio
async def test_preferred_first_name_never_uses_legal_first_name():
    page = FakeFormPage()
    context = WorkdayApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile={
            "personal": {
                "first_name": "Legal Synthetic",
                "preferred_name": "Preferred Synthetic",
            }
        },
        job_id="job-preferred",
        run_id="run-preferred",
    )
    field = WorkdayField(
        selector='[data-automation-id="preferredNameSection_firstName"]',
        automation_id="preferredNameSection_firstName",
        name="preferredFirstName",
        label="Preferred First Name",
        kind="text",
        required=True,
    )

    report = await fill_workday_fields(
        context, WorkdayStage.MY_INFORMATION, (field,)
    )

    assert report.filled_fields == ("preferred_name",)
    assert page.inputs[field.selector].value == "Preferred Synthetic"


class FakeTermsLocator:
    def __init__(self, checked=False):
        self.first = self
        self.checked = checked
        self.check_calls = 0

    async def is_checked(self):
        return self.checked

    async def check(self):
        self.checked = True
        self.check_calls += 1


class FakeTermsPage:
    def __init__(self, items):
        self.items = items
        self.locators = {}

    async def evaluate(self, _script):
        return self.items

    def locator(self, selector):
        return self.locators.setdefault(selector, FakeTermsLocator())


@pytest.mark.asyncio
async def test_registration_checks_only_versioned_known_terms():
    selector = '[data-automation-id="createAccountCheckbox"]'
    page = FakeTermsPage([{
        "automationId": "createAccountCheckbox",
        "label": "I agree to the Terms and Conditions",
        "selector": selector,
    }])

    unresolved = await _check_registration_terms(page)

    assert unresolved == ()
    assert page.locators[selector].check_calls == 1


@pytest.mark.asyncio
async def test_registration_never_checks_unknown_required_checkbox():
    selector = "#unknown-required-consent"
    page = FakeTermsPage([{
        "automationId": "customConsent",
        "label": "Allow unrelated marketing and data sharing",
        "selector": selector,
    }])

    unresolved = await _check_registration_terms(page)

    assert unresolved == ("Allow unrelated marketing and data sharing",)
    assert selector not in page.locators


@pytest.mark.asyncio
async def test_unknown_registration_agreement_stops_before_account_creation(
    monkeypatch,
):
    store = InMemoryCredentialStore()
    context = WorkdayApplicationContext(
        page=object(),
        job_url=WORKDAY_URL,
        profile={"personal": {"email": "candidate@example.test"}},
        job_id="job-register-unknown",
        run_id="run-register-unknown",
        credential_store=store,
    )
    prepare = AsyncMock(return_value=RegistrationFillResult(
        fields_ready=True,
        unresolved_required=("Unknown required agreement",),
    ))
    create = AsyncMock(side_effect=AssertionError("must not create account"))
    monkeypatch.setattr("adapters.workday._fill_registration", prepare)
    monkeypatch.setattr("adapters.workday._click_named", create)

    outcome, _password = await WorkdayAdapter()._register(context, None)

    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert outcome.reason_code is ReasonCode.UNKNOWN_REQUIRED_QUESTION
    assert outcome.details["terms_ruleset"] == "workday-registration-terms/v1"
    create.assert_not_awaited()
    assert store.get(
        workday_service(WORKDAY_URL), "candidate@example.test"
    ) is None


@pytest.mark.asyncio
async def test_failed_registration_restores_preexisting_keychain_credential(
    monkeypatch,
):
    store = InMemoryCredentialStore()
    service = workday_service(WORKDAY_URL)
    store.set(service, "candidate@example.test", "synthetic-existing-secret")
    context = WorkdayApplicationContext(
        page=object(),
        job_url=WORKDAY_URL,
        profile={"personal": {"email": "candidate@example.test"}},
        job_id="job-register-restore",
        run_id="run-register-restore",
        credential_store=store,
    )
    prepare = AsyncMock(
        return_value=RegistrationFillResult(
            fields_ready=True,
            unresolved_required=("Synthetic unknown agreement",),
        )
    )
    create = AsyncMock(side_effect=AssertionError("must not create account"))
    monkeypatch.setattr("adapters.workday._fill_registration", prepare)
    monkeypatch.setattr("adapters.workday._click_named", create)

    outcome, _password = await WorkdayAdapter()._register(context, None)

    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert store.get(service, "candidate@example.test") == "synthetic-existing-secret"
    create.assert_not_awaited()


class FailingCredentialStore:
    def get(self, _service, _account):
        return None

    def set(self, _service, _account, _secret):
        raise CredentialStoreError("synthetic backend failure")

    def delete(self, _service, _account):
        return False


@pytest.mark.asyncio
async def test_generated_password_store_failure_hands_off_before_form_use(
    monkeypatch,
):
    context = WorkdayApplicationContext(
        page=object(),
        job_url=WORKDAY_URL,
        profile={"personal": {"email": "candidate@example.test"}},
        job_id="job-register-store-failure",
        run_id="run-register-store-failure",
        credential_store=FailingCredentialStore(),
    )
    prepare = AsyncMock(side_effect=AssertionError("password must not enter form"))
    create = AsyncMock(side_effect=AssertionError("must not create account"))
    monkeypatch.setattr("adapters.workday._fill_registration", prepare)
    monkeypatch.setattr("adapters.workday._click_named", create)

    outcome, _password = await WorkdayAdapter()._register(context, None)

    assert outcome.status is OutcomeStatus.NEEDS_USER_LOGIN
    assert outcome.details["credential_store"] == "write_failed"
    prepare.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_sensitive_question_is_not_guessed():
    context = WorkdayApplicationContext(
        page=FakeFormPage(),
        job_url=WORKDAY_URL,
        profile={},
        job_id="job-1",
        run_id="run-1",
    )
    field = WorkdayField(
        selector="#gender",
        automation_id="gender",
        name="gender",
        label="Gender",
        kind="text",
        required=True,
    )

    report = await fill_workday_fields(
        context, WorkdayStage.VOLUNTARY_DISCLOSURES, (field,)
    )

    assert report.filled == 0
    assert report.sensitive_unresolved == ("Gender",)


class ConfirmationPage:
    def __init__(self, url=None):
        self.url = url or WORKDAY_URL + "/confirmation"

    async def evaluate(self, _script):
        return {
            "text": "Thank you for applying. Application submitted.",
            "heading": "Application submitted",
            "passwordFields": 0,
            "visibleInputs": 0,
            "hasApplyButton": False,
            "hasCreateAccount": False,
            "automationIds": ["applicationSubmitted"],
        }

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_uncorrelated_confirmation_is_never_counted_as_this_run_submission():
    context = WorkdayApplicationContext(
        page=ConfirmationPage(),
        job_url=WORKDAY_URL,
        profile={},
        job_id="job-1",
        run_id="run-1",
    )

    outcome = await WorkdayAdapter().run(context)

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert not outcome.evidence_refs
    assert outcome.phase is OutcomePhase.VERIFY
    assert outcome.details["do_not_count_as_submission"] is True


@pytest.mark.asyncio
async def test_protocol_confirmation_evidence_requires_prior_run_click_correlation():
    page = ConfirmationPage()
    context = ApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile={},
        job_id="job-correlated",
        run_id="run-correlated",
        navigate=False,
    )
    adapter = WorkdayAdapter()

    before_click = await adapter.verify_submission(page, context)
    assert not before_click.confirmation_text
    assert not before_click.confirmation_url

    adapter._submit_correlations.add((context.run_id, context.job_id))
    after_click = await adapter.verify_submission(page, context)
    assert after_click.confirmation_text
    assert after_click.confirmation_url.endswith("/confirmation")


@pytest.mark.asyncio
async def test_apply_smart_routes_workday_without_generic_fallback(monkeypatch):
    run_workday = AsyncMock(return_value=ApplicationOutcome.needs_user(
        run_id="routed-run",
        job_id="routed-job",
        status=OutcomeStatus.NEEDS_USER_LOGIN,
        phase=OutcomePhase.AUTHENTICATE,
        reason_code=ReasonCode.LOGIN_REQUIRED,
        message="synthetic login handoff",
        adapter="workday",
    ))
    monkeypatch.setattr("adapters.workday.WorkdayAdapter.run", run_workday)
    page = SimpleNamespace()
    profile = {"personal": {"email": "candidate@example.test"}}

    result = await apply_smart(page, WORKDAY_URL, profile, brain=object(), dry_run=False)

    assert result is False
    run_workday.assert_awaited_once()


@pytest.mark.asyncio
async def test_launch_browser_session_uses_persistent_context(tmp_path):
    page = object()
    context = SimpleNamespace(
        pages=[page],
        close=AsyncMock(),
        cookies=AsyncMock(return_value=[]),
        add_cookies=AsyncMock(),
    )
    launch = AsyncMock(return_value=context)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    profile = {
        "private_home": str(tmp_path / "private-home"),
        "browser": {
            "chromium_user_data_dir": str(tmp_path / "escaped-chromium"),
            "slow_mo_ms": 50,
        }
    }

    session = await launch_browser_session(playwright, profile)

    assert session.page is page
    expected = (tmp_path / "private-home" / "browser" / "chromium").resolve()
    assert session.user_data_dir == expected
    assert launch.await_args.kwargs["user_data_dir"] == str(expected)
    assert not (tmp_path / "escaped-chromium").exists()
    await session.close()
    context.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_session_persists_and_restores_session_cookies(tmp_path):
    private_home = tmp_path / "private-home"
    profile = {"private_home": str(private_home), "browser": {}}
    session_cookie = {
        "name": "synthetic-session",
        "value": "private-cookie-value",
        "domain": "career.example.test",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    }
    first_context = SimpleNamespace(
        pages=[object()],
        close=AsyncMock(),
        cookies=AsyncMock(return_value=[session_cookie]),
        add_cookies=AsyncMock(),
    )
    second_context = SimpleNamespace(
        pages=[object()],
        close=AsyncMock(),
        cookies=AsyncMock(return_value=[session_cookie]),
        add_cookies=AsyncMock(),
    )
    launch = AsyncMock(side_effect=[first_context, second_context])
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=launch)
    )

    first = await launch_browser_session(playwright, profile)
    await first.close()
    state_path = private_home / "browser" / "chromium-session-cookies.json"
    assert state_path.is_file()
    assert state_path.stat().st_mode & 0o777 == 0o600

    second = await launch_browser_session(playwright, profile)
    second_context.add_cookies.assert_awaited_once_with([session_cookie])
    await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("job.html", WorkdayStage.JOB),
        ("login.html", WorkdayStage.LOGIN),
        ("register.html", WorkdayStage.REGISTER),
        ("email_verification.html", WorkdayStage.EMAIL_VERIFICATION),
        ("autofill_with_resume.html", WorkdayStage.AUTOFILL_WITH_RESUME),
        ("my_information.html", WorkdayStage.MY_INFORMATION),
        ("my_experience.html", WorkdayStage.MY_EXPERIENCE),
        ("application_questions.html", WorkdayStage.APPLICATION_QUESTIONS),
        ("voluntary_disclosures.html", WorkdayStage.VOLUNTARY_DISCLOSURES),
        ("self_identify.html", WorkdayStage.SELF_IDENTIFY),
        ("review.html", WorkdayStage.REVIEW),
        ("confirmation.html", WorkdayStage.CONFIRMATION),
    ],
)
async def test_synthetic_html_contract_with_playwright(fixture_name, expected):
    playwright_module = pytest.importorskip("playwright.async_api")
    async with playwright_module.async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {type(exc).__name__}")
        try:
            page = await browser.new_page()
            await page.set_content((FIXTURES / fixture_name).read_text())
            signals = await inspect_workday_signals(page)
            assert classify_workday_state(signals) == expected.value
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_review_requires_gate_b_and_submits_exactly_once_with_permit():
    playwright_module = pytest.importorskip("playwright.async_api")
    async with playwright_module.async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {type(exc).__name__}")
        try:
            page = await browser.new_page()
            markup = (FIXTURES / "review.html").read_text()
            await page.set_content(markup)
            base = dict(
                page=page,
                job_url=WORKDAY_URL,
                profile={},
                job_id="job-review",
                run_id="run-review",
                navigate=False,
                request_submit=True,
            )

            awaiting = await WorkdayAdapter().run(WorkdayApplicationContext(**base))
            assert awaiting.status is OutcomeStatus.AWAITING_GATE_B
            assert await page.evaluate("window.submitCount || 0") == 0

            submitted = await WorkdayAdapter().run(WorkdayApplicationContext(
                **base,
                gate_b_permit="opaque-test-permit",
                gate_b_validator=lambda *_args, **_kwargs: True,
            ))
            assert submitted.status is OutcomeStatus.SUBMITTED_VERIFIED
            assert submitted.evidence_refs
            assert await page.evaluate("window.submitCount") == 1
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_workday_protocol_fills_native_and_aria_comboboxes_without_model():
    playwright_module = pytest.importorskip("playwright.async_api")
    async with playwright_module.async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"Playwright Chromium is unavailable: {type(exc).__name__}")
        try:
            page = await browser.new_page()
            await page.set_content((FIXTURES / "application_questions.html").read_text())
            adapter = WorkdayAdapter()
            context = ApplicationContext(
                page=page,
                job_url=WORKDAY_URL,
                profile={"personal": {"country": "Canada"}},
                answers={"Are you legally authorized to work in this country?": "Yes"},
                job_id="job-combobox",
                run_id="run-combobox",
                navigate=False,
            )

            form = await adapter.inspect(page)
            fill = await adapter.fill(page, context, form)
            validation = await adapter.validate(page, form, fill)

            assert validation.valid
            assert "country" in fill.filled_fields
            assert await page.locator('[role="combobox"]').inner_text() == "Canada"
            assert await page.locator('[data-automation-id="workAuthorization"]').input_value() == "yes"
        finally:
            await browser.close()


def test_workday_resume_requires_the_exact_requested_posting_identity():
    job_a = WORKDAY_URL + "-a"
    job_b = WORKDAY_URL + "-b"

    assert _same_workday_session_url(job_b + "/review", job_b)
    assert not _same_workday_session_url(job_a + "/review", job_b)
    assert not _same_workday_session_url(
        "https://exampleco.wd5.myworkdayjobs.com/External/candidate/review",
        job_b,
    )


class StaticSignalsPage:
    def __init__(self, url, *, posting_urls=()):
        self.url = url
        self.posting_urls = tuple(posting_urls)

    async def evaluate(self, _script):
        return {
            "text": "Review Your Application",
            "heading": "Review Your Application",
            "passwordFields": 0,
            "visibleInputs": 0,
            "hasApplyButton": False,
            "hasCreateAccount": False,
            "automationIds": ["reviewPage"],
            "postingUrls": list(self.posting_urls),
        }

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_url",
    [
        WORKDAY_URL + "-stale/review",
        "https://exampleco.wd5.myworkdayjobs.com/External/candidate/review",
    ],
)
async def test_workday_run_fails_closed_for_stale_or_unbound_review(active_url):
    context = WorkdayApplicationContext(
        page=StaticSignalsPage(active_url),
        job_url=WORKDAY_URL + "-requested",
        profile={},
        job_id="job-requested",
        run_id="run-requested",
        navigate=False,
    )

    outcome = await WorkdayAdapter().run(context)

    assert outcome.status is OutcomeStatus.FAILED_TERMINAL
    assert outcome.phase is OutcomePhase.INSPECT
    assert outcome.reason_code is ReasonCode.VALIDATION_FAILED
    assert outcome.checkpoint == "workday.identity"
    assert active_url not in outcome.to_json()


def test_confirmation_url_sanitizer_drops_userinfo_query_and_fragment():
    dirty = (
        "https://synthetic-user:synthetic-pass@"
        "exampleco.wd5.myworkdayjobs.com/External/job/example/confirmation"
        "?access_token=synthetic-secret#session-secret"
    )

    sanitized = _sanitize_confirmation_url(dirty)

    assert sanitized == WORKDAY_URL + "/confirmation"
    assert "synthetic-user" not in sanitized
    assert "synthetic-pass" not in sanitized
    assert "access_token" not in sanitized
    assert "session-secret" not in sanitized


@pytest.mark.asyncio
async def test_workday_confirmation_outcome_never_contains_url_tokens():
    dirty_url = (
        WORKDAY_URL
        + "/confirmation?access_token=synthetic-secret#session-secret"
    )
    page = ConfirmationPage(dirty_url)
    context = WorkdayApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile={},
        job_id="job-confirmation-sanitized",
        run_id="run-confirmation-sanitized",
        navigate=False,
    )
    adapter = WorkdayAdapter()
    adapter._submit_correlations.add((context.run_id, context.job_id))

    outcome = await adapter.run(context)

    assert outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
    url_evidence = [
        item for item in outcome.evidence_refs
        if item.kind is EvidenceKind.CONFIRMATION_URL
    ]
    assert len(url_evidence) == 1
    assert url_evidence[0].uri is None
    assert url_evidence[0].sha256 == hashlib.sha256(
        (WORKDAY_URL + "/confirmation").encode("utf-8")
    ).hexdigest()
    assert "synthetic-secret" not in outcome.to_json()
    assert "access_token" not in outcome.to_json()
    assert "session-secret" not in outcome.to_json()


class ExactReadbackLocator:
    def __init__(self, state):
        self.first = self
        self.state = dict(state)

    async def count(self):
        return 1

    async def evaluate(self, _script):
        return dict(self.state)


class ExactReadbackPage:
    def __init__(self, states):
        self.states = {
            selector: ExactReadbackLocator(state)
            for selector, state in states.items()
        }

    def locator(self, selector):
        return self.states.get(selector, ExactReadbackLocator({}))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_value", "matching_state", "mismatching_state"),
    [
        (
            "text",
            "Synthetic Candidate",
            {"value": "Synthetic Candidate"},
            {"value": "Different Candidate"},
        ),
        (
            "select",
            "Canada",
            {"selectedText": "Canada", "selectedValue": "CA"},
            {"selectedText": "United States", "selectedValue": "US"},
        ),
        (
            "radio",
            "Yes",
            {"groupChecked": True, "groupValue": "yes", "groupLabel": "Yes"},
            {"groupChecked": True, "groupValue": "no", "groupLabel": "No"},
        ),
        (
            "checkbox",
            False,
            {"checked": False},
            {"checked": True},
        ),
    ],
)
async def test_workday_exact_readback_is_type_aware(
    kind, expected_value, matching_state, mismatching_state
):
    selector = "#synthetic-control"
    binding = WorkdayExpectedReadback(
        selector=selector,
        canonical_key="synthetic",
        label="Synthetic control",
        kind=kind,
        expected_value=expected_value,
    )

    assert await _workday_readback_matches(
        ExactReadbackPage({selector: matching_state}), binding
    )
    assert not await _workday_readback_matches(
        ExactReadbackPage({selector: mismatching_state}), binding
    )
    assert str(expected_value) not in repr(binding)


@pytest.mark.asyncio
async def test_workday_file_readback_compares_uploaded_bytes(tmp_path):
    expected_bytes = b"synthetic resume bytes\x00\x01"
    resume = tmp_path / "synthetic-resume.pdf"
    resume.write_bytes(expected_bytes)
    selector = "#resume"
    binding = WorkdayExpectedReadback(
        selector=selector,
        canonical_key="resume",
        label="Resume",
        kind="file",
        expected_value=str(resume),
    )
    matching = {
        "fileContents": [base64.b64encode(expected_bytes).decode("ascii")]
    }
    mismatching = {
        "fileContents": [base64.b64encode(b"different bytes").decode("ascii")]
    }

    assert await _workday_readback_matches(
        ExactReadbackPage({selector: matching}), binding
    )
    assert not await _workday_readback_matches(
        ExactReadbackPage({selector: mismatching}), binding
    )


class TamperingInput(FakeInput):
    async def fill(self, _value):
        self.value = "tampered value"


class TamperingFormPage(FakeFormPage):
    def locator(self, selector):
        return self.inputs.setdefault(selector, TamperingInput())


@pytest.mark.asyncio
async def test_workday_does_not_advance_after_exact_readback_mismatch(monkeypatch):
    field = WorkdayField(
        selector='[data-automation-id="legalNameSection_firstName"]',
        automation_id="legalNameSection_firstName",
        name="firstName",
        label="First Name",
        kind="text",
        required=True,
    )
    page = TamperingFormPage()
    context = WorkdayApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile={"personal": {"first_name": "Synthetic"}},
        job_id="job-readback-mismatch",
        run_id="run-readback-mismatch",
        navigate=False,
    )
    monkeypatch.setattr(
        "adapters.workday.inspect_workday_fields", AsyncMock(return_value=(field,))
    )
    click_next = AsyncMock(side_effect=AssertionError("must not advance"))
    monkeypatch.setattr("adapters.workday._click_next", click_next)

    outcome = await WorkdayAdapter()._complete_stage(
        context, WorkdayStage.MY_INFORMATION
    )

    assert outcome is not None
    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert outcome.phase is OutcomePhase.VALIDATE
    assert outcome.reason_code is ReasonCode.VALIDATION_FAILED
    assert outcome.details["mismatched_labels"] == ["First Name"]
    click_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_workday_protocol_revalidates_exact_values_after_fill(monkeypatch):
    field = WorkdayField(
        selector='[data-automation-id="legalNameSection_firstName"]',
        automation_id="legalNameSection_firstName",
        name="firstName",
        label="First Name",
        kind="text",
        required=True,
    )
    monkeypatch.setattr(
        "adapters.workday.inspect_workday_fields", AsyncMock(return_value=(field,))
    )
    page = FakeFormPage()
    context = ApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile={"personal": {"first_name": "Synthetic"}},
        job_id="job-protocol-readback",
        run_id="run-protocol-readback",
        navigate=False,
    )
    adapter = WorkdayAdapter()
    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    page.inputs[field.selector].value = "tampered after fill"

    validation = await adapter.validate(page, form, fill)

    assert not validation.valid
    assert validation.missing_required == ("First Name",)


@pytest.mark.asyncio
async def test_workday_review_revalidates_stored_exact_values(monkeypatch):
    selector = "#synthetic-first-name"
    page = ExactReadbackPage({selector: {"value": "tampered value"}})
    context = WorkdayApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile={},
        job_id="job-review-readback",
        run_id="run-review-readback",
        navigate=False,
    )
    adapter = WorkdayAdapter()
    adapter._remember_expected_readbacks(
        context,
        (
            WorkdayExpectedReadback(
                selector=selector,
                canonical_key="first_name",
                label="First Name",
                kind="text",
                expected_value="Synthetic",
            ),
        ),
    )
    monkeypatch.setattr(
        "adapters.workday.inspect_workday_fields", AsyncMock(return_value=())
    )

    outcome = await adapter._review_or_submit(context)

    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert outcome.phase is OutcomePhase.VALIDATE
    assert outcome.reason_code is ReasonCode.VALIDATION_FAILED
    assert outcome.details["mismatched_labels"] == ["First Name"]


@pytest.mark.asyncio
async def test_resumed_review_can_be_reported_but_not_submitted_without_bindings(
    monkeypatch,
):
    monkeypatch.setattr(
        "adapters.workday.inspect_workday_fields", AsyncMock(return_value=())
    )
    monkeypatch.setattr(
        "adapters.workday.workday_review_fingerprint",
        AsyncMock(return_value="a" * 64),
    )
    adapter = WorkdayAdapter()
    base = dict(
        page=SimpleNamespace(),
        job_url=WORKDAY_URL,
        profile={"personal": {"first_name": "Synthetic"}},
        job_id="job-resumed-review",
        run_id="run-resumed-review",
        navigate=False,
    )

    review = await adapter._review_or_submit(
        WorkdayApplicationContext(**base, request_submit=False)
    )
    blocked_submit = await adapter._review_or_submit(
        WorkdayApplicationContext(**base, request_submit=True)
    )

    assert review.status is OutcomeStatus.REVIEW_READY
    assert review.details["exact_readback_bindings"] is False
    assert review.details["resumed_at_review"] is True
    assert blocked_submit.status is OutcomeStatus.NEEDS_USER
    assert blocked_submit.reason_code is ReasonCode.VALIDATION_FAILED


@pytest.mark.asyncio
async def test_persisted_review_attestation_allows_safe_cross_process_gate_b(
    monkeypatch,
):
    selector = "#synthetic-first-name"
    page = ExactReadbackPage({selector: {"value": "Synthetic"}})
    profile = {"personal": {"first_name": "Synthetic"}}
    monkeypatch.setattr(
        "adapters.workday.inspect_workday_fields", AsyncMock(return_value=())
    )
    monkeypatch.setattr(
        "adapters.workday._workday_review_surface_digest",
        AsyncMock(return_value="d" * 64),
    )
    monkeypatch.setattr(
        "adapters.workday.workday_review_fingerprint",
        AsyncMock(return_value="a" * 64),
    )

    initial = WorkdayAdapter()
    initial_context = WorkdayApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile=profile,
        job_id="job-attested-review",
        run_id="run-attested-review",
        navigate=False,
    )
    initial._remember_expected_readbacks(
        initial_context,
        (
            WorkdayExpectedReadback(
                selector=selector,
                canonical_key="first_name",
                label="First Name",
                kind="text",
                expected_value="Synthetic",
            ),
        ),
    )

    review = await initial._review_or_submit(initial_context)
    attestation = review.details["workday_binding_attestation"]

    assert review.status is OutcomeStatus.REVIEW_READY
    assert len(attestation) == 64
    assert "Synthetic" not in review.to_json()

    validator = AsyncMock(return_value=False)
    resumed_context = WorkdayApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile=profile,
        job_id="job-attested-review",
        run_id="run-attested-review",
        navigate=False,
        request_submit=True,
        gate_b_permit="opaque-test-permit",
        gate_b_validator=validator,
        persisted_review_attestation=attestation,
    )
    resumed = await WorkdayAdapter()._review_or_submit(resumed_context)

    assert resumed.status is OutcomeStatus.AWAITING_GATE_B
    validator.assert_awaited_once()

    changed_validator = AsyncMock(return_value=True)
    changed_context = WorkdayApplicationContext(
        page=page,
        job_url=WORKDAY_URL,
        profile={"personal": {"first_name": "Changed"}},
        job_id="job-attested-review",
        run_id="run-attested-review",
        navigate=False,
        request_submit=True,
        gate_b_permit="opaque-test-permit",
        gate_b_validator=changed_validator,
        persisted_review_attestation=attestation,
    )
    changed = await WorkdayAdapter()._review_or_submit(changed_context)

    assert changed.status is OutcomeStatus.NEEDS_USER
    assert changed.reason_code is ReasonCode.VALIDATION_FAILED
    changed_validator.assert_not_awaited()


@pytest.mark.asyncio
async def test_sanitized_workday_fixture_reaches_review_through_multiple_stages(
    tmp_path: Path,
):
    fixture = json.loads((FIXTURES / "multi_stage_fsm.json").read_text())
    page = FixtureWorkdayFsmPage(fixture)
    resume = tmp_path / "synthetic-resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% synthetic\n")
    question = "Why are you interested in this synthetic role?"
    context = WorkdayApplicationContext(
        page=page,
        job_url=fixture["posting_url"],
        profile={
            "personal": {
                "first_name": "Synthetic",
                "last_name": "Candidate",
            }
        },
        answers={
            question: "A verified synthetic answer.",
            "gender": "Prefer not to disclose",
            "disability_status": "Prefer not to disclose",
        },
        resume_path=str(resume),
        job_id="job-fixture-fsm",
        run_id="run-fixture-fsm",
        navigate=False,
        request_submit=False,
    )

    outcome = await WorkdayAdapter().run(context)

    assert outcome.status is OutcomeStatus.REVIEW_READY
    assert outcome.checkpoint == "workday.review"
    assert outcome.details["exact_readback_bindings"] is True
    assert outcome.details["resumed_at_review"] is False
    assert page.uploaded_file_count == 1
    assert page.next_clicks == len(fixture["stages"]) - 1
    assert page.stage_index == len(fixture["stages"]) - 1
