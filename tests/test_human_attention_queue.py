"""Synthetic acceptance tests for the P2b5 current attention read model."""

from __future__ import annotations

import ast
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.human_attention_queue as queue_module
from core.application_answers import (
    ApplicationAnswerPolicy,
    PrepareApplicationAnswersCommand,
    PreparedApplicationAnswerSetReadResult,
    PreparedApplicationAnswerSetReadStatus,
    PrivateHomeApplicationFactProvider,
    PrivateHomePreparedApplicationAnswerSetRepository,
    prepare_application_answers,
)
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.application_preparation_orchestrator import (
    APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION,
    BASE_LATEX_STOP_REASON_CONTRACT_VERSION,
    CANDIDATE_EVIDENCE_STOP_REASON_CONTRACT_VERSION,
    COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION,
    RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    ApplicationAnswersStopReason,
    ApplicationPreparationRunListResult,
    ApplicationPreparationRunListStatus,
    ApplicationPreparationStage,
    BaseLatexPreparationStopReason,
    CandidateEvidenceSnapshotStopReason,
    CoverLetterDraftStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    ApplicationPreparationStatus,
    PrivateHomeApplicationPreparationRunRepository,
    PublicPreparationStageResult,
    PublicStageDirective,
    PublicStageStatus,
    ResumeFactQAStopReason,
    RunApplicationPreparationCommand,
    run_application_preparation,
)
from core.human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionKind,
    HumanAttentionQueueFailureReason,
    HumanAttentionQueueStatus,
    HumanAttentionReasonCode,
    HumanAttentionResolutionCapability,
    build_current_human_attention_queue,
)
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from tests.test_application_preparation_orchestrator import (
    OUTPUTS,
    _Recorder,
    _hash,
    _recipe,
)


NOW = datetime(2026, 7, 29, 4, 0, tzinfo=timezone.utc)
SUBJECT = "subject-attention-synthetic"
OTHER_SUBJECT = "subject-attention-other"


def _plan(
    home: PrivateHome,
    *,
    job_id: str,
    priority: ProposedPriorityLevel = ProposedPriorityLevel.P1,
    subject_id: str = SUBJECT,
    revision: int = 1,
) -> tuple[ApplicationPlan, PrivateHomeApplicationPlanRepository]:
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id=job_id,
        job_revision=revision,
        job_content_hash=_hash(f"{job_id}:{revision}"),
        priority_decision_id=f"decision-{job_id}-{revision}",
        policy_id="priority-policy-v1",
        policy_version=1,
        policy_content_hash="a" * 64,
        accepted_job_intent_id=f"intent-{job_id}-{revision}",
        priority_level=priority,
        created_at=NOW,
    )
    repository = PrivateHomeApplicationPlanRepository(home)
    assert repository.save(plan).plan == plan
    return plan, repository


def _invoke(
    *,
    plan: ApplicationPlan,
    plan_repository,
    run_repository,
    recipe,
    now: datetime = NOW,
):
    result = run_application_preparation(
        RunApplicationPreparationCommand(
            subject_id=plan.subject_id,
            application_plan_id=plan.plan_id,
            now=now,
        ),
        application_plan_repository=plan_repository,
        recipe=recipe,
        run_repository=run_repository,
    )
    assert result.run is not None
    return result.run


def _deferred_recipe(
    *,
    stage: ApplicationPreparationStage,
    public_status: str,
    reason_code: str,
    input_binding: str = "deferred-binding",
):
    recorder = _Recorder()

    def deferred(request):
        return PublicPreparationStageResult.legacy_stopped(
            stage=request.stage,
            status=PublicStageStatus.DEFERRED,
            public_status=public_status,
            reason_code=reason_code,
            human_attention_required=True,
        )

    return recorder, _recipe(
        recorder,
        input_binding=input_binding,
        overrides={stage: deferred},
    )


def _failed_recipe(
    *, stage: ApplicationPreparationStage, input_binding: str
):
    recorder = _Recorder()

    def failed(request):
        return PublicPreparationStageResult.legacy_stopped(
            stage=request.stage,
            status=PublicStageStatus.FAILED,
            public_status="FAILED",
            reason_code="INTEGRITY_FAILURE",
        )

    return recorder, _recipe(
        recorder,
        input_binding=input_binding,
        overrides={stage: failed},
    )


