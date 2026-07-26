from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import jobctl
from auth import CorrelatedMailboxVerifier, InMemoryCredentialStore
from core.private_home import PrivateHome
from core.profile_store import CandidateVault
from core.outcomes import (
    ApplicationOutcome,
    ExitCode,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from jobctl import (
    _event_metrics,
    _materials_required_outcome,
    _project_csv_outcome,
    build_parser,
)
from utils.csv_apply import load_csv_queue
from utils.llm import UnsafeLLMBackendError


def _candidate_vault(tmp_path: Path, *, mailbox_enabled: bool = False) -> CandidateVault:
    home = PrivateHome(tmp_path / "private-home")
    paths = home.ensure()
    home.write_text(
        paths.profile_facts,
        json.dumps(
            {
                "schema_version": 1,
                "normalized": {
                    "personal": {
                        "first_name": "Synthetic",
                        "last_name": "Candidate",
                        "email": "candidate@example.test",
                    }
                },
            }
        ),
    )
    home.write_text(
        paths.verified_answers,
        json.dumps({"schema_version": 1, "answers": {}}),
    )
    home.write_text(
        paths.policy,
        json.dumps(
            {
                "schema_version": 1,
                "autonomy": {
                    "mode": "LOW_RISK_AUTOPILOT",
                    "email_verification_agent_enabled": mailbox_enabled,
                },
                "mailbox": {
                    "enabled": mailbox_enabled,
                    "provider": "imap",
                    "host": "imap.example.test" if mailbox_enabled else "",
                    "port": 993,
                    "mailbox": "INBOX",
                    "keychain_service": jobctl.DEFAULT_MAILBOX_KEYCHAIN_SERVICE,
                },
            }
        ),
    )
    return CandidateVault.load(home)


def _queue(path: Path) -> None:
    fields = [
        "company",
        "job_title",
        "job_url",
        "priority",
        "status",
        "resume_variant",
        "blocker",
        "next_action",
        "notes",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "company": "Synthetic Co",
                "job_title": "Test Engineer",
                "job_url": "https://example.test/jobs/1",
                "priority": "Medium",
                "status": "Pending",
                "resume_variant": "resume.pdf",
            }
        )


def test_cli_defaults_to_review_not_submit() -> None:
    args = build_parser().parse_args(["apply-csv"])
    assert args.submit is False
    assert args.approve_gate_a is False
    assert args.semantic_mapper is False


def test_invalidate_review_appends_correction_and_requeues(
    monkeypatch, tmp_path: Path
) -> None:
    application = SimpleNamespace(
        url="https://example.test/jobs/1",
        company="Synthetic Co",
        title="Test Engineer",
        row={"priority": "Medium"},
    )
    job = jobctl.JobSpec(
        url=application.url,
        company=application.company,
        title=application.title,
        tier=jobctl.priority_to_tier("Medium"),
    )
    run = SimpleNamespace(
        run_id="run-false-review",
        job_id=job.job_id,
        state=OutcomeStatus.REVIEW_READY.value,
        outcome={"adapter": "generic_ai"},
    )
    record = MagicMock()
    engine = SimpleNamespace(
        ledger=SimpleNamespace(get_run=lambda run_id: run),
        record_outcome=record,
    )
    vault = SimpleNamespace(
        paths=SimpleNamespace(
            job_queue=tmp_path / "queue.csv",
            master_documents=tmp_path / "documents",
        )
    )
    monkeypatch.setattr(jobctl.CandidateVault, "load", lambda *args, **kwargs: vault)
    monkeypatch.setattr(jobctl, "MacOSSecurityCredentialStore", lambda: object())
    monkeypatch.setattr(
        jobctl,
        "JobApplicationEngine",
        SimpleNamespace(from_private_home=lambda **kwargs: engine),
    )
    monkeypatch.setattr(jobctl, "load_csv_queue", lambda *args, **kwargs: [application])
    project = MagicMock()
    monkeypatch.setattr(jobctl, "_project_csv_outcome", project)
    args = build_parser().parse_args(
        ["--home", str(tmp_path), "invalidate-review", "--run-id", run.run_id]
    )

    exit_code = jobctl.cmd_invalidate_review(args)

    assert exit_code == int(ExitCode.NEEDS_USER)
    correction = record.call_args.args[0]
    assert correction.status is OutcomeStatus.NEEDS_USER
    assert correction.reason_code is ReasonCode.VALIDATION_FAILED
    assert correction.details["safe_to_retry_fill"] is True
    project.assert_called_once_with(vault.paths.job_queue, application, correction)


