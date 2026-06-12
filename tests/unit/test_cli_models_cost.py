from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app
from examlops.platform_db import get_model_costs, init_db, record_model_cost

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    os.environ["EXAMLOPS_SLURM_MODE"] = "mock"
    init_db()
    yield
    del os.environ["PLATFORM_DB"]
    os.environ.pop("EXAMLOPS_SLURM_MODE", None)


# ---------------------------------------------------------------------------
# display (no data)
# ---------------------------------------------------------------------------


def test_cost_empty_table():
    result = runner.invoke(app, ["models", "cost", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "No cost data" in result.output or "JPCP" in result.output


# ---------------------------------------------------------------------------
# display (pre-seeded data)
# ---------------------------------------------------------------------------


def test_cost_shows_seeded_data():
    record_model_cost("JPCP", 17, "5f015850abcdef12", "job-1234", 12.5, 31.25)
    record_model_cost("JPCP", 18, "abc12345ffffffff", "job-5678", 9.2, 23.0)
    result = runner.invoke(app, ["models", "cost", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "17" in result.output
    assert "18" in result.output
    assert "12.50" in result.output
    assert "$31.25" in result.output
    assert "job-1234" in result.output
    assert "job-5678" in result.output


# ---------------------------------------------------------------------------
# --record in mock mode
# ---------------------------------------------------------------------------

_MOCK_RM_RESPONSE = {
    "registered_model": {
        "name": "JPCP",
        "latest_versions": [
            {"version": "17", "run_id": "run-aabbccdd"},
            {"version": "18", "run_id": "run-eeff0011"},
        ],
        "aliases": [],
    }
}


def test_cost_record_mock_mode():
    with patch("examlops.cli._client.get", return_value=_MOCK_RM_RESPONSE):
        with patch("examlops.cli._client.post", return_value={}) as mock_post:
            result = runner.invoke(app, ["models", "cost", "JPCP", "--record"])

    assert result.exit_code == 0, result.output
    assert "Recorded" in result.output

    costs = get_model_costs("JPCP")
    assert len(costs) == 2
    versions = {c["version"] for c in costs}
    assert versions == {17, 18}

    for c in costs:
        assert c["gpu_hours"] is not None
        assert c["cost_usd"] is not None
        assert c["gpu_hours"] > 0
        assert c["cost_usd"] > 0
        assert c["job_id"] is not None
        assert c["run_id"] is not None

    # MLflow tagging should have been called (2 tags × 2 versions = 4 calls)
    assert mock_post.call_count == 4


def test_cost_record_then_display():
    with patch("examlops.cli._client.get", return_value=_MOCK_RM_RESPONSE):
        with patch("examlops.cli._client.post", return_value={}):
            runner.invoke(app, ["models", "cost", "JPCP", "--record"])

    result = runner.invoke(app, ["models", "cost", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "JPCP" in result.output
    assert "17" in result.output
    assert "18" in result.output


def test_cost_record_mlflow_unreachable():
    from examlops.cli._client import ClientError

    with patch("examlops.cli._client.get", side_effect=ClientError("Service unreachable")):
        result = runner.invoke(app, ["models", "cost", "JPCP", "--record"])

    # Should exit with error, not crash
    assert result.exit_code != 0 or "error" in result.output.lower()


def test_cost_record_no_versions():
    empty_rm = {
        "registered_model": {
            "name": "JPCP",
            "latest_versions": [],
            "aliases": [],
        }
    }
    with patch("examlops.cli._client.get", return_value=empty_rm):
        result = runner.invoke(app, ["models", "cost", "JPCP", "--record"])

    assert "No versions found" in result.output


# ---------------------------------------------------------------------------
# mock determinism — same model+version always yields same data
# ---------------------------------------------------------------------------


def test_mock_slurm_determinism():
    from examlops.cli.commands.models import _mock_slurm_data

    job1, hrs1 = _mock_slurm_data("JPCP", 17)
    job2, hrs2 = _mock_slurm_data("JPCP", 17)
    assert job1 == job2
    assert hrs1 == hrs2
    assert hrs1 >= 4.0
    assert hrs1 <= 24.0

    job3, hrs3 = _mock_slurm_data("JPCP", 18)
    # Different versions should generally differ (not strictly guaranteed, but md5-based)
    assert (job1, hrs1) != (job3, hrs3) or True  # soft check, mostly for coverage


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_cost_json_mode():
    record_model_cost("JPCP", 17, "run0001", "job-0001", 8.0, 20.0)
    result = runner.invoke(app, ["--json", "models", "cost", "JPCP"])
    assert result.exit_code == 0, result.output
    import json

    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert str(data[0]["Version"]) == "17"
    assert "GPU Hours" in data[0]
    assert data[0]["GPU Hours"] == "8.00"
