from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from typer.testing import CliRunner  # noqa: E402

from examlops.cli import _config  # noqa: E402
from examlops.cli.main import app  # noqa: E402

runner = CliRunner()


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(_config, "CONFIG_PATH", path)
    # Isolate from any ambient context / env overrides.
    for var in (
        "EXAMLOPS_CONFIG",
        "EXAMLOPS_CONTEXT",
        "CONTROL_PLANE_URL",
        "MLFLOW_TRACKING_URI",
        "AGENT_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield path


# ── context persistence & resolution ──────────────────────────────────────────


def test_set_into_context_and_activate(cfg_file):
    _config.write_config({"control_plane": "http://lxp:18002"}, context="lxp")
    # Not active yet → still default.
    assert _config.load_config().control_plane_url == "http://localhost:18002"
    _config.set_active_context("lxp")
    assert _config.load_config().control_plane_url == "http://lxp:18002"


def test_list_contexts_reports_active(cfg_file):
    _config.write_config({"mlflow": "http://lxp:15000"}, context="lxp")
    _config.write_config({"mlflow": "http://stg:15000"}, context="staging")
    _config.set_active_context("staging")
    names, active = _config.list_contexts()
    assert set(names) == {"lxp", "staging"}
    assert active == "staging"


def test_env_var_overrides_context(cfg_file, monkeypatch):
    _config.write_config({"control_plane": "http://lxp:18002"}, context="lxp")
    _config.set_active_context("lxp")
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://envwins:9999")
    assert _config.load_config().control_plane_url == "http://envwins:9999"


def test_examlops_context_env_selects_context(cfg_file, monkeypatch):
    _config.write_config({"control_plane": "http://lxp:18002"}, context="lxp")
    # No active_context in file, but env selects it.
    monkeypatch.setenv("EXAMLOPS_CONTEXT", "lxp")
    assert _config.load_config().control_plane_url == "http://lxp:18002"


# ── provenance ────────────────────────────────────────────────────────────────


def test_provenance_sources(cfg_file, monkeypatch):
    _config.write_config({"mlflow": "http://filed:15000"})  # legacy top-level → "file"
    _config.write_config({"control_plane": "http://lxp:18002"}, context="lxp")
    _config.set_active_context("lxp")
    monkeypatch.setenv("PREFECT_API_URL", "http://envd:14200")

    prov = {r["key"]: r["source"] for r in _config.resolve_with_provenance()}
    assert prov["control_plane_url"] == "context:lxp"
    assert prov["mlflow_url"] == "file"
    assert prov["prefect_url"] == "env:PREFECT_API_URL"
    assert prov["ray_serve_url"] == "default"


def test_provenance_redacts_secrets(cfg_file):
    _config.write_config({"control_plane_token": "supersecret", "agent_token": "agentsecret"})
    prov = {r["key"]: r["value"] for r in _config.resolve_with_provenance()}
    assert prov["control_plane_token"] == "***"
    assert prov["agent_token"] == "***"
    assert "supersecret" not in json.dumps(prov)
    assert "agentsecret" not in json.dumps(prov)


def test_agent_token_can_be_scoped_per_context(cfg_file):
    _config.write_config({"agent_token": "production-secret"}, context="production")
    _config.set_active_context("production")
    assert _config.load_config().agent_token == "production-secret"


def test_cli_prompts_for_secret_and_never_echoes_it(cfg_file):
    result = runner.invoke(
        app,
        ["config", "set", "agent_token", "--context", "production"],
        input="production-secret\nproduction-secret\n",
    )
    assert result.exit_code == 0, result.output
    assert "production-secret" not in result.output
    assert "***" in result.output
    _config.set_active_context("production")
    assert _config.load_config().agent_token == "production-secret"


def test_cli_redacts_a_secret_passed_as_an_argument(cfg_file):
    result = runner.invoke(app, ["config", "set", "agent_token", "legacy-secret"])
    assert result.exit_code == 0, result.output
    assert "legacy-secret" not in result.output
    assert "***" in result.output


def test_config_file_is_owner_only(cfg_file):
    _config.write_config({"agent_token": "private"})
    assert cfg_file.stat().st_mode & 0o777 == 0o600


def test_agent_sessions_are_namespaced_by_active_project(cfg_file):
    assert _config.scoped_agent_session("incident-42") == "incident-42"
    _config.set_active_project("research")
    assert _config.scoped_agent_session("incident-42") == "research:incident-42"


# ── CLI wiring ────────────────────────────────────────────────────────────────


def test_cli_config_use_and_contexts(cfg_file):
    runner.invoke(app, ["config", "set", "mlflow", "http://lxp:15000", "--context", "lxp"])
    result = runner.invoke(app, ["config", "use", "lxp"])
    assert result.exit_code == 0, result.output
    listing = runner.invoke(app, ["config", "contexts"])
    assert "lxp" in listing.output
    assert "active" in listing.output


def test_cli_env_json(cfg_file):
    runner.invoke(app, ["config", "set", "mlflow", "http://lxp:15000", "--context", "lxp"])
    runner.invoke(app, ["config", "use", "lxp"])
    result = runner.invoke(app, ["--json", "env"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["active_context"] == "lxp"
    src = {s["key"]: s["source"] for s in payload["settings"]}
    assert src["mlflow_url"] == "context:lxp"


def test_cli_global_context_flag_one_off(cfg_file, monkeypatch):
    runner.invoke(app, ["config", "set", "mlflow", "http://oneoff:15000", "--context", "ephemeral"])
    # -c selects the context for just this invocation (no `config use`).
    result = runner.invoke(app, ["-c", "ephemeral", "--json", "env"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    src = {s["key"]: s["source"] for s in payload["settings"]}
    assert src["mlflow_url"] == "context:ephemeral"
    monkeypatch.delenv("EXAMLOPS_CONTEXT", raising=False)
