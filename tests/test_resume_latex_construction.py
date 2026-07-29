from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import core.resume_latex_construction as construction_module
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.base_latex_selection import (
    BaseLatexSelectionAgentMetadata,
    BaseLatexSelectionKind,
    BaseLatexSelectionStatus,
    PrivateHomeBaseLatexSelectionDecisionRepository,
    SelectBaseLatexVersionCommand,
    select_base_latex_version,
)
from core.candidate_evidence import (
    CreateCandidateEvidenceSnapshotCommand,
    PrivateHomeCandidateEvidenceSnapshotRepository,
    create_candidate_evidence_snapshot,
)
from core.job_discovery import JobPosting
from core.job_prioritization import ProposedPriorityLevel
from core.managed_resume_template import (
    MANAGED_RESUME_TEMPLATE_ID,
    DefaultManagedResumeTemplateProvider,
)
from core.private_home import PrivateHome
from core.resume_candidates import (
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
    ResumeSummarySource,
    ResumeSummaryTrust,
    register_resume_candidate,
)
from core.resume_fact_qa import (
    PrivateHomeResumeFactQARepository,
    ResumeFactQAAgentMetadata,
    ResumeFactQAAgentOutput,
    ResumeFactQAAgentVerdict,
    ResumeFactQAVerdict,
    RunResumeFactQACommand,
    run_resume_fact_qa,
)
from core.resume_latex_construction import (
    ConstructResumeLatexCommand,
    PrivateHomeResumeLatexConstructionRecordRepository,
    ResumeLatexConstructionAgentMetadata,
    ResumeLatexConstructionAgentOutput,
    ResumeLatexConstructionAgentUnavailableError,
    ResumeLatexConstructionContext,
    ResumeLatexConstructionFailureReason,
    ResumeLatexConstructionMethod,
    ResumeLatexConstructionPath,
    ResumeLatexConstructionStatus,
    construct_resume_latex_version,
    render_controlled_region,
)
from core.resume_latex_markers import (
    JOBOPS_CONTENT_BEGIN,
    JOBOPS_CONTENT_END,
    MARKER_MACRO_DEFINITIONS,
    escape_latex,
    parse_markers,
    split_controlled_region,
)
from core.resume_latex_versions import (
    PrivateHomeResumeLatexVersionRepository,
    RegisterResumeLatexVersionCommand,
    ResumeLatexSourceKind,
    register_resume_latex_version,
)
from core.resume_selection import (
    RESUME_SELECTION_CONTRACT_VERSION,
    PrivateHomeResumeSelectionDecisionRepository,
    ResumeSelectionDecision,
    ResumeSelectionMethod,
)
from core.resume_tailoring import (
    PrivateHomeTailoredResumeDraftRepository,
    ResumeTailoringAgentDisposition,
    ResumeTailoringAgentMetadata,
    ResumeTailoringAgentOutput,
    TailorResumeCommand,
    TailoredBulletChangeType,
    TailoredBulletProposal,
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


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
METADATA = ResumeLatexConstructionAgentMetadata(
    agent_version="resume-latex-construction-agent-v1",
    prompt_version="resume-latex-construction-prompt-v1",
    model_id="synthetic-construction-model",
)
JOB_DESCRIPTION = (
    "Requirements: Streamlined geospatial data pipelines in Python. "
    "Responsibilities: deliver reproducible satellite imagery processing "
    "and maintain deterministic workflows."
)
REWRITTEN = (
    "Built deterministic geospatial pipelines in Python for satellite "
    "datasets."
)
LEGACY_BULLET = (
    "Managed a legacy warehouse migration for the retail analytics group "
    "across three regions."
)
UNMARKED_LATEX = (
    "\\documentclass[11pt]{article}\n"
    "\\usepackage{geometry}\n"
    "\\begin{document}\n"
    "\\textbf{Alex Candidate}\\\\\n"
    "\\section*{Experience}\n"
    "\\begin{itemize}\n"
    f"\\item {LEGACY_BULLET}\n"
    "\\end{itemize}\n"
    "\\end{document}\n"
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


class _StaticAgent:
    def __init__(self, output) -> None:
        self.output = output

    async def tailor(self, _context):
        return self.output

    async def review(self, _context):
        return ResumeFactQAAgentOutput(
            verdict=ResumeFactQAAgentVerdict.SUPPORTED,
            findings=(),
        )

    async def evaluate(self, _context):
        raise AssertionError("selection Agent must not be called")


class _FakeConstructionAgent:
    def __init__(self, output=None) -> None:
        self.output = output
        self.contexts: list[ResumeLatexConstructionContext] = []

    async def construct(self, context: ResumeLatexConstructionContext):
        self.contexts.append(context)
        if isinstance(self.output, Exception):
            raise self.output
        if callable(self.output):
            produced = self.output(context)
            if not isinstance(produced, str):
                return produced
            return ResumeLatexConstructionAgentOutput(latex_source=produced)
        if self.output is None:
            return ResumeLatexConstructionAgentOutput(
                latex_source=_faithful_reconstruction(context)
            )
        return self.output


def _faithful_reconstruction(
    context: ResumeLatexConstructionContext,
) -> str:
    """A well-behaved Agent: same layout, Draft content, no stale bullets."""

    return (
        "\\documentclass[11pt]{article}\n"
        "\\usepackage{geometry}\n"
        f"{MARKER_MACRO_DEFINITIONS}"
        "\\begin{document}\n"
        "\\textbf{Alex Candidate}\\\\\n"
        f"{JOBOPS_CONTENT_BEGIN}\n"
        f"{render_controlled_region(context.sections)}"
        f"{JOBOPS_CONTENT_END}\n"
        "\\end{document}\n"
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
    )
    plan_repository = PrivateHomeApplicationPlanRepository(home)
    plan_repository.save(plan)

    artifact = home.paths.master_documents / f"{subject_id}.docx"
    artifact.write_bytes(_docx_bytes())
    candidate_repository = PrivateHomeResumeCandidateRepository(home)
    candidate = register_resume_candidate(
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
    ).candidate
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

    job_repository = _JobRepository(
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
    )

    section = projection.sections[0]
    heading, bullet, paragraph = section.blocks
    evidence = {
        item.source_block_id: item.evidence_id
        for item in snapshot.evidence_items
    }
    draft_repository = PrivateHomeTailoredResumeDraftRepository(home)
    draft = (
        await tailor_resume(
            TailorResumeCommand(
                subject_id=subject_id,
                application_plan_id=plan.plan_id,
                evidence_snapshot_id=snapshot.snapshot_id,
                now=NOW,
            ),
            application_plan_repository=plan_repository,
            job_repository=job_repository,
            selection_repository=selection_repository,
            candidate_repository=candidate_repository,
            projection_repository=projection_repository,
            evidence_snapshot_repository=snapshot_repository,
            agent=_StaticAgent(
                ResumeTailoringAgentOutput(
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
                                    change_type=(
                                        TailoredBulletChangeType.UNCHANGED
                                    ),
                                    text=heading.text,
                                    evidence_ids=(),
                                    jd_alignment=(),
                                ),
                                TailoredBulletProposal(
                                    source_section_id=section.section_id,
                                    source_block_id=bullet.block_id,
                                    source_bullet_id=bullet.bullet_id,
                                    change_type=(
                                        TailoredBulletChangeType.REWRITTEN
                                    ),
                                    text=REWRITTEN,
                                    evidence_ids=(
                                        evidence[bullet.block_id],
                                        evidence[paragraph.block_id],
                                    ),
                                    jd_alignment=(
                                        "geospatial data pipelines in Python",
                                    ),
                                ),
                                TailoredBulletProposal(
                                    source_section_id=section.section_id,
                                    source_block_id=paragraph.block_id,
                                    source_bullet_id=paragraph.bullet_id,
                                    change_type=(
                                        TailoredBulletChangeType.OMITTED
                                    ),
                                    text=None,
                                    evidence_ids=(),
                                    jd_alignment=(),
                                ),
                            ),
                        ),
                    ),
                    rationale="Tightened toward the geospatial JD.",
                )
            ),
            metadata=ResumeTailoringAgentMetadata(
                agent_version="resume-tailoring-agent-v1",
                prompt_version="resume-tailoring-prompt-v1",
                model_id="synthetic-tailoring-model",
            ),
            draft_repository=draft_repository,
        )
    ).draft
    assert draft is not None

    qa_repository = PrivateHomeResumeFactQARepository(home)
    qa = await run_resume_fact_qa(
        RunResumeFactQACommand(
            subject_id=subject_id,
            tailored_resume_draft_id=draft.draft_id,
            now=NOW,
        ),
        draft_repository=draft_repository,
        application_plan_repository=plan_repository,
        job_repository=job_repository,
        selection_repository=selection_repository,
        projection_repository=projection_repository,
        evidence_snapshot_repository=snapshot_repository,
        agent=_StaticAgent(None),
        metadata=ResumeFactQAAgentMetadata(
            agent_version="resume-fact-qa-agent-v1",
            prompt_version="resume-fact-qa-prompt-v1",
            model_id="synthetic-qa-model",
        ),
        qa_repository=qa_repository,
    )
    assert qa.qa_result.verdict is ResumeFactQAVerdict.PASSED

    return {
        "home": home,
        "plan": plan,
        "plan_repository": plan_repository,
        "candidate": candidate,
        "selection_repository": selection_repository,
        "job_repository": job_repository,
        "draft": draft,
        "draft_repository": draft_repository,
        "qa_result": qa.qa_result,
        "qa_repository": qa_repository,
        "latex_repository": PrivateHomeResumeLatexVersionRepository(home),
        "base_repository": (
            PrivateHomeBaseLatexSelectionDecisionRepository(home)
        ),
        "construction_repository": (
            PrivateHomeResumeLatexConstructionRecordRepository(home)
        ),
        "template_provider": DefaultManagedResumeTemplateProvider(),
    }


