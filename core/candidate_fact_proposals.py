"""Bounded Agent-assisted proposals over exact Candidate Source Projections."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY,
    APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION,
    APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION,
    ApplicationExecutionIdentityFieldDefinition,
    ApplicationExecutionIdentityFieldKey,
    normalize_application_execution_identity_value,
)
from .candidate_source_projections import (
    CANDIDATE_SOURCE_LOCATOR_CONTRACT_VERSION,
    CandidateProjectionAsset,
    CandidateProjectionAssetPayload,
    CandidateProjectionBlock,
    CandidateProjectionBlockType,
    CandidateSourceLocator,
    CandidateSourceProjection,
    CandidateSourceProjectionReadStatus,
    CandidateSourceProjectionRepository,
    get_candidate_source_projection,
    read_candidate_projection_asset,
    read_candidate_projection_block,
)
from .candidate_identity_facts import (
    CandidateIdentityFactSourceKind,
    CandidateIdentityFactSourceRef,
)
from .candidate_information_sources import CandidateInformationSourceKind
from .private_home import PRIVATE_FILE_MODE, PrivateHome


CANDIDATE_FACT_PROPOSAL_CONTRACT_VERSION = "candidate-fact-proposal-v1"
CANDIDATE_FACT_PROPOSAL_RUN_CONTRACT_VERSION = "candidate-fact-proposal-run-v1"
CANDIDATE_FACT_PROPOSAL_INPUT_CONTRACT_VERSION = "candidate-fact-proposal-input-v1"
CANDIDATE_FACT_PROPOSAL_SELECTION_POLICY_VERSION = "candidate-fact-proposal-selection-v1"
CANDIDATE_FACT_PROPOSAL_AGENT_POLICY_VERSION = "candidate-fact-proposal-agent-policy-v1"
CANDIDATE_FACT_PROPOSAL_AGENT_SCHEMA_VERSION = "candidate-fact-proposal-agent-schema-v1"
CANDIDATE_FACT_PROPOSAL_REPOSITORY_SCHEMA_VERSION = 1
CANDIDATE_FACT_PROPOSAL_COMPONENT_ID = "candidate_fact_proposal"

MAX_PROPOSAL_BLOCKS = 80
MAX_PROPOSAL_TEXT_BYTES = 96 * 1024
MAX_PROPOSAL_ASSETS = 4
MAX_PROPOSAL_ASSET_BYTES = 8 * 1024 * 1024
MAX_PROPOSAL_TOTAL_ASSET_BYTES = 20 * 1024 * 1024
MAX_PROPOSALS_PER_RUN = 40
MAX_EVIDENCE_REFS = 8
MAX_EVIDENCE_EXCERPT_CHARS = 500
MAX_EXTRACTION_NOTE_CHARS = 240

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,239}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


class CandidateFactProposalConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class CandidateFactProposalRunStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    NO_PROPOSALS = "NO_PROPOSALS"
    PARTIAL_VALIDATION = "PARTIAL_VALIDATION"
    DEFERRED_INPUT_UNSUPPORTED = "DEFERRED_INPUT_UNSUPPORTED"
    DEFERRED_BACKEND_UNAVAILABLE = "DEFERRED_BACKEND_UNAVAILABLE"
    FAILED_AGENT_OUTPUT = "FAILED_AGENT_OUTPUT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


class CandidateFactProposalReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CandidateFactProposalListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CandidateFactProposalAgentUnavailableError(RuntimeError):
    pass


class CandidateFactProposalAgentOutputError(RuntimeError):
    pass


class CandidateFactProposalInputUnsupportedError(RuntimeError):
    pass


class _ProposalIntegrityError(RuntimeError):
    pass


def _clean_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _clean_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _time(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateFactProposalSelectionPolicy:
    max_blocks: int = MAX_PROPOSAL_BLOCKS
    max_text_bytes: int = MAX_PROPOSAL_TEXT_BYTES
    max_assets: int = MAX_PROPOSAL_ASSETS
    max_asset_bytes_each: int = MAX_PROPOSAL_ASSET_BYTES
    max_total_asset_bytes: int = MAX_PROPOSAL_TOTAL_ASSET_BYTES
    use_assets_only_when_no_text_blocks: bool = True
    policy_version: str = CANDIDATE_FACT_PROPOSAL_SELECTION_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.policy_version != CANDIDATE_FACT_PROPOSAL_SELECTION_POLICY_VERSION:
            raise ValueError("selection policy version is unsupported")
        for name in (
            "max_blocks", "max_text_bytes", "max_assets",
            "max_asset_bytes_each", "max_total_asset_bytes",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("selection limits must be positive")


@dataclass(frozen=True, slots=True)
class CandidateFactProposalSelectedBlock:
    block_id: str
    block_hash: str
    block_type: CandidateProjectionBlockType
    ordinal: int
    text: str = field(repr=False)
    source_locator: CandidateSourceLocator

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        value = {
            "block_hash": self.block_hash,
            "block_id": self.block_id,
            "block_type": self.block_type.value,
            "ordinal": self.ordinal,
            "source_locator": self.source_locator.to_dict(),
        }
        if include_text:
            value["text"] = self.text
        return value


@dataclass(frozen=True, slots=True)
class CandidateFactProposalSelectedAsset:
    asset_id: str
    asset_hash: str
    asset_kind: str
    media_type: str
    ordinal: int
    byte_size: int
    width: int
    height: int
    source_locator: CandidateSourceLocator
    content: bytes = field(repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_hash": self.asset_hash,
            "asset_id": self.asset_id,
            "asset_kind": self.asset_kind,
            "byte_size": self.byte_size,
            "height": self.height,
            "media_type": self.media_type,
            "ordinal": self.ordinal,
            "source_locator": self.source_locator.to_dict(),
            "width": self.width,
        }


@dataclass(frozen=True, slots=True)
class CandidateFactProposalInputSnapshot:
    input_snapshot_id: str
    subject_id: str
    source_id: str
    source_kind: CandidateInformationSourceKind
    source_version: str
    source_hash: str
    projection_id: str
    projection_hash: str
    selected_blocks: tuple[CandidateFactProposalSelectedBlock, ...]
    selected_assets: tuple[CandidateFactProposalSelectedAsset, ...]
    allowed_field_keys: tuple[ApplicationExecutionIdentityFieldKey, ...]
    truncation_codes: tuple[str, ...]
    input_snapshot_hash: str
    selection_policy_version: str = CANDIDATE_FACT_PROPOSAL_SELECTION_POLICY_VERSION
    agent_policy_version: str = CANDIDATE_FACT_PROPOSAL_AGENT_POLICY_VERSION
    input_contract_version: str = CANDIDATE_FACT_PROPOSAL_INPUT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("input_snapshot_id", "subject_id", "source_id", "projection_id"):
            _clean_id(name, getattr(self, name))
        object.__setattr__(
            self, "source_kind", CandidateInformationSourceKind(self.source_kind)
        )
        for name in ("source_hash", "projection_hash", "input_snapshot_hash"):
            _clean_hash(name, getattr(self, name))
        if (
            self.input_contract_version != CANDIDATE_FACT_PROPOSAL_INPUT_CONTRACT_VERSION
            or self.selection_policy_version != CANDIDATE_FACT_PROPOSAL_SELECTION_POLICY_VERSION
            or self.agent_policy_version != CANDIDATE_FACT_PROPOSAL_AGENT_POLICY_VERSION
        ):
            raise ValueError("input snapshot version is unsupported")
        if not self.selected_blocks and not self.selected_assets:
            raise ValueError("input snapshot has no selected evidence")
        expected = _hash(self.binding_dict())
        if (
            self.input_snapshot_hash != expected
            or self.input_snapshot_id != f"candidate-proposal-input-{expected[:32]}"
        ):
            raise ValueError("input snapshot identity is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "agent_policy_version": self.agent_policy_version,
            "allowed_field_keys": [item.value for item in self.allowed_field_keys],
            "input_contract_version": self.input_contract_version,
            "projection_hash": self.projection_hash,
            "projection_id": self.projection_id,
            "selected_assets": [item.to_dict() for item in self.selected_assets],
            "selected_blocks": [
                {
                    **item.to_dict(include_text=False),
                    "text_hash": _sha256(item.text.encode()),
                }
                for item in self.selected_blocks
            ],
            "selection_policy_version": self.selection_policy_version,
            "source_hash": self.source_hash,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "source_version": self.source_version,
            "subject_id": self.subject_id,
            "truncation_codes": list(self.truncation_codes),
        }


@dataclass(frozen=True, slots=True)
class CandidateFactProposalAgentEvidenceRef:
    block_id: str | None = None
    block_hash: str | None = None
    asset_id: str | None = None
    asset_hash: str | None = None
    source_locator: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CandidateFactProposalAgentItem:
    field_key: str
    proposed_value: str = field(repr=False)
    evidence_refs: tuple[CandidateFactProposalAgentEvidenceRef, ...]
    evidence_excerpt: str = field(repr=False)
    confidence: CandidateFactProposalConfidence
    extraction_note: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CandidateFactProposalAgentOutput:
    proposals: tuple[CandidateFactProposalAgentItem, ...]


@dataclass(frozen=True, slots=True)
class CandidateFactProposalAgentContext:
    input_snapshot: CandidateFactProposalInputSnapshot
    field_definitions: tuple[ApplicationExecutionIdentityFieldDefinition, ...]
    output_schema: Mapping[str, Any]


@runtime_checkable
class CandidateFactProposalAgentPort(Protocol):
    async def propose(
        self, context: CandidateFactProposalAgentContext
    ) -> CandidateFactProposalAgentOutput: ...


@dataclass(frozen=True, slots=True)
class CandidateFactProposalAgentMetadata:
    component_id: str
    backend_id: str
    model_id: str
    prompt_policy_version: str
    schema_version: str
    backend_resolution_identity: str

    def __post_init__(self) -> None:
        if self.component_id != CANDIDATE_FACT_PROPOSAL_COMPONENT_ID:
            raise ValueError("proposal Agent component is invalid")
        for name in (
            "backend_id", "model_id", "prompt_policy_version",
            "schema_version", "backend_resolution_identity",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError("proposal Agent metadata is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "backend_resolution_identity": self.backend_resolution_identity,
            "component_id": self.component_id,
            "model_id": self.model_id,
            "prompt_policy_version": self.prompt_policy_version,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class CandidateFactProposalEvidenceRef:
    evidence_kind: str
    evidence_id: str
    evidence_hash: str
    source_locator: CandidateSourceLocator

    def __post_init__(self) -> None:
        if self.evidence_kind not in {"BLOCK", "ASSET"}:
            raise ValueError("evidence kind is invalid")
        _clean_id("evidence_id", self.evidence_id)
        _clean_hash("evidence_hash", self.evidence_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_hash": self.evidence_hash,
            "evidence_id": self.evidence_id,
            "evidence_kind": self.evidence_kind,
            "source_locator": self.source_locator.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CandidateFactProposal:
    proposal_id: str
    subject_id: str
    field_key: ApplicationExecutionIdentityFieldKey
    proposed_raw_value: str = field(repr=False)
    proposed_normalized_value: str = field(repr=False)
    value_type: str
    normalization_policy_version: str
    source_id: str
    source_kind: CandidateInformationSourceKind
    source_version: str
    source_hash: str
    projection_id: str
    projection_hash: str
    evidence_refs: tuple[CandidateFactProposalEvidenceRef, ...]
    evidence_excerpt_hash: str
    agent_component_id: str
    backend_id: str
    model_id: str
    prompt_policy_version: str
    schema_version: str
    confidence: CandidateFactProposalConfidence
    proposal_hash: str
    proposal_run_id: str
    created_at: datetime
    invocation_id: str
    proposal_contract_version: str = CANDIDATE_FACT_PROPOSAL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "proposal_id", "subject_id", "source_id", "projection_id",
            "proposal_run_id", "invocation_id",
        ):
            _clean_id(name, getattr(self, name))
        object.__setattr__(self, "field_key", ApplicationExecutionIdentityFieldKey(self.field_key))
        object.__setattr__(
            self, "source_kind", CandidateInformationSourceKind(self.source_kind)
        )
        object.__setattr__(self, "confidence", CandidateFactProposalConfidence(self.confidence))
        for name in ("source_hash", "projection_hash", "evidence_excerpt_hash", "proposal_hash"):
            _clean_hash(name, getattr(self, name))
        if self.proposal_contract_version != CANDIDATE_FACT_PROPOSAL_CONTRACT_VERSION:
            raise ValueError("proposal contract version is unsupported")
        if not self.evidence_refs:
            raise ValueError("proposal evidence is empty")
        object.__setattr__(self, "created_at", _utc(self.created_at))
        expected = _hash(self.binding_dict())
        if self.proposal_hash != expected or self.proposal_id != f"candidate-fact-proposal-{expected[:32]}":
            raise ValueError("proposal identity is invalid")

    def binding_dict(self) -> dict[str, Any]:
        return {
            "agent_component_id": self.agent_component_id,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "field_key": self.field_key.value,
            "normalization_policy_version": self.normalization_policy_version,
            "normalized_value_hash": _sha256(self.proposed_normalized_value.encode()),
            "projection_hash": self.projection_hash,
            "projection_id": self.projection_id,
            "prompt_policy_version": self.prompt_policy_version,
            "proposal_contract_version": self.proposal_contract_version,
            "schema_version": self.schema_version,
            "source_hash": self.source_hash,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "source_version": self.source_version,
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.binding_dict(),
            "backend_id": self.backend_id,
            "confidence": self.confidence.value,
            "created_at": _time(self.created_at),
            "evidence_excerpt_hash": self.evidence_excerpt_hash,
            "invocation_id": self.invocation_id,
            "model_id": self.model_id,
            "proposal_hash": self.proposal_hash,
            "proposal_id": self.proposal_id,
            "proposal_run_id": self.proposal_run_id,
            "proposed_normalized_value": self.proposed_normalized_value,
            "proposed_raw_value": self.proposed_raw_value,
            "value_type": self.value_type,
        }

    def to_proposed_fact_source_ref(self) -> CandidateIdentityFactSourceRef:
        source_kind = {
            CandidateInformationSourceKind.FILE: (
                CandidateIdentityFactSourceKind.DOCUMENT_EXTRACTION
            ),
            CandidateInformationSourceKind.URL: (
                CandidateIdentityFactSourceKind.URL_EXTRACTION
            ),
            CandidateInformationSourceKind.USER_STATEMENT: (
                CandidateIdentityFactSourceKind.USER_STATEMENT
            ),
        }[self.source_kind]
        return CandidateIdentityFactSourceRef(
            source_kind=source_kind,
            source_id=self.source_id,
            source_version=self.source_version,
            source_hash=self.source_hash,
            source_locator=f"projection:{self.projection_id}",
            source_subject_id=self.subject_id,
        )


@dataclass(frozen=True, slots=True)
class CandidateFactProposalRun:
    proposal_run_id: str
    subject_id: str
    source_id: str
    projection_id: str
    input_snapshot_id: str
    input_snapshot_hash: str
    agent_binding_hash: str
    produced_proposal_ids: tuple[str, ...]
    rejected_output_items: tuple[str, ...]
    result_status: CandidateFactProposalRunStatus
    run_binding_hash: str
    created_at: datetime
    invocation_id: str
    run_contract_version: str = CANDIDATE_FACT_PROPOSAL_RUN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "proposal_run_id", "subject_id", "source_id", "projection_id",
            "input_snapshot_id", "invocation_id",
        ):
            _clean_id(name, getattr(self, name))
        for name in ("input_snapshot_hash", "agent_binding_hash", "run_binding_hash"):
            _clean_hash(name, getattr(self, name))
        object.__setattr__(self, "result_status", CandidateFactProposalRunStatus(self.result_status))
        object.__setattr__(self, "created_at", _utc(self.created_at))
        if self.run_contract_version != CANDIDATE_FACT_PROPOSAL_RUN_CONTRACT_VERSION:
            raise ValueError("proposal run version is unsupported")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_binding_hash": self.agent_binding_hash,
            "created_at": _time(self.created_at),
            "input_snapshot_hash": self.input_snapshot_hash,
            "input_snapshot_id": self.input_snapshot_id,
            "invocation_id": self.invocation_id,
            "produced_proposal_ids": list(self.produced_proposal_ids),
            "projection_id": self.projection_id,
            "proposal_run_id": self.proposal_run_id,
            "rejected_output_items": list(self.rejected_output_items),
            "result_status": self.result_status.value,
            "run_binding_hash": self.run_binding_hash,
            "run_contract_version": self.run_contract_version,
            "source_id": self.source_id,
            "subject_id": self.subject_id,
        }


@dataclass(frozen=True, slots=True)
class ProposeCandidateFactsCommand:
    subject_id: str
    source_id: str
    source_version: str
    source_hash: str
    projection_id: str
    projection_hash: str
    invocation_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class ProposeCandidateFactsResult:
    status: CandidateFactProposalRunStatus
    run: CandidateFactProposalRun | None = None
    proposals: tuple[CandidateFactProposal, ...] = ()
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateFactProposalReadResult:
    status: CandidateFactProposalReadStatus
    proposal: CandidateFactProposal | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateFactProposalSummary:
    proposal_id: str
    proposal_hash: str
    field_key: ApplicationExecutionIdentityFieldKey
    source_id: str
    projection_id: str
    confidence: CandidateFactProposalConfidence
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CandidateFactProposalListResult:
    status: CandidateFactProposalListStatus
    proposals: tuple[CandidateFactProposalSummary, ...]
    next_cursor: str | None = None
    failure_code: str | None = None


def _locator_from_dict(value: Mapping[str, Any]) -> CandidateSourceLocator:
    from .candidate_source_projections import CandidateSourceLocatorContainerKind

    data = dict(value)
    data["container_kind"] = CandidateSourceLocatorContainerKind(data["container_kind"])
    return CandidateSourceLocator(**data)


def _evidence_from_dict(value: Mapping[str, Any]) -> CandidateFactProposalEvidenceRef:
    data = dict(value)
    data["source_locator"] = _locator_from_dict(data["source_locator"])
    return CandidateFactProposalEvidenceRef(**data)


def _proposal_from_dict(value: Mapping[str, Any]) -> CandidateFactProposal:
    data = dict(value)
    data.pop("normalized_value_hash", None)
    data["field_key"] = ApplicationExecutionIdentityFieldKey(data["field_key"])
    data["source_kind"] = CandidateInformationSourceKind(data["source_kind"])
    data["confidence"] = CandidateFactProposalConfidence(data["confidence"])
    data["evidence_refs"] = tuple(_evidence_from_dict(item) for item in data["evidence_refs"])
    data["created_at"] = _parse_time(data["created_at"])
    return CandidateFactProposal(**data)


def _run_from_dict(value: Mapping[str, Any]) -> CandidateFactProposalRun:
    data = dict(value)
    data["produced_proposal_ids"] = tuple(data["produced_proposal_ids"])
    data["rejected_output_items"] = tuple(data["rejected_output_items"])
    data["result_status"] = CandidateFactProposalRunStatus(data["result_status"])
    data["created_at"] = _parse_time(data["created_at"])
    return CandidateFactProposalRun(**data)


@runtime_checkable
class CandidateFactProposalRepository(Protocol):
    def find_run(self, subject_id: str, invocation_id: str, request_hash: str, run_binding_hash: str | None = None) -> ProposeCandidateFactsResult | None: ...
    def save(self, *, snapshot: CandidateFactProposalInputSnapshot, run: CandidateFactProposalRun, proposals: Sequence[CandidateFactProposal], request_hash: str) -> ProposeCandidateFactsResult: ...
    def get(self, subject_id: str, proposal_id: str) -> CandidateFactProposalReadResult: ...
    def list_for_subject(self, subject_id: str, *, field_key: ApplicationExecutionIdentityFieldKey | None = None, source_id: str | None = None, projection_id: str | None = None, limit: int = 100, cursor: str | None = None) -> CandidateFactProposalListResult: ...


class PrivateHomeCandidateFactProposalRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    @property
    def path(self) -> Path:
        return self._home.paths.candidate_fact_proposals

    def _connect(self) -> sqlite3.Connection:
        self._home.ensure()
        self._home.ensure_private_file(self.path)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots(
          input_snapshot_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,
          input_snapshot_hash TEXT NOT NULL,record_hash TEXT NOT NULL,record_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(
          proposal_run_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,
          run_binding_hash TEXT NOT NULL,created_at TEXT NOT NULL,
          record_hash TEXT NOT NULL,record_json TEXT NOT NULL,
          UNIQUE(subject_id,run_binding_hash));
        CREATE TABLE IF NOT EXISTS proposals(
          proposal_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,field_key TEXT NOT NULL,
          source_id TEXT NOT NULL,projection_id TEXT NOT NULL,proposal_hash TEXT NOT NULL,
          created_at TEXT NOT NULL,record_hash TEXT NOT NULL,record_json TEXT NOT NULL,
          UNIQUE(subject_id,proposal_hash));
        CREATE TABLE IF NOT EXISTS invocations(
          invocation_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,request_hash TEXT NOT NULL,
          proposal_run_id TEXT NOT NULL,FOREIGN KEY(proposal_run_id) REFERENCES runs(proposal_run_id));
        """)
        expected = str(CANDIDATE_FACT_PROPOSAL_REPOSITORY_SCHEMA_VERSION)
        connection.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version',?)", (expected,))
        connection.commit()
        row = connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None or row["value"] != expected:
            connection.close()
            raise _ProposalIntegrityError("proposal schema is unsupported")
        os.chmod(self.path, PRIVATE_FILE_MODE)
        return connection

    @staticmethod
    def _record(row: sqlite3.Row, parser):
        raw = row["record_json"]
        if row["record_hash"] != _sha256(raw.encode()):
            raise _ProposalIntegrityError("proposal record drift")
        return parser(json.loads(raw))

    @staticmethod
    def _result(connection: sqlite3.Connection, run: CandidateFactProposalRun, status: CandidateFactProposalRunStatus) -> ProposeCandidateFactsResult:
        items = []
        for proposal_id in run.produced_proposal_ids:
            row = connection.execute("SELECT * FROM proposals WHERE proposal_id=? AND subject_id=?", (proposal_id, run.subject_id)).fetchone()
            if row is None:
                raise _ProposalIntegrityError("proposal run child missing")
            items.append(PrivateHomeCandidateFactProposalRepository._record(row, _proposal_from_dict))
        return ProposeCandidateFactsResult(status, run, tuple(items))

    def find_run(self, subject_id: str, invocation_id: str, request_hash: str, run_binding_hash: str | None = None) -> ProposeCandidateFactsResult | None:
        try:
            with self._connect() as connection:
                invocation = connection.execute("SELECT * FROM invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
                if invocation is not None:
                    if invocation["subject_id"] != subject_id or invocation["request_hash"] != request_hash:
                        return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.INTEGRITY_FAILURE, failure_code="PROPOSAL_INVOCATION_CONFLICT")
                    row = connection.execute("SELECT * FROM runs WHERE proposal_run_id=?", (invocation["proposal_run_id"],)).fetchone()
                    if row is None:
                        raise _ProposalIntegrityError("proposal invocation target missing")
                    return self._result(connection, self._record(row, _run_from_dict), CandidateFactProposalRunStatus.UNCHANGED)
                if run_binding_hash is not None:
                    row = connection.execute("SELECT * FROM runs WHERE subject_id=? AND run_binding_hash=?", (subject_id, run_binding_hash)).fetchone()
                    if row is not None:
                        run = self._record(row, _run_from_dict)
                        connection.execute(
                            "INSERT INTO invocations VALUES(?,?,?,?)",
                            (invocation_id, subject_id, request_hash, run.proposal_run_id),
                        )
                        connection.commit()
                        return self._result(connection, run, CandidateFactProposalRunStatus.UNCHANGED)
                return None
        except (sqlite3.Error, ValueError, json.JSONDecodeError, _ProposalIntegrityError):
            return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.INTEGRITY_FAILURE, failure_code="PROPOSAL_REPOSITORY_INTEGRITY")

    def save(self, *, snapshot: CandidateFactProposalInputSnapshot, run: CandidateFactProposalRun, proposals: Sequence[CandidateFactProposal], request_hash: str) -> ProposeCandidateFactsResult:
        try:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                prior_invocation = connection.execute(
                    "SELECT * FROM invocations WHERE invocation_id=?",
                    (run.invocation_id,),
                ).fetchone()
                if prior_invocation is not None:
                    connection.rollback()
                    return self.find_run(
                        run.subject_id, run.invocation_id, request_hash
                    ) or ProposeCandidateFactsResult(
                        CandidateFactProposalRunStatus.INTEGRITY_FAILURE,
                        failure_code="PROPOSAL_INVOCATION_CONFLICT",
                    )
                existing = connection.execute("SELECT * FROM runs WHERE subject_id=? AND run_binding_hash=?", (run.subject_id, run.run_binding_hash)).fetchone()
                if existing is not None:
                    existing_run = self._record(existing, _run_from_dict)
                    connection.execute("INSERT OR IGNORE INTO invocations VALUES(?,?,?,?)", (run.invocation_id, run.subject_id, request_hash, existing_run.proposal_run_id))
                    connection.commit()
                    return self._result(connection, existing_run, CandidateFactProposalRunStatus.UNCHANGED)
                snapshot_json = json.dumps(snapshot.binding_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                connection.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)", (snapshot.input_snapshot_id, snapshot.subject_id, snapshot.input_snapshot_hash, _sha256(snapshot_json.encode()), snapshot_json))
                for proposal in proposals:
                    raw = json.dumps(proposal.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                    connection.execute("INSERT OR IGNORE INTO proposals VALUES(?,?,?,?,?,?,?,?,?)", (proposal.proposal_id, proposal.subject_id, proposal.field_key.value, proposal.source_id, proposal.projection_id, proposal.proposal_hash, _time(proposal.created_at), _sha256(raw.encode()), raw))
                run_raw = json.dumps(run.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)", (run.proposal_run_id, run.subject_id, run.run_binding_hash, _time(run.created_at), _sha256(run_raw.encode()), run_raw))
                connection.execute("INSERT INTO invocations VALUES(?,?,?,?)", (run.invocation_id, run.subject_id, request_hash, run.proposal_run_id))
                connection.commit()
                return ProposeCandidateFactsResult(run.result_status, run, tuple(proposals))
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except (sqlite3.Error, ValueError, TypeError, _ProposalIntegrityError):
            return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.INTEGRITY_FAILURE, failure_code="PROPOSAL_REPOSITORY_INTEGRITY")

    def get(self, subject_id: str, proposal_id: str) -> CandidateFactProposalReadResult:
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM proposals WHERE proposal_id=? AND subject_id=?", (proposal_id, subject_id)).fetchone()
                if row is None:
                    return CandidateFactProposalReadResult(CandidateFactProposalReadStatus.NOT_FOUND)
                return CandidateFactProposalReadResult(CandidateFactProposalReadStatus.FOUND, self._record(row, _proposal_from_dict))
        except (sqlite3.Error, ValueError, json.JSONDecodeError, _ProposalIntegrityError):
            return CandidateFactProposalReadResult(CandidateFactProposalReadStatus.INTEGRITY_FAILURE, failure_code="PROPOSAL_REPOSITORY_INTEGRITY")

    def list_for_subject(self, subject_id: str, *, field_key: ApplicationExecutionIdentityFieldKey | None = None, source_id: str | None = None, projection_id: str | None = None, limit: int = 100, cursor: str | None = None) -> CandidateFactProposalListResult:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("proposal list limit is invalid")
        clauses = ["subject_id=?"]
        values: list[Any] = [subject_id]
        for column, value in (("field_key", field_key.value if field_key else None), ("source_id", source_id), ("projection_id", projection_id)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        if cursor is not None:
            clauses.append("proposal_id>?")
            values.append(cursor)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM proposals WHERE " + " AND ".join(clauses)
                    + " ORDER BY field_key,source_id,projection_id,proposal_id LIMIT ?",
                    (*values, limit + 1),
                ).fetchall()
                full_items = tuple(
                    self._record(row, _proposal_from_dict)
                    for row in rows[:limit]
                )
                items = tuple(
                    CandidateFactProposalSummary(
                        item.proposal_id,
                        item.proposal_hash,
                        item.field_key,
                        item.source_id,
                        item.projection_id,
                        item.confidence,
                        item.created_at,
                    )
                    for item in full_items
                )
                return CandidateFactProposalListResult(
                    CandidateFactProposalListStatus.SUCCEEDED,
                    items,
                    items[-1].proposal_id if len(rows) > limit and items else None,
                )
        except (sqlite3.Error, ValueError, json.JSONDecodeError, _ProposalIntegrityError):
            return CandidateFactProposalListResult(CandidateFactProposalListStatus.INTEGRITY_FAILURE, (), failure_code="PROPOSAL_REPOSITORY_INTEGRITY")


