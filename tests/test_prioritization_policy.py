from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.prioritization_policy import (
    ApprovePolicyRequest,
    CreatePolicyDraftRequest,
    HardConstraint,
    HardConstraintType,
    InMemoryPrioritizationPolicyDraftStore,
    PolicyDraftStatus,
    PolicyInterpretation,
    PolicyOperationStatus,
    PolicyReason,
    PreferenceImportance,
    PrioritizationPolicyService,
    PrioritizationPolicyStatus,
    PrivateHomePrioritizationPolicyRepository,
    SoftPreference,
    SoftPreferenceCategory,
    policy_content_hash,
)
from core.private_home import PrivateHome


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
RAW_POLICY = (
    "Vancouver is best. Do not apply to jobs in the United States. "
    "Prioritize AI for Earth."
)


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeInterpreter:
    def __init__(self, output=None, error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[CreatePolicyDraftRequest] = []

    async def interpret(self, request: CreatePolicyDraftRequest):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.output


def _hard(*, confirmed: bool = False) -> HardConstraint:
    return HardConstraint(
        constraint_type=HardConstraintType.EXCLUDED_COUNTRY,
        normalized_value="United   States",
        source_excerpt="Do not apply to jobs in the United States.",
        user_confirmed=confirmed,
    )


def _soft(
    *,
    preference_id: str = "pref-domain",
    statement: str = "Prioritize AI for Earth",
) -> SoftPreference:
    return SoftPreference(
        preference_id=preference_id,
        category=SoftPreferenceCategory.DOMAIN,
        statement=statement,
        importance=PreferenceImportance.HIGH,
        source_excerpt=statement,
    )


def _interpretation(
    *,
    subject_id: str = "candidate-synthetic",
    raw_text: str = RAW_POLICY,
    hard_constraints: tuple[HardConstraint, ...] | None = None,
    soft_preferences: tuple[SoftPreference, ...] | None = None,
    ambiguities: tuple[str, ...] = (),
    interpreter_version: str = "fake-interpreter-v1",
) -> PolicyInterpretation:
    return PolicyInterpretation(
        subject_id=subject_id,
        raw_preference_text=raw_text,
        hard_constraints=(
            (_hard(),) if hard_constraints is None else hard_constraints
        ),
        soft_preferences=(
            (_soft(),) if soft_preferences is None else soft_preferences
        ),
        ambiguities=ambiguities,
        interpreter_version=interpreter_version,
    )


def _service(
    tmp_path: Path,
    *,
    output=None,
    error: Exception | None = None,
    clock: MutableClock | None = None,
    draft_id: str = "draft-synthetic-1",
    ttl: timedelta = timedelta(minutes=30),
):
    home = PrivateHome(tmp_path / "synthetic-private-home")
    interpreter = FakeInterpreter(
        output=output if output is not None else _interpretation(),
        error=error,
    )
    store = InMemoryPrioritizationPolicyDraftStore()
    repository = PrivateHomePrioritizationPolicyRepository(home)
    active_clock = clock or MutableClock()
    service = PrioritizationPolicyService(
        interpreter=interpreter,
        draft_store=store,
        repository=repository,
        draft_ttl=ttl,
        clock=active_clock,
        draft_id_factory=lambda: draft_id,
    )
    return service, interpreter, store, repository, home, active_clock


async def _create_ready_draft(service: PrioritizationPolicyService):
    result = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=RAW_POLICY,
        )
    )
    assert result.draft is not None
    assert result.draft.status is PolicyDraftStatus.READY_FOR_APPROVAL
    return result.draft


def _approval(
    draft_id: str,
    *,
    subject_id: str = "candidate-synthetic",
    raw_text: str = RAW_POLICY,
    hard_constraints: tuple[HardConstraint, ...] | None = None,
    soft_preferences: tuple[SoftPreference, ...] | None = None,
) -> ApprovePolicyRequest:
    return ApprovePolicyRequest(
        draft_id=draft_id,
        subject_id=subject_id,
        reviewed_raw_preference_text=raw_text,
        reviewed_hard_constraints=(
            (_hard(confirmed=True),)
            if hard_constraints is None
            else hard_constraints
        ),
        reviewed_soft_preferences=(
            (_soft(),) if soft_preferences is None else soft_preferences
        ),
    )


