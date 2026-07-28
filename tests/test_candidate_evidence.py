from __future__ import annotations

import ast
import hashlib
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import core.candidate_evidence as evidence_module
from core.application_plan import ApplicationPlan, PrivateHomeApplicationPlanRepository
from core.candidate_evidence import (
    CANDIDATE_EVIDENCE_CONTRACT_VERSION,
    CandidateEvidenceFailureReason,
    CandidateEvidenceItem,
    CandidateEvidenceScope,
    CandidateEvidenceSensitivity,
    CandidateEvidenceSnapshot,
    CandidateEvidenceSnapshotReadStatus,
    CandidateEvidenceSnapshotStatus,
    CandidateEvidenceSnapshotWriteStatus,
    CandidateEvidenceSourceType,
    CandidateEvidenceVerificationStatus,
    CreateCandidateEvidenceSnapshotCommand,
    PrivateHomeCandidateEvidenceSnapshotRepository,
    candidate_evidence_snapshot_id,
    create_candidate_evidence_snapshot,
)
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome
from core.resume_candidates import (
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
    ResumeCandidate,
    ResumeCandidateReadResult,
    ResumeCandidateReadStatus,
    ResumeSummarySource,
    ResumeSummaryTrust,
    register_resume_candidate,
)
from core.resume_selection import (
    RESUME_SELECTION_CONTRACT_VERSION,
    PrivateHomeResumeSelectionDecisionRepository,
    ResumeSelectionDecision,
    ResumeSelectionDecisionReadResult,
    ResumeSelectionDecisionReadStatus,
    ResumeSelectionMethod,
)
from core.source_resume_projection import (
    SOURCE_RESUME_PROJECTION_CONTRACT_VERSION,
    DeterministicSourceResumeParser,
    PrivateHomeSourceResumeArtifactReader,
    PrivateHomeSourceResumeProjectionRepository,
    SourceResumeProjection,
    SourceResumeProjectionReadResult,
    SourceResumeProjectionReadStatus,
    create_source_resume_projection,
    source_resume_projection_id,
    CreateSourceResumeProjectionCommand,
)


NOW = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
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


def _docx_bytes(suffix: str = "") -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Experience{suffix}</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>Built deterministic geospatial pipelines.</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Python and remote sensing.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def _register(
    home: PrivateHome,
    *,
    subject_id: str = "subject-a",
    suffix: str = "",
) -> tuple[PrivateHomeResumeCandidateRepository, ResumeCandidate]:
    artifact = home.paths.master_documents / f"{subject_id}{suffix}.docx"
    artifact.write_bytes(_docx_bytes(suffix))
    repository = PrivateHomeResumeCandidateRepository(home)
    result = register_resume_candidate(
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
        repository=repository,
    )
    assert result.candidate is not None
    return repository, result.candidate


def _plan(
    home: PrivateHome,
    *,
    subject_id: str = "subject-a",
    suffix: str = "",
    created_at: datetime = NOW,
) -> tuple[PrivateHomeApplicationPlanRepository, ApplicationPlan]:
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id=f"job-{suffix or 'one'}",
        job_revision=1,
        job_content_hash=_hash({"job": suffix or "one"}),
        priority_decision_id=f"priority-decision-{suffix or 'one'}",
        policy_id=f"policy-{suffix or 'one'}",
        policy_version=1,
        policy_content_hash=_hash({"policy": suffix or "one"}),
        accepted_job_intent_id=f"accepted-intent-{suffix or 'one'}",
        priority_level=ProposedPriorityLevel.P1,
        created_at=created_at,
    )
    repository = PrivateHomeApplicationPlanRepository(home)
    repository.save(plan)
    return repository, plan