async def _base_decision(parts):
    result = await select_base_latex_version(
        SelectBaseLatexVersionCommand(
            subject_id=parts["plan"].subject_id,
            application_plan_id=parts["plan"].plan_id,
            fact_qa_result_id=parts["qa_result"].qa_result_id,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        fact_qa_repository=parts["qa_repository"],
        draft_repository=parts["draft_repository"],
        selection_repository=parts["selection_repository"],
        job_repository=parts["job_repository"],
        latex_version_provider=parts["latex_repository"],
        agent=_StaticAgent(None),
        metadata=BaseLatexSelectionAgentMetadata(
            agent_version="base-latex-selection-agent-v1",
            prompt_version="base-latex-selection-prompt-v1",
            model_id="synthetic-base-latex-model",
        ),
        decision_repository=parts["base_repository"],
    )
    assert result.status is BaseLatexSelectionStatus.CREATED, result.reason_code
    return result.decision


def _add_base_version(parts, source: str, *, bind_source_resume: bool = True):
    result = register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id=parts["plan"].subject_id,
            source_kind=ResumeLatexSourceKind.USER_PROVIDED,
            now=NOW,
            latex_source=source,
            source_resume_id=(
                parts["candidate"].resume_id if bind_source_resume else None
            ),
        ),
        home=parts["home"],
        repository=parts["latex_repository"],
    )
    assert result.version is not None, result.reason_code
    return result.version


