"""Pre-run identities shared by one application-preparation invocation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


PREPARATION_INVOCATION_BINDING_VERSION = "preparation-invocation-binding-v1"
PREPARATION_INVOCATION_REF_VERSION = "preparation-invocation-ref-v1"
COMPILATION_ATTEMPT_BINDING_VERSION = "resume-compilation-attempt-v1"

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_BINDING_ID_RE = re.compile(r"^preparation-invocation-[a-f0-9]{64}$")
_ATTEMPT_ID_RE = re.compile(r"^resume-compilation-attempt-[a-f0-9]{64}$")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _clean(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the invocation contract")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("created_at must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware("created_at", parsed)


@dataclass(frozen=True, slots=True)
class PreparationInvocationBindingRef:
    binding_id: str
    binding_version: str
    binding_hash: str
    reference_version: str = PREPARATION_INVOCATION_REF_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.binding_id, str)
            or _BINDING_ID_RE.fullmatch(self.binding_id) is None
        ):
            raise ValueError("preparation invocation binding ID is invalid")
        if self.binding_version != PREPARATION_INVOCATION_BINDING_VERSION:
            raise ValueError("preparation invocation binding is unsupported")
        _hash("binding_hash", self.binding_hash)
        if self.binding_id != f"preparation-invocation-{self.binding_hash}":
            raise ValueError("preparation invocation reference is inconsistent")
        if self.reference_version != PREPARATION_INVOCATION_REF_VERSION:
            raise ValueError("preparation invocation reference is unsupported")

    def to_dict(self) -> dict[str, str]:
        return {
            "binding_hash": self.binding_hash,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "reference_version": self.reference_version,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "PreparationInvocationBindingRef":
        expected = {
            "binding_hash",
            "binding_id",
            "binding_version",
            "reference_version",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("preparation invocation reference is invalid")
        return cls(
            binding_id=value["binding_id"],
            binding_version=value["binding_version"],
            binding_hash=value["binding_hash"],
            reference_version=value["reference_version"],
        )


@dataclass(frozen=True, slots=True)
class PreparationInvocationBinding:
    binding_id: str
    binding_version: str
    binding_hash: str
    invocation_id: str
    orchestration_contract_version: str
    subject_id: str
    application_plan_id: str
    created_at: datetime

    def __post_init__(self) -> None:
        subject = _clean("subject_id", self.subject_id, 160)
        plan = _clean("application_plan_id", self.application_plan_id, 180)
        invocation = _clean("invocation_id", self.invocation_id, 200)
        if _INVOCATION_ID_RE.fullmatch(invocation) is None:
            raise ValueError("invocation_id has invalid characters")
        orchestration = _clean(
            "orchestration_contract_version",
            self.orchestration_contract_version,
            120,
        )
        if self.binding_version != PREPARATION_INVOCATION_BINDING_VERSION:
            raise ValueError("preparation invocation binding is unsupported")
        binding_hash = _hash("binding_hash", self.binding_hash)
        expected = _canonical_hash(
            {
                "application_plan_id": plan,
                "binding_version": self.binding_version,
                "invocation_id": invocation,
                "orchestration_contract_version": orchestration,
                "subject_id": subject,
            }
        )
        if binding_hash != expected:
            raise ValueError("preparation invocation binding hash is invalid")
        if (
            not isinstance(self.binding_id, str)
            or _BINDING_ID_RE.fullmatch(self.binding_id) is None
            or self.binding_id != f"preparation-invocation-{binding_hash}"
        ):
            raise ValueError("preparation invocation binding ID is invalid")
        _aware("created_at", self.created_at)
        object.__setattr__(self, "subject_id", subject)
        object.__setattr__(self, "application_plan_id", plan)
        object.__setattr__(self, "invocation_id", invocation)

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        application_plan_id: str,
        invocation_id: str,
        orchestration_contract_version: str,
        created_at: datetime,
    ) -> "PreparationInvocationBinding":
        content = {
            "application_plan_id": _clean(
                "application_plan_id", application_plan_id, 180
            ),
            "binding_version": PREPARATION_INVOCATION_BINDING_VERSION,
            "invocation_id": _clean("invocation_id", invocation_id, 200),
            "orchestration_contract_version": _clean(
                "orchestration_contract_version",
                orchestration_contract_version,
                120,
            ),
            "subject_id": _clean("subject_id", subject_id, 160),
        }
        binding_hash = _canonical_hash(content)
        return cls(
            binding_id=f"preparation-invocation-{binding_hash}",
            binding_version=PREPARATION_INVOCATION_BINDING_VERSION,
            binding_hash=binding_hash,
            invocation_id=content["invocation_id"],
            orchestration_contract_version=content[
                "orchestration_contract_version"
            ],
            subject_id=content["subject_id"],
            application_plan_id=content["application_plan_id"],
            created_at=_aware("created_at", created_at),
        )

    @property
    def reference(self) -> PreparationInvocationBindingRef:
        return PreparationInvocationBindingRef(
            binding_id=self.binding_id,
            binding_version=self.binding_version,
            binding_hash=self.binding_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "binding_hash": self.binding_hash,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "created_at": _rfc3339(self.created_at),
            "invocation_id": self.invocation_id,
            "orchestration_contract_version": (
                self.orchestration_contract_version
            ),
            "subject_id": self.subject_id,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "PreparationInvocationBinding":
        expected = {
            "application_plan_id",
            "binding_hash",
            "binding_id",
            "binding_version",
            "created_at",
            "invocation_id",
            "orchestration_contract_version",
            "subject_id",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("preparation invocation binding is invalid")
        return cls(
            binding_id=value["binding_id"],
            binding_version=value["binding_version"],
            binding_hash=value["binding_hash"],
            invocation_id=value["invocation_id"],
            orchestration_contract_version=value[
                "orchestration_contract_version"
            ],
            subject_id=value["subject_id"],
            application_plan_id=value["application_plan_id"],
            created_at=_parse_time(value["created_at"]),
        )


def resume_compilation_attempt_id(
    *,
    invocation: PreparationInvocationBinding,
    subject_id: str,
    application_plan_id: str,
    attempt_number: int,
) -> str:
    if not isinstance(invocation, PreparationInvocationBinding):
        raise TypeError("invocation must be typed")
    subject = _clean("subject_id", subject_id, 160)
    plan = _clean("application_plan_id", application_plan_id, 180)
    if invocation.subject_id != subject or invocation.application_plan_id != plan:
        raise ValueError("compilation attempt binding is cross-subject or plan")
    if type(attempt_number) is not int or attempt_number < 1:
        raise ValueError("attempt_number must be positive")
    digest = _canonical_hash(
        {
            "application_plan_id": plan,
            "attempt_number": attempt_number,
            "attempt_version": COMPILATION_ATTEMPT_BINDING_VERSION,
            "invocation_binding_hash": invocation.binding_hash,
            "stage": "RESUME_COMPILATION",
            "subject_id": subject,
        }
    )
    attempt_id = f"resume-compilation-attempt-{digest}"
    if _ATTEMPT_ID_RE.fullmatch(attempt_id) is None:
        raise ValueError("compilation attempt ID is invalid")
    return attempt_id


__all__ = [
    "COMPILATION_ATTEMPT_BINDING_VERSION",
    "PREPARATION_INVOCATION_BINDING_VERSION",
    "PREPARATION_INVOCATION_REF_VERSION",
    "PreparationInvocationBinding",
    "PreparationInvocationBindingRef",
    "resume_compilation_attempt_id",
]
