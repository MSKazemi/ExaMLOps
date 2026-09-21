"""Policy-as-code at the control plane's retrain decision point (ADR 0079 d2 / ADR 0029 d3).

`exa retrain` already consults the ``retrain`` rule. The route it calls did not, so the dashboard's
trigger, the bus bridge and any direct API caller ran retrains a ``policy.yaml`` said to refuse.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

HEADERS = {"Authorization": "Bearer test-token-0123456789"}
BODY = {"model_name": "JPCP", "dataset_name": "PM100Dataset"}


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
    cp_app._policy_path = tmp_path / "policy.yaml"
    return cp_app


def _audit(cp, prefix):
    import os

    conn = sqlite3.connect(os.environ["PLATFORM_DB"])
    try:
        return conn.execute(
            "SELECT action, actor, source, details FROM audit_events WHERE action LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def test_default_unchanged_and_no_policy_audit(cp):
    r = TestClient(cp.app).post("/v1/retrain", json=BODY, headers=HEADERS)
    assert r.status_code == 202
    assert _audit(cp, "policy") == []


@pytest.mark.parametrize("path", ["/v1/retrain", "/retrain"])
def test_deny_is_403_names_the_rule_and_nothing_is_queued(cp, path):
    cp._policy_path.write_text(
        "policies:\n  - name: no-retrain\n    action: retrain\n"
        "    when: \"model == 'JPCP'\"\n    effect: deny\n"
    )
    r = TestClient(cp.app).post(path, json=BODY, headers=HEADERS)
    assert r.status_code == 403
    assert "no-retrain" in r.text
    rows = _audit(cp, "policy:retrain")
    assert len(rows) == 1 and rows[0][2] == "control-plane"


def test_same_rule_as_the_cli_context_keys(cp):
    """`model`/`dataset`/`dummy` — the keys `exa retrain` supplies — work here unchanged."""
    cp._policy_path.write_text(
        "policies:\n  - name: dummy-only\n    action: retrain\n"
        "    when: \"dataset == 'PM100Dataset' and not dummy\"\n    effect: deny\n"
    )
    c = TestClient(cp.app)
    assert c.post("/v1/retrain", json=BODY, headers=HEADERS).status_code == 403
    assert (
        c.post("/v1/retrain", json={**BODY, "is_dummy": True}, headers=HEADERS).status_code == 202
    )


def test_require_approval_needs_the_ack_header_then_proceeds(cp):
    cp._policy_path.write_text(
        "policies:\n  - name: four-eyes\n    action: retrain\n    effect: require_approval\n"
    )
    c = TestClient(cp.app)
    r = c.post("/v1/retrain", json=BODY, headers=HEADERS)
    assert r.status_code == 409 and "four-eyes" in r.text
    r2 = c.post("/v1/retrain", json=BODY, headers={**HEADERS, "X-Policy-Approved": "true"})
    assert r2.status_code == 202
    assert len(_audit(cp, "policy_approval:retrain")) == 1


def test_monitor_rule_never_blocks(cp):
    cp._policy_path.write_text(
        "policies:\n  - name: trial\n    action: retrain\n    effect: deny\n    mode: monitor\n"
    )
    assert TestClient(cp.app).post("/v1/retrain", json=BODY, headers=HEADERS).status_code == 202
    assert len(_audit(cp, "policy_monitor:retrain")) == 1


def test_engine_failure_denies_and_is_audited(cp, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr("examlops.policy.decide", boom)
    r = TestClient(cp.app).post("/v1/retrain", json=BODY, headers=HEADERS)
    assert r.status_code == 403
    assert len(_audit(cp, "policy_unavailable:retrain")) == 1


def test_scope_is_checked_before_policy(cp):
    """No credential / wrong scope: the auth answer, never a policy verdict or audit row."""
    cp._policy_path.write_text("policies:\n  - action: retrain\n    effect: deny\n")
    r = TestClient(cp.app).post("/v1/retrain", json=BODY)
    assert r.status_code in (401, 403) and "policy" not in r.text.lower()
    assert _audit(cp, "policy") == []
