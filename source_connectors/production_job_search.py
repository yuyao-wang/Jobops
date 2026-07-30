"""Production construction for typed, configured job-search ports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from core.job_search import JobSearchPort
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


PRODUCTION_JOB_SEARCH_FACTORY_CONTRACT_VERSION = (
    "production-job-search-factory-v1"
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
        if self.provider_id not in {"GREENHOUSE", "LEVER"}:
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
        if not self.ports:
            raise ValueError("at least one production job search port is required")
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
            "LEVER",
        ):
            raise ValueError("provider capabilities must have canonical order")


def build_production_job_search_ports(
    *,
    boards: tuple[GreenhouseBoardConfig, ...],
    http_port: BoundedJobSearchHttpPort,
    policy: JobSearchExecutionPolicy,
) -> ProductionJobSearchPorts:
    """Build all exact S3b source bindings without probing the network."""

    if not isinstance(boards, tuple) or not boards:
        raise ValueError("boards must be a non-empty tuple")
    if not isinstance(http_port, BoundedJobSearchHttpPort):
        raise TypeError("http_port must implement BoundedJobSearchHttpPort")
    if not isinstance(policy, JobSearchExecutionPolicy):
        raise TypeError("policy must be JobSearchExecutionPolicy")
    if "GREENHOUSE" not in policy.allowed_providers:
        raise ValueError("mandatory Greenhouse provider is disabled")

    ports: dict[SearchProfileSourceReference, JobSearchPort] = {}
    canonical_companies: set[str] = set()
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
        if company in canonical_companies:
            raise ValueError("canonical company bindings must be unique")
        canonical_companies.add(company)
        ports[source] = GreenhouseBoardJobSearch(
            boards=(board,),
            http_port=http_port,
            policy=policy,
        )

    return ProductionJobSearchPorts(
        contract_version=PRODUCTION_JOB_SEARCH_FACTORY_CONTRACT_VERSION,
        ports=ports,
        capabilities=(
            JobSearchProviderCapability(
                provider_id="GREENHOUSE",
                status=JobSearchProviderCapabilityStatus.SUPPORTED,
                adapter_version=GREENHOUSE_JOB_SEARCH_ADAPTER_VERSION,
            ),
            # SearchProfile V1 has no typed Lever tenant source. Advertising a
            # Lever port before that contract exists would make configuration lie.
            JobSearchProviderCapability(
                provider_id="LEVER",
                status=JobSearchProviderCapabilityStatus.UNSUPPORTED,
                adapter_version=None,
            ),
        ),
    )


__all__ = [
    "JobSearchProviderCapability",
    "JobSearchProviderCapabilityStatus",
    "PRODUCTION_JOB_SEARCH_FACTORY_CONTRACT_VERSION",
    "ProductionJobSearchPorts",
    "build_production_job_search_ports",
]
