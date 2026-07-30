"""Focused S3a SearchProfile contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from core.private_home import PrivateHome
from core.search_profile import (
    PrivateHomeSearchProfileRepository,
    SaveSearchProfileCommand,
    SaveSearchProfileStatus,
    SearchProfileListStatus,
    SearchProfileReadStatus,
    SearchProfileRefreshMode,
    SearchProfileSourceKind,
    SearchProfileSourceReference,
    save_search_profile,
)
from tests.test_application_plan import NOW, SUBJECT


SOURCE = SearchProfileSourceReference(
    kind=SearchProfileSourceKind.KNOWN_GREENHOUSE_BOARD,
    source_id="examplelabs",
)


def _command(**changes) -> SaveSearchProfileCommand:
    values = {
        "subject_id": SUBJECT,
        "display_name": "Example Labs ML",
        "company": "Example Labs",
        "title": "Machine Learning Engineer",
        "location": "Remote",
        "source": SOURCE,
        "enabled": True,
        "now": NOW,
    }
    values.update(changes)
    return SaveSearchProfileCommand(**values)


def test_create_persist_and_restart_read_typed_profile(tmp_path) -> None:
    home = PrivateHome(tmp_path)
    created = save_search_profile(
        _command(),
        repository=PrivateHomeSearchProfileRepository(home),
    )

    assert created.status is SaveSearchProfileStatus.CREATED
    assert created.profile.profile_version == 1
    assert created.profile.search_request.company == "example labs"
    assert created.profile.search_request.title == "machine learning engineer"
    restarted = PrivateHomeSearchProfileRepository(home).get(
        SUBJECT, created.profile.profile_id
    )
    assert restarted.status is SearchProfileReadStatus.FOUND
    assert restarted.profile == created.profile


def test_replay_is_unchanged_and_updates_append_versions(tmp_path) -> None:
    repository = PrivateHomeSearchProfileRepository(PrivateHome(tmp_path))
    first = save_search_profile(_command(), repository=repository)
    replay = save_search_profile(
        _command(
            company="  EXAMPLE   LABS ",
            title="Machine-learning ENGINEER",
            location=" remote ",
            now=NOW + timedelta(minutes=1),
        ),
        repository=repository,
    )
    changed = save_search_profile(
        _command(
            profile_id=first.profile.profile_id,
            title="Senior Machine Learning Engineer",
            now=NOW + timedelta(minutes=2),
        ),
        repository=repository,
    )
    disabled = save_search_profile(
        _command(
            profile_id=first.profile.profile_id,
            title="Senior Machine Learning Engineer",
            enabled=False,
            now=NOW + timedelta(minutes=3),
        ),
        repository=repository,
    )

    assert replay.status is SaveSearchProfileStatus.UNCHANGED
    assert replay.profile.profile_version == 1
    assert changed.profile.profile_version == 2
    assert disabled.profile.profile_version == 3
    assert first.profile.created_at == disabled.profile.created_at
    records = tuple(
        (
            tmp_path
            / "state"
            / "discovery"
            / "search-profiles"
        ).rglob("v*.json")
    )
    assert len(records) == 3


def test_enabled_current_order_and_subject_isolation_are_deterministic(
    tmp_path,
) -> None:
    repository = PrivateHomeSearchProfileRepository(PrivateHome(tmp_path))
    zulu = save_search_profile(
        _command(
            display_name="Zulu",
            source=replace(SOURCE, source_id="zulu-board"),
        ),
        repository=repository,
    )
    alpha = save_search_profile(
        _command(
            display_name="alpha",
            source=replace(SOURCE, source_id="alpha-board"),
        ),
        repository=repository,
    )
    save_search_profile(
        _command(
            profile_id=zulu.profile.profile_id,
            display_name="Zulu",
            source=replace(SOURCE, source_id="zulu-board"),
            enabled=False,
            now=NOW + timedelta(minutes=1),
        ),
        repository=repository,
    )
    save_search_profile(
        _command(
            subject_id="subject-other",
            display_name="Aardvark",
            source=replace(SOURCE, source_id="other-board"),
        ),
        repository=repository,
    )

    current = repository.list_current(SUBJECT)
    enabled = PrivateHomeSearchProfileRepository(
        PrivateHome(tmp_path)
    ).list_enabled(SUBJECT)
    assert current.status is SearchProfileListStatus.SUCCEEDED
    assert [item.display_name for item in current.profiles] == ["alpha", "Zulu"]
    assert [item.profile_id for item in enabled.profiles] == [
        alpha.profile.profile_id
    ]
    assert all(item.subject_id == SUBJECT for item in current.profiles)


def test_invalid_source_query_and_refresh_mode_fail_without_side_effects(
    tmp_path,
) -> None:
    repository = PrivateHomeSearchProfileRepository(PrivateHome(tmp_path))
    commands = (
        _command(source="KNOWN_LEVER"),
        _command(company=" "),
        _command(title=" "),
        _command(refresh_mode="SCHEDULED"),
    )
    results = tuple(
        save_search_profile(command, repository=repository)
        for command in commands
    )

    assert all(
        result.status is SaveSearchProfileStatus.FAILED
        for result in results
    )
    assert repository.list_current(SUBJECT).profiles == ()
    assert not (tmp_path / "state" / "discovery" / "search-profiles").exists()
