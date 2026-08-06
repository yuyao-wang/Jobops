"""Contract and boundary tests for bounded Greenhouse board candidate search."""

from __future__ import annotations

import ast
import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from core.job_search import (
    CandidateSet,
    JobSearchPort,
    JobSearchReason,
    JobSearchRequest,
    JobSearchResult,
    JobSearchStatus,
    SearchCandidate,
    search_jobs,
)
from source_connectors.contract import (
    ReadJobReason,
    ReadJobRequest,
    ReadJobResult,
    SourceJobObservation,
    SourcePlatform,
)
from source_connectors.greenhouse_board import (
    GreenhouseBoardConfig,
    GreenhouseBoardJobSearch,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "source_connectors"
    / "greenhouse_board_jobs.json"
)
CREATED_AT = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
BOARD = GreenhouseBoardConfig(
    canonical_company="Example Labs",
    board_token="examplelabs",
    aliases=("ExampleLabs",),
)


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _searcher(handler, *, boards=(BOARD,)) -> GreenhouseBoardJobSearch:
    return GreenhouseBoardJobSearch(
        boards=boards,
        transport=httpx.MockTransport(handler),
        clock=lambda: CREATED_AT,
        candidate_set_id_factory=lambda: "candidate-set-synthetic",
    )


def _request(
    *,
    company: str = "Example Labs",
    title: str = "Machine Learning Engineer",
    location: str | None = None,
) -> JobSearchRequest:
    return JobSearchRequest(
        request_id="search-request-synthetic",
        company=company,
        title=title,
        location=location,
    )


def _successful_set(candidates: tuple[SearchCandidate, ...] = ()) -> CandidateSet:
    return CandidateSet(
        candidate_set_id="candidate-set-contract",
        request_id="search-request-contract",
        candidates=candidates,
        created_at=CREATED_AT,
    )


def test_search_contract_is_independent_from_public_read_contract() -> None:
    assert [status.value for status in JobSearchStatus] == [
        "SUCCEEDED",
        "FAILED",
        "UNSUPPORTED",
    ]
    assert [reason.value for reason in JobSearchReason] == [
        "INVALID_REQUEST",
        "UNSUPPORTED_COMPANY",
        "PROVIDER_CONFIGURATION_ERROR",
        "SOURCE_TIMEOUT",
        "SOURCE_RATE_LIMITED",
        "NETWORK_UNAVAILABLE",
        "HTTP_ERROR",
        "REDIRECT_REJECTED",
        "RESPONSE_TOO_LARGE",
        "UNSUPPORTED_CONTENT_TYPE",
        "MALFORMED_RESPONSE",
        "CANDIDATE_VALIDATION_FAILED",
        "SOURCE_RESPONSE_INVALID",
        "SOURCE_UNAVAILABLE",
    ]
    assert [field.name for field in fields(JobSearchRequest)] == [
        "request_id",
        "company",
        "title",
        "location",
        "title_any",
        "result_limit",
    ]
    assert type(JobSearchReason.SOURCE_TIMEOUT) is JobSearchReason
    assert type(ReadJobReason.SOURCE_TIMEOUT) is ReadJobReason
    assert type(JobSearchReason.SOURCE_TIMEOUT) is not type(
        ReadJobReason.SOURCE_TIMEOUT
    )
    assert [field.name for field in fields(ReadJobRequest)] == ["url"]
    assert [field.name for field in fields(ReadJobResult)] == [
        "status",
        "reason_code",
        "retryable",
        "observation",
    ]
    assert [field.name for field in fields(SourceJobObservation)] == [
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
    ]
    assert [reason.value for reason in ReadJobReason] == [
        "INVALID_URL",
        "UNSAFE_URL",
        "UNSUPPORTED_URL",
        "JOB_NOT_FOUND",
        "JOB_CLOSED",
        "SOURCE_TIMEOUT",
        "SOURCE_RATE_LIMITED",
        "SOURCE_RESPONSE_INVALID",
        "SOURCE_UNAVAILABLE",
    ]
    source = (ROOT / "core" / "job_search.py").read_text(encoding="utf-8")
    assert "ReadJobResult" not in source
    assert "ReadJobReason" not in source


def test_search_result_contract_accepts_empty_success() -> None:
    result = JobSearchResult.succeeded(_successful_set())

    assert result.status is JobSearchStatus.SUCCEEDED
    assert result.reason_code is None
    assert result.retryable is False
    assert result.candidate_set is not None
    assert result.candidate_set.candidates == ()


