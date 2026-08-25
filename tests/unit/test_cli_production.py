from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli._config import Config
from examlops.cli.commands import production
from examlops.cli.main import app

runner = CliRunner()

FAKE_CONTROL_HEALTH = {"status": "ok", "pending_approvals": 0}
FAKE_RAY_HEALTH = {"status": "ok"}
FAKE_RAY_MODELS = [
    {"model_name": "jpcp", "alias": "Production", "model_version": "18", "status": "ok"},
    {"model_name": "mack", "alias": "Production", "model_version": "6", "status": "ok"},
]
FAKE_MODELZOO = {
    "models": [
        {
            "model_id": "JPCP",
            "status": "current",
            "stale_since": None,
            "latest_modelzoo_commit": "abc123",
        },
        {
            "model_id": "MACK",
            "status": "stale",
            "stale_since": "2026-05-23T07:12:00",
            "latest_modelzoo_commit": "def456",
        },
    ],
    "last_event": None,
}
FAKE_DASHBOARD_HEALTH = {"status": "ok", "services": {}}
FAKE_SEANERBUS_HEALTH = {"status": "ok"}
FAKE_SEANERBUS_STATS = {"inferences_total": 42, "per_model": {"JPCP": {"errors": 0}}}


def _fake_get(url: str, token: str = ""):
    if url.endswith("/health") and ":18002" in url:
        return FAKE_CONTROL_HEALTH
    if url.endswith("/health") and ":18001" in url:
        return FAKE_RAY_HEALTH
    if url.endswith("/models"):
        return FAKE_RAY_MODELS
    if url.endswith("/modelzoo/status"):
        return FAKE_MODELZOO
    if url.endswith("/api/health"):
        return FAKE_DASHBOARD_HEALTH
    if url.endswith("/stats"):
        return FAKE_SEANERBUS_STATS
    if url.endswith("/health") and ":18003" in url:
        return FAKE_SEANERBUS_HEALTH
    raise AssertionError(f"unexpected URL: {url}")


def test_production_command_is_registered():
    result = runner.invoke(app, ["production", "--help"])

    assert result.exit_code == 0
    assert "verify" in result.output


def test_production_modelzoo_helpers_forward_control_plane_token():
    cfg = Config(control_plane_token="configured-control-plane-token")
    with patch(
        "examlops.cli.commands.production._safe_get",
        return_value=(True, FAKE_MODELZOO, "reachable"),
    ) as safe_get:
        assert production._modelzoo_models(cfg)
        production._check_modelzoo(cfg)

    assert safe_get.call_count == 2
    for call in safe_get.call_args_list:
        assert call.kwargs["token"] == "configured-control-plane-token"


def test_production_verify_reports_pass_with_stale_warning():
    with patch("examlops.cli.commands.production._client.get", side_effect=_fake_get):
        result = runner.invoke(app, ["production", "verify"])

    assert result.exit_code == 0
    assert "Production Verification" in result.output
    assert "Control Plane" in result.output
    assert "Ray Serve" in result.output
    assert "ModelZoo" in result.output
    assert "STALE: 1" in result.output
    assert "PASS" in result.output


def test_production_verify_json_contains_services_and_summary():
    with patch("examlops.cli.commands.production._client.get", side_effect=_fake_get):
        result = runner.invoke(app, ["--json", "production", "verify"])

    assert result.exit_code == 0
    assert '"overall_status": "pass"' in result.output
    assert '"stale_models": 1' in result.output
    assert '"ray_serve"' in result.output


