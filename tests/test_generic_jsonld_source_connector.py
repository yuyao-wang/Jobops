"""Focused tests for bounded Generic JSON-LD public job reads."""

from __future__ import annotations

import ast
import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import source_connectors.public_reader as public_reader_module
from source_connectors import (
    AtsType,
    ReadJobReason,
    ReadJobRequest,
    ReadJobResult,
    ReadJobStatus,
    SourceJobObservation,
    SourceJobReader,
    SourcePlatform,
    WorkMode,
    read_public_job,
)
from source_connectors.generic_jsonld import GenericJsonLdJobReader


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "source_connectors"
SOURCE_URL = "https://careers.example.com/jobs/jsonld-123"
PUBLIC_IP = "93.184.216.34"
OBSERVED_AT = datetime(2026, 7, 27, 20, 30, tzinfo=timezone.utc)


def _fixture(name: str) -> str:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8")


def _html(payload) -> str:
    return (
        '<!doctype html><script type="application/ld+json">'
        f"{json.dumps(payload)}"
        "</script>"
    )


def _minimal_posting(**overrides):
    posting = {
        "@type": "JobPosting",
        "title": "Synthetic Minimal Engineer",
        "description": "Build deterministic readers.",
        "hiringOrganization": {"name": "Example Labs"},
    }
    posting.update(overrides)
    return posting


def _reader(
    handler,
    *,
    resolver=lambda hostname: (PUBLIC_IP,),
) -> GenericJsonLdJobReader:
    return GenericJsonLdJobReader(
        transport=httpx.MockTransport(handler),
        clock=lambda: OBSERVED_AT,
        resolver=resolver,
    )


@pytest.mark.asyncio
async def test_provider_neutral_entry_reads_one_generic_jsonld_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=_fixture("jsonld_job_object.html"),
        )

    monkeypatch.setattr(
        public_reader_module,
        "GenericJsonLdJobReader",
        lambda: _reader(handler),
    )

    result = await read_public_job(ReadJobRequest(url=SOURCE_URL))

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert "cookie" not in requests[0].headers
    assert "authorization" not in requests[0].headers
    assert result.status is ReadJobStatus.SUCCEEDED
    assert type(result.observation) is SourceJobObservation
    assert result.observation is not None
    observation = result.observation
    assert observation.source_platform is SourcePlatform.GENERIC_WEB
    assert SourcePlatform.GENERIC_WEB.value == "GENERIC_WEB"
    assert observation.ats_type is AtsType.UNKNOWN
    assert observation.source_job_id == "jsonld-123"
    assert observation.source_url == SOURCE_URL
    assert (
        observation.application_url
        == "https://careers.example.com/jobs/jsonld-123/apply"
    )
    assert observation.company == "Example Labs"
    assert observation.title == "Synthetic Infrastructure Engineer"
    assert observation.description == "Build safe distributed systems."
    assert observation.location == "Vancouver, BC, CA"
    assert observation.work_mode is WorkMode.REMOTE
    assert observation.posted_at == "2026-07-20T14:00:00Z"
    assert observation.observed_at == "2026-07-27T20:30:00Z"


@pytest.mark.parametrize(
    ("fixture_name", "expected_title"),
    (
        ("jsonld_job_object.html", "Synthetic Infrastructure Engineer"),
        ("jsonld_job_array.html", "Synthetic Array Engineer"),
        ("jsonld_job_graph.html", "Synthetic Graph Engineer"),
    ),
)
@pytest.mark.asyncio
async def test_jsonld_object_array_and_graph_are_supported(
    fixture_name: str,
    expected_title: str,
) -> None:
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/xhtml+xml"},
            text=_fixture(fixture_name),
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert isinstance(
        _reader(lambda request: httpx.Response(500)),
        SourceJobReader,
    )
    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.observation is not None
    assert result.observation.title == expected_title


@pytest.mark.asyncio
async def test_optional_jsonld_fields_are_not_guessed() -> None:
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=_html(_minimal_posting()),
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.observation is not None
    observation = result.observation
    assert observation.source_job_id is None
    assert observation.application_url is None
    assert observation.location == ""
    assert observation.work_mode is WorkMode.UNKNOWN
    assert observation.posted_at is None
    assert {field.name for field in fields(SourceJobObservation)} == {
        "source_platform",
        "source_job_id",
        "source_url",
        "application_url",
        "company",
        "title",
        "description",
        "location",
        "work_mode",
        "posted_at",
        "ats_type",
        "observed_at",
        "provenance",
    }


