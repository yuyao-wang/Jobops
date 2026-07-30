"""Subject-scoped membership over global canonical JobPosting records."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .job_discovery import JobPosting, JobPostingReadRepository
from .private_home import PrivateHome


SUBJECT_JOB_LIBRARY_MEMBERSHIP_CONTRACT_VERSION = (
    "subject-job-library-membership-v1"
)
SUBJECT_JOB_LIBRARY_READ_CONTRACT_VERSION = "subject-job-library-read-v1"
MAX_SUBJECT_JOB_LIBRARY_MEMBERSHIPS = 10_000
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,239}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _clean(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if cleaned != value or not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is invalid")
    if _ID_RE.fullmatch(cleaned) is None:
        raise ValueError(f"{name} is invalid")
    return cleaned


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("persisted timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("persisted timestamp is invalid")
    return parsed.astimezone(timezone.utc)


def subject_job_identity_hash(job_id: str) -> str:
    return _hash({"canonical_job_id": _clean("job_id", job_id, 160)})


class SubjectJobMembershipSourceKind(StrEnum):
    MANUAL_REFRESH = "MANUAL_REFRESH"
    CONVERSATIONAL_ADD = "CONVERSATIONAL_ADD"
    CONVERSATIONAL_APPLY = "CONVERSATIONAL_APPLY"
    EXPLICIT_TYPED_DISCOVERY = "EXPLICIT_TYPED_DISCOVERY"
    EXPLICIT_MIGRATION = "EXPLICIT_MIGRATION"


class RegisterSubjectJobMembershipStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    INVALID = "INVALID"
    FAILED = "FAILED"


class SubjectJobMembershipReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    EMPTY = "EMPTY"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class SubjectJobPostingReadStatus(StrEnum):
    READY = "READY"
    EMPTY = "EMPTY"
    PARTIAL = "PARTIAL"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SubjectJobLibraryMembership:
    membership_id: str
    subject_id: str
    job_id: str
    job_identity_hash: str
    first_discovery_run_id: str
    first_discovery_run_hash: str
    first_job_revision_id: str
    first_job_revision_hash: str
    source_kind: SubjectJobMembershipSourceKind
    source_ref: str
    created_at: datetime
    invocation_id: str
    membership_hash: str
    membership_contract_version: str = (
        SUBJECT_JOB_LIBRARY_MEMBERSHIP_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        for name in (
            "membership_id",
            "subject_id",
            "job_id",
            "first_discovery_run_id",
            "first_job_revision_id",
            "source_ref",
            "invocation_id",
        ):
            _clean(name, getattr(self, name))
        for name in (
            "job_identity_hash",
            "first_discovery_run_hash",
            "first_job_revision_hash",
            "membership_hash",
        ):
            if _HASH_RE.fullmatch(getattr(self, name)) is None:
                raise ValueError(f"{name} is invalid")
        object.__setattr__(
            self, "source_kind", SubjectJobMembershipSourceKind(self.source_kind)
        )
        if self.membership_contract_version != (
            SUBJECT_JOB_LIBRARY_MEMBERSHIP_CONTRACT_VERSION
        ):
            raise ValueError("membership contract version is unsupported")
        _time(self.created_at)
        identity = self.identity_dict()
        identity_hash = _hash(identity)
        expected_hash = _hash(self.binding_dict())
        if self.membership_hash != expected_hash:
            raise ValueError("membership hash is invalid")
        if self.membership_id != f"subject-job-membership-{identity_hash[:32]}":
            raise ValueError("membership ID is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_identity_hash": self.job_identity_hash,
            "membership_contract_version": self.membership_contract_version,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "created_at": _time(self.created_at),
            "first_discovery_run_hash": self.first_discovery_run_hash,
            "first_discovery_run_id": self.first_discovery_run_id,
            "first_job_revision_hash": self.first_job_revision_hash,
            "first_job_revision_id": self.first_job_revision_id,
            "invocation_id": self.invocation_id,
            "membership_hash": self.membership_hash,
            "membership_id": self.membership_id,
            "source_kind": self.source_kind.value,
            "source_ref": self.source_ref,
        }

    def binding_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "first_discovery_run_hash": self.first_discovery_run_hash,
            "first_discovery_run_id": self.first_discovery_run_id,
            "first_job_revision_hash": self.first_job_revision_hash,
            "first_job_revision_id": self.first_job_revision_id,
            "invocation_id": self.invocation_id,
            "source_kind": self.source_kind.value,
            "source_ref": self.source_ref,
        }

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        job_id: str,
        first_discovery_run_id: str,
        first_discovery_run_hash: str,
        first_job_revision_id: str,
        first_job_revision_hash: str,
        source_kind: SubjectJobMembershipSourceKind,
        source_ref: str,
        created_at: datetime,
        invocation_id: str,
    ) -> "SubjectJobLibraryMembership":
        identity = {
            "job_id": _clean("job_id", job_id, 160),
            "job_identity_hash": subject_job_identity_hash(job_id),
            "membership_contract_version": (
                SUBJECT_JOB_LIBRARY_MEMBERSHIP_CONTRACT_VERSION
            ),
            "subject_id": _clean("subject_id", subject_id, 160),
        }
        binding = {
            **identity,
            "first_discovery_run_hash": first_discovery_run_hash,
            "first_discovery_run_id": first_discovery_run_id,
            "first_job_revision_hash": first_job_revision_hash,
            "first_job_revision_id": first_job_revision_id,
            "invocation_id": invocation_id,
            "source_kind": SubjectJobMembershipSourceKind(source_kind).value,
            "source_ref": source_ref,
        }
        membership_hash = _hash(binding)
        identity_hash = _hash(identity)
        return cls(
            membership_id=f"subject-job-membership-{identity_hash[:32]}",
            subject_id=identity["subject_id"],
            job_id=identity["job_id"],
            job_identity_hash=identity["job_identity_hash"],
            first_discovery_run_id=first_discovery_run_id,
            first_discovery_run_hash=first_discovery_run_hash,
            first_job_revision_id=first_job_revision_id,
            first_job_revision_hash=first_job_revision_hash,
            source_kind=source_kind,
            source_ref=source_ref,
            created_at=created_at,
            invocation_id=invocation_id,
            membership_hash=membership_hash,
        )


def _membership_from_dict(value: Any) -> SubjectJobLibraryMembership:
    if not isinstance(value, Mapping):
        raise ValueError("persisted membership is invalid")
    payload = dict(value)
    payload["created_at"] = _parse_time(payload["created_at"])
    payload["source_kind"] = SubjectJobMembershipSourceKind(
        payload["source_kind"]
    )
    return SubjectJobLibraryMembership(**payload)


@dataclass(frozen=True, slots=True)
class RegisterSubjectJobMembershipCommand:
    subject_id: str
    job_id: str
    job_identity_hash: str
    discovery_run_id: str
    discovery_run_hash: str
    job_revision_id: str
    job_revision_hash: str
    source_kind: SubjectJobMembershipSourceKind
    source_ref: str
    invocation_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class RegisterSubjectJobMembershipResult:
    status: RegisterSubjectJobMembershipStatus
    membership: SubjectJobLibraryMembership | None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class SubjectJobMembershipReadResult:
    status: SubjectJobMembershipReadStatus
    membership: SubjectJobLibraryMembership | None = None


@dataclass(frozen=True, slots=True)
class SubjectJobMembershipListResult:
    status: SubjectJobMembershipReadStatus
    memberships: tuple[SubjectJobLibraryMembership, ...]
    snapshot_hash: str


@runtime_checkable
class SubjectJobLibraryMembershipRepository(Protocol):
    def register(
        self, command: RegisterSubjectJobMembershipCommand
    ) -> RegisterSubjectJobMembershipResult: ...

    def get(
        self, *, subject_id: str, job_id: str
    ) -> SubjectJobMembershipReadResult: ...

    def list_for_subject(
        self, subject_id: str
    ) -> SubjectJobMembershipListResult: ...


class PrivateHomeSubjectJobLibraryMembershipRepository:
    """Immutable subject partitions with recoverable invocation receipts."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    @property
    def _root(self) -> Path:
        return (
            self._home.root
            / "state"
            / "discovery"
            / "subject-job-library-memberships"
        )

    @staticmethod
    def _subject_key(subject_id: str) -> str:
        return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()

    def _directory(self, subject_id: str) -> Path:
        return self._root / self._subject_key(_clean("subject_id", subject_id, 160))

    def _membership_path(self, subject_id: str, job_id: str) -> Path:
        digest = subject_job_identity_hash(job_id)
        return self._directory(subject_id) / "memberships" / f"{digest}.json"

    def _receipt_path(self, subject_id: str, invocation_id: str) -> Path:
        digest = hashlib.sha256(
            _clean("invocation_id", invocation_id).encode("utf-8")
        ).hexdigest()
        return self._directory(subject_id) / "receipts" / f"{digest}.json"

    @staticmethod
    def _encoded(value: Mapping[str, Any]) -> bytes:
        return (
            json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _read_membership(path: Path) -> SubjectJobLibraryMembership:
        if path.is_symlink() or not path.is_file():
            raise ValueError("membership path is invalid")
        return _membership_from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def register(
        self, command: RegisterSubjectJobMembershipCommand
    ) -> RegisterSubjectJobMembershipResult:
        try:
            if command.job_identity_hash != subject_job_identity_hash(
                command.job_id
            ):
                raise ValueError("job identity hash is invalid")
            membership = SubjectJobLibraryMembership.create(
                subject_id=command.subject_id,
                job_id=command.job_id,
                first_discovery_run_id=command.discovery_run_id,
                first_discovery_run_hash=command.discovery_run_hash,
                first_job_revision_id=command.job_revision_id,
                first_job_revision_hash=command.job_revision_hash,
                source_kind=command.source_kind,
                source_ref=command.source_ref,
                created_at=command.now,
                invocation_id=command.invocation_id,
            )
            receipt_path = self._receipt_path(
                membership.subject_id, command.invocation_id
            )
            membership_path = self._membership_path(
                membership.subject_id, membership.job_id
            )
            command_binding_hash = _hash(
                {
                    "invocation_id": membership.invocation_id,
                    "job_identity_hash": membership.job_identity_hash,
                    "source_kind": command.source_kind.value,
                    "source_ref": command.source_ref,
                    "subject_id": membership.subject_id,
                }
            )
        except (AttributeError, TypeError, ValueError):
            return RegisterSubjectJobMembershipResult(
                RegisterSubjectJobMembershipStatus.INVALID, None, "INVALID"
            )
        with self._lock:
            try:
                existing: SubjectJobLibraryMembership | None = None
                if membership_path.exists():
                    existing = self._read_membership(membership_path)
                    if existing.identity_dict() != membership.identity_dict():
                        return RegisterSubjectJobMembershipResult(
                            RegisterSubjectJobMembershipStatus.INTEGRITY_FAILURE,
                            None,
                            "MEMBERSHIP_BINDING_MISMATCH",
                        )
                if receipt_path.exists():
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    receipt_membership = _membership_from_dict(
                        receipt["membership"]
                    )
                    if (
                        receipt["command_binding_hash"]
                        != command_binding_hash
                        or receipt_membership.identity_dict()
                        != membership.identity_dict()
                        or (
                            existing is not None
                            and receipt_membership.identity_dict()
                            != existing.identity_dict()
                        )
                    ):
                        return RegisterSubjectJobMembershipResult(
                            RegisterSubjectJobMembershipStatus.INTEGRITY_FAILURE,
                            None,
                            "INVOCATION_BINDING_MISMATCH",
                        )
                    membership = existing or receipt_membership
                elif existing is not None:
                    membership = existing
                self._home.ensure()
                if not receipt_path.exists():
                    receipt_created = self._home.write_bytes_if_absent(
                        receipt_path,
                        self._encoded(
                            {
                                "command_binding_hash": command_binding_hash,
                                "membership": membership.to_dict(),
                            }
                        ),
                    )
                    if not receipt_created:
                        receipt = json.loads(
                            receipt_path.read_text(encoding="utf-8")
                        )
                        receipt_membership = _membership_from_dict(
                            receipt["membership"]
                        )
                        if (
                            receipt["command_binding_hash"]
                            != command_binding_hash
                            or receipt_membership.identity_dict()
                            != membership.identity_dict()
                        ):
                            return RegisterSubjectJobMembershipResult(
                                RegisterSubjectJobMembershipStatus.INTEGRITY_FAILURE,
                                None,
                                "INVOCATION_BINDING_MISMATCH",
                            )
                        membership = receipt_membership
                created = False
                if existing is None:
                    created = self._home.write_bytes_if_absent(
                        membership_path, self._encoded(membership.to_dict())
                    )
                    existing = (
                        membership
                        if created
                        else self._read_membership(membership_path)
                    )
                if existing.identity_dict() != membership.identity_dict():
                    return RegisterSubjectJobMembershipResult(
                        RegisterSubjectJobMembershipStatus.INTEGRITY_FAILURE,
                        None,
                        "MEMBERSHIP_BINDING_MISMATCH",
                    )
                return RegisterSubjectJobMembershipResult(
                    (
                        RegisterSubjectJobMembershipStatus.CREATED
                        if created
                        else RegisterSubjectJobMembershipStatus.UNCHANGED
                    ),
                    existing,
                )
            except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
                return RegisterSubjectJobMembershipResult(
                    RegisterSubjectJobMembershipStatus.FAILED,
                    None,
                    "PERSISTENCE_FAILED",
                )

    def get(
        self, *, subject_id: str, job_id: str
    ) -> SubjectJobMembershipReadResult:
        try:
            path = self._membership_path(subject_id, job_id)
            if not path.exists():
                return SubjectJobMembershipReadResult(
                    SubjectJobMembershipReadStatus.NOT_FOUND
                )
            membership = self._read_membership(path)
            if (
                membership.subject_id != subject_id
                or membership.job_id != job_id
            ):
                raise ValueError("membership binding is invalid")
            return SubjectJobMembershipReadResult(
                SubjectJobMembershipReadStatus.FOUND, membership
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return SubjectJobMembershipReadResult(
                SubjectJobMembershipReadStatus.INTEGRITY_FAILURE
            )

    def list_for_subject(
        self, subject_id: str
    ) -> SubjectJobMembershipListResult:
        try:
            directory = self._directory(subject_id) / "memberships"
            if not directory.exists():
                return SubjectJobMembershipListResult(
                    SubjectJobMembershipReadStatus.EMPTY,
                    (),
                    _hash(
                        {
                            "membership_contract_version": (
                                SUBJECT_JOB_LIBRARY_MEMBERSHIP_CONTRACT_VERSION
                            ),
                            "membership_hashes": [],
                            "subject_id": subject_id,
                        }
                    ),
                )
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("membership directory is invalid")
            paths = tuple(directory.iterdir())
            if (
                len(paths) > MAX_SUBJECT_JOB_LIBRARY_MEMBERSHIPS
                or any(
                    path.suffix != ".json" or path.name.startswith(".")
                    for path in paths
                )
            ):
                raise ValueError("membership partition is invalid or oversized")
            memberships = tuple(
                sorted(
                    (self._read_membership(path) for path in paths),
                    key=lambda item: (
                        item.created_at.astimezone(timezone.utc),
                        item.job_id,
                        item.membership_id,
                    ),
                )
            )
            if any(item.subject_id != subject_id for item in memberships):
                raise ValueError("membership list mixes subjects")
            snapshot = _hash(
                {
                    "membership_contract_version": (
                        SUBJECT_JOB_LIBRARY_MEMBERSHIP_CONTRACT_VERSION
                    ),
                    "membership_hashes": [
                        item.membership_hash for item in memberships
                    ],
                    "subject_id": subject_id,
                }
            )
            return SubjectJobMembershipListResult(
                (
                    SubjectJobMembershipReadStatus.FOUND
                    if memberships
                    else SubjectJobMembershipReadStatus.EMPTY
                ),
                memberships,
                snapshot,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return SubjectJobMembershipListResult(
                SubjectJobMembershipReadStatus.INTEGRITY_FAILURE, (), ""
            )


def register_subject_job_membership(
    command: RegisterSubjectJobMembershipCommand,
    *,
    repository: SubjectJobLibraryMembershipRepository,
) -> RegisterSubjectJobMembershipResult:
    return repository.register(command)


def get_subject_job_membership(
    *,
    subject_id: str,
    job_id: str,
    repository: SubjectJobLibraryMembershipRepository,
) -> SubjectJobMembershipReadResult:
    return repository.get(subject_id=subject_id, job_id=job_id)


def list_subject_job_memberships(
    *,
    subject_id: str,
    repository: SubjectJobLibraryMembershipRepository,
) -> SubjectJobMembershipListResult:
    return repository.list_for_subject(subject_id)


@dataclass(frozen=True, slots=True)
class SubjectJobPostingItem:
    membership: SubjectJobLibraryMembership
    job_posting: JobPosting
    current_job_revision_ref: str
    item_hash: str


@dataclass(frozen=True, slots=True)
class SubjectJobPostingListResult:
    status: SubjectJobPostingReadStatus
    subject_id: str
    membership_snapshot_hash: str
    job_snapshot_hash: str
    ordered_items: tuple[SubjectJobPostingItem, ...]
    evaluated_at: datetime
    failure_count: int = 0
    read_contract_version: str = SUBJECT_JOB_LIBRARY_READ_CONTRACT_VERSION


@runtime_checkable
class SubjectScopedJobPostingReadPort(Protocol):
    def get(
        self, *, subject_id: str, job_id: str, now: datetime
    ) -> SubjectJobPostingListResult: ...

    def list_current(
        self, *, subject_id: str, now: datetime
    ) -> SubjectJobPostingListResult: ...


class SubjectScopedJobPostingReader:
    def __init__(
        self,
        *,
        membership_repository: SubjectJobLibraryMembershipRepository,
        job_posting_reader: JobPostingReadRepository,
    ) -> None:
        self._memberships = membership_repository
        self._jobs = job_posting_reader

    def get(
        self, *, subject_id: str, job_id: str, now: datetime
    ) -> SubjectJobPostingListResult:
        membership = self._memberships.get(
            subject_id=subject_id, job_id=job_id
        )
        if membership.status is SubjectJobMembershipReadStatus.NOT_FOUND:
            return SubjectJobPostingListResult(
                SubjectJobPostingReadStatus.NOT_FOUND,
                subject_id,
                "",
                "",
                (),
                now,
            )
        if (
            membership.status is not SubjectJobMembershipReadStatus.FOUND
            or membership.membership is None
        ):
            return SubjectJobPostingListResult(
                SubjectJobPostingReadStatus.INTEGRITY_FAILURE,
                subject_id,
                "",
                "",
                (),
                now,
                1,
            )
        return self._project(
            subject_id=subject_id,
            memberships=(membership.membership,),
            membership_snapshot_hash=_hash(
                {
                    "membership_contract_version": (
                        SUBJECT_JOB_LIBRARY_MEMBERSHIP_CONTRACT_VERSION
                    ),
                    "membership_hashes": [
                        membership.membership.membership_hash
                    ],
                    "subject_id": subject_id,
                }
            ),
            now=now,
        )

    def list_current(
        self, *, subject_id: str, now: datetime
    ) -> SubjectJobPostingListResult:
        listed = self._memberships.list_for_subject(subject_id)
        if listed.status is SubjectJobMembershipReadStatus.EMPTY:
            return SubjectJobPostingListResult(
                SubjectJobPostingReadStatus.EMPTY,
                subject_id,
                listed.snapshot_hash,
                _hash(
                    {
                        "item_hashes": [],
                        "membership_snapshot_hash": listed.snapshot_hash,
                        "read_contract_version": (
                            SUBJECT_JOB_LIBRARY_READ_CONTRACT_VERSION
                        ),
                        "status": SubjectJobPostingReadStatus.EMPTY.value,
                        "subject_id": subject_id,
                    }
                ),
                (),
                now,
            )
        if listed.status is not SubjectJobMembershipReadStatus.FOUND:
            return SubjectJobPostingListResult(
                SubjectJobPostingReadStatus.INTEGRITY_FAILURE,
                subject_id,
                "",
                "",
                (),
                now,
                1,
            )
        return self._project(
            subject_id=subject_id,
            memberships=listed.memberships,
            membership_snapshot_hash=listed.snapshot_hash,
            now=now,
        )

    def _project(
        self,
        *,
        subject_id: str,
        memberships: tuple[SubjectJobLibraryMembership, ...],
        membership_snapshot_hash: str,
        now: datetime,
    ) -> SubjectJobPostingListResult:
        items: list[SubjectJobPostingItem] = []
        for membership in memberships:
            if membership.subject_id != subject_id:
                return SubjectJobPostingListResult(
                    SubjectJobPostingReadStatus.INTEGRITY_FAILURE,
                    subject_id,
                    membership_snapshot_hash,
                    "",
                    (),
                    now,
                    1,
                )
            try:
                job = self._jobs.get(membership.job_id)
            except (OSError, RuntimeError, TypeError, ValueError):
                job = None
            if (
                job is None
                or job.job_id != membership.job_id
                or subject_job_identity_hash(job.job_id)
                != membership.job_identity_hash
            ):
                return SubjectJobPostingListResult(
                    SubjectJobPostingReadStatus.INTEGRITY_FAILURE,
                    subject_id,
                    membership_snapshot_hash,
                    "",
                    (),
                    now,
                    1,
                )
            ref = f"{job.job_id}:revision:{job.revision}"
            item_hash = _hash(
                {
                    "job_content_hash": job.content_hash,
                    "job_id": job.job_id,
                    "job_revision": job.revision,
                    "membership_hash": membership.membership_hash,
                    "read_contract_version": (
                        SUBJECT_JOB_LIBRARY_READ_CONTRACT_VERSION
                    ),
                }
            )
            items.append(
                SubjectJobPostingItem(membership, job, ref, item_hash)
            )
        ordered = tuple(items)
        return SubjectJobPostingListResult(
            SubjectJobPostingReadStatus.READY,
            subject_id,
            membership_snapshot_hash,
            _hash(
                {
                    "item_hashes": [item.item_hash for item in ordered],
                    "membership_snapshot_hash": membership_snapshot_hash,
                    "read_contract_version": (
                        SUBJECT_JOB_LIBRARY_READ_CONTRACT_VERSION
                    ),
                    "status": SubjectJobPostingReadStatus.READY.value,
                    "subject_id": subject_id,
                }
            ),
            ordered,
            now,
        )


def list_subject_job_postings(
    *,
    subject_id: str,
    now: datetime,
    reader: SubjectScopedJobPostingReadPort,
) -> SubjectJobPostingListResult:
    return reader.list_current(subject_id=subject_id, now=now)


__all__ = [
    "PrivateHomeSubjectJobLibraryMembershipRepository",
    "RegisterSubjectJobMembershipCommand",
    "RegisterSubjectJobMembershipResult",
    "RegisterSubjectJobMembershipStatus",
    "SubjectJobLibraryMembership",
    "SubjectJobMembershipListResult",
    "SubjectJobMembershipReadResult",
    "SubjectJobMembershipReadStatus",
    "SubjectJobMembershipSourceKind",
    "SubjectJobPostingItem",
    "SubjectJobPostingListResult",
    "SubjectJobPostingReadStatus",
    "SubjectScopedJobPostingReadPort",
    "SubjectScopedJobPostingReader",
    "get_subject_job_membership",
    "list_subject_job_memberships",
    "list_subject_job_postings",
    "register_subject_job_membership",
    "subject_job_identity_hash",
]
