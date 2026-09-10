"""Offline reporting — cost/carbon/project (enterprise-readiness Phase 3, item 3.5).

Proves reports assemble from real platform data, render to text + HTML (dependency-free), degrade
PDF→HTML gracefully when WeasyPrint is absent, and fail-open per section (a broken source never
blanks the whole report).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb

    pdb.init_db()
    pdb.record_model_cost("JPCP", 17, "run-1", "job-1", gpu_hours=4.0, cost_usd=10.0)
    pdb.record_model_cost("JPCP", 18, "run-2", "job-2", gpu_hours=2.0, cost_usd=5.0)
    pdb.write_carbon_record("JPCP", "run-1", 3.0, 1500.0, grid_intensity=500.0)
    pdb.create_project("research", cpu_limit=4, memory_limit_gb=8, storage_gb=100)
    return pdb


def test_assemble_aggregates_cost_and_carbon(seeded):
    from examlops import reporting

    r = reporting.assemble_report()
    cost = r["sections"]["cost"]
    assert cost["total_gpu_hours"] == 6.0
    assert cost["total_cost_usd"] == 15.0
    carbon = r["sections"]["carbon"]
    assert carbon["operational_kg_co2e"] == 1.5  # 1500 g → 1.5 kg
    # ADR 0112 R-ee: an operational sum is not a total while embodied carbon is unmeasured
    assert carbon["total_kg_co2e"] is None and carbon["embodied_kg_co2e"] is None
    assert carbon["scope"] == "operational"
    assert any(p["name"] == "research" for p in r["sections"]["projects"]["rows"])


def test_render_text_and_html(seeded):
    from examlops import reporting

    report = reporting.assemble_report()
    text = reporting.render_text(report)
    assert "COST" in text and "JPCP" in text and "CARBON" in text
    html = reporting.render_html(report)
    assert html.startswith("<!doctype html>") and "JPCP" in html and "kg CO2e" in html


def test_generate_writes_file(seeded, tmp_path):
    from examlops import reporting

    out = tmp_path / "r.html"
    result = reporting.generate("html", out=str(out))
    assert result["path"] == str(out) and out.exists()
    assert "<h1>" in out.read_text()


def test_pdf_degrades_to_html_without_weasyprint(seeded, monkeypatch):
    from examlops import reporting

    monkeypatch.setattr(reporting, "_weasyprint_available", lambda: False)
    result = reporting.generate("pdf")
    assert result["degraded"] is True and result["format"] == "html"
    assert result["content"].startswith("<!doctype html>")


def test_section_failure_is_isolated(seeded, monkeypatch):
    """A broken data source degrades its own section, not the whole report."""
    from examlops import reporting

    def _boom():
        raise RuntimeError("carbon table exploded")

    # get_carbon_records' body now lives in examlops.data.finops (item 4.5 relocation); patch there.
    monkeypatch.setattr("examlops.data.finops.get_carbon_records", _boom)
    r = reporting.assemble_report()
    assert "error" in r["sections"]["carbon"]  # isolated
    assert r["sections"]["cost"]["total_cost_usd"] == 15.0  # cost still fine
