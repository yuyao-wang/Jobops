"""Authenticated-user review boundary for Candidate Identity Fact proposals."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY,
    ApplicationExecutionIdentityFieldKey,
    ApplicationExecutionIdentityFieldRequiredness,
    normalize_application_execution_identity_value,
)
from .candidate_fact_proposals import (
    CandidateFactProposal,
    CandidateFactProposalConfidence,
    CandidateFactProposalListStatus,
    CandidateFactProposalReadStatus,
    CandidateFactProposalRepository,
)
from .candidate_identity_facts import (
    CANDIDATE_IDENTITY_FACT_SOURCE_CONTRACT_VERSION,
    CandidateIdentityFactRepository,
    CandidateIdentityFactSourceKind,
    CandidateIdentityFactSourceRef,
    CandidateIdentityFactVerificationStatus,
    GetCurrentCandidateIdentityFactCommand,
    GetCurrentCandidateIdentityFactStatus,
    WriteCandidateIdentityFactCommand,
    WriteCandidateIdentityFactStatus,
    get_current_candidate_identity_fact,
    write_candidate_identity_fact,
)
from .candidate_source_projections import (
    CandidateProjectionAssetPayload,
    CandidateSourceProjectionReadStatus,
    CandidateSourceProjectionRepository,
    read_candidate_projection_asset,
    read_candidate_projection_block,
)
from .private_home import PRIVATE_FILE_MODE, PrivateHome


CANDIDATE_FACT_REVIEW_CONTRACT_VERSION = "candidate-fact-review-v1"
CANDIDATE_FACT_REVIEW_DECISION_CONTRACT_VERSION = (
    "candidate-fact-review-decision-v1"
)
CANDIDATE_FACT_REVIEW_REPOSITORY_SCHEMA_VERSION = 1
MAX_REVIEW_ITEMS = 200
MAX_REVIEW_PREVIEWS = 3
MAX_REVIEW_TEXT_PREVIEW_CHARS = 240


class CandidateFactReviewItemKind(StrEnum):
    NEW_PROPOSAL = "NEW_PROPOSAL"
    UPDATE_PROPOSAL = "UPDATE_PROPOSAL"
    CONFLICTING_PROPOSALS = "CONFLICTING_PROPOSALS"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    DUPLICATE_OF_CURRENT = "DUPLICATE_OF_CURRENT"


class CandidateFactReviewAction(StrEnum):
    ACCEPT_PROPOSED = "ACCEPT_PROPOSED"
    ACCEPT_WITH_EDIT = "ACCEPT_WITH_EDIT"
    REJECT_PROPOSAL = "REJECT_PROPOSAL"
    KEEP_CURRENT = "KEEP_CURRENT"
    REPLACE_CURRENT = "REPLACE_CURRENT"
    PROVIDE_MISSING_VALUE = "PROVIDE_MISSING_VALUE"


class CandidateFactReviewQueueStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class CandidateFactReviewDecisionStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    STALE_REVIEW = "STALE_REVIEW"
    INVALID = "INVALID"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class CandidateFactReviewPreviewKind(StrEnum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"


class _ReviewIntegrityError(RuntimeError):
    pass


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("review time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


@dataclass(frozen=True, slots=True)
class CandidateFactReviewEvidencePreview:
    preview_kind: CandidateFactReviewPreviewKind
    evidence_id: str
    evidence_hash: str
    projection_id: str
    source_locator: Mapping[str, Any]
    text_excerpt: str | None = field(default=None, repr=False)
    media_type: str | None = None
    width: int | None = None
    height: int | None = None

    def identity_dict(self) -> dict[str, Any]:
        return {
            "evidence_hash": self.evidence_hash,
            "evidence_id": self.evidence_id,
            "preview_kind": self.preview_kind.value,
            "projection_id": self.projection_id,
            "source_locator": dict(self.source_locator),
        }


@dataclass(frozen=True, slots=True)
class CandidateFactReviewProposalView:
    proposal_id: str
    proposal_hash: str
    proposed_value: str = field(repr=False)
    confidence: CandidateFactProposalConfidence = (
        CandidateFactProposalConfidence.LOW
    )
    source_kind: str = ""
    source_id: str = ""
    projection_id: str = ""
    previews: tuple[CandidateFactReviewEvidencePreview, ...] = ()

    def identity_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence.value,
            "preview_bindings": tuple(item.identity_dict() for item in self.previews),
            "projection_id": self.projection_id,
            "proposal_hash": self.proposal_hash,
            "proposal_id": self.proposal_id,
            "proposed_value_hash": _sha(self.proposed_value.encode()),
            "source_kind": self.source_kind,
            "source_id": self.source_id,
        }


@dataclass(frozen=True, slots=True)
class CandidateFactReviewItem:
    review_item_id: str
    subject_id: str
    field_key: ApplicationExecutionIdentityFieldKey
    item_kind: CandidateFactReviewItemKind
    proposal: CandidateFactReviewProposalView | None
    conflicting_proposals: tuple[CandidateFactReviewProposalView, ...]
    current_fact_id: str | None
    current_value: str | None = field(repr=False)
    available_actions: tuple[CandidateFactReviewAction, ...] = ()
    priority: int = 0
    review_item_hash: str = ""
    review_contract_version: str = CANDIDATE_FACT_REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        expected = _hash(self.identity_dict())
        if (
            self.review_item_hash != expected
            or self.review_item_id
            != f"candidate-fact-review-item-{expected[:32]}"
        ):
            raise ValueError("review item identity is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "available_actions": tuple(item.value for item in self.available_actions),
            "conflicting_proposals": tuple(
                item.identity_dict() for item in self.conflicting_proposals
            ),
            "current_fact_id": self.current_fact_id,
            "field_key": self.field_key.value,
            "item_kind": self.item_kind.value,
            "priority": self.priority,
            "proposal": self.proposal.identity_dict() if self.proposal else None,
            "review_contract_version": self.review_contract_version,
            "subject_id": self.subject_id,
        }


@dataclass(frozen=True, slots=True)
class BuildCandidateFactReviewQueueCommand:
    subject_id: str
    now: datetime
    field_key: ApplicationExecutionIdentityFieldKey | None = None
    limit: int = 100


@dataclass(frozen=True, slots=True)
class CandidateFactReviewQueue:
    subject_id: str
    items: tuple[CandidateFactReviewItem, ...]
    queue_snapshot_hash: str
    pending_count: int
    conflict_count: int
    missing_required_count: int
    resolved_count: int
    evaluated_at: datetime
    review_contract_version: str = CANDIDATE_FACT_REVIEW_CONTRACT_VERSION


@dataclass(frozen=True, slots=True)
class BuildCandidateFactReviewQueueResult:
    status: CandidateFactReviewQueueStatus
    queue: CandidateFactReviewQueue | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class ResolveCandidateFactReviewCommand:
    subject_id: str
    review_item_id: str
    queue_snapshot_hash: str
    action: CandidateFactReviewAction
    invocation_id: str
    now: datetime
    submitted_value: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CandidateFactReviewClaim:
    decision_id: str
    subject_id: str
    review_item_id: str
    field_key: ApplicationExecutionIdentityFieldKey
    action: CandidateFactReviewAction
    proposal_id: str | None
    proposal_hash: str | None
    proposal_source_id: str | None
    proposal_projection_id: str | None
    expected_current_fact_id: str | None
    submitted_value: str | None = field(repr=False)
    submitted_value_hash: str | None = None
    queue_snapshot_hash: str = ""
    decision_hash: str = ""
    invocation_id: str = ""
    decided_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    decision_contract_version: str = (
        CANDIDATE_FACT_REVIEW_DECISION_CONTRACT_VERSION
    )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "decision_contract_version": self.decision_contract_version,
            "expected_current_fact_id": self.expected_current_fact_id,
            "field_key": self.field_key.value,
            "proposal_hash": self.proposal_hash,
            "proposal_id": self.proposal_id,
            "proposal_projection_id": self.proposal_projection_id,
            "proposal_source_id": self.proposal_source_id,
            "queue_snapshot_hash": self.queue_snapshot_hash,
            "review_item_id": self.review_item_id,
            "subject_id": self.subject_id,
            "submitted_value_hash": self.submitted_value_hash,
        }

    def __post_init__(self) -> None:
        expected = _hash(self.identity_dict())
        if (
            self.decision_hash != expected
            or self.decision_id
            != f"candidate-fact-review-decision-{expected[:32]}"
        ):
            raise ValueError("review claim identity is invalid")
        _time(self.decided_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "decided_at": _time(self.decided_at),
            "decision_hash": self.decision_hash,
            "decision_id": self.decision_id,
            "invocation_id": self.invocation_id,
            "submitted_value": self.submitted_value,
        }


@dataclass(frozen=True, slots=True)
class CandidateFactReviewDecision:
    decision_id: str
    decision_hash: str
    subject_id: str
    review_item_id: str
    field_key: ApplicationExecutionIdentityFieldKey
    action: CandidateFactReviewAction
    proposal_id: str | None
    proposal_hash: str | None
    proposal_source_id: str | None
    proposal_projection_id: str | None
    expected_current_fact_id: str | None
    submitted_value_hash: str | None
    queue_snapshot_hash: str
    created_fact_id: str | None
    fact_write_status: str | None
    decision_status: CandidateFactReviewDecisionStatus
    decided_at: datetime
    invocation_id: str
    decision_contract_version: str = (
        CANDIDATE_FACT_REVIEW_DECISION_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        identity = {
            "action": self.action.value,
            "decision_contract_version": self.decision_contract_version,
            "expected_current_fact_id": self.expected_current_fact_id,
            "field_key": self.field_key.value,
            "proposal_hash": self.proposal_hash,
            "proposal_id": self.proposal_id,
            "proposal_projection_id": self.proposal_projection_id,
            "proposal_source_id": self.proposal_source_id,
            "queue_snapshot_hash": self.queue_snapshot_hash,
            "review_item_id": self.review_item_id,
            "subject_id": self.subject_id,
            "submitted_value_hash": self.submitted_value_hash,
        }
        expected = _hash(identity)
        if (
            self.decision_hash != expected
            or self.decision_id
            != f"candidate-fact-review-decision-{expected[:32]}"
        ):
            raise ValueError("review decision identity is invalid")
        _time(self.decided_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "created_fact_id": self.created_fact_id,
            "decided_at": _time(self.decided_at),
            "decision_contract_version": self.decision_contract_version,
            "decision_hash": self.decision_hash,
            "decision_id": self.decision_id,
            "decision_status": self.decision_status.value,
            "expected_current_fact_id": self.expected_current_fact_id,
            "fact_write_status": self.fact_write_status,
            "field_key": self.field_key.value,
            "invocation_id": self.invocation_id,
            "proposal_hash": self.proposal_hash,
            "proposal_id": self.proposal_id,
            "proposal_projection_id": self.proposal_projection_id,
            "proposal_source_id": self.proposal_source_id,
            "queue_snapshot_hash": self.queue_snapshot_hash,
            "review_item_id": self.review_item_id,
            "subject_id": self.subject_id,
            "submitted_value_hash": self.submitted_value_hash,
        }


@dataclass(frozen=True, slots=True)
class ResolveCandidateFactReviewResult:
    status: CandidateFactReviewDecisionStatus
    decision: CandidateFactReviewDecision | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateFactReviewClaimResult:
    status: CandidateFactReviewDecisionStatus
    claim: CandidateFactReviewClaim | None = None
    decision: CandidateFactReviewDecision | None = None
    failure_code: str | None = None


@runtime_checkable
class CandidateFactReviewDecisionRepository(Protocol):
    def get_invocation(
        self, subject_id: str, invocation_id: str, request_hash: str
    ) -> CandidateFactReviewClaimResult | None: ...

    def claim(
        self, claim: CandidateFactReviewClaim, request_hash: str
    ) -> CandidateFactReviewClaimResult: ...

    def complete(
        self, decision: CandidateFactReviewDecision
    ) -> ResolveCandidateFactReviewResult: ...

    def resolved_proposal_ids(self, subject_id: str) -> frozenset[str]: ...

    def resolved_count(self, subject_id: str) -> int: ...


def _claim_from_dict(value: Mapping[str, Any]) -> CandidateFactReviewClaim:
    payload = dict(value)
    payload["field_key"] = ApplicationExecutionIdentityFieldKey(
        payload["field_key"]
    )
    payload["action"] = CandidateFactReviewAction(payload["action"])
    payload["decided_at"] = _parse_time(payload["decided_at"])
    return CandidateFactReviewClaim(**payload)


def _decision_from_dict(value: Mapping[str, Any]) -> CandidateFactReviewDecision:
    payload = dict(value)
    payload["field_key"] = ApplicationExecutionIdentityFieldKey(
        payload["field_key"]
    )
    payload["action"] = CandidateFactReviewAction(payload["action"])
    payload["decision_status"] = CandidateFactReviewDecisionStatus(
        payload["decision_status"]
    )
    payload["decided_at"] = _parse_time(payload["decided_at"])
    return CandidateFactReviewDecision(**payload)


class PrivateHomeCandidateFactReviewDecisionRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    @property
    def path(self) -> Path:
        return self._home.paths.candidate_fact_reviews

    def _connect(self) -> sqlite3.Connection:
        self._home.ensure()
        self._home.ensure_private_file(self.path)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata(
              key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS claims(
              decision_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,
              review_item_id TEXT NOT NULL,invocation_id TEXT NOT NULL UNIQUE,
              request_hash TEXT NOT NULL,record_hash TEXT NOT NULL,
              record_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(
              decision_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,
              proposal_id TEXT,decision_status TEXT NOT NULL,
              record_hash TEXT NOT NULL,record_json TEXT NOT NULL,
              FOREIGN KEY(decision_id) REFERENCES claims(decision_id));
            """
        )
        version = str(CANDIDATE_FACT_REVIEW_REPOSITORY_SCHEMA_VERSION)
        connection.execute(
            "INSERT OR IGNORE INTO metadata VALUES('schema_version',?)",
            (version,),
        )
        connection.commit()
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None or row["value"] != version:
            connection.close()
            raise _ReviewIntegrityError("review schema is unsupported")
        os.chmod(self.path, PRIVATE_FILE_MODE)
        return connection

    @staticmethod
    def _read(row: sqlite3.Row, parser):
        raw = row["record_json"]
        if row["record_hash"] != _sha(raw.encode()):
            raise _ReviewIntegrityError("review record drift")
        return parser(json.loads(raw))

    def get_invocation(
        self, subject_id: str, invocation_id: str, request_hash: str
    ) -> CandidateFactReviewClaimResult | None:
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM claims WHERE invocation_id=?",
                    (invocation_id,),
                ).fetchone()
                if row is None:
                    return None
                if (
                    row["subject_id"] != subject_id
                    or row["request_hash"] != request_hash
                ):
                    return CandidateFactReviewClaimResult(
                        CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE,
                        failure_code="REVIEW_INVOCATION_CONFLICT",
                    )
                claim = self._read(row, _claim_from_dict)
                receipt = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (claim.decision_id,),
                ).fetchone()
                if receipt is not None:
                    decision = self._read(receipt, _decision_from_dict)
                    return CandidateFactReviewClaimResult(
                        CandidateFactReviewDecisionStatus.UNCHANGED,
                        claim,
                        decision,
                    )
                return CandidateFactReviewClaimResult(
                    CandidateFactReviewDecisionStatus.UNCHANGED, claim
                )
        except (
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            _ReviewIntegrityError,
        ):
            return CandidateFactReviewClaimResult(
                CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE,
                failure_code="REVIEW_REPOSITORY_INTEGRITY",
            )

    def claim(
        self, claim: CandidateFactReviewClaim, request_hash: str
    ) -> CandidateFactReviewClaimResult:
        prior = self.get_invocation(
            claim.subject_id, claim.invocation_id, request_hash
        )
        if prior is not None:
            return prior
        try:
            raw = json.dumps(
                claim.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO claims VALUES(?,?,?,?,?,?,?)",
                    (
                        claim.decision_id,
                        claim.subject_id,
                        claim.review_item_id,
                        claim.invocation_id,
                        request_hash,
                        _sha(raw.encode()),
                        raw,
                    ),
                )
                connection.commit()
            return CandidateFactReviewClaimResult(
                CandidateFactReviewDecisionStatus.COMPLETED, claim
            )
        except sqlite3.IntegrityError:
            return self.get_invocation(
                claim.subject_id, claim.invocation_id, request_hash
            ) or CandidateFactReviewClaimResult(
                CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE,
                failure_code="REVIEW_CLAIM_CONFLICT",
            )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return CandidateFactReviewClaimResult(
                CandidateFactReviewDecisionStatus.FAILED,
                failure_code="REVIEW_CLAIM_FAILED",
            )

    def complete(
        self, decision: CandidateFactReviewDecision
    ) -> ResolveCandidateFactReviewResult:
        try:
            raw = json.dumps(
                decision.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM decisions WHERE decision_id=?",
                    (decision.decision_id,),
                ).fetchone()
                if existing is not None:
                    prior = self._read(existing, _decision_from_dict)
                    connection.rollback()
                    if prior.to_dict() != decision.to_dict():
                        return ResolveCandidateFactReviewResult(
                            CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE,
                            failure_code="REVIEW_RECEIPT_CONFLICT",
                        )
                    return ResolveCandidateFactReviewResult(
                        CandidateFactReviewDecisionStatus.UNCHANGED, prior
                    )
                connection.execute(
                    "INSERT INTO decisions VALUES(?,?,?,?,?,?)",
                    (
                        decision.decision_id,
                        decision.subject_id,
                        decision.proposal_id,
                        decision.decision_status.value,
                        _sha(raw.encode()),
                        raw,
                    ),
                )
                connection.commit()
            return ResolveCandidateFactReviewResult(
                decision.decision_status, decision
            )
        except (
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            _ReviewIntegrityError,
        ):
            return ResolveCandidateFactReviewResult(
                CandidateFactReviewDecisionStatus.FAILED,
                failure_code="REVIEW_RECEIPT_FAILED",
            )

    def resolved_proposal_ids(self, subject_id: str) -> frozenset[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT proposal_id FROM decisions
                WHERE subject_id=? AND proposal_id IS NOT NULL
                  AND decision_status IN ('COMPLETED','UNCHANGED')
                """,
                (subject_id,),
            ).fetchall()
        return frozenset(row["proposal_id"] for row in rows)

    def resolved_count(self, subject_id: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM decisions
                WHERE subject_id=? AND decision_status IN ('COMPLETED','UNCHANGED')
                """,
                (subject_id,),
            ).fetchone()
        return int(row["count"])


