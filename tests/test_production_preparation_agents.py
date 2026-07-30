from __future__ import annotations

import base64
import copy
import shutil
from dataclasses import replace

import pytest

from adapters.preparation_agents import (
    ProductionPreparationAgentError,
    ProductionPreparationAgentErrorCategory,
    build_production_preparation_agent_adapters,
)
from core.base_latex_selection import (
    BaseLatexSelectionAgentPort,
    BaseLatexSelectionContext,
    BaseLatexSelectionJobContext,
)
from core.cover_letter_draft import (
    COVER_LETTER_DRAFT_AGENT_POLICY,
    COVER_LETTER_DRAFT_POLICY_VERSION,
    CoverLetterAgentContext,
    CoverLetterAgentPort,
    CoverLetterEvidenceView,
    CoverLetterJobContext,
)
from core.cover_letter_fact_qa import (
    COVER_LETTER_FACT_QA_AGENT_POLICY,
    COVER_LETTER_FACT_QA_POLICY_VERSION,
    CoverLetterFactQAAgentContext,
    CoverLetterFactQAAgentPort,
)
from core.model_provider_capabilities import (
    AuxiliaryAccessMode,
    BackendAccessLevel,
    CredentialSourcePolicy,
    FilesystemAccessMode,
    ModelBackendAuthenticationMode,
    ModelBackendCapabilities,
    ModelBackendResolutionError,
    ModelBackendResolutionStatus,
    ModelBackendTransport,
    NativeModelBackendCapabilities,
    ShellAccessMode,
    ToolExecutionMode,
    model_execution_isolation_profiles,
)
from utils.llm import CodexCLIBackend, OpenAIAPIBackend
from core.resume_fact_qa import (
    RESUME_FACT_QA_AGENT_POLICY,
    RESUME_FACT_QA_POLICY_VERSION,
    ResumeFactQAAgentPort,
    ResumeFactQAContext,
)
from core.resume_latex_construction import (
    RESUME_LATEX_CONSTRUCTION_AGENT_POLICY,
    RESUME_LATEX_CONSTRUCTION_POLICY_VERSION,
    ResumeLatexConstructionAgentPort,
    ResumeLatexConstructionContext,
    validate_constructed_source,
)
from core.resume_layout_revision import (
    RESUME_LAYOUT_REVISION_AGENT_POLICY,
    ResumeLayoutRevisionAgentPort,
    ResumeLayoutRevisionContext,
)
from core.resume_selection import (
    ResumeSelectionAgentPort,
    ResumeSelectionContext,
    ResumeSelectionJobContext,
)
from core.resume_tailoring import (
    RESUME_TAILORING_AGENT_POLICY,
    RESUME_TAILORING_POLICY_VERSION,
    ResumeTailoringAgentPort,
    ResumeTailoringContext,
    ResumeTailoringJobContext,
)
from core.resume_visual_qa import (
    RESUME_VISUAL_QA_AGENT_POLICY,
    RESUME_VISUAL_QA_POLICY_VERSION,
    ResumeVisualQAAgentPort,
    ResumeVisualQAContext,
    ResumeVisualQAPageView,
)


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_OUTPUTS = {
    "resume_selection": {
        "disposition": "SELECTED",
        "selected_resume_id": "resume-synthetic",
        "selected_candidate_version": "resume-candidate-v1",
        "selected_artifact_sha256": "b" * 64,
        "rationale": "Synthetic bounded decision.",
    },
    "resume_tailoring": {
        "disposition": "TAILORED",
        "sections": [
            {
                "source_section_id": "section-synthetic",
                "order": 0,
                "bullets": [
                    {
                        "source_section_id": "section-synthetic",
                        "source_block_id": "block-synthetic",
                        "source_bullet_id": "bullet-synthetic",
                        "change_type": "REWRITTEN",
                        "text": "Synthetic evidence-bound bullet.",
                        "evidence_ids": ["evidence-synthetic"],
                        "jd_alignment": ["Synthetic requirement"],
                    }
                ],
            }
        ],
        "rationale": "Synthetic bounded decision.",
    },
    "resume_fact_qa": {
        "verdict": "UNSUPPORTED",
        "findings": [
            {
                "source_section_id": "section-synthetic",
                "source_block_id": "block-synthetic",
                "source_bullet_id": "bullet-synthetic",
                "finding_type": "UNSUPPORTED_IMPACT",
                "claim_text": "Synthetic unsupported impact.",
                "cited_evidence_ids": ["evidence-synthetic"],
                "explanation": "Synthetic bounded finding.",
            }
        ],
    },
    "base_latex_selection": {
        "disposition": "SELECTED",
        "selected_latex_version_id": "latex-version-synthetic",
        "rationale": "Synthetic bounded decision.",
    },
    "resume_latex_construction": {
        "latex_source": "\\documentclass{article}\\begin{document}x\\end{document}"
    },
    "resume_visual_qa": {
        "verdict": "ISSUES_FOUND",
        "findings": [
            {
                "finding_type": "EXCESSIVE_WHITESPACE",
                "page_number": 1,
                "explanation": "Synthetic visual finding.",
                "bounding_box": {
                    "x0": 0,
                    "top": 0,
                    "x1": 1,
                    "bottom": 1,
                },
            }
        ],
    },
    "resume_layout_revision": {
        "latex_source": "\\documentclass{article}\\begin{document}x\\end{document}"
    },
    "cover_letter": {
        "greeting": "Dear Hiring Team,",
        "paragraphs": [
            {
                "purpose": "INTRODUCTION",
                "text": "Synthetic introduction.",
                "evidence_ids": [],
                "jd_alignment": [],
            }
        ],
        "closing": "Sincerely,",
        "rationale": "Synthetic bounded decision.",
    },
    "cover_letter_fact_qa": {
        "verdict": "BLOCKED",
        "findings": [
            {
                "paragraph_id": "paragraph-synthetic",
                "finding_type": "UNSUPPORTED_IMPACT_OR_CAUSALITY",
                "severity": "BLOCKING",
                "claim_text": "Synthetic unsupported impact.",
                "evidence_ids": ["evidence-synthetic"],
                "jd_references": ["Synthetic requirement"],
                "explanation": "Synthetic bounded finding.",
            }
        ],
    },
}


