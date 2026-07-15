---
name: job-status
description: Analyze Jobops queue projections, run outcomes, blockers, permits, model-call counts, and submission evidence from the private event ledger. Use for progress reports, debugging, throughput analysis, or deciding the next safe action.
---

# Job Status

This is a repository-scoped Codex workflow for the Jobops CLI.

Use deterministic ledger projections; do not reconstruct status from browser recollection.

## Workflow

1. Run `.venv/bin/python jobctl.py status` for aggregate counts or `.venv/bin/python jobctl.py status --run-id <id>` for one run.
2. Run `.venv/bin/python jobctl.py queue --list` to compare the human-readable CSV projection with ledger outcomes.
3. Classify outcomes exactly:
   - `REVIEW_READY`: filled and validated, not submitted.
   - `SUBMITTED_VERIFIED`: submitted with explicit evidence.
   - `SUBMIT_UNKNOWN`: click may have occurred; never auto-retry.
   - `NEEDS_USER*` or `AWAITING_GATE_*`: report the named handoff.
   - retryable/terminal/policy failures: report reason, checkpoint, and next safe action.
4. Report supported-ATS Review rate, median model calls on normal forms, unknown-answer handoffs, duplicate-submit blocks, and evidence coverage. Do not claim the ≥95% target from synthetic fixtures as real-site production performance.
5. Suggest the next queue action without sending outreach, email, or follow-up; those extensions remain disabled.

## Evidence Rules

- A submit click is not evidence.
- Every `SUBMITTED_VERIFIED` must reference confirmation text, URL, ATS ID, network response, screenshot, or correlated email evidence.
- Redact private paths, candidate values, cookies, and mailbox contents from reports.
- Preserve ledger events; correct projections with a new event rather than editing history.
