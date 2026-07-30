"""Authoritative writes for explicit user-confirmed application facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import RLock
from typing import Any, Mapping

from .application_answer_taxonomy import (
    CanonicalApplicationAnswerKey,
    canonical_application_answer_definition,
)
from .application_answers import PrivateHomeApplicationFactProvider
from .private_home import PrivateHome
from .profile_store import CandidateVault, ProfileStoreError


USER_CONFIRMED_APPLICATION_FACT_CONTRACT_VERSION = (
    "user-confirmed-application-fact-v1"
)
_WRITE_LOCK = RLock()


def _json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ApplicationFactWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class WriteUserConfirmedApplicationFactCommand:
    subject_id: str
    canonical_key: CanonicalApplicationAnswerKey
    value: Any
    source_attention_item_id: str
    user_message_hash: str
    recorded_at: datetime
    allowed_scope: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ApplicationFactWriteResult:
    status: ApplicationFactWriteStatus
    record_id: str | None
    reason_code: str | None


class ApplicationFactWriteService:
    """Write immutable history plus the current CandidateVault projection."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def write_user_confirmed(
        self, command: WriteUserConfirmedApplicationFactCommand
    ) -> ApplicationFactWriteResult:
        try:
            subject = command.subject_id.strip()
            item_id = command.source_attention_item_id.strip()
            key = CanonicalApplicationAnswerKey(command.canonical_key)
            if not subject or not item_id:
                raise ValueError("fact binding is incomplete")
            if (
                not isinstance(command.user_message_hash, str)
                or len(command.user_message_hash) != 64
            ):
                raise ValueError("message hash is invalid")
            confirmed_at = _time(command.recorded_at)
            scope = dict(command.allowed_scope)
            if set(scope) - {"job_id", "job_ids"}:
                raise ValueError("fact scope is unsupported")
            definition = canonical_application_answer_definition(key)
            content = {
                "allowed_scope": scope,
                "canonical_key": key.value,
                "contract_version": (
                    USER_CONFIRMED_APPLICATION_FACT_CONTRACT_VERSION
                ),
                "sensitivity": definition.sensitivity.value,
                "source_attention_item_id": item_id,
                "subject_id": subject,
                "user_message_hash": command.user_message_hash,
                "value": command.value,
            }
            digest = _hash(content)
            record_id = "user-confirmed-application-fact-" + digest
            record = {
                **content,
                "fact_id": record_id,
                "recorded_at": confirmed_at,
                "source": "conversational_application_answer_resolution",
                "source_classification": "USER_CONFIRMED",
                "source_record_id": record_id,
                "verification_status": "USER_CONFIRMED",
                "verified_at": confirmed_at,
            }
            created = self._write_projection(
                subject=subject,
                key=key,
                record_id=record_id,
                record=record,
                confirmed_at=confirmed_at,
                scope=scope,
                sensitivity=definition.sensitivity.value,
                value=command.value,
            )
            return ApplicationFactWriteResult(
                (
                    ApplicationFactWriteStatus.CREATED
                    if created
                    else ApplicationFactWriteStatus.UNCHANGED
                ),
                record_id,
                None,
            )
        except (OSError, ProfileStoreError, TypeError, ValueError):
            return ApplicationFactWriteResult(
                ApplicationFactWriteStatus.FAILED,
                None,
                "AUTHORITATIVE_FACT_WRITE_FAILED",
            )

    def _write_projection(
        self,
        *,
        subject: str,
        key: CanonicalApplicationAnswerKey,
        record_id: str,
        record: Mapping[str, Any],
        confirmed_at: str,
        scope: Mapping[str, Any],
        sensitivity: str,
        value: Any,
    ) -> bool:
        with _WRITE_LOCK:
            vault = CandidateVault.load(self._home)
            if vault.facts.get("subject_id") != subject:
                raise ValueError("CandidateVault subject binding mismatch")
            raw = self._home.paths.verified_answers.read_bytes()
            document = json.loads(raw.decode("utf-8"))
            answers = document.get("answers")
            if not isinstance(answers, dict):
                raise ValueError("verified answer projection is invalid")
            projected = {
                "confirmed_at": confirmed_at,
                "fact_id": record_id,
                "recorded_at": confirmed_at,
                "scope": dict(scope),
                "sensitivity": sensitivity,
                "source": "conversational_application_answer_resolution",
                "source_classification": "USER_CONFIRMED",
                "source_record_id": record_id,
                "value": value,
                "verified": True,
            }
            existing = answers.get(key.value)
            history = (
                self._home.paths.profile
                / "application-fact-records"
                / (record_id + ".json")
            )
            history_bytes = _json(record)
            if existing == projected:
                if not self._home.write_bytes_if_absent(
                    history, history_bytes
                ) and history.read_bytes() != history_bytes:
                    raise ValueError(
                        "immutable fact history conflict"
                    )
                return False
            if not self._home.write_bytes_if_absent(history, history_bytes):
                if history.read_bytes() != history_bytes:
                    raise ValueError("immutable fact history conflict")
            backup_id = hashlib.sha256(raw).hexdigest()
            backup = (
                self._home.paths.profile
                / "verified-answer-projection-history"
                / (backup_id + ".json")
            )
            if not self._home.write_bytes_if_absent(backup, raw):
                if backup.read_bytes() != raw:
                    raise ValueError("projection backup conflict")
            answers[key.value] = projected
            self._home.write_bytes(
                self._home.paths.verified_answers, _json(document)
            )
            snapshot = PrivateHomeApplicationFactProvider(
                self._home
            ).get_current(subject)
            if not any(item.fact_id == record_id for item in snapshot.facts):
                raise ValueError("written fact did not verify")
            return True


__all__ = [
    "ApplicationFactWriteResult",
    "ApplicationFactWriteService",
    "ApplicationFactWriteStatus",
    "USER_CONFIRMED_APPLICATION_FACT_CONTRACT_VERSION",
    "WriteUserConfirmedApplicationFactCommand",
]
