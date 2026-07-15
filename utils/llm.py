"""
Pluggable LLM Backend — Route AI requests to different providers per component.

Supports Claude CLI (default), Codex CLI, and the OpenAI Responses API. Each
AI component (scoring, cover letters, resume tailoring, etc.) can be
independently configured to use a different backend.

Configuration in profile.yaml:
    ai:
      default_backend: claude_cli
      backends:
        claude_cli:
          timeout: 120
        codex_cli:
          timeout: 180
        openai_api:
          api_key_env: OPENAI_API_KEY
          model: <OpenAI model ID>
          timeout: 120
      components:
        scoring: claude_cli
        cover_letter: openai_api
"""

import os
import subprocess
import json
import re
import shutil
from abc import ABC, abstractmethod

import httpx


class LLMBackend(ABC):
    """Abstract base for all LLM backends."""

    @abstractmethod
    def ask(self, prompt: str, timeout: int = 120) -> str:
        """Send a prompt and return text response."""
        ...

    def ask_json(self, prompt: str, timeout: int = 120) -> dict:
        """Send a prompt and parse JSON from the response."""
        full_prompt = prompt + (
            "\n\nIMPORTANT: Respond ONLY with valid JSON. "
            "No markdown fencing, no explanation, no preamble. Just the JSON object."
        )
        raw = self.ask(full_prompt, timeout=timeout)
        cleaned = raw.strip()
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
        cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM didn't return valid JSON: {e}\nRaw: {raw[:500]}")


