"""Register one strict Base LaTeX source, then delegate replacement to S3g5."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
    HumanAttentionResolutionCapability,
)
from .input_replacement_resolution import (
    InputReplacementAction,
    InputReplacementResolutionCommand,
    InputReplacementResolutionResult,
    InputReplacementResolutionStatus,
)
from .input_replacement_target import (
    BaseLatexVersionReplacementTarget,
    InputReplacementMethod,
    InputReplacementTargetProvider,
)
from .private_home import PrivateHome
from .resume_latex_versions import (
    MAX_LATEX_SOURCE_BYTES,
    LatexSourceProfile,
    RegisterResumeLatexVersionCommand,
    RegisterResumeLatexVersionResult,
    RegisterResumeLatexVersionStatus,
    ResumeLatexSourceKind,
    ResumeLatexVersion,
    ResumeLatexVersionFailureReason,
    ResumeLatexVersionListStatus,
    ResumeLatexVersionProvider,
)


NEW_BASE_LATEX_REPLACEMENT_CONTRACT_VERSION = (
    "new-base-latex-version-replacement-v1"
)
NEW_BASE_LATEX_UPLOAD_POLICY_VERSION = "new-base-latex-upload-policy-v1"
NEW_BASE_LATEX_UPLOAD_MAX_BYTES = MAX_LATEX_SOURCE_BYTES
_INVOCATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_SAFE_TEXT = re.compile(r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*$")


class NewBaseLatexVersionReplacementStatus(StrEnum):
    REGISTERED_AND_REPLACED_COMPLETED = (
        "REGISTERED_AND_REPLACED_COMPLETED"
    )
    REGISTERED_AND_REPLACED_DEFERRED = (
        "REGISTERED_AND_REPLACED_DEFERRED"
    )
    REGISTERED_REPLACEMENT_FAILED = "REGISTERED_REPLACEMENT_FAILED"
    EXISTING_CONTENT_REUSED_AND_REPLACED = (
        "EXISTING_CONTENT_REUSED_AND_REPLACED"
    )
    UNCHANGED = "UNCHANGED"
    ITEM_NOT_CURRENT = "ITEM_NOT_CURRENT"
    TARGET_STALE = "TARGET_STALE"
    UPLOAD_REJECTED = "UPLOAD_REJECTED"
    INVALID_LATEX_SOURCE = "INVALID_LATEX_SOURCE"
    UNSAFE_LATEX_SOURCE = "UNSAFE_LATEX_SOURCE"
    UNSUPPORTED_UPLOAD_TYPE = "UNSUPPORTED_UPLOAD_TYPE"
    REGISTRATION_FAILED = "REGISTRATION_FAILED"
    VERSION_NOT_SELECTABLE = "VERSION_NOT_SELECTABLE"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class NewBaseLatexVersionReplacementCommand:
    subject_id: str
    attention_item_id: str
    invocation_id: str
    uploaded_content: bytes
    display_label: str
    version_note: str | None
    now: datetime


@dataclass(frozen=True, slots=True)
class NewBaseLatexVersionReplacementReceipt:
    receipt_id: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    invocation_id: str
    replacement_target_id: str
    replacement_target_version: str
    replacement_target_hash: str
    replaced_version_id: str
    replaced_version_contract: str
    replaced_source_hash: str
    root_family_id: str
    uploaded_source_hash: str
    upload_policy_version: str
    registration_status: str
    registered_version_id: str | None
    registered_version_contract: str | None
    delegated_invocation_id: str | None
    delegated_status: str | None
    contract_version: str
    created_at: datetime
    completed_at: datetime
    receipt_hash: str

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_plan_id": self.application_plan_id,
            "attention_item_id": self.attention_item_id,
            "contract_version": self.contract_version,
            "delegated_invocation_id": self.delegated_invocation_id,
            "delegated_status": self.delegated_status,
            "invocation_id": self.invocation_id,
            "registered_version_contract": self.registered_version_contract,
            "registered_version_id": self.registered_version_id,
            "registration_status": self.registration_status,
            "replaced_source_hash": self.replaced_source_hash,
            "replaced_version_contract": self.replaced_version_contract,
            "replaced_version_id": self.replaced_version_id,
            "replacement_target_hash": self.replacement_target_hash,
            "replacement_target_id": self.replacement_target_id,
            "replacement_target_version": self.replacement_target_version,
            "root_family_id": self.root_family_id,
            "subject_id": self.subject_id,
            "uploaded_source_hash": self.uploaded_source_hash,
            "upload_policy_version": self.upload_policy_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "completed_at": _time(self.completed_at),
            "created_at": _time(self.created_at),
            "receipt_hash": self.receipt_hash,
            "receipt_id": self.receipt_id,
        }

    @classmethod
    def create(
        cls,
        *,
        target: Any,
        invocation_id: str,
        uploaded_source_hash: str,
        registration_status: str,
        version: ResumeLatexVersion | None,
        delegated_invocation_id: str | None,
        delegated_status: str | None,
        now: datetime,
    ) -> "NewBaseLatexVersionReplacementReceipt":
        payload = target.payload
        values = {
            "application_plan_id": target.application_plan_id,
            "attention_item_id": target.attention_item_id,
            "contract_version": NEW_BASE_LATEX_REPLACEMENT_CONTRACT_VERSION,
            "delegated_invocation_id": delegated_invocation_id,
            "delegated_status": delegated_status,
            "invocation_id": invocation_id,
            "registered_version_contract": (
                version.contract_version if version is not None else None
            ),
            "registered_version_id": (
                version.latex_version_id if version is not None else None
            ),
            "registration_status": registration_status,
            "replaced_source_hash": target.current_input_content_hash,
            "replaced_version_contract": target.current_input_version,
            "replaced_version_id": target.current_input_record_id,
            "replacement_target_hash": target.target_hash,
            "replacement_target_id": target.target_id,
            "replacement_target_version": target.target_version,
            "root_family_id": payload.version_family,
            "subject_id": target.subject_id,
            "uploaded_source_hash": uploaded_source_hash,
            "upload_policy_version": NEW_BASE_LATEX_UPLOAD_POLICY_VERSION,
        }
        digest = _hash(values)
        return cls(
            receipt_id="new-base-latex-version-replacement-" + digest,
            subject_id=target.subject_id,
            application_plan_id=target.application_plan_id,
            attention_item_id=target.attention_item_id,
            invocation_id=invocation_id,
            replacement_target_id=target.target_id,
            replacement_target_version=target.target_version,
            replacement_target_hash=target.target_hash,
            replaced_version_id=target.current_input_record_id,
            replaced_version_contract=target.current_input_version,
            replaced_source_hash=target.current_input_content_hash,
            root_family_id=payload.version_family,
            uploaded_source_hash=uploaded_source_hash,
            upload_policy_version=NEW_BASE_LATEX_UPLOAD_POLICY_VERSION,
            registration_status=registration_status,
            registered_version_id=(
                version.latex_version_id if version is not None else None
            ),
            registered_version_contract=(
                version.contract_version if version is not None else None
            ),
            delegated_invocation_id=delegated_invocation_id,
            delegated_status=delegated_status,
            contract_version=NEW_BASE_LATEX_REPLACEMENT_CONTRACT_VERSION,
            created_at=now,
            completed_at=now,
            receipt_hash=digest,
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "NewBaseLatexVersionReplacementReceipt":
        receipt = cls(
            receipt_id=str(value["receipt_id"]),
            subject_id=str(value["subject_id"]),
            application_plan_id=str(value["application_plan_id"]),
            attention_item_id=str(value["attention_item_id"]),
            invocation_id=str(value["invocation_id"]),
            replacement_target_id=str(value["replacement_target_id"]),
            replacement_target_version=str(
                value["replacement_target_version"]
            ),
            replacement_target_hash=str(value["replacement_target_hash"]),
            replaced_version_id=str(value["replaced_version_id"]),
            replaced_version_contract=str(
                value["replaced_version_contract"]
            ),
            replaced_source_hash=str(value["replaced_source_hash"]),
            root_family_id=str(value["root_family_id"]),
            uploaded_source_hash=str(value["uploaded_source_hash"]),
            upload_policy_version=str(value["upload_policy_version"]),
            registration_status=str(value["registration_status"]),
            registered_version_id=value.get("registered_version_id"),
            registered_version_contract=value.get(
                "registered_version_contract"
            ),
            delegated_invocation_id=value.get("delegated_invocation_id"),
            delegated_status=value.get("delegated_status"),
            contract_version=str(value["contract_version"]),
            created_at=_parse_time(value["created_at"]),
            completed_at=_parse_time(value["completed_at"]),
            receipt_hash=str(value["receipt_hash"]),
        )
        if (
            receipt.contract_version
            != NEW_BASE_LATEX_REPLACEMENT_CONTRACT_VERSION
            or receipt.upload_policy_version
            != NEW_BASE_LATEX_UPLOAD_POLICY_VERSION
            or receipt.receipt_hash != _hash(receipt.identity_dict())
            or receipt.receipt_id
            != "new-base-latex-version-replacement-" + receipt.receipt_hash
        ):
            raise ValueError("new Base LaTeX receipt integrity failure")
        return receipt


class NewBaseLatexVersionReplacementReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        key = hashlib.sha256(subject_id.encode()).hexdigest()
        return (
            self._home.paths.preparation
            / "new-base-latex-version-replacements"
            / ("subject-" + key)
        )

    def save(self, receipt: NewBaseLatexVersionReplacementReceipt) -> bool:
        path = self._directory(receipt.subject_id) / (
            receipt.receipt_id + ".json"
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable new Base LaTeX receipt conflict")
        return created

    def get_by_invocation(
        self, *, subject_id: str, invocation_id: str
    ) -> NewBaseLatexVersionReplacementReceipt | None:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return None
        matches = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            path = self._home.contained_path(path)
            receipt = NewBaseLatexVersionReplacementReceipt.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if receipt.subject_id != subject_id:
                raise ValueError("new Base LaTeX receipt subject mismatch")
            if receipt.invocation_id == invocation_id:
                matches.append(receipt)
        if len(matches) > 1:
            raise ValueError("duplicate Base LaTeX replacement invocation")
        return matches[0] if matches else None


@dataclass(frozen=True, slots=True)
class NewBaseLatexVersionReplacementResult:
    status: NewBaseLatexVersionReplacementStatus
    receipt: NewBaseLatexVersionReplacementReceipt | None
    reason_code: str | None
    message: str


QueueReader = Callable[
    ..., HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult]
]
RegistrationCallable = Callable[
    ...,
    RegisterResumeLatexVersionResult
    | Awaitable[RegisterResumeLatexVersionResult],
]
ReplacementCallable = Callable[
    ...,
    InputReplacementResolutionResult
    | Awaitable[InputReplacementResolutionResult],
]


async def register_and_replace_base_latex_version(
    command: NewBaseLatexVersionReplacementCommand,
    *,
    queue_reader: QueueReader,
    target_provider: InputReplacementTargetProvider,
    registration_callable: RegistrationCallable,
    latex_version_provider: ResumeLatexVersionProvider,
    replacement_callable: ReplacementCallable,
    receipt_repository: NewBaseLatexVersionReplacementReceiptRepository,
) -> NewBaseLatexVersionReplacementResult:
    """Register/reuse one same-family strict source and delegate once."""

    try:
        subject = _text("subject_id", command.subject_id, 160)
        item_id = _text("attention_item_id", command.attention_item_id, 240)
        invocation = _invocation(command.invocation_id)
        _safe_display_text("display_label", command.display_label, 120)
        if (
            command.version_note is not None
            and str(command.version_note).strip()
        ):
            _safe_display_text("version_note", command.version_note, 500)
        _time(command.now)
        queue = await _resolve(queue_reader(subject_id=subject, now=command.now))
        if (
            not isinstance(queue, HumanAttentionQueueResult)
            or queue.status is not HumanAttentionQueueStatus.SUCCEEDED
            or queue.subject_id != subject
        ):
            raise ValueError("current attention queue is unavailable")
        replay = receipt_repository.get_by_invocation(
            subject_id=subject, invocation_id=invocation
        )
        if replay is not None:
            return _result(
                NewBaseLatexVersionReplacementStatus.UNCHANGED,
                replay,
                None,
                "This Base LaTeX upload invocation is unchanged.",
            )
        item = next(
            (value for value in queue.items if value.item_id == item_id), None
        )
        if item is None:
            return _result(
                NewBaseLatexVersionReplacementStatus.ITEM_NOT_CURRENT,
                None,
                "ITEM_NOT_CURRENT",
                "The replacement item is no longer current.",
            )
        if (
            item.subject_id != subject
            or item.audience is not HumanAttentionAudience.USER
            or item.resolution_capability
            is not HumanAttentionResolutionCapability.REPLACE_INPUT
        ):
            return _result(
                NewBaseLatexVersionReplacementStatus.UNSUPPORTED_TARGET,
                None,
                "UNSUPPORTED_TARGET",
                "This item does not accept a Base LaTeX source.",
            )
        target = target_provider.get_current_typed_target(item=item)
        if (
            target is None
            or not isinstance(
                target.payload, BaseLatexVersionReplacementTarget
            )
            or InputReplacementMethod.REGISTER_NEW_LATEX_VERSION
            not in target.payload.allowed_replacement_methods
        ):
            return _result(
                NewBaseLatexVersionReplacementStatus.UNSUPPORTED_TARGET,
                None,
                "UNSUPPORTED_TARGET",
                "This replacement target does not accept a LaTeX version.",
            )
        listed = latex_version_provider.list_selectable(subject)
        if (
            listed.status is not ResumeLatexVersionListStatus.SUCCEEDED
            or listed.subject_id != subject
        ):
            raise ValueError("LaTeX Version registry is unavailable")
        old = next(
            (
                value
                for value in listed.versions
                if value.latex_version_id
                == target.current_input_record_id
            ),
            None,
        )
        if (
            old is None
            or old.contract_version != target.current_input_version
            or old.source_sha256 != target.current_input_content_hash
            or old.root_family_id != target.payload.version_family
        ):
            return _result(
                NewBaseLatexVersionReplacementStatus.TARGET_STALE,
                None,
                "TARGET_STALE",
                "The replacement target no longer binds the old version.",
            )
        content = command.uploaded_content
        if (
            not isinstance(content, bytes)
            or not content
            or len(content) > NEW_BASE_LATEX_UPLOAD_MAX_BYTES
        ):
            return _result(
                NewBaseLatexVersionReplacementStatus.UPLOAD_REJECTED,
                None,
                "UPLOAD_REJECTED",
                "The source is empty or exceeds the service limit.",
            )
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError:
            return _result(
                NewBaseLatexVersionReplacementStatus.UNSUPPORTED_UPLOAD_TYPE,
                None,
                "UNSUPPORTED_UPLOAD_TYPE",
                "Only one validated UTF-8 LaTeX text source is supported.",
            )
        if _SAFE_TEXT.fullmatch(source) is None:
            return _result(
                NewBaseLatexVersionReplacementStatus.UNSUPPORTED_UPLOAD_TYPE,
                None,
                "UNSUPPORTED_UPLOAD_TYPE",
                "Binary or control-character content is not supported.",
            )
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash == target.current_input_content_hash:
            return _result(
                NewBaseLatexVersionReplacementStatus.UPLOAD_REJECTED,
                None,
                "SAME_INPUT_CONTENT",
                "The uploaded source is the same as the unusable input.",
            )
        registration = await _register(
            registration_callable,
            RegisterResumeLatexVersionCommand(
                subject_id=subject,
                source_kind=ResumeLatexSourceKind.USER_PROVIDED,
                now=command.now,
                latex_source=source,
                parent_version_id=old.latex_version_id,
                root_family_id=old.root_family_id,
                source_profile=(
                    LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1
                ),
            ),
        )
        if (
            not isinstance(registration, RegisterResumeLatexVersionResult)
            or registration.status is RegisterResumeLatexVersionStatus.FAILED
            or registration.version is None
        ):
            registration_status = (
                registration.status.value
                if isinstance(registration, RegisterResumeLatexVersionResult)
                else "FAILED"
            )
            receipt = NewBaseLatexVersionReplacementReceipt.create(
                target=target,
                invocation_id=invocation,
                uploaded_source_hash=content_hash,
                registration_status=registration_status,
                version=None,
                delegated_invocation_id=None,
                delegated_status=None,
                now=command.now,
            )
            receipt_repository.save(receipt)
            status = _registration_failure_status(registration)
            return _result(
                status,
                receipt,
                (
                    registration.reason_code.value
                    if isinstance(registration, RegisterResumeLatexVersionResult)
                    and registration.reason_code is not None
                    else "REGISTRATION_FAILED"
                ),
                "The Base LaTeX source could not be registered.",
            )
        version = registration.version
        confirmed_listing = latex_version_provider.list_selectable(subject)
        confirmed = (
            next(
                (
                    value
                    for value in confirmed_listing.versions
                    if value.latex_version_id == version.latex_version_id
                ),
                None,
            )
            if confirmed_listing.status
            is ResumeLatexVersionListStatus.SUCCEEDED
            and confirmed_listing.subject_id == subject
            else None
        )
        if (
            confirmed is None
            or confirmed != version
            or version.subject_id != subject
            or version.root_family_id != target.payload.version_family
            or version.parent_version_id != old.latex_version_id
            or version.source_sha256 != content_hash
            or version.source_profile
            is not LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1
        ):
            receipt = NewBaseLatexVersionReplacementReceipt.create(
                target=target,
                invocation_id=invocation,
                uploaded_source_hash=content_hash,
                registration_status=registration.status.value,
                version=version,
                delegated_invocation_id=None,
                delegated_status=None,
                now=command.now,
            )
            receipt_repository.save(receipt)
            return _result(
                NewBaseLatexVersionReplacementStatus.VERSION_NOT_SELECTABLE,
                receipt,
                "VERSION_NOT_SELECTABLE",
                "The registered LaTeX version is not safely selectable.",
            )
        child_id = _child_invocation(
            invocation, target.target_id, content_hash
        )
        delegated = await _delegate(
            replacement_callable,
            InputReplacementResolutionCommand(
                subject_id=subject,
                attention_item_id=item_id,
                action=InputReplacementAction.SELECT_EXISTING_REPLACEMENT,
                replacement_option_id=version.latex_version_id,
                now=command.now,
                invocation_id=child_id,
            ),
        )
        delegated_status = (
            delegated.status.value
            if isinstance(delegated, InputReplacementResolutionResult)
            else "FAILED"
        )
        receipt = NewBaseLatexVersionReplacementReceipt.create(
            target=target,
            invocation_id=invocation,
            uploaded_source_hash=content_hash,
            registration_status=registration.status.value,
            version=version,
            delegated_invocation_id=child_id,
            delegated_status=delegated_status,
            now=command.now,
        )
        receipt_repository.save(receipt)
        return _result(
            _map_status(registration.status, delegated),
            receipt,
            None,
            "The uploaded Base LaTeX source was processed.",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _result(
            NewBaseLatexVersionReplacementStatus.FAILED,
            None,
            "FAILED",
            "The Base LaTeX upload could not be processed safely.",
        )


async def _register(callable_: RegistrationCallable, command: Any) -> Any:
    try:
        return await _resolve(callable_(command))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


async def _delegate(callable_: ReplacementCallable, command: Any) -> Any:
    try:
        return await _resolve(callable_(command))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _registration_failure_status(value: Any):
    if not isinstance(value, RegisterResumeLatexVersionResult):
        return NewBaseLatexVersionReplacementStatus.REGISTRATION_FAILED
    if value.reason_code in {
        ResumeLatexVersionFailureReason.SOURCE_CAPABILITY_REJECTED,
        ResumeLatexVersionFailureReason.DEPENDENCY_POLICY_REJECTED,
        ResumeLatexVersionFailureReason.SOURCE_UNMANAGED,
    }:
        return NewBaseLatexVersionReplacementStatus.UNSAFE_LATEX_SOURCE
    if value.reason_code in {
        ResumeLatexVersionFailureReason.SOURCE_INVALID,
        ResumeLatexVersionFailureReason.SOURCE_NOT_UTF8,
        ResumeLatexVersionFailureReason.TEMPLATE_CONTRACT_REJECTED,
    }:
        return NewBaseLatexVersionReplacementStatus.INVALID_LATEX_SOURCE
    return NewBaseLatexVersionReplacementStatus.REGISTRATION_FAILED


def _map_status(registration_status: Any, delegated: Any):
    if not isinstance(delegated, InputReplacementResolutionResult):
        return (
            NewBaseLatexVersionReplacementStatus
            .REGISTERED_REPLACEMENT_FAILED
        )
    if delegated.status is (
        InputReplacementResolutionStatus
        .REPLACED_AND_PREPARATION_COMPLETED
    ):
        if registration_status is RegisterResumeLatexVersionStatus.UNCHANGED:
            return (
                NewBaseLatexVersionReplacementStatus
                .EXISTING_CONTENT_REUSED_AND_REPLACED
            )
        return (
            NewBaseLatexVersionReplacementStatus
            .REGISTERED_AND_REPLACED_COMPLETED
        )
    if delegated.status is (
        InputReplacementResolutionStatus.REPLACED_AND_PREPARATION_DEFERRED
    ):
        return (
            NewBaseLatexVersionReplacementStatus
            .REGISTERED_AND_REPLACED_DEFERRED
        )
    if delegated.status is InputReplacementResolutionStatus.UNCHANGED:
        return NewBaseLatexVersionReplacementStatus.UNCHANGED
    return (
        NewBaseLatexVersionReplacementStatus.REGISTERED_REPLACEMENT_FAILED
    )


def _child_invocation(parent: str, target_id: str, content_hash: str) -> str:
    digest = hashlib.sha256(
        f"{parent}\0{target_id}\0{content_hash}\0s3g5".encode()
    ).hexdigest()
    return "input-replacement-child-" + digest


def _invocation(value: Any) -> str:
    if not isinstance(value, str) or _INVOCATION.fullmatch(value) is None:
        raise ValueError("invocation ID is invalid")
    return value


def _safe_display_text(name: str, value: Any, maximum: int) -> str:
    text = _text(name, value, maximum)
    lowered = text.casefold()
    if (
        text.startswith(("/", "\\", "~"))
        or "/users/" in lowered
        or "\\users\\" in lowered
        or "://" in text
    ):
        raise ValueError(f"{name} is unsafe")
    return " ".join(text.split())


def _text(name: str, value: Any, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is invalid")
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{name} is invalid")
    return cleaned


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _time(parsed)
    return parsed


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _result(status, receipt, reason, message):
    return NewBaseLatexVersionReplacementResult(
        status, receipt, reason, message
    )


__all__ = [
    "NEW_BASE_LATEX_REPLACEMENT_CONTRACT_VERSION",
    "NEW_BASE_LATEX_UPLOAD_MAX_BYTES",
    "NEW_BASE_LATEX_UPLOAD_POLICY_VERSION",
    "NewBaseLatexVersionReplacementCommand",
    "NewBaseLatexVersionReplacementReceipt",
    "NewBaseLatexVersionReplacementReceiptRepository",
    "NewBaseLatexVersionReplacementResult",
    "NewBaseLatexVersionReplacementStatus",
    "register_and_replace_base_latex_version",
]
