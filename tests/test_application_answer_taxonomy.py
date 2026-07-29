from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import get_type_hints

import pytest

import core.application_answer_taxonomy as taxonomy_module
from adapters.generic_ai.semantic_mapper import (
    MappingResponse,
)
from adapters.protocol import FieldIR, FieldKind
from adapters.shared import FieldSpec, canonical_key_for
from core.application_answer_taxonomy import (
    CANONICAL_APPLICATION_ANSWER_TAXONOMY,
    CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION,
    CanonicalAnswerAutomationCategory,
    CanonicalAnswerSensitivity,
    CanonicalAnswerValueType,
    CanonicalApplicationAnswerKey,
    CanonicalApplicationAnswers,
    canonical_application_answer_definition,
    canonical_application_answer_taxonomy_dict,
    canonical_application_answer_taxonomy_hash,
    normalize_canonical_application_answer_key,
)
from core.bundles import (
    ApplicationBundle,
    JobSpec,
    MaterialBundle,
    canonical_hash,
)
from core.policy import (
    AutonomyMode,
    JobTier,
    PolicyConfig,
    PolicyEngine,
    RiskSignals,
)


EXPECTED_TAXONOMY_HASH = (
    "bc002c0a9d63dc2863c786797868d061803f77d88eebfffcafe4751cee46a079"
)


def _bundle(tmp_path: Path, answers) -> ApplicationBundle:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"synthetic resume")
    return ApplicationBundle(
        run_id="run-taxonomy",
        job=JobSpec(
            url="https://example.test/jobs/1",
            company="Synthetic",
            title="Tester",
            tier=JobTier.LOW,
        ),
        materials=MaterialBundle.build(resume_path=resume),
        profile={},
        answers=answers,
        policy=PolicyEngine(
            PolicyConfig(mode=AutonomyMode.LOW_RISK_AUTOPILOT)
        ).decide(JobTier.LOW, RiskSignals()),
    )


def test_registry_is_complete_typed_and_versioned() -> None:
    assert set(CANONICAL_APPLICATION_ANSWER_TAXONOMY) == set(
        CanonicalApplicationAnswerKey
    )
    assert all(
        definition.taxonomy_version
        == CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
        and isinstance(definition.value_type, CanonicalAnswerValueType)
        and isinstance(
            definition.sensitivity, CanonicalAnswerSensitivity
        )
        and isinstance(
            definition.automation_category,
            CanonicalAnswerAutomationCategory,
        )
        for definition in CANONICAL_APPLICATION_ANSWER_TAXONOMY.values()
    )
    with pytest.raises(TypeError):
        CANONICAL_APPLICATION_ANSWER_TAXONOMY[
            CanonicalApplicationAnswerKey.EMAIL
        ] = canonical_application_answer_definition("email")


def test_contact_legal_demographic_and_attestation_metadata_differ() -> None:
    email = canonical_application_answer_definition("email")
    authorization = canonical_application_answer_definition(
        "work_authorization"
    )
    sponsorship = canonical_application_answer_definition("sponsorship")
    demographic = canonical_application_answer_definition("gender")
    attestation = canonical_application_answer_definition("attestation")

    assert email.value_type is CanonicalAnswerValueType.TEXT
    assert email.sensitivity is CanonicalAnswerSensitivity.BASIC
    assert authorization.value_type is CanonicalAnswerValueType.BOOLEAN
    assert sponsorship.key is CanonicalApplicationAnswerKey.SPONSORSHIP
    assert authorization.key is not sponsorship.key
    assert demographic.automation_category is (
        CanonicalAnswerAutomationCategory.VOLUNTARY_DEMOGRAPHIC
    )
    assert attestation.value_type is CanonicalAnswerValueType.ATTESTATION
    assert attestation.automation_category is (
        CanonicalAnswerAutomationCategory.REQUIRES_ATTESTATION
    )


def test_phone_alias_normalizes_at_explicit_boundaries() -> None:
    assert normalize_canonical_application_answer_key(
        "phone_number", allow_legacy_alias=True
    ) is CanonicalApplicationAnswerKey.PHONE
    with pytest.raises(ValueError):
        normalize_canonical_application_answer_key("phone_number")

    field = FieldIR(
        canonical_key="phone_number",
        label="Phone",
        selectors=("#phone",),
        kind=FieldKind.TEL,
    )
    spec = FieldSpec("phone_number", ("#phone",))
    response = MappingResponse.for_key(2, "phone_number")

    assert field.canonical_key is CanonicalApplicationAnswerKey.PHONE
    assert spec.canonical_key is CanonicalApplicationAnswerKey.PHONE
    assert response.canonical_key is CanonicalApplicationAnswerKey.PHONE


