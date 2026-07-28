"""Bounded Greenhouse board listing search for configured companies."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

from core.job_search import (
    CandidateSet,
    JobSearchReason,
    JobSearchRequest,
    JobSearchResult,
    SearchCandidate,
)
from source_connectors.contract import SourcePlatform


_BOARD_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_GREENHOUSE_HOST_PATTERN = re.compile(
    r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io",
    re.IGNORECASE,
)
_MAX_RESPONSE_BYTES = 2_000_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_company(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalize_match_text(value: str) -> str:
    punctuation_as_spaces = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in value.casefold()
    )
    return " ".join(punctuation_as_spaces.split())


def _contains_phrase(container: str, phrase: str) -> bool:
    return f" {phrase} " in f" {container} "


def _normalized_text(value: Any, *, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} is outside the board response contract")
    return normalized


def _source_id(value: Any) -> str:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, str))
        or not str(value).strip()
        or len(str(value).strip()) > 240
    ):
        raise ValueError("job id is outside the board response contract")
    return str(value).strip()


def _location(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("location must be an object")
    name = value.get("name")
    if name is None or name == "":
        return None
    return _normalized_text(name, name="location.name", maximum=320)


def _greenhouse_source_url(
    value: Any,
    *,
    board_token: str,
    source_job_id: str,
) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("absolute_url is outside the board response contract")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("absolute_url is invalid") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or _GREENHOUSE_HOST_PATTERN.fullmatch(parsed.hostname) is None
    ):
        raise ValueError("absolute_url is not a supported Greenhouse job URL")
    expected_path = f"/{board_token}/jobs/{source_job_id}"
    if parsed.path.rstrip("/") != expected_path:
        raise ValueError("absolute_url does not match the board job")
    return value


@dataclass(frozen=True, slots=True)
class GreenhouseBoardConfig:
    canonical_company: str
    board_token: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.canonical_company, str)
            or not self.canonical_company.strip()
            or len(self.canonical_company.strip()) > 240
        ):
            raise ValueError("canonical_company must be non-empty")
        if (
            not isinstance(self.board_token, str)
            or _BOARD_TOKEN_PATTERN.fullmatch(self.board_token) is None
        ):
            raise ValueError("board_token is invalid")
        if not isinstance(self.aliases, tuple):
            raise TypeError("aliases must be a tuple")
        normalized_names = {_normalize_company(self.canonical_company)}
        for alias in self.aliases:
            if (
                not isinstance(alias, str)
                or not alias.strip()
                or len(alias.strip()) > 240
            ):
                raise ValueError("aliases must contain non-empty strings")
            normalized_alias = _normalize_company(alias)
            if normalized_alias in normalized_names:
                raise ValueError("company names and aliases must be unique")
            normalized_names.add(normalized_alias)


class GreenhouseBoardJobSearch:
    """Search one explicitly configured Greenhouse board with one HTTP GET."""

    def __init__(
        self,
        *,
        boards: Iterable[GreenhouseBoardConfig],
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        candidate_set_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._boards_by_name: dict[str, GreenhouseBoardConfig] = {}
        for board in boards:
            if not isinstance(board, GreenhouseBoardConfig):
                raise TypeError("boards must contain GreenhouseBoardConfig")
            for name in (board.canonical_company, *board.aliases):
                normalized = _normalize_company(name)
                if normalized in self._boards_by_name:
                    raise ValueError("company board names must be globally unique")
                self._boards_by_name[normalized] = board
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock or _utc_now
        self._candidate_set_id_factory = candidate_set_id_factory or (
            lambda: f"candidate-set-{uuid4()}"
        )

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        board = self._boards_by_name.get(_normalize_company(request.company))
        if board is None:
            return JobSearchResult.unsupported()

        api_url = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{quote(board.board_token, safe='')}/jobs"
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Jobops/1.0 GreenhouseBoardJobSearch",
                },
            ) as client:
                response = await client.get(api_url)
        except httpx.TimeoutException:
            return JobSearchResult.failed(JobSearchReason.SOURCE_TIMEOUT)
        except httpx.HTTPError:
            return JobSearchResult.failed(JobSearchReason.SOURCE_UNAVAILABLE)

        if response.status_code in {408, 504}:
            return JobSearchResult.failed(JobSearchReason.SOURCE_TIMEOUT)
        if response.status_code == 429:
            return JobSearchResult.failed(JobSearchReason.SOURCE_RATE_LIMITED)
        if response.status_code >= 500:
            return JobSearchResult.failed(JobSearchReason.SOURCE_UNAVAILABLE)
        if response.status_code != 200:
            return JobSearchResult.failed(
                JobSearchReason.SOURCE_UNAVAILABLE,
                retryable=False,
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            return JobSearchResult.failed(
                JobSearchReason.SOURCE_RESPONSE_INVALID
            )

        try:
            candidates = self._candidates_from_payload(
                payload=response.json(),
                board=board,
                request=request,
            )
            created_at = self._clock()
            candidate_set = CandidateSet(
                candidate_set_id=self._candidate_set_id_factory(),
                request_id=request.request_id,
                candidates=tuple(candidates[:10]),
                created_at=created_at,
            )
        except (TypeError, ValueError):
            return JobSearchResult.failed(
                JobSearchReason.SOURCE_RESPONSE_INVALID
            )
        return JobSearchResult.succeeded(candidate_set)

    @staticmethod
    def _candidates_from_payload(
        *,
        payload: Any,
        board: GreenhouseBoardConfig,
        request: JobSearchRequest,
    ) -> list[SearchCandidate]:
        if not isinstance(payload, Mapping):
            raise ValueError("Greenhouse board response must be an object")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise ValueError("Greenhouse board response must contain jobs")

        query_title = _normalize_match_text(request.title)
        query_location = (
            _normalize_match_text(request.location)
            if request.location is not None
            else None
        )
        ranked: list[tuple[int, str, str, SearchCandidate]] = []
        for item in jobs:
            if not isinstance(item, Mapping):
                raise ValueError("Greenhouse board jobs must be objects")
            source_job_id = _source_id(item.get("id"))
            title = _normalized_text(
                item.get("title"),
                name="title",
                maximum=240,
            )
            location = _location(item.get("location"))
            source_url = _greenhouse_source_url(
                item.get("absolute_url"),
                board_token=board.board_token,
                source_job_id=source_job_id,
            )

            normalized_title = _normalize_match_text(title)
            exact = normalized_title == query_title
            if not exact and not _contains_phrase(
                normalized_title,
                query_title,
            ):
                continue
            if query_location is not None:
                if location is None:
                    continue
                normalized_location = _normalize_match_text(location)
                if not (
                    _contains_phrase(normalized_location, query_location)
                    or _contains_phrase(query_location, normalized_location)
                ):
                    continue

            candidate = SearchCandidate(
                candidate_id=(
                    f"greenhouse:{board.board_token}:{source_job_id}"
                ),
                company=board.canonical_company.strip(),
                title=title,
                location=location,
                source_platform=SourcePlatform.GREENHOUSE,
                source_url=source_url,
                source_job_id=source_job_id,
            )
            ranked.append(
                (
                    0 if exact else 1,
                    normalized_title,
                    source_url,
                    candidate,
                )
            )

        ranked.sort(key=lambda item: item[:3])
        return [item[3] for item in ranked]


__all__ = ["GreenhouseBoardConfig", "GreenhouseBoardJobSearch"]
