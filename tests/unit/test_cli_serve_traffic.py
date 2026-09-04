from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

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
    yield
    os.environ.pop("PLATFORM_DB", None)


def test_traffic_set_writes_to_db():
    with patch("examlops.cli.commands.serve._client.post", return_value={"ok": True}):
        result = runner.invoke(
            app,
            [
                "--yes",
                "serve",
                "traffic",
                "JPCP",
                "--production",
                "90",
                "--canary",
                "10",
            ],
        )
    assert result.exit_code == 0, result.output
    from examlops.platform_db import get_traffic_rules

    rules = get_traffic_rules("JPCP")
    assert rules == {"Production": 90, "Canary": 10}


def test_traffic_set_must_sum_to_100():
    result = runner.invoke(
        app,
        [
            "serve",
            "traffic",
            "JPCP",
            "--production",
            "70",
            "--canary",
            "10",
        ],
    )
    assert result.exit_code != 0 or "100" in result.output


def test_traffic_show_with_no_rules():
    result = runner.invoke(app, ["serve", "traffic", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "No traffic rules" in result.output


def test_traffic_show_existing_rules():
    from examlops.platform_db import set_traffic_rules

    set_traffic_rules("JPCP", {"Production": 80, "Canary": 20}, "alice")
    result = runner.invoke(app, ["serve", "traffic", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "80" in result.output
    assert "Canary" in result.output


def test_traffic_json_mode():
    from examlops.platform_db import set_traffic_rules

    set_traffic_rules("JPCP", {"Production": 100}, "alice")
    result = runner.invoke(app, ["--json", "serve", "traffic", "JPCP"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["Production"] == 100


def test_traffic_dry_run_changes_nothing():
    from examlops.platform_db import get_traffic_rules

    result = runner.invoke(
        app, ["serve", "traffic", "JPCP", "--production", "70", "--canary", "30", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "dry run" in result.output.lower()
    assert get_traffic_rules("JPCP") is None  # nothing persisted


def test_traffic_list_renders_and_has_watch_option():
    from examlops.platform_db import set_traffic_rules

    set_traffic_rules("JPCP", {"Production": 90, "Canary": 10}, "bob")
    result = runner.invoke(app, ["serve", "traffic-list"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output and "Canary" in result.output
    help_result = runner.invoke(app, ["serve", "traffic-list", "--help"])
    assert "--watch" in help_result.output


class TestUpsertSemantics:
    """C4 regression guards: upserts must behave identically on SQLite and Postgres."""

    def test_reconfigure_auto_retrain_preserves_cooldown(self, tmp_path, monkeypatch):
        from examlops import platform_db as pdb

        monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
        pdb.init_db(force=True)
        pdb.set_drift_auto_retrain("jpcp", True, min_z_score=3.0, dataset_name="D")
        with pdb.get_db() as conn:
            conn.execute(
                "UPDATE drift_auto_retrain SET last_triggered=CURRENT_TIMESTAMP WHERE model='jpcp'"
            )
        # Re-configuring (e.g. changing the threshold) must NOT erase the cooldown stamp —
        # INSERT OR REPLACE did, making the model instantly re-eligible to retrain.
        pdb.set_drift_auto_retrain("jpcp", True, min_z_score=2.0, dataset_name="D")
        with pdb.get_db() as conn:
            row = conn.execute(
                "SELECT min_z_score, last_triggered FROM drift_auto_retrain WHERE model='jpcp'"
            ).fetchone()
        assert row["min_z_score"] == 2.0
        assert row["last_triggered"] is not None, "cooldown stamp must survive reconfiguration"

    def test_traffic_rules_update_stamps_updated_at(self, tmp_path, monkeypatch):
        from examlops import platform_db as pdb

        monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
        pdb.init_db(force=True)
        pdb.set_traffic_rules("jpcp", {"Production": 100}, updated_by="a")
        with pdb.get_db() as conn:
            conn.execute(
                "UPDATE traffic_rules SET updated_at='2020-01-01 00:00:00' WHERE model='jpcp'"
            )
        pdb.set_traffic_rules("jpcp", {"Production": 90, "Canary": 10}, updated_by="b")
        with pdb.get_db() as conn:
            row = conn.execute(
                "SELECT rules, updated_at, updated_by FROM traffic_rules WHERE model='jpcp'"
            ).fetchone()
        assert row["updated_at"] != "2020-01-01 00:00:00", "rule change must stamp updated_at"
        assert row["updated_by"] == "b"
