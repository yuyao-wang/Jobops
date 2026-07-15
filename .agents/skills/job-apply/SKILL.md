---
name: job-apply
description: Execute one private job application through the deterministic ATS adapter protocol and stop safely at Review or a standardized blocker. Use when filling, resuming, reviewing, or explicitly submitting one queued application.
---

# Job Apply

This is a repository-scoped Codex workflow for the Jobops CLI.

Run one bounded application episode. Playwright adapters execute; Codex handles policy and exceptions.

## Workflow

1. Inspect the next row with `.venv/bin/python jobctl.py queue --list --limit 1` and its tier with `.venv/bin/python jobctl.py policy`.
2. Ensure `$job-materials` has prepared the required tier-specific material. Do not silently substitute a missing requested resume variant.
3. Run `.venv/bin/python jobctl.py apply-csv --limit 1` to reach Review. Supported Greenhouse, Lever, Ashby, Jobvite, and Workday forms should use zero model calls.
4. If the Outcome is `REVIEW_READY`, report the review and permit requirement. Do not translate this into “submitted.”
5. For a human Gate B, first show the persisted Review and run ID. Only after a new explicit confirmation, run `.venv/bin/python jobctl.py submit-reviewed --run-id <run-id> --approve`. There is no same-invocation Gate B preapproval. Low-tier policy may use `apply-csv --submit`, but the core still consumes both bound permits.
6. Accept success only as `SUBMITTED_VERIFIED` with evidence. Treat `SUBMIT_UNKNOWN` as a hard no-retry handoff.

## Handoffs

Stop and tell the user exactly what remains when the Outcome identifies CAPTCHA, MFA, email-verification fallback, account lock, a new sensitive required question, or human Gate A/Gate B. When materials are missing, invoke `$job-materials` yourself and retry Review; do not ask the user to author them. Prefer Safari for a user-visible handoff when requested, but automated execution and reusable sessions remain in leased persistent Chromium.

## Safety

- Never bypass CAPTCHA, MFA, or anti-bot controls.
- Never guess a required answer or choose a merely similar option.
- Never click Submit outside the adapter's one-time Gate B path.
- Never retry after a click without explicit confirmation evidence.
- Keep normal observations compact; do not stream full HTML, page history, or candidate values to a model.