def test_unsafe_semantic_backend_fails_before_browser_start(
    monkeypatch, tmp_path: Path
) -> None:
    vault = SimpleNamespace(
        paths=SimpleNamespace(
            job_queue=tmp_path / "queue.csv",
            master_documents=tmp_path / "documents",
        ),
        application_profile=lambda: {
            "ai": {
                "default_backend": "codex_cli",
                "backends": {"codex_cli": {}},
                "components": {"form_analysis": "codex_cli"},
            }
        },
    )
    monkeypatch.setattr(jobctl.CandidateVault, "load", lambda *args, **kwargs: vault)
    monkeypatch.setattr(jobctl, "load_csv_queue", lambda *args, **kwargs: [object()])
    monkeypatch.setattr(jobctl, "MacOSSecurityCredentialStore", lambda: object())
    monkeypatch.setattr(
        jobctl,
        "JobApplicationEngine",
        SimpleNamespace(from_private_home=lambda **kwargs: object()),
    )
    browser_started = False

    def forbidden_browser_start():
        nonlocal browser_started
        browser_started = True
        raise AssertionError("Playwright must not start")

    monkeypatch.setattr(jobctl, "async_playwright", forbidden_browser_start)
    args = build_parser().parse_args(
        ["--home", str(tmp_path), "apply-csv", "--semantic-mapper"]
    )

    with pytest.raises(UnsafeLLMBackendError, match="not approved"):
        asyncio.run(jobctl.cmd_apply_csv(args))

    assert browser_started is False


def test_gate_b_has_a_separate_review_bound_command() -> None:
    args = build_parser().parse_args(
        ["submit-reviewed", "--run-id", "run-synthetic", "--approve"]
    )
    assert args.command == "submit-reviewed"
    assert args.run_id == "run-synthetic"
    assert args.approve is True


def test_mailbox_verifier_requires_both_policy_and_provider_enablement(
    tmp_path: Path,
) -> None:
    store = InMemoryCredentialStore()
    disabled = _candidate_vault(tmp_path / "disabled", mailbox_enabled=False)

    assert jobctl._mailbox_verifier(disabled, store) is None

    enabled = _candidate_vault(tmp_path / "enabled", mailbox_enabled=True)
    verifier = jobctl._mailbox_verifier(enabled, store)

    assert isinstance(verifier, CorrelatedMailboxVerifier)
    assert verifier.provider.config.account == "candidate@example.test"
    assert verifier.provider.config.host == "imap.example.test"
    assert verifier.provider.credential_store is store


