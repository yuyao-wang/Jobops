from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.resume_latex_versions as latex_module
from core.private_home import PrivateHome
from core.resume_latex_versions import (
    MAX_LATEX_SOURCE_BYTES,
    PrivateHomeResumeLatexVersionRepository,
    RegisterResumeLatexVersionCommand,
    RegisterResumeLatexVersionStatus,
    ResumeLatexCapability,
    ResumeLatexSourceKind,
    ResumeLatexVersionFailureReason,
    ResumeLatexVersionListStatus,
    ResumeLatexVersionReadStatus,
    ResumeLatexVersionWriteStatus,
    register_resume_latex_version,
)


NOW = datetime(2026, 7, 28, 23, 0, tzinfo=timezone.utc)
SOURCE = r"""\documentclass[11pt]{article}
\usepackage{geometry}
\begin{document}
\section*{Experience}
\begin{itemize}
  \item Built deterministic geospatial pipelines.
\end{itemize}
\end{document}
"""


def _home(tmp_path: Path) -> PrivateHome:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    return home


def _register(
    home: PrivateHome,
    *,
    subject_id: str = "subject-a",
    latex_source: str | None = SOURCE,
    source_path: Path | None = None,
    source_kind: ResumeLatexSourceKind = ResumeLatexSourceKind.USER_PROVIDED,
    parent_version_id: str | None = None,
    root_family_id: str | None = None,
    labels: tuple[str, ...] = (),
    now: datetime = NOW,
    repository=None,
    **bindings,
):
    return register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id=subject_id,
            source_kind=source_kind,
            now=now,
            latex_source=latex_source,
            source_path=source_path,
            parent_version_id=parent_version_id,
            root_family_id=root_family_id,
            labels=labels,
            **bindings,
        ),
        home=home,
        repository=repository
        or PrivateHomeResumeLatexVersionRepository(home),
    )


def _sources(home: PrivateHome) -> tuple[Path, ...]:
    return tuple(home.paths.resume_latex_version_sources.rglob("*.tex"))


def _records(home: PrivateHome) -> tuple[Path, ...]:
    return tuple(home.paths.resume_latex_version_records.rglob("*.json"))


