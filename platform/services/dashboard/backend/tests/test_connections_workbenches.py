"""Connections (ADR 0087) + Workbenches (ADR 0090) dashboard routers.

Verifies: connections are read-only and never leak a secret value; workbench listing is
viewer-gated; status flips require admin and are audited.
"""

import sqlite3

import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE connections (
            name TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}', secret_ref TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, created_by TEXT,
            PRIMARY KEY (project, name)
        );
        CREATE TABLE workbenches (
            name TEXT NOT NULL, project TEXT NOT NULL,
            image TEXT NOT NULL DEFAULT 'jupyter/scipy-notebook:latest',
            cpu REAL, memory_gb REAL, storage_volume TEXT,
            status TEXT NOT NULL DEFAULT 'STOPPED',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, created_by TEXT,
            PRIMARY KEY (project, name)
        );
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            source TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, target TEXT, details TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO connections (name, project, kind, config_json, secret_ref, created_by) "
        "VALUES ('minio', 'research', 's3', ?, 'conn/research/minio', 'alice')",
        ('{"endpoint":"http://localhost:19000","bucket":"data","access_key":"minioadmin"}',),
    )
    conn.execute(
        "INSERT INTO workbenches (name, project, image, status, storage_volume, created_by) "
        "VALUES ('nb', 'research', 'jupyter/scipy-notebook:latest', 'STOPPED', 'research-nb-data', 'alice')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def test_connections_list_hides_secret(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get(
        "/api/v1/connections?project=research", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    conn = body[0]
    assert conn["name"] == "minio"
    assert conn["kind"] == "s3"
    assert conn["hasSecret"] is True
    # The secret ref is a pointer, never the credential; and no secret value is present anywhere.
    blob = str(conn).lower()
    assert "secret_ref" not in conn  # raw column name never surfaces
    assert conn["config"]["access_key"] == "minioadmin"  # non-secret config passes through
    # The real credential value never appears (only the access key id, which is not secret).
    assert "password" not in blob


async def test_workbench_list_viewer(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get(
        "/api/v1/workbenches?project=research", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body[0]["name"] == "nb"
    assert body[0]["status"] == "STOPPED"


async def test_workbench_status_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/workbenches/research/nb/status",
        json={"status": "RUNNING"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_workbench_status_flip_audited(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/workbenches/research/nb/status",
        json={"status": "RUNNING"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "RUNNING"

    conn = sqlite3.connect(platform_db)
    row = conn.execute(
        "SELECT status FROM workbenches WHERE project='research' AND name='nb'"
    ).fetchone()
    assert row[0] == "RUNNING"
    audited = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE action='workbench_status_changed'"
    ).fetchone()[0]
    conn.close()
    assert audited == 1


async def test_workbench_status_unknown_404(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/workbenches/research/ghost/status",
        json={"status": "RUNNING"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404
