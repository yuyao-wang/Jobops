"""Policy, permits, leases, ledger, and adapters composed into one run engine."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from adapters.registry import AdapterRegistry, AdapterRunRequest
from auth.credentials import CredentialStore
from auth.mailbox import MailboxVerifier

from .bundles import ApplicationBundle, canonical_hash
from .event_ledger import (
    DuplicateSubmissionError,
    EventLedger,
    RunAlreadyExistsError,
    RunNotFoundError,
    SubmissionIntent,
    SubmissionStatus,
)
from .leases import (
    Lease,
    LeaseError,
    LeaseExpiredError,
    LeaseManager,
    LeaseNotFoundError,
    LeaseOwnershipError,
    LeaseUnavailableError,
    RenewingLease,
)
from .outcomes import (
    SUBMISSION_EVIDENCE_KINDS,
    ApplicationOutcome,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from .permits import (
    GateAConsumptionReference,
    PlanScopedSubmissionPermitBindings,
    PermitError,
    PermitGate,
    PermitService,
    SubmissionPermitAction,
)
from .policy import ApprovalActor
from .private_home import PrivateHome
from .secrets import (
    PERMIT_SECRET_ACCOUNT,
    PERMIT_SECRET_SERVICE,
    load_or_create_permit_secret,
)


class JobApplicationEngine:
    """Execute a single application with idempotent, permit-gated mutation."""

    def __init__(
        self,
        *,
        ledger: EventLedger,
        leases: LeaseManager,
        permits: PermitService,
        registry: AdapterRegistry | Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.leases = leases
        self.permits = permits
        self.registry = registry or AdapterRegistry()

    @classmethod
    def from_private_home(
        cls,
        *,
        home: PrivateHome | None = None,
        credential_store: CredentialStore | None = None,
        registry: AdapterRegistry | Any | None = None,
    ) -> "JobApplicationEngine":
        paths = (home or PrivateHome.discover()).ensure()
        ledger = EventLedger(paths.event_ledger)
        if registry is None:
            from adapters.generic_ai import GenericAIAdapter
            from adapters.generic_ai.cache import RecipeCache

            registry = AdapterRegistry(
                generic_adapter=GenericAIAdapter(
                    cache=RecipeCache(paths.private_recipes)
                )
            )
        permits = PermitService(
            secret=load_or_create_permit_secret(credential_store),
            ledger=ledger,
            signer_key_id=(
                f"keychain:{PERMIT_SECRET_SERVICE}:{PERMIT_SECRET_ACCOUNT}"
            ),
        )
        return cls(
            ledger=ledger,
            leases=LeaseManager(ledger),
            permits=permits,
            registry=registry,
        )

    def _ensure_run(self, bundle: ApplicationBundle) -> None:
        try:
            self.ledger.create_run(
                run_id=bundle.run_id,
                job_id=bundle.job.job_id,
                metadata=bundle.safe_metadata(),
            )
        except RunAlreadyExistsError:
            existing = self.ledger.get_run(bundle.run_id)
            if existing.job_id != bundle.job.job_id:
                raise ValueError("existing run belongs to a different job")

    def _record_outcome(self, outcome: ApplicationOutcome) -> None:
        run = self.ledger.get_run(outcome.run_id)
        self.ledger.compare_and_set_state(
            run_id=outcome.run_id,
            expected_version=run.state_version,
            new_state=outcome.status.value,
            outcome=outcome,
        )

    def record_outcome(
        self,
        outcome: ApplicationOutcome,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a unified pre-browser outcome such as MATERIALS_REQUIRED."""

        try:
            run = self.ledger.get_run(outcome.run_id)
        except RunNotFoundError:
            self.ledger.create_run(
                run_id=outcome.run_id,
                job_id=outcome.job_id,
                metadata=metadata or {},
            )
        else:
            if run.job_id != outcome.job_id:
                raise ValueError("existing run belongs to a different job")
        self._record_outcome(outcome)

    def submission_preflight(
        self, bundle: ApplicationBundle
    ) -> ApplicationOutcome | None:
        """Fail closed before browser launch for any durable submit guard.

        The lookup is read-only.  PENDING, SUBMITTING, and UNKNOWN are all
        uncertain after a process interruption and are never retired
        automatically.  VERIFIED is a completed duplicate guard.
        """

        intent = self.ledger.find_submission_intent_for_url(
            bundle.job.url,
            statuses=(
                SubmissionStatus.PENDING,
                SubmissionStatus.SUBMITTING,
                SubmissionStatus.UNKNOWN,
                SubmissionStatus.VERIFIED,
            ),
        )
        if intent is None:
            return None
        if intent.status is SubmissionStatus.VERIFIED:
            return ApplicationOutcome(
                run_id=bundle.run_id,
                job_id=bundle.job.job_id,
                status=OutcomeStatus.SKIPPED_POLICY,
                phase=OutcomePhase.QUEUE,
                reason_code=ReasonCode.DUPLICATE_SUBMISSION,
                message="A verified submission already exists for this posting",
                checkpoint=f"submission-intent:{intent.intent_id}",
                details={"submission_guard": intent.to_safe_dict()},
            )
        return ApplicationOutcome(
            run_id=bundle.run_id,
            job_id=bundle.job.job_id,
            status=OutcomeStatus.SUBMIT_UNKNOWN,
            phase=OutcomePhase.VERIFY,
            reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
            message=(
                "A prior submission intent remains unresolved; automatic retry is "
                "blocked until a human reconciles it"
            ),
            checkpoint=f"submission-intent:{intent.intent_id}",
            details={
                "submission_guard": intent.to_safe_dict(),
                "do_not_retry_submit": True,
                "human_reconciliation_required": True,
            },
        )

    @staticmethod
    def _reject_untrusted_verified(
        outcome: ApplicationOutcome,
        *,
        reason: str,
    ) -> ApplicationOutcome:
        """Replace an adapter success that did not cross the trusted boundary."""

        details = dict(outcome.details)
        details.update(
            {
                "adapter_reported_status": OutcomeStatus.SUBMITTED_VERIFIED.value,
                "core_submission_authorized": False,
                "do_not_retry_submit": True,
                "human_reconciliation_required": True,
            }
        )
        return ApplicationOutcome(
            run_id=outcome.run_id,
            job_id=outcome.job_id,
            status=OutcomeStatus.SUBMIT_UNKNOWN,
            phase=OutcomePhase.VERIFY,
            reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
            message=reason,
            adapter=outcome.adapter,
            checkpoint=outcome.checkpoint,
            evidence_refs=outcome.evidence_refs,
            details=details,
        )

    @staticmethod
    def _plan_hash(bundle: ApplicationBundle) -> str:
        return canonical_hash({"kind": "preflight", **bundle.safe_metadata()})

    @staticmethod
    def _review_hash(outcome: ApplicationOutcome) -> str:
        details = dict(outcome.details)
        review = details.get("review")
        if isinstance(review, dict) and review.get("fingerprint"):
            return str(review["fingerprint"])
        for key in ("review_fingerprint", "form_fingerprint"):
            if details.get(key):
                return str(details[key])
        for evidence in outcome.evidence_refs:
            if evidence.kind.value == "FORM_SNAPSHOT" and evidence.sha256:
                return evidence.sha256
        if outcome.checkpoint and len(outcome.checkpoint) == 64:
            return outcome.checkpoint
        return ""

    def latest_review_hash(self, run_id: str) -> str:
        """Return the most recent persisted Review fingerprint for a run.

        Gate B callers use this value only in a later invocation.  Looking at
        append-only outcome events (rather than the mutable run projection)
        preserves the Review even after the run transitions to AWAITING_GATE_B.
        """

        outcome = self.latest_review_outcome(run_id)
        return self._review_hash(outcome) if outcome is not None else ""

    def latest_review_outcome(self, run_id: str) -> ApplicationOutcome | None:
        """Return the latest append-only Review outcome for safe resumption."""

        for event in reversed(self.ledger.list_events(run_id=run_id)):
            if event.event_type != "RUN_STATE_CHANGED":
                continue
            raw = event.payload.get("outcome")
            if not isinstance(raw, dict) or raw.get("status") != OutcomeStatus.REVIEW_READY.value:
                continue
            try:
                return ApplicationOutcome.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def gate_a_consumption_reference(
        self, run_id: str
    ) -> GateAConsumptionReference | None:
        """Recover the latest consumed Gate A receipt without exposing a token."""

        for event in reversed(self.ledger.list_events(run_id=run_id)):
            if event.event_type != "GATE_A_PERMIT_CONSUMED":
                continue
            permit_jti = str(event.payload.get("jti") or "")
            if not permit_jti:
                return None
            return self.permits.gate_a_consumption_reference(
                permit_jti=permit_jti,
                consumer="P2C3_NON_SUBMIT_EXECUTION",
                action="PREPARE_REVIEW",
            )
        return None

    @staticmethod
    def _review_attestation(outcome: ApplicationOutcome | None) -> str:
        if outcome is None:
            return ""
        value = str(outcome.details.get("workday_binding_attestation") or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            return ""
        return value

    def _awaiting_gate(
        self,
        bundle: ApplicationBundle,
        *,
        gate: PermitGate,
        phase: OutcomePhase,
        review_hash: str,
    ) -> ApplicationOutcome:
        status = (
            OutcomeStatus.AWAITING_GATE_A
            if gate is PermitGate.GATE_A
            else OutcomeStatus.AWAITING_GATE_B
        )
        reason = (
            ReasonCode.GATE_A_REQUIRED
            if gate is PermitGate.GATE_A
            else ReasonCode.GATE_B_REQUIRED
        )
        return ApplicationOutcome(
            run_id=bundle.run_id,
            job_id=bundle.job.job_id,
            status=status,
            phase=phase,
            reason_code=reason,
            message=f"{gate.value} requires an explicit human approval for this job tier",
            details={
                "gate": gate.value,
                "binding_digest": bundle.permit_bindings(
                    review_hash=review_hash
                ).digest,
                "review_fingerprint": review_hash
                if gate is PermitGate.GATE_B
                else "",
                "tier": bundle.job.tier.value,
            },
        )

    async def execute(
        self,
        *,
        page: Any,
        bundle: ApplicationBundle,
        request_submit: bool = False,
        approve_gate_a: bool = False,
        approved_review_hash: str = "",
        submission_permit_token: str = "",
        submission_permit_bindings: (
            PlanScopedSubmissionPermitBindings | None
        ) = None,
        credential_store: CredentialStore | None = None,
        mailbox_verifier: MailboxVerifier | None = None,
        brain: Any = None,
        platform_hint: str = "",
        tenant: str = "",
        lease_ttl_seconds: float = 1800.0,
        browser_lease: Lease | None = None,
        private_home: PrivateHome | None = None,
    ) -> ApplicationOutcome:
        """Fill to Review, and submit only after a separate Gate B approval.

        ``approve_gate_a`` represents an explicit review of the application
        plan.  A human Gate B requires ``approved_review_hash`` from a Review
        persisted by an earlier invocation; it can never be pre-authorized in
        the same browser run.  No permit token or secret is returned in outcomes.
        """

        self._ensure_run(bundle)
        external_submission_permit = bool(submission_permit_token) or (
            submission_permit_bindings is not None
        )
        if external_submission_permit:
            if (
                not submission_permit_token
                or not isinstance(
                    submission_permit_bindings,
                    PlanScopedSubmissionPermitBindings,
                )
                or not request_submit
                or submission_permit_bindings.action
                is not SubmissionPermitAction.SUBMIT_APPLICATION
            ):
                raise ValueError(
                    "plan-scoped submission permit input is incomplete"
                )
            current = bundle.permit_bindings(
                review_hash=submission_permit_bindings.review_hash
            )
            if (
                submission_permit_bindings.run_id != current.run_id
                or submission_permit_bindings.job_id != current.job_id
                or submission_permit_bindings.job_url_hash
                != current.job_url_hash
                or submission_permit_bindings.material_hash
                != current.material_hash
                or submission_permit_bindings.answer_hash
                != current.answer_hash
                or submission_permit_bindings.policy_hash
                != current.policy_hash
                or approved_review_hash
                != submission_permit_bindings.review_hash
            ):
                raise ValueError(
                    "plan-scoped submission permit does not bind this bundle"
                )
        submission_guard = self.submission_preflight(bundle)
        if submission_guard is not None:
            self._record_outcome(submission_guard)
            return submission_guard
        previously_reviewed_outcome = self.latest_review_outcome(bundle.run_id)
        previously_reviewed_hash = (
            self._review_hash(previously_reviewed_outcome)
            if previously_reviewed_outcome is not None
            else ""
        )
        persisted_review_attestation = self._review_attestation(
            previously_reviewed_outcome
        )
        resume_approved_review = bool(
            request_submit
            and approved_review_hash
            and previously_reviewed_outcome is not None
            and approved_review_hash == previously_reviewed_hash
        )
        if bundle.policy.blockers:
            outcome = ApplicationOutcome(
                run_id=bundle.run_id,
                job_id=bundle.job.job_id,
                status=OutcomeStatus.SKIPPED_POLICY,
                phase=OutcomePhase.MATERIALS,
                reason_code=ReasonCode.POLICY_DENIED,
                message="Application is blocked by current policy signals",
                details={"blockers": [item.value for item in bundle.policy.blockers]},
            )
            self._record_outcome(outcome)
            return outcome

        plan_hash = self._plan_hash(bundle)
        gate_a_bindings = bundle.permit_bindings(review_hash=plan_hash)
        gate_a_authorized = external_submission_permit or (
            bundle.policy.gate_a_actor is ApprovalActor.CODEX or approve_gate_a
        )
        if not gate_a_authorized:
            outcome = self._awaiting_gate(
                bundle,
                gate=PermitGate.GATE_A,
                phase=OutcomePhase.MATERIALS,
                review_hash=plan_hash,
            )
            self._record_outcome(outcome)
            return outcome

        gate_a_claims = None
        if not external_submission_permit:
            gate_a_token = self.permits.issue_gate_a(gate_a_bindings)
            gate_a_claims = self.permits.consume(
                gate_a_token,
                expected_gate=PermitGate.GATE_A,
                expected_bindings=gate_a_bindings,
            )
            self.ledger.append_event(
                run_id=bundle.run_id,
                job_id=bundle.job.job_id,
                event_type="GATE_A_AUTHORIZED",
                payload={
                    "actor": bundle.policy.gate_a_actor.value
                    if not approve_gate_a
                    else ApprovalActor.HUMAN.value,
                    "binding_digest": gate_a_bindings.digest,
                },
            )

        intent_box: dict[str, SubmissionIntent | None] = {"intent": None}
        validator_succeeded = {"value": False}
        validator_error: dict[str, str] = {}

        try:
            async with AsyncExitStack() as lease_stack:
                run_lease = await lease_stack.enter_async_context(
                    self.leases.hold_renewing(
                        f"run:{bundle.run_id}",
                        owner=bundle.run_id,
                        ttl_seconds=lease_ttl_seconds,
                    )
                )
                managed_browser_lease: RenewingLease | None = None
                if browser_lease is None:
                    managed_browser_lease = await lease_stack.enter_async_context(
                        self.leases.hold_renewing(
                            "browser:chromium",
                            owner=bundle.run_id,
                            ttl_seconds=lease_ttl_seconds,
                        )
                    )
                else:
                    try:
                        current_browser_lease = self.leases.assert_current(browser_lease)
                    except (LeaseNotFoundError, LeaseOwnershipError, LeaseExpiredError):
                        current_browser_lease = self.leases.get("browser:chromium")
                        raise LeaseUnavailableError(
                            "browser:chromium",
                            current_browser_lease.owner if current_browser_lease else "unknown",
                            current_browser_lease.expires_at if current_browser_lease else 0,
                        )
                    if current_browser_lease.owner != bundle.run_id:
                        raise LeaseUnavailableError(
                            "browser:chromium",
                            current_browser_lease.owner,
                            current_browser_lease.expires_at,
                        )
                if resume_approved_review:
                    # A later human-approved invocation must perform one fresh
                    # fill/read-back/submit episode.  The adapter validates its
                    # new Review fingerprint through the Gate B callback before
                    # the click.  Running a separate non-submit episode first
                    # can cause React ATS controls and file inputs to be
                    # destructively filled twice on the same page.
                    assert previously_reviewed_outcome is not None
                    review_outcome = previously_reviewed_outcome
                else:
                    review_outcome = await self.registry.run(
                        AdapterRunRequest(
                            page=page,
                            job_url=bundle.job.url,
                            job_id=bundle.job.job_id,
                            run_id=bundle.run_id,
                            profile=bundle.identity_profile,
                            resume_path=str(bundle.materials.resume_path),
                            company=bundle.job.company,
                            cover_letter=bundle.materials.cover_letter,
                            answers=bundle.answers,
                            request_submit=False,
                            persisted_review_attestation=persisted_review_attestation,
                            credential_store=credential_store,
                            mailbox_verifier=mailbox_verifier,
                            brain=brain,
                            platform_hint=platform_hint,
                            tenant=tenant,
                            materials=bundle.materials,
                            private_home=private_home,
                        )
                    )
                    if review_outcome.status is OutcomeStatus.SUBMITTED_VERIFIED:
                        review_outcome = self._reject_untrusted_verified(
                            review_outcome,
                            reason=(
                                "The adapter reported a submission before Gate B was "
                                "validated; success was not recorded"
                            ),
                        )
                    self._record_outcome(review_outcome)
                    if review_outcome.status is not OutcomeStatus.REVIEW_READY:
                        return review_outcome
                    if not request_submit:
                        return review_outcome

                review_hash = self._review_hash(review_outcome)
                if not review_hash:
                    outcome = ApplicationOutcome(
                        run_id=bundle.run_id,
                        job_id=bundle.job.job_id,
                        status=OutcomeStatus.INTERNAL_ERROR,
                        phase=OutcomePhase.REVIEW,
                        reason_code=ReasonCode.INTERNAL_ERROR,
                        message="Adapter reached Review without a deterministic review fingerprint",
                        adapter=review_outcome.adapter,
                    )
                    self._record_outcome(outcome)
                    return outcome

                current_review_attestation = self._review_attestation(review_outcome)

                if external_submission_permit:
                    assert submission_permit_bindings is not None
                    gate_b_bindings = submission_permit_bindings
                    human_gate_b = False
                    gate_b_authorized = (
                        approved_review_hash == review_hash
                        and gate_b_bindings.review_hash == review_hash
                        and review_outcome.adapter
                        == gate_b_bindings.adapter_platform
                    )
                    if gate_b_authorized:
                        try:
                            self.permits.verify(
                                submission_permit_token,
                                expected_gate=PermitGate.GATE_B,
                                expected_bindings=gate_b_bindings,
                            )
                        except PermitError:
                            gate_b_authorized = False
                else:
                    gate_b_bindings = bundle.permit_bindings(
                        review_hash=review_hash
                    )
                    human_gate_b = (
                        bundle.policy.gate_b_actor is ApprovalActor.HUMAN
                    )
                    gate_b_authorized = not human_gate_b or (
                        bool(approved_review_hash)
                        and bool(previously_reviewed_hash)
                        and approved_review_hash == previously_reviewed_hash
                        and approved_review_hash == review_hash
                    )
                if not gate_b_authorized:
                    outcome = self._awaiting_gate(
                        bundle,
                        gate=PermitGate.GATE_B,
                        phase=OutcomePhase.REVIEW,
                        review_hash=review_hash,
                    )
                    self._record_outcome(outcome)
                    return outcome

                if external_submission_permit:
                    gate_b_token = submission_permit_token
                else:
                    assert gate_a_claims is not None
                    gate_b_token = self.permits.issue_gate_b(
                        gate_b_bindings,
                        gate_a_jti=gate_a_claims.jti,
                    )
                self.ledger.append_event(
                    run_id=bundle.run_id,
                    job_id=bundle.job.job_id,
                    event_type="GATE_B_AUTHORIZED",
                    payload={
                        "actor": (
                            "PLAN_SCOPED_SUBMISSION_PERMIT"
                            if external_submission_permit
                            else bundle.policy.gate_b_actor.value
                        ),
                        "binding_digest": gate_b_bindings.digest,
                        "prior_review_matched": not human_gate_b
                        or approved_review_hash == previously_reviewed_hash,
                    },
                )

                async def validate_and_reserve(
                    token: str,
                    *,
                    job_id: str,
                    run_id: str,
                    review_fingerprint: str,
                ) -> bool:
                    if (
                        job_id != bundle.job.job_id
                        or run_id != bundle.run_id
                        or review_fingerprint != review_hash
                    ):
                        validator_error["reason"] = "permit binding mismatch"
                        return False
                    try:
                        # A heartbeat keeps long application runs alive, but the
                        # authoritative token/owner/expiry check remains the
                        # final fail-closed boundary immediately before permit
                        # consumption and submission-intent reservation.
                        run_lease.assert_fresh()
                        if managed_browser_lease is not None:
                            managed_browser_lease.assert_fresh()
                        elif browser_lease is not None:
                            self.leases.assert_current(browser_lease)
                        self.permits.consume(
                            token,
                            expected_gate=PermitGate.GATE_B,
                            expected_bindings=gate_b_bindings,
                        )
                        intent = self.ledger.create_submission_intent(
                            run_id=bundle.run_id,
                            job_id=bundle.job.job_id,
                            job_url=bundle.job.url,
                            material_hash=bundle.materials.digest,
                            answer_hash=bundle.answer_hash,
                            review_hash=review_hash,
                            policy_hash=bundle.policy.policy_hash,
                            allow_existing_same=False,
                        )
                        # Publish the durable reservation before its state
                        # transition so any subsequent exception is treated as
                        # unresolved, never as a safe automatic retry.
                        intent_box["intent"] = intent
                        self.ledger.mark_submission_started(intent.intent_id)
                        validator_succeeded["value"] = True
                        return True
                    except DuplicateSubmissionError:
                        validator_error["reason"] = "duplicate submission"
                        return False
                    except LeaseError:
                        validator_error["reason"] = "lease unavailable"
                        return False
                    except PermitError:
                        validator_error["reason"] = "permit rejected"
                        return False

                try:
                    submit_outcome = await self.registry.run(
                        AdapterRunRequest(
                            page=page,
                            job_url=bundle.job.url,
                            job_id=bundle.job.job_id,
                            run_id=bundle.run_id,
                            profile=bundle.identity_profile,
                            resume_path=str(bundle.materials.resume_path),
                            company=bundle.job.company,
                            cover_letter=bundle.materials.cover_letter,
                            answers=bundle.answers,
                            request_submit=True,
                            gate_b_permit=gate_b_token,
                            gate_b_validator=validate_and_reserve,
                            persisted_review_attestation=(
                                current_review_attestation
                                or persisted_review_attestation
                            ),
                            credential_store=credential_store,
                            mailbox_verifier=mailbox_verifier,
                            brain=brain,
                            platform_hint=platform_hint,
                            tenant=tenant,
                            navigate=resume_approved_review,
                            materials=bundle.materials,
                            private_home=private_home,
                        )
                    )
                except Exception as exc:
                    intent = intent_box["intent"]
                    if intent is None:
                        submit_outcome = ApplicationOutcome(
                            run_id=bundle.run_id,
                            job_id=bundle.job.job_id,
                            status=OutcomeStatus.FAILED_RETRYABLE,
                            phase=OutcomePhase.SUBMIT,
                            reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
                            message="The adapter failed before reserving a submission intent",
                            retryable=True,
                            details={"error_type": type(exc).__name__},
                        )
                    else:
                        current = self.ledger.get_submission_intent(intent.intent_id)
                        if current.status is SubmissionStatus.SUBMITTING:
                            self.ledger.mark_submission_unknown(intent.intent_id)
                        submit_outcome = ApplicationOutcome(
                            run_id=bundle.run_id,
                            job_id=bundle.job.job_id,
                            status=OutcomeStatus.SUBMIT_UNKNOWN,
                            phase=OutcomePhase.VERIFY,
                            reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
                            message=(
                                "The adapter failed after reserving submission; "
                                "the result requires human reconciliation"
                            ),
                            checkpoint=f"submission-intent:{intent.intent_id}",
                            details={
                                "error_type": type(exc).__name__,
                                "do_not_retry_submit": True,
                                "human_reconciliation_required": True,
                            },
                        )
                    self._record_outcome(submit_outcome)
                    return submit_outcome

                if validator_error.get("reason") == "duplicate submission":
                    submit_outcome = ApplicationOutcome(
                        run_id=bundle.run_id,
                        job_id=bundle.job.job_id,
                        status=OutcomeStatus.SKIPPED_POLICY,
                        phase=OutcomePhase.SUBMIT,
                        reason_code=ReasonCode.DUPLICATE_SUBMISSION,
                        message="An active or verified submission already exists; no click was allowed",
                        adapter=submit_outcome.adapter,
                    )
                elif validator_error.get("reason") == "lease unavailable":
                    submit_outcome = ApplicationOutcome(
                        run_id=bundle.run_id,
                        job_id=bundle.job.job_id,
                        status=OutcomeStatus.FAILED_RETRYABLE,
                        phase=OutcomePhase.SUBMIT,
                        reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
                        message="The run or browser lease was no longer current; no click was allowed",
                        adapter=submit_outcome.adapter,
                        retryable=True,
                    )

                intent = intent_box["intent"]
                if submit_outcome.status is OutcomeStatus.SUBMITTED_VERIFIED and (
                    not validator_succeeded["value"] or intent is None
                ):
                    submit_outcome = self._reject_untrusted_verified(
                        submit_outcome,
                        reason=(
                            "The adapter reported a submission without a successful "
                            "Gate B validation and reserved submission intent; success "
                            "was not recorded"
                        ),
                    )
                if intent is not None:
                    if submit_outcome.status is OutcomeStatus.SUBMITTED_VERIFIED:
                        evidence = next(
                            (
                                item
                                for item in submit_outcome.evidence_refs
                                if item.kind in SUBMISSION_EVIDENCE_KINDS
                            ),
                            None,
                        )
                        if evidence is None:
                            submit_outcome = self._reject_untrusted_verified(
                                submit_outcome,
                                reason=(
                                    "The adapter reported a submission without eligible "
                                    "submission evidence; success was not recorded"
                                ),
                            )
                        else:
                            self.ledger.mark_submission_verified(
                                intent_id=intent.intent_id,
                                evidence=evidence,
                            )
                    if submit_outcome.status is not OutcomeStatus.SUBMITTED_VERIFIED:
                        current = self.ledger.get_submission_intent(intent.intent_id)
                        if current.status is SubmissionStatus.SUBMITTING:
                            self.ledger.mark_submission_unknown(intent.intent_id)
                        # The adapter may intentionally convert a browser
                        # exception into a structured failure. Once Gate B was
                        # consumed and an intent was reserved, however, every
                        # non-verified result is uncertain and must never remain
                        # retryable or look like a safe no-click policy skip.
                        adapter_reason = (
                            submit_outcome.reason_code.value
                            if hasattr(submit_outcome.reason_code, "value")
                            else str(submit_outcome.reason_code)
                        )
                        submit_outcome = ApplicationOutcome(
                            run_id=bundle.run_id,
                            job_id=bundle.job.job_id,
                            status=OutcomeStatus.SUBMIT_UNKNOWN,
                            phase=OutcomePhase.VERIFY,
                            reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
                            message=(
                                "A submission intent was reserved, but explicit "
                                "confirmation was not observed; human "
                                "reconciliation is required"
                            ),
                            adapter=submit_outcome.adapter,
                            checkpoint=f"submission-intent:{intent.intent_id}",
                            evidence_refs=submit_outcome.evidence_refs,
                            details={
                                "adapter_status": submit_outcome.status.value,
                                "adapter_reason_code": adapter_reason,
                                "do_not_retry_submit": True,
                                "human_reconciliation_required": True,
                            },
                        )
                if intent is not None:
                    outcome_value = submit_outcome.to_dict()
                    outcome_details = dict(submit_outcome.details)
                    outcome_details["submission_intent_id"] = intent.intent_id
                    outcome_details["actual_pre_submit_review_hash"] = (
                        review_hash
                    )
                    outcome_value["details"] = outcome_details
                    submit_outcome = ApplicationOutcome.from_dict(
                        outcome_value
                    )
                self._record_outcome(submit_outcome)
                return submit_outcome
        except LeaseUnavailableError as exc:
            outcome = ApplicationOutcome(
                run_id=bundle.run_id,
                job_id=bundle.job.job_id,
                status=OutcomeStatus.FAILED_RETRYABLE,
                phase=OutcomePhase.QUEUE,
                reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
                message=f"Another worker owns the required lease: {exc.resource}",
                retryable=True,
            )
            self._record_outcome(outcome)
            return outcome


__all__ = ["JobApplicationEngine"]
