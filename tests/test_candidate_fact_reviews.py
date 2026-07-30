"""Focused C1d Candidate Fact Review and Verification tests."""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from PIL import Image

from core.application_execution_profile import (
    ApplicationExecutionIdentityFieldKey,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
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
    PrivateHomeCandidateFactProposalRepository,
    ProposeCandidateFactsCommand,
    propose_candidate_facts,
)
from core.candidate_fact_reviews import (
    BuildCandidateFactReviewQueueCommand,
    CandidateFactReviewAction,
    CandidateFactReviewDecisionStatus,
    CandidateFactReviewItemKind,
    PrivateHomeCandidateFactReviewDecisionRepository,
    ResolveCandidateFactReviewCommand,
    ResolveCandidateFactReviewResult,
    build_candidate_fact_review_queue,
    read_candidate_fact_review_asset,
    resolve_candidate_fact_review,
)
from core.candidate_identity_facts import (
    CandidateIdentityFactSourceKind,
    CandidateIdentityFactSourceRef,
    CandidateIdentityFactVerificationStatus,
    GetCurrentCandidateIdentityFactCommand,
    GetCurrentCandidateIdentityFactStatus,
    PrivateHomeCandidateIdentityFactRepository,
    WriteCandidateIdentityFactCommand,
    get_current_candidate_identity_fact,
    write_candidate_identity_fact,
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
from core.private_home import PrivateHome
from dashboard.candidate_fact_reviews import CandidateFactReviewUIController
from dashboard.server import app


NOW = datetime(2026, 7, 29, 22, 0, tzinfo=timezone.utc)
SUBJECT = "subject-c1d-synthetic"


class _Agent:
    def __init__(self, items):
        self._items = items
        self.calls = 0

    async def propose(self, context):
        self.calls += 1
        return CandidateFactProposalAgentOutput(tuple(self._items(context)))


def _metadata():
    return CandidateFactProposalAgentMetadata(
        CANDIDATE_FACT_PROPOSAL_COMPONENT_ID,
        "synthetic-structured",
        "synthetic-model",
        CANDIDATE_FACT_PROPOSAL_AGENT_POLICY_VERSION,
        CANDIDATE_FACT_PROPOSAL_AGENT_SCHEMA_VERSION,
        "model-backend-resolution-" + "a" * 64,
    )


def _repositories(tmp_path: Path):
    home = PrivateHome(tmp_path / "private")
    return (
        PrivateHomeCandidateInformationSourceRepository(home),
        PrivateHomeCandidateSourceProjectionRepository(home),
        PrivateHomeCandidateFactProposalRepository(home),
        PrivateHomeCandidateIdentityFactRepository(home),
        PrivateHomeCandidateFactReviewDecisionRepository(home),
    )


async def _text_proposals(
    tmp_path: Path,
    *,
    field: str = "email",
    values=("person@example.test",),
    suffix="one",
):
    sources, projections, proposals, facts, decisions = _repositories(tmp_path)
    text = " | ".join(values)
    source = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT, f"register-{suffix}", NOW, text.encode()
        ),
        repository=sources,
    ).source
    projection = project_candidate_information_source(
        ProjectCandidateInformationSourceCommand(
            SUBJECT,
            source.source_id,
            source.source_version,
            source.source_identity_hash,
            f"project-{suffix}",
            NOW,
        ),
        source_repository=sources,
        projection_repository=projections,
    ).projection

    def output(context):
        block = context.input_snapshot.selected_blocks[0]
        return [
            CandidateFactProposalAgentItem(
                field,
                value,
                (
                    CandidateFactProposalAgentEvidenceRef(
                        block_id=block.block_id,
                        block_hash=block.block_hash,
                        source_locator=block.source_locator.to_dict(),
                    ),
                ),
                value,
                CandidateFactProposalConfidence.HIGH,
                "Explicit synthetic evidence.",
            )
            for value in values
        ]

    result = await propose_candidate_facts(
        ProposeCandidateFactsCommand(
            SUBJECT,
            source.source_id,
            source.source_version,
            source.source_identity_hash,
            projection.projection_id,
            projection.projection_hash,
            f"propose-{suffix}",
            NOW,
        ),
        projection_repository=projections,
        agent=_Agent(output),
        agent_metadata=_metadata(),
        repository=proposals,
    )
    return result.proposals, proposals, projections, facts, decisions


def _queue(proposals, projections, facts, decisions):
    return build_candidate_fact_review_queue(
        BuildCandidateFactReviewQueueCommand(SUBJECT, NOW),
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=decisions,
    ).queue


