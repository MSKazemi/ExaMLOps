"""Scaling & Routing write router — set autoscale policy / routing config (admin, audited, shared path).

Verifies the E4/E5 edit-parity: set a model's autoscale policy through the shared
`examlops.autoscale.set_policy` → `set_autoscale_config` code path and its routing config through
`examlops.data.gateway.set_gateway_config`, admin + `scaling.manage` gated, audited
`source=dashboard`. Reads surface the recorded config/events/stats (pure platform.db).
"""

import sqlite3

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    from examlops import data as pdb

    pdb.init_db(force=True)
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── autoscale ─────────────────────────────────────────────────────────────────


async def test_autoscale_set_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/scaling/autoscale",
        json={"model": "JPCP", "minReplicas": 0, "maxReplicas": 8},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_autoscale_set_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/scaling/autoscale",
        json={
            "model": "JPCP",
            "minReplicas": 0,
            "maxReplicas": 8,
            "targetMetric": "queue_depth",
            "targetValue": 12.0,
            "scaleToZeroAfterS": 300,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = sqlite3.connect(platform_db)
    row = conn.execute(
        "SELECT min_replicas, max_replicas, target_value, scale_to_zero_after_s "
        "FROM autoscale_config WHERE model='JPCP'"
    ).fetchone()
    assert row == (0, 8, 12.0, 300)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE source='dashboard' AND action='autoscale_policy_set'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_autoscale_set_rejects_bad_replicas(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/scaling/autoscale",
        json={"model": "JPCP", "minReplicas": 5, "maxReplicas": 2},
        headers=h,
    )
    assert r.status_code == 400
    r2 = await client.post("/api/v1/scaling/autoscale", json={"maxReplicas": 2}, headers=h)
    assert r2.status_code == 400  # missing model


async def test_autoscale_get_returns_config(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/api/v1/scaling/autoscale",
        json={"model": "MACK", "minReplicas": 1, "maxReplicas": 4, "scaleToZeroAfterS": 120},
        headers=h,
    )
    r = await client.get("/api/v1/scaling/autoscale", params={"model": "MACK"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["max_replicas"] == 4
    assert body["savings"] is not None  # scale-to-zero enabled → savings estimate present
    assert isinstance(body["events"], list)


async def test_autoscale_get_missing_policy_fails_open(client, platform_db):
    token = await _login(client, VIEWER_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.get("/api/v1/scaling/autoscale", params={"model": "NOPE"}, headers=h)
    assert r.status_code == 200
    assert r.json()["config"] is None


# ── routing ───────────────────────────────────────────────────────────────────


async def test_routing_set_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/scaling/routing",
        json={"model": "JPCP", "mode": "cache_aware"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_routing_set_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/scaling/routing",
        json={"model": "JPCP", "mode": "cache_aware", "sloLatencyMs": 500, "disaggregate": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = sqlite3.connect(platform_db)
    row = conn.execute(
        "SELECT mode, slo_latency_ms, disaggregate FROM inference_gateway_config "
        "WHERE model='JPCP' AND tenant='default'"
    ).fetchone()
    assert row == ("cache_aware", 500.0, 1)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE source='dashboard' AND action='routing_config_set'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_routing_set_rejects_bad_mode(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/scaling/routing", json={"model": "JPCP", "mode": "bogus"}, headers=h
    )
    assert r.status_code == 400


async def test_routing_get_returns_config_and_stats(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/api/v1/scaling/routing", json={"model": "MACK", "mode": "round_robin"}, headers=h
    )
    r = await client.get("/api/v1/scaling/routing", params={"model": "MACK"}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["mode"] == "round_robin"
    assert body["stats"]["total"] == 0  # no recorded routing events yet
