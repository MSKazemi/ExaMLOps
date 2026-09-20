"""Unit tests for Dataplane bus bridge Prometheus metrics."""

from __future__ import annotations

import os
import sys
import types

import pytest

# ── minimal stubs so bridge imports succeed without dataplane-bus installed ─────

capnp_stub = types.ModuleType("capnp")
capnp_stub.remove_import_hook = lambda: None
capnp_stub.load = lambda *a, **kw: types.ModuleType("capnp_schema")
capnp_stub.run = lambda coro: coro
sys.modules.setdefault("capnp", capnp_stub)

try:  # import the real module first: collection order must not let a stub win
    import httpx  # noqa: F401
except ImportError:
    pass
httpx_stub = types.ModuleType("httpx")
httpx_stub.HTTPError = Exception
httpx_stub.AsyncClient = object
sys.modules.setdefault("httpx", httpx_stub)

msgs_stub = types.ModuleType("dataplane_bus_msgs")


class _HpcJobV1:
    pass


class _HpcInferenceResV1:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _RetrainReqV1:
    pass


class _RetrainResV1:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _VectorReqV1:
    def __init__(self, values):
        self.values = values


class _VectorResV1:
    def __init__(self, results):
        self.results = results


msgs_stub.HpcJobV1 = _HpcJobV1
msgs_stub.HpcInferenceResV1 = _HpcInferenceResV1
msgs_stub.RetrainReqV1 = _RetrainReqV1
msgs_stub.RetrainResV1 = _RetrainResV1
msgs_stub.VectorReqV1 = _VectorReqV1
msgs_stub.VectorResV1 = _VectorResV1
sys.modules.setdefault("dataplane_bus_msgs", msgs_stub)

model_schema_stub = types.ModuleType("model_schema_registry")


class _MockRegistry:
    def __init__(self, *a, **kw):
        pass

    def build_features(self, model_name, msg):
        return {}

    def validate_features(self, model_name, features):
        pass


model_schema_stub.ModelSchemaRegistry = _MockRegistry
sys.modules.setdefault("model_schema_registry", model_schema_stub)

sb_client_stub = types.ModuleType("dataplane_bus_client")
sb_client_stub.Connection = object
sys.modules.setdefault("dataplane_bus_client", sb_client_stub)

# Add platform/clients to sys.path so the bridge can be imported directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "platform", "clients"))

import dataplane_bus_bridge as bridge  # noqa: E402
from prometheus_client import REGISTRY, generate_latest  # noqa: E402


def _metric_names() -> set[str]:
    return {m.name for m in REGISTRY.collect()}


def test_all_five_metrics_registered():
    # prometheus_client strips _total suffix from Counter .name; check base names
    names = _metric_names()
    assert "dataplane_bus_bridge_up" in names
    assert "dataplane_bus_inferences" in names  # registered as dataplane_bus_inferences_total
    assert (
        "dataplane_bus_inference_errors" in names
    )  # registered as dataplane_bus_inference_errors_total
    assert "dataplane_bus_inference_latency_seconds" in names
    assert (
        "dataplane_bus_retrain_triggers" in names
    )  # registered as dataplane_bus_retrain_triggers_total


def test_metrics_output_contains_expected_lines():
    # Trigger a histogram observation so bucket lines appear in generate_latest()
    bridge._LATENCY.labels(model="__probe__").observe(0.1)
    output = generate_latest(REGISTRY).decode()
    assert "dataplane_bus_bridge_up" in output
    assert "dataplane_bus_inferences_total" in output
    assert "dataplane_bus_inference_latency_seconds_bucket" in output


def test_inferences_counter_increments():
    before = REGISTRY.get_sample_value("dataplane_bus_inferences_total", {"model": "TEST"}) or 0.0
    bridge._INFERENCES.labels(model="TEST").inc()
    after = REGISTRY.get_sample_value("dataplane_bus_inferences_total", {"model": "TEST"}) or 0.0
    assert after == before + 1


