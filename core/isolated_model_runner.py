"""Provider-neutral contracts for one isolated subscription CLI generation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .model_provider_capabilities import ModelExecutionIsolationProfile


ISOLATED_STRUCTURED_MODEL_CONTRACT_VERSION = (
    "isolated-structured-model-request-v1"
)
ISOLATED_STRUCTURED_MODEL_RESULT_VERSION = (
    "isolated-structured-model-result-v1"
)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class IsolatedStructuredModelStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    ISOLATION_UNAVAILABLE = "ISOLATION_UNAVAILABLE"
    AUTHENTICATION_UNAVAILABLE = "AUTHENTICATION_UNAVAILABLE"
    CLI_CONTRACT_UNSUPPORTED = "CLI_CONTRACT_UNSUPPORTED"
    TEXT_INPUT_TOO_LARGE = "TEXT_INPUT_TOO_LARGE"
    IMAGE_INPUT_UNSUPPORTED = "IMAGE_INPUT_UNSUPPORTED"
    IMAGE_INPUT_INVALID = "IMAGE_INPUT_INVALID"
    IMAGE_INPUT_TOO_LARGE = "IMAGE_INPUT_TOO_LARGE"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    TIMEOUT = "TIMEOUT"
    PROCESS_FAILED = "PROCESS_FAILED"
    TOOL_ATTEMPTED = "TOOL_ATTEMPTED"
    SCHEMA_OUTPUT_INVALID = "SCHEMA_OUTPUT_INVALID"
    CLEANUP_FAILED = "CLEANUP_FAILED"


@dataclass(frozen=True, slots=True)
class ManagedModelImage:
    media_type: str
    content: bytes = field(repr=False)
    byte_size: int
    sha256: str
    order: int
    role_id: str

    def __post_init__(self) -> None:
        if self.media_type not in {"image/png", "image/jpeg"}:
            raise ValueError("image media type is unsupported")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("image content must be non-empty bytes")
        if self.byte_size != len(self.content):
            raise ValueError("image byte size is invalid")
        if (
            not _HASH_RE.fullmatch(self.sha256)
            or hashlib.sha256(self.content).hexdigest() != self.sha256
        ):
            raise ValueError("image hash is invalid")
        if type(self.order) is not int or self.order < 0:
            raise ValueError("image order is invalid")
        if not isinstance(self.role_id, str) or not self.role_id.strip():
            raise ValueError("image role identity is invalid")


@dataclass(frozen=True, slots=True)
class IsolatedStructuredModelRequest:
    component_id: str
    invocation_id: str
    model_id: str | None
    system_prompt: str
    input_data: Mapping[str, Any]
    images: tuple[ManagedModelImage, ...]
    output_schema_name: str
    output_schema: Mapping[str, Any]
    timeout_seconds: int
    max_input_bytes: int
    max_output_bytes: int
    max_images: int
    prompt_contract_version: str
    schema_contract_version: str
    contract_version: str = ISOLATED_STRUCTURED_MODEL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "component_id",
            "invocation_id",
            "system_prompt",
            "output_schema_name",
            "prompt_contract_version",
            "schema_contract_version",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.input_data, Mapping):
            raise TypeError("input_data must be a mapping")
        if not isinstance(self.images, tuple):
            raise TypeError("images must be a tuple")
        if tuple(image.order for image in self.images) != tuple(
            range(len(self.images))
        ):
            raise ValueError("image order must be stable and contiguous")
        if not isinstance(self.output_schema, Mapping):
            raise TypeError("output_schema must be a mapping")
        limits = (
            self.timeout_seconds,
            self.max_input_bytes,
            self.max_output_bytes,
            self.max_images,
        )
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("request limits must be positive")
        if self.contract_version != ISOLATED_STRUCTURED_MODEL_CONTRACT_VERSION:
            raise ValueError("request contract version is unsupported")

    def input_bytes(self) -> bytes:
        return json.dumps(
            {
                "data_type": "IsolatedStructuredModelInput",
                "input": self.input_data,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def output_schema_bytes(self) -> bytes:
        """Return the exact canonical schema bytes sent to the provider."""

        return json.dumps(
            self.output_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def total_input_byte_count(self) -> int:
        return (
            len(self.input_bytes())
            + len(self.system_prompt.encode("utf-8"))
            + len(self.output_schema_bytes())
        )

    def execution_identity(
        self,
        *,
        backend_id: str,
        isolation_profile_id: str,
        isolation_contract_version: str,
    ) -> str:
        content = {
            "backend_id": backend_id,
            "component_id": self.component_id,
            "contract_version": self.contract_version,
            "images": [
                {
                    "media_type": image.media_type,
                    "order": image.order,
                    "role_id": image.role_id,
                    "sha256": image.sha256,
                }
                for image in self.images
            ],
            "input_sha256": hashlib.sha256(self.input_bytes()).hexdigest(),
            "invocation_id": self.invocation_id,
            "isolation_contract_version": isolation_contract_version,
            "isolation_profile_id": isolation_profile_id,
            "model_id": self.model_id,
            "output_schema_name": self.output_schema_name,
            "output_schema": self.output_schema,
            "prompt_contract_version": self.prompt_contract_version,
            "schema_contract_version": self.schema_contract_version,
            "system_prompt_sha256": hashlib.sha256(
                self.system_prompt.encode("utf-8")
            ).hexdigest(),
        }
        encoded = json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return "isolated-model-execution-" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SubscriptionCLIProcessSpec:
    backend_id: str
    model_id: str
    argv: tuple[str, ...]
    environment: Mapping[str, str]
    result_file_name: str
    executable_read_roots: tuple[str, ...]
    allowed_process_executables: tuple[str, ...]
    allowed_preference_domains: tuple[str, ...] = ()


class SubscriptionCLIInvocationAdapter(Protocol):
    backend_id: str
    supports_image_input: bool

    def probe_contract(self) -> bool: ...

    def project_subscription_session(self, destination: str) -> bool: ...

    def build_process_spec(
        self,
        request: IsolatedStructuredModelRequest,
        *,
        workspace: str,
        session_home: str,
        schema_path: str,
        image_paths: tuple[str, ...],
    ) -> SubscriptionCLIProcessSpec: ...

    def parse_process_output(
        self, stdout: bytes, result_bytes: bytes
    ) -> tuple[bool, Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class IsolatedStructuredModelResult:
    status: IsolatedStructuredModelStatus
    output: Mapping[str, Any] | None
    backend_id: str
    model_id: str
    component_id: str
    prompt_contract_version: str
    schema_contract_version: str
    isolation_profile_id: str
    isolation_contract_version: str
    execution_identity: str
    input_byte_count: int
    output_byte_count: int
    image_count: int
    duration_ms: int
    diagnostic_category: str
    contract_version: str = ISOLATED_STRUCTURED_MODEL_RESULT_VERSION


class IsolatedSubscriptionRunner(Protocol):
    async def execute(
        self,
        request: IsolatedStructuredModelRequest,
        *,
        backend_adapter: SubscriptionCLIInvocationAdapter,
        isolation_profile: ModelExecutionIsolationProfile,
    ) -> IsolatedStructuredModelResult: ...


__all__ = [
    "ISOLATED_STRUCTURED_MODEL_CONTRACT_VERSION",
    "ISOLATED_STRUCTURED_MODEL_RESULT_VERSION",
    "IsolatedStructuredModelRequest",
    "IsolatedStructuredModelResult",
    "IsolatedStructuredModelStatus",
    "IsolatedSubscriptionRunner",
    "ManagedModelImage",
    "SubscriptionCLIInvocationAdapter",
    "SubscriptionCLIProcessSpec",
]