def test_production_deploy_dry_run_plans_stale_models_without_side_effects():
    with (
        patch("examlops.cli.commands.production._client.get", side_effect=_fake_get),
        patch("examlops.cli.commands.production._client.post") as mock_post,
        patch("examlops.cli.commands.production.subprocess.run") as mock_run,
    ):
        result = runner.invoke(app, ["production", "deploy"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "MACK" in result.output
    assert "pipeline deploy" in result.output.lower()
    mock_post.assert_not_called()
    mock_run.assert_not_called()


def test_production_deploy_execute_runs_pipeline_retrain_reload_and_verify(tmp_path):
    posts: list[tuple[str, dict]] = []
    history_path = tmp_path / "history.jsonl"

    def fake_post(url: str, body: dict, token: str = ""):
        posts.append((url, body))
        if url.endswith("/retrain"):
            return {"flow_run_id": f"flow-{body['model_name']}"}
        if url.endswith("/reload"):
            return {"count": 2}
        raise AssertionError(f"unexpected POST URL: {url}")

    with (
        patch("examlops.cli.commands.production._HISTORY_PATH", history_path),
        patch("examlops.cli.commands.production._client.get", side_effect=_fake_get),
        patch("examlops.cli.commands.production._client.post", side_effect=fake_post),
        patch("examlops.cli.commands.production.subprocess.run") as mock_run,
    ):
        result = runner.invoke(app, ["production", "deploy", "--execute"])

    assert result.exit_code == 0
    assert "EXECUTE" in result.output
    assert "flow-MACK" in result.output
    deploy_cmd = mock_run.call_args[0][0]
    assert "deploy.py" in " ".join(deploy_cmd)
    assert "--env" in deploy_cmd and "prod" in deploy_cmd
    assert any(url.endswith("/retrain") and body["model_name"] == "MACK" for url, body in posts)
    assert any(url.endswith("/reload") for url, _ in posts)

    records = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["deploy_id"].startswith("deploy-")
    assert record["status"] == "success"
    assert record["models"] == ["MACK"]
    assert record["retrain_results"] == [{"model": "MACK", "flow_run_id": "flow-MACK"}]
    assert record["verification_status"] == "pass"
    assert record["started_at"] <= record["ended_at"]


def test_production_deploy_dry_run_does_not_write_history(tmp_path):
    history_path = tmp_path / "history.jsonl"

    with (
        patch("examlops.cli.commands.production._HISTORY_PATH", history_path),
        patch("examlops.cli.commands.production._client.get", side_effect=_fake_get),
    ):
        result = runner.invoke(app, ["production", "deploy"])

    assert result.exit_code == 0
    assert not history_path.exists()


def test_production_deploy_history_lists_recent_records(tmp_path):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "deploy_id": "deploy-20260525T101010Z-abcd1234",
                "started_at": "2026-05-25T10:10:10Z",
                "ended_at": "2026-05-25T10:11:10Z",
                "env": "prod",
                "models": ["MACK"],
                "dataset": "FDataDataset",
                "status": "success",
                "verification_status": "pass",
                "rollback": {"previous_successful_deploy_id": None},
            }
        )
        + "\n"
    )

    with patch("examlops.cli.commands.production._HISTORY_PATH", history_path):
        result = runner.invoke(app, ["production", "deploy", "history"])

    assert result.exit_code == 0
    assert "Production Deploy History" in result.output
    assert "deploy-20260525T101010Z-abcd1234" in result.output
    assert "success" in result.output
    assert "MACK" in result.output


