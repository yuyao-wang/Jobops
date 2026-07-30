"""Narrow conversational intake for public job URLs and named-job search."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit
from uuid import uuid4

from core.accepted_job_intent import (
    AcceptedJobIntent,
    AcceptedJobIntentFailureReason,
    AcceptedJobIntentRepository,
    AcceptedJobIntentSourceProvenance,
    AcceptedJobIntentSourceType,
    AcceptedJobIntentWriteResult,
    AcceptedJobIntentWriteStatus,
)
from core.job_discovery import (
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryReason,
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobDiscoveryResponse,
    JobIntakeIntent,
    JobIntakeProposal,
    ProposalResolution,
    ResolvedJobCandidate,
    run_discovery,
)
from core.job_search import (
    CandidateSet,
    JobSearchPort,
    JobSearchReason,
    JobSearchRequest,
    JobSearchResult,
    JobSearchStatus,
    SearchCandidate,
    search_jobs,
)
from source_connectors import (
    ReadJobReason,
    ReadJobRequest,
    ReadJobResult,
    ReadJobStatus,
    SourceJobObservation,
    SourcePlatform,
    read_public_job,
)


_URL_PATTERN = re.compile(
    r"\b(?:https?|ftp|file)://"
    r"""[^\s<>"'，。；,;!?！？)\]}]+""",
    re.IGNORECASE,
)
_TRAILING_URL_PUNCTUATION = ".,:，。；：！？"


class IntakeResponseStatus(str, Enum):
    NEEDS_USER = "NEEDS_USER"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class IntakeReason(str, Enum):
    NEEDS_MORE_INFORMATION = "NEEDS_MORE_INFORMATION"
    NEEDS_USER_SELECTION = "NEEDS_USER_SELECTION"


class NamedSearchReason(str, Enum):
    NO_CANDIDATES = "NO_CANDIDATES"


class NamedJobIntentHint(str, Enum):
    ADD_JOB = "ADD_JOB"
    REQUEST_APPLICATION = "REQUEST_APPLICATION"
    UNSPECIFIED = "UNSPECIFIED"


class CandidateSelectionStatus(str, Enum):
    WAITING_FOR_CANDIDATE_SELECTION = "WAITING_FOR_CANDIDATE_SELECTION"
    RESOLVING_CANDIDATE = "RESOLVING_CANDIDATE"
    COMPLETED = "COMPLETED"


class CandidateSelectionReason(str, Enum):
    CANDIDATE_SET_NOT_FOUND = "CANDIDATE_SET_NOT_FOUND"
    CONVERSATION_MISMATCH = "CONVERSATION_MISMATCH"
    CANDIDATE_SET_EXPIRED = "CANDIDATE_SET_EXPIRED"
    CANDIDATE_SET_ALREADY_RESOLVED = "CANDIDATE_SET_ALREADY_RESOLVED"
    CANDIDATE_NOT_FOUND = "CANDIDATE_NOT_FOUND"
    CANDIDATE_SOURCE_INVALID = "CANDIDATE_SOURCE_INVALID"


class PendingIntakeStatus(str, Enum):
    WAITING_FOR_ACTION = "WAITING_FOR_ACTION"
    RESOLVING = "RESOLVING"
    PERSISTING_INTENT = "PERSISTING_INTENT"
    INTENT_PERSISTENCE_FAILED = "INTENT_PERSISTENCE_FAILED"
    COMPLETED = "COMPLETED"


class IntakeAction(str, Enum):
    ADD_JOB = "ADD_JOB"
    REQUEST_APPLICATION = "REQUEST_APPLICATION"


class ResolveIntakeStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ResolveIntakeReason(str, Enum):
    PENDING_INTAKE_NOT_FOUND = "PENDING_INTAKE_NOT_FOUND"
    CONVERSATION_MISMATCH = "CONVERSATION_MISMATCH"
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    PENDING_INTAKE_EXPIRED = "PENDING_INTAKE_EXPIRED"
    PENDING_INTAKE_ALREADY_RESOLVED = "PENDING_INTAKE_ALREADY_RESOLVED"
    INVALID_ACTION = "INVALID_ACTION"
    PENDING_OBSERVATION_INVALID = "PENDING_OBSERVATION_INVALID"
    DISCOVERY_TEMPORARILY_UNAVAILABLE = "DISCOVERY_TEMPORARILY_UNAVAILABLE"
    DISCOVERY_RESPONSE_INVALID = "DISCOVERY_RESPONSE_INVALID"
    ACCEPTED_INTENT_PERSISTENCE_FAILED = (
        "ACCEPTED_INTENT_PERSISTENCE_FAILED"
    )


@dataclass(frozen=True, slots=True)
class ConversationalIntakeRequest:
    conversation_id: str
    message: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.conversation_id, str)
            or not self.conversation_id.strip()
            or len(self.conversation_id) > 240
        ):
            raise ValueError("conversation_id must be a non-empty string")
        if not isinstance(self.message, str):
            raise TypeError("message must be a string")


@dataclass(frozen=True, slots=True)
class JobSummary:
    company: str
    title: str
    location: str
    source_platform: SourcePlatform

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_platform",
            SourcePlatform(self.source_platform),
        )


@dataclass(frozen=True, slots=True)
class PendingIntake:
    pending_intake_id: str
    conversation_id: str
    observation: SourceJobObservation | None
    status: PendingIntakeStatus
    created_at: datetime
    expires_at: datetime
    resolution_subject_id: str | None = None
    selected_action: IntakeAction | None = None
    discovery_response: JobDiscoveryResponse | None = None
    accepted_intent_write_result: AcceptedJobIntentWriteResult | None = None
    accepted_intent_recorded_at: datetime | None = None
    resolved_at: datetime | None = None
    intent_hint: NamedJobIntentHint = NamedJobIntentHint.UNSPECIFIED
    source_candidate_set_id: str | None = None
    source_candidate_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", PendingIntakeStatus(self.status))
        object.__setattr__(
            self,
            "intent_hint",
            NamedJobIntentHint(self.intent_hint),
        )
        if self.observation is not None and not isinstance(
            self.observation,
            SourceJobObservation,
        ):
            raise TypeError("observation must be a SourceJobObservation")
        if self.resolution_subject_id is not None and (
            not isinstance(self.resolution_subject_id, str)
            or not self.resolution_subject_id.strip()
            or len(self.resolution_subject_id) > 240
        ):
            raise ValueError("resolution_subject_id must be non-empty")
        if (
            self.created_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.created_at
        ):
            raise ValueError("pending intake timestamps are invalid")
        if self.selected_action is not None:
            object.__setattr__(
                self,
                "selected_action",
                IntakeAction(self.selected_action),
            )
        if self.discovery_response is not None and not isinstance(
            self.discovery_response,
            JobDiscoveryResponse,
        ):
            raise TypeError(
                "discovery_response must be a JobDiscoveryResponse"
            )
        if (
            self.accepted_intent_write_result is not None
            and not isinstance(
                self.accepted_intent_write_result,
                AcceptedJobIntentWriteResult,
            )
        ):
            raise TypeError(
                "accepted_intent_write_result must be typed"
            )
        if self.resolved_at is not None and self.resolved_at.tzinfo is None:
            raise ValueError("resolved_at must include a timezone")
        if (
            self.accepted_intent_recorded_at is not None
            and self.accepted_intent_recorded_at.tzinfo is None
        ):
            raise ValueError(
                "accepted_intent_recorded_at must include a timezone"
            )
        source_ids = (
            self.source_candidate_set_id,
            self.source_candidate_id,
        )
        if any(value is not None for value in source_ids):
            if any(
                not isinstance(value, str) or not value
                for value in source_ids
            ):
                raise ValueError(
                    "candidate source IDs must both be non-empty"
                )
        if self.status is PendingIntakeStatus.WAITING_FOR_ACTION and (
            self.selected_action is not None
            or self.discovery_response is not None
            or self.accepted_intent_write_result is not None
            or self.accepted_intent_recorded_at is not None
            or self.resolved_at is not None
        ):
            raise ValueError("waiting pending intake has resolution state")
        if self.status is PendingIntakeStatus.RESOLVING and (
            self.resolution_subject_id is None
            or self.selected_action is None
            or self.discovery_response is not None
            or self.accepted_intent_write_result is not None
            or self.accepted_intent_recorded_at is not None
            or self.resolved_at is not None
        ):
            raise ValueError("resolving pending intake has conflicting state")
        if self.status is PendingIntakeStatus.PERSISTING_INTENT and (
            self.resolution_subject_id is None
            or self.selected_action is None
            or self.discovery_response is None
            or self.discovery_response.disposition
            is not DiscoveryDisposition.ACCEPTED
            or self.accepted_intent_write_result is not None
            or self.accepted_intent_recorded_at is None
            or self.resolved_at is not None
        ):
            raise ValueError("persisting pending intake is invalid")
        if (
            self.status
            is PendingIntakeStatus.INTENT_PERSISTENCE_FAILED
            and (
                self.selected_action is None
                or self.resolution_subject_id is None
                or self.discovery_response is None
                or self.discovery_response.disposition
                is not DiscoveryDisposition.ACCEPTED
                or self.accepted_intent_write_result is None
                or self.accepted_intent_write_result.status
                is not AcceptedJobIntentWriteStatus.FAILED
                or self.accepted_intent_recorded_at is None
                or self.resolved_at is not None
            )
        ):
            raise ValueError("failed intent persistence state is invalid")
        if self.status is PendingIntakeStatus.COMPLETED:
            if (
                self.selected_action is None
                or self.resolution_subject_id is None
                or self.discovery_response is None
                or self.resolved_at is None
            ):
                raise ValueError("completed pending intake is incomplete")
            accepted = (
                self.discovery_response.disposition
                is DiscoveryDisposition.ACCEPTED
            )
            write_succeeded = (
                self.accepted_intent_write_result is not None
                and self.accepted_intent_write_result.status
                in {
                    AcceptedJobIntentWriteStatus.CREATED,
                    AcceptedJobIntentWriteStatus.UNCHANGED,
                }
            )
            if accepted != write_succeeded:
                raise ValueError(
                    "completed pending intent persistence is inconsistent"
                )
            if accepted != (
                self.accepted_intent_recorded_at is not None
            ):
                raise ValueError(
                    "completed pending intent timestamp is inconsistent"
                )


