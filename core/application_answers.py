"""Plan-scoped preparation of canonical application answers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .application_answer_taxonomy import (
    CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION,
    CanonicalAnswerSensitivity,
    CanonicalAnswerValueType,
    CanonicalApplicationAnswerKey,
    canonical_application_answer_definition,
    canonical_application_answer_taxonomy_hash,
    normalize_canonical_application_answer_key,
)
from .application_attestation import (
    ApplicationAttestationDecision,
    PlanScopedApplicationAttestationProvider,
)
from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .application_preparation_orchestrator import (
    APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION,
    ApplicationAnswersStopReason,
    ApplicationPreparationStage,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PublicPreparationStageResult,
)
from .private_home import PrivateHome, PrivateHomeError
from .profile_store import CandidateVault, ProfileStoreError


APPLICATION_FACT_SNAPSHOT_CONTRACT_VERSION = (
    "application-fact-snapshot-v1"
)
APPLICATION_ANSWER_POLICY_VERSION = "application-answer-policy-v2"
SUPPORTED_APPLICATION_ANSWER_POLICY_VERSIONS = frozenset(
    {"application-answer-policy-v1", APPLICATION_ANSWER_POLICY_VERSION}
)
PREPARED_APPLICATION_ANSWER_SET_CONTRACT_VERSION = (
    "prepared-application-answer-set-v1"
)
DEFAULT_APPLICATION_ANSWER_POLICY_ID = (
    "application-answer-policy-automation-first-v1"
)
DECLINE_TO_ANSWER = "DECLINE_TO_ANSWER"

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ANSWER_SET_ID_RE = re.compile(
    r"^prepared-application-answer-set-[a-f0-9]{64}$"
)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _clean_text(name: str, value: Any, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _require_aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return (
        _require_aware("timestamp", value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_time(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return _require_aware(
        name, datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _parse_optional_time(name: str, value: Any) -> datetime | None:
    return None if value is None else _parse_time(name, value)


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


class ApplicationFactSourceClassification(StrEnum):
    VERIFIED_FACT = "VERIFIED_FACT"
    USER_CONFIRMED = "USER_CONFIRMED"


class ApplicationFactVerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    USER_CONFIRMED = "USER_CONFIRMED"


@dataclass(frozen=True, slots=True)
class ApplicationFact:
    fact_id: str
    canonical_key: CanonicalApplicationAnswerKey
    value: Any
    source_record_id: str
    source: str
    source_classification: ApplicationFactSourceClassification
    verification_status: ApplicationFactVerificationStatus
    sensitivity: CanonicalAnswerSensitivity
    allowed_scope: Mapping[str, Any]
    recorded_at: datetime
    verified_at: datetime
    expires_at: datetime | None
    fact_content_hash: str

    def __post_init__(self) -> None:
        _clean_text("fact_id", self.fact_id, maximum=180)
        key = normalize_canonical_application_answer_key(
            self.canonical_key
        )
        object.__setattr__(self, "canonical_key", key)
        _clean_text(
            "source_record_id", self.source_record_id, maximum=180
        )
        _clean_text("source", self.source, maximum=200)
        classification = ApplicationFactSourceClassification(
            self.source_classification
        )
        verification = ApplicationFactVerificationStatus(
            self.verification_status
        )
        object.__setattr__(
            self, "source_classification", classification
        )
        object.__setattr__(self, "verification_status", verification)
        if (
            classification
            is ApplicationFactSourceClassification.VERIFIED_FACT
            and verification
            is not ApplicationFactVerificationStatus.VERIFIED
        ) or (
            classification
            is ApplicationFactSourceClassification.USER_CONFIRMED
            and verification
            is not ApplicationFactVerificationStatus.USER_CONFIRMED
        ):
            raise ValueError("fact source and verification conflict")
        sensitivity = CanonicalAnswerSensitivity(self.sensitivity)
        object.__setattr__(self, "sensitivity", sensitivity)
        if not isinstance(self.allowed_scope, Mapping):
            raise TypeError("allowed_scope must be a mapping")
        scope = dict(self.allowed_scope)
        if set(scope) - {"job_id", "job_ids"}:
            raise ValueError("application fact scope is unsupported")
        if "job_id" in scope and not isinstance(scope["job_id"], str):
            raise ValueError("job_id scope is invalid")
        if "job_ids" in scope and (
            not isinstance(scope["job_ids"], list)
            or any(not isinstance(item, str) for item in scope["job_ids"])
        ):
            raise ValueError("job_ids scope is invalid")
        object.__setattr__(self, "allowed_scope", scope)
        recorded = _require_aware("recorded_at", self.recorded_at)
        verified = _require_aware("verified_at", self.verified_at)
        expires = (
            _require_aware("expires_at", self.expires_at)
            if self.expires_at is not None
            else None
        )
        if verified < recorded:
            raise ValueError("verified_at cannot precede recorded_at")
        if expires is not None and expires <= verified:
            raise ValueError("expires_at must follow verified_at")
        object.__setattr__(self, "recorded_at", recorded)
        object.__setattr__(self, "verified_at", verified)
        object.__setattr__(self, "expires_at", expires)
        if _require_hash(
            "fact_content_hash", self.fact_content_hash
        ) != _canonical_hash(self.content_dict()):
            raise ValueError("application fact content hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "allowed_scope": dict(self.allowed_scope),
            "canonical_key": self.canonical_key.value,
            "expires_at": (
                _rfc3339(self.expires_at) if self.expires_at else None
            ),
            "fact_id": self.fact_id,
            "recorded_at": _rfc3339(self.recorded_at),
            "sensitivity": self.sensitivity.value,
            "source": self.source,
            "source_classification": self.source_classification.value,
            "source_record_id": self.source_record_id,
            "value": self.value,
            "verification_status": self.verification_status.value,
            "verified_at": _rfc3339(self.verified_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "fact_content_hash": self.fact_content_hash,
        }


def _fact_from_record(
    raw_key: str,
    record: Mapping[str, Any],
) -> ApplicationFact | None:
    if record.get("verified") is not True:
        return None
    # Existing loose answer records remain valid for execution, but only
    # records explicitly opting into this typed projection are authoritative.
    if "source_classification" not in record:
        return None
    required = {
        "fact_id",
        "source_record_id",
        "source",
        "source_classification",
        "sensitivity",
        "scope",
        "confirmed_at",
        "value",
    }
    if not required.issubset(record):
        raise ValueError("typed CandidateVault answer record is incomplete")
    try:
        try:
            key = normalize_canonical_application_answer_key(
                raw_key, allow_legacy_alias=True
            )
        except ValueError:
            key = CanonicalApplicationAnswerKey.UNKNOWN
        classification = ApplicationFactSourceClassification(
            record["source_classification"]
        )
        verification = (
            ApplicationFactVerificationStatus.VERIFIED
            if classification
            is ApplicationFactSourceClassification.VERIFIED_FACT
            else ApplicationFactVerificationStatus.USER_CONFIRMED
        )
        sensitivity = _stored_sensitivity(record["sensitivity"])
        value = record["value"]
        if (
            canonical_application_answer_definition(key).value_type
            is CanonicalAnswerValueType.MULTI_SELECT
            and isinstance(value, list)
        ):
            value = tuple(value)
        confirmed = _parse_time("confirmed_at", record["confirmed_at"])
        recorded = _parse_optional_time(
            "recorded_at", record.get("recorded_at")
        ) or confirmed
        expires = _parse_optional_time(
            "expires_at", record.get("expires_at")
        )
        content = {
            "allowed_scope": dict(record["scope"]),
            "canonical_key": key.value,
            "expires_at": _rfc3339(expires) if expires else None,
            "fact_id": record["fact_id"],
            "recorded_at": _rfc3339(recorded),
            "sensitivity": sensitivity.value,
            "source": record["source"],
            "source_classification": classification.value,
            "source_record_id": record["source_record_id"],
            "value": value,
            "verification_status": verification.value,
            "verified_at": _rfc3339(confirmed),
        }
        return ApplicationFact(
            fact_id=record["fact_id"],
            canonical_key=key,
            value=value,
            source_record_id=record["source_record_id"],
            source=record["source"],
            source_classification=classification,
            verification_status=verification,
            sensitivity=sensitivity,
            allowed_scope=dict(record["scope"]),
            recorded_at=recorded,
            verified_at=confirmed,
            expires_at=expires,
            fact_content_hash=_canonical_hash(content),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "typed CandidateVault answer record is invalid"
        ) from exc


def _stored_sensitivity(value: Any) -> CanonicalAnswerSensitivity:
    mapping = {
        "personal": CanonicalAnswerSensitivity.PERSONAL,
        "legal": CanonicalAnswerSensitivity.LEGAL,
        "compensation": CanonicalAnswerSensitivity.COMPENSATION,
        "voluntary_self_id": (
            CanonicalAnswerSensitivity.VOLUNTARY_SELF_ID
        ),
        "demographic": CanonicalAnswerSensitivity.VOLUNTARY_SELF_ID,
        "health": CanonicalAnswerSensitivity.HEALTH,
        "employment": CanonicalAnswerSensitivity.EMPLOYMENT,
        "education": CanonicalAnswerSensitivity.EDUCATION,
    }
    if isinstance(value, CanonicalAnswerSensitivity):
        return value
    try:
        return CanonicalAnswerSensitivity(value)
    except (TypeError, ValueError):
        if value in mapping:
            return mapping[value]
        raise ValueError("stored fact sensitivity is unsupported") from None


@dataclass(frozen=True, slots=True)
class ApplicationFactSnapshot:
    snapshot_id: str
    contract_version: str
    subject_id: str
    facts: tuple[ApplicationFact, ...]
    snapshot_content_hash: str

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != APPLICATION_FACT_SNAPSHOT_CONTRACT_VERSION
        ):
            raise ValueError("fact snapshot contract is unsupported")
        _clean_text("subject_id", self.subject_id, maximum=160)
        if not isinstance(self.facts, tuple) or any(
            not isinstance(item, ApplicationFact) for item in self.facts
        ):
            raise TypeError("facts must be a typed tuple")
        ordered = tuple(
            sorted(
                self.facts,
                key=lambda item: (
                    item.canonical_key.value,
                    item.fact_id,
                ),
            )
        )
        if ordered != self.facts:
            raise ValueError("application facts must be canonically ordered")
        if len({item.fact_id for item in self.facts}) != len(self.facts):
            raise ValueError("application fact IDs must be unique")
        content_hash = _canonical_hash(self.content_dict())
        if self.snapshot_content_hash != content_hash:
            raise ValueError("fact snapshot hash is invalid")
        expected_id = "application-fact-snapshot-" + content_hash
        if self.snapshot_id != expected_id:
            raise ValueError("fact snapshot ID is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "facts": [item.to_dict() for item in self.facts],
            "subject_id": self.subject_id,
        }


@runtime_checkable
class ApplicationFactProvider(Protocol):
    def get_current(self, subject_id: str) -> ApplicationFactSnapshot:
        """Return one current authoritative application-fact snapshot."""


class PrivateHomeApplicationFactProvider:
    """Project only explicitly typed trusted CandidateVault answer records."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def get_current(self, subject_id: str) -> ApplicationFactSnapshot:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        try:
            vault = CandidateVault.load(self._home)
        except ProfileStoreError as exc:
            raise ValueError("CandidateVault is unavailable") from exc
        if vault.facts.get("subject_id") != subject:
            raise ValueError("CandidateVault subject binding is invalid")
        records = vault.answers.get("answers")
        if not isinstance(records, Mapping):
            raise ValueError("CandidateVault answers are invalid")
        facts = tuple(
            sorted(
                (
                    fact
                    for raw_key, record in records.items()
                    if isinstance(raw_key, str)
                    and isinstance(record, Mapping)
                    and (
                        fact := _fact_from_record(raw_key, record)
                    )
                    is not None
                ),
                key=lambda item: (
                    item.canonical_key.value,
                    item.fact_id,
                ),
            )
        )
        content = {
            "contract_version": (
                APPLICATION_FACT_SNAPSHOT_CONTRACT_VERSION
            ),
            "facts": [item.to_dict() for item in facts],
            "subject_id": subject,
        }
        content_hash = _canonical_hash(content)
        return ApplicationFactSnapshot(
            snapshot_id="application-fact-snapshot-" + content_hash,
            contract_version=(
                APPLICATION_FACT_SNAPSHOT_CONTRACT_VERSION
            ),
            subject_id=subject,
            facts=facts,
            snapshot_content_hash=content_hash,
        )


