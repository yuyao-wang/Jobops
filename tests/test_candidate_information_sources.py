"""Focused C1a Candidate Information Source Registry tests."""

from __future__ import annotations

import inspect
import io
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

import core.candidate_information_sources as sources
from core.candidate_identity_facts import CandidateIdentityFactSourceKind
from core.candidate_information_sources import (
    CandidateFileDetectedFormat,
    CandidateInformationSourceKind,
    CandidateInformationSourcePayloadReadStatus,
    CandidateInformationSourceReadStatus,
    CandidateInformationSourceRegistrationStatus,
    GetCandidateInformationSourceCommand,
    PrivateHomeCandidateInformationSourceRepository,
    RegisterCandidateFileSourceCommand,
    RegisterCandidateURLSourceCommand,
    RegisterCandidateUserStatementSourceCommand,
    get_candidate_information_source,
    list_candidate_information_sources,
    read_candidate_information_source_payload,
    register_candidate_file_source,
    register_candidate_url_source,
    register_candidate_user_statement_source,
)
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
SUBJECT = "subject-synthetic"


def _pdf() -> bytes:
    stream = b"BT /F1 11 Tf 72 700 Td (Synthetic candidate source) Tj ET"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    )
    output = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref = len(output)
    output += b"xref\n0 6\n0000000000 65535 f \n"
    output += b"".join(
        f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets
    )
    output += (
        f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("ascii")
    return bytes(output)


def _office(kind: str, *, active: bool = False) -> bytes:
    output = io.BytesIO()
    if kind == "docx":
        main_name = "word/document.xml"
        main_type = (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document.main+xml"
        )
        main = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
            b'wordprocessingml/2006/main"><w:body/></w:document>'
        )
    else:
        main_name = "ppt/presentation.xml"
        main_type = (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation.main+xml"
        )
        main = (
            b'<p:presentation xmlns:p="http://schemas.openxmlformats.org/'
            b'presentationml/2006/main"/>'
        )
    content_types = (
        '<?xml version="1.0"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/{main_name}" ContentType="{main_type}"/>'
        "</Types>"
    ).encode()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr(
            "_rels/.rels",
            (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/'
                b'package/2006/relationships"/>'
            ),
        )
        archive.writestr(main_name, main)
        if active:
            archive.writestr(
                "word/vbaProject.bin" if kind == "docx" else "ppt/vbaProject.bin",
                b"synthetic-active-payload",
            )
    return output.getvalue()


def _image(format_name: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 3), color=(12, 34, 56)).save(
        output, format=format_name
    )
    return output.getvalue()


def _repository(tmp_path: Path) -> PrivateHomeCandidateInformationSourceRepository:
    return PrivateHomeCandidateInformationSourceRepository(
        PrivateHome(tmp_path / "private")
    )


def test_file_sources_detect_actual_bytes_and_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    fixtures = (
        ("pdf", _pdf(), CandidateFileDetectedFormat.PDF),
        ("docx", _office("docx"), CandidateFileDetectedFormat.DOCX),
        ("pptx", _office("pptx"), CandidateFileDetectedFormat.PPTX),
        ("png", _image("PNG"), CandidateFileDetectedFormat.PNG),
        ("jpeg", _image("JPEG"), CandidateFileDetectedFormat.JPEG),
        (
            "text",
            b"Synthetic candidate notes\r\nsecond line\n",
            CandidateFileDetectedFormat.UTF8_TEXT,
        ),
    )
    created = []
    for index, (name, content, expected_format) in enumerate(fixtures):
        result = register_candidate_file_source(
            RegisterCandidateFileSourceCommand(
                subject_id=SUBJECT,
                invocation_id=f"invocation-file-{index}",
                now=NOW,
                content=content,
                display_name=f"synthetic-{name}.display",
            ),
            repository=repository,
        )
        assert result.status is CandidateInformationSourceRegistrationStatus.CREATED
        assert result.source is not None
        assert result.source.source_kind is CandidateInformationSourceKind.FILE
        assert result.source.source_descriptor.detected_format is expected_format
        assert (
            result.source.source_descriptor.byte_size
            == len(
                read_candidate_information_source_payload(
                    GetCandidateInformationSourceCommand(
                        SUBJECT, result.source.source_id
                    ),
                    repository=repository,
                ).payload.file_bytes
            )
        )
        created.append(result.source)

    replay = register_candidate_file_source(
        RegisterCandidateFileSourceCommand(
            subject_id=SUBJECT,
            invocation_id="invocation-file-replay",
            now=NOW,
            content=_pdf(),
            display_name="different-display-name.pdf",
        ),
        repository=repository,
    )
    assert replay.status is CandidateInformationSourceRegistrationStatus.UNCHANGED
    assert replay.source == created[0]
    monkeypatch.setattr(
        repository,
        "_payload_bytes_tx",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("metadata list loaded payload bytes")
        ),
    )
    listed = list_candidate_information_sources(SUBJECT, repository=repository)
    assert len(listed.sources) == 6
    assert "Candidate notes" not in repr(listed)
    assert str(tmp_path) not in repr(listed)
    assert all(
        "payload_bytes" not in source.to_dict() for source in listed.sources
    )


