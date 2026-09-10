# tests/unit/test_carbon_is_not_reported_as_total.py
"""ADR 0112 R-ee — an operational-only carbon figure must never be reported as a total.

Every carbon number the platform records is operational (energy × grid intensity). Embodied carbon
(manufacturing the hardware) is not measured, so any figure labelled "total" understates emissions
in the direction that flatters the platform. `examlops.finops.carbon.carbon_scope` is the one
definition; this guard checks the report built on it, and scans every user-facing surface for a
carbon "total" label so one cannot creep back in.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "platform" / "cli" / "src"))


def test_carbon_scope_never_reports_a_total():
    from examlops.finops.carbon import carbon_scope

    measured = carbon_scope(1500.0)
    assert measured["operational_kg_co2e"] == 1.5
    assert measured["total_kg_co2e"] is None and measured["embodied_kg_co2e"] is None
    assert "not a total" in measured["embodied"]
    unmeasured = carbon_scope(None)
    assert unmeasured["operational_kg_co2e"] is None  # unmeasured is None, never 0.0


def test_the_offline_report_labels_its_carbon_operational(tmp_path, monkeypatch):
    from examlops import platform_db, reporting

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    platform_db.init_db()
    platform_db.write_carbon_record("JPCP", None, kwh=5.0, co2e_g=1500.0)
    report = reporting.assemble_report(generated_at="2026-09-10T00:00:00Z")
    text, page = reporting.render_text(report), reporting.render_html(report)
    assert "operational kg CO2e: 1.5" in text and "embodied kg CO2e: unavailable" in text
    assert "operational: <b>1.5</b>" in page and "embodied:" in page
    assert "total kg CO2e" not in text


# A carbon figure labelled as a total, in any casing, on a user-facing surface. The cost figures
# ("Total spend", "Total GPU-Hours") are totals and are not matched.
_TOTAL_CARBON = re.compile(
    r"total[\s_-]*(kg|g)?[\s_-]*co2e?|co2e?[\s_-]*total|total[\s_-]*carbon|carbon[\s_-]*total",
    re.IGNORECASE,
)
_SURFACES = [
    "platform/cli/src/examlops",
    "platform/services/dashboard/backend",
    "platform/services/dashboard/frontend/src",
    "platform/services/agent/skipper",
]
# Lines that name the rule itself (this ADR's own vocabulary) are not violations.
_ALLOWED = re.compile(
    r"R-ee|total_kg_co2e\"?\s*[:=]\s*None|\"total_kg_co2e\": None|not a total|``total_kg_co2e``"
)


@pytest.mark.parametrize("surface", _SURFACES)
def test_no_surface_labels_a_carbon_sum_as_a_total(surface):
    hits = []
    for path in (ROOT / surface).rglob("*"):
        if (
            path.suffix not in {".py", ".ts", ".tsx"}
            or "test" in path.name
            or "__pycache__" in path.parts
        ):
            continue
        for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if _TOTAL_CARBON.search(line) and not _ALLOWED.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:120]}")
    assert not hits, "carbon reported as a total (ADR 0112 R-ee):\n" + "\n".join(hits)
