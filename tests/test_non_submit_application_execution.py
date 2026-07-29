"""Focused P2c3 plan-scoped Gate A and non-submit execution tests."""

from __future__ import annotations

import ast
import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.registry import AdapterRegistry, AdapterRunRequest
from core.non_submit_application_execution import (
    ExecuteNonSubmitApplicationCommand,
    GateAOutcome,
    NonSubmitApplicationExecutionFailureReason,
    NonSubmitApplicationExecutionStatus,
    NonSubmitExecutionMetadata,
    NonSubmitExecutionRecordState,
    PrivateHomeNonSubmitApplicationExecutionRepository,
    execute_non_submit_application,
)
from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.policy import ApprovalActor
from core.permits import GateAConsumptionReference

from test_application_bundle_assembly import (
    NOW,
    SUBJECT_ID,
    _run as _assemble,
    _setup as _assembly_setup,
)


EXECUTION_NOW = NOW + timedelta(days=1)


class _BrowserProvider:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.calls = 0
        self.unavailable = unavailable

    @asynccontextmanager
    async def lease(self, *, owner: str):
        self.calls += 1
        if self.unavailable:
            raise RuntimeError("synthetic Browser unavailable")
        yield SimpleNamespace(
            page=object(),
            lease=SimpleNamespace(owner=owner, token="synthetic-lease"),
        )


class _Engine:
    def __init__(self, outcome: ApplicationOutcome | None = None) -> None:
        self.calls: list[dict] = []
        self.outcome = outcome
        self._job_id = ""

    def bind(self, bundle) -> None:
        self._job_id = bundle.job.job_id

    def gate_a_consumption_reference(
        self, run_id: str
    ) -> GateAConsumptionReference | None:
        if not self._job_id:
            return None
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        return GateAConsumptionReference.create(
            permit_jti=f"gate-a-{digest}",
            run_id=run_id,
            job_id=self._job_id,
            bindings_digest="a" * 64,
            claims_hash="b" * 64,
            consumed_at="2026-08-06T16:00:00Z",
            consumer="P2C3_NON_SUBMIT_EXECUTION",
            action="PREPARE_REVIEW",
        )

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        bundle = kwargs["bundle"]
        return self.outcome or ApplicationOutcome.review_ready(
            run_id=bundle.run_id,
            job_id=bundle.job.job_id,
            adapter="synthetic",
            checkpoint="b" * 64,
            details={
                "review": {
                    "filled_fields": ["email"],
                    "unresolved_required": [],
                }
            },
        )


def _setup(tmp_path: Path):
    parts = _assembly_setup(tmp_path)
    assembled = _assemble(parts)
    assert assembled.record is not None
    assert assembled.bundle is not None
    execution_repository = (
        PrivateHomeNonSubmitApplicationExecutionRepository(parts["home"])
    )
    return parts, assembled, execution_repository


def _command(assembled, **overrides):
    values = {
        "subject_id": SUBJECT_ID,
        "application_bundle_assembly_record_id": assembled.record.record_id,
        "now": EXECUTION_NOW,
        "approve_gate_a": True,
    }
    values.update(overrides)
    return ExecuteNonSubmitApplicationCommand(**values)


async def _execute(
    parts,
    assembled,
    repository,
    *,
    engine=None,
    browser=None,
    command=None,
):
    engine = engine or _Engine()
    if isinstance(engine, _Engine):
        engine.bind(assembled.bundle)
    browser = browser or _BrowserProvider()
    result = await execute_non_submit_application(
        command or _command(assembled),
        application_plan_repository=parts["plan_repository"],
        assembly_repository=parts["assembly_repository"],
        bundle_envelope_repository=parts["envelope_repository"],
        job_posting_repository=parts["job_repository"],
        browser_lease_provider=browser,
        application_engine=engine,
        execution_repository=repository,
        private_home=parts["home"],
        execution_metadata=NonSubmitExecutionMetadata.default(),
    )
    return result, engine, browser


