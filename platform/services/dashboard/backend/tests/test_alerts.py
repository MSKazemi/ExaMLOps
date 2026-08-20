"""Alert aggregation + /api/v1/alerts endpoints (F12 / ADR 0062)."""

import json

import alerts
import dbconn
import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = dbconn.connect(db, row_factory=None)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS drift_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME, model TEXT, alias TEXT,
            prediction REAL, job_id TEXT
        );
        CREATE TABLE IF NOT EXISTS drift_baselines (model TEXT PRIMARY KEY, stats TEXT, set_at DATETIME);
        CREATE TABLE IF NOT EXISTS project_budgets (
            project TEXT PRIMARY KEY, gpu_hours_budget REAL, cost_budget REAL,
            period TEXT, updated_at TEXT, updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS model_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model_name TEXT, gpu_hours REAL, cost_usd REAL
        );
        CREATE TABLE IF NOT EXISTS eval_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, model TEXT, suite TEXT, status TEXT, actor TEXT);
        CREATE TABLE IF NOT EXISTS eval_results (id INTEGER PRIMARY KEY AUTOINCREMENT, eval_run_id INTEGER, metric TEXT, value REAL, baseline REAL, passed INTEGER);
        CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, actor TEXT, action TEXT, target TEXT, details TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP);
        """
    )
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
    conn.execute("INSERT INTO model_costs (model_name, cost_usd) VALUES ('jpcp', 20.0)")
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
    monkeypatch.setenv("PLATFORM_DB", str(db))
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
    conn = dbconn.connect(db, row_factory=None)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS drift_snapshots (id INTEGER PRIMARY KEY, model TEXT, prediction REAL);"
        "CREATE TABLE IF NOT EXISTS drift_baselines (model TEXT PRIMARY KEY, stats TEXT);"
    )
    conn.execute(
        "INSERT INTO drift_baselines (model, stats) VALUES ('m', ?)",
        (json.dumps({"mean": 1.0, "std": 1.0}),),
    )
    conn.execute("INSERT INTO drift_snapshots (model, prediction) VALUES ('m', 1.2)")  # z=0.2
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
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
