"""ADR 0031 — signal sources (queue_depth via Little's law, gpu_util via an operator template) and
GPU-aware packing in the controller (clause 4, E3 fractions).

Real ``decide_scale``, real platform.db, real controller; only Prometheus and the applier are fakes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.autoscale import AutoscalePolicy, set_policy  # noqa: E402
from examlops.autoscale import controller as ctl  # noqa: E402
from examlops.autoscale.controller import (  # noqa: E402
    AutoscaleController,
    PrometheusSignals,
    Signals,
)
from examlops.autoscale.manifests import (  # noqa: E402
    ManifestError,
    render_keda_scaledobject,
    render_knative_overlay,
)
from examlops.autoscale.queries import QueryTemplateError, query_for, sourced_metrics  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_ENABLED", "1")
    monkeypatch.setenv("RAY_MODELS_DIR", str(tmp_path / "no-models"))
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(tmp_path / "no-pack"))
    for var in (
        "EXAMLOPS_AUTOSCALE_MAX_CHANGES",
        "EXAMLOPS_AUTOSCALE_GPU_CAPACITY",
        "EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY",
        "EXAMLOPS_AUTOSCALE_QUEUE_DEPTH_QUERY",
    ):
        monkeypatch.delenv(var, raising=False)
    from examlops import platform_db
    from examlops.coordination import reset_coordinator

    reset_coordinator()
    platform_db.init_db()


# ─── queries ────────────────────────────────────────────────────────────────


def test_queue_depth_is_the_latency_sum_rate_and_gpu_util_has_no_default():
    assert query_for("queue_depth", "JPCP") == (
        'sum(rate(examlops_predict_latency_seconds_sum{model_name=~"(?i)^JPCP$"}[1m]))'
    )
    assert query_for("gpu_util", "JPCP") is None
    assert sourced_metrics() == ("rps", "p95", "queue_depth")


def test_gpu_util_template_is_substituted_and_escaped(monkeypatch):
    monkeypatch.setenv(
        "EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY",
        'avg(DCGM_FI_DEV_GPU_UTIL{pod=~"{k8s_name}-predictor-.*",model="{model}"})',
    )
    q = query_for("gpu_util", "My.Model_1")
    # {model} is the regex-escaped name with its backslash doubled for PromQL's Go-style string
    # literal (a bare backslash-dot is an unknown escape the Prometheus lexer rejects).
    assert q == 'avg(DCGM_FI_DEV_GPU_UTIL{pod=~"my-model-1-predictor-.*",model="My\\\\.Model_1"})'
    assert "gpu_util" in sourced_metrics()


@pytest.mark.parametrize("tpl", ["avg(DCGM_FI_DEV_GPU_UTIL)", "x{model}" + "y" * 3000])
def test_template_without_a_model_or_oversized_is_refused(monkeypatch, tpl):
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY", tpl)
    with pytest.raises(QueryTemplateError):
        query_for("gpu_util", "JPCP")
    # the generator refuses rather than rendering a fleet-wide trigger
    with pytest.raises(ManifestError):
        render_keda_scaledobject("JPCP", AutoscalePolicy(target_metric="gpu_util"))


def test_unsafe_model_name_has_no_query():
    assert query_for("rps", 'x"} or vector(1) #') is None


def test_prometheus_reads_queue_depth_and_template_error_is_source_down(monkeypatch):
    seen: list[str] = []

    def q(expr):
        seen.append(expr)
        return 12.5 if "latency_seconds_sum" in expr else 1.0

    s = PrometheusSignals(q).read("JPCP", AutoscalePolicy())
    assert s.queue_depth == 12.5 and s.gpu_util is None and s.rps == 1.0
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY", "no model placeholder")
    with pytest.raises(ctl.SignalSourceDown):
        PrometheusSignals(q).read("JPCP", AutoscalePolicy())


def test_controller_scales_on_queue_depth_end_to_end():
    set_policy("m", min_replicas=1, max_replicas=8, target_metric="queue_depth", target_value=4)

    class Ap:
        name = "fake"
        replicas = {"m": 1}

        def current_replicas(self, model):
            return self.replicas.get(model)

        def apply(self, model, f, t):
            self.replicas[model] = t

    ap = Ap()
    prom = PrometheusSignals(lambda e: 18.0 if "latency_seconds_sum" in e else 3.0)
    rep = AutoscaleController(prom, ap, dry_run=False).run_cycle()
    assert rep.results[0].outcome == "applied" and ap.replicas["m"] == 5  # ceil(18/4)


def test_knative_maps_queue_depth_to_concurrency_and_refuses_gpu_util():
    doc = render_knative_overlay("JPCP", AutoscalePolicy(target_metric="queue_depth"))
    assert doc["metadata"]["annotations"]["autoscaling.knative.dev/metric"] == "concurrency"
    with pytest.raises(ManifestError):
        render_knative_overlay("JPCP", AutoscalePolicy(target_metric="gpu_util"))


# ─── GPU-aware packing ──────────────────────────────────────────────────────


class FakeSignals:
    def __init__(self, **by):
        self.by = by

    def read(self, model, policy):
        return self.by.get(model, Signals())


class FakeApplier:
    name = "fake"

    def __init__(self, replicas):
        self.replicas = dict(replicas)
        self.calls: list[tuple[str, int, int]] = []

    def current_replicas(self, model):
        return self.replicas.get(model)

    def apply(self, model, f, t):
        self.calls.append((model, f, t))
        self.replicas[model] = t


def _audit(action):
    from examlops.data.audit import export_audit_events

    return [e for e in export_audit_events() if e["action"] == action]


def test_gpu_capacity_refuses_a_scale_up_that_does_not_fit(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "2")
    for m in ("a", "b"):
        set_policy(
            m,
            min_replicas=1,
            max_replicas=8,
            target_metric="rps",
            target_value=10,
            gpu_fraction=0.5,
        )
    # a: 1 -> 3 (+1.0 GPU), b: 1 -> 4 (+1.5 GPU). Committed 1.0; a fits (2.0), b does not.
    ap = FakeApplier({"a": 1, "b": 1})
    rep = AutoscaleController(
        FakeSignals(a=Signals(rps=25), b=Signals(rps=35)), ap, dry_run=False
    ).run_cycle()
    by = {r.model: r for r in rep.results}
    assert by["a"].outcome == "applied" and ap.replicas["a"] == 3
    assert by["b"].outcome == "refused" and "GPU capacity" in by["b"].reason
    assert ap.replicas["b"] == 1 and ("b", 1, 4) not in ap.calls
    assert any(e["target"] == "b" for e in _audit("autoscale_refused"))


def test_scale_down_is_always_allowed_under_capacity_pressure(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "1")
    set_policy("a", min_replicas=1, max_replicas=8, target_metric="rps", target_value=10)
    ap = FakeApplier({"a": 6})  # already over capacity (6 GPUs committed)
    rep = AutoscaleController(FakeSignals(a=Signals(rps=15)), ap, dry_run=False).run_cycle()
    assert rep.results[0].outcome == "applied" and ap.replicas["a"] == 2


def test_unknown_replicas_count_at_max_and_bad_capacity_fails_closed(monkeypatch):
    set_policy(
        "a", min_replicas=1, max_replicas=8, target_metric="rps", target_value=10, gpu_fraction=0.25
    )
    set_policy("ghost", min_replicas=1, max_replicas=4, target_metric="rps", target_value=10)
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "4.5")
    ap = FakeApplier({"a": 1})  # ghost unknown -> counted as 4 GPUs; a: 0.25 committed
    rep = AutoscaleController(FakeSignals(a=Signals(rps=30)), ap, dry_run=False).run_cycle()
    a = next(r for r in rep.results if r.model == "a")
    assert a.outcome == "refused"  # +0.5 on 4.25 > 4.5
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "lots")
    assert ctl.gpu_capacity() == 0.0
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "-3")
    assert ctl.gpu_capacity() == 0.0


def test_dry_run_packs_like_the_real_cycle(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "3")
    for m in ("a", "b"):
        set_policy(m, min_replicas=1, max_replicas=8, target_metric="rps", target_value=10)
    ap = FakeApplier({"a": 1, "b": 1})
    rep = AutoscaleController(
        FakeSignals(a=Signals(rps=20), b=Signals(rps=20)),
        ap,
        dry_run=True,
        clock=lambda: time.time(),
    ).run_cycle()
    outcomes = sorted(r.outcome for r in rep.results)
    assert outcomes == ["dry_run", "refused"] and ap.calls == []


def test_no_capacity_configured_is_unbounded():
    set_policy("a", min_replicas=1, max_replicas=8, target_metric="rps", target_value=1)
    ap = FakeApplier({"a": 1})
    rep = AutoscaleController(FakeSignals(a=Signals(rps=8)), ap, dry_run=False).run_cycle()
    assert rep.results[0].outcome == "applied" and ap.replicas["a"] == 8
