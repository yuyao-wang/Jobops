"""Shared provider-neutral taxonomy for application-answer field semantics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION = (
    "canonical-application-answer-taxonomy-v1"
)


class CanonicalApplicationAnswerKey(StrEnum):
    PREFERRED_NAME = "preferred_name"
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    FULL_NAME = "full_name"
    EMAIL = "email"
    PHONE = "phone"
    LOCATION = "location"
    ADDRESS = "address"
    CITY = "city"
    STATE = "state"
    POSTAL_CODE = "postal_code"
    COUNTRY = "country"
    LINKEDIN = "linkedin"
    GITHUB = "github"
    PORTFOLIO = "portfolio"
    CURRENT_COMPANY = "current_company"
    RESUME = "resume"
    COVER_LETTER = "cover_letter"
    COVER_LETTER_FILE = "cover_letter_file"
    WORK_AUTHORIZATION = "work_authorization"
    SPONSORSHIP = "sponsorship"
    RELOCATION = "relocation"
    SALARY = "salary"
    START_DATE = "start_date"
    GENDER = "gender"
    RACE_ETHNICITY = "race_ethnicity"
    VETERAN_STATUS = "veteran_status"
    DISABILITY_STATUS = "disability_status"
    ATTESTATION = "attestation"
    CONSENT = "consent"
    SIGNATURE = "signature"
    UNKNOWN = "unknown"


class CanonicalAnswerValueType(StrEnum):
    TEXT = "TEXT"
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"
    MULTI_SELECT = "MULTI_SELECT"
    FILE_REFERENCE = "FILE_REFERENCE"
    ATTESTATION = "ATTESTATION"
    UNKNOWN = "UNKNOWN"


class CanonicalAnswerSensitivity(StrEnum):
    BASIC = "BASIC"
    PERSONAL = "PERSONAL"
    LEGAL = "LEGAL"
    COMPENSATION = "COMPENSATION"
    VOLUNTARY_SELF_ID = "VOLUNTARY_SELF_ID"
    APPLICATION_MATERIAL = "APPLICATION_MATERIAL"
    ATTESTATION = "ATTESTATION"
    UNSUPPORTED = "UNSUPPORTED"


class CanonicalAnswerAutomationCategory(StrEnum):
    ORDINARY_FACT = "ORDINARY_FACT"
    SENSITIVE_FACT = "SENSITIVE_FACT"
    VOLUNTARY_DEMOGRAPHIC = "VOLUNTARY_DEMOGRAPHIC"
    APPLICATION_MATERIAL = "APPLICATION_MATERIAL"
    REQUIRES_ATTESTATION = "REQUIRES_ATTESTATION"
    UNSUPPORTED_UNKNOWN = "UNSUPPORTED_UNKNOWN"


class CanonicalSemanticMappingStatus(StrEnum):
    MAPPED = "mapped"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class CanonicalApplicationAnswerDefinition:
    key: CanonicalApplicationAnswerKey
    value_type: CanonicalAnswerValueType
    sensitivity: CanonicalAnswerSensitivity
    automation_category: CanonicalAnswerAutomationCategory
    aliases: tuple[str, ...] = ()
    taxonomy_version: str = CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "key", CanonicalApplicationAnswerKey(self.key)
        )
        object.__setattr__(
            self, "value_type", CanonicalAnswerValueType(self.value_type)
        )
        object.__setattr__(
            self,
            "sensitivity",
            CanonicalAnswerSensitivity(self.sensitivity),
        )
        object.__setattr__(
            self,
            "automation_category",
            CanonicalAnswerAutomationCategory(self.automation_category),
        )
        if (
            self.taxonomy_version
            != CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
        ):
            raise ValueError("taxonomy definition version is unsupported")
        if (
            not isinstance(self.aliases, tuple)
            or any(
                not isinstance(alias, str)
                or not alias
                or alias != alias.strip().casefold()
                for alias in self.aliases
            )
            or len(self.aliases) != len(set(self.aliases))
        ):
            raise ValueError("taxonomy aliases are invalid")

    @property
    def semantic_mapping_status(
        self,
    ) -> CanonicalSemanticMappingStatus:
        if (
            self.automation_category
            is CanonicalAnswerAutomationCategory.UNSUPPORTED_UNKNOWN
        ):
            return CanonicalSemanticMappingStatus.UNSUPPORTED
        if self.automation_category in {
            CanonicalAnswerAutomationCategory.SENSITIVE_FACT,
            CanonicalAnswerAutomationCategory.VOLUNTARY_DEMOGRAPHIC,
            CanonicalAnswerAutomationCategory.REQUIRES_ATTESTATION,
        }:
            return CanonicalSemanticMappingStatus.NEEDS_REVIEW
        return CanonicalSemanticMappingStatus.MAPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "aliases": list(self.aliases),
            "automation_category": self.automation_category.value,
            "key": self.key.value,
            "sensitivity": self.sensitivity.value,
            "taxonomy_version": self.taxonomy_version,
            "value_type": self.value_type.value,
        }


def _definition(
    key: CanonicalApplicationAnswerKey,
    value_type: CanonicalAnswerValueType,
    sensitivity: CanonicalAnswerSensitivity,
    automation: CanonicalAnswerAutomationCategory,
    *aliases: str,
) -> CanonicalApplicationAnswerDefinition:
    return CanonicalApplicationAnswerDefinition(
        key=key,
        value_type=value_type,
        sensitivity=sensitivity,
        automation_category=automation,
        aliases=tuple(aliases),
    )


_BASIC_TEXT = (
    CanonicalAnswerValueType.TEXT,
    CanonicalAnswerSensitivity.BASIC,
    CanonicalAnswerAutomationCategory.ORDINARY_FACT,
)
_PERSONAL_TEXT = (
    CanonicalAnswerValueType.TEXT,
    CanonicalAnswerSensitivity.PERSONAL,
    CanonicalAnswerAutomationCategory.SENSITIVE_FACT,
)
_DEFINITIONS = (
    _definition(CanonicalApplicationAnswerKey.PREFERRED_NAME, *_BASIC_TEXT),
    _definition(CanonicalApplicationAnswerKey.FIRST_NAME, *_BASIC_TEXT),
    _definition(CanonicalApplicationAnswerKey.LAST_NAME, *_BASIC_TEXT),
    _definition(CanonicalApplicationAnswerKey.FULL_NAME, *_BASIC_TEXT),
    _definition(CanonicalApplicationAnswerKey.EMAIL, *_BASIC_TEXT),
    _definition(
        CanonicalApplicationAnswerKey.PHONE,
        *_BASIC_TEXT,
        "phone_number",
    ),
    _definition(CanonicalApplicationAnswerKey.LOCATION, *_PERSONAL_TEXT),
    _definition(CanonicalApplicationAnswerKey.ADDRESS, *_PERSONAL_TEXT),
    _definition(CanonicalApplicationAnswerKey.CITY, *_PERSONAL_TEXT),
    _definition(CanonicalApplicationAnswerKey.STATE, *_PERSONAL_TEXT),
    _definition(CanonicalApplicationAnswerKey.POSTAL_CODE, *_PERSONAL_TEXT),
    _definition(CanonicalApplicationAnswerKey.COUNTRY, *_PERSONAL_TEXT),
    _definition(CanonicalApplicationAnswerKey.LINKEDIN, *_BASIC_TEXT),
    _definition(CanonicalApplicationAnswerKey.GITHUB, *_BASIC_TEXT),
    _definition(CanonicalApplicationAnswerKey.PORTFOLIO, *_BASIC_TEXT),
    _definition(
        CanonicalApplicationAnswerKey.CURRENT_COMPANY,
        CanonicalAnswerValueType.TEXT,
        CanonicalAnswerSensitivity.PERSONAL,
        CanonicalAnswerAutomationCategory.SENSITIVE_FACT,
    ),
    _definition(
        CanonicalApplicationAnswerKey.RESUME,
        CanonicalAnswerValueType.FILE_REFERENCE,
        CanonicalAnswerSensitivity.APPLICATION_MATERIAL,
        CanonicalAnswerAutomationCategory.APPLICATION_MATERIAL,
    ),
    _definition(
        CanonicalApplicationAnswerKey.COVER_LETTER,
        CanonicalAnswerValueType.TEXT,
        CanonicalAnswerSensitivity.APPLICATION_MATERIAL,
        CanonicalAnswerAutomationCategory.APPLICATION_MATERIAL,
    ),
    _definition(
        CanonicalApplicationAnswerKey.COVER_LETTER_FILE,
        CanonicalAnswerValueType.FILE_REFERENCE,
        CanonicalAnswerSensitivity.APPLICATION_MATERIAL,
        CanonicalAnswerAutomationCategory.APPLICATION_MATERIAL,
    ),
    _definition(
        CanonicalApplicationAnswerKey.WORK_AUTHORIZATION,
        CanonicalAnswerValueType.BOOLEAN,
        CanonicalAnswerSensitivity.LEGAL,
        CanonicalAnswerAutomationCategory.SENSITIVE_FACT,
        "authorized_to_work",
    ),
    _definition(
        CanonicalApplicationAnswerKey.SPONSORSHIP,
        CanonicalAnswerValueType.BOOLEAN,
        CanonicalAnswerSensitivity.LEGAL,
        CanonicalAnswerAutomationCategory.SENSITIVE_FACT,
        "require_sponsorship",
    ),
    _definition(
        CanonicalApplicationAnswerKey.RELOCATION,
        CanonicalAnswerValueType.BOOLEAN,
        CanonicalAnswerSensitivity.PERSONAL,
        CanonicalAnswerAutomationCategory.SENSITIVE_FACT,
        "willing_to_relocate",
    ),
    _definition(
        CanonicalApplicationAnswerKey.SALARY,
        CanonicalAnswerValueType.TEXT,
        CanonicalAnswerSensitivity.COMPENSATION,
        CanonicalAnswerAutomationCategory.SENSITIVE_FACT,
        "salary_expectation",
    ),
    _definition(
        CanonicalApplicationAnswerKey.START_DATE,
        CanonicalAnswerValueType.TEXT,
        CanonicalAnswerSensitivity.PERSONAL,
        CanonicalAnswerAutomationCategory.SENSITIVE_FACT,
        "earliest_start_date",
    ),
    *(
        _definition(
            key,
            CanonicalAnswerValueType.ENUM,
            CanonicalAnswerSensitivity.VOLUNTARY_SELF_ID,
            CanonicalAnswerAutomationCategory.VOLUNTARY_DEMOGRAPHIC,
        )
        for key in (
            CanonicalApplicationAnswerKey.GENDER,
            CanonicalApplicationAnswerKey.RACE_ETHNICITY,
            CanonicalApplicationAnswerKey.VETERAN_STATUS,
            CanonicalApplicationAnswerKey.DISABILITY_STATUS,
        )
    ),
    *(
        _definition(
            key,
            CanonicalAnswerValueType.ATTESTATION,
            CanonicalAnswerSensitivity.ATTESTATION,
            CanonicalAnswerAutomationCategory.REQUIRES_ATTESTATION,
        )
        for key in (
            CanonicalApplicationAnswerKey.ATTESTATION,
            CanonicalApplicationAnswerKey.CONSENT,
            CanonicalApplicationAnswerKey.SIGNATURE,
        )
    ),
    _definition(
        CanonicalApplicationAnswerKey.UNKNOWN,
        CanonicalAnswerValueType.UNKNOWN,
        CanonicalAnswerSensitivity.UNSUPPORTED,
        CanonicalAnswerAutomationCategory.UNSUPPORTED_UNKNOWN,
    ),
)

CANONICAL_APPLICATION_ANSWER_TAXONOMY = MappingProxyType(
    {definition.key: definition for definition in _DEFINITIONS}
)
_ALIASES = MappingProxyType(
    {
        alias: definition.key
        for definition in _DEFINITIONS
        for alias in definition.aliases
    }
)

if set(CANONICAL_APPLICATION_ANSWER_TAXONOMY) != set(
    CanonicalApplicationAnswerKey
):
    raise RuntimeError("canonical application-answer taxonomy is incomplete")
if set(_ALIASES) & {
    key.value for key in CanonicalApplicationAnswerKey
}:
    raise RuntimeError("a taxonomy alias collides with a canonical key")
if len(_ALIASES) != sum(
    len(definition.aliases) for definition in _DEFINITIONS
):
    raise RuntimeError("canonical application-answer aliases are ambiguous")


def canonical_application_answer_definition(
    key: CanonicalApplicationAnswerKey | str,
) -> CanonicalApplicationAnswerDefinition:
    normalized = normalize_canonical_application_answer_key(key)
    return CANONICAL_APPLICATION_ANSWER_TAXONOMY[normalized]


def normalize_canonical_application_answer_key(
    value: CanonicalApplicationAnswerKey | str,
    *,
    allow_legacy_alias: bool = False,
    allow_custom_unknown: bool = False,
) -> CanonicalApplicationAnswerKey:
    if isinstance(value, CanonicalApplicationAnswerKey):
        return value
    if not isinstance(value, str):
        raise TypeError("canonical application-answer key must be a string")
    normalized = value.strip().casefold()
    try:
        return CanonicalApplicationAnswerKey(normalized)
    except ValueError:
        if allow_legacy_alias and normalized in _ALIASES:
            return _ALIASES[normalized]
        if allow_custom_unknown and normalized.startswith("custom:"):
            return CanonicalApplicationAnswerKey.UNKNOWN
        raise ValueError(
            "key is outside the canonical application-answer taxonomy"
        ) from None


def canonical_application_answer_taxonomy_dict() -> dict[str, Any]:
    return {
        "definitions": [
            CANONICAL_APPLICATION_ANSWER_TAXONOMY[key].to_dict()
            for key in sorted(
                CANONICAL_APPLICATION_ANSWER_TAXONOMY,
                key=lambda item: item.value,
            )
        ],
        "taxonomy_version": (
            CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
        ),
    }


def canonical_application_answer_taxonomy_hash() -> str:
    payload = json.dumps(
        canonical_application_answer_taxonomy_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalApplicationAnswers(Mapping[CanonicalApplicationAnswerKey, Any]):
    """Immutable answer mapping whose keys belong to the shared taxonomy."""

    entries: tuple[tuple[CanonicalApplicationAnswerKey, Any], ...]
    taxonomy_version: str = CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION

    def __post_init__(self) -> None:
        if (
            self.taxonomy_version
            != CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
        ):
            raise ValueError("answer mapping taxonomy version is unsupported")
        if not isinstance(self.entries, tuple):
            raise TypeError("answer mapping entries must be a tuple")
        normalized: list[tuple[CanonicalApplicationAnswerKey, Any]] = []
        for entry in self.entries:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError("answer mapping entry is invalid")
            key, answer = entry
            normalized.append(
                (normalize_canonical_application_answer_key(key), answer)
            )
        keys = [key for key, _ in normalized]
        if len(keys) != len(set(keys)):
            raise ValueError("answer mapping contains duplicate keys")
        normalized.sort(key=lambda item: item[0].value)
        object.__setattr__(self, "entries", tuple(normalized))

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[CanonicalApplicationAnswerKey | str, Any],
    ) -> "CanonicalApplicationAnswers":
        if not isinstance(values, Mapping):
            raise TypeError("answers must be a mapping")
        return cls(
            entries=tuple(
                (
                    normalize_canonical_application_answer_key(key),
                    value,
                )
                for key, value in values.items()
            )
        )

    @classmethod
    def from_legacy(
        cls,
        values: Mapping[str, Any],
    ) -> "CanonicalApplicationAnswers":
        if not isinstance(values, Mapping):
            raise TypeError("legacy answers must be a mapping")
        return cls(
            entries=tuple(
                (
                    normalize_canonical_application_answer_key(
                        key, allow_legacy_alias=True
                    ),
                    value,
                )
                for key, value in values.items()
            )
        )

    def __getitem__(
        self, key: CanonicalApplicationAnswerKey | str
    ) -> Any:
        normalized = normalize_canonical_application_answer_key(key)
        for candidate, value in self.entries:
            if candidate is normalized:
                return value
        raise KeyError(normalized.value)

    def __iter__(self) -> Iterator[CanonicalApplicationAnswerKey]:
        return (key for key, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {key.value: value for key, value in self.entries}


__all__ = [
    "CANONICAL_APPLICATION_ANSWER_TAXONOMY",
    "CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION",
    "CanonicalAnswerAutomationCategory",
    "CanonicalAnswerSensitivity",
    "CanonicalAnswerValueType",
    "CanonicalApplicationAnswerDefinition",
    "CanonicalApplicationAnswerKey",
    "CanonicalApplicationAnswers",
    "CanonicalSemanticMappingStatus",
    "canonical_application_answer_definition",
    "canonical_application_answer_taxonomy_dict",
    "canonical_application_answer_taxonomy_hash",
    "normalize_canonical_application_answer_key",
]
