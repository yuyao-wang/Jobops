"""Focused S3c SearchProfile auto-application policy tests."""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from core.accepted_job_intent import (
    AcceptedJobIntentReadStatus,
    PrivateHomeAcceptedJobIntentRepository,
)
from core.job_library_refresh import (
    CandidateIntentStatus,
    JobLibraryRefreshStatus,
    PrivateHomeJobLibraryRefreshRunRepository,
    refresh_job_library,
)
from core.private_home import PrivateHome
from core.search_profile import PrivateHomeSearchProfileRepository
from core.search_profile_intent_policy import (
    PrivateHomeSearchProfileIntentPolicyRepository,
    SaveSearchProfileIntentPolicyCommand,
    SaveSearchProfileIntentPolicyReason,
    SaveSearchProfileIntentPolicyStatus,
    SearchProfileIntentMode,
    SearchProfileIntentPolicyReadStatus,
    save_search_profile_intent_policy,
)
from tests.test_application_plan import NOW, SUBJECT
from tests.test_job_library_refresh import (
    _Discovery,
    _Priority,
    _ProfileProvider,
    _Reader,
    _SearchExecutor,
    _candidate,
    _command,
    _profile,
    _search_result,
)


def _save_policy(
    *,
    home: PrivateHome,
    profile_id: str,
    mode: SearchProfileIntentMode,
    enabled: bool = True,
    now=NOW,
    subject_id: str = SUBJECT,
):
    return save_search_profile_intent_policy(
        SaveSearchProfileIntentPolicyCommand(
            subject_id=subject_id,
            search_profile_id=profile_id,
            intent_mode=mode,
            enabled=enabled,
            now=now,
        ),
        search_profile_repository=PrivateHomeSearchProfileRepository(home),
        policy_repository=(
            PrivateHomeSearchProfileIntentPolicyRepository(home)
        ),
    )


async def _refresh(
    *,
    home: PrivateHome,
    profiles,
    urls,
    invocation: str,
    discovery=None,
):
    search = {
        profile.profile_id: _search_result(
            profile,
            (_candidate(profile, f"candidate-{index}", url),),
        )
        for index, (profile, url) in enumerate(zip(profiles, urls), start=1)
    }
    return await refresh_job_library(
        _command(invocation),
        profile_provider=_ProfileProvider(
            PrivateHomeSearchProfileRepository(home)
        ),
        search_executor=_SearchExecutor(search),
        public_job_reader=_Reader(),
        discovery=discovery or _Discovery(),
        priority_refresh=_Priority(),
        repository=PrivateHomeJobLibraryRefreshRunRepository(home),
        intent_policy_provider=(
            PrivateHomeSearchProfileIntentPolicyRepository(home)
        ),
        accepted_intent_repository=(
            PrivateHomeAcceptedJobIntentRepository(home)
        ),
    )


