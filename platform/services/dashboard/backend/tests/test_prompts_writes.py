"""Prompt registry write router — create version / set label (admin, audited, shared examlops path).

Verifies the B1 prompt-registry edit-parity: create immutable versions (variables auto-declared from
the template) and point/rollback labels, through the shared `examlops.data.prompts` code paths,
admin + `prompt.manage` gated, audited `source=dashboard`.
"""

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


async def test_create_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/prompts/triage/versions",
        json={"template": "Hi {name}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_create_autodeclares_vars_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/prompts/triage/versions",
        json={"template": "Summarize {ticket} for {team}", "label": "dev"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 1
    assert set(body["variables"]) == {"ticket", "team"}
    assert body["label"] == "dev"
    conn = sqlite3.connect(platform_db)
    assert (
        conn.execute("SELECT COUNT(*) FROM prompt_versions WHERE name='triage'").fetchone()[0] == 1
    )
    assert (
        conn.execute(
            "SELECT version FROM prompt_labels WHERE name='triage' AND label='dev'"
        ).fetchone()[0]
        == 1
    )
    # Both create + label audited as source=dashboard.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action IN ('prompt_create','prompt_label')"
        ).fetchone()[0]
        == 2
    )
    conn.close()


async def test_create_requires_template(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/prompts/triage/versions", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 400


async def test_second_version_increments_and_label_rollback(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    v1 = await client.post("/api/prompts/triage/versions", json={"template": "v1 {x}"}, headers=h)
    assert v1.json()["version"] == 1
    v2 = await client.post(
        "/api/prompts/triage/versions", json={"template": "v2 {x}", "label": "prod"}, headers=h
    )
    assert v2.json()["version"] == 2
    # Roll 'prod' back to v1.
    r = await client.post(
        "/api/prompts/triage/label", json={"label": "prod", "version": 1}, headers=h
    )
    assert r.status_code == 200, r.text
    conn = sqlite3.connect(platform_db)
    assert (
        conn.execute(
            "SELECT version FROM prompt_labels WHERE name='triage' AND label='prod'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_label_nonexistent_version_404(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/api/prompts/triage/versions", json={"template": "only v1"}, headers=h)
    r = await client.post(
        "/api/prompts/triage/label", json={"label": "prod", "version": 99}, headers=h
    )
    assert r.status_code == 404


async def test_list_prompts(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/api/prompts/triage/versions", json={"template": "hi {x}", "label": "dev"}, headers=h
    )
    r = await client.get("/api/prompts", headers=h)
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()}
    assert by_name["triage"]["versions"][0]["version"] == 1
    assert by_name["triage"]["labels"][0]["label"] == "dev"
