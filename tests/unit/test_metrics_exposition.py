# tests/unit/test_metrics_exposition.py
"""Prometheus exposition of platform-recorded metrics — ADR 0023, 0020 clause 5, 0025 clause 3.

The platform ingests SLIs into `slo_samples` and records vector latency into `vector_metrics`, and
neither reached Prometheus. That is not merely a missing dashboard: `exa slo generate` emits
burn-rate rules that range over a **Prometheus series**, so every SLI the platform ingests itself
(`c2` eval, `c5` drift, `c8` fairness) had an alert that could never fire. ADR 0025 clause 3 asks
for "a C6 fairness SLI **and alert**" — iteration 18 landed the SLI, and the alert had nowhere to
fire from.
"""

from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.cli.commands import slo_cmd  # noqa: E402
from examlops.slo import apply_spec, record_sample  # noqa: E402
from examlops.telemetry import exposition  # noqa: E402

runner = CliRunner()


def _slo(model: str, name: str, *, target: float = 0.9) -> None:
    apply_spec(
        {"model": model, "name": name, "target": target, "window": "30d", "sli_source": "c8"}
    )


# ── the text format ───────────────────────────────────────────────────────────


def test_help_and_type_are_emitted_once_per_family():
    """Repeating them per sample makes the file unparseable, not merely verbose."""
    text = exposition.render(
        [
            ("examlops_slo_sli", {"model": "a"}, 0.9),
            ("examlops_slo_sli", {"model": "b"}, 0.8),
        ]
    )
    assert text.count("# HELP examlops_slo_sli") == 1
    assert text.count("# TYPE examlops_slo_sli") == 1


def test_labels_are_sorted_and_quoted():
    line = exposition.render([("m", {"b": "2", "a": "1"}, 1.0)]).strip().splitlines()[-1]
    assert line == 'm{a="1",b="2"} 1.0'


def test_a_label_value_with_quotes_is_escaped():
    """An unescaped quote silently truncates the series name Prometheus reads."""
    line = exposition.render([("m", {"k": 'a"b'}, 1.0)]).strip().splitlines()[-1]
    assert '\\"' in line


def test_no_labels_renders_without_braces():
    assert exposition.render([("m", {}, 2.0)]).strip().endswith("m 2.0")


def test_a_none_label_is_dropped_not_rendered_as_none():
    line = exposition.render([("m", {"a": "1", "b": None}, 1.0)]).strip().splitlines()[-1]
    assert "None" not in line


def test_an_empty_sample_list_renders_nothing():
    assert exposition.render([]) == ""


def test_every_family_has_help_text():
    """A metric nobody can interpret is barely better than one nobody exports."""
    for metric, (kind, help_text) in exposition._HELP.items():
        assert kind in ("gauge", "counter", "histogram")
        assert help_text and help_text != metric


# ── SLO gauges ────────────────────────────────────────────────────────────────


def test_a_measured_slo_exports_its_sli_and_budget():
    _slo("ExpA", "fair")
    record_sample("ExpA", "fair", 8, 10)

    text = exposition.export("ExpA")

    assert 'examlops_slo_sli{model="ExpA",slo="fair",tenant="default"} 0.8' in text
    assert "examlops_slo_budget_remaining" in text
    assert "examlops_slo_burn_rate" in text


def test_an_unmeasured_slo_exports_no_sli(monkeypatch):
    """Its placeholder 1.0 would put a perfect ratio on a dashboard for something nobody
    measured — and a burn-rate alert cannot fire on a perfect ratio."""
    _slo("ExpB", "fair")

    text = exposition.export("ExpB")

    assert 'examlops_slo_measured{model="ExpB",slo="fair",tenant="default"} 0.0' in text
    assert "examlops_slo_sli{" not in text


def test_the_target_is_exported_even_when_unmeasured():
    """The objective is a declared fact; only the observation is missing."""
    _slo("ExpC", "fair", target=0.95)
    assert "examlops_slo_target" in exposition.export("ExpC")


