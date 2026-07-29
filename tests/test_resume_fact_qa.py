from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import core.resume_fact_qa as fact_qa_module
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
    ResumeSummarySource,
    ResumeSummaryTrust,
    register_resume_candidate,
)
from core.resume_fact_qa import (
    RESUME_FACT_QA_AGENT_POLICY,
    RESUME_FACT_QA_CONTRACT_VERSION,
    RESUME_FACT_QA_POLICY_VERSION,
    PrivateHomeResumeFactQARepository,
    ResumeFactQAAgentFinding,
    ResumeFactQAAgentMetadata,
    ResumeFactQAAgentOutput,
    ResumeFactQAAgentUnavailableError,
    ResumeFactQAAgentVerdict,
    ResumeFactQAContext,
    ResumeFactQAFailureReason,
    ResumeFactQAFindingSeverity,
    ResumeFactQAFindingSource,
    ResumeFactQAFindingType,
    ResumeFactQAReadStatus,
    ResumeFactQAStatus,
    ResumeFactQAVerdict,
    ResumeFactQAWriteStatus,
    RunResumeFactQACommand,
    run_resume_fact_qa,
)
from core.resume_selection import (
    RESUME_SELECTION_CONTRACT_VERSION,
    PrivateHomeResumeSelectionDecisionRepository,
    ResumeSelectionDecision,
    ResumeSelectionMethod,
)
from core.resume_tailoring import (
    RESUME_TAILORING_CONTRACT_VERSION,
    RESUME_TAILORING_POLICY_VERSION,
    PrivateHomeTailoredResumeDraftRepository,
    ResumeTailoringAgentDisposition,
    ResumeTailoringAgentMetadata,
    ResumeTailoringAgentOutput,
    TailorResumeCommand,
    TailoredBulletChangeType,
    TailoredBulletProposal,
    TailoredResumeBullet,
    TailoredResumeDraft,
    TailoredResumeSection,
    TailoredSectionProposal,
    tailor_resume,
)
from core.source_resume_projection import (
    CreateSourceResumeProjectionCommand,
    DeterministicSourceResumeParser,
    PrivateHomeSourceResumeArtifactReader,
    PrivateHomeSourceResumeProjectionRepository,
    create_source_resume_projection,
)


