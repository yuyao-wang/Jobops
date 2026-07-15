"""Safety and token-budget contracts for the modular generic ATS adapter.

These tests deliberately use synthetic values.  They assert that the generic
path observes form *structure*, resolves verified values locally, and stops at
Review unless a separately validated submit permit is present.
"""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adapters.generic_ai.adapter import GenericAIAdapter
from adapters.generic_ai.cache import RecipeAction, RecipeCache
from adapters.generic_ai.executor import (
    FillReport,
    click_submit,
    execute_field,
    execute_resolved_fields,
)
from adapters.generic_ai.fingerprinter import fingerprint_form, fingerprint_review
from adapters.generic_ai.models import FormControl, FormIR, FormOption
from adapters.generic_ai.observer import observe_form
from adapters.generic_ai.resolver import (
    AnswerResolver,
    ResolvedField,
    Sensitivity,
    map_unknown_controls,
)
from adapters.generic_ai.verifier import (
    SubmissionEvidence,
    VerificationReport,
    detect_submission_evidence,
    is_confirmation_text,
    is_review_ready,
    verify_fields,
)
from core.outcomes import EvidenceKind, OutcomeStatus, ReasonCode
from core.event_ledger import (
    DuplicateSubmissionError,
    EventLedger,
    SubmissionStatus,
    hash_job_url,
)
from core.permits import PermitBindings, PermitError, PermitGate, PermitService


SYNTHETIC_EMAIL = "candidate-9384@example.invalid"
SYNTHETIC_PHONE = "+1 555 010 9384"


def _control(
    *,
    index: int = 0,
    label: str = "Email",
    required: bool = True,
    selector: str = "#email",
    element_id: str = "email",
    autocomplete: str = "email",
    options: tuple[FormOption, ...] = (),
) -> FormControl:
    return FormControl(
        index=index,
        role="textbox",
        tag="input",
        input_type="email",
        label=label,
        name="email",
        element_id=element_id,
        autocomplete=autocomplete,
        required=required,
        selector=selector,
        options=options,
    )


def _form(control: FormControl | None = None, *, submit: bool = True) -> FormIR:
    return FormIR(
        platform="generic",
        tenant="careers.example.invalid",
        stage="review" if submit else "form",
        url_path="/jobs/42/apply",
        controls=(control or _control(),),
        submit_selector="#submit" if submit else "",
        submit_text="Submit application" if submit else "",
    )


def _profile() -> dict:
    return {
        "personal": {
            "email": SYNTHETIC_EMAIL,
            "phone": SYNTHETIC_PHONE,
        },
        "common_answers": {},
    }


def _submission_permit(
    tmp_path, form: FormIR, *, run_id: str, job_id: str, job_url: str
):
    ledger = EventLedger(tmp_path / "events.sqlite3")
    ledger.create_run(run_id=run_id, job_id=job_id)
    service = PermitService(secret=b"s" * 32, ledger=ledger)
    gate_a_bindings = PermitBindings(
        run_id=run_id,
        job_id=job_id,
        job_url_hash=hash_job_url(job_url),
        material_hash="b" * 64,
        answer_hash="c" * 64,
        review_hash="pre-review-plan",
        policy_hash="d" * 64,
    )
    gate_a_token = service.issue_gate_a(gate_a_bindings)
    gate_a_claims = service.consume(
        gate_a_token,
        expected_gate=PermitGate.GATE_A,
        expected_bindings=gate_a_bindings,
    )
    gate_b_bindings = PermitBindings(
        run_id=run_id,
        job_id=job_id,
        job_url_hash=gate_a_bindings.job_url_hash,
        material_hash=gate_a_bindings.material_hash,
        answer_hash=gate_a_bindings.answer_hash,
        review_hash=fingerprint_review(form, VerificationReport(True, (), ())),
        policy_hash=gate_a_bindings.policy_hash,
    )
    gate_b_token = service.issue_gate_b(
        gate_b_bindings, gate_a_jti=gate_a_claims.jti
    )
    intent_box = {"intent": None}

    async def validate_and_reserve(
        candidate_token, *, job_id: str, run_id: str, review_fingerprint: str
    ) -> bool:
        if (
            job_id != gate_b_bindings.job_id
            or run_id != gate_b_bindings.run_id
            or review_fingerprint != gate_b_bindings.review_hash
        ):
            return False
        try:
            service.consume(
                candidate_token,
                expected_gate=PermitGate.GATE_B,
                expected_bindings=gate_b_bindings,
            )
            intent = ledger.create_submission_intent(
                run_id=run_id,
                job_id=job_id,
                job_url=job_url,
                material_hash=gate_b_bindings.material_hash,
                answer_hash=gate_b_bindings.answer_hash,
                review_hash=gate_b_bindings.review_hash,
                policy_hash=gate_b_bindings.policy_hash,
                allow_existing_same=False,
            )
            ledger.mark_submission_started(intent.intent_id)
            intent_box["intent"] = intent
            return True
        except (DuplicateSubmissionError, PermitError):
            return False

    return (
        ledger,
        service,
        gate_a_claims.jti,
        gate_b_bindings,
        gate_b_token,
        validate_and_reserve,
        intent_box,
    )


