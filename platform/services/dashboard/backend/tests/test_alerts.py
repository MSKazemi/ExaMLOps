"""Alert aggregation + /api/v1/alerts endpoints (F12 / ADR 0062)."""

import json

import alerts
import dbconn
import pytest

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    # Build the *real* platform schema instead of a hand-rolled subset of it. The subset was a
    # column subset — no `model_costs.version`, no `recorded_at` — so a seed written against the
    # product's actual NOT NULL columns failed here while passing on Postgres, where the real
    # schema already exists and `CREATE TABLE IF NOT EXISTS` is a no-op. One schema, both engines.
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    # drift: baseline mean 1.0 std 0.5; latest mean ~5 → z=8 → critical
    conn.execute(
        "INSERT INTO drift_baselines (model, stats) VALUES ('jpcp', ?)",
        (json.dumps({"mean": 1.0, "std": 0.5}),),
    )
    conn.executemany(
        "INSERT INTO drift_snapshots (model, alias, prediction) VALUES (?,?,?)",
        [("jpcp", "Production", 5.0), ("jpcp", "Production", 5.0)],
    )
    # budget: consumed 20 > budget 15 → error
    conn.execute("INSERT INTO project_budgets (project, cost_budget) VALUES ('eu-hpc', 15.0)")
    conn.execute(  # `version` and `recorded_at` are NOT NULL in the real schema
        "INSERT INTO model_costs (model_name, version, cost_usd, recorded_at) "
        "VALUES ('jpcp', 1, 20.0, '2026-01-01T00:00:00')"
    )
    # eval regression: latest run has a failed metric → warn
    conn.execute(
        "INSERT INTO eval_runs (id, model, suite, status) VALUES (1, 'llama3', 'mmlu', 'complete')"
    )
    conn.executemany(
        "INSERT INTO eval_results (eval_run_id, metric, value, baseline, passed) VALUES (?,?,?,?,?)",
        [(1, "accuracy", 0.7, 0.8, 0), (1, "latency", 0.1, 0.2, 1)],
    )
    conn.commit()
    conn.close()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── aggregation (F12 R1) ─────────────────────────────────────────────────────


def test_active_alerts_merges_sources_severity_sorted(platform_db):
    out = alerts.active_alerts(platform_db)
    sources = [a["source"] for a in out["alerts"]]
    assert set(sources) == {"drift", "budget", "eval"}
    # critical (drift) sorts before error (budget) before warn (eval)
    assert out["alerts"][0]["severity"] == "critical"
    assert out["alerts"][0]["source"] == "drift"
    assert out["counts"] == {"critical": 1, "error": 1, "warn": 1}


def test_no_drift_alert_within_baseline(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()  # the real schema, so `alias NOT NULL` is the same constraint on both engines
    conn = dbconn.connect(db, row_factory=None)
    conn.execute(
        "INSERT INTO drift_baselines (model, stats) VALUES ('m', ?)",
        (json.dumps({"mean": 1.0, "std": 1.0}),),
    )
    conn.execute(  # z=0.2 — `alias` is NOT NULL in the real schema, so it must be seeded
        "INSERT INTO drift_snapshots (model, alias, prediction) VALUES ('m', 'Production', 1.2)"
    )
    conn.commit()
    conn.close()
    assert alerts.active_alerts(str(db))["count"] == 0


def test_active_alerts_graceful_empty(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    dbconn.connect(db, row_factory=None).close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    assert alerts.active_alerts(str(db)) == {"alerts": [], "count": 0, "counts": {}}


# ── acknowledge (F12 R3 / D4) ────────────────────────────────────────────────


def test_acknowledge_writes_audit(platform_db):
    assert alerts.acknowledge(platform_db, "drift:jpcp", "admin") is True
    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT source, action, target FROM audit_events WHERE action='alert_ack'"
    ).fetchone()
    conn.close()
    assert row == ("dashboard-alerts", "alert_ack", "drift:jpcp")


# ── endpoints ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alerts_endpoint_requires_auth(client, platform_db):
    r = await client.get("/api/v1/alerts")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_alerts_endpoint_lists(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/alerts", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    inbox = r.json()["inbox"]
    assert inbox["count"] == 3


@pytest.mark.asyncio
async def test_ack_endpoint_audits_and_publishes(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/alerts/drift:jpcp/ack", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["acked"] is True and body["audited"] is True
