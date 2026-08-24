"""A run that used no accelerator was accounted at exactly zero emissions.

Green-AI accounting took one input, `gpu_hours`, and every built-in provider multiplied by it. A
CPU-only job therefore came out at 0.000 kWh / 0.0 gCO2e — the best possible figure, published with
an uncertainty band, for work that really happened. This is not a corner case for this platform:
`exa models cost` already reads `(gpu_hours, cpu_hours)` per job from Flux, prices both through the
cost provider, and then stored only the GPU half; and the sites this runs on include clusters whose
nodes carry no accelerators at all.

These tests pin the three properties that keep the number honest: CPU-hours are counted, a provider
that cannot count them refuses instead of dropping them, and the GPU-only arithmetic is untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.finops import carbon  # noqa: E402


def test_cpu_only_work_has_a_carbon_figure_at_all():
    est = carbon.estimate_carbon_via_provider(0.0, cpu_hours=32.0)
    assert est["kwh"] > 0, "32 CPU-core-hours of real work cannot be 0 kWh"
    assert est["co2e_g"] > 0
    # 32 core-h × 120 W × PUE 1.5 = 5.76 kWh; × 300 gCO2e/kWh = 1728 g.
    assert est["kwh"] == pytest.approx(5.76)
    assert est["co2e_g"] == pytest.approx(1728.0)


def test_the_gpu_only_number_is_exactly_what_it_always_was():
    """The control. Adding a term must not move any figure a GPU site has already reported."""
    assert carbon.estimate_energy_kwh(10.0) == pytest.approx(10.0 * 0.4 * 1.5)
    legacy = carbon.estimate_carbon(10.0)
    assert legacy == {"kwh": pytest.approx(6.0), "co2e_g": pytest.approx(1800.0)}
    via_provider = carbon.estimate_carbon_via_provider(10.0)
    assert via_provider["kwh"] == pytest.approx(6.0)
    assert via_provider["co2e_g"] == pytest.approx(1800.0)
    assert via_provider["provider"] == "green-ai-default"


def test_cpu_and_gpu_hours_add_rather_than_replace():
    both = carbon.estimate_energy_kwh(10.0, cpu_hours=32.0)
    assert both == pytest.approx(carbon.estimate_energy_kwh(10.0) + 32.0 * 0.12 * 1.5)


def test_a_provider_with_no_cpu_term_refuses_instead_of_dropping_the_input():
    """The failure mode this file is about, one level up: a smaller number, silently.

    `ccf-like` prices a per-GPU-hour energy coefficient and has no CPU term. Handed CPU-hours it
    would return the GPU-only answer — zero, here — and nothing would say the input was ignored.
    """
    with pytest.raises(carbon.CarbonInputUnaccounted) as caught:
        carbon.estimate_carbon_via_provider(0.0, cpu_hours=32.0, provider="ccf-like")
    assert "cpu_hours" in str(caught.value)
    assert "ccf-like" in str(caught.value)
    # Naming a provider that *can* answer is what makes the refusal actionable.
    assert "green-ai-default" in str(caught.value)


def test_the_same_provider_still_answers_a_gpu_only_question():
    """The refusal is about the input it was given, not a provider that stopped working."""
    est = carbon.estimate_carbon_via_provider(10.0, provider="ccf-like")
    assert est["kwh"] > 0
    assert est["provider"] == "ccf-like"


def test_the_default_provider_declares_the_term_it_now_has():
    """A provider's `params` is what the seam trusts; the term and the declaration move together."""
    from examlops.finops import carbon_providers  # noqa: F401 - registers the built-ins
    from examlops.providers import get_provider

    meta = get_provider("carbon", "green-ai-default").metadata()
    assert "cpu_hours" in meta.params
    assert "cpu_tdp_watts" in meta.params
    assert "CPU" in meta.methodology, "the published methodology must say what it counts"


def test_negative_cpu_hours_are_rejected_like_every_other_input():
    with pytest.raises(ValueError):
        carbon.estimate_energy_kwh(1.0, cpu_hours=-1.0)


# ── the two surfaces that publish the number ────────────────────────────────────────────


def _runner():
    from typer.testing import CliRunner

    return CliRunner()


def test_the_cli_refuses_to_record_a_run_it_was_told_nothing_about(tmp_path, monkeypatch):
    """`--gpu-hours` used to be required, so 0 was the only way to describe a CPU-only run.

    Recording that wrote a row asserting the run consumed no energy. Both options are optional now,
    and supplying neither is refused rather than stored.
    """
    from examlops.cli.commands import finops_cmd

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    result = _runner().invoke(finops_cmd.app, ["carbon", "record", "JPCP"])
    assert result.exit_code == 1
    assert "nothing to account for" in result.output


def test_the_cli_records_a_cpu_only_run(tmp_path, monkeypatch):
    from examlops.cli.commands import finops_cmd
    from examlops.data.finops import get_carbon_records

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    result = _runner().invoke(finops_cmd.app, ["carbon", "record", "JPCP", "--cpu-hours", "32"])
    assert result.exit_code == 0, result.output
    rows = get_carbon_records("JPCP")
    assert len(rows) == 1
    assert rows[0]["kwh"] > 0, "the stored record, not just the printed one, must be non-zero"


def test_the_measured_cpu_hours_are_stored_not_only_priced(tmp_path, monkeypatch):
    """Flux reports `(gpu_hours, cpu_hours)`; the cost path used both and kept only one."""
    from examlops.data.finops import get_model_costs, record_model_cost
    from examlops.platform_db import init_db

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    init_db()
    record_model_cost("JPCP", 17, "run-1", "f42", 0.0, 1.28, cpu_hours=32.0)
    rows = get_model_costs("JPCP")
    assert rows[0]["cpu_hours"] == 32.0
    assert rows[0]["gpu_hours"] == 0.0, "a GPU-less job is 0 GPU-hours and that part is honest"


def test_a_report_with_no_carbon_records_does_not_publish_a_zero(tmp_path, monkeypatch):
    """`0.000 kg CO2e` reads as an achievement; this report is the kind that reaches a funder."""
    from examlops import reporting

    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "t.db"))
    report = reporting.assemble_report(generated_at="2026-08-24T00:00:00Z")
    assert report["sections"]["carbon"]["records"] == 0
    assert report["sections"]["carbon"]["total_kg_co2e"] is None
    assert "not measured" in reporting.render_text(report)
