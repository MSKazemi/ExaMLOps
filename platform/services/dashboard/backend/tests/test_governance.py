"""Governance & compliance aggregators + /api/v1/governance/overview (F14 / ADR 0063)."""

import sqlite3

import governance
import pytest

from tests.conftest import VIEWER_PW


@pytest.fixture
def platform_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE compliance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, model TEXT, version INTEGER,
            risk_class TEXT, annex_iv_path TEXT, provenance_hash TEXT
        );
        CREATE TABLE model_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, model TEXT, output_path TEXT, actor TEXT
        );
        CREATE TABLE model_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, model_name TEXT, version INTEGER,
            gpu_hours REAL, cost_usd REAL, recorded_at TEXT
        );
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, actor TEXT,
            action TEXT, target TEXT, details TEXT, ts TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.execute(
        "INSERT INTO compliance_records (model, version, risk_class, annex_iv_path, provenance_hash) "
        "VALUES ('jpcp', 18, 'high', '/files/jpcp_annexiv.pdf', 'abc123')"
    )
    conn.execute("INSERT INTO model_cards (model, output_path) VALUES ('jpcp', '/cards/jpcp.md')")
    # 'demo' has costs but no card → uncarded
    conn.execute(
        "INSERT INTO model_costs (model_name, version, gpu_hours, cost_usd) VALUES ('demo', 1, 1.0, 4.0)"
    )
    conn.executemany(
        "INSERT INTO audit_events (source, actor, action, target) VALUES (?,?,?,?)",
        [
            ("exa", "mohsen", "promote", "JPCP"),
            ("control-plane", "sysadmin", "approval_granted", "JPCP"),
        ],
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
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


def test_audit_integrity_chain(platform_db):
    out = governance.audit_integrity(platform_db)
    assert out["count"] == 2
    assert out["headDigest"] is not None
    assert out["verified"] is True
    # chain links: 2nd entry's prevHash == 1st entry's hash
    e = out["entries"]
    assert e[1]["prevHash"] == e[0]["hash"]
    assert e[0]["prevHash"] == "genesis"


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
    sqlite3.connect(db).close()
    monkeypatch.setenv("PLATFORM_DB", str(db))
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
