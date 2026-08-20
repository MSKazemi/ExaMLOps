"""Collaboration store + endpoints (F22 / ADR 0073)."""

import collab
import dbconn
import pytest

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(path))
    pdb.init_db()
    conn = dbconn.connect(path, row_factory=None)
    conn.commit()
    conn.close()
    return str(path)


# ── pure helpers (R1/R6) ──────────────────────────────────────────────────────


def test_extract_mentions_unique_ordered():
    assert collab.extract_mentions("hi @alice and @bob and @alice") == ["alice", "bob"]


def test_sanitize_strips_html_and_js():
    out = collab.sanitize_comment('<script>x()</script>see <a href="javascript:evil()">link</a>')
    assert "<" not in out and "javascript:" not in out
    assert "see" in out


# ── comments (R1/R5) ──────────────────────────────────────────────────────────


def test_add_comment_sanitizes_extracts_mentions_and_audits(db):
    c = collab.add_comment(db, "models", "jpcp", "default", "alice", "look @bob <b>bold</b>")
    assert c["mentions"] == ["bob"]
    assert "<b>" not in c["body"]
    conn = dbconn.connect(db, row_factory=None)
    row = conn.execute("SELECT source, action, target FROM audit_events").fetchone()
    conn.close()
    assert row == ("dashboard-collab", "comment_added", "models/jpcp")


def test_list_comments_is_tenant_scoped(db):
    collab.add_comment(db, "models", "jpcp", "tenantA", "a", "hi from A")
    collab.add_comment(db, "models", "jpcp", "tenantB", "b", "hi from B")
    a = collab.list_comments(db, "models", "jpcp", "tenantA")
    assert len(a) == 1 and a[0]["body"] == "hi from A"


def test_entity_activity_merges_comments_and_audit(db):
    collab.add_comment(db, "models", "jpcp", "default", "alice", "note")
    acts = collab.entity_activity(db, "models", "jpcp", "default")
    kinds = {a["kind"] for a in acts}
    assert "comment" in kinds and "audit" in kinds  # the comment + its audit event


# ── snapshots (R2/GWT-3) ──────────────────────────────────────────────────────


def test_snapshot_round_trip(db):
    snap = collab.create_snapshot(db, "default", {"page": "/models/jpcp", "range": "24h"}, "alice")
    got = collab.get_snapshot(db, snap["token"])
    assert got is not None
    assert got["view"]["page"] == "/models/jpcp"
    assert got["read_only"] is True


def test_expired_snapshot_returns_none(db):
    snap = collab.create_snapshot(db, "default", {"x": 1}, "alice", ttl_hours=-1)
    assert collab.get_snapshot(db, snap["token"]) is None


def test_unknown_snapshot_returns_none(db):
    assert collab.get_snapshot(db, "nope") is None


# ── endpoints ─────────────────────────────────────────────────────────────────


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.asyncio
async def test_comments_endpoint_requires_auth(client, db):
    r = await client.get("/api/v1/collab/models/jpcp/comments")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_add_then_list_comment_via_api(client, db):
    token = await _login(client, VIEWER_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/collab/models/jpcp/comments", json={"body": "ping @carol"}, headers=h
    )
    assert r.status_code == 200
    assert r.json()["mentions"] == ["carol"]
    r2 = await client.get("/api/v1/collab/models/jpcp/comments", headers=h)
    assert len(r2.json()["comments"]) == 1


@pytest.mark.asyncio
async def test_snapshot_create_and_resolve_via_api(client, db):
    token = await _login(client, VIEWER_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/v1/collab/snapshot", json={"view": {"page": "/finops"}}, headers=h)
    snap_token = r.json()["token"]
    r2 = await client.get(f"/api/v1/collab/snapshot/{snap_token}", headers=h)
    body = r2.json()
    assert body["found"] is True and body["view"]["page"] == "/finops"
    # unknown token → found:false, never a 500
    r3 = await client.get("/api/v1/collab/snapshot/bogus", headers=h)
    assert r3.json()["found"] is False
