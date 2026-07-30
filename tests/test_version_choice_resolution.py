"""Focused S3g2 Resume/LaTeX choice resolution tests."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.application_preparation_orchestrator import (
    ApplicationPreparationStage,
    ApplicationPreparationStatus,
    RunApplicationPreparationResult,
)
from core.base_latex_selection import (
    BaseLatexSelectionMethod,
    SelectBaseLatexVersionCommand,
    select_base_latex_version,
)
from core.human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionKind,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
)
from core.plan_scoped_version_override import (
    PlanScopedVersionOverrideKind,
    PlanScopedVersionOverrideRepository,
)
from core.resume_selection import (
    PrivateHomeResumeSelectionDecisionRepository,
    ResumeSelectionMethod,
    SelectBaseResumeCommand,
    select_base_resume,
)
from core.resume_candidates import (
    ResumeCandidateListResult,
    ResumeCandidateListStatus,
)
from core.version_choice_resolution import (
    VersionChoiceResolutionCommand,
    VersionChoiceResolutionParserProposal,
    VersionChoiceResolutionReceiptRepository,
    VersionChoiceResolutionStatus,
    resolve_version_choice,
)
from tests.test_base_latex_selection import (
    METADATA as LATEX_METADATA,
    _FakeSelectionAgent,
    _add_version,
    _setup,
)
from tests.test_resume_selection import (
    METADATA as RESUME_METADATA,
    FakeAgent,
    FakeCandidateProvider,
    FakeJobRepository,
    FakePlanRepository,
    _candidate,
    _home,
    _job,
    _plan,
)


NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)


def _raw(cls, **values):
    instance = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    return instance


def _item(plan, stage, suffix="a", *, audience=HumanAttentionAudience.USER):
    return _raw(
        __import__(
            "core.human_attention_queue", fromlist=["HumanAttentionQueueItem"]
        ).HumanAttentionQueueItem,
        item_id="human-attention-item-" + suffix * 64,
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        audience=audience,
        attention_kind=HumanAttentionKind.USER_CHOICE_REQUIRED,
        source_stage=stage,
        required_action="Choose one current selectable version.",
        source_record_id="selection-deferred-record",
        source_preparation_run_id="preparation-run-1",
    )


def _queue(subject_id, items):
    return _raw(
        HumanAttentionQueueResult,
        status=HumanAttentionQueueStatus.SUCCEEDED,
        subject_id=subject_id,
        items=tuple(items),
    )


def _preparation(status, run_id="preparation-rerun-1"):
    class _Run:
        pass

    return _raw(
        RunApplicationPreparationResult,
        status=status,
        run=_raw(_Run, run_id=run_id),
        reason_code=None,
    )


def _preparation_callable(status, calls=None, raises=False):
    async def invoke(command):
        if calls is not None:
            calls.append(command)
        if raises:
            raise RuntimeError("synthetic rerun failure")
        return _preparation(status)

    return invoke


class _Parser:
    def __init__(self, option_id):
        self.option_id = option_id
        self.calls = []

    def parse(self, request):
        self.calls.append(request)
        return VersionChoiceResolutionParserProposal(
            selected_option_id=self.option_id,
            unambiguous=True,
        )


@pytest.mark.asyncio
async def test_resume_choice_writes_override_reruns_and_p2a3_consumes_it(
    tmp_path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    first = _candidate(home, name="general.pdf", marker=b"one")
    second = _candidate(home, name="targeted.pdf", marker=b"two")
    provider = FakeCandidateProvider("subject-a", (first, second))
    overrides = PlanScopedVersionOverrideRepository(home)
    item = _item(
        plan, ApplicationPreparationStage.BASE_RESUME_SELECTION
    )
    preparation_calls = []

    result = await resolve_version_choice(
        VersionChoiceResolutionCommand(
            plan.subject_id,
            item.item_id,
            f"Use {second.resume_id}.",
            NOW,
        ),
        queue_reader=lambda **_: _queue(plan.subject_id, (item,)),
        resume_candidate_provider=provider,
        latex_version_provider=object(),
        parser=None,
        override_repository=overrides,
        preparation_callable=_preparation_callable(
            ApplicationPreparationStatus.COMPLETED, preparation_calls
        ),
        receipt_repository=VersionChoiceResolutionReceiptRepository(home),
    )

    assert result.status is (
        VersionChoiceResolutionStatus
        .RESOLVED_AND_PREPARATION_COMPLETED
    )
    assert len(preparation_calls) == 1
    override = overrides.get_current(
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        override_kind=(
            PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE
        ),
    )
    assert override is not None
    assert override.selected_option_id == second.resume_id

    selection = await select_base_resume(
        SelectBaseResumeCommand(plan.subject_id, plan.plan_id, NOW),
        application_plan_repository=FakePlanRepository((plan,)),
        job_repository=FakeJobRepository(job),
        candidate_provider=provider,
        agent=FakeAgent(AssertionError("override must bypass Agent")),
        metadata=RESUME_METADATA,
        decision_repository=PrivateHomeResumeSelectionDecisionRepository(
            home
        ),
        override_provider=overrides,
    )
    assert selection.decision.source_resume_id == second.resume_id
    assert selection.decision.selection_method is (
        ResumeSelectionMethod.USER_OVERRIDE
    )


@pytest.mark.asyncio
async def test_latex_parser_choice_is_validated_and_p2a6b_consumes_override(
    tmp_path,
) -> None:
    parts = await _setup(tmp_path)
    first = _add_version(parts, marker="one", labels=("classic",))
    second = _add_version(parts, marker="two", labels=("compact",))
    plan = parts["plan"]
    item = _item(
        plan, ApplicationPreparationStage.BASE_LATEX_SELECTION
    )
    parser = _Parser(second.latex_version_id)
    overrides = PlanScopedVersionOverrideRepository(parts["home"])

    result = await resolve_version_choice(
        VersionChoiceResolutionCommand(
            plan.subject_id,
            item.item_id,
            "Use the second layout.",
            NOW,
        ),
        queue_reader=lambda **_: _queue(plan.subject_id, (item,)),
        resume_candidate_provider=object(),
        latex_version_provider=parts["latex_repository"],
        parser=parser,
        override_repository=overrides,
        preparation_callable=_preparation_callable(
            ApplicationPreparationStatus.DEFERRED
        ),
        receipt_repository=VersionChoiceResolutionReceiptRepository(
            parts["home"]
        ),
    )
    assert result.status is (
        VersionChoiceResolutionStatus
        .RESOLVED_AND_PREPARATION_DEFERRED
    )
    assert len(parser.calls) == 1
    assert {
        key
        for option in parser.calls[0].options
        for key in option.parser_dict()
    } == {"display_labels", "option_id", "option_kind"}

    selection = await select_base_latex_version(
        SelectBaseLatexVersionCommand(
            plan.subject_id,
            plan.plan_id,
            parts["qa_result"].qa_result_id,
            NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        fact_qa_repository=parts["qa_repository"],
        draft_repository=parts["draft_repository"],
        selection_repository=parts["selection_repository"],
        job_repository=parts["job_repository"],
        latex_version_provider=parts["latex_repository"],
        agent=_FakeSelectionAgent(
            AssertionError("override must bypass Agent")
        ),
        metadata=LATEX_METADATA,
        decision_repository=parts["decision_repository"],
        override_provider=overrides,
    )
    assert selection.decision.selected_latex_version_id == (
        second.latex_version_id
    )
    assert selection.decision.selection_method is (
        BaseLatexSelectionMethod.USER_OVERRIDE
    )
    assert selection.decision.selected_latex_version_id != (
        first.latex_version_id
    )

    invalid_parser = _Parser("resume-latex-version-" + "0" * 64)
    invalid_item = _item(
        plan,
        ApplicationPreparationStage.BASE_LATEX_SELECTION,
        "b",
    )
    invalid = await resolve_version_choice(
        VersionChoiceResolutionCommand(
            plan.subject_id,
            invalid_item.item_id,
            "Use a missing layout.",
            NOW,
        ),
        queue_reader=lambda **_: _queue(
            plan.subject_id,
            (invalid_item,),
        ),
        resume_candidate_provider=object(),
        latex_version_provider=parts["latex_repository"],
        parser=invalid_parser,
        override_repository=overrides,
        preparation_callable=lambda _: pytest.fail("must not rerun"),
        receipt_repository=VersionChoiceResolutionReceiptRepository(
            parts["home"]
        ),
    )
    assert invalid.status is VersionChoiceResolutionStatus.OPTION_NOT_SELECTABLE


@pytest.mark.asyncio
async def test_ambiguous_unsupported_failure_and_replay_preserve_history(
    tmp_path,
) -> None:
    home = _home(tmp_path)
    job = _job()
    plan = _plan(job)
    candidate = _candidate(home)
    provider = FakeCandidateProvider("subject-a", (candidate,))
    item = _item(
        plan, ApplicationPreparationStage.BASE_RESUME_SELECTION
    )
    overrides = PlanScopedVersionOverrideRepository(home)
    receipts = VersionChoiceResolutionReceiptRepository(home)
    queue_calls = []

    def queue_reader(**kwargs):
        queue_calls.append(kwargs)
        return _queue(plan.subject_id, (item,))

    ambiguous = await resolve_version_choice(
        VersionChoiceResolutionCommand(
            plan.subject_id, item.item_id, "Use the best one.", NOW
        ),
        queue_reader=queue_reader,
        resume_candidate_provider=provider,
        latex_version_provider=object(),
        parser=_Parser(candidate.resume_id),
        override_repository=overrides,
        preparation_callable=lambda _: pytest.fail("must not rerun"),
        receipt_repository=receipts,
    )
    assert ambiguous.status is (
        VersionChoiceResolutionStatus.DEFERRED_AMBIGUOUS_INPUT
    )
    other = _candidate(
        home,
        subject_id="subject-b",
        name="other.pdf",
        marker=b"other",
    )

    class _CrossSubjectProvider:
        def list_selectable(self, _subject_id):
            return _raw(
                ResumeCandidateListResult,
                status=ResumeCandidateListStatus.SUCCEEDED,
                subject_id=plan.subject_id,
                candidates=(other,),
            )

    cross_item = _item(
        plan,
        ApplicationPreparationStage.BASE_RESUME_SELECTION,
        "b",
    )
    cross = await resolve_version_choice(
        VersionChoiceResolutionCommand(
            plan.subject_id,
            cross_item.item_id,
            other.resume_id,
            NOW,
        ),
        queue_reader=lambda **_: _queue(
            plan.subject_id, (cross_item,)
        ),
        resume_candidate_provider=_CrossSubjectProvider(),
        latex_version_provider=object(),
        parser=None,
        override_repository=overrides,
        preparation_callable=lambda _: pytest.fail("must not rerun"),
        receipt_repository=receipts,
    )
    assert cross.status is VersionChoiceResolutionStatus.FAILED
    assert overrides.get_current(
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        override_kind=(
            PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE
        ),
    ) is None

    unsupported_item = _item(
        plan, ApplicationPreparationStage.RESUME_VISUAL_QA, "c"
    )
    unsupported = await resolve_version_choice(
        VersionChoiceResolutionCommand(
            plan.subject_id,
            unsupported_item.item_id,
            candidate.resume_id,
            NOW,
        ),
        queue_reader=lambda **_: _queue(
            plan.subject_id, (unsupported_item,)
        ),
        resume_candidate_provider=provider,
        latex_version_provider=object(),
        parser=None,
        override_repository=overrides,
        preparation_callable=lambda _: pytest.fail("must not rerun"),
        receipt_repository=receipts,
    )
    assert unsupported.status is (
        VersionChoiceResolutionStatus.UNSUPPORTED_ITEM
    )

    command = VersionChoiceResolutionCommand(
        plan.subject_id,
        item.item_id,
        candidate.resume_id,
        NOW,
    )
    first = await resolve_version_choice(
        command,
        queue_reader=queue_reader,
        resume_candidate_provider=provider,
        latex_version_provider=object(),
        parser=None,
        override_repository=overrides,
        preparation_callable=_preparation_callable(
            ApplicationPreparationStatus.DEFERRED
        ),
        receipt_repository=receipts,
    )
    assert first.status is (
        VersionChoiceResolutionStatus
        .RESOLVED_AND_PREPARATION_DEFERRED
    )
    assert first.receipt is not None
    assert overrides.get_current(
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        override_kind=(
            PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE
        ),
    ) is not None

    def empty_queue(**kwargs):
        queue_calls.append(kwargs)
        return _queue(plan.subject_id, ())

    replay = await resolve_version_choice(
        command,
        queue_reader=empty_queue,
        resume_candidate_provider=provider,
        latex_version_provider=object(),
        parser=None,
        override_repository=overrides,
        preparation_callable=lambda _: pytest.fail("must not rerun"),
        receipt_repository=receipts,
    )
    assert replay.status is VersionChoiceResolutionStatus.UNCHANGED
    assert replay.receipt == first.receipt

    failure_item = _item(
        plan,
        ApplicationPreparationStage.BASE_RESUME_SELECTION,
        "d",
    )
    failure = await resolve_version_choice(
        VersionChoiceResolutionCommand(
            plan.subject_id,
            failure_item.item_id,
            f"Use {candidate.resume_id} for this retry.",
            NOW,
        ),
        queue_reader=lambda **_: _queue(
            plan.subject_id, (failure_item,)
        ),
        resume_candidate_provider=provider,
        latex_version_provider=object(),
        parser=None,
        override_repository=overrides,
        preparation_callable=_preparation_callable(
            ApplicationPreparationStatus.FAILED, raises=True
        ),
        receipt_repository=receipts,
    )
    assert failure.status is VersionChoiceResolutionStatus.FAILED
    assert failure.receipt is not None
    current = overrides.get_current(
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        override_kind=(
            PlanScopedVersionOverrideKind.RESUME_CANDIDATE_OVERRIDE
        ),
    )
    assert current is not None
    assert current.previous_override_id == first.receipt.override_record_id
    assert len(queue_calls) == 3
    root = Path(__file__).parents[1]
    ui_source = (
        root / "dashboard/version_choice_resolution.py"
    ).read_text()
    server_source = (root / "dashboard/server.py").read_text()
    javascript = (root / "dashboard/static/app.js").read_text()
    assert "version_choice_resolution_controller" in server_source
    assert "resolveVersionChoice" in javascript
    for forbidden in (
        "Browser",
        "ApplicationEngine",
        "PermitService",
        "submit",
    ):
        assert forbidden not in ui_source
