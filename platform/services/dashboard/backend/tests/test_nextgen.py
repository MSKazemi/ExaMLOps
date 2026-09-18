"""Next-Gen 40 read surface — /api/nextgen/* (E4/E5/E6/E7/E8 + A3).

Verifies the router surfaces federated runs, device pools/placements/bursts, autoscale,
distributed runs, gateway config, and feature views, is viewer-gated, and fails open (empty
result, never 500) when the tables/DB are absent.
"""

import dbconn
import pytest

from examlops import platform_db as pdb
from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    """A seeded platform.db wired into the nextgen router via PLATFORM_DB."""
    db = tmp_path / "platform.db"
    # Build the *real* platform schema rather than a hand-rolled approximation of it. The router
    # reads these tables with ``SELECT *``, so a simplified local DDL let the test assert columns
    # the product does not have — and it only stayed hidden because each SQLite test got its own
    # file. Under Postgres the tables already exist in the shared schema, so ``CREATE TABLE IF NOT
    # EXISTS`` was a silent no-op and the invented shape evaporated.
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
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
    # The real `scale_events` records replica counts, not a `direction` string; the router
    # reads it with SELECT *, so seed the columns that actually exist.
    conn.execute(
        "INSERT INTO scale_events (model, from_replicas, to_replicas, reason) "
        "VALUES ('JPCP', 1, 3, 'load')"
    )
    conn.execute(
        # `model` is NOT NULL in the real schema
        "INSERT INTO distributed_runs (run_id, model, strategy, status) "
        "VALUES ('d-1','JPCP','fsdp','running')"
    )
    conn.execute(
        "INSERT INTO inference_gateway_config (model, tenant, mode) VALUES "
        "('JPCP','default','cache_aware')"
    )
    conn.execute(
        "INSERT INTO feature_views (name, entity, features_json, ttl_seconds) "
        "VALUES ('user_activity','user','[\"embedding\"]',3600)"
    )
    conn.commit()
    conn.close()
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
    assert ev.json()[0]["to_replicas"] == 3
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
    dbconn.connect(empty, row_factory=None).close()
    monkeypatch.setenv("PLATFORM_DB", str(empty))
    token = await _login(client, VIEWER_PW)
    r = await _get(client, "/api/nextgen/federated/runs", token)
    assert r.status_code == 200 and r.json() == []
    s = await _get(client, "/api/nextgen/summary", token)
    assert s.json()["federated_runs"] == 0


@pytest.mark.asyncio
async def test_another_tenants_events_do_not_crowd_out_the_callers_own(client, platform_db):
    """`LIMIT` must not run before the tenant filter.

    The query took the newest N rows across **every** tenant and `scope_to_tenant` then dropped the
    ones belonging to others. On a busy platform a caller's own events are pushed out of that
    window by tenants they cannot see, and the page shows fewer rows than exist — or none — while
    saying nothing. Here the caller's single event is older than three belonging to `other`.
    """
    conn = dbconn.connect(pdb._db_path(), row_factory=None)
    conn.executemany(
        "INSERT INTO scale_events (model, from_replicas, to_replicas, reason, tenant) "
        "VALUES (?,?,?,?,?)",
        [("JPCP", 1, 9, "load", "other")] * 3,
    )
    conn.commit()
    conn.close()

    token = await _login(client, VIEWER_PW)
    r = await _get(client, "/api/nextgen/autoscale/events?limit=2", token)

    rows = r.json()
    assert rows, "the caller's own scale events were crowded out by another tenant's"
    assert all(row.get("tenant") in (None, "default") for row in rows), rows


@pytest.mark.asyncio
async def test_a_platform_admin_still_sees_every_tenants_events(client, platform_db):
    """The SQL predicate must not narrow a cross-tenant admin (F15 R4).

    `tenant_sql_filter` returns `1=1` for a platform admin, which is the half a well-meaning
    "always filter by tenant" simplification would remove — silently hiding other centres' events
    from the one role that is supposed to see them.
    """
    conn = dbconn.connect(pdb._db_path(), row_factory=None)
    conn.execute(
        "INSERT INTO scale_events (model, from_replicas, to_replicas, reason, tenant) "
        "VALUES ('MACK', 1, 4, 'load', 'other')"
    )
    conn.commit()
    conn.close()

    token = await _login(client, ADMIN_PW)
    r = await _get(client, "/api/nextgen/autoscale/events?limit=50", token)

    tenants = {row.get("tenant") for row in r.json()}
    assert "other" in tenants, f"a platform admin saw only {tenants}"
