from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import core.cover_letter_fact_qa as fact_qa_module
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.cover_letter_draft import (
    CoverLetterAgentMetadata,
    CoverLetterAgentOutput,
    CoverLetterDraft,
    CoverLetterDraftReadResult,
    CoverLetterDraftReadStatus,
    CoverLetterDraftStatus,
    CoverLetterParagraph,
    CoverLetterParagraphProposal,
    CoverLetterParagraphPurpose,
    DraftCoverLetterCommand,
    PrivateHomeCoverLetterDraftRepository,
    draft_cover_letter,
)
from core.cover_letter_evidence import (
    CreateCoverLetterEvidenceSnapshotCommand,
    CoverLetterEvidenceSnapshotReadResult,
    CoverLetterEvidenceSnapshotReadStatus,
    PrivateHomeCoverLetterEvidenceSnapshotRepository,
    create_cover_letter_evidence_snapshot,
)
from core.cover_letter_fact_qa import (
    COVER_LETTER_FACT_QA_AGENT_POLICY,
    COVER_LETTER_FACT_QA_CONTRACT_VERSION,
    COVER_LETTER_FACT_QA_POLICY_VERSION,
    CoverLetterFactQAAgentContext,
    CoverLetterFactQAAgentMetadata,
    CoverLetterFactQAAgentOutput,
    CoverLetterFactQAAgentVerdict,
    CoverLetterFactQAFailureReason,
    CoverLetterFactQAFindingProposal,
    CoverLetterFactQAFindingSeverity,
    CoverLetterFactQAFindingSource,
    CoverLetterFactQAReadStatus,
    CoverLetterFactQAStatus,
    CoverLetterFactQAVerdict,
    PrivateHomeCoverLetterFactQARepository,
    RunCoverLetterFactQACommand,
    review_cover_letter_fact_qa,
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
DRAFT_METADATA = CoverLetterAgentMetadata(
    agent_version="cover-letter-draft-agent-v1",
    prompt_version="cover-letter-draft-prompt-v1",
    model_id="synthetic-cover-letter-model",
)
QA_METADATA = CoverLetterFactQAAgentMetadata(
    agent_version="cover-letter-fact-qa-agent-v1",
    prompt_version="cover-letter-fact-qa-prompt-v1",
    model_id="synthetic-fact-qa-model",
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
        self.contexts = []

    async def generate(self, context):
        self.contexts.append(context)
        return self.output


class _FakeFactQAAgent:
    def __init__(self, output=None) -> None:
        self.output = output
        self.contexts: list[CoverLetterFactQAAgentContext] = []

    async def review(self, context: CoverLetterFactQAAgentContext):
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


def _evidence_for_block_containing(snapshot, text: str) -> str:
    for item in snapshot.evidence_items:
        if text in item.evidence_text:
            return item.evidence_id
    raise AssertionError(f"no evidence containing {text!r}")


def _default_draft_output(parts) -> CoverLetterAgentOutput:
    snapshot = parts["snapshot"]
    qualification_evidence = _evidence_for_block_containing(
        snapshot, "geospatial pipelines"
    )
    motivation_evidence = _evidence_for_block_containing(
        snapshot, "satellite datasets"
    )
    return CoverLetterAgentOutput(
        greeting="Dear Hiring Team,",
        paragraphs=(
            CoverLetterParagraphProposal(
                purpose=CoverLetterParagraphPurpose.INTRODUCTION,
                text=(
                    "I am writing to apply for the Geospatial Data "
                    "Engineer role at Example Robotics."
                ),
                evidence_ids=(),
                jd_alignment=(),
            ),
            CoverLetterParagraphProposal(
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
            CoverLetterParagraphProposal(
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
            CoverLetterParagraphProposal(
                purpose=CoverLetterParagraphPurpose.CLOSING,
                text=(
                    "Thank you for considering my application; I look "
                    "forward to discussing the role further."
                ),
                evidence_ids=(),
                jd_alignment=(),
            ),
        ),
        closing="Sincerely,\nSynthetic Candidate",
        rationale="Selected the two evidence items most aligned to the JD.",
    )


async def _setup(tmp_path: Path, *, subject_id: str = "subject-a"):
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
        user_preparation_instructions=None,
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

    draft_repository = PrivateHomeCoverLetterDraftRepository(home)
    job_repository = _JobRepository(_job_for_plan(plan))
    parts = {
        "home": home,
        "plan_repository": plan_repository,
        "plan": plan,
        "job_repository": job_repository,
        "candidate": candidate,
        "snapshot_repository": snapshot_repository,
        "snapshot": snapshot,
        "draft_repository": draft_repository,
        "result_repository": PrivateHomeCoverLetterFactQARepository(home),
    }

    draft_agent = _FakeCoverLetterAgent(_default_draft_output(parts))

    draft_result = await draft_cover_letter(
        DraftCoverLetterCommand(
            subject_id=subject_id,
            application_plan_id=plan.plan_id,
            cover_letter_evidence_snapshot_id=snapshot.snapshot_id,
            now=NOW,
        ),
        application_plan_repository=plan_repository,
        job_repository=job_repository,
        evidence_snapshot_repository=snapshot_repository,
        agent=draft_agent,
        metadata=DRAFT_METADATA,
        draft_repository=draft_repository,
    )
    assert draft_result.status is CoverLetterDraftStatus.CREATED
    parts["draft"] = draft_result.draft
    return parts


def _paragraph(order, purpose, text, evidence_ids=(), jd_alignment=()):
    content = {
        "evidence_ids": list(evidence_ids),
        "jd_alignment": list(jd_alignment),
        "order": order,
        "purpose": purpose.value,
        "text": text,
    }
    return CoverLetterParagraph(
        paragraph_id="cover-letter-paragraph-" + _hash(content),
        order=order,
        purpose=purpose,
        text=text,
        evidence_ids=evidence_ids,
        jd_alignment=jd_alignment,
    )


def _custom_draft(
    base_draft: CoverLetterDraft,
    *,
    paragraphs=None,
    greeting=None,
    closing=None,
) -> CoverLetterDraft:
    paragraphs = (
        paragraphs if paragraphs is not None else base_draft.paragraphs
    )
    greeting = greeting if greeting is not None else base_draft.greeting
    closing = closing if closing is not None else base_draft.closing
    content = {
        "draft_id": base_draft.draft_id,
        "contract_version": base_draft.contract_version,
        "draft_binding": base_draft.draft_binding,
        "subject_id": base_draft.subject_id,
        "application_plan_id": base_draft.application_plan_id,
        "job_id": base_draft.job_id,
        "job_revision": base_draft.job_revision,
        "job_content_hash": base_draft.job_content_hash,
        "evidence_snapshot_id": base_draft.evidence_snapshot_id,
        "evidence_snapshot_hash": base_draft.evidence_snapshot_hash,
        "user_preparation_instructions_hash": (
            base_draft.user_preparation_instructions_hash
        ),
        "agent_version": base_draft.agent_version,
        "prompt_version": base_draft.prompt_version,
        "model_id": base_draft.model_id,
        "agent_policy_version": base_draft.agent_policy_version,
        "greeting": greeting,
        "paragraphs": [item.to_dict() for item in paragraphs],
        "closing": closing,
        "rationale": base_draft.rationale,
    }
    return CoverLetterDraft(
        draft_id=base_draft.draft_id,
        contract_version=base_draft.contract_version,
        draft_binding=base_draft.draft_binding,
        subject_id=base_draft.subject_id,
        application_plan_id=base_draft.application_plan_id,
        job_id=base_draft.job_id,
        job_revision=base_draft.job_revision,
        job_content_hash=base_draft.job_content_hash,
        evidence_snapshot_id=base_draft.evidence_snapshot_id,
        evidence_snapshot_hash=base_draft.evidence_snapshot_hash,
        user_preparation_instructions_hash=(
            base_draft.user_preparation_instructions_hash
        ),
        agent_version=base_draft.agent_version,
        prompt_version=base_draft.prompt_version,
        model_id=base_draft.model_id,
        agent_policy_version=base_draft.agent_policy_version,
        greeting=greeting,
        paragraphs=paragraphs,
        closing=closing,
        rationale=base_draft.rationale,
        draft_content_hash=_hash(content),
        created_at=base_draft.created_at,
    )


class _DraftPassThrough:
    def __init__(self, draft: CoverLetterDraft) -> None:
        self.draft = draft
        self.save_calls = 0

    def get(self, **_kwargs) -> CoverLetterDraftReadResult:
        return CoverLetterDraftReadResult(
            status=CoverLetterDraftReadStatus.FOUND, draft=self.draft
        )

    def save(self, draft):  # pragma: no cover - must never be called
        self.save_calls += 1
        raise AssertionError("Fact QA must never persist or modify a draft")


class _SnapshotPassThrough:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    def get(self, **_kwargs) -> CoverLetterEvidenceSnapshotReadResult:
        return CoverLetterEvidenceSnapshotReadResult(
            status=CoverLetterEvidenceSnapshotReadStatus.FOUND,
            snapshot=self.snapshot,
        )


async def _run_qa(
    parts,
    agent,
    *,
    subject_id: str = "subject-a",
    plan_id: str | None = None,
    snapshot_id: str | None = None,
    draft_id: str | None = None,
    draft_repository=None,
    snapshot_repository=None,
    metadata: CoverLetterFactQAAgentMetadata = QA_METADATA,
    now: datetime = NOW,
):
    return await review_cover_letter_fact_qa(
        RunCoverLetterFactQACommand(
            subject_id=subject_id,
            application_plan_id=plan_id or parts["plan"].plan_id,
            cover_letter_evidence_snapshot_id=(
                snapshot_id or parts["snapshot"].snapshot_id
            ),
            cover_letter_draft_id=draft_id or parts["draft"].draft_id,
            now=now,
        ),
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        evidence_snapshot_repository=(
            snapshot_repository or parts["snapshot_repository"]
        ),
        draft_repository=draft_repository or parts["draft_repository"],
        agent=agent,
        metadata=metadata,
        result_repository=parts["result_repository"],
    )


def _result_files(parts) -> tuple[Path, ...]:
    return tuple(
        parts["home"].paths.cover_letter_fact_qa_results.rglob("*.json")
    )


@pytest.mark.asyncio
async def test_consistent_binding_creates_passed_result(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )

    result = await _run_qa(parts, agent)

    assert result.status is CoverLetterFactQAStatus.CREATED
    assert result.result is not None
    assert result.result.verdict is CoverLetterFactQAVerdict.PASSED
    assert result.result.contract_version == (
        COVER_LETTER_FACT_QA_CONTRACT_VERSION
    )
    assert result.result.cover_letter_draft_id == parts["draft"].draft_id
    assert result.result.draft_content_hash == (
        parts["draft"].draft_content_hash
    )
    assert result.result.evidence_snapshot_id == (
        parts["snapshot"].snapshot_id
    )
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["plan_subject", "job", "snapshot", "draft"]
)
async def test_binding_mismatch_blocks_without_agent_call(
    tmp_path: Path, damage: str
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )
    subject_id = "subject-a"
    kwargs = {}

    if damage == "plan_subject":
        subject_id = "subject-b"
    elif damage == "job":
        job = parts["job_repository"].job
        parts["job_repository"] = _JobRepository(
            JobPosting(**{**job.to_dict(), "content_hash": "f" * 64})
        )
    elif damage == "snapshot":
        object.__setattr__(
            parts["snapshot"],
            "application_plan_id",
            "application-plan-" + "0" * 64,
        )
        kwargs["snapshot_repository"] = _SnapshotPassThrough(
            parts["snapshot"]
        )
    else:
        tampered = _custom_draft(parts["draft"])
        object.__setattr__(tampered, "job_content_hash", "f" * 64)
        kwargs["draft_repository"] = _DraftPassThrough(tampered)

    result = await _run_qa(parts, agent, subject_id=subject_id, **kwargs)

    assert result.status is CoverLetterFactQAStatus.BLOCKED_BINDING_MISMATCH
    assert agent.contexts == []
    assert not _result_files(parts)


@pytest.mark.asyncio
async def test_unknown_evidence_reference_blocks_deterministically(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    draft = parts["draft"]
    bad_paragraphs = list(draft.paragraphs)
    bad_paragraphs[1] = _paragraph(
        1,
        CoverLetterParagraphPurpose.QUALIFICATION,
        draft.paragraphs[1].text,
        evidence_ids=("cover-letter-evidence-" + "0" * 64,),
        jd_alignment=draft.paragraphs[1].jd_alignment,
    )
    tampered = _custom_draft(draft, paragraphs=tuple(bad_paragraphs))
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )

    result = await _run_qa(
        parts, agent, draft_repository=_DraftPassThrough(tampered)
    )

    assert result.status is CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    assert result.result is not None
    assert result.result.verdict is CoverLetterFactQAVerdict.BLOCKED
    types = {item.finding_type for item in result.result.findings}
    assert "UNKNOWN_EVIDENCE_REFERENCE" in types
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_unsupported_candidate_claim_blocks_deterministically(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    draft = parts["draft"]
    evidence_id = draft.paragraphs[1].evidence_ids[0]
    bad_paragraphs = list(draft.paragraphs)
    bad_paragraphs[1] = _paragraph(
        1,
        CoverLetterParagraphPurpose.QUALIFICATION,
        "Led enterprise Kubernetes migrations across 40 clusters.",
        evidence_ids=(evidence_id,),
        jd_alignment=draft.paragraphs[1].jd_alignment,
    )
    tampered = _custom_draft(draft, paragraphs=tuple(bad_paragraphs))
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )

    result = await _run_qa(
        parts, agent, draft_repository=_DraftPassThrough(tampered)
    )

    assert result.status is CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    types = {item.finding_type for item in result.result.findings}
    assert "UNSUPPORTED_CANDIDATE_CLAIM" in types
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_jd_requirement_as_candidate_fact_blocks_deterministically(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    draft = parts["draft"]
    evidence_id = draft.paragraphs[1].evidence_ids[0]
    bad_paragraphs = list(draft.paragraphs)
    bad_paragraphs[1] = _paragraph(
        1,
        CoverLetterParagraphPurpose.QUALIFICATION,
        "Streamlined geospatial data pipelines in Python for years.",
        evidence_ids=(evidence_id,),
        jd_alignment=(
            "Streamlined geospatial data pipelines in Python",
        ),
    )
    tampered = _custom_draft(draft, paragraphs=tuple(bad_paragraphs))
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )

    result = await _run_qa(
        parts, agent, draft_repository=_DraftPassThrough(tampered)
    )

    assert result.status is CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    types = {item.finding_type for item in result.result.findings}
    assert "JD_REQUIREMENT_PRESENTED_AS_FACT" in types
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_agent_blocks_participation_to_leadership_exaggeration(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    draft = parts["draft"]
    qualification_id = draft.paragraphs[1].paragraph_id
    evidence_id = draft.paragraphs[1].evidence_ids[0]

    def _output(context: CoverLetterFactQAAgentContext):
        return CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.BLOCKED,
            findings=(
                CoverLetterFactQAFindingProposal(
                    paragraph_id=qualification_id,
                    finding_type="RESPONSIBILITY_LEVEL_EXAGGERATION",
                    severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                    claim_text=draft.paragraphs[1].text,
                    evidence_ids=(evidence_id,),
                    jd_references=(),
                    explanation=(
                        "the evidence describes participation, not sole "
                        "ownership of the pipeline"
                    ),
                ),
            ),
        )

    agent = _FakeFactQAAgent(_output)
    result = await _run_qa(parts, agent)

    assert result.status is CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    assert result.result.verdict is CoverLetterFactQAVerdict.BLOCKED
    finding = result.result.findings[0]
    assert finding.finding_type == "RESPONSIBILITY_LEVEL_EXAGGERATION"
    assert finding.source is CoverLetterFactQAFindingSource.AGENT
    assert finding.severity is CoverLetterFactQAFindingSeverity.BLOCKING
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
async def test_agent_blocks_fabricated_company_connection(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    draft = parts["draft"]
    motivation_id = draft.paragraphs[2].paragraph_id

    def _output(context: CoverLetterFactQAAgentContext):
        return CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.BLOCKED,
            findings=(
                CoverLetterFactQAFindingProposal(
                    paragraph_id=motivation_id,
                    finding_type="FABRICATED_COMPANY_CONNECTION",
                    severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                    claim_text=draft.paragraphs[2].text,
                    evidence_ids=(),
                    jd_references=(),
                    explanation=(
                        "no evidence supports a personal connection to "
                        "the company's mission"
                    ),
                ),
            ),
        )

    agent = _FakeFactQAAgent(_output)
    result = await _run_qa(parts, agent)

    assert result.status is CoverLetterFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    finding = result.result.findings[0]
    assert finding.finding_type == "FABRICATED_COMPANY_CONNECTION"
    assert finding.source is CoverLetterFactQAFindingSource.AGENT


@pytest.mark.asyncio
async def test_agent_receives_only_current_draft_evidence_and_jd(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )

    await _run_qa(parts, agent)

    assert len(agent.contexts) == 1
    context = agent.contexts[0]
    assert context.subject_id == "subject-a"
    assert context.application_plan_id == parts["plan"].plan_id
    assert context.job.description == JOB_DESCRIPTION
    assert context.greeting == parts["draft"].greeting
    assert context.closing == parts["draft"].closing
    assert tuple(item.paragraph_id for item in context.paragraphs) == tuple(
        item.paragraph_id for item in parts["draft"].paragraphs
    )
    assert tuple(item.evidence_id for item in context.evidence_items) == (
        tuple(
            item.evidence_id
            for item in parts["snapshot"].evidence_items
        )
    )
    assert context.qa_policy == COVER_LETTER_FACT_QA_AGENT_POLICY
    assert context.qa_policy_version == COVER_LETTER_FACT_QA_POLICY_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["unknown_paragraph", "unknown_evidence", "unknown_jd"]
)
async def test_agent_findings_with_unknown_reference_defer(
    tmp_path: Path, corruption: str
) -> None:
    parts = await _setup(tmp_path)
    draft = parts["draft"]

    if corruption == "unknown_paragraph":
        paragraph_id = "cover-letter-paragraph-" + "0" * 64
        evidence_ids: tuple[str, ...] = ()
        jd_references: tuple[str, ...] = ()
    elif corruption == "unknown_evidence":
        paragraph_id = draft.paragraphs[1].paragraph_id
        evidence_ids = ("cover-letter-evidence-" + "0" * 64,)
        jd_references = ()
    else:
        paragraph_id = draft.paragraphs[1].paragraph_id
        evidence_ids = ()
        jd_references = ("a requirement that is not in the JD",)

    def _output(context: CoverLetterFactQAAgentContext):
        return CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.BLOCKED,
            findings=(
                CoverLetterFactQAFindingProposal(
                    paragraph_id=paragraph_id,
                    finding_type="SEMANTIC_SCOPE_OVERREACH",
                    severity=CoverLetterFactQAFindingSeverity.BLOCKING,
                    claim_text="some claim",
                    evidence_ids=evidence_ids,
                    jd_references=jd_references,
                    explanation="illegal reference",
                ),
            ),
        )

    agent = _FakeFactQAAgent(_output)
    result = await _run_qa(parts, agent)

    assert result.status is CoverLetterFactQAStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is CoverLetterFactQAFailureReason.AGENT_OUTPUT_UNSAFE
    )
    assert not result.retryable
    assert not _result_files(parts)


