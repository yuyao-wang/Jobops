"""Authenticated Dashboard boundary for reviewed job-priority preferences."""

from __future__ import annotations

from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.prioritization_policy import (
    ApprovePolicyRequest,
    CreatePolicyDraftRequest,
    HardConstraint,
    PolicyOperationStatus,
    PrioritizationPolicy,
    PrioritizationPolicyService,
    ReviseSoftPreferencesRequest,
    SoftPreference,
)


def _public_policy(policy: PrioritizationPolicy) -> dict[str, Any]:
    value = policy.to_dict()
    value.pop("subject_id", None)
    value.pop("policy_content_hash", None)
    return value


class PrioritizationPolicyUIController:
    """Keep subject identity server-side across interpret and approve actions."""

    def __init__(self, *, service: PrioritizationPolicyService) -> None:
        if not isinstance(service, PrioritizationPolicyService):
            raise TypeError("service must be PrioritizationPolicyService")
        self._service = service

    def read(self, context: AuthenticatedSubjectContext) -> dict[str, Any]:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        try:
            policy = self._service.get_active_policy(context.subject_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            return {
                "status": "FAILED",
                "policy": None,
                "message": "The approved job-preference policy could not be read safely.",
            }
        return {
            "status": "ACTIVE" if policy is not None else "EMPTY",
            "policy": _public_policy(policy) if policy is not None else None,
            "message": (
                "An approved job-preference policy is active."
                if policy is not None
                else "Describe and approve job preferences before Priority can rank jobs."
            ),
        }

    async def create_draft(
        self,
        context: AuthenticatedSubjectContext,
        *,
        raw_preference_text: str,
    ) -> dict[str, Any]:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        result = await self._service.create_policy_draft(
            CreatePolicyDraftRequest(
                subject_id=context.subject_id,
                raw_preference_text=raw_preference_text,
            )
        )
        return {
            "status": result.status.value,
            "reason": result.reason_code.value,
            "retryable": result.retryable,
            "message": result.message,
            "draft": result.draft.to_dict() if result.draft is not None else None,
        }

    def approve(
        self,
        context: AuthenticatedSubjectContext,
        *,
        draft_id: str,
        confirm_hard_constraints: bool,
    ) -> dict[str, Any]:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if type(confirm_hard_constraints) is not bool:
            raise TypeError("confirm_hard_constraints must be a boolean")
        draft = self._service.get_draft_for_review(
            subject_id=context.subject_id,
            draft_id=draft_id,
        )
        if draft is None:
            return {
                "status": PolicyOperationStatus.FAILED.value,
                "reason": "DRAFT_NOT_FOUND",
                "message": "The policy draft is missing or no longer belongs to this session.",
                "policy": None,
            }
        reviewed_hard = tuple(
            HardConstraint(
                constraint_type=item.constraint_type,
                normalized_value=item.normalized_value,
                source_excerpt=item.source_excerpt,
                user_confirmed=confirm_hard_constraints,
            )
            for item in draft.hard_constraints
        )
        result = self._service.approve_policy(
            ApprovePolicyRequest(
                draft_id=draft.draft_id,
                subject_id=context.subject_id,
                reviewed_raw_preference_text=draft.raw_preference_text,
                reviewed_hard_constraints=reviewed_hard,
                reviewed_soft_preferences=draft.soft_preferences,
                reviewed_preparation_admission=draft.preparation_admission,
            )
        )
        return {
            "status": result.status.value,
            "reason": result.reason_code.value if result.reason_code else None,
            "retryable": result.retryable,
            "message": result.message,
            "policy": (
                _public_policy(result.policy)
                if result.policy is not None
                else None
            ),
        }

    def revise_soft_preferences(
        self,
        context: AuthenticatedSubjectContext,
        *,
        expected_policy_version: int,
        preferences: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Persist exact user edits; category and identity remain server-owned."""

        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        active = self._service.get_active_policy(context.subject_id)
        if active is None:
            return {
                "status": PolicyOperationStatus.FAILED.value,
                "reason": "ACTIVE_POLICY_NOT_FOUND",
                "message": "No active preference policy is available to edit.",
                "policy": None,
            }
        if not isinstance(preferences, list) or any(
            not isinstance(item, dict)
            or set(item) != {"preference_id", "statement", "importance"}
            for item in preferences
        ):
            return {
                "status": PolicyOperationStatus.FAILED.value,
                "reason": "INVALID_REQUEST",
                "message": "The edited preference list is invalid.",
                "policy": None,
            }
        submitted = {item.get("preference_id"): item for item in preferences}
        if len(submitted) != len(preferences) or set(submitted) != {
            item.preference_id for item in active.soft_preferences
        }:
            return {
                "status": PolicyOperationStatus.FAILED.value,
                "reason": "INVALID_REQUEST",
                "message": "Reload the current preference list before saving.",
                "policy": None,
            }
        try:
            revised = tuple(
                SoftPreference(
                    preference_id=item.preference_id,
                    category=item.category,
                    statement=submitted[item.preference_id]["statement"],
                    importance=(
                        submitted[item.preference_id]["importance"] or None
                    ),
                    source_excerpt="Edited directly in Job Preferences.",
                )
                for item in active.soft_preferences
            )
            result = self._service.revise_soft_preferences(
                ReviseSoftPreferencesRequest(
                    subject_id=context.subject_id,
                    expected_policy_version=expected_policy_version,
                    soft_preferences=revised,
                )
            )
        except (AttributeError, TypeError, ValueError):
            return {
                "status": PolicyOperationStatus.FAILED.value,
                "reason": "INVALID_REQUEST",
                "message": "The edited preference values are invalid.",
                "policy": None,
            }
        return {
            "status": result.status.value,
            "reason": result.reason_code.value if result.reason_code else None,
            "retryable": result.retryable,
            "message": result.message,
            "policy": (
                _public_policy(result.policy)
                if result.policy is not None
                else None
            ),
        }


__all__ = ["PrioritizationPolicyUIController"]
