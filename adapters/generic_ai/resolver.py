"""Resolve canonical field semantics locally and inject verified values only."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from core.application_answer_taxonomy import (
    CanonicalApplicationAnswerKey,
    normalize_canonical_application_answer_key,
)
from core.application_execution_profile import (
    ApplicationExecutionIdentityProfile,
)
from adapters.shared import resolve_confirmed_value
from .models import FormControl, FormIR


class Sensitivity(StrEnum):
    BASIC = "basic"
    PERSONAL = "personal"
    LEGAL = "legal"
    COMPENSATION = "compensation"
    VOLUNTARY_SELF_ID = "voluntary_self_id"


@dataclass(frozen=True)
class ResolvedField:
    control: FormControl
    canonical_key: CanonicalApplicationAnswerKey
    value: str
    source: str
    sensitivity: Sensitivity

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
class UnresolvedField:
    control: FormControl
    reason: str
    sensitivity: Sensitivity


_PATTERNS: tuple[
    tuple[CanonicalApplicationAnswerKey, Sensitivity, tuple[str, ...]], ...
] = (
    (CanonicalApplicationAnswerKey.PREFERRED_NAME, Sensitivity.BASIC, (r"\bpreferred(?:\s+first)?\s*name\b", r"\bchosen\s*name\b")),
    (CanonicalApplicationAnswerKey.FIRST_NAME, Sensitivity.BASIC, (r"\bfirst\s*name\b", r"\bgiven\s*name\b")),
    (CanonicalApplicationAnswerKey.LAST_NAME, Sensitivity.BASIC, (r"\blast\s*name\b", r"\bsurname\b", r"\bfamily\s*name\b")),
    (CanonicalApplicationAnswerKey.FULL_NAME, Sensitivity.BASIC, (r"\bfull\s*name\b", r"\blegal\s*name\b")),
    (CanonicalApplicationAnswerKey.EMAIL, Sensitivity.BASIC, (r"\be-?mail\b",)),
    (CanonicalApplicationAnswerKey.PHONE, Sensitivity.BASIC, (r"\bphone\b", r"\bmobile\b")),
    (CanonicalApplicationAnswerKey.LOCATION, Sensitivity.PERSONAL, (r"\bcurrent\s+location\b", r"\bhome\s+location\b", r"\bwhere\s+do\s+you\s+live\b")),
    (CanonicalApplicationAnswerKey.LINKEDIN, Sensitivity.BASIC, (r"linkedin",)),
    (CanonicalApplicationAnswerKey.GITHUB, Sensitivity.BASIC, (r"github",)),
    (CanonicalApplicationAnswerKey.PORTFOLIO, Sensitivity.BASIC, (r"portfolio", r"personal\s+website", r"website\s+url")),
    (CanonicalApplicationAnswerKey.RESUME, Sensitivity.PERSONAL, (r"\bresume\b", r"\bcv\b")),
    (CanonicalApplicationAnswerKey.COVER_LETTER, Sensitivity.PERSONAL, (r"cover\s+letter",)),
    (CanonicalApplicationAnswerKey.WORK_AUTHORIZATION, Sensitivity.LEGAL, (r"authori[sz]ed\s+to\s+work", r"legally\s+eligible")),
    (CanonicalApplicationAnswerKey.SPONSORSHIP, Sensitivity.LEGAL, (r"sponsorship", r"visa\s+sponsor")),
    (CanonicalApplicationAnswerKey.RELOCATION, Sensitivity.PERSONAL, (r"relocat",)),
    (CanonicalApplicationAnswerKey.SALARY, Sensitivity.COMPENSATION, (r"(?:salary|compensation|pay)\s+expect", r"(?:expected|desired)\s+(?:salary|compensation|pay)")),
    (CanonicalApplicationAnswerKey.START_DATE, Sensitivity.PERSONAL, (r"start\s+date", r"available\s+to\s+start", r"notice\s+period")),
    (CanonicalApplicationAnswerKey.GENDER, Sensitivity.VOLUNTARY_SELF_ID, (r"\bgender\b",)),
    (CanonicalApplicationAnswerKey.RACE_ETHNICITY, Sensitivity.VOLUNTARY_SELF_ID, (r"race", r"ethnic")),
    (CanonicalApplicationAnswerKey.VETERAN_STATUS, Sensitivity.VOLUNTARY_SELF_ID, (r"veteran",)),
    (CanonicalApplicationAnswerKey.DISABILITY_STATUS, Sensitivity.VOLUNTARY_SELF_ID, (r"disab",)),
)

_THIRD_PARTY_IDENTITY_TERMS = (
    "reference",
    "referee",
    "hiring manager",
    "manager",
    "supervisor",
    "recruiter",
    "emergency contact",
    "contact person",
    "school",
    "university",
    "institution",
    "former employer",
    "company contact",
)

_CANDIDATE_SELF_TERMS = (
    "your",
    "candidate",
    "applicant",
    "personal",
)


def _looks_like_third_party_identity(text: str) -> bool:
    return any(term in text for term in _THIRD_PARTY_IDENTITY_TERMS) and any(
        term in text
        for term in (
            "name",
            "email",
            "e-mail",
            "phone",
            "mobile",
            "address",
            "location",
        )
    )


def classify_control(
    control: FormControl,
) -> tuple[CanonicalApplicationAnswerKey, Sensitivity] | None:
    text = control.semantic_text.casefold()
    if _looks_like_third_party_identity(text):
        return None
    autocomplete = control.autocomplete.replace("-", "_")
    autocomplete_map = {
        "given_name": CanonicalApplicationAnswerKey.FIRST_NAME,
        "family_name": CanonicalApplicationAnswerKey.LAST_NAME,
        "name": CanonicalApplicationAnswerKey.FULL_NAME,
        "email": CanonicalApplicationAnswerKey.EMAIL,
        "tel": CanonicalApplicationAnswerKey.PHONE,
        "address_level2": CanonicalApplicationAnswerKey.LOCATION,
    }
    if autocomplete in autocomplete_map:
        key = autocomplete_map[autocomplete]
        for candidate, sensitivity, _ in _PATTERNS:
            if candidate == key:
                return candidate, sensitivity
    if control.input_type == "file" and re.search(r"resume|cv", text):
        return CanonicalApplicationAnswerKey.RESUME, Sensitivity.PERSONAL
    for key, sensitivity, patterns in _PATTERNS:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return key, sensitivity
    return None


def semantic_mapping_is_compatible(
    control: FormControl,
    key: CanonicalApplicationAnswerKey | str,
) -> bool:
    """Require deterministic structure before accepting a model/cache mapping.

    A semantic model may propose a canonical key, but its proposal alone is not
    authority to place a candidate value into an ambiguous control.
    """

    normalized_key = normalize_canonical_application_answer_key(
        key, allow_legacy_alias=True
    )
    if _looks_like_third_party_identity(control.semantic_text.casefold()):
        return False
    classified = classify_control(control)
    if classified is not None:
        return classified[0] is normalized_key

    input_type = control.input_type.casefold()
    autocomplete = control.autocomplete.replace("-", "_").casefold()
    has_candidate_self_semantics = any(
        re.search(rf"\b{re.escape(term)}\b", control.semantic_text, re.IGNORECASE)
        for term in _CANDIDATE_SELF_TERMS
    )
    if normalized_key is CanonicalApplicationAnswerKey.EMAIL:
        return has_candidate_self_semantics and (
            input_type == "email" or autocomplete == "email"
        )
    if normalized_key is CanonicalApplicationAnswerKey.PHONE:
        return has_candidate_self_semantics and (
            input_type == "tel" or autocomplete == "tel"
        )
    if normalized_key is CanonicalApplicationAnswerKey.FIRST_NAME:
        return autocomplete == "given_name"
    if normalized_key is CanonicalApplicationAnswerKey.LAST_NAME:
        return autocomplete == "family_name"
    if normalized_key is CanonicalApplicationAnswerKey.FULL_NAME:
        return autocomplete == "name"
    if normalized_key is CanonicalApplicationAnswerKey.LOCATION:
        return autocomplete in {"address_level1", "address_level2", "country"}
    return False


class AnswerResolver:
    """Map controls to canonical keys; values never enter semantic prompts."""

    def __init__(
        self,
        profile: ApplicationExecutionIdentityProfile | Mapping[str, Any],
        *,
        answers: Mapping[str, Any] | None = None,
        cover_letter: str = "",
        resume_path: str = "",
    ):
        self.identity_profile = (
            profile
            if isinstance(profile, ApplicationExecutionIdentityProfile)
            else ApplicationExecutionIdentityProfile.from_application_bundle_profile(
                profile
            )
        )
        self.answers = dict(answers or {})
        self.cover_letter = cover_letter
        self.resume_path = str(resume_path or "")

    def prompt_redactions(self) -> tuple[str, ...]:
        """Return every locally injected value that a page must not echo."""

        values: set[str] = set()
        pending: list[Any] = [
            self.identity_profile.redaction_values(),
            self.answers,
            self.resume_path,
            self.cover_letter,
        ]
        while pending:
            value = pending.pop()
            if isinstance(value, Mapping):
                pending.extend(value.values())
            elif isinstance(value, (list, tuple, set)):
                pending.extend(value)
            elif isinstance(value, (str, int, float)):
                normalized = str(value).strip()
                if normalized:
                    values.add(normalized)
        return tuple(sorted(values, key=len, reverse=True))

    def value_for_key(
        self, key: CanonicalApplicationAnswerKey | str
    ) -> str:
        normalized_key = normalize_canonical_application_answer_key(
            key, allow_legacy_alias=True
        )
        if normalized_key is CanonicalApplicationAnswerKey.RESUME:
            return self.resume_path
        value = resolve_confirmed_value(
            normalized_key,
            normalized_key.value,
            profile=self.identity_profile,
            answers=self.answers,
            cover_letter=self.cover_letter,
        )
        return "" if value is None else str(value).strip()

    def resolve(
        self,
        control: FormControl,
        mapped_key: CanonicalApplicationAnswerKey | str = "",
    ) -> ResolvedField | UnresolvedField:
        normalized_mapped_key = (
            normalize_canonical_application_answer_key(
                mapped_key, allow_legacy_alias=True
            )
            if mapped_key
            else None
        )
        if normalized_mapped_key and not semantic_mapping_is_compatible(
            control, normalized_mapped_key
        ):
            mapped_sensitivity = next(
                (
                    sensitivity
                    for candidate, sensitivity, _patterns in _PATTERNS
                    if candidate is normalized_mapped_key
                ),
                Sensitivity.PERSONAL,
            )
            return UnresolvedField(
                control,
                "semantic mapping lacks deterministic structural confirmation",
                mapped_sensitivity,
            )
        classification = (
            (normalized_mapped_key, Sensitivity.PERSONAL)
            if normalized_mapped_key
            else classify_control(control)
        )
        if classification:
            key, sensitivity = classification
            for candidate, candidate_sensitivity, _ in _PATTERNS:
                if candidate == key:
                    sensitivity = candidate_sensitivity
                    break
            if normalized_mapped_key and sensitivity in {
                Sensitivity.LEGAL,
                Sensitivity.COMPENSATION,
                Sensitivity.VOLUNTARY_SELF_ID,
            }:
                return UnresolvedField(
                    control,
                    f"new sensitive mapping for {key} requires confirmation",
                    sensitivity,
                )
            value = self.value_for_key(key)
            if value:
                return ResolvedField(control, key, value, "verified_profile", sensitivity)
            return UnresolvedField(control, f"verified value missing for {key}", sensitivity)

        return UnresolvedField(control, "no unambiguous canonical mapping", Sensitivity.PERSONAL)

    def resolve_form(
        self, form: FormIR, semantic_mappings: dict[int, str] | None = None
    ) -> tuple[list[ResolvedField], list[UnresolvedField]]:
        mapped = semantic_mappings or {}
        resolved: list[ResolvedField] = []
        unresolved: list[UnresolvedField] = []
        for control in form.controls:
            if control.disabled or control.role == "button":
                continue
            result = self.resolve(control, mapped.get(control.index, ""))
            if isinstance(result, ResolvedField):
                resolved.append(result)
            elif control.required:
                unresolved.append(result)
        return resolved, unresolved


_SEMANTIC_MAPPING_PROMPT = """Map each unresolved job-application control to one existing canonical key.
Do not answer any question and do not create candidate facts. Return JSON only:
{{"mappings":[{{"index":0,"canonical_key":"email","confidence":0.99}}]}}

