"""D1 — EU AI Act compliance tooling (ADR 0012).

GWT acceptance criteria from ``design/vision/specs/D1-eu-ai-act-compliance.md`` §5.
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


def test_gwt1_classify_gate_blocks_unclassified():
    """GWT-1: an in-scope model with no risk tier is blocked from promotion."""
    from examlops import platform_db
    from examlops.compliance import promotion_blocked_reason

    # Register in scope but unclassified.
    platform_db.set_compliance_system("JPCP", in_scope=True, risk_tier=None)
    reason = promotion_blocked_reason("JPCP")
    assert reason is not None
    assert "risk classification" in reason

    # Not in scope => no block.
    assert promotion_blocked_reason("OTHER") is None


def test_classify_then_unblocked():
    from examlops.compliance import classify_system, promotion_blocked_reason

    classify_system("JPCP", "high", "HPC triage", "internal", "tester")
    assert promotion_blocked_reason("JPCP") is None


def test_classify_rejects_bad_tier():
    from examlops.compliance import classify_system

    with pytest.raises(ValueError):
        classify_system("JPCP", "super-dangerous", "x", "y", "tester")


def test_gwt2_technical_file_has_all_sections_and_disclaimer():
    """GWT-2: the technical file contains all Annex-IV sections + disclaimer."""
    from examlops.compliance import DISCLAIMER, FRAMEWORK, classify_system, generate_technical_file

    classify_system("JPCP", "high", "HPC triage", "internal ops", "tester")
    doc = generate_technical_file("JPCP")
    assert len(doc.sections) == len(FRAMEWORK)
    md = doc.to_markdown()
    assert DISCLAIMER in md
    assert "Annex IV" in md or "Art. 12" in md
    # The system-description section should be present (we classified it).
    sysdesc = next(s for s in doc.sections if s.key == "system_description")
    assert sysdesc.present is True


def test_gwt3_missing_fairness_flagged_not_omitted():
    """GWT-3: a model missing a fairness report has that section flagged, not omitted."""
    from examlops.compliance import classify_system, generate_technical_file

    classify_system("JPCP", "high", "purpose", "ctx", "tester")
    doc = generate_technical_file("JPCP")
    fairness = next(s for s in doc.sections if s.key == "fairness")
    assert fairness.present is False  # flagged
    assert "MISSING EVIDENCE" in doc.to_markdown()
    assert doc.gaps > 0


def test_technical_file_versioned():
    from examlops.compliance import classify_system, generate_technical_file
    from examlops.platform_db import list_technical_files, save_technical_file

    classify_system("JPCP", "high", "p", "c", "tester")
    doc = generate_technical_file("JPCP")
    v1 = save_technical_file("JPCP", doc.to_markdown(), gaps=doc.gaps)
    v2 = save_technical_file("JPCP", doc.to_markdown(), gaps=doc.gaps)
    assert v1 == 1 and v2 == 2
    assert len(list_technical_files("JPCP")) == 2


def test_gwt4_art12_reports_uncovered():
    """GWT-4: uncovered event types are reported."""
    from examlops import platform_db
    from examlops.compliance import check_art12_logging

    # Seed one covered event type.
    platform_db.write_audit_event("cli", "tester", "retrain_triggered", "JPCP", None)
    cov = check_art12_logging("JPCP")
    assert cov["coverage"]["retrain_triggered"] is True
    assert "promotion" in cov["uncovered"]
    assert cov["coverage_pct"] < 1.0


def test_gwt5_conformity_invalid_transition_rejected():
    """GWT-5: documented → declared (skipping assessed) is rejected."""
    from examlops.compliance import classify_system, set_conformity_state

    classify_system("JPCP", "high", "p", "c", "tester")
    set_conformity_state("JPCP", "documented", "tester")
    with pytest.raises(ValueError):
        set_conformity_state("JPCP", "declared", "tester")  # must go through 'assessed'
    # Valid path works.
    set_conformity_state("JPCP", "assessed", "tester")
    set_conformity_state("JPCP", "declared", "tester")


def test_framework_shared_shape():
    """R9: the framework is a data-driven control→article→evidence list."""
    from examlops.compliance import FRAMEWORK

    assert all({"control", "article", "evidence"} <= set(e) for e in FRAMEWORK)
    controls = {e["control"] for e in FRAMEWORK}
    assert "fairness" in controls and "record_keeping" in controls


def test_cli_smoke():
    from typer.testing import CliRunner

    from examlops.cli.main import app

    runner = CliRunner()
    r1 = runner.invoke(
        app,
        ["compliance", "classify", "JPCP", "--risk-tier", "high", "--purpose", "triage"],
    )
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["compliance", "technical-file", "JPCP"])
    assert r2.exit_code == 0, r2.output
    assert "Annex IV" in r2.output
    r3 = runner.invoke(app, ["compliance", "status", "JPCP"])
    assert r3.exit_code == 0, r3.output
    assert "high" in r3.output
    r4 = runner.invoke(app, ["compliance", "art12", "JPCP"])
    assert r4.exit_code == 0, r4.output
