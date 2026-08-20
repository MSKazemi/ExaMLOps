"""Providers editor router (ADR 0074) — dashboard surface over authored calculation providers.

Verifies: reads are viewer-gated; every mutation requires admin (project.manage), is AST-sandboxed
(malicious upload rejected before it lands), and is audited.
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
EVIL = "import os\nclass P(Provider):\n    def compute(self, i): return {}\n"


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


async def test_save_requires_admin(client, env):
    token = await _login(client, VIEWER_PW)
    r = await _save(client, token, project="research", domain="cost", name="c", code=GOOD)
    assert r.status_code == 403


async def test_malicious_upload_rejected(client, env):
    token = await _login(client, ADMIN_PW)
    r = await _save(client, token, project="research", domain="cost", name="evil", code=EVIL)
    assert r.status_code == 400
    assert "import" in r.text.lower()
    # Never appears in the listing.
    lst = await client.get(
        "/api/v1/providers?project=research", headers={"Authorization": f"Bearer {token}"}
    )
    assert all(p["name"] != "evil" for p in lst.json())


async def test_validate_endpoint(client, env):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    ok = await client.post("/api/v1/providers/validate", json={"code": GOOD}, headers=h)
    assert ok.json() == {"ok": True, "class": "MyCost"}
    bad = await client.post("/api/v1/providers/validate", json={"code": EVIL}, headers=h)
    assert bad.json()["ok"] is False


async def test_save_list_read_activate_delete(client, env):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await _save(client, token, project="research", domain="cost", name="c1", code=GOOD)
    assert r.status_code == 201, r.text
    assert r.json()["class"] == "MyCost"
    # Viewer can list + read.
    v = await _login(client, VIEWER_PW)
    vh = {"Authorization": f"Bearer {v}"}
    lst = await client.get("/api/v1/providers?project=research", headers=vh)
    row = next(p for p in lst.json() if p["name"] == "c1")
    assert row["domain"] == "cost" and row["ok"] is True and row["active"] is False
    src = await client.get("/api/v1/providers/research/cost/c1", headers=vh)
    assert "MyCost" in src.json()["code"]
    # Activate (admin).
    act = await client.post("/api/v1/providers/research/cost/c1/activate", headers=h)
    assert act.status_code == 200 and act.json()["active"] is True
    lst2 = await client.get("/api/v1/providers?project=research", headers=vh)
    assert next(p for p in lst2.json() if p["name"] == "c1")["active"] is True
    # Delete (admin).
    d = await client.delete("/api/v1/providers/research/cost/c1", headers=h)
    assert d.status_code == 200 and d.json()["deleted"] is True
    # Audited (authored + activated + removed).
    conn = dbconn.connect(env, row_factory=None)
    actions = {
        r[0]
        for r in conn.execute(
            "SELECT action FROM audit_events WHERE target='research/cost/c1'"
        ).fetchall()
    }
    conn.close()
    assert {"provider_authored", "provider_activated", "provider_removed"} <= actions


async def test_delete_unknown_404(client, env):
    token = await _login(client, ADMIN_PW)
    r = await client.delete(
        "/api/v1/providers/research/cost/ghost", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 404
