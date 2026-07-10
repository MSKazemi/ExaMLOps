"""Dashboard self-observability: metrics + status page + UI audit (F24 / ADR 0067)."""

import sqlite3

import pytest
import selfobs

from tests.conftest import VIEWER_PW


# ── metrics collector (F24 R4) ───────────────────────────────────────────────


def test_metrics_counts_status_classes():
    m = selfobs.Metrics()
    m.record(200, 5.0)
    m.record(404, 3.0)
    m.record(429, 1.0)
    m.record(503, 9.0)
    snap = m.snapshot()
    assert snap["requests"] == 4
    assert snap["clientErrors"] == 2  # 404 + 429
    assert snap["rateLimitHits"] == 1
    assert snap["errors"] == 1  # 503
    assert snap["latencyMs"]["count"] == 4


def test_metrics_percentiles_on_empty():
    snap = selfobs.Metrics().snapshot()
    assert snap["latencyMs"]["p50"] is None


# ── dependency health / status payload (F24 R5) ──────────────────────────────


def test_status_payload_reports_platform_db_up(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    sqlite3.connect(db).close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    payload = selfobs.status_payload()
    names = {d["name"] for d in payload["dependencies"]}
    assert "platform_db" in names and "bff" in names
    assert payload["status"] == "up"
    assert "metrics" in payload


# ── UI-action audit (F24 R4 / D4) ────────────────────────────────────────────


def test_record_ui_action_writes_audit(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, "
        "actor TEXT, action TEXT, target TEXT, details TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))

    assert selfobs.record_ui_action("view_model", "JPCP", "viewer") is True
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT source, actor, action, target FROM audit_events").fetchone()
    conn.close()
    assert row == ("dashboard-ui", "viewer", "view_model", "JPCP")


def test_record_ui_action_graceful_without_table(tmp_path, monkeypatch):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    assert selfobs.record_ui_action("x", "y", "viewer") is False


# ── endpoints ────────────────────────────────────────────────────────────────


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.asyncio
async def test_status_endpoint_requires_auth(client):
    r = await client.get("/api/v1/selfobs/status")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_status_endpoint_returns_health_and_metrics(client, tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    sqlite3.connect(db).close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/selfobs/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("up", "degraded")
    # the metrics middleware has been counting requests during this test
    assert body["metrics"]["requests"] >= 1


@pytest.mark.asyncio
async def test_action_endpoint_audits(client, tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, "
        "actor TEXT, action TEXT, target TEXT, details TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/selfobs/action",
        json={"action": "open_page", "target": "/mlops"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["audited"] is True
