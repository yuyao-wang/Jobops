from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.application_plan import (
    ApplicationPlan,
    ApplicationPlanReadResult,
    ApplicationPlanReadStatus,
)
from core.job_discovery import JobPosting
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from core.resume_candidates import (
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
    ResumeCandidate,
    ResumeCandidateFailureReason,
    ResumeCandidateListResult,
    ResumeCandidateListStatus,
    ResumeSummarySource,
    ResumeSummaryTrust,
    register_resume_candidate,
)
from core.resume_selection import (
    PrivateHomeResumeSelectionDecisionRepository,
    ResumeSelectionAgentDisposition,
    ResumeSelectionAgentMetadata,
    ResumeSelectionAgentOutput,
    ResumeSelectionDecisionReadStatus,
    ResumeSelectionDecisionWriteResult,
    ResumeSelectionDecisionWriteStatus,
    ResumeSelectionFailureReason,
    ResumeSelectionMethod,
    ResumeSelectionStatus,
    SelectBaseResumeCommand,
    select_base_resume,
)


NOW = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
METADATA = ResumeSelectionAgentMetadata(
    agent_version="resume-selector-v1",
    prompt_version="resume-selector-prompt-v1",
    model_id="synthetic-model",
)


def _home(tmp_path: Path) -> PrivateHome:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    return home


def _job(
    *,
    revision: int = 1,
    content_hash: str = "a" * 64,
) -> JobPosting:
    return JobPosting(
        schema_version="1.0",
        job_id="job-synthetic",
        revision=revision,
        source_platform="GREENHOUSE",
        source_job_id="123",
        source_url="https://example.test/jobs/123",
        company="Example Company",
        title="Machine Learning Engineer",
        location="Vancouver, Canada",
        work_mode="HYBRID",
        posted_at="2026-07-27T12:00:00Z",
        observed_at="2026-07-28T12:00:00Z",
        application_url="https://example.test/jobs/123/apply",
        ats_type="greenhouse",
        description="Build verified geospatial machine-learning systems.",
        content_hash=content_hash,
        status="NORMALIZED",
    )


def _plan(
    job: JobPosting,
    *,
    subject_id: str = "subject-a",
    instructions: str | None = "Prefer the geospatial-focused base resume.",
) -> ApplicationPlan:
    return ApplicationPlan.create(
        subject_id=subject_id,
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        priority_decision_id="priority-decision-synthetic",
        policy_id="policy-synthetic",
        policy_version=1,
        policy_content_hash="b" * 64,
        accepted_job_intent_id="accepted-intent-synthetic",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
        user_preparation_instructions=instructions,
    )


def _candidate(
    home: PrivateHome,
    *,
    subject_id: str = "subject-a",
    name: str = "geospatial.pdf",
    marker: bytes = b"geo",
    summary: str = "Verified geospatial ML and Python experience.",
) -> ResumeCandidate:
    artifact = home.paths.master_documents / name
    artifact.write_bytes(
        b"%PDF-1.7\nsynthetic resume " + marker + b"\n%%EOF\n"
    )
    repository = PrivateHomeResumeCandidateRepository(home)
    result = register_resume_candidate(
        RegisterResumeCandidateCommand(
            subject_id=subject_id,
            artifact_path=artifact,
            display_name=name,
            selection_safe_summary=summary,
            summary_source=ResumeSummarySource.AUTHENTICATED_CALLER,
            summary_trust=ResumeSummaryTrust.USER_CONFIRMED,
            now=NOW,
        ),
        home=home,
        repository=repository,
    )
    assert result.candidate is not None
    return result.candidate


