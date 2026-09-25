"""ADR 0031 review fixes — defects found in the slot-16 adversarial review, each pinned by outcome.

1. An expired request deadline (``Deadline.remaining() == 0``) must not become a 120 s cold wait.
2. The router must answer a cold request by its deadline even when the activator thread is stuck,
   and a known-warm model must not hop to a thread at all.
3. The activator's scale-up from zero obeys ``EXAMLOPS_AUTOSCALE_GPU_CAPACITY`` like the controller.
4. A failing wake is not retried (and audited) on every request: a short failure backoff.
5. Per-model PromQL is a valid PromQL string literal for names with ``.`` / ``-``.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "platform" / "cli" / "src"))

from examlops.autoscale import set_policy  # noqa: E402
from examlops.autoscale.activator import Activator, ActivatorError, ActivatorTimeout  # noqa: E402
from examlops.autoscale.controller import ScaleApplyError  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.setenv("RAY_MODELS_DIR", str(tmp_path / "no-models"))
    monkeypatch.setenv("EXAMLOPS_USECASE_DIR", str(tmp_path / "no-pack"))
    monkeypatch.delenv("EXAMLOPS_AUTOSCALE_ACTIVATOR", raising=False)
    monkeypatch.delenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", raising=False)
    from examlops import platform_db

    platform_db.init_db()


class FakeApplier:
    name = "fake"

    def __init__(self, replicas, fail=False):
        self.replicas = dict(replicas)
        self.fail = fail
        self.calls: list[tuple[str, int, int]] = []

    def current_replicas(self, model):
        return self.replicas.get(model)

    def apply(self, model, f, t):
        self.calls.append((model, f, t))
        if self.fail:
            raise ScaleApplyError("api down")
        self.replicas[model] = t


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


# 1 ────────────────────────────────────────────────────────────────────────────
def test_an_expired_deadline_fails_fast_instead_of_waiting_the_default():
    _stz()
    ap = FakeApplier({"JPCP": 0})
    act = Activator(ap, probe=lambda m: False, poll_s=0.01)
    t0 = time.monotonic()
    with pytest.raises(ActivatorTimeout):
        act.ensure_warm("JPCP", 0.0)  # what Deadline.remaining() returns once expired
    assert time.monotonic() - t0 < 1.0
    assert ap.calls == []


# 2 ────────────────────────────────────────────────────────────────────────────
def test_router_answers_by_the_deadline_when_the_activator_hangs(monkeypatch):
    from examlops.autoscale import activator as act_mod
    from serving.budgets import Deadline
    from serving.inference_pipeline import app as pipeline

    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_ACTIVATOR", "1")
    release = threading.Event()

    class Hangs:
        def cached_warm(self, model):
            return False

        def ensure_warm(self, model, timeout):
            release.wait(10)  # ignores its timeout: a stuck probe / API call

    monkeypatch.setattr(act_mod, "shared_activator", lambda: Hangs())
    t0 = time.monotonic()
    try:
        res = asyncio.run(pipeline._activate_if_cold("jpcp", "Production", Deadline.after(0.3)))
    finally:
        release.set()
    assert time.monotonic() - t0 < 2.0
    assert res is not None and res["error"] == "cold_start"


def test_router_known_warm_model_never_calls_ensure_warm(monkeypatch):
    from examlops.autoscale import activator as act_mod
    from serving.budgets import Deadline
    from serving.inference_pipeline import app as pipeline

    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_ACTIVATOR", "1")
    called: list[str] = []

    class Warm:
        def cached_warm(self, model):
            return True

        def ensure_warm(self, model, timeout):  # pragma: no cover - must not run
            called.append(model)

    monkeypatch.setattr(act_mod, "shared_activator", lambda: Warm())
    assert (
        asyncio.run(pipeline._activate_if_cold("jpcp", "Production", Deadline.after(5.0))) is None
    )
    assert called == []


def test_activator_cached_warm_reflects_the_ttl():
    _stz()
    act = Activator(FakeApplier({"JPCP": 2}), probe=lambda m: True, warm_ttl_s=60)
    assert act.cached_warm("JPCP") is False
    act.ensure_warm("JPCP", 5)
    assert act.cached_warm("JPCP") is True


# 3 ────────────────────────────────────────────────────────────────────────────
def test_activator_scale_up_respects_gpu_capacity(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "1")
    _stz("JPCP")
    _stz("MACK")
    ap = FakeApplier({"JPCP": 0, "MACK": 1})  # MACK already holds the only GPU
    act = Activator(ap, probe=lambda m: True)
    with pytest.raises(ActivatorError, match="GPU capacity"):
        act.ensure_warm("JPCP", 5)
    assert ap.calls == []
    assert ("autoscale_activation_refused", "JPCP") in _actions()


def test_activator_scale_up_within_gpu_capacity_proceeds(monkeypatch):
    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_CAPACITY", "2")
    _stz("JPCP")
    _stz("MACK")
    ap = FakeApplier({"JPCP": 0, "MACK": 1})
    act = Activator(ap, probe=lambda m: True)
    assert act.ensure_warm("JPCP", 5).woke
    assert ap.calls == [("JPCP", 0, 1)]


# 4 ────────────────────────────────────────────────────────────────────────────
def test_a_failed_wake_backs_off_instead_of_retrying_every_request():
    _stz()
    ap = FakeApplier({"JPCP": 0}, fail=True)
    act = Activator(ap, probe=lambda m: True, warm_ttl_s=30)
    for _ in range(5):
        with pytest.raises(ActivatorError):
            act.ensure_warm("JPCP", 5)
    assert len(ap.calls) == 1
    assert _actions().count(("autoscale_activation_failed", "JPCP")) == 1


def test_the_failure_backoff_expires():
    _stz()
    now = {"t": 1000.0}
    ap = FakeApplier({"JPCP": 0}, fail=True)
    act = Activator(ap, probe=lambda m: True, warm_ttl_s=5, clock=lambda: now["t"])
    with pytest.raises(ActivatorError):
        act.ensure_warm("JPCP", 5)
    now["t"] += 6
    ap.fail = False
    assert act.ensure_warm("JPCP", 5).woke
    assert len(ap.calls) == 2


# 5 ────────────────────────────────────────────────────────────────────────────
_GO_ESC = re.compile(r"\\(.)", re.S)


def _promql_strings_valid(expr: str) -> bool:
    """Every double-quoted literal uses only escapes the PromQL (Go) lexer accepts."""
    for lit in re.findall(r'"((?:[^"\\]|\\.)*)"', expr):
        for m in _GO_ESC.finditer(lit):
            if m.group(1) not in "abfnrtv\\\"'01234567xuU":
                return False
    return True


@pytest.mark.parametrize("metric", ["rps", "p95", "queue_depth"])
def test_promql_for_dotted_and_dashed_models_is_a_valid_literal(metric):
    from examlops.autoscale.queries import query_for

    expr = query_for(metric, "demo.v2-x")
    assert expr is not None and _promql_strings_valid(expr), expr
    # Unescaped by the PromQL lexer, the matcher is the anchored regex for the exact name.
    lit = re.search(r'model_name=~"((?:[^"\\]|\\.)*)"', expr).group(1)
    regex = lit.replace("\\\\", "\\").removeprefix("(?i)")
    assert re.fullmatch(regex, "demo.v2-x") and not re.fullmatch(regex, "demoXv2-x")


def test_promql_template_substitution_is_a_valid_literal(monkeypatch):
    from examlops.autoscale.queries import query_for

    monkeypatch.setenv("EXAMLOPS_AUTOSCALE_GPU_UTIL_QUERY", 'avg(gpu_util{model=~"(?i)^{model}$"})')
    expr = query_for("gpu_util", "demo.v2-x")
    assert expr is not None and _promql_strings_valid(expr), expr


# 6 ────────────────────────────────────────────────────────────────────────────
def test_cli_activate_refuses_a_misconfigured_k8s_api(monkeypatch):
    from typer.testing import CliRunner

    from examlops.cli.main import app as exa_app

    _stz()
    monkeypatch.delenv("EXAMLOPS_K8S_API", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    r = CliRunner().invoke(exa_app, ["serve", "autoscale", "activate", "JPCP", "--applier", "k8s"])
    assert r.exit_code == 2, r.output
    assert "Kubernetes API" in r.output


# 7 ────────────────────────────────────────────────────────────────────────────
def test_a_lost_activation_audit_is_counted_and_the_refusal_stands(monkeypatch):
    """`_wake`'s audit writes fail open, but a lost one is counted (never silent)."""
    from examlops.data import audit as audit_mod
    from examlops.data.audit import dropped_audit_events, reset_dropped_audit_events

    _stz()
    reset_dropped_audit_events()

    def boom(*a, **k):
        raise RuntimeError("audit datastore unavailable")

    monkeypatch.setattr(audit_mod, "write_audit_event", boom)
    act = Activator(FakeApplier({"JPCP": 0}, fail=True), probe=lambda m: True)
    with pytest.raises(ActivatorError, match="could not scale up"):
        act.ensure_warm("JPCP", 5)  # the operation's outcome does not depend on the audit log
    assert dropped_audit_events().get("autoscale_activation_failed") == 1
    reset_dropped_audit_events()