@dataclass(frozen=True, slots=True)
class ApplicationAnswerPolicy:
    policy_id: str
    policy_version: str
    tracked_keys: tuple[CanonicalApplicationAnswerKey, ...]
    demographic_decline_keys: tuple[
        CanonicalApplicationAnswerKey, ...
    ]
    attestation_keys: tuple[CanonicalApplicationAnswerKey, ...]
    policy_content_hash: str

    def __post_init__(self) -> None:
        _clean_text("policy_id", self.policy_id, maximum=160)
        if self.policy_version not in (
            SUPPORTED_APPLICATION_ANSWER_POLICY_VERSIONS
        ):
            raise ValueError("answer policy version is unsupported")
        for name, values in (
            ("tracked_keys", self.tracked_keys),
            (
                "demographic_decline_keys",
                self.demographic_decline_keys,
            ),
            ("attestation_keys", self.attestation_keys),
        ):
            if not isinstance(values, tuple):
                raise TypeError(f"{name} must be a tuple")
            normalized = tuple(
                normalize_canonical_application_answer_key(item)
                for item in values
            )
            if normalized != tuple(
                sorted(set(normalized), key=lambda item: item.value)
            ):
                raise ValueError(f"{name} must be unique and ordered")
            object.__setattr__(self, name, normalized)
        if not set(self.demographic_decline_keys).issubset(
            self.tracked_keys
        ) or not set(self.attestation_keys).issubset(self.tracked_keys):
            raise ValueError("answer policy key subsets are invalid")
        if self.policy_content_hash != _canonical_hash(
            self.content_dict()
        ):
            raise ValueError("answer policy hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "attestation_keys": [
                item.value for item in self.attestation_keys
            ],
            "demographic_decline_keys": [
                item.value for item in self.demographic_decline_keys
            ],
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "tracked_keys": [item.value for item in self.tracked_keys],
        }

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        tracked_keys: tuple[CanonicalApplicationAnswerKey, ...],
        demographic_decline_keys: tuple[
            CanonicalApplicationAnswerKey, ...
        ] = (),
        attestation_keys: tuple[
            CanonicalApplicationAnswerKey, ...
        ] = (),
    ) -> "ApplicationAnswerPolicy":
        tracked = tuple(
            sorted(set(tracked_keys), key=lambda item: item.value)
        )
        demographic = tuple(
            sorted(
                set(demographic_decline_keys),
                key=lambda item: item.value,
            )
        )
        attestations = tuple(
            sorted(set(attestation_keys), key=lambda item: item.value)
        )
        content = {
            "attestation_keys": [item.value for item in attestations],
            "demographic_decline_keys": [
                item.value for item in demographic
            ],
            "policy_id": policy_id,
            "policy_version": APPLICATION_ANSWER_POLICY_VERSION,
            "tracked_keys": [item.value for item in tracked],
        }
        return cls(
            policy_id=policy_id,
            policy_version=APPLICATION_ANSWER_POLICY_VERSION,
            tracked_keys=tracked,
            demographic_decline_keys=demographic,
            attestation_keys=attestations,
            policy_content_hash=_canonical_hash(content),
        )

    @classmethod
    def default(cls) -> "ApplicationAnswerPolicy":
        demographic = tuple(
            sorted(
                (
                    CanonicalApplicationAnswerKey.GENDER,
                    CanonicalApplicationAnswerKey.RACE_ETHNICITY,
                    CanonicalApplicationAnswerKey.VETERAN_STATUS,
                    CanonicalApplicationAnswerKey.DISABILITY_STATUS,
                ),
                key=lambda item: item.value,
            )
        )
        attestations = tuple(
            sorted(
                (
                    CanonicalApplicationAnswerKey.ATTESTATION,
                    CanonicalApplicationAnswerKey.CONSENT,
                    CanonicalApplicationAnswerKey.SIGNATURE,
                ),
                key=lambda item: item.value,
            )
        )
        return cls.create(
            policy_id=DEFAULT_APPLICATION_ANSWER_POLICY_ID,
            tracked_keys=tuple(CanonicalApplicationAnswerKey),
            demographic_decline_keys=demographic,
            attestation_keys=attestations,
        )


