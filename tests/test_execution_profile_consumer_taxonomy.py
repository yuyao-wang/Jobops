"""Focused P2c1d1a execution-profile consumer contract tests."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from adapters.generic_ai.resolver import (
    AnswerResolver,
    UnresolvedField,
)
from adapters.registry import AdapterRegistry, AdapterRunRequest
from adapters.workday import WorkdayRuntimeConfig, fill_workday_fields
from core.application_execution_profile import (
    APPLICATION_BUNDLE_CONSUMER_BINDINGS,
    APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS,
    ApplicationBundleConsumerInputClass,
    ApplicationExecutionIdentityProfile,
)
from core.bundles import (
    ApplicationBundle,
    JobSpec,
    MaterialBundle,
    application_bundle_canonical_hash,
)
from core.policy import (
    AutonomyMode,
    JobTier,
    PolicyConfig,
    PolicyEngine,
    RiskSignals,
)


def test_closed_identity_taxonomy_rejects_mixed_profile_namespaces() -> None:
    expected = {
        "address",
        "city",
        "country",
        "email",
        "first_name",
        "github",
        "last_name",
        "linkedin",
        "location",
        "phone",
        "portfolio",
        "postal_code",
        "preferred_name",
        "state",
    }
    assert {item.value for item in APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS} == expected
    profile = ApplicationExecutionIdentityProfile.from_application_bundle_profile(
        {"personal": {"email": "synthetic@example.test"}}
    )
    assert dict(profile.to_application_bundle_profile()) == {
        "personal": {"email": "synthetic@example.test"}
    }
    with pytest.raises(ValueError, match="only personal"):
        ApplicationExecutionIdentityProfile.from_application_bundle_profile(
            {
                "personal": {"email": "synthetic@example.test"},
                "common_answers": {"sponsorship": False},
            }
        )
    assert {item.consumer_id for item in APPLICATION_BUNDLE_CONSUMER_BINDINGS} == {
        "application_engine",
        "base_ats_adapter",
        "generic_ai_adapter",
        "workday_adapter",
    }
    root = Path(__file__).resolve().parents[1]
    production_sources = {
        name: (root / name).read_text(encoding="utf-8")
        for name in (
            "adapters/protocol.py",
            "adapters/registry.py",
            "adapters/generic_ai/adapter.py",
            "adapters/generic_ai/resolver.py",
            "core/application_engine.py",
        )
    }
    forbidden = (
        'profile.get("common_answers"',
        'profile.get("verified_question_answers"',
        'profile.get("resume_path"',
        'profile.get("workday"',
        "profile, \"documents.resume\"",
        "profile=bundle.profile",
    )
    assert not {
        f"{name}:{token}"
        for name, source in production_sources.items()
        for token in forbidden
        if token in source
    }


def test_generic_answers_are_separate_and_dynamic_unknown_stays_unresolved() -> None:
    resolver = AnswerResolver(
        {"personal": {"email": "synthetic@example.test"}},
        answers={"sponsorship": False},
    )
    assert resolver.value_for_key("email") == "synthetic@example.test"
    assert resolver.value_for_key("sponsorship") == "False"
    assert "common_answers" not in inspect.getsource(AnswerResolver)
    assert "verified_question_answers" not in inspect.getsource(AnswerResolver)

    from adapters.generic_ai.models import FormControl

    result = resolver.resolve(
        FormControl(
            index=0,
            role="textbox",
            tag="input",
            label="Describe one role-specific example",
            required=True,
            selector="#custom",
        )
    )
    assert isinstance(result, UnresolvedField)


@pytest.mark.asyncio
async def test_registry_passes_typed_workday_context_not_profile_overrides() -> None:
    class _Capture:
        def __init__(self) -> None:
            self.context = None

        async def run(self, context):
            self.context = context
            return context

    config = WorkdayRuntimeConfig(
        auto_login=False,
        auto_register=False,
        generated_password_length=32,
    )
    class _GenericCapture:
        def __init__(self) -> None:
            self.values = None

        async def run(self, **values):
            self.values = values
            return values

    generic = _GenericCapture()
    registry = AdapterRegistry(
        generic_adapter=generic,
        workday_runtime_config=config,
    )
    capture = _Capture()
    registry._specialized["workday"] = capture
    identity = ApplicationExecutionIdentityProfile(
        email="synthetic@example.test"
    )
    generic_result = await registry.run(
        AdapterRunRequest(
            page=object(),
            job_url="https://careers.example.test/apply",
            job_id="job-generic",
            run_id="run-generic",
            profile=identity,
            resume_path="managed-resume.pdf",
            answers={"city": "Synthetic City"},
        )
    )

    returned = await registry.run(
        AdapterRunRequest(
            page=object(),
            job_url=(
                "https://synthetic.wd5.myworkdayjobs.com/"
                "Synthetic/job/example"
            ),
            job_id="job-synthetic",
            run_id="run-synthetic",
            company="Synthetic Company",
            profile=identity,
            resume_path="managed-resume.pdf",
            answers={"city": "Synthetic City"},
        )
    )

    assert generic_result is generic.values
    assert generic.values["profile"] is identity
    assert generic.values["answers"] == {"city": "Synthetic City"}
    assert generic.values["resume_path"] == "managed-resume.pdf"
    assert returned is capture.context
    assert capture.context.profile is identity
    assert capture.context.answers == {"city": "Synthetic City"}
    assert capture.context.resume_path == "managed-resume.pdf"
    assert capture.context.company == "Synthetic Company"
    assert capture.context.runtime_config is config
    assert "context.profile.get" not in inspect.getsource(fill_workday_fields)


def test_historical_mixed_profile_identity_and_hash_remain_untouched() -> None:
    legacy = {
        "personal": {"email": "synthetic@example.test"},
        "common_answers": {"legacy": "preserved"},
        "resume_path": "historical-managed-resume.pdf",
    }
    bundle = ApplicationBundle(
        run_id="run-history",
        job=JobSpec(
            url="https://jobs.example.test/one",
            company="Synthetic Company",
            title="Synthetic Role",
            tier=JobTier.LOW,
            job_id="job-history",
        ),
        materials=MaterialBundle(
            resume_path=Path("historical.pdf"),
            resume_sha256="1" * 64,
        ),
        profile=legacy,
        answers={},
        policy=PolicyEngine(
            PolicyConfig(mode=AutonomyMode.SUPERVISED)
        ).decide(JobTier.LOW, RiskSignals()),
    )
    before = application_bundle_canonical_hash(bundle)

    with pytest.raises(ValueError, match="only personal"):
        _ = bundle.identity_profile

    assert bundle.profile == legacy
    assert application_bundle_canonical_hash(bundle) == before
    assert (
        ApplicationExecutionIdentityProfile.from_legacy_profile(
            {"email": "legacy-alias@example.test"}
        ).email
        == "legacy-alias@example.test"
    )
    assert (
        ApplicationBundleConsumerInputClass.EXECUTION_POLICY
        not in next(
            item
            for item in APPLICATION_BUNDLE_CONSUMER_BINDINGS
            if item.consumer_id == "workday_adapter"
        ).input_classes
    )