def test_errors_counter_increments():
    before = (
        REGISTRY.get_sample_value("dataplane_bus_inference_errors_total", {"model": "TEST"}) or 0.0
    )
    bridge._ERRORS.labels(model="TEST").inc()
    after = (
        REGISTRY.get_sample_value("dataplane_bus_inference_errors_total", {"model": "TEST"}) or 0.0
    )
    assert after == before + 1


def _model_error():
    """The bridge's own model-failure exception, resolved after the stubbed import."""
    return bridge.ModelInferenceError


# `dataplane_bus_inferences_total` says "Total inference calls dispatched to Ray Serve", and the
# bridge's own internal stats count every call — success and all four error branches alike. The
# Prometheus counter did not: it was incremented after `raise_for_status()`, so it counted only
# **successes**, while `dataplane_bus_inference_errors_total` counted the failures.
#
# Everything that divides one by the other therefore computed errors ÷ successes and called it an
# error rate: 90 % failures rendered as 900 % on three `percent` panels, and a total outage — no
# successes at all — as +Inf. The alert kept firing only because its `clamp_min` denominator
# saved it from the division.
def _drive(monkeypatch, *, fails: int, succeeds: int, exc: type[BaseException] = RuntimeError):
    import asyncio

    class _Req:
        job_id = "j"
        model_name = "TEST2"
        alias = "Production"
        num_nodes = 1
        user_id = "u"

    async def _ok(*_a, **_k):
        return 1.0, "run", "1"

    async def _boom(*_a, **_k):
        raise exc("ray serve is down")

    def _count(name: str) -> float:
        return REGISTRY.get_sample_value(name, {"model": "TEST2"}) or 0.0

    # Deltas: the registry is process-global, so an earlier test's calls are still in it.
    base_total = _count("dataplane_bus_inferences_total")
    base_errors = _count("dataplane_bus_inference_errors_total")

    for _ in range(succeeds):
        monkeypatch.setattr(bridge, "_call_pipeline", _ok)
        asyncio.run(bridge._call_inference(_Req()))
    for _ in range(fails):
        monkeypatch.setattr(bridge, "_call_pipeline", _boom)
        asyncio.run(bridge._call_inference(_Req()))
    return (
        _count("dataplane_bus_inferences_total") - base_total,
        _count("dataplane_bus_inference_errors_total") - base_errors,
    )


def test_every_dispatched_call_is_counted_so_the_error_rate_cannot_exceed_one(monkeypatch):
    total, errors = _drive(monkeypatch, fails=9, succeeds=1)

    assert total == 10, (
        f"{total} calls counted for 10 dispatched — the denominator of every error-rate panel "
        "and of DataplaneBusHighErrorRate is not the population it claims to measure."
    )
    assert errors == 9
    assert errors / total == 0.9, "90% of calls failed; the error rate must read 0.9, not 9.0"


def test_a_total_outage_reads_as_one_hundred_percent_and_not_infinity(monkeypatch):
    total, errors = _drive(monkeypatch, fails=5, succeeds=0)

    # The moment the operator actually looks. With a success-only denominator this was 5/0.
    assert total == 5
    assert errors / total == 1.0


# Each `except` branch in `_call_inference` counts the error and the call separately, so the
# invariant has to hold in all of them — not just the one an arbitrary exception happens to land
# in. A mutant that double-counted a `ModelInferenceError` survived until this was parametrised.
@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(RuntimeError, id="unexpected"),
        pytest.param(ValueError, id="schema"),
        pytest.param(_model_error(), id="model-failure"),
    ],
)
def test_errors_are_a_subset_of_calls_in_every_failure_branch(monkeypatch, exc):
    total, errors = _drive(monkeypatch, fails=3, succeeds=1, exc=exc)
    assert total == 4, f"{exc.__name__}: {total} calls counted for 4 dispatched"
    assert errors == 3, f"{exc.__name__}: {errors} errors counted for 3 failures"
    assert errors <= total, "an error rate above 100% means the denominator is the wrong population"
