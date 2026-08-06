"""Typed, repository-external production configuration and bootstrap.

This module deliberately stops before the P2c10c automation composition root.
It validates and constructs infrastructure dependencies without running search,
model generation, preparation, browser navigation, or execution.  Startup also
performs the bounded, idempotent legacy ResumeCandidate compatibility projection.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable

import yaml
from yaml.tokens import AliasToken, AnchorToken, TagToken

from adapters.preparation_agents import (
    ProductionPreparationAgentAdapters,
    build_production_preparation_agent_adapters,
)
from auth.credentials import CredentialStore, MacOSSecurityCredentialStore
from core.application_answers import (
    ApplicationAnswerPolicy,
    PrivateHomeApplicationFactProvider,
)
from core.authenticated_subject import (
    AUTHENTICATED_SUBJECT_SESSION_CONTRACT_VERSION,
    KeychainAuthenticatedSubjectSessionProvider,
    LocalAuthenticatedSubjectSessionIssuer,
    MAX_LOCAL_SESSION_TTL_SECONDS,
    MIN_LOCAL_SESSION_TTL_SECONDS,
)
from core.event_ledger import EventLedger
from core.latex_compiler import SandboxedPdfLatexCompiler
from core.legacy_resume_candidate_migration import (
    LegacyResumeCandidateMigrationError,
    migrate_hash_attested_legacy_resume_candidates,
)
from core.leases import LeaseManager
from core.managed_resume_template import DefaultManagedResumeTemplateProvider
from core.model_provider_capabilities import (
    ModelBackendResolutionError,
    PREPARATION_MODEL_COMPONENT_IDS,
    PRIORITY_MODEL_COMPONENT_ID,
)
from core.pdf_page_renderer import PdfiumPageRenderer
from core.plan_execution_policy import (
    PLAN_EXECUTION_POLICY_CONFIGURATION_CONTRACT_VERSION,
    PlanExecutionPolicyConfiguration,
    PlanExecutionPolicyRulesV1,
)
from core.policy import AutonomyMode, PolicyConfig
from core.prepared_cover_letter_material import (
    DefaultManagedCoverLetterTemplateProvider,
)
from core.private_home import PrivateHome, containing_git_worktree
from core.production_application_preparation_recipe import (
    ProductionPreparationStageDependencies,
)
from core.production_browser_runtime import (
    BROWSER_RUNTIME_CONFIG_CONTRACT_VERSION,
    BrowserRuntimeConfig,
    ProductionBrowserRuntime,
    build_production_browser_runtime,
    project_browser_runtime_config,
)
from core.production_priority_agent import build_production_priority_agent
from dashboard.automation_cycle import (
    AUTOMATION_CYCLE_UI_CONFIG_VERSION,
    AutomationCycleUIBudgetConfig,
)
from source_connectors.greenhouse_board import (
    JOB_SEARCH_EXECUTION_POLICY_VERSION,
    GreenhouseBoardConfig,
    JobSearchExecutionPolicy,
)
from source_connectors.provider_job_search import (
    AshbyBoardConfig,
    GlassdoorPartnerConfig,
    JobviteFeedConfig,
    LeverSiteConfig,
)


PRODUCTION_APPLICATION_CONFIG_CONTRACT_VERSION = (
    "production-application-config-v1"
)
PRODUCTION_APPLICATION_BOOTSTRAP_CONTRACT_VERSION = (
    "production-application-bootstrap-v1"
)
PRODUCTION_REPOSITORY_BUNDLE_CONTRACT_VERSION = (
    "production-repository-bundle-v1"
)
PRODUCTION_SEARCH_BOOTSTRAP_CONTRACT_VERSION = (
    "production-search-bootstrap-v1"
)
PRODUCTION_PREPARATION_RUNTIME_CONTRACT_VERSION = (
    "production-preparation-runtime-v1"
)
PRODUCTION_INFRASTRUCTURE_CONTRACT_VERSION = (
    "production-infrastructure-v1"
)
PRODUCTION_AUTHENTICATION_CONFIG_VERSION = (
    "production-authentication-runtime-v2"
)
PRODUCTION_EXECUTION_POLICY_RUNTIME_VERSION = (
    "production-execution-policy-runtime-v1"
)
JOBOPS_CONFIG_FILE_ENV = "JOBOPS_CONFIG_FILE"
MAX_CONFIG_BYTES = 256 * 1024
MAX_AUTOMATION_BUDGET = 100


class ProductionBootstrapFailure(StrEnum):
    CONFIG_NOT_FOUND = "CONFIG_NOT_FOUND"
    CONFIG_PERMISSION_INVALID = "CONFIG_PERMISSION_INVALID"
    CONFIG_SCHEMA_INVALID = "CONFIG_SCHEMA_INVALID"
    CONFIG_VERSION_UNSUPPORTED = "CONFIG_VERSION_UNSUPPORTED"
    PRIVATE_HOME_INVALID = "PRIVATE_HOME_INVALID"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    AUTH_CONFIGURATION_INVALID = "AUTH_CONFIGURATION_INVALID"
    SEARCH_CONFIGURATION_INVALID = "SEARCH_CONFIGURATION_INVALID"
    AI_CONFIGURATION_INVALID = "AI_CONFIGURATION_INVALID"
    PREPARATION_DEPENDENCY_MISSING = "PREPARATION_DEPENDENCY_MISSING"
    EXECUTION_POLICY_CONFIGURATION_INVALID = (
        "EXECUTION_POLICY_CONFIGURATION_INVALID"
    )
    BROWSER_CONFIGURATION_INVALID = "BROWSER_CONFIGURATION_INVALID"
    AUTOMATION_CONFIGURATION_INVALID = "AUTOMATION_CONFIGURATION_INVALID"
    RESOURCE_STARTUP_FAILED = "RESOURCE_STARTUP_FAILED"
    BOOTSTRAP_PARTIAL_FAILURE = "BOOTSTRAP_PARTIAL_FAILURE"


class ProductionApplicationBootstrapError(RuntimeError):
    """Typed startup failure with no secret, PII, payload, or absolute path."""

    def __init__(
        self,
        failure: ProductionBootstrapFailure,
        *,
        section: str | None = None,
    ) -> None:
        self.failure = ProductionBootstrapFailure(failure)
        self.section = section
        message = self.failure.value
        if section:
            message += f":{section}"
        super().__init__(message)


class SecretReferenceSource(StrEnum):
    ENV = "ENV"
    KEYCHAIN = "KEYCHAIN"
    CREDENTIAL_STORE = "CREDENTIAL_STORE"


@dataclass(frozen=True, slots=True)
class SecretReference:
    source: SecretReferenceSource
    name: str | None = None
    service: str | None = None
    account: str | None = None

    def __post_init__(self) -> None:
        raw_source = (
            self.source.upper() if isinstance(self.source, str) else self.source
        )
        object.__setattr__(
            self, "source", SecretReferenceSource(raw_source)
        )
        if self.source is SecretReferenceSource.ENV:
            if (
                not isinstance(self.name, str)
                or not self.name.strip()
                or self.service is not None
                or self.account is not None
            ):
                raise ValueError("ENV secret reference is invalid")
        elif (
            not isinstance(self.service, str)
            or not self.service.strip()
            or not isinstance(self.account, str)
            or not self.account.strip()
            or self.name is not None
        ):
            raise ValueError("credential secret reference is invalid")

    def resolve(
        self,
        *,
        environ: Mapping[str, str],
        credential_store: CredentialStore,
    ) -> str:
        if self.source is SecretReferenceSource.ENV:
            value = environ.get(self.name or "")
        else:
            value = credential_store.get(
                self.service or "", self.account or ""
            )
        if not isinstance(value, str) or not value:
            raise ProductionApplicationBootstrapError(
                ProductionBootstrapFailure.SECRET_UNAVAILABLE
            )
        return value

    def safe_dict(self) -> dict[str, str]:
        return {"source": self.source.value}


@dataclass(frozen=True, slots=True)
class PrivateHomeConfig:
    root: Path
    schema_version: str
    create_if_missing: bool
    permissions_policy: str

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("private home must be an absolute path")
        if self.schema_version != "private-home-v1":
            raise ValueError("private home schema is unsupported")
        if self.create_if_missing is not True:
            raise ValueError("production bootstrap requires managed creation")
        if self.permissions_policy != "OWNER_ONLY":
            raise ValueError("private home permissions policy is unsupported")


@dataclass(frozen=True, slots=True)
class AuthenticationRuntimeConfig:
    provider_id: str
    session_contract_version: str
    session_secret_ref: SecretReference
    cookie_policy: str
    local_subject_id: str
    local_subject_binding_policy: str
    local_session_issuance_policy: str
    session_ttl_seconds: int
    trusted_proxy_policy: str
    contract_version: str = PRODUCTION_AUTHENTICATION_CONFIG_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PRODUCTION_AUTHENTICATION_CONFIG_VERSION:
            raise ValueError("authentication config version is unsupported")
        if self.provider_id != "KEYCHAIN_AUTHENTICATED_SESSION":
            raise ValueError("authentication provider is unsupported")
        if (
            self.session_contract_version
            != AUTHENTICATED_SUBJECT_SESSION_CONTRACT_VERSION
        ):
            raise ValueError("session contract version is unsupported")
        if not isinstance(self.session_secret_ref, SecretReference):
            raise TypeError("session_secret_ref must be typed")
        if self.cookie_policy != "HTTP_ONLY_SAME_SITE_STRICT":
            raise ValueError("cookie policy is unsupported")
        if (
            not isinstance(self.local_subject_id, str)
            or not self.local_subject_id.strip()
            or len(self.local_subject_id.strip()) > 160
        ):
            raise ValueError("local subject ID is invalid")
        if self.local_subject_binding_policy != "SESSION_RECORD":
            raise ValueError("subject binding policy is unsupported")
        if self.local_session_issuance_policy != (
            "LOOPBACK_SAME_ORIGIN_AUTO"
        ):
            raise ValueError("local session issuance policy is unsupported")
        if (
            type(self.session_ttl_seconds) is not int
            or not MIN_LOCAL_SESSION_TTL_SECONDS
            <= self.session_ttl_seconds
            <= MAX_LOCAL_SESSION_TTL_SECONDS
        ):
            raise ValueError("session TTL is outside policy")
        if self.trusted_proxy_policy != "LOOPBACK_ONLY":
            raise ValueError("trusted proxy policy is unsupported")
        object.__setattr__(
            self, "local_subject_id", self.local_subject_id.strip()
        )


@dataclass(frozen=True, slots=True)
class GlassdoorPartnerRuntimeConfig:
    source_id: str
    partner_id_ref: SecretReference
    partner_key_ref: SecretReference
    user_ip: str

    def __post_init__(self) -> None:
        if not isinstance(self.partner_id_ref, SecretReference) or not isinstance(
            self.partner_key_ref, SecretReference
        ):
            raise TypeError("Glassdoor secret references must be typed")
        if not self.source_id.strip() or not self.user_ip.strip():
            raise ValueError("Glassdoor runtime config is invalid")


@dataclass(frozen=True, slots=True)
class JobviteFeedRuntimeConfig:
    canonical_company: str
    career_site: str
    aliases: tuple[str, ...]
    api_key_ref: SecretReference
    api_secret_ref: SecretReference

    def __post_init__(self) -> None:
        if not isinstance(self.api_key_ref, SecretReference) or not isinstance(
            self.api_secret_ref, SecretReference
        ):
            raise TypeError("Jobvite secret references must be typed")
        if not self.canonical_company.strip() or not self.career_site.strip():
            raise ValueError("Jobvite feed config is invalid")


@dataclass(frozen=True, slots=True)
class AuthorizedWebSearchRuntimeConfig:
    """Optional, explicitly licensed search-index discovery configuration."""

    provider_id: str
    api_key_ref: SecretReference
    storage_rights_confirmed: bool
    country: str = "CA"
    search_language: str = "en"
    lookback_days: int = 14
    max_search_requests: int = 20
    results_per_request: int = 20
    max_resolution_searches: int = 20

    def __post_init__(self) -> None:
        if self.provider_id != "BRAVE":
            raise ValueError("authorized web search provider is unsupported")
        if not isinstance(self.api_key_ref, SecretReference):
            raise TypeError("web search secret reference must be typed")
        if self.storage_rights_confirmed is not True:
            raise ValueError("web search storage rights must be confirmed")
        if (
            not isinstance(self.country, str)
            or len(self.country) != 2
            or self.country.upper() != self.country
            or not self.country.isalpha()
        ):
            raise ValueError("web search country is invalid")
        if (
            not isinstance(self.search_language, str)
            or not 2 <= len(self.search_language) <= 5
            or self.search_language.casefold() != self.search_language
            or not self.search_language.isalpha()
        ):
            raise ValueError("web search language is invalid")
        for name, value, minimum, maximum in (
            ("lookback_days", self.lookback_days, 1, 90),
            ("max_search_requests", self.max_search_requests, 1, 200),
            ("results_per_request", self.results_per_request, 1, 20),
            ("max_resolution_searches", self.max_resolution_searches, 0, 200),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside the web search policy")


@dataclass(frozen=True, slots=True)
class JobAlertInboxRuntimeConfig:
    """Optional local, read-only mailbox scope for job-alert ingestion."""

    host: str
    recipient: str = field(repr=False)
    credential_ref: SecretReference = field(repr=False)
    mailbox: str = "JobOps Alerts"
    port: int = 993
    allowed_sender_domains: tuple[str, ...] = (
        "linkedin.com",
        "indeed.com",
    )
    trusted_authserv_ids: tuple[str, ...] = ()
    max_age_hours: int = 24
    max_messages: int = 25

    def __post_init__(self) -> None:
        if not isinstance(self.credential_ref, SecretReference):
            raise TypeError("job-alert credential reference must be typed")
        if self.credential_ref.source is SecretReferenceSource.ENV:
            raise ValueError("job-alert mailbox credentials must use Keychain")
        if not self.host.strip() or not self.recipient.strip():
            raise ValueError("job-alert mailbox identity is invalid")
        if (
            not isinstance(self.credential_ref.account, str)
            or self.credential_ref.account.casefold()
            != self.recipient.casefold()
        ):
            raise ValueError("job-alert credential account must match recipient")
        if not self.mailbox.strip() or len(self.mailbox) > 128:
            raise ValueError("job-alert mailbox name is invalid")
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("job-alert mailbox port is invalid")
        if not self.allowed_sender_domains or any(
            not isinstance(value, str)
            or not value
            or value != value.casefold()
            or len(value) > 253
            for value in self.allowed_sender_domains
        ):
            raise ValueError("job-alert sender allowlist is invalid")
        if not self.trusted_authserv_ids or any(
            not isinstance(value, str)
            or value != value.casefold()
            or len(value) > 253
            or len(value.split(".")) < 2
            or any(
                not label
                or len(label) > 63
                or not label[0].isalnum()
                or not label[-1].isalnum()
                or any(
                    not character.isascii()
                    or not (character.isalnum() or character == "-")
                    for character in label
                )
                for label in value.split(".")
            )
            for value in self.trusted_authserv_ids
        ):
            raise ValueError("job-alert trusted authentication service is invalid")
        if (
            type(self.max_age_hours) is not int
            or not 1 <= self.max_age_hours <= 24
            or type(self.max_messages) is not int
            or not 1 <= self.max_messages <= 25
        ):
            raise ValueError("job-alert mailbox bounds are invalid")


@dataclass(frozen=True, slots=True)
class ProductionSearchConfig:
    enabled_providers: tuple[str, ...]
    boards: tuple[GreenhouseBoardConfig, ...]
    ashby_boards: tuple[AshbyBoardConfig, ...]
    lever_sites: tuple[LeverSiteConfig, ...]
    glassdoor: GlassdoorPartnerRuntimeConfig | None
    jobvite_feeds: tuple[JobviteFeedRuntimeConfig, ...]
    authorized_web_search: AuthorizedWebSearchRuntimeConfig | None
    job_alert_inbox: JobAlertInboxRuntimeConfig | None
    policy: JobSearchExecutionPolicy
    contract_version: str = PRODUCTION_SEARCH_BOOTSTRAP_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PRODUCTION_SEARCH_BOOTSTRAP_CONTRACT_VERSION:
            raise ValueError("search bootstrap version is unsupported")
        provider_order = (
            "GREENHOUSE",
            "ASHBY",
            "LEVER",
            "GLASSDOOR",
            "JOBVITE",
        )
        if (
            tuple(
                provider
                for provider in provider_order
                if provider in self.enabled_providers
            )
            != self.enabled_providers
            or len(set(self.enabled_providers)) != len(self.enabled_providers)
        ):
            raise ValueError("search providers are invalid or out of order")
        configured = {
            "GREENHOUSE": bool(self.boards),
            "ASHBY": bool(self.ashby_boards),
            "LEVER": bool(self.lever_sites),
            "GLASSDOOR": self.glassdoor is not None,
            "JOBVITE": bool(self.jobvite_feeds),
        }
        if any(
            (provider in self.enabled_providers) is not enabled
            for provider, enabled in configured.items()
        ):
            raise ValueError("enabled providers and configured sources differ")
        if self.policy.allowed_providers != self.enabled_providers:
            raise ValueError("search provider policy is inconsistent")


@dataclass(frozen=True, slots=True)
class AIBackendRuntimeConfig:
    value: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.value, Mapping):
            raise TypeError("AI config must be a mapping")
        copied = _deep_plain_copy(self.value)
        if set(copied) != {"default_backend", "backends", "components"}:
            raise ValueError("AI config keys are invalid")
        if not isinstance(copied["default_backend"], str):
            raise ValueError("default backend is invalid")
        if not isinstance(copied["backends"], dict):
            raise ValueError("backend definitions are invalid")
        if not isinstance(copied["components"], dict):
            raise ValueError("component mappings are invalid")
        expected_components = {
            *PREPARATION_MODEL_COMPONENT_IDS,
            PRIORITY_MODEL_COMPONENT_ID,
        }
        if set(copied["components"]) != expected_components:
            raise ValueError("AI component mappings are incomplete or unknown")
        forbidden = {
            "api_key",
            "token",
            "password",
            "secret",
            "cookie",
        }
        if any(
            key.casefold() in forbidden
            for key in _walk_mapping_keys(copied)
        ):
            raise ValueError("AI config cannot contain inline secrets")
        object.__setattr__(self, "value", _deep_freeze(copied))

    def as_mapping(self) -> Mapping[str, Any]:
        return self.value


@dataclass(frozen=True, slots=True)
class PreparationRuntimeConfig:
    recipe_contract_version: str
    latex_engine: str
    compile_timeout_seconds: int
    renderer_dpi: int
    max_source_bytes: int
    max_material_bytes: int
    max_revisions: int
    contract_version: str = PRODUCTION_PREPARATION_RUNTIME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PRODUCTION_PREPARATION_RUNTIME_CONTRACT_VERSION:
            raise ValueError("preparation runtime version is unsupported")
        if not self.recipe_contract_version.strip():
            raise ValueError("recipe contract version is required")
        if self.latex_engine not in {"pdflatex", "xelatex"}:
            raise ValueError("LaTeX engine is unsupported")
        for name, value, maximum in (
            ("compile_timeout_seconds", self.compile_timeout_seconds, 600),
            ("renderer_dpi", self.renderer_dpi, 600),
            ("max_source_bytes", self.max_source_bytes, 20_000_000),
            ("max_material_bytes", self.max_material_bytes, 25_000_000),
            ("max_revisions", self.max_revisions, 10),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside policy")


@dataclass(frozen=True, slots=True)
class ExecutionPolicyRuntimeConfig:
    configuration_id: str
    configuration_version: int
    mode: AutonomyMode
    authority_configured: bool
    email_verification_agent_enabled: bool
    allow_keychain_login: bool
    allow_account_registration: bool
    rules_contract_version: str
    contract_version: str = PRODUCTION_EXECUTION_POLICY_RUNTIME_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", AutonomyMode(self.mode))
        if self.contract_version != PRODUCTION_EXECUTION_POLICY_RUNTIME_VERSION:
            raise ValueError("execution policy runtime version is unsupported")
        if (
            self.rules_contract_version
            != PLAN_EXECUTION_POLICY_CONFIGURATION_CONTRACT_VERSION
        ):
            raise ValueError("execution policy rules config is unsupported")
        if not self.configuration_id.strip():
            raise ValueError("configuration_id is required")
        if self.configuration_version < 1:
            raise ValueError("configuration_version must be positive")
        if self.authority_configured is not True:
            raise ValueError("submit authority must be explicitly configured")


@dataclass(frozen=True, slots=True)
class AutomationRuntimeConfig:
    budgets: AutomationCycleUIBudgetConfig
    rate_limit_policy: str

    def __post_init__(self) -> None:
        if self.rate_limit_policy != "SERIAL_BOUNDED_V1":
            raise ValueError("automation rate limit policy is unsupported")
        values = (
            self.budgets.max_reprioritizations,
            self.budgets.max_plan_creations,
            self.budgets.max_preparations,
            self.budgets.max_bundle_assemblies,
            self.budgets.max_executions,
        )
        if any(value > MAX_AUTOMATION_BUDGET for value in values):
            raise ValueError("automation budget exceeds server upper bound")


@dataclass(frozen=True, slots=True)
class InfrastructureRuntimeConfig:
    http_client_id: str
    repository_contract_version: str
    contract_version: str = PRODUCTION_INFRASTRUCTURE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PRODUCTION_INFRASTRUCTURE_CONTRACT_VERSION:
            raise ValueError("infrastructure contract version is unsupported")
        if self.http_client_id != "HTTPX_BOUNDED_V1":
            raise ValueError("HTTP client is unsupported")
        if (
            self.repository_contract_version
            != PRODUCTION_REPOSITORY_BUNDLE_CONTRACT_VERSION
        ):
            raise ValueError("repository contract version is unsupported")


@dataclass(frozen=True, slots=True)
class DiagnosticsRuntimeConfig:
    level: str = "SAFE"

    def __post_init__(self) -> None:
        if self.level != "SAFE":
            raise ValueError("only safe diagnostics are supported")


@dataclass(frozen=True, slots=True)
class ProductionApplicationConfig:
    private_home: PrivateHomeConfig
    authentication: AuthenticationRuntimeConfig
    search: ProductionSearchConfig
    ai: AIBackendRuntimeConfig
    preparation: PreparationRuntimeConfig
    execution_policy: ExecutionPolicyRuntimeConfig
    browser: BrowserRuntimeConfig
    automation: AutomationRuntimeConfig
    infrastructure: InfrastructureRuntimeConfig
    diagnostics: DiagnosticsRuntimeConfig
    config_contract_version: str = (
        PRODUCTION_APPLICATION_CONFIG_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if self.config_contract_version != (
            PRODUCTION_APPLICATION_CONFIG_CONTRACT_VERSION
        ):
            raise ValueError("production config version is unsupported")

    def safe_diagnostics(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "config_contract_version": self.config_contract_version,
                "auth_provider_id": self.authentication.provider_id,
                "search_provider_ids": self.search.enabled_providers,
                "authorized_web_search": (
                    self.search.authorized_web_search.provider_id
                    if self.search.authorized_web_search is not None
                    else "DISABLED"
                ),
                "job_alert_inbox": (
                    "ENABLED"
                    if self.search.job_alert_inbox is not None
                    else "DISABLED"
                ),
                "ai_default_backend": self.ai.value["default_backend"],
                "browser_engine": self.browser.browser_engine,
                "automation_budgets": {
                    "priority": self.automation.budgets.max_reprioritizations,
                    "plans": self.automation.budgets.max_plan_creations,
                    "preparation": self.automation.budgets.max_preparations,
                    "assembly": self.automation.budgets.max_bundle_assemblies,
                    "execution": self.automation.budgets.max_executions,
                },
            }
        )


@dataclass(frozen=True, slots=True)
class ProductionRepositoryBundle:
    repositories: Mapping[str, object]
    contract_version: str = PRODUCTION_REPOSITORY_BUNDLE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PRODUCTION_REPOSITORY_BUNDLE_CONTRACT_VERSION:
            raise ValueError("repository bundle version is unsupported")
        copied = dict(self.repositories)
        if not copied or any(value is None for value in copied.values()):
            raise ValueError("repository bundle is incomplete")
        object.__setattr__(self, "repositories", MappingProxyType(copied))

    def require(self, name: str) -> object:
        try:
            return self.repositories[name]
        except KeyError as exc:
            raise ProductionApplicationBootstrapError(
                ProductionBootstrapFailure.PREPARATION_DEPENDENCY_MISSING,
                section=name,
            ) from exc


@dataclass(frozen=True, slots=True)
class ProductionAuthorizedWebSearchFactoryInputs:
    api_key: str = field(repr=False)
    storage_rights_confirmed: bool
    country: str
    search_language: str
    lookback_days: int
    max_search_requests: int
    results_per_request: int
    max_resolution_searches: int


@dataclass(frozen=True, slots=True)
class ProductionJobSearchFactoryInputs:
    boards: tuple[GreenhouseBoardConfig, ...]
    ashby_boards: tuple[AshbyBoardConfig, ...]
    lever_sites: tuple[LeverSiteConfig, ...]
    glassdoor: GlassdoorPartnerConfig | None
    jobvite_feeds: tuple[JobviteFeedConfig, ...]
    policy: JobSearchExecutionPolicy
    http_client_id: str
    authorized_web_search: ProductionAuthorizedWebSearchFactoryInputs | None = None


@dataclass(frozen=True, slots=True)
class ProductionPriorityAgentFactoryInputs:
    ai_config: Mapping[str, Any]
    backend_registry: Mapping[str, type] | None
    isolation_profile_registry: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ProductionApplicationBootstrap:
    config: ProductionApplicationConfig
    private_home: PrivateHome
    credential_store: CredentialStore
    repository_bundle: ProductionRepositoryBundle
    authentication_session_provider: KeychainAuthenticatedSubjectSessionProvider
    local_session_issuer: LocalAuthenticatedSubjectSessionIssuer
    job_search_factory_inputs: ProductionJobSearchFactoryInputs
    priority_agent_factory_inputs: ProductionPriorityAgentFactoryInputs
    preparation_stage_dependencies: ProductionPreparationStageDependencies
    execution_policy_rules: PlanExecutionPolicyRulesV1
    browser_runtime: ProductionBrowserRuntime
    automation_runtime_policy: AutomationCycleUIBudgetConfig
    owned_resources: tuple[object, ...]
    safe_diagnostics: Mapping[str, Any]
    bootstrap_contract_version: str = (
        PRODUCTION_APPLICATION_BOOTSTRAP_CONTRACT_VERSION
    )
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    async def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        for resource in reversed(self.owned_resources):
            close = getattr(resource, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                # Shutdown remains best effort and never exposes resource data.
                continue


_REPOSITORY_SPECS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        "accepted_job_intents": (
            "core.accepted_job_intent",
            "PrivateHomeAcceptedJobIntentRepository",
        ),
        "search_profiles": (
            "core.search_profile",
            "PrivateHomeSearchProfileRepository",
        ),
        "search_profile_intent_policies": (
            "core.search_profile_intent_policy",
            "PrivateHomeSearchProfileIntentPolicyRepository",
        ),
        "job_library_refresh_runs": (
            "core.job_library_refresh",
            "PrivateHomeJobLibraryRefreshRunRepository",
        ),
        "job_leads": (
            "core.job_leads",
            "PrivateHomeJobLeadRepository",
        ),
        "subject_job_library_memberships": (
            "core.subject_job_library",
            "PrivateHomeSubjectJobLibraryMembershipRepository",
        ),
        "prioritization_policies": (
            "core.prioritization_policy",
            "PrivateHomePrioritizationPolicyRepository",
        ),
        "priority_decisions": (
            "core.job_prioritization",
            "PrivateHomePriorityDecisionRepository",
        ),
        "single_job_priority": (
            "core.single_job_priority",
            "PrivateHomeSingleJobPriorityRepository",
        ),
        "application_plans": (
            "core.application_plan",
            "PrivateHomeApplicationPlanRepository",
        ),
        "application_preparation_runs": (
            "core.application_preparation_orchestrator",
            "PrivateHomeApplicationPreparationRunRepository",
        ),
        "base_latex_selections": (
            "core.base_latex_selection",
            "PrivateHomeBaseLatexSelectionDecisionRepository",
        ),
        "candidate_evidence_snapshots": (
            "core.candidate_evidence",
            "PrivateHomeCandidateEvidenceSnapshotRepository",
        ),
        "candidate_identity_facts": (
            "core.candidate_identity_facts",
            "PrivateHomeCandidateIdentityFactRepository",
        ),
        "candidate_information_sources": (
            "core.candidate_information_sources",
            "PrivateHomeCandidateInformationSourceRepository",
        ),
        "cover_letter_drafts": (
            "core.cover_letter_draft",
            "PrivateHomeCoverLetterDraftRepository",
        ),
        "cover_letter_evidence": (
            "core.cover_letter_evidence",
            "PrivateHomeCoverLetterEvidenceSnapshotRepository",
        ),
        "cover_letter_fact_qa": (
            "core.cover_letter_fact_qa",
            "PrivateHomeCoverLetterFactQARepository",
        ),
        "job_postings": (
            "core.job_discovery",
            "PrivateHomeJobPostingRepository",
        ),
        "latex_versions": (
            "core.resume_latex_versions",
            "PrivateHomeResumeLatexVersionRepository",
        ),
        "plan_material_manifests": (
            "core.plan_material_manifest",
            "PrivateHomePlanMaterialManifestRepository",
        ),
        "prepared_answer_sets": (
            "core.application_answers",
            "PrivateHomePreparedApplicationAnswerSetRepository",
        ),
        "prepared_cover_letters": (
            "core.prepared_cover_letter_material",
            "PrivateHomePreparedCoverLetterMaterialRepository",
        ),
        "prepared_resumes": (
            "core.prepared_resume_material",
            "PrivateHomePreparedResumeMaterialRepository",
        ),
        "resume_candidates": (
            "core.resume_candidates",
            "PrivateHomeResumeCandidateRepository",
        ),
        "resume_compilations": (
            "core.resume_compilation",
            "PrivateHomeResumeCompilationRepository",
        ),
        "resume_compilation_stopped_sources": (
            "core.resume_compilation_stopped_source",
            "PrivateHomeResumeCompilationStoppedSourceRepository",
        ),
        "resume_fact_qa": (
            "core.resume_fact_qa",
            "PrivateHomeResumeFactQARepository",
        ),
        "resume_latex_constructions": (
            "core.resume_latex_construction",
            "PrivateHomeResumeLatexConstructionRecordRepository",
        ),
        "resume_layout_revision_records": (
            "core.resume_layout_revision",
            "PrivateHomeResumeLayoutRevisionRecordRepository",
        ),
        "resume_layout_revisions": (
            "core.resume_layout_revision",
            "PrivateHomeResumeLayoutRevisionRepository",
        ),
        "resume_selection_decisions": (
            "core.resume_selection",
            "PrivateHomeResumeSelectionDecisionRepository",
        ),
        "resume_visual_qa": (
            "core.resume_visual_qa",
            "PrivateHomeResumeVisualQARepository",
        ),
        "source_resume_projections": (
            "core.source_resume_projection",
            "PrivateHomeSourceResumeProjectionRepository",
        ),
        "tailored_resume_drafts": (
            "core.resume_tailoring",
            "PrivateHomeTailoredResumeDraftRepository",
        ),
        "verified_execution_profiles": (
            "core.verified_application_execution_profile",
            "PrivateHomeVerifiedApplicationExecutionProfileRepository",
        ),
        "plan_execution_policy_decisions": (
            "core.plan_execution_policy",
            "PrivateHomePlanExecutionPolicyDecisionRepository",
        ),
        "plan_execution_context_bindings": (
            "core.plan_assembly_execution_context_binding",
            "PrivateHomePlanAssemblyExecutionContextBindingRepository",
        ),
        "application_bundle_assemblies": (
            "core.application_bundle_assembly",
            "PrivateHomeApplicationBundleAssemblyRepository",
        ),
        "recoverable_application_bundles": (
            "core.recoverable_application_bundle",
            "PrivateHomeRecoverableApplicationBundleEnvelopeRepository",
        ),
        "non_submit_executions": (
            "core.non_submit_application_execution",
            "PrivateHomeNonSubmitApplicationExecutionRepository",
        ),
        "submission_authorizations": (
            "core.submission_authorization",
            "PrivateHomeSubmissionAuthorizationRepository",
        ),
        "submission_permits": (
            "core.submission_permit",
            "PrivateHomeSubmissionPermitRepository",
        ),
        "authorized_submission_executions": (
            "core.authorized_submission_execution",
            "PrivateHomeAuthorizedSubmissionExecutionRepository",
        ),
        "application_execution_runs": (
            "core.application_execution_orchestrator",
            "PrivateHomeApplicationExecutionRunRepository",
        ),
        "automation_cycle_runs": (
            "core.automation_cycle",
            "PrivateHomeAutomationCycleRunRepository",
        ),
    }
)


def resolve_production_config_path(
    *,
    cli_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve CLI > environment > platform default without CWD scanning."""

    active_environ = os.environ if environ is None else environ
    raw = cli_path or active_environ.get(JOBOPS_CONFIG_FILE_ENV)
    if raw is None:
        active_home = (home or Path.home()).expanduser()
        raw = (
            active_home
            / "Library"
            / "Application Support"
            / "Jobops"
            / "config"
            / "application.yaml"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_SCHEMA_INVALID,
            section="config_location",
        )
    return path


