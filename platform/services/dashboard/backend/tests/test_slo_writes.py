"""SLO write router — define spec / list (admin, audited, shared examlops path).

Verifies the C6 model-quality-SLO edit-parity: define/update an SLO spec through the shared
`examlops.slo.apply_spec` → `upsert_slo_spec` code path, admin + `slo.manage` gated, audited
`source=dashboard`.
"""

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    from examlops import data as pdb

    # force=False on purpose: the DDL is cached per engine (SQLite: this tmp path, never seen
    # before; Postgres: this schema, already built), and re-running 127 CREATE TABLEs per test
    # cost ~30s each there. Row isolation is the autouse fixture in conftest, not the DDL.
    pdb.init_db()
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
    conn = dbconn.connect(platform_db, row_factory=None)
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
    # Live status is best-effort, so it may be absent entirely. When present, an SLO with no
    # samples must not claim to be meeting its target: this used to assert `ok is True` and
    # called it "a trivial rollup", which is the defect — zero samples score a perfect SLI, so
    # the console rendered a green "Meeting" pill for a target nobody had measured.
    st = specs[("MACK", "latency")]["status"]
    if st is not None:
        assert st["measured"] is False
        assert st["ok"] is None