class _FakeStructuredBackend:
    capabilities = ModelBackendCapabilities(
        backend_id="synthetic_structured",
        supports_text_input=True,
        supports_image_input=True,
        supports_strict_json_schema=True,
        supports_single_semantic_generation=True,
        safe_for_untrusted_input=True,
        tool_execution_mode=ToolExecutionMode.NONE,
        filesystem_access_mode=FilesystemAccessMode.NONE,
        shell_access_mode=ShellAccessMode.NONE,
        browser_access_mode=AuxiliaryAccessMode.NONE,
        external_function_access_mode=AuxiliaryAccessMode.NONE,
        credential_source_policy=CredentialSourcePolicy.UNVERIFIED,
        provider_family="synthetic",
        transport=ModelBackendTransport.DIRECT_API,
        authentication_mode=ModelBackendAuthenticationMode.LOCAL_NO_CREDENTIAL,
    )
    native_capabilities = NativeModelBackendCapabilities(
        backend_id="synthetic_structured",
        provider_family="synthetic",
        transport=ModelBackendTransport.DIRECT_API,
        authentication_mode=ModelBackendAuthenticationMode.LOCAL_NO_CREDENTIAL,
        supports_text_input=True,
        supports_image_input=True,
        supports_schema_constrained_output=True,
        supports_provider_native_strict_schema=True,
        supports_single_semantic_generation=True,
        supports_ephemeral_workspace=False,
        supports_non_interactive_execution=True,
        supports_bounded_output=True,
        supports_subscription_authentication=False,
        native_tool_access=BackendAccessLevel.NONE,
        native_filesystem_access=BackendAccessLevel.NONE,
        native_shell_access=BackendAccessLevel.NONE,
        native_browser_access=BackendAccessLevel.NONE,
        native_external_function_access=BackendAccessLevel.NONE,
    )
    calls = []
    outputs = copy.deepcopy(_OUTPUTS)
    failure = None

    def __init__(self, config):
        self.model = config.get("model", "synthetic-model")

    async def complete_structured_request(self, request):
        type(self).calls.append(request)
        if type(self).failure == "timeout":
            raise TimeoutError("synthetic timeout")
        if type(self).failure == "unavailable":
            raise RuntimeError("synthetic provider detail must be redacted")
        return copy.deepcopy(type(self).outputs[request.component_id])