def load_production_application_config(
    path: str | Path,
) -> ProductionApplicationConfig:
    """Load one bounded, non-symlinked, owner-only, single-document YAML."""

    config_path = Path(path).expanduser()
    if not config_path.exists() or not config_path.is_file():
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_NOT_FOUND
        )
    if containing_git_worktree(config_path) is not None:
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_PERMISSION_INVALID
        )
    if config_path.is_symlink():
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_PERMISSION_INVALID
        )
    file_stat = config_path.stat()
    if file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_PERMISSION_INVALID
        )
    if file_stat.st_size > MAX_CONFIG_BYTES:
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_SCHEMA_INVALID
        )
    try:
        payload = config_path.read_bytes()
        tokens = tuple(yaml.scan(payload.decode("utf-8")))
        if any(isinstance(token, (AliasToken, AnchorToken, TagToken)) for token in tokens):
            raise ValueError("YAML aliases, anchors, and tags are unsupported")
        documents = tuple(yaml.safe_load_all(payload))
        if len(documents) != 1:
            raise ValueError("exactly one YAML document is required")
        return production_application_config_from_mapping(documents[0])
    except ProductionApplicationBootstrapError:
        raise
    except Exception:
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_SCHEMA_INVALID
        ) from None


def production_application_config_from_mapping(
    value: Any,
) -> ProductionApplicationConfig:
    root = _closed_mapping(
        value,
        {
            "config_contract_version",
            "private_home",
            "authentication",
            "search",
            "ai",
            "preparation",
            "execution_policy",
            "browser",
            "automation",
            "infrastructure",
            "diagnostics",
        },
    )
    if root["config_contract_version"] != (
        PRODUCTION_APPLICATION_CONFIG_CONTRACT_VERSION
    ):
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_VERSION_UNSUPPORTED
        )
    try:
        private_raw = _closed_mapping(
            root["private_home"],
            {
                "root",
                "schema_version",
                "create_if_missing",
                "permissions_policy",
            },
        )
        private_home_config = PrivateHomeConfig(
            root=Path(private_raw["root"]).expanduser(),
            schema_version=private_raw["schema_version"],
            create_if_missing=private_raw["create_if_missing"],
            permissions_policy=private_raw["permissions_policy"],
        )
        private_home = PrivateHome(private_home_config.root)
        auth_raw = _closed_mapping(
            root["authentication"],
            {
                "provider_id",
                "session_contract_version",
                "session_secret_ref",
                "cookie_policy",
                "local_subject_id",
                "local_subject_binding_policy",
                "local_session_issuance_policy",
                "session_ttl_seconds",
                "trusted_proxy_policy",
                "contract_version",
            },
        )
        auth = AuthenticationRuntimeConfig(
            session_secret_ref=_secret_reference(
                auth_raw["session_secret_ref"]
            ),
            **{
                key: item
                for key, item in auth_raw.items()
                if key != "session_secret_ref"
            },
        )
        search_raw = _closed_mapping_with_optional(
            root["search"],
            required={
                "enabled_providers",
                "boards",
                "request_limits",
                "timeout_policy",
                "search_contract_version",
            },
            optional={
                "ashby_boards",
                "lever_sites",
                "glassdoor",
                "jobvite_feeds",
                "authorized_web_search",
                "job_alert_inbox",
            },
        )
        search_raw.setdefault("ashby_boards", [])
        search_raw.setdefault("lever_sites", [])
        search_raw.setdefault("glassdoor", None)
        search_raw.setdefault("jobvite_feeds", [])
        search_raw.setdefault("authorized_web_search", None)
        search_raw.setdefault("job_alert_inbox", None)
        limits = _closed_mapping(
            search_raw["request_limits"],
            {
                "max_queries_per_refresh",
                "max_results_per_query",
                "max_response_bytes",
                "max_redirects",
                "max_concurrent_requests",
            },
        )
        timeout = _closed_mapping(
            search_raw["timeout_policy"],
            {"connect_timeout_seconds", "read_timeout_seconds"},
        )
        providers = tuple(search_raw["enabled_providers"])
        glassdoor_raw = search_raw["glassdoor"]
        glassdoor = None
        if glassdoor_raw is not None:
            glassdoor_values = _closed_mapping(
                glassdoor_raw,
                {
                    "source_id",
                    "partner_id_ref",
                    "partner_key_ref",
                    "user_ip",
                },
            )
            glassdoor = GlassdoorPartnerRuntimeConfig(
                source_id=glassdoor_values["source_id"],
                partner_id_ref=_secret_reference(
                    glassdoor_values["partner_id_ref"]
                ),
                partner_key_ref=_secret_reference(
                    glassdoor_values["partner_key_ref"]
                ),
                user_ip=glassdoor_values["user_ip"],
            )
        web_search_raw = search_raw["authorized_web_search"]
        authorized_web_search = None
        if web_search_raw is not None:
            web_search_values = _closed_mapping(
                web_search_raw,
                {
                    "provider_id",
                    "api_key_ref",
                    "storage_rights_confirmed",
                    "country",
                    "search_language",
                    "lookback_days",
                    "max_search_requests",
                    "results_per_request",
                    "max_resolution_searches",
                },
            )
            authorized_web_search = AuthorizedWebSearchRuntimeConfig(
                api_key_ref=_secret_reference(
                    web_search_values["api_key_ref"]
                ),
                **{
                    key: value
                    for key, value in web_search_values.items()
                    if key != "api_key_ref"
                },
            )
        job_alert_raw = search_raw["job_alert_inbox"]
        job_alert_inbox = None
        if job_alert_raw is not None:
            job_alert_values = _closed_mapping(
                job_alert_raw,
                {
                    "host",
                    "recipient",
                    "credential_ref",
                    "mailbox",
                    "port",
                    "allowed_sender_domains",
                    "trusted_authserv_ids",
                    "max_age_hours",
                    "max_messages",
                },
            )
            job_alert_inbox = JobAlertInboxRuntimeConfig(
                host=job_alert_values["host"],
                recipient=job_alert_values["recipient"],
                credential_ref=_secret_reference(
                    job_alert_values["credential_ref"]
                ),
                mailbox=job_alert_values["mailbox"],
                port=job_alert_values["port"],
                allowed_sender_domains=tuple(
                    job_alert_values["allowed_sender_domains"]
                ),
                trusted_authserv_ids=tuple(
                    job_alert_values["trusted_authserv_ids"]
                ),
                max_age_hours=job_alert_values["max_age_hours"],
                max_messages=job_alert_values["max_messages"],
            )
        search = ProductionSearchConfig(
            enabled_providers=providers,
            boards=tuple(
                GreenhouseBoardConfig(
                    canonical_company=board["canonical_company"],
                    board_token=board["board_token"],
                    aliases=tuple(board.get("aliases", ())),
                )
                for board in _closed_board_list(search_raw["boards"])
            ),
            ashby_boards=tuple(
                AshbyBoardConfig(
                    canonical_company=board["canonical_company"],
                    board_name=board["board_name"],
                    aliases=tuple(board.get("aliases", ())),
                )
                for board in _closed_provider_list(
                    search_raw["ashby_boards"],
                    required={"canonical_company", "board_name"},
                    optional={"aliases"},
                )
            ),
            lever_sites=tuple(
                LeverSiteConfig(
                    canonical_company=site["canonical_company"],
                    site_name=site["site_name"],
                    aliases=tuple(site.get("aliases", ())),
                    region=site.get("region", "GLOBAL"),
                )
                for site in _closed_provider_list(
                    search_raw["lever_sites"],
                    required={"canonical_company", "site_name"},
                    optional={"aliases", "region"},
                )
            ),
            glassdoor=glassdoor,
            jobvite_feeds=tuple(
                JobviteFeedRuntimeConfig(
                    canonical_company=feed["canonical_company"],
                    career_site=feed["career_site"],
                    aliases=tuple(feed.get("aliases", ())),
                    api_key_ref=_secret_reference(feed["api_key_ref"]),
                    api_secret_ref=_secret_reference(
                        feed["api_secret_ref"]
                    ),
                )
                for feed in _closed_provider_list(
                    search_raw["jobvite_feeds"],
                    required={
                        "canonical_company",
                        "career_site",
                        "api_key_ref",
                        "api_secret_ref",
                    },
                    optional={"aliases"},
                )
            ),
            authorized_web_search=authorized_web_search,
            job_alert_inbox=job_alert_inbox,
            policy=JobSearchExecutionPolicy(
                contract_version=search_raw["search_contract_version"],
                allowed_providers=providers,
                **limits,
                **timeout,
            ),
        )
        prep_raw = _closed_mapping(
            root["preparation"],
            {
                "recipe_contract_version",
                "latex_engine",
                "compile_timeout_seconds",
                "renderer_dpi",
                "max_source_bytes",
                "max_material_bytes",
                "max_revisions",
                "contract_version",
            },
        )
        policy_raw = _closed_mapping(
            root["execution_policy"],
            {
                "configuration_id",
                "configuration_version",
                "mode",
                "authority_configured",
                "email_verification_agent_enabled",
                "allow_keychain_login",
                "allow_account_registration",
                "rules_contract_version",
                "contract_version",
            },
        )
        browser_raw = _closed_mapping(
            root["browser"],
            {
                "browser_engine",
                "headless",
                "profile_name",
                "slow_mo_ms",
                "launch_timeout_seconds",
                "navigation_timeout_seconds",
                "lease_ttl_seconds",
                "max_active_leases",
                "locale",
                "timezone_id",
                "download_policy",
                "tracing_policy",
                "browser_args_policy_version",
                "page_policy_version",
                "single_subject_mode",
                "config_contract_version",
            },
        )
        browser = project_browser_runtime_config(
            {"browser_runtime": browser_raw},
            private_home=private_home,
        )
        automation_raw = _closed_mapping(
            root["automation"],
            {
                "max_priority_refreshes",
                "max_plan_creations",
                "max_preparations",
                "max_bundle_assemblies",
                "max_executions",
                "rate_limit_policy",
                "cycle_contract_version",
                "composition_binding",
            },
        )
        automation = AutomationRuntimeConfig(
            budgets=AutomationCycleUIBudgetConfig(
                max_reprioritizations=automation_raw[
                    "max_priority_refreshes"
                ],
                max_plan_creations=automation_raw["max_plan_creations"],
                max_preparations=automation_raw["max_preparations"],
                max_bundle_assemblies=automation_raw[
                    "max_bundle_assemblies"
                ],
                max_executions=automation_raw["max_executions"],
                composition_binding=automation_raw["composition_binding"],
                contract_version=automation_raw["cycle_contract_version"],
            ),
            rate_limit_policy=automation_raw["rate_limit_policy"],
        )
        return ProductionApplicationConfig(
            config_contract_version=root["config_contract_version"],
            private_home=private_home_config,
            authentication=auth,
            search=search,
            ai=AIBackendRuntimeConfig(root["ai"]),
            preparation=PreparationRuntimeConfig(**prep_raw),
            execution_policy=ExecutionPolicyRuntimeConfig(**policy_raw),
            browser=browser,
            automation=automation,
            infrastructure=InfrastructureRuntimeConfig(
                **_closed_mapping(
                    root["infrastructure"],
                    {
                        "http_client_id",
                        "repository_contract_version",
                        "contract_version",
                    },
                )
            ),
            diagnostics=DiagnosticsRuntimeConfig(
                **_closed_mapping(root["diagnostics"], {"level"})
            ),
        )
    except ProductionApplicationBootstrapError:
        raise
    except Exception:
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_SCHEMA_INVALID
        ) from None


