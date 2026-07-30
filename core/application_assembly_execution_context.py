"""Pure P2c1 handoff for exact verified profile and execution policy."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .application_execution_profile import ApplicationExecutionIdentityProfile
from .application_plan import ApplicationPlan
from .plan_execution_policy import (
    PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION,
    PlanExecutionPolicyDecisionRecord,
    PlanExecutionPolicyReadResult,
    PlanExecutionPolicyReadStatus,
    plan_execution_policy_plan_binding_hash,
    plan_execution_policy_record_hash,
)
from .policy import PolicyDecision
from .verified_application_execution_profile import (
    VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION,
    VerifiedApplicationExecutionProfile,
    VerifiedApplicationExecutionProfileReadResult,
    VerifiedApplicationExecutionProfileReadStatus,
    to_application_execution_identity_profile,
    verified_execution_profile_plan_binding_hash,
)


APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION = (
    "application-assembly-execution-context-v1"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class LoadApplicationAssemblyExecutionContextStatus(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"
    CONFLICT = "CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class ApplicationAssemblyExecutionContextFailureReason(StrEnum):
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    POLICY_NOT_FOUND = "POLICY_NOT_FOUND"
    PROFILE_CONFLICT = "PROFILE_CONFLICT"
    POLICY_CONFLICT = "POLICY_CONFLICT"
    PROFILE_INTEGRITY_FAILURE = "PROFILE_INTEGRITY_FAILURE"
    POLICY_INTEGRITY_FAILURE = "POLICY_INTEGRITY_FAILURE"
    CROSS_BINDING_MISMATCH = "CROSS_BINDING_MISMATCH"
    UNSUPPORTED_CONTRACT_VERSION = "UNSUPPORTED_CONTRACT_VERSION"
    PROVIDER_FAILED = "PROVIDER_FAILED"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _clean_id(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the context contract")
    return cleaned


def _clean_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class LoadApplicationAssemblyExecutionContextCommand:
    subject_id: str
    application_plan: ApplicationPlan
    job_id: str
    verified_profile_id: str
    verified_profile_hash: str
    execution_policy_record_id: str
    execution_policy_record_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean_id("subject_id", self.subject_id)
        )
        if not isinstance(self.application_plan, ApplicationPlan):
            raise TypeError("application_plan must be typed")
        object.__setattr__(self, "job_id", _clean_id("job_id", self.job_id))
        object.__setattr__(
            self,
            "verified_profile_id",
            _clean_id("verified_profile_id", self.verified_profile_id),
        )
        object.__setattr__(
            self,
            "verified_profile_hash",
            _clean_hash("verified_profile_hash", self.verified_profile_hash),
        )
        object.__setattr__(
            self,
            "execution_policy_record_id",
            _clean_id(
                "execution_policy_record_id",
                self.execution_policy_record_id,
            ),
        )
        object.__setattr__(
            self,
            "execution_policy_record_hash",
            _clean_hash(
                "execution_policy_record_hash",
                self.execution_policy_record_hash,
            ),
        )


@dataclass(frozen=True, slots=True)
class ApplicationAssemblyExecutionContext:
    subject_id: str
    application_plan_id: str
    job_id: str
    verified_profile_id: str
    verified_profile_version: str
    verified_profile_hash: str
    execution_policy_record_id: str
    execution_policy_record_version: str
    execution_policy_record_hash: str
    identity_profile: ApplicationExecutionIdentityProfile = field(repr=False)
    policy_decision: PolicyDecision = field(repr=False)
    verified_profile: VerifiedApplicationExecutionProfile = field(repr=False)
    execution_policy_record: PlanExecutionPolicyDecisionRecord = field(
        repr=False
    )
    context_binding_hash: str = ""
    context_contract_version: str = (
        APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "verified_profile_id",
            "verified_profile_version",
            "execution_policy_record_id",
            "execution_policy_record_version",
        ):
            object.__setattr__(
                self, name, _clean_id(name, getattr(self, name))
            )
        for name in (
            "verified_profile_hash",
            "execution_policy_record_hash",
            "context_binding_hash",
        ):
            _clean_hash(name, getattr(self, name))
        if self.context_contract_version != (
            APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION
        ):
            raise ValueError("execution context contract is unsupported")
        if not isinstance(
            self.identity_profile, ApplicationExecutionIdentityProfile
        ):
            raise TypeError("identity_profile must use the closed profile type")
        if not isinstance(self.policy_decision, PolicyDecision):
            raise TypeError("policy_decision must use core.policy.PolicyDecision")
        if not isinstance(
            self.verified_profile, VerifiedApplicationExecutionProfile
        ):
            raise TypeError("verified_profile lineage must be typed")
        if not isinstance(
            self.execution_policy_record,
            PlanExecutionPolicyDecisionRecord,
        ):
            raise TypeError("execution_policy_record lineage must be typed")
        if self.context_binding_hash != _hash(self.binding_dict()):
            raise ValueError("execution context binding hash is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "context_contract_version": self.context_contract_version,
            "execution_policy_record_hash": (
                self.execution_policy_record_hash
            ),
            "execution_policy_record_id": self.execution_policy_record_id,
            "execution_policy_record_version": (
                self.execution_policy_record_version
            ),
            "job_id": self.job_id,
            "subject_id": self.subject_id,
            "verified_profile_hash": self.verified_profile_hash,
            "verified_profile_id": self.verified_profile_id,
            "verified_profile_version": self.verified_profile_version,
        }


@dataclass(frozen=True, slots=True)
class LoadApplicationAssemblyExecutionContextResult:
    status: LoadApplicationAssemblyExecutionContextStatus
    context: ApplicationAssemblyExecutionContext | None = field(
        default=None, repr=False
    )
    failure_reason: (
        ApplicationAssemblyExecutionContextFailureReason | None
    ) = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            LoadApplicationAssemblyExecutionContextStatus(self.status),
        )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                ApplicationAssemblyExecutionContextFailureReason(
                    self.failure_reason
                ),
            )
        ready = (
            self.status
            is LoadApplicationAssemblyExecutionContextStatus.READY
        )
        if ready != isinstance(
            self.context, ApplicationAssemblyExecutionContext
        ):
            raise ValueError("execution context result payload is invalid")
        if ready == (self.failure_reason is not None):
            raise ValueError("execution context result reason is invalid")


class VerifiedExecutionProfileProvider(Protocol):
    def get(
        self, subject_id: str, profile_snapshot_id: str
    ) -> VerifiedApplicationExecutionProfileReadResult: ...


class PlanExecutionPolicyProvider(Protocol):
    def get(
        self, *, subject_id: str, record_id: str
    ) -> PlanExecutionPolicyReadResult: ...


def _result(
    status: LoadApplicationAssemblyExecutionContextStatus,
    reason: ApplicationAssemblyExecutionContextFailureReason,
) -> LoadApplicationAssemblyExecutionContextResult:
    return LoadApplicationAssemblyExecutionContextResult(
        status=status, failure_reason=reason
    )


def load_application_assembly_execution_context(
    command: LoadApplicationAssemblyExecutionContextCommand,
    *,
    verified_profile_provider: VerifiedExecutionProfileProvider,
    execution_policy_provider: PlanExecutionPolicyProvider,
) -> LoadApplicationAssemblyExecutionContextResult:
    """Load exact public records and validate their shared Plan binding."""

    if not isinstance(
        command, LoadApplicationAssemblyExecutionContextCommand
    ):
        raise TypeError("command must be typed")
    plan = command.application_plan
    if (
        plan.subject_id != command.subject_id
        or plan.job_id != command.job_id
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
            ApplicationAssemblyExecutionContextFailureReason
            .CROSS_BINDING_MISMATCH,
        )
    try:
        profile_read = verified_profile_provider.get(
            command.subject_id, command.verified_profile_id
        )
    except Exception:
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.FAILED,
            ApplicationAssemblyExecutionContextFailureReason.PROVIDER_FAILED,
        )
    if (
        profile_read.status
        is VerifiedApplicationExecutionProfileReadStatus.NOT_FOUND
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.NOT_READY,
            ApplicationAssemblyExecutionContextFailureReason.PROFILE_NOT_FOUND,
        )
    if (
        profile_read.status
        is VerifiedApplicationExecutionProfileReadStatus.CONFLICT
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.CONFLICT,
            ApplicationAssemblyExecutionContextFailureReason.PROFILE_CONFLICT,
        )
    if (
        profile_read.status
        is not VerifiedApplicationExecutionProfileReadStatus.FOUND
        or profile_read.snapshot is None
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
            ApplicationAssemblyExecutionContextFailureReason
            .PROFILE_INTEGRITY_FAILURE,
        )
    profile = profile_read.snapshot
    if (
        profile.profile_contract_version
        != VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
            ApplicationAssemblyExecutionContextFailureReason
            .UNSUPPORTED_CONTRACT_VERSION,
        )
    if (
        profile.subject_id != command.subject_id
        or profile.application_plan_id != plan.plan_id
        or profile.job_id != command.job_id
        or profile.profile_snapshot_id != command.verified_profile_id
        or profile.profile_snapshot_hash != command.verified_profile_hash
        or profile.plan_binding_hash
        != verified_execution_profile_plan_binding_hash(plan)
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
            ApplicationAssemblyExecutionContextFailureReason
            .CROSS_BINDING_MISMATCH,
        )

    try:
        policy_read = execution_policy_provider.get(
            subject_id=command.subject_id,
            record_id=command.execution_policy_record_id,
        )
    except Exception:
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.FAILED,
            ApplicationAssemblyExecutionContextFailureReason.PROVIDER_FAILED,
        )
    if policy_read.status is PlanExecutionPolicyReadStatus.NOT_FOUND:
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.NOT_READY,
            ApplicationAssemblyExecutionContextFailureReason.POLICY_NOT_FOUND,
        )
    if policy_read.status is PlanExecutionPolicyReadStatus.CONFLICT:
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.CONFLICT,
            ApplicationAssemblyExecutionContextFailureReason.POLICY_CONFLICT,
        )
    if (
        policy_read.status is not PlanExecutionPolicyReadStatus.FOUND
        or policy_read.record is None
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
            ApplicationAssemblyExecutionContextFailureReason
            .POLICY_INTEGRITY_FAILURE,
        )
    policy = policy_read.record
    if (
        policy.record_contract_version
        != PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
            ApplicationAssemblyExecutionContextFailureReason
            .UNSUPPORTED_CONTRACT_VERSION,
        )
    if (
        policy.subject_id != command.subject_id
        or policy.application_plan_id != plan.plan_id
        or policy.job_id != command.job_id
        or policy.record_id != command.execution_policy_record_id
        or plan_execution_policy_record_hash(policy)
        != command.execution_policy_record_hash
        or policy.plan_binding_hash
        != plan_execution_policy_plan_binding_hash(plan)
        or not isinstance(policy.policy_decision, PolicyDecision)
    ):
        return _result(
            LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
            ApplicationAssemblyExecutionContextFailureReason
            .CROSS_BINDING_MISMATCH,
        )

    identity_profile = to_application_execution_identity_profile(profile)
    binding = {
        "application_plan_id": plan.plan_id,
        "context_contract_version": (
            APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION
        ),
        "execution_policy_record_hash": (
            command.execution_policy_record_hash
        ),
        "execution_policy_record_id": policy.record_id,
        "execution_policy_record_version": policy.record_contract_version,
        "job_id": command.job_id,
        "subject_id": command.subject_id,
        "verified_profile_hash": profile.profile_snapshot_hash,
        "verified_profile_id": profile.profile_snapshot_id,
        "verified_profile_version": profile.profile_contract_version,
    }
    context = ApplicationAssemblyExecutionContext(
        subject_id=command.subject_id,
        application_plan_id=plan.plan_id,
        job_id=command.job_id,
        verified_profile_id=profile.profile_snapshot_id,
        verified_profile_version=profile.profile_contract_version,
        verified_profile_hash=profile.profile_snapshot_hash,
        execution_policy_record_id=policy.record_id,
        execution_policy_record_version=policy.record_contract_version,
        execution_policy_record_hash=command.execution_policy_record_hash,
        identity_profile=identity_profile,
        policy_decision=policy.policy_decision,
        verified_profile=profile,
        execution_policy_record=policy,
        context_binding_hash=_hash(binding),
    )
    return LoadApplicationAssemblyExecutionContextResult(
        LoadApplicationAssemblyExecutionContextStatus.READY,
        context=context,
    )


__all__ = [
    "APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION",
    "ApplicationAssemblyExecutionContext",
    "ApplicationAssemblyExecutionContextFailureReason",
    "LoadApplicationAssemblyExecutionContextCommand",
    "LoadApplicationAssemblyExecutionContextResult",
    "LoadApplicationAssemblyExecutionContextStatus",
    "PlanExecutionPolicyProvider",
    "VerifiedExecutionProfileProvider",
    "load_application_assembly_execution_context",
]