def test_fingerprint_is_value_free_and_ignores_dynamic_browser_attributes():
    """Selectors, option values, titles, and generated IDs cannot bust recipes."""

    left = _form(
        _control(
            selector="#react-123456-email",
            element_id="react-123456-email",
            options=(FormOption("Yes", "private-server-value-a"),),
        )
    )
    right = FormIR(
        platform=left.platform,
        tenant=left.tenant,
        stage=left.stage,
        url_path=left.url_path,
        title="A changing job title",
        controls=(
            _control(
                selector="#react-999999-email",
                element_id="react-999999-email",
                options=(FormOption("Yes", "private-server-value-b"),),
            ),
        ),
        errors=("A transient message",),
        submit_selector="button[data-random='different']",
        submit_text="Submit application",
        metadata={"candidate_value": SYNTHETIC_EMAIL},
    )

    assert fingerprint_form(left) == fingerprint_form(right)
    assert fingerprint_form(left) != fingerprint_form(
        _form(_control(label="Work email address"))
    )


@pytest.mark.asyncio
async def test_observer_discards_control_values_and_emits_one_bounded_snapshot():
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply?session=private"
    page.title = AsyncMock(return_value="Synthetic application")
    page.evaluate = AsyncMock(
        return_value={
            "controls": [
                {
                    "index": 0,
                    "role": "textbox",
                    "tag": "input",
                    "input_type": "email",
                    "label": "Email",
                    "required": True,
                    "selector": "#email",
                    # A compromised page could include these. FormIR must ignore them.
                    "value": SYNTHETIC_EMAIL,
                    "current_value": SYNTHETIC_EMAIL,
                }
            ],
            "errors": [f"Invalid value for {SYNTHETIC_EMAIL}"],
            "next_selector": "",
            "next_text": "",
            "submit_selector": "#submit",
            "submit_text": "Submit application",
            "page_text_hints": [],
            "raw_html": f"<input value='{SYNTHETIC_EMAIL}'>",
        }
    )

    form = await observe_form(page, platform="generic", tenant="example")
    serialized = json.dumps(form.to_dict(), sort_keys=True)

    assert SYNTHETIC_EMAIL not in serialized
    assert "raw_html" not in serialized
    assert form.url_path == "/jobs/42/apply"
    assert form.errors[0].startswith("dom_error:")
    assert SYNTHETIC_EMAIL not in form.errors[0]
    page.evaluate.assert_awaited_once()


def test_recipe_cache_persists_only_canonical_actions_with_private_mode(tmp_path):
    cache = RecipeCache(tmp_path / "recipes")
    recipe = cache.save(
        fingerprint="a" * 64,
        platform="generic",
        tenant="careers.example.invalid",
        stage="form",
        actions=(
            RecipeAction(
                control_signature='{"label":"email"}',
                canonical_key="email",
                selector="#email",
                operation="fill",
            ),
        ),
    )

    payload_path = next((tmp_path / "recipes").glob("*.json"))
    payload = payload_path.read_text(encoding="utf-8")

    assert SYNTHETIC_EMAIL not in payload
    assert SYNTHETIC_PHONE not in payload
    assert "canonical_key" in payload
    assert '{"label":"email"}' not in payload
    assert "option_map" not in payload
    assert cache.load(recipe.fingerprint) == recipe
    assert stat.S_IMODE(payload_path.stat().st_mode) == 0o600


def test_known_fields_resolve_locally_without_a_model():
    brain = MagicMock()
    resolver = AnswerResolver(_profile())

    resolved, unresolved = resolver.resolve_form(_form())

    assert not unresolved
    assert [(item.canonical_key, item.value) for item in resolved] == [
        ("email", SYNTHETIC_EMAIL)
    ]
    brain.ask_json.assert_not_called()


def test_preferred_name_uses_the_explicit_private_fact():
    resolver = AnswerResolver(
        {"personal": {"preferred_name": "Synthetic Preferred"}}
    )
    control = FormControl(
        index=0,
        role="textbox",
        tag="input",
        label="Preferred name",
        required=True,
        selector="#preferred-name",
    )

    result = resolver.resolve(control)

    assert isinstance(result, ResolvedField)
    assert result.canonical_key == "preferred_name"
    assert result.value == "Synthetic Preferred"