def _typed_deferred_recipe(
    *,
    stage: ApplicationPreparationStage,
    reason,
    contract_version: str,
    input_binding: str = "typed-deferred-binding",
):
    recorder = _Recorder()

    def deferred(request):
        return PublicPreparationStageResult.deferred(
            stage=request.stage,
            stop_reason=PreparationStopReasonEnvelope(
                stage=request.stage,
                code=reason,
                contract_version=contract_version,
                outcome=PreparationStageOutcome.DEFERRED,
            ),
            human_attention_required=True,
        )

    return recorder, _recipe(
        recorder,
        input_binding=input_binding,
        overrides={stage: deferred},
    )


def _queue(home: PrivateHome, *, subject_id: str = SUBJECT, now=NOW):
    return build_current_human_attention_queue(
        subject_id=subject_id,
        now=now,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        application_plan_repository=PrivateHomeApplicationPlanRepository(
            home
        ),
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
    )


def _write_vault(home: PrivateHome, *, subject_id: str = SUBJECT) -> None:
    paths = home.ensure()
    paths.profile_facts.write_text(
        json.dumps(
            {
                "normalized": {},
                "schema_version": 1,
                "subject_id": subject_id,
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


def _completed_with_real_answers(
    home: PrivateHome,
    *,
    job_id: str = "job-answer-attention",
    priority: ProposedPriorityLevel = ProposedPriorityLevel.P1,
):
    _write_vault(home)
    plan, plans = _plan(home, job_id=job_id, priority=priority)
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    answers = PrivateHomePreparedApplicationAnswerSetRepository(home)
    recorder = _Recorder()

    def real_answers(request):
        result = prepare_application_answers(
            PrepareApplicationAnswersCommand(
                subject_id=request.subject_id,
                application_plan_id=request.application_plan_id,
                now=request.now,
            ),
            application_plan_repository=plans,
            fact_provider=PrivateHomeApplicationFactProvider(home),
            answer_policy=ApplicationAnswerPolicy.default(),
            answer_set_repository=answers,
        )
        assert result.answer_set is not None
        answer_set = result.answer_set
        return PublicPreparationStageResult.legacy_success(
            stage=request.stage,
            status=PublicStageStatus.CREATED,
            public_status=result.status.value,
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

    run = _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=_recipe(
            recorder,
            input_binding=f"answers:{job_id}",
            overrides={
                ApplicationPreparationStage.APPLICATION_ANSWERS: real_answers
            },
        ),
    )
    return plan, run, plans, runs, answers


def test_current_deferred_run_creates_typed_user_fact_item(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-deferred-fact")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    _recorder, recipe = _typed_deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_EVIDENCE,
        reason=CandidateEvidenceSnapshotStopReason.NO_USABLE_EVIDENCE,
        contract_version=CANDIDATE_EVIDENCE_STOP_REASON_CONTRACT_VERSION,
    )
    run = _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=recipe,
    )

    result = _queue(home)

    assert result.status is HumanAttentionQueueStatus.SUCCEEDED
    assert result.item_count == result.user_item_count == 1
    item = result.items[0]
    assert item.source_preparation_run_id == run.run_id
    assert item.source_stage is ApplicationPreparationStage.RESUME_EVIDENCE
    assert item.attention_kind is HumanAttentionKind.USER_FACT_REQUIRED
    assert item.audience is HumanAttentionAudience.USER
    assert item.reason_code is HumanAttentionReasonCode.MISSING_TRUSTED_FACT


def test_completed_without_attention_is_absent(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-clean")
    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        recipe=_recipe(_Recorder(), input_binding="clean"),
    )

    result = _queue(home)

    assert result.status is HumanAttentionQueueStatus.SUCCEEDED
    assert result.items == ()
    assert result.item_count == result.affected_plan_count == 0


def test_completed_answer_set_expands_only_blocking_items(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    _plan_value, run, *_ = _completed_with_real_answers(home)

    result = _queue(home)

    assert result.status is HumanAttentionQueueStatus.SUCCEEDED
    assert result.item_count == 3
    assert all(
        item.source_preparation_run_id == run.run_id
        and item.attention_kind
        is HumanAttentionKind.USER_ATTESTATION_REQUIRED
        and item.canonical_answer_key is not None
        for item in result.items
    )
    assert {
        item.canonical_answer_key.value for item in result.items
    } == {"attestation", "consent", "signature"}


@pytest.mark.parametrize(
    (
        "stage",
        "reason",
        "contract_version",
        "kind",
        "audience",
        "capability",
    ),
    (
        (
            ApplicationPreparationStage.APPLICATION_ANSWERS,
            ApplicationAnswersStopReason.NO_TRUSTED_FACTS,
            APPLICATION_ANSWERS_STOP_REASON_CONTRACT_VERSION,
            HumanAttentionKind.USER_FACT_REQUIRED,
            HumanAttentionAudience.USER,
            HumanAttentionResolutionCapability.PROVIDE_FACT,
        ),
        (
            ApplicationPreparationStage.BASE_LATEX_SELECTION,
            (
                BaseLatexPreparationStopReason
                .USER_REQUIREMENT_UNSATISFIABLE
            ),
            BASE_LATEX_STOP_REASON_CONTRACT_VERSION,
            HumanAttentionKind.USER_CHOICE_REQUIRED,
            HumanAttentionAudience.USER,
            HumanAttentionResolutionCapability.MAKE_CHOICE,
        ),
        (
            ApplicationPreparationStage.RESUME_FACT_QA,
            ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
            RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
            HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER,
            HumanAttentionAudience.OPERATOR,
            HumanAttentionResolutionCapability.NON_OVERRIDABLE,
        ),
        (
            ApplicationPreparationStage.COVER_LETTER_DRAFT,
            CoverLetterDraftStopReason.AGENT_OUTPUT_UNSAFE,
            COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION,
            HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
            HumanAttentionAudience.OPERATOR,
            HumanAttentionResolutionCapability.OPERATOR_REPAIR,
        ),
    ),
)
def test_explicit_defer_mapping_categories(
    tmp_path: Path,
    stage,
    reason,
    contract_version,
    kind,
    audience,
    capability,
) -> None:
    home = PrivateHome(tmp_path / reason.value)
    plan, plans = _plan(home, job_id=f"job-{reason.value.lower()}")
    recorder, recipe = _typed_deferred_recipe(
        stage=stage,
        reason=reason,
        contract_version=contract_version,
    )
    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        recipe=recipe,
    )

    result = _queue(home)

    assert result.items[0].attention_kind is kind
    assert result.items[0].audience is audience
    assert result.items[0].resolution_capability is capability
    assert result.items[0].source_stage is stage


def test_failed_run_is_always_operator_item(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-failed")
    _recorder, recipe = _failed_recipe(
        stage=ApplicationPreparationStage.RESUME_PUBLICATION,
        input_binding="failed",
    )
    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        recipe=recipe,
    )

    result = _queue(home)

    assert result.items[0].attention_kind is (
        HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED
    )
    assert result.items[0].audience is HumanAttentionAudience.OPERATOR
    assert result.items[0].reason_code is (
        HumanAttentionReasonCode.SYSTEM_INTEGRITY_OR_CONTRACT_FAILURE
    )


def test_unknown_typed_defer_reason_fails_safe_to_operator(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-unknown")
    _recorder, recipe = _deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_TAILORING,
        public_status="DEFERRED_NEW_TYPED_REASON_V2",
        reason_code="NEW_TYPED_REASON_V2",
    )
    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        recipe=recipe,
    )

    item = _queue(home).items[0]

    assert item.audience is HumanAttentionAudience.OPERATOR
    assert (
        item.reason_code
        is HumanAttentionReasonCode.UNCLASSIFIED_SYSTEM_BLOCKER
    )
    assert (
        item.resolution_capability
        is HumanAttentionResolutionCapability.NON_OVERRIDABLE
    )


def test_new_clean_run_makes_old_deferred_item_disappear(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-current")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    _recorder, deferred = _deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_EVIDENCE,
        public_status="DEFERRED_NO_EVIDENCE",
        reason_code="NO_EVIDENCE",
        input_binding="old",
    )
    old = _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=deferred,
        now=NOW,
    )
    assert _queue(home).item_count == 1
    new = _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=_recipe(_Recorder(), input_binding="new-clean"),
        now=NOW + timedelta(minutes=1),
    )

    result = _queue(home, now=NOW + timedelta(minutes=2))

    assert old.run_id != new.run_id
    assert result.items == ()


