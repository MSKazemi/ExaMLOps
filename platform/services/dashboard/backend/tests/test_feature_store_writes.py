"""Feature-store write router — apply/list feature views (admin, audited, shared examlops path).

Verifies the A3 feature-store edit-parity: register/patch a feature view through the shared
`examlops.feature_store.apply_view` code path, admin + `feature.manage` gated, audited
`source=dashboard`.
"""

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    from examlops import data as pdb

    # force=False on purpose: the DDL is cached per engine (SQLite: this tmp path, never seen
    # before; Postgres: this schema, already built), and re-running 127 CREATE TABLEs per test
    # cost ~30s each there. Row isolation is the autouse fixture in conftest, not the DDL.
    pdb.init_db()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def test_apply_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/feature-store/views",
        json={"name": "fv", "entity": "job", "features": ["a"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_apply_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/feature-store/views",
        json={"name": "power_fv", "entity": "job", "features": ["cpu", "mem"], "ttlSeconds": 3600},
        headers=h,
    )
    assert r.status_code == 201, r.text
    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT entity, features_json, ttl_seconds FROM feature_views WHERE name='power_fv'"
    ).fetchone()
    assert row[0] == "job"
    assert '"cpu"' in row[1] and '"mem"' in row[1]
    assert row[2] == 3600
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='feature_view_apply'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_apply_validates(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    assert (
        await client.post(
            "/api/feature-store/views", json={"entity": "e", "features": ["a"]}, headers=h
        )
    ).status_code == 400
    assert (
        await client.post(
            "/api/feature-store/views", json={"name": "n", "entity": "e", "features": []}, headers=h
        )
    ).status_code == 400


async def test_apply_is_upsert_and_list_parses_features(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/api/feature-store/views",
        json={"name": "fv", "entity": "job", "features": ["a"]},
        headers=h,
    )
    await client.post(
        "/api/feature-store/views",
        json={"name": "fv", "entity": "job", "features": ["a", "b"]},
        headers=h,
    )
    r = await client.get("/api/feature-store/views", headers=h)
    assert r.status_code == 200
    views = {v["name"]: v for v in r.json()}
    assert views["fv"]["features"] == ["a", "b"]  # upserted, features parsed from JSON
