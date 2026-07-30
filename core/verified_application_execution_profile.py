"""Plan-scoped execution profiles projected from verified identity facts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS,
    APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION,
    ApplicationExecutionIdentityFieldKey,
    ApplicationExecutionIdentityFieldRequiredness,
    ApplicationExecutionIdentityFieldValueType,
    ApplicationExecutionIdentityProfile,
)
from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .candidate_identity_facts import (
    CandidateIdentityFact,
    CandidateIdentityFactConflictState,
    CandidateIdentityFactRepository,
    CandidateIdentityFactSourceKind,
    CandidateIdentityFactVerificationStatus,
    GetCurrentCandidateIdentityFactCommand,
    GetCurrentCandidateIdentityFactStatus,
    get_current_candidate_identity_fact,
)
from .private_home import PrivateHome, PrivateHomeError


VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION = (
    "verified-application-execution-profile-v1"
)
VERIFIED_EXECUTION_PROFILE_FIELD_BINDING_CONTRACT_VERSION = (
    "verified-execution-profile-field-binding-v1"
)
VERIFIED_APPLICATION_EXECUTION_PROFILE_REPOSITORY_SCHEMA_VERSION = 1

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,239}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


class ProjectVerifiedApplicationExecutionProfileStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_READY = "NOT_READY"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class VerifiedApplicationExecutionProfileReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


def _clean_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _clean_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _format_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("created_at is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("created_at is invalid")
    return parsed.astimezone(timezone.utc)


def _plan_binding_hash(plan: ApplicationPlan) -> str:
    return _hash(
        {
            "application_plan_contract_version": plan.contract_version,
            "application_plan_id": plan.plan_id,
            "job_content_hash": plan.job_content_hash,
            "job_id": plan.job_id,
            "job_revision": plan.job_revision,
            "subject_id": plan.subject_id,
        }
    )


@dataclass(frozen=True, slots=True)
class VerifiedExecutionProfileField:
    field_key: ApplicationExecutionIdentityFieldKey
    normalized_value: str = field(repr=False)
    value_type: ApplicationExecutionIdentityFieldValueType
    source_fact_id: str
    source_fact_version: int
    source_fact_hash: str
    source_fact_type: CandidateIdentityFactSourceKind
    verification_status: CandidateIdentityFactVerificationStatus
    normalization_policy_version: str
    field_binding_hash: str
    field_binding_contract_version: str = (
        VERIFIED_EXECUTION_PROFILE_FIELD_BINDING_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "field_key", ApplicationExecutionIdentityFieldKey(self.field_key)
        )
        object.__setattr__(
            self,
            "value_type",
            ApplicationExecutionIdentityFieldValueType(self.value_type),
        )
        object.__setattr__(
            self,
            "source_fact_type",
            CandidateIdentityFactSourceKind(self.source_fact_type),
        )
        object.__setattr__(
            self,
            "verification_status",
            CandidateIdentityFactVerificationStatus(self.verification_status),
        )
        if not isinstance(self.normalized_value, str) or not self.normalized_value:
            raise ValueError("normalized profile value is invalid")
        _clean_id("source_fact_id", self.source_fact_id)
        if type(self.source_fact_version) is not int or self.source_fact_version <= 0:
            raise ValueError("source fact version is invalid")
        _clean_hash("source_fact_hash", self.source_fact_hash)
        _clean_id("normalization_policy_version", self.normalization_policy_version)
        _clean_hash("field_binding_hash", self.field_binding_hash)
        if (
            self.field_binding_contract_version
            != VERIFIED_EXECUTION_PROFILE_FIELD_BINDING_CONTRACT_VERSION
        ):
            raise ValueError("field binding contract version is unsupported")
        if not self.verification_status.eligible_for_current:
            raise ValueError("execution profile field is not verified")
        if self.field_binding_hash != _hash(self.identity_dict()):
            raise ValueError("field binding hash is invalid")

    @classmethod
    def from_fact(
        cls,
        fact: CandidateIdentityFact,
        *,
        value_type: ApplicationExecutionIdentityFieldValueType,
    ) -> "VerifiedExecutionProfileField":
        identity = {
            "field_binding_contract_version": (
                VERIFIED_EXECUTION_PROFILE_FIELD_BINDING_CONTRACT_VERSION
            ),
            "field_key": fact.field_key.value,
            "normalization_policy_version": fact.normalization_policy_version,
            "normalized_value": fact.normalized_value,
            "source_fact_hash": fact.content_hash,
            "source_fact_id": fact.fact_id,
            "source_fact_type": fact.source_ref.source_kind.value,
            "source_fact_version": fact.field_version,
            "value_type": value_type.value,
            "verification_status": fact.verification_status.value,
        }
        return cls(
            field_key=fact.field_key,
            normalized_value=fact.normalized_value,
            value_type=value_type,
            source_fact_id=fact.fact_id,
            source_fact_version=fact.field_version,
            source_fact_hash=fact.content_hash,
            source_fact_type=fact.source_ref.source_kind,
            verification_status=fact.verification_status,
            normalization_policy_version=fact.normalization_policy_version,
            field_binding_hash=_hash(identity),
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "field_binding_contract_version": self.field_binding_contract_version,
            "field_key": self.field_key.value,
            "normalization_policy_version": self.normalization_policy_version,
            "normalized_value": self.normalized_value,
            "source_fact_hash": self.source_fact_hash,
            "source_fact_id": self.source_fact_id,
            "source_fact_type": self.source_fact_type.value,
            "source_fact_version": self.source_fact_version,
            "value_type": self.value_type.value,
            "verification_status": self.verification_status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "field_binding_hash": self.field_binding_hash}


def _field_from_dict(value: Mapping[str, Any]) -> VerifiedExecutionProfileField:
    expected = {
        "field_binding_contract_version",
        "field_binding_hash",
        "field_key",
        "normalization_policy_version",
        "normalized_value",
        "source_fact_hash",
        "source_fact_id",
        "source_fact_type",
        "source_fact_version",
        "value_type",
        "verification_status",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted profile field is invalid")
    return VerifiedExecutionProfileField(**dict(value))


@dataclass(frozen=True, slots=True)
class VerifiedApplicationExecutionProfile:
    profile_snapshot_id: str
    subject_id: str
    application_plan_id: str
    job_id: str
    plan_binding_hash: str
    ordered_fields: tuple[VerifiedExecutionProfileField, ...] = field(repr=False)
    required_field_keys: tuple[ApplicationExecutionIdentityFieldKey, ...]
    optional_field_keys: tuple[ApplicationExecutionIdentityFieldKey, ...]
    profile_snapshot_hash: str
    created_at: datetime
    invocation_id: str
    profile_contract_version: str = (
        VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION
    )
    field_registry_version: str = (
        APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION
    )

    def __post_init__(self) -> None:
        _clean_id("profile_snapshot_id", self.profile_snapshot_id)
        _clean_id("subject_id", self.subject_id)
        _clean_id("application_plan_id", self.application_plan_id)
        _clean_id("job_id", self.job_id)
        _clean_hash("plan_binding_hash", self.plan_binding_hash)
        _clean_hash("profile_snapshot_hash", self.profile_snapshot_hash)
        _clean_id("invocation_id", self.invocation_id)
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(
            self, "created_at", self.created_at.astimezone(timezone.utc)
        )
        if (
            self.profile_contract_version
            != VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION
        ):
            raise ValueError("profile contract version is unsupported")
        if self.field_registry_version != APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION:
            raise ValueError("field registry version is unsupported")
        fields = tuple(self.ordered_fields)
        if any(not isinstance(item, VerifiedExecutionProfileField) for item in fields):
            raise TypeError("ordered profile fields are invalid")
        registry_order = tuple(
            definition.field_key
            for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS
        )
        actual_keys = tuple(item.field_key for item in fields)
        if actual_keys != tuple(key for key in registry_order if key in actual_keys):
            raise ValueError("ordered profile fields are not in registry order")
        if len(actual_keys) != len(set(actual_keys)):
            raise ValueError("ordered profile fields contain duplicates")
        required = tuple(ApplicationExecutionIdentityFieldKey(item) for item in self.required_field_keys)
        optional = tuple(ApplicationExecutionIdentityFieldKey(item) for item in self.optional_field_keys)
        expected_required = tuple(
            definition.field_key
            for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS
            if definition.requiredness
            is ApplicationExecutionIdentityFieldRequiredness.REQUIRED_FOR_EXECUTION
        )
        expected_optional = tuple(
            definition.field_key
            for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS
            if definition.requiredness
            is ApplicationExecutionIdentityFieldRequiredness.OPTIONAL
        )
        if required != expected_required or optional != expected_optional:
            raise ValueError("profile requiredness registry binding is invalid")
        if not set(required).issubset(actual_keys):
            raise ValueError("profile is missing required fields")
        object.__setattr__(self, "ordered_fields", fields)
        object.__setattr__(self, "required_field_keys", required)
        object.__setattr__(self, "optional_field_keys", optional)
        if self.profile_snapshot_hash != _hash(self.identity_dict()):
            raise ValueError("profile snapshot hash is invalid")
        if self.profile_snapshot_id != (
            f"verified-execution-profile-{self.profile_snapshot_hash[:32]}"
        ):
            raise ValueError("profile snapshot ID is invalid")

    @property
    def source_fact_bindings(self) -> tuple[tuple[str, int, str], ...]:
        return tuple(
            (item.source_fact_id, item.source_fact_version, item.source_fact_hash)
            for item in self.ordered_fields
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "field_registry_version": self.field_registry_version,
            "job_id": self.job_id,
            "optional_field_keys": tuple(key.value for key in self.optional_field_keys),
            "ordered_fields": tuple(item.to_dict() for item in self.ordered_fields),
            "plan_binding_hash": self.plan_binding_hash,
            "profile_contract_version": self.profile_contract_version,
            "required_field_keys": tuple(key.value for key in self.required_field_keys),
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "created_at": _format_time(self.created_at),
            "invocation_id": self.invocation_id,
            "profile_snapshot_hash": self.profile_snapshot_hash,
            "profile_snapshot_id": self.profile_snapshot_id,
        }


def _snapshot_from_dict(value: Mapping[str, Any]) -> VerifiedApplicationExecutionProfile:
    expected = {
        "application_plan_id",
        "created_at",
        "field_registry_version",
        "invocation_id",
        "job_id",
        "optional_field_keys",
        "ordered_fields",
        "plan_binding_hash",
        "profile_contract_version",
        "profile_snapshot_hash",
        "profile_snapshot_id",
        "required_field_keys",
        "subject_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted verified profile fields are invalid")
    payload = dict(value)
    payload["created_at"] = _parse_time(payload["created_at"])
    payload["ordered_fields"] = tuple(_field_from_dict(item) for item in payload["ordered_fields"])
    payload["required_field_keys"] = tuple(payload["required_field_keys"])
    payload["optional_field_keys"] = tuple(payload["optional_field_keys"])
    return VerifiedApplicationExecutionProfile(**payload)


@dataclass(frozen=True, slots=True)
class ProjectVerifiedApplicationExecutionProfileCommand:
    subject_id: str
    application_plan_id: str
    invocation_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ProjectVerifiedApplicationExecutionProfileResult:
    status: ProjectVerifiedApplicationExecutionProfileStatus
    snapshot: VerifiedApplicationExecutionProfile | None = field(
        default=None, repr=False
    )
    missing_field_keys: tuple[ApplicationExecutionIdentityFieldKey, ...] = ()
    failure_code: str | None = None

    def __post_init__(self) -> None:
        status = ProjectVerifiedApplicationExecutionProfileStatus(self.status)
        object.__setattr__(self, "status", status)
        missing = tuple(ApplicationExecutionIdentityFieldKey(item) for item in self.missing_field_keys)
        object.__setattr__(self, "missing_field_keys", missing)
        if status in {
            ProjectVerifiedApplicationExecutionProfileStatus.CREATED,
            ProjectVerifiedApplicationExecutionProfileStatus.UNCHANGED,
        }:
            if self.snapshot is None or missing or self.failure_code is not None:
                raise ValueError("successful verified profile result is invalid")
        elif self.snapshot is not None:
            raise ValueError("failed verified profile result exposes a partial snapshot")


@dataclass(frozen=True, slots=True)
class VerifiedApplicationExecutionProfileReadResult:
    status: VerifiedApplicationExecutionProfileReadStatus
    snapshot: VerifiedApplicationExecutionProfile | None = field(
        default=None, repr=False
    )
    failure_code: str | None = None

    def __post_init__(self) -> None:
        status = VerifiedApplicationExecutionProfileReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if status is VerifiedApplicationExecutionProfileReadStatus.FOUND:
            if self.snapshot is None or self.failure_code is not None:
                raise ValueError("found verified profile result is invalid")
        elif self.snapshot is not None:
            raise ValueError("non-found verified profile result exposes a snapshot")
        elif (
            status is VerifiedApplicationExecutionProfileReadStatus.NOT_FOUND
            and self.failure_code is not None
        ):
            raise ValueError("not-found verified profile result is invalid")
        elif (
            status is VerifiedApplicationExecutionProfileReadStatus.INTEGRITY_FAILURE
            and not self.failure_code
        ):
            raise ValueError("integrity-failure verified profile result is invalid")


@dataclass(frozen=True, slots=True)
class VerifiedApplicationExecutionProfileWriteResult:
    created: bool
    snapshot: VerifiedApplicationExecutionProfile = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.created) is not bool:
            raise TypeError("created must be a boolean")
        if not isinstance(self.snapshot, VerifiedApplicationExecutionProfile):
            raise TypeError("snapshot is invalid")


@runtime_checkable
class VerifiedApplicationExecutionProfileRepository(Protocol):
    def save(
        self,
        snapshot: VerifiedApplicationExecutionProfile,
        *,
        request_hash: str,
    ) -> VerifiedApplicationExecutionProfileWriteResult: ...

    def get(
        self, subject_id: str, profile_snapshot_id: str
    ) -> VerifiedApplicationExecutionProfileReadResult: ...


class PrivateHomeVerifiedApplicationExecutionProfileRepository:
    """Immutable JSON snapshots and invocation receipts in Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    @property
    def root(self) -> Path:
        return self._home.paths.verified_application_execution_profiles

    def _snapshot_path(self, snapshot_id: str) -> Path:
        return self.root / "snapshots" / f"{_clean_id('profile_snapshot_id', snapshot_id)}.json"

    def _invocation_path(self, invocation_id: str) -> Path:
        return self.root / "invocations" / f"{_clean_id('invocation_id', invocation_id)}.json"

    @staticmethod
    def _encoded(value: Mapping[str, Any]) -> bytes:
        return (
            json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

    def get(
        self, subject_id: str, profile_snapshot_id: str
    ) -> VerifiedApplicationExecutionProfileReadResult:
        try:
            subject = _clean_id("subject_id", subject_id)
            path = self._snapshot_path(profile_snapshot_id)
            if not path.exists():
                return VerifiedApplicationExecutionProfileReadResult(
                    VerifiedApplicationExecutionProfileReadStatus.NOT_FOUND
                )
            if path.is_symlink() or not path.is_file():
                raise ValueError("snapshot path is invalid")
            snapshot = _snapshot_from_dict(json.loads(path.read_text(encoding="utf-8")))
            if (
                snapshot.subject_id != subject
                or path.stem != snapshot.profile_snapshot_id
            ):
                raise ValueError("snapshot subject binding is invalid")
            return VerifiedApplicationExecutionProfileReadResult(
                VerifiedApplicationExecutionProfileReadStatus.FOUND,
                snapshot=snapshot,
            )
        except (
            OSError,
            PrivateHomeError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return VerifiedApplicationExecutionProfileReadResult(
                VerifiedApplicationExecutionProfileReadStatus.INTEGRITY_FAILURE,
                failure_code="VERIFIED_PROFILE_INTEGRITY_FAILURE",
            )

    def save(
        self,
        snapshot: VerifiedApplicationExecutionProfile,
        *,
        request_hash: str,
    ) -> VerifiedApplicationExecutionProfileWriteResult:
        if not isinstance(snapshot, VerifiedApplicationExecutionProfile):
            raise TypeError("snapshot is invalid")
        request = _clean_hash("request_hash", request_hash)
        self._home.ensure()
        (self.root / "snapshots").mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / "invocations").mkdir(mode=0o700, parents=True, exist_ok=True)
        invocation_path = self._invocation_path(snapshot.invocation_id)
        receipt = {
            "profile_snapshot_id": snapshot.profile_snapshot_id,
            "repository_schema_version": (
                VERIFIED_APPLICATION_EXECUTION_PROFILE_REPOSITORY_SCHEMA_VERSION
            ),
            "request_hash": request,
            "subject_id": snapshot.subject_id,
        }
        if invocation_path.exists():
            if invocation_path.is_symlink() or not invocation_path.is_file():
                raise ValueError("invocation receipt path is invalid")
            existing = json.loads(invocation_path.read_text(encoding="utf-8"))
            if existing != receipt:
                raise ValueError("invocation payload mismatch")
            read = self.get(snapshot.subject_id, snapshot.profile_snapshot_id)
            if read.status is not VerifiedApplicationExecutionProfileReadStatus.FOUND:
                raise ValueError("invocation snapshot binding is invalid")
            return VerifiedApplicationExecutionProfileWriteResult(False, read.snapshot)
        snapshot_path = self._snapshot_path(snapshot.profile_snapshot_id)
        created = self._home.write_bytes_if_absent(
            snapshot_path, self._encoded(snapshot.to_dict())
        )
        read = self.get(snapshot.subject_id, snapshot.profile_snapshot_id)
        if (
            read.status is not VerifiedApplicationExecutionProfileReadStatus.FOUND
            or read.snapshot is None
            or read.snapshot.profile_snapshot_hash != snapshot.profile_snapshot_hash
        ):
            raise ValueError("saved snapshot integrity check failed")
        self._home.write_bytes_if_absent(invocation_path, self._encoded(receipt))
        persisted_receipt = json.loads(invocation_path.read_text(encoding="utf-8"))
        if persisted_receipt != receipt:
            raise ValueError("invocation receipt integrity check failed")
        return VerifiedApplicationExecutionProfileWriteResult(created, read.snapshot)


def to_application_bundle_profile(
    snapshot: VerifiedApplicationExecutionProfile,
) -> Mapping[str, object]:
    """Purely project verified values into the existing closed Bundle shape."""

    if not isinstance(snapshot, VerifiedApplicationExecutionProfile):
        raise TypeError("snapshot is invalid")
    values = {item.field_key.value: item.normalized_value for item in snapshot.ordered_fields}
    profile = ApplicationExecutionIdentityProfile(**values)
    return profile.to_application_bundle_profile()


def project_verified_application_execution_profile(
    command: ProjectVerifiedApplicationExecutionProfileCommand,
    *,
    plan_repository: ApplicationPlanRepository,
    fact_repository: CandidateIdentityFactRepository,
    repository: VerifiedApplicationExecutionProfileRepository,
) -> ProjectVerifiedApplicationExecutionProfileResult:
    """Project exact current verified facts into one immutable Plan snapshot."""

    try:
        subject_id = _clean_id("subject_id", command.subject_id)
        plan_id = _clean_id("application_plan_id", command.application_plan_id)
        invocation_id = _clean_id("invocation_id", command.invocation_id)
        _format_time(command.now)
        plan_read = plan_repository.get(plan_id)
        if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
            return ProjectVerifiedApplicationExecutionProfileResult(
                ProjectVerifiedApplicationExecutionProfileStatus.NOT_READY,
                failure_code="APPLICATION_PLAN_NOT_FOUND",
            )
        if (
            plan_read.status is not ApplicationPlanReadStatus.FOUND
            or plan_read.plan is None
        ):
            return ProjectVerifiedApplicationExecutionProfileResult(
                ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE,
                failure_code="APPLICATION_PLAN_INTEGRITY_FAILURE",
            )
        plan = plan_read.plan
        if plan.subject_id != subject_id:
            return ProjectVerifiedApplicationExecutionProfileResult(
                ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE,
                failure_code="APPLICATION_PLAN_SUBJECT_MISMATCH",
            )

        fields: list[VerifiedExecutionProfileField] = []
        missing: list[ApplicationExecutionIdentityFieldKey] = []
        current_index = fact_repository.get_index(subject_id)
        if current_index.subject_id != subject_id:
            return ProjectVerifiedApplicationExecutionProfileResult(
                ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE,
                failure_code="CANDIDATE_IDENTITY_INDEX_SUBJECT_MISMATCH",
            )
        index_entries = {item.field_key: item for item in current_index.entries}
        for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS:
            index_entry = index_entries.get(definition.field_key)
            if index_entry is None or index_entry.conflict_state is not (
                CandidateIdentityFactConflictState.NONE
            ):
                return ProjectVerifiedApplicationExecutionProfileResult(
                    ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE,
                    failure_code="CANDIDATE_IDENTITY_INDEX_INTEGRITY_FAILURE",
                )
            current = get_current_candidate_identity_fact(
                GetCurrentCandidateIdentityFactCommand(
                    subject_id=subject_id,
                    field_key=definition.field_key,
                ),
                repository=fact_repository,
            )
            if current.status is GetCurrentCandidateIdentityFactStatus.MISSING:
                if index_entry.current_fact_id is not None:
                    return ProjectVerifiedApplicationExecutionProfileResult(
                        ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE,
                        failure_code="CANDIDATE_IDENTITY_INDEX_DRIFT",
                    )
                if (
                    definition.requiredness
                    is ApplicationExecutionIdentityFieldRequiredness.REQUIRED_FOR_EXECUTION
                ):
                    missing.append(definition.field_key)
                continue
            if (
                current.status is not GetCurrentCandidateIdentityFactStatus.FOUND
                or current.fact is None
                or current.current_lineage_head_id != current.fact.fact_id
            ):
                return ProjectVerifiedApplicationExecutionProfileResult(
                    ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE,
                    failure_code="CANDIDATE_IDENTITY_CURRENT_INTEGRITY_FAILURE",
                )
            fact = current.fact
            if (
                index_entry.current_fact_id != fact.fact_id
                or index_entry.current_fact_hash != fact.content_hash
                or index_entry.current_fact_version != fact.field_version
                or index_entry.verification_status is not fact.verification_status
                or fact.subject_id != subject_id
                or fact.field_key is not definition.field_key
                or not fact.eligible_for_current
                or fact.source_ref.source_subject_id != subject_id
                or fact.normalization_policy_version
                != definition.normalization_policy_version
            ):
                return ProjectVerifiedApplicationExecutionProfileResult(
                    ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE,
                    failure_code="CANDIDATE_IDENTITY_FACT_BINDING_MISMATCH",
                )
            fields.append(
                VerifiedExecutionProfileField.from_fact(
                    fact, value_type=definition.value_type
                )
            )
        if missing:
            return ProjectVerifiedApplicationExecutionProfileResult(
                ProjectVerifiedApplicationExecutionProfileStatus.NOT_READY,
                missing_field_keys=tuple(missing),
                failure_code="REQUIRED_IDENTITY_FACTS_MISSING",
            )

        required = tuple(
            definition.field_key
            for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS
            if definition.requiredness
            is ApplicationExecutionIdentityFieldRequiredness.REQUIRED_FOR_EXECUTION
        )
        optional = tuple(
            definition.field_key
            for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS
            if definition.requiredness
            is ApplicationExecutionIdentityFieldRequiredness.OPTIONAL
        )
        identity = {
            "application_plan_id": plan.plan_id,
            "field_registry_version": APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION,
            "job_id": plan.job_id,
            "optional_field_keys": tuple(key.value for key in optional),
            "ordered_fields": tuple(item.to_dict() for item in fields),
            "plan_binding_hash": _plan_binding_hash(plan),
            "profile_contract_version": VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION,
            "required_field_keys": tuple(key.value for key in required),
            "subject_id": subject_id,
        }
        snapshot_hash = _hash(identity)
        snapshot = VerifiedApplicationExecutionProfile(
            profile_snapshot_id=f"verified-execution-profile-{snapshot_hash[:32]}",
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            job_id=plan.job_id,
            plan_binding_hash=identity["plan_binding_hash"],
            ordered_fields=tuple(fields),
            required_field_keys=required,
            optional_field_keys=optional,
            profile_snapshot_hash=snapshot_hash,
            created_at=command.now,
            invocation_id=invocation_id,
        )
        request_hash = _hash(
            {
                "application_plan_id": plan.plan_id,
                "profile_snapshot_hash": snapshot_hash,
                "subject_id": subject_id,
            }
        )
        write = repository.save(snapshot, request_hash=request_hash)
        return ProjectVerifiedApplicationExecutionProfileResult(
            (
                ProjectVerifiedApplicationExecutionProfileStatus.CREATED
                if write.created
                else ProjectVerifiedApplicationExecutionProfileStatus.UNCHANGED
            ),
            snapshot=write.snapshot,
        )
    except ValueError as exc:
        code = (
            "INVOCATION_PAYLOAD_MISMATCH"
            if "invocation payload mismatch" in str(exc)
            else "VERIFIED_PROFILE_INTEGRITY_FAILURE"
        )
        return ProjectVerifiedApplicationExecutionProfileResult(
            ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE,
            failure_code=code,
        )
    except (OSError, PrivateHomeError, RuntimeError, TypeError):
        return ProjectVerifiedApplicationExecutionProfileResult(
            ProjectVerifiedApplicationExecutionProfileStatus.FAILED,
            failure_code="VERIFIED_PROFILE_PERSISTENCE_FAILED",
        )


__all__ = [
    "VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION",
    "VERIFIED_APPLICATION_EXECUTION_PROFILE_REPOSITORY_SCHEMA_VERSION",
    "VERIFIED_EXECUTION_PROFILE_FIELD_BINDING_CONTRACT_VERSION",
    "PrivateHomeVerifiedApplicationExecutionProfileRepository",
    "ProjectVerifiedApplicationExecutionProfileCommand",
    "ProjectVerifiedApplicationExecutionProfileResult",
    "ProjectVerifiedApplicationExecutionProfileStatus",
    "VerifiedApplicationExecutionProfile",
    "VerifiedApplicationExecutionProfileReadResult",
    "VerifiedApplicationExecutionProfileReadStatus",
    "VerifiedApplicationExecutionProfileRepository",
    "VerifiedApplicationExecutionProfileWriteResult",
    "VerifiedExecutionProfileField",
    "project_verified_application_execution_profile",
    "to_application_bundle_profile",
]
