"""Synthetic contract tests for P2b3b application-answer preparation."""

from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import core.application_answers as answers_module
from core.application_answer_taxonomy import (
    CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION,
    CanonicalAnswerSensitivity,
    CanonicalApplicationAnswerKey,
    canonical_application_answer_taxonomy_hash,
)
from core.application_answers import (
    ApplicationAnswerPolicy,
    ApplicationFactSnapshot,
    ApplicationFactSourceClassification,
    ApplicationFactVerificationStatus,
    DECLINE_TO_ANSWER,
    PrepareApplicationAnswersCommand,
    PreparedAnswerSource,
    PreparedApplicationAnswerSetFailureReason,
    PreparedApplicationAnswerSetReadStatus,
    PreparedApplicationAnswerSetStatus,
    PrivateHomeApplicationFactProvider,
    PrivateHomePreparedApplicationAnswerSetRepository,
    UnresolvedAnswerReason,
    UnresolvedDefaultHandling,
    prepare_application_answers,
)
from core.application_plan import (
    ApplicationPlan,
    PrivateHomeApplicationPlanRepository,
)
from core.job_prioritization import ProposedPriorityLevel
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
SUBJECT = "subject-synthetic-answers"
OTHER_SUBJECT = "subject-synthetic-other"
JOB_ID = "job-synthetic-answers"


def _plan(
    home: PrivateHome,
    *,
    subject_id: str = SUBJECT,
    instructions: str | None = None,
    job_revision: int = 1,
) -> tuple[ApplicationPlan, PrivateHomeApplicationPlanRepository]:
    plan = ApplicationPlan.create(
        subject_id=subject_id,
        job_id=JOB_ID,
        job_revision=job_revision,
        job_content_hash=("%064x" % job_revision),
        priority_decision_id=f"priority-decision-{job_revision}",
        policy_id="priority-policy-v1",
        policy_version=1,
        policy_content_hash="a" * 64,
        accepted_job_intent_id=f"intent-{job_revision}",
        priority_level=ProposedPriorityLevel.P1,
        created_at=NOW,
        user_preparation_instructions=instructions,
    )
    repository = PrivateHomeApplicationPlanRepository(home)
    assert repository.save(plan).plan == plan
    return plan, repository


def _record(
    key: str,
    value,
    *,
    classification: str = "VERIFIED_FACT",
    sensitivity: str = "BASIC",
    fact_id: str | None = None,
    source_record_id: str | None = None,
    scope: dict | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, dict]:
    fact = fact_id or f"fact-{key}"
    return key, {
        "confirmed_at": NOW.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "fact_id": fact,
        "recorded_at": (NOW - timedelta(days=1)).isoformat(),
        "scope": scope or {},
        "sensitivity": sensitivity,
        "source": "synthetic_candidate_confirmation",
        "source_classification": classification,
        "source_record_id": source_record_id or f"record-{fact}",
        "value": value,
        "verified": True,
    }


def _write_vault(
    home: PrivateHome,
    records: list[tuple[str, dict]],
    *,
    subject_id: str = SUBJECT,
    normalized: dict | None = None,
) -> None:
    paths = home.ensure()
    paths.profile_facts.write_text(
        json.dumps(
            {
                "normalized": normalized or {},
                "schema_version": 1,
                "subject_id": subject_id,
            }
        ),
        encoding="utf-8",
    )
    paths.verified_answers.write_text(
        json.dumps(
            {
                "answers": dict(records),
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    paths.policy.write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )


def _run(
    home: PrivateHome,
    records: list[tuple[str, dict]],
    *,
    instructions: str | None = None,
    now: datetime = NOW,
    policy: ApplicationAnswerPolicy | None = None,
):
    _write_vault(home, records)
    plan, plan_repository = _plan(home, instructions=instructions)
    repository = PrivateHomePreparedApplicationAnswerSetRepository(home)
    result = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=now,
        ),
        application_plan_repository=plan_repository,
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=policy or ApplicationAnswerPolicy.default(),
        answer_set_repository=repository,
    )
    return result, plan, repository