class _TextOnlyBackend(_FakeStructuredBackend):
    capabilities = replace(
        _FakeStructuredBackend.capabilities,
        backend_id="text_only",
        supports_image_input=False,
    )
    native_capabilities = replace(
        _FakeStructuredBackend.native_capabilities,
        backend_id="text_only",
        supports_image_input=False,
    )


@pytest.fixture(autouse=True)
def _reset_backend():
    _FakeStructuredBackend.calls = []
    _FakeStructuredBackend.outputs = copy.deepcopy(_OUTPUTS)
    _FakeStructuredBackend.failure = None
    yield


def _config(*, backend="synthetic_structured", model="synthetic-model"):
    return {
        "default_backend": backend,
        "backends": {backend: {"model": model}},
        "components": {
            component_id: backend for component_id in _OUTPUTS
        },
    }


def _contexts():
    resume_job = ResumeSelectionJobContext(
        job_id="job-synthetic",
        revision=1,
        content_hash="a" * 64,
        company="Synthetic Co",
        title="Engineer",
        description="Synthetic job description.",
        location="Remote",
        work_mode="REMOTE",
        posted_at=None,
        source_platform="synthetic",
    )
    tailoring_job = ResumeTailoringJobContext(
        job_id="job-synthetic",
        revision=1,
        content_hash="a" * 64,
        company="Synthetic Co",
        title="Engineer",
        description="Synthetic job description.",
        location="Remote",
        work_mode="REMOTE",
    )
    cover_job = CoverLetterJobContext(
        job_id="job-synthetic",
        revision=1,
        content_hash="a" * 64,
        company="Synthetic Co",
        title="Engineer",
        description="Synthetic job description.",
        location="Remote",
        work_mode="REMOTE",
    )
    return {
        "resume_selection": ResumeSelectionContext(
            subject_id="subject-synthetic",
            application_plan_id="plan-synthetic",
            job=resume_job,
            candidates=(),
            user_preparation_instructions=None,
        ),
        "resume_tailoring": ResumeTailoringContext(
            subject_id="subject-synthetic",
            application_plan_id="plan-synthetic",
            job=tailoring_job,
            source_projection={"projection": "synthetic"},
            evidence_items=(),
            user_preparation_instructions=None,
            agent_policy=RESUME_TAILORING_AGENT_POLICY,
            agent_policy_version=RESUME_TAILORING_POLICY_VERSION,
            correction_constraints=(),
        ),
        "resume_fact_qa": ResumeFactQAContext(
            subject_id="subject-synthetic",
            tailored_resume_draft_id="draft-synthetic",
            bullets=(),
            evidence_items=(),
            agent_policy=RESUME_FACT_QA_AGENT_POLICY,
            agent_policy_version=RESUME_FACT_QA_POLICY_VERSION,
        ),
        "base_latex_selection": BaseLatexSelectionContext(
            subject_id="subject-synthetic",
            application_plan_id="plan-synthetic",
            job=BaseLatexSelectionJobContext(
                job_id="job-synthetic",
                revision=1,
                content_hash="a" * 64,
                company="Synthetic Co",
                title="Engineer",
                description="Synthetic job description.",
            ),
            candidates=(),
            user_preparation_instructions=None,
        ),
        "resume_latex_construction": ResumeLatexConstructionContext(
            subject_id="subject-synthetic",
            tailored_resume_draft_id="draft-synthetic",
            base_latex_source="\\documentclass{article}",
            sections=(),
            user_preparation_instructions=None,
            marker_contract={"version": "synthetic"},
            agent_policy=RESUME_LATEX_CONSTRUCTION_AGENT_POLICY,
            agent_policy_version=RESUME_LATEX_CONSTRUCTION_POLICY_VERSION,
        ),
        "resume_visual_qa": ResumeVisualQAContext(
            subject_id="subject-synthetic",
            pages=(
                ResumeVisualQAPageView(
                    page_number=1,
                    width_px=1,
                    height_px=1,
                    image_format="PNG",
                    image_bytes=_PNG,
                ),
            ),
            deterministic_findings=(),
            policy={"version": "synthetic"},
            policy_version=RESUME_VISUAL_QA_POLICY_VERSION,
            agent_policy=RESUME_VISUAL_QA_AGENT_POLICY,
        ),
        "resume_layout_revision": ResumeLayoutRevisionContext(
            subject_id="subject-synthetic",
            attempt_number=1,
            latex_source="\\documentclass{article}",
            pages=(),
            findings=(),
            visual_qa_policy={"version": "synthetic"},
            layout_revision_policy={"version": "synthetic"},
            user_preparation_instructions=None,
            agent_policy=RESUME_LAYOUT_REVISION_AGENT_POLICY,
        ),
        "cover_letter": CoverLetterAgentContext(
            subject_id="subject-synthetic",
            application_plan_id="plan-synthetic",
            job=cover_job,
            evidence_items=(
                CoverLetterEvidenceView(
                    evidence_id="evidence-synthetic",
                    evidence_text="Synthetic evidence.",
                ),
            ),
            user_preparation_instructions=None,
            agent_policy=COVER_LETTER_DRAFT_AGENT_POLICY,
            agent_policy_version=COVER_LETTER_DRAFT_POLICY_VERSION,
            correction_constraints=(),
        ),
        "cover_letter_fact_qa": CoverLetterFactQAAgentContext(
            subject_id="subject-synthetic",
            application_plan_id="plan-synthetic",
            job=cover_job,
            greeting="Dear Hiring Team,",
            paragraphs=(),
            closing="Sincerely,",
            evidence_items=(),
            qa_policy=COVER_LETTER_FACT_QA_AGENT_POLICY,
            qa_policy_version=COVER_LETTER_FACT_QA_POLICY_VERSION,
        ),
    }


