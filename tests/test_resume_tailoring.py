from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import core.resume_tailoring as tailoring_module
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.candidate_evidence import (
    CreateCandidateEvidenceSnapshotCommand,
    PrivateHomeCandidateEvidenceSnapshotRepository,
    create_candidate_evidence_snapshot,
)
from core.job_discovery import JobPosting
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from core.resume_candidates import (
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
    ResumeCandidate,
    ResumeSummarySource,
    ResumeSummaryTrust,
    register_resume_candidate,
)
from core.resume_selection import (
    RESUME_SELECTION_CONTRACT_VERSION,
    PrivateHomeResumeSelectionDecisionRepository,
    ResumeSelectionDecision,
    ResumeSelectionMethod,
)
from core.resume_tailoring import (
    RESUME_TAILORING_AGENT_POLICY,
    RESUME_TAILORING_CONTRACT_VERSION,
    RESUME_TAILORING_POLICY_VERSION,
    PrivateHomeTailoredResumeDraftRepository,
    ResumeTailoringAgentDisposition,
    ResumeTailoringAgentMetadata,
    ResumeTailoringAgentOutput,
    ResumeTailoringAgentUnavailableError,
    ResumeTailoringContext,
    ResumeTailoringFailureReason,
    ResumeTailoringStatus,
    TailorResumeCommand,
    TailoredBulletChangeType,
    TailoredBulletProposal,
    TailoredResumeDraftReadStatus,
    TailoredResumeDraftWriteStatus,
    TailoredSectionProposal,
    tailor_resume,
)
from core.source_resume_projection import (
    CreateSourceResumeProjectionCommand,
    DeterministicSourceResumeParser,
    PrivateHomeSourceResumeArtifactReader,
    PrivateHomeSourceResumeProjectionRepository,
    SourceResumeProjection,
    create_source_resume_projection,
)


NOW = datetime(2026, 7, 28, 21, 0, tzinfo=timezone.utc)
METADATA = ResumeTailoringAgentMetadata(
    agent_version="resume-tailoring-agent-v1",
    prompt_version="resume-tailoring-prompt-v1",
    model_id="synthetic-tailoring-model",
)
JOB_DESCRIPTION = (
    "Requirements: Streamlined geospatial data pipelines in Python. "
    "Responsibilities: deliver reproducible satellite imagery processing "
    "and maintain deterministic workflows."
)
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
    "</Types>"
)


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _home(tmp_path: Path) -> PrivateHome:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    return home


def _docx_bytes() -> bytes:
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Experience</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>Built deterministic geospatial pipelines.</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Streamlined Python processing for 12 satellite datasets.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", document)
    return output.getvalue()


class _JobRepository:
    def __init__(self, job: JobPosting | None) -> None:
        self.job = job

    def get(self, job_id: str) -> JobPosting | None:
        if self.job is not None and self.job.job_id == job_id:
            return self.job
        return None

    def list_current(self) -> tuple[JobPosting, ...]:
        return (self.job,) if self.job is not None else ()


class _FakeTailoringAgent:
    def __init__(self, output) -> None:
        self.output = output
        self.contexts: list[ResumeTailoringContext] = []

    async def tailor(self, context: ResumeTailoringContext):
        self.contexts.append(context)
        if isinstance(self.output, Exception):
            raise self.output
        if callable(self.output):
            return self.output(context)
        return self.output


def _job_for_plan(plan: ApplicationPlan) -> JobPosting:
    return JobPosting(
        schema_version="job-posting-v1",
        job_id=plan.job_id,
        revision=plan.job_revision,
        source_platform="synthetic",
        source_job_id=None,
        source_url="https://jobs.example.com/one",
        company="Synthetic Geospatial Inc",
        title="Geospatial Data Engineer",
        location="Remote",
        work_mode="REMOTE",
        posted_at=None,
        observed_at="2026-07-28T00:00:00Z",
        application_url=None,
        ats_type="unknown",
        description=JOB_DESCRIPTION,
        content_hash=plan.job_content_hash,
        status="ACTIVE",
    )


