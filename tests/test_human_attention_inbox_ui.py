"""Focused S3f read-only Human Attention Inbox UI tests."""

from __future__ import annotations

import ast
import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from starlette.requests import Request

from core.application_preparation_orchestrator import (
    ApplicationPreparationStage,
)
from core.authenticated_subject import (
    AuthenticatedSubjectContext,
    AuthenticationMethod,
)
from core.human_attention_queue import (
    HumanAttentionAudience,
    HumanAttentionKind,
    HumanAttentionQueueFailureReason,
    HumanAttentionQueueItem,
    HumanAttentionQueueResult,
    HumanAttentionQueueStatus,
    HumanAttentionResolutionCapability,
)
from core.job_prioritization import ProposedPriorityLevel
from dashboard.human_attention_inbox import (
    HumanAttentionInboxUIController,
    HumanAttentionInboxUIStatus,
    map_human_attention_queue,
)
from dashboard.server import app, human_attention_inbox_ui
from tests.test_application_plan import NOW, SUBJECT


def _raw(cls, **values):
    instance = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(instance, name, value)
    return instance


def _context() -> AuthenticatedSubjectContext:
    return AuthenticatedSubjectContext(
        session_id="session_reference_0123456789abcdef",
        subject_id=SUBJECT,
        authentication_method=AuthenticationMethod.LOCAL_KEYCHAIN_SESSION,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
    )


def _item(
    suffix: str,
    *,
    audience: HumanAttentionAudience,
    kind: HumanAttentionKind,
    plan_id: str = "plan-1",
    action: str = "Review the typed application input.",
) -> HumanAttentionQueueItem:
    capability = {
        HumanAttentionKind.USER_FACT_REQUIRED: (
            HumanAttentionResolutionCapability.PROVIDE_FACT
        ),
        HumanAttentionKind.USER_CHOICE_REQUIRED: (
            HumanAttentionResolutionCapability.MAKE_CHOICE
        ),
        HumanAttentionKind.USER_ATTESTATION_REQUIRED: (
            HumanAttentionResolutionCapability.ATTEST
        ),
    }.get(
        kind,
        HumanAttentionResolutionCapability.OPERATOR_REPAIR,
    )
    return _raw(
        HumanAttentionQueueItem,
        item_id=f"human-attention-item-{'a' * 63}{suffix}",
        application_plan_id=plan_id,
        job_id=f"job-{suffix}",
        priority=ProposedPriorityLevel.P1,
        audience=audience,
        attention_kind=kind,
        resolution_capability=capability,
        required_action=action,
        blocking=True,
        source_stage=ApplicationPreparationStage.APPLICATION_ANSWERS,
        canonical_answer_key=None,
    )


def _queue(
    items: tuple[HumanAttentionQueueItem, ...],
) -> HumanAttentionQueueResult:
    return _raw(
        HumanAttentionQueueResult,
        status=HumanAttentionQueueStatus.SUCCEEDED,
        subject_id=SUBJECT,
        items=items,
        item_count=len(items),
        user_item_count=sum(
            item.audience is HumanAttentionAudience.USER for item in items
        ),
        operator_item_count=sum(
            item.audience is HumanAttentionAudience.OPERATOR
            for item in items
        ),
        affected_plan_count=len(
            {item.application_plan_id for item in items}
        ),
        evaluated_at=NOW,
        reason_code=None,
    )


