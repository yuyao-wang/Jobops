---
name: job-materials
description: Prepare truthful, tier-aware resumes and cover letters from the private candidate vault and a job description. Use for resume routing, important-job tailoring, narrative cover letters, or material quality checks before application.
---

# Job Materials

This is a repository-scoped Codex workflow for the Jobops CLI.

Create application materials in Private Home; keep source facts and generated files outside Git.

## Workflow

1. Confirm the job tier with `.venv/bin/python jobctl.py policy` and the queue row with `.venv/bin/python jobctl.py queue --list`.
2. Read only the minimum private facts and source resume needed for this job. Treat every claim as immutable unless it has explicit provenance in the vault.
3. Apply the tier treatment:
   - High: parse the JD, select only verified experience, reorder and rephrase for relevance, render and visually inspect a dedicated resume, and write a concise narrative cover letter. Connect one real experience to the role and the company's stated mission or product; avoid a mechanical template.
   - Medium: route the closest verified resume and make limited keyword/order edits when they improve evidence alignment. Under the current default policy, write a concise targeted letter.
   - Low: use an approved existing variant. Generate a letter only if the ATS requires it.
4. Save generated sources and rendered files below `~/Library/Application Support/Jobops/documents/generated/<job_id>/`; never write materials into the repository.
5. Verify that dates, employers, degrees, metrics, technologies, location, authorization, and contact details match the private vault. Remove any unsupported assertion.
6. Render the final resume and visually inspect every page for clipping, overflow, missing glyphs, broken links, and accidental blank pages.
7. Write `manifest.json` in the job directory using schema version 1. Bind it to `job_id`, `job_url_hash`, tier, artifact content hashes, and the attestations below. Use relative artifact paths. Never copy candidate content into the manifest.
8. Hand the prepared job back to `$job-apply`. Do not submit from this skill.

## Private Manifest Contract

High and Medium jobs fail closed before browser launch unless the manifest validates. Include:

```json
{
  "schema_version": 1,
  "job_id": "job-...",
  "job_url_hash": "<sha256>",
  "tier": "HIGH",
  "resume_path": "resume.pdf",
  "resume_sha256": "<sha256>",
  "cover_letter_path": "cover-letter.md",
  "cover_letter_sha256": "<sha256>",
  "facts_verified": true,
  "job_specific": true,
  "bespoke": true,
  "targeted": false,
  "cover_letter_job_specific": true,
  "narrative_alignment": true,
  "resume_visual_qa": {
    "passed": true,
    "artifact_sha256": "<same resume sha256>",
    "checked_at": "<ISO-8601 timestamp>"
  }
}
```

For Medium, set `tier` to `MEDIUM`, `targeted` to true, and `bespoke` to false. High requires `narrative_alignment`; both High and Medium require a job-specific text cover letter under the current default policy.

## Quality Gate

- High-priority resumes must be job-specific and visually valid.
- A narrative letter must explain fit through a concrete, true story and company alignment, not generic enthusiasm.
- ATS keywords may change phrasing and ordering, never underlying facts.
- If the JD or company mission cannot be obtained reliably, state the limitation and use a factual targeted letter.
