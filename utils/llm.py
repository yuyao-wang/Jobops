"""
Pluggable LLM Backend — Route AI requests to different providers per component.

Supports Codex CLI by default, plus a tool-free OpenAI Responses API backend and
a legacy Claude CLI compatibility backend for trusted prompts. Each
AI component (scoring, cover letters, resume tailoring, etc.) can be
independently configured to use a different backend.

Configuration in profile.yaml:
    ai:
        default_backend: codex_cli
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
        scoring: codex_cli
        cover_letter: openai_api
"""

import os
import subprocess
import json
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

import httpx


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MINIMAL_ENV_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
)


class UnsafeLLMBackendError(RuntimeError):
    """Raised before untrusted browser text can reach an agentic backend."""


def _minimal_cli_env(*allowed_auth_keys: str) -> dict[str, str]:
    """Return the small environment needed to start an authenticated CLI.

    Arbitrary parent-process variables are deliberately excluded.  In
    particular, application/profile secrets cannot become visible to a model
    merely because a trusted CLI call happens to invoke a shell tool.
    """

    allowed = set(_MINIMAL_ENV_KEYS) | set(allowed_auth_keys)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allowed and isinstance(value, str)
    }


def _redact_env_secrets(message: str, *env_names: str) -> str:
    """Remove authentication values from subprocess diagnostics."""

    redacted = str(message)
    for env_name in env_names:
        value = os.environ.get(env_name, "")
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


class LLMBackend(ABC):
    """Abstract base for all LLM backends."""

    # Agentic CLIs are fail-closed for browser-derived text.  A backend may
    # opt in only when it makes a plain model request with no tools or local
    # context attached.
    safe_for_untrusted_input = False

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
            raise ValueError(
                f"LLM did not return valid JSON ({type(e).__name__})"
            ) from None


class ClaudeCLIBackend(LLMBackend):
    """Claude Code CLI backend for trusted prompts only."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.default_timeout = self.config.get("timeout", 120)
        self.executable = shutil.which("claude")
        if not self.executable:
            raise RuntimeError("Claude CLI not found on PATH")

    def ask(self, prompt: str, timeout: int = None) -> str:
        timeout = timeout or self.default_timeout
        # Current Claude CLI (2.1.x) can explicitly disable built-in tools,
        # project/user settings, slash commands, MCP discovery, Chrome, and
        # session persistence.  Keep those controls even though this backend
        # is still not approved for untrusted browser text.
        command = [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-chrome",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
        ]
        with tempfile.TemporaryDirectory(prefix="jobops-claude-") as workspace:
            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_minimal_cli_env("ANTHROPIC_API_KEY"),
                cwd=workspace,
            )
        if result.returncode != 0:
            error_msg = _redact_env_secrets(
                result.stderr.strip() or "Unknown error",
                "ANTHROPIC_API_KEY",
            )
            raise RuntimeError(f"Claude CLI error: {error_msg}")
        try:
            data = json.loads(result.stdout)
            return data.get("result", result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()


class CodexCLIBackend(LLMBackend):
    """Codex CLI backend for trusted prompts only."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.default_timeout = self.config.get("timeout", 180)
        self.model = self.config.get("model", "")
        self.executable = shutil.which("codex")
        if not self.executable:
            raise RuntimeError("Codex CLI not found on PATH")

    def ask(self, prompt: str, timeout: int = None) -> str:
        timeout = timeout or self.default_timeout
        # Codex 0.144.x documents that --ignore-user-config preserves auth in
        # CODEX_HOME.  Run in a fresh non-repository directory with read-only
        # sandboxing, no persistence, and no inherited shell environment.
        with tempfile.TemporaryDirectory(prefix="jobops-codex-") as workspace:
            command = [
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
            ]
            if self.model:
                command.extend(["--model", self.model])
            command.append("-")

            result = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=_minimal_cli_env(
                    "CODEX_API_KEY",
                    "CODEX_HOME",
                    "OPENAI_API_KEY",
                ),
                cwd=workspace,
            )
        if result.returncode != 0:
            error_msg = _redact_env_secrets(
                result.stderr.strip() or "Unknown error",
                "CODEX_API_KEY",
                "OPENAI_API_KEY",
            )
            raise RuntimeError(f"Codex CLI error: {error_msg}")
        return result.stdout.strip()