def test_company_name_and_specific_address_do_not_inherit_personal_fallbacks():
    resolver = AnswerResolver(
        {
            "personal": {
                "first_name": "Synthetic",
                "last_name": "Candidate",
                "location": "Example City",
            },
            "common_answers": {},
        }
    )
    form = FormIR(
        platform="generic",
        tenant="careers.example.invalid",
        stage="form",
        url_path="/apply",
        controls=(
            FormControl(
                index=0,
                role="textbox",
                tag="input",
                label="Company name",
                required=True,
                selector="#company",
            ),
            FormControl(
                index=1,
                role="textbox",
                tag="input",
                label="Street address line 1",
                required=True,
                selector="#street",
            ),
        ),
    )

    resolved, unresolved = resolver.resolve_form(form)

    assert resolved == []
    assert [item.control.label for item in unresolved] == [
        "Company name",
        "Street address line 1",
    ]


def test_third_party_contact_never_inherits_candidate_data_or_model_mapping():
    resolver = AnswerResolver(_profile())
    reference_email = FormControl(
        index=0,
        role="textbox",
        tag="input",
        input_type="email",
        label="Reference email address",
        required=True,
        selector="#reference-email",
    )
    manager_phone = FormControl(
        index=1,
        role="textbox",
        tag="input",
        input_type="tel",
        label="Hiring manager phone number",
        required=True,
        selector="#manager-phone",
    )

    email_result = resolver.resolve(reference_email, "email")
    phone_result = resolver.resolve(manager_phone, "phone")

    assert not isinstance(email_result, ResolvedField)
    assert not isinstance(phone_result, ResolvedField)
    assert "structural confirmation" in email_result.reason
    assert "structural confirmation" in phone_result.reason


def test_exact_verified_question_answer_is_allowed_without_fuzzy_matching():
    question = "How did you hear about this specific role?"
    resolver = AnswerResolver(
        {
            "verified_question_answers": {
                question: {"value": "Company careers page", "verified": True}
            }
        }
    )
    control = FormControl(
        index=0,
        role="textbox",
        tag="input",
        label=question,
        required=True,
        selector="#source",
    )

    result = resolver.resolve(control)

    assert isinstance(result, ResolvedField)
    assert result.value == "Company careers page"


@pytest.mark.asyncio
async def test_readback_requires_verified_value_not_merely_nonempty():
    control = _control()
    field = ResolvedField(
        control=control,
        canonical_key="email",
        value=SYNTHETIC_EMAIL,
        source="verified_profile",
        sensitivity=Sensitivity.BASIC,
    )
    page = MagicMock()
    page.eval_on_selector = AsyncMock(
        return_value={
            "value": "wrong-but-nonempty@example.invalid",
            "checked": False,
            "groupChecked": False,
            "files": 0,
            "invalid": False,
        }
    )

    report = await verify_fields(page, _form(control), [field])

    assert not report.valid
    assert report.failures[0].reason == "read-back differs from verified value"


@pytest.mark.asyncio
async def test_readback_normalizes_phone_format_but_not_phone_digits():
    control = FormControl(
        index=0,
        role="textbox",
        tag="input",
        input_type="tel",
        label="Phone",
        required=True,
        selector="#phone",
    )
    field = ResolvedField(
        control=control,
        canonical_key="phone",
        value="+1 555 010 9384",
        source="verified_profile",
        sensitivity=Sensitivity.BASIC,
    )
    page = MagicMock()
    page.eval_on_selector = AsyncMock(
        return_value={"value": "(155) 501-09384", "invalid": False}
    )

    report = await verify_fields(page, _form(control), [field])

    assert report.valid
    assert len(report.readback_hashes) == 1
    first_review = fingerprint_review(_form(control), report)
    page.eval_on_selector = AsyncMock(
        return_value={"value": "+1 555-010-9384", "invalid": False}
    )
    reformatted = await verify_fields(page, _form(control), [field])
    assert reformatted.valid
    assert fingerprint_review(_form(control), reformatted) != first_review


