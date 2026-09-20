"""Autoscale controller — executes decide_scale (ADR 0031 clauses 1/4/5).

Real ``decide_scale`` + real platform.db; only the signal source, the applier and the clock are
fakes. Nothing here talks to Prometheus or Ray.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.autoscale import controller as ctl  # noqa: E402
from examlops.autoscale.controller import (  # noqa: E402
    AutoscaleController,
    RayServeApplier,
    RecordApplier,
    ScaleApplyError,
    Signals,
    SignalSourceDown,
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_ENABLED", "1")
    monkeypatch.delenv("EXAMLOPS_AUTOSCALE_MAX_CHANGES", raising=False)
    from examlops import platform_db
    from examlops.coordination import reset_coordinator

    reset_coordinator()
    platform_db.init_db()


class FakeSignals:
    def __init__(self, **by_model):
        self.by_model = by_model

    def read(self, model, policy):
        v = self.by_model.get(model, Signals())
        if isinstance(v, Exception):
            raise v
        return v


class FakeApplier:
    name = "fake"

    def __init__(self, replicas=None, fail=False):
        self.replicas = dict(replicas or {})
        self.fail = fail
        self.calls = []

    def current_replicas(self, model):
        return self.replicas.get(model)

    def apply(self, model, from_replicas, to_replicas):
        self.calls.append((model, from_replicas, to_replicas))
        if self.fail:
            raise ScaleApplyError("boom")
        self.replicas[model] = to_replicas


def _policy(model, **kw):
    from examlops.autoscale import set_policy

    base = dict(
        min_replicas=1,
        max_replicas=8,
        target_metric="rps",
        target_value=10,
        stabilization_s=30,
        cooldown_s=60,
    )
    set_policy(model, **{**base, **kw})


def _events(model):
    from examlops.data.serving import list_scale_events

    return list_scale_events(model, last_n=50)


def _audit_actions():
    from examlops.data.audit import export_audit_events

    return [e["action"] for e in export_audit_events()]


def _mk(signals, applier, **kw):
    return AutoscaleController(signals, applier, dry_run=kw.pop("dry_run", False), **kw)


def test_scale_up_applies_and_records():
    _policy("m")
    ap = FakeApplier({"m": 1})
    rep = _mk(FakeSignals(m=Signals(rps=45)), ap).run_cycle()
    assert rep.count("applied") == 1
    assert ap.calls == [("m", 1, 5)]  # ceil(45/10)
    assert _events("m")[0]["to_replicas"] == 5
    assert "autoscale_event" in _audit_actions()


def test_scale_down_applies():
    _policy("m")
    ap = FakeApplier({"m": 5})
    rep = _mk(FakeSignals(m=Signals(rps=5)), ap).run_cycle()
    assert rep.results[0].outcome == "applied" and ap.calls == [("m", 5, 1)]


def test_hold_at_target_is_steady_and_calls_nothing():
    _policy("m")
    ap = FakeApplier({"m": 5})
    rep = _mk(FakeSignals(m=Signals(rps=45)), ap).run_cycle()
    assert rep.results[0].outcome == "steady" and ap.calls == []


def test_anti_thrash_across_cycles():
    _policy("m")
    ap = FakeApplier({"m": 1})
    sig = FakeSignals(m=Signals(rps=45))
    now = time.time()
    c = _mk(sig, ap, clock=lambda: now)
    assert c.run_cycle().count("applied") == 1  # 1 -> 5, event stamped "now"
    sig.by_model["m"] = Signals(rps=200)  # wants 8 but the change was just made
    c.clock = lambda: now + 5
    r = c.run_cycle().results[0]
    assert r.outcome == "held" and "stabilization" in r.reason
    sig.by_model["m"] = Signals(rps=5)  # scale-down inside cooldown (past stabilization)
    c.clock = lambda: now + 45
    r = c.run_cycle().results[0]
    assert r.outcome == "held" and "cooldown" in r.reason
    c.clock = lambda: now + 500
    assert c.run_cycle().results[0].outcome == "applied"
    assert len(ap.calls) == 2


def test_kill_switch_off_refuses_real_apply(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUTOSCALE_ENABLED")
    _policy("m")
    ap = FakeApplier({"m": 1})
    rep = _mk(FakeSignals(m=Signals(rps=45)), ap).run_cycle()
    assert not rep.ran and ap.calls == [] and _events("m") == []
    assert "autoscale_skipped" in _audit_actions()


def test_dry_run_needs_no_switch_changes_nothing_but_audits(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUTOSCALE_ENABLED")
    _policy("m")
    ap = FakeApplier({"m": 1})
    rep = _mk(FakeSignals(m=Signals(rps=45)), ap, dry_run=True).run_cycle()
    assert rep.results[0].outcome == "dry_run" and rep.results[0].to_replicas == 5
    assert ap.calls == [] and _events("m") == []
    assert "autoscale_dry_run" in _audit_actions()


def test_storm_cap_limits_changes_per_cycle():
    for m in ("a", "b", "c"):
        _policy(m)
    ap = FakeApplier({"a": 1, "b": 1, "c": 1})
    sig = FakeSignals(a=Signals(rps=45), b=Signals(rps=45), c=Signals(rps=45))
    rep = _mk(sig, ap, cap=2).run_cycle()
    assert rep.count("applied") == 2 and rep.count("refused") == 1
    assert len(ap.calls) == 2 and "autoscale_refused" in _audit_actions()


def test_lease_blocks_second_controller():
    from examlops.coordination import get_coordinator

    _policy("m")
    assert get_coordinator().try_lock(ctl.LEASE_KEY, "other-host:1", 60)
    ap = FakeApplier({"m": 1})
    rep = _mk(FakeSignals(m=Signals(rps=45)), ap).run_cycle()
    assert not rep.ran and "lease" in rep.note and ap.calls == []


def test_lease_released_after_cycle():
    from examlops.coordination import get_coordinator

    _policy("m")
    c = _mk(FakeSignals(m=Signals(rps=1)), FakeApplier({"m": 1}))
    c.run_cycle()
    assert get_coordinator().try_lock(ctl.LEASE_KEY, "someone-else", 60)


def test_absent_signal_holds_never_zero():
    _policy("m", target_metric="queue_depth")  # no source exists for it
    ap = FakeApplier({"m": 4})
    rep = _mk(FakeSignals(m=Signals(rps=0.0)), ap).run_cycle()
    r = rep.results[0]
    assert r.outcome == "held" and "absent" in r.reason and ap.calls == []
    assert "autoscale_held" in _audit_actions()


def test_signal_source_down_holds_and_is_audited_once():
    _policy("m")
    ap = FakeApplier({"m": 4})
    c = _mk(FakeSignals(m=SignalSourceDown("prometheus down")), ap)
    assert c.run_cycle().results[0].outcome == "held"
    c.run_cycle()
    assert ap.calls == []
    assert _audit_actions().count("autoscale_held") == 1  # deduped, not one per interval


def test_unknown_current_replicas_holds():
    _policy("m")
    rep = _mk(FakeSignals(m=Signals(rps=45)), FakeApplier({})).run_cycle()
    assert rep.results[0].outcome == "held" and "unknown" in rep.results[0].reason


def test_applier_failure_counted_retried_no_event_no_crash():
    _policy("m")
    ap = FakeApplier({"m": 1}, fail=True)
    c = _mk(FakeSignals(m=Signals(rps=45)), ap)
    assert c.run_cycle().results[0].outcome == "failed"
    assert c.run_cycle().results[0].outcome == "failed"  # retried next cycle
    assert c.consecutive_failures["m"] == 2 and _events("m") == []
    assert "autoscale_apply_failed" in _audit_actions()
    ap.fail = False
    assert c.run_cycle().results[0].outcome == "applied"
    assert "m" not in c.consecutive_failures


def test_run_forever_survives_a_crashing_cycle():
    _policy("m")
    c = _mk(FakeSignals(), FakeApplier())
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("x")

    c.run_cycle = boom  # type: ignore[method-assign]
    assert c.run_forever(max_cycles=3, sleep=lambda s: None) == 3 and len(calls) == 3


def test_scale_to_zero_needs_proven_idle():
    _policy("m", min_replicas=1, scale_to_zero_after_s=300, stabilization_s=0, cooldown_s=0)
    ap = FakeApplier({"m": 2})
    # rps reads 0 but idleness over the window is NOT proven: decide_scale alone would go to 0.
    rep = _mk(FakeSignals(m=Signals(rps=0.0, idle_confirmed=None)), ap).run_cycle()
    assert rep.results[0].outcome == "refused" and ap.calls == []
    rep = _mk(FakeSignals(m=Signals(rps=0.0, idle_confirmed=False)), ap).run_cycle()
    assert rep.results[0].outcome == "refused" and ap.calls == []
    rep = _mk(FakeSignals(m=Signals(rps=0.0, idle_confirmed=True)), ap).run_cycle()
    assert rep.results[0].outcome == "applied" and ap.calls == [("m", 2, 0)]
    assert _events("m")[0]["to_replicas"] == 0


def test_no_scale_to_zero_when_policy_forbids():
    _policy("m", min_replicas=2, stabilization_s=0, cooldown_s=0)
    ap = FakeApplier({"m": 3})
    _mk(FakeSignals(m=Signals(rps=0.0, idle_confirmed=True)), ap).run_cycle()
    assert ap.calls == [("m", 3, 2)]  # floors at min


def test_record_applier_uses_ledger_and_ray_applier_refuses():
    _policy("m")
    assert RecordApplier().current_replicas("m") is None
    from examlops.autoscale import ScaleDecision, apply_scale

    apply_scale("m", 1, ScaleDecision(3, 1, "seed", True))
    assert RecordApplier().current_replicas("m") == 3
    rep = _mk(
        FakeSignals(m=Signals(rps=100)), RayServeApplier(), clock=lambda: time.time() + 1e4
    ).run_cycle()
    r = rep.results[0]
    assert r.outcome == "failed" and "not built" in r.reason
    assert _events("m")[0]["reason"] == "seed"  # nothing pretended to happen


def test_prometheus_signals_maps_queries_and_absent_is_none():
    seen = []

    def q(expr):
        seen.append(expr)
        return None if "histogram_quantile" in expr else 0.0

    from examlops.autoscale import AutoscalePolicy

    s = ctl.PrometheusSignals(q).read("JPCP", AutoscalePolicy(scale_to_zero_after_s=120))
    assert s.rps == 0.0 and s.p95 is None and s.idle_confirmed is True
    assert any("examlops_predict_requests_total" in e and "(?i)^JPCP$" in e for e in seen)
    assert s.queue_depth is None and s.gpu_util is None
    s2 = ctl.PrometheusSignals(lambda e: None).read("m", AutoscalePolicy(scale_to_zero_after_s=9))
    assert s2.idle_confirmed is None  # no series is not "no traffic"


def test_prometheus_signals_rejects_unsafe_model_name():
    called = []
    s = ctl.PrometheusSignals(lambda e: called.append(e) or 1.0).read(
        'x"} or vector(1) #', __import__("examlops.autoscale").autoscale.AutoscalePolicy()
    )
    assert s.rps is None and not called


def test_prometheus_down_raises_source_down(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_PROBE_TIMEOUT", "1")
    with pytest.raises(SignalSourceDown):
        ctl.PrometheusSignals()._query("up")


def test_kill_switch_default_off(monkeypatch):
    monkeypatch.delenv("EXAMLOPS_AUTOSCALE_ENABLED")
    assert ctl.is_enabled() is False


def test_cli_run_once_dry_run(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.commands import autoscale_cmd

    monkeypatch.setenv("PROMETHEUS_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_PROBE_TIMEOUT", "1")
    _policy("m")
    out = CliRunner().invoke(autoscale_cmd.app, ["run", "--once"])
    assert out.exit_code == 0, out.output
    assert "dry run" in out.output and "held" in out.output  # Prometheus down -> hold


def test_a_lost_autoscale_audit_is_counted(monkeypatch):
    from examlops.data import audit

    audit.reset_dropped_audit_events()

    def _boom(*a, **k):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit, "write_audit_event", _boom)
    _policy("m")
    ap = FakeApplier({"m": 1})
    rep = _mk(FakeSignals(m=Signals(rps=45)), ap, dry_run=True).run_cycle()
    assert rep.results[0].outcome == "dry_run"  # the loss does not stop the cycle...
    assert audit.dropped_audit_events().get("autoscale_dry_run") == 1  # ...and is not hidden