class OpenAIAPIBackend(LLMBackend):
    """OpenAI Responses API backend using the existing httpx dependency."""

    # This client deliberately sends a plain Responses request: no tools,
    # files, remote MCP servers, computer use, or local execution context.
    safe_for_untrusted_input = True

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.default_timeout = self.config.get("timeout", 120)
        self.model = self.config.get("model") or os.environ.get("OPENAI_MODEL", "")
        self.base_url = (
            self.config.get("base_url")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self._validate_base_url()
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
        if configured:
            if not isinstance(configured, str):
                raise ValueError(
                    "OpenAI api_key must be an environment reference, not a literal secret"
                )
            env_reference = re.fullmatch(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", configured.strip()
            )
            if not env_reference:
                raise ValueError(
                    "Literal OpenAI API keys are forbidden in profiles; use "
                    "api_key_env or api_key: ${ENV_NAME}"
                )
            return os.environ.get(env_reference.group(1), "")

        env_name = str(self.config.get("api_key_env", "OPENAI_API_KEY") or "").strip()
        env_reference = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", env_name)
        if env_reference:
            env_name = env_reference.group(1)
        if not _ENV_NAME.fullmatch(env_name):
            raise ValueError("OpenAI api_key_env must name one environment variable")
        return os.environ.get(env_name, "")

    def _validate_base_url(self) -> None:
        parsed = urlsplit(str(self.base_url))
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("OpenAI base_url must be a credential-free origin or path")
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("OpenAI base_url must use HTTPS (HTTP is local-only)")

    def ask(self, prompt: str, timeout: int = None) -> str:
        timeout = timeout or self.default_timeout
        payload = self._base_payload(prompt)
        data = self._post_response(payload, timeout=timeout)
        return self._extract_response_text(data)

    def ask_structured(
        self,
        *,
        system_prompt: str,
        input_data: dict,
        schema_name: str,
        schema: dict,
        timeout: int = None,
    ) -> dict:
        """Make one tool-free Responses API call with strict JSON Schema output."""

        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt must be non-empty")
        if not isinstance(input_data, dict):
            raise TypeError("input_data must be a dictionary")
        if not isinstance(schema, dict):
            raise TypeError("schema must be a dictionary")
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", schema_name or "") is None:
            raise ValueError("schema_name is invalid")
        try:
            serialized_input = json.dumps(
                input_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("input_data is not JSON serializable") from exc

        timeout = timeout or self.default_timeout
        payload = self._base_payload(
            [
                {"role": "system", "content": system_prompt.strip()},
                {"role": "user", "content": serialized_input},
            ]
        )
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        }
        data = self._post_response(payload, timeout=timeout)
        raw = self._extract_response_text(data)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise ValueError(
                "OpenAI API did not return a valid structured JSON object"
            ) from None
        if not isinstance(parsed, dict):
            raise ValueError(
                "OpenAI API structured output must be a JSON object"
            )
        return parsed

    def _base_payload(self, input_value) -> dict:
        payload = {
            "model": self.model,
            "input": input_value,
            "store": bool(self.store),
        }
        if self.max_output_tokens is not None:
            payload["max_output_tokens"] = self.max_output_tokens
        if self.reasoning_effort:
            payload["reasoning"] = {"effort": self.reasoning_effort}
        return payload

    def _post_response(self, payload: dict, *, timeout: int) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.organization:
            headers["OpenAI-Organization"] = self.organization
        if self.project:
            headers["OpenAI-Project"] = self.project

        try:
            response = httpx.post(
                f"{self.base_url}/responses",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
        except httpx.TimeoutException:
            raise TimeoutError("OpenAI API request timed out") from None
        except httpx.HTTPError as exc:
            # httpx exceptions may render request URLs.  Do not echo arbitrary
            # configured URLs or credentials into logs.
            raise RuntimeError(
                f"OpenAI API request failed ({type(exc).__name__})"
            ) from None

        if response.status_code >= 400:
            self._raise_api_error(response)

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("OpenAI API returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise RuntimeError("OpenAI API returned an invalid response object")
        return data

    def _raise_api_error(self, response) -> None:
        try:
            data = response.json()
        except ValueError:
            data = {}

        error = data.get("error", {}) if isinstance(data, dict) else {}
        raw_code = error.get("code") or error.get("type") if isinstance(error, dict) else ""
        code = str(raw_code or "api_error")
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) is None:
            code = "api_error"
        request_id = str(response.headers.get("x-request-id", ""))
        if re.fullmatch(r"req_[A-Za-z0-9]{1,100}", request_id) is None:
            request_id = ""
        request_suffix = f", request_id={request_id}" if request_id else ""
        raise RuntimeError(
            f"OpenAI API error ({response.status_code}{request_suffix}, code={code})"
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


def get_backend(
    component: str,
    profile: dict,
    *,
    require_untrusted_input_safe: bool = False,
) -> LLMBackend:
    """
    Get the configured LLM backend for a specific component.

    Falls back to default_backend if component isn't specifically configured.
    Falls back to codex_cli if nothing is configured at all.
    """
    ai_config = profile.get("ai", {})
    backend_name = ai_config.get("components", {}).get(
        component, ai_config.get("default_backend", "codex_cli")
    )
    backend_config = ai_config.get("backends", {}).get(backend_name, {})

    backend_class = _BACKENDS.get(backend_name)
    if not backend_class:
        if require_untrusted_input_safe:
            raise UnsafeLLMBackendError(
                f"Unknown form-analysis backend '{backend_name}'; browser input was not sent"
            )
        print(f"  Warning: Unknown LLM backend '{backend_name}', falling back to codex_cli")
        backend_class = CodexCLIBackend
        backend_config = ai_config.get("backends", {}).get("codex_cli", {})

    if require_untrusted_input_safe and not backend_class.safe_for_untrusted_input:
        raise UnsafeLLMBackendError(
            f"LLM backend '{backend_name}' is not approved for untrusted browser input; "
            "configure form_analysis to use the tool-free openai_api backend or run "
            "without --semantic-mapper"
        )

    # Do the trust check before consulting the cache: a backend previously
    # created for trusted work must never bypass the untrusted-input gate.
    cache_key = f"{backend_name}:{hash(json.dumps(backend_config, sort_keys=True, default=str))}"
    if cache_key in _backend_cache:
        return _backend_cache[cache_key]

    instance = backend_class(backend_config)
    _backend_cache[cache_key] = instance
    return instance


def require_safe_backend_for_untrusted_input(
    component: str, profile: dict
) -> LLMBackend:
    """Resolve and validate a backend before any browser session is opened."""

    return get_backend(
        component,
        profile,
        require_untrusted_input_safe=True,
    )


def clear_backend_cache():
    """Clear the backend instance cache (useful after profile changes)."""
    _backend_cache.clear()
