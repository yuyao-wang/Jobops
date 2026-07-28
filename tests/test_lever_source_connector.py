"""Focused contract tests for the read-only Lever public job reader."""

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
from source_connectors.lever import LeverPublicJobReader


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT / "tests" / "fixtures" / "source_connectors" / "lever_job.json"
)
SOURCE_URL = "https://jobs.lever.co/examplelabs/abc-123"
OBSERVED_AT = datetime(2026, 7, 27, 19, 30, tzinfo=timezone.utc)


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _reader(handler) -> LeverPublicJobReader:
    return LeverPublicJobReader(
        transport=httpx.MockTransport(handler),
        clock=lambda: OBSERVED_AT,
    )


@pytest.mark.asyncio
async def test_provider_neutral_entry_routes_lever_and_returns_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_payload())

    monkeypatch.setattr(
        public_reader_module,
        "LeverPublicJobReader",
        lambda: _reader(handler),
    )

    result = await read_public_job(ReadJobRequest(url=SOURCE_URL))

    assert calls == ["https://api.lever.co/v0/postings/examplelabs/abc-123"]
    assert result.status is ReadJobStatus.SUCCEEDED
    assert type(result.observation) is SourceJobObservation
    assert result.observation is not None
    observation = result.observation
    assert observation.source_platform is SourcePlatform.LEVER
    assert observation.source_job_id == "abc-123"
    assert observation.source_url == SOURCE_URL
    assert observation.application_url == f"{SOURCE_URL}/apply"
    assert observation.company == "examplelabs"
    assert observation.title == "Synthetic Reliability Engineer"
    assert observation.description == (
        "Build synthetic distributed systems and reliable developer tools."
    )
    assert observation.location == "Vancouver, BC"
    assert observation.work_mode is WorkMode.HYBRID
    assert observation.posted_at == "2026-07-20T14:00:00Z"
    assert observation.ats_type is AtsType.LEVER
    assert observation.observed_at == "2026-07-27T19:30:00Z"

    public_fields = {field.name for field in fields(SourceJobObservation)}
    assert public_fields == {
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
    assert {"text", "hostedUrl", "applyUrl", "categories", "createdAt"}.isdisjoint(
        public_fields
    )


@pytest.mark.parametrize(
    "url",
    (
        "https://jobs.lever.co/examplelabs/abc-123",
        "http://jobs.lever.co/examplelabs/abc-123/",
        "https://jobs.lever.co/examplelabs/abc-123?lever-source=synthetic#job",
        "https://jobs.lever.co/examplelabs/abc-123/apply",
        "https://jobs.lever.co/examplelabs/abc-123/apply/",
    ),
)
@pytest.mark.asyncio
async def test_explicit_lever_url_forms_are_supported(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == (
            "https://api.lever.co/v0/postings/examplelabs/abc-123"
        )
        return httpx.Response(200, json=_payload())

    reader = _reader(handler)
    result = await reader.read_job(ReadJobRequest(url=url))

    assert isinstance(reader, SourceJobReader)
    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.observation is not None
    assert result.observation.source_url == SOURCE_URL


@pytest.mark.asyncio
async def test_greenhouse_and_lever_use_same_observation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        public_reader_module,
        "LeverPublicJobReader",
        lambda: _reader(
            lambda request: httpx.Response(200, json=_payload())
        ),
    )

    result = await read_public_job(ReadJobRequest(url=SOURCE_URL))

    assert result.observation is not None
    assert type(result.observation) is SourceJobObservation
    assert [field.name for field in fields(ReadJobRequest)] == ["url"]
    serialized = json.loads(json.dumps(asdict(result.observation)))
    assert serialized["source_platform"] == "LEVER"
    assert serialized["ats_type"] == "LEVER"


@pytest.mark.asyncio
async def test_missing_optional_fields_are_not_guessed() -> None:
    payload = _payload()
    for field in (
        "applyUrl",
        "categories",
        "createdAt",
        "hostedUrl",
        "workplaceType",
    ):
        payload.pop(field)

    result = await _reader(
        lambda request: httpx.Response(200, json=payload)
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.SUCCEEDED
    assert result.observation is not None
    observation = result.observation
    assert observation.source_url == SOURCE_URL
    assert observation.application_url is None
    assert observation.location == ""
    assert observation.posted_at is None
    assert observation.work_mode is WorkMode.UNKNOWN


@pytest.mark.parametrize(
    "url",
    (
        "https://jobs.example.invalid/examplelabs/abc-123",
        "https://lever.co/examplelabs/abc-123",
        "https://api.lever.co/v0/postings/examplelabs/abc-123",
        "https://jobs.lever.co/examplelabs",
        "https://jobs.lever.co/examplelabs/abc-123/other",
        "https://jobs.lever.co./examplelabs/abc-123",
        "https://jobs.lever.co:443/examplelabs/abc-123",
        "https://jobs.lever.co:8443/examplelabs/abc-123",
    ),
)
@pytest.mark.asyncio
async def test_unsupported_lever_url_returns_without_http(url: str) -> None:
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
        "jobs.lever.co/examplelabs/abc-123",
        "/examplelabs/abc-123",
        "ftp://jobs.lever.co/examplelabs/abc-123",
        "https://user@jobs.lever.co/examplelabs/abc-123",
    ),
)
@pytest.mark.asyncio
async def test_invalid_lever_url_returns_without_http(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid URL must not perform HTTP")

    result = await _reader(handler).read_job(ReadJobRequest(url=url))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.INVALID_URL
    assert result.retryable is False
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
async def test_lever_http_failures_are_typed(
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
async def test_lever_timeout_is_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    result = await _reader(timeout).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_TIMEOUT
    assert result.retryable is True
    assert result.observation is None


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"id": "abc-123"},
        {
            "id": "different-id",
            "text": "Synthetic Engineer",
            "descriptionPlain": "Description",
        },
        {
            "id": "abc-123",
            "text": "",
            "descriptionPlain": "Description",
        },
        {
            "id": "abc-123",
            "text": "Synthetic Engineer",
            "descriptionPlain": "",
        },
        {
            "id": "abc-123",
            "text": "Synthetic Engineer",
            "descriptionPlain": "Description",
            "categories": "Vancouver",
        },
        {
            "id": "abc-123",
            "text": "Synthetic Engineer",
            "descriptionPlain": "Description",
            "createdAt": "not-a-timestamp",
        },
        {
            "id": "abc-123",
            "text": "Synthetic Engineer",
            "descriptionPlain": "Description",
            "hostedUrl": "https://jobs.lever.co/other/abc-123",
        },
    ),
)
@pytest.mark.asyncio
async def test_invalid_lever_response_is_not_empty_success(payload) -> None:
    result = await _reader(
        lambda request: httpx.Response(200, json=payload)
    ).read_job(ReadJobRequest(url=SOURCE_URL))

    assert result.status is ReadJobStatus.FAILED
    assert result.reason_code is ReadJobReason.SOURCE_RESPONSE_INVALID
    assert result.retryable is False
    assert result.observation is None


@pytest.mark.asyncio
async def test_unknown_url_constructs_no_known_reader_and_uses_generic(
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
        ReadJobRequest(url="https://careers.example.invalid/jobs/123")
    )

    assert result.status is ReadJobStatus.UNSUPPORTED
    assert result.reason_code is ReadJobReason.UNSUPPORTED_URL
    assert result.retryable is False
    assert result.observation is None
    assert generic_calls == 1


@pytest.mark.asyncio
async def test_lever_reader_has_no_persistence_or_execution_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_home = tmp_path / "synthetic-private-home"
    monkeypatch.setenv("JOBOPS_HOME", str(synthetic_home))
    monkeypatch.setattr(
        public_reader_module,
        "LeverPublicJobReader",
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
        "source_connectors/lever.py",
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