def test_the_sample_count_is_exported():
    _slo("ExpD", "fair")
    record_sample("ExpD", "fair", 5, 5)
    assert (
        'examlops_slo_samples{model="ExpD",slo="fair",tenant="default"} 5.0'
        in exposition.export("ExpD")
    )


def test_every_declared_slo_is_exported_when_no_model_is_named():
    _slo("ExpE", "one")
    _slo("ExpF", "two")
    text = exposition.export()
    assert 'model="ExpE"' in text and 'model="ExpF"' in text


# ── vector gauges (ADR 0020 clause 5) ─────────────────────────────────────────


def _vec(collection: str, operation: str, latency: float, items: int) -> None:
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO vector_metrics (collection, tenant, operation, latency_ms, item_count) "
            "VALUES (?,?,?,?,?)",
            (collection, "default", operation, latency, items),
        )


def test_vector_latency_and_items_are_exported():
    _vec("expcoll", "upsert", 12.5, 100)
    text = exposition.export()
    assert "examlops_vector_latency_ms" in text
    assert 'collection="expcoll"' in text


def test_only_the_latest_row_per_series_is_exported():
    """An append-only log averaged into a gauge moves less and less as it grows — the opposite
    of what an operator watching a reindex needs."""
    _vec("expcoll2", "search", 100.0, 1)
    _vec("expcoll2", "search", 5.0, 2)

    text = exposition.export()

    assert "examlops_vector_latency_ms" in text
    lines = [ln for ln in text.splitlines() if 'collection="expcoll2"' in ln and "latency" in ln]
    assert len(lines) == 1
    assert lines[0].endswith("5.0")


# ── resilience ────────────────────────────────────────────────────────────────


def test_one_unreadable_source_does_not_blank_the_export(monkeypatch):
    """A textfile collector that vanishes takes every series with it."""
    _slo("ExpG", "fair")
    record_sample("ExpG", "fair", 9, 10)
    monkeypatch.setattr(
        exposition,
        "vector_samples",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("table gone")),
    )

    text = exposition.export("ExpG")

    assert "examlops_slo_sli" in text, "the SLO gauges must survive a broken vector table"


def test_an_export_with_nothing_to_say_is_empty_not_broken(monkeypatch):
    monkeypatch.setattr(exposition, "slo_samples", lambda *a, **k: [])
    monkeypatch.setattr(exposition, "vector_samples", lambda *a, **k: [])
    assert exposition.export() == ""


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_the_cli_writes_a_prom_file(tmp_path):
    _slo("ExpH", "fair")
    record_sample("ExpH", "fair", 7, 10)
    out = tmp_path / "examlops.prom"

    result = runner.invoke(slo_cmd.app, ["export-metrics", "--model", "ExpH", "--out", str(out)])

    assert result.exit_code == 0
    assert "examlops_slo_sli" in out.read_text()
    assert "node_exporter" in result.output


def test_the_cli_says_so_when_there_is_nothing_to_export(monkeypatch):
    monkeypatch.setattr("examlops.telemetry.exposition.export", lambda *a, **k: "")
    result = runner.invoke(slo_cmd.app, ["export-metrics"])
    assert result.exit_code == 0
    assert "No metrics to export" in result.output


# ── the reason this exists ────────────────────────────────────────────────────


def test_a_platform_ingested_sli_can_now_back_its_generated_alert():
    """`exa slo generate` emits burn-rate rules over a Prometheus series. Before this export,
    a c8/c5/c2 SLO had rules that could never fire because the series did not exist."""
    from examlops.data.governance import get_slo_spec
    from examlops.slo import generate_rules

    _slo("ExpI", "fair")
    record_sample("ExpI", "fair", 5, 10)

    rules = generate_rules(get_slo_spec("ExpI", "fair", "default"))
    exported = exposition.export("ExpI")

    assert any("alert" in str(g).lower() for g in rules.groups), "alerts are generated"
    assert 'examlops_slo_sli{model="ExpI"' in exported, "and the series they need now exists"