def _answers_by_key(result) -> dict:
    assert result.answer_set is not None
    return {
        item.canonical_key: item for item in result.answer_set.answers
    }


def _unresolved_by_key(result) -> dict:
    assert result.answer_set is not None
    return {
        item.canonical_key: item
        for item in result.answer_set.unresolved_items
    }


def test_trusted_candidate_vault_records_create_typed_immutable_set(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    result, plan, _repository = _run(
        home,
        [
            _record("email", "synthetic@example.test"),
            _record(
                "sponsorship",
                False,
                classification="USER_CONFIRMED",
                sensitivity="LEGAL",
            ),
        ],
    )

    assert result.status is PreparedApplicationAnswerSetStatus.CREATED
    answer_set = result.answer_set
    assert answer_set is not None
    assert answer_set.application_plan_id == plan.plan_id
    assert answer_set.taxonomy_version == (
        CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
    )
    assert answer_set.taxonomy_hash == (
        canonical_application_answer_taxonomy_hash()
    )
    projected = _answers_by_key(result)
    assert projected[CanonicalApplicationAnswerKey.EMAIL].answer_source is (
        PreparedAnswerSource.VERIFIED_FACT
    )
    assert projected[
        CanonicalApplicationAnswerKey.SPONSORSHIP
    ].answer_source is PreparedAnswerSource.USER_CONFIRMED
    with pytest.raises(FrozenInstanceError):
        answer_set.subject_id = "changed"


def test_snapshot_preserves_provenance_scope_and_stable_identity(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    records = [
        _record(
            "phone_number",
            "+1 555 0100",
            classification="USER_CONFIRMED",
            sensitivity="PERSONAL",
            scope={"job_ids": [JOB_ID]},
        ),
        _record("email", "synthetic@example.test"),
    ]
    _write_vault(home, records)
    provider = PrivateHomeApplicationFactProvider(home)
    first = provider.get_current(SUBJECT)
    _write_vault(home, list(reversed(records)))
    second = provider.get_current(SUBJECT)

    assert first == second
    assert first.snapshot_id.endswith(first.snapshot_content_hash)
    phone = next(
        item
        for item in first.facts
        if item.canonical_key is CanonicalApplicationAnswerKey.PHONE
    )
    assert phone.fact_id == "fact-phone_number"
    assert phone.source_record_id == "record-fact-phone_number"
    assert phone.source_classification is (
        ApplicationFactSourceClassification.USER_CONFIRMED
    )
    assert phone.verification_status is (
        ApplicationFactVerificationStatus.USER_CONFIRMED
    )
    assert phone.allowed_scope == {"job_ids": [JOB_ID]}
    assert phone.recorded_at < phone.verified_at


def test_legacy_alias_normalizes_and_unknown_never_becomes_answer(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record("phone_number", "+1 555 0100"),
            _record("favorite_color", "blue"),
        ],
    )

    projected = _answers_by_key(result)
    unresolved = _unresolved_by_key(result)
    assert CanonicalApplicationAnswerKey.PHONE in projected
    assert CanonicalApplicationAnswerKey.UNKNOWN not in projected
    assert unresolved[CanonicalApplicationAnswerKey.UNKNOWN].reason is (
        UnresolvedAnswerReason.UNSUPPORTED
    )


def test_type_mismatch_fails_closed_without_success_record(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    result, _plan_value, repository = _run(
        home,
        [
            _record(
                "work_authorization",
                "probably",
                sensitivity="LEGAL",
            )
        ],
    )

    assert result.status is PreparedApplicationAnswerSetStatus.FAILED
    assert result.reason_code is (
        PreparedApplicationAnswerSetFailureReason.FACT_VALUE_TYPE_MISMATCH
    )
    assert not tuple(
        home.paths.prepared_application_answer_sets.rglob("*.json")
    )
    assert repository.find_current_for_plan(
        subject_id=SUBJECT, application_plan_id="application-plan-" + "0" * 64
    ).status is PreparedApplicationAnswerSetReadStatus.NOT_FOUND


def test_high_stakes_unknowns_are_not_inferred_and_are_safe_skip_defaults(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [_record("email", "synthetic@example.test")],
    )
    unresolved = _unresolved_by_key(result)

    for key in (
        CanonicalApplicationAnswerKey.WORK_AUTHORIZATION,
        CanonicalApplicationAnswerKey.SPONSORSHIP,
        CanonicalApplicationAnswerKey.LOCATION,
        CanonicalApplicationAnswerKey.SALARY,
    ):
        item = unresolved[key]
        assert item.reason is UnresolvedAnswerReason.MISSING_FACT
        assert item.default_handling is (
            UnresolvedDefaultHandling.SAFE_TO_SKIP
        )
        assert item.blocking is False


def test_demographics_use_explicit_choice_or_policy_decline_only(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record("email", "synthetic@example.test"),
            _record(
                "gender",
                "DECLINE_TO_ANSWER",
                classification="USER_CONFIRMED",
                sensitivity="VOLUNTARY_SELF_ID",
            ),
        ],
    )
    projected = _answers_by_key(result)

    assert projected[CanonicalApplicationAnswerKey.GENDER].value == (
        DECLINE_TO_ANSWER
    )
    assert projected[
        CanonicalApplicationAnswerKey.GENDER
    ].answer_source is PreparedAnswerSource.USER_CONFIRMED
    for key in (
        CanonicalApplicationAnswerKey.RACE_ETHNICITY,
        CanonicalApplicationAnswerKey.VETERAN_STATUS,
        CanonicalApplicationAnswerKey.DISABILITY_STATUS,
    ):
        assert projected[key].value == DECLINE_TO_ANSWER
        assert projected[key].answer_source is (
            PreparedAnswerSource.POLICY_DEFAULT
        )
        assert projected[key].supporting_fact_ids == ()


def test_attestation_consent_signature_are_always_human_required(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record("email", "synthetic@example.test"),
            _record(
                "attestation",
                True,
                classification="USER_CONFIRMED",
                sensitivity="ATTESTATION",
            ),
        ],
    )
    projected = _answers_by_key(result)
    unresolved = _unresolved_by_key(result)

    for key in (
        CanonicalApplicationAnswerKey.ATTESTATION,
        CanonicalApplicationAnswerKey.CONSENT,
        CanonicalApplicationAnswerKey.SIGNATURE,
    ):
        assert key not in projected
        assert unresolved[key].reason is (
            UnresolvedAnswerReason.REQUIRES_ATTESTATION
        )
        assert unresolved[key].blocking is True


