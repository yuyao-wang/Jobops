"""Recover one formally assembled ApplicationBundle for live Workday execution."""

from __future__ import annotations

from typing import Any

from adapters.workday import workday_external_job_id

from core.application_answers import (
    PreparedApplicationAnswerSetReadStatus,
    PrivateHomePreparedApplicationAnswerSetRepository,
)
from core.application_bundle_assembly import (
    APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION,
    ApplicationBundleAssemblyReadStatus,
    PrivateHomeApplicationBundleAssemblyRepository,
)
from core.application_answer_taxonomy import CanonicalApplicationAnswerKey
from core.bundles import (
    ApplicationBundle,
    application_bundle_canonical_hash,
    canonical_hash,
    file_sha256,
)
from core.private_home import PrivateHome
from core.real_application_control_plane import RealApplicationPreparation
from core.recoverable_application_bundle import (
    PrivateHomeRecoverableApplicationBundleEnvelopeRepository,
    RecoverableApplicationBundleEnvelopeReadStatus,
)


class RealApplicationPreparationError(RuntimeError):
    """Formal preparation is absent, stale, or unsafe for live execution."""


_ELIGIBILITY_KEYS = {
    CanonicalApplicationAnswerKey.WORK_AUTHORIZATION.value,
    CanonicalApplicationAnswerKey.SPONSORSHIP.value,
    CanonicalApplicationAnswerKey.LOCATION.value,
    CanonicalApplicationAnswerKey.RELOCATION.value,
    CanonicalApplicationAnswerKey.SALARY.value,
}


def _answer_bundle(bundle: ApplicationBundle, answer_set: Any) -> dict[str, Any]:
    sources = {
        item.canonical_key.value: {
            "certainty": (
                "VERIFIED"
                if item.answer_source.value == "VERIFIED_FACT"
                else "USER_CONFIRMED"
                if item.answer_source.value == "USER_CONFIRMED"
                else "POLICY_DEFAULT"
            ),
            "source": item.answer_source.value,
            "supporting_fact_ids": list(item.supporting_fact_ids),
        }
        for item in answer_set.answers
    }
    profile = bundle.identity_profile.to_application_bundle_profile()["personal"]
    contact_items = []
    for key, value in profile.items():
        if value is None:
            continue
        contact_items.append(
            {
                "certainty": "VERIFIED_PROFILE",
                "key": key,
                "label": key.replace("_", " ").title(),
                "source": "VERIFIED_APPLICATION_EXECUTION_PROFILE",
                "status": "READY",
                "value": value,
            }
        )

    eligibility: list[dict[str, Any]] = []
    additional: list[dict[str, Any]] = []
    for key, value in bundle.answers.to_dict().items():
        source = sources.get(key, {})
        item = {
            "certainty": source.get("certainty", "FORMAL_BUNDLE"),
            "key": key,
            "label": key.replace("_", " ").title(),
            "source": source.get("source", "FORMAL_APPLICATION_BUNDLE"),
            "status": "READY",
            "supporting_fact_ids": source.get("supporting_fact_ids", []),
            "value": value,
        }
        (eligibility if key in _ELIGIBILITY_KEYS else additional).append(item)
    represented = {item["key"] for item in eligibility}
    unresolved_by_key = {
        item.canonical_key.value: item for item in answer_set.unresolved_items
    }
    for key in (
        CanonicalApplicationAnswerKey.WORK_AUTHORIZATION.value,
        CanonicalApplicationAnswerKey.SPONSORSHIP.value,
        CanonicalApplicationAnswerKey.LOCATION.value,
        CanonicalApplicationAnswerKey.RELOCATION.value,
        CanonicalApplicationAnswerKey.SALARY.value,
    ):
        if key in represented:
            continue
        unresolved_item = unresolved_by_key.get(key)
        eligibility.append(
            {
                "certainty": "UNAVAILABLE",
                "key": key,
                "label": key.replace("_", " ").title(),
                "source": "NO_CONFIRMED_VALUE",
                "status": (
                    "MISSING" if unresolved_item and unresolved_item.blocking
                    else "DISCOVER_AT_RUNTIME"
                ),
                "value": (
                    "No confirmed value; execution stops if Workday requires it"
                ),
            }
        )

    cover = bundle.materials.cover_letter_pdf
    if cover is not None:
        cover_state = "FILE"
        cover_hash = cover.sha256
    elif bundle.materials.cover_letter:
        cover_state = "TEXT"
        cover_hash = bundle.materials.cover_letter_sha256
    else:
        cover_state = "NONE"
        cover_hash = bundle.materials.cover_letter_sha256

    unresolved = [
        {
            "blocking": item.blocking,
            "key": item.canonical_key.value,
            "reason": item.reason.value,
            "required_human_action": item.required_human_action,
        }
        for item in answer_set.unresolved_items
    ]
    return {
        "contract_version": "real-application-answer-review-v1",
        "materials": {
            "cover_letter": {"sha256": cover_hash, "state": cover_state},
            "resume": {
                "sha256": bundle.materials.resume_sha256,
                "state": "FILE",
            },
        },
        "sections": [
            {
                "items": contact_items,
                "key": "contact",
                "label": "Contact information",
            },
            {
                "items": [
                    {
                        "certainty": "PENDING_ATS_READBACK",
                        "key": "employment_history",
                        "label": "Employment history",
                        "source": "RESUME_AUTOFILL_OR_ATS_REVIEW",
                        "status": "DISCOVER_AT_RUNTIME",
                        "value": "Will be shown from exact Workday Review readback",
                    }
                ],
                "key": "employment",
                "label": "Employment history",
            },
            {
                "items": [
                    {
                        "certainty": "PENDING_ATS_READBACK",
                        "key": "education_history",
                        "label": "Education history",
                        "source": "RESUME_AUTOFILL_OR_ATS_REVIEW",
                        "status": "DISCOVER_AT_RUNTIME",
                        "value": "Will be shown from exact Workday Review readback",
                    }
                ],
                "key": "education",
                "label": "Education history",
            },
            {
                "items": eligibility,
                "key": "eligibility",
                "label": "Work authorization, sponsorship, location, relocation and salary",
            },
            {
                "items": additional,
                "key": "additional_required",
                "label": "Other prepared answers",
            },
        ],
        "unresolved": unresolved,
    }


