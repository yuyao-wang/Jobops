from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from adapters.preparation_agents import (
    ProductionBaseLatexSelectionAgent,
    ProductionCoverLetterAgent,
    ProductionCoverLetterFactQAAgent,
    ProductionPreparationAgentAdapters,
    ProductionResumeFactQAAgent,
    ProductionResumeLatexConstructionAgent,
    ProductionResumeLayoutRevisionAgent,
    ProductionResumeSelectionAgent,
    ProductionResumeTailoringAgent,
    ProductionResumeVisualQAAgent,
)
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    APPLICATION_PREPARATION_STAGE_ORDER,
    ApplicationPreparationOutputReference,
    ApplicationPreparationStage,
    ApplicationPreparationStageRequest,
)
from core.preparation_invocation import PreparationInvocationBinding
from core.production_application_preparation_recipe import (
    ProductionPreparationRecipeConfigurationError,
    ProductionPreparationRecipeErrorCategory,
    ProductionPreparationStageDependencies,
    build_production_application_preparation_recipe,
)


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _agents() -> ProductionPreparationAgentAdapters:
    def agent(agent_type):
        value = Mock(spec=agent_type)
        value.metadata = object()
        return value

    return ProductionPreparationAgentAdapters(
        resume_selection=agent(ProductionResumeSelectionAgent),
        resume_tailoring=agent(ProductionResumeTailoringAgent),
        resume_fact_qa=agent(ProductionResumeFactQAAgent),
        base_latex_selection=agent(ProductionBaseLatexSelectionAgent),
        resume_latex_construction=agent(
            ProductionResumeLatexConstructionAgent
        ),
        resume_visual_qa=agent(ProductionResumeVisualQAAgent),
        resume_layout_revision=agent(
            ProductionResumeLayoutRevisionAgent
        ),
        cover_letter=agent(ProductionCoverLetterAgent),
        cover_letter_fact_qa=agent(
            ProductionCoverLetterFactQAAgent
        ),
    )


def _dependencies() -> ProductionPreparationStageDependencies:
    optional = {
        "resume_selection_override_provider",
        "resume_tailoring_correction_provider",
        "base_latex_override_provider",
        "latex_construction_correction_provider",
        "resume_visual_qa_policy",
        "resume_layout_revision_policy",
        "resume_layout_correction_provider",
        "cover_letter_correction_provider",
        "cover_letter_overflow_correction_provider",
        "application_attestation_provider",
    }
    values = {
        name: object()
        for name in ProductionPreparationStageDependencies.__dataclass_fields__
        if name not in optional
        and name not in {"agents", "dependency_configuration_hash"}
    }
    return ProductionPreparationStageDependencies(
        **values,
        agents=_agents(),
        dependency_configuration_hash="a" * 64,
    )


def _request(stage: ApplicationPreparationStage):
    output_values = {
        "base_latex_selection_id": "base-latex-id",
        "compilation_record_id": "compilation-id",
        "cover_letter_draft_id": "cover-draft-id",
        "cover_letter_evidence_snapshot_id": "cover-evidence-id",
        "cover_letter_fact_qa_result_id": "cover-qa-id",
        "latex_construction_record_id": "construction-id",
        "latex_version_id": "latex-version-id",
        "plan_material_manifest_id": "manifest-id",
        "prepared_cover_letter_material_id": "cover-material-id",
        "prepared_resume_material_id": "resume-material-id",
        "resume_evidence_snapshot_id": "resume-evidence-id",
        "resume_fact_qa_result_id": "resume-qa-id",
        "resume_id": "resume-id",
        "tailored_resume_draft_id": "resume-draft-id",
        "visual_qa_result_id": "visual-id",
    }
    binding = PreparationInvocationBinding.create(
        subject_id="subject-synthetic",
        application_plan_id="plan-synthetic",
        invocation_id="invocation-synthetic",
        orchestration_contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        created_at=NOW,
    )
    return ApplicationPreparationStageRequest(
        stage=stage,
        subject_id="subject-synthetic",
        application_plan_id="plan-synthetic",
        job_id="job-synthetic",
        now=NOW,
        outputs=tuple(
            ApplicationPreparationOutputReference(key, value)
            for key, value in sorted(output_values.items())
        ),
        prior_stage_results=(),
        preparation_invocation_binding=binding,
    )