def _request_hash(command: ProposeCandidateFactsCommand) -> str:
    return _hash({
        "invocation_id": command.invocation_id,
        "projection_hash": command.projection_hash,
        "projection_id": command.projection_id,
        "source_hash": command.source_hash,
        "source_id": command.source_id,
        "source_version": command.source_version,
        "subject_id": command.subject_id,
    })


def _select_input(
    command: ProposeCandidateFactsCommand,
    projection: CandidateSourceProjection,
    repository: CandidateSourceProjectionRepository,
    policy: CandidateFactProposalSelectionPolicy,
) -> CandidateFactProposalInputSnapshot:
    priorities = {
        CandidateProjectionBlockType.TITLE: 0,
        CandidateProjectionBlockType.HEADING: 0,
        CandidateProjectionBlockType.METADATA: 1,
        CandidateProjectionBlockType.LINK: 1,
        CandidateProjectionBlockType.USER_STATEMENT: 2,
        CandidateProjectionBlockType.PARAGRAPH: 3,
        CandidateProjectionBlockType.LIST_ITEM: 3,
        CandidateProjectionBlockType.TABLE_CELL: 3,
        CandidateProjectionBlockType.SLIDE_TEXT: 3,
        CandidateProjectionBlockType.SPEAKER_NOTE: 4,
    }
    loaded_blocks: list[CandidateProjectionBlock] = []
    for block_id in projection.block_ids:
        result = read_candidate_projection_block(command.subject_id, projection.projection_id, block_id, repository=repository)
        if result.status is not CandidateSourceProjectionReadStatus.FOUND or result.block is None:
            raise _ProposalIntegrityError("projection block is unavailable")
        loaded_blocks.append(result.block)
    loaded_blocks.sort(key=lambda item: (priorities[item.block_type], item.ordinal, item.block_id))
    selected_blocks: list[CandidateFactProposalSelectedBlock] = []
    text_bytes = 0
    truncation: list[str] = []
    for block in loaded_blocks:
        size = len(block.text.encode())
        if len(selected_blocks) >= policy.max_blocks or text_bytes + size > policy.max_text_bytes:
            truncation.append("BLOCKS_TRUNCATED")
            break
        selected_blocks.append(CandidateFactProposalSelectedBlock(
            block.block_id, block.block_hash, block.block_type, block.ordinal,
            block.text, block.source_locator,
        ))
        text_bytes += size
    selected_assets: list[CandidateFactProposalSelectedAsset] = []
    use_assets = not selected_blocks or not policy.use_assets_only_when_no_text_blocks
    if use_assets:
        total = 0
        for asset_id in projection.asset_ids:
            result = read_candidate_projection_asset(command.subject_id, projection.projection_id, asset_id, repository=repository)
            if result.status is not CandidateSourceProjectionReadStatus.FOUND or result.asset_payload is None:
                raise _ProposalIntegrityError("projection asset is unavailable")
            payload = result.asset_payload
            asset = payload.asset
            if len(selected_assets) >= policy.max_assets or asset.byte_size > policy.max_asset_bytes_each or total + asset.byte_size > policy.max_total_asset_bytes:
                truncation.append("ASSETS_TRUNCATED")
                break
            selected_assets.append(CandidateFactProposalSelectedAsset(
                asset.asset_id, asset.content_hash, asset.asset_kind.value,
                asset.media_type, asset.ordinal, asset.byte_size, asset.width,
                asset.height, asset.source_locator, payload.content,
            ))
            total += asset.byte_size
    definitions = tuple(
        definition for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY.values()
        if definition.agent_proposal_allowed
        and (selected_blocks and definition.text_evidence_allowed or selected_assets and definition.image_evidence_allowed)
    )
    if not definitions or (not selected_blocks and not selected_assets):
        raise CandidateFactProposalInputUnsupportedError("projection has no supported Agent input")
    binding = {
        "agent_policy_version": CANDIDATE_FACT_PROPOSAL_AGENT_POLICY_VERSION,
        "allowed_field_keys": [definition.field_key.value for definition in definitions],
        "input_contract_version": CANDIDATE_FACT_PROPOSAL_INPUT_CONTRACT_VERSION,
        "projection_hash": projection.projection_hash,
        "projection_id": projection.projection_id,
        "selected_assets": [item.to_dict() for item in selected_assets],
        "selected_blocks": [{**item.to_dict(include_text=False), "text_hash": _sha256(item.text.encode())} for item in selected_blocks],
        "selection_policy_version": policy.policy_version,
        "source_hash": command.source_hash,
        "source_id": command.source_id,
        "source_kind": projection.source_kind.value,
        "source_version": command.source_version,
        "subject_id": command.subject_id,
        "truncation_codes": sorted(set(truncation)),
    }
    digest = _hash(binding)
    return CandidateFactProposalInputSnapshot(
        input_snapshot_id=f"candidate-proposal-input-{digest[:32]}",
        subject_id=command.subject_id,
        source_id=command.source_id,
        source_kind=projection.source_kind,
        source_version=command.source_version,
        source_hash=command.source_hash,
        projection_id=projection.projection_id,
        projection_hash=projection.projection_hash,
        selected_blocks=tuple(selected_blocks),
        selected_assets=tuple(selected_assets),
        allowed_field_keys=tuple(definition.field_key for definition in definitions),
        truncation_codes=tuple(binding["truncation_codes"]),
        input_snapshot_hash=digest,
    )


