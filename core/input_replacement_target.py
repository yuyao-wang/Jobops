"""Immutable typed targets for current REPLACE_INPUT attention items."""

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
    LatexConstructionStopReason,
    PreparationStopReasonEnvelope,
    SourceResumeProjectionStopReason,
)
from .human_attention_queue import (
    HumanAttentionQueueItem,
    HumanAttentionResolutionCapability,
)
from .input_replacement_ref import (
    INPUT_REPLACEMENT_TARGET_CONTRACT_VERSION,
    InputReplacementTargetRef,
)
from .private_home import PrivateHome, PrivateHomeError
from .resume_candidates import (
    ResumeArtifactType,
    ResumeCandidate,
    ResumeCandidateProvider,
    ResumeCandidateReadStatus,
)
from .resume_latex_versions import (
    RESUME_LATEX_VERSION_CONTRACT_VERSION,
    ResumeLatexVersionListStatus,
    ResumeLatexVersionProvider,
)


INPUT_REPLACEMENT_STATEMENT_VERSION = "input-replacement-statement-v1"
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_TARGET_ID_RE = re.compile(r"^input-replacement-target-[a-f0-9]{64}$")


class InputReplacementTargetKind(StrEnum):
    SOURCE_RESUME = "SOURCE_RESUME"
    BASE_LATEX_VERSION = "BASE_LATEX_VERSION"


class InputReplacementMethod(StrEnum):
    SELECT_EXISTING_RESUME_CANDIDATE = (
        "SELECT_EXISTING_RESUME_CANDIDATE"
    )
    REGISTER_NEW_RESUME_CANDIDATE = "REGISTER_NEW_RESUME_CANDIDATE"
    SELECT_EXISTING_LATEX_VERSION = "SELECT_EXISTING_LATEX_VERSION"
    REGISTER_NEW_LATEX_VERSION = "REGISTER_NEW_LATEX_VERSION"


class InputReplacementTargetStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    ITEM_NOT_CURRENT = "ITEM_NOT_CURRENT"
    NOT_REPLACEABLE = "NOT_REPLACEABLE"
    TARGET_STALE = "TARGET_STALE"
    TARGET_INCOMPLETE = "TARGET_INCOMPLETE"
    FAILED = "FAILED"


class InputReplacementTargetWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class InputReplacementTargetReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


def _text(name: str, value: Any, maximum: int = 300) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is invalid")
    return cleaned


def _hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return (
        _aware("timestamp", value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
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


@dataclass(frozen=True, slots=True)
class SourceResumeReplacementTarget:
    resume_candidate_id: str
    candidate_version: str
    source_record_id: str
    source_content_hash: str
    source_media_type: str
    display_name: str
    unreadable_lineage_id: str
    allowed_replacement_methods: tuple[InputReplacementMethod, ...]
    kind: InputReplacementTargetKind = (
        InputReplacementTargetKind.SOURCE_RESUME
    )

    def __post_init__(self) -> None:
        for name in (
            "resume_candidate_id",
            "candidate_version",
            "source_record_id",
            "source_media_type",
            "display_name",
            "unreadable_lineage_id",
        ):
            _text(name, getattr(self, name))
        _hash("source_content_hash", self.source_content_hash)
        methods = tuple(
            InputReplacementMethod(item)
            for item in self.allowed_replacement_methods
        )
        if methods != (
            InputReplacementMethod.SELECT_EXISTING_RESUME_CANDIDATE,
            InputReplacementMethod.REGISTER_NEW_RESUME_CANDIDATE,
        ):
            raise ValueError("Resume replacement methods are invalid")
        if self.source_record_id != self.resume_candidate_id:
            raise ValueError("Resume source record binding is invalid")
        object.__setattr__(self, "allowed_replacement_methods", methods)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_replacement_methods": [
                item.value for item in self.allowed_replacement_methods
            ],
            "candidate_version": self.candidate_version,
            "display_name": self.display_name,
            "kind": self.kind.value,
            "resume_candidate_id": self.resume_candidate_id,
            "source_content_hash": self.source_content_hash,
            "source_media_type": self.source_media_type,
            "source_record_id": self.source_record_id,
            "unreadable_lineage_id": self.unreadable_lineage_id,
        }


