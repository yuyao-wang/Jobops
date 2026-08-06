"""Sanitized preference-NLP and authenticated Dashboard closure tests."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request

from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.model_provider_capabilities import MODEL_EXECUTION_ISOLATION_PROFILES
from core.prioritization_policy import (
    CreatePolicyDraftRequest,
    HardConstraint,
    HardConstraintType,
    InMemoryPrioritizationPolicyDraftStore,
    PolicyInterpretation,
    PreferenceImportance,
    PrioritizationPolicyService,
    PrivateHomePrioritizationPolicyRepository,
    SoftPreference,
    SoftPreferenceCategory,
)
from core.private_home import PrivateHome
from core.production_prioritization_policy_interpreter import (
    POLICY_INTERPRETER_COMPONENT_ID,
    POLICY_INTERPRETER_OUTPUT_SCHEMA,
    StructuredBackendPrioritizationPolicyInterpreter,
    build_production_prioritization_policy_interpreter,
)
from dashboard.prioritization_policy import PrioritizationPolicyUIController
from dashboard.server import (
    app,
    approve_prioritization_policy_ui,
    create_prioritization_policy_draft_ui,
    read_prioritization_policy_ui,
    revise_prioritization_policy_preferences_ui,
)
from utils import llm


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
SUBJECT = "synthetic-preference-subject"
RAW = (
    "Prefer machine-learning engineer roles in climate technology. "
    "Do not include unpaid roles."
)


class FakeStructuredBackend:
    capabilities = llm.OpenAIAPIBackend.capabilities
    native_capabilities = llm.OpenAIAPIBackend.native_capabilities
    response = {
        "hard_constraints": [],
        "soft_preferences": [],
        "ambiguities": [],
    }
    calls = []

    def __init__(self, config):
        self.model = config.get("model", "")

    async def complete_structured_request(self, request):
        type(self).calls.append(request)
        return copy.deepcopy(type(self).response)


@pytest.mark.asyncio
async def test_production_preference_interpreter_is_one_tool_free_nlp_call() -> None:
    FakeStructuredBackend.calls = []
    FakeStructuredBackend.response = {
        "hard_constraints": [
            {
                "constraint_type": "EXCLUDED_ROLE_PHRASE",
                "normalized_value": "unpaid",
                "source_excerpt": "Do not include unpaid roles.",
            }
        ],
        "soft_preferences": [
            {
                "preference_id": "pref-role-1",
                "category": "ROLE",
                "statement": "Prefer machine-learning engineer roles",
                "importance": "UNSPECIFIED",
                "source_excerpt": "Prefer machine-learning engineer roles",
            },
            {
                "preference_id": "pref-domain-1",
                "category": "DOMAIN",
                "statement": "Prefer climate technology",
                "importance": "UNSPECIFIED",
                "source_excerpt": "in climate technology",
            },
        ],
        "ambiguities": [],
    }
    interpreter = build_production_prioritization_policy_interpreter(
        ai_config={
            "default_backend": "openai_api",
            "backends": {"openai_api": {"model": "synthetic-model"}},
            "components": {"priority_evaluation": "openai_api"},
        },
        backend_registry={"openai_api": FakeStructuredBackend},
        isolation_profile_registry=MODEL_EXECUTION_ISOLATION_PROFILES,
    )

    result = await interpreter.interpret(
        CreatePolicyDraftRequest(
            subject_id=SUBJECT,
            raw_preference_text=RAW,
        )
    )

    assert isinstance(
        interpreter, StructuredBackendPrioritizationPolicyInterpreter
    )
    assert len(FakeStructuredBackend.calls) == 1
    request = FakeStructuredBackend.calls[0]
    assert request.component_id == POLICY_INTERPRETER_COMPONENT_ID
    assert request.output_schema == POLICY_INTERPRETER_OUTPUT_SCHEMA
    assert request.images == ()
    assert request.input_data == {"raw_preference_text": RAW}
    assert SUBJECT not in repr(request.input_data)
    assert result.subject_id == SUBJECT
    assert result.hard_constraints[0].user_confirmed is False


class FakePolicyInterpreter:
    async def interpret(self, request: CreatePolicyDraftRequest):
        return PolicyInterpretation(
            subject_id=request.subject_id,
            raw_preference_text=request.raw_preference_text,
            hard_constraints=(
                HardConstraint(
                    constraint_type=HardConstraintType.EXCLUDED_ROLE_PHRASE,
                    normalized_value="unpaid",
                    source_excerpt="Do not include unpaid roles.",
                    user_confirmed=False,
                ),
            ),
            soft_preferences=(
                SoftPreference(
                    preference_id="pref-role-1",
                    category=SoftPreferenceCategory.ROLE,
                    statement="Prefer machine-learning engineer roles",
                    importance=PreferenceImportance.HIGH,
                    source_excerpt="Prefer machine-learning engineer roles",
                ),
            ),
            ambiguities=(),
            interpreter_version="synthetic-interpreter-v1",
        )


def _context(subject_id: str = SUBJECT) -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id=f"session-{subject_id}-0123456789",
        subject_id=subject_id,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


@pytest.mark.asyncio
async def test_dashboard_interpret_review_approve_ignores_forged_subject(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path / "synthetic-private-home")
    draft_ids = iter(
        ("draft-synthetic-preference", "draft-synthetic-preference-edit")
    )
    service = PrioritizationPolicyService(
        interpreter=FakePolicyInterpreter(),
        draft_store=InMemoryPrioritizationPolicyDraftStore(),
        repository=PrivateHomePrioritizationPolicyRepository(home),
        clock=lambda: NOW,
        draft_id_factory=lambda: next(draft_ids),
    )
    controller = PrioritizationPolicyUIController(service=service)
    app.state.prioritization_policy_controller = controller
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": "/api/prioritization-policy/draft",
            "query_string": b"subject_id=forged-subject",
            "headers": [(b"x-subject-id", b"forged-subject")],
        }
    )

    empty = await read_prioritization_policy_ui(request, _context())
    created = await create_prioritization_policy_draft_ui(
        {
            "subject_id": "forged-subject",
            "raw_preference_text": RAW,
        },
        request,
        _context(),
    )
    unconfirmed = await approve_prioritization_policy_ui(
        {
            "subject_id": "forged-subject",
            "draft_id": created["draft"]["draft_id"],
            "confirm_hard_constraints": False,
        },
        request,
        _context(),
    )
    approved = await approve_prioritization_policy_ui(
        {
            "subject_id": "forged-subject",
            "draft_id": created["draft"]["draft_id"],
            "confirm_hard_constraints": True,
        },
        request,
        _context(),
    )
    active = await read_prioritization_policy_ui(request, _context())
    revised = await revise_prioritization_policy_preferences_ui(
        {
            "subject_id": "forged-subject",
            "expected_policy_version": 1,
            "preferences": [
                {
                    "preference_id": "pref-role-1",
                    "statement": "Machine Learning Engineer",
                    "importance": "HIGH",
                }
            ],
        },
        request,
        _context(),
    )

    assert empty["status"] == "EMPTY"
    assert created["status"] == "NEEDS_USER"
    assert "subject_id" not in created["draft"]
    assert unconfirmed["reason"] == "HARD_CONSTRAINT_NOT_CONFIRMED"
    assert approved["status"] == "SUCCEEDED"
    assert active["status"] == "ACTIVE"
    assert active["policy"]["policy_version"] == 1
    assert revised["status"] == "SUCCEEDED"
    assert revised["policy"]["policy_version"] == 2
    assert revised["policy"]["soft_preferences"][0]["statement"] == (
        "Machine Learning Engineer"
    )
    assert "subject_id" not in active["policy"]
    assert service.get_active_policy("forged-subject") is None