class PreparedAnswerSource(StrEnum):
    VERIFIED_FACT = "VERIFIED_FACT"
    USER_CONFIRMED = "USER_CONFIRMED"
    POLICY_DEFAULT = "POLICY_DEFAULT"


class UnresolvedAnswerReason(StrEnum):
    MISSING_FACT = "MISSING_FACT"
    REQUIRES_ATTESTATION = "REQUIRES_ATTESTATION"
    REQUIRES_USER_CHOICE = "REQUIRES_USER_CHOICE"
    POLICY_FORBIDS_AUTOMATION = "POLICY_FORBIDS_AUTOMATION"
    UNSUPPORTED = "UNSUPPORTED"


class UnresolvedDefaultHandling(StrEnum):
    SAFE_TO_SKIP = "SAFE_TO_SKIP"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


@dataclass(frozen=True, slots=True)
class PreparedApplicationAnswer:
    canonical_key: CanonicalApplicationAnswerKey
    value: Any
    answer_source: PreparedAnswerSource
    supporting_fact_ids: tuple[str, ...]
    sensitivity: CanonicalAnswerSensitivity
    answer_content_hash: str

    def __post_init__(self) -> None:
        key = normalize_canonical_application_answer_key(
            self.canonical_key
        )
        if key is CanonicalApplicationAnswerKey.UNKNOWN:
            raise ValueError("UNKNOWN cannot be a prepared answer")
        object.__setattr__(self, "canonical_key", key)
        source = PreparedAnswerSource(self.answer_source)
        object.__setattr__(self, "answer_source", source)
        if (
            not isinstance(self.supporting_fact_ids, tuple)
            or any(
                not isinstance(item, str) or not item
                for item in self.supporting_fact_ids
            )
            or len(self.supporting_fact_ids)
            != len(set(self.supporting_fact_ids))
        ):
            raise ValueError("supporting fact IDs are invalid")
        if (
            source is PreparedAnswerSource.POLICY_DEFAULT
            and self.supporting_fact_ids
        ) or (
            source is not PreparedAnswerSource.POLICY_DEFAULT
            and not self.supporting_fact_ids
        ):
            raise ValueError("answer source does not match fact support")
        sensitivity = CanonicalAnswerSensitivity(self.sensitivity)
        object.__setattr__(self, "sensitivity", sensitivity)
        _validate_value(key, self.value)
        if self.answer_content_hash != _canonical_hash(
            self.content_dict()
        ):
            raise ValueError("answer item hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "answer_source": self.answer_source.value,
            "canonical_key": self.canonical_key.value,
            "sensitivity": self.sensitivity.value,
            "supporting_fact_ids": list(self.supporting_fact_ids),
            "value": self.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "answer_content_hash": self.answer_content_hash,
        }


@dataclass(frozen=True, slots=True)
class UnresolvedApplicationAnswer:
    canonical_key: CanonicalApplicationAnswerKey
    reason: UnresolvedAnswerReason
    default_handling: UnresolvedDefaultHandling
    blocking: bool
    required_human_action: str
    unresolved_content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_key",
            normalize_canonical_application_answer_key(
                self.canonical_key
            ),
        )
        reason = UnresolvedAnswerReason(self.reason)
        handling = UnresolvedDefaultHandling(self.default_handling)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "default_handling", handling)
        if type(self.blocking) is not bool:
            raise TypeError("blocking must be boolean")
        if self.blocking != (
            handling is UnresolvedDefaultHandling.HUMAN_REQUIRED
        ):
            raise ValueError("blocking conflicts with default handling")
        _clean_text(
            "required_human_action",
            self.required_human_action,
            maximum=300,
        )
        if self.unresolved_content_hash != _canonical_hash(
            self.content_dict()
        ):
            raise ValueError("unresolved item hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "blocking": self.blocking,
            "canonical_key": self.canonical_key.value,
            "default_handling": self.default_handling.value,
            "reason": self.reason.value,
            "required_human_action": self.required_human_action,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "unresolved_content_hash": self.unresolved_content_hash,
        }


def _validate_value(
    key: CanonicalApplicationAnswerKey, value: Any
) -> None:
    value_type = canonical_application_answer_definition(key).value_type
    valid = {
        CanonicalAnswerValueType.TEXT: (
            isinstance(value, str) and bool(value.strip())
        ),
        CanonicalAnswerValueType.BOOLEAN: type(value) is bool,
        CanonicalAnswerValueType.ENUM: (
            isinstance(value, str) and bool(value.strip())
        ),
        CanonicalAnswerValueType.MULTI_SELECT: (
            isinstance(value, tuple)
            and bool(value)
            and all(isinstance(item, str) and item for item in value)
        ),
        CanonicalAnswerValueType.FILE_REFERENCE: (
            isinstance(value, str) and bool(value.strip())
        ),
        CanonicalAnswerValueType.ATTESTATION: False,
        CanonicalAnswerValueType.UNKNOWN: False,
    }[value_type]
    if not valid:
        raise ValueError(
            f"value does not match taxonomy type {value_type.value}"
        )


