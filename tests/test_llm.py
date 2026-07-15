"""Focused tests for pluggable LLM backends."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import llm
from utils.brain import ClaudeBrain


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class InvalidJSONBackend(llm.LLMBackend):
    def ask(self, prompt: str, timeout: int = 120) -> str:
        return "candidate-private-canary is not JSON"


def test_json_parse_errors_never_echo_model_or_candidate_text():
    with pytest.raises(ValueError) as exc_info:
        InvalidJSONBackend().ask_json("synthetic prompt")

    assert "candidate-private-canary" not in str(exc_info.value)


def test_codex_cli_backend_uses_ephemeral_read_only_exec(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/codex")
    monkeypatch.setenv("CODEX_THREAD_ID", "must-not-leak")
    monkeypatch.setenv("CANDIDATE_SECRET", "must-not-leak")
    monkeypatch.setenv("CODEX_HOME", "/tmp/synthetic-codex-home")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        captured["workspace_existed"] = Path(kwargs["cwd"]).is_dir()
        return subprocess.CompletedProcess(command, 0, stdout="codex result", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    backend = llm.CodexCLIBackend({"model": "gpt-test", "timeout": 42})

    assert backend.ask("score this job") == "codex result"
    assert captured["command"] == [
        "/usr/local/bin/codex",
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
        captured["kwargs"]["cwd"],
        "--config",
        'shell_environment_policy.inherit="none"',
        "--model",
        "gpt-test",
        "-",
    ]
    assert captured["kwargs"]["input"] == "score this job"
    assert captured["kwargs"]["timeout"] == 42
    assert captured["workspace_existed"] is True
    assert not Path(captured["kwargs"]["cwd"]).exists()
    assert "CODEX_THREAD_ID" not in captured["kwargs"]["env"]
    assert "CANDIDATE_SECRET" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["env"]["CODEX_HOME"] == "/tmp/synthetic-codex-home"


def test_claude_cli_backend_disables_tools_and_uses_isolated_workspace(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setenv("CANDIDATE_SECRET", "must-not-leak")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        captured["workspace_existed"] = Path(kwargs["cwd"]).is_dir()
        return subprocess.CompletedProcess(
            command, 0, stdout='{"result":"claude result"}', stderr=""
        )

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    backend = llm.ClaudeCLIBackend({"timeout": 17})

    assert backend.ask("trusted prompt") == "claude result"
    command = captured["command"]
    assert command[0] == "/usr/local/bin/claude"
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in command
    assert "--no-chrome" in command
    assert "--no-session-persistence" in command
    assert captured["workspace_existed"] is True
    assert not Path(captured["kwargs"]["cwd"]).exists()
    assert "CANDIDATE_SECRET" not in captured["kwargs"]["env"]


def test_codex_cli_error_redacts_auth_environment(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/codex")
    monkeypatch.setenv("OPENAI_API_KEY", "auth-secret-canary")
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr="bad auth-secret-canary"
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        llm.CodexCLIBackend().ask("trusted prompt")

    assert "[REDACTED]" in str(exc_info.value)
    assert "auth-secret-canary" not in str(exc_info.value)


def test_openai_api_backend_calls_responses_and_extracts_text(monkeypatch):
    monkeypatch.setenv("MR_JOBS_OPENAI_KEY", "test-secret")
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return DummyResponse(
            payload={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "first"},
                            {"type": "output_text", "text": "second"},
                        ],
                    }
                ],
            }
        )

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    backend = llm.OpenAIAPIBackend(
        {
            "api_key_env": "MR_JOBS_OPENAI_KEY",
            "model": "gpt-test",
            "base_url": "https://example.test/v1/",
            "timeout": 33,
            "reasoning_effort": "low",
            "max_output_tokens": 900,
        }
    )

    assert backend.ask("match this role") == "first\nsecond"
    assert captured["url"] == "https://example.test/v1/responses"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer test-secret"
    assert captured["kwargs"]["json"] == {
        "model": "gpt-test",
        "input": "match this role",
        "store": False,
        "max_output_tokens": 900,
        "reasoning": {"effort": "low"},
    }
    assert "tools" not in captured["kwargs"]["json"]
    assert captured["kwargs"]["timeout"] == 33


def test_openai_api_backend_resolves_env_placeholder(monkeypatch):
    monkeypatch.setenv("CUSTOM_OPENAI_KEY", "resolved-secret")
    backend = llm.OpenAIAPIBackend(
        {"api_key": "${CUSTOM_OPENAI_KEY}", "model": "gpt-test"}
    )

    assert backend.api_key == "resolved-secret"


def test_openai_api_backend_rejects_literal_keys_and_accepts_env_name_placeholder(
    monkeypatch,
):
    with pytest.raises(ValueError, match="Literal OpenAI API keys are forbidden"):
        llm.OpenAIAPIBackend({"api_key": "literal-secret", "model": "gpt-test"})

    monkeypatch.setenv("CUSTOM_OPENAI_KEY", "resolved-secret")
    backend = llm.OpenAIAPIBackend(
        {"api_key_env": "${CUSTOM_OPENAI_KEY}", "model": "gpt-test"}
    )
    assert backend.api_key == "resolved-secret"


def test_openai_api_backend_requires_key_and_model(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="API key not found"):
        llm.OpenAIAPIBackend({"model": "gpt-test"})

    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    with pytest.raises(RuntimeError, match="model not configured"):
        llm.OpenAIAPIBackend()


def test_openai_api_backend_reports_safe_api_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-appear")

    def fake_post(*args, **kwargs):
        return DummyResponse(
            status_code=429,
            payload={
                "error": {
                    "message": "Rate limit reached for must-not-appear",
                    "code": "rate_limit_exceeded",
                }
            },
            headers={"x-request-id": "req_must-not-appear"},
        )

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    backend = llm.OpenAIAPIBackend({"model": "gpt-test"})

    with pytest.raises(RuntimeError) as exc_info:
        backend.ask("hello")

    message = str(exc_info.value)
    assert "429" in message
    assert "request_id" not in message
    assert "rate_limit_exceeded" in message
    assert "must-not-appear" not in message


def test_openai_base_url_rejects_embedded_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")

    with pytest.raises(ValueError, match="credential-free"):
        llm.OpenAIAPIBackend(
            {
                "model": "gpt-test",
                "base_url": "https://user:transport-secret@example.test/v1",
            }
        )


@pytest.mark.parametrize("backend_name", ["codex_cli", "claude_cli"])
def test_agentic_cli_backends_are_rejected_for_untrusted_browser_input(
    monkeypatch, backend_name
):
    llm.clear_backend_cache()
    monkeypatch.setattr(
        llm.shutil,
        "which",
        lambda name: (_ for _ in ()).throw(AssertionError("CLI must not start")),
    )
    profile = {
        "ai": {
            "default_backend": backend_name,
            "backends": {backend_name: {}},
            "components": {"form_analysis": backend_name},
        }
    }

    with pytest.raises(llm.UnsafeLLMBackendError, match="not approved"):
        llm.require_safe_backend_for_untrusted_input("form_analysis", profile)


def test_cached_trusted_cli_backend_cannot_bypass_untrusted_gate(monkeypatch):
    llm.clear_backend_cache()
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/codex")
    profile = {
        "ai": {
            "default_backend": "codex_cli",
            "backends": {"codex_cli": {}},
            "components": {"form_analysis": "codex_cli"},
        }
    }
    assert isinstance(llm.get_backend("form_analysis", profile), llm.CodexCLIBackend)

    with pytest.raises(llm.UnsafeLLMBackendError, match="not approved"):
        llm.require_safe_backend_for_untrusted_input("form_analysis", profile)


def test_tool_free_openai_backend_is_safe_for_untrusted_browser_input(monkeypatch):
    llm.clear_backend_cache()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret")
    profile = {
        "ai": {
            "default_backend": "codex_cli",
            "backends": {"openai_api": {"model": "gpt-test"}},
            "components": {"form_analysis": "openai_api"},
        }
    }

    backend = llm.require_safe_backend_for_untrusted_input(
        "form_analysis", profile
    )
    assert isinstance(backend, llm.OpenAIAPIBackend)
    assert backend.safe_for_untrusted_input is True


def test_brain_enforces_untrusted_boundary_for_form_analysis(monkeypatch):
    llm.clear_backend_cache()
    monkeypatch.setattr(
        llm.shutil,
        "which",
        lambda name: (_ for _ in ()).throw(AssertionError("CLI must not start")),
    )
    brain = object.__new__(ClaudeBrain)
    brain.verbose = False
    brain.profile = {
        "ai": {
            "default_backend": "codex_cli",
            "backends": {"codex_cli": {}},
            "components": {"form_analysis": "codex_cli"},
        }
    }

    with pytest.raises(llm.UnsafeLLMBackendError, match="not approved"):
        brain.ask_json("untrusted label", component="form_analysis")


def test_get_backend_supports_openai_api_and_legacy_alias(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    llm.clear_backend_cache()

    for backend_name in ("openai_api", "openai"):
        profile = {
            "ai": {
                "default_backend": backend_name,
                "backends": {backend_name: {"model": "gpt-test"}},
            }
        }
        assert isinstance(llm.get_backend("scoring", profile), llm.OpenAIAPIBackend)

    llm.clear_backend_cache()