def _proposal_view(
    proposal: CandidateFactProposal,
    *,
    projection_repository: CandidateSourceProjectionRepository,
) -> CandidateFactReviewProposalView:
    previews: list[CandidateFactReviewEvidencePreview] = []
    for evidence in proposal.evidence_refs[:MAX_REVIEW_PREVIEWS]:
        if evidence.evidence_kind == "BLOCK":
            result = read_candidate_projection_block(
                proposal.subject_id,
                proposal.projection_id,
                evidence.evidence_id,
                repository=projection_repository,
            )
            if (
                result.status is not CandidateSourceProjectionReadStatus.FOUND
                or result.block is None
                or result.block.block_hash != evidence.evidence_hash
                or result.block.source_locator.to_dict()
                != evidence.source_locator.to_dict()
            ):
                raise _ReviewIntegrityError("review block evidence drift")
            previews.append(
                CandidateFactReviewEvidencePreview(
                    CandidateFactReviewPreviewKind.TEXT,
                    evidence.evidence_id,
                    evidence.evidence_hash,
                    proposal.projection_id,
                    evidence.source_locator.to_dict(),
                    result.block.text[:MAX_REVIEW_TEXT_PREVIEW_CHARS],
                )
            )
        else:
            result = read_candidate_projection_asset(
                proposal.subject_id,
                proposal.projection_id,
                evidence.evidence_id,
                repository=projection_repository,
            )
            payload = result.asset_payload
            if (
                result.status is not CandidateSourceProjectionReadStatus.FOUND
                or payload is None
                or payload.asset.content_hash != evidence.evidence_hash
                or payload.asset.source_locator.to_dict()
                != evidence.source_locator.to_dict()
            ):
                raise _ReviewIntegrityError("review asset evidence drift")
            previews.append(
                CandidateFactReviewEvidencePreview(
                    CandidateFactReviewPreviewKind.IMAGE,
                    evidence.evidence_id,
                    evidence.evidence_hash,
                    proposal.projection_id,
                    evidence.source_locator.to_dict(),
                    media_type=payload.asset.media_type,
                    width=payload.asset.width,
                    height=payload.asset.height,
                )
            )
    return CandidateFactReviewProposalView(
        proposal.proposal_id,
        proposal.proposal_hash,
        proposal.proposed_normalized_value,
        proposal.confidence,
        proposal.source_kind.value,
        proposal.source_id,
        proposal.projection_id,
        tuple(previews),
    )


