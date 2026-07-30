"""Focused P2c1d1b candidate identity fact lineage tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import core.candidate_identity_facts as identity_facts
import pytest
from core.application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS,
    ApplicationExecutionIdentityFieldKey,
)
from core.candidate_identity_facts import (
    CandidateIdentityFact,
    CandidateIdentityFactConflictState,
    CandidateIdentityFactSourceKind,
    CandidateIdentityFactSourceRef,
    CandidateIdentityFactVerificationStatus,
    GetCurrentCandidateIdentityFactCommand,
    GetCurrentCandidateIdentityFactStatus,
    PrivateHomeCandidateIdentityFactRepository,
    WriteCandidateIdentityFactCommand,
    WriteCandidateIdentityFactStatus,
    get_current_candidate_identity_fact,
    write_candidate_identity_fact,
)
from core.private_home import PrivateHome
from core.profile_store import CandidateVault


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
SUBJECT = "subject-synthetic"


def _source(
    kind: CandidateIdentityFactSourceKind,
    *,
    subject_id: str = SUBJECT,
    suffix: str = "one",
) -> CandidateIdentityFactSourceRef:
    return CandidateIdentityFactSourceRef(
        source_kind=kind,
        source_id=f"source-{suffix}",
        source_version="v1",
        source_hash=hashlib.sha256(
            f"synthetic-source-{suffix}".encode()
        ).hexdigest(),
        source_locator=f"assertion:{suffix}",
        source_subject_id=subject_id,
    )


def _command(
    *,
    field: ApplicationExecutionIdentityFieldKey,
    value: str,
    status: CandidateIdentityFactVerificationStatus,
    source: CandidateIdentityFactSourceRef,
    invocation: str,
    expected: str | None = None,
    parent: str | None = None,
    subject_id: str = SUBJECT,
) -> WriteCandidateIdentityFactCommand:
    return WriteCandidateIdentityFactCommand(
        subject_id=subject_id,
        field_key=field,
        submitted_value=value,
        verification_status=status,
        source_ref=source,
        expected_current_fact_id=expected,
        invocation_id=invocation,
        now=NOW,
        parent_fact_id=parent,
    )


def test_verified_fact_current_read_and_replay(tmp_path: Path) -> None:
    repository = PrivateHomeCandidateIdentityFactRepository(
        PrivateHome(tmp_path / "private")
    )
    command = _command(
        field=ApplicationExecutionIdentityFieldKey.EMAIL,
        value="Synthetic.User@EXAMPLE.TEST",
        status=CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
        source=_source(CandidateIdentityFactSourceKind.USER_CONFIRMATION),
        invocation="invocation-email-one",
    )

    created = write_candidate_identity_fact(command, repository=repository)
    replay = write_candidate_identity_fact(
        replace(command, invocation_id="invocation-email-replay"),
        repository=repository,
    )
    current = get_current_candidate_identity_fact(
        GetCurrentCandidateIdentityFactCommand(
            SUBJECT, ApplicationExecutionIdentityFieldKey.EMAIL
        ),
        repository=repository,
    )

    assert created.status is WriteCandidateIdentityFactStatus.CREATED
    assert created.fact is not None
    assert created.fact.normalized_value == "Synthetic.User@example.test"
    assert created.fact.source_ref == command.source_ref
    assert created.fact.field_version == 1
    assert "Synthetic.User@example.test" not in repr(created)
    assert replay.status is WriteCandidateIdentityFactStatus.UNCHANGED
    assert replay.fact == created.fact
    invocation_mismatch = write_candidate_identity_fact(
        replace(command, submitted_value="other@example.test"),
        repository=repository,
    )
    assert (
        invocation_mismatch.status
        is WriteCandidateIdentityFactStatus.INTEGRITY_FAILURE
    )
    assert invocation_mismatch.failure_code == "INVOCATION_PAYLOAD_MISMATCH"
    assert current.status is GetCurrentCandidateIdentityFactStatus.FOUND
    assert current.fact == created.fact
    assert current.current_lineage_head_id == created.fact.fact_id
    index = repository.get_index(SUBJECT)
    assert tuple(item.field_key for item in index.entries) == tuple(
        sorted(APPLICATION_EXECUTION_IDENTITY_FIELD_KEYS, key=lambda item: item.value)
    )
    assert repository.get_index(SUBJECT).index_hash == index.index_hash


def test_proposal_legacy_and_verification_source_boundaries(tmp_path: Path) -> None:
    repository = PrivateHomeCandidateIdentityFactRepository(
        PrivateHome(tmp_path / "private")
    )
    proposed = repository.write(
        _command(
            field=ApplicationExecutionIdentityFieldKey.FIRST_NAME,
            value="Synthetic",
            status=CandidateIdentityFactVerificationStatus.PROPOSED,
            source=_source(CandidateIdentityFactSourceKind.DOCUMENT_EXTRACTION),
            invocation="invocation-proposal",
        )
    )
    legacy = repository.write(
        _command(
            field=ApplicationExecutionIdentityFieldKey.FIRST_NAME,
            value="Legacy",
            status=CandidateIdentityFactVerificationStatus.LEGACY_UNVERIFIED,
            source=_source(
                CandidateIdentityFactSourceKind.LEGACY_NORMALIZED_PROFILE,
                suffix="legacy",
            ),
            invocation="invocation-legacy",
        )
    )
    invalid_confirmation = repository.write(
        _command(
            field=ApplicationExecutionIdentityFieldKey.LAST_NAME,
            value="Candidate",
            status=CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
            source=_source(
                CandidateIdentityFactSourceKind.DOCUMENT_EXTRACTION,
                suffix="invalid-confirmation",
            ),
            invocation="invocation-invalid-confirmation",
        )
    )

    assert proposed.status is WriteCandidateIdentityFactStatus.CREATED
    assert legacy.status is WriteCandidateIdentityFactStatus.CREATED
    assert proposed.fact is not None and proposed.fact.field_version == 1
    assert legacy.fact is not None and legacy.fact.field_version == 2
    current_command = GetCurrentCandidateIdentityFactCommand(
        SUBJECT, ApplicationExecutionIdentityFieldKey.FIRST_NAME
    )
    assert (
        repository.get_current(current_command).status
        is GetCurrentCandidateIdentityFactStatus.MISSING
    )
    confirmed = repository.write(
        _command(
            field=ApplicationExecutionIdentityFieldKey.FIRST_NAME,
            value="Synthetic",
            status=CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
            source=_source(
                CandidateIdentityFactSourceKind.USER_CONFIRMATION,
                suffix="proposal-confirmation",
            ),
            invocation="invocation-proposal-confirmation",
            parent=proposed.fact.fact_id,
        )
    )
    assert confirmed.status is WriteCandidateIdentityFactStatus.CREATED
    assert confirmed.fact is not None
    assert confirmed.fact.field_version == 3
    assert confirmed.fact.parent_fact_id == proposed.fact.fact_id
    assert (
        repository.get_current(current_command).fact == confirmed.fact
    )
    assert invalid_confirmation.status is WriteCandidateIdentityFactStatus.INVALID
    assert "Synthetic" not in str(invalid_confirmation)
    assert str(tmp_path) not in str(invalid_confirmation)
    with pytest.raises(ValueError, match="structural locator"):
        CandidateIdentityFactSourceRef(
            source_kind=CandidateIdentityFactSourceKind.DOCUMENT_EXTRACTION,
            source_id="source-path",
            source_version="v1",
            source_hash="1" * 64,
            source_locator=str(tmp_path / "resume.pdf"),
            source_subject_id=SUBJECT,
        )


def test_atomic_cas_supersede_stale_conflict_and_integrity(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "private")
    repository = PrivateHomeCandidateIdentityFactRepository(home)
    first = repository.write(
        _command(
            field=ApplicationExecutionIdentityFieldKey.PHONE,
            value="+1 555 0100",
            status=CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
            source=_source(
                CandidateIdentityFactSourceKind.USER_STATEMENT,
                suffix="phone-first",
            ),
            invocation="invocation-phone-first",
        )
    ).fact
    assert first is not None

    commands = tuple(
        _command(
            field=ApplicationExecutionIdentityFieldKey.PHONE,
            value=f"+1 555 010{number}",
            status=CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
            source=_source(
                CandidateIdentityFactSourceKind.USER_CONFIRMATION,
                suffix=f"phone-{number}",
            ),
            invocation=f"invocation-phone-{number}",
            expected=first.fact_id,
        )
        for number in (1, 2)
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(repository.write, commands))
    assert {item.status for item in results} == {
        WriteCandidateIdentityFactStatus.CREATED,
        WriteCandidateIdentityFactStatus.STALE_CURRENT,
    }
    winner = next(item.fact for item in results if item.fact is not None)
    assert winner is not None
    assert winner.field_version == 2
    assert winner.supersedes_fact_id == first.fact_id
    cross_subject_parent = repository.write(
        _command(
            field=ApplicationExecutionIdentityFieldKey.PHONE,
            value="+1 555 0188",
            status=CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
            source=_source(
                CandidateIdentityFactSourceKind.USER_CONFIRMATION,
                subject_id="subject-other",
                suffix="cross-subject",
            ),
            invocation="invocation-cross-subject",
            parent=first.fact_id,
            subject_id="subject-other",
        )
    )
    assert (
        cross_subject_parent.status
        is WriteCandidateIdentityFactStatus.INTEGRITY_FAILURE
    )

    branch_identity = {
        "fact_contract_version": identity_facts.CANDIDATE_IDENTITY_FACT_CONTRACT_VERSION,
        "field_key": ApplicationExecutionIdentityFieldKey.PHONE.value,
        "field_schema_version": identity_facts.APPLICATION_EXECUTION_IDENTITY_FIELD_SCHEMA_VERSION,
        "field_version": 3,
        "normalization_policy_version": identity_facts.APPLICATION_EXECUTION_IDENTITY_NORMALIZATION_POLICY_VERSION,
        "normalized_value": "+1 555 0199",
        "parent_fact_id": first.fact_id,
        "source_ref": _source(
            CandidateIdentityFactSourceKind.USER_CONFIRMATION,
            suffix="phone-branch",
        ).to_dict(),
        "subject_id": SUBJECT,
        "supersedes_fact_id": first.fact_id,
        "value_type": "string",
        "verification_status": CandidateIdentityFactVerificationStatus.USER_CONFIRMED.value,
    }
    branch_hash = identity_facts._hash(branch_identity)
    branch = CandidateIdentityFact(
        fact_id=f"candidate-identity-fact-{branch_hash[:32]}",
        subject_id=SUBJECT,
        field_key=ApplicationExecutionIdentityFieldKey.PHONE,
        normalized_value="+1 555 0199",
        verification_status=CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
        source_ref=identity_facts._source_from_dict(branch_identity["source_ref"]),
        parent_fact_id=first.fact_id,
        field_version=3,
        content_hash=branch_hash,
        created_at=NOW,
        invocation_id="invocation-phone-branch",
        supersedes_fact_id=first.fact_id,
    )
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            """
            INSERT INTO facts(
                fact_id, subject_id, field_key, field_version, content_hash,
                verification_status, semantic_hash, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                branch.fact_id,
                SUBJECT,
                branch.field_key.value,
                branch.field_version,
                branch.content_hash,
                branch.verification_status.value,
                identity_facts._semantic_hash_for_fact(branch),
                json.dumps(branch.to_dict(), sort_keys=True),
            ),
        )
    conflicted = repository.get_current(
        GetCurrentCandidateIdentityFactCommand(
            SUBJECT, ApplicationExecutionIdentityFieldKey.PHONE
        )
    )
    assert conflicted.status is GetCurrentCandidateIdentityFactStatus.CONFLICT

    email = repository.write(
        _command(
            field=ApplicationExecutionIdentityFieldKey.EMAIL,
            value="integrity@example.test",
            status=CandidateIdentityFactVerificationStatus.USER_CONFIRMED,
            source=_source(
                CandidateIdentityFactSourceKind.USER_STATEMENT,
                suffix="integrity-email",
            ),
            invocation="invocation-integrity-email",
        )
    ).fact
    assert email is not None
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE facts SET content_hash = ? WHERE fact_id = ?",
            ("0" * 64, email.fact_id),
        )
    damaged = repository.get_current(
        GetCurrentCandidateIdentityFactCommand(
            SUBJECT, ApplicationExecutionIdentityFieldKey.EMAIL
        )
    )
    assert damaged.status is GetCurrentCandidateIdentityFactStatus.INTEGRITY_FAILURE


