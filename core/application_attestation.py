"""Plan-scoped candidate attestations for application-answer resolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol

from .application_answer_taxonomy import CanonicalApplicationAnswerKey
from .private_home import PrivateHome


PLAN_SCOPED_APPLICATION_ATTESTATION_CONTRACT_VERSION = (
    "plan-scoped-application-attestation-v1"
)


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


class ApplicationAttestationDecision(StrEnum):
    CONFIRMED = "CONFIRMED"
    DECLINED = "DECLINED"


@dataclass(frozen=True, slots=True)
class PlanScopedApplicationAttestation:
    attestation_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    canonical_key: CanonicalApplicationAnswerKey
    statement: str
    statement_version: str
    decision: ApplicationAttestationDecision
    source_attention_item_id: str
    user_message_hash: str
    decided_at: datetime
    attestation_content_hash: str

    def content_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "canonical_key": self.canonical_key.value,
            "contract_version": self.contract_version,
            "decision": self.decision.value,
            "source_attention_item_id": self.source_attention_item_id,
            "statement": self.statement,
            "statement_version": self.statement_version,
            "subject_id": self.subject_id,
            "user_message_hash": self.user_message_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "attestation_content_hash": self.attestation_content_hash,
            "attestation_id": self.attestation_id,
            "decided_at": _time(self.decided_at),
        }

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        application_plan_id: str,
        canonical_key: CanonicalApplicationAnswerKey,
        statement: str,
        statement_version: str,
        decision: ApplicationAttestationDecision,
        source_attention_item_id: str,
        user_message_hash: str,
        decided_at: datetime,
    ) -> "PlanScopedApplicationAttestation":
        key = CanonicalApplicationAnswerKey(canonical_key)
        if key not in {
            CanonicalApplicationAnswerKey.ATTESTATION,
            CanonicalApplicationAnswerKey.CONSENT,
            CanonicalApplicationAnswerKey.SIGNATURE,
        }:
            raise ValueError("canonical key is not an attestation")
        for value in (
            subject_id,
            application_plan_id,
            statement,
            statement_version,
            source_attention_item_id,
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("attestation binding is incomplete")
        if (
            not isinstance(user_message_hash, str)
            or len(user_message_hash) != 64
        ):
            raise ValueError("user message hash is invalid")
        content = {
            "application_plan_id": application_plan_id,
            "canonical_key": key.value,
            "contract_version": (
                PLAN_SCOPED_APPLICATION_ATTESTATION_CONTRACT_VERSION
            ),
            "decision": ApplicationAttestationDecision(decision).value,
            "source_attention_item_id": source_attention_item_id,
            "statement": statement,
            "statement_version": statement_version,
            "subject_id": subject_id,
            "user_message_hash": user_message_hash,
        }
        digest = _hash(content)
        return cls(
            attestation_id="plan-application-attestation-" + digest,
            contract_version=(
                PLAN_SCOPED_APPLICATION_ATTESTATION_CONTRACT_VERSION
            ),
            subject_id=subject_id,
            application_plan_id=application_plan_id,
            canonical_key=key,
            statement=statement,
            statement_version=statement_version,
            decision=ApplicationAttestationDecision(decision),
            source_attention_item_id=source_attention_item_id,
            user_message_hash=user_message_hash,
            decided_at=decided_at,
            attestation_content_hash=digest,
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "PlanScopedApplicationAttestation":
        created = cls(
            attestation_id=str(payload["attestation_id"]),
            contract_version=str(payload["contract_version"]),
            subject_id=str(payload["subject_id"]),
            application_plan_id=str(payload["application_plan_id"]),
            canonical_key=CanonicalApplicationAnswerKey(
                payload["canonical_key"]
            ),
            statement=str(payload["statement"]),
            statement_version=str(payload["statement_version"]),
            decision=ApplicationAttestationDecision(payload["decision"]),
            source_attention_item_id=str(
                payload["source_attention_item_id"]
            ),
            user_message_hash=str(payload["user_message_hash"]),
            decided_at=datetime.fromisoformat(
                str(payload["decided_at"]).replace("Z", "+00:00")
            ),
            attestation_content_hash=str(
                payload["attestation_content_hash"]
            ),
        )
        if created.contract_version != (
            PLAN_SCOPED_APPLICATION_ATTESTATION_CONTRACT_VERSION
        ):
            raise ValueError("attestation contract is unsupported")
        if created.attestation_content_hash != _hash(created.content_dict()):
            raise ValueError("attestation content hash is invalid")
        if created.attestation_id != (
            "plan-application-attestation-"
            + created.attestation_content_hash
        ):
            raise ValueError("attestation ID is invalid")
        _time(created.decided_at)
        return created


class PlanScopedApplicationAttestationProvider(Protocol):
    def get_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        canonical_key: CanonicalApplicationAnswerKey,
    ) -> PlanScopedApplicationAttestation | None: ...


class PlanScopedApplicationAttestationRepository(
    PlanScopedApplicationAttestationProvider
):
    """Immutable, subject-isolated Private Home repository."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        subject_key = hashlib.sha256(subject_id.encode()).hexdigest()
        return (
            self._home.paths.preparation
            / "plan-application-attestations"
            / ("subject-" + subject_key)
        )

    def save(self, value: PlanScopedApplicationAttestation) -> bool:
        if not isinstance(value, PlanScopedApplicationAttestation):
            raise TypeError("attestation must be typed")
        path = self._directory(value.subject_id) / (
            value.attestation_id + ".json"
        )
        content = _json(value.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable attestation conflict")
        return created

    def get_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        canonical_key: CanonicalApplicationAnswerKey,
    ) -> PlanScopedApplicationAttestation | None:
        directory = self._home.contained_path(
            self._directory(subject_id)
        )
        if not directory.exists():
            return None
        matches: list[PlanScopedApplicationAttestation] = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            path = self._home.contained_path(path)
            value = PlanScopedApplicationAttestation.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if value.subject_id != subject_id:
                raise ValueError("attestation subject binding mismatch")
            if (
                value.application_plan_id == application_plan_id
                and value.canonical_key
                is CanonicalApplicationAnswerKey(canonical_key)
            ):
                matches.append(value)
        if not matches:
            return None
        return max(
            matches,
            key=lambda item: (
                item.decided_at.astimezone(timezone.utc),
                item.attestation_id,
            ),
        )


__all__ = [
    "ApplicationAttestationDecision",
    "PLAN_SCOPED_APPLICATION_ATTESTATION_CONTRACT_VERSION",
    "PlanScopedApplicationAttestation",
    "PlanScopedApplicationAttestationProvider",
    "PlanScopedApplicationAttestationRepository",
]
