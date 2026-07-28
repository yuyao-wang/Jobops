"""Read one hosted Lever public job through the public Postings API."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .contract import (
    AtsType,
    FieldProvenance,
    ProvenanceSource,
    ReadJobReason,
    ReadJobRequest,
    ReadJobResult,
    SourceJobObservation,
    SourcePlatform,
    WorkMode,
)


_PATH_PATTERN = re.compile(
    r"/(?P<company>[A-Za-z0-9_-]{1,128})/"
    r"(?P<job_id>[A-Za-z0-9_-]{1,128})(?:/apply)?/?"
)
_MAX_RESPONSE_BYTES = 2_000_000


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _absolute_http_url(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("source URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source URL is invalid") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        raise ValueError("source URL is invalid")
    return value


def _parse_public_job_url(
    url: str,
) -> tuple[str, str, str, str] | ReadJobResult:
    raw = url.strip()
    if not raw or len(raw) > 2048:
        return ReadJobResult.failed(ReadJobReason.INVALID_URL)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ReadJobResult.failed(ReadJobReason.INVALID_URL)
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ReadJobResult.failed(ReadJobReason.INVALID_URL)
    if port is not None or parsed.hostname.casefold() != "jobs.lever.co":
        return ReadJobResult.failed(ReadJobReason.UNSUPPORTED_URL)
    path_match = _PATH_PATTERN.fullmatch(parsed.path)
    if path_match is None:
        return ReadJobResult.failed(ReadJobReason.UNSUPPORTED_URL)

    company = path_match.group("company")
    job_id = path_match.group("job_id")
    canonical_source_url = (
        "https://jobs.lever.co/"
        f"{quote(company, safe='')}/{quote(job_id, safe='')}"
    )
    api_url = (
        "https://api.lever.co/v0/postings/"
        f"{quote(company, safe='')}/{quote(job_id, safe='')}"
    )
    return canonical_source_url, company, job_id, api_url


def _required_text(payload: Mapping[str, Any], field: str, maximum: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} is outside the source contract")
    return normalized


def _posted_at(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("createdAt must be milliseconds since epoch")
    if value < 0:
        raise ValueError("createdAt must be non-negative")
    try:
        parsed = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("createdAt is invalid") from exc
    return _rfc3339(parsed)


def _work_mode(value: Any) -> tuple[WorkMode, ProvenanceSource, str]:
    if value is None or value == "":
        return WorkMode.UNKNOWN, ProvenanceSource.SYSTEM, "UNKNOWN"
    if not isinstance(value, str):
        raise ValueError("workplaceType must be a string")
    mapped = {
        "onsite": WorkMode.ONSITE,
        "hybrid": WorkMode.HYBRID,
        "remote": WorkMode.REMOTE,
    }.get(value.strip().casefold(), WorkMode.UNKNOWN)
    return mapped, ProvenanceSource.SOURCE_API, "workplaceType"


def _observation_from_payload(
    *,
    payload: Any,
    canonical_source_url: str,
    company: str,
    expected_job_id: str,
    observed_at: datetime,
) -> SourceJobObservation:
    if not isinstance(payload, Mapping):
        raise ValueError("Lever response must be an object")

    raw_id = payload.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raise ValueError("Lever response has no job id")
    source_job_id = raw_id.strip()
    if source_job_id != expected_job_id:
        raise ValueError("Lever response job id does not match the request")

    title = _required_text(payload, "text", 240)
    description = _required_text(payload, "descriptionPlain", 100_000)

    raw_categories = payload.get("categories")
    if raw_categories is None:
        location = ""
    elif isinstance(raw_categories, Mapping):
        raw_location = raw_categories.get("location")
        if raw_location is None:
            location = ""
        elif isinstance(raw_location, str):
            location = " ".join(raw_location.split())
        else:
            raise ValueError("categories.location must be a string")
    else:
        raise ValueError("categories must be an object")
    if len(location) > 320:
        raise ValueError("location is outside the source contract")

    raw_hosted_url = payload.get("hostedUrl")
    source_url_source = ProvenanceSource.REQUEST
    source_url_field = "url"
    if raw_hosted_url not in {None, ""}:
        hosted_url = _absolute_http_url(raw_hosted_url)
        if hosted_url is None:
            raise ValueError("hostedUrl is invalid")
        hosted = _parse_public_job_url(hosted_url)
        if (
            isinstance(hosted, ReadJobResult)
            or hosted[1] != company
            or hosted[2] != expected_job_id
        ):
            raise ValueError("hostedUrl does not match the request")
        canonical_source_url = hosted[0]
        source_url_source = ProvenanceSource.SOURCE_API
        source_url_field = "hostedUrl"

    application_url = _absolute_http_url(payload.get("applyUrl"))
    work_mode, work_mode_source, work_mode_field = _work_mode(
        payload.get("workplaceType")
    )
    posted_at = _posted_at(payload.get("createdAt"))

    provenance = (
        FieldProvenance("source_platform", ProvenanceSource.SYSTEM, "reader"),
        FieldProvenance("source_job_id", ProvenanceSource.SOURCE_API, "id"),
        FieldProvenance("source_url", source_url_source, source_url_field),
        FieldProvenance(
            "application_url", ProvenanceSource.SOURCE_API, "applyUrl"
        ),
        FieldProvenance("company", ProvenanceSource.REQUEST, "url.company"),
        FieldProvenance("title", ProvenanceSource.SOURCE_API, "text"),
        FieldProvenance(
            "description", ProvenanceSource.SOURCE_API, "descriptionPlain"
        ),
        FieldProvenance(
            "location", ProvenanceSource.SOURCE_API, "categories.location"
        ),
        FieldProvenance("work_mode", work_mode_source, work_mode_field),
        FieldProvenance("posted_at", ProvenanceSource.SOURCE_API, "createdAt"),
        FieldProvenance("ats_type", ProvenanceSource.SYSTEM, "reader"),
        FieldProvenance("observed_at", ProvenanceSource.SYSTEM, "clock"),
    )
    return SourceJobObservation(
        source_platform=SourcePlatform.LEVER,
        source_job_id=source_job_id,
        source_url=canonical_source_url,
        application_url=application_url,
        company=company,
        title=title,
        description=description,
        location=location,
        work_mode=work_mode,
        posted_at=posted_at,
        ats_type=AtsType.LEVER,
        observed_at=_rfc3339(observed_at),
        provenance=provenance,
    )


class LeverPublicJobReader:
    """Narrow, read-only reader for hosted Lever public job URLs."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock or _utc_now

    async def read_job(self, request: ReadJobRequest) -> ReadJobResult:
        if not isinstance(request, ReadJobRequest):
            raise TypeError("request must be a ReadJobRequest")
        parsed = _parse_public_job_url(request.url)
        if isinstance(parsed, ReadJobResult):
            return parsed
        canonical_source_url, company, expected_job_id, api_url = parsed

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Jobops/1.0 LeverPublicJobReader",
                },
            ) as client:
                response = await client.get(api_url)
        except httpx.TimeoutException:
            return ReadJobResult.failed(ReadJobReason.SOURCE_TIMEOUT)
        except httpx.HTTPError:
            return ReadJobResult.failed(ReadJobReason.SOURCE_UNAVAILABLE)

        if response.status_code == 404:
            return ReadJobResult.failed(ReadJobReason.JOB_NOT_FOUND)
        if response.status_code == 410:
            return ReadJobResult.failed(ReadJobReason.JOB_CLOSED)
        if response.status_code in {408, 504}:
            return ReadJobResult.failed(ReadJobReason.SOURCE_TIMEOUT)
        if response.status_code == 429:
            return ReadJobResult.failed(ReadJobReason.SOURCE_RATE_LIMITED)
        if response.status_code >= 500:
            return ReadJobResult.failed(ReadJobReason.SOURCE_UNAVAILABLE)
        if response.status_code != 200:
            return ReadJobResult.failed(
                ReadJobReason.SOURCE_UNAVAILABLE,
                retryable=False,
            )
        if len(response.content) > _MAX_RESPONSE_BYTES:
            return ReadJobResult.failed(ReadJobReason.SOURCE_RESPONSE_INVALID)

        try:
            payload = response.json()
            observation = _observation_from_payload(
                payload=payload,
                canonical_source_url=canonical_source_url,
                company=company,
                expected_job_id=expected_job_id,
                observed_at=self._clock(),
            )
        except (TypeError, ValueError):
            return ReadJobResult.failed(ReadJobReason.SOURCE_RESPONSE_INVALID)
        return ReadJobResult.succeeded(observation)


__all__ = ["LeverPublicJobReader"]