@dataclass(frozen=True, slots=True)
class PreparedApplicationAnswerSet:
    answer_set_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    plan_instructions_hash: str
    fact_snapshot_id: str
    fact_snapshot_hash: str
    taxonomy_version: str
    taxonomy_hash: str
    answer_policy_id: str
    answer_policy_version: str
    answer_policy_hash: str
    answers: tuple[PreparedApplicationAnswer, ...]
    unresolved_items: tuple[UnresolvedApplicationAnswer, ...]
    answer_set_content_hash: str
    prepared_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != PREPARED_APPLICATION_ANSWER_SET_CONTRACT_VERSION
        ):
            raise ValueError("answer-set contract is unsupported")
        _clean_text("subject_id", self.subject_id, maximum=160)
        _clean_text(
            "application_plan_id",
            self.application_plan_id,
            maximum=180,
        )
        _clean_text("job_id", self.job_id, maximum=160)
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job revision is invalid")
        _require_hash("job_content_hash", self.job_content_hash)
        _require_hash(
            "plan_instructions_hash", self.plan_instructions_hash
        )
        _clean_text("fact_snapshot_id", self.fact_snapshot_id)
        _require_hash("fact_snapshot_hash", self.fact_snapshot_hash)
        if self.fact_snapshot_id != (
            "application-fact-snapshot-" + self.fact_snapshot_hash
        ):
            raise ValueError("fact snapshot ID/hash binding is invalid")
        if (
            self.taxonomy_version
            != CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
            or self.taxonomy_hash
            != canonical_application_answer_taxonomy_hash()
        ):
            raise ValueError("answer taxonomy binding is invalid")
        _clean_text("answer_policy_id", self.answer_policy_id)
        if self.answer_policy_version not in (
            SUPPORTED_APPLICATION_ANSWER_POLICY_VERSIONS
        ):
            raise ValueError("answer policy version is invalid")
        _require_hash("answer_policy_hash", self.answer_policy_hash)
        if not self.answers:
            raise ValueError("a successful answer set needs safe answers")
        if tuple(
            sorted(self.answers, key=lambda item: item.canonical_key.value)
        ) != self.answers:
            raise ValueError("prepared answers must be ordered")
        if tuple(
            sorted(
                self.unresolved_items,
                key=lambda item: item.canonical_key.value,
            )
        ) != self.unresolved_items:
            raise ValueError("unresolved answers must be ordered")
        answer_keys = [item.canonical_key for item in self.answers]
        unresolved_keys = [
            item.canonical_key for item in self.unresolved_items
        ]
        if (
            len(answer_keys) != len(set(answer_keys))
            or len(unresolved_keys) != len(set(unresolved_keys))
            or set(answer_keys) & set(unresolved_keys)
        ):
            raise ValueError("answer-set canonical keys conflict")
        identity = self.identity_dict()
        expected_id = "prepared-application-answer-set-" + _canonical_hash(
            identity
        )
        if (
            _ANSWER_SET_ID_RE.fullmatch(self.answer_set_id) is None
            or self.answer_set_id != expected_id
        ):
            raise ValueError("answer-set identity is invalid")
        _require_aware("prepared_at", self.prepared_at)
        if self.answer_set_content_hash != _canonical_hash(
            self.content_dict()
        ):
            raise ValueError("answer-set content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "answer_hashes": [
                item.answer_content_hash for item in self.answers
            ],
            "answer_policy_hash": self.answer_policy_hash,
            "answer_policy_id": self.answer_policy_id,
            "answer_policy_version": self.answer_policy_version,
            "application_plan_id": self.application_plan_id,
            "contract_version": self.contract_version,
            "fact_snapshot_hash": self.fact_snapshot_hash,
            "fact_snapshot_id": self.fact_snapshot_id,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "plan_instructions_hash": self.plan_instructions_hash,
            "subject_id": self.subject_id,
            "taxonomy_hash": self.taxonomy_hash,
            "taxonomy_version": self.taxonomy_version,
            "unresolved_hashes": [
                item.unresolved_content_hash
                for item in self.unresolved_items
            ],
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            "answer_set_id": self.answer_set_id,
            "answers": [item.to_dict() for item in self.answers],
            "answer_policy_hash": self.answer_policy_hash,
            "answer_policy_id": self.answer_policy_id,
            "answer_policy_version": self.answer_policy_version,
            "application_plan_id": self.application_plan_id,
            "contract_version": self.contract_version,
            "fact_snapshot_hash": self.fact_snapshot_hash,
            "fact_snapshot_id": self.fact_snapshot_id,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "plan_instructions_hash": self.plan_instructions_hash,
            "prepared_at": _rfc3339(self.prepared_at),
            "subject_id": self.subject_id,
            "taxonomy_hash": self.taxonomy_hash,
            "taxonomy_version": self.taxonomy_version,
            "unresolved_items": [
                item.to_dict() for item in self.unresolved_items
            ],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "answer_set_content_hash": self.answer_set_content_hash,
        }


class PreparedApplicationAnswerSetStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED_NO_TRUSTED_FACTS = "DEFERRED_NO_TRUSTED_FACTS"
    DEFERRED_NEEDS_HUMAN = "DEFERRED_NEEDS_HUMAN"
    FAILED = "FAILED"


class PreparedApplicationAnswerSetReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class PreparedApplicationAnswerSetWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class PreparedApplicationAnswerSetFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    FACT_SNAPSHOT_INTEGRITY_FAILURE = "FACT_SNAPSHOT_INTEGRITY_FAILURE"
    FACT_SNAPSHOT_SUBJECT_MISMATCH = "FACT_SNAPSHOT_SUBJECT_MISMATCH"
    FACT_VALUE_TYPE_MISMATCH = "FACT_VALUE_TYPE_MISMATCH"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    ANSWER_SET_INTEGRITY_FAILURE = "ANSWER_SET_INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class PreparedApplicationAnswerSetReadResult:
    status: PreparedApplicationAnswerSetReadStatus
    answer_set: PreparedApplicationAnswerSet | None


@dataclass(frozen=True, slots=True)
class PreparedApplicationAnswerSetWriteResult:
    status: PreparedApplicationAnswerSetWriteStatus
    answer_set: PreparedApplicationAnswerSet | None
    reason_code: PreparedApplicationAnswerSetFailureReason | None
    retryable: bool


@runtime_checkable
class PreparedApplicationAnswerSetRepository(Protocol):
    def get(
        self, *, subject_id: str, answer_set_id: str
    ) -> PreparedApplicationAnswerSetReadResult: ...

    def save(
        self, answer_set: PreparedApplicationAnswerSet
    ) -> PreparedApplicationAnswerSetWriteResult: ...

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> PreparedApplicationAnswerSetReadResult: ...


def _answer_from_dict(value: Mapping[str, Any]) -> PreparedApplicationAnswer:
    return PreparedApplicationAnswer(
        canonical_key=CanonicalApplicationAnswerKey(
            value["canonical_key"]
        ),
        value=value["value"],
        answer_source=PreparedAnswerSource(value["answer_source"]),
        supporting_fact_ids=tuple(value["supporting_fact_ids"]),
        sensitivity=CanonicalAnswerSensitivity(value["sensitivity"]),
        answer_content_hash=value["answer_content_hash"],
    )


def _unresolved_from_dict(
    value: Mapping[str, Any],
) -> UnresolvedApplicationAnswer:
    return UnresolvedApplicationAnswer(
        canonical_key=CanonicalApplicationAnswerKey(
            value["canonical_key"]
        ),
        reason=UnresolvedAnswerReason(value["reason"]),
        default_handling=UnresolvedDefaultHandling(
            value["default_handling"]
        ),
        blocking=value["blocking"],
        required_human_action=value["required_human_action"],
        unresolved_content_hash=value["unresolved_content_hash"],
    )


