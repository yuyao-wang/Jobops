"""Focused S3g4b LaTeX Compilation correction tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application_answers import (
    PrivateHomePreparedApplicationAnswerSetRepository,
)
from core.application_preparation_orchestrator import (
    ApplicationPreparationStage,
    ApplicationPreparationStatus,
    LatexCompilationStopReason,
    PrivateHomeApplicationPreparationRunRepository,
    RunApplicationPreparationResult,
)
from core.human_attention_queue import (
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
    build_current_human_attention_queue,
)
from core.latex_compilation_correction import (
    LatexCompilationCorrectionAction,
    LatexCompilationCorrectionCommand,
    LatexCompilationCorrectionDirectiveProvider,
    LatexCompilationCorrectionMode,
    LatexCompilationCorrectionReceiptRepository,
    LatexCompilationCorrectionStatus,
    PrivateHomeLatexCompilationCorrectionDirectiveRepository,
    resolve_latex_compilation_correction,
)
from core.latex_compiler import LatexCompileOutcome, LatexCompileStatus
from core.resume_compilation import (
    PrivateHomeResumeCompilationRepository,
    resume_compilation_public_result,
)
from core.resume_compilation_stopped_source import (
    PrivateHomeResumeCompilationStoppedSourceRepository,
    RepositoryResumeCompilationStoppedSourceProvider,
)
from core.resume_latex_construction import (
    ConstructResumeLatexCommand,
    ResumeLatexConstructionStatus,
    construct_resume_latex_version,
    unmanaged_file_dependencies,
)
from tests.test_application_preparation_orchestrator import (
    _Recorder,
    _recipe,
)
from tests.test_human_attention_queue import NOW, _invoke
from tests.test_material_correction_target import (
    _MissingFindingProvider,
    _target_provider,
)
from tests.test_resume_compilation import (
    _FakeCompiler,
    _compile as _compile_resume,
)
from tests.test_resume_latex_construction import (
    METADATA,
    _FakeConstructionAgent,
    _add_base_version,
    _base_decision,
    _construct,
    _marked_base_source,
    _setup as _construction_setup,
)


class _QueueReader:
    def __init__(self, queue) -> None:
        self.queue = queue
        self.calls = 0

    def __call__(self, **_kwargs):
        self.calls += 1
        return self.queue


def _preparation(status: ApplicationPreparationStatus):
    result = object.__new__(RunApplicationPreparationResult)
    object.__setattr__(result, "status", status)
    object.__setattr__(
        result, "run", SimpleNamespace(run_id=f"run-{status.value.lower()}")
    )
    object.__setattr__(result, "reason_code", None)
    return result


async def _current_compilation_item(tmp_path, *, compilation_error=False):
    parts = await _construction_setup(tmp_path / "construction")
    source = _marked_base_source(parts)
    if not compilation_error:
        source = source.replace(
            "\\end{document}",
            "\\input{external.tex}\n\\end{document}",
        )
    _add_base_version(parts, source)
    decision = await _base_decision(parts)
    construction = await _construct(parts, decision)
    assert construction.status is ResumeLatexConstructionStatus.CREATED

    compile_parts = {
        "home": parts["home"],
        "version": construction.version,
        "record": construction.record,
        "latex_repository": parts["latex_repository"],
        "construction_repository": parts["construction_repository"],
        "compilation_repository": PrivateHomeResumeCompilationRepository(
            parts["home"]
        ),
    }
    compiler = (
        _FakeCompiler(
            LatexCompileOutcome(
                status=LatexCompileStatus.COMPILATION_ERROR,
                pdf_bytes=None,
                diagnostics="bounded synthetic diagnostic",
                exit_code=1,
                compiler_started=True,
            )
        )
        if compilation_error
        else _FakeCompiler()
    )
    stopped = _compile_resume(compile_parts, compiler)
    stopped_repository = (
        PrivateHomeResumeCompilationStoppedSourceRepository(parts["home"])
    )

    def compilation_stage(request):
        return resume_compilation_public_result(
            stopped,
            preparation_invocation_binding=(
                request.preparation_invocation_binding
            ),
            application_plan_id=parts["plan"].plan_id,
            stopped_source_repository=stopped_repository,
        )

    runs = PrivateHomeApplicationPreparationRunRepository(parts["home"])
    _invoke(
        plan=parts["plan"],
        plan_repository=parts["plan_repository"],
        run_repository=runs,
        recipe=_recipe(
            _Recorder(),
            input_binding=(
                "compile-error" if compilation_error else "dependency-error"
            ),
            overrides={
                ApplicationPreparationStage.RESUME_COMPILATION: (
                    compilation_stage
                )
            },
        ),
    )
    projector = _target_provider(
        parts["home"],
        stopped_provider=RepositoryResumeCompilationStoppedSourceProvider(
            stopped_repository
        ),
    )
    queue = build_current_human_attention_queue(
        subject_id=parts["plan"].subject_id,
        now=NOW,
        run_repository=runs,
        application_plan_repository=parts["plan_repository"],
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(parts["home"])
        ),
        fact_qa_finding_provider=_MissingFindingProvider(),
        material_correction_target_projector=projector,
    )
    assert queue.status is HumanAttentionQueueStatus.SUCCEEDED
    assert queue.item_count == 1
    return parts, decision, construction, queue, projector


@pytest.mark.asyncio
async def test_unmanaged_dependency_regenerates_new_managed_construction_once(
    tmp_path,
) -> None:
    parts, decision, failed, queue, targets = (
        await _current_compilation_item(tmp_path)
    )
    directives = (
        PrivateHomeLatexCompilationCorrectionDirectiveRepository(
            parts["home"]
        )
    )
    preparation_calls = []
    queue_reader = _QueueReader(queue)
    result = await resolve_latex_compilation_correction(
        LatexCompilationCorrectionCommand(
            subject_id=parts["plan"].subject_id,
            attention_item_id=queue.items[0].item_id,
            action=(
                LatexCompilationCorrectionAction.REGENERATE_AND_RETRY
            ),
            now=NOW,
        ),
        queue_reader=queue_reader,
        target_provider=targets,
        directive_repository=directives,
        preparation_callable=lambda command: (
            preparation_calls.append(command)
            or _preparation(ApplicationPreparationStatus.COMPLETED)
        ),
        receipt_repository=LatexCompilationCorrectionReceiptRepository(
            parts["home"]
        ),
    )

    assert result.status is (
        LatexCompilationCorrectionStatus
        .CORRECTED_AND_PREPARATION_COMPLETED
    )
    assert queue_reader.calls == 1
    assert len(preparation_calls) == 1
    directive = directives.get_current(
        subject_id=parts["plan"].subject_id,
        application_plan_id=parts["plan"].plan_id,
    )
    assert directive.mode is (
        LatexCompilationCorrectionMode
        .REGENERATE_WITH_MANAGED_DEPENDENCIES
    )

    agent = _FakeConstructionAgent()
    corrected = await construct_resume_latex_version(
        ConstructResumeLatexCommand(
            subject_id=parts["plan"].subject_id,
            application_plan_id=parts["plan"].plan_id,
            base_latex_selection_decision_id=decision.decision_id,
            fact_qa_result_id=parts["qa_result"].qa_result_id,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        fact_qa_repository=parts["qa_repository"],
        base_selection_repository=parts["base_repository"],
        latex_version_repository=parts["latex_repository"],
        template_provider=parts["template_provider"],
        agent=agent,
        metadata=METADATA,
        construction_repository=parts["construction_repository"],
        correction_provider=(
            LatexCompilationCorrectionDirectiveProvider(directives)
        ),
        home=parts["home"],
    )
    assert corrected.status is ResumeLatexConstructionStatus.CREATED
    assert corrected.record.record_id != failed.record.record_id
    assert corrected.version.latex_version_id != failed.version.latex_version_id
    assert agent.contexts[0].compilation_correction.mode is (
        LatexCompilationCorrectionMode
        .REGENERATE_WITH_MANAGED_DEPENDENCIES
    )
    source = parts["home"].contained_path(
        corrected.version.source_reference
    ).read_text(encoding="utf-8")
    assert unmanaged_file_dependencies(source) == ()


@pytest.mark.asyncio
async def test_compile_error_mode_and_drift_or_unsupported_target_fail_closed(
    tmp_path,
) -> None:
    parts, _decision, _failed, queue, targets = (
        await _current_compilation_item(
            tmp_path / "compile-error", compilation_error=True
        )
    )
    directives = (
        PrivateHomeLatexCompilationCorrectionDirectiveRepository(
            parts["home"]
        )
    )
    calls = []
    command = LatexCompilationCorrectionCommand(
        subject_id=parts["plan"].subject_id,
        attention_item_id=queue.items[0].item_id,
        action=LatexCompilationCorrectionAction.REGENERATE_AND_RETRY,
        now=NOW,
    )
    created = await resolve_latex_compilation_correction(
        command,
        queue_reader=_QueueReader(queue),
        target_provider=targets,
        directive_repository=directives,
        preparation_callable=lambda value: (
            calls.append(value)
            or _preparation(ApplicationPreparationStatus.COMPLETED)
        ),
        receipt_repository=LatexCompilationCorrectionReceiptRepository(
            parts["home"]
        ),
    )
    directive = directives.get_current(
        subject_id=parts["plan"].subject_id,
        application_plan_id=parts["plan"].plan_id,
    )
    assert created.status is (
        LatexCompilationCorrectionStatus
        .CORRECTED_AND_PREPARATION_COMPLETED
    )
    assert directive.mode is (
        LatexCompilationCorrectionMode.REGENERATE_COMPILABLE_LATEX
    )

    class _Drift:
        def get_current_typed_target(self, **kwargs):
            return targets.get_current_typed_target(**kwargs)

        def get_compilation_stopped_source_for_target(self, **_kwargs):
            raise ValueError("synthetic identity drift")

    drift_home = parts["home"]
    drift_directives = (
        PrivateHomeLatexCompilationCorrectionDirectiveRepository(
            drift_home
        )
    )
    drift = await resolve_latex_compilation_correction(
        command,
        queue_reader=_QueueReader(queue),
        target_provider=_Drift(),
        directive_repository=drift_directives,
        preparation_callable=lambda value: calls.append(value),
        receipt_repository=LatexCompilationCorrectionReceiptRepository(
            drift_home
        ),
    )
    assert drift.status is LatexCompilationCorrectionStatus.TARGET_STALE

    class _Unsupported:
        def get_current_typed_target(self, **_kwargs):
            from core.material_correction_target import (
                MaterialCorrectionTargetStatus,
                MaterialCorrectionTypedTargetResult,
            )

            return MaterialCorrectionTypedTargetResult(
                MaterialCorrectionTargetStatus.AVAILABLE,
                SimpleNamespace(payload=object()),
            )

    unsupported_home = parts["home"]
    unsupported = await resolve_latex_compilation_correction(
        command,
        queue_reader=_QueueReader(queue),
        target_provider=_Unsupported(),
        directive_repository=(
            PrivateHomeLatexCompilationCorrectionDirectiveRepository(
                unsupported_home
            )
        ),
        preparation_callable=lambda value: calls.append(value),
        receipt_repository=LatexCompilationCorrectionReceiptRepository(
            unsupported_home
        ),
    )
    assert unsupported.status is (
        LatexCompilationCorrectionStatus.UNSUPPORTED_TARGET
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_replay_defer_failure_and_disappeared_item_do_not_auto_loop(
    tmp_path,
) -> None:
    parts, _decision, _failed, queue, targets = (
        await _current_compilation_item(tmp_path / "defer")
    )
    directives = (
        PrivateHomeLatexCompilationCorrectionDirectiveRepository(
            parts["home"]
        )
    )
    receipts = LatexCompilationCorrectionReceiptRepository(parts["home"])
    calls = []
    command = LatexCompilationCorrectionCommand(
        subject_id=parts["plan"].subject_id,
        attention_item_id=queue.items[0].item_id,
        action=LatexCompilationCorrectionAction.REGENERATE_AND_RETRY,
        now=NOW,
    )

    def prepare(value):
        calls.append(value)
        return _preparation(ApplicationPreparationStatus.DEFERRED)

    first = await resolve_latex_compilation_correction(
        command,
        queue_reader=_QueueReader(queue),
        target_provider=targets,
        directive_repository=directives,
        preparation_callable=prepare,
        receipt_repository=receipts,
    )
    replay = await resolve_latex_compilation_correction(
        command,
        queue_reader=_QueueReader(queue),
        target_provider=targets,
        directive_repository=directives,
        preparation_callable=prepare,
        receipt_repository=receipts,
    )
    empty = object.__new__(HumanAttentionQueueResult)
    object.__setattr__(
        empty, "status", HumanAttentionQueueStatus.SUCCEEDED
    )
    object.__setattr__(empty, "subject_id", parts["plan"].subject_id)
    object.__setattr__(empty, "items", ())
    disappeared = await resolve_latex_compilation_correction(
        command,
        queue_reader=_QueueReader(empty),
        target_provider=targets,
        directive_repository=directives,
        preparation_callable=prepare,
        receipt_repository=receipts,
    )
    assert first.status is (
        LatexCompilationCorrectionStatus
        .CORRECTED_AND_PREPARATION_DEFERRED
    )
    assert replay.status is LatexCompilationCorrectionStatus.UNCHANGED
    assert disappeared.status is LatexCompilationCorrectionStatus.UNCHANGED
    assert len(calls) == 1

    failed_parts, _, _, failed_queue, failed_targets = (
        await _current_compilation_item(tmp_path / "failed")
    )
    failed_directives = (
        PrivateHomeLatexCompilationCorrectionDirectiveRepository(
            failed_parts["home"]
        )
    )
    failed = await resolve_latex_compilation_correction(
        LatexCompilationCorrectionCommand(
            subject_id=failed_parts["plan"].subject_id,
            attention_item_id=failed_queue.items[0].item_id,
            action=LatexCompilationCorrectionAction.REGENERATE_AND_RETRY,
            now=NOW,
        ),
        queue_reader=_QueueReader(failed_queue),
        target_provider=failed_targets,
        directive_repository=failed_directives,
        preparation_callable=lambda _value: _preparation(
            ApplicationPreparationStatus.FAILED
        ),
        receipt_repository=LatexCompilationCorrectionReceiptRepository(
            failed_parts["home"]
        ),
    )
    assert failed.status is (
        LatexCompilationCorrectionStatus
        .CORRECTION_RECORDED_PREPARATION_FAILED
    )
    assert failed_directives.get_current(
        subject_id=failed_parts["plan"].subject_id,
        application_plan_id=failed_parts["plan"].plan_id,
    ) is not None
