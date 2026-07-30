from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.prepared_resume_material as publication_module
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.application_preparation_orchestrator import (
    PreparationStageOutcome,
)
from core.job_prioritization import ProposedPriorityLevel
from core.prepared_resume_material import (
    PREPARED_RESUME_MATERIAL_CONTRACT_VERSION,
    PreparedMaterialRole,
    PreparedResumeMaterialFailureReason,
    PreparedResumeMaterialNotReadyReason,
    PreparedResumeMaterialReadStatus,
    PreparedResumeMaterialStatus,
    PreparedResumeMaterialWriteStatus,
    PrivateHomePreparedResumeMaterialRepository,
    PublishPreparedResumeCommand,
    prepared_resume_publication_public_result,
    publish_prepared_resume,
)
from core.private_home import PrivateHome
from core.resume_compilation import (
    PrivateHomeResumeCompilationRepository,
    RESUME_COMPILATION_CONTRACT_VERSION,
    ResumeCompilationRecord,
    compiled_pdf_reference,
    pdf_page_count,
)
from core.resume_fact_qa import (
    PrivateHomeResumeFactQARepository,
    RESUME_FACT_QA_CONTRACT_VERSION,
    RESUME_FACT_QA_POLICY_VERSION,
    ResumeFactQAResult,
    ResumeFactQAVerdict,
)
from core.latex_compiler import (
    LATEX_COMPILE_POLICY_VERSION,
    LATEX_SANDBOX_POLICY_VERSION,
    normalized_compile_flags,
)
from core.resume_latex_markers import (
    JOBOPS_CONTENT_BEGIN,
    JOBOPS_CONTENT_END,
    MARKER_MACRO_DEFINITIONS,
)
from core.resume_latex_versions import (
    PrivateHomeResumeLatexVersionRepository,
    RegisterResumeLatexVersionCommand,
    ResumeLatexSourceKind,
    register_resume_latex_version,
)
from core.resume_layout_revision import (
    PrivateHomeResumeLayoutRevisionRepository,
    RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
    RESUME_LAYOUT_REVISION_POLICY_VERSION,
    ResumeLayoutAttemptOutcome,
    ResumeLayoutRevisionAttempt,
    ResumeLayoutRevisionRun,
    ResumeLayoutRevisionStatus,
)
from core.resume_tailoring import (
    PrivateHomeTailoredResumeDraftRepository,
    RESUME_TAILORING_CONTRACT_VERSION,
    RESUME_TAILORING_POLICY_VERSION,
    TailoredBulletChangeType,
    TailoredResumeBullet,
    TailoredResumeDraft,
    TailoredResumeSection,
)
from core.resume_visual_qa import (
    PrivateHomeResumeVisualQARepository,
    RESUME_VISUAL_QA_CONTRACT_VERSION,
    RESUME_VISUAL_QA_POLICY_VERSION,
    ResumeVisualQAResult,
    ResumeVisualQAVerdict,
)

from test_resume_visual_qa import synthetic_pdf


NOW = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
SECTION_TITLE = "Experience"
BULLET_TEXT = "Built deterministic geospatial pipelines in Python."


