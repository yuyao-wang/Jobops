"""Plan-scoped handoff from immutable preparation records to ApplicationBundle."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from pathlib import PurePosixPath
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .application_answer_taxonomy import (
    CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION,
    CanonicalApplicationAnswers,
    canonical_application_answer_taxonomy_hash,
)
from .application_answers import (
    PreparedApplicationAnswerSet,
    PreparedApplicationAnswerSetReadStatus,
    PreparedApplicationAnswerSetRepository,
)
from .application_assembly_execution_context import (
    APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION,
    ApplicationAssemblyExecutionContext,
    ApplicationAssemblyExecutionContextFailureReason,
    LoadApplicationAssemblyExecutionContextCommand,
    LoadApplicationAssemblyExecutionContextStatus,
    PlanExecutionPolicyProvider,
    VerifiedExecutionProfileProvider,
    load_application_assembly_execution_context,
)
from .application_execution_profile import ApplicationExecutionIdentityProfile
from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .bundles import (
    APPLICATION_BUNDLE_CONTRACT_VERSION,
    ApplicationBundle,
    ManagedArtifactReference,
    MaterialBundle,
    application_bundle_canonical_hash,
    normalized_job_url,
)
from .job_discovery import (
    JobPosting,
    JobPostingReadRepository,
    JobPostingRepositoryError,
)
from .plan_material_manifest import (
    PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION,
    PlanMaterialAssemblyState,
    PlanMaterialEntry,
    PlanMaterialManifest,
    PlanMaterialManifestReadStatus,
    PlanMaterialManifestRepository,
    PlanMaterialRole,
)
from .private_home import PrivateHome, PrivateHomeError
from .plan_execution_policy import (
    PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION,
    PlanExecutionPolicyDecisionRecord,
)
from .policy import PolicyDecision
from .resume_compilation import pdf_page_count
from .recoverable_application_bundle import (
    RecoverableApplicationBundleEnvelopeReadStatus,
    RecoverableApplicationBundleEnvelopeRepository,
    RecoverableApplicationBundleEnvelopeWriteStatus,
    create_recoverable_application_bundle_envelope,
)
from .verified_application_execution_profile import (
    VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION,
    VerifiedApplicationExecutionProfile,
)


APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION_V1 = (
    "plan-scoped-application-bundle-assembly-v1"
)
APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION = (
    "plan-scoped-application-bundle-assembly-v2"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_RECORD_ID_RE = re.compile(
    r"^application-bundle-assembly-[a-f0-9]{64}$"
)


class ApplicationBundleAssemblyStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NOT_READY = "NOT_READY"
    FAILED = "FAILED"


class ApplicationBundleAssemblyReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ApplicationBundleAssemblyWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ApplicationBundleAssemblyListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ApplicationBundleAssemblyNotReadyReason(StrEnum):
    MANIFEST_NOT_FOUND = "MANIFEST_NOT_FOUND"
    REQUIRED_MATERIALS_INCOMPLETE = "REQUIRED_MATERIALS_INCOMPLETE"
    ANSWER_SET_NOT_FOUND = "ANSWER_SET_NOT_FOUND"
    BLOCKING_UNRESOLVED_ANSWERS = "BLOCKING_UNRESOLVED_ANSWERS"
    VERIFIED_PROFILE_NOT_READY = "VERIFIED_PROFILE_NOT_READY"
    EXECUTION_POLICY_NOT_READY = "EXECUTION_POLICY_NOT_READY"


class ApplicationBundleAssemblyFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = "APPLICATION_PLAN_INTEGRITY_FAILURE"
    APPLICATION_PLAN_SUBJECT_MISMATCH = "APPLICATION_PLAN_SUBJECT_MISMATCH"
    JOB_POSTING_NOT_FOUND = "JOB_POSTING_NOT_FOUND"
    JOB_POSTING_INTEGRITY_FAILURE = "JOB_POSTING_INTEGRITY_FAILURE"
    JOB_POSTING_BINDING_MISMATCH = "JOB_POSTING_BINDING_MISMATCH"
    MANIFEST_INTEGRITY_FAILURE = "MANIFEST_INTEGRITY_FAILURE"
    MANIFEST_BINDING_MISMATCH = "MANIFEST_BINDING_MISMATCH"
    MANIFEST_VERSION_INCOMPATIBLE = "MANIFEST_VERSION_INCOMPATIBLE"
    ANSWER_SET_INTEGRITY_FAILURE = "ANSWER_SET_INTEGRITY_FAILURE"
    ANSWER_SET_BINDING_MISMATCH = "ANSWER_SET_BINDING_MISMATCH"
    TAXONOMY_BINDING_MISMATCH = "TAXONOMY_BINDING_MISMATCH"
    ARTIFACT_INTEGRITY_FAILURE = "ARTIFACT_INTEGRITY_FAILURE"
    VERIFIED_PROFILE_CONFLICT = "VERIFIED_PROFILE_CONFLICT"
    EXECUTION_POLICY_CONFLICT = "EXECUTION_POLICY_CONFLICT"
    VERIFIED_PROFILE_INTEGRITY_FAILURE = (
        "VERIFIED_PROFILE_INTEGRITY_FAILURE"
    )
    EXECUTION_POLICY_INTEGRITY_FAILURE = (
        "EXECUTION_POLICY_INTEGRITY_FAILURE"
    )
    EXECUTION_CONTEXT_BINDING_MISMATCH = (
        "EXECUTION_CONTEXT_BINDING_MISMATCH"
    )
    EXECUTION_CONTEXT_VERSION_INCOMPATIBLE = (
        "EXECUTION_CONTEXT_VERSION_INCOMPATIBLE"
    )
    EXECUTION_CONTEXT_PROVIDER_FAILED = "EXECUTION_CONTEXT_PROVIDER_FAILED"
    BUNDLE_FACTORY_FAILURE = "BUNDLE_FACTORY_FAILURE"
    BUNDLE_CONTRACT_MISMATCH = "BUNDLE_CONTRACT_MISMATCH"
    BUNDLE_ENVELOPE_PERSISTENCE_FAILED = (
        "BUNDLE_ENVELOPE_PERSISTENCE_FAILED"
    )
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    RECORD_INTEGRITY_FAILURE = "RECORD_INTEGRITY_FAILURE"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _clean(name: str, value: Any, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the assembly contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return (
        _aware("timestamp", value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("persisted timestamp is invalid")
    return _aware(
        "assembled_at", datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def _entry_hash(entry: PlanMaterialEntry) -> str:
    return _hash(entry.to_dict())


@dataclass(frozen=True, slots=True)
class ApplicationBundleFactoryRequest:
    """Exact prepared inputs passed to the existing execution-bundle factory."""

    run_id: str
    subject_id: str
    application_plan: ApplicationPlan
    job_posting: JobPosting
    materials: MaterialBundle
    answers: CanonicalApplicationAnswers
    identity_profile: ApplicationExecutionIdentityProfile
    policy_decision: PolicyDecision
    verified_profile_ref: VerifiedApplicationExecutionProfile
    execution_policy_ref: PlanExecutionPolicyDecisionRecord
    execution_context_binding_hash: str


@runtime_checkable
class ApplicationBundleFactory(Protocol):
    def create(
        self, request: ApplicationBundleFactoryRequest
    ) -> ApplicationBundle: ...


@dataclass(frozen=True, slots=True)
class ApplicationBundleAssemblyRecord:
    record_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    manifest_id: str
    manifest_content_hash: str
    answer_set_id: str
    answer_set_content_hash: str
    resume_entry_id: str
    resume_entry_hash: str
    cover_letter_entry_id: str
    cover_letter_entry_hash: str
    prepared_resume_material_id: str
    prepared_resume_material_hash: str
    prepared_cover_letter_material_id: str
    prepared_cover_letter_material_hash: str
    taxonomy_version: str
    taxonomy_hash: str
    application_bundle_contract_version: str
    application_bundle_run_id: str
    application_bundle_canonical_hash: str
    record_content_hash: str
    assembled_at: datetime
    execution_context_contract_version: str | None = None
    execution_context_binding_hash: str | None = None
    verified_profile_id: str | None = None
    verified_profile_version: str | None = None
    verified_profile_hash: str | None = None
    execution_policy_record_id: str | None = None
    execution_policy_record_version: str | None = None
    execution_policy_record_hash: str | None = None

    def __post_init__(self) -> None:
        if self.contract_version not in {
            APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION_V1,
            APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION,
        }:
            raise ValueError("assembly record contract is unsupported")
        for name in (
            "subject_id",
            "application_plan_id",
            "job_id",
            "manifest_id",
            "answer_set_id",
            "resume_entry_id",
            "cover_letter_entry_id",
            "prepared_resume_material_id",
            "prepared_cover_letter_material_id",
            "taxonomy_version",
            "application_bundle_contract_version",
            "application_bundle_run_id",
        ):
            _clean(name, getattr(self, name))
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job_revision must be positive")
        for name in (
            "job_content_hash",
            "manifest_content_hash",
            "answer_set_content_hash",
            "resume_entry_hash",
            "cover_letter_entry_hash",
            "prepared_resume_material_hash",
            "prepared_cover_letter_material_hash",
            "taxonomy_hash",
            "application_bundle_canonical_hash",
            "record_content_hash",
        ):
            _require_hash(name, getattr(self, name))
        if self.taxonomy_version != CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION:
            raise ValueError("assembly taxonomy version is unsupported")
        if self.taxonomy_hash != canonical_application_answer_taxonomy_hash():
            raise ValueError("assembly taxonomy hash is unsupported")
        if (
            self.application_bundle_contract_version
            != APPLICATION_BUNDLE_CONTRACT_VERSION
        ):
            raise ValueError("ApplicationBundle contract is unsupported")
        context_values = (
            self.execution_context_contract_version,
            self.execution_context_binding_hash,
            self.verified_profile_id,
            self.verified_profile_version,
            self.verified_profile_hash,
            self.execution_policy_record_id,
            self.execution_policy_record_version,
            self.execution_policy_record_hash,
        )
        if (
            self.contract_version
            == APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION_V1
        ):
            if any(item is not None for item in context_values):
                raise ValueError("legacy assembly cannot contain context")
        else:
            if any(item is None for item in context_values):
                raise ValueError("assembly execution context is incomplete")
            if self.execution_context_contract_version != (
                APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION
            ):
                raise ValueError("assembly execution context is unsupported")
            if self.execution_policy_record_version != (
                PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
            ):
                raise ValueError("assembly execution policy is unsupported")
            for name in (
                "execution_context_contract_version",
                "verified_profile_id",
                "verified_profile_version",
                "execution_policy_record_id",
                "execution_policy_record_version",
            ):
                _clean(name, getattr(self, name))
            for name in (
                "execution_context_binding_hash",
                "verified_profile_hash",
                "execution_policy_record_hash",
            ):
                _require_hash(name, getattr(self, name))
        expected_id = "application-bundle-assembly-" + _hash(
            self.identity_dict()
        )
        if (
            _RECORD_ID_RE.fullmatch(self.record_id) is None
            or self.record_id != expected_id
        ):
            raise ValueError("assembly record identity is invalid")
        _aware("assembled_at", self.assembled_at)
        if self.record_content_hash != _hash(self.content_dict()):
            raise ValueError("assembly record content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        identity = {
            "answer_set_content_hash": self.answer_set_content_hash,
            "answer_set_id": self.answer_set_id,
            "application_bundle_canonical_hash": (
                self.application_bundle_canonical_hash
            ),
            "application_bundle_contract_version": (
                self.application_bundle_contract_version
            ),
            "application_bundle_run_id": self.application_bundle_run_id,
            "application_plan_id": self.application_plan_id,
            "contract_version": self.contract_version,
            "cover_letter_entry_hash": self.cover_letter_entry_hash,
            "cover_letter_entry_id": self.cover_letter_entry_id,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "manifest_content_hash": self.manifest_content_hash,
            "manifest_id": self.manifest_id,
            "prepared_cover_letter_material_hash": (
                self.prepared_cover_letter_material_hash
            ),
            "prepared_cover_letter_material_id": (
                self.prepared_cover_letter_material_id
            ),
            "prepared_resume_material_hash": self.prepared_resume_material_hash,
            "prepared_resume_material_id": self.prepared_resume_material_id,
            "resume_entry_hash": self.resume_entry_hash,
            "resume_entry_id": self.resume_entry_id,
            "subject_id": self.subject_id,
            "taxonomy_hash": self.taxonomy_hash,
            "taxonomy_version": self.taxonomy_version,
        }
        if self.contract_version == APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION:
            identity.update(
                {
                    "execution_context_binding_hash": (
                        self.execution_context_binding_hash
                    ),
                    "execution_context_contract_version": (
                        self.execution_context_contract_version
                    ),
                    "execution_policy_record_hash": (
                        self.execution_policy_record_hash
                    ),
                    "execution_policy_record_id": (
                        self.execution_policy_record_id
                    ),
                    "execution_policy_record_version": (
                        self.execution_policy_record_version
                    ),
                    "verified_profile_hash": self.verified_profile_hash,
                    "verified_profile_id": self.verified_profile_id,
                    "verified_profile_version": self.verified_profile_version,
                }
            )
        return identity

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "record_id": self.record_id,
            "assembled_at": _rfc3339(self.assembled_at),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "record_content_hash": self.record_content_hash,
        }


@dataclass(frozen=True, slots=True)
class ApplicationBundleAssemblyReadResult:
    status: ApplicationBundleAssemblyReadStatus
    record: ApplicationBundleAssemblyRecord | None


@dataclass(frozen=True, slots=True)
class ApplicationBundleAssemblyWriteResult:
    status: ApplicationBundleAssemblyWriteStatus
    record: ApplicationBundleAssemblyRecord | None
    reason_code: ApplicationBundleAssemblyFailureReason | None = None


@dataclass(frozen=True, slots=True)
class ApplicationBundleAssemblyListResult:
    status: ApplicationBundleAssemblyListStatus
    records: tuple[ApplicationBundleAssemblyRecord, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", ApplicationBundleAssemblyListStatus(self.status)
        )
        if not isinstance(self.records, tuple) or any(
            not isinstance(item, ApplicationBundleAssemblyRecord)
            for item in self.records
        ):
            raise TypeError("listed assembly records must be typed")
        if self.status is ApplicationBundleAssemblyListStatus.INTEGRITY_FAILURE:
            if self.records:
                raise ValueError("failed assembly list cannot expose records")
            return
        expected = tuple(
            sorted(
                self.records,
                key=lambda item: (
                    item.application_plan_id,
                    item.assembled_at.astimezone(timezone.utc),
                    item.record_id,
                ),
            )
        )
        if self.records != expected or len(
            {item.record_id for item in self.records}
        ) != len(self.records):
            raise ValueError("assembly list ordering is invalid")
        if self.records and len(
            {item.subject_id for item in self.records}
        ) != 1:
            raise ValueError("assembly list mixes subjects")


@runtime_checkable
class ApplicationBundleAssemblyRepository(Protocol):
    def save(
        self, record: ApplicationBundleAssemblyRecord
    ) -> ApplicationBundleAssemblyWriteResult: ...

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ApplicationBundleAssemblyReadResult: ...

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> ApplicationBundleAssemblyReadResult: ...

    def list_for_subject(
        self, *, subject_id: str
    ) -> ApplicationBundleAssemblyListResult: ...


def _record_from_dict(value: Any) -> ApplicationBundleAssemblyRecord:
    expected_v1 = {
        "answer_set_content_hash",
        "answer_set_id",
        "application_bundle_canonical_hash",
        "application_bundle_contract_version",
        "application_bundle_run_id",
        "application_plan_id",
        "assembled_at",
        "contract_version",
        "cover_letter_entry_hash",
        "cover_letter_entry_id",
        "job_content_hash",
        "job_id",
        "job_revision",
        "manifest_content_hash",
        "manifest_id",
        "prepared_cover_letter_material_hash",
        "prepared_cover_letter_material_id",
        "prepared_resume_material_hash",
        "prepared_resume_material_id",
        "record_content_hash",
        "record_id",
        "resume_entry_hash",
        "resume_entry_id",
        "subject_id",
        "taxonomy_hash",
        "taxonomy_version",
    }
    expected_v2 = expected_v1 | {
        "execution_context_binding_hash",
        "execution_context_contract_version",
        "execution_policy_record_hash",
        "execution_policy_record_id",
        "execution_policy_record_version",
        "verified_profile_hash",
        "verified_profile_id",
        "verified_profile_version",
    }
    if not isinstance(value, Mapping):
        raise ValueError("persisted assembly record fields are invalid")
    if (
        value.get("contract_version")
        == APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION_V1
        and set(value) == expected_v1
    ):
        context = {}
    elif (
        value.get("contract_version")
        == APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION
        and set(value) == expected_v2
    ):
        context = {
            name: value[name] for name in expected_v2 - expected_v1
        }
    else:
        raise ValueError("persisted assembly record fields are invalid")
    return ApplicationBundleAssemblyRecord(
        **{
            **{name: value[name] for name in expected_v1},
            **context,
            "assembled_at": _parse_time(value["assembled_at"]),
        }
    )


class PrivateHomeApplicationBundleAssemblyRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.application_bundle_assemblies
            / _subject_key(_clean("subject_id", subject_id, maximum=160))
        )

    def _path(self, subject_id: str, record_id: str) -> Path:
        if (
            not isinstance(record_id, str)
            or _RECORD_ID_RE.fullmatch(record_id) is None
        ):
            raise ValueError("record_id is invalid")
        return self._directory(subject_id) / f"{record_id}.json"

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ApplicationBundleAssemblyReadResult:
        path = self._path(subject_id, record_id)
        with self._lock:
            if not path.exists():
                return ApplicationBundleAssemblyReadResult(
                    ApplicationBundleAssemblyReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return ApplicationBundleAssemblyReadResult(
                    ApplicationBundleAssemblyReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            try:
                record = _record_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return ApplicationBundleAssemblyReadResult(
                    ApplicationBundleAssemblyReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            if (
                record.subject_id != subject_id.strip()
                or record.record_id != record_id
            ):
                return ApplicationBundleAssemblyReadResult(
                    ApplicationBundleAssemblyReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            return ApplicationBundleAssemblyReadResult(
                ApplicationBundleAssemblyReadStatus.FOUND, record
            )

    def save(
        self, record: ApplicationBundleAssemblyRecord
    ) -> ApplicationBundleAssemblyWriteResult:
        if not isinstance(record, ApplicationBundleAssemblyRecord):
            raise TypeError("record must be an ApplicationBundleAssemblyRecord")
        path = self._path(record.subject_id, record.record_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            record.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ApplicationBundleAssemblyWriteResult(
                    ApplicationBundleAssemblyWriteStatus.FAILED,
                    None,
                    ApplicationBundleAssemblyFailureReason.PERSISTENCE_FAILED,
                )
            if created:
                return ApplicationBundleAssemblyWriteResult(
                    ApplicationBundleAssemblyWriteStatus.CREATED, record
                )
            existing = self.get(
                subject_id=record.subject_id, record_id=record.record_id
            )
            if (
                existing.status is ApplicationBundleAssemblyReadStatus.FOUND
                and existing.record is not None
                and existing.record.identity_dict() == record.identity_dict()
            ):
                return ApplicationBundleAssemblyWriteResult(
                    ApplicationBundleAssemblyWriteStatus.UNCHANGED,
                    existing.record,
                )
            return ApplicationBundleAssemblyWriteResult(
                ApplicationBundleAssemblyWriteStatus.FAILED,
                None,
                ApplicationBundleAssemblyFailureReason.RECORD_INTEGRITY_FAILURE,
            )

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> ApplicationBundleAssemblyReadResult:
        listed = self.list_for_subject(subject_id=subject_id)
        if (
            listed.status
            is ApplicationBundleAssemblyListStatus.INTEGRITY_FAILURE
        ):
            return ApplicationBundleAssemblyReadResult(
                ApplicationBundleAssemblyReadStatus.INTEGRITY_FAILURE, None
            )
        records = [
            item
            for item in listed.records
            if item.application_plan_id == application_plan_id
        ]
        if not records:
            return ApplicationBundleAssemblyReadResult(
                ApplicationBundleAssemblyReadStatus.NOT_FOUND, None
            )
        current = max(
            records,
            key=lambda item: (
                item.assembled_at.astimezone(timezone.utc),
                item.record_id,
            ),
        )
        return ApplicationBundleAssemblyReadResult(
            ApplicationBundleAssemblyReadStatus.FOUND, current
        )

    def list_for_subject(
        self, *, subject_id: str
    ) -> ApplicationBundleAssemblyListResult:
        directory = self._directory(subject_id)
        if not directory.exists():
            return ApplicationBundleAssemblyListResult(
                ApplicationBundleAssemblyListStatus.SUCCEEDED, ()
            )
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            return ApplicationBundleAssemblyListResult(
                ApplicationBundleAssemblyListStatus.INTEGRITY_FAILURE, ()
            )
        records: list[ApplicationBundleAssemblyRecord] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or _RECORD_ID_RE.fullmatch(path.stem) is None
            ):
                return ApplicationBundleAssemblyListResult(
                    ApplicationBundleAssemblyListStatus.INTEGRITY_FAILURE,
                    (),
                )
            read = self.get(subject_id=subject_id, record_id=path.stem)
            if (
                read.status is not ApplicationBundleAssemblyReadStatus.FOUND
                or read.record is None
            ):
                return ApplicationBundleAssemblyListResult(
                    ApplicationBundleAssemblyListStatus.INTEGRITY_FAILURE,
                    (),
                )
            records.append(read.record)
        return ApplicationBundleAssemblyListResult(
            ApplicationBundleAssemblyListStatus.SUCCEEDED,
            tuple(
                sorted(
                    records,
                    key=lambda item: (
                        item.application_plan_id,
                        item.assembled_at.astimezone(timezone.utc),
                        item.record_id,
                    ),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class AssembleApplicationBundleCommand:
    subject_id: str
    application_plan_id: str
    plan_material_manifest_id: str
    prepared_application_answer_set_id: str
    now: datetime
    verified_profile_id: str | None = None
    verified_profile_version: str | None = None
    verified_profile_hash: str | None = None
    execution_policy_record_id: str | None = None
    execution_policy_record_version: str | None = None
    execution_policy_record_hash: str | None = None
    execution_context_binding_hash: str | None = None


@dataclass(frozen=True, slots=True)
class AssembleApplicationBundleResult:
    status: ApplicationBundleAssemblyStatus
    record: ApplicationBundleAssemblyRecord | None
    bundle: ApplicationBundle | None
    not_ready_reason: ApplicationBundleAssemblyNotReadyReason | None
    failure_reason: ApplicationBundleAssemblyFailureReason | None
    retryable: bool
    message: str


def _failed(
    reason: ApplicationBundleAssemblyFailureReason,
    message: str,
    *,
    retryable: bool = False,
) -> AssembleApplicationBundleResult:
    return AssembleApplicationBundleResult(
        ApplicationBundleAssemblyStatus.FAILED,
        None,
        None,
        None,
        reason,
        retryable,
        message,
    )


def _not_ready(
    reason: ApplicationBundleAssemblyNotReadyReason, message: str
) -> AssembleApplicationBundleResult:
    return AssembleApplicationBundleResult(
        ApplicationBundleAssemblyStatus.NOT_READY,
        None,
        None,
        reason,
        None,
        False,
        message,
    )


def _validate_artifact(
    home: PrivateHome, subject_id: str, entry: PlanMaterialEntry
) -> tuple[Path, bytes] | None:
    if _subject_key(subject_id) not in PurePosixPath(
        entry.artifact_reference
    ).parts:
        return None
    try:
        path = home.contained_path(entry.artifact_reference)
        if path.is_symlink() or not path.is_file():
            return None
        content = path.read_bytes()
    except (OSError, PrivateHomeError):
        return None
    if (
        entry.artifact_byte_size is None
        or len(content) != entry.artifact_byte_size
        or hashlib.sha256(content).hexdigest() != entry.artifact_sha256
        or not content.startswith(b"%PDF-")
        or pdf_page_count(content) != entry.page_count
    ):
        return None
    return path, content


def _record(
    *,
    plan: ApplicationPlan,
    manifest: PlanMaterialManifest,
    answer_set: PreparedApplicationAnswerSet,
    resume: PlanMaterialEntry,
    cover: PlanMaterialEntry,
    bundle: ApplicationBundle,
    context: ApplicationAssemblyExecutionContext,
    now: datetime,
) -> ApplicationBundleAssemblyRecord:
    values = {
        "contract_version": APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION,
        "subject_id": plan.subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "manifest_id": manifest.manifest_id,
        "manifest_content_hash": manifest.manifest_content_hash,
        "answer_set_id": answer_set.answer_set_id,
        "answer_set_content_hash": answer_set.answer_set_content_hash,
        "resume_entry_id": resume.entry_id,
        "resume_entry_hash": _entry_hash(resume),
        "cover_letter_entry_id": cover.entry_id,
        "cover_letter_entry_hash": _entry_hash(cover),
        "prepared_resume_material_id": manifest.prepared_resume_material_id,
        "prepared_resume_material_hash": manifest.prepared_resume_material_hash,
        "prepared_cover_letter_material_id": (
            manifest.prepared_cover_letter_material_id
        ),
        "prepared_cover_letter_material_hash": (
            manifest.prepared_cover_letter_material_hash
        ),
        "taxonomy_version": answer_set.taxonomy_version,
        "taxonomy_hash": answer_set.taxonomy_hash,
        "application_bundle_contract_version": (
            APPLICATION_BUNDLE_CONTRACT_VERSION
        ),
        "application_bundle_run_id": bundle.run_id,
        "application_bundle_canonical_hash": (
            application_bundle_canonical_hash(bundle)
        ),
        "execution_context_contract_version": (
            context.context_contract_version
        ),
        "execution_context_binding_hash": context.context_binding_hash,
        "verified_profile_id": context.verified_profile_id,
        "verified_profile_version": context.verified_profile_version,
        "verified_profile_hash": context.verified_profile_hash,
        "execution_policy_record_id": context.execution_policy_record_id,
        "execution_policy_record_version": (
            context.execution_policy_record_version
        ),
        "execution_policy_record_hash": (
            context.execution_policy_record_hash
        ),
    }
    record_id = "application-bundle-assembly-" + _hash(values)
    content = {**values, "record_id": record_id, "assembled_at": _rfc3339(now)}
    return ApplicationBundleAssemblyRecord(
        record_id=record_id,
        record_content_hash=_hash(content),
        assembled_at=now,
        **values,
    )


def assemble_application_bundle(
    command: AssembleApplicationBundleCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    job_posting_repository: JobPostingReadRepository,
    plan_material_manifest_repository: PlanMaterialManifestRepository,
    answer_set_repository: PreparedApplicationAnswerSetRepository,
    verified_execution_profile_provider: VerifiedExecutionProfileProvider,
    plan_execution_policy_provider: PlanExecutionPolicyProvider,
    bundle_factory: ApplicationBundleFactory,
    assembly_repository: ApplicationBundleAssemblyRepository,
    bundle_envelope_repository: (
        RecoverableApplicationBundleEnvelopeRepository
    ),
    private_home: PrivateHome,
) -> AssembleApplicationBundleResult:
    """Validate prepared inputs and create one existing execution bundle."""

    try:
        subject_id = _clean("subject_id", command.subject_id, maximum=160)
        plan_id = _clean(
            "application_plan_id", command.application_plan_id, maximum=180
        )
        manifest_id = _clean(
            "plan_material_manifest_id",
            command.plan_material_manifest_id,
        )
        answer_set_id = _clean(
            "prepared_application_answer_set_id",
            command.prepared_application_answer_set_id,
        )
        now = _aware("now", command.now)
        verified_profile_id = _clean(
            "verified_profile_id", command.verified_profile_id
        )
        verified_profile_version = _clean(
            "verified_profile_version", command.verified_profile_version
        )
        verified_profile_hash = _require_hash(
            "verified_profile_hash", command.verified_profile_hash
        )
        execution_policy_record_id = _clean(
            "execution_policy_record_id",
            command.execution_policy_record_id,
        )
        execution_policy_record_version = _clean(
            "execution_policy_record_version",
            command.execution_policy_record_version,
        )
        execution_policy_record_hash = _require_hash(
            "execution_policy_record_hash",
            command.execution_policy_record_hash,
        )
        execution_context_binding_hash = _require_hash(
            "execution_context_binding_hash",
            command.execution_context_binding_hash,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return _failed(
            ApplicationBundleAssemblyFailureReason.INVALID_REQUEST, str(exc)
        )
    if (
        verified_profile_version
        != VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION
        or execution_policy_record_version
        != PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .EXECUTION_CONTEXT_VERSION_INCOMPATIBLE,
            "execution context contract version is unsupported",
        )

    try:
        plan_read = application_plan_repository.get(plan_id)
    except Exception:
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
            "ApplicationPlan could not be read safely",
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _failed(
            ApplicationBundleAssemblyFailureReason.APPLICATION_PLAN_NOT_FOUND,
            "ApplicationPlan was not found",
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or plan_read.plan is None
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE,
            "ApplicationPlan failed integrity validation",
        )
    plan = plan_read.plan
    if plan.subject_id != subject_id:
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH,
            "ApplicationPlan belongs to another subject",
        )

    try:
        posting = job_posting_repository.get(plan.job_id)
    except (JobPostingRepositoryError, OSError, TypeError, ValueError):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .JOB_POSTING_INTEGRITY_FAILURE,
            "JobPosting could not be read safely",
        )
    if posting is None:
        return _failed(
            ApplicationBundleAssemblyFailureReason.JOB_POSTING_NOT_FOUND,
            "JobPosting was not found",
        )
    if (
        posting.job_id != plan.job_id
        or posting.revision != plan.job_revision
        or posting.content_hash != plan.job_content_hash
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .JOB_POSTING_BINDING_MISMATCH,
            "JobPosting does not match the ApplicationPlan",
        )

    try:
        manifest_read = plan_material_manifest_repository.get(
            subject_id=subject_id, manifest_id=manifest_id
        )
    except Exception:
        return _failed(
            ApplicationBundleAssemblyFailureReason.MANIFEST_INTEGRITY_FAILURE,
            "PlanMaterialManifest could not be read safely",
        )
    if manifest_read.status is PlanMaterialManifestReadStatus.NOT_FOUND:
        return _not_ready(
            ApplicationBundleAssemblyNotReadyReason.MANIFEST_NOT_FOUND,
            "PlanMaterialManifest is not available",
        )
    if (
        manifest_read.status is not PlanMaterialManifestReadStatus.FOUND
        or manifest_read.manifest is None
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason.MANIFEST_INTEGRITY_FAILURE,
            "PlanMaterialManifest failed integrity validation",
        )
    manifest = manifest_read.manifest
    if manifest.contract_version != PLAN_MATERIAL_MANIFEST_CONTRACT_VERSION:
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .MANIFEST_VERSION_INCOMPATIBLE,
            "execution handoff requires the v2 material manifest",
        )
    if (
        manifest.subject_id != subject_id
        or manifest.application_plan_id != plan.plan_id
        or manifest.job_id != plan.job_id
        or manifest.job_revision != plan.job_revision
        or manifest.job_content_hash != plan.job_content_hash
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason.MANIFEST_BINDING_MISMATCH,
            "PlanMaterialManifest does not match the ApplicationPlan",
        )
    expected_roles = (
        PlanMaterialRole.RESUME,
        PlanMaterialRole.COVER_LETTER,
    )
    if (
        manifest.assembly_state
        is not PlanMaterialAssemblyState.RESUME_AND_COVER_LETTER
        or manifest.included_roles != expected_roles
        or len(manifest.entries) != 2
        or tuple(item.material_role for item in manifest.entries)
        != expected_roles
    ):
        return _not_ready(
            ApplicationBundleAssemblyNotReadyReason
            .REQUIRED_MATERIALS_INCOMPLETE,
            "manifest must contain exactly Resume and Cover Letter",
        )

    try:
        answer_read = answer_set_repository.get(
            subject_id=subject_id, answer_set_id=answer_set_id
        )
    except Exception:
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .ANSWER_SET_INTEGRITY_FAILURE,
            "PreparedApplicationAnswerSet could not be read safely",
        )
    if answer_read.status is PreparedApplicationAnswerSetReadStatus.NOT_FOUND:
        return _not_ready(
            ApplicationBundleAssemblyNotReadyReason.ANSWER_SET_NOT_FOUND,
            "PreparedApplicationAnswerSet is not available",
        )
    if (
        answer_read.status
        is not PreparedApplicationAnswerSetReadStatus.FOUND
        or answer_read.answer_set is None
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .ANSWER_SET_INTEGRITY_FAILURE,
            "PreparedApplicationAnswerSet failed integrity validation",
        )
    answer_set = answer_read.answer_set
    if (
        answer_set.subject_id != subject_id
        or answer_set.application_plan_id != plan.plan_id
        or answer_set.job_id != plan.job_id
        or answer_set.job_revision != plan.job_revision
        or answer_set.job_content_hash != plan.job_content_hash
        or answer_set.plan_instructions_hash
        != plan.user_preparation_instructions_hash
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .ANSWER_SET_BINDING_MISMATCH,
            "PreparedApplicationAnswerSet does not match the ApplicationPlan",
        )
    if (
        answer_set.taxonomy_version
        != CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
        or answer_set.taxonomy_hash
        != canonical_application_answer_taxonomy_hash()
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .TAXONOMY_BINDING_MISMATCH,
            "PreparedApplicationAnswerSet taxonomy does not match execution",
        )
    if any(item.blocking for item in answer_set.unresolved_items):
        return _not_ready(
            ApplicationBundleAssemblyNotReadyReason
            .BLOCKING_UNRESOLVED_ANSWERS,
            "blocking canonical answers still require human attention",
        )

    resume, cover = manifest.entries
    resume_artifact = _validate_artifact(private_home, subject_id, resume)
    cover_artifact = _validate_artifact(private_home, subject_id, cover)
    if resume_artifact is None or cover_artifact is None:
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .ARTIFACT_INTEGRITY_FAILURE,
            "a managed PDF failed location, hash, size, signature, or page validation",
        )
    resume_path, _ = resume_artifact
    answers = CanonicalApplicationAnswers.from_mapping(
        {item.canonical_key: item.value for item in answer_set.answers}
    )
    metadata = {
        "answer_set_content_hash": answer_set.answer_set_content_hash,
        "answer_set_id": answer_set.answer_set_id,
        "application_plan_id": plan.plan_id,
        "cover_letter_entry_id": cover.entry_id,
        "manifest_content_hash": manifest.manifest_content_hash,
        "manifest_id": manifest.manifest_id,
        "prepared_cover_letter_material_id": (
            manifest.prepared_cover_letter_material_id
        ),
        "prepared_resume_material_id": manifest.prepared_resume_material_id,
        "resume_entry_id": resume.entry_id,
        "source": "plan-scoped-application-bundle-assembly",
    }
    try:
        materials = MaterialBundle.build(
            resume_path=resume_path,
            metadata=metadata,
            cover_letter_pdf=ManagedArtifactReference(
                reference=cover.artifact_reference,
                sha256=cover.artifact_sha256,
                byte_size=cover.artifact_byte_size,
                media_type=cover.media_type,
            ),
        )
    except (OSError, TypeError, ValueError):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .ARTIFACT_INTEGRITY_FAILURE,
            "managed materials could not be represented safely",
        )

    # All preparation bytes are validated before replay, but exact context
    # IDs/hashes come from the command so a completed v2 assembly can return
    # without consulting either context provider or the Factory.
    try:
        listed = assembly_repository.list_for_subject(
            subject_id=subject_id
        )
    except Exception:
        return _failed(
            ApplicationBundleAssemblyFailureReason.RECORD_INTEGRITY_FAILURE,
            "assembly history could not be read safely",
        )
    if (
        listed.status
        is ApplicationBundleAssemblyListStatus.INTEGRITY_FAILURE
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason.RECORD_INTEGRITY_FAILURE,
            "assembly history failed integrity validation",
        )
    replay_records = tuple(
        record
        for record in listed.records
        if (
            record.contract_version
            == APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION
            and record.subject_id == subject_id
            and record.application_plan_id == plan.plan_id
            and record.job_id == plan.job_id
            and record.job_revision == plan.job_revision
            and record.job_content_hash == plan.job_content_hash
            and record.manifest_id == manifest.manifest_id
            and record.manifest_content_hash
            == manifest.manifest_content_hash
            and record.answer_set_id == answer_set.answer_set_id
            and record.answer_set_content_hash
            == answer_set.answer_set_content_hash
            and record.resume_entry_id == resume.entry_id
            and record.resume_entry_hash == _entry_hash(resume)
            and record.cover_letter_entry_id == cover.entry_id
            and record.cover_letter_entry_hash == _entry_hash(cover)
            and record.verified_profile_id == verified_profile_id
            and record.verified_profile_version
            == verified_profile_version
            and record.verified_profile_hash == verified_profile_hash
            and record.execution_policy_record_id
            == execution_policy_record_id
            and record.execution_policy_record_version
            == execution_policy_record_version
            and record.execution_policy_record_hash
            == execution_policy_record_hash
            and record.execution_context_binding_hash
            == execution_context_binding_hash
        )
    )
    if len(replay_records) > 1:
        return _failed(
            ApplicationBundleAssemblyFailureReason.RECORD_INTEGRITY_FAILURE,
            "multiple identical assembly bindings exist",
        )
    if replay_records:
        existing_record = replay_records[0]
        try:
            envelope_read = bundle_envelope_repository.get_for_assembly(
                subject_id=subject_id,
                assembly_record_id=existing_record.record_id,
            )
        except Exception:
            envelope_read = None
        if (
            envelope_read is None
            or envelope_read.status
            is not RecoverableApplicationBundleEnvelopeReadStatus.FOUND
            or envelope_read.envelope is None
            or envelope_read.envelope.application_plan_id != plan.plan_id
            or envelope_read.envelope.assembly_record_content_hash
            != existing_record.record_content_hash
            or envelope_read.envelope.bundle_canonical_hash
            != existing_record.application_bundle_canonical_hash
        ):
            return _failed(
                ApplicationBundleAssemblyFailureReason
                .BUNDLE_ENVELOPE_PERSISTENCE_FAILED,
                "existing assembly has no valid recoverable bundle envelope",
            )
        return AssembleApplicationBundleResult(
            ApplicationBundleAssemblyStatus.UNCHANGED,
            existing_record,
            envelope_read.envelope.bundle,
            None,
            None,
            False,
            "identical execution-context assembly already exists",
        )

    context_result = load_application_assembly_execution_context(
        LoadApplicationAssemblyExecutionContextCommand(
            subject_id=subject_id,
            application_plan=plan,
            job_id=posting.job_id,
            verified_profile_id=verified_profile_id,
            verified_profile_hash=verified_profile_hash,
            execution_policy_record_id=execution_policy_record_id,
            execution_policy_record_hash=execution_policy_record_hash,
        ),
        verified_profile_provider=verified_execution_profile_provider,
        execution_policy_provider=plan_execution_policy_provider,
    )
    if (
        context_result.status
        is LoadApplicationAssemblyExecutionContextStatus.NOT_READY
    ):
        return _not_ready(
            ApplicationBundleAssemblyNotReadyReason
            .VERIFIED_PROFILE_NOT_READY
            if context_result.failure_reason
            is ApplicationAssemblyExecutionContextFailureReason
            .PROFILE_NOT_FOUND
            else ApplicationBundleAssemblyNotReadyReason
            .EXECUTION_POLICY_NOT_READY,
            "required execution context is not ready",
        )
    if (
        context_result.status
        is LoadApplicationAssemblyExecutionContextStatus.CONFLICT
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .VERIFIED_PROFILE_CONFLICT
            if context_result.failure_reason
            is ApplicationAssemblyExecutionContextFailureReason
            .PROFILE_CONFLICT
            else ApplicationBundleAssemblyFailureReason
            .EXECUTION_POLICY_CONFLICT,
            "execution context has conflicting immutable records",
        )
    if (
        context_result.status
        is LoadApplicationAssemblyExecutionContextStatus.FAILED
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .EXECUTION_CONTEXT_PROVIDER_FAILED,
            "execution context provider failed",
            retryable=True,
        )
    if (
        context_result.status
        is not LoadApplicationAssemblyExecutionContextStatus.READY
        or context_result.context is None
    ):
        reason = context_result.failure_reason
        if reason is (
            ApplicationAssemblyExecutionContextFailureReason
            .PROFILE_INTEGRITY_FAILURE
        ):
            failure = (
                ApplicationBundleAssemblyFailureReason
                .VERIFIED_PROFILE_INTEGRITY_FAILURE
            )
        elif reason is (
            ApplicationAssemblyExecutionContextFailureReason
            .POLICY_INTEGRITY_FAILURE
        ):
            failure = (
                ApplicationBundleAssemblyFailureReason
                .EXECUTION_POLICY_INTEGRITY_FAILURE
            )
        elif reason is (
            ApplicationAssemblyExecutionContextFailureReason
            .UNSUPPORTED_CONTRACT_VERSION
        ):
            failure = (
                ApplicationBundleAssemblyFailureReason
                .EXECUTION_CONTEXT_VERSION_INCOMPATIBLE
            )
        else:
            failure = (
                ApplicationBundleAssemblyFailureReason
                .EXECUTION_CONTEXT_BINDING_MISMATCH
            )
        return _failed(
            failure,
            "execution context failed exact binding validation",
        )
    context = context_result.context
    if context.context_binding_hash != execution_context_binding_hash:
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .EXECUTION_CONTEXT_BINDING_MISMATCH,
            "execution context binding hash does not match the command",
        )
    run_binding = _hash(
        {
            "answer_set_content_hash": answer_set.answer_set_content_hash,
            "application_bundle_contract_version": (
                APPLICATION_BUNDLE_CONTRACT_VERSION
            ),
            "application_plan_id": plan.plan_id,
            "assembly_contract_version": (
                APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION
            ),
            "job_content_hash": plan.job_content_hash,
            "manifest_content_hash": manifest.manifest_content_hash,
            "taxonomy_hash": answer_set.taxonomy_hash,
            "execution_context_binding_hash": context.context_binding_hash,
        }
    )
    run_id = "prepared-application-bundle-" + run_binding
    request = ApplicationBundleFactoryRequest(
        run_id=run_id,
        subject_id=subject_id,
        application_plan=plan,
        job_posting=posting,
        materials=materials,
        answers=answers,
        identity_profile=context.identity_profile,
        policy_decision=context.policy_decision,
        verified_profile_ref=context.verified_profile,
        execution_policy_ref=context.execution_policy_record,
        execution_context_binding_hash=context.context_binding_hash,
    )
    try:
        bundle = bundle_factory.create(request)
    except Exception:
        return _failed(
            ApplicationBundleAssemblyFailureReason.BUNDLE_FACTORY_FAILURE,
            "ApplicationBundle factory failed",
        )
    try:
        expected_url = normalized_job_url(
            posting.application_url or posting.source_url
        )
    except ValueError:
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .JOB_POSTING_INTEGRITY_FAILURE,
            "JobPosting execution URL is invalid",
        )
    if (
        not isinstance(bundle, ApplicationBundle)
        or bundle.run_id != run_id
        or bundle.job.job_id != plan.job_id
        or bundle.job.url != expected_url
        or bundle.job.company != posting.company
        or bundle.job.title != posting.title
        or bundle.materials != materials
        or not isinstance(bundle.answers, CanonicalApplicationAnswers)
        or bundle.answers != answers
        or dict(bundle.profile)
        != dict(context.identity_profile.to_application_bundle_profile())
        or bundle.policy != context.policy_decision
    ):
        return _failed(
            ApplicationBundleAssemblyFailureReason
            .BUNDLE_CONTRACT_MISMATCH,
            "ApplicationBundle factory changed prepared inputs",
        )

    record = _record(
        plan=plan,
        manifest=manifest,
        answer_set=answer_set,
        resume=resume,
        cover=cover,
        bundle=bundle,
        context=context,
        now=now,
    )
    try:
        write = assembly_repository.save(record)
    except Exception:
        return _failed(
            ApplicationBundleAssemblyFailureReason.PERSISTENCE_FAILED,
            "assembly record could not be persisted",
            retryable=True,
        )
    if (
        write.status is ApplicationBundleAssemblyWriteStatus.CREATED
        and write.record is not None
    ):
        persisted_record = write.record
        try:
            envelope = create_recoverable_application_bundle_envelope(
                subject_id=subject_id,
                application_plan_id=plan.plan_id,
                assembly_record=persisted_record,
                bundle=bundle,
                home=private_home,
                created_at=persisted_record.assembled_at,
            )
            envelope_write = bundle_envelope_repository.save(envelope)
        except Exception:
            return _failed(
                ApplicationBundleAssemblyFailureReason
                .BUNDLE_ENVELOPE_PERSISTENCE_FAILED,
                "recoverable ApplicationBundle envelope could not be persisted",
                retryable=True,
            )
        if (
            envelope_write.status
            is RecoverableApplicationBundleEnvelopeWriteStatus.FAILED
            or envelope_write.envelope is None
        ):
            return _failed(
                ApplicationBundleAssemblyFailureReason
                .BUNDLE_ENVELOPE_PERSISTENCE_FAILED,
                "recoverable ApplicationBundle envelope persistence failed closed",
                retryable=True,
            )
    elif (
        write.status is ApplicationBundleAssemblyWriteStatus.UNCHANGED
        and write.record is not None
    ):
        persisted_record = write.record
        try:
            envelope_read = (
                bundle_envelope_repository.get_for_assembly(
                    subject_id=subject_id,
                    assembly_record_id=persisted_record.record_id,
                )
            )
        except Exception:
            envelope_read = None
        if (
            envelope_read is None
            or envelope_read.status
            is not RecoverableApplicationBundleEnvelopeReadStatus.FOUND
            or envelope_read.envelope is None
            or envelope_read.envelope.application_plan_id != plan.plan_id
            or envelope_read.envelope.assembly_record_content_hash
            != persisted_record.record_content_hash
            or envelope_read.envelope.bundle_canonical_hash
            != persisted_record.application_bundle_canonical_hash
        ):
            return _failed(
                ApplicationBundleAssemblyFailureReason
                .BUNDLE_ENVELOPE_PERSISTENCE_FAILED,
                "existing assembly has no valid recoverable bundle envelope",
                retryable=False,
            )
    if (
        write.status is ApplicationBundleAssemblyWriteStatus.CREATED
        and write.record is not None
    ):
        return AssembleApplicationBundleResult(
            ApplicationBundleAssemblyStatus.CREATED,
            write.record,
            bundle,
            None,
            None,
            False,
            "execution-compatible ApplicationBundle assembled",
        )
    if (
        write.status is ApplicationBundleAssemblyWriteStatus.UNCHANGED
        and write.record is not None
    ):
        return AssembleApplicationBundleResult(
            ApplicationBundleAssemblyStatus.UNCHANGED,
            write.record,
            bundle,
            None,
            None,
            False,
            "identical assembly binding already exists",
        )
    return _failed(
        write.reason_code
        or ApplicationBundleAssemblyFailureReason.PERSISTENCE_FAILED,
        "assembly record persistence failed closed",
        retryable=(
            write.reason_code
            is ApplicationBundleAssemblyFailureReason.PERSISTENCE_FAILED
        ),
    )


__all__ = [
    "APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION",
    "APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION_V1",
    "ApplicationBundleAssemblyFailureReason",
    "ApplicationBundleAssemblyListResult",
    "ApplicationBundleAssemblyListStatus",
    "ApplicationBundleAssemblyNotReadyReason",
    "ApplicationBundleAssemblyReadResult",
    "ApplicationBundleAssemblyReadStatus",
    "ApplicationBundleAssemblyRecord",
    "ApplicationBundleAssemblyRepository",
    "ApplicationBundleAssemblyStatus",
    "ApplicationBundleAssemblyWriteResult",
    "ApplicationBundleAssemblyWriteStatus",
    "ApplicationBundleFactory",
    "ApplicationBundleFactoryRequest",
    "AssembleApplicationBundleCommand",
    "AssembleApplicationBundleResult",
    "PrivateHomeApplicationBundleAssemblyRepository",
    "assemble_application_bundle",
]
