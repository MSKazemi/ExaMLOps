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
