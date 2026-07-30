"""Deterministic Greenhouse adapter.

The legacy ``apply_greenhouse`` coroutine remains available, but the ``brain``
argument is intentionally unused: unknown questions now stop for confirmed
input instead of triggering an LLM call.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from core.application_execution_profile import (
    ApplicationExecutionIdentityProfile,
)
from core.outcomes import OutcomeStatus

from .protocol import ApplicationContext, BaseATSAdapter
from .shared import FieldSpec, select_exact_option


class GreenhouseAdapter(BaseATSAdapter):
    name = "greenhouse"
    host_patterns = ("greenhouse.io", "greenhouse.com")
    dom_markers = (
        "#application form #first_name",
        'form[action*="greenhouse"]',
        '[data-qa="job-application-form"]',
    )
    field_specs = (
        FieldSpec("first_name", ("#first_name", 'input[name="job_application[first_name]"]', 'input[name="first_name"]'), "text", "First name"),
        FieldSpec("last_name", ("#last_name", 'input[name="job_application[last_name]"]', 'input[name="last_name"]'), "text", "Last name"),
        FieldSpec("email", ("#email", 'input[name="job_application[email]"]', 'input[name="email"]'), "email", "Email"),
        FieldSpec("phone", ("#phone", 'input[name="job_application[phone]"]', 'input[name="phone"]'), "tel", "Phone"),
        FieldSpec(
            "resume",
            (
                'input[type="file"][name*="resume"]',
                'input[type="file"][id*="resume"]',
                '[data-qa="resume-input"] input[type="file"]',
            ),
            "file",
            "Resume",
        ),
        FieldSpec("cover_letter", ('textarea[name*="cover_letter"]', "#cover_letter", 'textarea[id*="cover"]'), "textarea", "Cover letter"),
        FieldSpec("linkedin", ('input[name*="linkedin" i]', 'input[aria-label*="LinkedIn" i]'), "url", "LinkedIn"),
        FieldSpec("github", ('input[name*="github" i]', 'input[aria-label*="GitHub" i]'), "url", "GitHub"),
        FieldSpec("portfolio", ('input[name*="website" i]', 'input[name*="portfolio" i]'), "url", "Portfolio"),
    )
    submit_selectors = (
        "#submit_app",
        'button[data-qa="submit-application"]',
        'button[type="submit"]',
        'input[type="submit"]',
    )
    confirmation_selectors = (
        '[data-qa="application-confirmation"]',
        ".application--confirmation",
        ".flash-success",
        "#application_confirmation",
        '[data-jobops-confirmation="greenhouse"]',
    )


async def apply_greenhouse(
    page: Any,
    job_url: str,
    profile: Mapping[str, Any],
    brain: Any,
    cover_letter: str = "",
    dry_run: bool = True,
    *,
    gate_b_permit: Any = None,
    gate_b_validator: Any = None,
) -> bool:
    """Compatibility wrapper for the historical MR.Jobs API.

    A live call without a trusted Gate B validator stops safely and returns
    ``False``.  ``brain`` is accepted for call compatibility but never used.
    """

    del brain
    digest = hashlib.sha256(job_url.encode("utf-8")).hexdigest()[:16]
    context = ApplicationContext(
        page=page,
        job_url=job_url,
        job_id=str(profile.get("job_id") or f"greenhouse-{digest}"),
        run_id=str(profile.get("run_id") or f"legacy-{digest}"),
        profile=ApplicationExecutionIdentityProfile.from_legacy_profile(
            profile
        ),
        resume_path=profile.get("resume_path"),
        cover_letter=cover_letter,
        answers=profile.get("common_answers", {}),
        request_submit=not dry_run,
        gate_b_permit=gate_b_permit,
        gate_b_validator=gate_b_validator,
    )
    outcome = await GreenhouseAdapter().run(context)
    if dry_run:
        return outcome.status is OutcomeStatus.REVIEW_READY
    return outcome.status is OutcomeStatus.SUBMITTED_VERIFIED


async def _select_best_option(select_el: Any, target_value: str) -> bool:
    """Backward-compatible exact option selector (no fuzzy substring match)."""

    return await select_exact_option(select_el, target_value)


__all__ = ["GreenhouseAdapter", "apply_greenhouse"]
