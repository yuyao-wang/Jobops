"""Typed, in-process entry point for V1 Job Discovery writes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import uuid4

from .bundles import normalized_job_url, stable_job_id
from .event_ledger import _canonical_url_identity
from .private_home import PrivateHome


JOB_POSTING_SCHEMA_VERSION = "1.0"
JOB_POSTING_INITIAL_STATUS = "NORMALIZED"
WORK_MODES = frozenset({"ONSITE", "HYBRID", "REMOTE", "UNKNOWN"})
JOB_POSTING_STATUSES = frozenset(
    {
        "NORMALIZED",
        "ANALYZED",
        "PRIORITIZED",
        "READY",
        "QUEUED",
        "DUPLICATE",
        "EXCLUDED",
        "SKIPPED",
        "EXPIRED",
    }
)
ATS_TYPES = frozenset(
    {"greenhouse", "lever", "ashby", "jobvite", "workday", "custom", "unknown"}
)
_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_CONTENT_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class DiscoveryTrigger(str, Enum):
    CONVERSATIONAL = "CONVERSATIONAL"


class JobIntakeIntent(str, Enum):
    ADD_JOB = "ADD_JOB"
    REQUEST_APPLICATION = "REQUEST_APPLICATION"


class ProposalResolution(str, Enum):
    RESOLVED = "RESOLVED"
    INCOMPLETE = "INCOMPLETE"
    AMBIGUOUS = "AMBIGUOUS"
    UNSUPPORTED = "UNSUPPORTED"


class DiscoveryDisposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    REJECTED = "REJECTED"


class DiscoveryChange(str, Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"


class DiscoveryReason(str, Enum):
    JOB_CREATED = "JOB_CREATED"
    JOB_UPDATED = "JOB_UPDATED"
    JOB_UNCHANGED = "JOB_UNCHANGED"
    PROPOSAL_INCOMPLETE = "PROPOSAL_INCOMPLETE"
    PROPOSAL_AMBIGUOUS = "PROPOSAL_AMBIGUOUS"
    PROPOSAL_UNSUPPORTED = "PROPOSAL_UNSUPPORTED"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    RESOLVED_CANDIDATE_REQUIRED = "RESOLVED_CANDIDATE_REQUIRED"
    RESOLVED_CANDIDATE_NOT_ALLOWED = "RESOLVED_CANDIDATE_NOT_ALLOWED"
    PROPOSAL_CONTRACT_INVALID = "PROPOSAL_CONTRACT_INVALID"
    REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"
    INVALID_SOURCE_URL = "INVALID_SOURCE_URL"
    INVALID_APPLICATION_URL = "INVALID_APPLICATION_URL"
    INVALID_POSTED_AT = "INVALID_POSTED_AT"
    INVALID_ENUM_VALUE = "INVALID_ENUM_VALUE"
    FIELD_OUT_OF_RANGE = "FIELD_OUT_OF_RANGE"


class DiscoveryRunStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ResolvedJobCandidate:
    source_platform: str
    source_url: str
    company: str
    title: str
    description: str
    source_job_id: str | None = None
    application_url: str | None = None
    location: str = ""
    work_mode: str = "UNKNOWN"
    posted_at: str | None = None
    ats_type: str = "unknown"


@dataclass(frozen=True, slots=True)
class JobIntakeProposal:
    proposal_id: str
    intent: JobIntakeIntent
    resolution: ProposalResolution
    resolved_candidate: ResolvedJobCandidate | None = None
    missing_fields: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent", JobIntakeIntent(self.intent))
        object.__setattr__(self, "resolution", ProposalResolution(self.resolution))
        object.__setattr__(
            self,
            "missing_fields",
            tuple(str(value).strip() for value in self.missing_fields if str(value).strip()),
        )
        object.__setattr__(
            self,
            "alternatives",
            tuple(str(value).strip() for value in self.alternatives if str(value).strip()),
        )


@dataclass(frozen=True, slots=True)
class JobDiscoveryRequest:
    request_id: str
    trigger: DiscoveryTrigger
    proposal: JobIntakeProposal

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger", DiscoveryTrigger(self.trigger))
        if not isinstance(self.proposal, JobIntakeProposal):
            raise TypeError("proposal must be a JobIntakeProposal")


@dataclass(frozen=True, slots=True)
class JobPosting:
    schema_version: str
    job_id: str
    revision: int
    source_platform: str
    source_job_id: str | None
    source_url: str
    company: str
    title: str
    location: str
    work_mode: str
    posted_at: str | None
    observed_at: str
    application_url: str | None
    ats_type: str
    description: str
    content_hash: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "revision": self.revision,
            "source_platform": self.source_platform,
            "source_job_id": self.source_job_id,
            "source_url": self.source_url,
            "company": self.company,
            "title": self.title,
            "location": self.location,
            "work_mode": self.work_mode,
            "posted_at": self.posted_at,
            "observed_at": self.observed_at,
            "application_url": self.application_url,
            "ats_type": self.ats_type,
            "description": self.description,
            "content_hash": self.content_hash,
            "status": self.status,
        }


class JobPostingRepositoryError(RuntimeError):
    """Raised when a persisted V1 JobPosting cannot be read safely."""


@runtime_checkable
class JobPostingReadRepository(Protocol):
    def get(self, job_id: str) -> JobPosting | None:
        """Return one current typed V1 JobPosting without modifying it."""


@dataclass(frozen=True, slots=True)
class DiscoveryRun:
    run_id: str
    request_id: str
    proposal_id: str
    status: DiscoveryRunStatus
    disposition: DiscoveryDisposition
    change: DiscoveryChange | None
    job_id: str | None
    reason_code: DiscoveryReason
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "proposal_id": self.proposal_id,
            "status": self.status.value,
            "disposition": self.disposition.value,
            "change": self.change.value if self.change else None,
            "job_id": self.job_id,
            "reason_code": self.reason_code.value,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class JobDiscoveryResponse:
    disposition: DiscoveryDisposition
    original_intent: JobIntakeIntent
    reason_code: DiscoveryReason
    run_id: str | None = None
    job_id: str | None = None
    change: DiscoveryChange | None = None
    missing_fields: tuple[str, ...] = field(default_factory=tuple)
    alternatives: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "original_intent": self.original_intent.value,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "change": self.change.value if self.change else None,
            "missing_fields": list(self.missing_fields),
            "alternatives": list(self.alternatives),
            "reason_code": self.reason_code.value,
        }


class _CandidateError(ValueError):
    def __init__(
        self, reason: DiscoveryReason, *, fields: tuple[str, ...] = ()
    ) -> None:
        self.reason = reason
        self.fields = fields
        super().__init__(reason.value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def _canonical_http_url(value: str, reason: DiscoveryReason) -> str:
    try:
        normalized = normalized_job_url(value)
        canonical = _canonical_url_identity(normalized)
    except (TypeError, ValueError) as exc:
        raise _CandidateError(reason) from exc
    if len(canonical) > 2048:
        raise _CandidateError(DiscoveryReason.FIELD_OUT_OF_RANGE)
    return canonical


def _normalize_timestamp(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise _CandidateError(DiscoveryReason.INVALID_POSTED_AT) from exc
    if parsed.tzinfo is None:
        raise _CandidateError(DiscoveryReason.INVALID_POSTED_AT)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _required_text(candidate: ResolvedJobCandidate) -> dict[str, str]:
    values = {
        "source_platform": str(candidate.source_platform or "").strip().casefold(),
        "source_url": str(candidate.source_url or "").strip(),
        "company": " ".join(str(candidate.company or "").split()),
        "title": " ".join(str(candidate.title or "").split()),
        "description": str(candidate.description or "").strip(),
    }
    missing = tuple(key for key, value in values.items() if not value)
    if missing:
        raise _CandidateError(
            DiscoveryReason.REQUIRED_FIELD_MISSING, fields=missing
        )
    limits = {
        "source_platform": 80,
        "source_url": 2048,
        "company": 240,
        "title": 240,
        "description": 100000,
    }
    excessive = tuple(key for key, limit in limits.items() if len(values[key]) > limit)
    if excessive:
        raise _CandidateError(
            DiscoveryReason.FIELD_OUT_OF_RANGE, fields=excessive
        )
    return values


def _normalize_candidate(candidate: ResolvedJobCandidate) -> dict[str, Any]:
    required = _required_text(candidate)
    source_url = _canonical_http_url(
        required["source_url"], DiscoveryReason.INVALID_SOURCE_URL
    )
    application_url = (
        _canonical_http_url(
            str(candidate.application_url),
            DiscoveryReason.INVALID_APPLICATION_URL,
        )
        if candidate.application_url is not None
        and str(candidate.application_url).strip()
        else None
    )
    work_mode = str(candidate.work_mode or "UNKNOWN").strip().upper()
    ats_type = str(candidate.ats_type or "unknown").strip().casefold()
    if work_mode not in WORK_MODES or ats_type not in ATS_TYPES:
        raise _CandidateError(DiscoveryReason.INVALID_ENUM_VALUE)
    location = str(candidate.location or "").strip()
    source_job_id = (
        str(candidate.source_job_id).strip()
        if candidate.source_job_id is not None
        and str(candidate.source_job_id).strip()
        else None
    )
    excessive = []
    if len(location) > 320:
        excessive.append("location")
    if source_job_id is not None and len(source_job_id) > 240:
        excessive.append("source_job_id")
    if excessive:
        raise _CandidateError(
            DiscoveryReason.FIELD_OUT_OF_RANGE, fields=tuple(excessive)
        )
    return {
        "source_platform": required["source_platform"],
        "source_job_id": source_job_id,
        "source_url": source_url,
        "company": required["company"],
        "title": required["title"],
        "location": location,
        "work_mode": work_mode,
        "posted_at": _normalize_timestamp(candidate.posted_at),
        "application_url": application_url,
        "ats_type": ats_type,
        "description": required["description"],
    }


def _content_hash(candidate: Mapping[str, Any]) -> str:
    payload = json.dumps(
        dict(candidate),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _job_posting_from_dict(
    value: Any,
    *,
    expected_job_id: str | None = None,
) -> JobPosting:
    expected_fields = set(JobPosting.__dataclass_fields__)
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError("persisted JobPosting fields are invalid")
    try:
        posting = JobPosting(**dict(value))
    except TypeError as exc:
        raise ValueError("persisted JobPosting contract is invalid") from exc
    if (
        posting.schema_version != JOB_POSTING_SCHEMA_VERSION
        or not isinstance(posting.job_id, str)
        or _JOB_ID_PATTERN.fullmatch(posting.job_id) is None
        or (
            expected_job_id is not None
            and posting.job_id != expected_job_id
        )
        or isinstance(posting.revision, bool)
        or not isinstance(posting.revision, int)
        or posting.revision < 1
        or not isinstance(posting.content_hash, str)
        or _CONTENT_HASH_PATTERN.fullmatch(posting.content_hash) is None
        or posting.status not in JOB_POSTING_STATUSES
    ):
        raise ValueError("persisted JobPosting identity is invalid")
    normalized = _normalize_candidate(
        ResolvedJobCandidate(
            source_platform=posting.source_platform,
            source_url=posting.source_url,
            company=posting.company,
            title=posting.title,
            description=posting.description,
            source_job_id=posting.source_job_id,
            application_url=posting.application_url,
            location=posting.location,
            work_mode=posting.work_mode,
            posted_at=posting.posted_at,
            ats_type=posting.ats_type,
        )
    )
    if any(
        getattr(posting, field) != normalized[field]
        for field in normalized
    ):
        raise ValueError("persisted JobPosting is not canonical")
    observed_at = _normalize_timestamp(posting.observed_at)
    if (
        observed_at != posting.observed_at
        or stable_job_id(url=posting.source_url) != posting.job_id
        or _content_hash(normalized) != posting.content_hash
    ):
        raise ValueError("persisted JobPosting binding is invalid")
    return posting


def _load_existing(path: Path, job_id: str) -> JobPosting | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return _job_posting_from_dict(
            value,
            expected_job_id=job_id,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("persisted JobPosting is unreadable") from exc


class PrivateHomeJobPostingRepository:
    """Read the current typed V1 JobPosting from Discovery persistence."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def get(self, job_id: str) -> JobPosting | None:
        if (
            not isinstance(job_id, str)
            or _JOB_ID_PATTERN.fullmatch(job_id) is None
        ):
            raise ValueError("job_id is outside the JobPosting read contract")
        path = self._home.paths.job_postings / f"{job_id}.json"
        try:
            return _load_existing(path, job_id)
        except RuntimeError as exc:
            raise JobPostingRepositoryError(
                "persisted JobPosting could not be loaded"
            ) from exc


