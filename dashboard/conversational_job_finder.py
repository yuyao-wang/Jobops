"""Authenticated Dashboard adapter for the existing conversational intake."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from core.accepted_job_intent import AcceptedJobIntentRepository
from core.authenticated_subject import AuthenticatedSubjectContext
from core.conversational_intake import (
    CandidateSelectionRequest,
    ConversationalIntakeRequest,
    ConversationalIntakeResponse,
    InMemoryCandidateSelectionStore,
    InMemoryPendingIntakeStore,
    IntakeAction,
    NamedJobClueExtractor,
    NamedJobSearchResponse,
    ResolvePendingIntakeRequest,
    ResolvePendingIntakeResponse,
    handle_conversational_intake,
    resolve_pending_intake,
    select_search_candidate,
)
from core.job_search import JobSearchPort
from core.production_named_job_clue_extractor import (
    NamedJobClueOutputError,
    NamedJobClueExtractorRuntimeError,
)
from source_connectors.contract import ReadJobRequest

from dashboard.job_source_intake import (
    AssistedDiscoveryPlatform,
    AssistedJobImportCommand,
    AssistedJobImportController,
)


def _enum(value: object) -> str | None:
    return value.value if isinstance(value, Enum) else None


def _internal_conversation_id(subject_id: str, conversation_id: str) -> str:
    digest = hashlib.sha256(
        f"{subject_id}\0{conversation_id}".encode("utf-8")
    ).hexdigest()
    return f"dashboard-job-finder-{digest}"


def _single_platform_url(
    message: str,
) -> tuple[AssistedDiscoveryPlatform, str] | None:
    """Recognize one explicitly pasted aggregator-platform URL.

    This is deliberately not a general URL extractor.  Mixed prose, multiple
    URLs, non-HTTP schemes, credentials, and non-platform hosts stay on the
    ordinary bounded conversational path.  Most importantly, recognizing the
    URL never opens, navigates, or reads the platform page; the assisted intake
    controller records it only as an unverified lead.
    """

    if not isinstance(message, str):
        return None
    candidate = message.strip()
    if (
        not candidate
        or len(candidate) > 2_048
        or any(character.isspace() for character in candidate)
    ):
        return None
    try:
        parsed = urlsplit(candidate)
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return AssistedDiscoveryPlatform.LINKEDIN, candidate
    if host == "indeed.com" or host.endswith(".indeed.com"):
        return AssistedDiscoveryPlatform.INDEED, candidate
    if (
        host == "glassdoor.com"
        or host.endswith(".glassdoor.com")
        or host == "glassdoor.ca"
        or host.endswith(".glassdoor.ca")
    ):
        return AssistedDiscoveryPlatform.GLASSDOOR, candidate
    return None


def _summary(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "company": value.company,
        "title": value.title,
        "location": value.location,
        "source_platform": value.source_platform.value,
    }


def _intake_response(value: ConversationalIntakeResponse) -> dict[str, Any]:
    return {
        "kind": "INTAKE",
        "status": value.status.value,
        "reason": _enum(value.reason_code),
        "retryable": value.retryable,
        "prompt": value.prompt,
        "pending_intake_id": value.pending_intake_id,
        "pending_status": _enum(value.pending_status),
        "summary": _summary(value.summary),
        "actions": [action.value for action in value.actions],
        "intent_hint": value.intent_hint.value,
        "selected_candidate_id": value.selected_candidate_id,
    }


def _search_response(value: NamedJobSearchResponse) -> dict[str, Any]:
    return {
        "kind": "SEARCH",
        "status": value.status.value,
        "reason": _enum(value.reason_code),
        "retryable": value.retryable,
        "prompt": value.prompt,
        "candidate_set_id": value.candidate_set_id,
        "selection_status": _enum(value.selection_status),
        "intent_hint": value.intent_hint.value,
        "missing_fields": list(value.missing_fields),
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "company": candidate.company,
                "title": candidate.title,
                "location": candidate.location,
                "source_platform": candidate.source_platform.value,
                "source_url": candidate.source_url,
            }
            for candidate in value.candidates
        ],
    }


def _resolution_response(
    value: ResolvePendingIntakeResponse,
) -> dict[str, Any]:
    return {
        "kind": "RESOLUTION",
        "status": value.status.value,
        "reason": _enum(value.reason_code),
        "retryable": value.retryable,
        "prompt": value.prompt,
        "pending_intake_id": value.pending_intake_id,
        "selected_action": _enum(value.selected_action),
        "job_id": value.job_id,
        "change": _enum(value.change),
        "summary": _summary(value.summary),
    }


class ConversationalJobFinderUIController:
    """Keep NLP, search, selection, and subject-scoped writes separated."""

    def __init__(
        self,
        *,
        pending_store: InMemoryPendingIntakeStore,
        candidate_store: InMemoryCandidateSelectionStore,
        clue_extractor: NamedJobClueExtractor,
        job_search_port: JobSearchPort,
        public_job_reader: Callable[[ReadJobRequest], Any],
        accepted_intent_repository: AcceptedJobIntentRepository,
        discovery: Callable[[Any], Any],
        clock: Callable[[], datetime],
        assisted_import: AssistedJobImportController | None = None,
    ) -> None:
        if not isinstance(pending_store, InMemoryPendingIntakeStore):
            raise TypeError("pending_store is invalid")
        if not isinstance(candidate_store, InMemoryCandidateSelectionStore):
            raise TypeError("candidate_store is invalid")
        if not isinstance(clue_extractor, NamedJobClueExtractor):
            raise TypeError("clue_extractor is invalid")
        if not isinstance(job_search_port, JobSearchPort):
            raise TypeError("job_search_port is invalid")
        if not callable(public_job_reader) or not callable(discovery):
            raise TypeError("job finder callable is invalid")
        if not isinstance(
            accepted_intent_repository, AcceptedJobIntentRepository
        ):
            raise TypeError("accepted_intent_repository is invalid")
        if not callable(clock):
            raise TypeError("clock is invalid")
        self._pending_store = pending_store
        self._candidate_store = candidate_store
        self._clue_extractor = clue_extractor
        self._job_search_port = job_search_port
        self._public_job_reader = public_job_reader
        self._accepted_intent_repository = accepted_intent_repository
        self._discovery = discovery
        self._clock = clock
        if assisted_import is not None and not isinstance(
            assisted_import, AssistedJobImportController
        ):
            raise TypeError("assisted_import is invalid")
        self._assisted_import = assisted_import

    @staticmethod
    def _conversation(
        context: AuthenticatedSubjectContext,
        conversation_id: str,
    ) -> str:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("authenticated subject context is required")
        if (
            not isinstance(conversation_id, str)
            or not conversation_id.strip()
            or len(conversation_id) > 160
        ):
            raise ValueError("conversation_id is invalid")
        return _internal_conversation_id(
            context.subject_id, conversation_id.strip()
        )

    async def message(
        self,
        context: AuthenticatedSubjectContext,
        *,
        conversation_id: str,
        messages: tuple[str, ...],
    ) -> dict[str, Any]:
        internal_id = self._conversation(context, conversation_id)
        if (
            not isinstance(messages, tuple)
            or not 1 <= len(messages) <= 2
            or any(
                not isinstance(message, str)
                or not message.strip()
                or len(message) > 4_000
                for message in messages
            )
        ):
            raise ValueError("messages must contain one or two short turns")
        transcript = "\n".join(
            f"User turn {index}: {message.strip()}"
            for index, message in enumerate(messages, start=1)
        )
        platform_url = _single_platform_url(messages[-1])
        if platform_url is not None and self._assisted_import is not None:
            platform, url = platform_url
            imported = await self._assisted_import.import_job(
                context,
                AssistedJobImportCommand(
                    platform=platform,
                    job_url=url,
                    invocation_id=internal_id,
                ),
            )
            return {
                "kind": "LEAD",
                "status": "NEEDS_USER",
                "reason": imported.get("reason"),
                "retryable": False,
                "prompt": imported.get("message")
                or (
                    "This platform URL was saved as an unverified lead. Open "
                    "it yourself, then paste the official employer or ATS URL."
                ),
                "lead_id": imported.get("lead_id"),
                "lead_status": imported.get("lead_status"),
                "candidates": [],
                "missing_fields": [],
            }
        try:
            result = await handle_conversational_intake(
                ConversationalIntakeRequest(
                    conversation_id=internal_id,
                    message=transcript,
                ),
                pending_store=self._pending_store,
                candidate_store=self._candidate_store,
                clue_extractor=self._clue_extractor,
                job_search_port=self._job_search_port,
                reader=self._public_job_reader,
                clock=self._clock,
            )
        except NamedJobClueExtractorRuntimeError as exc:
            return {
                "kind": "SEARCH",
                "status": "FAILED",
                "reason": "AI_INTERPRETATION_FAILED",
                "retryable": exc.status.value in {"TIMEOUT", "PROCESS_FAILED"},
                "prompt": (
                    "The configured AI backend could not understand this job "
                    "request. No search or job-library write was performed."
                ),
                "ai_status": exc.status.value,
                "diagnostic_category": exc.diagnostic_category,
                "candidates": [],
                "missing_fields": [],
            }
        except NamedJobClueOutputError:
            return {
                "kind": "SEARCH",
                "status": "FAILED",
                "reason": "AI_OUTPUT_INVALID",
                "retryable": False,
                "prompt": (
                    "The AI response did not match the job-clue contract. "
                    "No search or job-library write was performed."
                ),
                "candidates": [],
                "missing_fields": [],
            }
        if isinstance(result, NamedJobSearchResponse):
            response = _search_response(result)
            if (
                len(messages) == 2
                and result.status.value == "NEEDS_USER"
                and result.candidate_set_id is None
            ):
                response.update(
                    status="FAILED",
                    reason="CLARIFICATION_LIMIT_REACHED",
                    retryable=False,
                    prompt=(
                        "JobOps still cannot identify one bounded search after "
                        "one clarification. Start a new request with a company "
                        "and job title, or paste one public employer/ATS URL."
                    ),
                )
            return response
        if isinstance(result, ConversationalIntakeResponse):
            return _intake_response(result)
        raise TypeError("conversational intake returned an invalid response")

    async def select_candidate(
        self,
        context: AuthenticatedSubjectContext,
        *,
        conversation_id: str,
        candidate_set_id: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        internal_id = self._conversation(context, conversation_id)
        result = await select_search_candidate(
            CandidateSelectionRequest(
                conversation_id=internal_id,
                candidate_set_id=candidate_set_id,
                candidate_id=candidate_id,
            ),
            candidate_store=self._candidate_store,
            pending_store=self._pending_store,
            reader=self._public_job_reader,
            clock=self._clock,
        )
        return _intake_response(result)

    def resolve(
        self,
        context: AuthenticatedSubjectContext,
        *,
        conversation_id: str,
        pending_intake_id: str,
        action: IntakeAction | str,
    ) -> dict[str, Any]:
        internal_id = self._conversation(context, conversation_id)
        result = resolve_pending_intake(
            ResolvePendingIntakeRequest(
                subject_id=context.subject_id,
                conversation_id=internal_id,
                pending_intake_id=pending_intake_id,
                action=action,
            ),
            pending_store=self._pending_store,
            accepted_intent_repository=self._accepted_intent_repository,
            discovery_port=self._discovery,
            clock=self._clock,
        )
        return _resolution_response(result)


__all__ = ["ConversationalJobFinderUIController"]
