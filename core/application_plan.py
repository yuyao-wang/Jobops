"""Immutable automation-first handoff into Application Preparation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .accepted_job_intent import AcceptedJobIntent
from .job_discovery import JobIntakeIntent, JobPosting
from .job_prioritization import (
    PriorityDecision,
    PriorityQualification,
    ProposedPriorityLevel,
)
from .prioritization_policy import (
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
)
from .private_home import PrivateHome
from .runnable_application_queue import (
    RunnableApplicationQueueCommand,
    RunnableApplicationQueueResult,
    RunnableApplicationQueueStatus,
    RunnableApplicationStatus,
)


APPLICATION_PLAN_CONTRACT_VERSION = "application-plan-v1"
_PLAN_ID_PATTERN = re.compile(r"^application-plan-[a-f0-9]{64}$")
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class ApplicationAutomationPolicy(str, Enum):
    AUTOMATION_FIRST = "AUTOMATION_FIRST"


class HumanAttentionPolicy(str, Enum):
    DEFER_ITEM_AND_CONTINUE = "DEFER_ITEM_AND_CONTINUE"


class ApplicationPlanStage(str, Enum):
    RESUME_PREPARATION = "RESUME_PREPARATION"
    COVER_LETTER = "COVER_LETTER"
    APPLICATION_ANSWERS = "APPLICATION_ANSWERS"
    FACT_QA = "FACT_QA"
    VISUAL_QA = "VISUAL_QA"
    MATERIAL_ASSEMBLY = "MATERIAL_ASSEMBLY"
    APPROVAL_GATE_A = "APPROVAL_GATE_A"


APPLICATION_PLAN_STAGES = tuple(ApplicationPlanStage)


class ApplicationPlanWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ApplicationPlanReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ApplicationPlanFailureReason(str, Enum):
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


class CreateApplicationPlanStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_RUNNABLE = "NOT_RUNNABLE"
    FAILED = "FAILED"


class CreateApplicationPlanReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    RUNNABLE_QUEUE_FAILED = "RUNNABLE_QUEUE_FAILED"
    RUNNABLE_QUEUE_RESULT_INVALID = "RUNNABLE_QUEUE_RESULT_INVALID"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_RUNNABLE = "JOB_NOT_RUNNABLE"
    PLAN_PERSISTENCE_FAILED = "PLAN_PERSISTENCE_FAILED"
    PLAN_INTEGRITY_FAILURE = "PLAN_INTEGRITY_FAILURE"


def _clean_id(name: str, value: Any, *, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the ApplicationPlan contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
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


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("created_at is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _require_aware("created_at", parsed)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _instructions(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("user_preparation_instructions must be a string")
    if not value.strip() or len(value) > 20_000:
        raise ValueError(
            "user_preparation_instructions is outside the contract"
        )
    return value


def application_plan_instruction_hash(value: str | None) -> str:
    preserved = _instructions(value)
    return hashlib.sha256(
        _canonical_json({"user_preparation_instructions": preserved})
    ).hexdigest()


def _identity_payload(
    *,
    contract_version: str,
    subject_id: str,
    job_id: str,
    job_revision: int,
    job_content_hash: str,
    priority_decision_id: str,
    policy_id: str,
    policy_version: int,
    policy_content_hash: str,
    accepted_job_intent_id: str,
    priority_level: ProposedPriorityLevel,
    automation_policy: ApplicationAutomationPolicy,
    human_attention_policy: HumanAttentionPolicy,
    user_preparation_instructions_hash: str,
    planned_stages: tuple[ApplicationPlanStage, ...],
) -> dict[str, Any]:
    return {
        "accepted_job_intent_id": accepted_job_intent_id,
        "automation_policy": automation_policy.value,
        "contract_version": contract_version,
        "human_attention_policy": human_attention_policy.value,
        "job_content_hash": job_content_hash,
        "job_id": job_id,
        "job_revision": job_revision,
        "planned_stages": [item.value for item in planned_stages],
        "policy_content_hash": policy_content_hash,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "priority_decision_id": priority_decision_id,
        "priority_level": priority_level.value,
        "subject_id": subject_id,
        "user_preparation_instructions_hash": (
            user_preparation_instructions_hash
        ),
    }


def _plan_id(**values: Any) -> str:
    digest = hashlib.sha256(
        _canonical_json(_identity_payload(**values))
    ).hexdigest()
    return f"application-plan-{digest}"


@dataclass(frozen=True, slots=True)
class ApplicationPlan:
    plan_id: str
    contract_version: str
    subject_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    priority_decision_id: str
    policy_id: str
    policy_version: int
    policy_content_hash: str
    accepted_job_intent_id: str
    priority_level: ProposedPriorityLevel
    created_at: datetime
    automation_policy: ApplicationAutomationPolicy
    human_attention_policy: HumanAttentionPolicy
    user_preparation_instructions: str | None
    user_preparation_instructions_hash: str
    planned_stages: tuple[ApplicationPlanStage, ...]

    def __post_init__(self) -> None:
        contract_version = _clean_id(
            "contract_version",
            self.contract_version,
            maximum=80,
        )
        if contract_version != APPLICATION_PLAN_CONTRACT_VERSION:
            raise ValueError("ApplicationPlan contract version is unsupported")
        subject_id = _clean_id("subject_id", self.subject_id, maximum=160)
        job_id = _clean_id("job_id", self.job_id, maximum=160)
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be a positive integer")
        job_hash = _require_hash("job_content_hash", self.job_content_hash)
        decision_id = _clean_id(
            "priority_decision_id",
            self.priority_decision_id,
        )
        policy_id = _clean_id("policy_id", self.policy_id)
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise ValueError("policy_version must be a positive integer")
        policy_hash = _require_hash(
            "policy_content_hash",
            self.policy_content_hash,
        )
        intent_id = _clean_id(
            "accepted_job_intent_id",
            self.accepted_job_intent_id,
        )
        priority = ProposedPriorityLevel(self.priority_level)
        created_at = _require_aware("created_at", self.created_at)
        automation = ApplicationAutomationPolicy(self.automation_policy)
        attention = HumanAttentionPolicy(self.human_attention_policy)
        if automation is not ApplicationAutomationPolicy.AUTOMATION_FIRST:
            raise ValueError("ApplicationPlan must be automation-first")
        if attention is not HumanAttentionPolicy.DEFER_ITEM_AND_CONTINUE:
            raise ValueError(
                "ApplicationPlan must defer only the current blocked item"
            )
        instructions = _instructions(self.user_preparation_instructions)
        instruction_hash = _require_hash(
            "user_preparation_instructions_hash",
            self.user_preparation_instructions_hash,
        )
        if instruction_hash != application_plan_instruction_hash(instructions):
            raise ValueError("user preparation instruction hash is invalid")
        if not isinstance(self.planned_stages, tuple):
            raise TypeError("planned_stages must be a tuple")
        stages = tuple(ApplicationPlanStage(item) for item in self.planned_stages)
        if stages != APPLICATION_PLAN_STAGES:
            raise ValueError("ApplicationPlan stages are invalid")

        identity = _plan_id(
            contract_version=contract_version,
            subject_id=subject_id,
            job_id=job_id,
            job_revision=self.job_revision,
            job_content_hash=job_hash,
            priority_decision_id=decision_id,
            policy_id=policy_id,
            policy_version=self.policy_version,
            policy_content_hash=policy_hash,
            accepted_job_intent_id=intent_id,
            priority_level=priority,
            automation_policy=automation,
            human_attention_policy=attention,
            user_preparation_instructions_hash=instruction_hash,
            planned_stages=stages,
        )
        if (
            not isinstance(self.plan_id, str)
            or _PLAN_ID_PATTERN.fullmatch(self.plan_id) is None
            or self.plan_id != identity
        ):
            raise ValueError("ApplicationPlan identity is invalid")

        object.__setattr__(self, "contract_version", contract_version)
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "job_content_hash", job_hash)
        object.__setattr__(self, "priority_decision_id", decision_id)
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "policy_content_hash", policy_hash)
        object.__setattr__(self, "accepted_job_intent_id", intent_id)
        object.__setattr__(self, "priority_level", priority)
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "automation_policy", automation)
        object.__setattr__(self, "human_attention_policy", attention)
        object.__setattr__(
            self,
            "user_preparation_instructions",
            instructions,
        )
        object.__setattr__(
            self,
            "user_preparation_instructions_hash",
            instruction_hash,
        )
        object.__setattr__(self, "planned_stages", stages)

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        job_id: str,
        job_revision: int,
        job_content_hash: str,
        priority_decision_id: str,
        policy_id: str,
        policy_version: int,
        policy_content_hash: str,
        accepted_job_intent_id: str,
        priority_level: ProposedPriorityLevel,
        created_at: datetime,
        user_preparation_instructions: str | None = None,
    ) -> "ApplicationPlan":
        instructions = _instructions(user_preparation_instructions)
        instruction_hash = application_plan_instruction_hash(instructions)
        values = {
            "contract_version": APPLICATION_PLAN_CONTRACT_VERSION,
            "subject_id": subject_id,
            "job_id": job_id,
            "job_revision": job_revision,
            "job_content_hash": job_content_hash,
            "priority_decision_id": priority_decision_id,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "policy_content_hash": policy_content_hash,
            "accepted_job_intent_id": accepted_job_intent_id,
            "priority_level": ProposedPriorityLevel(priority_level),
            "automation_policy": (
                ApplicationAutomationPolicy.AUTOMATION_FIRST
            ),
            "human_attention_policy": (
                HumanAttentionPolicy.DEFER_ITEM_AND_CONTINUE
            ),
            "user_preparation_instructions_hash": instruction_hash,
            "planned_stages": APPLICATION_PLAN_STAGES,
        }
        return cls(
            plan_id=_plan_id(**values),
            created_at=created_at,
            user_preparation_instructions=instructions,
            **values,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "contract_version": self.contract_version,
            "subject_id": self.subject_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "priority_decision_id": self.priority_decision_id,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_content_hash": self.policy_content_hash,
            "accepted_job_intent_id": self.accepted_job_intent_id,
            "priority_level": self.priority_level.value,
            "created_at": _rfc3339(self.created_at),
            "automation_policy": self.automation_policy.value,
            "human_attention_policy": self.human_attention_policy.value,
            "user_preparation_instructions": (
                self.user_preparation_instructions
            ),
            "user_preparation_instructions_hash": (
                self.user_preparation_instructions_hash
            ),
            "planned_stages": [item.value for item in self.planned_stages],
        }


@dataclass(frozen=True, slots=True)
class ApplicationPlanWriteResult:
    status: ApplicationPlanWriteStatus
    plan: ApplicationPlan | None
    reason_code: ApplicationPlanFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = ApplicationPlanWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ApplicationPlanFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            ApplicationPlanWriteStatus.CREATED,
            ApplicationPlanWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.plan, ApplicationPlan)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful plan write result is invalid")
        elif self.plan is not None or self.reason_code is None:
            raise ValueError("failed plan write result is invalid")


@dataclass(frozen=True, slots=True)
class ApplicationPlanReadResult:
    status: ApplicationPlanReadStatus
    plan: ApplicationPlan | None
    reason_code: ApplicationPlanFailureReason | None = None

    def __post_init__(self) -> None:
        status = ApplicationPlanReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                ApplicationPlanFailureReason(self.reason_code),
            )
        if status is ApplicationPlanReadStatus.FOUND:
            if (
                not isinstance(self.plan, ApplicationPlan)
                or self.reason_code is not None
            ):
                raise ValueError("found ApplicationPlan result is invalid")
        elif status is ApplicationPlanReadStatus.NOT_FOUND:
            if self.plan is not None or self.reason_code is not None:
                raise ValueError("not-found ApplicationPlan result is invalid")
        elif (
            self.plan is not None
            or self.reason_code
            is not ApplicationPlanFailureReason.INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure plan result is invalid")


@runtime_checkable
class ApplicationPlanRepository(Protocol):
    def save(self, plan: ApplicationPlan) -> ApplicationPlanWriteResult:
        """Persist one immutable ApplicationPlan."""

    def get(self, plan_id: str) -> ApplicationPlanReadResult:
        """Read one immutable ApplicationPlan by stable identity."""


def _plan_from_dict(value: Any) -> ApplicationPlan:
    expected = {
        "plan_id",
        "contract_version",
        "subject_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "priority_decision_id",
        "policy_id",
        "policy_version",
        "policy_content_hash",
        "accepted_job_intent_id",
        "priority_level",
        "created_at",
        "automation_policy",
        "human_attention_policy",
        "user_preparation_instructions",
        "user_preparation_instructions_hash",
        "planned_stages",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted ApplicationPlan fields are invalid")
    stages = value["planned_stages"]
    if not isinstance(stages, list):
        raise ValueError("persisted ApplicationPlan stages are invalid")
    return ApplicationPlan(
        plan_id=value["plan_id"],
        contract_version=value["contract_version"],
        subject_id=value["subject_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        priority_decision_id=value["priority_decision_id"],
        policy_id=value["policy_id"],
        policy_version=value["policy_version"],
        policy_content_hash=value["policy_content_hash"],
        accepted_job_intent_id=value["accepted_job_intent_id"],
        priority_level=ProposedPriorityLevel(value["priority_level"]),
        created_at=_parse_timestamp(value["created_at"]),
        automation_policy=ApplicationAutomationPolicy(
            value["automation_policy"]
        ),
        human_attention_policy=HumanAttentionPolicy(
            value["human_attention_policy"]
        ),
        user_preparation_instructions=value[
            "user_preparation_instructions"
        ],
        user_preparation_instructions_hash=value[
            "user_preparation_instructions_hash"
        ],
        planned_stages=tuple(ApplicationPlanStage(item) for item in stages),
    )


def _semantic_content(plan: ApplicationPlan) -> dict[str, Any]:
    value = plan.to_dict()
    value.pop("created_at")
    return value


class PrivateHomeApplicationPlanRepository:
    """Immutable, atomic ApplicationPlan records in Private Home."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, plan_id: str) -> Path:
        if (
            not isinstance(plan_id, str)
            or _PLAN_ID_PATTERN.fullmatch(plan_id) is None
        ):
            raise ValueError("plan_id is invalid")
        return self._home.paths.application_plans / f"{plan_id}.json"

    @staticmethod
    def _encoded(plan: ApplicationPlan) -> bytes:
        return (
            json.dumps(
                plan.to_dict(),
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    def get(self, plan_id: str) -> ApplicationPlanReadResult:
        path = self._path(plan_id)
        with self._lock:
            if not path.exists():
                return ApplicationPlanReadResult(
                    status=ApplicationPlanReadStatus.NOT_FOUND,
                    plan=None,
                )
            if path.is_symlink() or not path.is_file():
                return ApplicationPlanReadResult(
                    status=ApplicationPlanReadStatus.INTEGRITY_FAILURE,
                    plan=None,
                    reason_code=(
                        ApplicationPlanFailureReason.INTEGRITY_FAILURE
                    ),
                )
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                plan = _plan_from_dict(value)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return ApplicationPlanReadResult(
                    status=ApplicationPlanReadStatus.INTEGRITY_FAILURE,
                    plan=None,
                    reason_code=(
                        ApplicationPlanFailureReason.INTEGRITY_FAILURE
                    ),
                )
            if path.name != f"{plan.plan_id}.json" or plan.plan_id != plan_id:
                return ApplicationPlanReadResult(
                    status=ApplicationPlanReadStatus.INTEGRITY_FAILURE,
                    plan=None,
                    reason_code=(
                        ApplicationPlanFailureReason.INTEGRITY_FAILURE
                    ),
                )
            return ApplicationPlanReadResult(
                status=ApplicationPlanReadStatus.FOUND,
                plan=plan,
            )

    def save(self, plan: ApplicationPlan) -> ApplicationPlanWriteResult:
        if not isinstance(plan, ApplicationPlan):
            raise TypeError("plan must be an ApplicationPlan")
        path = self._path(plan.plan_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    self._encoded(plan),
                )
            except (OSError, RuntimeError):
                return ApplicationPlanWriteResult(
                    status=ApplicationPlanWriteStatus.FAILED,
                    plan=None,
                    reason_code=(
                        ApplicationPlanFailureReason.PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return ApplicationPlanWriteResult(
                    status=ApplicationPlanWriteStatus.CREATED,
                    plan=plan,
                    reason_code=None,
                    retryable=False,
                )
            existing = self.get(plan.plan_id)
            if (
                existing.status is ApplicationPlanReadStatus.FOUND
                and existing.plan is not None
                and _semantic_content(existing.plan)
                == _semantic_content(plan)
            ):
                return ApplicationPlanWriteResult(
                    status=ApplicationPlanWriteStatus.UNCHANGED,
                    plan=existing.plan,
                    reason_code=None,
                    retryable=False,
                )
            return ApplicationPlanWriteResult(
                status=ApplicationPlanWriteStatus.FAILED,
                plan=None,
                reason_code=ApplicationPlanFailureReason.INTEGRITY_FAILURE,
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class CreateApplicationPlanCommand:
    subject_id: str
    job_id: str
    now: datetime
    user_preparation_instructions: str | None = None


@dataclass(frozen=True, slots=True)
class CreateApplicationPlanResult:
    status: CreateApplicationPlanStatus
    reason_code: CreateApplicationPlanReason | None
    retryable: bool
    subject_id: str
    job_id: str
    runnable_status: RunnableApplicationStatus | None
    plan: ApplicationPlan | None
    write_result: ApplicationPlanWriteResult | None
    message: str

    def __post_init__(self) -> None:
        status = CreateApplicationPlanStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                CreateApplicationPlanReason(self.reason_code),
            )
        if self.runnable_status is not None:
            object.__setattr__(
                self,
                "runnable_status",
                RunnableApplicationStatus(self.runnable_status),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if status in {
            CreateApplicationPlanStatus.CREATED,
            CreateApplicationPlanStatus.UNCHANGED,
        }:
            expected_write = ApplicationPlanWriteStatus(status.value)
            if (
                self.reason_code is not None
                or self.retryable
                or self.runnable_status
                is not RunnableApplicationStatus.RUNNABLE
                or not isinstance(self.plan, ApplicationPlan)
                or not isinstance(
                    self.write_result,
                    ApplicationPlanWriteResult,
                )
                or self.write_result.status is not expected_write
                or self.write_result.plan != self.plan
            ):
                raise ValueError("successful ApplicationPlan result is invalid")
        elif status is CreateApplicationPlanStatus.NOT_RUNNABLE:
            if (
                self.reason_code
                not in {
                    CreateApplicationPlanReason.JOB_NOT_FOUND,
                    CreateApplicationPlanReason.JOB_NOT_RUNNABLE,
                }
                or self.retryable
                or self.plan is not None
                or self.write_result is not None
                or (
                    self.reason_code
                    is CreateApplicationPlanReason.JOB_NOT_FOUND
                    and self.runnable_status is not None
                )
                or (
                    self.reason_code
                    is CreateApplicationPlanReason.JOB_NOT_RUNNABLE
                    and (
                        self.runnable_status is None
                        or self.runnable_status
                        is RunnableApplicationStatus.RUNNABLE
                    )
                )
            ):
                raise ValueError("not-runnable ApplicationPlan result is invalid")
        elif (
            self.reason_code is None
            or self.plan is not None
            or self.runnable_status is not None
            or (
                self.write_result is not None
                and self.write_result.status
                is not ApplicationPlanWriteStatus.FAILED
            )
        ):
            raise ValueError("failed ApplicationPlan result is invalid")


_RunnableQueueReader = Callable[
    [RunnableApplicationQueueCommand],
    Awaitable[RunnableApplicationQueueResult],
]


def _create_failure(
    *,
    subject_id: str,
    job_id: str,
    reason: CreateApplicationPlanReason,
    message: str,
    retryable: bool = False,
    write_result: ApplicationPlanWriteResult | None = None,
) -> CreateApplicationPlanResult:
    return CreateApplicationPlanResult(
        status=CreateApplicationPlanStatus.FAILED,
        reason_code=reason,
        retryable=retryable,
        subject_id=subject_id,
        job_id=job_id,
        runnable_status=None,
        plan=None,
        write_result=write_result,
        message=message,
    )


def _not_runnable(
    *,
    subject_id: str,
    job_id: str,
    reason: CreateApplicationPlanReason,
    runnable_status: RunnableApplicationStatus | None,
) -> CreateApplicationPlanResult:
    return CreateApplicationPlanResult(
        status=CreateApplicationPlanStatus.NOT_RUNNABLE,
        reason_code=reason,
        retryable=False,
        subject_id=subject_id,
        job_id=job_id,
        runnable_status=runnable_status,
        plan=None,
        write_result=None,
        message=(
            "The selected job is not currently runnable for preparation."
        ),
    )


def _validate_runnable_binding(
    *,
    subject_id: str,
    job_id: str,
    queue: RunnableApplicationQueueResult,
) -> tuple[
    PriorityDecision,
    PrioritizationPolicy,
    AcceptedJobIntent,
    JobPosting,
]:
    matches = tuple(item for item in queue.items if item.job.job_id == job_id)
    if len(matches) != 1:
        raise ValueError("runnable queue job identity is ambiguous")
    item = matches[0]
    if item.runnable_status is not RunnableApplicationStatus.RUNNABLE:
        raise LookupError(item.runnable_status)
    decision = item.priority_decision
    policy = queue.policy_snapshot
    intent = item.application_intent
    job = item.job
    if (
        not isinstance(decision, PriorityDecision)
        or decision.qualification is not PriorityQualification.QUALIFIED
        or decision.priority_level is None
        or not isinstance(policy, PrioritizationPolicy)
        or policy.status is not PrioritizationPolicyStatus.ACTIVE
        or not isinstance(intent, AcceptedJobIntent)
        or intent.intent is not JobIntakeIntent.REQUEST_APPLICATION
        or item.subject_id != subject_id
        or job.job_id != job_id
        or decision.subject_id != subject_id
        or decision.job_id != job_id
        or decision.job_revision != job.revision
        or decision.job_content_hash != job.content_hash
        or policy.subject_id != subject_id
        or decision.policy_id != policy.policy_id
        or decision.policy_version != policy.policy_version
        or decision.policy_content_hash != policy.policy_content_hash
        or intent.subject_id != subject_id
        or intent.job_id != job_id
    ):
        raise ValueError("runnable queue bindings are inconsistent")
    return decision, policy, intent, job


async def create_application_plan(
    command: CreateApplicationPlanCommand,
    *,
    runnable_queue_reader: _RunnableQueueReader,
    repository: ApplicationPlanRepository,
) -> CreateApplicationPlanResult:
    """Create one immutable plan only from a P1d4 RUNNABLE item."""

    if not isinstance(command, CreateApplicationPlanCommand):
        raise TypeError("command must be a CreateApplicationPlanCommand")
    try:
        subject_id = _clean_id(
            "subject_id",
            command.subject_id,
            maximum=160,
        )
        job_id = _clean_id("job_id", command.job_id, maximum=160)
        now = _require_aware("now", command.now)
        instructions = _instructions(
            command.user_preparation_instructions
        )
    except (TypeError, ValueError) as exc:
        return _create_failure(
            subject_id=(
                command.subject_id
                if isinstance(command.subject_id, str)
                else ""
            ),
            job_id=(
                command.job_id if isinstance(command.job_id, str) else ""
            ),
            reason=CreateApplicationPlanReason.INVALID_REQUEST,
            message=str(exc),
        )

    try:
        queue = await runnable_queue_reader(
            RunnableApplicationQueueCommand(subject_id=subject_id, now=now)
        )
    except RuntimeError:
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=CreateApplicationPlanReason.RUNNABLE_QUEUE_FAILED,
            message="The runnable Application Queue could not be read.",
            retryable=True,
        )
    if not isinstance(queue, RunnableApplicationQueueResult):
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=(
                CreateApplicationPlanReason.RUNNABLE_QUEUE_RESULT_INVALID
            ),
            message="The runnable Application Queue returned invalid data.",
        )
    if queue.status is RunnableApplicationQueueStatus.FAILED:
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=CreateApplicationPlanReason.RUNNABLE_QUEUE_FAILED,
            message="The runnable Application Queue could not be built.",
            retryable=queue.retryable,
        )
    if queue.subject_id != subject_id or queue.now != now:
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=(
                CreateApplicationPlanReason.RUNNABLE_QUEUE_RESULT_INVALID
            ),
            message="The runnable Application Queue binding is invalid.",
        )

    matching = tuple(
        item for item in queue.items if item.job.job_id == job_id
    )
    if not matching:
        return _not_runnable(
            subject_id=subject_id,
            job_id=job_id,
            reason=CreateApplicationPlanReason.JOB_NOT_FOUND,
            runnable_status=None,
        )
    if len(matching) != 1:
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=(
                CreateApplicationPlanReason.RUNNABLE_QUEUE_RESULT_INVALID
            ),
            message="The runnable Application Queue contains duplicate jobs.",
        )
    if (
        matching[0].runnable_status
        is not RunnableApplicationStatus.RUNNABLE
    ):
        return _not_runnable(
            subject_id=subject_id,
            job_id=job_id,
            reason=CreateApplicationPlanReason.JOB_NOT_RUNNABLE,
            runnable_status=matching[0].runnable_status,
        )

    try:
        decision, policy, intent, job = _validate_runnable_binding(
            subject_id=subject_id,
            job_id=job_id,
            queue=queue,
        )
        plan = ApplicationPlan.create(
            subject_id=subject_id,
            job_id=job_id,
            job_revision=job.revision,
            job_content_hash=job.content_hash,
            priority_decision_id=decision.decision_id,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            policy_content_hash=policy.policy_content_hash,
            accepted_job_intent_id=intent.accepted_job_intent_id,
            priority_level=decision.priority_level,
            created_at=now,
            user_preparation_instructions=instructions,
        )
    except (AttributeError, TypeError, ValueError):
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=(
                CreateApplicationPlanReason.RUNNABLE_QUEUE_RESULT_INVALID
            ),
            message="The runnable Application Queue item is inconsistent.",
        )

    try:
        write_result = repository.save(plan)
    except (OSError, RuntimeError):
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=CreateApplicationPlanReason.PLAN_PERSISTENCE_FAILED,
            message="The ApplicationPlan could not be persisted.",
            retryable=True,
        )
    if not isinstance(write_result, ApplicationPlanWriteResult):
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=CreateApplicationPlanReason.PLAN_INTEGRITY_FAILURE,
            message="The ApplicationPlan repository returned invalid data.",
        )
    if write_result.status is ApplicationPlanWriteStatus.FAILED:
        reason = (
            CreateApplicationPlanReason.PLAN_PERSISTENCE_FAILED
            if write_result.reason_code
            is ApplicationPlanFailureReason.PERSISTENCE_FAILED
            else CreateApplicationPlanReason.PLAN_INTEGRITY_FAILURE
        )
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=reason,
            message="The ApplicationPlan repository rejected the plan.",
            retryable=write_result.retryable,
            write_result=write_result,
        )
    if (
        write_result.plan is None
        or write_result.plan.plan_id != plan.plan_id
        or _semantic_content(write_result.plan) != _semantic_content(plan)
    ):
        return _create_failure(
            subject_id=subject_id,
            job_id=job_id,
            reason=CreateApplicationPlanReason.PLAN_INTEGRITY_FAILURE,
            message="The persisted ApplicationPlan binding is invalid.",
        )
    result_status = CreateApplicationPlanStatus(write_result.status.value)
    return CreateApplicationPlanResult(
        status=result_status,
        reason_code=None,
        retryable=False,
        subject_id=subject_id,
        job_id=job_id,
        runnable_status=RunnableApplicationStatus.RUNNABLE,
        plan=write_result.plan,
        write_result=write_result,
        message=(
            "The automation-first ApplicationPlan was created."
            if result_status is CreateApplicationPlanStatus.CREATED
            else "The existing automation-first ApplicationPlan was reused."
        ),
    )


__all__ = [
    "APPLICATION_PLAN_CONTRACT_VERSION",
    "APPLICATION_PLAN_STAGES",
    "ApplicationAutomationPolicy",
    "ApplicationPlan",
    "ApplicationPlanFailureReason",
    "ApplicationPlanReadResult",
    "ApplicationPlanReadStatus",
    "ApplicationPlanRepository",
    "ApplicationPlanStage",
    "ApplicationPlanWriteResult",
    "ApplicationPlanWriteStatus",
    "CreateApplicationPlanCommand",
    "CreateApplicationPlanReason",
    "CreateApplicationPlanResult",
    "CreateApplicationPlanStatus",
    "HumanAttentionPolicy",
    "PrivateHomeApplicationPlanRepository",
    "application_plan_instruction_hash",
    "create_application_plan",
]
