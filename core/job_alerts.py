"""Bounded, opt-in ingestion of job-alert email projections.

This module deliberately consumes the narrow :class:`MailboxProvider`
projection instead of exposing a general inbox reader.  Parsing is local and
deterministic: message bodies are never sent to a model, and the returned lead
objects retain neither raw message content nor mailbox identifiers.

An alert-derived URL is evidence of a possible job, not an authoritative job
fact.  Callers must still resolve and verify an employer or ATS posting before
creating a normalized job.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from enum import StrEnum
from html import unescape
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from auth.mailbox import (
    MailAuthenticationEvidence,
    MailboxMessage,
    MailboxProvider,
)
from source_connectors.contract import PUBLIC_ATS_JOB_HOST_SUFFIXES

from .job_leads import (
    JobLead,
    JobLeadListStatus,
    JobLeadOrigin,
    JobLeadRepository,
    JobLeadSource,
    JobLeadWriteStatus,
)


_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$",
    re.ASCII,
)
_DOMAIN_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$",
    re.ASCII,
)
_TEXT_URL_RE = re.compile(r"https://[^\s<>'\"]+", re.IGNORECASE)
_LINKEDIN_JOB_PATH_RE = re.compile(
    r"^/(?:comm/)?jobs/view/([0-9]{5,24})/?$",
    re.IGNORECASE,
)
_INDEED_JOB_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$", re.ASCII)
_SAFE_JOB_QUERY_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$", re.ASCII)
_MAX_PROVIDER_MESSAGES = 25
_MAX_AGE = timedelta(days=7)
_MAX_FUTURE_SKEW = timedelta(minutes=2)

_ATS_DOMAINS = PUBLIC_ATS_JOB_HOST_SUFFIXES
_JOB_PATH_MARKERS = frozenset(
    {"career", "careers", "job", "jobs", "opening", "openings", "position", "positions", "vacancy", "vacancies"}
)
_UNSAFE_PATH_MARKERS = frozenset(
    {
        "account",
        "auth",
        "captcha",
        "login",
        "logout",
        "mfa",
        "oauth",
        "password",
        "reset",
        "security",
        "signin",
        "sign-in",
        "unsubscribe",
        "verify",
        "verification",
    }
)
_SAFE_JOB_QUERY_KEYS = frozenset(
    {
        "gh_jid",
        "id",
        "jid",
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
_UNSAFE_SUBJECT_PHRASES = (
    "account security",
    "login code",
    "mfa code",
    "new sign-in",
    "password reset",
    "reset your password",
    "security alert",
    "verification code",
    "verify your account",
)
_GENERIC_ANCHOR_TEXT = frozenset(
    {"apply", "apply now", "learn more", "open", "see job", "view", "view job", "view jobs"}
)


class JobAlertIngestionStatus(StrEnum):
    DISABLED = "DISABLED"
    SUCCEEDED = "SUCCEEDED"
    NO_MATCHES = "NO_MATCHES"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_CONFIG = "INVALID_CONFIG"


class JobAlertIssueCode(StrEnum):
    INVALID_CONFIG = "INVALID_CONFIG"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_LIMIT_EXCEEDED = "PROVIDER_LIMIT_EXCEEDED"
    INVALID_MESSAGE = "INVALID_MESSAGE"
    RECIPIENT_MISMATCH = "RECIPIENT_MISMATCH"
    SENDER_NOT_ALLOWED = "SENDER_NOT_ALLOWED"
    SENDER_NOT_AUTHENTICATED = "SENDER_NOT_AUTHENTICATED"
    OUTSIDE_TIME_WINDOW = "OUTSIDE_TIME_WINDOW"
    UNSAFE_MESSAGE = "UNSAFE_MESSAGE"
    NO_JOB_LINKS = "NO_JOB_LINKS"
    PARSE_FAILED = "PARSE_FAILED"


class JobAlertLeadSource(StrEnum):
    LINKEDIN_ALERT_EMAIL = "LINKEDIN_ALERT_EMAIL"
    INDEED_ALERT_EMAIL = "INDEED_ALERT_EMAIL"
    EMPLOYER_OR_ATS_ALERT_EMAIL = "EMPLOYER_OR_ATS_ALERT_EMAIL"


class JobAlertLeadUrlKind(StrEnum):
    PLATFORM_LEAD = "PLATFORM_LEAD"
    OFFICIAL_CANDIDATE = "OFFICIAL_CANDIDATE"


class JobAlertLeadOrigin(StrEnum):
    LINKEDIN_SEARCH_INDEX = "LINKEDIN_SEARCH_INDEX"
    INDEED_SEARCH_INDEX = "INDEED_SEARCH_INDEX"
    EMPLOYER = "EMPLOYER"
    ATS = "ATS"
    UNKNOWN_WEB = "UNKNOWN_WEB"


@dataclass(frozen=True, slots=True)
class JobAlertInboxConfig:
    """Non-secret policy for one explicitly authorized alert inbox."""

    enabled: bool = False
    recipient: str = field(default="", repr=False)
    allowed_sender_domains: tuple[str, ...] = ("linkedin.com", "indeed.com")
    max_age: timedelta = timedelta(hours=1)
    max_messages: int = 25
    max_links_per_message: int = 50
    max_message_chars: int = 64 * 1024

    def validate(self) -> None:
        if not self.enabled:
            return
        if not isinstance(self.recipient, str) or _EMAIL_RE.fullmatch(self.recipient) is None:
            raise ValueError("job-alert recipient is invalid")
        if not self.allowed_sender_domains:
            raise ValueError("job-alert sender allowlist is required")
        for domain in self.allowed_sender_domains:
            if (
                not isinstance(domain, str)
                or _DOMAIN_RE.fullmatch(domain) is None
                or domain != domain.casefold()
            ):
                raise ValueError("job-alert sender allowlist is invalid")
        if not isinstance(self.max_age, timedelta) or not timedelta(minutes=1) <= self.max_age <= _MAX_AGE:
            raise ValueError("job-alert time window is invalid")
        if (
            isinstance(self.max_messages, bool)
            or not isinstance(self.max_messages, int)
            or not 1 <= self.max_messages <= _MAX_PROVIDER_MESSAGES
        ):
            raise ValueError("job-alert message limit is invalid")
        if (
            isinstance(self.max_links_per_message, bool)
            or not isinstance(self.max_links_per_message, int)
            or not 1 <= self.max_links_per_message <= 100
        ):
            raise ValueError("job-alert link limit is invalid")
        if (
            isinstance(self.max_message_chars, bool)
            or not isinstance(self.max_message_chars, int)
            or not 1_024 <= self.max_message_chars <= 128 * 1024
        ):
            raise ValueError("job-alert content limit is invalid")


@dataclass(frozen=True, slots=True)
class JobAlertLead:
    """Sanitized pre-normalization evidence derived from one alert link."""

    lead_id: str
    source: JobAlertLeadSource
    source_url: str
    url_kind: JobAlertLeadUrlKind
    origin: JobAlertLeadOrigin
    discovered_at: datetime
    provenance_digest: str
    title_hint: str = ""
    company_hint: str = ""
    location_hint: str = ""

    @property
    def is_authoritative(self) -> bool:
        # Even an official-looking link must be fetched and verified later.
        return False


@dataclass(frozen=True, slots=True)
class JobAlertIssue:
    code: JobAlertIssueCode
    reason: str
    provenance_digest: str = ""


@dataclass(frozen=True, slots=True)
class JobAlertIngestionResult:
    status: JobAlertIngestionStatus
    leads: tuple[JobAlertLead, ...] = ()
    issues: tuple[JobAlertIssue, ...] = ()
    messages_examined: int = 0
    messages_accepted: int = 0
    provider_was_called: bool = False

    @property
    def provider_called(self) -> bool:
        return self.provider_was_called


class JobAlertPersistenceStatus(StrEnum):
    DISABLED = "DISABLED"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class JobAlertPersistenceResult:
    status: JobAlertPersistenceStatus
    leads: tuple[JobLead, ...] = ()
    acquisition_sources: tuple[JobLeadSource, ...] = ()
    messages_examined: int = 0
    messages_accepted: int = 0
    created: int = 0
    duplicates: int = 0
    failed: int = 0
    source_status: JobAlertIngestionStatus = JobAlertIngestionStatus.DISABLED

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", JobAlertPersistenceStatus(self.status)
        )
        object.__setattr__(
            self, "source_status", JobAlertIngestionStatus(self.source_status)
        )
        if not isinstance(self.leads, tuple) or any(
            not isinstance(lead, JobLead) for lead in self.leads
        ):
            raise TypeError("persisted job-alert leads must be typed")
        if not isinstance(self.acquisition_sources, tuple) or any(
            not isinstance(source, JobLeadSource)
            for source in self.acquisition_sources
        ):
            raise TypeError("job-alert acquisition sources must be typed")
        if self.acquisition_sources and len(self.acquisition_sources) != len(
            self.leads
        ):
            raise ValueError(
                "job-alert acquisition sources must align with persisted leads"
            )
        for name in (
            "messages_examined",
            "messages_accepted",
            "created",
            "duplicates",
            "failed",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be non-negative")


@dataclass
class JobAlertInboxIngestor:
    """Read a bounded alert window and return sanitized, unverified leads."""

    provider: MailboxProvider = field(repr=False)
    config: JobAlertInboxConfig
    now: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc),
        repr=False,
    )

    async def ingest(self) -> JobAlertIngestionResult:
        if not self.config.enabled:
            return JobAlertIngestionResult(JobAlertIngestionStatus.DISABLED)

        try:
            self.config.validate()
            current = _as_utc(self.now())
        except Exception:
            return JobAlertIngestionResult(
                JobAlertIngestionStatus.INVALID_CONFIG,
                issues=(JobAlertIssue(JobAlertIssueCode.INVALID_CONFIG, "job-alert configuration is invalid"),),
            )

        since = current - self.config.max_age
        try:
            messages = await self.provider.search_recent(
                recipient=self.config.recipient,
                since=since,
                limit=self.config.max_messages,
            )
        except Exception:
            return JobAlertIngestionResult(
                JobAlertIngestionStatus.PROVIDER_UNAVAILABLE,
                issues=(JobAlertIssue(JobAlertIssueCode.PROVIDER_UNAVAILABLE, "job-alert mailbox is unavailable"),),
                provider_was_called=True,
            )

        issues: list[JobAlertIssue] = []
        try:
            bounded_messages = tuple(messages)
        except Exception:
            return JobAlertIngestionResult(
                JobAlertIngestionStatus.PARTIAL_FAILURE,
                issues=(JobAlertIssue(JobAlertIssueCode.PARSE_FAILED, "job-alert provider response could not be parsed"),),
                provider_was_called=True,
            )
        if len(bounded_messages) > self.config.max_messages:
            issues.append(
                JobAlertIssue(
                    JobAlertIssueCode.PROVIDER_LIMIT_EXCEEDED,
                    "job-alert provider returned more messages than requested",
                )
            )
            bounded_messages = bounded_messages[: self.config.max_messages]

        leads: list[JobAlertLead] = []
        accepted_messages = 0
        for message in bounded_messages:
            parsed_leads, parsed_issues = _parse_message(
                message,
                config=self.config,
                since=since,
                current=current,
            )
            if parsed_leads:
                accepted_messages += 1
                leads.extend(parsed_leads)
            issues.extend(parsed_issues)

        unique: dict[str, JobAlertLead] = {}
        for lead in leads:
            unique.setdefault(lead.source_url, lead)
        final_leads = tuple(unique.values())
        if final_leads and issues:
            status = JobAlertIngestionStatus.PARTIAL_FAILURE
        elif final_leads:
            status = JobAlertIngestionStatus.SUCCEEDED
        elif issues:
            status = JobAlertIngestionStatus.PARTIAL_FAILURE
        else:
            status = JobAlertIngestionStatus.NO_MATCHES
        return JobAlertIngestionResult(
            status,
            leads=final_leads,
            issues=tuple(issues),
            messages_examined=len(bounded_messages),
            messages_accepted=accepted_messages,
            provider_was_called=True,
        )


async def ingest_job_alerts_for_subject(
    *,
    subject_id: str,
    ingestor: JobAlertInboxIngestor,
    repository: JobLeadRepository,
) -> JobAlertPersistenceResult:
    """Persist sanitized alert evidence without crossing the job-fact boundary."""

    if not isinstance(subject_id, str) or not subject_id.strip():
        raise ValueError("subject_id is required")
    if not isinstance(ingestor, JobAlertInboxIngestor):
        raise TypeError("ingestor must be JobAlertInboxIngestor")
    if not isinstance(repository, JobLeadRepository):
        raise TypeError("repository must implement JobLeadRepository")
    clean_subject = subject_id.strip()
    source_result = await ingestor.ingest()
    if source_result.status is JobAlertIngestionStatus.DISABLED:
        return JobAlertPersistenceResult(
            status=JobAlertPersistenceStatus.DISABLED,
            source_status=source_result.status,
        )
    if source_result.status in {
        JobAlertIngestionStatus.PROVIDER_UNAVAILABLE,
        JobAlertIngestionStatus.INVALID_CONFIG,
    }:
        return JobAlertPersistenceResult(
            status=JobAlertPersistenceStatus.FAILED,
            messages_examined=source_result.messages_examined,
            messages_accepted=source_result.messages_accepted,
            failed=1,
            source_status=source_result.status,
        )
    listed = repository.list_current(clean_subject)
    if listed.status is not JobLeadListStatus.SUCCEEDED:
        return JobAlertPersistenceResult(
            status=JobAlertPersistenceStatus.FAILED,
            messages_examined=source_result.messages_examined,
            messages_accepted=source_result.messages_accepted,
            failed=max(1, len(source_result.leads)),
            source_status=source_result.status,
        )
    existing_by_url = {lead.source_url: lead for lead in listed.leads}
    source_map = {
        JobAlertLeadSource.LINKEDIN_ALERT_EMAIL: (
            JobLeadSource.LINKEDIN_ALERT_EMAIL
        ),
        JobAlertLeadSource.INDEED_ALERT_EMAIL: (
            JobLeadSource.INDEED_ALERT_EMAIL
        ),
        JobAlertLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL: (
            JobLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL
        ),
    }
    origin_map = {
        JobAlertLeadOrigin.LINKEDIN_SEARCH_INDEX: (
            JobLeadOrigin.LINKEDIN_SEARCH_INDEX
        ),
        JobAlertLeadOrigin.INDEED_SEARCH_INDEX: (
            JobLeadOrigin.INDEED_SEARCH_INDEX
        ),
        JobAlertLeadOrigin.EMPLOYER: JobLeadOrigin.EMPLOYER,
        JobAlertLeadOrigin.ATS: JobLeadOrigin.ATS,
        JobAlertLeadOrigin.UNKNOWN_WEB: JobLeadOrigin.UNKNOWN_WEB,
    }
    persisted: list[JobLead] = []
    acquisition_sources: list[JobLeadSource] = []
    created = duplicates = failed = 0
    for draft in source_result.leads:
        existing = existing_by_url.get(draft.source_url)
        if existing is not None:
            duplicates += 1
            persisted.append(existing)
            acquisition_sources.append(source_map[draft.source])
            continue
        try:
            lead = JobLead.discover(
                subject_id=clean_subject,
                source=source_map[draft.source],
                origin=origin_map[draft.origin],
                source_url=draft.source_url,
                discovered_at=draft.discovered_at,
                confidence=0.70,
                title_hint=draft.title_hint or None,
                company_hint=draft.company_hint or None,
                location_hint=draft.location_hint or None,
                source_message_digest=draft.provenance_digest,
            )
            written = repository.save(lead)
        except (KeyError, TypeError, ValueError):
            failed += 1
            continue
        if written.status is JobLeadWriteStatus.CREATED and written.lead:
            created += 1
            persisted.append(written.lead)
            acquisition_sources.append(source_map[draft.source])
            existing_by_url[written.lead.source_url] = written.lead
        elif written.status is JobLeadWriteStatus.UNCHANGED and written.lead:
            duplicates += 1
            persisted.append(written.lead)
            acquisition_sources.append(source_map[draft.source])
        else:
            failed += 1
    status = (
        JobAlertPersistenceStatus.PARTIAL_FAILURE
        if failed
        or source_result.status is JobAlertIngestionStatus.PARTIAL_FAILURE
        else JobAlertPersistenceStatus.SUCCEEDED
    )
    return JobAlertPersistenceResult(
        status=status,
        leads=tuple(persisted),
        acquisition_sources=tuple(acquisition_sources),
        messages_examined=source_result.messages_examined,
        messages_accepted=source_result.messages_accepted,
        created=created,
        duplicates=duplicates,
        failed=failed,
        source_status=source_result.status,
    )


def _parse_message(
    message: object,
    *,
    config: JobAlertInboxConfig,
    since: datetime,
    current: datetime,
) -> tuple[tuple[JobAlertLead, ...], tuple[JobAlertIssue, ...]]:
    if not isinstance(message, MailboxMessage):
        return (), (JobAlertIssue(JobAlertIssueCode.INVALID_MESSAGE, "job-alert message projection is invalid"),)

    digest = _message_digest(message)
    try:
        received_at = _as_utc(message.received_at)
        if received_at < since or received_at > current + _MAX_FUTURE_SKEW:
            return (), (_issue(JobAlertIssueCode.OUTSIDE_TIME_WINDOW, digest),)
        if config.recipient.casefold() not in {
            recipient.casefold() for recipient in message.recipients if isinstance(recipient, str)
        }:
            return (), (_issue(JobAlertIssueCode.RECIPIENT_MISMATCH, digest),)
        sender_domain = _allowed_sender_domain(message.sender, config.allowed_sender_domains)
        if sender_domain is None:
            return (), (_issue(JobAlertIssueCode.SENDER_NOT_ALLOWED, digest),)
        if not _sender_is_authenticated(message):
            return (), (_issue(JobAlertIssueCode.SENDER_NOT_AUTHENTICATED, digest),)
        if _subject_is_unsafe(message.subject):
            return (), (_issue(JobAlertIssueCode.UNSAFE_MESSAGE, digest),)

        source = _source_for_sender(sender_domain)
        links = _extract_link_candidates(
            message,
            maximum_chars=config.max_message_chars,
        )
        leads: list[JobAlertLead] = []
        seen: set[str] = set()
        for raw_url, anchor_text in links:
            normalized = _normalize_job_url(raw_url)
            if normalized is None:
                continue
            source_url, url_kind, origin = normalized
            if source_url in seen:
                continue
            seen.add(source_url)
            title_hint, company_hint = _job_hints(anchor_text)
            leads.append(
                JobAlertLead(
                    lead_id="job-lead-" + hashlib.sha256(source_url.encode("utf-8")).hexdigest(),
                    source=source,
                    source_url=source_url,
                    url_kind=url_kind,
                    origin=origin,
                    discovered_at=received_at,
                    provenance_digest=digest,
                    title_hint=title_hint,
                    company_hint=company_hint,
                )
            )
            if len(leads) >= config.max_links_per_message:
                break
        if not leads:
            return (), (_issue(JobAlertIssueCode.NO_JOB_LINKS, digest),)
        return tuple(leads), ()
    except Exception:
        return (), (_issue(JobAlertIssueCode.PARSE_FAILED, digest),)


def _issue(code: JobAlertIssueCode, digest: str) -> JobAlertIssue:
    reasons = {
        JobAlertIssueCode.RECIPIENT_MISMATCH: "job-alert message did not match the configured recipient",
        JobAlertIssueCode.SENDER_NOT_ALLOWED: "job-alert sender is not allowed",
        JobAlertIssueCode.SENDER_NOT_AUTHENTICATED: "job-alert sender authentication could not be verified",
        JobAlertIssueCode.OUTSIDE_TIME_WINDOW: "job-alert message is outside the requested time window",
        JobAlertIssueCode.UNSAFE_MESSAGE: "job-alert message is an account or security message",
        JobAlertIssueCode.NO_JOB_LINKS: "job-alert message contained no safe job links",
        JobAlertIssueCode.PARSE_FAILED: "job-alert message could not be parsed",
    }
    return JobAlertIssue(code, reasons[code], digest)


def _allowed_sender_domain(sender: str, allowed: tuple[str, ...]) -> str | None:
    if not isinstance(sender, str) or "\r" in sender or "\n" in sender:
        return None
    addresses = getaddresses((sender,))
    if len(addresses) != 1:
        return None
    _display_name, address = addresses[0]
    if not address or _EMAIL_RE.fullmatch(address) is None:
        return None
    domain = address.rsplit("@", 1)[1].casefold().rstrip(".")
    for allowed_domain in allowed:
        if domain == allowed_domain or domain.endswith(f".{allowed_domain}"):
            return domain
    return None


def _source_for_sender(domain: str) -> JobAlertLeadSource:
    if domain == "linkedin.com" or domain.endswith(".linkedin.com"):
        return JobAlertLeadSource.LINKEDIN_ALERT_EMAIL
    if domain == "indeed.com" or domain.endswith(".indeed.com"):
        return JobAlertLeadSource.INDEED_ALERT_EMAIL
    return JobAlertLeadSource.EMPLOYER_OR_ATS_ALERT_EMAIL


def _sender_is_authenticated(message: MailboxMessage) -> bool:
    evidence = message.authentication
    return (
        isinstance(evidence, MailAuthenticationEvidence)
        and evidence.sender_is_authenticated
    )


def _subject_is_unsafe(subject: str) -> bool:
    if not isinstance(subject, str):
        return True
    normalized = " ".join(subject.casefold().split())
    return any(phrase in normalized for phrase in _UNSAFE_SUBJECT_PHRASES)


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a" or self._href is not None:
            return
        attributes = {name.casefold(): value for name, value in attrs}
        href = attributes.get("href")
        if isinstance(href, str):
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None and len("".join(self._text)) < 512:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


def _extract_link_candidates(
    message: MailboxMessage,
    *,
    maximum_chars: int,
) -> tuple[tuple[str, str], ...]:
    text = message.text[:maximum_chars] if isinstance(message.text, str) else ""
    html = message.html[:maximum_chars] if isinstance(message.html, str) else ""
    collector = _AnchorCollector()
    collector.feed(html)
    links = list(collector.links)
    for candidate in _TEXT_URL_RE.findall(unescape(f"{text}\n{html}")):
        links.append((candidate.rstrip(".,);]"), ""))
    return tuple(links)


def _normalize_job_url(
    raw_url: str,
) -> tuple[str, JobAlertLeadUrlKind, JobAlertLeadOrigin] | None:
    if not isinstance(raw_url, str):
        return None
    candidate = unescape(raw_url.strip())
    if not candidate or len(candidate) > 2_048 or any(ord(char) < 32 for char in candidate):
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() != "https" or parsed.username is not None or parsed.password is not None:
        return None
    if port not in (None, 443):
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if _DOMAIN_RE.fullmatch(host) is None:
        return None
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    inspected_path = unquote(path)
    path_segments = {
        segment.casefold() for segment in inspected_path.split("/") if segment
    }
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=50)
    query_keys = {key.casefold() for key, _value in query_pairs}
    if path_segments & _UNSAFE_PATH_MARKERS or query_keys & _UNSAFE_PATH_MARKERS:
        return None

    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        match = _LINKEDIN_JOB_PATH_RE.fullmatch(path)
        if match is None:
            return None
        return (
            f"https://www.linkedin.com/jobs/view/{match.group(1)}",
            JobAlertLeadUrlKind.PLATFORM_LEAD,
            JobAlertLeadOrigin.LINKEDIN_SEARCH_INDEX,
        )

    if host == "indeed.com" or host.endswith(".indeed.com"):
        if path.casefold().rstrip("/") not in {"/viewjob", "/rc/clk"}:
            return None
        job_keys = [value for key, value in query_pairs if key.casefold() == "jk"]
        if len(job_keys) != 1 or _INDEED_JOB_KEY_RE.fullmatch(job_keys[0]) is None:
            return None
        return (
            urlunsplit(("https", "www.indeed.com", "/viewjob", urlencode({"jk": job_keys[0]}), "")),
            JobAlertLeadUrlKind.PLATFORM_LEAD,
            JobAlertLeadOrigin.INDEED_SEARCH_INDEX,
        )

    known_ats = any(host == domain or host.endswith(f".{domain}") for domain in _ATS_DOMAINS)
    has_job_path = bool(path_segments & _JOB_PATH_MARKERS)
    if not known_ats and not has_job_path:
        return None
    if host == "successfactors.com" or host.endswith(".successfactors.com") or host == "successfactors.eu" or host.endswith(".successfactors.eu"):
        identity: dict[str, str] = {}
        for key, value in query_pairs:
            normalized_key = key.casefold()
            if (
                normalized_key not in _SUCCESSFACTORS_JOB_QUERY_KEYS
                or _SAFE_JOB_QUERY_VALUE_RE.fullmatch(value) is None
            ):
                continue
            if normalized_key == "career_ns" and value.casefold() != "job_listing":
                continue
            identity.setdefault(
                normalized_key,
                "job_listing" if normalized_key == "career_ns" else value,
            )
        safe_query = [
            (key, identity[key])
            for key in _SUCCESSFACTORS_JOB_QUERY_KEYS
            if key in identity
        ]
    else:
        safe_query = [
            (key, value)
            for key, value in query_pairs
            if key.casefold() in _SAFE_JOB_QUERY_KEYS
            and _SAFE_JOB_QUERY_VALUE_RE.fullmatch(value) is not None
        ]
    return (
        urlunsplit(("https", host, path, urlencode(safe_query), "")),
        JobAlertLeadUrlKind.OFFICIAL_CANDIDATE,
        JobAlertLeadOrigin.ATS if known_ats else JobAlertLeadOrigin.UNKNOWN_WEB,
    )


def _job_hints(value: str) -> tuple[str, str]:
    """Extract only explicit title/company relationships from anchor text."""

    if not isinstance(value, str):
        return "", ""
    cleaned = " ".join(value.split())
    if not cleaned or cleaned.casefold() in _GENERIC_ANCHOR_TEXT:
        return "", ""
    cleaned = re.sub(
        r"\s*(?:\||-)\s*(?:LinkedIn|Indeed)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    hiring = re.fullmatch(
        r"(.+?)\s+hiring\s+(.+?)(?:\s+in\s+.+)?",
        cleaned,
        flags=re.IGNORECASE,
    )
    if hiring is not None:
        return hiring.group(2).strip()[:240], hiring.group(1).strip()[:240]
    at_company = re.fullmatch(
        r"(.+?)\s+at\s+(.+)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if at_company is not None:
        return (
            at_company.group(1).strip()[:240],
            at_company.group(2).strip()[:240],
        )
    return cleaned[:240], ""


def _message_digest(message: MailboxMessage) -> str:
    received = message.received_at.isoformat() if isinstance(message.received_at, datetime) else "invalid-time"
    material = "\n".join(
        (
            message.message_id if isinstance(message.message_id, str) else "invalid-id",
            received,
            message.sender if isinstance(message.sender, str) else "invalid-sender",
        )
    )
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("timestamp must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
