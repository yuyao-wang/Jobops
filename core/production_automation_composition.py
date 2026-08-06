"""Authoritative production automation composition for P2c10c.

The root constructs and connects existing public business callables.  It does
not execute any stage while being built and owns no business decision logic.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Mapping

from adapters.preparation_agents import ProductionPreparationAgentAdapters
from auth.imap_provider import IMAPMailboxProvider, IMAPProviderConfig
from core.application_assembly_execution_context import (
    load_application_assembly_execution_context,
)
from core.application_answers import PreparedApplicationAnswerSetReadStatus
from core.application_bundle_assembly import (
    ApplicationBundleAssemblyReadStatus,
    assemble_application_bundle,
)
from core.application_engine import JobApplicationEngine
from core.application_execution_orchestrator import run_application_execution
from core.application_plan import (
    CreateApplicationPlanCommand,
    create_application_plan,
)
from core.application_preparation_orchestrator import (
    P2_APPROVED_RESUME_REUSE_SKIPPED_STAGES,
    run_application_preparation,
)
from core.authenticated_subject import KeychainAuthenticatedSubjectSessionProvider
from core.authorized_submission_execution import (
    AuthorizedSubmissionExecutionMetadata,
    execute_authorized_submission,
)
from core.automation_cycle import run_automation_cycle
from core.current_application_execution_queue import (
    CurrentApplicationExecutionQueueStatus,
    CurrentApplicationExecutionStatus,
    build_current_application_execution_queue,
)
from core.current_priority_queue import (
    CurrentPriorityQueueCommand,
    CurrentPriorityQueueStatus,
    build_current_priority_queue,
)
from core.conversational_intake import (
    InMemoryCandidateSelectionStore,
    InMemoryPendingIntakeStore,
)
from core.dashboard_read_models import (
    DashboardApplicationsReader,
    DashboardCandidateProfileReader,
    DashboardJobsReader,
    DashboardOverviewReader,
)
from core.fact_qa_findings import (
    FactQAMaterialKind,
    RepositoryFactQABlockingFindingProvider,
)
from core.human_attention_queue import (
    HumanAttentionQueueStatus,
    build_current_human_attention_queue,
)
from core.subject_job_discovery import build_subject_job_discovery
from core.subject_job_library import SubjectScopedJobPostingReader
from core.job_discovery import build_production_job_discovery
from core.job_alerts import (
    JobAlertInboxConfig,
    JobAlertInboxIngestor,
    JobAlertPersistenceStatus,
    ingest_job_alerts_for_subject,
)
from core.job_lead_discovery import (
    JobLeadDiscoveryCommand,
    JobLeadDiscoveryPhase,
    JobLeadDiscoveryProgress,
    JobLeadDiscoveryRunSummary,
    JobLeadDiscoverySource,
    discover_job_leads,
    resolve_persisted_job_leads,
)
from core.job_leads import (
    JobLead,
    JobLeadListStatus,
    JobLeadSource,
    JobLeadStatus,
)
from core.job_library_refresh import (
    ConfiguredSearchProfileExecutor,
    refresh_job_library,
)
from core.non_submit_application_execution import (
    NonSubmitExecutionMetadata,
    execute_non_submit_application,
)
from core.material_correction_target import (
    MaterialCorrectionTargetProvider,
    PrivateHomeMaterialCorrectionTargetRepository,
)
from core.permits import OpaquePermitTokenStore
from core.plan_assembly_execution_context_binding import (
    bind_plan_assembly_execution_context,
)
from core.plan_execution_policy import decide_plan_execution_policy
from core.production_application_bootstrap import (
    PRODUCTION_APPLICATION_BOOTSTRAP_CONTRACT_VERSION,
    ProductionApplicationBootstrap,
)
from core.production_application_bundle_factory import (
    ProductionApplicationBundleFactory,
    build_production_application_bundle_factory,
)
from core.production_application_preparation_recipe import (
    ApplicationPreparationRecipe,
    build_production_application_preparation_recipe,
)
from core.production_priority_agent import (
    StructuredBackendPriorityAgentAdapter,
    build_production_priority_agent,
)
from core.production_named_job_clue_extractor import (
    build_production_named_job_clue_extractor,
)
from core.production_prioritization_policy_interpreter import (
    build_production_prioritization_policy_interpreter,
)
from core.prioritization_policy import (
    InMemoryPrioritizationPolicyDraftStore,
    PrioritizationPolicyService,
)
from core.runnable_application_queue import (
    RunnableApplicationQueueCommand,
    RunnableApplicationQueueStatus,
    build_runnable_application_queue,
)
from core.unsupported_claim_correction import (
    PrivateHomeUnsupportedClaimCorrectionDirectiveRepository,
    UnsupportedClaimCorrectionDirectiveProvider,
    UnsupportedClaimCorrectionReceiptRepository,
    resolve_unsupported_claim_correction,
)
from core.search_profile_intent_policy import (
    EnableAutoRequestApplicationBatchCommand,
    EnableAutoRequestApplicationBatchStatus,
    enable_auto_request_application_for_enabled_search_profiles,
)
from core.selective_batch_execution import run_selective_batch_execution
from core.selective_batch_plan_creation import (
    run_selective_batch_plan_creation,
)
from core.selective_batch_preparation import run_selective_batch_preparation
from core.selective_bundle_assembly import run_selective_bundle_assembly
from core.selective_reprioritization import selectively_reprioritize_jobs
from core.single_job_priority import (
    SingleJobPriorityCommand,
    SingleJobPriorityStatus,
    orchestrate_single_job_priority,
)
from core.submission_authorization import decide_submission_authorization
from core.submission_permit import (
    SubmissionPermitPolicy,
    issue_submission_permit,
)
from core.verified_application_execution_profile import (
    project_verified_application_execution_profile,
)
from dashboard.authentication import (
    AuthenticatedSubjectDependency,
    LocalDashboardSessionController,
    make_authenticated_subject_dependency,
)
from dashboard.application_review_submission import (
    ApplicationReviewSubmissionUIController,
)
from dashboard.reviewed_application_compatibility import (
    ReviewedApplicationCompatibilityUIController,
)
from dashboard.conversational_job_finder import (
    ConversationalJobFinderUIController,
)
from dashboard.automation_cycle import (
    AutomationPreflightProgress,
    AutomationPreflightProgressObserver,
    AutomationPreflightResult,
    AutomationPreflightStatus,
    ContinueAutomationUIController,
)
from dashboard.unsupported_claim_correction import (
    UnsupportedClaimCorrectionUIController,
)
from dashboard.human_attention_inbox import HumanAttentionInboxUIController
from dashboard.job_library_refresh import (
    LeadRefreshPhase,
    LeadRefreshProgress,
    LeadRefreshResult,
    LeadRefreshSourceResult,
    LeadRefreshSourceStatus,
    LeadRefreshStatus,
    RefreshJobLibraryUICommand,
    RefreshJobLibraryUIController,
    RefreshJobLibraryUIStatus,
)
from dashboard.job_source_intake import (
    AssistedJobImportController,
    SearchProfileUIController,
)
from dashboard.prioritization_policy import (
    PrioritizationPolicyUIController,
)
from dashboard.read_models import (
    DashboardApplicationsController,
    DashboardJobsController,
    DashboardOverviewController,
    DashboardProfileController,
)
from source_connectors.greenhouse_board import (
    HttpxBoundedJobSearchHttpClient,
)
from source_connectors.authorized_web_search import (
    BraveAuthorizedWebSearch,
    BraveWebSearchConfig,
)
from source_connectors.production_job_search import (
    ProductionJobSearchPorts,
    build_conversational_job_search_port,
    build_production_job_search_ports,
)
from source_connectors.public_reader import read_public_job


PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION = (
    "production-automation-composition-v3"
)
DEFAULT_AUTOMATION_PREFLIGHT_REFRESH_TIMEOUT_SECONDS = 300.0
_P2_RETRYABLE_SYSTEM_ATTENTION_REASONS = frozenset(
    {
        "AGENT_SELECTION_UNSAFE",
        "AGENT_UNAVAILABLE",
        "AGENT_TIMEOUT",
    }
)
_P2_RECOVERABLE_EXECUTION_REASONS = frozenset(
    {
        "DEFERRED_GATE_A_REQUIRED",
    }
)
_P2_APPROVED_RESUME_REUSE_SKIPPED_STAGE_VALUES = frozenset(
    stage.value for stage in P2_APPROVED_RESUME_REUSE_SKIPPED_STAGES
)


def _p2_attention_is_migration_retryable(attention: Any) -> bool:
    priority = getattr(attention, "priority", None)
    priority_value = getattr(priority, "value", priority)
    stage = getattr(attention, "source_stage", None)
    stage_value = getattr(stage, "value", stage)
    reason = getattr(attention, "source_reason_code", None)
    return priority_value == "P2" and (
        stage_value in _P2_APPROVED_RESUME_REUSE_SKIPPED_STAGE_VALUES
        or reason in _P2_RETRYABLE_SYSTEM_ATTENTION_REASONS
        or (
            stage_value == "COVER_LETTER_DRAFT"
            and reason == "AGENT_OUTPUT_UNSAFE"
        )
        or (
            stage_value == "APPLICATION_ANSWERS"
            and reason == "REQUIRES_ATTESTATION"
        )
    )


def _project_resumable_attention_plan_ids_by_job(
    attention_items: tuple[Any, ...],
) -> dict[str, str]:
    """Recover P2 plans blocked only by an explicitly migrated policy rule."""

    grouped: dict[tuple[str, str], list[Any]] = {}
    for item in attention_items:
        if not getattr(item, "blocking", False):
            continue
        job_id = getattr(item, "job_id", None)
        plan_id = getattr(item, "application_plan_id", None)
        if not isinstance(job_id, str) or not isinstance(plan_id, str):
            continue
        grouped.setdefault((job_id, plan_id), []).append(item)

    candidates: dict[str, list[tuple[datetime, str]]] = {}
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for (job_id, plan_id), items in grouped.items():
        if not items or not all(
            _p2_attention_is_migration_retryable(item) for item in items
        ):
            continue
        candidates.setdefault(job_id, []).append(
            (
                max(
                    (
                        getattr(item, "source_event_time", None) or epoch
                        for item in items
                    ),
                    default=epoch,
                ),
                plan_id,
            )
        )
    return {
        job_id: max(plans, key=lambda item: (item[0], item[1]))[1]
        for job_id, plans in sorted(candidates.items())
    }


def _project_resumable_plan_ids_by_job(
    execution_items: tuple[Any, ...],
) -> dict[str, str]:
    """Select one exact P2 plan whose assembled progress can safely resume."""

    candidates: dict[str, list[Any]] = {}
    for item in execution_items:
        priority = getattr(item, "priority", None)
        priority_value = getattr(priority, "value", priority)
        status = getattr(item, "execution_status", None)
        deferred_stage = getattr(item, "deferred_stage", None)
        deferred_stage_value = getattr(deferred_stage, "value", deferred_stage)
        deferred_reason = getattr(item, "deferred_reason", None)
        resumable = (
            status is CurrentApplicationExecutionStatus.READY
            or (
                status is CurrentApplicationExecutionStatus.DEFERRED
                and deferred_stage_value == "NON_SUBMIT_EXECUTION"
                and deferred_reason in _P2_RECOVERABLE_EXECUTION_REASONS
            )
        )
        if priority_value != "P2" or not resumable:
            continue
        job_id = getattr(item, "job_id", None)
        plan_id = getattr(item, "application_plan_id", None)
        if not isinstance(job_id, str) or not isinstance(plan_id, str):
            continue
        candidates.setdefault(job_id, []).append(item)

    projected: dict[str, str] = {}
    for job_id, items in candidates.items():
        selected = min(
            items,
            key=lambda item: (
                0
                if getattr(item, "execution_status", None)
                is CurrentApplicationExecutionStatus.DEFERRED
                else 1,
                getattr(item, "plan_created_at", datetime.max.replace(
                    tzinfo=timezone.utc
                )),
                getattr(item, "application_plan_id", ""),
            ),
        )
        projected[job_id] = selected.application_plan_id
    return projected


def _project_fact_stale_runtime_input_plan_ids_by_job(
    execution_items: tuple[Any, ...],
    *,
    subject_id: str,
    assembly_repository: Any,
    answer_set_repository: Any,
    fact_provider: Any,
) -> dict[str, str]:
    """Resume a runtime handoff once when its trusted facts have changed."""

    try:
        current_snapshot = fact_provider.get_current(subject_id)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return {}
    current_hash = getattr(current_snapshot, "snapshot_content_hash", None)
    if not isinstance(current_hash, str) or not current_hash:
        return {}
    terminal_jobs = {
        item.job_id
        for item in execution_items
        if item.execution_status
        in {
            CurrentApplicationExecutionStatus.SUBMITTED,
            CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN,
        }
    }
    candidates: dict[str, list[Any]] = {}
    for item in execution_items:
        priority = getattr(item, "priority", None)
        priority_value = getattr(priority, "value", priority)
        deferred_stage = getattr(item, "deferred_stage", None)
        stage_value = getattr(deferred_stage, "value", deferred_stage)
        if (
            item.job_id in terminal_jobs
            or priority_value != "P2"
            or item.execution_status
            is not CurrentApplicationExecutionStatus.DEFERRED
            or stage_value != "NON_SUBMIT_EXECUTION"
            or item.deferred_reason != "DEFERRED_RUNTIME_INPUT_REQUIRED"
        ):
            continue
        try:
            assembly_read = assembly_repository.get(
                subject_id=subject_id,
                record_id=item.assembly_record_id,
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            continue
        if (
            assembly_read.status
            is not ApplicationBundleAssemblyReadStatus.FOUND
            or assembly_read.record is None
        ):
            continue
        assembly = assembly_read.record
        try:
            answer_read = answer_set_repository.get(
                subject_id=subject_id,
                answer_set_id=assembly.answer_set_id,
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            continue
        if (
            answer_read.status
            is not PreparedApplicationAnswerSetReadStatus.FOUND
            or answer_read.answer_set is None
        ):
            continue
        answers = answer_read.answer_set
        if (
            assembly.subject_id != subject_id
            or assembly.application_plan_id != item.application_plan_id
            or assembly.job_id != item.job_id
            or answers.subject_id != subject_id
            or answers.application_plan_id != item.application_plan_id
            or answers.job_id != item.job_id
            or answers.answer_set_id != assembly.answer_set_id
            or answers.answer_set_content_hash
            != assembly.answer_set_content_hash
            or answers.fact_snapshot_hash == current_hash
        ):
            continue
        candidates.setdefault(item.job_id, []).append(item)
    return {
        job_id: max(
            items,
            key=lambda item: (
                getattr(
                    item,
                    "plan_created_at",
                    datetime.min.replace(tzinfo=timezone.utc),
                ),
                item.application_plan_id,
            ),
        ).application_plan_id
        for job_id, items in sorted(candidates.items())
    }


def _project_automation_work_ids(
    *,
    runnable_items: tuple[Any, ...],
    execution_items: tuple[Any, ...],
    attention_items: tuple[Any, ...] = (),
    fact_stale_runtime_input_plan_ids_by_job: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Keep runnable order while removing submitted or current blockers.

    Attention is plan-priority scoped.  A historical P1 preparation failure
    must not suppress a newly-current P2 route for the same job.  P2 failures
    in stages that the approved-resume reuse route no longer executes are also
    migration-retryable; every other current attention item remains blocking.
    """

    terminal_job_ids = frozenset(
        item.job_id
        for item in execution_items
        if item.execution_status
        in {
            CurrentApplicationExecutionStatus.SUBMITTED,
            CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN,
        }
    )
    blocking_by_job: dict[str, list[Any]] = {}
    for attention in attention_items:
        if attention.blocking:
            blocking_by_job.setdefault(attention.job_id, []).append(attention)

    fact_stale_by_job = dict(
        fact_stale_runtime_input_plan_ids_by_job or {}
    )
    resumable_by_job = _project_resumable_plan_ids_by_job(execution_items)
    nonrecoverable_execution_job_ids = frozenset(
        item.job_id
        for item in execution_items
        if item.execution_status
        is CurrentApplicationExecutionStatus.DEFERRED
        and item.job_id not in resumable_by_job
        and fact_stale_by_job.get(item.job_id)
        != getattr(item, "application_plan_id", None)
    )
    resumable_attention_by_job = (
        _project_resumable_attention_plan_ids_by_job(attention_items)
    )
    resumable_projected: list[str] = [
        job_id
        for job_id in fact_stale_by_job
        if job_id not in terminal_job_ids
    ] + [
        job_id
        for job_id in resumable_attention_by_job
        if job_id not in terminal_job_ids
        and job_id not in nonrecoverable_execution_job_ids
        and job_id not in fact_stale_by_job
    ]
    projected: list[str] = []
    for item in runnable_items:
        job_id = item.job.job_id
        if (
            job_id in terminal_job_ids
            or job_id in nonrecoverable_execution_job_ids
        ):
            continue
        decision = getattr(item, "priority_decision", None)
        current_priority = getattr(decision, "priority_level", None)
        if (
            job_id in resumable_by_job
            and getattr(current_priority, "value", current_priority) == "P2"
        ):
            resumable_projected.append(job_id)
            continue
        current_attention = tuple(
            attention
            for attention in blocking_by_job.get(job_id, ())
            if current_priority is None
            or getattr(attention, "priority", current_priority)
            == current_priority
        )
        if current_attention and not (
            getattr(current_priority, "value", current_priority) == "P2"
            and all(
                _p2_attention_is_migration_retryable(attention)
                for attention in current_attention
            )
        ):
            continue
        if job_id in resumable_attention_by_job:
            continue
        if job_id in fact_stale_by_job:
            continue
        projected.append(job_id)
    return tuple(resumable_projected + projected)