class FakePlanRepository:
    def __init__(
        self,
        plans: tuple[ApplicationPlan, ...] = (),
        *,
        integrity_failure: bool = False,
    ) -> None:
        self.plans = {item.plan_id: item for item in plans}
        self.integrity_failure = integrity_failure
        self.calls: list[str] = []

    def get(self, plan_id: str) -> ApplicationPlanReadResult:
        self.calls.append(plan_id)
        if self.integrity_failure:
            from core.application_plan import ApplicationPlanFailureReason

            return ApplicationPlanReadResult(
                status=ApplicationPlanReadStatus.INTEGRITY_FAILURE,
                plan=None,
                reason_code=ApplicationPlanFailureReason.INTEGRITY_FAILURE,
            )
        plan = self.plans.get(plan_id)
        return ApplicationPlanReadResult(
            status=(
                ApplicationPlanReadStatus.FOUND
                if plan is not None
                else ApplicationPlanReadStatus.NOT_FOUND
            ),
            plan=plan,
        )


class FakeJobRepository:
    def __init__(self, job: JobPosting | None) -> None:
        self.job = job
        self.calls: list[str] = []

    def get(self, job_id: str) -> JobPosting | None:
        self.calls.append(job_id)
        return self.job

    def list_current(self) -> tuple[JobPosting, ...]:
        raise AssertionError("P2a3 must not enumerate jobs")


class FakeCandidateProvider:
    def __init__(
        self,
        subject_id: str,
        candidates: tuple[ResumeCandidate, ...],
        *,
        fail: bool = False,
    ) -> None:
        self.subject_id = subject_id
        self.candidates = candidates
        self.fail = fail
        self.calls: list[str] = []

    def list_selectable(self, subject_id: str) -> ResumeCandidateListResult:
        self.calls.append(subject_id)
        if self.fail:
            return ResumeCandidateListResult(
                status=ResumeCandidateListStatus.FAILED,
                subject_id=subject_id,
                candidates=(),
                reason_code=ResumeCandidateFailureReason.INTEGRITY_FAILURE,
            )
        return ResumeCandidateListResult(
            status=ResumeCandidateListStatus.SUCCEEDED,
            subject_id=self.subject_id,
            candidates=self.candidates,
        )


class FakeAgent:
    def __init__(self, output: Any) -> None:
        self.output = output
        self.calls: list[Any] = []

    async def evaluate(self, context):
        self.calls.append(context)
        if isinstance(self.output, BaseException):
            raise self.output
        return self.output


def _selected(candidate: ResumeCandidate, rationale: str = "Best fit.") -> ResumeSelectionAgentOutput:
    return ResumeSelectionAgentOutput(
        disposition=ResumeSelectionAgentDisposition.SELECTED,
        selected_resume_id=candidate.resume_id,
        selected_candidate_version=candidate.contract_version,
        selected_artifact_sha256=candidate.artifact_sha256,
        rationale=rationale,
    )


async def _run(
    *,
    plan: ApplicationPlan,
    job: JobPosting | None,
    provider: FakeCandidateProvider,
    agent: FakeAgent,
    repository: Any,
    metadata: ResumeSelectionAgentMetadata = METADATA,
    subject_id: str | None = None,
    now: datetime = NOW,
):
    return await select_base_resume(
        SelectBaseResumeCommand(
            subject_id=subject_id or plan.subject_id,
            application_plan_id=plan.plan_id,
            now=now,
        ),
        application_plan_repository=FakePlanRepository((plan,)),
        job_repository=FakeJobRepository(job),
        candidate_provider=provider,
        agent=agent,
        metadata=metadata,
        decision_repository=repository,
    )


@pytest.mark.asyncio
async def test_single_candidate_is_selected_without_agent(tmp_path: Path) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    candidate = _candidate(home)
    agent = FakeAgent(AssertionError("Agent must not be called"))
    repository = PrivateHomeResumeSelectionDecisionRepository(home)

    result = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (candidate,)),
        agent=agent,
        repository=repository,
    )

    assert result.status is ResumeSelectionStatus.CREATED
    assert result.decision is not None
    assert result.decision.selection_method is ResumeSelectionMethod.ONLY_CANDIDATE
    assert result.decision.source_resume_id == candidate.resume_id
    assert result.decision.source_candidate_version == candidate.contract_version
    assert result.decision.source_artifact_sha256 == candidate.artifact_sha256
    assert agent.calls == []


