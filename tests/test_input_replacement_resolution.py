"""Focused S3g5 existing-input replacement resolution tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.application_preparation_orchestrator import (
    LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION,
    SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    ApplicationPreparationStatus,
    LatexConstructionStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PrivateHomeApplicationPreparationRunRepository,
    PublicPreparationStageResult,
    RunApplicationPreparationResult,
    SourceResumeProjectionStopReason,
)
from core.input_replacement_resolution import (
    InputReplacementAction,
    InputReplacementResolutionCommand,
    InputReplacementResolutionReceiptRepository,
    InputReplacementResolutionStatus,
    resolve_input_replacement,
)
from core.input_replacement_target import (
    InputReplacementTargetProvider,
    PrivateHomeInputReplacementTargetRepository,
)
from core.plan_scoped_version_override import (
    PLAN_SCOPED_VERSION_OVERRIDE_REPLACEMENT_CONTRACT_VERSION,
    PlanScopedVersionOverrideKind,
    PlanScopedVersionOverrideRepository,
)
from core.private_home import PrivateHome
from core.resume_candidates import PrivateHomeResumeCandidateRepository
from core.resume_latex_versions import (
    PrivateHomeResumeLatexVersionRepository,
)
from tests.test_application_preparation_orchestrator import (
    _Recorder,
    _hash,
    _recipe,
)
from tests.test_human_attention_queue import NOW, SUBJECT, _invoke, _plan
from tests.test_input_replacement_target import _queue
from tests.test_resume_candidates import _pdf, _register as _register_resume
from tests.test_resume_latex_versions import (
    SOURCE,
    _register as _register_latex,
)


def _completed(_command):
    return RunApplicationPreparationResult(
        ApplicationPreparationStatus.COMPLETED,
        None,
        None,
        False,
        "synthetic completed",
    )


def _provider(home, candidates, versions):
    return InputReplacementTargetProvider(
        repository=PrivateHomeInputReplacementTargetRepository(home),
        resume_candidate_provider=candidates,
        latex_version_provider=versions,
    )


def _resume_case(home: PrivateHome):
    candidates, first_write = _register_resume(
        home, subject_id=SUBJECT
    )
    old = first_write.candidate
    _, second_write = _register_resume(
        home,
        subject_id=SUBJECT,
        artifact_path=_pdf(home, name="replacement.pdf", marker=b"two"),
        display_name="Synthetic Replacement Resume",
    )
    replacement = second_write.candidate
    versions = PrivateHomeResumeLatexVersionRepository(home)
    plan, plans = _plan(home, job_id="job-replace-resume")
    runs = PrivateHomeApplicationPreparationRunRepository(home)

    def selected(request):
        return PublicPreparationStageResult.completed(
            stage=request.stage,
            result_id="resume-selection-replacement",
            result_content_hash=_hash("resume-selection-replacement"),
            outputs={
                "resume_artifact_sha256": old.artifact_sha256,
                "resume_id": old.resume_id,
                "resume_selection_decision_id": (
                    "resume-selection-replacement"
                ),
            },
        )

    def stopped(request):
        return PublicPreparationStageResult.deferred(
            stage=request.stage,
            stop_reason=PreparationStopReasonEnvelope(
                stage=request.stage,
                code=SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE,
                contract_version=(
                    SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION
                ),
                outcome=PreparationStageOutcome.DEFERRED,
            ),
        )

    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=_recipe(
            _Recorder(),
            input_binding="replace-resume",
            overrides={
                ApplicationPreparationStage.BASE_RESUME_SELECTION: selected,
                ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: stopped,
            },
        ),
    )
    provider = _provider(home, candidates, versions)
    queue = _queue(home, plans, runs, provider)
    item = queue.items[0]
    provider.current_item_reader = (
        lambda subject, item_id: item
        if subject == SUBJECT and item_id == item.item_id
        else None
    )
    return plan, queue, item, provider, candidates, versions, old, replacement


def _latex_case(home: PrivateHome):
    candidates = PrivateHomeResumeCandidateRepository(home)
    versions = PrivateHomeResumeLatexVersionRepository(home)
    old = _register_latex(
        home,
        subject_id=SUBJECT,
        repository=versions,
        labels=("old",),
    ).version
    replacement = _register_latex(
        home,
        subject_id=SUBJECT,
        repository=versions,
        latex_source=SOURCE + "\n% replacement\n",
        labels=("replacement",),
    ).version
    plan, plans = _plan(home, job_id="job-replace-latex")
    runs = PrivateHomeApplicationPreparationRunRepository(home)

    def selected(request):
        return PublicPreparationStageResult.completed(
            stage=request.stage,
            result_id="latex-selection-replacement",
            result_content_hash=_hash("latex-selection-replacement"),
            outputs={
                "base_latex_selection_id": "latex-selection-replacement",
                "selected_latex_source_sha256": old.source_sha256,
                "selected_latex_version_id": old.latex_version_id,
                "selected_root_family_id": old.root_family_id,
            },
        )

    def stopped(request):
        return PublicPreparationStageResult.deferred(
            stage=request.stage,
            stop_reason=PreparationStopReasonEnvelope(
                stage=request.stage,
                code=LatexConstructionStopReason.BASE_VERSION_UNREADABLE,
                contract_version=(
                    LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION
                ),
                outcome=PreparationStageOutcome.DEFERRED,
            ),
        )

    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=_recipe(
            _Recorder(),
            input_binding="replace-latex",
            overrides={
                ApplicationPreparationStage.BASE_LATEX_SELECTION: selected,
                ApplicationPreparationStage.LATEX_CONSTRUCTION: stopped,
            },
        ),
    )
    provider = _provider(home, candidates, versions)
    queue = _queue(home, plans, runs, provider)
    item = queue.items[0]
    provider.current_item_reader = (
        lambda subject, item_id: item
        if subject == SUBJECT and item_id == item.item_id
        else None
    )
    return plan, queue, item, provider, candidates, versions, old, replacement


def _resolve(
    *,
    home,
    queue,
    item,
    provider,
    candidates,
    versions,
    option_id,
    preparation=_completed,
):
    return asyncio.run(
        resolve_input_replacement(
            InputReplacementResolutionCommand(
                subject_id=SUBJECT,
                attention_item_id=item.item_id,
                action=InputReplacementAction.SELECT_EXISTING_REPLACEMENT,
                replacement_option_id=option_id,
                now=NOW,
            ),
            queue_reader=lambda **_kwargs: queue,
            target_provider=provider,
            resume_candidate_provider=candidates,
            latex_version_provider=versions,
            override_repository=PlanScopedVersionOverrideRepository(home),
            preparation_callable=preparation,
            receipt_repository=InputReplacementResolutionReceiptRepository(
                home
            ),
        )
    )


def test_resume_replacement_reuses_plan_override_and_reruns_once(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "resume")
    home.ensure()
    plan, queue, item, provider, candidates, versions, old, replacement = (
        _resume_case(home)
    )
    calls = []

    result = _resolve(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        versions=versions,
        option_id=replacement.resume_id,
        preparation=lambda command: (calls.append(command) or _completed(command)),
    )
    override = PlanScopedVersionOverrideRepository(home).get_current(
        subject_id=SUBJECT,
        application_plan_id=plan.plan_id,
        override_kind=(
            PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE
        ),
    )

    assert result.status is (
        InputReplacementResolutionStatus
        .REPLACED_AND_PREPARATION_COMPLETED
    )
    assert len(calls) == 1
    assert override.selected_option_id == replacement.resume_id
    assert override.replaced_option_id == old.resume_id
    assert override.replaced_option_content_hash == old.artifact_sha256
    assert override.replacement_target_id == (
        item.replacement_target_ref.target_id
    )
    assert override.contract_version == (
        PLAN_SCOPED_VERSION_OVERRIDE_REPLACEMENT_CONTRACT_VERSION
    )


def test_latex_replacement_rejects_same_and_unselectable_options(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "latex")
    home.ensure()
    plan, queue, item, provider, candidates, versions, old, replacement = (
        _latex_case(home)
    )
    calls = []
    same = _resolve(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        versions=versions,
        option_id=old.latex_version_id,
        preparation=lambda command: (calls.append(command) or _completed(command)),
    )
    missing = _resolve(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        versions=versions,
        option_id="resume-latex-version-" + "0" * 64,
        preparation=lambda command: (calls.append(command) or _completed(command)),
    )
    accepted = _resolve(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        versions=versions,
        option_id=replacement.latex_version_id,
        preparation=lambda command: (calls.append(command) or _completed(command)),
    )
    override = PlanScopedVersionOverrideRepository(home).get_current(
        subject_id=SUBJECT,
        application_plan_id=plan.plan_id,
        override_kind=PlanScopedVersionOverrideKind.LATEX_VERSION_OVERRIDE,
    )

    assert same.status is InputReplacementResolutionStatus.SAME_INPUT_SELECTED
    assert missing.status is (
        InputReplacementResolutionStatus.OPTION_NOT_SELECTABLE
    )
    assert accepted.status is (
        InputReplacementResolutionStatus
        .REPLACED_AND_PREPARATION_COMPLETED
    )
    assert len(calls) == 1
    assert override.selected_option_id == replacement.latex_version_id
    assert override.replaced_option_id == old.latex_version_id


def test_replay_and_failed_rerun_keep_one_override_without_auto_loop(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "replay")
    home.ensure()
    plan, queue, item, provider, candidates, versions, _old, replacement = (
        _resume_case(home)
    )
    calls = []

    first = _resolve(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        versions=versions,
        option_id=replacement.resume_id,
        preparation=lambda command: (
            calls.append(command)
            or RunApplicationPreparationResult(
                ApplicationPreparationStatus.FAILED,
                None,
                None,
                False,
                "synthetic failure",
            )
        ),
    )
    replay = _resolve(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        versions=versions,
        option_id=replacement.resume_id,
        preparation=lambda command: (calls.append(command) or _completed(command)),
    )
    override = PlanScopedVersionOverrideRepository(home).get_current(
        subject_id=SUBJECT,
        application_plan_id=plan.plan_id,
        override_kind=(
            PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE
        ),
    )

    assert first.status is (
        InputReplacementResolutionStatus
        .REPLACEMENT_RECORDED_PREPARATION_FAILED
    )
    assert replay.status is InputReplacementResolutionStatus.UNCHANGED
    assert replay.receipt == first.receipt
    assert len(calls) == 1
    assert override.override_id == first.receipt.override_id
