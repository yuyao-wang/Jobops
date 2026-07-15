"""Shared trust boundary for Workday-owned application hosts."""

from __future__ import annotations


WORKDAY_HOST_SUFFIXES = (
    "myworkdayjobs.com",
    "myworkdaysite.com",
    "workdayjobs.com",
    "workday.com",
)


def is_trusted_workday_host(host: str) -> bool:
    normalized = str(host or "").casefold().rstrip(".")
    return any(
        normalized == suffix or normalized.endswith(f".{suffix}")
        for suffix in WORKDAY_HOST_SUFFIXES
    )


__all__ = ["WORKDAY_HOST_SUFFIXES", "is_trusted_workday_host"]
