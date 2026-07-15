"""Expiring, owner-checked leases for browser and application workers."""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Iterator

from .event_ledger import EventLedger


class LeaseError(RuntimeError):
    pass


class LeaseUnavailableError(LeaseError):
    def __init__(self, resource: str, owner: str, expires_at: float):
        super().__init__(f"lease {resource!r} is held by {owner!r} until {expires_at}")
        self.resource = resource
        self.owner = owner
        self.expires_at = expires_at


class LeaseNotFoundError(LeaseError):
    pass


class LeaseOwnershipError(LeaseError):
    pass


class LeaseExpiredError(LeaseError):
    pass


@dataclass(frozen=True, slots=True)
class Lease:
    resource: str
    owner: str
    token: str
    acquired_at: float
    renewed_at: float
    expires_at: float

    def is_expired(self, *, now: float | None = None) -> bool:
        return self.expires_at <= (time.time() if now is None else now)


@dataclass(slots=True)
class RenewingLease:
    """Mutable handle whose lease value is replaced after each heartbeat."""

    manager: "LeaseManager"
    lease: Lease
    renewal_error: BaseException | None = None

    def assert_fresh(self) -> Lease:
        """Return the authoritative current lease or fail closed."""

        current = self.manager.assert_current(self.lease)
        self.lease = current
        return current