@dataclass(frozen=True, slots=True)
class ConversationalIntakeResponse:
    status: IntakeResponseStatus
    reason_code: (
        IntakeReason | CandidateSelectionReason | ReadJobReason | None
    )
    retryable: bool
    pending_intake_id: str | None
    pending_status: PendingIntakeStatus | None
    summary: JobSummary | None
    actions: tuple[IntakeAction, ...]
    prompt: str
    selected_candidate_id: str | None = None
    intent_hint: NamedJobIntentHint = NamedJobIntentHint.UNSPECIFIED

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", IntakeResponseStatus(self.status))
        object.__setattr__(
            self,
            "intent_hint",
            NamedJobIntentHint(self.intent_hint),
        )
        object.__setattr__(
            self,
            "actions",
            tuple(IntakeAction(action) for action in self.actions),
        )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("prompt must be non-empty")
        if self.pending_status is not None:
            object.__setattr__(
                self,
                "pending_status",
                PendingIntakeStatus(self.pending_status),
            )
        if self.pending_intake_id is not None:
            if not self.pending_intake_id:
                raise ValueError("pending_intake_id must be non-empty")
        if self.summary is not None and not isinstance(self.summary, JobSummary):
            raise TypeError("summary must be a JobSummary")
        if self.selected_candidate_id is not None and (
            not isinstance(self.selected_candidate_id, str)
            or not self.selected_candidate_id
        ):
            raise ValueError("selected_candidate_id must be non-empty")


@dataclass(frozen=True, slots=True)
class NamedJobClues:
    company: str | None
    title: str | None
    location: str | None
    intent_hint: NamedJobIntentHint
    missing_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "intent_hint",
            NamedJobIntentHint(self.intent_hint),
        )
        for name, value in (
            ("company", self.company),
            ("title", self.title),
            ("location", self.location),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string or None")
        if not isinstance(self.missing_fields, tuple):
            raise TypeError("missing_fields must be a tuple")
        normalized_missing = tuple(
            str(field).strip() for field in self.missing_fields
        )
        if (
            any(field not in {"company", "title"} for field in normalized_missing)
            or len(normalized_missing) != len(set(normalized_missing))
        ):
            raise ValueError("missing_fields contains an invalid field")
        object.__setattr__(self, "missing_fields", normalized_missing)


@runtime_checkable
class NamedJobClueExtractor(Protocol):
    async def extract(self, message: str) -> NamedJobClues:
        """Extract bounded named-job clues without calling search or tools."""


@dataclass(frozen=True, slots=True)
class PendingCandidateSelection:
    candidate_set_id: str
    conversation_id: str
    candidate_set: CandidateSet
    intent_hint: NamedJobIntentHint
    search_request: JobSearchRequest
    status: CandidateSelectionStatus
    created_at: datetime
    expires_at: datetime
    selected_candidate_id: str | None = None
    pending_intake_id: str | None = None
    read_result: ReadJobResult | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "intent_hint",
            NamedJobIntentHint(self.intent_hint),
        )
        object.__setattr__(
            self,
            "status",
            CandidateSelectionStatus(self.status),
        )
        if (
            not isinstance(self.candidate_set_id, str)
            or not self.candidate_set_id
            or not isinstance(self.conversation_id, str)
            or not self.conversation_id
        ):
            raise ValueError("candidate selection IDs must be non-empty")
        if not isinstance(self.candidate_set, CandidateSet):
            raise TypeError("candidate_set must be a CandidateSet")
        if not self.candidate_set.candidates:
            raise ValueError("an empty CandidateSet cannot await selection")
        if self.candidate_set.candidate_set_id != self.candidate_set_id:
            raise ValueError("candidate_set_id does not match CandidateSet")
        if not isinstance(self.search_request, JobSearchRequest):
            raise TypeError("search_request must be a JobSearchRequest")
        if self.candidate_set.request_id != self.search_request.request_id:
            raise ValueError("search request does not match CandidateSet")
        if (
            not isinstance(self.created_at, datetime)
            or self.created_at.tzinfo is None
            or not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.created_at
        ):
            raise ValueError("candidate selection timestamps are invalid")
        if self.read_result is not None and not isinstance(
            self.read_result,
            ReadJobResult,
        ):
            raise TypeError("read_result must be a ReadJobResult")
        if self.status is CandidateSelectionStatus.WAITING_FOR_CANDIDATE_SELECTION:
            if (
                self.selected_candidate_id is not None
                or self.pending_intake_id is not None
                or self.read_result is not None
            ):
                raise ValueError("waiting candidate selection has result state")
        elif self.status is CandidateSelectionStatus.RESOLVING_CANDIDATE:
            if (
                self.selected_candidate_id is None
                or self.pending_intake_id is not None
                or self.read_result is not None
            ):
                raise ValueError("resolving candidate selection has conflicting state")
        elif (
            self.selected_candidate_id is None
            or self.pending_intake_id is None
            or self.read_result is None
            or self.read_result.status is not ReadJobStatus.SUCCEEDED
        ):
            raise ValueError("completed candidate selection is incomplete")


@dataclass(frozen=True, slots=True)
class CandidateSelectionRequest:
    conversation_id: str
    candidate_set_id: str
    candidate_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("conversation_id", self.conversation_id),
            ("candidate_set_id", self.candidate_set_id),
            ("candidate_id", self.candidate_id),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 320
            ):
                raise ValueError(f"{name} must be a non-empty string")


class _CandidateClaimStatus(str, Enum):
    CLAIMED = "CLAIMED"
    REPLAY = "REPLAY"
    NOT_FOUND = "NOT_FOUND"
    CONVERSATION_MISMATCH = "CONVERSATION_MISMATCH"
    EXPIRED = "EXPIRED"
    ALREADY_RESOLVED = "ALREADY_RESOLVED"
    CANDIDATE_NOT_FOUND = "CANDIDATE_NOT_FOUND"
    SOURCE_INVALID = "SOURCE_INVALID"


@dataclass(frozen=True, slots=True)
class _CandidateClaim:
    status: _CandidateClaimStatus
    pending: PendingCandidateSelection | None
    candidate: SearchCandidate | None = None