@pytest.mark.asyncio
async def test_factory_builds_and_executes_all_nine_typed_ports_once():
    bundle = build_production_preparation_agent_adapters(
        ai_config=_config(),
        backend_registry={"synthetic_structured": _FakeStructuredBackend},
    )
    contexts = _contexts()
    calls = (
        (bundle.resume_selection, ResumeSelectionAgentPort, "evaluate"),
        (bundle.resume_tailoring, ResumeTailoringAgentPort, "tailor"),
        (bundle.resume_fact_qa, ResumeFactQAAgentPort, "review"),
        (
            bundle.base_latex_selection,
            BaseLatexSelectionAgentPort,
            "evaluate",
        ),
        (
            bundle.resume_latex_construction,
            ResumeLatexConstructionAgentPort,
            "construct",
        ),
        (bundle.resume_visual_qa, ResumeVisualQAAgentPort, "review"),
        (
            bundle.resume_layout_revision,
            ResumeLayoutRevisionAgentPort,
            "revise",
        ),
        (bundle.cover_letter, CoverLetterAgentPort, "generate"),
        (
            bundle.cover_letter_fact_qa,
            CoverLetterFactQAAgentPort,
            "review",
        ),
    )
    for component_id, (adapter, port, method) in zip(_OUTPUTS, calls):
        assert isinstance(adapter, port)
        output = await getattr(adapter, method)(contexts[component_id])
        assert output is not None
        assert adapter.call_metadata.component_id == component_id
        assert adapter.call_metadata.prompt_version.startswith(
            component_id.replace("_", "-")
        )

    assert len(_FakeStructuredBackend.calls) == 9
    assert [call.component_id for call in _FakeStructuredBackend.calls] == list(
        _OUTPUTS
    )
    visual = next(
        call
        for call in _FakeStructuredBackend.calls
        if call.component_id == "resume_visual_qa"
    )
    assert len(visual.images) == 1
    assert visual.images[0].content == _PNG
    assert all(
        not call.images
        for call in _FakeStructuredBackend.calls
        if call.component_id != "resume_visual_qa"
    )
    prompt_versions = {
        adapter.call_metadata.prompt_version
        for adapter, _, _ in calls
    }
    schema_versions = {
        adapter.call_metadata.schema_contract_version
        for adapter, _, _ in calls
    }
    assert len(prompt_versions) == len(schema_versions) == 9
    if shutil.which("codex"):
        codex_bundle = build_production_preparation_agent_adapters(
            ai_config={
                "default_backend": "codex_cli",
                "backends": {
                    "codex_cli": {
                        "isolation_profile": (
                            "isolated_subscription_cli_v1"
                        )
                    }
                },
                "components": {
                    component_id: "codex_cli"
                    for component_id in _OUTPUTS
                },
            },
            backend_registry={"codex_cli": CodexCLIBackend},
            isolation_profile_registry=model_execution_isolation_profiles(
                isolated_subscription_cli_runner_available=True
            ),
        )
        assert isinstance(
            codex_bundle.resume_visual_qa,
            ResumeVisualQAAgentPort,
        )


