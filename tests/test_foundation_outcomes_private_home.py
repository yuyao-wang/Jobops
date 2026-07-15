import stat
from pathlib import Path

import pytest

from core.outcomes import (
    ApplicationOutcome,
    EvidenceKind,
    EvidenceRef,
    ExitCode,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.private_home import (
    PRIVATE_HOME_MARKER,
    PrivateHome,
    PrivateHomeError,
    default_private_home,
)


def test_private_home_has_macos_default_and_environment_override(tmp_path: Path) -> None:
    expected = Path("/Users/synthetic/Library/Application Support/Jobops")
    assert (
        default_private_home(
            environ={}, home=Path("/Users/synthetic"), platform="darwin"
        )
        == expected
    )
    override = tmp_path / "custom-jobops"
    assert default_private_home(environ={"JOBOPS_HOME": str(override)}) == override


def test_private_home_creates_named_private_paths_and_secure_files(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    paths = home.ensure()

    assert paths.job_queue == paths.queue / "job_pool.csv"
    assert paths.event_ledger == paths.state / "events.sqlite3"
    assert paths.chromium_profile.is_dir()
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.profile.stat().st_mode) == 0o700
    marker = paths.root / PRIVATE_HOME_MARKER
    assert marker.read_text(encoding="ascii") == "jobops-private-home-v1\n"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600

    written = home.write_text(paths.profile_facts, '{"synthetic":true}')
    assert written.read_text() == '{"synthetic":true}'
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


def test_private_home_rejects_paths_outside_root(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    home.ensure()
    with pytest.raises(PrivateHomeError):
        home.write_text(tmp_path / "not-private.txt", "no")


def test_private_home_rejects_git_worktree_containment(tmp_path: Path) -> None:
    repository = tmp_path / "public-repository"
    (repository / ".git").mkdir(parents=True)

    with pytest.raises(PrivateHomeError, match="outside every Git worktree"):
        PrivateHome(repository / "private-candidate-data").ensure()


def test_private_home_does_not_take_over_arbitrary_existing_directory(
    tmp_path: Path,
) -> None:
    unrelated = tmp_path / "Documents"
    unrelated.mkdir(mode=0o755)
    sentinel = unrelated / "keep.txt"
    sentinel.write_text("synthetic", encoding="utf-8")

    with pytest.raises(PrivateHomeError, match="not an owned Jobops directory"):
        PrivateHome(unrelated).ensure()

    assert stat.S_IMODE(unrelated.stat().st_mode) == 0o755
    assert sentinel.read_text(encoding="utf-8") == "synthetic"
    assert not (unrelated / PRIVATE_HOME_MARKER).exists()


def test_private_home_adopts_only_secure_recognizable_legacy_layout(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "Jobops"
    legacy.mkdir(mode=0o700)
    (legacy / "profile").mkdir(mode=0o700)
    (legacy / "state").mkdir(mode=0o700)
    (legacy / "browser").mkdir(mode=0o700)

    paths = PrivateHome(legacy).ensure()

    assert paths.profile.is_dir()
    assert (legacy / PRIVATE_HOME_MARKER).is_file()


def test_outcome_json_roundtrip_and_stable_exit_codes() -> None:
    evidence = EvidenceRef(
        kind=EvidenceKind.CONFIRMATION_URL,
        sha256="a" * 64,
    )
    outcome = ApplicationOutcome.submitted_verified(
        run_id="run-1",
        job_id="job-1",
        adapter="synthetic",
        evidence_refs=(evidence,),
        details={"ats_application_id": "synthetic-123"},
    )

    restored = ApplicationOutcome.from_json(outcome.to_json())
    assert restored.to_dict() == outcome.to_dict()
    assert restored.exit_code is ExitCode.SUCCESS
    assert restored.reason_code is ReasonCode.SUBMISSION_CONFIRMED

    blocked = ApplicationOutcome.needs_user(
        run_id="run-2",
        job_id="job-2",
        status=OutcomeStatus.NEEDS_USER_CAPTCHA,
        phase=OutcomePhase.AUTHENTICATE,
        reason_code=ReasonCode.CAPTCHA,
        message="Synthetic CAPTCHA fixture",
    )
    assert blocked.exit_code is ExitCode.NEEDS_USER


def test_verified_submission_cannot_exist_without_evidence() -> None:
    with pytest.raises(ValueError, match="requires explicit submission evidence"):
        ApplicationOutcome(
            run_id="run-1",
            job_id="job-1",
            status=OutcomeStatus.SUBMITTED_VERIFIED,
            phase=OutcomePhase.COMPLETE,
            reason_code=ReasonCode.SUBMISSION_CONFIRMED,
        )

    with pytest.raises(ValueError, match="requires explicit submission evidence"):
        ApplicationOutcome(
            run_id="run-1",
            job_id="job-1",
            status=OutcomeStatus.SUBMITTED_VERIFIED,
            phase=OutcomePhase.COMPLETE,
            evidence_refs=(
                EvidenceRef(
                    kind=EvidenceKind.FORM_SNAPSHOT,
                    sha256="c" * 64,
                ),
            ),
        )


@pytest.mark.parametrize(
    "uri",
    [
        "https://user:secret@example.invalid/application/confirmed",
        "https://example.invalid/application/confirmed?token=secret",
        "https://example.invalid/application/confirmed#receipt",
        "javascript:alert(1)",
        "/application/confirmed",
    ],
)
def test_confirmation_url_evidence_rejects_sensitive_url_components(uri: str) -> None:
    with pytest.raises(ValueError, match="confirmation URL evidence"):
        EvidenceRef(kind=EvidenceKind.CONFIRMATION_URL, uri=uri)


def test_confirmation_url_evidence_can_be_digest_only() -> None:
    evidence = EvidenceRef(
        kind=EvidenceKind.CONFIRMATION_URL,
        sha256="d" * 64,
    )

    assert evidence.uri is None
    assert evidence.sha256 == "d" * 64


def test_submit_unknown_is_a_hard_stop_with_needs_user_exit_category() -> None:
    outcome = ApplicationOutcome.needs_user(
        run_id="run-unknown",
        job_id="job-unknown",
        status=OutcomeStatus.SUBMIT_UNKNOWN,
        phase=OutcomePhase.VERIFY,
        reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
        message="Human reconciliation required",
    )

    assert outcome.exit_code is ExitCode.NEEDS_USER


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (OutcomeStatus.MATERIALS_REQUIRED, ExitCode.POLICY_BLOCKED),
        (OutcomeStatus.AWAITING_GATE_A, ExitCode.AWAITING_GATE_A),
        (OutcomeStatus.AWAITING_GATE_B, ExitCode.AWAITING_GATE_B),
        (OutcomeStatus.FAILED_RETRYABLE, ExitCode.RETRYABLE_FAILURE),
        (OutcomeStatus.FAILED_UNSUPPORTED, ExitCode.TERMINAL_FAILURE),
        (OutcomeStatus.SKIPPED_POLICY, ExitCode.POLICY_BLOCKED),
        (OutcomeStatus.INTERNAL_ERROR, ExitCode.INTERNAL_ERROR),
    ],
)
def test_exit_code_categories_are_stable(status, expected) -> None:
    outcome = ApplicationOutcome(
        run_id="run-exit",
        job_id="job-exit",
        status=status,
        phase=OutcomePhase.REVIEW,
    )
    assert outcome.exit_code is expected