def test_factory_builds_exact_stable_canonical_production_recipe():
    first = build_production_application_preparation_recipe(_dependencies())
    second = build_production_application_preparation_recipe(_dependencies())

    assert tuple(item.stage for item in first.stages) == (
        APPLICATION_PREPARATION_STAGE_ORDER
    )
    assert len(first.stages) == len(set(item.stage for item in first.stages))
    assert first.input_binding_hash == second.input_binding_hash
    assert first.metadata_hash == second.metadata_hash
    assert all(
        item.public_callable_name.startswith("production_")
        for item in first.stages
    )
    async_stages = {
        item.stage
        for item in first.stages
        if inspect.iscoroutinefunction(item.invoke.__call__)
    }
    assert async_stages == {
        ApplicationPreparationStage.BASE_RESUME_SELECTION,
        ApplicationPreparationStage.RESUME_TAILORING,
        ApplicationPreparationStage.RESUME_FACT_QA,
        ApplicationPreparationStage.BASE_LATEX_SELECTION,
        ApplicationPreparationStage.LATEX_CONSTRUCTION,
        ApplicationPreparationStage.RESUME_VISUAL_QA,
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
        ApplicationPreparationStage.COVER_LETTER_DRAFT,
        ApplicationPreparationStage.COVER_LETTER_FACT_QA,
    }


@pytest.mark.asyncio
async def test_all_stage_adapters_map_request_to_typed_command_once(
    monkeypatch,
):
    import core.production_application_preparation_recipe as module

    service_and_converter = {
        ApplicationPreparationStage.BASE_RESUME_SELECTION: (
            "select_base_resume",
            "base_resume_selection_public_result",
        ),
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: (
            "create_source_resume_projection",
            "source_resume_projection_public_result",
        ),
        ApplicationPreparationStage.RESUME_EVIDENCE: (
            "create_candidate_evidence_snapshot",
            "candidate_evidence_snapshot_public_result",
        ),
        ApplicationPreparationStage.RESUME_TAILORING: (
            "tailor_resume",
            "tailored_resume_draft_public_result",
        ),
        ApplicationPreparationStage.RESUME_FACT_QA: (
            "run_resume_fact_qa",
            "resume_fact_qa_public_result",
        ),
        ApplicationPreparationStage.BASE_LATEX_SELECTION: (
            "select_base_latex_version",
            "base_latex_selection_public_result",
        ),
        ApplicationPreparationStage.LATEX_CONSTRUCTION: (
            "construct_resume_latex_version",
            "resume_latex_construction_public_result",
        ),
        ApplicationPreparationStage.RESUME_COMPILATION: (
            "compile_resume_latex",
            "resume_compilation_public_result",
        ),
        ApplicationPreparationStage.RESUME_VISUAL_QA: (
            "review_resume_visual_qa",
            "resume_visual_qa_public_result",
        ),
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION: (
            "revise_resume_layout",
            "resume_layout_revision_public_result",
        ),
        ApplicationPreparationStage.RESUME_PUBLICATION: (
            "publish_prepared_resume",
            "prepared_resume_publication_public_result",
        ),
        ApplicationPreparationStage.RESUME_MANIFEST: (
            "assemble_plan_material_manifest",
            "resume_manifest_entry_public_result",
        ),
        ApplicationPreparationStage.COVER_LETTER_EVIDENCE: (
            "create_cover_letter_evidence_snapshot",
            "cover_letter_evidence_public_result",
        ),
        ApplicationPreparationStage.COVER_LETTER_DRAFT: (
            "draft_cover_letter",
            "cover_letter_draft_public_result",
        ),
        ApplicationPreparationStage.COVER_LETTER_FACT_QA: (
            "review_cover_letter_fact_qa",
            "cover_letter_fact_qa_public_result",
        ),
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION: (
            "publish_prepared_cover_letter",
            "prepared_cover_letter_publication_public_result",
        ),
        ApplicationPreparationStage.COVER_LETTER_MANIFEST: (
            "include_cover_letter_in_plan_material_manifest",
            "cover_letter_manifest_entry_public_result",
        ),
        ApplicationPreparationStage.APPLICATION_ANSWERS: (
            "prepare_application_answers",
            "application_answers_public_result",
        ),
    }
    expected_command_fields = {
        ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: {
            "resume_id": "resume-id"
        },
        ApplicationPreparationStage.RESUME_TAILORING: {
            "evidence_snapshot_id": "resume-evidence-id"
        },
        ApplicationPreparationStage.RESUME_FACT_QA: {
            "tailored_resume_draft_id": "resume-draft-id"
        },
        ApplicationPreparationStage.BASE_LATEX_SELECTION: {
            "fact_qa_result_id": "resume-qa-id"
        },
        ApplicationPreparationStage.LATEX_CONSTRUCTION: {
            "base_latex_selection_decision_id": "base-latex-id",
            "fact_qa_result_id": "resume-qa-id",
        },
        ApplicationPreparationStage.RESUME_COMPILATION: {
            "resume_latex_construction_record_id": "construction-id",
            "resume_latex_version_id": "latex-version-id",
        },
        ApplicationPreparationStage.RESUME_VISUAL_QA: {
            "resume_compilation_record_id": "compilation-id"
        },
        ApplicationPreparationStage.RESUME_LAYOUT_REVISION: {
            "resume_visual_qa_result_id": "visual-id"
        },
        ApplicationPreparationStage.RESUME_PUBLICATION: {
            "resume_visual_qa_result_id": "visual-id",
            "resume_layout_revision_run_id": None,
        },
        ApplicationPreparationStage.RESUME_MANIFEST: {
            "prepared_resume_material_id": "resume-material-id"
        },
        ApplicationPreparationStage.COVER_LETTER_DRAFT: {
            "cover_letter_evidence_snapshot_id": "cover-evidence-id"
        },
        ApplicationPreparationStage.COVER_LETTER_FACT_QA: {
            "cover_letter_evidence_snapshot_id": "cover-evidence-id",
            "cover_letter_draft_id": "cover-draft-id",
        },
        ApplicationPreparationStage.COVER_LETTER_PUBLICATION: {
            "cover_letter_fact_qa_result_id": "cover-qa-id"
        },
        ApplicationPreparationStage.COVER_LETTER_MANIFEST: {
            "plan_material_manifest_id": "manifest-id",
            "prepared_cover_letter_material_id": "cover-material-id",
        },
    }
    calls = []
    expected = object()
    recipe = build_production_application_preparation_recipe(_dependencies())
    definitions = {item.stage: item for item in recipe.stages}

    for stage in APPLICATION_PREPARATION_STAGE_ORDER:
        service_name, converter_name = service_and_converter[stage]
        original = getattr(module, service_name)
        if inspect.iscoroutinefunction(original):
            async def service(command, _stage=stage, **kwargs):
                calls.append((_stage, command, kwargs))
                return object()
        else:
            def service(command, _stage=stage, **kwargs):
                calls.append((_stage, command, kwargs))
                return object()
        monkeypatch.setattr(module, service_name, service)
        monkeypatch.setattr(
            module,
            converter_name,
            lambda result, **kwargs: expected,
        )
        value = definitions[stage].invoke(_request(stage))
        actual = await value if inspect.isawaitable(value) else value
        assert actual is expected
        called_stage, command, kwargs = calls[-1]
        assert called_stage is stage
        assert command.subject_id == "subject-synthetic"
        assert command.now is NOW
        if hasattr(command, "application_plan_id"):
            assert command.application_plan_id == "plan-synthetic"
        for name, expected_value in expected_command_fields.get(
            stage, {}
        ).items():
            assert getattr(command, name) == expected_value
        assert kwargs

    assert len(calls) == len(APPLICATION_PREPARATION_STAGE_ORDER)