@pytest.mark.asyncio
async def test_agent_uncertain_verdict_defers_needs_human(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.UNCERTAIN, findings=()
        )
    )

    result = await _run_qa(parts, agent)

    assert result.status is CoverLetterFactQAStatus.DEFERRED_NEEDS_HUMAN
    assert not _result_files(parts)


@pytest.mark.asyncio
async def test_untyped_agent_output_defers_needs_human(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(lambda _c: {"verdict": "PASSED"})

    result = await _run_qa(parts, agent)

    assert result.status is CoverLetterFactQAStatus.DEFERRED_NEEDS_HUMAN
    assert not _result_files(parts)


@pytest.mark.asyncio
async def test_defer_does_not_modify_or_persist_the_draft(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.UNCERTAIN, findings=()
        )
    )
    before = parts["draft_repository"].get(
        subject_id="subject-a", draft_id=parts["draft"].draft_id
    )

    await _run_qa(parts, agent)

    after = parts["draft_repository"].get(
        subject_id="subject-a", draft_id=parts["draft"].draft_id
    )
    assert before.draft == after.draft


@pytest.mark.asyncio
async def test_completed_binding_replays_unchanged_with_zero_extra_agent_calls(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )

    first = await _run_qa(parts, agent)
    replay = await _run_qa(parts, agent, now=NOW + timedelta(days=2))

    assert first.status is CoverLetterFactQAStatus.CREATED
    assert replay.status is CoverLetterFactQAStatus.UNCHANGED
    assert replay.result == first.result
    assert replay.result.validated_at == NOW
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
async def test_new_qa_version_creates_a_new_result(tmp_path: Path) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )
    other_metadata = CoverLetterFactQAAgentMetadata(
        agent_version="cover-letter-fact-qa-agent-v2",
        prompt_version=QA_METADATA.prompt_version,
        model_id=QA_METADATA.model_id,
    )

    first = await _run_qa(parts, agent)
    second = await _run_qa(parts, agent, metadata=other_metadata)

    assert first.status is CoverLetterFactQAStatus.CREATED
    assert second.status is CoverLetterFactQAStatus.CREATED
    assert first.result.result_id != second.result.result_id
    assert len(agent.contexts) == 2


