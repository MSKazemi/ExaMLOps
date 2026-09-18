"""Governance & compliance aggregators + /api/v1/governance/overview (F14 / ADR 0063)."""

import dbconn
import governance
import pytest

from examlops import platform_db as pdb
from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    monkeypatch.setenv("PLATFORM_DB", str(db))
    pdb.init_db()
    conn = dbconn.connect(db, row_factory=None)
    conn.execute(
        "INSERT INTO compliance_records (model, version, risk_class, annex_iv_path, provenance_hash) "
        "VALUES ('jpcp', 18, 'high', '/files/jpcp_annexiv.pdf', 'abc123')"
    )
    conn.execute("INSERT INTO model_cards (model, output_path) VALUES ('jpcp', '/cards/jpcp.md')")
    # 'demo' has costs but no card → uncarded
    conn.execute(
        # `recorded_at` is NOT NULL in the real schema
        "INSERT INTO model_costs (model_name, version, gpu_hours, cost_usd, recorded_at) "
        "VALUES ('demo', 1, 1.0, 4.0, '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()
    # Audit events go through the real writer, which chains them. Inserting them raw (as this
    # fixture used to) leaves them with no hash — and the integrity report then has nothing to
    # anchor, which is the truth it must tell rather than paper over.
    from examlops.data.audit import write_audit_event

    write_audit_event("exa", "mohsen", "promote", "JPCP")
    write_audit_event("control-plane", "sysadmin", "approval_granted", "JPCP")
    return str(db)


async def _login(client, password):
    r = await client.post("/api/auth/login", json={"password": password})
    return r.json()["token"]


# ── EU AI Act compliance (F14 R2) ────────────────────────────────────────────


def test_compliance_status(platform_db):
    out = governance.compliance_status(platform_db)
    jpcp = out["rows"][0]
    assert jpcp["model"] == "jpcp"
    assert jpcp["riskClass"] == "high"
    assert jpcp["technicalFile"] is True
    assert jpcp["provenance"] is True


# ── model-card coverage (F14 R5) ─────────────────────────────────────────────


def test_card_coverage_reports_gaps(platform_db):
    out = governance.model_card_coverage(platform_db)
    assert out["withCard"] == ["jpcp"]
    assert "demo" in out["withoutCard"]  # honest gap, not hidden
    assert 0 < out["coverage"] < 1


# ── audit integrity (F14 R3) ─────────────────────────────────────────────────


def test_audit_integrity_reports_the_logs_own_chain(platform_db):
    """The digest must be the one `exa audit verify` computes — not a second chain of our own."""
    from examlops.data.audit import verify_audit_chain

    out = governance.audit_integrity(platform_db)
    assert out["count"] == 2
    assert out["unchained"] == 0
    assert out["verified"] is True
    assert out["headDigest"] == verify_audit_chain()["head_hash"], (
        "the page shows a digest the platform's own verifier does not recognise, so an operator "
        "copying it as an external anchor is anchoring nothing"
    )
    e = out["entries"]
    assert e[1]["prevHash"] == e[0]["hash"], "entries must carry the stored links, in order"


def test_audit_integrity_says_what_it_actually_verified(platform_db):
    """`verified` used to be True by construction. It must now name its scope."""
    out = governance.audit_integrity(platform_db)
    assert "newest" in out["verifiedScope"], out["verifiedScope"]


def test_audit_integrity_does_not_invent_a_digest_for_unchained_events(platform_db, tmp_path):
    """Rows written outside the chain carry no tamper evidence, and the page must say so.

    The old implementation hashed five columns of every row itself, so it produced a confident
    digest for events that had none — the failure mode the platform already hit once, when every
    dashboard-written event turned out to be unchained.
    """
    # A fresh store: the append-only triggers refuse a DELETE from `audit_events`, which is the
    # tamper-evidence doing its job — so this cannot be set up by clearing the fixture's log.
    fresh = tmp_path / "unchained.db"
    import os

    before = os.environ.get("PLATFORM_DB")
    os.environ["PLATFORM_DB"] = str(fresh)
    try:
        pdb.init_db()
        conn = dbconn.connect(fresh, row_factory=None)
        conn.execute(
            "INSERT INTO audit_events (source, actor, action, target) VALUES ('raw','x','y','z')"
        )
        conn.commit()
        conn.close()
        out = governance.audit_integrity(str(fresh))
    finally:
        if before is None:
            os.environ.pop("PLATFORM_DB", None)
        else:
            os.environ["PLATFORM_DB"] = before
    assert out["count"] == 1
    assert out["unchained"] == 1
    assert out["headDigest"] is None
    assert out["entries"] == []


def test_audit_integrity_reports_a_broken_tail_as_broken(platform_db):
    """The test that makes `verified` mean something.

    Without this, hard-coding `verified = True` passes every other assertion here — which is how
    the old implementation got away with being true by construction. The append-only triggers
    refuse an UPDATE or a DELETE, so a broken chain is built the only way the schema allows: an
    INSERT carrying a hash that does not follow from the head.
    """
    conn = dbconn.connect(platform_db, row_factory=None)
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, tenant, prev_hash, hash) "
        "VALUES ('exa','m','promote','TAMPERED','default','notthehead','bogus')"
    )
    conn.commit()
    conn.close()

    out = governance.audit_integrity(platform_db)
    assert out["verified"] is False, (
        "a chained event whose hash does not follow from the head was reported as verified"
    )
    assert "newest" in out["verifiedScope"]


def test_audit_integrity_deterministic(platform_db):
    a = governance.audit_integrity(platform_db)["headDigest"]
    b = governance.audit_integrity(platform_db)["headDigest"]
    assert a == b  # deterministic → an external copy can verify tamper-evidence


# ── NIST posture (F14 R1 — honest) ───────────────────────────────────────────


def test_nist_posture_honest_grading(platform_db):
    out = governance.nist_posture(platform_db)
    by_control = {c["control"]: c for c in out["controls"]}
    assert by_control["MANAGE-4.1"]["status"] == "satisfied"  # has approval event
    assert by_control["MAP-1.1"]["status"] == "satisfied"  # has compliance record
    # model-card coverage is partial (jpcp carded, demo not) → not false-green
    assert by_control["MEASURE-2.1"]["status"] == "partial"
    assert out["total"] == 4


def test_posture_graceful_on_empty(tmp_path, monkeypatch):
    db = tmp_path / "e.db"
    dbconn.connect(db, row_factory=None).close()
    out = governance.nist_posture(str(db))
    # empty evidence → all gaps, never false green
    assert all(c["status"] == "gap" for c in out["controls"])


# ── endpoint ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_governance_endpoint_requires_auth(client, platform_db):
    r = await client.get("/api/v1/governance/overview")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_governance_endpoint_composes(client, platform_db):
    token = await _login(client, VIEWER_PW)
    r = await client.get(
        "/api/v1/governance/overview", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["compliance"]["rows"][0]["model"] == "jpcp"
    assert body["audit"]["count"] == 2
    assert body["posture"]["total"] == 4
