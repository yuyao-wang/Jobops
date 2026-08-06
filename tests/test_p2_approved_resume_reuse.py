from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.job_prioritization import ProposedPriorityLevel
from core.plan_material_manifest import (
    AssemblePlanMaterialManifestCommand,
    PlanMaterialManifestStatus,
    PrivateHomePlanMaterialManifestRepository,
    assemble_plan_material_manifest,
)
from core.prepared_resume_material import (
    ApprovedResumeReuseMaterial,
    PreparedResumeMaterialStatus,
    PrivateHomePreparedResumeMaterialRepository,
    PublishApprovedResumeReuseCommand,
    publish_approved_resume_reuse,
)
from core.private_home import PrivateHome
from core.resume_candidates import (
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
    ResumeSummarySource,
    ResumeSummaryTrust,
    register_resume_candidate,
)
from core.resume_selection import (
    PrivateHomeResumeSelectionDecisionRepository,
    SelectBaseResumeCommand,
    select_base_resume,
)

from test_resume_selection import (
    FakeAgent,
    FakeJobRepository,
    METADATA,
    _job,
    _selected,
)
from test_resume_visual_qa import synthetic_pdf


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_p2_publication_and_manifest_preserve_selected_pdf_bytes(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    job = _job()
    plan = ApplicationPlan.create(
        subject_id="subject-p2-reuse",
        job_id=job.job_id,
        job_revision=job.revision,
        job_content_hash=job.content_hash,
        priority_decision_id="priority-decision-p2-reuse",
        policy_id="priority-policy-p2-reuse",
        policy_version=1,
        policy_content_hash="a" * 64,
        accepted_job_intent_id="accepted-intent-p2-reuse",
        priority_level=ProposedPriorityLevel.P2,
        created_at=NOW,
    )
    plan_repository = PrivateHomeApplicationPlanRepository(home)
    assert plan_repository.save(plan).plan == plan

    source_bytes = synthetic_pdf(pages=1)
    source_path = home.paths.master_documents / "approved-p2.pdf"
    source_path.write_bytes(source_bytes)
    candidate_repository = PrivateHomeResumeCandidateRepository(home)
    registered = register_resume_candidate(
        RegisterResumeCandidateCommand(
            subject_id=plan.subject_id,
            artifact_path=source_path,
            display_name="Approved P2 resume",
            selection_safe_summary="User-confirmed software resume.",
            summary_source=ResumeSummarySource.AUTHENTICATED_CALLER,
            summary_trust=ResumeSummaryTrust.USER_CONFIRMED,
            now=NOW,
        ),
        home=home,
        repository=candidate_repository,
    )
    assert registered.candidate is not None
    candidate = registered.candidate

    selection_repository = PrivateHomeResumeSelectionDecisionRepository(home)
    selected = await select_base_resume(
        SelectBaseResumeCommand(plan.subject_id, plan.plan_id, NOW),
        application_plan_repository=plan_repository,
        job_repository=FakeJobRepository(job),
        candidate_provider=candidate_repository,
        agent=FakeAgent(_selected(candidate)),
        metadata=METADATA,
        decision_repository=selection_repository,
    )
    assert selected.decision is not None

    material_repository = PrivateHomePreparedResumeMaterialRepository(home)
    published = publish_approved_resume_reuse(
        PublishApprovedResumeReuseCommand(
            plan.subject_id,
            plan.plan_id,
            selected.decision.decision_id,
            NOW,
        ),
        application_plan_repository=plan_repository,
        selection_repository=selection_repository,
        candidate_repository=candidate_repository,
        material_repository=material_repository,
        home=home,
    )

    assert published.status is PreparedResumeMaterialStatus.CREATED
    assert isinstance(published.material, ApprovedResumeReuseMaterial)
    material = published.material
    assert material.pdf_reference == candidate.artifact_reference
    assert material.pdf_sha256 == candidate.artifact_sha256
    assert material.source_artifact_sha256 == candidate.artifact_sha256
    managed_path = home.contained_path(material.pdf_reference)
    assert managed_path.read_bytes() == source_bytes
    assert hashlib.sha256(managed_path.read_bytes()).hexdigest() == (
        candidate.artifact_sha256
    )

    assembled = assemble_plan_material_manifest(
        AssemblePlanMaterialManifestCommand(
            plan.subject_id, plan.plan_id, material.material_id, NOW
        ),
        application_plan_repository=plan_repository,
        prepared_resume_repository=material_repository,
        manifest_repository=PrivateHomePlanMaterialManifestRepository(home),
        home=home,
    )
    assert assembled.status is PlanMaterialManifestStatus.CREATED
    assert assembled.manifest is not None
    assert assembled.manifest.resume_artifact_sha256 == (
        candidate.artifact_sha256
    )
    assert assembled.manifest.entries[0].artifact_reference == (
        candidate.artifact_reference
    )