def _confirmed(
    facts,
    *,
    value: str,
    expected: str | None,
    invocation: str,
):
    source_hash = hashlib.sha256(invocation.encode()).hexdigest()
    return write_candidate_identity_fact(
        WriteCandidateIdentityFactCommand(
            SUBJECT,
            ApplicationExecutionIdentityFieldKey.EMAIL,
            value,
            CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
            CandidateIdentityFactSourceRef(
                CandidateIdentityFactSourceKind.USER_CONFIRMATION,
                f"source-{invocation}",
                "synthetic-source-v1",
                source_hash,
                f"review:{invocation}",
                SUBJECT,
            ),
            expected,
            invocation,
            NOW,
        ),
        repository=facts,
    )


@pytest.mark.asyncio
async def test_accept_and_edit_create_user_confirmed_facts(tmp_path: Path) -> None:
    produced, proposals, projections, facts, decisions = await _text_proposals(
        tmp_path
    )
    queue = _queue(proposals, projections, facts, decisions)
    item = next(value for value in queue.items if value.proposal)
    accepted = resolve_candidate_fact_review(
        ResolveCandidateFactReviewCommand(
            SUBJECT,
            item.review_item_id,
            queue.queue_snapshot_hash,
            CandidateFactReviewAction.ACCEPT_PROPOSED,
            "review-accept",
            NOW,
        ),
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=decisions,
    )
    current = get_current_candidate_identity_fact(
        GetCurrentCandidateIdentityFactCommand(
            SUBJECT, ApplicationExecutionIdentityFieldKey.EMAIL
        ),
        repository=facts,
    )
    assert accepted.status is CandidateFactReviewDecisionStatus.COMPLETED
    assert current.status is GetCurrentCandidateIdentityFactStatus.FOUND
    assert current.fact.normalized_value == produced[0].proposed_normalized_value
    assert (
        current.fact.source_ref.source_kind
        is CandidateIdentityFactSourceKind.USER_CONFIRMATION
    )

    second, _, _, _, _ = await _text_proposals(
        tmp_path,
        values=("replacement@example.test",),
        suffix="edit",
    )
    queue = _queue(proposals, projections, facts, decisions)
    edit_item = next(
        value
        for value in queue.items
        if value.proposal
        and value.proposal.proposal_id == second[0].proposal_id
    )
    edited = resolve_candidate_fact_review(
        ResolveCandidateFactReviewCommand(
            SUBJECT,
            edit_item.review_item_id,
            queue.queue_snapshot_hash,
            CandidateFactReviewAction.ACCEPT_WITH_EDIT,
            "review-edit",
            NOW,
            "edited@example.test",
        ),
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=decisions,
    )
    assert edited.status is CandidateFactReviewDecisionStatus.COMPLETED
    assert edited.decision.proposal_id == second[0].proposal_id
    assert get_current_candidate_identity_fact(
        GetCurrentCandidateIdentityFactCommand(
            SUBJECT, ApplicationExecutionIdentityFieldKey.EMAIL
        ),
        repository=facts,
    ).fact.normalized_value == "edited@example.test"


class _CountingFacts:
    def __init__(self, inner):
        self.inner = inner
        self.write_calls = 0

    def write(self, command):
        self.write_calls += 1
        return self.inner.write(command)

    def get_current(self, command):
        return self.inner.get_current(command)

    def get_index(self, subject_id):
        return self.inner.get_index(subject_id)


class _DriftAfterClaim:
    def __init__(self, inner, facts, current_id):
        self.inner = inner
        self.facts = facts
        self.current_id = current_id
        self.drifted = False

    def get_invocation(self, *args):
        return self.inner.get_invocation(*args)

    def claim(self, claim, request_hash):
        result = self.inner.claim(claim, request_hash)
        if not self.drifted:
            self.drifted = True
            _confirmed(
                self.facts,
                value="concurrent@example.test",
                expected=self.current_id,
                invocation="concurrent-drift",
            )
        return result

    def complete(self, decision):
        return self.inner.complete(decision)

    def resolved_proposal_ids(self, subject_id):
        return self.inner.resolved_proposal_ids(subject_id)

    def resolved_count(self, subject_id):
        return self.inner.resolved_count(subject_id)


