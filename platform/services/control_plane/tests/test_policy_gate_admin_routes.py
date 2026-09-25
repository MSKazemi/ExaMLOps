"""Policy on the control plane's approval and admin routes (ADR 0079 d2, ADR 0029 d3).

These routes were recorded as exempt "open gaps" because no CLI command decided under a shared
action name. ``examlops.cli._policy_hook`` now gates every mutating ``exa`` command, so
``exa approvals approve`` decides as ``approval_approve``, ``exa production reload`` as
``production_reload`` and so on — and the routes those commands call decide under the same
names, so one ``policy.yaml`` rule governs the terminal, the dashboard and a direct API call.
"""

from __future__ import annotations

import importlib
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

HEADERS = {"Authorization": "Bearer test-token-0123456789"}


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token-0123456789")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "sqlite")
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_EVENT_RELAY_SECONDS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_WORKERS", "0")
    monkeypatch.setenv("CONTROL_PLANE_COMMAND_BACKOFF_SECONDS", "0")
    monkeypatch.setattr("examlops.policy.POLICY_YAML", tmp_path / "policy.yaml")
    import app as cp_app

    importlib.reload(cp_app)
    cp_app._platform_schema_ready = False
    monkeypatch.setattr(cp_app, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})
    monkeypatch.setattr(cp_app, "_run_poll_cycle", lambda: {})
    monkeypatch.setattr(cp_app, "_run_startup_checks", lambda: None)
    cp_app._policy_path = tmp_path / "policy.yaml"
    return cp_app


def _audit(prefix: str) -> list[tuple]:
    conn = sqlite3.connect(os.environ["PLATFORM_DB"])
    try:
        return conn.execute(
            "SELECT action, source FROM audit_events WHERE action LIKE ?", (f"{prefix}%",)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _settings(cp) -> dict:
    return TestClient(cp.app).get("/modelzoo/config", headers=HEADERS).json()


ROUTES = [
    ("post", "/v1/approvals/JPCP/approve", "approval_approve", None),
    ("post", "/approve/JPCP", "approval_approve", None),
    ("post", "/v1/approvals/JPCP/reject", "approval_reject", {"reason": "no"}),
    ("post", "/reject/JPCP", "approval_reject", {"reason": "no"}),
    ("post", "/v1/modelzoo/sync", "modelzoo_sync", None),
    ("put", "/v1/modelzoo/config", "modelzoo_config_set", {"auto_retrain": False}),
    ("post", "/v1/admin/reload", "production_reload", None),
]


def _call(cp, method, path, body, headers=HEADERS):
    client = TestClient(cp.app)
    return getattr(client, method)(path, headers=headers, **({"json": body} if body else {}))


@pytest.mark.parametrize(("method", "path", "action", "body"), ROUTES)
def test_no_rule_is_unchanged_and_writes_no_policy_row(cp, method, path, action, body):
    r = _call(cp, method, path, body)
    assert "policy" not in r.text.lower()
    assert _audit("policy") == []


@pytest.mark.parametrize(("method", "path", "action", "body"), ROUTES)
def test_deny_is_403_naming_the_rule_and_audited(cp, method, path, action, body):
    cp._policy_path.write_text(
        f"policies:\n  - name: stop\n    action: {action}\n    effect: deny\n"
    )
    r = _call(cp, method, path, body)
    assert r.status_code == 403 and "stop" in r.text
    rows = _audit(f"policy:{action}")
    assert len(rows) == 1 and rows[0][1] == "control-plane"


@pytest.mark.parametrize(("method", "path", "action", "body"), ROUTES)
def test_require_approval_is_409_until_acknowledged(cp, method, path, action, body):
    cp._policy_path.write_text(
        f"policies:\n  - name: four-eyes\n    action: {action}\n    effect: require_approval\n"
    )
    assert _call(cp, method, path, body).status_code == 409
    r = _call(cp, method, path, body, headers={**HEADERS, "X-Policy-Approved": "true"})
    assert r.status_code != 409
    assert len(_audit(f"policy_approval:{action}")) == 1


def test_denied_config_change_changes_nothing(cp):
    before = _settings(cp)
    cp._policy_path.write_text(
        'policies:\n  - action: modelzoo_config_set\n    when: "not auto_retrain"\n'
        "    effect: deny\n"
    )
    r = _call(cp, "put", "/v1/modelzoo/config", {"auto_retrain": not before["auto_retrain"]})
    if before["auto_retrain"]:  # the change would switch it off → denied, nothing written
        assert r.status_code == 403
        assert _settings(cp)["auto_retrain"] == before["auto_retrain"]
    else:
        assert r.status_code == 200


def test_approval_condition_reads_the_model_key(cp):
    """`model` — the key `exa approvals approve JPCP` and the dashboard both supply."""
    cp._policy_path.write_text(
        "policies:\n  - action: approval_approve\n    when: \"model == 'OTHER'\"\n    effect: deny\n"
    )
    r = _call(cp, "post", "/v1/approvals/JPCP/approve", None)
    assert r.status_code == 404  # no match → gate silent → the route's own answer (no pending row)


def test_scope_is_checked_before_policy(cp):
    cp._policy_path.write_text("policies:\n  - action: production_reload\n    effect: deny\n")
    r = TestClient(cp.app).post("/v1/admin/reload")
    assert r.status_code in (401, 403) and "policy" not in r.text.lower()
    assert _audit("policy") == []
