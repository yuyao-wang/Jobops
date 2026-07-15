"""Safe Python client for the optional standard-library-only Node worker.

The bridge intentionally starts a short-lived process for each request.  This
keeps failure and timeout handling simple while the Node surface is small.  The
worker itself accepts multiple JSON-lines requests, so a persistent transport can
be added later without changing the protocol.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


PROTOCOL_NAME = "jobops.node-worker"
PROTOCOL_VERSION = 1
EXPECTED_METHODS = ("capabilities", "probe_url")
EXPECTED_ADAPTERS = (
    "greenhouse",
    "lever",
    "ashby",
    "jobvite",
    "workday",
    "generic",
)
SUPPORTED_ADAPTERS = frozenset(EXPECTED_ADAPTERS)

_REMOTE_ERROR_MESSAGES = {
    "INVALID_REQUEST": "Node worker rejected an invalid request",
    "INVALID_JSON": "Node worker rejected malformed JSON",
    "INVALID_URL": "Node worker rejected an invalid URL",
    "METHOD_NOT_FOUND": "Node worker does not support the requested method",
    "PROTOCOL_MISMATCH": "Node worker rejected the protocol",
    "REQUEST_TOO_LARGE": "Node worker rejected an oversized request",
    "VERSION_MISMATCH": "Node worker rejected the protocol version",
    "INTERNAL_ERROR": "Node worker request failed",
}


class NodeWorkerError(RuntimeError):
    """Base error for sanitized worker failures."""


class NodeWorkerUnavailable(NodeWorkerError):
    """The configured Node executable or worker entry point is unavailable."""


class NodeWorkerTimeout(NodeWorkerError):
    """The worker did not complete before its request deadline."""


class NodeWorkerProtocolError(NodeWorkerError):
    """The worker returned malformed or incompatible protocol output."""


class NodeWorkerRemoteError(NodeWorkerError):
    """The worker rejected a well-formed request."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class NodeCapabilities:
    worker_version: str
    runtime_name: str
    runtime_version: str
    methods: tuple[str, ...]
    adapters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AtsUrlProbe:
    adapter: str
    supported: bool
    deterministic: bool
    match_basis: str


