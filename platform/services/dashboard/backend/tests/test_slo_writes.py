"""SLO write router — define spec / list (admin, audited, shared examlops path).

Verifies the C6 model-quality-SLO edit-parity: define/update an SLO spec through the shared
`examlops.slo.apply_spec` → `upsert_slo_spec` code path, admin + `slo.manage` gated, audited
`source=dashboard`.
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


async def test_set_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/slo",
        json={"model": "JPCP", "name": "availability", "target": 0.99},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_set_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/slo",
        json={"model": "JPCP", "name": "availability", "target": 0.995, "gatePromotion": True},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = sqlite3.connect(platform_db)
    row = conn.execute(
        "SELECT target, gate_promotion FROM slo_specs WHERE model='JPCP' AND name='availability'"
    ).fetchone()
    assert row == (0.995, 1)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='slo_set'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_set_rejects_out_of_range_target(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/slo", json={"model": "JPCP", "name": "x", "target": 1.5}, headers=h)
    assert r.status_code == 400
    r2 = await client.post("/api/slo", json={"model": "JPCP", "name": "x"}, headers=h)
    assert r2.status_code == 400  # missing target


async def test_list_returns_specs(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/api/slo", json={"model": "MACK", "name": "latency", "target": 0.98}, headers=h
    )
    r = await client.get("/api/slo", headers=h)
    assert r.status_code == 200
    specs = {(s["model"], s["name"]): s for s in r.json()}
    assert specs[("MACK", "latency")]["target"] == 0.98
    # Live status is best-effort; with no samples it is either null or a trivial (ok=True) rollup.
    st = specs[("MACK", "latency")]["status"]
    assert st is None or st["ok"] is True
