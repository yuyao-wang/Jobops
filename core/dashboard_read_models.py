"""Authenticated, subject-scoped, zero-write Dashboard read projections."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Awaitable, Callable, Mapping

from .application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS,
    ApplicationExecutionIdentityFieldRequiredness,
)
from .application_plan import ApplicationPlanListStatus
from .application_preparation_orchestrator import (
    ApplicationPreparationRunReadStatus,
    ApplicationPreparationRunStatus,
)
from .candidate_identity_facts import (
    CandidateIdentityFactConflictState,
    GetCurrentCandidateIdentityFactCommand,
    GetCurrentCandidateIdentityFactStatus,
)
from .candidate_information_sources import (
    CandidateInformationSourceKind,
    CandidateInformationSourceListStatus,
)
from .current_application_execution_queue import (
    CurrentApplicationExecutionQueueStatus,
    CurrentApplicationExecutionStatus,
)
from .current_priority_queue import CurrentPriorityItemStatus
from .human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionQueueStatus,
)
from .job_prioritization import PriorityQualification
from .runnable_application_queue import (
    RunnableApplicationQueueCommand,
    RunnableApplicationQueueStatus,
    RunnableApplicationStatus,
)
from .search_profile import SearchProfileListStatus


DASHBOARD_READ_CONTRACT_VERSION = "dashboard-read-v1"
DASHBOARD_MAPPING_POLICY_VERSION = "dashboard-mapping-v1"


class DashboardReadStatus(StrEnum):
    READY = "READY"
    EMPTY = "EMPTY"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class DashboardProfileState(StrEnum):
    EMPTY = "EMPTY"
    INCOMPLETE = "INCOMPLETE"
    READY = "READY"
    CONFLICT = "CONFLICT"
    SYSTEM_ISSUE = "SYSTEM_ISSUE"


class DashboardJobLibraryState(StrEnum):
    EMPTY = "EMPTY"
    READY = "READY"
    PARTIAL = "PARTIAL"
    SYSTEM_ISSUE = "SYSTEM_ISSUE"


class DashboardJobStatus(StrEnum):
    NOT_EVALUATED = "NOT_EVALUATED"
    EVALUATING = "EVALUATING"
    HIGH_MATCH = "HIGH_MATCH"
    READY_TO_PREPARE = "READY_TO_PREPARE"
    NEEDS_INPUT = "NEEDS_INPUT"
    NOT_A_MATCH = "NOT_A_MATCH"
    APPLICATION_CREATED = "APPLICATION_CREATED"
    SYSTEM_ISSUE = "SYSTEM_ISSUE"


class DashboardApplicationStatus(StrEnum):
    SELECTED = "SELECTED"
    PREPARING = "PREPARING"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    SYSTEM_ISSUE = "SYSTEM_ISSUE"


class DashboardApplicationProgressStage(StrEnum):
    SELECTED = "SELECTED"
    PREPARING = "PREPARING"
    REVIEW = "REVIEW"
    READY = "READY"
    SUBMITTED = "SUBMITTED"


class DashboardProgressStepState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    CURRENT = "CURRENT"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"


class DashboardApplicationNextAction(StrEnum):
    REVIEW_ATTENTION = "REVIEW_ATTENTION"
    VIEW_PROGRESS = "VIEW_PROGRESS"
    CONTINUE_AUTOMATION = "CONTINUE_AUTOMATION"
    VIEW_SUBMISSION = "VIEW_SUBMISSION"
    REVIEW_UNCERTAIN_SUBMISSION = "REVIEW_UNCERTAIN_SUBMISSION"
    CONTACT_SYSTEM_OPERATOR = "CONTACT_SYSTEM_OPERATOR"
    NONE = "NONE"


class DashboardNextStep(StrEnum):
    SYSTEM_ATTENTION = "SYSTEM_ATTENTION"
    COMPLETE_PROFILE = "COMPLETE_PROFILE"
    SET_JOB_PREFERENCES = "SET_JOB_PREFERENCES"
    REVIEW_ATTENTION = "REVIEW_ATTENTION"
    REFRESH_JOB_LIBRARY = "REFRESH_JOB_LIBRARY"
    CONTINUE_AUTOMATION = "CONTINUE_AUTOMATION"
    VIEW_APPLICATIONS = "VIEW_APPLICATIONS"
    ALL_CAUGHT_UP = "ALL_CAUGHT_UP"


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    return value


async def _call(callable_: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = callable_(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


@dataclass(frozen=True, slots=True)
class DashboardIdentityField:
    field_key: str
    display_value: str | None
    value_state: str
    verification_state: str
    source_count: int
    can_edit: bool


@dataclass(frozen=True, slots=True)
class DashboardCandidateProfile:
    read_status: DashboardReadStatus
    profile_state: DashboardProfileState
    identity_fields: tuple[DashboardIdentityField, ...]
    required_field_count: int
    verified_required_field_count: int
    missing_required_fields: tuple[str, ...]
    conflicting_fields: tuple[str, ...]
    source_summary: Mapping[str, Any]
    search_preference_summary: Mapping[str, Any]
    review_summary: Mapping[str, Any]
    capabilities: Mapping[str, str]
    snapshot_hash: str
    evaluated_at: datetime
    read_contract_version: str = DASHBOARD_READ_CONTRACT_VERSION
    mapping_policy_version: str = DASHBOARD_MAPPING_POLICY_VERSION

    def to_public_dict(self) -> dict[str, Any]:
        value = _json_value(self)
        value.pop("snapshot_hash", None)
        return value


class DashboardCandidateProfileReader:
    def __init__(
        self,
        *,
        fact_repository: Any,
        source_provider: Any,
        search_profile_provider: Any,
    ) -> None:
        self._facts = fact_repository
        self._sources = source_provider
        self._search_profiles = search_profile_provider

    async def read(
        self, *, subject_id: str, evaluated_at: datetime
    ) -> DashboardCandidateProfile:
        _aware(evaluated_at)
        try:
            index = self._facts.get_index(subject_id)
            sources = self._sources.list_for_subject(subject_id)
            profiles = self._search_profiles.list_enabled(subject_id)
        except Exception:
            return self._failed(evaluated_at, DashboardReadStatus.FAILED)
        if getattr(index, "subject_id", None) != subject_id:
            return self._failed(
                evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
            )
        if (
            sources.status
            is CandidateInformationSourceListStatus.INTEGRITY_FAILURE
            or profiles.status is SearchProfileListStatus.INTEGRITY_FAILURE
        ):
            return self._failed(
                evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
            )

        by_key = {entry.field_key: entry for entry in index.entries}
        fields: list[DashboardIdentityField] = []
        missing: list[str] = []
        conflicts: list[str] = []
        verified_required = 0
        verified_field_count = 0
        required_count = 0
        identity_refs: list[dict[str, Any]] = []
        for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS:
            key = definition.field_key
            entry = by_key.get(key)
            required = (
                definition.requiredness
                is ApplicationExecutionIdentityFieldRequiredness.REQUIRED_FOR_EXECUTION
            )
            required_count += int(required)
            conflict = (
                entry is not None
                and entry.conflict_state
                is not CandidateIdentityFactConflictState.NONE
            )
            if conflict:
                conflicts.append(key.value)
            display_value: str | None = None
            verification = "MISSING"
            if entry is not None and entry.current_fact_id is not None and not conflict:
                current = self._facts.get_current(
                    GetCurrentCandidateIdentityFactCommand(
                        subject_id=subject_id, field_key=key
                    )
                )
                if (
                    current.status
                    is GetCurrentCandidateIdentityFactStatus.FOUND
                    and current.fact is not None
                    and current.fact.subject_id == subject_id
                ):
                    display_value = current.fact.normalized_value
                    verification = current.fact.verification_status.value
                    verified_field_count += 1
                    if required:
                        verified_required += 1
                elif current.status is GetCurrentCandidateIdentityFactStatus.CONFLICT:
                    conflicts.append(key.value)
                    conflict = True
                elif current.status is GetCurrentCandidateIdentityFactStatus.INTEGRITY_FAILURE:
                    return self._failed(
                        evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
                    )
            if required and display_value is None:
                missing.append(key.value)
            fields.append(
                DashboardIdentityField(
                    field_key=key.value,
                    display_value=display_value,
                    value_state=(
                        "CONFLICT"
                        if conflict
                        else "PRESENT"
                        if display_value is not None
                        else "MISSING"
                    ),
                    verification_state=verification,
                    source_count=(
                        len(entry.source_refs) if entry is not None else 0
                    ),
                    can_edit=True,
                )
            )
            identity_refs.append(
                entry.identity_dict()
                if entry is not None
                else {"field_key": key.value, "state": "MISSING"}
            )

        if conflicts:
            state = DashboardProfileState.CONFLICT
        elif verified_field_count == 0:
            state = DashboardProfileState.EMPTY
        elif missing:
            state = DashboardProfileState.INCOMPLETE
        else:
            state = DashboardProfileState.READY
        profile_status = (
            DashboardReadStatus.EMPTY
            if state is DashboardProfileState.EMPTY
            else DashboardReadStatus.READY
        )
        source_values = sources.sources
        source_summary = {
            "total_sources": len(source_values),
            "file_source_count": sum(
                item.source_kind is CandidateInformationSourceKind.FILE
                for item in source_values
            ),
            "url_source_count": sum(
                item.source_kind is CandidateInformationSourceKind.URL
                for item in source_values
            ),
            "user_statement_count": sum(
                item.source_kind is CandidateInformationSourceKind.USER_STATEMENT
                for item in source_values
            ),
            "latest_registered_at": (
                max(item.registered_at for item in source_values)
                if source_values
                else None
            ),
            "source_capabilities": ("FILE", "URL", "USER_STATEMENT"),
        }
        enabled = tuple(profiles.profiles)
        preference_summary = {
            "enabled_profile_count": len(enabled),
            "target_roles": tuple(
                sorted(
                    {
                        item.search_request.title
                        for item in enabled
                        if item.search_request.title
                    }
                )
            ),
            "target_locations": tuple(
                sorted(
                    {
                        item.search_request.location
                        for item in enabled
                        if item.search_request.location
                    }
                )
            ),
            "minimum_match_score": None,
            "configured_source_count": len(
                {item.source.source_id for item in enabled}
            ),
        }
        snapshot = _hash(
            {
                "contract": DASHBOARD_READ_CONTRACT_VERSION,
                "identity": identity_refs,
                "mapping": DASHBOARD_MAPPING_POLICY_VERSION,
                "profiles": [
                    {
                        "id": item.profile_id,
                        "version": item.profile_version,
                        "hash": item.content_hash,
                    }
                    for item in enabled
                ],
                "sources": [
                    {
                        "id": item.source_id,
                        "hash": item.source_identity_hash,
                    }
                    for item in source_values
                ],
                "state": state.value,
                "subject_id": subject_id,
            }
        )
        return DashboardCandidateProfile(
            read_status=profile_status,
            profile_state=state,
            identity_fields=tuple(fields),
            required_field_count=required_count,
            verified_required_field_count=verified_required,
            missing_required_fields=tuple(sorted(set(missing))),
            conflicting_fields=tuple(sorted(set(conflicts))),
            source_summary=source_summary,
            search_preference_summary=preference_summary,
            review_summary={
                "pending_proposals": None,
                "conflicts": len(set(conflicts)),
                "missing_required_fields": len(set(missing)),
            },
            capabilities={"review_capability": "UNAVAILABLE"},
            snapshot_hash=snapshot,
            evaluated_at=evaluated_at,
        )

    @staticmethod
    def _failed(
        evaluated_at: datetime, status: DashboardReadStatus
    ) -> DashboardCandidateProfile:
        return DashboardCandidateProfile(
            read_status=status,
            profile_state=DashboardProfileState.SYSTEM_ISSUE,
            identity_fields=(),
            required_field_count=0,
            verified_required_field_count=0,
            missing_required_fields=(),
            conflicting_fields=(),
            source_summary={},
            search_preference_summary={"enabled_profile_count": None},
            review_summary={},
            capabilities={"review_capability": "UNAVAILABLE"},
            snapshot_hash=_hash(
                {"contract": DASHBOARD_READ_CONTRACT_VERSION, "status": status}
            ),
            evaluated_at=evaluated_at,
        )


@dataclass(frozen=True, slots=True)
class DashboardJobItem:
    job_id: str
    title: str
    company: str
    location: str
    canonical_url: str
    priority_bucket: str | None
    match_score: float | None
    match_reasons: tuple[str, ...]
    priority_state: str
    application_intent_state: str
    preparation_eligibility: str
    application_status: DashboardJobStatus
    next_action: str
    discovered_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DashboardJobsReadModel:
    read_status: DashboardReadStatus
    last_refreshed_at: datetime | None
    library_state: DashboardJobLibraryState
    counts: Mapping[str, int | None]
    ordered_items: tuple[DashboardJobItem, ...]
    snapshot_hash: str
    evaluated_at: datetime
    capabilities: Mapping[str, str]
    read_contract_version: str = DASHBOARD_READ_CONTRACT_VERSION
    mapping_policy_version: str = DASHBOARD_MAPPING_POLICY_VERSION

    def to_public_dict(self) -> dict[str, Any]:
        value = _json_value(self)
        value.pop("snapshot_hash", None)
        for item in value["ordered_items"]:
            item.pop("priority_state", None)
            item.pop("application_intent_state", None)
            item.pop("preparation_eligibility", None)
        return value


_JOB_STATUS_ORDER = {
    DashboardJobStatus.READY_TO_PREPARE: 0,
    DashboardJobStatus.APPLICATION_CREATED: 1,
    DashboardJobStatus.HIGH_MATCH: 2,
    DashboardJobStatus.NEEDS_INPUT: 3,
    DashboardJobStatus.NOT_EVALUATED: 4,
    DashboardJobStatus.EVALUATING: 5,
    DashboardJobStatus.NOT_A_MATCH: 6,
    DashboardJobStatus.SYSTEM_ISSUE: 7,
}
_PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, None: 4}


class DashboardJobsReader:
    def __init__(
        self,
        *,
        runnable_queue_reader: Callable[..., Awaitable[Any] | Any],
        application_plan_repository: Any,
    ) -> None:
        self._queue = runnable_queue_reader
        self._plans = application_plan_repository

    async def read(
        self, *, subject_id: str, evaluated_at: datetime
    ) -> DashboardJobsReadModel:
        _aware(evaluated_at)
        try:
            queue = await _call(
                self._queue,
                RunnableApplicationQueueCommand(
                    subject_id=subject_id, now=evaluated_at
                ),
            )
            plans = self._plans.list_for_subject(subject_id)
        except Exception:
            return self._failed(evaluated_at, DashboardReadStatus.FAILED)
        if queue.status is not RunnableApplicationQueueStatus.SUCCEEDED:
            return self._failed(evaluated_at, DashboardReadStatus.FAILED)
        if (
            plans.status is ApplicationPlanListStatus.INTEGRITY_FAILURE
            or queue.subject_id != subject_id
        ):
            return self._failed(
                evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
            )
        plan_jobs = {plan.job_id for plan in plans.plans}
        items: list[DashboardJobItem] = []
        identities: list[dict[str, Any]] = []
        for source in queue.items:
            if source.subject_id != subject_id:
                return self._failed(
                    evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
                )
            decision = source.priority_decision
            if source.job.job_id in plan_jobs:
                status = DashboardJobStatus.APPLICATION_CREATED
            elif source.priority_queue_status is CurrentPriorityItemStatus.INCOMPLETE:
                status = DashboardJobStatus.SYSTEM_ISSUE
            elif source.priority_queue_status in {
                CurrentPriorityItemStatus.MISSING,
                CurrentPriorityItemStatus.STALE,
            }:
                status = DashboardJobStatus.NOT_EVALUATED
            elif (
                decision is not None
                and decision.qualification is PriorityQualification.NEEDS_USER
            ):
                status = DashboardJobStatus.NEEDS_INPUT
            elif (
                decision is not None
                and decision.qualification is PriorityQualification.EXCLUDED
            ):
                status = DashboardJobStatus.NOT_A_MATCH
            elif source.runnable_status is RunnableApplicationStatus.RUNNABLE:
                status = DashboardJobStatus.READY_TO_PREPARE
            elif (
                decision is not None
                and decision.qualification is PriorityQualification.QUALIFIED
            ):
                status = DashboardJobStatus.HIGH_MATCH
            else:
                status = DashboardJobStatus.NOT_EVALUATED
            bucket = (
                decision.priority_level.value
                if decision is not None and decision.priority_level is not None
                else None
            )
            reasons = (
                tuple(
                    item.explanation
                    for item in decision.positive_signals[:3]
                    if item.explanation
                )
                if decision is not None
                else ()
            )
            item = DashboardJobItem(
                job_id=source.job.job_id,
                title=source.job.title,
                company=source.job.company,
                location=source.job.location,
                canonical_url=(
                    source.job.application_url or source.job.source_url
                ),
                priority_bucket=bucket,
                match_score=None,
                match_reasons=reasons,
                priority_state=source.priority_queue_status.value,
                application_intent_state=(
                    source.application_intent.intent.value
                    if source.application_intent is not None
                    else "NONE"
                ),
                preparation_eligibility=source.runnable_status.value,
                application_status=status,
                next_action=(
                    "VIEW_APPLICATION"
                    if status is DashboardJobStatus.APPLICATION_CREATED
                    else "REVIEW_INPUT"
                    if status is DashboardJobStatus.NEEDS_INPUT
                    else "CONTINUE_AUTOMATION"
                    if status
                    in {
                        DashboardJobStatus.READY_TO_PREPARE,
                        DashboardJobStatus.HIGH_MATCH,
                    }
                    else "VIEW_JOB"
                ),
                discovered_at=source.job.observed_at,
                updated_at=source.job.observed_at,
            )
            items.append(item)
            identities.append(
                {
                    "job_id": source.job.job_id,
                    "revision": source.job.revision,
                    "content_hash": source.job.content_hash,
                    "decision_id": (
                        decision.decision_id if decision is not None else None
                    ),
                    "status": status.value,
                }
            )
        ordered = tuple(
            sorted(
                items,
                key=lambda item: (
                    _JOB_STATUS_ORDER[item.application_status],
                    _PRIORITY_ORDER[item.priority_bucket],
                    item.discovered_at,
                    item.job_id,
                ),
            )
        )
        total = len(ordered)
        counts = {
            "total": total,
            "current_priority": sum(
                item.priority_state == CurrentPriorityItemStatus.CURRENT.value
                for item in ordered
            ),
            "high_match": sum(
                item.application_status
                in {
                    DashboardJobStatus.HIGH_MATCH,
                    DashboardJobStatus.READY_TO_PREPARE,
                }
                for item in ordered
            ),
            "ready_to_prepare": sum(
                item.application_status
                is DashboardJobStatus.READY_TO_PREPARE
                for item in ordered
            ),
            "needs_input": sum(
                item.application_status is DashboardJobStatus.NEEDS_INPUT
                for item in ordered
            ),
            "excluded": sum(
                item.application_status is DashboardJobStatus.NOT_A_MATCH
                for item in ordered
            ),
            "new_since_last_refresh": None,
        }
        return DashboardJobsReadModel(
            read_status=(
                DashboardReadStatus.READY
                if ordered
                else DashboardReadStatus.EMPTY
            ),
            last_refreshed_at=None,
            library_state=(
                DashboardJobLibraryState.READY
                if ordered
                else DashboardJobLibraryState.EMPTY
            ),
            counts=counts,
            ordered_items=ordered,
            snapshot_hash=_hash(
                {
                    "contract": DASHBOARD_READ_CONTRACT_VERSION,
                    "identities": identities,
                    "mapping": DASHBOARD_MAPPING_POLICY_VERSION,
                    "membership_snapshot": (
                        queue.priority_queue_result.membership_snapshot_hash
                        if queue.priority_queue_result is not None
                        else None
                    ),
                    "subject_id": subject_id,
                }
            ),
            evaluated_at=evaluated_at,
            capabilities={
                "last_refresh": "UNAVAILABLE",
                "new_since_last_refresh": "UNAVAILABLE",
                "staleness": "UNAVAILABLE",
            },
        )

    @staticmethod
    def _failed(
        evaluated_at: datetime, status: DashboardReadStatus
    ) -> DashboardJobsReadModel:
        return DashboardJobsReadModel(
            read_status=status,
            last_refreshed_at=None,
            library_state=DashboardJobLibraryState.SYSTEM_ISSUE,
            counts={
                key: None
                for key in (
                    "total",
                    "current_priority",
                    "high_match",
                    "ready_to_prepare",
                    "needs_input",
                    "excluded",
                    "new_since_last_refresh",
                )
            },
            ordered_items=(),
            snapshot_hash=_hash({"status": status.value}),
            evaluated_at=evaluated_at,
            capabilities={},
        )


@dataclass(frozen=True, slots=True)
class DashboardProgressStep:
    stage: DashboardApplicationProgressStage
    state: DashboardProgressStepState


@dataclass(frozen=True, slots=True)
class DashboardApplicationItem:
    application_plan_id: str
    job_id: str
    title: str
    company: str
    location: str
    product_status: DashboardApplicationStatus
    progress_stage: DashboardApplicationProgressStage
    progress_steps: tuple[DashboardProgressStep, ...]
    attention_count: int
    user_attention_count: int
    operator_attention_count: int
    next_action: DashboardApplicationNextAction
    last_business_event_at: datetime
    safe_status_detail: str


@dataclass(frozen=True, slots=True)
class DashboardApplicationsReadModel:
    read_status: DashboardReadStatus
    counts: Mapping[str, int]
    ordered_items: tuple[DashboardApplicationItem, ...]
    snapshot_hash: str
    evaluated_at: datetime
    read_contract_version: str = DASHBOARD_READ_CONTRACT_VERSION
    mapping_policy_version: str = DASHBOARD_MAPPING_POLICY_VERSION

    def to_public_dict(self) -> dict[str, Any]:
        value = _json_value(self)
        value.pop("snapshot_hash", None)
        return value


_APPLICATION_STAGE = {
    DashboardApplicationStatus.SELECTED: DashboardApplicationProgressStage.SELECTED,
    DashboardApplicationStatus.PREPARING: DashboardApplicationProgressStage.PREPARING,
    DashboardApplicationStatus.NEEDS_ATTENTION: DashboardApplicationProgressStage.REVIEW,
    DashboardApplicationStatus.READY: DashboardApplicationProgressStage.READY,
    DashboardApplicationStatus.SUBMITTED: DashboardApplicationProgressStage.SUBMITTED,
    DashboardApplicationStatus.SUBMISSION_UNCERTAIN: DashboardApplicationProgressStage.SUBMITTED,
    DashboardApplicationStatus.SYSTEM_ISSUE: DashboardApplicationProgressStage.PREPARING,
}
_APPLICATION_DETAIL = {
    DashboardApplicationStatus.SELECTED: "Selected for application",
    DashboardApplicationStatus.PREPARING: "Preparing application materials",
    DashboardApplicationStatus.NEEDS_ATTENTION: "Waiting for your answer",
    DashboardApplicationStatus.READY: "Ready for the next automation cycle",
    DashboardApplicationStatus.SUBMITTED: "Submission confirmed",
    DashboardApplicationStatus.SUBMISSION_UNCERTAIN: "Submission could not be confirmed automatically",
    DashboardApplicationStatus.SYSTEM_ISSUE: "JobOps needs system attention",
}
_APPLICATION_ACTION = {
    DashboardApplicationStatus.SELECTED: DashboardApplicationNextAction.CONTINUE_AUTOMATION,
    DashboardApplicationStatus.PREPARING: DashboardApplicationNextAction.VIEW_PROGRESS,
    DashboardApplicationStatus.NEEDS_ATTENTION: DashboardApplicationNextAction.REVIEW_ATTENTION,
    DashboardApplicationStatus.READY: DashboardApplicationNextAction.CONTINUE_AUTOMATION,
    DashboardApplicationStatus.SUBMITTED: DashboardApplicationNextAction.VIEW_SUBMISSION,
    DashboardApplicationStatus.SUBMISSION_UNCERTAIN: DashboardApplicationNextAction.REVIEW_UNCERTAIN_SUBMISSION,
    DashboardApplicationStatus.SYSTEM_ISSUE: DashboardApplicationNextAction.CONTACT_SYSTEM_OPERATOR,
}


def _progress(status: DashboardApplicationStatus) -> tuple[DashboardProgressStep, ...]:
    current = _APPLICATION_STAGE[status]
    stages = tuple(DashboardApplicationProgressStage)
    current_index = stages.index(current)
    blocked = status in {
        DashboardApplicationStatus.NEEDS_ATTENTION,
        DashboardApplicationStatus.SUBMISSION_UNCERTAIN,
        DashboardApplicationStatus.SYSTEM_ISSUE,
    }
    return tuple(
        DashboardProgressStep(
            stage=stage,
            state=(
                DashboardProgressStepState.COMPLETED
                if stages.index(stage) < current_index
                else DashboardProgressStepState.BLOCKED
                if stage is current and blocked
                else DashboardProgressStepState.CURRENT
                if stage is current
                else DashboardProgressStepState.NOT_STARTED
            ),
        )
        for stage in stages
    )


class DashboardApplicationsReader:
    def __init__(
        self,
        *,
        application_plan_repository: Any,
        preparation_run_repository: Any,
        human_attention_reader: Callable[..., Any],
        execution_queue_reader: Callable[..., Any],
        job_posting_reader: Any,
    ) -> None:
        self._plans = application_plan_repository
        self._preparations = preparation_run_repository
        self._attention = human_attention_reader
        self._execution = execution_queue_reader
        self._jobs = job_posting_reader

    async def read(
        self,
        *,
        subject_id: str,
        evaluated_at: datetime,
        attention_snapshot: Any | None = None,
    ) -> DashboardApplicationsReadModel:
        _aware(evaluated_at)
        try:
            plans = self._plans.list_for_subject(subject_id)
            attention = attention_snapshot or await _call(
                self._attention, subject_id=subject_id, now=evaluated_at
            )
            execution = await _call(
                self._execution, subject_id=subject_id, now=evaluated_at
            )
        except Exception:
            return self._failed(evaluated_at, DashboardReadStatus.FAILED)
        if (
            attention.status is not HumanAttentionQueueStatus.SUCCEEDED
            or execution.status
            is not CurrentApplicationExecutionQueueStatus.SUCCEEDED
        ):
            return self._failed(evaluated_at, DashboardReadStatus.FAILED)
        if (
            plans.status is ApplicationPlanListStatus.INTEGRITY_FAILURE
            or attention.subject_id != subject_id
        ):
            return self._failed(
                evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
            )
        attention_by_plan: dict[str, list[Any]] = {}
        for item in attention.items:
            if item.subject_id != subject_id:
                return self._failed(
                    evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
                )
            attention_by_plan.setdefault(item.application_plan_id, []).append(item)
        execution_by_plan = {
            item.application_plan_id: item for item in execution.items
        }
        result_items: list[DashboardApplicationItem] = []
        identity: list[dict[str, Any]] = []
        for plan in plans.plans:
            if plan.subject_id != subject_id:
                return self._failed(
                    evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
                )
            job = self._jobs.get(plan.job_id)
            if (
                job is None
                or job.job_id != plan.job_id
                or job.revision != plan.job_revision
                or job.content_hash != plan.job_content_hash
            ):
                return self._failed(
                    evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
                )
            current = self._preparations.find_current_for_plan(
                subject_id=subject_id, application_plan_id=plan.plan_id
            )
            if current.status is ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE:
                return self._failed(
                    evaluated_at, DashboardReadStatus.INTEGRITY_FAILURE
                )
            prep = current.run
            execution_item = execution_by_plan.get(plan.plan_id)
            attention_items = attention_by_plan.get(plan.plan_id, [])
            user_count = sum(
                item.audience is HumanAttentionAudience.USER
                for item in attention_items
            )
            operator_count = len(attention_items) - user_count
            if (
                execution_item is not None
                and execution_item.execution_status
                is CurrentApplicationExecutionStatus.SUBMITTED
            ):
                status = DashboardApplicationStatus.SUBMITTED
            elif (
                execution_item is not None
                and execution_item.execution_status
                is CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN
            ):
                status = DashboardApplicationStatus.SUBMISSION_UNCERTAIN
            elif user_count:
                status = DashboardApplicationStatus.NEEDS_ATTENTION
            elif operator_count or (
                execution_item is not None
                and execution_item.execution_status
                in {
                    CurrentApplicationExecutionStatus.FAILED,
                    CurrentApplicationExecutionStatus.DEFERRED,
                }
            ) or (
                prep is not None
                and prep.status is ApplicationPreparationRunStatus.FAILED
            ):
                status = DashboardApplicationStatus.SYSTEM_ISSUE
            elif (
                execution_item is not None
                and execution_item.execution_status
                is CurrentApplicationExecutionStatus.READY
            ):
                status = DashboardApplicationStatus.READY
            elif prep is not None:
                status = DashboardApplicationStatus.PREPARING
            else:
                status = DashboardApplicationStatus.SELECTED
            event_times = [plan.created_at]
            if prep is not None:
                event_times.append(prep.completed_at)
            if attention_items:
                event_times.append(
                    max(item.source_event_time for item in attention_items)
                )
            item = DashboardApplicationItem(
                application_plan_id=plan.plan_id,
                job_id=plan.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                product_status=status,
                progress_stage=_APPLICATION_STAGE[status],
                progress_steps=_progress(status),
                attention_count=len(attention_items),
                user_attention_count=user_count,
                operator_attention_count=operator_count,
                next_action=_APPLICATION_ACTION[status],
                last_business_event_at=max(event_times),
                safe_status_detail=_APPLICATION_DETAIL[status],
            )
            result_items.append(item)
            identity.append(
                {
                    "plan_id": plan.plan_id,
                    "job_hash": plan.job_content_hash,
                    "preparation_run_id": prep.run_id if prep is not None else None,
                    "execution_item_hash": (
                        execution_item.item_hash
                        if execution_item is not None
                        else None
                    ),
                    "attention_hashes": sorted(
                        value.item_content_hash for value in attention_items
                    ),
                    "status": status.value,
                }
            )
        ordered = tuple(
            sorted(
                result_items,
                key=lambda item: (
                    -item.last_business_event_at.timestamp(),
                    item.application_plan_id,
                ),
            )
        )
        counts = {
            status.value.lower(): sum(
                item.product_status is status for item in ordered
            )
            for status in DashboardApplicationStatus
        }
        counts["total"] = len(ordered)
        return DashboardApplicationsReadModel(
            read_status=(
                DashboardReadStatus.READY
                if ordered
                else DashboardReadStatus.EMPTY
            ),
            counts=counts,
            ordered_items=ordered,
            snapshot_hash=_hash(
                {
                    "attention_snapshot": attention.queue_snapshot_hash,
                    "contract": DASHBOARD_READ_CONTRACT_VERSION,
                    "execution_snapshot": execution.snapshot_hash,
                    "identity": identity,
                    "mapping": DASHBOARD_MAPPING_POLICY_VERSION,
                    "subject_id": subject_id,
                }
            ),
            evaluated_at=evaluated_at,
        )

    @staticmethod
    def _failed(
        evaluated_at: datetime, status: DashboardReadStatus
    ) -> DashboardApplicationsReadModel:
        return DashboardApplicationsReadModel(
            read_status=status,
            counts={},
            ordered_items=(),
            snapshot_hash=_hash({"status": status.value}),
            evaluated_at=evaluated_at,
        )


@dataclass(frozen=True, slots=True)
class DashboardOverviewReadModel:
    read_status: DashboardReadStatus
    next_step: DashboardNextStep
    profile_summary: Mapping[str, Any]
    job_library_summary: Mapping[str, Any]
    application_summary: Mapping[str, Any]
    attention_summary: Mapping[str, Any]
    top_matches: tuple[DashboardJobItem, ...]
    recent_applications: tuple[DashboardApplicationItem, ...]
    source_snapshot_hashes: Mapping[str, str]
    overview_snapshot_hash: str
    evaluated_at: datetime
    read_contract_version: str = DASHBOARD_READ_CONTRACT_VERSION
    mapping_policy_version: str = DASHBOARD_MAPPING_POLICY_VERSION

    def to_public_dict(self) -> dict[str, Any]:
        value = _json_value(self)
        value.pop("overview_snapshot_hash", None)
        value.pop("source_snapshot_hashes", None)
        for item in value["top_matches"]:
            item.pop("priority_state", None)
            item.pop("application_intent_state", None)
            item.pop("preparation_eligibility", None)
        return value


class DashboardOverviewReader:
    def __init__(
        self,
        *,
        profile_reader: DashboardCandidateProfileReader,
        jobs_reader: DashboardJobsReader,
        applications_reader: DashboardApplicationsReader,
        human_attention_reader: Callable[..., Any],
    ) -> None:
        self._profile = profile_reader
        self._jobs = jobs_reader
        self._applications = applications_reader
        self._attention = human_attention_reader

    async def read(
        self, *, subject_id: str, evaluated_at: datetime
    ) -> DashboardOverviewReadModel:
        profile = await self._profile.read(
            subject_id=subject_id, evaluated_at=evaluated_at
        )
        jobs = await self._jobs.read(
            subject_id=subject_id, evaluated_at=evaluated_at
        )
        try:
            attention = await _call(
                self._attention, subject_id=subject_id, now=evaluated_at
            )
        except Exception:
            attention = None
        applications = await self._applications.read(
            subject_id=subject_id,
            evaluated_at=evaluated_at,
            attention_snapshot=attention,
        )
        mandatory = (
            profile.read_status,
            jobs.read_status,
            applications.read_status,
        )
        attention_failed = (
            attention is None
            or attention.status is not HumanAttentionQueueStatus.SUCCEEDED
        )
        if attention_failed or any(
            status
            in {
                DashboardReadStatus.FAILED,
                DashboardReadStatus.INTEGRITY_FAILURE,
            }
            for status in mandatory
        ):
            next_step = DashboardNextStep.SYSTEM_ATTENTION
            status = DashboardReadStatus.INTEGRITY_FAILURE
        elif profile.profile_state in {
            DashboardProfileState.EMPTY,
            DashboardProfileState.INCOMPLETE,
            DashboardProfileState.CONFLICT,
        }:
            next_step = DashboardNextStep.COMPLETE_PROFILE
            status = DashboardReadStatus.READY
        elif (
            profile.search_preference_summary.get("enabled_profile_count", 0)
            == 0
        ):
            next_step = DashboardNextStep.SET_JOB_PREFERENCES
            status = DashboardReadStatus.READY
        elif attention.user_item_count:
            next_step = DashboardNextStep.REVIEW_ATTENTION
            status = DashboardReadStatus.READY
        elif jobs.library_state is DashboardJobLibraryState.EMPTY:
            next_step = DashboardNextStep.REFRESH_JOB_LIBRARY
            status = DashboardReadStatus.READY
        elif any(
            item.product_status
            in {
                DashboardApplicationStatus.SELECTED,
                DashboardApplicationStatus.PREPARING,
                DashboardApplicationStatus.READY,
            }
            for item in applications.ordered_items
        ):
            next_step = DashboardNextStep.CONTINUE_AUTOMATION
            status = DashboardReadStatus.READY
        elif applications.ordered_items:
            next_step = DashboardNextStep.VIEW_APPLICATIONS
            status = DashboardReadStatus.READY
        else:
            next_step = DashboardNextStep.ALL_CAUGHT_UP
            status = DashboardReadStatus.READY
        source_hashes = {
            "profile": profile.snapshot_hash,
            "jobs": jobs.snapshot_hash,
            "applications": applications.snapshot_hash,
            "attention": (
                attention.queue_snapshot_hash if attention is not None else ""
            ),
        }
        top = tuple(
            item
            for item in jobs.ordered_items
            if item.application_status
            not in {
                DashboardJobStatus.NOT_A_MATCH,
                DashboardJobStatus.SYSTEM_ISSUE,
            }
        )[:5]
        recent = applications.ordered_items[:5]
        return DashboardOverviewReadModel(
            read_status=status,
            next_step=next_step,
            profile_summary={
                "state": profile.profile_state,
                "verified_required_field_count": (
                    profile.verified_required_field_count
                ),
                "required_field_count": profile.required_field_count,
            },
            job_library_summary={
                "state": jobs.library_state,
                "counts": jobs.counts,
            },
            application_summary={"counts": applications.counts},
            attention_summary={
                "status": (
                    attention.status.value if attention is not None else "FAILED"
                ),
                "total": (
                    attention.item_count if attention is not None else None
                ),
                "user": (
                    attention.user_item_count if attention is not None else None
                ),
                "operator": (
                    attention.operator_item_count
                    if attention is not None
                    else None
                ),
            },
            top_matches=top,
            recent_applications=recent,
            source_snapshot_hashes=source_hashes,
            overview_snapshot_hash=_hash(
                {
                    "contract": DASHBOARD_READ_CONTRACT_VERSION,
                    "mapping": DASHBOARD_MAPPING_POLICY_VERSION,
                    "next_step": next_step.value,
                    "sources": source_hashes,
                    "subject_id": subject_id,
                }
            ),
            evaluated_at=evaluated_at,
        )
