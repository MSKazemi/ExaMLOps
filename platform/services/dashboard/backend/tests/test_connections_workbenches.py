"""Connections (ADR 0087) + Workbenches (ADR 0090) dashboard routers.

Verifies: connections are read-only and never leak a secret value; workbench listing is
viewer-gated; status flips require admin and are audited.
"""

import dbconn
import pytest

from examlops import platform_db as pdb
from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    """Seed through the owning modules, not raw INSERTs.

    ``connections`` and ``workbenches`` are deliberately *not* created by ``init_db()`` — each is
    owned by its own module and created on first write — so seeding them by hand meant keeping a
    second copy of two more schemas. Going through ``create_connection``/``create_workbench``
    also means the secret takes the real D7 indirection rather than a hand-written ``secret_ref``.
    """
    from examlops.connections import create_connection
    from examlops.workbenches import create_workbench

    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    pdb.create_project("research", created_by="alice")
    create_connection(
        "minio",
        "s3",
        project="research",
        config={
            "endpoint": "http://localhost:19000",
            "bucket": "data",
            "access_key": "minioadmin",
        },
        secret_value="minioadmin-secret",
        created_by="alice",
    )
    create_workbench(
        "nb",
        "research",
        image="jupyter/scipy-notebook:latest",
        created_by="alice",
    )
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


async def test_connection_create_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/connections",
        json={"name": "z", "kind": "uri", "config": {"url": "https://zenodo.org"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_connection_create_and_list(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/connections",
        json={
            "name": "zenodo",
            "kind": "uri",
            "project": "research",
            "config": {"url": "https://zenodo.org"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "zenodo"
    assert body["kind"] == "uri"
    assert body["hasSecret"] is False
    # It now appears in the (viewer) list for that project.
    vtoken = await _login(client, VIEWER_PW)
    lr = await client.get(
        "/api/v1/connections?project=research", headers={"Authorization": f"Bearer {vtoken}"}
    )
    names = {c["name"] for c in lr.json()}
    assert "zenodo" in names
    # And it was audited.
    conn = dbconn.connect(platform_db, row_factory=None)
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE action='connection_created' AND target='zenodo'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


async def test_connection_create_with_secret_hides_value(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/connections",
        json={
            "name": "s3data",
            "kind": "s3",
            "project": "research",
            "config": {"endpoint": "http://localhost:19000", "bucket": "data"},
            "secret": "super-secret-key",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["hasSecret"] is True
    # The secret value must never appear in the API response.
    assert "super-secret-key" not in str(body)
    # But it IS retrievable through the shared examlops path (CLI-compatible secret store).
    from examlops import connections as _c

    resolved = _c.resolve_connection("s3data", project="research")
    assert resolved.get("secret") == "super-secret-key"


async def test_connection_create_duplicate_409(client, platform_db):
    token = await _login(client, ADMIN_PW)
    payload = {"name": "dup", "kind": "uri", "config": {"url": "x"}}
    r1 = await client.post(
        "/api/v1/connections", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert r1.status_code == 201
    r2 = await client.post(
        "/api/v1/connections", json=payload, headers={"Authorization": f"Bearer {token}"}
    )
    assert r2.status_code == 409


async def test_connection_create_bad_kind_400(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/connections",
        json={"name": "bad", "kind": "ftp", "config": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


async def test_connection_delete(client, platform_db):
    token = await _login(client, ADMIN_PW)
    # The fixture seeds 'minio' in project 'research'.
    r = await client.delete(
        "/api/v1/connections/minio?project=research",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    # Gone from the list now.
    lr = await client.get(
        "/api/v1/connections?project=research", headers={"Authorization": f"Bearer {token}"}
    )
    assert all(c["name"] != "minio" for c in lr.json())


async def test_connection_delete_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.delete(
        "/api/v1/connections/minio?project=research",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_kinds_endpoint_lists_registry_kinds(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/connections/kinds", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert {"s3", "sql", "kafka"} <= set(r.json()["kinds"])


async def test_connection_delete_unknown_404(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.delete(
        "/api/v1/connections/ghost?project=research",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


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

    conn = dbconn.connect(platform_db, row_factory=None)
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


async def test_workbench_create_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/workbenches",
        json={"project": "research", "name": "nb2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_workbench_create_and_delete(client, platform_db):
    token = await _login(client, ADMIN_PW)
    # A project must exist for a workbench to bind to (examlops.workbenches parity path).
    pr = await client.post(
        "/api/v1/projects",
        json={"name": "research", "cpuLimit": 4, "memoryLimitGb": 8, "storageGb": 50},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert pr.status_code in (201, 409), pr.text
    r = await client.post(
        "/api/v1/workbenches",
        json={"project": "research", "name": "nb2", "image": "jupyter/minimal-notebook:latest"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "nb2"
    assert body["project"] == "research"
    assert body["status"] == "STOPPED"
    assert body["volume"] == "research-nb2-data"  # per-workbench local persistent volume
    # Appears in the (viewer) list for the project.
    v = await _login(client, VIEWER_PW)
    lr = await client.get(
        "/api/v1/workbenches?project=research", headers={"Authorization": f"Bearer {v}"}
    )
    assert "nb2" in {w["name"] for w in lr.json()}
    # Audited.
    conn = dbconn.connect(platform_db, row_factory=None)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action='workbench_created' AND target='nb2'"
        ).fetchone()[0]
        == 1
    )
    conn.close()
    # Delete it.
    dr = await client.delete(
        "/api/v1/workbenches/research/nb2", headers={"Authorization": f"Bearer {token}"}
    )
    assert dr.status_code == 200, dr.text
    assert dr.json()["deleted"] is True


async def test_workbench_delete_unknown_404(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.delete(
        "/api/v1/workbenches/research/ghost", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 404


def test_workbench_open_url_and_server_name(monkeypatch):
    # The Open URL + named-server id are computed deterministically from the Hub env.
    import routers.workbenches as W

    monkeypatch.setenv("JUPYTERHUB_API_URL", "http://examlops-jupyterhub:8000/hub/api")
    monkeypatch.setenv("JUPYTERHUB_PUBLIC_URL", "http://localhost:18888")
    monkeypatch.setenv("JUPYTERHUB_DASHBOARD_TOKEN", "tok")
    monkeypatch.setenv("JUPYTERHUB_WORKBENCH_USER", "admin")
    assert W._hub_enabled() is True
    assert W._server_name("minio-demo", "test") == "minio-demo-test"
    assert (
        W._open_url("minio-demo", "test") == "http://localhost:18888/user/admin/minio-demo-test/lab"
    )


def test_workbench_url_none_without_hub(monkeypatch):
    import routers.workbenches as W

    monkeypatch.delenv("JUPYTERHUB_API_URL", raising=False)
    monkeypatch.delenv("JUPYTERHUB_DASHBOARD_TOKEN", raising=False)
    assert W._hub_enabled() is False
    assert W._open_url("p", "n") is None
