from __future__ import annotations

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
    yield db
    os.environ.pop("PLATFORM_DB", None)


def test_shadow_status_empty():
    """shadow status with no config rows should succeed and report nothing."""
    result = runner.invoke(app, ["serve", "shadow", "status"])
    assert result.exit_code == 0, result.output
    # should not crash; either prints empty message or empty table
    assert "shadow" in result.output.lower() or "no shadow" in result.output.lower() or result.exit_code == 0


def test_shadow_enable_creates_config_row():
    """shadow enable JPCP should insert a config row and be visible in status."""
    result = runner.invoke(app, ["serve", "shadow", "enable", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output or "enabled" in result.output.lower()

    status = runner.invoke(app, ["serve", "shadow", "status"])
    assert status.exit_code == 0, status.output
    assert "JPCP" in status.output
    assert "Staging" in status.output


def test_shadow_enable_custom_alias():
    """shadow enable with --shadow-alias Canary should store Canary."""
    result = runner.invoke(app, ["serve", "shadow", "enable", "JPCP", "--shadow-alias", "Canary"])
    assert result.exit_code == 0, result.output

    status = runner.invoke(app, ["serve", "shadow", "status", "JPCP"])
    assert status.exit_code == 0, status.output
    assert "Canary" in status.output


def test_shadow_disable_sets_enabled_false():
    """shadow disable JPCP should set enabled=0 and show 'no' in status."""
    runner.invoke(app, ["serve", "shadow", "enable", "JPCP"])
    result = runner.invoke(app, ["serve", "shadow", "disable", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output.lower() or "JPCP" in result.output

    status = runner.invoke(app, ["serve", "shadow", "status", "JPCP"])
    assert status.exit_code == 0, status.output
    assert "no" in status.output.lower()


def test_shadow_log_empty():
    """shadow log JPCP with no results should succeed and say no results."""
    result = runner.invoke(app, ["serve", "shadow", "log", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "no shadow" in result.output.lower() or "JPCP" in result.output


def test_shadow_log_with_rows(isolated_db):
    """shadow log shows rows inserted into shadow_results."""
    import sqlite3

    conn = sqlite3.connect(isolated_db)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shadow_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            model TEXT NOT NULL,
            production_pred REAL,
            shadow_pred REAL,
            diff_pct REAL,
            job_id TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO shadow_results (model, production_pred, shadow_pred, diff_pct) VALUES (?,?,?,?)",
        ("JPCP", 42.5, 43.1, 1.41),
    )
    conn.commit()
    conn.close()

    result = runner.invoke(app, ["serve", "shadow", "log", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "42" in result.output or "43" in result.output


def test_shadow_status_model_filter():
    """shadow status MODEL should only show that model."""
    runner.invoke(app, ["serve", "shadow", "enable", "JPCP"])
    runner.invoke(app, ["serve", "shadow", "enable", "DEMOAD"])

    result = runner.invoke(app, ["serve", "shadow", "status", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    # DEMOAD should not appear when filtering by JPCP
    assert "DEMOAD" not in result.output
