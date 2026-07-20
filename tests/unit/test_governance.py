"""D2 — NIST AI RMF control backbone (ADR 0027).

GWT acceptance criteria from ``design/vision/specs/D2-nist-ai-rmf.md`` §5.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()
    yield


def test_gwt1_catalogue_loads():
    """GWT-1: the catalogue YAML loads and validates against the schema."""
    from examlops.governance import catalogue_version, load_catalogue

    controls = load_catalogue()
    assert len(controls) >= 8
    assert catalogue_version()
    functions = {c.function for c in controls}
    assert functions <= {"Govern", "Map", "Measure", "Manage"}
    assert all(c.id and c.evidence for c in controls)


def test_gwt2_mapping_validation_passes():
    """GWT-2: the shipped mapping references real controls + evidence collectors."""
    from examlops.governance import validate_mapping

    errors = validate_mapping()
    assert errors == [], [f"{e.control_id}: {e.problem}" for e in errors]


def test_gwt2_mapping_validation_catches_bad_evidence(monkeypatch):
    """GWT-2: a control referencing a nonexistent evidence collector is flagged."""
    import examlops.governance as gov
    from examlops.governance import Control

    def fake_catalogue():
        return [Control("BAD-1", "Govern", "x", ["does_not_exist"])]

    monkeypatch.setattr(gov, "load_catalogue", fake_catalogue)
    errors = gov.validate_mapping()
    assert any("does_not_exist" in e.problem for e in errors)


def test_gwt3_coverage_satisfied_when_evidence_present():
    """GWT-3: a control whose evidence exists is marked satisfied."""
    from examlops.compliance import classify_system
    from examlops.governance import governance_report

    # system_description evidence = intended purpose present.
    classify_system("JPCP", "high", "HPC triage", "internal", "tester")
    rep = governance_report("default", model="JPCP")
    by_id = {c.control.id: c for c in rep.controls}
    assert by_id["MAP-1.1"].status == "satisfied"  # system_description present


def test_gwt4_gap_when_evidence_missing():
    """GWT-4: a control whose evidence source is absent is a gap, not a false pass."""
    from examlops.compliance import classify_system
    from examlops.governance import governance_report

    classify_system("JPCP", "high", "p", "c", "tester")  # only system_description
    rep = governance_report("default", model="JPCP")
    by_id = {c.control.id: c for c in rep.controls}
    # No fairness evidence => MEASURE-2.11 is a gap.
    assert by_id["MEASURE-2.11"].status == "gap"
    assert "fairness" in by_id["MEASURE-2.11"].missing_evidence


def test_gwt5_crosswalk_resolves():
    """GWT-5: controls crosswalk to EU AI Act articles + ISO 42001."""
    from examlops.governance import crosswalk

    rows = crosswalk()
    assert rows
    assert all("eu_ai_act" in r and "iso_42001" in r for r in rows)
    # MAP-1.1 crosswalks to Annex IV §1.
    map11 = next(r for r in rows if r["control"] == "MAP-1.1")
    assert "Annex IV" in map11["eu_ai_act"]


def test_report_summary_and_disclaimer():
    from examlops.compliance import classify_system
    from examlops.governance import COVERAGE_DISCLAIMER, governance_report

    classify_system("JPCP", "high", "p", "c", "tester")
    rep = governance_report("default", model="JPCP")
    s = rep.summary
    assert s["satisfied"] + s["partial"] + s["gap"] == len(rep.controls)
    assert "not a certification" in COVERAGE_DISCLAIMER
    assert rep.as_dict()["disclaimer"] == COVERAGE_DISCLAIMER


def test_report_audited():
    from examlops import platform_db
    from examlops.compliance import classify_system
    from examlops.governance import governance_report

    classify_system("JPCP", "high", "p", "c", "tester")
    governance_report("default", model="JPCP", persist_by="tester")
    with platform_db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE action='governance_report'"
        ).fetchall()
    assert len(rows) == 1


def test_shared_evidence_layer_with_d1():
    """The governance evidence keys resolve to the same D1 collectors."""
    from examlops.compliance import _COLLECTORS
    from examlops.governance import load_catalogue

    for c in load_catalogue():
        for ev in c.evidence:
            assert ev in _COLLECTORS


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r1 = runner.invoke(app, ["governance", "catalogue"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["governance", "validate"])
    assert r2.exit_code == 0, r2.output
    r3 = runner.invoke(app, ["governance", "report"])
    assert r3.exit_code == 0, r3.output
    r4 = runner.invoke(app, ["governance", "crosswalk"])
    assert r4.exit_code == 0, r4.output
    assert "MAP-1.1" in r4.output
