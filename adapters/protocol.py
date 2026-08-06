"""Versioned deterministic contract shared by ATS adapters.

The protocol is intentionally browser-implementation focused.  Codex may
orchestrate it, but an adapter never calls an LLM and never invents an answer.
Every adapter stops at ``REVIEW_READY`` unless the core validates an opaque
Gate B permit for the exact review fingerprint.
"""

from __future__ import annotations

import hashlib
import base64
import json
import re
import time
from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.application_answer_taxonomy import (
    CanonicalApplicationAnswerKey,
    normalize_canonical_application_answer_key,
)
from core.application_execution_profile import (
    ApplicationExecutionIdentityProfile,
)
from core.bundles import MaterialBundle
from core.private_home import PrivateHome

from .shared import (
    FieldSpec,
    canonical_key_for,
    css_string,
    element_label,
    first_locator,
    invoke_gate_b_validator,
    is_sensitive_question,
    normalize_text,
    resolve_confirmed_value,
    select_exact_option,
    unique,
)
from .document_upload import (
    ApplicationDocumentUploadFailure,
    ApplicationDocumentUploadPlanStatus,
    document_control_id,
    plan_application_document_uploads,
)


PROTOCOL_VERSION = "jobops.adapter/v2"
REVIEW_BINDING_VERSION = "review-binding-v4"


class FieldKind(str, Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    EMAIL = "email"
    TEL = "tel"
    URL = "url"
    SELECT = "select"
    COMBOBOX = "combobox"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    FILE = "file"


@dataclass(frozen=True)
class AdapterSupport:
    adapter: str
    supported: bool
    confidence: float
    reason: str
    protocol_version: str = PROTOCOL_VERSION


@dataclass(frozen=True)
class FieldIR:
    canonical_key: CanonicalApplicationAnswerKey
    label: str
    selectors: tuple[str, ...]
    kind: FieldKind
    required: bool = False
    name: str = ""
    element_id: str = ""
    options: tuple[tuple[str, str], ...] = ()
    sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_key",
            normalize_canonical_application_answer_key(
                self.canonical_key,
                allow_legacy_alias=True,
                allow_custom_unknown=True,
            ),
        )

    @property
    def is_custom(self) -> bool:
        return self.canonical_key is CanonicalApplicationAnswerKey.UNKNOWN


@dataclass(frozen=True)
class FormIR:
    adapter: str
    url: str
    fields: tuple[FieldIR, ...]
    submit_selectors: tuple[str, ...]
    confirmation_selectors: tuple[str, ...]
    review_selectors: tuple[str, ...] = ()
    signature: str = ""
    protocol_version: str = PROTOCOL_VERSION


@dataclass(frozen=True)
class ApplicationContext:
    page: Any
    job_url: str
    job_id: str
    run_id: str
    profile: ApplicationExecutionIdentityProfile | Mapping[str, Any]
    resume_path: str | Path | None = None
    cover_letter: str = ""
    answers: Mapping[str, Any] = field(default_factory=dict)
    request_submit: bool = False
    gate_b_permit: Any = None
    gate_b_validator: Callable[..., Any] | None = None
    persisted_review_attestation: str = ""
    navigate: bool = True
    navigation_timeout_ms: int = 30_000
    settle_timeout_ms: int = 250
    submission_evidence_timeout_ms: int = 10_000
    materials: MaterialBundle | None = None
    private_home: PrivateHome | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.profile, ApplicationExecutionIdentityProfile):
            object.__setattr__(
                self,
                "profile",
                ApplicationExecutionIdentityProfile.from_application_bundle_profile(
                    self.profile
                ),
            )


@dataclass(frozen=True)
class UnresolvedField:
    canonical_key: CanonicalApplicationAnswerKey
    label: str
    reason: str
    sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_key",
            normalize_canonical_application_answer_key(
                self.canonical_key,
                allow_legacy_alias=True,
                allow_custom_unknown=True,
            ),
        )


@dataclass(frozen=True)
class FillReport:
    filled_fields: tuple[str, ...] = ()
    uploaded_files: tuple[str, ...] = ()
    unresolved_required: tuple[UnresolvedField, ...] = ()
    errors: tuple[str, ...] = ()
    document_upload_failure: ApplicationDocumentUploadFailure | None = None


@dataclass(frozen=True)
class ValidationReport:
    valid: bool
    missing_required: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewDigest:
    fingerprint: str
    adapter: str
    job_id: str
    filled_fields: tuple[str, ...]
    uploaded_files: tuple[str, ...]
    unresolved_required: tuple[str, ...]
    validation_errors: tuple[str, ...]
    submit_control_present: bool
    review_marker_present: bool
    ready: bool
    readback_digest: str = ""
    material_content_digest: str = ""

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "binding_version": REVIEW_BINDING_VERSION,
            "fingerprint": self.fingerprint,
            "adapter": self.adapter,
            "job_id": self.job_id,
            "filled_fields": list(self.filled_fields),
            "uploaded_files": list(self.uploaded_files),
            "unresolved_required": list(self.unresolved_required),
            "validation_errors": list(self.validation_errors),
            "submit_control_present": self.submit_control_present,
            "review_marker_present": self.review_marker_present,
            "ready": self.ready,
            "readback_digest": self.readback_digest,
            "material_content_digest": self.material_content_digest,
        }


@dataclass(frozen=True)
class SubmissionEvidence:
    confirmation_text: str = ""
    confirmation_url: str = ""
    ats_application_id: str = ""

    @property
    def verified(self) -> bool:
        return bool(self.confirmation_text or self.confirmation_url or self.ats_application_id)

    def as_evidence_refs(self) -> tuple[EvidenceRef, ...]:
        refs: list[EvidenceRef] = []
        if self.confirmation_text:
            digest = hashlib.sha256(self.confirmation_text.encode("utf-8")).hexdigest()
            refs.append(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    sha256=digest,
                    metadata={"source": "ats_confirmation_dom"},
                )
            )
        if self.confirmation_url:
            refs.append(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_URL,
                    sha256=hashlib.sha256(
                        self.confirmation_url.encode("utf-8")
                    ).hexdigest(),
                    metadata={"source": "ats_confirmation_url"},
                )
            )
        if self.ats_application_id:
            refs.append(
                EvidenceRef(
                    kind=EvidenceKind.ATS_APPLICATION_ID,
                    sha256=hashlib.sha256(
                        self.ats_application_id.encode("utf-8")
                    ).hexdigest(),
                    metadata={"source": "ats_confirmation_dom"},
                )
            )
        return tuple(refs)


