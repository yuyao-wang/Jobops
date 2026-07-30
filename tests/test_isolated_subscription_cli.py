from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from core.isolated_model_runner import (
    IsolatedStructuredModelRequest,
    IsolatedStructuredModelStatus,
    ManagedModelImage,
    SubscriptionCLIProcessSpec,
)
from core.model_provider_capabilities import (
    model_execution_isolation_profiles,
    resolve_component_backend,
)
from utils.isolated_subscription_cli import (
    CodexSubscriptionCLIInvocationAdapter,
    IsolatedSubscriptionCLIRunner,
    probe_isolated_subscription_cli_runtime,
)
from utils.llm import model_backend_registry


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_HOST_SUPPORTS_SEATBELT = (
    sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()
)


def _request(
    *,
    images: tuple[ManagedModelImage, ...] = (),
    max_output_bytes: int = 4096,
    timeout_seconds: int = 3,
) -> IsolatedStructuredModelRequest:
    return IsolatedStructuredModelRequest(
        component_id="resume_visual_qa",
        invocation_id="synthetic-invocation-1",
        model_id="synthetic-model",
        system_prompt="Return the bounded synthetic result.",
        input_data={"synthetic": True},
        images=images,
        output_schema_name="synthetic_result",
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
        timeout_seconds=timeout_seconds,
        max_input_bytes=100_000,
        max_output_bytes=max_output_bytes,
        max_images=4,
        prompt_contract_version="synthetic-prompt-v1",
        schema_contract_version="synthetic-schema-v1",
    )


def _image(content: bytes = _PNG, *, order: int = 0) -> ManagedModelImage:
    return ManagedModelImage(
        media_type="image/png",
        content=content,
        byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        order=order,
        role_id=f"page-{order + 1}",
    )


class _FakeSubscriptionAdapter:
    backend_id = "fake_subscription_cli"
    supports_image_input = True

    def __init__(self, script: str) -> None:
        self.script = script
        self.build_calls = 0
        self.image_hashes: tuple[str, ...] = ()
        self.workspace: str | None = None

    def probe_contract(self) -> bool:
        return True

    def project_subscription_session(self, destination: str) -> bool:
        target = Path(destination) / "session.json"
        target.write_text('{"synthetic":true}', encoding="utf-8")
        target.chmod(0o600)
        return True

    def build_process_spec(
        self,
        request,
        *,
        workspace: str,
        session_home: str,
        schema_path: str,
        image_paths: tuple[str, ...],
    ) -> SubscriptionCLIProcessSpec:
        self.build_calls += 1
        self.workspace = workspace
        self.image_hashes = tuple(
            hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in image_paths
        )
        return SubscriptionCLIProcessSpec(
            backend_id=self.backend_id,
            model_id=request.model_id or "fake-default",
            argv=("/bin/sh", "-c", self.script),
            environment={
                "HOME": session_home,
                "LANG": "C",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": workspace,
            },
            result_file_name="result.json",
            executable_read_roots=("/bin",),
            allowed_process_executables=("/bin/sh", "/bin/bash"),
        )

    def parse_process_output(self, stdout: bytes, result_bytes: bytes):
        attempted = any(
            json.loads(line).get("type") == "tool_call"
            for line in stdout.splitlines()
            if line
        )
        return attempted, json.loads(result_bytes)


def _profile():
    return model_execution_isolation_profiles(
        isolated_subscription_cli_runner_available=True
    )["ISOLATED_SUBSCRIPTION_CLI_V1"]


@pytest.mark.skipif(
    not _HOST_SUPPORTS_SEATBELT, reason="requires macOS Seatbelt"
)
@pytest.mark.asyncio
async def test_isolated_runner_structured_image_success_and_cleanup(tmp_path):
    adapter = _FakeSubscriptionAdapter(
        """printf '%s\n' '{"type":"turn.completed"}'; """
        """printf '%s' '{"answer":"ok"}' > result.json"""
    )
    runner = IsolatedSubscriptionCLIRunner(temporary_root=str(tmp_path))
    result = await runner.execute(
        _request(images=(_image(),)),
        backend_adapter=adapter,
        isolation_profile=_profile(),
    )

    assert result.status is IsolatedStructuredModelStatus.SUCCEEDED
    assert result.output == {"answer": "ok"}
    assert result.image_count == 1
    assert adapter.image_hashes == (hashlib.sha256(_PNG).hexdigest(),)
    assert adapter.build_calls == 1
    assert adapter.workspace is not None
    assert not Path(adapter.workspace).exists()
    assert not any(tmp_path.iterdir())


