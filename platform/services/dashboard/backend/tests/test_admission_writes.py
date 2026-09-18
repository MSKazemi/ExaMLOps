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


# ── the total counts items, and nothing else ─────────────────────────────────


async def test_an_empty_queue_does_not_500(client, platform_db):
    """The regression this pins: `stats()` gained `oldest_queued_age_s`, which is `None` when the
    queue is empty, and the router summed **every** value — so the commonest state of the page
    raised `TypeError` outside the fail-open `try`. The endpoint promises never to 500.
    """
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/admission", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 0
    assert body["oldestQueuedAgeSeconds"] is None


async def test_the_total_counts_items_not_seconds(client, platform_db, monkeypatch):
    """A waiting queue must not add its wait to the item count.

    With a real wait the old sum reported `items + seconds`; a queue of one item waiting five
    minutes read as 301. The age is reported in its own field instead.
    """
    import routers.admission as mod

    class _Stub:
        @staticmethod
        def stats():
            return {
                "queued": 2,
                "running": 1,
                "done": 0,
                "rejected": 0,
                "failed": 0,
                "oldest_queued_age_s": 300,
            }

    monkeypatch.setattr(mod, "_examlops_admission", lambda: _Stub)
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/admission", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3, f"the wait leaked into the item count: {body}"
    assert body["oldestQueuedAgeSeconds"] == 300
    assert "oldest_queued_age_s" not in body["stats"], "the state map holds states only"


async def test_a_field_added_to_stats_later_cannot_join_the_total(client, platform_db, monkeypatch):
    """The total is summed by name, so a future field is inert here rather than silently counted."""
    import routers.admission as mod

    class _Stub:
        @staticmethod
        def stats():
            return {
                "queued": 1,
                "running": 0,
                "done": 0,
                "rejected": 0,
                "failed": 0,
                "oldest_queued_age_s": 10,
                "some_future_gauge": 9999,
            }

    monkeypatch.setattr(mod, "_examlops_admission", lambda: _Stub)
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/admission", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["total"] == 1