def test_unresolved_items_do_not_discard_safe_answers(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record("email", "synthetic@example.test"),
            _record("sponsorship", False, sensitivity="LEGAL"),
        ],
    )

    assert result.status is PreparedApplicationAnswerSetStatus.CREATED
    assert CanonicalApplicationAnswerKey.EMAIL in _answers_by_key(result)
    assert CanonicalApplicationAnswerKey.CONSENT in _unresolved_by_key(
        result
    )


def test_plan_instruction_can_restrict_but_not_authorize_attestation(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record("email", "synthetic@example.test"),
            _record(
                "salary",
                "User-confirmed range",
                classification="USER_CONFIRMED",
                sensitivity="COMPENSATION",
            ),
            _record(
                "consent",
                True,
                classification="USER_CONFIRMED",
                sensitivity="ATTESTATION",
            ),
        ],
        instructions="Do not fill salary expectation. Consent is approved.",
    )
    projected = _answers_by_key(result)
    unresolved = _unresolved_by_key(result)

    assert CanonicalApplicationAnswerKey.SALARY not in projected
    assert unresolved[CanonicalApplicationAnswerKey.SALARY].reason is (
        UnresolvedAnswerReason.POLICY_FORBIDS_AUTOMATION
    )
    assert unresolved[CanonicalApplicationAnswerKey.CONSENT].reason is (
        UnresolvedAnswerReason.REQUIRES_ATTESTATION
    )