@pytest.mark.asyncio
async def test_authenticated_load_calls_p2b5_once_and_preserves_group_order(
) -> None:
    items = (
        _item(
            "1",
            audience=HumanAttentionAudience.USER,
            kind=HumanAttentionKind.USER_ATTESTATION_REQUIRED,
        ),
        _item(
            "2",
            audience=HumanAttentionAudience.USER,
            kind=HumanAttentionKind.USER_FACT_REQUIRED,
            plan_id="plan-2",
        ),
        _item(
            "3",
            audience=HumanAttentionAudience.OPERATOR,
            kind=HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
            plan_id="plan-2",
        ),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def reader(*, subject_id, now):
        calls.append((subject_id, now))
        started.set()
        await release.wait()
        return _queue(items)

    controller = HumanAttentionInboxUIController(
        queue_reader=reader, clock=lambda: NOW
    )
    first = asyncio.create_task(controller.load(context=_context()))
    await started.wait()
    duplicate = asyncio.create_task(controller.load(context=_context()))
    release.set()
    first_result, duplicate_result = await asyncio.gather(first, duplicate)

    assert first_result.status is HumanAttentionInboxUIStatus.READY
    assert duplicate_result == first_result
    assert calls == [(SUBJECT, NOW)]
    assert [item.job_id for item in first_result.user_items] == [
        "job-1",
        "job-2",
    ]
    assert [item.job_id for item in first_result.operator_items] == ["job-3"]
    assert first_result.affected_plan_count == 2
    assert first_result.user_items[0].attention_label == "Attestation required"
    assert first_result.user_items[0].resolution_capability == "ATTEST"
    assert first_result.operator_items[0].attention_label == "Operator action required"


def test_empty_failure_and_unsafe_action_are_displayed_safely() -> None:
    empty = map_human_attention_queue(
        _queue(()), subject_id=SUBJECT, now=NOW
    )
    unsafe = map_human_attention_queue(
        _queue(
            (
                _item(
                    "4",
                    audience=HumanAttentionAudience.OPERATOR,
                    kind=HumanAttentionKind.SYSTEM_OPERATOR_REQUIRED,
                    action=(
                        "Inspect /Users/private/secret token=credential."
                    ),
                ),
            )
        ),
        subject_id=SUBJECT,
        now=NOW,
    )
    failed_queue = _raw(
        HumanAttentionQueueResult,
        status=HumanAttentionQueueStatus.FAILED,
        subject_id=SUBJECT,
        items=(),
        item_count=0,
        user_item_count=0,
        operator_item_count=0,
        affected_plan_count=0,
        evaluated_at=NOW,
        reason_code=(
            HumanAttentionQueueFailureReason
            .ANSWER_SET_INTEGRITY_FAILURE
        ),
        message=(
            "Traceback /private/secret bearer full-sensitive-token"
        ),
    )
    failed = map_human_attention_queue(
        failed_queue, subject_id=SUBJECT, now=NOW
    )

    assert empty.status is HumanAttentionInboxUIStatus.EMPTY
    assert empty.message == "There are no items that need your attention."
    assert unsafe.status is HumanAttentionInboxUIStatus.READY
    assert "/Users/" not in repr(unsafe.to_dict())
    assert "credential" not in repr(unsafe.to_dict()).casefold()
    assert failed.status is HumanAttentionInboxUIStatus.FAILED
    serialized = repr(failed.to_dict())
    assert "Traceback" not in serialized
    assert "/private/" not in serialized
    assert "bearer" not in serialized.casefold()


@pytest.mark.asyncio
async def test_route_and_ui_are_read_only_with_one_post_cycle_refresh() -> None:
    calls = []

    def reader(*, subject_id, now):
        calls.append((subject_id, now))
        return _queue(())

    app.state.human_attention_inbox_controller = (
        HumanAttentionInboxUIController(
            queue_reader=reader, clock=lambda: NOW
        )
    )
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "GET",
            "path": "/api/human-attention-inbox",
            "headers": [],
            "query_string": b"subject_id=subject-attacker",
        }
    )
    result = await human_attention_inbox_ui(request, _context())

    assert result["status"] == "EMPTY"
    assert calls == [(SUBJECT, NOW)]

    root = Path(__file__).parents[1]
    source = (root / "dashboard/human_attention_inbox.py").read_text()
    imports = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imports.intersection(
        {
            "core.application_preparation_orchestrator",
            "core.application_plan",
            "core.application_answers",
            "core.automation_cycle",
            "core.application_engine",
            "core.private_home",
        }
    )
    assert ".save(" not in source
    javascript = (root / "dashboard/static/app.js").read_text()
    load_block = javascript[
        javascript.index("async function loadDashboard()"):
        javascript.index("function setHeader")
    ]
    automation_block = javascript[
        javascript.index("async function runAutomation()"):
        javascript.index("function updateRunningButtons")
    ]
    assert load_block.count('getJson("/api/human-attention-inbox")') == 1
    assert automation_block.count("await loadDashboard()") == 1
    assert "setInterval(" not in javascript
    template = (root / "dashboard/templates/index.html").read_text()
    assert "Needs your attention" in template
    assert "Items waiting for you" in template
    # S3f's reader remains read-only; S3g1 owns the separate typed write path.
    assert "resolveHumanAttention" not in source
    assert 'data-attention-id=' in javascript