@pytest.mark.asyncio
async def test_policy_text_calls_interpreter_once_and_creates_typed_draft(
    tmp_path: Path,
) -> None:
    service, interpreter, store, _, home, _ = _service(tmp_path)

    result = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=RAW_POLICY,
        )
    )

    assert len(interpreter.calls) == 1
    assert result.status is PolicyOperationStatus.NEEDS_USER
    assert result.reason_code is PolicyReason.POLICY_REVIEW_REQUIRED
    assert result.draft is not None
    assert result.draft.status is PolicyDraftStatus.READY_FOR_APPROVAL
    assert result.draft.hard_constraints[0].user_confirmed is False
    assert store.get(result.draft.draft_id) == result.draft
    assert not home.root.exists()


@pytest.mark.asyncio
async def test_empty_policy_text_never_calls_interpreter(tmp_path: Path) -> None:
    service, interpreter, store, _, home, _ = _service(tmp_path)

    result = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text="   ",
        )
    )

    assert result.status is PolicyOperationStatus.FAILED
    assert result.reason_code is PolicyReason.INVALID_REQUEST
    assert interpreter.calls == []
    assert store.get("draft-synthetic-1") is None
    assert not home.root.exists()


@pytest.mark.asyncio
async def test_malformed_or_subject_changing_interpreter_output_is_discarded(
    tmp_path: Path,
) -> None:
    service, interpreter, store, _, home, _ = _service(
        tmp_path,
        output=_interpretation(subject_id="different-subject"),
    )

    result = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=RAW_POLICY,
        )
    )

    assert len(interpreter.calls) == 1
    assert result.reason_code is PolicyReason.INTERPRETER_OUTPUT_INVALID
    assert result.draft is None
    assert store.get("draft-synthetic-1") is None
    assert not home.root.exists()


@pytest.mark.asyncio
async def test_non_typed_interpreter_output_is_discarded(tmp_path: Path) -> None:
    service, _, store, _, home, _ = _service(
        tmp_path,
        output={"hard_constraints": []},
    )

    result = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=RAW_POLICY,
        )
    )

    assert result.reason_code is PolicyReason.INTERPRETER_OUTPUT_INVALID
    assert store.get("draft-synthetic-1") is None
    assert not home.root.exists()


@pytest.mark.asyncio
async def test_interpreter_cannot_self_confirm_a_hard_constraint(
    tmp_path: Path,
) -> None:
    service, _, store, _, home, _ = _service(
        tmp_path,
        output=_interpretation(
            hard_constraints=(_hard(confirmed=True),),
        ),
    )

    result = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=RAW_POLICY,
        )
    )

    assert result.reason_code is PolicyReason.INTERPRETER_OUTPUT_INVALID
    assert store.get("draft-synthetic-1") is None
    assert not home.root.exists()


@pytest.mark.asyncio
async def test_interpreter_failure_is_typed_and_changes_no_active_policy(
    tmp_path: Path,
) -> None:
    service, interpreter, _, repository, home, _ = _service(
        tmp_path,
        error=RuntimeError("synthetic interpreter failure"),
    )

    result = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=RAW_POLICY,
        )
    )

    assert len(interpreter.calls) == 1
    assert result.reason_code is PolicyReason.INTERPRETER_FAILED
    assert repository.get_active_policy("candidate-synthetic") is None
    assert not home.root.exists()


def test_hard_constraint_and_soft_preference_allowlists_are_enforced() -> None:
    with pytest.raises(ValueError):
        HardConstraint(
            constraint_type="SALARY_FORMULA",
            normalized_value="100000",
            source_excerpt="at least 100000",
        )
    with pytest.raises(ValueError):
        SoftPreference(
            preference_id="pref-invalid",
            category="TOOL",
            statement="call a browser",
            source_excerpt="call a browser",
        )
    with pytest.raises(ValueError):
        SoftPreference(
            preference_id="pref-invalid",
            category=SoftPreferenceCategory.OTHER,
            statement="consider this",
            source_excerpt="consider this",
            importance="CRITICAL",
        )


@pytest.mark.asyncio
async def test_prefer_text_is_not_upgraded_by_ordinary_code(
    tmp_path: Path,
) -> None:
    raw = "Vancouver is preferred."
    service, _, _, _, _, _ = _service(
        tmp_path,
        output=_interpretation(
            raw_text=raw,
            hard_constraints=(),
            soft_preferences=(
                SoftPreference(
                    preference_id="pref-location",
                    category=SoftPreferenceCategory.LOCATION,
                    statement="Prefer Vancouver",
                    importance=PreferenceImportance.HIGH,
                    source_excerpt=raw,
                ),
            ),
        ),
    )

    result = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=raw,
        )
    )

    assert result.draft is not None
    assert result.draft.hard_constraints == ()
    assert result.draft.soft_preferences[0].category is (
        SoftPreferenceCategory.LOCATION
    )