def test_factory_fails_closed_without_partial_recipe_or_agent(monkeypatch):
    dependencies = _dependencies()
    with pytest.raises(ProductionPreparationRecipeConfigurationError) as exc:
        build_production_application_preparation_recipe(
            replace(dependencies, latex_compiler=None)
        )
    assert exc.value.category is (
        ProductionPreparationRecipeErrorCategory.MISSING_DEPENDENCY
    )
    assert exc.value.dependency == "latex_compiler"

    with pytest.raises(ProductionPreparationRecipeConfigurationError) as exc:
        build_production_application_preparation_recipe(
            replace(
                dependencies,
                agents=SimpleNamespace(contract_version="invalid"),
            )
        )
    assert exc.value.category is (
        ProductionPreparationRecipeErrorCategory.INVALID_AGENT_BUNDLE
    )

    import core.production_application_preparation_recipe as module

    with monkeypatch.context() as scoped:
        scoped.setattr(module, "application_answers_public_result", None)
        with pytest.raises(
            ProductionPreparationRecipeConfigurationError
        ) as exc:
            build_production_application_preparation_recipe(dependencies)
        assert exc.value.category is (
            ProductionPreparationRecipeErrorCategory.MISSING_CONVERTER
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(
            module,
            "_STAGE_CONTRACTS",
            tuple(reversed(module._STAGE_CONTRACTS)),
        )
        with pytest.raises(
            ProductionPreparationRecipeConfigurationError
        ) as exc:
            build_production_application_preparation_recipe(dependencies)
        assert exc.value.category is (
            ProductionPreparationRecipeErrorCategory
            .INVALID_CANONICAL_ORDER
        )


def test_production_recipe_has_no_event_loop_bridge_or_test_dependency():
    source = Path(
        "core/production_application_preparation_recipe.py"
    ).read_text(encoding="utf-8")
    assert "asyncio.run" not in source
    assert "run_until_complete" not in source
    assert "to_thread" not in source
    assert "tests." not in source
    assert "lambda success" not in source
