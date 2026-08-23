"""An SLO nobody has measured is not an SLO being met.

``sli = (good / total) if total else 1.0`` scores **zero samples as a perfect ratio**. Everything
downstream then reads maximally healthy: ``budget_remaining`` 1.0, ``burn_rate`` 0.0, ``ok`` True.
So a target nobody has taken a single measurement against is published as *met* — printed as ``OK``
by ``exa slo status``, rendered as a green **Meeting** pill by the dashboard, and treated as "no
regression" by ``budget_exhausted`` on the ``exa pipeline promote`` road.

The value that means "we have no evidence" and the value that means "the evidence is perfect" were
the same value. ``T85`` fixed this for the champion/challenger gate by checking ``n`` at that one
call site; this fixes it where it originates, so every reader inherits the distinction.

Note on what is deliberately *not* changed: an unmeasured SLO does not block promotion. A model
cannot produce SLI samples before it serves, and it cannot serve before it is promoted, so
blocking would deadlock the first promotion of every model. The gate reports instead.
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


def test_an_unmeasured_slo_does_not_report_itself_as_met():
    from examlops.slo import apply_spec, slo_status

    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99})
    (st,) = slo_status("JPCP")

    assert st.n == 0
    assert st.measured is False
    assert st.ok is None, "zero samples must not be reported as meeting the target"


def test_the_status_table_says_no_data_rather_than_ok():
    from typer.testing import CliRunner

    from examlops.cli.main import app
    from examlops.slo import apply_spec

    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99})
    result = CliRunner().invoke(app, ["slo", "status", "JPCP"])

    assert result.exit_code == 0, result.output
    assert "NO DATA" in result.output, result.output
    assert "OK" not in result.output, result.output


def test_a_gate_flagged_slo_with_no_samples_is_reported_as_unevaluated():
    """The operator asked this SLO to gate promotion. It cannot, and that must not be silent."""
    from examlops.slo import apply_spec, unmeasured_gates

    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99, "gate_promotion": True})
    assert unmeasured_gates("JPCP") == ["quality"]


def test_an_slo_that_does_not_gate_is_not_reported_as_an_unevaluated_gate():
    from examlops.slo import apply_spec, unmeasured_gates

    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99, "gate_promotion": False})
    assert unmeasured_gates("JPCP") == []


# ── controls: measured SLOs keep their existing verdicts exactly ─────────────────────────


def test_a_measured_slo_meeting_its_target_still_reports_ok():
    from examlops import platform_db
    from examlops.slo import apply_spec, slo_status

    apply_spec({"model": "JPCP", "name": "quality", "target": 0.90})
    platform_db.record_slo_sample("JPCP", "quality", good=99, total=100)
    (st,) = slo_status("JPCP")

    assert st.measured is True
    assert st.ok is True
    assert st.n == 100


def test_a_measured_slo_breaching_its_target_still_reports_not_ok():
    from examlops import platform_db
    from examlops.slo import apply_spec, budget_exhausted, slo_status

    apply_spec({"model": "JPCP", "name": "quality", "target": 0.99})
    platform_db.record_slo_sample("JPCP", "quality", good=50, total=100)
    (st,) = slo_status("JPCP")

    assert st.ok is False
    assert budget_exhausted("JPCP", "quality") is True
