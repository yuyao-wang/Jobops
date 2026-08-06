"""Headed local Workday executor for one explicitly approved real attempt."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import async_playwright

from adapters.workday import (
    WorkdayAdapter,
    WorkdayApplicationContext,
    WorkdayRuntimeConfig,
)
from auth.credentials import CredentialStore, MacOSSecurityCredentialStore
from core.browser_broker import lease_browser_session
from core.event_ledger import EventLedger
from core.leases import LeaseManager
from core.outcomes import (
    ApplicationOutcome,
    OutcomePhase,
    OutcomeStatus,
    ReasonCode,
)
from core.private_home import PrivateHome
from core.production_application_bootstrap import (
    ProductionApplicationConfig,
    load_production_application_config,
    resolve_production_config_path,
)
from jobops.control_client import (
    ControlPlaneClientError,
    RealApplicationControlClient,
)
from jobops.real_application import (
    RealApplicationPreparationError,
    load_formal_real_application,
)
from utils.browser_session import BrowserSession, launch_browser_session


_NEEDS_HUMAN = frozenset(
    {
        OutcomeStatus.NEEDS_USER,
        OutcomeStatus.NEEDS_USER_LOGIN,
        OutcomeStatus.NEEDS_USER_2FA,
        OutcomeStatus.NEEDS_USER_CAPTCHA,
        OutcomeStatus.NEEDS_USER_EMAIL_VERIFICATION,
        OutcomeStatus.NEEDS_USER_ACCOUNT_LOCKED,
        OutcomeStatus.NEEDS_USER_SENSITIVE_ANSWER,
    }
)
_FINAL_STATUSES = frozenset(
    {"CONFIRMED", "SUBMISSION_OUTCOME_UNKNOWN", "FAILED"}
)
_CONFIRMATION_ID_RE = re.compile(
    r"(?:(?:confirmation|application)\s*(?:id|number|#)|reference(?:\s*(?:id|number|#))?)"
    r"\s*[:#-]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9._-]{3,80})",
    re.IGNORECASE,
)


class BrowserExecutorError(RuntimeError):
    pass


async def _default_playwright_factory() -> Any:
    return await async_playwright().start()


async def _review_projection(page: Any) -> dict[str, Any]:
    """Read a bounded review projection; never return passwords or file bytes."""

    value = await page.evaluate(
        r"""() => {
            const clean = (value, maximum = 500) => String(value || '')
                .replace(/\s+/g, ' ').trim().slice(0, maximum);
            const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const root = document.querySelector('[data-automation-id="reviewPage"]')
                || document.querySelector('main') || document.body;
            const fields = [];
            for (const el of Array.from(root.querySelectorAll('input, textarea, select')).slice(0, 250)) {
                const type = String(el.type || '').toLowerCase();
                if (!visible(el) || ['hidden', 'password', 'file'].includes(type)) continue;
                let label = clean(el.getAttribute('aria-label'));
                if (!label && el.labels) label = clean(Array.from(el.labels).map(item => item.innerText).join(' '));
                if (!label) label = clean(el.name || el.id || el.getAttribute('data-automation-id'));
                let fieldValue = type === 'checkbox' || type === 'radio'
                    ? String(Boolean(el.checked))
                    : el.tagName === 'SELECT'
                        ? clean(el.selectedOptions?.[0]?.textContent || el.value)
                        : clean(el.value);
                if (label) fields.push({
                    certainty: 'EXACT_ATS_READBACK', label, source: 'WORKDAY_REVIEW', value: fieldValue,
                });
            }
            for (const dt of Array.from(root.querySelectorAll('dt')).slice(0, 150)) {
                const label = clean(dt.textContent);
                const fieldValue = clean(dt.nextElementSibling?.textContent);
                if (label && fieldValue) fields.push({
                    certainty: 'EXACT_ATS_READBACK', label, source: 'WORKDAY_REVIEW', value: fieldValue,
                });
            }
            const legal = [];
            for (const el of Array.from(root.querySelectorAll('label, legend, p, li')).slice(0, 500)) {
                const text = clean(el.textContent, 2000);
                if (text && /(certif|attest|agree|consent|signature|truth|accurate|terms|privacy)/i.test(text)) {
                    legal.push(text);
                }
            }
            const submit = Array.from(root.querySelectorAll('button, input[type="submit"]'))
                .some(el => visible(el) && /submit/i.test(clean(el.innerText || el.value || el.getAttribute('aria-label'))));
            const visited = Array.isArray(window.__jobopsVisitedStates)
                ? window.__jobopsVisitedStates.map(item => clean(item, 100)).filter(Boolean).slice(0, 50)
                : ['workday.review'];
            return {
                legal_declarations: Array.from(new Set(legal)).slice(0, 30),
                page_states: visited,
                review_fields: fields.slice(0, 250),
                submit_control_present: submit,
                unresolved_required: [],
            };
        }"""
    )
    if not isinstance(value, Mapping):
        raise BrowserExecutorError("Workday Review projection was unavailable")
    return dict(value)


async def _confirmation_metadata(page: Any) -> tuple[str, str]:
    try:
        text = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        text = ""
    match = _CONFIRMATION_ID_RE.search(str(text)[:50_000])
    confirmation_id = match.group(1) if match else ""
    parsed = urlsplit(str(getattr(page, "url", "")))
    success_url = (
        urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if parsed.scheme in {"http", "https"} and parsed.hostname
        else ""
    )
    return confirmation_id, success_url


class LocalWorkdayBrowserExecutor:
    """Own the local browser while the control plane owns approval/state."""

    def __init__(
        self,
        *,
        client: RealApplicationControlClient,
        config: ProductionApplicationConfig,
        home: PrivateHome,
        credential_store: CredentialStore | None = None,
        playwright_factory: Callable[[], Awaitable[Any]] = _default_playwright_factory,
        launch: Callable[..., Awaitable[BrowserSession]] = launch_browser_session,
        poll_seconds: float = 2.0,
    ) -> None:
        self.client = client
        self.config = config
        self.home = home
        self.credential_store = credential_store or MacOSSecurityCredentialStore()
        self.playwright_factory = playwright_factory
        self.launch = launch
        self.poll_seconds = float(poll_seconds)
        self.local_leases = LeaseManager(EventLedger(home.paths.event_ledger))

    async def run_once(self) -> str:
        await self.client.heartbeat_worker()
        claimed = await self.client.claim_next()
        if claimed.get("status") == "EMPTY":
            return "EMPTY"
        task = claimed.get("task")
        lease = claimed.get("task_lease")
        if not isinstance(task, Mapping) or not isinstance(lease, Mapping):
            raise BrowserExecutorError("claimed task response was invalid")
        attempt_id = str(task.get("attempt_id") or "")
        lease_token = str(lease.get("token") or "")
        preparation, bundle = load_formal_real_application(
            subject_id=self.config.authentication.local_subject_id,
            assembly_record_id=str(task.get("assembly_record_id") or ""),
            home=self.home,
        )
        self._validate_task(task, preparation.to_dict())

        heartbeat_stop = asyncio.Event()
        heartbeat_errors: list[BaseException] = []
        heartbeat = asyncio.create_task(
            self._heartbeat(attempt_id, lease_token, heartbeat_stop, heartbeat_errors),
            name=f"jobops-real-task-heartbeat:{attempt_id}",
        )
        playwright = await self.playwright_factory()
        try:
            profile = {
                "private_home": str(self.home.root),
                "browser": {"slow_mo_ms": self.config.browser.slow_mo_ms},
            }
            async with lease_browser_session(
                playwright,
                profile=profile,
                leases=self.local_leases,
                owner=f"real-application:{attempt_id}",
                headless=False,
                ttl_seconds=float(self.config.browser.lease_ttl_seconds),
                launch=self.launch,
            ) as browser:
                return await self._execute_claimed(
                    task=dict(task),
                    lease_token=lease_token,
                    preparation=preparation,
                    bundle=bundle,
                    browser=browser,
                    heartbeat_errors=heartbeat_errors,
                )
        finally:
            heartbeat_stop.set()
            await heartbeat
            stop = getattr(playwright, "stop", None)
            if callable(stop):
                await stop()

    async def _heartbeat(
        self,
        attempt_id: str,
        lease_token: str,
        stop: asyncio.Event,
        errors: list[BaseException],
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=20.0)
                return
            except TimeoutError:
                pass
            try:
                await self.client.heartbeat_task(attempt_id, lease_token)
            except BaseException as exc:
                errors.append(exc)
                return

    @staticmethod
    def _validate_task(task: Mapping[str, Any], local: Mapping[str, Any]) -> None:
        names = (
            "answer_bundle_hash",
            "answer_hash",
            "application_plan_id",
            "assembly_record_content_hash",
            "assembly_record_id",
            "attempt_id",
            "bundle_canonical_hash",
            "canonical_job_url",
            "cover_letter_sha256",
            "external_job_id",
            "job_id",
            "material_hash",
            "policy_hash",
            "profile_snapshot_hash",
            "provider",
            "resume_sha256",
        )
        if any(str(task.get(name)) != str(local.get(name)) for name in names):
            raise BrowserExecutorError(
                "claimed task differs from the formal local ApplicationBundle"
            )

    async def _execute_claimed(
        self,
        *,
        task: dict[str, Any],
        lease_token: str,
        preparation: Any,
        bundle: Any,
        browser: Any,
        heartbeat_errors: list[BaseException],
    ) -> str:
        attempt_id = preparation.attempt_id
        adapter = WorkdayAdapter(credential_store=self.credential_store)
        context = WorkdayApplicationContext(
            page=browser.session.page,
            job_url=preparation.canonical_job_url,
            profile=bundle.identity_profile,
            job_id=preparation.job_id,
            run_id=attempt_id,
            company=preparation.company,
            resume_path=str(bundle.materials.resume_path),
            cover_letter=bundle.materials.cover_letter,
            answers=bundle.answers.to_dict(),
            request_submit=False,
            credential_store=self.credential_store,
            materials=bundle.materials,
            private_home=self.home,
            runtime_config=WorkdayRuntimeConfig(
                auto_login=self.config.execution_policy.allow_keychain_login,
                auto_register=self.config.execution_policy.allow_account_registration,
            ),
            navigation_timeout_ms=self.config.browser.navigation_timeout_seconds * 1000,
        )
        outcome = await adapter.run(context)
        while outcome.status in _NEEDS_HUMAN:
            await self.client.report_human_intervention(
                attempt_id,
                lease_token,
                reason=outcome.message or outcome.status.value,
                checkpoint=outcome.checkpoint or "workday.human_intervention",
            )
            await self._wait_for_status(
                attempt_id, lease_token, {"CLAIMED"}, heartbeat_errors
            )
            context.navigate = False
            outcome = await adapter.run(context)

        if outcome.status is not OutcomeStatus.REVIEW_READY:
            await self.client.report_failure(
                attempt_id, lease_token, outcome.to_dict()
            )
            return "FAILED"
        review_hash = str(outcome.details.get("review_fingerprint") or "")
        if not review_hash:
            raise BrowserExecutorError("Workday Review fingerprint was unavailable")
        review = await _review_projection(browser.session.page)
        await self.client.report_review(
            attempt_id,
            lease_token,
            review_hash=review_hash,
            review=review,
        )
        await self._wait_for_status(
            attempt_id, lease_token, {"APPROVED"}, heartbeat_errors
        )
        permit = await self.client.permit(attempt_id, lease_token)
        fence_crossed = False

        async def final_validator(
            supplied_permit: str,
            *,
            job_id: str,
            run_id: str,
            review_fingerprint: str,
        ) -> bool:
            nonlocal fence_crossed
            if heartbeat_errors:
                raise BrowserExecutorError("remote task lease heartbeat failed")
            browser.lease_guard.assert_fresh()
            await self.client.heartbeat_task(attempt_id, lease_token)
            current, _current_bundle = load_formal_real_application(
                subject_id=self.config.authentication.local_subject_id,
                assembly_record_id=preparation.assembly_record_id,
                home=self.home,
            )
            if job_id != current.job_id or run_id != current.attempt_id:
                return False
            await self.client.final_fence(
                attempt_id,
                lease_token,
                {
                    "answer_bundle_hash": current.answer_bundle_hash,
                    "answer_hash": current.answer_hash,
                    "assembly_record_content_hash": current.assembly_record_content_hash,
                    "assembly_record_id": current.assembly_record_id,
                    "bundle_canonical_hash": current.bundle_canonical_hash,
                    "cover_letter_sha256": current.cover_letter_sha256,
                    "current_url": str(browser.session.page.url),
                    "external_job_id": current.external_job_id,
                    "material_hash": current.material_hash,
                    "permit": supplied_permit,
                    "profile_snapshot_hash": current.profile_snapshot_hash,
                    "resume_sha256": current.resume_sha256,
                    "review_hash": review_fingerprint,
                },
            )
            fence_crossed = True
            return True

        context.request_submit = True
        context.navigate = False
        context.gate_b_permit = permit
        context.gate_b_validator = final_validator
        context.persisted_review_attestation = str(
            outcome.details.get("workday_binding_attestation") or ""
        )
        try:
            outcome = await adapter.run(context)
        except BaseException as exc:
            if not fence_crossed:
                raise
            outcome = ApplicationOutcome(
                run_id=attempt_id,
                job_id=preparation.job_id,
                status=OutcomeStatus.SUBMIT_UNKNOWN,
                phase=OutcomePhase.VERIFY,
                reason_code=ReasonCode.SUBMISSION_CONFIRMATION_MISSING,
                message=(
                    "The durable Submit fence was crossed but local execution "
                    "ended before confirmation; automatic retry is blocked"
                ),
                adapter="workday",
                checkpoint="workday.submit_unknown",
                details={"exception_type": type(exc).__name__, "do_not_retry_submit": True},
            )
        if not fence_crossed:
            await self.client.report_failure(
                attempt_id, lease_token, outcome.to_dict()
            )
            return "FAILED"
        confirmation_id, success_url = await _confirmation_metadata(
            browser.session.page
        )
        result = await self.client.report_outcome(
            attempt_id,
            lease_token,
            {
                "confirmation_id": confirmation_id,
                "outcome": outcome.to_dict(),
                "success_url": success_url,
            },
        )
        status = str(result.get("status") or "SUBMISSION_OUTCOME_UNKNOWN")
        self._write_acceptance_metadata(
            preparation=preparation,
            outcome=outcome,
            status=status,
            confirmation_id=confirmation_id,
        )
        return status

    async def _wait_for_status(
        self,
        attempt_id: str,
        lease_token: str,
        accepted: set[str],
        heartbeat_errors: list[BaseException],
    ) -> Mapping[str, Any]:
        while True:
            if heartbeat_errors:
                raise BrowserExecutorError("remote task lease heartbeat failed")
            task = await self.client.task(attempt_id)
            status = str(task.get("status") or "")
            if status in accepted:
                return task
            if status in _FINAL_STATUSES:
                raise BrowserExecutorError(
                    "control-plane attempt closed while browser was waiting"
                )
            await asyncio.sleep(self.poll_seconds)

    def _write_acceptance_metadata(
        self,
        *,
        preparation: Any,
        outcome: ApplicationOutcome,
        status: str,
        confirmation_id: str,
    ) -> None:
        path = (
            self.home.paths.logs
            / "live-acceptance"
            / preparation.attempt_id
            / "result.json"
        )
        self.home.write_text(
            path,
            json.dumps(
                {
                    "attempt_id": preparation.attempt_id,
                    "bundle_canonical_hash": preparation.bundle_canonical_hash,
                    "confirmation_id": confirmation_id,
                    "cover_letter_sha256": preparation.cover_letter_sha256,
                    "external_job_id": preparation.external_job_id,
                    "outcome_status": outcome.status.value,
                    "resume_sha256": preparation.resume_sha256,
                    "status": status,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jobops.browser_executor",
        description="Run the single headed local Workday browser executor.",
    )
    parser.add_argument("--server", default="http://127.0.0.1:9000")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--once", action="store_true", help="Exit after one task or one empty poll."
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    config_path = resolve_production_config_path(cli_path=args.config)
    config = load_production_application_config(config_path)
    home = PrivateHome(config.private_home.root)
    client = RealApplicationControlClient(args.server)
    if not client.has_session():
        enrollment = getpass.getpass(
            "Paste the one-time worker enrollment token (input hidden): "
        )
        await client.enroll(enrollment)
    executor = LocalWorkdayBrowserExecutor(
        client=client, config=config, home=home
    )
    while True:
        status = await executor.run_once()
        print(f"Local browser executor status: {status}")
        if args.once:
            return 0
        await asyncio.sleep(2.0 if status == "EMPTY" else 0.1)


def main() -> int:
    try:
        return asyncio.run(_run(_parser().parse_args()))
    except (BrowserExecutorError, ControlPlaneClientError, RealApplicationPreparationError) as exc:
        print(f"Browser executor stopped safely: {exc}")
        return 10
    except Exception as exc:
        print(
            "Browser executor stopped safely with an internal failure: "
            f"{type(exc).__name__}"
        )
        return 50


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BrowserExecutorError", "LocalWorkdayBrowserExecutor", "main"]
