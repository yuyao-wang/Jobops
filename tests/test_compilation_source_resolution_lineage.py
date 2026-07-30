"""Focused P2b4e2a invocation and compilation source-lineage tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import core.application_preparation_orchestrator as preparation
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    ApplicationPreparationStageResult,
    ResolvedCompilationSourceLineage,
    RunApplicationPreparationCommand,
    UnresolvedCompilationSourceLineage,
    UnresolvedCompilationSourceState,
    run_application_preparation,
)
from core.latex_compiler import LatexCompileOutcome, LatexCompileStatus
from core.preparation_invocation import PreparationInvocationBinding
from core.resume_compilation import (
    CompileResumeLatexCommand,
    CompileResumeLatexResult,
    ResumeCompilationFailureReason,
    ResumeCompilationStatus,
    compile_resume_latex,
    resume_compilation_public_result,
)
from core.resume_compilation_stopped_source import (
    PrivateHomeResumeCompilationStoppedSourceRepository,
)
from core.resume_latex_versions import (
    ResumeLatexVersionReadResult,
    ResumeLatexVersionReadStatus,
)
from tests.test_application_preparation_orchestrator import (
    NOW,
    SUBJECT,
    _Recorder,
    _plan,
    _recipe,
)
from tests.test_resume_compilation import (
    LATEX,
    _FakeCompiler,
    _compile,
    _setup,
)


def _binding(*, plan_id: str, invocation_id: str) -> PreparationInvocationBinding:
    return PreparationInvocationBinding.create(
        subject_id="subject-a",
        application_plan_id=plan_id,
        invocation_id=invocation_id,
        orchestration_contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        created_at=NOW,
    )


async def test_pre_run_binding_is_shared_by_every_stage_and_final_run(
    tmp_path,
) -> None:
    home = preparation.PrivateHome(tmp_path / "private")
    recorder = _Recorder()
    plan, plan_repository = _plan(home)
    invocation_id = "explicit-preparation-invocation-001"
    run_repository = (
        preparation.PrivateHomeApplicationPreparationRunRepository(home)
    )
    command = RunApplicationPreparationCommand(
        subject_id=SUBJECT,
        application_plan_id=plan.plan_id,
        now=NOW,
        invocation_id=invocation_id,
    )

    result = await run_application_preparation(
        command,
        application_plan_repository=plan_repository,
        recipe=_recipe(recorder),
        run_repository=run_repository,
    )

    assert result.run is not None
    binding = result.run.preparation_invocation_binding
    assert binding is not None
    assert binding.invocation_id == invocation_id
    assert all(
        request.preparation_invocation_binding == binding
        for request in recorder.requests
    )
    assert all(
        stage.preparation_invocation_ref == binding.reference
        for stage in result.run.stage_results
    )
    replayed_binding = PreparationInvocationBinding.create(
        subject_id=SUBJECT,
        application_plan_id=plan.plan_id,
        invocation_id=invocation_id,
        orchestration_contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        created_at=NOW + timedelta(hours=1),
    )
    assert replayed_binding.binding_id == binding.binding_id
    assert "run_id" not in binding.to_dict()
    assert "stage_hashes" not in binding.to_dict()
    call_count = len(recorder.requests)
    replay = await run_application_preparation(
        command,
        application_plan_repository=plan_repository,
        recipe=_recipe(recorder),
        run_repository=run_repository,
    )
    assert replay.run == result.run
    assert len(recorder.requests) == call_count


async def test_resolved_content_stops_bind_exact_source_and_distinct_reason(
    tmp_path,
) -> None:
    unmanaged = _setup(
        tmp_path / "unmanaged",
        source=LATEX.replace(
            "\\end{document}",
            "\\input{extra.tex}\n\\end{document}",
        ),
    )
    compilation_error = _setup(tmp_path / "compile-error")
    error_compiler = _FakeCompiler(
        LatexCompileOutcome(
            status=LatexCompileStatus.COMPILATION_ERROR,
            pdf_bytes=None,
            diagnostics="synthetic bounded diagnostic",
            exit_code=1,
            compiler_started=True,
        )
    )

    results = (
        _compile(unmanaged, _FakeCompiler()),
        _compile(compilation_error, error_compiler),
    )
    expected_reasons = (
        ResumeCompilationFailureReason.UNMANAGED_DEPENDENCY,
        ResumeCompilationFailureReason.COMPILATION_ERROR,
    )
    for index, (parts, result, reason) in enumerate(
        zip((unmanaged, compilation_error), results, expected_reasons),
        start=1,
    ):
        plan_id = parts["record"].application_plan_id
        invocation = _binding(
            plan_id=plan_id, invocation_id=f"resolved-source-{index}"
        )
        public = resume_compilation_public_result(
            result,
            preparation_invocation_binding=invocation,
            application_plan_id=plan_id,
            stopped_source_repository=(
                PrivateHomeResumeCompilationStoppedSourceRepository(
                    parts["home"]
                )
            ),
        )
        lineage = public.compilation_source_lineage
        assert isinstance(lineage, ResolvedCompilationSourceLineage)
        assert public.stop_reason is not None
        assert public.stop_reason.code.value == reason.value
        assert lineage.construction_result_id == parts["record"].record_id
        assert lineage.latex_version_id == parts["version"].latex_version_id
        assert lineage.source_content_hash == parts["version"].source_sha256
        assert lineage.application_plan_id == plan_id
        assert (
            resume_compilation_public_result(
                result,
                preparation_invocation_binding=invocation,
                application_plan_id=plan_id,
                stopped_source_repository=(
                    PrivateHomeResumeCompilationStoppedSourceRepository(
                        parts["home"]
                    )
                ),
            )
            == public
        )


async def test_early_stops_use_closed_unresolved_lineage_without_source_hash(
    tmp_path,
) -> None:
    parts = _setup(tmp_path)
    plan_id = parts["record"].application_plan_id
    invocation = _binding(
        plan_id=plan_id, invocation_id="unresolved-source-branches"
    )
    invalid = _compile(
        parts,
        _FakeCompiler(),
        now=datetime(2026, 7, 29, 15, 0),
    )
    missing_construction = compile_resume_latex(
        CompileResumeLatexCommand(
            subject_id="subject-a",
            resume_latex_construction_record_id=(
                "resume-latex-construction-" + "9" * 64
            ),
            resume_latex_version_id=parts["version"].latex_version_id,
            now=NOW,
        ),
        construction_repository=parts["construction_repository"],
        latex_version_repository=parts["latex_repository"],
        compiler=_FakeCompiler(),
        compilation_repository=parts["compilation_repository"],
        home=parts["home"],
    )

    class _MissingVersionRepository:
        def get(self, *, subject_id, latex_version_id):
            return ResumeLatexVersionReadResult(
                status=ResumeLatexVersionReadStatus.NOT_FOUND,
                version=None,
            )

    missing_version = compile_resume_latex(
        CompileResumeLatexCommand(
            subject_id="subject-a",
            resume_latex_construction_record_id=parts["record"].record_id,
            resume_latex_version_id=parts["version"].latex_version_id,
            now=NOW,
        ),
        construction_repository=parts["construction_repository"],
        latex_version_repository=_MissingVersionRepository(),
        compiler=_FakeCompiler(),
        compilation_repository=parts["compilation_repository"],
        home=parts["home"],
    )
    expected = (
        (invalid, UnresolvedCompilationSourceState.INVALID_REQUEST),
        (
            missing_construction,
            UnresolvedCompilationSourceState.CONSTRUCTION_NOT_FOUND,
        ),
        (
            missing_version,
            UnresolvedCompilationSourceState.LATEX_VERSION_NOT_FOUND,
        ),
    )
    for attempt_number, (result, state) in enumerate(expected, start=1):
        public = resume_compilation_public_result(
            result,
            preparation_invocation_binding=invocation,
            application_plan_id=plan_id,
            attempt_number=attempt_number,
            stopped_source_repository=(
                PrivateHomeResumeCompilationStoppedSourceRepository(
                    parts["home"]
                )
            ),
        )
        lineage = public.compilation_source_lineage
        assert isinstance(lineage, UnresolvedCompilationSourceLineage)
        assert lineage.resolution_state is state
        serialized = lineage.to_dict()
        assert "source_content_hash" not in serialized
        assert "construction_result_id" not in serialized


async def test_binding_mismatch_and_resolution_state_conflicts_fail_closed(
    tmp_path,
) -> None:
    parts = _setup(tmp_path)
    stopped = _compile(
        parts,
        _FakeCompiler(
            LatexCompileOutcome(
                status=LatexCompileStatus.COMPILATION_ERROR,
                pdf_bytes=None,
                diagnostics="bounded",
                exit_code=1,
                compiler_started=True,
            )
        ),
    )
    plan_id = parts["record"].application_plan_id
    invocation = _binding(plan_id=plan_id, invocation_id="binding-check")
    with pytest.raises(ValueError):
        resume_compilation_public_result(
            stopped,
            preparation_invocation_binding=invocation,
            application_plan_id="different-plan",
        )
    with pytest.raises(ValueError):
        resume_compilation_public_result(
            stopped,
            preparation_invocation_binding=invocation,
            application_plan_id=plan_id,
            attempt_number=0,
        )
    with pytest.raises(ValueError):
        CompileResumeLatexResult(
            status=ResumeCompilationStatus.FAILED,
            subject_id="subject-a",
            compilation_binding="",
            record=None,
            write_result=None,
            reason_code=ResumeCompilationFailureReason.INVALID_REQUEST,
            compiler_started=False,
            diagnostics="",
            retryable=False,
            message="invalid",
            unresolved_source_state=(
                UnresolvedCompilationSourceState.LATEX_VERSION_NOT_FOUND
            ),
        )

    public = resume_compilation_public_result(
        stopped,
        preparation_invocation_binding=invocation,
        application_plan_id=plan_id,
        stopped_source_repository=(
            PrivateHomeResumeCompilationStoppedSourceRepository(parts["home"])
        ),
    )
    persisted = ApplicationPreparationStageResult.from_public(
        public,
        preparation_invocation_ref=invocation.reference,
    )
    restored = preparation._stage_result_from_dict(
        persisted.to_dict(),
        run_contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
    )
    assert restored == persisted

    old_reason = preparation.PreparationStopReasonEnvelope(
        stage=preparation.ApplicationPreparationStage.BASE_RESUME_SELECTION,
        code=preparation.BaseResumeSelectionStopReason.NO_SELECTABLE_RESUME,
        contract_version=(
            preparation.BASE_RESUME_SELECTION_STOP_REASON_CONTRACT_VERSION
        ),
        outcome=preparation.PreparationStageOutcome.DEFERRED,
    )
    old_stage_content = {
        "execution_status": "DEFERRED",
        "human_attention_required": True,
        "legacy_public_status": None,
        "legacy_reason_code": None,
        "outcome": "DEFERRED",
        "outputs": [],
        "result_content_hash": None,
        "result_id": None,
        "retryable": False,
        "schema_version": (
            preparation.PREVIOUS_PREPARATION_STAGE_RESULT_SCHEMA_VERSION
        ),
        "stage": "BASE_RESUME_SELECTION",
        "stop_reason": old_reason.to_dict(),
    }
    old_stage = {
        **old_stage_content,
        "stage_content_hash": preparation._canonical_hash(
            old_stage_content
        ),
    }
    old_identity = {
        "application_plan_id": "application-plan-historical-v2",
        "completed_roles": [],
        "contract_version": (
            preparation
            .PREVIOUS_APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        "deferred_reason": "NO_SELECTABLE_RESUME",
        "deferred_stage": "BASE_RESUME_SELECTION",
        "failed_reason": None,
        "failed_stage": None,
        "final_plan_material_manifest_id": None,
        "final_prepared_application_answer_set_id": None,
        "human_attention_required": True,
        "job_content_hash": "1" * 64,
        "job_id": "job-historical-v2",
        "job_revision": 1,
        "overall_status": "DEFERRED",
        "preparation_binding": "2" * 64,
        "recipe_metadata_hash": "3" * 64,
        "required_material_policy_hash": "4" * 64,
        "required_material_policy_id": "required-application-materials-v1",
        "required_material_policy_version": (
            "required-application-materials-v1"
        ),
        "stage_hashes": [old_stage["stage_content_hash"]],
        "subject_id": "subject-a",
    }
    old_run_id = (
        "application-preparation-run-"
        + preparation._canonical_hash(old_identity)
    )
    old_content = {
        "application_plan_id": old_identity["application_plan_id"],
        "completed_at": preparation._rfc3339(NOW),
        "completed_roles": [],
        "contract_version": old_identity["contract_version"],
        "deferred_reason": old_identity["deferred_reason"],
        "deferred_stage": old_identity["deferred_stage"],
        "failed_reason": None,
        "failed_stage": None,
        "final_plan_material_manifest_id": None,
        "final_prepared_application_answer_set_id": None,
        "human_attention_required": True,
        "job_content_hash": old_identity["job_content_hash"],
        "job_id": old_identity["job_id"],
        "job_revision": 1,
        "overall_status": "DEFERRED",
        "preparation_binding": old_identity["preparation_binding"],
        "recipe_metadata_hash": old_identity["recipe_metadata_hash"],
        "required_material_policy_hash": (
            old_identity["required_material_policy_hash"]
        ),
        "required_material_policy_id": (
            old_identity["required_material_policy_id"]
        ),
        "required_material_policy_version": (
            old_identity["required_material_policy_version"]
        ),
        "run_id": old_run_id,
        "stage_results": [old_stage],
        "started_at": preparation._rfc3339(NOW),
        "subject_id": "subject-a",
    }
    old_run = preparation._run_from_dict(
        {
            **old_content,
            "run_content_hash": preparation._canonical_hash(old_content),
        }
    )
    assert old_run.preparation_invocation_binding is None
    assert old_run.to_dict() == {
        **old_content,
        "run_content_hash": preparation._canonical_hash(old_content),
    }