@pytest.mark.asyncio
async def test_missing_policy_defaults_add_only_and_writes_no_intent(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    profile = _profile(home, "Default", "default")
    url = "https://job-boards.greenhouse.io/example/jobs/default"

    result = await _refresh(
        home=home,
        profiles=(profile,),
        urls=(url,),
        invocation="refresh-default-policy",
    )

    assert result.status is JobLibraryRefreshStatus.COMPLETED
    assert (
        result.run.candidate_results[0].intent_status
        is CandidateIntentStatus.ADD_JOB_ONLY
    )
    assert (
        PrivateHomeAcceptedJobIntentRepository(home)
        .get_current(subject_id=SUBJECT, job_id="job-1")
        .status
        is AcceptedJobIntentReadStatus.NOT_FOUND
    )


@pytest.mark.asyncio
async def test_explicit_auto_writes_only_after_successful_discovery(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    profile = _profile(home, "Auto", "auto")
    created = _save_policy(
        home=home,
        profile_id=profile.profile_id,
        mode=SearchProfileIntentMode.AUTO_REQUEST_APPLICATION,
    )
    success_url = "https://job-boards.greenhouse.io/example/jobs/auto"

    successful = await _refresh(
        home=home,
        profiles=(profile,),
        urls=(success_url,),
        invocation="refresh-auto-success",
    )
    record_paths = tuple(
        home.paths.accepted_job_intents.rglob("*.json")
    )
    failed_url = "https://job-boards.greenhouse.io/example/jobs/rejected"
    failed = await _refresh(
        home=home,
        profiles=(profile,),
        urls=(failed_url,),
        invocation="refresh-auto-failed",
        discovery=_Discovery(failed_urls=(failed_url,)),
    )

    assert created.status is SaveSearchProfileIntentPolicyStatus.CREATED
    assert (
        successful.run.candidate_results[0].intent_status
        is CandidateIntentStatus.CREATED
    )
    assert failed.run.candidate_results[0].accepted_job_intent_id is None
    assert tuple(home.paths.accepted_job_intents.rglob("*.json")) == record_paths


@pytest.mark.asyncio
async def test_any_auto_source_writes_once_with_all_profile_provenance(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    auto = _profile(home, "Auto Source", "auto-source")
    add_only = _profile(home, "Library Source", "library-source")
    _save_policy(
        home=home,
        profile_id=auto.profile_id,
        mode=SearchProfileIntentMode.AUTO_REQUEST_APPLICATION,
    )
    url = "https://job-boards.greenhouse.io/example/jobs/shared"

    result = await _refresh(
        home=home,
        profiles=(auto, add_only),
        urls=(url, f"{url}#duplicate"),
        invocation="refresh-shared",
    )
    current = PrivateHomeAcceptedJobIntentRepository(home).get_current(
        subject_id=SUBJECT, job_id="job-1"
    )
    _save_policy(
        home=home,
        profile_id=auto.profile_id,
        mode=SearchProfileIntentMode.ADD_JOB_ONLY,
        now=NOW + timedelta(minutes=1),
    )
    later = await _refresh(
        home=home,
        profiles=(auto, add_only),
        urls=(url, f"{url}#duplicate"),
        invocation="refresh-shared-add-only",
    )

    assert (
        result.run.candidate_results[0].intent_status
        is CandidateIntentStatus.CREATED
    )
    assert current.status is AcceptedJobIntentReadStatus.FOUND
    assert current.intent.provenance.source_profile_ids == tuple(
        sorted((auto.profile_id, add_only.profile_id))
    )
    assert (
        later.run.candidate_results[0].intent_status
        is CandidateIntentStatus.ADD_JOB_ONLY
    )
    assert (
        PrivateHomeAcceptedJobIntentRepository(home)
        .get_current(subject_id=SUBJECT, job_id="job-1")
        .intent
        == current.intent
    )


def test_policy_versions_replay_subject_isolation_and_dependency_boundary(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    profile = _profile(home, "Versioned", "versioned")
    first = _save_policy(
        home=home,
        profile_id=profile.profile_id,
        mode=SearchProfileIntentMode.AUTO_REQUEST_APPLICATION,
    )
    replay = _save_policy(
        home=home,
        profile_id=profile.profile_id,
        mode=SearchProfileIntentMode.AUTO_REQUEST_APPLICATION,
        now=NOW + timedelta(minutes=1),
    )
    changed = _save_policy(
        home=home,
        profile_id=profile.profile_id,
        mode=SearchProfileIntentMode.ADD_JOB_ONLY,
        now=NOW + timedelta(minutes=2),
    )
    wrong_subject = _save_policy(
        home=home,
        profile_id=profile.profile_id,
        mode=SearchProfileIntentMode.AUTO_REQUEST_APPLICATION,
        subject_id="subject-other",
    )
    current = PrivateHomeSearchProfileIntentPolicyRepository(
        home
    ).get_current(SUBJECT, profile.profile_id)

    assert first.status is SaveSearchProfileIntentPolicyStatus.CREATED
    assert replay.status is SaveSearchProfileIntentPolicyStatus.UNCHANGED
    assert changed.policy.policy_version == 2
    assert current.status is SearchProfileIntentPolicyReadStatus.FOUND
    assert current.policy == changed.policy
    assert wrong_subject.reason is SaveSearchProfileIntentPolicyReason.PROFILE_NOT_FOUND

    forbidden = {
        "application_plan",
        "application_engine",
        "selective_batch_preparation",
        "selective_batch_execution",
        "browser",
        "adapters",
    }
    for module in (
        Path("core/search_profile_intent_policy.py"),
        Path("core/job_library_refresh.py"),
    ):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert all(
            not any(
                name == item or name.startswith(f"{item}.")
                for item in forbidden
            )
            for name in imports
        )
