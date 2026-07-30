"""Focused S3g5b2 Base LaTeX registration/replacement tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.input_replacement_resolution import (
    InputReplacementResolutionResult,
    InputReplacementResolutionStatus,
)
from core.new_base_latex_version_replacement import (
    NEW_BASE_LATEX_UPLOAD_MAX_BYTES,
    NewBaseLatexVersionReplacementCommand,
    NewBaseLatexVersionReplacementReceiptRepository,
    NewBaseLatexVersionReplacementStatus,
    register_and_replace_base_latex_version,
)
from core.private_home import PrivateHome
from core.resume_latex_versions import (
    LatexSourceProfile,
    RegisterResumeLatexVersionStatus,
    register_resume_latex_version,
)
from tests.test_human_attention_queue import NOW, SUBJECT
from tests.test_input_replacement_resolution import (
    _latex_case,
    _resume_case,
)


STRICT_SOURCE = r"""\documentclass[11pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{geometry}
\usepackage{enumitem}
\providecommand{\JobopsSection}[2]{\section*{#2}}
\providecommand{\JobopsBullet}[2]{\item #2}
\begin{document}
%% JOBOPS-CONTENT-BEGIN

%% JOBOPS-CONTENT-END
\end{document}
""".encode()


def _delegated(status):
    return InputReplacementResolutionResult(
        status=status,
        receipt=None,
        reason_code=None,
        message="synthetic delegated result",
    )


async def _run(
    *,
    queue,
    item,
    provider,
    versions,
    invocation,
    content,
    registration,
    replacement,
    receipts,
    subject=SUBJECT,
):
    return await register_and_replace_base_latex_version(
        NewBaseLatexVersionReplacementCommand(
            subject_id=subject,
            attention_item_id=item.item_id,
            invocation_id=invocation,
            uploaded_content=content,
            display_label="Uploaded Base Template",
            version_note="Synthetic strict single-file replacement.",
            now=NOW,
        ),
        queue_reader=lambda **_kwargs: queue,
        target_provider=provider,
        registration_callable=registration,
        latex_version_provider=versions,
        replacement_callable=replacement,
        receipt_repository=receipts,
    )


@pytest.mark.asyncio
async def test_valid_source_registers_same_family_then_delegates_once(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "success")
    home.ensure()
    _plan, queue, item, provider, _candidates, versions, old, _replacement = (
        await _latex_case(home)
    )
    registration_calls = []
    delegated_calls = []

    def register(command):
        registration_calls.append(command)
        return register_resume_latex_version(
            command, home=home, repository=versions
        )

    result = await _run(
        queue=queue,
        item=item,
        provider=provider,
        versions=versions,
        invocation="base-latex-upload-success-0001",
        content=STRICT_SOURCE,
        registration=register,
        replacement=lambda command: (
            delegated_calls.append(command)
            or _delegated(
                InputReplacementResolutionStatus
                .REPLACED_AND_PREPARATION_COMPLETED
            )
        ),
        receipts=NewBaseLatexVersionReplacementReceiptRepository(home),
    )

    assert result.status is (
        NewBaseLatexVersionReplacementStatus
        .REGISTERED_AND_REPLACED_COMPLETED
    )
    assert len(registration_calls) == len(delegated_calls) == 1
    command = registration_calls[0]
    assert command.source_profile is (
        LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1
    )
    assert command.root_family_id == old.root_family_id
    assert command.parent_version_id == old.latex_version_id
    registered = versions.get(
        subject_id=SUBJECT,
        latex_version_id=result.receipt.registered_version_id,
    ).version
    assert registered.root_family_id == old.root_family_id
    assert registered.parent_version_id == old.latex_version_id
    assert delegated_calls[0].replacement_option_id == (
        registered.latex_version_id
    )
    assert delegated_calls[0].invocation_id.startswith(
        "input-replacement-child-"
    )


@pytest.mark.asyncio
async def test_unsafe_invalid_or_wrong_target_never_delegates(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "rejected")
    home.ensure()
    _plan, queue, item, provider, _candidates, versions, *_ = (
        await _latex_case(home)
    )
    receipt_repository = NewBaseLatexVersionReplacementReceiptRepository(
        home
    )
    delegated_calls = []

    def register(command):
        return register_resume_latex_version(
            command, home=home, repository=versions
        )

    cases = (
        (
            b"x" * (NEW_BASE_LATEX_UPLOAD_MAX_BYTES + 1),
            "base-latex-upload-oversized-0001",
            NewBaseLatexVersionReplacementStatus.UPLOAD_REJECTED,
        ),
        (
            b"\x00\xffbinary",
            "base-latex-upload-binary-0001",
            NewBaseLatexVersionReplacementStatus.UNSUPPORTED_UPLOAD_TYPE,
        ),
        (
            STRICT_SOURCE.replace(
                b"\\begin{document}",
                b"\\write18{forbidden}\n\\begin{document}",
            ),
            "base-latex-upload-unsafe-0001",
            NewBaseLatexVersionReplacementStatus.UNSAFE_LATEX_SOURCE,
        ),
        (
            STRICT_SOURCE.replace(
                b"%% JOBOPS-CONTENT-END", b""
            ),
            "base-latex-upload-invalid-0001",
            NewBaseLatexVersionReplacementStatus.INVALID_LATEX_SOURCE,
        ),
    )
    for content, invocation, expected in cases:
        result = await _run(
            queue=queue,
            item=item,
            provider=provider,
            versions=versions,
            invocation=invocation,
            content=content,
            registration=register,
            replacement=lambda command: delegated_calls.append(command),
            receipts=receipt_repository,
        )
        assert result.status is expected

    other_home = PrivateHome(tmp_path / "resume-target")
    other_home.ensure()
    (
        _resume_plan,
        resume_queue,
        resume_item,
        resume_provider,
        _resume_candidates,
        resume_versions,
        *_,
    ) = await _resume_case(other_home)
    unsupported = await _run(
        queue=resume_queue,
        item=resume_item,
        provider=resume_provider,
        versions=resume_versions,
        invocation="base-latex-upload-resume-target-0001",
        content=STRICT_SOURCE,
        registration=lambda command: delegated_calls.append(command),
        replacement=lambda command: delegated_calls.append(command),
        receipts=NewBaseLatexVersionReplacementReceiptRepository(other_home),
    )
    cross_subject = await _run(
        queue=queue,
        item=item,
        provider=provider,
        versions=versions,
        invocation="base-latex-upload-cross-subject-0001",
        content=STRICT_SOURCE,
        registration=lambda command: delegated_calls.append(command),
        replacement=lambda command: delegated_calls.append(command),
        receipts=receipt_repository,
        subject="subject-other",
    )

    assert unsupported.status is (
        NewBaseLatexVersionReplacementStatus.UNSUPPORTED_TARGET
    )
    assert cross_subject.status is NewBaseLatexVersionReplacementStatus.FAILED
    assert delegated_calls == []
    assert len(versions.list_selectable(SUBJECT).versions) == 2
    command_fields = set(
        NewBaseLatexVersionReplacementCommand.__dataclass_fields__
    )
    assert command_fields.isdisjoint(
        {
            "root_family_id",
            "parent_version_id",
            "content_hash",
            "source_path",
            "media_type",
        }
    )
    assert "register-and-replace-base-latex" in Path(
        "dashboard/server.py"
    ).read_text(encoding="utf-8")
    javascript = Path("dashboard/static/app.js").read_text(encoding="utf-8")
    assert "specialized correction or replacement capability" in javascript
    assert "register-and-replace-base-latex" not in javascript


@pytest.mark.asyncio
async def test_replay_reuses_version_and_partial_failure_preserves_registration(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "replay")
    home.ensure()
    _plan, queue, item, provider, _candidates, versions, *_ = (
        await _latex_case(home)
    )
    receipts = NewBaseLatexVersionReplacementReceiptRepository(home)
    registration_calls = []
    delegated_calls = []

    def register(command):
        registration_calls.append(command)
        return register_resume_latex_version(
            command, home=home, repository=versions
        )

    def replacement(command):
        delegated_calls.append(command)
        return _delegated(InputReplacementResolutionStatus.TARGET_STALE)

    first = await _run(
        queue=queue,
        item=item,
        provider=provider,
        versions=versions,
        invocation="base-latex-upload-partial-0001",
        content=STRICT_SOURCE,
        registration=register,
        replacement=replacement,
        receipts=receipts,
    )
    replay = await _run(
        queue=queue,
        item=item,
        provider=provider,
        versions=versions,
        invocation="base-latex-upload-partial-0001",
        content=STRICT_SOURCE,
        registration=register,
        replacement=replacement,
        receipts=receipts,
    )
    reused = await _run(
        queue=queue,
        item=item,
        provider=provider,
        versions=versions,
        invocation="base-latex-upload-reuse-0002",
        content=STRICT_SOURCE,
        registration=register,
        replacement=lambda command: (
            delegated_calls.append(command)
            or _delegated(
                InputReplacementResolutionStatus
                .REPLACED_AND_PREPARATION_COMPLETED
            )
        ),
        receipts=receipts,
    )

    assert first.status is (
        NewBaseLatexVersionReplacementStatus.REGISTERED_REPLACEMENT_FAILED
    )
    assert replay.status is NewBaseLatexVersionReplacementStatus.UNCHANGED
    assert replay.receipt == first.receipt
    assert reused.status is (
        NewBaseLatexVersionReplacementStatus
        .EXISTING_CONTENT_REUSED_AND_REPLACED
    )
    assert reused.receipt.registration_status == (
        RegisterResumeLatexVersionStatus.UNCHANGED.value
    )
    assert versions.get(
        subject_id=SUBJECT,
        latex_version_id=first.receipt.registered_version_id,
    ).version is not None
    assert len(registration_calls) == len(delegated_calls) == 2