def _make_item(
    *,
    subject_id: str,
    field_key: ApplicationExecutionIdentityFieldKey,
    kind: CandidateFactReviewItemKind,
    proposal: CandidateFactReviewProposalView | None,
    conflicts: Sequence[CandidateFactReviewProposalView],
    current_fact_id: str | None,
    current_value: str | None,
) -> CandidateFactReviewItem:
    if kind is CandidateFactReviewItemKind.MISSING_REQUIRED_FIELD:
        actions = (CandidateFactReviewAction.PROVIDE_MISSING_VALUE,)
        priority = 1
    elif kind is CandidateFactReviewItemKind.DUPLICATE_OF_CURRENT:
        actions = (
            CandidateFactReviewAction.REJECT_PROPOSAL,
            CandidateFactReviewAction.KEEP_CURRENT,
        )
        priority = 3
    elif current_fact_id is None:
        actions = (
            CandidateFactReviewAction.ACCEPT_PROPOSED,
            CandidateFactReviewAction.ACCEPT_WITH_EDIT,
            CandidateFactReviewAction.REJECT_PROPOSAL,
        )
        priority = 0 if kind is CandidateFactReviewItemKind.CONFLICTING_PROPOSALS else 2
    else:
        actions = (
            CandidateFactReviewAction.REPLACE_CURRENT,
            CandidateFactReviewAction.ACCEPT_WITH_EDIT,
            CandidateFactReviewAction.KEEP_CURRENT,
            CandidateFactReviewAction.REJECT_PROPOSAL,
        )
        priority = 0 if kind is CandidateFactReviewItemKind.CONFLICTING_PROPOSALS else 2
    identity = {
        "available_actions": tuple(item.value for item in actions),
        "conflicting_proposals": tuple(item.identity_dict() for item in conflicts),
        "current_fact_id": current_fact_id,
        "field_key": field_key.value,
        "item_kind": kind.value,
        "priority": priority,
        "proposal": proposal.identity_dict() if proposal else None,
        "review_contract_version": CANDIDATE_FACT_REVIEW_CONTRACT_VERSION,
        "subject_id": subject_id,
    }
    digest = _hash(identity)
    return CandidateFactReviewItem(
        f"candidate-fact-review-item-{digest[:32]}",
        subject_id,
        field_key,
        kind,
        proposal,
        tuple(conflicts),
        current_fact_id,
        current_value,
        actions,
        priority,
        digest,
    )


