"""Typed source lineage for stopped material-publication results."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .application_preparation_orchestrator import (
    ApplicationPreparationStage,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
)


PUBLICATION_STOPPED_SOURCE_LINEAGE_CONTRACT_VERSION = (
    "publication-stopped-source-lineage-v1"
)
_HASH_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class PublicationMaterialKind(StrEnum):
    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"


class PublicationStoppedSourceKind(StrEnum):
    FACT_QA_BLOCKER = "FACT_QA_BLOCKER"
    VISUAL_QA_DIRECTIVE = "VISUAL_QA_DIRECTIVE"
    LAYOUT_REVISION_STOP = "LAYOUT_REVISION_STOP"
    COVER_LETTER_LAYOUT_OVERFLOW = "COVER_LETTER_LAYOUT_OVERFLOW"


class PublicationBlockingDirective(StrEnum):
    FACT_QA_BLOCKED = "FACT_QA_BLOCKED"
    FACT_QA_RESULT_MISSING = "FACT_QA_RESULT_MISSING"
    VISUAL_QA_REVISION_REQUIRED = "VISUAL_QA_REVISION_REQUIRED"
    LAYOUT_REVISION_NOT_SUCCESSFUL = "LAYOUT_REVISION_NOT_SUCCESSFUL"
    COVER_LETTER_LAYOUT_OVERFLOW = "COVER_LETTER_LAYOUT_OVERFLOW"


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text(name: str, value: Any, *, maximum: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is too long")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 hash")
    return value


_SOURCE_STAGE_BY_KIND = {
    PublicationStoppedSourceKind.VISUAL_QA_DIRECTIVE: (
        ApplicationPreparationStage.RESUME_VISUAL_QA
    ),
    PublicationStoppedSourceKind.LAYOUT_REVISION_STOP: (
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION
    ),
    PublicationStoppedSourceKind.COVER_LETTER_LAYOUT_OVERFLOW: (
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION
    ),
}


@dataclass(frozen=True, slots=True)
class PublicationStoppedSourceLineage:
    lineage_id: str
    lineage_content_hash: str
    subject_id: str
    application_plan_id: str
    publication_stage: ApplicationPreparationStage
    publication_result_id: str
    material_kind: PublicationMaterialKind
    source_kind: PublicationStoppedSourceKind
    source_stage: ApplicationPreparationStage
    source_result_id: str
    source_outcome: PreparationStageOutcome
    source_contract_version: str
    source_result_content_hash: str
    source_directive: PublicationBlockingDirective | None
    source_stop_reason: PreparationStopReasonEnvelope | None
    source_artifact_id: str | None
    source_artifact_version: str | None
    source_artifact_content_hash: str | None
    blocking_lineage_ids: tuple[str, ...]
    contract_version: str = (
        PUBLICATION_STOPPED_SOURCE_LINEAGE_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if self.contract_version != (
            PUBLICATION_STOPPED_SOURCE_LINEAGE_CONTRACT_VERSION
        ):
            raise ValueError("publication source-lineage contract is unsupported")
        _text("subject_id", self.subject_id, maximum=160)
        _text("application_plan_id", self.application_plan_id, maximum=160)
        stage = ApplicationPreparationStage(self.publication_stage)
        source_stage = ApplicationPreparationStage(self.source_stage)
        kind = PublicationStoppedSourceKind(self.source_kind)
        material = PublicationMaterialKind(self.material_kind)
        outcome = PreparationStageOutcome(self.source_outcome)
        directive = (
            PublicationBlockingDirective(self.source_directive)
            if self.source_directive is not None
            else None
        )
        object.__setattr__(self, "publication_stage", stage)
        object.__setattr__(self, "source_stage", source_stage)
        object.__setattr__(self, "source_kind", kind)
        object.__setattr__(self, "material_kind", material)
        object.__setattr__(self, "source_outcome", outcome)
        object.__setattr__(self, "source_directive", directive)
        expected_stage = (
            ApplicationPreparationStage.RESUME_PUBLICATION
            if material is PublicationMaterialKind.RESUME
            else ApplicationPreparationStage.COVER_LETTER_PUBLICATION
        )
        if stage is not expected_stage:
            raise ValueError("publication stage does not match material kind")
        if kind is PublicationStoppedSourceKind.FACT_QA_BLOCKER:
            expected_source = (
                ApplicationPreparationStage.RESUME_FACT_QA
                if material is PublicationMaterialKind.RESUME
                else ApplicationPreparationStage.COVER_LETTER_FACT_QA
            )
        else:
            expected_source = _SOURCE_STAGE_BY_KIND[kind]
        if source_stage is not expected_source:
            raise ValueError("source stage does not match lineage kind")
        if (
            material is PublicationMaterialKind.COVER_LETTER
            and kind
            in {
                PublicationStoppedSourceKind.VISUAL_QA_DIRECTIVE,
                PublicationStoppedSourceKind.LAYOUT_REVISION_STOP,
            }
        ):
            raise ValueError("resume lineage cannot bind a cover letter")
        _text("publication_result_id", self.publication_result_id)
        _text("source_result_id", self.source_result_id)
        _text("source_contract_version", self.source_contract_version, maximum=80)
        _hash("source_result_content_hash", self.source_result_content_hash)
        if (directive is None) == (self.source_stop_reason is None):
            raise ValueError(
                "source lineage needs exactly one directive or stop reason"
            )
        if self.source_stop_reason is not None:
            if (
                self.source_stop_reason.stage is not source_stage
                or self.source_stop_reason.outcome is not outcome
            ):
                raise ValueError("child stop reason does not match its source")
        if outcome not in {
            PreparationStageOutcome.COMPLETED,
            PreparationStageOutcome.DEFERRED,
            PreparationStageOutcome.FAILED,
        }:
            raise ValueError("source outcome cannot block publication")
        if self.source_artifact_id is not None:
            _text("source_artifact_id", self.source_artifact_id)
        if self.source_artifact_version is not None:
            _text("source_artifact_version", self.source_artifact_version)
        if self.source_artifact_content_hash is not None:
            _hash(
                "source_artifact_content_hash",
                self.source_artifact_content_hash,
            )
        if (
            not isinstance(self.blocking_lineage_ids, tuple)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in self.blocking_lineage_ids
            )
            or len(set(self.blocking_lineage_ids))
            != len(self.blocking_lineage_ids)
        ):
            raise ValueError("blocking lineage IDs must be unique identifiers")
        if (
            kind is PublicationStoppedSourceKind.FACT_QA_BLOCKER
            and directive is PublicationBlockingDirective.FACT_QA_BLOCKED
            and not self.blocking_lineage_ids
        ):
            raise ValueError("a blocked Fact QA result needs blocker lineage")
        expected_hash = _canonical_hash(self.identity_dict())
        if _hash("lineage_content_hash", self.lineage_content_hash) != expected_hash:
            raise ValueError("publication source-lineage hash is invalid")
        if self.lineage_id != f"publication-stopped-source-{expected_hash}":
            raise ValueError("publication source-lineage ID is invalid")
        expected_publication_id = (
            "publication-stopped-result-"
            + _canonical_hash(self.publication_identity_dict())
        )
        if self.publication_result_id != expected_publication_id:
            raise ValueError("publication result ID is invalid")

    def publication_identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "material_kind": self.material_kind.value,
            "publication_stage": self.publication_stage.value,
            "source_kind": self.source_kind.value,
            "source_result_content_hash": self.source_result_content_hash,
            "source_result_id": self.source_result_id,
            "subject_id": self.subject_id,
        }

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "blocking_lineage_ids": list(self.blocking_lineage_ids),
            "contract_version": self.contract_version,
            "material_kind": self.material_kind.value,
            "publication_result_id": self.publication_result_id,
            "publication_stage": self.publication_stage.value,
            "source_artifact_content_hash": self.source_artifact_content_hash,
            "source_artifact_id": self.source_artifact_id,
            "source_artifact_version": self.source_artifact_version,
            "source_contract_version": self.source_contract_version,
            "source_directive": (
                self.source_directive.value if self.source_directive else None
            ),
            "source_kind": self.source_kind.value,
            "source_outcome": self.source_outcome.value,
            "source_result_content_hash": self.source_result_content_hash,
            "source_result_id": self.source_result_id,
            "source_stage": self.source_stage.value,
            "source_stop_reason": (
                self.source_stop_reason.to_dict()
                if self.source_stop_reason is not None
                else None
            ),
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "lineage_content_hash": self.lineage_content_hash,
            **self.identity_dict(),
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "PublicationStoppedSourceLineage":
        if not isinstance(value, Mapping):
            raise TypeError("publication source lineage must be an object")
        expected = {
            "application_plan_id",
            "blocking_lineage_ids",
            "contract_version",
            "lineage_content_hash",
            "lineage_id",
            "material_kind",
            "publication_result_id",
            "publication_stage",
            "source_artifact_content_hash",
            "source_artifact_id",
            "source_artifact_version",
            "source_contract_version",
            "source_directive",
            "source_kind",
            "source_outcome",
            "source_result_content_hash",
            "source_result_id",
            "source_stage",
            "source_stop_reason",
            "subject_id",
        }
        if set(value) != expected:
            raise ValueError("persisted publication source lineage is invalid")
        stop_reason = None
        if value["source_stop_reason"] is not None:
            from .application_preparation_orchestrator import (
                _stop_reason_from_dict,
            )

            stop_reason = _stop_reason_from_dict(value["source_stop_reason"])
        blockers = value["blocking_lineage_ids"]
        if not isinstance(blockers, list):
            raise TypeError("blocking_lineage_ids must be a list")
        return cls(
            lineage_id=value["lineage_id"],
            lineage_content_hash=value["lineage_content_hash"],
            subject_id=value["subject_id"],
            application_plan_id=value["application_plan_id"],
            publication_stage=value["publication_stage"],
            publication_result_id=value["publication_result_id"],
            material_kind=value["material_kind"],
            source_kind=value["source_kind"],
            source_stage=value["source_stage"],
            source_result_id=value["source_result_id"],
            source_outcome=value["source_outcome"],
            source_contract_version=value["source_contract_version"],
            source_result_content_hash=value[
                "source_result_content_hash"
            ],
            source_directive=value["source_directive"],
            source_stop_reason=stop_reason,
            source_artifact_id=value["source_artifact_id"],
            source_artifact_version=value["source_artifact_version"],
            source_artifact_content_hash=value[
                "source_artifact_content_hash"
            ],
            blocking_lineage_ids=tuple(blockers),
            contract_version=value["contract_version"],
        )

    def output_references(self) -> dict[str, str]:
        values = {
            "publication_stopped_application_plan_id": (
                self.application_plan_id
            ),
            "publication_stopped_material_kind": self.material_kind.value,
            "publication_stopped_source_contract_version": (
                self.source_contract_version
            ),
            "publication_stopped_source_content_hash": (
                self.source_result_content_hash
            ),
            "publication_stopped_source_directive": (
                self.source_directive.value
                if self.source_directive is not None
                else "STOP_REASON"
            ),
            "publication_stopped_source_lineage_id": self.lineage_id,
            "publication_stopped_source_kind": self.source_kind.value,
            "publication_stopped_source_outcome": self.source_outcome.value,
            "publication_stopped_source_result_id": self.source_result_id,
            "publication_stopped_source_stage": self.source_stage.value,
            "publication_stopped_subject_id": self.subject_id,
        }
        if self.source_artifact_id is not None:
            values["publication_stopped_source_artifact_id"] = (
                self.source_artifact_id
            )
        if self.source_artifact_version is not None:
            values["publication_stopped_source_artifact_version"] = (
                self.source_artifact_version
            )
        if self.source_artifact_content_hash is not None:
            values["publication_stopped_source_artifact_hash"] = (
                self.source_artifact_content_hash
            )
        if self.source_stop_reason is not None:
            values["publication_stopped_child_reason_code"] = (
                self.source_stop_reason.code.value
            )
            values["publication_stopped_child_reason_version"] = (
                self.source_stop_reason.contract_version
            )
        for index, blocker_id in enumerate(self.blocking_lineage_ids):
            values[
                f"publication_stopped_blocker_{index:03d}"
            ] = blocker_id
        return values


def create_publication_stopped_source_lineage(
    *,
    subject_id: str,
    application_plan_id: str,
    publication_stage: ApplicationPreparationStage,
    material_kind: PublicationMaterialKind,
    source_kind: PublicationStoppedSourceKind,
    source_stage: ApplicationPreparationStage,
    source_result_id: str,
    source_outcome: PreparationStageOutcome,
    source_contract_version: str,
    source_result_content_hash: str,
    source_directive: PublicationBlockingDirective | None = None,
    source_stop_reason: PreparationStopReasonEnvelope | None = None,
    source_artifact_id: str | None = None,
    source_artifact_version: str | None = None,
    source_artifact_content_hash: str | None = None,
    blocking_lineage_ids: tuple[str, ...] = (),
) -> PublicationStoppedSourceLineage:
    publication_identity = {
        "application_plan_id": application_plan_id,
        "material_kind": PublicationMaterialKind(material_kind).value,
        "publication_stage": ApplicationPreparationStage(
            publication_stage
        ).value,
        "source_kind": PublicationStoppedSourceKind(source_kind).value,
        "source_result_content_hash": source_result_content_hash,
        "source_result_id": source_result_id,
        "subject_id": subject_id,
    }
    publication_result_id = (
        "publication-stopped-result-" + _canonical_hash(publication_identity)
    )
    prototype = {
        "application_plan_id": application_plan_id,
        "blocking_lineage_ids": list(blocking_lineage_ids),
        "contract_version": (
            PUBLICATION_STOPPED_SOURCE_LINEAGE_CONTRACT_VERSION
        ),
        "material_kind": PublicationMaterialKind(material_kind).value,
        "publication_result_id": publication_result_id,
        "publication_stage": ApplicationPreparationStage(
            publication_stage
        ).value,
        "source_artifact_content_hash": source_artifact_content_hash,
        "source_artifact_id": source_artifact_id,
        "source_artifact_version": source_artifact_version,
        "source_contract_version": source_contract_version,
        "source_directive": (
            PublicationBlockingDirective(source_directive).value
            if source_directive is not None
            else None
        ),
        "source_kind": PublicationStoppedSourceKind(source_kind).value,
        "source_outcome": PreparationStageOutcome(source_outcome).value,
        "source_result_content_hash": source_result_content_hash,
        "source_result_id": source_result_id,
        "source_stage": ApplicationPreparationStage(source_stage).value,
        "source_stop_reason": (
            source_stop_reason.to_dict()
            if source_stop_reason is not None
            else None
        ),
        "subject_id": subject_id,
    }
    content_hash = _canonical_hash(prototype)
    return PublicationStoppedSourceLineage(
        lineage_id=f"publication-stopped-source-{content_hash}",
        lineage_content_hash=content_hash,
        subject_id=subject_id,
        application_plan_id=application_plan_id,
        publication_stage=publication_stage,
        publication_result_id=publication_result_id,
        material_kind=material_kind,
        source_kind=source_kind,
        source_stage=source_stage,
        source_result_id=source_result_id,
        source_outcome=source_outcome,
        source_contract_version=source_contract_version,
        source_result_content_hash=source_result_content_hash,
        source_directive=source_directive,
        source_stop_reason=source_stop_reason,
        source_artifact_id=source_artifact_id,
        source_artifact_version=source_artifact_version,
        source_artifact_content_hash=source_artifact_content_hash,
        blocking_lineage_ids=blocking_lineage_ids,
    )


__all__ = [
    "PUBLICATION_STOPPED_SOURCE_LINEAGE_CONTRACT_VERSION",
    "PublicationBlockingDirective",
    "PublicationMaterialKind",
    "PublicationStoppedSourceKind",
    "PublicationStoppedSourceLineage",
    "create_publication_stopped_source_lineage",
]