@pytest.mark.asyncio
async def test_file_readback_binds_actual_uploaded_bytes_without_filename(
    tmp_path: Path,
):
    artifact = tmp_path / "candidate-name-must-not-leak.pdf"
    content = b"%PDF-1.4\nsynthetic resume bytes\n"
    artifact.write_bytes(content)
    control = FormControl(
        index=2,
        role="file_upload",
        tag="input",
        input_type="file",
        label="Resume",
        required=True,
        selector="#resume",
    )
    field = ResolvedField(
        control=control,
        canonical_key="resume",
        value=str(artifact),
        source="verified_profile",
        sensitivity=Sensitivity.PERSONAL,
    )
    page = MagicMock()
    page.eval_on_selector = AsyncMock(
        return_value={
            "files": 1,
            "fileContents": [
                {
                    "size": len(content),
                    "contentBase64": base64.b64encode(content).decode("ascii"),
                }
            ],
            "invalid": False,
        }
    )

    report = await verify_fields(page, _form(control), [field])

    expected = hashlib.sha256(content).hexdigest()
    assert report.valid
    assert report.material_content_hashes == ((2, "resume", expected),)
    review = fingerprint_review(_form(control), report)
    assert len(review) == 64
    serialized = json.dumps(report.material_content_hashes)
    assert str(artifact) not in serialized
    assert artifact.name not in serialized


def test_semantic_mapper_is_one_bounded_classification_call_without_values():
    control = FormControl(
        index=7,
        role="textbox",
        tag="input",
        label="Primary electronic contact",
        required=True,
        selector="#contact",
    )
    brain = MagicMock()
    brain.ask_json.return_value = {
        "mappings": [
            {"index": 7, "canonical_key": "email", "confidence": 0.99},
            {"index": 8, "canonical_key": "invented_fact", "confidence": 1.0},
            {"index": 9, "canonical_key": "salary", "confidence": 0.50},
        ]
    }

    mappings = map_unknown_controls(brain, [control])

    assert mappings == {7: "email"}
    brain.ask_json.assert_called_once()
    prompt = brain.ask_json.call_args.args[0]
    assert SYNTHETIC_EMAIL not in prompt
    assert SYNTHETIC_PHONE not in prompt
    assert "Primary electronic contact" in prompt
    assert "invented_fact" not in prompt


def test_semantic_mapper_ignores_malformed_or_out_of_snapshot_results():
    control = FormControl(
        index=2,
        role="textbox",
        tag="input",
        label="Electronic contact",
        required=True,
        selector="#contact",
    )
    brain = MagicMock()
    brain.ask_json.return_value = {
        "mappings": [
            None,
            "not-an-object",
            {"index": "bad", "canonical_key": "email", "confidence": 1},
            {"index": 999, "canonical_key": "email", "confidence": 1},
            {"index": 2, "canonical_key": "email", "confidence": "nan"},
            {"index": 2, "canonical_key": "email", "confidence": 0.99},
        ]
    }

    assert map_unknown_controls(brain, [control]) == {2: "email"}


def test_semantic_mapper_redacts_identity_echoed_by_browser_text():
    control = FormControl(
        index=2,
        role="textbox",
        tag="input",
        input_type="email",
        label=f"Confirm {SYNTHETIC_EMAIL}",
        required=True,
        selector="#contact",
    )
    brain = MagicMock()
    brain.ask_json.return_value = {
        "mappings": [{"index": 2, "canonical_key": "email", "confidence": 0.99}]
    }

    assert map_unknown_controls(
        brain,
        [control],
        private_values=(SYNTHETIC_EMAIL,),
    ) == {2: "email"}
    prompt = brain.ask_json.call_args.args[0]
    assert SYNTHETIC_EMAIL not in prompt
    assert "[PRIVATE]" in prompt


