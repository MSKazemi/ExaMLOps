"""Fairness config write router — set / list (admin, audited, shared examlops path).

Verifies the C8 fairness edit-parity: declare a model's slicing attributes + disparity threshold
through the shared `examlops.data.governance.set_fairness_config` code path, admin + `fairness.manage`
gated, audited `source=dashboard`.
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


async def test_set_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/fairness",
        json={"model": "JPCP", "sliceAttrs": ["region"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_set_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/fairness",
        json={
            "model": "JPCP",
            "sliceAttrs": ["region", "cluster"],
            "threshold": 0.15,
            "gatePromotion": True,
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    conn = sqlite3.connect(platform_db)
    row = conn.execute(
        "SELECT slice_attrs, threshold, gate_promotion FROM fairness_config WHERE model='JPCP'"
    ).fetchone()
    assert '"region"' in row[0] and '"cluster"' in row[0]
    assert row[1] == 0.15
    assert row[2] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='fairness_config_set'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_set_validates(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    assert (
        await client.post("/api/fairness", json={"sliceAttrs": ["a"]}, headers=h)
    ).status_code == 400
    assert (
        await client.post("/api/fairness", json={"model": "m", "sliceAttrs": []}, headers=h)
    ).status_code == 400
    assert (
        await client.post(
            "/api/fairness", json={"model": "m", "sliceAttrs": ["a"], "threshold": 2}, headers=h
        )
    ).status_code == 400


async def test_list_parses_slice_attrs(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/api/fairness", json={"model": "MACK", "sliceAttrs": ["region"]}, headers=h)
    r = await client.get("/api/fairness", headers=h)
    assert r.status_code == 200
    by_model = {c["model"]: c for c in r.json()}
    assert by_model["MACK"]["slice_attrs"] == ["region"]
    assert by_model["MACK"]["enabled"] is True
