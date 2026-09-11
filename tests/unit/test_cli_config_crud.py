"""`exa config` as full CRUD: keys are validated, values and contexts can be removed.

Before: `exa config set token X` wrote `[auth] token = X`, which nothing reads — success printed,
nothing changed (the readable key is `control_plane_token`). `exa config show` prints field names
like `mlflow_url`, and `exa config set mlflow_url …` was the same silent no-op. And a value or a
context, once written, could only be removed by editing the TOML by hand.
"""

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
    for var in ("EXAMLOPS_CONFIG", "EXAMLOPS_CONTEXT", "CONTROL_PLANE_URL", "MLFLOW_TRACKING_URI"):
        monkeypatch.delenv(var, raising=False)
    for _field, _key, env, _default, _secret in _config._FIELDS:
        monkeypatch.delenv(env, raising=False)
    yield path


def _json(*argv):
    result = runner.invoke(app, ["--json", *argv])
    return result, (json.loads(result.output) if result.output.strip() else None)


# ── keys are validated ────────────────────────────────────────────────────────────────────


def test_an_unknown_key_is_refused_and_nothing_is_written(cfg_file):
    with pytest.raises(_config.UnknownConfigKey) as exc:
        _config.write_config({"token": "x"})
    assert "control_plane_token" in str(exc.value)  # the suggestion names the real key
    assert not cfg_file.exists()


def test_the_field_names_config_show_prints_are_accepted(cfg_file):
    _config.write_config({"mlflow_url": "http://m:5000"})
    assert _config.load_config().mlflow_url == "http://m:5000"
    assert _config.canonical_key("control_plane_url") == "control_plane"
    assert _config.canonical_key("agent_token") == "agent_token"


def test_cli_set_rejects_an_unknown_key_with_a_suggestion(cfg_file):
    result, payload = _json("config", "set", "token", "x")
    assert result.exit_code == 1
    assert "control_plane_token" in payload["error"]
    assert not cfg_file.exists()


def test_cli_set_accepts_the_shown_field_name(cfg_file):
    result, _ = _json("config", "set", "mlflow_url", "http://m:5000")
    assert result.exit_code == 0, result.output
    _, shown = _json("config", "show")
    assert shown["mlflow_url"] == "http://m:5000"


def test_show_reports_every_field(cfg_file):
    _, shown = _json("config", "show")
    assert {f for f, *_ in _config._FIELDS} <= set(shown)
    assert shown["dashboard_token"] == "(unset)"


# ── values can be removed ─────────────────────────────────────────────────────────────────


def test_unset_reverts_a_value_to_its_default(cfg_file):
    _config.write_config({"mlflow": "http://m:5000"})
    result, _ = _json("config", "unset", "mlflow")
    assert result.exit_code == 0, result.output
    assert _config.load_config().mlflow_url == "http://localhost:15000"


def test_unset_inside_a_context_leaves_the_base_value(cfg_file):
    _config.write_config({"mlflow": "http://base:5000"})
    _config.write_config({"mlflow": "http://ctx:5000"}, context="stg")
    assert _config.unset_config("mlflow_url", context="stg") is True
    _config.set_active_context("stg")
    assert _config.load_config().mlflow_url == "http://base:5000"


def test_unset_is_idempotent_and_still_validates(cfg_file):
    assert _config.unset_config("agent_token") is False  # nothing to remove, no error
    with pytest.raises(_config.UnknownConfigKey):
        _config.unset_config("agnet_token")


# ── contexts can be left and removed ──────────────────────────────────────────────────────


def test_use_clear_returns_to_the_base_configuration(cfg_file):
    _config.write_config({"mlflow": "http://ctx:5000"}, context="stg")
    runner.invoke(app, ["config", "use", "stg"])
    result, _ = _json("config", "use", "--clear")
    assert result.exit_code == 0, result.output
    assert _config.list_contexts()[1] is None
    assert _config.load_config().mlflow_url == "http://localhost:15000"


def test_use_needs_a_name_or_clear(cfg_file):
    result, _ = _json("config", "use")
    assert result.exit_code == 1


def test_delete_context_removes_it_and_its_active_pointer(cfg_file):
    _config.write_config({"mlflow": "http://ctx:5000"}, context="stg")
    _config.set_active_context("stg")
    result, _ = _json("config", "delete-context", "stg")
    assert result.exit_code == 0, result.output
    assert _config.list_contexts() == ([], None)


def test_deleting_a_missing_context_is_an_error(cfg_file):
    result, payload = _json("config", "delete-context", "nope")
    assert result.exit_code == 1
    assert "nope" in payload["error"]


# ── values are checked, and the file is audited ───────────────────────────────────────────


def test_a_url_key_needs_a_url(cfg_file):
    result, payload = _json("config", "set", "mlflow", "mlflow-host")
    assert result.exit_code == 1
    assert "http" in payload["error"]
    assert not cfg_file.exists()
    assert _json("config", "set", "mlflow", "https://mlflow.example:443")[0].exit_code == 0


def test_validate_reports_keys_nothing_reads(cfg_file):
    # A file written before keys were validated (or by hand) keeps its dead keys; say so.
    cfg_file.write_text('[auth]\ntoken = "x"\n\n[contexts.stg.urls]\nmlfow = "http://m"\n')
    result, findings = _json("env", "--validate")
    messages = " ".join(f["message"] for f in findings)
    assert "token" in messages and "control_plane_token" in messages
    assert "mlfow" in messages and "stg" in messages
    assert result.exit_code == 0  # dead keys are warnings, not errors


def test_validate_reports_an_active_context_that_does_not_exist(cfg_file):
    cfg_file.write_text('active_context = "gone"\n')
    _result, findings = _json("env", "--validate")
    assert any("gone" in f["message"] and f["level"] == "warn" for f in findings)