NOW = datetime(2026, 7, 28, 22, 0, tzinfo=timezone.utc)
QA_METADATA = ResumeFactQAAgentMetadata(
    agent_version="resume-fact-qa-agent-v1",
    prompt_version="resume-fact-qa-prompt-v1",
    model_id="synthetic-qa-model",
)
TAILORING_METADATA = ResumeTailoringAgentMetadata(
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


class _FakeQAAgent:
    def __init__(self, output=None) -> None:
        self.output = (
            output
            if output is not None
            else ResumeFactQAAgentOutput(
                verdict=ResumeFactQAAgentVerdict.SUPPORTED,
                findings=(),
            )
        )
        self.contexts: list[ResumeFactQAContext] = []

    async def review(self, context: ResumeFactQAContext):
        self.contexts.append(context)
        if isinstance(self.output, Exception):
            raise self.output
        if callable(self.output):
            return self.output(context)
        return self.output


class _FakeTailoringAgent:
    def __init__(self, output) -> None:
        self.output = output

    async def tailor(self, _context):
        return self.output


def _setup(tmp_path: Path, *, subject_id: str = "subject-a"):
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
    projection = create_source_resume_projection(
        CreateSourceResumeProjectionCommand(
            subject_id=subject_id,
            resume_id=candidate.resume_id,
            now=NOW,
        ),
        candidate_repository=candidate_repository,
        artifact_reader=PrivateHomeSourceResumeArtifactReader(home),
        parser=DeterministicSourceResumeParser(),
        projection_repository=projection_repository,
    ).projection
    assert projection is not None

    snapshot_repository = PrivateHomeCandidateEvidenceSnapshotRepository(home)
    snapshot = create_candidate_evidence_snapshot(
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
    ).snapshot
    assert snapshot is not None

    return {
        "home": home,
        "plan": plan,
        "plan_repository": plan_repository,
        "job_repository": _JobRepository(
            JobPosting(
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
        ),
        "candidate": candidate,
        "candidate_repository": candidate_repository,
        "selection": selection,
        "selection_repository": selection_repository,
        "projection": projection,
        "projection_repository": projection_repository,
        "snapshot": snapshot,
        "snapshot_repository": snapshot_repository,
        "draft_repository": PrivateHomeTailoredResumeDraftRepository(home),
        "qa_repository": PrivateHomeResumeFactQARepository(home),
    }


def _evidence_for_block(snapshot, block_id: str) -> str:
    for item in snapshot.evidence_items:
        if item.source_block_id == block_id:
            return item.evidence_id
    raise AssertionError("no evidence for block")


async def _tailored_draft(parts) -> TailoredResumeDraft:
    """Produce a real P2a4c draft so QA runs against genuine upstream output."""

    section = parts["projection"].sections[0]
    heading, bullet, paragraph = section.blocks
    output = ResumeTailoringAgentOutput(
        disposition=ResumeTailoringAgentDisposition.TAILORED,
        sections=(
            TailoredSectionProposal(
                source_section_id=section.section_id,
                order=0,
                bullets=(
                    TailoredBulletProposal(
                        source_section_id=section.section_id,
                        source_block_id=heading.block_id,
                        source_bullet_id=heading.bullet_id,
                        change_type=TailoredBulletChangeType.UNCHANGED,
                        text=heading.text,
                        evidence_ids=(),
                        jd_alignment=(),
                    ),
                    TailoredBulletProposal(
                        source_section_id=section.section_id,
                        source_block_id=bullet.block_id,
                        source_bullet_id=bullet.bullet_id,
                        change_type=TailoredBulletChangeType.REWRITTEN,
                        text=(
                            "Built deterministic geospatial pipelines in "
                            "Python, delivering reproducible processing for "
                            "12 satellite datasets."
                        ),
                        evidence_ids=(
                            _evidence_for_block(
                                parts["snapshot"], bullet.block_id
                            ),
                            _evidence_for_block(
                                parts["snapshot"], paragraph.block_id
                            ),
                        ),
                        jd_alignment=(
                            "geospatial data pipelines in Python",
                        ),
                    ),
                    TailoredBulletProposal(
                        source_section_id=section.section_id,
                        source_block_id=paragraph.block_id,
                        source_bullet_id=paragraph.bullet_id,
                        change_type=TailoredBulletChangeType.UNCHANGED,
                        text=paragraph.text,
                        evidence_ids=(),
                        jd_alignment=(),
                    ),
                ),
            ),
        ),
        rationale="Bullets were tightened toward the geospatial JD.",
    )
    result = await tailor_resume(
        TailorResumeCommand(
            subject_id=parts["plan"].subject_id,
            application_plan_id=parts["plan"].plan_id,
            evidence_snapshot_id=parts["snapshot"].snapshot_id,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        selection_repository=parts["selection_repository"],
        candidate_repository=parts["candidate_repository"],
        projection_repository=parts["projection_repository"],
        evidence_snapshot_repository=parts["snapshot_repository"],
        agent=_FakeTailoringAgent(output),
        metadata=TAILORING_METADATA,
        draft_repository=parts["draft_repository"],
    )
    assert result.draft is not None
    return result.draft


def _draft_with_bullets(parts, bullets) -> TailoredResumeDraft:
    """Build a draft directly so QA can be tested against non-P2a4c output."""

    section = parts["projection"].sections[0]
    sections = (
        TailoredResumeSection(
            order=0,
            source_section_id=section.section_id,
            title=section.title,
            bullets=tuple(bullets),
        ),
    )
    binding = _hash({"synthetic-draft": [item.to_dict() for item in bullets]})
    draft_id = f"tailored-resume-draft-{binding}"
    content = {
        "draft_id": draft_id,
        "contract_version": RESUME_TAILORING_CONTRACT_VERSION,
        "tailoring_binding": binding,
        "subject_id": parts["plan"].subject_id,
        "application_plan_id": parts["plan"].plan_id,
        "job_id": parts["plan"].job_id,
        "job_revision": parts["plan"].job_revision,
        "job_content_hash": parts["plan"].job_content_hash,
        "resume_selection_decision_id": parts["selection"].decision_id,
        "source_resume_id": parts["candidate"].resume_id,
        "source_artifact_sha256": parts["candidate"].artifact_sha256,
        "source_projection_id": parts["projection"].projection_id,
        "source_projection_hash": (
            parts["projection"].projection_content_hash
        ),
        "evidence_snapshot_id": parts["snapshot"].snapshot_id,
        "evidence_snapshot_hash": (
            parts["snapshot"].snapshot_content_hash
        ),
        "user_preparation_instructions_hash": (
            parts["plan"].user_preparation_instructions_hash
        ),
        "agent_version": TAILORING_METADATA.agent_version,
        "prompt_version": TAILORING_METADATA.prompt_version,
        "model_id": TAILORING_METADATA.model_id,
        "agent_policy_version": RESUME_TAILORING_POLICY_VERSION,
        "rationale": "Synthetic draft built directly for fact-QA coverage.",
        "sections": [item.to_dict() for item in sections],
    }
    draft = TailoredResumeDraft(
        draft_id=draft_id,
        contract_version=RESUME_TAILORING_CONTRACT_VERSION,
        tailoring_binding=binding,
        subject_id=parts["plan"].subject_id,
        application_plan_id=parts["plan"].plan_id,
        job_id=parts["plan"].job_id,
        job_revision=parts["plan"].job_revision,
        job_content_hash=parts["plan"].job_content_hash,
        resume_selection_decision_id=parts["selection"].decision_id,
        source_resume_id=parts["candidate"].resume_id,
        source_artifact_sha256=parts["candidate"].artifact_sha256,
        source_projection_id=parts["projection"].projection_id,
        source_projection_hash=(
            parts["projection"].projection_content_hash
        ),
        evidence_snapshot_id=parts["snapshot"].snapshot_id,
        evidence_snapshot_hash=parts["snapshot"].snapshot_content_hash,
        user_preparation_instructions_hash=(
            parts["plan"].user_preparation_instructions_hash
        ),
        agent_version=TAILORING_METADATA.agent_version,
        prompt_version=TAILORING_METADATA.prompt_version,
        model_id=TAILORING_METADATA.model_id,
        agent_policy_version=RESUME_TAILORING_POLICY_VERSION,
        rationale="Synthetic draft built directly for fact-QA coverage.",
        sections=sections,
        draft_content_hash=_hash(content),
        created_at=NOW,
    )
    parts["draft_repository"].save(draft)
    return draft


def _bullets(parts, *, rewritten_text: str, evidence_ids=None, jd=None):
    section = parts["projection"].sections[0]
    heading, bullet, paragraph = section.blocks
    if evidence_ids is None:
        evidence_ids = (
            _evidence_for_block(parts["snapshot"], bullet.block_id),
        )
    return (
        TailoredResumeBullet(
            order=0,
            change_type=TailoredBulletChangeType.UNCHANGED,
            text=heading.text,
            source_section_id=section.section_id,
            source_block_id=heading.block_id,
            source_bullet_id=heading.bullet_id,
            evidence_ids=(),
            jd_alignment=(),
        ),
        TailoredResumeBullet(
            order=1,
            change_type=TailoredBulletChangeType.REWRITTEN,
            text=rewritten_text,
            source_section_id=section.section_id,
            source_block_id=bullet.block_id,
            source_bullet_id=bullet.bullet_id,
            evidence_ids=tuple(evidence_ids),
            jd_alignment=tuple(
                jd
                if jd is not None
                else ("geospatial data pipelines in Python",)
            ),
        ),
        TailoredResumeBullet(
            order=2,
            change_type=TailoredBulletChangeType.UNCHANGED,
            text=paragraph.text,
            source_section_id=section.section_id,
            source_block_id=paragraph.block_id,
            source_bullet_id=paragraph.bullet_id,
            evidence_ids=(),
            jd_alignment=(),
        ),
    )


async def _run_qa(
    parts,
    draft,
    agent,
    *,
    subject_id: str = "subject-a",
    metadata: ResumeFactQAAgentMetadata = QA_METADATA,
    now: datetime = NOW,
):
    return await run_resume_fact_qa(
        RunResumeFactQACommand(
            subject_id=subject_id,
            tailored_resume_draft_id=draft.draft_id,
            now=now,
        ),
        draft_repository=parts["draft_repository"],
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        selection_repository=parts["selection_repository"],
        projection_repository=parts["projection_repository"],
        evidence_snapshot_repository=parts["snapshot_repository"],
        agent=agent,
        metadata=metadata,
        qa_repository=parts["qa_repository"],
    )


def _qa_files(parts) -> tuple[Path, ...]:
    return tuple(parts["home"].paths.resume_fact_qa_results.rglob("*.json"))


@pytest.mark.asyncio
async def test_supported_draft_passes_with_immutable_result(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)
    agent = _FakeQAAgent()

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.CREATED
    qa_result = result.qa_result
    assert qa_result is not None
    assert qa_result.verdict is ResumeFactQAVerdict.PASSED
    assert qa_result.contract_version == RESUME_FACT_QA_CONTRACT_VERSION
    assert qa_result.agent_policy_version == RESUME_FACT_QA_POLICY_VERSION
    assert qa_result.tailored_resume_draft_id == draft.draft_id
    assert qa_result.tailored_resume_draft_hash == draft.draft_content_hash
    assert qa_result.source_projection_id == draft.source_projection_id
    assert qa_result.evidence_snapshot_id == draft.evidence_snapshot_id
    assert qa_result.evidence_snapshot_hash == draft.evidence_snapshot_hash
    assert qa_result.agent_invoked is True
    assert qa_result.agent_version == QA_METADATA.agent_version
    assert qa_result.model_id == QA_METADATA.model_id
    assert qa_result.findings == ()
    assert qa_result.validated_at == NOW
    assert qa_result.qa_result_id == f"resume-fact-qa-{qa_result.qa_binding}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (
            "subject",
            ResumeFactQAFailureReason.DRAFT_SUBJECT_MISMATCH,
        ),
        (
            "job",
            ResumeFactQAFailureReason.JOB_BINDING_MISMATCH,
        ),
        (
            "selection",
            ResumeFactQAFailureReason.RESUME_SELECTION_BINDING_MISMATCH,
        ),
        (
            "projection",
            ResumeFactQAFailureReason.SOURCE_PROJECTION_BINDING_MISMATCH,
        ),
        (
            "snapshot",
            ResumeFactQAFailureReason.EVIDENCE_SNAPSHOT_BINDING_MISMATCH,
        ),
    ],
)
async def test_binding_mismatch_blocks_without_agent_or_write(
    tmp_path: Path, damage: str, reason: ResumeFactQAFailureReason
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)
    agent = _FakeQAAgent()
    subject_id = "subject-a"

    if damage == "subject":
        object.__setattr__(draft, "subject_id", "subject-b")

        class _DraftRepository:
            def __init__(self, value) -> None:
                self.value = value

            def get(self, **_kwargs):
                from core.resume_tailoring import (
                    TailoredResumeDraftReadResult,
                    TailoredResumeDraftReadStatus,
                )

                return TailoredResumeDraftReadResult(
                    status=TailoredResumeDraftReadStatus.FOUND,
                    draft=self.value,
                )

        parts["draft_repository"] = _DraftRepository(draft)
    elif damage == "job":
        job = parts["job_repository"].job
        parts["job_repository"] = _JobRepository(
            JobPosting(**{**job.to_dict(), "revision": 2})
        )
    elif damage == "selection":
        object.__setattr__(
            parts["selection"], "source_artifact_sha256", "f" * 64
        )

        class _SelectionRepository:
            def __init__(self, value) -> None:
                self.value = value

            def get(self, **_kwargs):
                from core.resume_selection import (
                    ResumeSelectionDecisionReadResult,
                    ResumeSelectionDecisionReadStatus,
                )

                return ResumeSelectionDecisionReadResult(
                    status=ResumeSelectionDecisionReadStatus.FOUND,
                    decision=self.value,
                )

        parts["selection_repository"] = _SelectionRepository(
            parts["selection"]
        )
    elif damage == "projection":
        object.__setattr__(
            parts["projection"], "projection_content_hash", "f" * 64
        )

        class _ProjectionRepository:
            def __init__(self, value) -> None:
                self.value = value

            def get(self, **_kwargs):
                from core.source_resume_projection import (
                    SourceResumeProjectionReadResult,
                    SourceResumeProjectionReadStatus,
                )

                return SourceResumeProjectionReadResult(
                    status=SourceResumeProjectionReadStatus.FOUND,
                    projection=self.value,
                )

        parts["projection_repository"] = _ProjectionRepository(
            parts["projection"]
        )
    else:
        object.__setattr__(
            parts["snapshot"], "snapshot_content_hash", "f" * 64
        )

        class _SnapshotRepository:
            def __init__(self, value) -> None:
                self.value = value

            def get(self, **_kwargs):
                from core.candidate_evidence import (
                    CandidateEvidenceSnapshotReadResult,
                    CandidateEvidenceSnapshotReadStatus,
                )

                return CandidateEvidenceSnapshotReadResult(
                    status=CandidateEvidenceSnapshotReadStatus.FOUND,
                    snapshot=self.value,
                )

        parts["snapshot_repository"] = _SnapshotRepository(parts["snapshot"])

    result = await _run_qa(parts, draft, agent, subject_id=subject_id)

    assert result.status is ResumeFactQAStatus.BLOCKED_BINDING_MISMATCH
    assert result.reason_code is reason
    assert result.qa_result is None
    assert agent.contexts == []
    assert not _qa_files(parts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "evidence_ids", "finding_type"),
    [
        (
            "Built pipelines processing 400 satellite scenes.",
            None,
            ResumeFactQAFindingType.UNSUPPORTED_FACT_TOKEN,
        ),
        (
            "Built deterministic pipelines with Kubernetes orchestration.",
            None,
            ResumeFactQAFindingType.UNSUPPORTED_FACT_TOKEN,
        ),
        (
            "Built deterministic geospatial pipelines.",
            ("candidate-evidence-" + "0" * 64,),
            ResumeFactQAFindingType.UNKNOWN_EVIDENCE_REFERENCE,
        ),
        (
            "Built deterministic geospatial pipelines.",
            ("candidate-evidence-" + "0" * 64,),
            ResumeFactQAFindingType.MISSING_EVIDENCE_REFERENCE,
        ),
    ],
)
async def test_deterministic_unsupported_claim_blocks_without_agent(
    tmp_path: Path,
    text: str,
    evidence_ids,
    finding_type: ResumeFactQAFindingType,
) -> None:
    parts = _setup(tmp_path)
    draft = _draft_with_bullets(
        parts, _bullets(parts, rewritten_text=text, evidence_ids=evidence_ids)
    )
    agent = _FakeQAAgent()

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    assert result.reason_code is ResumeFactQAFailureReason.UNSUPPORTED_CLAIM
    assert agent.contexts == []
    qa_result = result.qa_result
    assert qa_result is not None
    assert qa_result.verdict is ResumeFactQAVerdict.BLOCKED
    assert qa_result.agent_invoked is False
    assert qa_result.agent_version is None
    types = {item.finding_type for item in qa_result.findings}
    assert finding_type in types
    assert all(
        item.source is ResumeFactQAFindingSource.DETERMINISTIC
        for item in qa_result.findings
    )


