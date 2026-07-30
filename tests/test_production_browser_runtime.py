"""Focused P2c7a production Browser runtime tests with fake Playwright."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.authorized_submission_execution import (
    BrowserLeaseProvider as AuthorizedBrowserLeaseProvider,
)
from core.event_ledger import EventLedger
from core.leases import LeaseManager
from core.non_submit_application_execution import (
    BrowserLeaseProvider as NonSubmitBrowserLeaseProvider,
)
from core.private_home import PrivateHome
from core.production_browser_runtime import (
    BROWSER_RUNTIME_CONFIG_CONTRACT_VERSION,
    BrowserRuntimeFailure,
    BrowserRuntimeState,
    ProductionBrowserRuntimeError,
    build_production_browser_runtime,
    project_browser_runtime_config,
)


class _Page:
    def __init__(self, context: "_Context") -> None:
        self.context = context
        self.closed = False
        self.timeout = 0
        self.navigation_timeout = 0

    def set_default_timeout(self, value: int) -> None:
        self.timeout = value

    def set_default_navigation_timeout(self, value: int) -> None:
        self.navigation_timeout = value

    async def close(self) -> None:
        self.closed = True
        if self in self.context.pages:
            self.context.pages.remove(self)


class _Context:
    def __init__(self) -> None:
        self.pages = [_Page(self)]
        self.closed = False
        self.created: list[_Page] = []
        self.timeout = 0
        self.navigation_timeout = 0

    def set_default_timeout(self, value: int) -> None:
        self.timeout = value

    def set_default_navigation_timeout(self, value: int) -> None:
        self.navigation_timeout = value

    async def new_page(self) -> _Page:
        page = _Page(self)
        self.pages.append(page)
        self.created.append(page)
        return page

    async def close(self) -> None:
        self.closed = True
        for page in tuple(self.pages):
            await page.close()


class _Chromium:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.context = _Context()

    async def launch_persistent_context(self, **kwargs):
        self.calls.append(kwargs)
        return self.context


class _Playwright:
    def __init__(self) -> None:
        self.chromium = _Chromium()
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


def _parts(tmp_path: Path):
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    playwright = _Playwright()
    factory_calls = []

    async def factory():
        factory_calls.append("start")
        return playwright

    config = project_browser_runtime_config(
        {
            "personal": {"email": "must-not-be-read"},
            "common_answers": {"secret": "must-not-be-read"},
            "resume_path": "/must/not/be/read",
            "browser_runtime": {
                "config_contract_version": (
                    BROWSER_RUNTIME_CONFIG_CONTRACT_VERSION
                ),
                "profile_name": "synthetic-production",
                "headless": True,
                "single_subject_mode": True,
            },
        },
        private_home=home,
    )
    manager = LeaseManager(EventLedger(home.paths.event_ledger))
    runtime = build_production_browser_runtime(
        config=config,
        private_home=home,
        lease_manager=manager,
        playwright_factory=factory,
    )
    return home, playwright, factory_calls, runtime


@pytest.mark.asyncio
async def test_typed_config_startup_and_safe_diagnostics(tmp_path: Path) -> None:
    home, playwright, calls, runtime = _parts(tmp_path)

    assert calls == []
    await runtime.start()
    await runtime.start()

    assert calls == ["start"]
    assert runtime.state is BrowserRuntimeState.STARTED
    assert len(playwright.chromium.calls) == 1
    launch = playwright.chromium.calls[0]
    assert launch["accept_downloads"] is False
    assert launch["headless"] is True
    assert not playwright.chromium.context.pages
    diagnostic = runtime.diagnostics
    assert diagnostic.persistent_context_enabled is True
    assert str(home.root) not in repr(diagnostic)

    locked_home, _, _, locked = _parts(tmp_path / "locked")
    lock = locked.config.user_data_directory / "SingletonLock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("synthetic", encoding="utf-8")
    with pytest.raises(ProductionBrowserRuntimeError) as exc:
        await locked.start()
    assert exc.value.failure is BrowserRuntimeFailure.PROFILE_LOCKED
    assert str(locked_home.root) not in str(exc.value)

    _, _, _, linked = _parts(tmp_path / "linked")
    linked.config.user_data_directory.parent.mkdir(parents=True, exist_ok=True)
    linked.config.user_data_directory.symlink_to(
        tmp_path / "uncontrolled-profile",
        target_is_directory=True,
    )
    with pytest.raises(ProductionBrowserRuntimeError) as unsafe:
        await linked.start()
    assert unsafe.value.failure is (
        BrowserRuntimeFailure.PROFILE_DIRECTORY_INVALID
    )


@pytest.mark.asyncio
async def test_exclusive_lease_page_lifecycle_and_exception_release(
    tmp_path: Path,
) -> None:
    _, playwright, _, runtime = _parts(tmp_path)
    await runtime.start()
    provider = runtime.lease_provider()

    with pytest.raises(RuntimeError, match="synthetic"):
        async with provider.lease(owner="run-synthetic-one") as leased:
            first_page = leased.page
            assert leased.owner == "run-synthetic-one"
            assert leased.lease.resource == "browser:chromium"
            with pytest.raises(ProductionBrowserRuntimeError) as busy:
                async with provider.lease(owner="run-synthetic-two"):
                    pass
            assert busy.value.failure is BrowserRuntimeFailure.LEASE_UNAVAILABLE
            raise RuntimeError("synthetic")

    assert first_page.closed is True
    assert runtime.lease_manager.get("browser:chromium") is None
    assert playwright.chromium.context.closed is False

    acquired = asyncio.Event()

    async def cancelled_lease() -> None:
        async with provider.lease(owner="run-synthetic-cancel"):
            acquired.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(cancelled_lease())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.lease_manager.get("browser:chromium") is None

    async with provider.lease(owner="run-synthetic-three") as second:
        assert second.page is not first_page
        assert second.page.timeout == 30_000


@pytest.mark.asyncio
async def test_provider_satisfies_both_execution_ports_without_side_effects(
    tmp_path: Path,
) -> None:
    _, playwright, _, runtime = _parts(tmp_path)
    provider = runtime.lease_provider()
    assert isinstance(provider, NonSubmitBrowserLeaseProvider)
    assert isinstance(provider, AuthorizedBrowserLeaseProvider)
    assert playwright.chromium.calls == []

    with pytest.raises(ProductionBrowserRuntimeError) as not_started:
        async with provider.lease(owner="run-before-start"):
            pass
    assert not_started.value.failure is BrowserRuntimeFailure.RUNTIME_NOT_STARTED

    await runtime.start()
    async with provider.lease(owner="run-p2c3") as p2c3:
        p2c3_token = p2c3.lease.token
    async with provider.lease(owner="run-p2c6") as p2c6:
        assert p2c6.lease.token != p2c3_token
    assert len(playwright.chromium.context.created) == 2


@pytest.mark.asyncio
async def test_shutdown_is_idempotent_and_blocks_new_leases(
    tmp_path: Path,
) -> None:
    _, playwright, _, runtime = _parts(tmp_path)
    await runtime.start()
    page = None
    async with runtime.lease_provider().lease(owner="run-before-close") as lease:
        page = lease.page
    await runtime.close()
    await runtime.close()

    assert page is not None and page.closed is True
    assert playwright.chromium.context.closed is True
    assert playwright.stopped is True
    assert runtime.state is BrowserRuntimeState.CLOSED
    assert runtime.diagnostics.persistent_context_enabled is False
    with pytest.raises(ProductionBrowserRuntimeError) as closed:
        async with runtime.lease_provider().lease(owner="run-after-close"):
            pass
    assert closed.value.failure is BrowserRuntimeFailure.RUNTIME_CLOSED
