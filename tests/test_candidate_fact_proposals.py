"""Focused C1c Candidate Fact Proposal tests."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from adapters.candidate_fact_proposals import (
    CANDIDATE_FACT_PROPOSAL_COMPONENT_REQUIREMENTS,
    build_production_candidate_fact_proposal_agent,
)
from core.application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY,
    ApplicationExecutionIdentityFieldKey,
)
from core.candidate_fact_proposals import (
    CANDIDATE_FACT_PROPOSAL_AGENT_POLICY_VERSION,
    CANDIDATE_FACT_PROPOSAL_AGENT_SCHEMA_VERSION,
    CANDIDATE_FACT_PROPOSAL_COMPONENT_ID,
    CandidateFactProposalAgentEvidenceRef,
    CandidateFactProposalAgentItem,
    CandidateFactProposalAgentMetadata,
    CandidateFactProposalAgentOutput,
    CandidateFactProposalConfidence,
    CandidateFactProposalReadStatus,
    CandidateFactProposalRunStatus,
    PrivateHomeCandidateFactProposalRepository,
    ProposeCandidateFactsCommand,
    get_candidate_fact_proposal,
    list_candidate_fact_proposals,
    list_current_candidate_fact_proposals,
    propose_candidate_facts,
)
from core.candidate_information_sources import (
    PrivateHomeCandidateInformationSourceRepository,
    RegisterCandidateFileSourceCommand,
    RegisterCandidateUserStatementSourceCommand,
    register_candidate_file_source,
    register_candidate_user_statement_source,
)
from core.candidate_source_projections import (
    PrivateHomeCandidateSourceProjectionRepository,
    ProjectCandidateInformationSourceCommand,
    project_candidate_information_source,
)
from core.model_provider_capabilities import (
    AuxiliaryAccessMode,
    BackendAccessLevel,
    CredentialSourcePolicy,
    FilesystemAccessMode,
    ModelBackendAuthenticationMode,
    ModelBackendCapabilities,
    ModelBackendResolutionError,
    ModelBackendTransport,
    NativeModelBackendCapabilities,
    ShellAccessMode,
    ToolExecutionMode,
)
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 29, 20, 0, tzinfo=timezone.utc)
SUBJECT = "subject-c1c-synthetic"


def _stores(tmp_path: Path):
    home = PrivateHome(tmp_path / "private")
    return (
        PrivateHomeCandidateInformationSourceRepository(home),
        PrivateHomeCandidateSourceProjectionRepository(home),
        PrivateHomeCandidateFactProposalRepository(home),
    )


def _statement_projection(tmp_path: Path, *, subject=SUBJECT, suffix="one", text=None):
    source_store, projection_store, proposal_store = _stores(tmp_path)
    source = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            subject,
            f"register-{suffix}",
            NOW,
            (text or "Contact email: person@example.test").encode(),
        ),
        repository=source_store,
    ).source
    projection = project_candidate_information_source(
        ProjectCandidateInformationSourceCommand(
            subject,
            source.source_id,
            source.source_version,
            source.source_identity_hash,
            f"project-{suffix}",
            NOW,
        ),
        source_repository=source_store,
        projection_repository=projection_store,
    ).projection
    return source, projection, projection_store, proposal_store


def _command(source, projection, invocation):
    return ProposeCandidateFactsCommand(
        source.subject_id,
        source.source_id,
        source.source_version,
        source.source_identity_hash,
        projection.projection_id,
        projection.projection_hash,
        invocation,
        NOW,
    )


def _metadata():
    return CandidateFactProposalAgentMetadata(
        CANDIDATE_FACT_PROPOSAL_COMPONENT_ID,
        "synthetic-structured",
        "synthetic-model",
        CANDIDATE_FACT_PROPOSAL_AGENT_POLICY_VERSION,
        CANDIDATE_FACT_PROPOSAL_AGENT_SCHEMA_VERSION,
        "model-backend-resolution-" + "a" * 64,
    )


class _Agent:
    def __init__(self, factory):
        self.factory = factory
        self.calls = 0

    async def propose(self, context):
        self.calls += 1
        return self.factory(context)


def _block_item(context, *, field="email", value="person@example.test", excerpt=None):
    block = context.input_snapshot.selected_blocks[0]
    return CandidateFactProposalAgentItem(
        field,
        value,
        (
            CandidateFactProposalAgentEvidenceRef(
                block_id=block.block_id,
                block_hash=block.block_hash,
                source_locator=block.source_locator.to_dict(),
            ),
        ),
        excerpt if excerpt is not None else value,
        CandidateFactProposalConfidence.HIGH,
        "Explicit synthetic evidence.",
    )


@pytest.mark.asyncio
async def test_text_and_image_proposals_bind_exact_projection_evidence(tmp_path: Path) -> None:
    source, projection, projection_store, proposal_store = _statement_projection(tmp_path)
    agent = _Agent(lambda context: CandidateFactProposalAgentOutput((_block_item(context),)))
    result = await propose_candidate_facts(
        _command(source, projection, "propose-text"),
        projection_repository=projection_store,
        agent=agent,
        agent_metadata=_metadata(),
        repository=proposal_store,
    )
    assert result.status is CandidateFactProposalRunStatus.CREATED
    assert agent.calls == 1
    proposal = result.proposals[0]
    assert proposal.proposed_normalized_value == "person@example.test"
    assert proposal.source_id == source.source_id
    assert proposal.projection_hash == projection.projection_hash
    assert proposal.evidence_refs[0].evidence_id == projection.block_ids[0]
    source_ref = proposal.to_proposed_fact_source_ref()
    assert source_ref.source_id == proposal.source_id
    assert source_ref.source_hash == proposal.source_hash

    image_bytes = io.BytesIO()
    Image.new("RGB", (3, 2), color=(20, 30, 40)).save(image_bytes, "PNG")
    image_source = register_candidate_file_source(
        RegisterCandidateFileSourceCommand(
            SUBJECT, "register-image", NOW, image_bytes.getvalue()
        ),
        repository=PrivateHomeCandidateInformationSourceRepository(
            PrivateHome(tmp_path / "private")
        ),
    ).source
    image_projection = project_candidate_information_source(
        ProjectCandidateInformationSourceCommand(
            SUBJECT, image_source.source_id, image_source.source_version,
            image_source.source_identity_hash, "project-image", NOW
        ),
        source_repository=PrivateHomeCandidateInformationSourceRepository(
            PrivateHome(tmp_path / "private")
        ),
        projection_repository=projection_store,
    ).projection

    def image_output(context):
        asset = context.input_snapshot.selected_assets[0]
        return CandidateFactProposalAgentOutput((
            CandidateFactProposalAgentItem(
                "first_name", "Synthetic",
                (CandidateFactProposalAgentEvidenceRef(
                    asset_id=asset.asset_id, asset_hash=asset.asset_hash,
                    source_locator=asset.source_locator.to_dict(),
                ),),
                "", CandidateFactProposalConfidence.MEDIUM,
                "Visible synthetic label.",
            ),
        ))

    image_agent = _Agent(image_output)
    image_result = await propose_candidate_facts(
        _command(image_source, image_projection, "propose-image"),
        projection_repository=projection_store,
        agent=image_agent,
        agent_metadata=_metadata(),
        repository=proposal_store,
    )
    assert image_result.status is CandidateFactProposalRunStatus.CREATED
    assert image_result.proposals[0].evidence_refs[0].evidence_kind == "ASSET"


@pytest.mark.asyncio
async def test_unknown_invalid_or_unbound_output_is_rejected(tmp_path: Path) -> None:
    source, projection, projection_store, proposal_store = _statement_projection(tmp_path)

    def invalid(context):
        block = context.input_snapshot.selected_blocks[0]
        locator = block.source_locator.to_dict()
        return CandidateFactProposalAgentOutput((
            _block_item(context, field="not_a_field"),
            CandidateFactProposalAgentItem(
                "email", "invalid", (
                    CandidateFactProposalAgentEvidenceRef(
                        block_id=block.block_id, block_hash=block.block_hash,
                        source_locator=locator,
                    ),
                ), "invalid", CandidateFactProposalConfidence.LOW, "invalid"
            ),
            replace(_block_item(context), evidence_excerpt="rewritten evidence"),
            replace(
                _block_item(context),
                evidence_refs=(CandidateFactProposalAgentEvidenceRef(
                    block_id="candidate-block-missing",
                    block_hash="0" * 64,
                    source_locator=locator,
                ),),
            ),
            replace(_block_item(context), evidence_refs=()),
        ))

    result = await propose_candidate_facts(
        _command(source, projection, "propose-invalid"),
        projection_repository=projection_store,
        agent=_Agent(invalid),
        agent_metadata=_metadata(),
        repository=proposal_store,
    )
    assert result.status is CandidateFactProposalRunStatus.FAILED_AGENT_OUTPUT
    assert result.proposals == ()
    assert len(result.run.rejected_output_items) == 5
    assert list_candidate_fact_proposals(SUBJECT, repository=proposal_store).proposals == ()


@pytest.mark.asyncio
async def test_replay_dedupe_multivalue_and_source_identity(tmp_path: Path) -> None:
    source, projection, projection_store, proposal_store = _statement_projection(tmp_path)
    agent = _Agent(lambda context: CandidateFactProposalAgentOutput((
        _block_item(context),
        _block_item(context),
        _block_item(context, value="other@example.test", excerpt="Contact email"),
    )))
    command = _command(source, projection, "propose-replay")
    first = await propose_candidate_facts(
        command, projection_repository=projection_store, agent=agent,
        agent_metadata=_metadata(), repository=proposal_store,
    )
    replay = await propose_candidate_facts(
        command, projection_repository=projection_store, agent=agent,
        agent_metadata=_metadata(), repository=proposal_store,
    )
    assert len(first.proposals) == 2
    assert replay.status is CandidateFactProposalRunStatus.UNCHANGED
    assert agent.calls == 1
    conflict = await propose_candidate_facts(
        replace(command, projection_hash="0" * 64),
        projection_repository=projection_store, agent=agent,
        agent_metadata=_metadata(), repository=proposal_store,
    )
    assert conflict.status is CandidateFactProposalRunStatus.INTEGRITY_FAILURE
    second_source, second_projection, _, _ = _statement_projection(
        tmp_path, suffix="two", text="Contact email: person@example.test\n"
    )
    second = await propose_candidate_facts(
        _command(second_source, second_projection, "propose-second-source"),
        projection_repository=projection_store,
        agent=_Agent(lambda context: CandidateFactProposalAgentOutput((_block_item(context),))),
        agent_metadata=_metadata(), repository=proposal_store,
    )
    first_match = next(item for item in first.proposals if item.proposed_normalized_value == "person@example.test")
    assert second.proposals[0].proposal_id != first_match.proposal_id


class _StructuredBackend:
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

    def __init__(self, config):
        self.model = "synthetic-model"
        self.calls = 0

    async def complete_structured_request(self, request):
        self.calls += 1
        block = request.input_data["blocks"][0]
        return {
            "proposals": [
                {
                    "field_key": "email",
                    "proposed_value": "person@example.test",
                    "evidence_refs": [
                        {
                            "block_id": block["block_id"],
                            "block_hash": block["block_hash"],
                            "asset_id": None,
                            "asset_hash": None,
                            "source_locator": block["source_locator"],
                        }
                    ],
                    "evidence_excerpt": "person@example.test",
                    "confidence": "HIGH",
                    "extraction_note": "Explicit synthetic evidence.",
                }
            ]
        }


class _TextOnlyBackend(_StructuredBackend):
    capabilities = replace(
        _StructuredBackend.capabilities,
        backend_id="text_only",
        supports_image_input=False,
    )
    native_capabilities = replace(
        _StructuredBackend.native_capabilities,
        backend_id="text_only",
        supports_image_input=False,
    )


@pytest.mark.asyncio
async def test_production_backend_requirements_and_subject_safe_reads(
    tmp_path: Path,
) -> None:
    adapter, metadata = build_production_candidate_fact_proposal_agent(
        ai_config={
            "default_backend": "synthetic_structured",
            "backends": {"synthetic_structured": {}},
            "components": {
                CANDIDATE_FACT_PROPOSAL_COMPONENT_ID: "synthetic_structured"
            },
        },
        backend_registry={"synthetic_structured": _StructuredBackend},
    )
    assert adapter.metadata == metadata
    assert CANDIDATE_FACT_PROPOSAL_COMPONENT_REQUIREMENTS.requires_image_input
    with pytest.raises(ModelBackendResolutionError):
        build_production_candidate_fact_proposal_agent(
            ai_config={
                "default_backend": "text_only",
                "backends": {"text_only": {}},
            },
            backend_registry={"text_only": _TextOnlyBackend},
        )
    source, projection, projection_store, proposal_store = _statement_projection(tmp_path)
    result = await propose_candidate_facts(
        _command(source, projection, "propose-production-adapter"),
        projection_repository=projection_store,
        agent=adapter,
        agent_metadata=metadata,
        repository=proposal_store,
    )
    assert result.status is CandidateFactProposalRunStatus.CREATED
    assert adapter._resolved.backend.calls == 1
    assert get_candidate_fact_proposal(
        "other-subject", result.proposals[0].proposal_id,
        repository=proposal_store,
    ).status is CandidateFactProposalReadStatus.NOT_FOUND
    listed = list_current_candidate_fact_proposals(
        SUBJECT, repository=proposal_store
    )
    assert len(listed.proposals) == 1
    assert not hasattr(listed.proposals[0], "proposed_normalized_value")
    assert all(
        definition.agent_proposal_allowed
        for definition in APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY.values()
    )
    import core.candidate_fact_proposals as module
    source_text = Path(module.__file__).read_text()
    assert "write_candidate_identity_fact" not in source_text
    assert "get_current_candidate_identity_fact" not in source_text