@dataclass(frozen=True, slots=True)
class BaseLatexVersionReplacementTarget:
    latex_version_id: str
    version_family: str
    source_record_id: str
    source_content_hash: str
    display_name: str
    unreadable_lineage_id: str
    allowed_replacement_methods: tuple[InputReplacementMethod, ...]
    kind: InputReplacementTargetKind = (
        InputReplacementTargetKind.BASE_LATEX_VERSION
    )

    def __post_init__(self) -> None:
        for name in (
            "latex_version_id",
            "version_family",
            "source_record_id",
            "display_name",
            "unreadable_lineage_id",
        ):
            _text(name, getattr(self, name))
        _hash("source_content_hash", self.source_content_hash)
        methods = tuple(
            InputReplacementMethod(item)
            for item in self.allowed_replacement_methods
        )
        if methods != (
            InputReplacementMethod.SELECT_EXISTING_LATEX_VERSION,
            InputReplacementMethod.REGISTER_NEW_LATEX_VERSION,
        ):
            raise ValueError("LaTeX replacement methods are invalid")
        if self.source_record_id != self.latex_version_id:
            raise ValueError("LaTeX source record binding is invalid")
        object.__setattr__(self, "allowed_replacement_methods", methods)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_replacement_methods": [
                item.value for item in self.allowed_replacement_methods
            ],
            "display_name": self.display_name,
            "kind": self.kind.value,
            "latex_version_id": self.latex_version_id,
            "source_content_hash": self.source_content_hash,
            "source_record_id": self.source_record_id,
            "unreadable_lineage_id": self.unreadable_lineage_id,
            "version_family": self.version_family,
        }


InputReplacementPayload = (
    SourceResumeReplacementTarget | BaseLatexVersionReplacementTarget
)


@dataclass(frozen=True, slots=True)
class InputReplacementTarget:
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
    target_kind: InputReplacementTargetKind
    current_input_record_id: str
    current_input_version: str
    current_input_content_hash: str
    replacement_statement_id: str
    replacement_statement_version: str
    payload: InputReplacementPayload
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            self.target_version
            != INPUT_REPLACEMENT_TARGET_CONTRACT_VERSION
            or self.replacement_statement_version
            != INPUT_REPLACEMENT_STATEMENT_VERSION
        ):
            raise ValueError("input replacement target version is unsupported")
        for name in (
            "subject_id",
            "application_plan_id",
            "preparation_run_id",
            "attention_item_id",
            "origin_stage_result_id",
            "current_input_record_id",
            "current_input_version",
            "replacement_statement_id",
        ):
            _text(name, getattr(self, name))
        _hash("current_input_content_hash", self.current_input_content_hash)
        stage = ApplicationPreparationStage(self.origin_stage)
        kind = InputReplacementTargetKind(self.target_kind)
        if (
            not isinstance(
                self.origin_stop_reason, PreparationStopReasonEnvelope
            )
            or self.origin_stop_reason.stage is not stage
            or (
                kind is InputReplacementTargetKind.SOURCE_RESUME
                and not isinstance(
                    self.payload, SourceResumeReplacementTarget
                )
            )
            or (
                kind is InputReplacementTargetKind.BASE_LATEX_VERSION
                and not isinstance(
                    self.payload, BaseLatexVersionReplacementTarget
                )
            )
            or self.payload.source_record_id
            != self.current_input_record_id
            or self.payload.source_content_hash
            != self.current_input_content_hash
        ):
            raise ValueError("input replacement target binding is invalid")
        object.__setattr__(self, "origin_stage", stage)
        object.__setattr__(self, "target_kind", kind)
        _aware("created_at", self.created_at)
        digest = _canonical_hash(self.identity_dict())
        if (
            not isinstance(self.target_id, str)
            or _TARGET_ID_RE.fullmatch(self.target_id) is None
            or self.target_hash != digest
            or self.target_id != f"input-replacement-target-{digest}"
        ):
            raise ValueError("input replacement target identity is invalid")

    @property
    def reference(self) -> InputReplacementTargetRef:
        return InputReplacementTargetRef(
            self.target_id, self.target_version, self.target_hash
        )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "current_input_content_hash": self.current_input_content_hash,
            "current_input_record_id": self.current_input_record_id,
            "current_input_version": self.current_input_version,
            "origin_stage": self.origin_stage.value,
            "origin_stage_result_id": self.origin_stage_result_id,
            "origin_stop_reason": self.origin_stop_reason.to_dict(),
            "payload": self.payload.to_dict(),
            "preparation_run_id": self.preparation_run_id,
            "replacement_statement_id": self.replacement_statement_id,
            "replacement_statement_version": (
                self.replacement_statement_version
            ),
            "subject_id": self.subject_id,
            "target_kind": self.target_kind.value,
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
        payload: InputReplacementPayload,
        current_input_version: str,
        statement_id: str,
        created_at: datetime,
    ) -> "InputReplacementTarget":
        prototype = {
            "application_plan_id": item.application_plan_id,
            "attention_item_id": item.item_id,
            "current_input_content_hash": payload.source_content_hash,
            "current_input_record_id": payload.source_record_id,
            "current_input_version": current_input_version,
            "origin_stage": item.source_stage.value,
            "origin_stage_result_id": item.source_record_id,
            "origin_stop_reason": stage_result.stop_reason.to_dict(),
            "payload": payload.to_dict(),
            "preparation_run_id": run.run_id,
            "replacement_statement_id": statement_id,
            "replacement_statement_version": (
                INPUT_REPLACEMENT_STATEMENT_VERSION
            ),
            "subject_id": item.subject_id,
            "target_kind": payload.kind.value,
            "target_version": INPUT_REPLACEMENT_TARGET_CONTRACT_VERSION,
        }
        digest = _canonical_hash(prototype)
        return cls(
            target_id=f"input-replacement-target-{digest}",
            target_version=INPUT_REPLACEMENT_TARGET_CONTRACT_VERSION,
            target_hash=digest,
            subject_id=item.subject_id,
            application_plan_id=item.application_plan_id,
            preparation_run_id=run.run_id,
            attention_item_id=item.item_id,
            origin_stage=item.source_stage,
            origin_stage_result_id=item.source_record_id,
            origin_stop_reason=stage_result.stop_reason,
            target_kind=payload.kind,
            current_input_record_id=payload.source_record_id,
            current_input_version=current_input_version,
            current_input_content_hash=payload.source_content_hash,
            replacement_statement_id=statement_id,
            replacement_statement_version=(
                INPUT_REPLACEMENT_STATEMENT_VERSION
            ),
            payload=payload,
            created_at=created_at,
        )


