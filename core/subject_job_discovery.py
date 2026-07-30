"""One subject-aware boundary over global Discovery plus membership."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from .job_discovery import (
    DiscoveryDisposition,
    JobDiscoveryRequest,
    JobDiscoveryResponse,
    JobPostingReadRepository,
)
from .subject_job_library import (
    RegisterSubjectJobMembershipCommand,
    RegisterSubjectJobMembershipStatus,
    SubjectJobLibraryMembership,
    SubjectJobLibraryMembershipRepository,
    SubjectJobMembershipSourceKind,
    register_subject_job_membership,
    subject_job_identity_hash,
)


class SubjectJobDiscoveryStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    MEMBERSHIP_FAILED = "MEMBERSHIP_FAILED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class SubjectJobDiscoveryCommand:
    subject_id: str
    request: JobDiscoveryRequest
    source_kind: SubjectJobMembershipSourceKind
    source_ref: str
    invocation_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class SubjectJobDiscoveryResult:
    status: SubjectJobDiscoveryStatus
    discovery_response: JobDiscoveryResponse
    membership: SubjectJobLibraryMembership | None
    membership_status: RegisterSubjectJobMembershipStatus | None


class GlobalDiscoveryCallable(Protocol):
    def __call__(
        self, request: JobDiscoveryRequest
    ) -> JobDiscoveryResponse: ...


def run_subject_job_discovery(
    command: SubjectJobDiscoveryCommand,
    *,
    discovery: GlobalDiscoveryCallable,
    job_reader: JobPostingReadRepository,
    membership_repository: SubjectJobLibraryMembershipRepository,
) -> SubjectJobDiscoveryResult:
    """Create membership only after one accepted canonical Discovery write."""

    response = discovery(command.request)
    if not isinstance(response, JobDiscoveryResponse):
        raise TypeError("discovery must return JobDiscoveryResponse")
    if response.disposition is not DiscoveryDisposition.ACCEPTED:
        return SubjectJobDiscoveryResult(
            SubjectJobDiscoveryStatus.NOT_ACCEPTED, response, None, None
        )
    if (
        response.job_id is None
        or response.run_id is None
        or response.run_hash is None
        or len(response.run_hash) != 64
    ):
        return SubjectJobDiscoveryResult(
            SubjectJobDiscoveryStatus.INTEGRITY_FAILURE,
            response,
            None,
            RegisterSubjectJobMembershipStatus.INTEGRITY_FAILURE,
        )
    try:
        job = job_reader.get(response.job_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        job = None
    if job is None or job.job_id != response.job_id:
        return SubjectJobDiscoveryResult(
            SubjectJobDiscoveryStatus.INTEGRITY_FAILURE,
            response,
            None,
            RegisterSubjectJobMembershipStatus.INTEGRITY_FAILURE,
        )
    registered = register_subject_job_membership(
        RegisterSubjectJobMembershipCommand(
            subject_id=command.subject_id,
            job_id=job.job_id,
            job_identity_hash=subject_job_identity_hash(job.job_id),
            discovery_run_id=response.run_id,
            discovery_run_hash=response.run_hash,
            job_revision_id=f"{job.job_id}:revision:{job.revision}",
            job_revision_hash=job.content_hash,
            source_kind=command.source_kind,
            source_ref=command.source_ref,
            invocation_id=command.invocation_id,
            now=command.now,
        ),
        repository=membership_repository,
    )
    if registered.status not in {
        RegisterSubjectJobMembershipStatus.CREATED,
        RegisterSubjectJobMembershipStatus.UNCHANGED,
    }:
        return SubjectJobDiscoveryResult(
            (
                SubjectJobDiscoveryStatus.INTEGRITY_FAILURE
                if registered.status
                is RegisterSubjectJobMembershipStatus.INTEGRITY_FAILURE
                else SubjectJobDiscoveryStatus.MEMBERSHIP_FAILED
            ),
            response,
            None,
            registered.status,
        )
    return SubjectJobDiscoveryResult(
        SubjectJobDiscoveryStatus.ACCEPTED,
        response,
        registered.membership,
        registered.status,
    )


def build_subject_job_discovery(
    *,
    discovery: GlobalDiscoveryCallable,
    job_reader: JobPostingReadRepository,
    membership_repository: SubjectJobLibraryMembershipRepository,
) -> Callable[
    [SubjectJobDiscoveryCommand], SubjectJobDiscoveryResult
]:
    def invoke(
        command: SubjectJobDiscoveryCommand,
    ) -> SubjectJobDiscoveryResult:
        return run_subject_job_discovery(
            command,
            discovery=discovery,
            job_reader=job_reader,
            membership_repository=membership_repository,
        )

    return invoke


__all__ = [
    "SubjectJobDiscoveryCommand",
    "SubjectJobDiscoveryResult",
    "SubjectJobDiscoveryStatus",
    "build_subject_job_discovery",
    "run_subject_job_discovery",
]
