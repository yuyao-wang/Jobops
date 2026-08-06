"""Synthetic acceptance tests for the P2b4 single-job orchestrator."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.application_preparation_orchestrator as orchestrator_module
from core.application_answers import (
    ApplicationAnswerPolicy,
    PrepareApplicationAnswersCommand,
    PreparedApplicationAnswerSetStatus,
    PrivateHomeApplicationFactProvider,
    PrivateHomePreparedApplicationAnswerSetRepository,
    prepare_application_answers,
)
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_STAGE_ORDER,
    ApplicationPreparationCompletedRole,
    ApplicationPreparationFailureReason,
    ApplicationPreparationRecipe,
    ApplicationPreparationRunReadStatus,
    ApplicationPreparationStage,
    ApplicationPreparationStageDefinition,
    ApplicationPreparationStatus,
    PreparationStageExecutionStatus,
    PreparationStageOutcome,
    PrivateHomeApplicationPreparationRunRepository,
    PublicPreparationStageResult,
    PublicStageDirective,
    PublicStageStatus,
    RequiredApplicationMaterialPolicy,
    RunApplicationPreparationCommand,
    run_application_preparation,
)
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 29, 1, 0, tzinfo=timezone.utc)
SUBJECT = "subject-orchestration-synthetic"
OTHER_SUBJECT = "subject-orchestration-other"
JOB_ID = "job-orchestration-synthetic"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _plan(
    home: PrivateHome,
    *,
    subject_id: str = SUBJECT,
    revision: int = 1,
    priority: ProposedPriorityLevel = ProposedPriorityLevel.P1,
) -> tuple[ApplicationPlan, PrivateHomeApplicationPlanRepository]:
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id=JOB_ID,
        job_revision=revision,
        job_content_hash=_hash(f"job-{revision}"),
        priority_decision_id=f"decision-{revision}",
        policy_id="priority-policy-v1",
        policy_version=1,
        policy_content_hash="a" * 64,
        accepted_job_intent_id=f"intent-{revision}",
        priority_level=priority,
        created_at=NOW,
    )
    repository = PrivateHomeApplicationPlanRepository(home)
    assert repository.save(plan).plan == plan
    return plan, repository


OUTPUTS = {
    ApplicationPreparationStage.BASE_RESUME_SELECTION: {
        "resume_selection_decision_id": "resume-selection-1",
        "resume_id": "resume-1",
    },
    ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: {
        "source_resume_projection_id": "source-projection-1",
    },
    ApplicationPreparationStage.RESUME_EVIDENCE: {
        "resume_evidence_snapshot_id": "resume-evidence-1",
    },
    ApplicationPreparationStage.RESUME_TAILORING: {
        "tailored_resume_draft_id": "resume-draft-1",
    },
    ApplicationPreparationStage.RESUME_FACT_QA: {
        "resume_fact_qa_result_id": "resume-fact-qa-1",
    },
    ApplicationPreparationStage.BASE_LATEX_SELECTION: {
        "base_latex_selection_id": "base-latex-selection-1",
    },
    ApplicationPreparationStage.LATEX_CONSTRUCTION: {
        "latex_version_id": "latex-version-initial",
        "latex_construction_record_id": "latex-construction-1",
    },
    ApplicationPreparationStage.RESUME_COMPILATION: {
        "compilation_record_id": "compilation-initial",
    },
    ApplicationPreparationStage.RESUME_VISUAL_QA: {
        "visual_qa_result_id": "visual-qa-initial",
    },
    ApplicationPreparationStage.RESUME_LAYOUT_REVISION: {
        "layout_revision_run_id": "layout-revision-1",
        "latex_version_id": "latex-version-revised",
        "compilation_record_id": "compilation-revised",
        "visual_qa_result_id": "visual-qa-revised",
    },
    ApplicationPreparationStage.RESUME_PUBLICATION: {
        "prepared_resume_material_id": "prepared-resume-1",
    },
    ApplicationPreparationStage.RESUME_MANIFEST: {
        "plan_material_manifest_id": "resume-manifest-1",
    },
    ApplicationPreparationStage.COVER_LETTER_EVIDENCE: {
        "cover_letter_evidence_snapshot_id": "cover-evidence-1",
    },
    ApplicationPreparationStage.COVER_LETTER_DRAFT: {
        "cover_letter_draft_id": "cover-draft-1",
    },
    ApplicationPreparationStage.COVER_LETTER_FACT_QA: {
        "cover_letter_fact_qa_result_id": "cover-fact-qa-1",
    },
    ApplicationPreparationStage.COVER_LETTER_PUBLICATION: {
        "prepared_cover_letter_material_id": "prepared-cover-1",
    },
    ApplicationPreparationStage.COVER_LETTER_MANIFEST: {
        "plan_material_manifest_id": "resume-cover-manifest-1",
    },
    ApplicationPreparationStage.APPLICATION_ANSWERS: {
        "prepared_application_answer_set_id": "answers-1",
    },
}


class _Recorder:
    def __init__(
        self,
        *,
        statuses: dict[ApplicationPreparationStage, PublicStageStatus]
        | None = None,
        visual_directive: PublicStageDirective = (
            PublicStageDirective.PASSED
        ),
        human_attention: bool = False,
    ) -> None:
        self.statuses = statuses or {}
        self.visual_directive = visual_directive
        self.human_attention = human_attention
        self.requests = []
        self.active = 0
        self.max_active = 0

    def invoke(self, request):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.requests.append(request)
            status = self.statuses.get(
                request.stage, PublicStageStatus.CREATED
            )
            if status in {
                PublicStageStatus.DEFERRED,
                PublicStageStatus.FAILED,
            }:
                return PublicPreparationStageResult.legacy_stopped(
                    stage=request.stage,
                    status=status,
                    public_status=f"SYNTHETIC_{status.value}",
                    reason_code=f"{request.stage.value}_{status.value}",
                    human_attention_required=(
                        status is PublicStageStatus.DEFERRED
                    ),
                )
            directive = (
                self.visual_directive
                if request.stage
                is ApplicationPreparationStage.RESUME_VISUAL_QA
                else PublicStageDirective.CONTINUE
            )
            return PublicPreparationStageResult.legacy_success(
                stage=request.stage,
                status=status,
                public_status=f"SYNTHETIC_{status.value}",
                result_id=f"result-{request.stage.value.lower()}",
                result_content_hash=_hash(
                    f"{request.stage.value}:{status.value}"
                ),
                outputs=OUTPUTS[request.stage],
                human_attention_required=(
                    self.human_attention
                    and request.stage
                    is ApplicationPreparationStage.APPLICATION_ANSWERS
                ),
                directive=directive,
            )
        finally:
            self.active -= 1


def _recipe(
    recorder: _Recorder,
    *,
    input_binding: str = "binding-v1",
    overrides: dict | None = None,
) -> ApplicationPreparationRecipe:
    overrides = overrides or {}
    definitions = tuple(
        ApplicationPreparationStageDefinition(
            stage=stage,
            public_callable_name=f"public_{stage.value.lower()}",
            slice_contract_version=f"{stage.value.lower()}-contract-v1",
            slice_policy_version=f"{stage.value.lower()}-policy-v1",
            configuration_hash=_hash(f"config:{stage.value}"),
            invoke=overrides.get(stage, recorder.invoke),
        )
        for stage in APPLICATION_PREPARATION_STAGE_ORDER
    )
    return ApplicationPreparationRecipe(
        input_binding_hash=_hash(input_binding),
        stages=definitions,
        required_material_policy=RequiredApplicationMaterialPolicy.v1(),
    )


async def _run(
    home: PrivateHome,
    recorder: _Recorder,
    *,
    recipe: ApplicationPreparationRecipe | None = None,
    now: datetime = NOW,
    priority: ProposedPriorityLevel = ProposedPriorityLevel.P1,
):
    plan, plan_repository = _plan(home, priority=priority)
    run_repository = PrivateHomeApplicationPreparationRunRepository(home)
    result = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=now,
        ),
        application_plan_repository=plan_repository,
        recipe=recipe or _recipe(recorder),
        run_repository=run_repository,
    )
    return result, plan, plan_repository, run_repository


async def test_p2_reuses_selected_resume_without_tailoring_or_latex(
    tmp_path: Path,
) -> None:
    recorder = _Recorder()
    result, *_ = await _run(
        PrivateHome(tmp_path / "private"),
        recorder,
        priority=ProposedPriorityLevel.P2,
    )

    skipped = {
        ApplicationPreparationStage.RESUME_TAILORING,
        ApplicationPreparationStage.RESUME_FACT_QA,
        ApplicationPreparationStage.BASE_LATEX_SELECTION,
        ApplicationPreparationStage.LATEX_CONSTRUCTION,
        ApplicationPreparationStage.RESUME_COMPILATION,
        ApplicationPreparationStage.RESUME_VISUAL_QA,
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
    }
    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert result.run is not None
    assert {
        item.stage
        for item in result.run.stage_results
        if item.execution_status is PreparationStageExecutionStatus.SKIPPED
    } == skipped
    assert not skipped.intersection(
        request.stage for request in recorder.requests
    )
    assert ApplicationPreparationStage.RESUME_PUBLICATION in {
        request.stage for request in recorder.requests
    }


async def test_happy_path_is_serial_ordered_and_complete(tmp_path: Path) -> None:
    recorder = _Recorder()
    result, plan, _plan_repository, _run_repository = await _run(
        PrivateHome(tmp_path / "private"), recorder
    )

    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert result.run is not None
    assert [item.stage for item in result.run.stage_results] == list(
        APPLICATION_PREPARATION_STAGE_ORDER
    )
    assert [
        request.stage for request in recorder.requests
    ] == [
        stage
        for stage in APPLICATION_PREPARATION_STAGE_ORDER
        if stage
        is not ApplicationPreparationStage.RESUME_LAYOUT_REVISION
    ]
    assert recorder.max_active == 1
    assert len({request.stage for request in recorder.requests}) == len(
        recorder.requests
    )
    assert all(
        request.subject_id == SUBJECT
        and request.application_plan_id == plan.plan_id
        and request.now == NOW
        for request in recorder.requests
    )
    assert result.run.completed_roles == tuple(
        ApplicationPreparationCompletedRole
    )
    assert result.run.final_plan_material_manifest_id == (
        "resume-cover-manifest-1"
    )
    assert result.run.final_prepared_application_answer_set_id == "answers-1"
    assert result.assembly_lineage is not None
    assert result.assembly_lineage.subject_id == SUBJECT
    assert result.assembly_lineage.application_plan_id == plan.plan_id
    assert result.assembly_lineage.preparation_run_id == result.run.run_id
    assert (
        result.assembly_lineage.plan_material_manifest_id
        == "resume-cover-manifest-1"
    )
    assert (
        result.assembly_lineage.prepared_application_answer_set_id
        == "answers-1"
    )
    assert (
        result.assembly_lineage.preparation_completion_hash
        == result.run.run_content_hash
    )
    assert result.run.started_at == result.run.completed_at == NOW
    assert result.run.stage_results[9].execution_status is (
        PreparationStageExecutionStatus.SKIPPED
    )
    serialized = json.dumps(result.run.to_dict()).casefold()
    assert not any(
        claim in serialized
        for claim in ("gate_a", "submission", "submit_authorized", "ats_state")
    )


async def test_mixed_sync_and_async_stages_invoke_once_in_order(
    tmp_path: Path,
) -> None:
    recorder = _Recorder()
    async_active = 0
    max_async_active = 0

    async def invoke_async(request):
        nonlocal async_active, max_async_active
        async_active += 1
        max_async_active = max(max_async_active, async_active)
        try:
            await asyncio.sleep(0)
            return recorder.invoke(request)
        finally:
            async_active -= 1

    async_stages = {
        stage
        for index, stage in enumerate(APPLICATION_PREPARATION_STAGE_ORDER)
        if index % 2
    }
    recipe = _recipe(
        recorder,
        overrides={stage: invoke_async for stage in async_stages},
    )

    result, *_ = await _run(
        PrivateHome(tmp_path / "private"),
        recorder,
        recipe=recipe,
    )

    expected = [
        stage
        for stage in APPLICATION_PREPARATION_STAGE_ORDER
        if stage is not ApplicationPreparationStage.RESUME_LAYOUT_REVISION
    ]
    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert [request.stage for request in recorder.requests] == expected
    assert len(recorder.requests) == len(set(expected))
    assert max_async_active == 1
    assert recorder.max_active == 1


async def test_async_stage_cancellation_propagates_without_later_calls(
    tmp_path: Path,
) -> None:
    recorder = _Recorder()
    cancelled_stage = ApplicationPreparationStage.RESUME_TAILORING

    async def cancel(_request):
        raise asyncio.CancelledError

    recipe = _recipe(recorder, overrides={cancelled_stage: cancel})

    with pytest.raises(asyncio.CancelledError):
        await _run(
            PrivateHome(tmp_path / "private"),
            recorder,
            recipe=recipe,
        )

    assert [request.stage for request in recorder.requests] == [
        ApplicationPreparationStage.BASE_RESUME_SELECTION,
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
        ApplicationPreparationStage.RESUME_EVIDENCE,
    ]


async def test_full_pipeline_accepts_only_typed_new_stage_results(
    tmp_path: Path,
) -> None:
    class _TypedRecorder(_Recorder):
        def invoke(self, request):
            self.requests.append(request)
            directive = (
                PublicStageDirective.PASSED
                if request.stage
                is ApplicationPreparationStage.RESUME_VISUAL_QA
                else PublicStageDirective.CONTINUE
            )
            return PublicPreparationStageResult.completed(
                stage=request.stage,
                result_id=f"result-{request.stage.value.lower()}",
                result_content_hash=_hash(request.stage.value),
                outputs=OUTPUTS[request.stage],
                directive=directive,
            )

    recorder = _TypedRecorder()
    result, *_ = await _run(PrivateHome(tmp_path / "private"), recorder)

    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert result.run is not None
    assert all(
        not item.is_legacy_untyped
        for item in result.run.stage_results
    )
    assert {
        item.outcome for item in result.run.stage_results
    } == {
        PreparationStageOutcome.COMPLETED,
        PreparationStageOutcome.SKIPPED,
    }


async def test_created_and_unchanged_stage_results_both_continue(
    tmp_path: Path,
) -> None:
    statuses = {
        stage: (
            PublicStageStatus.UNCHANGED
            if index % 2
            else PublicStageStatus.CREATED
        )
        for index, stage in enumerate(APPLICATION_PREPARATION_STAGE_ORDER)
    }
    recorder = _Recorder(statuses=statuses)
    result, _plan_value, _plans, _runs = await _run(
        PrivateHome(tmp_path / "private"), recorder
    )

    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert result.run is not None
    assert {
        item.execution_status
        for item in result.run.stage_results
        if item.execution_status
        is not PreparationStageExecutionStatus.SKIPPED
    } == {
        PreparationStageExecutionStatus.CREATED,
        PreparationStageExecutionStatus.UNCHANGED,
    }


async def test_visual_pass_skips_layout_revision(tmp_path: Path) -> None:
    recorder = _Recorder(
        visual_directive=PublicStageDirective.PASSED
    )
    result, *_ = await _run(PrivateHome(tmp_path / "private"), recorder)

    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert not any(
        request.stage
        is ApplicationPreparationStage.RESUME_LAYOUT_REVISION
        for request in recorder.requests
    )


async def test_revision_required_uses_final_lineage_for_publication(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(
        visual_directive=PublicStageDirective.REVISION_REQUIRED
    )
    result, *_ = await _run(PrivateHome(tmp_path / "private"), recorder)

    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert any(
        request.stage
        is ApplicationPreparationStage.RESUME_LAYOUT_REVISION
        for request in recorder.requests
    )
    publication = next(
        request
        for request in recorder.requests
        if request.stage
        is ApplicationPreparationStage.RESUME_PUBLICATION
    )
    assert publication.output("latex_version_id") == (
        "latex-version-revised"
    )
    assert publication.output("compilation_record_id") == (
        "compilation-revised"
    )
    assert publication.output("visual_qa_result_id") == (
        "visual-qa-revised"
    )


@pytest.mark.parametrize(
    "stage",
    (
        ApplicationPreparationStage.BASE_RESUME_SELECTION,
        ApplicationPreparationStage.RESUME_FACT_QA,
        ApplicationPreparationStage.RESUME_COMPILATION,
        ApplicationPreparationStage.RESUME_VISUAL_QA,
    ),
)
async def test_resume_defer_stops_before_cover_and_answers(
    tmp_path: Path, stage: ApplicationPreparationStage
) -> None:
    recorder = _Recorder(
        statuses={stage: PublicStageStatus.DEFERRED}
    )
    result, *_ = await _run(PrivateHome(tmp_path / stage.value), recorder)

    assert result.status is ApplicationPreparationStatus.DEFERRED
    assert result.run is not None
    assert result.run.deferred_stage is stage
    assert recorder.requests[-1].stage is stage
    assert not any(
        request.stage
        in {
            ApplicationPreparationStage.COVER_LETTER_EVIDENCE,
            ApplicationPreparationStage.APPLICATION_ANSWERS,
        }
        for request in recorder.requests
    )


async def test_cover_letter_defer_preserves_completed_resume_role(
    tmp_path: Path,
) -> None:
    stage = ApplicationPreparationStage.COVER_LETTER_DRAFT
    recorder = _Recorder(
        statuses={stage: PublicStageStatus.DEFERRED}
    )
    result, *_ = await _run(PrivateHome(tmp_path / "private"), recorder)

    assert result.status is ApplicationPreparationStatus.DEFERRED
    assert result.run is not None
    assert result.run.completed_roles == (
        ApplicationPreparationCompletedRole.RESUME,
    )
    assert result.run.final_plan_material_manifest_id == "resume-manifest-1"
    assert recorder.requests[-1].stage is stage


async def test_blocking_answers_mark_human_attention_but_complete(
    tmp_path: Path,
) -> None:
    recorder = _Recorder(human_attention=True)
    result, *_ = await _run(PrivateHome(tmp_path / "private"), recorder)

    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert result.run is not None
    assert result.run.human_attention_required is True


async def test_public_failure_stops_without_rollback(tmp_path: Path) -> None:
    stage = ApplicationPreparationStage.COVER_LETTER_FACT_QA
    recorder = _Recorder(
        statuses={stage: PublicStageStatus.FAILED}
    )

    async def fail(request):
        await asyncio.sleep(0)
        return recorder.invoke(request)

    result, *_ = await _run(
        PrivateHome(tmp_path / "private"),
        recorder,
        recipe=_recipe(recorder, overrides={stage: fail}),
    )

    assert result.status is ApplicationPreparationStatus.FAILED
    assert result.run is not None
    assert result.run.failed_stage is stage
    assert result.run.completed_roles == (
        ApplicationPreparationCompletedRole.RESUME,
    )
    assert recorder.requests[-1].stage is stage


async def test_public_exception_becomes_persisted_failed_run(
    tmp_path: Path,
) -> None:
    recorder = _Recorder()

    async def explode(_request):
        await asyncio.sleep(0)
        raise RuntimeError("synthetic")

    recipe = _recipe(
        recorder,
        overrides={
            ApplicationPreparationStage.RESUME_TAILORING: explode
        },
    )
    result, _plan_value, _plans, repository = await _run(
        PrivateHome(tmp_path / "private"),
        recorder,
        recipe=recipe,
    )

    assert result.status is ApplicationPreparationStatus.FAILED
    assert result.run is not None
    assert result.run.failed_stage is (
        ApplicationPreparationStage.RESUME_TAILORING
    )
    assert not result.run.stage_results[-1].is_legacy_untyped
    assert result.run.stage_results[-1].stop_reason is not None
    assert repository.get(
        subject_id=SUBJECT, run_id=result.run.run_id
    ).status is ApplicationPreparationRunReadStatus.FOUND


async def test_completed_replay_is_unchanged_with_zero_slice_calls(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    first_recorder = _Recorder()
    first, plan, plans, runs = await _run(home, first_recorder)
    second_recorder = _Recorder()
    second = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(days=1),
        ),
        application_plan_repository=plans,
        recipe=_recipe(second_recorder),
        run_repository=runs,
    )

    assert first.status is ApplicationPreparationStatus.COMPLETED
    assert second.status is ApplicationPreparationStatus.UNCHANGED
    assert second.run is not None and first.run is not None
    assert second.run.run_id == first.run.run_id
    assert second.run.completed_at == first.run.completed_at
    assert second.assembly_lineage == first.assembly_lineage
    assert second_recorder.requests == []


@pytest.mark.parametrize(
    "terminal_status",
    (PublicStageStatus.DEFERRED, PublicStageStatus.FAILED),
)
async def test_terminal_replay_does_not_retry_slices(
    tmp_path: Path,
    terminal_status: PublicStageStatus,
) -> None:
    home = PrivateHome(tmp_path / terminal_status.value.lower())
    stage = ApplicationPreparationStage.RESUME_TAILORING
    first_recorder = _Recorder(statuses={stage: terminal_status})
    first, plan, plans, runs = await _run(home, first_recorder)
    replay_recorder = _Recorder()

    replay = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(days=1),
        ),
        application_plan_repository=plans,
        recipe=_recipe(replay_recorder),
        run_repository=runs,
    )

    assert replay.status is ApplicationPreparationStatus(
        terminal_status.value
    )
    assert replay.run is not None and first.run is not None
    assert replay.run.run_id == first.run.run_id
    assert replay.retryable is False
    assert replay_recorder.requests == []


async def test_changed_input_binding_creates_new_run_and_reinvokes_slices(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    first_recorder = _Recorder()
    first, plan, plans, runs = await _run(home, first_recorder)
    second_recorder = _Recorder()
    second = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(minutes=1),
        ),
        application_plan_repository=plans,
        recipe=_recipe(
            second_recorder, input_binding="upstream-binding-v2"
        ),
        run_repository=runs,
    )

    assert first.run is not None and second.run is not None
    assert second.status is ApplicationPreparationStatus.COMPLETED
    assert second.run.run_id != first.run.run_id
    assert second_recorder.requests
    assert len(
        tuple(home.paths.application_preparation_runs.rglob("*.json"))
    ) == 2


async def test_changed_fact_snapshot_creates_new_preparation_run(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private-fact-snapshot")
    first_recorder = _Recorder()
    plan, plans = _plan(home)
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    first = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW,
            input_snapshot_hash="a" * 64,
        ),
        application_plan_repository=plans,
        recipe=_recipe(first_recorder),
        run_repository=runs,
    )
    second_recorder = _Recorder()
    second = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(minutes=1),
            input_snapshot_hash="b" * 64,
        ),
        application_plan_repository=plans,
        recipe=_recipe(second_recorder),
        run_repository=runs,
    )

    assert first.run is not None and second.run is not None
    assert second.status is ApplicationPreparationStatus.COMPLETED
    assert second.run.run_id != first.run.run_id
    assert second_recorder.requests


async def test_stage_lineage_and_run_are_immutable_and_restart_stable(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    result, plan, _plans, _runs = await _run(home, _Recorder())
    assert result.run is not None
    with pytest.raises(FrozenInstanceError):
        result.run.job_id = "changed"

    restarted = PrivateHomeApplicationPreparationRunRepository(home)
    current = restarted.find_current_for_plan(
        subject_id=SUBJECT, application_plan_id=plan.plan_id
    )
    assert current.status is ApplicationPreparationRunReadStatus.FOUND
    assert current.run == result.run
    assert current.run.run_content_hash == result.run.run_content_hash


async def test_corrupt_run_fails_closed_without_overwrite(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    result, plan, plans, runs = await _run(home, _Recorder())
    assert result.run is not None
    artifact = next(home.paths.application_preparation_runs.rglob("*.json"))
    artifact.write_text("{broken", encoding="utf-8")
    replay_recorder = _Recorder()
    replay = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(days=1),
        ),
        application_plan_repository=plans,
        recipe=_recipe(replay_recorder),
        run_repository=runs,
    )

    assert replay.status is ApplicationPreparationStatus.FAILED
    assert replay.reason_code is (
        ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE
    )
    assert replay_recorder.requests == []
    assert artifact.read_text(encoding="utf-8") == "{broken"


async def test_subject_ownership_and_repository_isolation(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    result, plan, plans, runs = await _run(home, _Recorder())
    assert result.run is not None
    mismatch = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=OTHER_SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=plans,
        recipe=_recipe(_Recorder()),
        run_repository=runs,
    )

    assert mismatch.status is ApplicationPreparationStatus.FAILED
    assert mismatch.reason_code is (
        ApplicationPreparationFailureReason
        .APPLICATION_PLAN_SUBJECT_MISMATCH
    )
    assert runs.get(
        subject_id=OTHER_SUBJECT, run_id=result.run.run_id
    ).status is ApplicationPreparationRunReadStatus.NOT_FOUND


async def test_missing_required_output_fails_contract_and_stops(
    tmp_path: Path,
) -> None:
    recorder = _Recorder()

    def malformed(request):
        return PublicPreparationStageResult.legacy_success(
            stage=request.stage,
            status=PublicStageStatus.CREATED,
            public_status="CREATED",
            result_id="result-malformed",
            result_content_hash=_hash("malformed"),
            outputs={"wrong_id": "wrong"},
        )

    recipe = _recipe(
        recorder,
        overrides={
            ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: (
                malformed
            )
        },
    )
    result, *_ = await _run(
        PrivateHome(tmp_path / "private"), recorder, recipe=recipe
    )

    assert result.status is ApplicationPreparationStatus.FAILED
    assert result.reason_code is (
        ApplicationPreparationFailureReason
        .PUBLIC_STAGE_CONTRACT_FAILURE
    )
    assert result.run is not None
    assert not result.run.stage_results[-1].is_legacy_untyped
    assert result.run.stage_results[-1].stop_reason is not None
    assert [item.stage for item in recorder.requests] == [
        ApplicationPreparationStage.BASE_RESUME_SELECTION
    ]


def _write_vault(home: PrivateHome) -> None:
    paths = home.ensure()
    paths.profile_facts.write_text(
        json.dumps(
            {
                "normalized": {},
                "schema_version": 1,
                "subject_id": SUBJECT,
            }
        ),
        encoding="utf-8",
    )
    paths.verified_answers.write_text(
        json.dumps(
            {
                "answers": {
                    "email": {
                        "confirmed_at": NOW.isoformat(),
                        "fact_id": "fact-email",
                        "recorded_at": (
                            NOW - timedelta(days=1)
                        ).isoformat(),
                        "scope": {},
                        "sensitivity": "BASIC",
                        "source": "synthetic",
                        "source_classification": "VERIFIED_FACT",
                        "source_record_id": "record-email",
                        "value": "synthetic@example.test",
                        "verified": True,
                    }
                },
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    paths.policy.write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )


async def test_real_public_application_answers_slice_composes_at_final_stage(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    _write_vault(home)
    plan, plans = _plan(home)
    recorder = _Recorder()
    answer_repository = PrivateHomePreparedApplicationAnswerSetRepository(
        home
    )

    def real_answers(request):
        typed = prepare_application_answers(
            PrepareApplicationAnswersCommand(
                subject_id=request.subject_id,
                application_plan_id=request.application_plan_id,
                now=request.now,
            ),
            application_plan_repository=plans,
            fact_provider=PrivateHomeApplicationFactProvider(home),
            answer_policy=ApplicationAnswerPolicy.default(),
            answer_set_repository=answer_repository,
        )
        assert typed.status is (
            PreparedApplicationAnswerSetStatus.CREATED
        )
        assert typed.answer_set is not None
        answer_set = typed.answer_set
        return PublicPreparationStageResult.legacy_success(
            stage=request.stage,
            status=PublicStageStatus.CREATED,
            public_status=typed.status.value,
            result_id=answer_set.answer_set_id,
            result_content_hash=answer_set.answer_set_content_hash,
            outputs={
                "prepared_application_answer_set_id": (
                    answer_set.answer_set_id
                )
            },
            human_attention_required=any(
                item.blocking for item in answer_set.unresolved_items
            ),
        )

    recipe = _recipe(
        recorder,
        overrides={
            ApplicationPreparationStage.APPLICATION_ANSWERS: real_answers
        },
    )
    result = await run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=plans,
        recipe=recipe,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
    )

    assert result.status is ApplicationPreparationStatus.COMPLETED
    assert result.run is not None
    assert result.run.final_prepared_application_answer_set_id.startswith(
        "prepared-application-answer-set-"
    )
    assert result.run.human_attention_required is False


async def test_source_has_no_slice_private_repository_or_execution_imports() -> None:
    tree = ast.parse(
        Path(orchestrator_module.__file__).read_text(encoding="utf-8")
    )
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "inspect",
        "json",
        "pathlib",
        "re",
        "threading",
        "typing",
        "application_plan",
        "private_home",
        "preparation_invocation",
        "uuid",
    }
    source = Path(orchestrator_module.__file__).read_text(encoding="utf-8")
    assert not any(
        forbidden in source
        for forbidden in (
            "SemanticMapper",
            "CandidateVault",
            "ApplicationEngine",
            "Browser",
            "submit(",
            "compiler.compile",
            "renderer.render",
        )
    )
