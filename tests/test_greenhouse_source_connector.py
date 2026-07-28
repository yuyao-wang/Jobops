"""Focused contract tests for the read-only Greenhouse public job reader."""

from __future__ import annotations

import ast
import json
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import source_connectors.public_reader as public_reader_module
from source_connectors import (
    AtsType,
    GreenhousePublicJobReader,
    ReadJobReason,
    ReadJobRequest,
    ReadJobResult,
    ReadJobStatus,
    SourceJobReader,
    SourcePlatform,
    WorkMode,
    read_public_job,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "source_connectors"
    / "greenhouse_job.json"
)
SOURCE_URL = "https://job-boards.greenhouse.io/examplelabs/jobs/123456"
OBSERVED_AT = datetime(2026, 7, 27, 18, 30, tzinfo=timezone.utc)


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _reader(handler) -> GreenhousePublicJobReader:
    return GreenhousePublicJobReader(
        transport=httpx.MockTransport(handler),
        clock=lambda: OBSERVED_AT,
    )


@pytest.mark.asyncio
async def test_provider_neutral_entry_preserves_greenhouse_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == (
            "https://boards-api.greenhouse.io/"
            "v1/boards/examplelabs/jobs/123456"
        )
        return httpx.Response(200, json=_payload())

    expected = await _reader(handler).read_job(ReadJobRequest(url=SOURCE_URL))
    monkeypatch.setattr(
        public_reader_module,
        "GreenhousePublicJobReader",
        lambda: _reader(handler),
    )

    result = await read_public_job(ReadJobRequest(url=SOURCE_URL))

    assert result == expected
    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.observation is not None
    assert result.observation.source_platform is SourcePlatform.GREENHOUSE
    assert {item.field for item in result.observation.provenance} == {
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
    }


def test_provider_neutral_request_has_no_platform_parameter() -> None:
    assert [field.name for field in fields(ReadJobRequest)] == ["url"]


def test_source_platform_and_ats_type_are_distinct_contracts() -> None:
    assert type(SourcePlatform.GREENHOUSE) is SourcePlatform
    assert type(AtsType.GREENHOUSE) is AtsType
    assert type(SourcePlatform.GREENHOUSE) is not type(AtsType.GREENHOUSE)
    assert SourcePlatform.LEVER.value == "LEVER"
    assert AtsType.LEVER.value == "LEVER"
    assert AtsType.UNKNOWN.value == "UNKNOWN"


@pytest.mark.asyncio
async def test_provider_neutral_unknown_url_delegates_only_to_generic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_reader():
        raise AssertionError("unsupported URL must not construct a reader")

    monkeypatch.setattr(
        public_reader_module,
        "GreenhousePublicJobReader",
        unexpected_reader,
    )
    monkeypatch.setattr(
        public_reader_module,
        "LeverPublicJobReader",
        unexpected_reader,
    )
    generic_calls = 0

    class UnsupportedGenericReader:
        async def read_job(self, request: ReadJobRequest) -> ReadJobResult:
            nonlocal generic_calls
            generic_calls += 1
            return ReadJobResult.failed(ReadJobReason.UNSUPPORTED_URL)

    monkeypatch.setattr(
        public_reader_module,
        "GenericJsonLdJobReader",
        UnsupportedGenericReader,
    )

    result = await read_public_job(
        ReadJobRequest(url="https://jobs.example.invalid/jobs/123456")
    )

    assert result.status is ReadJobStatus.UNSUPPORTED
    assert result.reason_code is ReadJobReason.UNSUPPORTED_URL
    assert result.retryable is False
    assert result.observation is None
    assert generic_calls == 1


@pytest.mark.asyncio
async def test_provider_neutral_entry_rejects_invalid_url_without_reader_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_reader():
        raise AssertionError("invalid URL must not construct a reader")

    monkeypatch.setattr(
        public_reader_module,
        "GreenhousePublicJobReader",
        unexpected_reader,
    )
    monkeypatch.setattr(
        public_reader_module,
        "LeverPublicJobReader",
        unexpected_reader,
    )

    result = await read_public_job(ReadJobRequest(url="not-an-absolute-url"))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.INVALID_URL
    assert result.retryable is False
    assert result.observation is None


