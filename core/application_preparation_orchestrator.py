"""Serial single-plan orchestration over public preparation Slice callables."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .application_plan import (
    ApplicationPlan,
    ApplicationPlanReadStatus,
    ApplicationPlanRepository,
)
from .private_home import PrivateHome, PrivateHomeError


APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION = (
    "single-job-application-preparation-orchestration-v1"
)
REQUIRED_MATERIAL_POLICY_ID = "required-application-materials-v1"
REQUIRED_MATERIAL_POLICY_VERSION = "required-application-materials-v1"

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_RUN_ID_RE = re.compile(r"^application-preparation-run-[a-f0-9]{64}$")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _clean_text(name: str, value: Any, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _require_aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return (
        _require_aware("timestamp", value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_time(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is invalid")
    return _require_aware(
        name, datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


class ApplicationPreparationStage(StrEnum):
    BASE_RESUME_SELECTION = "BASE_RESUME_SELECTION"
    SOURCE_RESUME_PROJECTION = "SOURCE_RESUME_PROJECTION"
    RESUME_EVIDENCE = "RESUME_EVIDENCE"
    RESUME_TAILORING = "RESUME_TAILORING"
    RESUME_FACT_QA = "RESUME_FACT_QA"
    BASE_LATEX_SELECTION = "BASE_LATEX_SELECTION"
    LATEX_CONSTRUCTION = "LATEX_CONSTRUCTION"
    RESUME_COMPILATION = "RESUME_COMPILATION"
    RESUME_VISUAL_QA = "RESUME_VISUAL_QA"
    RESUME_LAYOUT_REVISION = "RESUME_LAYOUT_REVISION"
    RESUME_PUBLICATION = "RESUME_PUBLICATION"
    RESUME_MANIFEST = "RESUME_MANIFEST"
    COVER_LETTER_EVIDENCE = "COVER_LETTER_EVIDENCE"
    COVER_LETTER_DRAFT = "COVER_LETTER_DRAFT"
    COVER_LETTER_FACT_QA = "COVER_LETTER_FACT_QA"
    COVER_LETTER_PUBLICATION = "COVER_LETTER_PUBLICATION"
    COVER_LETTER_MANIFEST = "COVER_LETTER_MANIFEST"
    APPLICATION_ANSWERS = "APPLICATION_ANSWERS"


APPLICATION_PREPARATION_STAGE_ORDER = tuple(ApplicationPreparationStage)


class PublicStageStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class PublicStageDirective(StrEnum):
    CONTINUE = "CONTINUE"
    PASSED = "PASSED"
    REVISION_REQUIRED = "REVISION_REQUIRED"


class PreparationStageExecutionStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    SKIPPED = "SKIPPED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class ApplicationPreparationRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class ApplicationPreparationStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNCHANGED = "UNCHANGED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


class ApplicationPreparationCompletedRole(StrEnum):
    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"
    APPLICATION_ANSWERS = "APPLICATION_ANSWERS"


class ApplicationPreparationFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    APPLICATION_PLAN_NOT_FOUND = "APPLICATION_PLAN_NOT_FOUND"
    APPLICATION_PLAN_INTEGRITY_FAILURE = (
        "APPLICATION_PLAN_INTEGRITY_FAILURE"
    )
    APPLICATION_PLAN_SUBJECT_MISMATCH = (
        "APPLICATION_PLAN_SUBJECT_MISMATCH"
    )
    ORCHESTRATION_RECIPE_INVALID = "ORCHESTRATION_RECIPE_INVALID"
    PUBLIC_STAGE_CONTRACT_FAILURE = "PUBLIC_STAGE_CONTRACT_FAILURE"
    PUBLIC_STAGE_EXCEPTION = "PUBLIC_STAGE_EXCEPTION"
    RUN_INTEGRITY_FAILURE = "RUN_INTEGRITY_FAILURE"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class RequiredApplicationMaterialPolicy:
    policy_id: str
    policy_version: str
    cover_letter_required: bool
    policy_content_hash: str

    def __post_init__(self) -> None:
        if self.policy_id != REQUIRED_MATERIAL_POLICY_ID:
            raise ValueError("required-material policy ID is unsupported")
        if self.policy_version != REQUIRED_MATERIAL_POLICY_VERSION:
            raise ValueError("required-material policy version is unsupported")
        if self.cover_letter_required is not True:
            raise ValueError("V1 requires a cover letter")
        if self.policy_content_hash != _canonical_hash(
            self.content_dict()
        ):
            raise ValueError("required-material policy hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "cover_letter_required": self.cover_letter_required,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }

    @classmethod
    def v1(cls) -> "RequiredApplicationMaterialPolicy":
        content = {
            "cover_letter_required": True,
            "policy_id": REQUIRED_MATERIAL_POLICY_ID,
            "policy_version": REQUIRED_MATERIAL_POLICY_VERSION,
        }
        return cls(
            policy_id=REQUIRED_MATERIAL_POLICY_ID,
            policy_version=REQUIRED_MATERIAL_POLICY_VERSION,
            cover_letter_required=True,
            policy_content_hash=_canonical_hash(content),
        )


@dataclass(frozen=True, slots=True)
class ApplicationPreparationOutputReference:
    key: str
    value: str

    def __post_init__(self) -> None:
        _clean_text("output key", self.key, 100)
        _clean_text("output value", self.value, 240)

    def to_dict(self) -> dict[str, str]:
        return {"key": self.key, "value": self.value}


def _ordered_outputs(
    values: Mapping[str, str],
) -> tuple[ApplicationPreparationOutputReference, ...]:
    if not isinstance(values, Mapping):
        raise TypeError("stage outputs must be a mapping")
    return tuple(
        ApplicationPreparationOutputReference(key=key, value=value)
        for key, value in sorted(values.items())
    )


@dataclass(frozen=True, slots=True)
class PublicPreparationStageResult:
    stage: ApplicationPreparationStage
    status: PublicStageStatus
    public_status: str
    result_id: str | None
    result_content_hash: str | None
    outputs: tuple[ApplicationPreparationOutputReference, ...]
    reason_code: str | None = None
    retryable: bool = False
    human_attention_required: bool = False
    directive: PublicStageDirective = PublicStageDirective.CONTINUE

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stage", ApplicationPreparationStage(self.stage)
        )
        status = PublicStageStatus(self.status)
        object.__setattr__(self, "status", status)
        _clean_text("public_status", self.public_status, 120)
        if (
            not isinstance(self.outputs, tuple)
            or any(
                not isinstance(item, ApplicationPreparationOutputReference)
                for item in self.outputs
            )
            or tuple(sorted(self.outputs, key=lambda item: item.key))
            != self.outputs
            or len({item.key for item in self.outputs}) != len(self.outputs)
        ):
            raise ValueError("stage outputs must be unique and ordered")
        if type(self.retryable) is not bool or type(
            self.human_attention_required
        ) is not bool:
            raise TypeError("stage flags must be boolean")
        directive = PublicStageDirective(self.directive)
        object.__setattr__(self, "directive", directive)
        if status in {PublicStageStatus.CREATED, PublicStageStatus.UNCHANGED}:
            _clean_text("result_id", self.result_id, 240)
            _require_hash("result_content_hash", self.result_content_hash)
            if self.reason_code is not None or self.retryable:
                raise ValueError("successful public stage is malformed")
        else:
            if self.reason_code is None:
                raise ValueError("stopped public stage needs a reason")
            _clean_text("reason_code", self.reason_code, 200)
            if self.result_content_hash is not None:
                _require_hash(
                    "result_content_hash", self.result_content_hash
                )
        if (
            self.stage is not ApplicationPreparationStage.RESUME_VISUAL_QA
            and directive is not PublicStageDirective.CONTINUE
        ):
            raise ValueError("only Visual QA may direct revision")

    @classmethod
    def success(
        cls,
        *,
        stage: ApplicationPreparationStage,
        status: PublicStageStatus,
        public_status: str,
        result_id: str,
        result_content_hash: str,
        outputs: Mapping[str, str],
        human_attention_required: bool = False,
        directive: PublicStageDirective = PublicStageDirective.CONTINUE,
    ) -> "PublicPreparationStageResult":
        if status not in {
            PublicStageStatus.CREATED,
            PublicStageStatus.UNCHANGED,
        }:
            raise ValueError("success requires CREATED or UNCHANGED")
        return cls(
            stage=stage,
            status=status,
            public_status=public_status,
            result_id=result_id,
            result_content_hash=result_content_hash,
            outputs=_ordered_outputs(outputs),
            human_attention_required=human_attention_required,
            directive=directive,
        )

    @classmethod
    def stopped(
        cls,
        *,
        stage: ApplicationPreparationStage,
        status: PublicStageStatus,
        public_status: str,
        reason_code: str,
        retryable: bool = False,
        human_attention_required: bool = False,
    ) -> "PublicPreparationStageResult":
        if status not in {
            PublicStageStatus.DEFERRED,
            PublicStageStatus.FAILED,
        }:
            raise ValueError("stopped result must defer or fail")
        return cls(
            stage=stage,
            status=status,
            public_status=public_status,
            result_id=None,
            result_content_hash=None,
            outputs=(),
            reason_code=reason_code,
            retryable=retryable,
            human_attention_required=human_attention_required,
        )


@dataclass(frozen=True, slots=True)
class ApplicationPreparationStageResult:
    stage: ApplicationPreparationStage
    execution_status: PreparationStageExecutionStatus
    public_status: str
    result_id: str | None
    result_content_hash: str | None
    outputs: tuple[ApplicationPreparationOutputReference, ...]
    reason_code: str | None
    retryable: bool
    human_attention_required: bool
    stage_content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stage", ApplicationPreparationStage(self.stage)
        )
        status = PreparationStageExecutionStatus(self.execution_status)
        object.__setattr__(self, "execution_status", status)
        _clean_text("public_status", self.public_status, 120)
        if not isinstance(self.outputs, tuple) or tuple(
            sorted(self.outputs, key=lambda item: item.key)
        ) != self.outputs:
            raise ValueError("stage-result outputs are invalid")
        if type(self.retryable) is not bool or type(
            self.human_attention_required
        ) is not bool:
            raise TypeError("stage-result flags must be boolean")
        if status in {
            PreparationStageExecutionStatus.CREATED,
            PreparationStageExecutionStatus.UNCHANGED,
        }:
            _clean_text("result_id", self.result_id, 240)
            _require_hash("result_content_hash", self.result_content_hash)
            if self.reason_code is not None:
                raise ValueError("successful stage cannot have a reason")
        else:
            _clean_text("reason_code", self.reason_code, 200)
        if self.stage_content_hash != _canonical_hash(
            self.content_dict()
        ):
            raise ValueError("stage-result hash is invalid")

    def content_dict(self) -> dict[str, Any]:
        return {
            "execution_status": self.execution_status.value,
            "human_attention_required": self.human_attention_required,
            "outputs": [item.to_dict() for item in self.outputs],
            "public_status": self.public_status,
            "reason_code": self.reason_code,
            "result_content_hash": self.result_content_hash,
            "result_id": self.result_id,
            "retryable": self.retryable,
            "stage": self.stage.value,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "stage_content_hash": self.stage_content_hash,
        }

    @classmethod
    def from_public(
        cls, result: PublicPreparationStageResult
    ) -> "ApplicationPreparationStageResult":
        execution = PreparationStageExecutionStatus(result.status.value)
        content = {
            "execution_status": execution.value,
            "human_attention_required": result.human_attention_required,
            "outputs": [item.to_dict() for item in result.outputs],
            "public_status": result.public_status,
            "reason_code": result.reason_code,
            "result_content_hash": result.result_content_hash,
            "result_id": result.result_id,
            "retryable": result.retryable,
            "stage": result.stage.value,
        }
        return cls(
            stage=result.stage,
            execution_status=execution,
            public_status=result.public_status,
            result_id=result.result_id,
            result_content_hash=result.result_content_hash,
            outputs=result.outputs,
            reason_code=result.reason_code,
            retryable=result.retryable,
            human_attention_required=result.human_attention_required,
            stage_content_hash=_canonical_hash(content),
        )

    @classmethod
    def skipped_layout(cls) -> "ApplicationPreparationStageResult":
        content = {
            "execution_status": PreparationStageExecutionStatus.SKIPPED.value,
            "human_attention_required": False,
            "outputs": [],
            "public_status": "NOT_REQUIRED_VISUAL_QA_PASSED",
            "reason_code": "VISUAL_QA_PASSED",
            "result_content_hash": None,
            "result_id": None,
            "retryable": False,
            "stage": ApplicationPreparationStage.RESUME_LAYOUT_REVISION.value,
        }
        return cls(
            stage=ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
            execution_status=PreparationStageExecutionStatus.SKIPPED,
            public_status="NOT_REQUIRED_VISUAL_QA_PASSED",
            result_id=None,
            result_content_hash=None,
            outputs=(),
            reason_code="VISUAL_QA_PASSED",
            retryable=False,
            human_attention_required=False,
            stage_content_hash=_canonical_hash(content),
        )


@dataclass(frozen=True, slots=True)
class ApplicationPreparationStageRequest:
    stage: ApplicationPreparationStage
    subject_id: str
    application_plan_id: str
    job_id: str
    now: datetime
    outputs: tuple[ApplicationPreparationOutputReference, ...]
    prior_stage_results: tuple[ApplicationPreparationStageResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stage", ApplicationPreparationStage(self.stage)
        )
        _clean_text("subject_id", self.subject_id, 160)
        _clean_text("application_plan_id", self.application_plan_id, 180)
        _clean_text("job_id", self.job_id, 160)
        _require_aware("now", self.now)
        if tuple(sorted(self.outputs, key=lambda item: item.key)) != self.outputs:
            raise ValueError("request outputs must be ordered")
        if not isinstance(self.prior_stage_results, tuple):
            raise TypeError("prior_stage_results must be a tuple")

    def output(self, key: str) -> str:
        for item in self.outputs:
            if item.key == key:
                return item.value
        raise KeyError(key)


@runtime_checkable
class ApplicationPreparationPublicCallable(Protocol):
    def __call__(
        self, request: ApplicationPreparationStageRequest
    ) -> PublicPreparationStageResult: ...


@dataclass(frozen=True, slots=True)
class ApplicationPreparationStageDefinition:
    stage: ApplicationPreparationStage
    public_callable_name: str
    slice_contract_version: str
    slice_policy_version: str
    configuration_hash: str
    invoke: Callable[
        [ApplicationPreparationStageRequest],
        PublicPreparationStageResult,
    ] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stage", ApplicationPreparationStage(self.stage)
        )
        _clean_text("public_callable_name", self.public_callable_name, 120)
        _clean_text("slice_contract_version", self.slice_contract_version)
        _clean_text("slice_policy_version", self.slice_policy_version)
        _require_hash("configuration_hash", self.configuration_hash)
        if not callable(self.invoke):
            raise TypeError("stage invoke must be callable")

    def identity_dict(self) -> dict[str, str]:
        return {
            "configuration_hash": self.configuration_hash,
            "public_callable_name": self.public_callable_name,
            "slice_contract_version": self.slice_contract_version,
            "slice_policy_version": self.slice_policy_version,
            "stage": self.stage.value,
        }


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRecipe:
    input_binding_hash: str
    stages: tuple[ApplicationPreparationStageDefinition, ...]
    required_material_policy: RequiredApplicationMaterialPolicy

    def __post_init__(self) -> None:
        _require_hash("input_binding_hash", self.input_binding_hash)
        if not isinstance(self.stages, tuple) or tuple(
            item.stage for item in self.stages
        ) != APPLICATION_PREPARATION_STAGE_ORDER:
            raise ValueError("recipe must define every stage in order")
        if not isinstance(
            self.required_material_policy,
            RequiredApplicationMaterialPolicy,
        ):
            raise TypeError("required material policy must be typed")

    @property
    def metadata_hash(self) -> str:
        return _canonical_hash(
            {
                "input_binding_hash": self.input_binding_hash,
                "required_material_policy_hash": (
                    self.required_material_policy.policy_content_hash
                ),
                "stages": [
                    item.identity_dict() for item in self.stages
                ],
            }
        )


_REQUIRED_OUTPUTS: dict[ApplicationPreparationStage, frozenset[str]] = {
    ApplicationPreparationStage.BASE_RESUME_SELECTION: frozenset(
        {"resume_selection_decision_id", "resume_id"}
    ),
    ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: frozenset(
        {"source_resume_projection_id"}
    ),
    ApplicationPreparationStage.RESUME_EVIDENCE: frozenset(
        {"resume_evidence_snapshot_id"}
    ),
    ApplicationPreparationStage.RESUME_TAILORING: frozenset(
        {"tailored_resume_draft_id"}
    ),
    ApplicationPreparationStage.RESUME_FACT_QA: frozenset(
        {"resume_fact_qa_result_id"}
    ),
    ApplicationPreparationStage.BASE_LATEX_SELECTION: frozenset(
        {"base_latex_selection_id"}
    ),
    ApplicationPreparationStage.LATEX_CONSTRUCTION: frozenset(
        {"latex_version_id", "latex_construction_record_id"}
    ),
    ApplicationPreparationStage.RESUME_COMPILATION: frozenset(
        {"compilation_record_id"}
    ),
    ApplicationPreparationStage.RESUME_VISUAL_QA: frozenset(
        {"visual_qa_result_id"}
    ),
    ApplicationPreparationStage.RESUME_LAYOUT_REVISION: frozenset(
        {
            "layout_revision_run_id",
            "latex_version_id",
            "compilation_record_id",
            "visual_qa_result_id",
        }
    ),
    ApplicationPreparationStage.RESUME_PUBLICATION: frozenset(
        {"prepared_resume_material_id"}
    ),
    ApplicationPreparationStage.RESUME_MANIFEST: frozenset(
        {"plan_material_manifest_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_EVIDENCE: frozenset(
        {"cover_letter_evidence_snapshot_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_DRAFT: frozenset(
        {"cover_letter_draft_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_FACT_QA: frozenset(
        {"cover_letter_fact_qa_result_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_PUBLICATION: frozenset(
        {"prepared_cover_letter_material_id"}
    ),
    ApplicationPreparationStage.COVER_LETTER_MANIFEST: frozenset(
        {"plan_material_manifest_id"}
    ),
    ApplicationPreparationStage.APPLICATION_ANSWERS: frozenset(
        {"prepared_application_answer_set_id"}
    ),
}


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRun:
    run_id: str
    contract_version: str
    preparation_binding: str
    recipe_metadata_hash: str
    required_material_policy_id: str
    required_material_policy_version: str
    required_material_policy_hash: str
    subject_id: str
    application_plan_id: str
    job_id: str
    job_revision: int
    job_content_hash: str
    stage_results: tuple[ApplicationPreparationStageResult, ...]
    final_plan_material_manifest_id: str | None
    final_prepared_application_answer_set_id: str | None
    completed_roles: tuple[ApplicationPreparationCompletedRole, ...]
    human_attention_required: bool
    deferred_stage: ApplicationPreparationStage | None
    deferred_reason: str | None
    failed_stage: ApplicationPreparationStage | None
    failed_reason: str | None
    overall_status: ApplicationPreparationRunStatus
    run_content_hash: str
    started_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ):
            raise ValueError("orchestration contract is unsupported")
        for name, value in (
            ("preparation_binding", self.preparation_binding),
            ("recipe_metadata_hash", self.recipe_metadata_hash),
            (
                "required_material_policy_hash",
                self.required_material_policy_hash,
            ),
            ("job_content_hash", self.job_content_hash),
        ):
            _require_hash(name, value)
        expected_id = "application-preparation-run-" + _canonical_hash(
            self.identity_dict()
        )
        if (
            _RUN_ID_RE.fullmatch(self.run_id) is None
            or self.run_id != expected_id
        ):
            raise ValueError("preparation run ID is invalid")
        _clean_text("subject_id", self.subject_id, 160)
        _clean_text("application_plan_id", self.application_plan_id, 180)
        _clean_text("job_id", self.job_id, 160)
        if (
            self.required_material_policy_id
            != REQUIRED_MATERIAL_POLICY_ID
            or self.required_material_policy_version
            != REQUIRED_MATERIAL_POLICY_VERSION
        ):
            raise ValueError("run required-material policy is unsupported")
        if type(self.job_revision) is not int or self.job_revision < 1:
            raise ValueError("job revision is invalid")
        if not isinstance(self.stage_results, tuple) or any(
            not isinstance(item, ApplicationPreparationStageResult)
            for item in self.stage_results
        ):
            raise TypeError("stage results must be typed")
        if tuple(item.stage for item in self.stage_results) != (
            APPLICATION_PREPARATION_STAGE_ORDER[: len(self.stage_results)]
        ):
            raise ValueError("stage lineage must be an exact ordered prefix")
        roles = tuple(
            ApplicationPreparationCompletedRole(item)
            for item in self.completed_roles
        )
        if roles != tuple(
            sorted(
                set(roles),
                key=lambda item: list(
                    ApplicationPreparationCompletedRole
                ).index(item),
            )
        ):
            raise ValueError("completed roles are invalid")
        object.__setattr__(self, "completed_roles", roles)
        successful_stages = {
            item.stage
            for item in self.stage_results
            if item.execution_status
            in {
                PreparationStageExecutionStatus.CREATED,
                PreparationStageExecutionStatus.UNCHANGED,
            }
        }
        expected_roles = tuple(
            role
            for role, stage in (
                (
                    ApplicationPreparationCompletedRole.RESUME,
                    ApplicationPreparationStage.RESUME_MANIFEST,
                ),
                (
                    ApplicationPreparationCompletedRole.COVER_LETTER,
                    ApplicationPreparationStage.COVER_LETTER_MANIFEST,
                ),
                (
                    ApplicationPreparationCompletedRole.APPLICATION_ANSWERS,
                    ApplicationPreparationStage.APPLICATION_ANSWERS,
                ),
            )
            if stage in successful_stages
        )
        if roles != expected_roles:
            raise ValueError("completed roles do not match stage lineage")
        if type(self.human_attention_required) is not bool:
            raise TypeError("human_attention_required must be boolean")
        if self.human_attention_required != any(
            item.human_attention_required for item in self.stage_results
        ):
            raise ValueError("human-attention summary conflicts with lineage")
        lineage_outputs: dict[str, str] = {}
        for item in self.stage_results:
            lineage_outputs.update(
                {output.key: output.value for output in item.outputs}
            )
        if self.final_plan_material_manifest_id != lineage_outputs.get(
            "plan_material_manifest_id"
        ) or self.final_prepared_application_answer_set_id != (
            lineage_outputs.get("prepared_application_answer_set_id")
        ):
            raise ValueError("final output IDs conflict with stage lineage")
        status = ApplicationPreparationRunStatus(self.overall_status)
        object.__setattr__(self, "overall_status", status)
        if status is ApplicationPreparationRunStatus.COMPLETED:
            if (
                len(self.stage_results)
                != len(APPLICATION_PREPARATION_STAGE_ORDER)
                or not self.final_plan_material_manifest_id
                or not self.final_prepared_application_answer_set_id
                or roles
                != tuple(ApplicationPreparationCompletedRole)
                or self.deferred_stage is not None
                or self.deferred_reason is not None
                or self.failed_stage is not None
                or self.failed_reason is not None
            ):
                raise ValueError("completed preparation run is incomplete")
        elif status is ApplicationPreparationRunStatus.DEFERRED:
            if (
                self.deferred_stage is None
                or self.deferred_reason is None
                or self.failed_stage is not None
                or self.failed_reason is not None
            ):
                raise ValueError("deferred preparation run is malformed")
        elif (
            self.failed_stage is None
            or self.failed_reason is None
            or self.deferred_stage is not None
            or self.deferred_reason is not None
        ):
            raise ValueError("failed preparation run is malformed")
        if status is not ApplicationPreparationRunStatus.COMPLETED:
            final_stage = self.stage_results[-1]
            expected_stage = (
                self.deferred_stage
                if status is ApplicationPreparationRunStatus.DEFERRED
                else self.failed_stage
            )
            expected_execution = (
                PreparationStageExecutionStatus.DEFERRED
                if status is ApplicationPreparationRunStatus.DEFERRED
                else PreparationStageExecutionStatus.FAILED
            )
            if (
                final_stage.stage is not expected_stage
                or final_stage.execution_status is not expected_execution
                or final_stage.reason_code
                != (
                    self.deferred_reason
                    if status is ApplicationPreparationRunStatus.DEFERRED
                    else self.failed_reason
                )
            ):
                raise ValueError("stopped outcome conflicts with stage lineage")
        started = _require_aware("started_at", self.started_at)
        completed = _require_aware("completed_at", self.completed_at)
        if completed < started:
            raise ValueError("completed_at precedes started_at")
        if self.run_content_hash != _canonical_hash(self.content_dict()):
            raise ValueError("preparation run content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "completed_roles": [item.value for item in self.completed_roles],
            "contract_version": self.contract_version,
            "deferred_reason": self.deferred_reason,
            "deferred_stage": (
                self.deferred_stage.value if self.deferred_stage else None
            ),
            "failed_reason": self.failed_reason,
            "failed_stage": (
                self.failed_stage.value if self.failed_stage else None
            ),
            "final_plan_material_manifest_id": (
                self.final_plan_material_manifest_id
            ),
            "final_prepared_application_answer_set_id": (
                self.final_prepared_application_answer_set_id
            ),
            "human_attention_required": self.human_attention_required,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "overall_status": self.overall_status.value,
            "preparation_binding": self.preparation_binding,
            "recipe_metadata_hash": self.recipe_metadata_hash,
            "required_material_policy_hash": (
                self.required_material_policy_hash
            ),
            "required_material_policy_id": (
                self.required_material_policy_id
            ),
            "required_material_policy_version": (
                self.required_material_policy_version
            ),
            "stage_hashes": [
                item.stage_content_hash for item in self.stage_results
            ],
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "completed_at": _rfc3339(self.completed_at),
            "completed_roles": [item.value for item in self.completed_roles],
            "contract_version": self.contract_version,
            "deferred_reason": self.deferred_reason,
            "deferred_stage": (
                self.deferred_stage.value if self.deferred_stage else None
            ),
            "failed_reason": self.failed_reason,
            "failed_stage": (
                self.failed_stage.value if self.failed_stage else None
            ),
            "final_plan_material_manifest_id": (
                self.final_plan_material_manifest_id
            ),
            "final_prepared_application_answer_set_id": (
                self.final_prepared_application_answer_set_id
            ),
            "human_attention_required": self.human_attention_required,
            "job_content_hash": self.job_content_hash,
            "job_id": self.job_id,
            "job_revision": self.job_revision,
            "overall_status": self.overall_status.value,
            "preparation_binding": self.preparation_binding,
            "recipe_metadata_hash": self.recipe_metadata_hash,
            "required_material_policy_hash": (
                self.required_material_policy_hash
            ),
            "required_material_policy_id": (
                self.required_material_policy_id
            ),
            "required_material_policy_version": (
                self.required_material_policy_version
            ),
            "run_id": self.run_id,
            "stage_results": [
                item.to_dict() for item in self.stage_results
            ],
            "started_at": _rfc3339(self.started_at),
            "subject_id": self.subject_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "run_content_hash": self.run_content_hash,
        }


class ApplicationPreparationRunReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ApplicationPreparationRunWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class ApplicationPreparationRunListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRunReadResult:
    status: ApplicationPreparationRunReadStatus
    run: ApplicationPreparationRun | None


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRunWriteResult:
    status: ApplicationPreparationRunWriteStatus
    run: ApplicationPreparationRun | None
    reason_code: ApplicationPreparationFailureReason | None
    retryable: bool


@dataclass(frozen=True, slots=True)
class ApplicationPreparationRunListResult:
    status: ApplicationPreparationRunListStatus
    runs: tuple[ApplicationPreparationRun, ...]


@runtime_checkable
class ApplicationPreparationRunRepository(Protocol):
    def get(
        self, *, subject_id: str, run_id: str
    ) -> ApplicationPreparationRunReadResult: ...

    def save(
        self, run: ApplicationPreparationRun
    ) -> ApplicationPreparationRunWriteResult: ...

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> ApplicationPreparationRunReadResult: ...

    def list_for_subject(
        self, *, subject_id: str
    ) -> ApplicationPreparationRunListResult: ...


def _stage_result_from_dict(
    value: Mapping[str, Any],
) -> ApplicationPreparationStageResult:
    return ApplicationPreparationStageResult(
        stage=ApplicationPreparationStage(value["stage"]),
        execution_status=PreparationStageExecutionStatus(
            value["execution_status"]
        ),
        public_status=value["public_status"],
        result_id=value["result_id"],
        result_content_hash=value["result_content_hash"],
        outputs=tuple(
            ApplicationPreparationOutputReference(
                key=item["key"], value=item["value"]
            )
            for item in value["outputs"]
        ),
        reason_code=value["reason_code"],
        retryable=value["retryable"],
        human_attention_required=value["human_attention_required"],
        stage_content_hash=value["stage_content_hash"],
    )


def _run_from_dict(value: Mapping[str, Any]) -> ApplicationPreparationRun:
    expected = {
        "application_plan_id",
        "completed_at",
        "completed_roles",
        "contract_version",
        "deferred_reason",
        "deferred_stage",
        "failed_reason",
        "failed_stage",
        "final_plan_material_manifest_id",
        "final_prepared_application_answer_set_id",
        "human_attention_required",
        "job_content_hash",
        "job_id",
        "job_revision",
        "overall_status",
        "preparation_binding",
        "recipe_metadata_hash",
        "required_material_policy_hash",
        "required_material_policy_id",
        "required_material_policy_version",
        "run_content_hash",
        "run_id",
        "stage_results",
        "started_at",
        "subject_id",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected
        or not isinstance(value["stage_results"], list)
        or not isinstance(value["completed_roles"], list)
    ):
        raise ValueError("persisted preparation run is invalid")
    return ApplicationPreparationRun(
        run_id=value["run_id"],
        contract_version=value["contract_version"],
        preparation_binding=value["preparation_binding"],
        recipe_metadata_hash=value["recipe_metadata_hash"],
        required_material_policy_id=value[
            "required_material_policy_id"
        ],
        required_material_policy_version=value[
            "required_material_policy_version"
        ],
        required_material_policy_hash=value[
            "required_material_policy_hash"
        ],
        subject_id=value["subject_id"],
        application_plan_id=value["application_plan_id"],
        job_id=value["job_id"],
        job_revision=value["job_revision"],
        job_content_hash=value["job_content_hash"],
        stage_results=tuple(
            _stage_result_from_dict(item)
            for item in value["stage_results"]
        ),
        final_plan_material_manifest_id=value[
            "final_plan_material_manifest_id"
        ],
        final_prepared_application_answer_set_id=value[
            "final_prepared_application_answer_set_id"
        ],
        completed_roles=tuple(
            ApplicationPreparationCompletedRole(item)
            for item in value["completed_roles"]
        ),
        human_attention_required=value["human_attention_required"],
        deferred_stage=(
            ApplicationPreparationStage(value["deferred_stage"])
            if value["deferred_stage"]
            else None
        ),
        deferred_reason=value["deferred_reason"],
        failed_stage=(
            ApplicationPreparationStage(value["failed_stage"])
            if value["failed_stage"]
            else None
        ),
        failed_reason=value["failed_reason"],
        overall_status=ApplicationPreparationRunStatus(
            value["overall_status"]
        ),
        run_content_hash=value["run_content_hash"],
        started_at=_parse_time("started_at", value["started_at"]),
        completed_at=_parse_time("completed_at", value["completed_at"]),
    )


class PrivateHomeApplicationPreparationRunRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        subject = _clean_text("subject_id", subject_id, 160)
        return (
            self._home.paths.application_preparation_runs
            / _subject_key(subject)
        )

    def _path(self, subject_id: str, run_id: str) -> Path:
        if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
            raise ValueError("run_id is invalid")
        return self._directory(subject_id) / f"{run_id}.json"

    def get(
        self, *, subject_id: str, run_id: str
    ) -> ApplicationPreparationRunReadResult:
        path = self._path(subject_id, run_id)
        with self._lock:
            if not path.exists():
                return ApplicationPreparationRunReadResult(
                    ApplicationPreparationRunReadStatus.NOT_FOUND, None
                )
            if path.is_symlink() or not path.is_file():
                return ApplicationPreparationRunReadResult(
                    ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            try:
                run = _run_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return ApplicationPreparationRunReadResult(
                    ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            if run.subject_id != subject_id.strip() or run.run_id != run_id:
                return ApplicationPreparationRunReadResult(
                    ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE,
                    None,
                )
            return ApplicationPreparationRunReadResult(
                ApplicationPreparationRunReadStatus.FOUND, run
            )

    def save(
        self, run: ApplicationPreparationRun
    ) -> ApplicationPreparationRunWriteResult:
        if not isinstance(run, ApplicationPreparationRun):
            raise TypeError("run must be typed")
        path = self._path(run.subject_id, run.run_id)
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            run.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return ApplicationPreparationRunWriteResult(
                    ApplicationPreparationRunWriteStatus.FAILED,
                    None,
                    ApplicationPreparationFailureReason.PERSISTENCE_FAILED,
                    True,
                )
            if created:
                return ApplicationPreparationRunWriteResult(
                    ApplicationPreparationRunWriteStatus.CREATED,
                    run,
                    None,
                    False,
                )
            existing = self.get(
                subject_id=run.subject_id, run_id=run.run_id
            )
            if (
                existing.status is ApplicationPreparationRunReadStatus.FOUND
                and existing.run is not None
                and existing.run.identity_dict() == run.identity_dict()
            ):
                return ApplicationPreparationRunWriteResult(
                    ApplicationPreparationRunWriteStatus.UNCHANGED,
                    existing.run,
                    None,
                    False,
                )
            return ApplicationPreparationRunWriteResult(
                ApplicationPreparationRunWriteStatus.FAILED,
                None,
                ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE,
                False,
            )

    def find_current_for_plan(
        self, *, subject_id: str, application_plan_id: str
    ) -> ApplicationPreparationRunReadResult:
        listed = self.list_for_subject(subject_id=subject_id)
        if (
            listed.status
            is ApplicationPreparationRunListStatus.INTEGRITY_FAILURE
        ):
            return ApplicationPreparationRunReadResult(
                ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE,
                None,
            )
        matches = tuple(
            run
            for run in listed.runs
            if run.application_plan_id == application_plan_id
        )
        if not matches:
            return ApplicationPreparationRunReadResult(
                ApplicationPreparationRunReadStatus.NOT_FOUND, None
            )
        current = max(
            matches,
            key=lambda item: (
                item.completed_at.astimezone(timezone.utc),
                item.run_id,
            ),
        )
        return ApplicationPreparationRunReadResult(
            ApplicationPreparationRunReadStatus.FOUND, current
        )

    def list_for_subject(
        self, *, subject_id: str
    ) -> ApplicationPreparationRunListResult:
        directory = self._directory(subject_id)
        if not directory.exists():
            return ApplicationPreparationRunListResult(
                ApplicationPreparationRunListStatus.SUCCEEDED, ()
            )
        try:
            paths = tuple(directory.iterdir())
        except OSError:
            return ApplicationPreparationRunListResult(
                ApplicationPreparationRunListStatus.INTEGRITY_FAILURE, ()
            )
        runs: list[ApplicationPreparationRun] = []
        for path in paths:
            if (
                path.suffix != ".json"
                or _RUN_ID_RE.fullmatch(path.stem) is None
            ):
                return ApplicationPreparationRunListResult(
                    ApplicationPreparationRunListStatus.INTEGRITY_FAILURE,
                    (),
                )
            read = self.get(subject_id=subject_id, run_id=path.stem)
            if (
                read.status is not ApplicationPreparationRunReadStatus.FOUND
                or read.run is None
            ):
                return ApplicationPreparationRunListResult(
                    ApplicationPreparationRunListStatus.INTEGRITY_FAILURE,
                    (),
                )
            runs.append(read.run)
        ordered = tuple(
            sorted(
                runs,
                key=lambda item: (
                    item.application_plan_id,
                    item.completed_at.astimezone(timezone.utc),
                    item.run_id,
                ),
            ),
        )
        return ApplicationPreparationRunListResult(
            ApplicationPreparationRunListStatus.SUCCEEDED, ordered
        )


@dataclass(frozen=True, slots=True)
class RunApplicationPreparationCommand:
    subject_id: str
    application_plan_id: str
    now: datetime


@dataclass(frozen=True, slots=True)
class RunApplicationPreparationResult:
    status: ApplicationPreparationStatus
    run: ApplicationPreparationRun | None
    reason_code: ApplicationPreparationFailureReason | None
    retryable: bool
    message: str


def _preparation_binding(
    plan: ApplicationPlan, recipe: ApplicationPreparationRecipe
) -> str:
    return _canonical_hash(
        {
            "application_plan_id": plan.plan_id,
            "contract_version": (
                APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
            ),
            "job_content_hash": plan.job_content_hash,
            "job_id": plan.job_id,
            "job_revision": plan.job_revision,
            "recipe_metadata_hash": recipe.metadata_hash,
            "required_material_policy_hash": (
                recipe.required_material_policy.policy_content_hash
            ),
            "subject_id": plan.subject_id,
        }
    )


def _run_result_failure(
    reason: ApplicationPreparationFailureReason,
    *,
    retryable: bool = False,
) -> RunApplicationPreparationResult:
    return RunApplicationPreparationResult(
        status=ApplicationPreparationStatus.FAILED,
        run=None,
        reason_code=reason,
        retryable=retryable,
        message=f"Application preparation failed: {reason.value}.",
    )


def _build_run(
    *,
    plan: ApplicationPlan,
    recipe: ApplicationPreparationRecipe,
    preparation_binding: str,
    stages: tuple[ApplicationPreparationStageResult, ...],
    outputs: Mapping[str, str],
    roles: tuple[ApplicationPreparationCompletedRole, ...],
    human_attention_required: bool,
    status: ApplicationPreparationRunStatus,
    stopped_stage: ApplicationPreparationStage | None,
    stopped_reason: str | None,
    now: datetime,
) -> ApplicationPreparationRun:
    policy = recipe.required_material_policy
    deferred_stage = (
        stopped_stage
        if status is ApplicationPreparationRunStatus.DEFERRED
        else None
    )
    failed_stage = (
        stopped_stage
        if status is ApplicationPreparationRunStatus.FAILED
        else None
    )
    values = {
        "application_plan_id": plan.plan_id,
        "completed_roles": [item.value for item in roles],
        "contract_version": (
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        "deferred_reason": (
            stopped_reason
            if status is ApplicationPreparationRunStatus.DEFERRED
            else None
        ),
        "deferred_stage": (
            deferred_stage.value if deferred_stage else None
        ),
        "failed_reason": (
            stopped_reason
            if status is ApplicationPreparationRunStatus.FAILED
            else None
        ),
        "failed_stage": failed_stage.value if failed_stage else None,
        "final_plan_material_manifest_id": outputs.get(
            "plan_material_manifest_id"
        ),
        "final_prepared_application_answer_set_id": outputs.get(
            "prepared_application_answer_set_id"
        ),
        "human_attention_required": human_attention_required,
        "job_content_hash": plan.job_content_hash,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "overall_status": status.value,
        "preparation_binding": preparation_binding,
        "recipe_metadata_hash": recipe.metadata_hash,
        "required_material_policy_hash": policy.policy_content_hash,
        "required_material_policy_id": policy.policy_id,
        "required_material_policy_version": policy.policy_version,
        "stage_hashes": [item.stage_content_hash for item in stages],
        "subject_id": plan.subject_id,
    }
    run_id = "application-preparation-run-" + _canonical_hash(values)
    content = {
        "application_plan_id": plan.plan_id,
        "completed_at": _rfc3339(now),
        "completed_roles": [item.value for item in roles],
        "contract_version": (
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        "deferred_reason": values["deferred_reason"],
        "deferred_stage": values["deferred_stage"],
        "failed_reason": values["failed_reason"],
        "failed_stage": values["failed_stage"],
        "final_plan_material_manifest_id": values[
            "final_plan_material_manifest_id"
        ],
        "final_prepared_application_answer_set_id": values[
            "final_prepared_application_answer_set_id"
        ],
        "human_attention_required": human_attention_required,
        "job_content_hash": plan.job_content_hash,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "overall_status": status.value,
        "preparation_binding": preparation_binding,
        "recipe_metadata_hash": recipe.metadata_hash,
        "required_material_policy_hash": policy.policy_content_hash,
        "required_material_policy_id": policy.policy_id,
        "required_material_policy_version": policy.policy_version,
        "run_id": run_id,
        "stage_results": [item.to_dict() for item in stages],
        "started_at": _rfc3339(now),
        "subject_id": plan.subject_id,
    }
    return ApplicationPreparationRun(
        run_id=run_id,
        contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        preparation_binding=preparation_binding,
        recipe_metadata_hash=recipe.metadata_hash,
        required_material_policy_id=policy.policy_id,
        required_material_policy_version=policy.policy_version,
        required_material_policy_hash=policy.policy_content_hash,
        subject_id=plan.subject_id,
        application_plan_id=plan.plan_id,
        job_id=plan.job_id,
        job_revision=plan.job_revision,
        job_content_hash=plan.job_content_hash,
        stage_results=stages,
        final_plan_material_manifest_id=values[
            "final_plan_material_manifest_id"
        ],
        final_prepared_application_answer_set_id=values[
            "final_prepared_application_answer_set_id"
        ],
        completed_roles=roles,
        human_attention_required=human_attention_required,
        deferred_stage=deferred_stage,
        deferred_reason=values["deferred_reason"],
        failed_stage=failed_stage,
        failed_reason=values["failed_reason"],
        overall_status=status,
        run_content_hash=_canonical_hash(content),
        started_at=now,
        completed_at=now,
    )


def _persist_outcome(
    run: ApplicationPreparationRun,
    repository: ApplicationPreparationRunRepository,
    *,
    operation_reason: ApplicationPreparationFailureReason | None = None,
) -> RunApplicationPreparationResult:
    try:
        write = repository.save(run)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason.PERSISTENCE_FAILED,
            retryable=True,
        )
    if (
        write.status is ApplicationPreparationRunWriteStatus.FAILED
        or write.run is None
    ):
        return _run_result_failure(
            write.reason_code
            or ApplicationPreparationFailureReason.PERSISTENCE_FAILED,
            retryable=write.retryable,
        )
    if (
        run.overall_status is ApplicationPreparationRunStatus.COMPLETED
        and write.status is ApplicationPreparationRunWriteStatus.UNCHANGED
    ):
        status = ApplicationPreparationStatus.UNCHANGED
    else:
        status = ApplicationPreparationStatus(run.overall_status.value)
    return RunApplicationPreparationResult(
        status=status,
        run=write.run,
        reason_code=operation_reason,
        retryable=False,
        message=f"Application preparation is {status.value}.",
    )


def run_application_preparation(
    command: RunApplicationPreparationCommand,
    *,
    application_plan_repository: ApplicationPlanRepository,
    recipe: ApplicationPreparationRecipe,
    run_repository: ApplicationPreparationRunRepository,
) -> RunApplicationPreparationResult:
    try:
        subject = _clean_text("subject_id", command.subject_id, 160)
        plan_id = _clean_text(
            "application_plan_id", command.application_plan_id, 180
        )
        now = _require_aware("now", command.now)
        if not isinstance(recipe, ApplicationPreparationRecipe):
            raise TypeError("recipe must be typed")
    except (AttributeError, TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason.INVALID_REQUEST
        )
    try:
        plan_read = application_plan_repository.get(plan_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE
        )
    if plan_read.status is ApplicationPlanReadStatus.NOT_FOUND:
        return _run_result_failure(
            ApplicationPreparationFailureReason.APPLICATION_PLAN_NOT_FOUND
        )
    if (
        plan_read.status is not ApplicationPlanReadStatus.FOUND
        or not isinstance(plan_read.plan, ApplicationPlan)
    ):
        return _run_result_failure(
            ApplicationPreparationFailureReason
            .APPLICATION_PLAN_INTEGRITY_FAILURE
        )
    plan = plan_read.plan
    if plan.subject_id != subject:
        return _run_result_failure(
            ApplicationPreparationFailureReason
            .APPLICATION_PLAN_SUBJECT_MISMATCH
        )
    binding = _preparation_binding(plan, recipe)
    try:
        current = run_repository.find_current_for_plan(
            subject_id=subject, application_plan_id=plan.plan_id
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _run_result_failure(
            ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE
        )
    if (
        current.status
        is ApplicationPreparationRunReadStatus.INTEGRITY_FAILURE
    ):
        return _run_result_failure(
            ApplicationPreparationFailureReason.RUN_INTEGRITY_FAILURE
        )
    if (
        current.status is ApplicationPreparationRunReadStatus.FOUND
        and current.run is not None
        and current.run.overall_status
        is ApplicationPreparationRunStatus.COMPLETED
        and current.run.preparation_binding == binding
    ):
        return RunApplicationPreparationResult(
            status=ApplicationPreparationStatus.UNCHANGED,
            run=current.run,
            reason_code=None,
            retryable=False,
            message="Completed application preparation is unchanged.",
        )

    outputs: dict[str, str] = {}
    stage_results: list[ApplicationPreparationStageResult] = []
    roles: list[ApplicationPreparationCompletedRole] = []
    human_attention_required = False
    visual_directive: PublicStageDirective | None = None

    def persist_contract_failure(
        stage: ApplicationPreparationStage,
    ) -> RunApplicationPreparationResult:
        failed = ApplicationPreparationStageResult.from_public(
            PublicPreparationStageResult.stopped(
                stage=stage,
                status=PublicStageStatus.FAILED,
                public_status="PUBLIC_STAGE_CONTRACT_FAILURE",
                reason_code=(
                    ApplicationPreparationFailureReason
                    .PUBLIC_STAGE_CONTRACT_FAILURE.value
                ),
            )
        )
        stage_results.append(failed)
        run = _build_run(
            plan=plan,
            recipe=recipe,
            preparation_binding=binding,
            stages=tuple(stage_results),
            outputs=outputs,
            roles=tuple(roles),
            human_attention_required=human_attention_required,
            status=ApplicationPreparationRunStatus.FAILED,
            stopped_stage=stage,
            stopped_reason=(
                ApplicationPreparationFailureReason
                .PUBLIC_STAGE_CONTRACT_FAILURE.value
            ),
            now=now,
        )
        return _persist_outcome(
            run,
            run_repository,
            operation_reason=(
                ApplicationPreparationFailureReason
                .PUBLIC_STAGE_CONTRACT_FAILURE
            ),
        )

    for definition in recipe.stages:
        stage = definition.stage
        if stage is ApplicationPreparationStage.RESUME_LAYOUT_REVISION:
            if visual_directive is PublicStageDirective.PASSED:
                stage_results.append(
                    ApplicationPreparationStageResult.skipped_layout()
                )
                continue
            if visual_directive is not PublicStageDirective.REVISION_REQUIRED:
                return persist_contract_failure(
                    ApplicationPreparationStage.RESUME_LAYOUT_REVISION
                )
        request = ApplicationPreparationStageRequest(
            stage=stage,
            subject_id=subject,
            application_plan_id=plan.plan_id,
            job_id=plan.job_id,
            now=now,
            outputs=_ordered_outputs(outputs),
            prior_stage_results=tuple(stage_results),
        )
        try:
            public_result = definition.invoke(request)
        except Exception:
            failed = ApplicationPreparationStageResult.from_public(
                PublicPreparationStageResult.stopped(
                    stage=stage,
                    status=PublicStageStatus.FAILED,
                    public_status="PUBLIC_STAGE_EXCEPTION",
                    reason_code=(
                        ApplicationPreparationFailureReason
                        .PUBLIC_STAGE_EXCEPTION.value
                    ),
                )
            )
            stage_results.append(failed)
            run = _build_run(
                plan=plan,
                recipe=recipe,
                preparation_binding=binding,
                stages=tuple(stage_results),
                outputs=outputs,
                roles=tuple(roles),
                human_attention_required=human_attention_required,
                status=ApplicationPreparationRunStatus.FAILED,
                stopped_stage=stage,
                stopped_reason=(
                    ApplicationPreparationFailureReason
                    .PUBLIC_STAGE_EXCEPTION.value
                ),
                now=now,
            )
            return _persist_outcome(
                run,
                run_repository,
                operation_reason=(
                    ApplicationPreparationFailureReason
                    .PUBLIC_STAGE_EXCEPTION
                ),
            )
        if (
            not isinstance(public_result, PublicPreparationStageResult)
            or public_result.stage is not stage
        ):
            return persist_contract_failure(stage)
        stage_record = ApplicationPreparationStageResult.from_public(
            public_result
        )
        if public_result.status in {
            PublicStageStatus.CREATED,
            PublicStageStatus.UNCHANGED,
        }:
            output_map = {
                item.key: item.value for item in public_result.outputs
            }
            if not _REQUIRED_OUTPUTS[stage].issubset(output_map):
                return persist_contract_failure(stage)
            if stage is ApplicationPreparationStage.RESUME_VISUAL_QA:
                visual_directive = public_result.directive
                if visual_directive not in {
                    PublicStageDirective.PASSED,
                    PublicStageDirective.REVISION_REQUIRED,
                }:
                    return persist_contract_failure(stage)
            stage_results.append(stage_record)
            human_attention_required = (
                human_attention_required
                or public_result.human_attention_required
            )
            outputs.update(output_map)
            if stage is ApplicationPreparationStage.RESUME_MANIFEST:
                roles.append(ApplicationPreparationCompletedRole.RESUME)
            elif stage is ApplicationPreparationStage.COVER_LETTER_MANIFEST:
                roles.append(
                    ApplicationPreparationCompletedRole.COVER_LETTER
                )
            elif stage is ApplicationPreparationStage.APPLICATION_ANSWERS:
                roles.append(
                    ApplicationPreparationCompletedRole.APPLICATION_ANSWERS
                )
            continue
        stage_results.append(stage_record)
        human_attention_required = (
            human_attention_required
            or public_result.human_attention_required
        )
        stopped_status = (
            ApplicationPreparationRunStatus.DEFERRED
            if public_result.status is PublicStageStatus.DEFERRED
            else ApplicationPreparationRunStatus.FAILED
        )
        run = _build_run(
            plan=plan,
            recipe=recipe,
            preparation_binding=binding,
            stages=tuple(stage_results),
            outputs=outputs,
            roles=tuple(roles),
            human_attention_required=human_attention_required,
            status=stopped_status,
            stopped_stage=stage,
            stopped_reason=public_result.reason_code,
            now=now,
        )
        return _persist_outcome(run, run_repository)

    run = _build_run(
        plan=plan,
        recipe=recipe,
        preparation_binding=binding,
        stages=tuple(stage_results),
        outputs=outputs,
        roles=tuple(roles),
        human_attention_required=human_attention_required,
        status=ApplicationPreparationRunStatus.COMPLETED,
        stopped_stage=None,
        stopped_reason=None,
        now=now,
    )
    return _persist_outcome(run, run_repository)


__all__ = [
    "APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION",
    "APPLICATION_PREPARATION_STAGE_ORDER",
    "ApplicationPreparationCompletedRole",
    "ApplicationPreparationFailureReason",
    "ApplicationPreparationOutputReference",
    "ApplicationPreparationPublicCallable",
    "ApplicationPreparationRecipe",
    "ApplicationPreparationRun",
    "ApplicationPreparationRunReadResult",
    "ApplicationPreparationRunReadStatus",
    "ApplicationPreparationRunListResult",
    "ApplicationPreparationRunListStatus",
    "ApplicationPreparationRunRepository",
    "ApplicationPreparationRunStatus",
    "ApplicationPreparationRunWriteResult",
    "ApplicationPreparationRunWriteStatus",
    "ApplicationPreparationStage",
    "ApplicationPreparationStageDefinition",
    "ApplicationPreparationStageRequest",
    "ApplicationPreparationStageResult",
    "ApplicationPreparationStatus",
    "PreparationStageExecutionStatus",
    "PrivateHomeApplicationPreparationRunRepository",
    "PublicPreparationStageResult",
    "PublicStageDirective",
    "PublicStageStatus",
    "RequiredApplicationMaterialPolicy",
    "RunApplicationPreparationCommand",
    "RunApplicationPreparationResult",
    "run_application_preparation",
]
