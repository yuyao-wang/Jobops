"""Editable, versioned prioritization policy for the P1a business slice."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, runtime_checkable
from uuid import uuid4

from .private_home import PrivateHome


POLICY_REPOSITORY_SCHEMA_VERSION = 2
PREPARATION_ADMISSION_CONTRACT_VERSION = "preparation-admission-v1"
DEFAULT_DRAFT_TTL = timedelta(minutes=30)
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class HardConstraintType(str, Enum):
    ALLOWED_COUNTRY = "ALLOWED_COUNTRY"
    EXCLUDED_COUNTRY = "EXCLUDED_COUNTRY"
    EXCLUDED_COMPANY = "EXCLUDED_COMPANY"
    EXCLUDED_ROLE_PHRASE = "EXCLUDED_ROLE_PHRASE"
    EXCLUDED_STUDENT_ONLY_ROLE = "EXCLUDED_STUDENT_ONLY_ROLE"
    WORK_MODE_REQUIREMENT = "WORK_MODE_REQUIREMENT"


class SoftPreferenceCategory(str, Enum):
    ROLE = "ROLE"
    DOMAIN = "DOMAIN"
    LOCATION = "LOCATION"
    COMPANY = "COMPANY"
    FRESHNESS = "FRESHNESS"
    SENIORITY = "SENIORITY"
    WORK_MODE = "WORK_MODE"
    APPLICATION_EFFORT = "APPLICATION_EFFORT"
    ELIGIBILITY = "ELIGIBILITY"
    OTHER = "OTHER"


class PreferenceImportance(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PolicyDraftStatus(str, Enum):
    DRAFT = "DRAFT"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY_FOR_APPROVAL = "READY_FOR_APPROVAL"
    APPROVED = "APPROVED"
    EXPIRED = "EXPIRED"


class PrioritizationPolicyStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class PreparationPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class PolicyOperationStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    NEEDS_USER = "NEEDS_USER"
    FAILED = "FAILED"


class PolicyReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    POLICY_REVIEW_REQUIRED = "POLICY_REVIEW_REQUIRED"
    POLICY_NEEDS_CLARIFICATION = "POLICY_NEEDS_CLARIFICATION"
    INTERPRETER_FAILED = "INTERPRETER_FAILED"
    INTERPRETER_OUTPUT_INVALID = "INTERPRETER_OUTPUT_INVALID"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    DRAFT_EXPIRED = "DRAFT_EXPIRED"
    DRAFT_ALREADY_APPROVED = "DRAFT_ALREADY_APPROVED"
    HARD_CONSTRAINT_NOT_CONFIRMED = "HARD_CONSTRAINT_NOT_CONFIRMED"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _rfc3339(value: datetime) -> str:
    _require_aware("timestamp", value)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    _require_aware(name, parsed)
    return parsed.astimezone(timezone.utc)


def _clean_text(
    name: str,
    value: Any,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = " ".join(value.split())
    if (not cleaned and not allow_empty) or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the policy contract")
    return cleaned


def _clean_raw_text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("raw_preference_text must be a string")
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned or len(cleaned) > 20_000:
        raise ValueError("raw_preference_text is outside the policy contract")
    return cleaned


def _normalize_constraint_value(value: Any) -> str:
    cleaned = _clean_text("normalized_value", value, maximum=320)
    return " ".join(unicodedata.normalize("NFKC", cleaned).casefold().split())


def _validate_subject_id(value: Any) -> str:
    return _clean_text("subject_id", value, maximum=160)


def _validate_collection(
    name: str,
    value: Any,
    item_type: type,
    *,
    maximum: int = 100,
) -> tuple[Any, ...]:
    if not isinstance(value, tuple) or not all(
        isinstance(item, item_type) for item in value
    ):
        raise TypeError(f"{name} must be a tuple of {item_type.__name__}")
    if len(value) > maximum:
        raise ValueError(f"{name} contains too many items")
    return value


@dataclass(frozen=True, slots=True)
class HardConstraint:
    constraint_type: HardConstraintType
    normalized_value: str
    source_excerpt: str
    user_confirmed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "constraint_type",
            HardConstraintType(self.constraint_type),
        )
        object.__setattr__(
            self,
            "normalized_value",
            _normalize_constraint_value(self.normalized_value),
        )
        object.__setattr__(
            self,
            "source_excerpt",
            _clean_text("source_excerpt", self.source_excerpt, maximum=1000),
        )
        if type(self.user_confirmed) is not bool:
            raise TypeError("user_confirmed must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_type": self.constraint_type.value,
            "normalized_value": self.normalized_value,
            "source_excerpt": self.source_excerpt,
            "user_confirmed": self.user_confirmed,
        }


@dataclass(frozen=True, slots=True)
class SoftPreference:
    preference_id: str
    category: SoftPreferenceCategory
    statement: str
    source_excerpt: str
    importance: PreferenceImportance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "preference_id",
            _clean_text("preference_id", self.preference_id, maximum=160),
        )
        object.__setattr__(
            self,
            "category",
            SoftPreferenceCategory(self.category),
        )
        object.__setattr__(
            self,
            "statement",
            _clean_text("statement", self.statement, maximum=2000),
        )
        object.__setattr__(
            self,
            "source_excerpt",
            _clean_text("source_excerpt", self.source_excerpt, maximum=1000),
        )
        if self.importance is not None:
            object.__setattr__(
                self,
                "importance",
                PreferenceImportance(self.importance),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "preference_id": self.preference_id,
            "category": self.category.value,
            "statement": self.statement,
            "importance": self.importance.value if self.importance else None,
            "source_excerpt": self.source_excerpt,
        }


def _validated_hard_constraints(
    value: Any,
    *,
    require_confirmation: bool,
) -> tuple[HardConstraint, ...]:
    raw_constraints = _validate_collection(
        "hard_constraints",
        value,
        HardConstraint,
    )
    constraints = tuple(
        HardConstraint(
            constraint_type=item.constraint_type,
            normalized_value=item.normalized_value,
            source_excerpt=item.source_excerpt,
            user_confirmed=item.user_confirmed,
        )
        for item in raw_constraints
    )
    identities = [
        (item.constraint_type.value, item.normalized_value)
        for item in constraints
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("hard constraints must be unique")
    if require_confirmation and any(
        not item.user_confirmed for item in constraints
    ):
        raise PermissionError("approved hard constraints require user confirmation")
    return constraints


def _validated_soft_preferences(value: Any) -> tuple[SoftPreference, ...]:
    raw_preferences = _validate_collection(
        "soft_preferences",
        value,
        SoftPreference,
    )
    preferences = tuple(
        SoftPreference(
            preference_id=item.preference_id,
            category=item.category,
            statement=item.statement,
            source_excerpt=item.source_excerpt,
            importance=item.importance,
        )
        for item in raw_preferences
    )
    identifiers = [item.preference_id for item in preferences]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("soft preference IDs must be unique")
    return preferences


def _validated_ambiguities(value: Any) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError("ambiguities must be a tuple")
    if len(value) > 100:
        raise ValueError("ambiguities contains too many items")
    cleaned = tuple(
        _clean_text("ambiguity", item, maximum=2000) for item in value
    )
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("ambiguities must be unique")
    return cleaned


_PREPARATION_PRIORITY_ORDER = tuple(PreparationPriority)
_PREPARATION_PRIORITY_RANK = {
    priority: rank
    for rank, priority in enumerate(_PREPARATION_PRIORITY_ORDER)
}


def _validated_preparation_priorities(
    name: str,
    value: Any,
) -> tuple[PreparationPriority, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{name} must be a tuple")
    try:
        priorities = tuple(PreparationPriority(item) for item in value)
    except ValueError as exc:
        raise ValueError(
            f"{name} contains an unsupported priority outcome"
        ) from exc
    if len(priorities) != len(set(priorities)):
        raise ValueError(f"{name} must not contain duplicate priorities")
    return tuple(sorted(priorities, key=_PREPARATION_PRIORITY_RANK.__getitem__))


@dataclass(frozen=True, slots=True)
class PreparationAdmissionPolicy:
    preparation_eligible_priorities: tuple[PreparationPriority, ...]
    explicit_promotion_priorities: tuple[PreparationPriority, ...]
    contract_version: str = PREPARATION_ADMISSION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        eligible = _validated_preparation_priorities(
            "preparation_eligible_priorities",
            self.preparation_eligible_priorities,
        )
        promotion = _validated_preparation_priorities(
            "explicit_promotion_priorities",
            self.explicit_promotion_priorities,
        )
        if set(eligible).intersection(promotion):
            raise ValueError(
                "direct and explicit-promotion priorities must not overlap"
            )
        if self.contract_version != PREPARATION_ADMISSION_CONTRACT_VERSION:
            raise ValueError("preparation admission contract version is unsupported")
        object.__setattr__(self, "preparation_eligible_priorities", eligible)
        object.__setattr__(self, "explicit_promotion_priorities", promotion)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "preparation_eligible_priorities": [
                item.value for item in self.preparation_eligible_priorities
            ],
            "explicit_promotion_priorities": [
                item.value for item in self.explicit_promotion_priorities
            ],
        }


def default_preparation_admission_policy() -> PreparationAdmissionPolicy:
    return PreparationAdmissionPolicy(
        preparation_eligible_priorities=(
            PreparationPriority.P0,
            PreparationPriority.P1,
            PreparationPriority.P2,
        ),
        explicit_promotion_priorities=(PreparationPriority.P3,),
    )


@dataclass(frozen=True, slots=True)
class CreatePolicyDraftRequest:
    subject_id: str
    raw_preference_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, str):
            raise TypeError("subject_id must be a string")
        if not isinstance(self.raw_preference_text, str):
            raise TypeError("raw_preference_text must be a string")


@dataclass(frozen=True, slots=True)
class PolicyInterpretation:
    subject_id: str
    raw_preference_text: str
    hard_constraints: tuple[HardConstraint, ...]
    soft_preferences: tuple[SoftPreference, ...]
    ambiguities: tuple[str, ...]
    interpreter_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subject_id",
            _validate_subject_id(self.subject_id),
        )
        object.__setattr__(
            self,
            "raw_preference_text",
            _clean_raw_text(self.raw_preference_text),
        )
        _validated_hard_constraints(
            self.hard_constraints,
            require_confirmation=False,
        )
        _validated_soft_preferences(self.soft_preferences)
        object.__setattr__(
            self,
            "ambiguities",
            _validated_ambiguities(self.ambiguities),
        )
        object.__setattr__(
            self,
            "interpreter_version",
            _clean_text(
                "interpreter_version",
                self.interpreter_version,
                maximum=160,
            ),
        )


@runtime_checkable
class PrioritizationPolicyInterpreterPort(Protocol):
    async def interpret(
        self,
        request: CreatePolicyDraftRequest,
    ) -> PolicyInterpretation:
        """Interpret policy text once without persistence or tool authority."""


@dataclass(frozen=True, slots=True)
class PrioritizationPolicyDraft:
    draft_id: str
    subject_id: str
    raw_preference_text: str
    hard_constraints: tuple[HardConstraint, ...]
    soft_preferences: tuple[SoftPreference, ...]
    preparation_admission: PreparationAdmissionPolicy
    ambiguities: tuple[str, ...]
    status: PolicyDraftStatus
    created_at: datetime
    expires_at: datetime
    interpreter_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "draft_id",
            _clean_text("draft_id", self.draft_id, maximum=160),
        )
        object.__setattr__(
            self,
            "subject_id",
            _validate_subject_id(self.subject_id),
        )
        object.__setattr__(
            self,
            "raw_preference_text",
            _clean_raw_text(self.raw_preference_text),
        )
        _validated_hard_constraints(
            self.hard_constraints,
            require_confirmation=False,
        )
        _validated_soft_preferences(self.soft_preferences)
        object.__setattr__(
            self,
            "ambiguities",
            _validated_ambiguities(self.ambiguities),
        )
        object.__setattr__(self, "status", PolicyDraftStatus(self.status))
        _require_aware("created_at", self.created_at)
        _require_aware("expires_at", self.expires_at)
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        object.__setattr__(
            self,
            "interpreter_version",
            _clean_text(
                "interpreter_version",
                self.interpreter_version,
                maximum=160,
            ),
        )
        if not isinstance(
            self.preparation_admission,
            PreparationAdmissionPolicy,
        ):
            raise TypeError(
                "preparation_admission must be a PreparationAdmissionPolicy"
            )
        expected = (
            PolicyDraftStatus.NEEDS_CLARIFICATION
            if self.ambiguities
            else PolicyDraftStatus.READY_FOR_APPROVAL
        )
        if self.status in {
            PolicyDraftStatus.NEEDS_CLARIFICATION,
            PolicyDraftStatus.READY_FOR_APPROVAL,
        } and self.status is not expected:
            raise ValueError("draft ambiguity and status conflict")


@dataclass(frozen=True, slots=True)
class ApprovePolicyRequest:
    draft_id: str
    subject_id: str
    reviewed_raw_preference_text: str
    reviewed_hard_constraints: tuple[HardConstraint, ...]
    reviewed_soft_preferences: tuple[SoftPreference, ...]
    reviewed_preparation_admission: PreparationAdmissionPolicy

    def __post_init__(self) -> None:
        if not isinstance(self.draft_id, str):
            raise TypeError("draft_id must be a string")
        if not isinstance(self.subject_id, str):
            raise TypeError("subject_id must be a string")
        if not isinstance(self.reviewed_raw_preference_text, str):
            raise TypeError("reviewed_raw_preference_text must be a string")
        if not isinstance(self.reviewed_hard_constraints, tuple):
            raise TypeError("reviewed_hard_constraints must be a tuple")
        if not isinstance(self.reviewed_soft_preferences, tuple):
            raise TypeError("reviewed_soft_preferences must be a tuple")
        if not isinstance(
            self.reviewed_preparation_admission,
            PreparationAdmissionPolicy,
        ):
            raise TypeError(
                "reviewed_preparation_admission must be a "
                "PreparationAdmissionPolicy"
            )


@dataclass(frozen=True, slots=True)
class PrioritizationPolicy:
    policy_id: str
    subject_id: str
    policy_version: int
    policy_content_hash: str
    raw_preference_text: str
    hard_constraints: tuple[HardConstraint, ...]
    soft_preferences: tuple[SoftPreference, ...]
    preparation_admission: PreparationAdmissionPolicy
    status: PrioritizationPolicyStatus
    created_at: datetime
    approved_at: datetime
    interpreter_version: str
    previous_policy_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _clean_text("policy_id", self.policy_id, maximum=160),
        )
        object.__setattr__(
            self,
            "subject_id",
            _validate_subject_id(self.subject_id),
        )
        if (
            isinstance(self.policy_version, bool)
            or not isinstance(self.policy_version, int)
            or self.policy_version < 1
        ):
            raise ValueError("policy_version must be a positive integer")
        if (
            not isinstance(self.policy_content_hash, str)
            or _HASH_PATTERN.fullmatch(self.policy_content_hash) is None
        ):
            raise ValueError("policy_content_hash must be a SHA-256 digest")
        object.__setattr__(
            self,
            "raw_preference_text",
            _clean_raw_text(self.raw_preference_text),
        )
        _validated_hard_constraints(
            self.hard_constraints,
            require_confirmation=True,
        )
        _validated_soft_preferences(self.soft_preferences)
        object.__setattr__(
            self,
            "status",
            PrioritizationPolicyStatus(self.status),
        )
        _require_aware("created_at", self.created_at)
        _require_aware("approved_at", self.approved_at)
        if self.approved_at < self.created_at:
            raise ValueError("approved_at cannot be before created_at")
        object.__setattr__(
            self,
            "interpreter_version",
            _clean_text(
                "interpreter_version",
                self.interpreter_version,
                maximum=160,
            ),
        )
        if self.previous_policy_id is not None:
            object.__setattr__(
                self,
                "previous_policy_id",
                _clean_text(
                    "previous_policy_id",
                    self.previous_policy_id,
                    maximum=160,
                ),
            )
        if not isinstance(
            self.preparation_admission,
            PreparationAdmissionPolicy,
        ):
            raise TypeError(
                "preparation_admission must be a PreparationAdmissionPolicy"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "subject_id": self.subject_id,
            "policy_version": self.policy_version,
            "policy_content_hash": self.policy_content_hash,
            "raw_preference_text": self.raw_preference_text,
            "hard_constraints": [
                item.to_dict() for item in self.hard_constraints
            ],
            "soft_preferences": [
                item.to_dict() for item in self.soft_preferences
            ],
            "status": self.status.value,
            "created_at": _rfc3339(self.created_at),
            "approved_at": _rfc3339(self.approved_at),
            "interpreter_version": self.interpreter_version,
            "previous_policy_id": self.previous_policy_id,
            "preparation_admission": self.preparation_admission.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CreatePolicyDraftResult:
    status: PolicyOperationStatus
    reason_code: PolicyReason
    retryable: bool
    draft: PrioritizationPolicyDraft | None
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", PolicyOperationStatus(self.status))
        object.__setattr__(self, "reason_code", PolicyReason(self.reason_code))
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if self.status is PolicyOperationStatus.NEEDS_USER:
            if self.draft is None or self.retryable:
                raise ValueError("NEEDS_USER draft result is inconsistent")
        elif self.draft is not None:
            raise ValueError("failed draft result cannot contain a draft")


@dataclass(frozen=True, slots=True)
class PrioritizationPolicyResult:
    status: PolicyOperationStatus
    reason_code: PolicyReason | None
    retryable: bool
    policy: PrioritizationPolicy | None
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", PolicyOperationStatus(self.status))
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                PolicyReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if self.status is PolicyOperationStatus.SUCCEEDED:
            if self.policy is None or self.reason_code is not None or self.retryable:
                raise ValueError("successful policy result is inconsistent")
        elif self.policy is not None or self.reason_code is None:
            raise ValueError("unsuccessful policy result is inconsistent")


class InMemoryPrioritizationPolicyDraftStore:
    """Process-local, TTL-bound draft state. Approved policy never lives here."""

    def __init__(self) -> None:
        self._drafts: dict[str, PrioritizationPolicyDraft] = {}
        self._lock = RLock()

    def put(self, draft: PrioritizationPolicyDraft) -> None:
        if not isinstance(draft, PrioritizationPolicyDraft):
            raise TypeError("draft must be a PrioritizationPolicyDraft")
        with self._lock:
            if draft.draft_id in self._drafts:
                raise ValueError("draft_id already exists")
            self._drafts[draft.draft_id] = draft

    def get(self, draft_id: str) -> PrioritizationPolicyDraft | None:
        with self._lock:
            return self._drafts.get(str(draft_id))

    def mark_expired(self, draft_id: str) -> PrioritizationPolicyDraft:
        with self._lock:
            draft = self._drafts[draft_id]
            if draft.status is not PolicyDraftStatus.EXPIRED:
                draft = replace(draft, status=PolicyDraftStatus.EXPIRED)
                self._drafts[draft_id] = draft
            return draft

    def mark_approved(self, draft_id: str) -> PrioritizationPolicyDraft:
        with self._lock:
            draft = self._drafts[draft_id]
            if draft.status is PolicyDraftStatus.APPROVED:
                return draft
            if draft.status is not PolicyDraftStatus.READY_FOR_APPROVAL:
                raise ValueError("only a ready draft can be approved")
            draft = replace(draft, status=PolicyDraftStatus.APPROVED)
            self._drafts[draft_id] = draft
            return draft


def _canonical_policy_content(
    *,
    raw_preference_text: str,
    hard_constraints: tuple[HardConstraint, ...],
    soft_preferences: tuple[SoftPreference, ...],
    preparation_admission: PreparationAdmissionPolicy,
) -> dict[str, Any]:
    hard = sorted(
        (item.to_dict() for item in hard_constraints),
        key=lambda item: (
            item["constraint_type"],
            item["normalized_value"],
            item["source_excerpt"],
        ),
    )
    soft = sorted(
        (item.to_dict() for item in soft_preferences),
        key=lambda item: (
            item["preference_id"],
            item["category"],
            item["statement"],
            item["importance"] or "",
            item["source_excerpt"],
        ),
    )
    return {
        "raw_preference_text": _clean_raw_text(raw_preference_text),
        "hard_constraints": hard,
        "soft_preferences": soft,
        "preparation_admission": preparation_admission.to_dict(),
    }


def policy_content_hash(
    *,
    raw_preference_text: str,
    hard_constraints: tuple[HardConstraint, ...],
    soft_preferences: tuple[SoftPreference, ...],
    preparation_admission: PreparationAdmissionPolicy,
) -> str:
    """Hash only approved business content, excluding IDs, time and interpreter."""

    if not isinstance(preparation_admission, PreparationAdmissionPolicy):
        raise TypeError(
            "preparation_admission must be a PreparationAdmissionPolicy"
        )
    payload = json.dumps(
        _canonical_policy_content(
            raw_preference_text=raw_preference_text,
            hard_constraints=hard_constraints,
            soft_preferences=soft_preferences,
            preparation_admission=preparation_admission,
        ),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


def _policy_from_dict(value: Any) -> PrioritizationPolicy:
    if not isinstance(value, Mapping):
        raise ValueError("persisted policy must be an object")
    hard_raw = value.get("hard_constraints")
    soft_raw = value.get("soft_preferences")
    admission_raw = value.get("preparation_admission")
    if not isinstance(hard_raw, list) or not isinstance(soft_raw, list):
        raise ValueError("persisted policy collections are invalid")
    if not isinstance(admission_raw, Mapping):
        raise PrioritizationPolicyCompatibilityError(
            "approved policy lacks preparation admission; migration is required"
        )
    required_admission_keys = {
        "contract_version",
        "preparation_eligible_priorities",
        "explicit_promotion_priorities",
    }
    if (
        set(admission_raw) != required_admission_keys
        or not isinstance(
            admission_raw.get("preparation_eligible_priorities"),
            list,
        )
        or not isinstance(
            admission_raw.get("explicit_promotion_priorities"),
            list,
        )
    ):
        raise ValueError("persisted preparation admission is invalid")
    hard_constraints = tuple(
        HardConstraint(
            constraint_type=item["constraint_type"],
            normalized_value=item["normalized_value"],
            source_excerpt=item["source_excerpt"],
            user_confirmed=item["user_confirmed"],
        )
        for item in hard_raw
        if isinstance(item, Mapping)
    )
    soft_preferences = tuple(
        SoftPreference(
            preference_id=item["preference_id"],
            category=item["category"],
            statement=item["statement"],
            importance=item.get("importance"),
            source_excerpt=item["source_excerpt"],
        )
        for item in soft_raw
        if isinstance(item, Mapping)
    )
    if len(hard_constraints) != len(hard_raw) or len(soft_preferences) != len(
        soft_raw
    ):
        raise ValueError("persisted policy collection item is invalid")
    preparation_admission = PreparationAdmissionPolicy(
        preparation_eligible_priorities=tuple(
            admission_raw["preparation_eligible_priorities"]
        ),
        explicit_promotion_priorities=tuple(
            admission_raw["explicit_promotion_priorities"]
        ),
        contract_version=admission_raw["contract_version"],
    )
    policy = PrioritizationPolicy(
        policy_id=value.get("policy_id"),
        subject_id=value.get("subject_id"),
        policy_version=value.get("policy_version"),
        policy_content_hash=value.get("policy_content_hash"),
        raw_preference_text=value.get("raw_preference_text"),
        hard_constraints=hard_constraints,
        soft_preferences=soft_preferences,
        status=value.get("status"),
        created_at=_parse_timestamp(value.get("created_at"), "created_at"),
        approved_at=_parse_timestamp(value.get("approved_at"), "approved_at"),
        interpreter_version=value.get("interpreter_version"),
        previous_policy_id=value.get("previous_policy_id"),
        preparation_admission=preparation_admission,
    )
    expected_hash = policy_content_hash(
        raw_preference_text=policy.raw_preference_text,
        hard_constraints=policy.hard_constraints,
        soft_preferences=policy.soft_preferences,
        preparation_admission=policy.preparation_admission,
    )
    if policy.policy_content_hash != expected_hash:
        raise ValueError("persisted policy content hash is invalid")
    return policy


class PrioritizationPolicyCompatibilityError(RuntimeError):
    """Stored approved policy needs explicit migration for this contract."""


class PrivateHomePrioritizationPolicyRepository:
    """Minimal JSON repository for approved policy history and active policy."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    @staticmethod
    def _subject_digest(subject_id: str) -> str:
        return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()

    def _path(self, subject_id: str) -> Path:
        clean_subject = _validate_subject_id(subject_id)
        return (
            self._home.paths.prioritization_policies
            / f"{self._subject_digest(clean_subject)}.json"
        )

    def _load(self, subject_id: str) -> tuple[PrioritizationPolicy, ...]:
        clean_subject = _validate_subject_id(subject_id)
        path = self._path(clean_subject)
        if not path.is_file():
            return ()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("approved prioritization policy is unreadable") from exc
        if (
            not isinstance(document, Mapping)
            or document.get("subject_id") != clean_subject
            or not isinstance(document.get("policies"), list)
        ):
            raise RuntimeError("approved prioritization policy index is invalid")
        if document.get("schema_version") != POLICY_REPOSITORY_SCHEMA_VERSION:
            raise PrioritizationPolicyCompatibilityError(
                "approved policy repository requires explicit migration"
            )
        try:
            policies = tuple(
                _policy_from_dict(item) for item in document["policies"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("approved prioritization policy is invalid") from exc
        if any(item.subject_id != clean_subject for item in policies):
            raise RuntimeError("approved prioritization policy subject is invalid")
        versions = [item.policy_version for item in policies]
        identifiers = [item.policy_id for item in policies]
        active = [
            item
            for item in policies
            if item.status is PrioritizationPolicyStatus.ACTIVE
        ]
        if (
            len(versions) != len(set(versions))
            or len(identifiers) != len(set(identifiers))
            or len(active) > 1
            or document.get("active_policy_id")
            != (active[0].policy_id if active else None)
        ):
            raise RuntimeError("approved prioritization policy history is invalid")
        return policies

    def _write(
        self,
        subject_id: str,
        policies: tuple[PrioritizationPolicy, ...],
    ) -> None:
        active = [
            item
            for item in policies
            if item.status is PrioritizationPolicyStatus.ACTIVE
        ]
        if len(active) != 1:
            raise ValueError("policy history must contain exactly one active policy")
        document = {
            "schema_version": POLICY_REPOSITORY_SCHEMA_VERSION,
            "subject_id": subject_id,
            "active_policy_id": active[0].policy_id,
            "policies": [item.to_dict() for item in policies],
        }
        self._home.ensure()
        self._home.write_bytes(self._path(subject_id), _json_bytes(document))

    def approve(
        self,
        *,
        draft: PrioritizationPolicyDraft,
        raw_preference_text: str,
        hard_constraints: tuple[HardConstraint, ...],
        soft_preferences: tuple[SoftPreference, ...],
        preparation_admission: PreparationAdmissionPolicy,
        approved_at: datetime,
    ) -> PrioritizationPolicy:
        _require_aware("approved_at", approved_at)
        clean_subject = _validate_subject_id(draft.subject_id)
        clean_raw = _clean_raw_text(raw_preference_text)
        hard = _validated_hard_constraints(
            hard_constraints,
            require_confirmation=True,
        )
        soft = _validated_soft_preferences(soft_preferences)
        if not isinstance(
            preparation_admission,
            PreparationAdmissionPolicy,
        ):
            raise TypeError(
                "preparation_admission must be a PreparationAdmissionPolicy"
            )
        content_hash = policy_content_hash(
            raw_preference_text=clean_raw,
            hard_constraints=hard,
            soft_preferences=soft,
            preparation_admission=preparation_admission,
        )
        with self._lock:
            history = self._load(clean_subject)
            active = next(
                (
                    item
                    for item in history
                    if item.status is PrioritizationPolicyStatus.ACTIVE
                ),
                None,
            )
            if active is not None and active.policy_content_hash == content_hash:
                return active

            version = max(
                (item.policy_version for item in history),
                default=0,
            ) + 1
            subject_digest = self._subject_digest(clean_subject)[:16]
            policy = PrioritizationPolicy(
                policy_id=(
                    f"prioritization-policy-{subject_digest}-v{version:06d}"
                ),
                subject_id=clean_subject,
                policy_version=version,
                policy_content_hash=content_hash,
                raw_preference_text=clean_raw,
                hard_constraints=hard,
                soft_preferences=soft,
                status=PrioritizationPolicyStatus.ACTIVE,
                created_at=draft.created_at,
                approved_at=approved_at,
                interpreter_version=draft.interpreter_version,
                previous_policy_id=active.policy_id if active else None,
                preparation_admission=preparation_admission,
            )
            superseded = tuple(
                replace(item, status=PrioritizationPolicyStatus.SUPERSEDED)
                if item.status is PrioritizationPolicyStatus.ACTIVE
                else item
                for item in history
            )
            self._write(clean_subject, superseded + (policy,))
            return policy

    def get_active_policy(
        self,
        subject_id: str,
    ) -> PrioritizationPolicy | None:
        with self._lock:
            return next(
                (
                    item
                    for item in self._load(subject_id)
                    if item.status is PrioritizationPolicyStatus.ACTIVE
                ),
                None,
            )

    def get_policy(
        self,
        subject_id: str,
        policy_id: str,
    ) -> PrioritizationPolicy | None:
        clean_id = _clean_text("policy_id", policy_id, maximum=160)
        with self._lock:
            return next(
                (
                    item
                    for item in self._load(subject_id)
                    if item.policy_id == clean_id
                ),
                None,
            )

    def list_policies(
        self,
        subject_id: str,
    ) -> tuple[PrioritizationPolicy, ...]:
        with self._lock:
            return self._load(subject_id)


class PrioritizationPolicyService:
    """Application service for draft interpretation and reviewed approval."""

    def __init__(
        self,
        *,
        interpreter: PrioritizationPolicyInterpreterPort,
        draft_store: InMemoryPrioritizationPolicyDraftStore,
        repository: PrivateHomePrioritizationPolicyRepository,
        draft_ttl: timedelta = DEFAULT_DRAFT_TTL,
        clock: Callable[[], datetime] | None = None,
        draft_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(interpreter, PrioritizationPolicyInterpreterPort):
            raise TypeError(
                "interpreter must implement PrioritizationPolicyInterpreterPort"
            )
        if not isinstance(
            draft_store,
            InMemoryPrioritizationPolicyDraftStore,
        ):
            raise TypeError(
                "draft_store must be an InMemoryPrioritizationPolicyDraftStore"
            )
        if not isinstance(
            repository,
            PrivateHomePrioritizationPolicyRepository,
        ):
            raise TypeError(
                "repository must be a PrivateHomePrioritizationPolicyRepository"
            )
        if not isinstance(draft_ttl, timedelta) or draft_ttl <= timedelta(0):
            raise ValueError("draft_ttl must be positive")
        self._interpreter = interpreter
        self._draft_store = draft_store
        self._repository = repository
        self._draft_ttl = draft_ttl
        self._clock = clock or _utc_now
        self._draft_id_factory = draft_id_factory or (
            lambda: f"prioritization-policy-draft-{uuid4().hex}"
        )
        self._approval_lock = RLock()

    async def create_policy_draft(
        self,
        request: CreatePolicyDraftRequest,
    ) -> CreatePolicyDraftResult:
        if not isinstance(request, CreatePolicyDraftRequest):
            raise TypeError("request must be a CreatePolicyDraftRequest")
        try:
            subject_id = _validate_subject_id(request.subject_id)
            raw_text = _clean_raw_text(request.raw_preference_text)
        except (AttributeError, TypeError, ValueError):
            return CreatePolicyDraftResult(
                status=PolicyOperationStatus.FAILED,
                reason_code=PolicyReason.INVALID_REQUEST,
                retryable=False,
                draft=None,
                message="The policy request is invalid.",
            )

        try:
            interpretation = await self._interpreter.interpret(request)
        except Exception:
            return CreatePolicyDraftResult(
                status=PolicyOperationStatus.FAILED,
                reason_code=PolicyReason.INTERPRETER_FAILED,
                retryable=False,
                draft=None,
                message="The policy could not be interpreted.",
            )

        try:
            if not isinstance(interpretation, PolicyInterpretation):
                raise TypeError("interpreter must return PolicyInterpretation")
            if interpretation.subject_id != subject_id:
                raise ValueError("interpreter changed the subject")
            if interpretation.raw_preference_text != raw_text:
                raise ValueError("interpreter changed the raw policy text")
            if any(
                item.user_confirmed
                for item in interpretation.hard_constraints
            ):
                raise ValueError("interpreter cannot confirm hard constraints")
            now = self._clock()
            _require_aware("clock result", now)
            draft_id = _clean_text(
                "draft_id",
                self._draft_id_factory(),
                maximum=160,
            )
            status = (
                PolicyDraftStatus.NEEDS_CLARIFICATION
                if interpretation.ambiguities
                else PolicyDraftStatus.READY_FOR_APPROVAL
            )
            draft = PrioritizationPolicyDraft(
                draft_id=draft_id,
                subject_id=subject_id,
                raw_preference_text=raw_text,
                hard_constraints=interpretation.hard_constraints,
                soft_preferences=interpretation.soft_preferences,
                preparation_admission=default_preparation_admission_policy(),
                ambiguities=interpretation.ambiguities,
                status=status,
                created_at=now,
                expires_at=now + self._draft_ttl,
                interpreter_version=interpretation.interpreter_version,
            )
            self._draft_store.put(draft)
        except (TypeError, ValueError):
            return CreatePolicyDraftResult(
                status=PolicyOperationStatus.FAILED,
                reason_code=PolicyReason.INTERPRETER_OUTPUT_INVALID,
                retryable=False,
                draft=None,
                message="The interpreted policy failed contract validation.",
            )

        reason = (
            PolicyReason.POLICY_NEEDS_CLARIFICATION
            if draft.ambiguities
            else PolicyReason.POLICY_REVIEW_REQUIRED
        )
        message = (
            "Review the blocking ambiguities before creating a new draft."
            if draft.ambiguities
            else "Review and confirm the interpreted policy before approval."
        )
        return CreatePolicyDraftResult(
            status=PolicyOperationStatus.NEEDS_USER,
            reason_code=reason,
            retryable=False,
            draft=draft,
            message=message,
        )

    def approve_policy(
        self,
        request: ApprovePolicyRequest,
    ) -> PrioritizationPolicyResult:
        if not isinstance(request, ApprovePolicyRequest):
            raise TypeError("request must be an ApprovePolicyRequest")
        try:
            draft_id = _clean_text(
                "draft_id",
                request.draft_id,
                maximum=160,
            )
            subject_id = _validate_subject_id(request.subject_id)
        except (TypeError, ValueError):
            return self._policy_failure(
                PolicyReason.INVALID_REQUEST,
                "The approval request is invalid.",
            )

        with self._approval_lock:
            draft = self._draft_store.get(draft_id)
            if draft is None:
                return self._policy_failure(
                    PolicyReason.DRAFT_NOT_FOUND,
                    "The policy draft was not found.",
                )
            if draft.subject_id != subject_id:
                return self._policy_failure(
                    PolicyReason.SUBJECT_MISMATCH,
                    "The policy draft belongs to another subject.",
                )
            now = self._clock()
            _require_aware("clock result", now)
            if now >= draft.expires_at:
                self._draft_store.mark_expired(draft_id)
                return self._policy_failure(
                    PolicyReason.DRAFT_EXPIRED,
                    "The policy draft has expired.",
                )
            if draft.status is PolicyDraftStatus.APPROVED:
                return self._policy_failure(
                    PolicyReason.DRAFT_ALREADY_APPROVED,
                    "The policy draft has already been approved.",
                )
            if (
                draft.status is PolicyDraftStatus.NEEDS_CLARIFICATION
                or draft.ambiguities
            ):
                return self._policy_failure(
                    PolicyReason.POLICY_NEEDS_CLARIFICATION,
                    "Resolve the blocking policy ambiguities first.",
                    status=PolicyOperationStatus.NEEDS_USER,
                )
            if draft.status is not PolicyDraftStatus.READY_FOR_APPROVAL:
                return self._policy_failure(
                    PolicyReason.INVALID_REQUEST,
                    "The policy draft is not ready for approval.",
                )

            try:
                reviewed_raw = _clean_raw_text(
                    request.reviewed_raw_preference_text
                )
                reviewed_hard = _validated_hard_constraints(
                    request.reviewed_hard_constraints,
                    require_confirmation=True,
                )
                reviewed_soft = _validated_soft_preferences(
                    request.reviewed_soft_preferences
                )
                reviewed_admission = request.reviewed_preparation_admission
                if not isinstance(
                    reviewed_admission,
                    PreparationAdmissionPolicy,
                ):
                    raise TypeError(
                        "reviewed preparation admission is invalid"
                    )
            except PermissionError:
                return self._policy_failure(
                    PolicyReason.HARD_CONSTRAINT_NOT_CONFIRMED,
                    "Every approved hard constraint requires user confirmation.",
                    status=PolicyOperationStatus.NEEDS_USER,
                )
            except (AttributeError, TypeError, ValueError):
                return self._policy_failure(
                    PolicyReason.INVALID_REQUEST,
                    "The reviewed policy content is invalid.",
                )

            policy = self._repository.approve(
                draft=draft,
                raw_preference_text=reviewed_raw,
                hard_constraints=reviewed_hard,
                soft_preferences=reviewed_soft,
                preparation_admission=reviewed_admission,
                approved_at=now,
            )
            self._draft_store.mark_approved(draft_id)
            return PrioritizationPolicyResult(
                status=PolicyOperationStatus.SUCCEEDED,
                reason_code=None,
                retryable=False,
                policy=policy,
                message=(
                    f"Prioritization policy version "
                    f"{policy.policy_version} is active."
                ),
            )

    def get_active_policy(
        self,
        subject_id: str,
    ) -> PrioritizationPolicy | None:
        return self._repository.get_active_policy(
            _validate_subject_id(subject_id)
        )

    @staticmethod
    def _policy_failure(
        reason: PolicyReason,
        message: str,
        *,
        status: PolicyOperationStatus = PolicyOperationStatus.FAILED,
    ) -> PrioritizationPolicyResult:
        return PrioritizationPolicyResult(
            status=status,
            reason_code=reason,
            retryable=False,
            policy=None,
            message=message,
        )


__all__ = [
    "ApprovePolicyRequest",
    "CreatePolicyDraftRequest",
    "CreatePolicyDraftResult",
    "DEFAULT_DRAFT_TTL",
    "HardConstraint",
    "HardConstraintType",
    "InMemoryPrioritizationPolicyDraftStore",
    "PolicyDraftStatus",
    "PolicyInterpretation",
    "PolicyOperationStatus",
    "PolicyReason",
    "PREPARATION_ADMISSION_CONTRACT_VERSION",
    "PreparationAdmissionPolicy",
    "PreparationPriority",
    "PreferenceImportance",
    "PrioritizationPolicy",
    "PrioritizationPolicyDraft",
    "PrioritizationPolicyInterpreterPort",
    "PrioritizationPolicyResult",
    "PrioritizationPolicyService",
    "PrioritizationPolicyStatus",
    "PrioritizationPolicyCompatibilityError",
    "PrivateHomePrioritizationPolicyRepository",
    "SoftPreference",
    "SoftPreferenceCategory",
    "default_preparation_admission_policy",
    "policy_content_hash",
]
