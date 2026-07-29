"""HMAC-signed, expiring, one-time permits for both submission gates."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping

from auth.credentials import CredentialStore

from .event_ledger import EventLedger, PermitAlreadyConsumedError


PERMIT_SCHEMA_VERSION = 1
PLAN_SCOPED_SUBMISSION_BINDING_VERSION = (
    "plan-scoped-submission-permit-bindings-v1"
)
GATE_A_CONSUMPTION_REFERENCE_VERSION = "gate-a-consumption-reference-v1"
PERMIT_SIGNER_PROVIDER_VERSION = "foundation-permit-signer-v1"
PERMIT_TOKEN_REFERENCE_VERSION = "opaque-permit-token-reference-v1"
PERMIT_TOKEN_SERVICE = "jobops.core.permit-tokens.v1"
SUBMISSION_PERMIT_CONSUMPTION_REFERENCE_VERSION = (
    "submission-permit-consumption-reference-v1"
)


class PermitError(RuntimeError):
    pass


class PermitSignatureError(PermitError):
    pass


class PermitExpiredError(PermitError):
    pass


class PermitBindingError(PermitError):
    pass


class PermitGateError(PermitError):
    pass


class PermitPrerequisiteError(PermitError):
    pass


class PermitConsumedError(PermitError):
    pass


class PermitIssuerUnavailableError(PermitError):
    pass


class PermitGate(StrEnum):
    GATE_A = "GATE_A"
    GATE_B = "GATE_B"


class SubmissionPermitAction(StrEnum):
    SUBMIT_APPLICATION = "SUBMIT_APPLICATION"


def hash_value(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PermitSignatureError("invalid permit encoding") from exc


@dataclass(frozen=True, slots=True)
class PermitBindings:
    """All mutable inputs that approval authorizes."""

    run_id: str
    job_id: str
    job_url_hash: str
    material_hash: str
    answer_hash: str
    review_hash: str
    policy_hash: str

    def __post_init__(self) -> None:
        for field_name in (
            "run_id",
            "job_id",
            "job_url_hash",
            "material_hash",
            "answer_hash",
            "review_hash",
            "policy_hash",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "job_url_hash": self.job_url_hash,
            "material_hash": self.material_hash,
            "answer_hash": self.answer_hash,
            "review_hash": self.review_hash,
            "policy_hash": self.policy_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PermitBindings":
        return cls(**{key: str(value[key]) for key in cls.__dataclass_fields__})

    @property
    def digest(self) -> str:
        return hash_value(_canonical_json(self.to_dict()))

    @property
    def stable_digest(self) -> str:
        """Digest fields that must remain unchanged between Gate A and Gate B.

        The review hash intentionally changes: Gate A approves the application
        plan, while Gate B approves the final read-back from the ATS.
        """

        value = self.to_dict()
        value.pop("review_hash")
        return hash_value(_canonical_json(value))


@dataclass(frozen=True, slots=True)
class PlanScopedSubmissionPermitBindings:
    """Explicit V1 bindings for one authorized Plan/Bundle/Review submit."""

    contract_version: str
    run_id: str
    job_id: str
    job_url_hash: str
    material_hash: str
    answer_hash: str
    review_hash: str
    policy_hash: str
    subject_id: str
    application_plan_id: str
    bundle_canonical_hash: str
    authorization_decision_id: str
    authorization_decision_hash: str
    execution_record_id: str
    execution_record_hash: str
    adapter_platform: str
    action: SubmissionPermitAction
    permit_policy_version: str

    def __post_init__(self) -> None:
        if self.contract_version != PLAN_SCOPED_SUBMISSION_BINDING_VERSION:
            raise ValueError("plan-scoped permit binding version is unsupported")
        for field_name in self.__dataclass_fields__:
            if field_name == "action":
                continue
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")
        object.__setattr__(
            self, "action", SubmissionPermitAction(self.action)
        )
        if self.action is not SubmissionPermitAction.SUBMIT_APPLICATION:
            raise ValueError("plan-scoped permit action is unsupported")

    def to_dict(self) -> dict[str, str]:
        return {
            "contract_version": self.contract_version,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "job_url_hash": self.job_url_hash,
            "material_hash": self.material_hash,
            "answer_hash": self.answer_hash,
            "review_hash": self.review_hash,
            "policy_hash": self.policy_hash,
            "subject_id": self.subject_id,
            "application_plan_id": self.application_plan_id,
            "bundle_canonical_hash": self.bundle_canonical_hash,
            "authorization_decision_id": self.authorization_decision_id,
            "authorization_decision_hash": self.authorization_decision_hash,
            "execution_record_id": self.execution_record_id,
            "execution_record_hash": self.execution_record_hash,
            "adapter_platform": self.adapter_platform,
            "action": self.action.value,
            "permit_policy_version": self.permit_policy_version,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "PlanScopedSubmissionPermitBindings":
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise ValueError("plan-scoped permit binding fields are invalid")
        return cls(**{key: str(value[key]) for key in expected})

    @property
    def digest(self) -> str:
        return hash_value(_canonical_json(self.to_dict()))

    @property
    def legacy_stable_digest(self) -> str:
        return PermitBindings(
            run_id=self.run_id,
            job_id=self.job_id,
            job_url_hash=self.job_url_hash,
            material_hash=self.material_hash,
            answer_hash=self.answer_hash,
            review_hash=self.review_hash,
            policy_hash=self.policy_hash,
        ).stable_digest


PermitBindingContract = PermitBindings | PlanScopedSubmissionPermitBindings


@dataclass(frozen=True, slots=True)
class PermitSignerMetadata:
    key_id: str
    algorithm: str
    provider_version: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.key_id,
                self.algorithm,
                self.provider_version,
            )
        ):
            raise ValueError("permit signer metadata is invalid")
        if self.algorithm != "HMAC-SHA256":
            raise ValueError("permit signer algorithm is unsupported")

    def to_dict(self) -> dict[str, str]:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "provider_version": self.provider_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PermitSignerMetadata":
        expected = {"algorithm", "key_id", "provider_version"}
        if set(value) != expected:
            raise ValueError("permit signer metadata fields are invalid")
        return cls(**{key: str(value[key]) for key in expected})


@dataclass(frozen=True, slots=True)
class GateAConsumptionReference:
    contract_version: str
    permit_schema_version: int
    permit_jti: str
    run_id: str
    job_id: str
    bindings_digest: str
    claims_hash: str
    consumed_at: str
    consumer: str
    action: str
    reference_hash: str

    def __post_init__(self) -> None:
        if self.contract_version != GATE_A_CONSUMPTION_REFERENCE_VERSION:
            raise ValueError("Gate A consumption reference is unsupported")
        if self.permit_schema_version != PERMIT_SCHEMA_VERSION:
            raise ValueError("Gate A permit schema is unsupported")
        for name in (
            "permit_jti",
            "run_id",
            "job_id",
            "bindings_digest",
            "claims_hash",
            "consumed_at",
            "consumer",
            "action",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(
                self, name
            ).strip():
                raise ValueError(f"{name} is required")
        if self.reference_hash != hash_value(
            _canonical_json(self.identity_dict())
        ):
            raise ValueError("Gate A consumption reference hash is invalid")

    def identity_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "bindings_digest": self.bindings_digest,
            "claims_hash": self.claims_hash,
            "consumed_at": self.consumed_at,
            "consumer": self.consumer,
            "contract_version": self.contract_version,
            "job_id": self.job_id,
            "permit_jti": self.permit_jti,
            "permit_schema_version": self.permit_schema_version,
            "run_id": self.run_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "reference_hash": self.reference_hash}

    @classmethod
    def create(
        cls,
        *,
        permit_jti: str,
        run_id: str,
        job_id: str,
        bindings_digest: str,
        claims_hash: str,
        consumed_at: str,
        consumer: str,
        action: str,
    ) -> "GateAConsumptionReference":
        values = {
            "action": action,
            "bindings_digest": bindings_digest,
            "claims_hash": claims_hash,
            "consumed_at": consumed_at,
            "consumer": consumer,
            "contract_version": GATE_A_CONSUMPTION_REFERENCE_VERSION,
            "job_id": job_id,
            "permit_jti": permit_jti,
            "permit_schema_version": PERMIT_SCHEMA_VERSION,
            "run_id": run_id,
        }
        return cls(
            reference_hash=hash_value(_canonical_json(values)), **values
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GateAConsumptionReference":
        expected = {
            "action",
            "bindings_digest",
            "claims_hash",
            "consumed_at",
            "consumer",
            "contract_version",
            "job_id",
            "permit_jti",
            "permit_schema_version",
            "reference_hash",
            "run_id",
        }
        if set(value) != expected:
            raise ValueError("Gate A consumption reference fields are invalid")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class SubmissionPermitConsumptionReference:
    contract_version: str
    permit_schema_version: int
    permit_jti: str
    prior_gate_jti: str
    run_id: str
    job_id: str
    bindings_digest: str
    claims_hash: str
    consumed_at: str
    consumer: str
    action: SubmissionPermitAction
    reference_hash: str

    def __post_init__(self) -> None:
        if (
            self.contract_version
            != SUBMISSION_PERMIT_CONSUMPTION_REFERENCE_VERSION
        ):
            raise ValueError(
                "submission permit consumption reference is unsupported"
            )
        if self.permit_schema_version != PERMIT_SCHEMA_VERSION:
            raise ValueError("submission permit schema is unsupported")
        for name in (
            "permit_jti",
            "prior_gate_jti",
            "run_id",
            "job_id",
            "bindings_digest",
            "claims_hash",
            "consumed_at",
            "consumer",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(
                self, name
            ).strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(
            self, "action", SubmissionPermitAction(self.action)
        )
        if self.reference_hash != hash_value(
            _canonical_json(self.identity_dict())
        ):
            raise ValueError(
                "submission permit consumption reference hash is invalid"
            )

    def identity_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "bindings_digest": self.bindings_digest,
            "claims_hash": self.claims_hash,
            "consumed_at": self.consumed_at,
            "consumer": self.consumer,
            "contract_version": self.contract_version,
            "job_id": self.job_id,
            "permit_jti": self.permit_jti,
            "permit_schema_version": self.permit_schema_version,
            "prior_gate_jti": self.prior_gate_jti,
            "run_id": self.run_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.identity_dict(), "reference_hash": self.reference_hash}

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any]
    ) -> "SubmissionPermitConsumptionReference":
        expected = {
            "action",
            "bindings_digest",
            "claims_hash",
            "consumed_at",
            "consumer",
            "contract_version",
            "job_id",
            "permit_jti",
            "permit_schema_version",
            "prior_gate_jti",
            "reference_hash",
            "run_id",
        }
        if set(value) != expected:
            raise ValueError(
                "submission permit consumption reference fields are invalid"
            )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class OpaquePermitTokenReference:
    contract_version: str
    reference_id: str
    subject_id: str
    token_hash: str
    store_service: str
    reference_hash: str

    def __post_init__(self) -> None:
        if self.contract_version != PERMIT_TOKEN_REFERENCE_VERSION:
            raise ValueError("permit token reference is unsupported")
        for name in ("reference_id", "subject_id", "token_hash", "store_service"):
            if not isinstance(getattr(self, name), str) or not getattr(
                self, name
            ).strip():
                raise ValueError(f"{name} is required")
        if self.store_service != PERMIT_TOKEN_SERVICE:
            raise ValueError("permit token store service is unsupported")
        if self.reference_hash != hash_value(
            _canonical_json(self.identity_dict())
        ):
            raise ValueError("permit token reference hash is invalid")

    def identity_dict(self) -> dict[str, str]:
        return {
            "contract_version": self.contract_version,
            "reference_id": self.reference_id,
            "store_service": self.store_service,
            "subject_id": self.subject_id,
            "token_hash": self.token_hash,
        }

    def to_dict(self) -> dict[str, str]:
        return {**self.identity_dict(), "reference_hash": self.reference_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OpaquePermitTokenReference":
        expected = {
            "contract_version",
            "reference_hash",
            "reference_id",
            "store_service",
            "subject_id",
            "token_hash",
        }
        if set(value) != expected:
            raise ValueError("permit token reference fields are invalid")
        return cls(**{key: str(value[key]) for key in expected})


class OpaquePermitTokenStore:
    """Keep bearer tokens in the existing credential store, never JSON."""

    def __init__(self, store: CredentialStore):
        self._store = store

    @staticmethod
    def _account(subject_id: str, reference_id: str) -> str:
        subject_key = hash_value(subject_id)
        return f"{subject_key}:{reference_id}"

    def save(
        self, *, subject_id: str, reference_id: str, token: str
    ) -> OpaquePermitTokenReference:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (subject_id, reference_id, token)
        ):
            raise ValueError("permit token reference inputs are required")
        token_hash = hash_value(token)
        account = self._account(subject_id, reference_id)
        existing = self._store.get(PERMIT_TOKEN_SERVICE, account)
        if existing is not None and not hmac.compare_digest(
            hash_value(existing), token_hash
        ):
            raise PermitBindingError("permit token reference conflicts")
        if existing is None:
            self._store.set(PERMIT_TOKEN_SERVICE, account, token)
        identity = {
            "contract_version": PERMIT_TOKEN_REFERENCE_VERSION,
            "reference_id": reference_id,
            "store_service": PERMIT_TOKEN_SERVICE,
            "subject_id": subject_id,
            "token_hash": token_hash,
        }
        return OpaquePermitTokenReference(
            reference_hash=hash_value(_canonical_json(identity)),
            **identity,
        )

    def load(
        self, *, subject_id: str, reference: OpaquePermitTokenReference
    ) -> str:
        if subject_id != reference.subject_id:
            raise PermitBindingError("permit token belongs to another subject")
        account = self._account(subject_id, reference.reference_id)
        token = self._store.get(PERMIT_TOKEN_SERVICE, account)
        if token is None:
            raise PermitBindingError("permit token reference was not found")
        if not hmac.compare_digest(hash_value(token), reference.token_hash):
            raise PermitBindingError("permit token hash mismatch")
        return token


@dataclass(frozen=True, slots=True)
class PermitClaims:
    version: int
    jti: str
    gate: PermitGate
    bindings: PermitBindingContract
    issued_at: int
    expires_at: int
    prior_gate_jti: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "jti": self.jti,
            "gate": self.gate.value,
            "bindings": self.bindings.to_dict(),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "prior_gate_jti": self.prior_gate_jti,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PermitClaims":
        raw_bindings = value["bindings"]
        if not isinstance(raw_bindings, Mapping):
            raise ValueError("permit bindings are invalid")
        if "contract_version" in raw_bindings:
            bindings: PermitBindingContract = (
                PlanScopedSubmissionPermitBindings.from_dict(raw_bindings)
            )
        else:
            bindings = PermitBindings.from_dict(raw_bindings)
        return cls(
            version=int(value["version"]),
            jti=str(value["jti"]),
            gate=PermitGate(value["gate"]),
            bindings=bindings,
            issued_at=int(value["issued_at"]),
            expires_at=int(value["expires_at"]),
            prior_gate_jti=value.get("prior_gate_jti"),
        )


class PermitService:
    """Issue and consume approvals without making the browser worker trusted."""

    def __init__(
        self,
        *,
        secret: bytes,
        ledger: EventLedger,
        clock: Callable[[], float] = time.time,
        signer_key_id: str | None = None,
        signer_provider_version: str = PERMIT_SIGNER_PROVIDER_VERSION,
    ):
        if len(secret) < 32:
            raise ValueError("permit HMAC secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self.ledger = ledger
        self.clock = clock
        self._signer_metadata = PermitSignerMetadata(
            key_id=signer_key_id
            or "foundation-permit:hmac-v1",
            algorithm="HMAC-SHA256",
            provider_version=signer_provider_version,
        )

    @property
    def signer_metadata(self) -> PermitSignerMetadata:
        return self._signer_metadata

    @staticmethod
    def generate_secret() -> bytes:
        """Generate a secret suitable for storage in the platform Keychain."""

        return secrets.token_bytes(32)

    def _issue(
        self,
        *,
        gate: PermitGate,
        bindings: PermitBindingContract,
        ttl_seconds: int,
        prior_gate_jti: str | None = None,
    ) -> str:
        return self._issue_at(
            gate=gate,
            bindings=bindings,
            ttl_seconds=ttl_seconds,
            issued_at=int(self.clock()),
            prior_gate_jti=prior_gate_jti,
        )

    def _issue_at(
        self,
        *,
        gate: PermitGate,
        bindings: PermitBindingContract,
        ttl_seconds: int,
        issued_at: int,
        prior_gate_jti: str | None = None,
    ) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if type(issued_at) is not int or issued_at < 0:
            raise ValueError("issued_at must be a non-negative integer")
        claims = PermitClaims(
            version=PERMIT_SCHEMA_VERSION,
            jti=str(uuid.uuid4()),
            gate=gate,
            bindings=bindings,
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
            prior_gate_jti=prior_gate_jti,
        )
        payload = _canonical_json(claims.to_dict())
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"

    def issue_gate_a(
        self, bindings: PermitBindings, *, ttl_seconds: int = 1800
    ) -> str:
        return self._issue(
            gate=PermitGate.GATE_A,
            bindings=bindings,
            ttl_seconds=ttl_seconds,
        )

    def issue_gate_b(
        self,
        bindings: PermitBindings,
        *,
        gate_a_jti: str,
        ttl_seconds: int = 300,
    ) -> str:
        consumption = self.ledger.get_permit_consumption(gate_a_jti)
        if consumption is None or consumption["gate"] != PermitGate.GATE_A.value:
            raise PermitPrerequisiteError("Gate A must be consumed before Gate B")
        prior = PermitClaims.from_dict(consumption["claims"])
        if not hmac.compare_digest(
            prior.bindings.stable_digest, bindings.stable_digest
        ):
            raise PermitBindingError("Gate B inputs differ from approved Gate A inputs")
        return self._issue(
            gate=PermitGate.GATE_B,
            bindings=bindings,
            ttl_seconds=ttl_seconds,
            prior_gate_jti=gate_a_jti,
        )

    def gate_a_consumption_reference(
        self,
        *,
        permit_jti: str,
        consumer: str,
        action: str,
    ) -> GateAConsumptionReference:
        consumption = self.ledger.get_permit_consumption(permit_jti)
        if consumption is None or consumption["gate"] != PermitGate.GATE_A.value:
            raise PermitPrerequisiteError("consumed Gate A permit was not found")
        claims = PermitClaims.from_dict(consumption["claims"])
        return GateAConsumptionReference.create(
            permit_jti=str(consumption["jti"]),
            run_id=str(consumption["run_id"]),
            job_id=str(consumption["job_id"]),
            bindings_digest=str(consumption["bindings_digest"]),
            claims_hash=hash_value(_canonical_json(claims.to_dict())),
            consumed_at=str(consumption["consumed_at"]),
            consumer=str(consumer),
            action=str(action),
        )

    def verify_gate_a_consumption_reference(
        self, reference: GateAConsumptionReference
    ) -> PermitClaims:
        expected = self.gate_a_consumption_reference(
            permit_jti=reference.permit_jti,
            consumer=reference.consumer,
            action=reference.action,
        )
        if not hmac.compare_digest(
            expected.reference_hash, reference.reference_hash
        ):
            raise PermitBindingError("Gate A consumption reference mismatch")
        consumption = self.ledger.get_permit_consumption(reference.permit_jti)
        assert consumption is not None
        return PermitClaims.from_dict(consumption["claims"])

    def submission_permit_consumption_reference(
        self,
        *,
        permit_jti: str,
        consumer: str,
    ) -> SubmissionPermitConsumptionReference:
        consumption = self.ledger.get_permit_consumption(permit_jti)
        if consumption is None or consumption["gate"] != PermitGate.GATE_B.value:
            raise PermitPrerequisiteError(
                "consumed submission permit was not found"
            )
        claims = PermitClaims.from_dict(consumption["claims"])
        if not isinstance(
            claims.bindings, PlanScopedSubmissionPermitBindings
        ):
            raise PermitBindingError(
                "consumed Gate B permit is not plan-scoped"
            )
        if claims.prior_gate_jti is None:
            raise PermitBindingError(
                "submission permit is missing Gate A lineage"
            )
        values = {
            "action": claims.bindings.action.value,
            "bindings_digest": str(consumption["bindings_digest"]),
            "claims_hash": hash_value(_canonical_json(claims.to_dict())),
            "consumed_at": str(consumption["consumed_at"]),
            "consumer": str(consumer),
            "contract_version": (
                SUBMISSION_PERMIT_CONSUMPTION_REFERENCE_VERSION
            ),
            "job_id": str(consumption["job_id"]),
            "permit_jti": str(consumption["jti"]),
            "permit_schema_version": PERMIT_SCHEMA_VERSION,
            "prior_gate_jti": claims.prior_gate_jti,
            "run_id": str(consumption["run_id"]),
        }
        return SubmissionPermitConsumptionReference(
            reference_hash=hash_value(_canonical_json(values)),
            **values,
        )

    def verify_submission_permit_consumption_reference(
        self, reference: SubmissionPermitConsumptionReference
    ) -> PermitClaims:
        expected = self.submission_permit_consumption_reference(
            permit_jti=reference.permit_jti,
            consumer=reference.consumer,
        )
        if not hmac.compare_digest(
            expected.reference_hash, reference.reference_hash
        ):
            raise PermitBindingError(
                "submission permit consumption reference mismatch"
            )
        consumption = self.ledger.get_permit_consumption(reference.permit_jti)
        assert consumption is not None
        return PermitClaims.from_dict(consumption["claims"])

    def validate_plan_scoped_submission_bindings(
        self,
        bindings: PlanScopedSubmissionPermitBindings,
        *,
        expected_bindings: PlanScopedSubmissionPermitBindings,
        gate_a_reference: GateAConsumptionReference,
    ) -> PermitClaims:
        if not hmac.compare_digest(
            bindings.digest, expected_bindings.digest
        ):
            raise PermitBindingError(
                "plan-scoped submission permit inputs differ"
            )
        gate_a_claims = self.verify_gate_a_consumption_reference(
            gate_a_reference
        )
        if not isinstance(gate_a_claims.bindings, PermitBindings):
            raise PermitBindingError("Gate A must use legacy bundle bindings")
        if not hmac.compare_digest(
            gate_a_claims.bindings.stable_digest,
            bindings.legacy_stable_digest,
        ):
            raise PermitBindingError(
                "plan-scoped permit differs from consumed Gate A inputs"
            )
        return gate_a_claims

    def issue_plan_scoped_submission_permit(
        self,
        bindings: PlanScopedSubmissionPermitBindings,
        *,
        expected_bindings: PlanScopedSubmissionPermitBindings,
        gate_a_reference: GateAConsumptionReference,
        issued_at: int,
        ttl_seconds: int,
    ) -> str:
        """Sign one plan-scoped Gate B permit with the existing HMAC protocol."""

        self.validate_plan_scoped_submission_bindings(
            bindings,
            expected_bindings=expected_bindings,
            gate_a_reference=gate_a_reference,
        )
        return self._issue_at(
            gate=PermitGate.GATE_B,
            bindings=bindings,
            ttl_seconds=ttl_seconds,
            issued_at=issued_at,
            prior_gate_jti=gate_a_reference.permit_jti,
        )

    def verify(
        self,
        token: str,
        *,
        expected_gate: PermitGate | None = None,
        expected_bindings: PermitBindingContract | None = None,
    ) -> PermitClaims:
        return self.verify_at(
            token,
            now=int(self.clock()),
            expected_gate=expected_gate,
            expected_bindings=expected_bindings,
        )

    def verify_at(
        self,
        token: str,
        *,
        now: int,
        expected_gate: PermitGate | None = None,
        expected_bindings: PermitBindingContract | None = None,
    ) -> PermitClaims:
        if type(now) is not int or now < 0:
            raise ValueError("now must be a non-negative integer")
        parts = token.split(".")
        if len(parts) != 2:
            raise PermitSignatureError("invalid permit format")
        payload = _b64url_decode(parts[0])
        supplied_signature = _b64url_decode(parts[1])
        expected_signature = hmac.new(
            self._secret, payload, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise PermitSignatureError("permit signature mismatch")
        try:
            claims = PermitClaims.from_dict(json.loads(payload))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PermitSignatureError("invalid permit claims") from exc
        if claims.version != PERMIT_SCHEMA_VERSION:
            raise PermitSignatureError(
                f"unsupported permit schema version {claims.version}"
            )
        if now >= claims.expires_at:
            raise PermitExpiredError(claims.jti)
        if claims.issued_at > now + 30:
            raise PermitSignatureError("permit issued in the future")
        if expected_gate is not None and claims.gate is not PermitGate(expected_gate):
            raise PermitGateError(
                f"expected {PermitGate(expected_gate).value}, got {claims.gate.value}"
            )
        if expected_bindings is not None and not hmac.compare_digest(
            claims.bindings.digest, expected_bindings.digest
        ):
            raise PermitBindingError("permit does not match current application inputs")
        return claims

    def consume(
        self,
        token: str,
        *,
        expected_gate: PermitGate,
        expected_bindings: PermitBindingContract,
    ) -> PermitClaims:
        claims = self.verify(
            token,
            expected_gate=expected_gate,
            expected_bindings=expected_bindings,
        )
        token_digest = hash_value(token)
        try:
            self.ledger.consume_permit(
                jti=claims.jti,
                gate=claims.gate.value,
                run_id=claims.bindings.run_id,
                job_id=claims.bindings.job_id,
                token_digest=token_digest,
                bindings_digest=claims.bindings.digest,
                claims=claims.to_dict(),
            )
        except PermitAlreadyConsumedError as exc:
            raise PermitConsumedError(claims.jti) from exc
        return claims


__all__ = [
    "GATE_A_CONSUMPTION_REFERENCE_VERSION",
    "PERMIT_SCHEMA_VERSION",
    "PERMIT_SIGNER_PROVIDER_VERSION",
    "PERMIT_TOKEN_REFERENCE_VERSION",
    "PERMIT_TOKEN_SERVICE",
    "PLAN_SCOPED_SUBMISSION_BINDING_VERSION",
    "SUBMISSION_PERMIT_CONSUMPTION_REFERENCE_VERSION",
    "GateAConsumptionReference",
    "OpaquePermitTokenReference",
    "OpaquePermitTokenStore",
    "PermitBindingError",
    "PermitBindingContract",
    "PermitBindings",
    "PermitClaims",
    "PermitConsumedError",
    "PermitError",
    "PermitExpiredError",
    "PermitGate",
    "PermitGateError",
    "PermitIssuerUnavailableError",
    "PermitPrerequisiteError",
    "PermitService",
    "PermitSignerMetadata",
    "PermitSignatureError",
    "PlanScopedSubmissionPermitBindings",
    "SubmissionPermitAction",
    "SubmissionPermitConsumptionReference",
    "hash_value",
]
