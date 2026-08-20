"""Admission write router — enqueue work / read queue depth (viewer read, admin write, shared path).

Verifies the admission-console edit-parity: enqueue a work item through the shared
`examlops.admission.submit` code path (mirrors `exa admission submit`), admin + `admission.manage`
gated, audited `source=dashboard`; reads surface queue depth by state via `examlops.admission.stats`
(mirrors `exa admission stats`, pure platform.db).
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


async def test_submit_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/admission",
        json={"kind": "retrain"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_submit_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/admission",
        json={
            "kind": "retrain",
            "payload": {"model": "JPCP"},
            "tenant": "acme",
            "priority": 5,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    item_id = r.json()["id"]
    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT tenant, kind, priority, state FROM admission_queue WHERE id=?", (item_id,)
    ).fetchone()
    assert row == ("acme", "retrain", 5, "queued")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE source='dashboard' AND action='admission_submit'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_submit_accepts_json_string_payload(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/admission",
        json={"kind": "pipeline", "payload": '{"dataset": "PM100"}'},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = dbconn.connect(platform_db, row_factory=None)
    payload = conn.execute(
        "SELECT payload FROM admission_queue WHERE id=?", (r.json()["id"],)
    ).fetchone()[0]
    assert '"dataset"' in payload
    conn.close()


async def test_submit_rejects_missing_kind_and_bad_json(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/v1/admission", json={"kind": "  "}, headers=h)
    assert r.status_code == 400
    r2 = await client.post(
        "/api/v1/admission", json={"kind": "retrain", "payload": "{bad"}, headers=h
    )
    assert r2.status_code == 400


async def test_stats_returns_queue_depth(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/admission", json={"kind": "retrain", "tenant": "t1"}, headers=h)
    await client.post("/api/v1/admission", json={"kind": "pipeline", "tenant": "t2"}, headers=h)
    r = await client.get("/api/v1/admission", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["queued"] == 2
    assert body["total"] == 2


async def test_stats_readable_by_viewer(client, platform_db):
    token = await _login(client, VIEWER_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.get("/api/v1/admission", headers=h)
    assert r.status_code == 200
    assert r.json()["stats"]["queued"] == 0