@pytest.mark.asyncio
async def test_malformed_timeout_and_unavailable_fail_typed_without_retry():
    contexts = _contexts()
    bundle = build_production_preparation_agent_adapters(
        ai_config=_config(),
        backend_registry={"synthetic_structured": _FakeStructuredBackend},
    )
    _FakeStructuredBackend.outputs["resume_selection"]["unknown"] = "field"
    with pytest.raises(Exception) as malformed:
        await bundle.resume_selection.evaluate(contexts["resume_selection"])
    assert isinstance(
        malformed.value.__cause__, ProductionPreparationAgentError
    )
    assert malformed.value.__cause__.category is (
        ProductionPreparationAgentErrorCategory.SCHEMA_OUTPUT_INVALID
    )
    assert len(_FakeStructuredBackend.calls) == 1

    _FakeStructuredBackend.calls = []
    _FakeStructuredBackend.outputs = copy.deepcopy(_OUTPUTS)
    _FakeStructuredBackend.outputs["resume_selection"][
        "disposition"
    ] = "UNKNOWN_ENUM"
    with pytest.raises(Exception) as unknown_enum:
        await bundle.resume_selection.evaluate(contexts["resume_selection"])
    assert unknown_enum.value.__cause__.category is (
        ProductionPreparationAgentErrorCategory.SCHEMA_OUTPUT_INVALID
    )
    assert len(_FakeStructuredBackend.calls) == 1

    _FakeStructuredBackend.calls = []
    _FakeStructuredBackend.outputs = copy.deepcopy(_OUTPUTS)
    _FakeStructuredBackend.failure = "timeout"
    with pytest.raises(TimeoutError):
        await bundle.resume_tailoring.tailor(contexts["resume_tailoring"])
    assert len(_FakeStructuredBackend.calls) == 1

    _FakeStructuredBackend.calls = []
    _FakeStructuredBackend.failure = "unavailable"
    with pytest.raises(Exception) as unavailable:
        await bundle.resume_fact_qa.review(contexts["resume_fact_qa"])
    assert unavailable.value.__cause__.category is (
        ProductionPreparationAgentErrorCategory.BACKEND_UNAVAILABLE
    )
    assert "synthetic provider detail" not in str(unavailable.value)
    assert len(_FakeStructuredBackend.calls) == 1


