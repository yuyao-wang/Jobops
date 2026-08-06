"""Sanitized contract tests for authorized web-search discovery."""

from __future__ import annotations

import json

import httpx
import pytest

from source_connectors.authorized_web_search import (
    AuthorizedWebSearchReason,
    AuthorizedWebSearchRequest,
    AuthorizedWebSearchStatus,
    BraveAuthorizedWebSearch,
    BraveWebSearchConfig,
)
from source_connectors.greenhouse_board import (
    BoundedHttpRequest,
    BoundedHttpResult,
    BoundedHttpStatus,
    HttpxBoundedJobSearchHttpClient,
)


API_KEY = "synthetic-brave-key"


def _success(payload: object) -> BoundedHttpResult:
    return BoundedHttpResult(
        BoundedHttpStatus.SUCCEEDED,
        response_status=200,
        headers={"content-type": "application/json; charset=utf-8"},
        content=json.dumps(payload).encode("utf-8"),
    )


class FakeBoundedHttp:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.requests: list[BoundedHttpRequest] = []

    async def get(self, request: BoundedHttpRequest) -> object:
        self.requests.append(request)
        return self.results.pop(0)


def _adapter(
    http: FakeBoundedHttp,
    *,
    api_key: str = API_KEY,
) -> BraveAuthorizedWebSearch:
    return BraveAuthorizedWebSearch(
        config=BraveWebSearchConfig(
            api_key=api_key,
            storage_rights_confirmed=True,
        ),
        http_port=http,
    )


def _request(**changes: object) -> AuthorizedWebSearchRequest:
    values = {
        "query_id": "query-synthetic-1",
        "query": (
            '("machine learning engineer" OR "applied scientist") '
            '("Vancouver" OR "Remote Canada") '
            "(site:linkedin.com/jobs/view OR site:indeed.com/viewjob)"
        ),
        "count": 10,
        "offset": 0,
        "country": "CA",
        "search_language": "en",
    }
    values.update(changes)
    return AuthorizedWebSearchRequest(**values)  # type: ignore[arg-type]


def test_storage_attestation_and_secret_repr_are_mandatory() -> None:
    with pytest.raises(ValueError, match="storage rights"):
        BraveWebSearchConfig(api_key=API_KEY)

    config = BraveWebSearchConfig(
        api_key=API_KEY,
        storage_rights_confirmed=True,
    )
    adapter = BraveAuthorizedWebSearch(
        config=config,
        http_port=FakeBoundedHttp(),
    )

    assert API_KEY not in repr(config)
    assert API_KEY not in repr(adapter)


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"query": "x" * 401}, "query exceeds"),
        ({"query": " ".join(f"word{index}" for index in range(51))}, "query exceeds"),
        ({"count": 0}, "count"),
        ({"count": 21}, "count"),
        ({"offset": -1}, "offset"),
        ({"offset": 10}, "offset"),
    ),
)
def test_request_query_count_and_offset_are_bounded(
    changes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _request(**changes)


@pytest.mark.asyncio
async def test_brave_adapter_sends_one_bounded_request_and_returns_leads() -> None:
    http = FakeBoundedHttp(
        _success(
            {
                "web": {
                    "results": [
                        {
                            "title": "Example role | LinkedIn",
                            "url": "https://www.linkedin.com/jobs/view/123",
                            "description": "Example Corp is hiring in Canada.",
                            "page_age": "2026-08-03T10:00:00Z",
                        },
                        {
                            "title": "Example role | LinkedIn",
                            "url": "https://www.linkedin.com/jobs/view/123",
                            "description": "Duplicate search result.",
                        },
                    ]
                }
            }
        )
    )
    result = await _adapter(http).search(_request())

    assert result.status is AuthorizedWebSearchStatus.SUCCEEDED
    assert len(result.hits) == 1
    assert result.hits[0].page_age == "2026-08-03T10:00:00Z"
    assert len(http.requests) == 1
    sent = http.requests[0]
    assert sent.url == "https://api.search.brave.com/res/v1/web/search"
    assert sent.allowed_hosts == ("api.search.brave.com",)
    assert sent.query == {
        "q": _request().query,
        "count": "10",
        "offset": "0",
        "country": "CA",
        "search_lang": "en",
    }
    assert sent.secret_headers["X-Subscription-Token"] == API_KEY
    assert "X-Subscription-Token" not in sent.headers
    assert API_KEY not in repr(sent)


@pytest.mark.asyncio
async def test_secret_header_is_sent_only_on_first_hop() -> None:
    observed_tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_tokens.append(request.headers.get("x-subscription-token"))
        if len(observed_tokens) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://api.search.brave.com/res/v1/web/redirected"
                    )
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"web":{"results":[]}}',
        )

    client = HttpxBoundedJobSearchHttpClient(
        transport=httpx.MockTransport(handler)
    )
    result = await client.get(
        BoundedHttpRequest(
            url="https://api.search.brave.com/res/v1/web/search",
            allowed_hosts=("api.search.brave.com",),
            headers={"Accept": "application/json"},
            secret_headers={"X-Subscription-Token": API_KEY},
            query={"q": "synthetic role"},
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_redirects=1,
            max_response_bytes=1024,
        )
    )

    assert result.status is BoundedHttpStatus.SUCCEEDED
    assert observed_tokens == [API_KEY, None]


