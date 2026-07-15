import csv

import pytest

from utils.csv_apply import load_csv_queue, update_csv_application


FIELDNAMES = [
    "company",
    "job_title",
    "job_url",
    "priority",
    "status",
    "resume_variant",
    "skip_reason",
    "blocker",
    "next_action",
    "notes",
]


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _row(company, priority, status, resume="resume.pdf"):
    return {
        "company": company,
        "job_title": f"{company} Engineer",
        "job_url": f"https://example.com/{company.lower()}",
        "priority": priority,
        "status": status,
        "resume_variant": resume,
        "skip_reason": "",
        "blocker": "",
        "next_action": "",
        "notes": "original note",
    }


def test_load_csv_queue_filters_and_sorts_by_requested_order(tmp_path):
    csv_path = tmp_path / "jobs.csv"
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "resume.pdf").write_bytes(b"pdf")
    _write_csv(
        csv_path,
        [
            _row("MediumPending", "Medium", "Pending"),
            _row("LowNeedsUser", "Low", "Needs user"),
            _row("HighPending", "High", "Pending"),
            _row("HighNeedsUser", "High", "Needs user"),
            _row("Submitted", "High", "Submitted"),
        ],
    )

    queue = load_csv_queue(
        csv_path,
        resume_dir,
        priorities="High,Medium",
        statuses="Needs user,Pending",
    )

    assert [item.company for item in queue] == [
        "HighNeedsUser",
        "HighPending",
        "MediumPending",
    ]
    assert all(item.resume_path == (resume_dir / "resume.pdf").resolve() for item in queue)


def test_load_csv_queue_honors_limit(tmp_path):
    csv_path = tmp_path / "jobs.csv"
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    _write_csv(csv_path, [_row("One", "High", "Pending"), _row("Two", "High", "Pending")])

    queue = load_csv_queue(csv_path, resume_dir, limit=1, statuses="Pending")

    assert [item.company for item in queue] == ["One"]


def test_update_csv_application_is_atomic_and_appends_note(tmp_path):
    csv_path = tmp_path / "jobs.csv"
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    _write_csv(csv_path, [_row("Acme", "High", "Pending")])
    application = load_csv_queue(csv_path, resume_dir, statuses="Pending")[0]

    update_csv_application(
        csv_path,
        application,
        {"status": "Submitted", "blocker": "", "next_action": "Monitor email."},
        note="explicit confirmation detected",
    )

    with csv_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["status"] == "Submitted"
    assert row["next_action"] == "Monitor email."
    assert row["notes"] == "original note | explicit confirmation detected"


def test_update_csv_application_rejects_stale_row_identity(tmp_path):
    csv_path = tmp_path / "jobs.csv"
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    _write_csv(csv_path, [_row("Acme", "High", "Pending")])
    application = load_csv_queue(csv_path, resume_dir, statuses="Pending")[0]
    _write_csv(csv_path, [_row("Different", "High", "Pending")])

    with pytest.raises(RuntimeError, match="identity"):
        update_csv_application(csv_path, application, {"status": "Submitted"})


def test_load_csv_queue_validates_schema(tmp_path):
    csv_path = tmp_path / "jobs.csv"
    csv_path.write_text("company,status\nAcme,Pending\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required columns"):
        load_csv_queue(csv_path, tmp_path)
