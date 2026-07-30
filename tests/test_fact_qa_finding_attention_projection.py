from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core.application_answers import (
    PrivateHomePreparedApplicationAnswerSetRepository,
)
from core.application_preparation_orchestrator import (
    COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
    RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    CoverLetterFactQAStopReason,
    CoverLetterPublicationStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PreparedResumePublicationStopReason,
    PrivateHomeApplicationPreparationRunRepository,
    PublicPreparationStageResult,
    ResumeFactQAStopReason,
)
from core.fact_qa_findings import (
    FactQABlockingFinding,
    FactQABlockingFindingReadStatus,
    FactQABlockingFindingSet,
    FactQABlockingFindingSetResult,
    FactQAMaterialKind,
)
from core.human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionKind,
    HumanAttentionResolutionCapability,
    build_current_human_attention_queue,
)
from core.private_home import PrivateHome
from core.publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    create_publication_stopped_source_lineage,
)
from tests.test_application_preparation_orchestrator import (
    _Recorder,
    _hash,
    _recipe,
)
from tests.test_human_attention_queue import NOW, _invoke, _plan


class _FindingProvider:
    def __init__(self, values):
        self.values = values

    def list_blocking_findings(
        self, *, subject_id, qa_result_id, material_kind
    ):
        value = self.values.get((subject_id, qa_result_id, material_kind))
        if value is None:
            return FactQABlockingFindingSetResult(
                FactQABlockingFindingReadStatus.NOT_FOUND, None
            )
        return FactQABlockingFindingSetResult(
            FactQABlockingFindingReadStatus.FOUND, value
        )


def _finding_set(plan, material, qa_id, qa_hash, names=("one", "two")):
    source_id = f"{material.value.lower()}-draft-1"
    source_hash = _hash(source_id)
    return FactQABlockingFindingSet(
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        material_kind=material,
        qa_result_id=qa_id,
        qa_result_content_hash=qa_hash,
        qa_contract_version=f"{material.value.lower()}-fact-qa-v1",
        findings=tuple(
            FactQABlockingFinding(
                finding_id=f"finding-{name}",
                order=index,
                finding_kind="UNSUPPORTED_CLAIM",
                claim_summary=f"Synthetic unsupported claim {name}.",
                source_material_id=source_id,
                source_material_content_hash=source_hash,
            )
            for index, name in enumerate(names)
        ),
    )


async def _deferred_run(
    *,
    plan,
    plans,
    runs,
    stage,
    reason,
    reason_version,
    result_id,
    result_hash,
    outputs=None,
):
    recorder = _Recorder()

    def stopped(request):
        return PublicPreparationStageResult.deferred(
            stage=request.stage,
            stop_reason=PreparationStopReasonEnvelope(
                stage=request.stage,
                code=reason,
                contract_version=reason_version,
                outcome=PreparationStageOutcome.DEFERRED,
            ),
            result_id=result_id,
            result_content_hash=result_hash,
            outputs=outputs or {},
            human_attention_required=True,
        )

    return await _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=_recipe(
            recorder,
            input_binding=f"finding:{plan.plan_id}:{stage.value}",
            overrides={stage: stopped},
        ),
    )


def _queue(home, provider, *, subject_id):
    from core.application_plan import PrivateHomeApplicationPlanRepository

    return build_current_human_attention_queue(
        subject_id=subject_id,
        now=NOW,
        run_repository=PrivateHomeApplicationPreparationRunRepository(home),
        application_plan_repository=PrivateHomeApplicationPlanRepository(home),
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
        fact_qa_finding_provider=provider,
    )