def build_production_repository_bundle(
    private_home: PrivateHome,
) -> ProductionRepositoryBundle:
    repositories: dict[str, object] = {}
    try:
        for name, (module_name, class_name) in _REPOSITORY_SPECS.items():
            cls = getattr(importlib.import_module(module_name), class_name)
            repositories[name] = cls(private_home)
    except Exception:
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.PREPARATION_DEPENDENCY_MISSING,
            section="repository_bundle",
        ) from None
    return ProductionRepositoryBundle(repositories)


def build_production_preparation_stage_dependencies(
    *,
    config: ProductionApplicationConfig,
    repositories: ProductionRepositoryBundle,
    private_home: PrivateHome,
    agents: ProductionPreparationAgentAdapters,
    latex_compiler: object,
    pdf_renderer: object,
) -> ProductionPreparationStageDependencies:
    """Construct the existing P2b4g dependency type without building a recipe."""

    from core.source_resume_projection import (
        DeterministicSourceResumeParser,
        PrivateHomeSourceResumeArtifactReader,
    )

    async def layout_revision_review_step(**kwargs: Any) -> Any:
        from core.resume_visual_qa import review_resume_visual_qa

        return await review_resume_visual_qa(**kwargs)

    def layout_revision_compile_step(**kwargs: Any) -> Any:
        from core.resume_compilation import compile_resume_latex

        return compile_resume_latex(**kwargs)

    binding = {
        "config_contract_version": config.config_contract_version,
        "preparation_contract_version": config.preparation.contract_version,
        "recipe_contract_version": config.preparation.recipe_contract_version,
        "ai_default_backend": config.ai.value["default_backend"],
    }
    dependency_hash = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ProductionPreparationStageDependencies(
        application_plan_repository=repositories.require("application_plans"),
        job_repository=repositories.require("job_postings"),
        resume_candidate_repository=repositories.require("resume_candidates"),
        source_resume_artifact_reader=PrivateHomeSourceResumeArtifactReader(
            private_home
        ),
        source_resume_parser=DeterministicSourceResumeParser(),
        resume_selection_decision_repository=repositories.require(
            "resume_selection_decisions"
        ),
        source_resume_projection_repository=repositories.require(
            "source_resume_projections"
        ),
        candidate_evidence_snapshot_repository=repositories.require(
            "candidate_evidence_snapshots"
        ),
        tailored_resume_draft_repository=repositories.require(
            "tailored_resume_drafts"
        ),
        resume_fact_qa_repository=repositories.require("resume_fact_qa"),
        latex_version_repository=repositories.require("latex_versions"),
        base_latex_selection_decision_repository=repositories.require(
            "base_latex_selections"
        ),
        managed_resume_template_provider=DefaultManagedResumeTemplateProvider(),
        resume_latex_construction_repository=repositories.require(
            "resume_latex_constructions"
        ),
        latex_compiler=latex_compiler,
        resume_compilation_repository=repositories.require(
            "resume_compilations"
        ),
        resume_compilation_stopped_source_repository=repositories.require(
            "resume_compilation_stopped_sources"
        ),
        pdf_renderer=pdf_renderer,
        resume_visual_qa_repository=repositories.require("resume_visual_qa"),
        resume_layout_revision_record_repository=repositories.require(
            "resume_layout_revision_records"
        ),
        resume_layout_revision_repository=repositories.require(
            "resume_layout_revisions"
        ),
        layout_revision_compile_step=layout_revision_compile_step,
        layout_revision_review_step=layout_revision_review_step,
        prepared_resume_material_repository=repositories.require(
            "prepared_resumes"
        ),
        plan_material_manifest_repository=repositories.require(
            "plan_material_manifests"
        ),
        cover_letter_evidence_snapshot_repository=repositories.require(
            "cover_letter_evidence"
        ),
        cover_letter_draft_repository=repositories.require(
            "cover_letter_drafts"
        ),
        cover_letter_fact_qa_repository=repositories.require(
            "cover_letter_fact_qa"
        ),
        managed_cover_letter_template_provider=(
            DefaultManagedCoverLetterTemplateProvider()
        ),
        prepared_cover_letter_material_repository=repositories.require(
            "prepared_cover_letters"
        ),
        application_fact_provider=PrivateHomeApplicationFactProvider(
            private_home
        ),
        application_answer_policy=ApplicationAnswerPolicy.default(),
        prepared_application_answer_set_repository=repositories.require(
            "prepared_answer_sets"
        ),
        private_home=private_home,
        agents=agents,
        dependency_configuration_hash=dependency_hash,
    )


