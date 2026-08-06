"""Production construction for typed, configured job-search ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from core.job_search import (
    JobSearchPort,
    JobSearchRequest,
    JobSearchResult,
    JobSearchStatus,
)
from core.search_profile import (
    SearchProfileSourceKind,
    SearchProfileSourceReference,
)
from source_connectors.greenhouse_board import (
    BoundedJobSearchHttpPort,
    GREENHOUSE_JOB_SEARCH_ADAPTER_VERSION,
    GreenhouseBoardConfig,
    GreenhouseBoardJobSearch,
    JobSearchExecutionPolicy,
)
from source_connectors.provider_job_search import (
    ASHBY_JOB_SEARCH_ADAPTER_VERSION,
    GLASSDOOR_JOB_SEARCH_ADAPTER_VERSION,
    JOBVITE_JOB_SEARCH_ADAPTER_VERSION,
    LEVER_JOB_SEARCH_ADAPTER_VERSION,
    AshbyBoardConfig,
    AshbyBoardJobSearch,
    GlassdoorPartnerConfig,
    GlassdoorPartnerJobSearch,
    JobviteFeedConfig,
    JobviteFeedJobSearch,
    LeverPostingsJobSearch,
    LeverSiteConfig,
)


PRODUCTION_JOB_SEARCH_FACTORY_CONTRACT_VERSION = (
    "production-job-search-factory-v2"
)


class JobSearchProviderCapabilityStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class JobSearchProviderCapability:
    provider_id: str
    status: JobSearchProviderCapabilityStatus
    adapter_version: str | None

    def __post_init__(self) -> None:
        if self.provider_id not in {
            "GREENHOUSE",
            "ASHBY",
            "LEVER",
            "GLASSDOOR",
            "JOBVITE",
        }:
            raise ValueError("unknown job search provider capability")
        object.__setattr__(
            self,
            "status",
            JobSearchProviderCapabilityStatus(self.status),
        )
        if self.status is JobSearchProviderCapabilityStatus.SUPPORTED:
            if not self.adapter_version:
                raise ValueError("supported provider requires adapter version")
        elif self.adapter_version is not None:
            raise ValueError("unsupported provider cannot name an adapter")


@dataclass(frozen=True, slots=True)
class ProductionJobSearchPorts:
    """Exact SearchProfile source-to-port bindings consumed by S3b."""

    contract_version: str
    ports: Mapping[SearchProfileSourceReference, JobSearchPort]
    capabilities: tuple[JobSearchProviderCapability, ...]

    def __post_init__(self) -> None:
        if self.contract_version != PRODUCTION_JOB_SEARCH_FACTORY_CONTRACT_VERSION:
            raise ValueError("unsupported production job search factory version")
        copied = dict(self.ports)
        if not all(
            isinstance(source, SearchProfileSourceReference)
            and isinstance(port, JobSearchPort)
            for source, port in copied.items()
        ):
            raise TypeError("ports must bind typed sources to JobSearchPort")
        object.__setattr__(self, "ports", MappingProxyType(copied))
        if tuple(capability.provider_id for capability in self.capabilities) != (
            "GREENHOUSE",
            "ASHBY",
            "LEVER",
            "GLASSDOOR",
            "JOBVITE",
        ):
            raise ValueError("provider capabilities must have canonical order")


class ConfiguredProviderJobSearchRouter:
    """Choose the first configured source that owns the requested company."""

    def __init__(self, ports: tuple[JobSearchPort, ...]) -> None:
        if not isinstance(ports, tuple):
            raise ValueError("configured search ports must be a tuple")
        if not all(isinstance(port, JobSearchPort) for port in ports):
            raise TypeError("ports must implement JobSearchPort")
        self._ports = ports

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        for port in self._ports:
            result = await port.search(request)
            if not isinstance(result, JobSearchResult):
                raise TypeError("configured search port returned an invalid result")
            if result.status is not JobSearchStatus.UNSUPPORTED:
                return result
        return JobSearchResult.unsupported()


def build_conversational_job_search_port(
    bindings: ProductionJobSearchPorts,
) -> ConfiguredProviderJobSearchRouter:
    """Build deterministic named-job routing; generic Glassdoor stays last."""

    if not isinstance(bindings, ProductionJobSearchPorts):
        raise TypeError("bindings must be ProductionJobSearchPorts")
    ordered = sorted(
        bindings.ports.items(),
        key=lambda item: (
            item[0].kind is SearchProfileSourceKind.GLASSDOOR_PARTNER_SEARCH,
        ),
    )
    return ConfiguredProviderJobSearchRouter(
        tuple(port for _, port in ordered)
    )


def build_production_job_search_ports(
    *,
    boards: tuple[GreenhouseBoardConfig, ...],
    http_port: BoundedJobSearchHttpPort,
    policy: JobSearchExecutionPolicy,
    ashby_boards: tuple[AshbyBoardConfig, ...] = (),
    lever_sites: tuple[LeverSiteConfig, ...] = (),
    glassdoor: GlassdoorPartnerConfig | None = None,
    jobvite_feeds: tuple[JobviteFeedConfig, ...] = (),
) -> ProductionJobSearchPorts:
    """Build all exact S3b source bindings without probing the network."""

    if not isinstance(boards, tuple):
        raise ValueError("boards must be a tuple")
    if not isinstance(http_port, BoundedJobSearchHttpPort):
        raise TypeError("http_port must implement BoundedJobSearchHttpPort")
    if not isinstance(policy, JobSearchExecutionPolicy):
        raise TypeError("policy must be JobSearchExecutionPolicy")
    configured = {
        "GREENHOUSE": bool(boards),
        "ASHBY": bool(ashby_boards),
        "LEVER": bool(lever_sites),
        "GLASSDOOR": glassdoor is not None,
        "JOBVITE": bool(jobvite_feeds),
    }
    if any(
        enabled and provider not in policy.allowed_providers
        for provider, enabled in configured.items()
    ):
        raise ValueError("configured provider is disabled by policy")

    ports: dict[SearchProfileSourceReference, JobSearchPort] = {}
    canonical_companies: set[tuple[SearchProfileSourceKind, str]] = set()
    for board in boards:
        if not isinstance(board, GreenhouseBoardConfig):
            raise TypeError("boards must contain GreenhouseBoardConfig")
        source = SearchProfileSourceReference(
            kind=SearchProfileSourceKind.KNOWN_GREENHOUSE_BOARD,
            source_id=board.board_token,
        )
        if source in ports:
            raise ValueError("provider source IDs must be unique")
        company = " ".join(board.canonical_company.casefold().split())
        company_key = (source.kind, company)
        if company_key in canonical_companies:
            raise ValueError("canonical company bindings must be unique")
        canonical_companies.add(company_key)
        ports[source] = GreenhouseBoardJobSearch(
            boards=(board,),
            http_port=http_port,
            policy=policy,
        )

    for config in ashby_boards:
        if not isinstance(config, AshbyBoardConfig):
            raise TypeError("ashby_boards must contain AshbyBoardConfig")
        source = SearchProfileSourceReference(
            SearchProfileSourceKind.KNOWN_ASHBY_BOARD,
            config.board_name,
        )
        if source in ports:
            raise ValueError("provider source IDs must be unique")
        ports[source] = AshbyBoardJobSearch(
            config=config,
            http_port=http_port,
            policy=policy,
        )

    for config in lever_sites:
        if not isinstance(config, LeverSiteConfig):
            raise TypeError("lever_sites must contain LeverSiteConfig")
        source = SearchProfileSourceReference(
            SearchProfileSourceKind.KNOWN_LEVER_SITE,
            config.site_name,
        )
        if source in ports:
            raise ValueError("provider source IDs must be unique")
        ports[source] = LeverPostingsJobSearch(
            config=config,
            http_port=http_port,
            policy=policy,
        )

    if glassdoor is not None:
        if not isinstance(glassdoor, GlassdoorPartnerConfig):
            raise TypeError("glassdoor must be GlassdoorPartnerConfig")
        source = SearchProfileSourceReference(
            SearchProfileSourceKind.GLASSDOOR_PARTNER_SEARCH,
            glassdoor.source_id,
        )
        ports[source] = GlassdoorPartnerJobSearch(
            config=glassdoor,
            http_port=http_port,
            policy=policy,
        )

    for config in jobvite_feeds:
        if not isinstance(config, JobviteFeedConfig):
            raise TypeError("jobvite_feeds must contain JobviteFeedConfig")
        source = SearchProfileSourceReference(
            SearchProfileSourceKind.KNOWN_JOBVITE_FEED,
            config.career_site,
        )
        if source in ports:
            raise ValueError("provider source IDs must be unique")
        ports[source] = JobviteFeedJobSearch(
            config=config,
            http_port=http_port,
            policy=policy,
        )

    versions = {
        "GREENHOUSE": (
            GREENHOUSE_JOB_SEARCH_ADAPTER_VERSION if boards else None
        ),
        "ASHBY": (
            ASHBY_JOB_SEARCH_ADAPTER_VERSION if ashby_boards else None
        ),
        "LEVER": LEVER_JOB_SEARCH_ADAPTER_VERSION if lever_sites else None,
        "GLASSDOOR": (
            GLASSDOOR_JOB_SEARCH_ADAPTER_VERSION
            if glassdoor is not None
            else None
        ),
        "JOBVITE": (
            JOBVITE_JOB_SEARCH_ADAPTER_VERSION if jobvite_feeds else None
        ),
    }

    return ProductionJobSearchPorts(
        contract_version=PRODUCTION_JOB_SEARCH_FACTORY_CONTRACT_VERSION,
        ports=ports,
        capabilities=tuple(
            JobSearchProviderCapability(
                provider_id=provider_id,
                status=(
                    JobSearchProviderCapabilityStatus.SUPPORTED
                    if version is not None
                    else JobSearchProviderCapabilityStatus.UNSUPPORTED
                ),
                adapter_version=version,
            )
            for provider_id, version in versions.items()
        ),
    )


__all__ = [
    "ConfiguredProviderJobSearchRouter",
    "JobSearchProviderCapability",
    "JobSearchProviderCapabilityStatus",
    "PRODUCTION_JOB_SEARCH_FACTORY_CONTRACT_VERSION",
    "ProductionJobSearchPorts",
    "build_conversational_job_search_port",
    "build_production_job_search_ports",
]