def test_prompt_redactions_cover_all_locally_injected_answer_sources():
    resolver = AnswerResolver(
        {
            "personal": {"email": SYNTHETIC_EMAIL},
            "canonical_answers": {"salary": {"value": "Synthetic salary"}},
            "common_answers": {"require_sponsorship": "Synthetic answer"},
            "verified_question_answers": {
                "Synthetic question": {"value": "Synthetic exact response"}
            },
        },
        cover_letter="Synthetic narrative letter",
    )

    redactions = resolver.prompt_redactions()

    assert SYNTHETIC_EMAIL in redactions
    assert "Synthetic salary" in redactions
    assert "Synthetic answer" in redactions
    assert "Synthetic exact response" in redactions
    assert "Synthetic narrative letter" in redactions


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["radio", "select"])
async def test_unmatched_choice_is_a_failure_and_is_never_cached(kind):
    if kind == "radio":
        control = FormControl(
            index=0,
            role="radio",
            tag="input",
            input_type="radio",
            label="No",
            required=True,
            selector="#choice-no",
        )
    else:
        control = FormControl(
            index=0,
            role="combobox",
            tag="select",
            label="Authorization",
            required=True,
            selector="#authorization",
            options=(FormOption("No", "no"),),
        )
    field = ResolvedField(
        control=control,
        canonical_key="work_authorization",
        value="Yes",
        source="verified_profile",
        sensitivity=Sensitivity.LEGAL,
    )
    element = AsyncMock()
    element.select_option = AsyncMock(side_effect=ValueError("no option"))
    page = MagicMock()
    page.wait_for_selector = AsyncMock(return_value=element)

    report = await execute_resolved_fields(page, [field])

    assert report.completed == 0
    assert len(report.failures) == 1
    assert report.recipe_actions == ()
    if kind == "radio":
        element.check.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_failure_does_not_echo_candidate_value():
    control = _control()
    field = ResolvedField(
        control=control,
        canonical_key="email",
        value=SYNTHETIC_EMAIL,
        source="verified_profile",
        sensitivity=Sensitivity.BASIC,
    )
    element = AsyncMock()
    element.fill = AsyncMock(
        side_effect=ValueError(f"invalid browser value {SYNTHETIC_EMAIL}")
    )
    page = MagicMock()
    page.wait_for_selector = AsyncMock(return_value=element)

    ok, reason = await execute_field(page, field)

    assert not ok
    assert SYNTHETIC_EMAIL not in reason
    assert reason == "ValueError: deterministic operation failed"


@pytest.mark.asyncio
async def test_submit_click_requires_observed_control_text_not_just_submit_type():
    save = AsyncMock()
    save.inner_text = AsyncMock(return_value="")
    save.get_attribute = AsyncMock(return_value="Save draft")
    page = MagicMock()
    page.wait_for_selector = AsyncMock(return_value=save)

    assert not await click_submit(page, "#missing", "Submit application")
    save.click.assert_not_awaited()

    submit = AsyncMock()
    submit.inner_text = AsyncMock(return_value="Submit application")
    page.wait_for_selector = AsyncMock(return_value=submit)
    assert await click_submit(page, "#submit", "Submit application")
    submit.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_adapter_normal_known_form_reaches_review_with_zero_model_calls(
    tmp_path, monkeypatch
):
    form = _form()
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply"
    brain = MagicMock()

    observe = AsyncMock(side_effect=(form, form))
    fill = AsyncMock(return_value=FillReport(1, 1, (), ()))
    verify = AsyncMock(return_value=VerificationReport(True, (), ()))
    evidence = AsyncMock(return_value=None)
    monkeypatch.setattr("adapters.generic_ai.adapter.observe_form", observe)
    monkeypatch.setattr("adapters.generic_ai.adapter.execute_resolved_fields", fill)
    monkeypatch.setattr("adapters.generic_ai.adapter.verify_fields", verify)
    monkeypatch.setattr("adapters.generic_ai.adapter.detect_submission_evidence", evidence)

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=_profile(),
        brain=brain,
        run_id="run-known",
        job_id="job-known",
    )

    assert outcome.status is OutcomeStatus.REVIEW_READY
    assert outcome.details["model_calls"] == 0
    assert outcome.phase.value == "REVIEW"
    brain.ask_json.assert_not_called()
    fill.assert_awaited_once()
    observe.assert_awaited()


