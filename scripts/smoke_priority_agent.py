#!/usr/bin/env python3
"""Run one opt-in real Priority Agent call with synthetic data only."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.job_discovery import JobPosting
from core.job_prioritization import (
    CandidateFact,
    CandidateFactCategory,
    CreatePriorityProposalRequest,
    build_candidate_summary,
    create_priority_proposal,
)
from core.prioritization_policy import (
    HardConstraint,
    HardConstraintType,
    PreferenceImportance,
    PrioritizationPolicy,
    PrioritizationPolicyStatus,
    SoftPreference,
    SoftPreferenceCategory,
    default_preparation_admission_policy,
    policy_content_hash,
)
from core.priority_agent_adapter import OpenAIPriorityAgentAdapter
from utils.llm import OpenAIAPIBackend


SUBJECT_ID = "synthetic-smoke-subject"


def _request(now: datetime) -> CreatePriorityProposalRequest:
    hard_constraints = (
        HardConstraint(
            constraint_type=HardConstraintType.EXCLUDED_COUNTRY,
            normalized_value="united states",
            source_excerpt="Do not apply to jobs in the United States.",
            user_confirmed=True,
        ),
    )
    soft_preferences = (
        SoftPreference(
            preference_id="synthetic-preference-environmental-ai",
            category=SoftPreferenceCategory.DOMAIN,
            statement="Prefer environmental AI and remote-sensing roles.",
            source_excerpt="Environmental AI is a high priority.",
            importance=PreferenceImportance.HIGH,
        ),
    )
    raw_policy = (
        "Prefer recent environmental AI and remote-sensing roles. "
        "Do not apply to jobs in the United States."
    )
    preparation_admission = default_preparation_admission_policy()
    policy = PrioritizationPolicy(
        policy_id="synthetic-prioritization-policy-v1",
        subject_id=SUBJECT_ID,
        policy_version=1,
        policy_content_hash=policy_content_hash(
            raw_preference_text=raw_policy,
            hard_constraints=hard_constraints,
            soft_preferences=soft_preferences,
            preparation_admission=preparation_admission,
        ),
        raw_preference_text=raw_policy,
        hard_constraints=hard_constraints,
        soft_preferences=soft_preferences,
        preparation_admission=preparation_admission,
        status=PrioritizationPolicyStatus.ACTIVE,
        created_at=now - timedelta(days=2),
        approved_at=now - timedelta(days=1),
        interpreter_version="synthetic-smoke-interpreter-v1",
    )
    candidate_summary = build_candidate_summary(
        subject_id=SUBJECT_ID,
        candidate_summary_version="synthetic-summary-v1",
        facts=(
            CandidateFact(
                fact_id="synthetic-fact-environmental-ml",
                category=CandidateFactCategory.DOMAIN,
                statement=(
                    "Has verified synthetic project experience in "
                    "environmental machine learning."
                ),
                source="synthetic-smoke-fixture",
                verified=True,
                prioritization_safe=True,
                confirmed_at=now - timedelta(days=7),
            ),
        ),
        created_at=now - timedelta(minutes=5),
    )
    job = JobPosting(
        schema_version="1.0",
        job_id="synthetic-smoke-job",
        revision=1,
        source_platform="generic_web",
        source_job_id="synthetic-source-job",
        source_url="https://example.test/jobs/environmental-ml",
        company="Synthetic Earth Systems",
        title="Machine Learning Engineer",
        location="Vancouver, Canada",
        work_mode="HYBRID",
        posted_at=(
            (now - timedelta(days=1))
            .isoformat()
            .replace("+00:00", "Z")
        ),
        observed_at=now.isoformat().replace("+00:00", "Z"),
        application_url=None,
        ats_type="unknown",
        description=(
            "Build synthetic geospatial machine-learning systems for "
            "environmental monitoring."
        ),
        content_hash="a" * 64,
        status="NORMALIZED",
    )
    return CreatePriorityProposalRequest(
        request_id="synthetic-smoke-priority-request",
        subject_id=SUBJECT_ID,
        job_posting=job,
        policy=policy,
        candidate_summary=candidate_summary,
        now=now,
    )


async def _main() -> int:
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get(
        "OPENAI_MODEL"
    ):
        print(
            "Skipped: set OPENAI_API_KEY and OPENAI_MODEL explicitly "
            "to run one synthetic model call."
        )
        return 2

    backend = OpenAIAPIBackend(
        {
            "api_key_env": "OPENAI_API_KEY",
            "model": os.environ["OPENAI_MODEL"],
            "store": False,
        }
    )
    adapter = OpenAIPriorityAgentAdapter(backend)
    result = await create_priority_proposal(
        _request(datetime.now(timezone.utc)),
        agent=adapter,
        metadata=adapter.metadata,
        proposal_id_factory=lambda: "synthetic-smoke-priority-proposal",
    )
    proposal = result.proposal
    print(
        json.dumps(
            {
                "status": result.status.value,
                "reason": (
                    result.reason_code.value if result.reason_code else None
                ),
                "retryable": result.retryable,
                "proposal": (
                    {
                        "proposal_id": proposal.proposal_id,
                        "job_id": proposal.job_id,
                        "qualification": (
                            proposal.proposed_qualification.value
                        ),
                        "priority_level": (
                            proposal.proposed_priority_level.value
                            if proposal.proposed_priority_level
                            else None
                        ),
                        "confidence": proposal.confidence.value,
                        "summary": proposal.summary,
                        "positive_signal_count": len(
                            proposal.positive_signals
                        ),
                        "concern_count": len(proposal.concerns),
                    }
                    if proposal
                    else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if proposal is not None else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