def test_verified_salary_requires_explicit_user_confirmation(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record("email", "synthetic@example.test"),
            _record(
                "salary",
                "Synthetic range",
                sensitivity="COMPENSATION",
            ),
        ],
    )

    assert CanonicalApplicationAnswerKey.SALARY not in _answers_by_key(
        result
    )
    salary = _unresolved_by_key(result)[
        CanonicalApplicationAnswerKey.SALARY
    ]
    assert salary.reason is UnresolvedAnswerReason.REQUIRES_USER_CHOICE
    assert salary.blocking is True


def test_no_typed_trusted_facts_defers_without_writing(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    legacy = {
        "confirmed_at": NOW.isoformat(),
        "scope": {},
        "sensitivity": "personal",
        "source": "legacy",
        "value": "synthetic@example.test",
        "verified": True,
    }
    result, _plan_value, _repository = _run(
        home, [("email", legacy)]
    )

    assert result.status is (
        PreparedApplicationAnswerSetStatus.DEFERRED_NO_TRUSTED_FACTS
    )
    assert result.answer_set is None
    assert not tuple(
        home.paths.prepared_application_answer_sets.rglob("*.json")
    )


def test_expired_and_other_job_scoped_facts_are_not_current(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record(
                "email",
                "expired@example.test",
                expires_at=NOW + timedelta(hours=1),
            ),
            _record(
                "phone",
                "+1 555 0199",
                scope={"job_id": "job-other"},
            ),
        ],
        now=NOW + timedelta(days=1),
    )

    assert result.status is (
        PreparedApplicationAnswerSetStatus.DEFERRED_NO_TRUSTED_FACTS
    )


def test_conflicting_trusted_values_require_human_but_keep_safe_answers(
    tmp_path: Path,
) -> None:
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record("email", "synthetic@example.test"),
            _record(
                "sponsorship",
                False,
                sensitivity="LEGAL",
                fact_id="fact-sponsorship-a",
            ),
            _record(
                "require_sponsorship",
                True,
                classification="USER_CONFIRMED",
                sensitivity="LEGAL",
                fact_id="fact-sponsorship-b",
            ),
        ],
    )

    assert result.status is PreparedApplicationAnswerSetStatus.CREATED
    assert CanonicalApplicationAnswerKey.EMAIL in _answers_by_key(result)
    sponsorship = _unresolved_by_key(result)[
        CanonicalApplicationAnswerKey.SPONSORSHIP
    ]
    assert sponsorship.reason is (
        UnresolvedAnswerReason.REQUIRES_USER_CHOICE
    )
    assert sponsorship.blocking is True


def test_malformed_typed_record_and_subject_mismatch_fail_closed(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    malformed_key, malformed = _record(
        "email", "synthetic@example.test"
    )
    malformed.pop("fact_id")
    result, _plan_value, _repository = _run(
        home, [(malformed_key, malformed)]
    )
    assert result.status is PreparedApplicationAnswerSetStatus.FAILED
    assert result.reason_code is (
        PreparedApplicationAnswerSetFailureReason
        .FACT_SNAPSHOT_INTEGRITY_FAILURE
    )

    _write_vault(
        home,
        [_record("email", "synthetic@example.test")],
        subject_id=OTHER_SUBJECT,
    )
    plan, plan_repository = _plan(
        home, subject_id=SUBJECT, job_revision=2
    )
    mismatch = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=plan_repository,
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=ApplicationAnswerPolicy.default(),
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
    )
    assert mismatch.status is PreparedApplicationAnswerSetStatus.FAILED
    assert mismatch.reason_code is (
        PreparedApplicationAnswerSetFailureReason
        .FACT_SNAPSHOT_INTEGRITY_FAILURE
    )


