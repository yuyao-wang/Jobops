"""Closed candidate identity inputs for production application execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


APPLICATION_EXECUTION_IDENTITY_PROFILE_CONTRACT_VERSION = (
    "application-execution-identity-profile-v1"
)
APPLICATION_BUNDLE_CONSUMER_CLASSIFICATION_VERSION = (
    "application-bundle-consumer-classification-v1"
)
APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION = (
    "application-execution-identity-fields-v1"
)
APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION = (
    "application-execution-identity-normalization-v1"
)


class ApplicationExecutionIdentityFieldKey(StrEnum):
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    PREFERRED_NAME = "preferred_name"
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


class ApplicationExecutionIdentityFieldRequiredness(StrEnum):
    REQUIRED_FOR_EXECUTION = "REQUIRED_FOR_EXECUTION"
    OPTIONAL = "OPTIONAL"


class ApplicationExecutionIdentityFieldValueType(StrEnum):
    TEXT = "TEXT"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    URL = "URL"


@dataclass(frozen=True, slots=True)
class ApplicationExecutionIdentityFieldDefinition:
    field_key: ApplicationExecutionIdentityFieldKey
    value_type: ApplicationExecutionIdentityFieldValueType
    requiredness: ApplicationExecutionIdentityFieldRequiredness
    agent_proposal_allowed: bool
    text_evidence_allowed: bool
    image_evidence_allowed: bool
    normalization_policy_version: str = (
        APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION
    )


APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS = tuple(
    ApplicationExecutionIdentityFieldKey
)
_FIELD_NAMES = frozenset(item.value for item in APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS)
_URL_FIELDS = frozenset(
    {
        ApplicationExecutionIdentityFieldKey.LINKEDIN,
        ApplicationExecutionIdentityFieldKey.GITHUB,
        ApplicationExecutionIdentityFieldKey.PORTFOLIO,
    }
)
APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS = tuple(
    ApplicationExecutionIdentityFieldDefinition(
        field_key=key,
        value_type=(
            ApplicationExecutionIdentityFieldValueType.EMAIL
            if key is ApplicationExecutionIdentityFieldKey.EMAIL
            else ApplicationExecutionIdentityFieldValueType.PHONE
            if key is ApplicationExecutionIdentityFieldKey.PHONE
            else ApplicationExecutionIdentityFieldValueType.URL
            if key in _URL_FIELDS
            else ApplicationExecutionIdentityFieldValueType.TEXT
        ),
        requiredness=(
            ApplicationExecutionIdentityFieldRequiredness.REQUIRED_FOR_EXECUTION
            if key
            in {
                ApplicationExecutionIdentityFieldKey.FIRST_NAME,
                ApplicationExecutionIdentityFieldKey.LAST_NAME,
                ApplicationExecutionIdentityFieldKey.EMAIL,
            }
            else ApplicationExecutionIdentityFieldRequiredness.OPTIONAL
        ),
        agent_proposal_allowed=True,
        text_evidence_allowed=True,
        image_evidence_allowed=True,
    )
    for key in APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS
)
APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY = MappingProxyType(
    {definition.field_key: definition for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS}
)


class ApplicationBundleConsumerInputClass(StrEnum):
    IDENTITY_PROFILE = "IDENTITY_PROFILE"
    APPLICATION_ANSWERS = "APPLICATION_ANSWERS"
    MATERIALS = "MATERIALS"
    JOB_CONTEXT = "JOB_CONTEXT"
    EXECUTION_POLICY = "EXECUTION_POLICY"
    ADAPTER_RUNTIME_CONFIG = "ADAPTER_RUNTIME_CONFIG"


@dataclass(frozen=True, slots=True)
class ApplicationBundleConsumerBinding:
    consumer_id: str
    input_classes: tuple[ApplicationBundleConsumerInputClass, ...]
    contract_version: str = APPLICATION_BUNDLE_CONSUMER_CLASSIFICATION_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.consumer_id, str)
            or not self.consumer_id.strip()
            or self.consumer_id != self.consumer_id.strip()
        ):
            raise ValueError("consumer_id is invalid")
        if self.contract_version != APPLICATION_BUNDLE_CONSUMER_CLASSIFICATION_VERSION:
            raise ValueError("consumer classification version is unsupported")
        classes = tuple(ApplicationBundleConsumerInputClass(item) for item in self.input_classes)
        if not classes or len(classes) != len(set(classes)):
            raise ValueError("consumer input classes are invalid")
        object.__setattr__(self, "input_classes", classes)


APPLICATION_BUNDLE_CONSUMER_BINDINGS = (
    ApplicationBundleConsumerBinding(
        consumer_id="application_engine",
        input_classes=(
            ApplicationBundleConsumerInputClass.IDENTITY_PROFILE,
            ApplicationBundleConsumerInputClass.APPLICATION_ANSWERS,
            ApplicationBundleConsumerInputClass.MATERIALS,
            ApplicationBundleConsumerInputClass.JOB_CONTEXT,
            ApplicationBundleConsumerInputClass.EXECUTION_POLICY,
        ),
    ),
    ApplicationBundleConsumerBinding(
        consumer_id="base_ats_adapter",
        input_classes=(
            ApplicationBundleConsumerInputClass.IDENTITY_PROFILE,
            ApplicationBundleConsumerInputClass.APPLICATION_ANSWERS,
            ApplicationBundleConsumerInputClass.MATERIALS,
            ApplicationBundleConsumerInputClass.JOB_CONTEXT,
        ),
    ),
    ApplicationBundleConsumerBinding(
        consumer_id="generic_ai_adapter",
        input_classes=(
            ApplicationBundleConsumerInputClass.IDENTITY_PROFILE,
            ApplicationBundleConsumerInputClass.APPLICATION_ANSWERS,
            ApplicationBundleConsumerInputClass.MATERIALS,
            ApplicationBundleConsumerInputClass.JOB_CONTEXT,
        ),
    ),
    ApplicationBundleConsumerBinding(
        consumer_id="workday_adapter",
        input_classes=(
            ApplicationBundleConsumerInputClass.IDENTITY_PROFILE,
            ApplicationBundleConsumerInputClass.APPLICATION_ANSWERS,
            ApplicationBundleConsumerInputClass.MATERIALS,
            ApplicationBundleConsumerInputClass.JOB_CONTEXT,
            ApplicationBundleConsumerInputClass.ADAPTER_RUNTIME_CONFIG,
        ),
    ),
)


def _profile_value(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"identity field {name} must be a string")
    preserved = value.strip()
    if not preserved:
        return None
    if len(preserved) > 2_000:
        raise ValueError(f"identity field {name} exceeds the contract")
    return preserved


def normalize_application_execution_identity_value(
    field_key: ApplicationExecutionIdentityFieldKey | str,
    value: Any,
) -> str:
    """Normalize one closed identity value without inference or external I/O."""

    key = ApplicationExecutionIdentityFieldKey(field_key)
    normalized = _profile_value(key.value, value)
    if normalized is None:
        raise ValueError(f"identity field {key.value} is empty")
    if key is ApplicationExecutionIdentityFieldKey.EMAIL:
        if normalized.count("@") != 1:
            raise ValueError("identity email is invalid")
        local, domain = normalized.rsplit("@", 1)
        if not local or not domain or any(char.isspace() for char in normalized):
            raise ValueError("identity email is invalid")
        normalized = f"{local}@{domain.casefold()}"
    elif key is ApplicationExecutionIdentityFieldKey.PHONE:
        if not any(char.isdigit() for char in normalized):
            raise ValueError("identity phone is invalid")
        if any(
            not (char.isdigit() or char in " +().-")
            for char in normalized
        ):
            raise ValueError("identity phone is invalid")
    elif key in _URL_FIELDS:
        parsed = urlsplit(normalized)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(f"identity {key.value} URL is invalid")
        host = parsed.hostname.casefold()
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        normalized = urlunsplit(
            (
                parsed.scheme.casefold(),
                host,
                parsed.path,
                parsed.query,
                "",
            )
        )
    return normalized


@dataclass(frozen=True, slots=True)
class ApplicationExecutionIdentityProfile:
    """Closed identity/contact projection consumed by production adapters.

    Verification and per-field provenance intentionally remain outside this
    contract until P2c1d1 can bind these values to immutable Candidate facts.
    """

    first_name: str | None = None
    last_name: str | None = None
    preferred_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    contract_version: str = APPLICATION_EXECUTION_IDENTITY_PROFILE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != APPLICATION_EXECUTION_IDENTITY_PROFILE_CONTRACT_VERSION:
            raise ValueError("identity profile contract version is unsupported")
        for key in APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS:
            object.__setattr__(
                self,
                key.value,
                _profile_value(key.value, getattr(self, key.value)),
            )

    @classmethod
    def from_application_bundle_profile(
        cls,
        value: Mapping[str, Any],
    ) -> "ApplicationExecutionIdentityProfile":
        """Read the new closed ``{"personal": ...}`` production projection."""

        if not isinstance(value, Mapping) or not set(value).issubset(
            {"personal"}
        ):
            raise ValueError(
                "production ApplicationBundle profile must contain only personal"
            )
        personal = value.get("personal", {})
        if not isinstance(personal, Mapping):
            raise TypeError("production personal profile must be a mapping")
        unknown = set(personal) - _FIELD_NAMES
        if unknown:
            raise ValueError("production personal profile contains an unknown field")
        return cls(**{key: personal[key] for key in personal})

    @classmethod
    def from_legacy_profile(
        cls,
        value: Mapping[str, Any],
    ) -> "ApplicationExecutionIdentityProfile":
        """Explicit compatibility boundary for historical mixed profiles only."""

        if not isinstance(value, Mapping):
            raise TypeError("legacy profile must be a mapping")
        personal = value.get("personal", {})
        if not isinstance(personal, Mapping):
            raise TypeError("legacy personal profile must be a mapping")
        legacy_values = {
            key.value: personal.get(key.value)
            for key in APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS
        }
        if legacy_values["email"] is None:
            legacy_values["email"] = value.get("email")
        return cls(
            **legacy_values
        )

    def value_for(self, key: ApplicationExecutionIdentityFieldKey | str) -> str | None:
        return getattr(self, ApplicationExecutionIdentityFieldKey(key).value)

    @property
    def full_name(self) -> str | None:
        parts = tuple(item for item in (self.first_name, self.last_name) if item)
        return " ".join(parts) if parts else None

    def to_application_bundle_profile(self) -> Mapping[str, object]:
        personal = {
            key.value: value
            for key in APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS
            if (value := getattr(self, key.value)) is not None
        }
        return MappingProxyType({"personal": MappingProxyType(personal)})

    def redaction_values(self) -> tuple[str, ...]:
        full_name = self.full_name
        return tuple(
            sorted(
                {
                    value
                    for key in APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS
                    if (value := getattr(self, key.value))
                }
                | ({full_name} if full_name else set()),
                key=len,
                reverse=True,
            )
        )


__all__ = [
    "APPLICATION_BUNDLE_CONSUMER_BINDINGS",
    "APPLICATION_BUNDLE_CONSUMER_CLASSIFICATION_VERSION",
    "APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS",
    "APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS",
    "APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY",
    "APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION",
    "APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION",
    "APPLICATION_EXECUTION_IDENTITY_PROFILE_CONTRACT_VERSION",
    "ApplicationBundleConsumerBinding",
    "ApplicationBundleConsumerInputClass",
    "ApplicationExecutionIdentityFieldKey",
    "ApplicationExecutionIdentityFieldDefinition",
    "ApplicationExecutionIdentityFieldRequiredness",
    "ApplicationExecutionIdentityFieldValueType",
    "ApplicationExecutionIdentityProfile",
    "normalize_application_execution_identity_value",
]
