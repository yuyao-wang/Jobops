"""Focused C1b deterministic Candidate Source Projection tests."""

from __future__ import annotations

import io
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

import core.candidate_source_projections as projections
from core.candidate_information_sources import (
    CandidateInformationSourceRegistrationStatus,
    PrivateHomeCandidateInformationSourceRepository,
    RegisterCandidateFileSourceCommand,
    RegisterCandidateURLSourceCommand,
    RegisterCandidateUserStatementSourceCommand,
    register_candidate_file_source,
    register_candidate_url_source,
    register_candidate_user_statement_source,
)
from core.candidate_source_projections import (
    CandidateProjectionAssetKind,
    CandidateProjectionBlockType,
    CandidateSourceProjectionCompleteness,
    CandidateSourceProjectionReadStatus,
    CandidateURLFetchResponse,
    CandidateURLFetchStatus,
    PrivateHomeCandidateSourceProjectionRepository,
    ProjectCandidateInformationSourceCommand,
    ProjectCandidateSourceStatus,
    get_candidate_source_projection,
    list_candidate_source_projections,
    project_candidate_information_source,
    read_candidate_projection_asset,
    read_candidate_projection_block,
    read_candidate_url_capture,
)
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
SUBJECT = "subject-c1b-synthetic"


def _pdf() -> bytes:
    stream = b"BT /F1 11 Tf 72 700 Td (SYNTHETIC PROFILE) Tj 0 -20 Td (Public text) Tj ET"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _image() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), color=(10, 20, 30)).save(output, "PNG")
    return output.getvalue()


def _office(kind: str) -> bytes:
    output = io.BytesIO()
    if kind == "docx":
        main_name = "word/document.xml"
        main_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
        main = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:p><w:pPr><w:pStyle w:val=\"Heading1\"/></w:pPr><w:r><w:t>Synthetic heading</w:t></w:r></w:p>"
            b"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Synthetic cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            b"</w:body></w:document>"
        )
    else:
        main_name = "ppt/presentation.xml"
        main_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
        main = b'<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'
    types = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        f'<Override PartName="/{main_name}" ContentType="{main_type}"/></Types>'
    ).encode()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        archive.writestr(main_name, main)
        if kind == "pptx":
            archive.writestr(
                "ppt/slides/slide1.xml",
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>Synthetic slide</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
            )
    return output.getvalue()


def _stores(tmp_path: Path):
    home = PrivateHome(tmp_path / "private")
    return (
        PrivateHomeCandidateInformationSourceRepository(home),
        PrivateHomeCandidateSourceProjectionRepository(home),
    )


def _project(source, invocation, source_store, projection_store, **kwargs):
    return project_candidate_information_source(
        ProjectCandidateInformationSourceCommand(
            subject_id=source.subject_id,
            source_id=source.source_id,
            source_version=source.source_version,
            source_identity_hash=source.source_identity_hash,
            invocation_id=invocation,
            now=NOW,
        ),
        source_repository=source_store,
        projection_repository=projection_store,
        **kwargs,
    )


def test_file_and_statement_projection_is_deterministic_and_path_free(tmp_path: Path) -> None:
    source_store, projection_store = _stores(tmp_path)
    fixtures = (_pdf(), _office("docx"), _office("pptx"), b"One\n\nTwo\n", _image())
    outputs = []
    for index, content in enumerate(fixtures):
        registered = register_candidate_file_source(
            RegisterCandidateFileSourceCommand(SUBJECT, f"register-{index}", NOW, content),
            repository=source_store,
        )
        assert registered.status is CandidateInformationSourceRegistrationStatus.CREATED
        result = _project(registered.source, f"project-{index}", source_store, projection_store)
        assert result.status is ProjectCandidateSourceStatus.CREATED
        assert result.projection is not None
        outputs.append(result.projection)
    statement = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT, "register-statement", NOW, b"First assertion.\r\n\r\nSecond assertion."
        ),
        repository=source_store,
    )
    statement_result = _project(statement.source, "project-statement", source_store, projection_store)
    assert statement_result.status is ProjectCandidateSourceStatus.CREATED
    assert len(statement_result.projection.block_ids) == 2
    replay = _project(fixtures and outputs[0] and register_candidate_file_source(
        RegisterCandidateFileSourceCommand(SUBJECT, "register-pdf-replay", NOW, _pdf()),
        repository=source_store,
    ).source, "project-pdf-replay", source_store, projection_store)
    assert replay.status is ProjectCandidateSourceStatus.UNCHANGED
    assert replay.projection.projection_id == outputs[0].projection_id
    assert outputs[2].completeness is CandidateSourceProjectionCompleteness.COMPLETED_WITH_LIMITS
    assert outputs[4].asset_ids
    assert str(tmp_path) not in repr(outputs)


