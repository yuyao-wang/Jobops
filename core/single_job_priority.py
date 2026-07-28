"""Application orchestration for one persisted V1 JobPosting priority decision."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .job_discovery import (
    JobPosting,
    JobPostingReadRepository,
    JobPostingRepositoryError,
)
from .job_prioritization import (
    PRIORITY_VALIDATION_VERSION,
    CandidateSummary,
    CreatePriorityProposalRequest,
    CreatePriorityProposalResult,
    EvidenceRef,
    EligibilityFinding,
    HardConstraintFinding,
    PriorityAgentMetadata,
    PriorityAgentPort,
    PriorityDecision,
    PriorityDecisionRepositoryError,
    PriorityDecisionResult,
    PriorityDecisionStatus,
    PriorityProposal,
    PriorityProposalStatus,
    PriorityRationale,
    ProposedQualification,
    FinalizePriorityProposalRequest,
    PrivateHomePriorityDecisionRepository,
    create_priority_proposal,
    finalize_priority_proposal,
    priority_proposal_content_hash,
)
from .prioritization_policy import (
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
)
from .private_home import PrivateHome
from .profile_store import (
    CandidateSummaryProvider,
    CandidateSummaryProviderError,
)


SINGLE_JOB_PRIORITY_ORCHESTRATION_VERSION = "single-job-priority-v1"
_ORCHESTRATION_RECORD_SCHEMA_VERSION = 1


class SingleJobPriorityStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SingleJobPriorityChange(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"


class PriorityArtifactWriteOutcome(str, Enum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"


class SingleJobPriorityReason(str, Enum):
    INVALID_REQUEST = "INVALID_REQUEST"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_READ_FAILED = "JOB_READ_FAILED"
    ACTIVE_POLICY_NOT_FOUND = "ACTIVE_POLICY_NOT_FOUND"
    POLICY_READ_FAILED = "POLICY_READ_FAILED"
    CANDIDATE_SUMMARY_UNAVAILABLE = "CANDIDATE_SUMMARY_UNAVAILABLE"
    ORCHESTRATION_INCOMPLETE = "ORCHESTRATION_INCOMPLETE"
    PROPOSAL_FAILED = "PROPOSAL_FAILED"
    FINALIZATION_FAILED = "FINALIZATION_FAILED"
    ORCHESTRATION_PERSISTENCE_FAILED = (
        "ORCHESTRATION_PERSISTENCE_FAILED"
    )


class OrchestrationRecordStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SingleJobPriorityRepositoryError(RuntimeError):
    """Raised when orchestration idempotency state cannot be trusted."""


@runtime_checkable
class ActivePrioritizationPolicyProvider(Protocol):
    def get_active_policy(
        self,
        subject_id: str,
    ) -> PrioritizationPolicy | None:
        """Return the current approved policy for one subject."""


@dataclass(frozen=True, slots=True)
class SingleJobPriorityCommand:
    subject_id: str
    job_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class SingleJobPriorityBinding:
    subject_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    policy_id: str
    policy_version: int
    policy_content_hash: str
    candidate_summary_version: str
    candidate_summary_content_hash: str
    agent_version: str
    prompt_version: str
    model_id: str
    validation_version: str
    evaluated_at: str
    orchestration_version: str = SINGLE_JOB_PRIORITY_ORCHESTRATION_VERSION

    def __post_init__(self) -> None:
        for name in (
            "subject_id",
            "job_id",
            "policy_id",
            "candidate_summary_version",
            "agent_version",
            "prompt_version",
            "model_id",
            "validation_version",
            "orchestration_version",
        ):
            value = getattr(self, name)
            if _clean_id(name, value) != value:
                raise ValueError(f"{name} must be canonical")
        for name in ("job_revision", "policy_version"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise ValueError(f"{name} must be a positive integer")
        for name in (
            "job_content_hash",
            "policy_content_hash",
            "candidate_summary_content_hash",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in value
                )
            ):
                raise ValueError(f"{name} must be a SHA-256 digest")
        if (
            not isinstance(self.evaluated_at, str)
            or _rfc3339(_parse_timestamp(self.evaluated_at))
            != self.evaluated_at
        ):
            raise ValueError("evaluated_at must be canonical RFC 3339 UTC")

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "job_content_hash": self.job_content_hash,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_content_hash": self.policy_content_hash,
            "candidate_summary_version": self.candidate_summary_version,
            "candidate_summary_content_hash": (
                self.candidate_summary_content_hash
            ),
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "validation_version": self.validation_version,
            "evaluated_at": self.evaluated_at,
            "orchestration_version": self.orchestration_version,
        }

    @property
    def input_binding(self) -> str:
        return f"priority-input-{_hash_json(self.to_dict())}"


@dataclass(frozen=True, slots=True)
class StoredSingleJobPriority:
    input_binding: str
    binding: SingleJobPriorityBinding
    status: OrchestrationRecordStatus
    proposal: PriorityProposal | None
    decision_id: str | None
    failure_reason: str | None
    claim_acquired: bool = False


@dataclass(frozen=True, slots=True)
class SingleJobPriorityResult:
    status: SingleJobPriorityStatus
    change: SingleJobPriorityChange | None
    reason_code: SingleJobPriorityReason | None
    retryable: bool
    subject_id: str
    job_id: str
    input_binding: str | None
    proposal_outcome: PriorityArtifactWriteOutcome | None
    decision_outcome: PriorityArtifactWriteOutcome | None
    proposal_result: CreatePriorityProposalResult | None
    decision_result: PriorityDecisionResult | None
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SingleJobPriorityStatus(self.status))
        if self.change is not None:
            object.__setattr__(
                self,
                "change",
                SingleJobPriorityChange(self.change),
            )
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                SingleJobPriorityReason(self.reason_code),
            )
        if self.proposal_outcome is not None:
            object.__setattr__(
                self,
                "proposal_outcome",
                PriorityArtifactWriteOutcome(self.proposal_outcome),
            )
        if self.decision_outcome is not None:
            object.__setattr__(
                self,
                "decision_outcome",
                PriorityArtifactWriteOutcome(self.decision_outcome),
            )
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("message must be non-empty")
        if self.status is SingleJobPriorityStatus.SUCCEEDED:
            if (
                self.change is None
                or self.reason_code is not None
                or self.retryable
                or self.input_binding is None
                or self.proposal_outcome is None
                or self.decision_outcome is None
                or self.proposal_result is None
                or self.proposal_result.proposal is None
                or self.decision_result is None
                or self.decision_result.decision is None
            ):
                raise ValueError("successful orchestration result is invalid")
        elif (
            self.change is not None
            or self.reason_code is None
            or self.proposal_outcome is not None
            or self.decision_outcome is not None
            or self.decision_result is not None
        ):
            raise ValueError("failed orchestration result is invalid")

    @property
    def proposal(self) -> PriorityProposal | None:
        return (
            self.proposal_result.proposal
            if self.proposal_result is not None
            else None
        )

    @property
    def decision(self) -> PriorityDecision | None:
        return (
            self.decision_result.decision
            if self.decision_result is not None
            else None
        )


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rfc3339(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_id(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 160:
        raise ValueError(f"{name} is outside the orchestration contract")
    return cleaned


def build_single_job_priority_binding(
    *,
    subject_id: str,
    job: JobPosting,
    policy: PrioritizationPolicy,
    candidate_summary: CandidateSummary,
    metadata: PriorityAgentMetadata,
    now: datetime,
) -> SingleJobPriorityBinding:
    return SingleJobPriorityBinding(
        subject_id=_clean_id("subject_id", subject_id),
        job_id=_clean_id("job_id", job.job_id),
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_content_hash=policy.policy_content_hash,
        candidate_summary_version=(
            candidate_summary.candidate_summary_version
        ),
        candidate_summary_content_hash=(
            candidate_summary.candidate_summary_content_hash
        ),
        agent_version=metadata.agent_version,
        prompt_version=metadata.prompt_version,
        model_id=metadata.model_id,
        validation_version=PRIORITY_VALIDATION_VERSION,
        evaluated_at=_rfc3339(now),
    )


def _binding_from_dict(value: Any) -> SingleJobPriorityBinding:
    expected = {
        "subject_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "policy_id",
        "policy_version",
        "policy_content_hash",
        "candidate_summary_version",
        "candidate_summary_content_hash",
        "agent_version",
        "prompt_version",
        "model_id",
        "validation_version",
        "evaluated_at",
        "orchestration_version",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted orchestration binding is invalid")
    return SingleJobPriorityBinding(**dict(value))


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("persisted proposal timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("persisted proposal timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("persisted proposal timestamp is invalid")
    return parsed.astimezone(timezone.utc)


def _evidence_from_dict(value: Any) -> EvidenceRef:
    expected = {"source_type", "source_id", "field", "excerpt"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted proposal evidence is invalid")
    return EvidenceRef(
        source_type=value["source_type"],
        source_id=value["source_id"],
        field=value["field"],
        excerpt=value["excerpt"],
    )


def _rationale_from_dict(value: Any) -> PriorityRationale:
    expected = {"signal_id", "category", "explanation", "evidence_refs"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["evidence_refs"], list)
    ):
        raise ValueError("persisted proposal rationale is invalid")
    return PriorityRationale(
        signal_id=value["signal_id"],
        category=value["category"],
        explanation=value["explanation"],
        evidence_refs=tuple(
            _evidence_from_dict(item) for item in value["evidence_refs"]
        ),
    )


def _hard_finding_from_dict(value: Any) -> HardConstraintFinding:
    expected = {"constraint_id", "result", "explanation", "evidence_refs"}
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["evidence_refs"], list)
    ):
        raise ValueError("persisted proposal hard finding is invalid")
    return HardConstraintFinding(
        constraint_id=value["constraint_id"],
        result=value["result"],
        explanation=value["explanation"],
        evidence_refs=tuple(
            _evidence_from_dict(item) for item in value["evidence_refs"]
        ),
    )


def _eligibility_from_dict(value: Any) -> EligibilityFinding:
    expected = {
        "category",
        "result",
        "impact",
        "explanation",
        "evidence_refs",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["evidence_refs"], list)
    ):
        raise ValueError("persisted proposal eligibility is invalid")
    return EligibilityFinding(
        category=value["category"],
        result=value["result"],
        impact=value["impact"],
        explanation=value["explanation"],
        evidence_refs=tuple(
            _evidence_from_dict(item) for item in value["evidence_refs"]
        ),
    )


def _proposal_from_dict(value: Any) -> PriorityProposal:
    expected = {
        "proposal_id",
        "request_id",
        "subject_id",
        "job_id",
        "job_revision",
        "job_content_hash",
        "policy_id",
        "policy_version",
        "policy_content_hash",
        "candidate_summary_version",
        "candidate_summary_content_hash",
        "agent_version",
        "prompt_version",
        "model_id",
        "created_at",
        "proposed_qualification",
        "proposed_priority_level",
        "confidence",
        "summary",
        "positive_signals",
        "concerns",
        "hard_constraint_findings",
        "eligibility_findings",
        "missing_information",
        "questions_for_user",
    }
    list_fields = {
        "positive_signals",
        "concerns",
        "hard_constraint_findings",
        "eligibility_findings",
        "missing_information",
        "questions_for_user",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or any(not isinstance(value[field], list) for field in list_fields)
    ):
        raise ValueError("persisted PriorityProposal is invalid")
    return PriorityProposal(
        proposal_id=value["proposal_id"],
        request_id=value["request_id"],
        subject_id=value["subject_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        policy_id=value["policy_id"],
        policy_version=value["policy_version"],
        policy_content_hash=value["policy_content_hash"],
        candidate_summary_version=value["candidate_summary_version"],
        candidate_summary_content_hash=(
            value["candidate_summary_content_hash"]
        ),
        agent_version=value["agent_version"],
        prompt_version=value["prompt_version"],
        model_id=value["model_id"],
        created_at=_parse_timestamp(value["created_at"]),
        proposed_qualification=value["proposed_qualification"],
        proposed_priority_level=value["proposed_priority_level"],
        confidence=value["confidence"],
        summary=value["summary"],
        positive_signals=tuple(
            _rationale_from_dict(item) for item in value["positive_signals"]
        ),
        concerns=tuple(
            _rationale_from_dict(item) for item in value["concerns"]
        ),
        hard_constraint_findings=tuple(
            _hard_finding_from_dict(item)
            for item in value["hard_constraint_findings"]
        ),
        eligibility_findings=tuple(
            _eligibility_from_dict(item)
            for item in value["eligibility_findings"]
        ),
        missing_information=tuple(value["missing_information"]),
        questions_for_user=tuple(value["questions_for_user"]),
    )


class PrivateHomeSingleJobPriorityRepository:
    """Persist pre-Agent input claims and completed Proposal/Decision references."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, input_binding: str) -> Path:
        if (
            not isinstance(input_binding, str)
            or not input_binding.startswith("priority-input-")
            or len(input_binding) != len("priority-input-") + 64
        ):
            raise ValueError("input binding is invalid")
        return (
            self._home.paths.prioritization
            / "orchestrations"
            / f"{input_binding}.json"
        )

    def _read(
        self,
        binding: SingleJobPriorityBinding,
    ) -> StoredSingleJobPriority | None:
        return self._read_path(
            self._path(binding.input_binding),
            expected_binding=binding,
        )

    def _read_path(
        self,
        path: Path,
        *,
        expected_binding: SingleJobPriorityBinding | None = None,
    ) -> StoredSingleJobPriority | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SingleJobPriorityRepositoryError(
                "orchestration record is unreadable"
            ) from exc
        expected = {
            "schema_version",
            "input_binding",
            "binding",
            "status",
            "proposal_content_hash",
            "proposal",
            "decision_id",
            "failure_reason",
        }
        try:
            stored_binding = _binding_from_dict(value["binding"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SingleJobPriorityRepositoryError(
                "orchestration record binding is invalid"
            ) from exc
        input_binding = stored_binding.input_binding
        if (
            not isinstance(value, Mapping)
            or set(value) != expected
            or value["schema_version"]
            != _ORCHESTRATION_RECORD_SCHEMA_VERSION
            or value["input_binding"] != input_binding
            or path.name != f"{input_binding}.json"
            or (
                expected_binding is not None
                and stored_binding != expected_binding
            )
        ):
            raise SingleJobPriorityRepositoryError(
                "orchestration record binding is invalid"
            )
        try:
            status = OrchestrationRecordStatus(value["status"])
        except ValueError as exc:
            raise SingleJobPriorityRepositoryError(
                "orchestration record status is invalid"
            ) from exc
        proposal = None
        if value["proposal"] is not None:
            try:
                proposal = _proposal_from_dict(value["proposal"])
            except (TypeError, ValueError) as exc:
                raise SingleJobPriorityRepositoryError(
                    "persisted PriorityProposal is invalid"
                ) from exc
            if (
                value["proposal_content_hash"]
                != priority_proposal_content_hash(proposal)
            ):
                raise SingleJobPriorityRepositoryError(
                    "persisted PriorityProposal hash is invalid"
                )
        if status is OrchestrationRecordStatus.COMPLETED:
            if (
                proposal is None
                or not isinstance(value["decision_id"], str)
                or value["failure_reason"] is not None
            ):
                raise SingleJobPriorityRepositoryError(
                    "completed orchestration record is invalid"
                )
        elif status is OrchestrationRecordStatus.IN_PROGRESS:
            if (
                proposal is not None
                or value["proposal_content_hash"] is not None
                or value["decision_id"] is not None
                or value["failure_reason"] is not None
            ):
                raise SingleJobPriorityRepositoryError(
                    "in-progress orchestration record is invalid"
                )
        elif (
            value["decision_id"] is not None
            or not isinstance(value["failure_reason"], str)
            or not value["failure_reason"].strip()
        ):
            raise SingleJobPriorityRepositoryError(
                "failed orchestration record is invalid"
            )
        return StoredSingleJobPriority(
            input_binding=input_binding,
            binding=stored_binding,
            status=status,
            proposal=proposal,
            decision_id=value["decision_id"],
            failure_reason=value["failure_reason"],
            claim_acquired=False,
        )

    def _write(
        self,
        binding: SingleJobPriorityBinding,
        *,
        status: OrchestrationRecordStatus,
        proposal: PriorityProposal | None = None,
        decision_id: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        encoded = self._record_bytes(
            binding,
            status=status,
            proposal=proposal,
            decision_id=decision_id,
            failure_reason=failure_reason,
        )
        try:
            self._home.ensure()
            self._home.write_bytes(
                self._path(binding.input_binding),
                encoded,
            )
        except (OSError, RuntimeError) as exc:
            raise SingleJobPriorityRepositoryError(
                "orchestration state persistence failed"
            ) from exc

    @staticmethod
    def _record_bytes(
        binding: SingleJobPriorityBinding,
        *,
        status: OrchestrationRecordStatus,
        proposal: PriorityProposal | None = None,
        decision_id: str | None = None,
        failure_reason: str | None = None,
    ) -> bytes:
        value = {
            "schema_version": _ORCHESTRATION_RECORD_SCHEMA_VERSION,
            "input_binding": binding.input_binding,
            "binding": binding.to_dict(),
            "status": status.value,
            "proposal_content_hash": (
                priority_proposal_content_hash(proposal)
                if proposal is not None
                else None
            ),
            "proposal": proposal.to_dict() if proposal is not None else None,
            "decision_id": decision_id,
            "failure_reason": failure_reason,
        }
        encoded = (
            json.dumps(
                value,
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        return encoded

    def claim(
        self,
        binding: SingleJobPriorityBinding,
    ) -> StoredSingleJobPriority:
        with self._lock:
            existing = self._read(binding)
            if existing is not None:
                return existing
            encoded = self._record_bytes(
                binding,
                status=OrchestrationRecordStatus.IN_PROGRESS,
            )
            try:
                self._home.ensure()
                acquired = self._home.write_bytes_if_absent(
                    self._path(binding.input_binding),
                    encoded,
                )
            except (OSError, RuntimeError) as exc:
                raise SingleJobPriorityRepositoryError(
                    "orchestration claim persistence failed"
                ) from exc
            claimed = self._read(binding)
            if claimed is None:
                raise SingleJobPriorityRepositoryError(
                    "orchestration claim was not persisted"
                )
            return replace(claimed, claim_acquired=acquired)

    def find_completed_by_input_binding(
        self,
        binding: SingleJobPriorityBinding,
    ) -> StoredSingleJobPriority | None:
        with self._lock:
            existing = self._read(binding)
            if (
                existing is not None
                and existing.status is OrchestrationRecordStatus.COMPLETED
            ):
                return existing
            return None

    def list_for_subject(
        self,
        subject_id: str,
    ) -> tuple[StoredSingleJobPriority, ...]:
        clean_subject = _clean_id("subject_id", subject_id)
        directory = self._home.paths.prioritization / "orchestrations"
        if not directory.is_dir():
            return ()
        try:
            paths = tuple(sorted(directory.glob("priority-input-*.json")))
        except OSError as exc:
            raise SingleJobPriorityRepositoryError(
                "orchestration history could not be listed"
            ) from exc
        records: list[StoredSingleJobPriority] = []
        with self._lock:
            for path in paths:
                record = self._read_path(path)
                if record is None:
                    raise SingleJobPriorityRepositoryError(
                        "orchestration record disappeared during listing"
                    )
                if record.binding.subject_id == clean_subject:
                    records.append(record)
        return tuple(
            sorted(records, key=lambda item: item.input_binding)
        )

    def complete(
        self,
        binding: SingleJobPriorityBinding,
        *,
        proposal: PriorityProposal,
        decision: PriorityDecision,
    ) -> StoredSingleJobPriority:
        with self._lock:
            existing = self._read(binding)
            if existing is None:
                raise SingleJobPriorityRepositoryError(
                    "orchestration claim is missing"
                )
            if existing.status is OrchestrationRecordStatus.COMPLETED:
                if (
                    existing.proposal == proposal
                    and existing.decision_id == decision.decision_id
                ):
                    return existing
                raise SingleJobPriorityRepositoryError(
                    "completed orchestration is immutable"
                )
            if existing.status is not OrchestrationRecordStatus.IN_PROGRESS:
                raise SingleJobPriorityRepositoryError(
                    "failed orchestration cannot be completed"
                )
            self._write(
                binding,
                status=OrchestrationRecordStatus.COMPLETED,
                proposal=proposal,
                decision_id=decision.decision_id,
            )
            completed = self._read(binding)
            if completed is None:
                raise SingleJobPriorityRepositoryError(
                    "completed orchestration was not persisted"
                )
            return completed

    def release_claim(
        self,
        binding: SingleJobPriorityBinding,
    ) -> None:
        """Release a claim when P1b returned no Proposal or side effect."""

        with self._lock:
            existing = self._read(binding)
            if (
                existing is None
                or existing.status is not OrchestrationRecordStatus.IN_PROGRESS
                or existing.proposal is not None
            ):
                raise SingleJobPriorityRepositoryError(
                    "only an empty in-progress claim can be released"
                )
            try:
                self._path(binding.input_binding).unlink()
            except OSError as exc:
                raise SingleJobPriorityRepositoryError(
                    "orchestration claim release failed"
                ) from exc

    def fail(
        self,
        binding: SingleJobPriorityBinding,
        *,
        reason: str,
        proposal: PriorityProposal | None = None,
    ) -> StoredSingleJobPriority:
        with self._lock:
            existing = self._read(binding)
            if existing is None:
                raise SingleJobPriorityRepositoryError(
                    "orchestration claim is missing"
                )
            if existing.status is OrchestrationRecordStatus.COMPLETED:
                raise SingleJobPriorityRepositoryError(
                    "completed orchestration is immutable"
                )
            if existing.status is OrchestrationRecordStatus.FAILED:
                return existing
            self._write(
                binding,
                status=OrchestrationRecordStatus.FAILED,
                proposal=proposal,
                failure_reason=_clean_id("failure_reason", reason),
            )
            failed = self._read(binding)
            if failed is None:
                raise SingleJobPriorityRepositoryError(
                    "failed orchestration was not persisted"
                )
            return failed


def _failure(
    command: SingleJobPriorityCommand,
    reason: SingleJobPriorityReason,
    message: str,
    *,
    input_binding: str | None = None,
    retryable: bool = False,
    proposal_result: CreatePriorityProposalResult | None = None,
) -> SingleJobPriorityResult:
    return SingleJobPriorityResult(
        status=SingleJobPriorityStatus.FAILED,
        change=None,
        reason_code=reason,
        retryable=retryable,
        subject_id=(
            command.subject_id
            if isinstance(command.subject_id, str)
            else ""
        ),
        job_id=command.job_id if isinstance(command.job_id, str) else "",
        input_binding=input_binding,
        proposal_outcome=None,
        decision_outcome=None,
        proposal_result=proposal_result,
        decision_result=None,
        message=message,
    )


def _loaded_proposal_result(
    proposal: PriorityProposal,
) -> CreatePriorityProposalResult:
    status = (
        PriorityProposalStatus.NEEDS_USER
        if proposal.proposed_qualification
        is ProposedQualification.NEEDS_USER
        else PriorityProposalStatus.SUCCEEDED
    )
    return CreatePriorityProposalResult(
        status=status,
        reason_code=None,
        retryable=False,
        proposal=proposal,
        message="The completed PriorityProposal was loaded.",
    )


def _loaded_decision_result(
    decision: PriorityDecision,
) -> PriorityDecisionResult:
    return PriorityDecisionResult(
        status=PriorityDecisionStatus.SUCCEEDED,
        reason_code=None,
        retryable=False,
        decision=decision,
        message="The completed PriorityDecision was loaded.",
    )


def _completed_result(
    *,
    command: SingleJobPriorityCommand,
    binding: SingleJobPriorityBinding,
    proposal: PriorityProposal,
    decision: PriorityDecision,
    change: SingleJobPriorityChange,
) -> SingleJobPriorityResult:
    outcome = PriorityArtifactWriteOutcome(change.value)
    return SingleJobPriorityResult(
        status=SingleJobPriorityStatus.SUCCEEDED,
        change=change,
        reason_code=None,
        retryable=False,
        subject_id=command.subject_id,
        job_id=command.job_id,
        input_binding=binding.input_binding,
        proposal_outcome=outcome,
        decision_outcome=outcome,
        proposal_result=_loaded_proposal_result(proposal),
        decision_result=_loaded_decision_result(decision),
        message=(
            "A new PriorityProposal and PriorityDecision were persisted."
            if change is SingleJobPriorityChange.CREATED
            else "The existing completed priority result was reused."
        ),
    )


def completed_priority_bindings_match(
    *,
    binding: SingleJobPriorityBinding,
    proposal: PriorityProposal,
    decision: PriorityDecision,
) -> bool:
    proposal_hash = priority_proposal_content_hash(proposal)
    return (
        proposal.subject_id == binding.subject_id
        and proposal.job_id == binding.job_id
        and proposal.job_revision == binding.job_revision
        and proposal.job_content_hash == binding.job_content_hash
        and proposal.policy_id == binding.policy_id
        and proposal.policy_version == binding.policy_version
        and proposal.policy_content_hash == binding.policy_content_hash
        and proposal.candidate_summary_version
        == binding.candidate_summary_version
        and proposal.candidate_summary_content_hash
        == binding.candidate_summary_content_hash
        and proposal.agent_version == binding.agent_version
        and proposal.prompt_version == binding.prompt_version
        and proposal.model_id == binding.model_id
        and _rfc3339(proposal.created_at) == binding.evaluated_at
        and decision.subject_id == binding.subject_id
        and decision.job_id == binding.job_id
        and decision.job_revision == binding.job_revision
        and decision.job_content_hash == binding.job_content_hash
        and decision.policy_id == binding.policy_id
        and decision.policy_version == binding.policy_version
        and decision.policy_content_hash == binding.policy_content_hash
        and decision.candidate_summary_version
        == binding.candidate_summary_version
        and decision.candidate_summary_content_hash
        == binding.candidate_summary_content_hash
        and decision.agent_version == binding.agent_version
        and decision.prompt_version == binding.prompt_version
        and decision.model_id == binding.model_id
        and decision.validation_version == binding.validation_version
        and decision.source_proposal_id == proposal.proposal_id
        and decision.source_proposal_content_hash == proposal_hash
    )


async def orchestrate_single_job_priority(
    command: SingleJobPriorityCommand,
    *,
    job_repository: JobPostingReadRepository,
    policy_provider: ActivePrioritizationPolicyProvider,
    candidate_summary_provider: CandidateSummaryProvider,
    orchestration_repository: PrivateHomeSingleJobPriorityRepository,
    decision_repository: PrivateHomePriorityDecisionRepository,
    agent: PriorityAgentPort,
    metadata: PriorityAgentMetadata,
) -> SingleJobPriorityResult:
    """Create or reuse one completed Proposal → Gate → Decision orchestration."""

    if not isinstance(command, SingleJobPriorityCommand):
        raise TypeError("command must be a SingleJobPriorityCommand")
    try:
        subject_id = _clean_id("subject_id", command.subject_id)
        job_id = _clean_id("job_id", command.job_id)
        _rfc3339(command.now)
    except (TypeError, ValueError) as exc:
        return _failure(
            command,
            SingleJobPriorityReason.INVALID_REQUEST,
            str(exc),
        )

    try:
        job = job_repository.get(job_id)
    except ValueError as exc:
        return _failure(
            command,
            SingleJobPriorityReason.INVALID_REQUEST,
            str(exc),
        )
    except JobPostingRepositoryError:
        return _failure(
            command,
            SingleJobPriorityReason.JOB_READ_FAILED,
            "The persisted JobPosting could not be read.",
            retryable=True,
        )
    if job is None:
        return _failure(
            command,
            SingleJobPriorityReason.JOB_NOT_FOUND,
            "The persisted JobPosting was not found.",
        )

    try:
        policy = policy_provider.get_active_policy(subject_id)
    except RuntimeError:
        return _failure(
            command,
            SingleJobPriorityReason.POLICY_READ_FAILED,
            "The active prioritization policy could not be read.",
            retryable=True,
        )
    if (
        policy is None
        or not isinstance(policy, PrioritizationPolicy)
        or policy.status is not PrioritizationPolicyStatus.ACTIVE
        or policy.subject_id != subject_id
    ):
        return _failure(
            command,
            SingleJobPriorityReason.ACTIVE_POLICY_NOT_FOUND,
            "No matching ACTIVE PrioritizationPolicy is available.",
        )

    try:
        candidate_summary = candidate_summary_provider.get_current(
            subject_id,
            now=command.now,
        )
    except CandidateSummaryProviderError:
        return _failure(
            command,
            SingleJobPriorityReason.CANDIDATE_SUMMARY_UNAVAILABLE,
            "The current CandidateSummary is unavailable or invalid.",
        )
    if candidate_summary.subject_id != subject_id:
        return _failure(
            command,
            SingleJobPriorityReason.CANDIDATE_SUMMARY_UNAVAILABLE,
            "The current CandidateSummary subject binding is invalid.",
        )

    try:
        binding = build_single_job_priority_binding(
            subject_id=subject_id,
            job=job,
            policy=policy,
            candidate_summary=candidate_summary,
            metadata=metadata,
            now=command.now,
        )
        claimed = orchestration_repository.claim(binding)
    except (AttributeError, TypeError, ValueError):
        return _failure(
            command,
            SingleJobPriorityReason.INVALID_REQUEST,
            "The priority orchestration binding is invalid.",
        )
    except SingleJobPriorityRepositoryError:
        return _failure(
            command,
            SingleJobPriorityReason.ORCHESTRATION_PERSISTENCE_FAILED,
            "The priority orchestration claim could not be persisted.",
            retryable=True,
        )

    if claimed.status is OrchestrationRecordStatus.COMPLETED:
        if claimed.proposal is None or claimed.decision_id is None:
            return _failure(
                command,
                SingleJobPriorityReason.ORCHESTRATION_PERSISTENCE_FAILED,
                "The completed priority orchestration is invalid.",
                input_binding=binding.input_binding,
            )
        try:
            decision = decision_repository.get_decision(
                subject_id=subject_id,
                job_id=job_id,
                decision_id=claimed.decision_id,
            )
        except PriorityDecisionRepositoryError:
            return _failure(
                command,
                SingleJobPriorityReason.ORCHESTRATION_PERSISTENCE_FAILED,
                "The completed PriorityDecision could not be read.",
                input_binding=binding.input_binding,
                retryable=True,
            )
        if decision is None or not completed_priority_bindings_match(
            binding=binding,
            proposal=claimed.proposal,
            decision=decision,
        ):
            return _failure(
                command,
                SingleJobPriorityReason.ORCHESTRATION_PERSISTENCE_FAILED,
                "The completed priority bindings are invalid.",
                input_binding=binding.input_binding,
            )
        return _completed_result(
            command=command,
            binding=binding,
            proposal=claimed.proposal,
            decision=decision,
            change=SingleJobPriorityChange.UNCHANGED,
        )

    if claimed.status is not OrchestrationRecordStatus.IN_PROGRESS:
        return _failure(
            command,
            SingleJobPriorityReason.ORCHESTRATION_INCOMPLETE,
            "A previous matching priority orchestration did not complete.",
            input_binding=binding.input_binding,
        )
    if not claimed.claim_acquired:
        return _failure(
            command,
            SingleJobPriorityReason.ORCHESTRATION_INCOMPLETE,
            "A matching priority orchestration is already in progress.",
            input_binding=binding.input_binding,
            retryable=True,
        )

    request_id = f"single-job-priority-{binding.input_binding.removeprefix('priority-input-')}"
    proposal_id = f"priority-proposal-{binding.input_binding.removeprefix('priority-input-')}"
    proposal_result = await create_priority_proposal(
        CreatePriorityProposalRequest(
            request_id=request_id,
            subject_id=subject_id,
            job_posting=job,
            policy=policy,
            candidate_summary=candidate_summary,
            now=command.now,
        ),
        agent=agent,
        metadata=metadata,
        proposal_id_factory=lambda: proposal_id,
    )
    proposal = proposal_result.proposal
    if proposal is None:
        try:
            orchestration_repository.release_claim(binding)
        except SingleJobPriorityRepositoryError:
            return _failure(
                command,
                SingleJobPriorityReason.ORCHESTRATION_PERSISTENCE_FAILED,
                "The empty Proposal claim could not be released safely.",
                input_binding=binding.input_binding,
                retryable=True,
                proposal_result=proposal_result,
            )
        return _failure(
            command,
            SingleJobPriorityReason.PROPOSAL_FAILED,
            proposal_result.message,
            input_binding=binding.input_binding,
            retryable=proposal_result.retryable,
            proposal_result=proposal_result,
        )

    decision_result = finalize_priority_proposal(
        FinalizePriorityProposalRequest(
            request_id=request_id,
            subject_id=subject_id,
            job_posting=job,
            policy=policy,
            candidate_summary=candidate_summary,
            proposal=proposal,
            now=command.now,
        ),
        repository=decision_repository,
    )
    decision = decision_result.decision
    if decision is None:
        try:
            orchestration_repository.fail(
                binding,
                reason=(
                    decision_result.reason_code.value
                    if decision_result.reason_code is not None
                    else SingleJobPriorityReason.FINALIZATION_FAILED.value
                ),
                proposal=proposal,
            )
        except SingleJobPriorityRepositoryError:
            return _failure(
                command,
                SingleJobPriorityReason.ORCHESTRATION_PERSISTENCE_FAILED,
                "The failed Gate audit state could not be persisted.",
                input_binding=binding.input_binding,
                retryable=True,
                proposal_result=proposal_result,
            )
        return _failure(
            command,
            SingleJobPriorityReason.FINALIZATION_FAILED,
            decision_result.message,
            input_binding=binding.input_binding,
            retryable=decision_result.retryable,
            proposal_result=proposal_result,
        )

    try:
        orchestration_repository.complete(
            binding,
            proposal=proposal,
            decision=decision,
        )
    except SingleJobPriorityRepositoryError:
        try:
            orchestration_repository.fail(
                binding,
                reason=(
                    SingleJobPriorityReason.ORCHESTRATION_PERSISTENCE_FAILED.value
                ),
                proposal=proposal,
            )
        except SingleJobPriorityRepositoryError:
            pass
        return _failure(
            command,
            SingleJobPriorityReason.ORCHESTRATION_PERSISTENCE_FAILED,
            "The completed orchestration record could not be persisted.",
            input_binding=binding.input_binding,
            retryable=True,
            proposal_result=proposal_result,
        )
    return SingleJobPriorityResult(
        status=SingleJobPriorityStatus.SUCCEEDED,
        change=SingleJobPriorityChange.CREATED,
        reason_code=None,
        retryable=False,
        subject_id=subject_id,
        job_id=job_id,
        input_binding=binding.input_binding,
        proposal_outcome=PriorityArtifactWriteOutcome.CREATED,
        decision_outcome=PriorityArtifactWriteOutcome.CREATED,
        proposal_result=proposal_result,
        decision_result=decision_result,
        message="A new PriorityProposal and PriorityDecision were persisted.",
    )


__all__ = [
    "ActivePrioritizationPolicyProvider",
    "OrchestrationRecordStatus",
    "PriorityArtifactWriteOutcome",
    "PrivateHomeSingleJobPriorityRepository",
    "SINGLE_JOB_PRIORITY_ORCHESTRATION_VERSION",
    "SingleJobPriorityBinding",
    "SingleJobPriorityChange",
    "SingleJobPriorityCommand",
    "SingleJobPriorityReason",
    "SingleJobPriorityRepositoryError",
    "SingleJobPriorityResult",
    "SingleJobPriorityStatus",
    "StoredSingleJobPriority",
    "build_single_job_priority_binding",
    "completed_priority_bindings_match",
    "orchestrate_single_job_priority",
]