def _setup(
    tmp_path: Path,
    *,
    subject_id: str = "subject-a",
    user_instructions: str | None = None,
):
    home = _home(tmp_path)
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id="job-one",
        job_revision=1,
        job_content_hash=_hash({"job": "one"}),
        priority_decision_id="priority-decision-one",
        policy_id="policy-one",
        policy_version=1,
        policy_content_hash=_hash({"policy": "one"}),
        accepted_job_intent_id="accepted-intent-one",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
        user_preparation_instructions=user_instructions,
    )
    plan_repository = PrivateHomeApplicationPlanRepository(home)
    plan_repository.save(plan)

    artifact = home.paths.master_documents / f"{subject_id}.docx"
    artifact.write_bytes(_docx_bytes())
    candidate_repository = PrivateHomeResumeCandidateRepository(home)
    registration = register_resume_candidate(
        RegisterResumeCandidateCommand(
            subject_id=subject_id,
            artifact_path=artifact,
            display_name="Synthetic Resume",
            selection_safe_summary="Synthetic summary not used as evidence.",
            summary_source=ResumeSummarySource.AUTHENTICATED_CALLER,
            summary_trust=ResumeSummaryTrust.USER_CONFIRMED,
            now=NOW,
        ),
        home=home,
        repository=candidate_repository,
    )
    candidate = registration.candidate
    assert candidate is not None

    binding = _hash({"selection": "one", "plan": plan.plan_id})
    selection_values = {
        "decision_id": f"resume-selection-{binding}",
        "contract_version": RESUME_SELECTION_CONTRACT_VERSION,
        "selection_binding": binding,
        "subject_id": plan.subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "source_resume_id": candidate.resume_id,
        "source_candidate_version": candidate.contract_version,
        "source_artifact_sha256": candidate.artifact_sha256,
        "candidate_set_hash": _hash({"candidate": candidate.resume_id}),
        "selection_method": ResumeSelectionMethod.ONLY_CANDIDATE.value,
        "rationale": "The only registered candidate was selected.",
        "agent_version": "resume-selection-agent-v1",
        "prompt_version": "resume-selection-prompt-v1",
        "model_id": "none",
    }
    selection = ResumeSelectionDecision(
        decision_content_hash=_hash(selection_values),
        selected_at=NOW,
        **selection_values,
    )
    selection_repository = PrivateHomeResumeSelectionDecisionRepository(home)
    selection_repository.save(selection)

    projection_repository = PrivateHomeSourceResumeProjectionRepository(home)
    projection_result = create_source_resume_projection(
        CreateSourceResumeProjectionCommand(
            subject_id=subject_id,
            resume_id=candidate.resume_id,
            now=NOW,
        ),
        candidate_repository=candidate_repository,
        artifact_reader=PrivateHomeSourceResumeArtifactReader(home),
        parser=DeterministicSourceResumeParser(),
        projection_repository=projection_repository,
    )
    projection = projection_result.projection
    assert projection is not None

    snapshot_repository = PrivateHomeCandidateEvidenceSnapshotRepository(home)
    snapshot_result = create_candidate_evidence_snapshot(
        CreateCandidateEvidenceSnapshotCommand(
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=plan_repository,
        selection_repository=selection_repository,
        candidate_repository=candidate_repository,
        projection_repository=projection_repository,
        snapshot_repository=snapshot_repository,
    )
    snapshot = snapshot_result.snapshot
    assert snapshot is not None

    return {
        "home": home,
        "plan_repository": plan_repository,
        "plan": plan,
        "job_repository": _JobRepository(_job_for_plan(plan)),
        "candidate_repository": candidate_repository,
        "candidate": candidate,
        "selection_repository": selection_repository,
        "selection": selection,
        "projection_repository": projection_repository,
        "projection": projection,
        "snapshot_repository": snapshot_repository,
        "snapshot": snapshot,
        "draft_repository": PrivateHomeTailoredResumeDraftRepository(home),
    }


def _blocks(projection: SourceResumeProjection):
    return tuple(
        (section, block)
        for section in projection.sections
        for block in section.blocks
    )


def _evidence_for_block(snapshot, block_id: str) -> str:
    for item in snapshot.evidence_items:
        if item.source_block_id == block_id:
            return item.evidence_id
    raise AssertionError("no evidence for block")


def _default_output(parts, **overrides) -> ResumeTailoringAgentOutput:
    """Heading unchanged, bullet rewritten with evidence, paragraph unchanged."""

    projection = parts["projection"]
    snapshot = parts["snapshot"]
    section = projection.sections[0]
    heading, bullet, paragraph = section.blocks
    bullet_evidence = _evidence_for_block(snapshot, bullet.block_id)
    paragraph_evidence = _evidence_for_block(snapshot, paragraph.block_id)
    rewritten = TailoredBulletProposal(
        source_section_id=section.section_id,
        source_block_id=bullet.block_id,
        source_bullet_id=bullet.bullet_id,
        change_type=TailoredBulletChangeType.REWRITTEN,
        text=(
            "Built deterministic geospatial pipelines in Python, delivering "
            "reproducible processing for 12 satellite datasets."
        ),
        evidence_ids=(bullet_evidence, paragraph_evidence),
        jd_alignment=("geospatial data pipelines in Python",),
    )
    proposals = {
        "heading": TailoredBulletProposal(
            source_section_id=section.section_id,
            source_block_id=heading.block_id,
            source_bullet_id=heading.bullet_id,
            change_type=TailoredBulletChangeType.UNCHANGED,
            text=heading.text,
            evidence_ids=(),
            jd_alignment=(),
        ),
        "bullet": rewritten,
        "paragraph": TailoredBulletProposal(
            source_section_id=section.section_id,
            source_block_id=paragraph.block_id,
            source_bullet_id=paragraph.bullet_id,
            change_type=TailoredBulletChangeType.UNCHANGED,
            text=paragraph.text,
            evidence_ids=(),
            jd_alignment=(),
        ),
    }
    proposals.update(overrides)
    return ResumeTailoringAgentOutput(
        disposition=ResumeTailoringAgentDisposition.TAILORED,
        sections=(
            TailoredSectionProposal(
                source_section_id=section.section_id,
                order=0,
                bullets=(
                    proposals["heading"],
                    proposals["bullet"],
                    proposals["paragraph"],
                ),
            ),
        ),
        rationale="Bullets were tightened toward the geospatial JD.",
    )


async def _tailor(
    parts,
    agent,
    *,
    subject_id: str = "subject-a",
    snapshot_id: str | None = None,
    now: datetime = NOW,
):
    return await tailor_resume(
        TailorResumeCommand(
            subject_id=subject_id,
            application_plan_id=parts["plan"].plan_id,
            evidence_snapshot_id=(
                snapshot_id or parts["snapshot"].snapshot_id
            ),
            now=now,
        ),
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        selection_repository=parts["selection_repository"],
        candidate_repository=parts["candidate_repository"],
        projection_repository=parts["projection_repository"],
        evidence_snapshot_repository=parts["snapshot_repository"],
        agent=agent,
        metadata=METADATA,
        draft_repository=parts["draft_repository"],
    )


def _draft_files(parts) -> tuple[Path, ...]:
    return tuple(
        parts["home"].paths.tailored_resume_drafts.rglob("*.json")
    )


@pytest.mark.asyncio
async def test_consistent_binding_creates_typed_immutable_draft(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeTailoringAgent(_default_output(parts))

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.CREATED
    draft = result.draft
    assert draft is not None
    assert draft.contract_version == RESUME_TAILORING_CONTRACT_VERSION
    assert draft.subject_id == "subject-a"
    assert draft.application_plan_id == parts["plan"].plan_id
    assert draft.job_id == parts["plan"].job_id
    assert draft.job_revision == parts["plan"].job_revision
    assert draft.job_content_hash == parts["plan"].job_content_hash
    assert (
        draft.resume_selection_decision_id
        == parts["selection"].decision_id
    )
    assert draft.source_resume_id == parts["candidate"].resume_id
    assert (
        draft.source_artifact_sha256 == parts["candidate"].artifact_sha256
    )
    assert draft.source_projection_id == parts["projection"].projection_id
    assert (
        draft.source_projection_hash
        == parts["projection"].projection_content_hash
    )
    assert draft.evidence_snapshot_id == parts["snapshot"].snapshot_id
    assert (
        draft.evidence_snapshot_hash
        == parts["snapshot"].snapshot_content_hash
    )
    assert (
        draft.user_preparation_instructions_hash
        == parts["plan"].user_preparation_instructions_hash
    )
    assert draft.agent_version == METADATA.agent_version
    assert draft.prompt_version == METADATA.prompt_version
    assert draft.model_id == METADATA.model_id
    assert draft.agent_policy_version == RESUME_TAILORING_POLICY_VERSION
    assert draft.draft_id == f"tailored-resume-draft-{draft.tailoring_binding}"
    assert draft.created_at == NOW
    section = draft.sections[0]
    assert section.title == "Experience"
    changes = [bullet.change_type for bullet in section.bullets]
    assert changes == [
        TailoredBulletChangeType.UNCHANGED,
        TailoredBulletChangeType.REWRITTEN,
        TailoredBulletChangeType.UNCHANGED,
    ]
    rewritten = section.bullets[1]
    assert rewritten.evidence_ids
    assert rewritten.jd_alignment == (
        "geospatial data pipelines in Python",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    ["plan_subject", "job", "selection", "projection", "snapshot"],
)
async def test_binding_mismatch_fails_closed_without_agent_call(
    tmp_path: Path, damage: str
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeTailoringAgent(_default_output(parts))
    subject_id = "subject-a"
    snapshot_id = parts["snapshot"].snapshot_id
    expected: ResumeTailoringFailureReason
    if damage == "plan_subject":
        subject_id = "subject-b"
        expected = (
            ResumeTailoringFailureReason.APPLICATION_PLAN_SUBJECT_MISMATCH
        )
    elif damage == "job":
        job = parts["job_repository"].job
        parts["job_repository"] = _JobRepository(
            JobPosting(**{**job.to_dict(), "content_hash": "f" * 64})
        )
        expected = ResumeTailoringFailureReason.JOB_BINDING_MISMATCH
    elif damage == "selection":
        object.__setattr__(parts["selection"], "job_content_hash", "f" * 64)

        class _Damaged:
            def __init__(self, decision) -> None:
                self.decision = decision

            def find_current_for_plan(self, **_kwargs):
                from core.resume_selection import (
                    ResumeSelectionDecisionReadResult,
                    ResumeSelectionDecisionReadStatus,
                )

                return ResumeSelectionDecisionReadResult(
                    status=ResumeSelectionDecisionReadStatus.FOUND,
                    decision=self.decision,
                )

        parts["selection_repository"] = _Damaged(parts["selection"])
        expected = (
            ResumeTailoringFailureReason.RESUME_SELECTION_BINDING_MISMATCH
        )
    elif damage == "projection":
        object.__setattr__(
            parts["projection"], "artifact_sha256", "f" * 64
        )

        class _DamagedProjection:
            def __init__(self, projection) -> None:
                self.projection = projection

            def find_current_for_resume(self, **_kwargs):
                from core.source_resume_projection import (
                    SourceResumeProjectionReadResult,
                    SourceResumeProjectionReadStatus,
                )

                return SourceResumeProjectionReadResult(
                    status=SourceResumeProjectionReadStatus.FOUND,
                    projection=self.projection,
                )

        parts["projection_repository"] = _DamagedProjection(
            parts["projection"]
        )
        expected = (
            ResumeTailoringFailureReason.SOURCE_PROJECTION_BINDING_MISMATCH
        )
    else:
        object.__setattr__(
            parts["snapshot"],
            "application_plan_id",
            "application-plan-" + "0" * 64,
        )

        class _DamagedSnapshot:
            def __init__(self, snapshot) -> None:
                self.snapshot = snapshot

            def get(self, **_kwargs):
                from core.candidate_evidence import (
                    CandidateEvidenceSnapshotReadResult,
                    CandidateEvidenceSnapshotReadStatus,
                )

                return CandidateEvidenceSnapshotReadResult(
                    status=CandidateEvidenceSnapshotReadStatus.FOUND,
                    snapshot=self.snapshot,
                )

        parts["snapshot_repository"] = _DamagedSnapshot(parts["snapshot"])
        expected = (
            ResumeTailoringFailureReason.EVIDENCE_SNAPSHOT_BINDING_MISMATCH
        )

    result = await _tailor(
        parts, agent, subject_id=subject_id, snapshot_id=snapshot_id
    )

    assert result.status is ResumeTailoringStatus.FAILED
    assert result.reason_code is expected
    assert agent.contexts == []
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_agent_receives_only_bound_inputs_and_static_policy(
    tmp_path: Path,
) -> None:
    instructions = "Emphasize reproducible processing work."
    parts = _setup(tmp_path, user_instructions=instructions)
    agent = _FakeTailoringAgent(_default_output(parts))

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.CREATED
    assert len(agent.contexts) == 1
    context = agent.contexts[0]
    assert context.subject_id == "subject-a"
    assert context.application_plan_id == parts["plan"].plan_id
    assert context.job.description == JOB_DESCRIPTION
    assert context.job.content_hash == parts["plan"].job_content_hash
    assert context.source_projection == parts["projection"]
    assert tuple(
        item.evidence_id for item in context.evidence_items
    ) == tuple(
        item.evidence_id for item in parts["snapshot"].evidence_items
    )
    assert tuple(
        item.evidence_text for item in context.evidence_items
    ) == tuple(
        item.evidence_text for item in parts["snapshot"].evidence_items
    )
    assert context.user_preparation_instructions == instructions
    assert context.agent_policy == RESUME_TAILORING_AGENT_POLICY
    assert context.agent_policy_version == RESUME_TAILORING_POLICY_VERSION
    assert "Action Verb + Details + Outcome" in context.agent_policy
    summary = parts["candidate"].selection_safe_summary
    assert all(
        summary not in item.evidence_text
        for item in context.evidence_items
    )


@pytest.mark.asyncio
async def test_completed_binding_replays_unchanged_with_zero_agent_calls(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeTailoringAgent(_default_output(parts))

    first = await _tailor(parts, agent)
    replay = await _tailor(parts, agent, now=NOW + timedelta(days=2))

    assert first.status is ResumeTailoringStatus.CREATED
    assert replay.status is ResumeTailoringStatus.UNCHANGED
    assert replay.draft == first.draft
    assert replay.draft.created_at == NOW
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
async def test_restart_reads_identical_draft_identity_and_hash(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeTailoringAgent(_default_output(parts))
    first = await _tailor(parts, agent)
    assert first.draft is not None

    restarted = PrivateHomeTailoredResumeDraftRepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(
        subject_id="subject-a", draft_id=first.draft.draft_id
    )

    assert read.status is TailoredResumeDraftReadStatus.FOUND
    assert read.draft == first.draft
    assert (
        read.draft.draft_content_hash == first.draft.draft_content_hash
    )
    cross = restarted.get(
        subject_id="subject-b", draft_id=first.draft.draft_id
    )
    assert cross.status is TailoredResumeDraftReadStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_jd_verb_without_evidence_support_needs_human(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    section = parts["projection"].sections[0]
    bullet = section.blocks[1]
    evidence_id = _evidence_for_block(parts["snapshot"], bullet.block_id)
    # "maintain" appears in the JD but in no cited evidence text.
    unsupported = TailoredBulletProposal(
        source_section_id=section.section_id,
        source_block_id=bullet.block_id,
        source_bullet_id=bullet.bullet_id,
        change_type=TailoredBulletChangeType.REWRITTEN,
        text="maintain deterministic geospatial pipelines.",
        evidence_ids=(evidence_id,),
        jd_alignment=("maintain deterministic workflows",),
    )
    agent = _FakeTailoringAgent(
        _default_output(parts, bullet=unsupported)
    )

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is ResumeTailoringFailureReason.AGENT_OUTPUT_UNSAFE
    )
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_supported_jd_verb_with_evidence_is_accepted(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    section = parts["projection"].sections[0]
    bullet = section.blocks[1]
    paragraph = section.blocks[2]
    # "Streamlined" appears in the JD and in the cited paragraph evidence.
    supported = TailoredBulletProposal(
        source_section_id=section.section_id,
        source_block_id=bullet.block_id,
        source_bullet_id=bullet.bullet_id,
        change_type=TailoredBulletChangeType.REWRITTEN,
        text="Streamlined deterministic geospatial pipelines.",
        evidence_ids=(
            _evidence_for_block(parts["snapshot"], bullet.block_id),
            _evidence_for_block(parts["snapshot"], paragraph.block_id),
        ),
        jd_alignment=("Streamlined geospatial data pipelines",),
    )
    agent = _FakeTailoringAgent(
        _default_output(parts, bullet=supported)
    )

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.CREATED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "detailed"),
    [
        (
            "Built pipelines processing 400 satellite scenes.",
            "unevidenced number",
        ),
        (
            "Built pipelines with Kubernetes orchestration.",
            "unevidenced tool",
        ),
        (
            "helped build deterministic geospatial pipelines.",
            "weak leading verb",
        ),
    ],
)
async def test_unevidenced_facts_and_weak_verbs_are_rejected(
    tmp_path: Path, text: str, detailed: str
) -> None:
    parts = _setup(tmp_path)
    section = parts["projection"].sections[0]
    bullet = section.blocks[1]
    proposal = TailoredBulletProposal(
        source_section_id=section.section_id,
        source_block_id=bullet.block_id,
        source_bullet_id=bullet.bullet_id,
        change_type=TailoredBulletChangeType.REWRITTEN,
        text=text,
        evidence_ids=(
            _evidence_for_block(parts["snapshot"], bullet.block_id),
        ),
        jd_alignment=("geospatial data pipelines in Python",),
    )
    agent = _FakeTailoringAgent(_default_output(parts, bullet=proposal))

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN, detailed
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_evidenced_number_and_tool_are_accepted(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    result = await _tailor(
        parts, _FakeTailoringAgent(_default_output(parts))
    )

    assert result.status is ResumeTailoringStatus.CREATED
    rewritten = result.draft.sections[0].bullets[1]
    assert "Python" in rewritten.text
    assert "12" in rewritten.text


@pytest.mark.asyncio
async def test_user_required_content_cannot_be_silently_omitted(
    tmp_path: Path,
) -> None:
    keep_text = "Built deterministic geospatial pipelines."
    parts = _setup(
        tmp_path,
        user_instructions=f"Keep this bullet exactly: {keep_text}",
    )
    section = parts["projection"].sections[0]
    bullet = section.blocks[1]
    omitted = TailoredBulletProposal(
        source_section_id=section.section_id,
        source_block_id=bullet.block_id,
        source_bullet_id=bullet.bullet_id,
        change_type=TailoredBulletChangeType.OMITTED,
        text=None,
        evidence_ids=(),
        jd_alignment=(),
    )
    agent = _FakeTailoringAgent(_default_output(parts, bullet=omitted))

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_omitting_unprotected_content_is_recorded_not_dropped(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    section = parts["projection"].sections[0]
    paragraph = section.blocks[2]
    omitted = TailoredBulletProposal(
        source_section_id=section.section_id,
        source_block_id=paragraph.block_id,
        source_bullet_id=paragraph.bullet_id,
        change_type=TailoredBulletChangeType.OMITTED,
        text=None,
        evidence_ids=(),
        jd_alignment=(),
    )
    agent = _FakeTailoringAgent(
        _default_output(parts, paragraph=omitted)
    )

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.CREATED
    recorded = result.draft.sections[0].bullets[2]
    assert recorded.change_type is TailoredBulletChangeType.OMITTED
    assert recorded.text is None
    assert recorded.source_block_id == paragraph.block_id


@pytest.mark.asyncio
async def test_insufficient_evidence_disposition_defers_without_draft(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeTailoringAgent(
        ResumeTailoringAgentOutput(
            disposition=(
                ResumeTailoringAgentDisposition.INSUFFICIENT_EVIDENCE
            ),
            sections=(),
            rationale="The evidence does not cover the JD focus areas.",
        )
    )

    result = await _tailor(parts, agent)

    assert (
        result.status
        is ResumeTailoringStatus.DEFERRED_INSUFFICIENT_EVIDENCE
    )
    assert result.draft is None
    assert not result.retryable
    assert not _draft_files(parts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["untyped", "unknown_evidence", "unknown_block", "missing_block"],
)
async def test_illegal_agent_output_defers_needs_human(
    tmp_path: Path, corruption: str
) -> None:
    parts = _setup(tmp_path)
    section = parts["projection"].sections[0]
    bullet = section.blocks[1]
    if corruption == "untyped":
        agent = _FakeTailoringAgent(
            lambda _context: {"sections": "free text"}
        )
    elif corruption == "unknown_evidence":
        unknown = TailoredBulletProposal(
            source_section_id=section.section_id,
            source_block_id=bullet.block_id,
            source_bullet_id=bullet.bullet_id,
            change_type=TailoredBulletChangeType.REWRITTEN,
            text="Built deterministic geospatial pipelines.",
            evidence_ids=("candidate-evidence-" + "0" * 64,),
            jd_alignment=("geospatial data pipelines in Python",),
        )
        agent = _FakeTailoringAgent(
            _default_output(parts, bullet=unknown)
        )
    elif corruption == "unknown_block":
        moved = TailoredBulletProposal(
            source_section_id=section.section_id,
            source_block_id="resume-block-" + "0" * 64,
            source_bullet_id=None,
            change_type=TailoredBulletChangeType.UNCHANGED,
            text="Fabricated block.",
            evidence_ids=(),
            jd_alignment=(),
        )
        agent = _FakeTailoringAgent(
            _default_output(parts, paragraph=moved)
        )
    else:
        base = _default_output(parts)
        truncated = ResumeTailoringAgentOutput(
            disposition=ResumeTailoringAgentDisposition.TAILORED,
            sections=(
                TailoredSectionProposal(
                    source_section_id=section.section_id,
                    order=0,
                    bullets=base.sections[0].bullets[:2],
                ),
            ),
            rationale=base.rationale,
        )
        agent = _FakeTailoringAgent(truncated)

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is ResumeTailoringFailureReason.AGENT_OUTPUT_UNSAFE
    )
    assert not result.retryable
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_agent_unavailable_fails_without_auto_retry(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeTailoringAgent(
        ResumeTailoringAgentUnavailableError("provider offline")
    )

    result = await _tailor(parts, agent)

    assert result.status is ResumeTailoringStatus.FAILED
    assert (
        result.reason_code
        is ResumeTailoringFailureReason.AGENT_UNAVAILABLE
    )
    assert result.retryable
    assert len(agent.contexts) == 1
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_repository_conflict_and_corruption_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeTailoringAgent(_default_output(parts))
    first = await _tailor(parts, agent)
    draft = first.draft
    assert draft is not None
    record = next(
        parts["home"].paths.tailored_resume_drafts.rglob(
            f"{draft.draft_id}.json"
        )
    )
    corrupted = b"{broken"
    record.write_bytes(corrupted)

    conflict = parts["draft_repository"].save(draft)
    read = parts["draft_repository"].get(
        subject_id=draft.subject_id, draft_id=draft.draft_id
    )

    assert conflict.status is TailoredResumeDraftWriteStatus.FAILED
    assert (
        conflict.reason_code
        is ResumeTailoringFailureReason.DRAFT_INTEGRITY_FAILURE
    )
    assert record.read_bytes() == corrupted
    assert read.status is TailoredResumeDraftReadStatus.INTEGRITY_FAILURE


@pytest.mark.asyncio
async def test_invalid_command_fails_without_side_effects(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeTailoringAgent(_default_output(parts))

    naive = await _tailor(
        parts, agent, now=datetime(2026, 7, 28, 21, 0)
    )
    missing = await tailor_resume(
        TailorResumeCommand(
            subject_id="subject-a",
            application_plan_id="application-plan-" + "0" * 64,
            evidence_snapshot_id=parts["snapshot"].snapshot_id,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        selection_repository=parts["selection_repository"],
        candidate_repository=parts["candidate_repository"],
        projection_repository=parts["projection_repository"],
        evidence_snapshot_repository=parts["snapshot_repository"],
        agent=agent,
        metadata=METADATA,
        draft_repository=parts["draft_repository"],
    )

    assert naive.reason_code is ResumeTailoringFailureReason.INVALID_REQUEST
    assert (
        missing.reason_code
        is ResumeTailoringFailureReason.APPLICATION_PLAN_NOT_FOUND
    )
    assert agent.contexts == []
    assert not _draft_files(parts)


def test_module_has_no_rendering_qa_browser_or_execution_dependency() -> None:
    module_path = Path(tailoring_module.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = {
        "core.application_engine",
        "core.browser_broker",
        "core.materials",
        "core.profile_store",
        "core.priority_agent_adapter",
        "playwright",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
