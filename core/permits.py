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

from .event_ledger import EventLedger, PermitAlreadyConsumedError


PERMIT_SCHEMA_VERSION = 1


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


class PermitGate(StrEnum):
    GATE_A = "GATE_A"
    GATE_B = "GATE_B"


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
class PermitClaims:
    version: int
    jti: str
    gate: PermitGate
    bindings: PermitBindings
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
        return cls(
            version=int(value["version"]),
            jti=str(value["jti"]),
            gate=PermitGate(value["gate"]),
            bindings=PermitBindings.from_dict(value["bindings"]),
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
    ):
        if len(secret) < 32:
            raise ValueError("permit HMAC secret must contain at least 32 bytes")
        self._secret = bytes(secret)
        self.ledger = ledger
        self.clock = clock

    @staticmethod
    def generate_secret() -> bytes:
        """Generate a secret suitable for storage in the platform Keychain."""

        return secrets.token_bytes(32)

    def _issue(
        self,
        *,
        gate: PermitGate,
        bindings: PermitBindings,
        ttl_seconds: int,
        prior_gate_jti: str | None = None,
    ) -> str:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        issued_at = int(self.clock())
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

    def verify(
        self,
        token: str,
        *,
        expected_gate: PermitGate | None = None,
        expected_bindings: PermitBindings | None = None,
    ) -> PermitClaims:
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
        now = int(self.clock())
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
        expected_bindings: PermitBindings,
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
    "PERMIT_SCHEMA_VERSION",
    "PermitBindingError",
    "PermitBindings",
    "PermitClaims",
    "PermitConsumedError",
    "PermitError",
    "PermitExpiredError",
    "PermitGate",
    "PermitGateError",
    "PermitPrerequisiteError",
    "PermitService",
    "PermitSignatureError",
    "hash_value",
]
