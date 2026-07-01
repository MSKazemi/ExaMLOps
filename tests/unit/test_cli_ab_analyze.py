from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

runner = CliRunner()


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "test_ab.db")
    os.environ["PLATFORM_DB"] = p
    yield p
    os.environ.pop("PLATFORM_DB", None)
    import importlib

    import examlops.platform_db as m

    importlib.reload(m)


def _seed_test(model="JPCP", a_vals=None, b_vals=None):
    from examlops.cli.commands import ab_cmd

    runner.invoke(ab_cmd.app, ["start", model])
    for v in a_vals or []:
        runner.invoke(ab_cmd.app, ["record", model, "Production", str(v)])
    for v in b_vals or []:
        runner.invoke(ab_cmd.app, ["record", model, "Canary", str(v)])


def test_analyze_no_test():
    from examlops.cli.commands import ab_cmd

    res = runner.invoke(ab_cmd.app, ["analyze", "NOPE"])
    assert res.exit_code == 1


def test_analyze_insufficient_sample(db_path):
    from examlops.cli.commands import ab_cmd

    _seed_test(a_vals=[0.9, 0.91], b_vals=[0.8, 0.81])
    res = runner.invoke(ab_cmd.app, ["analyze", "JPCP"])
    assert res.exit_code == 0
    assert "insufficient sample" in res.output.lower()


def test_analyze_significant_winner(db_path):
    from examlops.cli.commands import ab_cmd

    a = [0.90 + (i % 3) * 0.01 for i in range(35)]
    b = [0.80 + (i % 3) * 0.01 for i in range(35)]
    _seed_test(a_vals=a, b_vals=b)
    res = runner.invoke(ab_cmd.app, ["analyze", "JPCP"])
    assert res.exit_code == 0
    assert "Production" in res.output  # winner (higher-is-better)
    assert "p-value" in res.output.lower() or "p-value" in res.output


def test_analyze_json_mode(db_path):
    import json

    from examlops.cli import _output
    from examlops.cli.commands import ab_cmd

    a = [1.0 + (i % 3) * 0.01 for i in range(35)]
    b = [2.0 + (i % 3) * 0.01 for i in range(35)]
    _seed_test(a_vals=a, b_vals=b)
    _output.json_mode = True
    try:
        res = runner.invoke(ab_cmd.app, ["analyze", "JPCP", "--lower-is-better"])
    finally:
        _output.json_mode = False
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["significant"] is True
    assert payload["winner"] == "a"  # Production has lower mean, lower-is-better
