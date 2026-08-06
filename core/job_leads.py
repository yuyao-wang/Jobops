"""Durable, subject-scoped evidence for pre-normalized job leads.

``JobLead`` is the explicit boundary between source search candidates and a
verified normalized job.  A lead may contain incomplete or stale hints; it is
never an authoritative job fact and cannot enter application preparation until
another boundary resolves it to a verified canonical posting.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .private_home import PrivateHome, PrivateHomeError


JOB_LEAD_CONTRACT_VERSION = "job-lead-v1"
_LEAD_ID_RE = re.compile(r"job-lead-[0-9a-f]{64}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_QUERY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_LINKEDIN_JOB_PATH_RE = re.compile(
    r"^/jobs/view/(?:[A-Za-z0-9._~-]+-)?([0-9]{5,24})/?$",
    re.ASCII | re.IGNORECASE,
)
_INDEED_JOB_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$", re.ASCII)
_SAFE_JOB_QUERY_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$", re.ASCII)
_SAFE_JOB_QUERY_KEYS = frozenset(
    {
        "gh_jid",
        "id",
        "jid",
        "jl",
        "job",
        "job_id",
        "job_key",
        "jobid",
        "jobkey",
        "jobreqid",
        "position_id",
        "positionid",
        "posting_id",
        "postingid",
        "req_id",
        "reqid",
        "requisition_id",
        "requisitionid",
        "rid",
        "vacancy_id",
        "vacancyid",
    }
)
_SUCCESSFACTORS_JOB_QUERY_KEYS = (
    "company",
    "career_job_req_id",
    "career_ns",
)


def _clean_required(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the JobLead contract")
    return cleaned


def _clean_optional(name: str, value: Any, maximum: int) -> str | None:
    if value is None:
        return None
    return _clean_required(name, value, maximum)


def _utc(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must have a valid UTC offset")
    return value.astimezone(timezone.utc)


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("persisted timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc("persisted timestamp", parsed)


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _subject_key(subject_id: str) -> str:
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_is_or_is_below(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _safe_query_pairs(query: str) -> list[tuple[str, str]]:
    """Keep only bounded posting-identity fields, never ambient URL state."""

    try:
        pairs = parse_qsl(
            query,
            keep_blank_values=True,
            max_num_fields=50,
        )
    except ValueError as exc:
        raise ValueError("JobLead URL query is outside the safe bound") from exc
    safe = [
        (key, item)
        for key, item in pairs
        if key.casefold() in _SAFE_JOB_QUERY_KEYS
        and _SAFE_JOB_QUERY_VALUE_RE.fullmatch(item) is not None
    ]
    return sorted(safe, key=lambda pair: (pair[0].casefold(), pair[0], pair[1]))


def _successfactors_query_pairs(query: str) -> list[tuple[str, str]]:
    """Retain the bounded tenant + requisition identity used by SF career URLs."""

    pairs = parse_qsl(
        query,
        keep_blank_values=True,
        max_num_fields=50,
    )
    values: dict[str, str] = {}
    for key, item in pairs:
        normalized_key = key.casefold()
        if (
            normalized_key not in _SUCCESSFACTORS_JOB_QUERY_KEYS
            or _SAFE_JOB_QUERY_VALUE_RE.fullmatch(item) is None
        ):
            continue
        if normalized_key == "career_ns" and item.casefold() != "job_listing":
            continue
        values.setdefault(
            normalized_key,
            "job_listing" if normalized_key == "career_ns" else item,
        )
    return [
        (key, values[key])
        for key in _SUCCESSFACTORS_JOB_QUERY_KEYS
        if key in values
    ]


def canonicalize_job_lead_url(name: str, value: Any) -> str:
    """Return a stable, secret-free HTTP(S) URL for durable Lead evidence."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if value != value.strip() or not value or len(value) > 4096:
        raise ValueError(f"{name} is outside the JobLead URL contract")
    if any(ord(character) < 0x20 for character in value) or "\\" in value:
        raise ValueError(f"{name} contains unsafe URL characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} is malformed") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{name} must use HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} cannot contain URL credentials")
    host = parsed.hostname
    if not host:
        raise ValueError(f"{name} must contain a host")
    host = host.casefold().rstrip(".")
    if not host:
        raise ValueError(f"{name} must contain a host")
    if scheme == "http" and not _is_loopback(host):
        raise ValueError(f"{name} must use HTTPS for non-loopback hosts")
    host_text = f"[{host}]" if ":" in host else host
    default_port = (scheme == "https" and port == 443) or (
        scheme == "http" and port == 80
    )
    netloc = host_text if port is None or default_port else f"{host_text}:{port}"
    path = parsed.path or "/"
    query_pairs = _safe_query_pairs(parsed.query)

    if _host_is_or_is_below(host, "linkedin.com"):
        netloc = "www.linkedin.com"
        match = _LINKEDIN_JOB_PATH_RE.fullmatch(path)
        if match is not None:
            path = f"/jobs/view/{match.group(1)}"
        query_pairs = []
    elif _host_is_or_is_below(host, "indeed.com"):
        netloc = "www.indeed.com"
        job_keys = [
            item
            for key, item in parse_qsl(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=50,
            )
            if key.casefold() == "jk"
            and _INDEED_JOB_KEY_RE.fullmatch(item) is not None
        ]
        if path.casefold().rstrip("/") == "/viewjob" and len(job_keys) == 1:
            path = "/viewjob"
            query_pairs = [("jk", job_keys[0])]
        else:
            query_pairs = []
    elif _host_is_or_is_below(host, "glassdoor.com"):
        netloc = "www.glassdoor.com"
        query_pairs = [pair for pair in query_pairs if pair[0].casefold() == "jl"]
    elif _host_is_or_is_below(host, "glassdoor.ca"):
        netloc = "www.glassdoor.ca"
        query_pairs = [pair for pair in query_pairs if pair[0].casefold() == "jl"]
    elif _host_is_or_is_below(host, "successfactors.com") or _host_is_or_is_below(
        host, "successfactors.eu"
    ):
        query_pairs = _successfactors_query_pairs(parsed.query)

    canonical = urlunsplit((scheme, netloc, path, urlencode(query_pairs), ""))
    if len(canonical) > 4096:
        raise ValueError(f"{name} is outside the JobLead URL contract")
    return canonical


