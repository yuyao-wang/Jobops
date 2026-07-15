"""Stable form fingerprints that ignore candidate values and dynamic attributes."""

from __future__ import annotations

import hashlib
import json
import re

from .models import FormControl, FormIR


_SPACE_RE = re.compile(r"\s+")
_DYNAMIC_ID_RE = re.compile(r"(?:^|[-_])(?:\d{5,}|[0-9a-f]{8,})(?:$|[-_])", re.IGNORECASE)


def normalize_text(value: str) -> str:
    return _SPACE_RE.sub(" ", value.strip().casefold())


def stable_element_id(value: str) -> str:
    value = normalize_text(value)
    return "" if _DYNAMIC_ID_RE.search(value) else value


def control_signature(control: FormControl) -> dict:
    """Return a value-free, ordering-stable control signature."""
    return {
        "role": control.role,
        "tag": control.tag,
        "type": control.input_type,
        "id": stable_element_id(control.element_id),
        "name": normalize_text(control.name),
        "label": normalize_text(control.label),
        "aria": normalize_text(control.aria_label),
        "placeholder": normalize_text(control.placeholder),
        "autocomplete": normalize_text(control.autocomplete),
        "required": control.required,
        "options": sorted(normalize_text(option.label) for option in control.options),
    }


def fingerprint_form(form: FormIR, adapter_major: int = 1) -> str:
    payload = {
        "adapter_major": adapter_major,
        "platform": normalize_text(form.platform),
        "tenant": normalize_text(form.tenant),
        "stage": normalize_text(form.stage),
        "path": normalize_text(form.url_path),
        "controls": [control_signature(control) for control in form.controls],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def fingerprint_review(form: FormIR, verification, adapter_major: int = 1) -> str:
    """Bind Gate B to structure plus freshly hashed browser read-back.

    ``fingerprint_form`` remains value-free because it is the public recipe
    cache key.  This separate fingerprint is private run state: it includes
    only SHA-256 digests of values and uploaded bytes, never the values, paths,
    or filenames themselves.
    """

    payload = {
        "adapter_major": adapter_major,
        "form_fingerprint": fingerprint_form(form, adapter_major),
        "readback_hashes": [list(item) for item in verification.readback_hashes],
        "material_content_hashes": [
            list(item) for item in verification.material_content_hashes
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