def test_legacy_vault_unchanged_private_index_and_safe_diagnostics(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    paths = home.ensure()
    facts_document = {
        "schema_version": 1,
        "normalized": {
            "personal": {
                "first_name": "Legacy",
                "last_name": "Candidate",
                "email": "legacy@example.test",
            }
        },
    }
    paths.profile_facts.write_text(json.dumps(facts_document), encoding="utf-8")
    paths.verified_answers.write_text(
        json.dumps({"schema_version": 1, "answers": {}}), encoding="utf-8"
    )
    paths.policy.write_text(
        json.dumps({"schema_version": 1, "autonomy": {}}), encoding="utf-8"
    )
    before = paths.profile_facts.read_bytes()
    legacy_profile = CandidateVault.load(home).application_profile()
    repository = PrivateHomeCandidateIdentityFactRepository(home)
    missing = repository.get_current(
        GetCurrentCandidateIdentityFactCommand(
            SUBJECT, ApplicationExecutionIdentityFieldKey.FIRST_NAME
        )
    )
    index = repository.get_index(SUBJECT)

    assert legacy_profile["personal"]["first_name"] == "Legacy"
    assert paths.profile_facts.read_bytes() == before
    assert missing.status is GetCurrentCandidateIdentityFactStatus.MISSING
    assert all(
        entry.current_fact_id is None
        and entry.conflict_state is CandidateIdentityFactConflictState.NONE
        for entry in index.entries
    )
    assert repository.path.stat().st_mode & 0o777 == 0o600
    rendered = f"{missing.failure_code or ''}{index.index_hash}"
    for forbidden in (
        "Legacy",
        "legacy@example.test",
        str(home.root),
    ):
        assert forbidden not in rendered
