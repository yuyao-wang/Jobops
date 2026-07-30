"""Focused S3g4d Cover Letter overflow correction tests."""

from __future__ import annotations

import asyncio
import hashlib

from core.application_preparation_orchestrator import (
    ApplicationPreparationStatus,
    RunApplicationPreparationResult,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.cover_letter_overflow_correction import (
    CoverLetterOverflowCorrectionAction,
    CoverLetterOverflowCorrectionCommand,
    CoverLetterOverflowCorrectionDirectiveProvider,
    CoverLetterOverflowCorrectionReceiptRepository,
    CoverLetterOverflowCorrectionStatus,
    PrivateHomeCoverLetterOverflowCorrectionDirectiveRepository,
    resolve_cover_letter_overflow_correction,
)
from core.cover_letter_overflow_preview import (
    CoverLetterOverflowPreviewStatus,
)
from core.human_attention_queue import HumanAttentionQueueStatus
from core.prepared_cover_letter_material import (
    PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION,
    CoverLetterOverflowCorrectionConstraint,
    CoverLetterOverflowCorrectionConstraintReadResult,
    CoverLetterOverflowCorrectionConstraintStatus,
    PreparedCoverLetterMaterialStatus,
    cover_letter_source_reference,
    reformat_cover_letter_latex,
    render_cover_letter_latex,
)
from dashboard.cover_letter_overflow_correction import (
    CoverLetterOverflowCorrectionUIController,
)
from dashboard.server import app, correct_cover_letter_overflow_ui
from starlette.requests import Request
from tests.test_cover_letter_overflow_preview import (
    _Renderer,
    _overflow_target,
    _provider,
)
from tests.test_human_attention_queue import NOW
from tests.test_material_correction_target import (
    _MissingFindingProvider,
    _queue,
)
from tests.test_prepared_cover_letter_material import (
    _publish,
    _setup,
)
from tests.test_resume_layout_correction_preview import _visual_target


def _completed():
    return RunApplicationPreparationResult(
        ApplicationPreparationStatus.COMPLETED,
        None,
        None,
        False,
        "Synthetic preparation completed.",
    )


def test_current_preview_records_directive_publication_reformats_once(
    tmp_path,
) -> None:
    home, item, target, target_provider, source, compiler = _overflow_target(
        tmp_path / "resolution"
    )
    preview_provider = _provider(
        home, item, target_provider, source, compiler, _Renderer()
    )
    preview = preview_provider.get_or_create_cover_letter_overflow_preview(
        subject_id=item.subject_id,
        correction_target_ref=target.reference,
    )
    assert preview.status is CoverLetterOverflowPreviewStatus.AVAILABLE
    queue = _queue(home, _MissingFindingProvider(), target_provider)
    queue_calls = 0
    preparation_calls = []

    def queue_reader(**_kwargs):
        nonlocal queue_calls
        queue_calls += 1
        return queue

    def prepare(command):
        preparation_calls.append(command)
        return _completed()

    directives = (
        PrivateHomeCoverLetterOverflowCorrectionDirectiveRepository(home)
    )
    receipts = CoverLetterOverflowCorrectionReceiptRepository(home)

    async def correction_callable(command):
        return await resolve_cover_letter_overflow_correction(
            command,
            queue_reader=queue_reader,
            target_provider=target_provider,
            preview_provider=preview_provider,
            directive_repository=directives,
            receipt_repository=receipts,
            preparation_callable=prepare,
        )

    controller = CoverLetterOverflowCorrectionUIController(
        resolver=correction_callable, clock=lambda: NOW
    )
    context = AuthenticatedSubjectContext(
        session_id="session_cover_letter_correction_0123456789",
        subject_id=item.subject_id,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW,
        expires_at=NOW.replace(year=NOW.year + 1),
    )
    app.state.cover_letter_overflow_correction_controller = controller
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/human-attention-inbox/correct-cover-letter",
            "headers": [],
            "app": app,
        }
    )
    payload = asyncio.run(
        correct_cover_letter_overflow_ui(
            item.item_id,
            {"action": "REFORMAT_AND_RETRY"},
            request,
            context,
        )
    )
    replay = asyncio.run(
        correction_callable(
            CoverLetterOverflowCorrectionCommand(
                item.subject_id,
                item.item_id,
                CoverLetterOverflowCorrectionAction.REFORMAT_AND_RETRY,
                NOW,
            )
        )
    )
    assert payload["status"] == "CORRECTED_AND_PREPARATION_COMPLETED"
    assert replay.status is CoverLetterOverflowCorrectionStatus.UNCHANGED
    assert queue_calls == 2
    assert len(preparation_calls) == 1
    directive = directives.get_current(
        subject_id=item.subject_id,
        application_plan_id=item.application_plan_id,
    )
    assert directive.correction_target_ref == target.reference
    assert directive.safe_preview_ref == preview.preview_ref
    assert (
        CoverLetterOverflowCorrectionDirectiveProvider(directives)
        .get_current(
            subject_id=item.subject_id,
            application_plan_id=item.application_plan_id,
        )
        .constraint.directive_id
        == directive.directive_id
    )

    parts = _setup(tmp_path / "publication")
    template = parts["template_provider"].get()
    original_source = render_cover_letter_latex(parts["draft"], template)
    original_hash = hashlib.sha256(original_source.encode()).hexdigest()
    original_reference = cover_letter_source_reference(
        subject_id=parts["plan"].subject_id,
        source_sha256=original_hash,
    )
    parts["home"].write_bytes_if_absent(
        parts["home"].contained_path(original_reference),
        original_source.encode("utf-8"),
    )
    constraint = CoverLetterOverflowCorrectionConstraint(
        directive_id="cover-letter-format-directive-synthetic",
        directive_version=1,
        directive_hash="1" * 64,
        subject_id=parts["plan"].subject_id,
        application_plan_id=parts["plan"].plan_id,
        correction_target_id="material-correction-target-synthetic",
        correction_target_hash="2" * 64,
        safe_preview_id="cover-letter-overflow-preview-synthetic",
        safe_preview_hash="3" * 64,
        publication_result_id="publication-stopped-result-synthetic",
        overflow_evaluation_id=(
            "cover-letter-overflow-evaluation-synthetic"
        ),
        overflow_evaluation_version=(
            PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION
        ),
        source_record_id=f"cover-letter-latex-source-{original_hash}",
        source_version=template.template_version,
        source_content_hash=original_hash,
        correction_mode="REFORMAT_EXISTING_CONTENT",
    )

    class Provider:
        def get_current(self, **_kwargs):
            return CoverLetterOverflowCorrectionConstraintReadResult(
                CoverLetterOverflowCorrectionConstraintStatus.FOUND,
                constraint,
            )

    original_draft = parts["draft"].to_dict()
    publication = _publish(parts, correction_provider=Provider())
    corrected_source = parts["compiler"].compile_calls[0].latex_source
    assert publication.status is PreparedCoverLetterMaterialStatus.CREATED
    assert publication.material.latex_source_sha256 != original_hash
    assert "% JOBOPS_FORMAT_DIRECTIVE" in corrected_source
    assert corrected_source.split(r"\begin{document}", 1)[1] == (
        original_source.split(r"\begin{document}", 1)[1]
    )
    assert parts["draft"].to_dict() == original_draft

    corrected_hash = hashlib.sha256(corrected_source.encode()).hexdigest()
    next_constraint = CoverLetterOverflowCorrectionConstraint(
        directive_id="cover-letter-format-directive-second",
        directive_version=2,
        directive_hash="4" * 64,
        subject_id=parts["plan"].subject_id,
        application_plan_id=parts["plan"].plan_id,
        correction_target_id="material-correction-target-second",
        correction_target_hash="5" * 64,
        safe_preview_id="cover-letter-overflow-preview-second",
        safe_preview_hash="6" * 64,
        publication_result_id="publication-stopped-result-second",
        overflow_evaluation_id="cover-letter-overflow-evaluation-second",
        overflow_evaluation_version=(
            PREPARED_COVER_LETTER_MATERIAL_CONTRACT_VERSION
        ),
        source_record_id=f"cover-letter-latex-source-{corrected_hash}",
        source_version=template.template_version,
        source_content_hash=corrected_hash,
        correction_mode="REFORMAT_EXISTING_CONTENT",
    )
    second_source = reformat_cover_letter_latex(
        corrected_source, parts["draft"], next_constraint
    )
    assert second_source != corrected_source
    assert second_source.split(r"\begin{document}", 1)[1] == (
        original_source.split(r"\begin{document}", 1)[1]
    )


