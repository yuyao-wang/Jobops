from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from dashboard.server import run_server


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "dashboard.test"])
def test_production_dashboard_refuses_non_loopback_bind(host: str) -> None:
    with pytest.raises(RuntimeError, match="permits only.*loopback"):
        run_server(host=host, port=8080)


@pytest.mark.parametrize(
    "arguments",
    [
        ("apply", "--live"),
        ("single", "https://example.test/jobs/1", "--live"),
        (
            "apply-csv",
            "synthetic.csv",
            "--resume-dir",
            "synthetic-resumes",
            "--live",
        ),
    ],
)
def test_legacy_live_apply_cli_fails_before_loading_private_profile(
    arguments: tuple[str, ...],
) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "legacy live application execution is disabled" in result.stderr
    assert "jobctl.py" in result.stderr
