# tests/unit/test_evidence_sufficiency.py
"""ADR 0110 decision 6 — a compliance pack names what it cannot vouch for.

The collectors only count rows. These tests break the records underneath them — for real, with
the access an attacker with the database would have — and check that the pack stops vouching:
an edited audit event makes the change log *insufficient* (a gap that keeps a declaration a
draft), an edited anchored row does the same to data governance, and evidence in records outside
the chain is named as not tamper-evident rather than passed off as verified.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def _section(doc, key):
    return next(s for s in doc.sections if s.key == key)


def _seed_changes(model: str = "JPCP") -> None:
    from examlops import platform_db
    from examlops.compliance import classify_system

    classify_system(model, "high", "HPC triage", "internal ops", "tester")
    platform_db.write_audit_event("cli", "tester", "retrain_triggered", model, {"n": 1})
    platform_db.write_audit_event("cli", "tester", "promotion", model, {"to": "Production"})


def _tamper_audit_event(event_id: int) -> None:
    """Edit a chained event the way someone with database access would: drop the guard first."""
    from examlops.data import get_db

    with get_db() as conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        conn.execute("UPDATE audit_events SET details = '{\"n\": 999}' WHERE id = ?", (event_id,))


def test_every_collector_has_an_evidence_source_mapping():
    # An unmapped section would have unknown provenance; the test that keeps the two in step.
    from examlops.compliance import _COLLECTORS
    from examlops.compliance.sufficiency import EVIDENCE_SOURCES

    assert set(EVIDENCE_SOURCES) == set(_COLLECTORS)


def test_intact_chain_verifies_the_change_log_and_names_untamperable_sources():
    from examlops.compliance import generate_technical_file

    _seed_changes()
    doc = generate_technical_file("JPCP")
    assert _section(doc, "changes").status == "verified"
    assert _section(doc, "record_keeping").status == "verified"
    sysdesc = _section(doc, "system_description")
    assert sysdesc.present and sysdesc.status == "unverified"
    assert any("compliance_systems is outside the hash chain" in r for r in sysdesc.reasons)
    # Unverified is named, not counted: otherwise no technical file could ever be complete.
    assert doc.gaps == doc.missing and doc.insufficient == 0
    md = doc.to_markdown()
    assert "## Insufficient evidence" in md and "not tamper-evident" in md
    assert "## Evidence integrity" in md and "intact" in md


def test_an_edited_audit_event_makes_chain_backed_sections_insufficient():
    from examlops.compliance import generate_technical_file
    from examlops.data import get_db

    _seed_changes()
    with get_db() as conn:
        first = conn.execute("SELECT MIN(id) AS i FROM audit_events").fetchone()["i"]
    _tamper_audit_event(first)
    doc = generate_technical_file("JPCP")
    for key in ("changes", "record_keeping"):
        s = _section(doc, key)
        assert s.present is True  # the rows are still there…
        assert s.status == "insufficient"  # …and the pack no longer vouches for them
        assert any("audit chain is broken at event" in r for r in s.reasons)
    assert doc.insufficient == 2
    assert doc.gaps == doc.missing + 2
    md = doc.to_markdown()
    assert "INSUFFICIENT EVIDENCE" in md
    assert f"BROKEN at event {first}" in md


def test_a_declaration_resting_on_a_broken_chain_stays_a_draft():
    from examlops.compliance import generate_declaration, generate_technical_file
    from examlops.data.governance import save_technical_file

    _seed_changes("D1")
    _tamper_audit_event(1)
    doc = generate_technical_file("D1")
    save_technical_file("D1", doc.to_markdown(), gaps=doc.gaps)
    decl = generate_declaration(
        "D1",
        issued_at="Bologna, 2026-09-10",
        provider="ACME",
        provider_address="Via X 1",
        signatory="A. Person",
        signatory_function="CTO",
    )
    assert decl.draft
    assert any("gap" in b for b in decl.blockers)


def test_a_failing_check_is_insufficient_never_a_pass(monkeypatch):
    from examlops.compliance import generate_technical_file
    from examlops.data import audit

    _seed_changes()

    def _boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(audit, "verify_audit_chain", _boom)
    doc = generate_technical_file("JPCP")
    s = _section(doc, "changes")
    assert s.status == "insufficient"
    assert any("could not be verified" in r and "database is locked" in r for r in s.reasons)
    assert "not verified — the check failed" in doc.to_markdown()


def test_an_edited_anchored_row_makes_data_governance_insufficient():
    from examlops.compliance import generate_technical_file
    from examlops.data import get_db
    from examlops.data.data_assets import record_dataset_revision
    from examlops.telemetry_anchor import anchor_telemetry

    _seed_changes()
    rev = SimpleNamespace(
        backend="minio",
        dataset="FData",
        revision_id="rev-1",
        kind="content-hash",
        uri="s3://datasets/FData",
        schema_hash="abc",
    )
    record_dataset_revision(rev, row_count=10)

    before = _section(generate_technical_file("JPCP"), "data_governance")
    assert before.status == "unverified"
    assert any("newer than the last anchor" in r for r in before.reasons)

    anchor_telemetry("tester")
    anchored = _section(generate_technical_file("JPCP"), "data_governance")
    assert not any("dataset_revisions" in r for r in anchored.reasons)  # now vouched for
    assert any("data_quality_checks" in r for r in anchored.reasons)  # this one never is

    with get_db() as conn:
        conn.execute("UPDATE dataset_revisions SET uri = 's3://elsewhere' WHERE dataset='FData'")
    after = _section(generate_technical_file("JPCP"), "data_governance")
    assert after.status == "insufficient"
    assert any("dataset_revisions: anchor" in r and "no longer matches" in r for r in after.reasons)


def test_an_autonomous_action_without_an_inverse_makes_record_keeping_insufficient():
    from examlops import platform_db
    from examlops.compliance import generate_technical_file
    from examlops.evidence import correlated

    _seed_changes()
    with correlated(mode="autonomous"):
        platform_db.write_audit_event("autopilot", "autopilot", "promotion", "JPCP", {"v": 2})
    s = _section(generate_technical_file("JPCP"), "record_keeping")
    assert s.status == "insufficient"
    assert any("declared no rollback_ref" in r for r in s.reasons)


def test_the_nist_report_does_not_count_insufficient_evidence_as_satisfied():
    from examlops.governance import governance_report

    _seed_changes()
    clean = {c.control.id: c for c in governance_report(model="JPCP").controls}
    assert clean["GOVERN-1.1"].status == "satisfied"  # evidence: record_keeping (chain)

    _tamper_audit_event(1)
    rep = governance_report(model="JPCP")
    tampered = {c.control.id: c for c in rep.controls}
    assert tampered["GOVERN-1.1"].status == "gap"
    assert tampered["GOVERN-1.1"].insufficient_evidence == ["record_keeping"]
    as_dict = next(c for c in rep.as_dict()["controls"] if c["id"] == "GOVERN-1.1")
    assert as_dict["insufficient_evidence"] == ["record_keeping"]
    assert any("audit chain is broken" in n for n in as_dict["evidence_notes"])