def _hashed(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _latex(marker: str = "") -> str:
    return (
        "\\documentclass[11pt]{article}\n"
        f"\\usepackage[margin=0.75in]{{geometry}}{marker}\n"
        f"{MARKER_MACRO_DEFINITIONS}"
        "\\begin{document}\n"
        f"{JOBOPS_CONTENT_BEGIN}\n"
        f"\\JobopsSection{{resume-section-a}}{{{SECTION_TITLE}}}\n"
        "\\begin{itemize}\n"
        f"\\JobopsBullet{{resume-block-a}}{{{BULLET_TEXT}}}\n"
        "\\end{itemize}\n"
        f"{JOBOPS_CONTENT_END}\n"
        "\\end{document}\n"
    )


def _setup(
    tmp_path: Path,
    *,
    subject_id: str = "subject-a",
    fact_qa_verdict: ResumeFactQAVerdict = ResumeFactQAVerdict.PASSED,
    visual_verdict: ResumeVisualQAVerdict = ResumeVisualQAVerdict.PASSED,
    latex_marker: str = "",
    pdf_pages: int = 1,
):
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id="job-one",
        job_revision=1,
        job_content_hash=_hashed({"job": "one"}),
        priority_decision_id="priority-decision-one",
        policy_id="policy-one",
        policy_version=1,
        policy_content_hash=_hashed({"policy": "one"}),
        accepted_job_intent_id="accepted-intent-one",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
    )
    plan_repository = PrivateHomeApplicationPlanRepository(home)
    plan_repository.save(plan)

    section = TailoredResumeSection(
        order=0,
        source_section_id="resume-section-a",
        title=SECTION_TITLE,
        bullets=(
            TailoredResumeBullet(
                order=0,
                change_type=TailoredBulletChangeType.REWRITTEN,
                text=BULLET_TEXT,
                source_section_id="resume-section-a",
                source_block_id="resume-block-a",
                source_bullet_id=None,
                evidence_ids=("candidate-evidence-" + "1" * 64,),
                jd_alignment=("geospatial data pipelines",),
            ),
        ),
    )
    draft_binding = _hashed({"draft": subject_id, "marker": latex_marker})
    draft_fields = {
        "draft_id": f"tailored-resume-draft-{draft_binding}",
        "contract_version": RESUME_TAILORING_CONTRACT_VERSION,
        "tailoring_binding": draft_binding,
        "subject_id": subject_id,
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "resume_selection_decision_id": "resume-selection-" + "4" * 64,
        "source_resume_id": "resume-candidate-" + "5" * 64,
        "source_artifact_sha256": "6" * 64,
        "source_projection_id": "source-resume-projection-" + "7" * 64,
        "source_projection_hash": "8" * 64,
        "evidence_snapshot_id": "candidate-evidence-snapshot-" + "9" * 64,
        "evidence_snapshot_hash": "a" * 64,
        "user_preparation_instructions_hash": (
            plan.user_preparation_instructions_hash
        ),
        "agent_version": "resume-tailoring-agent-v1",
        "prompt_version": "resume-tailoring-prompt-v1",
        "model_id": "synthetic-tailoring-model",
        "agent_policy_version": RESUME_TAILORING_POLICY_VERSION,
        "rationale": "Synthetic draft for publication coverage.",
        "sections": [section.to_dict()],
    }
    draft = TailoredResumeDraft(
        draft_content_hash=_hashed(draft_fields),
        created_at=NOW,
        sections=(section,),
        **{k: v for k, v in draft_fields.items() if k != "sections"},
    )
    draft_repository = PrivateHomeTailoredResumeDraftRepository(home)
    draft_repository.save(draft)

    qa_binding = _hashed({"fact-qa": draft.draft_id})
    fact_fields = {
        "qa_result_id": f"resume-fact-qa-{qa_binding}",
        "contract_version": RESUME_FACT_QA_CONTRACT_VERSION,
        "qa_binding": qa_binding,
        "subject_id": subject_id,
        "tailored_resume_draft_id": draft.draft_id,
        "tailored_resume_draft_hash": draft.draft_content_hash,
        "application_plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "job_revision": plan.job_revision,
        "job_content_hash": plan.job_content_hash,
        "resume_selection_decision_id": draft.resume_selection_decision_id,
        "source_projection_id": draft.source_projection_id,
        "source_projection_hash": draft.source_projection_hash,
        "evidence_snapshot_id": draft.evidence_snapshot_id,
        "evidence_snapshot_hash": draft.evidence_snapshot_hash,
        "agent_policy_version": RESUME_FACT_QA_POLICY_VERSION,
        "agent_invoked": True,
        "agent_version": "resume-fact-qa-agent-v1",
        "prompt_version": "resume-fact-qa-prompt-v1",
        "model_id": "synthetic-qa-model",
        "verdict": fact_qa_verdict.value,
        "findings": [],
    }
    fact_qa = ResumeFactQAResult(
        qa_content_hash=_hashed(fact_fields),
        validated_at=NOW,
        findings=(),
        verdict=fact_qa_verdict,
        **{
            k: v
            for k, v in fact_fields.items()
            if k not in {"findings", "verdict"}
        },
    )
    fact_qa_repository = PrivateHomeResumeFactQARepository(home)
    fact_qa_repository.save(fact_qa)

    latex_repository = PrivateHomeResumeLatexVersionRepository(home)
    version = register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id=subject_id,
            source_kind=ResumeLatexSourceKind.SYSTEM_TEMPLATE_DERIVED,
            now=NOW,
            latex_source=_latex(latex_marker),
            template_id="managed-resume-one-page-v1",
            template_sha256="c" * 64,
            tailored_resume_draft_id=draft.draft_id,
            tailored_resume_draft_hash=draft.draft_content_hash,
            fact_qa_result_id=fact_qa.qa_result_id,
            fact_qa_result_hash=fact_qa.qa_content_hash,
        ),
        home=home,
        repository=latex_repository,
    ).version
    assert version is not None

    pdf = synthetic_pdf(pages=pdf_pages, text=BULLET_TEXT, title=SECTION_TITLE)
    pdf_hash = hashlib.sha256(pdf).hexdigest()
    reference = compiled_pdf_reference(
        subject_id=subject_id, pdf_sha256=pdf_hash
    )
    home.write_bytes_if_absent(home.contained_path(reference), pdf)

    compilation_binding = _hashed(
        {"compilation": version.latex_version_id, "pdf": pdf_hash}
    )
    compilation = ResumeCompilationRecord(
        record_id=f"resume-compilation-{compilation_binding}",
        contract_version=RESUME_COMPILATION_CONTRACT_VERSION,
        compilation_binding=compilation_binding,
        subject_id=subject_id,
        construction_record_id="resume-latex-construction-" + "d" * 64,
        construction_binding="e" * 64,
        latex_version_id=version.latex_version_id,
        latex_source_sha256=version.source_sha256,
        compiler_engine="pdflatex",
        compiler_version="pdfTeX 3.141592653 (synthetic)",
        compile_policy_version=LATEX_COMPILE_POLICY_VERSION,
        sandbox_policy_version=LATEX_SANDBOX_POLICY_VERSION,
        normalized_flags=normalized_compile_flags(),
        pdf_reference=reference,
        pdf_sha256=pdf_hash,
        pdf_byte_size=len(pdf),
        page_count=pdf_page_count(pdf),
        diagnostics="",
        compiled_at=NOW,
    )
    compilation_repository = PrivateHomeResumeCompilationRepository(home)
    assert compilation_repository.save(compilation).record is not None

    visual_binding = _hashed(
        {"visual": compilation.record_id, "verdict": visual_verdict.value}
    )
    visual_fields = {
        "result_id": f"resume-visual-qa-{visual_binding}",
        "contract_version": RESUME_VISUAL_QA_CONTRACT_VERSION,
        "visual_qa_binding": visual_binding,
        "subject_id": subject_id,
        "compilation_record_id": compilation.record_id,
        "compilation_binding": compilation.compilation_binding,
        "pdf_sha256": compilation.pdf_sha256,
        "latex_version_id": version.latex_version_id,
        "latex_source_sha256": version.source_sha256,
        "tailored_resume_draft_id": draft.draft_id,
        "tailored_resume_draft_hash": draft.draft_content_hash,
        "renderer_name": "fake-renderer",
        "renderer_version": "1.0.0",
        "renderer_dpi": 150,
        "policy_version": RESUME_VISUAL_QA_POLICY_VERSION,
        "max_pages": 1,
        "page_count": compilation.page_count,
        "agent_invoked": True,
        "agent_version": "resume-visual-qa-agent-v1",
        "prompt_version": "resume-visual-qa-prompt-v1",
        "model_id": "synthetic-visual-model",
        "verdict": visual_verdict.value,
        "findings": [],
    }
    if visual_verdict is not ResumeVisualQAVerdict.PASSED:
        from core.resume_visual_qa import (
            ResumeVisualQAFindingSeverity,
            ResumeVisualQAFindingSource,
            ResumeVisualQAFindingType,
            resume_visual_qa_finding_id,
            ResumeVisualQAFinding,
        )

        finding_type = (
            ResumeVisualQAFindingType.UNEXPECTED_PAGE_COUNT
            if visual_verdict is ResumeVisualQAVerdict.REVISION_REQUIRED
            else ResumeVisualQAFindingType.AGENT_OUTPUT_UNRELIABLE
        )
        finding_content = {
            "bounding_box": None,
            "explanation": "Synthetic finding for publication coverage.",
            "finding_type": finding_type.value,
            "order": 0,
            "page_number": 0,
            "severity": ResumeVisualQAFindingSeverity.BLOCKING.value,
            "source": ResumeVisualQAFindingSource.DETERMINISTIC.value,
        }
        finding = ResumeVisualQAFinding(
            finding_id=resume_visual_qa_finding_id(finding_content),
            order=0,
            finding_type=finding_type,
            severity=ResumeVisualQAFindingSeverity.BLOCKING,
            source=ResumeVisualQAFindingSource.DETERMINISTIC,
            page_number=0,
            bounding_box=None,
            explanation="Synthetic finding for publication coverage.",
        )
        findings = (finding,)
        visual_fields["findings"] = [finding.to_dict()]
    else:
        findings = ()
    visual_qa = ResumeVisualQAResult(
        result_content_hash=_hashed(visual_fields),
        validated_at=NOW,
        findings=findings,
        verdict=visual_verdict,
        **{
            k: v
            for k, v in visual_fields.items()
            if k not in {"findings", "verdict"}
        },
    )
    visual_repository = PrivateHomeResumeVisualQARepository(home)
    assert visual_repository.save(visual_qa).result is not None

    return {
        "home": home,
        "plan": plan,
        "plan_repository": plan_repository,
        "draft": draft,
        "draft_repository": draft_repository,
        "fact_qa": fact_qa,
        "fact_qa_repository": fact_qa_repository,
        "version": version,
        "latex_repository": latex_repository,
        "compilation": compilation,
        "compilation_repository": compilation_repository,
        "visual_qa": visual_qa,
        "visual_repository": visual_repository,
        "revision_repository": PrivateHomeResumeLayoutRevisionRepository(home),
        "material_repository": (
            PrivateHomePreparedResumeMaterialRepository(home)
        ),
        "pdf": pdf,
    }