async def _construct(
    parts,
    decision,
    agent=None,
    *,
    subject_id: str | None = None,
    now: datetime = NOW,
    fact_qa_result_id: str | None = None,
):
    return await construct_resume_latex_version(
        ConstructResumeLatexCommand(
            subject_id=subject_id or parts["plan"].subject_id,
            application_plan_id=parts["plan"].plan_id,
            base_latex_selection_decision_id=decision.decision_id,
            fact_qa_result_id=(
                fact_qa_result_id or parts["qa_result"].qa_result_id
            ),
            now=now,
        ),
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        fact_qa_repository=parts["qa_repository"],
        base_selection_repository=parts["base_repository"],
        latex_version_repository=parts["latex_repository"],
        template_provider=parts["template_provider"],
        agent=agent if agent is not None else _FakeConstructionAgent(),
        metadata=METADATA,
        construction_repository=parts["construction_repository"],
        home=parts["home"],
    )


def _read_source(parts, version) -> str:
    return parts["home"].contained_path(
        version.source_reference
    ).read_text(encoding="utf-8")


def _marked_base_source(parts) -> str:
    """A base version that already speaks the controlled marker contract."""

    return (
        "\\documentclass[12pt]{article}\n"
        "\\usepackage{geometry}\n"
        "\\usepackage{xcolor}\n"
        f"{MARKER_MACRO_DEFINITIONS}"
        "\\begin{document}\n"
        "\\textbf{Alex Candidate}\\\\\n"
        f"{JOBOPS_CONTENT_BEGIN}\n"
        "\\JobopsSection{resume-section-old}{Legacy Experience}\n"
        "\\begin{itemize}\n"
        f"\\JobopsBullet{{resume-block-old}}{{{LEGACY_BULLET}}}\n"
        "\\end{itemize}\n"
        f"{JOBOPS_CONTENT_END}\n"
        "\\end{document}\n"
    )