@pytest.mark.asyncio
async def test_multiple_candidates_call_agent_once_with_safe_context(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    first = _candidate(home, name="general.pdf", marker=b"general")
    second = _candidate(
        home,
        name="geo.pdf",
        marker=b"geo",
        summary="Verified remote-sensing and geospatial ML experience.",
    )
    agent = FakeAgent(_selected(second, "Matches the geospatial JD."))

    result = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (first, second)),
        agent=agent,
        repository=PrivateHomeResumeSelectionDecisionRepository(home),
    )

    assert result.status is ResumeSelectionStatus.CREATED
    assert result.decision is not None
    assert result.decision.selection_method is ResumeSelectionMethod.AGENT_SELECTED
    assert result.decision.source_resume_id == second.resume_id
    assert len(agent.calls) == 1
    context = agent.calls[0]
    assert context.job.description == job.description
    assert context.job.revision == plan.job_revision
    assert context.job.content_hash == plan.job_content_hash
    assert context.user_preparation_instructions == plan.user_preparation_instructions
    assert all(
        not hasattr(item, "artifact_reference") for item in context.candidates
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "revision", "hash"])
async def test_job_must_exist_and_match_plan_binding(
    tmp_path: Path,
    case: str,
) -> None:
    home = _home(tmp_path)
    bound_job = _job()
    plan = _plan(bound_job)
    supplied = {
        "missing": None,
        "revision": _job(revision=2),
        "hash": _job(content_hash="c" * 64),
    }[case]
    provider = FakeCandidateProvider(
        "subject-a",
        (_candidate(home),),
    )
    agent = FakeAgent(AssertionError("Agent must not be called"))
    repository = PrivateHomeResumeSelectionDecisionRepository(home)

    result = await _run(
        plan=plan,
        job=supplied,
        provider=provider,
        agent=agent,
        repository=repository,
    )

    assert result.status is ResumeSelectionStatus.FAILED
    assert result.reason_code is (
        ResumeSelectionFailureReason.JOB_NOT_FOUND
        if case == "missing"
        else ResumeSelectionFailureReason.JOB_BINDING_MISMATCH
    )
    assert provider.calls == []
    assert agent.calls == []
    assert not tuple(home.paths.resume_selection_decisions.rglob("*.json"))


@pytest.mark.asyncio
async def test_no_candidate_defers_without_agent_or_write(tmp_path: Path) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    agent = FakeAgent(AssertionError("Agent must not be called"))

    result = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", ()),
        agent=agent,
        repository=PrivateHomeResumeSelectionDecisionRepository(home),
    )

    assert result.status is ResumeSelectionStatus.DEFERRED_NO_RESUME
    assert result.decision is None
    assert result.write_result is None
    assert agent.calls == []
    assert not tuple(home.paths.resume_selection_decisions.rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_factory",
    [
        lambda first, second: ResumeSelectionAgentOutput(
            disposition=ResumeSelectionAgentDisposition.SELECTED,
            selected_resume_id="resume-candidate-" + "f" * 64,
            selected_candidate_version=first.contract_version,
            selected_artifact_sha256=first.artifact_sha256,
            rationale="Unknown candidate.",
        ),
        lambda first, second: ResumeSelectionAgentOutput(
            disposition=ResumeSelectionAgentDisposition.SELECTED,
            selected_resume_id=first.resume_id,
            selected_candidate_version="resume-candidate-v999",
            selected_artifact_sha256=first.artifact_sha256,
            rationale="Wrong version.",
        ),
        lambda first, second: ResumeSelectionAgentOutput(
            disposition=ResumeSelectionAgentDisposition.SELECTED,
            selected_resume_id=first.resume_id,
            selected_candidate_version=first.contract_version,
            selected_artifact_sha256="f" * 64,
            rationale="Wrong hash.",
        ),
        lambda first, second: ResumeSelectionAgentOutput(
            disposition=ResumeSelectionAgentDisposition.DEFERRED,
            selected_resume_id=None,
            selected_candidate_version=None,
            selected_artifact_sha256=None,
            rationale="The candidates cannot be distinguished safely.",
        ),
        lambda first, second: {"selected_resume_id": first.resume_id},
    ],
)
async def test_unsafe_agent_choice_defers_for_human(
    tmp_path: Path,
    output_factory,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    first = _candidate(home, name="first.pdf", marker=b"first")
    second = _candidate(home, name="second.pdf", marker=b"second")
    agent = FakeAgent(output_factory(first, second))

    result = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (first, second)),
        agent=agent,
        repository=PrivateHomeResumeSelectionDecisionRepository(home),
    )

    assert result.status is ResumeSelectionStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is ResumeSelectionFailureReason.AGENT_SELECTION_UNSAFE
    )
    assert len(agent.calls) == 1
    assert result.decision is None
    assert not tuple(home.paths.resume_selection_decisions.rglob("*.json"))


