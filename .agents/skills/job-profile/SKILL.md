---
name: job-profile
description: Initialize, migrate, inspect, or carefully update the repository-external Jobops candidate vault and Keychain policy. Use when personal facts, verified answers, resume routing, privacy, or account credentials need maintenance.
---

# Job Profile

This is a repository-scoped Codex workflow for the Jobops CLI.

Maintain the private source of truth without coupling it to public Skill or source code.

## Workflow

1. Initialize with `.venv/bin/python jobctl.py init`. Migrate an existing workflow with `.venv/bin/python jobctl.py migrate <workflow> --legacy-profile <ignored-profile>`.
2. Keep facts, verified answers, policy, queue, documents, browser state, evidence, and logs under `~/Library/Application Support/Jobops` or `JOBOPS_HOME`.
3. Keep ATS credentials, optional mailbox credentials, and the permit HMAC key in macOS Keychain. Never put passwords, tokens, recovery codes, or mailbox credentials in JSON, YAML, CSV, prompts, logs, or command-line arguments. Configure the default-off read-only verifier with `.venv/bin/python jobctl.py mailbox --host <imap-host>`; disable it with `.venv/bin/python jobctl.py mailbox --disable`.
4. Before changing a fact, identify its canonical key, source, sensitivity, geographic scope, confirmation time, and expiry. Ask the user for a new sensitive required answer unless an exact in-scope verified record already exists.
5. Write private JSON atomically with `0600` permissions and preserve a backup. Do not print unrelated values while validating the update.
6. Validate through the program projection, then use `$job-apply` on a fixture or Review-only episode before relying on a new answer mapping.

## Boundaries

- Preferred name and legal name are separate facts.
- A model may map a form label to an existing canonical key; it may not create the value.
- Voluntary self-identification answers are never inferred from other facts.
- Workday account passwords are tenant-scoped Keychain items and may be auto-created only under policy.
- Never stage, commit, attach, or publish the Private Home.
