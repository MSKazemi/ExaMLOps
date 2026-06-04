from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from typer.testing import CliRunner

from examlops.cli.main import app

runner = CliRunner()

_V17_META = {"model_version": {"run_id": "run-v17", "version": "17"}}
_V18_META = {"model_version": {"run_id": "run-v18", "version": "18"}}
_RUN_V17 = {"run": {"data": {"metrics": {"rmse": 6.1, "mae": 4.0}, "params": {"n_estimators": "100"}}}}
_RUN_V18 = {"run": {"data": {"metrics": {"rmse": 4.9, "mae": 3.8}, "params": {"n_estimators": "200"}}}}


def _get_side_effect(url, **kwargs):
    if "version=17" in url:
        return _V17_META
    if "version=18" in url:
        return _V18_META
    if "run-v17" in url:
        return _RUN_V17
    if "run-v18" in url:
        return _RUN_V18
    return {}


def test_diff_shows_metric_comparison():
    with patch("examlops.cli.commands.models._client.get", side_effect=_get_side_effect):
        result = runner.invoke(app, ["models", "diff", "jpcp", "17", "18"])
    assert result.exit_code == 0, result.output
    assert "rmse" in result.output
    assert "6.1" in result.output
    assert "4.9" in result.output


def test_diff_json_mode():
    with patch("examlops.cli.commands.models._client.get", side_effect=_get_side_effect):
        result = runner.invoke(app, ["--json", "models", "diff", "jpcp", "17", "18"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "metrics" in data
    assert data["metrics"]["rmse"]["v1"] == 6.1


def test_diff_shows_param_comparison():
    with patch("examlops.cli.commands.models._client.get", side_effect=_get_side_effect):
        result = runner.invoke(app, ["models", "diff", "jpcp", "17", "18"])
    assert "n_estimators" in result.output


def test_diff_client_error_exits_gracefully():
    from examlops.cli._client import ClientError
    with patch("examlops.cli.commands.models._client.get", side_effect=ClientError("HTTP 404")):
        result = runner.invoke(app, ["models", "diff", "jpcp", "17", "18"])
    assert result.exit_code != 0