@pytest.mark.asyncio
async def test_conflict_keep_reject_and_stale_replace_use_cas(tmp_path: Path) -> None:
    _, proposals, projections, facts, decisions = await _text_proposals(
        tmp_path,
        values=(
            "one@example.test",
            "two@example.test",
            "three@example.test",
        ),
        suffix="conflict",
    )
    original = _confirmed(
        facts,
        value="old@example.test",
        expected=None,
        invocation="initial-current",
    ).fact
    queue = _queue(proposals, projections, facts, decisions)
    conflict = next(
        item
        for item in queue.items
        if item.item_kind is CandidateFactReviewItemKind.CONFLICTING_PROPOSALS
    )
    assert len(conflict.conflicting_proposals) == 3

    counting = _CountingFacts(facts)
    kept = resolve_candidate_fact_review(
        ResolveCandidateFactReviewCommand(
            SUBJECT,
            conflict.review_item_id,
            queue.queue_snapshot_hash,
            CandidateFactReviewAction.KEEP_CURRENT,
            "review-keep",
            NOW,
        ),
        proposal_repository=proposals,
        current_fact_repository=counting,
        projection_repository=projections,
        decision_repository=decisions,
    )
    assert kept.status is CandidateFactReviewDecisionStatus.COMPLETED
    assert counting.write_calls == 0

    queue = _queue(proposals, projections, facts, decisions)
    rejected_item = next(
        item
        for item in queue.items
        if item.item_kind is CandidateFactReviewItemKind.CONFLICTING_PROPOSALS
    )
    rejected = resolve_candidate_fact_review(
        ResolveCandidateFactReviewCommand(
            SUBJECT,
            rejected_item.review_item_id,
            queue.queue_snapshot_hash,
            CandidateFactReviewAction.REJECT_PROPOSAL,
            "review-reject",
            NOW,
        ),
        proposal_repository=proposals,
        current_fact_repository=counting,
        projection_repository=projections,
        decision_repository=decisions,
    )
    assert rejected.status is CandidateFactReviewDecisionStatus.COMPLETED
    assert counting.write_calls == 0

    queue = _queue(proposals, projections, facts, decisions)
    remaining = next(
        item for item in queue.items if item.proposal is not None
    )
    drift = _DriftAfterClaim(decisions, facts, original.fact_id)
    stale = resolve_candidate_fact_review(
        ResolveCandidateFactReviewCommand(
            SUBJECT,
            remaining.review_item_id,
            queue.queue_snapshot_hash,
            CandidateFactReviewAction.REPLACE_CURRENT,
            "review-stale",
            NOW,
        ),
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=drift,
    )
    assert stale.status is CandidateFactReviewDecisionStatus.STALE_REVIEW
    assert get_current_candidate_identity_fact(
        GetCurrentCandidateIdentityFactCommand(
            SUBJECT, ApplicationExecutionIdentityFieldKey.EMAIL
        ),
        repository=facts,
    ).fact.normalized_value == "concurrent@example.test"


@pytest.mark.asyncio
async def test_missing_field_and_bounded_source_preview(tmp_path: Path) -> None:
    _, proposals, projections, facts, decisions = await _text_proposals(
        tmp_path, field="preferred_name", values=("Synthetic",), suffix="preview"
    )
    queue = _queue(proposals, projections, facts, decisions)
    missing = next(
        item
        for item in queue.items
        if item.field_key is ApplicationExecutionIdentityFieldKey.FIRST_NAME
        and item.item_kind is CandidateFactReviewItemKind.MISSING_REQUIRED_FIELD
    )
    provided = resolve_candidate_fact_review(
        ResolveCandidateFactReviewCommand(
            SUBJECT,
            missing.review_item_id,
            queue.queue_snapshot_hash,
            CandidateFactReviewAction.PROVIDE_MISSING_VALUE,
            "review-missing",
            NOW,
            "Synthetic",
        ),
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=decisions,
    )
    assert provided.status is CandidateFactReviewDecisionStatus.COMPLETED
    proposal_item = next(item for item in queue.items if item.proposal)
    preview = proposal_item.proposal.previews[0]
    assert len(preview.text_excerpt) <= 240
    assert "/private/" not in preview.text_excerpt.casefold()

    sources, _, _, _, _ = _repositories(tmp_path)
    png = io.BytesIO()
    Image.new("RGB", (4, 3), color=(10, 20, 30)).save(png, "PNG")
    image_source = register_candidate_file_source(
        RegisterCandidateFileSourceCommand(
            SUBJECT, "register-review-image", NOW, png.getvalue()
        ),
        repository=sources,
    ).source
    image_projection = project_candidate_information_source(
        ProjectCandidateInformationSourceCommand(
            SUBJECT,
            image_source.source_id,
            image_source.source_version,
            image_source.source_identity_hash,
            "project-review-image",
            NOW,
        ),
        source_repository=sources,
        projection_repository=projections,
    ).projection

    def image_output(context):
        asset = context.input_snapshot.selected_assets[0]
        return [
            CandidateFactProposalAgentItem(
                "last_name",
                "ImageName",
                (
                    CandidateFactProposalAgentEvidenceRef(
                        asset_id=asset.asset_id,
                        asset_hash=asset.asset_hash,
                        source_locator=asset.source_locator.to_dict(),
                    ),
                ),
                "",
                CandidateFactProposalConfidence.MEDIUM,
                "Synthetic image evidence.",
            )
        ]

    await propose_candidate_facts(
        ProposeCandidateFactsCommand(
            SUBJECT,
            image_source.source_id,
            image_source.source_version,
            image_source.source_identity_hash,
            image_projection.projection_id,
            image_projection.projection_hash,
            "propose-review-image",
            NOW,
        ),
        projection_repository=projections,
        agent=_Agent(image_output),
        agent_metadata=_metadata(),
        repository=proposals,
    )
    queue = _queue(proposals, projections, facts, decisions)
    image_item = next(
        item
        for item in queue.items
        if item.proposal
        and any(
            preview.preview_kind.value == "IMAGE"
            for preview in item.proposal.previews
        )
    )
    image_preview = image_item.proposal.previews[0]
    assert read_candidate_fact_review_asset(
        subject_id=SUBJECT,
        review_item_id=image_item.review_item_id,
        evidence_id=image_preview.evidence_id,
        now=NOW,
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=decisions,
    ).content == png.getvalue()
    assert read_candidate_fact_review_asset(
        subject_id="other-subject",
        review_item_id=image_item.review_item_id,
        evidence_id=image_preview.evidence_id,
        now=NOW,
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=decisions,
    ) is None


