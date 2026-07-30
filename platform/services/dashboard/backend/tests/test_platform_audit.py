"""Platform-audit router: reads audit_events from shared platform.db.

Regression coverage for the P0 "Audit page shows 0 while Governance shows N" bug — the
endpoint previously hardcoded a 30-day window (hiding older events) and swallowed any error
into an empty result. It now defaults to the full history and surfaces errors as HTTP 500.
"""

import sqlite3

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


def _seed_audit_db(path: str) -> None:
    """Create an audit_events table with one old (>30d) and one recent event."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE audit_events ("
        "id INTEGER PRIMARY KEY, ts DATETIME, source TEXT, actor TEXT, "
        "action TEXT, target TEXT, details TEXT)"
    )
    conn.execute(
        "INSERT INTO audit_events (ts, source, actor, action, target, details) "
        "VALUES (datetime('now','-400 days'), 'cli', 'alice', 'retrain', 'JPCP', NULL)"
    )
    conn.execute(
        "INSERT INTO audit_events (ts, source, actor, action, target, details) "
        "VALUES (datetime('now','-1 days'), 'dashboard', 'bob', 'promote', 'MACK', "
        "'{\"note\": \"recent\"}')"
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_platform_audit_requires_admin(client, monkeypatch, tmp_path):
    db = tmp_path / "platform.db"
    _seed_audit_db(str(db))
    monkeypatch.setenv("PLATFORM_DB", str(db))
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/platform-audit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_platform_audit_defaults_to_full_history(client, monkeypatch, tmp_path):
    """The regression: an event older than 30 days must still surface (all-time default)."""
    db = tmp_path / "platform.db"
    _seed_audit_db(str(db))
    monkeypatch.setenv("PLATFORM_DB", str(db))
    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/platform-audit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2  # both the 400-day-old and the recent event
    actions = {row["action"] for row in body["items"]}
    assert actions == {"retrain", "promote"}


@pytest.mark.asyncio
async def test_platform_audit_last_days_narrows(client, monkeypatch, tmp_path):
    db = tmp_path / "platform.db"
    _seed_audit_db(str(db))
    monkeypatch.setenv("PLATFORM_DB", str(db))
    token = await _login(client, ADMIN_PW)
    r = await client.get(
        "/api/platform-audit?last_days=30", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "promote"
    # details JSON is decoded, not returned as a raw string
    assert body["items"][0]["details"] == {"note": "recent"}


@pytest.mark.asyncio
async def test_platform_audit_surfaces_errors_not_empty(client, monkeypatch, tmp_path):
    """A missing/unreadable DB must 500, never masquerade as 'no audit activity' (0 total)."""
    db = tmp_path / "missing.db"  # no such file, and no audit_events table
    _ = db
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "missing.db"))
    token = await _login(client, ADMIN_PW)
    r = await client.get("/api/platform-audit", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 500
