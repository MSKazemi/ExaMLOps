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
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    from examlops.platform_db import init_db

    init_db()
    # Ensure A/B tables exist (created lazily by the command itself)
    yield
    os.environ.pop("PLATFORM_DB", None)


# ------------------------------------------------------------------
# 1. ab status with no tests returns empty/ok message
# ------------------------------------------------------------------
def test_ab_status_empty():
    result = runner.invoke(app, ["serve", "ab", "status"])
    assert result.exit_code == 0, result.output
    # Either a "No A/B tests found" message or an empty table
    output_lower = result.output.lower()
    assert "no a/b tests" in output_lower or "no" in output_lower or "a/b" in output_lower


# ------------------------------------------------------------------
# 2. ab start creates a new test
# ------------------------------------------------------------------
def test_ab_start_creates_test():
    result = runner.invoke(app, ["serve", "ab", "start", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "A/B test started" in result.output or "started" in result.output.lower()
    assert "JPCP" in result.output
    assert "Production" in result.output
    assert "Canary" in result.output


# ------------------------------------------------------------------
# 3. ab start with an already-active test returns exit 1
# ------------------------------------------------------------------
def test_ab_start_duplicate_fails():
    # First start should succeed
    first = runner.invoke(app, ["serve", "ab", "start", "JPCP"])
    assert first.exit_code == 0, first.output

    # Second start for same model must fail
    second = runner.invoke(app, ["serve", "ab", "start", "JPCP"])
    assert second.exit_code != 0, second.output
    combined = second.output + (
        second.stderr if hasattr(second, "stderr") and second.stderr else ""
    )
    assert "active" in combined.lower() or "already exists" in combined.lower()


# ------------------------------------------------------------------
# 4. ab stop marks the test as completed
# ------------------------------------------------------------------
def test_ab_stop_marks_completed():
    runner.invoke(app, ["serve", "ab", "start", "JPCP"])
    stop_result = runner.invoke(app, ["serve", "ab", "stop", "JPCP"])
    assert stop_result.exit_code == 0, stop_result.output
    assert "completed" in stop_result.output.lower()

    # Verify in the DB
    from examlops.platform_db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT status FROM ab_tests WHERE model='JPCP'").fetchone()
    assert row is not None
    assert row["status"] == "completed"


# ------------------------------------------------------------------
# 5. ab status shows the completed test
# ------------------------------------------------------------------
def test_ab_status_shows_completed_test():
    runner.invoke(app, ["serve", "ab", "start", "JPCP"])
    runner.invoke(app, ["serve", "ab", "stop", "JPCP"])

    result = runner.invoke(app, ["serve", "ab", "status"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "completed" in result.output.lower()


# ------------------------------------------------------------------
# 6. ab status with model filter only shows that model
# ------------------------------------------------------------------
def test_ab_status_model_filter():
    runner.invoke(app, ["serve", "ab", "start", "JPCP"])
    runner.invoke(app, ["serve", "ab", "start", "DEMO"])

    result = runner.invoke(app, ["serve", "ab", "status", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    # DEMO should not appear when filtering by JPCP
    assert "DEMO" not in result.output


# ------------------------------------------------------------------
# 7. ab record stores a metric observation
# ------------------------------------------------------------------
def test_ab_record_stores_observation():
    runner.invoke(app, ["serve", "ab", "start", "JPCP"])
    result = runner.invoke(app, ["serve", "ab", "record", "JPCP", "Production", "0.95"])
    assert result.exit_code == 0, result.output
    assert "recorded" in result.output.lower() or "0.95" in result.output

    from examlops.platform_db import get_db

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM ab_results").fetchall()
    assert len(rows) == 1
    assert rows[0]["variant"] == "Production"
    assert abs(rows[0]["value"] - 0.95) < 1e-9


# ------------------------------------------------------------------
# 8. ab record with no active test returns exit 1
# ------------------------------------------------------------------
def test_ab_record_no_active_test_fails():
    result = runner.invoke(app, ["serve", "ab", "record", "JPCP", "Canary", "0.80"])
    assert result.exit_code != 0, result.output


# ------------------------------------------------------------------
# 9. ab status --json returns valid JSON list
# ------------------------------------------------------------------
def test_ab_status_json_mode():
    runner.invoke(app, ["serve", "ab", "start", "JPCP"])
    result = runner.invoke(app, ["--json", "serve", "ab", "status"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data[0]["model"] == "JPCP"
    assert data[0]["status"] == "running"


# ------------------------------------------------------------------
# 10. ab start with custom split and name
# ------------------------------------------------------------------
def test_ab_start_custom_options():
    result = runner.invoke(
        app,
        [
            "serve",
            "ab",
            "start",
            "JPCP",
            "--variant-a",
            "Production",
            "--variant-b",
            "Staging",
            "--split",
            "70",
            "--name",
            "exp-001",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "70" in result.output or "30" in result.output

    from examlops.platform_db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT * FROM ab_tests WHERE model='JPCP'").fetchone()
    assert row["split_pct"] == 70
    assert row["name"] == "exp-001"
    assert row["variant_b"] == "Staging"