@pytest.mark.asyncio
async def test_identical_completed_binding_is_unchanged_without_agent(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    first = _candidate(home, name="first.pdf", marker=b"first")
    second = _candidate(home, name="second.pdf", marker=b"second")
    provider = FakeCandidateProvider("subject-a", (first, second))
    repository = PrivateHomeResumeSelectionDecisionRepository(home)
    first_agent = FakeAgent(_selected(first))
    created = await _run(
        plan=plan,
        job=job,
        provider=provider,
        agent=first_agent,
        repository=repository,
    )
    second_agent = FakeAgent(AssertionError("Agent must not run on replay"))

    replay = await _run(
        plan=plan,
        job=job,
        provider=provider,
        agent=second_agent,
        repository=repository,
        now=NOW + timedelta(days=1),
    )

    assert created.status is ResumeSelectionStatus.CREATED
    assert replay.status is ResumeSelectionStatus.UNCHANGED
    assert replay.decision == created.decision
    assert replay.decision.selected_at == NOW
    assert second_agent.calls == []
    assert len(tuple(home.paths.resume_selection_decisions.rglob("*.json"))) == 1


@pytest.mark.asyncio
async def test_changed_plan_job_candidate_set_and_metadata_change_binding(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    first = _candidate(home, name="first.pdf", marker=b"first")
    second = _candidate(home, name="second.pdf", marker=b"second")
    repository = PrivateHomeResumeSelectionDecisionRepository(home)
    initial = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (first,)),
        agent=FakeAgent(None),
        repository=repository,
    )
    changed_plan = _plan(job, instructions="Use a general-purpose resume.")
    by_plan = await _run(
        plan=changed_plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (first,)),
        agent=FakeAgent(None),
        repository=repository,
    )
    changed_job = _job(revision=2, content_hash="c" * 64)
    changed_job_plan = _plan(changed_job)
    by_job = await _run(
        plan=changed_job_plan,
        job=changed_job,
        provider=FakeCandidateProvider("subject-a", (first,)),
        agent=FakeAgent(None),
        repository=repository,
    )
    candidate_agent = FakeAgent(_selected(second))
    by_candidates = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (first, second)),
        agent=candidate_agent,
        repository=repository,
    )
    metadata_agent = FakeAgent(_selected(second))
    by_metadata = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (first, second)),
        agent=metadata_agent,
        metadata=replace(METADATA, model_id="synthetic-model-v2"),
        repository=repository,
    )

    results = (initial, by_plan, by_job, by_candidates, by_metadata)
    assert all(item.status is ResumeSelectionStatus.CREATED for item in results)
    assert len({item.selection_binding for item in results}) == len(results)
    assert len(candidate_agent.calls) == 1
    assert len(metadata_agent.calls) == 1


