"""Focused S3g5b1 new ResumeCandidate registration/replacement tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.input_replacement_resolution import (
    InputReplacementResolutionResult,
    InputReplacementResolutionStatus,
)
from core.new_resume_candidate_replacement import (
    NEW_RESUME_UPLOAD_MAX_BYTES,
    NewResumeCandidateReplacementCommand,
    NewResumeCandidateReplacementReceiptRepository,
    NewResumeCandidateReplacementStatus,
    register_and_replace_resume_candidate,
)
from core.resume_candidates import (
    RegisterResumeCandidateStatus,
    register_resume_candidate,
)
from tests.test_human_attention_queue import NOW, SUBJECT
from tests.test_input_replacement_resolution import (
    _latex_case,
    _resume_case,
)
from core.private_home import PrivateHome


def _delegated(status):
    return InputReplacementResolutionResult(
        status=status,
        receipt=None,
        reason_code=None,
        message="synthetic delegated result",
    )


async def _run(
    *,
    home,
    queue,
    item,
    provider,
    candidates,
    invocation,
    content,
    registration,
    replacement,
):
    return await register_and_replace_resume_candidate(
        NewResumeCandidateReplacementCommand(
            subject_id=SUBJECT,
            attention_item_id=item.item_id,
            invocation_id=invocation,
            uploaded_content=content,
            display_name="Uploaded Replacement Resume",
            now=NOW,
        ),
        queue_reader=lambda **_kwargs: queue,
        target_provider=provider,
        registration_callable=registration,
        candidate_provider=candidates,
        replacement_callable=replacement,
        receipt_repository=(
            NewResumeCandidateReplacementReceiptRepository(home)
        ),
        home=home,
    )


@pytest.mark.asyncio
async def test_valid_upload_registers_then_delegates_once_with_child_invocation(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "success")
    home.ensure()
    _plan, queue, item, provider, candidates, _versions, _old, _existing = (
        await _resume_case(home)
    )
    registration_calls = []
    delegated_calls = []
    content = b"%PDF-1.7\nsynthetic new replacement\n%%EOF\n"

    def register(command):
        registration_calls.append(command)
        return register_resume_candidate(
            command, home=home, repository=candidates
        )

    result = await _run(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        invocation="resume-upload-success-0001",
        content=content,
        registration=register,
        replacement=lambda command: (
            delegated_calls.append(command)
            or _delegated(
                InputReplacementResolutionStatus
                .REPLACED_AND_PREPARATION_COMPLETED
            )
        ),
    )

    assert result.status is (
        NewResumeCandidateReplacementStatus
        .REGISTERED_AND_REPLACED_COMPLETED
    )
    assert len(registration_calls) == len(delegated_calls) == 1
    assert delegated_calls[0].replacement_option_id == (
        result.receipt.candidate_id
    )
    assert delegated_calls[0].invocation_id.startswith(
        "input-replacement-child-"
    )
    assert result.receipt.delegated_invocation_id == (
        delegated_calls[0].invocation_id
    )
    assert candidates.get(
        subject_id=SUBJECT, resume_id=result.receipt.candidate_id
    ).candidate is not None
    assert not tuple(
        (
            home.paths.master_documents
            / ".resume-candidate-upload-staging"
        ).glob("*")
    )


@pytest.mark.asyncio
async def test_rejected_uploads_and_latex_target_never_register_or_delegate(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "rejected")
    home.ensure()
    _plan, queue, item, provider, candidates, _versions, *_ = (
        await _resume_case(home)
    )
    calls = []
    invalid = await _run(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        invocation="resume-upload-invalid-0001",
        content=b"not a PDF despite client naming",
        registration=lambda command: calls.append(("register", command)),
        replacement=lambda command: calls.append(("replace", command)),
    )
    oversized = await _run(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        invocation="resume-upload-oversized-0001",
        content=b"x" * (NEW_RESUME_UPLOAD_MAX_BYTES + 1),
        registration=lambda command: calls.append(("register", command)),
        replacement=lambda command: calls.append(("replace", command)),
    )

    latex_home = PrivateHome(tmp_path / "latex")
    latex_home.ensure()
    (
        _latex_plan,
        latex_queue,
        latex_item,
        latex_provider,
        latex_candidates,
        _latex_versions,
        *_,
    ) = await _latex_case(latex_home)
    latex = await _run(
        home=latex_home,
        queue=latex_queue,
        item=latex_item,
        provider=latex_provider,
        candidates=latex_candidates,
        invocation="resume-upload-latex-0001",
        content=b"%PDF-1.7\nvalid but wrong target\n%%EOF\n",
        registration=lambda command: calls.append(("register", command)),
        replacement=lambda command: calls.append(("replace", command)),
    )

    assert invalid.status is (
        NewResumeCandidateReplacementStatus.UNSUPPORTED_MEDIA_TYPE
    )
    assert oversized.status is (
        NewResumeCandidateReplacementStatus.UPLOAD_REJECTED
    )
    assert latex.status is (
        NewResumeCandidateReplacementStatus.UNSUPPORTED_TARGET
    )
    assert calls == []


@pytest.mark.asyncio
async def test_replay_reuses_content_and_partial_failure_keeps_candidate(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "replay")
    home.ensure()
    _plan, queue, item, provider, candidates, _versions, _old, existing = (
        await _resume_case(home)
    )
    existing_content = home.contained_path(
        existing.artifact_reference
    ).read_bytes()
    registration_calls = []
    delegated_calls = []

    def register(command):
        registration_calls.append(command)
        return register_resume_candidate(
            command, home=home, repository=candidates
        )

    first = await _run(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        invocation="resume-upload-reuse-0001",
        content=existing_content,
        registration=register,
        replacement=lambda command: (
            delegated_calls.append(command)
            or _delegated(
                InputReplacementResolutionStatus
                .REPLACED_AND_PREPARATION_COMPLETED
            )
        ),
    )
    replay = await _run(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        invocation="resume-upload-reuse-0001",
        content=existing_content,
        registration=register,
        replacement=lambda command: (
            delegated_calls.append(command)
            or _delegated(InputReplacementResolutionStatus.FAILED)
        ),
    )
    failed = await _run(
        home=home,
        queue=queue,
        item=item,
        provider=provider,
        candidates=candidates,
        invocation="resume-upload-partial-0001",
        content=b"%PDF-1.7\npartial retained candidate\n%%EOF\n",
        registration=register,
        replacement=lambda command: (
            delegated_calls.append(command)
            or _delegated(InputReplacementResolutionStatus.TARGET_STALE)
        ),
    )

    assert first.status is (
        NewResumeCandidateReplacementStatus
        .EXISTING_CONTENT_REUSED_AND_REPLACED
    )
    assert first.receipt.registration_status == (
        RegisterResumeCandidateStatus.UNCHANGED.value
    )
    assert replay.status is NewResumeCandidateReplacementStatus.UNCHANGED
    assert replay.receipt == first.receipt
    assert failed.status is (
        NewResumeCandidateReplacementStatus.REGISTERED_REPLACEMENT_FAILED
    )
    assert candidates.get(
        subject_id=SUBJECT, resume_id=failed.receipt.candidate_id
    ).candidate is not None
    assert len(registration_calls) == len(delegated_calls) == 2