def _locator_equal(left: Mapping[str, Any], right: CandidateSourceLocator) -> bool:
    return dict(left) == right.to_dict()


def _validate_items(
    output: CandidateFactProposalAgentOutput,
    snapshot: CandidateFactProposalInputSnapshot,
    metadata: CandidateFactProposalAgentMetadata,
    run_id: str,
    command: ProposeCandidateFactsCommand,
) -> tuple[tuple[CandidateFactProposal, ...], tuple[str, ...]]:
    blocks = {item.block_id: item for item in snapshot.selected_blocks}
    assets = {item.asset_id: item for item in snapshot.selected_assets}
    allowed = set(snapshot.allowed_field_keys)
    combined: dict[tuple[ApplicationExecutionIdentityFieldKey, str], tuple[CandidateFactProposalAgentItem, list[CandidateFactProposalEvidenceRef]]] = {}
    rejected: list[str] = []
    for index, item in enumerate(output.proposals[:MAX_PROPOSALS_PER_RUN]):
        try:
            key = ApplicationExecutionIdentityFieldKey(item.field_key)
            if key not in allowed or not item.evidence_refs or len(item.evidence_refs) > MAX_EVIDENCE_REFS:
                raise ValueError
            normalized = normalize_application_execution_identity_value(key, item.proposed_value)
            refs: list[CandidateFactProposalEvidenceRef] = []
            text_evidence = []
            for raw in item.evidence_refs:
                if raw.block_id is not None:
                    if raw.asset_id is not None or raw.block_hash is None:
                        raise ValueError
                    selected = blocks.get(raw.block_id)
                    if selected is None or selected.block_hash != raw.block_hash or not _locator_equal(raw.source_locator, selected.source_locator):
                        raise ValueError
                    refs.append(CandidateFactProposalEvidenceRef("BLOCK", selected.block_id, selected.block_hash, selected.source_locator))
                    text_evidence.append(selected.text)
                elif raw.asset_id is not None:
                    if raw.asset_hash is None:
                        raise ValueError
                    selected_asset = assets.get(raw.asset_id)
                    if selected_asset is None or selected_asset.asset_hash != raw.asset_hash or not _locator_equal(raw.source_locator, selected_asset.source_locator):
                        raise ValueError
                    refs.append(CandidateFactProposalEvidenceRef("ASSET", selected_asset.asset_id, selected_asset.asset_hash, selected_asset.source_locator))
                else:
                    raise ValueError
            if item.evidence_excerpt:
                if len(item.evidence_excerpt) > MAX_EVIDENCE_EXCERPT_CHARS or not any(item.evidence_excerpt in value for value in text_evidence):
                    raise ValueError
            elif text_evidence:
                raise ValueError
            if len(item.extraction_note) > MAX_EXTRACTION_NOTE_CHARS:
                raise ValueError
            dedupe_key = (key, normalized)
            if dedupe_key in combined:
                previous, prior_refs = combined[dedupe_key]
                for ref in refs:
                    if ref not in prior_refs:
                        prior_refs.append(ref)
            else:
                combined[dedupe_key] = (item, refs)
        except (TypeError, ValueError):
            rejected.append(f"ITEM_{index}_INVALID")
    proposals: list[CandidateFactProposal] = []
    for (key, normalized), (item, refs) in combined.items():
        refs.sort(key=lambda ref: (ref.source_locator.block_index or 0, ref.evidence_kind, ref.evidence_id))
        binding = {
            "agent_component_id": metadata.component_id,
            "evidence_refs": [ref.to_dict() for ref in refs],
            "field_key": key.value,
            "normalization_policy_version": APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION,
            "normalized_value_hash": _sha256(normalized.encode()),
            "projection_hash": snapshot.projection_hash,
            "projection_id": snapshot.projection_id,
            "prompt_policy_version": metadata.prompt_policy_version,
            "proposal_contract_version": CANDIDATE_FACT_PROPOSAL_CONTRACT_VERSION,
            "schema_version": metadata.schema_version,
            "source_hash": snapshot.source_hash,
            "source_id": snapshot.source_id,
            "source_kind": snapshot.source_kind.value,
            "source_version": snapshot.source_version,
            "subject_id": snapshot.subject_id,
        }
        digest = _hash(binding)
        proposals.append(CandidateFactProposal(
            proposal_id=f"candidate-fact-proposal-{digest[:32]}",
            subject_id=snapshot.subject_id,
            field_key=key,
            proposed_raw_value=item.proposed_value,
            proposed_normalized_value=normalized,
            value_type=APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY[key].value_type.value,
            normalization_policy_version=APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION,
            source_id=snapshot.source_id,
            source_kind=snapshot.source_kind,
            source_version=snapshot.source_version,
            source_hash=snapshot.source_hash,
            projection_id=snapshot.projection_id,
            projection_hash=snapshot.projection_hash,
            evidence_refs=tuple(refs),
            evidence_excerpt_hash=_sha256(item.evidence_excerpt.encode()),
            agent_component_id=metadata.component_id,
            backend_id=metadata.backend_id,
            model_id=metadata.model_id,
            prompt_policy_version=metadata.prompt_policy_version,
            schema_version=metadata.schema_version,
            confidence=item.confidence,
            proposal_hash=digest,
            proposal_run_id=run_id,
            created_at=command.now,
            invocation_id=command.invocation_id,
        ))
    proposals.sort(key=lambda item: (list(ApplicationExecutionIdentityFieldKey).index(item.field_key), item.proposal_id))
    return tuple(proposals), tuple(rejected)