def _revision_run(
    parts,
    *,
    status: ResumeLayoutRevisionStatus = ResumeLayoutRevisionStatus.CREATED,
    outcome: ResumeLayoutAttemptOutcome = ResumeLayoutAttemptOutcome.PASSED,
    final_qa_id: str | None = None,
    max_attempts: int = 3,
):
    binding = _hashed({"run": parts["visual_qa"].result_id, "s": status.value})
    attempts = (
        ResumeLayoutRevisionAttempt(
            attempt_number=1,
            input_latex_version_id="resume-latex-version-" + "0" * 64,
            input_compilation_record_id="resume-compilation-" + "0" * 64,
            input_visual_qa_result_id="resume-visual-qa-" + "0" * 64,
            blocking_finding_ids=("resume-visual-qa-finding-" + "1" * 64,),
            agent_version="resume-layout-revision-agent-v1",
            prompt_version="resume-layout-revision-prompt-v1",
            model_id="synthetic-revision-model",
            output_latex_version_id=parts["version"].latex_version_id,
            output_compilation_record_id=parts["compilation"].record_id,
            output_visual_qa_result_id=(
                final_qa_id or parts["visual_qa"].result_id
            ),
            outcome=outcome,
            detail="Synthetic attempt for publication coverage.",
        ),
    )
    if status is ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED:
        attempts = tuple(
            ResumeLayoutRevisionAttempt(
                **{
                    **attempts[0].to_dict(),
                    "attempt_number": index + 1,
                    "blocking_finding_ids": tuple(
                        attempts[0].blocking_finding_ids
                    ),
                    "outcome": ResumeLayoutAttemptOutcome.REVISION_REQUIRED,
                }
            )
            for index in range(max_attempts)
        )
    content = {
        "run_id": f"resume-layout-revision-run-{binding}",
        "contract_version": RESUME_LAYOUT_REVISION_CONTRACT_VERSION,
        "run_binding": binding,
        "subject_id": parts["plan"].subject_id,
        "application_plan_id": parts["plan"].plan_id,
        "tailored_resume_draft_id": parts["draft"].draft_id,
        "tailored_resume_draft_hash": parts["draft"].draft_content_hash,
        "initial_visual_qa_result_id": "resume-visual-qa-" + "0" * 64,
        "initial_visual_qa_result_hash": "b" * 64,
        "initial_latex_version_id": "resume-latex-version-" + "0" * 64,
        "initial_latex_source_sha256": "c" * 64,
        "policy_version": RESUME_LAYOUT_REVISION_POLICY_VERSION,
        "max_attempts": max_attempts,
        "attempts": [item.to_dict() for item in attempts],
        "final_latex_version_id": parts["version"].latex_version_id,
        "final_compilation_record_id": parts["compilation"].record_id,
        "final_visual_qa_result_id": (
            final_qa_id or parts["visual_qa"].result_id
        ),
        "final_status": status.value,
    }
    run = ResumeLayoutRevisionRun(
        run_content_hash=_hashed(content),
        started_at=NOW,
        completed_at=NOW,
        attempts=attempts,
        final_status=status,
        **{
            k: v
            for k, v in content.items()
            if k not in {"attempts", "final_status", "run_content_hash"}
        },
    )
    assert parts["revision_repository"].save(run).run is not None
    return run


