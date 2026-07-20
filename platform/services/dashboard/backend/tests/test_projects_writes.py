"""Projects (ADR 0086) write-completion routers: delete project, remove member, bind storage.

Verifies the config-write parity added to the dashboard: every mutation requires admin
(project.manage) and is audited, and the storage bind goes through the shared examlops helpers.
"""

import sqlite3

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    """Empty platform.db; the projects router self-provisions its tables on first write."""
    db = tmp_path / "platform.db"
    sqlite3.connect(db).close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def _create_project(client, token, name="research"):
    return await client.post(
        "/api/v1/projects",
        json={"name": name, "cpuLimit": 4, "memoryLimitGb": 8, "storageGb": 50, "gpuLimit": 0},
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_delete_project_requires_admin(client, platform_db):
    admin = await _login(client, ADMIN_PW)
    await _create_project(client, admin)
    viewer = await _login(client, VIEWER_PW)
    r = await client.delete(
        "/api/v1/projects/research", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert r.status_code == 403


async def test_delete_project(client, platform_db):
    token = await _login(client, ADMIN_PW)
    await _create_project(client, token)
    r = await client.delete(
        "/api/v1/projects/research", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    # Gone from the list.
    lr = await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
    assert all(p["name"] != "research" for p in lr.json())
    # Audited.
    conn = sqlite3.connect(platform_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE action='project_deleted' AND target='research'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


async def test_delete_project_unknown_404(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.delete("/api/v1/projects/ghost", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


async def test_add_then_remove_member(client, platform_db):
    token = await _login(client, ADMIN_PW)
    await _create_project(client, token)
    add = await client.post(
        "/api/v1/projects/research/members",
        json={"subject": "alice", "role": "editor"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert add.status_code == 200, add.text
    rm = await client.delete(
        "/api/v1/projects/research/members/alice",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rm.status_code == 200, rm.text
    assert rm.json()["removed"] == 1
    # Second removal is a 404 (no longer a member).
    rm2 = await client.delete(
        "/api/v1/projects/research/members/alice",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert rm2.status_code == 404


async def test_remove_member_requires_admin(client, platform_db):
    admin = await _login(client, ADMIN_PW)
    await _create_project(client, admin)
    viewer = await _login(client, VIEWER_PW)
    r = await client.delete(
        "/api/v1/projects/research/members/alice",
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert r.status_code == 403


async def test_bind_storage(client, platform_db):
    token = await _login(client, ADMIN_PW)
    await _create_project(client, token)
    r = await client.post(
        "/api/v1/projects/research/storage",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"] == "research"
    assert body["bucket"]  # a bucket was provisioned
    # Audited.
    conn = sqlite3.connect(platform_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE action='project_storage_bound'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


async def test_bind_storage_requires_admin(client, platform_db):
    admin = await _login(client, ADMIN_PW)
    await _create_project(client, admin)
    viewer = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/projects/research/storage",
        json={},
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert r.status_code == 403


async def test_update_project_quota_requires_admin(client, platform_db):
    admin = await _login(client, ADMIN_PW)
    await _create_project(client, admin)
    viewer = await _login(client, VIEWER_PW)
    r = await client.put(
        "/api/v1/projects/research",
        json={"cpuLimit": 16},
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert r.status_code == 403


async def test_update_project_quota_budget_namespace(client, platform_db):
    token = await _login(client, ADMIN_PW)
    await _create_project(client, token)
    r = await client.put(
        "/api/v1/projects/research",
        json={
            "cpuLimit": 16,
            "memoryLimitGb": 64,
            "storageGb": 200,
            "gpuLimit": 2,
            "description": "edited",
            "networkName": "examlops-research-ns",
            "gpuHoursBudget": 100,
            "costBudget": 250,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["quotaUpdated"] is True
    assert body["budgetUpdated"] is True
    # Reflected in the anatomy view (viewer).
    v = await _login(client, VIEWER_PW)
    a = await client.get("/api/v1/projects/research", headers={"Authorization": f"Bearer {v}"})
    anat = a.json()
    assert anat["quota"]["cpuLimit"] == 16
    assert anat["quota"]["memoryLimitGb"] == 64
    assert anat["quota"]["gpuLimit"] == 2
    assert anat["namespace"] == "examlops-research-ns"
    assert anat["description"] == "edited"
    assert anat["budget"]["gpuHours"] == 100
    assert anat["budget"]["costUsd"] == 250
    # Audited.
    conn = sqlite3.connect(platform_db)
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE action='project_updated' AND target='research'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


async def test_update_project_unknown_404(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.put(
        "/api/v1/projects/ghost",
        json={"cpuLimit": 8},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
