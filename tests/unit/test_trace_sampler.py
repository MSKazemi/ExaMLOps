"""Trace sampler defaults to a bounded parent-based ratio (Phase 0 item 0.2 / QW4).

A manually-built TracerProvider samples every trace by default; at fleet scale that firehoses Tempo
the moment tracing is enabled. `observability._build_sampler()` defaults to `parentbased_traceidratio`
at 5% and honors the standard `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` overrides.
"""

from __future__ import annotations

from examlops import observability


def test_default_is_parentbased_5pct(monkeypatch):
    monkeypatch.delenv("OTEL_TRACES_SAMPLER", raising=False)
    monkeypatch.delenv("OTEL_TRACES_SAMPLER_ARG", raising=False)
    desc = observability._build_sampler().get_description()
    assert desc.startswith("ParentBased")
    assert "TraceIdRatioBased{0.05}" in desc  # not 1.0 — bounded by default


def test_env_overrides_ratio(monkeypatch):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.5")
    assert "TraceIdRatioBased{0.5}" in observability._build_sampler().get_description()


def test_env_selects_always_on_for_debug(monkeypatch):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    assert observability._build_sampler().get_description() == "AlwaysOnSampler"


def test_bad_arg_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "parentbased_traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "not-a-number")
    assert "TraceIdRatioBased{0.05}" in observability._build_sampler().get_description()


def test_ratio_clamped_to_unit_interval(monkeypatch):
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "traceidratio")
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "9.0")
    assert "TraceIdRatioBased{1.0}" in observability._build_sampler().get_description()
