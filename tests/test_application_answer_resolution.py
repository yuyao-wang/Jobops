"""Focused S3g1 conversational application-answer resolution tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.application_answer_resolution import (
    ApplicationAnswerResolutionCommand,
    ApplicationAnswerResolutionKind,
    ApplicationAnswerResolutionProposal,
    ApplicationAnswerResolutionReceiptRepository,
    ApplicationAnswerResolutionStatus,
    resolve_application_answer,
)
from core.application_answer_taxonomy import (
    CanonicalApplicationAnswerKey,
)
from core.application_attestation import (
    ApplicationAttestationDecision,
    PlanScopedApplicationAttestationRepository,
)
from core.application_fact_writer import (
    ApplicationFactWriteResult,
    ApplicationFactWriteService,
    ApplicationFactWriteStatus,
)
from core.application_preparation_orchestrator import (
    ApplicationPreparationStage,
    ApplicationPreparationStatus,
    RunApplicationPreparationResult,
)
from core.human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionKind,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
)
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
SUBJECT = "subject-resolution-synthetic"


def _raw(cls, **values):
    value = object.__new__(cls)
    for name, item in values.items():
        object.__setattr__(value, name, item)
    return value


def _item(
    kind=HumanAttentionKind.USER_FACT_REQUIRED,
    *,
    audience=HumanAttentionAudience.USER,
    key=CanonicalApplicationAnswerKey.WORK_AUTHORIZATION,
):
    return _raw(
        __import__(
            "core.human_attention_queue", fromlist=["HumanAttentionQueueItem"]
        ).HumanAttentionQueueItem,
        item_id="human-attention-item-" + "a" * 64,
        subject_id=SUBJECT,
        application_plan_id="plan-1",
        job_id="job-1",
        audience=audience,
        attention_kind=kind,
        source_stage=ApplicationPreparationStage.APPLICATION_ANSWERS,
        canonical_answer_key=key,
        required_action="Confirm this application answer.",
        source_record_id="answer-set-1",
    )


def _queue(item):
    return _raw(
        HumanAttentionQueueResult,
        status=HumanAttentionQueueStatus.SUCCEEDED,
        subject_id=SUBJECT,
        items=(item,),
    )


class _Parser:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = 0

    def parse(self, request):
        self.calls += 1
        return self.proposal


class _Writer:
    def __init__(self):
        self.calls = []

    def write_user_confirmed(self, command):
        self.calls.append(command)
        return ApplicationFactWriteResult(
            ApplicationFactWriteStatus.CREATED, "fact-1", None
        )


def _preparation(status):
    class _Run:
        pass

    return _raw(
        RunApplicationPreparationResult,
        status=status,
        run=_raw(_Run, run_id="run-1"),
        reason_code=None,
    )


def _preparation_callable(status, calls=None):
    async def invoke(command):
        if calls is not None:
            calls.append(command)
        return _preparation(status)

    return invoke


@pytest.mark.asyncio
async def test_current_fact_is_user_confirmed_then_preparation_runs_once(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    paths = home.ensure()
    paths.profile_facts.write_text(
        json.dumps(
            {
                "normalized": {},
                "schema_version": 1,
                "subject_id": SUBJECT,
            }
        )
    )
    paths.verified_answers.write_text(
        json.dumps({"answers": {}, "schema_version": 1})
    )
    paths.policy.write_text(json.dumps({"schema_version": 1}))
    parser = _Parser(
        ApplicationAnswerResolutionProposal(
            canonical_key=CanonicalApplicationAnswerKey.WORK_AUTHORIZATION,
            resolution_kind=ApplicationAnswerResolutionKind.FACT,
            value=True,
            evidence_text="Yes, I am authorized to work.",
            unambiguous=True,
        )
    )
    writer = ApplicationFactWriteService(home)
    queue_calls = []
    preparation_calls = []

    def queue_reader(**kwargs):
        queue_calls.append(kwargs)
        return _queue(_item())

    prepare = _preparation_callable(
        ApplicationPreparationStatus.COMPLETED, preparation_calls
    )

    result = await resolve_application_answer(
        ApplicationAnswerResolutionCommand(
            SUBJECT,
            _item().item_id,
            "Yes, I am authorized to work.",
            NOW,
        ),
        queue_reader=queue_reader,
        parser=parser,
        fact_write_service=writer,
        attestation_repository=PlanScopedApplicationAttestationRepository(
            home
        ),
        preparation_callable=prepare,
        receipt_repository=ApplicationAnswerResolutionReceiptRepository(home),
    )

    assert result.status is (
        ApplicationAnswerResolutionStatus
        .RESOLVED_AND_PREPARATION_COMPLETED
    )
    assert len(queue_calls) == parser.calls == 1
    assert len(preparation_calls) == 1
    projected = json.loads(paths.verified_answers.read_text())
    fact = projected["answers"]["work_authorization"]
    assert fact["value"] is True
    assert fact["source_classification"] == "USER_CONFIRMED"
    assert fact["scope"] == {}
    assert preparation_calls[0].now == NOW


@pytest.mark.asyncio
async def test_attestation_is_plan_scoped_and_unsafe_items_do_not_write(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    repository = PlanScopedApplicationAttestationRepository(home)
    receipt_repository = ApplicationAnswerResolutionReceiptRepository(home)
    preparation_calls = []
    attestation = _item(
        HumanAttentionKind.USER_ATTESTATION_REQUIRED,
        key=CanonicalApplicationAnswerKey.ATTESTATION,
    )
    result = await resolve_application_answer(
        ApplicationAnswerResolutionCommand(
            SUBJECT, attestation.item_id, "I personally confirm.", NOW
        ),
        queue_reader=lambda **_: _queue(attestation),
        parser=_Parser(
            ApplicationAnswerResolutionProposal(
                canonical_key=CanonicalApplicationAnswerKey.ATTESTATION,
                resolution_kind=ApplicationAnswerResolutionKind.ATTESTATION,
                attestation_decision=(
                    ApplicationAttestationDecision.CONFIRMED
                ),
                evidence_text="personally confirm",
                unambiguous=True,
            )
        ),
        fact_write_service=_Writer(),
        attestation_repository=repository,
        preparation_callable=_preparation_callable(
            ApplicationPreparationStatus.DEFERRED, preparation_calls
        ),
        receipt_repository=receipt_repository,
    )
    saved = repository.get_current(
        subject_id=SUBJECT,
        application_plan_id="plan-1",
        canonical_key=CanonicalApplicationAnswerKey.ATTESTATION,
    )
    assert result.status is (
        ApplicationAnswerResolutionStatus
        .RESOLVED_AND_PREPARATION_DEFERRED
    )
    assert saved is not None and saved.decision is (
        ApplicationAttestationDecision.CONFIRMED
    )
    assert len(preparation_calls) == 1

    ambiguous = await resolve_application_answer(
        ApplicationAnswerResolutionCommand(
            SUBJECT,
            _item().item_id,
            "Maybe, you decide.",
            NOW,
        ),
        queue_reader=lambda **_: _queue(_item()),
        parser=_Parser(
            ApplicationAnswerResolutionProposal(
                canonical_key=CanonicalApplicationAnswerKey.WORK_AUTHORIZATION,
                resolution_kind=ApplicationAnswerResolutionKind.FACT,
                value=True,
                evidence_text="Maybe",
                unambiguous=True,
            )
        ),
        fact_write_service=_Writer(),
        attestation_repository=repository,
        preparation_callable=lambda _: pytest.fail("must not rerun"),
        receipt_repository=receipt_repository,
    )
    assert ambiguous.status is (
        ApplicationAnswerResolutionStatus.DEFERRED_AMBIGUOUS_INPUT
    )


@pytest.mark.asyncio
async def test_replay_is_unchanged_without_queue_parser_write_or_rerun(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    receipts = ApplicationAnswerResolutionReceiptRepository(home)
    parser = _Parser(
        ApplicationAnswerResolutionProposal(
            canonical_key=CanonicalApplicationAnswerKey.WORK_AUTHORIZATION,
            resolution_kind=ApplicationAnswerResolutionKind.FACT,
            value=True,
            evidence_text="Yes, explicitly authorized to work.",
            unambiguous=True,
        )
    )
    common = dict(
        queue_reader=lambda **_: _queue(_item()),
        parser=parser,
        fact_write_service=_Writer(),
        attestation_repository=PlanScopedApplicationAttestationRepository(
            home
        ),
        preparation_callable=_preparation_callable(
            ApplicationPreparationStatus.DEFERRED
        ),
        receipt_repository=receipts,
    )
    command = ApplicationAnswerResolutionCommand(
        SUBJECT,
        _item().item_id,
        "Yes, explicitly authorized to work.",
        NOW,
    )
    first = await resolve_application_answer(command, **common)

    replay_queue_calls = []

    def replay_queue(**kwargs):
        replay_queue_calls.append(kwargs)
        return _queue(_item())

    replay = await resolve_application_answer(
        command,
        **{
            **common,
            "queue_reader": replay_queue,
            "preparation_callable": lambda _: pytest.fail("must not rerun"),
        },
    )
    assert first.receipt is not None
    assert replay.status is ApplicationAnswerResolutionStatus.UNCHANGED
    assert replay.receipt == first.receipt
    assert parser.calls == 1
    assert replay_queue_calls == [{"subject_id": SUBJECT, "now": NOW}]
    root = Path(__file__).parents[1]
    ui_source = (
        root / "dashboard/application_answer_resolution.py"
    ).read_text()
    server_source = (root / "dashboard/server.py").read_text()
    javascript = (root / "dashboard/static/app.js").read_text()
    assert "application_answer_resolution_controller" in server_source
    assert "resolveHumanAttention" in javascript
    for forbidden in (
        "PermitService",
        "Browser",
        "ApplicationEngine",
        "ATS",
        "submit",
    ):
        assert forbidden not in ui_source