def _answer_set_from_dict(
    value: Mapping[str, Any],
) -> PreparedApplicationAnswerSet:
    expected = {
        "answer_policy_hash",
        "answer_policy_id",
        "answer_policy_version",
        "answer_set_content_hash",
        "answer_set_id",
        "answers",
        "application_plan_id",
        "contract_version",
        "fact_snapshot_hash",
        "fact_snapshot_id",
        "job_content_hash",
        "job_id",
        "job_revision",
        "plan_instructions_hash",
        "prepared_at",
        "subject_id",
        "taxonomy_hash",
        "taxonomy_version",
        "unresolved_items",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["answers"], list)
        or not isinstance(value["unresolved_items"], list)
    ):
        raise ValueError("persisted answer set is invalid")
    return PreparedApplicationAnswerSet(
        answer_set_id=value["answer_set_id"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        plan_instructions_hash=value["plan_instructions_hash"],
        fact_snapshot_id=value["fact_snapshot_id"],
        fact_snapshot_hash=value["fact_snapshot_hash"],
        taxonomy_version=value["taxonomy_version"],
        taxonomy_hash=value["taxonomy_hash"],
        answer_policy_id=value["answer_policy_id"],
        answer_policy_version=value["answer_policy_version"],
        answer_policy_hash=value["answer_policy_hash"],
        answers=tuple(_answer_from_dict(item) for item in value["answers"]),
        unresolved_items=tuple(
            _unresolved_from_dict(item)
            for item in value["unresolved_items"]
        ),
        answer_set_content_hash=value["answer_set_content_hash"],
        prepared_at=_parse_time("prepared_at", value["prepared_at"]),
    )


class PrivateHomePreparedApplicationAnswerSetRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, maximum=160)
        return (
            self._home.paths.prepared_application_answer_sets
            / _subject_key(subject)
        )

    def _path(self, subject_id: str, answer_set_id: str) -> Path:
        if (
            not isinstance(answer_set_id, str)
            or _ANSWER_SET_ID_RE.fullmatch(answer_set_id) is None
        ):
            raise ValueError("answer_set_id is invalid")
        return self._directory(subject_id) / f"{answer_set_id}.json"

    def get(
        self, *, subject_id: str, answer_set_id: str
    ) -> PreparedApplicationAnswerSetReadResult:
        path = self._path(subject_id, answer_set_id)
        with self._lock:
            if not path.exists():
                return PreparedApplicationAnswerSetReadResult(
                    PreparedApplicationAnswerSetReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return PreparedApplicationAnswerSetReadResult(
                    PreparedApplicationAnswerSetReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            try:
                answer_set = _answer_set_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return PreparedApplicationAnswerSetReadResult(
                    PreparedApplicationAnswerSetReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            if (
                answer_set.subject_id != subject_id.strip()
                or answer_set.answer_set_id != answer_set_id
            ):
                return PreparedApplicationAnswerSetReadResult(
                    PreparedApplicationAnswerSetReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            return PreparedApplicationAnswerSetReadResult(
                PreparedApplicationAnswerSetReadStatus.FOUND, answer_set
            )

    def save(
        self, answer_set: PreparedApplicationAnswerSet
    ) -> PreparedApplicationAnswerSetWriteResult:
        if not isinstance(answer_set, PreparedApplicationAnswerSet):
            raise TypeError("answer_set must be typed")
        path = self._path(
            answer_set.subject_id, answer_set.answer_set_id
        )
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            answer_set.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return PreparedApplicationAnswerSetWriteResult(
                    PreparedApplicationAnswerSetWriteStatus.FAILED,
                    None,
                    PreparedApplicationAnswerSetFailureReason
                    .PERSISTENCE_FAILED,
                    True,
                )
            if created:
                return PreparedApplicationAnswerSetWriteResult(
                    PreparedApplicationAnswerSetWriteStatus.CREATED,
                    answer_set,
                    None,
                    False,
                )
            existing = self.get(
                subject_id=answer_set.subject_id,
                answer_set_id=answer_set.answer_set_id,
            )
            if (
                existing.status
                is PreparedApplicationAnswerSetReadStatus.FOUND
                and existing.answer_set is not None
                and existing.answer_set.identity_dict()
                == answer_set.identity_dict()
            ):
                return PreparedApplicationAnswerSetWriteResult(
                    PreparedApplicationAnswerSetWriteStatus.UNCHANGED,
                    existing.answer_set,
                    None,
                    False,
                )
            return PreparedApplicationAnswerSetWriteResult(
                PreparedApplicationAnswerSetWriteStatus.FAILED,
                None,
                PreparedApplicationAnswerSetFailureReason
                .ANSWER_SET_INTEGRITY_FAILURE,
                False,
            )

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> PreparedApplicationAnswerSetReadResult:
        directory = self._directory(subject_id)
        if not directory.exists():
            return PreparedApplicationAnswerSetReadResult(
                PreparedApplicationAnswerSetReadStatus.NOT_FOUND, None
            )
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            return PreparedApplicationAnswerSetReadResult(
                PreparedApplicationAnswerSetReadStatus.INTEGRITY_FAILURE,
                None,
            )
        matches: list[PreparedApplicationAnswerSet] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or _ANSWER_SET_ID_RE.fullmatch(path.stem) is None
            ):
                return PreparedApplicationAnswerSetReadResult(
                    PreparedApplicationAnswerSetReadStatus
                    .INTEGRITY_FAILURE,
                    None,
                )
            read = self.get(
                subject_id=subject_id, answer_set_id=path.stem
            )
            if (
                read.status
                is not PreparedApplicationAnswerSetReadStatus.FOUND
                or read.answer_set is None
            ):
                return PreparedApplicationAnswerSetReadResult(
                    PreparedApplicationAnswerSetReadStatus
                    .INTEGRITY_FAILURE,
                    None,
                )
            if read.answer_set.application_plan_id == application_plan_id:
                matches.append(read.answer_set)
        if not matches:
            return PreparedApplicationAnswerSetReadResult(
                PreparedApplicationAnswerSetReadStatus.NOT_FOUND, None
            )
        current = max(
            matches,
            key=lambda item: (
                item.prepared_at.astimezone(timezone.utc),
                item.answer_set_id,
            ),
        )
        return PreparedApplicationAnswerSetReadResult(
            PreparedApplicationAnswerSetReadStatus.FOUND, current
        )


@dataclass(frozen=True, slots=True)
class PrepareApplicationAnswersCommand:
    subject_id: str
    application_plan_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class PrepareApplicationAnswersResult:
    status: PreparedApplicationAnswerSetStatus
    answer_set: PreparedApplicationAnswerSet | None
    reason_code: PreparedApplicationAnswerSetFailureReason | None
    retryable: bool
    message: str
    unresolved_reasons: tuple[UnresolvedAnswerReason, ...] = ()

    def __post_init__(self) -> None:
        status = PreparedApplicationAnswerSetStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PreparedApplicationAnswerSetFailureReason(self.reason_code),
            )
        if (
            not isinstance(self.unresolved_reasons, tuple)
            or any(
                not isinstance(item, UnresolvedAnswerReason)
                for item in self.unresolved_reasons
            )
            or tuple(
                sorted(
                    set(self.unresolved_reasons),
                    key=lambda item: item.value,
                )
            )
            != self.unresolved_reasons
        ):
            raise ValueError("unresolved reasons must be unique and ordered")
        if (
            status
            is PreparedApplicationAnswerSetStatus.DEFERRED_NEEDS_HUMAN
        ) != bool(self.unresolved_reasons):
            raise ValueError(
                "only a needs-human result carries unresolved reasons"
            )


def _unresolved(
    key: CanonicalApplicationAnswerKey,
    reason: UnresolvedAnswerReason,
    *,
    blocking: bool,
    action: str,
) -> UnresolvedApplicationAnswer:
    content = {
        "blocking": blocking,
        "canonical_key": key.value,
        "default_handling": (
            UnresolvedDefaultHandling.HUMAN_REQUIRED.value
            if blocking
            else UnresolvedDefaultHandling.SAFE_TO_SKIP.value
        ),
        "reason": reason.value,
        "required_human_action": action,
    }
    return UnresolvedApplicationAnswer(
        canonical_key=key,
        reason=reason,
        default_handling=(
            UnresolvedDefaultHandling.HUMAN_REQUIRED
            if blocking
            else UnresolvedDefaultHandling.SAFE_TO_SKIP
        ),
        blocking=blocking,
        required_human_action=action,
        unresolved_content_hash=_canonical_hash(content),
    )


def _prepared(
    key: CanonicalApplicationAnswerKey,
    value: Any,
    source: PreparedAnswerSource,
    fact_ids: tuple[str, ...],
    sensitivity: CanonicalAnswerSensitivity,
) -> PreparedApplicationAnswer:
    content = {
        "answer_source": source.value,
        "canonical_key": key.value,
        "sensitivity": sensitivity.value,
        "supporting_fact_ids": list(fact_ids),
        "value": value,
    }
    return PreparedApplicationAnswer(
        canonical_key=key,
        value=value,
        answer_source=source,
        supporting_fact_ids=fact_ids,
        sensitivity=sensitivity,
        answer_content_hash=_canonical_hash(content),
    )


def _scope_allows(fact: ApplicationFact, job_id: str) -> bool:
    scope = fact.allowed_scope
    if not scope:
        return True
    allowed: set[str] = set()
    if scope.get("job_id"):
        allowed.add(scope["job_id"])
    allowed.update(scope.get("job_ids", []))
    return job_id in allowed


def _forbidden_by_plan(plan: ApplicationPlan) -> set[
    CanonicalApplicationAnswerKey
]:
    text = " ".join(
        (plan.user_preparation_instructions or "").casefold().split()
    )
    forbidden: set[CanonicalApplicationAnswerKey] = set()
    for key in CanonicalApplicationAnswerKey:
        label = key.value.replace("_", " ")
        if any(
            phrase in text
            for phrase in (
                f"do not fill {label}",
                f"do not use {label}",
                f"omit {label}",
            )
        ):
            forbidden.add(key)
    if any(
        phrase in text
        for phrase in (
            "do not fill salary expectation",
            "do not include salary expectation",
            "不要填写薪资",
        )
    ):
        forbidden.add(CanonicalApplicationAnswerKey.SALARY)
    return forbidden


def _failure_result(
    reason: PreparedApplicationAnswerSetFailureReason,
    *,
    retryable: bool = False,
) -> PrepareApplicationAnswersResult:
    return PrepareApplicationAnswersResult(
        status=PreparedApplicationAnswerSetStatus.FAILED,
        answer_set=None,
        reason_code=reason,
        retryable=retryable,
        message=f"Application-answer preparation failed: {reason.value}.",
    )


def prepare_application_answers(
    command: PrepareApplicationAnswersCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    fact_provider: ApplicationFactProvider,
    answer_policy: ApplicationAnswerPolicy,
    answer_set_repository: PreparedApplicationAnswerSetRepository,
    attestation_provider: (
        PlanScopedApplicationAttestationProvider | None
    ) = None,
) -> PrepareApplicationAnswersResult:
    try:
        subject = _clean_text(
            "subject_id", command.subject_id, maximum=160
        )
        plan_id = _clean_text(
            "application_plan_id",
            command.application_plan_id,
            maximum=180,
        )
        now = _require_aware("now", command.now)
        if not isinstance(answer_policy, ApplicationAnswerPolicy):
            raise TypeError("answer_policy must be typed")
    except (AttributeError, TypeError, ValueError):
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason.INVALID_REQUEST
        )
    try:
        plan_read = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .APPLICATION_PLAN_NOT_FOUND
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_read.plan, ApplicationPlan)
    ):
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE
        )
    plan = plan_read.plan
    if plan.subject_id != subject:
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH
        )
    try:
        snapshot = fact_provider.get_current(subject)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .FACT_SNAPSHOT_INTEGRITY_FAILURE
        )
    if not isinstance(snapshot, ApplicationFactSnapshot):
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .FACT_SNAPSHOT_INTEGRITY_FAILURE
        )
    if snapshot.subject_id != subject:
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .FACT_SNAPSHOT_SUBJECT_MISMATCH
        )
    active_facts = tuple(
        fact
        for fact in snapshot.facts
        if fact.verified_at <= now
        and (fact.expires_at is None or fact.expires_at > now)
        and _scope_allows(fact, plan.job_id)
    )
    attestations = {}
    if attestation_provider is not None:
        try:
            for key in answer_policy.attestation_keys:
                attestation = attestation_provider.get_current(
                    subject_id=subject,
                    application_plan_id=plan.plan_id,
                    canonical_key=key,
                )
                if attestation is not None:
                    if (
                        attestation.subject_id != subject
                        or attestation.application_plan_id != plan.plan_id
                        or attestation.canonical_key is not key
                    ):
                        raise ValueError(
                            "attestation binding mismatch"
                        )
                    attestations[key] = attestation
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return _failure_result(
                PreparedApplicationAnswerSetFailureReason
                .FACT_SNAPSHOT_INTEGRITY_FAILURE
            )
    if not active_facts and not attestations:
        return PrepareApplicationAnswersResult(
            status=(
                PreparedApplicationAnswerSetStatus
                .DEFERRED_NO_TRUSTED_FACTS
            ),
            answer_set=None,
            reason_code=None,
            retryable=False,
            message="No current trusted application facts are available.",
        )

    by_key: dict[
        CanonicalApplicationAnswerKey, list[ApplicationFact]
    ] = {}
    for fact in active_facts:
        by_key.setdefault(fact.canonical_key, []).append(fact)
    forbidden = _forbidden_by_plan(plan)
    answers: list[PreparedApplicationAnswer] = []
    unresolved: list[UnresolvedApplicationAnswer] = []
    considered = set(answer_policy.tracked_keys) | set(by_key)
    for key in sorted(considered, key=lambda item: item.value):
        facts = by_key.get(key, [])
        if key is CanonicalApplicationAnswerKey.UNKNOWN:
            unresolved.append(
                _unresolved(
                    key,
                    UnresolvedAnswerReason.UNSUPPORTED,
                    blocking=False,
                    action=(
                        "Skip unless a later FormIR marks this exact "
                        "question required."
                    ),
                )
            )
            continue
        if key in answer_policy.attestation_keys:
            attestation = attestations.get(key)
            if (
                attestation is not None
                and attestation.decision
                is ApplicationAttestationDecision.CONFIRMED
            ):
                answers.append(
                    _prepared(
                        key,
                        True,
                        PreparedAnswerSource.USER_CONFIRMED,
                        (attestation.attestation_id,),
                        canonical_application_answer_definition(
                            key
                        ).sensitivity,
                    )
                )
                continue
            unresolved.append(
                _unresolved(
                    key,
                    UnresolvedAnswerReason.REQUIRES_ATTESTATION,
                    blocking=False,
                    action=(
                        "The candidate must personally review and attest "
                        "if a later FormIR observes this field as required."
                    ),
                )
            )
            continue
        if key in forbidden:
            unresolved.append(
                _unresolved(
                    key,
                    UnresolvedAnswerReason.POLICY_FORBIDS_AUTOMATION,
                    blocking=False,
                    action=(
                        "Respect the plan instruction and skip unless "
                        "the candidate changes it."
                    ),
                )
            )
            continue
        if not facts:
            if key in answer_policy.demographic_decline_keys:
                definition = canonical_application_answer_definition(key)
                answers.append(
                    _prepared(
                        key,
                        DECLINE_TO_ANSWER,
                        PreparedAnswerSource.POLICY_DEFAULT,
                        (),
                        definition.sensitivity,
                    )
                )
            else:
                unresolved.append(
                    _unresolved(
                        key,
                        UnresolvedAnswerReason.MISSING_FACT,
                        blocking=False,
                        action=(
                            "Skip by default; wait for the candidate only "
                            "if a later FormIR marks it required."
                        ),
                    )
                )
            continue
        values = {
            json.dumps(
                fact.value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            for fact in facts
        }
        if len(values) != 1:
            unresolved.append(
                _unresolved(
                    key,
                    UnresolvedAnswerReason.REQUIRES_USER_CHOICE,
                    blocking=True,
                    action=(
                        "The candidate must reconcile conflicting trusted "
                        "records."
                    ),
                )
            )
            continue
        try:
            _validate_value(key, facts[0].value)
        except ValueError:
            return _failure_result(
                PreparedApplicationAnswerSetFailureReason
                .FACT_VALUE_TYPE_MISMATCH
            )
        if (
            key is CanonicalApplicationAnswerKey.SALARY
            and not any(
                fact.source_classification
                is ApplicationFactSourceClassification.USER_CONFIRMED
                for fact in facts
            )
        ):
            unresolved.append(
                _unresolved(
                    key,
                    UnresolvedAnswerReason.REQUIRES_USER_CHOICE,
                    blocking=True,
                    action=(
                        "The candidate must explicitly confirm compensation "
                        "preference."
                    ),
                )
            )
            continue
        source = (
            PreparedAnswerSource.USER_CONFIRMED
            if any(
                fact.source_classification
                is ApplicationFactSourceClassification.USER_CONFIRMED
                for fact in facts
            )
            else PreparedAnswerSource.VERIFIED_FACT
        )
        sensitivity = max(
            (
                canonical_application_answer_definition(key).sensitivity,
                *(fact.sensitivity for fact in facts),
            ),
            key=lambda item: list(CanonicalAnswerSensitivity).index(item),
        )
        answers.append(
            _prepared(
                key,
                facts[0].value,
                source,
                tuple(sorted(fact.fact_id for fact in facts)),
                sensitivity,
            )
        )
    if not answers:
        unresolved_reasons = tuple(
            sorted(
                {item.reason for item in unresolved},
                key=lambda item: item.value,
            )
        )
        return PrepareApplicationAnswersResult(
            status=(
                PreparedApplicationAnswerSetStatus.DEFERRED_NEEDS_HUMAN
            ),
            answer_set=None,
            reason_code=None,
            retryable=False,
            message=(
                "Trusted records exist, but no answer is safe to prepare."
            ),
            unresolved_reasons=unresolved_reasons,
        )
    answers_tuple = tuple(
        sorted(answers, key=lambda item: item.canonical_key.value)
    )
    unresolved_tuple = tuple(
        sorted(unresolved, key=lambda item: item.canonical_key.value)
    )
    identity = {
        "answer_hashes": [
            item.answer_content_hash for item in answers_tuple
        ],
        "answer_policy_hash": answer_policy.policy_content_hash,
        "answer_policy_id": answer_policy.policy_id,
        "answer_policy_version": answer_policy.policy_version,
        "application_plan_id": plan.plan_id,
        "contract_version": (
            PREPARED_APPLICATION_ANSWER_SET_CONTRACT_VERSION
        ),
        "fact_snapshot_hash": snapshot.snapshot_content_hash,
        "fact_snapshot_id": snapshot.snapshot_id,
        "job_content_hash": plan.job_content_hash,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "plan_instructions_hash": (
            plan.user_preparation_instructions_hash
        ),
        "subject_id": subject,
        "taxonomy_hash": canonical_application_answer_taxonomy_hash(),
        "taxonomy_version": (
            CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
        ),
        "unresolved_hashes": [
            item.unresolved_content_hash for item in unresolved_tuple
        ],
    }
    answer_set_id = (
        "prepared-application-answer-set-" + _canonical_hash(identity)
    )
    try:
        existing = answer_set_repository.get(
            subject_id=subject, answer_set_id=answer_set_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .ANSWER_SET_INTEGRITY_FAILURE
        )
    if existing.status is PreparedApplicationAnswerSetReadStatus.FOUND:
        if existing.answer_set is None:
            return _failure_result(
                PreparedApplicationAnswerSetFailureReason
                .ANSWER_SET_INTEGRITY_FAILURE
            )
        return PrepareApplicationAnswersResult(
            status=PreparedApplicationAnswerSetStatus.UNCHANGED,
            answer_set=existing.answer_set,
            reason_code=None,
            retryable=False,
            message="The prepared application answers are unchanged.",
        )
    if (
        existing.status
        is PreparedApplicationAnswerSetReadStatus.INTEGRITY_FAILURE
    ):
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason
            .ANSWER_SET_INTEGRITY_FAILURE
        )
    content = {
        "answer_set_id": answer_set_id,
        "answers": [item.to_dict() for item in answers_tuple],
        "answer_policy_hash": answer_policy.policy_content_hash,
        "answer_policy_id": answer_policy.policy_id,
        "answer_policy_version": answer_policy.policy_version,
        "application_plan_id": plan.plan_id,
        "contract_version": (
            PREPARED_APPLICATION_ANSWER_SET_CONTRACT_VERSION
        ),
        "fact_snapshot_hash": snapshot.snapshot_content_hash,
        "fact_snapshot_id": snapshot.snapshot_id,
        "job_content_hash": plan.job_content_hash,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "plan_instructions_hash": (
            plan.user_preparation_instructions_hash
        ),
        "prepared_at": _rfc3339(now),
        "subject_id": subject,
        "taxonomy_hash": canonical_application_answer_taxonomy_hash(),
        "taxonomy_version": (
            CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
        ),
        "unresolved_items": [
            item.to_dict() for item in unresolved_tuple
        ],
    }
    try:
        answer_set = PreparedApplicationAnswerSet(
            answer_set_id=answer_set_id,
            contract_version=(
                PREPARED_APPLICATION_ANSWER_SET_CONTRACT_VERSION
            ),
            subject_id=subject,
            application_plan_id=plan.plan_id,
            job_id=plan.job_id,
            job_revision=plan.job_revision,
            job_content_hash=plan.job_content_hash,
            plan_instructions_hash=(
                plan.user_preparation_instructions_hash
            ),
            fact_snapshot_id=snapshot.snapshot_id,
            fact_snapshot_hash=snapshot.snapshot_content_hash,
            taxonomy_version=(
                CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
            ),
            taxonomy_hash=canonical_application_answer_taxonomy_hash(),
            answer_policy_id=answer_policy.policy_id,
            answer_policy_version=answer_policy.policy_version,
            answer_policy_hash=answer_policy.policy_content_hash,
            answers=answers_tuple,
            unresolved_items=unresolved_tuple,
            answer_set_content_hash=_canonical_hash(content),
            prepared_at=now,
        )
        write = answer_set_repository.save(answer_set)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _failure_result(
            PreparedApplicationAnswerSetFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )
    if write.status is PreparedApplicationAnswerSetWriteStatus.FAILED:
        return _failure_result(
            write.reason_code
            or PreparedApplicationAnswerSetFailureReason.PERSISTENCE_FAILED,
            retryable=write.retryable,
        )
    return PrepareApplicationAnswersResult(
        status=PreparedApplicationAnswerSetStatus(write.status.value),
        answer_set=write.answer_set,
        reason_code=None,
        retryable=False,
        message="Canonical application answers were prepared.",
    )


