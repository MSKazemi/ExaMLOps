"""Platform clients send the control plane the credential in CONTROL_PLANE_TOKEN_FILE (ADR 0125).

A workload identity (a JWT-SVID) expires within minutes; SPIRE's spiffe-helper rewrites it in a
file before it does. Every client must read that file when it calls, not once at start, and fall
back to the static credential while the file is missing, so a service can be moved over first.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from examlops import service_auth

ROOT = Path(__file__).resolve().parents[2]
STATIC = "-".join(("static", "token", "0123456789"))


@pytest.fixture()
def token_file(tmp_path, monkeypatch):
    path = tmp_path / "control-plane.jwt"
    monkeypatch.setenv("CONTROL_PLANE_TOKEN_FILE", str(path))
    return path


def _rotate(path: Path, token: str) -> None:
    path.write_text(token + "\n")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))  # a distinct mtime


def test_the_file_wins_and_a_rotation_is_read_on_the_next_call(token_file):
    """Same length on purpose: a cache on (mtime, size) would miss this rotation."""
    _rotate(token_file, "svid-1")
    assert service_auth.control_plane_bearer(STATIC) == "svid-1"
    _rotate(token_file, "svid-2")
    assert service_auth.control_plane_bearer(STATIC) == "svid-2"


@pytest.mark.parametrize("content", [None, ""])
def test_a_missing_or_empty_file_falls_back_to_the_static_credential(token_file, content):
    if content is not None:
        token_file.write_text(content)
    assert service_auth.control_plane_bearer(STATIC) == STATIC


def test_without_a_file_the_static_credential_is_used(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", STATIC)
    assert service_auth.control_plane_bearer() == STATIC


def test_the_cli_and_the_autopilot_use_it(token_file, monkeypatch, tmp_path):
    from examlops.cli._config import load_config

    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", STATIC)
    _rotate(token_file, "svid-cli")
    assert load_config().control_plane_token == "svid-cli"


def test_the_dashboard_uses_it(token_file, monkeypatch):
    # `monkeypatch.syspath_prepend`, not `sys.path.insert`: this directory contains the dashboard's
    # own `alembic/` migrations, so leaving it on the path shadows the installed `alembic`
    # package for the rest of the session. Every later test that reaches MLflow's SQLAlchemy store
    # then dies with `ModuleNotFoundError: No module named 'alembic.migration'` — 15 of them in the
    # serial Postgres job, while `-n auto` hid it by putting the victims in other workers.
    monkeypatch.syspath_prepend(str(ROOT / "platform" / "services" / "dashboard" / "backend"))
    for name in ("DASHBOARD_VIEWER_PASSWORD", "DASHBOARD_ADMIN_PASSWORD"):
        monkeypatch.setenv(name, "x" * 16)
    monkeypatch.setenv("DASHBOARD_JWT_SECRET", "j" * 40)
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "k" * 43 + "=")
    import asyncio

    control_plane_auth = pytest.importorskip("control_plane_auth")
    _rotate(token_file, "svid-dashboard")
    assert asyncio.run(control_plane_auth.control_plane_token()) == "svid-dashboard"
    time.sleep(0)  # no cache between calls: a rotation is visible at once
    _rotate(token_file, "svid-dashboard-2")
    assert asyncio.run(control_plane_auth.control_plane_token()) == "svid-dashboard-2"
