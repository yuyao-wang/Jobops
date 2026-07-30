"""Versioned model-backend capabilities and deterministic component resolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION = "model-backend-capabilities-v1"
MODEL_COMPONENT_REQUIREMENTS_CONTRACT_VERSION = (
    "model-component-requirements-v1"
)
MODEL_BACKEND_RESOLUTION_CONTRACT_VERSION = "model-backend-resolution-v1"
NATIVE_MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION = (
    "native-model-backend-capabilities-v1"
)
MODEL_EXECUTION_ISOLATION_CONTRACT_VERSION = (
    "model-execution-isolation-profile-v1"
)
EFFECTIVE_MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION = (
    "effective-model-backend-capabilities-v1"
)


class ModelBackendTransport(StrEnum):
    DIRECT_API = "DIRECT_API"
    SUBSCRIPTION_CLI = "SUBSCRIPTION_CLI"
    LOCAL_RUNTIME = "LOCAL_RUNTIME"
    CUSTOM_REMOTE = "CUSTOM_REMOTE"


class ModelBackendAuthenticationMode(StrEnum):
    API_KEY_ENV = "API_KEY_ENV"
    SUBSCRIPTION_SESSION = "SUBSCRIPTION_SESSION"
    LOCAL_NO_CREDENTIAL = "LOCAL_NO_CREDENTIAL"
    EXTERNAL_CREDENTIAL_BROKER = "EXTERNAL_CREDENTIAL_BROKER"


class BackendAccessLevel(StrEnum):
    NONE = "NONE"
    RESTRICTABLE = "RESTRICTABLE"
    PRESENT = "PRESENT"
    UNKNOWN = "UNKNOWN"


class ToolExecutionMode(StrEnum):
    NONE = "NONE"
    PROVIDER_MANAGED_UNVERIFIED = "PROVIDER_MANAGED_UNVERIFIED"
    HOST_AGENT_TOOLS = "HOST_AGENT_TOOLS"


class FilesystemAccessMode(StrEnum):
    NONE = "NONE"
    READ_ONLY = "READ_ONLY"
    READ_WRITE = "READ_WRITE"


class ShellAccessMode(StrEnum):
    NONE = "NONE"
    AVAILABLE = "AVAILABLE"


class AuxiliaryAccessMode(StrEnum):
    NONE = "NONE"
    AVAILABLE = "AVAILABLE"


class CredentialSourcePolicy(StrEnum):
    RUNTIME_ENVIRONMENT_ONLY = "RUNTIME_ENVIRONMENT_ONLY"
    LOCAL_CLI_AUTH = "LOCAL_CLI_AUTH"
    UNVERIFIED = "UNVERIFIED"


class ComponentInputTrust(StrEnum):
    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"


@dataclass(frozen=True, slots=True)
class ModelBackendCapabilities:
    backend_id: str
    supports_text_input: bool
    supports_image_input: bool
    supports_strict_json_schema: bool
    supports_single_semantic_generation: bool
    safe_for_untrusted_input: bool
    tool_execution_mode: ToolExecutionMode
    filesystem_access_mode: FilesystemAccessMode
    shell_access_mode: ShellAccessMode
    browser_access_mode: AuxiliaryAccessMode
    external_function_access_mode: AuxiliaryAccessMode
    credential_source_policy: CredentialSourcePolicy
    provider_family: str = "legacy"
    transport: ModelBackendTransport = ModelBackendTransport.CUSTOM_REMOTE
    authentication_mode: ModelBackendAuthenticationMode = (
        ModelBackendAuthenticationMode.EXTERNAL_CREDENTIAL_BROKER
    )
    capability_contract_version: str = (
        MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if not isinstance(self.backend_id, str) or not self.backend_id.strip():
            raise ValueError("backend_id must be non-empty")
        if (
            self.capability_contract_version
            != MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION
        ):
            raise ValueError("backend capability contract version is unsupported")
        if not isinstance(self.provider_family, str) or not (
            self.provider_family.strip()
        ):
            raise ValueError("provider_family must be non-empty")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "browser_access_mode": self.browser_access_mode.value,
            "capability_contract_version": self.capability_contract_version,
            "credential_source_policy": self.credential_source_policy.value,
            "external_function_access_mode": (
                self.external_function_access_mode.value
            ),
            "filesystem_access_mode": self.filesystem_access_mode.value,
            "authentication_mode": self.authentication_mode.value,
            "provider_family": self.provider_family,
            "safe_for_untrusted_input": self.safe_for_untrusted_input,
            "shell_access_mode": self.shell_access_mode.value,
            "supports_image_input": self.supports_image_input,
            "supports_single_semantic_generation": (
                self.supports_single_semantic_generation
            ),
            "supports_strict_json_schema": self.supports_strict_json_schema,
            "supports_text_input": self.supports_text_input,
            "tool_execution_mode": self.tool_execution_mode.value,
            "transport": self.transport.value,
        }


@dataclass(frozen=True, slots=True)
class NativeModelBackendCapabilities:
    backend_id: str
    provider_family: str
    transport: ModelBackendTransport
    authentication_mode: ModelBackendAuthenticationMode
    supports_text_input: bool
    supports_image_input: bool
    supports_schema_constrained_output: bool
    supports_provider_native_strict_schema: bool
    supports_single_semantic_generation: bool
    supports_ephemeral_workspace: bool
    supports_non_interactive_execution: bool
    supports_bounded_output: bool
    supports_subscription_authentication: bool
    native_tool_access: BackendAccessLevel
    native_filesystem_access: BackendAccessLevel
    native_shell_access: BackendAccessLevel
    native_browser_access: BackendAccessLevel
    native_external_function_access: BackendAccessLevel
    native_capability_contract_version: str = (
        NATIVE_MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if (
            self.native_capability_contract_version
            != NATIVE_MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION
        ):
            raise ValueError("native capability contract version is unsupported")
        if not self.backend_id or not self.provider_family:
            raise ValueError("native capability identity is incomplete")

    def identity_dict(self) -> dict[str, Any]:
        return {
            field: (
                value.value
                if isinstance(value, StrEnum)
                else value
            )
            for field, value in (
                (name, getattr(self, name))
                for name in self.__dataclass_fields__
            )
        }


@dataclass(frozen=True, slots=True)
class ModelExecutionIsolationProfile:
    isolation_profile_id: str
    runner_available: bool
    ephemeral_workspace: bool
    repository_visible: bool
    private_home_visible: bool
    host_home_visible: bool
    ambient_environment_visible: bool
    credential_files_visible: bool
    filesystem_access: BackendAccessLevel
    shell_access: BackendAccessLevel
    tool_access: BackendAccessLevel
    browser_access: BackendAccessLevel
    external_function_access: BackendAccessLevel
    network_access: BackendAccessLevel
    max_wall_time_seconds: int
    max_input_bytes: int
    max_output_bytes: int
    max_semantic_generations: int
    cleanup_required: bool
    diagnostic_redaction_required: bool
    isolation_contract_version: str = (
        MODEL_EXECUTION_ISOLATION_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if (
            self.isolation_contract_version
            != MODEL_EXECUTION_ISOLATION_CONTRACT_VERSION
        ):
            raise ValueError("isolation profile version is unsupported")
        if not self.isolation_profile_id:
            raise ValueError("isolation_profile_id must be non-empty")
        limits = (
            self.max_wall_time_seconds,
            self.max_input_bytes,
            self.max_output_bytes,
            self.max_semantic_generations,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("isolation limits must be positive")


@dataclass(frozen=True, slots=True)
class EffectiveModelBackendCapabilities:
    backend_id: str
    safe_for_untrusted_text: bool
    safe_for_untrusted_images: bool
    effective_tool_access: BackendAccessLevel
    effective_filesystem_access: BackendAccessLevel
    effective_shell_access: BackendAccessLevel
    effective_browser_access: BackendAccessLevel
    effective_external_function_access: BackendAccessLevel
    effective_network_access: BackendAccessLevel
    effective_credential_exposure: BackendAccessLevel
    supports_schema_constrained_output: bool
    supports_provider_native_strict_schema: bool
    supports_single_semantic_generation: bool
    supports_text_input: bool
    supports_image_input: bool
    effective_capability_contract_version: str = (
        EFFECTIVE_MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if (
            self.effective_capability_contract_version
            != EFFECTIVE_MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION
        ):
            raise ValueError("effective capability version is unsupported")


@dataclass(frozen=True, slots=True)
class ModelComponentRequirements:
    component_id: str
    input_trust: ComponentInputTrust
    requires_text_input: bool
    requires_image_input: bool
    requires_strict_json_schema: bool
    requires_single_semantic_generation: bool
    required_tool_execution_mode: ToolExecutionMode
    required_filesystem_access_mode: FilesystemAccessMode
    required_shell_access_mode: ShellAccessMode
    required_browser_access_mode: AuxiliaryAccessMode
    required_external_function_access_mode: AuxiliaryAccessMode
    requirements_contract_version: str = (
        MODEL_COMPONENT_REQUIREMENTS_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if not isinstance(self.component_id, str) or not self.component_id.strip():
            raise ValueError("component_id must be non-empty")
        if (
            self.requirements_contract_version
            != MODEL_COMPONENT_REQUIREMENTS_CONTRACT_VERSION
        ):
            raise ValueError("component requirements version is unsupported")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "input_trust": self.input_trust.value,
            "required_browser_access_mode": (
                self.required_browser_access_mode.value
            ),
            "required_external_function_access_mode": (
                self.required_external_function_access_mode.value
            ),
            "required_filesystem_access_mode": (
                self.required_filesystem_access_mode.value
            ),
            "required_shell_access_mode": self.required_shell_access_mode.value,
            "required_tool_execution_mode": (
                self.required_tool_execution_mode.value
            ),
            "requirements_contract_version": (
                self.requirements_contract_version
            ),
            "requires_image_input": self.requires_image_input,
            "requires_single_semantic_generation": (
                self.requires_single_semantic_generation
            ),
            "requires_strict_json_schema": (
                self.requires_strict_json_schema
            ),
            "requires_text_input": self.requires_text_input,
        }


class BackendCompatibilityStatus(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"


@dataclass(frozen=True, slots=True)
class BackendCompatibilityResult:
    status: BackendCompatibilityStatus
    backend_id: str
    component_id: str
    missing_or_forbidden_capabilities: tuple[str, ...]


class ModelBackendResolutionFailure(StrEnum):
    INCOMPATIBLE_CAPABILITIES = "INCOMPATIBLE_CAPABILITIES"
    MISSING_BACKEND = "MISSING_BACKEND"
    MISSING_CREDENTIAL = "MISSING_CREDENTIAL"
    UNKNOWN_COMPONENT_REQUIREMENTS = "UNKNOWN_COMPONENT_REQUIREMENTS"
    UNSUPPORTED_CAPABILITY_VERSION = "UNSUPPORTED_CAPABILITY_VERSION"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    ISOLATION_UNAVAILABLE = "ISOLATION_UNAVAILABLE"


class ModelBackendResolutionStatus(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    BACKEND_NOT_FOUND = "BACKEND_NOT_FOUND"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    CREDENTIAL_UNAVAILABLE = "CREDENTIAL_UNAVAILABLE"
    ISOLATION_UNAVAILABLE = "ISOLATION_UNAVAILABLE"
    CAPABILITY_INCOMPATIBLE = "CAPABILITY_INCOMPATIBLE"
    MODALITY_UNSUPPORTED = "MODALITY_UNSUPPORTED"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    CONTRACT_VERSION_UNSUPPORTED = "CONTRACT_VERSION_UNSUPPORTED"


class BackendCredentialUnavailableError(RuntimeError):
    """A configured backend has no runtime credential."""


class ModelBackendResolutionError(RuntimeError):
    """Typed, bounded failure from deterministic backend resolution."""

    def __init__(
        self,
        reason: ModelBackendResolutionFailure,
        *,
        component_id: str,
        backend_id: str | None,
        details: tuple[str, ...] = (),
        status: ModelBackendResolutionStatus | None = None,
        transport: ModelBackendTransport | None = None,
        isolation_profile_id: str | None = None,
    ) -> None:
        self.reason = ModelBackendResolutionFailure(reason)
        self.component_id = component_id
        self.backend_id = backend_id
        self.details = details
        self.status = status or _legacy_failure_status(self.reason, details)
        self.transport = transport
        self.isolation_profile_id = isolation_profile_id
        suffix = f": {','.join(details)}" if details else ""
        boundary = (
            f" status={self.status.value}"
            f" transport={transport.value if transport else 'UNKNOWN'}"
            f" isolation={isolation_profile_id or 'NONE'}"
        )
        super().__init__(
            f"{self.reason.value} component={component_id} "
            f"backend={backend_id or 'NONE'}{boundary}{suffix}"
        )


@dataclass(frozen=True, slots=True)
class ResolvedComponentBackend:
    component_id: str
    selected_backend_id: str
    backend_capability_contract_version: str
    component_requirements_version: str
    compatibility: BackendCompatibilityResult
    resolution_identity: str
    backend: Any
    transport: ModelBackendTransport
    authentication_mode: ModelBackendAuthenticationMode
    native_capability_contract_version: str
    isolation_profile_id: str
    isolation_contract_version: str
    effective_capability_contract_version: str
    resolution_contract_version: str = (
        MODEL_BACKEND_RESOLUTION_CONTRACT_VERSION
    )


def _legacy_failure_status(
    reason: ModelBackendResolutionFailure, details: tuple[str, ...]
) -> ModelBackendResolutionStatus:
    if reason is ModelBackendResolutionFailure.MISSING_BACKEND:
        return ModelBackendResolutionStatus.BACKEND_NOT_FOUND
    if reason is ModelBackendResolutionFailure.MISSING_CREDENTIAL:
        return ModelBackendResolutionStatus.CREDENTIAL_UNAVAILABLE
    if reason is ModelBackendResolutionFailure.BACKEND_UNAVAILABLE:
        return ModelBackendResolutionStatus.BACKEND_UNAVAILABLE
    if reason is ModelBackendResolutionFailure.ISOLATION_UNAVAILABLE:
        return ModelBackendResolutionStatus.ISOLATION_UNAVAILABLE
    if reason is ModelBackendResolutionFailure.UNSUPPORTED_CAPABILITY_VERSION:
        return ModelBackendResolutionStatus.CONTRACT_VERSION_UNSUPPORTED
    if "IMAGE_INPUT" in details or "TEXT_INPUT" in details:
        return ModelBackendResolutionStatus.MODALITY_UNSUPPORTED
    if "STRICT_JSON_SCHEMA" in details:
        return ModelBackendResolutionStatus.SCHEMA_UNSUPPORTED
    return ModelBackendResolutionStatus.CAPABILITY_INCOMPATIBLE


_TEXT_PREPARATION_COMPONENTS = (
    "resume_selection",
    "resume_tailoring",
    "resume_fact_qa",
    "base_latex_selection",
    "resume_latex_construction",
    "resume_layout_revision",
    "cover_letter",
    "cover_letter_fact_qa",
)
PREPARATION_MODEL_COMPONENT_IDS = (
    *_TEXT_PREPARATION_COMPONENTS,
    "resume_visual_qa",
)


def _preparation_requirement(
    component_id: str, *, image: bool = False
) -> ModelComponentRequirements:
    return ModelComponentRequirements(
        component_id=component_id,
        input_trust=ComponentInputTrust.UNTRUSTED,
        requires_text_input=True,
        requires_image_input=image,
        requires_strict_json_schema=True,
        requires_single_semantic_generation=True,
        required_tool_execution_mode=ToolExecutionMode.NONE,
        required_filesystem_access_mode=FilesystemAccessMode.NONE,
        required_shell_access_mode=ShellAccessMode.NONE,
        required_browser_access_mode=AuxiliaryAccessMode.NONE,
        required_external_function_access_mode=AuxiliaryAccessMode.NONE,
    )


PREPARATION_COMPONENT_REQUIREMENTS: Mapping[
    str, ModelComponentRequirements
] = MappingProxyType(
    {
        component_id: _preparation_requirement(component_id)
        for component_id in _TEXT_PREPARATION_COMPONENTS
    }
    | {
        "resume_visual_qa": _preparation_requirement(
            "resume_visual_qa", image=True
        )
    }
)


def _profile(
    profile_id: str,
    *,
    runner_available: bool,
    isolated: bool,
    network: BackendAccessLevel,
) -> ModelExecutionIsolationProfile:
    access = (
        BackendAccessLevel.NONE
        if isolated
        else BackendAccessLevel.UNKNOWN
    )
    return ModelExecutionIsolationProfile(
        isolation_profile_id=profile_id,
        runner_available=runner_available,
        ephemeral_workspace=isolated,
        repository_visible=not isolated,
        private_home_visible=not isolated,
        host_home_visible=not isolated,
        ambient_environment_visible=not isolated,
        credential_files_visible=not isolated,
        filesystem_access=access,
        shell_access=access,
        tool_access=access,
        browser_access=access,
        external_function_access=access,
        network_access=network,
        max_wall_time_seconds=300,
        max_input_bytes=1_000_000,
        max_output_bytes=1_000_000,
        max_semantic_generations=1,
        cleanup_required=isolated,
        diagnostic_redaction_required=True,
    )


MODEL_EXECUTION_ISOLATION_PROFILES: Mapping[
    str, ModelExecutionIsolationProfile
] = MappingProxyType(
    {
        "NO_ISOLATION": _profile(
            "NO_ISOLATION",
            runner_available=True,
            isolated=False,
            network=BackendAccessLevel.UNKNOWN,
        ),
        "DIRECT_STRUCTURED_API_V1": _profile(
            "DIRECT_STRUCTURED_API_V1",
            runner_available=True,
            isolated=True,
            network=BackendAccessLevel.PRESENT,
        ),
        "ISOLATED_SUBSCRIPTION_CLI_V1": _profile(
            "ISOLATED_SUBSCRIPTION_CLI_V1",
            runner_available=False,
            isolated=True,
            network=BackendAccessLevel.RESTRICTABLE,
        ),
        "LOCAL_RUNTIME_V1": _profile(
            "LOCAL_RUNTIME_V1",
            runner_available=False,
            isolated=True,
            network=BackendAccessLevel.NONE,
        ),
    }
)


def model_execution_isolation_profiles(
    *, isolated_subscription_cli_runner_available: bool = False
) -> Mapping[str, ModelExecutionIsolationProfile]:
    """Build the runtime registry without changing profile semantics."""

    profiles = dict(MODEL_EXECUTION_ISOLATION_PROFILES)
    profiles["ISOLATED_SUBSCRIPTION_CLI_V1"] = replace(
        profiles["ISOLATED_SUBSCRIPTION_CLI_V1"],
        runner_available=isolated_subscription_cli_runner_available,
    )
    return MappingProxyType(profiles)


def native_capabilities_from_legacy(
    capabilities: ModelBackendCapabilities,
) -> NativeModelBackendCapabilities:
    """Read an M1a declaration through an explicit conservative legacy map."""

    access_map = {
        ToolExecutionMode.NONE: BackendAccessLevel.NONE,
        ToolExecutionMode.PROVIDER_MANAGED_UNVERIFIED: (
            BackendAccessLevel.UNKNOWN
        ),
        ToolExecutionMode.HOST_AGENT_TOOLS: BackendAccessLevel.PRESENT,
        FilesystemAccessMode.NONE: BackendAccessLevel.NONE,
        FilesystemAccessMode.READ_ONLY: BackendAccessLevel.PRESENT,
        FilesystemAccessMode.READ_WRITE: BackendAccessLevel.PRESENT,
        ShellAccessMode.NONE: BackendAccessLevel.NONE,
        ShellAccessMode.AVAILABLE: BackendAccessLevel.PRESENT,
        AuxiliaryAccessMode.NONE: BackendAccessLevel.NONE,
        AuxiliaryAccessMode.AVAILABLE: BackendAccessLevel.PRESENT,
    }
    return NativeModelBackendCapabilities(
        backend_id=capabilities.backend_id,
        provider_family=capabilities.provider_family,
        transport=capabilities.transport,
        authentication_mode=capabilities.authentication_mode,
        supports_text_input=capabilities.supports_text_input,
        supports_image_input=capabilities.supports_image_input,
        supports_schema_constrained_output=(
            capabilities.supports_strict_json_schema
        ),
        supports_provider_native_strict_schema=(
            capabilities.supports_strict_json_schema
        ),
        supports_single_semantic_generation=(
            capabilities.supports_single_semantic_generation
        ),
        supports_ephemeral_workspace=False,
        supports_non_interactive_execution=True,
        supports_bounded_output=False,
        supports_subscription_authentication=(
            capabilities.authentication_mode
            is ModelBackendAuthenticationMode.SUBSCRIPTION_SESSION
        ),
        native_tool_access=access_map[capabilities.tool_execution_mode],
        native_filesystem_access=access_map[
            capabilities.filesystem_access_mode
        ],
        native_shell_access=access_map[capabilities.shell_access_mode],
        native_browser_access=access_map[capabilities.browser_access_mode],
        native_external_function_access=access_map[
            capabilities.external_function_access_mode
        ],
    )


def derive_effective_capabilities(
    native: NativeModelBackendCapabilities,
    isolation: ModelExecutionIsolationProfile,
) -> EffectiveModelBackendCapabilities:
    """Derive expected effective capabilities; never invent modality/schema."""

    no_isolation = isolation.isolation_profile_id == "NO_ISOLATION"
    tool_access = (
        native.native_tool_access
        if no_isolation
        else isolation.tool_access
    )
    filesystem_access = (
        native.native_filesystem_access
        if no_isolation
        else isolation.filesystem_access
    )
    shell_access = (
        native.native_shell_access
        if no_isolation
        else isolation.shell_access
    )
    browser_access = (
        native.native_browser_access
        if no_isolation
        else isolation.browser_access
    )
    external_access = (
        native.native_external_function_access
        if no_isolation
        else isolation.external_function_access
    )
    isolated = (
        isolation.ephemeral_workspace
        and not isolation.repository_visible
        and not isolation.private_home_visible
        and not isolation.host_home_visible
        and not isolation.ambient_environment_visible
        and isolation.filesystem_access is BackendAccessLevel.NONE
        and isolation.shell_access is BackendAccessLevel.NONE
        and isolation.tool_access is BackendAccessLevel.NONE
        and isolation.browser_access is BackendAccessLevel.NONE
        and isolation.external_function_access is BackendAccessLevel.NONE
        and isolation.max_semantic_generations == 1
    )
    return EffectiveModelBackendCapabilities(
        backend_id=native.backend_id,
        safe_for_untrusted_text=isolated and native.supports_text_input,
        safe_for_untrusted_images=isolated and native.supports_image_input,
        effective_tool_access=tool_access,
        effective_filesystem_access=filesystem_access,
        effective_shell_access=shell_access,
        effective_browser_access=browser_access,
        effective_external_function_access=external_access,
        effective_network_access=isolation.network_access,
        effective_credential_exposure=(
            BackendAccessLevel.PRESENT
            if isolation.credential_files_visible
            else BackendAccessLevel.NONE
        ),
        supports_schema_constrained_output=(
            native.supports_schema_constrained_output
        ),
        supports_provider_native_strict_schema=(
            native.supports_provider_native_strict_schema
        ),
        supports_single_semantic_generation=(
            native.supports_single_semantic_generation
            and isolation.max_semantic_generations == 1
        ),
        supports_text_input=native.supports_text_input,
        supports_image_input=native.supports_image_input,
    )


def _effective_failures(
    effective: EffectiveModelBackendCapabilities,
    requirements: ModelComponentRequirements,
) -> tuple[str, ...]:
    failures: list[str] = []
    if requirements.requires_text_input and not effective.supports_text_input:
        failures.append("TEXT_INPUT")
    if requirements.requires_image_input and not effective.supports_image_input:
        failures.append("IMAGE_INPUT")
    if (
        requirements.requires_strict_json_schema
        and not effective.supports_provider_native_strict_schema
    ):
        failures.append("STRICT_JSON_SCHEMA")
    if (
        requirements.requires_single_semantic_generation
        and not effective.supports_single_semantic_generation
    ):
        failures.append("SINGLE_SEMANTIC_GENERATION")
    modality_available = (
        (not requirements.requires_text_input or effective.supports_text_input)
        and (
            not requirements.requires_image_input
            or effective.supports_image_input
        )
    )
    if (
        modality_available
        and requirements.input_trust is ComponentInputTrust.UNTRUSTED
        and not (
            effective.safe_for_untrusted_images
            if requirements.requires_image_input
            else effective.safe_for_untrusted_text
        )
    ):
        failures.append("UNTRUSTED_INPUT_SAFETY")
    for name, value in (
        ("TOOL_EXECUTION", effective.effective_tool_access),
        ("FILESYSTEM_ACCESS", effective.effective_filesystem_access),
        ("SHELL_ACCESS", effective.effective_shell_access),
        ("BROWSER_ACCESS", effective.effective_browser_access),
        (
            "EXTERNAL_FUNCTION_ACCESS",
            effective.effective_external_function_access,
        ),
        ("CREDENTIAL_EXPOSURE", effective.effective_credential_exposure),
    ):
        if value is not BackendAccessLevel.NONE:
            failures.append(name)
    return tuple(failures)


def check_backend_compatibility(
    capabilities: ModelBackendCapabilities,
    requirements: ModelComponentRequirements,
) -> BackendCompatibilityResult:
    failures: list[str] = []
    if requirements.requires_text_input and not capabilities.supports_text_input:
        failures.append("TEXT_INPUT")
    if requirements.requires_image_input and not capabilities.supports_image_input:
        failures.append("IMAGE_INPUT")
    if (
        requirements.requires_strict_json_schema
        and not capabilities.supports_strict_json_schema
    ):
        failures.append("STRICT_JSON_SCHEMA")
    if (
        requirements.requires_single_semantic_generation
        and not capabilities.supports_single_semantic_generation
    ):
        failures.append("SINGLE_SEMANTIC_GENERATION")
    if (
        requirements.input_trust is ComponentInputTrust.UNTRUSTED
        and not capabilities.safe_for_untrusted_input
    ):
        failures.append("UNTRUSTED_INPUT_SAFETY")
    comparisons = (
        (
            "TOOL_EXECUTION",
            capabilities.tool_execution_mode,
            requirements.required_tool_execution_mode,
        ),
        (
            "FILESYSTEM_ACCESS",
            capabilities.filesystem_access_mode,
            requirements.required_filesystem_access_mode,
        ),
        (
            "SHELL_ACCESS",
            capabilities.shell_access_mode,
            requirements.required_shell_access_mode,
        ),
        (
            "BROWSER_ACCESS",
            capabilities.browser_access_mode,
            requirements.required_browser_access_mode,
        ),
        (
            "EXTERNAL_FUNCTION_ACCESS",
            capabilities.external_function_access_mode,
            requirements.required_external_function_access_mode,
        ),
    )
    for name, actual, required in comparisons:
        if actual is not required:
            failures.append(name)
    return BackendCompatibilityResult(
        status=(
            BackendCompatibilityStatus.INCOMPATIBLE
            if failures
            else BackendCompatibilityStatus.COMPATIBLE
        ),
        backend_id=capabilities.backend_id,
        component_id=requirements.component_id,
        missing_or_forbidden_capabilities=tuple(failures),
    )


_SAFE_IDENTITY_CONFIG_KEYS = frozenset(
    {
        "api_key_env",
        "base_url",
        "max_output_tokens",
        "model",
        "reasoning_effort",
        "store",
        "timeout",
    }
)


def _identity(
    *,
    component_id: str,
    backend_id: str,
    selection_source: str,
    backend_config: Mapping[str, Any],
    capabilities: ModelBackendCapabilities,
    requirements: ModelComponentRequirements,
    native: NativeModelBackendCapabilities,
    isolation: ModelExecutionIsolationProfile,
    effective: EffectiveModelBackendCapabilities,
) -> str:
    safe_config = {
        key: backend_config[key]
        for key in sorted(backend_config)
        if key in _SAFE_IDENTITY_CONFIG_KEYS and key != "api_key"
    }
    content = {
        "backend": capabilities.identity_dict(),
        "backend_config": safe_config,
        "backend_id": backend_id,
        "component": requirements.identity_dict(),
        "component_id": component_id,
        "contract_version": MODEL_BACKEND_RESOLUTION_CONTRACT_VERSION,
        "effective_capability_contract_version": (
            effective.effective_capability_contract_version
        ),
        "isolation_contract_version": isolation.isolation_contract_version,
        "isolation_profile_id": isolation.isolation_profile_id,
        "native": native.identity_dict(),
        "selection_source": selection_source,
    }
    encoded = json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return "model-backend-resolution-" + hashlib.sha256(encoded).hexdigest()


def resolve_component_backend(
    *,
    ai_config: Mapping[str, Any],
    component_id: str,
    backend_registry: Mapping[str, type],
    component_requirements_registry: Mapping[
        str, ModelComponentRequirements
    ] = PREPARATION_COMPONENT_REQUIREMENTS,
    isolation_profile_registry: Mapping[
        str, ModelExecutionIsolationProfile
    ] = MODEL_EXECUTION_ISOLATION_PROFILES,
) -> ResolvedComponentBackend:
    requirements = component_requirements_registry.get(component_id)
    if not isinstance(requirements, ModelComponentRequirements):
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.UNKNOWN_COMPONENT_REQUIREMENTS,
            component_id=component_id,
            backend_id=None,
        )
    components = ai_config.get("components", {})
    explicit = isinstance(components, Mapping) and component_id in components
    backend_id = (
        components.get(component_id)
        if explicit
        else ai_config.get("default_backend", "codex_cli")
    )
    if not isinstance(backend_id, str) or backend_id not in backend_registry:
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.MISSING_BACKEND,
            component_id=component_id,
            backend_id=backend_id if isinstance(backend_id, str) else None,
        )
    backend_class = backend_registry[backend_id]
    capabilities = getattr(backend_class, "capabilities", None)
    if not isinstance(capabilities, ModelBackendCapabilities):
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.UNSUPPORTED_CAPABILITY_VERSION,
            component_id=component_id,
            backend_id=backend_id,
        )
    native = getattr(backend_class, "native_capabilities", None)
    if native is None:
        native = native_capabilities_from_legacy(capabilities)
    if not isinstance(native, NativeModelBackendCapabilities):
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.UNSUPPORTED_CAPABILITY_VERSION,
            component_id=component_id,
            backend_id=backend_id,
        )
    backends = ai_config.get("backends", {})
    backend_config = (
        backends.get(backend_id, {})
        if isinstance(backends, Mapping)
        else {}
    )
    if not isinstance(backend_config, Mapping):
        backend_config = {}
    configured_profile = backend_config.get("isolation_profile")
    if configured_profile is None:
        configured_profile = (
            "DIRECT_STRUCTURED_API_V1"
            if native.transport is ModelBackendTransport.DIRECT_API
            else "NO_ISOLATION"
        )
    if not isinstance(configured_profile, str):
        configured_profile = ""
    configured_profile = configured_profile.strip().upper()
    isolation = isolation_profile_registry.get(configured_profile)
    if not isinstance(isolation, ModelExecutionIsolationProfile):
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.UNSUPPORTED_CAPABILITY_VERSION,
            component_id=component_id,
            backend_id=backend_id,
            status=(
                ModelBackendResolutionStatus.CONTRACT_VERSION_UNSUPPORTED
            ),
            transport=native.transport,
            isolation_profile_id=configured_profile or None,
        )
    effective = derive_effective_capabilities(native, isolation)
    failures = _effective_failures(effective, requirements)
    if failures:
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.INCOMPATIBLE_CAPABILITIES,
            component_id=component_id,
            backend_id=backend_id,
            details=failures,
            transport=native.transport,
            isolation_profile_id=isolation.isolation_profile_id,
        )
    if not isolation.runner_available:
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.ISOLATION_UNAVAILABLE,
            component_id=component_id,
            backend_id=backend_id,
            status=ModelBackendResolutionStatus.ISOLATION_UNAVAILABLE,
            transport=native.transport,
            isolation_profile_id=isolation.isolation_profile_id,
        )
    compatibility = BackendCompatibilityResult(
        status=BackendCompatibilityStatus.COMPATIBLE,
        backend_id=capabilities.backend_id,
        component_id=component_id,
        missing_or_forbidden_capabilities=(),
    )
    try:
        backend = backend_class(dict(backend_config))
    except BackendCredentialUnavailableError:
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.MISSING_CREDENTIAL,
            component_id=component_id,
            backend_id=backend_id,
            transport=native.transport,
            isolation_profile_id=isolation.isolation_profile_id,
        ) from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ModelBackendResolutionError(
            ModelBackendResolutionFailure.BACKEND_UNAVAILABLE,
            component_id=component_id,
            backend_id=backend_id,
            transport=native.transport,
            isolation_profile_id=isolation.isolation_profile_id,
        ) from None
    return ResolvedComponentBackend(
        component_id=component_id,
        selected_backend_id=backend_id,
        backend_capability_contract_version=(
            capabilities.capability_contract_version
        ),
        component_requirements_version=(
            requirements.requirements_contract_version
        ),
        compatibility=compatibility,
        resolution_identity=_identity(
            component_id=component_id,
            backend_id=backend_id,
            selection_source="COMPONENT" if explicit else "DEFAULT",
            backend_config=backend_config,
            capabilities=capabilities,
            requirements=requirements,
            native=native,
            isolation=isolation,
            effective=effective,
        ),
        backend=backend,
        transport=native.transport,
        authentication_mode=native.authentication_mode,
        native_capability_contract_version=(
            native.native_capability_contract_version
        ),
        isolation_profile_id=isolation.isolation_profile_id,
        isolation_contract_version=isolation.isolation_contract_version,
        effective_capability_contract_version=(
            effective.effective_capability_contract_version
        ),
    )


def preflight_preparation_component_backends(
    *,
    ai_config: Mapping[str, Any],
    backend_registry: Mapping[str, type],
) -> tuple[ResolvedComponentBackend, ...]:
    """Resolve all nine mandatory components or fail without a partial result."""

    return tuple(
        resolve_component_backend(
            ai_config=ai_config,
            component_id=component_id,
            backend_registry=backend_registry,
        )
        for component_id in PREPARATION_MODEL_COMPONENT_IDS
    )


__all__ = [
    "AuxiliaryAccessMode",
    "BackendAccessLevel",
    "BackendCompatibilityResult",
    "BackendCompatibilityStatus",
    "BackendCredentialUnavailableError",
    "ComponentInputTrust",
    "CredentialSourcePolicy",
    "EFFECTIVE_MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION",
    "EffectiveModelBackendCapabilities",
    "FilesystemAccessMode",
    "MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION",
    "MODEL_BACKEND_RESOLUTION_CONTRACT_VERSION",
    "MODEL_COMPONENT_REQUIREMENTS_CONTRACT_VERSION",
    "MODEL_EXECUTION_ISOLATION_CONTRACT_VERSION",
    "MODEL_EXECUTION_ISOLATION_PROFILES",
    "ModelBackendAuthenticationMode",
    "ModelBackendCapabilities",
    "ModelBackendResolutionError",
    "ModelBackendResolutionFailure",
    "ModelBackendResolutionStatus",
    "ModelBackendTransport",
    "ModelComponentRequirements",
    "ModelExecutionIsolationProfile",
    "NATIVE_MODEL_BACKEND_CAPABILITY_CONTRACT_VERSION",
    "NativeModelBackendCapabilities",
    "PREPARATION_COMPONENT_REQUIREMENTS",
    "PREPARATION_MODEL_COMPONENT_IDS",
    "ResolvedComponentBackend",
    "ShellAccessMode",
    "ToolExecutionMode",
    "check_backend_compatibility",
    "derive_effective_capabilities",
    "native_capabilities_from_legacy",
    "model_execution_isolation_profiles",
    "preflight_preparation_component_backends",
    "resolve_component_backend",
]
