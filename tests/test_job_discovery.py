from __future__ import annotations

import ast
import inspect
import json
import re
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from core.job_discovery import (
    ATS_TYPES,
    WORK_MODES,
    DiscoveryChange,
    DiscoveryDisposition,
    DiscoveryReason,
    DiscoveryTrigger,
    JobDiscoveryRequest,
    JobIntakeIntent,
    JobIntakeProposal,
    ProposalResolution,
    ResolvedJobCandidate,
    run_discovery,
)
from core.private_home import JOBOPS_HOME_ENV, PrivateHome


ROOT = Path(__file__).resolve().parents[1]
JOB_POSTING_SCHEMA = (
    ROOT / "development_doc" / "contracts" / "job-posting.schema.json"
)


@pytest.fixture
def synthetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PrivateHome:
    root = tmp_path / "synthetic-private-home"
    monkeypatch.setenv(JOBOPS_HOME_ENV, str(root))
    return PrivateHome(root)


def _candidate(
    *,
    source_url: str = "https://boards.greenhouse.io/acme/jobs/123",
    description: str = "Build synthetic distributed systems.",
    application_url: str | None = None,
    **updates,
) -> ResolvedJobCandidate:
    values = {
        "source_platform": "greenhouse",
        "source_url": source_url,
        "source_job_id": "123",
        "application_url": application_url,
        "company": "Acme",
        "title": "Synthetic Engineer",
        "description": description,
        "location": "Remote",
        "work_mode": "REMOTE",
        "posted_at": "2026-07-01T12:30:00Z",
        "ats_type": "greenhouse",
    }
    values.update(updates)
    return ResolvedJobCandidate(**values)


def _request(
    *,
    request_id: str = "request-1",
    proposal_id: str = "proposal-1",
    intent: JobIntakeIntent = JobIntakeIntent.ADD_JOB,
    resolution: ProposalResolution = ProposalResolution.RESOLVED,
    candidate: ResolvedJobCandidate | None = None,
    missing_fields: tuple[str, ...] = (),
    alternatives: tuple[str, ...] = (),
) -> JobDiscoveryRequest:
    resolved_candidate = (
        _candidate()
        if candidate is None and resolution is ProposalResolution.RESOLVED
        else candidate
    )
    return JobDiscoveryRequest(
        request_id=request_id,
        trigger=DiscoveryTrigger.CONVERSATIONAL,
        proposal=JobIntakeProposal(
            proposal_id=proposal_id,
            intent=intent,
            resolution=resolution,
            resolved_candidate=resolved_candidate,
            missing_fields=missing_fields,
            alternatives=alternatives,
        ),
    )


def _json_files(path: Path) -> list[Path]:
    return sorted(path.glob("*.json")) if path.is_dir() else []


def _read_single(path: Path) -> dict:
    files = _json_files(path)
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_run_discovery_has_one_request_parameter() -> None:
    assert tuple(inspect.signature(run_discovery).parameters) == ("request",)


def test_resolved_proposal_creates_revision_one_schema_compatible_job_posting(
    synthetic_home: PrivateHome,
) -> None:
    response = run_discovery(_request())

    assert response.disposition is DiscoveryDisposition.ACCEPTED
    assert response.change is DiscoveryChange.CREATED
    assert response.reason_code is DiscoveryReason.JOB_CREATED
    assert response.run_id
    assert response.job_id

    posting = _read_single(synthetic_home.paths.job_postings)
    schema = json.loads(JOB_POSTING_SCHEMA.read_text(encoding="utf-8"))
    assert set(posting) == set(schema["required"]) == set(schema["properties"])
    assert posting["schema_version"] == schema["properties"]["schema_version"]["const"]
    assert posting["revision"] == 1
    assert posting["status"] == "NORMALIZED"
    assert posting["status"] in schema["properties"]["status"]["enum"]
    assert posting["work_mode"] in schema["properties"]["work_mode"]["enum"]
    assert posting["ats_type"] in schema["properties"]["ats_type"]["enum"]
    assert posting["work_mode"] in WORK_MODES
    assert posting["ats_type"] in ATS_TYPES
    assert re.fullmatch(r"[a-f0-9]{64}", posting["content_hash"])
    assert posting["application_url"] is None

    run = _read_single(synthetic_home.paths.discovery_runs)
    assert run["request_id"] == "request-1"
    assert run["proposal_id"] == "proposal-1"
    assert run["status"] == "SUCCEEDED"
    assert run["disposition"] == "ACCEPTED"
    assert run["change"] == "CREATED"
    assert run["job_id"] == response.job_id
    assert stat.S_IMODE(
        _json_files(synthetic_home.paths.job_postings)[0].stat().st_mode
    ) == 0o600


