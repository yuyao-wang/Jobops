"""Authorized, bounded web-search discovery ports.

This module performs search-index discovery only.  It never opens, scrapes, or
operates a LinkedIn or Indeed page.  Results are unverified leads; callers must
resolve and read an employer or ATS posting before creating a normalized job.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from source_connectors.greenhouse_board import (
    BoundedHttpRequest,
    BoundedHttpResult,
    BoundedHttpStatus,
    BoundedJobSearchHttpPort,
    HttpxBoundedJobSearchHttpClient,
)


AUTHORIZED_WEB_SEARCH_CONTRACT_VERSION = "authorized-web-search-v1"
BRAVE_WEB_SEARCH_ADAPTER_VERSION = "brave-web-search-v1"
_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_HOST = "api.search.brave.com"
_QUERY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_COUNTRY = re.compile(r"[A-Z]{2}")
_SEARCH_LANGUAGE = re.compile(r"[a-z]{2,5}")


class AuthorizedWebSearchProvider(StrEnum):
    BRAVE = "BRAVE"


class AuthorizedWebSearchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class AuthorizedWebSearchReason(StrEnum):
    SOURCE_TIMEOUT = "SOURCE_TIMEOUT"
    NETWORK_UNAVAILABLE = "NETWORK_UNAVAILABLE"
    REDIRECT_REJECTED = "REDIRECT_REJECTED"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    CREDENTIAL_REJECTED = "CREDENTIAL_REJECTED"
    SOURCE_RATE_LIMITED = "SOURCE_RATE_LIMITED"
    HTTP_ERROR = "HTTP_ERROR"
    UNSUPPORTED_CONTENT_TYPE = "UNSUPPORTED_CONTENT_TYPE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    HIT_VALIDATION_FAILED = "HIT_VALIDATION_FAILED"


def _bounded_text(
    value: Any,
    *,
    name: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = " ".join(value.split())
    if (not normalized and not allow_empty) or len(normalized) > maximum:
        raise ValueError(f"{name} is outside the web-search contract")
    return normalized


def _result_url(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(ord(character) <= 32 for character in value)
    ):
        raise ValueError("search result URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("search result URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise ValueError("search result URL is invalid")
    return value


@dataclass(frozen=True, slots=True)
class AuthorizedWebSearchRequest:
    """One bounded, user-policy-derived search-index query."""

    query_id: str
    query: str
    count: int = 20
    offset: int = 0
    country: str = "CA"
    search_language: str = "en"
    contract_version: str = AUTHORIZED_WEB_SEARCH_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != AUTHORIZED_WEB_SEARCH_CONTRACT_VERSION:
            raise ValueError("unsupported authorized web-search contract")
        if not isinstance(self.query_id, str) or _QUERY_ID.fullmatch(
            self.query_id
        ) is None:
            raise ValueError("query_id is invalid")
        if not isinstance(self.query, str):
            raise TypeError("query must be a string")
        query = " ".join(self.query.split())
        if (
            not query
            or len(query) > 400
            or len(query.split()) > 50
            or any(ord(character) < 32 for character in query)
        ):
            raise ValueError("query exceeds the authorized provider bounds")
        if type(self.count) is not int or not 1 <= self.count <= 20:
            raise ValueError("count must be between 1 and 20")
        if type(self.offset) is not int or not 0 <= self.offset <= 9:
            raise ValueError("offset must be between 0 and 9")
        if not isinstance(self.country, str) or _COUNTRY.fullmatch(
            self.country
        ) is None:
            raise ValueError("country must be an uppercase ISO alpha-2 code")
        if not isinstance(
            self.search_language, str
        ) or _SEARCH_LANGUAGE.fullmatch(self.search_language) is None:
            raise ValueError("search_language is invalid")
        object.__setattr__(self, "query", query)


@dataclass(frozen=True, slots=True)
class AuthorizedWebSearchHit:
    """Unverified search-index evidence, never a normalized job fact."""

    title: str
    url: str
    description: str = ""
    page_age: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "title",
            _bounded_text(self.title, name="title", maximum=500),
        )
        object.__setattr__(self, "url", _result_url(self.url))
        object.__setattr__(
            self,
            "description",
            _bounded_text(
                self.description,
                name="description",
                maximum=4096,
                allow_empty=True,
            ),
        )
        if self.page_age is not None:
            object.__setattr__(
                self,
                "page_age",
                _bounded_text(
                    self.page_age,
                    name="page_age",
                    maximum=128,
                ),
            )


@dataclass(frozen=True, slots=True)
class AuthorizedWebSearchResult:
    status: AuthorizedWebSearchStatus
    query_id: str
    hits: tuple[AuthorizedWebSearchHit, ...] = ()
    reason_code: AuthorizedWebSearchReason | None = None
    retryable: bool = False
    rejected_hit_count: int = 0
    provider: AuthorizedWebSearchProvider = AuthorizedWebSearchProvider.BRAVE
    contract_version: str = AUTHORIZED_WEB_SEARCH_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AuthorizedWebSearchStatus(self.status))
        object.__setattr__(self, "provider", AuthorizedWebSearchProvider(self.provider))
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                AuthorizedWebSearchReason(self.reason_code),
            )
        if self.contract_version != AUTHORIZED_WEB_SEARCH_CONTRACT_VERSION:
            raise ValueError("unsupported authorized web-search contract")
        if not isinstance(self.query_id, str) or _QUERY_ID.fullmatch(
            self.query_id
        ) is None:
            raise ValueError("query_id is invalid")
        if not isinstance(self.hits, tuple) or any(
            not isinstance(hit, AuthorizedWebSearchHit) for hit in self.hits
        ):
            raise TypeError("hits must contain AuthorizedWebSearchHit values")
        if type(self.rejected_hit_count) is not int or self.rejected_hit_count < 0:
            raise ValueError("rejected_hit_count is invalid")
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a bool")
        if self.status is AuthorizedWebSearchStatus.SUCCEEDED:
            if (
                self.reason_code is not None
                or self.rejected_hit_count
                or self.retryable
            ):
                raise ValueError("successful search cannot contain a failure")
        elif self.status is AuthorizedWebSearchStatus.PARTIAL:
            if (
                not self.hits
                or self.reason_code
                is not AuthorizedWebSearchReason.HIT_VALIDATION_FAILED
                or self.rejected_hit_count < 1
                or self.retryable
            ):
                raise ValueError("partial search result is inconsistent")
        elif (
            self.hits
            or self.reason_code is None
            or self.rejected_hit_count < 0
        ):
            raise ValueError("failed search result is inconsistent")

    @classmethod
    def succeeded(
        cls,
        query_id: str,
        hits: tuple[AuthorizedWebSearchHit, ...],
    ) -> "AuthorizedWebSearchResult":
        return cls(
            status=AuthorizedWebSearchStatus.SUCCEEDED,
            query_id=query_id,
            hits=hits,
        )

    @classmethod
    def partial(
        cls,
        query_id: str,
        hits: tuple[AuthorizedWebSearchHit, ...],
        *,
        rejected_hit_count: int,
    ) -> "AuthorizedWebSearchResult":
        return cls(
            status=AuthorizedWebSearchStatus.PARTIAL,
            query_id=query_id,
            hits=hits,
            reason_code=AuthorizedWebSearchReason.HIT_VALIDATION_FAILED,
            rejected_hit_count=rejected_hit_count,
        )

    @classmethod
    def failed(
        cls,
        query_id: str,
        reason_code: AuthorizedWebSearchReason,
        *,
        retryable: bool = False,
        rejected_hit_count: int = 0,
    ) -> "AuthorizedWebSearchResult":
        return cls(
            status=AuthorizedWebSearchStatus.FAILED,
            query_id=query_id,
            reason_code=reason_code,
            retryable=retryable,
            rejected_hit_count=rejected_hit_count,
        )


@runtime_checkable
class AuthorizedWebSearchPort(Protocol):
    async def search(
        self,
        request: AuthorizedWebSearchRequest,
    ) -> AuthorizedWebSearchResult:
        """Return unverified, storage-authorized search-index evidence."""


@dataclass(frozen=True, slots=True)
class BraveWebSearchConfig:
    """Brave credential and server-owned execution/storage policy."""

    api_key: str = field(repr=False)
    storage_rights_confirmed: bool = False
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    max_response_bytes: int = 1_000_000
    max_redirects: int = 1
    user_agent: str = "Jobops/3 AuthorizedWebSearch"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.api_key, str)
            or not self.api_key
            or self.api_key != self.api_key.strip()
            or len(self.api_key) > 4096
            or any(character in self.api_key for character in "\r\n")
        ):
            raise ValueError("Brave API key is invalid")
        if self.storage_rights_confirmed is not True:
            raise ValueError(
                "Brave result storage rights must be explicitly confirmed"
            )
        for name, value in (
            ("connect_timeout_seconds", self.connect_timeout_seconds),
            ("read_timeout_seconds", self.read_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= 30
            ):
                raise ValueError(f"{name} is outside the provider bounds")
        if (
            type(self.max_response_bytes) is not int
            or not 1_024 <= self.max_response_bytes <= 5_000_000
        ):
            raise ValueError("max_response_bytes is outside the provider bounds")
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 2:
            raise ValueError("max_redirects is outside the provider bounds")
        if (
            not isinstance(self.user_agent, str)
            or not self.user_agent.strip()
            or len(self.user_agent) > 120
            or any(character in self.user_agent for character in "\r\n")
        ):
            raise ValueError("user_agent is invalid")


class BraveAuthorizedWebSearch:
    """Brave Search API adapter; returned hits remain unverified leads."""

    def __init__(
        self,
        *,
        config: BraveWebSearchConfig,
        http_port: BoundedJobSearchHttpPort | None = None,
    ) -> None:
        if not isinstance(config, BraveWebSearchConfig):
            raise TypeError("config must be BraveWebSearchConfig")
        if http_port is not None and not isinstance(
            http_port, BoundedJobSearchHttpPort
        ):
            raise TypeError("http_port must implement BoundedJobSearchHttpPort")
        self._config = config
        self._http_port = http_port or HttpxBoundedJobSearchHttpClient()

    async def search(
        self,
        request: AuthorizedWebSearchRequest,
    ) -> AuthorizedWebSearchResult:
        if not isinstance(request, AuthorizedWebSearchRequest):
            raise TypeError("request must be AuthorizedWebSearchRequest")
        http_request = BoundedHttpRequest(
            url=_BRAVE_ENDPOINT,
            allowed_hosts=(_BRAVE_HOST,),
            headers={
                "Accept": "application/json",
                "User-Agent": self._config.user_agent,
            },
            secret_headers={
                "X-Subscription-Token": self._config.api_key,
            },
            query={
                "q": request.query,
                "count": str(request.count),
                "offset": str(request.offset),
                "country": request.country,
                "search_lang": request.search_language,
            },
            connect_timeout_seconds=self._config.connect_timeout_seconds,
            read_timeout_seconds=self._config.read_timeout_seconds,
            max_redirects=self._config.max_redirects,
            max_response_bytes=self._config.max_response_bytes,
        )
        try:
            response = await self._http_port.get(http_request)
        except Exception:
            return AuthorizedWebSearchResult.failed(
                request.query_id,
                AuthorizedWebSearchReason.NETWORK_UNAVAILABLE,
                retryable=True,
            )
        failure = _http_failure(request.query_id, response)
        if failure is not None:
            return failure
        assert isinstance(response, BoundedHttpResult)
        assert response.content is not None
        try:
            payload = json.loads(response.content.decode("utf-8"))
            raw_results = _raw_results(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return AuthorizedWebSearchResult.failed(
                request.query_id,
                AuthorizedWebSearchReason.MALFORMED_RESPONSE,
            )

        hits: list[AuthorizedWebSearchHit] = []
        seen_urls: set[str] = set()
        rejected = 0
        for item in raw_results[: request.count]:
            try:
                hit = _hit_from_payload(item)
            except (TypeError, ValueError):
                rejected += 1
                continue
            if hit.url in seen_urls:
                continue
            seen_urls.add(hit.url)
            hits.append(hit)
        bounded_hits = tuple(hits)
        if rejected and bounded_hits:
            return AuthorizedWebSearchResult.partial(
                request.query_id,
                bounded_hits,
                rejected_hit_count=rejected,
            )
        if rejected:
            return AuthorizedWebSearchResult.failed(
                request.query_id,
                AuthorizedWebSearchReason.HIT_VALIDATION_FAILED,
                rejected_hit_count=rejected,
            )
        return AuthorizedWebSearchResult.succeeded(
            request.query_id,
            bounded_hits,
        )


def _http_failure(
    query_id: str,
    response: Any,
) -> AuthorizedWebSearchResult | None:
    if not isinstance(response, BoundedHttpResult):
        return AuthorizedWebSearchResult.failed(
            query_id,
            AuthorizedWebSearchReason.NETWORK_UNAVAILABLE,
            retryable=True,
        )
    bounded_reasons = {
        BoundedHttpStatus.TIMEOUT: (
            AuthorizedWebSearchReason.SOURCE_TIMEOUT,
            True,
        ),
        BoundedHttpStatus.NETWORK_UNAVAILABLE: (
            AuthorizedWebSearchReason.NETWORK_UNAVAILABLE,
            True,
        ),
        BoundedHttpStatus.REDIRECT_REJECTED: (
            AuthorizedWebSearchReason.REDIRECT_REJECTED,
            False,
        ),
        BoundedHttpStatus.RESPONSE_TOO_LARGE: (
            AuthorizedWebSearchReason.RESPONSE_TOO_LARGE,
            False,
        ),
    }
    mapped = bounded_reasons.get(response.status)
    if mapped is not None:
        reason, retryable = mapped
        return AuthorizedWebSearchResult.failed(
            query_id,
            reason,
            retryable=retryable,
        )
    if (
        response.response_status is None
        or response.headers is None
        or response.content is None
    ):
        return AuthorizedWebSearchResult.failed(
            query_id,
            AuthorizedWebSearchReason.NETWORK_UNAVAILABLE,
            retryable=True,
        )
    if response.response_status in {408, 504}:
        return AuthorizedWebSearchResult.failed(
            query_id,
            AuthorizedWebSearchReason.SOURCE_TIMEOUT,
            retryable=True,
        )
    if response.response_status in {401, 403}:
        return AuthorizedWebSearchResult.failed(
            query_id,
            AuthorizedWebSearchReason.CREDENTIAL_REJECTED,
        )
    if response.response_status == 429:
        return AuthorizedWebSearchResult.failed(
            query_id,
            AuthorizedWebSearchReason.SOURCE_RATE_LIMITED,
            retryable=True,
        )
    if response.response_status >= 500:
        return AuthorizedWebSearchResult.failed(
            query_id,
            AuthorizedWebSearchReason.HTTP_ERROR,
            retryable=True,
        )
    if response.response_status != 200:
        return AuthorizedWebSearchResult.failed(
            query_id,
            AuthorizedWebSearchReason.HTTP_ERROR,
        )
    content_type = response.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().casefold() not in {
        "application/json",
        "application/vnd.api+json",
    }:
        return AuthorizedWebSearchResult.failed(
            query_id,
            AuthorizedWebSearchReason.UNSUPPORTED_CONTENT_TYPE,
        )
    return None


def _raw_results(payload: Any) -> list[Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Brave response must be an object")
    web = payload.get("web")
    if not isinstance(web, Mapping):
        raise ValueError("Brave response must contain web results")
    results = web.get("results")
    if not isinstance(results, list) or len(results) > 100:
        raise ValueError("Brave web results are outside the response bounds")
    return results


def _hit_from_payload(payload: Any) -> AuthorizedWebSearchHit:
    if not isinstance(payload, Mapping):
        raise ValueError("Brave web result must be an object")
    page_age = payload.get("page_age")
    if page_age is not None and not isinstance(page_age, str):
        raise ValueError("Brave page_age is invalid")
    return AuthorizedWebSearchHit(
        title=payload.get("title"),
        url=payload.get("url"),
        description=payload.get("description", ""),
        page_age=page_age,
    )


__all__ = [
    "AUTHORIZED_WEB_SEARCH_CONTRACT_VERSION",
    "BRAVE_WEB_SEARCH_ADAPTER_VERSION",
    "AuthorizedWebSearchHit",
    "AuthorizedWebSearchPort",
    "AuthorizedWebSearchProvider",
    "AuthorizedWebSearchReason",
    "AuthorizedWebSearchRequest",
    "AuthorizedWebSearchResult",
    "AuthorizedWebSearchStatus",
    "BraveAuthorizedWebSearch",
    "BraveWebSearchConfig",
]