@pytest.mark.asyncio
async def test_authorized_assembly_runs_one_non_submit_engine_and_persists(
    tmp_path: Path,
) -> None:
    parts, assembled, repository = _setup(tmp_path)

    result, engine, browser = await _execute(
        parts, assembled, repository
    )

    assert result.status is NonSubmitApplicationExecutionStatus.CREATED
    assert result.record is not None
    assert result.record.gate_a_outcome is GateAOutcome.HUMAN_AUTHORIZED
    assert result.record.execution_state is (
        NonSubmitExecutionRecordState.REVIEW_READY
    )
    assert result.record.gate_a_consumption_reference is not None
    assert (
        result.record.gate_a_consumption_reference.consumer
        == "P2C3_NON_SUBMIT_EXECUTION"
    )
    assert result.record.submission_attempted is False
    assert browser.calls == 1
    assert len(engine.calls) == 1


@pytest.mark.asyncio
async def test_human_gate_a_without_approval_defers_before_browser_or_engine(
    tmp_path: Path,
) -> None:
    parts, assembled, repository = _setup(tmp_path)
    engine = _Engine()
    browser = _BrowserProvider()

    result, _, _ = await _execute(
        parts,
        assembled,
        repository,
        engine=engine,
        browser=browser,
        command=_command(assembled, approve_gate_a=False),
    )

    assert result.status is (
        NonSubmitApplicationExecutionStatus.DEFERRED_GATE_A_REQUIRED
    )
    assert browser.calls == 0
    assert engine.calls == []


@pytest.mark.asyncio
async def test_engine_receives_exact_bundle_and_hard_non_submit_arguments(
    tmp_path: Path,
) -> None:
    parts, assembled, repository = _setup(tmp_path)

    result, engine, _ = await _execute(parts, assembled, repository)
    request = engine.calls[0]

    assert result.status is NonSubmitApplicationExecutionStatus.CREATED
    assert request["bundle"] == assembled.bundle
    assert request["bundle"].materials.cover_letter_pdf is not None
    assert request["bundle"].answers == assembled.bundle.answers
    assert request["request_submit"] is False
    assert request["approved_review_hash"] == ""
    assert request["private_home"] is parts["home"]
    tree = ast.parse(
        (
            Path(__file__).parents[1]
            / "core"
            / "non_submit_application_execution.py"
        ).read_text(encoding="utf-8")
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
    }
    assert not {
        "submit",
        "verify_submission",
        "create_submission_intent",
        "issue_gate_b",
    } & called_attributes


@pytest.mark.asyncio
async def test_browser_unavailable_is_typed_defer_without_retry(
    tmp_path: Path,
) -> None:
    parts, assembled, repository = _setup(tmp_path)
    engine = _Engine()

    result, _, browser = await _execute(
        parts,
        assembled,
        repository,
        engine=engine,
        browser=_BrowserProvider(unavailable=True),
    )

    assert result.status is (
        NonSubmitApplicationExecutionStatus.DEFERRED_BROWSER_UNAVAILABLE
    )
    assert browser.calls == 1
    assert engine.calls == []


@pytest.mark.asyncio
async def test_runtime_required_sensitive_input_defers_and_records_controls(
    tmp_path: Path,
) -> None:
    parts, assembled, repository = _setup(tmp_path)
    bundle = assembled.bundle
    outcome = ApplicationOutcome.needs_user(
        run_id=bundle.run_id,
        job_id=bundle.job.job_id,
        status=OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
        phase=OutcomePhase.VALIDATE,
        reason_code=ReasonCode.SENSITIVE_ANSWER_REQUIRED,
        message="synthetic attestation required",
        adapter="synthetic",
        details={"review": {"unresolved_required": ["attestation"]}},
    )

    result, engine, _ = await _execute(
        parts, assembled, repository, engine=_Engine(outcome)
    )

    assert result.status is (
        NonSubmitApplicationExecutionStatus
        .DEFERRED_RUNTIME_INPUT_REQUIRED
    )
    assert result.record is not None
    assert result.record.runtime_unresolved_controls == ("attestation",)
    assert engine.calls[0]["request_submit"] is False


