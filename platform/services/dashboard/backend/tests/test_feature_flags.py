"""Server-side feature-flag evaluator + admin endpoints (F25 / ADR 0070)."""

import dbconn
import feature_flags as ff
import pytest

from examlops import platform_db as pdb
from tests.conftest import ADMIN_PW, VIEWER_PW

# ── deterministic bucket (F25 R5) ────────────────────────────────────────────


def test_subject_bucket_is_deterministic_and_bounded():
    a = ff.subject_bucket("flagX", "user-1")
    b = ff.subject_bucket("flagX", "user-1")
    assert a == b and 0 <= a < 100
    # different subject usually different bucket (not guaranteed, but the pair below differs)
    assert ff.subject_bucket("flagX", "user-1") != ff.subject_bucket("flagX", "user-99999")


# ── evaluation (F25 R1/R3) ───────────────────────────────────────────────────


def test_default_on_flag_enabled():
    d = ff.FLAG_DEFS["mlopsConsole"]
    assert ff.evaluate(d, role="viewer", tenant="default", subject="u", override=None) is True


def test_override_off_wins():
    d = ff.FLAG_DEFS["mlopsConsole"]
    assert ff.evaluate(d, role="viewer", tenant="default", subject="u", override=False) is False


def test_role_targeting():
    d = ff.FlagDef("x", "admin only", default=True, roles=("admin",))
    assert ff.evaluate(d, role="admin", tenant="default", subject="u", override=None) is True
    assert ff.evaluate(d, role="viewer", tenant="default", subject="u", override=None) is False


def test_percentage_rollout_deterministic_and_admin_bypass():
    d = ff.FlagDef("x", "50%", default=True, percentage=50)
    # admin always in
    assert ff.evaluate(d, role="admin", tenant="default", subject="u", override=None) is True
    # a viewer's inclusion matches their bucket
    subject = "user-1"
    expected = ff.subject_bucket("x", subject) < 50
    assert (
        ff.evaluate(d, role="viewer", tenant="default", subject=subject, override=None) is expected
    )


def test_evaluate_all_returns_decisions(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    dbconn.connect(db, row_factory=None).close()
    decisions = ff.evaluate_all(str(db), role="viewer", tenant="default", subject="u")
    assert set(decisions) == set(ff.FLAG_DEFS)
    assert decisions["mlopsConsole"] is True


# ── admin override + audit (F25 R4) ──────────────────────────────────────────


def test_set_override_persists_and_audits(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    conn.commit()
    conn.close()

    assert ff.set_override(str(db), "mlopsConsole", False, "admin") is True
    # override now visible in evaluation
    decisions = ff.evaluate_all(str(db), role="admin", tenant="default", subject="u")
    assert decisions["mlopsConsole"] is False
    # audited
    conn = dbconn.connect(db, row_factory=None)
    row = conn.execute("SELECT action, target, details FROM audit_events").fetchone()
    conn.close()
    assert row == ("flag_set", "mlopsConsole", "enabled=False")


def test_set_override_rejects_unknown_flag(tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    dbconn.connect(db, row_factory=None).close()
    assert ff.set_override(str(db), "nope", True, "admin") is False


# ── endpoints ────────────────────────────────────────────────────────────────


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


@pytest.mark.asyncio
async def test_flags_endpoint_returns_decisions(client, tmp_path, monkeypatch):
    db = tmp_path / "p.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    dbconn.connect(db, row_factory=None).close()
    token = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/flags", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["flags"]["mlopsConsole"] is True


@pytest.mark.asyncio
async def test_admin_view_requires_admin(client):
    viewer = await _login(client, VIEWER_PW)
    r = await client.get("/api/v1/flags/admin", headers={"Authorization": f"Bearer {viewer}"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_set_flag(client, tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    conn.commit()
    conn.close()
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/v1/flags/mlopsConsole",
        json={"enabled": False},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["updated"] is True
