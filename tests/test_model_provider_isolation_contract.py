"""Focused M1a2 transport, isolation, and effective-capability tests."""

from __future__ import annotations

import pytest

from core.model_provider_capabilities import (
    AuxiliaryAccessMode,
    CredentialSourcePolicy,
    FilesystemAccessMode,
    ModelBackendAuthenticationMode,
    ModelBackendCapabilities,
    ModelBackendResolutionError,
    ModelBackendResolutionFailure,
    ModelBackendResolutionStatus,
    ModelBackendTransport,
    ShellAccessMode,
    ToolExecutionMode,
    check_backend_compatibility,
    native_capabilities_from_legacy,
    PREPARATION_COMPONENT_REQUIREMENTS,
    resolve_component_backend,
)
from utils.llm import (
    CodexCLIBackend,
    OpenAIAPIBackend,
    model_backend_registry,
)


def _config(backend_id: str, *, isolation: str | None = None) -> dict:
    backend = {
        "model": "synthetic-model",
        "api_key_env": "SYNTHETIC_OPENAI_KEY",
    }
    if isolation:
        backend["isolation_profile"] = isolation
    return {
        "default_backend": backend_id,
        "backends": {backend_id: backend},
        "components": {"resume_selection": backend_id},
    }


def test_legacy_m1a_declaration_maps_conservatively() -> None:
    legacy = ModelBackendCapabilities(
        backend_id="legacy-backend",
        supports_text_input=True,
        supports_image_input=False,
        supports_strict_json_schema=True,
        supports_single_semantic_generation=True,
        safe_for_untrusted_input=False,
        tool_execution_mode=ToolExecutionMode.HOST_AGENT_TOOLS,
        filesystem_access_mode=FilesystemAccessMode.READ_ONLY,
        shell_access_mode=ShellAccessMode.AVAILABLE,
        browser_access_mode=AuxiliaryAccessMode.NONE,
        external_function_access_mode=AuxiliaryAccessMode.NONE,
        credential_source_policy=CredentialSourcePolicy.UNVERIFIED,
    )
    native = native_capabilities_from_legacy(legacy)
    assert native.transport is ModelBackendTransport.CUSTOM_REMOTE
    assert native.authentication_mode is (
        ModelBackendAuthenticationMode.EXTERNAL_CREDENTIAL_BROKER
    )
    assert (
        check_backend_compatibility(
            legacy, PREPARATION_COMPONENT_REQUIREMENTS["resume_selection"]
        ).missing_or_forbidden_capabilities
    ) == (
        "UNTRUSTED_INPUT_SAFETY",
        "TOOL_EXECUTION",
        "FILESYSTEM_ACCESS",
        "SHELL_ACCESS",
    )


def test_openai_direct_api_resolves_text_and_rejects_visual_modality(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SYNTHETIC_OPENAI_KEY", "secret-canary")
    registry = model_backend_registry()
    resolved = resolve_component_backend(
        ai_config=_config("openai_api"),
        component_id="resume_selection",
        backend_registry=registry,
    )
    assert resolved.transport is ModelBackendTransport.DIRECT_API
    assert resolved.authentication_mode is (
        ModelBackendAuthenticationMode.API_KEY_ENV
    )
    assert resolved.isolation_profile_id == "DIRECT_STRUCTURED_API_V1"
    assert "secret-canary" not in resolved.resolution_identity

    visual = _config("openai_api")
    visual["components"] = {"resume_visual_qa": "openai_api"}
    with pytest.raises(ModelBackendResolutionError) as exc_info:
        resolve_component_backend(
            ai_config=visual,
            component_id="resume_visual_qa",
            backend_registry=registry,
        )
    assert exc_info.value.status is (
        ModelBackendResolutionStatus.MODALITY_UNSUPPORTED
    )
    assert exc_info.value.details == ("IMAGE_INPUT",)


def test_codex_subscription_is_raw_incompatible_or_isolation_unavailable() -> None:
    registry = model_backend_registry()
    raw = _config("codex_cli")
    with pytest.raises(ModelBackendResolutionError) as raw_error:
        resolve_component_backend(
            ai_config=raw,
            component_id="resume_selection",
            backend_registry=registry,
        )
    assert raw_error.value.status is (
        ModelBackendResolutionStatus.CAPABILITY_INCOMPATIBLE
    )
    assert raw_error.value.transport is (
        ModelBackendTransport.SUBSCRIPTION_CLI
    )

    isolated = _config(
        "codex_cli", isolation="isolated_subscription_cli_v1"
    )
    with pytest.raises(ModelBackendResolutionError) as isolated_error:
        resolve_component_backend(
            ai_config=isolated,
            component_id="resume_selection",
            backend_registry=registry,
        )
    error = isolated_error.value
    assert error.reason is ModelBackendResolutionFailure.ISOLATION_UNAVAILABLE
    assert error.status is ModelBackendResolutionStatus.ISOLATION_UNAVAILABLE
    assert error.isolation_profile_id == "ISOLATED_SUBSCRIPTION_CLI_V1"
    assert error.transport is ModelBackendTransport.SUBSCRIPTION_CLI


def test_selected_backend_never_falls_back_across_auth_modes(
    monkeypatch,
) -> None:
    registry = model_backend_registry()
    monkeypatch.delenv("SYNTHETIC_OPENAI_KEY", raising=False)
    with pytest.raises(ModelBackendResolutionError) as api_error:
        resolve_component_backend(
            ai_config=_config("openai_api"),
            component_id="resume_selection",
            backend_registry=registry,
        )
    assert api_error.value.status is (
        ModelBackendResolutionStatus.CREDENTIAL_UNAVAILABLE
    )
    assert api_error.value.backend_id == "openai_api"

    codex = _config(
        "codex_cli", isolation="isolated_subscription_cli_v1"
    )
    codex["backends"]["openai_api"] = {
        "api_key_env": "SYNTHETIC_OPENAI_KEY",
        "model": "synthetic-model",
    }
    with pytest.raises(ModelBackendResolutionError) as cli_error:
        resolve_component_backend(
            ai_config=codex,
            component_id="resume_selection",
            backend_registry=registry,
        )
    assert cli_error.value.status is (
        ModelBackendResolutionStatus.ISOLATION_UNAVAILABLE
    )
    assert cli_error.value.backend_id == "codex_cli"


def test_config_cannot_forge_capabilities_or_leak_identity(monkeypatch) -> None:
    monkeypatch.setenv("SYNTHETIC_OPENAI_KEY", "identity-secret-canary")
    config = _config("openai_api")
    config["backends"]["openai_api"].update(
        {
            "transport": "SUBSCRIPTION_CLI",
            "safe_for_untrusted_input": False,
            "api_key": "${SYNTHETIC_OPENAI_KEY}",
            "private_path": "/Users/private/candidate.tex",
        }
    )
    resolved = resolve_component_backend(
        ai_config=config,
        component_id="resume_selection",
        backend_registry=model_backend_registry(),
    )
    assert resolved.transport is ModelBackendTransport.DIRECT_API
    assert resolved.authentication_mode is (
        ModelBackendAuthenticationMode.API_KEY_ENV
    )
    assert "identity-secret-canary" not in resolved.resolution_identity
    assert "/Users/private" not in resolved.resolution_identity
    assert "candidate" not in resolved.resolution_identity
    assert OpenAIAPIBackend.capabilities.transport is (
        ModelBackendTransport.DIRECT_API
    )
    assert CodexCLIBackend.capabilities.authentication_mode is (
        ModelBackendAuthenticationMode.SUBSCRIPTION_SESSION
    )