_APPLICATION_ANSWERS_FAILURE_REASON_MAP = {
    reason: ApplicationAnswersStopReason[reason.name]
    for reason in PreparedApplicationAnswerSetFailureReason
}


def _application_answers_user_stop_reason(
    unresolved_reasons: tuple[UnresolvedAnswerReason, ...],
) -> ApplicationAnswersStopReason:
    reasons = frozenset(unresolved_reasons)
    attestation = UnresolvedAnswerReason.REQUIRES_ATTESTATION in reasons
    choice = UnresolvedAnswerReason.REQUIRES_USER_CHOICE in reasons
    missing_fact = UnresolvedAnswerReason.MISSING_FACT in reasons
    if missing_fact and choice and attestation:
        return (
            ApplicationAnswersStopReason
            .USER_FACT_CHOICE_AND_ATTESTATION_REQUIRED
        )
    if missing_fact and choice:
        return ApplicationAnswersStopReason.USER_FACT_AND_CHOICE_REQUIRED
    if missing_fact and attestation:
        return (
            ApplicationAnswersStopReason.USER_FACT_AND_ATTESTATION_REQUIRED
        )
    if choice and attestation:
        return (
            ApplicationAnswersStopReason.USER_CHOICE_AND_ATTESTATION_REQUIRED
        )
    if attestation:
        return ApplicationAnswersStopReason.USER_ATTESTATION_REQUIRED
    if choice:
        return ApplicationAnswersStopReason.USER_CHOICE_REQUIRED
    if missing_fact:
        return ApplicationAnswersStopReason.USER_FACT_REQUIRED
    return ApplicationAnswersStopReason.NO_SAFE_AUTOMATABLE_ANSWER


