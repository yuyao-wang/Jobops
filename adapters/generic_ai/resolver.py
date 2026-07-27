"""Resolve canonical field semantics locally and inject verified values only."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

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
    canonical_key: str
    value: str
    source: str
    sensitivity: Sensitivity


@dataclass(frozen=True)
class UnresolvedField:
    control: FormControl
    reason: str
    sensitivity: Sensitivity


_PATTERNS: tuple[tuple[str, Sensitivity, tuple[str, ...]], ...] = (
    ("preferred_name", Sensitivity.BASIC, (r"\bpreferred(?:\s+first)?\s*name\b", r"\bchosen\s*name\b")),
    ("first_name", Sensitivity.BASIC, (r"\bfirst\s*name\b", r"\bgiven\s*name\b")),
    ("last_name", Sensitivity.BASIC, (r"\blast\s*name\b", r"\bsurname\b", r"\bfamily\s*name\b")),
    ("full_name", Sensitivity.BASIC, (r"\bfull\s*name\b", r"\blegal\s*name\b")),
    ("email", Sensitivity.BASIC, (r"\be-?mail\b",)),
    ("phone", Sensitivity.BASIC, (r"\bphone\b", r"\bmobile\b")),
    ("location", Sensitivity.PERSONAL, (r"\bcurrent\s+location\b", r"\bhome\s+location\b", r"\bwhere\s+do\s+you\s+live\b")),
    ("linkedin", Sensitivity.BASIC, (r"linkedin",)),
    ("github", Sensitivity.BASIC, (r"github",)),
    ("portfolio", Sensitivity.BASIC, (r"portfolio", r"personal\s+website", r"website\s+url")),
    ("resume", Sensitivity.PERSONAL, (r"\bresume\b", r"\bcv\b")),
    ("cover_letter", Sensitivity.PERSONAL, (r"cover\s+letter",)),
    ("work_authorization", Sensitivity.LEGAL, (r"authori[sz]ed\s+to\s+work", r"legally\s+eligible")),
    ("sponsorship", Sensitivity.LEGAL, (r"sponsorship", r"visa\s+sponsor")),
    ("relocation", Sensitivity.PERSONAL, (r"relocat",)),
    ("salary", Sensitivity.COMPENSATION, (r"(?:salary|compensation|pay)\s+expect", r"(?:expected|desired)\s+(?:salary|compensation|pay)")),
    ("start_date", Sensitivity.PERSONAL, (r"start\s+date", r"available\s+to\s+start", r"notice\s+period")),
    ("gender", Sensitivity.VOLUNTARY_SELF_ID, (r"\bgender\b",)),
    ("race_ethnicity", Sensitivity.VOLUNTARY_SELF_ID, (r"race", r"ethnic")),
    ("veteran_status", Sensitivity.VOLUNTARY_SELF_ID, (r"veteran",)),
    ("disability_status", Sensitivity.VOLUNTARY_SELF_ID, (r"disab",)),
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


def classify_control(control: FormControl) -> tuple[str, Sensitivity] | None:
    text = control.semantic_text.casefold()
    if _looks_like_third_party_identity(text):
        return None
    autocomplete = control.autocomplete.replace("-", "_")
    autocomplete_map = {
        "given_name": "first_name",
        "family_name": "last_name",
        "name": "full_name",
        "email": "email",
        "tel": "phone",
        "address_level2": "location",
    }
    if autocomplete in autocomplete_map:
        key = autocomplete_map[autocomplete]
        for candidate, sensitivity, _ in _PATTERNS:
            if candidate == key:
                return candidate, sensitivity
    if control.input_type == "file" and re.search(r"resume|cv", text):
        return "resume", Sensitivity.PERSONAL
    for key, sensitivity, patterns in _PATTERNS:
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            return key, sensitivity
    return None


def semantic_mapping_is_compatible(control: FormControl, key: str) -> bool:
    """Require deterministic structure before accepting a model/cache mapping.

    A semantic model may propose a canonical key, but its proposal alone is not
    authority to place a candidate value into an ambiguous control.
    """

    if _looks_like_third_party_identity(control.semantic_text.casefold()):
        return False
    classified = classify_control(control)
    if classified is not None:
        return classified[0] == key

    input_type = control.input_type.casefold()
    autocomplete = control.autocomplete.replace("-", "_").casefold()
    has_candidate_self_semantics = any(
        re.search(rf"\b{re.escape(term)}\b", control.semantic_text, re.IGNORECASE)
        for term in _CANDIDATE_SELF_TERMS
    )
    if key == "email":
        return has_candidate_self_semantics and (
            input_type == "email" or autocomplete == "email"
        )
    if key == "phone":
        return has_candidate_self_semantics and (
            input_type == "tel" or autocomplete == "tel"
        )
    if key == "first_name":
        return autocomplete == "given_name"
    if key == "last_name":
        return autocomplete == "family_name"
    if key == "full_name":
        return autocomplete == "name"
    if key == "location":
        return autocomplete in {"address_level1", "address_level2", "country"}
    return False


def _legacy_value(profile: dict[str, Any], key: str, cover_letter: str) -> str:
    personal = profile.get("personal", {})
    common = profile.get("common_answers", {})
    values = {
        "first_name": personal.get("first_name", ""),
        "last_name": personal.get("last_name", ""),
        "preferred_name": personal.get("preferred_name", ""),
        "full_name": " ".join(filter(None, (personal.get("first_name"), personal.get("last_name")))),
        "email": personal.get("email", ""),
        "phone": personal.get("phone", ""),
        "location": personal.get("location", ""),
        "linkedin": personal.get("linkedin", ""),
        "github": personal.get("github", ""),
        "portfolio": personal.get("portfolio", ""),
        "resume": profile.get("resume_path", ""),
        "cover_letter": cover_letter,
        "work_authorization": common.get("authorized_to_work", ""),
        "sponsorship": common.get("require_sponsorship", ""),
        "relocation": common.get("willing_to_relocate", ""),
        "salary": common.get("salary_expectation", ""),
        "start_date": common.get("earliest_start_date", ""),
        "gender": common.get("gender", ""),
        "race_ethnicity": common.get("race_ethnicity", ""),
        "veteran_status": common.get("veteran_status", ""),
        "disability_status": common.get("disability_status", ""),
    }
    return str(values.get(key) or "").strip()


class AnswerResolver:
    """Map controls to canonical keys; values never enter semantic prompts."""

    def __init__(
        self,
        profile: dict[str, Any],
        *,
        cover_letter: str = "",
        resume_path: str = "",
    ):
        # Keep the artifact path only in this process-local projection.  It is
        # never included in the value-free FormIR, cache, prompt, or outcome.
        self.profile = dict(profile)
        if resume_path:
            self.profile["resume_path"] = resume_path
        self.cover_letter = cover_letter

    def prompt_redactions(self) -> tuple[str, ...]:
        """Return every locally injected value that a page must not echo."""

        values: set[str] = set()
        pending: list[Any] = [
            self.profile.get("personal", {}),
            self.profile.get("canonical_answers", {}),
            self.profile.get("common_answers", {}),
            self.profile.get("verified_question_answers", {}),
            self.profile.get("resume_path", ""),
            self.cover_letter,
        ]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.values())
            elif isinstance(value, (list, tuple, set)):
                pending.extend(value)
            elif isinstance(value, (str, int, float)):
                normalized = str(value).strip()
                if normalized:
                    values.add(normalized)
        return tuple(sorted(values, key=len, reverse=True))

    def value_for_key(self, key: str) -> str:
        canonical = self.profile.get("canonical_answers", {})
        if key in canonical:
            value = canonical[key]
            if isinstance(value, dict):
                value = value.get("value", "")
            return str(value or "").strip()
        return _legacy_value(self.profile, key, self.cover_letter)

    def exact_verified_answer(self, question: str) -> str:
        """Return only an explicitly verified answer for the exact prompt.

        Broad keyword matching is intentionally excluded here.  For example,
        "Company name" must never inherit the candidate's full name, and a
        technology-specific experience question must never inherit a generic
        years-of-experience answer.
        """

        normalized = " ".join(str(question or "").casefold().split())
        if not normalized:
            return ""
        answers = self.profile.get("verified_question_answers", {})
        if not isinstance(answers, dict):
            return ""
        for stored_question, stored in answers.items():
            if " ".join(str(stored_question).casefold().split()) != normalized:
                continue
            if isinstance(stored, dict):
                if stored.get("verified") is False:
                    return ""
                stored = stored.get("value", "")
            return str(stored or "").strip()
        return ""

    def resolve(self, control: FormControl, mapped_key: str = "") -> ResolvedField | UnresolvedField:
        if mapped_key and not semantic_mapping_is_compatible(control, mapped_key):
            mapped_sensitivity = next(
                (
                    sensitivity
                    for candidate, sensitivity, _patterns in _PATTERNS
                    if candidate == mapped_key
                ),
                Sensitivity.PERSONAL,
            )
            return UnresolvedField(
                control,
                "semantic mapping lacks deterministic structural confirmation",
                mapped_sensitivity,
            )
        classification = (mapped_key, Sensitivity.PERSONAL) if mapped_key else classify_control(control)
        if classification:
            key, sensitivity = classification
            for candidate, candidate_sensitivity, _ in _PATTERNS:
                if candidate == key:
                    sensitivity = candidate_sensitivity
                    break
            if mapped_key and sensitivity in {
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

        exact_answer = self.exact_verified_answer(
            control.label or control.aria_label or control.placeholder
        )
        if exact_answer:
            return ResolvedField(
                control,
                "verified_question_answer",
                exact_answer,
                "verified_answer_bank",
                Sensitivity.PERSONAL,
            )
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
    keys = [key for key, _, _ in _PATTERNS]
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