class ProductionAutomationCompositionFailure(StrEnum):
    BOOTSTRAP_INCOMPATIBLE = "BOOTSTRAP_INCOMPATIBLE"
    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
    JOB_FINDER_UNAVAILABLE = "JOB_FINDER_UNAVAILABLE"
    PRIORITY_AGENT_UNAVAILABLE = "PRIORITY_AGENT_UNAVAILABLE"
    PREPARATION_UNAVAILABLE = "PREPARATION_UNAVAILABLE"
    EXECUTION_CONTEXT_UNAVAILABLE = "EXECUTION_CONTEXT_UNAVAILABLE"
    BUNDLE_FACTORY_UNAVAILABLE = "BUNDLE_FACTORY_UNAVAILABLE"
    EXECUTION_UNAVAILABLE = "EXECUTION_UNAVAILABLE"
    AUTHENTICATION_UNAVAILABLE = "AUTHENTICATION_UNAVAILABLE"
    CONTROLLER_UNAVAILABLE = "CONTROLLER_UNAVAILABLE"
    REPOSITORY_UNAVAILABLE = "REPOSITORY_UNAVAILABLE"


class ProductionAutomationCompositionError(RuntimeError):
    def __init__(
        self,
        failure: ProductionAutomationCompositionFailure,
        *,
        component: str | None = None,
    ) -> None:
        self.failure = ProductionAutomationCompositionFailure(failure)
        self.component = component
        message = self.failure.value
        if component:
            message += f":{component}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProductionAutomationComposition:
    refresh_job_library_controller: RefreshJobLibraryUIController
    search_profile_controller: SearchProfileUIController
    assisted_job_import_controller: AssistedJobImportController
    conversational_job_finder_controller: ConversationalJobFinderUIController
    prioritization_policy_controller: PrioritizationPolicyUIController
    continue_automatic_application_controller: ContinueAutomationUIController
    human_attention_controller: HumanAttentionInboxUIController
    unsupported_claim_correction_controller: (
        UnsupportedClaimCorrectionUIController
    )
    dashboard_profile_controller: DashboardProfileController
    dashboard_jobs_controller: DashboardJobsController
    dashboard_applications_controller: DashboardApplicationsController
    application_review_submission_controller: (
        ApplicationReviewSubmissionUIController
    )
    reviewed_application_compatibility_controller: (
        ReviewedApplicationCompatibilityUIController
    )
    dashboard_overview_controller: DashboardOverviewController
    local_session_controller: LocalDashboardSessionController
    authenticated_subject_dependency: AuthenticatedSubjectDependency
    production_job_search_ports: ProductionJobSearchPorts
    production_priority_agent: StructuredBackendPriorityAgentAdapter
    priority_agent_metadata: object
    preparation_agent_adapters: ProductionPreparationAgentAdapters
    application_preparation_recipe: ApplicationPreparationRecipe
    selective_batch_preparation_callable: Callable[..., Any]
    verified_profile_projector: Callable[..., Any]
    verified_profile_provider: object
    execution_policy_decider: Callable[..., Any]
    execution_policy_provider: object
    execution_context_binder: Callable[..., Any]
    application_bundle_factory: ProductionApplicationBundleFactory
    application_bundle_assembly_callable: Callable[..., Any]
    selective_bundle_assembly_callable: Callable[..., Any]
    current_execution_queue_callable: Callable[..., Any]
    selective_execution_callable: Callable[..., Any]
    automation_cycle_callable: Callable[..., Any]
    owned_resources: tuple[object, ...]
    safe_diagnostics: Mapping[str, Any]
    composition_contract_version: str = (
        PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if self.composition_contract_version != (
            PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION
        ):
            raise ValueError("composition contract version is unsupported")
        mandatory = (
            self.authenticated_subject_dependency,
            self.selective_batch_preparation_callable,
            self.verified_profile_projector,
            self.execution_policy_decider,
            self.execution_context_binder,
            self.application_bundle_assembly_callable,
            self.selective_bundle_assembly_callable,
            self.current_execution_queue_callable,
            self.selective_execution_callable,
            self.automation_cycle_callable,
        )
        if not all(callable(item) for item in mandatory):
            raise ValueError("composition contains a missing callable")
        controllers = (
            (
                self.local_session_controller,
                LocalDashboardSessionController,
            ),
            (self.search_profile_controller, SearchProfileUIController),
            (
                self.assisted_job_import_controller,
                AssistedJobImportController,
            ),
            (
                self.conversational_job_finder_controller,
                ConversationalJobFinderUIController,
            ),
            (
                self.prioritization_policy_controller,
                PrioritizationPolicyUIController,
            ),
            (self.human_attention_controller, HumanAttentionInboxUIController),
            (
                self.unsupported_claim_correction_controller,
                UnsupportedClaimCorrectionUIController,
            ),
            (self.dashboard_profile_controller, DashboardProfileController),
            (self.dashboard_jobs_controller, DashboardJobsController),
            (
                self.dashboard_applications_controller,
                DashboardApplicationsController,
            ),
            (
                self.application_review_submission_controller,
                ApplicationReviewSubmissionUIController,
            ),
            (
                self.reviewed_application_compatibility_controller,
                ReviewedApplicationCompatibilityUIController,
            ),
            (self.dashboard_overview_controller, DashboardOverviewController),
        )
        if any(
            not isinstance(controller, expected)
            for controller, expected in controllers
        ):
            raise ValueError("composition contains an invalid read controller")

    def install_dashboard(self, application: Any) -> None:
        """Atomically inject both production controllers and lifecycle."""

        from dashboard.server import configure_production_automation_ui

        configure_production_automation_ui(
            application=application,
            refresh_controller=self.refresh_job_library_controller,
            search_profile_controller=self.search_profile_controller,
            assisted_job_import_controller=(
                self.assisted_job_import_controller
            ),
            conversational_job_finder_controller=(
                self.conversational_job_finder_controller
            ),
            prioritization_policy_controller=(
                self.prioritization_policy_controller
            ),
            automation_controller=self.continue_automatic_application_controller,
            local_session_controller=self.local_session_controller,
            authenticated_subject=self.authenticated_subject_dependency,
            owned_resources=self.owned_resources,
            composition_diagnostics=self.safe_diagnostics,
            human_attention_controller=self.human_attention_controller,
            unsupported_claim_correction_controller=(
                self.unsupported_claim_correction_controller
            ),
            dashboard_profile_controller=self.dashboard_profile_controller,
            dashboard_jobs_controller=self.dashboard_jobs_controller,
            dashboard_applications_controller=(
                self.dashboard_applications_controller
            ),
            application_review_submission_controller=(
                self.application_review_submission_controller
            ),
            reviewed_application_compatibility_controller=(
                self.reviewed_application_compatibility_controller
            ),
            dashboard_overview_controller=self.dashboard_overview_controller,
        )


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _repo(bootstrap: ProductionApplicationBootstrap, name: str) -> object:
    try:
        return bootstrap.repository_bundle.require(name)
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.REPOSITORY_UNAVAILABLE,
            component=name,
        ) from None