@pytest.mark.asyncio
async def test_altered_unchanged_text_is_caught_independently(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    section = parts["projection"].sections[0]
    heading, bullet, paragraph = section.blocks
    tampered = _bullets(
        parts, rewritten_text="Built deterministic geospatial pipelines."
    )
    tampered = (
        tampered[0],
        tampered[1],
        TailoredResumeBullet(
            order=2,
            change_type=TailoredBulletChangeType.UNCHANGED,
            text="Streamlined Python processing for 99 satellite datasets.",
            source_section_id=section.section_id,
            source_block_id=paragraph.block_id,
            source_bullet_id=paragraph.bullet_id,
            evidence_ids=(),
            jd_alignment=(),
        ),
    )
    draft = _draft_with_bullets(parts, tampered)
    agent = _FakeQAAgent()

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    assert agent.contexts == []
    assert any(
        item.finding_type is ResumeFactQAFindingType.SOURCE_TEXT_ALTERED
        for item in result.qa_result.findings
    )


@pytest.mark.asyncio
async def test_missing_source_coverage_blocks_deterministically(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    bullets = _bullets(
        parts, rewritten_text="Built deterministic geospatial pipelines."
    )
    draft = _draft_with_bullets(parts, bullets[:2])
    agent = _FakeQAAgent()

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    assert agent.contexts == []
    assert any(
        item.finding_type
        is ResumeFactQAFindingType.MISSING_SOURCE_COVERAGE
        for item in result.qa_result.findings
    )


@pytest.mark.asyncio
async def test_unknown_jd_reference_is_advisory_and_still_passes(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = _draft_with_bullets(
        parts,
        _bullets(
            parts,
            rewritten_text="Built deterministic geospatial pipelines.",
            jd=("a requirement that is absent from the job description",),
        ),
    )
    agent = _FakeQAAgent()

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.CREATED
    findings = result.qa_result.findings
    assert len(findings) == 1
    assert (
        findings[0].finding_type
        is ResumeFactQAFindingType.UNKNOWN_JD_REFERENCE
    )
    assert findings[0].severity is ResumeFactQAFindingSeverity.ADVISORY
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "finding_type"),
    [
        (
            "Led deterministic geospatial pipelines.",
            ResumeFactQAFindingType.OVERSTATED_OWNERSHIP,
        ),
        (
            "Deployed deterministic geospatial pipelines to production.",
            ResumeFactQAFindingType.OVERSTATED_MATURITY,
        ),
        (
            "Built deterministic geospatial pipelines, cutting customer churn.",
            ResumeFactQAFindingType.UNSUPPORTED_IMPACT,
        ),
        (
            "Built deterministic geospatial pipelines, which doubled revenue.",
            ResumeFactQAFindingType.UNSUPPORTED_CAUSALITY,
        ),
    ],
)
async def test_semantic_exaggeration_is_blocked_by_the_qa_agent(
    tmp_path: Path, text: str, finding_type: ResumeFactQAFindingType
) -> None:
    parts = _setup(tmp_path)
    section = parts["projection"].sections[0]
    bullet_block = section.blocks[1]
    evidence_id = _evidence_for_block(
        parts["snapshot"], bullet_block.block_id
    )
    draft = _draft_with_bullets(
        parts, _bullets(parts, rewritten_text=text)
    )
    agent = _FakeQAAgent(
        ResumeFactQAAgentOutput(
            verdict=ResumeFactQAAgentVerdict.UNSUPPORTED,
            findings=(
                ResumeFactQAAgentFinding(
                    source_section_id=section.section_id,
                    source_block_id=bullet_block.block_id,
                    source_bullet_id=bullet_block.bullet_id,
                    finding_type=finding_type,
                    claim_text=text,
                    cited_evidence_ids=(evidence_id,),
                    explanation=(
                        "The evidence does not support the claim as written."
                    ),
                ),
            ),
        )
    )

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    assert result.reason_code is ResumeFactQAFailureReason.UNSUPPORTED_CLAIM
    qa_result = result.qa_result
    assert qa_result.verdict is ResumeFactQAVerdict.BLOCKED
    assert qa_result.agent_invoked is True
    recorded = qa_result.findings[0]
    assert recorded.finding_type is finding_type
    assert recorded.source is ResumeFactQAFindingSource.AGENT
    assert recorded.severity is ResumeFactQAFindingSeverity.BLOCKING
    assert recorded.cited_evidence_ids == (evidence_id,)


@pytest.mark.asyncio
async def test_agent_sees_only_rewritten_bullets_and_evidence(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)
    agent = _FakeQAAgent()

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.CREATED
    assert len(agent.contexts) == 1
    context = agent.contexts[0]
    assert context.subject_id == "subject-a"
    assert context.tailored_resume_draft_id == draft.draft_id
    assert all(
        item.change_type is TailoredBulletChangeType.REWRITTEN
        for item in context.bullets
    )
    assert len(context.bullets) == 1
    assert tuple(
        item.evidence_id for item in context.evidence_items
    ) == tuple(
        item.evidence_id for item in parts["snapshot"].evidence_items
    )
    assert context.agent_policy == RESUME_FACT_QA_AGENT_POLICY
    assert context.agent_policy_version == RESUME_FACT_QA_POLICY_VERSION
    assert not hasattr(context, "job")
    assert not hasattr(context, "source_projection")
    summary = parts["candidate"].selection_safe_summary
    assert all(
        summary not in item.evidence_text for item in context.evidence_items
    )


@pytest.mark.asyncio
async def test_replay_returns_unchanged_with_zero_agent_calls(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)
    agent = _FakeQAAgent()

    first = await _run_qa(parts, draft, agent)
    replay = await _run_qa(
        parts, draft, agent, now=NOW + timedelta(days=3)
    )

    assert first.status is ResumeFactQAStatus.CREATED
    assert replay.status is ResumeFactQAStatus.UNCHANGED
    assert replay.qa_result == first.qa_result
    assert replay.qa_result.validated_at == NOW
    assert (
        replay.write_result.status is ResumeFactQAWriteStatus.UNCHANGED
    )
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
async def test_blocked_result_replays_unchanged_without_reevaluation(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = _draft_with_bullets(
        parts,
        _bullets(
            parts,
            rewritten_text="Built pipelines processing 400 satellite scenes.",
        ),
    )
    agent = _FakeQAAgent()

    first = await _run_qa(parts, draft, agent)
    replay = await _run_qa(parts, draft, agent)

    assert first.status is ResumeFactQAStatus.BLOCKED_UNSUPPORTED_CLAIM
    assert replay.status is ResumeFactQAStatus.UNCHANGED
    assert replay.qa_result.verdict is ResumeFactQAVerdict.BLOCKED
    assert replay.qa_result == first.qa_result
    assert agent.contexts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["unknown_bullet", "unknown_evidence", "untyped"]
)
async def test_invalid_agent_findings_defer_for_human_review(
    tmp_path: Path, corruption: str
) -> None:
    parts = _setup(tmp_path)
    section = parts["projection"].sections[0]
    bullet_block = section.blocks[1]
    draft = _draft_with_bullets(
        parts,
        _bullets(
            parts, rewritten_text="Built deterministic geospatial pipelines."
        ),
    )
    if corruption == "untyped":
        agent = _FakeQAAgent(lambda _context: {"verdict": "SUPPORTED"})
    else:
        block_id = (
            "resume-block-" + "0" * 64
            if corruption == "unknown_bullet"
            else bullet_block.block_id
        )
        bullet_id = (
            None
            if corruption == "unknown_bullet"
            else bullet_block.bullet_id
        )
        evidence_id = (
            "candidate-evidence-" + "0" * 64
            if corruption == "unknown_evidence"
            else _evidence_for_block(
                parts["snapshot"], bullet_block.block_id
            )
        )
        agent = _FakeQAAgent(
            ResumeFactQAAgentOutput(
                verdict=ResumeFactQAAgentVerdict.UNSUPPORTED,
                findings=(
                    ResumeFactQAAgentFinding(
                        source_section_id=section.section_id,
                        source_block_id=block_id,
                        source_bullet_id=bullet_id,
                        finding_type=(
                            ResumeFactQAFindingType.OUT_OF_SCOPE_CLAIM
                        ),
                        claim_text="Built deterministic geospatial pipelines.",
                        cited_evidence_ids=(evidence_id,),
                        explanation="Synthetic invalid reference.",
                    ),
                ),
            )
        )

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is ResumeFactQAFailureReason.AGENT_OUTPUT_UNRELIABLE
    )
    assert not result.retryable
    qa_result = result.qa_result
    assert qa_result.verdict is ResumeFactQAVerdict.DEFERRED
    assert any(
        item.finding_type
        is ResumeFactQAFindingType.AGENT_OUTPUT_UNRELIABLE
        for item in qa_result.findings
    )
    stored = parts["draft_repository"].get(
        subject_id="subject-a", draft_id=draft.draft_id
    )
    assert stored.draft == draft