@pytest.mark.asyncio
async def test_subject_isolation_blocks_other_subject_candidates(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan_a = _plan(job, subject_id="subject-a")
    candidate_a = _candidate(home, subject_id="subject-a")
    provider_b = PrivateHomeResumeCandidateRepository(home)
    agent = FakeAgent(AssertionError("Agent must not be called"))

    result = await select_base_resume(
        SelectBaseResumeCommand(
            subject_id="subject-b",
            application_plan_id=plan_a.plan_id,
            now=NOW,
        ),
        application_plan_repository=FakePlanRepository((plan_a,)),
        job_repository=FakeJobRepository(job),
        candidate_provider=provider_b,
        agent=agent,
        metadata=METADATA,
        decision_repository=(
            PrivateHomeResumeSelectionDecisionRepository(home)
        ),
    )

    assert candidate_a.subject_id == "subject-a"
    assert result.status is ResumeSelectionStatus.FAILED
    assert (
        result.reason_code
        is ResumeSelectionFailureReason.APPLICATION_PLAN_SUBJECT_MISMATCH
    )
    assert agent.calls == []


@pytest.mark.asyncio
async def test_missing_plan_and_candidate_integrity_failure_stop_early(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    agent = FakeAgent(AssertionError("Agent must not be called"))
    repository = PrivateHomeResumeSelectionDecisionRepository(home)
    missing = await select_base_resume(
        SelectBaseResumeCommand(
            subject_id="subject-a",
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=FakePlanRepository(),
        job_repository=FakeJobRepository(job),
        candidate_provider=FakeCandidateProvider("subject-a", ()),
        agent=agent,
        metadata=METADATA,
        decision_repository=repository,
    )
    failed_provider = FakeCandidateProvider("subject-a", (), fail=True)
    corrupt_candidates = await _run(
        plan=plan,
        job=job,
        provider=failed_provider,
        agent=agent,
        repository=repository,
    )

    assert (
        missing.reason_code
        is ResumeSelectionFailureReason.APPLICATION_PLAN_NOT_FOUND
    )
    assert (
        corrupt_candidates.reason_code
        is ResumeSelectionFailureReason.CANDIDATE_PROVIDER_FAILED
    )
    assert agent.calls == []
    assert not tuple(home.paths.resume_selection_decisions.rglob("*.json"))


@pytest.mark.asyncio
async def test_corrupt_decision_fails_closed_and_is_not_overwritten(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    first = _candidate(home, name="first.pdf", marker=b"first")
    second = _candidate(home, name="second.pdf", marker=b"second")
    provider = FakeCandidateProvider("subject-a", (first, second))
    repository = PrivateHomeResumeSelectionDecisionRepository(home)
    created = await _run(
        plan=plan,
        job=job,
        provider=provider,
        agent=FakeAgent(_selected(first)),
        repository=repository,
    )
    assert created.decision is not None
    path = next(
        home.paths.resume_selection_decisions.rglob(
            f"{created.decision.decision_id}.json"
        )
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["rationale"] = "Tampered rationale."
    path.write_text(json.dumps(document), encoding="utf-8")
    before = path.read_bytes()
    replay_agent = FakeAgent(AssertionError("Agent must not run"))

    replay = await _run(
        plan=plan,
        job=job,
        provider=provider,
        agent=replay_agent,
        repository=repository,
    )

    assert replay.status is ResumeSelectionStatus.FAILED
    assert (
        replay.reason_code
        is ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
    )
    assert replay_agent.calls == []
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_immutable_identity_conflict_does_not_overwrite(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    first = _candidate(home, name="first.pdf", marker=b"first")
    second = _candidate(home, name="second.pdf", marker=b"second")
    repository = PrivateHomeResumeSelectionDecisionRepository(home)
    created = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (first, second)),
        agent=FakeAgent(_selected(first)),
        repository=repository,
    )
    assert created.decision is not None
    original = created.decision
    document = original.to_dict()
    document["source_resume_id"] = second.resume_id
    document["source_candidate_version"] = second.contract_version
    document["source_artifact_sha256"] = second.artifact_sha256
    document["rationale"] = "Conflicting valid selection."
    content = {
        key: value
        for key, value in document.items()
        if key not in {"decision_content_hash", "selected_at"}
    }
    document["decision_content_hash"] = hashlib.sha256(
        json.dumps(
            content,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    from core.resume_selection import ResumeSelectionDecision

    conflicting = ResumeSelectionDecision(
        decision_id=document["decision_id"],
        contract_version=document["contract_version"],
        selection_binding=document["selection_binding"],
        decision_content_hash=document["decision_content_hash"],
        subject_id=document["subject_id"],
        application_plan_id=document["application_plan_id"],
        job_id=document["job_id"],
        job_revision=document["job_revision"],
        job_content_hash=document["job_content_hash"],
        source_resume_id=document["source_resume_id"],
        source_candidate_version=document["source_candidate_version"],
        source_artifact_sha256=document["source_artifact_sha256"],
        candidate_set_hash=document["candidate_set_hash"],
        selection_method=document["selection_method"],
        rationale=document["rationale"],
        agent_version=document["agent_version"],
        prompt_version=document["prompt_version"],
        model_id=document["model_id"],
        selected_at=original.selected_at,
    )
    path = next(
        home.paths.resume_selection_decisions.rglob(
            f"{original.decision_id}.json"
        )
    )
    before = path.read_bytes()

    conflict = repository.save(conflicting)

    assert conflict.status is ResumeSelectionDecisionWriteStatus.FAILED
    assert (
        conflict.reason_code
        is ResumeSelectionFailureReason.DECISION_INTEGRITY_FAILURE
    )
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_repository_failure_does_not_report_success(tmp_path: Path) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    candidate = _candidate(home)

    class FailingRepository:
        def find_completed_by_binding(self, **_kwargs):
            from core.resume_selection import (
                ResumeSelectionDecisionReadResult,
            )

            return ResumeSelectionDecisionReadResult(
                status=ResumeSelectionDecisionReadStatus.NOT_FOUND,
                decision=None,
            )

        def save(self, _decision):
            return ResumeSelectionDecisionWriteResult(
                status=ResumeSelectionDecisionWriteStatus.FAILED,
                decision=None,
                reason_code=(
                    ResumeSelectionFailureReason.DECISION_PERSISTENCE_FAILED
                ),
                retryable=True,
            )

    result = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (candidate,)),
        agent=FakeAgent(None),
        repository=FailingRepository(),
    )

    assert result.status is ResumeSelectionStatus.FAILED
    assert result.retryable is True
    assert result.decision is None


@pytest.mark.asyncio
async def test_restart_reads_same_immutable_decision(tmp_path: Path) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    candidate = _candidate(home)
    first_repository = PrivateHomeResumeSelectionDecisionRepository(home)
    created = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (candidate,)),
        agent=FakeAgent(None),
        repository=first_repository,
    )
    assert created.decision is not None
    restarted = PrivateHomeResumeSelectionDecisionRepository(
        PrivateHome(home.root)
    )

    read = restarted.get(
        subject_id="subject-a",
        decision_id=created.decision.decision_id,
    )

    assert read.status is ResumeSelectionDecisionReadStatus.FOUND
    assert read.decision == created.decision


@pytest.mark.asyncio
async def test_agent_timeout_is_typed_and_not_retried(tmp_path: Path) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    first = _candidate(home, name="first.pdf", marker=b"first")
    second = _candidate(home, name="second.pdf", marker=b"second")
    agent = FakeAgent(TimeoutError("synthetic timeout"))

    result = await _run(
        plan=plan,
        job=job,
        provider=FakeCandidateProvider("subject-a", (first, second)),
        agent=agent,
        repository=PrivateHomeResumeSelectionDecisionRepository(home),
    )

    assert result.status is ResumeSelectionStatus.FAILED
    assert result.reason_code is ResumeSelectionFailureReason.AGENT_TIMEOUT
    assert result.retryable is True
    assert len(agent.calls) == 1


def test_module_has_no_tailoring_execution_or_priority_dependencies() -> None:
    source = Path("core/resume_selection.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = {
        "core.job_prioritization",
        "core.runnable_application_queue",
        "core.application_engine",
        "core.materials",
        "core.accepted_job_intent",
        "adapters",
        "browser",
    }
    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
