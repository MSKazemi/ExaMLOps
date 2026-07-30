"""Platform Ops console router — reads (viewer) + governed writes (admin, source=dashboard).

Guards the edit-parity guarantee: the dashboard's platform-management writes reuse the *same*
``examlops.platform_admin`` façade the workbench/CLI use (so the UI can't drift), every write is
attributed to the logged-in principal + audited ``source=dashboard``, and viewers are denied writes.
"""

import sqlite3

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW

GOOD = (
    "class MyCost(Provider):\n"
    "    name = 'nb-cost'\n"
    "    def compute(self, inputs):\n"
    "        return {'cost_usd': inputs.get('gpu_hours', 0) * 0.85}\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    monkeypatch.setenv("EXAMLOPS_PROVIDERS_DIR", str(tmp_path / "providers"))
    monkeypatch.setenv("EXAMLOPS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("EXAMLOPS_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.delenv("EXAMLOPS_MULTITENANCY", raising=False)
    # loader/policy cache the config-dir constant at import; point them at the isolated tmp dir.
    import examlops.policy as policy
    import examlops.providers.loader as loader

    monkeypatch.setattr(loader, "FINOPS_YAML", tmp_path / "cfg" / "finops.yaml")
    monkeypatch.setattr(policy, "POLICY_YAML", tmp_path / "cfg" / "policy.yaml")
    from examlops.platform_db import init_db

    init_db()  # build the real schema on the tmp DB
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


def _audit(db, action):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT actor, source FROM audit_events WHERE action=?", (action,)
    ).fetchall()
    conn.close()
    return rows


async def test_overview_is_viewer_readable(client, env):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/platform-ops/overview", headers=_hdr(token))
    assert r.status_code == 200
    body = r.json()
    assert {"cost_card", "providers", "changes"} <= body.keys()
    assert "cost_per_gpu_hour" in body["cost_card"]


async def test_viewer_cannot_set_cost(client, env):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/platform-ops/cost", json={"gpu_per_hour": 9.0}, headers=_hdr(token)
    )
    assert r.status_code == 403


async def test_admin_set_cost_is_attributed_and_audited(client, env):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/platform-ops/cost", json={"gpu_per_hour": 3.5}, headers=_hdr(token)
    )
    assert r.status_code == 200, r.text

    # rate card reflects the change
    r2 = await client.get("/api/v1/platform-ops/cost-card", headers=_hdr(token))
    assert r2.json()["gpu_rate"] == 3.5

    rows = _audit(env, "platform_admin:set_compute_cost")
    assert len(rows) == 1
    actor, source = rows[0]
    assert source == "dashboard" and actor  # attributed to the logged-in principal, not container


async def test_admin_deploy_provider_then_visible(client, env):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/platform-ops/provider",
        json={"domain": "cost", "name": "nb-cost", "code": GOOD},
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    r2 = await client.get("/api/v1/platform-ops/providers", headers=_hdr(token))
    assert any(p["name"] == "nb-cost" for p in r2.json())
    assert _audit(env, "platform_admin:deploy_provider:cost")


async def test_deploy_provider_bad_code_is_400_not_500(client, env):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/platform-ops/provider",
        json={
            "domain": "cost",
            "name": "evil",
            "code": "import os\nclass X(Provider):\n    name='x'\n",
        },
        headers=_hdr(token),
    )
    assert r.status_code == 400


async def test_changes_feed_reflects_writes(client, env):
    token = await _login(client, ADMIN_PW)
    await client.post("/api/v1/platform-ops/cost", json={"gpu_per_hour": 2.0}, headers=_hdr(token))
    r = await client.get("/api/v1/platform-ops/changes", headers=_hdr(token))
    assert r.status_code == 200
    assert any(c["action"] == "platform_admin:set_compute_cost" for c in r.json())
