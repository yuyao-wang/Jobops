from __future__ import annotations

from dataclasses import replace

import pytest

from core.application_preparation_orchestrator import (
    ApplicationPreparationStage,
    PreparationStageOutcome,
)
from core.publication_stopped_lineage import (
    PublicationBlockingDirective,
    PublicationMaterialKind,
    PublicationStoppedSourceKind,
    PublicationStoppedSourceLineage,
    create_publication_stopped_source_lineage,
)


HASH_A = "a" * 64
HASH_B = "b" * 64


def _lineage(
    kind: PublicationStoppedSourceKind,
) -> PublicationStoppedSourceLineage:
    values = {
        PublicationStoppedSourceKind.FACT_QA_BLOCKER: (
            PublicationMaterialKind.RESUME,
            ApplicationPreparationStage.RESUME_PUBLICATION,
            ApplicationPreparationStage.RESUME_FACT_QA,
            PublicationBlockingDirective.FACT_QA_BLOCKED,
            ("finding-1",),
        ),
        PublicationStoppedSourceKind.VISUAL_QA_DIRECTIVE: (
            PublicationMaterialKind.RESUME,
            ApplicationPreparationStage.RESUME_PUBLICATION,
            ApplicationPreparationStage.RESUME_VISUAL_QA,
            PublicationBlockingDirective.VISUAL_QA_REVISION_REQUIRED,
            ("visual-finding-1",),
        ),
        PublicationStoppedSourceKind.LAYOUT_REVISION_STOP: (
            PublicationMaterialKind.RESUME,
            ApplicationPreparationStage.RESUME_PUBLICATION,
            ApplicationPreparationStage.RESUME_LAYOUT_REVISION,
            PublicationBlockingDirective.LAYOUT_REVISION_NOT_SUCCESSFUL,
            ("visual-finding-1",),
        ),
        PublicationStoppedSourceKind.COVER_LETTER_LAYOUT_OVERFLOW: (
            PublicationMaterialKind.COVER_LETTER,
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
            ApplicationPreparationStage.COVER_LETTER_PUBLICATION,
            PublicationBlockingDirective.COVER_LETTER_LAYOUT_OVERFLOW,
            (),
        ),
    }
    material, publication, source, directive, blockers = values[kind]
    return create_publication_stopped_source_lineage(
        subject_id="subject-1",
        application_plan_id="plan-1",
        publication_stage=publication,
        material_kind=material,
        source_kind=kind,
        source_stage=source,
        source_result_id=f"source-{kind.value.lower()}",
        source_outcome=PreparationStageOutcome.COMPLETED,
        source_contract_version="source-v1",
        source_result_content_hash=HASH_A,
        source_directive=directive,
        source_artifact_id="artifact-1",
        source_artifact_version="1",
        source_artifact_content_hash=HASH_B,
        blocking_lineage_ids=blockers,
    )


def test_all_current_source_lineage_kinds_round_trip_and_replay() -> None:
    lineages = tuple(_lineage(kind) for kind in PublicationStoppedSourceKind)

    assert {item.source_kind for item in lineages} == set(
        PublicationStoppedSourceKind
    )
    for item in lineages:
        assert PublicationStoppedSourceLineage.from_dict(
            item.to_dict()
        ) == item
        assert _lineage(item.source_kind) == item


def test_parent_source_stage_and_material_binding_fail_closed() -> None:
    visual = _lineage(PublicationStoppedSourceKind.VISUAL_QA_DIRECTIVE)

    with pytest.raises(ValueError, match="source stage"):
        replace(
            visual,
            source_stage=ApplicationPreparationStage.RESUME_FACT_QA,
        )
    with pytest.raises(ValueError, match="material kind"):
        replace(visual, material_kind=PublicationMaterialKind.COVER_LETTER)


def test_identity_tampering_and_incomplete_fact_qa_blockers_fail_closed() -> None:
    fact = _lineage(PublicationStoppedSourceKind.FACT_QA_BLOCKER)

    with pytest.raises(ValueError, match="hash"):
        replace(fact, lineage_content_hash=HASH_B)
    with pytest.raises(ValueError, match="blocker lineage"):
        create_publication_stopped_source_lineage(
            subject_id="subject-1",
            application_plan_id="plan-1",
            publication_stage=ApplicationPreparationStage.RESUME_PUBLICATION,
            material_kind=PublicationMaterialKind.RESUME,
            source_kind=PublicationStoppedSourceKind.FACT_QA_BLOCKER,
            source_stage=ApplicationPreparationStage.RESUME_FACT_QA,
            source_result_id="qa-1",
            source_outcome=PreparationStageOutcome.COMPLETED,
            source_contract_version="qa-v1",
            source_result_content_hash=HASH_A,
            source_directive=PublicationBlockingDirective.FACT_QA_BLOCKED,
        )


def test_public_projection_contains_only_bounded_typed_references() -> None:
    lineage = _lineage(
        PublicationStoppedSourceKind.COVER_LETTER_LAYOUT_OVERFLOW
    )
    outputs = lineage.output_references()

    assert {
        "publication_stopped_source_artifact_id",
        "publication_stopped_source_kind",
        "publication_stopped_source_lineage_id",
        "publication_stopped_source_result_id",
        "publication_stopped_source_stage",
    } <= set(outputs)
    serialized = str(outputs).lower()
    assert "/" not in serialized
    assert "stderr" not in serialized
    assert "credential" not in serialized
    assert "permit" not in serialized