def test_missing_stale_unsupported_and_invalid_actions_do_not_write(
    tmp_path,
) -> None:
    home, item, target, target_provider, source, compiler = _overflow_target(
        tmp_path / "missing"
    )
    preview_provider = _provider(
        home, item, target_provider, source, compiler, _Renderer()
    )
    queue = _queue(home, _MissingFindingProvider(), target_provider)
    directives = (
        PrivateHomeCoverLetterOverflowCorrectionDirectiveRepository(home)
    )
    preparation_calls = []

    async def invoke(command):
        return await resolve_cover_letter_overflow_correction(
            command,
            queue_reader=lambda **_kwargs: queue,
            target_provider=target_provider,
            preview_provider=preview_provider,
            directive_repository=directives,
            receipt_repository=(
                CoverLetterOverflowCorrectionReceiptRepository(home)
            ),
            preparation_callable=lambda command: preparation_calls.append(
                command
            ),
        )

    missing = asyncio.run(
        invoke(
            CoverLetterOverflowCorrectionCommand(
                item.subject_id,
                item.item_id,
                CoverLetterOverflowCorrectionAction.REFORMAT_AND_RETRY,
                NOW,
            )
        )
    )
    invalid = asyncio.run(
        invoke(
            CoverLetterOverflowCorrectionCommand(
                item.subject_id, item.item_id, "RAW_LATEX_PATCH", NOW
            )
        )
    )
    assert missing.status is (
        CoverLetterOverflowCorrectionStatus.PREVIEW_UNAVAILABLE
    )
    assert invalid.status is CoverLetterOverflowCorrectionStatus.INVALID_ACTION

    (
        resume_home,
        resume_item,
        _resume_target,
        resume_target_provider,
        _artifact,
    ) = _visual_target(tmp_path / "unsupported")
    resume_queue = _queue(
        resume_home, _MissingFindingProvider(), resume_target_provider
    )
    unsupported = asyncio.run(
        resolve_cover_letter_overflow_correction(
            CoverLetterOverflowCorrectionCommand(
                resume_item.subject_id,
                resume_item.item_id,
                CoverLetterOverflowCorrectionAction.REFORMAT_AND_RETRY,
                NOW,
            ),
            queue_reader=lambda **_kwargs: resume_queue,
            target_provider=resume_target_provider,
            preview_provider=preview_provider,
            directive_repository=directives,
            receipt_repository=(
                CoverLetterOverflowCorrectionReceiptRepository(home)
            ),
            preparation_callable=lambda command: preparation_calls.append(
                command
            ),
        )
    )
    assert unsupported.status is (
        CoverLetterOverflowCorrectionStatus.UNSUPPORTED_TARGET
    )
    assert directives.get_current(
        subject_id=item.subject_id,
        application_plan_id=item.application_plan_id,
    ) is None
    assert preparation_calls == []


