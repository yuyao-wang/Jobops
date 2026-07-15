"""Transactional event ledger and duplicate-submission protection.

The event stream is append-only at the SQLite level.  Mutable projections (runs,
submission intents, leases) are updated in the same transaction as their audit
event, allowing workers to resume without reconstructing browser history.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from .outcomes import (
    SUBMISSION_EVIDENCE_KINDS,
    ApplicationOutcome,
    EvidenceRef,
    utc_now,
)


LEDGER_SCHEMA_VERSION = 1
ACTIVE_SUBMISSION_STATUSES = ("PENDING", "SUBMITTING", "UNKNOWN", "VERIFIED")


class LedgerError(RuntimeError):
    pass


class RunAlreadyExistsError(LedgerError):
    pass


class RunNotFoundError(LedgerError):
    pass


class StateConflictError(LedgerError):
    pass


class DuplicateSubmissionError(LedgerError):
    def __init__(self, message: str, existing: "SubmissionIntent | None" = None):
        super().__init__(message)
        self.existing = existing


class SubmissionStateError(LedgerError):
    pass


class PermitAlreadyConsumedError(LedgerError):
    pass


class SubmissionStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    UNKNOWN = "UNKNOWN"
    VERIFIED = "VERIFIED"
    ABORTED = "ABORTED"


@dataclass(frozen=True, slots=True)
class EventRecord:
    sequence: int
    event_id: str
    run_id: str
    job_id: str
    event_type: str
    payload: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    job_id: str
    state: str
    state_version: int
    metadata: Mapping[str, Any]
    outcome: Mapping[str, Any] | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SubmissionIntent:
    intent_id: str
    run_id: str
    job_id: str
    application_key: str
    idempotency_key: str
    job_url_hash: str
    material_hash: str
    answer_hash: str
    review_hash: str
    policy_hash: str
    status: SubmissionStatus
    created_at: str
    updated_at: str

    def to_safe_dict(self) -> dict[str, str]:
        """Return a projection safe for outcomes, logs, and user handoff."""

        return {
            "intent_id": self.intent_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "job_url_hash": self.job_url_hash,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class SubmissionEvidence:
    evidence_id: str
    intent_id: str
    kind: str
    uri: str | None
    sha256: str | None
    metadata: Mapping[str, Any]
    observed_at: str


def _canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_parts(*parts: str) -> str:
    encoded = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_NATIVE_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{0,255}", re.IGNORECASE)
_WORKDAY_ID = re.compile(
    r"(?:\d+|(?:jr|r|req|job)[_-]?[a-z0-9._-]*\d[a-z0-9._-]*)",
    re.IGNORECASE,
)


def _safe_native_token(value: str) -> str:
    token = unquote(str(value or "")).strip().casefold()
    if token in {"", ".", ".."} or _NATIVE_TOKEN.fullmatch(token) is None:
        return ""
    return token


def _query_native_token(parsed: Any, *names: str) -> str:
    accepted = {name.casefold() for name in names}
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if key.casefold() in accepted:
            token = _safe_native_token(value)
            if token:
                return token
    return ""


def _native_ats_posting_identity(parsed: Any) -> str:
    """Return a tenant-scoped native ATS identity when extraction is safe."""

    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port not in {None, 80, 443}:
        return ""
    host = parsed.hostname.casefold().rstrip(".")
    raw_segments = [unquote(item) for item in parsed.path.split("/") if item]
    segments = [_safe_native_token(item) for item in raw_segments]
    lowered = [item.casefold() for item in raw_segments]

    greenhouse = re.fullmatch(
        r"(?P<board>job-boards|boards)(?P<eu>\.eu)?\.greenhouse\.io",
        host,
    )
    if greenhouse:
        region = "eu" if greenhouse.group("eu") else "us"
        tenant = posting = ""
        if len(segments) >= 3 and lowered[1] == "jobs":
            tenant, posting = segments[0], segments[2]
        elif lowered[:1] == ["embed"]:
            tenant = _query_native_token(parsed, "for")
            posting = _query_native_token(parsed, "token")
        if tenant and posting:
            return f"ats:greenhouse:{region}:{tenant}:{posting}"

    if host in {"boards-api.greenhouse.io", "boards-api.eu.greenhouse.io"}:
        region = "eu" if ".eu." in host else "us"
        if (
            len(segments) >= 5
            and lowered[0] == "v1"
            and lowered[1] == "boards"
            and lowered[3] == "jobs"
            and segments[2]
            and segments[4]
        ):
            return f"ats:greenhouse:{region}:{segments[2]}:{segments[4]}"

    lever_jobs_hosts = {"jobs.lever.co", "jobs.eu.lever.co"}
    lever_api_hosts = {"api.lever.co", "api.eu.lever.co"}
    if host in lever_jobs_hosts and len(segments) >= 2:
        region = "eu" if ".eu." in host else "us"
        if segments[0] and segments[1]:
            return f"ats:lever:{region}:{segments[0]}:{segments[1]}"
    if host in lever_api_hosts:
        region = "eu" if ".eu." in host else "us"
        if (
            len(segments) >= 4
            and lowered[0] == "v0"
            and lowered[1] == "postings"
            and segments[2]
            and segments[3]
        ):
            return f"ats:lever:{region}:{segments[2]}:{segments[3]}"

    ashby_hosts = {"jobs.ashbyhq.com", "jobs.eu.ashbyhq.com"}
    if host in ashby_hosts and len(segments) >= 2:
        region = "eu" if ".eu." in host else "us"
        if segments[0] and segments[1]:
            return f"ats:ashby:{region}:{segments[0]}:{segments[1]}"

    if host in {"jobs.jobvite.com", "apply.jobvite.com"}:
        try:
            job_index = lowered.index("job")
        except ValueError:
            job_index = -1
        if job_index == 1 and len(segments) > job_index + 1:
            tenant = segments[0]
            posting = segments[job_index + 1]
            if tenant and posting:
                return f"ats:jobvite:{tenant}:{posting}"

    if host.endswith(".myworkdayjobs.com"):
        labels = host.split(".")
        tenant = _safe_native_token(labels[0]) if len(labels) >= 3 else ""
        if tenant and re.fullmatch(r"wd\d+", tenant) is None:
            requisition = _query_native_token(
                parsed,
                "jobid",
                "job_id",
                "requisitionid",
                "requisition_id",
                "jobreqid",
            )
            try:
                job_index = lowered.index("job")
            except ValueError:
                job_index = -1
            posting_segments: list[str] = []
            if job_index >= 0:
                stage_tokens = {
                    "apply",
                    "autofillwithresume",
                    "myinformation",
                    "myexperience",
                    "applicationquestions",
                    "voluntarydisclosures",
                    "selfidentify",
                    "review",
                    "confirmation",
                }
                for raw, safe in zip(
                    lowered[job_index + 1 :],
                    segments[job_index + 1 :],
                    strict=True,
                ):
                    if raw.replace("-", "").replace("_", "") in stage_tokens:
                        break
                    if safe:
                        posting_segments.append(safe)
            if not requisition and posting_segments:
                final = posting_segments[-1]
                suffix = final.rsplit("_", 1)[-1] if "_" in final else ""
                if suffix and any(char.isdigit() for char in suffix):
                    requisition = suffix
                elif len(posting_segments) == 1 or _WORKDAY_ID.fullmatch(final):
                    requisition = final
            if requisition:
                return f"ats:workday:{tenant}:{requisition}"
    return ""


def _canonical_url_identity(url: str) -> str:
    """Canonicalize a URL without guessing an identity from arbitrary sites."""

    if not url.strip():
        raise ValueError("job URL is required")
    parsed = urlsplit(url.strip())
    tracking_keys = {
        "source",
        "src",
        "ref",
        "referrer",
        "referral",
        "gh_src",
        "lever-source",
        "ashby_jid_source",
        "__jvst",
        "__jvsd",
        "trackingid",
        "trk",
    }
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold()
        if normalized_key.startswith("utm_") or normalized_key in tracking_keys:
            continue
        query.append((key, value))
    host = parsed.hostname.casefold() if parsed.hostname else parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            host,
            parsed.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def _native_token_spellings(parsed: Any, normalized: str) -> tuple[str, ...]:
    """Retain the current URL spelling while also covering normalized keys."""

    values = {normalized}
    for raw in (unquote(item) for item in parsed.path.split("/") if item):
        if _safe_native_token(raw) == normalized:
            values.add(raw)
    for _key, raw in parse_qsl(parsed.query, keep_blank_values=False):
        if _safe_native_token(raw) == normalized:
            values.add(raw)
    return tuple(sorted(values))


def _legacy_ats_url_identities(url: str) -> tuple[str, ...]:
    """Return reconstructable pre-native-key URL identities for a known ATS.

    Earlier ledgers used a canonical URL hash as ``application_key``.  Once an
    ATS gains a native tenant/posting key, checking only the currently supplied
    URL would allow an old intent created through another hosted alias to be
    missed.  These aliases are deliberately limited to URL shapes whose tenant
    and posting identity are explicit and already accepted by
    ``_native_ats_posting_identity``.

    Workday embeds mutable site/title segments in many posting paths.  Those
    segments cannot be recovered from a one-way historical hash, so this covers
    every safe alias reconstructable from the current URL (host shard, scheme,
    stage suffix, and query-key forms) without guessing private or unrelated
    paths.
    """

    parsed = urlsplit(str(url).strip())
    native = _native_ats_posting_identity(parsed)
    if not native:
        return ()
    parts = native.split(":")
    candidates: set[str] = set()

    def add(scheme: str, host: str, path: str, query: str = "") -> None:
        candidate = urlunsplit((scheme, host, path, query, ""))
        candidates.add(_canonical_url_identity(candidate))

    schemes = ("http", "https")
    platform = parts[1]
    if platform in {"greenhouse", "lever", "ashby"} and len(parts) == 5:
        region, tenant, posting = parts[2], parts[3], parts[4]
        tenants = _native_token_spellings(parsed, tenant)
        postings = _native_token_spellings(parsed, posting)
        if platform == "greenhouse":
            suffix = ".eu" if region == "eu" else ""
            for scheme in schemes:
                for tenant_value in tenants:
                    for posting_value in postings:
                        tenant_path = quote(tenant_value, safe="._-")
                        posting_path = quote(posting_value, safe="._-")
                        for host in (
                            f"boards{suffix}.greenhouse.io",
                            f"job-boards{suffix}.greenhouse.io",
                        ):
                            add(scheme, host, f"/{tenant_path}/jobs/{posting_path}")
                            query = urlencode(
                                (("for", tenant_value), ("token", posting_value))
                            )
                            add(scheme, host, "/embed", query)
                            add(scheme, host, "/embed/job_app", query)
                        add(
                            scheme,
                            f"boards-api{suffix}.greenhouse.io",
                            f"/v1/boards/{tenant_path}/jobs/{posting_path}",
                        )
        elif platform == "lever":
            suffix = ".eu" if region == "eu" else ""
            for scheme in schemes:
                for tenant_value in tenants:
                    for posting_value in postings:
                        tenant_path = quote(tenant_value, safe="._-")
                        posting_path = quote(posting_value, safe="._-")
                        base = f"/{tenant_path}/{posting_path}"
                        add(scheme, f"jobs{suffix}.lever.co", base)
                        add(scheme, f"jobs{suffix}.lever.co", base + "/apply")
                        add(
                            scheme,
                            f"api{suffix}.lever.co",
                            f"/v0/postings/{tenant_path}/{posting_path}",
                        )
        else:
            suffix = ".eu" if region == "eu" else ""
            for scheme in schemes:
                for tenant_value in tenants:
                    for posting_value in postings:
                        base = (
                            f"/{quote(tenant_value, safe='._-')}/"
                            f"{quote(posting_value, safe='._-')}"
                        )
                        add(scheme, f"jobs{suffix}.ashbyhq.com", base)
                        add(scheme, f"jobs{suffix}.ashbyhq.com", base + "/application")
    elif platform == "jobvite" and len(parts) == 4:
        tenant, posting = parts[2], parts[3]
        for scheme in schemes:
            for tenant_value in _native_token_spellings(parsed, tenant):
                for posting_value in _native_token_spellings(parsed, posting):
                    base = (
                        f"/{quote(tenant_value, safe='._-')}/job/"
                        f"{quote(posting_value, safe='._-')}"
                    )
                    for host in ("jobs.jobvite.com", "apply.jobvite.com"):
                        add(scheme, host, base)
                        add(scheme, host, base + "/apply")
    elif platform == "workday" and len(parts) == 4:
        tenant, requisition = parts[2], parts[3]
        host_variants = {f"{tenant}.myworkdayjobs.com"}
        host_variants.update(
            f"{tenant}.wd{shard}.myworkdayjobs.com" for shard in range(1, 13)
        )
        current_host = (parsed.hostname or "").casefold().rstrip(".")
        if current_host:
            host_variants.add(current_host)

        raw_segments = [item for item in parsed.path.split("/") if item]
        compact_stage_tokens = {
            "apply",
            "autofillwithresume",
            "myinformation",
            "myexperience",
            "applicationquestions",
            "voluntarydisclosures",
            "selfidentify",
            "review",
            "confirmation",
        }
        base_segments: list[str] = []
        for segment in raw_segments:
            if segment.casefold().replace("-", "").replace("_", "") in compact_stage_tokens:
                break
            base_segments.append(segment)
        base_path = "/" + "/".join(base_segments) if base_segments else "/"
        path_variants = {base_path, parsed.path.rstrip("/") or "/"}
        if base_path != "/":
            path_variants.update(
                {
                    base_path + "/apply",
                    base_path + "/review",
                    base_path + "/confirmation",
                }
            )
        for spelling in _native_token_spellings(parsed, requisition):
            encoded = quote(spelling, safe="._-")
            path_variants.update({f"/job/{encoded}", f"/job/{encoded}/apply"})
        for scheme in schemes:
            for host in host_variants:
                for path in path_variants:
                    add(scheme, host, path)
                for key in (
                    "jobId",
                    "job_id",
                    "requisitionId",
                    "requisition_id",
                    "jobReqId",
                ):
                    add(scheme, host, "/", urlencode(((key, requisition),)))

    return tuple(sorted(candidates))


def hash_job_url(url: str) -> str:
    """Hash a conservative, durable posting identity.

    Greenhouse, Lever, Ashby, Jobvite, and Workday URLs use tenant-scoped native posting
    keys only for the explicitly recognized hosted URL shapes above.  This
    unifies their apply/referral paths and known host aliases.  Every other URL
    remains scoped to its exact normalized scheme, host, path, and non-tracking
    query; Jobops deliberately does not guess across arbitrary career domains.
    """

    if not str(url).strip():
        raise ValueError("job URL is required")
    parsed = urlsplit(str(url).strip())
    identity = _native_ats_posting_identity(parsed) or _canonical_url_identity(url)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _job_url_hash_candidates(url: str) -> tuple[str, ...]:
    """Include every safe, reconstructable pre-native-key ATS URL hash."""

    identities = {
        _native_ats_posting_identity(urlsplit(str(url).strip()))
        or _canonical_url_identity(url),
        _canonical_url_identity(url),
        *_legacy_ats_url_identities(url),
    }
    return tuple(
        sorted(
            hashlib.sha256(identity.encode("utf-8")).hexdigest()
            for identity in identities
            if identity
        )
    )


class EventLedger:
    """SQLite-backed event stream and mutable state projections."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0):
        self.path = Path(path).expanduser().resolve()
        self.timeout_seconds = timeout_seconds
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            self.path.parent.chmod(0o700)
        self._initialize()
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout_seconds * 1000)}")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_run_sequence
                    ON events(run_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_job_sequence
                    ON events(job_id, sequence);

                CREATE TRIGGER IF NOT EXISTS events_are_append_only_update
                BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS events_are_append_only_delete
                BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'events are append-only');
                END;

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL,
                    outcome_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_job ON runs(job_id);

                CREATE TABLE IF NOT EXISTS submission_intents (
                    intent_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    application_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    job_url_hash TEXT NOT NULL,
                    material_hash TEXT NOT NULL,
                    answer_hash TEXT NOT NULL,
                    review_hash TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('PENDING', 'SUBMITTING', 'UNKNOWN', 'VERIFIED', 'ABORTED')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_submission_per_application
                    ON submission_intents(application_key)
                    WHERE status IN ('PENDING', 'SUBMITTING', 'UNKNOWN', 'VERIFIED');

                CREATE TABLE IF NOT EXISTS submission_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    uri TEXT,
                    sha256 TEXT,
                    metadata_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    FOREIGN KEY(intent_id) REFERENCES submission_intents(intent_id)
                );
                CREATE INDEX IF NOT EXISTS idx_submission_evidence_intent
                    ON submission_evidence(intent_id);

                CREATE TABLE IF NOT EXISTS permit_consumptions (
                    jti TEXT PRIMARY KEY,
                    gate TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    token_digest TEXT NOT NULL,
                    bindings_digest TEXT NOT NULL,
                    claims_json TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS leases (
                    resource TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    acquired_at REAL NOT NULL,
                    renewed_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )
            existing = connection.execute(
                "SELECT value FROM ledger_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO ledger_meta(key, value) VALUES ('schema_version', ?)",
                    (str(LEDGER_SCHEMA_VERSION),),
                )
            elif int(existing["value"]) != LEDGER_SCHEMA_VERSION:
                raise LedgerError(
                    f"unsupported ledger schema version {existing['value']}"
                )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> EventRecord:
        event_id = str(uuid.uuid4())
        timestamp = created_at or utc_now()
        event_payload = dict(payload or {})
        cursor = connection.execute(
            """
            INSERT INTO events(event_id, run_id, job_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                job_id,
                event_type,
                _canonical_json(event_payload),
                timestamp,
            ),
        )
        return EventRecord(
            sequence=int(cursor.lastrowid),
            event_id=event_id,
            run_id=run_id,
            job_id=job_id,
            event_type=event_type,
            payload=event_payload,
            created_at=timestamp,
        )

    def append_event(
        self,
        *,
        run_id: str,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        with self.transaction() as connection:
            return self._insert_event(
                connection,
                run_id=run_id,
                job_id=job_id,
                event_type=event_type,
                payload=payload,
            )

    def create_run(
        self,
        *,
        run_id: str,
        job_id: str,
        initial_state: str = "QUEUED",
        metadata: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        timestamp = utc_now()
        run_metadata = dict(metadata or {})
        with self.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, job_id, state, state_version, metadata_json,
                        outcome_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 0, ?, NULL, ?, ?)
                    """,
                    (
                        run_id,
                        job_id,
                        str(initial_state),
                        _canonical_json(run_metadata),
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RunAlreadyExistsError(run_id) from exc
            self._insert_event(
                connection,
                run_id=run_id,
                job_id=job_id,
                event_type="RUN_CREATED",
                payload={"state": str(initial_state), "metadata": run_metadata},
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(row)

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            job_id=row["job_id"],
            state=row["state"],
            state_version=int(row["state_version"]),
            metadata=json.loads(row["metadata_json"]),
            outcome=json.loads(row["outcome_json"]) if row["outcome_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(run_id)
        return self._run_from_row(row)

    def compare_and_set_state(
        self,
        *,
        run_id: str,
        expected_version: int,
        new_state: str,
        outcome: ApplicationOutcome | Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        timestamp = utc_now()
        if isinstance(outcome, ApplicationOutcome):
            outcome_value: Mapping[str, Any] | None = outcome.to_dict()
        else:
            outcome_value = outcome
        outcome_json = _canonical_json(outcome_value) if outcome_value is not None else None
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT job_id, state FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current is None:
                raise RunNotFoundError(run_id)
            cursor = connection.execute(
                """
                UPDATE runs
                SET state = ?, state_version = state_version + 1,
                    outcome_json = COALESCE(?, outcome_json), updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (str(new_state), outcome_json, timestamp, run_id, expected_version),
            )
            if cursor.rowcount != 1:
                actual = connection.execute(
                    "SELECT state_version FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()["state_version"]
                raise StateConflictError(
                    f"run {run_id} expected version {expected_version}, found {actual}"
                )
            event_payload = {
                "from": current["state"],
                "to": str(new_state),
                "expected_version": expected_version,
                "new_version": expected_version + 1,
                **dict(payload or {}),
            }
            if outcome_value is not None:
                event_payload["outcome"] = dict(outcome_value)
            self._insert_event(
                connection,
                run_id=run_id,
                job_id=current["job_id"],
                event_type="RUN_STATE_CHANGED",
                payload=event_payload,
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(row)

    def list_events(
        self, *, run_id: str | None = None, job_id: str | None = None
    ) -> list[EventRecord]:
        clauses: list[str] = []
        parameters: list[str] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        if job_id is not None:
            clauses.append("job_id = ?")
            parameters.append(job_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM events{where} ORDER BY sequence", parameters
            ).fetchall()
        return [
            EventRecord(
                sequence=int(row["sequence"]),
                event_id=row["event_id"],
                run_id=row["run_id"],
                job_id=row["job_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _intent_from_row(row: sqlite3.Row) -> SubmissionIntent:
        return SubmissionIntent(
            intent_id=row["intent_id"],
            run_id=row["run_id"],
            job_id=row["job_id"],
            application_key=row["application_key"],
            idempotency_key=row["idempotency_key"],
            job_url_hash=row["job_url_hash"],
            material_hash=row["material_hash"],
            answer_hash=row["answer_hash"],
            review_hash=row["review_hash"],
            policy_hash=row["policy_hash"],
            status=SubmissionStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_submission_intent(
        self,
        *,
        run_id: str,
        job_id: str,
        job_url: str,
        material_hash: str,
        answer_hash: str,
        review_hash: str,
        policy_hash: str,
        allow_existing_same: bool = True,
    ) -> SubmissionIntent:
        url_hash = hash_job_url(job_url)
        identity_hashes = _job_url_hash_candidates(job_url)
        # The canonical posting URL is the durable application identity.  Human-
        # editable company/title metadata and caller-provided job IDs must not
        # create a duplicate-submission escape hatch.
        application_key = url_hash
        idempotency_key = _hash_parts(
            application_key,
            material_hash,
            answer_hash,
            review_hash,
            policy_hash,
        )
        timestamp = utc_now()
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT job_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RunNotFoundError(run_id)
            if run["job_id"] != job_id:
                raise LedgerError("run job_id does not match submission job_id")
            identity_placeholders = ", ".join("?" for _ in identity_hashes)
            existing_row = connection.execute(
                f"""
                SELECT * FROM submission_intents
                WHERE application_key IN ({identity_placeholders})
                  AND status IN ('PENDING', 'SUBMITTING', 'UNKNOWN', 'VERIFIED')
                """,
                identity_hashes,
            ).fetchone()
            if existing_row is not None:
                existing = self._intent_from_row(existing_row)
                if (
                    allow_existing_same
                    and existing.run_id == run_id
                    and existing.idempotency_key == idempotency_key
                ):
                    return existing
                raise DuplicateSubmissionError(
                    f"active or verified submission already exists for job {job_id}",
                    existing,
                )
            intent_id = str(uuid.uuid4())
            try:
                connection.execute(
                    """
                    INSERT INTO submission_intents(
                        intent_id, run_id, job_id, application_key, idempotency_key,
                        job_url_hash, material_hash, answer_hash, review_hash,
                        policy_hash, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                    """,
                    (
                        intent_id,
                        run_id,
                        job_id,
                        application_key,
                        idempotency_key,
                        url_hash,
                        material_hash,
                        answer_hash,
                        review_hash,
                        policy_hash,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateSubmissionError(
                    f"concurrent submission reservation for job {job_id} was rejected"
                ) from exc
            self._insert_event(
                connection,
                run_id=run_id,
                job_id=job_id,
                event_type="SUBMISSION_INTENT_CREATED",
                payload={
                    "intent_id": intent_id,
                    "application_key": application_key,
                    "idempotency_key": idempotency_key,
                },
                created_at=timestamp,
            )
            row = connection.execute(
                "SELECT * FROM submission_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return self._intent_from_row(row)

    def get_submission_intent(self, intent_id: str) -> SubmissionIntent:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM submission_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        if row is None:
            raise LedgerError(f"submission intent not found: {intent_id}")
        return self._intent_from_row(row)

    def find_submission_intent_for_url(
        self,
        job_url: str,
        *,
        statuses: Sequence[SubmissionStatus] = (SubmissionStatus.UNKNOWN,),
    ) -> SubmissionIntent | None:
        """Find a durable intent by canonical posting URL without mutating it."""

        normalized_statuses = tuple(SubmissionStatus(status) for status in statuses)
        if not normalized_statuses:
            return None
        placeholders = ", ".join("?" for _ in normalized_statuses)
        identity_hashes = _job_url_hash_candidates(job_url)
        identity_placeholders = ", ".join("?" for _ in identity_hashes)
        parameters = (*identity_hashes, *(status.value for status in normalized_statuses))
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM submission_intents
                WHERE application_key IN ({identity_placeholders})
                  AND status IN ({placeholders})
                ORDER BY created_at DESC, intent_id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
        return self._intent_from_row(row) if row is not None else None

    def transition_submission_intent(
        self,
        *,
        intent_id: str,
        expected_status: SubmissionStatus,
        new_status: SubmissionStatus,
    ) -> SubmissionIntent:
        expected = SubmissionStatus(expected_status)
        target = SubmissionStatus(new_status)
        allowed = {
            SubmissionStatus.PENDING: {
                SubmissionStatus.SUBMITTING,
                SubmissionStatus.ABORTED,
            },
            SubmissionStatus.SUBMITTING: {
                SubmissionStatus.UNKNOWN,
                SubmissionStatus.ABORTED,
            },
            SubmissionStatus.UNKNOWN: {SubmissionStatus.ABORTED},
        }
        if target not in allowed.get(expected, set()):
            raise SubmissionStateError(f"invalid submission transition {expected} -> {target}")
        timestamp = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM submission_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"submission intent not found: {intent_id}")
            if row["status"] != expected.value:
                raise SubmissionStateError(
                    f"intent {intent_id} expected {expected}, found {row['status']}"
                )
            connection.execute(
                "UPDATE submission_intents SET status = ?, updated_at = ? WHERE intent_id = ?",
                (target.value, timestamp, intent_id),
            )
            self._insert_event(
                connection,
                run_id=row["run_id"],
                job_id=row["job_id"],
                event_type=f"SUBMISSION_{target.value}",
                payload={"intent_id": intent_id, "from": expected.value},
                created_at=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM submission_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return self._intent_from_row(updated)

    def mark_submission_started(self, intent_id: str) -> SubmissionIntent:
        return self.transition_submission_intent(
            intent_id=intent_id,
            expected_status=SubmissionStatus.PENDING,
            new_status=SubmissionStatus.SUBMITTING,
        )

    def mark_submission_unknown(self, intent_id: str) -> SubmissionIntent:
        return self.transition_submission_intent(
            intent_id=intent_id,
            expected_status=SubmissionStatus.SUBMITTING,
            new_status=SubmissionStatus.UNKNOWN,
        )

    def mark_submission_verified(
        self, *, intent_id: str, evidence: EvidenceRef
    ) -> SubmissionIntent:
        if not isinstance(evidence, EvidenceRef):
            raise TypeError("evidence must be an EvidenceRef")
        if evidence.kind not in SUBMISSION_EVIDENCE_KINDS:
            raise SubmissionStateError(
                f"ineligible submission evidence kind: {evidence.kind.value}"
            )
        timestamp = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM submission_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"submission intent not found: {intent_id}")
            if row["status"] not in {
                SubmissionStatus.SUBMITTING.value,
                SubmissionStatus.UNKNOWN.value,
            }:
                raise SubmissionStateError(
                    f"cannot verify submission from status {row['status']}"
                )
            evidence_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO submission_evidence(
                    evidence_id, intent_id, kind, uri, sha256, metadata_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    intent_id,
                    evidence.kind.value,
                    evidence.uri,
                    evidence.sha256,
                    _canonical_json(dict(evidence.metadata)),
                    evidence.observed_at,
                ),
            )
            connection.execute(
                """
                UPDATE submission_intents
                SET status = 'VERIFIED', updated_at = ?
                WHERE intent_id = ?
                """,
                (timestamp, intent_id),
            )
            self._insert_event(
                connection,
                run_id=row["run_id"],
                job_id=row["job_id"],
                event_type="SUBMISSION_VERIFIED",
                payload={
                    "intent_id": intent_id,
                    "evidence_id": evidence_id,
                    "evidence_kind": evidence.kind.value,
                },
                created_at=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM submission_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return self._intent_from_row(updated)

    def list_submission_evidence(self, intent_id: str) -> list[SubmissionEvidence]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM submission_evidence
                WHERE intent_id = ? ORDER BY observed_at, evidence_id
                """,
                (intent_id,),
            ).fetchall()
        return [
            SubmissionEvidence(
                evidence_id=row["evidence_id"],
                intent_id=row["intent_id"],
                kind=row["kind"],
                uri=row["uri"],
                sha256=row["sha256"],
                metadata=json.loads(row["metadata_json"]),
                observed_at=row["observed_at"],
            )
            for row in rows
        ]

    def consume_permit(
        self,
        *,
        jti: str,
        gate: str,
        run_id: str,
        job_id: str,
        token_digest: str,
        bindings_digest: str,
        claims: Mapping[str, Any],
    ) -> None:
        timestamp = utc_now()
        with self.transaction() as connection:
            run = connection.execute(
                "SELECT job_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise RunNotFoundError(run_id)
            if run["job_id"] != job_id:
                raise LedgerError("permit job_id does not match its run")
            try:
                connection.execute(
                    """
                    INSERT INTO permit_consumptions(
                        jti, gate, run_id, job_id, token_digest, bindings_digest,
                        claims_json, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        jti,
                        gate,
                        run_id,
                        job_id,
                        token_digest,
                        bindings_digest,
                        _canonical_json(dict(claims)),
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PermitAlreadyConsumedError(jti) from exc
            self._insert_event(
                connection,
                run_id=run_id,
                job_id=job_id,
                event_type=f"{gate}_PERMIT_CONSUMED",
                payload={"jti": jti, "bindings_digest": bindings_digest},
                created_at=timestamp,
            )

    def get_permit_consumption(self, jti: str) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM permit_consumptions WHERE jti = ?", (jti,)
            ).fetchone()
        if row is None:
            return None
        return {
            "jti": row["jti"],
            "gate": row["gate"],
            "run_id": row["run_id"],
            "job_id": row["job_id"],
            "token_digest": row["token_digest"],
            "bindings_digest": row["bindings_digest"],
            "claims": json.loads(row["claims_json"]),
            "consumed_at": row["consumed_at"],
        }


__all__ = [
    "ACTIVE_SUBMISSION_STATUSES",
    "DuplicateSubmissionError",
    "EventLedger",
    "EventRecord",
    "LedgerError",
    "PermitAlreadyConsumedError",
    "RunAlreadyExistsError",
    "RunNotFoundError",
    "RunRecord",
    "StateConflictError",
    "SubmissionEvidence",
    "SubmissionIntent",
    "SubmissionStateError",
    "SubmissionStatus",
    "hash_job_url",
]
