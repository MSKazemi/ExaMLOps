"""Compliance page — the Annex-IV technical file with evidence sufficiency (ADR 0012 clause 4).

The dashboard's Compliance page previews the technical file through the same generator as
`exa compliance technical-file`, with ADR 0110's per-section sufficiency, and saves versions
(admin, audited) into the same versioned store `exa compliance declare` rests on.
"""

import dbconn
import pytest

from tests.conftest import ADMIN_PW, VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    from examlops import data as pdb

    pdb.init_db()
    return str(db)


async def _h(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed(model="JPCP"):
    from examlops import platform_db
    from examlops.compliance import classify_system

    classify_system(model, "high", "HPC power prediction", "internal ops", "tester")
    platform_db.write_audit_event("cli", "tester", "retrain_triggered", model, {"n": 1})


async def test_viewer_previews_the_technical_file_with_sufficiency(client, platform_db):
    _seed()
    r = await client.get("/api/compliance/technical-file/JPCP", headers=await _h(client, VIEWER_PW))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "JPCP" and "NOT legal advice" in body["disclaimer"]
    by_key = {s["key"]: s for s in body["sections"]}
    assert by_key["system_description"]["status"] == "unverified"  # compliance_systems row
    assert by_key["changes"]["status"] == "verified"  # intact audit chain
    assert by_key["fairness"]["status"] == "missing"
    assert body["gaps"] == body["missing"] + body["insufficient"]
    assert body["auditChain"].startswith("intact")
    # a preview stores nothing
    r = await client.get(
        "/api/compliance/technical-files/JPCP", headers=await _h(client, VIEWER_PW)
    )
    assert r.json() == []


async def test_a_broken_chain_shows_as_insufficient_on_the_page(client, platform_db):
    _seed()
    conn = dbconn.connect(platform_db, row_factory=None)
    conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
    conn.execute("UPDATE audit_events SET details='{\"n\": 9}' WHERE action='retrain_triggered'")
    conn.commit()
    conn.close()
    r = await client.get("/api/compliance/technical-file/JPCP", headers=await _h(client, VIEWER_PW))
    body = r.json()
    changes = next(s for s in body["sections"] if s["key"] == "changes")
    assert changes["status"] == "insufficient"
    assert any("audit chain is broken" in reason for reason in changes["reasons"])
    assert body["insufficient"] >= 1 and body["auditChain"].startswith("BROKEN")


async def test_saving_a_version_is_admin_only_and_audited(client, platform_db):
    _seed()
    r = await client.post(
        "/api/compliance/technical-file/JPCP", headers=await _h(client, VIEWER_PW)
    )
    assert r.status_code == 403
    admin = await _h(client, ADMIN_PW)
    r = await client.post("/api/compliance/technical-file/JPCP", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1
    versions = (await client.get("/api/compliance/technical-files/JPCP", headers=admin)).json()
    assert [v["version"] for v in versions] == [1] and versions[0]["gaps"] == r.json()["gaps"]
    conn = dbconn.connect(platform_db, row_factory=None)
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE source='dashboard' AND action='technical_file_saved'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


async def test_art12_coverage_is_readable(client, platform_db):
    _seed()
    r = await client.get("/api/compliance/art12/JPCP", headers=await _h(client, VIEWER_PW))
    assert r.status_code == 200
    body = r.json()
    assert body["coverage"]["retrain_triggered"] is True
    assert "approval" in body["uncovered"]
