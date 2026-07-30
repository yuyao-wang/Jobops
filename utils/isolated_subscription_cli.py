"""Seatbelt-isolated, bounded subscription CLI structured execution."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from PIL import Image

from core.isolated_model_runner import (
    IsolatedStructuredModelRequest,
    IsolatedStructuredModelResult,
    IsolatedStructuredModelStatus,
    SubscriptionCLIInvocationAdapter,
    SubscriptionCLIProcessSpec,
)
from core.model_provider_capabilities import (
    ModelExecutionIsolationProfile,
    model_execution_isolation_profiles,
)


_CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "code_mode_host",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "apps",
    "plugins",
    "tool_call_mcp_elicitation",
    "image_generation",
    "standalone_web_search",
)
_FORBIDDEN_EVENT_ITEMS = frozenset(
    {
        "command_execution",
        "computer_tool_call",
        "file_change",
        "mcp_tool_call",
        "web_search",
    }
)
_ALLOWED_CHILD_ENVIRONMENT = frozenset(
    {"CODEX_HOME", "HOME", "LANG", "PATH", "TMPDIR"}
)
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _HandledRunnerResult(Exception):
    """Internal control flow after a typed result has already been built."""


def _result(
    request: IsolatedStructuredModelRequest,
    profile: ModelExecutionIsolationProfile,
    *,
    backend_id: str,
    model_id: str,
    status: IsolatedStructuredModelStatus,
    output: Mapping[str, Any] | None,
    input_bytes: int,
    output_bytes: int,
    duration_ms: int,
    diagnostic: str,
) -> IsolatedStructuredModelResult:
    return IsolatedStructuredModelResult(
        status=status,
        output=output,
        backend_id=backend_id,
        model_id=model_id,
        component_id=request.component_id,
        prompt_contract_version=request.prompt_contract_version,
        schema_contract_version=request.schema_contract_version,
        isolation_profile_id=profile.isolation_profile_id,
        isolation_contract_version=profile.isolation_contract_version,
        execution_identity=request.execution_identity(
            backend_id=backend_id,
            isolation_profile_id=profile.isolation_profile_id,
            isolation_contract_version=profile.isolation_contract_version,
        ),
        input_byte_count=input_bytes,
        output_byte_count=output_bytes,
        image_count=len(request.images),
        duration_ms=duration_ms,
        diagnostic_category=diagnostic,
    )


def _validate_image(content: bytes, media_type: str) -> bool:
    expected = "PNG" if media_type == "image/png" else "JPEG"
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != expected:
                return False
            width, height = image.size
            if width < 1 or height < 1 or width > 4096 or height > 4096:
                return False
            if width * height > 16_000_000:
                return False
            image.verify()
    except (OSError, ValueError):
        return False
    return True


def _seatbelt_profile(
    *,
    workspace: Path,
    session_home: Path,
    spec: SubscriptionCLIProcessSpec,
) -> str:
    def literal(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    process_rules = " ".join(
        f'(literal "{literal(path)}")'
        for path in spec.allowed_process_executables
    )
    read_roots = (
        "/System",
        "/Library",
        "/usr/lib",
        "/usr/share",
        "/private/etc",
        "/dev",
        *spec.executable_read_roots,
        str(workspace),
        str(session_home),
    )
    reads = " ".join(
        f'(subpath "{literal(path)}")' for path in read_roots
    )
    return (
        '(version 1) (deny default) '
        '(import "/System/Library/Sandbox/Profiles/system.sb") '
        "(allow file-read-metadata) "
        f"(allow process-exec {process_rules}) "
        f"(allow file-read* {reads}) "
        f'(allow file-write* (subpath "{literal(str(workspace))}") '
        f'(subpath "{literal(str(session_home))}")) '
        "(allow network-outbound)"
    )


async def _read_bounded(
    stream: asyncio.StreamReader, maximum: int
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(min(65536, maximum + 1))
        if not chunk:
            return b"".join(chunks), False
        size += len(chunk)
        if size > maximum:
            return b"".join(chunks), True
        chunks.append(chunk)


def _terminate_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()


class IsolatedSubscriptionCLIRunner:
    """One invocation in a fresh macOS Seatbelt workspace."""

    def __init__(
        self,
        *,
        sandbox_executable: str = "/usr/bin/sandbox-exec",
        temporary_root: str | None = None,
        cleanup=shutil.rmtree,
    ) -> None:
        self._sandbox_executable = sandbox_executable
        self._temporary_root = temporary_root
        self._cleanup = cleanup

    async def execute(
        self,
        request: IsolatedStructuredModelRequest,
        *,
        backend_adapter: SubscriptionCLIInvocationAdapter,
        isolation_profile: ModelExecutionIsolationProfile,
    ) -> IsolatedStructuredModelResult:
        started = time.monotonic()
        backend_id = getattr(backend_adapter, "backend_id", "subscription_cli")
        model_id = request.model_id or "cli-default"
        input_bytes = request.input_bytes()
        total_input_bytes = request.total_input_byte_count()
        if (
            not isolation_profile.runner_available
            or isolation_profile.isolation_profile_id
            != "ISOLATED_SUBSCRIPTION_CLI_V1"
            or not Path(self._sandbox_executable).is_file()
        ):
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.ISOLATION_UNAVAILABLE,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=0,
                diagnostic="ISOLATION_UNAVAILABLE",
            )
        if total_input_bytes > min(
            request.max_input_bytes, isolation_profile.max_input_bytes
        ):
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.TEXT_INPUT_TOO_LARGE,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=0,
                diagnostic="TEXT_INPUT_TOO_LARGE",
            )
        if len(request.images) > min(
            request.max_images, 4
        ):
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.IMAGE_INPUT_TOO_LARGE,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=0,
                diagnostic="IMAGE_COUNT",
            )
        if request.images and not bool(
            getattr(backend_adapter, "supports_image_input", False)
        ):
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.IMAGE_INPUT_UNSUPPORTED,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=0,
                diagnostic="IMAGE_INPUT_UNSUPPORTED",
            )
        image_bytes = sum(image.byte_size for image in request.images)
        if image_bytes + total_input_bytes > min(
            request.max_input_bytes, isolation_profile.max_input_bytes
        ) or any(image.byte_size > 4_000_000 for image in request.images):
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.IMAGE_INPUT_TOO_LARGE,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=0,
                diagnostic="IMAGE_BYTES",
            )
        if any(
            not _validate_image(image.content, image.media_type)
            for image in request.images
        ):
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.IMAGE_INPUT_INVALID,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=0,
                diagnostic="IMAGE_INVALID",
            )
        try:
            Draft202012Validator.check_schema(dict(request.output_schema))
        except Exception:
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.SCHEMA_OUTPUT_INVALID,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=0,
                diagnostic="SCHEMA_INVALID",
            )
        if not backend_adapter.probe_contract():
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=(
                    IsolatedStructuredModelStatus.CLI_CONTRACT_UNSUPPORTED
                ),
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=0,
                diagnostic="CLI_CONTRACT_UNSUPPORTED",
            )

        if self._temporary_root is not None:
            Path(self._temporary_root).mkdir(mode=0o700, parents=True, exist_ok=True)
        root = Path(
            tempfile.mkdtemp(
                prefix="jobops-isolated-cli-",
                dir=self._temporary_root,
            )
        ).resolve()
        root.chmod(0o700)
        workspace = root / "workspace"
        session_home = root / "session"
        workspace.mkdir(mode=0o700)
        session_home.mkdir(mode=0o700)
        final_result: IsolatedStructuredModelResult | None = None
        try:
            if not backend_adapter.project_subscription_session(
                str(session_home)
            ):
                final_result = _result(
                    request,
                    isolation_profile,
                    backend_id=backend_id,
                    model_id=model_id,
                    status=(
                        IsolatedStructuredModelStatus
                        .AUTHENTICATION_UNAVAILABLE
                    ),
                    output=None,
                    input_bytes=total_input_bytes,
                    output_bytes=0,
                    duration_ms=0,
                    diagnostic="AUTHENTICATION_UNAVAILABLE",
                )
            else:
                schema_path = workspace / "output-schema.json"
                schema_path.write_text(
                    json.dumps(
                        request.output_schema,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                image_paths: list[str] = []
                for image in request.images:
                    suffix = ".png" if image.media_type == "image/png" else ".jpg"
                    path = workspace / f"image-{image.order:02d}{suffix}"
                    path.write_bytes(image.content)
                    if (
                        path.is_symlink()
                        or path.stat().st_size != image.byte_size
                        or hashlib.sha256(path.read_bytes()).hexdigest()
                        != image.sha256
                    ):
                        raise OSError("managed image integrity failure")
                    image_paths.append(str(path))
                spec = backend_adapter.build_process_spec(
                    request,
                    workspace=str(workspace),
                    session_home=str(session_home),
                    schema_path=str(schema_path),
                    image_paths=tuple(image_paths),
                )
                if (
                    not spec.argv
                    or any(
                        key not in _ALLOWED_CHILD_ENVIRONMENT
                        for key in spec.environment
                    )
                    or any(
                        not isinstance(key, str)
                        or not isinstance(value, str)
                        for key, value in spec.environment.items()
                    )
                ):
                    final_result = _result(
                        request,
                        isolation_profile,
                        backend_id=backend_id,
                        model_id=model_id,
                        status=(
                            IsolatedStructuredModelStatus
                            .CLI_CONTRACT_UNSUPPORTED
                        ),
                        output=None,
                        input_bytes=total_input_bytes,
                        output_bytes=0,
                        duration_ms=int(
                            (time.monotonic() - started) * 1000
                        ),
                        diagnostic="CLI_CONTRACT_UNSUPPORTED",
                    )
                    raise _HandledRunnerResult
                profile = _seatbelt_profile(
                    workspace=workspace,
                    session_home=session_home,
                    spec=spec,
                )
                process = await asyncio.create_subprocess_exec(
                    self._sandbox_executable,
                    "-p",
                    profile,
                    *spec.argv,
                    cwd=str(workspace),
                    env=dict(spec.environment),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                assert process.stdin and process.stdout and process.stderr
                process.stdin.write(input_bytes)
                await process.stdin.drain()
                process.stdin.close()
                output_limit = min(
                    request.max_output_bytes,
                    isolation_profile.max_output_bytes,
                )
                stdout_task = asyncio.create_task(
                    _read_bounded(process.stdout, output_limit)
                )
                stderr_task = asyncio.create_task(
                    _read_bounded(process.stderr, 8192)
                )
                process_task = asyncio.create_task(process.wait())
                timeout = min(
                    request.timeout_seconds,
                    isolation_profile.max_wall_time_seconds,
                )
                deadline = time.monotonic() + timeout
                stdout_pair: tuple[bytes, bool] | None = None
                stderr_pair: tuple[bytes, bool] | None = None
                timed_out = False
                while process.returncode is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    done, _ = await asyncio.wait(
                        {stdout_task, stderr_task, process_task},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        timed_out = True
                        break
                    if stdout_task in done and stdout_pair is None:
                        stdout_pair = stdout_task.result()
                        if stdout_pair[1]:
                            break
                    if stderr_task in done and stderr_pair is None:
                        stderr_pair = stderr_task.result()
                        if stderr_pair[1]:
                            break
                    if process_task in done:
                        break
                early_oversized = bool(
                    (stdout_pair and stdout_pair[1])
                    or (stderr_pair and stderr_pair[1])
                )
                if timed_out or early_oversized:
                    _terminate_group(process)
                    await process.wait()
                    for task in (stdout_task, stderr_task, process_task):
                        if not task.done():
                            task.cancel()
                    status = (
                        IsolatedStructuredModelStatus.OUTPUT_TOO_LARGE
                        if early_oversized
                        else IsolatedStructuredModelStatus.TIMEOUT
                    )
                    final_result = _result(
                        request,
                        isolation_profile,
                        backend_id=backend_id,
                        model_id=spec.model_id,
                        status=status,
                        output=None,
                        input_bytes=total_input_bytes,
                        output_bytes=0,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        diagnostic=status.value,
                    )
                else:
                    stdout_pair = stdout_pair or await stdout_task
                    stderr_pair = stderr_pair or await stderr_task
                    returncode = await process_task
                    stdout, stdout_oversized = stdout_pair
                    _, stderr_oversized = stderr_pair
                    result_path = workspace / spec.result_file_name
                    result_bytes = (
                        result_path.read_bytes()
                        if result_path.is_file()
                        else b""
                    )
                    oversized = (
                        stdout_oversized
                        or stderr_oversized
                        or len(result_bytes) > output_limit
                    )
                    if oversized:
                        _terminate_group(process)
                        final_result = _result(
                            request,
                            isolation_profile,
                            backend_id=backend_id,
                            model_id=spec.model_id,
                            status=(
                                IsolatedStructuredModelStatus.OUTPUT_TOO_LARGE
                            ),
                            output=None,
                            input_bytes=total_input_bytes,
                            output_bytes=min(len(stdout), output_limit),
                            duration_ms=int(
                                (time.monotonic() - started) * 1000
                            ),
                            diagnostic="OUTPUT_TOO_LARGE",
                        )
                    else:
                        try:
                            tool_attempted, parsed = (
                                backend_adapter.parse_process_output(
                                    stdout, result_bytes
                                )
                            )
                        except (TypeError, ValueError):
                            tool_attempted, parsed = False, {}
                        if tool_attempted:
                            status = (
                                IsolatedStructuredModelStatus.TOOL_ATTEMPTED
                            )
                            parsed_output = None
                            diagnostic = "TOOL_ATTEMPTED"
                        elif returncode != 0:
                            status = (
                                IsolatedStructuredModelStatus.PROCESS_FAILED
                            )
                            parsed_output = None
                            diagnostic = "PROCESS_FAILED"
                        else:
                            try:
                                Draft202012Validator(
                                    dict(request.output_schema)
                                ).validate(parsed)
                            except Exception:
                                status = (
                                    IsolatedStructuredModelStatus
                                    .SCHEMA_OUTPUT_INVALID
                                )
                                parsed_output = None
                                diagnostic = "SCHEMA_OUTPUT_INVALID"
                            else:
                                status = (
                                    IsolatedStructuredModelStatus.SUCCEEDED
                                )
                                parsed_output = parsed
                                diagnostic = "NONE"
                        final_result = _result(
                            request,
                            isolation_profile,
                            backend_id=backend_id,
                            model_id=spec.model_id,
                            status=status,
                            output=parsed_output,
                            input_bytes=total_input_bytes,
                            output_bytes=len(result_bytes),
                            duration_ms=int(
                                (time.monotonic() - started) * 1000
                            ),
                            diagnostic=diagnostic,
                        )
        except _HandledRunnerResult:
            pass
        except (OSError, RuntimeError, TypeError, ValueError):
            final_result = _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.PROCESS_FAILED,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=int((time.monotonic() - started) * 1000),
                diagnostic="PROCESS_FAILED",
            )
        try:
            self._cleanup(root)
        except (OSError, RuntimeError):
            shutil.rmtree(root, ignore_errors=True)
            return _result(
                request,
                isolation_profile,
                backend_id=backend_id,
                model_id=model_id,
                status=IsolatedStructuredModelStatus.CLEANUP_FAILED,
                output=None,
                input_bytes=total_input_bytes,
                output_bytes=0,
                duration_ms=int((time.monotonic() - started) * 1000),
                diagnostic="CLEANUP_FAILED",
            )
        assert final_result is not None
        return final_result


class CodexSubscriptionCLIInvocationAdapter:
    """Thin Codex CLI contract over the provider-neutral isolation runner."""

    backend_id = "codex_cli"
    supports_image_input = True

    def __init__(
        self,
        *,
        executable: str | None = None,
        source_codex_home: str | None = None,
    ) -> None:
        self.executable = executable or shutil.which("codex") or ""
        self.source_codex_home = Path(
            source_codex_home
            or os.environ.get("CODEX_HOME")
            or (Path.home() / ".codex")
        )

    def probe_contract(self) -> bool:
        if not self.executable or not Path(self.executable).is_file():
            return False
        try:
            probe = subprocess.run(
                [self.executable, "exec", "--help"],
                capture_output=True,
                text=True,
                timeout=5,
                env={"PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.SubprocessError):
            return False
        output = probe.stdout
        return (
            probe.returncode == 0
            and "--output-schema" in output
            and "--image" in output
            and "--ephemeral" in output
            and "--ignore-user-config" in output
        )

    def project_subscription_session(self, destination: str) -> bool:
        source = self.source_codex_home / "auth.json"
        target_home = Path(destination)
        if not source.is_file() or source.is_symlink():
            return False
        try:
            content = source.read_bytes()
            value = json.loads(content)
            if not isinstance(value, dict) or not content:
                return False
            tokens = value.get("tokens")
            if (
                value.get("auth_mode") != "chatgpt"
                or not isinstance(tokens, dict)
                or any(
                    not isinstance(tokens.get(name), str)
                    or not tokens[name]
                    for name in (
                        "access_token",
                        "account_id",
                        "refresh_token",
                    )
                )
                or value.get("OPENAI_API_KEY") not in (None, "")
            ):
                return False
            target = target_home / "auth.json"
            target.write_bytes(content)
            target.chmod(0o600)
        except (OSError, ValueError):
            return False
        return True

    def build_process_spec(
        self,
        request: IsolatedStructuredModelRequest,
        *,
        workspace: str,
        session_home: str,
        schema_path: str,
        image_paths: tuple[str, ...],
    ) -> SubscriptionCLIProcessSpec:
        argv = [
            self.executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--strict-config",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--cd",
            workspace,
            "--config",
            'shell_environment_policy.inherit="none"',
            "--config",
            "developer_instructions="
            + json.dumps(request.system_prompt, ensure_ascii=False),
            "--json",
            "--output-schema",
            schema_path,
            "--output-last-message",
            str(Path(workspace) / "result.json"),
        ]
        for feature in _CODEX_DISABLED_FEATURES:
            argv.extend(["--disable", feature])
        if request.model_id:
            argv.extend(["--model", request.model_id])
        for image_path in image_paths:
            argv.extend(["--image", image_path])
        argv.append("-")
        executable = str(Path(self.executable).resolve())
        resource_root = str(Path(executable).parent)
        return SubscriptionCLIProcessSpec(
            backend_id=self.backend_id,
            model_id=request.model_id or "codex-cli-default",
            argv=tuple(argv),
            environment={
                "CODEX_HOME": session_home,
                "HOME": session_home,
                "LANG": "en_US.UTF-8",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": workspace,
            },
            result_file_name="result.json",
            executable_read_roots=(resource_root,),
            allowed_process_executables=(executable,),
        )

    def parse_process_output(
        self, stdout: bytes, result_bytes: bytes
    ) -> tuple[bool, Mapping[str, Any]]:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("Codex event stream is invalid") from None
            if not isinstance(event, dict):
                raise ValueError("Codex event is invalid")
            item = event.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            event_type = str(event.get("type", "")).lower()
            if item_type in _FORBIDDEN_EVENT_ITEMS or any(
                marker in event_type
                for marker in ("tool_call", "command_execution", "web_search")
            ):
                return True, {}
        try:
            parsed = json.loads(result_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Codex structured result is invalid") from None
        if not isinstance(parsed, dict):
            raise ValueError("Codex structured result must be an object")
        return False, parsed


def probe_isolated_subscription_cli_runtime(
    adapter: CodexSubscriptionCLIInvocationAdapter,
    *,
    sandbox_executable: str = "/usr/bin/sandbox-exec",
) -> bool:
    """No-generation runtime probe for Seatbelt, CLI contract, and auth projection."""

    if (
        not Path(sandbox_executable).is_file()
        or not adapter.probe_contract()
    ):
        return False
    root = Path(
        tempfile.mkdtemp(prefix="jobops-isolation-probe-")
    ).resolve()
    try:
        workspace = root / "workspace"
        session = root / "session"
        workspace.mkdir(mode=0o700)
        session.mkdir(mode=0o700)
        if not adapter.project_subscription_session(str(session)):
            return False
        login_status = subprocess.run(
            [adapter.executable, "login", "status"],
            cwd=workspace,
            env={
                "CODEX_HOME": str(session),
                "HOME": str(session),
                "LANG": "en_US.UTF-8",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(workspace),
            },
            capture_output=True,
            text=True,
            timeout=5,
        )
        if (
            login_status.returncode != 0
            or "logged in"
            not in (login_status.stdout + login_status.stderr).lower()
        ):
            return False
        (workspace / "probe-schema.json").write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (workspace / "probe.png").write_bytes(_TINY_PNG)

        sentinel = root / "outside-sentinel"
        sentinel.write_text("synthetic-sentinel", encoding="utf-8")
        result = workspace / "boundary-result"
        fake_spec = SubscriptionCLIProcessSpec(
            backend_id="probe",
            model_id="probe",
            argv=("/bin/sh",),
            environment={},
            result_file_name="unused",
            executable_read_roots=("/bin",),
            allowed_process_executables=("/bin/sh", "/bin/bash"),
        )
        profile = _seatbelt_profile(
            workspace=workspace,
            session_home=session,
            spec=fake_spec,
        )
        script = (
            'if read x < "$OUTSIDE"; then exit 41; fi; '
            'printf "outside-denied" > "$RESULT"; '
            "/usr/bin/true"
        )
        boundary = subprocess.run(
            [
                sandbox_executable,
                "-p",
                profile,
                "/bin/sh",
                "-c",
                script,
            ],
            cwd="/",
            env={
                "OUTSIDE": str(sentinel),
                "PATH": "/usr/bin:/bin",
                "RESULT": str(result),
            },
            capture_output=True,
            timeout=5,
        )
        if (
            boundary.returncode == 0
            or not result.is_file()
            or result.read_text(encoding="utf-8") != "outside-denied"
        ):
            return False

        executable = str(Path(adapter.executable).resolve())
        cli_spec = SubscriptionCLIProcessSpec(
            backend_id="codex_cli",
            model_id="probe",
            argv=(executable,),
            environment={},
            result_file_name="unused",
            executable_read_roots=(str(Path(executable).parent),),
            allowed_process_executables=(executable,),
        )
        cli_profile = _seatbelt_profile(
            workspace=workspace,
            session_home=session,
            spec=cli_spec,
        )
        contract = subprocess.run(
            [
                sandbox_executable,
                "-p",
                cli_profile,
                executable,
                "exec",
                "--ignore-user-config",
                "--strict-config",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(workspace / "probe-schema.json"),
                "--image",
                str(workspace / "probe.png"),
                "--help",
            ],
            cwd=workspace,
            env={
                "CODEX_HOME": str(session),
                "HOME": str(session),
                "LANG": "en_US.UTF-8",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(workspace),
            },
            capture_output=True,
            text=True,
            timeout=5,
        )
        basic_contract = (
            contract.returncode == 0
            and "--output-schema" in contract.stdout
            and "--image" in contract.stdout
        )
        if not basic_contract:
            return False

        debug_home = root / "debug-home"
        debug_home.mkdir(mode=0o700)
        debug = subprocess.run(
            [
                executable,
                "debug",
                "prompt-input",
                "--config",
                'developer_instructions="SYNTHETIC_M1B_POLICY_MARKER"',
                *(
                    argument
                    for feature in _CODEX_DISABLED_FEATURES
                    for argument in ("--disable", feature)
                ),
                "--image",
                str(workspace / "probe.png"),
                "synthetic contract probe",
            ],
            cwd=workspace,
            env={
                "CODEX_HOME": str(debug_home),
                "HOME": str(debug_home),
                "LANG": "en_US.UTF-8",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": str(workspace),
            },
            capture_output=True,
            text=True,
            timeout=5,
        )
        if debug.returncode != 0:
            return False
        prompt_items = json.loads(debug.stdout)
        if not isinstance(prompt_items, list) or not prompt_items:
            return False
        if any(
            not isinstance(item, dict) or item.get("type") != "message"
            for item in prompt_items
        ):
            return False
        has_image = any(
            isinstance(content, dict)
            and content.get("type") in {"input_image", "image"}
            for item in prompt_items
            for content in (
                item.get("content", [])
                if isinstance(item.get("content"), list)
                else []
            )
        )
        has_developer_policy = any(
            item.get("role") == "developer"
            and any(
                isinstance(content, dict)
                and content.get("type") == "input_text"
                and "SYNTHETIC_M1B_POLICY_MARKER"
                in str(content.get("text", ""))
                for content in (
                    item.get("content", [])
                    if isinstance(item.get("content"), list)
                    else []
                )
            )
            for item in prompt_items
        )
        return has_image and has_developer_policy
    except (
        json.JSONDecodeError,
        OSError,
        subprocess.SubprocessError,
    ):
        return False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def runtime_model_execution_isolation_profiles(
    adapter: CodexSubscriptionCLIInvocationAdapter | None = None,
) -> Mapping[str, ModelExecutionIsolationProfile]:
    """Return M1a2 profiles with M1b availability derived from a real probe."""

    codex_adapter = adapter or CodexSubscriptionCLIInvocationAdapter()
    return model_execution_isolation_profiles(
        isolated_subscription_cli_runner_available=(
            probe_isolated_subscription_cli_runtime(codex_adapter)
        )
    )


__all__ = [
    "CodexSubscriptionCLIInvocationAdapter",
    "IsolatedSubscriptionCLIRunner",
    "probe_isolated_subscription_cli_runtime",
    "runtime_model_execution_isolation_profiles",
]