async def build_production_application_bootstrap(
    config: ProductionApplicationConfig,
    *,
    credential_store: CredentialStore | None = None,
    backend_registry: Mapping[str, type] | None = None,
    isolation_profile_registry: Mapping[str, object] | None = None,
    playwright_factory: Callable[[], Awaitable[Any]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProductionApplicationBootstrap:
    """Construct all P2c10c inputs without running any business operation."""

    if not isinstance(config, ProductionApplicationConfig):
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_SCHEMA_INVALID
        )
    active_environ = os.environ if environ is None else environ
    store = credential_store or MacOSSecurityCredentialStore()
    owned: list[object] = []
    try:
        root = config.private_home.root.expanduser().resolve(strict=False)
        repository_root = Path(__file__).resolve().parents[1]
        if containing_git_worktree(root) is not None or (
            root == repository_root or repository_root in root.parents
        ):
            raise ProductionApplicationBootstrapError(
                ProductionBootstrapFailure.PRIVATE_HOME_INVALID
            )
        private_home = PrivateHome(root)
        if config.private_home.create_if_missing:
            private_home.ensure()
        session_master_secret = (
            config.authentication.session_secret_ref.resolve(
                environ=active_environ,
                credential_store=store,
            )
        )
        glassdoor_config = None
        if config.search.glassdoor is not None:
            glassdoor_config = GlassdoorPartnerConfig(
                source_id=config.search.glassdoor.source_id,
                partner_id=config.search.glassdoor.partner_id_ref.resolve(
                    environ=active_environ,
                    credential_store=store,
                ),
                partner_key=config.search.glassdoor.partner_key_ref.resolve(
                    environ=active_environ,
                    credential_store=store,
                ),
                user_ip=config.search.glassdoor.user_ip,
            )
        jobvite_configs = tuple(
            JobviteFeedConfig(
                canonical_company=feed.canonical_company,
                career_site=feed.career_site,
                aliases=feed.aliases,
                api_key=feed.api_key_ref.resolve(
                    environ=active_environ,
                    credential_store=store,
                ),
                api_secret=feed.api_secret_ref.resolve(
                    environ=active_environ,
                    credential_store=store,
                ),
            )
            for feed in config.search.jobvite_feeds
        )
        authorized_web_search_inputs = None
        if config.search.authorized_web_search is not None:
            web_search = config.search.authorized_web_search
            authorized_web_search_inputs = (
                ProductionAuthorizedWebSearchFactoryInputs(
                    api_key=web_search.api_key_ref.resolve(
                        environ=active_environ,
                        credential_store=store,
                    ),
                    storage_rights_confirmed=(
                        web_search.storage_rights_confirmed
                    ),
                    country=web_search.country,
                    search_language=web_search.search_language,
                    lookback_days=web_search.lookback_days,
                    max_search_requests=web_search.max_search_requests,
                    results_per_request=web_search.results_per_request,
                    max_resolution_searches=(
                        web_search.max_resolution_searches
                    ),
                )
            )
        authentication = KeychainAuthenticatedSubjectSessionProvider(store)
        local_session_issuer = LocalAuthenticatedSubjectSessionIssuer(
            session_writer=authentication,
            subject_id=config.authentication.local_subject_id,
            master_secret=session_master_secret,
            ttl_seconds=config.authentication.session_ttl_seconds,
        )
        repositories = build_production_repository_bundle(private_home)
        try:
            agents = build_production_preparation_agent_adapters(
                ai_config=config.ai.as_mapping(),
                backend_registry=backend_registry,
                isolation_profile_registry=isolation_profile_registry,
            )
            # P1b3 construction is a static capability/availability preflight;
            # it performs no semantic request and is rebuilt by P2c10c later.
            build_production_priority_agent(
                ai_config=config.ai.as_mapping(),
                backend_registry=backend_registry,
                isolation_profile_registry=isolation_profile_registry,
            )
        except ModelBackendResolutionError as exc:
            raise ProductionApplicationBootstrapError(
                ProductionBootstrapFailure.AI_CONFIGURATION_INVALID,
                section=exc.status.value,
            ) from None
        except Exception:
            raise ProductionApplicationBootstrapError(
                ProductionBootstrapFailure.AI_CONFIGURATION_INVALID
            ) from None
        compiler = SandboxedPdfLatexCompiler(
            engine=config.preparation.latex_engine,
            timeout_seconds=config.preparation.compile_timeout_seconds,
        )
        renderer = PdfiumPageRenderer(dpi=config.preparation.renderer_dpi)
        preparation_dependencies = (
            build_production_preparation_stage_dependencies(
                config=config,
                repositories=repositories,
                private_home=private_home,
                agents=agents,
                latex_compiler=compiler,
                pdf_renderer=renderer,
            )
        )
        policy_config = PolicyConfig(
            mode=config.execution_policy.mode,
            email_verification_agent_enabled=(
                config.execution_policy.email_verification_agent_enabled
            ),
            allow_keychain_login=(
                config.execution_policy.allow_keychain_login
            ),
            allow_account_registration=(
                config.execution_policy.allow_account_registration
            ),
        )
        rules = PlanExecutionPolicyRulesV1(
            PlanExecutionPolicyConfiguration.create(
                configuration_id=config.execution_policy.configuration_id,
                configuration_version=(
                    config.execution_policy.configuration_version
                ),
                policy_config=policy_config,
                authority_configured=(
                    config.execution_policy.authority_configured
                ),
            )
        )
        ledger = EventLedger(private_home.paths.event_ledger)
        browser = build_production_browser_runtime(
            config=config.browser,
            private_home=private_home,
            lease_manager=LeaseManager(ledger),
            playwright_factory=playwright_factory,
        )
        owned.append(browser)
        try:
            legacy_resume_candidate_migration = (
                migrate_hash_attested_legacy_resume_candidates(
                    home=private_home,
                    subject_id=config.authentication.local_subject_id,
                    now=datetime.now(timezone.utc),
                    repository=repositories.require("resume_candidates"),
                )
            )
        except LegacyResumeCandidateMigrationError as exc:
            raise ProductionApplicationBootstrapError(
                ProductionBootstrapFailure.PREPARATION_DEPENDENCY_MISSING,
                section=(
                    "LEGACY_RESUME_CANDIDATE_MIGRATION_"
                    f"{exc.failure.value}"
                ),
            ) from None
        diagnostics = dict(config.safe_diagnostics())
        diagnostics.update(
            {
                "bootstrap_contract_version": (
                    PRODUCTION_APPLICATION_BOOTSTRAP_CONTRACT_VERSION
                ),
                "repository_contract_version": repositories.contract_version,
                "browser_runtime_contract": (
                    config.browser.config_contract_version
                ),
                "preparation_dependency_hash": (
                    preparation_dependencies.dependency_configuration_hash
                ),
                "legacy_resume_candidate_migration": (
                    legacy_resume_candidate_migration.safe_dict()
                ),
            }
        )
        return ProductionApplicationBootstrap(
            config=config,
            private_home=private_home,
            credential_store=store,
            repository_bundle=repositories,
            authentication_session_provider=authentication,
            local_session_issuer=local_session_issuer,
            job_search_factory_inputs=ProductionJobSearchFactoryInputs(
                boards=config.search.boards,
                ashby_boards=config.search.ashby_boards,
                lever_sites=config.search.lever_sites,
                glassdoor=glassdoor_config,
                jobvite_feeds=jobvite_configs,
                policy=config.search.policy,
                http_client_id=config.infrastructure.http_client_id,
                authorized_web_search=authorized_web_search_inputs,
            ),
            priority_agent_factory_inputs=(
                ProductionPriorityAgentFactoryInputs(
                    ai_config=config.ai.as_mapping(),
                    backend_registry=backend_registry,
                    isolation_profile_registry=isolation_profile_registry,
                )
            ),
            preparation_stage_dependencies=preparation_dependencies,
            execution_policy_rules=rules,
            browser_runtime=browser,
            automation_runtime_policy=config.automation.budgets,
            owned_resources=tuple(owned),
            safe_diagnostics=MappingProxyType(diagnostics),
        )
    except ProductionApplicationBootstrapError:
        for resource in reversed(owned):
            close = getattr(resource, "close", None)
            if close is not None:
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    pass
        raise
    except Exception:
        for resource in reversed(owned):
            close = getattr(resource, "close", None)
            if close is not None:
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    pass
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.BOOTSTRAP_PARTIAL_FAILURE
        ) from None


