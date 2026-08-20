"""Drift write routers — baseline / reset / auto-retrain (admin, audited).

Verifies the editability added to the Drift page: every mutation requires admin, is audited, and
goes through the shared examlops.data.drift code paths (so the dashboard can't drift from the CLI).
"""

import json

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = dbconn.connect(db, row_factory=None)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS drift_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, alias TEXT,
            prediction REAL NOT NULL, job_id TEXT, ts DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS drift_baselines (model TEXT PRIMARY KEY, stats TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS drift_auto_retrain (
            model TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
            min_z_score REAL NOT NULL DEFAULT 3.0, dataset_name TEXT NOT NULL DEFAULT '',
            cooldown_s INTEGER NOT NULL DEFAULT 3600
        );
        CREATE TABLE IF NOT EXISTS input_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, alias TEXT,
            emb_norm REAL NOT NULL, emb_mean REAL NOT NULL, emb_std REAL NOT NULL,
            job_id TEXT, ts DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS input_baselines (model TEXT PRIMARY KEY, stats TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, details TEXT
        );
        """
    )
    for i in range(20):
        conn.execute(
            "INSERT INTO drift_snapshots (model, alias, prediction) VALUES ('JPCP','Production',?)",
            (5.0 + (i % 3) * 0.1,),
        )
        conn.execute(
            "INSERT INTO input_snapshots (model, alias, emb_norm, emb_mean, emb_std) "
            "VALUES ('JPCP','Production',?,?,?)",
            (10.0 + (i % 4) * 0.2, 0.1 + (i % 3) * 0.01, 0.5 + (i % 2) * 0.02),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def test_baseline_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post("/api/drift/baseline/JPCP", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_baseline_dry_run_then_set(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    dry = await client.post("/api/drift/baseline/JPCP?dry_run=true", headers=h)
    assert dry.status_code == 200, dry.text
    assert dry.json()["dryRun"] is True
    assert dry.json()["wouldSet"]["n"] == 20
    # Nothing written yet.
    conn = dbconn.connect(platform_db, row_factory=None)
    assert conn.execute("SELECT COUNT(*) FROM drift_baselines").fetchone()[0] == 0
    conn.close()
    # Real set.
    r = await client.post("/api/drift/baseline/JPCP", headers=h)
    assert r.status_code == 200, r.text
    assert "baseline" in r.json()
    conn = dbconn.connect(platform_db, row_factory=None)
    stats = json.loads(
        conn.execute("SELECT stats FROM drift_baselines WHERE model='JPCP'").fetchone()[0]
    )
    assert stats["n"] == 20
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='drift_baseline_set'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_baseline_too_few_snapshots_400(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/drift/baseline/UNKNOWN", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 400


async def test_reset_snapshots(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    dry = await client.post("/api/drift/reset/JPCP?dry_run=true", headers=h)
    assert dry.json()["wouldClear"] == 20
    r = await client.post("/api/drift/reset/JPCP", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] == 20
    conn = dbconn.connect(platform_db, row_factory=None)
    assert (
        conn.execute("SELECT COUNT(*) FROM drift_snapshots WHERE model='JPCP'").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='drift_reset'").fetchone()[0]
        == 1
    )
    conn.close()


async def test_auto_retrain_enable_requires_dataset(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/drift/auto-retrain/JPCP",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


async def test_auto_retrain_enable_then_disable(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    en = await client.post(
        "/api/drift/auto-retrain/JPCP",
        json={"enabled": True, "dataset": "PM100Dataset", "minZ": 2.5, "cooldown": 1800},
        headers=h,
    )
    assert en.status_code == 200, en.text
    assert en.json()["enabled"] is True
    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT enabled, dataset_name, min_z_score, cooldown_s FROM drift_auto_retrain WHERE model='JPCP'"
    ).fetchone()
    assert row == (1, "PM100Dataset", 2.5, 1800)
    conn.close()
    dis = await client.post("/api/drift/auto-retrain/JPCP", json={"enabled": False}, headers=h)
    assert dis.status_code == 200
    assert dis.json()["enabled"] is False
    conn = dbconn.connect(platform_db, row_factory=None)
    assert (
        conn.execute("SELECT enabled FROM drift_auto_retrain WHERE model='JPCP'").fetchone()[0] == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='drift_auto_retrain_configured'"
        ).fetchone()[0]
        == 2
    )
    conn.close()


# ── input-drift edit parity (BL-014) ──────────────────────────────────────────


async def test_input_baseline_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/drift/input-baseline/JPCP", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


async def test_input_baseline_dry_run_then_set(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    dry = await client.post("/api/drift/input-baseline/JPCP?dry_run=true", headers=h)
    assert dry.status_code == 200, dry.text
    assert dry.json()["dryRun"] is True
    assert dry.json()["wouldSet"]["n"] == 20
    conn = dbconn.connect(platform_db, row_factory=None)
    assert conn.execute("SELECT COUNT(*) FROM input_baselines").fetchone()[0] == 0
    conn.close()
    r = await client.post("/api/drift/input-baseline/JPCP", headers=h)
    assert r.status_code == 200, r.text
    conn = dbconn.connect(platform_db, row_factory=None)
    stats = json.loads(
        conn.execute("SELECT stats FROM input_baselines WHERE model='JPCP'").fetchone()[0]
    )
    # The CLI stat shape: per-metric mean + its std, plus n.
    assert set(stats) == {
        "norm_mean",
        "norm_mean_std",
        "mean_mean",
        "mean_mean_std",
        "std_mean",
        "std_mean_std",
        "n",
    }
    assert stats["n"] == 20
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='input_baseline_set'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_input_baseline_too_few_snapshots_400(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/drift/input-baseline/UNKNOWN", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 400


async def test_input_reset(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    dry = await client.post("/api/drift/input-reset/JPCP?dry_run=true", headers=h)
    assert dry.json()["wouldClear"] == 20
    r = await client.post("/api/drift/input-reset/JPCP", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["cleared"] == 20
    conn = dbconn.connect(platform_db, row_factory=None)
    assert (
        conn.execute("SELECT COUNT(*) FROM input_snapshots WHERE model='JPCP'").fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM audit_events WHERE action='input_reset'").fetchone()[0]
        == 1
    )
    conn.close()


async def test_input_reset_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/drift/input-reset/JPCP", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403
