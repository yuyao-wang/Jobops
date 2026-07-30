"""Provider-neutral production implementation of the P1b Priority Agent port."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .isolated_model_runner import (
    IsolatedStructuredModelRequest,
    IsolatedStructuredModelResult,
    IsolatedStructuredModelStatus,
)
from .job_prioritization import (
    PriorityAgentMetadata,
    PriorityAgentOutput,
    PriorityAgentOutputInvalidError,
    PriorityAgentUnavailableError,
    PriorityContext,
)
from .model_provider_capabilities import (
    MODEL_EXECUTION_ISOLATION_PROFILES,
    PRIORITY_COMPONENT_REQUIREMENTS,
    PRIORITY_MODEL_COMPONENT_ID,
    ModelBackendResolutionError,
    ModelBackendResolutionFailure,
    ModelBackendResolutionStatus,
    ModelExecutionIsolationProfile,
    ResolvedComponentBackend,
    resolve_component_backend,
)
from .priority_agent_adapter import (
    DEFAULT_PROMPT_VERSION,
    PRIORITY_AGENT_OUTPUT_SCHEMA,
    PRIORITY_AGENT_OUTPUT_SCHEMA_NAME,
    PRIORITY_AGENT_OUTPUT_SCHEMA_VERSION,
    PRIORITY_AGENT_SYSTEM_PROMPT,
    priority_agent_output_from_data,
    priority_context_data,
)


PRODUCTION_PRIORITY_AGENT_ADAPTER_CONTRACT_VERSION = (
    "production-priority-agent-adapter-v1"
)
PRODUCTION_PRIORITY_AGENT_FACTORY_CONTRACT_VERSION = (
    "production-priority-agent-factory-v1"
)
PRODUCTION_PRIORITY_AGENT_LIMITS_CONTRACT_VERSION = (
    "production-priority-agent-limits-v1"
)


class ProductionPriorityAgentErrorCategory(StrEnum):
    AUTHENTICATION_UNAVAILABLE = "AUTHENTICATION_UNAVAILABLE"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    ISOLATION_UNAVAILABLE = "ISOLATION_UNAVAILABLE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    SCHEMA_OUTPUT_INVALID = "SCHEMA_OUTPUT_INVALID"
    TOOL_ATTEMPTED = "TOOL_ATTEMPTED"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"


class ProductionPriorityAgentRuntimeError(RuntimeError):
    """Typed, bounded cause retained beneath the existing P1b error surface."""

    def __init__(
        self,
        category: ProductionPriorityAgentErrorCategory,
        *,
        backend_id: str,
    ) -> None:
        self.category = ProductionPriorityAgentErrorCategory(category)
        self.component_id = PRIORITY_MODEL_COMPONENT_ID
        self.backend_id = backend_id
        super().__init__(
            f"{self.category.value} component={self.component_id} "
            f"backend={backend_id}"
        )


@dataclass(frozen=True, slots=True)
class ProductionPriorityAgentLimits:
    timeout_seconds: int = 120
    max_input_bytes: int = 250_000
    max_output_bytes: int = 128_000
    contract_version: str = PRODUCTION_PRIORITY_AGENT_LIMITS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != PRODUCTION_PRIORITY_AGENT_LIMITS_CONTRACT_VERSION:
            raise ValueError("Priority Agent limits version is unsupported")
        if type(self.timeout_seconds) is not int or not (
            1 <= self.timeout_seconds <= 300
        ):
            raise ValueError("Priority Agent timeout is outside policy")
        for name in ("max_input_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 1_000_000:
                raise ValueError(f"{name} is outside policy")


@dataclass(frozen=True, slots=True)
class ProductionPriorityAgentCallMetadata:
    component_id: str
    adapter_contract_version: str
    factory_contract_version: str
    backend_id: str
    backend_transport: str
    authentication_mode: str
    model_id: str
    prompt_policy_version: str
    schema_version: str
    backend_capability_contract_version: str
    component_requirements_version: str
    native_capability_contract_version: str
    isolation_profile_id: str
    isolation_profile_version: str
    effective_capability_contract_version: str
    backend_resolution_identity: str

    def identity_dict(self) -> dict[str, str]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


def _stable_domain_metadata(
    metadata: ProductionPriorityAgentCallMetadata,
) -> PriorityAgentMetadata:
    encoded = json.dumps(
        metadata.identity_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    identity_suffix = hashlib.sha256(encoded).hexdigest()[:16]
    return PriorityAgentMetadata(
        agent_version=f"priority-agent-production-v1-{identity_suffix}",
        prompt_version=metadata.prompt_policy_version,
        model_id=f"{metadata.backend_id}:{metadata.model_id}",
    )


def _invocation_id(
    *,
    input_data: Mapping[str, Any],
    metadata: ProductionPriorityAgentCallMetadata,
) -> str:
    encoded = json.dumps(
        {
            "backend_resolution_identity": (
                metadata.backend_resolution_identity
            ),
            "component_id": metadata.component_id,
            "input": input_data,
            "prompt_policy_version": metadata.prompt_policy_version,
            "schema_version": metadata.schema_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "priority-agent-invocation-" + hashlib.sha256(encoded).hexdigest()


_INVALID_RESULT_STATUSES = frozenset(
    {
        IsolatedStructuredModelStatus.OUTPUT_TOO_LARGE,
        IsolatedStructuredModelStatus.SCHEMA_OUTPUT_INVALID,
    }
)


def _runtime_category(
    status: IsolatedStructuredModelStatus,
) -> ProductionPriorityAgentErrorCategory:
    return {
        IsolatedStructuredModelStatus.AUTHENTICATION_UNAVAILABLE: (
            ProductionPriorityAgentErrorCategory.AUTHENTICATION_UNAVAILABLE
        ),
        IsolatedStructuredModelStatus.ISOLATION_UNAVAILABLE: (
            ProductionPriorityAgentErrorCategory.ISOLATION_UNAVAILABLE
        ),
        IsolatedStructuredModelStatus.TEXT_INPUT_TOO_LARGE: (
            ProductionPriorityAgentErrorCategory.INPUT_TOO_LARGE
        ),
        IsolatedStructuredModelStatus.OUTPUT_TOO_LARGE: (
            ProductionPriorityAgentErrorCategory.OUTPUT_TOO_LARGE
        ),
        IsolatedStructuredModelStatus.SCHEMA_OUTPUT_INVALID: (
            ProductionPriorityAgentErrorCategory.SCHEMA_OUTPUT_INVALID
        ),
        IsolatedStructuredModelStatus.TOOL_ATTEMPTED: (
            ProductionPriorityAgentErrorCategory.TOOL_ATTEMPTED
        ),
    }.get(
        status,
        ProductionPriorityAgentErrorCategory.BACKEND_UNAVAILABLE,
    )


class StructuredBackendPriorityAgentAdapter:
    """One async structured generation over an already-resolved backend."""

    def __init__(
        self,
        *,
        resolved_backend: ResolvedComponentBackend,
        call_metadata: ProductionPriorityAgentCallMetadata,
        limits: ProductionPriorityAgentLimits,
    ) -> None:
        if resolved_backend.component_id != PRIORITY_MODEL_COMPONENT_ID:
            raise ValueError("resolved backend component is not Priority")
        method = getattr(
            resolved_backend.backend,
            "complete_structured_request",
            None,
        )
        if not callable(method) or not inspect.iscoroutinefunction(method):
            raise ModelBackendResolutionError(
                ModelBackendResolutionFailure.BACKEND_UNAVAILABLE,
                component_id=PRIORITY_MODEL_COMPONENT_ID,
                backend_id=resolved_backend.selected_backend_id,
                status=ModelBackendResolutionStatus.BACKEND_UNAVAILABLE,
                transport=resolved_backend.transport,
                isolation_profile_id=resolved_backend.isolation_profile_id,
            )
        self._resolved_backend = resolved_backend
        self._complete = method
        self._call_metadata = call_metadata
        self._metadata = _stable_domain_metadata(call_metadata)
        self._limits = limits

    @property
    def metadata(self) -> PriorityAgentMetadata:
        return self._metadata

    @property
    def call_metadata(self) -> ProductionPriorityAgentCallMetadata:
        return self._call_metadata

    async def evaluate(self, context: PriorityContext) -> PriorityAgentOutput:
        input_data = priority_context_data(context)
        request = IsolatedStructuredModelRequest(
            component_id=PRIORITY_MODEL_COMPONENT_ID,
            invocation_id=_invocation_id(
                input_data=input_data,
                metadata=self.call_metadata,
            ),
            model_id=self.call_metadata.model_id,
            system_prompt=PRIORITY_AGENT_SYSTEM_PROMPT,
            input_data=input_data,
            images=(),
            output_schema_name=PRIORITY_AGENT_OUTPUT_SCHEMA_NAME,
            output_schema=PRIORITY_AGENT_OUTPUT_SCHEMA,
            timeout_seconds=self._limits.timeout_seconds,
            max_input_bytes=self._limits.max_input_bytes,
            max_output_bytes=self._limits.max_output_bytes,
            max_images=1,
            prompt_contract_version=DEFAULT_PROMPT_VERSION,
            schema_contract_version=PRIORITY_AGENT_OUTPUT_SCHEMA_VERSION,
        )
        if request.total_input_byte_count() > request.max_input_bytes:
            cause = ProductionPriorityAgentRuntimeError(
                ProductionPriorityAgentErrorCategory.INPUT_TOO_LARGE,
                backend_id=self.call_metadata.backend_id,
            )
            raise PriorityAgentUnavailableError(
                "priority provider input exceeds the bounded policy"
            ) from cause
        try:
            raw = await self._complete(request)
        except TimeoutError:
            raise
        except (TypeError, ValueError):
            cause = ProductionPriorityAgentRuntimeError(
                ProductionPriorityAgentErrorCategory.SCHEMA_OUTPUT_INVALID,
                backend_id=self.call_metadata.backend_id,
            )
            raise PriorityAgentOutputInvalidError(
                "priority provider output is not valid structured JSON"
            ) from cause
        except Exception:
            cause = ProductionPriorityAgentRuntimeError(
                ProductionPriorityAgentErrorCategory.TRANSPORT_FAILURE,
                backend_id=self.call_metadata.backend_id,
            )
            raise PriorityAgentUnavailableError(
                "priority provider is unavailable"
            ) from cause
        if isinstance(raw, IsolatedStructuredModelResult):
            if raw.status is IsolatedStructuredModelStatus.TIMEOUT:
                raise TimeoutError("priority provider timed out")
            if raw.status is not IsolatedStructuredModelStatus.SUCCEEDED:
                cause = ProductionPriorityAgentRuntimeError(
                    _runtime_category(raw.status),
                    backend_id=self.call_metadata.backend_id,
                )
                error_type = (
                    PriorityAgentOutputInvalidError
                    if raw.status in _INVALID_RESULT_STATUSES
                    else PriorityAgentUnavailableError
                )
                raise error_type(
                    "priority provider did not return an accepted output"
                ) from cause
            raw = raw.output
        if not isinstance(raw, Mapping):
            raise PriorityAgentOutputInvalidError(
                "priority provider output is not a structured object"
            )
        try:
            encoded = json.dumps(
                dict(raw),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError):
            raise PriorityAgentOutputInvalidError(
                "priority provider output is not canonical JSON"
            ) from None
        if len(encoded) > request.max_output_bytes:
            cause = ProductionPriorityAgentRuntimeError(
                ProductionPriorityAgentErrorCategory.OUTPUT_TOO_LARGE,
                backend_id=self.call_metadata.backend_id,
            )
            raise PriorityAgentOutputInvalidError(
                "priority provider output exceeds the bounded policy"
            ) from cause
        try:
            Draft202012Validator(
                dict(PRIORITY_AGENT_OUTPUT_SCHEMA)
            ).validate(raw)
            return priority_agent_output_from_data(raw)
        except (ValidationError, AttributeError, KeyError, TypeError, ValueError):
            raise PriorityAgentOutputInvalidError(
                "priority provider output does not match PriorityAgentOutput"
            ) from None


def _selected_backend_id(ai_config: Mapping[str, Any]) -> str:
    components = ai_config.get("components", {})
    if (
        isinstance(components, Mapping)
        and PRIORITY_MODEL_COMPONENT_ID in components
    ):
        value = components[PRIORITY_MODEL_COMPONENT_ID]
    else:
        value = ai_config.get("default_backend", "codex_cli")
    return value if isinstance(value, str) else ""


def build_production_priority_agent(
    *,
    ai_config: Mapping[str, Any],
    backend_registry: Mapping[str, type] | None = None,
    isolation_profile_registry: Mapping[
        str, ModelExecutionIsolationProfile
    ] | None = None,
    limits: ProductionPriorityAgentLimits | None = None,
) -> StructuredBackendPriorityAgentAdapter:
    """Resolve one mandatory backend without fallback or a model probe."""

    if backend_registry is None:
        from utils.llm import model_backend_registry

        backend_registry = model_backend_registry()
    if isolation_profile_registry is None:
        if _selected_backend_id(ai_config) == "codex_cli":
            from utils.isolated_subscription_cli import (
                runtime_model_execution_isolation_profiles,
            )

            isolation_profile_registry = (
                runtime_model_execution_isolation_profiles()
            )
        else:
            isolation_profile_registry = MODEL_EXECUTION_ISOLATION_PROFILES
    resolved = resolve_component_backend(
        ai_config=ai_config,
        component_id=PRIORITY_MODEL_COMPONENT_ID,
        backend_registry=backend_registry,
        component_requirements_registry=PRIORITY_COMPONENT_REQUIREMENTS,
        isolation_profile_registry=isolation_profile_registry,
    )
    active_limits = limits or ProductionPriorityAgentLimits()
    model_id = str(
        getattr(resolved.backend, "model", "")
        or resolved.selected_backend_id + "-provider-default"
    )
    call_metadata = ProductionPriorityAgentCallMetadata(
        component_id=PRIORITY_MODEL_COMPONENT_ID,
        adapter_contract_version=(
            PRODUCTION_PRIORITY_AGENT_ADAPTER_CONTRACT_VERSION
        ),
        factory_contract_version=(
            PRODUCTION_PRIORITY_AGENT_FACTORY_CONTRACT_VERSION
        ),
        backend_id=resolved.selected_backend_id,
        backend_transport=resolved.transport.value,
        authentication_mode=resolved.authentication_mode.value,
        model_id=model_id,
        prompt_policy_version=DEFAULT_PROMPT_VERSION,
        schema_version=PRIORITY_AGENT_OUTPUT_SCHEMA_VERSION,
        backend_capability_contract_version=(
            resolved.backend_capability_contract_version
        ),
        component_requirements_version=(
            resolved.component_requirements_version
        ),
        native_capability_contract_version=(
            resolved.native_capability_contract_version
        ),
        isolation_profile_id=resolved.isolation_profile_id,
        isolation_profile_version=resolved.isolation_contract_version,
        effective_capability_contract_version=(
            resolved.effective_capability_contract_version
        ),
        backend_resolution_identity=resolved.resolution_identity,
    )
    return StructuredBackendPriorityAgentAdapter(
        resolved_backend=resolved,
        call_metadata=call_metadata,
        limits=active_limits,
    )


__all__ = [
    "PRODUCTION_PRIORITY_AGENT_ADAPTER_CONTRACT_VERSION",
    "PRODUCTION_PRIORITY_AGENT_FACTORY_CONTRACT_VERSION",
    "PRODUCTION_PRIORITY_AGENT_LIMITS_CONTRACT_VERSION",
    "ProductionPriorityAgentCallMetadata",
    "ProductionPriorityAgentErrorCategory",
    "ProductionPriorityAgentLimits",
    "ProductionPriorityAgentRuntimeError",
    "StructuredBackendPriorityAgentAdapter",
    "build_production_priority_agent",
]