Allowed canonical keys:
{keys}

Unresolved controls (structure only; no candidate values):
{controls}
"""


def _redact_private_strings(value: Any, private_values: Iterable[str]) -> Any:
    if isinstance(value, str):
        redacted = value
        for private_value in private_values:
            if not private_value:
                continue
            escaped = re.escape(private_value)
            if private_value.isalnum() and len(private_value) <= 3:
                escaped = rf"(?<!\w){escaped}(?!\w)"
            redacted = re.sub(
                escaped,
                "[PRIVATE]",
                redacted,
                flags=re.IGNORECASE,
            )
        return redacted
    if isinstance(value, list):
        return [_redact_private_strings(item, private_values) for item in value]
    if isinstance(value, dict):
        return {
            key: _redact_private_strings(item, private_values)
            for key, item in value.items()
        }
    return value


def map_unknown_controls(
    brain,
    controls: list[FormControl],
    *,
    private_values: Iterable[str] = (),
) -> dict[int, str]:
    """Use one bounded model call to classify unknown controls, never answer them."""
    if not controls or brain is None:
        return {}
    keys = [key.value for key, _, _ in _PATTERNS]
    payload = _redact_private_strings(
        [control.compact_dict() for control in controls[:40]],
        tuple(private_values),
    )
    result = brain.ask_json(
        _SEMANTIC_MAPPING_PROMPT.format(keys=", ".join(keys), controls=json.dumps(payload, ensure_ascii=True)),
        timeout=60,
        component="form_analysis",
    )
    mappings: dict[int, str] = {}
    raw_mappings = result.get("mappings", []) if isinstance(result, dict) else []
    allowed_indices = {control.index for control in controls[:40]}
    for item in raw_mappings if isinstance(raw_mappings, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item["index"])
            key = str(item.get("canonical_key") or "")
            confidence = float(item.get("confidence") or 0)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if index in allowed_indices and key in keys and confidence >= 0.90:
            mappings[index] = key
    return mappings
