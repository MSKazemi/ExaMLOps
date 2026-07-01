from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app

runner = CliRunner()


def test_retrain():
    fake = {
        "flow_run_id": "run-abc",
        "deployment": "nightly",
        "status_url": "/retrain/run-abc",
        "parameters": {},
    }
    with patch("examlops.cli.commands.retrain._client.post", return_value=fake):
        result = runner.invoke(app, ["retrain", "JPCP", "--dataset", "PM100Dataset"])
    assert result.exit_code == 0
    assert "run-abc" in result.output


def test_retrain_dummy():
    fake = {
        "flow_run_id": "run-xyz",
        "deployment": "nightly",
        "status_url": "/retrain/run-xyz",
        "parameters": {},
    }
    with patch("examlops.cli.commands.retrain._client.post", return_value=fake) as mock_post:
        runner.invoke(app, ["retrain", "JPCP", "--dummy"])
    body = mock_post.call_args[0][1]
    assert body["is_dummy"] is True


def test_predict():
    fake = {"prediction": 42.0, "model_name": "JPCP", "model_version": "3"}
    with patch("examlops.cli.commands.predict._client.post", return_value=fake):
        result = runner.invoke(app, ["predict", "JPCP", "--features", '{"x": 1}'])
    assert result.exit_code == 0
    assert "42" in result.output


def test_predict_sends_inference_pipeline_payload_shape():
    fake = {"prediction": 42.0, "model_name": "JPCP", "model_version": "3"}
    features = '{"embedding": [0.1, 0.2], "num_nodes": 4, "user_id": "smoke"}'
    with patch("examlops.cli.commands.predict._client.post", return_value=fake) as mock_post:
        result = runner.invoke(app, ["predict", "JPCP", "--features", features])
    assert result.exit_code == 0
    body = mock_post.call_args[0][1]
    assert body == {
        "model_name": "JPCP",
        "embedding": [0.1, 0.2],
        "num_nodes": 4,
        "user_id": "smoke",
    }