class _ReversedListRepository:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def list_for_subject(self, *, subject_id):
        result = self.delegate.list_for_subject(subject_id=subject_id)
        return ApplicationPreparationRunListResult(
            status=result.status, runs=tuple(reversed(result.runs))
        )

    def find_current_for_plan(self, **values):
        return self.delegate.find_current_for_plan(**values)


def test_item_identity_snapshot_and_current_ignore_mtime_and_list_order(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-stable")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    _recorder, recipe = _deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_FACT_QA,
        public_status="DEFERRED_NEEDS_HUMAN",
        reason_code="QA_UNCERTAIN",
    )
    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=recipe,
    )
    first = _queue(home)
    for path in home.paths.application_preparation_runs.rglob("*.json"):
        os.utime(path, (1_000_000_000, 1_000_000_000))
    reversed_result = build_current_human_attention_queue(
        subject_id=SUBJECT,
        now=NOW + timedelta(days=1),
        run_repository=_ReversedListRepository(runs),
        application_plan_repository=plans,
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
    )
    restarted = _queue(home, now=NOW + timedelta(days=2))

    assert first.items[0].item_id == reversed_result.items[0].item_id
    assert (
        first.queue_snapshot_hash
        == reversed_result.queue_snapshot_hash
        == restarted.queue_snapshot_hash
    )
    assert first.evaluated_at != reversed_result.evaluated_at


