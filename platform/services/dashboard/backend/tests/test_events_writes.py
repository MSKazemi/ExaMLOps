"""Events write router — publish to the outbox / read backlog (viewer read, admin write, shared path).

Verifies the events-console edit-parity: enqueue an event through the shared `examlops.events.publish`
code path (mirrors `exa events publish`), admin + `events.manage` gated, audited `source=dashboard`;
reads surface outbox backlog by state via `examlops.data.events.outbox_stats` (mirrors
`exa events stats`, pure platform.db).
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


async def test_publish_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/events",
        json={"topic": "drift.detected"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_publish_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/events",
        json={"topic": "drift.detected", "payload": {"model": "JPCP"}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    event_id = r.json()["id"]
    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT topic, payload, published_at FROM event_outbox WHERE id=?", (event_id,)
    ).fetchone()
    assert row[0] == "drift.detected"
    assert '"model"' in row[1]
    assert row[2] is None  # enqueued, not yet relayed
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE source='dashboard' AND action='event_published'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_publish_accepts_json_string_payload(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/events",
        json={"topic": "retrain.requested", "payload": '{"dataset": "PM100"}'},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = dbconn.connect(platform_db, row_factory=None)
    payload = conn.execute(
        "SELECT payload FROM event_outbox WHERE id=?", (r.json()["id"],)
    ).fetchone()[0]
    assert '"dataset"' in payload
    conn.close()


async def test_publish_rejects_missing_topic_and_bad_json(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/v1/events", json={"topic": "  "}, headers=h)
    assert r.status_code == 400
    r2 = await client.post(
        "/api/v1/events", json={"topic": "drift.detected", "payload": "{bad"}, headers=h
    )
    assert r2.status_code == 400


async def test_stats_returns_backlog(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/events", json={"topic": "a.one"}, headers=h)
    await client.post("/api/v1/events", json={"topic": "b.two"}, headers=h)
    r = await client.get("/api/v1/events", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["stats"]["pending"] == 2
    assert body["total"] == 2


async def test_stats_readable_by_viewer(client, platform_db):
    token = await _login(client, VIEWER_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.get("/api/v1/events", headers=h)
    assert r.status_code == 200
    assert r.json()["stats"]["pending"] == 0
