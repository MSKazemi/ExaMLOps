"""Unit tests for SeanerBUS bridge Prometheus metrics."""
from __future__ import annotations

import os
import sys
import types

# ── minimal stubs so bridge imports succeed without seanerbus installed ─────

capnp_stub = types.ModuleType("capnp")
capnp_stub.remove_import_hook = lambda: None
capnp_stub.load = lambda *a, **kw: types.ModuleType("capnp_schema")
capnp_stub.run = lambda coro: coro
sys.modules.setdefault("capnp", capnp_stub)

httpx_stub = types.ModuleType("httpx")
httpx_stub.HTTPError = Exception
httpx_stub.AsyncClient = object
sys.modules.setdefault("httpx", httpx_stub)

msgs_stub = types.ModuleType("seanerbus_msgs")


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
sys.modules.setdefault("seanerbus_msgs", msgs_stub)

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

sb_client_stub = types.ModuleType("seanerbus_client")
sb_client_stub.Connection = object
sys.modules.setdefault("seanerbus_client", sb_client_stub)

# Add platform/clients to sys.path so the bridge can be imported directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "platform", "clients"))

import seanerbus_bridge as bridge  # noqa: E402
from prometheus_client import REGISTRY, generate_latest  # noqa: E402


def _metric_names() -> set[str]:
    return {m.name for m in REGISTRY.collect()}


def test_all_five_metrics_registered():
    # prometheus_client strips _total suffix from Counter .name; check base names
    names = _metric_names()
    assert "seanerbus_bridge_up" in names
    assert "seanerbus_inferences" in names          # registered as seanerbus_inferences_total
    assert "seanerbus_inference_errors" in names    # registered as seanerbus_inference_errors_total
    assert "seanerbus_inference_latency_seconds" in names
    assert "seanerbus_retrain_triggers" in names    # registered as seanerbus_retrain_triggers_total


def test_metrics_output_contains_expected_lines():
    # Trigger a histogram observation so bucket lines appear in generate_latest()
    bridge._LATENCY.labels(model="__probe__").observe(0.1)
    output = generate_latest(REGISTRY).decode()
    assert "seanerbus_bridge_up" in output
    assert "seanerbus_inferences_total" in output
    assert "seanerbus_inference_latency_seconds_bucket" in output


def test_inferences_counter_increments():
    before = REGISTRY.get_sample_value("seanerbus_inferences_total", {"model": "TEST"}) or 0.0
    bridge._INFERENCES.labels(model="TEST").inc()
    after = REGISTRY.get_sample_value("seanerbus_inferences_total", {"model": "TEST"}) or 0.0
    assert after == before + 1


def test_errors_counter_increments():
    before = REGISTRY.get_sample_value("seanerbus_inference_errors_total", {"model": "TEST"}) or 0.0
    bridge._ERRORS.labels(model="TEST").inc()
    after = REGISTRY.get_sample_value("seanerbus_inference_errors_total", {"model": "TEST"}) or 0.0
    assert after == before + 1