@pytest.mark.asyncio
async def test_restart_reads_identical_result_identity_and_hash(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )
    first = await _run_qa(parts, agent)
    assert first.result is not None

    restarted = PrivateHomeCoverLetterFactQARepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(
        subject_id="subject-a", result_id=first.result.result_id
    )

    assert read.status is CoverLetterFactQAReadStatus.FOUND
    assert read.result == first.result
    assert read.result.result_content_hash == first.result.result_content_hash
    cross = restarted.get(
        subject_id="subject-b", result_id=first.result.result_id
    )
    assert cross.status is CoverLetterFactQAReadStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_repository_conflict_and_corruption_fail_closed(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )
    first = await _run_qa(parts, agent)
    result = first.result
    assert result is not None
    record = next(
        parts["home"].paths.cover_letter_fact_qa_results.rglob(
            f"{result.result_id}.json"
        )
    )
    corrupted = b"{broken"
    record.write_bytes(corrupted)

    conflict = parts["result_repository"].save(result)
    read = parts["result_repository"].get(
        subject_id=result.subject_id, result_id=result.result_id
    )

    assert conflict.status.value == "FAILED"
    assert record.read_bytes() == corrupted
    assert read.status is CoverLetterFactQAReadStatus.INTEGRITY_FAILURE