def build_candidate_fact_review_queue(
    command: BuildCandidateFactReviewQueueCommand,
    *,
    proposal_repository: CandidateFactProposalRepository,
    current_fact_repository: CandidateIdentityFactRepository,
    projection_repository: CandidateSourceProjectionRepository,
    decision_repository: CandidateFactReviewDecisionRepository,
) -> BuildCandidateFactReviewQueueResult:
    try:
        if not 1 <= command.limit <= MAX_REVIEW_ITEMS:
            raise ValueError("review item limit is invalid")
        _time(command.now)
        listing = proposal_repository.list_for_subject(
            command.subject_id,
            field_key=command.field_key,
            limit=MAX_REVIEW_ITEMS,
        )
        if listing.status is not CandidateFactProposalListStatus.SUCCEEDED:
            raise _ReviewIntegrityError("proposal list is unavailable")
        resolved = decision_repository.resolved_proposal_ids(command.subject_id)
        grouped: dict[
            ApplicationExecutionIdentityFieldKey, list[CandidateFactProposal]
        ] = {}
        for summary in listing.proposals:
            if summary.proposal_id in resolved:
                continue
            read = proposal_repository.get(
                command.subject_id, summary.proposal_id
            )
            if (
                read.status is not CandidateFactProposalReadStatus.FOUND
                or read.proposal is None
                or read.proposal.proposal_hash != summary.proposal_hash
            ):
                raise _ReviewIntegrityError("proposal binding is unavailable")
            grouped.setdefault(read.proposal.field_key, []).append(read.proposal)

        field_keys = (
            (command.field_key,)
            if command.field_key is not None
            else tuple(APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY)
        )
        items: list[CandidateFactReviewItem] = []
        for field_key in field_keys:
            current = get_current_candidate_identity_fact(
                GetCurrentCandidateIdentityFactCommand(
                    command.subject_id, field_key
                ),
                repository=current_fact_repository,
            )
            if current.status in {
                GetCurrentCandidateIdentityFactStatus.CONFLICT,
                GetCurrentCandidateIdentityFactStatus.INTEGRITY_FAILURE,
            }:
                raise _ReviewIntegrityError("current fact is unavailable")
            current_fact = current.fact
            proposals = sorted(
                grouped.get(field_key, ()), key=lambda item: item.proposal_id
            )
            views = tuple(
                _proposal_view(
                    proposal, projection_repository=projection_repository
                )
                for proposal in proposals
            )
            unique_values = {item.proposed_normalized_value for item in proposals}
            for proposal, view in zip(proposals, views, strict=True):
                if (
                    current_fact is not None
                    and proposal.proposed_normalized_value
                    == current_fact.normalized_value
                ):
                    kind = CandidateFactReviewItemKind.DUPLICATE_OF_CURRENT
                elif len(unique_values) > 1:
                    kind = CandidateFactReviewItemKind.CONFLICTING_PROPOSALS
                elif current_fact is None:
                    kind = CandidateFactReviewItemKind.NEW_PROPOSAL
                else:
                    kind = CandidateFactReviewItemKind.UPDATE_PROPOSAL
                items.append(
                    _make_item(
                        subject_id=command.subject_id,
                        field_key=field_key,
                        kind=kind,
                        proposal=view,
                        conflicts=views if len(unique_values) > 1 else (),
                        current_fact_id=(
                            current_fact.fact_id if current_fact else None
                        ),
                        current_value=(
                            current_fact.normalized_value
                            if current_fact
                            else None
                        ),
                    )
                )
            definition = (
                APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY[
                    field_key
                ]
            )
            if (
                current_fact is None
                and not proposals
                and definition.requiredness
                is ApplicationExecutionIdentityFieldRequiredness.REQUIRED_FOR_EXECUTION
            ):
                items.append(
                    _make_item(
                        subject_id=command.subject_id,
                        field_key=field_key,
                        kind=CandidateFactReviewItemKind.MISSING_REQUIRED_FIELD,
                        proposal=None,
                        conflicts=(),
                        current_fact_id=None,
                        current_value=None,
                    )
                )
        items.sort(
            key=lambda item: (
                item.priority,
                list(ApplicationExecutionIdentityFieldKey).index(
                    item.field_key
                ),
                item.review_item_id,
            )
        )
        items = items[: command.limit]
        snapshot = _hash(
            {
                "items": tuple(item.review_item_hash for item in items),
                "review_contract_version": CANDIDATE_FACT_REVIEW_CONTRACT_VERSION,
                "subject_id": command.subject_id,
            }
        )
        queue = CandidateFactReviewQueue(
            command.subject_id,
            tuple(items),
            snapshot,
            len(items),
            sum(
                item.item_kind
                is CandidateFactReviewItemKind.CONFLICTING_PROPOSALS
                for item in items
            ),
            sum(
                item.item_kind
                is CandidateFactReviewItemKind.MISSING_REQUIRED_FIELD
                for item in items
            ),
            decision_repository.resolved_count(command.subject_id),
            command.now,
        )
        return BuildCandidateFactReviewQueueResult(
            CandidateFactReviewQueueStatus.SUCCEEDED, queue
        )
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        _ReviewIntegrityError,
    ):
        return BuildCandidateFactReviewQueueResult(
            CandidateFactReviewQueueStatus.INTEGRITY_FAILURE,
            failure_code="REVIEW_QUEUE_INTEGRITY",
        )
    except Exception:
        return BuildCandidateFactReviewQueueResult(
            CandidateFactReviewQueueStatus.FAILED,
            failure_code="REVIEW_QUEUE_FAILED",
        )


