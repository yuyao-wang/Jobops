"""Dependency-light reference for immutable input replacement targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


INPUT_REPLACEMENT_TARGET_CONTRACT_VERSION = "input-replacement-target-v1"
INPUT_REPLACEMENT_TARGET_REF_VERSION = "input-replacement-target-ref-v1"

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_TARGET_ID_RE = re.compile(r"^input-replacement-target-[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class InputReplacementTargetRef:
    target_id: str
    target_version: str
    target_hash: str
    reference_version: str = INPUT_REPLACEMENT_TARGET_REF_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.target_id, str)
            or _TARGET_ID_RE.fullmatch(self.target_id) is None
            or self.target_version
            != INPUT_REPLACEMENT_TARGET_CONTRACT_VERSION
            or not isinstance(self.target_hash, str)
            or _HASH_RE.fullmatch(self.target_hash) is None
            or self.target_id
            != f"input-replacement-target-{self.target_hash}"
            or self.reference_version
            != INPUT_REPLACEMENT_TARGET_REF_VERSION
        ):
            raise ValueError("input replacement target reference is invalid")

    def to_dict(self) -> dict[str, str]:
        return {
            "reference_version": self.reference_version,
            "target_hash": self.target_hash,
            "target_id": self.target_id,
            "target_version": self.target_version,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "InputReplacementTargetRef":
        if not isinstance(value, Mapping) or set(value) != {
            "reference_version",
            "target_hash",
            "target_id",
            "target_version",
        }:
            raise ValueError("input replacement target reference is invalid")
        return cls(
            target_id=value["target_id"],
            target_version=value["target_version"],
            target_hash=value["target_hash"],
            reference_version=value["reference_version"],
        )


__all__ = [
    "INPUT_REPLACEMENT_TARGET_CONTRACT_VERSION",
    "INPUT_REPLACEMENT_TARGET_REF_VERSION",
    "InputReplacementTargetRef",
]
