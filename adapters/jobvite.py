"""Deterministic Jobvite application adapter."""

from __future__ import annotations

from .protocol import BaseATSAdapter
from .shared import FieldSpec


class JobviteAdapter(BaseATSAdapter):
    name = "jobvite"
    host_patterns = ("jobs.jobvite.com", "jobvite.com")
    dom_markers = (
        "form.jv-application-form",
        "#jvApplicationForm",
        '[data-qa="jv-application-form"]',
    )
    field_specs = (
        FieldSpec("first_name", ("#jvFirstName", 'input[name="firstName"]', 'input[name="candidate.firstName"]'), "text", "First name"),
        FieldSpec("last_name", ("#jvLastName", 'input[name="lastName"]', 'input[name="candidate.lastName"]'), "text", "Last name"),
        FieldSpec("email", ("#jvEmail", 'input[name="email"]', 'input[name="candidate.email"]'), "email", "Email"),
        FieldSpec("phone", ("#jvPhone", 'input[name="phone"]', 'input[name="candidate.phone"]'), "tel", "Phone"),
        FieldSpec("resume", ("#jvResume", 'input[type="file"][name*="resume" i]', 'input[data-qa="resume-upload"]'), "file", "Resume"),
        FieldSpec("linkedin", ('input[name*="linkedin" i]', 'input[aria-label*="LinkedIn" i]'), "url", "LinkedIn"),
        FieldSpec("github", ('input[name*="github" i]', 'input[aria-label*="GitHub" i]'), "url", "GitHub"),
        FieldSpec("portfolio", ('input[name*="portfolio" i]', 'input[name*="website" i]'), "url", "Portfolio"),
        FieldSpec("cover_letter", ('textarea[name*="cover" i]', "#jvCoverLetter"), "textarea", "Cover letter"),
    )
    submit_selectors = (
        '#jvApplicationForm button[type="submit"]',
        'button[data-qa="submit-application"]',
        "button.jv-button-primary",
        'input[type="submit"]',
    )
    confirmation_selectors = (
        ".jv-application-confirmation",
        '[data-qa="application-confirmation"]',
        "#jvThankYou",
        '[data-jobops-confirmation="jobvite"]',
    )


__all__ = ["JobviteAdapter"]
