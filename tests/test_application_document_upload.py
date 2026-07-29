"""Focused P2c2 tests for canonical document upload mapping."""

from __future__ import annotations

import asyncio
import ast
import hashlib
from pathlib import Path

from adapters.ashby import AshbyAdapter
import adapters.document_upload as upload_module
from adapters.document_upload import (
    ApplicationDocumentUploadFailure,
    ApplicationDocumentUploadPlanStatus,
    plan_application_document_uploads,
)
from adapters.greenhouse import GreenhouseAdapter
from adapters.protocol import (
    ApplicationContext,
    BaseATSAdapter,
    FieldIR,
    FieldKind,
    FormIR,
)
from adapters.shared import canonical_key_for
from core.application_answer_taxonomy import (
    CanonicalApplicationAnswerKey,
)
from core.bundles import ManagedArtifactReference, MaterialBundle
from core.private_home import PrivateHome


SUBJECT = "synthetic-upload-subject"


def _subject_key() -> str:
    return "subject-" + hashlib.sha256(SUBJECT.encode()).hexdigest()


def _field(
    key: CanonicalApplicationAnswerKey,
    *,
    control_id: str,
    required: bool = False,
) -> FieldIR:
    return FieldIR(
        canonical_key=key,
        label=key.value.replace("_", " ").title(),
        selectors=(f"#{control_id}",),
        kind=FieldKind.FILE,
        required=required,
        element_id=control_id,
    )


def _form(*fields: FieldIR) -> FormIR:
    return FormIR(
        adapter="synthetic",
        url="https://jobs.example.test/apply",
        fields=tuple(fields),
        submit_selectors=(),
        confirmation_selectors=(),
    )


def _materials(
    tmp_path: Path, *, cover: bool = True
) -> tuple[PrivateHome, MaterialBundle]:
    home = PrivateHome(tmp_path / "private")
    home.ensure()
    resume = (
        home.paths.compiled_resumes / _subject_key() / "resume.pdf"
    )
    resume.parent.mkdir(parents=True, exist_ok=True)
    resume.write_bytes(b"%PDF-1.4\nsynthetic resume")
    cover_reference = (
        "state/preparation/compiled-cover-letters/"
        f"{_subject_key()}/cover-letter.pdf"
    )
    cover_path = home.contained_path(cover_reference)
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    cover_bytes = b"%PDF-1.4\nsynthetic cover letter"
    cover_path.write_bytes(cover_bytes)
    reference = (
        ManagedArtifactReference(
            reference=cover_reference,
            sha256=hashlib.sha256(cover_bytes).hexdigest(),
            byte_size=len(cover_bytes),
            media_type="application/pdf",
        )
        if cover
        else None
    )
    return home, MaterialBundle.build(
        resume_path=resume, cover_letter_pdf=reference
    )


class _Locator:
    def __init__(self) -> None:
        self.uploaded: Path | None = None

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return 1

    async def set_input_files(self, path: str) -> None:
        self.uploaded = Path(path)

    async def evaluate(self, _script: str):
        return self.uploaded.name if self.uploaded else ""


class _Page:
    def __init__(self, *selectors: str) -> None:
        self.controls = {
            selector: _Locator() for selector in selectors
        }

    def locator(self, selector: str) -> _Locator:
        return self.controls[selector]


class _Adapter(BaseATSAdapter):
    name = "synthetic"


