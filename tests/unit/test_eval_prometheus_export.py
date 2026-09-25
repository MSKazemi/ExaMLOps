"""ADR 0007 decision 3 — eval results reach Prometheus.

`eval_suite_results` had writers and a CLI reader, and no series. The exposition now publishes the
latest score per (suite, model, alias, metric), its Wilson bounds when it is a proportion, the
sample size and the record time (so a suite that stopped running reads as stale, not healthy).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.telemetry import exposition  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    from examlops import platform_db

    platform_db.init_db()


def _record(suite, model, alias, scores, run_id, n=10, **kw):
    from examlops.data.evaluation import record_eval_result

    record_eval_result(suite, model, scores, run_id=run_id, alias=alias, sample_size=n, **kw)


def _series(text):
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def test_latest_score_per_series_with_interval_and_timestamp():
    _record("live", "JPCP", "Production", {"accuracy": 0.5}, "r1")
    _record("live", "JPCP", "Production", {"accuracy": 0.9}, "r2")  # same second: id breaks tie
    text = exposition.export()
    lines = _series(text)
    labels = 'alias="Production",metric="accuracy",model="JPCP",suite="live",tenant="default"'
    assert f"examlops_eval_score{{{labels}}} 0.9" in lines
    assert any(ln.startswith(f"examlops_eval_score_lower{{{labels}}}") for ln in lines)
    assert any(ln.startswith(f"examlops_eval_score_upper{{{labels}}}") for ln in lines)
    assert f"examlops_eval_sample_size{{{labels}}} 10.0" in lines
    assert any(
        ln.startswith(f"examlops_eval_last_run_timestamp_seconds{{{labels}}}") for ln in lines
    )
    assert sum(ln.startswith("examlops_eval_score{") for ln in lines) == 1  # one series, not a run


def test_unit_metrics_get_no_bounds_and_families_are_declared_once():
    _record("live", "A", "Production", {"mae": 3.2}, "r1", non_proportion_metrics={"mae"})
    _record("live", "B", None, {"accuracy": 0.7}, "r1")
    text = exposition.export()
    assert text.count("# TYPE examlops_eval_score gauge") == 1
    assert "examlops_eval_score_lower{" in text  # B's proportion
    assert 'examlops_eval_score_lower{alias="Production",metric="mae"' not in text
    assert 'alias="",metric="accuracy",model="B"' in text
    # every sample of a family follows its own HELP/TYPE block (parseable exposition)
    current = None
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            current = line.split()[2]
        elif line and not line.startswith("#"):
            name = line.split("{")[0]
            assert name == current or name.rsplit("_", 1)[0] == current, line


def test_model_filter_and_empty_store():
    assert exposition.eval_samples() == []
    _record("s", "A", "Production", {"m": 1.0}, "r")
    _record("s", "B", "Production", {"m": 0.0}, "r")
    assert {s[1]["model"] for s in exposition.eval_samples("A")} == {"A"}


def test_an_unreadable_eval_store_does_not_blank_the_other_sources(monkeypatch):
    import examlops.data.evaluation as de

    def boom(*a, **k):
        raise RuntimeError("locked")

    monkeypatch.setattr(de, "latest_eval_scores", boom)
    assert exposition.export() == ""  # nothing else recorded — but no exception either


def test_the_model_filter_is_applied_before_the_series_bound():
    """`eval_samples(model)` used to filter the bounded (500-series) result in Python.

    With more than ``limit`` series ahead of it in sort order, the requested model's series was
    cut by the LIMIT and the filter then returned nothing: an absent gauge, not an error.
    """
    from examlops.data.evaluation import latest_eval_scores

    for i in range(5):
        _record("s", f"A{i}", "Production", {"m": 0.5}, "r")
    _record("s", "Z", "Production", {"m": 0.25}, "r")
    rows = latest_eval_scores(limit=2, model="Z")
    assert [r["model"] for r in rows] == ["Z"]
    assert latest_eval_scores(limit=2)[0]["model"] == "A0"  # the bound itself still holds


def test_export_is_tenant_scoped():
    _record("s", "M", "Production", {"m": 0.9}, "r", tenant="acme")
    _record("s", "M", "Production", {"m": 0.1}, "r", tenant="default")
    default = exposition.export()
    acme = exposition.export(tenant="acme")
    assert 'tenant="default"} 0.1' in default and 'tenant="acme"' not in default
    assert 'tenant="acme"} 0.9' in acme and 'tenant="default"' not in acme
