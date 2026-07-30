"""Gateway virtual-key write router — issue / revoke (admin, audited, shared examlops path).

Verifies the B2 gateway edit-parity: issue a virtual key (raw returned ONCE, only the hash stored)
and revoke it, through the shared `examlops.gateway` / `examlops.data.governance` code paths,
admin + `gateway.manage` gated, audited `source=dashboard`.
"""

import hashlib
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


async def test_issue_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/gateway/keys",
        json={"project": "research"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_issue_returns_raw_once_and_stores_only_hash(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/gateway/keys",
        json={"tenant": "default", "project": "research", "models": ["JPCP"], "budgetUsd": 25.0},
        headers=h,
    )
    assert r.status_code == 201, r.text
    raw = r.json()["key"]
    assert raw.startswith("exa-")
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    conn = sqlite3.connect(platform_db)
    row = conn.execute(
        "SELECT project, budget_usd, revoked FROM virtual_keys WHERE key_hash=?", (key_hash,)
    ).fetchone()
    assert row == ("research", 25.0, 0)
    # The raw key is NEVER stored — only its hash.
    assert (
        conn.execute("SELECT COUNT(*) FROM virtual_keys WHERE key_hash=?", (raw,)).fetchone()[0]
        == 0
    )
    # Audited as source=dashboard through the shared path.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='virtual_key_issued'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_list_shows_hash_not_raw(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    issued = await client.post("/api/gateway/keys", json={"project": "p"}, headers=h)
    raw = issued.json()["key"]
    r = await client.get("/api/gateway/keys", headers=h)
    assert r.status_code == 200
    keys = r.json()
    assert len(keys) == 1
    assert "key" not in keys[0] and "key_hash" in keys[0]
    assert keys[0]["key_hash"] != raw  # a hash, not the raw key
    assert keys[0]["revoked"] is False


async def test_revoke_flips_revoked_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    issued = await client.post("/api/gateway/keys", json={"project": "p"}, headers=h)
    key_hash = hashlib.sha256(issued.json()["key"].encode()).hexdigest()
    r = await client.post(f"/api/gateway/keys/{key_hash}/revoke", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["revoked"] is True
    conn = sqlite3.connect(platform_db)
    assert (
        conn.execute("SELECT revoked FROM virtual_keys WHERE key_hash=?", (key_hash,)).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='virtual_key_revoked'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_revoke_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/gateway/keys/deadbeef/revoke", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403
