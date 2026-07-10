from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

runner = CliRunner()


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "test_finops.db")
    os.environ["PLATFORM_DB"] = p
    yield p
    os.environ.pop("PLATFORM_DB", None)
    import importlib

    import examlops.platform_db as m

    importlib.reload(m)


def test_budget_set_requires_a_value(db_path):
    from examlops.cli.commands import finops_cmd

    res = runner.invoke(finops_cmd.app, ["budget", "set", "eu-hpc"])
    assert res.exit_code == 1


def test_budget_status_reflects_consumption(db_path):
    from examlops.cli.commands import finops_cmd
    from examlops.platform_db import get_db, init_db, record_model_cost, set_project_budget

    init_db()
    set_project_budget("eu-hpc", gpu_hours_budget=100.0, cost_budget=1000.0)
    # assign a model to the namespace and record spend against it
    with get_db() as conn:
        conn.execute(
            "INSERT INTO namespace_models (model, namespace) VALUES (?, ?)", ("JPCP", "eu-hpc")
        )
    record_model_cost("JPCP", 1, "run-1", "job-1", gpu_hours=60.0, cost_usd=300.0)
    record_model_cost("JPCP", 2, "run-2", "job-2", gpu_hours=50.0, cost_usd=250.0)

    from examlops.cli import _output

    _output.json_mode = True
    try:
        res = runner.invoke(finops_cmd.app, ["budget", "status", "eu-hpc"])
    finally:
        _output.json_mode = False
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)[0]
    # 110 GPU-h consumed vs 100 budget → OVER
    assert payload["status"] == "OVER"
    assert "110" in payload["gpu_hours"]


def test_budget_status_no_budgets(db_path):
    from examlops.cli.commands import finops_cmd
    from examlops.platform_db import init_db

    init_db()
    res = runner.invoke(finops_cmd.app, ["budget", "status"])
    assert res.exit_code == 0
    assert "no project budgets" in res.output.lower()


def test_carbon_estimate(db_path):
    from examlops.cli.commands import finops_cmd

    res = runner.invoke(finops_cmd.app, ["carbon", "estimate", "--gpu-hours", "10"])
    assert res.exit_code == 0
    assert "6.0" in res.output  # 6 kWh
    assert "1800" in res.output  # 1800 gCO2e


def test_carbon_record_and_report(db_path):
    from examlops.cli.commands import finops_cmd

    r1 = runner.invoke(finops_cmd.app, ["carbon", "record", "JPCP", "--gpu-hours", "10"])
    r2 = runner.invoke(finops_cmd.app, ["carbon", "record", "JPCP", "--gpu-hours", "5"])
    assert r1.exit_code == 0 and r2.exit_code == 0

    from examlops.cli import _output

    _output.json_mode = True
    try:
        res = runner.invoke(finops_cmd.app, ["carbon", "report", "--model", "JPCP"])
    finally:
        _output.json_mode = False
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["n"] == 2
    # 15 GPU-h total → 9 kWh → 2700 gCO2e
    assert payload["total_kwh"] == pytest.approx(9.0)
    assert payload["total_co2e_g"] == pytest.approx(2700.0)


def test_carbon_report_empty(db_path):
    from examlops.cli.commands import finops_cmd
    from examlops.platform_db import init_db

    init_db()
    res = runner.invoke(finops_cmd.app, ["carbon", "report"])
    assert res.exit_code == 0
    assert "no carbon records" in res.output.lower()