class NodeWorkerClient:
    """Invoke the versioned Node worker without putting payloads in argv."""

    def __init__(
        self,
        *,
        node_executable: str | os.PathLike[str] | None = None,
        worker_path: str | os.PathLike[str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.timeout_seconds = timeout
        self.node_executable = self._resolve_node(node_executable)
        default_worker = (
            Path(__file__).resolve().parents[1] / "workers" / "node" / "worker.mjs"
        )
        self.worker_path = Path(worker_path or default_worker).resolve()
        if not self.worker_path.is_file():
            raise NodeWorkerUnavailable("Node worker entry point is unavailable")

    @staticmethod
    def _resolve_node(value: str | os.PathLike[str] | None) -> str:
        if value is None:
            resolved = shutil.which("node")
        else:
            candidate = os.fspath(value)
            resolved = (
                candidate
                if os.path.isabs(candidate) and os.path.isfile(candidate)
                else shutil.which(candidate)
            )
        if not resolved:
            raise NodeWorkerUnavailable("Node.js executable is unavailable")
        return os.path.abspath(resolved)

    @staticmethod
    def _minimal_environment() -> dict[str, str]:
        """Pass runtime essentials only; never leak credential-bearing env vars."""

        env: dict[str, str] = {"NODE_NO_WARNINGS": "1"}
        for name in ("PATH", "LANG", "LC_ALL"):
            value = os.environ.get(name)
            if value:
                env[name] = value
        return env

    @staticmethod
    def _protocol_failure(message: str) -> NodeWorkerProtocolError:
        # Deliberately exclude stdout/stderr/request data from exceptions.
        return NodeWorkerProtocolError(message)

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a non-empty string")
        if params is not None and not isinstance(params, Mapping):
            raise TypeError("params must be a mapping")

        request_id = uuid.uuid4().hex
        request = {
            "protocol": PROTOCOL_NAME,
            "version": PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        try:
            payload = json.dumps(
                request, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ) + "\n"
        except (TypeError, ValueError) as exc:
            raise ValueError("request params must be JSON serializable") from exc

        try:
            completed = subprocess.run(
                [self.node_executable, str(self.worker_path)],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
                cwd=str(self.worker_path.parent),
                env=self._minimal_environment(),
            )
        except subprocess.TimeoutExpired as exc:
            raise NodeWorkerTimeout("Node worker request timed out") from exc
        except (OSError, UnicodeError) as exc:
            raise NodeWorkerUnavailable("Node worker could not be executed") from exc

        if completed.returncode != 0:
            raise NodeWorkerError("Node worker exited unsuccessfully")
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise self._protocol_failure("Node worker returned an invalid response count")
        try:
            response = json.loads(lines[0])
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise self._protocol_failure("Node worker returned malformed JSON") from exc
        if not isinstance(response, dict):
            raise self._protocol_failure("Node worker response must be a JSON object")
        if response.get("protocol") != PROTOCOL_NAME:
            raise self._protocol_failure("Node worker protocol mismatch")
        if response.get("version") != PROTOCOL_VERSION:
            raise self._protocol_failure("Node worker protocol version mismatch")
        if response.get("id") != request_id:
            raise self._protocol_failure("Node worker response id mismatch")
        if not isinstance(response.get("ok"), bool):
            raise self._protocol_failure("Node worker response is missing a valid status")

        if not response["ok"]:
            error = response.get("error")
            if not isinstance(error, dict):
                raise self._protocol_failure("Node worker error response is malformed")
            code = error.get("code")
            message = error.get("message")
            if not isinstance(code, str) or not code:
                raise self._protocol_failure("Node worker error code is malformed")
            if not isinstance(message, str) or not message:
                raise self._protocol_failure("Node worker error message is malformed")
            # Treat the remote message as untrusted. Fixed local text prevents a
            # future worker regression from echoing a submitted value.
            safe_message = _REMOTE_ERROR_MESSAGES.get(
                code, "Node worker rejected the request"
            )
            raise NodeWorkerRemoteError(code, safe_message)

        result = response.get("result")
        if not isinstance(result, dict):
            raise self._protocol_failure("Node worker result must be a JSON object")
        return MappingProxyType(result)

    def capabilities(self) -> NodeCapabilities:
        result = self.request("capabilities")
        if result.get("protocol") != PROTOCOL_NAME:
            raise self._protocol_failure("Node capabilities protocol mismatch")
        if result.get("protocol_version") != PROTOCOL_VERSION:
            raise self._protocol_failure("Node capabilities version mismatch")

        worker_version = result.get("worker_version")
        methods = result.get("methods")
        adapters = result.get("adapters")
        runtime = result.get("runtime")
        if not isinstance(worker_version, str) or not worker_version:
            raise self._protocol_failure("Node capabilities worker version is malformed")
        if not isinstance(methods, list) or not all(
            isinstance(value, str) and value for value in methods
        ):
            raise self._protocol_failure("Node capabilities methods are malformed")
        if not isinstance(adapters, list) or not all(
            isinstance(value, str) and value in SUPPORTED_ADAPTERS for value in adapters
        ):
            raise self._protocol_failure("Node capabilities adapters are malformed")
        if not isinstance(runtime, dict):
            raise self._protocol_failure("Node capabilities runtime is malformed")
        runtime_name = runtime.get("name")
        runtime_version = runtime.get("version")
        if not isinstance(runtime_name, str) or not runtime_name:
            raise self._protocol_failure("Node runtime name is malformed")
        if not isinstance(runtime_version, str) or not runtime_version:
            raise self._protocol_failure("Node runtime version is malformed")
        if tuple(methods) != EXPECTED_METHODS:
            raise self._protocol_failure("Node capabilities method set is malformed")
        if tuple(adapters) != EXPECTED_ADAPTERS:
            raise self._protocol_failure("Node capabilities adapter set is malformed")
        if runtime_name != "node":
            raise self._protocol_failure("Node capabilities runtime is unsupported")

        return NodeCapabilities(
            worker_version=worker_version,
            runtime_name=runtime_name,
            runtime_version=runtime_version,
            methods=tuple(methods),
            adapters=tuple(adapters),
        )

    def probe_url(self, url: str) -> AtsUrlProbe:
        if not isinstance(url, str) or not url:
            raise ValueError("url must be a non-empty string")
        result = self.request("probe_url", {"url": url})
        adapter = result.get("adapter")
        supported = result.get("supported")
        deterministic = result.get("deterministic")
        match_basis = result.get("match_basis")
        if not isinstance(adapter, str) or adapter not in SUPPORTED_ADAPTERS:
            raise self._protocol_failure("Node URL probe adapter is malformed")
        if not isinstance(supported, bool) or supported != (adapter != "generic"):
            raise self._protocol_failure("Node URL probe support flag is malformed")
        if deterministic is not True:
            raise self._protocol_failure("Node URL probe determinism flag is malformed")
        if match_basis != "hostname":
            raise self._protocol_failure("Node URL probe match basis is malformed")
        return AtsUrlProbe(
            adapter=adapter,
            supported=supported,
            deterministic=True,
            match_basis=match_basis,
        )
