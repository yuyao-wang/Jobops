"""Focused S3g4a unsupported-claim correction tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application_preparation_orchestrator import (
    COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    ApplicationPreparationStatus,
    CoverLetterFactQAStopReason,
    PrivateHomeApplicationPreparationRunRepository,
    ResumeFactQAStopReason,
    RunApplicationPreparationResult,
)
from core.cover_letter_draft import draft_cover_letter, DraftCoverLetterCommand
from core.fact_qa_findings import FactQAMaterialKind
from core.material_correction_target import (
    MaterialCorrectionTargetStatus,
    MaterialCorrectionTypedTargetResult,
)
from core.private_home import PrivateHome
from core.resume_tailoring import TailorResumeCommand, tailor_resume
from core.unsupported_claim_correction import (
    PrivateHomeUnsupportedClaimCorrectionDirectiveRepository,
    UnsupportedClaimCorrectionAction,
    UnsupportedClaimCorrectionCommand,
    UnsupportedClaimCorrectionConstraint,
    UnsupportedClaimCorrectionDirectiveProvider,
    UnsupportedClaimCorrectionDirectiveSetResult,
    UnsupportedClaimCorrectionReceiptRepository,
    UnsupportedClaimCorrectionStatus,
    resolve_unsupported_claim_correction,
)
from tests.test_application_preparation_orchestrator import _hash
from tests.test_cover_letter_draft import (
    METADATA as COVER_METADATA,
    _FakeCoverLetterAgent,
    _default_output as _cover_output,
    _setup as _cover_setup,
)
from tests.test_human_attention_queue import NOW, _plan
from tests.test_material_correction_target import (
    _FindingProvider,
    _deferred_run,
    _finding_set,
    _queue,
    _target_provider,
)
from tests.test_resume_tailoring import (
    METADATA as RESUME_METADATA,
    _FakeTailoringAgent,
    _default_output as _resume_output,
    _setup as _resume_setup,
)


def _current_claim(tmp_path, *, material: FactQAMaterialKind):
    home = PrivateHome(tmp_path / material.value.lower())
    plan, plans = _plan(
        home, job_id=f"job-{material.value.lower()}-correction"
    )
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    qa_id = f"{material.value.lower()}-qa-correction"
    qa_hash = _hash(qa_id)
    if material is FactQAMaterialKind.RESUME:
        stage = ApplicationPreparationStage.RESUME_FACT_QA
        reason = ResumeFactQAStopReason.UNSUPPORTED_CLAIM
        version = RESUME_FACT_QA_STOP_REASON_CONTRACT_VERSION
    else:
        stage = ApplicationPreparationStage.COVER_LETTER_FACT_QA
        reason = CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM
        version = COVER_LETTER_FACT_QA_STOP_REASON_CONTRACT_VERSION
    _deferred_run(
        plan=plan,
        plans=plans,
        runs=runs,
        stage=stage,
        reason=reason,
        reason_version=version,
        result_id=qa_id,
        result_hash=qa_hash,
    )
    finding_set = _finding_set(
        plan, material, qa_id, qa_hash, names=("exact",)
    )
    findings = _FindingProvider(
        {
            (
                plan.subject_id,
                qa_id,
                material,
            ): finding_set
        }
    )
    target_provider = _target_provider(
        home, finding_provider=findings
    )
    queue = _queue(home, findings, target_provider)
    return home, plan, queue, target_provider


def _preparation(status: ApplicationPreparationStatus):
    result = object.__new__(RunApplicationPreparationResult)
    object.__setattr__(result, "status", status)
    object.__setattr__(
        result, "run", SimpleNamespace(run_id=f"run-{status.value.lower()}")
    )
    object.__setattr__(result, "reason_code", None)
    return result


class _Queue:
    def __init__(self, queue):
        self.queue = queue
        self.calls = 0

    def __call__(self, **_kwargs):
        self.calls += 1
        return self.queue


@pytest.mark.asyncio
async def test_resume_and_cover_claims_save_exact_directives_and_rerun_once(
    tmp_path,
) -> None:
    for material, action in (
        (
            FactQAMaterialKind.RESUME,
            UnsupportedClaimCorrectionAction.REMOVE_UNSUPPORTED_CLAIM,
        ),
        (
            FactQAMaterialKind.COVER_LETTER,
            UnsupportedClaimCorrectionAction
            .REWRITE_USING_EXISTING_EVIDENCE,
        ),
    ):
        home, plan, queue, targets = _current_claim(
            tmp_path, material=material
        )
        queue_reader = _Queue(queue)
        preparation_calls = []

        def prepare(command):
            preparation_calls.append(command)
            return _preparation(ApplicationPreparationStatus.COMPLETED)

        directives = (
            PrivateHomeUnsupportedClaimCorrectionDirectiveRepository(home)
        )
        result = await resolve_unsupported_claim_correction(
            UnsupportedClaimCorrectionCommand(
                subject_id=plan.subject_id,
                attention_item_id=queue.items[0].item_id,
                action=action,
                instruction=(
                    "Keep only claims supported by current evidence."
                    if material is FactQAMaterialKind.COVER_LETTER
                    else None
                ),
                now=NOW,
            ),
            queue_reader=queue_reader,
            target_provider=targets,
            directive_repository=directives,
            preparation_callable=prepare,
            receipt_repository=(
                UnsupportedClaimCorrectionReceiptRepository(home)
            ),
        )

        assert result.status is (
            UnsupportedClaimCorrectionStatus
            .CORRECTED_AND_PREPARATION_COMPLETED
        )
        assert queue_reader.calls == 1
        assert len(preparation_calls) == 1
        assert preparation_calls[0].subject_id == plan.subject_id
        current = directives.get_current(
            subject_id=plan.subject_id,
            application_plan_id=plan.plan_id,
            material_kind=material,
            finding_id="finding-exact",
        )
        assert current.finding_ref == queue.items[0].fact_qa_finding_ref
        assert current.correction_target_ref == (
            queue.items[0].correction_target_ref
        )
        assert current.action is action


@pytest.mark.asyncio
async def test_both_draft_stages_consume_only_bound_non_evidence_constraints(
    tmp_path,
) -> None:
    constraint = UnsupportedClaimCorrectionConstraint(
        directive_id="unsupported-claim-correction-" + "a" * 64,
        directive_hash="a" * 64,
        material_kind=FactQAMaterialKind.RESUME,
        finding_id="finding-exact",
        action=(
            UnsupportedClaimCorrectionAction
            .REWRITE_USING_EXISTING_EVIDENCE
        ),
        claim_summary="Unsupported synthetic claim.",
        instruction="Use a shorter sentence.",
    )

    class _Provider:
        def list_current(self, **kwargs):
            material = FactQAMaterialKind(kwargs["material_kind"])
            value = (
                constraint
                if material is FactQAMaterialKind.RESUME
                else UnsupportedClaimCorrectionConstraint(
                    directive_id=constraint.directive_id,
                    directive_hash=constraint.directive_hash,
                    material_kind=FactQAMaterialKind.COVER_LETTER,
                    finding_id=constraint.finding_id,
                    action=constraint.action,
                    claim_summary=constraint.claim_summary,
                    instruction=constraint.instruction,
                )
            )
            return UnsupportedClaimCorrectionDirectiveSetResult(
                True, (SimpleNamespace(constraint=value),)
            )

    resume = _resume_setup(tmp_path / "resume")
    resume_agent = _FakeTailoringAgent(_resume_output(resume))
    await tailor_resume(
        TailorResumeCommand(
            subject_id=resume["plan"].subject_id,
            application_plan_id=resume["plan"].plan_id,
            evidence_snapshot_id=resume["snapshot"].snapshot_id,
            now=NOW,
        ),
        application_plan_repository=resume["plan_repository"],
        job_repository=resume["job_repository"],
        selection_repository=resume["selection_repository"],
        candidate_repository=resume["candidate_repository"],
        projection_repository=resume["projection_repository"],
        evidence_snapshot_repository=resume["snapshot_repository"],
        agent=resume_agent,
        metadata=RESUME_METADATA,
        draft_repository=resume["draft_repository"],
        correction_provider=_Provider(),
    )
    cover = _cover_setup(tmp_path / "cover")
    cover_agent = _FakeCoverLetterAgent(_cover_output(cover))
    await draft_cover_letter(
        DraftCoverLetterCommand(
            subject_id=cover["plan"].subject_id,
            application_plan_id=cover["plan"].plan_id,
            cover_letter_evidence_snapshot_id=cover["snapshot"].snapshot_id,
            now=NOW,
        ),
        application_plan_repository=cover["plan_repository"],
        job_repository=cover["job_repository"],
        evidence_snapshot_repository=cover["snapshot_repository"],
        agent=cover_agent,
        metadata=COVER_METADATA,
        draft_repository=cover["draft_repository"],
        correction_provider=_Provider(),
    )

    for context, material in (
        (resume_agent.contexts[0], FactQAMaterialKind.RESUME),
        (cover_agent.contexts[0], FactQAMaterialKind.COVER_LETTER),
    ):
        assert context.correction_constraints[0].material_kind is material
        assert context.correction_constraints[0].instruction == (
            "Use a shorter sentence."
        )
        assert all(
            "Use a shorter sentence." not in item.evidence_text
            for item in context.evidence_items
        )


@pytest.mark.asyncio
async def test_replay_stale_unsupported_defer_and_failure_fail_closed(
    tmp_path,
) -> None:
    home, plan, queue, targets = _current_claim(
        tmp_path, material=FactQAMaterialKind.RESUME
    )
    directives = PrivateHomeUnsupportedClaimCorrectionDirectiveRepository(
        home
    )
    receipts = UnsupportedClaimCorrectionReceiptRepository(home)
    calls = []
    statuses = iter(
        (
            ApplicationPreparationStatus.DEFERRED,
            ApplicationPreparationStatus.FAILED,
        )
    )

    def prepare(command):
        calls.append(command)
        return _preparation(next(statuses))

    base = dict(
        subject_id=plan.subject_id,
        attention_item_id=queue.items[0].item_id,
        now=NOW,
    )
    first = await resolve_unsupported_claim_correction(
        UnsupportedClaimCorrectionCommand(
            action=(
                UnsupportedClaimCorrectionAction
                .REMOVE_UNSUPPORTED_CLAIM
            ),
            instruction=None,
            **base,
        ),
        queue_reader=_Queue(queue),
        target_provider=targets,
        directive_repository=directives,
        preparation_callable=prepare,
        receipt_repository=receipts,
    )
    replay = await resolve_unsupported_claim_correction(
        UnsupportedClaimCorrectionCommand(
            action=(
                UnsupportedClaimCorrectionAction
                .REMOVE_UNSUPPORTED_CLAIM
            ),
            instruction=None,
            **base,
        ),
        queue_reader=_Queue(queue),
        target_provider=targets,
        directive_repository=directives,
        preparation_callable=prepare,
        receipt_repository=receipts,
    )
    changed = await resolve_unsupported_claim_correction(
        UnsupportedClaimCorrectionCommand(
            action=(
                UnsupportedClaimCorrectionAction
                .REWRITE_USING_EXISTING_EVIDENCE
            ),
            instruction="Use only the existing evidence.",
            **base,
        ),
        queue_reader=_Queue(queue),
        target_provider=targets,
        directive_repository=directives,
        preparation_callable=prepare,
        receipt_repository=receipts,
    )

    assert first.status is (
        UnsupportedClaimCorrectionStatus
        .CORRECTED_AND_PREPARATION_DEFERRED
    )
    assert replay.status is UnsupportedClaimCorrectionStatus.UNCHANGED
    assert changed.status is (
        UnsupportedClaimCorrectionStatus
        .CORRECTION_RECORDED_PREPARATION_FAILED
    )
    assert len(calls) == 2
    current = directives.get_current(
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        material_kind=FactQAMaterialKind.RESUME,
        finding_id="finding-exact",
    )
    assert current.directive_version == 2
    assert current.previous_directive_id is not None

    class _Stale:
        def get_current_typed_target(self, **_kwargs):
            return MaterialCorrectionTypedTargetResult(
                MaterialCorrectionTargetStatus.TARGET_STALE, None
            )

    stale = await resolve_unsupported_claim_correction(
        UnsupportedClaimCorrectionCommand(
            action=(
                UnsupportedClaimCorrectionAction
                .REMOVE_UNSUPPORTED_CLAIM
            ),
            instruction="different",
            **base,
        ),
        queue_reader=_Queue(queue),
        target_provider=_Stale(),
        directive_repository=directives,
        preparation_callable=prepare,
        receipt_repository=receipts,
    )

    class _Unsupported:
        def get_current_typed_target(self, **_kwargs):
            return MaterialCorrectionTypedTargetResult(
                MaterialCorrectionTargetStatus.AVAILABLE,
                SimpleNamespace(payload=object()),
            )

    unsupported = await resolve_unsupported_claim_correction(
        UnsupportedClaimCorrectionCommand(
            action=(
                UnsupportedClaimCorrectionAction
                .REMOVE_UNSUPPORTED_CLAIM
            ),
            instruction="another",
            **base,
        ),
        queue_reader=_Queue(queue),
        target_provider=_Unsupported(),
        directive_repository=directives,
        preparation_callable=prepare,
        receipt_repository=receipts,
    )
    assert stale.status is UnsupportedClaimCorrectionStatus.TARGET_STALE
    assert unsupported.status is (
        UnsupportedClaimCorrectionStatus.UNSUPPORTED_TARGET
    )
    assert len(calls) == 2
