"""Focused S4a0 authenticated Dashboard read-model tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.application_plan import ApplicationPlanListStatus
from core.application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS,
)
from core.application_preparation_orchestrator import (
    ApplicationPreparationRunReadStatus,
    ApplicationPreparationRunStatus,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.candidate_identity_facts import (
    CandidateIdentityFactConflictState,
    PrivateHomeCandidateIdentityFactRepository,
)
from core.candidate_information_sources import (
    CandidateInformationSourceListStatus,
    PrivateHomeCandidateInformationSourceRepository,
)
from core.current_application_execution_queue import (
    CurrentApplicationExecutionQueueStatus,
    CurrentApplicationExecutionStatus,
)
from core.current_priority_queue import CurrentPriorityItemStatus
from core.dashboard_read_models import (
    DashboardApplicationStatus,
    DashboardApplicationsReader,
    DashboardCandidateProfileReader,
    DashboardJobStatus,
    DashboardJobsReader,
    DashboardNextStep,
    DashboardOverviewReader,
    DashboardProfileState,
    DashboardReadStatus,
)
from core.human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionQueueStatus,
)
from core.job_prioritization import PriorityQualification
from core.private_home import PrivateHome
from core.runnable_application_queue import (
    RunnableApplicationQueueStatus,
    RunnableApplicationStatus,
)
from core.search_profile import (
    PrivateHomeSearchProfileRepository,
    SearchProfileListStatus,
)
from dashboard.read_models import DashboardOverviewController
from dashboard.server import app
from fastapi.testclient import TestClient


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
SUBJECT = "subject-dashboard-synthetic"


def _files(root: Path) -> dict[Path, tuple[bytes, int]]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_profile_empty_is_verified_fact_only_and_zero_write(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    reader = DashboardCandidateProfileReader(
        fact_repository=PrivateHomeCandidateIdentityFactRepository(home),
        source_provider=PrivateHomeCandidateInformationSourceRepository(home),
        search_profile_provider=PrivateHomeSearchProfileRepository(home),
    )
    before = _files(home.root)

    result = await reader.read(subject_id=SUBJECT, evaluated_at=NOW)

    assert result.read_status is DashboardReadStatus.EMPTY
    assert result.profile_state is DashboardProfileState.EMPTY
    assert result.required_field_count == 3
    assert set(result.missing_required_fields) == {
        "first_name",
        "last_name",
        "email",
    }
    assert result.search_preference_summary["enabled_profile_count"] == 0
    assert result.capabilities["review_capability"] == "UNAVAILABLE"
    assert _files(home.root) == before == {}


@pytest.mark.asyncio
async def test_jobs_maps_formal_priority_without_inventing_score() -> None:
    job = SimpleNamespace(
        job_id="job-one",
        revision=1,
        content_hash="a" * 64,
        title="Synthetic Engineer",
        company="Example Labs",
        location="Remote",
        application_url="https://example.test/jobs/one",
        source_url="https://example.test/jobs/one",
        observed_at="2026-07-30T11:00:00Z",
    )
    rationale = SimpleNamespace(explanation="Relevant verified experience")
    decision = SimpleNamespace(
        decision_id="decision-one",
        priority_level=SimpleNamespace(value="P0"),
        qualification=PriorityQualification.QUALIFIED,
        positive_signals=(rationale,),
    )
    queue = SimpleNamespace(
        status=RunnableApplicationQueueStatus.SUCCEEDED,
        subject_id=SUBJECT,
        items=(
            SimpleNamespace(
                subject_id=SUBJECT,
                job=job,
                priority_queue_status=CurrentPriorityItemStatus.CURRENT,
                runnable_status=RunnableApplicationStatus.RUNNABLE,
                priority_decision=decision,
                application_intent=SimpleNamespace(
                    intent=SimpleNamespace(value="REQUEST_APPLICATION")
                ),
            ),
        ),
        priority_queue_result=SimpleNamespace(
            membership_snapshot_hash="b" * 64
        ),
    )

    async def queue_reader(_command):
        return queue

    reader = DashboardJobsReader(
        runnable_queue_reader=queue_reader,
        application_plan_repository=SimpleNamespace(
            list_for_subject=lambda _subject: SimpleNamespace(
                status=ApplicationPlanListStatus.SUCCEEDED, plans=()
            )
        ),
    )
    result = await reader.read(subject_id=SUBJECT, evaluated_at=NOW)

    assert result.read_status is DashboardReadStatus.READY
    assert result.counts["ready_to_prepare"] == 1
    assert result.ordered_items[0].application_status is (
        DashboardJobStatus.READY_TO_PREPARE
    )
    assert result.ordered_items[0].match_score is None
    assert result.ordered_items[0].match_reasons == (
        "Relevant verified experience",
    )


@pytest.mark.asyncio
async def test_applications_uses_terminal_and_attention_precedence() -> None:
    plans = (
        SimpleNamespace(
            plan_id="plan-submitted",
            subject_id=SUBJECT,
            job_id="job-submitted",
            job_revision=1,
            job_content_hash="1" * 64,
            created_at=NOW - timedelta(days=2),
        ),
        SimpleNamespace(
            plan_id="plan-attention",
            subject_id=SUBJECT,
            job_id="job-attention",
            job_revision=1,
            job_content_hash="2" * 64,
            created_at=NOW - timedelta(days=1),
        ),
    )
    attention_item = SimpleNamespace(
        subject_id=SUBJECT,
        application_plan_id="plan-attention",
        audience=HumanAttentionAudience.USER,
        source_event_time=NOW,
        item_content_hash="3" * 64,
    )
    attention = SimpleNamespace(
        status=HumanAttentionQueueStatus.SUCCEEDED,
        subject_id=SUBJECT,
        items=(attention_item,),
        queue_snapshot_hash="4" * 64,
        item_count=1,
        user_item_count=1,
        operator_item_count=0,
    )
    execution_item = SimpleNamespace(
        application_plan_id="plan-submitted",
        execution_status=CurrentApplicationExecutionStatus.SUBMITTED,
        item_hash="5" * 64,
    )
    execution = SimpleNamespace(
        status=CurrentApplicationExecutionQueueStatus.SUCCEEDED,
        items=(execution_item,),
        snapshot_hash="6" * 64,
    )
    prep = SimpleNamespace(
        run_id="prep-current",
        status=ApplicationPreparationRunStatus.COMPLETED,
        completed_at=NOW - timedelta(hours=2),
    )
    jobs = {
        plan.job_id: SimpleNamespace(
            job_id=plan.job_id,
            revision=plan.job_revision,
            content_hash=plan.job_content_hash,
            title="Synthetic Role",
            company="Example Labs",
            location="Remote",
        )
        for plan in plans
    }
    calls = {"attention": 0, "execution": 0}

    def attention_reader(**_kwargs):
        calls["attention"] += 1
        return attention

    def execution_reader(**_kwargs):
        calls["execution"] += 1
        return execution

    reader = DashboardApplicationsReader(
        application_plan_repository=SimpleNamespace(
            list_for_subject=lambda _subject: SimpleNamespace(
                status=ApplicationPlanListStatus.SUCCEEDED, plans=plans
            )
        ),
        preparation_run_repository=SimpleNamespace(
            find_current_for_plan=lambda **_kwargs: SimpleNamespace(
                status=ApplicationPreparationRunReadStatus.FOUND,
                run=prep,
            )
        ),
        human_attention_reader=attention_reader,
        execution_queue_reader=execution_reader,
        job_posting_reader=SimpleNamespace(get=lambda job_id: jobs[job_id]),
    )
    result = await reader.read(subject_id=SUBJECT, evaluated_at=NOW)

    statuses = {
        item.application_plan_id: item.product_status
        for item in result.ordered_items
    }
    assert statuses["plan-submitted"] is DashboardApplicationStatus.SUBMITTED
    assert statuses["plan-attention"] is (
        DashboardApplicationStatus.NEEDS_ATTENTION
    )
    assert calls == {"attention": 1, "execution": 1}


@pytest.mark.asyncio
async def test_overview_has_deterministic_next_step_and_reuses_attention() -> None:
    profile_reader = DashboardCandidateProfileReader(
        fact_repository=SimpleNamespace(
            get_index=lambda subject: SimpleNamespace(
                subject_id=subject,
                entries=tuple(
                    SimpleNamespace(
                        field_key=definition.field_key,
                        current_fact_id=None,
                        conflict_state=CandidateIdentityFactConflictState.NONE,
                        source_refs=(),
                        identity_dict=lambda: {},
                    )
                    for definition in (
                        APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITIONS
                    )
                ),
            )
        ),
        source_provider=SimpleNamespace(
            list_for_subject=lambda _subject: SimpleNamespace(
                status=CandidateInformationSourceListStatus.SUCCEEDED,
                sources=(),
            )
        ),
        search_profile_provider=SimpleNamespace(
            list_enabled=lambda _subject: SimpleNamespace(
                status=SearchProfileListStatus.SUCCEEDED, profiles=()
            )
        ),
    )
    # Use already typed readers with deterministic empty formal snapshots.
    jobs_reader = DashboardJobsReader(
        runnable_queue_reader=lambda _command: SimpleNamespace(
            status=RunnableApplicationQueueStatus.SUCCEEDED,
            subject_id=SUBJECT,
            items=(),
            priority_queue_result=SimpleNamespace(
                membership_snapshot_hash="7" * 64
            ),
        ),
        application_plan_repository=SimpleNamespace(
            list_for_subject=lambda _subject: SimpleNamespace(
                status=ApplicationPlanListStatus.SUCCEEDED, plans=()
            )
        ),
    )
    attention_calls = 0

    def attention_reader(**_kwargs):
        nonlocal attention_calls
        attention_calls += 1
        return SimpleNamespace(
            status=HumanAttentionQueueStatus.SUCCEEDED,
            subject_id=SUBJECT,
            items=(),
            item_count=0,
            user_item_count=0,
            operator_item_count=0,
            queue_snapshot_hash="8" * 64,
        )

    applications_reader = DashboardApplicationsReader(
        application_plan_repository=SimpleNamespace(
            list_for_subject=lambda _subject: SimpleNamespace(
                status=ApplicationPlanListStatus.SUCCEEDED, plans=()
            )
        ),
        preparation_run_repository=SimpleNamespace(),
        human_attention_reader=attention_reader,
        execution_queue_reader=lambda **_kwargs: SimpleNamespace(
            status=CurrentApplicationExecutionQueueStatus.SUCCEEDED,
            items=(),
            snapshot_hash="9" * 64,
        ),
        job_posting_reader=SimpleNamespace(),
    )
    overview = DashboardOverviewReader(
        profile_reader=profile_reader,
        jobs_reader=jobs_reader,
        applications_reader=applications_reader,
        human_attention_reader=attention_reader,
    )
    result = await overview.read(subject_id=SUBJECT, evaluated_at=NOW)

    assert result.next_step is DashboardNextStep.COMPLETE_PROFILE
    assert attention_calls == 1
    assert result.top_matches == ()
    assert result.recent_applications == ()

    context = AuthenticatedSubjectContext(
        session_id=f"session-{'a' * 64}",
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )

    async def authenticated(_request):
        return context

    app.state.authenticated_subject_dependency = authenticated
    app.state.dashboard_overview_controller = DashboardOverviewController(
        reader=overview, clock=lambda: NOW
    )
    response = TestClient(app).get(
        "/api/dashboard/overview?subject_id=subject-other"
    )
    assert response.status_code == 200
    assert response.json()["next_step"] == "COMPLETE_PROFILE"
    assert "overview_snapshot_hash" not in response.json()