def test_url_and_statement_are_canonical_bounded_and_network_free(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    url = register_candidate_url_source(
        RegisterCandidateURLSourceCommand(
            subject_id=SUBJECT,
            invocation_id="invocation-url",
            now=NOW,
            url="https://EXAMPLE.test:443/path?q=meaningful#fragment",
        ),
        repository=repository,
    )
    statement = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            subject_id=SUBJECT,
            invocation_id="invocation-statement",
            now=NOW,
            statement_utf8="Résumé detail\r\nkept here.".encode("utf-8"),
        ),
        repository=repository,
    )
    assert url.status is CandidateInformationSourceRegistrationStatus.CREATED
    assert statement.status is CandidateInformationSourceRegistrationStatus.CREATED
    assert url.source is not None and statement.source is not None
    url_payload = repository.read_payload(
        GetCandidateInformationSourceCommand(SUBJECT, url.source.source_id)
    )
    statement_payload = repository.read_payload(
        GetCandidateInformationSourceCommand(
            SUBJECT, statement.source.source_id
        )
    )
    assert url_payload.payload.canonical_url == "https://example.test/path?q=meaningful"
    assert statement_payload.payload.statement_text == "Résumé detail\nkept here."
    url_replay = register_candidate_url_source(
        RegisterCandidateURLSourceCommand(
            SUBJECT,
            "invocation-url-replay",
            NOW,
            "https://example.test/path?q=meaningful",
            display_name="Different URL label",
        ),
        repository=repository,
    )
    statement_replay = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT,
            "invocation-statement-replay",
            NOW,
            "Résumé detail\nkept here.".encode("utf-8"),
        ),
        repository=repository,
    )
    assert (
        url_replay.status
        is CandidateInformationSourceRegistrationStatus.UNCHANGED
    )
    assert (
        statement_replay.status
        is CandidateInformationSourceRegistrationStatus.UNCHANGED
    )
    assert register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT,
            "invocation-statement-invalid",
            NOW,
            b"\xff",
        ),
        repository=repository,
    ).status is CandidateInformationSourceRegistrationStatus.INVALID
    assert register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT,
            "invocation-statement-large",
            NOW,
            b"x" * (sources.MAX_CANDIDATE_STATEMENT_BYTES + 1),
        ),
        repository=repository,
    ).status is CandidateInformationSourceRegistrationStatus.TOO_LARGE
    assert "Résumé detail" not in repr(statement)
    assert "Résumé detail" not in repr(
        repository.list_for_subject(SUBJECT)
    )
    assert register_candidate_url_source(
        RegisterCandidateURLSourceCommand(
            SUBJECT,
            "invocation-url-file",
            NOW,
            "file:///private/source",
        ),
        repository=repository,
    ).status is CandidateInformationSourceRegistrationStatus.INVALID
    for invalid in (
        "https://user:password@example.test/",
        "https://localhost/profile",
        "https://127.0.0.1/profile",
        "https://example.test/profile?access_token=secret",
    ):
        result = register_candidate_url_source(
            RegisterCandidateURLSourceCommand(
                SUBJECT,
                f"invocation-url-invalid-{len(invalid)}-{invalid[:1]}",
                NOW,
                invalid,
            ),
            repository=repository,
        )
        assert result.status is CandidateInformationSourceRegistrationStatus.INVALID
    implementation = inspect.getsource(sources._canonicalize_url)
    assert "httpx" not in implementation
    assert "urlopen" not in implementation


