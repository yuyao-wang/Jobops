"""Immutable typed targets for current CORRECT_MATERIAL attention items."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .application_preparation_orchestrator import (
    ApplicationPreparationRun,
    ApplicationPreparationStage,
    ApplicationPreparationStageResult,
    CoverLetterFactQAStopReason,
    CoverLetterPublicationStopReason,
    LatexCompilationStopReason,
    PreparedResumePublicationStopReason,
    PreparationStopReasonEnvelope,
    ResumeCompilationStoppedSourceRef,
    ResumeFactQAStopReason,
    ResumeLayoutRevisionStopReason,
    ResolvedCompilationSourceLineage,
)
from .fact_qa_findings import (
    FactQABlockingFindingProvider,
    FactQABlockingFindingReadStatus,
)
from .human_attention_queue import (
    FactQAFindingAttentionRef,
    HumanAttentionQueueItem,
    HumanAttentionResolutionCapability,
)
from .material_correction_ref import (
    MATERIAL_CORRECTION_TARGET_CONTRACT_VERSION,
    MaterialCorrectionTargetRef,
)
from .private_home import PrivateHome, PrivateHomeError
from .publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    PublicationStoppedSourceLineage,
)
from .resume_compilation import (
    ResumeCompilationReadStatus,
    ResumeCompilationRepository,
)
from .resume_compilation_stopped_source import (
    ResumeCompilationStoppedSourceReadResult,
    ResumeCompilationStoppedSourceProvider,
    ResumeCompilationStoppedSourceReadStatus,
)
from .resume_layout_revision import (
    ResumeLayoutRevisionReadStatus,
    ResumeLayoutRevisionRepository,
    ResumeLayoutRevisionStatus,
)


MATERIAL_CORRECTION_TARGET_STATEMENT_VERSION = (
    "material-correction-statement-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_TARGET_ID_RE = re.compile(r"^material-correction-target-[a-f0-9]{64}$")


class MaterialCorrectionTargetKind(StrEnum):
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    LATEX_COMPILATION = "LATEX_COMPILATION"
    RESUME_VISUAL_LAYOUT = "RESUME_VISUAL_LAYOUT"
    COVER_LETTER_LAYOUT = "COVER_LETTER_LAYOUT"


class ResumeVisualLayoutOriginKind(StrEnum):
    PUBLICATION_VISUAL_QA_DIRECTIVE = (
        "PUBLICATION_VISUAL_QA_DIRECTIVE"
    )
    PUBLICATION_LAYOUT_REVISION_STOP = (
        "PUBLICATION_LAYOUT_REVISION_STOP"
    )
    DIRECT_LAYOUT_ATTEMPTS_EXHAUSTED = (
        "DIRECT_LAYOUT_ATTEMPTS_EXHAUSTED"
    )


class MaterialCorrectionTargetStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    ITEM_NOT_CURRENT = "ITEM_NOT_CURRENT"
    NOT_CORRECTABLE = "NOT_CORRECTABLE"
    TARGET_STALE = "TARGET_STALE"
    TARGET_INCOMPLETE = "TARGET_INCOMPLETE"
    PREVIEW_UNAVAILABLE = "PREVIEW_UNAVAILABLE"
    FAILED = "FAILED"


class MaterialCorrectionTargetWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class MaterialCorrectionTargetReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class UnsupportedClaimCorrectionTarget:
    finding_ref: FactQAFindingAttentionRef
    claim_summary: str
    evidence_support_status: str

    kind: MaterialCorrectionTargetKind = (
        MaterialCorrectionTargetKind.UNSUPPORTED_CLAIM
    )

    def __post_init__(self) -> None:
        if not isinstance(self.finding_ref, FactQAFindingAttentionRef):
            raise TypeError("finding reference must be typed")
        _text("claim_summary", self.claim_summary, 1_200)
        if self.evidence_support_status != "UNSUPPORTED":
            raise ValueError("unsupported-claim status is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_summary": self.claim_summary,
            "evidence_support_status": self.evidence_support_status,
            "finding_ref": self.finding_ref.to_dict(),
            "kind": self.kind.value,
        }


@dataclass(frozen=True, slots=True)
class LatexCompilationCorrectionTarget:
    stopped_source_ref: ResumeCompilationStoppedSourceRef
    construction_result_id: str
    latex_version_id: str
    source_content_hash: str
    compilation_attempt_id: str
    compilation_reason: LatexCompilationStopReason
    diagnostic_category: str

    kind: MaterialCorrectionTargetKind = (
        MaterialCorrectionTargetKind.LATEX_COMPILATION
    )

    def __post_init__(self) -> None:
        if not isinstance(
            self.stopped_source_ref, ResumeCompilationStoppedSourceRef
        ):
            raise TypeError("Compilation stopped-source reference is invalid")
        for name, value in (
            ("construction_result_id", self.construction_result_id),
            ("latex_version_id", self.latex_version_id),
            ("compilation_attempt_id", self.compilation_attempt_id),
            ("diagnostic_category", self.diagnostic_category),
        ):
            _text(name, value, 240)
        _hash("source_content_hash", self.source_content_hash)
        reason = LatexCompilationStopReason(self.compilation_reason)
        if reason not in {
            LatexCompilationStopReason.UNMANAGED_DEPENDENCY,
            LatexCompilationStopReason.COMPILATION_ERROR,
        }:
            raise ValueError("Compilation reason is not correctable")
        object.__setattr__(self, "compilation_reason", reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "compilation_attempt_id": self.compilation_attempt_id,
            "compilation_reason": self.compilation_reason.value,
            "construction_result_id": self.construction_result_id,
            "diagnostic_category": self.diagnostic_category,
            "kind": self.kind.value,
            "latex_version_id": self.latex_version_id,
            "source_content_hash": self.source_content_hash,
            "stopped_source_ref": self.stopped_source_ref.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ResumeVisualLayoutCorrectionTarget:
    origin_kind: ResumeVisualLayoutOriginKind
    source_result_id: str
    artifact_id: str
    artifact_version: str
    artifact_content_hash: str
    latex_source_id: str
    latex_source_content_hash: str
    final_attempt_id: str | None
    attempt_count: int | None
    attempt_limit: int | None
    safe_preview_reference: str | None

    kind: MaterialCorrectionTargetKind = (
        MaterialCorrectionTargetKind.RESUME_VISUAL_LAYOUT
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "origin_kind", ResumeVisualLayoutOriginKind(self.origin_kind)
        )
        for name, value in (
            ("source_result_id", self.source_result_id),
            ("artifact_id", self.artifact_id),
            ("artifact_version", self.artifact_version),
            ("latex_source_id", self.latex_source_id),
        ):
            _text(name, value, 240)
        _hash("artifact_content_hash", self.artifact_content_hash)
        _hash("latex_source_content_hash", self.latex_source_content_hash)
        if (self.final_attempt_id is None) != (self.attempt_count is None):
            raise ValueError("layout attempt identity is incomplete")
        if (self.attempt_count is None) != (self.attempt_limit is None):
            raise ValueError("layout attempt bound is incomplete")
        if self.final_attempt_id is not None:
            _text("final_attempt_id", self.final_attempt_id, 300)
            if (
                type(self.attempt_count) is not int
                or type(self.attempt_limit) is not int
                or not 1 <= self.attempt_count <= self.attempt_limit
            ):
                raise ValueError("layout attempt count is invalid")
        if self.safe_preview_reference is not None:
            _safe_reference(self.safe_preview_reference)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_content_hash": self.artifact_content_hash,
            "artifact_id": self.artifact_id,
            "artifact_version": self.artifact_version,
            "attempt_count": self.attempt_count,
            "attempt_limit": self.attempt_limit,
            "final_attempt_id": self.final_attempt_id,
            "kind": self.kind.value,
            "latex_source_content_hash": self.latex_source_content_hash,
            "latex_source_id": self.latex_source_id,
            "origin_kind": self.origin_kind.value,
            "safe_preview_reference": self.safe_preview_reference,
            "source_result_id": self.source_result_id,
        }


@dataclass(frozen=True, slots=True)
class CoverLetterLayoutCorrectionTarget:
    publication_result_id: str
    overflow_evaluation_id: str
    latex_source_id: str
    source_version: str
    source_content_hash: str
    overflow_summary: str
    safe_preview_reference: str | None

    kind: MaterialCorrectionTargetKind = (
        MaterialCorrectionTargetKind.COVER_LETTER_LAYOUT
    )

    def __post_init__(self) -> None:
        for name, value in (
            ("publication_result_id", self.publication_result_id),
            ("overflow_evaluation_id", self.overflow_evaluation_id),
            ("latex_source_id", self.latex_source_id),
            ("source_version", self.source_version),
            ("overflow_summary", self.overflow_summary),
        ):
            _text(name, value, 500)
        _hash("source_content_hash", self.source_content_hash)
        if self.safe_preview_reference is not None:
            _safe_reference(self.safe_preview_reference)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "latex_source_id": self.latex_source_id,
            "overflow_evaluation_id": self.overflow_evaluation_id,
            "overflow_summary": self.overflow_summary,
            "publication_result_id": self.publication_result_id,
            "safe_preview_reference": self.safe_preview_reference,
            "source_content_hash": self.source_content_hash,
            "source_version": self.source_version,
        }


MaterialCorrectionTargetPayload = (
    UnsupportedClaimCorrectionTarget
    | LatexCompilationCorrectionTarget
    | ResumeVisualLayoutCorrectionTarget
    | CoverLetterLayoutCorrectionTarget
)


@dataclass(frozen=True, slots=True)
class MaterialCorrectionTarget:
    target_id: str
    target_version: str
    target_hash: str
    subject_id: str
    application_plan_id: str
    preparation_run_id: str
    attention_item_id: str
    origin_stage: ApplicationPreparationStage
    origin_stage_result_id: str
    origin_stop_reason: PreparationStopReasonEnvelope
    source_record_id: str
    source_version: str
    source_content_hash: str
    statement_id: str
    statement_version: str
    payload: MaterialCorrectionTargetPayload
    created_at: datetime

    def __post_init__(self) -> None:
        if self.target_version != MATERIAL_CORRECTION_TARGET_CONTRACT_VERSION:
            raise ValueError("material correction target is unsupported")
        for name, value, maximum in (
            ("subject_id", self.subject_id, 160),
            ("application_plan_id", self.application_plan_id, 180),
            ("preparation_run_id", self.preparation_run_id, 240),
            ("attention_item_id", self.attention_item_id, 240),
            ("origin_stage_result_id", self.origin_stage_result_id, 240),
            ("source_record_id", self.source_record_id, 240),
            ("source_version", self.source_version, 160),
            ("statement_id", self.statement_id, 160),
        ):
            _text(name, value, maximum)
        _hash("source_content_hash", self.source_content_hash)
        stage = ApplicationPreparationStage(self.origin_stage)
        object.__setattr__(self, "origin_stage", stage)
        if (
            not isinstance(self.origin_stop_reason, PreparationStopReasonEnvelope)
            or self.origin_stop_reason.stage is not stage
        ):
            raise ValueError("target stop reason does not match its origin")
        if self.statement_version != (
            MATERIAL_CORRECTION_TARGET_STATEMENT_VERSION
        ):
            raise ValueError("target statement version is unsupported")
        if not isinstance(
            self.payload,
            (
                UnsupportedClaimCorrectionTarget,
                LatexCompilationCorrectionTarget,
                ResumeVisualLayoutCorrectionTarget,
                CoverLetterLayoutCorrectionTarget,
            ),
        ):
            raise TypeError("material correction payload must be typed")
        _aware("created_at", self.created_at)
        expected = _canonical_hash(self.identity_dict())
        if (
            self.target_hash != expected
            or self.target_id != f"material-correction-target-{expected}"
            or _TARGET_ID_RE.fullmatch(self.target_id) is None
        ):
            raise ValueError("material correction target identity is invalid")

    @property
    def kind(self) -> MaterialCorrectionTargetKind:
        return self.payload.kind

    @property
    def reference(self) -> MaterialCorrectionTargetRef:
        return MaterialCorrectionTargetRef(
            target_id=self.target_id,
            target_version=self.target_version,
            target_hash=self.target_hash,
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "origin_stage": self.origin_stage.value,
            "origin_stage_result_id": self.origin_stage_result_id,
            "origin_stop_reason": self.origin_stop_reason.to_dict(),
            "payload": self.payload.to_dict(),
            "preparation_run_id": self.preparation_run_id,
            "source_content_hash": self.source_content_hash,
            "source_record_id": self.source_record_id,
            "source_version": self.source_version,
            "statement_id": self.statement_id,
            "statement_version": self.statement_version,
            "subject_id": self.subject_id,
            "target_version": self.target_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "created_at": _rfc3339(self.created_at),
            "target_hash": self.target_hash,
            "target_id": self.target_id,
        }

    @classmethod
    def create(
        cls,
        *,
        item: HumanAttentionQueueItem,
        run: ApplicationPreparationRun,
        stage_result: ApplicationPreparationStageResult,
        source_record_id: str,
        source_version: str,
        source_content_hash: str,
        statement_id: str,
        payload: MaterialCorrectionTargetPayload,
        created_at: datetime,
    ) -> "MaterialCorrectionTarget":
        if (
            item.subject_id != run.subject_id
            or item.application_plan_id != run.application_plan_id
            or item.source_preparation_run_id != run.run_id
            or item.source_stage is not stage_result.stage
            or item.source_record_id != stage_result.result_id
            or item.resolution_capability
            is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
            or stage_result.stop_reason is None
        ):
            raise ValueError("current correction item binding is invalid")
        prototype = {
            "application_plan_id": item.application_plan_id,
            "attention_item_id": item.item_id,
            "origin_stage": item.source_stage.value,
            "origin_stage_result_id": item.source_record_id,
            "origin_stop_reason": stage_result.stop_reason.to_dict(),
            "payload": payload.to_dict(),
            "preparation_run_id": run.run_id,
            "source_content_hash": source_content_hash,
            "source_record_id": source_record_id,
            "source_version": source_version,
            "statement_id": statement_id,
            "statement_version": (
                MATERIAL_CORRECTION_TARGET_STATEMENT_VERSION
            ),
            "subject_id": item.subject_id,
            "target_version": MATERIAL_CORRECTION_TARGET_CONTRACT_VERSION,
        }
        target_hash = _canonical_hash(prototype)
        return cls(
            target_id=f"material-correction-target-{target_hash}",
            target_version=MATERIAL_CORRECTION_TARGET_CONTRACT_VERSION,
            target_hash=target_hash,
            subject_id=item.subject_id,
            application_plan_id=item.application_plan_id,
            preparation_run_id=run.run_id,
            attention_item_id=item.item_id,
            origin_stage=item.source_stage,
            origin_stage_result_id=item.source_record_id,
            origin_stop_reason=stage_result.stop_reason,
            source_record_id=source_record_id,
            source_version=source_version,
            source_content_hash=source_content_hash,
            statement_id=statement_id,
            statement_version=MATERIAL_CORRECTION_TARGET_STATEMENT_VERSION,
            payload=payload,
            created_at=_aware("created_at", created_at),
        )


CORRECT_MATERIAL_TARGET_KIND_REGISTRY = {
    (
        ApplicationPreparationStage.RESUME_FACT_QA,
        ResumeFactQAStopReason.UNSUPPORTED_CLAIM,
    ): MaterialCorrectionTargetKind.UNSUPPORTED_CLAIM,
    (
        ApplicationPreparationStage.COVER_LETTER_FACT_QA,
        CoverLetterFactQAStopReason.UNSUPPORTED_CLAIM,
    ): MaterialCorrectionTargetKind.UNSUPPORTED_CLAIM,
    (
        ApplicationPreparationStage.RESUME_PUBLICATION,
        PreparedResumePublicationStopReason.FACT_QA_NOT_PASSED,
    ): MaterialCorrectionTargetKind.UNSUPPORTED_CLAIM,
    (
        ApplicationPreparationStage.RESUME_PUBLICATION,
        PreparedResumePublicationStopReason.VISUAL_QA_NOT_PASSED,
    ): MaterialCorrectionTargetKind.RESUME_VISUAL_LAYOUT,
    (
        ApplicationPreparationStage.RESUME_PUBLICATION,
        PreparedResumePublicationStopReason.REVISION_RUN_NOT_SUCCESSFUL,
    ): MaterialCorrectionTargetKind.RESUME_VISUAL_LAYOUT,
    (
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        CoverLetterPublicationStopReason.FACT_QA_NOT_PASSED,
    ): MaterialCorrectionTargetKind.UNSUPPORTED_CLAIM,
    (
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
        CoverLetterPublicationStopReason.LAYOUT_OVERFLOW,
    ): MaterialCorrectionTargetKind.COVER_LETTER_LAYOUT,
    (
        ApplicationPreparationStage.RESUME_COMPILATION,
        LatexCompilationStopReason.UNMANAGED_DEPENDENCY,
    ): MaterialCorrectionTargetKind.LATEX_COMPILATION,
    (
        ApplicationPreparationStage.RESUME_COMPILATION,
        LatexCompilationStopReason.COMPILATION_ERROR,
    ): MaterialCorrectionTargetKind.LATEX_COMPILATION,
    (
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
        ResumeLayoutRevisionStopReason.ATTEMPTS_EXHAUSTED,
    ): MaterialCorrectionTargetKind.RESUME_VISUAL_LAYOUT,
}


@dataclass(frozen=True, slots=True)
class MaterialCorrectionTargetWriteResult:
    status: MaterialCorrectionTargetWriteStatus
    target: MaterialCorrectionTarget | None


@dataclass(frozen=True, slots=True)
class MaterialCorrectionTargetReadResult:
    status: MaterialCorrectionTargetReadStatus
    target: MaterialCorrectionTarget | None


@dataclass(frozen=True, slots=True)
class SafeMaterialCorrectionTarget:
    target_id: str
    target_kind: MaterialCorrectionTargetKind
    material_kind: str
    title: str
    required_action: str
    summary: str
    preview_reference: str | None
    attempt_count: int | None = None
    attempt_limit: int | None = None


@dataclass(frozen=True, slots=True)
class MaterialCorrectionTargetResult:
    status: MaterialCorrectionTargetStatus
    safe_target: SafeMaterialCorrectionTarget | None


@dataclass(frozen=True, slots=True)
class MaterialCorrectionTypedTargetResult:
    status: MaterialCorrectionTargetStatus
    target: MaterialCorrectionTarget | None


@runtime_checkable
class MaterialCorrectionTargetRepository(Protocol):
    def save(
        self, target: MaterialCorrectionTarget
    ) -> MaterialCorrectionTargetWriteResult: ...

    def get(
        self, *, subject_id: str, target_id: str
    ) -> MaterialCorrectionTargetReadResult: ...


class PrivateHomeMaterialCorrectionTargetRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, target_id: str) -> Path:
        subject = _text("subject_id", subject_id, 160)
        if (
            not isinstance(target_id, str)
            or _TARGET_ID_RE.fullmatch(target_id) is None
        ):
            raise ValueError("material correction target ID is invalid")
        return (
            self._home.paths.material_correction_targets
            / ("subject-" + hashlib.sha256(subject.encode()).hexdigest())
            / f"{target_id}.json"
        )

    def save(
        self, target: MaterialCorrectionTarget
    ) -> MaterialCorrectionTargetWriteResult:
        if not isinstance(target, MaterialCorrectionTarget):
            raise TypeError("target must be typed")
        path = self._path(target.subject_id, target.target_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            target.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode(),
                )
            except (OSError, PrivateHomeError):
                return MaterialCorrectionTargetWriteResult(
                    MaterialCorrectionTargetWriteStatus.FAILED, None
                )
            if created:
                return MaterialCorrectionTargetWriteResult(
                    MaterialCorrectionTargetWriteStatus.CREATED, target
                )
            read = self.get(
                subject_id=target.subject_id, target_id=target.target_id
            )
            if (
                read.status is MaterialCorrectionTargetReadStatus.FOUND
                and read.target is not None
                and read.target.identity_dict() == target.identity_dict()
            ):
                return MaterialCorrectionTargetWriteResult(
                    MaterialCorrectionTargetWriteStatus.UNCHANGED,
                    read.target,
                )
            return MaterialCorrectionTargetWriteResult(
                MaterialCorrectionTargetWriteStatus.FAILED, None
            )

    def get(
        self, *, subject_id: str, target_id: str
    ) -> MaterialCorrectionTargetReadResult:
        path = self._path(subject_id, target_id)
        with self._lock:
            if not path.exists():
                return MaterialCorrectionTargetReadResult(
                    MaterialCorrectionTargetReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return MaterialCorrectionTargetReadResult(
                    MaterialCorrectionTargetReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                target = _target_from_dict(value)
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return MaterialCorrectionTargetReadResult(
                    MaterialCorrectionTargetReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            if (
                target.subject_id != subject_id.strip()
                or target.target_id != target_id
            ):
                return MaterialCorrectionTargetReadResult(
                    MaterialCorrectionTargetReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            return MaterialCorrectionTargetReadResult(
                MaterialCorrectionTargetReadStatus.FOUND, target
            )


CurrentAttentionItemReader = Callable[
    [str, str],
    HumanAttentionQueueItem | None,
]


@dataclass(slots=True)
class MaterialCorrectionTargetProvider:
    repository: MaterialCorrectionTargetRepository
    finding_provider: FactQABlockingFindingProvider
    compilation_stopped_provider: ResumeCompilationStoppedSourceProvider
    compilation_repository: ResumeCompilationRepository
    layout_repository: ResumeLayoutRevisionRepository
    current_item_reader: CurrentAttentionItemReader | None = None

    def get_current_ref(
        self,
        *,
        item: HumanAttentionQueueItem,
        run: ApplicationPreparationRun,
        stage_result: ApplicationPreparationStageResult,
        now: datetime,
    ) -> MaterialCorrectionTargetRef | None:
        try:
            target = self._project(
                item=item, run=run, stage_result=stage_result, now=now
            )
            write = self.repository.save(target)
            if (
                write.status is MaterialCorrectionTargetWriteStatus.FAILED
                or write.target is None
                or write.target.reference != target.reference
            ):
                return None
            return write.target.reference
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    def get_current_material_correction_target(
        self, *, subject_id: str, attention_item_id: str
    ) -> MaterialCorrectionTargetResult:
        if self.current_item_reader is None:
            return MaterialCorrectionTargetResult(
                MaterialCorrectionTargetStatus.FAILED, None
            )
        try:
            item = self.current_item_reader(subject_id, attention_item_id)
            if item is None:
                return MaterialCorrectionTargetResult(
                    MaterialCorrectionTargetStatus.ITEM_NOT_CURRENT, None
                )
            if (
                item.subject_id != subject_id
                or item.item_id != attention_item_id
                or item.resolution_capability
                is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
            ):
                return MaterialCorrectionTargetResult(
                    MaterialCorrectionTargetStatus.NOT_CORRECTABLE, None
                )
            typed = self.get_current_typed_target(item=item)
            if typed.target is None:
                return MaterialCorrectionTargetResult(
                    typed.status, None
                )
            target = typed.target
            safe = _safe_target(target)
            status = (
                MaterialCorrectionTargetStatus.PREVIEW_UNAVAILABLE
                if target.kind
                in {
                    MaterialCorrectionTargetKind.RESUME_VISUAL_LAYOUT,
                    MaterialCorrectionTargetKind.COVER_LETTER_LAYOUT,
                }
                and safe.preview_reference is None
                else MaterialCorrectionTargetStatus.AVAILABLE
            )
            return MaterialCorrectionTargetResult(status, safe)
        except (OSError, RuntimeError, TypeError, ValueError):
            return MaterialCorrectionTargetResult(
                MaterialCorrectionTargetStatus.FAILED, None
            )

    def get_current_typed_target(
        self, *, item: HumanAttentionQueueItem
    ) -> MaterialCorrectionTypedTargetResult:
        """Revalidate one already-current item without rereading P2b5."""

        try:
            if (
                not isinstance(item, HumanAttentionQueueItem)
                or item.resolution_capability
                is not HumanAttentionResolutionCapability.CORRECT_MATERIAL
            ):
                return MaterialCorrectionTypedTargetResult(
                    MaterialCorrectionTargetStatus.NOT_CORRECTABLE, None
                )
            reference = item.correction_target_ref
            if reference is None:
                return MaterialCorrectionTypedTargetResult(
                    MaterialCorrectionTargetStatus.TARGET_INCOMPLETE, None
                )
            read = self.repository.get(
                subject_id=item.subject_id, target_id=reference.target_id
            )
            if (
                read.status is not MaterialCorrectionTargetReadStatus.FOUND
                or read.target is None
            ):
                return MaterialCorrectionTypedTargetResult(
                    MaterialCorrectionTargetStatus.TARGET_STALE, None
                )
            target = read.target
            if (
                target.reference != reference
                or target.subject_id != item.subject_id
                or target.attention_item_id != item.item_id
                or target.application_plan_id != item.application_plan_id
                or target.preparation_run_id
                != item.source_preparation_run_id
                or target.origin_stage is not item.source_stage
                or target.origin_stage_result_id != item.source_record_id
                or target.origin_stop_reason.code.value
                != item.source_reason_code
            ):
                return MaterialCorrectionTypedTargetResult(
                    MaterialCorrectionTargetStatus.TARGET_STALE, None
                )
            return MaterialCorrectionTypedTargetResult(
                MaterialCorrectionTargetStatus.AVAILABLE, target
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return MaterialCorrectionTypedTargetResult(
                MaterialCorrectionTargetStatus.FAILED, None
            )

    def get_compilation_stopped_source_for_target(
        self, *, target: MaterialCorrectionTarget
    ) -> ResumeCompilationStoppedSourceReadResult:
        """Resolve and revalidate the exact stopped source behind one target."""

        if (
            not isinstance(target, MaterialCorrectionTarget)
            or not isinstance(
                target.payload, LatexCompilationCorrectionTarget
            )
        ):
            raise TypeError("target must be a LaTeX Compilation target")
        payload = target.payload
        read = self.compilation_stopped_provider.get(
            subject_id=target.subject_id,
            stopped_source_ref=payload.stopped_source_ref,
        )
        if (
            read.status
            is not ResumeCompilationStoppedSourceReadStatus.FOUND
            or read.record is None
            or read.record.subject_id != target.subject_id
            or read.record.application_plan_id
            != target.application_plan_id
            or read.record.reference != payload.stopped_source_ref
            or read.record.compilation_attempt_id
            != payload.compilation_attempt_id
            or not isinstance(
                read.record.source_resolution_lineage,
                ResolvedCompilationSourceLineage,
            )
        ):
            raise ValueError("Compilation stopped-source target drifted")
        lineage = read.record.source_resolution_lineage
        if (
            lineage.construction_result_id
            != payload.construction_result_id
            or lineage.latex_version_id != payload.latex_version_id
            or lineage.source_content_hash
            != payload.source_content_hash
            or read.record.stop_reason.code
            is not payload.compilation_reason
            or target.source_record_id != lineage.construction_result_id
            or target.source_content_hash != lineage.source_content_hash
        ):
            raise ValueError("Compilation target source identity drifted")
        return read

    def get_layout_run_for_target(
        self, *, target: MaterialCorrectionTarget
    ):
        """Resolve the exact immutable Layout run behind one Resume target."""

        if (
            not isinstance(target, MaterialCorrectionTarget)
            or not isinstance(
                target.payload, ResumeVisualLayoutCorrectionTarget
            )
            or target.payload.origin_kind
            is ResumeVisualLayoutOriginKind.PUBLICATION_VISUAL_QA_DIRECTIVE
        ):
            return None
        payload = target.payload
        run = self._layout_run(target.subject_id, payload.source_result_id)
        if (
            run.subject_id != target.subject_id
            or run.application_plan_id != target.application_plan_id
            or not run.attempts
        ):
            raise ValueError("Layout correction run binding is invalid")
        final = run.attempts[-1]
        expected_attempt = f"{run.run_id}:attempt:{final.attempt_number}"
        compilation_id = (
            final.output_compilation_record_id
            or final.input_compilation_record_id
        )
        version_id = (
            final.output_latex_version_id or final.input_latex_version_id
        )
        if (
            payload.final_attempt_id != expected_attempt
            or payload.artifact_id != compilation_id
            or payload.latex_source_id != version_id
            or payload.attempt_count != len(run.attempts)
            or payload.attempt_limit != run.max_attempts
        ):
            raise ValueError("Layout correction final attempt drifted")
        return run

    def _project(
        self,
        *,
        item: HumanAttentionQueueItem,
        run: ApplicationPreparationRun,
        stage_result: ApplicationPreparationStageResult,
        now: datetime,
    ) -> MaterialCorrectionTarget:
        if (
            stage_result.stop_reason is None
            or stage_result.stop_reason.code.value
            != item.source_reason_code
        ):
            raise ValueError("correction item reason binding drifted")
        key = (item.source_stage, stage_result.stop_reason.code)
        kind = CORRECT_MATERIAL_TARGET_KIND_REGISTRY.get(key)
        if kind is None:
            raise ValueError("correctable reason has no target mapping")
        if kind is MaterialCorrectionTargetKind.UNSUPPORTED_CLAIM:
            return self._finding_target(item, run, stage_result, now)
        if kind is MaterialCorrectionTargetKind.LATEX_COMPILATION:
            return self._compilation_target(item, run, stage_result, now)
        if kind is MaterialCorrectionTargetKind.COVER_LETTER_LAYOUT:
            return self._cover_layout_target(item, run, stage_result, now)
        return self._resume_layout_target(item, run, stage_result, now)

    def _finding_target(self, item, run, stage_result, now):
        reference = item.fact_qa_finding_ref
        if reference is None:
            raise ValueError("unsupported claim has no exact finding")
        read = self.finding_provider.list_blocking_findings(
            subject_id=item.subject_id,
            qa_result_id=reference.qa_result_id,
            material_kind=reference.material_kind,
        )
        if (
            read.status is not FactQABlockingFindingReadStatus.FOUND
            or read.finding_set is None
        ):
            raise ValueError("Fact QA findings are unavailable")
        finding = next(
            (
                candidate
                for candidate in read.finding_set.findings
                if candidate.finding_id == reference.finding_id
            ),
            None,
        )
        if (
            finding is None
            or read.finding_set.subject_id != item.subject_id
            or read.finding_set.application_plan_id
            != item.application_plan_id
            or read.finding_set.qa_result_content_hash
            != reference.qa_result_content_hash
            or finding.source_material_id != reference.source_material_id
            or finding.source_material_content_hash
            != reference.source_material_content_hash
        ):
            raise ValueError("Fact QA target binding drifted")
        payload = UnsupportedClaimCorrectionTarget(
            finding_ref=reference,
            claim_summary=finding.claim_summary,
            evidence_support_status="UNSUPPORTED",
        )
        return MaterialCorrectionTarget.create(
            item=item,
            run=run,
            stage_result=stage_result,
            source_record_id=finding.source_material_id,
            source_version=read.finding_set.qa_contract_version,
            source_content_hash=finding.source_material_content_hash,
            statement_id="unsupported-claim-correction",
            payload=payload,
            created_at=now,
        )

    def _compilation_target(self, item, run, stage_result, now):
        reference = stage_result.stopped_source_ref
        if reference is None:
            raise ValueError("Compilation stopped source is unavailable")
        read = self.compilation_stopped_provider.get(
            subject_id=item.subject_id, stopped_source_ref=reference
        )
        if (
            read.status
            is not ResumeCompilationStoppedSourceReadStatus.FOUND
            or read.record is None
            or read.record.application_plan_id != item.application_plan_id
            or read.record.compilation_attempt_id
            != read.record.source_resolution_lineage.compilation_attempt_id
            or not isinstance(
                read.record.source_resolution_lineage,
                ResolvedCompilationSourceLineage,
            )
            or read.record.stop_reason.code
            not in {
                LatexCompilationStopReason.UNMANAGED_DEPENDENCY,
                LatexCompilationStopReason.COMPILATION_ERROR,
            }
        ):
            raise ValueError("Compilation target lineage is invalid")
        lineage = read.record.source_resolution_lineage
        reason = read.record.stop_reason.code
        payload = LatexCompilationCorrectionTarget(
            stopped_source_ref=reference,
            construction_result_id=lineage.construction_result_id,
            latex_version_id=lineage.latex_version_id,
            source_content_hash=lineage.source_content_hash,
            compilation_attempt_id=lineage.compilation_attempt_id,
            compilation_reason=reason,
            diagnostic_category=(
                "UNMANAGED_LATEX_DEPENDENCY"
                if reason is LatexCompilationStopReason.UNMANAGED_DEPENDENCY
                else "LATEX_CONTENT_COMPILATION_ERROR"
            ),
        )
        return MaterialCorrectionTarget.create(
            item=item,
            run=run,
            stage_result=stage_result,
            source_record_id=lineage.construction_result_id,
            source_version=lineage.source_contract_version,
            source_content_hash=lineage.source_content_hash,
            statement_id="latex-compilation-correction",
            payload=payload,
            created_at=now,
        )

    def _resume_layout_target(self, item, run, stage_result, now):
        lineage = (
            _publication_lineage(stage_result)
            if item.source_stage
            is ApplicationPreparationStage.RESUME_PUBLICATION
            else None
        )
        if lineage is not None and lineage.source_kind is (
            PublicationStoppedSourceKind.VISUAL_QA_DIRECTIVE
        ):
            if (
                lineage.source_artifact_id is None
                or lineage.source_artifact_version is None
                or lineage.source_artifact_content_hash is None
            ):
                raise ValueError("Visual QA source identity is incomplete")
            compilation = self._compilation_record(
                item.subject_id, lineage.source_artifact_id
            )
            if (
                compilation.pdf_sha256
                != lineage.source_artifact_content_hash
                or compilation.latex_version_id
                != lineage.source_artifact_version
            ):
                raise ValueError("Visual QA artifact identity drifted")
            payload = ResumeVisualLayoutCorrectionTarget(
                origin_kind=(
                    ResumeVisualLayoutOriginKind
                    .PUBLICATION_VISUAL_QA_DIRECTIVE
                ),
                source_result_id=lineage.source_result_id,
                artifact_id=compilation.record_id,
                artifact_version=compilation.latex_version_id,
                artifact_content_hash=compilation.pdf_sha256,
                latex_source_id=compilation.latex_version_id,
                latex_source_content_hash=compilation.latex_source_sha256,
                final_attempt_id=None,
                attempt_count=None,
                attempt_limit=None,
                safe_preview_reference=None,
            )
            return MaterialCorrectionTarget.create(
                item=item,
                run=run,
                stage_result=stage_result,
                source_record_id=compilation.record_id,
                source_version=compilation.contract_version,
                source_content_hash=compilation.pdf_sha256,
                statement_id="resume-visual-correction",
                payload=payload,
                created_at=now,
            )
        layout_id = (
            lineage.source_result_id
            if lineage is not None
            else stage_result.result_id
        )
        if not layout_id:
            raise ValueError("Layout result identity is unavailable")
        layout = self._layout_run(item.subject_id, layout_id)
        if (
            layout.application_plan_id != item.application_plan_id
            or not layout.attempts
            or (
                lineage is not None
                and layout.run_content_hash
                != lineage.source_result_content_hash
            )
        ):
            raise ValueError("Layout target binding drifted")
        attempt = layout.attempts[-1]
        compilation_id = (
            attempt.output_compilation_record_id
            or attempt.input_compilation_record_id
        )
        compilation = self._compilation_record(
            item.subject_id, compilation_id
        )
        latex_version_id = (
            attempt.output_latex_version_id
            or attempt.input_latex_version_id
        )
        if compilation.latex_version_id != latex_version_id:
            raise ValueError("Layout source version drifted")
        payload = ResumeVisualLayoutCorrectionTarget(
            origin_kind=(
                ResumeVisualLayoutOriginKind
                .PUBLICATION_LAYOUT_REVISION_STOP
                if lineage is not None
                else ResumeVisualLayoutOriginKind
                .DIRECT_LAYOUT_ATTEMPTS_EXHAUSTED
            ),
            source_result_id=layout.run_id,
            artifact_id=compilation.record_id,
            artifact_version=compilation.latex_version_id,
            artifact_content_hash=compilation.pdf_sha256,
            latex_source_id=compilation.latex_version_id,
            latex_source_content_hash=compilation.latex_source_sha256,
            final_attempt_id=(
                f"{layout.run_id}:attempt:{attempt.attempt_number}"
            ),
            attempt_count=len(layout.attempts),
            attempt_limit=layout.max_attempts,
            safe_preview_reference=None,
        )
        return MaterialCorrectionTarget.create(
            item=item,
            run=run,
            stage_result=stage_result,
            source_record_id=layout.run_id,
            source_version=layout.contract_version,
            source_content_hash=layout.run_content_hash,
            statement_id="resume-layout-correction",
            payload=payload,
            created_at=now,
        )

    def _cover_layout_target(self, item, run, stage_result, now):
        lineage = _publication_lineage(stage_result)
        if (
            lineage.source_kind
            is not PublicationStoppedSourceKind.COVER_LETTER_LAYOUT_OVERFLOW
            or lineage.source_artifact_id is None
            or lineage.source_artifact_version is None
            or lineage.source_artifact_content_hash is None
        ):
            raise ValueError("Cover Letter overflow lineage is incomplete")
        payload = CoverLetterLayoutCorrectionTarget(
            publication_result_id=lineage.publication_result_id,
            overflow_evaluation_id=lineage.source_result_id,
            latex_source_id=lineage.source_artifact_id,
            source_version=lineage.source_artifact_version,
            source_content_hash=lineage.source_artifact_content_hash,
            overflow_summary=(
                "The current cover letter exceeds the one-page "
                "publication policy."
            ),
            safe_preview_reference=None,
        )
        return MaterialCorrectionTarget.create(
            item=item,
            run=run,
            stage_result=stage_result,
            source_record_id=lineage.source_artifact_id,
            source_version=lineage.source_artifact_version,
            source_content_hash=lineage.source_artifact_content_hash,
            statement_id="cover-letter-layout-correction",
            payload=payload,
            created_at=now,
        )

    def _compilation_record(self, subject_id, record_id):
        read = self.compilation_repository.get(
            subject_id=subject_id, record_id=record_id
        )
        if (
            read.status is not ResumeCompilationReadStatus.FOUND
            or read.record is None
        ):
            raise ValueError("Compilation artifact is unavailable")
        return read.record

    def _layout_run(self, subject_id, run_id):
        read = self.layout_repository.get(
            subject_id=subject_id, run_id=run_id
        )
        if (
            read.status is not ResumeLayoutRevisionReadStatus.FOUND
            or read.run is None
            or read.run.final_status
            not in {
                ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED,
                ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN,
            }
        ):
            raise ValueError("Layout revision result is unavailable")
        return read.run


def _publication_lineage(
    stage_result: ApplicationPreparationStageResult,
) -> PublicationStoppedSourceLineage:
    outputs = {item.key: item.value for item in stage_result.outputs}
    required = {
        "publication_stopped_application_plan_id",
        "publication_stopped_material_kind",
        "publication_stopped_source_contract_version",
        "publication_stopped_source_content_hash",
        "publication_stopped_source_directive",
        "publication_stopped_source_lineage_id",
        "publication_stopped_source_kind",
        "publication_stopped_source_outcome",
        "publication_stopped_source_result_id",
        "publication_stopped_source_stage",
        "publication_stopped_subject_id",
    }
    if not required.issubset(outputs) or stage_result.result_id is None:
        raise ValueError("publication stopped-source lineage is incomplete")
    prefix = "publication-stopped-source-"
    lineage_id = outputs["publication_stopped_source_lineage_id"]
    if not lineage_id.startswith(prefix):
        raise ValueError("publication stopped-source ID is invalid")
    blocker_ids = tuple(
        outputs[key]
        for key in sorted(outputs)
        if key.startswith("publication_stopped_blocker_")
    )
    directive_value = outputs["publication_stopped_source_directive"]
    directive = (
        None
        if directive_value == "STOP_REASON"
        else PublicationBlockingDirective(directive_value)
    )
    stop_reason = None
    if directive is None:
        raise ValueError(
            "correction publication lineage needs a typed directive"
        )
    lineage = PublicationStoppedSourceLineage(
        lineage_id=lineage_id,
        lineage_content_hash=lineage_id[len(prefix):],
        subject_id=outputs["publication_stopped_subject_id"],
        application_plan_id=outputs[
            "publication_stopped_application_plan_id"
        ],
        publication_stage=stage_result.stage,
        publication_result_id=stage_result.result_id,
        material_kind=PublicationMaterialKind(
            outputs["publication_stopped_material_kind"]
        ),
        source_kind=PublicationStoppedSourceKind(
            outputs["publication_stopped_source_kind"]
        ),
        source_stage=ApplicationPreparationStage(
            outputs["publication_stopped_source_stage"]
        ),
        source_result_id=outputs["publication_stopped_source_result_id"],
        source_outcome=outputs["publication_stopped_source_outcome"],
        source_contract_version=outputs[
            "publication_stopped_source_contract_version"
        ],
        source_result_content_hash=outputs[
            "publication_stopped_source_content_hash"
        ],
        source_directive=directive,
        source_stop_reason=stop_reason,
        source_artifact_id=outputs.get(
            "publication_stopped_source_artifact_id"
        ),
        source_artifact_version=outputs.get(
            "publication_stopped_source_artifact_version"
        ),
        source_artifact_content_hash=outputs.get(
            "publication_stopped_source_artifact_hash"
        ),
        blocking_lineage_ids=blocker_ids,
    )
    if stage_result.result_content_hash != lineage.lineage_content_hash:
        raise ValueError("publication lineage hash drifted")
    return lineage


def _safe_target(
    target: MaterialCorrectionTarget,
) -> SafeMaterialCorrectionTarget:
    payload = target.payload
    if isinstance(payload, UnsupportedClaimCorrectionTarget):
        return SafeMaterialCorrectionTarget(
            target.target_id,
            target.kind,
            payload.finding_ref.material_kind.value,
            "Unsupported claim needs correction",
            "Delete or rewrite this unsupported statement.",
            payload.claim_summary,
            None,
        )
    if isinstance(payload, LatexCompilationCorrectionTarget):
        return SafeMaterialCorrectionTarget(
            target.target_id,
            target.kind,
            "RESUME",
            "Resume LaTeX needs correction",
            "Correct the current managed LaTeX source.",
            (
                "The source uses an unsupported dependency."
                if payload.compilation_reason
                is LatexCompilationStopReason.UNMANAGED_DEPENDENCY
                else "The current LaTeX content cannot be compiled."
            ),
            None,
        )
    if isinstance(payload, ResumeVisualLayoutCorrectionTarget):
        return SafeMaterialCorrectionTarget(
            target.target_id,
            target.kind,
            "RESUME",
            "Resume layout needs correction",
            "Adjust the current resume layout or source content.",
            "The current resume visual/layout result remains blocking.",
            payload.safe_preview_reference,
            payload.attempt_count,
            payload.attempt_limit,
        )
    return SafeMaterialCorrectionTarget(
        target.target_id,
        target.kind,
        "COVER_LETTER",
        "Cover Letter layout needs correction",
        "Shorten or adjust the current Cover Letter source.",
        payload.overflow_summary,
        payload.safe_preview_reference,
    )


def _target_from_dict(value: Mapping[str, Any]) -> MaterialCorrectionTarget:
    expected = {
        "application_plan_id",
        "attention_item_id",
        "created_at",
        "origin_stage",
        "origin_stage_result_id",
        "origin_stop_reason",
        "payload",
        "preparation_run_id",
        "source_content_hash",
        "source_record_id",
        "source_version",
        "statement_id",
        "statement_version",
        "subject_id",
        "target_hash",
        "target_id",
        "target_version",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted material correction target is invalid")
    payload_value = value["payload"]
    if not isinstance(payload_value, Mapping):
        raise ValueError("persisted correction target payload is invalid")
    kind = MaterialCorrectionTargetKind(payload_value.get("kind"))
    if kind is MaterialCorrectionTargetKind.UNSUPPORTED_CLAIM:
        payload = UnsupportedClaimCorrectionTarget(
            finding_ref=FactQAFindingAttentionRef.from_dict(
                payload_value["finding_ref"]
            ),
            claim_summary=payload_value["claim_summary"],
            evidence_support_status=payload_value[
                "evidence_support_status"
            ],
        )
    elif kind is MaterialCorrectionTargetKind.LATEX_COMPILATION:
        from .application_preparation_orchestrator import (
            ResumeCompilationStoppedSourceRef,
        )

        payload = LatexCompilationCorrectionTarget(
            stopped_source_ref=ResumeCompilationStoppedSourceRef.from_dict(
                payload_value["stopped_source_ref"]
            ),
            construction_result_id=payload_value[
                "construction_result_id"
            ],
            latex_version_id=payload_value["latex_version_id"],
            source_content_hash=payload_value["source_content_hash"],
            compilation_attempt_id=payload_value[
                "compilation_attempt_id"
            ],
            compilation_reason=payload_value["compilation_reason"],
            diagnostic_category=payload_value["diagnostic_category"],
        )
    elif kind is MaterialCorrectionTargetKind.RESUME_VISUAL_LAYOUT:
        payload = ResumeVisualLayoutCorrectionTarget(
            origin_kind=payload_value["origin_kind"],
            source_result_id=payload_value["source_result_id"],
            artifact_id=payload_value["artifact_id"],
            artifact_version=payload_value["artifact_version"],
            artifact_content_hash=payload_value[
                "artifact_content_hash"
            ],
            latex_source_id=payload_value["latex_source_id"],
            latex_source_content_hash=payload_value[
                "latex_source_content_hash"
            ],
            final_attempt_id=payload_value["final_attempt_id"],
            attempt_count=payload_value["attempt_count"],
            attempt_limit=payload_value["attempt_limit"],
            safe_preview_reference=payload_value[
                "safe_preview_reference"
            ],
        )
    else:
        payload = CoverLetterLayoutCorrectionTarget(
            publication_result_id=payload_value["publication_result_id"],
            overflow_evaluation_id=payload_value[
                "overflow_evaluation_id"
            ],
            latex_source_id=payload_value["latex_source_id"],
            source_version=payload_value["source_version"],
            source_content_hash=payload_value["source_content_hash"],
            overflow_summary=payload_value["overflow_summary"],
            safe_preview_reference=payload_value[
                "safe_preview_reference"
            ],
        )
    return MaterialCorrectionTarget(
        target_id=value["target_id"],
        target_version=value["target_version"],
        target_hash=value["target_hash"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        preparation_run_id=value["preparation_run_id"],
        attention_item_id=value["attention_item_id"],
        origin_stage=value["origin_stage"],
        origin_stage_result_id=value["origin_stage_result_id"],
        origin_stop_reason=PreparationStopReasonEnvelope.from_dict(
            value["origin_stop_reason"]
        ),
        source_record_id=value["source_record_id"],
        source_version=value["source_version"],
        source_content_hash=value["source_content_hash"],
        statement_id=value["statement_id"],
        statement_version=value["statement_version"],
        payload=payload,
        created_at=_parse_time(value["created_at"]),
    )


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
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
    return _aware(
        "created_at", datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _safe_reference(value: str) -> str:
    candidate = _text("safe_preview_reference", value, 300)
    lowered = candidate.casefold()
    if (
        candidate.startswith("/")
        or "://" in candidate
        or any(
            marker in lowered
            for marker in (
                "token",
                "credential",
                "permit",
                "private",
                "users/",
            )
        )
    ):
        raise ValueError("preview reference is not UI-safe")
    return candidate


__all__ = [
    "CORRECT_MATERIAL_TARGET_KIND_REGISTRY",
    "CoverLetterLayoutCorrectionTarget",
    "LatexCompilationCorrectionTarget",
    "MaterialCorrectionTarget",
    "MaterialCorrectionTargetKind",
    "MaterialCorrectionTargetProvider",
    "MaterialCorrectionTargetReadResult",
    "MaterialCorrectionTargetReadStatus",
    "MaterialCorrectionTargetResult",
    "MaterialCorrectionTargetStatus",
    "MaterialCorrectionTypedTargetResult",
    "MaterialCorrectionTargetWriteResult",
    "MaterialCorrectionTargetWriteStatus",
    "PrivateHomeMaterialCorrectionTargetRepository",
    "ResumeVisualLayoutCorrectionTarget",
    "SafeMaterialCorrectionTarget",
    "UnsupportedClaimCorrectionTarget",
]
