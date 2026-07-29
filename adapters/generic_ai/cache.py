"""Value-free recipe cache for deterministic generic-form replay."""

from __future__ import annotations

import json
import os
import hashlib
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from core.application_answer_taxonomy import (
    CanonicalApplicationAnswerKey,
    normalize_canonical_application_answer_key,
)

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_SELECTOR_RE = re.compile(
    r"^(?:"
    r"#[A-Za-z_][A-Za-z0-9_-]{0,127}|"
    r"\[data-automation-id=\"[A-Za-z0-9_.:-]{1,128}\"\]|"
    r"[a-z][a-z0-9-]*\[name=\"[A-Za-z0-9_.:-]{1,128}\"\]|"
    r"[a-z][a-z0-9-]*:nth-of-type\([1-9][0-9]{0,3}\)"
    r")$"
)


def _safe_action(action: "RecipeAction") -> "RecipeAction | None":
    """Drop text-bearing selectors and digest any legacy raw signature."""

    if not _SAFE_SELECTOR_RE.fullmatch(action.selector):
        return None
    signature = action.control_signature
    if not _DIGEST_RE.fullmatch(signature):
        signature = hashlib.sha256(signature.encode("utf-8")).hexdigest()
    return RecipeAction(
        control_signature=signature,
        canonical_key=action.canonical_key,
        selector=action.selector,
        operation=action.operation,
    )


@dataclass(frozen=True)
class RecipeAction:
    control_signature: str
    canonical_key: CanonicalApplicationAnswerKey
    selector: str
    operation: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "canonical_key",
            normalize_canonical_application_answer_key(
                self.canonical_key,
                allow_legacy_alias=True,
                allow_custom_unknown=True,
            ),
        )


@dataclass(frozen=True)
class FormRecipe:
    protocol_version: str
    fingerprint: str
    platform: str
    tenant: str
    stage: str
    created_at: str
    actions: tuple[RecipeAction, ...]

    def to_dict(self) -> dict:
        value = asdict(self)
        value["actions"] = [asdict(action) for action in self.actions]
        return value

    @classmethod
    def from_dict(cls, value: dict) -> "FormRecipe":
        return cls(
            protocol_version=str(value.get("protocol_version") or "1.0"),
            fingerprint=str(value["fingerprint"]),
            platform=str(value.get("platform") or "generic"),
            tenant=str(value.get("tenant") or ""),
            stage=str(value.get("stage") or "form"),
            created_at=str(value.get("created_at") or ""),
            actions=tuple(RecipeAction(**action) for action in value.get("actions", [])),
        )


class RecipeCache:
    """Store only selectors and canonical keys; candidate values are forbidden."""

    def __init__(self, root: Path, ttl_days: int = 30):
        self.root = Path(root).expanduser().resolve()
        self.ttl = timedelta(days=ttl_days)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, fingerprint: str) -> Path:
        safe = "".join(char for char in fingerprint if char.isalnum())
        return self.root / f"{safe}.json"

    def load(self, fingerprint: str) -> FormRecipe | None:
        path = self._path(fingerprint)
        if not path.is_file():
            return None
        try:
            recipe = FormRecipe.from_dict(json.loads(path.read_text(encoding="utf-8")))
            safe_actions = tuple(
                safe
                for action in recipe.actions
                if (safe := _safe_action(action)) is not None
            )
            if len(safe_actions) != len(recipe.actions):
                return None
            recipe = FormRecipe(
                protocol_version=recipe.protocol_version,
                fingerprint=recipe.fingerprint,
                platform=recipe.platform,
                tenant=recipe.tenant,
                stage=recipe.stage,
                created_at=recipe.created_at,
                actions=safe_actions,
            )
            created = datetime.fromisoformat(recipe.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created > self.ttl:
                return None
            return recipe
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(
        self,
        *,
        fingerprint: str,
        platform: str,
        tenant: str,
        stage: str,
        actions: Iterable[RecipeAction],
    ) -> FormRecipe:
        safe_actions = tuple(
            safe
            for action in actions
            if (safe := _safe_action(action)) is not None
        )
        recipe = FormRecipe(
            protocol_version="1.0",
            fingerprint=fingerprint,
            platform=platform,
            tenant=tenant,
            stage=stage,
            created_at=datetime.now(timezone.utc).isoformat(),
            actions=safe_actions,
        )
        payload = json.dumps(recipe.to_dict(), indent=2, sort_keys=True)
        fd, temp_name = tempfile.mkstemp(prefix="recipe-", suffix=".tmp", dir=self.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(temp_name, self._path(fingerprint))
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        return recipe