@pytest.mark.parametrize(
    "result",
    (
        JobSearchResult.failed(JobSearchReason.INVALID_REQUEST),
        JobSearchResult.unsupported(),
        JobSearchResult.failed(JobSearchReason.SOURCE_TIMEOUT),
        JobSearchResult.failed(JobSearchReason.SOURCE_RATE_LIMITED),
        JobSearchResult.failed(JobSearchReason.SOURCE_UNAVAILABLE),
        JobSearchResult.failed(JobSearchReason.SOURCE_RESPONSE_INVALID),
    ),
)
def test_search_reason_retry_policy(result: JobSearchResult) -> None:
    expected_retryable = result.reason_code in {
        JobSearchReason.SOURCE_TIMEOUT,
        JobSearchReason.SOURCE_RATE_LIMITED,
        JobSearchReason.SOURCE_UNAVAILABLE,
    }
    assert result.retryable is expected_retryable
    assert result.candidate_set is None


@pytest.mark.parametrize(
    "kwargs",
    (
        {
            "status": JobSearchStatus.SUCCEEDED,
            "reason_code": JobSearchReason.SOURCE_TIMEOUT,
            "retryable": True,
            "candidate_set": None,
        },
        {
            "status": JobSearchStatus.FAILED,
            "reason_code": None,
            "retryable": False,
            "candidate_set": None,
        },
        {
            "status": JobSearchStatus.UNSUPPORTED,
            "reason_code": JobSearchReason.INVALID_REQUEST,
            "retryable": False,
            "candidate_set": None,
        },
        {
            "status": JobSearchStatus.FAILED,
            "reason_code": JobSearchReason.INVALID_REQUEST,
            "retryable": False,
            "candidate_set": _successful_set(),
        },
    ),
)
def test_search_result_rejects_conflicting_state(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        JobSearchResult(**kwargs)


@pytest.mark.parametrize(
    ("reason", "retryable"),
    (
        (JobSearchReason.INVALID_REQUEST, True),
        (JobSearchReason.UNSUPPORTED_COMPANY, True),
        (JobSearchReason.SOURCE_RESPONSE_INVALID, True),
        (JobSearchReason.SOURCE_TIMEOUT, False),
        (JobSearchReason.SOURCE_RATE_LIMITED, False),
    ),
)
def test_search_result_rejects_retry_policy_conflicts(
    reason: JobSearchReason,
    retryable: bool,
) -> None:
    status = (
        JobSearchStatus.UNSUPPORTED
        if reason is JobSearchReason.UNSUPPORTED_COMPANY
        else JobSearchStatus.FAILED
    )
    with pytest.raises(ValueError, match="conflict"):
        JobSearchResult(
            status=status,
            reason_code=reason,
            retryable=retryable,
            candidate_set=None,
        )


@pytest.mark.parametrize(
    "search_request",
    (
        _request(company=" "),
        _request(title="\t"),
        _request(location="  "),
    ),
)
@pytest.mark.asyncio
async def test_invalid_request_does_not_call_search_port(
    search_request: JobSearchRequest,
) -> None:
    class UnexpectedPort:
        calls = 0

        async def search(self, request: JobSearchRequest) -> JobSearchResult:
            self.calls += 1
            raise AssertionError("invalid request must not reach a source")

    port = UnexpectedPort()
    result = await search_jobs(search_request, port=port)

    assert result.status is JobSearchStatus.FAILED
    assert result.reason_code is JobSearchReason.INVALID_REQUEST
    assert result.retryable is False
    assert result.candidate_set is None
    assert port.calls == 0


@pytest.mark.parametrize("company", ("Example Labs", "  example labs  ", "examplelabs"))
@pytest.mark.asyncio
async def test_known_company_and_explicit_alias_use_one_board_get(
    company: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert request.url == (
            "https://boards-api.greenhouse.io/"
            "v1/boards/examplelabs/jobs?content=true"
        )
        assert request.url.query == b"content=true"
        return httpx.Response(200, json=_payload())

    port = _searcher(handler)
    assert isinstance(port, JobSearchPort)
    result = await search_jobs(_request(company=company), port=port)

    assert result.status is JobSearchStatus.SUCCEEDED
    assert result.candidate_set is not None
    assert calls == 1
    first = result.candidate_set.candidates[0]
    assert first.source_job_id == "1001"
    assert first.observation is not None
    assert first.observation.description == (
        "This description must not affect candidate matching."
    )


@pytest.mark.parametrize("company", ("Unknown Labs", "Example", "Labs"))
@pytest.mark.asyncio
async def test_unknown_or_substring_company_is_unsupported_without_http(
    company: str,
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("unknown company must not perform HTTP")

    result = await search_jobs(
        _request(company=company),
        port=_searcher(handler),
    )

    assert result.status is JobSearchStatus.UNSUPPORTED
    assert result.reason_code is JobSearchReason.UNSUPPORTED_COMPANY
    assert result.retryable is False
    assert result.candidate_set is None
    assert called is False


@pytest.mark.asyncio
async def test_title_matching_sorting_limit_and_candidate_shape() -> None:
    result = await search_jobs(
        _request(title="  MACHINE   learning--ENGINEER "),
        port=_searcher(
            lambda request: httpx.Response(200, json=_payload())
        ),
    )

    assert result.status is JobSearchStatus.SUCCEEDED
    assert result.candidate_set is not None
    candidates = result.candidate_set.candidates
    assert len(candidates) == 10
    assert candidates[0].title == "Machine Learning Engineer"
    assert candidates[0].company == "Example Labs"
    assert candidates[0].source_platform is SourcePlatform.GREENHOUSE
    assert candidates[0].source_job_id == "1001"
    assert candidates[0].source_url == (
        "https://job-boards.greenhouse.io/examplelabs/jobs/1001"
    )
    assert all(
        candidate.source_url.startswith(
            "https://job-boards.greenhouse.io/examplelabs/jobs/"
        )
        for candidate in candidates
    )
    assert not hasattr(candidates[0], "description")
    assert not hasattr(candidates[0], "job_id")
    assert not hasattr(candidates[0], "revision")
    assert not hasattr(candidates[0], "content_hash")
    assert not hasattr(candidates[0], "priority")
    assert not hasattr(candidates[0], "application_plan")
    assert [candidate.source_job_id for candidate in candidates] == [
        "1001",
        "1003",
        "1005",
        "1006",
        "1007",
        "1008",
        "1009",
        "1010",
        "1011",
        "1012",
    ]


@pytest.mark.asyncio
async def test_approved_role_phrases_are_or_matched_without_six_job_selection() -> None:
    request = JobSearchRequest(
        request_id="approved-role-search",
        company="Example Labs",
        title="Machine Learning Engineer",
        title_any=("Machine Learning Engineer", "Backend Engineer"),
        result_limit=1000,
    )

    result = await search_jobs(
        request,
        port=_searcher(
            lambda request: httpx.Response(200, json=_payload())
        ),
    )

    assert result.candidate_set is not None
    identifiers = tuple(
        candidate.source_job_id
        for candidate in result.candidate_set.candidates
    )
    assert "1004" in identifiers
    assert len(identifiers) == 12


@pytest.mark.asyncio
async def test_location_filter_is_deterministic_and_optional() -> None:
    async def run(location: str | None) -> JobSearchResult:
        return await search_jobs(
            _request(location=location),
            port=_searcher(
                lambda request: httpx.Response(200, json=_payload())
            ),
        )

    unfiltered = await run(None)
    filtered = await run("  VANCOUVER--BC ")
    broader = await run("Vancouver")

    assert unfiltered.candidate_set is not None
    assert filtered.candidate_set is not None
    assert broader.candidate_set is not None
    assert len(unfiltered.candidate_set.candidates) == 10
    assert [item.source_job_id for item in filtered.candidate_set.candidates] == [
        "1001"
    ]
    assert [item.source_job_id for item in broader.candidate_set.candidates] == [
        "1001",
        "1002",
    ]


@pytest.mark.asyncio
async def test_description_is_not_used_for_title_or_location_matching() -> None:
    result = await search_jobs(
        _request(title="Quantum Botanist", location="Vancouver"),
        port=_searcher(
            lambda request: httpx.Response(200, json=_payload())
        ),
    )

    assert result.status is JobSearchStatus.SUCCEEDED
    assert result.candidate_set is not None
    assert result.candidate_set.candidates == ()


@pytest.mark.parametrize(
    ("title", "expected_ids"),
    (
        ("Backend Engineer", ("1004",)),
        ("Unlisted Role", ()),
    ),
)
@pytest.mark.asyncio
async def test_one_and_zero_candidate_results_are_successful(
    title: str,
    expected_ids: tuple[str, ...],
) -> None:
    result = await search_jobs(
        _request(title=title),
        port=_searcher(
            lambda request: httpx.Response(200, json=_payload())
        ),
    )

    assert result.status is JobSearchStatus.SUCCEEDED
    assert result.reason_code is None
    assert result.retryable is False
    assert result.candidate_set is not None
    assert tuple(
        candidate.source_job_id
        for candidate in result.candidate_set.candidates
    ) == expected_ids


@pytest.mark.asyncio
async def test_multiple_candidates_are_returned_without_selection() -> None:
    result = await search_jobs(
        _request(),
        port=_searcher(
            lambda request: httpx.Response(200, json=_payload())
        ),
    )

    assert result.candidate_set is not None
    assert len(result.candidate_set.candidates) > 1
    assert result.candidate_set.candidate_set_id == "candidate-set-synthetic"
    assert result.candidate_set.request_id == "search-request-synthetic"
    assert result.candidate_set.created_at == CREATED_AT


@pytest.mark.parametrize(
    ("status_code", "reason", "retryable"),
    (
        (429, JobSearchReason.SOURCE_RATE_LIMITED, True),
        (503, JobSearchReason.HTTP_ERROR, True),
        (403, JobSearchReason.HTTP_ERROR, False),
    ),
)
@pytest.mark.asyncio
async def test_board_http_failures_are_typed(
    status_code: int,
    reason: JobSearchReason,
    retryable: bool,
) -> None:
    result = await search_jobs(
        _request(),
        port=_searcher(lambda request: httpx.Response(status_code)),
    )

    assert result.status is JobSearchStatus.FAILED
    assert result.reason_code is reason
    assert result.retryable is retryable
    assert result.candidate_set is None


@pytest.mark.asyncio
async def test_board_timeout_is_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    result = await search_jobs(_request(), port=_searcher(timeout))

    assert result.status is JobSearchStatus.FAILED
    assert result.reason_code is JobSearchReason.SOURCE_TIMEOUT
    assert result.retryable is True
    assert result.candidate_set is None


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {},
        {"jobs": "not-a-list"},
        {"jobs": [None]},
        {"jobs": [{"id": 1001, "title": "Engineer"}]},
        {
            "jobs": [
                {
                    "id": 1001,
                    "title": "",
                    "location": {"name": "Vancouver"},
                    "absolute_url": (
                        "https://job-boards.greenhouse.io/"
                        "examplelabs/jobs/1001"
                    ),
                }
            ]
        },
    ),
)
@pytest.mark.asyncio
async def test_invalid_board_response_is_not_empty_success(payload) -> None:
    result = await search_jobs(
        _request(),
        port=_searcher(lambda request: httpx.Response(200, json=payload)),
    )

    assert result.status is JobSearchStatus.FAILED
    assert result.reason_code in {
        JobSearchReason.MALFORMED_RESPONSE,
        JobSearchReason.CANDIDATE_VALIDATION_FAILED,
    }
    assert result.retryable is False
    assert result.candidate_set is None


@pytest.mark.asyncio
async def test_custom_greenhouse_career_url_uses_safe_board_identity() -> None:
    payload = _payload()
    payload["jobs"][0]["absolute_url"] = (
        "https://careers.example.com/jobs/1001?gh_jid=1001"
    )

    result = await search_jobs(
        _request(),
        port=_searcher(lambda request: httpx.Response(200, json=payload)),
    )

    assert result.status is JobSearchStatus.SUCCEEDED
    assert result.candidate_set is not None
    assert result.candidate_set.candidates[0].source_url == (
        "https://job-boards.greenhouse.io/examplelabs/jobs/1001"
    )
    assert result.candidate_set.candidates[0].observation is not None
    assert result.candidate_set.candidates[0].observation.application_url == (
        "https://careers.example.com/jobs/1001?gh_jid=1001"
    )


@pytest.mark.asyncio
async def test_custom_greenhouse_career_url_requires_matching_job_id() -> None:
    payload = _payload()
    payload["jobs"][0]["absolute_url"] = (
        "https://careers.example.com/jobs/1001?gh_jid=other"
    )

    result = await search_jobs(
        _request(),
        port=_searcher(lambda request: httpx.Response(200, json=payload)),
    )

    assert result.status is JobSearchStatus.FAILED
    assert result.reason_code is JobSearchReason.CANDIDATE_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_search_has_no_persistence_reader_or_execution_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic_home = tmp_path / "synthetic-private-home"
    monkeypatch.setenv("JOBOPS_HOME", str(synthetic_home))

    result = await search_jobs(
        _request(title="Backend Engineer"),
        port=_searcher(
            lambda request: httpx.Response(200, json=_payload())
        ),
    )

    assert result.status is JobSearchStatus.SUCCEEDED
    assert not synthetic_home.exists()

    forbidden_roots = {
        "adapters",
        "anthropic",
        "csv",
        "openai",
        "playwright",
        "utils",
    }
    forbidden_symbols = {
        "read_public_job",
        "run_discovery",
        "PendingIntake",
        "JobIntakeProposal",
    }
    for relative_path in (
        "core/job_search.py",
        "source_connectors/greenhouse_board.py",
    ):
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_roots = {
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        imported_roots.update(
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        imported_symbols = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert imported_roots.isdisjoint(forbidden_roots)
        assert imported_symbols.isdisjoint(forbidden_symbols)
