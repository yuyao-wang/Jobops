"""Pure production construction of the existing execution ApplicationBundle."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .application_answer_taxonomy import (
    CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION,
    CanonicalApplicationAnswers,
)
from .application_assembly_execution_context import (
    APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION,
)
from .application_bundle_assembly import (
    ApplicationBundleFactoryRequest,
)
from .application_plan import ApplicationPlan
from .application_execution_profile import (
    APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY,
    ApplicationExecutionIdentityFieldRequiredness,
    ApplicationExecutionIdentityProfile,
)
from .bundles import (
    ApplicationBundle,
    JobSpec,
    MaterialBundle,
    normalized_job_url,
)
from .plan_execution_policy import (
    PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION,
    PlanExecutionPolicyDecisionRecord,
    plan_execution_policy_plan_binding_hash,
    plan_execution_policy_record_hash,
)
from .policy import PolicyDecision
from .job_discovery import JobPosting
from .verified_application_execution_profile import (
    VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION,
    VerifiedApplicationExecutionProfile,
    to_application_execution_identity_profile,
    verified_execution_profile_plan_binding_hash,
)


PRODUCTION_APPLICATION_BUNDLE_FACTORY_CONTRACT_VERSION = (
    "production-application-bundle-factory-v1"
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


class ProductionApplicationBundleFactoryFailureReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    SUBJECT_BINDING_MISMATCH = "SUBJECT_BINDING_MISMATCH"
    PLAN_JOB_BINDING_MISMATCH = "PLAN_JOB_BINDING_MISMATCH"
    EXECUTION_CONTEXT_BINDING_MISMATCH = (
        "EXECUTION_CONTEXT_BINDING_MISMATCH"
    )
    PROFILE_INCOMPLETE = "PROFILE_INCOMPLETE"
    MATERIAL_CONTRACT_INVALID = "MATERIAL_CONTRACT_INVALID"
    ANSWER_CONTRACT_INVALID = "ANSWER_CONTRACT_INVALID"
    POLICY_CONTRACT_INVALID = "POLICY_CONTRACT_INVALID"


class ProductionApplicationBundleFactoryError(ValueError):
    """Bounded construction failure that never includes candidate values."""

    def __init__(
        self, reason: ProductionApplicationBundleFactoryFailureReason
    ) -> None:
        self.reason = ProductionApplicationBundleFactoryFailureReason(reason)
        self.contract_version = (
            PRODUCTION_APPLICATION_BUNDLE_FACTORY_CONTRACT_VERSION
        )
        super().__init__(
            f"{self.contract_version}:{self.reason.value}"
        )


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _subject_key(subject_id: str) -> str:
    return "subject-" + hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


def _fail(
    reason: ProductionApplicationBundleFactoryFailureReason,
) -> None:
    raise ProductionApplicationBundleFactoryError(reason)


def _validate_profile(
    request: ApplicationBundleFactoryRequest,
) -> Mapping[str, object]:
    profile = request.identity_profile
    snapshot = request.verified_profile_ref
    if (
        not isinstance(profile, ApplicationExecutionIdentityProfile)
        or snapshot.profile_contract_version
        != VERIFIED_APPLICATION_EXECUTION_PROFILE_CONTRACT_VERSION
        or snapshot.subject_id != request.subject_id
        or snapshot.application_plan_id != request.application_plan.plan_id
        or snapshot.job_id != request.application_plan.job_id
        or snapshot.plan_binding_hash
        != verified_execution_profile_plan_binding_hash(
            request.application_plan
        )
    ):
        _fail(
            ProductionApplicationBundleFactoryFailureReason
            .EXECUTION_CONTEXT_BINDING_MISMATCH
        )
    try:
        projected = to_application_execution_identity_profile(snapshot)
    except (TypeError, ValueError):
        _fail(
            ProductionApplicationBundleFactoryFailureReason
            .EXECUTION_CONTEXT_BINDING_MISMATCH
        )
    if projected != profile:
        _fail(
            ProductionApplicationBundleFactoryFailureReason
            .EXECUTION_CONTEXT_BINDING_MISMATCH
        )
    required = tuple(
        definition.field_key
        for definition in (
            APPLICATION_EXECUTION_IDENTITY_FIELD_DEFINITION_BY_KEY.values()
        )
        if definition.requiredness
        is ApplicationExecutionIdentityFieldRequiredness
        .REQUIRED_FOR_EXECUTION
    )
    if any(profile.value_for(key) is None for key in required):
        _fail(
            ProductionApplicationBundleFactoryFailureReason
            .PROFILE_INCOMPLETE
        )
    return profile.to_application_bundle_profile()


def _validate_policy(request: ApplicationBundleFactoryRequest) -> None:
    policy = request.policy_decision
    record = request.execution_policy_ref
    if (
        not isinstance(policy, PolicyDecision)
        or record.record_contract_version
        != PLAN_EXECUTION_POLICY_RECORD_CONTRACT_VERSION
        or record.subject_id != request.subject_id
        or record.application_plan_id != request.application_plan.plan_id
        or record.job_id != request.application_plan.job_id
        or record.plan_binding_hash
        != plan_execution_policy_plan_binding_hash(
            request.application_plan
        )
        or record.policy_decision != policy
    ):
        _fail(
            ProductionApplicationBundleFactoryFailureReason
            .POLICY_CONTRACT_INVALID
        )


def _validate_context(request: ApplicationBundleFactoryRequest) -> None:
    snapshot = request.verified_profile_ref
    record = request.execution_policy_ref
    try:
        record_hash = plan_execution_policy_record_hash(record)
    except (TypeError, ValueError):
        _fail(
            ProductionApplicationBundleFactoryFailureReason
            .EXECUTION_CONTEXT_BINDING_MISMATCH
        )
    binding = {
        "application_plan_id": request.application_plan.plan_id,
        "context_contract_version": (
            APPLICATION_ASSEMBLY_EXECUTION_CONTEXT_CONTRACT_VERSION
        ),
        "execution_policy_record_hash": record_hash,
        "execution_policy_record_id": record.record_id,
        "execution_policy_record_version": record.record_contract_version,
        "job_id": request.application_plan.job_id,
        "subject_id": request.subject_id,
        "verified_profile_hash": snapshot.profile_snapshot_hash,
        "verified_profile_id": snapshot.profile_snapshot_id,
        "verified_profile_version": snapshot.profile_contract_version,
    }
    if (
        not isinstance(request.execution_context_binding_hash, str)
        or _HASH_RE.fullmatch(
            request.execution_context_binding_hash
        ) is None
        or request.execution_context_binding_hash
        != _canonical_hash(binding)
    ):
        _fail(
            ProductionApplicationBundleFactoryFailureReason
            .EXECUTION_CONTEXT_BINDING_MISMATCH
        )


def _validate_materials(request: ApplicationBundleFactoryRequest) -> None:
    materials = request.materials
    if (
        not isinstance(materials, MaterialBundle)
        or not isinstance(materials.resume_path, Path)
        or not isinstance(materials.resume_sha256, str)
        or _HASH_RE.fullmatch(materials.resume_sha256) is None
        or _subject_key(request.subject_id)
        not in materials.resume_path.parts
    ):
        _fail(
            ProductionApplicationBundleFactoryFailureReason
            .MATERIAL_CONTRACT_INVALID
        )


class ProductionApplicationBundleFactory:
    """Deterministically map exact P2c1 inputs to the existing Bundle."""

    contract_version = PRODUCTION_APPLICATION_BUNDLE_FACTORY_CONTRACT_VERSION

    def create(
        self, request: ApplicationBundleFactoryRequest
    ) -> ApplicationBundle:
        if not isinstance(request, ApplicationBundleFactoryRequest):
            _fail(
                ProductionApplicationBundleFactoryFailureReason
                .INVALID_REQUEST
            )
        plan = request.application_plan
        posting = request.job_posting
        if (
            not isinstance(plan, ApplicationPlan)
            or not isinstance(posting, JobPosting)
            or not isinstance(
                request.verified_profile_ref,
                VerifiedApplicationExecutionProfile,
            )
            or not isinstance(
                request.execution_policy_ref,
                PlanExecutionPolicyDecisionRecord,
            )
        ):
            _fail(
                ProductionApplicationBundleFactoryFailureReason
                .INVALID_REQUEST
            )
        if (
            request.subject_id != plan.subject_id
            or request.verified_profile_ref.subject_id != request.subject_id
            or request.execution_policy_ref.subject_id != request.subject_id
        ):
            _fail(
                ProductionApplicationBundleFactoryFailureReason
                .SUBJECT_BINDING_MISMATCH
            )
        if (
            posting.job_id != plan.job_id
            or posting.revision != plan.job_revision
            or posting.content_hash != plan.job_content_hash
        ):
            _fail(
                ProductionApplicationBundleFactoryFailureReason
                .PLAN_JOB_BINDING_MISMATCH
            )
        _validate_context(request)
        profile = _validate_profile(request)
        _validate_policy(request)
        _validate_materials(request)
        if (
            not isinstance(request.answers, CanonicalApplicationAnswers)
            or request.answers.taxonomy_version
            != CANONICAL_APPLICATION_ANSWER_TAXONOMY_VERSION
        ):
            _fail(
                ProductionApplicationBundleFactoryFailureReason
                .ANSWER_CONTRACT_INVALID
            )
        try:
            url = normalized_job_url(
                posting.application_url or posting.source_url
            )
        except ValueError:
            _fail(
                ProductionApplicationBundleFactoryFailureReason
                .PLAN_JOB_BINDING_MISMATCH
            )
        return ApplicationBundle(
            run_id=request.run_id,
            job=JobSpec(
                url=url,
                company=posting.company,
                title=posting.title,
                tier=request.policy_decision.tier,
                job_id=posting.job_id,
            ),
            materials=request.materials,
            profile={"personal": dict(profile["personal"])},
            answers=request.answers,
            policy=request.policy_decision,
        )


def build_production_application_bundle_factory(
) -> ProductionApplicationBundleFactory:
    """Return the stateless production factory for composition roots."""

    return ProductionApplicationBundleFactory()


__all__ = [
    "PRODUCTION_APPLICATION_BUNDLE_FACTORY_CONTRACT_VERSION",
    "ProductionApplicationBundleFactory",
    "ProductionApplicationBundleFactoryError",
    "ProductionApplicationBundleFactoryFailureReason",
    "build_production_application_bundle_factory",
]
