"""Traffic-console write router — A/B start/stop + shadow enable/disable (viewer read, admin write).

Verifies the traffic-console edit-parity: the writes go through the same `examlops.data` code paths
the CLI's `exa serve ab start|stop` / `exa serve shadow enable|disable` use (pure platform.db), are
admin + `traffic.manage` gated, and are audited `source=dashboard`; reads surface the A/B tests +
shadow config/comparisons (mirroring `exa serve ab status` / `shadow status|log`), fail-open.
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


# ── A/B testing ───────────────────────────────────────────────────────────────


async def test_ab_start_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/traffic/ab/start",
        json={"model": "JPCP"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_ab_start_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/traffic/ab/start",
        json={"model": "JPCP", "variant_a": "Production", "variant_b": "Canary", "split": 70},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT model, variant_a, variant_b, split_pct, status FROM ab_tests WHERE model='JPCP'"
    ).fetchone()
    assert row == ("JPCP", "Production", "Canary", 70, "running")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE source='dashboard' AND action='ab_test_started'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_ab_start_conflicts_when_already_running(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/traffic/ab/start", json={"model": "JPCP"}, headers=h)
    r = await client.post("/api/v1/traffic/ab/start", json={"model": "JPCP"}, headers=h)
    assert r.status_code == 409


async def test_ab_start_rejects_missing_model_and_bad_split(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    assert (
        await client.post("/api/v1/traffic/ab/start", json={"model": "  "}, headers=h)
    ).status_code == 400
    assert (
        await client.post(
            "/api/v1/traffic/ab/start", json={"model": "JPCP", "split": 150}, headers=h
        )
    ).status_code == 400


async def test_ab_stop_marks_completed_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/api/v1/traffic/ab/start", json={"model": "JPCP"}, headers=h)
    r = await client.post("/api/v1/traffic/ab/stop", json={"model": "JPCP"}, headers=h)
    assert r.status_code == 200
    assert r.json()["stopped"] is True
    conn = dbconn.connect(platform_db, row_factory=None)
    assert (
        conn.execute("SELECT status FROM ab_tests WHERE model='JPCP'").fetchone()[0] == "completed"
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE source='dashboard' AND action='ab_test_stopped'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


async def test_ab_stop_noop_when_none_running(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post("/api/v1/traffic/ab/stop", json={"model": "NONE"}, headers=h)
    assert r.status_code == 200
    assert r.json()["stopped"] is False


async def test_ab_list_readable_by_viewer(client, platform_db):
    admin = await _login(client, ADMIN_PW)
    await client.post(
        "/api/v1/traffic/ab/start",
        json={"model": "JPCP"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    viewer = await _login(client, VIEWER_PW)
    r = await client.get(
        "/api/v1/traffic/ab?model=JPCP", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tests"][0]["model"] == "JPCP"
    # analysis is present as a key (None until numpy/scipy + enough samples) — must never 500.
    assert "analysis" in body


# ── shadow deployments ────────────────────────────────────────────────────────


async def test_shadow_set_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/v1/traffic/shadow",
        json={"model": "JPCP", "enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_shadow_enable_then_disable_persists_and_audits(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/v1/traffic/shadow",
        json={"model": "JPCP", "enabled": True, "shadow_alias": "Canary"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = dbconn.connect(platform_db, row_factory=None)
    assert conn.execute(
        "SELECT shadow_alias, enabled FROM shadow_config WHERE model='JPCP'"
    ).fetchone() == ("Canary", 1)

    r2 = await client.post(
        "/api/v1/traffic/shadow", json={"model": "JPCP", "enabled": False}, headers=h
    )
    assert r2.status_code == 200
    assert conn.execute("SELECT enabled FROM shadow_config WHERE model='JPCP'").fetchone()[0] == 0
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE source='dashboard' AND action='shadow_config_set'"
        ).fetchone()[0]
        == 2
    )
    conn.close()


async def test_shadow_status_readable_by_viewer(client, platform_db):
    admin = await _login(client, ADMIN_PW)
    await client.post(
        "/api/v1/traffic/shadow",
        json={"model": "JPCP", "enabled": True},
        headers={"Authorization": f"Bearer {admin}"},
    )
    viewer = await _login(client, VIEWER_PW)
    r = await client.get(
        "/api/v1/traffic/shadow?model=JPCP", headers={"Authorization": f"Bearer {viewer}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["config"][0]["model"] == "JPCP"
    assert "results" in body


async def test_shadow_set_rejects_missing_model(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/traffic/shadow",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
