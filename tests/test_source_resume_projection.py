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
from pdfminer.pdfdocument import PDFPasswordIncorrect

import core.source_resume_projection as projection_module
from core.private_home import PrivateHome
from core.resume_candidates import (
    PrivateHomeResumeCandidateRepository,
    RegisterResumeCandidateCommand,
    ResumeCandidate,
    ResumeSummarySource,
    ResumeSummaryTrust,
    register_resume_candidate,
)
from core.source_resume_projection import (
    CreateSourceResumeProjectionCommand,
    DeterministicSourceResumeParser,
    PrivateHomeSourceResumeArtifactReader,
    PrivateHomeSourceResumeProjectionRepository,
    SourceResumeBlockKind,
    SourceResumeLocatorKind,
    SourceResumeProjection,
    SourceResumeProjectionFailureReason,
    SourceResumeProjectionReadStatus,
    SourceResumeProjectionStatus,
    SourceResumeProjectionWriteStatus,
    create_source_resume_projection,
)


NOW = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/>'
    "</Types>"
)


def _home(tmp_path: Path) -> PrivateHome:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    return home


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
    <w:p><w:r><w:t>Python and remote sensing.</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Education</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Synthetic University</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def _pdf_bytes(lines: tuple[str, ...]) -> bytes:
    stream_parts = ["BT", "/F1 12 Tf", "72 720 Td"]
    for index, line in enumerate(lines):
        if index:
            stream_parts.append("0 -18 Td")
        escaped = (
            line.replace("\\", "\\\\")
            .replace("(", "\\(")
            .replace(")", "\\)")
        )
        stream_parts.append(f"({escaped}) Tj")
    stream_parts.append("ET")
    stream = "\n".join(stream_parts).encode("ascii")
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    )
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{number} 0 obj\n".encode("ascii"))
        document.extend(value)
        document.extend(b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(document)


def _managed_source(
    home: PrivateHome,
    *,
    name: str,
    content: bytes,
) -> Path:
    path = home.paths.master_documents / name
    path.write_bytes(content)
    return path


def _register(
    home: PrivateHome,
    *,
    subject_id: str = "subject-a",
    name: str = "resume.docx",
    content: bytes | None = None,
) -> tuple[PrivateHomeResumeCandidateRepository, ResumeCandidate]:
    artifact = _managed_source(
        home,
        name=name,
        content=content if content is not None else _docx_bytes(),
    )
    repository = PrivateHomeResumeCandidateRepository(home)
    result = register_resume_candidate(
        RegisterResumeCandidateCommand(
            subject_id=subject_id,
            artifact_path=artifact,
            display_name="Synthetic Resume",
            selection_safe_summary="Verified synthetic resume summary.",
            summary_source=ResumeSummarySource.AUTHENTICATED_CALLER,
            summary_trust=ResumeSummaryTrust.USER_CONFIRMED,
            now=NOW,
        ),
        home=home,
        repository=repository,
    )
    assert result.candidate is not None
    return repository, result.candidate


def _project(
    home: PrivateHome,
    candidate_repository: PrivateHomeResumeCandidateRepository,
    candidate: ResumeCandidate,
    *,
    parser_version: str = "source-resume-parser-v1",
    now: datetime = NOW,
):
    return create_source_resume_projection(
        CreateSourceResumeProjectionCommand(
            subject_id=candidate.subject_id,
            resume_id=candidate.resume_id,
            now=now,
        ),
        candidate_repository=candidate_repository,
        artifact_reader=PrivateHomeSourceResumeArtifactReader(home),
        parser=DeterministicSourceResumeParser(parser_version),
        projection_repository=(
            PrivateHomeSourceResumeProjectionRepository(home)
        ),
    )


def _all_blocks(projection: SourceResumeProjection):
    return tuple(
        block
        for section in projection.sections
        for block in section.blocks
    )


def test_docx_projection_has_ordered_sections_bullets_and_locators(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidates, candidate = _register(home)

    result = _project(home, candidates, candidate)

    assert result.status is SourceResumeProjectionStatus.CREATED
    projection = result.projection
    assert projection is not None
    assert [section.title for section in projection.sections] == ["Experience"]
    blocks = _all_blocks(projection)
    assert [item.text for item in blocks] == [
        "Experience",
        "Built deterministic geospatial pipelines.",
        "Python and remote sensing.",
        "Education",
        "Synthetic University",
    ]
    assert blocks[1].kind is SourceResumeBlockKind.BULLET
    assert blocks[1].bullet_id is not None
    assert blocks[0].locator.kind is SourceResumeLocatorKind.DOCX_PARAGRAPH
    assert blocks[0].locator.paragraph_index == 0
    assert (
        blocks[3].locator.kind
        is SourceResumeLocatorKind.DOCX_TABLE_CELL_PARAGRAPH
    )
    assert (
        blocks[3].locator.table_index,
        blocks[3].locator.row_index,
        blocks[3].locator.cell_index,
        blocks[3].locator.cell_paragraph_index,
    ) == (0, 0, 0, 0)


def test_text_pdf_projection_has_page_line_locators_and_faithful_text(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    lines = (
        "EXPERIENCE",
        "- Built deterministic climate pipelines",
        "Python and geospatial systems",
    )
    candidates, candidate = _register(
        home,
        name="resume.pdf",
        content=_pdf_bytes(lines),
    )

    result = _project(home, candidates, candidate)

    assert result.status is SourceResumeProjectionStatus.CREATED
    assert result.projection is not None
    blocks = _all_blocks(result.projection)
    assert tuple(item.text for item in blocks) == lines
    assert blocks[0].kind is SourceResumeBlockKind.HEADING
    assert blocks[1].kind is SourceResumeBlockKind.BULLET
    assert tuple(
        (item.locator.page_number, item.locator.line_index)
        for item in blocks
    ) == ((1, 0), (1, 1), (1, 2))


def test_ids_and_hash_are_stable_across_replay_restart_and_mtime(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidates, candidate = _register(home)
    first = _project(home, candidates, candidate)
    artifact = home.contained_path(candidate.artifact_reference)
    artifact.touch()
    restarted_candidates = PrivateHomeResumeCandidateRepository(
        PrivateHome(home.root)
    )

    replay = _project(
        PrivateHome(home.root),
        restarted_candidates,
        candidate,
        now=NOW + timedelta(days=1),
    )

    assert replay.status is SourceResumeProjectionStatus.UNCHANGED
    assert replay.projection == first.projection
    assert replay.projection is not None
    assert replay.projection.projected_at == NOW
    assert [
        section.section_id for section in replay.projection.sections
    ] == [section.section_id for section in first.projection.sections]
    assert [
        block.block_id for block in _all_blocks(replay.projection)
    ] == [block.block_id for block in _all_blocks(first.projection)]


def test_artifact_hash_mismatch_fails_closed_without_projection(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidates, candidate = _register(home)
    artifact = home.contained_path(candidate.artifact_reference)
    artifact.write_bytes(_docx_bytes() + b"changed")

    result = _project(home, candidates, candidate)

    assert result.status is SourceResumeProjectionStatus.FAILED
    assert result.reason_code in {
        SourceResumeProjectionFailureReason.RESUME_INTEGRITY_FAILURE,
        SourceResumeProjectionFailureReason.ARTIFACT_HASH_MISMATCH,
    }
    assert not tuple(home.paths.source_resume_projections.rglob("*.json"))


def test_image_only_and_encrypted_pdf_are_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path)
    candidates, image_only = _register(
        home,
        name="image-only.pdf",
        content=_pdf_bytes(()),
    )
    image_result = _project(home, candidates, image_only)
    assert image_result.status is SourceResumeProjectionStatus.UNSUPPORTED
    assert (
        image_result.reason_code
        is SourceResumeProjectionFailureReason.FORMAT_UNSUPPORTED
    )

    candidates, encrypted = _register(
        home,
        name="encrypted.pdf",
        content=_pdf_bytes(("Encrypted placeholder",)),
    )

    def _encrypted_open(*_args, **_kwargs):
        raise PDFPasswordIncorrect

    monkeypatch.setattr(projection_module.pdfplumber, "open", _encrypted_open)
    encrypted_result = _project(home, candidates, encrypted)
    assert encrypted_result.status is SourceResumeProjectionStatus.UNSUPPORTED
    assert encrypted_result.projection is None


@pytest.mark.parametrize(
    "name",
    [
        "damaged.pdf",
        "damaged.docx",
    ],
)
def test_damaged_document_is_unreadable(
    tmp_path: Path,
    name: str,
) -> None:
    home = _home(tmp_path)
    if name.endswith(".docx"):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("word/document.xml", "<broken")
        content = output.getvalue()
    else:
        content = b"%PDF-1.7\nnot a complete PDF"
    candidates, candidate = _register(home, name=name, content=content)

    result = _project(home, candidates, candidate)

    assert result.status is SourceResumeProjectionStatus.UNREADABLE
    assert (
        result.reason_code
        is SourceResumeProjectionFailureReason.ARTIFACT_UNREADABLE
    )
    assert result.projection is None


def test_parser_version_or_artifact_change_creates_new_projection(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidates, first_candidate = _register(home)
    first = _project(home, candidates, first_candidate)
    parser_changed = _project(
        home,
        candidates,
        first_candidate,
        parser_version="source-resume-parser-v2",
    )
    _, artifact_changed_candidate = _register(
        home,
        name="changed.docx",
        content=_docx_bytes() + b"stable trailing package bytes",
    )
    artifact_changed = _project(
        home,
        candidates,
        artifact_changed_candidate,
    )

    assert first.status is SourceResumeProjectionStatus.CREATED
    assert parser_changed.status is SourceResumeProjectionStatus.CREATED
    assert artifact_changed.status is SourceResumeProjectionStatus.CREATED
    assert len(
        {
            first.projection.projection_id,
            parser_changed.projection.projection_id,
            artifact_changed.projection.projection_id,
        }
    ) == 3


def test_repository_corruption_and_immutable_conflict_fail_closed(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidates, candidate = _register(home)
    created = _project(home, candidates, candidate)
    projection = created.projection
    assert projection is not None
    repository = PrivateHomeSourceResumeProjectionRepository(home)
    record = next(
        home.paths.source_resume_projections.rglob(
            f"{projection.projection_id}.json"
        )
    )
    before = record.read_bytes()
    first_section = projection.sections[0]
    first_block = first_section.blocks[0]
    changed_block = replace(first_block, text="Conflicting source text")
    changed_section = replace(
        first_section,
        blocks=(changed_block, *first_section.blocks[1:]),
    )
    changed_sections = (changed_section, *projection.sections[1:])
    content_values = projection.content_dict()
    content_values["sections"] = [
        section.to_dict() for section in changed_sections
    ]
    conflicting = replace(
        projection,
        sections=changed_sections,
        projection_content_hash=hashlib.sha256(
            json.dumps(
                content_values,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
    )

    conflict = repository.save(conflicting)

    assert conflict.status is SourceResumeProjectionWriteStatus.FAILED
    assert (
        conflict.reason_code
        is SourceResumeProjectionFailureReason.PROJECTION_INTEGRITY_FAILURE
    )
    assert record.read_bytes() == before

    record.write_text("{broken", encoding="utf-8")
    corrupted = repository.get(
        subject_id="subject-a",
        projection_id=projection.projection_id,
    )
    assert (
        corrupted.status
        is SourceResumeProjectionReadStatus.INTEGRITY_FAILURE
    )


def test_subject_isolation_prevents_cross_subject_projection_read(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidates, candidate_a = _register(home, subject_id="subject-a")
    _, candidate_b = _register(
        home,
        subject_id="subject-b",
        name="resume-b.docx",
    )
    result_a = _project(home, candidates, candidate_a)
    result_b = _project(home, candidates, candidate_b)
    assert result_a.projection is not None
    assert result_b.projection is not None
    repository = PrivateHomeSourceResumeProjectionRepository(home)

    cross_read = repository.get(
        subject_id="subject-b",
        projection_id=result_a.projection.projection_id,
    )

    assert cross_read.status is SourceResumeProjectionReadStatus.NOT_FOUND
    assert (
        result_a.projection.projection_id
        != result_b.projection.projection_id
    )


def test_invalid_request_and_missing_candidate_fail_without_writes(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    candidates = PrivateHomeResumeCandidateRepository(home)
    projection_repository = PrivateHomeSourceResumeProjectionRepository(home)
    dependencies = {
        "candidate_repository": candidates,
        "artifact_reader": PrivateHomeSourceResumeArtifactReader(home),
        "parser": DeterministicSourceResumeParser(),
        "projection_repository": projection_repository,
    }

    naive = create_source_resume_projection(
        CreateSourceResumeProjectionCommand(
            subject_id="subject-a",
            resume_id="resume-candidate-" + "0" * 64,
            now=datetime(2026, 7, 28, 18, 0),
        ),
        **dependencies,
    )
    missing = create_source_resume_projection(
        CreateSourceResumeProjectionCommand(
            subject_id="subject-a",
            resume_id="resume-candidate-" + "0" * 64,
            now=NOW,
        ),
        **dependencies,
    )

    assert naive.reason_code is SourceResumeProjectionFailureReason.INVALID_REQUEST
    assert missing.reason_code is SourceResumeProjectionFailureReason.RESUME_NOT_FOUND
    assert not tuple(home.paths.source_resume_projections.rglob("*.json"))


def test_projection_module_has_no_agent_or_execution_dependencies() -> None:
    module_path = Path(projection_module.__file__)
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
        "core.job_prioritization",
        "core.priority_agent_adapter",
        "core.resume_selection",
        "adapters",
        "playwright",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