@dataclass(frozen=True, slots=True)
class NamedJobSearchResponse:
    status: IntakeResponseStatus
    reason_code: (
        IntakeReason | NamedSearchReason | JobSearchReason | None
    )
    retryable: bool
    candidate_set_id: str | None
    selection_status: CandidateSelectionStatus | None
    candidates: tuple[SearchCandidate, ...]
    intent_hint: NamedJobIntentHint
    missing_fields: tuple[str, ...]
    prompt: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", IntakeResponseStatus(self.status))
        object.__setattr__(
            self,
            "intent_hint",
            NamedJobIntentHint(self.intent_hint),
        )
        if self.selection_status is not None:
            object.__setattr__(
                self,
                "selection_status",
                CandidateSelectionStatus(self.selection_status),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if self.candidate_set_id is not None and not self.candidate_set_id:
            raise ValueError("candidate_set_id must be non-empty")
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(candidate, SearchCandidate)
            for candidate in self.candidates
        ):
            raise TypeError("candidates must be SearchCandidate values")
        if not isinstance(self.missing_fields, tuple):
            raise TypeError("missing_fields must be a tuple")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("prompt must be non-empty")
        if self.selection_status is not None and (
            self.candidate_set_id is None or not self.candidates
        ):
            raise ValueError("selection state requires non-empty candidates")
        if self.candidate_set_id is not None and self.selection_status is None:
            raise ValueError("candidate_set_id requires selection state")


@dataclass(frozen=True, slots=True)
class ResolvePendingIntakeRequest:
    subject_id: str
    conversation_id: str
    pending_intake_id: str
    action: IntakeAction | str

    def __post_init__(self) -> None:
        for name, value in (
            ("subject_id", self.subject_id),
            ("conversation_id", self.conversation_id),
            ("pending_intake_id", self.pending_intake_id),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 240
            ):
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.action, (str, IntakeAction)):
            raise TypeError("action must be a string or IntakeAction")


@dataclass(frozen=True, slots=True)
class ResolvePendingIntakeResponse:
    status: ResolveIntakeStatus
    reason_code: ResolveIntakeReason | DiscoveryReason | None
    retryable: bool
    pending_intake_id: str
    selected_action: IntakeAction | None
    discovery_response: JobDiscoveryResponse | None
    accepted_intent_write_result: AcceptedJobIntentWriteResult | None
    job_id: str | None
    change: DiscoveryChange | None
    summary: JobSummary | None
    prompt: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ResolveIntakeStatus(self.status))
        if self.selected_action is not None:
            object.__setattr__(
                self,
                "selected_action",
                IntakeAction(self.selected_action),
            )
        if self.change is not None:
            object.__setattr__(self, "change", DiscoveryChange(self.change))
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if (
            not isinstance(self.pending_intake_id, str)
            or not self.pending_intake_id
        ):
            raise ValueError("pending_intake_id must be non-empty")
        if self.discovery_response is not None and not isinstance(
            self.discovery_response,
            JobDiscoveryResponse,
        ):
            raise TypeError(
                "discovery_response must be a JobDiscoveryResponse"
            )
        if (
            self.accepted_intent_write_result is not None
            and not isinstance(
                self.accepted_intent_write_result,
                AcceptedJobIntentWriteResult,
            )
        ):
            raise TypeError(
                "accepted_intent_write_result must be typed"
            )
        if self.summary is not None and not isinstance(self.summary, JobSummary):
            raise TypeError("summary must be a JobSummary")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("prompt must be non-empty")


class _PendingClaimStatus(str, Enum):
    CLAIMED = "CLAIMED"
    RESUME_INTENT_PERSISTENCE = "RESUME_INTENT_PERSISTENCE"
    REPLAY = "REPLAY"
    NOT_FOUND = "NOT_FOUND"
    CONVERSATION_MISMATCH = "CONVERSATION_MISMATCH"
    SUBJECT_MISMATCH = "SUBJECT_MISMATCH"
    EXPIRED = "EXPIRED"
    ALREADY_RESOLVED = "ALREADY_RESOLVED"


@dataclass(frozen=True, slots=True)
class _PendingClaim:
    status: _PendingClaimStatus
    pending: PendingIntake | None


class InMemoryPendingIntakeStore:
    """Process-local pending state; not a Discovery or JobPosting repository."""

    def __init__(
        self,
        *,
        ttl: timedelta,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be a positive timedelta")
        self._ttl = ttl
        self._id_factory = id_factory or (
            lambda: f"pending-{uuid4()}"
        )
        self._items: dict[str, PendingIntake] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        conversation_id: str,
        observation: SourceJobObservation,
        created_at: datetime,
        intent_hint: NamedJobIntentHint = NamedJobIntentHint.UNSPECIFIED,
        source_candidate_set_id: str | None = None,
        source_candidate_id: str | None = None,
    ) -> PendingIntake:
        with self._lock:
            pending_intake_id = self._id_factory()
            if (
                not isinstance(pending_intake_id, str)
                or not pending_intake_id
                or pending_intake_id in self._items
            ):
                raise ValueError("pending intake id is invalid or duplicated")
            pending = PendingIntake(
                pending_intake_id=pending_intake_id,
                conversation_id=conversation_id,
                observation=observation,
                status=PendingIntakeStatus.WAITING_FOR_ACTION,
                created_at=created_at,
                expires_at=created_at + self._ttl,
                intent_hint=intent_hint,
                source_candidate_set_id=source_candidate_set_id,
                source_candidate_id=source_candidate_id,
            )
            self._items[pending_intake_id] = pending
            return pending

    def get(
        self,
        pending_intake_id: str,
        *,
        now: datetime | None = None,
    ) -> PendingIntake | None:
        with self._lock:
            pending = self._items.get(pending_intake_id)
            if pending is None:
                return None
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                raise ValueError("now must include a timezone")
            if current >= pending.expires_at:
                self._items.pop(pending_intake_id, None)
                return None
            return pending

    def claim(
        self,
        *,
        pending_intake_id: str,
        subject_id: str,
        conversation_id: str,
        action: IntakeAction,
        now: datetime,
    ) -> _PendingClaim:
        """Atomically reserve one pending intake for one Discovery call."""
        if now.tzinfo is None:
            raise ValueError("now must include a timezone")
        with self._lock:
            pending = self._items.get(pending_intake_id)
            if pending is None:
                return _PendingClaim(_PendingClaimStatus.NOT_FOUND, None)
            if pending.conversation_id != conversation_id:
                return _PendingClaim(
                    _PendingClaimStatus.CONVERSATION_MISMATCH,
                    pending,
                )
            if (
                pending.resolution_subject_id is not None
                and pending.resolution_subject_id != subject_id
            ):
                return _PendingClaim(
                    _PendingClaimStatus.SUBJECT_MISMATCH,
                    pending,
                )
            if now >= pending.expires_at:
                self._items.pop(pending_intake_id, None)
                return _PendingClaim(_PendingClaimStatus.EXPIRED, pending)
            if pending.status is PendingIntakeStatus.COMPLETED:
                status = (
                    _PendingClaimStatus.REPLAY
                    if pending.selected_action is action
                    else _PendingClaimStatus.ALREADY_RESOLVED
                )
                return _PendingClaim(status, pending)
            if (
                pending.status
                is PendingIntakeStatus.INTENT_PERSISTENCE_FAILED
            ):
                if pending.selected_action is not action:
                    return _PendingClaim(
                        _PendingClaimStatus.ALREADY_RESOLVED,
                        pending,
                    )
                resumed = replace(
                    pending,
                    status=PendingIntakeStatus.PERSISTING_INTENT,
                    accepted_intent_write_result=None,
                )
                self._items[pending_intake_id] = resumed
                return _PendingClaim(
                    _PendingClaimStatus.RESUME_INTENT_PERSISTENCE,
                    resumed,
                )
            if pending.status in {
                PendingIntakeStatus.RESOLVING,
                PendingIntakeStatus.PERSISTING_INTENT,
            }:
                return _PendingClaim(
                    _PendingClaimStatus.ALREADY_RESOLVED,
                    pending,
                )
            claimed = replace(
                pending,
                status=PendingIntakeStatus.RESOLVING,
                resolution_subject_id=subject_id,
                selected_action=action,
            )
            self._items[pending_intake_id] = claimed
            return _PendingClaim(_PendingClaimStatus.CLAIMED, claimed)

    def retain_accepted_discovery(
        self,
        *,
        pending_intake_id: str,
        action: IntakeAction,
        discovery_response: JobDiscoveryResponse,
        recorded_at: datetime,
    ) -> PendingIntake:
        """Retain accepted Discovery identity before durable intent writing."""
        if (
            discovery_response.disposition
            is not DiscoveryDisposition.ACCEPTED
        ):
            raise ValueError("Discovery response must be accepted")
        if recorded_at.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
        with self._lock:
            pending = self._items.get(pending_intake_id)
            if (
                pending is None
                or pending.status is not PendingIntakeStatus.RESOLVING
                or pending.selected_action is not action
            ):
                raise RuntimeError("pending intake resolution claim was lost")
            retained = replace(
                pending,
                status=PendingIntakeStatus.PERSISTING_INTENT,
                discovery_response=discovery_response,
                accepted_intent_recorded_at=recorded_at,
            )
            self._items[pending_intake_id] = retained
            return retained

    def fail_intent_persistence(
        self,
        *,
        pending_intake_id: str,
        action: IntakeAction,
        write_result: AcceptedJobIntentWriteResult,
    ) -> PendingIntake:
        if (
            write_result.status
            is not AcceptedJobIntentWriteStatus.FAILED
        ):
            raise ValueError("write_result must be failed")
        with self._lock:
            pending = self._items.get(pending_intake_id)
            if (
                pending is None
                or pending.status
                is not PendingIntakeStatus.PERSISTING_INTENT
                or pending.selected_action is not action
            ):
                raise RuntimeError("pending intent persistence claim was lost")
            failed = replace(
                pending,
                status=PendingIntakeStatus.INTENT_PERSISTENCE_FAILED,
                accepted_intent_write_result=write_result,
            )
            self._items[pending_intake_id] = failed
            return failed

    def complete(
        self,
        *,
        pending_intake_id: str,
        action: IntakeAction,
        discovery_response: JobDiscoveryResponse,
        resolved_at: datetime,
        accepted_intent_write_result: (
            AcceptedJobIntentWriteResult | None
        ) = None,
    ) -> PendingIntake:
        if resolved_at.tzinfo is None:
            raise ValueError("resolved_at must include a timezone")
        with self._lock:
            pending = self._items.get(pending_intake_id)
            accepted = (
                discovery_response.disposition
                is DiscoveryDisposition.ACCEPTED
            )
            expected_status = (
                PendingIntakeStatus.PERSISTING_INTENT
                if accepted
                else PendingIntakeStatus.RESOLVING
            )
            write_succeeded = (
                accepted_intent_write_result is not None
                and accepted_intent_write_result.status
                in {
                    AcceptedJobIntentWriteStatus.CREATED,
                    AcceptedJobIntentWriteStatus.UNCHANGED,
                }
            )
            if (
                pending is None
                or pending.status is not expected_status
                or pending.selected_action is not action
                or accepted != write_succeeded
            ):
                raise RuntimeError("pending intake resolution claim was lost")
            completed = replace(
                pending,
                status=PendingIntakeStatus.COMPLETED,
                discovery_response=discovery_response,
                accepted_intent_write_result=(
                    accepted_intent_write_result
                ),
                resolved_at=resolved_at,
            )
            self._items[pending_intake_id] = completed
            return completed

    def release(
        self,
        *,
        pending_intake_id: str,
        action: IntakeAction,
    ) -> None:
        """Return an uncompleted claim to a safely retryable state."""
        with self._lock:
            pending = self._items.get(pending_intake_id)
            if (
                pending is not None
                and pending.status is PendingIntakeStatus.RESOLVING
                and pending.selected_action is action
            ):
                self._items[pending_intake_id] = replace(
                    pending,
                    status=PendingIntakeStatus.WAITING_FOR_ACTION,
                    selected_action=None,
                )

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._items)