def _persist_run(home: PrivateHome, run: DiscoveryRun) -> None:
    path = home.paths.discovery_runs / f"{run.run_id}.json"
    home.write_bytes(path, _json_bytes(run.to_dict()))


def _formal_response(
    *,
    home: PrivateHome,
    request: JobDiscoveryRequest,
    disposition: DiscoveryDisposition,
    reason: DiscoveryReason,
    status: DiscoveryRunStatus,
    change: DiscoveryChange | None = None,
    job_id: str | None = None,
    missing_fields: tuple[str, ...] = (),
) -> JobDiscoveryResponse:
    run = DiscoveryRun(
        run_id=f"discovery-run-{uuid4().hex}",
        request_id=str(request.request_id),
        proposal_id=str(request.proposal.proposal_id),
        status=status,
        disposition=disposition,
        change=change,
        job_id=job_id,
        reason_code=reason,
        recorded_at=_utc_now(),
    )
    _persist_run(home, run)
    return JobDiscoveryResponse(
        disposition=disposition,
        original_intent=request.proposal.intent,
        reason_code=reason,
        run_id=run.run_id,
        job_id=job_id,
        change=change,
        missing_fields=missing_fields,
        alternatives=request.proposal.alternatives,
    )


def run_discovery(request: JobDiscoveryRequest) -> JobDiscoveryResponse:
    """Validate one typed proposal and perform the only V1 Discovery write path."""

    if not isinstance(request, JobDiscoveryRequest):
        raise TypeError("request must be a JobDiscoveryRequest")
    proposal = request.proposal

    if (
        proposal.resolution is not ProposalResolution.RESOLVED
        and proposal.resolved_candidate is not None
    ):
        home = PrivateHome.discover()
        home.ensure()
        return _formal_response(
            home=home,
            request=request,
            disposition=DiscoveryDisposition.REJECTED,
            reason=DiscoveryReason.RESOLVED_CANDIDATE_NOT_ALLOWED,
            status=DiscoveryRunStatus.FAILED,
        )
    if proposal.resolution is ProposalResolution.INCOMPLETE:
        return JobDiscoveryResponse(
            disposition=DiscoveryDisposition.NEEDS_CLARIFICATION,
            original_intent=proposal.intent,
            reason_code=DiscoveryReason.PROPOSAL_INCOMPLETE,
            missing_fields=proposal.missing_fields,
            alternatives=proposal.alternatives,
        )
    if proposal.resolution is ProposalResolution.AMBIGUOUS:
        return JobDiscoveryResponse(
            disposition=DiscoveryDisposition.NEEDS_CLARIFICATION,
            original_intent=proposal.intent,
            reason_code=DiscoveryReason.PROPOSAL_AMBIGUOUS,
            missing_fields=proposal.missing_fields,
            alternatives=proposal.alternatives,
        )

    if proposal.resolution is ProposalResolution.UNSUPPORTED:
        home = PrivateHome.discover()
        home.ensure()
        return _formal_response(
            home=home,
            request=request,
            disposition=DiscoveryDisposition.REJECTED,
            reason=DiscoveryReason.PROPOSAL_UNSUPPORTED,
            status=DiscoveryRunStatus.FAILED,
        )
    if proposal.alternatives:
        return JobDiscoveryResponse(
            disposition=DiscoveryDisposition.NEEDS_CLARIFICATION,
            original_intent=proposal.intent,
            reason_code=DiscoveryReason.MULTIPLE_CANDIDATES,
            missing_fields=proposal.missing_fields,
            alternatives=proposal.alternatives,
        )

    home = PrivateHome.discover()
    home.ensure()

    if proposal.resolved_candidate is None:
        return _formal_response(
            home=home,
            request=request,
            disposition=DiscoveryDisposition.REJECTED,
            reason=DiscoveryReason.RESOLVED_CANDIDATE_REQUIRED,
            status=DiscoveryRunStatus.FAILED,
        )
    if proposal.missing_fields:
        return _formal_response(
            home=home,
            request=request,
            disposition=DiscoveryDisposition.REJECTED,
            reason=DiscoveryReason.PROPOSAL_CONTRACT_INVALID,
            status=DiscoveryRunStatus.FAILED,
            missing_fields=proposal.missing_fields,
        )

    try:
        normalized = _normalize_candidate(proposal.resolved_candidate)
    except _CandidateError as exc:
        return _formal_response(
            home=home,
            request=request,
            disposition=DiscoveryDisposition.REJECTED,
            reason=exc.reason,
            status=DiscoveryRunStatus.FAILED,
            missing_fields=exc.fields,
        )

    job_id = stable_job_id(url=normalized["source_url"])
    content_hash = _content_hash(normalized)
    job_path = home.paths.job_postings / f"{job_id}.json"
    existing = _load_existing(job_path, job_id)
    if existing is None:
        change = DiscoveryChange.CREATED
        revision = 1
        reason = DiscoveryReason.JOB_CREATED
    elif existing.content_hash == content_hash:
        change = DiscoveryChange.UNCHANGED
        revision = existing.revision
        reason = DiscoveryReason.JOB_UNCHANGED
    else:
        change = DiscoveryChange.UPDATED
        revision = existing.revision + 1
        reason = DiscoveryReason.JOB_UPDATED

    if change is not DiscoveryChange.UNCHANGED:
        posting = JobPosting(
            schema_version=JOB_POSTING_SCHEMA_VERSION,
            job_id=job_id,
            revision=revision,
            observed_at=_utc_now(),
            content_hash=content_hash,
            status=JOB_POSTING_INITIAL_STATUS,
            **normalized,
        )
        home.write_bytes(job_path, _json_bytes(posting.to_dict()))

    return _formal_response(
        home=home,
        request=request,
        disposition=DiscoveryDisposition.ACCEPTED,
        reason=reason,
        status=DiscoveryRunStatus.SUCCEEDED,
        change=change,
        job_id=job_id,
    )


__all__ = [
    "DiscoveryChange",
    "DiscoveryDisposition",
    "DiscoveryReason",
    "DiscoveryRun",
    "DiscoveryRunStatus",
    "DiscoveryTrigger",
    "JobDiscoveryRequest",
    "JobDiscoveryResponse",
    "JobIntakeIntent",
    "JobIntakeProposal",
    "JobPosting",
    "JobPostingReadRepository",
    "JobPostingRepositoryError",
    "PrivateHomeJobPostingRepository",
    "ProposalResolution",
    "ResolvedJobCandidate",
    "run_discovery",
]
