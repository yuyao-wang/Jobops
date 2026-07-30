"""Production bounded Greenhouse board listing search."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote, urljoin, urlsplit

import httpx

from core.job_search import (
    CandidateSet,
    JobSearchReason,
    JobSearchRequest,
    JobSearchResult,
    SearchCandidate,
    canonicalize_search_company,
    canonicalize_search_match_text,
)
from source_connectors.contract import SourcePlatform


_BOARD_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_GREENHOUSE_HOST_PATTERN = re.compile(
    r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io",
    re.IGNORECASE,
)
_MAX_RESPONSE_BYTES = 2_000_000
GREENHOUSE_JOB_SEARCH_ADAPTER_VERSION = "greenhouse-board-search-v2"
JOB_SEARCH_EXECUTION_POLICY_VERSION = "job-search-execution-policy-v1"
_GREENHOUSE_API_HOST = "boards-api.greenhouse.io"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True, slots=True)
class JobSearchExecutionPolicy:
    """Versioned server-owned bounds for provider listing requests."""

    contract_version: str = JOB_SEARCH_EXECUTION_POLICY_VERSION
    max_queries_per_refresh: int = 20
    max_results_per_query: int = 10
    max_response_bytes: int = _MAX_RESPONSE_BYTES
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    max_redirects: int = 2
    max_concurrent_requests: int = 1
    allowed_providers: tuple[str, ...] = ("GREENHOUSE",)
    user_agent_version: str = "Jobops/2 GreenhouseBoardJobSearch"

    def __post_init__(self) -> None:
        if self.contract_version != JOB_SEARCH_EXECUTION_POLICY_VERSION:
            raise ValueError("unsupported job search policy version")
        for name, value, maximum in (
            ("max_queries_per_refresh", self.max_queries_per_refresh, 100),
            ("max_results_per_query", self.max_results_per_query, 10),
            ("max_response_bytes", self.max_response_bytes, 25_000_000),
            ("max_redirects", self.max_redirects, 5),
            ("max_concurrent_requests", self.max_concurrent_requests, 8),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside the server policy")
        for name, value in (
            ("connect_timeout_seconds", self.connect_timeout_seconds),
            ("read_timeout_seconds", self.read_timeout_seconds),
        ):
            if not isinstance(value, (int, float)) or not 0 < value <= 60:
                raise ValueError(f"{name} is outside the server policy")
        if (
            not isinstance(self.allowed_providers, tuple)
            or not self.allowed_providers
            or any(
                provider not in {"GREENHOUSE", "LEVER"}
                for provider in self.allowed_providers
            )
            or len(set(self.allowed_providers)) != len(self.allowed_providers)
        ):
            raise ValueError("allowed_providers is invalid")
        if (
            not isinstance(self.user_agent_version, str)
            or not self.user_agent_version.strip()
            or len(self.user_agent_version) > 120
        ):
            raise ValueError("user_agent_version is invalid")


class BoundedHttpStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    TIMEOUT = "TIMEOUT"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    REDIRECT_REJECTED = "REDIRECT_REJECTED"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"


@dataclass(frozen=True, slots=True)
class BoundedHttpRequest:
    url: str
    allowed_hosts: tuple[str, ...]
    headers: Mapping[str, str]
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_redirects: int
    max_response_bytes: int


@dataclass(frozen=True, slots=True)
class BoundedHttpResult:
    status: BoundedHttpStatus
    response_status: int | None = None
    headers: Mapping[str, str] | None = None
    content: bytes | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", BoundedHttpStatus(self.status))
        if self.status is BoundedHttpStatus.SUCCEEDED:
            if (
                type(self.response_status) is not int
                or self.headers is None
                or self.content is None
            ):
                raise ValueError("successful HTTP result is incomplete")
        elif any(
            value is not None
            for value in (self.response_status, self.headers, self.content)
        ):
            raise ValueError("failed HTTP result cannot contain response data")


@runtime_checkable
class BoundedJobSearchHttpPort(Protocol):
    async def get(self, request: BoundedHttpRequest) -> BoundedHttpResult:
        """Perform one bounded credential-free HTTP GET."""


class HttpxBoundedJobSearchHttpClient:
    """HTTPX transport with explicit redirect and decoded-byte bounds."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._transport = transport

    async def get(self, request: BoundedHttpRequest) -> BoundedHttpResult:
        current_url = request.url
        timeout = httpx.Timeout(
            connect=request.connect_timeout_seconds,
            read=request.read_timeout_seconds,
            write=request.connect_timeout_seconds,
            pool=request.connect_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                for redirect_count in range(request.max_redirects + 1):
                    parsed = urlsplit(current_url)
                    if (
                        parsed.scheme != "https"
                        or parsed.hostname not in request.allowed_hosts
                        or parsed.username is not None
                        or parsed.password is not None
                        or parsed.port is not None
                    ):
                        return BoundedHttpResult(
                            BoundedHttpStatus.REDIRECT_REJECTED
                        )
                    response = await client.send(
                        client.build_request(
                            "GET",
                            current_url,
                            headers=dict(request.headers),
                        ),
                        stream=True,
                    )
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        await response.aclose()
                        if (
                            redirect_count == request.max_redirects
                            or not location
                        ):
                            return BoundedHttpResult(
                                BoundedHttpStatus.REDIRECT_REJECTED
                            )
                        current_url = urljoin(current_url, location)
                        continue

                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > request.max_response_bytes:
                                await response.aclose()
                                return BoundedHttpResult(
                                    BoundedHttpStatus.RESPONSE_TOO_LARGE
                                )
                        except ValueError:
                            await response.aclose()
                            return BoundedHttpResult(
                                BoundedHttpStatus.NETWORK_UNAVAILABLE
                            )
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > request.max_response_bytes:
                            await response.aclose()
                            return BoundedHttpResult(
                                BoundedHttpStatus.RESPONSE_TOO_LARGE
                            )
                        chunks.append(chunk)
                    headers = {
                        key.casefold(): value
                        for key, value in response.headers.items()
                    }
                    status_code = response.status_code
                    await response.aclose()
                    return BoundedHttpResult(
                        BoundedHttpStatus.SUCCEEDED,
                        response_status=status_code,
                        headers=headers,
                        content=b"".join(chunks),
                    )
        except httpx.TimeoutException:
            return BoundedHttpResult(BoundedHttpStatus.TIMEOUT)
        except httpx.HTTPError:
            return BoundedHttpResult(BoundedHttpStatus.NETWORK_UNAVAILABLE)
        return BoundedHttpResult(BoundedHttpStatus.REDIRECT_REJECTED)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _candidate_set_id(
    request: JobSearchRequest,
    board: "GreenhouseBoardConfig",
    candidates: Iterable[SearchCandidate],
) -> str:
    payload = {
        "contract_version": GREENHOUSE_JOB_SEARCH_ADAPTER_VERSION,
        "request": {
            "company": canonicalize_search_company(request.company),
            "location": (
                canonicalize_search_match_text(
                    request.location,
                    name="location",
                    maximum=320,
                )
                if request.location is not None
                else None
            ),
            "request_id": request.request_id,
            "title": canonicalize_search_match_text(
                request.title,
                name="title",
                maximum=240,
            ),
        },
        "board_token": board.board_token,
        "candidate_ids": [
            {
                "candidate_id": candidate.candidate_id,
                "source_job_id": candidate.source_job_id,
                "source_url": candidate.source_url,
            }
            for candidate in candidates
        ],
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"candidate-set-{digest[:32]}"


class _CandidateValidationError(ValueError):
    pass


def _normalize_company(value: str) -> str:
    return canonicalize_search_company(value)


def _normalize_match_text(value: str) -> str:
    return canonicalize_search_match_text(
        value,
        name="search match text",
        maximum=320,
    )


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
        parsed.scheme.casefold() != "https"
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
        http_port: BoundedJobSearchHttpPort | None = None,
        policy: JobSearchExecutionPolicy | None = None,
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
        if not self._boards_by_name:
            raise ValueError("at least one Greenhouse board is required")
        if http_port is not None and transport is not None:
            raise ValueError("http_port and transport cannot both be provided")
        if http_port is not None and not isinstance(
            http_port, BoundedJobSearchHttpPort
        ):
            raise TypeError("http_port must implement BoundedJobSearchHttpPort")
        self._policy = policy or JobSearchExecutionPolicy(
            connect_timeout_seconds=timeout_seconds,
            read_timeout_seconds=timeout_seconds,
        )
        if "GREENHOUSE" not in self._policy.allowed_providers:
            raise ValueError("Greenhouse is disabled by the search policy")
        self._http_port = http_port or HttpxBoundedJobSearchHttpClient(
            transport=transport
        )
        self._clock = clock or _utc_now
        self._candidate_set_id_factory = candidate_set_id_factory

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        board = self._boards_by_name.get(_normalize_company(request.company))
        if board is None:
            return JobSearchResult.unsupported()

        api_url = (
            "https://boards-api.greenhouse.io/v1/boards/"
            f"{quote(board.board_token, safe='')}/jobs"
        )
        response = await self._http_port.get(
            BoundedHttpRequest(
                url=api_url,
                allowed_hosts=(_GREENHOUSE_API_HOST,),
                headers={
                    "Accept": "application/json",
                    "User-Agent": self._policy.user_agent_version,
                },
                connect_timeout_seconds=(
                    self._policy.connect_timeout_seconds
                ),
                read_timeout_seconds=self._policy.read_timeout_seconds,
                max_redirects=self._policy.max_redirects,
                max_response_bytes=self._policy.max_response_bytes,
            )
        )
        if not isinstance(response, BoundedHttpResult):
            return JobSearchResult.failed(
                JobSearchReason.NETWORK_UNAVAILABLE
            )
        if response.status is BoundedHttpStatus.TIMEOUT:
            return JobSearchResult.failed(JobSearchReason.SOURCE_TIMEOUT)
        if response.status is BoundedHttpStatus.NETWORK_UNAVAILABLE:
            return JobSearchResult.failed(JobSearchReason.NETWORK_UNAVAILABLE)
        if response.status is BoundedHttpStatus.REDIRECT_REJECTED:
            return JobSearchResult.failed(JobSearchReason.REDIRECT_REJECTED)
        if response.status is BoundedHttpStatus.RESPONSE_TOO_LARGE:
            return JobSearchResult.failed(JobSearchReason.RESPONSE_TOO_LARGE)
        if (
            response.response_status is None
            or response.headers is None
            or response.content is None
        ):
            return JobSearchResult.failed(
                JobSearchReason.NETWORK_UNAVAILABLE
            )

        if response.response_status in {408, 504}:
            return JobSearchResult.failed(JobSearchReason.SOURCE_TIMEOUT)
        if response.response_status == 429:
            return JobSearchResult.failed(JobSearchReason.SOURCE_RATE_LIMITED)
        if response.response_status >= 500:
            return JobSearchResult.failed(
                JobSearchReason.HTTP_ERROR,
                retryable=True,
            )
        if response.response_status != 200:
            return JobSearchResult.failed(
                JobSearchReason.HTTP_ERROR,
                retryable=False,
            )
        content_type = response.headers.get("content-type", "")
        if content_type.partition(";")[0].strip().casefold() not in {
            "application/json",
            "application/vnd.api+json",
        }:
            return JobSearchResult.failed(
                JobSearchReason.UNSUPPORTED_CONTENT_TYPE
            )

        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return JobSearchResult.failed(JobSearchReason.MALFORMED_RESPONSE)
        try:
            candidates = self._candidates_from_payload(
                payload=payload,
                board=board,
                request=request,
            )
        except _CandidateValidationError:
            return JobSearchResult.failed(
                JobSearchReason.CANDIDATE_VALIDATION_FAILED
            )
        except (TypeError, ValueError):
            return JobSearchResult.failed(JobSearchReason.MALFORMED_RESPONSE)
        try:
            bounded_candidates = tuple(
                candidates[: self._policy.max_results_per_query]
            )
            created_at = self._clock()
            candidate_set = CandidateSet(
                candidate_set_id=(
                    self._candidate_set_id_factory()
                    if self._candidate_set_id_factory is not None
                    else _candidate_set_id(
                        request,
                        board,
                        bounded_candidates,
                    )
                ),
                request_id=request.request_id,
                candidates=bounded_candidates,
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
        candidates_by_source_id: dict[str, SearchCandidate] = {}
        for item in jobs:
            if not isinstance(item, Mapping):
                raise _CandidateValidationError(
                    "Greenhouse board jobs must be objects"
                )
            try:
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
            except ValueError as exc:
                raise _CandidateValidationError(
                    "Greenhouse candidate is invalid"
                ) from exc

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
            existing = candidates_by_source_id.get(source_job_id)
            if existing is not None:
                if existing.source_url != candidate.source_url:
                    raise _CandidateValidationError(
                        "job ID has conflicting canonical URLs"
                    )
                continue
            candidates_by_source_id[source_job_id] = candidate
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


__all__ = [
    "BoundedHttpRequest",
    "BoundedHttpResult",
    "BoundedHttpStatus",
    "BoundedJobSearchHttpPort",
    "GREENHOUSE_JOB_SEARCH_ADAPTER_VERSION",
    "GreenhouseBoardConfig",
    "GreenhouseBoardJobSearch",
    "HttpxBoundedJobSearchHttpClient",
    "JOB_SEARCH_EXECUTION_POLICY_VERSION",
    "JobSearchExecutionPolicy",
]
