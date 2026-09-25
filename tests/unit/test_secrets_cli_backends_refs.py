"""``exa secrets backends`` / ``exa secrets refs`` and the write-backend errors on set/rotate (ADR 0011)."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from examlops import secrets as sec
from examlops.cli.main import app
from examlops.platform_db import init_db


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("EXAMLOPS_SECRETS_KEYS", f"k1:{Fernet.generate_key().decode()}")
    monkeypatch.setenv("EXAMLOPS_SECRETS_ACTIVE_KEY", "k1")
    for var in (
        "EXAMLOPS_VAULT_ADDR",
        "EXAMLOPS_SOPS_FILE",
        "EXAMLOPS_SECRETS_WRITE_BACKEND",
        "EXAMLOPS_SECRETS_KEY",
        "DASHBOARD_SECRET_KEY",
        "EXAMLOPS_VAULT_STRICT",
    ):
        monkeypatch.delenv(var, raising=False)
    init_db()


def test_backends_json_reports_every_tier_and_no_key_material():
    result = CliRunner().invoke(app, ["--json", "secrets", "backends"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["order"] == ["vault", "sops", "local", "env"]
    assert payload["write_backend"] == "local"
    assert payload["local"] == {"keys": ["k1"], "active_key_id": "k1", "error": None}
    assert payload["vault"]["configured"] is False and payload["sops"]["configured"] is False
    import os

    assert os.environ["EXAMLOPS_SECRETS_KEYS"].split(":", 1)[1] not in result.stdout


def test_backends_human_output_names_the_write_backend(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "bogus")
    result = CliRunner().invoke(app, ["secrets", "backends"])
    assert result.exit_code == 0
    assert "not one of" in result.output


def test_refs_strict_passes_on_resolvable_references(tmp_path):
    sec.set_secret("control-plane/token", "cp-v", actor="t")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CONTROL_PLANE_TOKEN=secret://control-plane/token\nMLFLOW_TRACKING_URI=http://m\n"
    )
    result = CliRunner().invoke(
        app, ["--json", "secrets", "refs", "--env-file", str(env_file), "--strict"]
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert rows == [
        {
            "name": "CONTROL_PLANE_TOKEN",
            "kind": "reference",
            "target": "secret://control-plane/token",
            "resolves": True,
            "error": None,
            "backend": "local",
        }
    ]
    assert "cp-v" not in result.stdout


def test_refs_strict_fails_on_plaintext_and_unresolved(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DASHBOARD_ADMIN_PASSWORD=hunter2\nX_TOKEN=secret://missing/x\n")
    result = CliRunner().invoke(app, ["secrets", "refs", "--env-file", str(env_file), "--strict"])
    assert result.exit_code == 1
    assert "hunter2" not in result.output
    assert "plaintext" in result.output and "NO" in result.output


def test_refs_without_strict_reports_but_exits_zero(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DASHBOARD_ADMIN_PASSWORD=hunter2\n")
    result = CliRunner().invoke(app, ["secrets", "refs", "--env-file", str(env_file)])
    assert result.exit_code == 0 and "plaintext" in result.output


def test_refs_missing_env_file_is_an_error(tmp_path):
    result = CliRunner().invoke(app, ["secrets", "refs", "--env-file", str(tmp_path / "none")])
    assert result.exit_code == 1


def test_set_reports_a_write_backend_failure_cleanly(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "vault")
    result = CliRunner().invoke(app, ["secrets", "set", "a/b", "v"])
    assert result.exit_code == 1
    assert "EXAMLOPS_VAULT_ADDR" in result.output
    assert sec.list_secrets() == []


def test_rotate_reports_a_write_backend_failure_cleanly(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_SECRETS_WRITE_BACKEND", "vault")
    result = CliRunner().invoke(app, ["--yes", "secrets", "rotate", "a/b"])
    assert result.exit_code == 1
    assert "EXAMLOPS_VAULT_ADDR" in result.output
