"""Offline contract tests for the deterministic ATS adapters.

The HTML is synthetic and sanitized.  These tests validate the adapter
protocol, not current third-party production markup.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from adapters.ashby import AshbyAdapter
from adapters.greenhouse import GreenhouseAdapter, apply_greenhouse
from adapters.jobvite import JobviteAdapter
from adapters.lever import LeverAdapter
from adapters.protocol import (
    ApplicationContext,
    FieldKind,
    PROTOCOL_VERSION,
    SubmissionEvidence,
)
from adapters.shared import canonical_key_for
from core.outcomes import EvidenceKind, OutcomeStatus, ReasonCode


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ats"
ADAPTERS = (
    ("greenhouse", GreenhouseAdapter, {"first_name", "last_name", "email", "resume"}),
    ("lever", LeverAdapter, {"full_name", "email", "resume"}),
    ("ashby", AshbyAdapter, {"full_name", "email", "resume"}),
    ("jobvite", JobviteAdapter, {"first_name", "last_name", "email", "resume"}),
)


def test_ashby_contract_includes_the_public_success_container() -> None:
    assert (
        ".ashby-application-form-success-container"
        in AshbyAdapter.confirmation_selectors
    )


@pytest.fixture
async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            pytest.skip(f"local Chromium is unavailable: {type(exc).__name__}")
        try:
            yield browser
        finally:
            await browser.close()


@pytest.fixture
async def page(browser):
    page = await browser.new_page()
    page.set_default_timeout(2_000)
    yield page
    await page.close()


@pytest.fixture
def resume_file(tmp_path: Path) -> Path:
    path = tmp_path / "sanitized-resume.pdf"
    path.write_bytes(b"%PDF-1.4\n% synthetic test fixture\n")
    return path


@pytest.fixture
def profile() -> dict:
    return {
        "personal": {
            "first_name": "Ada",
            "last_name": "Example",
            "email": "ada@example.test",
            "phone": "+1 555 0100",
            "location": "Example City",
            "linkedin": "https://linkedin.example/ada",
            "github": "https://github.example/ada",
            "portfolio": "https://portfolio.example/ada",
        }
    }


def context_for(
    page,
    name: str,
    profile: dict,
    resume_file: Path,
    *,
    answers: dict | None = None,
    request_submit: bool = False,
    permit=None,
    validator=None,
) -> ApplicationContext:
    return ApplicationContext(
        page=page,
        job_url=f"https://fixture.{name}.example/jobs/123/apply",
        job_id=f"{name}-fixture-job",
        run_id=f"{name}-fixture-run",
        profile=profile,
        resume_path=resume_file,
        answers=answers or {},
        request_submit=request_submit,
        gate_b_permit=permit,
        gate_b_validator=validator,
        navigate=False,
        settle_timeout_ms=0,
        submission_evidence_timeout_ms=100,
    )


async def load_fixture(page, name: str) -> None:
    await page.set_content((FIXTURE_DIR / f"{name}.html").read_text(encoding="utf-8"))


def accept_test_permit(permit, *, job_id, run_id, review_fingerprint) -> bool:
    return bool(
        permit == "gate-b:test-only"
        and job_id
        and run_id
        and len(review_fingerprint) == 64
    )


@pytest.mark.parametrize("name,adapter_cls,expected", ADAPTERS)
async def test_adapter_reaches_review_with_required_fields_and_upload(
    page, resume_file, profile, name, adapter_cls, expected
):
    await load_fixture(page, name)
    adapter = adapter_cls()
    context = context_for(
        page,
        name,
        profile,
        resume_file,
        answers={"work_authorization": "Yes"},
    )

    support = await adapter.support(page, context.job_url)
    form = await adapter.inspect(page)
    direct_fill = await adapter.fill(page, context, form)
    outcome = await adapter.run(context)

    assert support.supported
    assert form.protocol_version == PROTOCOL_VERSION
    assert expected <= {field.canonical_key for field in form.fields}
    assert any(field.kind is FieldKind.FILE and field.required for field in form.fields)
    assert not direct_fill.unresolved_required, direct_fill
    assert outcome.status is OutcomeStatus.REVIEW_READY
    assert outcome.reason_code is ReasonCode.REVIEW_COMPLETE
    assert expected <= set(outcome.details["review"]["filled_fields"])
    assert outcome.details["review"]["uploaded_files"] == ["resume"]
    assert len(outcome.details["review"]["readback_digest"]) == 64
    assert len(outcome.details["review"]["material_content_digest"]) == 64
    assert outcome.details["review"]["submit_control_present"] is True
    assert outcome.details["review"]["ready"] is True
    assert await page.evaluate("window.fixtureSubmitCount") == 0


async def test_review_fingerprint_binds_browser_values_and_uploaded_bytes(
    page, resume_file, profile, tmp_path
):
    await load_fixture(page, "greenhouse")
    adapter = GreenhouseAdapter()
    context = context_for(
        page,
        "greenhouse",
        profile,
        resume_file,
        answers={"work_authorization": "Yes"},
    )
    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    validation = await adapter.validate(page, form, fill)
    original = await adapter.prepare_review(page, context, form, fill, validation)
    reordered = await adapter.prepare_review(
        page,
        context,
        replace(form, fields=tuple(reversed(form.fields))),
        fill,
        validation,
    )

    assert reordered.fingerprint == original.fingerprint
    assert reordered.readback_digest == original.readback_digest
    assert reordered.material_content_digest == original.material_content_digest

    email = next(field for field in form.fields if field.canonical_key == "email")
    await page.locator(email.selectors[0]).fill("changed@example.invalid")
    changed_value = await adapter.prepare_review(page, context, form, fill, validation)

    assert changed_value.fingerprint != original.fingerprint
    assert changed_value.readback_digest != original.readback_digest
    assert changed_value.material_content_digest == original.material_content_digest
    assert changed_value.ready is False
    assert any(
        "differs from verified value for email" in item
        for item in changed_value.validation_errors
    )

    replacement = tmp_path / "replacement.pdf"
    replacement.write_bytes(b"%PDF-1.4\n% different synthetic bytes\n")
    resume = next(field for field in form.fields if field.canonical_key == "resume")
    await page.locator(resume.selectors[0]).set_input_files(str(replacement))
    changed_material = await adapter.prepare_review(page, context, form, fill, validation)

    assert changed_material.fingerprint != changed_value.fingerprint
    assert (
        changed_material.material_content_digest
        != changed_value.material_content_digest
    )
    serialized = json.dumps(changed_material.to_safe_dict())
    assert "changed@example.invalid" not in serialized
    assert str(replacement) not in serialized
    assert replacement.name not in serialized


def test_identity_classifier_rejects_third_party_contacts_and_prefers_preferred_name():
    assert canonical_key_for("Preferred First Name") == "preferred_name"
    assert canonical_key_for("Reference email address") == "unknown"
    assert canonical_key_for("Hiring manager phone number") == "unknown"
    assert canonical_key_for("Supervisor first name") == "unknown"
    assert canonical_key_for("Current salary") == "unknown"
    assert canonical_key_for("Desired salary") == "salary"
    assert canonical_key_for("Current employment status") == "employment_status"
    assert canonical_key_for("Expected graduation date") == "graduation_date"
    assert (
        canonical_key_for("Expected graduation date (MM/YYYY)")
        == "graduation_date"
    )
    assert (
        canonical_key_for(
            "Do you require a reasonable accommodation during the interview process?"
        )
        == "accommodation"
    )
    assert canonical_key_for("Employment status of spouse") == "unknown"
    assert canonical_key_for("Graduation") == "unknown"
    assert canonical_key_for("Accommodation") == "unknown"


def test_greenhouse_custom_question_classifier_maps_only_exact_known_semantics():
    assert canonical_key_for(
        "Please select your current province of residence."
    ) == "state"
    assert canonical_key_for(
        "This role will be in-office on a hybrid schedule, can you commit "
        "to being in-office three days per week?"
    ) == "office_attendance"
    assert canonical_key_for(
        "How many years of full-time experience do you have, excluding "
        "internships/co-ops?"
    ) == "full_time_experience"
    assert canonical_key_for(
        "Before seeing this job posting, how familiar were you with Faire "
        "as a company?"
    ) == "company_familiarity"
    assert canonical_key_for("How did you hear about Faire?") == (
        "job_discovery_source"
    )
    assert canonical_key_for(
        "Which of the following best describes your work authorization?"
    ) == "work_authorization_detail"
    assert canonical_key_for("How familiar are you with this technology?") == (
        "unknown"
    )
    assert canonical_key_for("How did you hear the alarm?") == "unknown"


async def test_greenhouse_boolean_answers_match_yes_no_select_and_radio(
    page, resume_file, profile
):
    await page.set_content(
        """
        <form action="https://boards.greenhouse.io/example/jobs/1">
          <label for="authorized">Are you legally authorized to work in Canada?</label>
          <select id="authorized" name="authorized" required>
            <option value="">Select</option>
            <option value="yes-value">Yes</option>
            <option value="no-value">No</option>
          </select>
          <fieldset>
            <legend>Will you now or in the future require sponsorship?</legend>
            <input id="sponsor-yes" name="sponsorship" type="radio"
                   value="yes-value" required>
            <label for="sponsor-yes">Yes</label>
            <input id="sponsor-no" name="sponsorship" type="radio"
                   value="no-value">
            <label for="sponsor-no">No</label>
          </fieldset>
          <button type="submit">Submit application</button>
        </form>
        <script>window.fixtureSubmitCount = 0;</script>
        """
    )
    adapter = GreenhouseAdapter()
    context = context_for(
        page,
        "greenhouse",
        profile,
        resume_file,
        answers={
            "work_authorization": True,
            "sponsorship": False,
        },
    )

    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    validation = await adapter.validate(page, form, fill)
    review = await adapter.prepare_review(
        page, context, form, fill, validation
    )

    assert fill.unresolved_required == ()
    assert validation.valid
    assert await page.locator("#authorized").input_value() == "yes-value"
    assert await page.locator("#sponsor-no").is_checked()
    assert review.ready


async def test_greenhouse_exact_multi_select_reaches_review(
    page, resume_file, profile
):
    await page.set_content(
        """
        <form action="https://boards.greenhouse.io/example/jobs/1">
          <select id="source" name="source" required multiple
                  aria-label="How did you hear about Faire?">
            <option value="referral">Employee referral</option>
            <option value="job-board">Job posting on LinkedIn, Indeed, or other job board</option>
          </select>
          <button type="submit">Submit application</button>
        </form>
        <script>window.fixtureSubmitCount = 0;</script>
        """
    )
    adapter = GreenhouseAdapter()
    context = context_for(
        page,
        "greenhouse",
        profile,
        resume_file,
        answers={
            "job_discovery_source": [
                "Job posting on LinkedIn, Indeed, or other job board"
            ]
        },
    )

    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    validation = await adapter.validate(page, form, fill)
    review = await adapter.prepare_review(
        page, context, form, fill, validation
    )

    assert fill.unresolved_required == ()
    assert validation.valid
    assert await page.locator("#source").input_value() == "job-board"
    assert review.ready


async def test_greenhouse_exact_checkbox_group_reaches_review(
    page, resume_file, profile
):
    await page.set_content(
        """
        <form action="https://boards.greenhouse.io/example/jobs/1">
          <fieldset>
            <legend>How did you hear about Faire?</legend>
            <input id="source-referral" name="source[]" type="checkbox"
                   value="referral" required>
            <label for="source-referral">Employee referral</label>
            <input id="source-board" name="source[]" type="checkbox"
                   value="job-board">
            <label for="source-board">Job posting on LinkedIn, Indeed, or other job board</label>
          </fieldset>
          <button type="submit">Submit application</button>
        </form>
        <script>window.fixtureSubmitCount = 0;</script>
        """
    )
    adapter = GreenhouseAdapter()
    context = context_for(
        page,
        "greenhouse",
        profile,
        resume_file,
        answers={
            "job_discovery_source": [
                "Job posting on LinkedIn, Indeed, or other job board"
            ]
        },
    )

    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    validation = await adapter.validate(page, form, fill)
    review = await adapter.prepare_review(
        page, context, form, fill, validation
    )

    assert fill.unresolved_required == ()
    assert validation.valid
    assert not await page.locator("#source-referral").is_checked()
    assert await page.locator("#source-board").is_checked()
    assert review.ready


async def test_greenhouse_modern_combobox_selects_exact_option_and_skips_validator_input(
    page, resume_file, profile
):
    await page.set_content(
        """
        <form action="https://boards.greenhouse.io/example/jobs/1">
          <div class="field-wrapper">
            <label for="question-province">Please select your current province of residence.*</label>
            <div class="select-shell">
              <input id="question-province" type="text" role="combobox"
                     aria-required="true" aria-autocomplete="list">
              <div class="select__single-value"></div>
              <input type="text" required tabindex="-1"
                     class="requiredInput">
              <div role="option" id="option-alberta">Alberta</div>
            </div>
          </div>
          <div class="file-upload" role="group" aria-required="true"></div>
          <button type="submit">Submit application</button>
        </form>
        <script>
          document.querySelector('#option-alberta').addEventListener('click', () => {
            document.querySelector('.select__single-value').textContent = 'Alberta';
            document.querySelector('#option-alberta').style.display = 'none';
          });
          window.fixtureSubmitCount = 0;
        </script>
        """
    )
    adapter = GreenhouseAdapter()
    context = context_for(
        page,
        "greenhouse",
        profile,
        resume_file,
        answers={"state": "Alberta"},
    )

    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    validation = await adapter.validate(page, form, fill)
    review = await adapter.prepare_review(
        page, context, form, fill, validation
    )

    assert len(form.fields) == 1
    assert form.fields[0].canonical_key == "state"
    assert form.fields[0].kind is FieldKind.COMBOBOX
    assert fill.unresolved_required == ()
    assert validation.valid
    assert await page.locator(".select__single-value").inner_text() == "Alberta"
    assert review.ready


async def test_greenhouse_replaced_file_input_keeps_hash_bound_review(
    page, resume_file, profile
):
    await page.set_content(
        """
        <form action="https://boards.greenhouse.io/example/jobs/1">
          <div id="resume-wrapper">
            <label for="resume">Resume*</label>
            <input id="resume" type="file" required>
          </div>
          <button type="submit">Submit application</button>
        </form>
        <script>
          document.querySelector('#resume').addEventListener('change', event => {
            const receipt = document.createElement('span');
            receipt.textContent = event.target.files[0].name;
            document.querySelector('#resume-wrapper').replaceChildren(receipt);
          });
          window.fixtureSubmitCount = 0;
        </script>
        """
    )
    adapter = GreenhouseAdapter()
    context = context_for(
        page,
        "greenhouse",
        profile,
        resume_file,
    )

    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    validation = await adapter.validate(page, form, fill)
    review = await adapter.prepare_review(
        page, context, form, fill, validation
    )

    assert await page.locator("#resume").count() == 0
    assert fill.uploaded_files == ("resume",)
    assert validation.valid
    assert review.ready
    assert len(review.material_content_digest) == 64


async def test_greenhouse_country_announcement_and_canadian_city_alias_read_back(
    page, resume_file, profile
):
    await page.set_content(
        """
        <form action="https://boards.greenhouse.io/example/jobs/1">
          <div class="field-wrapper">
            <label for="country">Country*</label>
            <div class="select-shell" id="country-shell">
              <input id="country" role="combobox" aria-required="true">
              <div class="select__single-value"></div>
              <span role="status" aria-live="polite"></span>
              <div role="option" id="country-option">Canada+1</div>
            </div>
          </div>
          <div class="field-wrapper">
            <label for="candidate-location">Location (City)*</label>
            <div class="select-shell" id="city-shell">
              <input id="candidate-location" role="combobox" aria-required="true">
              <div class="select__single-value"></div>
              <div role="option" id="city-option">Calgary, Alberta, Canada</div>
              <div role="option" id="other-city-option">Calgary, Texas, United States</div>
            </div>
          </div>
          <button type="submit">Submit application</button>
        </form>
        <script>
          document.querySelector('#country-option').addEventListener('click', () => {
            document.querySelector('#country-shell .select__single-value').textContent = '+1';
            document.querySelector('#country-shell [role=status]').textContent = 'Canada +1, 1 of 1.';
            document.querySelector('#country-option').style.display = 'none';
          });
          document.querySelector('#city-option').addEventListener('click', () => {
            document.querySelector('#city-shell .select__single-value').textContent = 'Calgary, Alberta, Canada';
            document.querySelector('#city-option').style.display = 'none';
          });
          window.fixtureSubmitCount = 0;
        </script>
        """
    )
    adapter = GreenhouseAdapter()
    context = context_for(
        page,
        "greenhouse",
        profile,
        resume_file,
        answers={
            "country": "Canada",
            "state": "Alberta",
            "city": "Calgary",
        },
    )

    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    validation = await adapter.validate(page, form, fill)
    review = await adapter.prepare_review(
        page, context, form, fill, validation
    )
    await page.locator("#country-shell [role=status]").evaluate(
        "element => { element.textContent = 'Selection available.'; }"
    )
    repeated_review = await adapter.prepare_review(
        page, context, form, fill, validation
    )

    assert fill.unresolved_required == ()
    assert validation.valid
    assert review.ready
    assert repeated_review.ready
    assert repeated_review.readback_digest == review.readback_digest
    assert repeated_review.fingerprint == review.fingerprint


def test_submission_url_and_application_id_evidence_are_digest_only():
    raw_url = "https://fixture.example/confirmation?session=private-token#secret"
    raw_id = "application-private-identifier"

    refs = SubmissionEvidence(
        confirmation_url=raw_url,
        ats_application_id=raw_id,
    ).as_evidence_refs()
    serialized = json.dumps([item.to_dict() for item in refs])

    assert raw_url not in serialized
    assert raw_id not in serialized
    assert all(item.uri is None for item in refs)
    assert all(item.sha256 and len(item.sha256) == 64 for item in refs)


@pytest.mark.parametrize("name,adapter_cls,_expected", ADAPTERS)
async def test_sensitive_required_question_stops_without_guessing(
    page, resume_file, profile, name, adapter_cls, _expected
):
    await load_fixture(page, name)

    outcome = await adapter_cls().run(context_for(page, name, profile, resume_file))

    assert outcome.status is OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER
    assert outcome.reason_code is ReasonCode.SENSITIVE_ANSWER_REQUIRED
    assert "legally authorized" in " ".join(outcome.details["review"]["unresolved_required"]).lower()
    assert await page.locator("#authorization").input_value() == ""
    assert await page.evaluate("window.fixtureSubmitCount") == 0


async def test_unknown_non_sensitive_question_stops_without_guessing(
    page, resume_file, profile
):
    await load_fixture(page, "greenhouse")
    await page.locator('label[for="authorization"]').evaluate(
        "label => label.textContent = 'What is your favorite programming language?'"
    )
    context = context_for(page, "greenhouse", profile, resume_file)

    outcome = await GreenhouseAdapter().run(context)

    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert outcome.reason_code is ReasonCode.UNKNOWN_REQUIRED_QUESTION
    assert "favorite programming language" in " ".join(
        outcome.details["review"]["unresolved_required"]
    ).lower()
    assert await page.evaluate("window.fixtureSubmitCount") == 0


@pytest.mark.parametrize("name,adapter_cls,_expected", ADAPTERS)
async def test_submit_requires_gate_b(
    page, resume_file, profile, name, adapter_cls, _expected
):
    await load_fixture(page, name)
    context = context_for(
        page,
        name,
        profile,
        resume_file,
        answers={"Are you legally authorized to work in this location?": "Yes"},
        request_submit=True,
    )

    outcome = await adapter_cls().run(context)

    assert outcome.status is OutcomeStatus.AWAITING_GATE_B
    assert outcome.reason_code is ReasonCode.GATE_B_REQUIRED
    assert outcome.checkpoint and len(outcome.checkpoint) == 64
    assert await page.evaluate("window.fixtureSubmitCount") == 0


async def test_review_requires_a_final_submit_control(
    page, resume_file, profile
):
    await load_fixture(page, "greenhouse")
    await page.locator("#submit_app").evaluate("element => element.remove()")
    context = context_for(
        page,
        "greenhouse",
        profile,
        resume_file,
        answers={"Are you legally authorized to work in this location?": "Yes"},
    )

    outcome = await GreenhouseAdapter().run(context)

    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert outcome.reason_code is ReasonCode.VALIDATION_FAILED
    assert outcome.details["review"]["submit_control_present"] is False
    assert outcome.details["review"]["ready"] is False


async def test_review_waits_for_a_transiently_unmounted_submit_control(
    page, resume_file, profile
):
    await load_fixture(page, "ashby")
    context = context_for(
        page,
        "ashby",
        profile,
        resume_file,
        answers={"Are you legally authorized to work in this location?": "Yes"},
    )
    adapter = AshbyAdapter()
    form = await adapter.inspect(page)
    fill = await adapter.fill(page, context, form)
    validation = await adapter.validate(page, form, fill)
    await page.evaluate(
        """() => {
            const original = document.querySelector(
                '.ashby-application-form-submit-button'
            );
            original.remove();
            setTimeout(() => document.body.appendChild(original), 300);
        }"""
    )

    review = await adapter.prepare_review(
        page,
        replace(context, navigate=True),
        form,
        fill,
        validation,
    )

    assert review.submit_control_present is True
    assert review.ready is True


async def test_review_outcome_does_not_expose_profile_values_or_private_paths(
    page, resume_file, profile
):
    await load_fixture(page, "greenhouse")
    outcome = await GreenhouseAdapter().run(
        context_for(
            page,
            "greenhouse",
            profile,
            resume_file,
            answers={"Are you legally authorized to work in this location?": "Yes"},
        )
    )

    payload = json.dumps(outcome.to_dict())
    assert "Ada" not in payload
    assert "ada@example.test" not in payload
    assert str(resume_file) not in payload
    assert resume_file.name not in payload


@pytest.mark.parametrize("name,adapter_cls,_expected", ADAPTERS)
async def test_explicit_confirmation_produces_verified_evidence(
    page, resume_file, profile, name, adapter_cls, _expected
):
    await load_fixture(page, name)
    context = context_for(
        page,
        name,
        profile,
        resume_file,
        answers={"Are you legally authorized to work in this location?": "Yes"},
        request_submit=True,
        permit="gate-b:test-only",
        validator=accept_test_permit,
    )

    outcome = await adapter_cls().run(context)

    assert outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
    assert outcome.reason_code is ReasonCode.SUBMISSION_CONFIRMED
    assert outcome.evidence_refs
    assert EvidenceKind.CONFIRMATION_TEXT in {item.kind for item in outcome.evidence_refs}
    assert EvidenceKind.ATS_APPLICATION_ID in {item.kind for item in outcome.evidence_refs}
    assert await page.evaluate("window.fixtureSubmitCount") == 1


async def test_lever_thanks_for_applying_is_verified_evidence(
    page, resume_file, profile
):
    await load_fixture(page, "lever")
    await page.locator('[data-jobops-confirmation="lever"]').evaluate(
        "element => { element.textContent = 'Thanks for applying!'; }"
    )
    outcome = await LeverAdapter().run(
        context_for(
            page,
            "lever",
            profile,
            resume_file,
            answers={"Are you legally authorized to work in this location?": "Yes"},
            request_submit=True,
            permit="gate-b:test-only",
            validator=accept_test_permit,
        )
    )

    assert outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
    assert await page.evaluate("window.fixtureSubmitCount") == 1


async def test_delayed_greenhouse_confirmation_is_polled_after_single_click(
    page, resume_file, profile
):
    await load_fixture(page, "greenhouse")
    await page.evaluate(
        """() => {
            const confirmation = document.querySelector(
                '[data-jobops-confirmation="greenhouse"]'
            );
            let delayed = false;
            const observer = new MutationObserver(() => {
                if (!confirmation.hidden && !delayed) {
                    delayed = true;
                    confirmation.hidden = true;
                    setTimeout(() => {
                        observer.disconnect();
                        confirmation.hidden = false;
                    }, 300);
                }
            });
            observer.observe(confirmation, {attributes: true});
        }"""
    )
    context = replace(
        context_for(
            page,
            "greenhouse",
            profile,
            resume_file,
            answers={"Are you legally authorized to work in this location?": "Yes"},
            request_submit=True,
            permit="gate-b:test-only",
            validator=accept_test_permit,
        ),
        submission_evidence_timeout_ms=1_000,
    )

    outcome = await GreenhouseAdapter().run(context)

    assert outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
    assert await page.evaluate("window.fixtureSubmitCount") == 1


@pytest.mark.parametrize("name,adapter_cls,_expected", ADAPTERS)
async def test_missing_confirmation_never_counts_as_submitted(
    page, resume_file, profile, name, adapter_cls, _expected
):
    await load_fixture(page, name)
    for selector in adapter_cls.confirmation_selectors:
        await page.locator(selector).evaluate_all("elements => elements.forEach(element => element.remove())")
    context = context_for(
        page,
        name,
        profile,
        resume_file,
        answers={"Are you legally authorized to work in this location?": "Yes"},
        request_submit=True,
        permit="gate-b:test-only",
        validator=accept_test_permit,
    )

    outcome = await adapter_cls().run(context)

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.reason_code is ReasonCode.SUBMISSION_CONFIRMATION_MISSING
    assert not outcome.evidence_refs
    assert await page.evaluate("window.fixtureSubmitCount") == 1


@pytest.mark.parametrize("name,adapter_cls,_expected", ADAPTERS)
async def test_preexisting_confirmation_never_authorizes_or_clicks_submit(
    page, resume_file, profile, name, adapter_cls, _expected
):
    await load_fixture(page, name)
    confirmation = None
    for selector in adapter_cls.confirmation_selectors:
        candidate = page.locator(selector).first
        if await candidate.count():
            confirmation = candidate
            break
    assert confirmation is not None
    await confirmation.evaluate(
        "element => { element.hidden = false; element.style.display = 'block'; }"
    )
    validator = AsyncMock(return_value=True)
    context = context_for(
        page,
        name,
        profile,
        resume_file,
        answers={"Are you legally authorized to work in this location?": "Yes"},
        request_submit=True,
        permit="gate-b:test-only",
        validator=validator,
    )

    outcome = await adapter_cls().run(context)

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.details["uncorrelated_confirmation"] is True
    assert await page.evaluate("window.fixtureSubmitCount") == 0
    validator.assert_not_awaited()


@pytest.mark.parametrize("name,adapter_cls,_expected", ADAPTERS)
async def test_duplicate_submit_is_blocked_for_same_review(
    page, resume_file, profile, name, adapter_cls, _expected
):
    await load_fixture(page, name)
    context = context_for(
        page,
        name,
        profile,
        resume_file,
        answers={"Are you legally authorized to work in this location?": "Yes"},
        request_submit=True,
        permit="gate-b:test-only",
        validator=accept_test_permit,
    )
    adapter = adapter_cls()

    first = await adapter.run(context)
    second = await adapter.run(context)

    assert first.status is OutcomeStatus.SUBMITTED_VERIFIED
    assert second.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert second.details["uncorrelated_confirmation"] is True
    assert await page.evaluate("window.fixtureSubmitCount") == 1


def test_adapter_normal_path_has_no_llm_dependency():
    modules = (GreenhouseAdapter, LeverAdapter, AshbyAdapter, JobviteAdapter)
    for adapter_cls in modules:
        source = inspect.getsource(inspect.getmodule(adapter_cls))
        assert "utils.brain" not in source
        assert "utils.llm" not in source
        assert ".ask(" not in source


async def test_legacy_greenhouse_wrapper_is_dry_run_compatible(
    page, resume_file, profile
):
    fixture_html = (FIXTURE_DIR / "greenhouse.html").read_text(encoding="utf-8")
    await page.route(
        "**/example/jobs/123",
        lambda route: route.fulfill(status=200, content_type="text/html", body=fixture_html),
    )
    legacy_profile = {
        **profile,
        "resume_path": str(resume_file),
        "common_answers": {"Are you legally authorized to work in this location?": "Yes"},
    }

    result = await apply_greenhouse(
        page,
        "https://boards.greenhouse.io/example/jobs/123",
        legacy_profile,
        brain=object(),
        dry_run=True,
    )

    assert result is True
    assert await page.evaluate("window.fixtureSubmitCount") == 0
