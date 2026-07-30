"""Server-owned Playwright runtime implementing the P2c3/P2c6 lease port."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from .leases import (
    Lease,
    LeaseError,
    LeaseManager,
    LeaseUnavailableError,
    RenewingLease,
)
from .private_home import PRIVATE_DIRECTORY_MODE, PrivateHome


BROWSER_RUNTIME_CONFIG_CONTRACT_VERSION = "browser-runtime-config-v1"
BROWSER_RUNTIME_CONTRACT_VERSION = "production-browser-runtime-v1"
BROWSER_LEASE_PROVIDER_CONTRACT_VERSION = (
    "production-browser-lease-provider-v1"
)
BROWSER_ARGS_POLICY_VERSION = "chromium-browser-args-v1"
BROWSER_PAGE_POLICY_VERSION = "new-page-per-lease-v1"
_OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}\Z")
_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_LOCK_NAMES = ("SingletonCookie", "SingletonLock", "SingletonSocket")


class BrowserRuntimeState(StrEnum):
    NEW = "NEW"
    STARTED = "STARTED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class BrowserRuntimeFailure(StrEnum):
    BROWSER_RUNTIME_UNAVAILABLE = "BROWSER_RUNTIME_UNAVAILABLE"
    BROWSER_STARTUP_FAILED = "BROWSER_STARTUP_FAILED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    LEASE_UNAVAILABLE = "LEASE_UNAVAILABLE"
    PAGE_CREATION_FAILED = "PAGE_CREATION_FAILED"
    PAGE_INITIALIZATION_FAILED = "PAGE_INITIALIZATION_FAILED"
    PROFILE_DIRECTORY_INVALID = "PROFILE_DIRECTORY_INVALID"
    PROFILE_LOCKED = "PROFILE_LOCKED"
    RELEASE_FAILED = "RELEASE_FAILED"
    RUNTIME_CLOSED = "RUNTIME_CLOSED"
    RUNTIME_NOT_STARTED = "RUNTIME_NOT_STARTED"


class ProductionBrowserRuntimeError(RuntimeError):
    """Typed failure carrying no path, page data, credential, or URL."""

    def __init__(self, failure: BrowserRuntimeFailure) -> None:
        self.failure = BrowserRuntimeFailure(failure)
        super().__init__(self.failure.value)


@dataclass(frozen=True, slots=True)
class BrowserRuntimeConfig:
    user_data_directory: Path
    browser_engine: str = "CHROMIUM"
    headless: bool = True
    slow_mo_ms: int = 0
    launch_timeout_seconds: int = 30
    navigation_timeout_seconds: int = 30
    lease_ttl_seconds: int = 1800
    max_active_leases: int = 1
    locale: str = "en-CA"
    timezone_id: str = "UTC"
    download_policy: str = "DENY"
    tracing_policy: str = "DISABLED"
    browser_args_policy_version: str = BROWSER_ARGS_POLICY_VERSION
    page_policy_version: str = BROWSER_PAGE_POLICY_VERSION
    single_subject_mode: bool = True
    config_contract_version: str = BROWSER_RUNTIME_CONFIG_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.config_contract_version != BROWSER_RUNTIME_CONFIG_CONTRACT_VERSION:
            raise ValueError("browser runtime config version is unsupported")
        if self.browser_engine != "CHROMIUM":
            raise ValueError("only Chromium automation is supported")
        if type(self.headless) is not bool:
            raise TypeError("headless must be boolean")
        if type(self.slow_mo_ms) is not int or not 0 <= self.slow_mo_ms <= 5_000:
            raise ValueError("slow_mo_ms is outside policy")
        for name, maximum in (
            ("launch_timeout_seconds", 120),
            ("navigation_timeout_seconds", 120),
            ("lease_ttl_seconds", 7_200),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside policy")
        if self.max_active_leases != 1:
            raise ValueError("V1 requires exactly one active Browser lease")
        for name, maximum in (("locale", 32), ("timezone_id", 80)):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{name} is invalid")
        if self.download_policy != "DENY" or self.tracing_policy != "DISABLED":
            raise ValueError("browser download or tracing policy is unsupported")
        if self.browser_args_policy_version != BROWSER_ARGS_POLICY_VERSION:
            raise ValueError("browser args policy version is unsupported")
        if self.page_policy_version != BROWSER_PAGE_POLICY_VERSION:
            raise ValueError("browser page policy version is unsupported")
        if self.single_subject_mode is not True:
            raise ValueError("V1 supports explicit single-subject servers only")
        path = self.user_data_directory
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("user data directory must be an absolute Path")


@dataclass(frozen=True, slots=True)
class BrowserRuntimeDiagnostics:
    runtime_contract_version: str
    engine: str
    headless: bool
    persistent_context_enabled: bool
    lease_provider_enabled: bool
    max_active_leases: int
    startup_status: str
    browser_version: str
    single_subject_mode: bool


@dataclass(slots=True)
class ProductionBrowserLease:
    context: Any
    page: Any
    lease_guard: RenewingLease
    provider_contract_version: str = BROWSER_LEASE_PROVIDER_CONTRACT_VERSION

    @property
    def lease(self) -> Lease:
        return self.lease_guard.lease

    @property
    def owner(self) -> str:
        return self.lease.owner

    @property
    def acquired_at(self) -> float:
        return self.lease.acquired_at

    @property
    def expires_at(self) -> float:
        return self.lease.expires_at


def project_browser_runtime_config(
    application_config: Mapping[str, Any],
    *,
    private_home: PrivateHome,
) -> BrowserRuntimeConfig:
    """Project only application-level browser settings into a closed type."""

    if not isinstance(application_config, Mapping):
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        )
    raw = application_config.get("browser_runtime")
    if not isinstance(raw, Mapping):
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        )
    allowed = {
        "browser_args_policy_version",
        "browser_engine",
        "config_contract_version",
        "download_policy",
        "headless",
        "launch_timeout_seconds",
        "lease_ttl_seconds",
        "locale",
        "max_active_leases",
        "navigation_timeout_seconds",
        "page_policy_version",
        "profile_name",
        "single_subject_mode",
        "slow_mo_ms",
        "timezone_id",
        "tracing_policy",
    }
    if set(raw) - allowed:
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        )
    profile_name = raw.get("profile_name")
    if not isinstance(profile_name, str) or _PROFILE_NAME_RE.fullmatch(
        profile_name
    ) is None:
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        )
    browser_root = private_home.paths.browser.resolve(strict=False)
    directory = browser_root / "automation" / profile_name
    try:
        return BrowserRuntimeConfig(
            user_data_directory=directory,
            **{
                key: value
                for key, value in raw.items()
                if key != "profile_name"
            },
        )
    except (TypeError, ValueError):
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        ) from None


async def _default_playwright_factory() -> Any:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.BROWSER_RUNTIME_UNAVAILABLE
        ) from None
    return await async_playwright().start()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_profile_directory(
    config: BrowserRuntimeConfig,
    private_home: PrivateHome,
) -> None:
    directory = config.user_data_directory
    browser_root = private_home.paths.browser.resolve(strict=False)
    if not _inside(directory, browser_root) or directory == browser_root:
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.PROFILE_DIRECTORY_INVALID
        )
    current = directory
    while _inside(current, browser_root):
        if current.is_symlink():
            raise ProductionBrowserRuntimeError(
                BrowserRuntimeFailure.PROFILE_DIRECTORY_INVALID
            )
        if current == browser_root:
            break
        current = current.parent
    try:
        private_home.ensure()
        directory.mkdir(
            parents=True,
            exist_ok=True,
            mode=PRIVATE_DIRECTORY_MODE,
        )
        if directory.is_symlink() or not directory.is_dir():
            raise OSError
        directory.chmod(PRIVATE_DIRECTORY_MODE)
    except OSError:
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.PROFILE_DIRECTORY_INVALID
        ) from None
    if any(os.path.lexists(directory / name) for name in _LOCK_NAMES):
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.PROFILE_LOCKED
        )


class ProductionBrowserLeaseProvider:
    """The single structural BrowserLeaseProvider shared by P2c3 and P2c6."""

    def __init__(self, runtime: "ProductionBrowserRuntime") -> None:
        self._runtime = runtime

    @asynccontextmanager
    async def lease(self, *, owner: str) -> AsyncIterator[ProductionBrowserLease]:
        if not isinstance(owner, str) or _OWNER_RE.fullmatch(owner.strip()) is None:
            raise ProductionBrowserRuntimeError(
                BrowserRuntimeFailure.CONFIGURATION_ERROR
            )
        owner = owner.strip()
        runtime = self._runtime
        if runtime.state is BrowserRuntimeState.CLOSED:
            raise ProductionBrowserRuntimeError(
                BrowserRuntimeFailure.RUNTIME_CLOSED
            )
        if runtime.state is not BrowserRuntimeState.STARTED:
            raise ProductionBrowserRuntimeError(
                BrowserRuntimeFailure.RUNTIME_NOT_STARTED
            )
        try:
            lease_context = runtime.lease_manager.hold_renewing(
                "browser:chromium",
                owner=owner,
                ttl_seconds=runtime.config.lease_ttl_seconds,
            )
            async with lease_context as guard:
                if runtime.state is not BrowserRuntimeState.STARTED:
                    raise ProductionBrowserRuntimeError(
                        BrowserRuntimeFailure.RUNTIME_CLOSED
                    )
                context = runtime.context
                baseline = set(context.pages)
                try:
                    page = await context.new_page()
                except Exception:
                    raise ProductionBrowserRuntimeError(
                        BrowserRuntimeFailure.PAGE_CREATION_FAILED
                    ) from None
                runtime._active_pages.add(page)
                runtime._active_count += 1
                runtime._idle.clear()
                try:
                    try:
                        page.set_default_timeout(
                            runtime.config.navigation_timeout_seconds * 1000
                        )
                        page.set_default_navigation_timeout(
                            runtime.config.navigation_timeout_seconds * 1000
                        )
                    except Exception:
                        raise ProductionBrowserRuntimeError(
                            BrowserRuntimeFailure.PAGE_INITIALIZATION_FAILED
                        ) from None
                    yield ProductionBrowserLease(
                        context=context,
                        page=page,
                        lease_guard=guard,
                    )
                finally:
                    pages = tuple(
                        candidate
                        for candidate in context.pages
                        if candidate not in baseline
                    )
                    for candidate in reversed(pages):
                        try:
                            await candidate.close()
                        except Exception:
                            runtime._cleanup_failures += 1
                    runtime._active_pages.discard(page)
                    runtime._active_count = max(0, runtime._active_count - 1)
                    if runtime._active_count == 0:
                        runtime._idle.set()
        except LeaseUnavailableError:
            raise ProductionBrowserRuntimeError(
                BrowserRuntimeFailure.LEASE_UNAVAILABLE
            ) from None
        except LeaseError:
            raise ProductionBrowserRuntimeError(
                BrowserRuntimeFailure.RELEASE_FAILED
            ) from None


class ProductionBrowserRuntime:
    """Explicitly owned Playwright driver and persistent Chromium context."""

    def __init__(
        self,
        *,
        config: BrowserRuntimeConfig,
        private_home: PrivateHome,
        lease_manager: LeaseManager,
        playwright_factory: Callable[[], Awaitable[Any]],
    ) -> None:
        self.config = config
        self.private_home = private_home
        self.lease_manager = lease_manager
        self._playwright_factory = playwright_factory
        self._playwright: Any | None = None
        self.context: Any | None = None
        self.state = BrowserRuntimeState.NEW
        self._lock = asyncio.Lock()
        self._provider = ProductionBrowserLeaseProvider(self)
        self._active_pages: set[Any] = set()
        self._active_count = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._cleanup_failures = 0

    def lease_provider(self) -> ProductionBrowserLeaseProvider:
        return self._provider

    @property
    def diagnostics(self) -> BrowserRuntimeDiagnostics:
        return BrowserRuntimeDiagnostics(
            runtime_contract_version=BROWSER_RUNTIME_CONTRACT_VERSION,
            engine=self.config.browser_engine,
            headless=self.config.headless,
            persistent_context_enabled=self.context is not None,
            lease_provider_enabled=self.state is BrowserRuntimeState.STARTED,
            max_active_leases=self.config.max_active_leases,
            startup_status=self.state.value,
            browser_version="provider-managed",
            single_subject_mode=self.config.single_subject_mode,
        )

    async def start(self) -> None:
        async with self._lock:
            if self.state is BrowserRuntimeState.STARTED:
                return
            if self.state in {
                BrowserRuntimeState.CLOSING,
                BrowserRuntimeState.CLOSED,
            }:
                raise ProductionBrowserRuntimeError(
                    BrowserRuntimeFailure.RUNTIME_CLOSED
                )
            _validate_profile_directory(self.config, self.private_home)
            try:
                produced = self._playwright_factory()
                self._playwright = (
                    await produced if inspect.isawaitable(produced) else produced
                )
                chromium = getattr(self._playwright, "chromium")
                self.context = await chromium.launch_persistent_context(
                    user_data_dir=str(self.config.user_data_directory),
                    headless=self.config.headless,
                    slow_mo=self.config.slow_mo_ms,
                    locale=self.config.locale,
                    timezone_id=self.config.timezone_id,
                    accept_downloads=False,
                    timeout=self.config.launch_timeout_seconds * 1000,
                )
                self.context.set_default_timeout(
                    self.config.navigation_timeout_seconds * 1000
                )
                self.context.set_default_navigation_timeout(
                    self.config.navigation_timeout_seconds * 1000
                )
                for page in tuple(self.context.pages):
                    await page.close()
            except ProductionBrowserRuntimeError:
                await self._close_resources()
                raise
            except Exception:
                await self._close_resources()
                raise ProductionBrowserRuntimeError(
                    BrowserRuntimeFailure.BROWSER_STARTUP_FAILED
                ) from None
            self.state = BrowserRuntimeState.STARTED

    async def _close_resources(self) -> None:
        context, playwright = self.context, self._playwright
        self.context = None
        self._playwright = None
        if context is not None:
            try:
                await context.close()
            except Exception:
                self._cleanup_failures += 1
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                self._cleanup_failures += 1

    async def close(self) -> None:
        async with self._lock:
            if self.state is BrowserRuntimeState.CLOSED:
                return
            if self.state is BrowserRuntimeState.NEW:
                self.state = BrowserRuntimeState.CLOSED
                return
            self.state = BrowserRuntimeState.CLOSING
        try:
            await asyncio.wait_for(
                self._idle.wait(),
                timeout=self.config.launch_timeout_seconds,
            )
        except TimeoutError:
            pass
        async with self._lock:
            await self._close_resources()
            self.state = BrowserRuntimeState.CLOSED


def build_production_browser_runtime(
    *,
    config: BrowserRuntimeConfig,
    private_home: PrivateHome,
    lease_manager: LeaseManager,
    playwright_factory: Callable[[], Awaitable[Any]] | None = None,
) -> ProductionBrowserRuntime:
    """Construct the unique runtime without starting Chromium or navigating."""

    if not isinstance(config, BrowserRuntimeConfig):
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        )
    if not isinstance(private_home, PrivateHome):
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        )
    if not isinstance(lease_manager, LeaseManager):
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        )
    factory = playwright_factory or _default_playwright_factory
    if not callable(factory):
        raise ProductionBrowserRuntimeError(
            BrowserRuntimeFailure.CONFIGURATION_ERROR
        )
    return ProductionBrowserRuntime(
        config=config,
        private_home=private_home,
        lease_manager=lease_manager,
        playwright_factory=factory,
    )


__all__ = [
    "BROWSER_ARGS_POLICY_VERSION",
    "BROWSER_LEASE_PROVIDER_CONTRACT_VERSION",
    "BROWSER_PAGE_POLICY_VERSION",
    "BROWSER_RUNTIME_CONFIG_CONTRACT_VERSION",
    "BROWSER_RUNTIME_CONTRACT_VERSION",
    "BrowserRuntimeConfig",
    "BrowserRuntimeDiagnostics",
    "BrowserRuntimeFailure",
    "BrowserRuntimeState",
    "ProductionBrowserLease",
    "ProductionBrowserLeaseProvider",
    "ProductionBrowserRuntime",
    "ProductionBrowserRuntimeError",
    "build_production_browser_runtime",
    "project_browser_runtime_config",
]