def _publish(
    parts,
    *,
    subject_id: str = "subject-a",
    visual_qa_result_id: str | None = None,
    run_id: str | None = None,
    now: datetime = NOW,
):
    if visual_qa_result_id is None and run_id is None:
        visual_qa_result_id = parts["visual_qa"].result_id
    return publish_prepared_resume(
        PublishPreparedResumeCommand(
            subject_id=subject_id,
            application_plan_id=parts["plan"].plan_id,
            now=now,
            resume_visual_qa_result_id=visual_qa_result_id,
            resume_layout_revision_run_id=run_id,
        ),
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        fact_qa_repository=parts["fact_qa_repository"],
        latex_version_repository=parts["latex_repository"],
        compilation_repository=parts["compilation_repository"],
        visual_qa_repository=parts["visual_repository"],
        layout_revision_repository=parts["revision_repository"],
        material_repository=parts["material_repository"],
        home=parts["home"],
    )


def _materials(parts) -> tuple[Path, ...]:
    return tuple(
        parts["home"].paths.prepared_resume_materials.rglob("*.json")
    )


def test_direct_passed_visual_qa_publishes_material(tmp_path: Path) -> None:
    parts = _setup(tmp_path)

    result = _publish(parts)

    assert result.status is PreparedResumeMaterialStatus.CREATED
    material = result.material
    assert material.contract_version == (
        PREPARED_RESUME_MATERIAL_CONTRACT_VERSION
    )
    assert material.material_role is PreparedMaterialRole.RESUME
    assert material.application_plan_id == parts["plan"].plan_id
    assert material.job_id == parts["plan"].job_id
    assert material.tailored_resume_draft_id == parts["draft"].draft_id
    assert material.fact_qa_result_id == parts["fact_qa"].qa_result_id
    assert material.latex_version_id == parts["version"].latex_version_id
    assert material.compilation_record_id == parts["compilation"].record_id
    assert material.visual_qa_result_id == parts["visual_qa"].result_id
    assert material.layout_revision_run_id is None
    assert material.pdf_sha256 == parts["compilation"].pdf_sha256
    assert material.pdf_byte_size == len(parts["pdf"])
    assert material.page_count == parts["compilation"].page_count
    assert material.published_at == NOW


