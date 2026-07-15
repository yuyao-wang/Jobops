"""Focused tests for pluggable LLM backends."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import llm


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


def test_codex_cli_backend_uses_ephemeral_read_only_exec(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/local/bin/codex")
    monkeypatch.setenv("CODEX_THREAD_ID", "must-not-leak")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="codex result", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    backend = llm.CodexCLIBackend({"model": "gpt-test", "timeout": 42})

    assert backend.ask("score this job") == "codex result"
    assert captured["command"] == [
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--color",
        "never",
        "--model",
        "gpt-test",
        "-",
    ]
    assert captured["kwargs"]["input"] == "score this job"
    assert captured["kwargs"]["timeout"] == 42
    assert "CODEX_THREAD_ID" not in captured["kwargs"]["env"]


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
    assert captured["kwargs"]["timeout"] == 33


def test_openai_api_backend_resolves_env_placeholder(monkeypatch):
    monkeypatch.setenv("CUSTOM_OPENAI_KEY", "resolved-secret")
    backend = llm.OpenAIAPIBackend(
        {"api_key": "${CUSTOM_OPENAI_KEY}", "model": "gpt-test"}
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
            payload={"error": {"message": "Rate limit reached"}},
            headers={"x-request-id": "req_test"},
        )

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    backend = llm.OpenAIAPIBackend({"model": "gpt-test"})

    with pytest.raises(RuntimeError) as exc_info:
        backend.ask("hello")

    message = str(exc_info.value)
    assert "429" in message
    assert "req_test" in message
    assert "Rate limit reached" in message
    assert "must-not-appear" not in message


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
