"""Register an uploaded ResumeCandidate, then delegate replacement to S3g5."""

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
    InputReplacementMethod,
    InputReplacementTargetProvider,
    SourceResumeReplacementTarget,
)
from .private_home import PrivateHome
from .resume_candidates import (
    MAX_RESUME_ARTIFACT_BYTES,
    RegisterResumeCandidateCommand,
    RegisterResumeCandidateResult,
    RegisterResumeCandidateStatus,
    ResumeArtifactType,
    ResumeCandidate,
    ResumeCandidateListStatus,
    ResumeCandidateReadStatus,
    ResumeCandidateRepository,
    ResumeSummarySource,
    ResumeSummaryTrust,
    detect_resume_artifact_type,
)


NEW_RESUME_REPLACEMENT_CONTRACT_VERSION = (
    "new-resume-candidate-replacement-v1"
)
NEW_RESUME_UPLOAD_POLICY_VERSION = "new-resume-upload-policy-v1"
NEW_RESUME_UPLOAD_MAX_BYTES = MAX_RESUME_ARTIFACT_BYTES
_INVOCATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$")
_SUMMARY = (
    "User-uploaded replacement resume; no skills or candidate facts are "
    "asserted by this registration summary."
)


class NewResumeCandidateReplacementStatus(StrEnum):
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
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    REGISTRATION_FAILED = "REGISTRATION_FAILED"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class NewResumeCandidateReplacementCommand:
    subject_id: str
    attention_item_id: str
    invocation_id: str
    uploaded_content: bytes
    display_name: str
    now: datetime


@dataclass(frozen=True, slots=True)
class NewResumeCandidateReplacementReceipt:
    receipt_id: str
    subject_id: str
    application_plan_id: str
    attention_item_id: str
    invocation_id: str
    replacement_target_id: str
    replacement_target_version: str
    replacement_target_hash: str
    uploaded_content_hash: str
    detected_media_type: str
    upload_policy_version: str
    registration_status: str
    candidate_id: str | None
    candidate_version: str | None
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
            "candidate_id": self.candidate_id,
            "candidate_version": self.candidate_version,
            "contract_version": self.contract_version,
            "delegated_invocation_id": self.delegated_invocation_id,
            "delegated_status": self.delegated_status,
            "detected_media_type": self.detected_media_type,
            "invocation_id": self.invocation_id,
            "registration_status": self.registration_status,
            "replacement_target_hash": self.replacement_target_hash,
            "replacement_target_id": self.replacement_target_id,
            "replacement_target_version": self.replacement_target_version,
            "subject_id": self.subject_id,
            "uploaded_content_hash": self.uploaded_content_hash,
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
        subject_id: str,
        application_plan_id: str,
        attention_item_id: str,
        invocation_id: str,
        target_id: str,
        target_version: str,
        target_hash: str,
        content_hash: str,
        media_type: str,
        registration_status: str,
        candidate: ResumeCandidate | None,
        delegated_invocation_id: str | None,
        delegated_status: str | None,
        created_at: datetime,
        completed_at: datetime,
    ) -> "NewResumeCandidateReplacementReceipt":
        content = {
            "application_plan_id": application_plan_id,
            "attention_item_id": attention_item_id,
            "candidate_id": candidate.resume_id if candidate else None,
            "candidate_version": (
                candidate.contract_version if candidate else None
            ),
            "contract_version": NEW_RESUME_REPLACEMENT_CONTRACT_VERSION,
            "delegated_invocation_id": delegated_invocation_id,
            "delegated_status": delegated_status,
            "detected_media_type": media_type,
            "invocation_id": invocation_id,
            "registration_status": registration_status,
            "replacement_target_hash": target_hash,
            "replacement_target_id": target_id,
            "replacement_target_version": target_version,
            "subject_id": subject_id,
            "uploaded_content_hash": content_hash,
            "upload_policy_version": NEW_RESUME_UPLOAD_POLICY_VERSION,
        }
        digest = _hash(content)
        return cls(
            receipt_id="new-resume-candidate-replacement-" + digest,
            subject_id=subject_id,
            application_plan_id=application_plan_id,
            attention_item_id=attention_item_id,
            invocation_id=invocation_id,
            replacement_target_id=target_id,
            replacement_target_version=target_version,
            replacement_target_hash=target_hash,
            uploaded_content_hash=content_hash,
            detected_media_type=media_type,
            upload_policy_version=NEW_RESUME_UPLOAD_POLICY_VERSION,
            registration_status=registration_status,
            candidate_id=candidate.resume_id if candidate else None,
            candidate_version=(
                candidate.contract_version if candidate else None
            ),
            delegated_invocation_id=delegated_invocation_id,
            delegated_status=delegated_status,
            contract_version=NEW_RESUME_REPLACEMENT_CONTRACT_VERSION,
            created_at=created_at,
            completed_at=completed_at,
            receipt_hash=digest,
        )

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "NewResumeCandidateReplacementReceipt":
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
            uploaded_content_hash=str(value["uploaded_content_hash"]),
            detected_media_type=str(value["detected_media_type"]),
            upload_policy_version=str(value["upload_policy_version"]),
            registration_status=str(value["registration_status"]),
            candidate_id=value.get("candidate_id"),
            candidate_version=value.get("candidate_version"),
            delegated_invocation_id=value.get("delegated_invocation_id"),
            delegated_status=value.get("delegated_status"),
            contract_version=str(value["contract_version"]),
            created_at=datetime.fromisoformat(
                str(value["created_at"]).replace("Z", "+00:00")
            ),
            completed_at=datetime.fromisoformat(
                str(value["completed_at"]).replace("Z", "+00:00")
            ),
            receipt_hash=str(value["receipt_hash"]),
        )
        if (
            receipt.contract_version
            != NEW_RESUME_REPLACEMENT_CONTRACT_VERSION
            or receipt.upload_policy_version
            != NEW_RESUME_UPLOAD_POLICY_VERSION
            or receipt.receipt_hash != _hash(receipt.identity_dict())
            or receipt.receipt_id
            != "new-resume-candidate-replacement-" + receipt.receipt_hash
        ):
            raise ValueError("new ResumeCandidate receipt integrity failure")
        _time(receipt.created_at)
        _time(receipt.completed_at)
        return receipt


class NewResumeCandidateReplacementReceiptRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()

    def _directory(self, subject_id: str) -> Path:
        key = hashlib.sha256(subject_id.encode()).hexdigest()
        return (
            self._home.paths.preparation
            / "new-resume-candidate-replacements"
            / ("subject-" + key)
        )

    def save(self, receipt: NewResumeCandidateReplacementReceipt) -> bool:
        path = self._directory(receipt.subject_id) / (
            receipt.receipt_id + ".json"
        )
        content = _json(receipt.to_dict())
        created = self._home.write_bytes_if_absent(path, content)
        if not created and path.read_bytes() != content:
            raise ValueError("immutable new ResumeCandidate receipt conflict")
        return created

    def get_by_invocation(
        self, *, subject_id: str, invocation_id: str
    ) -> NewResumeCandidateReplacementReceipt | None:
        directory = self._home.contained_path(self._directory(subject_id))
        if not directory.exists():
            return None
        matches = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            path = self._home.contained_path(path)
            receipt = NewResumeCandidateReplacementReceipt.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            if receipt.subject_id != subject_id:
                raise ValueError("new ResumeCandidate receipt subject mismatch")
            if receipt.invocation_id == invocation_id:
                matches.append(receipt)
        if len(matches) > 1:
            raise ValueError("duplicate replacement invocation")
        return matches[0] if matches else None


@dataclass(frozen=True, slots=True)
class NewResumeCandidateReplacementResult:
    status: NewResumeCandidateReplacementStatus
    receipt: NewResumeCandidateReplacementReceipt | None
    reason_code: str | None
    message: str


