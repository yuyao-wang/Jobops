"""Deterministic Ashby application adapter."""

from __future__ import annotations

from .protocol import BaseATSAdapter
from .shared import FieldSpec


class AshbyAdapter(BaseATSAdapter):
    name = "ashby"
    host_patterns = ("jobs.ashbyhq.com", "ashbyhq.com")
    dom_markers = (
        '[data-testid="application-form"]',
        'form[data-ashby-application-form="true"]',
        '.ashby-application-form',
    )
    field_specs = (
        FieldSpec("full_name", ('input[name="name"]', 'input[id*="_systemfield_name"]', 'input[autocomplete="name"]'), "text", "Full name"),
        FieldSpec("first_name", ('input[name="firstName"]', 'input[autocomplete="given-name"]'), "text", "First name"),
        FieldSpec("last_name", ('input[name="lastName"]', 'input[autocomplete="family-name"]'), "text", "Last name"),
        FieldSpec("email", ('input[name="email"]', 'input[id*="_systemfield_email"]', 'input[autocomplete="email"]'), "email", "Email"),
        FieldSpec("phone", ('input[name="phone"]', 'input[id*="_systemfield_phone"]', 'input[autocomplete="tel"]'), "tel", "Phone"),
        FieldSpec("location", ('input[name="location"]', 'input[id*="_systemfield_location"]', 'input[autocomplete="address-level2"]'), "text", "Location"),
        FieldSpec("resume", ('input[type="file"][name*="resume" i]', 'input[type="file"][data-testid="resume-upload"]', 'input[id*="_systemfield_resume"]'), "file", "Resume"),
        FieldSpec("linkedin", ('input[name*="linkedin" i]', 'input[aria-label*="LinkedIn" i]'), "url", "LinkedIn"),
        FieldSpec("github", ('input[name*="github" i]', 'input[aria-label*="GitHub" i]'), "url", "GitHub"),
        FieldSpec("portfolio", ('input[name*="portfolio" i]', 'input[name*="website" i]'), "url", "Portfolio"),
        FieldSpec("cover_letter", ('textarea[name*="cover" i]', 'textarea[aria-label*="cover letter" i]'), "textarea", "Cover letter"),
    )
    submit_selectors = (
        'button[data-testid="application-submit"]',
        'button[type="submit"]',
    )
    confirmation_selectors = (
        '[data-testid="application-confirmation"]',
        '[data-testid="application-success"]',
        '.ashby-application-confirmation',
        '[data-jobops-confirmation="ashby"]',
    )


__all__ = ["AshbyAdapter"]
