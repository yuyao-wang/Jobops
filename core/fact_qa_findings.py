"""Public typed access to blocking Resume and Cover Letter Fact QA findings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .cover_letter_fact_qa import (
    CoverLetterFactQAFindingSeverity,
    CoverLetterFactQAReadStatus,
    CoverLetterFactQARepository,
    CoverLetterFactQAResult,
)
from .resume_fact_qa import (
    ResumeFactQAFindingSeverity,
    ResumeFactQAReadStatus,
    ResumeFactQARepository,
    ResumeFactQAResult,
)


FACT_QA_BLOCKING_FINDING_PROVIDER_CONTRACT_VERSION = (
    "fact-qa-blocking-finding-provider-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


class FactQAMaterialKind(StrEnum):
    RESUME = "RESUME"
    COVER_LETTER = "COVER_LETTER"


class FactQABlockingFindingReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class FactQABlockingFinding:
    finding_id: str
    order: int
    finding_kind: str
    claim_summary: str
    source_material_id: str
    source_material_content_hash: str
    blocking: bool = True

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("finding_id", self.finding_id, 200),
            ("finding_kind", self.finding_kind, 100),
            ("claim_summary", self.claim_summary, 1_200),
            ("source_material_id", self.source_material_id, 200),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.strip()) > maximum
            ):
                raise ValueError(f"{name} is outside the finding contract")
        if type(self.order) is not int or self.order < 0:
            raise ValueError("finding order must be non-negative")
        if (
            not isinstance(self.source_material_content_hash, str)
            or _HASH_RE.fullmatch(self.source_material_content_hash) is None
        ):
            raise ValueError("source material hash is invalid")
        if self.blocking is not True:
            raise ValueError("the provider exposes only blocking findings")


@dataclass(frozen=True, slots=True)
class FactQABlockingFindingSet:
    subject_id: str
    application_plan_id: str
    material_kind: FactQAMaterialKind
    qa_result_id: str
    qa_result_content_hash: str
    qa_contract_version: str
    findings: tuple[FactQABlockingFinding, ...]
    contract_version: str = (
        FACT_QA_BLOCKING_FINDING_PROVIDER_CONTRACT_VERSION
    )

    def __post_init__(self) -> None:
        if self.contract_version != (
            FACT_QA_BLOCKING_FINDING_PROVIDER_CONTRACT_VERSION
        ):
            raise ValueError("blocking-finding provider version is unsupported")
        for name, value in (
            ("subject_id", self.subject_id),
            ("application_plan_id", self.application_plan_id),
            ("qa_result_id", self.qa_result_id),
            ("qa_contract_version", self.qa_contract_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        object.__setattr__(
            self, "material_kind", FactQAMaterialKind(self.material_kind)
        )
        if (
            not isinstance(self.qa_result_content_hash, str)
            or _HASH_RE.fullmatch(self.qa_result_content_hash) is None
        ):
            raise ValueError("QA result hash is invalid")
        if (
            not isinstance(self.findings, tuple)
            or not self.findings
            or any(
                not isinstance(item, FactQABlockingFinding)
                for item in self.findings
            )
            or tuple(item.order for item in self.findings)
            != tuple(sorted(item.order for item in self.findings))
            or len({item.finding_id for item in self.findings})
            != len(self.findings)
            or len(
                {
                    (
                        item.source_material_id,
                        item.source_material_content_hash,
                    )
                    for item in self.findings
                }
            )
            != 1
        ):
            raise ValueError("blocking findings are invalid or unordered")


@dataclass(frozen=True, slots=True)
class FactQABlockingFindingSetResult:
    status: FactQABlockingFindingReadStatus
    finding_set: FactQABlockingFindingSet | None

    def __post_init__(self) -> None:
        status = FactQABlockingFindingReadStatus(self.status)
        object.__setattr__(self, "status", status)
        if (status is FactQABlockingFindingReadStatus.FOUND) != isinstance(
            self.finding_set, FactQABlockingFindingSet
        ):
            raise ValueError("blocking-finding read result is invalid")


@runtime_checkable
class FactQABlockingFindingProvider(Protocol):
    def list_blocking_findings(
        self,
        *,
        subject_id: str,
        qa_result_id: str,
        material_kind: FactQAMaterialKind,
    ) -> FactQABlockingFindingSetResult:
        """Return the exact ordered blocking findings for one formal QA result."""


@dataclass(frozen=True, slots=True)
class RepositoryFactQABlockingFindingProvider:
    resume_repository: ResumeFactQARepository
    cover_letter_repository: CoverLetterFactQARepository

    def list_blocking_findings(
        self,
        *,
        subject_id: str,
        qa_result_id: str,
        material_kind: FactQAMaterialKind,
    ) -> FactQABlockingFindingSetResult:
        try:
            kind = FactQAMaterialKind(material_kind)
            if kind is FactQAMaterialKind.RESUME:
                read = self.resume_repository.get(
                    subject_id=subject_id, qa_result_id=qa_result_id
                )
                if read.status is ResumeFactQAReadStatus.NOT_FOUND:
                    return FactQABlockingFindingSetResult(
                        FactQABlockingFindingReadStatus.NOT_FOUND, None
                    )
                if (
                    read.status is not ResumeFactQAReadStatus.FOUND
                    or not isinstance(read.qa_result, ResumeFactQAResult)
                ):
                    raise ValueError("resume QA result is invalid")
                result = read.qa_result
                findings = tuple(
                    FactQABlockingFinding(
                        finding_id=item.finding_id,
                        order=item.order,
                        finding_kind=item.finding_type.value,
                        claim_summary=item.claim_text,
                        source_material_id=result.tailored_resume_draft_id,
                        source_material_content_hash=(
                            result.tailored_resume_draft_hash
                        ),
                    )
                    for item in result.findings
                    if item.severity is ResumeFactQAFindingSeverity.BLOCKING
                )
                finding_set = FactQABlockingFindingSet(
                    subject_id=result.subject_id,
                    application_plan_id=result.application_plan_id,
                    material_kind=kind,
                    qa_result_id=result.qa_result_id,
                    qa_result_content_hash=result.qa_content_hash,
                    qa_contract_version=result.contract_version,
                    findings=findings,
                )
            else:
                read = self.cover_letter_repository.get(
                    subject_id=subject_id, result_id=qa_result_id
                )
                if read.status is CoverLetterFactQAReadStatus.NOT_FOUND:
                    return FactQABlockingFindingSetResult(
                        FactQABlockingFindingReadStatus.NOT_FOUND, None
                    )
                if (
                    read.status is not CoverLetterFactQAReadStatus.FOUND
                    or not isinstance(read.result, CoverLetterFactQAResult)
                ):
                    raise ValueError("cover-letter QA result is invalid")
                result = read.result
                findings = tuple(
                    FactQABlockingFinding(
                        finding_id=item.finding_id,
                        order=index,
                        finding_kind=item.finding_type,
                        claim_summary=item.claim_text,
                        source_material_id=result.cover_letter_draft_id,
                        source_material_content_hash=result.draft_content_hash,
                    )
                    for index, item in enumerate(result.findings)
                    if item.severity
                    is CoverLetterFactQAFindingSeverity.BLOCKING
                )
                finding_set = FactQABlockingFindingSet(
                    subject_id=result.subject_id,
                    application_plan_id=result.application_plan_id,
                    material_kind=kind,
                    qa_result_id=result.result_id,
                    qa_result_content_hash=result.result_content_hash,
                    qa_contract_version=result.contract_version,
                    findings=findings,
                )
            return FactQABlockingFindingSetResult(
                FactQABlockingFindingReadStatus.FOUND, finding_set
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return FactQABlockingFindingSetResult(
                FactQABlockingFindingReadStatus.INTEGRITY_FAILURE, None
            )


__all__ = [
    "FACT_QA_BLOCKING_FINDING_PROVIDER_CONTRACT_VERSION",
    "FactQABlockingFinding",
    "FactQABlockingFindingProvider",
    "FactQABlockingFindingReadStatus",
    "FactQABlockingFindingSet",
    "FactQABlockingFindingSetResult",
    "FactQAMaterialKind",
    "RepositoryFactQABlockingFindingProvider",
]
