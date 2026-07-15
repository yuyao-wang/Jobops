#!/usr/bin/env python3
"""Jobops control plane for private queues and permit-gated ATS execution."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from statistics import median
from typing import Any, Mapping
from uuid import uuid4

from playwright.async_api import async_playwright

from auth import (
    CorrelatedMailboxVerifier,
    IMAPMailboxProvider,
    IMAPProviderConfig,
)
from auth.credentials import CredentialStore, MacOSSecurityCredentialStore
from core.application_engine import JobApplicationEngine
from core.browser_broker import lease_browser_session
from core.bundles import (
    ApplicationBundle,
    JobSpec,
    file_sha256,
    priority_to_tier,
)
from core.materials import MaterialValidationError, build_tier_materials
from core.event_ledger import hash_job_url
from core.outcomes import (
    ApplicationOutcome,
    ExitCode,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.policy import ApprovalActor, PolicyEngine, RiskSignals
from core.private_home import PrivateHome
from core.profile_store import CandidateVault
from core.secrets import load_or_create_permit_secret
from scripts.migrate_private_home import migrate
from utils.csv_apply import CSVApplication, load_csv_queue, update_csv_application


DEFAULT_STATUSES = "Needs user,Pending,Ready to apply"
REVIEWED_STATUSES = "Ready for review,Needs user"
SUPPORTED_ATS = frozenset({"greenhouse", "lever", "ashby", "jobvite", "workday"})
DEFAULT_MAILBOX_KEYCHAIN_SERVICE = "com.jobops.mailbox.imap"


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _mailbox_verifier(
    vault: CandidateVault,
    store: CredentialStore,
) -> CorrelatedMailboxVerifier | None:
    """Build the explicitly enabled, Keychain-backed mailbox verifier.

    The account is taken from the canonical private candidate facts.  The
    private policy contains only non-secret connection metadata; an incomplete
    or disabled configuration leaves email verification as a human handoff.
    """

    policy_config = getattr(vault, "policy_config", None)
    if not bool(
        getattr(policy_config, "email_verification_agent_enabled", False)
    ):
        return None
    policy = getattr(vault, "policy", {})
    raw = policy.get("mailbox") if isinstance(policy, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    if str(raw.get("provider") or "").casefold() != "imap":
        return None
    personal = vault.application_profile().get("personal", {})
    account = str(personal.get("email") or "").strip()
    config = IMAPProviderConfig(
        enabled=True,
        host=str(raw.get("host") or "").strip(),
        account=account,
        keychain_service=str(
            raw.get("keychain_service") or DEFAULT_MAILBOX_KEYCHAIN_SERVICE
        ).strip(),
        port=int(raw.get("port", 993)),
        mailbox=str(raw.get("mailbox") or "INBOX").strip(),
    )
    config.validate()
    return CorrelatedMailboxVerifier(IMAPMailboxProvider(config, store))


def _write_mailbox_policy(
    vault: CandidateVault,
    *,
    enabled: bool,
    host: str = "",
    port: int = 993,
    mailbox: str = "INBOX",
    keychain_service: str = DEFAULT_MAILBOX_KEYCHAIN_SERVICE,
) -> None:
    """Atomically update only the private mailbox/autonomy policy fields."""

    policy = deepcopy(dict(vault.policy))
    autonomy = policy.get("autonomy")
    if not isinstance(autonomy, dict):
        autonomy = {}
        policy["autonomy"] = autonomy
    autonomy["email_verification_agent_enabled"] = enabled
    policy["mailbox"] = {
        "enabled": enabled,
        "provider": "imap",
        "host": host if enabled else "",
        "port": port,
        "mailbox": mailbox,
        "keychain_service": keychain_service,
    }
    PrivateHome(vault.paths.root).write_text(
        vault.paths.policy,
        json.dumps(policy, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _event_metrics(events: list[Any]) -> dict[str, Any]:
    outcome_events: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "RUN_STATE_CHANGED":
            continue
        outcome = event.payload.get("outcome")
        if isinstance(outcome, dict):
            outcome_events.append(outcome)

    latest: dict[str, dict[str, Any]] = {}
    review_runs: set[str] = set()
    supported_runs: set[str] = set()
    model_calls: list[int] = []
    for outcome in outcome_events:
        run_id = str(outcome.get("run_id") or "")
        if run_id:
            latest[run_id] = outcome
        adapter = str(outcome.get("adapter") or "")
        if adapter in SUPPORTED_ATS and run_id:
            supported_runs.add(run_id)
            if outcome.get("status") in {"REVIEW_READY", "SUBMITTED_VERIFIED"}:
                review_runs.add(run_id)
        details = outcome.get("details")
        if isinstance(details, dict) and isinstance(details.get("model_calls"), int):
            model_calls.append(details["model_calls"])

    verified = [
        outcome
        for outcome in latest.values()
        if outcome.get("status") == "SUBMITTED_VERIFIED"
    ]
    verified_with_evidence = sum(
        bool(outcome.get("evidence_refs")) for outcome in verified
    )
    return {
        "latest_outcomes": dict(
            Counter(str(outcome.get("status") or "UNKNOWN") for outcome in latest.values())
        ),
        "supported_ats_review_arrival": {
            "reached": len(review_runs),
            "runs": len(supported_runs),
            "rate": (len(review_runs) / len(supported_runs)) if supported_runs else None,
        },
        "median_model_calls_observed": median(model_calls) if model_calls else None,
        "submitted_verified_evidence_coverage": (
            verified_with_evidence / len(verified) if verified else None
        ),
        "duplicate_submission_blocks": sum(
            outcome.get("reason_code") == "DUPLICATE_SUBMISSION"
            for outcome in outcome_events
        ),
    }


def _resolve_resume(application: CSVApplication, vault: CandidateVault) -> Path:
    if application.resume_path.is_file():
        candidate = application.resume_path.resolve()
        try:
            candidate.relative_to(vault.paths.root)
        except ValueError:
            # A migrated CSV may still contain a source-machine absolute path.
            # Never leave Private Home; resolve it through the copied variants.
            pass
        else:
            return candidate
    requested = application.row.get("resume_variant", "").strip()
    normalized_requested = "".join(char for char in Path(requested).stem.casefold() if char.isalnum())
    if requested:
        for entry in vault.facts.get("normalized", {}).get("resume_variants", []):
            candidate = Path(str(entry.get("file_path") or ""))
            normalized_candidate = "".join(
                char for char in candidate.stem.casefold() if char.isalnum()
            )
            role = "".join(
                char for char in str(entry.get("role_family") or "").casefold()
                if char.isalnum()
            )
            if candidate.is_file() and normalized_requested in {
                normalized_candidate,
                role,
            }:
                return candidate
        raise FileNotFoundError(f"routed resume variant is missing: {Path(requested).name}")
    default_resume = vault.facts.get("normalized", {}).get("default_resume", "")
    candidate = Path(str(default_resume)).expanduser()
    if not candidate.is_file():
        raise FileNotFoundError("private vault has no usable default resume")
    return candidate


def _resume_is_attested(resume: Path, vault: CandidateVault) -> bool:
    resolved = resume.expanduser().resolve()
    for entry in vault.facts.get("normalized", {}).get("resume_variants", []):
        if not isinstance(entry, dict):
            continue
        candidate = Path(str(entry.get("file_path") or "")).expanduser().resolve()
        if candidate != resolved or not candidate.is_file():
            continue
        expected = str(entry.get("artifact_id") or "")
        return len(expected) == 64 and expected == file_sha256(candidate)
    return False


def _build_application_bundle(
    *,
    application: CSVApplication,
    vault: CandidateVault,
    home: PrivateHome,
    run_id: str,
) -> tuple[ApplicationBundle, dict[str, Any]]:
    tier = priority_to_tier(application.row.get("priority", ""))
    job = JobSpec(
        url=application.url,
        company=application.company,
        title=application.title,
        tier=tier,
    )
    fallback_resume = _resolve_resume(application, vault)
    answer_report = vault.answer_trust_report(job_id=job.job_id)
    resume_verified = tier.value in {"HIGH", "MEDIUM"} or _resume_is_attested(
        fallback_resume, vault
    )
    decision = PolicyEngine(vault.policy_config).decide(
        tier,
        RiskSignals(
            resume_verified=resume_verified,
            answers_verified=answer_report.all_projected_answers_verified,
        ),
    )
    materials = build_tier_materials(
        home=home,
        job=job,
        policy=decision,
        fallback_resume=fallback_resume,
    )
    profile = vault.application_profile(
        resume_path=materials.resume_path,
        job_id=job.job_id,
    )
    bundle = ApplicationBundle(
        run_id=run_id,
        job=job,
        materials=materials,
        profile=profile,
        answers=dict(answer_report.values),
        policy=decision,
    )
    return bundle, profile


def _project_csv_outcome(
    csv_path: Path,
    application: CSVApplication,
    outcome: ApplicationOutcome,
) -> None:
    fields = set(application.row)
    if outcome.status is OutcomeStatus.SUBMITTED_VERIFIED:
        status = "Submitted"
        next_action = "Monitor the application ledger and mailbox for follow-up."
    elif outcome.status is OutcomeStatus.SUBMIT_UNKNOWN:
        status = "Submission unknown"
        next_action = (
            "Human: reconcile the prior submission before any retry; do not "
            "requeue this row as Needs user."
        )
    elif outcome.status is OutcomeStatus.REVIEW_READY:
        status = "Ready for review"
        next_action = "Review the filled application and authorize Gate B if submitting."
    elif outcome.status is OutcomeStatus.MATERIALS_REQUIRED:
        status = "Pending"
        next_action = "Codex: run job-materials and create the private job manifest."
    elif outcome.status in {
        OutcomeStatus.AWAITING_GATE_A,
        OutcomeStatus.AWAITING_GATE_B,
    } or outcome.exit_code is ExitCode.NEEDS_USER:
        status = "Needs user"
        next_action = outcome.message
    elif outcome.retryable:
        status = "Needs user"
        next_action = "Retry from the recorded checkpoint after inspecting the blocker."
    else:
        status = "Skipped"
        next_action = outcome.message

    updates = {
        key: value
        for key, value in {
            "status": status,
            "blocker": "" if outcome.status in {
                OutcomeStatus.REVIEW_READY,
                OutcomeStatus.SUBMITTED_VERIFIED,
            } else outcome.message,
            "next_action": next_action,
        }.items()
        if key in fields
    }
    note = (
        f"Jobops {outcome.run_id}: {outcome.status.value} / "
        f"{outcome.reason_code.value if hasattr(outcome.reason_code, 'value') else outcome.reason_code}"
    )
    update_csv_application(
        csv_path,
        application,
        updates,
        note=note if "notes" in fields else "",
    )


def _materials_required_outcome(
    application: CSVApplication,
    *,
    run_id: str,
    message: str,
) -> tuple[ApplicationOutcome, JobSpec]:
    tier = priority_to_tier(application.row.get("priority", ""))
    job = JobSpec(
        url=application.url,
        company=application.company,
        title=application.title,
        tier=tier,
    )
    return (
        ApplicationOutcome(
            run_id=run_id,
            job_id=job.job_id,
            status=OutcomeStatus.MATERIALS_REQUIRED,
            phase=OutcomePhase.MATERIALS,
            reason_code=ReasonCode.MISSING_MATERIAL,
            message=message,
            details={"actor": "CODEX", "tier": tier.value},
        ),
        job,
    )


def cmd_init(args: argparse.Namespace) -> int:
    home = PrivateHome(Path(args.home).expanduser().resolve()) if args.home else PrivateHome.discover()
    paths = home.ensure()
    store = MacOSSecurityCredentialStore()
    load_or_create_permit_secret(store)
    _json_print(
        {
            "private_home": str(paths.root),
            "permissions": {"directories": "0700", "files": "0600"},
            "permit_key": "stored_in_macos_keychain",
            "profile_initialized": paths.profile_facts.is_file(),
            "queue_initialized": paths.job_queue.is_file(),
        }
    )
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    home = PrivateHome(Path(args.home).expanduser().resolve()) if args.home else PrivateHome.discover()
    result = migrate(
        workflow_dir=Path(args.workflow),
        private_home=home,
        legacy_profile_path=Path(args.legacy_profile) if args.legacy_profile else None,
    )
    _json_print(result)
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    vault = CandidateVault.load(
        PrivateHome(Path(args.home).expanduser().resolve()) if args.home else None
    )
    csv_path = Path(args.csv).expanduser().resolve() if args.csv else vault.paths.job_queue
    resume_dir = (
        Path(args.resume_dir).expanduser().resolve()
        if args.resume_dir
        else vault.paths.master_documents
    )
    queue = load_csv_queue(
        csv_path,
        resume_dir,
        priorities=args.priorities,
        statuses=args.statuses,
        limit=args.limit,
    )
    summary = {
        "queue": str(csv_path),
        "selected": len(queue),
        "by_priority": dict(Counter(item.row.get("priority", "") for item in queue)),
        "by_status": dict(Counter(item.row.get("status", "") for item in queue)),
        "missing_resumes": sum(not item.resume_path.is_file() for item in queue),
    }
    if args.list:
        jobs = []
        for item in queue:
            tier = priority_to_tier(item.row.get("priority", ""))
            job = JobSpec(
                url=item.url,
                company=item.company,
                title=item.title,
                tier=tier,
            )
            jobs.append(
                {
                    "row": item.row_index + 2,
                    "priority": item.row.get("priority", ""),
                    "status": item.row.get("status", ""),
                    "company": item.company,
                    "title": item.title,
                    "ats": item.row.get("source", ""),
                    "job_id": job.job_id,
                    "job_url_hash": hash_job_url(job.url),
                    "material_manifest": str(
                        vault.paths.generated_documents / job.job_id / "manifest.json"
                    )
                    if tier.value in {"HIGH", "MEDIUM"}
                    else "not_required",
                }
            )
        summary["jobs"] = jobs
    _json_print(summary)
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    vault = CandidateVault.load(
        PrivateHome(Path(args.home).expanduser().resolve()) if args.home else None
    )
    engine = PolicyEngine(vault.policy_config)
    result = {"mode": vault.policy_config.mode.value, "tiers": {}}
    for priority in ("High", "Medium", "Low"):
        tier = priority_to_tier(priority)
        decision = engine.decide(tier, RiskSignals())
        result["tiers"][tier.value] = {
            "materials": decision.material_strategy.value,
            "cover_letter": decision.cover_letter_strategy.value,
            "gate_a": decision.gate_a_actor.value,
            "gate_b": decision.gate_b_actor.value,
            "submit_authority": decision.submit_authority.value,
        }
    _json_print(result)
    return 0


def cmd_mailbox(args: argparse.Namespace) -> int:
    """Configure or disable the optional read-only IMAP verifier privately."""

    home = (
        PrivateHome(Path(args.home).expanduser().resolve())
        if args.home
        else PrivateHome.discover()
    )
    vault = CandidateVault.load(home)
    if args.disable:
        _write_mailbox_policy(vault, enabled=False)
        _json_print(
            {
                "mailbox_verifier": "disabled",
                "credential": "left_in_keychain",
            }
        )
        return 0

    profile = vault.application_profile()
    account = str(profile.get("personal", {}).get("email") or "").strip()
    config = IMAPProviderConfig(
        enabled=True,
        host=args.host.strip(),
        account=account,
        keychain_service=args.keychain_service.strip(),
        port=args.port,
        mailbox=args.mailbox.strip(),
    )
    config.validate()
    password = getpass.getpass("IMAP/app password (stored only in macOS Keychain): ")
    if not password:
        raise ValueError("mailbox password cannot be empty")
    store = MacOSSecurityCredentialStore()
    try:
        store.set(config.keychain_service, account, password)
    finally:
        del password
    _write_mailbox_policy(
        vault,
        enabled=True,
        host=config.host,
        port=config.port,
        mailbox=config.mailbox,
        keychain_service=config.keychain_service,
    )
    _json_print(
        {
            "mailbox_verifier": "enabled",
            "provider": "imap_read_only",
            "credential": "stored_in_macos_keychain",
        }
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    home = PrivateHome(Path(args.home).expanduser().resolve()) if args.home else PrivateHome.discover()
    paths = home.ensure()
    from core.event_ledger import EventLedger

    ledger = EventLedger(paths.event_ledger)
    if args.run_id:
        run = ledger.get_run(args.run_id)
        _json_print(
            {
                "run_id": run.run_id,
                "job_id": run.job_id,
                "state": run.state,
                "version": run.state_version,
                "outcome": run.outcome,
                "events": [
                    {
                        "sequence": item.sequence,
                        "type": item.event_type,
                        "created_at": item.created_at,
                    }
                    for item in ledger.list_events(run_id=args.run_id)
                ],
            }
        )
        return 0
    events = ledger.list_events()
    metrics = _event_metrics(events)
    _json_print(
        {
            "event_count": len(events),
            "run_count": len({event.run_id for event in events}),
            "job_count": len({event.job_id for event in events}),
            "event_types": dict(Counter(event.event_type for event in events)),
            "metrics": metrics,
        }
    )
    return 0


async def cmd_apply_csv(args: argparse.Namespace) -> int:
    home = PrivateHome(Path(args.home).expanduser().resolve()) if args.home else PrivateHome.discover()
    vault = CandidateVault.load(home)
    csv_path = Path(args.csv).expanduser().resolve() if args.csv else vault.paths.job_queue
    resume_dir = (
        Path(args.resume_dir).expanduser().resolve()
        if args.resume_dir
        else vault.paths.master_documents
    )
    queue = load_csv_queue(
        csv_path,
        resume_dir,
        priorities=args.priorities,
        statuses=args.statuses,
        limit=args.limit,
    )
    if args.preview:
        args.list = True
        return cmd_queue(args)
    if not queue:
        _json_print({"queue": str(csv_path), "selected": 0})
        return 0

    store = MacOSSecurityCredentialStore()
    engine = JobApplicationEngine.from_private_home(
        home=home,
        credential_store=store,
    )
    mailbox_verifier = _mailbox_verifier(vault, store)
    profile_base = vault.application_profile()
    brain = None
    if args.semantic_mapper:
        from utils.llm import require_safe_backend_for_untrusted_input

        # Resolve credentials and reject tool-enabled/unknown backends before
        # Playwright starts.  Browser-derived labels are adversarial input.
        require_safe_backend_for_untrusted_input("form_analysis", profile_base)
        from utils.brain import ClaudeBrain

        brain = ClaudeBrain(verbose=False, profile=profile_base)

    attempted = 0
    outcomes: Counter[str] = Counter()
    final_exit = ExitCode.SUCCESS
    async with async_playwright() as playwright:
        for application in queue:
            run_id = f"run-{uuid4().hex}"
            try:
                bundle, profile = _build_application_bundle(
                    application=application,
                    vault=vault,
                    home=home,
                    run_id=run_id,
                )
            except (FileNotFoundError, MaterialValidationError, OSError) as exc:
                outcome, job = _materials_required_outcome(
                    application,
                    run_id=run_id,
                    message=str(exc),
                )
                engine.record_outcome(
                    outcome,
                    metadata={
                        "job_id": job.job_id,
                        "company": job.company,
                        "title": job.title,
                        "tier": job.tier.value,
                    },
                )
                attempted += 1
                outcomes[outcome.status.value] += 1
                final_exit = max(final_exit, outcome.exit_code)
                _project_csv_outcome(csv_path, application, outcome)
                print(outcome.to_json())
                if not args.continue_on_user:
                    return int(outcome.exit_code)
                continue

            submission_guard = engine.submission_preflight(bundle)
            if submission_guard is not None:
                engine.record_outcome(
                    submission_guard,
                    metadata=bundle.safe_metadata(),
                )
                outcome = submission_guard
            elif bundle.policy.blockers or (
                bundle.policy.gate_a_actor is ApprovalActor.HUMAN
                and not args.approve_gate_a
            ):
                outcome = await engine.execute(
                    page=None,
                    bundle=bundle,
                    request_submit=args.submit,
                )
            else:
                async with lease_browser_session(
                    playwright,
                    profile=profile,
                    leases=engine.leases,
                    owner=run_id,
                    headless=args.headless,
                    ttl_seconds=args.lease_ttl,
                ) as browser:
                    outcome = await engine.execute(
                        page=browser.session.page,
                        bundle=bundle,
                        request_submit=args.submit,
                        approve_gate_a=args.approve_gate_a,
                        credential_store=store,
                        mailbox_verifier=mailbox_verifier,
                        brain=brain,
                        platform_hint=application.row.get("source", ""),
                        lease_ttl_seconds=args.lease_ttl,
                        browser_lease=browser.lease,
                    )

            attempted += 1
            outcomes[outcome.status.value] += 1
            final_exit = max(final_exit, outcome.exit_code)
            _project_csv_outcome(csv_path, application, outcome)
            print(outcome.to_json())
            if outcome.exit_code in {
                ExitCode.NEEDS_USER,
                ExitCode.AWAITING_GATE_A,
                ExitCode.AWAITING_GATE_B,
            } and not args.continue_on_user:
                break

    _json_print(
        {
            "attempted": attempted,
            "outcomes": dict(outcomes),
            "queue": str(csv_path),
        }
    )
    return int(final_exit)


async def cmd_submit_reviewed(args: argparse.Namespace) -> int:
    """Re-read and submit one run whose Review was persisted earlier."""

    if not args.approve:
        raise ValueError(
            "submit-reviewed requires --approve after the recorded Review was inspected"
        )
    home = PrivateHome(Path(args.home).expanduser().resolve()) if args.home else PrivateHome.discover()
    vault = CandidateVault.load(home)
    csv_path = Path(args.csv).expanduser().resolve() if args.csv else vault.paths.job_queue
    resume_dir = (
        Path(args.resume_dir).expanduser().resolve()
        if args.resume_dir
        else vault.paths.master_documents
    )
    store = MacOSSecurityCredentialStore()
    engine = JobApplicationEngine.from_private_home(
        home=home,
        credential_store=store,
    )
    mailbox_verifier = _mailbox_verifier(vault, store)
    run = engine.ledger.get_run(args.run_id)
    approved_review_hash = engine.latest_review_hash(args.run_id)
    if not approved_review_hash:
        raise ValueError("run has no persisted REVIEW_READY fingerprint")

    candidates = load_csv_queue(
        csv_path,
        resume_dir,
        priorities="High,Medium,Low",
        statuses=REVIEWED_STATUSES,
        limit=0,
    )
    matches: list[CSVApplication] = []
    for candidate in candidates:
        try:
            candidate_job = JobSpec(
                url=candidate.url,
                company=candidate.company,
                title=candidate.title,
                tier=priority_to_tier(candidate.row.get("priority", "")),
            )
        except ValueError:
            continue
        if candidate_job.job_id == run.job_id:
            matches.append(candidate)
    if len(matches) != 1:
        raise ValueError(
            "reviewed run must match exactly one Ready-for-review CSV row"
        )
    application = matches[0]
    try:
        bundle, profile = _build_application_bundle(
            application=application,
            vault=vault,
            home=home,
            run_id=args.run_id,
        )
    except (FileNotFoundError, MaterialValidationError, OSError) as exc:
        outcome, _ = _materials_required_outcome(
            application,
            run_id=args.run_id,
            message=str(exc),
        )
        engine.record_outcome(outcome)
        _project_csv_outcome(csv_path, application, outcome)
        print(outcome.to_json())
        return int(outcome.exit_code)
    if bundle.job.job_id != run.job_id:
        raise ValueError("reviewed run no longer matches the selected job")

    submission_guard = engine.submission_preflight(bundle)
    if submission_guard is not None:
        engine.record_outcome(submission_guard)
        _project_csv_outcome(csv_path, application, submission_guard)
        print(submission_guard.to_json())
        return int(submission_guard.exit_code)

    if bundle.policy.blockers:
        outcome = await engine.execute(
            page=None,
            bundle=bundle,
            request_submit=True,
            approve_gate_a=True,
            approved_review_hash=approved_review_hash,
        )
        _project_csv_outcome(csv_path, application, outcome)
        print(outcome.to_json())
        return int(outcome.exit_code)

    brain = None
    if args.semantic_mapper:
        from utils.llm import require_safe_backend_for_untrusted_input

        require_safe_backend_for_untrusted_input("form_analysis", profile)
        from utils.brain import ClaudeBrain

        brain = ClaudeBrain(verbose=False, profile=profile)

    async with async_playwright() as playwright:
        async with lease_browser_session(
            playwright,
            profile=profile,
            leases=engine.leases,
            owner=args.run_id,
            headless=args.headless,
            ttl_seconds=args.lease_ttl,
        ) as browser:
            outcome = await engine.execute(
                page=browser.session.page,
                bundle=bundle,
                request_submit=True,
                approve_gate_a=True,
                approved_review_hash=approved_review_hash,
                credential_store=store,
                mailbox_verifier=mailbox_verifier,
                brain=brain,
                platform_hint=application.row.get("source", ""),
                lease_ttl_seconds=args.lease_ttl,
                browser_lease=browser.lease,
            )
    _project_csv_outcome(csv_path, application, outcome)
    print(outcome.to_json())
    return int(outcome.exit_code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default="", help="Override JOBOPS_HOME")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create Private Home and the Keychain permit key")

    migrate_parser = subparsers.add_parser("migrate", help="Import an ApplyPilot workflow privately")
    migrate_parser.add_argument("workflow")
    migrate_parser.add_argument("--legacy-profile", default="")

    mailbox_parser = subparsers.add_parser(
        "mailbox",
        help="Configure or disable the optional read-only IMAP verifier",
    )
    mailbox_parser.add_argument("--host", default="")
    mailbox_parser.add_argument("--port", type=int, default=993)
    mailbox_parser.add_argument("--mailbox", default="INBOX")
    mailbox_parser.add_argument(
        "--keychain-service", default=DEFAULT_MAILBOX_KEYCHAIN_SERVICE
    )
    mailbox_parser.add_argument("--disable", action="store_true")

    for name, help_text in (
        ("queue", "Inspect the private CSV queue"),
        ("apply-csv", "Run deterministic adapters from the private CSV queue"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--csv", default="")
        command.add_argument("--resume-dir", default="")
        command.add_argument("--priorities", default="High,Medium,Low")
        command.add_argument("--statuses", default=DEFAULT_STATUSES)
        command.add_argument("--limit", type=int, default=0)
        if name == "queue":
            command.add_argument("--list", action="store_true")
        else:
            command.add_argument("--preview", action="store_true")
            command.add_argument("--submit", action="store_true")
            command.add_argument("--approve-gate-a", action="store_true")
            command.add_argument("--continue-on-user", action="store_true")
            command.add_argument("--semantic-mapper", action="store_true")
            command.add_argument("--headless", action="store_true")
            command.add_argument("--lease-ttl", type=float, default=1800.0)

    submit_parser = subparsers.add_parser(
        "submit-reviewed",
        help="Submit one persisted Review in a separate Gate B invocation",
    )
    submit_parser.add_argument("--run-id", required=True)
    submit_parser.add_argument("--approve", action="store_true")
    submit_parser.add_argument("--csv", default="")
    submit_parser.add_argument("--resume-dir", default="")
    submit_parser.add_argument("--semantic-mapper", action="store_true")
    submit_parser.add_argument("--headless", action="store_true")
    submit_parser.add_argument("--lease-ttl", type=float, default=1800.0)

    subparsers.add_parser("policy", help="Show tier-specific material and permit policy")
    status_parser = subparsers.add_parser("status", help="Summarize the event ledger")
    status_parser.add_argument("--run-id", default="")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "limit", 0) < 0:
        parser.error("--limit must be zero or greater")
    if getattr(args, "lease_ttl", 1) <= 0:
        parser.error("--lease-ttl must be positive")
    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "migrate":
            return cmd_migrate(args)
        if args.command == "mailbox":
            return cmd_mailbox(args)
        if args.command == "queue":
            return cmd_queue(args)
        if args.command == "policy":
            return cmd_policy(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "apply-csv":
            return asyncio.run(cmd_apply_csv(args))
        if args.command == "submit-reviewed":
            return asyncio.run(cmd_submit_reviewed(args))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        parser.exit(int(ExitCode.INVALID_INPUT), f"jobctl: {exc}\n")
    return int(ExitCode.INTERNAL_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
