---
name: job-orchestrate
description: Orchestrate a private CSV job queue by priority, material strategy, ATS support, permits, and blockers. Use when the user asks to plan, continue, batch, or monitor job applications through Jobops.
---

# Job Orchestrate

This is a repository-scoped Codex workflow for the Jobops CLI.

Coordinate Jobops; do not drive routine browser clicks yourself.

## Workflow

1. From the repository root, run `.venv/bin/python jobctl.py policy` and `.venv/bin/python jobctl.py queue --list`. Do not open private JSON files merely to summarize the queue.
2. Process High, Medium, then Low. Preserve the CSV order within a tier.
3. Apply the tier policy:
   - High: invoke `$job-materials` for a bespoke resume and narrative cover letter before `$job-apply`; require both human permits in low-risk mode.
   - Medium: use targeted materials; Codex may authorize Gate A, but stop for human Gate B.
   - Low: route an approved existing resume; policy may authorize both gates when no risk signal exists.
4. Use `.venv/bin/python jobctl.py apply-csv --limit 1` for one Review episode. Human Gate A uses `--approve-gate-a` only after material review. Human Gate B must be temporally separate: after showing the persisted Review and receiving a new explicit confirmation, run `.venv/bin/python jobctl.py submit-reviewed --run-id <run-id> --approve`.
5. Read the structured Outcome. Continue only after `REVIEW_READY` or `SUBMITTED_VERIFIED`. Resolve `MATERIALS_REQUIRED` yourself through `$job-materials`. Stop for the user on `SUBMIT_UNKNOWN`, account lock, CAPTCHA, MFA, email-verification fallback, a new sensitive required answer, or a human permit request.
6. Do not classify a normal login or registration page as a handoff. Use the tenant-scoped Keychain credential when present; otherwise let `$job-apply` create the account automatically. Verification is the handoff boundary, and all safe fields must already be complete before the page is shown to the user.
7. Summarize the run with `$job-status` using ledger data, not browser memory.

## Invariants

- Never read or upload `~/Library/Application Support/Jobops` into Git.
- Never invent identity, legal, authorization, employment, education, compensation, EEO, or quantified resume facts.
- Keep outreach and follow-up disabled; they are outside this core workflow.
- Do not run two application episodes concurrently. The run/browser lease is authoritative.
- Prefer supported ATS adapters. The generic adapter may classify one compact unresolved form diff, but candidate values remain local.