class InMemoryCandidateSelectionStore:
    """Process-local CandidateSet state; never a JobPosting repository."""

    def __init__(self, *, ttl: timedelta) -> None:
        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be a positive timedelta")
        self._ttl = ttl
        self._items: dict[str, PendingCandidateSelection] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        conversation_id: str,
        candidate_set: CandidateSet,
        intent_hint: NamedJobIntentHint,
        search_request: JobSearchRequest,
        created_at: datetime,
    ) -> PendingCandidateSelection:
        with self._lock:
            candidate_set_id = candidate_set.candidate_set_id
            if candidate_set_id in self._items:
                raise ValueError("candidate set is already pending selection")
            pending = PendingCandidateSelection(
                candidate_set_id=candidate_set_id,
                conversation_id=conversation_id,
                candidate_set=candidate_set,
                intent_hint=intent_hint,
                search_request=search_request,
                status=(
                    CandidateSelectionStatus.WAITING_FOR_CANDIDATE_SELECTION
                ),
                created_at=created_at,
                expires_at=created_at + self._ttl,
            )
            self._items[candidate_set_id] = pending
            return pending

    def get(
        self,
        candidate_set_id: str,
        *,
        now: datetime | None = None,
    ) -> PendingCandidateSelection | None:
        with self._lock:
            pending = self._items.get(candidate_set_id)
            if pending is None:
                return None
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                raise ValueError("now must include a timezone")
            if current >= pending.expires_at:
                self._items.pop(candidate_set_id, None)
                return None
            return pending

    def claim(
        self,
        *,
        candidate_set_id: str,
        conversation_id: str,
        candidate_id: str,
        now: datetime,
    ) -> _CandidateClaim:
        """Atomically reserve one candidate for one public read."""
        if now.tzinfo is None:
            raise ValueError("now must include a timezone")
        with self._lock:
            pending = self._items.get(candidate_set_id)
            if pending is None:
                return _CandidateClaim(_CandidateClaimStatus.NOT_FOUND, None)
            if pending.conversation_id != conversation_id:
                return _CandidateClaim(
                    _CandidateClaimStatus.CONVERSATION_MISMATCH,
                    pending,
                )
            if now >= pending.expires_at:
                self._items.pop(candidate_set_id, None)
                return _CandidateClaim(_CandidateClaimStatus.EXPIRED, pending)
            if pending.status is CandidateSelectionStatus.COMPLETED:
                status = (
                    _CandidateClaimStatus.REPLAY
                    if pending.selected_candidate_id == candidate_id
                    else _CandidateClaimStatus.ALREADY_RESOLVED
                )
                return _CandidateClaim(status, pending)
            if pending.status is CandidateSelectionStatus.RESOLVING_CANDIDATE:
                return _CandidateClaim(
                    _CandidateClaimStatus.ALREADY_RESOLVED,
                    pending,
                )

            candidate = next(
                (
                    item
                    for item in pending.candidate_set.candidates
                    if item.candidate_id == candidate_id
                ),
                None,
            )
            if candidate is None:
                return _CandidateClaim(
                    _CandidateClaimStatus.CANDIDATE_NOT_FOUND,
                    pending,
                )
            if not _valid_candidate_source_url(candidate.source_url):
                return _CandidateClaim(
                    _CandidateClaimStatus.SOURCE_INVALID,
                    pending,
                    candidate,
                )

            claimed = replace(
                pending,
                status=CandidateSelectionStatus.RESOLVING_CANDIDATE,
                selected_candidate_id=candidate_id,
            )
            self._items[candidate_set_id] = claimed
            return _CandidateClaim(
                _CandidateClaimStatus.CLAIMED,
                claimed,
                candidate,
            )

    def complete(
        self,
        *,
        candidate_set_id: str,
        candidate_id: str,
        pending_intake_id: str,
        read_result: ReadJobResult,
    ) -> PendingCandidateSelection:
        with self._lock:
            pending = self._items.get(candidate_set_id)
            if (
                pending is None
                or pending.status
                is not CandidateSelectionStatus.RESOLVING_CANDIDATE
                or pending.selected_candidate_id != candidate_id
            ):
                raise RuntimeError("candidate selection claim was lost")
            completed = replace(
                pending,
                status=CandidateSelectionStatus.COMPLETED,
                pending_intake_id=pending_intake_id,
                read_result=read_result,
            )
            self._items[candidate_set_id] = completed
            return completed

    def release(
        self,
        *,
        candidate_set_id: str,
        candidate_id: str,
    ) -> None:
        """Return an unsuccessful read claim to explicit-retry state."""
        with self._lock:
            pending = self._items.get(candidate_set_id)
            if (
                pending is not None
                and pending.status
                is CandidateSelectionStatus.RESOLVING_CANDIDATE
                and pending.selected_candidate_id == candidate_id
            ):
                self._items[candidate_set_id] = replace(
                    pending,
                    status=(
                        CandidateSelectionStatus
                        .WAITING_FOR_CANDIDATE_SELECTION
                    ),
                    selected_candidate_id=None,
                )

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._items)


PublicJobReader = Callable[[ReadJobRequest], Awaitable[ReadJobResult]]
_JobDiscoveryPort = Callable[[JobDiscoveryRequest], JobDiscoveryResponse]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _extract_urls(message: str) -> tuple[str, ...]:
    return tuple(
        match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)
        for match in _URL_PATTERN.finditer(message)
    )


def _valid_candidate_source_url(value: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and port in {None, 80, 443}
    )


def _job_summary(observation: SourceJobObservation) -> JobSummary:
    return JobSummary(
        company=observation.company,
        title=observation.title,
        location=observation.location,
        source_platform=observation.source_platform,
    )