QueueReader = Callable[..., HumanAttentionQueueResult | Awaitable[HumanAttentionQueueResult]]
RegistrationCallable = Callable[
    ...,
    RegisterResumeCandidateResult | Awaitable[RegisterResumeCandidateResult],
]
ReplacementCallable = Callable[
    ...,
    InputReplacementResolutionResult
    | Awaitable[InputReplacementResolutionResult],
]


async def register_and_replace_resume_candidate(
    command: NewResumeCandidateReplacementCommand,
    *,
    queue_reader: QueueReader,
    target_provider: InputReplacementTargetProvider,
    registration_callable: RegistrationCallable,
    candidate_provider: ResumeCandidateRepository,
    replacement_callable: ReplacementCallable,
    receipt_repository: NewResumeCandidateReplacementReceiptRepository,
    home: PrivateHome | None = None,
) -> NewResumeCandidateReplacementResult:
    active_home = home or PrivateHome.discover()
    staged_path: Path | None = None
    staged_created = False
    try:
        subject = _text("subject_id", command.subject_id, 160)
        item_id = _text("attention_item_id", command.attention_item_id, 240)
        invocation = _invocation(command.invocation_id)
        display_name = _safe_display_name(command.display_name)
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
                NewResumeCandidateReplacementStatus.UNCHANGED,
                replay,
                None,
                "This upload invocation is unchanged.",
            )
        item = next(
            (value for value in queue.items if value.item_id == item_id), None
        )
        if item is None:
            return _result(
                NewResumeCandidateReplacementStatus.ITEM_NOT_CURRENT,
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
                NewResumeCandidateReplacementStatus.UNSUPPORTED_TARGET,
                None,
                "UNSUPPORTED_TARGET",
                "This item does not accept a new ResumeCandidate.",
            )
        target = target_provider.get_current_typed_target(item=item)
        if (
            target is None
            or not isinstance(target.payload, SourceResumeReplacementTarget)
            or InputReplacementMethod.REGISTER_NEW_RESUME_CANDIDATE
            not in target.payload.allowed_replacement_methods
        ):
            return _result(
                NewResumeCandidateReplacementStatus.UNSUPPORTED_TARGET,
                None,
                "UNSUPPORTED_TARGET",
                "This replacement target does not accept a new resume.",
            )
        content = command.uploaded_content
        if (
            not isinstance(content, bytes)
            or not content
            or len(content) > NEW_RESUME_UPLOAD_MAX_BYTES
        ):
            return _result(
                NewResumeCandidateReplacementStatus.UPLOAD_REJECTED,
                None,
                "UPLOAD_REJECTED",
                "The uploaded resume is empty or exceeds the service limit.",
            )
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash == target.current_input_content_hash:
            return _result(
                NewResumeCandidateReplacementStatus.UPLOAD_REJECTED,
                None,
                "SAME_INPUT_CONTENT",
                "The uploaded resume is the same as the unusable input.",
            )
        try:
            artifact_type = detect_resume_artifact_type(content)
        except ValueError:
            return _result(
                NewResumeCandidateReplacementStatus.UNSUPPORTED_MEDIA_TYPE,
                None,
                "UNSUPPORTED_MEDIA_TYPE",
                "Only validated PDF or DOCX resume content is supported.",
            )
        media_type = {
            ResumeArtifactType.PDF: "application/pdf",
            ResumeArtifactType.DOCX: (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
        }[artifact_type]
        listed = candidate_provider.list_selectable(subject)
        if (
            listed.status is not ResumeCandidateListStatus.SUCCEEDED
            or listed.subject_id != subject
        ):
            raise ValueError("ResumeCandidate registry is unavailable")
        old = next(
            (
                value
                for value in listed.candidates
                if value.resume_id == target.current_input_record_id
            ),
            None,
        )
        if (
            old is None
            or old.contract_version != target.current_input_version
            or old.artifact_sha256 != target.current_input_content_hash
        ):
            return _result(
                NewResumeCandidateReplacementStatus.TARGET_STALE,
                None,
                "TARGET_STALE",
                "The replacement target no longer binds the current input.",
            )
        existing = next(
            (
                value
                for value in listed.candidates
                if value.artifact_sha256 == content_hash
            ),
            None,
        )
        effective_name = existing.display_name if existing else display_name
        effective_summary = (
            existing.selection_safe_summary if existing else _SUMMARY
        )
        active_home.ensure()
        suffix = ".pdf" if artifact_type is ResumeArtifactType.PDF else ".docx"
        staged_path = active_home.contained_path(
            active_home.paths.master_documents
            / ".resume-candidate-upload-staging"
            / f"{hashlib.sha256(invocation.encode()).hexdigest()}{suffix}"
        )
        staged_created = active_home.write_bytes_if_absent(
            staged_path, content
        )
        if not staged_created and (
            staged_path.is_symlink()
            or not staged_path.is_file()
            or staged_path.read_bytes() != content
        ):
            raise ValueError("upload staging integrity failure")
        try:
            registration = await _resolve(
                registration_callable(
                    RegisterResumeCandidateCommand(
                        subject_id=subject,
                        artifact_path=staged_path,
                        display_name=effective_name,
                        selection_safe_summary=effective_summary,
                        summary_source=(
                            ResumeSummarySource.AUTHENTICATED_CALLER
                        ),
                        summary_trust=ResumeSummaryTrust.USER_CONFIRMED,
                        now=command.now,
                        claimed_artifact_sha256=content_hash,
                    )
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            registration = None
        if (
            not isinstance(registration, RegisterResumeCandidateResult)
            or registration.status is RegisterResumeCandidateStatus.FAILED
            or registration.candidate is None
        ):
            receipt = _receipt(
                target=target,
                command=command,
                content_hash=content_hash,
                media_type=media_type,
                registration_status=(
                    registration.status.value
                    if isinstance(registration, RegisterResumeCandidateResult)
                    else "FAILED"
                ),
                candidate=None,
                child_id=None,
                delegated_status=None,
            )
            receipt_repository.save(receipt)
            return _result(
                NewResumeCandidateReplacementStatus.REGISTRATION_FAILED,
                receipt,
                "REGISTRATION_FAILED",
                "The ResumeCandidate could not be registered.",
            )
        candidate = registration.candidate
        try:
            confirmed = candidate_provider.get(
                subject_id=subject, resume_id=candidate.resume_id
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            confirmed = None
        if (
            confirmed is None
            or confirmed.status is not ResumeCandidateReadStatus.FOUND
            or confirmed.candidate != candidate
            or candidate.subject_id != subject
            or candidate.artifact_sha256 != content_hash
        ):
            receipt = _receipt(
                target=target,
                command=command,
                content_hash=content_hash,
                media_type=media_type,
                registration_status=registration.status.value,
                candidate=candidate,
                child_id=None,
                delegated_status=None,
            )
            receipt_repository.save(receipt)
            return _result(
                NewResumeCandidateReplacementStatus
                .REGISTERED_REPLACEMENT_FAILED,
                receipt,
                "CANDIDATE_NOT_SELECTABLE",
                "The registered ResumeCandidate is not safely selectable.",
            )
        child_id = _child_invocation(invocation, target.target_id, content_hash)
        try:
            delegated = await _resolve(
                replacement_callable(
                    InputReplacementResolutionCommand(
                        subject_id=subject,
                        attention_item_id=item_id,
                        action=(
                            InputReplacementAction.SELECT_EXISTING_REPLACEMENT
                        ),
                        replacement_option_id=candidate.resume_id,
                        now=command.now,
                        invocation_id=child_id,
                    )
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            delegated = None
        delegated_status = (
            delegated.status.value
            if isinstance(delegated, InputReplacementResolutionResult)
            else "FAILED"
        )
        receipt = _receipt(
            target=target,
            command=command,
            content_hash=content_hash,
            media_type=media_type,
            registration_status=registration.status.value,
            candidate=candidate,
            child_id=child_id,
            delegated_status=delegated_status,
        )
        receipt_repository.save(receipt)
        status = _map_status(registration.status, delegated)
        return _result(status, receipt, None, "The uploaded resume was processed.")
    except (OSError, RuntimeError, TypeError, ValueError):
        return _result(
            NewResumeCandidateReplacementStatus.FAILED,
            None,
            "FAILED",
            "The uploaded resume could not be processed safely.",
        )
    finally:
        if staged_created and staged_path is not None:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass


def _receipt(
    *,
    target,
    command,
    content_hash,
    media_type,
    registration_status,
    candidate,
    child_id,
    delegated_status,
):
    return NewResumeCandidateReplacementReceipt.create(
        subject_id=target.subject_id,
        application_plan_id=target.application_plan_id,
        attention_item_id=target.attention_item_id,
        invocation_id=command.invocation_id,
        target_id=target.target_id,
        target_version=target.target_version,
        target_hash=target.target_hash,
        content_hash=content_hash,
        media_type=media_type,
        registration_status=registration_status,
        candidate=candidate,
        delegated_invocation_id=child_id,
        delegated_status=delegated_status,
        created_at=command.now,
        completed_at=command.now,
    )


def _map_status(registration_status, delegated):
    if not isinstance(delegated, InputReplacementResolutionResult):
        return NewResumeCandidateReplacementStatus.REGISTERED_REPLACEMENT_FAILED
    if delegated.status is (
        InputReplacementResolutionStatus
        .REPLACED_AND_PREPARATION_COMPLETED
    ):
        if registration_status is RegisterResumeCandidateStatus.UNCHANGED:
            return (
                NewResumeCandidateReplacementStatus
                .EXISTING_CONTENT_REUSED_AND_REPLACED
            )
        return (
            NewResumeCandidateReplacementStatus
            .REGISTERED_AND_REPLACED_COMPLETED
        )
    if delegated.status is (
        InputReplacementResolutionStatus.REPLACED_AND_PREPARATION_DEFERRED
    ):
        return (
            NewResumeCandidateReplacementStatus
            .REGISTERED_AND_REPLACED_DEFERRED
        )
    if delegated.status is InputReplacementResolutionStatus.UNCHANGED:
        return NewResumeCandidateReplacementStatus.UNCHANGED
    return NewResumeCandidateReplacementStatus.REGISTERED_REPLACEMENT_FAILED


def _child_invocation(parent: str, target_id: str, content_hash: str) -> str:
    digest = hashlib.sha256(
        f"{parent}\0{target_id}\0{content_hash}\0s3g5".encode()
    ).hexdigest()
    return "input-replacement-child-" + digest


def _safe_display_name(value: Any) -> str:
    text = _text("display_name", value, 120)
    lowered = text.casefold()
    if (
        text.startswith(("/", "\\", "~"))
        or "/users/" in lowered
        or "\\users\\" in lowered
        or "://" in text
    ):
        raise ValueError("display name is unsafe")
    return " ".join(text.split())


def _invocation(value: Any) -> str:
    if not isinstance(value, str) or _INVOCATION.fullmatch(value) is None:
        raise ValueError("invocation ID is invalid")
    return value


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


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_json(value)).hexdigest()


def _result(status, receipt, reason, message):
    return NewResumeCandidateReplacementResult(status, receipt, reason, message)


__all__ = [
    "NEW_RESUME_REPLACEMENT_CONTRACT_VERSION",
    "NEW_RESUME_UPLOAD_MAX_BYTES",
    "NEW_RESUME_UPLOAD_POLICY_VERSION",
    "NewResumeCandidateReplacementCommand",
    "NewResumeCandidateReplacementReceipt",
    "NewResumeCandidateReplacementReceiptRepository",
    "NewResumeCandidateReplacementResult",
    "NewResumeCandidateReplacementStatus",
    "register_and_replace_resume_candidate",
]