def test_production_deploy_status_shows_one_record_as_json(tmp_path):
    history_path = tmp_path / "history.jsonl"
    record = {
        "deploy_id": "deploy-20260525T101010Z-abcd1234",
        "started_at": "2026-05-25T10:10:10Z",
        "ended_at": "2026-05-25T10:11:10Z",
        "env": "prod",
        "registry": "pipelines/model_registry.yaml",
        "models": ["MACK"],
        "dataset": "FDataDataset",
        "status": "success",
        "verification_status": "pass",
        "pipeline_deploy": {"status": "success"},
        "retrain_results": [{"model": "MACK", "flow_run_id": "flow-MACK"}],
        "reload_result": {"count": 2},
        "rollback": {"previous_successful_deploy_id": None},
    }
    history_path.write_text(json.dumps(record) + "\n")

    with patch("examlops.cli.commands.production._HISTORY_PATH", history_path):
        result = runner.invoke(
            app,
            ["--json", "production", "deploy", "status", "deploy-20260525T101010Z-abcd1234"],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["deploy_id"] == "deploy-20260525T101010Z-abcd1234"
    assert payload["status"] == "success"
    assert payload["rollback"]["previous_successful_deploy_id"] is None


def test_production_deploy_execute_records_failed_retrain_as_failed_history(tmp_path):
    history_path = tmp_path / "history.jsonl"

    def fake_post(url: str, body: dict, token: str = ""):
        if url.endswith("/retrain"):
            raise production._client.ClientError("boom")
        raise AssertionError(f"unexpected POST URL: {url}")

    with (
        patch("examlops.cli.commands.production._HISTORY_PATH", history_path),
        patch("examlops.cli.commands.production._client.get", side_effect=_fake_get),
        patch("examlops.cli.commands.production._client.post", side_effect=fake_post),
        patch("examlops.cli.commands.production.subprocess.run"),
    ):
        result = runner.invoke(app, ["production", "deploy", "--execute"])

    assert result.exit_code == 1
    record = json.loads(history_path.read_text().strip())
    assert record["status"] == "failed"
    assert record["failed_step"] == "retrain:MACK"
    assert "boom" in record["error"]


def _write_rollback_history(history_path: Path) -> None:
    previous = {
        "deploy_id": "deploy-good",
        "started_at": "2026-05-25T09:00:00Z",
        "ended_at": "2026-05-25T09:01:00Z",
        "env": "prod",
        "models": ["MACK", "JPCP"],
        "dataset": "FDataDataset",
        "status": "success",
        "verification_status": "pass",
        "loaded_models": [
            {"model_name": "MACK", "model_version": "5", "alias": "Production"},
            {"model_name": "JPCP", "model_version": "17", "alias": "Production"},
        ],
        "rollback": {"previous_successful_deploy_id": None},
    }
    bad = {
        "deploy_id": "deploy-bad",
        "started_at": "2026-05-25T10:00:00Z",
        "ended_at": "2026-05-25T10:01:00Z",
        "env": "prod",
        "models": ["MACK"],
        "dataset": "FDataDataset",
        "status": "partial",
        "verification_status": "fail",
        "rollback": {"previous_successful_deploy_id": "deploy-good"},
    }
    history_path.write_text(json.dumps(previous) + "\n" + json.dumps(bad) + "\n")


def test_production_deploy_rollback_dry_run_plans_previous_successful_deploy(tmp_path):
    history_path = tmp_path / "history.jsonl"
    _write_rollback_history(history_path)

    with (
        patch("examlops.cli.commands.production._HISTORY_PATH", history_path),
        patch("examlops.cli.commands.production._client.post") as mock_post,
        patch("examlops.cli.commands.production._set_production_alias") as mock_alias,
    ):
        result = runner.invoke(app, ["production", "deploy", "rollback", "deploy-bad"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "deploy-bad" in result.output
    assert "deploy-good" in result.output
    assert "MACK" in result.output
    assert "v5" in result.output
    mock_post.assert_not_called()
    mock_alias.assert_not_called()
    assert len(history_path.read_text().splitlines()) == 2


def test_production_deploy_rollback_requires_known_deploy_id(tmp_path):
    history_path = tmp_path / "history.jsonl"
    _write_rollback_history(history_path)

    with patch("examlops.cli.commands.production._HISTORY_PATH", history_path):
        result = runner.invoke(app, ["production", "deploy", "rollback", "missing-deploy"])

    assert result.exit_code == 1
    assert "Deploy record not found" in result.output


def test_production_deploy_rollback_execute_restores_aliases_reloads_verifies_and_records(tmp_path):
    history_path = tmp_path / "history.jsonl"
    _write_rollback_history(history_path)
    alias_calls: list[tuple[str, str, str]] = []
    posts: list[tuple[str, dict]] = []

    def fake_alias(cfg, model_name: str, version: str):
        alias_calls.append((cfg.mlflow_url, model_name, version))
        return {"model": model_name, "alias": "Production", "version": version}

    def fake_post(url: str, body: dict, token: str = ""):
        posts.append((url, body))
        if url.endswith("/reload"):
            return {"count": 2}
        raise AssertionError(f"unexpected POST URL: {url}")

    with (
        patch("examlops.cli.commands.production._HISTORY_PATH", history_path),
        patch("examlops.cli.commands.production._client.get", side_effect=_fake_get),
        patch("examlops.cli.commands.production._client.post", side_effect=fake_post),
        patch("examlops.cli.commands.production._set_production_alias", side_effect=fake_alias),
    ):
        result = runner.invoke(app, ["production", "deploy", "rollback", "deploy-bad", "--execute"])

    assert result.exit_code == 0
    assert "ROLLBACK EXECUTE" in result.output
    assert alias_calls == [
        ("http://localhost:15000", "MACK", "5"),
        ("http://localhost:15000", "JPCP", "17"),
    ]
    assert any(url.endswith("/reload") for url, _ in posts)
    records = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert len(records) == 3
    rollback_record = records[-1]
    assert rollback_record["operation"] == "rollback"
    assert rollback_record["status"] == "success"
    assert rollback_record["rollback_of_deploy_id"] == "deploy-bad"
    assert rollback_record["restored_deploy_id"] == "deploy-good"
    assert rollback_record["alias_results"] == [
        {"model": "MACK", "alias": "Production", "version": "5"},
        {"model": "JPCP", "alias": "Production", "version": "17"},
    ]
    assert rollback_record["verification_status"] == "pass"


def _write_filter_history(history_path: Path) -> None:
    records = [
        {
            "deploy_id": "deploy-success-mack",
            "operation": "deploy",
            "status": "success",
            "env": "prod",
            "models": ["MACK"],
            "started_at": "2026-05-25T09:00:00Z",
            "verification_status": "pass",
        },
        {
            "deploy_id": "deploy-failed-jpcp",
            "operation": "deploy",
            "status": "failed",
            "env": "prod",
            "models": ["JPCP"],
            "started_at": "2026-05-25T10:00:00Z",
            "verification_status": "fail",
        },
        {
            "deploy_id": "rollback-mack",
            "operation": "rollback",
            "status": "success",
            "models": ["MACK", "JPCP"],
            "started_at": "2026-05-25T11:00:00Z",
            "verification_status": "pass",
        },
    ]
    history_path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_production_deploy_history_filters_by_status_model_and_operation(tmp_path):
    history_path = tmp_path / "history.jsonl"
    _write_filter_history(history_path)

    with patch("examlops.cli.commands.production._HISTORY_PATH", history_path):
        result = runner.invoke(
            app,
            [
                "production",
                "deploy",
                "history",
                "--status",
                "success",
                "--model",
                "MACK",
                "--operation",
                "rollback",
            ],
        )

    assert result.exit_code == 0
    assert "rollback-mack" in result.output
    assert "deploy-success-mack" not in result.output
    assert "deploy-failed-jpcp" not in result.output


def test_production_deploy_history_json_filters_records(tmp_path):
    history_path = tmp_path / "history.jsonl"
    _write_filter_history(history_path)

    with patch("examlops.cli.commands.production._HISTORY_PATH", history_path):
        result = runner.invoke(
            app,
            ["--json", "production", "deploy", "history", "--status", "failed", "--model", "JPCP"],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [record["deploy_id"] for record in payload] == ["deploy-failed-jpcp"]


def test_production_deploy_history_filter_no_matches_reports_empty(tmp_path):
    history_path = tmp_path / "history.jsonl"
    _write_filter_history(history_path)

    with patch("examlops.cli.commands.production._HISTORY_PATH", history_path):
        result = runner.invoke(app, ["production", "deploy", "history", "--status", "partial"])

    assert result.exit_code == 0
    assert "No production deploy history found" in result.output
