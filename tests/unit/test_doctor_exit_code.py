"""A diagnostic prescribed as a verification step has to be able to fail.

`exa doctor` printed everything it found — a missing config file, an unset credential, an
unreachable service — and exited **0**. That matters because it is not only an interactive
convenience: step 5 of the full-disaster recovery order in `docs/guides/backup-restore.md` is
"`exa doctor` + `exa status` to confirm coherence". A scripted recovery check therefore passed
whatever doctor reported — a success indicator that cannot distinguish success from failure.
`exa instance check` already sets the platform's convention: exit 1 on any problem.

Every probe is stubbed. `doctor` reaches MLflow, Prefect, Ray Serve and the dashboard over HTTP,
which is why it had no tests before: in the suite those calls are refused, so the command errored
before it could reach the branch under test. Stubbing `_ping` also makes both outcomes reachable —
an all-clear run cannot be produced from the environment alone.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

runner = CliRunner()


@pytest.fixture
def healthy(tmp_path, monkeypatch):
    """An environment in which every check passes, so the zero-issue branch is reachable."""
    # Assembled at runtime, never written as a literal: a credential-shaped string in a tracked
    # file is what turns the repository's own secret scan red.
    import secrets as _secrets

    token = "cp-" + _secrets.token_urlsafe(24)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"control_plane_token = {token!r}\n")
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(cfg))
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", token)
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))

    from examlops.cli.commands import doctor as doctor_cmd
    from examlops.platform_db import init_db

    init_db()
    monkeypatch.setattr(doctor_cmd, "_ping", lambda url, timeout=2.0: (True, "200"))
    # `shutil` is imported inside the command, so it is not an attribute of that module.
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    return tmp_path


def _run(*args: str):
    from examlops.cli.main import app

    return runner.invoke(app, list(args))


def test_a_clean_setup_exits_zero(healthy):
    result = _run("--json", "doctor")
    payload = json.loads(result.stdout)
    assert payload["issues"] == 0, f"expected a clean run: {payload['checks']}"
    assert result.exit_code == 0


def test_an_unreachable_service_makes_the_command_fail(healthy, monkeypatch):
    from examlops.cli.commands import doctor as doctor_cmd

    monkeypatch.setattr(doctor_cmd, "_ping", lambda url, timeout=2.0: (False, "connection refused"))

    result = _run("--json", "doctor")
    payload = json.loads(result.stdout)

    assert payload["issues"] > 0
    assert result.exit_code == 1, (
        "doctor reported unreachable services and exited 0 — a scripted recovery check would "
        "treat that as success"
    )


def test_a_missing_config_makes_the_command_fail(healthy, monkeypatch, tmp_path):
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "absent.toml"))

    result = _run("--json", "doctor")
    payload = json.loads(result.stdout)
    config = next(c for c in payload["checks"] if c["name"] == "Config file")

    assert config["ok"] is False
    assert result.exit_code == 1


def test_json_mode_still_prints_exactly_one_document(healthy, monkeypatch):
    """Exiting non-zero must not append a second JSON document (the `--json` contract)."""
    from examlops.cli.commands import doctor as doctor_cmd

    monkeypatch.setattr(doctor_cmd, "_ping", lambda url, timeout=2.0: (False, "down"))

    result = _run("--json", "doctor")

    json.loads(result.stdout)  # raises if anything was appended
    assert result.stdout.strip().startswith("{") and result.stdout.strip().endswith("}")
    assert result.exit_code == 1
