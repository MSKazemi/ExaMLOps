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
    yield
    os.environ.pop("PLATFORM_DB", None)


def _seed(n=3):
    from examlops.platform_db import write_audit_event
    for i in range(n):
        write_audit_event("cli", "alice", f"action_{i}", "JPCP", {"i": i})


def test_audit_shows_events():
    _seed(3)
    result = runner.invoke(app, ["audit"])
    assert result.exit_code == 0, result.output
    assert "action_0" in result.output


def test_audit_empty_shows_no_events():
    result = runner.invoke(app, ["audit"])
    assert result.exit_code == 0, result.output
    assert "No audit events" in result.output


def test_audit_filter_by_model():
    from examlops.platform_db import write_audit_event
    write_audit_event("cli", "alice", "retrain_triggered", "JPCP", {})
    write_audit_event("cli", "bob", "retrain_triggered", "MACK", {})
    result = runner.invoke(app, ["audit", "--model", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "MACK" not in result.output


def test_audit_filter_by_action():
    from examlops.platform_db import write_audit_event
    write_audit_event("cli", "alice", "model_approved", "JPCP", {})
    write_audit_event("cli", "alice", "retrain_triggered", "JPCP", {})
    result = runner.invoke(app, ["audit", "--action", "model_approved"])
    assert result.exit_code == 0, result.output
    assert "model_approved" in result.output
    assert "retrain_triggered" not in result.output


def test_audit_json_mode():
    _seed(2)
    result = runner.invoke(app, ["--json", "audit"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 2
    assert "action" in data[0]
