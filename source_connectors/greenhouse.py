"""Read one hosted Greenhouse public job through the public Job Board API."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from html.parser import HTMLParser
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


_HOST_PATTERN = re.compile(
    r"(?P<prefix>boards|job-boards)(?P<eu>\.eu)?\.greenhouse\.io",
    re.IGNORECASE,
)
_PATH_PATTERN = re.compile(
    r"/(?P<board>[A-Za-z0-9_-]{1,128})/jobs/(?P<job_id>[0-9]{1,32})/?"
)
_MAX_RESPONSE_BYTES = 2_000_000


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"br", "div", "h1", "h2", "h3", "h4", "li", "p", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"div", "h1", "h2", "h3", "h4", "li", "p", "tr"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_source_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("source timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("source timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("source timestamp must include a timezone")
    return _rfc3339(parsed)


def _plain_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("job content must be a string")
    decoded = html.unescape(html.unescape(value))
    parser = _HTMLTextExtractor()
    parser.feed(decoded)
    parser.close()
    return " ".join("".join(parser.parts).split())


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


def _parse_public_job_url(url: str) -> tuple[str, str, str] | ReadJobResult:
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
    if port is not None:
        return ReadJobResult.failed(ReadJobReason.UNSUPPORTED_URL)
    host_match = _HOST_PATTERN.fullmatch(parsed.hostname)
    path_match = _PATH_PATTERN.fullmatch(parsed.path)
    if host_match is None or path_match is None:
        return ReadJobResult.failed(ReadJobReason.UNSUPPORTED_URL)
    api_host = (
        "boards-api.eu.greenhouse.io"
        if host_match.group("eu")
        else "boards-api.greenhouse.io"
    )
    board = path_match.group("board")
    job_id = path_match.group("job_id")
    api_url = (
        f"https://{api_host}/v1/boards/{quote(board, safe='')}/"
        f"jobs/{quote(job_id, safe='')}"
    )
    return raw, job_id, api_url


def _required_text(payload: Mapping[str, Any], field: str, maximum: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} is outside the source contract")
    return normalized


def greenhouse_observation_from_payload(
    *,
    payload: Any,
    source_url: str,
    expected_job_id: str,
    observed_at: datetime,
    canonical_company: str | None = None,
) -> SourceJobObservation:
    if not isinstance(payload, Mapping):
        raise ValueError("Greenhouse response must be an object")
    raw_id = payload.get("id")
    if (
        isinstance(raw_id, bool)
        or not isinstance(raw_id, (int, str))
        or not str(raw_id).strip()
    ):
        raise ValueError("Greenhouse response has no job id")
    source_job_id = str(raw_id).strip()
    if source_job_id != expected_job_id:
        raise ValueError("Greenhouse response job id does not match the request")

    title = _required_text(payload, "title", 240)
    company = (
        _required_text(payload, "company_name", 240)
        if canonical_company is None
        else " ".join(canonical_company.split())
    )
    if not company or len(company) > 240:
        raise ValueError("company is outside the source contract")
    description = _plain_text(payload.get("content"))
    if not description or len(description) > 100_000:
        raise ValueError("content is outside the source contract")

    raw_location = payload.get("location")
    if raw_location is None:
        location = ""
    elif isinstance(raw_location, Mapping):
        raw_name = raw_location.get("name")
        if raw_name is None:
            location = ""
        elif isinstance(raw_name, str):
            location = " ".join(raw_name.split())
        else:
            raise ValueError("location name must be a string")
    else:
        raise ValueError("location must be an object")
    if len(location) > 320:
        raise ValueError("location is outside the source contract")

    application_url = _absolute_http_url(payload.get("absolute_url"))
    posted_at = _normalize_source_timestamp(payload.get("first_published"))
    provenance = (
        FieldProvenance("source_platform", ProvenanceSource.SYSTEM, "reader"),
        FieldProvenance("source_job_id", ProvenanceSource.SOURCE_API, "id"),
        FieldProvenance("source_url", ProvenanceSource.REQUEST, "url"),
        FieldProvenance(
            "application_url", ProvenanceSource.SOURCE_API, "absolute_url"
        ),
        FieldProvenance(
            "company",
            (
                ProvenanceSource.SOURCE_API
                if canonical_company is None
                else ProvenanceSource.REQUEST
            ),
            "company_name" if canonical_company is None else "board.company",
        ),
        FieldProvenance("title", ProvenanceSource.SOURCE_API, "title"),
        FieldProvenance("description", ProvenanceSource.SOURCE_API, "content"),
        FieldProvenance("location", ProvenanceSource.SOURCE_API, "location.name"),
        FieldProvenance("work_mode", ProvenanceSource.SYSTEM, "UNKNOWN"),
        FieldProvenance(
            "posted_at", ProvenanceSource.SOURCE_API, "first_published"
        ),
        FieldProvenance("ats_type", ProvenanceSource.SYSTEM, "reader"),
        FieldProvenance("observed_at", ProvenanceSource.SYSTEM, "clock"),
    )
    return SourceJobObservation(
        source_platform=SourcePlatform.GREENHOUSE,
        source_job_id=source_job_id,
        source_url=source_url,
        application_url=application_url,
        company=company,
        title=title,
        description=description,
        location=location,
        work_mode=WorkMode.UNKNOWN,
        posted_at=posted_at,
        ats_type=AtsType.GREENHOUSE,
        observed_at=_rfc3339(observed_at),
        provenance=provenance,
    )


class GreenhousePublicJobReader:
    """Narrow, read-only reader for allowlisted Greenhouse hosted job URLs."""

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
        source_url, expected_job_id, api_url = parsed

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Jobops/1.0 GreenhousePublicJobReader",
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
            observation = greenhouse_observation_from_payload(
                payload=payload,
                source_url=source_url,
                expected_job_id=expected_job_id,
                observed_at=self._clock(),
            )
        except (TypeError, ValueError):
            return ReadJobResult.failed(ReadJobReason.SOURCE_RESPONSE_INVALID)
        return ReadJobResult.succeeded(observation)


__all__ = [
    "GreenhousePublicJobReader",
    "greenhouse_observation_from_payload",
]
