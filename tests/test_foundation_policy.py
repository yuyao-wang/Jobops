import pytest

from core.policy import (
    ApprovalActor,
    AutonomyMode,
    CoverLetterStrategy,
    JobTier,
    MaterialStrategy,
    PolicyBlocker,
    PolicyConfig,
    PolicyEngine,
    RiskSignals,
    SubmitAuthority,
    VerificationAuthority,
)


@pytest.mark.parametrize(
    ("tier", "material", "cover"),
    [
        (JobTier.HIGH, MaterialStrategy.BESPOKE, CoverLetterStrategy.NARRATIVE),
        (JobTier.MEDIUM, MaterialStrategy.TARGETED, CoverLetterStrategy.TARGETED),
        (JobTier.LOW, MaterialStrategy.ROUTE_EXISTING, CoverLetterStrategy.IF_REQUIRED),
    ],
)
def test_job_tiers_select_different_material_quality(tier, material, cover) -> None:
    decision = PolicyEngine(PolicyConfig()).decide(tier, RiskSignals())
    assert decision.material_strategy is material
    assert decision.cover_letter_strategy is cover


def test_supervised_low_risk_and_full_autopilot_authority() -> None:
    supervised = PolicyEngine(
        PolicyConfig(mode=AutonomyMode.SUPERVISED)
    ).decide(JobTier.LOW, RiskSignals())
    assert supervised.gate_a_actor is ApprovalActor.HUMAN
    assert supervised.submit_authority is SubmitAuthority.HUMAN_WITH_PERMIT

    low = PolicyEngine(
        PolicyConfig(mode=AutonomyMode.LOW_RISK_AUTOPILOT)
    ).decide(JobTier.LOW, RiskSignals())
    medium = PolicyEngine(
        PolicyConfig(mode=AutonomyMode.LOW_RISK_AUTOPILOT)
    ).decide(JobTier.MEDIUM, RiskSignals())
    high = PolicyEngine(
        PolicyConfig(mode=AutonomyMode.LOW_RISK_AUTOPILOT)
    ).decide(JobTier.HIGH, RiskSignals())
    assert low.submit_authority is SubmitAuthority.CODEX_WITH_PERMIT
    assert medium.gate_a_actor is ApprovalActor.CODEX
    assert medium.gate_b_actor is ApprovalActor.HUMAN
    assert high.gate_a_actor is ApprovalActor.HUMAN

    full = PolicyEngine(
        PolicyConfig(mode=AutonomyMode.FULL_AUTOPILOT)
    ).decide(JobTier.HIGH, RiskSignals())
    assert full.gate_a_actor is ApprovalActor.CODEX
    assert full.submit_authority is SubmitAuthority.CODEX_WITH_PERMIT


def test_full_autopilot_never_invents_or_bypasses_hard_blockers() -> None:
    engine = PolicyEngine(PolicyConfig(mode=AutonomyMode.FULL_AUTOPILOT))
    decision = engine.decide(
        JobTier.LOW,
        RiskSignals(unknown_required_question=True, sensitive_required_question=True),
    )
    assert decision.submit_authority is SubmitAuthority.BLOCKED
    assert PolicyBlocker.UNKNOWN_REQUIRED_QUESTION in decision.blockers
    assert PolicyBlocker.SENSITIVE_ANSWER_REQUIRED in decision.blockers


def test_optional_email_agent_falls_back_to_human_policy() -> None:
    required = RiskSignals(email_verification_required=True)
    disabled = PolicyEngine(
        PolicyConfig(
            mode=AutonomyMode.FULL_AUTOPILOT,
            email_verification_agent_enabled=False,
        )
    ).decide(JobTier.LOW, required)
    assert PolicyBlocker.EMAIL_VERIFICATION in disabled.blockers
    assert disabled.email_verification_authority is VerificationAuthority.HUMAN

    enabled = PolicyEngine(
        PolicyConfig(
            mode=AutonomyMode.FULL_AUTOPILOT,
            email_verification_agent_enabled=True,
        )
    ).decide(JobTier.LOW, required)
    assert enabled.may_continue
    assert (
        enabled.email_verification_authority
        is VerificationAuthority.CODEX_THEN_HUMAN
    )


def test_keychain_login_is_allowed_but_captcha_still_blocks() -> None:
    engine = PolicyEngine(PolicyConfig(mode=AutonomyMode.FULL_AUTOPILOT))
    login = engine.decide(
        JobTier.LOW,
        RiskSignals(login_required=True, credentials_available=True),
    )
    assert login.may_continue

    captcha = engine.decide(JobTier.LOW, RiskSignals(captcha=True))
    assert captcha.submit_authority is SubmitAuthority.BLOCKED
    assert PolicyBlocker.CAPTCHA in captcha.blockers
