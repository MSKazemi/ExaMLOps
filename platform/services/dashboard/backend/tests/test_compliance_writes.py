"""Compliance write router — classify / conformity (admin, audited, shared examlops path).

Verifies the EU-AI-Act edit-parity added to the Governance area: classify a system's risk tier and
advance its conformity state, through the shared `examlops.compliance` code paths (validation +
state-machine + hash-chained `source=dashboard` audit), admin + `compliance.classify` gated.
"""

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    # Build the REAL schema via examlops so compliance_systems + hash-chained audit_events exist —
    # the router writes through the shared examlops path, so tests exercise that path end-to-end.
    from examlops import data as pdb

    # force=False on purpose: the DDL is cached per engine (SQLite: this tmp path, never seen
    # before; Postgres: this schema, already built), and re-running 127 CREATE TABLEs per test
    # cost ~30s each there. Row isolation is the autouse fixture in conftest, not the DDL.
    pdb.init_db()
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


async def test_classify_requires_admin(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.post(
        "/api/compliance/classify/JPCP",
        json={"riskTier": "high"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_classify_persists_and_audits_as_dashboard(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        "/api/compliance/classify/JPCP",
        json={"riskTier": "high", "intendedPurpose": "HPC power prediction"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    conn = dbconn.connect(platform_db, row_factory=None)
    row = conn.execute(
        "SELECT risk_tier, in_scope, intended_purpose FROM compliance_systems WHERE model='JPCP'"
    ).fetchone()
    assert row == ("high", 1, "HPC power prediction")
    # Audited through the shared path with source=dashboard.
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='compliance_classified'"
    ).fetchone()[0]
    assert n == 1
    conn.close()


async def test_classify_invalid_tier_400(client, platform_db):
    token = await _login(client, ADMIN_PW)
    r = await client.post(
        "/api/compliance/classify/JPCP",
        json={"riskTier": "bogus"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


async def test_conformity_valid_then_invalid_transition(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    # Establish the system (state defaults to 'draft').
    await client.post("/api/compliance/classify/JPCP", json={"riskTier": "high"}, headers=h)
    # draft → documented is allowed.
    ok = await client.post(
        "/api/compliance/conformity/JPCP", json={"state": "documented"}, headers=h
    )
    assert ok.status_code == 200, ok.text
    conn = dbconn.connect(platform_db, row_factory=None)
    assert (
        conn.execute(
            "SELECT conformity_state FROM compliance_systems WHERE model='JPCP'"
        ).fetchone()[0]
        == "documented"
    )
    conn.close()
    # documented → declared is NOT a valid transition (must pass through assessed).
    bad = await client.post(
        "/api/compliance/conformity/JPCP", json={"state": "declared"}, headers=h
    )
    assert bad.status_code == 400


async def test_list_systems_surfaces_classification(client, platform_db):
    token = await _login(client, ADMIN_PW)
    h = {"Authorization": f"Bearer {token}"}
    await client.post("/api/compliance/classify/MACK", json={"riskTier": "limited"}, headers=h)
    r = await client.get("/api/compliance/systems", headers=h)
    assert r.status_code == 200
    systems = {s["model"]: s for s in r.json()}
    assert systems["MACK"]["risk_tier"] == "limited"
