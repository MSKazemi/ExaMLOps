"""Autopilot kill-switch write router — enable / disable / status (admin, audited, shared path).

Verifies the ADR-0085 self-driving-autopilot kill-switch edit-parity: flip the persistent
`autopilot_config.enabled` value through the shared `examlops.data.autopilot.set_autopilot_config`
code path, admin + `autopilot.manage` gated, audited `source=dashboard`.
"""

import sqlite3

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    monkeypatch.delenv("EXAMLOPS_AUTOPILOT_ENABLED", raising=False)
    from examlops import data as pdb

    pdb.init_db(force=True)
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def test_enable_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post("/api/autopilot/enable", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_status_defaults_disabled(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/autopilot/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["envOverride"] is None
    assert body["recentRuns"] == []


async def test_enable_then_disable_persist_and_audit(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    en = await client.post("/api/autopilot/enable", headers=h)
    assert en.status_code == 200, en.text
    assert en.json()["enabled"] is True
    conn = sqlite3.connect(platform_db)
    assert (
        conn.execute("SELECT value FROM autopilot_config WHERE key='enabled'").fetchone()[0] == "1"
    )
    conn.close()
    # Status now reflects enabled.
    st = await client.get("/api/autopilot/status", headers=h)
    assert st.json()["enabled"] is True

    dis = await client.post("/api/autopilot/disable", headers=h)
    assert dis.json()["enabled"] is False
    conn = sqlite3.connect(platform_db)
    assert (
        conn.execute("SELECT value FROM autopilot_config WHERE key='enabled'").fetchone()[0] == "0"
    )
    # Both mutations audited as source=dashboard.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' "
            "AND action IN ('autopilot_enabled','autopilot_disabled')"
        ).fetchone()[0]
        == 2
    )
    conn.close()


async def test_env_override_surfaced_in_status(client, platform_db, monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUTOPILOT_ENABLED", "1")
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/autopilot/status", headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    # DB still disabled, but the env override makes the effective state enabled.
    assert body["enabled"] is False
    assert body["envOverride"] is True
    assert body["effective"] is True