@pytest.mark.skipif(
    not _HOST_SUPPORTS_SEATBELT, reason="requires macOS Seatbelt"
)
@pytest.mark.asyncio
async def test_isolated_runner_enforces_host_files_process_and_environment(
    tmp_path, monkeypatch
):
    sentinel = tmp_path / "outside-sentinel"
    sentinel.write_text("not-visible", encoding="utf-8")
    repository_sentinel = Path(__file__).resolve()
    monkeypatch.setenv("JOBOPS_SYNTHETIC_SECRET", "must-not-leak")
    boundary_script = (
        f"if read value < {shlex.quote(str(sentinel))}; "
        "then answer=breached; else answer=denied; fi; "
        f"if read value < {shlex.quote(str(repository_sentinel))}; "
        "then answer=breached; fi; "
        'if [ "${JOBOPS_SYNTHETIC_SECRET+x}" = x ]; '
        "then answer=breached; fi; "
        """printf '{"answer":"%s"}' "$answer" > result.json"""
    )
    boundary = await IsolatedSubscriptionCLIRunner(
        temporary_root=str(tmp_path / "runs")
    ).execute(
        _request(),
        backend_adapter=_FakeSubscriptionAdapter(boundary_script),
        isolation_profile=_profile(),
    )
    child = await IsolatedSubscriptionCLIRunner(
        temporary_root=str(tmp_path / "runs")
    ).execute(
        _request(),
        backend_adapter=_FakeSubscriptionAdapter(
            """printf '%s' '{"answer":"unexpected"}' > result.json; """
            "/usr/bin/true"
        ),
        isolation_profile=_profile(),
    )

    assert boundary.status is IsolatedStructuredModelStatus.SUCCEEDED
    assert boundary.output == {"answer": "denied"}
    assert child.status is IsolatedStructuredModelStatus.PROCESS_FAILED


@pytest.mark.asyncio
async def test_image_and_output_bounds_fail_closed_without_generation(tmp_path):
    adapter = _FakeSubscriptionAdapter(
        """printf '%s' '{"answer":"unused"}' > result.json"""
    )
    invalid = await IsolatedSubscriptionCLIRunner(
        temporary_root=str(tmp_path)
    ).execute(
        _request(images=(_image(b"not-a-real-png"),)),
        backend_adapter=adapter,
        isolation_profile=_profile(),
    )
    too_many = tuple(_image(order=index) for index in range(5))
    excessive = await IsolatedSubscriptionCLIRunner(
        temporary_root=str(tmp_path)
    ).execute(
        _request(images=too_many),
        backend_adapter=adapter,
        isolation_profile=_profile(),
    )

    assert invalid.status is IsolatedStructuredModelStatus.IMAGE_INPUT_INVALID
    assert excessive.status is IsolatedStructuredModelStatus.IMAGE_INPUT_TOO_LARGE
    assert adapter.build_calls == 0