class _FailReceiptOnce:
    def __init__(self, inner):
        self.inner = inner
        self.failed = False

    def get_invocation(self, *args):
        return self.inner.get_invocation(*args)

    def claim(self, *args):
        return self.inner.claim(*args)

    def complete(self, decision):
        if not self.failed:
            self.failed = True
            return ResolveCandidateFactReviewResult(
                CandidateFactReviewDecisionStatus.FAILED,
                failure_code="SYNTHETIC_RECEIPT_FAILURE",
            )
        return self.inner.complete(decision)

    def resolved_proposal_ids(self, subject_id):
        return self.inner.resolved_proposal_ids(subject_id)

    def resolved_count(self, subject_id):
        return self.inner.resolved_count(subject_id)


@pytest.mark.asyncio
async def test_replay_recovers_receipt_and_route_rejects_subject_binding(
    tmp_path: Path,
) -> None:
    _, proposals, projections, facts, decisions = await _text_proposals(
        tmp_path, suffix="recovery"
    )
    queue = _queue(proposals, projections, facts, decisions)
    item = next(value for value in queue.items if value.proposal)
    command = ResolveCandidateFactReviewCommand(
        SUBJECT,
        item.review_item_id,
        queue.queue_snapshot_hash,
        CandidateFactReviewAction.ACCEPT_PROPOSED,
        "review-recover",
        NOW,
    )
    flaky = _FailReceiptOnce(decisions)
    failed = resolve_candidate_fact_review(
        command,
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=flaky,
    )
    recovered = resolve_candidate_fact_review(
        command,
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=flaky,
    )
    conflict = resolve_candidate_fact_review(
        ResolveCandidateFactReviewCommand(
            SUBJECT,
            item.review_item_id,
            queue.queue_snapshot_hash,
            CandidateFactReviewAction.ACCEPT_WITH_EDIT,
            "review-recover",
            NOW,
            "other@example.test",
        ),
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=flaky,
    )
    assert failed.status is CandidateFactReviewDecisionStatus.FAILED
    assert recovered.status is CandidateFactReviewDecisionStatus.COMPLETED
    assert conflict.status is CandidateFactReviewDecisionStatus.INTEGRITY_FAILURE

    context = AuthenticatedSubjectContext(
        "s" * 24,
        SUBJECT,
        AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        NOW,
        NOW + timedelta(hours=1),
    )
    controller = CandidateFactReviewUIController(
        proposal_repository=proposals,
        current_fact_repository=facts,
        projection_repository=projections,
        decision_repository=decisions,
        clock=lambda: NOW,
    )
    previous_controller = getattr(
        app.state, "candidate_fact_review_controller", None
    )
    previous_dependency = getattr(
        app.state, "authenticated_subject_dependency", None
    )

    async def authenticated(_request):
        return context

    app.state.candidate_fact_review_controller = controller
    app.state.authenticated_subject_dependency = authenticated
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/api/candidate-facts/review/{item.review_item_id}/resolve",
                json={
                    "subject_id": "other-subject",
                    "action": "REJECT_PROPOSAL",
                    "invocation_id": "forbidden-subject",
                    "queue_snapshot_hash": queue.queue_snapshot_hash,
                },
            )
        assert response.status_code == 422
        assert "person@example.test" not in response.text
        assert str(tmp_path) not in response.text
    finally:
        if previous_controller is None:
            app.state.__delattr__("candidate_fact_review_controller")
        else:
            app.state.candidate_fact_review_controller = previous_controller
        if previous_dependency is None:
            app.state.__delattr__("authenticated_subject_dependency")
        else:
            app.state.authenticated_subject_dependency = previous_dependency
