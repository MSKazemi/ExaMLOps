from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()

_ALIAS_DATA = {
    "registered_model": {
        "aliases": [{"alias": "Production", "version": "18"}],
        "latest_versions": [],
    }
}
_VER_DATA = {"model_version": {"run_id": "run-abc", "version": "18", "creation_timestamp": 1700000000000}}
_RUN_DATA = {
    "run": {
        "data": {
            "metrics": [{"key": "rmse", "value": 4.9}],
            "params": [{"key": "n_estimators", "value": "200"}],
            "tags": [
                {"key": "prefect_flow_run_id", "value": "prefect-xyz"},
                {"key": "dataset_version", "value": "2026-04-15"},
                {"key": "training_rows", "value": "48291"},
            ],
        }
    }
}


def _get_side_effect(url, **kwargs):
    if "registered-models/get" in url:
        return _ALIAS_DATA
    if "model-versions/get" in url:
        return _VER_DATA
    if "runs/get" in url:
        return _RUN_DATA
    return {}


def test_lineage_shows_version_and_run():
    with patch("examlops.cli.commands.models._client.get", side_effect=_get_side_effect):
        result = runner.invoke(app, ["models", "lineage", "jpcp"])
    assert result.exit_code == 0, result.output
    assert "18" in result.output
    assert "run-abc" in result.output


def test_lineage_shows_prefect_run_id():
    with patch("examlops.cli.commands.models._client.get", side_effect=_get_side_effect):
        result = runner.invoke(app, ["models", "lineage", "jpcp"])
    assert "prefect-xyz" in result.output


def test_lineage_shows_dataset_version():
    with patch("examlops.cli.commands.models._client.get", side_effect=_get_side_effect):
        result = runner.invoke(app, ["models", "lineage", "jpcp"])
    assert "2026-04-15" in result.output


def test_lineage_json_mode():
    with patch("examlops.cli.commands.models._client.get", side_effect=_get_side_effect):
        result = runner.invoke(app, ["--json", "models", "lineage", "jpcp"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["model_version"] == "18"
    assert data["run_id"] == "run-abc"