def _waiting_for_action_response(
    *,
    pending_intake_id: str,
    observation: SourceJobObservation,
    intent_hint: NamedJobIntentHint = NamedJobIntentHint.UNSPECIFIED,
    selected_candidate_id: str | None = None,
) -> ConversationalIntakeResponse:
    summary = _job_summary(observation)
    location = (
        f", located in {summary.location}"
        if summary.location
        else ""
    )
    return ConversationalIntakeResponse(
        status=IntakeResponseStatus.NEEDS_USER,
        reason_code=None,
        retryable=False,
        pending_intake_id=pending_intake_id,
        pending_status=PendingIntakeStatus.WAITING_FOR_ACTION,
        summary=summary,
        actions=(
            IntakeAction.ADD_JOB,
            IntakeAction.REQUEST_APPLICATION,
        ),
        prompt=(
            f"Identified {summary.company} — {summary.title}{location}. "
            "Would you like to add it to your job list or start preparing "
            "an application?"
        ),
        selected_candidate_id=selected_candidate_id,
        intent_hint=intent_hint,
    )


def _needs_user_response(
    reason: IntakeReason,
) -> ConversationalIntakeResponse:
    prompts = {
        IntakeReason.NEEDS_MORE_INFORMATION: (
            "Please provide one absolute job URL."
        ),
        IntakeReason.NEEDS_USER_SELECTION: (
            "I found multiple URLs. Please choose and send exactly one job URL."
        ),
    }
    return ConversationalIntakeResponse(
        status=IntakeResponseStatus.NEEDS_USER,
        reason_code=reason,
        retryable=False,
        pending_intake_id=None,
        pending_status=None,
        summary=None,
        actions=(),
        prompt=prompts[reason],
    )


def _read_failure_response(
    result: ReadJobResult,
) -> ConversationalIntakeResponse:
    if result.reason_code is None:
        raise ValueError("failed read result has no reason code")
    prompts = {
        ReadJobReason.INVALID_URL: "The job URL is invalid.",
        ReadJobReason.UNSAFE_URL: (
            "This URL was blocked by the public network safety policy."
        ),
        ReadJobReason.UNSUPPORTED_URL: (
            "This public page cannot currently be read as a job posting."
        ),
        ReadJobReason.JOB_NOT_FOUND: "The job posting was not found.",
        ReadJobReason.JOB_CLOSED: "The job posting is closed.",
        ReadJobReason.SOURCE_TIMEOUT: "The job source timed out.",
        ReadJobReason.SOURCE_RATE_LIMITED: (
            "The job source is temporarily rate limited."
        ),
        ReadJobReason.SOURCE_RESPONSE_INVALID: (
            "The job source returned an invalid response."
        ),
        ReadJobReason.SOURCE_UNAVAILABLE: (
            "The job source is currently unavailable."
        ),
    }
    status = (
        IntakeResponseStatus.UNSUPPORTED
        if result.status is ReadJobStatus.UNSUPPORTED
        else IntakeResponseStatus.FAILED
    )
    return ConversationalIntakeResponse(
        status=status,
        reason_code=result.reason_code,
        retryable=result.retryable,
        pending_intake_id=None,
        pending_status=None,
        summary=None,
        actions=(),
        prompt=prompts[result.reason_code],
    )


async def handle_conversational_url_intake(
    request: ConversationalIntakeRequest,
    *,
    pending_store: InMemoryPendingIntakeStore,
    reader: PublicJobReader = read_public_job,
    clock: Callable[[], datetime] = _utc_now,
) -> ConversationalIntakeResponse:
    """Read exactly one URL and stop at an explicit add/apply choice."""
    if not isinstance(request, ConversationalIntakeRequest):
        raise TypeError("request must be a ConversationalIntakeRequest")
    if not isinstance(pending_store, InMemoryPendingIntakeStore):
        raise TypeError("pending_store must be an InMemoryPendingIntakeStore")

    urls = _extract_urls(request.message)
    if not urls:
        return _needs_user_response(IntakeReason.NEEDS_MORE_INFORMATION)
    if len(urls) != 1:
        return _needs_user_response(IntakeReason.NEEDS_USER_SELECTION)

    result = await reader(ReadJobRequest(url=urls[0]))
    if not isinstance(result, ReadJobResult):
        raise TypeError("reader must return a ReadJobResult")
    if result.status is not ReadJobStatus.SUCCEEDED:
        return _read_failure_response(result)
    if result.observation is None:
        raise ValueError("successful read result has no observation")

    created_at = clock()
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    pending = pending_store.create(
        conversation_id=request.conversation_id.strip(),
        observation=result.observation,
        created_at=created_at,
    )
    return _waiting_for_action_response(
        pending_intake_id=pending.pending_intake_id,
        observation=result.observation,
    )


def _clue_failure_response(
    reason: IntakeReason | JobSearchReason,
    *,
    missing_fields: tuple[str, ...] = (),
) -> NamedJobSearchResponse:
    if reason is IntakeReason.NEEDS_MORE_INFORMATION:
        missing = ", ".join(missing_fields)
        prompt = f"Please provide the missing job information: {missing}."
        status = IntakeResponseStatus.NEEDS_USER
    else:
        prompt = "The extracted job search clues were invalid."
        status = IntakeResponseStatus.FAILED
    return NamedJobSearchResponse(
        status=status,
        reason_code=reason,
        retryable=False,
        candidate_set_id=None,
        selection_status=None,
        candidates=(),
        intent_hint=NamedJobIntentHint.UNSPECIFIED,
        missing_fields=missing_fields,
        prompt=prompt,
    )


def _validated_named_clues(
    value: object,
) -> tuple[NamedJobClues | None, tuple[str, ...]]:
    if not isinstance(value, NamedJobClues):
        return None, ()
    for field_value, maximum in (
        (value.company, 240),
        (value.title, 240),
        (value.location, 320),
    ):
        if field_value is not None and len(field_value.strip()) > maximum:
            return None, ()
    if value.location is not None and not value.location.strip():
        return None, ()
    missing = list(value.missing_fields)
    if value.company is None or not value.company.strip():
        missing.append("company")
    if value.title is None or not value.title.strip():
        missing.append("title")
    return value, tuple(dict.fromkeys(missing))


def _search_failure_response(
    result: JobSearchResult,
    *,
    intent_hint: NamedJobIntentHint,
) -> NamedJobSearchResponse:
    if result.reason_code is None:
        raise ValueError("failed search result has no reason code")
    prompts = {
        JobSearchReason.INVALID_REQUEST: "The job search request was invalid.",
        JobSearchReason.UNSUPPORTED_COMPANY: (
            "This company is not configured for bounded job search."
        ),
        JobSearchReason.PROVIDER_CONFIGURATION_ERROR: (
            "The configured job search provider is invalid."
        ),
        JobSearchReason.SOURCE_TIMEOUT: "The job search source timed out.",
        JobSearchReason.SOURCE_RATE_LIMITED: (
            "The job search source is temporarily rate limited."
        ),
        JobSearchReason.NETWORK_UNAVAILABLE: (
            "The job search network is currently unavailable."
        ),
        JobSearchReason.HTTP_ERROR: (
            "The job search source returned an HTTP error."
        ),
        JobSearchReason.REDIRECT_REJECTED: (
            "The job search source redirected outside its configured boundary."
        ),
        JobSearchReason.RESPONSE_TOO_LARGE: (
            "The job search source response exceeded the server limit."
        ),
        JobSearchReason.UNSUPPORTED_CONTENT_TYPE: (
            "The job search source returned an unsupported content type."
        ),
        JobSearchReason.MALFORMED_RESPONSE: (
            "The job search source returned malformed content."
        ),
        JobSearchReason.CANDIDATE_VALIDATION_FAILED: (
            "The job search source returned an invalid candidate record."
        ),
        JobSearchReason.SOURCE_RESPONSE_INVALID: (
            "The job search source returned an invalid response."
        ),
        JobSearchReason.SOURCE_UNAVAILABLE: (
            "The job search source is currently unavailable."
        ),
    }
    return NamedJobSearchResponse(
        status=(
            IntakeResponseStatus.UNSUPPORTED
            if result.status is JobSearchStatus.UNSUPPORTED
            else IntakeResponseStatus.FAILED
        ),
        reason_code=result.reason_code,
        retryable=result.retryable,
        candidate_set_id=None,
        selection_status=None,
        candidates=(),
        intent_hint=intent_hint,
        missing_fields=(),
        prompt=prompts[result.reason_code],
    )