def application_answers_public_result(
    result: PrepareApplicationAnswersResult,
) -> PublicPreparationStageResult:
    """Adapt every authoritative P2b3b outcome to stage-result v2."""

    if not isinstance(result, PrepareApplicationAnswersResult):
        raise TypeError("result must be an application-answers result")
    stage = ApplicationPreparationStage.APPLICATION_ANSWERS
    if result.status in {
        PreparedApplicationAnswerSetStatus.CREATED,
        PreparedApplicationAnswerSetStatus.UNCHANGED,
    }:
        if result.answer_set is None:
            raise ValueError("successful answer preparation has no AnswerSet")
        constructor = (
            PublicPreparationStageResult.completed
            if result.status is PreparedApplicationAnswerSetStatus.CREATED
            else PublicPreparationStageResult.unchanged
        )
        return constructor(
            stage=stage,
            result_id=result.answer_set.answer_set_id,
            result_content_hash=result.answer_set.answer_set_content_hash,
            outputs={
                "prepared_application_answer_set_id": (
                    result.answer_set.answer_set_id
                )
            },
            human_attention_required=any(
                item.blocking for item in result.answer_set.unresolved_items
            ),
        )
    if (
        result.status
        is PreparedApplicationAnswerSetStatus.DEFERRED_NO_TRUSTED_FACTS
    ):
        reason = ApplicationAnswersStopReason.NO_TRUSTED_FACTS
        outcome = PreparationStageOutcome.DEFERRED
    elif (
        result.status
        is PreparedApplicationAnswerSetStatus.DEFERRED_NEEDS_HUMAN
    ):
        reason = _application_answers_user_stop_reason(
            result.unresolved_reasons
        )
        outcome = PreparationStageOutcome.DEFERRED
    else:
        if result.reason_code is None:
            raise ValueError("stopped answer preparation has no reason")
        try:
            reason = _APPLICATION_ANSWERS_FAILURE_REASON_MAP[
                result.reason_code
            ]
        except KeyError as error:
            raise ValueError(
                "unmapped application-answers stop reason"
            ) from error
        outcome = PreparationStageOutcome.FAILED
    stop_reason = PreparationStopReasonEnvelope(
        stage=stage,
        code=reason,
        contract_version=APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION,
        outcome=outcome,
    )
    constructor = (
        PublicPreparationStageResult.deferred
        if outcome is PreparationStageOutcome.DEFERRED
        else PublicPreparationStageResult.failed
    )
    return constructor(
        stage=stage,
        stop_reason=stop_reason,
        retryable=result.retryable,
        human_attention_required=(
            outcome is PreparationStageOutcome.DEFERRED
        ),
    )