@pytest.mark.asyncio
async def test_navigate_false_preserves_review_page_for_gate_b_resume(
    tmp_path, monkeypatch
):
    form = _form()
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/review"
    page.goto = AsyncMock(side_effect=AssertionError("review state must be preserved"))
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(side_effect=(form, form))
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.execute_resolved_fields",
        AsyncMock(return_value=FillReport(1, 1, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.verify_fields",
        AsyncMock(return_value=VerificationReport(True, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(return_value=None),
    )

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url="https://careers.example.invalid/jobs/42/apply",
        profile=_profile(),
        run_id="run-preserve",
        job_id="job-preserve",
        navigate=False,
    )

    assert outcome.status is OutcomeStatus.REVIEW_READY
    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_unknown_mapping_uses_exactly_one_value_free_model_call(
    tmp_path, monkeypatch
):
    control = FormControl(
        index=3,
        role="textbox",
        tag="input",
        input_type="email",
        label="Primary electronic contact",
        required=True,
        selector="#contact",
    )
    form = _form(control)
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply"
    brain = MagicMock()
    brain.ask_json.return_value = {
        "mappings": [{"index": 3, "canonical_key": "email", "confidence": 0.99}]
    }

    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(side_effect=(form, form))
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.execute_resolved_fields",
        AsyncMock(return_value=FillReport(1, 1, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.verify_fields",
        AsyncMock(return_value=VerificationReport(True, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(return_value=None),
    )

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=_profile(),
        brain=brain,
        run_id="run-mapped",
        job_id="job-mapped",
    )

    assert outcome.status is OutcomeStatus.REVIEW_READY
    assert outcome.details["model_calls"] == 1
    brain.ask_json.assert_called_once()
    prompt = brain.ask_json.call_args.args[0]
    assert SYNTHETIC_EMAIL not in prompt
    assert SYNTHETIC_PHONE not in prompt


@pytest.mark.asyncio
async def test_model_failure_is_redacted_and_becomes_safe_user_handoff(
    tmp_path, monkeypatch
):
    control = FormControl(
        index=3,
        role="textbox",
        tag="input",
        label="Unrecognized required field",
        required=True,
        selector="#unknown",
    )
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply"
    brain = MagicMock()
    brain.ask_json.side_effect = RuntimeError(
        f"provider failed while processing {SYNTHETIC_EMAIL}"
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(return_value=_form(control))
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(return_value=None),
    )

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=_profile(),
        brain=brain,
        run_id="run-model-error",
        job_id="job-model-error",
    )

    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert outcome.reason_code is ReasonCode.UNKNOWN_REQUIRED_QUESTION
    assert outcome.details["model_calls"] == 1
    assert outcome.details["classification_failed"] is True
    assert SYNTHETIC_EMAIL not in outcome.to_json()
    brain.ask_json.assert_called_once()


@pytest.mark.asyncio
async def test_model_cannot_auto_answer_new_sensitive_required_wording(
    tmp_path, monkeypatch
):
    control = FormControl(
        index=6,
        role="combobox",
        tag="select",
        label="Will employer support be necessary for your status?",
        required=True,
        selector="#status-support",
        options=(FormOption("Yes", "yes"), FormOption("No", "no")),
    )
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply"
    brain = MagicMock()
    brain.ask_json.return_value = {
        "mappings": [
            {"index": 6, "canonical_key": "sponsorship", "confidence": 0.99}
        ]
    }
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(return_value=_form(control))
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(return_value=None),
    )

    profile = _profile()
    profile["common_answers"]["require_sponsorship"] = "No"
    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=profile,
        brain=brain,
        run_id="run-new-sensitive",
        job_id="job-new-sensitive",
    )

    assert outcome.status is OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER
    assert outcome.reason_code is ReasonCode.SENSITIVE_ANSWER_REQUIRED
    assert outcome.details["model_calls"] == 1
    assert outcome.details["unresolved"][0]["sensitivity"] == "legal"


@pytest.mark.asyncio
async def test_model_classification_budget_is_one_call_for_entire_run(
    tmp_path, monkeypatch
):
    first_control = FormControl(
        index=1,
        role="textbox",
        tag="input",
        input_type="email",
        label="Primary electronic contact",
        required=True,
        selector="#contact-one",
    )
    second_control = FormControl(
        index=2,
        role="textbox",
        tag="input",
        label="A different unrecognized required field",
        required=True,
        selector="#contact-two",
    )
    first = FormIR(
        platform="generic",
        tenant="careers.example.invalid",
        stage="form",
        url_path="/jobs/42/apply/one",
        controls=(first_control,),
        next_selector="#next",
        next_text="Next",
    )
    second = FormIR(
        platform="generic",
        tenant="careers.example.invalid",
        stage="form",
        url_path="/jobs/42/apply/two",
        controls=(second_control,),
        submit_selector="#submit",
        submit_text="Submit application",
    )
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply"
    brain = MagicMock()
    brain.ask_json.return_value = {
        "mappings": [{"index": 1, "canonical_key": "email", "confidence": 0.99}]
    }
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form",
        AsyncMock(side_effect=(first, first, second)),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.execute_resolved_fields",
        AsyncMock(return_value=FillReport(1, 1, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.verify_fields",
        AsyncMock(return_value=VerificationReport(True, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.click_next", AsyncMock(return_value=True)
    )

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=_profile(),
        brain=brain,
        run_id="run-one-call",
        job_id="job-one-call",
    )

    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert outcome.details["model_calls"] == 1
    brain.ask_json.assert_called_once()


@pytest.mark.asyncio
async def test_unmapped_required_question_blocks_before_fill_or_navigation(
    tmp_path, monkeypatch
):
    control = FormControl(
        index=4,
        role="textbox",
        tag="textarea",
        label="Describe a novel fact not present in your verified profile",
        required=True,
        selector="#unknown",
    )
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply"
    execute = AsyncMock(side_effect=AssertionError("unknown answers must never be filled"))
    click = AsyncMock(side_effect=AssertionError("unknown answers must never navigate"))

    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(return_value=_form(control))
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("adapters.generic_ai.adapter.execute_resolved_fields", execute)
    monkeypatch.setattr("adapters.generic_ai.adapter.click_next", click)
    monkeypatch.setattr("adapters.generic_ai.adapter.click_submit", click)

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=_profile(),
        run_id="run-unknown",
        job_id="job-unknown",
    )

    assert outcome.status is OutcomeStatus.NEEDS_USER
    assert outcome.reason_code is ReasonCode.UNKNOWN_REQUIRED_QUESTION
    assert outcome.details["model_calls"] == 0
    unresolved = outcome.details["unresolved"][0]
    assert "label" not in unresolved
    assert len(unresolved["control_digest"]) == 64
    assert "Describe a novel" not in outcome.to_json()
    execute.assert_not_awaited()
    click.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_sensitive_verified_answer_gets_distinct_user_handoff(
    tmp_path, monkeypatch
):
    salary = FormControl(
        index=5,
        role="textbox",
        tag="input",
        label="Expected compensation",
        required=True,
        selector="#salary",
    )
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply"
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(return_value=_form(salary))
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(return_value=None),
    )

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=_profile(),
        run_id="run-sensitive",
        job_id="job-sensitive",
    )

    assert outcome.status is OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER
    assert outcome.reason_code is ReasonCode.SENSITIVE_ANSWER_REQUIRED


