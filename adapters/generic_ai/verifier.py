"""Read-back validation and strict review/submission evidence detection."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .models import FormIR
from .resolver import ResolvedField


CONFIRMATION_PHRASES = (
    "application submitted",
    "application has been submitted",
    "application received",
    "we received your application",
    "thank you for applying",
    "thanks for applying",
    "you have applied",
    "successfully submitted",
)


@dataclass(frozen=True)
class VerificationFailure:
    index: int
    label: str
    reason: str


@dataclass(frozen=True)
class VerificationReport:
    valid: bool
    failures: tuple[VerificationFailure, ...]
    errors: tuple[str, ...]
    # Only digests leave this module.  Candidate values and private artifact
    # paths exist transiently for comparison and are never ledgered/prompted.
    readback_hashes: tuple[tuple[int, str, str], ...] = ()
    material_content_hashes: tuple[tuple[int, str, str], ...] = ()


@dataclass(frozen=True)
class SubmissionEvidence:
    kind: str
    url: str
    text: str
    selector: str = "body"


def is_confirmation_text(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return any(phrase in normalized for phrase in CONFIRMATION_PHRASES)


async def detect_submission_evidence(page) -> SubmissionEvidence | None:
    try:
        body = await page.inner_text("body")
    except Exception:
        body = ""
    if is_confirmation_text(body):
        normalized = " ".join(body.split())
        lower = normalized.casefold()
        matching = next((phrase for phrase in CONFIRMATION_PHRASES if phrase in lower), "")
        start = max(0, lower.find(matching) - 80)
        return SubmissionEvidence(
            kind="confirmation_text",
            url=str(getattr(page, "url", "") or ""),
            text=normalized[start : start + 320],
        )
    url = str(getattr(page, "url", "") or "")
    if any(token in url.casefold() for token in ("/confirmation", "/submitted", "/thank-you", "/thanks")):
        return SubmissionEvidence(kind="confirmation_url", url=url, text="Explicit confirmation URL")
    return None


async def _control_value(page, selector: str, role: str) -> dict:
    return await page.eval_on_selector(
        selector,
        r"""async (el, role) => {
            const selected = el.tagName === 'SELECT' && el.selectedIndex >= 0
                ? el.options[el.selectedIndex] : null;
            const groupSelected = el.type === 'radio' && el.name
                ? document.querySelector(`input[type="radio"][name="${CSS.escape(el.name)}"]:checked`)
                : (el.type === 'radio' && el.checked ? el : null);
            const radioLabel = groupSelected
                ? (groupSelected.labels && groupSelected.labels.length
                    ? groupSelected.labels[0].innerText : groupSelected.getAttribute('aria-label'))
                : '';
            const fileContents = [];
            for (const file of Array.from(el.files || [])) {
                const bytes = new Uint8Array(await file.arrayBuffer());
                let binary = '';
                for (let offset = 0; offset < bytes.length; offset += 0x8000) {
                    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
                }
                fileContents.push({
                    size: Number(file.size || 0),
                    contentBase64: btoa(binary)
                });
            }
            return {
                exists: true,
                value: String(el.value || '').trim(),
                selectedText: String(selected ? selected.text : '').trim(),
                checked: Boolean(el.checked),
                groupChecked: Boolean(groupSelected),
                groupValue: String(groupSelected ? groupSelected.value : '').trim(),
                groupLabel: String(radioLabel || '').replace(/\s+/g, ' ').trim(),
                files: el.files ? el.files.length : 0,
                fileContents,
                invalid: el.getAttribute('aria-invalid') === 'true' || !el.checkValidity(),
                role
            };
        }""",
        role,
    )


def _normalized(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _expected_checked(value: str) -> bool:
    return _normalized(value) in {"yes", "true", "1", "checked", "agree", "i agree"}


def _text_matches(actual: str, expected: str, canonical_key: str) -> bool:
    actual_normalized = _normalized(actual)
    expected_normalized = _normalized(expected)
    if actual_normalized == expected_normalized:
        return True
    if canonical_key == "phone":
        return bool(expected_normalized) and re.sub(r"\D", "", actual) == re.sub(
            r"\D", "", expected
        )
    if canonical_key in {"linkedin", "github", "portfolio"}:
        return actual_normalized.rstrip("/") == expected_normalized.rstrip("/")
    return False


def _choice_matches(state: dict, expected: str, *, radio: bool = False) -> bool:
    candidates = (
        (state.get("groupValue"), state.get("groupLabel"))
        if radio
        else (state.get("value"), state.get("selectedText"))
    )
    normalized_expected = _normalized(expected)
    return bool(normalized_expected) and any(
        _normalized(candidate) == normalized_expected for candidate in candidates
    )


def _sha256_json(value: dict) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).expanduser().open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_readback_projection(state: dict, field: ResolvedField) -> dict:
    """Return the exact browser state that will be hashed immediately."""

    control = field.control
    if control.role == "file_upload" or control.input_type == "file":
        return {
            "kind": "file",
            "count": int(state.get("files") or 0),
            "content_sha256": sorted(str(item) for item in state.get("fileSha256") or ()),
        }
    if control.role == "radio":
        return {
            "kind": "radio",
            "checked": bool(state.get("groupChecked")),
            "value": str(state.get("groupValue") or ""),
            "label": str(state.get("groupLabel") or ""),
        }
    if control.role == "checkbox":
        return {"kind": "checkbox", "checked": bool(state.get("checked"))}
    if control.role in {"combobox", "listbox"} or control.tag == "select":
        return {
            "kind": "select",
            "value": str(state.get("value") or ""),
            "label": str(state.get("selectedText") or ""),
        }
    return {"kind": "value", "value": str(state.get("value") or "")}


async def verify_fields(page, form: FormIR, fields: list[ResolvedField]) -> VerificationReport:
    failures: list[VerificationFailure] = []
    readback_hashes: list[tuple[int, str, str]] = []
    material_hashes: list[tuple[int, str, str]] = []
    for field in fields:
        control = field.control
        if not control.selector:
            if control.required:
                failures.append(VerificationFailure(control.index, control.label, "missing stable selector"))
            continue
        try:
            state = await _control_value(page, control.selector, control.role)
            invalid = bool(state.get("invalid"))
            accepted = False
            if control.role == "file_upload" or control.input_type == "file":
                present = state.get("files", 0) > 0
                expected_digest = _file_sha256(field.value)
                observed_digests = [
                    str(item) for item in state.get("fileSha256") or ()
                ]
                for item in state.get("fileContents") or ():
                    if not isinstance(item, dict):
                        continue
                    content = base64.b64decode(
                        str(item.get("contentBase64") or ""), validate=True
                    )
                    if int(item.get("size") or 0) != len(content):
                        raise ValueError("uploaded byte count changed during read-back")
                    observed_digests.append(hashlib.sha256(content).hexdigest())
                state["fileSha256"] = observed_digests
                state.pop("fileContents", None)
                accepted = present and expected_digest in observed_digests
                if accepted:
                    material_hashes.append(
                        (control.index, field.canonical_key, expected_digest)
                    )
            elif control.role == "radio":
                present = bool(state.get("groupChecked"))
                accepted = present and _choice_matches(state, field.value, radio=True)
            elif control.role == "checkbox":
                expected_checked = _expected_checked(field.value)
                present = bool(state.get("checked")) or not expected_checked
                accepted = bool(state.get("checked")) is expected_checked
            elif control.role in {"combobox", "listbox"} or control.tag == "select":
                present = bool(state.get("value") or state.get("selectedText"))
                accepted = present and _choice_matches(state, field.value)
            else:
                present = bool(state.get("value"))
                accepted = present and _text_matches(
                    str(state.get("value") or ""), field.value, field.canonical_key
                )
            if not present:
                failures.append(
                    VerificationFailure(control.index, control.label, "resolved value is absent")
                )
            elif invalid:
                failures.append(
                    VerificationFailure(control.index, control.label, "browser rejected resolved value")
                )
            elif not accepted:
                failures.append(
                    VerificationFailure(control.index, control.label, "read-back differs from verified value")
                )
            else:
                readback_hashes.append(
                    (
                        control.index,
                        field.canonical_key,
                        _sha256_json(_safe_readback_projection(state, field)),
                    )
                )
        except Exception:
            failures.append(VerificationFailure(control.index, control.label, "read-back failed"))
    return VerificationReport(
        not failures and not form.errors,
        tuple(failures),
        form.errors,
        tuple(sorted(readback_hashes)),
        tuple(sorted(material_hashes)),
    )


def is_review_ready(form: FormIR, report: VerificationReport) -> bool:
    if not bool(form.submit_selector or form.submit_text) or not report.valid:
        return False

    # A careers landing page can expose an "Apply now" control while having
    # no application fields at all.  An empty verification report is therefore
    # not evidence that an application is ready to submit.  Permit an empty
    # report only on an explicitly observed Review stage; single-page forms
    # must have at least one locally verified field or uploaded material.
    return form.stage == "review" or bool(
        report.readback_hashes or report.material_content_hashes
    )
