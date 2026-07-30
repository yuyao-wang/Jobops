"""Dependency-light reference for immutable material correction targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


MATERIAL_CORRECTION_TARGET_CONTRACT_VERSION = (
    "material-correction-target-v1"
)
MATERIAL_CORRECTION_TARGET_REF_VERSION = (
    "material-correction-target-ref-v1"
)

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_TARGET_ID_RE = re.compile(r"^material-correction-target-[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class MaterialCorrectionTargetRef:
    target_id: str
    target_version: str
    target_hash: str
    reference_version: str = MATERIAL_CORRECTION_TARGET_REF_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.target_id, str)
            or _TARGET_ID_RE.fullmatch(self.target_id) is None
        ):
            raise ValueError("material correction target ID is invalid")
        if self.target_version != MATERIAL_CORRECTION_TARGET_CONTRACT_VERSION:
            raise ValueError("material correction target is unsupported")
        if (
            not isinstance(self.target_hash, str)
            or _HASH_RE.fullmatch(self.target_hash) is None
            or self.target_id
            != f"material-correction-target-{self.target_hash}"
        ):
            raise ValueError("material correction target hash is invalid")
        if self.reference_version != MATERIAL_CORRECTION_TARGET_REF_VERSION:
            raise ValueError(
                "material correction target reference is unsupported"
            )

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
    ) -> "MaterialCorrectionTargetRef":
        expected = {
            "reference_version",
            "target_hash",
            "target_id",
            "target_version",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError(
                "material correction target reference is invalid"
            )
        return cls(
            target_id=value["target_id"],
            target_version=value["target_version"],
            target_hash=value["target_hash"],
            reference_version=value["reference_version"],
        )


__all__ = [
    "MATERIAL_CORRECTION_TARGET_CONTRACT_VERSION",
    "MATERIAL_CORRECTION_TARGET_REF_VERSION",
    "MaterialCorrectionTargetRef",
]
