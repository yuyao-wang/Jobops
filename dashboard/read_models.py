"""Authenticated UI controllers for the zero-write Dashboard read layer."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from core.authenticated_subject import AuthenticatedSubjectContext
from core.dashboard_read_models import (
    DashboardApplicationsReader,
    DashboardCandidateProfileReader,
    DashboardJobsReader,
    DashboardOverviewReader,
)


def _subject(context: AuthenticatedSubjectContext) -> str:
    if not isinstance(context, AuthenticatedSubjectContext):
        raise TypeError("context must be authenticated")
    return context.subject_id


class DashboardProfileController:
    def __init__(
        self,
        *,
        reader: DashboardCandidateProfileReader,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(reader, DashboardCandidateProfileReader):
            raise TypeError("reader must be DashboardCandidateProfileReader")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._reader = reader
        self._clock = clock

    async def load(self, *, context: AuthenticatedSubjectContext) -> Any:
        return await self._reader.read(
            subject_id=_subject(context), evaluated_at=self._clock()
        )


class DashboardJobsController:
    def __init__(
        self, *, reader: DashboardJobsReader, clock: Callable[[], datetime]
    ) -> None:
        if not isinstance(reader, DashboardJobsReader):
            raise TypeError("reader must be DashboardJobsReader")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._reader = reader
        self._clock = clock

    async def load(self, *, context: AuthenticatedSubjectContext) -> Any:
        return await self._reader.read(
            subject_id=_subject(context), evaluated_at=self._clock()
        )


class DashboardApplicationsController:
    def __init__(
        self,
        *,
        reader: DashboardApplicationsReader,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(reader, DashboardApplicationsReader):
            raise TypeError("reader must be DashboardApplicationsReader")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._reader = reader
        self._clock = clock

    async def load(self, *, context: AuthenticatedSubjectContext) -> Any:
        return await self._reader.read(
            subject_id=_subject(context), evaluated_at=self._clock()
        )


class DashboardOverviewController:
    def __init__(
        self,
        *,
        reader: DashboardOverviewReader,
        clock: Callable[[], datetime],
    ) -> None:
        if not isinstance(reader, DashboardOverviewReader):
            raise TypeError("reader must be DashboardOverviewReader")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._reader = reader
        self._clock = clock

    async def load(self, *, context: AuthenticatedSubjectContext) -> Any:
        return await self._reader.read(
            subject_id=_subject(context), evaluated_at=self._clock()
        )


__all__ = [
    "DashboardApplicationsController",
    "DashboardJobsController",
    "DashboardOverviewController",
    "DashboardProfileController",
]
