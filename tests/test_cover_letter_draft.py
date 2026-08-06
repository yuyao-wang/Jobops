from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import core.cover_letter_draft as draft_module
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.cover_letter_draft import (
    COVER_LETTER_DRAFT_AGENT_POLICY,
    COVER_LETTER_DRAFT_CONTRACT_VERSION,
    COVER_LETTER_DRAFT_POLICY_VERSION,
    CoverLetterAgentContext,
    CoverLetterAgentMetadata,
    CoverLetterAgentOutput,
    CoverLetterAgentUnavailableError,
    CoverLetterDraftFailureReason,
    CoverLetterDraftReadStatus,
    CoverLetterDraftStatus,
    CoverLetterParagraphProposal,
    CoverLetterParagraphPurpose,
    DraftCoverLetterCommand,
    PrivateHomeCoverLetterDraftRepository,
    draft_cover_letter,
)
from core.cover_letter_evidence import (
    CreateCoverLetterEvidenceSnapshotCommand,
    PrivateHomeCoverLetterEvidenceSnapshotRepository,
    create_cover_letter_evidence_snapshot,
)
from core.job_discovery import JobPosting
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from core.resume_candidates import (
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
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
from core.source_resume_projection import (
    CreateSourceResumeProjectionCommand,
    DeterministicSourceResumeParser,
    PrivateHomeSourceResumeArtifactReader,
    PrivateHomeSourceResumeProjectionRepository,
    create_source_resume_projection,
)


NOW = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
METADATA = CoverLetterAgentMetadata(
    agent_version="cover-letter-draft-agent-v1",
    prompt_version="cover-letter-draft-prompt-v1",
    model_id="synthetic-cover-letter-model",
)
JOB_DESCRIPTION = (
    "Requirements: Streamlined geospatial data pipelines in Python. "
    "Responsibilities: deliver reproducible satellite imagery processing "
    "and maintain deterministic workflows at Example Robotics."
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


class _FakeCoverLetterAgent:
    def __init__(self, output=None) -> None:
        self.output = output
        self.contexts: list[CoverLetterAgentContext] = []

    async def generate(self, context: CoverLetterAgentContext):
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
        company="Example Robotics",
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
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
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

    snapshot_repository = PrivateHomeCoverLetterEvidenceSnapshotRepository(
        home
    )
    snapshot_result = create_cover_letter_evidence_snapshot(
        CreateCoverLetterEvidenceSnapshotCommand(
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
        "candidate": candidate,
        "snapshot_repository": snapshot_repository,
        "snapshot": snapshot,
        "draft_repository": PrivateHomeCoverLetterDraftRepository(home),
    }


def _evidence_for_block_containing(snapshot, text: str) -> str:
    for item in snapshot.evidence_items:
        if text in item.evidence_text:
            return item.evidence_id
    raise AssertionError(f"no evidence containing {text!r}")


def _default_output(parts, **overrides) -> CoverLetterAgentOutput:
    """A well-behaved letter: evidenced qualification, JD-aligned motivation."""

    snapshot = parts["snapshot"]
    qualification_evidence = _evidence_for_block_containing(
        snapshot, "geospatial pipelines"
    )
    motivation_evidence = _evidence_for_block_containing(
        snapshot, "satellite datasets"
    )
    proposals = {
        "introduction": CoverLetterParagraphProposal(
            purpose=CoverLetterParagraphPurpose.INTRODUCTION,
            text=(
                "I am writing to apply for the Geospatial Data Engineer "
                "role at Example Robotics."
            ),
            evidence_ids=(),
            jd_alignment=(),
        ),
        "qualification": CoverLetterParagraphProposal(
            purpose=CoverLetterParagraphPurpose.QUALIFICATION,
            text=(
                "Built deterministic geospatial pipelines, delivering "
                "reproducible processing for 12 satellite datasets."
            ),
            evidence_ids=(qualification_evidence, motivation_evidence),
            jd_alignment=(
                "Streamlined geospatial data pipelines in Python",
            ),
        ),
        "motivation": CoverLetterParagraphProposal(
            purpose=CoverLetterParagraphPurpose.MOTIVATION,
            text=(
                "This pipeline work aligns directly with delivering "
                "reproducible satellite imagery processing."
            ),
            evidence_ids=(motivation_evidence,),
            jd_alignment=(
                "deliver reproducible satellite imagery processing",
            ),
        ),
        "closing": CoverLetterParagraphProposal(
            purpose=CoverLetterParagraphPurpose.CLOSING,
            text=(
                "Thank you for considering my application; I look forward "
                "to discussing the role further."
            ),
            evidence_ids=(),
            jd_alignment=(),
        ),
    }
    proposals.update(overrides)
    return CoverLetterAgentOutput(
        greeting="Dear Hiring Team,",
        paragraphs=(
            proposals["introduction"],
            proposals["qualification"],
            proposals["motivation"],
            proposals["closing"],
        ),
        closing="Sincerely,\nSynthetic Candidate",
        rationale="Selected the two evidence items most aligned to the JD.",
    )


async def _draft(
    parts,
    agent,
    *,
    subject_id: str = "subject-a",
    snapshot_id: str | None = None,
    now: datetime = NOW,
):
    return await draft_cover_letter(
        DraftCoverLetterCommand(
            subject_id=subject_id,
            application_plan_id=parts["plan"].plan_id,
            cover_letter_evidence_snapshot_id=(
                snapshot_id or parts["snapshot"].snapshot_id
            ),
            now=now,
        ),
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        evidence_snapshot_repository=parts["snapshot_repository"],
        agent=agent,
        metadata=METADATA,
        draft_repository=parts["draft_repository"],
    )


def _draft_files(parts) -> tuple[Path, ...]:
    return tuple(parts["home"].paths.cover_letter_drafts.rglob("*.json"))


@pytest.mark.asyncio
async def test_consistent_binding_creates_typed_immutable_draft(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeCoverLetterAgent(_default_output(parts))

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.CREATED
    draft = result.draft
    assert draft is not None
    assert draft.contract_version == COVER_LETTER_DRAFT_CONTRACT_VERSION
    assert draft.subject_id == "subject-a"
    assert draft.application_plan_id == parts["plan"].plan_id
    assert draft.job_id == parts["plan"].job_id
    assert draft.job_revision == parts["plan"].job_revision
    assert draft.job_content_hash == parts["plan"].job_content_hash
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
    assert draft.agent_policy_version == COVER_LETTER_DRAFT_POLICY_VERSION
    assert draft.draft_id == f"cover-letter-draft-{draft.draft_binding}"
    assert draft.created_at == NOW
    purposes = [item.purpose for item in draft.paragraphs]
    assert purposes == [
        CoverLetterParagraphPurpose.INTRODUCTION,
        CoverLetterParagraphPurpose.QUALIFICATION,
        CoverLetterParagraphPurpose.MOTIVATION,
        CoverLetterParagraphPurpose.CLOSING,
    ]
    qualification = draft.paragraphs[1]
    assert qualification.evidence_ids
    assert qualification.jd_alignment == (
        "Streamlined geospatial data pipelines in Python",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    ["plan_subject", "job", "snapshot"],
)
async def test_binding_mismatch_fails_closed_without_agent_call(
    tmp_path: Path, damage: str
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeCoverLetterAgent(_default_output(parts))
    subject_id = "subject-a"
    snapshot_id = parts["snapshot"].snapshot_id
    if damage == "plan_subject":
        subject_id = "subject-b"
        expected = (
            CoverLetterDraftFailureReason.APPLICATION_PLAN_SUBJECT_MISMATCH
        )
    elif damage == "job":
        job = parts["job_repository"].job
        parts["job_repository"] = _JobRepository(
            JobPosting(**{**job.to_dict(), "content_hash": "f" * 64})
        )
        expected = CoverLetterDraftFailureReason.JOB_BINDING_MISMATCH
    else:
        object.__setattr__(
            parts["snapshot"], "application_plan_id",
            "application-plan-" + "0" * 64,
        )

        class _DamagedSnapshot:
            def __init__(self, snapshot) -> None:
                self.snapshot = snapshot

            def get(self, **_kwargs):
                from core.cover_letter_evidence import (
                    CoverLetterEvidenceSnapshotReadResult,
                    CoverLetterEvidenceSnapshotReadStatus,
                )

                return CoverLetterEvidenceSnapshotReadResult(
                    status=CoverLetterEvidenceSnapshotReadStatus.FOUND,
                    snapshot=self.snapshot,
                )

        parts["snapshot_repository"] = _DamagedSnapshot(parts["snapshot"])
        expected = (
            CoverLetterDraftFailureReason.EVIDENCE_SNAPSHOT_BINDING_MISMATCH
        )

    result = await _draft(
        parts, agent, subject_id=subject_id, snapshot_id=snapshot_id
    )

    assert result.status is CoverLetterDraftStatus.FAILED
    assert result.reason_code is expected
    assert agent.contexts == []
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_agent_receives_only_bound_inputs_and_static_policy(
    tmp_path: Path,
) -> None:
    instructions = "Keep the tone warm but professional."
    parts = _setup(tmp_path, user_instructions=instructions)
    agent = _FakeCoverLetterAgent(_default_output(parts))

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.CREATED
    assert len(agent.contexts) == 1
    context = agent.contexts[0]
    assert context.subject_id == "subject-a"
    assert context.application_plan_id == parts["plan"].plan_id
    assert context.job.description == JOB_DESCRIPTION
    assert context.job.content_hash == parts["plan"].job_content_hash
    assert tuple(
        item.evidence_id for item in context.evidence_items
    ) == tuple(
        item.evidence_id
        for item in parts["snapshot"].evidence_items
    )
    assert context.user_preparation_instructions == instructions
    assert context.agent_policy == COVER_LETTER_DRAFT_AGENT_POLICY
    assert context.agent_policy_version == COVER_LETTER_DRAFT_POLICY_VERSION
    assert "fabricate" in context.agent_policy.lower()
    summary = parts["candidate"].selection_safe_summary
    assert all(
        summary not in item.evidence_text for item in context.evidence_items
    )


@pytest.mark.asyncio
async def test_completed_binding_replays_unchanged_with_zero_agent_calls(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeCoverLetterAgent(_default_output(parts))

    first = await _draft(parts, agent)
    replay = await _draft(parts, agent, now=NOW + timedelta(days=2))

    assert first.status is CoverLetterDraftStatus.CREATED
    assert replay.status is CoverLetterDraftStatus.UNCHANGED
    assert replay.draft == first.draft
    assert replay.draft.created_at == NOW
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
async def test_restart_reads_identical_draft_identity_and_hash(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeCoverLetterAgent(_default_output(parts))
    first = await _draft(parts, agent)
    assert first.draft is not None

    restarted = PrivateHomeCoverLetterDraftRepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(subject_id="subject-a", draft_id=first.draft.draft_id)

    assert read.status is CoverLetterDraftReadStatus.FOUND
    assert read.draft == first.draft
    assert read.draft.draft_content_hash == first.draft.draft_content_hash
    cross = restarted.get(
        subject_id="subject-b", draft_id=first.draft.draft_id
    )
    assert cross.status is CoverLetterDraftReadStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_unevidenced_qualification_claim_needs_human(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    evidence_id = _evidence_for_block_containing(
        parts["snapshot"], "geospatial pipelines"
    )
    unsupported = CoverLetterParagraphProposal(
        purpose=CoverLetterParagraphPurpose.QUALIFICATION,
        text="Led enterprise Kubernetes migrations across 40 clusters.",
        evidence_ids=(evidence_id,),
        jd_alignment=("Streamlined geospatial data pipelines in Python",),
    )
    agent = _FakeCoverLetterAgent(
        _default_output(parts, qualification=unsupported)
    )

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code is CoverLetterDraftFailureReason.AGENT_OUTPUT_UNSAFE
    )
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_jd_requirement_cannot_be_used_as_candidate_fact_evidence(
    tmp_path: Path,
) -> None:
    """The JD states a requirement; it must never prove the candidate has it."""

    parts = _setup(tmp_path)
    evidence_id = _evidence_for_block_containing(
        parts["snapshot"], "geospatial pipelines"
    )
    jd_as_fact = CoverLetterParagraphProposal(
        purpose=CoverLetterParagraphPurpose.QUALIFICATION,
        text="Streamlined geospatial data pipelines in Python for years.",
        evidence_ids=(evidence_id,),
        jd_alignment=("Streamlined geospatial data pipelines in Python",),
    )
    agent = _FakeCoverLetterAgent(
        _default_output(parts, qualification=jd_as_fact)
    )

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN


@pytest.mark.asyncio
async def test_first_person_pronoun_is_not_treated_as_a_candidate_fact(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    evidence_id = _evidence_for_block_containing(
        parts["snapshot"], "geospatial pipelines"
    )
    qualification = CoverLetterParagraphProposal(
        purpose=CoverLetterParagraphPurpose.QUALIFICATION,
        text="Through this work, I built deterministic geospatial pipelines.",
        evidence_ids=(evidence_id,),
        jd_alignment=("Streamlined geospatial data pipelines in Python",),
    )
    agent = _FakeCoverLetterAgent(
        _default_output(parts, qualification=qualification)
    )

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.CREATED


@pytest.mark.asyncio
async def test_motivation_may_reference_capitalized_jd_terms(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    evidence_id = _evidence_for_block_containing(
        parts["snapshot"], "satellite datasets"
    )
    motivation = CoverLetterParagraphProposal(
        purpose=CoverLetterParagraphPurpose.MOTIVATION,
        text=(
            "This experience motivates my interest in the Geospatial Data "
            "Engineer role."
        ),
        evidence_ids=(evidence_id,),
        jd_alignment=("deliver reproducible satellite imagery processing",),
    )
    agent = _FakeCoverLetterAgent(
        _default_output(parts, motivation=motivation)
    )

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.CREATED


@pytest.mark.asyncio
async def test_motivation_may_begin_with_a_job_context_term(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    evidence_id = _evidence_for_block_containing(
        parts["snapshot"], "satellite datasets"
    )
    motivation = CoverLetterParagraphProposal(
        purpose=CoverLetterParagraphPurpose.MOTIVATION,
        text="Geospatial work is the focus that draws me to this role.",
        evidence_ids=(evidence_id,),
        jd_alignment=("deliver reproducible satellite imagery processing",),
    )
    agent = _FakeCoverLetterAgent(
        _default_output(parts, motivation=motivation)
    )

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.CREATED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["greeting", "closing", "paragraph"],
)
async def test_placeholders_are_rejected(tmp_path: Path, field: str) -> None:
    parts = _setup(tmp_path)
    overrides = {}
    kwargs = {}
    if field == "greeting":
        kwargs["greeting"] = "Dear [Hiring Manager],"
    elif field == "closing":
        kwargs["closing"] = "Sincerely,\n[Your Name]"
    else:
        base = _default_output(parts)
        placeholder_intro = CoverLetterParagraphProposal(
            purpose=CoverLetterParagraphPurpose.INTRODUCTION,
            text="I am excited to join [Company] as a data engineer.",
            evidence_ids=(),
            jd_alignment=(),
        )
        overrides["introduction"] = placeholder_intro

    output = _default_output(parts, **overrides)
    if kwargs:
        output = CoverLetterAgentOutput(
            greeting=kwargs.get("greeting", output.greeting),
            paragraphs=output.paragraphs,
            closing=kwargs.get("closing", output.closing),
            rationale=output.rationale,
        )
    agent = _FakeCoverLetterAgent(output)

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_insufficient_evidence_defers_without_agent_call(
    tmp_path: Path,
) -> None:
    """A snapshot with no COVER_LETTER-scoped items defers before the Agent.

    ``CoverLetterEvidenceSnapshot`` requires a non-empty ``evidence_items``
    tuple by contract, so an empty tuple is set directly on the already
    validated instance (post_init has already run) rather than constructed
    through the public API, purely to exercise this one filtering branch.
    """

    parts = _setup(tmp_path)
    agent = _FakeCoverLetterAgent(_default_output(parts))
    object.__setattr__(parts["snapshot"], "evidence_items", ())

    class _PassThrough:
        def get(self, **_kwargs):
            from core.cover_letter_evidence import (
                CoverLetterEvidenceSnapshotReadResult,
                CoverLetterEvidenceSnapshotReadStatus,
            )

            return CoverLetterEvidenceSnapshotReadResult(
                status=CoverLetterEvidenceSnapshotReadStatus.FOUND,
                snapshot=parts["snapshot"],
            )

    parts["snapshot_repository"] = _PassThrough()

    result = await _draft(parts, agent)

    assert result.status is (
        CoverLetterDraftStatus.DEFERRED_INSUFFICIENT_EVIDENCE
    )
    assert result.draft is None
    assert not result.retryable
    assert agent.contexts == []
    assert not _draft_files(parts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["untyped", "unknown_evidence", "unknown_jd_reference", "empty_paragraphs"],
)
async def test_illegal_agent_output_defers_needs_human(
    tmp_path: Path, corruption: str
) -> None:
    parts = _setup(tmp_path)
    if corruption == "untyped":
        agent = _FakeCoverLetterAgent(lambda _c: {"greeting": "free text"})
    elif corruption == "unknown_evidence":
        bad = CoverLetterParagraphProposal(
            purpose=CoverLetterParagraphPurpose.QUALIFICATION,
            text="Built deterministic geospatial pipelines.",
            evidence_ids=("cover-letter-evidence-" + "0" * 64,),
            jd_alignment=("Streamlined geospatial data pipelines in Python",),
        )
        agent = _FakeCoverLetterAgent(
            _default_output(parts, qualification=bad)
        )
    elif corruption == "unknown_jd_reference":
        evidence_id = _evidence_for_block_containing(
            parts["snapshot"], "geospatial pipelines"
        )
        bad = CoverLetterParagraphProposal(
            purpose=CoverLetterParagraphPurpose.QUALIFICATION,
            text="Built deterministic geospatial pipelines.",
            evidence_ids=(evidence_id,),
            jd_alignment=("A requirement that does not appear in the JD",),
        )
        agent = _FakeCoverLetterAgent(
            _default_output(parts, qualification=bad)
        )
    else:
        agent = _FakeCoverLetterAgent(
            lambda _c: {"paragraphs_missing": True}
        )

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is CoverLetterDraftFailureReason.AGENT_OUTPUT_UNSAFE
    )
    assert not result.retryable
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_agent_unavailable_fails_without_auto_retry(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeCoverLetterAgent(
        CoverLetterAgentUnavailableError("provider offline")
    )

    result = await _draft(parts, agent)

    assert result.status is CoverLetterDraftStatus.FAILED
    assert (
        result.reason_code
        is CoverLetterDraftFailureReason.AGENT_UNAVAILABLE
    )
    assert result.retryable
    assert len(agent.contexts) == 1
    assert not _draft_files(parts)


@pytest.mark.asyncio
async def test_repository_conflict_and_corruption_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeCoverLetterAgent(_default_output(parts))
    first = await _draft(parts, agent)
    draft = first.draft
    assert draft is not None
    record = next(
        parts["home"].paths.cover_letter_drafts.rglob(
            f"{draft.draft_id}.json"
        )
    )
    corrupted = b"{broken"
    record.write_bytes(corrupted)

    conflict = parts["draft_repository"].save(draft)
    read = parts["draft_repository"].get(
        subject_id=draft.subject_id, draft_id=draft.draft_id
    )

    assert conflict.status.value == "FAILED"
    assert (
        conflict.reason_code
        is CoverLetterDraftFailureReason.DRAFT_INTEGRITY_FAILURE
    )
    assert record.read_bytes() == corrupted
    assert read.status is CoverLetterDraftReadStatus.INTEGRITY_FAILURE


@pytest.mark.asyncio
async def test_invalid_command_fails_without_side_effects(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    agent = _FakeCoverLetterAgent(_default_output(parts))

    naive = await _draft(
        parts, agent, now=datetime(2026, 7, 31, 15, 0)
    )
    missing = await draft_cover_letter(
        DraftCoverLetterCommand(
            subject_id="subject-a",
            application_plan_id="application-plan-" + "0" * 64,
            cover_letter_evidence_snapshot_id=parts["snapshot"].snapshot_id,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        evidence_snapshot_repository=parts["snapshot_repository"],
        agent=agent,
        metadata=METADATA,
        draft_repository=parts["draft_repository"],
    )

    assert naive.reason_code is CoverLetterDraftFailureReason.INVALID_REQUEST
    assert (
        missing.reason_code
        is CoverLetterDraftFailureReason.APPLICATION_PLAN_NOT_FOUND
    )
    assert agent.contexts == []
    assert not _draft_files(parts)


def test_module_has_no_fact_qa_rendering_manifest_or_execution_dependency() -> None:
    module_path = Path(draft_module.__file__)
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
        "core.plan_material_manifest",
        "core.resume_fact_qa",
        "core.resume_tailoring",
        "core.priority_agent_adapter",
        "playwright",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