def _request_hash(command: ResolveCandidateFactReviewCommand) -> str:
    return _hash(
        {
            "action": command.action.value,
            "invocation_id": command.invocation_id,
            "queue_snapshot_hash": command.queue_snapshot_hash,
            "review_item_id": command.review_item_id,
            "subject_id": command.subject_id,
            "submitted_value_hash": (
                _sha(command.submitted_value.encode())
                if isinstance(command.submitted_value, str)
                else None
            ),
        }
    )


def _claim_for_item(
    command: ResolveCandidateFactReviewCommand,
    item: CandidateFactReviewItem,
) -> CandidateFactReviewClaim:
    action = CandidateFactReviewAction(command.action)
    if action not in item.available_actions:
        raise ValueError("review action is unavailable")
    proposal = item.proposal
    if action in {
        CandidateFactReviewAction.ACCEPT_PROPOSED,
        CandidateFactReviewAction.REPLACE_CURRENT,
    }:
        if proposal is None or command.submitted_value is not None:
            raise ValueError("proposal action payload is invalid")
        submitted = proposal.proposed_value
    elif action in {
        CandidateFactReviewAction.ACCEPT_WITH_EDIT,
        CandidateFactReviewAction.PROVIDE_MISSING_VALUE,
    }:
        if not isinstance(command.submitted_value, str):
            raise ValueError("review value is required")
        submitted = normalize_application_execution_identity_value(
            item.field_key, command.submitted_value
        )
    else:
        if command.submitted_value is not None:
            raise ValueError("review value is not allowed")
        submitted = None
    identity = {
        "action": action.value,
        "decision_contract_version": CANDIDATE_FACT_REVIEW_DECISION_CONTRACT_VERSION,
        "expected_current_fact_id": item.current_fact_id,
        "field_key": item.field_key.value,
        "proposal_hash": proposal.proposal_hash if proposal else None,
        "proposal_id": proposal.proposal_id if proposal else None,
        "proposal_projection_id": proposal.projection_id if proposal else None,
        "proposal_source_id": proposal.source_id if proposal else None,
        "queue_snapshot_hash": command.queue_snapshot_hash,
        "review_item_id": item.review_item_id,
        "subject_id": command.subject_id,
        "submitted_value_hash": _sha(submitted.encode()) if submitted else None,
    }
    digest = _hash(identity)
    return CandidateFactReviewClaim(
        f"candidate-fact-review-decision-{digest[:32]}",
        command.subject_id,
        item.review_item_id,
        item.field_key,
        action,
        proposal.proposal_id if proposal else None,
        proposal.proposal_hash if proposal else None,
        proposal.source_id if proposal else None,
        proposal.projection_id if proposal else None,
        item.current_fact_id,
        submitted,
        identity["submitted_value_hash"],
        command.queue_snapshot_hash,
        digest,
        command.invocation_id,
        command.now,
    )