@pytest.mark.asyncio
async def test_managed_fallback_renders_deterministically_without_agent(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    decision = await _base_decision(parts)
    agent = _FakeConstructionAgent()

    assert decision.selection_kind is (
        BaseLatexSelectionKind.MANAGED_TEMPLATE_FALLBACK
    )

    result = await _construct(parts, decision, agent)

    assert result.status is ResumeLatexConstructionStatus.CREATED
    version = result.version
    assert version.source_kind is (
        ResumeLatexSourceKind.SYSTEM_TEMPLATE_DERIVED
    )
    assert version.parent_version_id is None
    assert version.template_id == MANAGED_RESUME_TEMPLATE_ID
    assert version.template_sha256 is not None
    assert version.tailored_resume_draft_id == parts["draft"].draft_id
    assert (
        version.tailored_resume_draft_hash
        == parts["draft"].draft_content_hash
    )
    assert version.fact_qa_result_id == parts["qa_result"].qa_result_id
    assert agent.contexts == []
    record = result.record
    assert record.construction_path is (
        ResumeLatexConstructionPath.MANAGED_TEMPLATE
    )
    assert record.construction_method is (
        ResumeLatexConstructionMethod.DETERMINISTIC_TEMPLATE_RENDER
    )
    assert record.agent_invoked is False


@pytest.mark.asyncio
async def test_every_draft_section_and_bullet_appears_exactly_once(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    decision = await _base_decision(parts)

    result = await _construct(parts, decision)

    source = _read_source(parts, result.version)
    _, region, _ = split_controlled_region(source)
    markers = parse_markers(region)
    draft_section = parts["draft"].sections[0]
    retained = [
        bullet
        for bullet in draft_section.bullets
        if bullet.change_type is not TailoredBulletChangeType.OMITTED
    ]
    assert len(markers) == 1 + len(retained)
    assert markers[0].macro == "JobopsSection"
    assert markers[0].marker_id == draft_section.source_section_id
    bullet_markers = markers[1:]
    assert [item.marker_id for item in bullet_markers] == [
        bullet.source_block_id for bullet in retained
    ]
    assert [item.text for item in bullet_markers] == [
        escape_latex(bullet.text) for bullet in retained
    ]
    assert source.count(escape_latex(REWRITTEN)) == 1


@pytest.mark.asyncio
async def test_omitted_bullets_are_not_written_into_latex(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    decision = await _base_decision(parts)
    omitted = [
        bullet
        for bullet in parts["draft"].sections[0].bullets
        if bullet.change_type is TailoredBulletChangeType.OMITTED
    ]
    assert omitted

    result = await _construct(parts, decision)

    source = _read_source(parts, result.version)
    for bullet in omitted:
        assert bullet.source_block_id not in source


@pytest.mark.asyncio
async def test_marked_base_version_derives_deterministically(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    base = _add_base_version(parts, _marked_base_source(parts))
    decision = await _base_decision(parts)
    agent = _FakeConstructionAgent()

    assert decision.selection_kind is BaseLatexSelectionKind.EXISTING_VERSION

    result = await _construct(parts, decision, agent)

    assert result.status is ResumeLatexConstructionStatus.CREATED
    assert agent.contexts == []
    version = result.version
    assert version.source_kind is ResumeLatexSourceKind.AI_REVISED
    assert version.parent_version_id == base.latex_version_id
    assert version.root_family_id == base.root_family_id
    assert version.template_id is None
    assert result.record.construction_method is (
        ResumeLatexConstructionMethod.DETERMINISTIC_REGION_REPLACEMENT
    )
    source = _read_source(parts, version)
    assert "\\usepackage{xcolor}" in source
    assert "\\documentclass[12pt]{article}" in source
    assert "Alex Candidate" in source
    assert LEGACY_BULLET not in source
    assert "resume-block-old" not in source
    assert escape_latex(REWRITTEN) in source


@pytest.mark.asyncio
async def test_unmarked_base_version_uses_one_agent_call(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    base = _add_base_version(parts, UNMARKED_LATEX)
    decision = await _base_decision(parts)
    agent = _FakeConstructionAgent()

    result = await _construct(parts, decision, agent)

    assert result.status is ResumeLatexConstructionStatus.CREATED
    assert len(agent.contexts) == 1
    assert result.record.construction_method is (
        ResumeLatexConstructionMethod.AGENT_RECONSTRUCTED
    )
    assert result.record.agent_invoked is True
    version = result.version
    assert version.parent_version_id == base.latex_version_id
    assert version.root_family_id == base.root_family_id
    source = _read_source(parts, version)
    assert LEGACY_BULLET not in source
    assert escape_latex(REWRITTEN) in source


@pytest.mark.asyncio
async def test_agent_receives_only_base_source_draft_and_policy(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_base_version(parts, UNMARKED_LATEX)
    decision = await _base_decision(parts)
    agent = _FakeConstructionAgent()

    await _construct(parts, decision, agent)

    context = agent.contexts[0]
    assert context.base_latex_source == UNMARKED_LATEX
    assert context.tailored_resume_draft_id == parts["draft"].draft_id
    assert context.user_preparation_instructions is None
    assert context.marker_contract["section_macro"].startswith(
        "\\JobopsSection"
    )
    assert context.agent_policy.startswith("LaTeX Construction Agent policy")
    assert "Reword" in context.agent_policy
    texts = {
        bullet.text
        for section in context.sections
        for bullet in section.bullets
    }
    assert REWRITTEN in texts
    assert not hasattr(context, "evidence_items")
    assert not hasattr(context, "job")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        "reword",
        "extra_content",
        "drop_bullet",
        "unknown_marker",
        "stale_content",
        "dangerous",
        "no_region",
        "untyped",
    ],
)
async def test_unsafe_agent_output_defers_for_human(
    tmp_path: Path, mutate: str
) -> None:
    parts = await _setup(tmp_path)
    _add_base_version(parts, UNMARKED_LATEX)
    decision = await _base_decision(parts)

    def build(context) -> str:
        good = _faithful_reconstruction(context)
        if mutate == "reword":
            return good.replace(
                escape_latex(REWRITTEN),
                escape_latex(REWRITTEN.replace("Built", "Led")),
            )
        if mutate == "extra_content":
            return good.replace(
                JOBOPS_CONTENT_END,
                "\\JobopsBullet{resume-block-extra}{Invented achievement}\n"
                + JOBOPS_CONTENT_END,
            )
        if mutate == "drop_bullet":
            lines = [
                line
                for line in good.splitlines(keepends=True)
                if "JobopsBullet" not in line
            ]
            return "".join(lines)
        if mutate == "unknown_marker":
            return good.replace(
                "\\JobopsSection{", "\\JobopsSection{unknown-", 1
            )
        if mutate == "stale_content":
            return good.replace(
                JOBOPS_CONTENT_BEGIN,
                f"\\textit{{{LEGACY_BULLET}}}\n" + JOBOPS_CONTENT_BEGIN,
            )
        if mutate == "dangerous":
            return good.replace(
                "\\begin{document}",
                "\\immediate\\write18{curl http://x}\n\\begin{document}",
            )
        return good.replace(JOBOPS_CONTENT_BEGIN, "").replace(
            JOBOPS_CONTENT_END, ""
        )

    agent = (
        _FakeConstructionAgent(lambda _c: {"latex": "free text"})
        if mutate == "untyped"
        else _FakeConstructionAgent(build)
    )

    result = await _construct(parts, decision, agent)

    assert result.status is (
        ResumeLatexConstructionStatus.DEFERRED_NEEDS_HUMAN
    )
    assert (
        result.reason_code
        is ResumeLatexConstructionFailureReason.CONSTRUCTION_OUTPUT_UNSAFE
    )
    assert result.version is None
    assert not result.retryable
    assert not tuple(
        parts["home"].paths.resume_latex_constructions.rglob("*.json")
    )


@pytest.mark.asyncio
async def test_blocked_fact_qa_cannot_reach_construction(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    decision = await _base_decision(parts)
    passed = parts["qa_result"]
    blocked = object.__new__(type(passed))
    for field in type(passed).__dataclass_fields__:
        object.__setattr__(blocked, field, getattr(passed, field))
    object.__setattr__(blocked, "verdict", ResumeFactQAVerdict.BLOCKED)

    class _BlockedRepository:
        def get(self, **_kwargs):
            from core.resume_fact_qa import (
                ResumeFactQAReadResult,
                ResumeFactQAReadStatus,
            )

            return ResumeFactQAReadResult(
                status=ResumeFactQAReadStatus.FOUND,
                qa_result=blocked,
            )

    parts["qa_repository"] = _BlockedRepository()
    agent = _FakeConstructionAgent()

    result = await _construct(parts, decision, agent)

    assert result.status is ResumeLatexConstructionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLatexConstructionFailureReason.FACT_QA_NOT_PASSED
    )
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_base_selection_bound_to_another_draft_fails_closed(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    decision = await _base_decision(parts)
    drifted = object.__new__(type(decision))
    for field in type(decision).__dataclass_fields__:
        object.__setattr__(drifted, field, getattr(decision, field))
    object.__setattr__(drifted, "tailored_resume_draft_hash", "f" * 64)

    class _DriftedRepository:
        def get(self, **_kwargs):
            from core.base_latex_selection import (
                BaseLatexSelectionReadResult,
                BaseLatexSelectionReadStatus,
            )

            return BaseLatexSelectionReadResult(
                status=BaseLatexSelectionReadStatus.FOUND,
                decision=drifted,
            )

    parts["base_repository"] = _DriftedRepository()
    agent = _FakeConstructionAgent()

    result = await _construct(parts, decision, agent)

    assert result.status is ResumeLatexConstructionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLatexConstructionFailureReason
        .BASE_SELECTION_BINDING_MISMATCH
    )
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_missing_base_source_defers_without_substitution(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    base = _add_base_version(parts, _marked_base_source(parts))
    other = _add_base_version(
        parts,
        _marked_base_source(parts).replace("12pt", "11pt"),
        bind_source_resume=False,
    )
    decision = await _base_decision(parts)
    parts["home"].contained_path(base.source_reference).unlink()
    agent = _FakeConstructionAgent()

    result = await _construct(parts, decision, agent)

    assert result.status is (
        ResumeLatexConstructionStatus.DEFERRED_SOURCE_UNREADABLE
    )
    assert result.version is None
    assert agent.contexts == []
    assert other.latex_version_id != base.latex_version_id


@pytest.mark.asyncio
async def test_replay_returns_unchanged_with_zero_extra_agent_calls(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_base_version(parts, UNMARKED_LATEX)
    decision = await _base_decision(parts)
    agent = _FakeConstructionAgent()

    first = await _construct(parts, decision, agent)
    replay = await _construct(
        parts, decision, agent, now=NOW + timedelta(days=2)
    )

    assert first.status is ResumeLatexConstructionStatus.CREATED
    assert replay.status is ResumeLatexConstructionStatus.UNCHANGED
    assert replay.version.latex_version_id == (
        first.version.latex_version_id
    )
    assert replay.record.constructed_at == NOW
    assert len(agent.contexts) == 1
    assert len(
        tuple(parts["home"].paths.resume_latex_version_sources.rglob("*.tex"))
    ) == 2


@pytest.mark.asyncio
async def test_changed_template_binding_creates_a_new_version(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    decision = await _base_decision(parts)
    first = await _construct(parts, decision)

    class _AltTemplateProvider:
        def get(self):
            from core.managed_resume_template import ManagedResumeTemplate

            preamble = (
                "\\documentclass[10pt]{article}\n"
                f"{MARKER_MACRO_DEFINITIONS}"
                "\\begin{document}\n"
            )
            postamble = "\\end{document}\n"
            return ManagedResumeTemplate(
                template_id="managed-resume-compact-test",
                template_sha256=hashlib.sha256(
                    (preamble + postamble).encode("utf-8")
                ).hexdigest(),
                preamble=preamble,
                postamble=postamble,
            )

    parts["template_provider"] = _AltTemplateProvider()
    second = await _construct(
        parts, decision, now=NOW + timedelta(minutes=1)
    )

    assert second.status is ResumeLatexConstructionStatus.CREATED
    assert second.version.latex_version_id != (
        first.version.latex_version_id
    )
    assert second.construction_binding != first.construction_binding
    kept = parts["latex_repository"].get(
        subject_id="subject-a",
        latex_version_id=first.version.latex_version_id,
    )
    assert kept.version == first.version


@pytest.mark.asyncio
async def test_agent_unavailable_fails_retryable_without_version(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_base_version(parts, UNMARKED_LATEX)
    decision = await _base_decision(parts)

    result = await _construct(
        parts,
        decision,
        _FakeConstructionAgent(
            ResumeLatexConstructionAgentUnavailableError("offline")
        ),
    )

    assert result.status is ResumeLatexConstructionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLatexConstructionFailureReason.AGENT_UNAVAILABLE
    )
    assert result.retryable
    assert result.version is None


@pytest.mark.asyncio
async def test_restart_preserves_version_lineage_and_provenance(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    base = _add_base_version(parts, _marked_base_source(parts))
    decision = await _base_decision(parts)
    first = await _construct(parts, decision)

    restarted_versions = PrivateHomeResumeLatexVersionRepository(
        PrivateHome(parts["home"].root)
    )
    restarted_records = (
        PrivateHomeResumeLatexConstructionRecordRepository(
            PrivateHome(parts["home"].root)
        )
    )
    version_read = restarted_versions.get(
        subject_id="subject-a",
        latex_version_id=first.version.latex_version_id,
    )
    record_read = restarted_records.get(
        subject_id="subject-a", record_id=first.record.record_id
    )

    assert version_read.version == first.version
    assert version_read.version.parent_version_id == base.latex_version_id
    assert version_read.version.root_family_id == base.root_family_id
    assert record_read.record == first.record
    assert (
        record_read.record.latex_source_sha256
        == first.version.source_sha256
    )


@pytest.mark.asyncio
async def test_corrupt_construction_record_fails_closed(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    decision = await _base_decision(parts)
    first = await _construct(parts, decision)
    record_path = next(
        parts["home"].paths.resume_latex_constructions.rglob(
            f"{first.record.record_id}.json"
        )
    )
    corrupted = b"{broken"
    record_path.write_bytes(corrupted)

    result = await _construct(parts, decision, now=NOW + timedelta(hours=1))

    assert result.status is ResumeLatexConstructionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLatexConstructionFailureReason.RECORD_INTEGRITY_FAILURE
    )
    assert record_path.read_bytes() == corrupted


@pytest.mark.asyncio
async def test_invalid_command_and_missing_selection_fail_closed(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    decision = await _base_decision(parts)
    agent = _FakeConstructionAgent()

    naive = await _construct(
        parts, decision, agent, now=datetime(2026, 7, 29, 12, 0)
    )
    missing = await construct_resume_latex_version(
        ConstructResumeLatexCommand(
            subject_id="subject-a",
            application_plan_id=parts["plan"].plan_id,
            base_latex_selection_decision_id=(
                "base-latex-selection-" + "0" * 64
            ),
            fact_qa_result_id=parts["qa_result"].qa_result_id,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        fact_qa_repository=parts["qa_repository"],
        base_selection_repository=parts["base_repository"],
        latex_version_repository=parts["latex_repository"],
        template_provider=parts["template_provider"],
        agent=agent,
        metadata=METADATA,
        construction_repository=parts["construction_repository"],
        home=parts["home"],
    )

    assert (
        naive.reason_code
        is ResumeLatexConstructionFailureReason.INVALID_REQUEST
    )
    assert (
        missing.reason_code
        is ResumeLatexConstructionFailureReason.BASE_SELECTION_NOT_FOUND
    )
    assert agent.contexts == []


def test_latex_escaping_is_faithful_and_reversible_in_meaning() -> None:
    assert escape_latex("100% & $5 #1 _x {y}") == (
        "100\\% \\& \\$5 \\#1 \\_x \\{y\\}"
    )
    assert escape_latex("C\\C") == "C\\textbackslash{}C"


def test_module_never_compiles_latex_or_touches_execution() -> None:
    module_path = Path(construction_module.__file__)
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
        "subprocess",
        "shutil",
        "os",
        "core.application_engine",
        "core.browser_broker",
        "core.materials",
        "playwright",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