class _StaticProvider:
    def __init__(self, snapshot: ApplicationFactSnapshot) -> None:
        self.snapshot = snapshot

    def get_current(self, _subject_id: str) -> ApplicationFactSnapshot:
        return self.snapshot


def test_snapshot_subject_mismatch_has_typed_failure(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    _write_vault(
        home,
        [_record("email", "synthetic@example.test")],
        subject_id=OTHER_SUBJECT,
    )
    snapshot = PrivateHomeApplicationFactProvider(home).get_current(
        OTHER_SUBJECT
    )
    plan, plan_repository = _plan(home)
    result = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW,
        ),
        application_plan_repository=plan_repository,
        fact_provider=_StaticProvider(snapshot),
        answer_policy=ApplicationAnswerPolicy.default(),
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
    )

    assert result.status is PreparedApplicationAnswerSetStatus.FAILED
    assert result.reason_code is (
        PreparedApplicationAnswerSetFailureReason
        .FACT_SNAPSHOT_SUBJECT_MISMATCH
    )


def test_same_binding_is_unchanged_and_preserves_original_time(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    records = [_record("email", "synthetic@example.test")]
    first, plan, repository = _run(home, records)
    second = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(days=5),
        ),
        application_plan_repository=(
            PrivateHomeApplicationPlanRepository(home)
        ),
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=ApplicationAnswerPolicy.default(),
        answer_set_repository=repository,
    )

    assert first.status is PreparedApplicationAnswerSetStatus.CREATED
    assert second.status is PreparedApplicationAnswerSetStatus.UNCHANGED
    assert second.answer_set is not None and first.answer_set is not None
    assert second.answer_set.answer_set_id == first.answer_set.answer_set_id
    assert second.answer_set.prepared_at == first.answer_set.prepared_at
    assert len(
        tuple(home.paths.prepared_application_answer_sets.rglob("*.json"))
    ) == 1


def test_fact_policy_and_plan_changes_create_immutable_history(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    first, plan, repository = _run(
        home, [_record("email", "first@example.test")]
    )
    _write_vault(home, [_record("email", "second@example.test")])
    changed_fact = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(minutes=1),
        ),
        application_plan_repository=(
            PrivateHomeApplicationPlanRepository(home)
        ),
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=ApplicationAnswerPolicy.default(),
        answer_set_repository=repository,
    )
    email_only = ApplicationAnswerPolicy.create(
        policy_id="application-answer-policy-email-only-v1",
        tracked_keys=(CanonicalApplicationAnswerKey.EMAIL,),
    )
    changed_policy = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(minutes=2),
        ),
        application_plan_repository=(
            PrivateHomeApplicationPlanRepository(home)
        ),
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=email_only,
        answer_set_repository=repository,
    )
    changed_plan, changed_plan_repository = _plan(
        home, instructions="Do not fill email.", job_revision=2
    )
    changed_plan_result = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=changed_plan.plan_id,
            now=NOW + timedelta(minutes=3),
        ),
        application_plan_repository=changed_plan_repository,
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=ApplicationAnswerPolicy.default(),
        answer_set_repository=repository,
    )

    assert first.status is PreparedApplicationAnswerSetStatus.CREATED
    assert changed_fact.status is PreparedApplicationAnswerSetStatus.CREATED
    assert changed_policy.status is (
        PreparedApplicationAnswerSetStatus.CREATED
    )
    assert changed_plan_result.status is (
        PreparedApplicationAnswerSetStatus.CREATED
    )
    ids = {
        item.answer_set.answer_set_id
        for item in (first, changed_fact, changed_policy, changed_plan_result)
        if item.answer_set is not None
    }
    assert len(ids) == 4
    assert len(
        tuple(home.paths.prepared_application_answer_sets.rglob("*.json"))
    ) == 4