def test_incomplete_proposal_needs_clarification_without_any_persistence(
    synthetic_home: PrivateHome,
) -> None:
    response = run_discovery(
        _request(
            resolution=ProposalResolution.INCOMPLETE,
            candidate=None,
            missing_fields=("source_url", "description"),
        )
    )

    assert response.disposition is DiscoveryDisposition.NEEDS_CLARIFICATION
    assert response.reason_code is DiscoveryReason.PROPOSAL_INCOMPLETE
    assert response.run_id is None
    assert response.missing_fields == ("source_url", "description")
    assert not synthetic_home.root.exists()


def test_ambiguous_proposal_preserves_alternatives_and_selects_nothing(
    synthetic_home: PrivateHome,
) -> None:
    response = run_discovery(
        _request(
            resolution=ProposalResolution.AMBIGUOUS,
            candidate=None,
            alternatives=("candidate-a", "candidate-b"),
        )
    )

    assert response.disposition is DiscoveryDisposition.NEEDS_CLARIFICATION
    assert response.reason_code is DiscoveryReason.PROPOSAL_AMBIGUOUS
    assert response.alternatives == ("candidate-a", "candidate-b")
    assert response.job_id is None
    assert response.run_id is None
    assert not synthetic_home.root.exists()


def test_unsupported_proposal_is_rejected_and_persists_failed_run(
    synthetic_home: PrivateHome,
) -> None:
    response = run_discovery(
        _request(resolution=ProposalResolution.UNSUPPORTED, candidate=None)
    )

    assert response.disposition is DiscoveryDisposition.REJECTED
    assert response.reason_code is DiscoveryReason.PROPOSAL_UNSUPPORTED
    assert response.run_id
    assert not _json_files(synthetic_home.paths.job_postings)
    run = _read_single(synthetic_home.paths.discovery_runs)
    assert run["status"] == "FAILED"
    assert run["reason_code"] == "PROPOSAL_UNSUPPORTED"


def test_nonresolved_proposal_cannot_carry_a_resolved_candidate(
    synthetic_home: PrivateHome,
) -> None:
    request = JobDiscoveryRequest(
        request_id="request-1",
        trigger=DiscoveryTrigger.CONVERSATIONAL,
        proposal=JobIntakeProposal(
            proposal_id="proposal-1",
            intent=JobIntakeIntent.ADD_JOB,
            resolution=ProposalResolution.INCOMPLETE,
            resolved_candidate=_candidate(),
            missing_fields=("description",),
        ),
    )

    response = run_discovery(request)

    assert response.disposition is DiscoveryDisposition.REJECTED
    assert response.reason_code is DiscoveryReason.RESOLVED_CANDIDATE_NOT_ALLOWED
    assert not _json_files(synthetic_home.paths.job_postings)
    run = _read_single(synthetic_home.paths.discovery_runs)
    assert run["status"] == "FAILED"


@pytest.mark.parametrize(
    "field",
    ("source_platform", "source_url", "company", "title", "description"),
)
def test_missing_required_candidate_field_rejects_without_job_posting(
    synthetic_home: PrivateHome,
    field: str,
) -> None:
    response = run_discovery(
        _request(candidate=replace(_candidate(), **{field: ""}))
    )

    assert response.disposition is DiscoveryDisposition.REJECTED
    assert response.reason_code is DiscoveryReason.REQUIRED_FIELD_MISSING
    assert response.missing_fields == (field,)
    assert response.run_id
    assert not _json_files(synthetic_home.paths.job_postings)
    run = _read_single(synthetic_home.paths.discovery_runs)
    assert run["status"] == "FAILED"


