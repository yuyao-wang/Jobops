from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

import core.production_application_bootstrap as bootstrap_module
from core.model_provider_capabilities import (
    model_execution_isolation_profiles,
)
from core.private_home import PrivateHome
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


def test_pre_v2_search_mapping_defaults_to_greenhouse_only(
    tmp_path: Path,
) -> None:
    raw = _raw_config(tmp_path)
    raw["search"]["enabled_providers"] = ["GREENHOUSE"]
    raw["search"]["boards"] = [
        {
            "canonical_company": "Example Company",
            "board_token": "example",
            "aliases": [],
        }
    ]
    for key in (
        "ashby_boards",
        "lever_sites",
        "glassdoor",
        "jobvite_feeds",
    ):
        raw["search"].pop(key)

    config = production_application_config_from_mapping(raw)

    assert config.search.enabled_providers == ("GREENHOUSE",)
    assert config.search.ashby_boards == ()
    assert config.search.lever_sites == ()
    assert config.search.glassdoor is None
    assert config.search.jobvite_feeds == ()
    assert config.search.authorized_web_search is None
    assert config.search.job_alert_inbox is None


def test_web_search_does_not_require_a_configured_company_feed(
    tmp_path: Path,
) -> None:
    raw = _raw_config(tmp_path)
    raw["search"]["authorized_web_search"] = {
        "provider_id": "BRAVE",
        "api_key_ref": {
            "source": "ENV",
            "name": "JOBOPS_SYNTHETIC_WEB_SEARCH_KEY",
        },
        "storage_rights_confirmed": True,
        "country": "CA",
        "search_language": "en",
        "lookback_days": 14,
        "max_search_requests": 20,
        "results_per_request": 20,
        "max_resolution_searches": 20,
    }

    config = production_application_config_from_mapping(raw)

    assert config.search.enabled_providers == ()
    assert config.search.boards == ()
    assert config.search.authorized_web_search is not None


def test_optional_discovery_sources_require_explicit_rights_and_keychain_scope(
    tmp_path: Path,
) -> None:
    raw = _raw_config(tmp_path)
    raw["search"]["authorized_web_search"] = {
        "provider_id": "BRAVE",
        "api_key_ref": {
            "source": "ENV",
            "name": "JOBOPS_SYNTHETIC_WEB_SEARCH_KEY",
        },
        "storage_rights_confirmed": True,
        "country": "CA",
        "search_language": "en",
        "lookback_days": 14,
        "max_search_requests": 40,
        "results_per_request": 20,
        "max_resolution_searches": 20,
    }
    raw["search"]["job_alert_inbox"] = {
        "host": "imap.example.invalid",
        "recipient": "alerts@example.invalid",
        "credential_ref": {
            "source": "KEYCHAIN",
            "service": "jobops.synthetic.alerts",
            "account": "alerts@example.invalid",
        },
        "mailbox": "JobOps Alerts",
        "port": 993,
        "allowed_sender_domains": ["linkedin.com", "indeed.com"],
        "trusted_authserv_ids": ["mx.example.invalid"],
        "max_age_hours": 24,
        "max_messages": 25,
    }

    config = production_application_config_from_mapping(raw)

    assert config.search.authorized_web_search is not None
    assert config.search.authorized_web_search.max_search_requests == 40
    assert config.search.job_alert_inbox is not None
    assert config.search.job_alert_inbox.mailbox == "JobOps Alerts"
    assert "alerts@example.invalid" not in repr(config.search.job_alert_inbox)

    raw["search"]["authorized_web_search"]["storage_rights_confirmed"] = False
    with pytest.raises(ProductionApplicationBootstrapError):
        production_application_config_from_mapping(raw)
    raw["search"]["authorized_web_search"]["storage_rights_confirmed"] = True
    raw["search"]["job_alert_inbox"]["credential_ref"] = {
        "source": "ENV",
        "name": "MAIL_PASSWORD",
    }
    with pytest.raises(ProductionApplicationBootstrapError):
        production_application_config_from_mapping(raw)

    raw = _raw_config(tmp_path)
    raw["search"]["job_alert_inbox"] = {
        "host": "imap.example.invalid",
        "recipient": "alerts@example.invalid",
        "credential_ref": {
            "source": "KEYCHAIN",
            "service": "jobops.synthetic.alerts",
            "account": "different@example.invalid",
        },
        "mailbox": "JobOps Alerts",
        "port": 993,
        "allowed_sender_domains": ["linkedin.com"],
        "trusted_authserv_ids": ["mx.example.invalid"],
        "max_age_hours": 24,
        "max_messages": 25,
    }
    with pytest.raises(ProductionApplicationBootstrapError):
        production_application_config_from_mapping(raw)


@pytest.mark.asyncio
async def test_complete_bootstrap_constructs_only_typed_dependencies(
    tmp_path: Path,
) -> None:
    config = production_application_config_from_mapping(
        _raw_config(tmp_path)
    )
    home = PrivateHome(config.private_home.root)
    home.ensure()
    resume_content = b"%PDF-1.7\nsynthetic bootstrap resume\n%%EOF\n"
    resume_path = home.paths.master_documents / "synthetic.pdf"
    resume_path.write_bytes(resume_content)
    home.write_bytes(
        home.paths.profile_facts,
        (
            json.dumps(
                {
                    "subject_id": config.authentication.local_subject_id,
                    "normalized": {
                        "resume_variants": [
                            {
                                "artifact_id": hashlib.sha256(
                                    resume_content
                                ).hexdigest(),
                                "file_path": str(resume_path),
                                "role_family": "Synthetic Bootstrap",
                                "use_when": (
                                    "Use for synthetic bootstrap postings."
                                ),
                            }
                        ]
                    },
                },
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
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
        assert built.safe_diagnostics[
            "legacy_resume_candidate_migration"
        ]["created_count"] == 1
        candidates = built.repository_bundle.require(
            "resume_candidates"
        ).list_selectable(config.authentication.local_subject_id)
        assert len(candidates.candidates) == 1
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


@pytest.mark.asyncio
async def test_ai_runtime_failure_retains_safe_typed_status(
    tmp_path: Path,
) -> None:
    config = production_application_config_from_mapping(
        _raw_config(tmp_path)
    )
    with pytest.raises(ProductionApplicationBootstrapError) as unavailable:
        await build_production_application_bootstrap(
            config,
            environ={
                "JOBOPS_SYNTHETIC_SESSION_SECRET": "synthetic-session-secret"
            },
            backend_registry={"codex_cli": CodexCLIBackend},
            isolation_profile_registry=model_execution_isolation_profiles(
                isolated_subscription_cli_runner_available=False
            ),
        )
    assert unavailable.value.failure is (
        ProductionBootstrapFailure.AI_CONFIGURATION_INVALID
    )
    assert unavailable.value.section == "ISOLATION_UNAVAILABLE"
    assert str(unavailable.value) == (
        "AI_CONFIGURATION_INVALID:ISOLATION_UNAVAILABLE"
    )


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
    assert "f\"({exc}); provide --config or \"" in source