def test_successful_revision_run_publishes_its_final_lineage(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    run = _revision_run(parts)

    result = _publish(parts, run_id=run.run_id)

    material = result.material
    assert result.status is PreparedResumeMaterialStatus.CREATED
    assert material.layout_revision_run_id == run.run_id
    assert material.layout_revision_run_binding == run.run_binding
    assert material.latex_version_id == run.final_latex_version_id
    assert material.compilation_record_id == (
        run.final_compilation_record_id
    )
    assert material.visual_qa_result_id == run.final_visual_qa_result_id


def test_direct_and_revision_paths_produce_distinct_materials(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    run = _revision_run(parts)

    direct = _publish(parts)
    revised = _publish(parts, run_id=run.run_id, now=NOW + timedelta(minutes=1))

    assert direct.material.material_id != revised.material.material_id
    assert direct.material.layout_revision_run_id is None
    assert revised.material.layout_revision_run_id == run.run_id
    assert len(_materials(parts)) == 2


@pytest.mark.parametrize(
    "verdict",
    [
        ResumeVisualQAVerdict.REVISION_REQUIRED,
        ResumeVisualQAVerdict.DEFERRED,
    ],
)
def test_unapproved_visual_qa_is_not_ready(
    tmp_path: Path, verdict: ResumeVisualQAVerdict
) -> None:
    parts = _setup(tmp_path, visual_verdict=verdict)

    result = _publish(parts)

    assert result.status is PreparedResumeMaterialStatus.NOT_READY
    assert (
        result.not_ready_reason
        is PreparedResumeMaterialNotReadyReason.VISUAL_QA_NOT_PASSED
    )
    assert result.material is None
    assert result.stopped_source_lineage is not None
    assert (
        result.stopped_source_lineage.source_result_id
        == parts["visual_qa"].result_id
    )
    assert not _materials(parts)


@pytest.mark.parametrize(
    "status",
    [
        ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED,
        ResumeLayoutRevisionStatus.DEFERRED_NEEDS_HUMAN,
        ResumeLayoutRevisionStatus.NOT_REQUIRED,
    ],
)
def test_unsuccessful_revision_runs_are_not_ready(
    tmp_path: Path, status: ResumeLayoutRevisionStatus
) -> None:
    parts = _setup(tmp_path)
    outcome = (
        ResumeLayoutAttemptOutcome.REVISION_REQUIRED
        if status is ResumeLayoutRevisionStatus.DEFERRED_ATTEMPTS_EXHAUSTED
        else ResumeLayoutAttemptOutcome.VISUAL_QA_DEFERRED
    )
    run = _revision_run(parts, status=status, outcome=outcome)

    result = _publish(parts, run_id=run.run_id)

    assert result.status is PreparedResumeMaterialStatus.NOT_READY
    assert (
        result.not_ready_reason
        is PreparedResumeMaterialNotReadyReason.REVISION_RUN_NOT_SUCCESSFUL
    )
    assert result.stopped_source_lineage is not None
    assert result.stopped_source_lineage.source_result_id == run.run_id
    assert not _materials(parts)


def test_blocked_fact_qa_cannot_be_published(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    passed = parts["fact_qa"]
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

    parts["fact_qa_repository"] = _BlockedRepository()

    result = _publish(parts)

    assert result.status is PreparedResumeMaterialStatus.NOT_READY
    assert (
        result.not_ready_reason
        is PreparedResumeMaterialNotReadyReason.FACT_QA_NOT_PASSED
    )
    assert result.stopped_source_lineage is not None
    assert (
        result.stopped_source_lineage.source_result_id
        == blocked.qa_result_id
    )
    assert not _materials(parts)


def test_draft_from_another_plan_is_not_ready(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    draft = parts["draft"]
    object.__setattr__(
        draft, "application_plan_id", "application-plan-" + "0" * 64
    )

    class _DriftedDraftRepository:
        def get(self, **_kwargs):
            from core.resume_tailoring import (
                TailoredResumeDraftReadResult,
                TailoredResumeDraftReadStatus,
            )

            return TailoredResumeDraftReadResult(
                status=TailoredResumeDraftReadStatus.FOUND,
                draft=draft,
            )

    parts["draft_repository"] = _DriftedDraftRepository()

    result = _publish(parts)

    assert result.status is PreparedResumeMaterialStatus.NOT_READY
    assert (
        result.not_ready_reason
        is PreparedResumeMaterialNotReadyReason.DRAFT_BINDING_MISMATCH
    )


def test_compilation_not_matching_visual_qa_is_not_ready(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    compilation = parts["compilation"]
    object.__setattr__(compilation, "pdf_sha256", "f" * 64)

    class _DriftedCompilationRepository:
        def get(self, **_kwargs):
            from core.resume_compilation import (
                ResumeCompilationReadResult,
                ResumeCompilationReadStatus,
            )

            return ResumeCompilationReadResult(
                status=ResumeCompilationReadStatus.FOUND,
                record=compilation,
            )

    parts["compilation_repository"] = _DriftedCompilationRepository()

    result = _publish(parts)

    assert result.status is PreparedResumeMaterialStatus.NOT_READY
    assert (
        result.not_ready_reason
        is PreparedResumeMaterialNotReadyReason.COMPILATION_BINDING_MISMATCH
    )


def test_pdf_drift_and_removal_fail_closed(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    artifact = parts["home"].contained_path(
        parts["compilation"].pdf_reference
    )

    artifact.write_bytes(b"%PDF-1.4\ntampered\n%%EOF\n")

    class _PassThroughCompilation:
        def __init__(self, record) -> None:
            self.record = record

        def get(self, **_kwargs):
            from core.resume_compilation import (
                ResumeCompilationReadResult,
                ResumeCompilationReadStatus,
            )

            return ResumeCompilationReadResult(
                status=ResumeCompilationReadStatus.FOUND,
                record=self.record,
            )

    parts["compilation_repository"] = _PassThroughCompilation(
        parts["compilation"]
    )
    drifted = _publish(parts)

    artifact.unlink()
    missing = _publish(parts)

    assert drifted.status is PreparedResumeMaterialStatus.FAILED
    assert (
        drifted.reason_code
        is PreparedResumeMaterialFailureReason.PDF_HASH_DRIFT
    )
    assert missing.status is PreparedResumeMaterialStatus.FAILED
    assert (
        missing.reason_code
        is PreparedResumeMaterialFailureReason.PDF_UNREADABLE
    )
    assert not _materials(parts)


def test_page_count_drift_fails_closed(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    compilation = parts["compilation"]
    object.__setattr__(compilation, "page_count", 5)

    class _PassThrough:
        def get(self, **_kwargs):
            from core.resume_compilation import (
                ResumeCompilationReadResult,
                ResumeCompilationReadStatus,
            )

            return ResumeCompilationReadResult(
                status=ResumeCompilationReadStatus.FOUND,
                record=compilation,
            )

    parts["compilation_repository"] = _PassThrough()

    result = _publish(parts)

    assert result.status is PreparedResumeMaterialStatus.FAILED
    assert (
        result.reason_code is PreparedResumeMaterialFailureReason.PDF_INVALID
    )


def test_publication_never_copies_or_regenerates_the_pdf(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    before = tuple(
        sorted(parts["home"].paths.compiled_resumes.rglob("*.pdf"))
    )
    original = parts["home"].contained_path(
        parts["compilation"].pdf_reference
    ).read_bytes()

    result = _publish(parts)

    after = tuple(
        sorted(parts["home"].paths.compiled_resumes.rglob("*.pdf"))
    )
    assert after == before
    assert len(after) == 1
    assert (
        parts["home"]
        .contained_path(result.material.pdf_reference)
        .read_bytes()
        == original
    )
    assert result.material.pdf_reference == (
        parts["compilation"].pdf_reference
    )


def test_cross_subject_plan_and_material_are_isolated(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    published = _publish(parts)

    foreign_plan = _publish(parts, subject_id="subject-b")
    cross = parts["material_repository"].get(
        subject_id="subject-b",
        material_id=published.material.material_id,
    )
    listed = parts["material_repository"].find_current_for_plan(
        subject_id="subject-b",
        application_plan_id=parts["plan"].plan_id,
    )

    assert foreign_plan.status is PreparedResumeMaterialStatus.FAILED
    assert (
        foreign_plan.reason_code
        is PreparedResumeMaterialFailureReason
        .APPLICATION_PLAN_SUBJECT_MISMATCH
    )
    assert cross.status is PreparedResumeMaterialReadStatus.NOT_FOUND
    assert listed.status is PreparedResumeMaterialReadStatus.NOT_FOUND


def test_replay_returns_unchanged_without_duplicates(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    first = _publish(parts)
    replay = _publish(parts, now=NOW + timedelta(days=2))

    assert first.status is PreparedResumeMaterialStatus.CREATED
    assert replay.status is PreparedResumeMaterialStatus.UNCHANGED
    assert replay.material == first.material
    assert replay.material.published_at == NOW
    assert (
        prepared_resume_publication_public_result(first).outcome
        is PreparationStageOutcome.COMPLETED
    )
    assert (
        prepared_resume_publication_public_result(replay).outcome
        is PreparationStageOutcome.UNCHANGED
    )
    assert len(_materials(parts)) == 1
    assert len(tuple(parts["home"].paths.compiled_resumes.rglob("*.pdf"))) == 1


def test_changed_chain_creates_a_new_immutable_material(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _publish(parts)
    other = _setup(tmp_path / "other", latex_marker=" % variant")

    second = publish_prepared_resume(
        PublishPreparedResumeCommand(
            subject_id="subject-a",
            application_plan_id=other["plan"].plan_id,
            now=NOW + timedelta(minutes=1),
            resume_visual_qa_result_id=other["visual_qa"].result_id,
        ),
        application_plan_repository=other["plan_repository"],
        draft_repository=other["draft_repository"],
        fact_qa_repository=other["fact_qa_repository"],
        latex_version_repository=other["latex_repository"],
        compilation_repository=other["compilation_repository"],
        visual_qa_repository=other["visual_repository"],
        layout_revision_repository=other["revision_repository"],
        material_repository=other["material_repository"],
        home=other["home"],
    )

    assert second.status is PreparedResumeMaterialStatus.CREATED
    assert second.material.material_id != first.material.material_id
    assert second.material.latex_version_id != (
        first.material.latex_version_id
    )
    kept = parts["material_repository"].get(
        subject_id="subject-a",
        material_id=first.material.material_id,
    )
    assert kept.material == first.material


def test_find_current_for_plan_uses_publication_time_not_mtime(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    run = _revision_run(parts)
    direct = _publish(parts)
    revised = _publish(
        parts, run_id=run.run_id, now=NOW + timedelta(hours=1)
    )

    current = parts["material_repository"].find_current_for_plan(
        subject_id="subject-a",
        application_plan_id=parts["plan"].plan_id,
    )
    for path in _materials(parts):
        path.touch()
    again = PrivateHomePreparedResumeMaterialRepository(
        PrivateHome(parts["home"].root)
    ).find_current_for_plan(
        subject_id="subject-a",
        application_plan_id=parts["plan"].plan_id,
    )

    assert current.material == revised.material
    assert current.material != direct.material
    assert again.material == current.material


def test_conflict_corruption_and_restart(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    published = _publish(parts)
    material = published.material

    restarted = PrivateHomePreparedResumeMaterialRepository(
        PrivateHome(parts["home"].root)
    )
    read = restarted.get(
        subject_id="subject-a", material_id=material.material_id
    )
    assert read.status is PreparedResumeMaterialReadStatus.FOUND
    assert read.material == material

    path = next(
        parts["home"].paths.prepared_resume_materials.rglob(
            f"{material.material_id}.json"
        )
    )
    corrupted = b"{broken"
    path.write_bytes(corrupted)
    conflict = parts["material_repository"].save(material)
    corrupt = parts["material_repository"].get(
        subject_id="subject-a", material_id=material.material_id
    )

    assert conflict.status is PreparedResumeMaterialWriteStatus.FAILED
    assert (
        conflict.reason_code
        is PreparedResumeMaterialFailureReason.MATERIAL_INTEGRITY_FAILURE
    )
    assert path.read_bytes() == corrupted
    assert corrupt.status is PreparedResumeMaterialReadStatus.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"visual_qa_result_id": None, "run_id": None},
            PreparedResumeMaterialFailureReason.SOURCE_SELECTION_MISSING,
        ),
        (
            {"visual_qa_result_id": "resume-visual-qa-" + "0" * 64,
             "run_id": "resume-layout-revision-run-" + "0" * 64},
            PreparedResumeMaterialFailureReason.SOURCE_SELECTION_AMBIGUOUS,
        ),
        (
            {"visual_qa_result_id": "resume-visual-qa-" + "0" * 64},
            PreparedResumeMaterialFailureReason.VISUAL_QA_NOT_FOUND,
        ),
        (
            {"run_id": "resume-layout-revision-run-" + "0" * 64},
            PreparedResumeMaterialFailureReason.REVISION_RUN_NOT_FOUND,
        ),
    ],
)
def test_invalid_source_selection_fails_closed(
    tmp_path: Path, kwargs, reason
) -> None:
    parts = _setup(tmp_path)

    result = publish_prepared_resume(
        PublishPreparedResumeCommand(
            subject_id="subject-a",
            application_plan_id=parts["plan"].plan_id,
            now=NOW,
            resume_visual_qa_result_id=kwargs.get("visual_qa_result_id"),
            resume_layout_revision_run_id=kwargs.get("run_id"),
        ),
        application_plan_repository=parts["plan_repository"],
        draft_repository=parts["draft_repository"],
        fact_qa_repository=parts["fact_qa_repository"],
        latex_version_repository=parts["latex_repository"],
        compilation_repository=parts["compilation_repository"],
        visual_qa_repository=parts["visual_repository"],
        layout_revision_repository=parts["revision_repository"],
        material_repository=parts["material_repository"],
        home=parts["home"],
    )

    assert result.status is PreparedResumeMaterialStatus.FAILED
    assert result.reason_code is reason
    assert not _materials(parts)


def test_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    parts = _setup(tmp_path)

    result = _publish(parts, now=datetime(2026, 7, 30, 9, 0))

    assert result.status is PreparedResumeMaterialStatus.FAILED
    assert (
        result.reason_code
        is PreparedResumeMaterialFailureReason.INVALID_REQUEST
    )


def test_module_never_compiles_renders_or_reaches_execution() -> None:
    module_path = Path(publication_module.__file__)
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
        "core.latex_compiler",
        "core.materials",
        "core.pdf_page_renderer",
        "core.resume_candidates",
        "playwright",
        "subprocess",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
    for name in (
        "compile_resume_latex",
        "review_resume_visual_qa",
        "revise_resume_layout",
        "register_resume_latex_version",
    ):
        assert name not in text
    # The record itself is written, but the PDF is only ever read: the module
    # never derives a compiled-resume path or copies artifact bytes.
    assert "compiled_pdf_reference" not in text
    assert "compiled-resumes" not in text
    assert "compiled_resumes" not in text
    assert text.count("write_bytes_if_absent") == 1
    assert "read_bytes()" in text