def test_source_url_must_be_absolute_http_or_https(
    synthetic_home: PrivateHome,
) -> None:
    response = run_discovery(
        _request(candidate=_candidate(source_url="/jobs/123"))
    )

    assert response.disposition is DiscoveryDisposition.REJECTED
    assert response.reason_code is DiscoveryReason.INVALID_SOURCE_URL
    assert not _json_files(synthetic_home.paths.job_postings)
    assert _read_single(synthetic_home.paths.discovery_runs)["status"] == "FAILED"


def test_same_identity_and_content_is_unchanged(
    synthetic_home: PrivateHome,
) -> None:
    created = run_discovery(_request())
    unchanged = run_discovery(
        _request(request_id="request-2", proposal_id="proposal-2")
    )

    assert created.job_id == unchanged.job_id
    assert unchanged.change is DiscoveryChange.UNCHANGED
    assert unchanged.reason_code is DiscoveryReason.JOB_UNCHANGED
    posting = _read_single(synthetic_home.paths.job_postings)
    assert posting["revision"] == 1
    assert len(_json_files(synthetic_home.paths.discovery_runs)) == 2


def test_same_identity_with_changed_content_updates_one_posting(
    synthetic_home: PrivateHome,
) -> None:
    created = run_discovery(_request())
    updated = run_discovery(
        _request(
            request_id="request-2",
            proposal_id="proposal-2",
            candidate=_candidate(description="Build a changed synthetic platform."),
        )
    )

    assert created.job_id == updated.job_id
    assert updated.change is DiscoveryChange.UPDATED
    assert updated.reason_code is DiscoveryReason.JOB_UPDATED
    posting = _read_single(synthetic_home.paths.job_postings)
    assert posting["revision"] == 2
    assert posting["description"] == "Build a changed synthetic platform."
    assert len(_json_files(synthetic_home.paths.job_postings)) == 1


def test_tracking_parameters_do_not_create_or_update_another_job(
    synthetic_home: PrivateHome,
) -> None:
    first = run_discovery(
        _request(
            candidate=_candidate(
                source_url=(
                    "https://boards.greenhouse.io/acme/jobs/123"
                    "?utm_source=mail&gh_src=campaign"
                )
            )
        )
    )
    second = run_discovery(
        _request(
            request_id="request-2",
            proposal_id="proposal-2",
            candidate=_candidate(
                source_url="https://boards.greenhouse.io/acme/jobs/123?ref=friend"
            ),
        )
    )

    assert first.job_id == second.job_id
    assert second.change is DiscoveryChange.UNCHANGED
    assert len(_json_files(synthetic_home.paths.job_postings)) == 1
    posting = _read_single(synthetic_home.paths.job_postings)
    assert posting["source_url"] == "https://boards.greenhouse.io/acme/jobs/123"


def test_request_application_is_only_returned_as_intent(
    synthetic_home: PrivateHome,
) -> None:
    response = run_discovery(
        _request(
            intent=JobIntakeIntent.REQUEST_APPLICATION,
            candidate=_candidate(application_url=None),
        )
    )

    assert response.disposition is DiscoveryDisposition.ACCEPTED
    assert response.original_intent is JobIntakeIntent.REQUEST_APPLICATION
    posting = _read_single(synthetic_home.paths.job_postings)
    assert posting["application_url"] is None


def test_resolved_proposal_with_alternatives_needs_clarification_without_run(
    synthetic_home: PrivateHome,
) -> None:
    response = run_discovery(
        _request(alternatives=("candidate-a", "candidate-b"))
    )

    assert response.disposition is DiscoveryDisposition.NEEDS_CLARIFICATION
    assert response.reason_code is DiscoveryReason.MULTIPLE_CANDIDATES
    assert response.run_id is None
    assert not synthetic_home.root.exists()


def test_discovery_core_has_no_forbidden_dependencies() -> None:
    module_path = ROOT / "core" / "job_discovery.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    forbidden = (
        "utils.discovery",
        "utils.csv_apply",
        "utils.tracker",
        "adapters",
        "application_engine",
        "core.application_engine",
    )
    assert not {
        module
        for module in imported
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in forbidden)
    }
