"""Authenticated Gate B review and one-application submission UI boundary."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping

from core.application_answers import PreparedApplicationAnswerSetReadStatus
from core.application_bundle_assembly import ApplicationBundleAssemblyReadStatus
from core.application_execution_orchestrator import (
    ApplicationExecutionRunReadStatus,
    ApplicationExecutionStage,
    ApplicationExecutionStatus,
    RunApplicationExecutionCommand,
)
from core.authenticated_subject import AuthenticatedSubjectContext
from core.current_application_execution_queue import (
    CurrentApplicationExecutionQueueStatus,
    CurrentApplicationExecutionStatus,
)
from core.non_submit_application_execution import (
    NonSubmitApplicationExecutionReadStatus,
    NonSubmitExecutionRecordState,
)
from core.submission_authorization import (
    create_explicit_submission_authorization,
)


APPLICATION_REVIEW_SUBMISSION_UI_CONTRACT_VERSION = (
    "application-review-submission-ui-v1"
)


class ApplicationReviewUIStatus(StrEnum):
    READY = "READY"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ApplicationSubmissionUIStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    BLOCKED = "BLOCKED"
    STALE_REVIEW = "STALE_REVIEW"
    IN_PROGRESS = "IN_PROGRESS"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    FAILED = "FAILED"


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value


def _rfc3339(value: datetime) -> str:
    return (
        _aware(value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


async def _call(callable_: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = callable_(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


@dataclass(frozen=True, slots=True)
class ApplicationReviewUIResult:
    status: ApplicationReviewUIStatus
    application_plan_id: str
    job_id: str | None
    title: str | None
    company: str | None
    location: str | None
    ats_type: str | None
    routed_adapter: str | None
    reviewed_at: datetime | None
    review_fingerprint: str | None
    review_token: str | None
    resume_included: bool
    cover_letter_included: bool
    prepared_answer_count: int
    unresolved_control_count: int
    message: str
    contract_version: str = APPLICATION_REVIEW_SUBMISSION_UI_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "ats_type": self.ats_type,
            "company": self.company,
            "contract_version": self.contract_version,
            "cover_letter_included": self.cover_letter_included,
            "job_id": self.job_id,
            "location": self.location,
            "message": self.message,
            "resume_included": self.resume_included,
            "review_fingerprint": self.review_fingerprint,
            "review_token": self.review_token,
            "reviewed_at": (
                _rfc3339(self.reviewed_at) if self.reviewed_at else None
            ),
            "routed_adapter": self.routed_adapter,
            "status": self.status.value,
            "title": self.title,
            "unresolved_control_count": self.unresolved_control_count,
            "prepared_answer_count": self.prepared_answer_count,
        }


@dataclass(frozen=True, slots=True)
class ApplicationSubmissionUIResult:
    status: ApplicationSubmissionUIStatus
    application_plan_id: str
    message: str
    retry_allowed: bool
    contract_version: str = APPLICATION_REVIEW_SUBMISSION_UI_CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "contract_version": self.contract_version,
            "message": self.message,
            "retry_allowed": self.retry_allowed,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class _ReviewBinding:
    review: ApplicationReviewUIResult
    queue_item: Any
    execution_run: Any
    non_submit_record: Any


class ApplicationReviewSubmissionUIController:
    """Turn one current persisted Review into an action-time Gate B decision."""

    def __init__(
        self,
        *,
        execution_queue_reader: Callable[..., Any],
        execution_run_repository: Any,
        non_submit_execution_repository: Any,
        assembly_repository: Any,
        answer_set_repository: Any,
        job_posting_repository: Any,
        single_job_execution: Callable[..., Any],
        clock: Callable[[], datetime],
    ) -> None:
        for value in (execution_queue_reader, single_job_execution, clock):
            if not callable(value):
                raise TypeError("review submission callable is unavailable")
        for value in (
            execution_run_repository,
            non_submit_execution_repository,
            assembly_repository,
            answer_set_repository,
            job_posting_repository,
        ):
            if value is None:
                raise TypeError("review submission repository is unavailable")
        self._execution_queue = execution_queue_reader
        self._runs = execution_run_repository
        self._non_submit = non_submit_execution_repository
        self._assemblies = assembly_repository
        self._answers = answer_set_repository
        self._jobs = job_posting_repository
        self._execute = single_job_execution
        self._clock = clock
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    @staticmethod
    def _subject(context: AuthenticatedSubjectContext) -> str:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        return context.subject_id

    @staticmethod
    def _unavailable(
        plan_id: str,
        *,
        integrity: bool = False,
    ) -> ApplicationReviewUIResult:
        return ApplicationReviewUIResult(
            status=(
                ApplicationReviewUIStatus.INTEGRITY_FAILURE
                if integrity
                else ApplicationReviewUIStatus.NOT_AVAILABLE
            ),
            application_plan_id=plan_id,
            job_id=None,
            title=None,
            company=None,
            location=None,
            ats_type=None,
            routed_adapter=None,
            reviewed_at=None,
            review_fingerprint=None,
            review_token=None,
            resume_included=False,
            cover_letter_included=False,
            prepared_answer_count=0,
            unresolved_control_count=0,
            message=(
                "The current review could not be validated safely."
                if integrity
                else "This application is not awaiting final submission approval."
            ),
        )

    async def _load_binding(
        self,
        *,
        subject_id: str,
        application_plan_id: str,
        now: datetime,
    ) -> _ReviewBinding | ApplicationReviewUIResult:
        try:
            queue = await _call(
                self._execution_queue, subject_id=subject_id, now=now
            )
        except Exception:
            return self._unavailable(application_plan_id, integrity=True)
        if queue.status is not CurrentApplicationExecutionQueueStatus.SUCCEEDED:
            return self._unavailable(application_plan_id, integrity=True)
        matches = tuple(
            item
            for item in queue.items
            if item.application_plan_id == application_plan_id
        )
        if len(matches) != 1:
            return self._unavailable(
                application_plan_id, integrity=len(matches) > 1
            )
        item = matches[0]
        if (
            item.subject_id != subject_id
            or item.execution_status
            is not CurrentApplicationExecutionStatus.DEFERRED
            or item.deferred_stage
            is not ApplicationExecutionStage.GATE_B_AUTHORIZATION
            or item.deferred_reason != "USER_AUTHORIZATION_REQUIRED"
            or item.execution_run_id is None
        ):
            return self._unavailable(application_plan_id)

        try:
            run_read = self._runs.get(
                subject_id=subject_id, run_id=item.execution_run_id
            )
            assembly_read = self._assemblies.get(
                subject_id=subject_id, record_id=item.assembly_record_id
            )
        except Exception:
            return self._unavailable(application_plan_id, integrity=True)
        if (
            run_read.status is not ApplicationExecutionRunReadStatus.FOUND
            or run_read.run is None
            or assembly_read.status
            is not ApplicationBundleAssemblyReadStatus.FOUND
            or assembly_read.record is None
        ):
            return self._unavailable(application_plan_id, integrity=True)
        run = run_read.run
        assembly = assembly_read.record
        if (
            run.subject_id != subject_id
            or run.application_plan_id != application_plan_id
            or run.job_id != item.job_id
            or run.assembly_record_id != item.assembly_record_id
            or run.assembly_record_hash != item.assembly_record_hash
            or run.non_submit_execution_record_id is None
            or assembly.subject_id != subject_id
            or assembly.application_plan_id != application_plan_id
            or assembly.job_id != item.job_id
            or assembly.record_content_hash != item.assembly_record_hash
        ):
            return self._unavailable(application_plan_id, integrity=True)

        try:
            record_read = self._non_submit.get(
                subject_id=subject_id,
                record_id=run.non_submit_execution_record_id,
            )
            answer_read = self._answers.get(
                subject_id=subject_id, answer_set_id=assembly.answer_set_id
            )
            job = self._jobs.get(item.job_id)
        except Exception:
            return self._unavailable(application_plan_id, integrity=True)
        if (
            record_read.status
            is not NonSubmitApplicationExecutionReadStatus.FOUND
            or record_read.record is None
            or answer_read.status
            is not PreparedApplicationAnswerSetReadStatus.FOUND
            or answer_read.answer_set is None
            or job is None
        ):
            return self._unavailable(application_plan_id, integrity=True)
        record = record_read.record
        answers = answer_read.answer_set
        if (
            record.subject_id != subject_id
            or record.application_plan_id != application_plan_id
            or record.job_id != item.job_id
            or record.assembly_record_id != item.assembly_record_id
            or record.assembly_record_content_hash != item.assembly_record_hash
            or record.execution_state
            is not NonSubmitExecutionRecordState.REVIEW_READY
            or record.submission_attempted
            or record.runtime_unresolved_controls
            or answers.subject_id != subject_id
            or answers.application_plan_id != application_plan_id
            or answers.job_id != item.job_id
            or answers.answer_set_id != assembly.answer_set_id
            or answers.answer_set_content_hash != assembly.answer_set_content_hash
            or any(value.blocking for value in answers.unresolved_items)
            or job.job_id != item.job_id
            or job.revision != record.job_revision
            or job.content_hash != record.job_content_hash
        ):
            return self._unavailable(application_plan_id, integrity=True)

        token = _hash(
            {
                "answer_set_hash": answers.answer_set_content_hash,
                "assembly_hash": assembly.record_content_hash,
                "execution_run_hash": run.run_hash,
                "queue_item_hash": item.item_hash,
                "review_digest_hash": record.outcome_checkpoint,
                "review_record_hash": record.record_content_hash,
                "subject_id": subject_id,
            }
        )
        review = ApplicationReviewUIResult(
            status=ApplicationReviewUIStatus.READY,
            application_plan_id=application_plan_id,
            job_id=item.job_id,
            title=job.title,
            company=job.company,
            location=job.location,
            ats_type=job.ats_type,
            routed_adapter=record.routed_adapter,
            reviewed_at=record.executed_at,
            review_fingerprint=record.outcome_checkpoint[:12],
            review_token=token,
            resume_included=True,
            cover_letter_included=True,
            prepared_answer_count=len(answers.answers),
            unresolved_control_count=0,
            message=(
                "The completed form and uploaded materials are ready for your "
                "final submission approval."
            ),
        )
        return _ReviewBinding(review, item, run, record)

    async def load(
        self,
        *,
        context: AuthenticatedSubjectContext,
        application_plan_id: str,
    ) -> ApplicationReviewUIResult:
        subject_id = self._subject(context)
        if (
            not isinstance(application_plan_id, str)
            or not application_plan_id.strip()
        ):
            raise ValueError("application_plan_id is required")
        binding = await self._load_binding(
            subject_id=subject_id,
            application_plan_id=application_plan_id.strip(),
            now=_aware(self._clock()),
        )
        return binding.review if isinstance(binding, _ReviewBinding) else binding

    async def submit(
        self,
        *,
        context: AuthenticatedSubjectContext,
        application_plan_id: str,
        review_token: str,
        confirmed: bool,
    ) -> ApplicationSubmissionUIResult:
        subject_id = self._subject(context)
        if not isinstance(application_plan_id, str):
            raise ValueError("application_plan_id is required")
        plan_id = application_plan_id.strip()
        if not plan_id or not isinstance(review_token, str) or not review_token:
            raise ValueError("current review confirmation is required")
        if confirmed is not True:
            raise ValueError("explicit confirmation is required")
        key = (subject_id, plan_id)
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return ApplicationSubmissionUIResult(
                ApplicationSubmissionUIStatus.IN_PROGRESS,
                plan_id,
                "This application submission is already in progress.",
                False,
            )

        async with lock:
            now = _aware(self._clock())
            binding = await self._load_binding(
                subject_id=subject_id,
                application_plan_id=plan_id,
                now=now,
            )
            if not isinstance(binding, _ReviewBinding):
                return ApplicationSubmissionUIResult(
                    ApplicationSubmissionUIStatus.NOT_AVAILABLE,
                    plan_id,
                    binding.message,
                    False,
                )
            if binding.review.review_token != review_token:
                return ApplicationSubmissionUIResult(
                    ApplicationSubmissionUIStatus.STALE_REVIEW,
                    plan_id,
                    "The application changed. Review the current version before submitting.",
                    False,
                )

            explicit = create_explicit_submission_authorization(
                subject_id=subject_id,
                application_plan_id=plan_id,
                non_submit_execution_record_id=(
                    binding.non_submit_record.record_id
                ),
                review_digest_hash=binding.non_submit_record.outcome_checkpoint,
                authorized_at=now,
            )
            try:
                result = await _call(
                    self._execute,
                    RunApplicationExecutionCommand(
                        subject_id=subject_id,
                        application_bundle_assembly_record_id=(
                            binding.queue_item.assembly_record_id
                        ),
                        now=now,
                        approve_gate_a=binding.execution_run.gate_a_approved,
                        explicit_user_authorization=explicit,
                    ),
                )
                status = ApplicationExecutionStatus(result.status)
            except Exception:
                return ApplicationSubmissionUIResult(
                    ApplicationSubmissionUIStatus.FAILED,
                    plan_id,
                    "The application could not be submitted safely.",
                    False,
                )
            if status in {
                ApplicationExecutionStatus.COMPLETED,
                ApplicationExecutionStatus.UNCHANGED,
            }:
                return ApplicationSubmissionUIResult(
                    ApplicationSubmissionUIStatus.SUBMITTED,
                    plan_id,
                    "Submission was completed and verified.",
                    False,
                )
            if status is ApplicationExecutionStatus.SUBMISSION_UNCERTAIN:
                return ApplicationSubmissionUIResult(
                    ApplicationSubmissionUIStatus.SUBMISSION_UNCERTAIN,
                    plan_id,
                    "Submission evidence is uncertain. JobOps will not retry automatically.",
                    False,
                )
            if status is ApplicationExecutionStatus.DEFERRED:
                return ApplicationSubmissionUIResult(
                    ApplicationSubmissionUIStatus.BLOCKED,
                    plan_id,
                    "Submission stopped at a required safety boundary.",
                    False,
                )
            return ApplicationSubmissionUIResult(
                ApplicationSubmissionUIStatus.FAILED,
                plan_id,
                "The application could not be submitted safely.",
                False,
            )


__all__ = [
    "APPLICATION_REVIEW_SUBMISSION_UI_CONTRACT_VERSION",
    "ApplicationReviewSubmissionUIController",
    "ApplicationReviewUIResult",
    "ApplicationReviewUIStatus",
    "ApplicationSubmissionUIResult",
    "ApplicationSubmissionUIStatus",
]
