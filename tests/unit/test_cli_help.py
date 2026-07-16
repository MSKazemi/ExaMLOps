from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))
from examlops.cli.main import app

runner = CliRunner(env={"COLUMNS": "200"})


# ── scaffold ─────────────────────────────────────────────────────────────────


def test_scaffold_help_shows_task_choices():
    r = runner.invoke(app, ["scaffold", "--help"])
    assert r.exit_code == 0
    assert "anomaly_detection" in r.output
    assert "performance_prediction" in r.output
    assert "power_consumption_prediction" in r.output


def test_scaffold_help_shows_type_choices():
    r = runner.invoke(app, ["scaffold", "--help"])
    assert r.exit_code == 0
    assert "regression" in r.output
    assert "classification" in r.output


def test_scaffold_help_shows_examples():
    r = runner.invoke(app, ["scaffold", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa scaffold DemoAD" in r.output


# ── retrain ──────────────────────────────────────────────────────────────────


def test_retrain_help_shows_backend_choices():
    r = runner.invoke(app, ["retrain", "--help"])
    assert r.exit_code == 0
    assert "zenodo" in r.output
    assert "minio" in r.output
    assert "dataplane" in r.output


def test_retrain_help_shows_examples():
    r = runner.invoke(app, ["retrain", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa retrain JPCP" in r.output


# ── predict ───────────────────────────────────────────────────────────────────


def test_predict_help_shows_alias_choices():
    r = runner.invoke(app, ["predict", "--help"])
    assert r.exit_code == 0
    assert "Production" in r.output
    assert "Canary" in r.output
    assert "Staging" in r.output


def test_predict_help_shows_examples():
    r = runner.invoke(app, ["predict", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa predict JPCP" in r.output


def test_predict_help_examples_match_inference_pipeline_payload():
    r = runner.invoke(app, ["predict", "--help"])
    assert r.exit_code == 0
    assert '"embedding":[0.1]*384' in r.output
    assert '"num_nodes": 4' in r.output
    assert '"user_id": "smoke"' in r.output
    assert '"features": {"embedding": [0.1, 0.2]}' not in r.output


# ── status ────────────────────────────────────────────────────────────────────


def test_status_help_shows_examples():
    r = runner.invoke(app, ["status", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa status" in r.output


# ── pipeline ──────────────────────────────────────────────────────────────────


def test_pipeline_run_help_shows_env_choices():
    r = runner.invoke(app, ["pipeline", "run", "--help"])
    assert r.exit_code == 0
    assert "dev" in r.output
    assert "staging" in r.output
    assert "prod" in r.output


def test_pipeline_run_help_shows_examples():
    r = runner.invoke(app, ["pipeline", "run", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa pipeline run" in r.output


def test_pipeline_deploy_help_shows_examples():
    r = runner.invoke(app, ["pipeline", "deploy", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa pipeline deploy" in r.output


# ── stack ─────────────────────────────────────────────────────────────────────


def test_stack_up_help_shows_service_choices():
    r = runner.invoke(app, ["stack", "up", "--help"])
    assert r.exit_code == 0
    assert "dashboard" in r.output
    assert "mlflow" in r.output
    assert "ray-serving" in r.output


def test_stack_up_help_shows_examples():
    r = runner.invoke(app, ["stack", "up", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa stack up" in r.output


def test_stack_logs_help_shows_service_choices():
    r = runner.invoke(app, ["stack", "logs", "--help"])
    assert r.exit_code == 0
    assert "dashboard" in r.output
    assert "Examples:" in r.output


# ── serve ─────────────────────────────────────────────────────────────────────


def test_serve_reload_help_shows_examples():
    r = runner.invoke(app, ["serve", "reload", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa serve reload" in r.output


def test_serve_check_help_shows_examples():
    r = runner.invoke(app, ["serve", "check", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


# ── models ────────────────────────────────────────────────────────────────────


def test_models_list_help_shows_examples():
    r = runner.invoke(app, ["models", "list", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa models list" in r.output


def test_models_info_help_shows_examples():
    r = runner.invoke(app, ["models", "info", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa models info" in r.output


# ── modelzoo ──────────────────────────────────────────────────────────────────


def test_modelzoo_status_help_shows_examples():
    r = runner.invoke(app, ["modelzoo", "status", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_modelzoo_events_help_shows_examples():
    r = runner.invoke(app, ["modelzoo", "events", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa modelzoo events" in r.output


# ── approvals ─────────────────────────────────────────────────────────────────


def test_approvals_list_help_shows_examples():
    r = runner.invoke(app, ["approvals", "list", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_approvals_approve_help_shows_examples():
    r = runner.invoke(app, ["approvals", "approve", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa approvals approve" in r.output


def test_approvals_reject_help_shows_examples():
    r = runner.invoke(app, ["approvals", "reject", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


# ── dataplane ─────────────────────────────────────────────────────────────────


def test_dataplane_list_help_shows_examples():
    r = runner.invoke(app, ["dataplane", "list", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_dataplane_regen_uuid_help_shows_examples():
    r = runner.invoke(app, ["dataplane", "regen-uuid", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa dataplane regen-uuid" in r.output


# ── config ────────────────────────────────────────────────────────────────────


def test_config_show_help_shows_examples():
    r = runner.invoke(app, ["config", "show", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_config_set_help_shows_examples():
    r = runner.invoke(app, ["config", "set", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
    assert "exa config set" in r.output


def test_pipeline_list_help_shows_examples():
    r = runner.invoke(app, ["pipeline", "list", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_pipeline_export_registry_help_shows_examples():
    r = runner.invoke(app, ["pipeline", "export-registry", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_stack_down_help_shows_examples():
    r = runner.invoke(app, ["stack", "down", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_stack_restart_help_shows_examples():
    r = runner.invoke(app, ["stack", "restart", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_stack_status_help_shows_examples():
    r = runner.invoke(app, ["stack", "status", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_dataplane_init_uuids_help_shows_examples():
    r = runner.invoke(app, ["dataplane", "init-uuids", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output


def test_config_init_help_shows_examples():
    r = runner.invoke(app, ["config", "init", "--help"])
    assert r.exit_code == 0
    assert "Examples:" in r.output
