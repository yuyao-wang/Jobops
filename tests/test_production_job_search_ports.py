"""Focused S3b1 production JobSearchPort tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from core.job_library_refresh import ConfiguredSearchProfileExecutor
from core.job_search import (
    JobSearchReason,
    JobSearchRequest,
    JobSearchStatus,
    search_jobs,
)
from core.search_profile import (
    PrivateHomeSearchProfileRepository,
    SaveSearchProfileCommand,
    SearchProfileSourceKind,
    SearchProfileSourceReference,
    save_search_profile,
)
from core.private_home import PrivateHome
from source_connectors.greenhouse_board import (
    BoundedHttpRequest,
    BoundedHttpResult,
    BoundedHttpStatus,
    GreenhouseBoardConfig,
    HttpxBoundedJobSearchHttpClient,
    JobSearchExecutionPolicy,
)
from source_connectors.production_job_search import (
    JobSearchProviderCapabilityStatus,
    build_production_job_search_ports,
)


NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)
SOURCE = SearchProfileSourceReference(
    SearchProfileSourceKind.KNOWN_GREENHOUSE_BOARD,
    "examplelabs",
)
BOARD = GreenhouseBoardConfig(
    canonical_company="Example Labs",
    board_token=SOURCE.source_id,
)


def _job(job_id: int, title: str, location: str = "Calgary") -> dict:
    return {
        "id": job_id,
        "title": title,
        "location": {"name": location},
        "absolute_url": (
            "https://job-boards.greenhouse.io/"
            f"examplelabs/jobs/{job_id}"
        ),
    }


def _success(payload: object) -> BoundedHttpResult:
    return BoundedHttpResult(
        BoundedHttpStatus.SUCCEEDED,
        response_status=200,
        headers={"content-type": "application/json; charset=utf-8"},
        content=json.dumps(payload).encode("utf-8"),
    )


class FakeBoundedHttp:
    def __init__(self, *results: BoundedHttpResult) -> None:
        self.results = list(results)
        self.requests: list[BoundedHttpRequest] = []

    async def get(self, request: BoundedHttpRequest) -> BoundedHttpResult:
        self.requests.append(request)
        return self.results.pop(0)


def _request(
    *,
    company: str = "Example Labs",
    title: str = "Engineer",
    location: str | None = None,
) -> JobSearchRequest:
    return JobSearchRequest(
        request_id="search-request-synthetic",
        company=company,
        title=title,
        location=location,
    )


@pytest.mark.asyncio
async def test_greenhouse_production_search_is_bounded_stable_and_deduplicated() -> None:
    payload = {
        "jobs": [
            _job(2, "Senior Engineer", "Calgary"),
            _job(1, "Engineer", "Calgary"),
            _job(1, "Engineer", "Calgary"),
            _job(3, "Engineer", "Toronto"),
        ]
    }
    http = FakeBoundedHttp(_success(payload), _success(payload))
    built = build_production_job_search_ports(
        boards=(BOARD,),
        http_port=http,
        policy=JobSearchExecutionPolicy(max_results_per_query=2),
    )
    executor = ConfiguredSearchProfileExecutor(built.ports)
    port = built.ports[SOURCE]

    first = await search_jobs(
        _request(location="Calgary"),
        port=port,
    )
    second = await search_jobs(
        _request(location="Calgary"),
        port=port,
    )

    assert first.status is JobSearchStatus.SUCCEEDED
    assert first.candidate_set is not None
    assert second.candidate_set is not None
    assert tuple(
        item.source_job_id for item in first.candidate_set.candidates
    ) == ("1", "2")
    assert (
        first.candidate_set.candidate_set_id
        == second.candidate_set.candidate_set_id
    )
    assert all(request.max_response_bytes == 2_000_000 for request in http.requests)
    assert all(request.max_redirects == 2 for request in http.requests)
    assert isinstance(executor, ConfiguredSearchProfileExecutor)


@pytest.mark.asyncio
async def test_provider_boundary_is_exact_and_lever_is_not_advertised() -> None:
    http = FakeBoundedHttp(_success({"jobs": []}))
    built = build_production_job_search_ports(
        boards=(BOARD,),
        http_port=http,
        policy=JobSearchExecutionPolicy(),
    )

    assert tuple(built.ports) == (SOURCE,)
    assert tuple(
        (item.provider_id, item.status)
        for item in built.capabilities
    ) == (
        ("GREENHOUSE", JobSearchProviderCapabilityStatus.SUPPORTED),
        ("LEVER", JobSearchProviderCapabilityStatus.UNSUPPORTED),
    )
    result = await search_jobs(
        _request(company="Unconfigured Labs"),
        port=built.ports[SOURCE],
    )
    assert result.status is JobSearchStatus.UNSUPPORTED
    assert result.reason_code is JobSearchReason.UNSUPPORTED_COMPANY
    assert http.requests == []


@pytest.mark.asyncio
async def test_network_content_and_candidate_failures_are_typed() -> None:
    cases = (
        (
            BoundedHttpResult(BoundedHttpStatus.TIMEOUT),
            JobSearchReason.SOURCE_TIMEOUT,
        ),
        (
            BoundedHttpResult(BoundedHttpStatus.NETWORK_UNAVAILABLE),
            JobSearchReason.NETWORK_UNAVAILABLE,
        ),
        (
            BoundedHttpResult(BoundedHttpStatus.RESPONSE_TOO_LARGE),
            JobSearchReason.RESPONSE_TOO_LARGE,
        ),
        (
            BoundedHttpResult(BoundedHttpStatus.REDIRECT_REJECTED),
            JobSearchReason.REDIRECT_REJECTED,
        ),
        (
            BoundedHttpResult(
                BoundedHttpStatus.SUCCEEDED,
                response_status=403,
                headers={"content-type": "application/json"},
                content=b"{}",
            ),
            JobSearchReason.HTTP_ERROR,
        ),
        (
            BoundedHttpResult(
                BoundedHttpStatus.SUCCEEDED,
                response_status=429,
                headers={"content-type": "application/json"},
                content=b"{}",
            ),
            JobSearchReason.SOURCE_RATE_LIMITED,
        ),
        (
            BoundedHttpResult(
                BoundedHttpStatus.SUCCEEDED,
                response_status=200,
                headers={"content-type": "text/html"},
                content=b"not json",
            ),
            JobSearchReason.UNSUPPORTED_CONTENT_TYPE,
        ),
        (
            BoundedHttpResult(
                BoundedHttpStatus.SUCCEEDED,
                response_status=200,
                headers={"content-type": "application/json"},
                content=b"{",
            ),
            JobSearchReason.MALFORMED_RESPONSE,
        ),
        (
            _success({"jobs": [{"id": 1, "title": "Engineer"}]}),
            JobSearchReason.CANDIDATE_VALIDATION_FAILED,
        ),
    )
    for response, expected in cases:
        built = build_production_job_search_ports(
            boards=(BOARD,),
            http_port=FakeBoundedHttp(response),
            policy=JobSearchExecutionPolicy(),
        )
        result = await search_jobs(_request(), port=built.ports[SOURCE])
        assert result.reason_code is expected
        assert result.candidate_set is None

    redirect_client = HttpxBoundedJobSearchHttpClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"location": "https://untrusted.example/jobs"},
            )
        )
    )
    redirect = await redirect_client.get(
        BoundedHttpRequest(
            url="https://boards-api.greenhouse.io/v1/boards/x/jobs",
            allowed_hosts=("boards-api.greenhouse.io",),
            headers={},
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_redirects=1,
            max_response_bytes=100,
        )
    )
    assert redirect.status is BoundedHttpStatus.REDIRECT_REJECTED


@pytest.mark.asyncio
async def test_factory_is_network_free_and_s3b_executor_consumes_ports(
    tmp_path: Path,
) -> None:
    http = FakeBoundedHttp(_success({"jobs": []}))
    built = build_production_job_search_ports(
        boards=(BOARD,),
        http_port=http,
        policy=JobSearchExecutionPolicy(),
    )
    assert http.requests == []
    with pytest.raises(TypeError, match="http_port"):
        build_production_job_search_ports(
            boards=(BOARD,),
            http_port=object(),  # type: ignore[arg-type]
            policy=JobSearchExecutionPolicy(),
        )
    repository = PrivateHomeSearchProfileRepository(
        PrivateHome(tmp_path / "private-home")
    )
    saved = save_search_profile(
        SaveSearchProfileCommand(
            subject_id="subject-synthetic",
            display_name="Example Labs Engineering",
            company="Example Labs",
            title="Engineer",
            source=SOURCE,
            enabled=True,
            now=NOW,
        ),
        repository=repository,
    )
    assert saved.profile is not None

    result = await ConfiguredSearchProfileExecutor(built.ports).search(
        saved.profile
    )

    assert result.status is JobSearchStatus.SUCCEEDED
    assert len(http.requests) == 1
    production_source = (
        Path(__file__).resolve().parents[1]
        / "source_connectors"
        / "production_job_search.py"
    ).read_text(encoding="utf-8")
    assert "utils.discovery" not in production_source
    assert "playwright" not in production_source
    assert "openai" not in production_source