def build_production_automation_composition(
    *,
    bootstrap: ProductionApplicationBootstrap,
    clock: Callable[[], datetime] | None = None,
    preflight_refresh_timeout_seconds: float = (
        DEFAULT_AUTOMATION_PREFLIGHT_REFRESH_TIMEOUT_SECONDS
    ),
) -> ProductionAutomationComposition:
    """Build the complete production graph without executing any stage."""

    if (
        not isinstance(bootstrap, ProductionApplicationBootstrap)
        or bootstrap.bootstrap_contract_version
        != PRODUCTION_APPLICATION_BOOTSTRAP_CONTRACT_VERSION
    ):
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.BOOTSTRAP_INCOMPATIBLE
        )
    active_clock = clock or _clock
    if not callable(active_clock):
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.BOOTSTRAP_INCOMPATIBLE,
            component="clock",
        )
    if (
        isinstance(preflight_refresh_timeout_seconds, bool)
        or not isinstance(preflight_refresh_timeout_seconds, (int, float))
        or not 0 < float(preflight_refresh_timeout_seconds) <= 3600
    ):
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.BOOTSTRAP_INCOMPATIBLE,
            component="preflight_refresh_timeout_seconds",
        )
    active_preflight_refresh_timeout = float(
        preflight_refresh_timeout_seconds
    )

    plan_repository = _repo(bootstrap, "application_plans")
    job_repository = _repo(bootstrap, "job_postings")
    membership_repository = _repo(
        bootstrap, "subject_job_library_memberships"
    )
    subject_job_reader = SubjectScopedJobPostingReader(
        membership_repository=membership_repository,
        job_posting_reader=job_repository,
    )
    policy_repository = _repo(bootstrap, "prioritization_policies")
    priority_decision_repository = _repo(bootstrap, "priority_decisions")
    priority_orchestration_repository = _repo(
        bootstrap, "single_job_priority"
    )
    accepted_intent_repository = _repo(bootstrap, "accepted_job_intents")
    lead_repository = _repo(bootstrap, "job_leads")

    try:
        search_http_client = HttpxBoundedJobSearchHttpClient()
        search_ports = build_production_job_search_ports(
            boards=bootstrap.job_search_factory_inputs.boards,
            ashby_boards=(
                bootstrap.job_search_factory_inputs.ashby_boards
            ),
            lever_sites=bootstrap.job_search_factory_inputs.lever_sites,
            glassdoor=bootstrap.job_search_factory_inputs.glassdoor,
            jobvite_feeds=(
                bootstrap.job_search_factory_inputs.jobvite_feeds
            ),
            http_port=search_http_client,
            policy=bootstrap.job_search_factory_inputs.policy,
        )
        search_executor = ConfiguredSearchProfileExecutor(search_ports.ports)
        conversational_search_port = build_conversational_job_search_port(
            search_ports
        )
        authorized_web_search = None
        web_search_inputs = (
            bootstrap.job_search_factory_inputs.authorized_web_search
        )
        if web_search_inputs is not None:
            search_policy = bootstrap.job_search_factory_inputs.policy
            authorized_web_search = BraveAuthorizedWebSearch(
                config=BraveWebSearchConfig(
                    api_key=web_search_inputs.api_key,
                    storage_rights_confirmed=(
                        web_search_inputs.storage_rights_confirmed
                    ),
                    connect_timeout_seconds=(
                        search_policy.connect_timeout_seconds
                    ),
                    read_timeout_seconds=search_policy.read_timeout_seconds,
                    max_response_bytes=min(
                        search_policy.max_response_bytes,
                        5_000_000,
                    ),
                    max_redirects=min(search_policy.max_redirects, 2),
                ),
                http_port=search_http_client,
            )
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.SEARCH_UNAVAILABLE
        ) from None

    try:
        named_job_clue_extractor = build_production_named_job_clue_extractor(
            ai_config=bootstrap.priority_agent_factory_inputs.ai_config,
            backend_registry=(
                bootstrap.priority_agent_factory_inputs.backend_registry
            ),
            isolation_profile_registry=(
                bootstrap.priority_agent_factory_inputs
                .isolation_profile_registry
            ),
        )
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.JOB_FINDER_UNAVAILABLE
        ) from None

    try:
        priority_agent = build_production_priority_agent(
            ai_config=bootstrap.priority_agent_factory_inputs.ai_config,
            backend_registry=(
                bootstrap.priority_agent_factory_inputs.backend_registry
            ),
            isolation_profile_registry=(
                bootstrap.priority_agent_factory_inputs
                .isolation_profile_registry
            ),
        )
        policy_interpreter = (
            build_production_prioritization_policy_interpreter(
                ai_config=bootstrap.priority_agent_factory_inputs.ai_config,
                backend_registry=(
                    bootstrap.priority_agent_factory_inputs.backend_registry
                ),
                isolation_profile_registry=(
                    bootstrap.priority_agent_factory_inputs
                    .isolation_profile_registry
                ),
            )
        )
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.PRIORITY_AGENT_UNAVAILABLE
        ) from None

    from core.profile_store import PrivateHomeCandidateSummaryProvider

    candidate_summary_provider = PrivateHomeCandidateSummaryProvider(
        bootstrap.private_home
    )

    async def priority_queue(command: Any) -> Any:
        return await build_current_priority_queue(
            command,
            subject_job_reader=subject_job_reader,
            policy_provider=policy_repository,
            candidate_summary_provider=candidate_summary_provider,
            orchestration_repository=priority_orchestration_repository,
            decision_repository=priority_decision_repository,
            metadata=priority_agent.metadata,
        )

    async def single_job_priority(command: Any) -> Any:
        return await orchestrate_single_job_priority(
            command,
            job_repository=job_repository,
            policy_provider=policy_repository,
            candidate_summary_provider=candidate_summary_provider,
            orchestration_repository=priority_orchestration_repository,
            decision_repository=priority_decision_repository,
            agent=priority_agent,
            metadata=priority_agent.metadata,
        )

    async def priority_refresh(command: Any) -> Any:
        return await selectively_reprioritize_jobs(
            command,
            queue_reader=priority_queue,
            single_job_orchestrator=single_job_priority,
        )

    global_discovery = build_production_job_discovery(
        private_home=bootstrap.private_home
    )
    discovery = build_subject_job_discovery(
        discovery=global_discovery,
        job_reader=job_repository,
        membership_repository=membership_repository,
    )

    job_alert_ingestor = None
    job_alert_config = bootstrap.config.search.job_alert_inbox
    if job_alert_config is not None:
        credential_ref = job_alert_config.credential_ref
        try:
            job_alert_ingestor = JobAlertInboxIngestor(
                provider=IMAPMailboxProvider(
                    config=IMAPProviderConfig(
                        enabled=True,
                        host=job_alert_config.host,
                        account=job_alert_config.recipient,
                        keychain_service=credential_ref.service or "",
                        port=job_alert_config.port,
                        mailbox=job_alert_config.mailbox,
                        trusted_authserv_ids=(
                            job_alert_config.trusted_authserv_ids
                        ),
                        max_search_window=timedelta(
                            hours=job_alert_config.max_age_hours
                        ),
                    ),
                    credential_store=bootstrap.credential_store,
                    now=active_clock,
                ),
                config=JobAlertInboxConfig(
                    enabled=True,
                    recipient=job_alert_config.recipient,
                    allowed_sender_domains=(
                        job_alert_config.allowed_sender_domains
                    ),
                    max_age=timedelta(
                        hours=job_alert_config.max_age_hours
                    ),
                    max_messages=job_alert_config.max_messages,
                ),
                now=active_clock,
            )
        except Exception:
            raise ProductionAutomationCompositionError(
                ProductionAutomationCompositionFailure.SEARCH_UNAVAILABLE,
                component="job-alert-inbox",
            ) from None
    manual_refresh = partial(
        refresh_job_library,
        profile_provider=_repo(bootstrap, "search_profiles"),
        search_executor=search_executor,
        public_job_reader=read_public_job,
        discovery=discovery,
        priority_refresh=priority_refresh,
        repository=_repo(bootstrap, "job_library_refresh_runs"),
        intent_policy_provider=_repo(
            bootstrap, "search_profile_intent_policies"
        ),
        accepted_intent_repository=accepted_intent_repository,
        prioritization_policy_provider=policy_repository,
    )

    async def job_lead_refresh(
        *,
        subject_id: str,
        invocation_id: str,
        now: datetime,
        progress_observer: Callable[[LeadRefreshProgress], Any] | None = None,
    ) -> LeadRefreshResult:
        """Run configured lead channels without treating a lead as a job fact."""

        count_names = (
            "requests",
            "completed",
            "discovered",
            "unique",
            "duplicates",
            "resolved",
            "needs_user",
            "failed",
            "public_reads",
            "priorities_requested",
            "priorities_refreshed",
            "priorities_failed",
        )
        totals: dict[str, int | bool] = {
            **{name: 0 for name in count_names},
            "truncated": False,
        }
        source_counts: dict[
            tuple[JobLeadSource, str | None], dict[str, int | bool]
        ] = {}

        def bucket(
            source: JobLeadSource,
            family: str | None = None,
        ) -> dict[str, int | bool]:
            return source_counts.setdefault(
                (JobLeadSource(source), family),
                {
                    **{
                        name: 0
                        for name in (
                            "requests",
                            "completed",
                            "search_hits",
                            "discovered",
                            "unique",
                            "duplicates",
                            "resolved",
                            "needs_user",
                            "failed",
                            "public_reads",
                        )
                    },
                    "truncated": False,
                },
            )

        def add(
            target: dict[str, int | bool],
            **values: int | bool,
        ) -> None:
            for name, value in values.items():
                if name == "truncated":
                    target[name] = bool(target[name]) or bool(value)
                else:
                    target[name] = int(target[name]) + int(value)

        def source_result_values() -> tuple[LeadRefreshSourceResult, ...]:
            results: list[LeadRefreshSourceResult] = []
            for (source, family), values in sorted(
                source_counts.items(),
                key=lambda item: (item[0][0].value, item[0][1] or ""),
            ):
                failed = int(values["failed"])
                meaningful = any(
                    int(values[name])
                    for name in (
                        "requests",
                        "completed",
                        "search_hits",
                        "discovered",
                        "unique",
                        "duplicates",
                        "resolved",
                        "needs_user",
                        "public_reads",
                    )
                )
                truncated = bool(values["truncated"])
                if failed and not meaningful:
                    status = LeadRefreshSourceStatus.FAILED
                elif failed or truncated:
                    status = LeadRefreshSourceStatus.PARTIAL_FAILURE
                elif meaningful:
                    status = LeadRefreshSourceStatus.COMPLETED
                else:
                    status = LeadRefreshSourceStatus.NOOP
                results.append(
                    LeadRefreshSourceResult(
                        source=source,
                        status=status,
                        family=family,
                        requests=int(values["requests"]),
                        completed=int(values["completed"]),
                        search_hits=int(values["search_hits"]),
                        discovered=int(values["discovered"]),
                        unique=int(values["unique"]),
                        duplicates=int(values["duplicates"]),
                        resolved=int(values["resolved"]),
                        needs_user=int(values["needs_user"]),
                        failed=failed,
                        public_reads=int(values["public_reads"]),
                        truncated=truncated,
                    )
                )
            return tuple(results)

        async def emit(phase: LeadRefreshPhase) -> None:
            if progress_observer is None:
                return
            progress = LeadRefreshProgress(
                phase=phase,
                requests=int(totals["requests"]),
                completed=int(totals["completed"]),
                discovered=int(totals["discovered"]),
                unique=int(totals["unique"]),
                duplicates=int(totals["duplicates"]),
                resolved=int(totals["resolved"]),
                needs_user=int(totals["needs_user"]),
                failed=int(totals["failed"]),
                public_reads=int(totals["public_reads"]),
                priorities_requested=int(totals["priorities_requested"]),
                priorities_refreshed=int(totals["priorities_refreshed"]),
                priorities_failed=int(totals["priorities_failed"]),
                truncated=bool(totals["truncated"]),
                source_results=source_result_values(),
            )
            try:
                observed = progress_observer(progress)
                if inspect.isawaitable(observed):
                    await observed
            except (OSError, RuntimeError, TypeError, ValueError):
                return

        acquisition_sources = frozenset(
            {
                JobLeadDiscoverySource.LINKEDIN,
                JobLeadDiscoverySource.INDEED,
                JobLeadDiscoverySource.GLASSDOOR,
                JobLeadDiscoverySource.GREENHOUSE,
                JobLeadDiscoverySource.LEVER,
                JobLeadDiscoverySource.ASHBY,
                JobLeadDiscoverySource.JOBVITE,
                JobLeadDiscoverySource.WORKDAY,
                JobLeadDiscoverySource.SMARTRECRUITERS,
                JobLeadDiscoverySource.ICIMS,
                JobLeadDiscoverySource.SUCCESSFACTORS,
                JobLeadDiscoverySource.GENERIC_CAREERS,
            }
        )

        def sync_web_snapshot(
            snapshot: JobLeadDiscoveryProgress | JobLeadDiscoveryRunSummary,
            *,
            baseline: dict[str, int | bool],
        ) -> None:
            acquisition_hits = sum(
                item.hits
                for item in snapshot.source_results
                if item.source in acquisition_sources
            )
            for name, value in (
                ("requests", snapshot.requests),
                ("completed", snapshot.completed),
                ("discovered", acquisition_hits),
                ("unique", snapshot.unique),
                ("duplicates", snapshot.duplicates),
                ("resolved", snapshot.resolved),
                ("needs_user", snapshot.needs_user),
                ("failed", snapshot.failed),
                ("public_reads", snapshot.public_reads),
            ):
                totals[name] = int(baseline[name]) + value
            totals["truncated"] = bool(baseline["truncated"]) or bool(
                snapshot.truncated
            )

            for key in tuple(source_counts):
                if key[0] is JobLeadSource.AUTHORIZED_WEB_SEARCH:
                    source_counts.pop(key)
            for item in snapshot.source_results:
                values = bucket(
                    JobLeadSource.AUTHORIZED_WEB_SEARCH,
                    item.source.value,
                )
                is_acquisition = item.source in acquisition_sources
                values.update(
                    {
                        "requests": item.requests,
                        "completed": item.completed,
                        "search_hits": item.hits,
                        "discovered": item.hits if is_acquisition else 0,
                        "unique": item.unique,
                        "duplicates": item.duplicates,
                        "resolved": item.resolved,
                        "needs_user": item.needs_user,
                        "failed": item.failed,
                        "public_reads": item.public_reads,
                        "truncated": item.truncated,
                    }
                )

        async def observe_web_discovery(
            progress: JobLeadDiscoveryProgress,
            *,
            baseline: dict[str, int | bool],
        ) -> None:
            sync_web_snapshot(progress, baseline=baseline)
            phase = (
                LeadRefreshPhase.RESOLVING
                if progress.phase
                in {
                    JobLeadDiscoveryPhase.RESOLVING,
                    JobLeadDiscoveryPhase.COMPLETED,
                }
                else LeadRefreshPhase.DISCOVERING
            )
            await emit(phase)

        try:
            initial = lead_repository.list_current(subject_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            initial = None
        if (
            initial is None
            or initial.status is not JobLeadListStatus.SUCCEEDED
        ):
            return LeadRefreshResult(
                status=LeadRefreshStatus.FAILED,
                failed=1,
            )
        initial_by_id = {lead.lead_id: lead for lead in initial.leads}
        configured = bool(
            job_alert_ingestor is not None
            or authorized_web_search is not None
            or any(
                lead.status is JobLeadStatus.DISCOVERED
                for lead in initial.leads
            )
        )
        resolved_job_ids: list[str] = []

        if job_alert_ingestor is not None:
            await emit(LeadRefreshPhase.INGESTING_ALERTS)
            alert_inbox_bucket = bucket(
                JobLeadSource.JOB_ALERT_INBOX,
                "IMAP",
            )
            add(totals, requests=1)
            add(alert_inbox_bucket, requests=1)
            try:
                alert_result = await ingest_job_alerts_for_subject(
                    subject_id=subject_id,
                    ingestor=job_alert_ingestor,
                    repository=lead_repository,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                alert_result = None
            if alert_result is None:
                add(totals, failed=1)
                add(alert_inbox_bucket, failed=1)
            else:
                if alert_result.source_status.value not in {
                    "PROVIDER_UNAVAILABLE",
                    "INVALID_CONFIG",
                }:
                    add(totals, completed=1)
                    add(alert_inbox_bucket, completed=1)
                alert_failure_count = alert_result.failed
                if (
                    alert_result.status
                    in {
                        JobAlertPersistenceStatus.FAILED,
                        JobAlertPersistenceStatus.PARTIAL_FAILURE,
                    }
                    and alert_failure_count == 0
                ):
                    alert_failure_count = 1
                add(
                    totals,
                    discovered=len(alert_result.leads),
                    unique=alert_result.created,
                    duplicates=alert_result.duplicates,
                    failed=alert_failure_count,
                )
                add(alert_inbox_bucket, failed=alert_failure_count)
                initial_urls = {
                    lead.source_url for lead in initial_by_id.values()
                }
                acquisition_sources = (
                    alert_result.acquisition_sources
                    if alert_result.acquisition_sources
                    else tuple(lead.source for lead in alert_result.leads)
                )
                for lead, acquisition_source in zip(
                    alert_result.leads,
                    acquisition_sources,
                    strict=True,
                ):
                    source_bucket = bucket(acquisition_source)
                    add(source_bucket, discovered=1)
                    if (
                        lead.lead_id in initial_by_id
                        or lead.source_url in initial_urls
                    ):
                        add(source_bucket, duplicates=1)
                    else:
                        add(source_bucket, unique=1)

        web_inputs = bootstrap.job_search_factory_inputs.authorized_web_search
        if authorized_web_search is not None and web_inputs is not None:
            await emit(LeadRefreshPhase.DISCOVERING)
            web_bucket = bucket(
                JobLeadSource.AUTHORIZED_WEB_SEARCH,
                "AUTHORIZED_WEB_SEARCH",
            )
            try:
                active_policy = policy_repository.get_active_policy(subject_id)
            except (OSError, RuntimeError, TypeError, ValueError):
                active_policy = None
            if active_policy is None:
                add(totals, failed=1)
                add(web_bucket, failed=1)
            else:
                initial_budget = web_inputs.max_search_requests
                resolution_budget = web_inputs.max_resolution_searches
                offsets = tuple(range(10))
                web_baseline = dict(totals)

                async def web_progress(
                    progress: JobLeadDiscoveryProgress,
                ) -> None:
                    await observe_web_discovery(
                        progress,
                        baseline=web_baseline,
                    )

                try:
                    web_summary = await discover_job_leads(
                        JobLeadDiscoveryCommand(
                            subject_id=subject_id,
                            invocation_id=f"{invocation_id}:web",
                            now=now,
                            count=web_inputs.results_per_request,
                            offsets=offsets,
                            country=web_inputs.country,
                            search_language=web_inputs.search_language,
                            default_lookback_days=web_inputs.lookback_days,
                            max_requests=initial_budget,
                            max_initial_requests=initial_budget,
                            max_canonical_searches=resolution_budget,
                            max_public_reads=min(
                                500,
                                (initial_budget + resolution_budget)
                                * web_inputs.results_per_request,
                            ),
                            max_hits=min(
                                10_000,
                                initial_budget
                                * web_inputs.results_per_request,
                            ),
                            max_unique_leads=min(
                                10_000,
                                initial_budget
                                * web_inputs.results_per_request,
                            ),
                            max_resolution_candidates=(
                                web_inputs.results_per_request
                            ),
                        ),
                        policy=active_policy,
                        web_search=authorized_web_search,
                        lead_repository=lead_repository,
                        public_job_reader=read_public_job,
                        subject_discovery=discovery,
                        configured_job_search=(
                            conversational_search_port
                            if search_ports.ports
                            else None
                        ),
                        progress_observer=web_progress,
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    web_summary = None
                if web_summary is None:
                    add(totals, failed=1)
                    add(
                        bucket(
                            JobLeadSource.AUTHORIZED_WEB_SEARCH,
                            "AUTHORIZED_WEB_SEARCH",
                        ),
                        failed=1,
                    )
                else:
                    sync_web_snapshot(web_summary, baseline=web_baseline)
                    resolved_job_ids.extend(web_summary.resolved_job_ids)

        await emit(LeadRefreshPhase.RESOLVING)
        try:
            before_resolution = lead_repository.list_current(subject_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            before_resolution = None
        if (
            before_resolution is None
            or before_resolution.status is not JobLeadListStatus.SUCCEEDED
        ):
            add(totals, failed=1)
            targets: tuple[JobLead, ...] = ()
        else:
            targets = tuple(
                lead
                for lead in before_resolution.leads
                if lead.status is JobLeadStatus.DISCOVERED
            )
        if targets:
            resolution_budget = (
                web_inputs.max_resolution_searches
                if web_inputs is not None
                else 0
            )
            resolution_web_search = (
                authorized_web_search if resolution_budget > 0 else None
            )
            try:
                persisted_summary = await resolve_persisted_job_leads(
                    JobLeadDiscoveryCommand(
                        subject_id=subject_id,
                        invocation_id=f"{invocation_id}:persisted",
                        now=now,
                        count=(
                            web_inputs.results_per_request
                            if web_inputs is not None
                            else 20
                        ),
                        country=(
                            web_inputs.country
                            if web_inputs is not None
                            else "CA"
                        ),
                        search_language=(
                            web_inputs.search_language
                            if web_inputs is not None
                            else "en"
                        ),
                        max_requests=max(1, resolution_budget * 2),
                        max_initial_requests=1,
                        max_canonical_searches=resolution_budget,
                        max_public_reads=min(
                            500,
                            len(targets)
                            + resolution_budget
                            * (
                                web_inputs.results_per_request
                                if web_inputs is not None
                                else 20
                            ),
                        ),
                        max_resolution_candidates=(
                            web_inputs.results_per_request
                            if web_inputs is not None
                            else 20
                        ),
                    ),
                    web_search=resolution_web_search,
                    lead_repository=lead_repository,
                    public_job_reader=read_public_job,
                    subject_discovery=discovery,
                    configured_job_search=(
                        conversational_search_port
                        if search_ports.ports
                        else None
                    ),
                    leads=targets,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                persisted_summary = None
            if persisted_summary is None:
                add(totals, failed=1)
            else:
                add(
                    totals,
                    requests=persisted_summary.requests,
                    completed=persisted_summary.completed,
                    resolved=persisted_summary.resolved,
                    needs_user=persisted_summary.needs_user,
                    failed=persisted_summary.failed,
                    public_reads=persisted_summary.public_reads,
                    truncated=persisted_summary.truncated,
                )
                resolved_job_ids.extend(
                    persisted_summary.resolved_job_ids
                )
                for item in persisted_summary.source_results:
                    resolution_bucket = bucket(
                        JobLeadSource.CANONICAL_RESOLUTION,
                        item.source.value,
                    )
                    add(
                        resolution_bucket,
                        requests=item.requests,
                        completed=item.completed,
                        search_hits=item.hits,
                        resolved=item.resolved,
                        needs_user=item.needs_user,
                        failed=item.failed,
                        public_reads=item.public_reads,
                        truncated=item.truncated,
                    )

        unique_resolved_job_ids = tuple(dict.fromkeys(resolved_job_ids))
        add(totals, priorities_requested=len(unique_resolved_job_ids))
        for job_id in unique_resolved_job_ids:
            try:
                priority_result = await single_job_priority(
                    SingleJobPriorityCommand(
                        subject_id=subject_id,
                        job_id=job_id,
                        now=now,
                    )
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                priority_result = None
            if (
                priority_result is not None
                and priority_result.status is SingleJobPriorityStatus.SUCCEEDED
            ):
                add(totals, priorities_refreshed=1)
            else:
                add(totals, priorities_failed=1)

        await emit(LeadRefreshPhase.COMPLETED)
        failed = int(totals["failed"])
        priorities_failed = int(totals["priorities_failed"])
        successful = bool(
            int(totals["completed"])
            or int(totals["discovered"])
            or int(totals["resolved"])
            or int(totals["needs_user"])
        )
        if not configured:
            status = LeadRefreshStatus.NOOP
        elif failed and not successful:
            status = LeadRefreshStatus.FAILED
        elif failed or priorities_failed or bool(totals["truncated"]):
            status = LeadRefreshStatus.PARTIAL_FAILURE
        else:
            status = LeadRefreshStatus.COMPLETED
        return LeadRefreshResult(
            status=status,
            requests=int(totals["requests"]),
            completed=int(totals["completed"]),
            discovered=int(totals["discovered"]),
            unique=int(totals["unique"]),
            duplicates=int(totals["duplicates"]),
            resolved=int(totals["resolved"]),
            needs_user=int(totals["needs_user"]),
            failed=failed,
            public_reads=int(totals["public_reads"]),
            priorities_requested=int(totals["priorities_requested"]),
            priorities_refreshed=int(totals["priorities_refreshed"]),
            priorities_failed=priorities_failed,
            truncated=bool(totals["truncated"]),
            source_results=source_result_values(),
        )

    async def runnable_queue(command: Any) -> Any:
        return await build_runnable_application_queue(
            command,
            priority_queue_reader=priority_queue,
            accepted_intent_repository=accepted_intent_repository,
        )

    async def single_plan_creation(command: Any) -> Any:
        if isinstance(command, CreateApplicationPlanCommand):
            preferred_plan_id = None
            try:
                current_execution = execution_queue(
                    subject_id=command.subject_id,
                    now=command.now,
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                current_execution = None
            if (
                current_execution is not None
                and current_execution.status
                is CurrentApplicationExecutionQueueStatus.SUCCEEDED
            ):
                preferred_plan_id = fact_stale_runtime_input_plans(
                    subject_id=command.subject_id,
                    execution_items=current_execution.items,
                ).get(command.job_id)
                if preferred_plan_id is None:
                    preferred_plan_id = _project_resumable_plan_ids_by_job(
                        current_execution.items
                    ).get(command.job_id)
            if preferred_plan_id is None:
                try:
                    current_attention = attention_queue(
                        subject_id=command.subject_id,
                        now=command.now,
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    current_attention = None
                if (
                    current_attention is not None
                    and current_attention.status
                    is HumanAttentionQueueStatus.SUCCEEDED
                ):
                    preferred_plan_id = (
                        _project_resumable_attention_plan_ids_by_job(
                            current_attention.items
                        ).get(command.job_id)
                    )
            if preferred_plan_id is not None:
                command = replace(
                    command,
                    preferred_application_plan_id=preferred_plan_id,
                )
        return await create_application_plan(
            command,
            runnable_queue_reader=runnable_queue,
            repository=plan_repository,
        )

    async def plan_creation(command: Any) -> Any:
        return await run_selective_batch_plan_creation(
            command,
            runnable_queue_reader=runnable_queue,
            single_job_plan_creator=single_plan_creation,
        )

    unsupported_claim_directive_repository = (
        PrivateHomeUnsupportedClaimCorrectionDirectiveRepository(
            bootstrap.private_home
        )
    )
    unsupported_claim_correction_provider = (
        UnsupportedClaimCorrectionDirectiveProvider(
            unsupported_claim_directive_repository
        )
    )
    preparation_dependencies = replace(
        bootstrap.preparation_stage_dependencies,
        resume_tailoring_correction_provider=(
            unsupported_claim_correction_provider
        ),
        cover_letter_correction_provider=(
            unsupported_claim_correction_provider
        ),
    )
    try:
        recipe = build_production_application_preparation_recipe(
            preparation_dependencies
        )
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.PREPARATION_UNAVAILABLE
        ) from None

    preparation_run_repository = _repo(
        bootstrap, "application_preparation_runs"
    )

    async def single_preparation(command: Any) -> Any:
        try:
            fact_snapshot = (
                preparation_dependencies
                .application_fact_provider.get_current(command.subject_id)
            )
            directive_ids: list[str] = []
            for material_kind in (
                FactQAMaterialKind.RESUME,
                FactQAMaterialKind.COVER_LETTER,
            ):
                directive_set = (
                    unsupported_claim_correction_provider.list_current(
                        subject_id=command.subject_id,
                        application_plan_id=command.application_plan_id,
                        material_kind=material_kind,
                    )
                )
                if not directive_set.succeeded:
                    raise RuntimeError(
                        "unsupported-claim correction snapshot is unavailable"
                    )
                directive_ids.extend(
                    item.directive_id for item in directive_set.directives
                )
            input_snapshot_hash = fact_snapshot.snapshot_content_hash
            if directive_ids:
                input_snapshot_hash = hashlib.sha256(
                    (
                        fact_snapshot.snapshot_content_hash
                        + "\0"
                        + "\0".join(sorted(directive_ids))
                    ).encode("utf-8")
                ).hexdigest()
            command = replace(
                command,
                input_snapshot_hash=input_snapshot_hash,
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass
        return await run_application_preparation(
            command,
            application_plan_repository=plan_repository,
            recipe=recipe,
            run_repository=preparation_run_repository,
        )

    finding_provider = RepositoryFactQABlockingFindingProvider(
        resume_repository=_repo(bootstrap, "resume_fact_qa"),
        cover_letter_repository=_repo(bootstrap, "cover_letter_fact_qa"),
    )
    material_correction_target_provider = MaterialCorrectionTargetProvider(
        repository=PrivateHomeMaterialCorrectionTargetRepository(
            bootstrap.private_home
        ),
        finding_provider=finding_provider,
        compilation_stopped_provider=_repo(
            bootstrap, "resume_compilation_stopped_sources"
        ),
        compilation_repository=_repo(bootstrap, "resume_compilations"),
        layout_repository=_repo(bootstrap, "resume_layout_revisions"),
    )

    def attention_queue(*, subject_id: str, now: datetime) -> Any:
        return build_current_human_attention_queue(
            subject_id=subject_id,
            now=now,
            run_repository=preparation_run_repository,
            application_plan_repository=plan_repository,
            answer_set_repository=_repo(bootstrap, "prepared_answer_sets"),
            fact_qa_finding_provider=finding_provider,
            material_correction_target_projector=(
                material_correction_target_provider
            ),
        )

    async def unsupported_claim_correction(command: Any) -> Any:
        return await resolve_unsupported_claim_correction(
            command,
            queue_reader=attention_queue,
            target_provider=material_correction_target_provider,
            directive_repository=unsupported_claim_directive_repository,
            preparation_callable=single_preparation,
            receipt_repository=UnsupportedClaimCorrectionReceiptRepository(
                bootstrap.private_home
            ),
        )

    async def batch_preparation(command: Any) -> Any:
        def replay_superseded_p2_attention(
            plan: Any, attention_items: tuple[Any, ...]
        ) -> bool:
            return bool(attention_items) and all(
                getattr(item, "application_plan_id", None) == plan.plan_id
                and getattr(item, "job_id", None) == plan.job_id
                and getattr(item, "blocking", False)
                and _p2_attention_is_migration_retryable(item)
                for item in attention_items
            )

        return await run_selective_batch_preparation(
            command,
            application_plan_repository=plan_repository,
            human_attention_queue_reader=attention_queue,
            single_job_preparation=single_preparation,
            current_attention_replay_policy=(
                replay_superseded_p2_attention
            ),
        )

    verified_profile_repository = _repo(
        bootstrap, "verified_execution_profiles"
    )
    execution_policy_repository = _repo(
        bootstrap, "plan_execution_policy_decisions"
    )

    def profile_projector(command: Any) -> Any:
        return project_verified_application_execution_profile(
            command,
            plan_repository=plan_repository,
            fact_repository=_repo(bootstrap, "candidate_identity_facts"),
            repository=verified_profile_repository,
        )

    def policy_decider(command: Any) -> Any:
        return decide_plan_execution_policy(
            command,
            plan_provider=plan_repository,
            job_provider=job_repository,
            accepted_intent_provider=accepted_intent_repository,
            priority_decision_provider=priority_decision_repository,
            prioritization_policy_provider=policy_repository,
            execution_rules=bootstrap.execution_policy_rules,
            repository=execution_policy_repository,
        )

    def context_loader(command: Any) -> Any:
        return load_application_assembly_execution_context(
            command,
            verified_profile_provider=verified_profile_repository,
            execution_policy_provider=execution_policy_repository,
        )

    async def context_binder(command: Any) -> Any:
        return await bind_plan_assembly_execution_context(
            command,
            plan_provider=plan_repository,
            verified_profile_projector=profile_projector,
            execution_policy_decider=policy_decider,
            execution_context_loader=context_loader,
            repository=_repo(
                bootstrap, "plan_execution_context_bindings"
            ),
        )

    try:
        bundle_factory = build_production_application_bundle_factory()
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.BUNDLE_FACTORY_UNAVAILABLE
        ) from None

    assembly_repository = _repo(
        bootstrap, "application_bundle_assemblies"
    )
    bundle_envelope_repository = _repo(
        bootstrap, "recoverable_application_bundles"
    )

    def assemble_bundle(command: Any) -> Any:
        return assemble_application_bundle(
            command,
            application_plan_repository=plan_repository,
            job_posting_repository=job_repository,
            plan_material_manifest_repository=_repo(
                bootstrap, "plan_material_manifests"
            ),
            answer_set_repository=_repo(bootstrap, "prepared_answer_sets"),
            verified_execution_profile_provider=verified_profile_repository,
            plan_execution_policy_provider=execution_policy_repository,
            bundle_factory=bundle_factory,
            assembly_repository=assembly_repository,
            bundle_envelope_repository=bundle_envelope_repository,
            private_home=bootstrap.private_home,
        )

    async def selective_bundle_assembly(command: Any) -> Any:
        return await run_selective_bundle_assembly(
            command,
            plan_execution_context_binder=context_binder,
            assemble_application_bundle=assemble_bundle,
        )

    try:
        application_engine = JobApplicationEngine.from_private_home(
            home=bootstrap.private_home,
            credential_store=bootstrap.credential_store,
        )
        token_store = OpaquePermitTokenStore(bootstrap.credential_store)
        permit_policy = SubmissionPermitPolicy.v1()
        browser_lease_provider = bootstrap.browser_runtime.lease_provider()
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.EXECUTION_UNAVAILABLE
        ) from None

    non_submit_repository = _repo(bootstrap, "non_submit_executions")
    submission_authorization_repository = _repo(
        bootstrap, "submission_authorizations"
    )
    submission_permit_repository = _repo(
        bootstrap, "submission_permits"
    )

    async def non_submit_execution(command: Any) -> Any:
        return await execute_non_submit_application(
            command,
            application_plan_repository=plan_repository,
            assembly_repository=assembly_repository,
            bundle_envelope_repository=bundle_envelope_repository,
            job_posting_repository=job_repository,
            browser_lease_provider=browser_lease_provider,
            application_engine=application_engine,
            execution_repository=non_submit_repository,
            private_home=bootstrap.private_home,
            execution_metadata=NonSubmitExecutionMetadata.default(),
        )

    def gate_b_authorization(command: Any) -> Any:
        return decide_submission_authorization(
            command,
            application_plan_repository=plan_repository,
            non_submit_execution_repository=non_submit_repository,
            bundle_envelope_repository=bundle_envelope_repository,
            submission_authorization_repository=(
                submission_authorization_repository
            ),
        )

    def permit_issuance(command: Any) -> Any:
        return issue_submission_permit(
            command,
            application_plan_repository=plan_repository,
            submission_authorization_repository=(
                submission_authorization_repository
            ),
            non_submit_execution_repository=non_submit_repository,
            bundle_envelope_repository=bundle_envelope_repository,
            permit_service=application_engine.permits,
            token_store=token_store,
            permit_policy=permit_policy,
            submission_permit_repository=submission_permit_repository,
        )

    async def authorized_submission(command: Any) -> Any:
        return await execute_authorized_submission(
            command,
            submission_permit_repository=submission_permit_repository,
            submission_authorization_repository=(
                submission_authorization_repository
            ),
            non_submit_execution_repository=non_submit_repository,
            bundle_envelope_repository=bundle_envelope_repository,
            token_store=token_store,
            permit_service=application_engine.permits,
            browser_lease_provider=browser_lease_provider,
            application_engine=application_engine,
            execution_repository=_repo(
                bootstrap, "authorized_submission_executions"
            ),
            private_home=bootstrap.private_home,
            execution_metadata=(
                AuthorizedSubmissionExecutionMetadata.default()
            ),
        )

    async def single_execution(command: Any) -> Any:
        return await run_application_execution(
            command,
            assembly_repository=assembly_repository,
            non_submit_execution=non_submit_execution,
            gate_b_authorization=gate_b_authorization,
            submission_permit_issuance=permit_issuance,
            authorized_submission_execution=authorized_submission,
            run_repository=_repo(bootstrap, "application_execution_runs"),
        )

    def execution_queue(*, subject_id: str, now: datetime) -> Any:
        return build_current_application_execution_queue(
            subject_id=subject_id,
            now=now,
            assembly_repository=assembly_repository,
            execution_run_repository=_repo(
                bootstrap, "application_execution_runs"
            ),
            application_plan_repository=plan_repository,
        )

    def fact_stale_runtime_input_plans(
        *, subject_id: str, execution_items: tuple[Any, ...]
    ) -> dict[str, str]:
        return _project_fact_stale_runtime_input_plan_ids_by_job(
            execution_items,
            subject_id=subject_id,
            assembly_repository=assembly_repository,
            answer_set_repository=_repo(
                bootstrap, "prepared_answer_sets"
            ),
            fact_provider=(
                bootstrap.preparation_stage_dependencies
                .application_fact_provider
            ),
        )

    async def selective_execution(command: Any) -> Any:
        return await run_selective_batch_execution(
            command,
            execution_queue_reader=execution_queue,
            single_job_execution=single_execution,
        )

    async def automation_cycle(command: Any) -> Any:
        return await run_automation_cycle(
            command,
            priority_refresh=priority_refresh,
            plan_creation=plan_creation,
            preparation=batch_preparation,
            bundle_assembly=selective_bundle_assembly,
            execution=selective_execution,
            repository=_repo(bootstrap, "automation_cycle_runs"),
        )

    try:
        if not isinstance(
            bootstrap.authentication_session_provider,
            KeychainAuthenticatedSubjectSessionProvider,
        ):
            raise TypeError("authenticated session provider is unavailable")
        authenticated_subject = make_authenticated_subject_dependency(
            session_provider=bootstrap.authentication_session_provider,
            clock=active_clock,
        )
        local_session_controller = LocalDashboardSessionController(
            issuer=bootstrap.local_session_issuer,
            clock=active_clock,
        )
        refresh_controller = RefreshJobLibraryUIController(
            manual_refresh=manual_refresh,
            lead_refresh=job_lead_refresh,
            clock=active_clock,
            max_reprioritizations=(
                bootstrap.automation_runtime_policy.max_reprioritizations
            ),
        )
        search_profile_controller = SearchProfileUIController(
            repository=_repo(bootstrap, "search_profiles"),
            available_sources=tuple(search_ports.ports),
            source_companies={
                **{
                    next(
                        source
                        for source in search_ports.ports
                        if source.kind.value == "KNOWN_GREENHOUSE_BOARD"
                        and source.source_id == config.board_token
                    ): config.canonical_company
                    for config in bootstrap.job_search_factory_inputs.boards
                },
                **{
                    next(
                        source
                        for source in search_ports.ports
                        if source.kind.value == "KNOWN_ASHBY_BOARD"
                        and source.source_id == config.board_name
                    ): config.canonical_company
                    for config in (
                        bootstrap.job_search_factory_inputs.ashby_boards
                    )
                },
                **{
                    next(
                        source
                        for source in search_ports.ports
                        if source.kind.value == "KNOWN_LEVER_SITE"
                        and source.source_id == config.site_name
                    ): config.canonical_company
                    for config in (
                        bootstrap.job_search_factory_inputs.lever_sites
                    )
                },
                **(
                    {
                        next(
                            source
                            for source in search_ports.ports
                            if source.kind.value
                            == "GLASSDOOR_PARTNER_SEARCH"
                        ): None
                    }
                    if bootstrap.job_search_factory_inputs.glassdoor
                    is not None
                    else {}
                ),
                **{
                    next(
                        source
                        for source in search_ports.ports
                        if source.kind.value == "KNOWN_JOBVITE_FEED"
                        and source.source_id == config.career_site
                    ): config.canonical_company
                    for config in (
                        bootstrap.job_search_factory_inputs.jobvite_feeds
                    )
                },
            },
            clock=active_clock,
        )
        assisted_job_import_controller = AssistedJobImportController(
            public_job_reader=read_public_job,
            discovery=discovery,
            clock=active_clock,
            lead_repository=lead_repository,
        )
        conversational_job_finder_controller = (
            ConversationalJobFinderUIController(
                pending_store=InMemoryPendingIntakeStore(
                    ttl=timedelta(minutes=15)
                ),
                candidate_store=InMemoryCandidateSelectionStore(
                    ttl=timedelta(minutes=15)
                ),
                clue_extractor=named_job_clue_extractor,
                job_search_port=conversational_search_port,
                public_job_reader=read_public_job,
                accepted_intent_repository=accepted_intent_repository,
                discovery=discovery,
                clock=active_clock,
                assisted_import=assisted_job_import_controller,
            )
        )
        prioritization_policy_controller = PrioritizationPolicyUIController(
            service=PrioritizationPolicyService(
                interpreter=policy_interpreter,
                draft_store=InMemoryPrioritizationPolicyDraftStore(),
                repository=policy_repository,
                clock=active_clock,
            )
        )

        async def automation_preflight(
            *,
            context: Any,
            invocation_id: str,
            stop_requested: Callable[[], bool],
            progress_observer: AutomationPreflightProgressObserver,
        ) -> AutomationPreflightResult:
            """Bind the explicit start action to profile intent and refresh."""

            if stop_requested():
                return AutomationPreflightResult(
                    AutomationPreflightStatus.NOOP,
                    "Automatic application stopped before preflight work.",
                )

            try:
                resumable_snapshot = execution_queue(
                    subject_id=context.subject_id,
                    now=active_clock(),
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                resumable_snapshot = None
            if (
                resumable_snapshot is not None
                and resumable_snapshot.status
                is CurrentApplicationExecutionQueueStatus.SUCCEEDED
                and (
                    fact_stale_runtime_input_plans(
                        subject_id=context.subject_id,
                        execution_items=resumable_snapshot.items,
                    )
                    or _project_resumable_plan_ids_by_job(
                        resumable_snapshot.items
                    )
                )
            ):
                await progress_observer(
                    AutomationPreflightProgress(
                        "A prepared application can resume now; continuing it "
                        "before refreshing the job library."
                    )
                )
                return AutomationPreflightResult(
                    AutomationPreflightStatus.COMPLETED,
                    "Resuming a prepared application before library refresh.",
                )

            try:
                resumable_attention = attention_queue(
                    subject_id=context.subject_id,
                    now=active_clock(),
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                resumable_attention = None
            if (
                resumable_attention is not None
                and resumable_attention.status
                is HumanAttentionQueueStatus.SUCCEEDED
                and _project_resumable_attention_plan_ids_by_job(
                    resumable_attention.items
                )
            ):
                await progress_observer(
                    AutomationPreflightProgress(
                        "A P2 application can resume under the current "
                        "preparation policy; continuing it before refresh."
                    )
                )
                return AutomationPreflightResult(
                    AutomationPreflightStatus.COMPLETED,
                    "Resuming a P2 preparation before library refresh.",
                )

            await progress_observer(
                AutomationPreflightProgress(
                    "Recording automatic-application intent before refresh."
                )
            )

            intent_result = (
                enable_auto_request_application_for_enabled_search_profiles(
                    EnableAutoRequestApplicationBatchCommand(
                        subject_id=context.subject_id,
                        now=active_clock(),
                    ),
                    search_profile_repository=_repo(
                        bootstrap, "search_profiles"
                    ),
                    policy_repository=_repo(
                        bootstrap, "search_profile_intent_policies"
                    ),
                )
            )
            if (
                intent_result.status
                is EnableAutoRequestApplicationBatchStatus.FAILED
            ):
                return AutomationPreflightResult(
                    AutomationPreflightStatus.FAILED,
                    "Automatic application intent could not be recorded safely.",
                )
            if stop_requested():
                return AutomationPreflightResult(
                    AutomationPreflightStatus.NOOP,
                    "Automatic application stopped after recording intent.",
                )

            await progress_observer(
                AutomationPreflightProgress(
                    "Automatic-application intent is ready. Starting the "
                    "job-library refresh."
                )
            )

            refresh_digest = hashlib.sha256(
                invocation_id.encode("utf-8")
            ).hexdigest()
            refresh_invocation_id = f"automation-refresh-{refresh_digest}"
            refresh_started_at = asyncio.get_running_loop().time()

            async def publish_refresh_progress(result: Any) -> None:
                base = (
                    result.message
                    if isinstance(result.message, str)
                    and result.message.strip()
                    else "The job-library refresh is still running."
                )
                elapsed = max(
                    0,
                    int(
                        asyncio.get_running_loop().time()
                        - refresh_started_at
                    ),
                )
                message = (
                    f"{base.strip()} Preflight refresh has been running for "
                    f"{elapsed} seconds."
                )
                await progress_observer(
                    AutomationPreflightProgress(message[:1000])
                )

            try:
                async with asyncio.timeout(
                    active_preflight_refresh_timeout
                ):
                    while True:
                        if stop_requested():
                            return AutomationPreflightResult(
                                AutomationPreflightStatus.NOOP,
                                "Automatic application stopped while the job "
                                "library refresh continues safely in the "
                                "background.",
                            )
                        refresh_result = await refresh_controller.start(
                            context=context,
                            command=RefreshJobLibraryUICommand(
                                invocation_id=refresh_invocation_id
                            ),
                        )
                        await publish_refresh_progress(refresh_result)
                        if (
                            refresh_result.status
                            is not RefreshJobLibraryUIStatus.RUNNING
                        ):
                            break
                        while True:
                            if stop_requested():
                                return AutomationPreflightResult(
                                    AutomationPreflightStatus.NOOP,
                                    "Automatic application stopped while the "
                                    "job library refresh continues safely in "
                                    "the background.",
                                )
                            await asyncio.sleep(0.1)
                            active_refresh = await refresh_controller.status(
                                context=context
                            )
                            await publish_refresh_progress(active_refresh)
                            if (
                                active_refresh.status
                                is not RefreshJobLibraryUIStatus.RUNNING
                            ):
                                break
                        if (
                            active_refresh.invocation_id
                            == refresh_invocation_id
                        ):
                            refresh_result = active_refresh
                            break
            except TimeoutError:
                if stop_requested():
                    return AutomationPreflightResult(
                        AutomationPreflightStatus.NOOP,
                        "Automatic application stopped as the job-library "
                        "refresh reached its preflight deadline.",
                    )
                return AutomationPreflightResult(
                    AutomationPreflightStatus.FAILED,
                    "The job-library refresh exceeded the automatic-application "
                    "preflight deadline. The refresh may still finish safely in "
                    "the background; start automatic applications again after "
                    "the refresh status is terminal.",
                )

            if refresh_result.status is RefreshJobLibraryUIStatus.FAILED:
                return AutomationPreflightResult(
                    AutomationPreflightStatus.FAILED,
                    refresh_result.message
                    or "The job library could not be refreshed safely.",
                )
            partial = (
                intent_result.status
                is EnableAutoRequestApplicationBatchStatus.PARTIAL_FAILURE
                or refresh_result.status
                is RefreshJobLibraryUIStatus.PARTIAL_FAILURE
            )
            return AutomationPreflightResult(
                AutomationPreflightStatus.PARTIAL_FAILURE
                if partial
                else AutomationPreflightStatus.NOOP
                if (
                    intent_result.status
                    is EnableAutoRequestApplicationBatchStatus.NOOP
                    and refresh_result.status
                    is RefreshJobLibraryUIStatus.NOOP
                )
                else AutomationPreflightStatus.COMPLETED,
                (
                    "Some job sources could not be refreshed; completed "
                    "sources remain eligible."
                    if partial
                    else None
                ),
            )

        async def automation_work_snapshot(
            *, subject_id: str, now: datetime
        ) -> tuple[str, ...]:
            queue = await runnable_queue(
                RunnableApplicationQueueCommand(
                    subject_id=subject_id,
                    now=now,
                )
            )
            if queue.status is not RunnableApplicationQueueStatus.SUCCEEDED:
                raise RuntimeError("runnable application queue is unavailable")
            current_execution = execution_queue(
                subject_id=subject_id,
                now=now,
            )
            if (
                current_execution.status
                is not CurrentApplicationExecutionQueueStatus.SUCCEEDED
            ):
                raise RuntimeError("application execution queue is unavailable")
            attention = attention_queue(subject_id=subject_id, now=now)
            if attention.status is HumanAttentionQueueStatus.FAILED:
                raise RuntimeError("human attention queue is unavailable")
            return _project_automation_work_ids(
                runnable_items=queue.runnable_items,
                execution_items=current_execution.items,
                attention_items=attention.items,
                fact_stale_runtime_input_plan_ids_by_job=(
                    fact_stale_runtime_input_plans(
                        subject_id=subject_id,
                        execution_items=current_execution.items,
                    )
                ),
            )

        automation_controller = ContinueAutomationUIController(
            automation_cycle=automation_cycle,
            preflight=automation_preflight,
            work_snapshot=automation_work_snapshot,
            clock=active_clock,
            budgets=bootstrap.automation_runtime_policy,
        )
        human_attention_controller = HumanAttentionInboxUIController(
            queue_reader=attention_queue,
            clock=active_clock,
        )
        unsupported_claim_correction_controller = (
            UnsupportedClaimCorrectionUIController(
                correction_callable=unsupported_claim_correction,
                clock=active_clock,
            )
        )
        profile_reader = DashboardCandidateProfileReader(
            fact_repository=_repo(bootstrap, "candidate_identity_facts"),
            source_provider=_repo(
                bootstrap, "candidate_information_sources"
            ),
            search_profile_provider=_repo(bootstrap, "search_profiles"),
        )
        jobs_reader = DashboardJobsReader(
            runnable_queue_reader=runnable_queue,
            application_plan_repository=plan_repository,
            subject_job_reader=subject_job_reader,
            job_lead_repository=lead_repository,
        )
        applications_reader = DashboardApplicationsReader(
            application_plan_repository=plan_repository,
            preparation_run_repository=preparation_run_repository,
            human_attention_reader=attention_queue,
            execution_queue_reader=execution_queue,
            job_posting_reader=job_repository,
        )
        overview_reader = DashboardOverviewReader(
            profile_reader=profile_reader,
            jobs_reader=jobs_reader,
            applications_reader=applications_reader,
            human_attention_reader=attention_queue,
        )
        profile_controller = DashboardProfileController(
            reader=profile_reader, clock=active_clock
        )
        jobs_controller = DashboardJobsController(
            reader=jobs_reader, clock=active_clock
        )
        applications_controller = DashboardApplicationsController(
            reader=applications_reader, clock=active_clock
        )
        application_review_submission_controller = (
            ApplicationReviewSubmissionUIController(
                execution_queue_reader=execution_queue,
                execution_run_repository=_repo(
                    bootstrap, "application_execution_runs"
                ),
                non_submit_execution_repository=non_submit_repository,
                assembly_repository=assembly_repository,
                answer_set_repository=_repo(
                    bootstrap, "prepared_answer_sets"
                ),
                job_posting_repository=job_repository,
                single_job_execution=single_execution,
                clock=active_clock,
            )
        )
        async def submit_compatible_review(args: Any) -> Any:
            from jobctl import submit_reviewed_application

            return await submit_reviewed_application(
                args,
                credential_store=bootstrap.credential_store,
                browser_lease_provider=browser_lease_provider,
            )

        async def refresh_compatible_review(args: Any) -> Any:
            from jobctl import submit_reviewed_application

            return await submit_reviewed_application(
                args,
                credential_store=bootstrap.credential_store,
                browser_lease_provider=browser_lease_provider,
                request_submit=False,
            )

        reviewed_application_compatibility_controller = (
            ReviewedApplicationCompatibilityUIController(
                home=bootstrap.private_home,
                subject_id=(
                    bootstrap.config.authentication.local_subject_id
                ),
                submit_reviewed=submit_compatible_review,
                refresh_reviewed=refresh_compatible_review,
                headless=bootstrap.config.browser.headless,
                lease_ttl_seconds=(
                    bootstrap.config.browser.lease_ttl_seconds
                ),
            )
        )
        overview_controller = DashboardOverviewController(
            reader=overview_reader, clock=active_clock
        )
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.CONTROLLER_UNAVAILABLE
        ) from None

    diagnostics = MappingProxyType(
        {
            "composition_contract_version": (
                PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION
            ),
            "enabled_controller_ids": (
                "job-library-refresh",
                "search-profile",
                "assisted-job-import",
                "conversational-job-finder",
                "prioritization-policy",
                "automation-cycle",
                "human-attention",
                "dashboard-profile",
                "dashboard-jobs",
                "dashboard-applications",
                "application-review-submission",
                "reviewed-application-compatibility",
                "dashboard-overview",
                "local-authenticated-session",
            ),
            "search_provider_ids": tuple(
                capability.provider_id
                for capability in search_ports.capabilities
                if capability.status.value == "SUPPORTED"
            ),
            "authorized_web_search": (
                "BRAVE" if authorized_web_search is not None else "DISABLED"
            ),
            "job_alert_inbox": (
                "ENABLED" if job_alert_ingestor is not None else "DISABLED"
            ),
            "job_lead_channels": (
                "AUTHORIZED_WEB_SEARCH",
                "LINKEDIN_ALERT_EMAIL",
                "INDEED_ALERT_EMAIL",
                "EMPLOYER_OR_ATS_ALERT_EMAIL",
                "WEB_CLIPPER",
                "PASTED_URL",
            ),
            "priority_backend_id": (
                priority_agent.call_metadata.backend_id
            ),
            "priority_model_id": priority_agent.call_metadata.model_id,
            "priority_prompt_version": (
                priority_agent.call_metadata.prompt_policy_version
            ),
            "priority_schema_version": (
                priority_agent.call_metadata.schema_version
            ),
            "preparation_stage_ids": tuple(
                definition.stage.value for definition in recipe.stages
            ),
            "bundle_factory_type": type(bundle_factory).__name__,
            "verified_profile_provider_type": (
                type(verified_profile_repository).__name__
            ),
            "execution_policy_provider_type": (
                type(execution_policy_repository).__name__
            ),
            "context_binder_type": "public-p2c10b1-binder",
        }
    )
    return ProductionAutomationComposition(
        refresh_job_library_controller=refresh_controller,
        search_profile_controller=search_profile_controller,
        assisted_job_import_controller=assisted_job_import_controller,
        conversational_job_finder_controller=(
            conversational_job_finder_controller
        ),
        prioritization_policy_controller=(
            prioritization_policy_controller
        ),
        continue_automatic_application_controller=automation_controller,
        human_attention_controller=human_attention_controller,
        unsupported_claim_correction_controller=(
            unsupported_claim_correction_controller
        ),
        dashboard_profile_controller=profile_controller,
        dashboard_jobs_controller=jobs_controller,
        dashboard_applications_controller=applications_controller,
        application_review_submission_controller=(
            application_review_submission_controller
        ),
        reviewed_application_compatibility_controller=(
            reviewed_application_compatibility_controller
        ),
        dashboard_overview_controller=overview_controller,
        local_session_controller=local_session_controller,
        authenticated_subject_dependency=authenticated_subject,
        production_job_search_ports=search_ports,
        production_priority_agent=priority_agent,
        priority_agent_metadata=priority_agent.metadata,
        preparation_agent_adapters=(
            bootstrap.preparation_stage_dependencies.agents
        ),
        application_preparation_recipe=recipe,
        selective_batch_preparation_callable=batch_preparation,
        verified_profile_projector=profile_projector,
        verified_profile_provider=verified_profile_repository,
        execution_policy_decider=policy_decider,
        execution_policy_provider=execution_policy_repository,
        execution_context_binder=context_binder,
        application_bundle_factory=bundle_factory,
        application_bundle_assembly_callable=assemble_bundle,
        selective_bundle_assembly_callable=selective_bundle_assembly,
        current_execution_queue_callable=execution_queue,
        selective_execution_callable=selective_execution,
        automation_cycle_callable=automation_cycle,
        owned_resources=bootstrap.owned_resources,
        safe_diagnostics=diagnostics,
    )


__all__ = [
    "DEFAULT_AUTOMATION_PREFLIGHT_REFRESH_TIMEOUT_SECONDS",
    "PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION",
    "ProductionAutomationComposition",
    "ProductionAutomationCompositionError",
    "ProductionAutomationCompositionFailure",
    "build_production_automation_composition",
]
