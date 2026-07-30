"""Focused S4a0a subject Job Library membership tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.job_discovery import (
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobIntakeIntent,
    JobIntakeProposal,
    PrivateHomeJobPostingRepository,
    ProposalResolution,
    ResolvedJobCandidate,
    build_production_job_discovery,
)
from core.private_home import PrivateHome
from core.subject_job_discovery import (
    SubjectJobDiscoveryCommand,
    SubjectJobDiscoveryStatus,
    build_subject_job_discovery,
)
from core.subject_job_library import (
    PrivateHomeSubjectJobLibraryMembershipRepository,
    RegisterSubjectJobMembershipCommand,
    RegisterSubjectJobMembershipStatus,
    SubjectJobMembershipReadStatus,
    SubjectJobMembershipSourceKind,
    SubjectJobPostingReadStatus,
    SubjectScopedJobPostingReader,
    register_subject_job_membership,
    subject_job_identity_hash,
)


NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)


class _InterruptMembershipWrite:
    def __init__(self, home: PrivateHome) -> None:
        self.root = home.root
        self._home = home
        self._interrupted = False

    def ensure(self):
        return self._home.ensure()

    def write_bytes_if_absent(self, path, content):
        candidate = Path(path)
        if candidate.parent.name == "memberships" and not self._interrupted:
            self._interrupted = True
            raise OSError("synthetic interruption after receipt")
        return self._home.write_bytes_if_absent(candidate, content)


def _request(*, suffix: str = "shared") -> JobDiscoveryRequest:
    return JobDiscoveryRequest(
        request_id=f"typed-discovery-{suffix}",
        trigger=DiscoveryTrigger.CONVERSATIONAL,
        proposal=JobIntakeProposal(
            proposal_id=f"typed-proposal-{suffix}",
            intent=JobIntakeIntent.ADD_JOB,
            resolution=ProposalResolution.RESOLVED,
            resolved_candidate=ResolvedJobCandidate(
                source_platform="greenhouse",
                source_url=f"https://boards.greenhouse.io/acme/jobs/{suffix}",
                company="Synthetic Co",
                title="Platform Engineer",
                description="Synthetic job description.",
                source_job_id=suffix,
                application_url=(
                    f"https://boards.greenhouse.io/acme/jobs/{suffix}"
                ),
                location="Remote",
                work_mode="REMOTE",
                ats_type="greenhouse",
            ),
        ),
    )


async def _discover(
    *,
    subject: str,
    request: JobDiscoveryRequest,
    invocation: str,
    home: PrivateHome,
):
    jobs = PrivateHomeJobPostingRepository(home)
    memberships = PrivateHomeSubjectJobLibraryMembershipRepository(home)
    discover = build_subject_job_discovery(
        discovery=build_production_job_discovery(private_home=home),
        job_reader=jobs,
        membership_repository=memberships,
    )
    return discover(
        SubjectJobDiscoveryCommand(
            subject_id=subject,
            request=request,
            source_kind=SubjectJobMembershipSourceKind.EXPLICIT_TYPED_DISCOVERY,
            source_ref=invocation,
            invocation_id=invocation,
            now=NOW,
        )
    )


@pytest.mark.asyncio
async def test_same_global_job_has_isolated_subject_memberships(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    request = _request()

    first = await _discover(
        subject="subject-a",
        request=request,
        invocation="membership-a",
        home=home,
    )
    second = await _discover(
        subject="subject-b",
        request=request,
        invocation="membership-b",
        home=home,
    )
    global_only = build_production_job_discovery(private_home=home)(
        _request(suffix="global-only")
    )

    assert first.status is SubjectJobDiscoveryStatus.ACCEPTED
    assert second.status is SubjectJobDiscoveryStatus.ACCEPTED
    assert first.discovery_response.job_id == second.discovery_response.job_id
    assert first.membership is not None and second.membership is not None
    assert first.membership.membership_id != second.membership.membership_id
    assert (
        first.membership.first_discovery_run_hash
        == first.discovery_response.run_hash
    )
    jobs = PrivateHomeJobPostingRepository(home)
    reader = SubjectScopedJobPostingReader(
        membership_repository=(
            PrivateHomeSubjectJobLibraryMembershipRepository(home)
        ),
        job_posting_reader=jobs,
    )
    assert len(reader.list_current(subject_id="subject-a", now=NOW).ordered_items) == 1
    assert len(reader.list_current(subject_id="subject-b", now=NOW).ordered_items) == 1
    assert (
        reader.get(
            subject_id="subject-c",
            job_id=first.discovery_response.job_id,
            now=NOW,
        ).status
        is SubjectJobPostingReadStatus.NOT_FOUND
    )
    assert global_only.job_id is not None
    assert (
        reader.get(
            subject_id="subject-a",
            job_id=global_only.job_id,
            now=NOW,
        ).status
        is SubjectJobPostingReadStatus.NOT_FOUND
    )


@pytest.mark.asyncio
async def test_membership_replay_is_recoverable_and_invocation_bound(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    first = await _discover(
        subject="subject-a",
        request=_request(suffix="first"),
        invocation="same-invocation",
        home=home,
    )
    replay = await _discover(
        subject="subject-a",
        request=_request(suffix="first"),
        invocation="same-invocation",
        home=home,
    )
    collision = await _discover(
        subject="subject-a",
        request=_request(suffix="second"),
        invocation="same-invocation",
        home=home,
    )

    assert first.membership_status is RegisterSubjectJobMembershipStatus.CREATED
    assert replay.membership_status is RegisterSubjectJobMembershipStatus.UNCHANGED
    assert collision.status is SubjectJobDiscoveryStatus.INTEGRITY_FAILURE
    listed = PrivateHomeSubjectJobLibraryMembershipRepository(
        home
    ).list_for_subject("subject-a")
    assert listed.status is SubjectJobMembershipReadStatus.FOUND
    assert len(listed.memberships) == 1

    interrupted_command = RegisterSubjectJobMembershipCommand(
        subject_id="subject-recovery",
        job_id="job-recovery",
        job_identity_hash=subject_job_identity_hash("job-recovery"),
        discovery_run_id="first-discovery",
        discovery_run_hash="c" * 64,
        job_revision_id="job-recovery:revision:1",
        job_revision_hash="d" * 64,
        source_kind=SubjectJobMembershipSourceKind.EXPLICIT_TYPED_DISCOVERY,
        source_ref="recovery-source",
        invocation_id="recovery-invocation",
        now=NOW,
    )
    interrupted = register_subject_job_membership(
        interrupted_command,
        repository=PrivateHomeSubjectJobLibraryMembershipRepository(
            _InterruptMembershipWrite(home)
        ),
    )
    recovered = register_subject_job_membership(
        replace(
            interrupted_command,
            discovery_run_id="replayed-discovery",
            discovery_run_hash="e" * 64,
        ),
        repository=PrivateHomeSubjectJobLibraryMembershipRepository(home),
    )
    assert interrupted.status is RegisterSubjectJobMembershipStatus.FAILED
    assert recovered.status is RegisterSubjectJobMembershipStatus.CREATED
    assert recovered.membership is not None
    assert recovered.membership.first_discovery_run_id == "first-discovery"


def test_exact_projection_fails_closed_and_reads_are_zero_write(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    repository = PrivateHomeSubjectJobLibraryMembershipRepository(home)
    registered = register_subject_job_membership(
        RegisterSubjectJobMembershipCommand(
            subject_id="subject-a",
            job_id="job-missing",
            job_identity_hash=subject_job_identity_hash("job-missing"),
            discovery_run_id="discovery-missing",
            discovery_run_hash="a" * 64,
            job_revision_id="job-missing:revision:1",
            job_revision_hash="b" * 64,
            source_kind=SubjectJobMembershipSourceKind.EXPLICIT_TYPED_DISCOVERY,
            source_ref="typed-source",
            invocation_id="typed-invocation",
            now=NOW,
        ),
        repository=repository,
    )
    assert registered.status is RegisterSubjectJobMembershipStatus.CREATED
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.root.rglob("*")
        if path.is_file()
    }

    result = SubjectScopedJobPostingReader(
        membership_repository=repository,
        job_posting_reader=PrivateHomeJobPostingRepository(home),
    ).list_current(subject_id="subject-a", now=NOW)

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in home.root.rglob("*")
        if path.is_file()
    }
    assert result.status is SubjectJobPostingReadStatus.INTEGRITY_FAILURE
    assert before == after
