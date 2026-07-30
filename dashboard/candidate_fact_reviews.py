"""Authenticated UI adapter for Candidate Fact review and verification."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from core.authenticated_subject import AuthenticatedSubjectContext
from core.candidate_fact_proposals import CandidateFactProposalRepository
from core.candidate_fact_reviews import (
    BuildCandidateFactReviewQueueCommand,
    CandidateFactReviewAction,
    CandidateFactReviewDecisionRepository,
    CandidateFactReviewDecisionStatus,
    CandidateFactReviewItem,
    CandidateFactReviewPreviewKind,
    CandidateFactReviewQueueStatus,
    ResolveCandidateFactReviewCommand,
    build_candidate_fact_review_queue,
    read_candidate_fact_review_asset,
    resolve_candidate_fact_review,
)
from core.candidate_identity_facts import CandidateIdentityFactRepository
from core.candidate_source_projections import CandidateSourceProjectionRepository


@dataclass(frozen=True, slots=True)
class CandidateFactReviewUIResult:
    status: str
    queue_snapshot_hash: str | None
    items: tuple[dict[str, Any], ...]
    counts: dict[str, int]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "counts": dict(self.counts),
            "items": [dict(item) for item in self.items],
            "message": self.message,
            "queue_snapshot_hash": self.queue_snapshot_hash,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class CandidateFactReviewResolutionUIResult:
    status: str
    decision_id: str | None
    created_fact_id: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_fact_id": self.created_fact_id,
            "decision_id": self.decision_id,
            "message": self.message,
            "status": self.status,
        }


def _safe(value: str | None) -> str | None:
    return html.escape(value, quote=True) if value is not None else None


def _locator_label(locator: dict[str, Any]) -> str:
    parts = []
    for key, label in (
        ("page_number", "page"),
        ("slide_number", "slide"),
        ("paragraph_index", "paragraph"),
        ("block_index", "block"),
    ):
        value = locator.get(key)
        if value is not None:
            parts.append(f"{label} {value}")
    return ", ".join(parts) or "source root"


def _proposal_to_dict(item, review_item_id: str) -> dict[str, Any]:
    return {
        "confidence": item.confidence.value,
        "previews": [
            {
                "asset_url": (
                    "/api/candidate-facts/review/"
                    f"{review_item_id}/assets/{preview.evidence_id}"
                    if preview.preview_kind
                    is CandidateFactReviewPreviewKind.IMAGE
                    else None
                ),
                "evidence_id": preview.evidence_id,
                "height": preview.height,
                "kind": preview.preview_kind.value,
                "locator": _safe(_locator_label(dict(preview.source_locator))),
                "media_type": preview.media_type,
                "text_excerpt": _safe(preview.text_excerpt),
                "width": preview.width,
            }
            for preview in item.previews
        ],
        "projection_id": item.projection_id,
        "proposal_id": item.proposal_id,
        "proposed_value": _safe(item.proposed_value),
        "source_display_name": _safe(
            f"Managed {item.source_kind.casefold()} candidate source"
        ),
        "source_id": item.source_id,
    }


def _item_to_dict(item: CandidateFactReviewItem) -> dict[str, Any]:
    return {
        "available_actions": [
            action.value for action in item.available_actions
        ],
        "conflicting_proposals": [
            _proposal_to_dict(proposal, item.review_item_id)
            for proposal in item.conflicting_proposals
        ],
        "current_fact_id": item.current_fact_id,
        "current_value": _safe(item.current_value),
        "field_key": item.field_key.value,
        "field_label": item.field_key.value.replace("_", " ").title(),
        "input_type": (
            "email"
            if item.field_key.value == "email"
            else "tel"
            if item.field_key.value == "phone"
            else "url"
            if item.field_key.value in {"linkedin", "github", "portfolio"}
            else "text"
        ),
        "item_kind": item.item_kind.value,
        "priority": item.priority,
        "proposal": (
            _proposal_to_dict(item.proposal, item.review_item_id)
            if item.proposal is not None
            else None
        ),
        "review_item_id": item.review_item_id,
    }


class CandidateFactReviewUIController:
    def __init__(
        self,
        *,
        proposal_repository: CandidateFactProposalRepository,
        current_fact_repository: CandidateIdentityFactRepository,
        projection_repository: CandidateSourceProjectionRepository,
        decision_repository: CandidateFactReviewDecisionRepository,
        clock: Callable[[], datetime],
    ) -> None:
        self._proposals = proposal_repository
        self._facts = current_fact_repository
        self._projections = projection_repository
        self._decisions = decision_repository
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        return value

    def load(
        self, *, context: AuthenticatedSubjectContext
    ) -> CandidateFactReviewUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        result = build_candidate_fact_review_queue(
            BuildCandidateFactReviewQueueCommand(
                context.subject_id, self._now()
            ),
            proposal_repository=self._proposals,
            current_fact_repository=self._facts,
            projection_repository=self._projections,
            decision_repository=self._decisions,
        )
        if (
            result.status is not CandidateFactReviewQueueStatus.SUCCEEDED
            or result.queue is None
        ):
            return CandidateFactReviewUIResult(
                "FAILED",
                None,
                (),
                {"pending": 0, "conflicts": 0, "missing_required": 0, "resolved": 0},
                "Candidate fact review is temporarily unavailable.",
            )
        queue = result.queue
        return CandidateFactReviewUIResult(
            "EMPTY" if not queue.items else "READY",
            queue.queue_snapshot_hash,
            tuple(_item_to_dict(item) for item in queue.items),
            {
                "pending": queue.pending_count,
                "conflicts": queue.conflict_count,
                "missing_required": queue.missing_required_count,
                "resolved": queue.resolved_count,
            },
            (
                "No candidate facts require review."
                if not queue.items
                else "Review each candidate fact explicitly."
            ),
        )

    def resolve(
        self,
        *,
        context: AuthenticatedSubjectContext,
        review_item_id: str,
        queue_snapshot_hash: str,
        action: CandidateFactReviewAction,
        invocation_id: str,
        submitted_value: str | None,
    ) -> CandidateFactReviewResolutionUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        result = resolve_candidate_fact_review(
            ResolveCandidateFactReviewCommand(
                context.subject_id,
                review_item_id,
                queue_snapshot_hash,
                action,
                invocation_id,
                self._now(),
                submitted_value,
            ),
            proposal_repository=self._proposals,
            current_fact_repository=self._facts,
            projection_repository=self._projections,
            decision_repository=self._decisions,
        )
        messages = {
            CandidateFactReviewDecisionStatus.COMPLETED: "Candidate fact review saved.",
            CandidateFactReviewDecisionStatus.UNCHANGED: "Candidate fact review was already saved.",
            CandidateFactReviewDecisionStatus.STALE_REVIEW: "Candidate facts changed; refresh before deciding.",
            CandidateFactReviewDecisionStatus.INVALID: "Candidate fact value or action is invalid.",
            CandidateFactReviewDecisionStatus.PARTIAL_FAILURE: "The review could not be completed safely.",
            CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE: "Candidate fact bindings failed integrity validation.",
            CandidateFactReviewDecisionStatus.FAILED: "Candidate fact review is temporarily unavailable.",
        }
        return CandidateFactReviewResolutionUIResult(
            result.status.value,
            result.decision.decision_id if result.decision else None,
            result.decision.created_fact_id if result.decision else None,
            messages[result.status],
        )

    def read_asset(
        self,
        *,
        context: AuthenticatedSubjectContext,
        review_item_id: str,
        evidence_id: str,
    ):
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        return read_candidate_fact_review_asset(
            subject_id=context.subject_id,
            review_item_id=review_item_id,
            evidence_id=evidence_id,
            now=self._now(),
            proposal_repository=self._proposals,
            current_fact_repository=self._facts,
            projection_repository=self._projections,
            decision_repository=self._decisions,
        )


__all__ = [
    "CandidateFactReviewResolutionUIResult",
    "CandidateFactReviewUIController",
    "CandidateFactReviewUIResult",
]
