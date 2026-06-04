from __future__ import annotations

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
def isolate_db(tmp_path):
    os.environ["PLATFORM_DB"] = str(tmp_path / "test.db")
    yield
    del os.environ["PLATFORM_DB"]


def test_validate_model_pass():
    with patch("examlops.cli._client.post", return_value={"prediction": 42.0}) as mock_post:
        result = runner.invoke(app, ["pipeline", "validate-model", "JPCP"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert mock_post.call_count == 3  # default n_requests=3


def test_validate_model_custom_n():
    with patch("examlops.cli._client.post", return_value={"prediction": 1.0}) as mock_post:
        result = runner.invoke(app, ["pipeline", "validate-model", "JPCP", "--n", "1"])
    assert result.exit_code == 0, result.output
    assert mock_post.call_count == 1


def test_validate_model_uses_alias():
    with patch("examlops.cli._client.post", return_value={"prediction": 1.0}) as mock_post:
        result = runner.invoke(app, ["pipeline", "validate-model", "JPCP", "--alias", "Production"])
    assert result.exit_code == 0, result.output
    call_body = mock_post.call_args[0][1]
    assert call_body["alias"] == "Production"


def test_validate_model_fail_on_error():
    from examlops.cli._client import ClientError
    with patch("examlops.cli._client.post", side_effect=ClientError("Service down", status=503)):
        result = runner.invoke(app, ["pipeline", "validate-model", "JPCP"])
    assert result.exit_code != 0
    assert "FAIL" in result.output


def test_validate_model_json_output():
    import json
    with patch("examlops.cli._client.post", return_value={"prediction": 1.0}):
        result = runner.invoke(app, ["--json", "pipeline", "validate-model", "JPCP", "--n", "1"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["model"] == "JPCP"
    assert data["result"] == "PASS"
    assert "avg_latency_s" in data


def test_validate_model_fail_high_latency():
    import time

    def slow_post(*a, **k):
        time.sleep(0.01)
        return {"prediction": 1.0}

    with patch("examlops.cli._client.post", side_effect=slow_post):
        result = runner.invoke(app, ["pipeline", "validate-model", "JPCP", "--max-latency", "0.0001"])
    assert result.exit_code != 0
    assert "FAIL" in result.output
