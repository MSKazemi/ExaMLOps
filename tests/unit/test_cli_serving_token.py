"""`exa` sends a serving credential to the serving gateway, and to nothing else (ADR 0126).

With the workload-identity overlay the model server is no longer reachable from the host: inference
goes through the serving gateway, which needs `Authorization: Bearer <virtual key or IdP token>`.
`exa` had no way to send one, so `exa predict`, `exa serve infer-check` and `exa serve batch`
stopped working there. `serving_token` (config key, or `EXAMLOPS_SERVING_TOKEN`) is that credential.
It is attached only to requests under the configured `ray_serve` URL, and never over a credential
the caller set itself (the serving admin token), so a key for inference cannot leak to MLflow,
Prefect, the control plane or a `--url` somewhere else.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from examlops.service_auth import headers_for

GATEWAY = "http://gw.example:18088"
KEY = "-".join(("exa", "test", "serving", "key"))  # assembled at runtime: not a literal secret


@pytest.fixture()
def serving_env(monkeypatch, tmp_path):
    """A hermetic config whose `ray_serve` is the gateway and whose serving token is KEY."""
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("RAY_SERVE_URL", GATEWAY)
    monkeypatch.setenv("EXAMLOPS_SERVING_TOKEN", KEY)
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    for var in ("MLFLOW_TRACKING_TOKEN", "MLFLOW_TRACKING_USERNAME", "PREFECT_API_KEY",
                "PREFECT_API_AUTH_STRING", "RAY_SERVE_ADMIN_TOKEN"):  # fmt: skip
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{GATEWAY}/v2/models/jpcp/infer", True),
        (f"{GATEWAY}", True),
        (f"{GATEWAY.upper()}/v2/health/ready", True),  # scheme and host are case-insensitive
        ("http://gw.example:1808/v2", False),  # a port that merely starts the same
        ("http://gw.example:180880/v2", False),
        ("http://gw.example.evil:18088/v2", False),
        ("http://mlflow:5000/api/2.0/mlflow/experiments/list", False),
        ("http://control-plane:8002/v1/models", False),
    ],
)
def test_the_serving_credential_goes_only_under_the_serving_base(url, expected):
    headers = headers_for(url, serving_base=GATEWAY, serving_token=KEY)
    assert (headers == {"Authorization": f"Bearer {KEY}"}) is expected, (url, headers)


def test_no_serving_token_means_no_header():
    assert headers_for(f"{GATEWAY}/v2", serving_base=GATEWAY, serving_token="  ") == {}
    assert headers_for(f"{GATEWAY}/v2", serving_base="", serving_token=KEY) == {}


def test_mlflow_keeps_its_own_credential_when_it_shares_nothing_with_serving(monkeypatch):
    monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", "mlflow-side")
    headers = headers_for(
        "http://mlflow:5000/api", mlflow_base="http://mlflow:5000", serving_base=GATEWAY,
        serving_token=KEY,
    )  # fmt: skip
    assert headers == {"Authorization": "Bearer mlflow-side"}


def _capture_urlopen(seen: list):
    def fake(req, timeout=10):
        seen.append((req.full_url, req.get_header("Authorization")))
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"model_name": "jpcp", "outputs": [{"name": "predict", "shape": [1], "data": [42]}]}
        ).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    return fake


def test_the_cli_client_sends_it_to_the_gateway_and_nowhere_else(serving_env):
    from examlops.cli import _client

    seen: list = []
    with patch("urllib.request.urlopen", side_effect=_capture_urlopen(seen)):
        _client.post(f"{GATEWAY}/infer-pipeline/infer", {"model": "jpcp"})
        _client.get("http://control-plane:8002/v1/models")
        _client.post(f"{GATEWAY}/reload", {}, token="admin-token")  # the caller's own credential
    assert seen == [
        (f"{GATEWAY}/infer-pipeline/infer", f"Bearer {KEY}"),
        ("http://control-plane:8002/v1/models", None),
        (f"{GATEWAY}/reload", "Bearer admin-token"),  # never overwritten
    ]


def test_batch_inference_sends_it(serving_env, tmp_path):
    from examlops.cli.commands.batch_cmd import _ensure_table
    from examlops.cli.main import app
    from examlops.platform_db import init_db

    init_db()
    _ensure_table()
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"x": 1}, {"x": 2}]))
    seen: list = []
    with patch("urllib.request.urlopen", side_effect=_capture_urlopen(seen)):
        result = CliRunner().invoke(app, ["serve", "batch", "submit", "JPCP", str(rows)])
    assert result.exit_code == 0, result.output
    assert [auth for _, auth in seen] == [f"Bearer {KEY}"] * 2
    assert all(url.startswith(f"{GATEWAY}/v2/models/JPCP/") for url, _ in seen), seen


@pytest.fixture()
def loadtest_cli(monkeypatch, tmp_path, serving_env):
    from examlops import loadtest
    from examlops.cli.main import app

    seen: dict = {}

    async def fake_run(url, body, **kwargs):
        seen.update(url=url, headers=kwargs["headers"])
        report = loadtest.LoadReport(target_rate=kwargs["rate"], duration_s=kwargs["duration"])
        report.sent = 1
        report.statuses[200] = 1
        report.latencies_ms = [5.0]
        report.elapsed_s = 1.0
        return report

    monkeypatch.setattr(loadtest, "run", fake_run)
    monkeypatch.delenv("EXAMLOPS_LOADTEST_TOKEN", raising=False)
    body = tmp_path / "body.json"
    body.write_text(json.dumps({"inputs": []}))
    return app, seen, str(body)


def test_loadtest_falls_back_to_the_serving_token_for_the_configured_url(loadtest_cli):
    app, seen, body = loadtest_cli
    result = CliRunner().invoke(app, ["--json", "serve", "loadtest", "jpcp", "--body", body])
    assert result.exit_code == 0, result.output
    assert seen["headers"] == {"Authorization": f"Bearer {KEY}"}


def test_loadtest_never_sends_the_serving_token_to_another_url(loadtest_cli):
    app, seen, body = loadtest_cli
    args = ["--json", "serve", "loadtest", "jpcp", "--body", body, "--url", "http://other:9"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert seen["headers"] == {}


def test_the_serving_token_is_a_secret_setting(serving_env):
    """`exa config set serving_token …` works, and `exa env` never prints the value."""
    from examlops.cli.main import app

    runner = CliRunner()
    os.environ.pop("EXAMLOPS_SERVING_TOKEN")
    stored = "-".join(("exa", "stored", "key"))
    assert runner.invoke(app, ["config", "set", "serving_token", stored]).exit_code == 0
    from examlops.cli._config import load_config

    assert load_config().serving_token == stored
    shown = runner.invoke(app, ["--json", "env"])
    assert shown.exit_code == 0, shown.output
    assert stored not in shown.output and "serving_token" in shown.output