async def propose_candidate_facts(
    command: ProposeCandidateFactsCommand,
    *,
    projection_repository: CandidateSourceProjectionRepository,
    agent: CandidateFactProposalAgentPort,
    agent_metadata: CandidateFactProposalAgentMetadata,
    repository: CandidateFactProposalRepository,
    selection_policy: CandidateFactProposalSelectionPolicy | None = None,
) -> ProposeCandidateFactsResult:
    try:
        request_hash = _request_hash(command)
        replay = repository.find_run(command.subject_id, command.invocation_id, request_hash)
        if replay is not None:
            return replay
        projection_result = get_candidate_source_projection(command.subject_id, command.projection_id, repository=projection_repository)
        if projection_result.status is not CandidateSourceProjectionReadStatus.FOUND or projection_result.projection is None:
            return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.INTEGRITY_FAILURE, failure_code="PROJECTION_UNAVAILABLE")
        projection = projection_result.projection
        if (
            projection.projection_hash != command.projection_hash
            or projection.source_id != command.source_id
            or projection.source_version != command.source_version
            or projection.source_identity_hash != command.source_hash
        ):
            return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.INTEGRITY_FAILURE, failure_code="PROJECTION_BINDING_MISMATCH")
        snapshot = _select_input(command, projection, projection_repository, selection_policy or CandidateFactProposalSelectionPolicy())
        agent_binding_hash = _hash(agent_metadata.binding_dict())
        run_binding_hash = _hash({
            "agent_binding_hash": agent_binding_hash,
            "input_snapshot_hash": snapshot.input_snapshot_hash,
            "run_contract_version": CANDIDATE_FACT_PROPOSAL_RUN_CONTRACT_VERSION,
        })
        existing = repository.find_run(command.subject_id, command.invocation_id, request_hash, run_binding_hash)
        if existing is not None:
            return existing
        run_id = f"candidate-fact-proposal-run-{run_binding_hash[:32]}"
        try:
            output = await agent.propose(CandidateFactProposalAgentContext(
                snapshot,
                tuple(APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY[key] for key in snapshot.allowed_field_keys),
                CANDIDATE_FACT_PROPOSAL_OUTPUT_SCHEMA,
            ))
            if not isinstance(output, CandidateFactProposalAgentOutput):
                raise CandidateFactProposalAgentOutputError(
                    "Agent output contract is invalid"
                )
        except CandidateFactProposalAgentOutputError:
            run = CandidateFactProposalRun(
                proposal_run_id=run_id,
                subject_id=command.subject_id,
                source_id=command.source_id,
                projection_id=command.projection_id,
                input_snapshot_id=snapshot.input_snapshot_id,
                input_snapshot_hash=snapshot.input_snapshot_hash,
                agent_binding_hash=agent_binding_hash,
                produced_proposal_ids=(),
                rejected_output_items=("AGENT_OUTPUT_SCHEMA_INVALID",),
                result_status=CandidateFactProposalRunStatus.FAILED_AGENT_OUTPUT,
                run_binding_hash=run_binding_hash,
                created_at=command.now,
                invocation_id=command.invocation_id,
            )
            return repository.save(
                snapshot=snapshot, run=run, proposals=(),
                request_hash=request_hash,
            )
        proposals, rejected = _validate_items(output, snapshot, agent_metadata, run_id, command)
        if proposals:
            status = CandidateFactProposalRunStatus.PARTIAL_VALIDATION if rejected else CandidateFactProposalRunStatus.CREATED
        else:
            status = CandidateFactProposalRunStatus.FAILED_AGENT_OUTPUT if rejected else CandidateFactProposalRunStatus.NO_PROPOSALS
        run = CandidateFactProposalRun(
            proposal_run_id=run_id,
            subject_id=command.subject_id,
            source_id=command.source_id,
            projection_id=command.projection_id,
            input_snapshot_id=snapshot.input_snapshot_id,
            input_snapshot_hash=snapshot.input_snapshot_hash,
            agent_binding_hash=agent_binding_hash,
            produced_proposal_ids=tuple(item.proposal_id for item in proposals),
            rejected_output_items=rejected,
            result_status=status,
            run_binding_hash=run_binding_hash,
            created_at=command.now,
            invocation_id=command.invocation_id,
        )
        return repository.save(snapshot=snapshot, run=run, proposals=proposals, request_hash=request_hash)
    except CandidateFactProposalInputUnsupportedError:
        return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.DEFERRED_INPUT_UNSUPPORTED, failure_code="PROPOSAL_INPUT_UNSUPPORTED")
    except CandidateFactProposalAgentUnavailableError:
        return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.DEFERRED_BACKEND_UNAVAILABLE, failure_code="PROPOSAL_BACKEND_UNAVAILABLE")
    except (_ProposalIntegrityError, TypeError, ValueError):
        return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.INTEGRITY_FAILURE, failure_code="PROPOSAL_INTEGRITY")
    except Exception:
        return ProposeCandidateFactsResult(CandidateFactProposalRunStatus.FAILED, failure_code="PROPOSAL_FAILED")


