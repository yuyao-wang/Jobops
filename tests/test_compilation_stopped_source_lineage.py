"""Focused P2b4e2 immutable Compilation stopped-source tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import core.application_preparation_orchestrator as preparation_module
from core.application_preparation_orchestrator import (
    APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION,
    ApplicationPreparationStageResult,
    LatexCompilationStopReason,
    PreparationStageOutcome,
    ResolvedCompilationSourceLineage,
    UnresolvedCompilationSourceLineage,
)
from core.latex_compiler import LatexCompileOutcome, LatexCompileStatus
from core.preparation_invocation import PreparationInvocationBinding
from core.resume_compilation import (
    CompileResumeLatexCommand,
    ResumeCompilationFailureReason,
    compile_resume_latex,
    resume_compilation_public_result,
)
from core.resume_compilation_stopped_source import (
    PrivateHomeResumeCompilationStoppedSourceRepository,
    RepositoryResumeCompilationStoppedSourceProvider,
    ResumeCompilationStoppedSourceReadStatus,
    ResumeCompilationStoppedSourceRecord,
    ResumeCompilationStoppedSourceWriteResult,
    ResumeCompilationStoppedSourceWriteStatus,
)
from core.resume_latex_versions import (
    ResumeLatexVersionReadResult,
    ResumeLatexVersionReadStatus,
)
from tests.test_application_preparation_orchestrator import NOW
from tests.test_resume_compilation import (
    LATEX,
    _FakeCompiler,
    _compile,
    _setup,
)


def _binding(plan_id: str, invocation_id: str) -> PreparationInvocationBinding:
    return PreparationInvocationBinding.create(
        subject_id="subject-a",
        application_plan_id=plan_id,
        invocation_id=invocation_id,
        orchestration_contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
        created_at=NOW,
    )


def _adapt(parts, result, invocation_id, *, repository=None):
    binding = _binding(parts["record"].application_plan_id, invocation_id)
    repository = repository or (
        PrivateHomeResumeCompilationStoppedSourceRepository(parts["home"])
    )
    return resume_compilation_public_result(
        result,
        preparation_invocation_binding=binding,
        application_plan_id=parts["record"].application_plan_id,
        stopped_source_repository=repository,
    )


def test_content_stops_persist_distinct_exact_resolved_records(
    tmp_path,
) -> None:
    unmanaged_parts = _setup(
        tmp_path / "unmanaged",
        source=LATEX.replace(
            "\\end{document}",
            "\\input{extra.tex}\n\\end{document}",
        ),
    )
    error_parts = _setup(tmp_path / "error")
    error_result = _compile(
        error_parts,
        _FakeCompiler(
            LatexCompileOutcome(
                status=LatexCompileStatus.COMPILATION_ERROR,
                pdf_bytes=None,
                diagnostics="bounded",
                exit_code=1,
                compiler_started=True,
            )
        ),
    )
    cases = (
        (
            unmanaged_parts,
            _compile(unmanaged_parts, _FakeCompiler()),
            LatexCompilationStopReason.UNMANAGED_DEPENDENCY,
        ),
        (
            error_parts,
            error_result,
            LatexCompilationStopReason.COMPILATION_ERROR,
        ),
    )

    for index, (parts, result, expected_reason) in enumerate(cases):
        public = _adapt(parts, result, f"content-stop-{index}")
        assert public.stopped_source_ref is not None
        provider = RepositoryResumeCompilationStoppedSourceProvider(
            PrivateHomeResumeCompilationStoppedSourceRepository(parts["home"])
        )
        read = provider.get(
            subject_id="subject-a",
            stopped_source_ref=public.stopped_source_ref,
        )
        assert read.status is ResumeCompilationStoppedSourceReadStatus.FOUND
        record = read.record
        assert record is not None
        assert record.stop_reason.code is expected_reason
        assert isinstance(
            record.source_resolution_lineage,
            ResolvedCompilationSourceLineage,
        )
        lineage = record.source_resolution_lineage
        assert lineage.construction_result_id == parts["record"].record_id
        assert lineage.latex_version_id == parts["version"].latex_version_id
        assert lineage.source_content_hash == parts["version"].source_sha256
        stage = ApplicationPreparationStageResult.from_public(
            public,
            preparation_invocation_ref=(
                record.preparation_invocation_ref
            ),
        )
        assert stage.stopped_source_ref == record.reference
        assert stage.to_dict()["stopped_source_ref"] == (
            record.reference.to_dict()
        )


def test_resolved_infrastructure_and_unresolved_early_stops_are_distinct(
    tmp_path,
) -> None:
    parts = _setup(tmp_path)
    unavailable = _compile(
        parts,
        _FakeCompiler(describe_error=RuntimeError("synthetic unavailable")),
    )
    missing_construction = compile_resume_latex(
        CompileResumeLatexCommand(
            subject_id="subject-a",
            resume_latex_construction_record_id=(
                "resume-latex-construction-" + "9" * 64
            ),
            resume_latex_version_id=parts["version"].latex_version_id,
            now=NOW,
        ),
        construction_repository=parts["construction_repository"],
        latex_version_repository=parts["latex_repository"],
        compiler=_FakeCompiler(),
        compilation_repository=parts["compilation_repository"],
        home=parts["home"],
    )

    class _MissingVersion:
        def get(self, *, subject_id, latex_version_id):
            return ResumeLatexVersionReadResult(
                status=ResumeLatexVersionReadStatus.NOT_FOUND,
                version=None,
            )

    missing_version = compile_resume_latex(
        CompileResumeLatexCommand(
            subject_id="subject-a",
            resume_latex_construction_record_id=parts["record"].record_id,
            resume_latex_version_id=parts["version"].latex_version_id,
            now=NOW,
        ),
        construction_repository=parts["construction_repository"],
        latex_version_repository=_MissingVersion(),
        compiler=_FakeCompiler(),
        compilation_repository=parts["compilation_repository"],
        home=parts["home"],
    )
    invalid = _compile(
        parts,
        _FakeCompiler(),
        now=datetime(2026, 7, 29, 15, 0),
    )
    expected = (
        (unavailable, ResolvedCompilationSourceLineage),
        (invalid, UnresolvedCompilationSourceLineage),
        (missing_construction, UnresolvedCompilationSourceLineage),
        (missing_version, UnresolvedCompilationSourceLineage),
    )
    for index, (result, lineage_type) in enumerate(expected):
        public = _adapt(parts, result, f"source-kind-{index}")
        repository = PrivateHomeResumeCompilationStoppedSourceRepository(
            parts["home"]
        )
        read = repository.get(
            subject_id="subject-a",
            record_id=public.stopped_source_ref.record_id,
        )
        assert isinstance(read.record.source_resolution_lineage, lineage_type)
        if lineage_type is UnresolvedCompilationSourceLineage:
            serialized = read.record.source_resolution_lineage.to_dict()
            assert "source_content_hash" not in serialized
            assert "construction_result_id" not in serialized


def test_binding_and_reference_drift_fail_closed(tmp_path) -> None:
    parts = _setup(
        tmp_path,
        source=LATEX.replace(
            "\\end{document}",
            "\\input{extra.tex}\n\\end{document}",
        ),
    )
    public = _adapt(
        parts,
        _compile(parts, _FakeCompiler()),
        "binding-validation",
    )
    repository = PrivateHomeResumeCompilationStoppedSourceRepository(
        parts["home"]
    )
    provider = RepositoryResumeCompilationStoppedSourceProvider(repository)
    cross_subject = provider.get(
        subject_id="subject-b",
        stopped_source_ref=public.stopped_source_ref,
    )
    assert cross_subject.status is (
        ResumeCompilationStoppedSourceReadStatus.NOT_FOUND
    )
    found = provider.get(
        subject_id="subject-a",
        stopped_source_ref=public.stopped_source_ref,
    )
    record = found.record
    with pytest.raises(ValueError):
        ResumeCompilationStoppedSourceRecord.create(
            subject_id="subject-a",
            application_plan_id="different-plan",
            preparation_invocation_ref=record.preparation_invocation_ref,
            compilation_attempt_id=record.compilation_attempt_id,
            outcome=record.outcome,
            stop_reason=record.stop_reason,
            source_resolution_lineage=record.source_resolution_lineage,
            created_at=NOW,
        )
    with pytest.raises(ValueError):
        ResumeCompilationStoppedSourceRecord.create(
            subject_id=record.subject_id,
            application_plan_id=record.application_plan_id,
            preparation_invocation_ref=record.preparation_invocation_ref,
            compilation_attempt_id=record.compilation_attempt_id,
            outcome=PreparationStageOutcome.FAILED,
            stop_reason=record.stop_reason,
            source_resolution_lineage=record.source_resolution_lineage,
            created_at=NOW,
        )


def test_replay_repository_failure_and_legacy_absence_do_not_forge_refs(
    tmp_path,
) -> None:
    parts = _setup(
        tmp_path,
        source=LATEX.replace(
            "\\end{document}",
            "\\input{extra.tex}\n\\end{document}",
        ),
    )
    result = _compile(parts, _FakeCompiler())
    repository = PrivateHomeResumeCompilationStoppedSourceRepository(
        parts["home"]
    )
    first = _adapt(parts, result, "replay", repository=repository)
    replay = _adapt(parts, result, "replay", repository=repository)
    assert replay.stopped_source_ref == first.stopped_source_ref
    assert len(
        tuple(
            parts["home"].paths.resume_compilation_stopped_sources.rglob(
                "*.json"
            )
        )
    ) == 1

    class _FailingRepository:
        def __init__(self):
            self.save_calls = 0

        def save(self, record):
            self.save_calls += 1
            return ResumeCompilationStoppedSourceWriteResult(
                ResumeCompilationStoppedSourceWriteStatus.FAILED,
                None,
                retryable=True,
            )

        def get(self, *, subject_id, record_id):
            raise AssertionError("get must not be called after save failure")

    failing = _FailingRepository()
    failed = _adapt(
        parts,
        result,
        "repository-failure",
        repository=failing,
    )
    assert failing.save_calls == 1
    assert failed.outcome is PreparationStageOutcome.FAILED
    assert failed.stop_reason.code is (
        LatexCompilationStopReason.RECORD_PERSISTENCE_FAILED
    )
    assert failed.stopped_source_ref is None

    legacy_adapter = resume_compilation_public_result(result)
    assert legacy_adapter.stopped_source_ref is None

    current_stage = ApplicationPreparationStageResult.from_public(
        first,
        preparation_invocation_ref=(
            first.compilation_source_lineage.invocation_binding_ref
        ),
    )
    historical_v3 = current_stage.to_dict()
    historical_v3.pop("stopped_source_ref")
    historical_v3["stage_content_hash"] = (
        preparation_module._canonical_hash(
            {
                key: value
                for key, value in historical_v3.items()
                if key != "stage_content_hash"
            }
        )
    )
    restored = preparation_module._stage_result_from_dict(
        historical_v3,
        run_contract_version=(
            APPLICATION_PREPARATION_ORCHESTRATION_CONTRACT_VERSION
        ),
    )
    assert restored.stopped_source_ref is None
    assert restored.historical_missing_stopped_source_ref is True
    assert restored.to_dict() == historical_v3
