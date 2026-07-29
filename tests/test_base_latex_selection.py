from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import core.base_latex_selection as selection_module
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.base_latex_selection import (
    BASE_LATEX_SELECTION_CONTRACT_VERSION,
    BaseLatexSelectionAgentDisposition,
    BaseLatexSelectionAgentMetadata,
    BaseLatexSelectionAgentOutput,
    BaseLatexSelectionAgentUnavailableError,
    BaseLatexSelectionContext,
    BaseLatexSelectionFailureReason,
    BaseLatexSelectionKind,
    BaseLatexSelectionMethod,
    BaseLatexSelectionReadStatus,
    BaseLatexSelectionStatus,
    BaseLatexSelectionWriteStatus,
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
    ResumeFactQAStatus,
    ResumeFactQAVerdict,
    RunResumeFactQACommand,
    run_resume_fact_qa,
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


NOW = datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc)
METADATA = BaseLatexSelectionAgentMetadata(
    agent_version="base-latex-selection-agent-v1",
    prompt_version="base-latex-selection-prompt-v1",
    model_id="synthetic-base-latex-model",
)
JOB_DESCRIPTION = (
    "Requirements: Streamlined geospatial data pipelines in Python. "
    "Responsibilities: deliver reproducible satellite imagery processing "
    "and maintain deterministic workflows."
)
LATEX = r"""\documentclass[11pt]{article}
\begin{document}
\section*{Experience}
\item Built deterministic geospatial pipelines.
\end{document}
"""
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


class _FakeSelectionAgent:
    def __init__(self, output=None) -> None:
        self.output = output
        self.contexts: list[BaseLatexSelectionContext] = []

    async def evaluate(self, context: BaseLatexSelectionContext):
        self.contexts.append(context)
        if isinstance(self.output, Exception):
            raise self.output
        if callable(self.output):
            return self.output(context)
        if self.output is None:
            return BaseLatexSelectionAgentOutput(
                disposition=BaseLatexSelectionAgentDisposition.SELECTED,
                selected_latex_version_id=(
                    context.candidates[0].latex_version_id
                ),
                rationale="The first candidate fits the target role best.",
            )
        return self.output


class _StaticTailoringAgent:
    def __init__(self, output) -> None:
        self.output = output

    async def tailor(self, _context):
        return self.output


class _PassingFactQAAgent:
    async def review(self, _context):
        return ResumeFactQAAgentOutput(
            verdict=ResumeFactQAAgentVerdict.SUPPORTED,
            findings=(),
        )


async def _setup(
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
            agent=_StaticTailoringAgent(
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
                                    text=(
                                        "Built deterministic geospatial "
                                        "pipelines in Python for satellite "
                                        "datasets."
                                    ),
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
                                        TailoredBulletChangeType.UNCHANGED
                                    ),
                                    text=paragraph.text,
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
        agent=_PassingFactQAAgent(),
        metadata=ResumeFactQAAgentMetadata(
            agent_version="resume-fact-qa-agent-v1",
            prompt_version="resume-fact-qa-prompt-v1",
            model_id="synthetic-qa-model",
        ),
        qa_repository=qa_repository,
    )
    assert qa.status is ResumeFactQAStatus.CREATED
    assert qa.qa_result.verdict is ResumeFactQAVerdict.PASSED

    return {
        "home": home,
        "plan": plan,
        "plan_repository": plan_repository,
        "candidate": candidate,
        "selection": selection,
        "selection_repository": selection_repository,
        "job_repository": job_repository,
        "draft": draft,
        "draft_repository": draft_repository,
        "qa_result": qa.qa_result,
        "qa_repository": qa_repository,
        "latex_repository": PrivateHomeResumeLatexVersionRepository(home),
        "decision_repository": (
            PrivateHomeBaseLatexSelectionDecisionRepository(home)
        ),
    }


def _add_version(
    parts,
    *,
    marker: str,
    source_kind: ResumeLatexSourceKind = ResumeLatexSourceKind.USER_PROVIDED,
    source_resume_id: str | None = None,
    labels: tuple[str, ...] = (),
    subject_id: str = "subject-a",
    fact_qa_result_id: str | None = None,
    fact_qa_result_hash: str | None = None,
    tailored_resume_draft_id: str | None = None,
    tailored_resume_draft_hash: str | None = None,
):
    result = register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id=subject_id,
            source_kind=source_kind,
            now=NOW,
            latex_source=LATEX.replace("Experience", f"Experience {marker}"),
            source_resume_id=source_resume_id,
            labels=labels,
            fact_qa_result_id=fact_qa_result_id,
            fact_qa_result_hash=fact_qa_result_hash,
            tailored_resume_draft_id=tailored_resume_draft_id,
            tailored_resume_draft_hash=tailored_resume_draft_hash,
        ),
        home=parts["home"],
        repository=parts["latex_repository"],
    )
    assert result.version is not None, result.reason_code
    return result.version


