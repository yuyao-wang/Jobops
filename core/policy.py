"""Autonomy and quality policy for differently ranked applications.

Policy decides *who* may approve each gate.  It never relaxes the invariant that
unverified or sensitive facts cannot be invented by an agent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class AutonomyMode(StrEnum):
    SUPERVISED = "SUPERVISED"
    LOW_RISK_AUTOPILOT = "LOW_RISK_AUTOPILOT"
    FULL_AUTOPILOT = "FULL_AUTOPILOT"


class JobTier(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class MaterialStrategy(StrEnum):
    BESPOKE = "BESPOKE"
    TARGETED = "TARGETED"
    ROUTE_EXISTING = "ROUTE_EXISTING"


class CoverLetterStrategy(StrEnum):
    NARRATIVE = "NARRATIVE"
    TARGETED = "TARGETED"
    IF_REQUIRED = "IF_REQUIRED"


class ApprovalActor(StrEnum):
    HUMAN = "HUMAN"
    CODEX = "CODEX"


class SubmitAuthority(StrEnum):
    HUMAN_WITH_PERMIT = "HUMAN_WITH_PERMIT"
    CODEX_WITH_PERMIT = "CODEX_WITH_PERMIT"
    BLOCKED = "BLOCKED"


class AnswerAuthority(StrEnum):
    VERIFIED_FACTS_ONLY = "VERIFIED_FACTS_ONLY"


class VerificationAuthority(StrEnum):
    HUMAN = "HUMAN"
    CODEX_THEN_HUMAN = "CODEX_THEN_HUMAN"
    NOT_REQUIRED = "NOT_REQUIRED"


class PolicyBlocker(StrEnum):
    UNVERIFIED_RESUME = "UNVERIFIED_RESUME"
    UNVERIFIED_ANSWERS = "UNVERIFIED_ANSWERS"
    UNKNOWN_REQUIRED_QUESTION = "UNKNOWN_REQUIRED_QUESTION"
    SENSITIVE_ANSWER_REQUIRED = "SENSITIVE_ANSWER_REQUIRED"
    MISSING_MATERIAL = "MISSING_MATERIAL"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"
    TWO_FACTOR_AUTH = "TWO_FACTOR_AUTH"
    CAPTCHA = "CAPTCHA"
    ANTI_BOT = "ANTI_BOT"
    EMAIL_VERIFICATION = "EMAIL_VERIFICATION"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PAYMENT_OR_PERMISSION = "PAYMENT_OR_PERMISSION"


@dataclass(frozen=True, slots=True)
class RiskSignals:
    resume_verified: bool = True
    answers_verified: bool = True
    unknown_required_question: bool = False
    sensitive_required_question: bool = False
    missing_material: bool = False
    login_required: bool = False
    credentials_available: bool = False
    registration_required: bool = False
    two_factor_auth: bool = False
    captcha: bool = False
    anti_bot: bool = False
    email_verification_required: bool = False
    account_locked: bool = False
    payment_or_permission: bool = False


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    mode: AutonomyMode = AutonomyMode.SUPERVISED
    email_verification_agent_enabled: bool = False
    allow_keychain_login: bool = True
    allow_account_registration: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", AutonomyMode(self.mode))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "email_verification_agent_enabled": self.email_verification_agent_enabled,
            "allow_keychain_login": self.allow_keychain_login,
            "allow_account_registration": self.allow_account_registration,
        }


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    mode: AutonomyMode
    tier: JobTier
    material_strategy: MaterialStrategy
    cover_letter_strategy: CoverLetterStrategy
    answer_authority: AnswerAuthority
    gate_a_actor: ApprovalActor
    gate_b_actor: ApprovalActor
    submit_authority: SubmitAuthority
    email_verification_authority: VerificationAuthority
    blockers: tuple[PolicyBlocker, ...]
    policy_hash: str

    @property
    def may_continue(self) -> bool:
        return not self.blockers

    @property
    def is_autonomous_submission(self) -> bool:
        return self.submit_authority is SubmitAuthority.CODEX_WITH_PERMIT


def _hash_policy(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class PolicyEngine:
    def __init__(self, config: PolicyConfig):
        self.config = config

    def _blockers(self, signals: RiskSignals) -> tuple[PolicyBlocker, ...]:
        blockers: list[PolicyBlocker] = []
        if not signals.resume_verified:
            blockers.append(PolicyBlocker.UNVERIFIED_RESUME)
        if not signals.answers_verified:
            blockers.append(PolicyBlocker.UNVERIFIED_ANSWERS)
        if signals.unknown_required_question:
            blockers.append(PolicyBlocker.UNKNOWN_REQUIRED_QUESTION)
        if signals.sensitive_required_question:
            blockers.append(PolicyBlocker.SENSITIVE_ANSWER_REQUIRED)
        if signals.missing_material:
            blockers.append(PolicyBlocker.MISSING_MATERIAL)
        if signals.login_required and not (
            self.config.allow_keychain_login and signals.credentials_available
        ):
            blockers.append(PolicyBlocker.LOGIN_REQUIRED)
        if signals.registration_required and not self.config.allow_account_registration:
            blockers.append(PolicyBlocker.REGISTRATION_REQUIRED)
        if signals.two_factor_auth:
            blockers.append(PolicyBlocker.TWO_FACTOR_AUTH)
        if signals.captcha:
            blockers.append(PolicyBlocker.CAPTCHA)
        if signals.anti_bot:
            blockers.append(PolicyBlocker.ANTI_BOT)
        if signals.email_verification_required and not (
            self.config.email_verification_agent_enabled
        ):
            blockers.append(PolicyBlocker.EMAIL_VERIFICATION)
        if signals.account_locked:
            blockers.append(PolicyBlocker.ACCOUNT_LOCKED)
        if signals.payment_or_permission:
            blockers.append(PolicyBlocker.PAYMENT_OR_PERMISSION)
        return tuple(blockers)

    @staticmethod
    def _materials(
        tier: JobTier,
    ) -> tuple[MaterialStrategy, CoverLetterStrategy]:
        if tier is JobTier.HIGH:
            return MaterialStrategy.BESPOKE, CoverLetterStrategy.NARRATIVE
        if tier is JobTier.MEDIUM:
            return MaterialStrategy.TARGETED, CoverLetterStrategy.TARGETED
        return MaterialStrategy.ROUTE_EXISTING, CoverLetterStrategy.IF_REQUIRED

    def _approval_actors(
        self, tier: JobTier
    ) -> tuple[ApprovalActor, ApprovalActor]:
        if self.config.mode is AutonomyMode.SUPERVISED:
            return ApprovalActor.HUMAN, ApprovalActor.HUMAN
        if self.config.mode is AutonomyMode.FULL_AUTOPILOT:
            return ApprovalActor.CODEX, ApprovalActor.CODEX
        if tier is JobTier.LOW:
            return ApprovalActor.CODEX, ApprovalActor.CODEX
        if tier is JobTier.MEDIUM:
            return ApprovalActor.CODEX, ApprovalActor.HUMAN
        return ApprovalActor.HUMAN, ApprovalActor.HUMAN

    def decide(self, tier: JobTier, signals: RiskSignals) -> PolicyDecision:
        selected_tier = JobTier(tier)
        blockers = self._blockers(signals)
        material_strategy, cover_letter_strategy = self._materials(selected_tier)
        gate_a_actor, gate_b_actor = self._approval_actors(selected_tier)
        if blockers:
            submit_authority = SubmitAuthority.BLOCKED
        elif gate_b_actor is ApprovalActor.CODEX:
            submit_authority = SubmitAuthority.CODEX_WITH_PERMIT
        else:
            submit_authority = SubmitAuthority.HUMAN_WITH_PERMIT

        if not signals.email_verification_required:
            email_authority = VerificationAuthority.NOT_REQUIRED
        elif self.config.email_verification_agent_enabled:
            email_authority = VerificationAuthority.CODEX_THEN_HUMAN
        else:
            email_authority = VerificationAuthority.HUMAN

        hash_input = {
            "config": self.config.to_dict(),
            "tier": selected_tier.value,
            "material_strategy": material_strategy.value,
            "cover_letter_strategy": cover_letter_strategy.value,
            "answer_authority": AnswerAuthority.VERIFIED_FACTS_ONLY.value,
            "gate_a_actor": gate_a_actor.value,
            "gate_b_actor": gate_b_actor.value,
            "submit_authority": submit_authority.value,
            "email_verification_authority": email_authority.value,
            "blockers": [item.value for item in blockers],
        }
        return PolicyDecision(
            mode=self.config.mode,
            tier=selected_tier,
            material_strategy=material_strategy,
            cover_letter_strategy=cover_letter_strategy,
            answer_authority=AnswerAuthority.VERIFIED_FACTS_ONLY,
            gate_a_actor=gate_a_actor,
            gate_b_actor=gate_b_actor,
            submit_authority=submit_authority,
            email_verification_authority=email_authority,
            blockers=blockers,
            policy_hash=_hash_policy(hash_input),
        )


__all__ = [
    "AnswerAuthority",
    "ApprovalActor",
    "AutonomyMode",
    "CoverLetterStrategy",
    "JobTier",
    "MaterialStrategy",
    "PolicyBlocker",
    "PolicyConfig",
    "PolicyDecision",
    "PolicyEngine",
    "RiskSignals",
    "SubmitAuthority",
    "VerificationAuthority",
]