def _selection(
    home: PrivateHome,
    *,
    plan: ApplicationPlan,
    candidate: ResumeCandidate,
    suffix: str = "one",
    selected_at: datetime = NOW,
) -> tuple[
    PrivateHomeResumeSelectionDecisionRepository,
    ResumeSelectionDecision,
]:
    binding = _hash({"selection": suffix, "plan": plan.plan_id})
    values = {
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
    decision = ResumeSelectionDecision(
        decision_content_hash=_hash(values),
        selected_at=selected_at,
        **values,
    )
    repository = PrivateHomeResumeSelectionDecisionRepository(home)
    repository.save(decision)
    return repository, decision


def _projection(
    home: PrivateHome,
    *,
    candidate_repository: PrivateHomeResumeCandidateRepository,
    candidate: ResumeCandidate,
    parser_version: str = "source-resume-parser-v1",
    projected_at: datetime = NOW,
) -> tuple[
    PrivateHomeSourceResumeProjectionRepository,
    SourceResumeProjection,
]:
    repository = PrivateHomeSourceResumeProjectionRepository(home)
    result = create_source_resume_projection(
        CreateSourceResumeProjectionCommand(
            subject_id=candidate.subject_id,
            resume_id=candidate.resume_id,
            now=projected_at,
        ),
        candidate_repository=candidate_repository,
        artifact_reader=PrivateHomeSourceResumeArtifactReader(home),
        parser=DeterministicSourceResumeParser(parser_version),
        projection_repository=repository,
    )
    assert result.projection is not None
    return repository, result.projection


def _setup(tmp_path: Path, *, subject_id: str = "subject-a"):
    home = _home(tmp_path)
    plan_repository, plan = _plan(home, subject_id=subject_id)
    candidate_repository, candidate = _register(
        home, subject_id=subject_id
    )
    selection_repository, selection = _selection(
        home, plan=plan, candidate=candidate
    )
    projection_repository, projection = _projection(
        home,
        candidate_repository=candidate_repository,
        candidate=candidate,
    )
    snapshot_repository = PrivateHomeCandidateEvidenceSnapshotRepository(home)
    return {
        "home": home,
        "plan_repository": plan_repository,
        "plan": plan,
        "candidate_repository": candidate_repository,
        "candidate": candidate,
        "selection_repository": selection_repository,
        "selection": selection,
        "projection_repository": projection_repository,
        "projection": projection,
        "snapshot_repository": snapshot_repository,
    }


def _create(parts, *, subject_id: str = "subject-a", now: datetime = NOW):
    return create_candidate_evidence_snapshot(
        CreateCandidateEvidenceSnapshotCommand(
            subject_id=subject_id,
            application_plan_id=parts["plan"].plan_id,
            now=now,
        ),
        application_plan_repository=parts["plan_repository"],
        selection_repository=parts["selection_repository"],
        candidate_repository=parts["candidate_repository"],
        projection_repository=parts["projection_repository"],
        snapshot_repository=parts["snapshot_repository"],
    )


def _projection_blocks(projection: SourceResumeProjection):
    return tuple(
        (section, block)
        for section in projection.sections
        for block in section.blocks
    )


def test_projection_builds_ordered_exact_typed_evidence(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)

    result = _create(parts)

    assert result.status is CandidateEvidenceSnapshotStatus.CREATED
    snapshot = result.snapshot
    assert snapshot is not None
    source = _projection_blocks(parts["projection"])
    assert tuple(item.order for item in snapshot.evidence_items) == (0, 1, 2)
    assert tuple(item.evidence_text for item in snapshot.evidence_items) == tuple(
        block.text for _, block in source
    )
    for item, (section, block) in zip(snapshot.evidence_items, source):
        assert item.source_section_id == section.section_id
        assert item.source_block_id == block.block_id
        assert item.source_bullet_id == block.bullet_id
        assert item.source_locator == block.locator
        assert item.source_projection_id == parts["projection"].projection_id
        assert (
            item.source_projection_hash
            == parts["projection"].projection_content_hash
        )


def test_evidence_uses_conservative_trust_scope_and_no_expiry(
    tmp_path: Path,
) -> None:
    result = _create(_setup(tmp_path))
    assert result.snapshot is not None

    for item in result.snapshot.evidence_items:
        assert (
            item.source_type
            is CandidateEvidenceSourceType.SOURCE_RESUME_PROJECTION
        )
        assert item.sensitivity is CandidateEvidenceSensitivity.PERSONAL
        assert item.allowed_scopes == (
            CandidateEvidenceScope.RESUME_TAILORING,
        )
        assert (
            item.verification_status
            is CandidateEvidenceVerificationStatus
            .USER_PROVIDED_DOCUMENT_STATEMENT
        )
        assert item.verified_at is None
        assert item.expires_at is None
        assert item.recorded_at == result.snapshot.created_at


def test_replay_and_restart_preserve_ids_hashes_and_created_time(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _create(parts)
    restarted = dict(parts)
    restarted["plan_repository"] = PrivateHomeApplicationPlanRepository(
        PrivateHome(parts["home"].root)
    )
    restarted["selection_repository"] = (
        PrivateHomeResumeSelectionDecisionRepository(
            PrivateHome(parts["home"].root)
        )
    )
    restarted["candidate_repository"] = (
        PrivateHomeResumeCandidateRepository(
            PrivateHome(parts["home"].root)
        )
    )
    restarted["projection_repository"] = (
        PrivateHomeSourceResumeProjectionRepository(
            PrivateHome(parts["home"].root)
        )
    )
    restarted["snapshot_repository"] = (
        PrivateHomeCandidateEvidenceSnapshotRepository(
            PrivateHome(parts["home"].root)
        )
    )

    replay = _create(restarted, now=NOW + timedelta(days=1))

    assert replay.status is CandidateEvidenceSnapshotStatus.UNCHANGED
    assert replay.snapshot == first.snapshot
    assert replay.snapshot.created_at == NOW
    assert [
        item.evidence_id for item in replay.snapshot.evidence_items
    ] == [item.evidence_id for item in first.snapshot.evidence_items]


class _SelectionResultRepository:
    def __init__(self, decision: ResumeSelectionDecision) -> None:
        self.decision = decision

    def find_current_for_plan(self, **_kwargs):
        return ResumeSelectionDecisionReadResult(
            status=ResumeSelectionDecisionReadStatus.FOUND,
            decision=self.decision,
        )


class _CandidateResultRepository:
    def __init__(self, candidate: ResumeCandidate) -> None:
        self.candidate = candidate

    def get(self, **_kwargs):
        return ResumeCandidateReadResult(
            status=ResumeCandidateReadStatus.FOUND,
            candidate=self.candidate,
        )


class _ProjectionResultRepository:
    def __init__(self, projection: SourceResumeProjection) -> None:
        self.projection = projection

    def find_current_for_resume(self, **_kwargs):
        return SourceResumeProjectionReadResult(
            status=SourceResumeProjectionReadStatus.FOUND,
            projection=self.projection,
        )


@pytest.mark.parametrize(
    ("target", "field", "reason"),
    [
        (
            "selection",
            "job_content_hash",
            CandidateEvidenceFailureReason.RESUME_SELECTION_BINDING_MISMATCH,
        ),
        (
            "candidate",
            "artifact_sha256",
            CandidateEvidenceFailureReason.RESUME_CANDIDATE_BINDING_MISMATCH,
        ),
        (
            "projection",
            "artifact_sha256",
            CandidateEvidenceFailureReason.SOURCE_PROJECTION_BINDING_MISMATCH,
        ),
    ],
)
def test_binding_mismatch_fails_without_snapshot(
    tmp_path: Path,
    target: str,
    field: str,
    reason: CandidateEvidenceFailureReason,
) -> None:
    parts = _setup(tmp_path)
    damaged = parts[target]
    object.__setattr__(damaged, field, "f" * 64)
    if target == "selection":
        parts["selection_repository"] = _SelectionResultRepository(damaged)
    elif target == "candidate":
        parts["candidate_repository"] = _CandidateResultRepository(damaged)
    else:
        parts["projection_repository"] = _ProjectionResultRepository(damaged)

    result = _create(parts)

    assert result.status is CandidateEvidenceSnapshotStatus.FAILED
    assert result.reason_code is reason
    assert not tuple(
        parts["home"].paths.candidate_evidence_snapshots.rglob("*.json")
    )


def test_missing_or_changed_locator_and_projection_hash_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    projection = parts["projection"]
    block = projection.sections[0].blocks[0]
    object.__setattr__(block, "locator", None)
    object.__setattr__(projection, "projection_content_hash", "e" * 64)
    parts["projection_repository"] = _ProjectionResultRepository(projection)

    result = _create(parts)

    assert result.status is CandidateEvidenceSnapshotStatus.FAILED
    assert (
        result.reason_code
        is CandidateEvidenceFailureReason.SOURCE_PROJECTION_BINDING_MISMATCH
    )


def test_empty_projection_defers_without_snapshot(tmp_path: Path) -> None:
    parts = _setup(tmp_path)
    candidate = parts["candidate"]
    parser_version = "empty-source-resume-parser-v1"
    projection_id = source_resume_projection_id(
        subject_id=candidate.subject_id,
        resume_id=candidate.resume_id,
        artifact_sha256=candidate.artifact_sha256,
        artifact_type=candidate.artifact_type,
        parser_version=parser_version,
    )
    content = {
        "artifact_sha256": candidate.artifact_sha256,
        "artifact_type": candidate.artifact_type.value,
        "contract_version": SOURCE_RESUME_PROJECTION_CONTRACT_VERSION,
        "parser_version": parser_version,
        "resume_id": candidate.resume_id,
        "subject_id": candidate.subject_id,
        "projection_id": projection_id,
        "sections": [],
    }
    empty = SourceResumeProjection(
        projection_id=projection_id,
        contract_version=SOURCE_RESUME_PROJECTION_CONTRACT_VERSION,
        parser_version=parser_version,
        subject_id=candidate.subject_id,
        resume_id=candidate.resume_id,
        artifact_sha256=candidate.artifact_sha256,
        artifact_type=candidate.artifact_type,
        sections=(),
        projection_content_hash=_hash(content),
        projected_at=NOW + timedelta(minutes=1),
    )
    parts["projection_repository"].save(empty)

    result = _create(parts, now=NOW + timedelta(minutes=2))

    assert (
        result.status
        is CandidateEvidenceSnapshotStatus.DEFERRED_NO_EVIDENCE
    )
    assert result.snapshot is None
    assert not tuple(
        parts["home"].paths.candidate_evidence_snapshots.rglob("*.json")
    )


def test_new_plan_selection_or_projection_creates_new_snapshot(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    first = _create(parts)
    assert first.snapshot is not None
    _, newer_selection = _selection(
        parts["home"],
        plan=parts["plan"],
        candidate=parts["candidate"],
        suffix="newer",
        selected_at=NOW + timedelta(minutes=1),
    )

    selection_changed = _create(parts, now=NOW + timedelta(minutes=2))

    assert selection_changed.status is CandidateEvidenceSnapshotStatus.CREATED
    assert selection_changed.snapshot is not None
    assert (
        selection_changed.snapshot.resume_selection_decision_id
        == newer_selection.decision_id
    )
    assert selection_changed.snapshot.snapshot_id != first.snapshot.snapshot_id

    _, newer_projection = _projection(
        parts["home"],
        candidate_repository=parts["candidate_repository"],
        candidate=parts["candidate"],
        parser_version="source-resume-parser-v2",
        projected_at=NOW + timedelta(minutes=3),
    )
    projection_changed = _create(parts, now=NOW + timedelta(minutes=4))
    assert projection_changed.snapshot is not None
    assert (
        projection_changed.snapshot.source_projection_id
        == newer_projection.projection_id
    )
    assert (
        projection_changed.snapshot.snapshot_id
        != selection_changed.snapshot.snapshot_id
    )

    plan_repository, plan_two = _plan(
        parts["home"],
        subject_id="subject-a",
        suffix="two",
        created_at=NOW + timedelta(minutes=5),
    )
    _selection(
        parts["home"],
        plan=plan_two,
        candidate=parts["candidate"],
        suffix="plan-two",
        selected_at=NOW + timedelta(minutes=5),
    )
    plan_parts = dict(parts)
    plan_parts["plan_repository"] = plan_repository
    plan_parts["plan"] = plan_two
    plan_changed = _create(plan_parts, now=NOW + timedelta(minutes=6))
    assert plan_changed.snapshot is not None
    assert plan_changed.snapshot.snapshot_id not in {
        first.snapshot.snapshot_id,
        selection_changed.snapshot.snapshot_id,
        projection_changed.snapshot.snapshot_id,
    }


def test_contract_version_changes_identity_without_using_time() -> None:
    common = {
        "subject_id": "subject-a",
        "application_plan_id": "plan-a",
        "job_id": "job-a",
        "resume_selection_decision_id": "selection-a",
        "source_resume_id": "resume-a",
        "source_artifact_sha256": "a" * 64,
        "source_projection_id": "projection-a",
        "source_projection_hash": "b" * 64,
        "evidence_item_hashes": ("c" * 64,),
    }

    first = candidate_evidence_snapshot_id(
        contract_version="candidate-evidence-v1", **common
    )
    changed = candidate_evidence_snapshot_id(
        contract_version="candidate-evidence-v2", **common
    )

    assert first != changed


def test_subject_isolation_and_restart_read(tmp_path: Path) -> None:
    parts_a = _setup(tmp_path, subject_id="subject-a")
    result_a = _create(parts_a)
    assert result_a.snapshot is not None
    repository = PrivateHomeCandidateEvidenceSnapshotRepository(
        PrivateHome(parts_a["home"].root)
    )

    cross = repository.get(
        subject_id="subject-b",
        snapshot_id=result_a.snapshot.snapshot_id,
    )
    read = repository.get(
        subject_id="subject-a",
        snapshot_id=result_a.snapshot.snapshot_id,
    )

    assert cross.status is CandidateEvidenceSnapshotReadStatus.NOT_FOUND
    assert read.status is CandidateEvidenceSnapshotReadStatus.FOUND
    assert read.snapshot == result_a.snapshot


def test_repository_conflict_corruption_and_duplicate_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    result = _create(parts)
    snapshot = result.snapshot
    assert snapshot is not None
    repository = parts["snapshot_repository"]
    record = next(
        parts["home"].paths.candidate_evidence_snapshots.rglob(
            f"{snapshot.snapshot_id}.json"
        )
    )
    first_item = snapshot.evidence_items[0]
    duplicate_content = first_item.content_dict()
    duplicate_content["order"] = 1
    duplicate = replace(
        first_item,
        order=1,
        item_content_hash=_hash(duplicate_content),
    )
    with pytest.raises(ValueError, match="unique"):
        CandidateEvidenceSnapshot(
            snapshot_id=snapshot.snapshot_id,
            contract_version=CANDIDATE_EVIDENCE_CONTRACT_VERSION,
            subject_id=snapshot.subject_id,
            application_plan_id=snapshot.application_plan_id,
            job_id=snapshot.job_id,
            resume_selection_decision_id=(
                snapshot.resume_selection_decision_id
            ),
            source_resume_id=snapshot.source_resume_id,
            source_artifact_sha256=snapshot.source_artifact_sha256,
            source_projection_id=snapshot.source_projection_id,
            source_projection_hash=snapshot.source_projection_hash,
            evidence_items=(first_item, duplicate),
            snapshot_content_hash="f" * 64,
            created_at=snapshot.created_at,
        )

    corrupted_bytes = b"{broken"
    record.write_bytes(corrupted_bytes)
    conflict = repository.save(snapshot)
    assert conflict.status is CandidateEvidenceSnapshotWriteStatus.FAILED
    assert (
        conflict.reason_code
        is CandidateEvidenceFailureReason.SNAPSHOT_INTEGRITY_FAILURE
    )
    assert record.read_bytes() == corrupted_bytes

    corrupt = repository.get(
        subject_id=snapshot.subject_id,
        snapshot_id=snapshot.snapshot_id,
    )
    assert (
        corrupt.status
        is CandidateEvidenceSnapshotReadStatus.INTEGRITY_FAILURE
    )


def test_invalid_command_and_missing_upstream_fail_without_snapshot(
    tmp_path: Path,
) -> None:
    parts = _setup(tmp_path)
    naive = _create(parts, now=datetime(2026, 7, 28, 20, 0))
    missing = create_candidate_evidence_snapshot(
        CreateCandidateEvidenceSnapshotCommand(
            subject_id="subject-a",
            application_plan_id="application-plan-" + "0" * 64,
            now=NOW,
        ),
        application_plan_repository=parts["plan_repository"],
        selection_repository=parts["selection_repository"],
        candidate_repository=parts["candidate_repository"],
        projection_repository=parts["projection_repository"],
        snapshot_repository=parts["snapshot_repository"],
    )

    assert naive.reason_code is CandidateEvidenceFailureReason.INVALID_REQUEST
    assert (
        missing.reason_code
        is CandidateEvidenceFailureReason.APPLICATION_PLAN_NOT_FOUND
    )


def test_module_has_no_profile_job_agent_tailoring_or_execution_dependency() -> None:
    module_path = Path(evidence_module.__file__)
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
        "core.job_discovery",
        "core.job_prioritization",
        "core.profile_store",
        "core.priority_agent_adapter",
        "playwright",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
