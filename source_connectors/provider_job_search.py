"""Bounded production search for public and credential-gated job feeds."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from core.job_search import (
    CandidateSet,
    JobSearchReason,
    JobSearchRequest,
    JobSearchResult,
    SearchCandidate,
    canonicalize_search_company,
    canonicalize_search_match_text,
)
from source_connectors.contract import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)
from source_connectors.greenhouse_board import (
    BoundedHttpRequest,
    BoundedHttpResult,
    BoundedHttpStatus,
    BoundedJobSearchHttpPort,
    JobSearchExecutionPolicy,
)


ASHBY_JOB_SEARCH_ADAPTER_VERSION = "ashby-board-search-v1"
LEVER_JOB_SEARCH_ADAPTER_VERSION = "lever-postings-search-v1"
GLASSDOOR_JOB_SEARCH_ADAPTER_VERSION = "glassdoor-partner-search-v1"
JOBVITE_JOB_SEARCH_ADAPTER_VERSION = "jobvite-feed-search-v1"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SOURCE_ID = re.compile(r"[A-Za-z0-9._:-]{1,240}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_timestamp(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool):
        raise ValueError("source timestamp is invalid")
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError("source timestamp is invalid")
    return _rfc3339(value)


def _text(value: Any, *, name: str, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = " ".join(value.split())
    if (not empty and not normalized) or len(normalized) > maximum:
        raise ValueError(f"{name} is outside the source contract")
    return normalized


def _url(value: Any, *, hosts: tuple[str, ...] | None = None) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("source URL is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or (hosts is not None and parsed.hostname.casefold() not in hosts)
    ):
        raise ValueError("source URL is invalid")
    return value


def _normalized(value: str, *, maximum: int) -> str:
    return canonicalize_search_match_text(
        value,
        name="search match text",
        maximum=maximum,
    )


def _contains(container: str, phrase: str) -> bool:
    return f" {phrase} " in f" {container} "


def _matches(request: JobSearchRequest, *, title: str, location: str) -> bool:
    query_titles = tuple(
        dict.fromkeys(
            _normalized(value, maximum=240)
            for value in (request.title_any or (request.title,))
        )
    )
    candidate_title = _normalized(title, maximum=240)
    if candidate_title not in query_titles and not any(
        _contains(candidate_title, query_title)
        for query_title in query_titles
    ):
        return False
    if request.location is None:
        return True
    if not location:
        return False
    query_location = _normalized(request.location, maximum=320)
    candidate_location = _normalized(location, maximum=320)
    return _contains(candidate_location, query_location) or _contains(
        query_location, candidate_location
    )


def _candidate_set(
    *,
    version: str,
    source_id: str,
    request: JobSearchRequest,
    candidates: list[SearchCandidate],
    created_at: datetime,
    limit: int,
) -> CandidateSet:
    query_titles = {
        _normalized(value, maximum=240)
        for value in (request.title_any or (request.title,))
    }
    effective_limit = request.result_limit or limit
    ordered = sorted(
        candidates,
        key=lambda item: (
            0
            if _normalized(item.title, maximum=240)
            in query_titles
            else 1,
            _normalized(item.title, maximum=240),
            item.source_url,
        ),
    )[:effective_limit]
    payload = {
        "version": version,
        "source_id": source_id,
        "request_id": request.request_id,
        "candidates": [
            (item.candidate_id, item.source_job_id, item.source_url)
            for item in ordered
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return CandidateSet(
        candidate_set_id=f"candidate-set-{digest[:32]}",
        request_id=request.request_id,
        candidates=tuple(ordered),
        created_at=created_at,
    )


def _http_failure(result: Any) -> JobSearchResult | None:
    if not isinstance(result, BoundedHttpResult):
        return JobSearchResult.failed(JobSearchReason.NETWORK_UNAVAILABLE)
    status_reasons = {
        BoundedHttpStatus.TIMEOUT: JobSearchReason.SOURCE_TIMEOUT,
        BoundedHttpStatus.NETWORK_UNAVAILABLE: (
            JobSearchReason.NETWORK_UNAVAILABLE
        ),
        BoundedHttpStatus.REDIRECT_REJECTED: JobSearchReason.REDIRECT_REJECTED,
        BoundedHttpStatus.RESPONSE_TOO_LARGE: JobSearchReason.RESPONSE_TOO_LARGE,
    }
    reason = status_reasons.get(result.status)
    if reason is not None:
        return JobSearchResult.failed(reason)
    if (
        result.response_status is None
        or result.headers is None
        or result.content is None
    ):
        return JobSearchResult.failed(JobSearchReason.NETWORK_UNAVAILABLE)
    if result.response_status in {408, 504}:
        return JobSearchResult.failed(JobSearchReason.SOURCE_TIMEOUT)
    if result.response_status == 429:
        return JobSearchResult.failed(JobSearchReason.SOURCE_RATE_LIMITED)
    if result.response_status >= 500:
        return JobSearchResult.failed(JobSearchReason.HTTP_ERROR, retryable=True)
    if result.response_status != 200:
        return JobSearchResult.failed(JobSearchReason.HTTP_ERROR, retryable=False)
    content_type = result.headers.get("content-type", "")
    if content_type.partition(";")[0].strip().casefold() not in {
        "application/json",
        "application/vnd.api+json",
    }:
        return JobSearchResult.failed(
            JobSearchReason.UNSUPPORTED_CONTENT_TYPE
        )
    return None


def _json(result: BoundedHttpResult) -> Any:
    assert result.content is not None
    try:
        return json.loads(result.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source response is not JSON") from exc


@dataclass(frozen=True, slots=True)
class AshbyBoardConfig:
    canonical_company: str
    board_name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_tenant_config(
            self.canonical_company, self.board_name, self.aliases
        )


@dataclass(frozen=True, slots=True)
class LeverSiteConfig:
    canonical_company: str
    site_name: str
    aliases: tuple[str, ...] = ()
    region: str = "GLOBAL"

    def __post_init__(self) -> None:
        _validate_tenant_config(
            self.canonical_company, self.site_name, self.aliases
        )
        if self.region not in {"GLOBAL", "EU"}:
            raise ValueError("Lever region is invalid")


@dataclass(frozen=True, slots=True)
class GlassdoorPartnerConfig:
    source_id: str
    partner_id: str = field(repr=False)
    partner_key: str = field(repr=False)
    user_ip: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if _TOKEN.fullmatch(self.source_id) is None:
            raise ValueError("Glassdoor source ID is invalid")
        for value in (self.partner_id, self.partner_key):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError("Glassdoor partner credentials are invalid")
        if (
            not isinstance(self.user_ip, str)
            or not self.user_ip
            or len(self.user_ip) > 64
        ):
            raise ValueError("Glassdoor user IP is invalid")


@dataclass(frozen=True, slots=True)
class JobviteFeedConfig:
    canonical_company: str
    career_site: str
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_tenant_config(
            self.canonical_company, self.career_site, self.aliases
        )
        for value in (self.api_key, self.api_secret):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError("Jobvite feed credentials are invalid")


def _validate_tenant_config(
    company: str, source_id: str, aliases: tuple[str, ...]
) -> None:
    canonicalize_search_company(company)
    if _TOKEN.fullmatch(source_id) is None:
        raise ValueError("provider source ID is invalid")
    if not isinstance(aliases, tuple):
        raise TypeError("aliases must be a tuple")
    names = [canonicalize_search_company(company)]
    names.extend(canonicalize_search_company(value) for value in aliases)
    if len(names) != len(set(names)):
        raise ValueError("company names and aliases must be unique")


def _supports(request: JobSearchRequest, company: str, aliases: tuple[str, ...]) -> bool:
    requested = canonicalize_search_company(request.company)
    return requested in {
        canonicalize_search_company(value) for value in (company, *aliases)
    }


def _provenance(
    *,
    company_field: str,
    title_field: str,
    description_field: str,
    location_field: str,
    posted_field: str,
) -> tuple[FieldProvenance, ...]:
    return (
        FieldProvenance("source_platform", ProvenanceSource.SYSTEM, "adapter"),
        FieldProvenance("source_job_id", ProvenanceSource.SOURCE_API, "id"),
        FieldProvenance("source_url", ProvenanceSource.SOURCE_API, "job_url"),
        FieldProvenance(
            "application_url", ProvenanceSource.SOURCE_API, "apply_url"
        ),
        FieldProvenance("company", ProvenanceSource.REQUEST, company_field),
        FieldProvenance("title", ProvenanceSource.SOURCE_API, title_field),
        FieldProvenance(
            "description", ProvenanceSource.SOURCE_API, description_field
        ),
        FieldProvenance("location", ProvenanceSource.SOURCE_API, location_field),
        FieldProvenance("work_mode", ProvenanceSource.SOURCE_API, "workplace"),
        FieldProvenance("posted_at", ProvenanceSource.SOURCE_API, posted_field),
        FieldProvenance("ats_type", ProvenanceSource.SYSTEM, "adapter"),
        FieldProvenance("observed_at", ProvenanceSource.SYSTEM, "clock"),
    )


class AshbyBoardJobSearch:
    def __init__(self, *, config: AshbyBoardConfig, http_port: BoundedJobSearchHttpPort, policy: JobSearchExecutionPolicy, clock=_utc_now) -> None:
        self.config, self.http, self.policy, self.clock = config, http_port, policy, clock
        if "ASHBY" not in policy.allowed_providers:
            raise ValueError("Ashby is disabled by policy")

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        if not _supports(request, self.config.canonical_company, self.config.aliases):
            return JobSearchResult.unsupported()
        result = await self.http.get(BoundedHttpRequest(
            url=f"https://api.ashbyhq.com/posting-api/job-board/{quote(self.config.board_name, safe='')}",
            allowed_hosts=("api.ashbyhq.com",), headers={"Accept": "application/json", "User-Agent": self.policy.user_agent_version},
            connect_timeout_seconds=self.policy.connect_timeout_seconds, read_timeout_seconds=self.policy.read_timeout_seconds,
            max_redirects=self.policy.max_redirects, max_response_bytes=self.policy.max_response_bytes,
            query={"includeCompensation": "true"},
        ))
        failure = _http_failure(result)
        if failure: return failure
        try:
            payload = _json(result)
            if not isinstance(payload, Mapping) or payload.get("apiVersion") != "1" or not isinstance(payload.get("jobs"), list):
                raise ValueError("Ashby response is malformed")
            observed_at = self.clock()
            candidates: list[SearchCandidate] = []
            seen: set[str] = set()
            for item in payload["jobs"]:
                if not isinstance(item, Mapping) or item.get("isListed") is False:
                    continue
                source_url = _url(item.get("jobUrl"), hosts=("jobs.ashbyhq.com",))
                path = tuple(part for part in urlsplit(source_url).path.split("/") if part)
                if len(path) < 2 or _SOURCE_ID.fullmatch(path[-1]) is None:
                    raise ValueError("Ashby job URL has no stable posting ID")
                source_id = path[-1]
                title = _text(item.get("title"), name="title", maximum=240)
                location = _text(item.get("location", ""), name="location", maximum=320, empty=True)
                if not _matches(request, title=title, location=location): continue
                if source_id in seen: continue
                seen.add(source_id)
                description = _text(item.get("descriptionPlain"), name="descriptionPlain", maximum=100_000)
                application_url = _url(item.get("applyUrl"), hosts=("jobs.ashbyhq.com",))
                work_mode = {"onsite": WorkMode.ONSITE, "remote": WorkMode.REMOTE, "hybrid": WorkMode.HYBRID}.get(str(item.get("workplaceType", "")).casefold(), WorkMode.UNKNOWN)
                observation = SourceJobObservation(
                    SourcePlatform.ASHBY, source_id, source_url, application_url,
                    self.config.canonical_company.strip(), title, description, location,
                    work_mode, _source_timestamp(item.get("publishedAt")), AtsType.ASHBY,
                    _rfc3339(observed_at), _provenance(company_field="board.company", title_field="title", description_field="descriptionPlain", location_field="location", posted_field="publishedAt"),
                )
                candidates.append(SearchCandidate(f"ashby:{self.config.board_name}:{source_id}", observation.company, title, location or None, SourcePlatform.ASHBY, source_url, source_id, observation))
            return JobSearchResult.succeeded(_candidate_set(version=ASHBY_JOB_SEARCH_ADAPTER_VERSION, source_id=self.config.board_name, request=request, candidates=candidates, created_at=observed_at, limit=self.policy.max_results_per_query))
        except (TypeError, ValueError, OverflowError, OSError):
            return JobSearchResult.failed(JobSearchReason.CANDIDATE_VALIDATION_FAILED)


class LeverPostingsJobSearch:
    def __init__(self, *, config: LeverSiteConfig, http_port: BoundedJobSearchHttpPort, policy: JobSearchExecutionPolicy, clock=_utc_now) -> None:
        self.config, self.http, self.policy, self.clock = config, http_port, policy, clock
        if "LEVER" not in policy.allowed_providers: raise ValueError("Lever is disabled by policy")

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        if not _supports(request, self.config.canonical_company, self.config.aliases): return JobSearchResult.unsupported()
        api_host = "api.eu.lever.co" if self.config.region == "EU" else "api.lever.co"
        jobs_host = "jobs.eu.lever.co" if self.config.region == "EU" else "jobs.lever.co"
        result = await self.http.get(BoundedHttpRequest(
            url=f"https://{api_host}/v0/postings/{quote(self.config.site_name, safe='')}", allowed_hosts=(api_host,),
            headers={"Accept": "application/json", "User-Agent": self.policy.user_agent_version},
            connect_timeout_seconds=self.policy.connect_timeout_seconds, read_timeout_seconds=self.policy.read_timeout_seconds,
            max_redirects=self.policy.max_redirects, max_response_bytes=self.policy.max_response_bytes, query={"mode": "json"},
        ))
        failure = _http_failure(result)
        if failure: return failure
        try:
            payload = _json(result)
            if not isinstance(payload, list): raise ValueError("Lever response must be a list")
            observed_at = self.clock(); candidates: list[SearchCandidate] = []; seen: set[str] = set()
            for item in payload:
                if not isinstance(item, Mapping): raise ValueError("Lever job must be an object")
                source_id = _text(item.get("id"), name="id", maximum=240)
                title = _text(item.get("text"), name="text", maximum=240)
                categories = item.get("categories") or {}
                if not isinstance(categories, Mapping): raise ValueError("Lever categories are invalid")
                location = _text(categories.get("location", ""), name="location", maximum=320, empty=True)
                if not _matches(request, title=title, location=location): continue
                if source_id in seen: continue
                seen.add(source_id)
                source_url = _url(item.get("hostedUrl"), hosts=(jobs_host,))
                application_url = _url(item.get("applyUrl"), hosts=(jobs_host,))
                description = _text(item.get("descriptionPlain"), name="descriptionPlain", maximum=100_000)
                work_mode = {"on-site": WorkMode.ONSITE, "onsite": WorkMode.ONSITE, "remote": WorkMode.REMOTE, "hybrid": WorkMode.HYBRID}.get(str(item.get("workplaceType", "")).casefold(), WorkMode.UNKNOWN)
                observation = SourceJobObservation(
                    SourcePlatform.LEVER, source_id, source_url, application_url, self.config.canonical_company.strip(), title,
                    description, location, work_mode, _source_timestamp(item.get("createdAt")), AtsType.LEVER,
                    _rfc3339(observed_at), _provenance(company_field="site.company", title_field="text", description_field="descriptionPlain", location_field="categories.location", posted_field="createdAt"),
                )
                candidates.append(SearchCandidate(f"lever:{self.config.site_name}:{source_id}", observation.company, title, location or None, SourcePlatform.LEVER, source_url, source_id, observation))
            return JobSearchResult.succeeded(_candidate_set(version=LEVER_JOB_SEARCH_ADAPTER_VERSION, source_id=self.config.site_name, request=request, candidates=candidates, created_at=observed_at, limit=self.policy.max_results_per_query))
        except (TypeError, ValueError, OverflowError, OSError):
            return JobSearchResult.failed(JobSearchReason.CANDIDATE_VALIDATION_FAILED)


class GlassdoorPartnerJobSearch:
    """Legacy/partner Jobs API binding; enabled only with explicit credentials."""
    def __init__(self, *, config: GlassdoorPartnerConfig, http_port: BoundedJobSearchHttpPort, policy: JobSearchExecutionPolicy, clock=_utc_now) -> None:
        self.config, self.http, self.policy, self.clock = config, http_port, policy, clock
        if "GLASSDOOR" not in policy.allowed_providers: raise ValueError("Glassdoor is disabled by policy")

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        query = {"v": "1.1", "format": "json", "action": "jobs", "q": f"{request.company} {request.title}", "pn": "1", "ps": str(self.policy.max_results_per_query), "userip": self.config.user_ip, "useragent": self.policy.user_agent_version}
        if request.location is not None: query["l"] = request.location
        result = await self.http.get(BoundedHttpRequest(
            url="https://api.glassdoor.com/api/api.htm", allowed_hosts=("api.glassdoor.com",),
            headers={"Accept": "application/json", "User-Agent": self.policy.user_agent_version},
            connect_timeout_seconds=self.policy.connect_timeout_seconds, read_timeout_seconds=self.policy.read_timeout_seconds,
            max_redirects=self.policy.max_redirects, max_response_bytes=self.policy.max_response_bytes,
            query=query, secret_query={"t.p": self.config.partner_id, "t.k": self.config.partner_key},
        ))
        failure = _http_failure(result)
        if failure: return failure
        try:
            payload = _json(result); response = payload.get("response") if isinstance(payload, Mapping) else None
            jobs = response.get("jobListings") if isinstance(response, Mapping) else None
            if not isinstance(jobs, list): raise ValueError("Glassdoor response has no job listings")
            observed_at = self.clock(); candidates: list[SearchCandidate] = []; seen: set[str] = set()
            requested_company = canonicalize_search_company(request.company)
            for item in jobs:
                if not isinstance(item, Mapping): raise ValueError("Glassdoor job must be an object")
                employer = item.get("employer") or {}; employer_name = employer.get("name") if isinstance(employer, Mapping) else item.get("employerName")
                company = _text(employer_name, name="employer", maximum=240)
                if canonicalize_search_company(company) != requested_company: continue
                source_id = _text(item.get("jobListingId"), name="jobListingId", maximum=240)
                title = _text(item.get("jobTitle"), name="jobTitle", maximum=240)
                location = _text(item.get("location", ""), name="location", maximum=320, empty=True)
                if not _matches(request, title=title, location=location): continue
                if source_id in seen: continue
                seen.add(source_id)
                source_url = _url(item.get("jobViewUrl")); application_url = _url(item.get("applyUrl", source_url))
                description = _text(item.get("jobDescription", item.get("description")), name="jobDescription", maximum=100_000)
                observation = SourceJobObservation(
                    SourcePlatform.GLASSDOOR, source_id, source_url, application_url, company, title, description, location,
                    WorkMode.UNKNOWN, _source_timestamp(item.get("postedAt")), AtsType.UNKNOWN,
                    _rfc3339(observed_at), _provenance(company_field="employer.name", title_field="jobTitle", description_field="jobDescription", location_field="location", posted_field="postedAt"),
                )
                candidates.append(SearchCandidate(f"glassdoor:{source_id}", company, title, location or None, SourcePlatform.GLASSDOOR, source_url, source_id, observation))
            return JobSearchResult.succeeded(_candidate_set(version=GLASSDOOR_JOB_SEARCH_ADAPTER_VERSION, source_id=self.config.source_id, request=request, candidates=candidates, created_at=observed_at, limit=self.policy.max_results_per_query))
        except (TypeError, ValueError, OverflowError, OSError):
            return JobSearchResult.failed(JobSearchReason.CANDIDATE_VALIDATION_FAILED)


def _first(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in item: return item[name]
    return None


class JobviteFeedJobSearch:
    def __init__(self, *, config: JobviteFeedConfig, http_port: BoundedJobSearchHttpPort, policy: JobSearchExecutionPolicy, clock=_utc_now) -> None:
        self.config, self.http, self.policy, self.clock = config, http_port, policy, clock
        if "JOBVITE" not in policy.allowed_providers: raise ValueError("Jobvite is disabled by policy")

    async def search(self, request: JobSearchRequest) -> JobSearchResult:
        if not _supports(request, self.config.canonical_company, self.config.aliases): return JobSearchResult.unsupported()
        result = await self.http.get(BoundedHttpRequest(
            url="https://api.jobvite.com/api/v2/job", allowed_hosts=("api.jobvite.com",),
            headers={"Accept": "application/json", "User-Agent": self.policy.user_agent_version},
            connect_timeout_seconds=self.policy.connect_timeout_seconds, read_timeout_seconds=self.policy.read_timeout_seconds,
            max_redirects=self.policy.max_redirects, max_response_bytes=self.policy.max_response_bytes,
            query={"jobStatus": "Open", "availableTo": "External"}, secret_query={"api": self.config.api_key, "sc": self.config.api_secret},
        ))
        failure = _http_failure(result)
        if failure: return failure
        try:
            payload = _json(result)
            jobs = payload if isinstance(payload, list) else _first(payload, "requisitions", "jobs") if isinstance(payload, Mapping) else None
            if not isinstance(jobs, list): raise ValueError("Jobvite response has no jobs")
            observed_at = self.clock(); candidates: list[SearchCandidate] = []; seen: set[str] = set()
            for item in jobs:
                if not isinstance(item, Mapping): raise ValueError("Jobvite job must be an object")
                if _first(item, "jobState", "jobStatus") not in {None, "Open"} or _first(item, "postingType", "availableTo") not in {None, "External"} or item.get("distribution") is not True: continue
                source_id = _text(
                    _first(item, "eId", "id", "jobId", "requisitionId"),
                    name="jobId",
                    maximum=240,
                )
                title = _text(_first(item, "title", "jobTitle"), name="jobTitle", maximum=240)
                raw_location = _first(item, "location", "jobLocation") or ""
                if isinstance(raw_location, Mapping): raw_location = _first(raw_location, "name", "displayName") or ""
                location = _text(raw_location, name="location", maximum=320, empty=True)
                if not _matches(request, title=title, location=location): continue
                if source_id in seen: continue
                seen.add(source_id)
                base = f"https://jobs.jobvite.com/{quote(self.config.career_site, safe='')}/job/{quote(source_id, safe='')}"
                source_url = _url(base, hosts=("jobs.jobvite.com",))
                application_url = _url(
                    f"{base}/apply",
                    hosts=("jobs.jobvite.com",),
                )
                description = _text(_first(item, "description", "jobDescription"), name="jobDescription", maximum=100_000)
                observation = SourceJobObservation(
                    SourcePlatform.JOBVITE, source_id, source_url, application_url, self.config.canonical_company.strip(), title,
                    description, location, WorkMode.UNKNOWN, _source_timestamp(_first(item, "sentDate", "postedAt", "publishDate", "datePosted")), AtsType.JOBVITE,
                    _rfc3339(observed_at), _provenance(company_field="feed.company", title_field="jobTitle", description_field="jobDescription", location_field="location", posted_field="sentDate"),
                )
                candidates.append(SearchCandidate(f"jobvite:{self.config.career_site}:{source_id}", observation.company, title, location or None, SourcePlatform.JOBVITE, source_url, source_id, observation))
            return JobSearchResult.succeeded(_candidate_set(version=JOBVITE_JOB_SEARCH_ADAPTER_VERSION, source_id=self.config.career_site, request=request, candidates=candidates, created_at=observed_at, limit=self.policy.max_results_per_query))
        except (TypeError, ValueError, OverflowError, OSError):
            return JobSearchResult.failed(JobSearchReason.CANDIDATE_VALIDATION_FAILED)


__all__ = [
    "ASHBY_JOB_SEARCH_ADAPTER_VERSION", "AshbyBoardConfig", "AshbyBoardJobSearch",
    "GLASSDOOR_JOB_SEARCH_ADAPTER_VERSION", "GlassdoorPartnerConfig", "GlassdoorPartnerJobSearch",
    "JOBVITE_JOB_SEARCH_ADAPTER_VERSION", "JobviteFeedConfig", "JobviteFeedJobSearch",
    "LEVER_JOB_SEARCH_ADAPTER_VERSION", "LeverPostingsJobSearch", "LeverSiteConfig",
]
