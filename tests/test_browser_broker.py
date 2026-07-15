from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from core.browser_broker import lease_browser_session
from core.event_ledger import EventLedger
from core.leases import LeaseManager, LeaseUnavailableError
from utils.browser_session import BrowserSession


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_broker_acquires_before_launch_and_releases_after_close(tmp_path: Path) -> None:
    leases = LeaseManager(EventLedger(tmp_path / "events.sqlite3"))
    context = FakeContext()

    async def launch(playwright, profile, headless=False):
        assert leases.get("browser:chromium") is not None
        return BrowserSession(context=context, page=object(), user_data_dir=tmp_path)

    async with lease_browser_session(
        object(), profile={}, leases=leases, owner="run-1", launch=launch
    ) as leased:
        assert leased.lease.owner == "run-1"
        with pytest.raises(LeaseUnavailableError):
            leases.acquire("browser:chromium", owner="run-2")

    assert context.closed is True
    assert leases.get("browser:chromium") is None


@pytest.mark.asyncio
async def test_broker_releases_lease_when_browser_close_fails(tmp_path: Path) -> None:
    leases = LeaseManager(EventLedger(tmp_path / "events.sqlite3"))

    class BrokenContext:
        async def close(self) -> None:
            raise RuntimeError("synthetic close failure")

    async def launch(playwright, profile, headless=False):
        return BrowserSession(context=BrokenContext(), page=object(), user_data_dir=tmp_path)

    with pytest.raises(RuntimeError, match="synthetic close failure"):
        async with lease_browser_session(
            object(), profile={}, leases=leases, owner="run-1", launch=launch
        ):
            pass

    assert leases.get("browser:chromium") is None


@pytest.mark.asyncio
async def test_broker_renews_short_lease_while_browser_is_open(tmp_path: Path) -> None:
    leases = LeaseManager(EventLedger(tmp_path / "events.sqlite3"))
    context = FakeContext()

    async def launch(playwright, profile, headless=False):
        return BrowserSession(context=context, page=object(), user_data_dir=tmp_path)

    async with lease_browser_session(
        object(),
        profile={},
        leases=leases,
        owner="run-heartbeat",
        launch=launch,
        ttl_seconds=0.18,
    ) as leased:
        initial = leased.lease
        await asyncio.sleep(0.28)
        current = leases.assert_current(initial)
        assert current.token == initial.token
        assert current.renewed_at > initial.renewed_at
        assert current.expires_at > time.time()
        assert leased.lease.expires_at == current.expires_at

    assert leases.get("browser:chromium") is None
