"""Small, redacted intermediate representation for application forms."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class FormOption:
    """One selectable option without any candidate data."""

    label: str
    value: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FormOption":
        return cls(
            label=str(value.get("label") or value.get("text") or "").strip(),
            value=str(value.get("value") or "").strip(),
        )


@dataclass(frozen=True)
class FormControl:
    """A stable description of one visible form control."""

    index: int
    role: str
    tag: str
    input_type: str = ""
    label: str = ""
    name: str = ""
    element_id: str = ""
    aria_label: str = ""
    placeholder: str = ""
    autocomplete: str = ""
    required: bool = False
    disabled: bool = False
    selector: str = ""
    options: tuple[FormOption, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FormControl":
        return cls(
            index=int(value.get("index", 0)),
            role=str(value.get("role") or "textbox").strip().lower(),
            tag=str(value.get("tag") or "input").strip().lower(),
            input_type=str(value.get("input_type") or value.get("type") or "").strip().lower(),
            label=str(value.get("label") or "").strip(),
            name=str(value.get("name") or "").strip(),
            element_id=str(value.get("element_id") or value.get("id") or "").strip(),
            aria_label=str(value.get("aria_label") or value.get("aria-label") or "").strip(),
            placeholder=str(value.get("placeholder") or "").strip(),
            autocomplete=str(value.get("autocomplete") or "").strip().lower(),
            required=bool(value.get("required", False)),
            disabled=bool(value.get("disabled", False)),
            selector=str(value.get("selector") or "").strip(),
            options=tuple(FormOption.from_dict(option) for option in value.get("options", [])),
        )

    @property
    def semantic_text(self) -> str:
        return " ".join(
            part
            for part in (
                self.label,
                self.aria_label,
                self.name,
                self.placeholder,
                self.autocomplete,
            )
            if part
        )

    def compact_dict(self) -> dict[str, Any]:
        """Return only structure that is safe to send to a semantic mapper."""
        return {
            "index": self.index,
            "role": self.role,
            "tag": self.tag,
            "type": self.input_type,
            "label": self.label[:240],
            "name": self.name[:160],
            "aria_label": self.aria_label[:240],
            "placeholder": self.placeholder[:160],
            "autocomplete": self.autocomplete,
            "required": self.required,
            "options": [option.label[:160] for option in self.options[:30]],
        }


@dataclass(frozen=True)
class FormIR:
    """A bounded form snapshot used instead of full HTML or page history."""

    platform: str
    tenant: str
    stage: str
    url_path: str
    title: str = ""
    controls: tuple[FormControl, ...] = ()
    errors: tuple[str, ...] = ()
    next_selector: str = ""
    next_text: str = ""
    submit_selector: str = ""
    submit_text: str = ""
    page_text_hints: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FormIR":
        return cls(
            platform=str(value.get("platform") or "generic"),
            tenant=str(value.get("tenant") or ""),
            stage=str(value.get("stage") or "form"),
            url_path=str(value.get("url_path") or "/"),
            title=str(value.get("title") or "")[:240],
            controls=tuple(FormControl.from_dict(control) for control in value.get("controls", [])),
            errors=tuple(str(error)[:240] for error in value.get("errors", [])),
            next_selector=str(value.get("next_selector") or ""),
            next_text=str(value.get("next_text") or "")[:120],
            submit_selector=str(value.get("submit_selector") or ""),
            submit_text=str(value.get("submit_text") or "")[:120],
            page_text_hints=tuple(str(hint)[:200] for hint in value.get("page_text_hints", [])),
            metadata=dict(value.get("metadata") or {}),
        )

    def compact_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "tenant": self.tenant,
            "stage": self.stage,
            "url_path": self.url_path,
            "title": self.title,
            "controls": [control.compact_dict() for control in self.controls],
            "errors": list(self.errors),
            "next_text": self.next_text,
            "submit_text": self.submit_text,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