def test_legacy_resume_only_fill_behavior_is_unchanged(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "legacy-resume.pdf"
    resume.write_bytes(b"%PDF-1.4\nlegacy")
    page = _Page("#resume")
    form = _form(
        _field(
            CanonicalApplicationAnswerKey.RESUME,
            control_id="resume",
            required=True,
        )
    )

    report = asyncio.run(
        _Adapter().fill(
            page,
            ApplicationContext(
                page=page,
                job_url="https://jobs.example.test/apply",
                job_id="job-one",
                run_id="run-one",
                profile={},
                resume_path=resume,
            ),
            form,
        )
    )

    assert page.controls["#resume"].uploaded == resume
    assert report.uploaded_files == ("resume",)
    assert report.document_upload_failure is None


def test_resume_and_cover_controls_receive_only_their_own_pdf(
    tmp_path: Path,
) -> None:
    home, materials = _materials(tmp_path)
    form = _form(
        _field(CanonicalApplicationAnswerKey.RESUME, control_id="resume"),
        _field(
            CanonicalApplicationAnswerKey.COVER_LETTER_FILE,
            control_id="cover",
        ),
    )

    result = plan_application_document_uploads(
        form=form, materials=materials, private_home=home
    )

    assert result.status is ApplicationDocumentUploadPlanStatus.READY
    items = {
        item.canonical_material_key: item for item in result.plan.items
    }
    assert items[
        CanonicalApplicationAnswerKey.RESUME
    ].artifact_sha256 == materials.resume_sha256
    assert items[
        CanonicalApplicationAnswerKey.COVER_LETTER_FILE
    ].artifact_sha256 == materials.cover_letter_pdf.sha256
    assert items[CanonicalApplicationAnswerKey.RESUME].resolved_path != (
        items[
            CanonicalApplicationAnswerKey.COVER_LETTER_FILE
        ].resolved_path
    )


def test_absent_cover_letter_control_is_normal(tmp_path: Path) -> None:
    home, materials = _materials(tmp_path, cover=False)

    result = plan_application_document_uploads(
        form=_form(
            _field(
                CanonicalApplicationAnswerKey.RESUME,
                control_id="resume",
            )
        ),
        materials=materials,
        private_home=home,
    )

    assert result.status is ApplicationDocumentUploadPlanStatus.READY
    assert tuple(
        item.canonical_material_key for item in result.plan.items
    ) == (CanonicalApplicationAnswerKey.RESUME,)


def test_required_cover_letter_without_pdf_is_typed_failure(
    tmp_path: Path,
) -> None:
    home, materials = _materials(tmp_path, cover=False)

    result = plan_application_document_uploads(
        form=_form(
            _field(
                CanonicalApplicationAnswerKey.COVER_LETTER_FILE,
                control_id="cover",
                required=True,
            )
        ),
        materials=materials,
        private_home=home,
    )

    assert result.status is ApplicationDocumentUploadPlanStatus.FAILED
    assert result.failure is ApplicationDocumentUploadFailure.MATERIAL_MISSING


def test_optional_cover_letter_without_pdf_is_safely_skipped(
    tmp_path: Path,
) -> None:
    home, materials = _materials(tmp_path, cover=False)

    result = plan_application_document_uploads(
        form=_form(
            _field(
                CanonicalApplicationAnswerKey.COVER_LETTER_FILE,
                control_id="cover",
            )
        ),
        materials=materials,
        private_home=home,
    )

    assert result.status is ApplicationDocumentUploadPlanStatus.READY
    assert result.plan.items == ()
    assert result.plan.skipped_control_ids == ("cover",)


def test_artifact_anomalies_fail_closed(tmp_path: Path) -> None:
    for anomaly in ("hash", "size", "signature", "symlink", "containment"):
        home, materials = _materials(tmp_path / anomaly)
        cover = materials.cover_letter_pdf
        if anomaly == "hash":
            cover = ManagedArtifactReference(
                reference=cover.reference,
                sha256="0" * 64,
                byte_size=cover.byte_size,
                media_type="application/pdf",
            )
        elif anomaly == "size":
            cover = ManagedArtifactReference(
                reference=cover.reference,
                sha256=cover.sha256,
                byte_size=cover.byte_size + 1,
                media_type="application/pdf",
            )
        elif anomaly == "signature":
            path = home.contained_path(cover.reference)
            changed = b"not a PDF"
            path.write_bytes(changed)
            cover = ManagedArtifactReference(
                reference=cover.reference,
                sha256=hashlib.sha256(changed).hexdigest(),
                byte_size=len(changed),
                media_type="application/pdf",
            )
        elif anomaly == "symlink":
            path = home.contained_path(cover.reference)
            target = path.with_name("target.pdf")
            path.rename(target)
            path.symlink_to(target)
        else:
            outside = tmp_path / "outside-resume.pdf"
            outside.write_bytes(b"%PDF-1.4\noutside")
            materials = MaterialBundle.build(resume_path=outside)
        if anomaly != "containment":
            materials = MaterialBundle.build(
                resume_path=materials.resume_path,
                cover_letter_pdf=cover,
            )
            form = _form(
                _field(
                    CanonicalApplicationAnswerKey.COVER_LETTER_FILE,
                    control_id="cover",
                )
            )
        else:
            form = _form(
                _field(
                    CanonicalApplicationAnswerKey.RESUME,
                    control_id="resume",
                )
            )

        result = plan_application_document_uploads(
            form=form, materials=materials, private_home=home
        )

        assert result.failure is (
            ApplicationDocumentUploadFailure.ARTIFACT_INTEGRITY_FAILURE
        )

    home, materials = _materials(tmp_path / "base-fill-failure")
    cover = materials.cover_letter_pdf
    materials = MaterialBundle.build(
        resume_path=materials.resume_path,
        cover_letter_pdf=ManagedArtifactReference(
            reference=cover.reference,
            sha256="0" * 64,
            byte_size=cover.byte_size,
            media_type="application/pdf",
        ),
    )
    page = _Page("#cover")
    form = _form(
        _field(
            CanonicalApplicationAnswerKey.COVER_LETTER_FILE,
            control_id="cover",
        )
    )
    adapter = _Adapter()
    context = ApplicationContext(
        page=page,
        job_url="https://jobs.example.test/apply",
        job_id="job-one",
        run_id="run-one",
        profile={},
        materials=materials,
        private_home=home,
    )
    fill = asyncio.run(adapter.fill(page, context, form))
    validation = asyncio.run(adapter.validate(page, form, fill))
    assert fill.document_upload_failure is (
        ApplicationDocumentUploadFailure.ARTIFACT_INTEGRITY_FAILURE
    )
    assert page.controls["#cover"].uploaded is None
    assert validation.errors == (
        "document_upload:ARTIFACT_INTEGRITY_FAILURE",
    )


def test_unknown_and_ambiguous_controls_are_never_guessed(
    tmp_path: Path,
) -> None:
    home, materials = _materials(tmp_path)
    unknown = plan_application_document_uploads(
        form=_form(
            _field(
                CanonicalApplicationAnswerKey.UNKNOWN,
                control_id="mystery",
                required=True,
            )
        ),
        materials=materials,
        private_home=home,
    )
    ambiguous = plan_application_document_uploads(
        form=_form(
            _field(
                CanonicalApplicationAnswerKey.RESUME,
                control_id="resume-one",
                required=True,
            ),
            _field(
                CanonicalApplicationAnswerKey.RESUME,
                control_id="resume-two",
                required=True,
            ),
        ),
        materials=materials,
        private_home=home,
    )

    assert unknown.failure is (
        ApplicationDocumentUploadFailure.UNSUPPORTED_FILE_CONTROL
    )
    assert ambiguous.failure is (
        ApplicationDocumentUploadFailure.AMBIGUOUS_ROLE
    )


def test_base_adapter_uploads_both_planned_documents_once(
    tmp_path: Path,
) -> None:
    home, materials = _materials(tmp_path)
    page = _Page("#resume", "#cover")
    form = _form(
        _field(
            CanonicalApplicationAnswerKey.RESUME,
            control_id="resume",
            required=True,
        ),
        _field(
            CanonicalApplicationAnswerKey.COVER_LETTER_FILE,
            control_id="cover",
            required=True,
        ),
    )

    report = asyncio.run(
        _Adapter().fill(
            page,
            ApplicationContext(
                page=page,
                job_url="https://jobs.example.test/apply",
                job_id="job-one",
                run_id="run-one",
                profile={},
                materials=materials,
                private_home=home,
            ),
            form,
        )
    )

    assert report.uploaded_files == ("resume", "cover_letter_file")
    assert page.controls["#resume"].uploaded == materials.resume_path
    assert page.controls["#cover"].uploaded == home.contained_path(
        materials.cover_letter_pdf.reference
    )
    assert report.document_upload_failure is None


def test_shared_taxonomy_mapping_and_representative_adapters() -> None:
    assert canonical_key_for(
        "Cover letter", "cover_letter", "file"
    ) is CanonicalApplicationAnswerKey.COVER_LETTER_FILE
    assert canonical_key_for(
        "Supporting document", "attachment", "file"
    ) is CanonicalApplicationAnswerKey.UNKNOWN
    assert issubclass(GreenhouseAdapter, BaseATSAdapter)
    assert issubclass(AshbyAdapter, BaseATSAdapter)
    assert any(
        spec.canonical_key is CanonicalApplicationAnswerKey.RESUME
        for spec in GreenhouseAdapter.field_specs
    )
    assert any(
        spec.canonical_key is CanonicalApplicationAnswerKey.RESUME
        for spec in AshbyAdapter.field_specs
    )
    tree = ast.parse(
        Path(upload_module.__file__).read_text(encoding="utf-8")
    )
    imports = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        marker in module
        for module in imports
        for marker in (
            "semantic_mapper",
            "browser",
            "permits",
            "application_engine",
            "agent",
        )
    )
