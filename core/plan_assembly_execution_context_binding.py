"""Exact P2c10b1 execution-context binding before selective assembly."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Awaitable, Mapping, Protocol

from .application_assembly_execution_context import (
    ApplicationAssemblyExecutionContext,
    LoadApplicationAssemblyExecutionContextCommand,
    LoadApplicationAssemblyExecutionContextResult,
    LoadApplicationAssemblyExecutionContextStatus,
)
from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .application_preparation_orchestrator import PreparationAssemblyLineage
from .plan_execution_policy import (
    DecidePlanExecutionPolicyCommand,
    DecidePlanExecutionPolicyResult,
    DecidePlanExecutionPolicyStatus,
    PlanExecutionPolicyDecisionRecord,
    plan_execution_policy_record_hash,
)
from .private_home import PrivateHome
from .verified_application_execution_profile import (
    ProjectVerifiedApplicationExecutionProfileCommand,
    ProjectVerifiedApplicationExecutionProfileResult,
    ProjectVerifiedApplicationExecutionProfileStatus,
    VerifiedApplicationExecutionProfile,
)


PLAN_ASSEMBLY_EXECUTION_CONTEXT_BINDING_CONTRACT_VERSION = (
    "plan-assembly-execution-context-binding-v1"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,239}$")


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _clean_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _clean_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _aware("timestamp", value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware("timestamp", parsed)


def _child_invocation(invocation_id: str, purpose: str) -> str:
    digest = hashlib.sha256(
        f"{invocation_id}\0{purpose}".encode("utf-8")
    ).hexdigest()
    return f"p2c10b1-{purpose}-{digest[:32]}"


@dataclass(frozen=True, slots=True)
class VerifiedProfileRecordRef:
    record_id: str
    record_version: str
    record_hash: str

    def __post_init__(self) -> None:
        _clean_id("verified_profile_record_id", self.record_id)
        _clean_id("verified_profile_record_version", self.record_version)
        _clean_hash("verified_profile_record_hash", self.record_hash)

    def to_dict(self) -> dict[str, str]:
        return {
            "record_hash": self.record_hash,
            "record_id": self.record_id,
            "record_version": self.record_version,
        }


@dataclass(frozen=True, slots=True)
class ExecutionPolicyRecordRef:
    record_id: str
    record_version: str
    record_hash: str

    def __post_init__(self) -> None:
        _clean_id("execution_policy_record_id", self.record_id)
        _clean_id("execution_policy_record_version", self.record_version)
        _clean_hash("execution_policy_record_hash", self.record_hash)

    def to_dict(self) -> dict[str, str]:
        return {
            "record_hash": self.record_hash,
            "record_id": self.record_id,
            "record_version": self.record_version,
        }


@dataclass(frozen=True, slots=True)
class PlanAssemblyExecutionContextBinding:
    binding_id: str
    subject_id: str
    application_plan_id: str
    job_id: str
    preparation_run_id: str
    plan_material_manifest_id: str
    prepared_application_answer_set_id: str
    preparation_lineage_hash: str
    verified_profile_ref: VerifiedProfileRecordRef
    execution_policy_ref: ExecutionPolicyRecordRef
    application_assembly_context_hash: str
    profile_input_lineage_hash: str
    policy_input_lineage_hash: str
    profile_plan_binding_hash: str
    policy_plan_binding_hash: str
    created_at: datetime
    invocation_id: str
    binding_hash: str
    binding_contract_version: str = (
        PLAN_ASSEMBLY_EXECUTION_CONTEXT_BINDING_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        for name in (
            "binding_id",
            "subject_id",
            "application_plan_id",
            "job_id",
            "preparation_run_id",
            "plan_material_manifest_id",
            "prepared_application_answer_set_id",
            "invocation_id",
        ):
            _clean_id(name, getattr(self, name))
        for name in (
            "preparation_lineage_hash",
            "application_assembly_context_hash",
            "profile_input_lineage_hash",
            "policy_input_lineage_hash",
            "profile_plan_binding_hash",
            "policy_plan_binding_hash",
            "binding_hash",
        ):
            _clean_hash(name, getattr(self, name))
        if not isinstance(self.verified_profile_ref, VerifiedProfileRecordRef):
            raise TypeError("verified_profile_ref must be typed")
        if not isinstance(self.execution_policy_ref, ExecutionPolicyRecordRef):
            raise TypeError("execution_policy_ref must be typed")
        if self.binding_contract_version != (
            PLAN_ASSEMBLY_EXECUTION_CONTEXT_BINDING_CONTRACT_VERSION
        ):
            raise ValueError("binding contract version is unsupported")
        object.__setattr__(self, "created_at", _aware("created_at", self.created_at))
        expected_hash = _digest(self.identity_dict())
        if self.binding_hash != expected_hash:
            raise ValueError("binding hash is invalid")
        if self.binding_id != f"plan-assembly-context-{expected_hash}":
            raise ValueError("binding ID is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_assembly_context_hash": (
                self.application_assembly_context_hash
            ),
            "application_plan_id": self.application_plan_id,
            "binding_contract_version": self.binding_contract_version,
            "execution_policy_ref": self.execution_policy_ref.to_dict(),
            "job_id": self.job_id,
            "plan_material_manifest_id": self.plan_material_manifest_id,
            "policy_input_lineage_hash": self.policy_input_lineage_hash,
            "policy_plan_binding_hash": self.policy_plan_binding_hash,
            "preparation_lineage_hash": self.preparation_lineage_hash,
            "preparation_run_id": self.preparation_run_id,
            "prepared_application_answer_set_id": (
                self.prepared_application_answer_set_id
            ),
            "profile_input_lineage_hash": self.profile_input_lineage_hash,
            "profile_plan_binding_hash": self.profile_plan_binding_hash,
            "subject_id": self.subject_id,
            "verified_profile_ref": self.verified_profile_ref.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "binding_hash": self.binding_hash,
            "binding_id": self.binding_id,
            "created_at": _timestamp(self.created_at),
            "invocation_id": self.invocation_id,
        }


def _binding_from_dict(value: Any) -> PlanAssemblyExecutionContextBinding:
    if not isinstance(value, Mapping):
        raise ValueError("persisted binding is invalid")
    expected = set(
        PlanAssemblyExecutionContextBinding.__dataclass_fields__.keys()
    )
    if set(value) != expected:
        raise ValueError("persisted binding fields are invalid")
    return PlanAssemblyExecutionContextBinding(
        **{
            **dict(value),
            "created_at": _parse_timestamp(value["created_at"]),
            "verified_profile_ref": VerifiedProfileRecordRef(
                **value["verified_profile_ref"]
            ),
            "execution_policy_ref": ExecutionPolicyRecordRef(
                **value["execution_policy_ref"]
            ),
        }
    )


class BindPlanAssemblyExecutionContextStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_READY = "NOT_READY"
    CONFLICT = "CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


class BindPlanAssemblyExecutionContextFailureReason(StrEnum):
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    PLAN_INTEGRITY_FAILURE = "PLAN_INTEGRITY_FAILURE"
    PROFILE_NOT_READY = "PROFILE_NOT_READY"
    PROFILE_INTEGRITY_FAILURE = "PROFILE_INTEGRITY_FAILURE"
    PROFILE_FAILED = "PROFILE_FAILED"
    POLICY_NOT_READY = "POLICY_NOT_READY"
    POLICY_UNSUPPORTED = "POLICY_UNSUPPORTED"
    POLICY_INTEGRITY_FAILURE = "POLICY_INTEGRITY_FAILURE"
    POLICY_FAILED = "POLICY_FAILED"
    CONTEXT_NOT_READY = "CONTEXT_NOT_READY"
    CONTEXT_CONFLICT = "CONTEXT_CONFLICT"
    CONTEXT_INTEGRITY_FAILURE = "CONTEXT_INTEGRITY_FAILURE"
    CONTEXT_FAILED = "CONTEXT_FAILED"
    INVOCATION_CONFLICT = "INVOCATION_CONFLICT"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class BindPlanAssemblyExecutionContextCommand:
    subject_id: str
    application_plan_id: str
    preparation_lineage: PreparationAssemblyLineage
    invocation_id: str
    now: datetime

    def __post_init__(self) -> None:
        _clean_id("subject_id", self.subject_id)
        _clean_id("application_plan_id", self.application_plan_id)
        _clean_id("invocation_id", self.invocation_id)
        _aware("now", self.now)
        if not isinstance(self.preparation_lineage, PreparationAssemblyLineage):
            raise TypeError("preparation_lineage must be typed")
        if (
            self.preparation_lineage.subject_id != self.subject_id
            or self.preparation_lineage.application_plan_id
            != self.application_plan_id
        ):
            raise ValueError("preparation lineage binding is invalid")

    def request_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "binding_contract_version": (
                PLAN_ASSEMBLY_EXECUTION_CONTEXT_BINDING_CONTRACT_VERSION
            ),
            "preparation_lineage": self.preparation_lineage.to_dict(),
            "subject_id": self.subject_id,
        }


@dataclass(frozen=True, slots=True)
class BindPlanAssemblyExecutionContextResult:
    status: BindPlanAssemblyExecutionContextStatus
    binding: PlanAssemblyExecutionContextBinding | None = None
    failure_reason: BindPlanAssemblyExecutionContextFailureReason | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", BindPlanAssemblyExecutionContextStatus(self.status)
        )
        if self.failure_reason is not None:
            object.__setattr__(
                self,
                "failure_reason",
                BindPlanAssemblyExecutionContextFailureReason(
                    self.failure_reason
                ),
            )
        successful = self.status in {
            BindPlanAssemblyExecutionContextStatus.CREATED,
            BindPlanAssemblyExecutionContextStatus.UNCHANGED,
        }
        if successful != isinstance(
            self.binding, PlanAssemblyExecutionContextBinding
        ):
            raise ValueError("binding result payload is invalid")
        if successful == (self.failure_reason is not None):
            raise ValueError("binding result reason is invalid")


class VerifiedProfileProjector(Protocol):
    def __call__(
        self, command: ProjectVerifiedApplicationExecutionProfileCommand
    ) -> ProjectVerifiedApplicationExecutionProfileResult | Awaitable[
        ProjectVerifiedApplicationExecutionProfileResult
    ]: ...


class ExecutionPolicyDecider(Protocol):
    def __call__(
        self, command: DecidePlanExecutionPolicyCommand
    ) -> DecidePlanExecutionPolicyResult | Awaitable[
        DecidePlanExecutionPolicyResult
    ]: ...


class ExecutionContextLoader(Protocol):
    def __call__(
        self, command: LoadApplicationAssemblyExecutionContextCommand
    ) -> LoadApplicationAssemblyExecutionContextResult | Awaitable[
        LoadApplicationAssemblyExecutionContextResult
    ]: ...


class PlanAssemblyExecutionContextBindingRepository(Protocol):
    def save(self, binding: PlanAssemblyExecutionContextBinding) -> bool: ...

    def get(
        self, *, subject_id: str, binding_id: str
    ) -> PlanAssemblyExecutionContextBinding | None: ...

    def get_invocation(
        self, *, subject_id: str, invocation_id: str
    ) -> tuple[str, str] | None: ...

    def save_invocation(
        self,
        *,
        subject_id: str,
        invocation_id: str,
        request_hash: str,
        binding_id: str,
    ) -> None: ...


class PrivateHomePlanAssemblyExecutionContextBindingRepository:
    """Immutable subject-scoped bindings plus recoverable invocation receipts."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    @staticmethod
    def _subject(subject_id: str) -> str:
        return hashlib.sha256(_clean_id("subject_id", subject_id).encode()).hexdigest()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.plan_assembly_execution_context_bindings
            / self._subject(subject_id)
        )

    def _path(self, subject_id: str, binding_id: str) -> Path:
        _clean_id("binding_id", binding_id)
        return self._directory(subject_id) / f"{binding_id}.json"

    def save(self, binding: PlanAssemblyExecutionContextBinding) -> bool:
        if not isinstance(binding, PlanAssemblyExecutionContextBinding):
            raise TypeError("binding must be typed")
        encoded = (
            json.dumps(binding.to_dict(), sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        with self._lock:
            self._home.ensure()
            created = self._home.write_bytes_if_absent(
                self._path(binding.subject_id, binding.binding_id), encoded
            )
            if not created and self.get(
                subject_id=binding.subject_id, binding_id=binding.binding_id
            ) != binding:
                raise RuntimeError("execution context binding conflicts")
            return created

    def get(
        self, *, subject_id: str, binding_id: str
    ) -> PlanAssemblyExecutionContextBinding | None:
        directory = self._directory(subject_id)
        if directory.exists() and (
            directory.is_symlink() or not directory.is_dir()
        ):
            raise RuntimeError("execution context binding is invalid")
        path = self._path(subject_id, binding_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("execution context binding is invalid")
        try:
            binding = _binding_from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("execution context binding is invalid") from exc
        if (
            binding.subject_id != subject_id
            or binding.binding_id != binding_id
        ):
            raise RuntimeError("execution context binding is invalid")
        return binding

    def _invocation_path(self, subject_id: str, invocation_id: str) -> Path:
        digest = hashlib.sha256(
            _clean_id("invocation_id", invocation_id).encode()
        ).hexdigest()
        return self._directory(subject_id) / "_invocations" / f"{digest}.json"

    def get_invocation(
        self, *, subject_id: str, invocation_id: str
    ) -> tuple[str, str] | None:
        directory = self._directory(subject_id)
        if directory.exists() and (
            directory.is_symlink() or not directory.is_dir()
        ):
            raise RuntimeError("binding invocation is invalid")
        path = self._invocation_path(subject_id, invocation_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("binding invocation is invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or set(value) != {
            "binding_id",
            "request_hash",
        }:
            raise RuntimeError("binding invocation is invalid")
        return (
            _clean_hash("request_hash", value["request_hash"]),
            _clean_id("binding_id", value["binding_id"]),
        )

    def save_invocation(
        self,
        *,
        subject_id: str,
        invocation_id: str,
        request_hash: str,
        binding_id: str,
    ) -> None:
        expected = (
            _clean_hash("request_hash", request_hash),
            _clean_id("binding_id", binding_id),
        )
        encoded = (
            json.dumps(
                {"binding_id": expected[1], "request_hash": expected[0]},
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        self._home.ensure()
        created = self._home.write_bytes_if_absent(
            self._invocation_path(subject_id, invocation_id), encoded
        )
        if not created and self.get_invocation(
            subject_id=subject_id, invocation_id=invocation_id
        ) != expected:
            raise RuntimeError("binding invocation conflicts")


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _failure(
    status: BindPlanAssemblyExecutionContextStatus,
    reason: BindPlanAssemblyExecutionContextFailureReason,
) -> BindPlanAssemblyExecutionContextResult:
    return BindPlanAssemblyExecutionContextResult(status, failure_reason=reason)


async def bind_plan_assembly_execution_context(
    command: BindPlanAssemblyExecutionContextCommand,
    *,
    plan_provider: ApplicationPlanRepository,
    verified_profile_projector: VerifiedProfileProjector,
    execution_policy_decider: ExecutionPolicyDecider,
    execution_context_loader: ExecutionContextLoader,
    repository: PlanAssemblyExecutionContextBindingRepository,
) -> BindPlanAssemblyExecutionContextResult:
    """Generate profile, policy, and exact P2c1d3 context in strict order."""

    if not isinstance(command, BindPlanAssemblyExecutionContextCommand):
        raise TypeError("command must be typed")
    request_hash = _digest(command.request_dict())
    try:
        replay = repository.get_invocation(
            subject_id=command.subject_id, invocation_id=command.invocation_id
        )
        if replay is not None:
            if replay[0] != request_hash:
                return _failure(
                    BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
                    BindPlanAssemblyExecutionContextFailureReason.INVOCATION_CONFLICT,
                )
            binding = repository.get(
                subject_id=command.subject_id, binding_id=replay[1]
            )
            if binding is None:
                return _failure(
                    BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
                    BindPlanAssemblyExecutionContextFailureReason.PERSISTENCE_FAILED,
                )
            return BindPlanAssemblyExecutionContextResult(
                BindPlanAssemblyExecutionContextStatus.UNCHANGED,
                binding=binding,
            )

        plan_read = plan_provider.get(command.application_plan_id)
        if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
            return _failure(
                BindPlanAssemblyExecutionContextStatus.NOT_READY,
                BindPlanAssemblyExecutionContextFailureReason.PLAN_NOT_FOUND,
            )
        if (
            plan_read.status is not ApplicationPlanReadStatus.FOUND
            or not isinstance(plan_read.plan, ApplicationPlan)
            or plan_read.plan.subject_id != command.subject_id
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.PLAN_INTEGRITY_FAILURE,
            )
        plan = plan_read.plan

        try:
            profile_result = await _resolve(
                verified_profile_projector(
                    ProjectVerifiedApplicationExecutionProfileCommand(
                        subject_id=command.subject_id,
                        application_plan_id=command.application_plan_id,
                        invocation_id=_child_invocation(
                            command.invocation_id, "profile"
                        ),
                        now=command.now,
                    )
                )
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.FAILED,
                BindPlanAssemblyExecutionContextFailureReason.PROFILE_FAILED,
            )
        if not isinstance(
            profile_result, ProjectVerifiedApplicationExecutionProfileResult
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.FAILED,
                BindPlanAssemblyExecutionContextFailureReason.PROFILE_FAILED,
            )
        if profile_result.status is (
            ProjectVerifiedApplicationExecutionProfileStatus.NOT_READY
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.NOT_READY,
                BindPlanAssemblyExecutionContextFailureReason.PROFILE_NOT_READY,
            )
        if profile_result.status is (
            ProjectVerifiedApplicationExecutionProfileStatus.INTEGRITY_FAILURE
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.PROFILE_INTEGRITY_FAILURE,
            )
        if (
            profile_result.status
            not in {
                ProjectVerifiedApplicationExecutionProfileStatus.CREATED,
                ProjectVerifiedApplicationExecutionProfileStatus.UNCHANGED,
            }
            or not isinstance(
                profile_result.snapshot, VerifiedApplicationExecutionProfile
            )
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.FAILED,
                BindPlanAssemblyExecutionContextFailureReason.PROFILE_FAILED,
            )
        profile = profile_result.snapshot

        try:
            policy_result = await _resolve(
                execution_policy_decider(
                    DecidePlanExecutionPolicyCommand(
                        subject_id=command.subject_id,
                        application_plan_id=command.application_plan_id,
                        invocation_id=_child_invocation(
                            command.invocation_id, "policy"
                        ),
                        now=command.now,
                    )
                )
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.PARTIAL_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.POLICY_FAILED,
            )
        if not isinstance(policy_result, DecidePlanExecutionPolicyResult):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.PARTIAL_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.POLICY_FAILED,
            )
        if policy_result.status is DecidePlanExecutionPolicyStatus.NOT_READY:
            return _failure(
                BindPlanAssemblyExecutionContextStatus.NOT_READY,
                BindPlanAssemblyExecutionContextFailureReason.POLICY_NOT_READY,
            )
        if policy_result.status is DecidePlanExecutionPolicyStatus.UNSUPPORTED_POLICY:
            return _failure(
                BindPlanAssemblyExecutionContextStatus.NOT_READY,
                BindPlanAssemblyExecutionContextFailureReason.POLICY_UNSUPPORTED,
            )
        if policy_result.status is DecidePlanExecutionPolicyStatus.INTEGRITY_FAILURE:
            return _failure(
                BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.POLICY_INTEGRITY_FAILURE,
            )
        if (
            policy_result.status
            not in {
                DecidePlanExecutionPolicyStatus.CREATED,
                DecidePlanExecutionPolicyStatus.UNCHANGED,
            }
            or not isinstance(
                policy_result.record, PlanExecutionPolicyDecisionRecord
            )
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.PARTIAL_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.POLICY_FAILED,
            )
        policy = policy_result.record
        policy_hash = plan_execution_policy_record_hash(policy)

        try:
            context_result = await _resolve(
                execution_context_loader(
                    LoadApplicationAssemblyExecutionContextCommand(
                        subject_id=command.subject_id,
                        application_plan=plan,
                        job_id=plan.job_id,
                        verified_profile_id=profile.profile_snapshot_id,
                        verified_profile_hash=profile.profile_snapshot_hash,
                        execution_policy_record_id=policy.record_id,
                        execution_policy_record_hash=policy_hash,
                    )
                )
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.PARTIAL_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.CONTEXT_FAILED,
            )
        if not isinstance(
            context_result, LoadApplicationAssemblyExecutionContextResult
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.PARTIAL_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.CONTEXT_FAILED,
            )
        if context_result.status is (
            LoadApplicationAssemblyExecutionContextStatus.NOT_READY
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.NOT_READY,
                BindPlanAssemblyExecutionContextFailureReason.CONTEXT_NOT_READY,
            )
        if context_result.status is (
            LoadApplicationAssemblyExecutionContextStatus.CONFLICT
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.CONFLICT,
                BindPlanAssemblyExecutionContextFailureReason.CONTEXT_CONFLICT,
            )
        if context_result.status is (
            LoadApplicationAssemblyExecutionContextStatus.INTEGRITY_FAILURE
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.INTEGRITY_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.CONTEXT_INTEGRITY_FAILURE,
            )
        if (
            context_result.status
            is not LoadApplicationAssemblyExecutionContextStatus.READY
            or not isinstance(
                context_result.context, ApplicationAssemblyExecutionContext
            )
        ):
            return _failure(
                BindPlanAssemblyExecutionContextStatus.PARTIAL_FAILURE,
                BindPlanAssemblyExecutionContextFailureReason.CONTEXT_FAILED,
            )
        context = context_result.context
        profile_ref = VerifiedProfileRecordRef(
            context.verified_profile_id,
            context.verified_profile_version,
            context.verified_profile_hash,
        )
        policy_ref = ExecutionPolicyRecordRef(
            context.execution_policy_record_id,
            context.execution_policy_record_version,
            context.execution_policy_record_hash,
        )
        identity = {
            "application_assembly_context_hash": context.context_binding_hash,
            "application_plan_id": plan.plan_id,
            "binding_contract_version": (
                PLAN_ASSEMBLY_EXECUTION_CONTEXT_BINDING_CONTRACT_VERSION
            ),
            "execution_policy_ref": policy_ref.to_dict(),
            "job_id": plan.job_id,
            "plan_material_manifest_id": (
                command.preparation_lineage.plan_material_manifest_id
            ),
            "policy_input_lineage_hash": policy.input_binding_hash,
            "policy_plan_binding_hash": policy.plan_binding_hash,
            "preparation_lineage_hash": command.preparation_lineage.lineage_hash,
            "preparation_run_id": (
                command.preparation_lineage.preparation_run_id
            ),
            "prepared_application_answer_set_id": (
                command.preparation_lineage.prepared_application_answer_set_id
            ),
            "profile_input_lineage_hash": profile.profile_snapshot_hash,
            "profile_plan_binding_hash": profile.plan_binding_hash,
            "subject_id": command.subject_id,
            "verified_profile_ref": profile_ref.to_dict(),
        }
        binding_hash = _digest(identity)
        binding = PlanAssemblyExecutionContextBinding(
            binding_id=f"plan-assembly-context-{binding_hash}",
            subject_id=command.subject_id,
            application_plan_id=plan.plan_id,
            job_id=plan.job_id,
            preparation_run_id=command.preparation_lineage.preparation_run_id,
            plan_material_manifest_id=(
                command.preparation_lineage.plan_material_manifest_id
            ),
            prepared_application_answer_set_id=(
                command.preparation_lineage.prepared_application_answer_set_id
            ),
            preparation_lineage_hash=command.preparation_lineage.lineage_hash,
            verified_profile_ref=profile_ref,
            execution_policy_ref=policy_ref,
            application_assembly_context_hash=context.context_binding_hash,
            profile_input_lineage_hash=profile.profile_snapshot_hash,
            policy_input_lineage_hash=policy.input_binding_hash,
            profile_plan_binding_hash=profile.plan_binding_hash,
            policy_plan_binding_hash=policy.plan_binding_hash,
            created_at=command.now,
            invocation_id=command.invocation_id,
            binding_hash=binding_hash,
        )
        created = repository.save(binding)
        repository.save_invocation(
            subject_id=command.subject_id,
            invocation_id=command.invocation_id,
            request_hash=request_hash,
            binding_id=binding.binding_id,
        )
        return BindPlanAssemblyExecutionContextResult(
            BindPlanAssemblyExecutionContextStatus.CREATED
            if created
            else BindPlanAssemblyExecutionContextStatus.UNCHANGED,
            binding=binding,
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return _failure(
            BindPlanAssemblyExecutionContextStatus.FAILED,
            BindPlanAssemblyExecutionContextFailureReason.PERSISTENCE_FAILED,
        )


__all__ = [
    "PLAN_ASSEMBLY_EXECUTION_CONTEXT_BINDING_CONTRACT_VERSION",
    "BindPlanAssemblyExecutionContextCommand",
    "BindPlanAssemblyExecutionContextFailureReason",
    "BindPlanAssemblyExecutionContextResult",
    "BindPlanAssemblyExecutionContextStatus",
    "ExecutionContextLoader",
    "ExecutionPolicyDecider",
    "ExecutionPolicyRecordRef",
    "PlanAssemblyExecutionContextBinding",
    "PlanAssemblyExecutionContextBindingRepository",
    "PrivateHomePlanAssemblyExecutionContextBindingRepository",
    "VerifiedProfileProjector",
    "VerifiedProfileRecordRef",
    "bind_plan_assembly_execution_context",
]