@pytest.mark.parametrize(
    ("status_code", "reason", "retryable"),
    (
        (404, ReadJobReason.JOB_NOT_FOUND, False),
        (429, ReadJobReason.SOURCE_RATE_LIMITED, True),
    ),
)
@pytest.mark.asyncio
async def test_provider_neutral_entry_preserves_greenhouse_http_failure(
    status_code: int,
    reason: ReadJobReason,
    retryable: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        public_reader_module,
        "GreenhousePublicJobReader",
        lambda: _reader(lambda request: httpx.Response(status_code)),
    )

    result = await read_public_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is reason
    assert result.retryable is retryable
    assert result.observation is None


@pytest.mark.asyncio
async def test_supported_greenhouse_url_returns_typed_observation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == (
            "https://boards-api.greenhouse.io/"
            "v1/boards/examplelabs/jobs/123456"
        )
        return httpx.Response(200, json=_payload())

    reader = _reader(handler)
    result = await reader.read_job(ReadJobRequest(url=SOURCE_URL))

    assert isinstance(reader, SourceJobReader)
    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.reason_code is None
    assert result.retryable is False
    assert result.observation is not None
    observation = result.observation
    assert observation.source_platform is SourcePlatform.GREENHOUSE
    assert observation.source_job_id == "123456"
    assert observation.source_url == SOURCE_URL
    assert observation.application_url == SOURCE_URL
    assert observation.company == "Example Labs"
    assert observation.title == "Synthetic Platform Engineer"
    assert observation.description == (
        "About the role Build synthetic distributed systems "
        "& reliable developer tools."
    )
    assert observation.location == "Vancouver, BC"
    assert observation.work_mode is WorkMode.UNKNOWN
    assert observation.posted_at == "2026-07-20T14:00:00Z"
    assert observation.ats_type is AtsType.GREENHOUSE
    assert observation.observed_at == "2026-07-27T18:30:00Z"
    assert {item.field for item in observation.provenance} == {
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
    }
    serialized = json.loads(json.dumps(asdict(observation)))
    assert serialized["source_platform"] == "GREENHOUSE"
    assert serialized["ats_type"] == "GREENHOUSE"


