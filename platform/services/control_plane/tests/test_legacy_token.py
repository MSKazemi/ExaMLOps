"""The shared legacy token can be watched, then retired (plan P3.2).

``CONTROL_PLANE_TOKEN`` grants read and write — every action — however narrowly the per-service
credentials are scoped. ``CONTROL_PLANE_LEGACY_TOKEN`` retires it in steps: ``on`` (as before),
``warn`` (accepted, counted and logged so its remaining callers can be found), ``off`` (refused).
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

LEGACY = "-".join(("legacy", "token", "0123456789"))
SERVICE = "-".join(("bridge", "token", "0123456789"))
CREDENTIALS = json.dumps(
    {SERVICE: {"principal": "bridge", "tenant": "default", "scopes": ["read", "retrain"]}}
)


def _load(monkeypatch, tmp_path, mode: str | None, credentials: str = CREDENTIALS):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", LEGACY)
    monkeypatch.setenv("CONTROL_PLANE_CREDENTIALS_JSON", credentials)
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "legacy.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_WORKERS", "0")
    if mode is None:
        monkeypatch.delenv("CONTROL_PLANE_LEGACY_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CONTROL_PLANE_LEGACY_TOKEN", mode)
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


def _uses(client: TestClient) -> float:
    body = client.get("/metrics").text
    return float(
        next(
            line
            for line in body.splitlines()
            if line.startswith("control_plane_legacy_token_uses_total ")
        ).rsplit(" ", 1)[1]
    )


def _status(client: TestClient, token: str) -> int:
    return client.get("/v1/commands", headers={"Authorization": f"Bearer {token}"}).status_code


def test_on_by_default_and_every_use_is_counted(monkeypatch, tmp_path):
    cp = _load(monkeypatch, tmp_path, None)
    client = TestClient(cp.app)
    before = _uses(client)
    assert _status(client, LEGACY) == 200
    assert _uses(client) == before + 1
    assert cp._runtime_capabilities()["legacy_token"] == "on"


def test_warn_accepts_it_and_says_who_at_most_once_a_minute(monkeypatch, tmp_path):
    cp = _load(monkeypatch, tmp_path, "warn")
    said: list[str] = []
    monkeypatch.setattr(cp.logger, "warning", lambda msg, *args: said.append(msg % args))
    client = TestClient(cp.app)
    assert _status(client, LEGACY) == 200
    assert _status(client, LEGACY) == 200
    warnings = [m for m in said if "legacy CONTROL_PLANE_TOKEN was used" in m]
    assert len(warnings) == 1
    assert "testclient" in warnings[0]  # the caller is named


def test_off_refuses_it_and_the_service_credentials_keep_working(monkeypatch, tmp_path):
    cp = _load(monkeypatch, tmp_path, "off")
    client = TestClient(cp.app)
    refused = client.get("/v1/commands", headers={"Authorization": f"Bearer {LEGACY}"})
    assert refused.status_code == 403 and "legacy token is disabled" in refused.text
    assert _status(client, SERVICE) == 200


def test_off_with_nothing_else_configured_is_reported_not_silently_open(monkeypatch, tmp_path):
    cp = _load(monkeypatch, tmp_path, "off", credentials="")
    client = TestClient(cp.app)
    assert _status(client, LEGACY) == 503  # no usable credential at all
    cp._run_startup_checks()
    assert cp._startup_checks["token"] == "missing"


@pytest.mark.parametrize("typo", ["of", "disabled", "false"])
def test_an_unknown_mode_is_off_and_fails_the_startup_check(monkeypatch, tmp_path, typo):
    cp = _load(monkeypatch, tmp_path, typo)
    assert cp.LEGACY_TOKEN_MODE == "off"
    client = TestClient(cp.app)
    assert (
        client.get("/v1/commands", headers={"Authorization": f"Bearer {LEGACY}"}).status_code == 403
    )
    cp._run_startup_checks()
    assert cp._startup_checks["legacy_token"].startswith("fail")