def test_explicit_source_registers_a_typed_subject_scoped_version(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    result = _register(home, labels=("base", "geospatial"))

    assert result.status is RegisterResumeLatexVersionStatus.CREATED
    version = result.version
    assert version is not None
    assert version.subject_id == "subject-a"
    assert version.source_kind is ResumeLatexSourceKind.USER_PROVIDED
    assert version.parent_version_id is None
    assert version.root_family_id.startswith("resume-latex-family-")
    assert version.latex_version_id.startswith("resume-latex-version-")
    assert version.labels == ("base", "geospatial")
    assert version.created_at == NOW
    assert version.template_id is None
    assert version.tailored_resume_draft_id is None
    assert version.fact_qa_result_id is None


def test_hash_comes_from_the_actual_managed_bytes(tmp_path: Path) -> None:
    import hashlib

    home = _home(tmp_path)

    result = _register(home)

    version = result.version
    managed = home.contained_path(version.source_reference)
    assert managed.is_file()
    assert (
        hashlib.sha256(managed.read_bytes()).hexdigest()
        == version.source_sha256
    )
    assert managed.read_text(encoding="utf-8") == SOURCE
    assert managed.name == f"{version.source_sha256}.tex"


def test_source_survives_deleting_the_original_input_file(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    original = home.paths.master_documents / "resume.tex"
    original.write_text(SOURCE, encoding="utf-8")

    result = _register(home, latex_source=None, source_path=original)
    original.unlink()

    version = result.version
    assert result.status is RegisterResumeLatexVersionStatus.CREATED
    assert not original.exists()
    read = PrivateHomeResumeLatexVersionRepository(home).get(
        subject_id="subject-a",
        latex_version_id=version.latex_version_id,
    )
    assert read.status is ResumeLatexVersionReadStatus.FOUND
    assert (
        home.contained_path(version.source_reference).read_text(
            encoding="utf-8"
        )
        == SOURCE
    )


def test_multiple_versions_and_families_coexist(tmp_path: Path) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)

    first = _register(home, repository=repository)
    second = _register(
        home,
        repository=repository,
        latex_source=SOURCE.replace("Experience", "Selected Experience"),
        source_kind=ResumeLatexSourceKind.IMPORTED_EXISTING,
    )

    listed = repository.list_selectable("subject-a")
    assert listed.status is ResumeLatexVersionListStatus.SUCCEEDED
    assert len(listed.versions) == 2
    assert (
        first.version.root_family_id != second.version.root_family_id
    )
    assert {item.latex_version_id for item in listed.versions} == {
        first.version.latex_version_id,
        second.version.latex_version_id,
    }


def test_child_inherits_family_and_records_parent_lineage(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    parent = _register(home, repository=repository).version

    child = _register(
        home,
        repository=repository,
        latex_source=SOURCE.replace("geospatial", "geospatial imaging"),
        source_kind=ResumeLatexSourceKind.AI_REVISED,
        parent_version_id=parent.latex_version_id,
        now=NOW + timedelta(minutes=1),
    ).version
    grandchild = _register(
        home,
        repository=repository,
        latex_source=SOURCE.replace("Built", "Engineered"),
        source_kind=ResumeLatexSourceKind.AI_REVISED,
        parent_version_id=child.latex_version_id,
        now=NOW + timedelta(minutes=2),
    ).version

    assert child.parent_version_id == parent.latex_version_id
    assert child.root_family_id == parent.root_family_id
    assert grandchild.parent_version_id == child.latex_version_id
    assert grandchild.root_family_id == parent.root_family_id
    assert (
        len({v.latex_version_id for v in (parent, child, grandchild)}) == 3
    )
    stored = repository.get(
        subject_id="subject-a", latex_version_id=parent.latex_version_id
    )
    assert stored.version == parent


@pytest.mark.parametrize(
    ("parent", "family", "reason"),
    [
        (
            "resume-latex-version-" + "0" * 64,
            None,
            ResumeLatexVersionFailureReason.PARENT_NOT_FOUND,
        ),
        (
            None,
            "not-a-family-id",
            ResumeLatexVersionFailureReason.INVALID_REQUEST,
        ),
    ],
)
def test_unknown_parent_or_malformed_family_fails_closed(
    tmp_path: Path, parent, family, reason
) -> None:
    home = _home(tmp_path)

    result = _register(home, parent_version_id=parent, root_family_id=family)

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert result.reason_code is reason
    assert not _records(home)


def test_root_family_conflict_with_parent_fails_closed(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    parent = _register(home, repository=repository).version
    other = _register(
        home,
        repository=repository,
        latex_source=SOURCE.replace("Experience", "Projects"),
    ).version

    result = _register(
        home,
        repository=repository,
        latex_source=SOURCE.replace("geospatial", "raster"),
        parent_version_id=parent.latex_version_id,
        root_family_id=other.root_family_id,
    )

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLatexVersionFailureReason.ROOT_FAMILY_CONFLICT
    )
    assert len(_records(home)) == 2


def test_cross_subject_parent_is_not_visible(tmp_path: Path) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    foreign = _register(
        home, repository=repository, subject_id="subject-b"
    ).version

    result = _register(
        home,
        repository=repository,
        subject_id="subject-a",
        parent_version_id=foreign.latex_version_id,
    )

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLatexVersionFailureReason.PARENT_NOT_FOUND
    )


@pytest.mark.parametrize(
    "kind",
    [
        ResumeLatexSourceKind.USER_PROVIDED,
        ResumeLatexSourceKind.IMPORTED_EXISTING,
        ResumeLatexSourceKind.SYSTEM_TEMPLATE_DERIVED,
        ResumeLatexSourceKind.AI_GENERATED,
        ResumeLatexSourceKind.AI_REVISED,
    ],
)
def test_every_source_kind_is_preserved(
    tmp_path: Path, kind: ResumeLatexSourceKind
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)

    result = _register(home, repository=repository, source_kind=kind)

    assert result.version.source_kind is kind
    stored = repository.get(
        subject_id="subject-a",
        latex_version_id=result.version.latex_version_id,
    )
    assert stored.version.source_kind is kind


def test_same_source_with_different_kind_creates_distinct_versions(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)

    user = _register(home, repository=repository).version
    generated = _register(
        home,
        repository=repository,
        source_kind=ResumeLatexSourceKind.AI_GENERATED,
    ).version

    assert user.latex_version_id != generated.latex_version_id
    assert user.source_sha256 == generated.source_sha256
    assert len(_sources(home)) == 1
    assert len(_records(home)) == 2


def test_identical_identity_replays_unchanged_without_duplicates(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)

    first = _register(home, repository=repository, labels=("base",))
    replay = _register(
        home,
        repository=repository,
        labels=("base",),
        now=NOW + timedelta(days=5),
    )

    assert first.status is RegisterResumeLatexVersionStatus.CREATED
    assert replay.status is RegisterResumeLatexVersionStatus.UNCHANGED
    assert replay.version == first.version
    assert replay.version.created_at == NOW
    assert len(_records(home)) == 1
    assert len(_sources(home)) == 1


def test_identity_conflict_does_not_overwrite_history(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    original = _register(home, repository=repository).version
    record = next(
        home.paths.resume_latex_version_records.rglob(
            f"{original.latex_version_id}.json"
        )
    )
    before = record.read_bytes()
    tampered = object.__new__(type(original))
    for field in type(original).__dataclass_fields__:
        object.__setattr__(tampered, field, getattr(original, field))
    object.__setattr__(tampered, "labels", ("tampered",))

    conflict = repository.save(tampered)

    assert conflict.status is ResumeLatexVersionWriteStatus.FAILED
    assert (
        conflict.reason_code
        is ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
    )
    assert record.read_bytes() == before


@pytest.mark.parametrize(
    ("snippet", "capability"),
    [
        (r"\immediate\write18{rm -rf /}", ResumeLatexCapability.SHELL_ESCAPE),
        (r"\ShellEscape{curl http://x}", ResumeLatexCapability.SHELL_ESCAPE),
        (r"\usepackage{shellesc}", ResumeLatexCapability.EXTERNAL_PROGRAM),
        (r"\directlua{os.execute('id')}", ResumeLatexCapability.EXTERNAL_PROGRAM),
        (r"\newwrite\out \openout\out=/etc/x", ResumeLatexCapability.FILE_WRITE),
        (r"\openin\rd=/etc/passwd", ResumeLatexCapability.FILE_READ),
        (r"\input{/etc/passwd}", ResumeLatexCapability.ABSOLUTE_PATH),
        (
            r"\includegraphics{~/Documents/secret.png}",
            ResumeLatexCapability.ABSOLUTE_PATH,
        ),
        (
            r"\input{C:\Users\someone\secret.tex}",
            ResumeLatexCapability.ABSOLUTE_PATH,
        ),
    ],
)
def test_dangerous_capabilities_are_rejected(
    tmp_path: Path, snippet: str, capability: ResumeLatexCapability
) -> None:
    home = _home(tmp_path)

    result = _register(
        home,
        latex_source=SOURCE.replace("\\end{document}", f"{snippet}\n\\end{{document}}"),
    )

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLatexVersionFailureReason.SOURCE_CAPABILITY_REJECTED
    )
    assert result.rejected_capability is capability
    assert not _records(home)
    assert not _sources(home)


def test_relative_includes_remain_allowed(tmp_path: Path) -> None:
    home = _home(tmp_path)

    result = _register(
        home,
        latex_source=SOURCE.replace(
            "\\end{document}",
            "\\input{sections/experience.tex}\n\\end{document}",
        ),
    )

    assert result.status is RegisterResumeLatexVersionStatus.CREATED


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {"latex_source": None, "source_path": None},
            ResumeLatexVersionFailureReason.SOURCE_MISSING,
        ),
        (
            {"latex_source": SOURCE, "source_path": Path("resume.tex")},
            ResumeLatexVersionFailureReason.SOURCE_AMBIGUOUS,
        ),
        (
            {"latex_source": "   "},
            ResumeLatexVersionFailureReason.SOURCE_INVALID,
        ),
        (
            {"latex_source": "x" * (MAX_LATEX_SOURCE_BYTES + 1)},
            ResumeLatexVersionFailureReason.SOURCE_INVALID,
        ),
    ],
)
def test_invalid_source_input_fails_closed(
    tmp_path: Path, kwargs, reason
) -> None:
    home = _home(tmp_path)

    result = _register(home, **kwargs)

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert result.reason_code is reason
    assert not _records(home)


