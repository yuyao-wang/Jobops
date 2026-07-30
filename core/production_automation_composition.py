"""Authoritative production automation composition for P2c10c.

The root constructs and connects existing public business callables.  It does
not execute any stage while being built and owns no business decision logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from functools import partial
from types import MappingProxyType
from typing import Any, Callable, Mapping

from adapters.preparation_agents import ProductionPreparationAgentAdapters
from core.application_assembly_execution_context import (
    load_application_assembly_execution_context,
)
from core.application_bundle_assembly import assemble_application_bundle
from core.application_engine import JobApplicationEngine
from core.application_execution_orchestrator import run_application_execution
from core.application_plan import create_application_plan
from core.application_preparation_orchestrator import run_application_preparation
from core.authenticated_subject import KeychainAuthenticatedSubjectSessionProvider
from core.authorized_submission_execution import (
    AuthorizedSubmissionExecutionMetadata,
    execute_authorized_submission,
)
from core.automation_cycle import run_automation_cycle
from core.current_application_execution_queue import (
    build_current_application_execution_queue,
)
from core.current_priority_queue import build_current_priority_queue
from core.dashboard_read_models import (
    DashboardApplicationsReader,
    DashboardCandidateProfileReader,
    DashboardJobsReader,
    DashboardOverviewReader,
)
from core.fact_qa_findings import RepositoryFactQABlockingFindingProvider
from core.human_attention_queue import build_current_human_attention_queue
from core.subject_job_discovery import build_subject_job_discovery
from core.subject_job_library import SubjectScopedJobPostingReader
from core.job_discovery import build_production_job_discovery
from core.job_library_refresh import (
    ConfiguredSearchProfileExecutor,
    refresh_job_library,
)
from core.non_submit_application_execution import (
    NonSubmitExecutionMetadata,
    execute_non_submit_application,
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
from core.runnable_application_queue import build_runnable_application_queue
from core.selective_batch_execution import run_selective_batch_execution
from core.selective_batch_plan_creation import (
    run_selective_batch_plan_creation,
)
from core.selective_batch_preparation import run_selective_batch_preparation
from core.selective_bundle_assembly import run_selective_bundle_assembly
from core.selective_reprioritization import selectively_reprioritize_jobs
from core.single_job_priority import orchestrate_single_job_priority
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
    make_authenticated_subject_dependency,
)
from dashboard.automation_cycle import ContinueAutomationUIController
from dashboard.human_attention_inbox import HumanAttentionInboxUIController
from dashboard.job_library_refresh import RefreshJobLibraryUIController
from dashboard.read_models import (
    DashboardApplicationsController,
    DashboardJobsController,
    DashboardOverviewController,
    DashboardProfileController,
)
from source_connectors.greenhouse_board import (
    HttpxBoundedJobSearchHttpClient,
)
from source_connectors.production_job_search import (
    ProductionJobSearchPorts,
    build_production_job_search_ports,
)
from source_connectors.public_reader import read_public_job


PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION = (
    "production-automation-composition-v2"
)


class ProductionAutomationCompositionFailure(StrEnum):
    BOOTSTRAP_INCOMPATIBLE = "BOOTSTRAP_INCOMPATIBLE"
    SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
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
    continue_automatic_application_controller: ContinueAutomationUIController
    human_attention_controller: HumanAttentionInboxUIController
    dashboard_profile_controller: DashboardProfileController
    dashboard_jobs_controller: DashboardJobsController
    dashboard_applications_controller: DashboardApplicationsController
    dashboard_overview_controller: DashboardOverviewController
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
            (self.human_attention_controller, HumanAttentionInboxUIController),
            (self.dashboard_profile_controller, DashboardProfileController),
            (self.dashboard_jobs_controller, DashboardJobsController),
            (
                self.dashboard_applications_controller,
                DashboardApplicationsController,
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
            automation_controller=self.continue_automatic_application_controller,
            authenticated_subject=self.authenticated_subject_dependency,
            owned_resources=self.owned_resources,
            composition_diagnostics=self.safe_diagnostics,
            human_attention_controller=self.human_attention_controller,
            dashboard_profile_controller=self.dashboard_profile_controller,
            dashboard_jobs_controller=self.dashboard_jobs_controller,
            dashboard_applications_controller=(
                self.dashboard_applications_controller
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

    try:
        search_ports = build_production_job_search_ports(
            boards=bootstrap.job_search_factory_inputs.boards,
            http_port=HttpxBoundedJobSearchHttpClient(),
            policy=bootstrap.job_search_factory_inputs.policy,
        )
        search_executor = ConfiguredSearchProfileExecutor(search_ports.ports)
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.SEARCH_UNAVAILABLE
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
    )

    async def runnable_queue(command: Any) -> Any:
        return await build_runnable_application_queue(
            command,
            priority_queue_reader=priority_queue,
            accepted_intent_repository=accepted_intent_repository,
        )

    async def single_plan_creation(command: Any) -> Any:
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

    try:
        recipe = build_production_application_preparation_recipe(
            bootstrap.preparation_stage_dependencies
        )
    except Exception:
        raise ProductionAutomationCompositionError(
            ProductionAutomationCompositionFailure.PREPARATION_UNAVAILABLE
        ) from None

    preparation_run_repository = _repo(
        bootstrap, "application_preparation_runs"
    )

    async def single_preparation(command: Any) -> Any:
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

    def attention_queue(*, subject_id: str, now: datetime) -> Any:
        return build_current_human_attention_queue(
            subject_id=subject_id,
            now=now,
            run_repository=preparation_run_repository,
            application_plan_repository=plan_repository,
            answer_set_repository=_repo(bootstrap, "prepared_answer_sets"),
            fact_qa_finding_provider=finding_provider,
        )

    async def batch_preparation(command: Any) -> Any:
        return await run_selective_batch_preparation(
            command,
            application_plan_repository=plan_repository,
            human_attention_queue_reader=attention_queue,
            single_job_preparation=single_preparation,
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
        refresh_controller = RefreshJobLibraryUIController(
            manual_refresh=manual_refresh,
            clock=active_clock,
            max_reprioritizations=(
                bootstrap.automation_runtime_policy.max_reprioritizations
            ),
        )
        automation_controller = ContinueAutomationUIController(
            automation_cycle=automation_cycle,
            clock=active_clock,
            budgets=bootstrap.automation_runtime_policy,
        )
        human_attention_controller = HumanAttentionInboxUIController(
            queue_reader=attention_queue,
            clock=active_clock,
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
                "automation-cycle",
                "human-attention",
                "dashboard-profile",
                "dashboard-jobs",
                "dashboard-applications",
                "dashboard-overview",
            ),
            "search_provider_ids": tuple(
                capability.provider_id
                for capability in search_ports.capabilities
                if capability.status.value == "SUPPORTED"
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
        continue_automatic_application_controller=automation_controller,
        human_attention_controller=human_attention_controller,
        dashboard_profile_controller=profile_controller,
        dashboard_jobs_controller=jobs_controller,
        dashboard_applications_controller=applications_controller,
        dashboard_overview_controller=overview_controller,
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
    "PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION",
    "ProductionAutomationComposition",
    "ProductionAutomationCompositionError",
    "ProductionAutomationCompositionFailure",
    "build_production_automation_composition",
]