def test_defer_and_failure_keep_directive_without_automatic_loop(
    tmp_path,
) -> None:
    statuses = (
        (
            ApplicationPreparationStatus.DEFERRED,
            CoverLetterOverflowCorrectionStatus
            .CORRECTED_AND_PREPARATION_DEFERRED,
        ),
        (
            ApplicationPreparationStatus.FAILED,
            CoverLetterOverflowCorrectionStatus
            .CORRECTION_RECORDED_PREPARATION_FAILED,
        ),
    )
    for index, (preparation_status, expected) in enumerate(statuses):
        home, item, target, target_provider, source, compiler = (
            _overflow_target(tmp_path / f"case-{index}")
        )
        preview_provider = _provider(
            home, item, target_provider, source, compiler, _Renderer()
        )
        preview_provider.get_or_create_cover_letter_overflow_preview(
            subject_id=item.subject_id,
            correction_target_ref=target.reference,
        )
        queue = _queue(home, _MissingFindingProvider(), target_provider)
        directives = (
            PrivateHomeCoverLetterOverflowCorrectionDirectiveRepository(home)
        )
        calls = []

        def prepare(command):
            calls.append(command)
            return RunApplicationPreparationResult(
                preparation_status,
                None,
                None,
                False,
                "Synthetic stopped preparation.",
            )

        async def invoke():
            return await resolve_cover_letter_overflow_correction(
                CoverLetterOverflowCorrectionCommand(
                    item.subject_id,
                    item.item_id,
                    CoverLetterOverflowCorrectionAction.REFORMAT_AND_RETRY,
                    NOW,
                ),
                queue_reader=lambda **_kwargs: queue,
                target_provider=target_provider,
                preview_provider=preview_provider,
                directive_repository=directives,
                receipt_repository=(
                    CoverLetterOverflowCorrectionReceiptRepository(home)
                ),
                preparation_callable=prepare,
            )

        result = asyncio.run(invoke())
        replay = asyncio.run(invoke())
        assert result.status is expected
        assert replay.status is CoverLetterOverflowCorrectionStatus.UNCHANGED
        assert len(calls) == 1
        assert directives.get_current(
            subject_id=item.subject_id,
            application_plan_id=item.application_plan_id,
        ) is not None
