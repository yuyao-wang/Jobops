"""Immutable plan-scoped ResumeCandidate and LaTeX version overrides."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol

from .private_home import PrivateHome


PLAN_SCOPED_VERSION_OVERRIDE_CONTRACT_VERSION = (
    "plan-scoped-version-override-v1"
)
PLAN_SCOPED_VERSION_OVERRIDE_REPLACEMENT_CONTRACT_VERSION = (
    "plan-scoped-version-override-v2"
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


class PlanScopedVersionOverrideKind(StrEnum):
    RESUME_CANDIDATE_OVERRIDE = "RESUME_CANDIDATE_OVERRIDE"
    LATEX_VERSION_OVERRIDE = "LATEX_VERSION_OVERRIDE"


@dataclass(frozen=True, slots=True)
class PlanScopedVersionOverride:
    override_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    override_kind: PlanScopedVersionOverrideKind
    selected_option_id: str
    source_attention_item_id: str
    source_stage: str
    source_record_id: str
    user_message_hash: str
    previous_override_id: str | None
    replacement_target_id: str | None
    replacement_target_version: str | None
    replacement_target_hash: str | None
    replacement_reason: str | None
    replaced_option_id: str | None
    replaced_option_version: str | None
    replaced_option_content_hash: str | None
    created_at: datetime
    override_content_hash: str

    def content_dict(self) -> dict[str, Any]:
        value = {
            "application_plan_id": self.application_plan_id,
            "contract_version": self.contract_version,
            "override_kind": self.override_kind.value,
            "previous_override_id": self.previous_override_id,
            "selected_option_id": self.selected_option_id,
            "source_attention_item_id": self.source_attention_item_id,
            "source_record_id": self.source_record_id,
            "source_stage": self.source_stage,
            "subject_id": self.subject_id,
            "user_message_hash": self.user_message_hash,
        }
        if self.contract_version == (
            PLAN_SCOPED_VERSION_OVERRIDE_REPLACEMENT_CONTRACT_VERSION
        ):
            value.update(
                {
                    "replaced_option_content_hash": (
                        self.replaced_option_content_hash
                    ),
                    "replaced_option_id": self.replaced_option_id,
                    "replaced_option_version": self.replaced_option_version,
                    "replacement_reason": self.replacement_reason,
                    "replacement_target_hash": self.replacement_target_hash,
                    "replacement_target_id": self.replacement_target_id,
                    "replacement_target_version": (
                        self.replacement_target_version
                    ),
                }
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "created_at": _time(self.created_at),
            "override_content_hash": self.override_content_hash,
            "override_id": self.override_id,
        }

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        application_plan_id: str,
        override_kind: PlanScopedVersionOverrideKind,
        selected_option_id: str,
        source_attention_item_id: str,
        source_stage: str,
        source_record_id: str,
        user_message_hash: str,
        previous_override_id: str | None,
        created_at: datetime,
        replacement_target_id: str | None = None,
        replacement_target_version: str | None = None,
        replacement_target_hash: str | None = None,
        replacement_reason: str | None = None,
        replaced_option_id: str | None = None,
        replaced_option_version: str | None = None,
        replaced_option_content_hash: str | None = None,
    ) -> "PlanScopedVersionOverride":
        kind = PlanScopedVersionOverrideKind(override_kind)
        values = (
            subject_id,
            application_plan_id,
            selected_option_id,
            source_attention_item_id,
            source_stage,
            source_record_id,
        )
        if any(
            not isinstance(value, str) or not value.strip()
            for value in values
        ):
            raise ValueError("version override binding is incomplete")
        if previous_override_id is not None and (
            not isinstance(previous_override_id, str)
            or not previous_override_id.strip()
        ):
            raise ValueError("previous override ID is invalid")
        if (
            not isinstance(user_message_hash, str)
            or len(user_message_hash) != 64
        ):
            raise ValueError("user message hash is invalid")
        provenance = (
            replacement_target_id,
            replacement_target_version,
            replacement_target_hash,
            replacement_reason,
            replaced_option_id,
            replaced_option_version,
            replaced_option_content_hash,
        )
        has_replacement = any(value is not None for value in provenance)
        if has_replacement and any(
            not isinstance(value, str) or not value.strip()
            for value in provenance
        ):
            raise ValueError("replacement override provenance is incomplete")
        if has_replacement and (
            len(replacement_target_hash or "") != 64
            or len(replaced_option_content_hash or "") != 64
            or selected_option_id == replaced_option_id
        ):
            raise ValueError("replacement override provenance is invalid")
        contract_version = (
            PLAN_SCOPED_VERSION_OVERRIDE_REPLACEMENT_CONTRACT_VERSION
            if has_replacement
            else PLAN_SCOPED_VERSION_OVERRIDE_CONTRACT_VERSION
        )
        content = {
            "application_plan_id": application_plan_id,
            "contract_version": contract_version,
            "override_kind": kind.value,
            "previous_override_id": previous_override_id,
            "selected_option_id": selected_option_id,
            "source_attention_item_id": source_attention_item_id,
            "source_record_id": source_record_id,
            "source_stage": source_stage,
            "subject_id": subject_id,
            "user_message_hash": user_message_hash,
        }
        if has_replacement:
            content.update(
                {
                    "replaced_option_content_hash": (
                        replaced_option_content_hash
                    ),
                    "replaced_option_id": replaced_option_id,
                    "replaced_option_version": replaced_option_version,
                    "replacement_reason": replacement_reason,
                    "replacement_target_hash": replacement_target_hash,
                    "replacement_target_id": replacement_target_id,
                    "replacement_target_version": replacement_target_version,
                }
            )
        digest = _hash(content)
        return cls(
            override_id="plan-scoped-version-override-" + digest,
            contract_version=contract_version,
            subject_id=subject_id,
            application_plan_id=application_plan_id,
            override_kind=kind,
            selected_option_id=selected_option_id,
            source_attention_item_id=source_attention_item_id,
            source_stage=source_stage,
            source_record_id=source_record_id,
            user_message_hash=user_message_hash,
            previous_override_id=previous_override_id,
            replacement_target_id=replacement_target_id,
            replacement_target_version=replacement_target_version,
            replacement_target_hash=replacement_target_hash,
            replacement_reason=replacement_reason,
            replaced_option_id=replaced_option_id,
            replaced_option_version=replaced_option_version,
            replaced_option_content_hash=replaced_option_content_hash,
            created_at=created_at,
            override_content_hash=digest,
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "PlanScopedVersionOverride":
        value = cls(
            override_id=str(payload["override_id"]),
            contract_version=str(payload["contract_version"]),
            subject_id=str(payload["subject_id"]),
            application_plan_id=str(payload["application_plan_id"]),
            override_kind=PlanScopedVersionOverrideKind(
                payload["override_kind"]
            ),
            selected_option_id=str(payload["selected_option_id"]),
            source_attention_item_id=str(
                payload["source_attention_item_id"]
            ),
            source_stage=str(payload["source_stage"]),
            source_record_id=str(payload["source_record_id"]),
            user_message_hash=str(payload["user_message_hash"]),
            previous_override_id=payload.get("previous_override_id"),
            replacement_target_id=payload.get("replacement_target_id"),
            replacement_target_version=payload.get(
                "replacement_target_version"
            ),
            replacement_target_hash=payload.get("replacement_target_hash"),
            replacement_reason=payload.get("replacement_reason"),
            replaced_option_id=payload.get("replaced_option_id"),
            replaced_option_version=payload.get("replaced_option_version"),
            replaced_option_content_hash=payload.get(
                "replaced_option_content_hash"
            ),
            created_at=datetime.fromisoformat(
                str(payload["created_at"]).replace("Z", "+00:00")
            ),
            override_content_hash=str(payload["override_content_hash"]),
        )
        if (
            value.contract_version
            not in {
                PLAN_SCOPED_VERSION_OVERRIDE_CONTRACT_VERSION,
                PLAN_SCOPED_VERSION_OVERRIDE_REPLACEMENT_CONTRACT_VERSION,
            }
            or value.override_content_hash != _hash(value.content_dict())
            or value.override_id
            != "plan-scoped-version-override-"
            + value.override_content_hash
        ):
            raise ValueError("version override integrity failure")
        _time(value.created_at)
        return value


class PlanScopedVersionOverrideProvider(Protocol):
    def get_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        override_kind: PlanScopedVersionOverrideKind,
    ) -> PlanScopedVersionOverride | None: ...


class PlanScopedVersionOverrideRepository(PlanScopedVersionOverrideProvider):
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        key = hashlib.sha256(subject_id.encode()).hexdigest()
        return (
            self._home.paths.preparation
            / "plan-scoped-version-overrides"
            / ("subject-" + key)
        )

    def save(self, value: PlanScopedVersionOverride) -> bool:
        if not isinstance(value, PlanScopedVersionOverride):
            raise TypeError("version override must be typed")
        path = self._directory(value.subject_id) / (
            value.override_id + ".json"
        )
        content = _json(value.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable version override conflict")
        return created

    def get_current(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        override_kind: PlanScopedVersionOverrideKind,
    ) -> PlanScopedVersionOverride | None:
        directory = self._home.contained_path(
            self._directory(subject_id)
        )
        if not directory.exists():
            return None
        matches: list[PlanScopedVersionOverride] = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            path = self._home.contained_path(path)
            value = PlanScopedVersionOverride.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if value.subject_id != subject_id:
                raise ValueError("version override subject mismatch")
            if (
                value.application_plan_id == application_plan_id
                and value.override_kind
                is PlanScopedVersionOverrideKind(override_kind)
            ):
                matches.append(value)
        if not matches:
            return None
        return max(
            matches,
            key=lambda item: (
                item.created_at.astimezone(timezone.utc),
                item.override_id,
            ),
        )


__all__ = [
    "PLAN_SCOPED_VERSION_OVERRIDE_CONTRACT_VERSION",
    "PLAN_SCOPED_VERSION_OVERRIDE_REPLACEMENT_CONTRACT_VERSION",
    "PlanScopedVersionOverride",
    "PlanScopedVersionOverrideKind",
    "PlanScopedVersionOverrideProvider",
    "PlanScopedVersionOverrideRepository",
]
