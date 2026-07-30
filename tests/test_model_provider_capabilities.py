"""Focused M1a capability-contract and backend-resolution tests."""

from __future__ import annotations

import pytest

from core.model_provider_capabilities import (
    BackendCompatibilityStatus,
    ModelBackendResolutionError,
    ModelBackendResolutionFailure,
    PREPARATION_COMPONENT_REQUIREMENTS,
    PREPARATION_MODEL_COMPONENT_IDS,
    check_backend_compatibility,
    preflight_preparation_component_backends,
    resolve_component_backend,
)
from utils.llm import (
    CodexCLIBackend,
    OpenAIAPIBackend,
    model_backend_registry,
)


def _openai_config(*, explicit: bool = True) -> dict:
    components = (
        {
            component_id: "openai_api"
            for component_id in PREPARATION_MODEL_COMPONENT_IDS
        }
        if explicit
        else {}
    )
    return {
        "default_backend": "openai_api",
        "backends": {
            "openai_api": {
                "api_key_env": "SYNTHETIC_OPENAI_KEY",
                "model": "synthetic-model",
                "timeout": 30,
                "store": False,
            }
        },
        "components": components,
    }


def test_codex_is_trusted_only_and_incompatible_with_all_preparation() -> None:
    capabilities = CodexCLIBackend.capabilities
    assert capabilities.safe_for_untrusted_input is False
    assert capabilities.filesystem_access_mode.value == "READ_ONLY"
    assert capabilities.shell_access_mode.value == "AVAILABLE"
    assert capabilities.tool_execution_mode.value == "HOST_AGENT_TOOLS"

    for component_id in PREPARATION_MODEL_COMPONENT_IDS:
        result = check_backend_compatibility(
            capabilities, PREPARATION_COMPONENT_REQUIREMENTS[component_id]
        )
        assert result.status is BackendCompatibilityStatus.INCOMPATIBLE
        assert "UNTRUSTED_INPUT_SAFETY" in (
            result.missing_or_forbidden_capabilities
        )


def test_openai_supports_eight_text_components_but_not_visual_qa(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SYNTHETIC_OPENAI_KEY", "secret-not-in-result")
    registry = model_backend_registry()
    config = _openai_config()
    identities = []
    for component_id in PREPARATION_MODEL_COMPONENT_IDS[:-1]:
        resolved = resolve_component_backend(
            ai_config=config,
            component_id=component_id,
            backend_registry=registry,
        )
        assert resolved.compatibility.status is (
            BackendCompatibilityStatus.COMPATIBLE
        )
        assert isinstance(resolved.backend, OpenAIAPIBackend)
        assert "secret-not-in-result" not in resolved.resolution_identity
        identities.append(resolved.resolution_identity)
    assert len(set(identities)) == 8

    with pytest.raises(ModelBackendResolutionError) as exc_info:
        resolve_component_backend(
            ai_config=config,
            component_id="resume_visual_qa",
            backend_registry=registry,
        )
    assert exc_info.value.reason is (
        ModelBackendResolutionFailure.INCOMPATIBLE_CAPABILITIES
    )
    assert exc_info.value.details == ("IMAGE_INPUT",)


def test_resolution_is_deterministic_and_never_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("SYNTHETIC_OPENAI_KEY", "synthetic-secret")
    registry = model_backend_registry()
    explicit = _openai_config()
    first = resolve_component_backend(
        ai_config=explicit,
        component_id="resume_selection",
        backend_registry=registry,
    )
    second = resolve_component_backend(
        ai_config=explicit,
        component_id="resume_selection",
        backend_registry=registry,
    )
    assert first.resolution_identity == second.resolution_identity

    incompatible = _openai_config()
    incompatible["default_backend"] = "openai_api"
    incompatible["components"]["resume_selection"] = "codex_cli"
    with pytest.raises(ModelBackendResolutionError) as exc_info:
        resolve_component_backend(
            ai_config=incompatible,
            component_id="resume_selection",
            backend_registry=registry,
        )
    assert exc_info.value.backend_id == "codex_cli"
    assert exc_info.value.reason is (
        ModelBackendResolutionFailure.INCOMPATIBLE_CAPABILITIES
    )


def test_preflight_distinguishes_closed_failures_without_secrets(
    monkeypatch,
) -> None:
    registry = model_backend_registry()
    config = _openai_config()
    monkeypatch.delenv("SYNTHETIC_OPENAI_KEY", raising=False)
    with pytest.raises(ModelBackendResolutionError) as credential:
        resolve_component_backend(
            ai_config=config,
            component_id="resume_selection",
            backend_registry=registry,
        )
    assert credential.value.reason is (
        ModelBackendResolutionFailure.MISSING_CREDENTIAL
    )
    assert "SYNTHETIC_OPENAI_KEY" not in str(credential.value)

    missing = _openai_config()
    missing["components"]["resume_selection"] = "not-registered"
    with pytest.raises(ModelBackendResolutionError) as absent:
        resolve_component_backend(
            ai_config=missing,
            component_id="resume_selection",
            backend_registry=registry,
        )
    assert absent.value.reason is ModelBackendResolutionFailure.MISSING_BACKEND

    class UndeclaredBackend:
        def __init__(self, _config):
            pass

    unsupported = _openai_config()
    unsupported["components"]["resume_selection"] = "undeclared"
    with pytest.raises(ModelBackendResolutionError) as version:
        resolve_component_backend(
            ai_config=unsupported,
            component_id="resume_selection",
            backend_registry={**registry, "undeclared": UndeclaredBackend},
        )
    assert version.value.reason is (
        ModelBackendResolutionFailure.UNSUPPORTED_CAPABILITY_VERSION
    )

    monkeypatch.setenv("SYNTHETIC_OPENAI_KEY", "synthetic-secret")
    with pytest.raises(ModelBackendResolutionError) as visual:
        preflight_preparation_component_backends(
            ai_config=config, backend_registry=registry
        )
    assert visual.value.component_id == "resume_visual_qa"
    assert visual.value.reason is (
        ModelBackendResolutionFailure.INCOMPATIBLE_CAPABILITIES
    )