def test_student_only_roles_can_be_reviewed_as_soft_or_hard_policy() -> None:
    soft = SoftPreference(
        preference_id="pref-student-eligibility",
        category=SoftPreferenceCategory.ELIGIBILITY,
        statement="Student-only roles should usually be lower priority.",
        importance=PreferenceImportance.HIGH,
        source_excerpt="Usually deprioritize student-only roles.",
    )
    hard = HardConstraint(
        constraint_type=HardConstraintType.EXCLUDED_STUDENT_ONLY_ROLE,
        normalized_value="student-only role",
        source_excerpt="Do not apply to student-only roles.",
        user_confirmed=True,
    )

    assert soft.category is SoftPreferenceCategory.ELIGIBILITY
    assert hard.constraint_type is (
        HardConstraintType.EXCLUDED_STUDENT_ONLY_ROLE
    )
    assert hard.user_confirmed is True


@pytest.mark.asyncio
async def test_blocking_ambiguity_creates_draft_but_prevents_approval(
    tmp_path: Path,
) -> None:
    service, _, _, repository, _, _ = _service(
        tmp_path,
        output=_interpretation(
            ambiguities=("Does Toronto remain acceptable?",),
        ),
    )
    created = await service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=RAW_POLICY,
        )
    )
    assert created.draft is not None
    assert created.reason_code is PolicyReason.POLICY_NEEDS_CLARIFICATION
    assert created.draft.status is PolicyDraftStatus.NEEDS_CLARIFICATION

    approved = service.approve_policy(_approval(created.draft.draft_id))

    assert approved.status is PolicyOperationStatus.NEEDS_USER
    assert approved.reason_code is PolicyReason.POLICY_NEEDS_CLARIFICATION
    assert repository.get_active_policy("candidate-synthetic") is None


@pytest.mark.asyncio
async def test_unconfirmed_hard_constraint_prevents_approval(
    tmp_path: Path,
) -> None:
    service, _, _, repository, _, _ = _service(tmp_path)
    draft = await _create_ready_draft(service)

    result = service.approve_policy(
        _approval(
            draft.draft_id,
            hard_constraints=(_hard(confirmed=False),),
        )
    )

    assert result.status is PolicyOperationStatus.NEEDS_USER
    assert result.reason_code is PolicyReason.HARD_CONSTRAINT_NOT_CONFIRMED
    assert repository.get_active_policy("candidate-synthetic") is None