def test_factory_fails_fast_for_missing_or_incompatible_mandatory_component(
    monkeypatch,
):
    missing = _config()
    missing["components"]["resume_selection"] = "missing_backend"
    with pytest.raises(ModelBackendResolutionError) as absent:
        build_production_preparation_agent_adapters(
            ai_config=missing,
            backend_registry={"synthetic_structured": _FakeStructuredBackend},
        )
    assert absent.value.status is ModelBackendResolutionStatus.BACKEND_NOT_FOUND

    with pytest.raises(ModelBackendResolutionError) as modality:
        build_production_preparation_agent_adapters(
            ai_config=_config(backend="text_only"),
            backend_registry={"text_only": _TextOnlyBackend},
        )
    assert modality.value.status is ModelBackendResolutionStatus.MODALITY_UNSUPPORTED

    mixed = _config(backend="text_only")
    mixed["backends"]["synthetic_structured"] = {
        "model": "synthetic-visual-model"
    }
    mixed["components"]["resume_visual_qa"] = "synthetic_structured"
    mixed_bundle = build_production_preparation_agent_adapters(
        ai_config=mixed,
        backend_registry={
            "text_only": _TextOnlyBackend,
            "synthetic_structured": _FakeStructuredBackend,
        },
    )
    assert (
        mixed_bundle.resume_visual_qa.call_metadata.backend_id
        == "synthetic_structured"
    )
    assert (
        mixed_bundle.resume_tailoring.call_metadata.backend_id == "text_only"
    )

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ModelBackendResolutionError) as credential:
        build_production_preparation_agent_adapters(
            ai_config={
                "default_backend": "openai_api",
                "backends": {
                    "openai_api": {
                        "model": "synthetic-openai-model",
                        "api_key_env": "OPENAI_API_KEY",
                    }
                },
                "components": {
                    component_id: "openai_api"
                    for component_id in _OUTPUTS
                },
            },
            backend_registry={"openai_api": OpenAIAPIBackend},
        )
    assert credential.value.status is (
        ModelBackendResolutionStatus.CREDENTIAL_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_request_projection_is_bounded_and_diagnostics_are_secret_safe(
    caplog,
):
    context = _contexts()["resume_visual_qa"]
    bundle = build_production_preparation_agent_adapters(
        ai_config=_config(model="synthetic-model-v2"),
        backend_registry={"synthetic_structured": _FakeStructuredBackend},
    )
    await bundle.resume_visual_qa.review(context)
    request = _FakeStructuredBackend.calls[-1]
    serialized = request.input_bytes()
    assert _PNG not in serialized
    assert b"OPENAI_API_KEY" not in serialized
    assert b"/Users/" not in serialized
    assert request.max_images == 4
    assert request.timeout_seconds == 120
    assert bundle.resume_visual_qa.call_metadata.model_id == "synthetic-model-v2"
    replay = build_production_preparation_agent_adapters(
        ai_config=_config(model="synthetic-model-v2"),
        backend_registry={"synthetic_structured": _FakeStructuredBackend},
    )
    assert (
        replay.resume_visual_qa.call_metadata.backend_resolution_identity
        == bundle.resume_visual_qa.call_metadata.backend_resolution_identity
    )
    assert "Synthetic evidence" not in caplog.text
    assert "/Users/" not in caplog.text


@pytest.mark.asyncio
async def test_existing_validator_still_rejects_unsafe_typed_domain_output():
    _FakeStructuredBackend.outputs["resume_latex_construction"] = {
        "latex_source": (
            "\\documentclass{article}\\input{outside}"
            "\\begin{document}x\\end{document}"
        )
    }
    bundle = build_production_preparation_agent_adapters(
        ai_config=_config(),
        backend_registry={"synthetic_structured": _FakeStructuredBackend},
    )
    output = await bundle.resume_latex_construction.construct(
        _contexts()["resume_latex_construction"]
    )
    with pytest.raises(ValueError):
        validate_constructed_source(
            output.latex_source,
            sections=(),
            base_source=None,
        )
