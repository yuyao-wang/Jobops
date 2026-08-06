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
from core.search_profile import (
    PrivateHomeSearchProfileRepository,
    SaveSearchProfileCommand,
    SearchProfileListResult,
    SearchProfileListStatus,
    SearchProfileSourceKind,
    SearchProfileSourceReference,
    save_search_profile,
)
from core.search_profile_intent_policy import (
    EnableAutoRequestApplicationBatchCommand,
    EnableAutoRequestApplicationBatchFailureReason,
    EnableAutoRequestApplicationBatchStatus,
    PrivateHomeSearchProfileIntentPolicyRepository,
    SaveSearchProfileIntentPolicyCommand,
    SaveSearchProfileIntentPolicyReason,
    SaveSearchProfileIntentPolicyStatus,
    SearchProfileIntentMode,
    SearchProfileIntentPolicyReadResult,
    SearchProfileIntentPolicyReadStatus,
    SearchProfileIntentPolicyWriteResult,
    SearchProfileIntentPolicyWriteStatus,
    enable_auto_request_application_for_enabled_search_profiles,
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


def _create_profile(
    *,
    home: PrivateHome,
    display_name: str,
    board: str,
    subject_id: str = SUBJECT,
):
    result = save_search_profile(
        SaveSearchProfileCommand(
            subject_id=subject_id,
            display_name=display_name,
            company=display_name,
            title="Engineer",
            source=SearchProfileSourceReference(
                SearchProfileSourceKind.KNOWN_GREENHOUSE_BOARD,
                board,
            ),
            enabled=True,
            now=NOW,
        ),
        repository=PrivateHomeSearchProfileRepository(home),
    )
    assert result.profile is not None
    return result.profile


def _enable_all(
    *,
    home: PrivateHome,
    subject_id: str = SUBJECT,
    now=NOW,
    search_profile_repository=None,
    policy_repository=None,
):
    return enable_auto_request_application_for_enabled_search_profiles(
        EnableAutoRequestApplicationBatchCommand(
            subject_id=subject_id,
            now=now,
        ),
        search_profile_repository=(
            search_profile_repository
            or PrivateHomeSearchProfileRepository(home)
        ),
        policy_repository=(
            policy_repository
            or PrivateHomeSearchProfileIntentPolicyRepository(home)
        ),
    )


class _RecordingSearchProfileRepository:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.get_calls: list[str] = []

    def list_enabled(self, subject_id):
        return self.delegate.list_enabled(subject_id)

    def get(self, subject_id, profile_id):
        self.get_calls.append(profile_id)
        return self.delegate.get(subject_id, profile_id)


class _FailOnePolicyRepository:
    def __init__(self, delegate, failing_profile_id: str) -> None:
        self.delegate = delegate
        self.failing_profile_id = failing_profile_id

    def get_current(self, subject_id, search_profile_id):
        return self.delegate.get_current(subject_id, search_profile_id)

    def save(self, policy):
        if policy.profile_id == self.failing_profile_id:
            return SearchProfileIntentPolicyWriteResult(
                SearchProfileIntentPolicyWriteStatus.FAILED,
                None,
            )
        return self.delegate.save(policy)


class _IntegrityFailingProfileRepository:
    def __init__(self) -> None:
        self.get_calls = 0

    def list_enabled(self, _subject_id):
        return SearchProfileListResult(
            SearchProfileListStatus.INTEGRITY_FAILURE,
            (),
        )

    def get(self, _subject_id, _profile_id):
        self.get_calls += 1
        raise AssertionError("an untrusted profile snapshot must not be used")


class _IntegrityFailingPolicyRepository:
    def __init__(self, delegate, failing_profile_id: str) -> None:
        self.delegate = delegate
        self.failing_profile_id = failing_profile_id
        self.get_calls: list[str] = []

    def get_current(self, subject_id, search_profile_id):
        self.get_calls.append(search_profile_id)
        if search_profile_id == self.failing_profile_id:
            return SearchProfileIntentPolicyReadResult(
                SearchProfileIntentPolicyReadStatus.INTEGRITY_FAILURE,
                None,
            )
        return self.delegate.get_current(subject_id, search_profile_id)

    def save(self, policy):
        return self.delegate.save(policy)


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


def test_enable_auto_request_application_for_all_enabled_profiles_serially(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    zulu = _create_profile(
        home=home,
        display_name="Zulu",
        board="zulu-board",
    )
    alpha = _create_profile(
        home=home,
        display_name="Alpha",
        board="alpha-board",
    )
    profile_repository = _RecordingSearchProfileRepository(
        PrivateHomeSearchProfileRepository(home)
    )
    policy_repository = PrivateHomeSearchProfileIntentPolicyRepository(home)

    result = _enable_all(
        home=home,
        search_profile_repository=profile_repository,
        policy_repository=policy_repository,
    )

    assert result.status is EnableAutoRequestApplicationBatchStatus.COMPLETED
    assert result.summary.selected == 2
    assert result.summary.created == 2
    assert result.summary.unchanged == result.summary.failed == 0
    assert profile_repository.get_calls == [alpha.profile_id, zulu.profile_id]
    for profile in (alpha, zulu):
        current = policy_repository.get_current(SUBJECT, profile.profile_id)
        assert current.status is SearchProfileIntentPolicyReadStatus.FOUND
        assert (
            current.policy.intent_mode
            is SearchProfileIntentMode.AUTO_REQUEST_APPLICATION
        )
        assert current.policy.enabled is True


def test_enable_auto_request_application_batch_is_idempotent(tmp_path) -> None:
    home = PrivateHome(tmp_path)
    profile = _create_profile(
        home=home,
        display_name="Idempotent",
        board="idempotent-board",
    )

    first = _enable_all(home=home)
    replay = _enable_all(home=home, now=NOW + timedelta(minutes=1))
    current = PrivateHomeSearchProfileIntentPolicyRepository(
        home
    ).get_current(SUBJECT, profile.profile_id)

    assert first.status is EnableAutoRequestApplicationBatchStatus.COMPLETED
    assert first.summary.created == 1
    assert replay.status is EnableAutoRequestApplicationBatchStatus.COMPLETED
    assert replay.summary.selected == replay.summary.unchanged == 1
    assert replay.summary.created == replay.summary.failed == 0
    assert current.status is SearchProfileIntentPolicyReadStatus.FOUND
    assert current.policy.policy_version == 1


def test_enable_auto_request_application_batch_is_noop_without_profiles(
    tmp_path,
) -> None:
    result = _enable_all(home=PrivateHome(tmp_path))

    assert result.status is EnableAutoRequestApplicationBatchStatus.NOOP
    assert result.summary.selected == 0
    assert result.summary.created == 0
    assert result.summary.unchanged == 0
    assert result.summary.failed == 0
    assert result.failure_reason is None


def test_enable_auto_request_application_batch_isolates_persistence_failure(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    alpha = _create_profile(
        home=home,
        display_name="Alpha",
        board="partial-alpha",
    )
    zulu = _create_profile(
        home=home,
        display_name="Zulu",
        board="partial-zulu",
    )
    stored = PrivateHomeSearchProfileIntentPolicyRepository(home)
    policy_repository = _FailOnePolicyRepository(stored, zulu.profile_id)

    result = _enable_all(
        home=home,
        policy_repository=policy_repository,
    )

    assert (
        result.status
        is EnableAutoRequestApplicationBatchStatus.PARTIAL_FAILURE
    )
    assert result.summary.selected == 2
    assert result.summary.created == 1
    assert result.summary.unchanged == 0
    assert result.summary.failed == 1
    assert (
        result.failure_reason
        is EnableAutoRequestApplicationBatchFailureReason.POLICY_UPDATE_FAILED
    )
    assert (
        stored.get_current(SUBJECT, alpha.profile_id).status
        is SearchProfileIntentPolicyReadStatus.FOUND
    )
    assert (
        stored.get_current(SUBJECT, zulu.profile_id).status
        is SearchProfileIntentPolicyReadStatus.NOT_FOUND
    )


def test_enable_auto_request_application_batch_is_subject_isolated(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    selected = _create_profile(
        home=home,
        display_name="Selected",
        board="selected-board",
    )
    other_subject = "subject-other"
    other = _create_profile(
        home=home,
        display_name="Other",
        board="other-board",
        subject_id=other_subject,
    )
    policies = PrivateHomeSearchProfileIntentPolicyRepository(home)

    result = _enable_all(home=home, policy_repository=policies)

    assert result.status is EnableAutoRequestApplicationBatchStatus.COMPLETED
    assert result.summary.selected == 1
    assert (
        policies.get_current(SUBJECT, selected.profile_id).status
        is SearchProfileIntentPolicyReadStatus.FOUND
    )
    assert (
        policies.get_current(other_subject, other.profile_id).status
        is SearchProfileIntentPolicyReadStatus.NOT_FOUND
    )


def test_enable_auto_request_application_batch_fails_closed_on_snapshot_integrity(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    profiles = _IntegrityFailingProfileRepository()

    result = _enable_all(
        home=home,
        search_profile_repository=profiles,
    )

    assert result.status is EnableAutoRequestApplicationBatchStatus.FAILED
    assert result.summary.selected == 0
    assert (
        result.failure_reason
        is EnableAutoRequestApplicationBatchFailureReason
        .PROFILE_SNAPSHOT_INTEGRITY_FAILURE
    )
    assert profiles.get_calls == 0


def test_enable_auto_request_application_batch_stops_on_policy_integrity_failure(
    tmp_path,
) -> None:
    home = PrivateHome(tmp_path)
    alpha = _create_profile(
        home=home,
        display_name="Alpha",
        board="integrity-alpha",
    )
    beta = _create_profile(
        home=home,
        display_name="Beta",
        board="integrity-beta",
    )
    zulu = _create_profile(
        home=home,
        display_name="Zulu",
        board="integrity-zulu",
    )
    stored = PrivateHomeSearchProfileIntentPolicyRepository(home)
    policies = _IntegrityFailingPolicyRepository(stored, beta.profile_id)

    result = _enable_all(home=home, policy_repository=policies)

    assert result.status is EnableAutoRequestApplicationBatchStatus.FAILED
    assert result.summary.selected == 3
    assert result.summary.created == 1
    assert result.summary.unchanged == 0
    assert result.summary.failed == 2
    assert (
        result.failure_reason
        is EnableAutoRequestApplicationBatchFailureReason
        .POLICY_INTEGRITY_FAILURE
    )
    assert policies.get_calls == [alpha.profile_id, beta.profile_id]
    assert (
        stored.get_current(SUBJECT, zulu.profile_id).status
        is SearchProfileIntentPolicyReadStatus.NOT_FOUND
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