__all__ = [
    "APPLICATION_ANSWER_POLICY_VERSION",
    "SUPPORTED_APPLICATION_ANSWER_POLICY_VERSIONS",
    "APPLICATION_FACT_SNAPSHOT_CONTRACT_VERSION",
    "ApplicationAnswerPolicy",
    "ApplicationFact",
    "ApplicationFactProvider",
    "ApplicationFactSnapshot",
    "ApplicationFactSourceClassification",
    "ApplicationFactVerificationStatus",
    "DECLINE_TO_ANSWER",
    "PREPARED_APPLICATION_ANSWER_SET_CONTRACT_VERSION",
    "PrepareApplicationAnswersCommand",
    "PrepareApplicationAnswersResult",
    "PreparedAnswerSource",
    "PreparedApplicationAnswer",
    "PreparedApplicationAnswerSet",
    "PreparedApplicationAnswerSetFailureReason",
    "PreparedApplicationAnswerSetReadResult",
    "PreparedApplicationAnswerSetReadStatus",
    "PreparedApplicationAnswerSetRepository",
    "PreparedApplicationAnswerSetStatus",
    "PreparedApplicationAnswerSetWriteResult",
    "PreparedApplicationAnswerSetWriteStatus",
    "PrivateHomeApplicationFactProvider",
    "PrivateHomePreparedApplicationAnswerSetRepository",
    "UnresolvedAnswerReason",
    "UnresolvedApplicationAnswer",
    "UnresolvedDefaultHandling",
    "application_answers_public_result",
    "prepare_application_answers",
]