async def _select(
    parts,
    agent=None,
    *,
    subject_id: str = "subject-a",
    fact_qa_result_id: str | None = None,
    now: datetime = NOW,
    metadata: BaseLatexSelectionAgentMetadata = METADATA,
):
    return await select_base_latex_version(
        SelectBaseLatexVersionCommand(
            subject_id=subject_id,
            application_plan_id=parts["plan"].plan_id,
            fact_qa_result_id=(
                fact_qa_result_id or parts["qa_result"].qa_result_id
            ),
            now=now,
        ),
        application_plan_repository=parts["plan_repository"],
        fact_qa_repository=parts["qa_repository"],
        draft_repository=parts["draft_repository"],
        selection_repository=parts["selection_repository"],
        job_repository=parts["job_repository"],
        latex_version_provider=parts["latex_repository"],
        agent=agent if agent is not None else _FakeSelectionAgent(),
        metadata=metadata,
        decision_repository=parts["decision_repository"],
    )


def _decisions(parts) -> tuple[Path, ...]:
    return tuple(parts["home"].paths.base_latex_selections.rglob("*.json"))


@pytest.mark.asyncio
async def test_no_candidate_falls_back_to_managed_template(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeSelectionAgent()

    result = await _select(parts, agent)

    assert result.status is BaseLatexSelectionStatus.CREATED
    decision = result.decision
    assert decision.selection_kind is (
        BaseLatexSelectionKind.MANAGED_TEMPLATE_FALLBACK
    )
    assert decision.selection_method is (
        BaseLatexSelectionMethod.MANAGED_TEMPLATE_FALLBACK
    )
    assert decision.selected_latex_version_id is None
    assert decision.selected_root_family_id is None
    assert decision.agent_invoked is False
    assert agent.contexts == []
    assert decision.contract_version == BASE_LATEX_SELECTION_CONTRACT_VERSION
    assert decision.selected_at == NOW


@pytest.mark.asyncio
async def test_single_candidate_is_chosen_deterministically(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    version = _add_version(parts, marker="only")
    agent = _FakeSelectionAgent()

    result = await _select(parts, agent)

    decision = result.decision
    assert decision.selection_kind is BaseLatexSelectionKind.EXISTING_VERSION
    assert decision.selection_method is (
        BaseLatexSelectionMethod.ONLY_CANDIDATE
    )
    assert decision.selected_latex_version_id == version.latex_version_id
    assert decision.selected_latex_source_sha256 == version.source_sha256
    assert decision.selected_root_family_id == version.root_family_id
    assert decision.agent_invoked is False
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_unique_source_resume_binding_wins_without_agent(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_version(parts, marker="unrelated-one")
    _add_version(parts, marker="unrelated-two")
    bound = _add_version(
        parts,
        marker="bound",
        source_resume_id=parts["candidate"].resume_id,
    )
    agent = _FakeSelectionAgent()

    result = await _select(parts, agent)

    decision = result.decision
    assert decision.selection_method is (
        BaseLatexSelectionMethod.EXACT_SOURCE_RESUME_MATCH
    )
    assert decision.selected_latex_version_id == bound.latex_version_id
    assert decision.agent_invoked is False
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_ambiguous_candidates_use_one_bounded_agent_call(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    first = _add_version(parts, marker="alpha")
    _add_version(parts, marker="beta")
    agent = _FakeSelectionAgent(
        BaseLatexSelectionAgentOutput(
            disposition=BaseLatexSelectionAgentDisposition.SELECTED,
            selected_latex_version_id=first.latex_version_id,
            rationale="Alpha matches the geospatial engineering focus.",
        )
    )

    result = await _select(parts, agent)

    decision = result.decision
    assert decision.selection_method is (
        BaseLatexSelectionMethod.AGENT_SELECTED
    )
    assert decision.selected_latex_version_id == first.latex_version_id
    assert decision.agent_invoked is True
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
async def test_agent_context_carries_metadata_only_and_no_latex_source(
    tmp_path: Path,
) -> None:
    instructions = "Prefer the compact single-column layout."
    parts = await _setup(tmp_path, user_instructions=instructions)
    _add_version(parts, marker="alpha", labels=("compact",))
    _add_version(parts, marker="beta", labels=("two-column",))
    agent = _FakeSelectionAgent()

    result = await _select(parts, agent)

    assert result.status is BaseLatexSelectionStatus.CREATED
    context = agent.contexts[0]
    assert context.subject_id == "subject-a"
    assert context.job.description == JOB_DESCRIPTION
    assert context.user_preparation_instructions == instructions
    assert len(context.candidates) == 2
    for candidate in context.candidates:
        assert candidate.latex_version_id.startswith("resume-latex-version-")
        assert not hasattr(candidate, "latex_source")
        assert not hasattr(candidate, "source_reference")
        rendered = json.dumps(candidate.to_dict())
        assert "documentclass" not in rendered
        assert "\\begin" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        BaseLatexSelectionAgentOutput(
            disposition=BaseLatexSelectionAgentDisposition.SELECTED,
            selected_latex_version_id="resume-latex-version-" + "0" * 64,
            rationale="A version that is not in the candidate set.",
        ),
        BaseLatexSelectionAgentOutput(
            disposition=(
                BaseLatexSelectionAgentDisposition.USE_MANAGED_TEMPLATE
            ),
            selected_latex_version_id=None,
            rationale="None of the candidates suit this role.",
        ),
        BaseLatexSelectionAgentOutput(
            disposition=BaseLatexSelectionAgentDisposition.NEEDS_HUMAN,
            selected_latex_version_id=None,
            rationale="The candidates are hard to tell apart.",
        ),
    ],
)
async def test_unusable_agent_answers_fall_back_to_managed_template(
    tmp_path: Path, output
) -> None:
    parts = await _setup(tmp_path)
    _add_version(parts, marker="alpha")
    _add_version(parts, marker="beta")

    result = await _select(parts, _FakeSelectionAgent(output))

    decision = result.decision
    assert result.status is BaseLatexSelectionStatus.CREATED
    assert decision.selection_kind is (
        BaseLatexSelectionKind.MANAGED_TEMPLATE_FALLBACK
    )
    assert decision.selected_latex_version_id is None
    assert decision.agent_invoked is True


@pytest.mark.asyncio
async def test_untyped_agent_output_falls_back_without_human(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_version(parts, marker="alpha")
    _add_version(parts, marker="beta")

    result = await _select(
        parts, _FakeSelectionAgent(lambda _context: {"pick": "alpha"})
    )

    assert result.status is BaseLatexSelectionStatus.CREATED
    assert result.decision.selection_kind is (
        BaseLatexSelectionKind.MANAGED_TEMPLATE_FALLBACK
    )


async def _version_id_for(tmp_path: Path, marker: str) -> str:
    """LaTeX identity excludes paths, so a throwaway home yields the same ID."""

    seed = await _setup(tmp_path / f"seed-{marker}")
    return _add_version(seed, marker=marker).latex_version_id


@pytest.mark.asyncio
async def test_explicitly_required_version_is_honoured_without_agent(
    tmp_path: Path,
) -> None:
    wanted_id = await _version_id_for(tmp_path, "alpha")
    parts = await _setup(
        tmp_path,
        user_instructions=f"Use {wanted_id} for this role.",
    )
    wanted = _add_version(parts, marker="alpha")
    _add_version(parts, marker="beta")
    agent = _FakeSelectionAgent()

    assert wanted.latex_version_id == wanted_id

    result = await _select(parts, agent)

    decision = result.decision
    assert decision.selection_method is (
        BaseLatexSelectionMethod.USER_REQUIRED_VERSION
    )
    assert decision.selection_kind is BaseLatexSelectionKind.EXISTING_VERSION
    assert decision.selected_latex_version_id == wanted_id
    assert decision.agent_invoked is False
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_unsatisfiable_required_version_defers_for_human(
    tmp_path: Path,
) -> None:
    missing = "resume-latex-version-" + "0" * 64
    parts = await _setup(
        tmp_path,
        user_instructions=f"You must use {missing} exactly.",
    )
    _add_version(parts, marker="alpha")
    _add_version(parts, marker="beta")
    agent = _FakeSelectionAgent()

    result = await _select(parts, agent)

    assert result.status is BaseLatexSelectionStatus.DEFERRED_NEEDS_HUMAN
    assert (
        result.reason_code
        is BaseLatexSelectionFailureReason.USER_REQUIREMENT_UNSATISFIABLE
    )
    assert result.decision is None
    assert agent.contexts == []
    assert not _decisions(parts)


@pytest.mark.asyncio
async def test_agent_leaving_a_required_family_defers_for_human(
    tmp_path: Path,
) -> None:
    seed = await _setup(tmp_path)
    first = _add_version(seed, marker="alpha")
    second = register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id="subject-a",
            source_kind=ResumeLatexSourceKind.AI_REVISED,
            now=NOW,
            latex_source=LATEX.replace("Built", "Delivered"),
            parent_version_id=first.latex_version_id,
        ),
        home=seed["home"],
        repository=seed["latex_repository"],
    ).version
    assert second is not None
    parts = await _setup(
        tmp_path / "with-family",
        user_instructions=f"Stay inside {first.root_family_id} please.",
    )
    parts["latex_repository"] = seed["latex_repository"]
    agent = _FakeSelectionAgent(
        BaseLatexSelectionAgentOutput(
            disposition=(
                BaseLatexSelectionAgentDisposition.USE_MANAGED_TEMPLATE
            ),
            selected_latex_version_id=None,
            rationale="Ignoring the requested family.",
        )
    )

    result = await _select(parts, agent)

    assert result.status is BaseLatexSelectionStatus.DEFERRED_NEEDS_HUMAN
    assert len(agent.contexts) == 1
    assert all(
        item.root_family_id == first.root_family_id
        for item in agent.contexts[0].candidates
    )


@pytest.mark.asyncio
async def test_blocked_or_deferred_fact_qa_cannot_reach_selection(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_version(parts, marker="alpha")
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
    agent = _FakeSelectionAgent()

    result = await _select(parts, agent)

    assert result.status is BaseLatexSelectionStatus.FAILED
    assert (
        result.reason_code
        is BaseLatexSelectionFailureReason.FACT_QA_NOT_PASSED
    )
    assert agent.contexts == []
    assert not _decisions(parts)


@pytest.mark.asyncio
async def test_fact_qa_bound_to_a_different_draft_hash_fails_closed(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    passed = parts["qa_result"]
    drifted = object.__new__(type(passed))
    for field in type(passed).__dataclass_fields__:
        object.__setattr__(drifted, field, getattr(passed, field))
    object.__setattr__(drifted, "tailored_resume_draft_hash", "f" * 64)

    class _DriftedRepository:
        def get(self, **_kwargs):
            from core.resume_fact_qa import (
                ResumeFactQAReadResult,
                ResumeFactQAReadStatus,
            )

            return ResumeFactQAReadResult(
                status=ResumeFactQAReadStatus.FOUND,
                qa_result=drifted,
            )

    parts["qa_repository"] = _DriftedRepository()
    agent = _FakeSelectionAgent()

    result = await _select(parts, agent)

    assert result.status is BaseLatexSelectionStatus.FAILED
    assert (
        result.reason_code
        is BaseLatexSelectionFailureReason.DRAFT_BINDING_MISMATCH
    )
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_missing_fact_qa_and_job_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    missing = await _select(
        parts,
        fact_qa_result_id="resume-fact-qa-" + "0" * 64,
    )
    job = parts["job_repository"].job
    parts["job_repository"] = _JobRepository(
        JobPosting(**{**job.to_dict(), "revision": 9})
    )
    mismatch = await _select(parts)

    assert (
        missing.reason_code
        is BaseLatexSelectionFailureReason.FACT_QA_NOT_FOUND
    )
    assert (
        mismatch.reason_code
        is BaseLatexSelectionFailureReason.JOB_BINDING_MISMATCH
    )
    assert not _decisions(parts)


@pytest.mark.asyncio
async def test_version_fact_qa_provenance_is_verified(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    qa_result = parts["qa_result"]
    good = _add_version(
        parts,
        marker="provenanced",
        tailored_resume_draft_id=parts["draft"].draft_id,
        tailored_resume_draft_hash=parts["draft"].draft_content_hash,
        fact_qa_result_id=qa_result.qa_result_id,
        fact_qa_result_hash=qa_result.qa_content_hash,
    )

    ok = await _select(parts)

    assert ok.status is BaseLatexSelectionStatus.CREATED
    assert ok.decision.selected_latex_version_id == good.latex_version_id

    _add_version(
        parts,
        marker="bad-provenance",
        tailored_resume_draft_id=parts["draft"].draft_id,
        tailored_resume_draft_hash=parts["draft"].draft_content_hash,
        fact_qa_result_id=qa_result.qa_result_id,
        fact_qa_result_hash="f" * 64,
    )
    corrupt = await _select(parts)

    assert corrupt.status is BaseLatexSelectionStatus.FAILED
    assert (
        corrupt.reason_code
        is BaseLatexSelectionFailureReason.LATEX_PROVENANCE_INVALID
    )


@pytest.mark.asyncio
async def test_subject_isolation_for_candidates(tmp_path: Path) -> None:
    parts = await _setup(tmp_path)
    foreign = _add_version(
        parts, marker="foreign", subject_id="subject-b"
    )
    agent = _FakeSelectionAgent()

    result = await _select(parts, agent)

    assert result.decision.selection_kind is (
        BaseLatexSelectionKind.MANAGED_TEMPLATE_FALLBACK
    )
    assert result.decision.selected_latex_version_id != (
        foreign.latex_version_id
    )
    assert agent.contexts == []


@pytest.mark.asyncio
async def test_replay_returns_unchanged_with_zero_extra_agent_calls(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_version(parts, marker="alpha")
    _add_version(parts, marker="beta")
    agent = _FakeSelectionAgent()

    first = await _select(parts, agent)
    replay = await _select(parts, agent, now=NOW + timedelta(days=4))

    assert first.status is BaseLatexSelectionStatus.CREATED
    assert replay.status is BaseLatexSelectionStatus.UNCHANGED
    assert replay.decision == first.decision
    assert replay.decision.selected_at == NOW
    assert len(agent.contexts) == 1


@pytest.mark.asyncio
async def test_candidate_set_change_creates_a_new_decision(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    first = await _select(parts)
    _add_version(parts, marker="added")

    second = await _select(parts, now=NOW + timedelta(minutes=1))

    assert second.status is BaseLatexSelectionStatus.CREATED
    assert second.decision.decision_id != first.decision.decision_id
    assert second.candidate_set_hash != first.candidate_set_hash
    assert len(_decisions(parts)) == 2
    kept = parts["decision_repository"].get(
        subject_id="subject-a",
        decision_id=first.decision.decision_id,
    )
    assert kept.decision == first.decision


@pytest.mark.asyncio
async def test_agent_unavailable_fails_retryable_without_decision(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_version(parts, marker="alpha")
    _add_version(parts, marker="beta")

    result = await _select(
        parts,
        _FakeSelectionAgent(
            BaseLatexSelectionAgentUnavailableError("provider offline")
        ),
    )

    assert result.status is BaseLatexSelectionStatus.FAILED
    assert (
        result.reason_code
        is BaseLatexSelectionFailureReason.AGENT_UNAVAILABLE
    )
    assert result.retryable
    assert not _decisions(parts)


@pytest.mark.asyncio
async def test_repository_conflict_and_restart_reads(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    _add_version(parts, marker="alpha")
    first = await _select(parts)
    decision = first.decision
    record = next(
        parts["home"].paths.base_latex_selections.rglob(
            f"{decision.decision_id}.json"
        )
    )

    restarted = PrivateHomeBaseLatexSelectionDecisionRepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(
        subject_id="subject-a", decision_id=decision.decision_id
    )
    cross = restarted.get(
        subject_id="subject-b", decision_id=decision.decision_id
    )
    corrupted = b"{broken"
    record.write_bytes(corrupted)
    conflict = parts["decision_repository"].save(decision)

    assert read.status is BaseLatexSelectionReadStatus.FOUND
    assert read.decision == decision
    assert read.decision.selection_binding == decision.selection_binding
    assert cross.status is BaseLatexSelectionReadStatus.NOT_FOUND
    assert conflict.status is BaseLatexSelectionWriteStatus.FAILED
    assert (
        conflict.reason_code
        is BaseLatexSelectionFailureReason.DECISION_INTEGRITY_FAILURE
    )
    assert record.read_bytes() == corrupted


@pytest.mark.asyncio
async def test_invalid_command_fails_without_side_effects(
    tmp_path: Path,
) -> None:
    parts = await _setup(tmp_path)
    agent = _FakeSelectionAgent()

    naive = await _select(
        parts, agent, now=datetime(2026, 7, 29, 9, 0)
    )

    assert naive.status is BaseLatexSelectionStatus.FAILED
    assert (
        naive.reason_code
        is BaseLatexSelectionFailureReason.INVALID_REQUEST
    )
    assert agent.contexts == []
    assert not _decisions(parts)


def test_module_never_reads_latex_source_or_compiles() -> None:
    module_path = Path(selection_module.__file__)
    text = module_path.read_text(encoding="utf-8")
    tree = ast.parse(text)
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
        "playwright",
        "subprocess",
        "shutil",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
    assert "source_reference" not in text
    assert "read_text" not in text or "read_bytes" not in text
    assert "register_resume_latex_version" not in imports