def test_secret_and_public_headers_cannot_overlap_case_insensitively() -> None:
    with pytest.raises(ValueError, match="header keys overlap"):
        BoundedHttpRequest(
            url="https://api.search.brave.com/res/v1/web/search",
            allowed_hosts=("api.search.brave.com",),
            headers={"x-subscription-token": "public"},
            secret_headers={"X-Subscription-Token": API_KEY},
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_redirects=0,
            max_response_bytes=1024,
        )


@pytest.mark.asyncio
async def test_invalid_search_hits_are_typed_partial_or_failed() -> None:
    invalid_hits = [
        {"title": "HTTP", "url": "http://example.com/job"},
        {"title": "Credentials", "url": "https://user@example.com/job"},
        {"title": "Port", "url": "https://example.com:8443/job"},
    ]
    partial_http = FakeBoundedHttp(
        _success(
            {
                "web": {
                    "results": [
                        {
                            "title": "Verified only as a search hit",
                            "url": "https://jobs.example.com/role",
                            "description": "Needs canonical resolution.",
                        },
                        *invalid_hits,
                    ]
                }
            }
        )
    )
    partial = await _adapter(partial_http).search(_request())

    assert partial.status is AuthorizedWebSearchStatus.PARTIAL
    assert partial.reason_code is AuthorizedWebSearchReason.HIT_VALIDATION_FAILED
    assert partial.rejected_hit_count == 3
    assert len(partial.hits) == 1

    failed = await _adapter(
        FakeBoundedHttp(_success({"web": {"results": invalid_hits}}))
    ).search(_request())
    assert failed.status is AuthorizedWebSearchStatus.FAILED
    assert failed.reason_code is AuthorizedWebSearchReason.HIT_VALIDATION_FAILED
    assert failed.rejected_hit_count == 3
    assert failed.hits == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason", "retryable"),
    (
        (
            BoundedHttpResult(BoundedHttpStatus.TIMEOUT),
            AuthorizedWebSearchReason.SOURCE_TIMEOUT,
            True,
        ),
        (
            BoundedHttpResult(BoundedHttpStatus.NETWORK_UNAVAILABLE),
            AuthorizedWebSearchReason.NETWORK_UNAVAILABLE,
            True,
        ),
        (
            BoundedHttpResult(BoundedHttpStatus.REDIRECT_REJECTED),
            AuthorizedWebSearchReason.REDIRECT_REJECTED,
            False,
        ),
        (
            BoundedHttpResult(BoundedHttpStatus.RESPONSE_TOO_LARGE),
            AuthorizedWebSearchReason.RESPONSE_TOO_LARGE,
            False,
        ),
        (
            BoundedHttpResult(
                BoundedHttpStatus.SUCCEEDED,
                response_status=401,
                headers={"content-type": "application/json"},
                content=b"{}",
            ),
            AuthorizedWebSearchReason.CREDENTIAL_REJECTED,
            False,
        ),
        (
            BoundedHttpResult(
                BoundedHttpStatus.SUCCEEDED,
                response_status=429,
                headers={"content-type": "application/json"},
                content=b"{}",
            ),
            AuthorizedWebSearchReason.SOURCE_RATE_LIMITED,
            True,
        ),
    ),
)
async def test_transport_and_http_failures_are_typed(
    response: BoundedHttpResult,
    reason: AuthorizedWebSearchReason,
    retryable: bool,
) -> None:
    result = await _adapter(FakeBoundedHttp(response)).search(_request())

    assert result.status is AuthorizedWebSearchStatus.FAILED
    assert result.reason_code is reason
    assert result.retryable is retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    (
        (
            BoundedHttpResult(
                BoundedHttpStatus.SUCCEEDED,
                response_status=200,
                headers={"content-type": "text/html"},
                content=b"not json",
            ),
            AuthorizedWebSearchReason.UNSUPPORTED_CONTENT_TYPE,
        ),
        (
            BoundedHttpResult(
                BoundedHttpStatus.SUCCEEDED,
                response_status=200,
                headers={"content-type": "application/json"},
                content=b"{",
            ),
            AuthorizedWebSearchReason.MALFORMED_RESPONSE,
        ),
        (
            _success({"web": {"results": "not-a-list"}}),
            AuthorizedWebSearchReason.MALFORMED_RESPONSE,
        ),
    ),
)
async def test_content_and_payload_failures_are_typed(
    response: BoundedHttpResult,
    reason: AuthorizedWebSearchReason,
) -> None:
    result = await _adapter(FakeBoundedHttp(response)).search(_request())

    assert result.status is AuthorizedWebSearchStatus.FAILED
    assert result.reason_code is reason


@pytest.mark.asyncio
async def test_provider_response_is_bounded_by_requested_count() -> None:
    results = [
        {
            "title": f"Synthetic role {index}",
            "url": f"https://jobs.example.com/{index}",
        }
        for index in range(5)
    ]
    result = await _adapter(
        FakeBoundedHttp(_success({"web": {"results": results}}))
    ).search(_request(count=2))

    assert result.status is AuthorizedWebSearchStatus.SUCCEEDED
    assert tuple(hit.title for hit in result.hits) == (
        "Synthetic role 0",
        "Synthetic role 1",
    )
