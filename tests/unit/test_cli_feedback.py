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
    p = str(tmp_path / "test_feedback.db")
    os.environ["PLATFORM_DB"] = p
    yield p
    os.environ.pop("PLATFORM_DB", None)
    import importlib

    import examlops.platform_db as m

    importlib.reload(m)


def _seed_predictions():
    from examlops.platform_db import init_db, write_prediction

    init_db()
    write_prediction("JPCP", "Production", "h1", 90.0)
    write_prediction("JPCP", "Production", "h2", 80.0)
    write_prediction("JPCP", "Canary", "h3", 70.0)


def test_ingest_and_accuracy(db_path):
    from examlops.cli.commands import feedback_cmd

    _seed_predictions()
    # label two production predictions with a known error
    r1 = runner.invoke(feedback_cmd.app, ["ingest", "-r", "h1", "-l", "88.0"])
    r2 = runner.invoke(feedback_cmd.app, ["ingest", "-r", "h2", "-l", "83.0"])
    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output

    res = runner.invoke(feedback_cmd.app, ["accuracy", "JPCP", "-a", "Production", "--record"])
    assert res.exit_code == 0, res.output
    assert "rmse" in res.output.lower()

    # RMSE over errors {2.0, 3.0} = sqrt((4+9)/2) ≈ 2.5495
    from examlops.platform_db import get_live_metrics

    rows = {m["metric"]: m["value"] for m in get_live_metrics("JPCP", alias="Production")}
    assert abs(rows["rmse"] - 2.549509) < 1e-4
    assert abs(rows["mae"] - 2.5) < 1e-6


def test_accuracy_no_labels_is_graceful(db_path):
    from examlops.cli.commands import feedback_cmd

    _seed_predictions()
    res = runner.invoke(feedback_cmd.app, ["accuracy", "JPCP"])
    assert res.exit_code == 0
    assert "no labelled predictions" in res.output.lower()


def test_ingest_requires_args(db_path):
    from examlops.cli.commands import feedback_cmd
    from examlops.platform_db import init_db

    init_db()
    res = runner.invoke(feedback_cmd.app, ["ingest"])
    assert res.exit_code == 1


def test_ingest_from_csv(db_path, tmp_path):
    from examlops.cli.commands import feedback_cmd

    _seed_predictions()
    csv_file = tmp_path / "labels.csv"
    csv_file.write_text("request_hash,label,source\nh1,88.0,slurm\nh3,71.0,slurm\n")
    res = runner.invoke(feedback_cmd.app, ["ingest", "--from-csv", str(csv_file)])
    assert res.exit_code == 0, res.output
    assert "Ingested" in res.output

    join = runner.invoke(feedback_cmd.app, ["join", "JPCP"])
    assert join.exit_code == 0
    assert "h1" in join.output
    assert "h3" in join.output


def test_join_empty(db_path):
    from examlops.cli.commands import feedback_cmd

    _seed_predictions()
    res = runner.invoke(feedback_cmd.app, ["join", "JPCP"])
    assert res.exit_code == 0
    assert "no labelled predictions" in res.output.lower()