@pytest.mark.asyncio
async def test_gate_b_review_hash_must_match_fresh_browser_state(
    tmp_path, monkeypatch
):
    form = _form()
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/apply"
    validator = AsyncMock(return_value=False)
    submit = AsyncMock(side_effect=AssertionError("stale review must not submit"))
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(side_effect=(form, form))
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.execute_resolved_fields",
        AsyncMock(return_value=FillReport(1, 1, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.verify_fields",
        AsyncMock(return_value=VerificationReport(True, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("adapters.generic_ai.adapter.click_submit", submit)

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=_profile(),
        run_id="run-stale",
        job_id="job-stale",
        gate_b_token="synthetic-token",
        gate_b_validator=validator,
    )

    assert outcome.status is OutcomeStatus.AWAITING_GATE_B
    assert outcome.reason_code is ReasonCode.GATE_B_REQUIRED
    expected_review = fingerprint_review(form, VerificationReport(True, (), ()))
    validator.assert_awaited_once_with(
        "synthetic-token",
        job_id="job-stale",
        run_id="run-stale",
        review_fingerprint=expected_review,
    )
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_unknown_leaves_intent_for_orchestrator_and_cannot_retry(
    tmp_path, monkeypatch
):
    form = _form()
    run_id = "run-submit-unknown"
    job_id = "job-submit-unknown"
    job_url = "https://careers.example.invalid/jobs/42/apply"
    (
        ledger,
        service,
        gate_a_jti,
        bindings,
        token,
        validator,
        intent_box,
    ) = _submission_permit(tmp_path, form, run_id=run_id, job_id=job_id, job_url=job_url)
    page = MagicMock()
    page.url = job_url
    submit = AsyncMock(return_value=True)
    observe = AsyncMock(side_effect=(form, form, form, form))
    evidence = AsyncMock(side_effect=(None, None, None))
    monkeypatch.setattr("adapters.generic_ai.adapter.observe_form", observe)
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.execute_resolved_fields",
        AsyncMock(return_value=FillReport(1, 1, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.verify_fields",
        AsyncMock(return_value=VerificationReport(True, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence", evidence
    )
    monkeypatch.setattr("adapters.generic_ai.adapter.click_submit", submit)
    monkeypatch.setattr("adapters.generic_ai.adapter.asyncio.sleep", AsyncMock())
    adapter = GenericAIAdapter(cache=RecipeCache(tmp_path / "cache"))

    first = await adapter.run(
        page=page,
        job_url=job_url,
        profile=_profile(),
        run_id=run_id,
        job_id=job_id,
        gate_b_token=token,
        gate_b_validator=validator,
    )

    assert first.status is OutcomeStatus.SUBMIT_UNKNOWN
    intent_id = intent_box["intent"].intent_id
    # The adapter never owns ledger state.  The outer engine reconciles the
    # outcome after the browser call returns.
    assert ledger.get_submission_intent(intent_id).status is SubmissionStatus.SUBMITTING
    assert ledger.list_submission_evidence(intent_id) == []
    ledger.mark_submission_unknown(intent_id)

    retry_token = service.issue_gate_b(bindings, gate_a_jti=gate_a_jti)
    retry = await adapter.run(
        page=page,
        job_url=job_url,
        profile=_profile(),
        run_id=run_id,
        job_id=job_id,
        gate_b_token=retry_token,
        gate_b_validator=validator,
    )

    assert retry.status is OutcomeStatus.AWAITING_GATE_B
    assert retry.reason_code is ReasonCode.GATE_B_REQUIRED
    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_permitted_submit_returns_evidence_but_does_not_mutate_ledger(
    tmp_path, monkeypatch
):
    form = _form()
    run_id = "run-submit-verified"
    job_id = "job-submit-verified"
    job_url = "https://careers.example.invalid/jobs/84/apply"
    (
        ledger,
        _service,
        _gate_a_jti,
        _bindings,
        token,
        validator,
        intent_box,
    ) = _submission_permit(tmp_path, form, run_id=run_id, job_id=job_id, job_url=job_url)
    page = MagicMock()
    page.url = job_url
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(side_effect=(form, form))
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.execute_resolved_fields",
        AsyncMock(return_value=FillReport(1, 1, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.verify_fields",
        AsyncMock(return_value=VerificationReport(True, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(
            side_effect=(
                None,
                SubmissionEvidence(
                    kind="confirmation_text",
                    url="https://careers.example.invalid/jobs/84/confirmation",
                    text="We received your application.",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.click_submit", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("adapters.generic_ai.adapter.asyncio.sleep", AsyncMock())

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=job_url,
        profile=_profile(),
        run_id=run_id,
        job_id=job_id,
        gate_b_token=token,
        gate_b_validator=validator,
    )

    assert outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
    assert len(outcome.evidence_refs) == 1
    assert outcome.evidence_refs[0].uri is None
    assert "https://careers.example.invalid/jobs/84/confirmation" not in outcome.to_json()
    intent_id = intent_box["intent"].intent_id
    assert ledger.get_submission_intent(intent_id).status is SubmissionStatus.SUBMITTING
    assert ledger.list_submission_evidence(intent_id) == []
    # This is the engine's post-adapter responsibility.
    ledger.mark_submission_verified(
        intent_id=intent_id, evidence=outcome.evidence_refs[0]
    )
    ledger_evidence = ledger.list_submission_evidence(intent_id)
    assert len(ledger_evidence) == 1
    assert ledger_evidence[0].kind == EvidenceKind.CONFIRMATION_TEXT.value
    assert ledger_evidence[0].sha256 == outcome.evidence_refs[0].sha256


def test_review_and_confirmation_detection_require_explicit_state():
    valid = VerificationReport(True, (), ())
    assert is_review_ready(_form(), valid)
    assert not is_review_ready(_form(submit=False), valid)
    assert not is_review_ready(_form(), VerificationReport(False, (), ("required",)))

    assert is_confirmation_text("Your application has been submitted successfully.")
    assert is_confirmation_text("Thank you for applying to Example Corp")
    assert not is_confirmation_text("Thank you for visiting our careers site")
    assert not is_confirmation_text("Your application form is ready to submit")


@pytest.mark.asyncio
async def test_submission_detection_rejects_ambiguous_thanks_and_accepts_evidence():
    ambiguous = MagicMock()
    ambiguous.url = "https://careers.example.invalid/jobs/42/apply"
    ambiguous.inner_text = AsyncMock(return_value="Thank you for your interest in us.")
    assert await detect_submission_evidence(ambiguous) is None

    confirmed = MagicMock()
    confirmed.url = "https://careers.example.invalid/jobs/42/confirmation"
    confirmed.inner_text = AsyncMock(return_value="We received your application.")
    evidence = await detect_submission_evidence(confirmed)
    assert evidence is not None
    assert evidence.kind == "confirmation_text"
    assert "received your application" in evidence.text.casefold()


@pytest.mark.asyncio
async def test_preexisting_confirmation_is_uncorrelated_and_never_verified(tmp_path):
    page = MagicMock()
    page.url = "https://careers.example.invalid/jobs/42/confirmation"
    page.inner_text = AsyncMock(return_value="Application submitted successfully.")
    brain = MagicMock()

    outcome = await GenericAIAdapter(cache=RecipeCache(tmp_path / "cache")).run(
        page=page,
        job_url=page.url,
        profile=_profile(),
        brain=brain,
        run_id="run-confirmed",
        job_id="job-confirmed",
    )

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.reason_code is ReasonCode.SUBMISSION_CONFIRMATION_MISSING
    assert outcome.evidence_refs == ()
    assert outcome.details["uncorrelated_confirmation"] is True
    assert outcome.details["model_calls"] == 0
    brain.ask_json.assert_not_called()
