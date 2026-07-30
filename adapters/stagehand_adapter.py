"""Small compatibility façade for the historical MR.Jobs apply API.

The old Stagehand implementation is retained under
``adapters.legacy.stagehand_monolith`` only for regression tests.  Production
calls are routed to deterministic ATS adapters or, for unknown sites, the
bounded modular ``GenericAIAdapter``.  The façade deliberately keeps the old
boolean return shape while enforcing the new outcome contract:

* dry-run succeeds only after a validated Review state;
* live mode succeeds only for ``SUBMITTED_VERIFIED``;
* supplying no valid Gate B permit can never result in a submit click.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
from contextlib import ExitStack
from typing import Any, Mapping
from uuid import uuid4

from adapters.generic_ai.verifier import detect_submission_evidence
from adapters.registry import AdapterRegistry, AdapterRunRequest
from core.application_execution_profile import (
    ApplicationExecutionIdentityProfile,
)
from core.outcomes import ApplicationOutcome, OutcomeStatus


logger = logging.getLogger("stagehand_adapter")


def is_stagehand_available() -> bool:
    """Return whether the replacement modular browser adapter is available."""

    return True


def _stable_job_id(job_url: str, profile: Mapping[str, Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    configured = str(profile.get("job_id") or "").strip()
    if configured:
        return configured
    digest = hashlib.sha256(job_url.encode("utf-8")).hexdigest()[:20]
    return f"legacy-{digest}"


def _run_id(profile: Mapping[str, Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    configured = str(profile.get("run_id") or "").strip()
    return configured or f"legacy-{uuid4().hex}"


def _legacy_bool(outcome: ApplicationOutcome, *, dry_run: bool) -> bool:
    """Map a structured result to the historical, intentionally strict bool."""

    if dry_run:
        return outcome.status in {
            OutcomeStatus.REVIEW_READY,
            OutcomeStatus.SUBMITTED_VERIFIED,
        }
    return outcome.status is OutcomeStatus.SUBMITTED_VERIFIED


async def _run_routed(
    *,
    page: Any,
    job_url: str,
    profile: Mapping[str, Any],
    brain: Any,
    cover_letter: str,
    dry_run: bool,
    platform: str,
    job_id: str | None,
    run_id: str | None,
    gate_b_permit: Any,
    gate_b_validator: Any,
    permit_service: Any,
    permit_bindings: Any,
    ledger: Any,
    credential_store: Any,
    mailbox_verifier: Any,
    navigate: bool,
) -> ApplicationOutcome:
    """Execute one registry route and preserve a structured result internally."""

    runtime_profile = dict(profile)
    resolved_job_id = _stable_job_id(job_url, runtime_profile, job_id)
    resolved_run_id = _run_id(runtime_profile, run_id)

    # Permit bindings are authoritative.  This also lets transitional callers
    # provide a bound permit without duplicating IDs in the compatibility API.
    if permit_bindings is not None:
        resolved_job_id = str(getattr(permit_bindings, "job_id", resolved_job_id))
        resolved_run_id = str(getattr(permit_bindings, "run_id", resolved_run_id))

    request = AdapterRunRequest(
        page=page,
        job_url=job_url,
        job_id=resolved_job_id,
        run_id=resolved_run_id,
        profile=ApplicationExecutionIdentityProfile.from_legacy_profile(
            runtime_profile
        ),
        resume_path=str(runtime_profile.get("resume_path") or ""),
        cover_letter=cover_letter,
        answers=runtime_profile.get("common_answers", {}),
        request_submit=not dry_run,
        # Never forward a submit capability during a dry-run.  The generic
        # adapter keys submission off the opaque token itself, while the ATS
        # protocol also checks ``request_submit``; clearing both paths here
        # makes the legacy flag an absolute boundary.
        gate_b_permit=None if dry_run else gate_b_permit,
        gate_b_validator=None if dry_run else gate_b_validator,
        credential_store=credential_store,
        mailbox_verifier=mailbox_verifier,
        brain=brain,
        platform_hint=platform,
        navigate=navigate,
    )
    outcome = await AdapterRegistry().run(request)
    logger.info(
        "adapter outcome: adapter=%s status=%s job_id=%s",
        outcome.adapter,
        outcome.status.value,
        resolved_job_id,
    )
    return outcome


async def apply_stagehand(
    page: Any,
    job_url: str,
    profile: dict,
    brain: Any,
    cover_letter: str = "",
    dry_run: bool = True,
    max_steps: int = 12,
    *,
    platform: str = "",
    job_id: str | None = None,
    run_id: str | None = None,
    gate_b_permit: Any = None,
    gate_b_validator: Any = None,
    permit_service: Any = None,
    permit_bindings: Any = None,
    ledger: Any = None,
    credential_store: Any = None,
    mailbox_verifier: Any = None,
    navigate: bool = True,
) -> bool:
    """Route the historical Stagehand call through Jobops adapters.

    ``max_steps`` remains accepted for source compatibility.  Step budgets now
    belong to the selected adapter and orchestration policy, not this façade.
    """

    del max_steps
    outcome = await _run_routed(
        page=page,
        job_url=job_url,
        profile=profile,
        brain=brain,
        cover_letter=cover_letter,
        dry_run=dry_run,
        platform=platform,
        job_id=job_id,
        run_id=run_id,
        gate_b_permit=gate_b_permit,
        gate_b_validator=gate_b_validator,
        permit_service=permit_service,
        permit_bindings=permit_bindings,
        ledger=ledger,
        credential_store=credential_store,
        mailbox_verifier=mailbox_verifier,
        navigate=navigate,
    )
    return _legacy_bool(outcome, dry_run=dry_run)


async def apply_smart(
    page: Any,
    job_url: str,
    profile: dict,
    brain: Any,
    cover_letter: str = "",
    dry_run: bool = True,
    platform: str = "",
    company: str = "",
    title: str = "",
    description: str = "",
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    gate_b_permit: Any = None,
    gate_b_validator: Any = None,
    permit_service: Any = None,
    permit_bindings: Any = None,
    ledger: Any = None,
    credential_store: Any = None,
    mailbox_verifier: Any = None,
    navigate: bool = True,
) -> bool:
    """Resolve aggregator links, then route through the adapter registry."""

    from utils.url_resolver import is_aggregator_url, resolve_apply_url

    if is_aggregator_url(job_url):
        resolution = await resolve_apply_url(
            page,
            job_url=job_url,
            company=company,
            title=title,
            description=description,
            platform=platform,
        )
        if resolution.get("apply_email"):
            logger.info("email-only application requires a separate authorized workflow")
            return False
        resolved_url = str(resolution.get("resolved_url") or job_url)
        if resolved_url == job_url and resolution.get("resolution") == "unresolved":
            logger.info("aggregator URL did not resolve to an application form")
            return False
        job_url = resolved_url

    outcome = await _run_routed(
        page=page,
        job_url=job_url,
        profile=profile,
        brain=brain,
        cover_letter=cover_letter,
        dry_run=dry_run,
        platform=platform,
        job_id=job_id,
        run_id=run_id,
        gate_b_permit=gate_b_permit,
        gate_b_validator=gate_b_validator,
        permit_service=permit_service,
        permit_bindings=permit_bindings,
        ledger=ledger,
        credential_store=credential_store,
        mailbox_verifier=mailbox_verifier,
        navigate=navigate,
    )
    return _legacy_bool(outcome, dry_run=dry_run)


async def _detect_page_state(page: Any) -> str:
    """Return a compact, strict state for remaining legacy callers."""

    if await detect_submission_evidence(page) is not None:
        return "confirmation"
    try:
        signals = await page.evaluate(
            """() => ({
                formControls: document.querySelectorAll(
                    'input:not([type="hidden"]), textarea, select'
                ).length,
                invalidControls: document.querySelectorAll(
                    '[aria-invalid="true"], :invalid'
                ).length
            })"""
        )
        if isinstance(signals, int):
            return "form" if signals > 0 else "other"
        if int(signals.get("formControls", 0)) > 0:
            return "form"
        if int(signals.get("invalidControls", 0)) > 0:
            return "error"
    except Exception:
        return "other"
    return "other"


# Two helpers remain as lazy proxies because an older local import script uses
# them in tests.  Their overrides are forwarded so unittest.mock patches keep
# working.  Neither helper is reachable from the production apply functions.
async def _fill_form_step(*args: Any, **kwargs: Any) -> Any:
    legacy = _legacy_module()
    override = globals().get("_resolve_selector")
    with ExitStack() as stack:
        if override is not None:
            stack.enter_context(_temporary_attribute(legacy, "_resolve_selector", override))
        return await legacy._fill_form_step(*args, **kwargs)


async def _handle_navigation_step(*args: Any, **kwargs: Any) -> Any:
    legacy = _legacy_module()
    overrides = {
        "_click_element": globals().get("_click_element"),
        "_detect_page_state": globals().get("_detect_page_state"),
    }
    with ExitStack() as stack:
        for name, value in overrides.items():
            if value is not None:
                stack.enter_context(_temporary_attribute(legacy, name, value))
        return await legacy._handle_navigation_step(*args, **kwargs)


class _temporary_attribute:
    """Tiny local patch helper that avoids a runtime unittest dependency."""

    def __init__(self, target: Any, name: str, value: Any):
        self.target = target
        self.name = name
        self.value = value
        self.original: Any = None

    def __enter__(self) -> None:
        self.original = getattr(self.target, self.name)
        setattr(self.target, self.name, self.value)

    def __exit__(self, *_exc: Any) -> None:
        setattr(self.target, self.name, self.original)


def _legacy_module() -> Any:
    return importlib.import_module("adapters.legacy.stagehand_monolith")


_LAZY_LEGACY_HELPERS = frozenset(
    {
        "get_form_snapshot",
        "analyze_form_fields",
        "_format_a11y_tree",
        "_format_form_summary",
        "build_selector",
        "get_field_value",
        "_find_form_in_iframes",
        "_resolve_selector",
        "_click_element",
    }
)


def __getattr__(name: str) -> Any:
    """Lazily expose a narrow deprecated test surface, never apply routing."""

    if name in _LAZY_LEGACY_HELPERS:
        return getattr(_legacy_module(), name)
    raise AttributeError(name)


__all__ = [
    "apply_smart",
    "apply_stagehand",
    "is_stagehand_available",
]