async def handle_conversational_intake(
    request: ConversationalIntakeRequest,
    *,
    pending_store: InMemoryPendingIntakeStore,
    candidate_store: InMemoryCandidateSelectionStore,
    clue_extractor: NamedJobClueExtractor,
    job_search_port: JobSearchPort,
    reader: PublicJobReader = read_public_job,
    clock: Callable[[], datetime] = _utc_now,
    request_id_factory: Callable[[], str] | None = None,
) -> ConversationalIntakeResponse | NamedJobSearchResponse:
    """Route URL intake first, otherwise run one typed named-job search."""
    if not isinstance(request, ConversationalIntakeRequest):
        raise TypeError("request must be a ConversationalIntakeRequest")
    if _extract_urls(request.message):
        return await handle_conversational_url_intake(
            request,
            pending_store=pending_store,
            reader=reader,
            clock=clock,
        )
    if not isinstance(candidate_store, InMemoryCandidateSelectionStore):
        raise TypeError(
            "candidate_store must be an InMemoryCandidateSelectionStore"
        )
    if not isinstance(clue_extractor, NamedJobClueExtractor):
        raise TypeError("clue_extractor must implement NamedJobClueExtractor")
    if not isinstance(job_search_port, JobSearchPort):
        raise TypeError("job_search_port must implement JobSearchPort")

    clues, missing_fields = _validated_named_clues(
        await clue_extractor.extract(request.message)
    )
    if clues is None:
        return _clue_failure_response(JobSearchReason.INVALID_REQUEST)
    if missing_fields:
        response = _clue_failure_response(
            IntakeReason.NEEDS_MORE_INFORMATION,
            missing_fields=missing_fields,
        )
        return replace(response, intent_hint=clues.intent_hint)

    request_id = (
        request_id_factory or (lambda: f"search-request-{uuid4()}")
    )()
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id_factory must return a non-empty string")
    search_request = JobSearchRequest(
        request_id=request_id.strip(),
        company=clues.company.strip(),
        title=clues.title.strip(),
        location=(
            clues.location.strip()
            if clues.location is not None
            else None
        ),
    )
    result = await search_jobs(search_request, port=job_search_port)
    if result.status is not JobSearchStatus.SUCCEEDED:
        return _search_failure_response(
            result,
            intent_hint=clues.intent_hint,
        )
    if result.candidate_set is None:
        raise ValueError("successful search result has no CandidateSet")
    if not result.candidate_set.candidates:
        return NamedJobSearchResponse(
            status=IntakeResponseStatus.NEEDS_USER,
            reason_code=NamedSearchReason.NO_CANDIDATES,
            retryable=False,
            candidate_set_id=None,
            selection_status=None,
            candidates=(),
            intent_hint=clues.intent_hint,
            missing_fields=(),
            prompt=(
                "No matching jobs were found. Please provide a more precise "
                "title or location."
            ),
        )

    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    pending = candidate_store.create(
        conversation_id=request.conversation_id.strip(),
        candidate_set=result.candidate_set,
        intent_hint=clues.intent_hint,
        search_request=search_request,
        created_at=now,
    )
    count = len(result.candidate_set.candidates)
    return NamedJobSearchResponse(
        status=IntakeResponseStatus.NEEDS_USER,
        reason_code=None,
        retryable=False,
        candidate_set_id=pending.candidate_set_id,
        selection_status=pending.status,
        candidates=result.candidate_set.candidates,
        intent_hint=clues.intent_hint,
        missing_fields=(),
        prompt=(
            f"Found {count} matching job candidate"
            f"{'s' if count != 1 else ''}. Please select one."
        ),
    )


def _candidate_selection_failure_response(
    reason: CandidateSelectionReason,
    *,
    selected_candidate_id: str | None = None,
    intent_hint: NamedJobIntentHint = NamedJobIntentHint.UNSPECIFIED,
) -> ConversationalIntakeResponse:
    prompts = {
        CandidateSelectionReason.CANDIDATE_SET_NOT_FOUND: (
            "This candidate set was not found."
        ),
        CandidateSelectionReason.CONVERSATION_MISMATCH: (
            "This candidate set belongs to a different conversation."
        ),
        CandidateSelectionReason.CANDIDATE_SET_EXPIRED: (
            "This candidate set has expired. Please run the search again."
        ),
        CandidateSelectionReason.CANDIDATE_SET_ALREADY_RESOLVED: (
            "This candidate set is already resolving or was completed with "
            "a different candidate."
        ),
        CandidateSelectionReason.CANDIDATE_NOT_FOUND: (
            "The selected candidate is not in this candidate set."
        ),
        CandidateSelectionReason.CANDIDATE_SOURCE_INVALID: (
            "The selected candidate has an invalid source URL."
        ),
    }
    return ConversationalIntakeResponse(
        status=IntakeResponseStatus.FAILED,
        reason_code=reason,
        retryable=False,
        pending_intake_id=None,
        pending_status=None,
        summary=None,
        actions=(),
        prompt=prompts[reason],
        selected_candidate_id=selected_candidate_id,
        intent_hint=intent_hint,
    )


async def select_search_candidate(
    request: CandidateSelectionRequest,
    *,
    candidate_store: InMemoryCandidateSelectionStore,
    pending_store: InMemoryPendingIntakeStore,
    reader: PublicJobReader = read_public_job,
    clock: Callable[[], datetime] = _utc_now,
) -> ConversationalIntakeResponse:
    """Read one explicitly selected candidate and stop at add/apply choice."""
    if not isinstance(request, CandidateSelectionRequest):
        raise TypeError("request must be a CandidateSelectionRequest")
    if not isinstance(candidate_store, InMemoryCandidateSelectionStore):
        raise TypeError(
            "candidate_store must be an InMemoryCandidateSelectionStore"
        )
    if not isinstance(pending_store, InMemoryPendingIntakeStore):
        raise TypeError("pending_store must be an InMemoryPendingIntakeStore")
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")

    claim = candidate_store.claim(
        candidate_set_id=request.candidate_set_id.strip(),
        conversation_id=request.conversation_id.strip(),
        candidate_id=request.candidate_id.strip(),
        now=now,
    )
    reason_by_claim = {
        _CandidateClaimStatus.NOT_FOUND: (
            CandidateSelectionReason.CANDIDATE_SET_NOT_FOUND
        ),
        _CandidateClaimStatus.CONVERSATION_MISMATCH: (
            CandidateSelectionReason.CONVERSATION_MISMATCH
        ),
        _CandidateClaimStatus.EXPIRED: (
            CandidateSelectionReason.CANDIDATE_SET_EXPIRED
        ),
        _CandidateClaimStatus.ALREADY_RESOLVED: (
            CandidateSelectionReason.CANDIDATE_SET_ALREADY_RESOLVED
        ),
        _CandidateClaimStatus.CANDIDATE_NOT_FOUND: (
            CandidateSelectionReason.CANDIDATE_NOT_FOUND
        ),
        _CandidateClaimStatus.SOURCE_INVALID: (
            CandidateSelectionReason.CANDIDATE_SOURCE_INVALID
        ),
    }
    if claim.status in reason_by_claim:
        pending = claim.pending
        return _candidate_selection_failure_response(
            reason_by_claim[claim.status],
            selected_candidate_id=(
                pending.selected_candidate_id
                if pending is not None
                and pending.selected_candidate_id is not None
                else request.candidate_id.strip()
            ),
            intent_hint=(
                pending.intent_hint
                if pending is not None
                else NamedJobIntentHint.UNSPECIFIED
            ),
        )
    if claim.status is _CandidateClaimStatus.REPLAY:
        pending = claim.pending
        if (
            pending is None
            or pending.pending_intake_id is None
            or pending.read_result is None
            or pending.read_result.observation is None
            or pending.selected_candidate_id is None
        ):
            raise RuntimeError("completed candidate selection has no result")
        return _waiting_for_action_response(
            pending_intake_id=pending.pending_intake_id,
            observation=pending.read_result.observation,
            intent_hint=pending.intent_hint,
            selected_candidate_id=pending.selected_candidate_id,
        )
    if (
        claim.status is not _CandidateClaimStatus.CLAIMED
        or claim.pending is None
        or claim.candidate is None
    ):
        raise RuntimeError("candidate selection claim is invalid")

    try:
        result = await reader(
            ReadJobRequest(url=claim.candidate.source_url)
        )
        if not isinstance(result, ReadJobResult):
            raise TypeError("reader must return a ReadJobResult")
    except Exception:
        candidate_store.release(
            candidate_set_id=claim.pending.candidate_set_id,
            candidate_id=claim.candidate.candidate_id,
        )
        response = _read_failure_response(
            ReadJobResult.failed(ReadJobReason.SOURCE_UNAVAILABLE)
        )
        return replace(
            response,
            selected_candidate_id=claim.candidate.candidate_id,
            intent_hint=claim.pending.intent_hint,
        )

    if result.status is not ReadJobStatus.SUCCEEDED:
        candidate_store.release(
            candidate_set_id=claim.pending.candidate_set_id,
            candidate_id=claim.candidate.candidate_id,
        )
        return replace(
            _read_failure_response(result),
            selected_candidate_id=claim.candidate.candidate_id,
            intent_hint=claim.pending.intent_hint,
        )
    if result.observation is None:
        raise ValueError("successful read result has no observation")

    try:
        pending_intake = pending_store.create(
            conversation_id=claim.pending.conversation_id,
            observation=result.observation,
            created_at=clock(),
            intent_hint=claim.pending.intent_hint,
            source_candidate_set_id=claim.pending.candidate_set_id,
            source_candidate_id=claim.candidate.candidate_id,
        )
    except Exception:
        candidate_store.release(
            candidate_set_id=claim.pending.candidate_set_id,
            candidate_id=claim.candidate.candidate_id,
        )
        response = _read_failure_response(
            ReadJobResult.failed(ReadJobReason.SOURCE_UNAVAILABLE)
        )
        return replace(
            response,
            selected_candidate_id=claim.candidate.candidate_id,
            intent_hint=claim.pending.intent_hint,
        )

    candidate_store.complete(
        candidate_set_id=claim.pending.candidate_set_id,
        candidate_id=claim.candidate.candidate_id,
        pending_intake_id=pending_intake.pending_intake_id,
        read_result=result,
    )
    return _waiting_for_action_response(
        pending_intake_id=pending_intake.pending_intake_id,
        observation=result.observation,
        intent_hint=claim.pending.intent_hint,
        selected_candidate_id=claim.candidate.candidate_id,
    )