def test_unknown_and_legacy_custom_fields_fail_safe() -> None:
    assert canonical_key_for("Unknown bespoke question") is (
        CanonicalApplicationAnswerKey.UNKNOWN
    )
    assert normalize_canonical_application_answer_key(
        "custom:synthetic question",
        allow_custom_unknown=True,
    ) is CanonicalApplicationAnswerKey.UNKNOWN
    assert MappingResponse.for_key(9, "unknown").status == "unsupported"


def test_semantic_mapper_sensitive_categories_need_review() -> None:
    for key in (
        "work_authorization",
        "sponsorship",
        "gender",
        "race_ethnicity",
        "veteran_status",
        "disability_status",
        "attestation",
        "consent",
        "signature",
    ):
        response = MappingResponse.for_key(1, key)
        assert response.status == "needs_review"


def test_application_bundle_uses_typed_canonical_answer_mapping(
    tmp_path: Path,
) -> None:
    bundle = _bundle(
        tmp_path,
        {
            CanonicalApplicationAnswerKey.EMAIL: (
                "synthetic@example.test"
            ),
            "sponsorship": "No",
        },
    )

    assert isinstance(bundle.answers, CanonicalApplicationAnswers)
    assert tuple(bundle.answers) == (
        CanonicalApplicationAnswerKey.EMAIL,
        CanonicalApplicationAnswerKey.SPONSORSHIP,
    )
    assert bundle.answers.to_dict() == {
        "email": "synthetic@example.test",
        "sponsorship": "No",
    }
    assert bundle.answer_hash == canonical_hash(
        {
            "email": "synthetic@example.test",
            "sponsorship": "No",
        }
    )


def test_application_bundle_rejects_out_of_taxonomy_keys(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="outside the canonical application-answer taxonomy",
    ):
        _bundle(tmp_path, {"favorite_color": "blue"})


def test_legacy_answer_mapping_requires_explicit_compatibility_adapter(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        _bundle(tmp_path, {"phone_number": "synthetic"})

    legacy = CanonicalApplicationAnswers.from_legacy(
        {
            "phone_number": "synthetic",
            "authorized_to_work": "Yes",
            "require_sponsorship": "No",
        }
    )
    bundle = _bundle(tmp_path, legacy)

    assert bundle.answers.to_dict() == {
        "phone": "synthetic",
        "sponsorship": "No",
        "work_authorization": "Yes",
    }


def test_canonical_serialization_and_hash_are_stable() -> None:
    first = CanonicalApplicationAnswers.from_mapping(
        {"sponsorship": "No", "email": "synthetic@example.test"}
    )
    second = CanonicalApplicationAnswers.from_mapping(
        {"email": "synthetic@example.test", "sponsorship": "No"}
    )
    serialized = canonical_application_answer_taxonomy_dict()

    assert first == second
    assert first.to_dict() == second.to_dict()
    assert json.loads(
        json.dumps(serialized, sort_keys=True)
    ) == serialized
    assert canonical_application_answer_taxonomy_hash() == (
        EXPECTED_TAXONOMY_HASH
    )


def test_formir_mapper_and_bundle_reference_shared_types() -> None:
    field_type = get_type_hints(FieldIR)["canonical_key"]
    response_type = get_type_hints(MappingResponse)["canonical_key"]
    bundle_type = get_type_hints(ApplicationBundle)["answers"]

    assert field_type is CanonicalApplicationAnswerKey
    assert response_type is CanonicalApplicationAnswerKey
    assert bundle_type is CanonicalApplicationAnswers


def test_taxonomy_contains_no_candidate_answer_or_execution_capability() -> None:
    source = Path(taxonomy_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert not {
        "core.profile_store",
        "core.application_engine",
        "adapters.protocol",
        "adapters.generic_ai.semantic_mapper",
        "playwright",
    } & imports
    for forbidden in (
        "CandidateVault",
        "ApplicationPlan",
        "SemanticMapper",
        "FormIR",
        "submit",
        "browser",
    ):
        assert forbidden not in source
