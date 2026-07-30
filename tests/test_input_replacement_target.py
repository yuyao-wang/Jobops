"""Focused P2b5f Input Replacement Target tests."""

from __future__ import annotations

from pathlib import Path

from core.application_answers import (
    PrivateHomePreparedApplicationAnswerSetRepository,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.application_preparation_orchestrator import (
    LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION,
    SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION,
    ApplicationPreparationStage,
    LatexConstructionStopReason,
    PreparationStageOutcome,
    PreparationStopReasonEnvelope,
    PrivateHomeApplicationPreparationRunRepository,
    PublicPreparationStageResult,
    SourceResumeProjectionStopReason,
)
from core.human_attention_queue import (
    HumanAttentionResolutionCapability,
    build_current_human_attention_queue,
)
from core.input_replacement_target import (
    REPLACE_INPUT_TARGET_KIND_REGISTRY,
    BaseLatexVersionReplacementTarget,
    InputReplacementTargetKind,
    InputReplacementTargetProvider,
    InputReplacementTargetStatus,
    PrivateHomeInputReplacementTargetRepository,
    SourceResumeReplacementTarget,
)
from core.private_home import PrivateHome
from core.resume_candidates import PrivateHomeResumeCandidateRepository
from core.resume_latex_versions import (
    PrivateHomeResumeLatexVersionRepository,
)
from dashboard.input_replacement_target import (
    InputReplacementTargetUIController,
)
from tests.test_application_preparation_orchestrator import (
    _Recorder,
    _hash,
    _recipe,
)
from tests.test_human_attention_queue import (
    NOW,
    SUBJECT,
    _invoke,
    _plan,
)
from tests.test_resume_candidates import _register as _register_resume
from tests.test_resume_latex_versions import _register as _register_latex


def _queue(home, plans, runs, projector=None):
    return build_current_human_attention_queue(
        subject_id=SUBJECT,
        now=NOW,
        run_repository=runs,
        application_plan_repository=plans,
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
        input_replacement_target_projector=projector,
    )


def _provider(home, candidates, versions):
    return InputReplacementTargetProvider(
        repository=PrivateHomeInputReplacementTargetRepository(home),
        resume_candidate_provider=candidates,
        latex_version_provider=versions,
    )


async def test_source_resume_reasons_bind_exact_candidate_and_replay(
    tmp_path: Path,
) -> None:
    for reason in (
        SourceResumeProjectionStopReason.FORMAT_UNSUPPORTED,
        SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE,
    ):
        home = PrivateHome(tmp_path / reason.value)
        home.ensure()
        candidates, registered = _register_resume(
            home, subject_id=SUBJECT
        )
        candidate = registered.candidate
        versions = PrivateHomeResumeLatexVersionRepository(home)
        plan, plans = _plan(home, job_id=f"job-{reason.value.lower()}")
        runs = PrivateHomeApplicationPreparationRunRepository(home)
        recorder = _Recorder()

        def selected(request):
            return PublicPreparationStageResult.completed(
                stage=request.stage,
                result_id="resume-selection-synthetic",
                result_content_hash=_hash("resume-selection"),
                outputs={
                    "resume_artifact_sha256": candidate.artifact_sha256,
                    "resume_id": candidate.resume_id,
                    "resume_selection_decision_id": (
                        "resume-selection-synthetic"
                    ),
                },
            )

        def stopped(request):
            return PublicPreparationStageResult.deferred(
                stage=request.stage,
                stop_reason=PreparationStopReasonEnvelope(
                    stage=request.stage,
                    code=reason,
                    contract_version=(
                        SOURCE_RESUME_PROJECTION_STOP_REASON_CONTRACT_VERSION
                    ),
                    outcome=PreparationStageOutcome.DEFERRED,
                ),
            )

        await _invoke(
            plan=plan,
            plan_repository=plans,
            run_repository=runs,
            recipe=_recipe(
                recorder,
                input_binding=reason.value,
                overrides={
                    ApplicationPreparationStage.BASE_RESUME_SELECTION: selected,
                    ApplicationPreparationStage.SOURCE_RESUME_PROJECTION: stopped,
                },
            ),
        )
        provider = _provider(home, candidates, versions)
        first = _queue(home, plans, runs, provider)
        second = _queue(home, plans, runs, provider)
        item = first.items[0]
        provider.current_item_reader = (
            lambda subject_id, item_id: item
            if subject_id == SUBJECT and item_id == item.item_id
            else None
        )
        safe = provider.get_current_input_replacement_target(
            subject_id=SUBJECT, attention_item_id=item.item_id
        )
        stored = provider.repository.get(
            subject_id=SUBJECT,
            target_id=item.replacement_target_ref.target_id,
        ).target

        assert item.resolution_capability is (
            HumanAttentionResolutionCapability.REPLACE_INPUT
        )
        assert (
            item.replacement_target_ref
            == second.items[0].replacement_target_ref
        )
        assert isinstance(stored.payload, SourceResumeReplacementTarget)
        assert stored.payload.resume_candidate_id == candidate.resume_id
        assert stored.payload.source_content_hash == candidate.artifact_sha256
        assert safe.status is InputReplacementTargetStatus.AVAILABLE
        assert safe.safe_target.display_name == candidate.display_name
        assert "artifact_reference" not in str(safe.safe_target)
        ui = (await InputReplacementTargetUIController(
            target_reader=(
                provider.get_current_input_replacement_target
            )
        ).get(
            context=AuthenticatedSubjectContext(
                session_id="session-input-replacement-0123456789",
                subject_id=SUBJECT,
                authentication_method=(
                    AuthenticationMethod.LOCAL_KEYCHAIN_SESSION
                ),
                issued_at=NOW,
                expires_at=NOW.replace(year=NOW.year + 1),
            ),
            attention_item_id=item.item_id,
        )).to_dict()
        serialized = str(ui).casefold()
        assert "artifact_reference" not in serialized
        assert "source_content_hash" not in serialized
        assert "/private/" not in serialized
        assert "credential" not in serialized
        home.contained_path(candidate.artifact_reference).write_bytes(
            b"%PDF-1.7\nsynthetic drift\n%%EOF\n"
        )
        stale = provider.get_current_input_replacement_target(
            subject_id=SUBJECT, attention_item_id=item.item_id
        )
        assert stale.status is InputReplacementTargetStatus.TARGET_STALE


async def test_base_latex_reason_binds_exact_version_and_source_record(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "latex")
    home.ensure()
    candidates = PrivateHomeResumeCandidateRepository(home)
    versions = PrivateHomeResumeLatexVersionRepository(home)
    version = _register_latex(
        home, subject_id=SUBJECT, repository=versions
    ).version
    plan, plans = _plan(home, job_id="job-latex-unreadable")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    recorder = _Recorder()

    def selected(request):
        return PublicPreparationStageResult.completed(
            stage=request.stage,
            result_id="base-latex-selection-synthetic",
            result_content_hash=_hash("base-latex-selection"),
            outputs={
                "base_latex_selection_id": "base-latex-selection-synthetic",
                "selected_latex_source_sha256": version.source_sha256,
                "selected_latex_version_id": version.latex_version_id,
                "selected_root_family_id": version.root_family_id,
            },
        )

    def stopped(request):
        return PublicPreparationStageResult.deferred(
            stage=request.stage,
            stop_reason=PreparationStopReasonEnvelope(
                stage=request.stage,
                code=LatexConstructionStopReason.BASE_VERSION_UNREADABLE,
                contract_version=(
                    LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION
                ),
                outcome=PreparationStageOutcome.DEFERRED,
            ),
        )

    await _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=_recipe(
            recorder,
            input_binding="latex-unreadable",
            overrides={
                ApplicationPreparationStage.BASE_LATEX_SELECTION: selected,
                ApplicationPreparationStage.LATEX_CONSTRUCTION: stopped,
            },
        ),
    )
    provider = _provider(home, candidates, versions)
    queue = _queue(home, plans, runs, provider)
    item = queue.items[0]
    stored = provider.repository.get(
        subject_id=SUBJECT,
        target_id=item.replacement_target_ref.target_id,
    ).target

    assert isinstance(stored.payload, BaseLatexVersionReplacementTarget)
    assert stored.target_kind is InputReplacementTargetKind.BASE_LATEX_VERSION
    assert stored.payload.latex_version_id == version.latex_version_id
    assert stored.payload.version_family == version.root_family_id
    assert stored.payload.source_record_id == version.latex_version_id
    assert stored.payload.source_content_hash == version.source_sha256