def _resolve_failure_response(
    request: ResolvePendingIntakeRequest,
    reason: ResolveIntakeReason,
    *,
    retryable: bool = False,
    selected_action: IntakeAction | None = None,
) -> ResolvePendingIntakeResponse:
    prompts = {
        ResolveIntakeReason.PENDING_INTAKE_NOT_FOUND: (
            "This pending job intake was not found."
        ),
        ResolveIntakeReason.CONVERSATION_MISMATCH: (
            "This pending job intake belongs to a different conversation."
        ),
        ResolveIntakeReason.SUBJECT_MISMATCH: (
            "This pending job intake belongs to a different subject."
        ),
        ResolveIntakeReason.PENDING_INTAKE_EXPIRED: (
            "This pending job intake has expired. Please read the job URL "
            "again."
        ),
        ResolveIntakeReason.PENDING_INTAKE_ALREADY_RESOLVED: (
            "This pending job intake has already been resolved with a "
            "different action."
        ),
        ResolveIntakeReason.INVALID_ACTION: (
            "Choose either ADD_JOB or REQUEST_APPLICATION."
        ),
        ResolveIntakeReason.PENDING_OBSERVATION_INVALID: (
            "The pending job observation is invalid and was not sent to "
            "Job Discovery."
        ),
        ResolveIntakeReason.DISCOVERY_TEMPORARILY_UNAVAILABLE: (
            "Job Discovery did not return a result. This action can be "
            "safely retried."
        ),
    }
    return ResolvePendingIntakeResponse(
        status=ResolveIntakeStatus.FAILED,
        reason_code=reason,
        retryable=retryable,
        pending_intake_id=request.pending_intake_id.strip(),
        selected_action=selected_action,
        discovery_response=None,
        accepted_intent_write_result=None,
        job_id=None,
        change=None,
        summary=None,
        prompt=prompts[reason],
    )


def _candidate_from_observation(
    observation: SourceJobObservation,
) -> ResolvedJobCandidate:
    return ResolvedJobCandidate(
        source_platform=observation.source_platform.value,
        source_job_id=observation.source_job_id,
        source_url=observation.source_url,
        application_url=observation.application_url,
        company=observation.company,
        title=observation.title,
        description=observation.description,
        location=observation.location,
        work_mode=observation.work_mode.value,
        posted_at=observation.posted_at,
        ats_type=observation.ats_type.value,
    )


def _discovery_result_response(
    pending: PendingIntake,
) -> ResolvePendingIntakeResponse:
    observation = pending.observation
    discovery_response = pending.discovery_response
    action = pending.selected_action
    if (
        not isinstance(observation, SourceJobObservation)
        or discovery_response is None
        or action is None
    ):
        raise ValueError("completed pending intake is missing its result")

    accepted = (
        discovery_response.disposition is DiscoveryDisposition.ACCEPTED
    )
    if not accepted:
        prompt = (
            "Job Discovery rejected this job. No application preparation or "
            "execution was started."
        )
    elif action is IntakeAction.REQUEST_APPLICATION:
        prompt = (
            "The job is in the system and your application intent was "
            "recorded. Materials preparation and application execution have "
            "not started."
        )
    else:
        messages = {
            DiscoveryChange.CREATED: "The job was added to your job list.",
            DiscoveryChange.UPDATED: (
                "The existing job was updated in your job list."
            ),
            DiscoveryChange.UNCHANGED: (
                "The job was already in your job list and is unchanged."
            ),
        }
        prompt = messages.get(
            discovery_response.change,
            "Job Discovery completed.",
        )
    return ResolvePendingIntakeResponse(
        status=(
            ResolveIntakeStatus.COMPLETED
            if accepted
            else ResolveIntakeStatus.FAILED
        ),
        reason_code=discovery_response.reason_code,
        retryable=False,
        pending_intake_id=pending.pending_intake_id,
        selected_action=action,
        discovery_response=discovery_response,
        accepted_intent_write_result=(
            pending.accepted_intent_write_result
        ),
        job_id=discovery_response.job_id,
        change=discovery_response.change,
        summary=_job_summary(observation),
        prompt=prompt,
    )


def _intent_persistence_failure_response(
    request: ResolvePendingIntakeRequest,
    pending: PendingIntake,
    write_result: AcceptedJobIntentWriteResult,
    *,
    reason: ResolveIntakeReason = (
        ResolveIntakeReason.ACCEPTED_INTENT_PERSISTENCE_FAILED
    ),
) -> ResolvePendingIntakeResponse:
    observation = pending.observation
    discovery_response = pending.discovery_response
    action = pending.selected_action
    if (
        not isinstance(observation, SourceJobObservation)
        or discovery_response is None
        or action is None
        or write_result.status
        is not AcceptedJobIntentWriteStatus.FAILED
    ):
        raise ValueError("intent persistence failure state is invalid")
    return ResolvePendingIntakeResponse(
        status=ResolveIntakeStatus.FAILED,
        reason_code=reason,
        retryable=write_result.retryable,
        pending_intake_id=request.pending_intake_id.strip(),
        selected_action=action,
        discovery_response=discovery_response,
        accepted_intent_write_result=write_result,
        job_id=discovery_response.job_id,
        change=discovery_response.change,
        summary=_job_summary(observation),
        prompt=(
            "Job Discovery succeeded, but the accepted job intent was not "
            "durably recorded. No application preparation or execution was "
            "started."
        ),
    )


def _failed_intent_write(
    reason: AcceptedJobIntentFailureReason,
    *,
    retryable: bool,
) -> AcceptedJobIntentWriteResult:
    return AcceptedJobIntentWriteResult(
        status=AcceptedJobIntentWriteStatus.FAILED,
        intent=None,
        reason_code=reason,
        retryable=retryable,
    )


