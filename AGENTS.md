# Jobops Agent Guide

Jobops is a Codex-native, privacy-first job application operating system. Codex coordinates policy, materials, and exceptions; deterministic adapters execute known ATS forms.

## Non-negotiable boundaries

- Real candidate data lives in `~/Library/Application Support/Jobops` or `JOBOPS_HOME`, never in this repository.
- ATS and mailbox account passwords plus the permit HMAC key live in macOS Keychain. Optional AI-provider API tokens may be supplied only through runtime environment variables and must never be persisted. Never pass a password, token, cookie, recovery code, or mailbox secret in argv, CSV, YAML, JSON, logs, prompts, fixtures, or Git.
- Never invent or infer identity, legal name, work authorization, sponsorship, employment, education, compensation, EEO/self-identification, criminal/conflict answers, dates, metrics, or portfolio claims.
- Never bypass CAPTCHA, MFA, account locks, anti-bot controls, or mailbox security warnings.
- A submit click is not success. Only `SUBMITTED_VERIFIED` with an eligible `EvidenceRef` is submitted.
- `SUBMIT_UNKNOWN` is a hard no-retry state until a human reconciles it.
- Never run application episodes concurrently. Respect both run and `browser:chromium` leases.
- Outreach, cold email, LinkedIn networking, and follow-up remain disabled unless a separate extension is explicitly enabled.

## Runtime commands

```bash
.venv/bin/python jobctl.py init
.venv/bin/python jobctl.py policy
.venv/bin/python jobctl.py mailbox --host imap.example.com
.venv/bin/python jobctl.py queue --list
.venv/bin/python jobctl.py apply-csv --limit 1
.venv/bin/python jobctl.py status
.venv/bin/python jobctl.py submit-reviewed --run-id run-... --approve
```

`apply-csv` stops at Review by default. `--approve-gate-a` is an explicit review of prepared materials. Human Gate B is never accepted in the same invocation: inspect the persisted Review, then use `submit-reviewed --run-id ... --approve`. Never infer either human approval.

## Tier treatment

- High: bespoke resume, visual QA, narrative cover letter grounded in a true experience and company/role alignment; human Gate A and Gate B in low-risk mode.
- Medium: targeted resume/letter; Codex Gate A and human Gate B.
- Low: approved existing variant and letter only when required; Codex may issue both gates if no risk signal exists.

High and Medium must load a valid `documents/generated/<job_id>/manifest.json` from Private Home before browser launch. The manifest binds artifact hashes, facts QA, job specificity, and visual QA; High also binds a true narrative-alignment attestation. Preparing a missing manifest is Codex work through `job-materials`, not a human handoff.

## Execution architecture

- `jobctl.py`: public CLI and private queue projection.
- `core/`: Private Home, profile projection, bundles, policy, outcomes, Event Ledger, leases, permits, browser broker, and application engine.
- `adapters/protocol.py`: shared deterministic lifecycle.
- `adapters/{greenhouse,lever,ashby,jobvite,workday}.py`: supported ATS implementations.
- `adapters/generic_ai/`: bounded observer, fingerprinter, resolver, executor, verifier, and value-free cache.
- `adapters/stagehand_adapter.py`: small legacy boolean façade; production routing must not use `adapters/legacy/stagehand_monolith.py`.
- `auth/`: Security.framework credential provider and optional mailbox correlation.
- `workers/node/`: versioned JSON-lines coexistence bridge; no payload in argv.
- `.agents/skills/`: auto-discovered, repository-scoped Codex workflows; no candidate data.

The normal supported-ATS path must make zero model calls. A generic semantic request may contain compact control structure only, is limited to one call per run, and must use a tool-free backend approved for untrusted browser input; agentic Codex/Claude CLIs fail closed. Values are injected and verified locally.

## Outcomes and permits

Every adapter returns `ApplicationOutcome`. Preserve exact statuses and exit codes. Do not collapse a blocker into a boolean in new code.

Gate A binds the preflight application bundle. Gate B binds hashed browser read-backs and uploaded bytes from a Review persisted in an earlier invocation, then recomputed immediately before submission. Both permits are signed, expiring, and one-time. Lease validation, Gate B consumption, and submission-intent reservation happen immediately before the click.

## Private profile changes

Use `PrivateHome` and `CandidateVault`; do not read ignored legacy `profile.yaml` in production. A new answer record needs a canonical key, verified value, source, sensitivity, scope, confirmation time, and optional expiry. Ask the user when a required sensitive answer is new or ambiguous.

## Testing

Use synthetic identities and sanitized fixtures only.

```bash
.venv/bin/python -m pytest -q --ignore=tests/test_real_forms.py
.venv/bin/python -m pytest -q tests/test_ats_adapter_contract.py tests/test_workday_adapter.py
```

Do not run `tests/test_real_forms.py` or submit to a live site as part of routine validation. Contract metrics and live-site metrics must be reported separately.

Before committing, run compile checks, `git diff --check`, the five Skill validators, and a privacy scan for candidate values, secrets, absolute private paths, cookies, and unredacted screenshots.

## Compatibility

This repository preserves MR.Jobs Git history and MIT licensing. `main.py`, dashboard/discovery utilities, and the legacy façade remain during migration. New application execution belongs in `jobctl`, the core engine, and structured adapters; do not add safety-sensitive behavior to the legacy monolith.