@pytest.mark.asyncio
async def test_page_without_job_posting_is_unsupported() -> None:
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=_html({"@type": "Organization", "name": "Example Labs"}),
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.UNSUPPORTED
    assert result.reason_code is ReadJobReason.UNSUPPORTED_URL
    assert result.retryable is False
    assert result.observation is None


@pytest.mark.asyncio
async def test_multiple_job_postings_are_not_selected() -> None:
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=_html([_minimal_posting(), _minimal_posting()]),
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_RESPONSE_INVALID
    assert result.retryable is False
    assert result.observation is None


@pytest.mark.asyncio
async def test_malformed_jsonld_is_invalid_source_response() -> None:
    html = (
        '<script type="application/ld+json">'
        '{"@type":"JobPosting",'
        "</script>"
    )
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=html,
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_RESPONSE_INVALID
    assert result.observation is None


@pytest.mark.parametrize(
    "posting",
    (
        _minimal_posting(title=None),
        _minimal_posting(description=""),
        _minimal_posting(hiringOrganization=None),
        _minimal_posting(hiringOrganization={"name": ""}),
    ),
)
@pytest.mark.asyncio
async def test_missing_required_jsonld_field_is_invalid(posting) -> None:
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=_html(posting),
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_RESPONSE_INVALID
    assert result.observation is None


@pytest.mark.parametrize(
    "content_type",
    ("application/json", "text/plain", "", "image/png"),
)
@pytest.mark.asyncio
async def test_non_html_content_type_is_unsupported(content_type: str) -> None:
    headers = {"content-type": content_type} if content_type else {}
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers=headers,
            text=_fixture("jsonld_job_object.html"),
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.UNSUPPORTED
    assert result.reason_code is ReadJobReason.UNSUPPORTED_URL
    assert result.observation is None


@pytest.mark.parametrize(
    ("status_code", "reason", "retryable"),
    (
        (404, ReadJobReason.JOB_NOT_FOUND, False),
        (410, ReadJobReason.JOB_CLOSED, False),
        (429, ReadJobReason.SOURCE_RATE_LIMITED, True),
        (503, ReadJobReason.SOURCE_UNAVAILABLE, True),
        (403, ReadJobReason.SOURCE_UNAVAILABLE, False),
    ),
)
@pytest.mark.asyncio
async def test_generic_http_failures_are_typed(
    status_code: int,
    reason: ReadJobReason,
    retryable: bool,
) -> None:
    result = await _reader(
        lambda request: httpx.Response(status_code)
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is reason
    assert result.retryable is retryable
    assert result.observation is None


@pytest.mark.asyncio
async def test_generic_timeout_is_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    result = await _reader(timeout).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_TIMEOUT
    assert result.retryable is True
    assert result.observation is None


@pytest.mark.parametrize(
    "url",
    (
        "",
        "careers.example.com/jobs/123",
        "file:///tmp/job.html",
        "ftp://careers.example.com/jobs/123",
        "https://user@careers.example.com/jobs/123",
    ),
)
@pytest.mark.asyncio
async def test_invalid_generic_url_is_rejected_before_http(url: str) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("invalid URL must not perform HTTP")

    result = await _reader(handler).read_job(ReadJobRequest(url=url))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.INVALID_URL
    assert result.retryable is False
    assert called is False


@pytest.mark.parametrize(
    "url",
    (
        "http://localhost/jobs/123",
        "http://jobs.localhost/jobs/123",
        "http://127.0.0.1/jobs/123",
        "http://10.0.0.1/jobs/123",
        "http://169.254.169.254/latest/meta-data",
        "http://224.0.0.1/jobs/123",
        "http://192.0.2.1/jobs/123",
        "http://[::1]/jobs/123",
        "http://metadata.google.internal/computeMetadata/v1",
    ),
)
@pytest.mark.asyncio
async def test_unsafe_initial_url_is_rejected_before_http(url: str) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("unsafe URL must not perform HTTP")

    result = await _reader(
        handler,
        resolver=lambda hostname: (_ for _ in ()).throw(
            AssertionError("blocked hostname must not resolve")
        ),
    ).read_job(ReadJobRequest(url=url))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.UNSAFE_URL
    assert result.retryable is False
    assert result.observation is None
    assert called is False


@pytest.mark.asyncio
async def test_hostname_resolving_private_is_rejected_before_http() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("private DNS result must not perform HTTP")

    result = await _reader(
        handler,
        resolver=lambda hostname: ("10.0.0.7",),
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.UNSAFE_URL
    assert result.retryable is False
    assert called is False


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_rejected_before_second_get() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
        )

    result = await _reader(handler).read_job(ReadJobRequest(url=SOURCE_URL))

    assert requests == [SOURCE_URL]
    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.UNSAFE_URL
    assert result.retryable is False


@pytest.mark.asyncio
async def test_redirects_are_bounded_and_carry_no_cookies() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        step = len(requests)
        return httpx.Response(
            302,
            headers={
                "location": f"/jobs/redirect-{step}",
                "set-cookie": "session=must-not-propagate",
            },
        )

    result = await _reader(handler).read_job(ReadJobRequest(url=SOURCE_URL))

    assert len(requests) == 4
    assert all("cookie" not in request.headers for request in requests)
    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_RESPONSE_INVALID


@pytest.mark.asyncio
async def test_safe_redirect_sets_final_source_url() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={"location": "https://jobs.example.org/final"},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=_html(_minimal_posting()),
        )

    result = await _reader(handler).read_job(ReadJobRequest(url=SOURCE_URL))

    assert requests == [SOURCE_URL, "https://jobs.example.org/final"]
    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.observation is not None
    assert result.observation.source_url == "https://jobs.example.org/final"


