"""Shared deterministic primitives for ATS adapters.

This module deliberately contains no model or agent integration.  ATS adapters
turn a page into a compact :class:`~adapters.protocol.FormIR`, resolve values
from already-confirmed local data, and execute Playwright operations directly.
Unknown required questions are returned to the orchestrator instead of being
guessed.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from core.application_execution_profile import (
    ApplicationExecutionIdentityFieldKey,
    ApplicationExecutionIdentityProfile,
)
from core.application_answer_taxonomy import (
    CanonicalApplicationAnswerKey,
    normalize_canonical_application_answer_key,
)

_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
_SENSITIVE_TERMS = (
    "authorized to work",
    "work authorization",
    "sponsor",
    "visa",
    "security clearance",
    "employment status",
    "currently employed",
    "full time experience",
    "in office",
    "graduation date",
    "date of graduation",
    "accommodation",
    "salary",
    "compensation",
    "gender",
    "race",
    "ethnicity",
    "categories describe you",
    "disability",
    "veteran",
    "criminal record",
    "background check",
)

_EXPLICIT_SENSITIVE_KEY_PATTERNS = (
    (
        CanonicalApplicationAnswerKey.EMPLOYMENT_STATUS,
        (
            re.compile(
                r"(?:(?:what is|select|please select)\s+)?"
                r"(?:your\s+)?(?:current\s+)?employment\s+status"
            ),
            re.compile(r"(?:are\s+you\s+)?currently\s+employed"),
        ),
    ),
    (
        CanonicalApplicationAnswerKey.GRADUATION_DATE,
        (
            re.compile(
                r"(?:(?:what is|enter|select|please enter|please select)\s+)?"
                r"(?:your\s+)?(?:actual\s+|expected\s+)?graduation\s+"
                r"(?:date|month|year)(?:\s+(?:mm|dd|yyyy|month|year))*"
            ),
            re.compile(
                r"(?:your\s+)?(?:actual\s+|expected\s+)?"
                r"date\s+of\s+graduation"
                r"(?:\s+(?:mm|dd|yyyy|month|year))*"
            ),
        ),
    ),
    (
        CanonicalApplicationAnswerKey.ACCOMMODATION,
        (
            re.compile(
                r"(?:(?:do|will)\s+you\s+)?(?:need|require)"
                r"(?:\s+(?:any|an?))?\s+"
                r"(?:(?:reasonable|workplace)\s+)?"
                r"accommodations?(?:\s+(?:to\s+(?:apply|interview|participate)"
                r"|(?:during|for)\s+(?:the\s+)?"
                r"(?:application|interview|hiring)\s+process))?"
            ),
            re.compile(
                r"(?:reasonable|workplace|interview|application\s+process|"
                r"hiring\s+process)\s+accommodations?"
            ),
        ),
    ),
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


def _looks_like_third_party_identity(text: str) -> bool:
    """Reject contact/name fields that refer to somebody other than the applicant."""

    return any(term in text for term in _THIRD_PARTY_IDENTITY_TERMS)


def normalize_text(value: Any) -> str:
    """Return a conservative key used for exact local answer matching."""

    return _NON_WORD_RE.sub(" ", str(value or "").casefold()).strip()


def _explicit_sensitive_key_for(
    label: str, name: str = ""
) -> CanonicalApplicationAnswerKey | None:
    """Map only self-evident employment, education, or health labels."""

    descriptors = tuple(
        dict.fromkeys(
            normalized
            for normalized in (normalize_text(label), normalize_text(name))
            if normalized
        )
    )
    for key, patterns in _EXPLICIT_SENSITIVE_KEY_PATTERNS:
        if any(
            pattern.fullmatch(descriptor)
            for descriptor in descriptors
            for pattern in patterns
        ):
            return key
    return None


def is_sensitive_question(label: str, name: str = "") -> bool:
    """Classify questions that must never be inferred or fuzzily matched."""

    text = normalize_text(" ".join((label, name)))
    return any(term in text for term in _SENSITIVE_TERMS)


def css_string(value: str) -> str:
    """Quote a value for a CSS attribute selector."""

    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def nested_value(mapping: Mapping[str, Any], *paths: str) -> Any:
    """Return the first non-empty value from dotted paths."""

    for path in paths:
        current: Any = mapping
        for part in path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                current = None
                break
            current = current[part]
        if current is not None and current != "":
            return current
    return None


def resolve_confirmed_value(
    canonical_key: CanonicalApplicationAnswerKey | str,
    label: str,
    *,
    profile: ApplicationExecutionIdentityProfile,
    answers: Mapping[str, Any],
    cover_letter: str = "",
) -> Any:
    """Resolve a field without inference.

    ``answers`` is treated as an already-confirmed answer projection supplied by
    the private profile layer.  Matching is intentionally exact after light
    punctuation/whitespace normalization; fuzzy semantic guessing belongs in a
    separate opt-in resolver and must never happen in the normal adapter path.
    """

    key = normalize_canonical_application_answer_key(
        canonical_key,
        allow_legacy_alias=True,
        allow_custom_unknown=True,
    )
    if key is CanonicalApplicationAnswerKey.COVER_LETTER:
        return cover_letter or None
    if key is CanonicalApplicationAnswerKey.FULL_NAME:
        return profile.full_name
    try:
        identity_key = ApplicationExecutionIdentityFieldKey(key.value)
    except ValueError:
        identity_key = None
    if identity_key is not None:
        value = profile.value_for(identity_key)
        if value is not None:
            return value

    candidates = (key.value, label, normalize_text(label))
    normalized_answers = {normalize_text(key): value for key, value in answers.items()}
    for candidate in candidates:
        if candidate in answers and answers[candidate] not in (None, ""):
            return answers[candidate]
        normalized = normalize_text(candidate)
        if normalized in normalized_answers and normalized_answers[normalized] not in (None, ""):
            return normalized_answers[normalized]
    return None


def _legacy_canonical_key_for(
    label: str, name: str = "", input_type: str = ""
) -> str:
    """Map common ATS field semantics to a local canonical key."""

    text = normalize_text(" ".join((label, name)))
    compact_name = normalize_text(name).replace(" ", "")
    if _looks_like_third_party_identity(text) and any(
        term in text
        for term in (
            "name",
            "email",
            "e mail",
            "phone",
            "telephone",
            "mobile",
            "address",
            "location",
        )
    ):
        return f"custom:{normalize_text(label or name) or 'unnamed'}"
    explicit_sensitive_key = _explicit_sensitive_key_for(label, name)
    if explicit_sensitive_key is not None:
        return explicit_sensitive_key.value
    if (
        "best describes your work authorization" in text
        or "which of the following best describes" in text
        and "work authorization" in text
    ):
        return "work_authorization_detail"
    if (
        "full time experience" in text
        and ("how many years" in text or "excluding internships" in text)
    ):
        return "full_time_experience"
    if (
        "in office" in text
        and (
            "commit" in text
            or "days per week" in text
            or "hybrid schedule" in text
        )
    ):
        return "office_attendance"
    if "how familiar" in text and (
        "company" in text or "job posting" in text
    ):
        return "company_familiarity"
    if "how did you hear about" in text:
        return "job_discovery_source"
    if "which categories describe you" in text and "select all" in text:
        return "race_ethnicity"
    # Consequential fields may map only to an explicit canonical answer key.
    # Everything else stays a custom question and therefore hands off.
    if is_sensitive_question(label, name):
        if "authorized to work" in text or "work authorization" in text:
            return "work_authorization"
        if "sponsor" in text:
            return "sponsorship"
        if any(
            phrase in text
            for phrase in (
                "salary expectation",
                "salary expectations",
                "expected salary",
                "desired salary",
                "compensation expectation",
                "compensation expectations",
                "expected compensation",
                "desired compensation",
                "pay expectation",
            )
        ):
            return "salary"
        if "gender" in text or text in {"sex", "legal sex"}:
            return "gender"
        if "race" in text or "ethnicity" in text:
            return "race_ethnicity"
        if "veteran" in text:
            return "veteran_status"
        if "disability" in text:
            return "disability_status"
        return f"custom:{normalize_text(label or name) or 'unnamed'}"
    if input_type == "file":
        if "cover" in text:
            return "cover_letter_file"
        if "resume" in text or text in {"cv", "curriculum vitae"}:
            return "resume"
        return f"custom:{normalize_text(label or name) or 'unnamed-file'}"
    if "resume" in text or text in {"cv", "curriculum vitae"}:
        return "resume"
    if "preferred name" in text or "preferred first name" in text:
        return "preferred_name"
    if "first name" in text or compact_name in {"firstname", "first_name"}:
        return "first_name"
    if "last name" in text or "surname" in text or compact_name in {"lastname", "last_name"}:
        return "last_name"
    if text in {"name", "full name", "legal name"} or compact_name in {"fullname", "name"}:
        return "full_name"
    if "email" in text:
        return "email"
    if "phone" in text or "telephone" in text or "mobile" in text:
        return "phone"
    if "linkedin" in text:
        return "linkedin"
    if "github" in text:
        return "github"
    if "portfolio" in text or "personal website" in text or text == "website":
        return "portfolio"
    if "cover letter" in text or "additional information" in text:
        return "cover_letter"
    if "current company" in text or "current employer" in text:
        return "current_company"
    if "postal" in text or "zip code" in text:
        return "postal_code"
    if text == "city" or text.endswith(" city"):
        return "city"
    if text == "state" or "province" in text:
        return "state"
    if "country" in text:
        return "country"
    if "address" in text:
        return "address"
    if text in {"location", "current location", "home location"}:
        return "location"
    return f"custom:{normalize_text(label or name) or 'unnamed'}"


def canonical_key_for(
    label: str, name: str = "", input_type: str = ""
) -> CanonicalApplicationAnswerKey:
    """Classify through the legacy rules, then enter the shared taxonomy."""

    return normalize_canonical_application_answer_key(
        _legacy_canonical_key_for(label, name, input_type),
        allow_legacy_alias=True,
        allow_custom_unknown=True,
    )


@dataclass(frozen=True)
class FieldSpec:
    canonical_key: CanonicalApplicationAnswerKey
    selectors: tuple[str, ...]
    kind: str = "text"
    label: str = ""

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


async def first_locator(page: Any, selectors: Sequence[str]) -> tuple[Any | None, str | None]:
    """Find the first attached element for a deterministic selector list."""

    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count():
                return locator, selector
        except Exception:
            continue
    return None, None


async def element_label(locator: Any, fallback: str = "") -> str:
    """Read an associated label without serializing the page."""

    try:
        label = await locator.evaluate(
            """element => {
                const own = element.getAttribute('aria-label') ||
                    element.getAttribute('data-label');
                if (own) return own.trim();
                if (element.labels && element.labels.length) {
                    return Array.from(element.labels)
                        .map(label => label.innerText || label.textContent || '')
                        .join(' ').trim();
                }
                const container = element.closest(
                    '.field, .application-question, .ashby-application-form-field, '
                    + '.jv-form-field, [data-field], fieldset'
                );
                const label = container && container.querySelector('label, legend');
                return label ? (label.innerText || label.textContent || '').trim() : '';
            }"""
        )
        return _SPACE_RE.sub(" ", label or "").strip() or fallback
    except Exception:
        return fallback


async def select_exact_option(locator: Any, value: Any) -> bool:
    """Select an option by exact value or exact normalized label."""

    multiple = isinstance(value, (list, tuple, set))
    targets = list(value) if multiple else [value]
    if not targets or any(not isinstance(item, str) or not item.strip() for item in targets):
        return False
    options = await locator.locator("option").evaluate_all(
        "options => options.map(option => ({value: option.value, label: option.textContent || ''}))"
    )
    matched: list[str] = []
    for target_value in targets:
        target = str(target_value)
        normalized = normalize_text(target)
        match = next(
            (
                option["value"]
                for option in options
                if option["value"] == target
                or normalize_text(option["label"]) == normalized
            ),
            None,
        )
        if match is None:
            return False
        if match not in matched:
            matched.append(match)
    await locator.select_option(value=matched if multiple else matched[0])
    return True


async def maybe_await(value: Any) -> Any:
    """Await a callback result when necessary."""

    if inspect.isawaitable(value):
        return await value
    return value


async def invoke_gate_b_validator(
    validator: Callable[..., bool | Awaitable[bool]] | None,
    permit: Any,
    *,
    job_id: str,
    run_id: str,
    review_fingerprint: str,
) -> bool:
    """Atomically validate and consume an opaque Gate B permit.

    The callback is part of the trusted core boundary and must perform the
    one-time ledger consumption before returning ``True``.  Adapters never
    inspect or accept unsigned permit data directly.
    """

    if validator is None or permit is None:
        return False
    try:
        result = validator(
            permit,
            job_id=job_id,
            run_id=run_id,
            review_fingerprint=review_fingerprint,
        )
    except Exception as exc:
        # Signature, expiry, binding, prerequisite, and one-time-consumption
        # failures are ordinary approval denials, not browser failures. Avoid a
        # module-level dependency so the adapter protocol remains reusable.
        if exc.__class__.__module__ == "core.permits" and exc.__class__.__name__.startswith("Permit"):
            return False
        raise
    return bool(await maybe_await(result))


def basename_only(path: str | Path | None) -> str | None:
    """Return only a filename so review metadata never leaks private paths."""

    return Path(path).name if path else None


def unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))