@pytest.mark.asyncio
async def test_uncertain_agent_verdict_defers_without_blocking_others(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = _draft_with_bullets(
        parts,
        _bullets(
            parts, rewritten_text="Built deterministic geospatial pipelines."
        ),
    )
    agent = _FakeQAAgent(
        ResumeFactQAAgentOutput(
            verdict=ResumeFactQAAgentVerdict.UNCERTAIN,
            findings=(),
        )
    )

    deferred = await _run_qa(parts, draft, agent)
    other = _setup(tmp_path / "other")
    other_draft = await _tailored_draft(other)
    unaffected = await _run_qa(other, other_draft, _FakeQAAgent())

    assert deferred.status is ResumeFactQAStatus.DEFERRED_NEEDS_HUMAN
    assert deferred.qa_result.verdict is ResumeFactQAVerdict.DEFERRED
    assert unaffected.status is ResumeFactQAStatus.CREATED


@pytest.mark.asyncio
async def test_agent_unavailable_fails_retryable_without_result(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)
    agent = _FakeQAAgent(
        ResumeFactQAAgentUnavailableError("provider offline")
    )

    result = await _run_qa(parts, draft, agent)

    assert result.status is ResumeFactQAStatus.FAILED
    assert (
        result.reason_code is ResumeFactQAFailureReason.AGENT_UNAVAILABLE
    )
    assert result.retryable
    assert result.qa_result is None
    assert not _qa_files(parts)


@pytest.mark.asyncio
async def test_changed_qa_version_creates_a_new_immutable_result(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)

    first = await _run_qa(parts, draft, _FakeQAAgent())
    upgraded = await _run_qa(
        parts,
        draft,
        _FakeQAAgent(),
        metadata=ResumeFactQAAgentMetadata(
            agent_version="resume-fact-qa-agent-v2",
            prompt_version="resume-fact-qa-prompt-v2",
            model_id="synthetic-qa-model",
        ),
        now=NOW + timedelta(minutes=5),
    )

    assert upgraded.status is ResumeFactQAStatus.CREATED
    assert upgraded.qa_result.qa_result_id != first.qa_result.qa_result_id
    assert len(_qa_files(parts)) == 2
    still_there = parts["qa_repository"].get(
        subject_id="subject-a",
        qa_result_id=first.qa_result.qa_result_id,
    )
    assert still_there.qa_result == first.qa_result


@pytest.mark.asyncio
async def test_restart_reads_identical_result_and_isolates_subjects(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)
    first = await _run_qa(parts, draft, _FakeQAAgent())

    restarted = PrivateHomeResumeFactQARepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(
        subject_id="subject-a",
        qa_result_id=first.qa_result.qa_result_id,
    )
    cross = restarted.get(
        subject_id="subject-b",
        qa_result_id=first.qa_result.qa_result_id,
    )

    assert read.status is ResumeFactQAReadStatus.FOUND
    assert read.qa_result == first.qa_result
    assert read.qa_result.qa_content_hash == first.qa_result.qa_content_hash
    assert cross.status is ResumeFactQAReadStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_repository_conflict_and_corruption_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)
    first = await _run_qa(parts, draft, _FakeQAAgent())
    qa_result = first.qa_result
    record = next(
        parts["home"].paths.resume_fact_qa_results.rglob(
            f"{qa_result.qa_result_id}.json"
        )
    )
    corrupted = b"{broken"
    record.write_bytes(corrupted)

    conflict = parts["qa_repository"].save(qa_result)
    read = parts["qa_repository"].get(
        subject_id=qa_result.subject_id,
        qa_result_id=qa_result.qa_result_id,
    )

    assert conflict.status is ResumeFactQAWriteStatus.FAILED
    assert (
        conflict.reason_code
        is ResumeFactQAFailureReason.QA_RESULT_INTEGRITY_FAILURE
    )
    assert record.read_bytes() == corrupted
    assert read.status is ResumeFactQAReadStatus.INTEGRITY_FAILURE


