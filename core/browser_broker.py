"""Exclusive persistent-browser launch broker."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from utils.browser_session import BrowserSession, launch_browser_session

from .leases import Lease, LeaseManager, RenewingLease


@dataclass(frozen=True, slots=True)
class LeasedBrowser:
    session: BrowserSession
    lease_guard: RenewingLease

    @property
    def lease(self) -> Lease:
        """Return the latest renewed lease while preserving the public API."""

        return self.lease_guard.lease


@asynccontextmanager
async def lease_browser_session(
    playwright: Any,
    *,
    profile: dict,
    leases: LeaseManager,
    owner: str,
    headless: bool = False,
    ttl_seconds: float = 1800.0,
    launch: Callable[..., Awaitable[BrowserSession]] = launch_browser_session,
) -> AsyncIterator[LeasedBrowser]:
    """Acquire the browser lease before opening the persistent profile."""

    async with leases.hold_renewing(
        "browser:chromium",
        owner=owner,
        ttl_seconds=ttl_seconds,
    ) as lease_guard:
        session: BrowserSession | None = None
        try:
            session = await launch(playwright, profile, headless=headless)
            yield LeasedBrowser(session=session, lease_guard=lease_guard)
        finally:
            if session is not None:
                await session.close()


__all__ = ["LeasedBrowser", "lease_browser_session"]