@pytest.mark.parametrize(
    ("url", "expected_api_host"),
    (
        (
            "https://boards.greenhouse.io/examplelabs/jobs/123456",
            "boards-api.greenhouse.io",
        ),
        (
            "https://job-boards.greenhouse.io/examplelabs/jobs/123456"
            "?gh_src=synthetic#app",
            "boards-api.greenhouse.io",
        ),
        (
            "https://boards.eu.greenhouse.io/examplelabs/jobs/123456",
            "boards-api.eu.greenhouse.io",
        ),
        (
            "https://job-boards.eu.greenhouse.io/examplelabs/jobs/123456/",
            "boards-api.eu.greenhouse.io",
        ),
    ),
)
@pytest.mark.asyncio
async def test_explicit_greenhouse_url_forms_are_supported(
    url: str,
    expected_api_host: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == expected_api_host
        return httpx.Response(200, json=_payload())

    result = await _reader(handler).read_job(ReadJobRequest(url=url))

    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.observation is not None
    assert result.observation.source_url == url


@pytest.mark.asyncio
async def test_missing_optional_fields_are_not_guessed() -> None:
    payload = _payload()
    payload.pop("first_published")
    payload.pop("absolute_url")
    payload.pop("location")

    result = await _reader(
        lambda request: httpx.Response(200, json=payload)
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.observation is not None
    assert result.observation.application_url is None
    assert result.observation.location == ""
    assert result.observation.posted_at is None
    assert result.observation.work_mode is WorkMode.UNKNOWN


@pytest.mark.parametrize(
    "url",
    (
        "https://jobs.example.invalid/examplelabs/123456",
        "https://support.greenhouse.io/examplelabs/jobs/123456",
        "https://boards-api.greenhouse.io/v1/boards/examplelabs/jobs/123456",
        "https://boards.greenhouse.io/examplelabs",
        "https://boards.greenhouse.io/examplelabs/jobs/not-numeric",
        "https://boards.greenhouse.io./examplelabs/jobs/123456",
        "https://boards.greenhouse.io:443/examplelabs/jobs/123456",
        "https://boards.greenhouse.io:8443/examplelabs/jobs/123456",
    ),
)
@pytest.mark.asyncio
async def test_unsupported_url_form_returns_without_http(url: str) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("unsupported URL must not perform HTTP")

    result = await _reader(handler).read_job(ReadJobRequest(url=url))

    assert result.status is ReadJobStatus.UNSUPPORTED
    assert result.reason_code is ReadJobReason.UNSUPPORTED_URL
    assert result.retryable is False
    assert result.observation is None
    assert called is False


@pytest.mark.parametrize(
    "url",
    (
        "",
        "job-boards.greenhouse.io/examplelabs/jobs/123456",
        "/examplelabs/jobs/123456",
        "ftp://job-boards.greenhouse.io/examplelabs/jobs/123456",
        "https://user@job-boards.greenhouse.io/examplelabs/jobs/123456",
    ),
)
@pytest.mark.asyncio
async def test_invalid_url_is_typed_failure_without_http(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid URL must not perform HTTP")

    result = await _reader(handler).read_job(ReadJobRequest(url=url))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.INVALID_URL
    assert result.retryable is False
    assert result.observation is None


@pytest.mark.parametrize(
    ("status_code", "reason"),
    (
        (404, ReadJobReason.JOB_NOT_FOUND),
        (410, ReadJobReason.JOB_CLOSED),
    ),
)
@pytest.mark.asyncio
async def test_missing_or_closed_job_has_stable_failure(
    status_code: int,
    reason: ReadJobReason,
) -> None:
    result = await _reader(
        lambda request: httpx.Response(status_code)
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is reason
    assert result.retryable is False
    assert result.observation is None


@pytest.mark.asyncio
async def test_timeout_is_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    result = await _reader(timeout).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_TIMEOUT
    assert result.retryable is True
    assert result.observation is None


@pytest.mark.parametrize(
    ("status_code", "reason", "retryable"),
    (
        (429, ReadJobReason.SOURCE_RATE_LIMITED, True),
        (503, ReadJobReason.SOURCE_UNAVAILABLE, True),
        (403, ReadJobReason.SOURCE_UNAVAILABLE, False),
    ),
)
@pytest.mark.asyncio
async def test_source_http_failures_are_typed(
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


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"id": 123456},
        {
            "id": 999999,
            "title": "Synthetic Engineer",
            "company_name": "Example Labs",
            "content": "Description",
        },
        {
            "id": 123456,
            "title": "Synthetic Engineer",
            "company_name": "Example Labs",
            "content": "",
        },
        {
            "id": 123456,
            "title": "Synthetic Engineer",
            "company_name": "Example Labs",
            "content": "Description",
            "location": "Vancouver",
        },
        {
            "id": 123456,
            "title": "Synthetic Engineer",
            "company_name": "Example Labs",
            "content": "Description",
            "first_published": "not-a-timestamp",
        },
    ),
)
@pytest.mark.asyncio
async def test_invalid_source_structure_is_not_an_empty_success(payload) -> None:
    result = await _reader(
        lambda request: httpx.Response(200, json=payload)
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_RESPONSE_INVALID
    assert result.retryable is False
    assert result.observation is None


def test_result_contract_rejects_failure_disguised_as_success() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        ReadJobResult(
            status=ReadJobStatus.SUCCEEDED,
            reason_code=ReadJobReason.SOURCE_UNAVAILABLE,
            retryable=True,
            observation=None,
        )


def test_unsafe_url_is_a_stable_non_retryable_failure() -> None:
    result = ReadJobResult.failed(ReadJobReason.UNSAFE_URL)

    assert ReadJobReason.UNSAFE_URL.value == "UNSAFE_URL"
    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.UNSAFE_URL
    assert result.retryable is False
    assert result.observation is None

    with pytest.raises(ValueError, match="retryable conflicts"):
        ReadJobResult(
            status=ReadJobStatus.FAILED,
            reason_code=ReadJobReason.UNSAFE_URL,
            retryable=True,
            observation=None,
        )

    invalid = ReadJobResult.failed(ReadJobReason.INVALID_URL)
    unsupported = ReadJobResult.failed(ReadJobReason.UNSUPPORTED_URL)
    assert invalid.status is ReadJobStatus.FAILED
    assert invalid.retryable is False
    assert unsupported.status is ReadJobStatus.UNSUPPORTED
    assert unsupported.retryable is False


@pytest.mark.asyncio
async def test_public_readers_have_no_persistence_or_execution_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_home = tmp_path / "synthetic-private-home"
    monkeypatch.setenv("JOBOPS_HOME", str(synthetic_home))
    monkeypatch.setattr(
        public_reader_module,
        "GreenhousePublicJobReader",
        lambda: _reader(
            lambda request: httpx.Response(200, json=_payload())
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
        "source_connectors/greenhouse.py",
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