@pytest.mark.asyncio
async def test_oversized_response_is_rejected() -> None:
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers={
                "content-type": "text/html",
                "content-length": "2000001",
            },
            content=b"",
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_RESPONSE_INVALID
    assert result.observation is None


@pytest.mark.asyncio
async def test_streamed_response_over_size_limit_is_rejected() -> None:
    result = await _reader(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * 2_000_001,
        )
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_RESPONSE_INVALID
    assert result.observation is None


@pytest.mark.parametrize(
    ("url", "reader_name"),
    (
        (
            "https://job-boards.greenhouse.io/examplelabs/jobs/123456",
            "GreenhousePublicJobReader",
        ),
        (
            "https://jobs.lever.co/examplelabs/abc-123",
            "LeverPublicJobReader",
        ),
    ),
)
@pytest.mark.asyncio
async def test_known_connector_failure_never_falls_back_to_jsonld(
    url: str,
    reader_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingReader:
        async def read_job(self, request: ReadJobRequest) -> ReadJobResult:
            return ReadJobResult.failed(ReadJobReason.SOURCE_TIMEOUT)

    monkeypatch.setattr(public_reader_module, reader_name, FailingReader)
    monkeypatch.setattr(
        public_reader_module,
        "GenericJsonLdJobReader",
        lambda: (_ for _ in ()).throw(
            AssertionError("known connector failure must not fall back")
        ),
    )

    result = await read_public_job(ReadJobRequest(url=url))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_TIMEOUT
    assert result.retryable is True


@pytest.mark.asyncio
async def test_generic_reader_has_no_persistence_or_execution_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_home = tmp_path / "synthetic-private-home"
    monkeypatch.setenv("JOBOPS_HOME", str(synthetic_home))
    monkeypatch.setattr(
        public_reader_module,
        "GenericJsonLdJobReader",
        lambda: _reader(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=_fixture("jsonld_job_object.html"),
            )
        ),
    )

    result = await read_public_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.SUCCEEDED
    assert not synthetic_home.exists()

    forbidden_roots = {
        "adapters",
        "anthropic",
        "auth",
        "core",
        "csv",
        "jobctl",
        "openai",
        "playwright",
        "service",
        "utils",
        "workers",
    }
    for relative_path in (
        "source_connectors/generic_jsonld.py",
        "source_connectors/public_reader.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        imported_roots = {
            node.module.split(".", 1)[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert imported_roots.isdisjoint(forbidden_roots)