@pytest.mark.asyncio
async def test_invalid_command_and_missing_draft_fail_without_result(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    draft = await _tailored_draft(parts)
    agent = _FakeQAAgent()

    naive = await _run_qa(
        parts, draft, agent, now=datetime(2026, 7, 28, 22, 0)
    )
    missing = await run_resume_fact_qa(
        RunResumeFactQACommand(
            subject_id="subject-a",
            tailored_resume_draft_id="tailored-resume-draft-" + "0" * 64,
            now=NOW,
        ),
        draft_repository=parts["draft_repository"],
        application_plan_repository=parts["plan_repository"],
        job_repository=parts["job_repository"],
        selection_repository=parts["selection_repository"],
        projection_repository=parts["projection_repository"],
        evidence_snapshot_repository=parts["snapshot_repository"],
        agent=agent,
        metadata=QA_METADATA,
        qa_repository=parts["qa_repository"],
    )

    assert naive.reason_code is ResumeFactQAFailureReason.INVALID_REQUEST
    assert missing.reason_code is ResumeFactQAFailureReason.DRAFT_NOT_FOUND
    assert agent.contexts == []
    assert not _qa_files(parts)


def test_module_has_no_rendering_visual_qa_browser_or_execution_dependency() -> None:
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
        "core.profile_store",
        "core.priority_agent_adapter",
        "playwright",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )


def test_qa_does_not_reuse_the_tailoring_validator() -> None:
    module_path = Path(fact_qa_module.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").endswith("resume_tailoring")
        for alias in node.names
    }

    assert "_validate_agent_sections" not in imported_names
    assert not any(name.startswith("_") for name in imported_names)
