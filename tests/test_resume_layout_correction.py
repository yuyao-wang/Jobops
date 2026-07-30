"""Focused S3g4c Resume layout correction tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.application_preparation_orchestrator import (
    ApplicationPreparationStatus,
    RunApplicationPreparationResult,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.fact_qa_findings import FactQAMaterialKind
from core.resume_layout_correction import (
    PrivateHomeResumeLayoutCorrectionDirectiveRepository,
    ResumeLayoutCorrectionAction,
    ResumeLayoutCorrectionCommand,
    ResumeLayoutCorrectionDirectiveProvider,
    ResumeLayoutCorrectionReceiptRepository,
    ResumeLayoutCorrectionStatus,
    ResumeLayoutVisualIssue,
    resolve_resume_layout_correction,
)
from core.resume_layout_correction_preview import (
    ResumeLayoutCorrectionPreviewStatus,
)
from core.resume_layout_revision import (
    ResumeLayoutCorrectionConstraint,
    ResumeLayoutCorrectionConstraintReadResult,
    ResumeLayoutRevisionPolicy,
    ReviseResumeLayoutCommand,
    revise_resume_layout,
)
from tests.test_human_attention_queue import NOW
from tests.test_material_correction_target import (
    _MissingFindingProvider,
    _queue,
)
from tests.test_resume_layout_correction_preview import (
    _Renderer,
    _preview_provider,
    _visual_target,
)
from tests.test_resume_layout_revision import (
    METADATA,
    _FakeRevisionAgent,
    _ScriptedCompiler,
    _setup as _layout_setup,
)
from tests.test_unsupported_claim_correction import _current_claim
from dashboard.resume_layout_correction import (
    ResumeLayoutCorrectionUIController,
)
from dashboard.server import app, correct_resume_layout_ui
from starlette.requests import Request


class _QueueReader:
    def __init__(self, queue):
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


@pytest.mark.asyncio
async def test_current_preview_records_directive_calls_p2b4_once_and_replays(
    tmp_path,
) -> None:
    home, item, target, target_provider, artifact = _visual_target(tmp_path)
    preview_provider = _preview_provider(
        home, item, target_provider, artifact, _Renderer()
    )
    preview = (
        preview_provider.get_or_create_resume_layout_correction_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        )
    )
    assert preview.status is ResumeLayoutCorrectionPreviewStatus.AVAILABLE
    queue = _queue(home, _MissingFindingProvider(), target_provider)
    reader = _QueueReader(queue)
    directives = PrivateHomeResumeLayoutCorrectionDirectiveRepository(home)
    receipts = ResumeLayoutCorrectionReceiptRepository(home)
    preparation_calls = []

    def prepare(command):
        preparation_calls.append(command)
        return _preparation(ApplicationPreparationStatus.DEFERRED)

    command = ResumeLayoutCorrectionCommand(
        subject_id=item.subject_id,
        attention_item_id=item.item_id,
        action=ResumeLayoutCorrectionAction.REVISE_LAYOUT_AND_RETRY,
        visual_issues=(
            ResumeLayoutVisualIssue.OVERFLOW_OR_CLIPPING,
            ResumeLayoutVisualIssue.POOR_READABILITY,
        ),
        now=NOW,
    )
    result = await resolve_resume_layout_correction(
        command,
        queue_reader=reader,
        target_provider=target_provider,
        preview_provider=preview_provider,
        directive_repository=directives,
        receipt_repository=receipts,
        preparation_callable=prepare,
    )
    replay = await resolve_resume_layout_correction(
        command,
        queue_reader=reader,
        target_provider=target_provider,
        preview_provider=preview_provider,
        directive_repository=directives,
        receipt_repository=receipts,
        preparation_callable=prepare,
    )
    assert result.status is (
        ResumeLayoutCorrectionStatus.CORRECTED_AND_PREPARATION_DEFERRED
    )
    assert result.receipt.safe_preview_ref == preview.preview_ref
    current = directives.get_current(
        subject_id=item.subject_id,
        application_plan_id=item.application_plan_id,
    )
    assert current.mode.value == "REVISE_FROM_VISUAL_QA_DIRECTIVE"
    assert current.visual_issues == command.visual_issues
    assert replay.status is ResumeLayoutCorrectionStatus.UNCHANGED
    assert len(preparation_calls) == 1
    assert reader.calls == 2
    async def correction_callable(command):
        return await resolve_resume_layout_correction(
            command,
            queue_reader=reader,
            target_provider=target_provider,
            preview_provider=preview_provider,
            directive_repository=directives,
            receipt_repository=receipts,
            preparation_callable=prepare,
        )

    app.state.resume_layout_correction_controller = (
        ResumeLayoutCorrectionUIController(
            correction_callable=correction_callable, clock=lambda: NOW
        )
    )
    context = AuthenticatedSubjectContext(
        session_id="session_reference_layout_0123456789",
        subject_id=item.subject_id,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW,
        expires_at=NOW.replace(year=NOW.year + 1),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/human-attention-inbox/layout",
            "headers": [],
            "app": app,
        }
    )
    route = await correct_resume_layout_ui(
        item.item_id,
        {
            "action": "REVISE_LAYOUT_AND_RETRY",
            "visual_issues": [
                "OVERFLOW_OR_CLIPPING",
                "POOR_READABILITY",
            ],
        },
        request,
        context,
    )
    assert route["status"] == "UNCHANGED"
    assert len(preparation_calls) == 1


@pytest.mark.asyncio
async def test_missing_preview_invalid_issue_and_unsupported_target_do_not_write(
    tmp_path,
) -> None:
    home, item, _target, target_provider, artifact = _visual_target(
        tmp_path / "layout"
    )
    preview_provider = _preview_provider(
        home, item, target_provider, artifact, _Renderer()
    )
    queue = _queue(home, _MissingFindingProvider(), target_provider)
    directives = PrivateHomeResumeLayoutCorrectionDirectiveRepository(home)
    calls = []
    missing = await resolve_resume_layout_correction(
        ResumeLayoutCorrectionCommand(
            item.subject_id,
            item.item_id,
            ResumeLayoutCorrectionAction.REVISE_LAYOUT_AND_RETRY,
            (),
            NOW,
        ),
        queue_reader=_QueueReader(queue),
        target_provider=target_provider,
        preview_provider=preview_provider,
        directive_repository=directives,
        receipt_repository=ResumeLayoutCorrectionReceiptRepository(home),
        preparation_callable=lambda command: calls.append(command),
    )
    invalid = await resolve_resume_layout_correction(
        SimpleNamespace(
            subject_id=item.subject_id,
            attention_item_id=item.item_id,
            action="REVISE_LAYOUT_AND_RETRY",
            visual_issues=("RAW_CSS_PATCH",),
            now=NOW,
        ),
        queue_reader=_QueueReader(queue),
        target_provider=target_provider,
        preview_provider=preview_provider,
        directive_repository=directives,
        receipt_repository=ResumeLayoutCorrectionReceiptRepository(home),
        preparation_callable=lambda command: calls.append(command),
    )
    claim_home, _plan, claim_queue, claim_targets = _current_claim(
        tmp_path / "claim", material=FactQAMaterialKind.RESUME
    )
    unsupported = await resolve_resume_layout_correction(
        ResumeLayoutCorrectionCommand(
            claim_queue.subject_id,
            claim_queue.items[0].item_id,
            ResumeLayoutCorrectionAction.REVISE_LAYOUT_AND_RETRY,
            (),
            NOW,
        ),
        queue_reader=_QueueReader(claim_queue),
        target_provider=claim_targets,
        preview_provider=preview_provider,
        directive_repository=(
            PrivateHomeResumeLayoutCorrectionDirectiveRepository(claim_home)
        ),
        receipt_repository=ResumeLayoutCorrectionReceiptRepository(
            claim_home
        ),
        preparation_callable=lambda command: calls.append(command),
    )
    assert missing.status is ResumeLayoutCorrectionStatus.PREVIEW_UNAVAILABLE
    assert invalid.status is ResumeLayoutCorrectionStatus.INVALID_ACTION
    assert unsupported.status is (
        ResumeLayoutCorrectionStatus.UNSUPPORTED_TARGET
    )
    assert directives.get_current(
        subject_id=item.subject_id,
        application_plan_id=item.application_plan_id,
    ) is None
    assert calls == []


@pytest.mark.asyncio
async def test_p2a8b_directive_starts_new_bounded_content_preserving_lineage(
    tmp_path,
) -> None:
    parts = await _layout_setup(
        tmp_path, compiler=_ScriptedCompiler([2] * 10)
    )
    sequence = {"value": 0}

    def revise_source(context):
        sequence["value"] += 1
        return context.latex_source.replace(
            "\\begin{document}",
            f"% layout-pass-{sequence['value']}\n\\begin{{document}}",
        )

    first_agent = _FakeRevisionAgent(revise_source)
    first = await revise_resume_layout(
        ReviseResumeLayoutCommand(
            "subject-a", parts["initial_qa"].result_id, NOW
        ),
        visual_qa_repository=parts["visual_qa_repository"],
        compilation_repository=parts["compilation_repository"],
        latex_version_repository=parts["latex_repository"],
        provenance_repository=parts["provenance"],
        revision_record_repository=parts["revision_records"],
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        renderer=parts["renderer"],
        agent=first_agent,
        metadata=METADATA,
        compile_step=parts["compile_step"],
        review_step=parts["review_step"],
        revision_repository=parts["revision_repository"],
        policy=ResumeLayoutRevisionPolicy(max_attempts=3),
        home=parts["home"],
    )
    final_compilation = parts["compilation_repository"].get(
        subject_id="subject-a",
        record_id=first.run.final_compilation_record_id,
    ).record
    constraint = ResumeLayoutCorrectionConstraint(
        directive_id="resume-layout-correction-" + "1" * 64,
        directive_hash="1" * 64,
        subject_id="subject-a",
        application_plan_id=parts["draft"].application_plan_id,
        source_visual_qa_result_id=first.run.final_visual_qa_result_id,
        source_artifact_id=final_compilation.record_id,
        source_artifact_content_hash=final_compilation.pdf_sha256,
        source_latex_version_id=final_compilation.latex_version_id,
        source_latex_content_hash=final_compilation.latex_source_sha256,
        preview_id="resume-layout-preview-" + "2" * 64,
        preview_hash="2" * 64,
        correction_mode="RESTART_BOUNDED_LAYOUT_REVISION",
        visual_issue_selections=("OVERFLOW_OR_CLIPPING",),
        previous_layout_run_id=first.run.run_id,
        previous_final_attempt_id=(
            f"{first.run.run_id}:attempt:{len(first.run.attempts)}"
        ),
    )

    class _Provider:
        def get_current(self, **_kwargs):
            return ResumeLayoutCorrectionConstraintReadResult(
                True, constraint
            )

    second_agent = _FakeRevisionAgent(revise_source)
    second = await revise_resume_layout(
        ReviseResumeLayoutCommand(
            "subject-a",
            parts["initial_qa"].result_id,
            NOW,
            application_plan_id=parts["draft"].application_plan_id,
        ),
        visual_qa_repository=parts["visual_qa_repository"],
        compilation_repository=parts["compilation_repository"],
        latex_version_repository=parts["latex_repository"],
        provenance_repository=parts["provenance"],
        revision_record_repository=parts["revision_records"],
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        renderer=parts["renderer"],
        agent=second_agent,
        metadata=METADATA,
        compile_step=parts["compile_step"],
        review_step=parts["review_step"],
        revision_repository=parts["revision_repository"],
        policy=ResumeLayoutRevisionPolicy(max_attempts=3),
        home=parts["home"],
        correction_provider=_Provider(),
    )
    assert second.run.run_id != first.run.run_id
    assert len(second.run.attempts) == second.run.max_attempts == 3
    assert second_agent.contexts[0].correction_mode == (
        "RESTART_BOUNDED_LAYOUT_REVISION"
    )
    assert second_agent.contexts[0].visual_issue_selections == (
        "OVERFLOW_OR_CLIPPING",
    )
    from core.resume_latex_markers import split_controlled_region

    initial_source = parts["home"].contained_path(
        parts["version"].source_reference
    ).read_text(encoding="utf-8")
    final_version = parts["latex_repository"].get(
        subject_id="subject-a",
        latex_version_id=second.run.final_latex_version_id,
    ).version
    final_source = parts["home"].contained_path(
        final_version.source_reference
    ).read_text(encoding="utf-8")
    assert split_controlled_region(final_source)[1] == (
        split_controlled_region(initial_source)[1]
    )