def get_candidate_fact_proposal(subject_id: str, proposal_id: str, *, repository: CandidateFactProposalRepository) -> CandidateFactProposalReadResult:
    return repository.get(subject_id, proposal_id)


def list_candidate_fact_proposals(subject_id: str, *, repository: CandidateFactProposalRepository, field_key: ApplicationExecutionIdentityFieldKey | None = None, source_id: str | None = None, projection_id: str | None = None, limit: int = 100, cursor: str | None = None) -> CandidateFactProposalListResult:
    return repository.list_for_subject(subject_id, field_key=field_key, source_id=source_id, projection_id=projection_id, limit=limit, cursor=cursor)


def list_current_candidate_fact_proposals(
    subject_id: str,
    *,
    repository: CandidateFactProposalRepository,
    field_key: ApplicationExecutionIdentityFieldKey | None = None,
    source_id: str | None = None,
    projection_id: str | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> CandidateFactProposalListResult:
    """List immutable proposal heads before C1d review-decision filtering exists."""

    return list_candidate_fact_proposals(
        subject_id,
        repository=repository,
        field_key=field_key,
        source_id=source_id,
        projection_id=projection_id,
        limit=limit,
        cursor=cursor,
    )


_LOCATOR_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "block_index", "character_end", "character_start", "column_index",
        "container_kind", "element_path", "locator_contract_version",
        "page_number", "paragraph_index", "row_index", "slide_number",
        "source_id", "source_version", "table_index",
    ],
    "properties": {
        "block_index": {"type": ["integer", "null"], "minimum": 0},
        "character_end": {"type": ["integer", "null"], "minimum": 0},
        "character_start": {"type": ["integer", "null"], "minimum": 0},
        "column_index": {"type": ["integer", "null"], "minimum": 0},
        "container_kind": {"type": "string"},
        "element_path": {"type": ["string", "null"]},
        "locator_contract_version": {
            "type": "string",
            "const": CANDIDATE_SOURCE_LOCATOR_CONTRACT_VERSION,
        },
        "page_number": {"type": ["integer", "null"], "minimum": 1},
        "paragraph_index": {"type": ["integer", "null"], "minimum": 0},
        "row_index": {"type": ["integer", "null"], "minimum": 0},
        "slide_number": {"type": ["integer", "null"], "minimum": 1},
        "source_id": {"type": "string"},
        "source_version": {"type": "string"},
        "table_index": {"type": ["integer", "null"], "minimum": 0},
    },
}


