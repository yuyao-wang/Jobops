"""Deterministic adapter routing and a single execution request shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

from auth.credentials import CredentialStore
from auth.mailbox import MailboxVerifier
from auth.workday_hosts import is_trusted_workday_host
from core.outcomes import ApplicationOutcome

from .ashby import AshbyAdapter
from .generic_ai import GenericAIAdapter
from .greenhouse import GreenhouseAdapter
from .jobvite import JobviteAdapter
from .lever import LeverAdapter
from .protocol import ApplicationContext
from .workday import WorkdayAdapter, WorkdayApplicationContext


@dataclass(frozen=True, slots=True)
class AdapterRunRequest:
    page: Any
    job_url: str
    job_id: str
    run_id: str
    profile: Mapping[str, Any]
    resume_path: str
    cover_letter: str = ""
    answers: Mapping[str, Any] = field(default_factory=dict)
    request_submit: bool = False
    gate_b_permit: str | None = None
    gate_b_validator: Any = None
    persisted_review_attestation: str = ""
    credential_store: CredentialStore | None = None
    mailbox_verifier: MailboxVerifier | None = None
    brain: Any = None
    platform_hint: str = ""
    tenant: str = ""
    navigate: bool = True


class AdapterRegistry:
    """Route known ATS hosts without a model call; generic is the fallback."""

    def __init__(self, *, generic_adapter: GenericAIAdapter | None = None) -> None:
        self._specialized = {
            "greenhouse": GreenhouseAdapter(),
            "lever": LeverAdapter(),
            "ashby": AshbyAdapter(),
            "jobvite": JobviteAdapter(),
            "workday": WorkdayAdapter(),
        }
        # GenericAIAdapter owns a private recipe cache.  Construct it lazily so
        # probing/running a supported ATS never touches Private Home.
        self._generic = generic_adapter

    def _generic_adapter(self) -> GenericAIAdapter:
        if self._generic is None:
            self._generic = GenericAIAdapter()
        return self._generic

    @property
    def supported_names(self) -> tuple[str, ...]:
        return tuple(self._specialized)

    @staticmethod
    def route_name(url: str, platform_hint: str = "") -> str:
        host = (urlparse(url).hostname or "").casefold()
        suffixes = {
            "greenhouse": ("greenhouse.io",),
            "lever": ("lever.co",),
            "ashby": ("ashbyhq.com",),
            "jobvite": ("jobvite.com",),
        }
        detected = "generic_ai"
        for adapter, trusted_suffixes in suffixes.items():
            if any(
                host == suffix or host.endswith(f".{suffix}")
                for suffix in trusted_suffixes
            ):
                detected = adapter
                break
        if is_trusted_workday_host(host):
            detected = "workday"
        # ``platform_hint`` is untrusted CSV metadata. It can never route
        # credentials or candidate values to a different origin.
        return detected

    async def run(self, request: AdapterRunRequest) -> ApplicationOutcome:
        name = self.route_name(request.job_url, request.platform_hint)
        if name == "generic_ai":
            return await self._generic_adapter().run(
                page=request.page,
                job_url=request.job_url,
                profile=dict(request.profile),
                brain=request.brain,
                cover_letter=request.cover_letter,
                resume_path=request.resume_path,
                run_id=request.run_id,
                job_id=request.job_id,
                platform=request.platform_hint or "generic",
                tenant=request.tenant,
                credential_store=request.credential_store,
                gate_b_token=request.gate_b_permit,
                gate_b_validator=request.gate_b_validator,
                navigate=request.navigate,
            )

        if name == "workday":
            context = WorkdayApplicationContext(
                page=request.page,
                job_url=request.job_url,
                profile=request.profile,
                job_id=request.job_id,
                run_id=request.run_id,
                resume_path=request.resume_path,
                cover_letter=request.cover_letter,
                answers=request.answers,
                request_submit=request.request_submit,
                gate_b_permit=request.gate_b_permit,
                gate_b_validator=request.gate_b_validator,
                persisted_review_attestation=request.persisted_review_attestation,
                credential_store=request.credential_store,
                mailbox_verifier=request.mailbox_verifier,
                navigate=request.navigate,
            )
            return await self._specialized[name].run(context)

        context = ApplicationContext(
            page=request.page,
            job_url=request.job_url,
            job_id=request.job_id,
            run_id=request.run_id,
            profile=request.profile,
            resume_path=request.resume_path,
            cover_letter=request.cover_letter,
            answers=request.answers,
            request_submit=request.request_submit,
            gate_b_permit=request.gate_b_permit,
            gate_b_validator=request.gate_b_validator,
            navigate=request.navigate,
        )
        return await self._specialized[name].run(context)


__all__ = ["AdapterRegistry", "AdapterRunRequest"]