REPLACE_INPUT_TARGET_KIND_REGISTRY = {
    (
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
        SourceResumeProjectionStopReason.FORMAT_UNSUPPORTED,
    ): InputReplacementTargetKind.SOURCE_RESUME,
    (
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
        SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE,
    ): InputReplacementTargetKind.SOURCE_RESUME,
    (
        ApplicationPreparationStage.LATEX_CONSTRUCTION,
        LatexConstructionStopReason.BASE_VERSION_UNREADABLE,
    ): InputReplacementTargetKind.BASE_LATEX_VERSION,
}


@dataclass(frozen=True, slots=True)
class InputReplacementTargetWriteResult:
    status: InputReplacementTargetWriteStatus
    target: InputReplacementTarget | None


@dataclass(frozen=True, slots=True)
class InputReplacementTargetReadResult:
    status: InputReplacementTargetReadStatus
    target: InputReplacementTarget | None


@dataclass(frozen=True, slots=True)
class SafeInputReplacementTarget:
    target_id: str
    target_kind: InputReplacementTargetKind
    input_kind: str
    display_name: str
    version: str
    media_type: str | None
    required_action: str
    replacement_methods: tuple[InputReplacementMethod, ...]


@dataclass(frozen=True, slots=True)
class InputReplacementTargetResult:
    status: InputReplacementTargetStatus
    safe_target: SafeInputReplacementTarget | None


@runtime_checkable
class InputReplacementTargetRepository(Protocol):
    def save(
        self, target: InputReplacementTarget
    ) -> InputReplacementTargetWriteResult: ...

    def get(
        self, *, subject_id: str, target_id: str
    ) -> InputReplacementTargetReadResult: ...


class PrivateHomeInputReplacementTargetRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _path(self, subject_id: str, target_id: str) -> Path:
        subject = _text("subject_id", subject_id, 160)
        if (
            not isinstance(target_id, str)
            or _TARGET_ID_RE.fullmatch(target_id) is None
        ):
            raise ValueError("input replacement target ID is invalid")
        return (
            self._home.paths.input_replacement_targets
            / ("subject-" + hashlib.sha256(subject.encode()).hexdigest())
            / f"{target_id}.json"
        )

    def save(
        self, target: InputReplacementTarget
    ) -> InputReplacementTargetWriteResult:
        if not isinstance(target, InputReplacementTarget):
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
                return InputReplacementTargetWriteResult(
                    InputReplacementTargetWriteStatus.FAILED, None
                )
            if created:
                return InputReplacementTargetWriteResult(
                    InputReplacementTargetWriteStatus.CREATED, target
                )
            read = self.get(
                subject_id=target.subject_id, target_id=target.target_id
            )
            if (
                read.status is InputReplacementTargetReadStatus.FOUND
                and read.target is not None
                and read.target.identity_dict() == target.identity_dict()
            ):
                return InputReplacementTargetWriteResult(
                    InputReplacementTargetWriteStatus.UNCHANGED, read.target
                )
            return InputReplacementTargetWriteResult(
                InputReplacementTargetWriteStatus.FAILED, None
            )

    def get(
        self, *, subject_id: str, target_id: str
    ) -> InputReplacementTargetReadResult:
        path = self._path(subject_id, target_id)
        with self._lock:
            if not path.exists():
                return InputReplacementTargetReadResult(
                    InputReplacementTargetReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return InputReplacementTargetReadResult(
                    InputReplacementTargetReadStatus.INTEGRITY_FAILURE, None
                )
            try:
                target = _target_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return InputReplacementTargetReadResult(
                    InputReplacementTargetReadStatus.INTEGRITY_FAILURE, None
                )
            if (
                target.subject_id != subject_id.strip()
                or target.target_id != target_id
            ):
                return InputReplacementTargetReadResult(
                    InputReplacementTargetReadStatus.INTEGRITY_FAILURE, None
                )
            return InputReplacementTargetReadResult(
                InputReplacementTargetReadStatus.FOUND, target
            )


CurrentAttentionItemReader = Callable[
    [str, str], HumanAttentionQueueItem | None
]


@dataclass(slots=True)
class InputReplacementTargetProvider:
    repository: InputReplacementTargetRepository
    resume_candidate_provider: ResumeCandidateProvider
    latex_version_provider: ResumeLatexVersionProvider
    current_item_reader: CurrentAttentionItemReader | None = None

    def get_current_ref(
        self,
        *,
        item: HumanAttentionQueueItem,
        run: ApplicationPreparationRun,
        stage_result: ApplicationPreparationStageResult,
        now: datetime,
    ) -> InputReplacementTargetRef | None:
        try:
            target = self._project(item, run, stage_result, now)
            write = self.repository.save(target)
            if (
                write.status is InputReplacementTargetWriteStatus.FAILED
                or write.target is None
            ):
                return None
            return write.target.reference
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    def get_current_input_replacement_target(
        self, *, subject_id: str, attention_item_id: str
    ) -> InputReplacementTargetResult:
        if self.current_item_reader is None:
            return InputReplacementTargetResult(
                InputReplacementTargetStatus.FAILED, None
            )
        try:
            item = self.current_item_reader(subject_id, attention_item_id)
            if item is None:
                return InputReplacementTargetResult(
                    InputReplacementTargetStatus.ITEM_NOT_CURRENT, None
                )
            if (
                item.subject_id != subject_id
                or item.resolution_capability
                is not HumanAttentionResolutionCapability.REPLACE_INPUT
            ):
                return InputReplacementTargetResult(
                    InputReplacementTargetStatus.NOT_REPLACEABLE, None
                )
            reference = item.replacement_target_ref
            if reference is None:
                return InputReplacementTargetResult(
                    InputReplacementTargetStatus.TARGET_INCOMPLETE, None
                )
            read = self.repository.get(
                subject_id=subject_id, target_id=reference.target_id
            )
            if (
                read.status is not InputReplacementTargetReadStatus.FOUND
                or read.target is None
            ):
                return InputReplacementTargetResult(
                    InputReplacementTargetStatus.TARGET_STALE, None
                )
            target = read.target
            if (
                target.reference != reference
                or target.attention_item_id != item.item_id
                or target.application_plan_id != item.application_plan_id
                or target.preparation_run_id
                != item.source_preparation_run_id
                or target.origin_stage is not item.source_stage
                or target.origin_stage_result_id != item.source_record_id
                or target.origin_stop_reason.code.value
                != item.source_reason_code
                or not self._input_is_current(target)
            ):
                return InputReplacementTargetResult(
                    InputReplacementTargetStatus.TARGET_STALE, None
                )
            return InputReplacementTargetResult(
                InputReplacementTargetStatus.AVAILABLE,
                _safe_target(target),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return InputReplacementTargetResult(
                InputReplacementTargetStatus.FAILED, None
            )

    def get_current_typed_target(
        self, *, item: HumanAttentionQueueItem
    ) -> InputReplacementTarget | None:
        """Return the exact item-bound target for a resolution service."""

        reference = item.replacement_target_ref
        if (
            item.resolution_capability
            is not HumanAttentionResolutionCapability.REPLACE_INPUT
            or reference is None
        ):
            return None
        read = self.repository.get(
            subject_id=item.subject_id, target_id=reference.target_id
        )
        if (
            read.status is not InputReplacementTargetReadStatus.FOUND
            or read.target is None
            or read.target.reference != reference
            or read.target.attention_item_id != item.item_id
            or read.target.subject_id != item.subject_id
            or read.target.application_plan_id != item.application_plan_id
            or read.target.preparation_run_id
            != item.source_preparation_run_id
            or read.target.origin_stage is not item.source_stage
            or read.target.origin_stage_result_id != item.source_record_id
            or read.target.origin_stop_reason.code.value
            != item.source_reason_code
        ):
            return None
        return read.target

    def _project(self, item, run, stage_result, now):
        if (
            item.resolution_capability
            is not HumanAttentionResolutionCapability.REPLACE_INPUT
            or stage_result.stop_reason is None
            or stage_result.stop_reason.code.value
            != item.source_reason_code
        ):
            raise ValueError("replacement item lineage is invalid")
        kind = REPLACE_INPUT_TARGET_KIND_REGISTRY.get(
            (item.source_stage, stage_result.stop_reason.code)
        )
        if kind is InputReplacementTargetKind.SOURCE_RESUME:
            return self._resume_target(item, run, stage_result, now)
        if kind is InputReplacementTargetKind.BASE_LATEX_VERSION:
            return self._latex_target(item, run, stage_result, now)
        raise ValueError("replaceable reason has no target kind")

    def _resume_target(self, item, run, stage_result, now):
        selected = _stage(
            run, ApplicationPreparationStage.BASE_RESUME_SELECTION
        )
        values = _outputs(selected)
        resume_id = values.get("resume_id")
        artifact_hash = values.get("resume_artifact_sha256")
        if resume_id is None:
            raise ValueError("selected ResumeCandidate lineage is incomplete")
        read = self.resume_candidate_provider.get(
            subject_id=item.subject_id, resume_id=resume_id
        )
        if (
            read.status is not ResumeCandidateReadStatus.FOUND
            or not isinstance(read.candidate, ResumeCandidate)
            or read.candidate.subject_id != item.subject_id
            or (
                artifact_hash is not None
                and read.candidate.artifact_sha256 != artifact_hash
            )
        ):
            raise ValueError("selected ResumeCandidate is unavailable")
        candidate = read.candidate
        payload = SourceResumeReplacementTarget(
            resume_candidate_id=candidate.resume_id,
            candidate_version=candidate.contract_version,
            source_record_id=candidate.resume_id,
            source_content_hash=candidate.artifact_sha256,
            source_media_type={
                ResumeArtifactType.PDF: "application/pdf",
                ResumeArtifactType.DOCX: (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
            }[candidate.artifact_type],
            display_name=candidate.display_name,
            unreadable_lineage_id=stage_result.stage_content_hash,
            allowed_replacement_methods=(
                InputReplacementMethod.SELECT_EXISTING_RESUME_CANDIDATE,
                InputReplacementMethod.REGISTER_NEW_RESUME_CANDIDATE,
            ),
        )
        return InputReplacementTarget.create(
            item=item,
            run=run,
            stage_result=stage_result,
            payload=payload,
            current_input_version=candidate.contract_version,
            statement_id="replace-unreadable-source-resume",
            created_at=now,
        )

    def _latex_target(self, item, run, stage_result, now):
        selected = _stage(run, ApplicationPreparationStage.BASE_LATEX_SELECTION)
        values = _outputs(selected)
        version_id = values.get("selected_latex_version_id")
        source_hash = values.get("selected_latex_source_sha256")
        family_id = values.get("selected_root_family_id")
        if version_id is None or source_hash is None or family_id is None:
            raise ValueError("selected LaTeX Version lineage is incomplete")
        listed = self.latex_version_provider.list_selectable(item.subject_id)
        if listed.status is ResumeLatexVersionListStatus.SUCCEEDED:
            current = next(
                (
                    version
                    for version in listed.versions
                    if version.latex_version_id == version_id
                ),
                None,
            )
            if current is not None and (
                current.source_sha256 != source_hash
                or current.root_family_id != family_id
            ):
                raise ValueError("selected LaTeX Version identity drifted")
        payload = BaseLatexVersionReplacementTarget(
            latex_version_id=version_id,
            version_family=family_id,
            source_record_id=version_id,
            source_content_hash=source_hash,
            display_name="Selected Base LaTeX Version",
            unreadable_lineage_id=stage_result.stage_content_hash,
            allowed_replacement_methods=(
                InputReplacementMethod.SELECT_EXISTING_LATEX_VERSION,
                InputReplacementMethod.REGISTER_NEW_LATEX_VERSION,
            ),
        )
        return InputReplacementTarget.create(
            item=item,
            run=run,
            stage_result=stage_result,
            payload=payload,
            current_input_version=RESUME_LATEX_VERSION_CONTRACT_VERSION,
            statement_id="replace-unreadable-base-latex-version",
            created_at=now,
        )

    def _input_is_current(self, target: InputReplacementTarget) -> bool:
        if isinstance(target.payload, SourceResumeReplacementTarget):
            read = self.resume_candidate_provider.get(
                subject_id=target.subject_id,
                resume_id=target.payload.resume_candidate_id,
            )
            return (
                read.status is ResumeCandidateReadStatus.FOUND
                and read.candidate is not None
                and read.candidate.artifact_sha256
                == target.current_input_content_hash
                and read.candidate.contract_version
                == target.current_input_version
            )
        listed = self.latex_version_provider.list_selectable(target.subject_id)
        if listed.status is not ResumeLatexVersionListStatus.SUCCEEDED:
            return True
        current = next(
            (
                version
                for version in listed.versions
                if version.latex_version_id
                == target.payload.latex_version_id
            ),
            None,
        )
        return current is None or (
            current.source_sha256 == target.current_input_content_hash
            and current.root_family_id == target.payload.version_family
        )


def _stage(
    run: ApplicationPreparationRun, stage: ApplicationPreparationStage
) -> ApplicationPreparationStageResult:
    value = next((item for item in run.stage_results if item.stage is stage), None)
    if value is None:
        raise ValueError("upstream selection stage is unavailable")
    return value


def _outputs(stage: ApplicationPreparationStageResult) -> dict[str, str]:
    return {item.key: item.value for item in stage.outputs}


def _safe_target(target: InputReplacementTarget) -> SafeInputReplacementTarget:
    payload = target.payload
    if isinstance(payload, SourceResumeReplacementTarget):
        return SafeInputReplacementTarget(
            target_id=target.target_id,
            target_kind=target.target_kind,
            input_kind="ResumeCandidate",
            display_name=payload.display_name,
            version=payload.resume_candidate_id,
            media_type=payload.source_media_type,
            required_action=(
                "Select another registered ResumeCandidate or register a new "
                "supported resume before preparation continues."
            ),
            replacement_methods=payload.allowed_replacement_methods,
        )
    return SafeInputReplacementTarget(
        target_id=target.target_id,
        target_kind=target.target_kind,
        input_kind="Base LaTeX Version",
        display_name=payload.display_name,
        version=payload.latex_version_id,
        media_type="text/x-tex",
        required_action=(
            "Select another registered LaTeX Version or register a new "
            "readable version before preparation continues."
        ),
        replacement_methods=payload.allowed_replacement_methods,
    )


def _target_from_dict(value: Mapping[str, Any]) -> InputReplacementTarget:
    expected = {
        "application_plan_id",
        "attention_item_id",
        "created_at",
        "current_input_content_hash",
        "current_input_record_id",
        "current_input_version",
        "origin_stage",
        "origin_stage_result_id",
        "origin_stop_reason",
        "payload",
        "preparation_run_id",
        "replacement_statement_id",
        "replacement_statement_version",
        "subject_id",
        "target_hash",
        "target_id",
        "target_kind",
        "target_version",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("input replacement target is invalid")
    payload_value = value["payload"]
    if not isinstance(payload_value, Mapping):
        raise ValueError("input replacement payload is invalid")
    kind = InputReplacementTargetKind(payload_value["kind"])
    if kind is InputReplacementTargetKind.SOURCE_RESUME:
        if set(payload_value) != {
            "allowed_replacement_methods",
            "candidate_version",
            "display_name",
            "kind",
            "resume_candidate_id",
            "source_content_hash",
            "source_media_type",
            "source_record_id",
            "unreadable_lineage_id",
        }:
            raise ValueError("Resume replacement payload is invalid")
        payload: InputReplacementPayload = SourceResumeReplacementTarget(
            resume_candidate_id=payload_value["resume_candidate_id"],
            candidate_version=payload_value["candidate_version"],
            source_record_id=payload_value["source_record_id"],
            source_content_hash=payload_value["source_content_hash"],
            source_media_type=payload_value["source_media_type"],
            display_name=payload_value["display_name"],
            unreadable_lineage_id=payload_value["unreadable_lineage_id"],
            allowed_replacement_methods=tuple(
                payload_value["allowed_replacement_methods"]
            ),
        )
    else:
        if set(payload_value) != {
            "allowed_replacement_methods",
            "display_name",
            "kind",
            "latex_version_id",
            "source_content_hash",
            "source_record_id",
            "unreadable_lineage_id",
            "version_family",
        }:
            raise ValueError("LaTeX replacement payload is invalid")
        payload = BaseLatexVersionReplacementTarget(
            latex_version_id=payload_value["latex_version_id"],
            version_family=payload_value["version_family"],
            source_record_id=payload_value["source_record_id"],
            source_content_hash=payload_value["source_content_hash"],
            display_name=payload_value["display_name"],
            unreadable_lineage_id=payload_value["unreadable_lineage_id"],
            allowed_replacement_methods=tuple(
                payload_value["allowed_replacement_methods"]
            ),
        )
    return InputReplacementTarget(
        target_id=value["target_id"],
        target_version=value["target_version"],
        target_hash=value["target_hash"],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        preparation_run_id=value["preparation_run_id"],
        attention_item_id=value["attention_item_id"],
        origin_stage=ApplicationPreparationStage(value["origin_stage"]),
        origin_stage_result_id=value["origin_stage_result_id"],
        origin_stop_reason=PreparationStopReasonEnvelope.from_dict(
            value["origin_stop_reason"]
        ),
        target_kind=kind,
        current_input_record_id=value["current_input_record_id"],
        current_input_version=value["current_input_version"],
        current_input_content_hash=value["current_input_content_hash"],
        replacement_statement_id=value["replacement_statement_id"],
        replacement_statement_version=value[
            "replacement_statement_version"
        ],
        payload=payload,
        created_at=datetime.fromisoformat(
            value["created_at"].replace("Z", "+00:00")
        ),
    )


__all__ = [
    "BaseLatexVersionReplacementTarget",
    "INPUT_REPLACEMENT_STATEMENT_VERSION",
    "InputReplacementMethod",
    "InputReplacementTarget",
    "InputReplacementTargetKind",
    "InputReplacementTargetProvider",
    "InputReplacementTargetReadResult",
    "InputReplacementTargetReadStatus",
    "InputReplacementTargetRepository",
    "InputReplacementTargetResult",
    "InputReplacementTargetStatus",
    "InputReplacementTargetWriteResult",
    "InputReplacementTargetWriteStatus",
    "PrivateHomeInputReplacementTargetRepository",
    "REPLACE_INPUT_TARGET_KIND_REGISTRY",
    "SafeInputReplacementTarget",
    "SourceResumeReplacementTarget",
]
