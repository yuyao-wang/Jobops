"""Immutable source lineage for stopped Resume Compilation attempts."""

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

from .application_preparation_orchestrator import (
    ApplicationPreparationStage,
    CompilationSourceResolutionLineage,
    LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION,
    LatexCompilationStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    RESUME_COMPILATION_STOPPED_SOURCE_CONTRACT_VERSION,
    ResolvedCompilationSourceLineage,
    ResumeCompilationStoppedSourceRef,
    UnresolvedCompilationSourceLineage,
)
from .private_home import PrivateHome, PrivateHomeError
from .preparation_invocation import PreparationInvocationBindingRef


_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_RECORD_ID_RE = re.compile(
    r"^resume-compilation-stopped-source-[a-f0-9]{64}$"
)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _clean(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("created_at must be a string")
    return _aware(
        "created_at", datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ResumeCompilationStoppedSourceRecord:
    record_id: str
    record_version: str
    record_hash: str
    subject_id: str
    application_plan_id: str
    preparation_invocation_ref: PreparationInvocationBindingRef
    compilation_attempt_id: str
    compilation_stage: ApplicationPreparationStage
    outcome: PreparationStageOutcome
    stop_reason: PreparationStopReasonEnvelope
    source_resolution_lineage: CompilationSourceResolutionLineage
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            self.record_version
            != RESUME_COMPILATION_STOPPED_SOURCE_CONTRACT_VERSION
        ):
            raise ValueError(
                "compilation stopped-source contract is unsupported"
            )
        if (
            not isinstance(self.record_id, str)
            or _RECORD_ID_RE.fullmatch(self.record_id) is None
        ):
            raise ValueError("compilation stopped-source ID is invalid")
        _hash("record_hash", self.record_hash)
        subject_id = _clean("subject_id", self.subject_id, 160)
        plan_id = _clean(
            "application_plan_id", self.application_plan_id, 180
        )
        _clean("compilation_attempt_id", self.compilation_attempt_id, 240)
        if not isinstance(
            self.preparation_invocation_ref,
            PreparationInvocationBindingRef,
        ):
            raise TypeError("preparation invocation reference must be typed")
        stage = ApplicationPreparationStage(self.compilation_stage)
        outcome = PreparationStageOutcome(self.outcome)
        object.__setattr__(self, "compilation_stage", stage)
        object.__setattr__(self, "outcome", outcome)
        if stage is not ApplicationPreparationStage.RESUME_COMPILATION:
            raise ValueError("stopped-source stage must be Resume Compilation")
        if outcome not in {
            PreparationStageOutcome.DEFERRED,
            PreparationStageOutcome.FAILED,
        }:
            raise ValueError("stopped-source outcome must stop compilation")
        if (
            not isinstance(self.stop_reason, PreparationStopReasonEnvelope)
            or self.stop_reason.stage is not stage
            or self.stop_reason.outcome is not outcome
            or type(self.stop_reason.code) is not LatexCompilationStopReason
            or self.stop_reason.contract_version
            != LATEX_COMPILATION_STOP_REASON_CONTRACT_VERSION
        ):
            raise ValueError("stopped-source reason is invalid")
        if not isinstance(
            self.source_resolution_lineage,
            (
                ResolvedCompilationSourceLineage,
                UnresolvedCompilationSourceLineage,
            ),
        ):
            raise TypeError("source-resolution lineage must be typed")
        lineage = self.source_resolution_lineage
        if (
            lineage.invocation_binding_ref
            != self.preparation_invocation_ref
            or lineage.compilation_attempt_id
            != self.compilation_attempt_id
            or lineage.subject_id != subject_id
            or lineage.application_plan_id != plan_id
        ):
            raise ValueError("stopped-source binding is inconsistent")
        _aware("created_at", self.created_at)
        expected_hash = _canonical_hash(self.identity_dict())
        if (
            self.record_hash != expected_hash
            or self.record_id
            != f"resume-compilation-stopped-source-{expected_hash}"
        ):
            raise ValueError("stopped-source identity is invalid")
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "application_plan_id", plan_id)

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "compilation_attempt_id": self.compilation_attempt_id,
            "compilation_stage": self.compilation_stage.value,
            "outcome": self.outcome.value,
            "preparation_invocation_ref": (
                self.preparation_invocation_ref.to_dict()
            ),
            "record_version": self.record_version,
            "source_resolution_lineage": (
                self.source_resolution_lineage.to_dict()
            ),
            "stop_reason": self.stop_reason.to_dict(),
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "created_at": _rfc3339(self.created_at),
            "record_hash": self.record_hash,
            "record_id": self.record_id,
        }

    @property
    def reference(self) -> ResumeCompilationStoppedSourceRef:
        return ResumeCompilationStoppedSourceRef(
            record_id=self.record_id,
            record_version=self.record_version,
            record_hash=self.record_hash,
        )

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        application_plan_id: str,
        preparation_invocation_ref: PreparationInvocationBindingRef,
        compilation_attempt_id: str,
        outcome: PreparationStageOutcome,
        stop_reason: PreparationStopReasonEnvelope,
        source_resolution_lineage: CompilationSourceResolutionLineage,
        created_at: datetime,
    ) -> "ResumeCompilationStoppedSourceRecord":
        identity = {
            "application_plan_id": _clean(
                "application_plan_id", application_plan_id, 180
            ),
            "compilation_attempt_id": _clean(
                "compilation_attempt_id", compilation_attempt_id, 240
            ),
            "compilation_stage": (
                ApplicationPreparationStage.RESUME_COMPILATION.value
            ),
            "outcome": PreparationStageOutcome(outcome).value,
            "preparation_invocation_ref": (
                preparation_invocation_ref.to_dict()
            ),
            "record_version": (
                RESUME_COMPILATION_STOPPED_SOURCE_CONTRACT_VERSION
            ),
            "source_resolution_lineage": (
                source_resolution_lineage.to_dict()
            ),
            "stop_reason": stop_reason.to_dict(),
            "subject_id": _clean("subject_id", subject_id, 160),
        }
        record_hash = _canonical_hash(identity)
        return cls(
            record_id=f"resume-compilation-stopped-source-{record_hash}",
            record_version=(
                RESUME_COMPILATION_STOPPED_SOURCE_CONTRACT_VERSION
            ),
            record_hash=record_hash,
            subject_id=identity["subject_id"],
            application_plan_id=identity["application_plan_id"],
            preparation_invocation_ref=preparation_invocation_ref,
            compilation_attempt_id=identity["compilation_attempt_id"],
            compilation_stage=(
                ApplicationPreparationStage.RESUME_COMPILATION
            ),
            outcome=PreparationStageOutcome(outcome),
            stop_reason=stop_reason,
            source_resolution_lineage=source_resolution_lineage,
            created_at=_aware("created_at", created_at),
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "ResumeCompilationStoppedSourceRecord":
        expected = {
            "application_plan_id",
            "compilation_attempt_id",
            "compilation_stage",
            "created_at",
            "outcome",
            "preparation_invocation_ref",
            "record_hash",
            "record_id",
            "record_version",
            "source_resolution_lineage",
            "stop_reason",
            "subject_id",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("persisted stopped-source record is invalid")
        lineage_value = value["source_resolution_lineage"]
        if not isinstance(lineage_value, Mapping):
            raise ValueError("persisted source lineage is invalid")
        lineage: CompilationSourceResolutionLineage
        if lineage_value.get("kind") == "RESOLVED":
            lineage = ResolvedCompilationSourceLineage.from_dict(
                lineage_value
            )
        elif lineage_value.get("kind") == "UNRESOLVED":
            lineage = UnresolvedCompilationSourceLineage.from_dict(
                lineage_value
            )
        else:
            raise ValueError("persisted source lineage kind is invalid")
        return cls(
            record_id=value["record_id"],
            record_version=value["record_version"],
            record_hash=value["record_hash"],
            subject_id=value["subject_id"],
            application_plan_id=value["application_plan_id"],
            preparation_invocation_ref=(
                PreparationInvocationBindingRef.from_dict(
                    value["preparation_invocation_ref"]
                )
            ),
            compilation_attempt_id=value["compilation_attempt_id"],
            compilation_stage=ApplicationPreparationStage(
                value["compilation_stage"]
            ),
            outcome=PreparationStageOutcome(value["outcome"]),
            stop_reason=PreparationStopReasonEnvelope.from_dict(
                value["stop_reason"]
            ),
            source_resolution_lineage=lineage,
            created_at=_parse_time(value["created_at"]),
        )


class ResumeCompilationStoppedSourceWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ResumeCompilationStoppedSourceReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class ResumeCompilationStoppedSourceWriteResult:
    status: ResumeCompilationStoppedSourceWriteStatus
    record: ResumeCompilationStoppedSourceRecord | None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ResumeCompilationStoppedSourceReadResult:
    status: ResumeCompilationStoppedSourceReadStatus
    record: ResumeCompilationStoppedSourceRecord | None


@runtime_checkable
class ResumeCompilationStoppedSourceRepository(Protocol):
    def save(
        self, record: ResumeCompilationStoppedSourceRecord
    ) -> ResumeCompilationStoppedSourceWriteResult: ...

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeCompilationStoppedSourceReadResult: ...


@runtime_checkable
class ResumeCompilationStoppedSourceProvider(Protocol):
    def get(
        self,
        *,
        subject_id: str,
        stopped_source_ref: ResumeCompilationStoppedSourceRef,
    ) -> ResumeCompilationStoppedSourceReadResult: ...


class PrivateHomeResumeCompilationStoppedSourceRepository:
    """Subject-isolated immutable stopped-source record persistence."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, record_id: str) -> Path:
        subject = _clean("subject_id", subject_id, 160)
        if (
            not isinstance(record_id, str)
            or _RECORD_ID_RE.fullmatch(record_id) is None
        ):
            raise ValueError("stopped-source record ID is invalid")
        return (
            self._home.paths.resume_compilation_stopped_sources
            / _subject_key(subject)
            / f"{record_id}.json"
        )

    def get(
        self, *, subject_id: str, record_id: str
    ) -> ResumeCompilationStoppedSourceReadResult:
        path = self._path(subject_id, record_id)
        with self._lock:
            if not path.exists():
                return ResumeCompilationStoppedSourceReadResult(
                    ResumeCompilationStoppedSourceReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return ResumeCompilationStoppedSourceReadResult(
                    ResumeCompilationStoppedSourceReadStatus
                    .INTEGRITY_FAILURE,
                    None,
                )
            try:
                record = ResumeCompilationStoppedSourceRecord.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return ResumeCompilationStoppedSourceReadResult(
                    ResumeCompilationStoppedSourceReadStatus
                    .INTEGRITY_FAILURE,
                    None,
                )
            if (
                record.subject_id != subject_id.strip()
                or record.record_id != record_id
                or path.name != f"{record.record_id}.json"
            ):
                return ResumeCompilationStoppedSourceReadResult(
                    ResumeCompilationStoppedSourceReadStatus
                    .INTEGRITY_FAILURE,
                    None,
                )
            return ResumeCompilationStoppedSourceReadResult(
                ResumeCompilationStoppedSourceReadStatus.FOUND, record
            )

    def save(
        self, record: ResumeCompilationStoppedSourceRecord
    ) -> ResumeCompilationStoppedSourceWriteResult:
        if not isinstance(record, ResumeCompilationStoppedSourceRecord):
            raise TypeError("record must be a stopped-source record")
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
                return ResumeCompilationStoppedSourceWriteResult(
                    ResumeCompilationStoppedSourceWriteStatus.FAILED,
                    None,
                    retryable=True,
                )
            if created:
                return ResumeCompilationStoppedSourceWriteResult(
                    ResumeCompilationStoppedSourceWriteStatus.CREATED,
                    record,
                )
            existing = self.get(
                subject_id=record.subject_id, record_id=record.record_id
            )
            if (
                existing.status
                is ResumeCompilationStoppedSourceReadStatus.FOUND
                and existing.record is not None
                and existing.record.identity_dict() == record.identity_dict()
            ):
                return ResumeCompilationStoppedSourceWriteResult(
                    ResumeCompilationStoppedSourceWriteStatus.UNCHANGED,
                    existing.record,
                )
            return ResumeCompilationStoppedSourceWriteResult(
                ResumeCompilationStoppedSourceWriteStatus.FAILED,
                None,
            )


class RepositoryResumeCompilationStoppedSourceProvider:
    def __init__(
        self, repository: ResumeCompilationStoppedSourceRepository
    ) -> None:
        if not isinstance(
            repository, ResumeCompilationStoppedSourceRepository
        ):
            raise TypeError("stopped-source repository is invalid")
        self._repository = repository

    def get(
        self,
        *,
        subject_id: str,
        stopped_source_ref: ResumeCompilationStoppedSourceRef,
    ) -> ResumeCompilationStoppedSourceReadResult:
        if not isinstance(
            stopped_source_ref, ResumeCompilationStoppedSourceRef
        ):
            raise TypeError("stopped-source reference must be typed")
        read = self._repository.get(
            subject_id=subject_id,
            record_id=stopped_source_ref.record_id,
        )
        if (
            read.status is not ResumeCompilationStoppedSourceReadStatus.FOUND
            or read.record is None
        ):
            return read
        if read.record.reference != stopped_source_ref:
            return ResumeCompilationStoppedSourceReadResult(
                ResumeCompilationStoppedSourceReadStatus.INTEGRITY_FAILURE,
                None,
            )
        return read


__all__ = [
    "PrivateHomeResumeCompilationStoppedSourceRepository",
    "RepositoryResumeCompilationStoppedSourceProvider",
    "ResumeCompilationStoppedSourceProvider",
    "ResumeCompilationStoppedSourceReadResult",
    "ResumeCompilationStoppedSourceReadStatus",
    "ResumeCompilationStoppedSourceRecord",
    "ResumeCompilationStoppedSourceRepository",
    "ResumeCompilationStoppedSourceWriteResult",
    "ResumeCompilationStoppedSourceWriteStatus",
]