def load_formal_real_application(
    *,
    subject_id: str,
    assembly_record_id: str,
    home: PrivateHome | None = None,
) -> tuple[RealApplicationPreparation, ApplicationBundle]:
    """Load and revalidate the formal assembly/envelope/answer-set chain."""

    private_home = home or PrivateHome.discover()
    assembly_read = PrivateHomeApplicationBundleAssemblyRepository(
        private_home
    ).get(subject_id=subject_id, record_id=assembly_record_id)
    if (
        assembly_read.status is not ApplicationBundleAssemblyReadStatus.FOUND
        or assembly_read.record is None
    ):
        raise RealApplicationPreparationError(
            "formal ApplicationBundle assembly was not found or failed integrity"
        )
    assembly = assembly_read.record
    if (
        assembly.contract_version != APPLICATION_BUNDLE_ASSEMBLY_CONTRACT_VERSION
        or not assembly.verified_profile_hash
    ):
        raise RealApplicationPreparationError(
            "live execution requires a current verified-profile assembly"
        )
    envelope_read = PrivateHomeRecoverableApplicationBundleEnvelopeRepository(
        private_home
    ).get_for_assembly(
        subject_id=subject_id, assembly_record_id=assembly_record_id
    )
    if (
        envelope_read.status
        is not RecoverableApplicationBundleEnvelopeReadStatus.FOUND
        or envelope_read.envelope is None
    ):
        raise RealApplicationPreparationError(
            "recoverable ApplicationBundle envelope is unavailable or corrupt"
        )
    envelope = envelope_read.envelope
    bundle = envelope.bundle
    answer_read = PrivateHomePreparedApplicationAnswerSetRepository(
        private_home
    ).get(subject_id=subject_id, answer_set_id=assembly.answer_set_id)
    if (
        answer_read.status is not PreparedApplicationAnswerSetReadStatus.FOUND
        or answer_read.answer_set is None
        or answer_read.answer_set.answer_set_content_hash
        != assembly.answer_set_content_hash
    ):
        raise RealApplicationPreparationError(
            "prepared answer set is unavailable, stale, or corrupt"
        )
    if (
        envelope.assembly_record_content_hash != assembly.record_content_hash
        or envelope.bundle_canonical_hash
        != assembly.application_bundle_canonical_hash
        or application_bundle_canonical_hash(bundle)
        != assembly.application_bundle_canonical_hash
        or bundle.run_id != assembly.application_bundle_run_id
        or bundle.job.job_id != assembly.job_id
    ):
        raise RealApplicationPreparationError(
            "formal assembly and recoverable bundle bindings differ"
        )
    if (
        bundle.materials.resume_path.is_symlink()
        or not bundle.materials.resume_path.is_file()
        or file_sha256(bundle.materials.resume_path)
        != bundle.materials.resume_sha256
    ):
        raise RealApplicationPreparationError("selected resume bytes changed")
    if bundle.materials.cover_letter_pdf is not None:
        cover_path = private_home.contained_path(
            bundle.materials.cover_letter_pdf.reference
        )
        if (
            cover_path.is_symlink()
            or not cover_path.is_file()
            or file_sha256(cover_path) != bundle.materials.cover_letter_pdf.sha256
        ):
            raise RealApplicationPreparationError(
                "selected cover-letter bytes changed"
            )

    answer_bundle = _answer_bundle(bundle, answer_read.answer_set)
    cover_hash = (
        bundle.materials.cover_letter_pdf.sha256
        if bundle.materials.cover_letter_pdf is not None
        else bundle.materials.cover_letter_sha256
    )
    external_job_id = workday_external_job_id(bundle.job.url)
    if not external_job_id:
        raise RealApplicationPreparationError(
            "Workday external job ID is not explicit in the canonical job URL"
        )
    preparation = RealApplicationPreparation(
        answer_bundle=answer_bundle,
        answer_bundle_hash=canonical_hash(answer_bundle),
        answer_hash=bundle.answer_hash,
        application_plan_id=assembly.application_plan_id,
        assembly_record_content_hash=assembly.record_content_hash,
        assembly_record_id=assembly.record_id,
        attempt_id=bundle.run_id,
        bundle_canonical_hash=assembly.application_bundle_canonical_hash,
        canonical_job_url=bundle.job.url,
        company=bundle.job.company,
        cover_letter_sha256=cover_hash,
        external_job_id=external_job_id,
        job_id=bundle.job.job_id,
        material_hash=bundle.materials.digest,
        policy_hash=bundle.policy.policy_hash,
        profile_snapshot_hash=assembly.verified_profile_hash,
        provider="workday",
        resume_sha256=bundle.materials.resume_sha256,
        subject_id=subject_id,
        title=bundle.job.title,
    )
    return preparation, bundle


__all__ = [
    "RealApplicationPreparationError",
    "load_formal_real_application",
]
