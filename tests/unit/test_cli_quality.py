from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    db = str(tmp_path / "test.db")
    os.environ["PLATFORM_DB"] = db
    from examlops.platform_db import init_db

    init_db()
    # Also create the quality table
    from examlops.cli.commands.quality_cmd import _init_quality_table

    _init_quality_table()
    yield tmp_path
    os.environ.pop("PLATFORM_DB", None)


# ── Test 1: history on empty DB ───────────────────────────────────────────────


def test_quality_history_empty(isolated_db):
    result = runner.invoke(app, ["pipeline", "quality", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    # Empty: should print info message, not crash
    assert "No quality checks" in result.output or result.exit_code == 0


# ── Test 2: check with no data dir → status "warn" ────────────────────────────


def test_quality_check_no_dir(isolated_db):
    """When .data_cache/<dataset> does not exist the status must be 'warn'."""
    result = runner.invoke(app, ["pipeline", "quality", "check", "JPCP", "PM100Dataset"])
    assert result.exit_code == 0, result.output
    assert "WARN" in result.output or "warn" in result.output.lower()

    # Verify it was recorded in DB
    from examlops import platform_db

    with platform_db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM data_quality_checks WHERE model=? AND dataset=?",
            ("JPCP", "PM100Dataset"),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "warn"


# ── Test 3: check with existing dir + files → status "pass" ──────────────────


def test_quality_check_with_files(isolated_db):
    """When .data_cache/<dataset>/ has parquet files the status must be 'pass'."""
    tmp_path: Path = isolated_db

    # Create .data_cache/PM100Dataset/ next to the DB file
    data_dir = tmp_path / ".data_cache" / "PM100Dataset"
    data_dir.mkdir(parents=True)
    (data_dir / "train.parquet").write_bytes(b"fake parquet data")
    (data_dir / "val.parquet").write_bytes(b"fake parquet data")

    result = runner.invoke(app, ["pipeline", "quality", "check", "JPCP", "PM100Dataset"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output or "pass" in result.output.lower()

    from examlops import platform_db

    with platform_db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM data_quality_checks WHERE model=? AND dataset=?",
            ("JPCP", "PM100Dataset"),
        ).fetchone()
    assert row is not None
    assert row["status"] == "pass"
    assert row["passed"] == 3  # all three checks pass
    assert row["failed"] == 0


# ── Test 4: history shows records after checks ────────────────────────────────


def test_quality_history_after_checks(isolated_db):
    """quality history shows rows recorded by quality check."""
    tmp_path: Path = isolated_db

    # First run: no dir → warn
    runner.invoke(app, ["pipeline", "quality", "check", "JPCP", "DatasetA"])

    # Second run: dir with files → pass
    data_dir = tmp_path / ".data_cache" / "DatasetB"
    data_dir.mkdir(parents=True)
    (data_dir / "data.json").write_text('{"records": []}')
    runner.invoke(app, ["pipeline", "quality", "check", "JPCP", "DatasetB"])

    result = runner.invoke(app, ["pipeline", "quality", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "DatasetA" in result.output
    assert "DatasetB" in result.output
    # Both statuses should appear
    assert "WARN" in result.output or "warn" in result.output.lower()
    assert "PASS" in result.output or "pass" in result.output.lower()


# ── Test 5: JSON mode output ──────────────────────────────────────────────────


def test_quality_check_json_mode(isolated_db):
    """--json flag must produce a parseable JSON list for the check table."""
    result = runner.invoke(app, ["--json", "pipeline", "quality", "check", "MACK", "FakeDS"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    # Each row should have Check, Status, Detail keys
    assert all("Check" in item for item in data)


# ── Test 6: history JSON mode ─────────────────────────────────────────────────


def test_quality_history_json_mode(isolated_db):
    """--json flag returns a list of history records."""
    runner.invoke(app, ["pipeline", "quality", "check", "JPCP", "PM100Dataset"])
    result = runner.invoke(app, ["--json", "pipeline", "quality", "history", "JPCP"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "Dataset" in data[0]
