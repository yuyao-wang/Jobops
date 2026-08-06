"""Authenticated UI bridge for current CSV-backed reviewed applications.

The CSV queue remains a compatibility projection while the canonical P2c
application model is being migrated.  This boundary exposes only the current
safe Review summary and delegates submission to the same permit-gated engine
used by ``jobctl submit-reviewed``.  It never fills or submits a form itself.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import inspect
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping

from core.authenticated_subject import AuthenticatedSubjectContext
from core.bundles import JobSpec, priority_to_tier
from core.event_ledger import EventLedger, RunRecord
from core.outcomes import ApplicationOutcome, OutcomeStatus
from core.private_home import PrivateHome
from adapters.protocol import REVIEW_BINDING_VERSION
from utils.csv_apply import CSVApplication, load_csv_queue


REVIEWED_APPLICATION_COMPATIBILITY_UI_CONTRACT_VERSION = (
    "reviewed-application-compatibility-ui-v1"
)
_VISIBLE_CSV_STATUSES = (
    "Ready for review,Needs user,Submitted,Submission unknown"
)
_VISIBLE_RUN_STATES = frozenset(
    {
        OutcomeStatus.REVIEW_READY.value,
        OutcomeStatus.AWAITING_GATE_B.value,
        OutcomeStatus.NEEDS_USER.value,
        OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER.value,
        OutcomeStatus.SUBMITTED_VERIFIED.value,
        OutcomeStatus.SUBMIT_UNKNOWN.value,
    }
)
_CSV_STATUS_BY_RUN_STATE = {
    OutcomeStatus.REVIEW_READY.value: "ready for review",
    OutcomeStatus.AWAITING_GATE_B.value: "needs user",
    OutcomeStatus.NEEDS_USER.value: "needs user",
    OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER.value: "needs user",
    OutcomeStatus.SUBMITTED_VERIFIED.value: "submitted",
    OutcomeStatus.SUBMIT_UNKNOWN.value: "submission unknown",
}
_PRODUCT_STATUS_BY_RUN_STATE = {
    OutcomeStatus.REVIEW_READY.value: "READY",
    OutcomeStatus.AWAITING_GATE_B.value: "READY",
    OutcomeStatus.NEEDS_USER.value: "READY",
    OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER.value: "READY",
    OutcomeStatus.SUBMITTED_VERIFIED.value: "SUBMITTED",
    OutcomeStatus.SUBMIT_UNKNOWN.value: "SUBMISSION_UNCERTAIN",
}
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ReviewedApplicationCompatibilityReadStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ReviewedApplicationCompatibilityReviewStatus(StrEnum):
    READY = "READY"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class ReviewedApplicationCompatibilitySubmissionStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    BLOCKED = "BLOCKED"
    STALE_REVIEW = "STALE_REVIEW"
    IN_PROGRESS = "IN_PROGRESS"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    FAILED = "FAILED"


def _canonical_hash(value: Mapping[str, Any]) -> str:
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


def _review_fingerprint(outcome: ApplicationOutcome) -> str:
    review = outcome.details.get("review")
    if isinstance(review, Mapping):
        value = str(review.get("fingerprint") or "").casefold()
        if _HEX_SHA256.fullmatch(value):
            return value
    for key in ("review_fingerprint", "form_fingerprint"):
        value = str(outcome.details.get(key) or "").casefold()
        if _HEX_SHA256.fullmatch(value):
            return value
    if outcome.checkpoint:
        value = str(outcome.checkpoint).casefold()
        if _HEX_SHA256.fullmatch(value):
            return value
    return ""


def _latest_review_outcome(
    ledger: EventLedger, run_id: str
) -> ApplicationOutcome | None:
    for event in reversed(ledger.list_events(run_id=run_id)):
        if event.event_type != "RUN_STATE_CHANGED":
            continue
        raw = event.payload.get("outcome")
        if not isinstance(raw, Mapping):
            continue
        if raw.get("status") != OutcomeStatus.REVIEW_READY.value:
            continue
        try:
            return ApplicationOutcome.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True, slots=True)
class ReviewedApplicationCompatibilityItem:
    run_id: str
    job_id: str
    company: str
    title: str
    location: str
    routed_adapter: str
    priority: str
    product_status: str
    safe_status_detail: str
    reviewed_at: str
    review_fingerprint: str
    prepared_answer_count: int
    unresolved_control_count: int
    uploaded_file_count: int
    resume_included: bool
    cover_letter_included: bool
    progress_steps: tuple[Mapping[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "cover_letter_included": self.cover_letter_included,
            "job_id": self.job_id,
            "location": self.location,
            "prepared_answer_count": self.prepared_answer_count,
            "priority": self.priority,
            "product_status": self.product_status,
            "progress_steps": [dict(step) for step in self.progress_steps],
            "resume_included": self.resume_included,
            "review_fingerprint": self.review_fingerprint[:12],
            "review_run_id": self.run_id,
            "reviewed_at": self.reviewed_at,
            "routed_adapter": self.routed_adapter,
            "safe_status_detail": self.safe_status_detail,
            "title": self.title,
            "unresolved_control_count": self.unresolved_control_count,
            "uploaded_file_count": self.uploaded_file_count,
        }


@dataclass(frozen=True, slots=True)
class ReviewedApplicationCompatibilityListResult:
    status: ReviewedApplicationCompatibilityReadStatus
    items: tuple[ReviewedApplicationCompatibilityItem, ...]
    message: str
    contract_version: str = (
        REVIEWED_APPLICATION_COMPATIBILITY_UI_CONTRACT_VERSION
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "items": [item.to_dict() for item in self.items],
            "message": self.message,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class ReviewedApplicationCompatibilityReviewResult:
    status: ReviewedApplicationCompatibilityReviewStatus
    item: ReviewedApplicationCompatibilityItem | None
    review_token: str | None
    message: str
    contract_version: str = (
        REVIEWED_APPLICATION_COMPATIBILITY_UI_CONTRACT_VERSION
    )

    def to_dict(self) -> dict[str, Any]:
        payload = self.item.to_dict() if self.item else {}
        payload.update(
            {
                "ats_type": (
                    self.item.routed_adapter if self.item else None
                ),
                "contract_version": self.contract_version,
                "message": self.message,
                "review_token": self.review_token,
                "status": self.status.value,
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class ReviewedApplicationCompatibilitySubmissionResult:
    status: ReviewedApplicationCompatibilitySubmissionStatus
    run_id: str
    message: str
    retry_allowed: bool
    contract_version: str = (
        REVIEWED_APPLICATION_COMPATIBILITY_UI_CONTRACT_VERSION
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "message": self.message,
            "retry_allowed": self.retry_allowed,
            "review_run_id": self.run_id,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class _ReviewedApplicationBinding:
    item: ReviewedApplicationCompatibilityItem
    run: RunRecord
    application: CSVApplication
    review_outcome: ApplicationOutcome
    review_token: str


def _progress(product_status: str) -> tuple[Mapping[str, str], ...]:
    stages = ("SELECTED", "PREPARING", "REVIEW", "READY", "SUBMITTED")
    completed_through = {
        "READY": 2,
        "SUBMITTED": 4,
        "SUBMISSION_UNCERTAIN": 3,
    }[product_status]
    current = {
        "READY": 3,
        "SUBMITTED": None,
        "SUBMISSION_UNCERTAIN": 4,
    }[product_status]
    values: list[Mapping[str, str]] = []
    for index, stage in enumerate(stages):
        if index <= completed_through:
            state = "COMPLETED"
        elif current == index:
            state = "BLOCKED" if product_status == "SUBMISSION_UNCERTAIN" else "CURRENT"
        else:
            state = "NOT_STARTED"
        values.append({"stage": stage, "state": state})
    return tuple(values)


class ReviewedApplicationCompatibilityUIController:
    """Project and submit current reviewed compatibility-queue records."""

    def __init__(
        self,
        *,
        home: PrivateHome,
        subject_id: str,
        submit_reviewed: Callable[[argparse.Namespace], Any],
        refresh_reviewed: Callable[[argparse.Namespace], Any],
        headless: bool,
        lease_ttl_seconds: int,
    ) -> None:
        if not isinstance(home, PrivateHome):
            raise TypeError("private home is unavailable")
        if not isinstance(subject_id, str) or not subject_id.strip():
            raise ValueError("subject_id is required")
        if not callable(submit_reviewed):
            raise TypeError("reviewed application submitter is unavailable")
        if not callable(refresh_reviewed):
            raise TypeError("review refresh callable is unavailable")
        if type(headless) is not bool:
            raise TypeError("headless must be boolean")
        if type(lease_ttl_seconds) is not int or lease_ttl_seconds < 1:
            raise ValueError("lease_ttl_seconds is invalid")
        self._home = home
        self._subject_id = subject_id.strip()
        self._submit_reviewed = submit_reviewed
        self._refresh_reviewed = refresh_reviewed
        self._headless = headless
        self._lease_ttl_seconds = lease_ttl_seconds
        self._locks: dict[str, asyncio.Lock] = {}

    def _authorize(self, context: AuthenticatedSubjectContext) -> None:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if context.subject_id != self._subject_id:
            raise ValueError("authenticated subject does not own this queue")

    def _bindings(self) -> tuple[_ReviewedApplicationBinding, ...]:
        paths = self._home.paths
        applications = load_csv_queue(
            paths.job_queue,
            paths.master_documents,
            priorities="High,Medium,Low",
            statuses=_VISIBLE_CSV_STATUSES,
            limit=0,
        )
        applications_by_job: dict[str, list[CSVApplication]] = {}
        for application in applications:
            try:
                job = JobSpec(
                    url=application.url,
                    company=application.company,
                    title=application.title,
                    tier=priority_to_tier(
                        application.row.get("priority", "")
                    ),
                )
            except ValueError:
                continue
            applications_by_job.setdefault(job.job_id, []).append(application)

        ledger = EventLedger(paths.event_ledger)
        run_ids = {event.run_id for event in ledger.list_events()}
        runs_by_job: dict[str, list[RunRecord]] = {}
        for run_id in run_ids:
            try:
                run = ledger.get_run(run_id)
            except Exception:
                continue
            if run.state in _VISIBLE_RUN_STATES:
                runs_by_job.setdefault(run.job_id, []).append(run)

        bindings: list[_ReviewedApplicationBinding] = []
        for job_id, matches in applications_by_job.items():
            if len(matches) != 1:
                continue
            application = matches[0]
            expected_csv_status = application.row.get("status", "").strip().casefold()
            candidates = [
                run
                for run in runs_by_job.get(job_id, ())
                if _CSV_STATUS_BY_RUN_STATE.get(run.state) == expected_csv_status
            ]
            if not candidates:
                continue
            candidates.sort(
                key=lambda value: (
                    value.updated_at,
                    value.state_version,
                    value.run_id,
                ),
                reverse=True,
            )
            run = candidates[0]
            review_outcome = _latest_review_outcome(ledger, run.run_id)
            if review_outcome is None:
                continue
            fingerprint = _review_fingerprint(review_outcome)
            review = review_outcome.details.get("review")
            if not fingerprint or not isinstance(review, Mapping):
                continue
            unresolved = review.get("unresolved_required") or ()
            validation_errors = review.get("validation_errors") or ()
            if not isinstance(unresolved, (list, tuple)) or not isinstance(
                validation_errors, (list, tuple)
            ):
                continue
            if unresolved or validation_errors:
                continue
            if review.get("ready") is not True or review.get(
                "submit_control_present"
            ) is not True:
                continue
            filled_fields = review.get("filled_fields") or ()
            uploaded_files = review.get("uploaded_files") or ()
            if not isinstance(filled_fields, (list, tuple)) or not isinstance(
                uploaded_files, (list, tuple)
            ):
                continue
            metadata = run.metadata
            resume_included = bool(metadata.get("resume_sha256")) and bool(
                uploaded_files
            )
            cover_strategy = str(
                metadata.get("cover_letter_strategy") or ""
            ).casefold()
            cover_letter_included = (
                cover_strategy not in {"", "none", "omit", "omitted"}
                and len(uploaded_files) >= 2
            )
            product_status = _PRODUCT_STATUS_BY_RUN_STATE[run.state]
            detail = {
                "READY": "Review complete — your approval is required before submission.",
                "SUBMITTED": "Submission completed and verified with eligible evidence.",
                "SUBMISSION_UNCERTAIN": "Submission evidence is uncertain; automatic retry is disabled.",
            }[product_status]
            item = ReviewedApplicationCompatibilityItem(
                run_id=run.run_id,
                job_id=job_id,
                company=application.company,
                title=application.title,
                location=application.row.get("location", "").strip(),
                routed_adapter=str(
                    review.get("adapter") or review_outcome.adapter or ""
                ),
                priority=application.row.get("priority", "").strip(),
                product_status=product_status,
                safe_status_detail=detail,
                reviewed_at=review_outcome.created_at,
                review_fingerprint=fingerprint,
                prepared_answer_count=len(filled_fields),
                unresolved_control_count=0,
                uploaded_file_count=len(uploaded_files),
                resume_included=resume_included,
                cover_letter_included=cover_letter_included,
                progress_steps=_progress(product_status),
            )
            token = _canonical_hash(
                {
                    "answer_hash": str(metadata.get("answer_hash") or ""),
                    "csv_identity": application.identity,
                    "csv_row_index": application.row_index,
                    "csv_status": expected_csv_status,
                    "job_id": job_id,
                    "material_hash": str(
                        metadata.get("material_hash") or ""
                    ),
                    "policy_hash": str(metadata.get("policy_hash") or ""),
                    "review_fingerprint": fingerprint,
                    "run_id": run.run_id,
                    "run_state": run.state,
                    "run_state_version": run.state_version,
                    "subject_id": self._subject_id,
                }
            )
            bindings.append(
                _ReviewedApplicationBinding(
                    item=item,
                    run=run,
                    application=application,
                    review_outcome=review_outcome,
                    review_token=token,
                )
            )
        bindings.sort(
            key=lambda value: (
                value.item.reviewed_at,
                value.item.run_id,
            ),
            reverse=True,
        )
        return tuple(bindings)

    def list(
        self, *, context: AuthenticatedSubjectContext
    ) -> ReviewedApplicationCompatibilityListResult:
        self._authorize(context)
        try:
            bindings = self._bindings()
        except Exception:
            return ReviewedApplicationCompatibilityListResult(
                ReviewedApplicationCompatibilityReadStatus.FAILED,
                (),
                "Reviewed applications could not be read safely.",
            )
        return ReviewedApplicationCompatibilityListResult(
            ReviewedApplicationCompatibilityReadStatus.SUCCEEDED,
            tuple(binding.item for binding in bindings),
            "Current reviewed applications are available.",
        )

    def load(
        self,
        *,
        context: AuthenticatedSubjectContext,
        run_id: str,
    ) -> ReviewedApplicationCompatibilityReviewResult:
        self._authorize(context)
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        try:
            matches = tuple(
                binding
                for binding in self._bindings()
                if binding.run.run_id == run_id.strip()
            )
        except Exception:
            return ReviewedApplicationCompatibilityReviewResult(
                ReviewedApplicationCompatibilityReviewStatus.INTEGRITY_FAILURE,
                None,
                None,
                "The current review could not be validated safely.",
            )
        if len(matches) != 1 or matches[0].run.state not in {
            OutcomeStatus.REVIEW_READY.value,
            OutcomeStatus.AWAITING_GATE_B.value,
        }:
            return ReviewedApplicationCompatibilityReviewResult(
                ReviewedApplicationCompatibilityReviewStatus.NOT_AVAILABLE,
                None,
                None,
                "This application is not awaiting final submission approval.",
            )
        binding = matches[0]
        return ReviewedApplicationCompatibilityReviewResult(
            ReviewedApplicationCompatibilityReviewStatus.READY,
            binding.item,
            binding.review_token,
            "The completed form and uploaded files are ready for your final submission approval.",
        )

    def _submission_args(self, run_id: str) -> argparse.Namespace:
        return argparse.Namespace(
            approve=True,
            csv="",
            headless=self._headless,
            home=str(self._home.paths.root),
            lease_ttl=float(self._lease_ttl_seconds),
            resume_dir="",
            run_id=run_id,
            semantic_mapper=False,
        )

    async def prepare_current_review(
        self,
        *,
        context: AuthenticatedSubjectContext,
        run_id: str,
    ) -> ReviewedApplicationCompatibilityReviewResult:
        """Refresh an obsolete/mismatched Review without requesting submit."""

        self._authorize(context)
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        normalized_run_id = run_id.strip()
        lock = self._locks.setdefault(normalized_run_id, asyncio.Lock())
        if lock.locked():
            return ReviewedApplicationCompatibilityReviewResult(
                ReviewedApplicationCompatibilityReviewStatus.NOT_AVAILABLE,
                None,
                None,
                "This application is already being refreshed or submitted.",
            )
        async with lock:
            try:
                matches = tuple(
                    binding
                    for binding in self._bindings()
                    if binding.run.run_id == normalized_run_id
                )
            except Exception:
                matches = ()
            if len(matches) != 1:
                return ReviewedApplicationCompatibilityReviewResult(
                    ReviewedApplicationCompatibilityReviewStatus.INTEGRITY_FAILURE,
                    None,
                    None,
                    "The current review could not be validated safely.",
                )
            binding = matches[0]
            review = binding.review_outcome.details.get("review")
            current_binding = (
                isinstance(review, Mapping)
                and review.get("binding_version") == REVIEW_BINDING_VERSION
                and binding.run.state == OutcomeStatus.REVIEW_READY.value
            )
            if not current_binding:
                try:
                    outcome = await _call(
                        self._refresh_reviewed,
                        self._submission_args(normalized_run_id),
                    )
                except Exception:
                    return ReviewedApplicationCompatibilityReviewResult(
                        ReviewedApplicationCompatibilityReviewStatus.INTEGRITY_FAILURE,
                        None,
                        None,
                        "The current application Review could not be refreshed safely.",
                    )
                if (
                    not isinstance(outcome, ApplicationOutcome)
                    or outcome.status is not OutcomeStatus.REVIEW_READY
                ):
                    return ReviewedApplicationCompatibilityReviewResult(
                        ReviewedApplicationCompatibilityReviewStatus.NOT_AVAILABLE,
                        None,
                        None,
                        outcome.message
                        if isinstance(outcome, ApplicationOutcome)
                        else "The application did not reach Review.",
                    )
            return self.load(context=context, run_id=normalized_run_id)

    async def submit(
        self,
        *,
        context: AuthenticatedSubjectContext,
        run_id: str,
        review_token: str,
        confirmed: bool,
    ) -> ReviewedApplicationCompatibilitySubmissionResult:
        self._authorize(context)
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        if not isinstance(review_token, str) or not review_token:
            raise ValueError("current review confirmation is required")
        if confirmed is not True:
            raise ValueError("explicit confirmation is required")
        normalized_run_id = run_id.strip()
        lock = self._locks.setdefault(normalized_run_id, asyncio.Lock())
        if lock.locked():
            return ReviewedApplicationCompatibilitySubmissionResult(
                ReviewedApplicationCompatibilitySubmissionStatus.IN_PROGRESS,
                normalized_run_id,
                "This application submission is already in progress.",
                False,
            )
        async with lock:
            review = self.load(context=context, run_id=normalized_run_id)
            if review.status is not ReviewedApplicationCompatibilityReviewStatus.READY:
                return ReviewedApplicationCompatibilitySubmissionResult(
                    ReviewedApplicationCompatibilitySubmissionStatus.NOT_AVAILABLE,
                    normalized_run_id,
                    review.message,
                    False,
                )
            if not hmac.compare_digest(review.review_token or "", review_token):
                return ReviewedApplicationCompatibilitySubmissionResult(
                    ReviewedApplicationCompatibilitySubmissionStatus.STALE_REVIEW,
                    normalized_run_id,
                    "The application changed. Review the current version before submitting.",
                    False,
                )
            try:
                outcome = await _call(
                    self._submit_reviewed,
                    self._submission_args(normalized_run_id),
                )
            except Exception:
                return ReviewedApplicationCompatibilitySubmissionResult(
                    ReviewedApplicationCompatibilitySubmissionStatus.FAILED,
                    normalized_run_id,
                    "The application could not be submitted safely.",
                    False,
                )
            if not isinstance(outcome, ApplicationOutcome):
                return ReviewedApplicationCompatibilitySubmissionResult(
                    ReviewedApplicationCompatibilitySubmissionStatus.FAILED,
                    normalized_run_id,
                    "The application engine returned an invalid result.",
                    False,
                )
            if outcome.status is OutcomeStatus.SUBMITTED_VERIFIED:
                return ReviewedApplicationCompatibilitySubmissionResult(
                    ReviewedApplicationCompatibilitySubmissionStatus.SUBMITTED,
                    normalized_run_id,
                    "Submission was completed and verified.",
                    False,
                )
            if outcome.status is OutcomeStatus.SUBMIT_UNKNOWN:
                return ReviewedApplicationCompatibilitySubmissionResult(
                    ReviewedApplicationCompatibilitySubmissionStatus.SUBMISSION_UNCERTAIN,
                    normalized_run_id,
                    "Submission evidence is uncertain. JobOps will not retry automatically.",
                    False,
                )
            return ReviewedApplicationCompatibilitySubmissionResult(
                ReviewedApplicationCompatibilitySubmissionStatus.BLOCKED,
                normalized_run_id,
                outcome.message or "Submission stopped at a required safety boundary.",
                False,
            )


__all__ = [
    "REVIEWED_APPLICATION_COMPATIBILITY_UI_CONTRACT_VERSION",
    "ReviewedApplicationCompatibilityListResult",
    "ReviewedApplicationCompatibilityReadStatus",
    "ReviewedApplicationCompatibilityReviewResult",
    "ReviewedApplicationCompatibilityReviewStatus",
    "ReviewedApplicationCompatibilitySubmissionResult",
    "ReviewedApplicationCompatibilitySubmissionStatus",
    "ReviewedApplicationCompatibilityUIController",
]