def test_sorting_uses_priority_then_audience_kind_and_ties(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    specifications = (
        (
            "job-p1-operator",
            ProposedPriorityLevel.P1,
            ApplicationPreparationStage.COVER_LETTER_DRAFT,
            CoverLetterDraftStopReason.AGENT_OUTPUT_UNSAFE,
            COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION,
        ),
        (
            "job-p0-operator",
            ProposedPriorityLevel.P0,
            ApplicationPreparationStage.COVER_LETTER_DRAFT,
            CoverLetterDraftStopReason.AGENT_OUTPUT_UNSAFE,
            COVER_LETTER_DRAFT_STOP_REASON_CONTRACT_VERSION,
        ),
        (
            "job-p1-fact",
            ProposedPriorityLevel.P1,
            ApplicationPreparationStage.RESUME_EVIDENCE,
            CandidateEvidenceSnapshotStopReason.NO_USABLE_EVIDENCE,
            CANDIDATE_EVIDENCE_STOP_REASON_CONTRACT_VERSION,
        ),
        (
            "job-p1-choice",
            ProposedPriorityLevel.P1,
            ApplicationPreparationStage.BASE_LATEX_SELECTION,
            (
                BaseLatexPreparationStopReason
                .USER_REQUIREMENT_UNSATISFIABLE
            ),
            BASE_LATEX_STOP_REASON_CONTRACT_VERSION,
        ),
        (
            "job-p1-correction",
            ProposedPriorityLevel.P1,
            ApplicationPreparationStage.RESUME_FACT_QA,
            ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
            RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        ),
    )
    plans = PrivateHomeApplicationPlanRepository(home)
    for index, (
        job_id,
        priority,
        stage,
        reason,
        contract_version,
    ) in enumerate(specifications):
        plan, _ = _plan(home, job_id=job_id, priority=priority)
        _recorder, recipe = _typed_deferred_recipe(
            stage=stage,
            reason=reason,
            contract_version=contract_version,
            input_binding=f"binding-{index}",
        )
        _invoke(
            plan=plan,
            plan_repository=plans,
            run_repository=runs,
            recipe=recipe,
            now=NOW + timedelta(seconds=index),
        )

    result = _queue(home)

    assert [item.job_id for item in result.items] == [
        "job-p0-operator",
        "job-p1-fact",
        "job-p1-choice",
        "job-p1-operator",
        "job-p1-correction",
    ]


def test_subject_isolation_excludes_other_subject(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    for subject, job_id in (
        (SUBJECT, "job-owned"),
        (OTHER_SUBJECT, "job-other"),
    ):
        plan, plans = _plan(
            home, job_id=job_id, subject_id=subject
        )
        _recorder, recipe = _deferred_recipe(
            stage=ApplicationPreparationStage.RESUME_EVIDENCE,
            public_status="DEFERRED_NO_EVIDENCE",
            reason_code="NO_EVIDENCE",
            input_binding=job_id,
        )
        _invoke(
            plan=plan,
            plan_repository=plans,
            run_repository=(
                PrivateHomeApplicationPreparationRunRepository(home)
            ),
            recipe=recipe,
        )

    result = _queue(home)

    assert [item.job_id for item in result.items] == ["job-owned"]
    assert all(item.subject_id == SUBJECT for item in result.items)


class _MissingAnswerRepository:
    def get(self, **_values):
        return PreparedApplicationAnswerSetReadResult(
            PreparedApplicationAnswerSetReadStatus.NOT_FOUND, None
        )


class _StaticAnswerRepository:
    def __init__(self, answer_set) -> None:
        self.answer_set = answer_set

    def get(self, **_values):
        return PreparedApplicationAnswerSetReadResult(
            PreparedApplicationAnswerSetReadStatus.FOUND,
            self.answer_set,
        )


def test_missing_or_mismatched_answer_binding_fails_closed(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    _plan_value, _run_value, plans, runs, _answers = (
        _completed_with_real_answers(home)
    )
    result = build_current_human_attention_queue(
        subject_id=SUBJECT,
        now=NOW,
        run_repository=runs,
        application_plan_repository=plans,
        answer_set_repository=_MissingAnswerRepository(),
    )

    assert result.status is HumanAttentionQueueStatus.FAILED
    assert result.reason_code is (
        HumanAttentionQueueFailureReason.ANSWER_SET_NOT_FOUND
    )
    assert result.items == ()


def test_answer_set_plan_binding_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    _plan_value, _run_value, plans, runs, answer_repository = (
        _completed_with_real_answers(home)
    )
    other_plan, _ = _plan(home, job_id="job-other-answer")
    other_result = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=other_plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=plans,
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=ApplicationAnswerPolicy.default(),
        answer_set_repository=answer_repository,
    )
    assert other_result.answer_set is not None

    result = build_current_human_attention_queue(
        subject_id=SUBJECT,
        now=NOW,
        run_repository=runs,
        application_plan_repository=plans,
        answer_set_repository=_StaticAnswerRepository(
            other_result.answer_set
        ),
    )

    assert result.status is HumanAttentionQueueStatus.FAILED
    assert result.reason_code is (
        HumanAttentionQueueFailureReason.ANSWER_SET_BINDING_MISMATCH
    )


def test_build_is_zero_write_and_calls_no_preparation_surface(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-read-only")
    _recorder, recipe = _deferred_recipe(
        stage=ApplicationPreparationStage.RESUME_EVIDENCE,
        public_status="DEFERRED_NO_EVIDENCE",
        reason_code="NO_EVIDENCE",
    )
    _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        recipe=recipe,
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.root.rglob("*")
        if path.is_file()
    }

    result = _queue(home)

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.root.rglob("*")
        if path.is_file()
    }
    assert result.status is HumanAttentionQueueStatus.SUCCEEDED
    assert after == before

    tree = ast.parse(
        Path(queue_module.__file__).read_text(encoding="utf-8")
    )
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    source = Path(queue_module.__file__).read_text(encoding="utf-8")
    assert not any(
        forbidden in source
        for forbidden in (
            "run_application_preparation",
            "CandidateVault",
            "SemanticMapper",
            "ApplicationEngine",
            "Browser",
            "compiler",
            "renderer",
            "submit(",
        )
    )
    assert not any(
        module.startswith("resume_")
        or module.startswith("cover_letter_")
        for module in imports
    )