def test_repository_restart_current_is_deterministic_and_subject_isolated(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    first, plan, _repository = _run(
        home, [_record("email", "first@example.test")]
    )
    _write_vault(home, [_record("email", "second@example.test")])
    second = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(minutes=1),
        ),
        application_plan_repository=(
            PrivateHomeApplicationPlanRepository(home)
        ),
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=ApplicationAnswerPolicy.default(),
        answer_set_repository=(
            PrivateHomePreparedApplicationAnswerSetRepository(home)
        ),
    )
    restarted = PrivateHomePreparedApplicationAnswerSetRepository(home)
    current = restarted.find_current_for_plan(
        subject_id=SUBJECT, application_plan_id=plan.plan_id
    )

    assert first.answer_set is not None and second.answer_set is not None
    assert current.status is PreparedApplicationAnswerSetReadStatus.FOUND
    assert current.answer_set == second.answer_set
    assert current.answer_set.answer_set_content_hash == (
        second.answer_set.answer_set_content_hash
    )
    assert restarted.get(
        subject_id=OTHER_SUBJECT,
        answer_set_id=second.answer_set.answer_set_id,
    ).status is PreparedApplicationAnswerSetReadStatus.NOT_FOUND


def test_repository_corruption_and_conflict_never_overwrite_history(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    created, plan, repository = _run(
        home, [_record("email", "synthetic@example.test")]
    )
    assert created.answer_set is not None
    artifact = next(
        home.paths.prepared_application_answer_sets.rglob("*.json")
    )
    artifact.write_text("{broken", encoding="utf-8")
    replay = prepare_application_answers(
        PrepareApplicationAnswersCommand(
            subject_id=SUBJECT,
            application_plan_id=plan.plan_id,
            now=NOW + timedelta(days=1),
        ),
        application_plan_repository=(
            PrivateHomeApplicationPlanRepository(home)
        ),
        fact_provider=PrivateHomeApplicationFactProvider(home),
        answer_policy=ApplicationAnswerPolicy.default(),
        answer_set_repository=repository,
    )

    assert replay.status is PreparedApplicationAnswerSetStatus.FAILED
    assert replay.reason_code is (
        PreparedApplicationAnswerSetFailureReason
        .ANSWER_SET_INTEGRITY_FAILURE
    )
    assert artifact.read_text(encoding="utf-8") == "{broken"


def test_deferred_needs_human_is_available_without_fake_answers(
    tmp_path: Path,
) -> None:
    policy = ApplicationAnswerPolicy.create(
        policy_id="application-answer-policy-attestation-only-v1",
        tracked_keys=(CanonicalApplicationAnswerKey.ATTESTATION,),
        attestation_keys=(CanonicalApplicationAnswerKey.ATTESTATION,),
    )
    result, _plan_value, _repository = _run(
        PrivateHome(tmp_path / "private"),
        [
            _record(
                "attestation",
                True,
                classification="USER_CONFIRMED",
                sensitivity="ATTESTATION",
            )
        ],
        policy=policy,
    )

    assert result.status is (
        PreparedApplicationAnswerSetStatus.DEFERRED_NEEDS_HUMAN
    )
    assert result.answer_set is None


def test_normal_profile_candidate_summary_and_execution_surfaces_are_unused(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "private")
    result, _plan_value, _repository = _run(
        home,
        [_record("email", "trusted@example.test")],
    )
    _write_vault(
        home,
        [_record("email", "trusted@example.test")],
        normalized={
            "personal": {"email": "untrusted@example.test"},
            "candidate_summary": {"work_authorization": True},
        },
    )

    assert _answers_by_key(result)[
        CanonicalApplicationAnswerKey.EMAIL
    ].value == "trusted@example.test"
    tree = ast.parse(
        Path(answers_module.__file__).read_text(encoding="utf-8")
    )
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
    assert not any(
        forbidden in imported.casefold()
        for imported in imports
        for forbidden in (
            "semantic_mapper",
            "form_ir",
            "browser",
            "application_engine",
            "gate",
            "adapters",
        )
    )