async def test_direct_resume_and_cover_letter_findings_split_without_aggregate(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    resume_plan, plans = _plan(home, job_id="job-resume-findings")
    cover_plan, _ = _plan(home, job_id="job-cover-findings")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    resume_id, cover_id = "resume-qa-1", "cover-qa-1"
    resume_hash, cover_hash = _hash(resume_id), _hash(cover_id)
    await _deferred_run(
        plan=resume_plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.RESUME_FACT_QA,
        reason=ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        reason_version=RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        result_id=resume_id,
        result_hash=resume_hash,
    )
    await _deferred_run(
        plan=cover_plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.COVER_LETTER_FACT_QA,
        reason=CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM,
        reason_version=COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        result_id=cover_id,
        result_hash=cover_hash,
    )
    sets = (
        _finding_set(
            resume_plan, FactQAMaterialKind.RESUME, resume_id, resume_hash
        ),
        _finding_set(
            cover_plan,
            FactQAMaterialKind.COVER_LETTER,
            cover_id,
            cover_hash,
        ),
    )
    provider = _FindingProvider(
        {
            (item.subject_id, item.qa_result_id, item.material_kind): item
            for item in sets
        }
    )

    result = _queue(home, provider, subject_id=resume_plan.subject_id)

    assert result.item_count == 4
    assert all(
        item.fact_qa_finding_ref is not None
        and item.audience is HumanAttentionAudience.USER
        and item.resolution_capability
        is HumanAttentionResolutionCapability.CORRECT_MATERIAL
        for item in result.items
    )
    assert len({item.item_id for item in result.items}) == 4


async def test_resume_and_cover_publication_lineage_resolves_exact_findings(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    resume_plan, plans = _plan(home, job_id="job-resume-publication")
    cover_plan, _ = _plan(home, job_id="job-cover-publication")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    specifications = (
        (
            resume_plan,
            FactQAMaterialKind.RESUME,
            ApplicationPreparationStage.RESUME_PUBLICATION,
            ApplicationPreparationStage.RESUME_FACT_QA,
            PreparedResumePublicationStopReason.FACT_QA_NOT_PASSED,
            PREPARED_RESUME_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
        ),
        (
            cover_plan,
            FactQAMaterialKind.COVER_LETTER,
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
            ApplicationPreparationStage.COVER_LETTER_FACT_QA,
            CoverLetterPublicationStopReason.FACT_QA_NOT_PASSED,
            COVER_LETTER_PUBLICATION_STOP_REASON_CONTRACT_VERSION,
        ),
    )
    provider_values = {}
    for index, (
        plan,
        material,
        publication_stage,
        qa_stage,
        reason,
        reason_version,
    ) in enumerate(specifications):
        qa_id = f"qa-publication-{index}"
        qa_hash = _hash(qa_id)
        finding_set = _finding_set(
            plan, material, qa_id, qa_hash, names=("a", "b")
        )
        provider_values[(plan.subject_id, qa_id, material)] = finding_set
        lineage = create_publication_stopped_source_lineage(
            subject_id=plan.subject_id,
            application_plan_id=plan.plan_id,
            publication_stage=publication_stage,
            material_kind=PublicationMaterialKind(material.value),
            source_kind=PublicationStoppedSourceKind.FACT_QA_BLOCKER,
            source_stage=qa_stage,
            source_result_id=qa_id,
            source_outcome=PreparationStageOutcome.COMPLETED,
            source_contract_version=finding_set.qa_contract_version,
            source_result_content_hash=qa_hash,
            source_directive=PublicationBlockingDirective.FACT_QA_BLOCKED,
            source_artifact_id=(
                finding_set.findings[0].source_material_id
            ),
            source_artifact_content_hash=(
                finding_set.findings[0].source_material_content_hash
            ),
            blocking_lineage_ids=tuple(
                item.finding_id for item in finding_set.findings
            ),
        )
        await _deferred_run(
            plan=plan,
            plans=plans,
            runs=runs,
            stage=publication_stage,
            reason=reason,
            reason_version=reason_version,
            result_id=lineage.publication_result_id,
            result_hash=lineage.lineage_content_hash,
            outputs=lineage.output_references(),
        )

    result = _queue(
        home,
        _FindingProvider(provider_values),
        subject_id=resume_plan.subject_id,
    )

    assert result.item_count == 4
    assert {
        item.fact_qa_finding_ref.attention_origin_stage
        for item in result.items
    } == {
        ApplicationPreparationStage.RESUME_PUBLICATION,
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
    }


async def test_damaged_finding_collection_fails_closed_as_one_system_item(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-damaged-findings")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    qa_id, qa_hash = "resume-qa-damaged", _hash("resume-qa-damaged")
    await _deferred_run(
        plan=plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.RESUME_FACT_QA,
        reason=ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        reason_version=RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        result_id=qa_id,
        result_hash=qa_hash,
    )
    finding_set = replace(
        _finding_set(plan, FactQAMaterialKind.RESUME, qa_id, qa_hash),
        application_plan_id="other-plan",
    )
    provider = _FindingProvider(
        {(plan.subject_id, qa_id, FactQAMaterialKind.RESUME): finding_set}
    )

    result = _queue(home, provider, subject_id=plan.subject_id)

    assert result.item_count == 1
    item = result.items[0]
    assert item.attention_kind is HumanAttentionKind.UNCLASSIFIED_SYSTEM_BLOCKER
    assert item.audience is HumanAttentionAudience.OPERATOR
    assert item.resolution_capability is (
        HumanAttentionResolutionCapability.NON_OVERRIDABLE
    )
    assert item.fact_qa_finding_ref is None


async def test_finding_item_identity_order_and_replay_are_stable(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    plan, plans = _plan(home, job_id="job-stable-findings")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    qa_id, qa_hash = "resume-qa-stable", _hash("resume-qa-stable")
    await _deferred_run(
        plan=plan,
        plans=plans,
        runs=runs,
        stage=ApplicationPreparationStage.RESUME_FACT_QA,
        reason=ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
        reason_version=RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
        result_id=qa_id,
        result_hash=qa_hash,
    )
    finding_set = _finding_set(
        plan,
        FactQAMaterialKind.RESUME,
        qa_id,
        qa_hash,
        names=("first", "second", "third"),
    )
    provider = _FindingProvider(
        {(plan.subject_id, qa_id, FactQAMaterialKind.RESUME): finding_set}
    )

    first = _queue(home, provider, subject_id=plan.subject_id)
    second = _queue(home, provider, subject_id=plan.subject_id)

    assert [item.fact_qa_finding_ref.finding_id for item in first.items] == [
        "finding-first",
        "finding-second",
        "finding-third",
    ]
    assert [item.item_id for item in first.items] == [
        item.item_id for item in second.items
    ]
    assert first.queue_snapshot_hash == second.queue_snapshot_hash
