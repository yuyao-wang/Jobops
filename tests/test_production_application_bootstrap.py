from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import core.production_application_bootstrap as bootstrap_module
from core.model_provider_capabilities import (
    model_execution_isolation_profiles,
)
from core.production_application_bootstrap import (
    PRODUCTION_APPLICATION_BOOTSTRAP_CONTRACT_VERSION,
    PRODUCTION_APPLICATION_CONFIG_CONTRACT_VERSION,
    ProductionApplicationBootstrapError,
    ProductionBootstrapFailure,
    build_production_application_bootstrap,
    load_production_application_config,
    production_application_config_from_mapping,
    resolve_production_config_path,
)
from utils.llm import CodexCLIBackend


def _raw_config(tmp_path: Path) -> dict:
    source = (
        Path(__file__).parents[1]
        / "config"
        / "production.application.example.yaml"
    )
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["private_home"]["root"] = str(tmp_path / "private-home")
    raw["authentication"]["session_secret_ref"] = {
        "source": "ENV",
        "name": "JOBOPS_SYNTHETIC_SESSION_SECRET",
    }
    return raw


def _write_external_config(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "application.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_closed_external_config_and_legacy_profile_are_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_config(tmp_path)
    path = _write_external_config(tmp_path, raw)
    config = load_production_application_config(path)
    assert (
        config.config_contract_version
        == PRODUCTION_APPLICATION_CONFIG_CONTRACT_VERSION
    )
    serialized = json.dumps(dict(config.safe_diagnostics()))
    assert "synthetic" not in serialized
    assert str(tmp_path) not in serialized
    assert "personal" not in serialized

    legacy = tmp_path / "profile.yaml"
    legacy.write_text(
        "personal:\n  email: legacy@example.invalid\n"
        "resume_path: /private/resume.pdf\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    assert resolve_production_config_path(
        cli_path=path,
        environ={"JOBOPS_CONFIG_FILE": str(legacy)},
    ) == path
    raw["personal"] = {"email": "must-not-enter@example.invalid"}
    with pytest.raises(ProductionApplicationBootstrapError) as unknown:
        production_application_config_from_mapping(raw)
    assert unknown.value.failure is (
        ProductionBootstrapFailure.CONFIG_SCHEMA_INVALID
    )
    raw.pop("personal")
    raw["config_contract_version"] = "unsupported"
    with pytest.raises(ProductionApplicationBootstrapError) as unsupported:
        production_application_config_from_mapping(raw)
    assert unsupported.value.failure is (
        ProductionBootstrapFailure.CONFIG_VERSION_UNSUPPORTED
    )


@pytest.mark.asyncio
async def test_complete_bootstrap_constructs_only_typed_dependencies(
    tmp_path: Path,
) -> None:
    config = production_application_config_from_mapping(
        _raw_config(tmp_path)
    )
    playwright_calls = 0

    async def playwright_factory():
        nonlocal playwright_calls
        playwright_calls += 1
        raise AssertionError("bootstrap must not start Playwright")

    built = await build_production_application_bootstrap(
        config,
        environ={
            "JOBOPS_SYNTHETIC_SESSION_SECRET": "synthetic-session-secret"
        },
        backend_registry={"codex_cli": CodexCLIBackend},
        isolation_profile_registry=model_execution_isolation_profiles(
            isolated_subscription_cli_runner_available=True
        ),
        playwright_factory=playwright_factory,
    )
    try:
        assert built.bootstrap_contract_version == (
            PRODUCTION_APPLICATION_BOOTSTRAP_CONTRACT_VERSION
        )
        assert len(built.repository_bundle.repositories) >= 30
        assert built.job_search_factory_inputs.boards == config.search.boards
        assert built.priority_agent_factory_inputs.ai_config[
            "default_backend"
        ] == "codex_cli"
        assert (
            built.preparation_stage_dependencies.agents.resume_visual_qa
            is not None
        )
        assert (
            built.execution_policy_rules.configuration.authority_configured
            is True
        )
        assert built.browser_runtime.context is None
        assert built.automation_runtime_policy.max_bundle_assemblies == 5
        assert playwright_calls == 0
    finally:
        await built.close()


@pytest.mark.asyncio
async def test_secret_failure_and_partial_bootstrap_cleanup_are_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = production_application_config_from_mapping(
        _raw_config(tmp_path)
    )
    with pytest.raises(ProductionApplicationBootstrapError) as missing:
        await build_production_application_bootstrap(
            config,
            environ={},
            backend_registry={"codex_cli": CodexCLIBackend},
            isolation_profile_registry=model_execution_isolation_profiles(
                isolated_subscription_cli_runner_available=True
            ),
        )
    assert missing.value.failure is ProductionBootstrapFailure.SECRET_UNAVAILABLE
    assert "SESSION" not in str(missing.value)

    closed = 0

    class OwnedResource:
        async def close(self) -> None:
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        bootstrap_module,
        "build_production_browser_runtime",
        lambda **_: OwnedResource(),
    )

    class BrokenBootstrap:
        def __init__(self, **_: object) -> None:
            raise RuntimeError("synthetic constructor failure")

    monkeypatch.setattr(
        bootstrap_module, "ProductionApplicationBootstrap", BrokenBootstrap
    )
    with pytest.raises(ProductionApplicationBootstrapError) as partial:
        await build_production_application_bootstrap(
            config,
            environ={
                "JOBOPS_SYNTHETIC_SESSION_SECRET": "synthetic-session-secret"
            },
            backend_registry={"codex_cli": CodexCLIBackend},
            isolation_profile_registry=model_execution_isolation_profiles(
                isolated_subscription_cli_runner_available=True
            ),
        )
    assert partial.value.failure is (
        ProductionBootstrapFailure.BOOTSTRAP_PARTIAL_FAILURE
    )
    assert closed == 1


def test_main_server_bootstraps_before_legacy_profile_and_never_schedules() -> None:
    source = (Path(__file__).parents[1] / "main.py").read_text(
        encoding="utf-8"
    )
    server_boundary = source.index('if args.command == "server":')
    legacy_load = source.index("profile = load_profile()", server_boundary)
    assert server_boundary < legacy_load
    assert "prepare_production_server_bootstrap(" in source
    assert "setup_scheduler()" not in source
    assert "build_production_automation_composition(" in source
    assert "composition.install_dashboard(app)" in source