class JobLeadSource(StrEnum):
    AUTHORIZED_WEB_SEARCH = "AUTHORIZED_WEB_SEARCH"
    CANONICAL_RESOLUTION = "CANONICAL_RESOLUTION"
    JOB_ALERT_INBOX = "JOB_ALERT_INBOX"
    LINKEDIN_ALERT_EMAIL = "LINKEDIN_ALERT_EMAIL"
    INDEED_ALERT_EMAIL = "INDEED_ALERT_EMAIL"
    EMPLOYER_OR_ATS_ALERT_EMAIL = "EMPLOYER_OR_ATS_ALERT_EMAIL"
    WEB_CLIPPER = "WEB_CLIPPER"
    PASTED_URL = "PASTED_URL"


class JobLeadOrigin(StrEnum):
    LINKEDIN_SEARCH_INDEX = "LINKEDIN_SEARCH_INDEX"
    INDEED_SEARCH_INDEX = "INDEED_SEARCH_INDEX"
    GLASSDOOR_SEARCH_INDEX = "GLASSDOOR_SEARCH_INDEX"
    EMPLOYER = "EMPLOYER"
    ATS = "ATS"
    UNKNOWN_WEB = "UNKNOWN_WEB"


class JobLeadStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    RESOLVED = "RESOLVED"
    NEEDS_USER = "NEEDS_USER"
    STALE = "STALE"


class JobLeadReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class JobLeadListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class JobLeadWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    CONFLICT = "CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    FAILED = "FAILED"


