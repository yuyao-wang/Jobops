"""UI-safe adapter for the S3b manual job-library refresh service."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from core.authenticated_subject import AuthenticatedSubjectContext
from core.job_library_refresh import (
    CandidateDiscoveryStatus,
    JobLibraryRefreshStatus,
    ManualJobLibraryRefreshCommand,
    ManualJobLibraryRefreshResult,
    ProfileRefreshSearchStatus,
)


class RefreshJobLibraryUIStatus(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    NOOP = "NOOP"


@dataclass(frozen=True, slots=True)
class RefreshJobLibraryUICommand:
    invocation_id: str
    max_reprioritizations: int

    def __post_init__(self) -> None:
        invocation_id = self.invocation_id.strip()
        if not invocation_id or len(invocation_id) > 240:
            raise ValueError("invocation_id is required")
        if (
            type(self.max_reprioritizations) is not int
            or self.max_reprioritizations < 1
        ):
            raise ValueError("max_reprioritizations must be positive")
        object.__setattr__(self, "invocation_id", invocation_id)


@dataclass(frozen=True, slots=True)
class RefreshJobLibraryUISummary:
    enabled_profiles: int = 0
    searched_profiles: int = 0
    candidates_found: int = 0
    jobs_created: int = 0
    jobs_updated: int = 0
    jobs_unchanged: int = 0
    jobs_failed: int = 0
    jobs_skipped: int = 0
    priorities_refreshed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class RefreshJobLibraryUIResult:
    status: RefreshJobLibraryUIStatus
    invocation_id: str
    summary: RefreshJobLibraryUISummary
    source_failures: tuple[str, ...] = ()
    refresh_run_id: str | None = None
    last_completed_refresh_time: datetime | None = None
    replayed: bool = False
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "last_completed_refresh_time": (
                self.last_completed_refresh_time.isoformat()
                if self.last_completed_refresh_time
                else None
            ),
            "message": self.message,
            "refresh_run_id": self.refresh_run_id,
            "replayed": self.replayed,
            "source_failures": list(self.source_failures),
            "status": self.status.value,
            "summary": self.summary.to_dict(),
        }


ManualRefreshCallable = Callable[
    [ManualJobLibraryRefreshCommand],
    Awaitable[ManualJobLibraryRefreshResult],
]


_SAFE_REASON_MESSAGES = {
    "PROFILE_SNAPSHOT_FAILED": "无法读取已启用的职位搜索配置。",
    "PROFILE_SNAPSHOT_INVALID": "职位搜索配置无效。",
    "PRIORITY_REFRESH_FAILED": "职位已刷新，但优先级更新失败。",
    "REPOSITORY_FAILURE": "刷新记录暂时不可用。",
    "REPLAY_INTEGRITY_FAILURE": "无法安全恢复本次刷新记录。",
    "SEARCH_FAILED": "该职位来源搜索失败。",
    "SEARCH_UNSUPPORTED": "该职位来源暂不受支持。",
    "SEARCH_RESULT_INVALID": "该职位来源返回了无效结果。",
    "SEARCH_EXCEPTION": "该职位来源暂时不可用。",
    "INVALID_CANDIDATE_URL": "发现结果缺少可读取的职位链接。",
    "PUBLIC_READ_FAILED": "无法读取该职位的公开信息。",
    "PUBLIC_READ_RESULT_INVALID": "职位公开信息格式无效。",
    "PUBLIC_READ_EXCEPTION": "职位公开信息读取失败。",
    "DISCOVERY_FAILED": "职位未能写入职位库。",
    "DISCOVERY_RESULT_INVALID": "职位库返回了无效结果。",
    "DISCOVERY_EXCEPTION": "职位库更新失败。",
}


def _safe_reason(reason: object | None) -> str:
    value = getattr(reason, "value", None)
    return _SAFE_REASON_MESSAGES.get(
        value, "刷新未完成，请稍后重试。"
    )


def _ui_status(
    result: ManualJobLibraryRefreshResult,
) -> RefreshJobLibraryUIStatus:
    status = result.status
    if status is JobLibraryRefreshStatus.UNCHANGED and result.run is not None:
        status = result.run.overall_status
    return RefreshJobLibraryUIStatus(status.value)


def map_manual_refresh_result(
    result: ManualJobLibraryRefreshResult,
    *,
    invocation_id: str,
) -> RefreshJobLibraryUIResult:
    """Project S3b output to a bounded, credential-safe UI result."""

    if not isinstance(result, ManualJobLibraryRefreshResult):
        return RefreshJobLibraryUIResult(
            RefreshJobLibraryUIStatus.FAILED,
            invocation_id,
            RefreshJobLibraryUISummary(),
            message="刷新服务返回了无效结果。",
        )
    run = result.run
    if run is None:
        return RefreshJobLibraryUIResult(
            RefreshJobLibraryUIStatus.FAILED,
            invocation_id,
            RefreshJobLibraryUISummary(),
            message=_safe_reason(result.reason),
        )

    discovery = run.discovery_summary
    priority = run.priority_summary
    summary = RefreshJobLibraryUISummary(
        enabled_profiles=len(run.profile_results),
        searched_profiles=sum(
            item.search_status is ProfileRefreshSearchStatus.SUCCEEDED
            for item in run.profile_results
        ),
        candidates_found=sum(
            item.candidate_count for item in run.profile_results
        ),
        jobs_created=discovery.created,
        jobs_updated=discovery.updated,
        jobs_unchanged=discovery.unchanged,
        jobs_failed=discovery.failed,
        jobs_skipped=discovery.skipped,
        priorities_refreshed=(
            priority.created + priority.unchanged if priority else 0
        ),
    )
    failures: list[str] = []
    for profile in run.profile_results:
        if profile.search_status is ProfileRefreshSearchStatus.SUCCEEDED:
            continue
        failures.append(
            f"{profile.source.kind.value}/{profile.source.source_id}: "
            f"{_safe_reason(profile.reason)}"
        )
    for candidate in run.candidate_results:
        if candidate.discovery_status not in {
            CandidateDiscoveryStatus.FAILED,
            CandidateDiscoveryStatus.SKIPPED,
        }:
            continue
        source_label = ", ".join(candidate.source_profile_ids)
        failures.append(
            f"搜索配置 {source_label}: {_safe_reason(candidate.reason)}"
        )
    if priority is None and run.overall_status in {
        JobLibraryRefreshStatus.PARTIAL_FAILURE,
        JobLibraryRefreshStatus.FAILED,
    }:
        failures.append("优先级刷新未完成。")

    status = _ui_status(result)
    message = (
        "没有已启用的职位搜索配置。"
        if status is RefreshJobLibraryUIStatus.NOOP
        else None
    )
    return RefreshJobLibraryUIResult(
        status=status,
        invocation_id=invocation_id,
        summary=summary,
        source_failures=tuple(failures),
        refresh_run_id=run.run_id,
        last_completed_refresh_time=run.completed_at,
        replayed=result.status is JobLibraryRefreshStatus.UNCHANGED,
        message=message,
    )


class RefreshJobLibraryUIController:
    """One authenticated UI action backed only by the S3b public callable."""

    def __init__(
        self,
        *,
        manual_refresh: ManualRefreshCallable,
        clock: Callable[[], datetime],
        max_reprioritizations: int | None = None,
    ) -> None:
        if max_reprioritizations is not None and (
            type(max_reprioritizations) is not int
            or max_reprioritizations < 0
        ):
            raise ValueError("max_reprioritizations must be non-negative")
        self._manual_refresh = manual_refresh
        self._clock = clock
        self._max_reprioritizations = max_reprioritizations
        self._active: dict[
            str, tuple[str, asyncio.Task[RefreshJobLibraryUIResult]]
        ] = {}

    async def refresh(
        self,
        *,
        context: AuthenticatedSubjectContext,
        command: RefreshJobLibraryUICommand,
    ) -> RefreshJobLibraryUIResult:
        if not isinstance(context, AuthenticatedSubjectContext):
            raise TypeError("context must be authenticated")
        if not isinstance(command, RefreshJobLibraryUICommand):
            raise TypeError("command must be typed")

        active = self._active.get(context.subject_id)
        if active is not None:
            active_invocation, task = active
            if active_invocation == command.invocation_id:
                return await asyncio.shield(task)
            return RefreshJobLibraryUIResult(
                RefreshJobLibraryUIStatus.RUNNING,
                active_invocation,
                RefreshJobLibraryUISummary(),
                message="职位库刷新正在进行中。",
            )

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        task = asyncio.create_task(
            self._invoke(
                subject_id=context.subject_id,
                command=command,
                now=now,
            )
        )
        self._active[context.subject_id] = (command.invocation_id, task)
        try:
            return await asyncio.shield(task)
        finally:
            current = self._active.get(context.subject_id)
            if current is not None and current[1] is task and task.done():
                self._active.pop(context.subject_id, None)

    async def _invoke(
        self,
        *,
        subject_id: str,
        command: RefreshJobLibraryUICommand,
        now: datetime,
    ) -> RefreshJobLibraryUIResult:
        s3b_command = ManualJobLibraryRefreshCommand(
            subject_id=subject_id,
            invocation_id=command.invocation_id,
            now=now,
            max_reprioritizations=(
                min(
                    command.max_reprioritizations,
                    self._max_reprioritizations,
                )
                if command.max_reprioritizations is not None
                and self._max_reprioritizations is not None
                else self._max_reprioritizations
                if self._max_reprioritizations is not None
                else command.max_reprioritizations
            ),
        )
        try:
            result = await self._manual_refresh(s3b_command)
        except (OSError, RuntimeError, TypeError, ValueError):
            return RefreshJobLibraryUIResult(
                RefreshJobLibraryUIStatus.FAILED,
                command.invocation_id,
                RefreshJobLibraryUISummary(),
                message="刷新服务暂时不可用。",
            )
        return map_manual_refresh_result(
            result, invocation_id=command.invocation_id
        )


__all__ = [
    "ManualRefreshCallable",
    "RefreshJobLibraryUICommand",
    "RefreshJobLibraryUIController",
    "RefreshJobLibraryUIResult",
    "RefreshJobLibraryUIStatus",
    "RefreshJobLibraryUISummary",
    "map_manual_refresh_result",
]
