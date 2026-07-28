"""Read one public Schema.org JobPosting from bounded JSON-LD HTML."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

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
from .greenhouse import _plain_text


_MAX_RESPONSE_BYTES = 2_000_000
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_BLOCKED_HOSTNAMES = frozenset(
    {
        "instance-data",
        "metadata.google.internal",
        "metadata.azure.internal",
    }
)


class _JsonLdScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() != "script" or self._parts is not None:
            return
        attributes = {
            name.casefold(): value
            for name, value in attrs
            if isinstance(name, str)
        }
        script_type = attributes.get("type")
        if (
            isinstance(script_type, str)
            and script_type.strip().casefold() == "application/ld+json"
        ):
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._parts is not None:
            self.documents.append("".join(self._parts))
            self._parts = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_resolver(hostname: str) -> tuple[str, ...]:
    records = socket.getaddrinfo(
        hostname,
        None,
        family=socket.AF_UNSPEC,
        type=socket.SOCK_STREAM,
    )
    return tuple(record[4][0] for record in records)


def _normalized_http_url(value: str) -> str | ReadJobResult:
    raw = value.strip()
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
        or port not in {None, 80, 443}
    ):
        return ReadJobResult.failed(ReadJobReason.INVALID_URL)
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def _is_blocked_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    return (
        not address.is_global
        or address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _literal_ip(hostname: str) -> str | None:
    candidate = hostname
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _hostname_is_blocked(hostname: str) -> bool:
    normalized = hostname.casefold().rstrip(".")
    return (
        normalized == "localhost"
        or normalized.endswith(".localhost")
        or normalized in _BLOCKED_HOSTNAMES
    )


def _resolved_addresses_are_public(addresses: Iterable[str]) -> bool:
    values = tuple(addresses)
    return bool(values) and all(not _is_blocked_ip(value) for value in values)


def _job_type_contains_job_posting(value: Any) -> bool:
    if isinstance(value, str):
        return value == "JobPosting"
    if isinstance(value, list):
        return any(item == "JobPosting" for item in value)
    return False


def _objects_from_document(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        roots = value
    else:
        roots = [value]
    objects: list[Mapping[str, Any]] = []
    for root in roots:
        if not isinstance(root, Mapping):
            continue
        objects.append(root)
        graph = root.get("@graph")
        if isinstance(graph, list):
            objects.extend(item for item in graph if isinstance(item, Mapping))
        elif isinstance(graph, Mapping):
            objects.append(graph)
    return objects


def _extract_job_postings(html_text: str) -> list[Mapping[str, Any]]:
    parser = _JsonLdScriptParser()
    parser.feed(html_text)
    parser.close()
    postings: list[Mapping[str, Any]] = []
    for document in parser.documents:
        try:
            decoded = json.loads(document)
        except (TypeError, ValueError) as exc:
            raise ValueError("JSON-LD is malformed") from exc
        postings.extend(
            item
            for item in _objects_from_document(decoded)
            if _job_type_contains_job_posting(item.get("@type"))
        )
    return postings


def _required_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} is outside the source contract")
    return normalized


def _application_url(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("url must be a string")
    parsed = _normalized_http_url(value)
    if isinstance(parsed, ReadJobResult):
        raise ValueError("url must be an absolute HTTP(S) URL")
    return parsed


def _posted_at(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _rfc3339(parsed)


def _identifier(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > 240:
        return None
    return normalized


def _address_country(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Mapping):
        name = value.get("name")
        if isinstance(name, str):
            return " ".join(name.split())
    return ""


def _location_text(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        parts = [_location_text(item) for item in value]
        return " | ".join(dict.fromkeys(part for part in parts if part))
    if not isinstance(value, Mapping):
        return ""
    name = value.get("name")
    if isinstance(name, str) and name.strip():
        return " ".join(name.split())
    address = value.get("address")
    if not isinstance(address, Mapping):
        return ""
    parts: list[str] = []
    for field in ("addressLocality", "addressRegion"):
        part = address.get(field)
        if isinstance(part, str) and part.strip():
            parts.append(" ".join(part.split()))
    country = _address_country(address.get("addressCountry"))
    if country:
        parts.append(country)
    return ", ".join(dict.fromkeys(parts))


def _location(posting: Mapping[str, Any]) -> tuple[str, str]:
    location = _location_text(posting.get("jobLocation"))
    if location:
        return location, "jobLocation"
    location = _location_text(posting.get("applicantLocationRequirements"))
    if location:
        return location, "applicantLocationRequirements"
    return "", "jobLocation"


def _work_mode(value: Any) -> tuple[WorkMode, ProvenanceSource, str]:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, str) and item.strip().casefold() == "telecommute":
            return (
                WorkMode.REMOTE,
                ProvenanceSource.SOURCE_API,
                "jobLocationType",
            )
    return WorkMode.UNKNOWN, ProvenanceSource.SYSTEM, "UNKNOWN"


def _observation_from_posting(
    *,
    posting: Mapping[str, Any],
    source_url: str,
    observed_at: datetime,
) -> SourceJobObservation:
    title = _required_text(posting.get("title"), "title", 240)
    raw_description = posting.get("description")
    if not isinstance(raw_description, str):
        raise ValueError("description must be a string")
    description = _plain_text(raw_description)
    if not description or len(description) > 100_000:
        raise ValueError("description is outside the source contract")
    organization = posting.get("hiringOrganization")
    if not isinstance(organization, Mapping):
        raise ValueError("hiringOrganization must be an object")
    company = _required_text(
        organization.get("name"),
        "hiringOrganization.name",
        240,
    )

    location, location_field = _location(posting)
    if len(location) > 320:
        raise ValueError("location is outside the source contract")
    work_mode, work_mode_source, work_mode_field = _work_mode(
        posting.get("jobLocationType")
    )
    application_url = _application_url(posting.get("url"))
    posted_at = _posted_at(posting.get("datePosted"))
    source_job_id = _identifier(posting.get("identifier"))

    provenance = (
        FieldProvenance("source_platform", ProvenanceSource.SYSTEM, "reader"),
        FieldProvenance(
            "source_job_id", ProvenanceSource.SOURCE_API, "identifier"
        ),
        FieldProvenance("source_url", ProvenanceSource.SYSTEM, "final_url"),
        FieldProvenance("application_url", ProvenanceSource.SOURCE_API, "url"),
        FieldProvenance(
            "company",
            ProvenanceSource.SOURCE_API,
            "hiringOrganization.name",
        ),
        FieldProvenance("title", ProvenanceSource.SOURCE_API, "title"),
        FieldProvenance("description", ProvenanceSource.SOURCE_API, "description"),
        FieldProvenance("location", ProvenanceSource.SOURCE_API, location_field),
        FieldProvenance("work_mode", work_mode_source, work_mode_field),
        FieldProvenance("posted_at", ProvenanceSource.SOURCE_API, "datePosted"),
        FieldProvenance("ats_type", ProvenanceSource.SYSTEM, "UNKNOWN"),
        FieldProvenance("observed_at", ProvenanceSource.SYSTEM, "clock"),
    )
    return SourceJobObservation(
        source_platform=SourcePlatform.GENERIC_WEB,
        source_job_id=source_job_id,
        source_url=source_url,
        application_url=application_url,
        company=company,
        title=title,
        description=description,
        location=location,
        work_mode=work_mode,
        posted_at=posted_at,
        ats_type=AtsType.UNKNOWN,
        observed_at=_rfc3339(observed_at),
        provenance=provenance,
    )


class GenericJsonLdJobReader:
    """Bounded reader for one public HTML page with one JSON-LD JobPosting."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        resolver: Callable[[str], Iterable[str]] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock or _utc_now
        self._resolver = resolver or _default_resolver

    async def _validate_public_url(self, value: str) -> str | ReadJobResult:
        normalized = _normalized_http_url(value)
        if isinstance(normalized, ReadJobResult):
            return normalized
        hostname = urlsplit(normalized).hostname
        if hostname is None:
            return ReadJobResult.failed(ReadJobReason.INVALID_URL)
        if _hostname_is_blocked(hostname):
            return ReadJobResult.failed(ReadJobReason.UNSAFE_URL)
        literal = _literal_ip(hostname)
        if literal is not None:
            if _is_blocked_ip(literal):
                return ReadJobResult.failed(ReadJobReason.UNSAFE_URL)
            return normalized
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(self._resolver, hostname),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return ReadJobResult.failed(ReadJobReason.SOURCE_TIMEOUT)
        except (OSError, socket.gaierror):
            return ReadJobResult.failed(ReadJobReason.SOURCE_UNAVAILABLE)
        try:
            is_public = _resolved_addresses_are_public(addresses)
        except (TypeError, ValueError):
            return ReadJobResult.failed(ReadJobReason.SOURCE_UNAVAILABLE)
        if not is_public:
            return ReadJobResult.failed(ReadJobReason.UNSAFE_URL)
        return normalized

    async def _fetch_html(
        self, initial_url: str
    ) -> tuple[str, str] | ReadJobResult:
        current: str | ReadJobResult = initial_url
        redirects = 0
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "text/html, application/xhtml+xml",
                "User-Agent": "Jobops/1.0 GenericJsonLdJobReader",
            },
        ) as client:
            while True:
                current = await self._validate_public_url(current)
                if isinstance(current, ReadJobResult):
                    return current
                client.cookies.clear()
                try:
                    async with client.stream("GET", current) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location or redirects >= _MAX_REDIRECTS:
                                return ReadJobResult.failed(
                                    ReadJobReason.SOURCE_RESPONSE_INVALID
                                )
                            redirect_url = urljoin(current, location)
                            validated = await self._validate_public_url(
                                redirect_url
                            )
                            if isinstance(validated, ReadJobResult):
                                if (
                                    validated.reason_code
                                    is ReadJobReason.INVALID_URL
                                ):
                                    return ReadJobResult.failed(
                                        ReadJobReason.SOURCE_RESPONSE_INVALID
                                    )
                                return validated
                            current = validated
                            redirects += 1
                            continue
                        if response.status_code == 404:
                            return ReadJobResult.failed(
                                ReadJobReason.JOB_NOT_FOUND
                            )
                        if response.status_code == 410:
                            return ReadJobResult.failed(ReadJobReason.JOB_CLOSED)
                        if response.status_code in {408, 504}:
                            return ReadJobResult.failed(
                                ReadJobReason.SOURCE_TIMEOUT
                            )
                        if response.status_code == 429:
                            return ReadJobResult.failed(
                                ReadJobReason.SOURCE_RATE_LIMITED
                            )
                        if response.status_code >= 500:
                            return ReadJobResult.failed(
                                ReadJobReason.SOURCE_UNAVAILABLE
                            )
                        if response.status_code != 200:
                            return ReadJobResult.failed(
                                ReadJobReason.SOURCE_UNAVAILABLE,
                                retryable=False,
                            )
                        content_type = (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .casefold()
                        )
                        if content_type not in _HTML_CONTENT_TYPES:
                            return ReadJobResult.failed(
                                ReadJobReason.UNSUPPORTED_URL
                            )
                        raw_length = response.headers.get("content-length")
                        if raw_length is not None:
                            try:
                                content_length = int(raw_length)
                                if (
                                    content_length < 0
                                    or content_length > _MAX_RESPONSE_BYTES
                                ):
                                    return ReadJobResult.failed(
                                        ReadJobReason.SOURCE_RESPONSE_INVALID
                                    )
                            except ValueError:
                                return ReadJobResult.failed(
                                    ReadJobReason.SOURCE_RESPONSE_INVALID
                                )
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > _MAX_RESPONSE_BYTES:
                                return ReadJobResult.failed(
                                    ReadJobReason.SOURCE_RESPONSE_INVALID
                                )
                        try:
                            encoding = response.encoding or "utf-8"
                            decoded = bytes(content).decode(
                                encoding,
                                errors="replace",
                            )
                        except LookupError:
                            return ReadJobResult.failed(
                                ReadJobReason.SOURCE_RESPONSE_INVALID
                            )
                        return current, decoded
                except httpx.TimeoutException:
                    return ReadJobResult.failed(ReadJobReason.SOURCE_TIMEOUT)
                except httpx.HTTPError:
                    return ReadJobResult.failed(
                        ReadJobReason.SOURCE_UNAVAILABLE
                    )

    async def read_job(self, request: ReadJobRequest) -> ReadJobResult:
        if not isinstance(request, ReadJobRequest):
            raise TypeError("request must be a ReadJobRequest")
        fetched = await self._fetch_html(request.url)
        if isinstance(fetched, ReadJobResult):
            return fetched
        source_url, html_text = fetched
        try:
            postings = _extract_job_postings(html_text)
        except ValueError:
            return ReadJobResult.failed(ReadJobReason.SOURCE_RESPONSE_INVALID)
        if not postings:
            return ReadJobResult.failed(ReadJobReason.UNSUPPORTED_URL)
        if len(postings) != 1:
            return ReadJobResult.failed(ReadJobReason.SOURCE_RESPONSE_INVALID)
        try:
            observation = _observation_from_posting(
                posting=postings[0],
                source_url=source_url,
                observed_at=self._clock(),
            )
        except (TypeError, ValueError):
            return ReadJobResult.failed(ReadJobReason.SOURCE_RESPONSE_INVALID)
        return ReadJobResult.succeeded(observation)


__all__ = ["GenericJsonLdJobReader"]
