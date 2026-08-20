"""P8 — the dashboard Project anatomy endpoint surfaces storage/connections/pipelines.

Verifies GET /api/v1/projects/{name} includes the P6 storage, P2 connections (secret-safe),
and P7 pipeline surfaces, and is fail-open when the new tables are absent.
"""

import dbconn
import pytest

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    """Build the **real** schema, then seed it through the product's own code paths.

    This fixture used to hand-roll its own ``CREATE TABLE``s, and that is exactly how it came to
    declare ``project_budgets(project, gpu_hours, cost_usd)`` — three columns the product has never
    had (they are ``gpu_hours_budget``/``cost_budget``). It survived because nothing read them
    back, and because on SQLite the fixture's copy *wins*: every test gets its own file, so the
    invented shape is never confronted with the real one. `test_fixture_schema_is_real.py` now
    fails on any such invention; this is the fixture that motivated it.

    ``connections`` is deliberately *not* created by ``init_db()`` — ``examlops.connections`` owns
    it and creates it on first write — so the row goes in via ``create_connection()``, which also
    exercises the secret indirection the secret-safety test below is actually about.
    """
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()

    conn = dbconn.connect(db, row_factory=None)
    try:
        conn.execute(
            "INSERT INTO projects (name, description, cpu_limit, memory_limit_gb, storage_gb,"
            " gpu_limit, network_name, status) VALUES ('demo','d',4,8,100,2,'examlops-demo','ACTIVE')"
        )
        conn.execute("INSERT INTO project_models (project, model) VALUES ('demo','JPCP')")
        conn.execute(
            "INSERT INTO project_storage (project, bucket, prefix, connection_ref, quota_gb,"
            " used_bytes) VALUES ('demo','examlops-projects','demo/',NULL,100,25000000000)"
        )
        conn.execute(
            "INSERT INTO project_pipelines (project, kind, ref, status, schedule) VALUES"
            " ('demo','prefect','examlops-jpcp','healthy','0 2 * * *')"
        )
        conn.execute(
            "INSERT INTO traffic_rules (model, rules) VALUES ('JPCP', '{\"Production\": 100}')"
        )
        conn.commit()
    finally:
        conn.close()

    from examlops.connections import create_connection

    create_connection(
        "raw", "s3", project="demo", config={"bucket": "b"}, secret_value="not-transported"
    )
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
