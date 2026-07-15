"""CSV-backed application queue helpers.

This module deliberately treats the CSV as data only.  It does not import or
invoke any external job-search workflow; MR.Jobs remains responsible for the
browser automation and submission decision.
"""

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REQUIRED_COLUMNS = {
    "company",
    "job_title",
    "job_url",
    "priority",
    "status",
    "resume_variant",
}


@dataclass(frozen=True)
class CSVApplication:
    """One selected application row plus its resolved resume path."""

    row_index: int
    row: dict[str, str]
    resume_path: Path

    @property
    def company(self) -> str:
        return self.row.get("company", "").strip()

    @property
    def title(self) -> str:
        return self.row.get("job_title", "").strip()

    @property
    def url(self) -> str:
        return self.row.get("job_url", "").strip()

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.company, self.title, self.url


def parse_csv_values(value: str | Iterable[str]) -> list[str]:
    """Parse a comma-separated CLI value while preserving caller order."""
    if isinstance(value, str):
        values = value.split(",")
    else:
        values = value
    return [str(item).strip() for item in values if str(item).strip()]


def load_csv_queue(
    csv_path: str | Path,
    resume_dir: str | Path,
    priorities: str | Iterable[str] = "High,Medium,Low",
    statuses: str | Iterable[str] = "Needs user,Pending",
    limit: int = 0,
) -> list[CSVApplication]:
    """Load, filter, and stably sort a CSV application queue.

    Priority is the primary sort key; status is secondary.  The order supplied
    in ``priorities`` and ``statuses`` defines their rank.
    """
    path = Path(csv_path).expanduser().resolve()
    resumes = Path(resume_dir).expanduser().resolve()
    priority_values = parse_csv_values(priorities)
    status_values = parse_csv_values(statuses)
    priority_rank = {value.casefold(): index for index, value in enumerate(priority_values)}
    status_rank = {value.casefold(): index for index, value in enumerate(status_values)}

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")
        rows = list(reader)

    selected: list[CSVApplication] = []
    for row_index, row in enumerate(rows):
        priority = row.get("priority", "").strip().casefold()
        status = row.get("status", "").strip().casefold()
        if priority not in priority_rank or status not in status_rank:
            continue

        resume_value = row.get("resume_variant", "").strip()
        resume_path = Path(resume_value).expanduser() if resume_value else Path()
        if resume_value and not resume_path.is_absolute():
            resume_path = resumes / resume_path

        selected.append(CSVApplication(row_index, row, resume_path.resolve()))

    selected.sort(
        key=lambda item: (
            priority_rank[item.row.get("priority", "").strip().casefold()],
            status_rank[item.row.get("status", "").strip().casefold()],
            item.row_index,
        )
    )
    return selected[:limit] if limit > 0 else selected


def update_csv_application(
    csv_path: str | Path,
    application: CSVApplication,
    updates: dict[str, str],
    note: str = "",
) -> None:
    """Atomically update one selected CSV row, rejecting stale row identities."""
    path = Path(csv_path).expanduser().resolve()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    if application.row_index >= len(rows):
        raise RuntimeError("CSV changed during the run: selected row no longer exists")

    current = rows[application.row_index]
    current_identity = (
        current.get("company", "").strip(),
        current.get("job_title", "").strip(),
        current.get("job_url", "").strip(),
    )
    if current_identity != application.identity:
        raise RuntimeError("CSV changed during the run: selected row identity no longer matches")

    unknown = sorted(set(updates) - set(fieldnames))
    if unknown:
        raise ValueError(f"Cannot update missing CSV columns: {', '.join(unknown)}")

    current.update({key: str(value) for key, value in updates.items()})
    if note:
        if "notes" not in fieldnames:
            raise ValueError("Cannot append a note because the CSV has no notes column")
        previous = current.get("notes", "").strip()
        current["notes"] = f"{previous} | {note}" if previous else note

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
