from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.private_home import PrivateHome
from core.resume_latex_dependencies import (
    RESUME_LATEX_DEPENDENCY_POLICY_VERSION,
)
from core.resume_latex_versions import (
    BASE_LATEX_TEMPLATE_CONTRACT_VERSION,
    RESUME_LATEX_SOURCE_SAFETY_POLICY_VERSION,
    LatexSourceProfile,
    PrivateHomeResumeLatexVersionRepository,
    RegisterResumeLatexVersionCommand,
    RegisterResumeLatexVersionStatus,
    ResumeLatexSourceKind,
    ResumeLatexVersionFailureReason,
    ResumeLatexVersionReadStatus,
    register_resume_latex_version,
)


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
STRICT_SOURCE = r"""\documentclass[11pt]{article}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{geometry}
\usepackage{enumitem}
\providecommand{\JobopsSection}[2]{\section*{#2}}
\providecommand{\JobopsBullet}[2]{\item #2}
\begin{document}
%% JOBOPS-CONTENT-BEGIN

%% JOBOPS-CONTENT-END
\end{document}
"""


def _home(tmp_path: Path) -> PrivateHome:
    home = PrivateHome(tmp_path / "private-home")
    home.ensure()
    return home


def _register(
    home: PrivateHome,
    source: str,
    *,
    profile: LatexSourceProfile = LatexSourceProfile.GENERAL_SOURCE_V1,
):
    return register_resume_latex_version(
        RegisterResumeLatexVersionCommand(
            subject_id="subject-synthetic",
            source_kind=ResumeLatexSourceKind.USER_PROVIDED,
            now=NOW,
            latex_source=source,
            source_profile=profile,
        ),
        home=home,
        repository=PrivateHomeResumeLatexVersionRepository(home),
    )


def test_strict_single_file_template_registers_with_versioned_contracts(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    created = _register(
        home,
        STRICT_SOURCE,
        profile=LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1,
    )
    replay = _register(
        home,
        STRICT_SOURCE,
        profile=LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1,
    )

    assert created.status is RegisterResumeLatexVersionStatus.CREATED
    assert replay.status is RegisterResumeLatexVersionStatus.UNCHANGED
    assert replay.version == created.version
    version = created.version
    assert version is not None
    assert (
        version.source_profile
        is LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1
    )
    assert (
        version.template_contract_version
        == BASE_LATEX_TEMPLATE_CONTRACT_VERSION
    )
    assert (
        version.dependency_policy_version
        == RESUME_LATEX_DEPENDENCY_POLICY_VERSION
    )
    assert (
        version.source_safety_policy_version
        == RESUME_LATEX_SOURCE_SAFETY_POLICY_VERSION
    )
    record = next(
        home.paths.resume_latex_version_records.rglob("*.json")
    )
    persisted = json.loads(record.read_text(encoding="utf-8"))
    assert persisted["source_profile"] == "SINGLE_FILE_BASE_TEMPLATE_V1"


def test_strict_profile_rejects_dependencies_capabilities_and_bad_markers(
    tmp_path: Path,
) -> None:
    rejected = (
        (
            STRICT_SOURCE.replace(
                "%% JOBOPS-CONTENT-BEGIN",
                "\\input{sections/experience}\n%% JOBOPS-CONTENT-BEGIN",
            ),
            ResumeLatexVersionFailureReason.DEPENDENCY_POLICY_REJECTED,
        ),
        (
            STRICT_SOURCE.replace(
                "\\usepackage{geometry}",
                "\\usepackage{xcolor}",
            ),
            ResumeLatexVersionFailureReason.DEPENDENCY_POLICY_REJECTED,
        ),
        (
            STRICT_SOURCE.replace(
                "\\begin{document}",
                "\\write18{touch forbidden}\n\\begin{document}",
            ),
            ResumeLatexVersionFailureReason.SOURCE_CAPABILITY_REJECTED,
        ),
        (
            STRICT_SOURCE.replace("%% JOBOPS-CONTENT-END", ""),
            ResumeLatexVersionFailureReason.TEMPLATE_CONTRACT_REJECTED,
        ),
        (
            STRICT_SOURCE.replace(
                "%% JOBOPS-CONTENT-END",
                "%% JOBOPS-CONTENT-BEGIN\n%% JOBOPS-CONTENT-END",
            ),
            ResumeLatexVersionFailureReason.TEMPLATE_CONTRACT_REJECTED,
        ),
        (
            STRICT_SOURCE.replace(
                "%% JOBOPS-CONTENT-BEGIN\n\n%% JOBOPS-CONTENT-END",
                "%% JOBOPS-CONTENT-END\n%% JOBOPS-CONTENT-BEGIN",
            ),
            ResumeLatexVersionFailureReason.TEMPLATE_CONTRACT_REJECTED,
        ),
    )

    for index, (source, reason) in enumerate(rejected):
        result = _register(
            _home(tmp_path / str(index)),
            source,
            profile=LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1,
        )
        assert result.status is RegisterResumeLatexVersionStatus.FAILED
        assert result.reason_code is reason


def test_general_profile_preserves_multifile_and_legacy_record_shape(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    source = r"""\documentclass{article}
\input{sections/experience.tex}
\begin{document}
General source.
\end{document}
"""

    created = _register(home, source)

    assert created.status is RegisterResumeLatexVersionStatus.CREATED
    assert (
        created.version.source_profile
        is LatexSourceProfile.GENERAL_SOURCE_V1
    )
    record = next(
        home.paths.resume_latex_version_records.rglob("*.json")
    )
    persisted = json.loads(record.read_text(encoding="utf-8"))
    assert "source_profile" not in persisted
    read = PrivateHomeResumeLatexVersionRepository(home).get(
        subject_id="subject-synthetic",
        latex_version_id=created.version.latex_version_id,
    )
    assert read.status is ResumeLatexVersionReadStatus.FOUND
    assert read.version == created.version


def test_profile_is_part_of_identity_without_changing_source_identity(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)

    general = _register(home, STRICT_SOURCE)
    strict = _register(
        home,
        STRICT_SOURCE,
        profile=LatexSourceProfile.SINGLE_FILE_BASE_TEMPLATE_V1,
    )

    assert general.status is RegisterResumeLatexVersionStatus.CREATED
    assert strict.status is RegisterResumeLatexVersionStatus.CREATED
    assert general.version.source_sha256 == strict.version.source_sha256
    assert general.version.source_reference == strict.version.source_reference
    assert general.version.latex_version_id != strict.version.latex_version_id
    assert general.version.root_family_id != strict.version.root_family_id
    with pytest.raises(ValueError, match="profile metadata"):
        replace(strict.version, template_contract_version="base-latex-v2")
