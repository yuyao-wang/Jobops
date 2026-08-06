"""Explicit SearchProfile policy for future application-request intent."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from .job_discovery import (
    DiscoveryDisposition,
    JobDiscoveryResponse,
    JobIntakeIntent,
)
from .private_home import PrivateHome, PrivateHomeError
from .search_profile import (
    SearchProfile,
    SearchProfileListResult,
    SearchProfileListStatus,
    SearchProfileReadResult,
    SearchProfileReadStatus,
    SearchProfileRepository,
    SearchProfileSourceReference,
)


SEARCH_PROFILE_INTENT_POLICY_CONTRACT_VERSION = (
    "search-profile-intent-policy-v1"
)
ENABLE_AUTO_REQUEST_APPLICATION_BATCH_CONTRACT_VERSION = (
    "enable-auto-request-application-batch-v1"
)
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _clean(name: str, value: Any, maximum: int = 320) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the intent policy contract")
    return cleaned


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("persisted policy timestamp must be a string")
    return _aware(
        "persisted policy timestamp",
        datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _subject_key(subject_id: str) -> str:
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


class SearchProfileIntentMode(StrEnum):
    ADD_JOB_ONLY = "ADD_JOB_ONLY"
    AUTO_REQUEST_APPLICATION = "AUTO_REQUEST_APPLICATION"


class SaveSearchProfileIntentPolicyStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class EnableAutoRequestApplicationBatchStatus(StrEnum):
    NOOP = "NOOP"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class EnableAutoRequestApplicationBatchFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PROFILE_SNAPSHOT_FAILED = "PROFILE_SNAPSHOT_FAILED"
    PROFILE_SNAPSHOT_INTEGRITY_FAILURE = (
        "PROFILE_SNAPSHOT_INTEGRITY_FAILURE"
    )
    POLICY_UPDATE_FAILED = "POLICY_UPDATE_FAILED"
    POLICY_INTEGRITY_FAILURE = "POLICY_INTEGRITY_FAILURE"


class SaveSearchProfileIntentPolicyReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    PROFILE_SOURCE_CHANGED = "PROFILE_SOURCE_CHANGED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


class SearchProfileIntentPolicyReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class SearchProfileIntentPolicyWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class SearchProfileIntentDecisionStatus(StrEnum):
    DECIDED = "DECIDED"
    FAILED = "FAILED"


class SearchProfileIntentDecisionReason(StrEnum):
    DEFAULT_ADD_JOB_ONLY = "DEFAULT_ADD_JOB_ONLY"
    EXPLICIT_ADD_JOB_ONLY = "EXPLICIT_ADD_JOB_ONLY"
    EXPLICIT_AUTO_REQUEST_APPLICATION = "EXPLICIT_AUTO_REQUEST_APPLICATION"
    PROFILE_NOT_ENABLED = "PROFILE_NOT_ENABLED"
    DISCOVERY_NOT_ACCEPTED = "DISCOVERY_NOT_ACCEPTED"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    POLICY_INTEGRITY_FAILURE = "POLICY_INTEGRITY_FAILURE"


def _content_payload(
    *,
    subject_id: str,
    profile_id: str,
    source: SearchProfileSourceReference,
    intent_mode: SearchProfileIntentMode,
    enabled: bool,
    policy_note: str | None,
) -> dict[str, Any]:
    return {
        "contract_version": SEARCH_PROFILE_INTENT_POLICY_CONTRACT_VERSION,
        "enabled": enabled,
        "intent_mode": intent_mode.value,
        "policy_note": policy_note,
        "profile_id": profile_id,
        "source": source.to_dict(),
        "subject_id": subject_id,
    }


@dataclass(frozen=True, slots=True)
class SearchProfileIntentPolicy:
    subject_id: str
    profile_id: str
    search_profile_version: int
    source: SearchProfileSourceReference
    intent_mode: SearchProfileIntentMode
    enabled: bool
    policy_note: str | None
    policy_version: int
    canonical_hash: str
    contract_version: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        subject_id = _clean("subject_id", self.subject_id, 160)
        profile_id = _clean("profile_id", self.profile_id, 160)
        if subject_id != self.subject_id or profile_id != self.profile_id:
            raise ValueError("policy identity is not canonical")
        if (
            type(self.search_profile_version) is not int
            or self.search_profile_version < 1
            or type(self.policy_version) is not int
            or self.policy_version < 1
        ):
            raise ValueError("policy versions must be positive")
        if not isinstance(self.source, SearchProfileSourceReference):
            raise TypeError("policy source must be typed")
        object.__setattr__(
            self, "intent_mode", SearchProfileIntentMode(self.intent_mode)
        )
        if type(self.enabled) is not bool:
            raise TypeError("policy enabled must be boolean")
        note = (
            _clean("policy_note", self.policy_note, 1000)
            if self.policy_note is not None
            else None
        )
        if note != self.policy_note:
            raise ValueError("policy note is not canonical")
        if self.contract_version != SEARCH_PROFILE_INTENT_POLICY_CONTRACT_VERSION:
            raise ValueError("intent policy contract version is unsupported")
        _aware("created_at", self.created_at)
        _aware("updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        expected = _hash(
            _content_payload(
                subject_id=subject_id,
                profile_id=profile_id,
                source=self.source,
                intent_mode=self.intent_mode,
                enabled=self.enabled,
                policy_note=note,
            )
        )
        if _HASH_RE.fullmatch(self.canonical_hash) is None or (
            self.canonical_hash != expected
        ):
            raise ValueError("intent policy canonical hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        profile: SearchProfile,
        intent_mode: SearchProfileIntentMode,
        enabled: bool,
        policy_note: str | None,
        policy_version: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> "SearchProfileIntentPolicy":
        if not isinstance(profile, SearchProfile):
            raise TypeError("profile must be typed")
        note = (
            _clean("policy_note", policy_note, 1000)
            if policy_note is not None
            else None
        )
        mode = SearchProfileIntentMode(intent_mode)
        payload = _content_payload(
            subject_id=profile.subject_id,
            profile_id=profile.profile_id,
            source=profile.source,
            intent_mode=mode,
            enabled=enabled,
            policy_note=note,
        )
        return cls(
            subject_id=profile.subject_id,
            profile_id=profile.profile_id,
            search_profile_version=profile.profile_version,
            source=profile.source,
            intent_mode=mode,
            enabled=enabled,
            policy_note=note,
            policy_version=policy_version,
            canonical_hash=_hash(payload),
            contract_version=SEARCH_PROFILE_INTENT_POLICY_CONTRACT_VERSION,
            created_at=created_at,
            updated_at=updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **_content_payload(
                subject_id=self.subject_id,
                profile_id=self.profile_id,
                source=self.source,
                intent_mode=self.intent_mode,
                enabled=self.enabled,
                policy_note=self.policy_note,
            ),
            "canonical_hash": self.canonical_hash,
            "created_at": _time(self.created_at),
            "policy_version": self.policy_version,
            "search_profile_version": self.search_profile_version,
            "updated_at": _time(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class SaveSearchProfileIntentPolicyCommand:
    subject_id: str
    search_profile_id: str
    intent_mode: SearchProfileIntentMode
    enabled: bool
    now: datetime
    policy_note: str | None = None


@dataclass(frozen=True, slots=True)
class EnableAutoRequestApplicationBatchCommand:
    subject_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class EnableAutoRequestApplicationBatchSummary:
    selected: int
    created: int
    unchanged: int
    failed: int

    def __post_init__(self) -> None:
        for name in ("selected", "created", "unchanged", "failed"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError("batch counts must be non-negative integers")
        if self.selected != self.created + self.unchanged + self.failed:
            raise ValueError("batch counts are inconsistent")


@dataclass(frozen=True, slots=True)
class EnableAutoRequestApplicationBatchResult:
    status: EnableAutoRequestApplicationBatchStatus
    summary: EnableAutoRequestApplicationBatchSummary
    failure_reason: EnableAutoRequestApplicationBatchFailureReason | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            EnableAutoRequestApplicationBatchStatus(self.status),
        )
        if not isinstance(
            self.summary, EnableAutoRequestApplicationBatchSummary
        ):
            raise TypeError("batch summary must be typed")
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                EnableAutoRequestApplicationBatchFailureReason(
                    self.failure_reason
                ),
            )
        succeeded = self.summary.created + self.summary.unchanged
        if self.status is EnableAutoRequestApplicationBatchStatus.NOOP:
            if self.summary.selected != 0 or self.failure_reason is not None:
                raise ValueError("NOOP batch result is malformed")
        elif self.status is EnableAutoRequestApplicationBatchStatus.COMPLETED:
            if (
                self.summary.selected == 0
                or self.summary.failed != 0
                or self.failure_reason is not None
            ):
                raise ValueError("completed batch result is malformed")
        elif (
            self.status
            is EnableAutoRequestApplicationBatchStatus.PARTIAL_FAILURE
        ):
            if (
                succeeded == 0
                or self.summary.failed == 0
                or self.failure_reason
                is not (
                    EnableAutoRequestApplicationBatchFailureReason
                    .POLICY_UPDATE_FAILED
                )
            ):
                raise ValueError("partial batch result is malformed")
        elif self.failure_reason is None:
            raise ValueError("failed batch result requires a failure reason")


@dataclass(frozen=True, slots=True)
class SaveSearchProfileIntentPolicyResult:
    status: SaveSearchProfileIntentPolicyStatus
    policy: SearchProfileIntentPolicy | None
    reason: SaveSearchProfileIntentPolicyReason | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            SaveSearchProfileIntentPolicyStatus(self.status),
        )
        if self.reason is not None:
            object.__setattr__(
                self,
                "reason",
                SaveSearchProfileIntentPolicyReason(self.reason),
            )
        if self.status in {
            SaveSearchProfileIntentPolicyStatus.CREATED,
            SaveSearchProfileIntentPolicyStatus.UNCHANGED,
        }:
            if not isinstance(self.policy, SearchProfileIntentPolicy) or (
                self.reason is not None
            ):
                raise ValueError("successful policy result is malformed")
        elif self.policy is not None or self.reason is None:
            raise ValueError("failed policy result is malformed")


@dataclass(frozen=True, slots=True)
class SearchProfileIntentPolicyReadResult:
    status: SearchProfileIntentPolicyReadStatus
    policy: SearchProfileIntentPolicy | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            SearchProfileIntentPolicyReadStatus(self.status),
        )
        if self.status is SearchProfileIntentPolicyReadStatus.FOUND:
            if not isinstance(self.policy, SearchProfileIntentPolicy):
                raise ValueError("FOUND policy read requires a policy")
        elif self.policy is not None:
            raise ValueError("non-FOUND policy read cannot expose a policy")


@dataclass(frozen=True, slots=True)
class SearchProfileIntentPolicyWriteResult:
    status: SearchProfileIntentPolicyWriteStatus
    policy: SearchProfileIntentPolicy | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            SearchProfileIntentPolicyWriteStatus(self.status),
        )
        if self.status in {
            SearchProfileIntentPolicyWriteStatus.CREATED,
            SearchProfileIntentPolicyWriteStatus.UNCHANGED,
        }:
            if not isinstance(self.policy, SearchProfileIntentPolicy):
                raise ValueError("successful policy write requires a policy")
        elif self.policy is not None:
            raise ValueError("failed policy write cannot expose a policy")


@dataclass(frozen=True, slots=True)
class SearchProfileIntentDecision:
    status: SearchProfileIntentDecisionStatus
    action: JobIntakeIntent | None
    reason: SearchProfileIntentDecisionReason
    policy: SearchProfileIntentPolicy | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", SearchProfileIntentDecisionStatus(self.status)
        )
        object.__setattr__(
            self, "reason", SearchProfileIntentDecisionReason(self.reason)
        )
        if self.action is not None:
            object.__setattr__(self, "action", JobIntakeIntent(self.action))
        if self.status is SearchProfileIntentDecisionStatus.DECIDED:
            if self.action not in {
                JobIntakeIntent.ADD_JOB,
                JobIntakeIntent.REQUEST_APPLICATION,
            }:
                raise ValueError("decided policy requires an action")
        elif self.action is not None:
            raise ValueError("failed policy decision cannot expose an action")


@runtime_checkable
class SearchProfileIntentPolicyRepository(Protocol):
    def save(
        self, policy: SearchProfileIntentPolicy
    ) -> SearchProfileIntentPolicyWriteResult: ...

    def get_current(
        self, subject_id: str, search_profile_id: str
    ) -> SearchProfileIntentPolicyReadResult: ...


@runtime_checkable
class SearchProfileIntentPolicyProvider(Protocol):
    def get_current(
        self, subject_id: str, search_profile_id: str
    ) -> SearchProfileIntentPolicyReadResult: ...


def _policy_from_dict(value: Any) -> SearchProfileIntentPolicy:
    expected = {
        "canonical_hash",
        "contract_version",
        "created_at",
        "enabled",
        "intent_mode",
        "policy_note",
        "policy_version",
        "profile_id",
        "search_profile_version",
        "source",
        "subject_id",
        "updated_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted intent policy is malformed")
    source = value["source"]
    if not isinstance(source, Mapping) or set(source) != {"kind", "source_id"}:
        raise ValueError("persisted intent policy source is malformed")
    return SearchProfileIntentPolicy(
        subject_id=value["subject_id"],
        profile_id=value["profile_id"],
        search_profile_version=value["search_profile_version"],
        source=SearchProfileSourceReference(
            kind=source["kind"], source_id=source["source_id"]
        ),
        intent_mode=SearchProfileIntentMode(value["intent_mode"]),
        enabled=value["enabled"],
        policy_note=value["policy_note"],
        policy_version=value["policy_version"],
        canonical_hash=value["canonical_hash"],
        contract_version=value["contract_version"],
        created_at=_parse_time(value["created_at"]),
        updated_at=_parse_time(value["updated_at"]),
    )


class PrivateHomeSearchProfileIntentPolicyRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str, profile_id: str) -> Path:
        subject = _clean("subject_id", subject_id, 160)
        profile = _clean("profile_id", profile_id, 160)
        return (
            self._home.root
            / "state"
            / "discovery"
            / "search-profile-intent-policies"
            / _subject_key(subject)
            / profile
        )

    def _path(self, policy: SearchProfileIntentPolicy) -> Path:
        return self._directory(policy.subject_id, policy.profile_id) / (
            f"v{policy.policy_version:08d}-{policy.canonical_hash}.json"
        )

    @staticmethod
    def _read(path: Path) -> SearchProfileIntentPolicy:
        if path.is_symlink() or not path.is_file():
            raise ValueError("policy path is invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
        policy = _policy_from_dict(value)
        if path.name != (
            f"v{policy.policy_version:08d}-{policy.canonical_hash}.json"
        ):
            raise ValueError("policy filename is invalid")
        return policy

    def get_current(
        self, subject_id: str, search_profile_id: str
    ) -> SearchProfileIntentPolicyReadResult:
        directory = self._directory(subject_id, search_profile_id)
        if not directory.exists():
            return SearchProfileIntentPolicyReadResult(
                SearchProfileIntentPolicyReadStatus.NOT_FOUND, None
            )
        if directory.is_symlink() or not directory.is_dir():
            return SearchProfileIntentPolicyReadResult(
                SearchProfileIntentPolicyReadStatus.INTEGRITY_FAILURE, None
            )
        try:
            policies = tuple(
                self._read(path) for path in sorted(directory.glob("*.json"))
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return SearchProfileIntentPolicyReadResult(
                SearchProfileIntentPolicyReadStatus.INTEGRITY_FAILURE, None
            )
        if not policies or any(
            policy.subject_id != subject_id
            or policy.profile_id != search_profile_id
            for policy in policies
        ):
            return SearchProfileIntentPolicyReadResult(
                SearchProfileIntentPolicyReadStatus.INTEGRITY_FAILURE, None
            )
        versions = [policy.policy_version for policy in policies]
        if len(set(versions)) != len(versions):
            return SearchProfileIntentPolicyReadResult(
                SearchProfileIntentPolicyReadStatus.INTEGRITY_FAILURE, None
            )
        return SearchProfileIntentPolicyReadResult(
            SearchProfileIntentPolicyReadStatus.FOUND,
            max(
                policies,
                key=lambda policy: (
                    policy.policy_version,
                    policy.canonical_hash,
                ),
            ),
        )

    def save(
        self, policy: SearchProfileIntentPolicy
    ) -> SearchProfileIntentPolicyWriteResult:
        if not isinstance(policy, SearchProfileIntentPolicy):
            raise TypeError("policy must be typed")
        path = self._path(policy)
        encoded = (
            json.dumps(
                policy.to_dict(),
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(path, encoded)
            except (OSError, PrivateHomeError, RuntimeError):
                return SearchProfileIntentPolicyWriteResult(
                    SearchProfileIntentPolicyWriteStatus.FAILED, None
                )
            if created:
                return SearchProfileIntentPolicyWriteResult(
                    SearchProfileIntentPolicyWriteStatus.CREATED, policy
                )
            try:
                existing = self._read(path)
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return SearchProfileIntentPolicyWriteResult(
                    SearchProfileIntentPolicyWriteStatus.FAILED, None
                )
            if existing != policy:
                return SearchProfileIntentPolicyWriteResult(
                    SearchProfileIntentPolicyWriteStatus.FAILED, None
                )
            return SearchProfileIntentPolicyWriteResult(
                SearchProfileIntentPolicyWriteStatus.UNCHANGED, existing
            )


def save_search_profile_intent_policy(
    command: SaveSearchProfileIntentPolicyCommand,
    *,
    search_profile_repository: SearchProfileRepository,
    policy_repository: SearchProfileIntentPolicyRepository,
) -> SaveSearchProfileIntentPolicyResult:
    if not isinstance(command, SaveSearchProfileIntentPolicyCommand):
        raise TypeError("command must be typed")
    try:
        subject_id = _clean("subject_id", command.subject_id, 160)
        profile_id = _clean(
            "search_profile_id", command.search_profile_id, 160
        )
        mode = SearchProfileIntentMode(command.intent_mode)
        if type(command.enabled) is not bool:
            raise TypeError("enabled must be boolean")
        now = _aware("now", command.now)
        note = (
            _clean("policy_note", command.policy_note, 1000)
            if command.policy_note is not None
            else None
        )
    except (TypeError, ValueError):
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.INVALID_REQUEST,
        )
    try:
        profile_read = search_profile_repository.get(subject_id, profile_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        profile_read = None
    if not isinstance(profile_read, SearchProfileReadResult):
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.PERSISTENCE_FAILURE,
        )
    if profile_read.status is SearchProfileReadStatus.NOT_FOUND:
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.PROFILE_NOT_FOUND,
        )
    if (
        profile_read.status is not SearchProfileReadStatus.FOUND
        or profile_read.profile is None
    ):
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.INTEGRITY_FAILURE,
        )
    profile = profile_read.profile
    if profile.subject_id != subject_id:
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.SUBJECT_MISMATCH,
        )
    try:
        current = policy_repository.get_current(subject_id, profile_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        current = None
    if not isinstance(current, SearchProfileIntentPolicyReadResult):
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.PERSISTENCE_FAILURE,
        )
    if current.status is SearchProfileIntentPolicyReadStatus.INTEGRITY_FAILURE:
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.INTEGRITY_FAILURE,
        )
    previous = current.policy
    if previous is not None and previous.source != profile.source:
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.PROFILE_SOURCE_CHANGED,
        )
    version = previous.policy_version + 1 if previous is not None else 1
    candidate = SearchProfileIntentPolicy.create(
        profile=profile,
        intent_mode=mode,
        enabled=command.enabled,
        policy_note=note,
        policy_version=version,
        created_at=previous.created_at if previous is not None else now,
        updated_at=now,
    )
    if previous is not None and (
        previous.canonical_hash == candidate.canonical_hash
    ):
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.UNCHANGED, previous, None
        )
    try:
        written = policy_repository.save(candidate)
    except (OSError, RuntimeError, TypeError, ValueError):
        written = None
    if (
        written is None
        or written.status is SearchProfileIntentPolicyWriteStatus.FAILED
        or written.policy != candidate
    ):
        return SaveSearchProfileIntentPolicyResult(
            SaveSearchProfileIntentPolicyStatus.FAILED,
            None,
            SaveSearchProfileIntentPolicyReason.PERSISTENCE_FAILURE,
        )
    return SaveSearchProfileIntentPolicyResult(
        SaveSearchProfileIntentPolicyStatus(written.status.value),
        written.policy,
        None,
    )


def _auto_request_batch_result(
    status: EnableAutoRequestApplicationBatchStatus,
    *,
    selected: int,
    created: int = 0,
    unchanged: int = 0,
    failed: int = 0,
    failure_reason: EnableAutoRequestApplicationBatchFailureReason
    | None = None,
) -> EnableAutoRequestApplicationBatchResult:
    return EnableAutoRequestApplicationBatchResult(
        status=status,
        summary=EnableAutoRequestApplicationBatchSummary(
            selected=selected,
            created=created,
            unchanged=unchanged,
            failed=failed,
        ),
        failure_reason=failure_reason,
    )


def _is_enabled_auto_policy_for_profile(
    policy: SearchProfileIntentPolicy | None,
    *,
    profile: SearchProfile,
    subject_id: str,
) -> bool:
    return (
        isinstance(policy, SearchProfileIntentPolicy)
        and policy.subject_id == subject_id
        and policy.profile_id == profile.profile_id
        and policy.source == profile.source
        and policy.intent_mode
        is SearchProfileIntentMode.AUTO_REQUEST_APPLICATION
        and policy.enabled
    )


def enable_auto_request_application_for_enabled_search_profiles(
    command: EnableAutoRequestApplicationBatchCommand,
    *,
    search_profile_repository: SearchProfileRepository,
    policy_repository: SearchProfileIntentPolicyRepository,
) -> EnableAutoRequestApplicationBatchResult:
    """Enable future application intent for one trusted enabled-profile snapshot."""

    if not isinstance(command, EnableAutoRequestApplicationBatchCommand):
        raise TypeError("command must be typed")
    try:
        subject_id = _clean("subject_id", command.subject_id, 160)
        now = _aware("now", command.now)
    except (TypeError, ValueError):
        return _auto_request_batch_result(
            EnableAutoRequestApplicationBatchStatus.FAILED,
            selected=0,
            failure_reason=(
                EnableAutoRequestApplicationBatchFailureReason.INVALID_REQUEST
            ),
        )

    try:
        listed = search_profile_repository.list_enabled(subject_id)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        listed = None
    if not isinstance(listed, SearchProfileListResult):
        return _auto_request_batch_result(
            EnableAutoRequestApplicationBatchStatus.FAILED,
            selected=0,
            failure_reason=(
                EnableAutoRequestApplicationBatchFailureReason
                .PROFILE_SNAPSHOT_FAILED
            ),
        )
    if listed.status is not SearchProfileListStatus.SUCCEEDED:
        return _auto_request_batch_result(
            EnableAutoRequestApplicationBatchStatus.FAILED,
            selected=0,
            failure_reason=(
                EnableAutoRequestApplicationBatchFailureReason
                .PROFILE_SNAPSHOT_INTEGRITY_FAILURE
            ),
        )

    profiles = listed.profiles
    if (
        any(
            profile.subject_id != subject_id or not profile.enabled
            for profile in profiles
        )
        or len({profile.profile_id for profile in profiles}) != len(profiles)
    ):
        return _auto_request_batch_result(
            EnableAutoRequestApplicationBatchStatus.FAILED,
            selected=0,
            failure_reason=(
                EnableAutoRequestApplicationBatchFailureReason
                .PROFILE_SNAPSHOT_INTEGRITY_FAILURE
            ),
        )
    selected = len(profiles)
    if selected == 0:
        return _auto_request_batch_result(
            EnableAutoRequestApplicationBatchStatus.NOOP,
            selected=0,
        )

    created = 0
    unchanged = 0
    failed = 0
    integrity_reasons = {
        SaveSearchProfileIntentPolicyReason.INVALID_REQUEST,
        SaveSearchProfileIntentPolicyReason.PROFILE_NOT_FOUND,
        SaveSearchProfileIntentPolicyReason.SUBJECT_MISMATCH,
        SaveSearchProfileIntentPolicyReason.PROFILE_SOURCE_CHANGED,
        SaveSearchProfileIntentPolicyReason.INTEGRITY_FAILURE,
    }
    for profile in profiles:
        try:
            result = save_search_profile_intent_policy(
                SaveSearchProfileIntentPolicyCommand(
                    subject_id=subject_id,
                    search_profile_id=profile.profile_id,
                    intent_mode=(
                        SearchProfileIntentMode.AUTO_REQUEST_APPLICATION
                    ),
                    enabled=True,
                    now=now,
                ),
                search_profile_repository=search_profile_repository,
                policy_repository=policy_repository,
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            result = None
        if not isinstance(result, SaveSearchProfileIntentPolicyResult):
            failed += 1
            continue
        if result.status in {
            SaveSearchProfileIntentPolicyStatus.CREATED,
            SaveSearchProfileIntentPolicyStatus.UNCHANGED,
        }:
            if not _is_enabled_auto_policy_for_profile(
                result.policy,
                profile=profile,
                subject_id=subject_id,
            ):
                failed = selected - created - unchanged
                return _auto_request_batch_result(
                    EnableAutoRequestApplicationBatchStatus.FAILED,
                    selected=selected,
                    created=created,
                    unchanged=unchanged,
                    failed=failed,
                    failure_reason=(
                        EnableAutoRequestApplicationBatchFailureReason
                        .POLICY_INTEGRITY_FAILURE
                    ),
                )
            if result.status is SaveSearchProfileIntentPolicyStatus.CREATED:
                created += 1
            else:
                unchanged += 1
            continue
        failed += 1
        if result.reason in integrity_reasons:
            failed = selected - created - unchanged
            return _auto_request_batch_result(
                EnableAutoRequestApplicationBatchStatus.FAILED,
                selected=selected,
                created=created,
                unchanged=unchanged,
                failed=failed,
                failure_reason=(
                    EnableAutoRequestApplicationBatchFailureReason
                    .POLICY_INTEGRITY_FAILURE
                ),
            )

    if failed == 0:
        return _auto_request_batch_result(
            EnableAutoRequestApplicationBatchStatus.COMPLETED,
            selected=selected,
            created=created,
            unchanged=unchanged,
        )
    if created + unchanged == 0:
        status = EnableAutoRequestApplicationBatchStatus.FAILED
    else:
        status = EnableAutoRequestApplicationBatchStatus.PARTIAL_FAILURE
    return _auto_request_batch_result(
        status,
        selected=selected,
        created=created,
        unchanged=unchanged,
        failed=failed,
        failure_reason=(
            EnableAutoRequestApplicationBatchFailureReason.POLICY_UPDATE_FAILED
        ),
    )


def decide_search_profile_intent(
    profile: SearchProfile,
    discovery_result: JobDiscoveryResponse,
    *,
    policy_provider: SearchProfileIntentPolicyProvider,
) -> SearchProfileIntentDecision:
    if not isinstance(profile, SearchProfile) or not isinstance(
        discovery_result, JobDiscoveryResponse
    ):
        raise TypeError("profile and discovery_result must be typed")
    if (
        discovery_result.disposition is not DiscoveryDisposition.ACCEPTED
        or discovery_result.original_intent is not JobIntakeIntent.ADD_JOB
        or discovery_result.job_id is None
        or discovery_result.run_id is None
    ):
        return SearchProfileIntentDecision(
            SearchProfileIntentDecisionStatus.FAILED,
            None,
            SearchProfileIntentDecisionReason.DISCOVERY_NOT_ACCEPTED,
            None,
        )
    if not profile.enabled:
        return SearchProfileIntentDecision(
            SearchProfileIntentDecisionStatus.DECIDED,
            JobIntakeIntent.ADD_JOB,
            SearchProfileIntentDecisionReason.PROFILE_NOT_ENABLED,
            None,
        )
    try:
        current = policy_provider.get_current(
            profile.subject_id, profile.profile_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        current = None
    if not isinstance(current, SearchProfileIntentPolicyReadResult):
        return SearchProfileIntentDecision(
            SearchProfileIntentDecisionStatus.FAILED,
            None,
            SearchProfileIntentDecisionReason.POLICY_INTEGRITY_FAILURE,
            None,
        )
    if current.status is SearchProfileIntentPolicyReadStatus.NOT_FOUND:
        return SearchProfileIntentDecision(
            SearchProfileIntentDecisionStatus.DECIDED,
            JobIntakeIntent.ADD_JOB,
            SearchProfileIntentDecisionReason.DEFAULT_ADD_JOB_ONLY,
            None,
        )
    policy = current.policy
    if (
        current.status is not SearchProfileIntentPolicyReadStatus.FOUND
        or policy is None
        or policy.subject_id != profile.subject_id
        or policy.profile_id != profile.profile_id
        or policy.source != profile.source
    ):
        return SearchProfileIntentDecision(
            SearchProfileIntentDecisionStatus.FAILED,
            None,
            SearchProfileIntentDecisionReason.BINDING_MISMATCH,
            policy,
        )
    if (
        policy.enabled
        and policy.intent_mode
        is SearchProfileIntentMode.AUTO_REQUEST_APPLICATION
    ):
        return SearchProfileIntentDecision(
            SearchProfileIntentDecisionStatus.DECIDED,
            JobIntakeIntent.REQUEST_APPLICATION,
            (
                SearchProfileIntentDecisionReason
                .EXPLICIT_AUTO_REQUEST_APPLICATION
            ),
            policy,
        )
    return SearchProfileIntentDecision(
        SearchProfileIntentDecisionStatus.DECIDED,
        JobIntakeIntent.ADD_JOB,
        SearchProfileIntentDecisionReason.EXPLICIT_ADD_JOB_ONLY,
        policy,
    )


__all__ = [
    "ENABLE_AUTO_REQUEST_APPLICATION_BATCH_CONTRACT_VERSION",
    "SEARCH_PROFILE_INTENT_POLICY_CONTRACT_VERSION",
    "EnableAutoRequestApplicationBatchCommand",
    "EnableAutoRequestApplicationBatchFailureReason",
    "EnableAutoRequestApplicationBatchResult",
    "EnableAutoRequestApplicationBatchStatus",
    "EnableAutoRequestApplicationBatchSummary",
    "PrivateHomeSearchProfileIntentPolicyRepository",
    "SaveSearchProfileIntentPolicyCommand",
    "SaveSearchProfileIntentPolicyReason",
    "SaveSearchProfileIntentPolicyResult",
    "SaveSearchProfileIntentPolicyStatus",
    "SearchProfileIntentDecision",
    "SearchProfileIntentDecisionReason",
    "SearchProfileIntentDecisionStatus",
    "SearchProfileIntentMode",
    "SearchProfileIntentPolicy",
    "SearchProfileIntentPolicyProvider",
    "SearchProfileIntentPolicyReadResult",
    "SearchProfileIntentPolicyReadStatus",
    "SearchProfileIntentPolicyRepository",
    "SearchProfileIntentPolicyWriteResult",
    "SearchProfileIntentPolicyWriteStatus",
    "decide_search_profile_intent",
    "enable_auto_request_application_for_enabled_search_profiles",
    "save_search_profile_intent_policy",
]