_ALLOWED_TRANSITIONS: Mapping[JobLeadStatus, frozenset[JobLeadStatus]] = {
    JobLeadStatus.DISCOVERED: frozenset(
        {
            JobLeadStatus.RESOLVED,
            JobLeadStatus.NEEDS_USER,
            JobLeadStatus.STALE,
        }
    ),
    JobLeadStatus.NEEDS_USER: frozenset(
        {JobLeadStatus.RESOLVED, JobLeadStatus.STALE}
    ),
    JobLeadStatus.RESOLVED: frozenset({JobLeadStatus.STALE}),
    JobLeadStatus.STALE: frozenset(),
}


def _identity_payload(
    *,
    subject_id: str,
    source: JobLeadSource,
    origin: JobLeadOrigin,
    source_url: str,
    query_id: str | None,
    source_message_digest: str | None,
) -> dict[str, Any]:
    return {
        "contract_version": JOB_LEAD_CONTRACT_VERSION,
        "origin": origin.value,
        "query_id": query_id,
        "source": source.value,
        "source_message_digest": source_message_digest,
        "source_url": source_url,
        "subject_id": subject_id,
    }


def _content_payload(
    *,
    subject_id: str,
    lead_id: str,
    lead_version: int,
    source: JobLeadSource,
    origin: JobLeadOrigin,
    status: JobLeadStatus,
    source_url: str,
    title_hint: str | None,
    company_hint: str | None,
    location_hint: str | None,
    snippet_hint: str | None,
    discovered_at: datetime,
    updated_at: datetime,
    query_id: str | None,
    confidence: float,
    source_message_digest: str | None,
    canonical_url: str | None,
    reason: str | None,
    previous_content_hash: str | None,
) -> dict[str, Any]:
    return {
        "canonical_url": canonical_url,
        "company_hint": company_hint,
        "confidence": confidence,
        "contract_version": JOB_LEAD_CONTRACT_VERSION,
        "discovered_at": _time(discovered_at),
        "lead_id": lead_id,
        "lead_version": lead_version,
        "location_hint": location_hint,
        "origin": origin.value,
        "previous_content_hash": previous_content_hash,
        "query_id": query_id,
        "reason": reason,
        "snippet_hint": snippet_hint,
        "source": source.value,
        "source_message_digest": source_message_digest,
        "source_url": source_url,
        "status": status.value,
        "subject_id": subject_id,
        "title_hint": title_hint,
        "updated_at": _time(updated_at),
    }


