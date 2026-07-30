"""Immutable, subject-specific intent accepted after successful Job Discovery."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .job_discovery import JobIntakeIntent
from .private_home import PrivateHome


ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION = "accepted-job-intent-v1"
ACCEPTED_JOB_INTENT_CONTRACT_VERSION = "accepted-job-intent-v2"
ACCEPTED_JOB_INTENT_PROVENANCE_CONTRACT_VERSION = (
    "accepted-job-intent-source-provenance-v1"
)
_RECORD_ID_PATTERN = re.compile(r"^accepted-job-intent-[a-f0-9]{64}$")


class AcceptedJobIntentWriteStatus(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class AcceptedJobIntentReadStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class AcceptedJobIntentFailureReason(str, Enum):
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


class AcceptedJobIntentSourceType(str, Enum):
    CONVERSATIONAL_INTAKE = "CONVERSATIONAL_INTAKE"
    SEARCH_PROFILE_REFRESH = "SEARCH_PROFILE_REFRESH"


def _clean_id(name: str, value: Any, *, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the accepted intent contract")
    return cleaned


def _require_aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("recorded_at is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _require_aware("recorded_at", parsed)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _record_identity_v1(
    *,
    subject_id: str,
    job_id: str,
    intent: JobIntakeIntent,
    intake_proposal_id: str,
    discovery_run_id: str,
    contract_version: str,
) -> str:
    payload = {
        "contract_version": contract_version,
        "discovery_run_id": discovery_run_id,
        "intake_proposal_id": intake_proposal_id,
        "intent": intent.value,
        "job_id": job_id,
        "subject_id": subject_id,
    }
    return f"accepted-job-intent-{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


@dataclass(frozen=True, slots=True)
class AcceptedJobIntentSourceProvenance:
    source_type: AcceptedJobIntentSourceType
    source_id: str | None = None
    source_version: str | None = None
    source_profile_ids: tuple[str, ...] = ()
    contract_version: str = ACCEPTED_JOB_INTENT_PROVENANCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        source_type = AcceptedJobIntentSourceType(self.source_type)
        source_id = (
            _clean_id("source_id", self.source_id)
            if self.source_id is not None
            else None
        )
        source_version = (
            _clean_id("source_version", self.source_version, maximum=120)
            if self.source_version is not None
            else None
        )
        if not isinstance(self.source_profile_ids, tuple):
            raise TypeError("source_profile_ids must be a tuple")
        source_profile_ids = tuple(
            sorted(
                {
                    _clean_id("source_profile_id", value, maximum=160)
                    for value in self.source_profile_ids
                }
            )
        )
        contract_version = _clean_id(
            "provenance contract_version",
            self.contract_version,
            maximum=100,
        )
        if contract_version != ACCEPTED_JOB_INTENT_PROVENANCE_CONTRACT_VERSION:
            raise ValueError("intent source provenance contract is unsupported")
        if (
            source_type is AcceptedJobIntentSourceType.CONVERSATIONAL_INTAKE
            and (source_id is None or source_profile_ids)
        ):
            raise ValueError("conversational intent provenance is invalid")
        if (
            source_type is AcceptedJobIntentSourceType.SEARCH_PROFILE_REFRESH
            and not source_profile_ids
        ):
            raise ValueError("search-profile intent provenance is invalid")
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "source_version", source_version)
        object.__setattr__(self, "source_profile_ids", source_profile_ids)
        object.__setattr__(self, "contract_version", contract_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "source_profile_ids": list(self.source_profile_ids),
            "contract_version": self.contract_version,
        }


def _record_identity_v2(
    *,
    subject_id: str,
    job_id: str,
    intent: JobIntakeIntent,
    intake_proposal_id: str,
    discovery_run_id: str,
    provenance: AcceptedJobIntentSourceProvenance,
    contract_version: str,
) -> str:
    payload = {
        "contract_version": contract_version,
        "discovery_run_id": discovery_run_id,
        "intake_proposal_id": intake_proposal_id,
        "intent": intent.value,
        "job_id": job_id,
        "provenance": provenance.to_dict(),
        "subject_id": subject_id,
    }
    return f"accepted-job-intent-{hashlib.sha256(_canonical_json(payload)).hexdigest()}"


@dataclass(frozen=True, slots=True)
class AcceptedJobIntent:
    accepted_job_intent_id: str
    subject_id: str
    job_id: str
    intent: JobIntakeIntent
    intake_proposal_id: str
    discovery_run_id: str
    recorded_at: datetime
    provenance: AcceptedJobIntentSourceProvenance | None = None
    contract_version: str = ACCEPTED_JOB_INTENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        subject_id = _clean_id("subject_id", self.subject_id)
        job_id = _clean_id("job_id", self.job_id, maximum=160)
        proposal_id = _clean_id(
            "intake_proposal_id",
            self.intake_proposal_id,
        )
        run_id = _clean_id("discovery_run_id", self.discovery_run_id)
        intent = JobIntakeIntent(self.intent)
        contract_version = _clean_id(
            "contract_version",
            self.contract_version,
            maximum=80,
        )
        if contract_version not in {
            ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION,
            ACCEPTED_JOB_INTENT_CONTRACT_VERSION,
        }:
            raise ValueError("accepted job intent contract version is unsupported")
        provenance = self.provenance
        if contract_version == ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION:
            if provenance is not None:
                raise ValueError("v1 accepted intent cannot contain provenance")
        elif not isinstance(provenance, AcceptedJobIntentSourceProvenance):
            raise ValueError("v2 accepted intent requires typed provenance")
        recorded_at = _require_aware("recorded_at", self.recorded_at)
        record_id = _clean_id(
            "accepted_job_intent_id",
            self.accepted_job_intent_id,
            maximum=128,
        )
        if _RECORD_ID_PATTERN.fullmatch(record_id) is None:
            raise ValueError("accepted_job_intent_id is invalid")
        if contract_version == ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION:
            expected_id = _record_identity_v1(
                subject_id=subject_id,
                job_id=job_id,
                intent=intent,
                intake_proposal_id=proposal_id,
                discovery_run_id=run_id,
                contract_version=contract_version,
            )
        else:
            assert provenance is not None
            expected_id = _record_identity_v2(
                subject_id=subject_id,
                job_id=job_id,
                intent=intent,
                intake_proposal_id=proposal_id,
                discovery_run_id=run_id,
                provenance=provenance,
                contract_version=contract_version,
            )
        if record_id != expected_id:
            raise ValueError("accepted job intent identity is invalid")
        object.__setattr__(self, "subject_id", subject_id)
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "intent", intent)
        object.__setattr__(self, "intake_proposal_id", proposal_id)
        object.__setattr__(self, "discovery_run_id", run_id)
        object.__setattr__(self, "recorded_at", recorded_at)
        object.__setattr__(self, "contract_version", contract_version)

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        job_id: str,
        intent: JobIntakeIntent,
        intake_proposal_id: str,
        discovery_run_id: str,
        recorded_at: datetime,
        provenance: AcceptedJobIntentSourceProvenance,
    ) -> "AcceptedJobIntent":
        clean_subject = _clean_id("subject_id", subject_id)
        clean_job = _clean_id("job_id", job_id, maximum=160)
        clean_proposal = _clean_id(
            "intake_proposal_id",
            intake_proposal_id,
        )
        clean_run = _clean_id("discovery_run_id", discovery_run_id)
        typed_intent = JobIntakeIntent(intent)
        if not isinstance(provenance, AcceptedJobIntentSourceProvenance):
            raise TypeError("provenance must be typed")
        return cls(
            accepted_job_intent_id=_record_identity_v2(
                subject_id=clean_subject,
                job_id=clean_job,
                intent=typed_intent,
                intake_proposal_id=clean_proposal,
                discovery_run_id=clean_run,
                provenance=provenance,
                contract_version=ACCEPTED_JOB_INTENT_CONTRACT_VERSION,
            ),
            subject_id=clean_subject,
            job_id=clean_job,
            intent=typed_intent,
            intake_proposal_id=clean_proposal,
            discovery_run_id=clean_run,
            recorded_at=recorded_at,
            provenance=provenance,
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "accepted_job_intent_id": self.accepted_job_intent_id,
            "subject_id": self.subject_id,
            "job_id": self.job_id,
            "intent": self.intent.value,
            "intake_proposal_id": self.intake_proposal_id,
            "discovery_run_id": self.discovery_run_id,
            "recorded_at": _rfc3339(self.recorded_at),
            "contract_version": self.contract_version,
        }
        if self.contract_version == ACCEPTED_JOB_INTENT_CONTRACT_VERSION:
            assert self.provenance is not None
            value["provenance"] = self.provenance.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class AcceptedJobIntentWriteResult:
    status: AcceptedJobIntentWriteStatus
    intent: AcceptedJobIntent | None
    reason_code: AcceptedJobIntentFailureReason | None
    retryable: bool

    def __post_init__(self) -> None:
        status = AcceptedJobIntentWriteStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                AcceptedJobIntentFailureReason(self.reason_code),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if status in {
            AcceptedJobIntentWriteStatus.CREATED,
            AcceptedJobIntentWriteStatus.UNCHANGED,
        }:
            if (
                not isinstance(self.intent, AcceptedJobIntent)
                or self.reason_code is not None
                or self.retryable
            ):
                raise ValueError("successful accepted intent write result is invalid")
        elif (
            self.intent is not None
            or self.reason_code is None
        ):
            raise ValueError("failed accepted intent write result is invalid")


@dataclass(frozen=True, slots=True)
class AcceptedJobIntentReadResult:
    status: AcceptedJobIntentReadStatus
    intent: AcceptedJobIntent | None
    reason_code: AcceptedJobIntentFailureReason | None = None

    def __post_init__(self) -> None:
        status = AcceptedJobIntentReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                AcceptedJobIntentFailureReason(self.reason_code),
            )
        if status is AcceptedJobIntentReadStatus.FOUND:
            if (
                not isinstance(self.intent, AcceptedJobIntent)
                or self.reason_code is not None
            ):
                raise ValueError("found accepted intent read result is invalid")
        elif status is AcceptedJobIntentReadStatus.NOT_FOUND:
            if self.intent is not None or self.reason_code is not None:
                raise ValueError("not-found accepted intent read result is invalid")
        elif (
            self.intent is not None
            or self.reason_code
            is not AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
        ):
            raise ValueError("integrity-failure accepted intent result is invalid")


@runtime_checkable
class AcceptedJobIntentRepository(Protocol):
    def save(
        self,
        intent: AcceptedJobIntent,
    ) -> AcceptedJobIntentWriteResult:
        """Persist one immutable accepted intent."""

    def get_current(
        self,
        *,
        subject_id: str,
        job_id: str,
    ) -> AcceptedJobIntentReadResult:
        """Read the deterministic current intent for one subject and job."""


def _intent_from_dict(value: Any) -> AcceptedJobIntent:
    if not isinstance(value, Mapping):
        raise ValueError("persisted accepted intent is invalid")
    contract_version = value.get("contract_version")
    expected_v1 = {
        "accepted_job_intent_id",
        "subject_id",
        "job_id",
        "intent",
        "intake_proposal_id",
        "discovery_run_id",
        "recorded_at",
        "contract_version",
    }
    expected_v2 = expected_v1 | {"provenance"}
    if (
        contract_version == ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION
        and set(value) == expected_v1
    ):
        provenance = None
    elif (
        contract_version == ACCEPTED_JOB_INTENT_CONTRACT_VERSION
        and set(value) == expected_v2
    ):
        provenance_value = value["provenance"]
        expected_provenance = {
            "source_type",
            "source_id",
            "source_version",
            "source_profile_ids",
            "contract_version",
        }
        if (
            not isinstance(provenance_value, Mapping)
            or set(provenance_value) != expected_provenance
            or not isinstance(provenance_value["source_profile_ids"], list)
        ):
            raise ValueError("persisted accepted intent provenance is invalid")
        provenance = AcceptedJobIntentSourceProvenance(
            source_type=AcceptedJobIntentSourceType(
                provenance_value["source_type"]
            ),
            source_id=provenance_value["source_id"],
            source_version=provenance_value["source_version"],
            source_profile_ids=tuple(provenance_value["source_profile_ids"]),
            contract_version=provenance_value["contract_version"],
        )
    else:
        raise ValueError("persisted accepted intent fields are invalid")
    return AcceptedJobIntent(
        accepted_job_intent_id=value["accepted_job_intent_id"],
        subject_id=value["subject_id"],
        job_id=value["job_id"],
        intent=JobIntakeIntent(value["intent"]),
        intake_proposal_id=value["intake_proposal_id"],
        discovery_run_id=value["discovery_run_id"],
        recorded_at=_parse_timestamp(value["recorded_at"]),
        provenance=provenance,
        contract_version=contract_version,
    )


class PrivateHomeAcceptedJobIntentRepository:
    """Immutable Private Home records for accepted I2 add/apply intent."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _directory(self, *, subject_id: str, job_id: str) -> Path:
        subject = _clean_id("subject_id", subject_id)
        job = _clean_id("job_id", job_id, maximum=160)
        return (
            self._home.paths.accepted_job_intents
            / self._digest(subject)
            / self._digest(job)
        )

    def _path(self, intent: AcceptedJobIntent) -> Path:
        return (
            self._directory(
                subject_id=intent.subject_id,
                job_id=intent.job_id,
            )
            / f"{intent.accepted_job_intent_id}.json"
        )

    @staticmethod
    def _encoded(intent: AcceptedJobIntent) -> bytes:
        return (
            json.dumps(
                intent.to_dict(),
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")

    @staticmethod
    def _read_path(path: Path) -> AcceptedJobIntent:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            intent = _intent_from_dict(value)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("persisted accepted job intent is invalid") from exc
        if path.name != f"{intent.accepted_job_intent_id}.json":
            raise RuntimeError("persisted accepted job intent filename is invalid")
        return intent

    def save(
        self,
        intent: AcceptedJobIntent,
    ) -> AcceptedJobIntentWriteResult:
        if not isinstance(intent, AcceptedJobIntent):
            raise TypeError("intent must be an AcceptedJobIntent")
        path = self._path(intent)
        with self._lock:
            if (
                intent.contract_version
                == ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION
            ):
                if not path.exists():
                    return AcceptedJobIntentWriteResult(
                        status=AcceptedJobIntentWriteStatus.FAILED,
                        intent=None,
                        reason_code=(
                            AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
                        ),
                        retryable=False,
                    )
                try:
                    existing_v1 = self._read_path(path)
                except RuntimeError:
                    return AcceptedJobIntentWriteResult(
                        status=AcceptedJobIntentWriteStatus.FAILED,
                        intent=None,
                        reason_code=(
                            AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
                        ),
                        retryable=False,
                    )
                if existing_v1 != intent:
                    return AcceptedJobIntentWriteResult(
                        status=AcceptedJobIntentWriteStatus.FAILED,
                        intent=None,
                        reason_code=(
                            AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
                        ),
                        retryable=False,
                    )
                return AcceptedJobIntentWriteResult(
                    status=AcceptedJobIntentWriteStatus.UNCHANGED,
                    intent=existing_v1,
                    reason_code=None,
                    retryable=False,
                )
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    self._encoded(intent),
                )
            except (OSError, RuntimeError):
                return AcceptedJobIntentWriteResult(
                    status=AcceptedJobIntentWriteStatus.FAILED,
                    intent=None,
                    reason_code=(
                        AcceptedJobIntentFailureReason.PERSISTENCE_FAILED
                    ),
                    retryable=True,
                )
            if created:
                return AcceptedJobIntentWriteResult(
                    status=AcceptedJobIntentWriteStatus.CREATED,
                    intent=intent,
                    reason_code=None,
                    retryable=False,
                )
            try:
                existing = self._read_path(path)
            except RuntimeError:
                return AcceptedJobIntentWriteResult(
                    status=AcceptedJobIntentWriteStatus.FAILED,
                    intent=None,
                    reason_code=(
                        AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
                    ),
                    retryable=False,
                )
            if existing != intent:
                return AcceptedJobIntentWriteResult(
                    status=AcceptedJobIntentWriteStatus.FAILED,
                    intent=None,
                    reason_code=(
                        AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
                    ),
                    retryable=False,
                )
            return AcceptedJobIntentWriteResult(
                status=AcceptedJobIntentWriteStatus.UNCHANGED,
                intent=existing,
                reason_code=None,
                retryable=False,
            )

    def get_current(
        self,
        *,
        subject_id: str,
        job_id: str,
    ) -> AcceptedJobIntentReadResult:
        subject = _clean_id("subject_id", subject_id)
        job = _clean_id("job_id", job_id, maximum=160)
        directory = self._directory(subject_id=subject, job_id=job)
        if not directory.exists():
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.NOT_FOUND,
                intent=None,
            )
        if directory.is_symlink() or not directory.is_dir():
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.INTEGRITY_FAILURE,
                intent=None,
                reason_code=AcceptedJobIntentFailureReason.INTEGRITY_FAILURE,
            )
        with self._lock:
            try:
                paths = tuple(sorted(directory.glob("*.json")))
                intents = tuple(self._read_path(path) for path in paths)
            except (OSError, RuntimeError):
                return AcceptedJobIntentReadResult(
                    status=AcceptedJobIntentReadStatus.INTEGRITY_FAILURE,
                    intent=None,
                    reason_code=(
                        AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
                    ),
                )
        if not intents:
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.NOT_FOUND,
                intent=None,
            )
        if any(
            item.subject_id != subject or item.job_id != job
            for item in intents
        ):
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.INTEGRITY_FAILURE,
                intent=None,
                reason_code=AcceptedJobIntentFailureReason.INTEGRITY_FAILURE,
            )
        requests = tuple(
            item
            for item in intents
            if item.intent is JobIntakeIntent.REQUEST_APPLICATION
        )
        eligible = requests or intents
        current = max(
            eligible,
            key=lambda item: (
                item.recorded_at,
                item.accepted_job_intent_id,
            ),
        )
        return AcceptedJobIntentReadResult(
            status=AcceptedJobIntentReadStatus.FOUND,
            intent=current,
        )

    def get_by_id(
        self,
        *,
        subject_id: str,
        job_id: str,
        accepted_job_intent_id: str,
    ) -> AcceptedJobIntentReadResult:
        """Read one exact immutable intent without consulting current order."""

        subject = _clean_id("subject_id", subject_id)
        job = _clean_id("job_id", job_id, maximum=160)
        intent_id = _clean_id(
            "accepted_job_intent_id",
            accepted_job_intent_id,
            maximum=128,
        )
        if _RECORD_ID_PATTERN.fullmatch(intent_id) is None:
            raise ValueError("accepted_job_intent_id is invalid")
        path = (
            self._directory(subject_id=subject, job_id=job)
            / f"{intent_id}.json"
        )
        if not path.exists():
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.NOT_FOUND,
                intent=None,
            )
        if path.is_symlink() or not path.is_file():
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.INTEGRITY_FAILURE,
                intent=None,
                reason_code=AcceptedJobIntentFailureReason.INTEGRITY_FAILURE,
            )
        with self._lock:
            try:
                intent = self._read_path(path)
            except RuntimeError:
                return AcceptedJobIntentReadResult(
                    status=AcceptedJobIntentReadStatus.INTEGRITY_FAILURE,
                    intent=None,
                    reason_code=(
                        AcceptedJobIntentFailureReason.INTEGRITY_FAILURE
                    ),
                )
        if (
            intent.subject_id != subject
            or intent.job_id != job
            or intent.accepted_job_intent_id != intent_id
        ):
            return AcceptedJobIntentReadResult(
                status=AcceptedJobIntentReadStatus.INTEGRITY_FAILURE,
                intent=None,
                reason_code=AcceptedJobIntentFailureReason.INTEGRITY_FAILURE,
            )
        return AcceptedJobIntentReadResult(
            status=AcceptedJobIntentReadStatus.FOUND,
            intent=intent,
        )


__all__ = [
    "ACCEPTED_JOB_INTENT_CONTRACT_VERSION",
    "ACCEPTED_JOB_INTENT_PROVENANCE_CONTRACT_VERSION",
    "ACCEPTED_JOB_INTENT_V1_CONTRACT_VERSION",
    "AcceptedJobIntent",
    "AcceptedJobIntentFailureReason",
    "AcceptedJobIntentReadResult",
    "AcceptedJobIntentReadStatus",
    "AcceptedJobIntentRepository",
    "AcceptedJobIntentSourceProvenance",
    "AcceptedJobIntentSourceType",
    "AcceptedJobIntentWriteResult",
    "AcceptedJobIntentWriteStatus",
    "PrivateHomeAcceptedJobIntentRepository",
]
