"""Providers console write router — activate / delete (admin, audited, shared examlops path).

Complements ``test_providers_router.py`` with the console-facing edit-parity guarantees the Providers
page relies on: a viewer can list; create / activate / delete require admin (``providers.manage``);
and every mutation is audited ``source=dashboard`` through the shared ``examlops.providers`` code
path (never a parallel implementation).
"""

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW

GOOD = (
    "class MyCost(Provider):\n"
    "    name = 'my-cost'\n"
    "    def compute(self, inputs):\n"
    "        return {'cost_usd': inputs.get('gpu_hours', 0) * 0.85}\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = dbconn.connect(db, row_factory=None)
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, details TEXT
        );"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    monkeypatch.setenv("EXAMLOPS_PROVIDERS_DIR", str(tmp_path / "providers"))
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def _save(client, token, **body):
    return await client.post(
        "/api/v1/providers", json=body, headers={"Authorization": f"Bearer {token}"}
    )


def _count(db, action):
    conn = dbconn.connect(db, row_factory=None)
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action=?", (action,)
    ).fetchone()[0]
    conn.close()
    return n


async def test_list_is_viewer_readable(client, env):
    """A viewer can list a project's providers (read-only)."""
    admin = await _login(client, ADMIN_PW)
    await _save(client, admin, project="research", domain="cost", name="c1", code=GOOD)
    viewer = await _login(client, VIEWER_PW)
    r = await client.get(
        "/api/v1/providers?project=research", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert r.status_code == 200
    assert any(p["name"] == "c1" and p["domain"] == "cost" for p in r.json())


async def test_create_requires_admin(client, env):
    viewer = await _login(client, VIEWER_PW)
    r = await _save(client, viewer, project="research", domain="cost", name="c1", code=GOOD)
    assert r.status_code == 403


async def test_create_admin_ok_and_audited(client, env):
    admin = await _login(client, ADMIN_PW)
    r = await _save(client, admin, project="research", domain="cost", name="c1", code=GOOD)
    assert r.status_code == 201, r.text
    assert r.json()["class"] == "MyCost"
    assert _count(env, "provider_authored") == 1


async def test_activate_requires_admin_and_admin_audits(client, env):
    admin = await _login(client, ADMIN_PW)
    ha = {"Authorization": f"Bearer {admin}"}
    await _save(client, admin, project="research", domain="cost", name="c1", code=GOOD)
    # Viewer is denied.
    viewer = await _login(client, VIEWER_PW)
    denied = await client.post(
        "/api/v1/providers/research/cost/c1/activate",
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert denied.status_code == 403
    # Admin activates through the shared path; audited source=dashboard.
    ok = await client.post("/api/v1/providers/research/cost/c1/activate", headers=ha)
    assert ok.status_code == 200 and ok.json()["active"] is True
    assert _count(env, "provider_activated") == 1


async def test_delete_requires_admin_and_admin_audits(client, env):
    admin = await _login(client, ADMIN_PW)
    ha = {"Authorization": f"Bearer {admin}"}
    await _save(client, admin, project="research", domain="cost", name="c1", code=GOOD)
    # Viewer is denied.
    viewer = await _login(client, VIEWER_PW)
    denied = await client.delete(
        "/api/v1/providers/research/cost/c1", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert denied.status_code == 403
    # Admin deletes; audited source=dashboard.
    ok = await client.delete("/api/v1/providers/research/cost/c1", headers=ha)
    assert ok.status_code == 200 and ok.json()["deleted"] is True
    assert _count(env, "provider_removed") == 1
