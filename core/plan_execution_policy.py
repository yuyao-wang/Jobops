"""Plan-bound, immutable execution policy decisions.

This module converts exact ApplicationPlan lineage into the existing
``core.policy.PolicyDecision``.  It does not run gates, inspect runtime state,
or grant a permit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol

from .accepted_job_intent import (
    AcceptedJobIntent,
    AcceptedJobIntentReadStatus,
)
from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
)
from .job_discovery import JobIntakeIntent, JobPosting
from .job_prioritization import (
    PriorityDecision,
    PriorityQualification,
    ProposedPriorityLevel,
)
from .policy import (
    AnswerAuthority,
    ApprovalActor,
    AutonomyMode,
    CoverLetterStrategy,
    JobTier,
    MaterialStrategy,
    PolicyBlocker,
    PolicyConfig,
    PolicyDecision,
    PolicyEngine,
    RiskSignals,
    SubmitAuthority,
    VerificationAuthority,
)
from .prioritization_policy import PrioritizationPolicy
from .private_home import PrivateHome


PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION = (
    "plan-execution-policy-decision-record-v1"
)
PLAN_EXECUTION_POLICY_RULES_VERSION = "plan-execution-policy-rules-v1"
PLAN_EXECUTION_POLICY_CONFIGURATION_CONTRACT_VERSION = (
    "plan-execution-policy-configuration-v1"
)
_RECORD_PATTERN = re.compile(r"^plan-execution-policy-[a-f0-9]{64}$")
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class DecidePlanExecutionPolicyStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_READY = "NOT_READY"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    UNSUPPORTED_POLICY = "UNSUPPORTED_POLICY"
    FAILED = "FAILED"


class PlanExecutionPolicyReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class PlanExecutionPolicyFailureReason(str, Enum):
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    INTENT_NOT_FOUND = "INTENT_NOT_FOUND"
    PRIORITY_DECISION_NOT_FOUND = "PRIORITY_DECISION_NOT_FOUND"
    PRIORITIZATION_POLICY_NOT_FOUND = "PRIORITIZATION_POLICY_NOT_FOUND"
    AUTHORITY_CONFIGURATION_REQUIRED = "AUTHORITY_CONFIGURATION_REQUIRED"
    INTENT_NOT_RUNNABLE = "INTENT_NOT_RUNNABLE"
    PRIORITY_NOT_RUNNABLE = "PRIORITY_NOT_RUNNABLE"
    PRIORITY_LEVEL_UNSUPPORTED = "PRIORITY_LEVEL_UNSUPPORTED"
    LINEAGE_MISMATCH = "LINEAGE_MISMATCH"
    INVOCATION_CONFLICT = "INVOCATION_CONFLICT"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    UNSUPPORTED_CONTRACT = "UNSUPPORTED_CONTRACT"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _clean_id(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the execution policy contract")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _timestamp(value: datetime) -> str:
    return (
        _aware("timestamp", value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is invalid")
    return _aware(
        "timestamp",
        datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def policy_decision_to_dict(decision: PolicyDecision) -> dict[str, Any]:
    if not isinstance(decision, PolicyDecision):
        raise TypeError("decision must be a PolicyDecision")
    return {
        "mode": decision.mode.value,
        "tier": decision.tier.value,
        "material_strategy": decision.material_strategy.value,
        "cover_letter_strategy": decision.cover_letter_strategy.value,
        "answer_authority": decision.answer_authority.value,
        "gate_a_actor": decision.gate_a_actor.value,
        "gate_b_actor": decision.gate_b_actor.value,
        "submit_authority": decision.submit_authority.value,
        "email_verification_authority": (
            decision.email_verification_authority.value
        ),
        "blockers": [item.value for item in decision.blockers],
        "policy_hash": decision.policy_hash,
    }


def _policy_decision_from_dict(value: Any) -> PolicyDecision:
    expected = {
        "mode",
        "tier",
        "material_strategy",
        "cover_letter_strategy",
        "answer_authority",
        "gate_a_actor",
        "gate_b_actor",
        "submit_authority",
        "email_verification_authority",
        "blockers",
        "policy_hash",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted PolicyDecision fields are invalid")
    blockers = value["blockers"]
    if not isinstance(blockers, list):
        raise ValueError("persisted PolicyDecision blockers are invalid")
    return PolicyDecision(
        mode=AutonomyMode(value["mode"]),
        tier=JobTier(value["tier"]),
        material_strategy=MaterialStrategy(value["material_strategy"]),
        cover_letter_strategy=CoverLetterStrategy(
            value["cover_letter_strategy"]
        ),
        answer_authority=AnswerAuthority(value["answer_authority"]),
        gate_a_actor=ApprovalActor(value["gate_a_actor"]),
        gate_b_actor=ApprovalActor(value["gate_b_actor"]),
        submit_authority=SubmitAuthority(value["submit_authority"]),
        email_verification_authority=VerificationAuthority(
            value["email_verification_authority"]
        ),
        blockers=tuple(PolicyBlocker(item) for item in blockers),
        policy_hash=_hash("policy_hash", value["policy_hash"]),
    )


@dataclass(frozen=True, slots=True)
class PlanExecutionPolicyConfiguration:
    configuration_id: str
    configuration_version: int
    policy_config: PolicyConfig
    authority_configured: bool
    configuration_hash: str
    contract_version: str = (
        PLAN_EXECUTION_POLICY_CONFIGURATION_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        identifier = _clean_id("configuration_id", self.configuration_id)
        if type(self.configuration_version) is not int or (
            self.configuration_version < 1
        ):
            raise ValueError("configuration_version must be positive")
        if not isinstance(self.policy_config, PolicyConfig):
            raise TypeError("policy_config must be a PolicyConfig")
        if type(self.authority_configured) is not bool:
            raise TypeError("authority_configured must be boolean")
        if self.contract_version != (
            PLAN_EXECUTION_POLICY_CONFIGURATION_CONTRACT_VERSION
        ):
            raise ValueError("execution policy configuration is unsupported")
        expected = _digest(self._content())
        if _hash("configuration_hash", self.configuration_hash) != expected:
            raise ValueError("execution policy configuration hash is invalid")
        object.__setattr__(self, "configuration_id", identifier)

    def _content(self) -> dict[str, Any]:
        return {
            "authority_configured": self.authority_configured,
            "configuration_id": self.configuration_id,
            "configuration_version": self.configuration_version,
            "contract_version": self.contract_version,
            "policy_config": self.policy_config.to_dict(),
        }

    @classmethod
    def create(
        cls,
        *,
        configuration_id: str,
        configuration_version: int,
        policy_config: PolicyConfig,
        authority_configured: bool,
    ) -> "PlanExecutionPolicyConfiguration":
        values = {
            "configuration_id": configuration_id,
            "configuration_version": configuration_version,
            "policy_config": policy_config,
            "authority_configured": authority_configured,
        }
        temporary = {
            "authority_configured": authority_configured,
            "configuration_id": configuration_id,
            "configuration_version": configuration_version,
            "contract_version": (
                PLAN_EXECUTION_POLICY_CONFIGURATION_CONTRACT_VERSION
            ),
            "policy_config": policy_config.to_dict(),
        }
        return cls(configuration_hash=_digest(temporary), **values)


class UnsupportedExecutionPolicy(ValueError):
    """The exact plan priority has no automatic execution mapping."""


@dataclass(frozen=True, slots=True)
class PlanExecutionPolicyRulesV1:
    configuration: PlanExecutionPolicyConfiguration
    rules_version: str = PLAN_EXECUTION_POLICY_RULES_VERSION

    def __post_init__(self) -> None:
        if not isinstance(
            self.configuration, PlanExecutionPolicyConfiguration
        ):
            raise TypeError("configuration must be typed")
        if self.rules_version != PLAN_EXECUTION_POLICY_RULES_VERSION:
            raise ValueError("execution policy rules version is unsupported")

    def decide(
        self,
        *,
        plan: ApplicationPlan,
        job: JobPosting,
        intent: AcceptedJobIntent,
        priority: PriorityDecision,
        prioritization_policy: PrioritizationPolicy,
    ) -> PolicyDecision:
        # These arguments are deliberately typed and exact-bound by the public
        # service.  Rules remain pure and never consult an active/latest alias.
        del job, intent, priority, prioritization_policy
        tier_by_priority = {
            ProposedPriorityLevel.P0: JobTier.HIGH,
            ProposedPriorityLevel.P1: JobTier.MEDIUM,
            ProposedPriorityLevel.P2: JobTier.LOW,
        }
        try:
            tier = tier_by_priority[plan.priority_level]
        except KeyError as exc:
            raise UnsupportedExecutionPolicy(
                "P3 requires explicit promotion before execution"
            ) from exc
        return PolicyEngine(self.configuration.policy_config).decide(
            tier,
            RiskSignals(),
        )


def plan_execution_policy_plan_binding_hash(plan: ApplicationPlan) -> str:
    """Return the public exact Plan binding used by policy records."""

    if not isinstance(plan, ApplicationPlan):
        raise TypeError("plan must be an ApplicationPlan")
    value = plan.to_dict()
    value.pop("created_at")
    value.pop("user_preparation_instructions")
    return _digest(value)


@dataclass(frozen=True, slots=True)
class PlanExecutionPolicyDecisionRecord:
    record_id: str
    subject_id: str
    application_plan_id: str
    job_id: str
    accepted_intent_id: str
    accepted_intent_hash: str
    priority_decision_id: str
    priority_decision_hash: str
    prioritization_policy_id: str
    prioritization_policy_version: int
    prioritization_policy_hash: str
    plan_binding_hash: str
    execution_rules_version: str
    execution_configuration_id: str
    execution_configuration_version: int
    execution_configuration_hash: str
    policy_decision: PolicyDecision
    policy_decision_hash: str
    input_binding_hash: str
    created_at: datetime
    invocation_id: str
    record_contract_version: str = (
        PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if self.record_contract_version != (
            PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
        ):
            raise ValueError("execution policy record contract is unsupported")
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "accepted_intent_id",
            "priority_decision_id",
            "prioritization_policy_id",
            "execution_rules_version",
            "execution_configuration_id",
            "invocation_id",
        ):
            object.__setattr__(
                self, name, _clean_id(name, getattr(self, name))
            )
        for name in (
            "accepted_intent_hash",
            "priority_decision_hash",
            "prioritization_policy_hash",
            "plan_binding_hash",
            "execution_configuration_hash",
            "policy_decision_hash",
            "input_binding_hash",
        ):
            _hash(name, getattr(self, name))
        if type(self.prioritization_policy_version) is not int or (
            self.prioritization_policy_version < 1
        ):
            raise ValueError("prioritization policy version must be positive")
        if type(self.execution_configuration_version) is not int or (
            self.execution_configuration_version < 1
        ):
            raise ValueError("execution configuration version must be positive")
        if not isinstance(self.policy_decision, PolicyDecision):
            raise TypeError("policy_decision must use core.policy.PolicyDecision")
        if self.policy_decision_hash != _digest(
            policy_decision_to_dict(self.policy_decision)
        ):
            raise ValueError("PolicyDecision hash is invalid")
        _aware("created_at", self.created_at)
        expected_id = f"plan-execution-policy-{_digest(self._identity())}"
        if (
            not isinstance(self.record_id, str)
            or _RECORD_PATTERN.fullmatch(self.record_id) is None
            or self.record_id != expected_id
        ):
            raise ValueError("execution policy record identity is invalid")

    def _identity(self) -> dict[str, Any]:
        return {
            "input_binding_hash": self.input_binding_hash,
            "policy_decision_hash": self.policy_decision_hash,
            "record_contract_version": self.record_contract_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "record_contract_version": self.record_contract_version,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "job_id": self.job_id,
            "accepted_intent_id": self.accepted_intent_id,
            "accepted_intent_hash": self.accepted_intent_hash,
            "priority_decision_id": self.priority_decision_id,
            "priority_decision_hash": self.priority_decision_hash,
            "prioritization_policy_id": self.prioritization_policy_id,
            "prioritization_policy_version": (
                self.prioritization_policy_version
            ),
            "prioritization_policy_hash": self.prioritization_policy_hash,
            "plan_binding_hash": self.plan_binding_hash,
            "execution_rules_version": self.execution_rules_version,
            "execution_configuration_id": self.execution_configuration_id,
            "execution_configuration_version": (
                self.execution_configuration_version
            ),
            "execution_configuration_hash": (
                self.execution_configuration_hash
            ),
            "policy_decision": policy_decision_to_dict(self.policy_decision),
            "policy_decision_hash": self.policy_decision_hash,
            "input_binding_hash": self.input_binding_hash,
            "created_at": _timestamp(self.created_at),
            "invocation_id": self.invocation_id,
        }


def plan_execution_policy_record_hash(
    record: PlanExecutionPolicyDecisionRecord,
) -> str:
    """Hash one exact immutable record without exposing its policy content."""

    if not isinstance(record, PlanExecutionPolicyDecisionRecord):
        raise TypeError("record must be typed")
    return _digest(record.to_dict())


def _record_from_dict(value: Any) -> PlanExecutionPolicyDecisionRecord:
    if not isinstance(value, Mapping):
        raise ValueError("persisted execution policy record is invalid")
    expected = set(
        PlanExecutionPolicyDecisionRecord.__dataclass_fields__.keys()
    )
    if set(value) != expected:
        raise ValueError("persisted execution policy record fields are invalid")
    return PlanExecutionPolicyDecisionRecord(
        **{
            **dict(value),
            "policy_decision": _policy_decision_from_dict(
                value["policy_decision"]
            ),
            "created_at": _parse_timestamp(value["created_at"]),
        }
    )


@dataclass(frozen=True, slots=True)
class DecidePlanExecutionPolicyCommand:
    subject_id: str
    application_plan_id: str
    invocation_id: str
    now: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean_id("subject_id", self.subject_id)
        )
        object.__setattr__(
            self,
            "application_plan_id",
            _clean_id("application_plan_id", self.application_plan_id),
        )
        object.__setattr__(
            self, "invocation_id", _clean_id("invocation_id", self.invocation_id)
        )
        _aware("now", self.now)


@dataclass(frozen=True, slots=True)
class DecidePlanExecutionPolicyResult:
    status: DecidePlanExecutionPolicyStatus
    record: PlanExecutionPolicyDecisionRecord | None = None
    reason: PlanExecutionPolicyFailureReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", DecidePlanExecutionPolicyStatus(self.status)
        )
        if self.reason is not None:
            object.__setattr__(
                self, "reason", PlanExecutionPolicyFailureReason(self.reason)
            )
        successful = self.status in {
            DecidePlanExecutionPolicyStatus.CREATED,
            DecidePlanExecutionPolicyStatus.UNCHANGED,
        }
        if successful != isinstance(
            self.record, PlanExecutionPolicyDecisionRecord
        ):
            raise ValueError("decision result payload is invalid")
        if successful == (self.reason is not None):
            raise ValueError("decision result reason is invalid")


@dataclass(frozen=True, slots=True)
class PlanExecutionPolicyReadResult:
    status: PlanExecutionPolicyReadStatus
    record: PlanExecutionPolicyDecisionRecord | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", PlanExecutionPolicyReadStatus(self.status)
        )
        if (
            self.status is PlanExecutionPolicyReadStatus.FOUND
        ) != isinstance(self.record, PlanExecutionPolicyDecisionRecord):
            raise ValueError("execution policy read result is invalid")


class PlanExecutionPolicyDecisionRepository(Protocol):
    def save(
        self, record: PlanExecutionPolicyDecisionRecord
    ) -> bool: ...

    def get(
        self, *, subject_id: str, record_id: str
    ) -> PlanExecutionPolicyReadResult: ...

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> PlanExecutionPolicyReadResult: ...

    def get_invocation(
        self, *, subject_id: str, invocation_id: str
    ) -> tuple[str, str] | None: ...

    def save_invocation(
        self,
        *,
        subject_id: str,
        invocation_id: str,
        input_binding_hash: str,
        record_id: str,
    ) -> None: ...


class PrivateHomePlanExecutionPolicyDecisionRepository:
    """Subject-isolated immutable records with deterministic current reads."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    @staticmethod
    def _subject(subject_id: str) -> str:
        return hashlib.sha256(
            _clean_id("subject_id", subject_id).encode("utf-8")
        ).hexdigest()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.plan_execution_policy_decisions
            / self._subject(subject_id)
        )

    def _path(self, subject_id: str, record_id: str) -> Path:
        if _RECORD_PATTERN.fullmatch(record_id) is None:
            raise ValueError("record_id is invalid")
        return self._directory(subject_id) / f"{record_id}.json"

    def save(self, record: PlanExecutionPolicyDecisionRecord) -> bool:
        if not isinstance(record, PlanExecutionPolicyDecisionRecord):
            raise TypeError("record must be typed")
        encoded = (
            json.dumps(
                record.to_dict(), sort_keys=True, ensure_ascii=False, indent=2
            )
            + "\n"
        ).encode("utf-8")
        path = self._path(record.subject_id, record.record_id)
        with self._lock:
            self._home.ensure()
            created = self._home.write_bytes_if_absent(path, encoded)
            if not created:
                read = self.get(
                    subject_id=record.subject_id, record_id=record.record_id
                )
                if read.record != record:
                    raise RuntimeError("execution policy record conflicts")
            return created

    def get(
        self, *, subject_id: str, record_id: str
    ) -> PlanExecutionPolicyReadResult:
        subject = _clean_id("subject_id", subject_id)
        directory = self._directory(subject)
        if directory.exists() and (
            directory.is_symlink() or not directory.is_dir()
        ):
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.INTEGRITY_FAILURE
            )
        path = self._path(subject, record_id)
        if not path.exists():
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.NOT_FOUND
            )
        if path.is_symlink() or not path.is_file():
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.INTEGRITY_FAILURE
            )
        try:
            record = _record_from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.INTEGRITY_FAILURE
            )
        if record.subject_id != subject or record.record_id != record_id:
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.INTEGRITY_FAILURE
            )
        return PlanExecutionPolicyReadResult(
            PlanExecutionPolicyReadStatus.FOUND, record
        )

    def get_current(
        self, *, subject_id: str, application_plan_id: str
    ) -> PlanExecutionPolicyReadResult:
        subject = _clean_id("subject_id", subject_id)
        plan_id = _clean_id("application_plan_id", application_plan_id)
        directory = self._directory(subject)
        if not directory.exists():
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.NOT_FOUND
            )
        if directory.is_symlink() or not directory.is_dir():
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.INTEGRITY_FAILURE
            )
        records: list[PlanExecutionPolicyDecisionRecord] = []
        try:
            paths = tuple(sorted(directory.glob("*.json")))
        except OSError:
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.INTEGRITY_FAILURE
            )
        for path in paths:
            read = self.get(subject_id=subject, record_id=path.stem)
            if (
                read.status is not PlanExecutionPolicyReadStatus.FOUND
                or read.record is None
            ):
                return PlanExecutionPolicyReadResult(
                    PlanExecutionPolicyReadStatus.INTEGRITY_FAILURE
                )
            if read.record.application_plan_id == plan_id:
                records.append(read.record)
        if not records:
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.NOT_FOUND
            )
        if len(records) != 1:
            return PlanExecutionPolicyReadResult(
                PlanExecutionPolicyReadStatus.CONFLICT
            )
        return PlanExecutionPolicyReadResult(
            PlanExecutionPolicyReadStatus.FOUND, records[0]
        )

    def _invocation_path(self, subject_id: str, invocation_id: str) -> Path:
        digest = hashlib.sha256(
            _clean_id("invocation_id", invocation_id).encode("utf-8")
        ).hexdigest()
        return self._directory(subject_id) / "_invocations" / f"{digest}.json"

    def get_invocation(
        self, *, subject_id: str, invocation_id: str
    ) -> tuple[str, str] | None:
        directory = self._directory(subject_id)
        if directory.exists() and (
            directory.is_symlink() or not directory.is_dir()
        ):
            raise RuntimeError("execution policy invocation is invalid")
        path = self._invocation_path(subject_id, invocation_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("execution policy invocation is invalid")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("execution policy invocation is invalid") from exc
        if (
            not isinstance(value, Mapping)
            or set(value) != {"input_binding_hash", "record_id"}
        ):
            raise RuntimeError("execution policy invocation is invalid")
        return (
            _hash("input_binding_hash", value["input_binding_hash"]),
            _clean_id("record_id", value["record_id"]),
        )

    def save_invocation(
        self,
        *,
        subject_id: str,
        invocation_id: str,
        input_binding_hash: str,
        record_id: str,
    ) -> None:
        value = {
            "input_binding_hash": _hash(
                "input_binding_hash", input_binding_hash
            ),
            "record_id": _clean_id("record_id", record_id),
        }
        encoded = (
            json.dumps(value, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        path = self._invocation_path(subject_id, invocation_id)
        self._home.ensure()
        created = self._home.write_bytes_if_absent(path, encoded)
        if not created and self.get_invocation(
            subject_id=subject_id, invocation_id=invocation_id
        ) != (input_binding_hash, record_id):
            raise RuntimeError("execution policy invocation conflicts")


class ApplicationPlanProvider(Protocol):
    def get(self, plan_id: str) -> Any: ...


class JobPostingProvider(Protocol):
    def get(self, job_id: str) -> JobPosting | None: ...


class AcceptedIntentProvider(Protocol):
    def get_by_id(
        self, *, subject_id: str, job_id: str, accepted_job_intent_id: str
    ) -> Any: ...


class PriorityDecisionProvider(Protocol):
    def get_decision(
        self, *, subject_id: str, job_id: str, decision_id: str
    ) -> PriorityDecision | None: ...


class PrioritizationPolicyProvider(Protocol):
    def get_policy(
        self, subject_id: str, policy_id: str
    ) -> PrioritizationPolicy | None: ...


def _failure(
    status: DecidePlanExecutionPolicyStatus,
    reason: PlanExecutionPolicyFailureReason,
) -> DecidePlanExecutionPolicyResult:
    return DecidePlanExecutionPolicyResult(status=status, reason=reason)


def decide_plan_execution_policy(
    command: DecidePlanExecutionPolicyCommand,
    *,
    plan_provider: ApplicationPlanProvider,
    job_provider: JobPostingProvider,
    accepted_intent_provider: AcceptedIntentProvider,
    priority_decision_provider: PriorityDecisionProvider,
    prioritization_policy_provider: PrioritizationPolicyProvider,
    execution_rules: PlanExecutionPolicyRulesV1,
    repository: PlanExecutionPolicyDecisionRepository,
) -> DecidePlanExecutionPolicyResult:
    """Create one exact Plan-bound execution decision, fail closed."""

    if not isinstance(command, DecidePlanExecutionPolicyCommand):
        raise TypeError("command must be typed")
    if not isinstance(execution_rules, PlanExecutionPolicyRulesV1):
        raise TypeError("execution_rules must be typed")
    if not execution_rules.configuration.authority_configured:
        return _failure(
            DecidePlanExecutionPolicyStatus.NOT_READY,
            PlanExecutionPolicyFailureReason.AUTHORITY_CONFIGURATION_REQUIRED,
        )
    try:
        plan_read = plan_provider.get(command.application_plan_id)
        if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
            return _failure(
                DecidePlanExecutionPolicyStatus.NOT_READY,
                PlanExecutionPolicyFailureReason.PLAN_NOT_FOUND,
            )
        if (
            plan_read.status is not ApplicationPlanReadStatus.FOUND
            or not isinstance(plan_read.plan, ApplicationPlan)
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        plan = plan_read.plan
        if (
            plan.subject_id != command.subject_id
            or plan.plan_id != command.application_plan_id
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        job = job_provider.get(plan.job_id)
        if job is None:
            return _failure(
                DecidePlanExecutionPolicyStatus.NOT_READY,
                PlanExecutionPolicyFailureReason.JOB_NOT_FOUND,
            )
        if not isinstance(job, JobPosting):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        if (
            job.job_id != plan.job_id
            or job.revision != plan.job_revision
            or job.content_hash != plan.job_content_hash
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        intent_read = accepted_intent_provider.get_by_id(
            subject_id=command.subject_id,
            job_id=plan.job_id,
            accepted_job_intent_id=plan.accepted_job_intent_id,
        )
        if intent_read.status is AcceptedJobIntentReadStatus.NOT_FOUND:
            return _failure(
                DecidePlanExecutionPolicyStatus.NOT_READY,
                PlanExecutionPolicyFailureReason.INTENT_NOT_FOUND,
            )
        if (
            intent_read.status is not AcceptedJobIntentReadStatus.FOUND
            or not isinstance(intent_read.intent, AcceptedJobIntent)
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        intent = intent_read.intent
        if (
            intent.subject_id != command.subject_id
            or intent.job_id != plan.job_id
            or intent.accepted_job_intent_id
            != plan.accepted_job_intent_id
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        if intent.intent is not JobIntakeIntent.REQUEST_APPLICATION:
            return _failure(
                DecidePlanExecutionPolicyStatus.NOT_READY,
                PlanExecutionPolicyFailureReason.INTENT_NOT_RUNNABLE,
            )
        priority = priority_decision_provider.get_decision(
            subject_id=command.subject_id,
            job_id=plan.job_id,
            decision_id=plan.priority_decision_id,
        )
        if priority is None:
            return _failure(
                DecidePlanExecutionPolicyStatus.NOT_READY,
                PlanExecutionPolicyFailureReason.PRIORITY_DECISION_NOT_FOUND,
            )
        if not isinstance(priority, PriorityDecision):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        if (
            priority.subject_id != command.subject_id
            or priority.job_id != plan.job_id
            or priority.job_revision != plan.job_revision
            or priority.job_content_hash != plan.job_content_hash
            or priority.decision_id != plan.priority_decision_id
            or priority.policy_id != plan.policy_id
            or priority.policy_version != plan.policy_version
            or priority.policy_content_hash != plan.policy_content_hash
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        if (
            priority.qualification is not PriorityQualification.QUALIFIED
            or priority.priority_level != plan.priority_level
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.NOT_READY,
                PlanExecutionPolicyFailureReason.PRIORITY_NOT_RUNNABLE,
            )
        prioritization_policy = prioritization_policy_provider.get_policy(
            command.subject_id, plan.policy_id
        )
        if prioritization_policy is None:
            return _failure(
                DecidePlanExecutionPolicyStatus.NOT_READY,
                PlanExecutionPolicyFailureReason.PRIORITIZATION_POLICY_NOT_FOUND,
            )
        if not isinstance(prioritization_policy, PrioritizationPolicy):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        if (
            prioritization_policy.subject_id != command.subject_id
            or prioritization_policy.policy_id != plan.policy_id
            or prioritization_policy.policy_version != plan.policy_version
            or prioritization_policy.policy_content_hash
            != plan.policy_content_hash
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )

        plan_hash = plan_execution_policy_plan_binding_hash(plan)
        intent_hash = _digest(intent.to_dict())
        priority_hash = _digest(priority.to_dict())
        binding = {
            "accepted_intent_hash": intent_hash,
            "accepted_intent_id": intent.accepted_job_intent_id,
            "execution_configuration_hash": (
                execution_rules.configuration.configuration_hash
            ),
            "execution_rules_version": execution_rules.rules_version,
            "job_content_hash": job.content_hash,
            "job_id": job.job_id,
            "job_revision": job.revision,
            "plan_binding_hash": plan_hash,
            "priority_decision_hash": priority_hash,
            "priority_decision_id": priority.decision_id,
            "prioritization_policy_hash": (
                prioritization_policy.policy_content_hash
            ),
            "prioritization_policy_id": prioritization_policy.policy_id,
            "prioritization_policy_version": (
                prioritization_policy.policy_version
            ),
            "record_contract_version": (
                PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
            ),
            "subject_id": command.subject_id,
        }
        input_hash = _digest(binding)
        prior_invocation = repository.get_invocation(
            subject_id=command.subject_id,
            invocation_id=command.invocation_id,
        )
        if prior_invocation is not None:
            if prior_invocation[0] != input_hash:
                return _failure(
                    DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                    PlanExecutionPolicyFailureReason.INVOCATION_CONFLICT,
                )
            prior = repository.get(
                subject_id=command.subject_id,
                record_id=prior_invocation[1],
            )
            if (
                prior.status is PlanExecutionPolicyReadStatus.FOUND
                and prior.record is not None
            ):
                return DecidePlanExecutionPolicyResult(
                    DecidePlanExecutionPolicyStatus.UNCHANGED,
                    record=prior.record,
                )
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        try:
            decision = execution_rules.decide(
                plan=plan,
                job=job,
                intent=intent,
                priority=priority,
                prioritization_policy=prioritization_policy,
            )
        except UnsupportedExecutionPolicy:
            return _failure(
                DecidePlanExecutionPolicyStatus.UNSUPPORTED_POLICY,
                PlanExecutionPolicyFailureReason.PRIORITY_LEVEL_UNSUPPORTED,
            )
        decision_hash = _digest(policy_decision_to_dict(decision))
        record_id = (
            f"plan-execution-policy-{_digest({
                'input_binding_hash': input_hash,
                'policy_decision_hash': decision_hash,
                'record_contract_version':
                    PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION,
            })}"
        )
        existing_record = repository.get(
            subject_id=command.subject_id,
            record_id=record_id,
        )
        if existing_record.status is PlanExecutionPolicyReadStatus.FOUND:
            assert existing_record.record is not None
            if (
                existing_record.record.input_binding_hash != input_hash
                or existing_record.record.policy_decision_hash != decision_hash
            ):
                return _failure(
                    DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                    PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
                )
            repository.save_invocation(
                subject_id=command.subject_id,
                invocation_id=command.invocation_id,
                input_binding_hash=input_hash,
                record_id=record_id,
            )
            return DecidePlanExecutionPolicyResult(
                DecidePlanExecutionPolicyStatus.UNCHANGED,
                record=existing_record.record,
            )
        if (
            existing_record.status
            is not PlanExecutionPolicyReadStatus.NOT_FOUND
        ):
            return _failure(
                DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE,
                PlanExecutionPolicyFailureReason.LINEAGE_MISMATCH,
            )
        record = PlanExecutionPolicyDecisionRecord(
            record_id=record_id,
            subject_id=command.subject_id,
            application_plan_id=plan.plan_id,
            job_id=job.job_id,
            accepted_intent_id=intent.accepted_job_intent_id,
            accepted_intent_hash=intent_hash,
            priority_decision_id=priority.decision_id,
            priority_decision_hash=priority_hash,
            prioritization_policy_id=prioritization_policy.policy_id,
            prioritization_policy_version=(
                prioritization_policy.policy_version
            ),
            prioritization_policy_hash=(
                prioritization_policy.policy_content_hash
            ),
            plan_binding_hash=plan_hash,
            execution_rules_version=execution_rules.rules_version,
            execution_configuration_id=(
                execution_rules.configuration.configuration_id
            ),
            execution_configuration_version=(
                execution_rules.configuration.configuration_version
            ),
            execution_configuration_hash=(
                execution_rules.configuration.configuration_hash
            ),
            policy_decision=decision,
            policy_decision_hash=decision_hash,
            input_binding_hash=input_hash,
            created_at=command.now,
            invocation_id=command.invocation_id,
        )
        created = repository.save(record)
        repository.save_invocation(
            subject_id=command.subject_id,
            invocation_id=command.invocation_id,
            input_binding_hash=input_hash,
            record_id=record.record_id,
        )
        return DecidePlanExecutionPolicyResult(
            DecidePlanExecutionPolicyStatus.CREATED
            if created
            else DecidePlanExecutionPolicyStatus.UNCHANGED,
            record=record,
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            DecidePlanExecutionPolicyStatus.FAILED,
            PlanExecutionPolicyFailureReason.PERSISTENCE_FAILED,
        )


def get_plan_execution_policy_decision(
    *,
    subject_id: str,
    record_id: str,
    repository: PlanExecutionPolicyDecisionRepository,
) -> PlanExecutionPolicyReadResult:
    return repository.get(subject_id=subject_id, record_id=record_id)


def get_current_plan_execution_policy_decision(
    *,
    subject_id: str,
    application_plan_id: str,
    repository: PlanExecutionPolicyDecisionRepository,
) -> PlanExecutionPolicyReadResult:
    return repository.get_current(
        subject_id=subject_id,
        application_plan_id=application_plan_id,
    )


__all__ = [
    "DecidePlanExecutionPolicyCommand",
    "DecidePlanExecutionPolicyResult",
    "DecidePlanExecutionPolicyStatus",
    "PLAN_EXECUTION_POLICY_CONFIGURATION_CONTRACT_VERSION",
    "PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION",
    "PLAN_EXECUTION_POLICY_RULES_VERSION",
    "PlanExecutionPolicyConfiguration",
    "PlanExecutionPolicyDecisionRecord",
    "PlanExecutionPolicyFailureReason",
    "PlanExecutionPolicyReadResult",
    "PlanExecutionPolicyReadStatus",
    "PlanExecutionPolicyRulesV1",
    "PrivateHomePlanExecutionPolicyDecisionRepository",
    "decide_plan_execution_policy",
    "get_current_plan_execution_policy_decision",
    "get_plan_execution_policy_decision",
    "plan_execution_policy_plan_binding_hash",
    "plan_execution_policy_record_hash",
    "policy_decision_to_dict",
]
