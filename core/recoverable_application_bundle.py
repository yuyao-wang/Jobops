"""Recoverable, subject-isolated snapshots of assembled ApplicationBundle values."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping, Protocol, runtime_checkable

from .application_answer_taxonomy import (
    CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION,
    CanonicalApplicationAnswerKey,
    CanonicalApplicationAnswers,
)
from .bundles import (
    APPLICATION_BUNDLE_CONTRACT_VERSION,
    ApplicationBundle,
    JobSpec,
    ManagedArtifactReference,
    MaterialBundle,
    application_bundle_canonical_hash,
)
from .policy import (
    AnswerAuthority,
    ApprovalActor,
    AutonomyMode,
    CoverLetterStrategy,
    JobTier,
    MaterialStrategy,
    PolicyBlocker,
    PolicyDecision,
    SubmitAuthority,
    VerificationAuthority,
)
from .private_home import PrivateHome, PrivateHomeError


RECOVERABLE_APPLICATION_BUNDLE_ENVELOPE_CONTRACT_VERSION = (
    "recoverable-application-bundle-envelope-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_ASSEMBLY_ID_RE = re.compile(
    r"^application-bundle-assembly-[a-f0-9]{64}$"
)
_ENVELOPE_ID_RE = re.compile(
    r"^recoverable-application-bundle-envelope-[a-f0-9]{64}$"
)


class RecoverableApplicationBundleEnvelopeWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class RecoverableApplicationBundleEnvelopeReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    FAILED = "FAILED"


class RecoverableApplicationBundleEnvelopeFailureReason(StrEnum):
    INVALID_BINDING = "INVALID_BINDING"
    UNSUPPORTED_BUNDLE_VALUE = "UNSUPPORTED_BUNDLE_VALUE"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    IMMUTABLE_CONFLICT = "IMMUTABLE_CONFLICT"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _clean(name: str, value: Any, *, maximum: int = 200) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the envelope contract")
    return cleaned


def _require_hash(name: str, value: Any) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _rfc3339(value: datetime) -> str:
    return (
        _aware("created_at", value)
        .astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("persisted envelope timestamp is invalid")
    return _aware(
        "created_at", datetime.fromisoformat(value.replace("Z", "+00:00"))
    )


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def _encode_value(value: Any) -> dict[str, Any]:
    """Encode the small value-only surface used by profile/answers/metadata."""

    if value is None:
        return {"kind": "none"}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if type(value) is int:
        return {"kind": "int", "value": value}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite bundle values are unsupported")
        return {"kind": "float", "value": value}
    if isinstance(value, str):
        return {"kind": "str", "value": value}
    if isinstance(value, tuple):
        return {
            "items": [_encode_value(item) for item in value],
            "kind": "tuple",
        }
    if isinstance(value, list):
        return {
            "items": [_encode_value(item) for item in value],
            "kind": "list",
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("bundle mappings must use string keys")
        return {
            "items": [
                [key, _encode_value(value[key])] for key in sorted(value)
            ],
            "kind": "mapping",
        }
    raise TypeError(
        f"runtime-only bundle value is unsupported: {type(value).__name__}"
    )


def _decode_value(value: Any) -> Any:
    if not isinstance(value, Mapping) or not isinstance(
        value.get("kind"), str
    ):
        raise ValueError("encoded bundle value is invalid")
    kind = value["kind"]
    if kind == "none" and set(value) == {"kind"}:
        return None
    if kind == "bool" and set(value) == {"kind", "value"} and type(
        value["value"]
    ) is bool:
        return value["value"]
    if kind == "int" and set(value) == {"kind", "value"} and type(
        value["value"]
    ) is int:
        return value["value"]
    if kind == "float" and set(value) == {"kind", "value"} and isinstance(
        value["value"], float
    ) and math.isfinite(value["value"]):
        return value["value"]
    if kind == "str" and set(value) == {"kind", "value"} and isinstance(
        value["value"], str
    ):
        return value["value"]
    if kind in {"tuple", "list"} and set(value) == {"items", "kind"}:
        if not isinstance(value["items"], list):
            raise ValueError("encoded sequence is invalid")
        decoded = [_decode_value(item) for item in value["items"]]
        return tuple(decoded) if kind == "tuple" else decoded
    if kind == "mapping" and set(value) == {"items", "kind"}:
        if not isinstance(value["items"], list):
            raise ValueError("encoded mapping is invalid")
        result: dict[str, Any] = {}
        for item in value["items"]:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
                or item[0] in result
            ):
                raise ValueError("encoded mapping entry is invalid")
            result[item[0]] = _decode_value(item[1])
        return result
    raise ValueError("encoded bundle value kind is unsupported")


def _relative_resume_reference(
    bundle: ApplicationBundle, home: PrivateHome, subject_id: str
) -> str:
    try:
        path = bundle.materials.resume_path.expanduser().resolve()
        relative = path.relative_to(home.root.expanduser().resolve()).as_posix()
    except (OSError, ValueError) as exc:
        raise ValueError("Resume is outside Private Home") from exc
    parts = PurePosixPath(relative).parts
    if (
        parts[:2] != ("state", "preparation")
        or _subject_key(subject_id) not in parts
    ):
        raise ValueError("Resume reference is not subject-isolated")
    return relative


def serialize_application_bundle(
    bundle: ApplicationBundle, *, home: PrivateHome, subject_id: str
) -> dict[str, Any]:
    if not isinstance(bundle, ApplicationBundle):
        raise TypeError("bundle must be an ApplicationBundle")
    cover = bundle.materials.cover_letter_pdf
    return {
        "answers": {
            "entries": [
                {
                    "key": key.value,
                    "value": _encode_value(value),
                }
                for key, value in bundle.answers.entries
            ],
            "taxonomy_version": bundle.answers.taxonomy_version,
        },
        "contract_version": APPLICATION_BUNDLE_CONTRACT_VERSION,
        "job": {
            "company": bundle.job.company,
            "job_id": bundle.job.job_id,
            "tier": bundle.job.tier.value,
            "title": bundle.job.title,
            "url": bundle.job.url,
        },
        "materials": {
            "cover_letter": bundle.materials.cover_letter,
            "cover_letter_pdf": cover.to_dict() if cover else None,
            "cover_letter_sha256": bundle.materials.cover_letter_sha256,
            "metadata": _encode_value(bundle.materials.metadata),
            "resume_reference": _relative_resume_reference(
                bundle, home, subject_id
            ),
            "resume_sha256": bundle.materials.resume_sha256,
        },
        "policy": {
            "answer_authority": bundle.policy.answer_authority.value,
            "blockers": [item.value for item in bundle.policy.blockers],
            "cover_letter_strategy": (
                bundle.policy.cover_letter_strategy.value
            ),
            "email_verification_authority": (
                bundle.policy.email_verification_authority.value
            ),
            "gate_a_actor": bundle.policy.gate_a_actor.value,
            "gate_b_actor": bundle.policy.gate_b_actor.value,
            "material_strategy": bundle.policy.material_strategy.value,
            "mode": bundle.policy.mode.value,
            "policy_hash": bundle.policy.policy_hash,
            "submit_authority": bundle.policy.submit_authority.value,
            "tier": bundle.policy.tier.value,
        },
        "profile": _encode_value(bundle.profile),
        "run_id": bundle.run_id,
    }


def deserialize_application_bundle(
    value: Any, *, home: PrivateHome, subject_id: str
) -> ApplicationBundle:
    expected = {
        "answers",
        "contract_version",
        "job",
        "materials",
        "policy",
        "profile",
        "run_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted ApplicationBundle fields are invalid")
    if value["contract_version"] != APPLICATION_BUNDLE_CONTRACT_VERSION:
        raise ValueError("ApplicationBundle contract is unsupported")
    job = value["job"]
    materials = value["materials"]
    answers = value["answers"]
    policy = value["policy"]
    if not all(isinstance(item, Mapping) for item in (job, materials, answers, policy)):
        raise ValueError("persisted ApplicationBundle component is invalid")
    if set(job) != {"company", "job_id", "tier", "title", "url"}:
        raise ValueError("persisted JobSpec fields are invalid")
    if set(materials) != {
        "cover_letter",
        "cover_letter_pdf",
        "cover_letter_sha256",
        "metadata",
        "resume_reference",
        "resume_sha256",
    }:
        raise ValueError("persisted MaterialBundle fields are invalid")
    if set(answers) != {"entries", "taxonomy_version"}:
        raise ValueError("persisted canonical answers fields are invalid")
    if set(policy) != {
        "answer_authority",
        "blockers",
        "cover_letter_strategy",
        "email_verification_authority",
        "gate_a_actor",
        "gate_b_actor",
        "material_strategy",
        "mode",
        "policy_hash",
        "submit_authority",
        "tier",
    }:
        raise ValueError("persisted PolicyDecision fields are invalid")
    if answers["taxonomy_version"] != CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION:
        raise ValueError("canonical answers taxonomy is unsupported")
    if not isinstance(answers["entries"], list):
        raise ValueError("canonical answers entries are invalid")
    answer_entries: list[tuple[CanonicalApplicationAnswerKey, Any]] = []
    for entry in answers["entries"]:
        if not isinstance(entry, Mapping) or set(entry) != {"key", "value"}:
            raise ValueError("canonical answer entry is invalid")
        answer_entries.append(
            (
                CanonicalApplicationAnswerKey(entry["key"]),
                _decode_value(entry["value"]),
            )
        )
    resume_reference = _clean(
        "resume_reference", materials["resume_reference"], maximum=800
    )
    if _subject_key(subject_id) not in PurePosixPath(resume_reference).parts:
        raise ValueError("Resume reference belongs to another subject")
    resume_path = home.contained_path(resume_reference)
    cover_value = materials["cover_letter_pdf"]
    cover = (
        None
        if cover_value is None
        else ManagedArtifactReference(**dict(cover_value))
    )
    if cover is not None and _subject_key(subject_id) not in PurePosixPath(
        cover.reference
    ).parts:
        raise ValueError("Cover Letter reference belongs to another subject")
    decoded_metadata = _decode_value(materials["metadata"])
    decoded_profile = _decode_value(value["profile"])
    if not isinstance(decoded_metadata, Mapping) or not isinstance(
        decoded_profile, Mapping
    ):
        raise ValueError("bundle metadata and profile must be mappings")
    return ApplicationBundle(
        run_id=_clean("run_id", value["run_id"], maximum=200),
        job=JobSpec(
            url=job["url"],
            company=job["company"],
            title=job["title"],
            tier=JobTier(job["tier"]),
            job_id=job["job_id"],
        ),
        materials=MaterialBundle(
            resume_path=resume_path,
            resume_sha256=_require_hash(
                "resume_sha256", materials["resume_sha256"]
            ),
            cover_letter=materials["cover_letter"],
            cover_letter_sha256=_require_hash(
                "cover_letter_sha256", materials["cover_letter_sha256"]
            ),
            metadata=dict(decoded_metadata),
            cover_letter_pdf=cover,
        ),
        profile=dict(decoded_profile),
        answers=CanonicalApplicationAnswers(
            entries=tuple(answer_entries),
            taxonomy_version=answers["taxonomy_version"],
        ),
        policy=PolicyDecision(
            mode=AutonomyMode(policy["mode"]),
            tier=JobTier(policy["tier"]),
            material_strategy=MaterialStrategy(policy["material_strategy"]),
            cover_letter_strategy=CoverLetterStrategy(
                policy["cover_letter_strategy"]
            ),
            answer_authority=AnswerAuthority(policy["answer_authority"]),
            gate_a_actor=ApprovalActor(policy["gate_a_actor"]),
            gate_b_actor=ApprovalActor(policy["gate_b_actor"]),
            submit_authority=SubmitAuthority(policy["submit_authority"]),
            email_verification_authority=VerificationAuthority(
                policy["email_verification_authority"]
            ),
            blockers=tuple(PolicyBlocker(item) for item in policy["blockers"]),
            policy_hash=_require_hash("policy_hash", policy["policy_hash"]),
        ),
    )


@dataclass(frozen=True, slots=True)
class RecoverableApplicationBundleEnvelope:
    envelope_id: str
    contract_version: str
    subject_id: str
    application_plan_id: str
    assembly_record_id: str
    assembly_record_content_hash: str
    application_bundle_contract_version: str
    bundle: ApplicationBundle
    bundle_payload: Mapping[str, Any]
    bundle_canonical_hash: str
    envelope_content_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != RECOVERABLE_APPLICATION_BUNDLE_ENVELOPE_CONTRACT_VERSION
        ):
            raise ValueError("envelope contract is unsupported")
        _clean("subject_id", self.subject_id, maximum=160)
        _clean("application_plan_id", self.application_plan_id, maximum=180)
        if _ASSEMBLY_ID_RE.fullmatch(self.assembly_record_id) is None:
            raise ValueError("assembly_record_id is invalid")
        _require_hash(
            "assembly_record_content_hash", self.assembly_record_content_hash
        )
        if (
            self.application_bundle_contract_version
            != APPLICATION_BUNDLE_CONTRACT_VERSION
        ):
            raise ValueError("ApplicationBundle contract is unsupported")
        _require_hash("bundle_canonical_hash", self.bundle_canonical_hash)
        _require_hash("envelope_content_hash", self.envelope_content_hash)
        if not isinstance(self.bundle, ApplicationBundle):
            raise TypeError("envelope bundle must be an ApplicationBundle")
        if application_bundle_canonical_hash(self.bundle) != self.bundle_canonical_hash:
            raise ValueError("envelope bundle hash is invalid")
        expected_id = "recoverable-application-bundle-envelope-" + _hash(
            self.identity_dict()
        )
        if (
            _ENVELOPE_ID_RE.fullmatch(self.envelope_id) is None
            or self.envelope_id != expected_id
        ):
            raise ValueError("envelope identity is invalid")
        _aware("created_at", self.created_at)
        if self.envelope_content_hash != _hash(self.content_dict()):
            raise ValueError("envelope content hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "application_bundle_contract_version": (
                self.application_bundle_contract_version
            ),
            "assembly_record_content_hash": self.assembly_record_content_hash,
            "assembly_record_id": self.assembly_record_id,
            "bundle_canonical_hash": self.bundle_canonical_hash,
            "contract_version": self.contract_version,
            "subject_id": self.subject_id,
        }

    def content_dict(self) -> dict[str, Any]:
        return {
            **self.identity_dict(),
            "application_plan_id": self.application_plan_id,
            "bundle_payload": dict(self.bundle_payload),
            "created_at": _rfc3339(self.created_at),
            "envelope_id": self.envelope_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_dict(),
            "envelope_content_hash": self.envelope_content_hash,
        }


@dataclass(frozen=True, slots=True)
class RecoverableApplicationBundleEnvelopeWriteResult:
    status: RecoverableApplicationBundleEnvelopeWriteStatus
    envelope: RecoverableApplicationBundleEnvelope | None
    reason: RecoverableApplicationBundleEnvelopeFailureReason | None = None


@dataclass(frozen=True, slots=True)
class RecoverableApplicationBundleEnvelopeReadResult:
    status: RecoverableApplicationBundleEnvelopeReadStatus
    envelope: RecoverableApplicationBundleEnvelope | None
    reason: RecoverableApplicationBundleEnvelopeFailureReason | None = None


@runtime_checkable
class RecoverableApplicationBundleEnvelopeRepository(Protocol):
    def save(
        self, envelope: RecoverableApplicationBundleEnvelope
    ) -> RecoverableApplicationBundleEnvelopeWriteResult: ...

    def get_for_assembly(
        self, *, subject_id: str, assembly_record_id: str
    ) -> RecoverableApplicationBundleEnvelopeReadResult: ...


class ApplicationBundleAssemblyBinding(Protocol):
    record_id: str
    record_content_hash: str
    subject_id: str
    application_plan_id: str
    manifest_id: str
    manifest_content_hash: str
    answer_set_id: str
    answer_set_content_hash: str
    resume_entry_id: str
    cover_letter_entry_id: str
    prepared_resume_material_id: str
    prepared_cover_letter_material_id: str
    taxonomy_version: str
    application_bundle_run_id: str
    application_bundle_canonical_hash: str


def create_recoverable_application_bundle_envelope(
    *,
    subject_id: str,
    application_plan_id: str,
    assembly_record: ApplicationBundleAssemblyBinding,
    bundle: ApplicationBundle,
    home: PrivateHome,
    created_at: datetime,
) -> RecoverableApplicationBundleEnvelope:
    subject_id = _clean("subject_id", subject_id, maximum=160)
    application_plan_id = _clean(
        "application_plan_id", application_plan_id, maximum=180
    )
    assembly_record_id = assembly_record.record_id
    assembly_record_content_hash = assembly_record.record_content_hash
    expected_bundle_canonical_hash = (
        assembly_record.application_bundle_canonical_hash
    )
    if _ASSEMBLY_ID_RE.fullmatch(assembly_record_id) is None:
        raise ValueError("assembly_record_id is invalid")
    _require_hash(
        "assembly_record_content_hash", assembly_record_content_hash
    )
    _require_hash(
        "expected_bundle_canonical_hash", expected_bundle_canonical_hash
    )
    created_at = _aware("created_at", created_at)
    if (
        assembly_record.subject_id != subject_id
        or assembly_record.application_plan_id != application_plan_id
        or assembly_record.application_bundle_run_id != bundle.run_id
        or assembly_record.taxonomy_version
        != bundle.answers.taxonomy_version
    ):
        raise ValueError("ApplicationBundle does not match AssemblyRecord binding")
    metadata = bundle.materials.metadata
    expected_metadata = {
        "answer_set_content_hash": assembly_record.answer_set_content_hash,
        "answer_set_id": assembly_record.answer_set_id,
        "application_plan_id": application_plan_id,
        "cover_letter_entry_id": assembly_record.cover_letter_entry_id,
        "manifest_content_hash": assembly_record.manifest_content_hash,
        "manifest_id": assembly_record.manifest_id,
        "prepared_cover_letter_material_id": (
            assembly_record.prepared_cover_letter_material_id
        ),
        "prepared_resume_material_id": (
            assembly_record.prepared_resume_material_id
        ),
        "resume_entry_id": assembly_record.resume_entry_id,
        "source": "plan-scoped-application-bundle-assembly",
    }
    if dict(metadata) != expected_metadata:
        raise ValueError(
            "ApplicationBundle materials do not match AssemblyRecord provenance"
        )
    actual_hash = application_bundle_canonical_hash(bundle)
    if actual_hash != expected_bundle_canonical_hash:
        raise ValueError("ApplicationBundle does not match AssemblyRecord hash")
    payload = serialize_application_bundle(
        bundle, home=home, subject_id=subject_id
    )
    # Round-trip before persistence so unsupported or lossy values fail now.
    recovered = deserialize_application_bundle(
        payload, home=home, subject_id=subject_id
    )
    if application_bundle_canonical_hash(recovered) != actual_hash:
        raise ValueError("ApplicationBundle cannot be recovered losslessly")
    identity = {
        "application_bundle_contract_version": (
            APPLICATION_BUNDLE_CONTRACT_VERSION
        ),
        "assembly_record_content_hash": assembly_record_content_hash,
        "assembly_record_id": assembly_record_id,
        "bundle_canonical_hash": actual_hash,
        "contract_version": (
            RECOVERABLE_APPLICATION_BUNDLE_ENVELOPE_CONTRACT_VERSION
        ),
        "subject_id": subject_id,
    }
    envelope_id = "recoverable-application-bundle-envelope-" + _hash(identity)
    content = {
        **identity,
        "application_plan_id": application_plan_id,
        "bundle_payload": payload,
        "created_at": _rfc3339(created_at),
        "envelope_id": envelope_id,
    }
    return RecoverableApplicationBundleEnvelope(
        envelope_id=envelope_id,
        contract_version=(
            RECOVERABLE_APPLICATION_BUNDLE_ENVELOPE_CONTRACT_VERSION
        ),
        subject_id=subject_id,
        application_plan_id=application_plan_id,
        assembly_record_id=assembly_record_id,
        assembly_record_content_hash=assembly_record_content_hash,
        application_bundle_contract_version=(
            APPLICATION_BUNDLE_CONTRACT_VERSION
        ),
        bundle=recovered,
        bundle_payload=payload,
        bundle_canonical_hash=actual_hash,
        envelope_content_hash=_hash(content),
        created_at=created_at,
    )


def _envelope_from_dict(
    value: Any, *, home: PrivateHome
) -> RecoverableApplicationBundleEnvelope:
    expected = {
        "application_bundle_contract_version",
        "application_plan_id",
        "assembly_record_content_hash",
        "assembly_record_id",
        "bundle_canonical_hash",
        "bundle_payload",
        "contract_version",
        "created_at",
        "envelope_content_hash",
        "envelope_id",
        "subject_id",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted envelope fields are invalid")
    bundle = deserialize_application_bundle(
        value["bundle_payload"],
        home=home,
        subject_id=value["subject_id"],
    )
    return RecoverableApplicationBundleEnvelope(
        **{
            **value,
            "bundle": bundle,
            "created_at": _parse_time(value["created_at"]),
        }
    )


class PrivateHomeRecoverableApplicationBundleEnvelopeRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _directory(self, subject_id: str) -> Path:
        return (
            self._home.paths.recoverable_application_bundle_envelopes
            / _subject_key(_clean("subject_id", subject_id, maximum=160))
        )

    def _path(self, subject_id: str, assembly_record_id: str) -> Path:
        if _ASSEMBLY_ID_RE.fullmatch(assembly_record_id) is None:
            raise ValueError("assembly_record_id is invalid")
        return self._directory(subject_id) / f"{assembly_record_id}.json"

    def get_for_assembly(
        self, *, subject_id: str, assembly_record_id: str
    ) -> RecoverableApplicationBundleEnvelopeReadResult:
        try:
            path = self._path(subject_id, assembly_record_id)
        except (TypeError, ValueError):
            return RecoverableApplicationBundleEnvelopeReadResult(
                RecoverableApplicationBundleEnvelopeReadStatus.FAILED,
                None,
                RecoverableApplicationBundleEnvelopeFailureReason
                .INVALID_BINDING,
            )
        with self._lock:
            if not path.exists():
                return RecoverableApplicationBundleEnvelopeReadResult(
                    RecoverableApplicationBundleEnvelopeReadStatus.NOT_FOUND,
                    None,
                )
            if path.is_symlink() or not path.is_file():
                return RecoverableApplicationBundleEnvelopeReadResult(
                    RecoverableApplicationBundleEnvelopeReadStatus.FAILED,
                    None,
                    RecoverableApplicationBundleEnvelopeFailureReason
                    .INTEGRITY_FAILURE,
                )
            try:
                envelope = _envelope_from_dict(
                    json.loads(path.read_text(encoding="utf-8")),
                    home=self._home,
                )
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
                PrivateHomeError,
            ):
                return RecoverableApplicationBundleEnvelopeReadResult(
                    RecoverableApplicationBundleEnvelopeReadStatus.FAILED,
                    None,
                    RecoverableApplicationBundleEnvelopeFailureReason
                    .INTEGRITY_FAILURE,
                )
            if (
                envelope.subject_id != subject_id.strip()
                or envelope.assembly_record_id != assembly_record_id
            ):
                return RecoverableApplicationBundleEnvelopeReadResult(
                    RecoverableApplicationBundleEnvelopeReadStatus.FAILED,
                    None,
                    RecoverableApplicationBundleEnvelopeFailureReason
                    .INTEGRITY_FAILURE,
                )
            return RecoverableApplicationBundleEnvelopeReadResult(
                RecoverableApplicationBundleEnvelopeReadStatus.FOUND,
                envelope,
            )

    def save(
        self, envelope: RecoverableApplicationBundleEnvelope
    ) -> RecoverableApplicationBundleEnvelopeWriteResult:
        if not isinstance(envelope, RecoverableApplicationBundleEnvelope):
            raise TypeError(
                "envelope must be a RecoverableApplicationBundleEnvelope"
            )
        path = self._path(
            envelope.subject_id, envelope.assembly_record_id
        )
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            envelope.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return RecoverableApplicationBundleEnvelopeWriteResult(
                    RecoverableApplicationBundleEnvelopeWriteStatus.FAILED,
                    None,
                    RecoverableApplicationBundleEnvelopeFailureReason
                    .PERSISTENCE_FAILED,
                )
            if created:
                return RecoverableApplicationBundleEnvelopeWriteResult(
                    RecoverableApplicationBundleEnvelopeWriteStatus.CREATED,
                    envelope,
                )
            existing = self.get_for_assembly(
                subject_id=envelope.subject_id,
                assembly_record_id=envelope.assembly_record_id,
            )
            if (
                existing.status
                is RecoverableApplicationBundleEnvelopeReadStatus.FOUND
                and existing.envelope is not None
                and existing.envelope.envelope_id == envelope.envelope_id
                and existing.envelope.envelope_content_hash
                == envelope.envelope_content_hash
            ):
                return RecoverableApplicationBundleEnvelopeWriteResult(
                    RecoverableApplicationBundleEnvelopeWriteStatus.UNCHANGED,
                    existing.envelope,
                )
            return RecoverableApplicationBundleEnvelopeWriteResult(
                RecoverableApplicationBundleEnvelopeWriteStatus.FAILED,
                None,
                RecoverableApplicationBundleEnvelopeFailureReason
                .IMMUTABLE_CONFLICT,
            )


__all__ = [
    "RECOVERABLE_APPLICATION_BUNDLE_ENVELOPE_CONTRACT_VERSION",
    "PrivateHomeRecoverableApplicationBundleEnvelopeRepository",
    "RecoverableApplicationBundleEnvelope",
    "RecoverableApplicationBundleEnvelopeFailureReason",
    "RecoverableApplicationBundleEnvelopeReadResult",
    "RecoverableApplicationBundleEnvelopeReadStatus",
    "RecoverableApplicationBundleEnvelopeRepository",
    "RecoverableApplicationBundleEnvelopeWriteResult",
    "RecoverableApplicationBundleEnvelopeWriteStatus",
    "ApplicationBundleAssemblyBinding",
    "create_recoverable_application_bundle_envelope",
    "deserialize_application_bundle",
    "serialize_application_bundle",
]