def _decision(
    claim: CandidateFactReviewClaim,
    *,
    status: CandidateFactReviewDecisionStatus,
    created_fact_id: str | None = None,
    fact_write_status: str | None = None,
) -> CandidateFactReviewDecision:
    return CandidateFactReviewDecision(
        claim.decision_id,
        claim.decision_hash,
        claim.subject_id,
        claim.review_item_id,
        claim.field_key,
        claim.action,
        claim.proposal_id,
        claim.proposal_hash,
        claim.proposal_source_id,
        claim.proposal_projection_id,
        claim.expected_current_fact_id,
        claim.submitted_value_hash,
        claim.queue_snapshot_hash,
        created_fact_id,
        fact_write_status,
        status,
        claim.decided_at,
        claim.invocation_id,
    )


def resolve_candidate_fact_review(
    command: ResolveCandidateFactReviewCommand,
    *,
    proposal_repository: CandidateFactProposalRepository,
    current_fact_repository: CandidateIdentityFactRepository,
    projection_repository: CandidateSourceProjectionRepository,
    decision_repository: CandidateFactReviewDecisionRepository,
) -> ResolveCandidateFactReviewResult:
    try:
        request_hash = _request_hash(command)
        existing = decision_repository.get_invocation(
            command.subject_id, command.invocation_id, request_hash
        )
        if existing is not None:
            if existing.status is CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE:
                return ResolveCandidateFactReviewResult(
                    existing.status, failure_code=existing.failure_code
                )
            if existing.decision is not None:
                replay_status = (
                    CandidateFactReviewDecisionStatus.UNCHANGED
                    if existing.decision.decision_status
                    is CandidateFactReviewDecisionStatus.COMPLETED
                    else existing.decision.decision_status
                )
                return ResolveCandidateFactReviewResult(
                    replay_status,
                    existing.decision,
                )
            if existing.claim is None:
                return ResolveCandidateFactReviewResult(
                    CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE,
                    failure_code="REVIEW_CLAIM_MISSING",
                )
            claim = existing.claim
        else:
            queue_result = build_candidate_fact_review_queue(
                BuildCandidateFactReviewQueueCommand(
                    command.subject_id, command.now
                ),
                proposal_repository=proposal_repository,
                current_fact_repository=current_fact_repository,
                projection_repository=projection_repository,
                decision_repository=decision_repository,
            )
            if (
                queue_result.status is not CandidateFactReviewQueueStatus.SUCCEEDED
                or queue_result.queue is None
            ):
                return ResolveCandidateFactReviewResult(
                    CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE,
                    failure_code="REVIEW_QUEUE_UNAVAILABLE",
                )
            queue = queue_result.queue
            if queue.queue_snapshot_hash != command.queue_snapshot_hash:
                return ResolveCandidateFactReviewResult(
                    CandidateFactReviewDecisionStatus.STALE_REVIEW,
                    failure_code="REVIEW_SNAPSHOT_STALE",
                )
            item = next(
                (
                    value
                    for value in queue.items
                    if value.review_item_id == command.review_item_id
                ),
                None,
            )
            if item is None:
                return ResolveCandidateFactReviewResult(
                    CandidateFactReviewDecisionStatus.STALE_REVIEW,
                    failure_code="REVIEW_ITEM_STALE",
                )
            claim = _claim_for_item(command, item)
            claimed = decision_repository.claim(claim, request_hash)
            if claimed.status in {
                CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE,
                CandidateFactReviewDecisionStatus.FAILED,
            }:
                return ResolveCandidateFactReviewResult(
                    claimed.status, failure_code=claimed.failure_code
                )
            claim = claimed.claim or claim
            if claimed.decision is not None:
                replay_status = (
                    CandidateFactReviewDecisionStatus.UNCHANGED
                    if claimed.decision.decision_status
                    is CandidateFactReviewDecisionStatus.COMPLETED
                    else claimed.decision.decision_status
                )
                return ResolveCandidateFactReviewResult(
                    replay_status,
                    claimed.decision,
                )

        if claim.action in {
            CandidateFactReviewAction.REJECT_PROPOSAL,
            CandidateFactReviewAction.KEEP_CURRENT,
        }:
            return decision_repository.complete(
                _decision(
                    claim, status=CandidateFactReviewDecisionStatus.COMPLETED
                )
            )
        if claim.submitted_value is None:
            return ResolveCandidateFactReviewResult(
                CandidateFactReviewDecisionStatus.INVALID,
                failure_code="REVIEW_VALUE_MISSING",
            )
        source_ref = CandidateIdentityFactSourceRef(
            CandidateIdentityFactSourceKind.USER_CONFIRMATION,
            claim.decision_id,
            CANDIDATE_FACT_REVIEW_DECISION_CONTRACT_VERSION,
            claim.decision_hash,
            f"review:{claim.review_item_id}",
            claim.subject_id,
            CANDIDATE_IDENTITY_FACT_SOURCE_CONTRACT_VERSION,
        )
        written = write_candidate_identity_fact(
            WriteCandidateIdentityFactCommand(
                claim.subject_id,
                claim.field_key,
                claim.submitted_value,
                CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
                source_ref,
                claim.expected_current_fact_id,
                f"candidate-review-fact-{claim.decision_hash[:32]}",
                claim.decided_at,
            ),
            repository=current_fact_repository,
        )
        if written.status in {
            WriteCandidateIdentityFactStatus.CREATED,
            WriteCandidateIdentityFactStatus.UNCHANGED,
        }:
            decision_status = CandidateFactReviewDecisionStatus.COMPLETED
        elif written.status is WriteCandidateIdentityFactStatus.STALE_CURRENT:
            decision_status = CandidateFactReviewDecisionStatus.STALE_REVIEW
        elif written.status is WriteCandidateIdentityFactStatus.INVALID:
            decision_status = CandidateFactReviewDecisionStatus.INVALID
        elif written.status is WriteCandidateIdentityFactStatus.INTEGRITY_FAILURE:
            decision_status = CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE
        else:
            decision_status = CandidateFactReviewDecisionStatus.PARTIAL_FAILURE
        decision = _decision(
            claim,
            status=decision_status,
            created_fact_id=written.fact.fact_id if written.fact else None,
            fact_write_status=written.status.value,
        )
        return decision_repository.complete(decision)
    except (TypeError, ValueError):
        return ResolveCandidateFactReviewResult(
            CandidateFactReviewDecisionStatus.INVALID,
            failure_code="REVIEW_REQUEST_INVALID",
        )
    except Exception:
        return ResolveCandidateFactReviewResult(
            CandidateFactReviewDecisionStatus.FAILED,
            failure_code="REVIEW_FAILED",
        )


