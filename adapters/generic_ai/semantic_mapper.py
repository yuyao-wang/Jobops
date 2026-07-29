"""Provider-neutral contract for bounded semantic control classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Iterable, Literal, Protocol, runtime_checkable

from core.application_answer_taxonomy import (
    CANONICAL_APPLICATION_ANSWER_TAXONOMY,
    CanonicalApplicationAnswerKey,
    normalize_canonical_application_answer_key,
)
from .models import FormControl


CanonicalKey = CanonicalApplicationAnswerKey
MappingStatus = Literal["mapped", "needs_review", "unsupported"]

_STATUS_BY_KEY: dict[CanonicalApplicationAnswerKey, MappingStatus] = {
    key: definition.semantic_mapping_status.value
    for key, definition in CANONICAL_APPLICATION_ANSWER_TAXONOMY.items()
}


def _validate_text(name: str, value: str, *, max_length: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds the contract limit")


def _redact_text(value: str, private_values: tuple[str, ...]) -> str:
    redacted = value
    for private_value in private_values:
        if not private_value:
            continue
        escaped = re.escape(private_value)
        if private_value.isalnum() and len(private_value) <= 3:
            escaped = rf"(?<!\w){escaped}(?!\w)"
        redacted = re.sub(escaped, "[PRIVATE]", redacted, flags=re.IGNORECASE)
        # The DOM observer truncates at each field's end. If that cut lands
        # inside a known-private value, redact the remaining suffix when it is
        # a meaningful prefix of that private value.
        private_folded = private_value.casefold()
        redacted_folded = redacted.casefold()
        for start in range(len(redacted)):
            fragment = redacted_folded[start:]
            if (
                len(fragment) >= 8
                and len(fragment) < len(private_folded)
                and private_folded.startswith(fragment)
            ):
                redacted = f"{redacted[:start]}[PRIVATE]"
                break
    return redacted


@dataclass(frozen=True, slots=True)
class MappingRequest:
    """One value-free control in a mapper batch.

    A batch is passed to ``SemanticMapper.map_controls`` so an implementation
    can classify several controls with one provider dispatch.
    """

    index: int
    role: str
    tag: str
    input_type: str = ""
    label: str = ""
    name: str = ""
    aria_label: str = ""
    placeholder: str = ""
    autocomplete: str = ""
    required: bool = True
    options: tuple[str, ...] = ()

    MAX_BATCH_SIZE: ClassVar[int] = 40

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("index must be a non-negative integer")
        _validate_text("role", self.role, max_length=32)
        _validate_text("tag", self.tag, max_length=32)
        _validate_text("input_type", self.input_type, max_length=32)
        _validate_text("label", self.label, max_length=240)
        _validate_text("name", self.name, max_length=160)
        _validate_text("aria_label", self.aria_label, max_length=240)
        _validate_text("placeholder", self.placeholder, max_length=160)
        _validate_text("autocomplete", self.autocomplete, max_length=80)
        if not self.role or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.role):
            raise ValueError("role is outside the mapping contract")
        if not self.tag or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.tag):
            raise ValueError("tag is outside the mapping contract")
        if type(self.required) is not bool or not self.required:
            raise ValueError("only required unresolved controls may be mapped")
        if not isinstance(self.options, tuple):
            raise TypeError("options must be a tuple")
        if len(self.options) > 30:
            raise ValueError("options exceeds the contract limit")
        for option in self.options:
            _validate_text("option", option, max_length=160)
        if not any(
            (
                self.label,
                self.name,
                self.aria_label,
                self.placeholder,
                self.autocomplete,
                self.options,
            )
        ):
            raise ValueError("a mapping request needs a semantic descriptor")

    @classmethod
    def from_control(
        cls,
        control: FormControl,
        *,
        private_values: Iterable[str] = (),
    ) -> "MappingRequest":
        """Project a form control without selectors, values, or page identity."""

        redaction_values: set[str] = set()
        for value in private_values:
            normalized = " ".join(str(value).split())
            if not normalized:
                continue
            redaction_values.add(normalized)
        redactions = tuple(
            sorted(redaction_values, key=len, reverse=True)
        )
        return cls(
            index=control.index,
            role=control.role,
            tag=control.tag,
            input_type=control.input_type,
            label=_redact_text(control.label, redactions)[:240],
            name=_redact_text(control.name, redactions)[:160],
            aria_label=_redact_text(control.aria_label, redactions)[:240],
            placeholder=_redact_text(control.placeholder, redactions)[:160],
            autocomplete=_redact_text(control.autocomplete, redactions)[:80],
            required=control.required,
            options=tuple(
                _redact_text(option.label, redactions)[:160]
                for option in control.options[:30]
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "role": self.role,
            "tag": self.tag,
            "type": self.input_type,
            "label": self.label,
            "name": self.name,
            "aria_label": self.aria_label,
            "placeholder": self.placeholder,
            "autocomplete": self.autocomplete,
            "required": self.required,
            "options": list(self.options),
        }


@dataclass(frozen=True, slots=True)
class MappingResponse:
    """One validated decision returned for a requested control."""

    index: int
    canonical_key: CanonicalKey
    status: MappingStatus

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("index must be a non-negative integer")
        try:
            key = normalize_canonical_application_answer_key(
                self.canonical_key,
                allow_legacy_alias=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "canonical_key is outside the mapping taxonomy"
            ) from exc
        object.__setattr__(self, "canonical_key", key)
        if not isinstance(self.status, str) or self.status not in {
            "mapped",
            "needs_review",
            "unsupported",
        }:
            raise ValueError("status is outside the mapping contract")
        if self.status != _STATUS_BY_KEY[self.canonical_key]:
            raise ValueError("status conflicts with canonical-key policy")

    @classmethod
    def for_key(
        cls,
        index: int,
        canonical_key: CanonicalKey | str,
    ) -> "MappingResponse":
        try:
            key = normalize_canonical_application_answer_key(
                canonical_key,
                allow_legacy_alias=True,
            )
            status = _STATUS_BY_KEY[key]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                "canonical_key is outside the mapping taxonomy"
            ) from exc
        except ValueError as exc:
            raise ValueError(
                "canonical_key is outside the mapping taxonomy"
            ) from exc
        return cls(index=index, canonical_key=key, status=status)


@runtime_checkable
class SemanticMapper(Protocol):
    """The only semantic capability visible to the generic adapter."""

    async def map_controls(
        self,
        requests: tuple[MappingRequest, ...],
    ) -> tuple[MappingResponse, ...]:
        """Classify one bounded batch without receiving candidate values."""


class FakeSemanticMapper:
    """Deterministic mapper for tests; performs no model or network access."""

    def __init__(
        self,
        responses: Iterable[MappingResponse] = (),
        *,
        error: Exception | None = None,
    ) -> None:
        self._responses = tuple(responses)
        self._error = error
        self.calls: list[tuple[MappingRequest, ...]] = []

    async def map_controls(
        self,
        requests: tuple[MappingRequest, ...],
    ) -> tuple[MappingResponse, ...]:
        batch = tuple(requests)
        if not 1 <= len(batch) <= MappingRequest.MAX_BATCH_SIZE:
            raise ValueError("mapping batch size is outside the contract")
        if not all(isinstance(request, MappingRequest) for request in batch):
            raise TypeError("mapping batch contains an invalid request")
        request_indices = [request.index for request in batch]
        if len(request_indices) != len(set(request_indices)):
            raise ValueError("mapping request indices must be unique")

        self.calls.append(batch)
        if self._error is not None:
            raise self._error

        response_indices: list[int] = []
        for response in self._responses:
            if not isinstance(response, MappingResponse):
                raise TypeError("mapper returned an invalid response")
            if response.index not in request_indices:
                raise ValueError("mapper returned an unrequested index")
            response_indices.append(response.index)
        if len(response_indices) != len(set(response_indices)):
            raise ValueError("mapping response indices must be unique")
        return self._responses
