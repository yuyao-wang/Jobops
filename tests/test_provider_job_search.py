"""Sanitized contracts for production job-feed search connectors."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from core.job_search import JobSearchRequest, JobSearchStatus
from source_connectors.contract import AtsType, SourcePlatform, WorkMode
from source_connectors.greenhouse_board import (
    BoundedHttpRequest,
    BoundedHttpResult,
    BoundedHttpStatus,
    JobSearchExecutionPolicy,
)
from source_connectors.provider_job_search import (
    AshbyBoardConfig,
    AshbyBoardJobSearch,
    GlassdoorPartnerConfig,
    GlassdoorPartnerJobSearch,
    JobviteFeedConfig,
    JobviteFeedJobSearch,
    LeverPostingsJobSearch,
    LeverSiteConfig,
)


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)


class FakeHttp:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.requests: list[BoundedHttpRequest] = []

    async def get(self, request: BoundedHttpRequest) -> BoundedHttpResult:
        self.requests.append(request)
        return BoundedHttpResult(
            BoundedHttpStatus.SUCCEEDED,
            response_status=200,
            headers={"content-type": "application/json"},
            content=json.dumps(self.payload).encode(),
        )


def _request(company: str) -> JobSearchRequest:
    return JobSearchRequest(
        request_id="synthetic-provider-search",
        company=company,
        title="Software Engineer",
        location="Calgary",
    )


def _policy(provider: str) -> JobSearchExecutionPolicy:
    return JobSearchExecutionPolicy(allowed_providers=(provider,))


def _assert_one(result, platform: SourcePlatform, ats: AtsType) -> None:
    assert result.status is JobSearchStatus.SUCCEEDED
    assert result.candidate_set is not None
    assert len(result.candidate_set.candidates) == 1
    candidate = result.candidate_set.candidates[0]
    assert candidate.source_platform is platform
    assert candidate.observation is not None
    assert candidate.observation.ats_type is ats
    assert candidate.observation.description == "Build reliable systems."


@pytest.mark.asyncio
async def test_ashby_public_board_returns_normalized_inline_observation() -> None:
    http = FakeHttp(
        {
            "apiVersion": "1",
            "jobs": [
                {
                    "title": "Software Engineer",
                    "location": "Calgary, AB",
                    "isListed": True,
                    "workplaceType": "Hybrid",
                    "descriptionPlain": "Build reliable systems.",
                    "publishedAt": "2026-08-01T10:00:00Z",
                    "jobUrl": (
                        "https://jobs.ashbyhq.com/example/"
                        "synthetic-posting"
                    ),
                    "applyUrl": (
                        "https://jobs.ashbyhq.com/example/"
                        "synthetic-posting/application"
                    ),
                }
            ],
        }
    )
    port = AshbyBoardJobSearch(
        config=AshbyBoardConfig("Example Ashby", "example"),
        http_port=http,
        policy=_policy("ASHBY"),
        clock=lambda: NOW,
    )

    result = await port.search(_request("Example Ashby"))

    _assert_one(result, SourcePlatform.ASHBY, AtsType.ASHBY)
    assert result.candidate_set.candidates[0].observation.work_mode is (
        WorkMode.HYBRID
    )
    assert http.requests[0].query == {"includeCompensation": "true"}


@pytest.mark.asyncio
async def test_lever_public_postings_feed_returns_inline_observation() -> None:
    http = FakeHttp(
        [
            {
                "id": "synthetic-posting",
                "text": "Software Engineer",
                "categories": {"location": "Calgary, AB"},
                "descriptionPlain": "Build reliable systems.",
                "hostedUrl": (
                    "https://jobs.lever.co/example/synthetic-posting"
                ),
                "applyUrl": (
                    "https://jobs.lever.co/example/"
                    "synthetic-posting/apply"
                ),
                "workplaceType": "on-site",
                "createdAt": 1785588000000,
            }
        ]
    )
    port = LeverPostingsJobSearch(
        config=LeverSiteConfig("Example Lever", "example"),
        http_port=http,
        policy=_policy("LEVER"),
        clock=lambda: NOW,
    )

    result = await port.search(_request("Example Lever"))

    _assert_one(result, SourcePlatform.LEVER, AtsType.LEVER)
    assert result.candidate_set.candidates[0].observation.work_mode is (
        WorkMode.ONSITE
    )
    assert http.requests[0].query == {"mode": "json"}


@pytest.mark.asyncio
async def test_glassdoor_partner_search_is_credential_gated_and_attributed() -> None:
    config = GlassdoorPartnerConfig(
        source_id="glassdoor-ca",
        partner_id="synthetic-partner-id",
        partner_key="synthetic-partner-key",
    )
    http = FakeHttp(
        {
            "response": {
                "jobListings": [
                    {
                        "jobListingId": "synthetic-listing",
                        "jobTitle": "Software Engineer",
                        "jobDescription": "Build reliable systems.",
                        "employer": {"name": "Example Glassdoor"},
                        "location": "Calgary, AB",
                        "jobViewUrl": (
                            "https://www.glassdoor.com/job-listing/"
                            "synthetic-listing"
                        ),
                        "applyUrl": "https://employer.example/jobs/123",
                        "postedAt": "2026-08-01T10:00:00Z",
                    }
                ]
            }
        }
    )
    port = GlassdoorPartnerJobSearch(
        config=config,
        http_port=http,
        policy=_policy("GLASSDOOR"),
        clock=lambda: NOW,
    )

    result = await port.search(_request("Example Glassdoor"))

    _assert_one(result, SourcePlatform.GLASSDOOR, AtsType.UNKNOWN)
    request = http.requests[0]
    assert request.secret_query == {
        "t.p": "synthetic-partner-id",
        "t.k": "synthetic-partner-key",
    }
    assert "synthetic-partner-key" not in repr(config)
    assert "synthetic-partner-key" not in repr(request)


@pytest.mark.asyncio
async def test_jobvite_licensed_feed_filters_external_distributed_jobs() -> None:
    config = JobviteFeedConfig(
        canonical_company="Example Jobvite",
        career_site="example",
        api_key="synthetic-api-key",
        api_secret="synthetic-api-secret",
    )
    http = FakeHttp(
        {
            "requisitions": [
                {
                    "eId": "synthetic-posting",
                    "title": "Software Engineer",
                    "description": "Build reliable systems.",
                    "location": "Calgary, AB",
                    "jobState": "Open",
                    "postingType": "External",
                    "distribution": True,
                    "sentDate": 1785588000000,
                },
                {
                    "eId": "internal-posting",
                    "title": "Software Engineer",
                    "description": "Must never be collected.",
                    "location": "Calgary, AB",
                    "jobState": "Open",
                    "postingType": "Internal",
                    "distribution": True,
                },
            ]
        }
    )
    port = JobviteFeedJobSearch(
        config=config,
        http_port=http,
        policy=_policy("JOBVITE"),
        clock=lambda: NOW,
    )

    result = await port.search(_request("Example Jobvite"))

    _assert_one(result, SourcePlatform.JOBVITE, AtsType.JOBVITE)
    request = http.requests[0]
    assert request.secret_query == {
        "api": "synthetic-api-key",
        "sc": "synthetic-api-secret",
    }
    assert "synthetic-api-secret" not in repr(config)
    assert "synthetic-api-secret" not in repr(request)