_POSITIVE_CONFIRMATION_RE = re.compile(
    r"\b(thank you|thanks for applying|application (?:was )?(?:received|submitted)|"
    r"successfully submitted|submission (?:was )?received|application complete)\b",
    re.IGNORECASE,
)
_CONFIRMATION_URL_RE = re.compile(
    r"(?:confirmation|thank[-_]?you|thanks(?:[-_/]?for[-_/]?applying)?|"
    r"application[-_]?submitted|submitted=true)",
    re.IGNORECASE,
)


class BaseATSAdapter(ABC):
    """Template method implementation for a deterministic ATS application."""

    name: str
    host_patterns: tuple[str, ...] = ()
    field_specs: tuple[FieldSpec, ...] = ()
    submit_selectors: tuple[str, ...] = (
        'button[type="submit"]',
        'input[type="submit"]',
    )
    confirmation_selectors: tuple[str, ...] = ()
    review_selectors: tuple[str, ...] = ()
    dom_markers: tuple[str, ...] = ()

    async def support(self, page: Any, url: str) -> AdapterSupport:
        host = (urlparse(url or "").hostname or "").casefold()
        host_match = any(
            host == pattern.casefold() or host.endswith(f".{pattern.casefold()}")
            for pattern in self.host_patterns
        )
        dom_match = False
        if self.dom_markers:
            locator, _ = await first_locator(page, self.dom_markers)
            dom_match = locator is not None
        supported = host_match or dom_match
        return AdapterSupport(
            adapter=self.name,
            supported=supported,
            confidence=1.0 if host_match and dom_match else (0.9 if host_match else 0.75 if dom_match else 0.0),
            reason="host and DOM matched" if host_match and dom_match else "host matched" if host_match else "DOM matched" if dom_match else "no deterministic ATS marker",
        )

    async def inspect(self, page: Any) -> FormIR:
        fields: list[FieldIR] = []
        claimed: set[tuple[str, str]] = set()

        for spec in self.field_specs:
            locator, selector = await first_locator(page, spec.selectors)
            if locator is None or selector is None:
                continue
            attrs = await locator.evaluate(
                """element => ({
                    id: element.id || '',
                    name: element.name || '',
                    type: (element.type || element.tagName || 'text').toLowerCase(),
                    tag: element.tagName.toLowerCase(),
                    role: (element.getAttribute('role') || '').toLowerCase(),
                    required: Boolean(element.required) || element.getAttribute('aria-required') === 'true',
                    options: element.tagName === 'SELECT'
                        ? Array.from(element.options).map(option => [option.value, option.textContent || ''])
                        : []
                })"""
            )
            kind = _kind_from_attributes(attrs, spec.kind)
            label = await element_label(locator, spec.label or spec.canonical_key.replace("_", " ").title())
            canonical_key = spec.canonical_key
            if (
                kind is FieldKind.FILE
                and canonical_key is CanonicalApplicationAnswerKey.COVER_LETTER
            ):
                canonical_key = (
                    CanonicalApplicationAnswerKey.COVER_LETTER_FILE
                )
            fields.append(
                FieldIR(
                    canonical_key=canonical_key,
                    label=label,
                    selectors=(selector,),
                    kind=kind,
                    required=bool(attrs["required"]),
                    name=attrs["name"],
                    element_id=attrs["id"],
                    options=tuple(tuple(option) for option in attrs["options"]),
                    sensitive=is_sensitive_question(label, attrs["name"]),
                )
            )
            claimed.add((attrs["id"], attrs["name"]))

        # Capture only additional required controls.  This is a compact local
        # representation, not a page snapshot, and therefore remains cheap.
        required_controls = await page.evaluate(
            """() => Array.from(document.querySelectorAll(
                'input[type="file"], input[required], textarea[required], select[required], '
                + 'input[aria-required="true"], textarea[aria-required="true"], '
                + 'select[aria-required="true"]'
            )).filter(element => {
                if (element.disabled || ['hidden', 'submit', 'button'].includes(
                    (element.type || '').toLowerCase()
                )) return false;
                const validatingSelectInput = element.tabIndex < 0
                    && String(element.className || '').includes('requiredInput')
                    && element.closest('.select-shell');
                const validatingCheckboxInput = Boolean(element.id)
                    && Array.from(document.querySelectorAll('input[type="checkbox"]'))
                        .some(item => item.name === element.id);
                return !validatingSelectInput && !validatingCheckboxInput;
            })
            .map((element, index) => {
                if (!element.id && !element.name && !element.dataset.jobopsInspectId) {
                    element.dataset.jobopsInspectId = String(index);
                }
                const ownLabel = element.getAttribute('aria-label') ||
                    (element.labels && element.labels.length
                        ? Array.from(element.labels).map(item => item.innerText || item.textContent || '').join(' ')
                        : '');
                const fieldset = ['checkbox', 'radio'].includes(
                    (element.type || '').toLowerCase()
                ) ? element.closest('fieldset') : null;
                const legend = fieldset && fieldset.querySelector('legend');
                const label = (legend && (legend.innerText || legend.textContent))
                    || ownLabel;
                let selector;
                if (element.id) selector = '#' + CSS.escape(element.id);
                else if (element.name) selector = element.tagName.toLowerCase() + '[name=' + JSON.stringify(element.name) + ']';
                else selector = '[data-jobops-inspect-id=' + JSON.stringify(element.dataset.jobopsInspectId) + ']';
                return {
                    id: element.id || '',
                    name: element.name || '',
                    type: (element.type || element.tagName || 'text').toLowerCase(),
                    tag: element.tagName.toLowerCase(),
                    role: (element.getAttribute('role') || '').toLowerCase(),
                    label: label.trim(),
                    selector,
                    options: element.tagName === 'SELECT'
                        ? Array.from(element.options).map(option => [option.value, option.textContent || ''])
                        : []
                };
            })"""
        )
        for raw in required_controls:
            if (raw["id"], raw["name"]) in claimed:
                continue
            canonical_key = canonical_key_for(raw["label"], raw["name"], raw["type"])
            # A known semantic field may already have been claimed through a
            # different duplicate selector. Keep the first stable field.
            if (
                raw["type"] != "file"
                and any(
                    field.canonical_key == canonical_key
                    and not field.is_custom
                    for field in fields
                )
            ):
                continue
            fields.append(
                FieldIR(
                    canonical_key=canonical_key,
                    label=raw["label"] or raw["name"] or "Unnamed required field",
                    selectors=(raw["selector"],),
                    kind=_kind_from_attributes(raw),
                    required=True,
                    name=raw["name"],
                    element_id=raw["id"],
                    options=tuple(tuple(option) for option in raw["options"]),
                    sensitive=is_sensitive_question(raw["label"], raw["name"]),
                )
            )

        signature_payload = [
            {
                "key": item.canonical_key,
                "kind": item.kind.value,
                "required": item.required,
                "sensitive": item.sensitive,
                "options": [normalize_text(label) for _, label in item.options],
            }
            for item in fields
        ]
        signature_payload.sort(
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":")
            )
        )
        signature = hashlib.sha256(
            json.dumps(signature_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return FormIR(
            adapter=self.name,
            url=page.url,
            fields=tuple(fields),
            submit_selectors=self.submit_selectors,
            confirmation_selectors=self.confirmation_selectors,
            review_selectors=self.review_selectors,
            signature=signature,
        )

    async def fill(self, page: Any, context: ApplicationContext, form: FormIR) -> FillReport:
        filled: list[str] = []
        uploaded: list[str] = []
        unresolved: list[UnresolvedField] = []
        errors: list[str] = []
        resume_path = context.resume_path
        document_upload_failure = None
        upload_items = {}
        if context.materials is not None:
            if context.private_home is None:
                document_upload_failure = (
                    ApplicationDocumentUploadFailure
                    .ARTIFACT_INTEGRITY_FAILURE
                )
            else:
                upload_result = plan_application_document_uploads(
                    form=form,
                    materials=context.materials,
                    private_home=context.private_home,
                )
                if (
                    upload_result.status
                    is ApplicationDocumentUploadPlanStatus.FAILED
                ):
                    document_upload_failure = upload_result.failure
                elif upload_result.plan is not None:
                    upload_items = {
                        item.control_id: item
                        for item in upload_result.plan.items
                    }
        location_constraints = tuple(
            str(value)
            for key in (
                CanonicalApplicationAnswerKey.STATE,
                CanonicalApplicationAnswerKey.COUNTRY,
            )
            if (
                value := resolve_confirmed_value(
                    key,
                    key.value,
                    profile=context.profile,
                    answers=context.answers,
                    cover_letter=context.cover_letter,
                )
            )
        )

        for form_field in form.fields:
            locator, _ = await first_locator(page, form_field.selectors)
            if locator is None:
                if form_field.required:
                    unresolved.append(UnresolvedField(form_field.canonical_key, form_field.label, "field disappeared", form_field.sensitive))
                continue
            try:
                if form_field.kind is FieldKind.FILE:
                    if context.materials is not None:
                        item = upload_items.get(
                            document_control_id(form_field)
                        )
                        if item is not None:
                            await locator.set_input_files(str(item.resolved_path))
                            if await _file_upload_is_recognized(
                                page, form_field, item.resolved_path
                            ):
                                uploaded.append(
                                    item.canonical_material_key
                                )
                                filled.append(
                                    item.canonical_material_key
                                )
                            elif form_field.required:
                                unresolved.append(
                                    UnresolvedField(
                                        form_field.canonical_key,
                                        form_field.label,
                                        "document upload was not recognized",
                                        form_field.sensitive,
                                    )
                                )
                        elif form_field.required:
                            unresolved.append(
                                UnresolvedField(
                                    form_field.canonical_key,
                                    form_field.label,
                                    "required document upload is unavailable",
                                    form_field.sensitive,
                                )
                            )
                        continue
                    if form_field.canonical_key == "resume" and resume_path:
                        path = Path(resume_path).expanduser()
                        if not path.is_file():
                            if form_field.required:
                                unresolved.append(UnresolvedField("resume", form_field.label, "resume file is missing", form_field.sensitive))
                            continue
                        await locator.set_input_files(str(path))
                        if await _file_upload_is_recognized(
                            page, form_field, path
                        ):
                            # Record the artifact role, never the private local
                            # filename (which often contains the applicant name).
                            uploaded.append(form_field.canonical_key)
                            filled.append(form_field.canonical_key)
                        elif form_field.required:
                            unresolved.append(UnresolvedField("resume", form_field.label, "resume upload was not recognized", form_field.sensitive))
                    elif form_field.required:
                        unresolved.append(UnresolvedField(form_field.canonical_key, form_field.label, "required file is unavailable", form_field.sensitive))
                    continue

                value = resolve_confirmed_value(
                    form_field.canonical_key,
                    form_field.label,
                    profile=context.profile,
                    answers=context.answers,
                    cover_letter=context.cover_letter,
                )
                if value is None or value == "":
                    if form_field.required:
                        unresolved.append(
                            UnresolvedField(
                                form_field.canonical_key,
                                form_field.label,
                                "no exact confirmed answer",
                                form_field.sensitive,
                            )
                        )
                    continue

                was_filled = await _fill_locator(
                    page,
                    locator,
                    form_field,
                    value,
                    location_constraints=location_constraints,
                )
                if was_filled:
                    filled.append(form_field.canonical_key)
                elif form_field.required:
                    unresolved.append(
                        UnresolvedField(form_field.canonical_key, form_field.label, "confirmed answer did not match a form option", form_field.sensitive)
                    )
            except Exception as exc:
                errors.append(f"{form_field.canonical_key}: {type(exc).__name__}")
                if form_field.required:
                    unresolved.append(UnresolvedField(form_field.canonical_key, form_field.label, "deterministic fill failed", form_field.sensitive))

        return FillReport(
            filled_fields=unique(filled),
            uploaded_files=unique(uploaded),
            unresolved_required=tuple(_unique_unresolved(unresolved)),
            errors=unique(errors),
            document_upload_failure=document_upload_failure,
        )

    async def validate(self, page: Any, form: FormIR, fill: FillReport) -> ValidationReport:
        missing: list[str] = [item.label for item in fill.unresolved_required]
        errors: list[str] = list(fill.errors)
        if fill.document_upload_failure is not None:
            errors.append(
                "document_upload:"
                f"{fill.document_upload_failure.value}"
            )
        for form_field in form.fields:
            if not form_field.required:
                continue
            locator, _ = await first_locator(page, form_field.selectors)
            if locator is None:
                if (
                    form_field.kind is FieldKind.FILE
                    and form_field.canonical_key in fill.uploaded_files
                ):
                    continue
                missing.append(form_field.label)
                continue
            try:
                populated = await _is_populated(page, locator, form_field)
                if not populated:
                    missing.append(form_field.label)
            except Exception as exc:
                errors.append(f"{form_field.canonical_key}: {type(exc).__name__}")
                missing.append(form_field.label)
        missing_tuple = unique(missing)
        return ValidationReport(
            valid=not missing_tuple and not errors,
            missing_required=missing_tuple,
            errors=unique(errors),
        )

    async def prepare_review(
        self,
        page: Any,
        context: ApplicationContext,
        form: FormIR,
        fill: FillReport,
        validation: ValidationReport,
    ) -> ReviewDigest:
        unresolved_labels = tuple(item.label for item in fill.unresolved_required)
        submit_control, _ = await first_locator(page, form.submit_selectors)
        if submit_control is None and context.navigate and form.submit_selectors:
            # React ATS forms may briefly unmount the footer while a resume
            # upload is parsed.  Do not turn that transient render gap into a
            # human-attention blocker; wait for the deterministic control to
            # return before concluding that the form is unsupported.
            deadline = time.monotonic() + 5.0
            while submit_control is None and time.monotonic() < deadline:
                await page.wait_for_timeout(100)
                submit_control, _ = await first_locator(
                    page, form.submit_selectors
                )
        submit_control_present = submit_control is not None
        review_marker_present = False
        if form.review_selectors:
            review_marker, _ = await first_locator(page, form.review_selectors)
            review_marker_present = review_marker is not None
        review_errors = list(validation.missing_required + validation.errors)
        if not submit_control_present:
            review_errors.append("Final submit control was not found")
        readback_digest, material_content_digest, binding_errors = (
            await _review_binding_digests(page, context, form, fill)
        )
        review_errors.extend(binding_errors)
        payload = {
            "protocol": PROTOCOL_VERSION,
            "review_binding": REVIEW_BINDING_VERSION,
            "adapter": self.name,
            "job_id": context.job_id,
            "form_signature": form.signature,
            "filled_fields": sorted(fill.filled_fields),
            "uploaded_files": sorted(fill.uploaded_files),
            "unresolved": sorted(unresolved_labels),
            "validation": sorted(review_errors),
            "submit_control_present": submit_control_present,
            "review_marker_present": review_marker_present,
            # The raw browser values and private filenames never enter this
            # payload.  Gate B is nevertheless bound to exact, freshly read
            # browser state and the bytes attached to every uploaded control.
            "readback_digest": readback_digest,
            "material_content_digest": material_content_digest,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ReviewDigest(
            fingerprint=fingerprint,
            adapter=self.name,
            job_id=context.job_id,
            filled_fields=fill.filled_fields,
            uploaded_files=fill.uploaded_files,
            unresolved_required=unresolved_labels,
            validation_errors=unique(review_errors),
            submit_control_present=submit_control_present,
            review_marker_present=review_marker_present,
            ready=(
                validation.valid
                and not unresolved_labels
                and submit_control_present
                and not binding_errors
            ),
            readback_digest=readback_digest,
            material_content_digest=material_content_digest,
        )

    async def submit(self, page: Any, context: ApplicationContext, review: ReviewDigest) -> bool:
        """Click submit at most once for a job/review pair."""

        submit, _ = await first_locator(page, self.submit_selectors)
        if submit is None:
            raise RuntimeError("final submit control disappeared after review")
        lock_key = f"{context.job_id}:{review.fingerprint}"
        acquired = await page.evaluate(
            """key => {
                globalThis.__jobopsSubmitLocks ||= Object.create(null);
                if (globalThis.__jobopsSubmitLocks[key]) return false;
                globalThis.__jobopsSubmitLocks[key] = new Date().toISOString();
                return true;
            }""",
            lock_key,
        )
        if not acquired:
            return False
        await submit.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=2_000)
        except Exception:
            pass
        if context.settle_timeout_ms:
            await page.wait_for_timeout(context.settle_timeout_ms)
        return True

    async def verify_submission(self, page: Any, context: ApplicationContext) -> SubmissionEvidence:
        for selector in self.confirmation_selectors:
            locator = page.locator(selector).first
            try:
                if not await locator.count() or not await locator.is_visible():
                    continue
                text = " ".join((await locator.inner_text()).split())
                if text and _POSITIVE_CONFIRMATION_RE.search(text):
                    application_id = await locator.get_attribute("data-application-id") or ""
                    return SubmissionEvidence(
                        confirmation_text=text,
                        confirmation_url=page.url if _CONFIRMATION_URL_RE.search(page.url) else "",
                        ats_application_id=application_id,
                    )
            except Exception:
                continue
        if _CONFIRMATION_URL_RE.search(page.url):
            return SubmissionEvidence(confirmation_url=page.url)
        return SubmissionEvidence()

    async def wait_for_submission_evidence(
        self, page: Any, context: ApplicationContext
    ) -> SubmissionEvidence:
        """Poll for delayed ATS confirmation after the one permitted click."""

        timeout_ms = max(0, int(context.submission_evidence_timeout_ms))
        deadline = time.monotonic() + (timeout_ms / 1_000)
        while True:
            evidence = await self.verify_submission(page, context)
            if evidence.verified or time.monotonic() >= deadline:
                return evidence
            await page.wait_for_timeout(
                min(250, max(1, int((deadline - time.monotonic()) * 1_000)))
            )

    async def run(self, context: ApplicationContext) -> ApplicationOutcome:
        current_phase = OutcomePhase.INSPECT
        try:
            if context.navigate:
                await context.page.goto(
                    context.job_url,
                    wait_until="domcontentloaded",
                    timeout=context.navigation_timeout_ms,
                )
                if context.settle_timeout_ms:
                    await context.page.wait_for_timeout(context.settle_timeout_ms)

            support = await self.support(context.page, context.job_url)
            if not support.supported:
                return self._outcome(
                    context,
                    OutcomeStatus.FAILED_UNSUPPORTED,
                    OutcomePhase.INSPECT,
                    ReasonCode.UNSUPPORTED_ATS,
                    support.reason,
                )
            current_phase = OutcomePhase.INSPECT
            form = await self.inspect(context.page)
            current_phase = OutcomePhase.FILL
            fill = await self.fill(context.page, context, form)
            current_phase = OutcomePhase.VALIDATE
            validation = await self.validate(context.page, form, fill)
            current_phase = OutcomePhase.REVIEW
            review = await self.prepare_review(context.page, context, form, fill, validation)
            details = {"protocol_version": PROTOCOL_VERSION, "review": review.to_safe_dict()}

            if not review.ready:
                has_sensitive = any(item.sensitive for item in fill.unresolved_required)
                has_unknown = any(
                    item.canonical_key
                    is CanonicalApplicationAnswerKey.UNKNOWN
                    for item in fill.unresolved_required
                )
                if has_sensitive:
                    reason = ReasonCode.SENSITIVE_ANSWER_REQUIRED
                elif has_unknown:
                    reason = ReasonCode.UNKNOWN_REQUIRED_QUESTION
                elif review.validation_errors:
                    reason = ReasonCode.VALIDATION_FAILED
                else:
                    reason = ReasonCode.MISSING_MATERIAL
                return self._outcome(
                    context,
                    OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER if has_sensitive else OutcomeStatus.NEEDS_USER,
                    OutcomePhase.VALIDATE,
                    reason,
                    "Required fields need confirmed user data or correction.",
                    details=details,
                )

            if not context.request_submit:
                return self._outcome(
                    context,
                    OutcomeStatus.REVIEW_READY,
                    OutcomePhase.REVIEW,
                    ReasonCode.REVIEW_COMPLETE,
                    "Application is filled and validated; no submit action was taken.",
                    checkpoint=review.fingerprint,
                    details=details,
                )

            preexisting_evidence = await self.verify_submission(
                context.page, context
            )
            if preexisting_evidence.verified:
                return self._outcome(
                    context,
                    OutcomeStatus.SUBMIT_UNKNOWN,
                    OutcomePhase.VERIFY,
                    ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
                    "Confirmation was already present before this run's permitted click; reconcile manually and do not retry.",
                    checkpoint=review.fingerprint,
                    details={**details, "uncorrelated_confirmation": True},
                )

            permit_valid = await invoke_gate_b_validator(
                context.gate_b_validator,
                context.gate_b_permit,
                job_id=context.job_id,
                run_id=context.run_id,
                review_fingerprint=review.fingerprint,
            )
            if not permit_valid:
                return self._outcome(
                    context,
                    OutcomeStatus.AWAITING_GATE_B,
                    OutcomePhase.REVIEW,
                    ReasonCode.GATE_B_REQUIRED,
                    "A valid Gate B permit bound to this review is required.",
                    checkpoint=review.fingerprint,
                    details=details,
                )

            current_phase = OutcomePhase.SUBMIT
            clicked = await self.submit(context.page, context, review)
            if not clicked:
                return self._outcome(
                    context,
                    OutcomeStatus.SKIPPED_POLICY,
                    OutcomePhase.SUBMIT,
                    ReasonCode.DUPLICATE_SUBMISSION,
                    "Submit was not clicked because the review was already submitted or the control was unavailable.",
                    checkpoint=review.fingerprint,
                    details=details,
                )
            current_phase = OutcomePhase.VERIFY
            evidence = await self.wait_for_submission_evidence(
                context.page, context
            )
            if not evidence.verified:
                return self._outcome(
                    context,
                    OutcomeStatus.SUBMIT_UNKNOWN,
                    OutcomePhase.VERIFY,
                    ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
                    "Submit was clicked, but explicit confirmation was not observed. Do not retry automatically.",
                    checkpoint=review.fingerprint,
                    details=details,
                )
            return self._outcome(
                context,
                OutcomeStatus.SUBMITTED_VERIFIED,
                OutcomePhase.COMPLETE,
                ReasonCode.SUBMISSION_CONFIRMED,
                "Application submission was explicitly confirmed.",
                checkpoint=review.fingerprint,
                evidence_refs=evidence.as_evidence_refs(),
                details=details,
            )
        except Exception as exc:
            return self._outcome(
                context,
                OutcomeStatus.FAILED_RETRYABLE,
                current_phase,
                ReasonCode.RETRYABLE_BROWSER_ERROR,
                f"Deterministic adapter failed: {type(exc).__name__}",
                retryable=True,
            )

    def _outcome(
        self,
        context: ApplicationContext,
        status: OutcomeStatus,
        phase: OutcomePhase,
        reason_code: ReasonCode,
        message: str,
        *,
        retryable: bool = False,
        checkpoint: str | None = None,
        evidence_refs: Sequence[EvidenceRef] = (),
        details: Mapping[str, Any] | None = None,
    ) -> ApplicationOutcome:
        return ApplicationOutcome(
            run_id=context.run_id,
            job_id=context.job_id,
            status=status,
            phase=phase,
            reason_code=reason_code,
            message=message,
            adapter=self.name,
            retryable=retryable,
            checkpoint=checkpoint,
            evidence_refs=tuple(evidence_refs),
            details={**dict(details or {}), "model_calls": 0},
        )


_REVIEW_STATE_SCRIPT = r"""async element => {
    const type = String(element.type || '').toLowerCase();
    if (String(element.getAttribute('role') || '').toLowerCase() === 'combobox') {
        const shell = element.closest('.select-shell')
            || element.closest('.field-wrapper');
        const selected = shell ? Array.from(shell.querySelectorAll(
            '.select__single-value, .select__multi-value__label, '
            + '[class*="single-value"], [class*="multi-value__label"]'
        )).map(item => String(item.textContent || '').replace(/\s+/g, ' ').trim())
            .filter(Boolean) : [];
        const announcements = shell ? Array.from(shell.querySelectorAll(
            '[aria-live], [role="status"]'
        )).map(item => String(item.textContent || '').replace(/\s+/g, ' ').trim())
            .filter(Boolean) : [];
        return {
            kind: 'combobox',
            value: selected.length ? selected[0] : '',
            selected,
            announcements,
            selectionDigest: element.getAttribute(
                'data-jobops-selected-option-digest'
            ) || ''
        };
    }
    if (type === 'file') {
        const files = [];
        for (const file of Array.from(element.files || [])) {
            const bytes = new Uint8Array(await file.arrayBuffer());
            let binary = '';
            for (let offset = 0; offset < bytes.length; offset += 0x8000) {
                binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
            }
            files.push({
                name: String(file.name || ''),
                size: Number(file.size || 0),
                type: String(file.type || ''),
                contentBase64: btoa(binary)
            });
        }
        return {kind: 'file', count: files.length, files};
    }
    if (type === 'radio') {
        const selected = element.name
            ? document.querySelector(`input[type="radio"][name="${CSS.escape(element.name)}"]:checked`)
            : (element.checked ? element : null);
        const label = selected && selected.labels && selected.labels.length
            ? selected.labels[0].innerText : selected?.getAttribute('aria-label');
        return {
            kind: 'radio',
            checked: Boolean(selected),
            value: String(selected?.value || ''),
            label: String(label || '').replace(/\s+/g, ' ').trim()
        };
    }
    if (type === 'checkbox') {
        const group = element.name
            ? Array.from(document.querySelectorAll(
                `input[type="checkbox"][name="${CSS.escape(element.name)}"]`
            ))
            : [element];
        const selected = group.filter(item => item.checked).map(item => {
            const label = item.labels && item.labels.length
                ? Array.from(item.labels)
                    .map(node => node.innerText || node.textContent || '')
                    .join(' ')
                : item.getAttribute('aria-label');
            return {
                value: String(item.value || ''),
                label: String(label || '').replace(/\s+/g, ' ').trim()
            };
        });
        return {kind: 'checkbox', checked: Boolean(element.checked), selected};
    }
    if (element.tagName === 'SELECT') {
        const selectedOptions = Array.from(element.selectedOptions || []).map(option => ({
            value: String(option.value || ''),
            label: String(option.textContent || '').replace(/\s+/g, ' ').trim()
        }));
        const selected = selectedOptions.length ? selectedOptions[0] : null;
        return {
            kind: 'select',
            value: String(selected?.value || ''),
            label: String(selected?.label || ''),
            selected: selectedOptions
        };
    }
    return {kind: 'value', value: String(element.value || '')};
}"""


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _review_field_identity(field: FieldIR) -> str:
    """Return an order-independent public identity for one ATS control.

    DOM position, generated IDs and selectors are presentation details and may
    change between otherwise identical Greenhouse renders.  Duplicate public
    identities remain safe because the digest input is a sorted multiset of
    identity/state pairs.
    """

    return _sha256_json(
        {
            "canonical_key": str(field.canonical_key),
            "kind": field.kind.value,
            "label": normalize_text(field.label),
            "options": sorted(
                normalize_text(label) for _, label in field.options
            ),
            "required": field.required,
            "sensitive": field.sensitive,
        }
    )


def _stable_review_state(
    state: Mapping[str, Any], field: FieldIR
) -> Mapping[str, Any]:
    """Remove render-only ATS state from an already verified read-back.

    React/Select controls may change live-region announcements and generated
    option values between otherwise identical renders.  Those values are useful
    while matching the browser state to the verified answer, but they are not
    part of the submitted application.  Gate B therefore binds the stable,
    user-visible selection after ``_readback_matches_verified_value`` has
    validated it.
    """

    if field.kind is FieldKind.COMBOBOX:
        return {
            "kind": "combobox",
            "selected": sorted(
                normalized
                for item in state.get("selected") or ()
                if (normalized := normalize_text(item))
            ),
            "selectionDigest": str(state.get("selectionDigest") or ""),
        }
    if field.kind is FieldKind.SELECT:
        return {
            "kind": "select",
            "selected": sorted(
                normalized
                for item in state.get("selected") or ()
                if isinstance(item, Mapping)
                and (
                    normalized := normalize_text(
                        item.get("label") or item.get("value")
                    )
                )
            ),
        }
    if field.kind is FieldKind.RADIO:
        return {
            "kind": "radio",
            "checked": bool(state.get("checked")),
            "selection": normalize_text(
                state.get("label") or state.get("value")
            ),
        }
    if field.kind is FieldKind.CHECKBOX:
        return {
            "kind": "checkbox",
            "checked": bool(state.get("checked")),
            "selected": sorted(
                normalized
                for item in state.get("selected") or ()
                if isinstance(item, Mapping)
                and (
                    normalized := normalize_text(
                        item.get("label") or item.get("value")
                    )
                )
            ),
        }
    return state


async def _review_binding_digests(
    page: Any,
    context: ApplicationContext,
    form: FormIR,
    fill: FillReport,
) -> tuple[str, str, tuple[str, ...]]:
    """Verify and hash exact read-back without retaining raw candidate values."""

    readbacks: list[tuple[str, str]] = []
    materials: list[tuple[str, str]] = []
    errors: list[str] = []
    uploaded_roles = set(fill.uploaded_files)
    filled_roles = set(fill.filled_fields)
    for field in form.fields:
        field_identity = _review_field_identity(field)
        locator, _ = await first_locator(page, field.selectors)
        if locator is None:
            if (
                field.kind is FieldKind.FILE
                and field.canonical_key in uploaded_roles
            ):
                expected = _expected_field_value(context, field)
                expected_path = Path(str(expected or "")).expanduser()
                try:
                    if (
                        not expected_path.is_file()
                        or not await _file_upload_is_recognized(
                            page, field, expected_path
                        )
                    ):
                        raise ValueError("uploaded file UI evidence is absent")
                    digest = hashlib.sha256(
                        expected_path.read_bytes()
                    ).hexdigest()
                    materials.append((str(field.canonical_key), digest))
                    readbacks.append(
                        (
                            field_identity,
                            _sha256_json(
                                {
                                    "contentSha256": digest,
                                    "kind": "uploaded-file-ui-confirmed",
                                }
                            ),
                        )
                    )
                except (OSError, ValueError):
                    errors.append(
                        f"Uploaded material read-back unavailable for {field.canonical_key}"
                    )
                continue
            if field.required or field.canonical_key in fill.filled_fields:
                errors.append(f"Review read-back unavailable for {field.canonical_key}")
            continue
        try:
            state = await locator.evaluate(_REVIEW_STATE_SCRIPT)
            if not isinstance(state, Mapping):
                raise TypeError("invalid browser read-back")
            safe_state = dict(state)
            should_match = (
                field.required
                or field.canonical_key in filled_roles
                or field.canonical_key in uploaded_roles
            )
            expected = _expected_field_value(context, field)
            if field.kind is FieldKind.FILE:
                safe_files: list[dict[str, Any]] = []
                content_hashes: list[str] = []
                for item in state.get("files") or ():
                    if not isinstance(item, Mapping):
                        continue
                    encoded = str(item.get("contentBase64") or "")
                    content = base64.b64decode(encoded, validate=True)
                    if int(item.get("size") or 0) != len(content):
                        raise ValueError("uploaded byte count changed during read-back")
                    digest = hashlib.sha256(content).hexdigest()
                    content_hashes.append(digest)
                    safe_files.append(
                        {
                            "size": len(content),
                            "type": str(item.get("type") or ""),
                            "contentSha256": digest,
                        }
                    )
                safe_state["files"] = safe_files
                if field.canonical_key in uploaded_roles and not content_hashes:
                    errors.append(
                        f"Uploaded material read-back unavailable for {field.canonical_key}"
                    )
                materials.extend(
                    (str(field.canonical_key), digest)
                    for digest in content_hashes
                )
                if should_match:
                    expected_path = Path(str(expected or "")).expanduser()
                    if not expected_path.is_file():
                        errors.append(
                            f"Verified material unavailable for {field.canonical_key}"
                        )
                    else:
                        expected_digest = hashlib.sha256(
                            expected_path.read_bytes()
                        ).hexdigest()
                        if expected_digest not in content_hashes:
                            errors.append(
                                f"Review read-back differs from verified material for {field.canonical_key}"
                            )
            elif should_match:
                if expected is None or expected == "":
                    errors.append(
                        f"Verified value unavailable for {field.canonical_key}"
                    )
                elif not _readback_matches_verified_value(state, field, expected):
                    errors.append(
                        f"Review read-back differs from verified value for {field.canonical_key}"
                    )
            readbacks.append(
                (
                    field_identity,
                    _sha256_json(_stable_review_state(safe_state, field)),
                )
            )
        except Exception:
            errors.append(f"Review read-back failed for {field.canonical_key}")
    return (
        _sha256_json(
            {"binding": REVIEW_BINDING_VERSION, "fields": sorted(readbacks)}
        ),
        _sha256_json(
            {
                "binding": REVIEW_BINDING_VERSION,
                "materials": sorted(materials),
            }
        ),
        unique(errors),
    )


def _expected_field_value(context: ApplicationContext, field: FieldIR) -> Any:
    if field.kind is FieldKind.FILE:
        if context.materials is not None:
            if field.canonical_key is CanonicalApplicationAnswerKey.RESUME:
                return context.materials.resume_path
            if (
                field.canonical_key
                is CanonicalApplicationAnswerKey.COVER_LETTER_FILE
                and context.materials.cover_letter_pdf is not None
                and context.private_home is not None
            ):
                try:
                    return context.private_home.contained_path(
                        context.materials.cover_letter_pdf.reference
                    )
                except Exception:
                    return None
            return None
        if field.canonical_key is CanonicalApplicationAnswerKey.RESUME:
            return context.resume_path
    return resolve_confirmed_value(
        field.canonical_key,
        field.label,
        profile=context.profile,
        answers=context.answers,
        cover_letter=context.cover_letter,
    )


def _fold_readback(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _is_decline_answer(value: Any) -> bool:
    normalized = normalize_text(value)
    return any(
        marker in normalized
        for marker in (
            "decline",
            "prefer not",
            "choose not",
            "do not wish",
            "don t wish",
            "not disclosed",
        )
    )


def _canonical_option_target(field: FieldIR, expected: Any) -> str:
    if type(expected) is bool:
        return "Yes" if expected else "No"
    normalized = normalize_text(expected)
    if field.canonical_key in {
        CanonicalApplicationAnswerKey.GENDER,
        CanonicalApplicationAnswerKey.RACE_ETHNICITY,
        CanonicalApplicationAnswerKey.VETERAN_STATUS,
        CanonicalApplicationAnswerKey.DISABILITY_STATUS,
    } and _is_decline_answer(expected):
        return "I don't wish to answer"
    if field.canonical_key is CanonicalApplicationAnswerKey.GENDER:
        aliases = {
            "male": "Man, male or masculine",
            "man": "Man, male or masculine",
            "man male or masculine": "Man, male or masculine",
            "female": "Woman, female or feminine",
            "woman": "Woman, female or feminine",
            "woman female or feminine": "Woman, female or feminine",
            "non binary": (
                "Non-binary/third gender (For example - bigender, gender "
                "non-conforming, androgynous)"
            ),
            "nonbinary": (
                "Non-binary/third gender (For example - bigender, gender "
                "non-conforming, androgynous)"
            ),
            "transgender man": "Transgender man, male, or masculine",
            "trans man": "Transgender man, male, or masculine",
            "transgender woman": "Transgender woman, female or feminine",
            "trans woman": "Transgender woman, female or feminine",
        }
        if normalized in aliases:
            return aliases[normalized]
    return str(expected).strip()


def _selection_attestation(field: FieldIR, expected: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "canonicalKey": field.canonical_key.value,
                "expected": normalize_text(
                    _canonical_option_target(field, expected)
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _option_matches_verified_value(
    field: FieldIR,
    expected: Any,
    option_value: Any,
    option_label: Any,
) -> bool:
    target = _canonical_option_target(field, expected)
    normalized_target = normalize_text(target)
    if any(
        normalize_text(candidate) == normalized_target
        for candidate in (option_value, option_label)
    ):
        return True
    label = str(option_label or "").strip()
    if field.canonical_key is CanonicalApplicationAnswerKey.COUNTRY:
        normalized_label = normalize_text(label)
        suffix = normalized_label.removeprefix(normalized_target).strip()
        return (
            normalized_label.startswith(normalized_target + " ")
            and bool(suffix)
            and suffix.split()[0].isdigit()
        )
    if field.canonical_key is CanonicalApplicationAnswerKey.CITY:
        province_aliases = {
            "ab": "alberta",
            "bc": "british columbia",
            "mb": "manitoba",
            "nb": "new brunswick",
            "nl": "newfoundland and labrador",
            "ns": "nova scotia",
            "nt": "northwest territories",
            "nu": "nunavut",
            "on": "ontario",
            "pe": "prince edward island",
            "qc": "quebec",
            "sk": "saskatchewan",
            "yt": "yukon",
        }

        def parts(raw: str) -> tuple[str, ...]:
            return tuple(
                province_aliases.get(item, item)
                for item in (
                    normalize_text(part)
                    for part in raw.split(",")
                )
                if item
            )

        expected_parts = parts(str(target))
        label_parts = parts(label)
        if (
            not expected_parts
            or not label_parts
            or expected_parts[0] != label_parts[0]
        ):
            return False
        cursor = 1
        for expected_part in expected_parts[1:]:
            try:
                cursor = label_parts.index(expected_part, cursor) + 1
            except ValueError:
                return False
        return True
    return False


def _readback_matches_verified_value(
    state: Mapping[str, Any], field: FieldIR, expected: Any
) -> bool:
    target = str(expected)
    if field.kind is FieldKind.CHECKBOX:
        if isinstance(expected, (list, tuple, set)):
            wanted = {normalize_text(item) for item in expected}
            selected = state.get("selected") or ()
            actual = {
                normalize_text(item.get("label") or item.get("value"))
                for item in selected
                if isinstance(item, Mapping)
            }
            return actual == wanted
        expected_checked = (
            expected
            if isinstance(expected, bool)
            else normalize_text(expected) in {"yes", "true", "1", "checked"}
        )
        return bool(state.get("checked")) is bool(expected_checked)
    if field.kind is FieldKind.RADIO:
        if not bool(state.get("checked")):
            return False
        return any(
            _option_matches_verified_value(
                field, expected, candidate, candidate
            )
            for candidate in (state.get("value"), state.get("label"))
        )
    if field.kind is FieldKind.SELECT:
        if isinstance(expected, (list, tuple, set)):
            wanted = {normalize_text(item) for item in expected}
            selected = state.get("selected") or ()
            actual = {
                normalize_text(item.get("label") or item.get("value"))
                for item in selected
                if isinstance(item, Mapping)
            }
            return actual == wanted
        return any(
            _option_matches_verified_value(
                field, expected, candidate, candidate
            )
            for candidate in (state.get("value"), state.get("label"))
        )
    if field.kind is FieldKind.COMBOBOX:
        if isinstance(expected, (list, tuple, set)):
            selected = tuple(state.get("selected") or ())
            return len(selected) == len(expected) and all(
                any(
                    _option_matches_verified_value(
                        field, item, label, label
                    )
                    for label in selected
                )
                for item in expected
            )
        selected = tuple(state.get("selected") or ())
        if selected and state.get("selectionDigest") == (
            _selection_attestation(field, expected)
        ):
            return True
        candidates = tuple(
            dict.fromkeys(
                (
                    state.get("value"),
                    *(state.get("selected") or ()),
                    *(state.get("announcements") or ()),
                )
            )
        )
        return any(
            _option_matches_verified_value(
                field, expected, candidate, candidate
            )
            for candidate in candidates
        )

    actual = str(state.get("value") or "")
    if field.canonical_key == "phone":
        return bool(re.sub(r"\D", "", target)) and re.sub(
            r"\D", "", actual
        ) == re.sub(r"\D", "", target)
    if field.canonical_key in {"linkedin", "github", "portfolio"}:
        return _fold_readback(actual).rstrip("/") == _fold_readback(target).rstrip(
            "/"
        )
    return _fold_readback(actual) == _fold_readback(target)


def _kind_from_attributes(attrs: Mapping[str, Any], fallback: str = "text") -> FieldKind:
    raw = str(attrs.get("type") or attrs.get("tag") or fallback).casefold()
    if str(attrs.get("role") or "").casefold() == "combobox":
        raw = "combobox"
    elif attrs.get("tag") == "textarea":
        raw = "textarea"
    elif attrs.get("tag") == "select":
        raw = "select"
    aliases = {"select-one": "select", "select-multiple": "select"}
    try:
        return FieldKind(aliases.get(raw, raw))
    except ValueError:
        try:
            return FieldKind(fallback)
        except ValueError:
            return FieldKind.TEXT


async def _fill_locator(
    page: Any,
    locator: Any,
    field: FieldIR,
    value: Any,
    *,
    location_constraints: Sequence[str] = (),
) -> bool:
    if field.kind is FieldKind.SELECT:
        return await select_exact_option(
            locator, _canonical_option_target(field, value)
        )
    if field.kind is FieldKind.COMBOBOX:
        if isinstance(value, (list, tuple, set)):
            return False
        target = _canonical_option_target(field, value)
        await locator.click()
        await locator.fill(target)
        await locator.press("ArrowDown")
        options = page.locator('[role="option"]')
        matches: list[tuple[Any, str]] = []
        for attempt in range(50):
            matches = []
            for index in range(await options.count()):
                option = options.nth(index)
                if not await option.is_visible():
                    continue
                label = (await option.inner_text()).strip()
                option_value = await option.get_attribute("data-value") or label
                if _option_matches_verified_value(
                    field, value, option_value, label
                ):
                    matches.append((option, label))
            if matches:
                break
            if attempt < 49:
                await page.wait_for_timeout(100)
        if (
            field.canonical_key is CanonicalApplicationAnswerKey.CITY
            and len(matches) > 1
            and location_constraints
        ):
            normalized_constraints = {
                normalize_text(item)
                for item in location_constraints
                if normalize_text(item)
            }
            constrained = [
                match
                for match in matches
                if normalized_constraints.issubset(
                    {
                        normalize_text(part)
                        for part in match[1].split(",")
                        if normalize_text(part)
                    }
                )
            ]
            matches = constrained
        if len(matches) != 1:
            await locator.press("Escape")
            return False
        await matches[0][0].click()
        await locator.evaluate(
            "(element, digest) => element.setAttribute("
            "'data-jobops-selected-option-digest', digest)",
            _selection_attestation(field, value),
        )
        return True
    if field.kind is FieldKind.CHECKBOX:
        if isinstance(value, (list, tuple, set)):
            if not field.name or not value:
                return False
            requested = {normalize_text(item) for item in value}
            if "" in requested:
                return False
            checkboxes = page.locator(
                f'input[type="checkbox"][name={css_string(field.name)}]'
            )
            matched: dict[str, Any] = {}
            all_boxes: list[Any] = []
            for index in range(await checkboxes.count()):
                checkbox = checkboxes.nth(index)
                all_boxes.append(checkbox)
                option_value = await checkbox.get_attribute("value") or ""
                option_label = await element_label(checkbox, option_value)
                for candidate in (option_value, option_label):
                    normalized = normalize_text(candidate)
                    if normalized in requested and normalized not in matched:
                        matched[normalized] = checkbox
            if set(matched) != requested:
                return False
            selected_ids = {id(box) for box in matched.values()}
            for checkbox in all_boxes:
                if id(checkbox) in selected_ids:
                    await checkbox.check()
                else:
                    await checkbox.uncheck()
            return True
        expected = value if isinstance(value, bool) else normalize_text(value) in {"yes", "true", "1", "checked"}
        if expected:
            await locator.check()
        else:
            await locator.uncheck()
        return True
    if field.kind is FieldKind.RADIO:
        if not field.name:
            return False
        radios = page.locator(f'input[type="radio"][name={css_string(field.name)}]')
        for index in range(await radios.count()):
            radio = radios.nth(index)
            option_value = await radio.get_attribute("value") or ""
            option_label = await element_label(radio, option_value)
            if _option_matches_verified_value(
                field, value, option_value, option_label
            ):
                await radio.check()
                return True
        return False
    if field.kind in {FieldKind.TEXT, FieldKind.TEXTAREA, FieldKind.EMAIL, FieldKind.TEL, FieldKind.URL}:
        await locator.fill(str(value))
        return True
    return False


async def _is_populated(page: Any, locator: Any, field: FieldIR) -> bool:
    if field.kind is FieldKind.FILE:
        return bool(await locator.evaluate("element => element.files && element.files.length"))
    if field.kind is FieldKind.CHECKBOX:
        if field.name:
            return bool(
                await page.locator(
                    f'input[type="checkbox"][name={css_string(field.name)}]:checked'
                ).count()
            )
        return bool(await locator.is_checked())
    if field.kind is FieldKind.RADIO:
        if not field.name:
            return bool(await locator.is_checked())
        return bool(await page.locator(f'input[type="radio"][name={css_string(field.name)}]:checked').count())
    if field.kind is FieldKind.COMBOBOX:
        return bool(
            await locator.evaluate(
                """element => {
                    const shell = element.closest('.select-shell')
                        || element.closest('.field-wrapper');
                    if (!shell) return false;
                    const selected = shell.querySelector(
                        '.select__single-value, .select__multi-value__label, '
                        + '[class*="single-value"], [class*="multi-value__label"]'
                    );
                    return Boolean(selected && String(
                        selected.textContent || ''
                    ).trim());
                }"""
            )
        )
    return bool((await locator.input_value()).strip())


async def _file_upload_is_recognized(
    page: Any, field: FieldIR, path: Path, *, timeout_ms: int = 10_000
) -> bool:
    """Verify either the retained file input or Greenhouse's upload receipt UI."""

    attempts = max(1, timeout_ms // 100)
    for attempt in range(attempts):
        locator, _ = await first_locator(page, field.selectors)
        if locator is not None:
            try:
                if await locator.evaluate(
                    "element => Boolean(element.files && element.files.length)"
                ):
                    return True
            except Exception:
                pass
        try:
            receipts = page.get_by_text(path.name, exact=True)
            for index in range(await receipts.count()):
                if await receipts.nth(index).is_visible():
                    return True
            if await page.locator("body").evaluate(
                "(body, name) => String(body.innerText || '').includes(name)",
                path.name,
            ):
                return True
        except Exception:
            return False
        if attempt + 1 < attempts:
            await page.wait_for_timeout(100)
    return False


def _unique_unresolved(items: Sequence[UnresolvedField]) -> list[UnresolvedField]:
    result: list[UnresolvedField] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.canonical_key, item.label)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