def test_subject_replay_reads_and_fact_source_projection(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT,
            "invocation-source-one",
            NOW,
            b"Synthetic statement.",
        ),
        repository=repository,
    )
    other = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            "subject-other",
            "invocation-source-other",
            NOW,
            b"Synthetic statement.",
        ),
        repository=repository,
    )
    assert first.source is not None and other.source is not None
    assert first.source.source_id != other.source.source_id
    mismatch = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT,
            "invocation-source-one",
            NOW,
            b"Different statement.",
        ),
        repository=repository,
    )
    assert (
        mismatch.status
        is CandidateInformationSourceRegistrationStatus.INTEGRITY_FAILURE
    )
    cross = GetCandidateInformationSourceCommand(
        "subject-other", first.source.source_id
    )
    assert (
        get_candidate_information_source(cross, repository=repository).status
        is CandidateInformationSourceReadStatus.NOT_FOUND
    )
    assert (
        read_candidate_information_source_payload(
            cross, repository=repository
        ).status
        is CandidateInformationSourcePayloadReadStatus.NOT_FOUND
    )
    source_ref = first.source.to_candidate_identity_fact_source_ref()
    assert source_ref.source_kind is CandidateIdentityFactSourceKind.USER_STATEMENT
    assert source_ref.source_id == first.source.source_id
    assert source_ref.source_version == first.source.source_version
    assert (
        source_ref.source_version
        == sources.CANDIDATE_INFORMATION_SOURCE_CONTRACT_VERSION
    )
    assert source_ref.source_hash == first.source.source_identity_hash
    assert source_ref.source_subject_id == SUBJECT
    assert source_ref.source_locator == "source:root"


def test_fail_closed_formats_atomic_rollback_and_payload_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    path_target = tmp_path / "not-accepted.bin"
    path_target.write_bytes(b"synthetic")
    symlink = tmp_path / "not-accepted-link"
    symlink.symlink_to(path_target)
    assert register_candidate_file_source(
        RegisterCandidateFileSourceCommand(
            SUBJECT,
            "invocation-path",
            NOW,
            symlink,
        ),
        repository=repository,
    ).status is CandidateInformationSourceRegistrationStatus.INVALID
    generic_zip = io.BytesIO()
    with zipfile.ZipFile(generic_zip, "w") as archive:
        archive.writestr("payload.txt", "not Office")
    for invocation, content, expected in (
        (
            "invocation-generic-zip",
            generic_zip.getvalue(),
            CandidateInformationSourceRegistrationStatus.UNSUPPORTED,
        ),
        (
            "invocation-active-office",
            _office("docx", active=True),
            CandidateInformationSourceRegistrationStatus.UNSUPPORTED,
        ),
        (
            "invocation-invalid-image",
            b"\x89PNG\r\n\x1a\nnot-an-image",
            CandidateInformationSourceRegistrationStatus.INVALID,
        ),
        (
            "invocation-binary",
            b"\x00\x01\x02\xff",
            CandidateInformationSourceRegistrationStatus.UNSUPPORTED,
        ),
    ):
        result = register_candidate_file_source(
            RegisterCandidateFileSourceCommand(
                SUBJECT, invocation, NOW, content
            ),
            repository=repository,
        )
        assert result.status is expected
    monkeypatch.setattr(sources, "MAX_CANDIDATE_FILE_BYTES", 8)
    assert register_candidate_file_source(
        RegisterCandidateFileSourceCommand(
            SUBJECT,
            "invocation-oversized",
            NOW,
            b"more than eight bytes",
        ),
        repository=repository,
    ).status is CandidateInformationSourceRegistrationStatus.TOO_LARGE
    monkeypatch.setattr(sources, "MAX_CANDIDATE_FILE_BYTES", 25 * 1024 * 1024)

    def fail_source_insert(*args, **kwargs):
        raise OSError("synthetic metadata failure")

    monkeypatch.setattr(repository, "_insert_source", fail_source_insert)
    failed = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT,
            "invocation-atomic-failure",
            NOW,
            b"Atomic synthetic statement.",
        ),
        repository=repository,
    )
    assert failed.status is CandidateInformationSourceRegistrationStatus.FAILED
    with sqlite3.connect(repository.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM payloads").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    monkeypatch.undo()

    stored = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT,
            "invocation-drift",
            NOW,
            b"Payload drift sentinel.",
        ),
        repository=repository,
    )
    assert stored.source is not None
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            UPDATE payloads SET payload_bytes = ?
            WHERE subject_id = ? AND payload_hash = ?
            """,
            (b"tampered", SUBJECT, stored.source.source_payload_hash),
        )
    drift = repository.read_payload(
        GetCandidateInformationSourceCommand(
            SUBJECT, stored.source.source_id
        )
    )
    assert (
        drift.status
        is CandidateInformationSourcePayloadReadStatus.INTEGRITY_FAILURE
    )
    rendered = repr(drift)
    assert "Payload drift sentinel" not in rendered
    assert str(tmp_path) not in rendered
