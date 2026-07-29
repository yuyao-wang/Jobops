"""Deterministic Workday authentication and application adapter.

The normal path uses stable ``data-automation-id`` attributes and confirmed
local values only.  It never sends a page to a model and never guesses an
answer.  CAPTCHA, MFA, account locks, mailbox ambiguity, and unknown required
questions produce structured handoff outcomes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit
from uuid import uuid4

from adapters.shared import (
    canonical_key_for,
    invoke_gate_b_validator,
    normalize_text,
    resolve_confirmed_value,
    select_exact_option,
)
from adapters.protocol import (
    AdapterSupport,
    ApplicationContext as ProtocolApplicationContext,
    BaseATSAdapter,
    FieldIR,
    FieldKind,
    FillReport as ProtocolFillReport,
    FormIR,
    ReviewDigest,
    SubmissionEvidence,
    UnresolvedField,
    ValidationReport,
)
from auth.credentials import CredentialStore, CredentialStoreError
from auth.mailbox import (
    MailboxVerificationStatus,
    MailboxVerifier,
    VerificationArtifactKind,
    VerificationRequest,
)
from auth.workday_hosts import is_trusted_workday_host
from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.bundles import MaterialBundle
from core.private_home import PrivateHome
from utils.keychain import (
    KeychainError,
    default_credential_store,
    delete_workday_credential,
    generate_strong_password,
    get_workday_credential,
    save_workday_credential,
)


ADAPTER_NAME = "workday"


class WorkdayStage(StrEnum):
    JOB = "job"
    LOGIN = "login"
    REGISTER = "register"
    EMAIL_VERIFICATION = "email_verification"
    MFA = "mfa"
    CAPTCHA = "captcha"
    ACCOUNT_LOCKED = "account_locked"
    AUTOFILL_WITH_RESUME = "autofillWithResume"
    MY_INFORMATION = "myInformation"
    MY_EXPERIENCE = "myExperience"
    APPLICATION_QUESTIONS = "applicationQuestions"
    VOLUNTARY_DISCLOSURES = "voluntaryDisclosures"
    SELF_IDENTIFY = "selfIdentify"
    REVIEW = "review"
    CONFIRMATION = "confirmation"
    LOADING = "loading"
    OTHER = "other"


APPLICATION_STAGES = frozenset({
    WorkdayStage.AUTOFILL_WITH_RESUME,
    WorkdayStage.MY_INFORMATION,
    WorkdayStage.MY_EXPERIENCE,
    WorkdayStage.APPLICATION_QUESTIONS,
    WorkdayStage.VOLUNTARY_DISCLOSURES,
    WorkdayStage.SELF_IDENTIFY,
})


LOGIN_EMAIL_SELECTORS = (
    '[data-automation-id="email"]',
    '[data-automation-id="username"]',
    'input[name="username"]',
    'input[name="email"]',
    'input[type="email"]',
)
PASSWORD_SELECTORS = (
    '[data-automation-id="password"]',
    'input[type="password"]',
)
CONFIRM_EMAIL_SELECTORS = (
    '[data-automation-id="verifyEmail"]',
    'input[name*="confirmEmail" i]',
    'input[name*="verifyEmail" i]',
)
CONFIRM_PASSWORD_SELECTORS = (
    '[data-automation-id="verifyPassword"]',
    'input[name*="confirmPassword" i]',
    'input[name*="verifyPassword" i]',
)
VERIFICATION_CODE_SELECTORS = (
    '[data-automation-id="verificationCode"]',
    '[data-automation-id="emailVerificationCode"]',
    'input[autocomplete="one-time-code"]',
)
NEXT_SELECTORS = (
    '[data-automation-id="bottom-navigation-next-button"]',
    '[data-automation-id="pageFooterNextButton"]',
    '[data-automation-id="saveAndContinueButton"]',
)
SUBMIT_SELECTORS = (
    '[data-automation-id="submitApplicationButton"]',
    '[data-automation-id="bottom-navigation-next-button"]',
)


REGISTRATION_TERMS_RULESET_VERSION = "workday-registration-terms/v1"
_REGISTRATION_TERMS_RULES = {
    "createaccountcheckbox": frozenset({
        "i agree to the terms and conditions",
        "i agree to the terms of use",
        "i have read and agree to the privacy statement",
        "i agree to the privacy policy and terms of use",
    }),
}


@dataclass(frozen=True)
class WorkdayPageSignals:
    text: str
    url: str
    password_fields: int = 0
    visible_inputs: int = 0
    has_apply_button: bool = False
    has_create_account: bool = False
    automation_ids: tuple[str, ...] = ()
    heading: str = ""
    has_captcha_challenge: bool = False
    posting_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkdayField:
    selector: str
    automation_id: str
    name: str
    label: str
    kind: str
    required: bool
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WorkdayFillReport:
    filled: int = 0
    skipped: int = 0
    filled_fields: tuple[str, ...] = ()
    uploaded_files: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    sensitive_unresolved: tuple[str, ...] = ()
    readback_mismatches: tuple[str, ...] = ()
    expected_readbacks: tuple["WorkdayExpectedReadback", ...] = field(
        default=(), repr=False
    )


@dataclass(frozen=True)
class WorkdayExpectedReadback:
    selector: str
    canonical_key: str
    label: str
    kind: str
    expected_value: Any = field(repr=False)


@dataclass(frozen=True)
class WorkdayPostingIdentity:
    origin: str
    posting_path: str
    requisition_id: str = ""


@dataclass(frozen=True)
class RegistrationFillResult:
    fields_ready: bool
    unresolved_required: tuple[str, ...] = ()


@dataclass
class WorkdayApplicationContext:
    page: Any
    job_url: str
    profile: Mapping[str, Any]
    job_id: str
    run_id: str
    resume_path: str | None = None
    cover_letter: str = ""
    answers: Mapping[str, Any] = field(default_factory=dict)
    request_submit: bool = False
    gate_b_permit: Any = None
    gate_b_validator: Any = None
    # A digest copied from an append-only REVIEW_READY outcome.  It contains no
    # candidate values and is used only to prove that a separately resumed
    # Workday Review has the same job, local inputs, and rendered review surface
    # that previously passed exact per-stage read-back validation.
    persisted_review_attestation: str = ""
    credential_store: CredentialStore | None = None
    mailbox_verifier: MailboxVerifier | None = None
    initiated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    max_steps: int = 40
    navigate: bool = True
    navigation_timeout_ms: int = 45_000
    settle_timeout_ms: int = 750
    materials: MaterialBundle | None = None
    private_home: PrivateHome | None = None


def classify_workday_state(signals: WorkdayPageSignals) -> str:
    """Compatibility classifier returning the historic string stage names."""
    return detect_workday_stage(signals).value


def detect_workday_stage(signals: WorkdayPageSignals) -> WorkdayStage:
    """Classify one Workday SPA state from URL and stable local markers."""
    text = normalize_text(signals.text)
    url = signals.url.casefold()
    ids = {item.casefold() for item in signals.automation_ids}

    if _contains_any(text, (
        "thank you for applying",
        "application submitted",
        "application received",
        "successfully submitted",
        "you have applied",
    )) or ids.intersection({"applicationsubmitted", "applicationconfirmation"}):
        return WorkdayStage.CONFIRMATION
    if _contains_any(text, (
        "account has been locked",
        "account is locked",
        "account may be locked",
        "too many failed",
        "wrong email or password",
        "wrong email password",
        "password you entered isn t correct",
        "incorrect username or password",
        "unable to sign in",
    )):
        return WorkdayStage.ACCOUNT_LOCKED
    if signals.has_captcha_challenge or _contains_any(text, (
        "complete the captcha",
        "solve the captcha",
        "verify you are human",
        "captcha challenge",
    )) or text in {"captcha", "recaptcha", "hcaptcha"}:
        return WorkdayStage.CAPTCHA
    if _contains_any(text, (
        "multi factor authentication",
        "two factor authentication",
        "two step verification",
        "authenticator app",
        "security key",
    )):
        return WorkdayStage.MFA
    if _contains_any(text, (
        "verify your email",
        "check your email",
        "email verification",
        "we sent you a code by email",
        "code sent to your email",
    )) or ids.intersection({"verificationcode", "emailverificationcode"}):
        return WorkdayStage.EMAIL_VERIFICATION

    route_map = (
        ("autofillwithresume", WorkdayStage.AUTOFILL_WITH_RESUME),
        ("myinformation", WorkdayStage.MY_INFORMATION),
        ("myexperience", WorkdayStage.MY_EXPERIENCE),
        ("applicationquestions", WorkdayStage.APPLICATION_QUESTIONS),
        ("voluntarydisclosures", WorkdayStage.VOLUNTARY_DISCLOSURES),
        ("selfidentify", WorkdayStage.SELF_IDENTIFY),
        ("review", WorkdayStage.REVIEW),
    )
    compact_url = url.replace("-", "").replace("_", "")
    for token, stage in route_map:
        if token in compact_url:
            return stage

    id_stage_map = (
        ({"resumeupload", "file-upload-input-ref", "autofillwithresume"}, WorkdayStage.AUTOFILL_WITH_RESUME),
        ({"myinformation", "legalnamesection_firstname"}, WorkdayStage.MY_INFORMATION),
        ({"myexperience", "workexperiencesection"}, WorkdayStage.MY_EXPERIENCE),
        ({"applicationquestions", "primaryquestionnairepage"}, WorkdayStage.APPLICATION_QUESTIONS),
        ({"voluntarydisclosures"}, WorkdayStage.VOLUNTARY_DISCLOSURES),
        ({"selfidentify"}, WorkdayStage.SELF_IDENTIFY),
        ({"reviewpage", "reviewsubmit"}, WorkdayStage.REVIEW),
    )
    for markers, stage in id_stage_map:
        if ids.intersection(markers):
            return stage

    if signals.password_fields >= 2 or "create an account" in text:
        return WorkdayStage.REGISTER
    if signals.password_fields == 1 and _contains_any(text, ("sign in", "log in", "forgot password")):
        return WorkdayStage.LOGIN
    if signals.has_apply_button:
        return WorkdayStage.JOB
    if signals.visible_inputs > 0 and any(token in url for token in ("/apply", "application")):
        # Workday routes can briefly omit their stage segment while hydrating.
        return WorkdayStage.LOADING
    if not text and not signals.automation_ids:
        return WorkdayStage.LOADING
    return WorkdayStage.OTHER


async def inspect_workday_state(page: Any) -> tuple[str, WorkdayPageSignals]:
    signals = await inspect_workday_signals(page)
    return classify_workday_state(signals), signals


async def inspect_workday_signals(page: Any) -> WorkdayPageSignals:
    """Read a compact deterministic Workday projection without an LLM."""
    try:
        details = await page.evaluate(
            """() => {
                const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const bodyText = (document.body && document.body.innerText || '').slice(0, 30000);
                return {
                    text: bodyText,
                    heading: (document.querySelector('h1, [role="heading"]')?.textContent || '').trim(),
                    passwordFields: Array.from(document.querySelectorAll('input[type="password"]'))
                        .filter(visible).length,
                    visibleInputs: Array.from(document.querySelectorAll('input, textarea, select'))
                        .filter(el => visible(el) && el.type !== 'hidden').length,
                    hasApplyButton: !!document.querySelector('[data-automation-id="jobPostingApplyButton"]') ||
                        Array.from(document.querySelectorAll('button, a')).some(el =>
                            visible(el) && /^apply( now)?$/i.test((el.innerText || '').trim())),
                    hasCreateAccount: Array.from(document.querySelectorAll('button, a')).some(el =>
                        visible(el) && /create (an )?account/i.test((el.innerText || '').trim())),
                    hasCaptchaChallenge: Array.from(document.querySelectorAll(
                        'iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], '
                        + '.g-recaptcha, .h-captcha, [data-sitekey]'
                    )).some(visible),
                    automationIds: Array.from(document.querySelectorAll('[data-automation-id]'))
                        .map(el => el.getAttribute('data-automation-id'))
                        .filter(Boolean).slice(0, 250),
                    postingUrls: Array.from(new Set([
                        document.querySelector('link[rel="canonical"]')?.href || '',
                        document.querySelector('meta[property="og:url"]')?.content || '',
                        ...Array.from(document.querySelectorAll(
                            'a[data-automation-id*="job" i][href], a[href*="/job/"]'
                        )).slice(0, 20).map(el => el.href || '')
                    ].filter(Boolean))).slice(0, 20),
                };
            }"""
        )
    except Exception:
        details = {}
    return WorkdayPageSignals(
        text=str(details.get("text", "")),
        url=str(getattr(page, "url", "")),
        password_fields=int(details.get("passwordFields", 0)),
        visible_inputs=int(details.get("visibleInputs", 0)),
        has_apply_button=bool(details.get("hasApplyButton", False)),
        has_create_account=bool(details.get("hasCreateAccount", False)),
        automation_ids=tuple(details.get("automationIds") or ()),
        heading=str(details.get("heading", "")),
        has_captcha_challenge=bool(details.get("hasCaptchaChallenge", False)),
        posting_urls=tuple(str(item) for item in details.get("postingUrls") or ()),
    )


class WorkdayAdapter(BaseATSAdapter):
    """State-machine driver for Workday candidate applications."""

    name = ADAPTER_NAME
    host_patterns = ("myworkdayjobs.com", "workday.com")
    dom_markers = (
        '[data-automation-id="jobPostingApplyButton"]',
        '[data-automation-id="myInformation"]',
        '[data-automation-id="reviewPage"]',
    )
    submit_selectors = SUBMIT_SELECTORS
    confirmation_selectors = (
        '[data-automation-id="applicationSubmitted"]',
        '[data-automation-id="applicationConfirmation"]',
    )
    review_selectors = ('[data-automation-id="reviewPage"]',)

    def __init__(
        self,
        *,
        credential_store: CredentialStore | None = None,
        mailbox_verifier: MailboxVerifier | None = None,
    ) -> None:
        self.credential_store = credential_store
        self.mailbox_verifier = mailbox_verifier
        # In-memory correlation complements the durable submission intent held
        # by the orchestrator. A confirmation page alone is never proof that
        # this adapter instance submitted during this run.
        self._submit_correlations: set[tuple[str, str]] = set()
        self._protocol_expected_readbacks: dict[
            tuple[int, str], tuple[WorkdayExpectedReadback, ...]
        ] = {}
        self._run_expected_readbacks: dict[
            tuple[str, str], dict[tuple[str, str], WorkdayExpectedReadback]
        ] = {}

    async def support(self, page: Any, url: str) -> AdapterSupport:
        host = (urlparse(url).hostname or "").casefold()
        host_match = is_trusted_workday_host(host)
        dom_match = False
        for selector in self.dom_markers:
            try:
                if await page.locator(selector).first.count():
                    dom_match = True
                    break
            except Exception:
                continue
        supported = host_match or dom_match
        return AdapterSupport(
            adapter=self.name,
            supported=supported,
            confidence=1.0 if host_match and dom_match else 0.9 if host_match else 0.75 if dom_match else 0.0,
            reason="host and DOM matched" if host_match and dom_match else "host matched" if host_match else "DOM matched" if dom_match else "no deterministic Workday marker",
        )

    async def inspect(self, page: Any) -> FormIR:
        """Expose the current Workday step through the shared Adapter Protocol."""
        fields = await inspect_workday_fields(page)
        protocol_fields = tuple(FieldIR(
            canonical_key=_workday_canonical_key(item),
            label=item.label or item.automation_id or item.name,
            selectors=(item.selector,),
            kind=_protocol_kind(item.kind),
            required=item.required,
            name=item.name,
            element_id=item.automation_id,
            options=item.options,
        ) for item in fields)
        signature_payload = [
            [item.canonical_key, item.kind.value, item.required, item.options]
            for item in protocol_fields
        ]
        signature = hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return FormIR(
            adapter=self.name,
            url=str(getattr(page, "url", "")),
            fields=protocol_fields,
            submit_selectors=self.submit_selectors,
            confirmation_selectors=self.confirmation_selectors,
            review_selectors=self.review_selectors,
            signature=signature,
        )

    async def fill(
        self,
        page: Any,
        context: ProtocolApplicationContext,
        form: FormIR,
    ) -> ProtocolFillReport:
        """Fill one Workday step through the shared Adapter Protocol."""
        workday_context = _coerce_context(
            context,
            credential_store=self.credential_store,
            mailbox_verifier=self.mailbox_verifier,
        )
        signals = await inspect_workday_signals(page)
        stage = detect_workday_stage(signals)
        # Re-inspect the current local controls to preserve Workday-specific
        # distinctions such as an ARIA combobox versus a native ``select``.
        # ``FieldIR`` intentionally normalizes both to FieldKind.SELECT.
        fields = await inspect_workday_fields(page)
        report = await fill_workday_fields(workday_context, stage, fields)
        self._protocol_expected_readbacks[(id(page), form.signature)] = (
            report.expected_readbacks
        )
        self._remember_expected_readbacks(workday_context, report.expected_readbacks)
        unresolved = tuple(
            UnresolvedField(
                canonical_key=f"custom:{normalize_text(label)}",
                label=label,
                reason="no exact confirmed answer",
            )
            for label in report.unresolved
        ) + tuple(
            UnresolvedField(
                canonical_key=f"custom:{normalize_text(label)}",
                label=label,
                reason="sensitive confirmed answer required",
            )
            for label in report.sensitive_unresolved
        )
        return ProtocolFillReport(
            filled_fields=report.filled_fields,
            uploaded_files=report.uploaded_files,
            unresolved_required=unresolved,
            errors=tuple(
                f"exact read-back mismatch: {label}"
                for label in report.readback_mismatches
            ),
        )

    async def validate(
        self,
        page: Any,
        form: FormIR,
        fill: ProtocolFillReport,
    ) -> ValidationReport:
        missing = [item.label for item in fill.unresolved_required]
        errors = list(fill.errors)
        expected = self._protocol_expected_readbacks.get((id(page), form.signature), ())
        expected_selectors = {item.selector for item in expected}
        for item in form.fields:
            if not item.required or item.label in missing:
                continue
            if not item.selectors or item.selectors[0] not in expected_selectors:
                missing.append(item.label)
        mismatched = await _validate_expected_readbacks(page, expected)
        missing.extend(mismatched)
        if fill.filled_fields and not expected:
            errors.append("exact Workday readback bindings are unavailable")
        return ValidationReport(
            valid=not missing and not errors,
            missing_required=tuple(dict.fromkeys(missing)),
            errors=tuple(dict.fromkeys(errors)),
        )

    def _remember_expected_readbacks(
        self,
        context: WorkdayApplicationContext,
        readbacks: Sequence[WorkdayExpectedReadback],
    ) -> None:
        if not readbacks:
            return
        key = (context.run_id, context.job_id)
        stored = self._run_expected_readbacks.setdefault(key, {})
        for item in readbacks:
            stored[(item.canonical_key, item.selector)] = item

    async def prepare_review(
        self,
        page: Any,
        context: ProtocolApplicationContext,
        form: FormIR,
        fill: ProtocolFillReport,
        validation: ValidationReport,
    ) -> ReviewDigest:
        expected = self._protocol_expected_readbacks.get((id(page), form.signature), ())
        mismatched = await _validate_expected_readbacks(page, expected)
        exact_validation = ValidationReport(
            valid=validation.valid and not mismatched,
            missing_required=tuple(dict.fromkeys(
                (*validation.missing_required, *mismatched)
            )),
            errors=validation.errors,
        )
        return await super().prepare_review(
            page, context, form, fill, exact_validation
        )

    async def submit(
        self,
        page: Any,
        context: ProtocolApplicationContext,
        review: ReviewDigest,
    ) -> bool:
        clicked = await _click_submit_once(
            page, f"{context.job_id}:{review.fingerprint}"
        )
        if clicked:
            self._submit_correlations.add((context.run_id, context.job_id))
        return clicked

    async def verify_submission(
        self,
        page: Any,
        context: ProtocolApplicationContext,
    ) -> SubmissionEvidence:
        correlation = (context.run_id, context.job_id)
        if correlation not in self._submit_correlations:
            return SubmissionEvidence()
        signals = await inspect_workday_signals(page)
        if detect_workday_stage(signals) is not WorkdayStage.CONFIRMATION:
            return SubmissionEvidence()
        marker = next((
            phrase for phrase in (
                "application submitted",
                "application received",
                "thank you for applying",
                "successfully submitted",
                "you have applied",
            ) if phrase in normalize_text(signals.text)
        ), "Workday application confirmation")
        self._submit_correlations.discard(correlation)
        return SubmissionEvidence(
            confirmation_text=marker,
            confirmation_url=_sanitize_confirmation_url(signals.url),
        )

    async def run(
        self,
        context: WorkdayApplicationContext | ProtocolApplicationContext,
    ) -> ApplicationOutcome:
        context = _coerce_context(
            context,
            credential_store=self.credential_store,
            mailbox_verifier=self.mailbox_verifier,
        )
        page = context.page
        generated_password: str | None = None
        previous_signature = ""
        repeated = 0
        requested_identity = _workday_posting_identity(context.job_url)
        if requested_identity is None:
            return _workday_identity_failure(
                context,
                "The requested Workday posting identity could not be established",
            )
        posting_bound = False

        try:
            if context.navigate and not _same_workday_session_url(
                str(getattr(page, "url", "")), context.job_url
            ):
                await page.goto(
                    context.job_url,
                    wait_until="domcontentloaded",
                    timeout=context.navigation_timeout_ms,
                )
            await _settle(page)
        except Exception as exc:
            return _retryable(context, OutcomePhase.AUTHENTICATE, "Workday page could not be loaded", exc)

        for _ in range(max(1, context.max_steps)):
            signals = await inspect_workday_signals(page)
            stage = detect_workday_stage(signals)
            identity_status = _workday_identity_status(requested_identity, signals)
            if identity_status == "mismatch":
                return _workday_identity_failure(
                    context,
                    "The active Workday application belongs to a different posting",
                    active_url=signals.url,
                )
            if identity_status == "match":
                posting_bound = True
            elif not posting_bound and stage is not WorkdayStage.LOADING:
                return _workday_identity_failure(
                    context,
                    "The active Workday stage could not be bound to the requested posting",
                    active_url=signals.url,
                )
            signature = f"{stage.value}:{signals.url}:{','.join(signals.automation_ids[:20])}"
            repeated = repeated + 1 if signature == previous_signature else 0
            previous_signature = signature

            # An action stage that does not change after two attempts is a
            # validation or handoff condition.  Never hammer login/register,
            # verification, or navigation controls in a loop.
            if repeated >= 2 and stage in {
                WorkdayStage.JOB,
                WorkdayStage.LOGIN,
                WorkdayStage.REGISTER,
                WorkdayStage.EMAIL_VERIFICATION,
                *APPLICATION_STAGES,
            }:
                if stage is WorkdayStage.EMAIL_VERIFICATION:
                    return _needs_user(
                        context,
                        OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
                        ReasonCode.EMAIL_VERIFICATION,
                        "Workday email verification did not advance after one safe attempt",
                        checkpoint="workday.auth.verify_email",
                    )
                if stage in {WorkdayStage.LOGIN, WorkdayStage.REGISTER}:
                    return _needs_user(
                        context,
                        OutcomeStatus.NEEDS_USER_LOGIN,
                        ReasonCode.LOGIN_REQUIRED,
                        "Workday authentication did not advance; no further automatic attempts were made",
                        checkpoint=f"workday.auth.{stage.value}",
                    )
                return ApplicationOutcome(
                    run_id=context.run_id,
                    job_id=context.job_id,
                    status=OutcomeStatus.FAILED_RETRYABLE,
                    phase=OutcomePhase.VALIDATE,
                    reason_code=ReasonCode.VALIDATION_FAILED,
                    message="Workday did not advance after deterministic form completion",
                    adapter=self.name,
                    retryable=True,
                    checkpoint=f"workday.application.{stage.value}",
                )

            if stage is WorkdayStage.CONFIRMATION:
                correlation = (context.run_id, context.job_id)
                if correlation in self._submit_correlations:
                    self._submit_correlations.discard(correlation)
                    return _confirmed_outcome(context, signals)
                return _uncorrelated_confirmation_outcome(context)
            if stage is WorkdayStage.CAPTCHA:
                return _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_CAPTCHA,
                    ReasonCode.CAPTCHA,
                    "Workday CAPTCHA requires user action",
                    checkpoint="workday.auth.captcha",
                )
            if stage is WorkdayStage.MFA:
                return _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_2FA,
                    ReasonCode.TWO_FACTOR_AUTH,
                    "Workday multi-factor authentication requires user action",
                    checkpoint="workday.auth.mfa",
                )
            if stage is WorkdayStage.ACCOUNT_LOCKED:
                return _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_ACCOUNT_LOCKED,
                    ReasonCode.ACCOUNT_LOCKED,
                    "Workday rejected the login or locked the account",
                    checkpoint="workday.auth.account_locked",
                )
            if stage is WorkdayStage.JOB:
                if not await _click_apply(page):
                    return _needs_user(
                        context,
                        OutcomeStatus.NEEDS_USER,
                        ReasonCode.LOGIN_REQUIRED,
                        "Workday Apply control could not be activated",
                        checkpoint="workday.job",
                    )
                await _settle(page)
                continue
            if stage is WorkdayStage.LOGIN:
                if generated_password:
                    email = _profile_email(context.profile)
                    if not email or not await _fill_login(page, email, generated_password):
                        return _needs_user(
                            context,
                            OutcomeStatus.NEEDS_USER_LOGIN,
                            ReasonCode.LOGIN_REQUIRED,
                            "The newly registered Workday account could not be signed in",
                            checkpoint="workday.auth.login",
                        )
                    # _register persisted and verified this generated password
                    # before it was entered into the registration form.
                    generated_password = None
                    await _settle(page)
                    continue
                result = await self._login(context, signals)
                if isinstance(result, ApplicationOutcome):
                    return result
                await _settle(page)
                continue
            if stage is WorkdayStage.REGISTER:
                result, generated_password = await self._register(context, generated_password)
                if isinstance(result, ApplicationOutcome):
                    return result
                await _settle(page)
                continue
            if stage is WorkdayStage.EMAIL_VERIFICATION:
                # The generated value is already in Keychain before account
                # creation. It is safe to drop the transient in-memory copy.
                generated_password = None
                result = await self._verify_email(context)
                if isinstance(result, ApplicationOutcome):
                    return result
                await _settle(page)
                continue
            if stage in APPLICATION_STAGES:
                generated_password = None
                result = await self._complete_stage(context, stage)
                if isinstance(result, ApplicationOutcome):
                    return result
                await _settle(page)
                continue
            if stage is WorkdayStage.REVIEW:
                return await self._review_or_submit(context)

            if repeated >= 4:
                return ApplicationOutcome(
                    run_id=context.run_id,
                    job_id=context.job_id,
                    status=OutcomeStatus.FAILED_RETRYABLE,
                    phase=OutcomePhase.INSPECT,
                    reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
                    message="Workday stage did not stabilize",
                    adapter=self.name,
                    retryable=True,
                    checkpoint=f"workday.{stage.value}",
                    details={"stage": stage.value},
                )
            await asyncio.sleep(0.75)

        return ApplicationOutcome(
            run_id=context.run_id,
            job_id=context.job_id,
            status=OutcomeStatus.FAILED_RETRYABLE,
            phase=OutcomePhase.INSPECT,
            reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
            message="Workday step limit reached",
            adapter=self.name,
            retryable=True,
            checkpoint="workday.step_limit",
        )

    async def _login(
        self,
        context: WorkdayApplicationContext,
        signals: WorkdayPageSignals,
    ) -> ApplicationOutcome | None:
        config = _workday_config(context.profile)
        if not bool(config.get("auto_login", True)):
            return _needs_user(
                context,
                OutcomeStatus.NEEDS_USER_LOGIN,
                ReasonCode.LOGIN_REQUIRED,
                "Automatic Workday login is disabled",
                checkpoint="workday.auth.login",
            )
        email = _profile_email(context.profile)
        if not email:
            return _missing_fact(context, "A confirmed email address is required for Workday login")
        try:
            credential = get_workday_credential(
                context.job_url,
                email,
                store=context.credential_store or default_credential_store(),
            )
        except (CredentialStoreError, KeychainError, ValueError):
            return _needs_user(
                context,
                OutcomeStatus.NEEDS_USER_LOGIN,
                ReasonCode.LOGIN_REQUIRED,
                "The local credential store is unavailable",
                checkpoint="workday.auth.login",
            )
        if credential:
            if not await _fill_login(context.page, email, credential.password):
                return _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_LOGIN,
                    ReasonCode.LOGIN_REQUIRED,
                    "Workday login fields could not be completed",
                    checkpoint="workday.auth.login",
                )
            return None

        if bool(config.get("auto_register", True)) and signals.has_create_account:
            if await _click_named(context.page, ("Create Account", "Create an Account")):
                return None
        return _needs_user(
            context,
            OutcomeStatus.NEEDS_USER_LOGIN,
            ReasonCode.LOGIN_REQUIRED,
            "No Workday tenant credential is available",
            checkpoint="workday.auth.login",
        )

    async def _register(
        self,
        context: WorkdayApplicationContext,
        generated_password: str | None,
    ) -> tuple[ApplicationOutcome | None, str | None]:
        config = _workday_config(context.profile)
        if not bool(config.get("auto_register", True)):
            return (
                _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_LOGIN,
                    ReasonCode.LOGIN_REQUIRED,
                    "Automatic Workday account registration is disabled",
                    checkpoint="workday.auth.register",
                ),
                generated_password,
            )
        email = _profile_email(context.profile)
        if not email:
            return _missing_fact(context, "A confirmed email address is required for registration"), None
        backend = context.credential_store or default_credential_store()
        try:
            existing = get_workday_credential(
                context.job_url,
                email,
                store=backend,
            )
        except (CredentialStoreError, KeychainError, ValueError):
            return (
                _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_LOGIN,
                    ReasonCode.LOGIN_REQUIRED,
                    "The existing Workday credential could not be read safely",
                    checkpoint="workday.auth.register",
                    details={"credential_store": "read_failed"},
                ),
                generated_password,
            )
        previous_password = existing.password if existing is not None else None
        password = generated_password or generate_strong_password(
            int(config.get("generated_password_length", 24))
        )
        # Persist first. If the Keychain write or read-back fails, never enter
        # this generated secret into Workday and never create an unrecoverable
        # account credential.
        if not self._save_generated_credential(context, password):
            self._restore_generated_credential(context, previous_password)
            return (
                _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_LOGIN,
                    ReasonCode.LOGIN_REQUIRED,
                    "The generated Workday password could not be stored safely",
                    checkpoint="workday.auth.register",
                    details={"credential_store": "write_failed"},
                ),
                password,
            )

        registration = await _fill_registration(context.page, email, password)
        if registration.unresolved_required:
            self._restore_generated_credential(context, previous_password)
            return (
                _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER,
                    ReasonCode.UNKNOWN_REQUIRED_QUESTION,
                    "An unknown required Workday registration agreement needs user review",
                    checkpoint="workday.auth.register",
                    details={
                        "unresolved_labels": list(registration.unresolved_required),
                        "terms_ruleset": REGISTRATION_TERMS_RULESET_VERSION,
                    },
                ),
                password,
            )
        if not registration.fields_ready:
            self._restore_generated_credential(context, previous_password)
            return (
                _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_LOGIN,
                    ReasonCode.LOGIN_REQUIRED,
                    "Workday registration fields could not be completed",
                    checkpoint="workday.auth.register",
                ),
                password,
            )
        if not await _click_named(context.page, ("Create Account", "Register")):
            self._restore_generated_credential(context, previous_password)
            return (
                _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_LOGIN,
                    ReasonCode.LOGIN_REQUIRED,
                    "Workday account creation control could not be activated",
                    checkpoint="workday.auth.register",
                ),
                password,
            )
        return None, password

    def _save_generated_credential(
        self,
        context: WorkdayApplicationContext,
        password: str,
    ) -> bool:
        email = _profile_email(context.profile)
        if not email:
            return False
        try:
            save_workday_credential(
                context.job_url,
                email,
                password,
                store=context.credential_store or default_credential_store(),
            )
            return True
        except (CredentialStoreError, KeychainError, ValueError):
            return False

    def _restore_generated_credential(
        self,
        context: WorkdayApplicationContext,
        previous_password: str | None,
    ) -> bool:
        """Restore the exact prior Keychain state after failed registration.

        A generated password may have replaced an older credential before the
        browser step failed. Deleting unconditionally would destroy that older
        value, so cleanup now restores it when present.
        """

        email = _profile_email(context.profile)
        if not email:
            return False
        backend = context.credential_store or default_credential_store()
        try:
            if previous_password is None:
                delete_workday_credential(
                    context.job_url,
                    email,
                    store=backend,
                )
                return True
            save_workday_credential(
                context.job_url,
                email,
                previous_password,
                store=backend,
            )
            return True
        except (CredentialStoreError, KeychainError, ValueError):
            # Cleanup failure must not cause another browser action. The caller
            # still receives a handoff and no account creation was clicked.
            return False

    async def _verify_email(
        self,
        context: WorkdayApplicationContext,
    ) -> ApplicationOutcome | None:
        verifier = context.mailbox_verifier
        email = _profile_email(context.profile)
        if verifier is None or not email:
            return _needs_user(
                context,
                OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
                ReasonCode.EMAIL_VERIFICATION,
                "Workday email verification requires user action",
                checkpoint="workday.auth.verify_email",
                details={"mailbox_agent": "disabled" if verifier is None else "missing_recipient"},
            )
        host = (urlparse(context.job_url).hostname or "").casefold()
        company = str(_nested(context.profile, "application.company") or "").strip()
        result = await verifier.find_verification(VerificationRequest(
            recipient=email,
            tenant_host=host,
            initiated_at=context.initiated_at,
            correlation_terms=(company,) if company else (),
        ))
        if result.status is not MailboxVerificationStatus.FOUND or result.artifact is None:
            return _needs_user(
                context,
                OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
                _mailbox_reason(result.status),
                "Mailbox verification could not select one safe, correlated message",
                checkpoint="workday.auth.verify_email",
                details={"mailbox_status": result.status.value},
            )
        if result.artifact.kind is VerificationArtifactKind.CODE:
            if not await _fill_first(context.page, VERIFICATION_CODE_SELECTORS, result.artifact.value):
                return _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
                    ReasonCode.EMAIL_VERIFICATION,
                    "The correlated email code could not be entered",
                    checkpoint="workday.auth.verify_email",
                )
            if not await _click_named(context.page, ("Verify", "Continue", "Submit")):
                return _needs_user(
                    context,
                    OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
                    ReasonCode.EMAIL_VERIFICATION,
                    "The Workday email verification control could not be activated",
                    checkpoint="workday.auth.verify_email",
                )
            return None
        if result.artifact.kind is VerificationArtifactKind.LINK:
            await context.page.goto(
                result.artifact.value,
                wait_until="domcontentloaded",
                timeout=45000,
            )
            return None
        return _needs_user(
            context,
            OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
            ReasonCode.EMAIL_VERIFICATION,
            "Unsupported email verification artifact",
            checkpoint="workday.auth.verify_email",
        )

    async def _complete_stage(
        self,
        context: WorkdayApplicationContext,
        stage: WorkdayStage,
    ) -> ApplicationOutcome | None:
        fields = await inspect_workday_fields(context.page)
        report = await fill_workday_fields(context, stage, fields)
        if report.readback_mismatches:
            return ApplicationOutcome.needs_user(
                run_id=context.run_id,
                job_id=context.job_id,
                status=OutcomeStatus.NEEDS_USER,
                phase=OutcomePhase.VALIDATE,
                reason_code=ReasonCode.VALIDATION_FAILED,
                message="Workday did not retain the exact verified values",
                adapter=self.name,
                checkpoint=f"workday.application.{stage.value}",
                details={
                    "mismatched_labels": list(report.readback_mismatches)
                },
            )
        if report.sensitive_unresolved:
            return _needs_user(
                context,
                OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
                ReasonCode.SENSITIVE_ANSWER_REQUIRED,
                "A required sensitive Workday answer is not in the confirmed answer bank",
                checkpoint=f"workday.application.{stage.value}",
                details={"unresolved_labels": list(report.sensitive_unresolved)},
            )
        if report.unresolved:
            return _needs_user(
                context,
                OutcomeStatus.NEEDS_USER,
                ReasonCode.UNKNOWN_REQUIRED_QUESTION,
                "A required Workday answer is not in the confirmed answer bank",
                checkpoint=f"workday.application.{stage.value}",
                details={"unresolved_labels": list(report.unresolved)},
            )
        mismatched = await _validate_expected_readbacks(
            context.page, report.expected_readbacks
        )
        if mismatched:
            return ApplicationOutcome.needs_user(
                run_id=context.run_id,
                job_id=context.job_id,
                status=OutcomeStatus.NEEDS_USER,
                phase=OutcomePhase.VALIDATE,
                reason_code=ReasonCode.VALIDATION_FAILED,
                message="Workday read-back differs from the exact verified values",
                adapter=self.name,
                checkpoint=f"workday.application.{stage.value}",
                details={"mismatched_labels": list(mismatched)},
            )
        self._remember_expected_readbacks(context, report.expected_readbacks)
        if not await _click_next(context.page):
            return ApplicationOutcome(
                run_id=context.run_id,
                job_id=context.job_id,
                status=OutcomeStatus.FAILED_RETRYABLE,
                phase=OutcomePhase.FILL,
                reason_code=ReasonCode.VALIDATION_FAILED,
                message="Workday stage could not advance",
                adapter=self.name,
                retryable=True,
                checkpoint=f"workday.application.{stage.value}",
                details={"stage": stage.value, "filled": report.filled},
            )
        return None

    async def _review_or_submit(
        self,
        context: WorkdayApplicationContext,
    ) -> ApplicationOutcome:
        current_fields = await inspect_workday_fields(context.page)
        current_expected, unresolved_review = _expected_readbacks_for_fields(
            context, current_fields
        )
        if unresolved_review:
            return ApplicationOutcome.needs_user(
                run_id=context.run_id,
                job_id=context.job_id,
                status=OutcomeStatus.NEEDS_USER,
                phase=OutcomePhase.VALIDATE,
                reason_code=ReasonCode.UNKNOWN_REQUIRED_QUESTION,
                message="Workday Review contains a required value without an exact verified answer",
                adapter=self.name,
                checkpoint="workday.review",
                details={"unresolved_labels": list(unresolved_review)},
            )
        self._remember_expected_readbacks(context, current_expected)
        stored = tuple(
            self._run_expected_readbacks.get(
                (context.run_id, context.job_id), {}
            ).values()
        )
        missing_bindings = (
            not stored and _context_has_verified_application_values(context)
        )
        review_surface_digest = await _workday_review_surface_digest(context.page)
        current_attestation = (
            _workday_binding_attestation(context, review_surface_digest)
            if review_surface_digest
            else ""
        )
        resumed_attested = bool(
            missing_bindings
            and current_attestation
            and context.persisted_review_attestation
            and current_attestation == context.persisted_review_attestation
        )
        if missing_bindings and context.request_submit and not resumed_attested:
            return ApplicationOutcome.needs_user(
                run_id=context.run_id,
                job_id=context.job_id,
                status=OutcomeStatus.NEEDS_USER,
                phase=OutcomePhase.VALIDATE,
                reason_code=ReasonCode.VALIDATION_FAILED,
                message="Exact Workday review read-back bindings are unavailable",
                adapter=self.name,
                checkpoint="workday.review",
            )
        mismatched = await _validate_expected_readbacks(context.page, stored)
        if mismatched:
            return ApplicationOutcome.needs_user(
                run_id=context.run_id,
                job_id=context.job_id,
                status=OutcomeStatus.NEEDS_USER,
                phase=OutcomePhase.VALIDATE,
                reason_code=ReasonCode.VALIDATION_FAILED,
                message="Workday Review differs from the exact verified values",
                adapter=self.name,
                checkpoint="workday.review",
                details={"mismatched_labels": list(mismatched)},
            )
        fingerprint = await workday_review_fingerprint(
            context,
            review_surface_digest=review_surface_digest,
        )
        binding_attestation = (
            current_attestation if stored or resumed_attested else ""
        )
        evidence = EvidenceRef(
            kind=EvidenceKind.FORM_SNAPSHOT,
            sha256=fingerprint,
            metadata={"stage": WorkdayStage.REVIEW.value},
        )
        if not context.request_submit:
            return ApplicationOutcome.review_ready(
                run_id=context.run_id,
                job_id=context.job_id,
                adapter=self.name,
                checkpoint="workday.review",
                evidence_refs=(evidence,),
                details={
                    "model_calls": 0,
                    "review_fingerprint": fingerprint,
                    "workday_binding_attestation": binding_attestation,
                    "resume_uploaded": bool(context.resume_path),
                    "exact_readback_bindings": bool(stored),
                    "resumed_at_review": missing_bindings,
                    "resumed_review_attested": resumed_attested,
                },
            )
        permit_valid = await invoke_gate_b_validator(
            context.gate_b_validator,
            context.gate_b_permit,
            job_id=context.job_id,
            run_id=context.run_id,
            review_fingerprint=fingerprint,
        )
        if not permit_valid:
            return ApplicationOutcome(
                run_id=context.run_id,
                job_id=context.job_id,
                status=OutcomeStatus.AWAITING_GATE_B,
                phase=OutcomePhase.REVIEW,
                reason_code=ReasonCode.GATE_B_REQUIRED,
                message="A valid one-time Gate B permit is required",
                adapter=self.name,
                checkpoint="workday.review",
                evidence_refs=(evidence,),
                details={"review_fingerprint": fingerprint},
            )
        if not await _click_submit_once(
            context.page, f"{context.job_id}:{fingerprint}"
        ):
            return ApplicationOutcome(
                run_id=context.run_id,
                job_id=context.job_id,
                status=OutcomeStatus.FAILED_RETRYABLE,
                phase=OutcomePhase.SUBMIT,
                reason_code=ReasonCode.VALIDATION_FAILED,
                message="The Workday Submit control was not available",
                adapter=self.name,
                retryable=True,
                checkpoint="workday.review",
            )

        correlation = (context.run_id, context.job_id)
        self._submit_correlations.add(correlation)

        # Never click twice.  Poll only for explicit confirmation evidence.
        for _ in range(15):
            await asyncio.sleep(1)
            signals = await inspect_workday_signals(context.page)
            if detect_workday_stage(signals) is WorkdayStage.CONFIRMATION:
                self._submit_correlations.discard(correlation)
                return _confirmed_outcome(context, signals)
        return ApplicationOutcome(
            run_id=context.run_id,
            job_id=context.job_id,
            status=OutcomeStatus.SUBMIT_UNKNOWN,
            phase=OutcomePhase.VERIFY,
            reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
            message="Submit was clicked but explicit Workday confirmation was not observed",
            adapter=self.name,
            checkpoint="workday.submit_unknown",
            details={"do_not_retry_submit": True},
        )


async def inspect_workday_fields(page: Any) -> tuple[WorkdayField, ...]:
    """Build a bounded form projection from stable Workday metadata."""
    try:
        raw_fields = await page.evaluate(
            """() => {
                const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                return Array.from(document.querySelectorAll(
                    'input, textarea, select, [role="combobox"], '
                    + 'button[aria-haspopup="listbox"], [role="radio"], [role="checkbox"]'
                ))
                    .filter(el => (visible(el) || el.type === 'file') && !el.disabled && el.type !== 'hidden')
                    .slice(0, 200)
                    .map((el, index) => {
                        const aid = el.getAttribute('data-automation-id') || '';
                        const name = el.getAttribute('name') || '';
                        const id = el.id || '';
                        let label = el.getAttribute('aria-label') || '';
                        if ((el.type === 'radio' || el.type === 'checkbox')) {
                            const group = el.closest('fieldset, [data-automation-id*="question" i]');
                            label = group?.querySelector('legend, [data-automation-id="formLabel"]')
                                ?.textContent || label;
                        }
                        if (!label && el.labels && el.labels.length) {
                            label = Array.from(el.labels)
                                .map(item => item.innerText || item.textContent || '')
                                .join(' ');
                        }
                        if (!label) {
                            const container = el.closest('fieldset, [data-automation-id*="question" i], div');
                            label = container?.querySelector('legend, label, [data-automation-id="formLabel"]')
                                ?.textContent || '';
                        }
                        if (!aid && !id && !name) el.dataset.jobopsWorkdayField = String(index);
                        const selector = aid ? `[data-automation-id="${CSS.escape(aid)}"]`
                            : id ? `#${CSS.escape(id)}`
                            : name ? `[name="${CSS.escape(name)}"]`
                            : `[data-jobops-workday-field="${index}"]`;
                        return {
                            selector,
                            automationId: aid,
                            name,
                            label: label.trim(),
                            kind: (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox')
                                ? 'combobox' :
                                el.getAttribute('role') === 'radio' ? 'radio' :
                                el.getAttribute('role') === 'checkbox' ? 'checkbox' :
                                el.tagName.toLowerCase() === 'select' ? 'select' :
                                el.tagName.toLowerCase() === 'textarea' ? 'textarea' :
                                (el.getAttribute('type') || 'text').toLowerCase(),
                            required: el.required || el.getAttribute('aria-required') === 'true' ||
                                Boolean(el.closest('[aria-required="true"]')),
                            options: el.tagName.toLowerCase() === 'select'
                                ? Array.from(el.options).map(option => [option.value, (option.textContent || '').trim()])
                                : [],
                        };
                    });
            }"""
        )
    except Exception:
        return ()
    return tuple(WorkdayField(
        selector=str(item.get("selector", "")),
        automation_id=str(item.get("automationId", "")),
        name=str(item.get("name", "")),
        label=str(item.get("label", "")),
        kind=str(item.get("kind", "text")),
        required=bool(item.get("required", False)),
        options=tuple((str(value), str(label)) for value, label in item.get("options", ())),
    ) for item in raw_fields if item.get("selector"))


_WORKDAY_READBACK_SCRIPT = r"""async el => {
    const type = String(el.type || '').toLowerCase();
    const role = String(el.getAttribute('role') || '').toLowerCase();
    const selected = el.tagName === 'SELECT' && el.selectedIndex >= 0
        ? el.options[el.selectedIndex] : null;
    let groupSelected = null;
    if (type === 'radio' && el.name) {
        groupSelected = document.querySelector(
            `input[type="radio"][name="${CSS.escape(el.name)}"]:checked`
        );
    } else if (type === 'radio' && el.checked) {
        groupSelected = el;
    } else if (role === 'radio') {
        const container = el.closest('[role="radiogroup"], fieldset') || document;
        groupSelected = container.querySelector('[role="radio"][aria-checked="true"]');
    }
    const groupLabel = groupSelected
        ? ((groupSelected.labels && groupSelected.labels.length
            ? Array.from(groupSelected.labels).map(item => item.innerText || item.textContent || '').join(' ')
            : groupSelected.getAttribute('aria-label')) || '')
        : '';
    const fileContents = [];
    if (type === 'file') {
        for (const file of Array.from(el.files || [])) {
            const bytes = new Uint8Array(await file.arrayBuffer());
            let binary = '';
            for (let offset = 0; offset < bytes.length; offset += 0x8000) {
                binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
            }
            fileContents.push(btoa(binary));
        }
    }
    return {
        value: String(el.value || ''),
        selectedText: String(selected ? selected.textContent || '' : ''),
        selectedValue: String(selected ? selected.value || '' : ''),
        checked: type === 'checkbox' || type === 'radio'
            ? Boolean(el.checked)
            : el.getAttribute('aria-checked') === 'true',
        groupChecked: Boolean(groupSelected),
        groupValue: String(groupSelected
            ? (groupSelected.value || groupSelected.getAttribute('data-value') || '')
            : ''),
        groupLabel: String(groupLabel),
        ariaValue: String(el.getAttribute('aria-valuetext') || ''),
        selectedDataValue: String(el.getAttribute('data-selected-value') || ''),
        text: String(el.textContent || ''),
        fileContents,
    };
}"""


def _exact_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").strip()


async def _workday_readback_matches(
    page: Any,
    expected: WorkdayExpectedReadback,
) -> bool:
    try:
        locator = page.locator(expected.selector).first
        if hasattr(locator, "count") and not await locator.count():
            return False
        state = await locator.evaluate(_WORKDAY_READBACK_SCRIPT)
    except Exception:
        return False
    kind = expected.kind.casefold()
    value = expected.expected_value
    if kind == "file":
        expected_sha256 = _file_sha256(str(value))
        if not expected_sha256:
            return False
        observed_hashes: list[str] = []
        for encoded in state.get("fileContents") or ():
            try:
                observed_hashes.append(hashlib.sha256(
                    base64.b64decode(str(encoded), validate=True)
                ).hexdigest())
            except (ValueError, binascii.Error):
                return False
        return observed_hashes == [expected_sha256]
    if kind == "checkbox":
        expected_bool = _strict_bool(value)
        return expected_bool is not None and bool(state.get("checked")) is expected_bool
    if kind == "radio":
        if not state.get("groupChecked"):
            return False
        target = normalize_text(value)
        return target in {
            normalize_text(state.get("groupValue")),
            normalize_text(state.get("groupLabel")),
        }
    if kind in {"select", "combobox", "select-one", "select-multiple"}:
        target = normalize_text(value)
        return bool(target) and target in {
            normalize_text(state.get("selectedText")),
            normalize_text(state.get("selectedValue")),
            normalize_text(state.get("ariaValue")),
            normalize_text(state.get("selectedDataValue")),
            normalize_text(state.get("text")),
            normalize_text(state.get("value")),
        }
    return _exact_text(state.get("value")) == _exact_text(value)


async def _validate_expected_readbacks(
    page: Any,
    expected: Sequence[WorkdayExpectedReadback],
) -> tuple[str, ...]:
    mismatched: list[str] = []
    for item in expected:
        if not await _workday_readback_matches(page, item):
            mismatched.append(_safe_label(item.label))
    return tuple(dict.fromkeys(mismatched))


def _expected_readbacks_for_fields(
    context: WorkdayApplicationContext,
    fields: Sequence[WorkdayField],
) -> tuple[tuple[WorkdayExpectedReadback, ...], tuple[str, ...]]:
    answers = dict(context.answers or {})
    common = context.profile.get("common_answers", {})
    if isinstance(common, Mapping):
        answers = {**common, **answers}
    expected: list[WorkdayExpectedReadback] = []
    unresolved: list[str] = []
    for item in fields:
        if item.kind in {"password", "submit", "button"}:
            continue
        canonical = _workday_canonical_key(item)
        label = item.label or item.automation_id or item.name or "Unlabelled required field"
        if item.kind == "file":
            value = context.resume_path if canonical == "resume" else None
        else:
            value = resolve_confirmed_value(
                canonical,
                label,
                profile=context.profile,
                answers=answers,
                cover_letter=context.cover_letter,
            )
        if value in (None, ""):
            if item.required:
                unresolved.append(_safe_label(label))
            continue
        expected.append(WorkdayExpectedReadback(
            selector=item.selector,
            canonical_key=canonical,
            label=_safe_label(label),
            kind=item.kind,
            expected_value=value,
        ))
    return tuple(expected), tuple(dict.fromkeys(unresolved))


def _context_has_verified_application_values(
    context: WorkdayApplicationContext,
) -> bool:
    return bool(
        context.resume_path
        or context.cover_letter
        or context.answers
        or context.profile.get("personal")
        or context.profile.get("common_answers")
    )


async def fill_workday_fields(
    context: WorkdayApplicationContext,
    stage: WorkdayStage,
    fields: Sequence[WorkdayField],
) -> WorkdayFillReport:
    filled = 0
    skipped = 0
    filled_fields: list[str] = []
    uploaded_files: list[str] = []
    unresolved: list[str] = []
    sensitive: list[str] = []
    readback_mismatches: list[str] = []
    expected_readbacks: list[WorkdayExpectedReadback] = []
    seen_radio_groups: set[str] = set()
    answers = dict(context.answers or {})
    common = context.profile.get("common_answers", {})
    if isinstance(common, Mapping):
        answers = {**common, **answers}

    for field in fields:
        if field.kind in {"password", "submit", "button"}:
            skipped += 1
            continue
        label = field.label or field.automation_id or field.name or "Unlabelled required field"
        canonical = _workday_canonical_key(field)
        is_sensitive = stage in {
            WorkdayStage.VOLUNTARY_DISCLOSURES,
            WorkdayStage.SELF_IDENTIFY,
        } or _is_sensitive_question(label)

        if field.kind == "file":
            value = context.resume_path if canonical == "resume" else None
        else:
            value = resolve_confirmed_value(
                canonical,
                label,
                profile=context.profile,
                answers=answers,
                cover_letter=context.cover_letter,
            )
        if value in (None, ""):
            if field.required:
                target = sensitive if is_sensitive else unresolved
                target.append(_safe_label(label))
            else:
                skipped += 1
            continue

        locator = context.page.locator(field.selector).first
        try:
            if field.kind == "file":
                path = Path(str(value)).expanduser()
                if not path.is_file():
                    if field.required:
                        unresolved.append("resume upload")
                    continue
                await locator.set_input_files(str(path.resolve()))
                # Outcomes record the artifact role, not a potentially
                # identifying local filename.
                uploaded_files.append("resume")
            elif field.kind in {"select", "combobox"}:
                selected = (
                    await select_exact_option(locator, value)
                    if field.kind == "select"
                    else await _select_combobox_exact(context.page, locator, value)
                )
                if not selected:
                    if field.required:
                        (sensitive if is_sensitive else unresolved).append(_safe_label(label))
                    continue
            elif field.kind == "radio":
                group = field.name or normalize_text(label)
                if group in seen_radio_groups:
                    continue
                seen_radio_groups.add(group)
                if not await _select_radio_exact(context.page, field, value):
                    if field.required:
                        (sensitive if is_sensitive else unresolved).append(_safe_label(label))
                    continue
            elif field.kind == "checkbox":
                expected = _strict_bool(value)
                if expected is None:
                    if field.required:
                        (sensitive if is_sensitive else unresolved).append(_safe_label(label))
                    continue
                try:
                    current = bool(await locator.is_checked())
                    if current != expected:
                        await locator.set_checked(expected)
                except Exception:
                    current = (await locator.get_attribute("aria-checked")) == "true"
                    if current != expected:
                        await locator.click()
            else:
                await locator.fill(str(value))
            expected = WorkdayExpectedReadback(
                selector=field.selector,
                canonical_key=canonical,
                label=_safe_label(label),
                kind=field.kind,
                expected_value=value,
            )
            if not await _workday_readback_matches(context.page, expected):
                if field.required:
                    readback_mismatches.append(_safe_label(label))
                continue
            expected_readbacks.append(expected)
            filled += 1
            filled_fields.append(canonical)
        except Exception:
            if field.required:
                (sensitive if is_sensitive else unresolved).append(_safe_label(label))

    return WorkdayFillReport(
        filled=filled,
        skipped=skipped,
        filled_fields=tuple(dict.fromkeys(filled_fields)),
        uploaded_files=tuple(dict.fromkeys(uploaded_files)),
        unresolved=tuple(dict.fromkeys(unresolved)),
        sensitive_unresolved=tuple(dict.fromkeys(sensitive)),
        readback_mismatches=tuple(dict.fromkeys(readback_mismatches)),
        expected_readbacks=tuple(expected_readbacks),
    )


async def _workday_review_surface_digest(page: Any) -> str:
    """Hash the rendered Review surface without retaining its private text."""

    try:
        projection = await page.evaluate(
            r"""() => {
                const root = document.querySelector('[data-automation-id="reviewPage"]')
                    || document.querySelector('main') || document.body;
                if (!root) return null;
                const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
                return {
                    text: clean(root.innerText).slice(0, 50000),
                    automationIds: Array.from(root.querySelectorAll('[data-automation-id]'))
                        .map(el => el.getAttribute('data-automation-id'))
                        .filter(Boolean).slice(0, 500),
                    headings: Array.from(root.querySelectorAll('h1, h2, h3, [role="heading"]'))
                        .map(el => clean(el.textContent)).filter(Boolean).slice(0, 100),
                    submitLabels: Array.from(root.querySelectorAll('button, input[type="submit"]'))
                        .map(el => clean(el.innerText || el.value || el.getAttribute('aria-label')))
                        .filter(Boolean).slice(0, 50),
                };
            }"""
        )
    except Exception:
        return ""
    if not isinstance(projection, Mapping):
        return ""
    # The raw projection may contain candidate values, but it is immediately
    # reduced to a digest and is never placed in an Outcome, event, or prompt.
    return _sha256_json(projection)


def _workday_binding_attestation(
    context: WorkdayApplicationContext,
    review_surface_digest: str,
) -> str:
    identity = _workday_posting_identity(context.job_url)
    identity_digest = _sha256_json({
        "origin": identity.origin if identity else "",
        "posting_path": identity.posting_path if identity else "",
        "requisition_id": identity.requisition_id if identity else "",
    })
    return _sha256_json({
        "schema": "jobops.workday-review-binding/v1",
        "job_identity_sha256": identity_digest,
        "review_surface_sha256": review_surface_digest,
        "resume_sha256": _file_sha256(context.resume_path),
        "cover_letter_sha256": _sha256_text(context.cover_letter),
        "candidate_projection_sha256": _sha256_json({
            "personal": context.profile.get("personal", {}),
            "common_answers": context.profile.get("common_answers", {}),
            "answers": context.answers,
        }),
    })


async def workday_review_fingerprint(
    context: WorkdayApplicationContext,
    *,
    review_surface_digest: str = "",
) -> str:
    fields = await inspect_workday_fields(context.page)
    try:
        current_values = await context.page.evaluate(
            """async () => Promise.all(Array.from(
                document.querySelectorAll('input, textarea, select')
            )
                .filter(el => !['password', 'hidden'].includes((el.type || '').toLowerCase()))
                .slice(0, 250)
                .map(async el => {
                    let value;
                    if (el.type === 'checkbox' || el.type === 'radio') {
                        value = String(Boolean(el.checked));
                    } else if (el.type === 'file') {
                        const contents = [];
                        for (const file of Array.from(el.files || [])) {
                            const bytes = new Uint8Array(await file.arrayBuffer());
                            let binary = '';
                            for (let offset = 0; offset < bytes.length; offset += 0x8000) {
                                binary += String.fromCharCode(
                                    ...bytes.subarray(offset, offset + 0x8000)
                                );
                            }
                            contents.push(btoa(binary));
                        }
                        // This process-local value is immediately SHA-256
                        // hashed in Python and never enters an outcome/event.
                        value = contents.sort().join(',');
                    } else {
                        value = String(el.value || '');
                    }
                    return {
                        key: el.getAttribute('data-automation-id') || el.name || el.id || el.type,
                        value
                    };
                }))"""
        )
    except Exception:
        current_values = []
    projection = {
        "job_url": context.job_url,
        "stage": WorkdayStage.REVIEW.value,
        "review_surface_sha256": review_surface_digest,
        "field_shape": [
            [field.automation_id, field.name, field.kind, field.required]
            for field in fields
        ],
        "field_value_hashes": [
            [str(item.get("key", "")), _sha256_text(str(item.get("value", "")))]
            for item in current_values
        ],
        "resume_sha256": _file_sha256(context.resume_path),
        "cover_letter_sha256": _sha256_text(context.cover_letter),
        "candidate_projection_sha256": _sha256_json({
            "personal": context.profile.get("personal", {}),
            "common_answers": context.profile.get("common_answers", {}),
            "answers": context.answers,
        }),
    }
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def apply_workday(
    page: Any,
    job_url: str,
    profile: dict,
    brain: Any,
    cover_letter: str = "",
    dry_run: bool = True,
) -> bool:
    """Legacy MR.Jobs wrapper around the structured adapter.

    Live legacy calls stop at Gate B rather than bypassing the new permit
    invariant.  New orchestration should call :class:`WorkdayAdapter` directly
    and consume its :class:`~core.outcomes.ApplicationOutcome`.
    """
    del brain  # Workday's deterministic path intentionally has no model call.
    context = WorkdayApplicationContext(
        page=page,
        job_url=job_url,
        profile=profile,
        job_id=_legacy_job_id(job_url),
        run_id=f"legacy-{uuid4().hex}",
        resume_path=profile.get("resume_path"),
        cover_letter=cover_letter,
        answers=profile.get("common_answers", {}),
        request_submit=not dry_run,
    )
    outcome = await WorkdayAdapter().run(context)
    return outcome.status in {
        OutcomeStatus.REVIEW_READY,
        OutcomeStatus.SUBMITTED_VERIFIED,
    }


async def _fill_first(page: Any, selectors: Sequence[str], value: str) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=800):
                await locator.fill(value)
                return True
        except Exception:
            continue
    return False


async def _click_named(page: Any, names: Sequence[str]) -> bool:
    for name in names:
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=name, exact=False).first
                if await locator.is_visible(timeout=800):
                    await locator.click()
                    return True
            except Exception:
                continue
    return False


async def _check_registration_terms(page: Any) -> tuple[str, ...]:
    """Check only versioned, exact Workday registration agreements.

    Any other required checkbox remains untouched and becomes a user handoff.
    The rules deliberately bind both a stable automation id and exact visible
    text; a generic ``required`` checkbox is never sufficient authorization.
    """

    try:
        items = await page.evaluate(
            """() => {
                const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                return Array.from(document.querySelectorAll(
                    'input[type="checkbox"][required], '
                    + '[role="checkbox"][aria-required="true"]'
                )).filter(visible).slice(0, 20).map((el, index) => {
                    const aid = el.getAttribute('data-automation-id') || '';
                    const id = el.id || '';
                    const name = el.getAttribute('name') || '';
                    let label = el.getAttribute('aria-label') || '';
                    if (!label && el.labels && el.labels.length) {
                        label = Array.from(el.labels)
                            .map(item => item.innerText || item.textContent || '')
                            .join(' ');
                    }
                    if (!label) {
                        label = el.closest('label, [data-automation-id*="agreement" i]')
                            ?.textContent || '';
                    }
                    if (!aid && !id && !name) {
                        el.dataset.jobopsRegistrationCheckbox = String(index);
                    }
                    const selector = aid ? `[data-automation-id="${CSS.escape(aid)}"]`
                        : id ? `#${CSS.escape(id)}`
                        : name ? `[name="${CSS.escape(name)}"]`
                        : `[data-jobops-registration-checkbox="${index}"]`;
                    return {automationId: aid, label: label.trim(), selector};
                });
            }"""
        )
    except Exception:
        return ("registration agreement inspection unavailable",)

    unresolved: list[str] = []
    for item in items or ():
        automation_id = normalize_text(item.get("automationId", "")).replace(" ", "")
        label = normalize_text(item.get("label", ""))
        allowed_labels = _REGISTRATION_TERMS_RULES.get(automation_id)
        if allowed_labels is None or label not in allowed_labels:
            unresolved.append(_safe_label(str(item.get("label") or "Required agreement")))
            continue
        try:
            locator = page.locator(str(item["selector"])).first
            if not await locator.is_checked():
                await locator.check()
        except Exception:
            unresolved.append(_safe_label(str(item.get("label") or "Required agreement")))
    return tuple(dict.fromkeys(unresolved))


async def _fill_login(page: Any, email: str, password: str) -> bool:
    email_ok = await _fill_first(page, LOGIN_EMAIL_SELECTORS, email)
    password_ok = await _fill_first(page, PASSWORD_SELECTORS, password)
    return bool(email_ok and password_ok and await _click_named(page, ("Sign In", "Log In")))


async def _fill_registration(
    page: Any,
    email: str,
    password: str,
) -> RegistrationFillResult:
    email_ok = await _fill_first(page, LOGIN_EMAIL_SELECTORS, email)
    await _fill_first(page, CONFIRM_EMAIL_SELECTORS, email)
    password_ok = await _fill_first(page, PASSWORD_SELECTORS, password)
    await _fill_first(page, CONFIRM_PASSWORD_SELECTORS, password)
    unresolved = await _check_registration_terms(page)
    return RegistrationFillResult(
        fields_ready=bool(email_ok and password_ok),
        unresolved_required=unresolved,
    )


async def _click_apply(page: Any) -> bool:
    try:
        locator = page.locator('[data-automation-id="jobPostingApplyButton"]').first
        if await locator.is_visible(timeout=1000):
            await locator.click()
            return True
    except Exception:
        pass
    return await _click_named(page, ("Apply", "Apply Now"))


async def _click_next(page: Any) -> bool:
    for selector in NEXT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=800):
                text = normalize_text(await locator.inner_text())
                if "submit" in text:
                    continue
                await locator.click()
                return True
        except Exception:
            continue
    return await _click_named(page, ("Save and Continue", "Next", "Continue"))


async def _click_submit(page: Any) -> bool:
    for selector in SUBMIT_SELECTORS:
        try:
            locator = page.locator(selector).first
            if not await locator.is_visible(timeout=800):
                continue
            text = normalize_text(await locator.inner_text())
            if text not in {"submit", "submit application"}:
                continue
            await locator.click()
            return True
        except Exception:
            continue
    try:
        locator = page.get_by_role("button", name="Submit", exact=True).first
        if await locator.is_visible(timeout=800):
            await locator.click()
            return True
    except Exception:
        pass
    return False


async def _click_submit_once(page: Any, lock_key: str) -> bool:
    """Acquire an in-page idempotency lock immediately before one submit click."""
    try:
        acquired = await page.evaluate(
            """key => {
                globalThis.__jobopsSubmitLocks ||= Object.create(null);
                if (globalThis.__jobopsSubmitLocks[key]) return false;
                globalThis.__jobopsSubmitLocks[key] = new Date().toISOString();
                return true;
            }""",
            lock_key,
        )
    except Exception:
        return False
    if not acquired:
        return False
    return await _click_submit(page)


async def _select_radio_exact(page: Any, field: WorkdayField, value: Any) -> bool:
    target = normalize_text(value)
    if not target:
        return False
    group_selector = f'input[type="radio"][name="{_css_escape(field.name)}"]' if field.name else field.selector
    locators = page.locator(group_selector)
    try:
        count = await locators.count()
    except Exception:
        count = 0
    for index in range(count):
        option = locators.nth(index)
        try:
            option_value = normalize_text(await option.get_attribute("value"))
            option_label = normalize_text(await option.evaluate(
                """el => (el.labels && el.labels.length)
                    ? Array.from(el.labels).map(label => label.textContent || '').join(' ')
                    : (el.getAttribute('aria-label') || '')"""
            ))
            if target in {option_value, option_label}:
                await option.check()
                return True
        except Exception:
            continue
    try:
        group = page.get_by_role("group", name=field.label, exact=True).first
        option = group.get_by_role("radio", name=str(value), exact=True).first
        if await option.is_visible(timeout=800):
            await option.click()
            return True
    except Exception:
        pass
    try:
        matches = page.get_by_role("radio", name=str(value), exact=True)
        if await matches.count() == 1 and await matches.first.is_visible(timeout=800):
            await matches.first.click()
            return True
    except Exception:
        pass
    return False


async def _select_combobox_exact(page: Any, locator: Any, value: Any) -> bool:
    """Select an exact visible ARIA option from a Workday custom combobox."""
    target = normalize_text(value)
    if not target:
        return False
    try:
        await locator.click()
    except Exception:
        return False
    try:
        exact = page.get_by_role("option", name=str(value), exact=True).first
        if await exact.is_visible(timeout=1000):
            await exact.click()
            return True
    except Exception:
        pass
    options = page.locator('[role="option"]')
    try:
        count = await options.count()
    except Exception:
        return False
    for index in range(count):
        option = options.nth(index)
        try:
            if not await option.is_visible():
                continue
            label = normalize_text(await option.inner_text())
            option_value = normalize_text(await option.get_attribute("data-value"))
            if target in {label, option_value}:
                await option.click()
                return True
        except Exception:
            continue
    # Close an unmatched dropdown without selecting an approximate value.
    try:
        await locator.press("Escape")
    except Exception:
        pass
    return False


def _workday_canonical_key(field: WorkdayField) -> str:
    joined = normalize_text(f"{field.automation_id} {field.name} {field.label}")
    aliases = (
        (("preferred first name", "preferred name", "preferrednamesection"), "preferred_name"),
        (("legal name section first name", "legalnamesection firstname"), "first_name"),
        (("legal name section last name", "legalnamesection lastname"), "last_name"),
        (("phone number", "phone device"), "phone"),
        (("address line 1",), "address"),
        (("postal code",), "postal_code"),
        (("resume upload", "file upload input ref"), "resume"),
    )
    for tokens, canonical in aliases:
        if any(token in joined for token in tokens):
            return canonical
    return canonical_key_for(field.label, f"{field.automation_id} {field.name}", field.kind)


def _is_sensitive_question(label: str) -> bool:
    text = normalize_text(label)
    return any(token in text for token in (
        "gender",
        "sex",
        "race",
        "ethnicity",
        "veteran",
        "disability",
        "indigenous",
        "sexual orientation",
        "transgender",
        "demographic",
        "self identify",
        "work authorization",
        "sponsorship",
        "security clearance",
        "criminal",
    ))


def _uncorrelated_confirmation_outcome(
    context: WorkdayApplicationContext,
) -> ApplicationOutcome:
    """Refuse to count an old or externally-created confirmation page."""

    return ApplicationOutcome.needs_user(
        run_id=context.run_id,
        job_id=context.job_id,
        status=OutcomeStatus.SUBMIT_UNKNOWN,
        phase=OutcomePhase.VERIFY,
        reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
        message=(
            "A Workday confirmation page is visible, but this run has no "
            "correlated permitted submit click"
        ),
        adapter=ADAPTER_NAME,
        checkpoint="workday.confirmation_uncorrelated",
        details={"do_not_count_as_submission": True},
    )


def _confirmed_outcome(
    context: WorkdayApplicationContext,
    signals: WorkdayPageSignals,
) -> ApplicationOutcome:
    marker = next((
        phrase for phrase in (
            "application submitted",
            "application received",
            "thank you for applying",
            "successfully submitted",
            "you have applied",
        ) if phrase in normalize_text(signals.text)
    ), "workday confirmation marker")
    evidence_items = [EvidenceRef(
        kind=EvidenceKind.CONFIRMATION_TEXT,
        metadata={"marker": marker},
    )]
    if signals.url:
        sanitized_url = _sanitize_confirmation_url(signals.url)
        if sanitized_url:
            evidence_items.append(EvidenceRef(
                kind=EvidenceKind.CONFIRMATION_URL,
                sha256=_sha256_text(sanitized_url),
                metadata={"source": "workday_confirmation_url"},
            ))
    return ApplicationOutcome.submitted_verified(
        run_id=context.run_id,
        job_id=context.job_id,
        adapter=ADAPTER_NAME,
        evidence_refs=tuple(evidence_items),
        details={"checkpoint": "workday.confirmation"},
    )


def _needs_user(
    context: WorkdayApplicationContext,
    status: OutcomeStatus,
    reason: ReasonCode | str,
    message: str,
    *,
    checkpoint: str,
    details: Mapping[str, Any] | None = None,
) -> ApplicationOutcome:
    return ApplicationOutcome.needs_user(
        run_id=context.run_id,
        job_id=context.job_id,
        status=status,
        phase=OutcomePhase.AUTHENTICATE if ".auth." in checkpoint else OutcomePhase.FILL,
        reason_code=reason,
        message=message,
        adapter=ADAPTER_NAME,
        checkpoint=checkpoint,
        details=details,
    )


def _missing_fact(context: WorkdayApplicationContext, message: str) -> ApplicationOutcome:
    return _needs_user(
        context,
        OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
        ReasonCode.SENSITIVE_ANSWER_REQUIRED,
        message,
        checkpoint="workday.auth.missing_identity",
    )


def _retryable(
    context: WorkdayApplicationContext,
    phase: OutcomePhase,
    message: str,
    exc: Exception,
) -> ApplicationOutcome:
    return ApplicationOutcome(
        run_id=context.run_id,
        job_id=context.job_id,
        status=OutcomeStatus.FAILED_RETRYABLE,
        phase=phase,
        reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
        message=message,
        adapter=ADAPTER_NAME,
        retryable=True,
        checkpoint="workday.browser",
        details={"error_type": type(exc).__name__},
    )


def _mailbox_reason(status: MailboxVerificationStatus) -> str:
    return {
        MailboxVerificationStatus.AMBIGUOUS: "MAILBOX_MATCH_AMBIGUOUS",
        MailboxVerificationStatus.UNAVAILABLE: "MAILBOX_UNAVAILABLE",
        MailboxVerificationStatus.NOT_FOUND: "MAILBOX_MESSAGE_NOT_FOUND",
        MailboxVerificationStatus.UNSAFE: "MAILBOX_MATCH_UNSAFE",
    }.get(status, ReasonCode.EMAIL_VERIFICATION.value)


def _profile_email(profile: Mapping[str, Any]) -> str:
    return str(_nested(profile, "personal.email") or _nested(profile, "email") or "").strip()


def _workday_config(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    value = profile.get("workday", {})
    return value if isinstance(value, Mapping) else {}


def _nested(mapping: Mapping[str, Any], path: str) -> Any:
    current: Any = mapping
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = normalize_text(value)
    if normalized in {"yes", "true", "1"}:
        return True
    if normalized in {"no", "false", "0"}:
        return False
    return None


def _contains_any(text: str, phrases: Sequence[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _safe_label(label: str) -> str:
    # Labels are useful for handoff but bound their size to avoid page dumps.
    return " ".join(label.split())[:180]


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _legacy_job_id(job_url: str) -> str:
    return f"workday-{hashlib.sha256(job_url.encode('utf-8')).hexdigest()[:16]}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        payload = "unserializable-answer-projection"
    return _sha256_text(payload)


def _file_sha256(path: str | None) -> str | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_workday_session_url(current_url: str, job_url: str) -> bool:
    """Preserve only a checkpoint provably belonging to this posting."""

    target = _workday_posting_identity(job_url)
    if target is None:
        return False
    signals = WorkdayPageSignals(text="", url=current_url)
    return _workday_identity_status(target, signals) == "match"


def _canonical_origin(parsed: Any) -> str:
    scheme = str(parsed.scheme or "").casefold()
    hostname = str(parsed.hostname or "").casefold().rstrip(".")
    if scheme not in {"http", "https"} or not hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    default = 443 if scheme == "https" else 80
    authority = hostname if port in {None, default} else f"{hostname}:{port}"
    return f"{scheme}://{authority}"


def _normalized_url_path(value: str) -> str:
    segments = [segment for segment in str(value or "").split("/") if segment]
    return "/" + "/".join(segments) if segments else "/"


def _query_requisition_id(parsed: Any) -> str:
    query = parse_qs(parsed.query, keep_blank_values=False)
    for key in (
        "jobid",
        "job_id",
        "requisitionid",
        "requisition_id",
        "jobreqid",
    ):
        for candidate_key, values in query.items():
            if candidate_key.casefold() == key and values:
                return normalize_text(values[0]).replace(" ", "")
    return ""


def _workday_posting_identity(url: str) -> WorkdayPostingIdentity | None:
    parsed = urlsplit(str(url or ""))
    if not is_trusted_workday_host(parsed.hostname or ""):
        return None
    origin = _canonical_origin(parsed)
    if not origin:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    lowered = [segment.casefold() for segment in segments]
    try:
        job_index = lowered.index("job")
    except ValueError:
        job_index = -1
    posting_path = ""
    if job_index >= 0 and job_index + 1 < len(segments):
        end = len(segments)
        stage_tokens = {
            "apply",
            "autofillwithresume",
            "myinformation",
            "myexperience",
            "applicationquestions",
            "voluntarydisclosures",
            "selfidentify",
            "review",
            "confirmation",
        }
        for index in range(job_index + 1, len(segments)):
            if lowered[index].replace("-", "").replace("_", "") in stage_tokens:
                end = index
                break
        posting_path = _normalized_url_path("/".join(segments[:end]))
    requisition_id = _query_requisition_id(parsed)
    if not posting_path and not requisition_id:
        return None
    return WorkdayPostingIdentity(
        origin=origin,
        posting_path=posting_path,
        requisition_id=requisition_id,
    )


def _identity_url_status(
    requested: WorkdayPostingIdentity,
    value: str,
) -> str:
    parsed = urlsplit(str(value or ""))
    if _canonical_origin(parsed) != requested.origin:
        return "mismatch" if parsed.hostname else "unknown"
    active_requisition = _query_requisition_id(parsed)
    if requested.requisition_id and active_requisition:
        return "match" if active_requisition == requested.requisition_id else "mismatch"
    active_path = _normalized_url_path(parsed.path)
    if requested.posting_path and (
        active_path == requested.posting_path
        or active_path.startswith(f"{requested.posting_path}/")
    ):
        return "match"
    active_identity = _workday_posting_identity(value)
    if active_identity is not None and active_identity.posting_path:
        return "mismatch"
    return "unknown"


def _workday_identity_status(
    requested: WorkdayPostingIdentity,
    signals: WorkdayPageSignals,
) -> str:
    primary = _identity_url_status(requested, signals.url)
    if primary in {"match", "mismatch"}:
        return primary
    statuses = [
        _identity_url_status(requested, item) for item in signals.posting_urls
    ]
    if "match" in statuses:
        return "match"
    if "mismatch" in statuses:
        return "mismatch"
    return "unknown"


def _sanitize_confirmation_url(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    origin = _canonical_origin(parsed)
    if not origin:
        return ""
    origin_parts = urlsplit(origin)
    return urlunsplit((
        origin_parts.scheme,
        origin_parts.netloc,
        _normalized_url_path(parsed.path),
        "",
        "",
    ))


def _workday_identity_failure(
    context: WorkdayApplicationContext,
    message: str,
    *,
    active_url: str = "",
) -> ApplicationOutcome:
    requested = _workday_posting_identity(context.job_url)
    requested_digest = _sha256_text(
        f"{requested.origin}{requested.posting_path}:{requested.requisition_id}"
        if requested
        else "unknown"
    )
    active_digest = _sha256_text(_sanitize_confirmation_url(active_url) or "unknown")
    return ApplicationOutcome(
        run_id=context.run_id,
        job_id=context.job_id,
        status=OutcomeStatus.FAILED_TERMINAL,
        phase=OutcomePhase.INSPECT,
        reason_code=ReasonCode.VALIDATION_FAILED,
        message=message,
        adapter=ADAPTER_NAME,
        checkpoint="workday.identity",
        details={
            "requested_identity_sha256": requested_digest,
            "active_identity_sha256": active_digest,
        },
    )


def _protocol_kind(kind: str) -> FieldKind:
    aliases = {
        "select-one": FieldKind.SELECT,
        "select-multiple": FieldKind.SELECT,
        "combobox": FieldKind.SELECT,
    }
    if kind in aliases:
        return aliases[kind]
    try:
        return FieldKind(kind)
    except ValueError:
        return FieldKind.TEXT


def _coerce_context(
    context: WorkdayApplicationContext | ProtocolApplicationContext,
    *,
    credential_store: CredentialStore | None = None,
    mailbox_verifier: MailboxVerifier | None = None,
) -> WorkdayApplicationContext:
    if isinstance(context, WorkdayApplicationContext):
        if context.credential_store is None and credential_store is not None:
            context.credential_store = credential_store
        if context.mailbox_verifier is None and mailbox_verifier is not None:
            context.mailbox_verifier = mailbox_verifier
        return context
    return WorkdayApplicationContext(
        page=context.page,
        job_url=context.job_url,
        profile=context.profile,
        job_id=context.job_id,
        run_id=context.run_id,
        resume_path=str(context.resume_path) if context.resume_path else None,
        cover_letter=context.cover_letter,
        answers=context.answers,
        request_submit=context.request_submit,
        gate_b_permit=context.gate_b_permit,
        gate_b_validator=context.gate_b_validator,
        persisted_review_attestation=getattr(
            context, "persisted_review_attestation", ""
        ),
        credential_store=getattr(context, "credential_store", None) or credential_store,
        mailbox_verifier=getattr(context, "mailbox_verifier", None) or mailbox_verifier,
        initiated_at=getattr(context, "initiated_at", datetime.now(timezone.utc)),
        max_steps=int(getattr(context, "max_steps", 40)),
        navigate=context.navigate,
        navigation_timeout_ms=context.navigation_timeout_ms,
        settle_timeout_ms=context.settle_timeout_ms,
        materials=context.materials,
        private_home=context.private_home,
    )


async def _settle(page: Any) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        await page.wait_for_timeout(750)
    except Exception:
        await asyncio.sleep(0.75)
