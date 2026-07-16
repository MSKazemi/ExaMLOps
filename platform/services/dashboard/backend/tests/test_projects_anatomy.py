"""P8 — the dashboard Project anatomy endpoint surfaces storage/connections/pipelines.

Verifies GET /api/v1/projects/{name} includes the P6 storage, P2 connections (secret-safe),
and P7 pipeline surfaces, and is fail-open when the new tables are absent.
"""

import sqlite3

import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE projects (
            name TEXT PRIMARY KEY, description TEXT, cpu_limit REAL, memory_limit_gb REAL,
            storage_gb REAL, gpu_limit INTEGER, network_name TEXT, status TEXT,
            created_at TEXT, created_by TEXT, updated_at TEXT
        );
        CREATE TABLE project_resources (project TEXT, kind TEXT, ref TEXT, PRIMARY KEY (project, kind, ref));
        CREATE TABLE project_models (project TEXT, model TEXT, assigned_at TEXT, PRIMARY KEY (project, model));
        CREATE TABLE project_storage (
            project TEXT PRIMARY KEY, bucket TEXT, prefix TEXT, connection_ref TEXT,
            quota_gb REAL, used_bytes INTEGER, updated_at TEXT
        );
        CREATE TABLE project_pipelines (
            project TEXT, kind TEXT, ref TEXT, status TEXT, schedule TEXT, last_run_at TEXT,
            updated_at TEXT, PRIMARY KEY (project, kind)
        );
        CREATE TABLE connections (
            name TEXT, project TEXT, kind TEXT, config_json TEXT, secret_ref TEXT,
            created_at TEXT, created_by TEXT, PRIMARY KEY (project, name)
        );
        CREATE TABLE traffic_rules (model TEXT PRIMARY KEY, rules TEXT, updated_at TEXT, updated_by TEXT);
        CREATE TABLE authz_relations (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, relation TEXT, object TEXT, actor TEXT, created_at TEXT);
        CREATE TABLE model_costs (id INTEGER PRIMARY KEY AUTOINCREMENT, model_name TEXT, project TEXT, gpu_hours REAL, cost_usd REAL, recorded_at TEXT);
        CREATE TABLE namespace_models (model TEXT, namespace TEXT, assigned_at TEXT, PRIMARY KEY (model, namespace));
        CREATE TABLE project_budgets (project TEXT PRIMARY KEY, gpu_hours REAL, cost_usd REAL);
        """
    )
    conn.execute(
        "INSERT INTO projects (name, description, cpu_limit, memory_limit_gb, storage_gb, gpu_limit,"
        " network_name, status) VALUES ('demo','d',4,8,100,2,'examlops-demo','ACTIVE')"
    )
    conn.execute("INSERT INTO project_models (project, model) VALUES ('demo','JPCP')")
    conn.execute(
        "INSERT INTO project_storage (project, bucket, prefix, connection_ref, quota_gb, used_bytes)"
        " VALUES ('demo','examlops-projects','demo/',NULL,100,25000000000)"
    )
    conn.execute(
        "INSERT INTO project_pipelines (project, kind, ref, status, schedule) VALUES"
        " ('demo','prefect','examlops-jpcp','healthy','0 2 * * *')"
    )
    conn.execute(
        "INSERT INTO connections (name, project, kind, config_json, secret_ref) VALUES"
        " ('raw','demo','s3','{\"bucket\":\"b\"}','connections/demo/raw')"
    )
    conn.execute(
        "INSERT INTO traffic_rules (model, rules) VALUES ('JPCP', '{\"Production\": 100}')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.asyncio
async def test_anatomy_includes_storage_connections_pipelines(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/projects/demo", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["storage"]["bucket"] == "examlops-projects"
    assert body["storage"]["usedBytes"] == 25000000000
    assert body["pipelines"]["prefect"]["deployments"] == ["examlops-jpcp"]
    assert body["pipelines"]["rayserve"]["models"] == ["JPCP"]
    assert body["pipelines"]["rayserve"]["traffic"]["JPCP"] == {"Production": 100}


@pytest.mark.asyncio
async def test_anatomy_connections_secret_safe(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/projects/demo", headers={"Authorization": f"Bearer {token}"})
    conns = r.json()["connections"]
    assert conns and conns[0]["name"] == "raw" and conns[0]["hasSecret"] is True
    # secret_ref path must not be transported
    assert "connections/demo/raw" not in r.text


@pytest.mark.asyncio
async def test_anatomy_requires_auth(client, platform_db):
    r = await client.get("/api/v1/projects/demo")
    assert r.status_code == 401
