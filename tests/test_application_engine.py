from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.generic_ai.adapter import GenericAIAdapter
from adapters.generic_ai.cache import RecipeCache
from adapters.generic_ai.executor import FillReport
from adapters.generic_ai.models import FormControl, FormIR
from adapters.generic_ai.verifier import SubmissionEvidence, VerificationReport
from adapters.registry import AdapterRegistry, AdapterRunRequest
from core.application_engine import JobApplicationEngine
from core.bundles import ApplicationBundle, JobSpec, MaterialBundle
from core.event_ledger import EventLedger, SubmissionStatus
from core.leases import LeaseManager
from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.permits import PermitService
from core.policy import (
    AutonomyMode,
    JobTier,
    PolicyConfig,
    PolicyEngine,
    RiskSignals,
)


class FakeRegistry:
    def __init__(self) -> None:
        self.submit_clicks = 0
        self.before_submit_validation = None

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        review_hash = "a" * 64
        if not request.request_submit:
            return ApplicationOutcome.review_ready(
                run_id=request.run_id,
                job_id=request.job_id,
                adapter="greenhouse",
                details={"review": {"fingerprint": review_hash}},
            )
        if self.before_submit_validation is not None:
            self.before_submit_validation()
        allowed = await request.gate_b_validator(
            request.gate_b_permit,
            job_id=request.job_id,
            run_id=request.run_id,
            review_fingerprint=review_hash,
        )
        if not allowed:
            return ApplicationOutcome(
                run_id=request.run_id,
                job_id=request.job_id,
                status=OutcomeStatus.AWAITING_GATE_B,
                phase=OutcomePhase.REVIEW,
                reason_code=ReasonCode.GATE_B_REQUIRED,
                adapter="greenhouse",
            )
        self.submit_clicks += 1
        return ApplicationOutcome.submitted_verified(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="greenhouse",
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    sha256="b" * 64,
                ),
            ),
        )


class UntrustedReviewRegistry:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        self.calls += 1
        assert request.request_submit is False
        return ApplicationOutcome.submitted_verified(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="untrusted-review",
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    sha256="e" * 64,
                ),
            ),
        )


class UntrustedSubmitRegistry:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        self.calls += 1
        if not request.request_submit:
            return ApplicationOutcome.review_ready(
                run_id=request.run_id,
                job_id=request.job_id,
                adapter="untrusted-submit",
                details={"review": {"fingerprint": "a" * 64}},
            )
        # Deliberately skip the Gate B callback.  The core must not trust the
        # returned status, even though the adapter supplies plausible evidence.
        return ApplicationOutcome.submitted_verified(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="untrusted-submit",
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    sha256="f" * 64,
                ),
            ),
        )


class MixedEvidenceRegistry:
    def __init__(self) -> None:
        self.submit_clicks = 0

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        review_hash = "a" * 64
        if not request.request_submit:
            return ApplicationOutcome.review_ready(
                run_id=request.run_id,
                job_id=request.job_id,
                adapter="mixed-evidence",
                details={"review": {"fingerprint": review_hash}},
            )
        allowed = await request.gate_b_validator(
            request.gate_b_permit,
            job_id=request.job_id,
            run_id=request.run_id,
            review_fingerprint=review_hash,
        )
        assert allowed is True
        self.submit_clicks += 1
        return ApplicationOutcome.submitted_verified(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="mixed-evidence",
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.FORM_SNAPSHOT,
                    sha256="1" * 64,
                ),
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    sha256="2" * 64,
                ),
            ),
        )


class ForbiddenRegistry:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        self.calls += 1
        raise AssertionError("an UNKNOWN submission must stop before the adapter")


class RaiseAfterReservationRegistry:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        self.calls += 1
        review_hash = "a" * 64
        if not request.request_submit:
            return ApplicationOutcome.review_ready(
                run_id=request.run_id,
                job_id=request.job_id,
                adapter="synthetic-exception",
                details={"review": {"fingerprint": review_hash}},
            )
        allowed = await request.gate_b_validator(
            request.gate_b_permit,
            job_id=request.job_id,
            run_id=request.run_id,
            review_fingerprint=review_hash,
        )
        assert allowed is True
        raise RuntimeError("synthetic post-reservation browser failure")