def test_mailbox_configuration_keeps_password_out_of_private_files_and_output(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    vault = _candidate_vault(tmp_path, mailbox_enabled=False)
    secret = "synthetic-app-password-never-persist"
    store = InMemoryCredentialStore()
    monkeypatch.setattr(jobctl, "MacOSSecurityCredentialStore", lambda: store)
    monkeypatch.setattr(jobctl.getpass, "getpass", lambda _prompt: secret)
    args = jobctl.build_parser().parse_args(
        [
            "--home",
            str(vault.paths.root),
            "mailbox",
            "--host",
            "imap.example.test",
        ]
    )

    assert jobctl.cmd_mailbox(args) == 0

    policy_text = vault.paths.policy.read_text(encoding="utf-8")
    output = capsys.readouterr().out
    assert secret not in policy_text
    assert secret not in output
    assert "candidate@example.test" not in output
    assert store.get(
        jobctl.DEFAULT_MAILBOX_KEYCHAIN_SERVICE,
        "candidate@example.test",
    ) == secret
    policy = json.loads(policy_text)
    assert policy["autonomy"]["email_verification_agent_enabled"] is True
    assert policy["mailbox"]["enabled"] is True


def test_mailbox_disable_never_prompts_or_deletes_keychain_item(
    monkeypatch, tmp_path: Path
) -> None:
    vault = _candidate_vault(tmp_path, mailbox_enabled=True)
    monkeypatch.setattr(
        jobctl.getpass,
        "getpass",
        lambda _prompt: pytest.fail("disable must not prompt for a password"),
    )
    args = jobctl.build_parser().parse_args(
        ["--home", str(vault.paths.root), "mailbox", "--disable"]
    )

    assert jobctl.cmd_mailbox(args) == 0

    policy = json.loads(vault.paths.policy.read_text(encoding="utf-8"))
    assert policy["autonomy"]["email_verification_agent_enabled"] is False
    assert policy["mailbox"]["enabled"] is False


def test_csv_projection_uses_unified_outcome(tmp_path: Path) -> None:
    csv_path = tmp_path / "queue.csv"
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "resume.pdf").write_bytes(b"synthetic")
    _queue(csv_path)
    application = load_csv_queue(
        csv_path, resume_dir, priorities="Medium", statuses="Pending"
    )[0]
    outcome = ApplicationOutcome(
        run_id="run-1",
        job_id="job-1",
        status=OutcomeStatus.AWAITING_GATE_B,
        phase=OutcomePhase.REVIEW,
        reason_code=ReasonCode.GATE_B_REQUIRED,
        message="Final approval required",
    )

    _project_csv_outcome(csv_path, application, outcome)

    row = next(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert row["status"] == "Needs user"
    assert row["blocker"] == "Final approval required"
    assert "GATE_B_REQUIRED" in row["notes"]


def test_submit_unknown_projection_is_excluded_from_default_queue(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "queue.csv"
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "resume.pdf").write_bytes(b"synthetic")
    _queue(csv_path)
    application = load_csv_queue(
        csv_path, resume_dir, priorities="Medium", statuses="Pending"
    )[0]
    outcome = ApplicationOutcome.needs_user(
        run_id="run-unknown",
        job_id="job-unknown",
        status=OutcomeStatus.SUBMIT_UNKNOWN,
        phase=OutcomePhase.VERIFY,
        reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
        message="A prior submission could not be confirmed",
    )

    _project_csv_outcome(csv_path, application, outcome)

    row = next(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert row["status"] == "Submission unknown"
    assert row["next_action"].startswith("Human: reconcile")
    assert load_csv_queue(
        csv_path,
        resume_dir,
        priorities="Medium",
        statuses=jobctl.DEFAULT_STATUSES,
    ) == []


def test_apply_csv_unknown_preflight_skips_browser_session(
    monkeypatch, tmp_path: Path
) -> None:
    vault = SimpleNamespace(
        paths=SimpleNamespace(
            job_queue=tmp_path / "queue.csv",
            master_documents=tmp_path / "documents",
        ),
        application_profile=lambda: {},
    )
    guard = ApplicationOutcome.needs_user(
        run_id="run-guard",
        job_id="job-guard",
        status=OutcomeStatus.SUBMIT_UNKNOWN,
        phase=OutcomePhase.VERIFY,
        reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
        message="Human reconciliation required",
    )
    record_outcome = MagicMock()
    engine = SimpleNamespace(
        submission_preflight=lambda bundle: guard,
        record_outcome=record_outcome,
        leases=object(),
    )
    bundle = SimpleNamespace(safe_metadata=lambda: {"job_id": "job-guard"})

    class FakePlaywrightContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    browser_session_started = False

    def forbidden_browser_session(*args, **kwargs):
        nonlocal browser_session_started
        browser_session_started = True
        raise AssertionError("submission guard must run before browser launch")

    monkeypatch.setattr(jobctl.CandidateVault, "load", lambda *args, **kwargs: vault)
    monkeypatch.setattr(jobctl, "load_csv_queue", lambda *args, **kwargs: [object()])
    monkeypatch.setattr(jobctl, "MacOSSecurityCredentialStore", lambda: object())
    monkeypatch.setattr(
        jobctl,
        "JobApplicationEngine",
        SimpleNamespace(from_private_home=lambda **kwargs: engine),
    )
    monkeypatch.setattr(
        jobctl,
        "_build_application_bundle",
        lambda **kwargs: (bundle, {}),
    )
    monkeypatch.setattr(jobctl, "async_playwright", FakePlaywrightContext)
    monkeypatch.setattr(jobctl, "lease_browser_session", forbidden_browser_session)
    project = MagicMock()
    monkeypatch.setattr(jobctl, "_project_csv_outcome", project)
    args = build_parser().parse_args(
        ["--home", str(tmp_path), "apply-csv", "--limit", "1"]
    )

    exit_code = asyncio.run(jobctl.cmd_apply_csv(args))

    assert exit_code == int(ExitCode.NEEDS_USER)
    assert browser_session_started is False
    record_outcome.assert_called_once_with(
        guard,
        metadata={"job_id": "job-guard"},
    )
    project.assert_called_once()


def test_missing_custom_materials_stay_assigned_to_codex(tmp_path: Path) -> None:
    csv_path = tmp_path / "queue.csv"
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "resume.pdf").write_bytes(b"synthetic")
    _queue(csv_path)
    application = load_csv_queue(
        csv_path, resume_dir, priorities="Medium", statuses="Pending"
    )[0]

    outcome, _ = _materials_required_outcome(
        application,
        run_id="run-materials",
        message="private manifest missing",
    )
    _project_csv_outcome(csv_path, application, outcome)

    row = next(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert outcome.status is OutcomeStatus.MATERIALS_REQUIRED
    assert row["status"] == "Pending"
    assert row["blocker"] == "private manifest missing"
    assert row["next_action"].startswith("Codex:")


def test_event_metrics_keep_fixture_and_live_claims_separate() -> None:
    class Event:
        event_type = "RUN_STATE_CHANGED"

        def __init__(self, outcome):
            self.payload = {"outcome": outcome}

    events = [
        Event(
            {
                "run_id": "run-1",
                "adapter": "greenhouse",
                "status": "REVIEW_READY",
                "details": {"model_calls": 0},
                "evidence_refs": [],
            }
        ),
        Event(
            {
                "run_id": "run-1",
                "adapter": "greenhouse",
                "status": "SUBMITTED_VERIFIED",
                "details": {"model_calls": 0},
                "evidence_refs": [{"kind": "CONFIRMATION_TEXT"}],
            }
        ),
    ]

    metrics = _event_metrics(events)

    assert metrics["supported_ats_review_arrival"]["rate"] == 1.0
    assert metrics["median_model_calls_observed"] == 0
    assert metrics["submitted_verified_evidence_coverage"] == 1.0
