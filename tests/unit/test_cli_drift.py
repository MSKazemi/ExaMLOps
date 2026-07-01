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
    yield db
    os.environ.pop("PLATFORM_DB", None)


def _insert_snapshots(predictions, model="JPCP", alias="Production"):
    from examlops.platform_db import write_drift_snapshot

    for p in predictions:
        write_drift_snapshot(model, alias, p, None)


def test_drift_status_no_snapshots_says_no_data():
    result = runner.invoke(app, ["drift", "status"])
    assert result.exit_code == 0, result.output
    output_lower = result.output.lower()
    assert "no data" in output_lower or "no drift" in output_lower


def test_drift_status_ok_within_threshold():
    baseline_preds = [89.0 + i * 0.01 for i in range(100)]
    _insert_snapshots(baseline_preds)
    runner.invoke(app, ["drift", "baseline", "JPCP"])
    _insert_snapshots([89.1] * 20)
    result = runner.invoke(app, ["drift", "status", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_drift_status_warning_beyond_2sigma():
    # Use a non-zero std baseline so z-score can be computed
    import random

    random.seed(42)
    baseline_preds = [89.0 + random.gauss(0, 2) for _ in range(100)]
    _insert_snapshots(baseline_preds)
    runner.invoke(app, ["drift", "baseline", "JPCP"])
    # Insert predictions far from baseline mean
    _insert_snapshots([150.0] * 20)
    result = runner.invoke(app, ["drift", "status", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "WARNING" in result.output or "CRITICAL" in result.output


def test_drift_baseline_stores_stats():
    _insert_snapshots([90.0, 91.0, 92.0, 89.0, 88.0] * 20)
    result = runner.invoke(app, ["drift", "baseline", "JPCP"])
    assert result.exit_code == 0, result.output
    from examlops.platform_db import get_drift_baseline

    b = get_drift_baseline("JPCP")
    assert b is not None
    assert "mean" in b
    assert "std" in b


def test_drift_reset_clears_snapshots():
    _insert_snapshots([89.0] * 10)
    result = runner.invoke(app, ["drift", "reset", "JPCP"])
    assert result.exit_code == 0, result.output
    from examlops.platform_db import get_db

    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM drift_snapshots WHERE model='JPCP'").fetchone()[
            0
        ]
    assert count == 0


def test_drift_status_json_mode():
    _insert_snapshots([89.0] * 50)
    runner.invoke(app, ["drift", "baseline", "JPCP"])
    _insert_snapshots([89.5] * 20)
    result = runner.invoke(app, ["--json", "drift", "status"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data[0]["model"] == "JPCP"
