"""Single-worker control-plane boundary for one real ATS application.

The Kubernetes process owns approval, the one-time permit, remote task lease,
submission intent, and audit projection.  It never owns a browser profile,
candidate document bytes, ATS credentials, cookies, or MFA material.  The
local executor owns those resources and may click Submit only after the final
fence below has atomically consumed Gate B and entered SUBMITTING.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from adapters.workday import _same_workday_session_url, workday_external_job_id
from auth.workday_hosts import is_trusted_workday_host
from core.bundles import canonical_hash, normalized_job_url
from core.event_ledger import (
    DuplicateSubmissionError,
    EventLedger,
    RunAlreadyExistsError,
    SubmissionIntent,
    SubmissionStatus,
    hash_job_url,
)
from core.leases import (
    Lease,
    LeaseError,
    LeaseManager,
    LeaseUnavailableError,
)
from core.outcomes import (
    SUBMISSION_EVIDENCE_KINDS,
    ApplicationOutcome,
    EvidenceRef,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.permits import (
    PermitBindings,
    PermitError,
    PermitGate,
    PermitService,
    hash_value,
)


REAL_APPLICATION_CONTROL_CONTRACT_VERSION = "real-application-control-v1"
REAL_APPLICATION_TASK_LEASE_TTL_SECONDS = 90
REAL_APPLICATION_PERMIT_TTL_SECONDS = 300
WORKER_SESSION_TTL_SECONDS = 2_592_000
WORKER_ONLINE_WINDOW_SECONDS = 30
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class RealApplicationControlError(RuntimeError):
    pass


class RealApplicationConflictError(RealApplicationControlError):
    pass


class RealApplicationNotAuthorizedError(RealApplicationControlError):
    pass


class RealApplicationTaskStatus(StrEnum):
    PREPARED = "PREPARED"
    CLAIMED = "CLAIMED"
    HUMAN_INTERVENTION_REQUIRED = "HUMAN_INTERVENTION_REQUIRED"
    REVIEW_READY = "REVIEW_READY"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    CONFIRMED = "CONFIRMED"
    SUBMISSION_OUTCOME_UNKNOWN = "SUBMISSION_OUTCOME_UNKNOWN"
    FAILED = "FAILED"


class RealApplicationExecutorStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"


def _clean(name: str, value: Any, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    result = " ".join(value.strip().split())
    if not result or len(result) > maximum or any(ord(char) < 32 for char in result):
        raise ValueError(f"{name} is invalid")
    return result


def _require_id(name: str, value: Any) -> str:
    result = _clean(name, value)
    if _ID_RE.fullmatch(result) is None:
        raise ValueError(f"{name} is invalid")
    return result


def _require_hash(name: str, value: Any) -> str:
    result = str(value or "")
    if _HASH_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _json_object(name: str, value: Any, *, maximum_bytes: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    copied = json.loads(_canonical_json(dict(value)))
    if len(_canonical_json(copied).encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{name} is too large")
    return copied


@dataclass(frozen=True, slots=True)
class RealApplicationPreparation:
    attempt_id: str
    subject_id: str
    application_plan_id: str
    assembly_record_id: str
    assembly_record_content_hash: str
    job_id: str
    external_job_id: str
    company: str
    title: str
    provider: str
    canonical_job_url: str
    bundle_canonical_hash: str
    profile_snapshot_hash: str
    answer_hash: str
    answer_bundle_hash: str
    material_hash: str
    resume_sha256: str
    cover_letter_sha256: str
    policy_hash: str
    answer_bundle: Mapping[str, Any]
    contract_version: str = REAL_APPLICATION_CONTROL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REAL_APPLICATION_CONTROL_CONTRACT_VERSION:
            raise ValueError("real application preparation version is unsupported")
        for name in (
            "attempt_id",
            "subject_id",
            "application_plan_id",
            "assembly_record_id",
            "job_id",
            "external_job_id",
        ):
            object.__setattr__(self, name, _require_id(name, getattr(self, name)))
        provider = _clean("provider", self.provider, maximum=40).casefold()
        if provider != "workday":
            raise ValueError("the first real Golden Path supports Workday only")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "company", _clean("company", self.company))
        object.__setattr__(self, "title", _clean("title", self.title))
        url = normalized_job_url(self.canonical_job_url)
        host = (urlsplit(url).hostname or "").casefold()
        if not is_trusted_workday_host(host):
            raise ValueError("canonical job URL is not a trusted Workday host")
        if workday_external_job_id(url).casefold() != self.external_job_id.casefold():
            raise ValueError("external job ID does not match the canonical Workday URL")
        object.__setattr__(self, "canonical_job_url", url)
        for name in (
            "bundle_canonical_hash",
            "assembly_record_content_hash",
            "profile_snapshot_hash",
            "answer_hash",
            "answer_bundle_hash",
            "material_hash",
            "resume_sha256",
            "cover_letter_sha256",
            "policy_hash",
        ):
            object.__setattr__(self, name, _require_hash(name, getattr(self, name)))
        answer_bundle = _json_object(
            "answer_bundle", self.answer_bundle, maximum_bytes=512_000
        )
        if canonical_hash(answer_bundle) != self.answer_bundle_hash:
            raise ValueError("answer bundle hash does not match its content")
        object.__setattr__(self, "answer_bundle", answer_bundle)

    @property
    def ats_host(self) -> str:
        return (urlsplit(self.canonical_job_url).hostname or "").casefold()

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "assembly_record_id": self.assembly_record_id,
            "assembly_record_content_hash": self.assembly_record_content_hash,
            "attempt_id": self.attempt_id,
            "bundle_canonical_hash": self.bundle_canonical_hash,
            "company": self.company,
            "external_job_id": self.external_job_id,
            "job_id": self.job_id,
            "provider": self.provider,
            "title": self.title,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_bundle": dict(self.answer_bundle),
            "answer_bundle_hash": self.answer_bundle_hash,
            "answer_hash": self.answer_hash,
            "application_plan_id": self.application_plan_id,
            "assembly_record_content_hash": self.assembly_record_content_hash,
            "assembly_record_id": self.assembly_record_id,
            "attempt_id": self.attempt_id,
            "bundle_canonical_hash": self.bundle_canonical_hash,
            "canonical_job_url": self.canonical_job_url,
            "company": self.company,
            "contract_version": self.contract_version,
            "cover_letter_sha256": self.cover_letter_sha256,
            "external_job_id": self.external_job_id,
            "job_id": self.job_id,
            "material_hash": self.material_hash,
            "policy_hash": self.policy_hash,
            "profile_snapshot_hash": self.profile_snapshot_hash,
            "provider": self.provider,
            "resume_sha256": self.resume_sha256,
            "subject_id": self.subject_id,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class WorkerEnrollment:
    worker_id: str
    session_secret: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class ClaimedRealApplicationTask:
    task: Mapping[str, Any]
    lease: Lease


class RealApplicationControlPlane:
    """Persist one-subject real-application coordination beside EventLedger."""

    def __init__(
        self,
        *,
        ledger: EventLedger,
        permit_service: PermitService,
        subject_id: str,
        enrollment_secret: str,
        clock: Callable[[], float] = time.time,
        task_lease_ttl_seconds: int = REAL_APPLICATION_TASK_LEASE_TTL_SECONDS,
        permit_ttl_seconds: int = REAL_APPLICATION_PERMIT_TTL_SECONDS,
    ) -> None:
        if not isinstance(ledger, EventLedger):
            raise TypeError("ledger must be an EventLedger")
        if not isinstance(permit_service, PermitService):
            raise TypeError("permit_service must be a PermitService")
        self.ledger = ledger
        self.permit_service = permit_service
        self.subject_id = _require_id("subject_id", subject_id)
        self._enrollment_hash = hash_value(_clean(
            "enrollment_secret", enrollment_secret, maximum=1024
        ))
        self.clock = clock
        if not 30 <= task_lease_ttl_seconds <= 600:
            raise ValueError("task lease TTL is outside policy")
        if not 30 <= permit_ttl_seconds <= 600:
            raise ValueError("permit TTL is outside policy")
        self.task_lease_ttl_seconds = int(task_lease_ttl_seconds)
        self.permit_ttl_seconds = int(permit_ttl_seconds)
        self.leases = LeaseManager(ledger, clock=clock)
        self._active_permits: dict[str, str] = {}
        self._initialize()

    def _initialize(self) -> None:
        with self.ledger._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS real_worker_enrollment (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    worker_id TEXT NOT NULL,
                    enrolled_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS real_worker_sessions (
                    worker_id TEXT PRIMARY KEY,
                    session_hash TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_heartbeat_at REAL NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS real_application_tasks (
                    attempt_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    application_plan_id TEXT NOT NULL,
                    assembly_record_id TEXT NOT NULL,
                    assembly_record_content_hash TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    external_job_id TEXT NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    ats_host TEXT NOT NULL,
                    canonical_job_url TEXT NOT NULL,
                    bundle_canonical_hash TEXT NOT NULL,
                    profile_snapshot_hash TEXT NOT NULL,
                    answer_hash TEXT NOT NULL,
                    answer_bundle_hash TEXT NOT NULL,
                    material_hash TEXT NOT NULL,
                    resume_sha256 TEXT NOT NULL,
                    cover_letter_sha256 TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    answer_bundle_json TEXT NOT NULL,
                    review_hash TEXT NOT NULL DEFAULT '',
                    review_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    worker_id TEXT NOT NULL DEFAULT '',
                    task_lease_token TEXT NOT NULL DEFAULT '',
                    permit_jti TEXT NOT NULL DEFAULT '',
                    permit_token_hash TEXT NOT NULL DEFAULT '',
                    permit_expires_at REAL,
                    submission_intent_id TEXT NOT NULL DEFAULT '',
                    confirmation_id TEXT NOT NULL DEFAULT '',
                    success_url TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_real_application_status
                    ON real_application_tasks(status, created_at, attempt_id);
                """
            )
            # A control-plane restart invalidates every in-memory opaque Gate B
            # token and interrupts every remote task lease.  Requeue only
            # pre-fence work; a durable intent always wins and remains
            # SUBMITTING/UNKNOWN with automatic retry blocked.
            connection.execute(
                "DELETE FROM leases WHERE resource LIKE 'real-application:%'"
            )
            connection.execute(
                """
                UPDATE real_application_tasks
                SET status = CASE (
                        SELECT status FROM submission_intents
                        WHERE submission_intents.run_id = real_application_tasks.attempt_id
                          AND submission_intents.status IN ('SUBMITTING', 'UNKNOWN', 'VERIFIED')
                        ORDER BY created_at DESC LIMIT 1
                    )
                        WHEN 'VERIFIED' THEN 'CONFIRMED'
                        WHEN 'UNKNOWN' THEN 'SUBMISSION_OUTCOME_UNKNOWN'
                        ELSE 'SUBMITTING'
                    END,
                    submission_intent_id = COALESCE((
                        SELECT intent_id FROM submission_intents
                        WHERE submission_intents.run_id = real_application_tasks.attempt_id
                          AND submission_intents.status IN ('SUBMITTING', 'UNKNOWN', 'VERIFIED')
                        ORDER BY created_at DESC LIMIT 1
                    ), submission_intent_id),
                    permit_jti = '', permit_token_hash = '', permit_expires_at = NULL
                WHERE EXISTS (
                    SELECT 1 FROM submission_intents
                    WHERE submission_intents.run_id = real_application_tasks.attempt_id
                      AND submission_intents.status IN ('SUBMITTING', 'UNKNOWN', 'VERIFIED')
                )
                """
            )
            connection.execute(
                """
                UPDATE real_application_tasks
                SET status = 'PREPARED', worker_id = '', task_lease_token = '',
                    permit_jti = '', permit_token_hash = '', permit_expires_at = NULL
                WHERE status IN (
                    'CLAIMED', 'HUMAN_INTERVENTION_REQUIRED',
                    'REVIEW_READY', 'APPROVED'
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM submission_intents
                    WHERE submission_intents.run_id = real_application_tasks.attempt_id
                      AND submission_intents.status IN ('SUBMITTING', 'UNKNOWN', 'VERIFIED')
                )
                """
            )

    def ready(self) -> bool:
        """Check the durable control state used by Kubernetes readiness."""

        try:
            with self.ledger._connect() as connection:
                row = connection.execute(
                    "SELECT 1 FROM real_application_tasks LIMIT 1"
                ).fetchone()
            del row
            return bool(self.subject_id and self._enrollment_hash)
        except Exception:
            return False

    def enroll_worker(self, enrollment_secret: str) -> WorkerEnrollment:
        supplied = hash_value(_clean(
            "enrollment_secret", enrollment_secret, maximum=1024
        ))
        if not hmac.compare_digest(supplied, self._enrollment_hash):
            raise RealApplicationNotAuthorizedError("worker enrollment was rejected")
        now = float(self.clock())
        worker_id = f"worker-{secrets.token_hex(16)}"
        session_secret = secrets.token_urlsafe(48)
        session_hash = hash_value(session_secret)
        expires_at = now + WORKER_SESSION_TTL_SECONDS
        with self.ledger.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM real_worker_enrollment WHERE singleton = 1"
            ).fetchone() is not None:
                raise RealApplicationConflictError(
                    "the one-time worker enrollment was already consumed"
                )
            connection.execute(
                "INSERT INTO real_worker_enrollment(singleton, worker_id, enrolled_at) VALUES (1, ?, ?)",
                (worker_id, now),
            )
            connection.execute(
                """
                INSERT INTO real_worker_sessions(
                    worker_id, session_hash, issued_at, expires_at,
                    last_heartbeat_at, disabled
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (worker_id, session_hash, now, expires_at, now),
            )
        return WorkerEnrollment(worker_id, session_secret, expires_at)

    def authenticate_worker(self, session_secret: str) -> str:
        if not isinstance(session_secret, str) or not session_secret:
            raise RealApplicationNotAuthorizedError("worker session is missing")
        digest = hash_value(session_secret)
        now = float(self.clock())
        with self.ledger._connect() as connection:
            rows = connection.execute(
                """
                SELECT worker_id, session_hash, expires_at, disabled
                FROM real_worker_sessions
                WHERE expires_at > ? AND disabled = 0
                """,
                (now,),
            ).fetchall()
        for row in rows:
            if hmac.compare_digest(row["session_hash"], digest):
                return str(row["worker_id"])
        raise RealApplicationNotAuthorizedError("worker session is invalid")

    def heartbeat_worker(self, worker_id: str) -> None:
        worker_id = _require_id("worker_id", worker_id)
        now = float(self.clock())
        with self.ledger.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE real_worker_sessions
                SET last_heartbeat_at = ?, expires_at = ?
                WHERE worker_id = ? AND expires_at > ? AND disabled = 0
                """,
                (now, now + WORKER_SESSION_TTL_SECONDS, worker_id, now),
            )
            if cursor.rowcount != 1:
                raise RealApplicationNotAuthorizedError("worker session is not current")

    def executor_status(self) -> RealApplicationExecutorStatus:
        threshold = float(self.clock()) - WORKER_ONLINE_WINDOW_SECONDS
        with self.ledger._connect() as connection:
            online = connection.execute(
                """
                SELECT 1 FROM real_worker_sessions
                WHERE last_heartbeat_at >= ? AND expires_at > ? AND disabled = 0
                LIMIT 1
                """,
                (threshold, float(self.clock())),
            ).fetchone()
        return (
            RealApplicationExecutorStatus.AVAILABLE
            if online is not None
            else RealApplicationExecutorStatus.EXECUTOR_UNAVAILABLE
        )

    def prepare(self, worker_id: str, preparation: RealApplicationPreparation) -> str:
        if preparation.subject_id != self.subject_id:
            raise RealApplicationNotAuthorizedError("attempt belongs to another subject")
        worker_id = _require_id("worker_id", worker_id)
        now = float(self.clock())
        try:
            self.ledger.create_run(
                run_id=preparation.attempt_id,
                job_id=preparation.job_id,
                metadata=preparation.safe_metadata(),
            )
        except RunAlreadyExistsError:
            run = self.ledger.get_run(preparation.attempt_id)
            if run.job_id != preparation.job_id:
                raise RealApplicationConflictError(
                    "attempt ID belongs to another job"
                ) from None
        values = (
            preparation.attempt_id,
            preparation.subject_id,
            preparation.application_plan_id,
            preparation.assembly_record_id,
            preparation.assembly_record_content_hash,
            preparation.job_id,
            preparation.external_job_id,
            preparation.company,
            preparation.title,
            preparation.provider,
            preparation.ats_host,
            preparation.canonical_job_url,
            preparation.bundle_canonical_hash,
            preparation.profile_snapshot_hash,
            preparation.answer_hash,
            preparation.answer_bundle_hash,
            preparation.material_hash,
            preparation.resume_sha256,
            preparation.cover_letter_sha256,
            preparation.policy_hash,
            _canonical_json(preparation.answer_bundle),
            RealApplicationTaskStatus.PREPARED.value,
            now,
            now,
        )
        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM real_application_tasks WHERE attempt_id = ?",
                (preparation.attempt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["bundle_canonical_hash"]
                    == preparation.bundle_canonical_hash
                    and existing["answer_bundle_hash"]
                    == preparation.answer_bundle_hash
                    and existing["resume_sha256"] == preparation.resume_sha256
                    and existing["cover_letter_sha256"]
                    == preparation.cover_letter_sha256
                ):
                    return "UNCHANGED"
                raise RealApplicationConflictError(
                    "attempt payload changed after preparation"
                )
            connection.execute(
                """
                INSERT INTO real_application_tasks(
                    attempt_id, subject_id, application_plan_id,
                    assembly_record_id, assembly_record_content_hash, job_id,
                    external_job_id, company, title, provider, ats_host,
                    canonical_job_url, bundle_canonical_hash,
                    profile_snapshot_hash, answer_hash, answer_bundle_hash,
                    material_hash, resume_sha256, cover_letter_sha256,
                    policy_hash, answer_bundle_json, status, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        self.ledger.append_event(
            run_id=preparation.attempt_id,
            job_id=preparation.job_id,
            event_type="REAL_APPLICATION_PREPARED",
            payload={
                "answer_bundle_hash": preparation.answer_bundle_hash,
                "bundle_canonical_hash": preparation.bundle_canonical_hash,
                "provider": preparation.provider,
                "worker_id": worker_id,
            },
        )
        return "CREATED"

    @staticmethod
    def _row_to_public(row: Any, *, include_answers: bool) -> dict[str, Any]:
        value = {
            key: row[key]
            for key in (
                "attempt_id",
                "application_plan_id",
                "assembly_record_id",
                "assembly_record_content_hash",
                "job_id",
                "external_job_id",
                "company",
                "title",
                "provider",
                "ats_host",
                "canonical_job_url",
                "bundle_canonical_hash",
                "profile_snapshot_hash",
                "answer_hash",
                "answer_bundle_hash",
                "material_hash",
                "resume_sha256",
                "cover_letter_sha256",
                "policy_hash",
                "review_hash",
                "status",
                "submission_intent_id",
                "confirmation_id",
                "success_url",
                "created_at",
                "updated_at",
            )
        }
        value["review"] = json.loads(row["review_json"] or "{}")
        if include_answers:
            value["answer_bundle"] = json.loads(row["answer_bundle_json"])
        return value

    def list_tasks(self) -> tuple[dict[str, Any], ...]:
        with self.ledger._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM real_application_tasks ORDER BY created_at DESC, attempt_id DESC"
            ).fetchall()
        executor = self.executor_status().value
        return tuple(
            {
                **self._row_to_public(row, include_answers=False),
                "executor_status": executor,
            }
            for row in rows
        )

    def get_task(self, attempt_id: str, *, include_answers: bool = True) -> dict[str, Any]:
        attempt_id = _require_id("attempt_id", attempt_id)
        with self.ledger._connect() as connection:
            row = connection.execute(
                "SELECT * FROM real_application_tasks WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None or row["subject_id"] != self.subject_id:
            raise KeyError(attempt_id)
        return {
            **self._row_to_public(row, include_answers=include_answers),
            "executor_status": self.executor_status().value,
            "timeline": tuple(
                {
                    "sequence": item.sequence,
                    "type": item.event_type,
                    "created_at": item.created_at,
                }
                for item in self.ledger.list_events(run_id=attempt_id)
            ),
        }

    def claim_next(self, worker_id: str) -> ClaimedRealApplicationTask | None:
        worker_id = _require_id("worker_id", worker_id)
        with self.ledger._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM real_application_tasks
                WHERE status = 'PREPARED'
                ORDER BY created_at, attempt_id
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        attempt_id = str(row["attempt_id"])
        resource = f"real-application:{attempt_id}"
        try:
            lease = self.leases.acquire(
                resource,
                owner=worker_id,
                ttl_seconds=self.task_lease_ttl_seconds,
            )
        except LeaseUnavailableError:
            return None
        now = float(self.clock())
        try:
            with self.ledger.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE real_application_tasks
                    SET status = 'CLAIMED', worker_id = ?,
                        task_lease_token = ?, updated_at = ?
                    WHERE attempt_id = ? AND status = 'PREPARED'
                    """,
                    (worker_id, lease.token, now, attempt_id),
                )
                if cursor.rowcount != 1:
                    raise RealApplicationConflictError("task claim raced")
        except Exception:
            try:
                self.leases.release(lease)
            except LeaseError:
                pass
            raise
        self.ledger.append_event(
            run_id=attempt_id,
            job_id=str(row["job_id"]),
            event_type="REAL_APPLICATION_CLAIMED",
            payload={"worker_id": worker_id},
        )
        task = self.get_task(attempt_id, include_answers=False)
        return ClaimedRealApplicationTask(task=task, lease=lease)

    def heartbeat_task(self, worker_id: str, attempt_id: str, lease_token: str) -> Lease:
        task = self._owned_task(worker_id, attempt_id, lease_token)
        current = self.leases.get(f"real-application:{attempt_id}")
        if current is None or current.token != lease_token or current.owner != worker_id:
            raise RealApplicationNotAuthorizedError("task lease is not current")
        renewed = self.leases.renew(
            current, ttl_seconds=self.task_lease_ttl_seconds
        )
        self.heartbeat_worker(worker_id)
        return renewed

    def _owned_task(self, worker_id: str, attempt_id: str, lease_token: str) -> Any:
        worker_id = _require_id("worker_id", worker_id)
        attempt_id = _require_id("attempt_id", attempt_id)
        lease_token = _clean("lease_token", lease_token, maximum=200)
        with self.ledger._connect() as connection:
            row = connection.execute(
                "SELECT * FROM real_application_tasks WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if (
            row is None
            or row["subject_id"] != self.subject_id
            or row["worker_id"] != worker_id
            or row["task_lease_token"] != lease_token
        ):
            raise RealApplicationNotAuthorizedError("worker does not own this task")
        current = self.leases.get(f"real-application:{attempt_id}")
        if current is None:
            raise RealApplicationNotAuthorizedError("task lease is missing")
        try:
            self.leases.assert_current(current)
        except LeaseError as exc:
            raise RealApplicationNotAuthorizedError("task lease expired") from exc
        if current.owner != worker_id or current.token != lease_token:
            raise RealApplicationNotAuthorizedError("task lease binding changed")
        return row

    def report_human_intervention(
        self,
        worker_id: str,
        attempt_id: str,
        lease_token: str,
        *,
        reason: str,
        checkpoint: str,
    ) -> None:
        row = self._owned_task(worker_id, attempt_id, lease_token)
        if row["status"] not in {
            RealApplicationTaskStatus.CLAIMED.value,
            RealApplicationTaskStatus.HUMAN_INTERVENTION_REQUIRED.value,
        }:
            raise RealApplicationConflictError("task cannot request intervention now")
        payload = {
            "human_intervention": {
                "checkpoint": _clean("checkpoint", checkpoint),
                "reason": _clean("reason", reason, maximum=500),
            }
        }
        with self.ledger.transaction() as connection:
            connection.execute(
                """
                UPDATE real_application_tasks
                SET status = 'HUMAN_INTERVENTION_REQUIRED', review_json = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (_canonical_json(payload), float(self.clock()), attempt_id),
            )
        self.ledger.append_event(
            run_id=attempt_id,
            job_id=str(row["job_id"]),
            event_type="HUMAN_INTERVENTION_REQUIRED",
            payload={"checkpoint": payload["human_intervention"]["checkpoint"]},
        )

    def continue_after_human(self, attempt_id: str) -> None:
        attempt_id = _require_id("attempt_id", attempt_id)
        with self.ledger._connect() as connection:
            row = connection.execute(
                "SELECT * FROM real_application_tasks WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None or row["subject_id"] != self.subject_id:
            raise KeyError(attempt_id)
        if row["status"] != RealApplicationTaskStatus.HUMAN_INTERVENTION_REQUIRED.value:
            raise RealApplicationConflictError("task is not waiting for human intervention")
        if self.executor_status() is not RealApplicationExecutorStatus.AVAILABLE:
            raise RealApplicationConflictError("executor is unavailable")
        self._owned_task(
            str(row["worker_id"]), attempt_id, str(row["task_lease_token"])
        )
        with self.ledger.transaction() as connection:
            connection.execute(
                """
                UPDATE real_application_tasks
                SET status = 'CLAIMED', updated_at = ?
                WHERE attempt_id = ? AND status = 'HUMAN_INTERVENTION_REQUIRED'
                """,
                (float(self.clock()), attempt_id),
            )
        self.ledger.append_event(
            run_id=attempt_id,
            job_id=str(task["job_id"]),
            event_type="REAL_APPLICATION_HUMAN_CONTINUED",
            payload={},
        )

    def report_review(
        self,
        worker_id: str,
        attempt_id: str,
        lease_token: str,
        *,
        review_hash: str,
        review: Mapping[str, Any],
    ) -> None:
        row = self._owned_task(worker_id, attempt_id, lease_token)
        if row["status"] != RealApplicationTaskStatus.CLAIMED.value:
            raise RealApplicationConflictError("task is not executing toward Review")
        review_hash = _require_hash("review_hash", review_hash)
        review_value = _json_object("review", review, maximum_bytes=128_000)
        if review_value.get("submit_control_present") is not True:
            raise ValueError("Review must expose a final submit control")
        declarations = review_value.get("legal_declarations")
        if not isinstance(declarations, list) or any(
            not isinstance(item, str) or not item.strip() or len(item) > 2_000
            for item in declarations
        ):
            raise ValueError("Review legal declarations must be an explicit list")
        unresolved = review_value.get("unresolved_required")
        if not isinstance(unresolved, list) or any(
            not isinstance(item, str) or not item.strip() or len(item) > 500
            for item in unresolved
        ):
            raise ValueError("Review unresolved fields must be an explicit list")
        review_fields = review_value.get("review_fields")
        if not isinstance(review_fields, list) or any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("label"), str)
            or not item.get("label", "").strip()
            or not isinstance(item.get("source"), str)
            or not item.get("source", "").strip()
            or not isinstance(item.get("certainty"), str)
            or not item.get("certainty", "").strip()
            or "value" not in item
            for item in review_fields
        ):
            raise ValueError("Review fields must be explicitly projected")
        with self.ledger.transaction() as connection:
            connection.execute(
                """
                UPDATE real_application_tasks
                SET status = 'REVIEW_READY', review_hash = ?, review_json = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'CLAIMED'
                """,
                (
                    review_hash,
                    _canonical_json(review_value),
                    float(self.clock()),
                    attempt_id,
                ),
            )
        outcome = ApplicationOutcome.review_ready(
            run_id=attempt_id,
            job_id=str(row["job_id"]),
            adapter=str(row["provider"]),
            checkpoint="real-application.review",
            details={
                "review_fingerprint": review_hash,
                "submit_control_present": True,
            },
        )
        self._record_outcome(outcome)

    def report_execution_failure(
        self,
        worker_id: str,
        attempt_id: str,
        lease_token: str,
        *,
        outcome: ApplicationOutcome,
    ) -> None:
        """Close an attempt that failed before the durable Submit fence."""

        row = self._owned_task(worker_id, attempt_id, lease_token)
        if row["status"] not in {
            RealApplicationTaskStatus.CLAIMED.value,
            RealApplicationTaskStatus.REVIEW_READY.value,
            RealApplicationTaskStatus.APPROVED.value,
        }:
            raise RealApplicationConflictError(
                "pre-submit execution failure cannot be recorded now"
            )
        if outcome.run_id != attempt_id or outcome.job_id != row["job_id"]:
            raise ValueError("outcome belongs to another attempt")
        if outcome.status in {
            OutcomeStatus.SUBMITTING,
            OutcomeStatus.SUBMITTED_VERIFIED,
            OutcomeStatus.SUBMIT_UNKNOWN,
        } or outcome.phase in {OutcomePhase.SUBMIT, OutcomePhase.VERIFY, OutcomePhase.COMPLETE}:
            raise ValueError("post-fence outcome cannot use the failure endpoint")
        with self.ledger.transaction() as connection:
            connection.execute(
                """
                UPDATE real_application_tasks
                SET status = 'FAILED', permit_jti = '', permit_token_hash = '',
                    permit_expires_at = NULL, updated_at = ?
                WHERE attempt_id = ? AND status != 'SUBMITTING'
                """,
                (float(self.clock()), attempt_id),
            )
        self._active_permits.pop(attempt_id, None)
        self._record_outcome(outcome)

    def _permit_policy_hash(self, row: Any) -> str:
        return canonical_hash(
            {
                "application_plan_id": row["application_plan_id"],
                "assembly_record_content_hash": row["assembly_record_content_hash"],
                "assembly_record_id": row["assembly_record_id"],
                "attempt_id": row["attempt_id"],
                "ats_host": row["ats_host"],
                "answer_bundle_hash": row["answer_bundle_hash"],
                "bundle_canonical_hash": row["bundle_canonical_hash"],
                "cover_letter_sha256": row["cover_letter_sha256"],
                "external_job_id": row["external_job_id"],
                "formal_policy_hash": row["policy_hash"],
                "profile_snapshot_hash": row["profile_snapshot_hash"],
                "provider": row["provider"],
                "resume_sha256": row["resume_sha256"],
                "subject_id": row["subject_id"],
            }
        )

    def _bindings(self, row: Any) -> PermitBindings:
        return PermitBindings(
            run_id=str(row["attempt_id"]),
            job_id=str(row["job_id"]),
            job_url_hash=hash_job_url(str(row["canonical_job_url"])),
            material_hash=str(row["material_hash"]),
            answer_hash=str(row["answer_hash"]),
            review_hash=str(row["review_hash"]),
            policy_hash=self._permit_policy_hash(row),
        )

    def approve(
        self,
        attempt_id: str,
        *,
        reviewed_hash: str,
        external_side_effect_acknowledged: bool,
    ) -> None:
        attempt_id = _require_id("attempt_id", attempt_id)
        with self.ledger._connect() as connection:
            row = connection.execute(
                "SELECT * FROM real_application_tasks WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        if row is None or row["subject_id"] != self.subject_id:
            raise KeyError(attempt_id)
        if row["status"] != RealApplicationTaskStatus.REVIEW_READY.value:
            raise RealApplicationConflictError("only a Review-ready task can be approved")
        if _require_hash("reviewed_hash", reviewed_hash) != row["review_hash"]:
            raise RealApplicationConflictError("the displayed Review changed")
        if external_side_effect_acknowledged is not True:
            raise RealApplicationConflictError("external Submit was not acknowledged")
        review = json.loads(row["review_json"])
        if review.get("unresolved_required"):
            raise RealApplicationConflictError(
                "required or low-confidence answers remain unresolved"
            )
        answer_bundle = json.loads(row["answer_bundle_json"])
        if any(
            bool(item.get("blocking"))
            for item in answer_bundle.get("unresolved", [])
            if isinstance(item, Mapping)
        ):
            raise RealApplicationConflictError(
                "the formal answer bundle still requires human resolution"
            )
        if self.executor_status() is not RealApplicationExecutorStatus.AVAILABLE:
            raise RealApplicationConflictError("executor is unavailable")
        self._owned_task(str(row["worker_id"]), attempt_id, str(row["task_lease_token"]))
        bindings = self._bindings(row)
        gate_a = self.permit_service.issue_gate_a(
            bindings, ttl_seconds=self.permit_ttl_seconds
        )
        gate_a_claims = self.permit_service.consume(
            gate_a,
            expected_gate=PermitGate.GATE_A,
            expected_bindings=bindings,
        )
        token = self.permit_service.issue_gate_b(
            bindings,
            gate_a_jti=gate_a_claims.jti,
            ttl_seconds=self.permit_ttl_seconds,
        )
        claims = self.permit_service.verify(
            token,
            expected_gate=PermitGate.GATE_B,
            expected_bindings=bindings,
        )
        with self.ledger.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE real_application_tasks
                SET status = 'APPROVED', permit_jti = ?,
                    permit_token_hash = ?, permit_expires_at = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'REVIEW_READY'
                """,
                (
                    claims.jti,
                    hash_value(token),
                    float(claims.expires_at),
                    float(self.clock()),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RealApplicationConflictError("approval state changed")
        self._active_permits[attempt_id] = token
        self.ledger.append_event(
            run_id=attempt_id,
            job_id=str(row["job_id"]),
            event_type="REAL_APPLICATION_APPROVED",
            payload={
                "permit_jti": claims.jti,
                "permit_expires_at": claims.expires_at,
            },
        )

    def load_worker_permit(
        self, worker_id: str, attempt_id: str, lease_token: str
    ) -> str:
        row = self._owned_task(worker_id, attempt_id, lease_token)
        if row["status"] != RealApplicationTaskStatus.APPROVED.value:
            raise RealApplicationConflictError("task is not approved")
        token = self._active_permits.get(attempt_id)
        if token is None or not hmac.compare_digest(
            hash_value(token), str(row["permit_token_hash"])
        ):
            raise RealApplicationConflictError(
                "approved permit is unavailable; a new explicit approval is required"
            )
        return token

    def final_fence(
        self,
        worker_id: str,
        attempt_id: str,
        lease_token: str,
        *,
        permit_token: str,
        current_url: str,
        external_job_id: str,
        bundle_canonical_hash: str,
        profile_snapshot_hash: str,
        answer_hash: str,
        answer_bundle_hash: str,
        material_hash: str,
        resume_sha256: str,
        cover_letter_sha256: str,
        review_hash: str,
        assembly_record_id: str,
        assembly_record_content_hash: str,
    ) -> SubmissionIntent:
        row = self._owned_task(worker_id, attempt_id, lease_token)
        if row["status"] != RealApplicationTaskStatus.APPROVED.value:
            raise RealApplicationConflictError("task is not approved for final fencing")
        expected_token = self._active_permits.get(attempt_id)
        if expected_token is None or not hmac.compare_digest(
            hash_value(permit_token), str(row["permit_token_hash"])
        ) or not hmac.compare_digest(permit_token, expected_token):
            raise RealApplicationNotAuthorizedError("submission permit was rejected")
        current_url = normalized_job_url(current_url)
        current_host = (urlsplit(current_url).hostname or "").casefold()
        expected = {
            "external_job_id": str(row["external_job_id"]),
            "bundle_canonical_hash": str(row["bundle_canonical_hash"]),
            "profile_snapshot_hash": str(row["profile_snapshot_hash"]),
            "answer_hash": str(row["answer_hash"]),
            "answer_bundle_hash": str(row["answer_bundle_hash"]),
            "material_hash": str(row["material_hash"]),
            "resume_sha256": str(row["resume_sha256"]),
            "cover_letter_sha256": str(row["cover_letter_sha256"]),
            "review_hash": str(row["review_hash"]),
            "assembly_record_id": str(row["assembly_record_id"]),
            "assembly_record_content_hash": str(row["assembly_record_content_hash"]),
        }
        actual = {
            "external_job_id": _require_id("external_job_id", external_job_id),
            "bundle_canonical_hash": _require_hash(
                "bundle_canonical_hash", bundle_canonical_hash
            ),
            "profile_snapshot_hash": _require_hash(
                "profile_snapshot_hash", profile_snapshot_hash
            ),
            "answer_hash": _require_hash("answer_hash", answer_hash),
            "answer_bundle_hash": _require_hash(
                "answer_bundle_hash", answer_bundle_hash
            ),
            "material_hash": _require_hash("material_hash", material_hash),
            "resume_sha256": _require_hash("resume_sha256", resume_sha256),
            "cover_letter_sha256": _require_hash(
                "cover_letter_sha256", cover_letter_sha256
            ),
            "review_hash": _require_hash("review_hash", review_hash),
            "assembly_record_id": _require_id(
                "assembly_record_id", assembly_record_id
            ),
            "assembly_record_content_hash": _require_hash(
                "assembly_record_content_hash", assembly_record_content_hash
            ),
        }
        if actual != expected:
            raise RealApplicationNotAuthorizedError(
                "current application inputs differ from the approved review"
            )
        if current_host != row["ats_host"] or not _same_workday_session_url(
            current_url, str(row["canonical_job_url"])
        ):
            raise RealApplicationNotAuthorizedError(
                "active Workday page does not match the approved posting"
            )
        bindings = self._bindings(row)
        try:
            claims = self.permit_service.verify(
                permit_token,
                expected_gate=PermitGate.GATE_B,
                expected_bindings=bindings,
            )
            intent = self.ledger.reserve_permitted_submission(
                jti=claims.jti,
                gate=claims.gate.value,
                run_id=attempt_id,
                job_id=str(row["job_id"]),
                token_digest=hash_value(permit_token),
                bindings_digest=bindings.digest,
                claims=claims.to_dict(),
                job_url=str(row["canonical_job_url"]),
                material_hash=str(row["material_hash"]),
                answer_hash=str(row["answer_hash"]),
                review_hash=str(row["review_hash"]),
                policy_hash=self._permit_policy_hash(row),
                application_key_override=canonical_hash(
                    {
                        "external_job_id": row["external_job_id"],
                        "provider": row["provider"],
                        "subject_id": row["subject_id"],
                    }
                ),
            )
        except (DuplicateSubmissionError, PermitError) as exc:
            raise RealApplicationNotAuthorizedError(
                "final submission fence was rejected"
            ) from exc
        with self.ledger.transaction() as connection:
            connection.execute(
                """
                UPDATE real_application_tasks
                SET status = 'SUBMITTING', submission_intent_id = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'APPROVED'
                """,
                (intent.intent_id, float(self.clock()), attempt_id),
            )
        self._active_permits.pop(attempt_id, None)
        return intent

    def report_outcome(
        self,
        worker_id: str,
        attempt_id: str,
        lease_token: str,
        *,
        outcome: ApplicationOutcome,
        confirmation_id: str = "",
        success_url: str = "",
    ) -> RealApplicationTaskStatus:
        row = self._owned_task(worker_id, attempt_id, lease_token)
        if row["status"] != RealApplicationTaskStatus.SUBMITTING.value:
            raise RealApplicationConflictError("task has not crossed final fencing")
        if outcome.run_id != attempt_id or outcome.job_id != row["job_id"]:
            raise ValueError("outcome belongs to another attempt")
        intent_id = str(row["submission_intent_id"])
        final_status: RealApplicationTaskStatus
        if outcome.status is OutcomeStatus.SUBMITTED_VERIFIED:
            evidence = next(
                (
                    item
                    for item in outcome.evidence_refs
                    if item.kind in SUBMISSION_EVIDENCE_KINDS
                ),
                None,
            )
            if evidence is None:
                raise ValueError("verified outcome lacks eligible evidence")
            self.ledger.mark_submission_verified(
                intent_id=intent_id, evidence=evidence
            )
            final_status = RealApplicationTaskStatus.CONFIRMED
        else:
            current = self.ledger.get_submission_intent(intent_id)
            if current.status is SubmissionStatus.SUBMITTING:
                self.ledger.mark_submission_unknown(intent_id)
            final_status = RealApplicationTaskStatus.SUBMISSION_OUTCOME_UNKNOWN
            outcome = ApplicationOutcome(
                run_id=attempt_id,
                job_id=str(row["job_id"]),
                status=OutcomeStatus.SUBMIT_UNKNOWN,
                phase=OutcomePhase.VERIFY,
                reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
                message=(
                    "Submit crossed the durable fence but explicit ATS confirmation "
                    "was not established; automatic retry is blocked"
                ),
                adapter=str(row["provider"]),
                checkpoint=f"submission-intent:{intent_id}",
                details={
                    "do_not_retry_submit": True,
                    "human_reconciliation_required": True,
                },
            )
        safe_confirmation = (
            _clean("confirmation_id", confirmation_id, maximum=200)
            if confirmation_id
            else ""
        )
        safe_url = ""
        if success_url:
            candidate = normalized_job_url(success_url)
            parsed = urlsplit(candidate)
            if (parsed.hostname or "").casefold() != row["ats_host"]:
                raise ValueError("success URL host differs from the approved ATS host")
            if not _same_workday_session_url(
                candidate, str(row["canonical_job_url"])
            ):
                raise ValueError("success URL differs from the approved posting")
            safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        with self.ledger.transaction() as connection:
            connection.execute(
                """
                UPDATE real_application_tasks
                SET status = ?, confirmation_id = ?, success_url = ?, updated_at = ?
                WHERE attempt_id = ? AND status = 'SUBMITTING'
                """,
                (
                    final_status.value,
                    safe_confirmation,
                    safe_url,
                    float(self.clock()),
                    attempt_id,
                ),
            )
        self._record_outcome(outcome)
        return final_status

    def _record_outcome(self, outcome: ApplicationOutcome) -> None:
        run = self.ledger.get_run(outcome.run_id)
        self.ledger.compare_and_set_state(
            run_id=outcome.run_id,
            expected_version=run.state_version,
            new_state=outcome.status.value,
            outcome=outcome,
        )


__all__ = [
    "ClaimedRealApplicationTask",
    "REAL_APPLICATION_CONTROL_CONTRACT_VERSION",
    "REAL_APPLICATION_PERMIT_TTL_SECONDS",
    "REAL_APPLICATION_TASK_LEASE_TTL_SECONDS",
    "RealApplicationConflictError",
    "RealApplicationControlError",
    "RealApplicationControlPlane",
    "RealApplicationExecutorStatus",
    "RealApplicationNotAuthorizedError",
    "RealApplicationPreparation",
    "RealApplicationTaskStatus",
    "WorkerEnrollment",
]
