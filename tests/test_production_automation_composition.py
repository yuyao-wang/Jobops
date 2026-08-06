from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from starlette.requests import Request

from auth.credentials import InMemoryCredentialStore
from auth.imap_provider import IMAPMailboxProvider
from auth.mailbox import (
    MailAuthenticationEvidence,
    MailAuthenticationResult,
    MailboxMessage,
)
from core.authenticated_subject import (
    AUTHENTICATED_SUBJECT_COOKIE_NAME,
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
import core.production_automation_composition as composition_module
from core.model_provider_capabilities import (
    model_execution_isolation_profiles,
)
from core.job_leads import JobLeadSource
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
from dashboard.automation_cycle import (
    ContinueAutomationUICommand,
    ContinueAutomationUIStatus,
    StopAutomationUICommand,
)
from dashboard.server import (
    app,
    configure_production_automation_ui,
    continue_automatic_application_ui,
    dashboard_readiness,
    health,
    issue_local_dashboard_session,
    lifespan,
    refresh_job_library_ui,
    run_server,
)
from dashboard.job_library_refresh import LeadRefreshStatus
from source_connectors.authorized_web_search import (
    AuthorizedWebSearchHit,
    AuthorizedWebSearchResult,
    BraveAuthorizedWebSearch,
)
from source_connectors.contract import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    ReadJobResult,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
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
    raw["authentication"]["local_subject_id"] = SUBJECT
    return raw


async def _build(
    tmp_path: Path,
    *,
    raw: dict | None = None,
    environ: dict[str, str] | None = None,
    store: InMemoryCredentialStore | None = None,
    preflight_refresh_timeout_seconds: float | None = None,
):
    store = store or InMemoryCredentialStore()
    config = production_application_config_from_mapping(
        raw or _raw_config(tmp_path)
    )
    active_environ = {
        "JOBOPS_SYNTHETIC_SESSION_SECRET": "synthetic-session-secret"
    }
    if environ:
        active_environ.update(environ)
    bootstrap = await build_production_application_bootstrap(
        config,
        credential_store=store,
        environ=active_environ,
        backend_registry={"codex_cli": CodexCLIBackend},
        isolation_profile_registry=model_execution_isolation_profiles(
            isolated_subscription_cli_runner_available=True
        ),
    )
    composition_kwargs = {
        "bootstrap": bootstrap,
        "clock": lambda: NOW,
    }
    if preflight_refresh_timeout_seconds is not None:
        composition_kwargs["preflight_refresh_timeout_seconds"] = (
            preflight_refresh_timeout_seconds
        )
    composition = build_production_automation_composition(
        **composition_kwargs
    )
    return bootstrap, composition, store


def _context() -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="session_reference_0123456789abcdef",
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


async def _wait_automation_terminal(controller):
    for _ in range(200):
        result = await controller.status(context=_context())
        if result.status not in {
            ContinueAutomationUIStatus.RUNNING,
            ContinueAutomationUIStatus.STOPPING,
        }:
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("production automation did not reach a terminal state")


def test_automation_work_snapshot_excludes_terminal_and_attention_jobs() -> None:
    runnable = tuple(
        SimpleNamespace(job=SimpleNamespace(job_id=job_id))
        for job_id in (
            "job-submitted",
            "job-uncertain",
            "job-review",
            "job-new",
        )
    )
    execution = (
        SimpleNamespace(
            job_id="job-submitted",
            execution_status=(
                composition_module.CurrentApplicationExecutionStatus.SUBMITTED
            ),
        ),
        SimpleNamespace(
            job_id="job-uncertain",
            execution_status=(
                composition_module.CurrentApplicationExecutionStatus
                .SUBMISSION_UNCERTAIN
            ),
        ),
        SimpleNamespace(
            job_id="job-review",
            execution_status=(
                composition_module.CurrentApplicationExecutionStatus.DEFERRED
            ),
        ),
    )
    attention = (
        SimpleNamespace(job_id="job-review", blocking=True),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=execution,
        attention_items=attention,
    ) == ("job-new",)


def test_resumable_plan_projection_keeps_exact_p2_progress_after_refresh() -> None:
    deferred = SimpleNamespace(
        job_id="job-resume",
        application_plan_id="application-plan-existing",
        priority=SimpleNamespace(value="P2"),
        plan_created_at=NOW,
        execution_status=(
            composition_module.CurrentApplicationExecutionStatus.DEFERRED
        ),
        deferred_stage=SimpleNamespace(value="NON_SUBMIT_EXECUTION"),
        deferred_reason="DEFERRED_GATE_A_REQUIRED",
    )
    uncertain = SimpleNamespace(
        job_id="job-uncertain",
        application_plan_id="application-plan-uncertain",
        priority=SimpleNamespace(value="P2"),
        plan_created_at=NOW,
        execution_status=(
            composition_module.CurrentApplicationExecutionStatus.SUBMISSION_UNCERTAIN
        ),
        deferred_stage=None,
        deferred_reason=None,
    )

    assert composition_module._project_resumable_plan_ids_by_job(
        (deferred, uncertain)
    ) == {"job-resume": "application-plan-existing"}


def test_automation_work_prioritizes_resumable_p2_over_new_jobs() -> None:
    p2 = SimpleNamespace(value="P2")
    runnable = tuple(
        SimpleNamespace(
            job=SimpleNamespace(job_id=job_id),
            priority_decision=SimpleNamespace(priority_level=p2),
        )
        for job_id in ("job-new", "job-resume")
    )
    execution = (
        SimpleNamespace(
            job_id="job-resume",
            application_plan_id="application-plan-existing",
            priority=p2,
            plan_created_at=NOW,
            execution_status=(
                composition_module.CurrentApplicationExecutionStatus.DEFERRED
            ),
            deferred_stage=SimpleNamespace(value="NON_SUBMIT_EXECUTION"),
            deferred_reason="DEFERRED_GATE_A_REQUIRED",
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-resume",
            blocking=True,
            priority=p2,
            source_stage=SimpleNamespace(value="COVER_LETTER_FACT_QA"),
            source_reason_code="FACT_QA_BLOCKED",
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=execution,
        attention_items=attention,
    ) == ("job-resume", "job-new")


def test_automation_work_does_not_retry_unresolved_runtime_input() -> None:
    p2 = SimpleNamespace(value="P2")
    runnable = tuple(
        SimpleNamespace(
            job=SimpleNamespace(job_id=job_id),
            priority_decision=SimpleNamespace(priority_level=p2),
        )
        for job_id in ("job-runtime-input", "job-new")
    )
    execution = (
        SimpleNamespace(
            job_id="job-runtime-input",
            application_plan_id="application-plan-blocked",
            priority=p2,
            plan_created_at=NOW,
            execution_status=(
                composition_module.CurrentApplicationExecutionStatus.DEFERRED
            ),
            deferred_stage=SimpleNamespace(value="NON_SUBMIT_EXECUTION"),
            deferred_reason="DEFERRED_RUNTIME_INPUT_REQUIRED",
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-runtime-input",
            blocking=True,
            priority=p2,
            source_stage="NON_SUBMIT_EXECUTION",
            source_reason_code="RUNTIME_INPUT_REQUIRED",
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=execution,
        attention_items=attention,
    ) == ("job-new",)


def test_changed_trusted_facts_retry_exact_runtime_input_plan_once() -> None:
    p2 = SimpleNamespace(value="P2")
    runtime_item = SimpleNamespace(
        job_id="job-runtime-input",
        application_plan_id="application-plan-blocked",
        assembly_record_id="assembly-current",
        priority=p2,
        plan_created_at=NOW,
        execution_status=(
            composition_module.CurrentApplicationExecutionStatus.DEFERRED
        ),
        deferred_stage=SimpleNamespace(value="NON_SUBMIT_EXECUTION"),
        deferred_reason="DEFERRED_RUNTIME_INPUT_REQUIRED",
    )
    assembly = SimpleNamespace(
        subject_id=SUBJECT,
        application_plan_id="application-plan-blocked",
        job_id="job-runtime-input",
        answer_set_id="answers-old",
        answer_set_content_hash="a" * 64,
    )
    answer_set = SimpleNamespace(
        subject_id=SUBJECT,
        application_plan_id="application-plan-blocked",
        job_id="job-runtime-input",
        answer_set_id="answers-old",
        answer_set_content_hash="a" * 64,
        fact_snapshot_hash="b" * 64,
    )
    assembly_repository = SimpleNamespace(
        get=lambda **_kwargs: SimpleNamespace(
            status=(
                composition_module.ApplicationBundleAssemblyReadStatus.FOUND
            ),
            record=assembly,
        )
    )
    answer_repository = SimpleNamespace(
        get=lambda **_kwargs: SimpleNamespace(
            status=(
                composition_module.PreparedApplicationAnswerSetReadStatus.FOUND
            ),
            answer_set=answer_set,
        )
    )
    fact_provider = SimpleNamespace(
        get_current=lambda _subject: SimpleNamespace(
            snapshot_content_hash="c" * 64
        )
    )

    stale = (
        composition_module
        ._project_fact_stale_runtime_input_plan_ids_by_job(
            (runtime_item,),
            subject_id=SUBJECT,
            assembly_repository=assembly_repository,
            answer_set_repository=answer_repository,
            fact_provider=fact_provider,
        )
    )

    assert stale == {
        "job-runtime-input": "application-plan-blocked"
    }
    assert composition_module._project_automation_work_ids(
        runnable_items=(),
        execution_items=(runtime_item,),
        attention_items=(),
        fact_stale_runtime_input_plan_ids_by_job=stale,
    ) == ("job-runtime-input",)

    answer_set.fact_snapshot_hash = "c" * 64
    assert (
        composition_module
        ._project_fact_stale_runtime_input_plan_ids_by_job(
            (runtime_item,),
            subject_id=SUBJECT,
            assembly_repository=assembly_repository,
            answer_set_repository=answer_repository,
            fact_provider=fact_provider,
        )
        == {}
    )


def test_automation_work_snapshot_ignores_stale_priority_attention() -> None:
    p2 = SimpleNamespace(value="P2")
    p1 = SimpleNamespace(value="P1")
    runnable = (
        SimpleNamespace(
            job=SimpleNamespace(job_id="job-reprioritized"),
            priority_decision=SimpleNamespace(priority_level=p2),
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-reprioritized",
            blocking=True,
            priority=p1,
            source_stage=SimpleNamespace(value="RESUME_TAILORING"),
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=(),
        attention_items=attention,
    ) == ("job-reprioritized",)


def test_automation_work_snapshot_retries_replaced_p2_resume_stage() -> None:
    p2 = SimpleNamespace(value="P2")
    retryable_stage = next(
        iter(
            composition_module.P2_APPROVED_RESUME_REUSE_SKIPPED_STAGES
        )
    )
    runnable = (
        SimpleNamespace(
            job=SimpleNamespace(job_id="job-p2-migration"),
            priority_decision=SimpleNamespace(priority_level=p2),
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-p2-migration",
            blocking=True,
            priority=p2,
            source_stage=retryable_stage,
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=(),
        attention_items=attention,
    ) == ("job-p2-migration",)


def test_automation_work_snapshot_retries_transient_p2_agent_failure() -> None:
    p2 = SimpleNamespace(value="P2")
    runnable = (
        SimpleNamespace(
            job=SimpleNamespace(job_id="job-p2-agent-retry"),
            priority_decision=SimpleNamespace(priority_level=p2),
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-p2-agent-retry",
            blocking=True,
            priority=p2,
            source_stage="BASE_RESUME_SELECTION",
            source_reason_code="AGENT_UNAVAILABLE",
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=(),
        attention_items=attention,
    ) == ("job-p2-agent-retry",)


def test_automation_work_snapshot_retries_p2_unsafe_resume_choice() -> None:
    p2 = SimpleNamespace(value="P2")
    runnable = (
        SimpleNamespace(
            job=SimpleNamespace(job_id="job-p2-resume-choice"),
            priority_decision=SimpleNamespace(priority_level=p2),
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-p2-resume-choice",
            blocking=True,
            priority=p2,
            source_stage="BASE_RESUME_SELECTION",
            source_reason_code="AGENT_SELECTION_UNSAFE",
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=(),
        attention_items=attention,
    ) == ("job-p2-resume-choice",)


def test_automation_work_snapshot_retries_revalidated_p2_cover_draft() -> None:
    p2 = SimpleNamespace(value="P2")
    runnable = (
        SimpleNamespace(
            job=SimpleNamespace(job_id="job-p2-cover-revalidation"),
            priority_decision=SimpleNamespace(priority_level=p2),
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-p2-cover-revalidation",
            blocking=True,
            priority=p2,
            source_stage="COVER_LETTER_DRAFT",
            source_reason_code="AGENT_OUTPUT_UNSAFE",
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=(),
        attention_items=attention,
    ) == ("job-p2-cover-revalidation",)


def test_automation_work_snapshot_retries_pre_runtime_attestation() -> None:
    p2 = SimpleNamespace(value="P2")
    runnable = (
        SimpleNamespace(
            job=SimpleNamespace(job_id="job-p2-attestation-migration"),
            priority_decision=SimpleNamespace(priority_level=p2),
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-p2-attestation-migration",
            blocking=True,
            priority=p2,
            source_stage="APPLICATION_ANSWERS",
            source_reason_code="REQUIRES_ATTESTATION",
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=runnable,
        execution_items=(),
        attention_items=attention,
    ) == ("job-p2-attestation-migration",)


def test_attention_only_p2_migration_recovers_exact_existing_plan() -> None:
    p2 = SimpleNamespace(value="P2")
    attention = (
        SimpleNamespace(
            job_id="job-p2-attention-only",
            application_plan_id="application-plan-existing",
            blocking=True,
            priority=p2,
            source_stage="APPLICATION_ANSWERS",
            source_reason_code="REQUIRES_ATTESTATION",
            source_event_time=NOW,
        ),
    )

    assert composition_module._project_resumable_attention_plan_ids_by_job(
        attention
    ) == {"job-p2-attention-only": "application-plan-existing"}
    assert composition_module._project_automation_work_ids(
        runnable_items=(),
        execution_items=(),
        attention_items=attention,
    ) == ("job-p2-attention-only",)


def test_attention_only_p2_migration_requires_every_blocker_retryable() -> None:
    p2 = SimpleNamespace(value="P2")
    attention = (
        SimpleNamespace(
            job_id="job-still-blocked",
            application_plan_id="application-plan-existing",
            blocking=True,
            priority=p2,
            source_stage="APPLICATION_ANSWERS",
            source_reason_code="REQUIRES_ATTESTATION",
            source_event_time=None,
        ),
        SimpleNamespace(
            job_id="job-still-blocked",
            application_plan_id="application-plan-existing",
            blocking=True,
            priority=p2,
            source_stage="APPLICATION_ANSWERS",
            source_reason_code="MISSING_VERIFIED_ANSWER",
            source_event_time=NOW,
        ),
    )

    assert composition_module._project_resumable_attention_plan_ids_by_job(
        attention
    ) == {}
    assert composition_module._project_automation_work_ids(
        runnable_items=(),
        execution_items=(),
        attention_items=attention,
    ) == ()


def test_attention_migration_does_not_override_runtime_input_blocker() -> None:
    p2 = SimpleNamespace(value="P2")
    execution = (
        SimpleNamespace(
            job_id="job-runtime-input",
            application_plan_id="application-plan-existing",
            priority=p2,
            plan_created_at=NOW,
            execution_status=(
                composition_module.CurrentApplicationExecutionStatus.DEFERRED
            ),
            deferred_stage=SimpleNamespace(value="NON_SUBMIT_EXECUTION"),
            deferred_reason="DEFERRED_RUNTIME_INPUT_REQUIRED",
        ),
    )
    attention = (
        SimpleNamespace(
            job_id="job-runtime-input",
            application_plan_id="application-plan-existing",
            blocking=True,
            priority=p2,
            source_stage="APPLICATION_ANSWERS",
            source_reason_code="REQUIRES_ATTESTATION",
            source_event_time=NOW,
        ),
    )

    assert composition_module._project_automation_work_ids(
        runnable_items=(),
        execution_items=execution,
        attention_items=attention,
    ) == ()


@pytest.mark.asyncio
async def test_production_lead_refresh_is_noop_without_enabled_channels(
    tmp_path: Path,
) -> None:
    bootstrap, composition, _ = await _build(tmp_path)
    try:
        lead_refresh = composition.refresh_job_library_controller._lead_refresh
        assert lead_refresh is not None
        result = await lead_refresh(
            subject_id=SUBJECT,
            invocation_id="synthetic-noop-lead-refresh",
            now=NOW,
        )
        assert result.status is LeadRefreshStatus.NOOP
        assert result.requests == 0
        assert result.resolved == 0
    finally:
        await bootstrap.close()


@pytest.mark.asyncio
async def test_authorized_web_search_wires_without_exposing_credential(
    tmp_path: Path,
) -> None:
    raw = _raw_config(tmp_path)
    raw["search"]["authorized_web_search"] = {
        "provider_id": "BRAVE",
        "api_key_ref": {
            "source": "ENV",
            "name": "JOBOPS_SYNTHETIC_BRAVE_KEY",
        },
        "storage_rights_confirmed": True,
        "country": "CA",
        "search_language": "en",
        "lookback_days": 14,
        "max_search_requests": 20,
        "results_per_request": 20,
        "max_resolution_searches": 20,
    }
    token = "synthetic-brave-key-not-a-real-secret"
    bootstrap, composition, _ = await _build(
        tmp_path,
        raw=raw,
        environ={"JOBOPS_SYNTHETIC_BRAVE_KEY": token},
    )
    try:
        lead_refresh = composition.refresh_job_library_controller._lead_refresh
        assert lead_refresh is not None
        result = await lead_refresh(
            subject_id=SUBJECT,
            invocation_id="synthetic-web-lead-refresh",
            now=NOW,
        )
        assert result.status is LeadRefreshStatus.FAILED
        assert result.requests == 0
        assert result.source_results[0].source is (
            JobLeadSource.AUTHORIZED_WEB_SEARCH
        )
        assert token not in repr(bootstrap.job_search_factory_inputs)
        assert token not in repr(composition.safe_diagnostics)
    finally:
        await bootstrap.close()


@pytest.mark.asyncio
async def test_authorized_web_search_streams_per_platform_lead_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_config(tmp_path)
    raw["search"]["authorized_web_search"] = {
        "provider_id": "BRAVE",
        "api_key_ref": {
            "source": "ENV",
            "name": "JOBOPS_SYNTHETIC_BRAVE_KEY",
        },
        "storage_rights_confirmed": True,
        "country": "CA",
        "search_language": "en",
        "lookback_days": 14,
        "max_search_requests": 2,
        "results_per_request": 1,
        "max_resolution_searches": 0,
    }
    calls: list[str] = []

    async def fake_search(self, request):
        calls.append(request.query_id)
        if "site:linkedin.com/jobs/view" in request.query:
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit(
                        "Machine Learning Engineer - Synthetic Labs | LinkedIn",
                        "https://www.linkedin.com/jobs/view/123456789",
                    ),
                ),
            )
        if "site:indeed.com/viewjob" in request.query:
            return AuthorizedWebSearchResult.succeeded(
                request.query_id,
                (
                    AuthorizedWebSearchHit(
                        "Machine Learning Engineer - Synthetic Labs | Indeed",
                        "https://ca.indeed.com/viewjob?jk=synthetic123",
                    ),
                ),
            )
        raise AssertionError("unexpected synthetic search family")

    monkeypatch.setattr(BraveAuthorizedWebSearch, "search", fake_search)
    bootstrap, composition, _ = await _build(
        tmp_path,
        raw=raw,
        environ={
            "JOBOPS_SYNTHETIC_BRAVE_KEY": "synthetic-progress-key"
        },
    )
    try:
        from tests.test_job_lead_discovery import _policy

        policy_repository = bootstrap.repository_bundle.require(
            "prioritization_policies"
        )
        monkeypatch.setattr(
            policy_repository,
            "get_active_policy",
            lambda subject_id: replace(
                _policy(),
                subject_id=subject_id,
                policy_id="policy-synthetic-production-progress",
            ),
        )
        progress = []
        lead_refresh = composition.refresh_job_library_controller._lead_refresh
        assert lead_refresh is not None
        result = await lead_refresh(
            subject_id=SUBJECT,
            invocation_id="synthetic-web-progress",
            now=NOW,
            progress_observer=progress.append,
        )

        assert len(calls) == 2
        assert all("lead-canonical-" not in query_id for query_id in calls)
        assert result.status is LeadRefreshStatus.PARTIAL_FAILURE
        assert result.truncated is True
        # Only the two authorized initial searches run. No synthetic company
        # feed is required, and canonical web search remains disabled.
        assert result.requests == 2
        assert result.discovered == 2
        assert result.unique == 2
        assert result.needs_user == 2
        assert result.public_reads == 0
        platform_results = {
            item.family: item
            for item in result.source_results
            if item.source is JobLeadSource.AUTHORIZED_WEB_SEARCH
        }
        assert platform_results["LINKEDIN"].search_hits == 1
        assert platform_results["LINKEDIN"].discovered == 1
        assert platform_results["INDEED"].search_hits == 1
        assert platform_results["INDEED"].discovered == 1
        assert any(
            any(item.family == "LINKEDIN" for item in item.source_results)
            for item in progress
        )
        assert any(
            any(item.family == "INDEED" for item in item.source_results)
            for item in progress
        )
    finally:
        await bootstrap.close()


@pytest.mark.asyncio
async def test_web_only_refresh_resolves_official_job_into_dashboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_config(tmp_path)
    raw["search"]["authorized_web_search"] = {
        "provider_id": "BRAVE",
        "api_key_ref": {
            "source": "ENV",
            "name": "JOBOPS_SYNTHETIC_BRAVE_KEY",
        },
        "storage_rights_confirmed": True,
        "country": "CA",
        "search_language": "en",
        "lookback_days": 14,
        "max_search_requests": 4,
        "results_per_request": 1,
        "max_resolution_searches": 0,
    }
    official_url = (
        "https://job-boards.greenhouse.io/synthetic/jobs/123456"
    )
    observation = SourceJobObservation(
        source_platform=SourcePlatform.GREENHOUSE,
        source_job_id="123456",
        source_url=official_url,
        application_url=official_url,
        company="Synthetic Labs",
        title="Machine Learning Engineer",
        description="Build deterministic synthetic systems.",
        location="Calgary",
        work_mode=WorkMode.HYBRID,
        posted_at=None,
        ats_type=AtsType.GREENHOUSE,
        observed_at=NOW.isoformat(),
        provenance=(
            FieldProvenance(
                "description",
                ProvenanceSource.SOURCE_API,
                "content",
            ),
        ),
    )
    search_calls = []
    read_calls = []

    async def fake_search(self, request):
        search_calls.append(request)
        hits = (
            (
                AuthorizedWebSearchHit(
                    "Machine Learning Engineer - Synthetic Labs",
                    official_url,
                ),
            )
            if "greenhouse.io" in request.query
            else ()
        )
        return AuthorizedWebSearchResult.succeeded(request.query_id, hits)

    async def fake_reader(request):
        read_calls.append(request.url)
        return ReadJobResult.succeeded(observation)

    monkeypatch.setattr(BraveAuthorizedWebSearch, "search", fake_search)
    monkeypatch.setattr(composition_module, "read_public_job", fake_reader)
    bootstrap, composition, _ = await _build(
        tmp_path,
        raw=raw,
        environ={"JOBOPS_SYNTHETIC_BRAVE_KEY": "synthetic-web-only-key"},
    )
    try:
        from tests.test_job_lead_discovery import _policy

        policy_repository = bootstrap.repository_bundle.require(
            "prioritization_policies"
        )
        active_policy = replace(
            _policy(),
            subject_id=SUBJECT,
            policy_id="policy-synthetic-web-only",
        )
        monkeypatch.setattr(
            policy_repository,
            "get_active_policy",
            lambda subject_id: active_policy,
        )
        lead_refresh = composition.refresh_job_library_controller._lead_refresh
        assert lead_refresh is not None

        result = await lead_refresh(
            subject_id=SUBJECT,
            invocation_id="synthetic-web-only-refresh",
            now=NOW,
        )
        active_policy = None
        jobs = await composition.dashboard_jobs_controller.load(
            context=AuthenticatedSubjectContext(
                session_id="session_reference_0123456789abcdef",
                subject_id=SUBJECT,
                authentication_method=(
                    AuthenticationMethod.LOCAL_KEYCHAIN_SESSION
                ),
                issued_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=10),
            )
        )

        assert len(search_calls) == 4
        assert read_calls == [official_url]
        assert result.resolved == 1
        assert result.public_reads == 1
        assert jobs.counts["total"] == 1
        assert jobs.ordered_items[0].title == "Machine Learning Engineer"
        assert jobs.ordered_items[0].application_status.value == "NOT_EVALUATED"
    finally:
        await bootstrap.close()


@pytest.mark.asyncio
async def test_enabled_job_alert_inbox_persists_unverified_platform_lead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipient = "synthetic-alerts@example.test"
    raw = _raw_config(tmp_path)
    raw["search"]["job_alert_inbox"] = {
        "host": "imap.example.test",
        "recipient": recipient,
        "credential_ref": {
            "source": "CREDENTIAL_STORE",
            "service": "jobops.synthetic.alerts",
            "account": recipient,
        },
        "mailbox": "JobOps-Alerts",
        "port": 993,
        "allowed_sender_domains": ["linkedin.com", "indeed.com"],
        "trusted_authserv_ids": ["mx.example.test"],
        "max_age_hours": 24,
        "max_messages": 25,
    }

    async def fake_search_recent(
        self,
        *,
        recipient: str,
        since: datetime,
        limit: int,
    ):
        assert recipient == "synthetic-alerts@example.test"
        assert since <= NOW
        assert limit == 25
        return (
            MailboxMessage(
                message_id="synthetic-linkedin-alert-001",
                received_at=NOW,
                sender="alerts-noreply@linkedin.com",
                recipients=(recipient,),
                subject="Synthetic jobs you may be interested in",
                html=(
                    '<a href="https://www.linkedin.com/jobs/view/123456">'
                    "Machine Learning Engineer</a>"
                ),
                authentication=MailAuthenticationEvidence(
                    spf=MailAuthenticationResult.PASS,
                    dkim=MailAuthenticationResult.PASS,
                    dmarc=MailAuthenticationResult.PASS,
                ),
            ),
        )

    monkeypatch.setattr(
        IMAPMailboxProvider,
        "search_recent",
        fake_search_recent,
    )
    store = InMemoryCredentialStore()
    store.set(
        "jobops.synthetic.alerts",
        recipient,
        "synthetic-mailbox-secret",
    )
    bootstrap, composition, _ = await _build(
        tmp_path,
        raw=raw,
        store=store,
    )
    try:
        lead_refresh = composition.refresh_job_library_controller._lead_refresh
        assert lead_refresh is not None
        result = await lead_refresh(
            subject_id=SUBJECT,
            invocation_id="synthetic-alert-lead-refresh",
            now=NOW,
        )
        assert result.status is LeadRefreshStatus.COMPLETED
        assert result.requests == 1
        assert result.completed == 1
        assert result.discovered == 1
        assert result.unique == 1
        assert result.resolved == 0
        assert result.needs_user == 1
        source = next(
            item
            for item in result.source_results
            if item.source is JobLeadSource.LINKEDIN_ALERT_EMAIL
        )
        assert source.unique == 1
        assert source.needs_user == 0
        resolution = next(
            item
            for item in result.source_results
            if item.source is JobLeadSource.CANONICAL_RESOLUTION
            and item.family == "LINKEDIN"
        )
        assert resolution.needs_user == 1
        assert sum(item.requests for item in result.source_results) == (
            result.requests
        )
        assert sum(item.completed for item in result.source_results) == (
            result.completed
        )
        assert sum(item.public_reads for item in result.source_results) == (
            result.public_reads
        )
        assert sum(item.needs_user for item in result.source_results) == (
            result.needs_user
        )
    finally:
        await bootstrap.close()


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
        assert dict(composition.production_job_search_ports.ports) == {}
        assert all(
            capability.status.value == "UNSUPPORTED"
            for capability in (
                composition.production_job_search_ports.capabilities
            )
        )
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
async def test_production_preflight_deadline_fails_closed_and_releases_run(
    tmp_path: Path,
) -> None:
    bootstrap, composition, _ = await _build(
        tmp_path,
        preflight_refresh_timeout_seconds=0.05,
    )
    refresh_controller = composition.refresh_job_library_controller
    automation_controller = (
        composition.continue_automatic_application_controller
    )
    original_manual_refresh = refresh_controller._manual_refresh
    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()

    async def blocked_refresh(command, *, progress_observer=None):
        refresh_entered.set()
        await release_refresh.wait()
        return await original_manual_refresh(
            command,
            progress_observer=progress_observer,
        )

    refresh_controller._manual_refresh = blocked_refresh
    try:
        await automation_controller.start(
            context=_context(),
            command=ContinueAutomationUICommand(
                "automation-production-preflight-deadline"
            ),
        )
        await asyncio.wait_for(refresh_entered.wait(), timeout=2)

        terminal = await _wait_automation_terminal(automation_controller)

        assert terminal.status is ContinueAutomationUIStatus.FAILED
        assert terminal.phase == "FAILED"
        assert terminal.cycles_completed == 0
        assert "preflight deadline" in terminal.message
        assert (
            await refresh_controller.status(context=_context())
        ).status.value == "RUNNING"

        restarted = await automation_controller.start(
            context=_context(),
            command=ContinueAutomationUICommand(
                "automation-production-after-preflight-deadline"
            ),
        )
        assert restarted.status is ContinueAutomationUIStatus.RUNNING
        assert restarted.invocation_id == (
            "automation-production-after-preflight-deadline"
        )
        await automation_controller.stop(
            context=_context(),
            command=StopAutomationUICommand(restarted.invocation_id),
        )
        stopped = await _wait_automation_terminal(automation_controller)
        assert stopped.status is ContinueAutomationUIStatus.STOPPED
    finally:
        release_refresh.set()
        for _ in range(200):
            refresh_result = await refresh_controller.status(
                context=_context()
            )
            if refresh_result.status.value != "RUNNING":
                break
            await asyncio.sleep(0.01)
        await bootstrap.close()


@pytest.mark.asyncio
async def test_stop_during_production_refresh_preflight_does_not_wait_for_refresh(
    tmp_path: Path,
) -> None:
    bootstrap, composition, _ = await _build(tmp_path)
    refresh_controller = composition.refresh_job_library_controller
    automation_controller = (
        composition.continue_automatic_application_controller
    )
    original_manual_refresh = refresh_controller._manual_refresh
    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()

    async def blocked_refresh(command, *, progress_observer=None):
        refresh_entered.set()
        await release_refresh.wait()
        return await original_manual_refresh(
            command,
            progress_observer=progress_observer,
        )

    refresh_controller._manual_refresh = blocked_refresh
    try:
        await automation_controller.start(
            context=_context(),
            command=ContinueAutomationUICommand(
                "automation-production-stop-preflight"
            ),
        )
        await asyncio.wait_for(refresh_entered.wait(), timeout=2)
        running = await automation_controller.status(context=_context())

        assert running.status is ContinueAutomationUIStatus.RUNNING
        assert running.phase == "PREFLIGHT"
        assert "Preflight refresh has been running" in running.message

        stopping = await automation_controller.stop(
            context=_context(),
            command=StopAutomationUICommand(running.invocation_id),
        )
        terminal = await _wait_automation_terminal(automation_controller)

        assert stopping.status is ContinueAutomationUIStatus.STOPPING
        assert terminal.status is ContinueAutomationUIStatus.STOPPED
        assert terminal.cycles_completed == terminal.total_jobs == 0
        assert (
            await refresh_controller.status(context=_context())
        ).status.value == "RUNNING"
    finally:
        release_refresh.set()
        for _ in range(200):
            refresh_result = await refresh_controller.status(
                context=_context()
            )
            if refresh_result.status.value != "RUNNING":
                break
            await asyncio.sleep(0.01)
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
        search_profile_controller=composition.search_profile_controller,
        assisted_job_import_controller=(
            composition.assisted_job_import_controller
        ),
        conversational_job_finder_controller=(
            composition.conversational_job_finder_controller
        ),
        prioritization_policy_controller=(
            composition.prioritization_policy_controller
        ),
        automation_controller=(
            composition.continue_automatic_application_controller
        ),
        local_session_controller=composition.local_session_controller,
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
        assert local_app.state.search_profile_controller is (
            composition.search_profile_controller
        )
        assert local_app.state.assisted_job_import_controller is (
            composition.assisted_job_import_controller
        )
        assert local_app.state.conversational_job_finder_controller is (
            composition.conversational_job_finder_controller
        )
        assert local_app.state.local_session_controller is (
            composition.local_session_controller
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
    composition.install_dashboard(app)
    issue_request = Request(
        {
            "type": "http",
            "app": app,
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "server": ("127.0.0.1", 8080),
            "client": ("127.0.0.1", 12345),
            "path": "/api/auth/local-session",
            "raw_path": b"/api/auth/local-session",
            "query_string": b"subject_id=attacker",
            "headers": [
                (b"host", b"127.0.0.1:8080"),
                (b"origin", b"http://127.0.0.1:8080"),
                (b"sec-fetch-site", b"same-origin"),
                (b"x-subject-id", b"attacker"),
            ],
        }
    )
    issue_response = await issue_local_dashboard_session(issue_request)
    cookie = SimpleCookie()
    cookie.load(issue_response.headers["set-cookie"])
    cookie_value = cookie[AUTHENTICATED_SUBJECT_COOKIE_NAME].value
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
                        f"{cookie_value}"
                    ).encode(),
                )
            ],
        }
    )
    authenticated = await composition.authenticated_subject_dependency(
        request
    )
    try:
        assert issue_response.status_code == 200
        assert "HttpOnly" in issue_response.headers["set-cookie"]
        assert "SameSite=strict" in issue_response.headers["set-cookie"]
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
        assert refresh["status"] == "RUNNING"
        for _ in range(200):
            refresh_result = await (
                composition.refresh_job_library_controller.status(
                    context=authenticated
                )
            )
            if refresh_result.status.value != "RUNNING":
                break
            await asyncio.sleep(0.01)
        assert refresh_result.status.value in {
            "NOOP",
            "COMPLETED",
            "PARTIAL_FAILURE",
        }
        assert automation["status"] == "RUNNING"
        for _ in range(100):
            automation_result = await (
                composition.continue_automatic_application_controller.status(
                    context=authenticated
                )
            )
            if automation_result.status.value not in {"RUNNING", "STOPPING"}:
                break
            await asyncio.sleep(0.01)
        assert automation_result.status.value in {
            "NOOP",
            "PARTIAL_FAILURE",
            "FAILED",
            "COMPLETED",
        }
    finally:
        await bootstrap.close()


@pytest.mark.asyncio
async def test_health_and_server_start_share_the_complete_readiness_predicate(
    tmp_path: Path, monkeypatch
) -> None:
    bootstrap, composition, _ = await _build(tmp_path)
    composition.install_dashboard(app)
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "uvicorn.run",
        lambda _app, *, host, port, log_level: calls.append((host, port)),
    )
    required = (
        "job_library_refresh_controller",
        "automation_cycle_controller",
        "local_session_controller",
        "authenticated_subject_dependency",
        "human_attention_inbox_controller",
        "dashboard_profile_controller",
        "dashboard_jobs_controller",
        "dashboard_applications_controller",
        "application_review_submission_controller",
        "dashboard_overview_controller",
    )
    try:
        assert dashboard_readiness(app)
        response = await health()
        assert response.status_code == 200
        run_server(host="127.0.0.1", port=8123)
        assert calls == [("127.0.0.1", 8123)]

        for name in required:
            value = getattr(app.state, name)
            setattr(app.state, name, None)
            try:
                response = await health()
                assert response.status_code == 503
                with pytest.raises(RuntimeError, match="readiness contract"):
                    run_server(host="127.0.0.1", port=8123)
            finally:
                setattr(app.state, name, value)
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