async def test_incomplete_lineage_and_identity_drift_fail_closed(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "incomplete")
    home.ensure()
    versions = PrivateHomeResumeLatexVersionRepository(home)
    candidates = PrivateHomeResumeCandidateRepository(home)
    plan, plans = _plan(home, job_id="job-incomplete")
    runs = PrivateHomeApplicationPreparationRunRepository(home)
    recorder = _Recorder()

    def selected(request):
        return PublicPreparationStageResult.completed(
            stage=request.stage,
            result_id="base-latex-selection-incomplete",
            result_content_hash=_hash("base-latex-incomplete"),
            outputs={
                "base_latex_selection_id": "base-latex-selection-incomplete"
            },
        )

    def stopped(request):
        return PublicPreparationStageResult.deferred(
            stage=request.stage,
            stop_reason=PreparationStopReasonEnvelope(
                stage=request.stage,
                code=LatexConstructionStopReason.BASE_VERSION_UNREADABLE,
                contract_version=(
                    LATEX_CONSTRUCTION_STOP_REASON_CONTRACT_VERSION
                ),
                outcome=PreparationStageOutcome.DEFERRED,
            ),
        )

    await _invoke(
        plan=plan,
        plan_repository=plans,
        run_repository=runs,
        recipe=_recipe(
            recorder,
            input_binding="incomplete",
            overrides={
                ApplicationPreparationStage.BASE_LATEX_SELECTION: selected,
                ApplicationPreparationStage.LATEX_CONSTRUCTION: stopped,
            },
        ),
    )
    provider = _provider(home, candidates, versions)
    item = _queue(home, plans, runs, provider).items[0]
    provider.current_item_reader = lambda *_args: item
    result = provider.get_current_input_replacement_target(
        subject_id=SUBJECT, attention_item_id=item.item_id
    )

    assert item.replacement_target_ref is None
    assert result.status is InputReplacementTargetStatus.TARGET_INCOMPLETE


async def test_registry_coverage_and_safe_projection_are_exact() -> None:
    expected = {
        (
            ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
            SourceResumeProjectionStopReason.FORMAT_UNSUPPORTED,
        ),
        (
            ApplicationPreparationStage.SOURCE_RESUME_PROJECTION,
            SourceResumeProjectionStopReason.ARTIFACT_UNREADABLE,
        ),
        (
            ApplicationPreparationStage.LATEX_CONSTRUCTION,
            LatexConstructionStopReason.BASE_VERSION_UNREADABLE,
        ),
    }
    assert set(REPLACE_INPUT_TARGET_KIND_REGISTRY) == expected
    assert len(REPLACE_INPUT_TARGET_KIND_REGISTRY) == 3
