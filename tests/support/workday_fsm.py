"""Sanitized deterministic browser double for Workday FSM contracts."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Mapping


class FsmMissingLocator:
    def __init__(self) -> None:
        self.first = self

    async def count(self) -> int:
        return 0

    async def is_visible(self, *_args: Any, **_kwargs: Any) -> bool:
        return False


class FsmFieldLocator:
    def __init__(self, field: Mapping[str, Any]) -> None:
        self.first = self
        self.field = dict(field)
        self.value = ""
        self.file_contents: list[str] = []

    async def count(self) -> int:
        return 1

    async def fill(self, value: Any) -> None:
        self.value = str(value)

    async def set_input_files(self, path: str) -> None:
        content = Path(path).read_bytes()
        self.file_contents = [base64.b64encode(content).decode("ascii")]
        # Match the Workday fingerprint projection without retaining a path.
        self.value = ",".join(self.file_contents)

    async def evaluate(self, _script: str) -> dict[str, Any]:
        return {
            "value": self.value,
            "selectedText": "",
            "selectedValue": "",
            "checked": False,
            "groupChecked": False,
            "groupValue": "",
            "groupLabel": "",
            "ariaValue": "",
            "selectedDataValue": "",
            "text": "",
            "fileContents": list(self.file_contents),
        }


class FsmNextLocator:
    def __init__(self, page: "FixtureWorkdayFsmPage") -> None:
        self.first = self
        self.page = page

    async def is_visible(self, *_args: Any, **_kwargs: Any) -> bool:
        return self.page.stage_index < len(self.page.stages) - 1

    async def inner_text(self) -> str:
        return "Next"

    async def click(self) -> None:
        self.page.next_clicks += 1
        self.page.stage_index += 1
        self.page._sync_url()


class FixtureWorkdayFsmPage:
    """Small deterministic browser double driven by a sanitized fixture."""

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        self.posting_url = str(fixture["posting_url"])
        self.stages = list(fixture["stages"])
        self.stage_index = 0
        self.next_clicks = 0
        self.controls = {
            item["selector"]: FsmFieldLocator(item)
            for stage in self.stages
            for item in stage["fields"]
        }
        self.next_locator = FsmNextLocator(self)
        self._sync_url()

    @property
    def uploaded_file_count(self) -> int:
        return sum(bool(locator.file_contents) for locator in self.controls.values())

    def _sync_url(self) -> None:
        route = self.stages[self.stage_index]["route"]
        self.url = f"{self.posting_url}/{route}"

    async def evaluate(self, script: str, *_args: Any) -> Any:
        stage = self.stages[self.stage_index]
        if "jobopsWorkdayField" in script:
            return list(stage["fields"])
        if "contents.sort().join" in script:
            return [
                {
                    "key": locator.field["automationId"],
                    "value": locator.value,
                }
                for locator in self.controls.values()
            ]
        if "submitLabels" in script:
            return {
                "text": stage["heading"],
                "automationIds": list(stage["automation_ids"]),
                "headings": [stage["heading"]],
                "submitLabels": [],
            }
        if "bodyText" in script:
            return {
                "text": stage["heading"],
                "heading": stage["heading"],
                "passwordFields": 0,
                "visibleInputs": len(stage["fields"]),
                "hasApplyButton": False,
                "hasCreateAccount": False,
                "hasCaptchaChallenge": False,
                "automationIds": list(stage["automation_ids"]),
                "postingUrls": [self.posting_url],
            }
        raise AssertionError("unexpected browser script in deterministic fixture")

    def locator(self, selector: str) -> Any:
        if "bottom-navigation-next-button" in selector:
            return self.next_locator
        return self.controls.get(selector, FsmMissingLocator())

    async def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def wait_for_timeout(self, *_args: Any, **_kwargs: Any) -> None:
        return None


__all__ = ["FixtureWorkdayFsmPage"]