@pytest.mark.asyncio
async def test_invalid_command_fails_without_side_effects(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeFactQAAgent(
        CoverLetterFactQAAgentOutput(
            verdict=CoverLetterFactQAAgentVerdict.PASSED, findings=()
        )
    )

    naive = await _run_qa(parts, agent, now=datetime(2026, 7, 31, 15, 0))
    missing = await review_cover_letter_fact_qa(
        RunCoverLetterFactQACommand(
            subject_id="subject-a",
            application_plan_id="application-plan-" + "0" * 64,
            cover_letter_evidence_snapshot_id=parts["snapshot"].snapshot_id,
            cover_letter_draft_id=parts["draft"].draft_id,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        evidence_snapshot_repository=parts["snapshot_repository"],
        draft_repository=parts["draft_repository"],
        agent=agent,
        metadata=QA_METADATA,
        result_repository=parts["result_repository"],
    )

    assert (
        naive.reason_code is CoverLetterFactQAFailureReason.INVALID_REQUEST
    )
    assert (
        missing.reason_code
        is CoverLetterFactQAFailureReason.APPLICATION_PLAN_NOT_FOUND
    )
    assert agent.contexts == []
    assert not _result_files(parts)


def test_module_has_no_rendering_manifest_or_execution_dependency() -> None:
    module_path = Path(fact_qa_module.__file__)
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
        "core.latex_compiler",
        "core.resume_compilation",
        "core.resume_latex_construction",
        "core.priority_agent_adapter",
        "playwright",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