class LeaseManager:
    """Serialize access to persistent browser profiles and application runs."""

    def __init__(
        self,
        ledger: EventLedger,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.ledger = ledger
        self.clock = clock

    @staticmethod
    def _validate(resource: str, owner: str, ttl_seconds: float) -> None:
        if not resource.strip() or not owner.strip():
            raise ValueError("resource and owner are required")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

    @staticmethod
    def _from_row(row) -> Lease:
        return Lease(
            resource=row["resource"],
            owner=row["owner"],
            token=row["token"],
            acquired_at=float(row["acquired_at"]),
            renewed_at=float(row["renewed_at"]),
            expires_at=float(row["expires_at"]),
        )

    def acquire(
        self,
        resource: str,
        *,
        owner: str,
        ttl_seconds: float = 300.0,
    ) -> Lease:
        self._validate(resource, owner, ttl_seconds)
        now = self.clock()
        token = str(uuid.uuid4())
        expires_at = now + ttl_seconds
        with self.ledger.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM leases WHERE resource = ?", (resource,)
            ).fetchone()
            if existing is not None and float(existing["expires_at"]) > now:
                raise LeaseUnavailableError(
                    resource, existing["owner"], float(existing["expires_at"])
                )
            connection.execute("DELETE FROM leases WHERE resource = ?", (resource,))
            connection.execute(
                """
                INSERT INTO leases(
                    resource, owner, token, acquired_at, renewed_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (resource, owner, token, now, now, expires_at),
            )
        return Lease(resource, owner, token, now, now, expires_at)

    def get(self, resource: str) -> Lease | None:
        with self.ledger._connect() as connection:
            row = connection.execute(
                "SELECT * FROM leases WHERE resource = ?", (resource,)
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def assert_current(self, lease: Lease) -> Lease:
        """Validate token, owner, and expiry against the authoritative row.

        Callers must use this immediately before a safety-sensitive mutation;
        the expiry timestamp on an older ``Lease`` value can be stale after a
        heartbeat, while its opaque token remains stable.
        """

        current = self.get(lease.resource)
        if current is None:
            raise LeaseNotFoundError(lease.resource)
        if current.owner != lease.owner or current.token != lease.token:
            raise LeaseOwnershipError(lease.resource)
        if current.expires_at <= self.clock():
            raise LeaseExpiredError(lease.resource)
        return current

    def renew(self, lease: Lease, *, ttl_seconds: float = 300.0) -> Lease:
        self._validate(lease.resource, lease.owner, ttl_seconds)
        now = self.clock()
        with self.ledger.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM leases WHERE resource = ?", (lease.resource,)
            ).fetchone()
            if current is None:
                raise LeaseNotFoundError(lease.resource)
            if current["owner"] != lease.owner or current["token"] != lease.token:
                raise LeaseOwnershipError(lease.resource)
            if float(current["expires_at"]) <= now:
                raise LeaseExpiredError(lease.resource)
            expires_at = now + ttl_seconds
            connection.execute(
                """
                UPDATE leases SET renewed_at = ?, expires_at = ?
                WHERE resource = ? AND owner = ? AND token = ?
                """,
                (now, expires_at, lease.resource, lease.owner, lease.token),
            )
            acquired_at = float(current["acquired_at"])
        return Lease(
            resource=lease.resource,
            owner=lease.owner,
            token=lease.token,
            acquired_at=acquired_at,
            renewed_at=now,
            expires_at=expires_at,
        )

    def release(self, lease: Lease) -> None:
        with self.ledger.transaction() as connection:
            current = connection.execute(
                "SELECT owner, token FROM leases WHERE resource = ?", (lease.resource,)
            ).fetchone()
            if current is None:
                raise LeaseNotFoundError(lease.resource)
            if current["owner"] != lease.owner or current["token"] != lease.token:
                raise LeaseOwnershipError(lease.resource)
            connection.execute(
                "DELETE FROM leases WHERE resource = ? AND token = ?",
                (lease.resource, lease.token),
            )

    def clear_expired(self) -> int:
        with self.ledger.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM leases WHERE expires_at <= ?", (self.clock(),)
            )
            return cursor.rowcount

    @contextmanager
    def hold(
        self,
        resource: str,
        *,
        owner: str,
        ttl_seconds: float = 300.0,
    ) -> Iterator[Lease]:
        lease = self.acquire(resource, owner=owner, ttl_seconds=ttl_seconds)
        try:
            yield lease
        finally:
            try:
                self.release(lease)
            except (LeaseNotFoundError, LeaseExpiredError):
                pass

    @asynccontextmanager
    async def hold_renewing(
        self,
        resource: str,
        *,
        owner: str,
        ttl_seconds: float = 300.0,
        heartbeat_interval: float | None = None,
    ) -> AsyncIterator[RenewingLease]:
        """Hold a lease and renew it until the async context exits.

        Renewal uses the same owner/token and stops permanently on an error.
        Safety-sensitive callers still call :meth:`assert_current` immediately
        before mutation, so a failed heartbeat can never silently authorize a
        submit after expiry.
        """

        lease = self.acquire(resource, owner=owner, ttl_seconds=ttl_seconds)
        guard = RenewingLease(manager=self, lease=lease)
        interval = (
            float(heartbeat_interval)
            if heartbeat_interval is not None
            else min(60.0, max(0.05, ttl_seconds / 3.0))
        )
        if interval <= 0 or interval >= ttl_seconds:
            try:
                self.release(lease)
            finally:
                raise ValueError("heartbeat_interval must be positive and less than ttl_seconds")

        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    pass
                try:
                    guard.lease = self.renew(
                        guard.lease,
                        ttl_seconds=ttl_seconds,
                    )
                except Exception as exc:  # recorded and checked fail-closed
                    guard.renewal_error = exc
                    return

        task = asyncio.create_task(
            heartbeat(), name=f"jobops-lease-heartbeat:{resource}"
        )
        try:
            yield guard
        finally:
            stop.set()
            try:
                await task
            finally:
                try:
                    self.release(guard.lease)
                except (
                    LeaseNotFoundError,
                    LeaseExpiredError,
                    LeaseOwnershipError,
                ):
                    # An expired lease may already have been replaced. Never
                    # release a successor owned by another worker.
                    pass


__all__ = [
    "Lease",
    "LeaseError",
    "LeaseExpiredError",
    "LeaseManager",
    "LeaseNotFoundError",
    "LeaseOwnershipError",
    "LeaseUnavailableError",
    "RenewingLease",
]