CANDIDATE_FACT_PROPOSAL_OUTPUT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["proposals"],
    "properties": {
        "proposals": {
            "type": "array",
            "maxItems": MAX_PROPOSALS_PER_RUN,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field_key", "proposed_value", "evidence_refs", "evidence_excerpt", "confidence", "extraction_note"],
                "properties": {
                    "field_key": {"type": "string", "enum": [key.value for key in ApplicationExecutionIdentityFieldKey]},
                    "proposed_value": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "evidence_refs": {
                        "type": "array", "minItems": 1, "maxItems": MAX_EVIDENCE_REFS,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["block_id", "block_hash", "asset_id", "asset_hash", "source_locator"],
                            "properties": {
                                "block_id": {"type": ["string", "null"]},
                                "block_hash": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
                                "asset_id": {"type": ["string", "null"]},
                                "asset_hash": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
                                "source_locator": _LOCATOR_SCHEMA,
                            },
                        },
                    },
                    "evidence_excerpt": {"type": "string", "maxLength": MAX_EVIDENCE_EXCERPT_CHARS},
                    "confidence": {"type": "string", "enum": [item.value for item in CandidateFactProposalConfidence]},
                    "extraction_note": {"type": "string", "maxLength": MAX_EXTRACTION_NOTE_CHARS},
                },
            },
        }
    },
}


