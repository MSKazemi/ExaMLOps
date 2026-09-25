"""ADR 0031 clause 2 — the cold-start activator, and its wiring into the inference router.

Real platform.db (scale events + audit), real policies; the applier, readiness probe and clock are
fakes so a cold start is deterministic.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.autoscale import cold_start_seconds, set_policy  # noqa: E402
from examlops.autoscale.activator import (  # noqa: E402
    Activator,
    ActivatorError,
    ActivatorOverloaded,
    ActivatorTimeout,
    http_ready_probe,
)
from examlops.autoscale.controller import ScaleApplyError  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("RAY_MODELS_DIR", str(tmp_path / "no-models"))
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(tmp_path / "no-pack"))
    monkeypatch.delenv("EXAMLOPS_AUTOSCALE_ACTIVATOR", raising=False)
    from examlops import platform_db

    platform_db.init_db()


class FakeApplier:
    name = "fake"

    def __init__(self, replicas, fail=False, apply_delay=0.0):
        self.replicas = dict(replicas)
        self.fail = fail
        self.apply_delay = apply_delay
        self.calls: list[tuple[str, int, int]] = []
        self._lock = threading.Lock()

    def current_replicas(self, model):
        return self.replicas.get(model)

    def apply(self, model, f, t):
        time.sleep(self.apply_delay)
        with self._lock:
            self.calls.append((model, f, t))
        if self.fail:
            raise ScaleApplyError("api down")
        self.replicas[model] = t


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.01)


def _stz(model="JPCP", **kw):
    base = dict(
        min_replicas=0,
        max_replicas=4,
        target_metric="rps",
        target_value=5,
        scale_to_zero_after_s=300,
        warm_pool=0,
    )
    set_policy(model, **{**base, **kw})


def _actions():
    from examlops.data.audit import export_audit_events

    return [(e["action"], e["target"]) for e in export_audit_events()]


def test_wakes_from_zero_measures_and_records_the_cold_start():
    _stz(warm_pool=2)
    clock = Clock()
    polls = {"n": 0}

    def probe(model):
        polls["n"] += 1
        return polls["n"] >= 4  # ready on the 4th probe

    ap = FakeApplier({"JPCP": 0})
    act = Activator(ap, probe=probe, clock=clock, sleep=clock.sleep, poll_s=0.5)
    res = act.ensure_warm("jpcp", timeout=30)  # router lower-cases; the policy is JPCP
    assert res.woke and res.replicas == 2 and ap.calls == [("JPCP", 0, 2)]
    assert res.cold_start_s == pytest.approx(1.5)
    assert cold_start_seconds("JPCP") == pytest.approx(1.5)  # surfaced for the C6 SLO
    assert ("autoscale_event", "JPCP") in _actions()


def test_warm_models_pass_through_and_are_cached():
    _stz()
    ap = FakeApplier({"JPCP": 3})
    act = Activator(ap, probe=lambda m: True, warm_ttl_s=60)
    assert not act.ensure_warm("JPCP", 5).woke
    ap.replicas["JPCP"] = 0  # within the TTL the hot path does not ask again
    assert act.ensure_warm("JPCP", 5).note == "warm (cached)"
    assert ap.calls == []


def test_absent_is_not_zero():
    ap = FakeApplier({})
    _stz()
    set_policy("FLOOR", min_replicas=1, max_replicas=2, target_metric="rps", target_value=1)
    act = Activator(ap, probe=lambda m: True)  # policies are read (TTL-cached) on first use
    assert act.ensure_warm("NOPOLICY", 5).note == "no autoscale policy"
    assert act.ensure_warm("JPCP", 5).note == "replicas unknown"  # never read as 0
    ap.replicas["FLOOR"] = 0
    assert act.ensure_warm("FLOOR", 5).note == "policy never scales to zero"
    assert ap.calls == []


def test_timeout_is_audited_and_the_scale_up_stands():
    _stz()
    clock = Clock()
    ap = FakeApplier({"JPCP": 0})
    act = Activator(ap, probe=lambda m: False, clock=clock, sleep=clock.sleep, poll_s=1)
    with pytest.raises(ActivatorTimeout) as ei:
        act.ensure_warm("JPCP", timeout=3)
    assert ei.value.status == 504
    assert ap.replicas["JPCP"] == 1
    assert ("autoscale_activation_timeout", "JPCP") in _actions()


def test_apply_failure_is_audited_and_raised():
    _stz()
    act = Activator(FakeApplier({"JPCP": 0}, fail=True), probe=lambda m: True)
    with pytest.raises(ActivatorError, match="could not scale up"):
        act.ensure_warm("JPCP", 5)
    assert ("autoscale_activation_failed", "JPCP") in _actions()


def test_concurrent_cold_requests_share_one_scale_up_and_overflow_is_refused():
    _stz()
    gate = threading.Event()
    ap = FakeApplier({"JPCP": 0})
    act = Activator(ap, probe=lambda m: gate.is_set(), poll_s=0.01, max_waiters=3)
    results, errors = [], []

    def call():
        try:
            results.append(act.ensure_warm("JPCP", 10))
        except ActivatorError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(6)]
    for t in threads:
        t.start()
    deadline = time.time() + 5
    while time.time() < deadline and len(errors) < 2:
        time.sleep(0.01)  # 1 leader + 3 buffered; the remaining 2 are refused at once
    gate.set()
    for t in threads:
        t.join(5)
    assert ap.calls == [("JPCP", 0, 1)]  # one scale-up for the whole burst
    assert len(results) == 4 and all(r.woke for r in results)
    assert len(errors) == 2 and all(isinstance(e, ActivatorOverloaded) for e in errors)


def test_http_ready_probe_rejects_bad_urls_and_names():
    with pytest.raises(ValueError):
        http_ready_probe("file:///etc/passwd")
    probe = http_ready_probe("http://127.0.0.1:9", timeout=0.2)
    assert probe('x"; drop') is False
    assert probe("jpcp") is False  # nothing listening -> not ready, never an exception


# ─── wiring: the inference router holds a cold request ────────────────────────


def _router(monkeypatch):
    from serving.inference_pipeline import app as pipeline

    return pipeline


def test_router_passes_through_when_the_activator_is_off(monkeypatch):
    pipeline = _router(monkeypatch)
    from serving.budgets import Deadline

    assert (
        asyncio.run(pipeline._activate_if_cold("jpcp", "Production", Deadline.after(5.0))) is None
    )


def test_router_maps_activator_outcomes(monkeypatch):
    pipeline = _router(monkeypatch)
    from examlops.autoscale import activator as act_mod
    from serving.budgets import Deadline

    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_ACTIVATOR", "1")

    class Stub:
        def __init__(self, exc):
            self.exc = exc
            self.seen = None

        def cached_warm(self, model):
            return False

        def ensure_warm(self, model, timeout):
            self.seen = (model, timeout)
            if self.exc:
                raise self.exc

    ok = Stub(None)
    monkeypatch.setattr(act_mod, "shared_activator", lambda: ok)
    assert (
        asyncio.run(pipeline._activate_if_cold("jpcp", "Production", Deadline.after(5.0))) is None
    )
    assert ok.seen[0] == "jpcp" and 0 < ok.seen[1] <= 5.0  # bounded by the request deadline

    monkeypatch.setattr(act_mod, "shared_activator", lambda: Stub(ActivatorTimeout("slow")))
    res = asyncio.run(pipeline._activate_if_cold("jpcp", "Production", Deadline.after(5.0)))
    assert res["error"] == "cold_start"
    assert pipeline._pipeline_response(res).status_code == 503

    monkeypatch.setattr(act_mod, "shared_activator", lambda: Stub(ActivatorOverloaded("full")))
    res = asyncio.run(pipeline._activate_if_cold("jpcp", "Production", Deadline.after(5.0)))
    assert res["error"] == "overloaded"

    monkeypatch.setattr(act_mod, "shared_activator", lambda: Stub(RuntimeError("bug")))
    assert (
        asyncio.run(pipeline._activate_if_cold("jpcp", "Production", Deadline.after(5.0))) is None
    )


def test_cli_activate_wakes_and_reports(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from examlops.cli.main import app as exa_app
    from examlops.data.autoscale_desired import get_desired, set_desired

    _stz()
    set_desired("JPCP", 0, reason="scaled to zero")
    monkeypatch.setattr(
        "examlops.autoscale.activator.http_ready_probe", lambda *a, **k: lambda m: True
    )
    r = CliRunner().invoke(exa_app, ["--json", "serve", "autoscale", "activate", "JPCP"])
    assert r.exit_code == 0, r.output
    assert '"woke": true' in r.output
    assert get_desired("JPCP")["replicas"] == 1
    r = CliRunner().invoke(exa_app, ["serve", "autoscale", "activate", "JPCP", "--timeout", "0"])
    assert r.exit_code == 2