def read_candidate_fact_review_asset(
    *,
    subject_id: str,
    review_item_id: str,
    evidence_id: str,
    now: datetime,
    proposal_repository: CandidateFactProposalRepository,
    current_fact_repository: CandidateIdentityFactRepository,
    projection_repository: CandidateSourceProjectionRepository,
    decision_repository: CandidateFactReviewDecisionRepository,
) -> CandidateProjectionAssetPayload | None:
    queue = build_candidate_fact_review_queue(
        BuildCandidateFactReviewQueueCommand(subject_id, now),
        proposal_repository=proposal_repository,
        current_fact_repository=current_fact_repository,
        projection_repository=projection_repository,
        decision_repository=decision_repository,
    )
    if queue.queue is None:
        return None
    item = next(
        (
            value
            for value in queue.queue.items
            if value.review_item_id == review_item_id
        ),
        None,
    )
    if item is None:
        return None
    views = (item.proposal,) if item.proposal is not None else ()
    for view in views:
        for preview in view.previews:
            if (
                preview.preview_kind is CandidateFactReviewPreviewKind.IMAGE
                and preview.evidence_id == evidence_id
            ):
                result = read_candidate_projection_asset(
                    subject_id,
                    preview.projection_id,
                    evidence_id,
                    repository=projection_repository,
                )
                if (
                    result.status is CandidateSourceProjectionReadStatus.FOUND
                    and result.asset_payload is not None
                    and result.asset_payload.asset.content_hash
                    == preview.evidence_hash
                ):
                    return result.asset_payload
    return None


__all__ = [
    "BuildCandidateFactReviewQueueCommand",
    "BuildCandidateFactReviewQueueResult",
    "CandidateFactReviewAction",
    "CandidateFactReviewDecision",
    "CandidateFactReviewDecisionRepository",
    "CandidateFactReviewDecisionStatus",
    "CandidateFactReviewEvidencePreview",
    "CandidateFactReviewItem",
    "CandidateFactReviewItemKind",
    "CandidateFactReviewPreviewKind",
    "CandidateFactReviewProposalView",
    "CandidateFactReviewQueue",
    "CandidateFactReviewQueueStatus",
    "PrivateHomeCandidateFactReviewDecisionRepository",
    "ResolveCandidateFactReviewCommand",
    "ResolveCandidateFactReviewResult",
    "build_candidate_fact_review_queue",
    "read_candidate_fact_review_asset",
    "resolve_candidate_fact_review",
]