class _FakeFetcher:
    def __init__(self, response: CandidateURLFetchResponse) -> None:
        self.response = response
        self.calls = 0

    def fetch(self, request):
        self.calls += 1
        return self.response


def test_url_capture_is_bounded_source_bound_and_ssrf_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_store, projection_store = _stores(tmp_path)
    registered = register_candidate_url_source(
        RegisterCandidateURLSourceCommand(
            SUBJECT, "register-url", NOW, "https://example.test/profile"
        ),
        repository=source_store,
    )
    fetcher = _FakeFetcher(
        CandidateURLFetchResponse(
            CandidateURLFetchStatus.SUCCEEDED,
            final_url="https://example.test/final",
            response_status=200,
            content_type="text/html",
            content=b"<html><title>Synthetic</title><script>secret()</script><h1>Heading</h1><p>Visible body</p></html>",
            redirect_chain=("https://example.test/profile",),
        )
    )
    result = _project(
        registered.source, "project-url", source_store, projection_store,
        url_fetcher=fetcher,
    )
    assert result.status is ProjectCandidateSourceStatus.CREATED
    assert result.projection.capture_id and result.projection.capture_hash
    capture = read_candidate_url_capture(
        SUBJECT,
        result.projection.projection_id,
        result.projection.capture_id,
        repository=projection_store,
    )
    assert capture.status is CandidateSourceProjectionReadStatus.FOUND
    assert capture.capture_payload.capture.capture_hash == result.projection.capture_hash
    texts = [
        read_candidate_projection_block(
            SUBJECT, result.projection.projection_id, block_id,
            repository=projection_store,
        ).block.text
        for block_id in result.projection.block_ids
    ]
    assert texts == ["Synthetic", "Heading", "Visible body"]
    assert fetcher.calls == 1
    monkeypatch.setattr(
        projections.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(projections.socket.AF_INET, projections.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    blocked = projections.PinnedHTTPSCandidateURLFetcher().fetch(
        projections.CandidateURLFetchRequest("https://example.test/")
    )
    assert blocked.status is CandidateURLFetchStatus.BLOCKED

    checked_hosts = []
    responses = [
        type("Response", (), {
            "status": 302,
            "getheader": lambda self, name, default=None: (
                "https://redirected.test/final" if name == "Location" else default
            ),
        })(),
        type("Response", (), {
            "status": 200,
            "getheader": lambda self, name, default=None: (
                "text/plain" if name == "Content-Type" else default
            ),
            "read": lambda self, limit: b"redirected body",
        })(),
    ]

    class Connection:
        def __init__(self, hostname, address, port, timeout):
            checked_hosts.append(hostname)

        def request(self, *args, **kwargs):
            return None

        def getresponse(self):
            return responses.pop(0)

        def close(self):
            return None

    monkeypatch.setattr(
        projections,
        "_public_addresses",
        lambda host, port: ("8.8.8.8",),
    )
    monkeypatch.setattr(projections, "_PinnedHTTPSConnection", Connection)
    redirected = projections.PinnedHTTPSCandidateURLFetcher().fetch(
        projections.CandidateURLFetchRequest("https://origin.test/start")
    )
    assert redirected.status is CandidateURLFetchStatus.SUCCEEDED
    assert checked_hosts == ["origin.test", "redirected.test"]

    for suffix, content, content_type in (
        ("pdf", _pdf(), "application/pdf"),
        ("image", _image(), "image/png"),
    ):
        url_source = register_candidate_url_source(
            RegisterCandidateURLSourceCommand(
                SUBJECT,
                f"register-url-{suffix}",
                NOW,
                f"https://example.test/{suffix}",
            ),
            repository=source_store,
        )
        projected = _project(
            url_source.source,
            f"project-url-{suffix}",
            source_store,
            projection_store,
            url_fetcher=_FakeFetcher(
                CandidateURLFetchResponse(
                    CandidateURLFetchStatus.SUCCEEDED,
                    final_url=f"https://example.test/{suffix}",
                    response_status=200,
                    content_type=content_type,
                    content=content,
                )
            ),
        )
        assert projected.status is ProjectCandidateSourceStatus.CREATED
        assert projected.projection.capture_id is not None

    too_large_source = register_candidate_url_source(
        RegisterCandidateURLSourceCommand(
            SUBJECT, "register-url-too-large", NOW,
            "https://example.test/too-large",
        ),
        repository=source_store,
    )
    too_large = _project(
        too_large_source.source,
        "project-url-too-large",
        source_store,
        projection_store,
        url_fetcher=_FakeFetcher(
            CandidateURLFetchResponse(
                CandidateURLFetchStatus.TOO_LARGE,
                failure_code="URL_TOO_LARGE",
            )
        ),
    )
    assert too_large.status is ProjectCandidateSourceStatus.UNSUPPORTED


def test_exact_lineage_subject_isolation_replay_and_payload_drift(tmp_path: Path) -> None:
    source_store, projection_store = _stores(tmp_path)
    registered = register_candidate_file_source(
        RegisterCandidateFileSourceCommand(SUBJECT, "register-image", NOW, _image()),
        repository=source_store,
    )
    result = _project(registered.source, "project-image", source_store, projection_store)
    projection = result.projection
    assert projection.source_id == registered.source.source_id
    assert projection.source_identity_hash == registered.source.source_identity_hash
    assert get_candidate_source_projection("other-subject", projection.projection_id, repository=projection_store).status is CandidateSourceProjectionReadStatus.NOT_FOUND
    conflict = project_candidate_information_source(
        ProjectCandidateInformationSourceCommand(
            SUBJECT, registered.source.source_id, registered.source.source_version,
            "0" * 64, "project-image", NOW
        ),
        source_repository=source_store,
        projection_repository=projection_store,
    )
    assert conflict.status is ProjectCandidateSourceStatus.INTEGRITY_FAILURE
    valid_asset = read_candidate_projection_asset(
        SUBJECT, projection.projection_id, projection.asset_ids[0],
        repository=projection_store,
    ).asset_payload.asset
    rollback_store = PrivateHomeCandidateSourceProjectionRepository(
        PrivateHome(tmp_path / "rollback-private")
    )
    rolled_back = rollback_store.save(
        projection=projection,
        blocks=(),
        assets=((valid_asset, b"wrong-bytes"),),
        capture=None,
        capture_content=None,
        request_hash="1" * 64,
    )
    assert rolled_back.status is ProjectCandidateSourceStatus.INTEGRITY_FAILURE
    with sqlite3.connect(rollback_store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM projections").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    with sqlite3.connect(projection_store.path) as connection:
        connection.execute(
            "UPDATE assets SET content_bytes=? WHERE asset_id=?",
            (b"drift", projection.asset_ids[0]),
        )
    drift = read_candidate_projection_asset(
        SUBJECT, projection.projection_id, projection.asset_ids[0],
        repository=projection_store,
    )
    assert drift.status is CandidateSourceProjectionReadStatus.INTEGRITY_FAILURE


def test_public_block_asset_boundary_has_no_fact_semantics_or_metadata_leaks(tmp_path: Path) -> None:
    source_store, projection_store = _stores(tmp_path)
    statement = register_candidate_user_statement_source(
        RegisterCandidateUserStatementSourceCommand(
            SUBJECT, "register-private-statement", NOW,
            b"Synthetic private statement that is not a verified fact.",
        ),
        repository=source_store,
    )
    statement_result = _project(statement.source, "project-private-statement", source_store, projection_store)
    block_result = read_candidate_projection_block(
        SUBJECT, statement_result.projection.projection_id,
        statement_result.projection.block_ids[0], repository=projection_store,
    )
    assert block_result.status is CandidateSourceProjectionReadStatus.FOUND
    assert block_result.block.block_type is CandidateProjectionBlockType.USER_STATEMENT
    assert block_result.block.source_locator.source_id == statement.source.source_id
    image = register_candidate_file_source(
        RegisterCandidateFileSourceCommand(SUBJECT, "register-private-image", NOW, _image()),
        repository=source_store,
    )
    image_result = _project(image.source, "project-private-image", source_store, projection_store)
    asset_result = read_candidate_projection_asset(
        SUBJECT, image_result.projection.projection_id,
        image_result.projection.asset_ids[0], repository=projection_store,
    )
    assert asset_result.status is CandidateSourceProjectionReadStatus.FOUND
    assert asset_result.asset_payload.asset.asset_kind is CandidateProjectionAssetKind.SOURCE_IMAGE
    listed = list_candidate_source_projections(SUBJECT, repository=projection_store)
    assert "Synthetic private statement" not in repr(listed)
    assert str(tmp_path) not in repr(listed)
    assert not hasattr(statement_result.projection, "verification_status")
    assert not hasattr(statement_result.projection, "candidate_field")
