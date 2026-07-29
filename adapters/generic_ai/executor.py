"""Deterministic Playwright execution for resolved generic-form controls."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from core.application_answer_taxonomy import CanonicalApplicationAnswerKey
from .cache import RecipeAction
from .fingerprinter import control_signature
from .models import FormControl
from .resolver import ResolvedField


@dataclass(frozen=True)
class FillFailure:
    index: int
    label: str
    canonical_key: CanonicalApplicationAnswerKey
    reason: str


@dataclass(frozen=True)
class FillReport:
    attempted: int
    completed: int
    failures: tuple[FillFailure, ...]
    recipe_actions: tuple[RecipeAction, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


async def _element(page, control: FormControl, selector_override: str = ""):
    selector = selector_override or control.selector
    if selector:
        try:
            return await page.wait_for_selector(selector, timeout=2500)
        except Exception:
            pass
    label = control.label or control.aria_label
    if label and hasattr(page, "get_by_label"):
        try:
            locator = page.get_by_label(label, exact=True)
            if await locator.count():
                return locator.first
        except Exception:
            pass
    return None


def _normalized(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _option_matches(control: FormControl, value: str) -> bool:
    expected = _normalized(value)
    candidates = [_normalized(control.label), _normalized(control.aria_label)]
    candidates.extend(_normalized(option.label) for option in control.options)
    return any(candidate and candidate == expected for candidate in candidates)


async def _fill_select(element, control: FormControl, value: str) -> None:
    try:
        await element.select_option(label=value)
        return
    except Exception:
        pass
    try:
        await element.select_option(value=value)
        return
    except Exception:
        pass
    expected = _normalized(value)
    fuzzy_matches = [
        option
        for option in control.options
        if expected in _normalized(option.label)
        or _normalized(option.label) in expected
    ]
    if len(fuzzy_matches) == 1:
        option = fuzzy_matches[0]
        if option.value:
            await element.select_option(value=option.value)
        else:
            await element.select_option(label=option.label)
        return
    raise ValueError("no unambiguous matching option")


async def _fill_choice(element, control: FormControl, value: str) -> bool:
    if control.role == "radio" and not _option_matches(control, value):
        return False
    expected_true = _normalized(value) in {"yes", "true", "1", "checked", "agree", "i agree"}
    if control.role == "checkbox":
        if expected_true:
            await element.check()
        else:
            try:
                await element.uncheck()
            except Exception:
                if await element.is_checked():
                    await element.click()
        return True
    await element.check()
    return True


async def _upload(element, value: str) -> None:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("verified resume artifact does not exist")
    await element.set_input_files(str(path))
    attached = await element.evaluate("el => Boolean(el.files && el.files.length)")
    if not attached:
        raise ValueError("browser did not report an attached file")


def operation_for(control: FormControl) -> str:
    if control.role == "file_upload" or control.input_type == "file":
        return "upload"
    if control.role in {"combobox", "listbox"} or control.tag == "select":
        return "select"
    if control.role in {"checkbox", "radio"}:
        return "check"
    return "fill"


async def execute_field(page, field: ResolvedField, selector_override: str = "") -> tuple[bool, str]:
    control = field.control
    element = await _element(page, control, selector_override)
    if element is None:
        return False, "stable selector and label lookup failed"
    operation = operation_for(control)
    try:
        if operation == "upload":
            await _upload(element, field.value)
        elif operation == "select":
            await _fill_select(element, control, field.value)
        elif operation == "check":
            selected = await _fill_choice(element, control, field.value)
            if not selected:
                return False, "no unambiguous matching radio option"
        else:
            await element.fill(field.value)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: deterministic operation failed"


async def execute_resolved_fields(page, fields: list[ResolvedField]) -> FillReport:
    failures: list[FillFailure] = []
    actions: list[RecipeAction] = []
    completed = 0
    for field in fields:
        ok, reason = await execute_field(page, field)
        if ok:
            completed += 1
            actions.append(
                RecipeAction(
                    control_signature=hashlib.sha256(
                        json.dumps(
                            control_signature(field.control),
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest(),
                    canonical_key=field.canonical_key,
                    selector=field.control.selector,
                    operation=operation_for(field.control),
                )
            )
        else:
            failures.append(
                FillFailure(
                    index=field.control.index,
                    label=field.control.label or field.control.aria_label or field.control.name,
                    canonical_key=field.canonical_key,
                    reason=reason,
                )
            )
    return FillReport(len(fields), completed, tuple(failures), tuple(actions))


async def click_next(page, selector: str, text: str = "") -> bool:
    candidates = [selector] if selector else []
    candidates.extend(
        [
            'button:has-text("Next")',
            'button:has-text("Continue")',
            'button:has-text("Save and Continue")',
            'button:has-text("Review")',
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            element = await page.wait_for_selector(candidate, timeout=1800)
            if element:
                await element.click()
                return True
        except Exception:
            continue
    return False


async def click_submit(page, selector: str, expected_text: str) -> bool:
    """Click only an explicitly identified application submission control."""
    if not any(token in _normalized(expected_text) for token in ("submit", "send application", "apply")):
        return False
    candidates = [selector] if selector else []
    candidates.extend(['button[type="submit"]', 'input[type="submit"]'])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            element = await page.wait_for_selector(candidate, timeout=2000)
            if element:
                visible_text = ""
                if hasattr(element, "inner_text"):
                    try:
                        visible_text = _normalized(await element.inner_text())
                    except Exception:
                        pass
                if not visible_text and hasattr(element, "get_attribute"):
                    for attribute in ("value", "aria-label", "title"):
                        try:
                            visible_text = _normalized(
                                await element.get_attribute(attribute)
                            )
                        except Exception:
                            visible_text = ""
                        if visible_text:
                            break
                if not visible_text or not any(
                    token in visible_text for token in ("submit", "send", "apply")
                ):
                    continue
                await element.click()
                return True
        except Exception:
            continue
    return False
