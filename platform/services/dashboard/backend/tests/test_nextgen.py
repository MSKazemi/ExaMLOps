"""Next-Gen 40 read surface — /api/nextgen/* (E4/E5/E6/E7/E8 + A3).

Verifies the router surfaces federated runs, device pools/placements/bursts, autoscale,
distributed runs, gateway config, and feature views, is viewer-gated, and fails open (empty
result, never 500) when the tables/DB are absent.
"""

import sqlite3

import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    """A seeded platform.db wired into the nextgen router via PLATFORM_DB."""
    db = tmp_path / "platform.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE federated_runs (
            run_id TEXT PRIMARY KEY, strategy TEXT, dp_enabled INTEGER, secure_agg INTEGER,
            epsilon REAL, delta REAL, epsilon_per_round REAL, rounds_completed INTEGER,
            status TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE federated_sites (run_id TEXT, site TEXT, authorized INTEGER);
        CREATE TABLE federated_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, round_num INTEGER,
            global_metric REAL, sites_participated INTEGER, epsilon REAL
        );
        CREATE TABLE device_pools (
            name TEXT PRIMARY KEY, target TEXT, accelerator TEXT, capabilities TEXT,
            count INTEGER, region TEXT, cost_per_hour REAL, carbon_factor REAL,
            supports_fractions INTEGER, status TEXT
        );
        CREATE TABLE placement_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workload TEXT, accelerator_requested TEXT,
            device_chosen TEXT, pool TEXT, target TEXT, region TEXT, decision TEXT,
            fraction_honored INTEGER, reason TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE burst_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workload TEXT, from_pool TEXT, to_pool TEXT,
            residency TEXT, allowed INTEGER, reason TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE autoscale_config (
            model TEXT PRIMARY KEY, min_replicas INTEGER, max_replicas INTEGER
        );
        CREATE TABLE scale_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT, direction TEXT
        );
        CREATE TABLE distributed_runs (
            run_id TEXT PRIMARY KEY, strategy TEXT, status TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE inference_gateway_config (
            model TEXT, tenant TEXT, mode TEXT
        );
        CREATE TABLE feature_views (name TEXT PRIMARY KEY, entity TEXT, ttl INTEGER);
        """
    )
    conn.execute(
        "INSERT INTO federated_runs (run_id, strategy, dp_enabled, secure_agg, epsilon, delta, "
        "epsilon_per_round, rounds_completed, status) VALUES "
        "('fed-1','fedavg',1,1,0.5,1e-5,0.5,1,'running')"
    )
    conn.executemany(
        "INSERT INTO federated_sites (run_id, site, authorized) VALUES (?,?,?)",
        [("fed-1", "siteA", 1), ("fed-1", "siteB", 0)],
    )
    conn.execute(
        "INSERT INTO federated_rounds (run_id, round_num, global_metric, sites_participated, "
        "epsilon) VALUES ('fed-1', 1, 0.42, 1, 0.5)"
    )
    conn.execute(
        "INSERT INTO device_pools (name, target, accelerator, capabilities, count, region, "
        "cost_per_hour, carbon_factor, supports_fractions, status) VALUES "
        "('hpc-amd','hpc','amd','[\"fp8\"]',4,'eu',1.8,250,0,'active')"
    )
    conn.execute(
        "INSERT INTO placement_decisions (workload, accelerator_requested, device_chosen, pool, "
        "target, region, decision, fraction_honored, reason) VALUES "
        "('w1','amd','amd','hpc-amd','hpc','eu','placed',1,NULL)"
    )
    conn.execute(
        "INSERT INTO burst_events (workload, from_pool, to_pool, residency, allowed, reason) "
        "VALUES ('w1','hpc',NULL,'no-egress',0,'blocked')"
    )
    conn.execute(
        "INSERT INTO autoscale_config (model, min_replicas, max_replicas) VALUES ('JPCP',0,8)"
    )
    conn.execute("INSERT INTO scale_events (model, direction) VALUES ('JPCP','up')")
    conn.execute(
        "INSERT INTO distributed_runs (run_id, strategy, status) VALUES ('d-1','fsdp','running')"
    )
    conn.execute(
        "INSERT INTO inference_gateway_config (model, tenant, mode) VALUES "
        "('JPCP','default','cache_aware')"
    )
    conn.execute(
        "INSERT INTO feature_views (name, entity, ttl) VALUES ('user_activity','user',3600)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def _get(client, path, token):
    return await client.get(path, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.asyncio
async def test_requires_auth(client):
    r = await client.get("/api/nextgen/federated/runs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_federated_runs(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await _get(client, "/api/nextgen/federated/runs", token)
    assert r.status_code == 200
    runs = r.json()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "fed-1" and runs[0]["dp_enabled"] == 1


@pytest.mark.asyncio
async def test_federated_run_detail(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await _get(client, "/api/nextgen/federated/runs/fed-1", token)
    body = r.json()
    assert body["run"]["strategy"] == "fedavg"
    assert {s["site"]: s["authorized"] for s in body["sites"]} == {"siteA": 1, "siteB": 0}
    assert body["rounds"][0]["global_metric"] == 0.42


@pytest.mark.asyncio
async def test_device_pools_parses_capabilities(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await _get(client, "/api/nextgen/hardware/pools", token)
    pools = r.json()
    assert pools[0]["capabilities"] == ["fp8"]
    assert pools[0]["supports_fractions"] is False


@pytest.mark.asyncio
async def test_placements_and_bursts(client, platform_db):
    token = await _login(client, VIEWER_PW)
    p = await _get(client, "/api/nextgen/hardware/placements", token)
    assert p.json()[0]["decision"] == "placed"
    b = await _get(client, "/api/nextgen/hardware/bursts", token)
    assert b.json()[0]["allowed"] == 0


@pytest.mark.asyncio
async def test_autoscale_and_distributed_and_gateway(client, platform_db):
    token = await _login(client, VIEWER_PW)
    cfg = await _get(client, "/api/nextgen/autoscale/config", token)
    assert cfg.json()[0]["max_replicas"] == 8
    ev = await _get(client, "/api/nextgen/autoscale/events", token)
    assert ev.json()[0]["direction"] == "up"
    dist = await _get(client, "/api/nextgen/distributed/runs", token)
    assert dist.json()[0]["strategy"] == "fsdp"
    gw = await _get(client, "/api/nextgen/gateway/config", token)
    assert gw.json()[0]["mode"] == "cache_aware"


@pytest.mark.asyncio
async def test_feature_views(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await _get(client, "/api/nextgen/features/views", token)
    assert r.json()[0]["name"] == "user_activity"


@pytest.mark.asyncio
async def test_summary_counts(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await _get(client, "/api/nextgen/summary", token)
    s = r.json()
    assert s["federated_runs"] == 1 and s["device_pools"] == 1
    assert s["placements"] == 1 and s["feature_views"] == 1


@pytest.mark.asyncio
async def test_fails_open_without_tables(client, tmp_path, monkeypatch):
    # No tables at all → empty list, never a 500.
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    monkeypatch.setenv("PLATFORM_DB", str(empty))
    token = await _login(client, VIEWER_PW)
    r = await _get(client, "/api/nextgen/federated/runs", token)
    assert r.status_code == 200 and r.json() == []
    s = await _get(client, "/api/nextgen/summary", token)
    assert s.json()["federated_runs"] == 0