@pytest.mark.asyncio
async def test_submission_evidence_from_engine_fails_closed(
    tmp_path: Path,
) -> None:
    parts, assembled, repository = _setup(tmp_path)
    bundle = assembled.bundle
    outcome = ApplicationOutcome.submitted_verified(
        run_id=bundle.run_id,
        job_id=bundle.job.job_id,
        adapter="synthetic",
        evidence_refs=(
            EvidenceRef(
                kind=EvidenceKind.CONFIRMATION_TEXT,
                sha256="c" * 64,
            ),
        ),
    )

    result, _, _ = await _execute(
        parts, assembled, repository, engine=_Engine(outcome)
    )

    assert result.status is NonSubmitApplicationExecutionStatus.FAILED
    assert result.failure_reason is (
        NonSubmitApplicationExecutionFailureReason
        .SUBMISSION_BOUNDARY_VIOLATION
    )
    assert not tuple(
        parts["home"].paths.non_submit_application_executions.rglob("*.json")
    )


@pytest.mark.asyncio
async def test_same_binding_replay_is_unchanged_without_browser_or_engine(
    tmp_path: Path,
) -> None:
    parts, assembled, repository = _setup(tmp_path)
    first, _, _ = await _execute(parts, assembled, repository)
    engine = _Engine()
    browser = _BrowserProvider()

    replay, _, _ = await _execute(
        parts,
        assembled,
        repository,
        engine=engine,
        browser=browser,
    )

    assert first.record is not None
    assert replay.status is NonSubmitApplicationExecutionStatus.UNCHANGED
    assert replay.record == first.record
    assert (
        replay.record.gate_a_consumption_reference
        == first.record.gate_a_consumption_reference
    )
    assert browser.calls == 0
    assert engine.calls == []


@pytest.mark.asyncio
async def test_artifact_drift_fails_before_browser_and_restart_reads_record(
    tmp_path: Path,
) -> None:
    parts, assembled, repository = _setup(tmp_path)
    created, _, _ = await _execute(parts, assembled, repository)
    assert created.record is not None
    restarted = PrivateHomeNonSubmitApplicationExecutionRepository(
        parts["home"]
    )
    read = restarted.get(
        subject_id=SUBJECT_ID, record_id=created.record.record_id
    )
    cover = assembled.bundle.materials.cover_letter_pdf
    assert cover is not None
    parts["home"].contained_path(cover.reference).write_bytes(b"%PDF-drift")
    browser = _BrowserProvider()
    failed, _, _ = await _execute(
        parts,
        assembled,
        repository,
        browser=browser,
    )

    assert read.record == created.record
    assert failed.status is NonSubmitApplicationExecutionStatus.FAILED
    assert failed.failure_reason is (
        NonSubmitApplicationExecutionFailureReason
        .MATERIAL_INTEGRITY_FAILURE
    )
    assert browser.calls == 0


@pytest.mark.asyncio
async def test_workday_special_route_receives_bundle_materials_and_private_home(
    tmp_path: Path,
) -> None:
    parts, assembled, _ = _setup(tmp_path)
    captured = []

    class _Workday:
        async def run(self, context):
            captured.append(context)
            return ApplicationOutcome.review_ready(
                run_id=context.run_id,
                job_id=context.job_id,
                adapter="workday",
            )

    registry = AdapterRegistry()
    registry._specialized["workday"] = _Workday()
    await registry.run(
        AdapterRunRequest(
            page=object(),
            job_url="https://example.wd5.myworkdayjobs.com/job/1",
            job_id=assembled.bundle.job.job_id,
            run_id=assembled.bundle.run_id,
            profile=assembled.bundle.profile,
            resume_path=str(assembled.bundle.materials.resume_path),
            answers=assembled.bundle.answers,
            materials=assembled.bundle.materials,
            private_home=parts["home"],
        )
    )

    assert captured[0].materials is assembled.bundle.materials
    assert captured[0].private_home is parts["home"]