@pytest.mark.asyncio
async def test_subject_mismatch_and_expiry_prevent_approval(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    service, _, store, repository, _, _ = _service(
        tmp_path,
        clock=clock,
        ttl=timedelta(minutes=5),
    )
    draft = await _create_ready_draft(service)

    mismatch = service.approve_policy(
        _approval(draft.draft_id, subject_id="another-subject")
    )
    assert mismatch.reason_code is PolicyReason.SUBJECT_MISMATCH
    assert repository.get_active_policy("candidate-synthetic") is None

    clock.value = NOW + timedelta(minutes=5)
    expired = service.approve_policy(_approval(draft.draft_id))
    assert expired.reason_code is PolicyReason.DRAFT_EXPIRED
    assert store.get(draft.draft_id).status is PolicyDraftStatus.EXPIRED
    assert repository.get_active_policy("candidate-synthetic") is None


@pytest.mark.asyncio
async def test_first_approval_persists_active_version_one_and_raw_policy(
    tmp_path: Path,
) -> None:
    service, _, store, repository, home, _ = _service(tmp_path)
    draft = await _create_ready_draft(service)

    result = service.approve_policy(_approval(draft.draft_id))

    assert result.status is PolicyOperationStatus.SUCCEEDED
    assert result.policy is not None
    assert result.policy.policy_version == 1
    assert result.policy.status is PrioritizationPolicyStatus.ACTIVE
    assert result.policy.raw_preference_text == RAW_POLICY
    assert result.policy.hard_constraints == (_hard(confirmed=True),)
    assert result.policy.soft_preferences == (_soft(),)
    assert store.get(draft.draft_id).status is PolicyDraftStatus.APPROVED
    assert service.get_active_policy("candidate-synthetic") == result.policy
    assert repository.get_active_policy("candidate-synthetic") == result.policy
    files = list(home.paths.prioritization_policies.glob("*.json"))
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    persisted = json.loads(files[0].read_text(encoding="utf-8"))
    assert persisted["active_policy_id"] == result.policy.policy_id


@pytest.mark.asyncio
async def test_approval_persists_user_reviewed_edits_not_interpreter_output(
    tmp_path: Path,
) -> None:
    service, _, _, _, _, _ = _service(tmp_path)
    draft = await _create_ready_draft(service)
    reviewed_raw = RAW_POLICY + " General ML at an excellent company is acceptable."
    reviewed_soft = (
        _soft(),
        SoftPreference(
            preference_id="pref-company-quality",
            category=SoftPreferenceCategory.COMPANY,
            statement="Consider general ML at an excellent company",
            importance=PreferenceImportance.MEDIUM,
            source_excerpt="General ML at an excellent company is acceptable.",
        ),
    )

    result = service.approve_policy(
        _approval(
            draft.draft_id,
            raw_text=reviewed_raw,
            soft_preferences=reviewed_soft,
        )
    )

    assert result.policy is not None
    assert result.policy.raw_preference_text == reviewed_raw
    assert result.policy.soft_preferences == reviewed_soft


def test_policy_content_hash_excludes_draft_time_and_interpreter_metadata() -> None:
    first = policy_content_hash(
        raw_preference_text=RAW_POLICY,
        hard_constraints=(_hard(confirmed=True),),
        soft_preferences=(_soft(),),
    )
    second = policy_content_hash(
        raw_preference_text=f"  {RAW_POLICY}  ",
        hard_constraints=(_hard(confirmed=True),),
        soft_preferences=(_soft(),),
    )

    assert first == second


@pytest.mark.asyncio
async def test_same_active_content_is_idempotent_across_new_drafts(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "synthetic-private-home")
    repository = PrivateHomePrioritizationPolicyRepository(home)
    clock = MutableClock()

    first_service = PrioritizationPolicyService(
        interpreter=FakeInterpreter(_interpretation()),
        draft_store=InMemoryPrioritizationPolicyDraftStore(),
        repository=repository,
        clock=clock,
        draft_id_factory=lambda: "draft-first",
    )
    first_draft = await _create_ready_draft(first_service)
    first = first_service.approve_policy(_approval(first_draft.draft_id))
    assert first.policy is not None

    clock.value = NOW + timedelta(minutes=1)
    second_service = PrioritizationPolicyService(
        interpreter=FakeInterpreter(
            _interpretation(interpreter_version="fake-interpreter-v2")
        ),
        draft_store=InMemoryPrioritizationPolicyDraftStore(),
        repository=repository,
        clock=clock,
        draft_id_factory=lambda: "draft-second",
    )
    second_draft = await _create_ready_draft(second_service)
    second = second_service.approve_policy(_approval(second_draft.draft_id))

    assert second.policy == first.policy
    assert len(repository.list_policies("candidate-synthetic")) == 1


@pytest.mark.asyncio
async def test_changed_content_appends_version_and_supersedes_without_deletion(
    tmp_path: Path,
) -> None:
    home = PrivateHome(tmp_path / "synthetic-private-home")
    repository = PrivateHomePrioritizationPolicyRepository(home)
    clock = MutableClock()
    first_service = PrioritizationPolicyService(
        interpreter=FakeInterpreter(_interpretation()),
        draft_store=InMemoryPrioritizationPolicyDraftStore(),
        repository=repository,
        clock=clock,
        draft_id_factory=lambda: "draft-first",
    )
    first_draft = await _create_ready_draft(first_service)
    first = first_service.approve_policy(_approval(first_draft.draft_id))
    assert first.policy is not None

    changed_raw = RAW_POLICY + " Toronto is also acceptable."
    clock.value = NOW + timedelta(minutes=1)
    second_service = PrioritizationPolicyService(
        interpreter=FakeInterpreter(
            _interpretation(
                raw_text=changed_raw,
                soft_preferences=(
                    _soft(),
                    SoftPreference(
                        preference_id="pref-location",
                        category=SoftPreferenceCategory.LOCATION,
                        statement="Toronto is acceptable",
                        importance=PreferenceImportance.MEDIUM,
                        source_excerpt="Toronto is also acceptable.",
                    ),
                ),
            )
        ),
        draft_store=InMemoryPrioritizationPolicyDraftStore(),
        repository=repository,
        clock=clock,
        draft_id_factory=lambda: "draft-second",
    )
    created = await second_service.create_policy_draft(
        CreatePolicyDraftRequest(
            subject_id="candidate-synthetic",
            raw_preference_text=changed_raw,
        )
    )
    assert created.draft is not None
    second = second_service.approve_policy(
        _approval(
            created.draft.draft_id,
            raw_text=changed_raw,
            soft_preferences=created.draft.soft_preferences,
        )
    )
    assert second.policy is not None

    history = repository.list_policies("candidate-synthetic")
    assert [item.policy_version for item in history] == [1, 2]
    assert [item.status for item in history] == [
        PrioritizationPolicyStatus.SUPERSEDED,
        PrioritizationPolicyStatus.ACTIVE,
    ]
    assert second.policy.previous_policy_id == first.policy.policy_id
    assert repository.get_policy(
        "candidate-synthetic",
        first.policy.policy_id,
    ) == history[0]
    assert history[0].raw_preference_text == RAW_POLICY


@pytest.mark.asyncio
async def test_policy_objects_are_frozen_and_approved_draft_cannot_reapply(
    tmp_path: Path,
) -> None:
    service, _, _, repository, _, _ = _service(tmp_path)
    draft = await _create_ready_draft(service)
    first = service.approve_policy(_approval(draft.draft_id))
    assert first.policy is not None

    with pytest.raises(FrozenInstanceError):
        first.policy.raw_preference_text = "mutated"

    repeated = service.approve_policy(_approval(draft.draft_id))
    assert repeated.reason_code is PolicyReason.DRAFT_ALREADY_APPROVED
    assert len(repository.list_policies("candidate-synthetic")) == 1


@pytest.mark.asyncio
async def test_subject_policy_histories_are_isolated(tmp_path: Path) -> None:
    home = PrivateHome(tmp_path / "synthetic-private-home")
    repository = PrivateHomePrioritizationPolicyRepository(home)

    async def approve_subject(subject_id: str, draft_id: str):
        raw = f"Policy for {subject_id}"
        service = PrioritizationPolicyService(
            interpreter=FakeInterpreter(
                _interpretation(
                    subject_id=subject_id,
                    raw_text=raw,
                    hard_constraints=(),
                    soft_preferences=(
                        _soft(
                            preference_id=f"pref-{subject_id}",
                            statement=f"Preference for {subject_id}",
                        ),
                    ),
                )
            ),
            draft_store=InMemoryPrioritizationPolicyDraftStore(),
            repository=repository,
            clock=MutableClock(),
            draft_id_factory=lambda: draft_id,
        )
        created = await service.create_policy_draft(
            CreatePolicyDraftRequest(
                subject_id=subject_id,
                raw_preference_text=raw,
            )
        )
        assert created.draft is not None
        return service.approve_policy(
            _approval(
                created.draft.draft_id,
                subject_id=subject_id,
                raw_text=raw,
                hard_constraints=(),
                soft_preferences=created.draft.soft_preferences,
            )
        ).policy

    first = await approve_subject("subject-one", "draft-one")
    second = await approve_subject("subject-two", "draft-two")

    assert first is not None and second is not None
    assert first.policy_version == second.policy_version == 1
    assert repository.get_active_policy("subject-one") == first
    assert repository.get_active_policy("subject-two") == second
    assert repository.get_policy("subject-one", second.policy_id) is None
    assert len(list(home.paths.prioritization_policies.glob("*.json"))) == 2


def test_policy_module_has_no_downstream_or_execution_dependencies() -> None:
    module_path = (
        Path(__file__).resolve().parents[1]
        / "core"
        / "prioritization_policy.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    forbidden = {
        "core.job_discovery",
        "core.job_search",
        "core.conversational_intake",
        "core.application_engine",
        "utils.tracker",
        "utils.csv_apply",
        "source_connectors",
        "adapters",
        "playwright",
    }
    assert imported.isdisjoint(forbidden)


def test_priority_decision_schema_uses_policy_agent_bindings_not_fixed_scores() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "development_doc"
        / "contracts"
        / "priority-decision.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    required = set(schema["required"])
    assert {
        "job_revision",
        "job_content_hash",
        "policy_id",
        "policy_version",
        "policy_content_hash",
        "candidate_summary_version",
        "candidate_summary_content_hash",
        "agent_version",
        "prompt_version",
        "model_id",
        "validation_version",
    } <= required
    assert {
        "match_score",
        "freshness_score",
        "priority_score",
    }.isdisjoint(schema["properties"])
    assert schema["properties"]["priority_level"]["enum"] == [
        "P0",
        "P1",
        "P2",
        "P3",
        None,
    ]
    assert schema["properties"]["qualification"]["enum"] == [
        "QUALIFIED",
        "EXCLUDED",
        "NEEDS_USER",
    ]