class StructuredFailureAfterReservationRegistry:
    """Mimic an adapter that catches its own post-reservation exception."""

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        review_hash = "a" * 64
        if not request.request_submit:
            return ApplicationOutcome.review_ready(
                run_id=request.run_id,
                job_id=request.job_id,
                adapter="synthetic-structured-failure",
                details={"review": {"fingerprint": review_hash}},
            )
        allowed = await request.gate_b_validator(
            request.gate_b_permit,
            job_id=request.job_id,
            run_id=request.run_id,
            review_fingerprint=review_hash,
        )
        assert allowed is True
        return ApplicationOutcome(
            run_id=request.run_id,
            job_id=request.job_id,
            status=OutcomeStatus.FAILED_RETRYABLE,
            phase=OutcomePhase.SUBMIT,
            reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
            message="synthetic adapter caught a browser exception",
            adapter="synthetic-structured-failure",
            retryable=True,
        )


class PersistedAttestationRegistry:
    def __init__(self) -> None:
        self.requests: list[tuple[bool, str]] = []

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        review_hash = "a" * 64
        attestation = "d" * 64
        self.requests.append(
            (request.request_submit, request.persisted_review_attestation)
        )
        if not request.request_submit:
            return ApplicationOutcome.review_ready(
                run_id=request.run_id,
                job_id=request.job_id,
                adapter="workday",
                details={
                    "review_fingerprint": review_hash,
                    "workday_binding_attestation": attestation,
                },
            )
        allowed = await request.gate_b_validator(
            request.gate_b_permit,
            job_id=request.job_id,
            run_id=request.run_id,
            review_fingerprint=review_hash,
        )
        assert allowed is True
        return ApplicationOutcome.submitted_verified(
            run_id=request.run_id,
            job_id=request.job_id,
            adapter="workday",
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.CONFIRMATION_TEXT,
                    sha256="e" * 64,
                ),
            ),
        )


def _bundle(tmp_path: Path, *, run_id: str, tier: JobTier, mode: AutonomyMode) -> ApplicationBundle:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic resume")
    policy = PolicyEngine(PolicyConfig(mode=mode)).decide(tier, RiskSignals())
    return ApplicationBundle(
        run_id=run_id,
        job=JobSpec(
            url="https://boards.greenhouse.io/acme/jobs/1",
            company="Acme",
            title="Engineer",
            tier=tier,
        ),
        materials=MaterialBundle.build(resume_path=resume),
        profile={"personal": {"email": "synthetic@example.test"}},
        answers={"sponsorship": "No"},
        policy=policy,
    )


def _engine(tmp_path: Path, registry) -> JobApplicationEngine:
    ledger = EventLedger(tmp_path / "events.sqlite3")
    return JobApplicationEngine(
        ledger=ledger,
        leases=LeaseManager(ledger),
        permits=PermitService(secret=b"x" * 32, ledger=ledger),
        registry=registry,
    )


@pytest.mark.asyncio
async def test_low_risk_autopilot_consumes_two_gates_and_records_evidence(tmp_path: Path) -> None:
    registry = FakeRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path, run_id="run-low", tier=JobTier.LOW, mode=AutonomyMode.LOW_RISK_AUTOPILOT
    )

    outcome = await engine.execute(page=object(), bundle=bundle, request_submit=True)

    assert outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
    assert registry.submit_clicks == 1
    assert [event.event_type for event in engine.ledger.list_events(run_id=bundle.run_id)].count(
        "GATE_A_AUTHORIZED"
    ) == 1
    assert [event.event_type for event in engine.ledger.list_events(run_id=bundle.run_id)].count(
        "GATE_B_AUTHORIZED"
    ) == 1
    assert engine.leases.get("browser:chromium") is None


