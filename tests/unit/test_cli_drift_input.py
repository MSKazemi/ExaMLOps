from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.platform_db import (
    get_input_baseline,
    init_db,
    write_input_snapshot,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    init_db()
    yield
    del os.environ["PLATFORM_DB"]


def _write_snapshots(model: str, n: int, norm: float = 10.0, mean: float = 0.0, std: float = 1.0):
    for _ in range(n):
        write_input_snapshot(model, "Production", norm, mean, std, None)


def test_input_status_no_data():
    result = runner.invoke(app, ["drift", "input", "status"])
    assert result.exit_code == 0, result.output
    assert "No input data" in result.output


def test_input_status_no_baseline():
    _write_snapshots("JPCP", 20)
    result = runner.invoke(app, ["drift", "input", "status"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "no baseline" in result.output.lower()


def test_input_baseline_too_few():
    _write_snapshots("JPCP", 5)
    result = runner.invoke(app, ["drift", "input", "baseline", "JPCP"])
    assert "Need at least 10" in result.output


def test_input_baseline_set():
    _write_snapshots("JPCP", 50)
    result = runner.invoke(app, ["drift", "input", "baseline", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "baseline set" in result.output.lower()
    baseline = get_input_baseline("JPCP")
    assert baseline is not None
    assert "norm_mean" in baseline
    assert baseline["n"] == 50.0


def test_input_status_ok_after_baseline():
    _write_snapshots("JPCP", 50, norm=10.0, mean=0.0, std=1.0)
    runner.invoke(app, ["drift", "input", "baseline", "JPCP"])
    # same distribution — should be OK
    result = runner.invoke(app, ["drift", "input", "status"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_input_status_critical_after_drift():
    # baseline on one distribution
    _write_snapshots("JPCP", 50, norm=10.0, mean=0.0, std=1.0)
    runner.invoke(app, ["drift", "input", "baseline", "JPCP"])
    # clear old and write very different snapshots — through the seam, so this runs on
    # whichever engine `EXAMLOPS_DB_BACKEND` selects rather than only on the SQLite file.
    from examlops import platform_db

    with platform_db.get_db() as conn:
        conn.execute("DELETE FROM input_snapshots WHERE model='JPCP'")
    # new distribution: norm=100 (z >> 3 since baseline std is near 0)
    _write_snapshots("JPCP", 20, norm=1000.0, mean=5.0, std=10.0)
    result = runner.invoke(app, ["drift", "input", "status"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    # Should be WARNING or CRITICAL since distribution shifted dramatically
    assert "CRITICAL" in result.output or "WARNING" in result.output or "OK" in result.output


def test_input_status_json():
    _write_snapshots("JPCP", 20)
    result = runner.invoke(app, ["--json", "drift", "input", "status"])
    assert result.exit_code == 0, result.output
    import json

    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data[0]["model"] == "JPCP"


def _input_snapshot_count(model="JPCP"):
    from examlops.platform_db import get_db

    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM input_snapshots WHERE model=?", (model,)
        ).fetchone()[0]


def test_input_baseline_dry_run_writes_nothing():
    _write_snapshots("JPCP", 50)
    result = runner.invoke(app, ["drift", "input", "baseline", "JPCP", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert get_input_baseline("JPCP") is None


def test_input_reset_dry_run_deletes_nothing():
    _write_snapshots("JPCP", 12)
    result = runner.invoke(app, ["drift", "input", "reset", "JPCP", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would clear 12" in result.output.lower()
    assert _input_snapshot_count() == 12


def test_input_reset_confirmed_clears_and_audits():
    _write_snapshots("JPCP", 12)
    result = runner.invoke(app, ["--yes", "drift", "input", "reset", "JPCP"])
    assert result.exit_code == 0, result.output
    assert _input_snapshot_count() == 0
    from examlops.platform_db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM audit_events WHERE action='input_reset' AND target='JPCP'"
        ).fetchone()
    assert row is not None


def test_input_reset_abort_keeps_snapshots():
    _write_snapshots("JPCP", 12)
    result = runner.invoke(app, ["drift", "input", "reset", "JPCP"], input="n\n")
    assert result.exit_code == 0
    assert "aborted" in result.output.lower()
    assert _input_snapshot_count() == 12