def _persist_retained_intent(
    *,
    request: ResolvePendingIntakeRequest,
    pending: PendingIntake,
    pending_store: InMemoryPendingIntakeStore,
    accepted_intent_repository: AcceptedJobIntentRepository,
) -> ResolvePendingIntakeResponse:
    action = pending.selected_action
    discovery_response = pending.discovery_response
    if (
        action is None
        or discovery_response is None
        or pending.accepted_intent_recorded_at is None
    ):
        raise ValueError("retained accepted intent state is invalid")
    if (
        not isinstance(discovery_response.job_id, str)
        or not discovery_response.job_id.strip()
        or not isinstance(discovery_response.run_id, str)
        or not discovery_response.run_id.strip()
        or discovery_response.original_intent
        is not JobIntakeIntent(action.value)
    ):
        write_result = _failed_intent_write(
            AcceptedJobIntentFailureReason.INTEGRITY_FAILURE,
            retryable=False,
        )
        failed = pending_store.fail_intent_persistence(
            pending_intake_id=pending.pending_intake_id,
            action=action,
            write_result=write_result,
        )
        return _intent_persistence_failure_response(
            request,
            failed,
            write_result,
            reason=ResolveIntakeReason.DISCOVERY_RESPONSE_INVALID,
        )
    record = AcceptedJobIntent.create(
        subject_id=request.subject_id,
        job_id=discovery_response.job_id,
        intent=JobIntakeIntent(action.value),
        intake_proposal_id=f"proposal-{pending.pending_intake_id}",
        discovery_run_id=discovery_response.run_id,
        recorded_at=pending.accepted_intent_recorded_at,
        provenance=AcceptedJobIntentSourceProvenance(
            source_type=(
                AcceptedJobIntentSourceType.CONVERSATIONAL_INTAKE
            ),
            source_id=f"proposal-{pending.pending_intake_id}",
        ),
    )
    try:
        write_result = accepted_intent_repository.save(record)
        if not isinstance(
            write_result,
            AcceptedJobIntentWriteResult,
        ):
            raise TypeError(
                "accepted_intent_repository must return a typed result"
            )
    except (OSError, RuntimeError):
        write_result = _failed_intent_write(
            AcceptedJobIntentFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )
    if write_result.status is AcceptedJobIntentWriteStatus.FAILED:
        failed = pending_store.fail_intent_persistence(
            pending_intake_id=pending.pending_intake_id,
            action=action,
            write_result=write_result,
        )
        return _intent_persistence_failure_response(
            request,
            failed,
            write_result,
        )
    if write_result.intent != record:
        conflict = _failed_intent_write(
            AcceptedJobIntentFailureReason.INTEGRITY_FAILURE,
            retryable=False,
        )
        failed = pending_store.fail_intent_persistence(
            pending_intake_id=pending.pending_intake_id,
            action=action,
            write_result=conflict,
        )
        return _intent_persistence_failure_response(
            request,
            failed,
            conflict,
        )
    completed = pending_store.complete(
        pending_intake_id=pending.pending_intake_id,
        action=action,
        discovery_response=discovery_response,
        accepted_intent_write_result=write_result,
        resolved_at=pending.accepted_intent_recorded_at,
    )
    return _discovery_result_response(completed)


def resolve_pending_intake(
    request: ResolvePendingIntakeRequest,
    *,
    pending_store: InMemoryPendingIntakeStore,
    accepted_intent_repository: AcceptedJobIntentRepository,
    discovery_port: _JobDiscoveryPort = run_discovery,
    clock: Callable[[], datetime] = _utc_now,
) -> ResolvePendingIntakeResponse:
    """Resolve one I1 choice through the typed Job Discovery boundary."""
    if not isinstance(request, ResolvePendingIntakeRequest):
        raise TypeError("request must be a ResolvePendingIntakeRequest")
    if not isinstance(pending_store, InMemoryPendingIntakeStore):
        raise TypeError("pending_store must be an InMemoryPendingIntakeStore")
    if not isinstance(
        accepted_intent_repository,
        AcceptedJobIntentRepository,
    ):
        raise TypeError(
            "accepted_intent_repository must implement the typed port"
        )
    try:
        action = IntakeAction(request.action)
    except ValueError:
        return _resolve_failure_response(
            request,
            ResolveIntakeReason.INVALID_ACTION,
        )

    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    claim = pending_store.claim(
        pending_intake_id=request.pending_intake_id.strip(),
        subject_id=request.subject_id.strip(),
        conversation_id=request.conversation_id.strip(),
        action=action,
        now=now,
    )
    if claim.status is _PendingClaimStatus.NOT_FOUND:
        return _resolve_failure_response(
            request,
            ResolveIntakeReason.PENDING_INTAKE_NOT_FOUND,
        )
    if claim.status is _PendingClaimStatus.CONVERSATION_MISMATCH:
        return _resolve_failure_response(
            request,
            ResolveIntakeReason.CONVERSATION_MISMATCH,
        )
    if claim.status is _PendingClaimStatus.SUBJECT_MISMATCH:
        return _resolve_failure_response(
            request,
            ResolveIntakeReason.SUBJECT_MISMATCH,
        )
    if claim.status is _PendingClaimStatus.EXPIRED:
        return _resolve_failure_response(
            request,
            ResolveIntakeReason.PENDING_INTAKE_EXPIRED,
        )
    if claim.status is _PendingClaimStatus.ALREADY_RESOLVED:
        return _resolve_failure_response(
            request,
            ResolveIntakeReason.PENDING_INTAKE_ALREADY_RESOLVED,
            selected_action=(
                claim.pending.selected_action if claim.pending else None
            ),
        )
    if claim.status is _PendingClaimStatus.REPLAY:
        if claim.pending is None:
            raise RuntimeError("replay claim has no pending intake")
        return _discovery_result_response(claim.pending)
    if (
        claim.status
        is _PendingClaimStatus.RESUME_INTENT_PERSISTENCE
    ):
        if claim.pending is None:
            raise RuntimeError("intent persistence replay has no pending")
        return _persist_retained_intent(
            request=request,
            pending=claim.pending,
            pending_store=pending_store,
            accepted_intent_repository=accepted_intent_repository,
        )
    if claim.pending is None:
        raise RuntimeError("claimed pending intake is missing")

    observation = claim.pending.observation
    if not isinstance(observation, SourceJobObservation):
        pending_store.release(
            pending_intake_id=claim.pending.pending_intake_id,
            action=action,
        )
        return _resolve_failure_response(
            request,
            ResolveIntakeReason.PENDING_OBSERVATION_INVALID,
            selected_action=action,
        )

    proposal = JobIntakeProposal(
        proposal_id=f"proposal-{claim.pending.pending_intake_id}",
        intent=JobIntakeIntent(action.value),
        resolution=ProposalResolution.RESOLVED,
        resolved_candidate=_candidate_from_observation(observation),
        missing_fields=(),
        alternatives=(),
    )
    discovery_request = JobDiscoveryRequest(
        request_id=f"intake-{claim.pending.pending_intake_id}",
        trigger=DiscoveryTrigger.CONVERSATIONAL,
        proposal=proposal,
    )
    try:
        discovery_response = discovery_port(discovery_request)
        if not isinstance(discovery_response, JobDiscoveryResponse):
            raise TypeError(
                "discovery_port must return a JobDiscoveryResponse"
            )
    except Exception:
        pending_store.release(
            pending_intake_id=claim.pending.pending_intake_id,
            action=action,
        )
        return _resolve_failure_response(
            request,
            ResolveIntakeReason.DISCOVERY_TEMPORARILY_UNAVAILABLE,
            retryable=True,
            selected_action=action,
        )

    if (
        discovery_response.disposition
        is not DiscoveryDisposition.ACCEPTED
    ):
        completed = pending_store.complete(
            pending_intake_id=claim.pending.pending_intake_id,
            action=action,
            discovery_response=discovery_response,
            resolved_at=clock(),
        )
        return _discovery_result_response(completed)

    recorded_at = clock()
    if not isinstance(recorded_at, datetime) or recorded_at.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    retained = pending_store.retain_accepted_discovery(
        pending_intake_id=claim.pending.pending_intake_id,
        action=action,
        discovery_response=discovery_response,
        recorded_at=recorded_at,
    )
    return _persist_retained_intent(
        request=request,
        pending=retained,
        pending_store=pending_store,
        accepted_intent_repository=accepted_intent_repository,
    )


__all__ = [
    "CandidateSelectionReason",
    "CandidateSelectionRequest",
    "CandidateSelectionStatus",
    "ConversationalIntakeRequest",
    "ConversationalIntakeResponse",
    "InMemoryCandidateSelectionStore",
    "InMemoryPendingIntakeStore",
    "IntakeAction",
    "IntakeReason",
    "IntakeResponseStatus",
    "JobSummary",
    "NamedJobClueExtractor",
    "NamedJobClues",
    "NamedJobIntentHint",
    "NamedJobSearchResponse",
    "NamedSearchReason",
    "PendingCandidateSelection",
    "PendingIntake",
    "PendingIntakeStatus",
    "ResolveIntakeReason",
    "ResolveIntakeStatus",
    "ResolvePendingIntakeRequest",
    "ResolvePendingIntakeResponse",
    "handle_conversational_intake",
    "handle_conversational_url_intake",
    "resolve_pending_intake",
    "select_search_candidate",
]