@pytest.mark.asyncio
async def test_engine_rejects_verified_status_before_gate_b(tmp_path: Path) -> None:
    registry = UntrustedReviewRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path,
        run_id="run-untrusted-review",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    outcome = await engine.execute(page=object(), bundle=bundle)

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.details["core_submission_authorized"] is False
    assert registry.calls == 1
    events = engine.ledger.list_events(run_id=bundle.run_id)
    assert "SUBMISSION_VERIFIED" not in {event.event_type for event in events}
    assert all(
        event.payload.get("outcome", {}).get("status") != "SUBMITTED_VERIFIED"
        for event in events
    )


@pytest.mark.asyncio
async def test_engine_rejects_verified_status_when_adapter_skips_validator(
    tmp_path: Path,
) -> None:
    registry = UntrustedSubmitRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path,
        run_id="run-untrusted-submit",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    outcome = await engine.execute(page=object(), bundle=bundle, request_submit=True)

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.reason_code is ReasonCode.SUBMISSION_CONFIRMATION_MISSING
    assert registry.calls == 2
    events = engine.ledger.list_events(run_id=bundle.run_id)
    event_types = {event.event_type for event in events}
    assert "SUBMISSION_INTENT_CREATED" not in event_types
    assert "SUBMISSION_VERIFIED" not in event_types
    assert all(
        event.payload.get("outcome", {}).get("status") != "SUBMITTED_VERIFIED"
        for event in events
    )


@pytest.mark.asyncio
async def test_engine_persists_first_eligible_submission_evidence(
    tmp_path: Path,
) -> None:
    registry = MixedEvidenceRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path,
        run_id="run-mixed-evidence",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    outcome = await engine.execute(page=object(), bundle=bundle, request_submit=True)

    assert outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
    created = next(
        event
        for event in engine.ledger.list_events(run_id=bundle.run_id)
        if event.event_type == "SUBMISSION_INTENT_CREATED"
    )
    evidence = engine.ledger.list_submission_evidence(created.payload["intent_id"])
    assert [item.kind for item in evidence] == [EvidenceKind.CONFIRMATION_TEXT.value]


