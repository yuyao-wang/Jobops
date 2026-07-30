from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from starlette.requests import Request

from auth.credentials import InMemoryCredentialStore
from core.authenticated_subject import (
    AUTHENTICATED_SUBJECT_COOKIE_NAME,
    AuthenticatedSessionCredential,
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.model_provider_capabilities import (
    model_execution_isolation_profiles,
)
from core.production_application_bootstrap import (
    ProductionRepositoryBundle,
    build_production_application_bootstrap,
    production_application_config_from_mapping,
)
from core.production_automation_composition import (
    PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION,
    ProductionAutomationCompositionError,
    ProductionAutomationCompositionFailure,
    build_production_automation_composition,
)
from dashboard.server import (
    app,
    configure_production_automation_ui,
    continue_automatic_application_ui,
    lifespan,
    refresh_job_library_ui,
)
from utils.llm import CodexCLIBackend


NOW = datetime(2035, 1, 2, 3, 4, tzinfo=timezone.utc)
SUBJECT = "subject-production-composition"


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


async def _build(tmp_path: Path):
    store = InMemoryCredentialStore()
    config = production_application_config_from_mapping(
        _raw_config(tmp_path)
    )
    bootstrap = await build_production_application_bootstrap(
        config,
        credential_store=store,
        environ={
            "JOBOPS_SYNTHETIC_SESSION_SECRET": "synthetic-session-secret"
        },
        backend_registry={"codex_cli": CodexCLIBackend},
        isolation_profile_registry=model_execution_isolation_profiles(
            isolated_subscription_cli_runner_available=True
        ),
    )
    composition = build_production_automation_composition(
        bootstrap=bootstrap, clock=lambda: NOW
    )
    return bootstrap, composition, store


@pytest.mark.asyncio
async def test_complete_production_root_is_static_canonical_and_exact(
    tmp_path: Path,
) -> None:
    bootstrap, composition, _ = await _build(tmp_path)
    try:
        assert composition.composition_contract_version == (
            PRODUCTION_AUTOMATION_COMPOSITION_CONTRACT_VERSION
        )
        assert len(composition.application_preparation_recipe.stages) == 18
        assert (
            composition.preparation_agent_adapters.resume_visual_qa
            is bootstrap.preparation_stage_dependencies.agents.resume_visual_qa
        )
        assert composition.production_priority_agent.call_metadata.backend_id == (
            "codex_cli"
        )
        assert composition.production_job_search_ports.ports
        assert composition.verified_profile_provider is (
            bootstrap.repository_bundle.require(
                "verified_execution_profiles"
            )
        )
        assert composition.execution_policy_provider is (
            bootstrap.repository_bundle.require(
                "plan_execution_policy_decisions"
            )
        )
        assert composition.application_bundle_factory.__class__.__name__ == (
            "ProductionApplicationBundleFactory"
        )
        assert bootstrap.browser_runtime.context is None
    finally:
        await bootstrap.close()


@pytest.mark.asyncio
async def test_dashboard_install_is_atomic_and_lifecycle_owned(
    tmp_path: Path,
) -> None:
    bootstrap, composition, _ = await _build(tmp_path)
    events: list[str] = []

    class Resource:
        async def start(self) -> None:
            events.append("start")

        async def close(self) -> None:
            events.append("close")

    local_app = FastAPI()
    configure_production_automation_ui(
        application=local_app,
        refresh_controller=composition.refresh_job_library_controller,
        automation_controller=(
            composition.continue_automatic_application_controller
        ),
        authenticated_subject=composition.authenticated_subject_dependency,
        owned_resources=(Resource(),),
        composition_diagnostics=composition.safe_diagnostics,
    )
    try:
        assert local_app.state.job_library_refresh_controller is (
            composition.refresh_job_library_controller
        )
        assert local_app.state.automation_cycle_controller is (
            composition.continue_automatic_application_controller
        )
        async with lifespan(local_app):
            assert events == ["start"]
        assert events == ["start", "close"]
    finally:
        await bootstrap.close()


@pytest.mark.asyncio
async def test_authenticated_routes_use_injected_s3b_and_p2c10a_not_503(
    tmp_path: Path,
) -> None:
    bootstrap, composition, _ = await _build(tmp_path)
    credential = AuthenticatedSessionCredential(
        session_id="session-production-0001",
        secret="synthetic_session_secret_00000001",
    )
    context = AuthenticatedSubjectContext(
        session_id=credential.session_id,
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    bootstrap.authentication_session_provider.save_session(
        context, credential
    )
    composition.install_dashboard(app)
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/job-library/refresh",
            "query_string": b"subject_id=attacker",
            "headers": [
                (
                    b"cookie",
                    (
                        f"{AUTHENTICATED_SUBJECT_COOKIE_NAME}="
                        f"{credential.session_id}.{credential.secret}"
                    ).encode(),
                )
            ],
        }
    )
    authenticated = await composition.authenticated_subject_dependency(
        request
    )
    try:
        refresh = await refresh_job_library_ui(
            {
                "subject_id": "attacker",
                "invocation_id": "refresh-production-0001",
                "max_reprioritizations": 999,
            },
            request,
            authenticated,
        )
        automation = await continue_automatic_application_ui(
            {
                "subject_id": "attacker",
                "invocation_id": "automation-production-0001",
                "max_executions": 999,
            },
            request,
            authenticated,
        )
        assert authenticated.subject_id == SUBJECT
        assert refresh["status"] in {"NOOP", "COMPLETED", "UNCHANGED"}
        assert automation["status"] in {
            "NOOP",
            "PARTIAL_FAILURE",
            "FAILED",
            "COMPLETED",
        }
    finally:
        await bootstrap.close()


@pytest.mark.asyncio
async def test_missing_mandatory_dependency_fails_and_diagnostics_are_safe(
    tmp_path: Path,
) -> None:
    bootstrap, composition, _ = await _build(tmp_path)
    repositories = dict(bootstrap.repository_bundle.repositories)
    repositories.pop("search_profiles")
    incomplete = replace(
        bootstrap,
        repository_bundle=ProductionRepositoryBundle(repositories),
    )
    try:
        with pytest.raises(ProductionAutomationCompositionError) as error:
            build_production_automation_composition(
                bootstrap=incomplete, clock=lambda: NOW
            )
        assert error.value.failure is (
            ProductionAutomationCompositionFailure.REPOSITORY_UNAVAILABLE
        )
        diagnostics = repr(dict(composition.safe_diagnostics))
        assert str(tmp_path) not in diagnostics
        assert "synthetic-session-secret" not in diagnostics
        assert "PolicyDecision(" not in diagnostics

        source = (
            Path(__file__).parents[1]
            / "core"
            / "production_automation_composition.py"
        ).read_text(encoding="utf-8")
        assert "utils.discovery" not in source
        assert "OpenAIPriorityAgentAdapter" not in source
        assert "asyncio.run" not in source
        assert "asyncio.gather" not in source
        assert "profile.yaml" not in source
    finally:
        await bootstrap.close()
