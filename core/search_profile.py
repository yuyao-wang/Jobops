"""Subject-scoped immutable SearchProfile configuration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from .job_search import (
    JobSearchRequest,
    canonicalize_search_company,
    canonicalize_search_match_text,
)
from .private_home import PrivateHome, PrivateHomeError


SEARCH_PROFILE_CONTRACT_VERSION = "search-profile-v1"
_PROFILE_ID_RE = re.compile(r"search-profile-[0-9a-f]{64}")
_SOURCE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _clean(name: str, value: Any, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > maximum:
        raise ValueError(f"{name} is outside the SearchProfile contract")
    return cleaned


def _aware(name: str, value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError("persisted timestamp must be a string")
    return _aware(
        "persisted timestamp",
        datetime.fromisoformat(value.replace("Z", "+00:00")),
    )


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _subject_key(subject_id: str) -> str:
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


class SearchProfileSourceKind(StrEnum):
    KNOWN_GREENHOUSE_BOARD = "KNOWN_GREENHOUSE_BOARD"


class SearchProfileRefreshMode(StrEnum):
    MANUAL = "MANUAL"


class SaveSearchProfileStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


class SaveSearchProfileReason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    PROFILE_INTEGRITY_FAILURE = "PROFILE_INTEGRITY_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


class SearchProfileReadStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class SearchProfileListStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"


class SearchProfileWriteStatus(StrEnum):
    CREATED = "CREATED"
    UNCHANGED = "UNCHANGED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SearchProfileSourceReference:
    kind: SearchProfileSourceKind
    source_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", SearchProfileSourceKind(self.kind))
        if (
            not isinstance(self.source_id, str)
            or _SOURCE_ID_RE.fullmatch(self.source_id) is None
        ):
            raise ValueError("source_id must be a Greenhouse board token")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind.value, "source_id": self.source_id}


def _canonical_query(
    *,
    profile_id: str,
    company: str,
    title: str,
    location: str | None,
) -> JobSearchRequest:
    canonical_company = canonicalize_search_company(company)
    canonical_title = canonicalize_search_match_text(
        title, name="title", maximum=240
    )
    canonical_location = (
        canonicalize_search_match_text(
            location, name="location", maximum=320
        )
        if location is not None
        else None
    )
    query_hash = _hash(
        {
            "company": canonical_company,
            "location": canonical_location,
            "profile_id": profile_id,
            "title": canonical_title,
        }
    )
    return JobSearchRequest(
        request_id=f"search-profile-request-{query_hash}",
        company=canonical_company,
        title=canonical_title,
        location=canonical_location,
    )


def _logical_profile_id(
    *,
    subject_id: str,
    display_name: str,
    source: SearchProfileSourceReference,
) -> str:
    digest = _hash(
        {
            "display_name": display_name.casefold(),
            "source": source.to_dict(),
            "subject_id": subject_id,
        }
    )
    return f"search-profile-{digest}"


def _content_payload(
    *,
    subject_id: str,
    profile_id: str,
    display_name: str,
    source: SearchProfileSourceReference,
    search_request: JobSearchRequest,
    enabled: bool,
    refresh_mode: SearchProfileRefreshMode,
) -> dict[str, Any]:
    return {
        "contract_version": SEARCH_PROFILE_CONTRACT_VERSION,
        "display_name": display_name,
        "enabled": enabled,
        "profile_id": profile_id,
        "refresh_mode": refresh_mode.value,
        "search_request": {
            "company": search_request.company,
            "location": search_request.location,
            "request_id": search_request.request_id,
            "title": search_request.title,
        },
        "source": source.to_dict(),
        "subject_id": subject_id,
    }


@dataclass(frozen=True, slots=True)
class SearchProfile:
    subject_id: str
    profile_id: str
    profile_version: int
    display_name: str
    source: SearchProfileSourceReference
    search_request: JobSearchRequest
    enabled: bool
    refresh_mode: SearchProfileRefreshMode
    content_hash: str
    contract_version: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        subject_id = _clean("subject_id", self.subject_id, 160)
        display_name = _clean("display_name", self.display_name, 240)
        if subject_id != self.subject_id or display_name != self.display_name:
            raise ValueError("SearchProfile text identity is not canonical")
        if _PROFILE_ID_RE.fullmatch(self.profile_id) is None:
            raise ValueError("profile_id is invalid")
        if type(self.profile_version) is not int or self.profile_version < 1:
            raise ValueError("profile_version must be positive")
        if not isinstance(self.source, SearchProfileSourceReference):
            raise TypeError("source must be typed")
        if not isinstance(self.search_request, JobSearchRequest):
            raise TypeError("search_request must be typed")
        if type(self.enabled) is not bool:
            raise TypeError("enabled must be boolean")
        object.__setattr__(
            self, "refresh_mode", SearchProfileRefreshMode(self.refresh_mode)
        )
        if self.refresh_mode is not SearchProfileRefreshMode.MANUAL:
            raise ValueError("only MANUAL refresh is supported")
        if self.contract_version != SEARCH_PROFILE_CONTRACT_VERSION:
            raise ValueError("SearchProfile contract version is unsupported")
        _aware("created_at", self.created_at)
        _aware("updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        if _HASH_RE.fullmatch(self.content_hash) is None:
            raise ValueError("content_hash is invalid")
        expected_query = _canonical_query(
            profile_id=self.profile_id,
            company=self.search_request.company,
            title=self.search_request.title,
            location=self.search_request.location,
        )
        if expected_query != self.search_request:
            raise ValueError("search_request is not canonical")
        expected_hash = _hash(
            _content_payload(
                subject_id=subject_id,
                profile_id=self.profile_id,
                display_name=display_name,
                source=self.source,
                search_request=self.search_request,
                enabled=self.enabled,
                refresh_mode=self.refresh_mode,
            )
        )
        if self.content_hash != expected_hash:
            raise ValueError("content_hash does not match SearchProfile")

    def to_dict(self) -> dict[str, Any]:
        return {
            **_content_payload(
                subject_id=self.subject_id,
                profile_id=self.profile_id,
                display_name=self.display_name,
                source=self.source,
                search_request=self.search_request,
                enabled=self.enabled,
                refresh_mode=self.refresh_mode,
            ),
            "content_hash": self.content_hash,
            "created_at": _time(self.created_at),
            "profile_version": self.profile_version,
            "updated_at": _time(self.updated_at),
        }

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        profile_id: str,
        profile_version: int,
        display_name: str,
        source: SearchProfileSourceReference,
        search_request: JobSearchRequest,
        enabled: bool,
        created_at: datetime,
        updated_at: datetime,
    ) -> "SearchProfile":
        display = _clean("display_name", display_name, 240)
        payload = _content_payload(
            subject_id=subject_id,
            profile_id=profile_id,
            display_name=display,
            source=source,
            search_request=search_request,
            enabled=enabled,
            refresh_mode=SearchProfileRefreshMode.MANUAL,
        )
        return cls(
            subject_id=subject_id,
            profile_id=profile_id,
            profile_version=profile_version,
            display_name=display,
            source=source,
            search_request=search_request,
            enabled=enabled,
            refresh_mode=SearchProfileRefreshMode.MANUAL,
            content_hash=_hash(payload),
            contract_version=SEARCH_PROFILE_CONTRACT_VERSION,
            created_at=created_at,
            updated_at=updated_at,
        )


@dataclass(frozen=True, slots=True)
class SaveSearchProfileCommand:
    subject_id: str
    display_name: str
    company: str
    title: str
    source: SearchProfileSourceReference
    enabled: bool
    now: datetime
    location: str | None = None
    profile_id: str | None = None
    refresh_mode: SearchProfileRefreshMode = SearchProfileRefreshMode.MANUAL


@dataclass(frozen=True, slots=True)
class SaveSearchProfileResult:
    status: SaveSearchProfileStatus
    profile: SearchProfile | None
    reason: SaveSearchProfileReason | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SaveSearchProfileStatus(self.status))
        if self.reason is not None:
            object.__setattr__(
                self, "reason", SaveSearchProfileReason(self.reason)
            )
        if self.status in {
            SaveSearchProfileStatus.CREATED,
            SaveSearchProfileStatus.UNCHANGED,
        }:
            if self.profile is None or self.reason is not None:
                raise ValueError("successful profile result is malformed")
        elif self.profile is not None or self.reason is None:
            raise ValueError("failed profile result is malformed")


@dataclass(frozen=True, slots=True)
class SearchProfileReadResult:
    status: SearchProfileReadStatus
    profile: SearchProfile | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", SearchProfileReadStatus(self.status)
        )
        if self.status is SearchProfileReadStatus.FOUND:
            if not isinstance(self.profile, SearchProfile):
                raise ValueError("FOUND SearchProfile read requires a profile")
        elif self.profile is not None:
            raise ValueError("failed SearchProfile read cannot expose a profile")


@dataclass(frozen=True, slots=True)
class SearchProfileListResult:
    status: SearchProfileListStatus
    profiles: tuple[SearchProfile, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", SearchProfileListStatus(self.status)
        )
        if not isinstance(self.profiles, tuple) or any(
            not isinstance(profile, SearchProfile)
            for profile in self.profiles
        ):
            raise TypeError("SearchProfile list must be typed")
        if (
            self.status is SearchProfileListStatus.INTEGRITY_FAILURE
            and self.profiles
        ):
            raise ValueError("failed SearchProfile list cannot expose profiles")


@dataclass(frozen=True, slots=True)
class SearchProfileWriteResult:
    status: SearchProfileWriteStatus
    profile: SearchProfile | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "status", SearchProfileWriteStatus(self.status)
        )
        if self.status in {
            SearchProfileWriteStatus.CREATED,
            SearchProfileWriteStatus.UNCHANGED,
        }:
            if not isinstance(self.profile, SearchProfile):
                raise ValueError("successful write requires a SearchProfile")
        elif self.profile is not None:
            raise ValueError("failed write cannot expose a SearchProfile")


@runtime_checkable
class SearchProfileRepository(Protocol):
    def save(self, profile: SearchProfile) -> SearchProfileWriteResult: ...

    def get(
        self, subject_id: str, profile_id: str
    ) -> SearchProfileReadResult: ...

    def list_current(self, subject_id: str) -> SearchProfileListResult: ...

    def list_enabled(self, subject_id: str) -> SearchProfileListResult: ...


@runtime_checkable
class SearchProfileProvider(Protocol):
    def list_current(self, subject_id: str) -> SearchProfileListResult: ...

    def list_enabled(self, subject_id: str) -> SearchProfileListResult: ...


def _profile_from_dict(value: Any) -> SearchProfile:
    expected = {
        "content_hash",
        "contract_version",
        "created_at",
        "display_name",
        "enabled",
        "profile_id",
        "profile_version",
        "refresh_mode",
        "search_request",
        "source",
        "subject_id",
        "updated_at",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("persisted SearchProfile is malformed")
    source = value["source"]
    request = value["search_request"]
    if not isinstance(source, Mapping) or set(source) != {"kind", "source_id"}:
        raise ValueError("persisted SearchProfile source is malformed")
    if not isinstance(request, Mapping) or set(request) != {
        "company",
        "location",
        "request_id",
        "title",
    }:
        raise ValueError("persisted SearchProfile request is malformed")
    return SearchProfile(
        subject_id=value["subject_id"],
        profile_id=value["profile_id"],
        profile_version=value["profile_version"],
        display_name=value["display_name"],
        source=SearchProfileSourceReference(
            kind=SearchProfileSourceKind(source["kind"]),
            source_id=source["source_id"],
        ),
        search_request=JobSearchRequest(**dict(request)),
        enabled=value["enabled"],
        refresh_mode=SearchProfileRefreshMode(value["refresh_mode"]),
        content_hash=value["content_hash"],
        contract_version=value["contract_version"],
        created_at=_parse_time(value["created_at"]),
        updated_at=_parse_time(value["updated_at"]),
    )


class PrivateHomeSearchProfileRepository:
    def __init__(self, home: PrivateHome | None = None) -> None:
        self._home = home or PrivateHome.discover()
        self._lock = RLock()

    def _subject_directory(self, subject_id: str) -> Path:
        subject = _clean("subject_id", subject_id, 160)
        return (
            self._home.root
            / "state"
            / "discovery"
            / "search-profiles"
            / _subject_key(subject)
        )

    def _profile_directory(self, subject_id: str, profile_id: str) -> Path:
        if _PROFILE_ID_RE.fullmatch(profile_id) is None:
            raise ValueError("profile_id is invalid")
        return self._subject_directory(subject_id) / profile_id

    def _path(
        self, subject_id: str, profile_id: str, profile_version: int
    ) -> Path:
        if type(profile_version) is not int or profile_version < 1:
            raise ValueError("profile_version must be positive")
        return self._profile_directory(subject_id, profile_id) / (
            f"v{profile_version:08d}.json"
        )

    def _read_path(self, path: Path) -> SearchProfile:
        if path.is_symlink() or not path.is_file():
            raise ValueError("SearchProfile record is not a regular file")
        return _profile_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def get(
        self, subject_id: str, profile_id: str
    ) -> SearchProfileReadResult:
        directory = self._profile_directory(subject_id, profile_id)
        if not directory.exists():
            return SearchProfileReadResult(
                SearchProfileReadStatus.NOT_FOUND, None
            )
        if directory.is_symlink() or not directory.is_dir():
            return SearchProfileReadResult(
                SearchProfileReadStatus.INTEGRITY_FAILURE, None
            )
        try:
            paths = tuple(sorted(directory.glob("v*.json")))
            if not paths:
                raise ValueError("SearchProfile has no versions")
            profiles = tuple(self._read_path(path) for path in paths)
            if any(
                path.name != f"v{index:08d}.json"
                or profile.subject_id != subject_id.strip()
                or profile.profile_id != profile_id
                or profile.profile_version != index
                for index, (path, profile) in enumerate(
                    zip(paths, profiles), start=1
                )
            ):
                raise ValueError("SearchProfile version history is invalid")
            return SearchProfileReadResult(
                SearchProfileReadStatus.FOUND, profiles[-1]
            )
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return SearchProfileReadResult(
                SearchProfileReadStatus.INTEGRITY_FAILURE, None
            )

    def _list(self, subject_id: str) -> SearchProfileListResult:
        directory = self._subject_directory(subject_id)
        if not directory.exists():
            return SearchProfileListResult(
                SearchProfileListStatus.SUCCEEDED, ()
            )
        if directory.is_symlink() or not directory.is_dir():
            return SearchProfileListResult(
                SearchProfileListStatus.INTEGRITY_FAILURE, ()
            )
        try:
            profiles: list[SearchProfile] = []
            for path in sorted(directory.iterdir(), key=lambda item: item.name):
                if path.is_symlink() or not path.is_dir():
                    raise ValueError("SearchProfile directory is malformed")
                read = self.get(subject_id, path.name)
                if (
                    read.status is not SearchProfileReadStatus.FOUND
                    or read.profile is None
                ):
                    raise ValueError("SearchProfile history is unreadable")
                profiles.append(read.profile)
            ordered = tuple(
                sorted(
                    profiles,
                    key=lambda item: (
                        item.display_name.casefold(),
                        item.profile_id,
                    ),
                )
            )
            return SearchProfileListResult(
                SearchProfileListStatus.SUCCEEDED, ordered
            )
        except (OSError, TypeError, ValueError):
            return SearchProfileListResult(
                SearchProfileListStatus.INTEGRITY_FAILURE, ()
            )

    def list_current(self, subject_id: str) -> SearchProfileListResult:
        return self._list(subject_id)

    def list_enabled(self, subject_id: str) -> SearchProfileListResult:
        listed = self._list(subject_id)
        if listed.status is SearchProfileListStatus.INTEGRITY_FAILURE:
            return listed
        return SearchProfileListResult(
            SearchProfileListStatus.SUCCEEDED,
            tuple(profile for profile in listed.profiles if profile.enabled),
        )

    def save(self, profile: SearchProfile) -> SearchProfileWriteResult:
        if not isinstance(profile, SearchProfile):
            raise TypeError("profile must be typed")
        path = self._path(
            profile.subject_id,
            profile.profile_id,
            profile.profile_version,
        )
        with self._lock:
            try:
                self._home.ensure()
                created = self._home.write_bytes_if_absent(
                    path,
                    (
                        json.dumps(
                            profile.to_dict(),
                            sort_keys=True,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
            except (OSError, PrivateHomeError):
                return SearchProfileWriteResult(
                    SearchProfileWriteStatus.FAILED, None
                )
            if created:
                return SearchProfileWriteResult(
                    SearchProfileWriteStatus.CREATED, profile
                )
            try:
                existing = self._read_path(path)
            except (
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                return SearchProfileWriteResult(
                    SearchProfileWriteStatus.FAILED, None
                )
            if existing.to_dict() == profile.to_dict():
                return SearchProfileWriteResult(
                    SearchProfileWriteStatus.UNCHANGED, existing
                )
            return SearchProfileWriteResult(
                SearchProfileWriteStatus.FAILED, None
            )


def save_search_profile(
    command: SaveSearchProfileCommand,
    *,
    repository: SearchProfileRepository,
) -> SaveSearchProfileResult:
    if not isinstance(command, SaveSearchProfileCommand):
        raise TypeError("command must be a SaveSearchProfileCommand")
    try:
        subject_id = _clean("subject_id", command.subject_id, 160)
        display_name = _clean("display_name", command.display_name, 240)
        if not isinstance(command.source, SearchProfileSourceReference):
            raise TypeError("source must be typed")
        if command.source.kind is not (
            SearchProfileSourceKind.KNOWN_GREENHOUSE_BOARD
        ):
            raise ValueError("source is unsupported")
        if type(command.enabled) is not bool:
            raise TypeError("enabled must be boolean")
        now = _aware("now", command.now)
        refresh_mode = SearchProfileRefreshMode(command.refresh_mode)
        if refresh_mode is not SearchProfileRefreshMode.MANUAL:
            raise ValueError("only MANUAL refresh is supported")
        profile_id = (
            command.profile_id
            if command.profile_id is not None
            else _logical_profile_id(
                subject_id=subject_id,
                display_name=display_name,
                source=command.source,
            )
        )
        if _PROFILE_ID_RE.fullmatch(profile_id) is None:
            raise ValueError("profile_id is invalid")
        query = _canonical_query(
            profile_id=profile_id,
            company=command.company,
            title=command.title,
            location=command.location,
        )
    except (TypeError, ValueError):
        return SaveSearchProfileResult(
            SaveSearchProfileStatus.FAILED,
            None,
            SaveSearchProfileReason.INVALID_REQUEST,
        )

    try:
        current = repository.get(subject_id, profile_id)
    except (OSError, RuntimeError, TypeError, ValueError):
        return SaveSearchProfileResult(
            SaveSearchProfileStatus.FAILED,
            None,
            SaveSearchProfileReason.PERSISTENCE_FAILURE,
        )
    if not isinstance(current, SearchProfileReadResult):
        return SaveSearchProfileResult(
            SaveSearchProfileStatus.FAILED,
            None,
            SaveSearchProfileReason.PROFILE_INTEGRITY_FAILURE,
        )
    if current.status is SearchProfileReadStatus.INTEGRITY_FAILURE:
        return SaveSearchProfileResult(
            SaveSearchProfileStatus.FAILED,
            None,
            SaveSearchProfileReason.PROFILE_INTEGRITY_FAILURE,
        )
    previous = current.profile
    version = previous.profile_version + 1 if previous is not None else 1
    created_at = previous.created_at if previous is not None else now
    candidate = SearchProfile.create(
        subject_id=subject_id,
        profile_id=profile_id,
        profile_version=version,
        display_name=display_name,
        source=command.source,
        search_request=query,
        enabled=command.enabled,
        created_at=created_at,
        updated_at=now,
    )
    if previous is not None and previous.content_hash == candidate.content_hash:
        return SaveSearchProfileResult(
            SaveSearchProfileStatus.UNCHANGED, previous, None
        )
    try:
        written = repository.save(candidate)
    except (OSError, RuntimeError, TypeError, ValueError):
        return SaveSearchProfileResult(
            SaveSearchProfileStatus.FAILED,
            None,
            SaveSearchProfileReason.PERSISTENCE_FAILURE,
        )
    if (
        not isinstance(written, SearchProfileWriteResult)
        or written.status is SearchProfileWriteStatus.FAILED
        or written.profile is None
        or written.profile.to_dict() != candidate.to_dict()
    ):
        return SaveSearchProfileResult(
            SaveSearchProfileStatus.FAILED,
            None,
            SaveSearchProfileReason.PERSISTENCE_FAILURE,
        )
    return SaveSearchProfileResult(
        SaveSearchProfileStatus.CREATED, written.profile, None
    )


__all__ = [
    "SEARCH_PROFILE_CONTRACT_VERSION",
    "PrivateHomeSearchProfileRepository",
    "SaveSearchProfileCommand",
    "SaveSearchProfileReason",
    "SaveSearchProfileResult",
    "SaveSearchProfileStatus",
    "SearchProfile",
    "SearchProfileListResult",
    "SearchProfileListStatus",
    "SearchProfileProvider",
    "SearchProfileReadResult",
    "SearchProfileReadStatus",
    "SearchProfileRefreshMode",
    "SearchProfileRepository",
    "SearchProfileSourceKind",
    "SearchProfileSourceReference",
    "SearchProfileWriteResult",
    "SearchProfileWriteStatus",
    "save_search_profile",
]