class ClaudeCLIBackend(LLMBackend):
    """Claude Code CLI backend (default). Uses `claude -p` subprocess."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.default_timeout = self.config.get("timeout", 120)

    def ask(self, prompt: str, timeout: int = None) -> str:
        timeout = timeout or self.default_timeout
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error"
            raise RuntimeError(f"Claude CLI error: {error_msg}")
        try:
            data = json.loads(result.stdout)
            return data.get("result", result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()


class CodexCLIBackend(LLMBackend):
    """Codex CLI backend for read-only scoring and form analysis."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.default_timeout = self.config.get("timeout", 180)
        self.model = self.config.get("model", "")
        if not shutil.which("codex"):
            raise RuntimeError("Codex CLI not found on PATH")

    def ask(self, prompt: str, timeout: int = None) -> str:
        timeout = timeout or self.default_timeout
        command = [
            "codex", "exec",
            "--ephemeral",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--color", "never",
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")

        env = {
            key: value for key, value in os.environ.items()
            if key not in {"CODEX_THREAD_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"}
        }
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            error_msg = result.stderr.strip() or "Unknown error"
            raise RuntimeError(f"Codex CLI error: {error_msg}")
        return result.stdout.strip()


class OpenAIAPIBackend(LLMBackend):
    """OpenAI Responses API backend using the existing httpx dependency."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.default_timeout = self.config.get("timeout", 120)
        self.model = self.config.get("model") or os.environ.get("OPENAI_MODEL", "")
        self.base_url = (
            self.config.get("base_url")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = self._resolve_api_key()
        self.organization = (
            self.config.get("organization")
            or os.environ.get("OPENAI_ORG_ID", "")
        )
        self.project = (
            self.config.get("project")
            or os.environ.get("OPENAI_PROJECT_ID", "")
        )
        self.max_output_tokens = self.config.get("max_output_tokens")
        self.reasoning_effort = self.config.get("reasoning_effort")
        self.store = self.config.get("store", False)

        if not self.api_key:
            env_name = self.config.get("api_key_env", "OPENAI_API_KEY")
            raise RuntimeError(
                f"OpenAI API key not found. Set {env_name} or configure "
                "ai.backends.openai_api.api_key_env."
            )
        if not self.model:
            raise RuntimeError(
                "OpenAI model not configured. Set "
                "ai.backends.openai_api.model or OPENAI_MODEL."
            )

    def _resolve_api_key(self) -> str:
        """Resolve a key without requiring secrets to be stored in profile.yaml."""
        configured = self.config.get("api_key", "")
        if isinstance(configured, str):
            configured = configured.strip()
            env_reference = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", configured)
            if env_reference:
                return os.environ.get(env_reference.group(1), "")
            if configured:
                return configured

        env_name = self.config.get("api_key_env", "OPENAI_API_KEY")
        return os.environ.get(env_name, "")

    def ask(self, prompt: str, timeout: int = None) -> str:
        timeout = timeout or self.default_timeout
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.organization:
            headers["OpenAI-Organization"] = self.organization
        if self.project:
            headers["OpenAI-Project"] = self.project

        payload = {
            "model": self.model,
            "input": prompt,
            "store": bool(self.store),
        }
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}

        try:
            response = httpx.post(
                f"{self.base_url}/responses",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"OpenAI API request failed: {exc}") from exc

        if response.status_code >= 400:
            self._raise_api_error(response)

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("OpenAI API returned invalid JSON") from exc

        return self._extract_response_text(data)

    @staticmethod
    def _raise_api_error(response) -> None:
        try:
            data = response.json()
        except ValueError:
            data = {}

        error = data.get("error", {}) if isinstance(data, dict) else {}
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
        else:
            message = str(error)
        message = message or response.text.strip() or "Unknown error"
        request_id = response.headers.get("x-request-id", "")
        request_suffix = f", request_id={request_id}" if request_id else ""
        raise RuntimeError(
            f"OpenAI API error ({response.status_code}{request_suffix}): {message}"
        )

    @staticmethod
    def _extract_response_text(data: dict) -> str:
        """Aggregate output_text parts from a raw Responses API payload."""
        convenience_text = data.get("output_text")
        if isinstance(convenience_text, str) and convenience_text.strip():
            return convenience_text.strip()

        texts = []
        refusals = []
        for item in data.get("output", []):
            if not isinstance(item, dict):
                continue
            for part in item.get("content", []):
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "output_text" and part.get("text"):
                    texts.append(str(part["text"]))
                elif part.get("type") == "refusal" and part.get("refusal"):
                    refusals.append(str(part["refusal"]))

        if texts:
            return "\n".join(texts).strip()
        if refusals:
            raise RuntimeError(f"OpenAI model refused the request: {' '.join(refusals)}")

        status = data.get("status", "unknown")
        incomplete = data.get("incomplete_details") or {}
        reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
        reason_suffix = f" ({reason})" if reason else ""
        raise RuntimeError(
            f"OpenAI API returned no text output; response status={status}{reason_suffix}"
        )


# Backend registry
_BACKENDS = {
    "claude_cli": ClaudeCLIBackend,
    "codex_cli": CodexCLIBackend,
    "openai_api": OpenAIAPIBackend,
    # Backwards-compatible alias for older example profiles.
    "openai": OpenAIAPIBackend,
}

# Cache instantiated backends
_backend_cache: dict[str, LLMBackend] = {}


def get_backend(component: str, profile: dict) -> LLMBackend:
    """
    Get the configured LLM backend for a specific component.

    Falls back to default_backend if component isn't specifically configured.
    Falls back to claude_cli if nothing is configured at all.
    """
    ai_config = profile.get("ai", {})
    backend_name = ai_config.get("components", {}).get(
        component, ai_config.get("default_backend", "claude_cli")
    )
    backend_config = ai_config.get("backends", {}).get(backend_name, {})

    # Cache key includes name + config hash for reuse
    cache_key = f"{backend_name}:{hash(json.dumps(backend_config, sort_keys=True, default=str))}"
    if cache_key in _backend_cache:
        return _backend_cache[cache_key]

    backend_class = _BACKENDS.get(backend_name)
    if not backend_class:
        print(f"  Warning: Unknown LLM backend '{backend_name}', falling back to claude_cli")
        backend_class = ClaudeCLIBackend
        backend_config = ai_config.get("backends", {}).get("claude_cli", {})

    instance = backend_class(backend_config)
    _backend_cache[cache_key] = instance
    return instance


def clear_backend_cache():
    """Clear the backend instance cache (useful after profile changes)."""
    _backend_cache.clear()
