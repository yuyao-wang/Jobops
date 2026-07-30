"""Immutable, source-bound candidate identity facts with atomic current lineage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION,
    APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION,
    ApplicationExecutionIdentityFieldKey,
    normalize_application_execution_identity_value,
)
from .private_home import PRIVATE_FILE_MODE, PrivateHome, PrivateHomeError


CANDIDATE_IDENTITY_FACT_CONTRACT_VERSION = "candidate-identity-fact-v1"
CANDIDATE_IDENTITY_FACT_SOURCE_CONTRACT_VERSION = (
    "candidate-identity-fact-source-v1"
)
CANDIDATE_IDENTITY_FACT_INDEX_CONTRACT_VERSION = (
    "candidate-identity-fact-index-v1"
)
CANDIDATE_IDENTITY_FACT_REPOSITORY_SCHEMA_VERSION = 1

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_WINDOWS_ABSOLUTE_RE = re.compile(r"[A-Za-z]:[\\/]")


class CandidateIdentityFactSourceKind(StrEnum):
    USER_STATEMENT = "USER_STATEMENT"
    USER_CONFIRMATION = "USER_CONFIRMATION"
    DOCUMENT_EXTRACTION = "DOCUMENT_EXTRACTION"
    URL_EXTRACTION = "URL_EXTRACTION"
    TRUSTED_CONNECTOR = "TRUSTED_CONNECTOR"
    LEGACY_NORMALIZED_PROFILE = "LEGACY_NORMALIZED_PROFILE"
    SYSTEM_MIGRATION = "SYSTEM_MIGRATION"


class CandidateIdentityFactVerificationStatus(StrEnum):
    PROPOSED = "PROPOSED"
    USER_CONFIRMED = "USER_CONFIRMED"
    TRUSTED_SOURCE_VERIFIED = "TRUSTED_SOURCE_VERIFIED"
    LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"

    @property
    def eligible_for_current(self) -> bool:
        return self in {
            CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
            CandidateIdentityFactVerificationStatus.TRUSTED_SOURCE_VERIFIED,
        }


class WriteCandidateIdentityFactStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    STALE_CURRENT = "STALE_CURRENT"
    CONFLICT = "CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    INVALID = "INVALID"
    FAILED = "FAILED"


class GetCurrentCandidateIdentityFactStatus(StrEnum):
    FOUND = "FOUND"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class CandidateIdentityFactConflictState(StrEnum):
    NONE = "NONE"
    CONFLICT = "CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


def _clean_id(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if cleaned != value or _ID_RE.fullmatch(cleaned) is None:
        raise ValueError(f"{name} is invalid")
    return cleaned


def _clean_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _clean_version(name: str, value: Any) -> str:
    return _clean_id(name, value)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _format_time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("created_at is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("created_at is invalid")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CandidateIdentityFactSourceRef:
    source_kind: CandidateIdentityFactSourceKind
    source_id: str
    source_version: str
    source_hash: str
    source_locator: str
    source_subject_id: str
    source_contract_version: str = CANDIDATE_IDENTITY_FACT_SOURCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_kind", CandidateIdentityFactSourceKind(self.source_kind)
        )
        object.__setattr__(self, "source_id", _clean_id("source_id", self.source_id))
        object.__setattr__(
            self,
            "source_version",
            _clean_version("source_version", self.source_version),
        )
        object.__setattr__(
            self, "source_hash", _clean_hash("source_hash", self.source_hash)
        )
        object.__setattr__(
            self,
            "source_subject_id",
            _clean_id("source_subject_id", self.source_subject_id),
        )
        if self.source_contract_version != CANDIDATE_IDENTITY_FACT_SOURCE_CONTRACT_VERSION:
            raise ValueError("source contract version is unsupported")
        if not isinstance(self.source_locator, str):
            raise TypeError("source_locator must be a string")
        locator = self.source_locator.strip()
        if locator != self.source_locator or len(locator) > 512:
            raise ValueError("source_locator is invalid")
        if (
            locator.startswith(("/", "~", "file:"))
            or _WINDOWS_ABSOLUTE_RE.match(locator)
            or (locator and Path(locator).is_absolute())
            or "://" in locator
            or any(ord(char) < 32 for char in locator)
        ):
            raise ValueError("source_locator must be a bounded structural locator")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_contract_version": self.source_contract_version,
            "source_hash": self.source_hash,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "source_locator": self.source_locator,
            "source_subject_id": self.source_subject_id,
            "source_version": self.source_version,
        }


def _source_from_dict(value: Mapping[str, Any]) -> CandidateIdentityFactSourceRef:
    expected = {
        "source_contract_version",
        "source_hash",
        "source_id",
        "source_kind",
        "source_locator",
        "source_subject_id",
        "source_version",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted source fields are invalid")
    return CandidateIdentityFactSourceRef(**dict(value))


def _validate_source_eligibility(
    status: CandidateIdentityFactVerificationStatus,
    source: CandidateIdentityFactSourceRef,
) -> None:
    allowed = {
        CandidateIdentityFactVerificationStatus.USER_CONFIRMED: {
            CandidateIdentityFactSourceKind.USER_STATEMENT,
            CandidateIdentityFactSourceKind.USER_CONFIRMATION,
        },
        CandidateIdentityFactVerificationStatus.TRUSTED_SOURCE_VERIFIED: {
            CandidateIdentityFactSourceKind.TRUSTED_CONNECTOR,
        },
        CandidateIdentityFactVerificationStatus.LEGACY_UNVERIFIED: {
            CandidateIdentityFactSourceKind.LEGACY_NORMALIZED_PROFILE,
        },
        CandidateIdentityFactVerificationStatus.PROPOSED: {
            CandidateIdentityFactSourceKind.DOCUMENT_EXTRACTION,
            CandidateIdentityFactSourceKind.URL_EXTRACTION,
            CandidateIdentityFactSourceKind.SYSTEM_MIGRATION,
            CandidateIdentityFactSourceKind.TRUSTED_CONNECTOR,
        },
    }[status]
    if source.source_kind not in allowed:
        raise ValueError("verification status and source kind are incompatible")


@dataclass(frozen=True, slots=True)
class CandidateIdentityFact:
    fact_id: str
    subject_id: str
    field_key: ApplicationExecutionIdentityFieldKey
    normalized_value: str = field(repr=False)
    verification_status: CandidateIdentityFactVerificationStatus
    source_ref: CandidateIdentityFactSourceRef
    parent_fact_id: str | None
    field_version: int
    content_hash: str
    created_at: datetime
    invocation_id: str
    supersedes_fact_id: str | None
    value_type: str = "string"
    field_schema_version: str = APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION
    normalization_policy_version: str = (
        APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION
    )
    fact_contract_version: str = CANDIDATE_IDENTITY_FACT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_id", _clean_id("fact_id", self.fact_id))
        object.__setattr__(
            self, "subject_id", _clean_id("subject_id", self.subject_id)
        )
        object.__setattr__(
            self, "field_key", ApplicationExecutionIdentityFieldKey(self.field_key)
        )
        object.__setattr__(
            self,
            "verification_status",
            CandidateIdentityFactVerificationStatus(self.verification_status),
        )
        if not isinstance(self.normalized_value, str) or not self.normalized_value:
            raise ValueError("normalized_value is invalid")
        if not isinstance(self.source_ref, CandidateIdentityFactSourceRef):
            raise TypeError("source_ref is invalid")
        if self.source_ref.source_subject_id != self.subject_id:
            raise ValueError("source subject binding is invalid")
        _validate_source_eligibility(self.verification_status, self.source_ref)
        for name in ("parent_fact_id", "supersedes_fact_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _clean_id(name, value))
        if type(self.field_version) is not int or self.field_version <= 0:
            raise ValueError("field_version is invalid")
        object.__setattr__(
            self, "content_hash", _clean_hash("content_hash", self.content_hash)
        )
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(
            self, "created_at", self.created_at.astimezone(timezone.utc)
        )
        object.__setattr__(
            self, "invocation_id", _clean_id("invocation_id", self.invocation_id)
        )
        if self.value_type != "string":
            raise ValueError("value_type is unsupported")
        if self.field_schema_version != APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION:
            raise ValueError("field schema version is unsupported")
        if (
            self.normalization_policy_version
            != APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION
        ):
            raise ValueError("normalization policy version is unsupported")
        if self.fact_contract_version != CANDIDATE_IDENTITY_FACT_CONTRACT_VERSION:
            raise ValueError("fact contract version is unsupported")
        if self.content_hash != _hash(self.identity_dict()):
            raise ValueError("fact content hash is invalid")
        expected_id = f"candidate-identity-fact-{self.content_hash[:32]}"
        if self.fact_id != expected_id:
            raise ValueError("fact ID is invalid")

    @property
    def eligible_for_current(self) -> bool:
        return self.verification_status.eligible_for_current

    def identity_dict(self) -> dict[str, Any]:
        return {
            "fact_contract_version": self.fact_contract_version,
            "field_key": self.field_key.value,
            "field_schema_version": self.field_schema_version,
            "field_version": self.field_version,
            "normalization_policy_version": self.normalization_policy_version,
            "normalized_value": self.normalized_value,
            "parent_fact_id": self.parent_fact_id,
            "source_ref": self.source_ref.to_dict(),
            "subject_id": self.subject_id,
            "supersedes_fact_id": self.supersedes_fact_id,
            "value_type": self.value_type,
            "verification_status": self.verification_status.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "content_hash": self.content_hash,
            "created_at": _format_time(self.created_at),
            "fact_id": self.fact_id,
            "invocation_id": self.invocation_id,
        }


def _fact_from_dict(value: Mapping[str, Any]) -> CandidateIdentityFact:
    expected = {
        "content_hash",
        "created_at",
        "fact_contract_version",
        "fact_id",
        "field_key",
        "field_schema_version",
        "field_version",
        "invocation_id",
        "normalization_policy_version",
        "normalized_value",
        "parent_fact_id",
        "source_ref",
        "subject_id",
        "supersedes_fact_id",
        "value_type",
        "verification_status",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted fact fields are invalid")
    payload = dict(value)
    payload["created_at"] = _parse_time(payload["created_at"])
    payload["source_ref"] = _source_from_dict(payload["source_ref"])
    return CandidateIdentityFact(**payload)


def _semantic_hash_for_fact(fact: CandidateIdentityFact) -> str:
    return _hash(
        {
            "expected_current_fact_id": (
                fact.supersedes_fact_id if fact.eligible_for_current else None
            ),
            "field_key": fact.field_key.value,
            "normalization_policy_version": fact.normalization_policy_version,
            "normalized_value": fact.normalized_value,
            "parent_fact_id": fact.parent_fact_id,
            "source_ref": fact.source_ref.to_dict(),
            "subject_id": fact.subject_id,
            "supersedes_fact_id": fact.supersedes_fact_id,
            "verification_status": fact.verification_status.value,
        }
    )


@dataclass(frozen=True, slots=True)
class WriteCandidateIdentityFactCommand:
    subject_id: str
    field_key: ApplicationExecutionIdentityFieldKey
    submitted_value: str = field(repr=False)
    verification_status: CandidateIdentityFactVerificationStatus
    source_ref: CandidateIdentityFactSourceRef
    expected_current_fact_id: str | None
    invocation_id: str
    now: datetime
    parent_fact_id: str | None = None


@dataclass(frozen=True, slots=True)
class WriteCandidateIdentityFactResult:
    status: WriteCandidateIdentityFactStatus
    fact: CandidateIdentityFact | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class GetCurrentCandidateIdentityFactCommand:
    subject_id: str
    field_key: ApplicationExecutionIdentityFieldKey


@dataclass(frozen=True, slots=True)
class GetCurrentCandidateIdentityFactResult:
    status: GetCurrentCandidateIdentityFactStatus
    fact: CandidateIdentityFact | None = None
    current_lineage_head_id: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateIdentityFactIndexEntry:
    field_key: ApplicationExecutionIdentityFieldKey
    current_fact_id: str | None
    current_fact_version: int | None
    current_fact_hash: str | None
    verification_status: CandidateIdentityFactVerificationStatus | None
    conflict_state: CandidateIdentityFactConflictState
    source_refs: tuple[CandidateIdentityFactSourceRef, ...] = ()

    def identity_dict(self) -> dict[str, Any]:
        return {
            "conflict_state": self.conflict_state.value,
            "current_fact_hash": self.current_fact_hash,
            "current_fact_id": self.current_fact_id,
            "current_fact_version": self.current_fact_version,
            "field_key": self.field_key.value,
            "source_refs": tuple(item.to_dict() for item in self.source_refs),
            "verification_status": (
                self.verification_status.value
                if self.verification_status is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class CandidateIdentityFactIndex:
    subject_id: str
    entries: tuple[CandidateIdentityFactIndexEntry, ...]
    index_hash: str
    index_contract_version: str = CANDIDATE_IDENTITY_FACT_INDEX_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "subject_id", _clean_id("subject_id", self.subject_id)
        )
        if self.index_contract_version != CANDIDATE_IDENTITY_FACT_INDEX_CONTRACT_VERSION:
            raise ValueError("index contract version is unsupported")
        ordered = tuple(sorted(self.entries, key=lambda item: item.field_key.value))
        if ordered != self.entries or len({item.field_key for item in ordered}) != len(
            ordered
        ):
            raise ValueError("index entries are invalid")
        expected = _hash(
            {
                "entries": tuple(item.identity_dict() for item in ordered),
                "index_contract_version": self.index_contract_version,
                "subject_id": self.subject_id,
            }
        )
        if self.index_hash != expected:
            raise ValueError("index hash is invalid")


@runtime_checkable
class CandidateIdentityFactRepository(Protocol):
    def write(
        self, command: WriteCandidateIdentityFactCommand
    ) -> WriteCandidateIdentityFactResult:
        """Atomically append one fact and CAS its eligible current head."""

    def get_current(
        self, command: GetCurrentCandidateIdentityFactCommand
    ) -> GetCurrentCandidateIdentityFactResult:
        """Return the exact verified current head for one closed field."""

    def get_index(self, subject_id: str) -> CandidateIdentityFactIndex:
        """Return the deterministic subject-scoped current fact index."""


class _CurrentIntegrityError(RuntimeError):
    pass


class _CurrentConflictError(RuntimeError):
    pass


class PrivateHomeCandidateIdentityFactRepository:
    """SQLite-backed immutable facts and cross-process atomic current CAS."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    @property
    def path(self) -> Path:
        return self._home.paths.candidate_identity_facts

    def _connect(self) -> sqlite3.Connection:
        self._home.ensure()
        self._home.ensure_private_file(self.path)
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = DELETE")
        self._initialize(connection)
        os.chmod(self.path, PRIVATE_FILE_MODE)
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS facts (
                fact_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL,
                field_key TEXT NOT NULL,
                field_version INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                semantic_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(subject_id, field_key, field_version),
                UNIQUE(subject_id, field_key, semantic_hash)
            );
            CREATE TABLE IF NOT EXISTS current_heads (
                subject_id TEXT NOT NULL,
                field_key TEXT NOT NULL,
                current_fact_id TEXT NOT NULL,
                current_fact_hash TEXT NOT NULL,
                PRIMARY KEY(subject_id, field_key),
                FOREIGN KEY(current_fact_id) REFERENCES facts(fact_id)
            );
            CREATE TABLE IF NOT EXISTS invocations (
                invocation_id TEXT PRIMARY KEY,
                subject_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                fact_id TEXT NOT NULL,
                FOREIGN KEY(fact_id) REFERENCES facts(fact_id)
            );
            """
        )
        expected = str(CANDIDATE_IDENTITY_FACT_REPOSITORY_SCHEMA_VERSION)
        connection.execute(
            """
            INSERT OR IGNORE INTO metadata(key, value)
            VALUES('schema_version', ?)
            """,
            (expected,),
        )
        connection.commit()
        existing = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if existing is None or existing["value"] != expected:
            raise _CurrentIntegrityError("repository schema version is unsupported")

    @staticmethod
    def _load_facts(
        connection: sqlite3.Connection,
        *,
        subject_id: str,
        field_key: ApplicationExecutionIdentityFieldKey,
    ) -> tuple[CandidateIdentityFact, ...]:
        rows = connection.execute(
            """
            SELECT fact_id, content_hash, semantic_hash, payload_json FROM facts
            WHERE subject_id = ? AND field_key = ?
            ORDER BY field_version
            """,
            (subject_id, field_key.value),
        ).fetchall()
        facts: list[CandidateIdentityFact] = []
        versions: list[int] = []
        for row in rows:
            try:
                fact = _fact_from_dict(json.loads(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise _CurrentIntegrityError("persisted fact is invalid") from exc
            if (
                fact.subject_id != subject_id
                or fact.field_key is not field_key
                or row["fact_id"] != fact.fact_id
                or row["content_hash"] != fact.content_hash
                or row["semantic_hash"] != _semantic_hash_for_fact(fact)
            ):
                raise _CurrentIntegrityError("persisted fact binding is invalid")
            facts.append(fact)
            versions.append(fact.field_version)
        if versions != list(range(1, len(versions) + 1)):
            raise _CurrentIntegrityError("field versions are not contiguous")
        by_id = {item.fact_id: item for item in facts}
        for fact in facts:
            for reference in (fact.parent_fact_id, fact.supersedes_fact_id):
                if reference is None:
                    continue
                target = by_id.get(reference)
                if target is None or target.field_version >= fact.field_version:
                    raise _CurrentIntegrityError("fact lineage is invalid")
            if (
                fact.supersedes_fact_id is not None
                and not by_id[fact.supersedes_fact_id].eligible_for_current
            ):
                raise _CurrentIntegrityError("superseded fact is not verified")
        return tuple(facts)

    @classmethod
    def _read_current_tx(
        cls,
        connection: sqlite3.Connection,
        *,
        subject_id: str,
        field_key: ApplicationExecutionIdentityFieldKey,
    ) -> CandidateIdentityFact | None:
        facts = cls._load_facts(
            connection, subject_id=subject_id, field_key=field_key
        )
        eligible = tuple(item for item in facts if item.eligible_for_current)
        current_row = connection.execute(
            """
            SELECT current_fact_id, current_fact_hash FROM current_heads
            WHERE subject_id = ? AND field_key = ?
            """,
            (subject_id, field_key.value),
        ).fetchone()
        if not eligible:
            if current_row is not None:
                raise _CurrentIntegrityError("current head has no verified fact")
            return None
        superseded = {
            item.supersedes_fact_id
            for item in eligible
            if item.supersedes_fact_id is not None
        }
        heads = tuple(item for item in eligible if item.fact_id not in superseded)
        if len(heads) != 1:
            raise _CurrentConflictError("verified fact lineage has multiple heads")
        head = heads[0]
        if (
            current_row is None
            or current_row["current_fact_id"] != head.fact_id
            or current_row["current_fact_hash"] != head.content_hash
        ):
            raise _CurrentIntegrityError("current index binding is invalid")
        return head

    def write(
        self,
        command: WriteCandidateIdentityFactCommand,
    ) -> WriteCandidateIdentityFactResult:
        try:
            subject_id = _clean_id("subject_id", command.subject_id)
            field_key = ApplicationExecutionIdentityFieldKey(command.field_key)
            status = CandidateIdentityFactVerificationStatus(
                command.verification_status
            )
            invocation_id = _clean_id("invocation_id", command.invocation_id)
            created_at = _format_time(command.now)
            normalized = normalize_application_execution_identity_value(
                field_key, command.submitted_value
            )
            source = command.source_ref
            if not isinstance(source, CandidateIdentityFactSourceRef):
                raise TypeError("source_ref is invalid")
            if source.source_subject_id != subject_id:
                raise ValueError("source subject binding is invalid")
            _validate_source_eligibility(status, source)
            parent_id = (
                _clean_id("parent_fact_id", command.parent_fact_id)
                if command.parent_fact_id is not None
                else None
            )
            expected_id = (
                _clean_id(
                    "expected_current_fact_id",
                    command.expected_current_fact_id,
                )
                if command.expected_current_fact_id is not None
                else None
            )
            if not status.eligible_for_current and expected_id is not None:
                raise ValueError("non-current fact cannot bind expected current")
            request_identity = {
                "expected_current_fact_id": expected_id,
                "field_key": field_key.value,
                "normalization_policy_version": (
                    APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION
                ),
                "normalized_value": normalized,
                "parent_fact_id": parent_id,
                "source_ref": source.to_dict(),
                "subject_id": subject_id,
                "verification_status": status.value,
            }
            request_hash = _hash(request_identity)
        except (TypeError, ValueError):
            return WriteCandidateIdentityFactResult(
                WriteCandidateIdentityFactStatus.INVALID,
                failure_code="CANDIDATE_IDENTITY_FACT_INVALID",
            )

        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = connection.execute(
                    """
                    SELECT subject_id, request_hash, fact_id FROM invocations
                    WHERE invocation_id = ?
                    """,
                    (invocation_id,),
                ).fetchone()
                if replay is not None:
                    if (
                        replay["subject_id"] != subject_id
                        or replay["request_hash"] != request_hash
                    ):
                        connection.rollback()
                        return WriteCandidateIdentityFactResult(
                            WriteCandidateIdentityFactStatus.INTEGRITY_FAILURE,
                            failure_code="INVOCATION_PAYLOAD_MISMATCH",
                        )
                    fact = self._fact_by_id_tx(
                        connection,
                        subject_id=subject_id,
                        fact_id=replay["fact_id"],
                    )
                    connection.rollback()
                    return WriteCandidateIdentityFactResult(
                        WriteCandidateIdentityFactStatus.UNCHANGED, fact=fact
                    )

                prior_request = connection.execute(
                    """
                    SELECT i.fact_id
                    FROM invocations AS i
                    WHERE i.subject_id = ? AND i.request_hash = ?
                    ORDER BY i.invocation_id
                    LIMIT 1
                    """,
                    (subject_id, request_hash),
                ).fetchone()
                if prior_request is not None:
                    fact = self._fact_by_id_tx(
                        connection,
                        subject_id=subject_id,
                        fact_id=prior_request["fact_id"],
                    )
                    connection.execute(
                        """
                        INSERT INTO invocations(
                            invocation_id, subject_id, request_hash, fact_id
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (invocation_id, subject_id, request_hash, fact.fact_id),
                    )
                    connection.commit()
                    return WriteCandidateIdentityFactResult(
                        WriteCandidateIdentityFactStatus.UNCHANGED, fact=fact
                    )

                current = self._read_current_tx(
                    connection, subject_id=subject_id, field_key=field_key
                )
                if parent_id is not None:
                    parent = self._fact_by_id_tx(
                        connection, subject_id=subject_id, fact_id=parent_id
                    )
                    if parent.field_key is not field_key:
                        raise _CurrentIntegrityError(
                            "parent fact field binding is invalid"
                        )
                if status.eligible_for_current:
                    actual_id = current.fact_id if current is not None else None
                    if expected_id != actual_id:
                        connection.rollback()
                        return WriteCandidateIdentityFactResult(
                            WriteCandidateIdentityFactStatus.STALE_CURRENT,
                            failure_code="EXPECTED_CURRENT_MISMATCH",
                        )
                supersedes_id = (
                    current.fact_id
                    if status.eligible_for_current and current is not None
                    else None
                )
                if status.eligible_for_current and parent_id is None:
                    parent_id = supersedes_id
                semantic_identity = {
                    **request_identity,
                    "parent_fact_id": parent_id,
                    "supersedes_fact_id": supersedes_id,
                }
                semantic_hash = _hash(semantic_identity)
                duplicate = connection.execute(
                    """
                    SELECT content_hash, payload_json FROM facts
                    WHERE subject_id = ? AND field_key = ? AND semantic_hash = ?
                    """,
                    (subject_id, field_key.value, semantic_hash),
                ).fetchone()
                if duplicate is not None:
                    try:
                        fact = _fact_from_dict(
                            json.loads(duplicate["payload_json"])
                        )
                    except (
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as exc:
                        raise _CurrentIntegrityError(
                            "duplicate fact is invalid"
                        ) from exc
                    if duplicate["content_hash"] != fact.content_hash:
                        raise _CurrentIntegrityError(
                            "duplicate fact binding is invalid"
                        )
                    connection.execute(
                        """
                        INSERT INTO invocations(
                            invocation_id, subject_id, request_hash, fact_id
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (invocation_id, subject_id, request_hash, fact.fact_id),
                    )
                    connection.commit()
                    return WriteCandidateIdentityFactResult(
                        WriteCandidateIdentityFactStatus.UNCHANGED, fact=fact
                    )

                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(field_version), 0) AS maximum
                    FROM facts WHERE subject_id = ? AND field_key = ?
                    """,
                    (subject_id, field_key.value),
                ).fetchone()
                field_version = int(row["maximum"]) + 1
                fact_identity = {
                    "fact_contract_version": CANDIDATE_IDENTITY_FACT_CONTRACT_VERSION,
                    "field_key": field_key.value,
                    "field_schema_version": (
                        APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION
                    ),
                    "field_version": field_version,
                    "normalization_policy_version": (
                        APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION
                    ),
                    "normalized_value": normalized,
                    "parent_fact_id": parent_id,
                    "source_ref": source.to_dict(),
                    "subject_id": subject_id,
                    "supersedes_fact_id": supersedes_id,
                    "value_type": "string",
                    "verification_status": status.value,
                }
                content_hash = _hash(fact_identity)
                fact = CandidateIdentityFact(
                    fact_id=f"candidate-identity-fact-{content_hash[:32]}",
                    subject_id=subject_id,
                    field_key=field_key,
                    normalized_value=normalized,
                    verification_status=status,
                    source_ref=source,
                    parent_fact_id=parent_id,
                    field_version=field_version,
                    content_hash=content_hash,
                    created_at=_parse_time(created_at),
                    invocation_id=invocation_id,
                    supersedes_fact_id=supersedes_id,
                )
                connection.execute(
                    """
                    INSERT INTO facts(
                        fact_id, subject_id, field_key, field_version,
                        content_hash, verification_status, semantic_hash,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact.fact_id,
                        subject_id,
                        field_key.value,
                        field_version,
                        content_hash,
                        status.value,
                        semantic_hash,
                        json.dumps(
                            fact.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    ),
                )
                if status.eligible_for_current:
                    if current is None:
                        connection.execute(
                            """
                            INSERT INTO current_heads(
                                subject_id, field_key, current_fact_id,
                                current_fact_hash
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                subject_id,
                                field_key.value,
                                fact.fact_id,
                                fact.content_hash,
                            ),
                        )
                    else:
                        updated = connection.execute(
                            """
                            UPDATE current_heads
                            SET current_fact_id = ?, current_fact_hash = ?
                            WHERE subject_id = ? AND field_key = ?
                              AND current_fact_id = ?
                            """,
                            (
                                fact.fact_id,
                                fact.content_hash,
                                subject_id,
                                field_key.value,
                                expected_id,
                            ),
                        ).rowcount
                        if updated != 1:
                            connection.rollback()
                            return WriteCandidateIdentityFactResult(
                                WriteCandidateIdentityFactStatus.STALE_CURRENT,
                                failure_code="CURRENT_CAS_FAILED",
                            )
                connection.execute(
                    """
                    INSERT INTO invocations(
                        invocation_id, subject_id, request_hash, fact_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (invocation_id, subject_id, request_hash, fact.fact_id),
                )
                connection.commit()
                return WriteCandidateIdentityFactResult(
                    WriteCandidateIdentityFactStatus.CREATED, fact=fact
                )
        except _CurrentConflictError:
            return WriteCandidateIdentityFactResult(
                WriteCandidateIdentityFactStatus.CONFLICT,
                failure_code="CURRENT_LINEAGE_CONFLICT",
            )
        except _CurrentIntegrityError:
            return WriteCandidateIdentityFactResult(
                WriteCandidateIdentityFactStatus.INTEGRITY_FAILURE,
                failure_code="CURRENT_LINEAGE_INTEGRITY_FAILURE",
            )
        except (
            OSError,
            PrivateHomeError,
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return WriteCandidateIdentityFactResult(
                WriteCandidateIdentityFactStatus.FAILED,
                failure_code="CANDIDATE_IDENTITY_FACT_WRITE_FAILED",
            )

    @staticmethod
    def _fact_by_id_tx(
        connection: sqlite3.Connection,
        *,
        subject_id: str,
        fact_id: str,
    ) -> CandidateIdentityFact:
        row = connection.execute(
            """
            SELECT content_hash, semantic_hash, payload_json FROM facts
            WHERE fact_id = ? AND subject_id = ?
            """,
            (fact_id, subject_id),
        ).fetchone()
        if row is None:
            raise _CurrentIntegrityError("fact reference is missing")
        try:
            fact = _fact_from_dict(json.loads(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _CurrentIntegrityError("fact reference is invalid") from exc
        if (
            row["content_hash"] != fact.content_hash
            or row["semantic_hash"] != _semantic_hash_for_fact(fact)
        ):
            raise _CurrentIntegrityError("fact reference binding is invalid")
        return fact

    def get_current(
        self,
        command: GetCurrentCandidateIdentityFactCommand,
    ) -> GetCurrentCandidateIdentityFactResult:
        try:
            subject_id = _clean_id("subject_id", command.subject_id)
            field_key = ApplicationExecutionIdentityFieldKey(command.field_key)
            if not self.path.exists():
                return GetCurrentCandidateIdentityFactResult(
                    GetCurrentCandidateIdentityFactStatus.MISSING
                )
            with closing(self._connect()) as connection:
                current = self._read_current_tx(
                    connection, subject_id=subject_id, field_key=field_key
                )
            if current is None:
                return GetCurrentCandidateIdentityFactResult(
                    GetCurrentCandidateIdentityFactStatus.MISSING
                )
            return GetCurrentCandidateIdentityFactResult(
                GetCurrentCandidateIdentityFactStatus.FOUND,
                fact=current,
                current_lineage_head_id=current.fact_id,
            )
        except _CurrentConflictError:
            return GetCurrentCandidateIdentityFactResult(
                GetCurrentCandidateIdentityFactStatus.CONFLICT,
                failure_code="CURRENT_LINEAGE_CONFLICT",
            )
        except (
            _CurrentIntegrityError,
            OSError,
            PrivateHomeError,
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return GetCurrentCandidateIdentityFactResult(
                GetCurrentCandidateIdentityFactStatus.INTEGRITY_FAILURE,
                failure_code="CURRENT_LINEAGE_INTEGRITY_FAILURE",
            )

    def get_index(self, subject_id: str) -> CandidateIdentityFactIndex:
        subject = _clean_id("subject_id", subject_id)
        entries: list[CandidateIdentityFactIndexEntry] = []
        if not self.path.exists():
            entries = [
                CandidateIdentityFactIndexEntry(
                    field_key=key,
                    current_fact_id=None,
                    current_fact_version=None,
                    current_fact_hash=None,
                    verification_status=None,
                    conflict_state=CandidateIdentityFactConflictState.NONE,
                    source_refs=(),
                )
                for key in sorted(
                    ApplicationExecutionIdentityFieldKey,
                    key=lambda item: item.value,
                )
            ]
            return CandidateIdentityFactIndex(
                subject_id=subject,
                entries=tuple(entries),
                index_hash=_hash(
                    {
                        "entries": tuple(
                            item.identity_dict() for item in entries
                        ),
                        "index_contract_version": (
                            CANDIDATE_IDENTITY_FACT_INDEX_CONTRACT_VERSION
                        ),
                        "subject_id": subject,
                    }
                ),
            )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            for key in sorted(
                ApplicationExecutionIdentityFieldKey,
                key=lambda item: item.value,
            ):
                try:
                    current = self._read_current_tx(
                        connection,
                        subject_id=subject,
                        field_key=key,
                    )
                    state = CandidateIdentityFactConflictState.NONE
                except _CurrentConflictError:
                    current = None
                    state = CandidateIdentityFactConflictState.CONFLICT
                except _CurrentIntegrityError:
                    current = None
                    state = CandidateIdentityFactConflictState.INTEGRITY_FAILURE
                if current is not None:
                    entries.append(
                        CandidateIdentityFactIndexEntry(
                            field_key=key,
                            current_fact_id=current.fact_id,
                            current_fact_version=current.field_version,
                            current_fact_hash=current.content_hash,
                            verification_status=current.verification_status,
                            conflict_state=state,
                            source_refs=(current.source_ref,),
                        )
                    )
                else:
                    entries.append(
                        CandidateIdentityFactIndexEntry(
                            field_key=key,
                            current_fact_id=None,
                            current_fact_version=None,
                            current_fact_hash=None,
                            verification_status=None,
                            conflict_state=state,
                        )
                    )
            connection.rollback()
        identity = {
            "entries": tuple(item.identity_dict() for item in entries),
            "index_contract_version": CANDIDATE_IDENTITY_FACT_INDEX_CONTRACT_VERSION,
            "subject_id": subject,
        }
        return CandidateIdentityFactIndex(
            subject_id=subject,
            entries=tuple(entries),
            index_hash=_hash(identity),
        )


def write_candidate_identity_fact(
    command: WriteCandidateIdentityFactCommand,
    *,
    repository: CandidateIdentityFactRepository,
) -> WriteCandidateIdentityFactResult:
    if not isinstance(repository, CandidateIdentityFactRepository):
        raise TypeError("repository is invalid")
    return repository.write(command)


def get_current_candidate_identity_fact(
    command: GetCurrentCandidateIdentityFactCommand,
    *,
    repository: CandidateIdentityFactRepository,
) -> GetCurrentCandidateIdentityFactResult:
    if not isinstance(repository, CandidateIdentityFactRepository):
        raise TypeError("repository is invalid")
    return repository.get_current(command)


__all__ = [
    "CANDIDATE_IDENTITY_FACT_CONTRACT_VERSION",
    "CANDIDATE_IDENTITY_FACT_INDEX_CONTRACT_VERSION",
    "CANDIDATE_IDENTITY_FACT_REPOSITORY_SCHEMA_VERSION",
    "CANDIDATE_IDENTITY_FACT_SOURCE_CONTRACT_VERSION",
    "CandidateIdentityFact",
    "CandidateIdentityFactConflictState",
    "CandidateIdentityFactIndex",
    "CandidateIdentityFactIndexEntry",
    "CandidateIdentityFactRepository",
    "CandidateIdentityFactSourceKind",
    "CandidateIdentityFactSourceRef",
    "CandidateIdentityFactVerificationStatus",
    "GetCurrentCandidateIdentityFactCommand",
    "GetCurrentCandidateIdentityFactResult",
    "GetCurrentCandidateIdentityFactStatus",
    "PrivateHomeCandidateIdentityFactRepository",
    "WriteCandidateIdentityFactCommand",
    "WriteCandidateIdentityFactResult",
    "WriteCandidateIdentityFactStatus",
    "get_current_candidate_identity_fact",
    "write_candidate_identity_fact",
]
