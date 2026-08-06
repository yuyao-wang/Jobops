"""Authenticated local-executor client for the real-application control plane."""

from __future__ import annotations

import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from auth.credentials import CredentialStore, MacOSSecurityCredentialStore


WORKER_SESSION_KEYCHAIN_SERVICE = "jobops.real-application.worker.v1"


class ControlPlaneClientError(RuntimeError):
    """A safe, value-free control-plane client failure."""


def normalized_server_origin(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("control-plane server must be an HTTP(S) origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("control-plane server port is invalid") from exc
    host = parsed.hostname.casefold()
    default = 443 if parsed.scheme.casefold() == "https" else 80
    authority = host if port in {None, default} else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    if path:
        raise ValueError("control-plane server must not contain a path")
    if parsed.scheme.casefold() == "http":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise ValueError(
                "cleartext control-plane connections are limited to loopback"
            )
    return urlunsplit((parsed.scheme.casefold(), authority, path, "", ""))


def _session_account(server: str) -> str:
    return "server-" + hashlib.sha256(server.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class RealApplicationControlClient:
    server: str
    credential_store: CredentialStore
    timeout_seconds: float = 30.0

    def __init__(
        self,
        server: str,
        *,
        credential_store: CredentialStore | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.server = normalized_server_origin(server)
        self.credential_store = credential_store or MacOSSecurityCredentialStore()
        self.timeout_seconds = float(timeout_seconds)

    @property
    def _account(self) -> str:
        return _session_account(self.server)

    def has_session(self) -> bool:
        return bool(
            self.credential_store.get(
                WORKER_SESSION_KEYCHAIN_SERVICE, self._account
            )
        )

    async def enroll(self, enrollment_token: str) -> None:
        if self.has_session():
            return
        payload = await self._request(
            "POST",
            "/api/worker/enroll",
            authenticated=False,
            json_value={"enrollment_token": enrollment_token},
        )
        secret = payload.get("session_secret")
        if not isinstance(secret, str) or not secret:
            raise ControlPlaneClientError("worker enrollment response was invalid")
        self.credential_store.set(
            WORKER_SESSION_KEYCHAIN_SERVICE, self._account, secret
        )

    async def heartbeat_worker(self) -> Mapping[str, Any]:
        return await self._request("POST", "/api/worker/heartbeat")

    async def prepare(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._request(
            "POST", "/api/worker/tasks/prepare", json_value=payload
        )

    async def claim_next(self) -> Mapping[str, Any]:
        return await self._request("GET", "/api/worker/tasks/next")

    async def task(self, attempt_id: str) -> Mapping[str, Any]:
        return await self._request(
            "GET", f"/api/worker/tasks/{attempt_id}"
        )

    async def heartbeat_task(
        self, attempt_id: str, lease_token: str
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/api/worker/tasks/{attempt_id}/heartbeat",
            lease_token=lease_token,
        )

    async def report_human_intervention(
        self,
        attempt_id: str,
        lease_token: str,
        *,
        reason: str,
        checkpoint: str,
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/api/worker/tasks/{attempt_id}/human-intervention",
            lease_token=lease_token,
            json_value={"reason": reason, "checkpoint": checkpoint},
        )

    async def report_review(
        self,
        attempt_id: str,
        lease_token: str,
        *,
        review_hash: str,
        review: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/api/worker/tasks/{attempt_id}/review",
            lease_token=lease_token,
            json_value={"review_hash": review_hash, "review": dict(review)},
        )

    async def permit(self, attempt_id: str, lease_token: str) -> str:
        payload = await self._request(
            "GET",
            f"/api/worker/tasks/{attempt_id}/permit",
            lease_token=lease_token,
        )
        value = payload.get("permit")
        if not isinstance(value, str) or not value:
            raise ControlPlaneClientError("approved permit response was invalid")
        return value

    async def report_failure(
        self,
        attempt_id: str,
        lease_token: str,
        outcome: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/api/worker/tasks/{attempt_id}/failure",
            lease_token=lease_token,
            json_value={"outcome": dict(outcome)},
        )

    async def final_fence(
        self,
        attempt_id: str,
        lease_token: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/api/worker/tasks/{attempt_id}/final-fence",
            lease_token=lease_token,
            json_value=payload,
        )

    async def report_outcome(
        self,
        attempt_id: str,
        lease_token: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return await self._request(
            "POST",
            f"/api/worker/tasks/{attempt_id}/outcome",
            lease_token=lease_token,
            json_value=payload,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        lease_token: str = "",
        json_value: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        headers: dict[str, str] = {}
        if authenticated:
            session = self.credential_store.get(
                WORKER_SESSION_KEYCHAIN_SERVICE, self._account
            )
            if not session:
                raise ControlPlaneClientError(
                    "worker is not enrolled; paste the one-time enrollment token"
                )
            headers["Authorization"] = f"Bearer {session}"
        if lease_token:
            headers["X-JobOps-Task-Lease"] = lease_token
        try:
            async with httpx.AsyncClient(
                base_url=self.server,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.request(
                    method, path, headers=headers, json=json_value
                )
        except httpx.HTTPError as exc:
            raise ControlPlaneClientError(
                "control plane is unavailable"
            ) from exc
        if response.status_code == 401 and authenticated:
            raise ControlPlaneClientError("worker session is not authorized")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            safe = (
                str(detail)
                if isinstance(detail, str) and len(detail) <= 300
                else f"control-plane request failed ({response.status_code})"
            )
            raise ControlPlaneClientError(safe)
        try:
            value = response.json()
        except ValueError as exc:
            raise ControlPlaneClientError(
                "control-plane response was not JSON"
            ) from exc
        if not isinstance(value, Mapping):
            raise ControlPlaneClientError("control-plane response was invalid")
        return dict(value)


__all__ = [
    "ControlPlaneClientError",
    "RealApplicationControlClient",
    "WORKER_SESSION_KEYCHAIN_SERVICE",
    "normalized_server_origin",
]