@pytest.mark.skipif(
    shutil.which("codex") is None, reason="requires installed Codex CLI"
)
def test_codex_contract_subscription_projection_and_runtime_probe(tmp_path):
    source_home = tmp_path / "source"
    source_home.mkdir()
    (source_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {
                    "access_token": "synthetic-access",
                    "account_id": "synthetic-account",
                    "refresh_token": "synthetic-refresh",
                },
            }
        ),
        encoding="utf-8",
    )
    adapter = CodexSubscriptionCLIInvocationAdapter(
        executable=shutil.which("codex"),
        source_codex_home=str(source_home),
    )
    destination = tmp_path / "session"
    destination.mkdir()

    assert adapter.probe_contract()
    assert adapter.project_subscription_session(str(destination))
    projected = list(destination.iterdir())
    assert [path.name for path in projected] == ["auth.json"]
    assert oct(projected[0].stat().st_mode & 0o777) == "0o600"
    spec = adapter.build_process_spec(
        _request(images=(_image(),)),
        workspace=str(tmp_path / "workspace"),
        session_home=str(destination),
        schema_path=str(tmp_path / "schema.json"),
        image_paths=(str(tmp_path / "image.png"),),
    )
    assert "--output-schema" in spec.argv
    assert "--image" in spec.argv
    assert any(
        argument.startswith("developer_instructions=")
        for argument in spec.argv
    )
    assert b"Return the bounded synthetic result." not in (
        _request(images=(_image(),)).input_bytes()
    )
    assert "OPENAI_API_KEY" not in spec.environment
    assert "CODEX_API_KEY" not in spec.environment
    assert set(spec.environment) == {
        "CODEX_HOME",
        "HOME",
        "LANG",
        "PATH",
        "TMPDIR",
    }
    missing_home = tmp_path / "missing-session"
    missing_home.mkdir()
    assert not probe_isolated_subscription_cli_runtime(
        CodexSubscriptionCLIInvocationAdapter(
            executable=shutil.which("codex"),
            source_codex_home=str(missing_home),
        )
    )
    resolved = resolve_component_backend(
        ai_config={
            "default_backend": "codex_cli",
            "backends": {
                "codex_cli": {
                    "isolation_profile": "isolated_subscription_cli_v1"
                }
            },
            "components": {"resume_visual_qa": "codex_cli"},
        },
        component_id="resume_visual_qa",
        backend_registry=model_backend_registry(),
        isolation_profile_registry=model_execution_isolation_profiles(
            isolated_subscription_cli_runner_available=True
        ),
    )
    assert resolved.selected_backend_id == "codex_cli"
    assert resolved.isolation_profile_id == "ISOLATED_SUBSCRIPTION_CLI_V1"


@pytest.mark.skipif(
    not _HOST_SUPPORTS_SEATBELT, reason="requires macOS Seatbelt"
)
@pytest.mark.asyncio
async def test_runner_failure_categories_do_not_retry_and_redact(tmp_path):
    cases = (
        (
            """printf '%s\n' '{"type":"tool_call"}'; """
            """printf '%s' '{"answer":"ignored"}' > result.json""",
            _request(),
            IsolatedStructuredModelStatus.TOOL_ATTEMPTED,
        ),
        (
            """printf '%s' '{"wrong":"shape"}' > result.json""",
            _request(),
            IsolatedStructuredModelStatus.SCHEMA_OUTPUT_INVALID,
        ),
        (
            "while :; do :; done",
            _request(timeout_seconds=1),
            IsolatedStructuredModelStatus.TIMEOUT,
        ),
        (
            "i=0; while [ $i -lt 9000 ]; do printf x; i=$((i+1)); done",
            _request(max_output_bytes=128),
            IsolatedStructuredModelStatus.OUTPUT_TOO_LARGE,
        ),
    )
    for index, (script, request, expected) in enumerate(cases):
        adapter = _FakeSubscriptionAdapter(script)
        result = await IsolatedSubscriptionCLIRunner(
            temporary_root=str(tmp_path / f"case-{index}")
        ).execute(
            request,
            backend_adapter=adapter,
            isolation_profile=_profile(),
        )
        assert result.status is expected
        assert adapter.build_calls == 1
        rendered = repr(result)
        assert "JOBOPS_SYNTHETIC_SECRET" not in rendered
        assert str(tmp_path) not in rendered

    cleanup_adapter = _FakeSubscriptionAdapter(
        """printf '%s' '{"answer":"ok"}' > result.json"""
    )
    cleanup = await IsolatedSubscriptionCLIRunner(
        temporary_root=str(tmp_path / "cleanup"),
        cleanup=lambda path: (_ for _ in ()).throw(
            RuntimeError("synthetic cleanup failure")
        ),
    ).execute(
        _request(),
        backend_adapter=cleanup_adapter,
        isolation_profile=_profile(),
    )
    assert cleanup.status is IsolatedStructuredModelStatus.CLEANUP_FAILED
    assert cleanup_adapter.build_calls == 1