def test_source_outside_private_home_is_rejected(tmp_path: Path) -> None:
    home = _home(tmp_path)
    outside = tmp_path / "outside.tex"
    outside.write_text(SOURCE, encoding="utf-8")

    result = _register(home, latex_source=None, source_path=outside)

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert (
        result.reason_code
        is ResumeLatexVersionFailureReason.SOURCE_UNMANAGED
    )
    assert not _records(home)


def test_non_utf8_source_file_is_rejected(tmp_path: Path) -> None:
    home = _home(tmp_path)
    broken = home.paths.master_documents / "broken.tex"
    broken.write_bytes(b"\\documentclass{article}\xff\xfe binary")

    result = _register(home, latex_source=None, source_path=broken)

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert (
        result.reason_code is ResumeLatexVersionFailureReason.SOURCE_NOT_UTF8
    )


def test_subject_isolation_for_reads_and_listing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    owned = _register(home, repository=repository, subject_id="subject-a")
    _register(
        home,
        repository=repository,
        subject_id="subject-b",
        latex_source=SOURCE.replace("Experience", "Projects"),
    )

    cross = repository.get(
        subject_id="subject-b",
        latex_version_id=owned.version.latex_version_id,
    )
    listed_a = repository.list_selectable("subject-a")
    listed_b = repository.list_selectable("subject-b")

    assert cross.status is ResumeLatexVersionReadStatus.NOT_FOUND
    assert [item.latex_version_id for item in listed_a.versions] == [
        owned.version.latex_version_id
    ]
    assert owned.version.latex_version_id not in {
        item.latex_version_id for item in listed_b.versions
    }