__all__ = [
    "CANDIDATE_FACT_PROPOSAL_AGENT_POLICY_VERSION",
    "CANDIDATE_FACT_PROPOSAL_AGENT_SCHEMA_VERSION",
    "CANDIDATE_FACT_PROPOSAL_COMPONENT_ID",
    "CANDIDATE_FACT_PROPOSAL_OUTPUT_SCHEMA",
    "CandidateFactProposal",
    "CandidateFactProposalAgentContext",
    "CandidateFactProposalAgentEvidenceRef",
    "CandidateFactProposalAgentItem",
    "CandidateFactProposalAgentMetadata",
    "CandidateFactProposalAgentOutput",
    "CandidateFactProposalAgentOutputError",
    "CandidateFactProposalAgentPort",
    "CandidateFactProposalAgentUnavailableError",
    "CandidateFactProposalConfidence",
    "CandidateFactProposalInputSnapshot",
    "CandidateFactProposalListResult",
    "CandidateFactProposalReadResult",
    "CandidateFactProposalReadStatus",
    "CandidateFactProposalRun",
    "CandidateFactProposalRunStatus",
    "CandidateFactProposalSelectionPolicy",
    "CandidateFactProposalSummary",
    "PrivateHomeCandidateFactProposalRepository",
    "ProposeCandidateFactsCommand",
    "ProposeCandidateFactsResult",
    "get_candidate_fact_proposal",
    "list_candidate_fact_proposals",
    "list_current_candidate_fact_proposals",
    "propose_candidate_facts",
]