@dataclass(frozen=True, slots=True)
class JobLead:
    subject_id: str
    lead_id: str
    lead_version: int
    source: JobLeadSource
    origin: JobLeadOrigin
    status: JobLeadStatus
    source_url: str = field(repr=False)
    title_hint: str | None
    company_hint: str | None
    location_hint: str | None
    snippet_hint: str | None = field(repr=False)
    discovered_at: datetime
    updated_at: datetime
    query_id: str | None
    confidence: float
    source_message_digest: str | None = field(repr=False)
    canonical_url: str | None = field(repr=False)
    reason: str | None
    previous_content_hash: str | None = field(repr=False)
    content_hash: str = field(repr=False)
    contract_version: str = JOB_LEAD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        subject_id = _clean_required("subject_id", self.subject_id, 160)
        if subject_id != self.subject_id:
            raise ValueError("subject_id is not canonical")
        if _LEAD_ID_RE.fullmatch(self.lead_id) is None:
            raise ValueError("lead_id is invalid")
        if type(self.lead_version) is not int or self.lead_version < 1:
            raise ValueError("lead_version must be positive")
        object.__setattr__(self, "source", JobLeadSource(self.source))
        object.__setattr__(self, "origin", JobLeadOrigin(self.origin))
        object.__setattr__(self, "status", JobLeadStatus(self.status))
        if self.source in {
            JobLeadSource.CANONICAL_RESOLUTION,
            JobLeadSource.JOB_ALERT_INBOX,
        }:
            raise ValueError(
                "refresh-only operational sources cannot be persisted as Leads"
            )
        if self.contract_version != JOB_LEAD_CONTRACT_VERSION:
            raise ValueError("JobLead contract version is unsupported")

        source_url = canonicalize_job_lead_url("source_url", self.source_url)
        if source_url != self.source_url:
            raise ValueError("source_url is not canonical")
        for name, value, maximum in (
            ("title_hint", self.title_hint, 320),
            ("company_hint", self.company_hint, 240),
            ("location_hint", self.location_hint, 320),
            ("snippet_hint", self.snippet_hint, 4000),
            ("reason", self.reason, 500),
        ):
            if _clean_optional(name, value, maximum) != value:
                raise ValueError(f"{name} is not canonical")

        discovered_at = _utc("discovered_at", self.discovered_at)
        updated_at = _utc("updated_at", self.updated_at)
        if discovered_at != self.discovered_at or updated_at != self.updated_at:
            raise ValueError("JobLead timestamps must be stored in UTC")
        if updated_at < discovered_at:
            raise ValueError("updated_at precedes discovered_at")

        if self.query_id is not None and (
            not isinstance(self.query_id, str)
            or _QUERY_ID_RE.fullmatch(self.query_id) is None
        ):
            raise ValueError("query_id is invalid")
        if self.source is JobLeadSource.AUTHORIZED_WEB_SEARCH:
            if self.query_id is None:
                raise ValueError("authorized web search leads require query_id")
        elif self.query_id is not None:
            raise ValueError("query_id is reserved for authorized web search")

        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ):
            raise TypeError("confidence must be numeric")
        confidence = float(self.confidence)
        if confidence < 0.0 or confidence > 1.0:
            raise ValueError("confidence must be between zero and one")
        object.__setattr__(self, "confidence", confidence)

        email_sources = {
            JobLeadSource.LINKEDIN_ALERT_EMAIL,
            JobLeadSource.INDEED_ALERT_EMAIL,
            JobLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL,
        }
        if self.source in email_sources:
            if (
                not isinstance(self.source_message_digest, str)
                or _HASH_RE.fullmatch(self.source_message_digest) is None
            ):
                raise ValueError("email leads require a source message digest")
        elif self.source_message_digest is not None:
            raise ValueError("source_message_digest is reserved for email leads")
        if self.previous_content_hash is None:
            if self.lead_version != 1:
                raise ValueError("only the first JobLead version may omit ancestry")
        elif (
            self.lead_version == 1
            or not isinstance(self.previous_content_hash, str)
            or _HASH_RE.fullmatch(self.previous_content_hash) is None
        ):
            raise ValueError("previous_content_hash is invalid")
        if _HASH_RE.fullmatch(self.content_hash) is None:
            raise ValueError("content_hash is invalid")

        if self.status is JobLeadStatus.DISCOVERED:
            if self.lead_version != 1:
                raise ValueError("DISCOVERED is only valid for the first version")
            if self.canonical_url is not None or self.reason is not None:
                raise ValueError("a discovered lead cannot claim a resolution")
        elif self.status is JobLeadStatus.RESOLVED:
            if self.canonical_url is None:
                raise ValueError("a resolved lead requires canonical_url")
            canonical = canonicalize_job_lead_url(
                "canonical_url", self.canonical_url
            )
            if canonical != self.canonical_url:
                raise ValueError("canonical_url is not canonical")
        else:
            if self.canonical_url is not None:
                raise ValueError("an unresolved lead cannot claim canonical_url")
            if self.reason is None:
                raise ValueError(f"{self.status.value} requires a reason")

        expected_id = "job-lead-" + _hash(
            _identity_payload(
                subject_id=self.subject_id,
                source=self.source,
                origin=self.origin,
                source_url=self.source_url,
                query_id=self.query_id,
                source_message_digest=self.source_message_digest,
            )
        )
        if self.lead_id != expected_id:
            raise ValueError("lead_id does not match JobLead identity")
        expected_hash = _hash(self._content_payload())
        if self.content_hash != expected_hash:
            raise ValueError("content_hash does not match JobLead")

    def _content_payload(self) -> dict[str, Any]:
        return _content_payload(
            subject_id=self.subject_id,
            lead_id=self.lead_id,
            lead_version=self.lead_version,
            source=self.source,
            origin=self.origin,
            status=self.status,
            source_url=self.source_url,
            title_hint=self.title_hint,
            company_hint=self.company_hint,
            location_hint=self.location_hint,
            snippet_hint=self.snippet_hint,
            discovered_at=self.discovered_at,
            updated_at=self.updated_at,
            query_id=self.query_id,
            confidence=self.confidence,
            source_message_digest=self.source_message_digest,
            canonical_url=self.canonical_url,
            reason=self.reason,
            previous_content_hash=self.previous_content_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {**self._content_payload(), "content_hash": self.content_hash}

    @classmethod
    def discover(
        cls,
        *,
        subject_id: str,
        source: JobLeadSource,
        origin: JobLeadOrigin,
        source_url: str,
        discovered_at: datetime,
        confidence: float,
        title_hint: str | None = None,
        company_hint: str | None = None,
        location_hint: str | None = None,
        snippet_hint: str | None = None,
        query_id: str | None = None,
        source_message_digest: str | None = None,
    ) -> "JobLead":
        subject = _clean_required("subject_id", subject_id, 160)
        typed_source = JobLeadSource(source)
        typed_origin = JobLeadOrigin(origin)
        url = canonicalize_job_lead_url("source_url", source_url)
        discovered = _utc("discovered_at", discovered_at)
        title = _clean_optional("title_hint", title_hint, 320)
        company = _clean_optional("company_hint", company_hint, 240)
        location = _clean_optional("location_hint", location_hint, 320)
        snippet = _clean_optional("snippet_hint", snippet_hint, 4000)
        numeric_confidence = float(confidence)
        identity = _identity_payload(
            subject_id=subject,
            source=typed_source,
            origin=typed_origin,
            source_url=url,
            query_id=query_id,
            source_message_digest=source_message_digest,
        )
        lead_id = "job-lead-" + _hash(identity)
        payload = _content_payload(
            subject_id=subject,
            lead_id=lead_id,
            lead_version=1,
            source=typed_source,
            origin=typed_origin,
            status=JobLeadStatus.DISCOVERED,
            source_url=url,
            title_hint=title,
            company_hint=company,
            location_hint=location,
            snippet_hint=snippet,
            discovered_at=discovered,
            updated_at=discovered,
            query_id=query_id,
            confidence=numeric_confidence,
            source_message_digest=source_message_digest,
            canonical_url=None,
            reason=None,
            previous_content_hash=None,
        )
        return cls(
            subject_id=subject,
            lead_id=lead_id,
            lead_version=1,
            source=typed_source,
            origin=typed_origin,
            status=JobLeadStatus.DISCOVERED,
            source_url=url,
            title_hint=title,
            company_hint=company,
            location_hint=location,
            snippet_hint=snippet,
            discovered_at=discovered,
            updated_at=discovered,
            query_id=query_id,
            confidence=numeric_confidence,
            source_message_digest=source_message_digest,
            canonical_url=None,
            reason=None,
            previous_content_hash=None,
            content_hash=_hash(payload),
        )

    def transition(
        self,
        status: JobLeadStatus,
        *,
        now: datetime,
        canonical_url: str | None = None,
        reason: str | None = None,
    ) -> "JobLead":
        """Create the next immutable status version, preserving source evidence."""

        target = JobLeadStatus(status)
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(
                f"invalid JobLead transition {self.status.value} -> {target.value}"
            )
        updated = _utc("now", now)
        if updated < self.updated_at:
            raise ValueError("transition time precedes the current JobLead")
        canonical = (
            canonicalize_job_lead_url("canonical_url", canonical_url)
            if canonical_url is not None
            else None
        )
        clean_reason = _clean_optional("reason", reason, 500)
        payload = _content_payload(
            subject_id=self.subject_id,
            lead_id=self.lead_id,
            lead_version=self.lead_version + 1,
            source=self.source,
            origin=self.origin,
            status=target,
            source_url=self.source_url,
            title_hint=self.title_hint,
            company_hint=self.company_hint,
            location_hint=self.location_hint,
            snippet_hint=self.snippet_hint,
            discovered_at=self.discovered_at,
            updated_at=updated,
            query_id=self.query_id,
            confidence=self.confidence,
            source_message_digest=self.source_message_digest,
            canonical_url=canonical,
            reason=clean_reason,
            previous_content_hash=self.content_hash,
        )
        return JobLead(
            subject_id=self.subject_id,
            lead_id=self.lead_id,
            lead_version=self.lead_version + 1,
            source=self.source,
            origin=self.origin,
            status=target,
            source_url=self.source_url,
            title_hint=self.title_hint,
            company_hint=self.company_hint,
            location_hint=self.location_hint,
            snippet_hint=self.snippet_hint,
            discovered_at=self.discovered_at,
            updated_at=updated,
            query_id=self.query_id,
            confidence=self.confidence,
            source_message_digest=self.source_message_digest,
            canonical_url=canonical,
            reason=clean_reason,
            previous_content_hash=self.content_hash,
            content_hash=_hash(payload),
        )

    def is_direct_successor_of(self, previous: "JobLead") -> bool:
        if not isinstance(previous, JobLead):
            return False
        return (
            self.subject_id == previous.subject_id
            and self.lead_id == previous.lead_id
            and self.lead_version == previous.lead_version + 1
            and self.previous_content_hash == previous.content_hash
            and self.status in _ALLOWED_TRANSITIONS[previous.status]
            and self.source == previous.source
            and self.origin == previous.origin
            and self.source_url == previous.source_url
            and self.title_hint == previous.title_hint
            and self.company_hint == previous.company_hint
            and self.location_hint == previous.location_hint
            and self.snippet_hint == previous.snippet_hint
            and self.discovered_at == previous.discovered_at
            and self.query_id == previous.query_id
            and self.confidence == previous.confidence
            and self.source_message_digest == previous.source_message_digest
            and self.updated_at >= previous.updated_at
        )


@dataclass(frozen=True, slots=True)
class JobLeadReadResult:
    status: JobLeadReadStatus
    lead: JobLead | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", JobLeadReadStatus(self.status))
        if self.status is JobLeadReadStatus.FOUND:
            if not isinstance(self.lead, JobLead):
                raise ValueError("FOUND JobLead read requires a lead")
        elif self.lead is not None:
            raise ValueError("failed JobLead read cannot expose a lead")


@dataclass(frozen=True, slots=True)
class JobLeadListResult:
    status: JobLeadListStatus
    leads: tuple[JobLead, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", JobLeadListStatus(self.status))
        if not isinstance(self.leads, tuple) or any(
            not isinstance(lead, JobLead) for lead in self.leads
        ):
            raise TypeError("JobLead list must be typed")
        if self.status is JobLeadListStatus.INTEGRITY_FAILURE and self.leads:
            raise ValueError("failed JobLead list cannot expose leads")


@dataclass(frozen=True, slots=True)
class JobLeadWriteResult:
    status: JobLeadWriteStatus
    lead: JobLead | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", JobLeadWriteStatus(self.status))
        if self.status in {
            JobLeadWriteStatus.CREATED,
            JobLeadWriteStatus.UNCHANGED,
        }:
            if not isinstance(self.lead, JobLead):
                raise ValueError("successful JobLead write requires a lead")
        elif self.lead is not None:
            raise ValueError("failed JobLead write cannot expose a lead")


@runtime_checkable
class JobLeadRepository(Protocol):
    def save(self, lead: JobLead) -> JobLeadWriteResult: ...

    def get(self, subject_id: str, lead_id: str) -> JobLeadReadResult: ...

    def list_current(self, subject_id: str) -> JobLeadListResult: ...


def _lead_from_dict(value: Any) -> JobLead:
    expected = {
        "canonical_url",
        "company_hint",
        "confidence",
        "content_hash",
        "contract_version",
        "discovered_at",
        "lead_id",
        "lead_version",
        "location_hint",
        "origin",
        "previous_content_hash",
        "query_id",
        "reason",
        "snippet_hint",
        "source",
        "source_message_digest",
        "source_url",
        "status",
        "subject_id",
        "title_hint",
        "updated_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted JobLead is malformed")
    return JobLead(
        subject_id=value["subject_id"],
        lead_id=value["lead_id"],
        lead_version=value["lead_version"],
        source=JobLeadSource(value["source"]),
        origin=JobLeadOrigin(value["origin"]),
        status=JobLeadStatus(value["status"]),
        source_url=value["source_url"],
        title_hint=value["title_hint"],
        company_hint=value["company_hint"],
        location_hint=value["location_hint"],
        snippet_hint=value["snippet_hint"],
        discovered_at=_parse_time(value["discovered_at"]),
        updated_at=_parse_time(value["updated_at"]),
        query_id=value["query_id"],
        confidence=value["confidence"],
        source_message_digest=value["source_message_digest"],
        canonical_url=value["canonical_url"],
        reason=value["reason"],
        previous_content_hash=value["previous_content_hash"],
        content_hash=value["content_hash"],
        contract_version=value["contract_version"],
    )


class PrivateHomeJobLeadRepository:
    """Immutable JobLead histories under ``state/discovery/job-leads``."""

    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _subject_directory(self, subject_id: str) -> Path:
        subject = _clean_required("subject_id", subject_id, 160)
        return (
            self._home.root
            / "state"
            / "discovery"
            / "job-leads"
            / _subject_key(subject)
        )

    def _lead_directory(self, subject_id: str, lead_id: str) -> Path:
        if not isinstance(lead_id, str) or _LEAD_ID_RE.fullmatch(lead_id) is None:
            raise ValueError("lead_id is invalid")
        return self._subject_directory(subject_id) / lead_id

    def _path(self, lead: JobLead) -> Path:
        return self._lead_directory(lead.subject_id, lead.lead_id) / (
            f"v{lead.lead_version:08d}.json"
        )

    @staticmethod
    def _read_path(path: Path) -> JobLead:
        if path.is_symlink() or not path.is_file():
            raise ValueError("JobLead record is not a regular file")
        return _lead_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def get(self, subject_id: str, lead_id: str) -> JobLeadReadResult:
        try:
            subject = _clean_required("subject_id", subject_id, 160)
            directory = self._lead_directory(subject, lead_id)
        except (TypeError, ValueError):
            return JobLeadReadResult(JobLeadReadStatus.INTEGRITY_FAILURE, None)
        if not directory.exists():
            return JobLeadReadResult(JobLeadReadStatus.NOT_FOUND, None)
        if directory.is_symlink() or not directory.is_dir():
            return JobLeadReadResult(JobLeadReadStatus.INTEGRITY_FAILURE, None)
        try:
            paths = tuple(sorted(directory.glob("v*.json")))
            if not paths:
                raise ValueError("JobLead has no versions")
            leads = tuple(self._read_path(path) for path in paths)
            for index, (path, lead) in enumerate(zip(paths, leads), start=1):
                if (
                    path.name != f"v{index:08d}.json"
                    or lead.subject_id != subject
                    or lead.lead_id != lead_id
                    or lead.lead_version != index
                ):
                    raise ValueError("JobLead version history is invalid")
                if index == 1:
                    if lead.status is not JobLeadStatus.DISCOVERED:
                        raise ValueError("JobLead history does not start discovered")
                elif not lead.is_direct_successor_of(leads[index - 2]):
                    raise ValueError("JobLead ancestry or transition is invalid")
            return JobLeadReadResult(JobLeadReadStatus.FOUND, leads[-1])
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return JobLeadReadResult(JobLeadReadStatus.INTEGRITY_FAILURE, None)

    def list_current(self, subject_id: str) -> JobLeadListResult:
        try:
            subject = _clean_required("subject_id", subject_id, 160)
            directory = self._subject_directory(subject)
        except (TypeError, ValueError):
            return JobLeadListResult(JobLeadListStatus.INTEGRITY_FAILURE, ())
        if not directory.exists():
            return JobLeadListResult(JobLeadListStatus.SUCCEEDED, ())
        if directory.is_symlink() or not directory.is_dir():
            return JobLeadListResult(JobLeadListStatus.INTEGRITY_FAILURE, ())
        try:
            leads: list[JobLead] = []
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if path.is_symlink() or not path.is_dir():
                    raise ValueError("JobLead directory is malformed")
                read = self.get(subject, path.name)
                if read.status is not JobLeadReadStatus.FOUND or read.lead is None:
                    raise ValueError("JobLead history is unreadable")
                leads.append(read.lead)
            ordered = tuple(
                sorted(
                    leads,
                    key=lambda item: (item.discovered_at, item.lead_id),
                    reverse=True,
                )
            )
            return JobLeadListResult(JobLeadListStatus.SUCCEEDED, ordered)
        except (OSError, TypeError, ValueError):
            return JobLeadListResult(JobLeadListStatus.INTEGRITY_FAILURE, ())

    def save(self, lead: JobLead) -> JobLeadWriteResult:
        if not isinstance(lead, JobLead):
            raise TypeError("lead must be typed")
        path = self._path(lead)
        with self._lock:
            try:
                self._home.ensure()
                if path.exists() or path.is_symlink():
                    existing = self._read_path(path)
                    if existing.to_dict() == lead.to_dict():
                        return JobLeadWriteResult(
                            JobLeadWriteStatus.UNCHANGED, existing
                        )
                    return JobLeadWriteResult(JobLeadWriteStatus.CONFLICT, None)

                current = self.get(lead.subject_id, lead.lead_id)
                if current.status is JobLeadReadStatus.INTEGRITY_FAILURE:
                    return JobLeadWriteResult(
                        JobLeadWriteStatus.INTEGRITY_FAILURE, None
                    )
                if lead.lead_version == 1:
                    if current.status is not JobLeadReadStatus.NOT_FOUND:
                        return JobLeadWriteResult(
                            JobLeadWriteStatus.CONFLICT, None
                        )
                elif (
                    current.status is not JobLeadReadStatus.FOUND
                    or current.lead is None
                    or not lead.is_direct_successor_of(current.lead)
                ):
                    return JobLeadWriteResult(JobLeadWriteStatus.CONFLICT, None)

                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            lead.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                if created:
                    return JobLeadWriteResult(JobLeadWriteStatus.CREATED, lead)
                existing = self._read_path(path)
                if existing.to_dict() == lead.to_dict():
                    return JobLeadWriteResult(
                        JobLeadWriteStatus.UNCHANGED, existing
                    )
                return JobLeadWriteResult(JobLeadWriteStatus.CONFLICT, None)
            except (OSError, PrivateHomeError):
                return JobLeadWriteResult(JobLeadWriteStatus.FAILED, None)
            except (TypeError, ValueError, json.JSONDecodeError):
                return JobLeadWriteResult(
                    JobLeadWriteStatus.INTEGRITY_FAILURE, None
                )


__all__ = [
    "JOB_LEAD_CONTRACT_VERSION",
    "JobLead",
    "JobLeadListResult",
    "JobLeadListStatus",
    "JobLeadOrigin",
    "JobLeadReadResult",
    "JobLeadReadStatus",
    "JobLeadRepository",
    "JobLeadSource",
    "JobLeadStatus",
    "JobLeadWriteResult",
    "JobLeadWriteStatus",
    "PrivateHomeJobLeadRepository",
    "canonicalize_job_lead_url",
]