def test_listing_order_is_stable_and_independent_of_the_filesystem(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    for index in range(5):
        _register(
            home,
            repository=repository,
            latex_source=SOURCE.replace("Experience", f"Experience {index}"),
            now=NOW + timedelta(minutes=index),
        )

    first = repository.list_selectable("subject-a")
    for record in _records(home):
        record.touch()
    second = PrivateHomeResumeLatexVersionRepository(
        PrivateHome(home.root)
    ).list_selectable("subject-a")

    identifiers = [item.latex_version_id for item in first.versions]
    assert len(identifiers) == 5
    assert identifiers == sorted(identifiers)
    assert identifiers == [
        item.latex_version_id for item in second.versions
    ]


def test_missing_or_altered_managed_source_fails_closed(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    version = _register(home, repository=repository).version
    managed = home.contained_path(version.source_reference)

    managed.write_text(SOURCE + "\n% drifted\n", encoding="utf-8")
    drifted = repository.get(
        subject_id="subject-a",
        latex_version_id=version.latex_version_id,
    )
    drifted_list = repository.list_selectable("subject-a")
    managed.unlink()
    missing = repository.get(
        subject_id="subject-a",
        latex_version_id=version.latex_version_id,
    )

    assert drifted.status is ResumeLatexVersionReadStatus.INTEGRITY_FAILURE
    assert drifted_list.status is ResumeLatexVersionListStatus.FAILED
    assert missing.status is ResumeLatexVersionReadStatus.INTEGRITY_FAILURE


def test_corrupt_record_fails_closed(tmp_path: Path) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    version = _register(home, repository=repository).version
    record = next(
        home.paths.resume_latex_version_records.rglob(
            f"{version.latex_version_id}.json"
        )
    )
    record.write_bytes(b"{broken")

    read = repository.get(
        subject_id="subject-a",
        latex_version_id=version.latex_version_id,
    )
    listed = repository.list_selectable("subject-a")

    assert read.status is ResumeLatexVersionReadStatus.INTEGRITY_FAILURE
    assert listed.status is ResumeLatexVersionListStatus.FAILED
    assert listed.reason_code is (
        ResumeLatexVersionFailureReason.INTEGRITY_FAILURE
    )


def test_restart_preserves_identity_lineage_and_content(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)
    parent = _register(home, repository=repository).version
    child = _register(
        home,
        repository=repository,
        latex_source=SOURCE.replace("Built", "Delivered"),
        source_kind=ResumeLatexSourceKind.AI_REVISED,
        parent_version_id=parent.latex_version_id,
        now=NOW + timedelta(minutes=1),
    ).version

    restarted = PrivateHomeResumeLatexVersionRepository(
        PrivateHome(home.root)
    )
    read_parent = restarted.get(
        subject_id="subject-a", latex_version_id=parent.latex_version_id
    )
    read_child = restarted.get(
        subject_id="subject-a", latex_version_id=child.latex_version_id
    )

    assert read_parent.version == parent
    assert read_child.version == child
    assert read_child.version.parent_version_id == parent.latex_version_id
    assert read_child.version.root_family_id == parent.root_family_id
    assert read_child.version.content_dict() == child.content_dict()


def test_optional_template_draft_and_qa_bindings_are_recorded(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    result = _register(
        home,
        source_kind=ResumeLatexSourceKind.SYSTEM_TEMPLATE_DERIVED,
        template_id="latex-template-modern",
        template_sha256="a" * 64,
        source_resume_id="resume-candidate-" + "b" * 64,
        tailored_resume_draft_id="tailored-resume-draft-" + "c" * 64,
        tailored_resume_draft_hash="d" * 64,
        fact_qa_result_id="resume-fact-qa-" + "e" * 64,
        fact_qa_result_hash="f" * 64,
    )

    version = result.version
    assert version.template_id == "latex-template-modern"
    assert version.template_sha256 == "a" * 64
    assert version.tailored_resume_draft_hash == "d" * 64
    assert version.fact_qa_result_id == "resume-fact-qa-" + "e" * 64


@pytest.mark.parametrize(
    "bindings",
    [
        {"template_id": "latex-template-modern"},
        {"template_sha256": "a" * 64},
        {"tailored_resume_draft_id": "tailored-resume-draft-" + "c" * 64},
        {
            "fact_qa_result_id": "resume-fact-qa-" + "e" * 64,
            "fact_qa_result_hash": "f" * 64,
        },
    ],
)
def test_incomplete_optional_bindings_fail_closed(
    tmp_path: Path, bindings
) -> None:
    home = _home(tmp_path)

    result = _register(home, **bindings)

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert (
        result.reason_code is ResumeLatexVersionFailureReason.INVALID_REQUEST
    )


def test_empty_registry_is_normal_and_not_an_error(tmp_path: Path) -> None:
    home = _home(tmp_path)

    listed = PrivateHomeResumeLatexVersionRepository(home).list_selectable(
        "subject-a"
    )

    assert listed.status is ResumeLatexVersionListStatus.SUCCEEDED
    assert listed.versions == ()
    assert listed.reason_code is None


def test_labels_are_normalized_so_identity_is_order_independent(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    repository = PrivateHomeResumeLatexVersionRepository(home)

    first = _register(
        home, repository=repository, labels=("beta", "alpha", "alpha")
    )
    reordered = _register(
        home, repository=repository, labels=("alpha", "beta")
    )

    assert first.version.labels == ("alpha", "beta")
    assert reordered.status is RegisterResumeLatexVersionStatus.UNCHANGED
    assert reordered.version.latex_version_id == (
        first.version.latex_version_id
    )


def test_naive_timestamp_is_rejected(tmp_path: Path) -> None:
    home = _home(tmp_path)

    result = _register(home, now=datetime(2026, 7, 28, 23, 0))

    assert result.status is RegisterResumeLatexVersionStatus.FAILED
    assert (
        result.reason_code is ResumeLatexVersionFailureReason.INVALID_REQUEST
    )


def test_module_has_no_selection_compilation_agent_or_execution_dependency() -> None:
    module_path = Path(latex_module.__file__)
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
        "core.browser_broker",
        "core.materials",
        "core.resume_fact_qa",
        "core.resume_tailoring",
        "playwright",
        "subprocess",
    }

    assert not any(
        imported == item or imported.startswith(f"{item}.")
        for imported in imports
        for item in forbidden
    )