@pytest.mark.asyncio
async def test_unknown_submission_preflight_is_a_non_mutating_hard_stop(
    tmp_path: Path,
) -> None:
    registry = ForbiddenRegistry()
    engine = _engine(tmp_path, registry)
    original = _bundle(
        tmp_path,
        run_id="run-original-unknown",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )
    engine._ensure_run(original)
    intent = engine.ledger.create_submission_intent(
        run_id=original.run_id,
        job_id=original.job.job_id,
        job_url=original.job.url,
        material_hash=original.materials.digest,
        answer_hash=original.answer_hash,
        review_hash="a" * 64,
        policy_hash=original.policy.policy_hash,
    )
    engine.ledger.mark_submission_started(intent.intent_id)
    engine.ledger.mark_submission_unknown(intent.intent_id)
    retry = _bundle(
        tmp_path,
        run_id="run-automatic-retry",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    outcome = await engine.execute(page=object(), bundle=retry, request_submit=True)

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.details["do_not_retry_submit"] is True
    assert outcome.details["human_reconciliation_required"] is True
    assert registry.calls == 0
    assert engine.ledger.get_submission_intent(intent.intent_id).status is SubmissionStatus.UNKNOWN
    assert engine.ledger.get_run(retry.run_id).state == OutcomeStatus.SUBMIT_UNKNOWN.value
    retry_events = engine.ledger.list_events(run_id=retry.run_id)
    assert "GATE_A_AUTHORIZED" not in {event.event_type for event in retry_events}
    safe_guard = str(outcome.details["submission_guard"])
    assert original.job.url not in safe_guard
    assert original.materials.digest not in safe_guard
    assert original.answer_hash not in safe_guard


@pytest.mark.asyncio
async def test_post_reservation_adapter_exception_becomes_unknown_and_blocks_retry(
    tmp_path: Path,
) -> None:
    registry = RaiseAfterReservationRegistry()
    engine = _engine(tmp_path, registry)
    original = _bundle(
        tmp_path,
        run_id="run-post-reservation-exception",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    outcome = await engine.execute(
        page=object(), bundle=original, request_submit=True
    )

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.details["do_not_retry_submit"] is True
    assert outcome.details["human_reconciliation_required"] is True
    assert outcome.details["error_type"] == "RuntimeError"
    created = next(
        event
        for event in engine.ledger.list_events(run_id=original.run_id)
        if event.event_type == "SUBMISSION_INTENT_CREATED"
    )
    intent = engine.ledger.get_submission_intent(created.payload["intent_id"])
    assert intent.status is SubmissionStatus.UNKNOWN
    assert "synthetic post-reservation browser failure" not in outcome.to_json()

    calls_before_retry = registry.calls
    retry = _bundle(
        tmp_path,
        run_id="run-blocked-post-reservation-retry",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )
    retry_outcome = await engine.execute(
        page=object(), bundle=retry, request_submit=True
    )

    assert retry_outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert registry.calls == calls_before_retry


@pytest.mark.asyncio
async def test_structured_failure_after_reservation_is_never_retryable(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, StructuredFailureAfterReservationRegistry())
    bundle = _bundle(
        tmp_path,
        run_id="run-structured-post-reservation-failure",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    outcome = await engine.execute(page=object(), bundle=bundle, request_submit=True)

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.retryable is False
    assert outcome.details["adapter_status"] == OutcomeStatus.FAILED_RETRYABLE.value
    assert outcome.details["do_not_retry_submit"] is True
    created = next(
        event
        for event in engine.ledger.list_events(run_id=bundle.run_id)
        if event.event_type == "SUBMISSION_INTENT_CREATED"
    )
    assert (
        engine.ledger.get_submission_intent(created.payload["intent_id"]).status
        is SubmissionStatus.UNKNOWN
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target_status", [SubmissionStatus.PENDING, SubmissionStatus.SUBMITTING])
async def test_unresolved_preflight_states_stop_before_browser(
    tmp_path: Path, target_status: SubmissionStatus
) -> None:
    registry = ForbiddenRegistry()
    engine = _engine(tmp_path, registry)
    original = _bundle(
        tmp_path,
        run_id=f"run-preflight-{target_status.value.casefold()}",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )
    engine._ensure_run(original)
    intent = engine.ledger.create_submission_intent(
        run_id=original.run_id,
        job_id=original.job.job_id,
        job_url=original.job.url,
        material_hash=original.materials.digest,
        answer_hash=original.answer_hash,
        review_hash="a" * 64,
        policy_hash=original.policy.policy_hash,
    )
    if target_status is SubmissionStatus.SUBMITTING:
        engine.ledger.mark_submission_started(intent.intent_id)
    retry = _bundle(
        tmp_path,
        run_id=f"run-retry-{target_status.value.casefold()}",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    outcome = await engine.execute(page=object(), bundle=retry, request_submit=True)

    assert outcome.status is OutcomeStatus.SUBMIT_UNKNOWN
    assert outcome.details["human_reconciliation_required"] is True
    assert registry.calls == 0
    assert engine.ledger.get_submission_intent(intent.intent_id).status is target_status


@pytest.mark.asyncio
async def test_engine_driven_generic_submit_uses_canonical_url_identity(
    tmp_path: Path, monkeypatch
) -> None:
    resume = tmp_path / "generic-resume.pdf"
    resume.write_bytes(b"synthetic resume")
    job = JobSpec(
        url="https://careers.example.invalid/jobs/7/?utm_source=queue&ref=jobops",
        company="Example",
        title="Engineer",
        tier=JobTier.LOW,
    )
    bundle = ApplicationBundle(
        run_id="run-generic-engine",
        job=job,
        materials=MaterialBundle.build(resume_path=resume),
        profile={"personal": {"email": "candidate@example.invalid"}},
        answers={},
        policy=PolicyEngine(
            PolicyConfig(mode=AutonomyMode.LOW_RISK_AUTOPILOT)
        ).decide(JobTier.LOW, RiskSignals()),
    )
    form = FormIR(
        platform="generic",
        tenant="careers.example.invalid",
        stage="review",
        url_path="/jobs/7/",
        controls=(
            FormControl(
                index=0,
                role="textbox",
                tag="input",
                input_type="email",
                label="Email",
                required=True,
                selector="#email",
            ),
        ),
        submit_selector="#submit",
        submit_text="Submit application",
    )
    verification = VerificationReport(
        True,
        (),
        (),
        ((0, "email", "a" * 64),),
        (),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.observe_form", AsyncMock(return_value=form)
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.execute_resolved_fields",
        AsyncMock(return_value=FillReport(1, 1, (), ())),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.verify_fields",
        AsyncMock(return_value=verification),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.detect_submission_evidence",
        AsyncMock(
            side_effect=(
                None,
                None,
                SubmissionEvidence(
                    kind="confirmation_text",
                    url="https://careers.example.invalid/jobs/7/confirmation",
                    text="We received your application.",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        "adapters.generic_ai.adapter.click_submit", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("adapters.generic_ai.adapter.asyncio.sleep", AsyncMock())
    ledger = EventLedger(tmp_path / "generic-events.sqlite3")
    engine = JobApplicationEngine(
        ledger=ledger,
        leases=LeaseManager(ledger),
        permits=PermitService(secret=b"x" * 32, ledger=ledger),
        registry=AdapterRegistry(
            generic_adapter=GenericAIAdapter(
                cache=RecipeCache(tmp_path / "generic-recipes")
            )
        ),
    )
    page = MagicMock()
    page.url = job.url

    outcome = await engine.execute(page=page, bundle=bundle, request_submit=True)

    assert outcome.status is OutcomeStatus.SUBMITTED_VERIFIED
    created = next(
        event
        for event in ledger.list_events(run_id=bundle.run_id)
        if event.event_type == "SUBMISSION_INTENT_CREATED"
    )
    intent_id = created.payload["intent_id"]
    assert ledger.get_submission_intent(intent_id).status is SubmissionStatus.VERIFIED
    assert len(ledger.list_submission_evidence(intent_id)) == 1


@pytest.mark.asyncio
async def test_high_priority_stops_at_first_human_gate(tmp_path: Path) -> None:
    registry = FakeRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path, run_id="run-high", tier=JobTier.HIGH, mode=AutonomyMode.LOW_RISK_AUTOPILOT
    )

    outcome = await engine.execute(page=object(), bundle=bundle)

    assert outcome.status is OutcomeStatus.AWAITING_GATE_A
    assert registry.submit_clicks == 0


@pytest.mark.asyncio
async def test_medium_priority_reaches_review_with_zero_submit_clicks(tmp_path: Path) -> None:
    registry = FakeRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path,
        run_id="run-medium",
        tier=JobTier.MEDIUM,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    outcome = await engine.execute(page=object(), bundle=bundle)

    assert outcome.status is OutcomeStatus.REVIEW_READY
    assert registry.submit_clicks == 0


@pytest.mark.asyncio
async def test_human_gate_b_requires_a_later_review_bound_invocation(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path,
        run_id="run-medium-two-step",
        tier=JobTier.MEDIUM,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    # Supplying the digest before this run has persisted Review cannot authorize
    # a click, even when the caller happens to guess the adapter fingerprint.
    first = await engine.execute(
        page=object(),
        bundle=bundle,
        request_submit=True,
        approved_review_hash="a" * 64,
    )
    assert first.status is OutcomeStatus.AWAITING_GATE_B
    assert registry.submit_clicks == 0

    reviewed_hash = engine.latest_review_hash(bundle.run_id)
    assert reviewed_hash == "a" * 64
    second = await engine.execute(
        page=object(),
        bundle=bundle,
        request_submit=True,
        approved_review_hash=reviewed_hash,
    )

    assert second.status is OutcomeStatus.SUBMITTED_VERIFIED
    assert registry.submit_clicks == 1


@pytest.mark.asyncio
async def test_persisted_workday_attestation_is_replayed_on_later_gate_b(
    tmp_path: Path,
) -> None:
    registry = PersistedAttestationRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path,
        run_id="run-workday-cross-process",
        tier=JobTier.MEDIUM,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )

    first = await engine.execute(page=object(), bundle=bundle, request_submit=True)

    assert first.status is OutcomeStatus.AWAITING_GATE_B
    assert registry.requests == [(False, "")]
    reviewed_hash = engine.latest_review_hash(bundle.run_id)

    second = await engine.execute(
        page=object(),
        bundle=bundle,
        request_submit=True,
        approved_review_hash=reviewed_hash,
    )

    assert second.status is OutcomeStatus.SUBMITTED_VERIFIED
    assert registry.requests[1:] == [(True, "d" * 64)]


@pytest.mark.asyncio
async def test_duplicate_submission_is_blocked_before_second_click(tmp_path: Path) -> None:
    registry = FakeRegistry()
    engine = _engine(tmp_path, registry)
    first = _bundle(
        tmp_path, run_id="run-first", tier=JobTier.LOW, mode=AutonomyMode.LOW_RISK_AUTOPILOT
    )
    second = _bundle(
        tmp_path, run_id="run-second", tier=JobTier.LOW, mode=AutonomyMode.LOW_RISK_AUTOPILOT
    )

    assert (await engine.execute(page=object(), bundle=first, request_submit=True)).status is OutcomeStatus.SUBMITTED_VERIFIED
    second_outcome = await engine.execute(page=object(), bundle=second, request_submit=True)

    assert second_outcome.reason_code is ReasonCode.DUPLICATE_SUBMISSION
    assert registry.submit_clicks == 1


def test_permit_secret_is_not_in_ledger_metadata(tmp_path: Path) -> None:
    registry = FakeRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path, run_id="run-meta", tier=JobTier.LOW, mode=AutonomyMode.LOW_RISK_AUTOPILOT
    )
    engine._ensure_run(bundle)
    assert "secret" not in str(engine.ledger.get_run(bundle.run_id).metadata).casefold()


def test_prebrowser_material_outcome_uses_same_ledger_contract(tmp_path: Path) -> None:
    engine = _engine(tmp_path, FakeRegistry())
    outcome = ApplicationOutcome(
        run_id="run-materials",
        job_id="job-materials",
        status=OutcomeStatus.MATERIALS_REQUIRED,
        phase=OutcomePhase.MATERIALS,
        reason_code=ReasonCode.MISSING_MATERIAL,
        message="Codex must prepare the private manifest",
    )

    engine.record_outcome(outcome, metadata={"tier": "HIGH"})

    run = engine.ledger.get_run(outcome.run_id)
    assert run.state == OutcomeStatus.MATERIALS_REQUIRED.value
    assert run.outcome["reason_code"] == ReasonCode.MISSING_MATERIAL.value


@pytest.mark.asyncio
async def test_stale_browser_lease_blocks_permit_consumption_and_click(tmp_path: Path) -> None:
    registry = FakeRegistry()
    engine = _engine(tmp_path, registry)
    bundle = _bundle(
        tmp_path,
        run_id="run-stale-browser",
        tier=JobTier.LOW,
        mode=AutonomyMode.LOW_RISK_AUTOPILOT,
    )
    original = engine.leases.acquire(
        "browser:chromium",
        owner=bundle.run_id,
        ttl_seconds=30,
    )
    replacement = None

    def replace_browser_owner() -> None:
        nonlocal replacement
        engine.leases.release(original)
        replacement = engine.leases.acquire(
            "browser:chromium",
            owner="another-run",
            ttl_seconds=30,
        )

    registry.before_submit_validation = replace_browser_owner
    try:
        outcome = await engine.execute(
            page=object(),
            bundle=bundle,
            request_submit=True,
            browser_lease=original,
        )
    finally:
        if replacement is not None:
            engine.leases.release(replacement)

    assert outcome.status is OutcomeStatus.FAILED_RETRYABLE
    assert outcome.reason_code is ReasonCode.RETRYABLE_BROWSER_ERROR
    assert registry.submit_clicks == 0
    event_types = [
        event.event_type
        for event in engine.ledger.list_events(run_id=bundle.run_id)
    ]
    assert "GATE_B_PERMIT_CONSUMED" not in event_types
    assert "SUBMISSION_INTENT_CREATED" not in event_types
