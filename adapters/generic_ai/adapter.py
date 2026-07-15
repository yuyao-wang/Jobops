"""Modular, token-efficient generic adapter with permit-gated submission."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.private_home import PrivateHome
from adapters.shared import invoke_gate_b_validator

from .cache import RecipeCache
from .executor import click_next, click_submit, execute_resolved_fields
from .fingerprinter import fingerprint_form, fingerprint_review
from .models import FormIR
from .observer import observe_form
from .resolver import AnswerResolver, Sensitivity, map_unknown_controls
from .verifier import detect_submission_evidence, is_review_ready, verify_fields


ADAPTER_NAME = "generic_ai"


def _digest_untrusted(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _control_digest(control: Any) -> str:
    projection = {
        "index": int(getattr(control, "index", 0)),
        "role": str(getattr(control, "role", "")),
        "type": str(getattr(control, "input_type", "")),
        "selector": str(getattr(control, "selector", "")),
        "label": str(getattr(control, "label", "")),
        "aria": str(getattr(control, "aria_label", "")),
        "name": str(getattr(control, "name", "")),
    }
    encoded = json.dumps(
        projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_failure(failure: Any, *, code: str) -> dict[str, Any]:
    return {
        "index": int(getattr(failure, "index", 0)),
        "control_digest": _digest_untrusted(
            "|".join(
                (
                    str(getattr(failure, "index", 0)),
                    str(getattr(failure, "label", "")),
                )
            )
        ),
        "canonical_key": str(getattr(failure, "canonical_key", "")),
        "error_code": code,
    }


def _safe_browser_error(code: str, error: BaseException) -> dict[str, str]:
    return {
        "error_code": code,
        "error_digest": _digest_untrusted(error),
    }


def _evidence_ref(evidence) -> EvidenceRef:
    kind = (
        EvidenceKind.CONFIRMATION_URL
        if evidence.kind == "confirmation_url"
        else EvidenceKind.CONFIRMATION_TEXT
    )
    evidence_value = evidence.url if kind is EvidenceKind.CONFIRMATION_URL else evidence.text
    digest = hashlib.sha256(evidence_value.encode("utf-8")).hexdigest()
    return EvidenceRef(
        kind=kind,
        sha256=digest,
        metadata={"adapter": ADAPTER_NAME, "source": evidence.kind},
    )


class GenericAIAdapter:
    """Use deterministic execution first and one semantic diff call at most."""

    name = ADAPTER_NAME
    protocol_version = "1.0"

    def __init__(self, *, cache: RecipeCache | None = None):
        if cache is None:
            paths = PrivateHome.discover().ensure()
            cache = RecipeCache(paths.private_recipes)
        self.cache = cache

    async def _load_page(self, page, job_url: str) -> None:
        current = str(getattr(page, "url", "") or "")
        if current == job_url:
            return
        try:
            await page.goto(job_url, wait_until="networkidle", timeout=20000)
        except Exception:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)

    async def run(
        self,
        *,
        page,
        job_url: str,
        profile: dict[str, Any],
        brain=None,
        cover_letter: str = "",
        resume_path: str = "",
        run_id: str,
        job_id: str,
        platform: str = "generic",
        tenant: str = "",
        navigate: bool = True,
        max_steps: int = 12,
        gate_b_token: str | None = None,
        gate_b_validator=None,
    ) -> ApplicationOutcome:
        if navigate:
            try:
                await self._load_page(page, job_url)
            except Exception as exc:
                return ApplicationOutcome(
                    run_id=run_id,
                    job_id=job_id,
                    status=OutcomeStatus.FAILED_RETRYABLE,
                    phase=OutcomePhase.INSPECT,
                    reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
                    message="Application page could not be loaded",
                    adapter=self.name,
                    retryable=True,
                    details=_safe_browser_error("PAGE_LOAD_FAILED", exc),
                )

        resolver = AnswerResolver(
            profile, cover_letter=cover_letter, resume_path=resume_path
        )
        model_calls = 0
        classification_failed = False

        for step in range(1, max_steps + 1):
            evidence = await detect_submission_evidence(page)
            if evidence is not None:
                # A confirmation-looking page is evidence only after this run
                # has reserved an intent and clicked submit.  It may be stale,
                # belong to another tab/run, or represent a prior application.
                return ApplicationOutcome.needs_user(
                    run_id=run_id,
                    job_id=job_id,
                    status=OutcomeStatus.SUBMIT_UNKNOWN,
                    phase=OutcomePhase.VERIFY,
                    reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
                    message=(
                        "A confirmation-like page was observed before this run "
                        "reserved a submission intent or clicked submit; reconcile manually"
                    ),
                    adapter=self.name,
                    checkpoint=f"generic.step.{step}.uncorrelated_confirmation",
                    details={
                        "model_calls": model_calls,
                        "step": step,
                        "uncorrelated_confirmation": True,
                    },
                )

            try:
                form = await observe_form(page, platform=platform, tenant=tenant)
            except Exception as exc:
                return ApplicationOutcome(
                    run_id=run_id,
                    job_id=job_id,
                    status=OutcomeStatus.FAILED_RETRYABLE,
                    phase=OutcomePhase.INSPECT,
                    reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
                    message="Compact form observation failed",
                    adapter=self.name,
                    retryable=True,
                    checkpoint=f"generic.step.{step}",
                    details=_safe_browser_error("FORM_OBSERVATION_FAILED", exc),
                )

            fingerprint = fingerprint_form(form)
            recipe = self.cache.load(fingerprint)
            semantic_mappings: dict[int, str] = {}
            if recipe:
                by_selector = {action.selector: action.canonical_key for action in recipe.actions}
                semantic_mappings.update(
                    {
                        control.index: by_selector[control.selector]
                        for control in form.controls
                        if control.selector in by_selector
                    }
                )

            resolved, unresolved = resolver.resolve_form(form, semantic_mappings)
            if unresolved and brain is not None and model_calls == 0:
                unknown_controls = [field.control for field in unresolved]
                model_calls += 1
                try:
                    mapped = map_unknown_controls(
                        brain,
                        unknown_controls,
                        private_values=resolver.prompt_redactions(),
                    )
                except Exception:
                    # Model/CLI errors are deliberately reduced to a redacted
                    # classification failure. Candidate values and provider
                    # exception text must not enter outcomes or logs here.
                    mapped = {}
                    classification_failed = True
                if mapped:
                    semantic_mappings.update(mapped)
                    resolved, unresolved = resolver.resolve_form(form, semantic_mappings)

            if unresolved:
                sensitive = any(
                    field.sensitivity
                    in {Sensitivity.LEGAL, Sensitivity.COMPENSATION, Sensitivity.VOLUNTARY_SELF_ID}
                    for field in unresolved
                )
                return ApplicationOutcome.needs_user(
                    run_id=run_id,
                    job_id=job_id,
                    status=(
                        OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER
                        if sensitive
                        else OutcomeStatus.NEEDS_USER
                    ),
                    phase=OutcomePhase.FILL,
                    reason_code=(
                        ReasonCode.SENSITIVE_ANSWER_REQUIRED
                        if sensitive
                        else ReasonCode.UNKNOWN_REQUIRED_QUESTION
                    ),
                    message="Required controls could not be mapped to verified answers",
                    adapter=self.name,
                    checkpoint=f"generic.step.{step}",
                    details={
                        "model_calls": model_calls,
                        "classification_failed": classification_failed,
                        "unresolved": [
                            {
                                "index": item.control.index,
                                "control_digest": _control_digest(item.control),
                                "reason_code": "UNMAPPED_REQUIRED_CONTROL",
                                "sensitivity": item.sensitivity.value,
                            }
                            for item in unresolved
                        ],
                    },
                )

            fill_report = await execute_resolved_fields(page, resolved)
            if fill_report.failures:
                return ApplicationOutcome.needs_user(
                    run_id=run_id,
                    job_id=job_id,
                    phase=OutcomePhase.FILL,
                    reason_code=ReasonCode.VALIDATION_FAILED,
                    message="One or more required controls could not be filled deterministically",
                    adapter=self.name,
                    checkpoint=f"generic.step.{step}",
                    details={
                        "model_calls": model_calls,
                        "failures": [
                            _safe_failure(failure, code="DETERMINISTIC_FILL_FAILED")
                            for failure in fill_report.failures
                        ],
                    },
                )

            fresh_form = await observe_form(page, platform=platform, tenant=tenant)
            verification = await verify_fields(page, fresh_form, resolved)
            if not verification.valid:
                return ApplicationOutcome.needs_user(
                    run_id=run_id,
                    job_id=job_id,
                    phase=OutcomePhase.VALIDATE,
                    reason_code=ReasonCode.VALIDATION_FAILED,
                    message="Browser read-back did not confirm all required values",
                    adapter=self.name,
                    checkpoint=f"generic.step.{step}",
                    details={
                        "model_calls": model_calls,
                        "failures": [
                            _safe_failure(failure, code="READBACK_VALIDATION_FAILED")
                            for failure in verification.failures
                        ],
                        "dom_error_digests": [
                            _digest_untrusted(error)
                            for error in verification.errors
                        ],
                    },
                )

            if fill_report.recipe_actions:
                self.cache.save(
                    fingerprint=fingerprint,
                    platform=form.platform,
                    tenant=form.tenant,
                    stage=form.stage,
                    actions=fill_report.recipe_actions,
                )

            if is_review_ready(fresh_form, verification):
                review_hash = fingerprint_review(fresh_form, verification)
                if not gate_b_token:
                    return ApplicationOutcome.review_ready(
                        run_id=run_id,
                        job_id=job_id,
                        adapter=self.name,
                        checkpoint=f"generic.step.{step}.review",
                        details={
                            "review_fingerprint": review_hash,
                            "model_calls": model_calls,
                            "filled_fields": fill_report.completed,
                        },
                    )

                if gate_b_validator is None:
                    return ApplicationOutcome(
                        run_id=run_id,
                        job_id=job_id,
                        status=OutcomeStatus.SKIPPED_POLICY,
                        phase=OutcomePhase.SUBMIT,
                        reason_code=ReasonCode.POLICY_DENIED,
                        message="A Gate B token was supplied without the orchestrator's validation callback",
                        adapter=self.name,
                    )
                permit_valid = await invoke_gate_b_validator(
                    gate_b_validator,
                    gate_b_token,
                    job_id=job_id,
                    run_id=run_id,
                    review_fingerprint=review_hash,
                )
                if not permit_valid:
                    return ApplicationOutcome(
                        run_id=run_id,
                        job_id=job_id,
                        status=OutcomeStatus.AWAITING_GATE_B,
                        phase=OutcomePhase.REVIEW,
                        reason_code=ReasonCode.GATE_B_REQUIRED,
                        message="A valid Gate B permit bound to this freshly verified review is required",
                        adapter=self.name,
                        checkpoint=review_hash,
                    )
                clicked = await click_submit(page, fresh_form.submit_selector, fresh_form.submit_text)
                if not clicked:
                    return ApplicationOutcome(
                        run_id=run_id,
                        job_id=job_id,
                        status=OutcomeStatus.FAILED_RETRYABLE,
                        phase=OutcomePhase.SUBMIT,
                        reason_code=ReasonCode.RETRYABLE_BROWSER_ERROR,
                        message="Validated submission control could not be activated",
                        adapter=self.name,
                        retryable=True,
                    )

                await asyncio.sleep(2)
                evidence = await detect_submission_evidence(page)
                if evidence is None:
                    return ApplicationOutcome.needs_user(
                        run_id=run_id,
                        job_id=job_id,
                        status=OutcomeStatus.SUBMIT_UNKNOWN,
                        phase=OutcomePhase.VERIFY,
                        reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
                        message="Submit was clicked but explicit confirmation was not observed; do not retry blindly",
                        adapter=self.name,
                        checkpoint=review_hash,
                    )
                ref = _evidence_ref(evidence)
                return ApplicationOutcome.submitted_verified(
                    run_id=run_id,
                    job_id=job_id,
                    adapter=self.name,
                    evidence_refs=(ref,),
                    details={
                        "model_calls": model_calls,
                        "review_fingerprint": review_hash,
                    },
                )

            if fresh_form.next_selector or fresh_form.next_text:
                if await click_next(page, fresh_form.next_selector, fresh_form.next_text):
                    await asyncio.sleep(1)
                    continue

            return ApplicationOutcome(
                run_id=run_id,
                job_id=job_id,
                status=OutcomeStatus.FAILED_UNSUPPORTED,
                phase=OutcomePhase.REVIEW,
                reason_code=ReasonCode.UNSUPPORTED_ATS,
                message="No deterministic next or review transition was found",
                adapter=self.name,
                checkpoint=f"generic.step.{step}",
                details={"model_calls": model_calls, "form_fingerprint": fingerprint},
            )

        return ApplicationOutcome(
            run_id=run_id,
            job_id=job_id,
            status=OutcomeStatus.FAILED_TERMINAL,
            phase=OutcomePhase.REVIEW,
            reason_code=ReasonCode.VALIDATION_FAILED,
            message=f"Generic adapter exceeded {max_steps} steps",
            adapter=self.name,
            details={"model_calls": model_calls},
        )


async def apply_generic_ai(**kwargs) -> ApplicationOutcome:
    """Functional entry point used by the adapter registry and Codex CLI."""
    return await GenericAIAdapter().run(**kwargs)