def _closed_mapping(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_SCHEMA_INVALID
        )
    return dict(value)


def _closed_mapping_with_optional(
    value: Any,
    *,
    required: set[str],
    optional: set[str],
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or not required.issubset(value)
        or set(value) - required - optional
    ):
        raise ProductionApplicationBootstrapError(
            ProductionBootstrapFailure.CONFIG_SCHEMA_INVALID
        )
    return dict(value)


def _closed_board_list(value: Any) -> tuple[dict[str, Any], ...]:
    return _closed_provider_list(
        value,
        required={"canonical_company", "board_token"},
        optional={"aliases"},
    )


def _closed_provider_list(
    value: Any,
    *,
    required: set[str],
    optional: set[str],
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("provider source list must be a list")
    sources = []
    for item in value:
        if (
            not isinstance(item, Mapping)
            or not required.issubset(item)
            or set(item) - required - optional
        ):
            raise ValueError("provider source configuration is invalid")
        sources.append(dict(item))
    return tuple(sources)


def _secret_reference(value: Any) -> SecretReference:
    if not isinstance(value, Mapping):
        raise ValueError("secret reference must be a mapping")
    source_value = value.get("source")
    source = SecretReferenceSource(
        source_value.upper()
        if isinstance(source_value, str)
        else source_value
    )
    if source is SecretReferenceSource.ENV:
        raw = _closed_mapping(value, {"source", "name"})
        return SecretReference(source=source, name=raw["name"])
    raw = _closed_mapping(value, {"source", "service", "account"})
    return SecretReference(
        source=source,
        service=raw["service"],
        account=raw["account"],
    )


def _deep_plain_copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_plain_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_plain_copy(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_deep_plain_copy(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("configuration contains an unsupported value")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _walk_mapping_keys(value: Any) -> tuple[str, ...]:
    keys: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(_walk_mapping_keys(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            keys.extend(_walk_mapping_keys(item))
    return tuple(keys)


__all__ = [
    "AIBackendRuntimeConfig",
    "AuthorizedWebSearchRuntimeConfig",
    "AuthenticationRuntimeConfig",
    "AutomationRuntimeConfig",
    "DiagnosticsRuntimeConfig",
    "ExecutionPolicyRuntimeConfig",
    "InfrastructureRuntimeConfig",
    "JobAlertInboxRuntimeConfig",
    "JOBOPS_CONFIG_FILE_ENV",
    "PreparationRuntimeConfig",
    "PrivateHomeConfig",
    "ProductionApplicationBootstrap",
    "ProductionApplicationBootstrapError",
    "ProductionApplicationConfig",
    "ProductionAuthorizedWebSearchFactoryInputs",
    "ProductionBootstrapFailure",
    "ProductionJobSearchFactoryInputs",
    "ProductionPriorityAgentFactoryInputs",
    "ProductionRepositoryBundle",
    "ProductionSearchConfig",
    "SecretReference",
    "SecretReferenceSource",
    "build_production_application_bootstrap",
    "build_production_preparation_stage_dependencies",
    "build_production_repository_bundle",
    "load_production_application_config",
    "production_application_config_from_mapping",
    "resolve_production_config_path",
]
