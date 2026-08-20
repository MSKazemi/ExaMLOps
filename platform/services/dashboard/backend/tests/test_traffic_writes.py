"""Traffic-split write router (admin, audited) — `exa serve traffic` parity.

Verifies the editability added to the serving traffic split: weights must sum to 100, only admin
can write, and the change is audited + goes through examlops.data.serving.set_traffic_rules.
"""

import json

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = dbconn.connect(db, row_factory=None)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS traffic_rules (
            model TEXT PRIMARY KEY, rules TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, details TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def test_traffic_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.put(
        "/api/platform-data/traffic-rules/JPCP",
        json={"rules": {"Production": 100}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_traffic_must_sum_to_100(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.put(
        "/api/platform-data/traffic-rules/JPCP",
        json={"rules": {"Production": 90, "Canary": 5}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert "sum to 100" in r.text


async def test_traffic_set_and_read_back(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.put(
        "/api/platform-data/traffic-rules/JPCP",
        json={"rules": {"Production": 90, "Canary": 10, "Staging": 0}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    # Zero-weight alias dropped, sum still 100.
    assert r.json()["rules"] == {"Production": 90, "Canary": 10}
    # Reflected in the viewer read endpoint.
    v = await _login(client, VIEWER_PW)
    got = await client.get(
        "/api/platform-data/traffic-rules/JPCP", headers={"Authorization": f"Bearer {v}"}
    )
    assert got.json()["rules"] == {"Production": 90, "Canary": 10}
    # Audited + persisted through the shared code path.
    conn = dbconn.connect(platform_db, row_factory=None)
    stored = json.loads(
        conn.execute("SELECT rules FROM traffic_rules WHERE model='JPCP'").fetchone()[0]
    )
    assert stored == {"Production": 90, "Canary": 10}
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='traffic_rules_set' AND target='JPCP'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_traffic_rejects_non_integer(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.put(
        "/api/platform-data/traffic-rules/JPCP",
        json={"rules": {"Production": "lots"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
