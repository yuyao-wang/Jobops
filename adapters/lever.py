"""Deterministic Lever application adapter."""

from __future__ import annotations

from .protocol import BaseATSAdapter
from .shared import FieldSpec


class LeverAdapter(BaseATSAdapter):
    name = "lever"
    host_patterns = ("jobs.lever.co", "lever.co")
    dom_markers = (
        "form.application-form",
        '[data-qa="application-form"]',
        '.application-page .application-form',
    )
    field_specs = (
        FieldSpec("full_name", ('input[name="name"]', 'input[data-qa="name-input"]'), "text", "Full name"),
        FieldSpec("email", ('input[name="email"]', 'input[data-qa="email-input"]'), "email", "Email"),
        FieldSpec("phone", ('input[name="phone"]', 'input[data-qa="phone-input"]'), "tel", "Phone"),
        FieldSpec("current_company", ('input[name="org"]', 'input[data-qa="org-input"]'), "text", "Current company"),
        FieldSpec("resume", ('input[type="file"][name="resume"]', 'input[type="file"][data-qa="resume-input"]'), "file", "Resume"),
        FieldSpec("linkedin", ('input[name="urls[LinkedIn]"]', 'input[name*="linkedin" i]', 'input[aria-label*="LinkedIn" i]'), "url", "LinkedIn"),
        FieldSpec("github", ('input[name="urls[GitHub]"]', 'input[name*="github" i]'), "url", "GitHub"),
        FieldSpec("portfolio", ('input[name="urls[Portfolio]"]', 'input[name="urls[Website]"]', 'input[name*="portfolio" i]'), "url", "Portfolio"),
        FieldSpec("cover_letter", ('textarea[name="comments"]', 'textarea[data-qa="additional-information"]'), "textarea", "Additional information"),
    )
    submit_selectors = (
        '[data-qa="btn-submit"]',
        'button[data-qa="submit"]',
        'button[type="submit"]',
        'input[type="submit"]',
    )
    confirmation_selectors = (
        '[data-qa="application-success"]',
        ".application-confirmation",
        ".postings-btn-wrapper + .confirmation",
        '[data-jobops-confirmation="lever"]',
    )


__all__ = ["LeverAdapter"]
