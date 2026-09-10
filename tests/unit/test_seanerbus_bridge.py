"""Unit tests for seanerbus_bridge (_handle_vector, _call_pipeline, _call_inference,
_make_inference_handler).

Import strategy
---------------
The bridge imports several C-extension packages not available in the test venv
(seanerbus, pycapnp) and the heavy prometheus_client.  All of them are stubbed
out before the bridge is imported.

Test-isolation note: other test files in this suite (e.g. test_drift_tracker.py)
also import seanerbus_bridge, and Python caches it in sys.modules.  If that
earlier import used different stubs, this file's stubs may not take effect on
a simple `import seanerbus_bridge`.  We therefore:

  1. Install all stubs unconditionally (overwrite, not setdefault) before import.
  2. Force-reload the bridge so its module-level names are rebound to our stubs.
  3. Pop model_schema_registry after reload so we don't pollute later tests.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 1. Install stubs (overwrite any existing entries) ─────────────────────────

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "platform", "clients"))

# capnp — needed only at import time of seanerbus_msgs; keep setdefault so
# we don't break test_drift_tracker which may already have a real capnp loaded
if "capnp" not in sys.modules:
    capnp_stub = types.ModuleType("capnp")
    capnp_stub.remove_import_hook = lambda: None
    capnp_stub.load = lambda *a, **kw: MagicMock()
    capnp_stub.run = lambda coro: coro
    sys.modules["capnp"] = capnp_stub

# seanerbus C extension
if "seanerbus" not in sys.modules:
    sb_client = types.ModuleType("seanerbus")
    sb_client.client = types.ModuleType("seanerbus.client")
    sb_client.client.Connection = MagicMock()
    sys.modules["seanerbus"] = sb_client
    sys.modules["seanerbus.client"] = sb_client.client

# httpx — always overwrite so our stub is in place when bridge is reloaded.
# The real module (when installed) is remembered here and put back in sys.modules
# right after the bridge import below; see the restore note at step 3.  Leaving the
# stub in place breaks every later test that imports prefect, because prefect does
# `from httpx import HTTPStatusError, Request, Response` and this stub is a bare
# ModuleType with no Request/Response and no __spec__ ("cannot import name 'Request'
# from 'httpx' (unknown location)").  It also leaves `httpx.Timeout` a MagicMock for
# examlops.resilience, which is what made test_resilience fail on '>=' comparisons.
try:  # import rather than sys.modules.get: httpx may not have been imported yet
    import httpx as _real_httpx
except ImportError:  # httpx is genuinely optional for this bridge's test env
    _real_httpx = None

# Import the shared resilience helpers BEFORE the stub goes in.  http.py and retry.py
# both do a module-level `import httpx` and keep the reference for the process, so if
# they are first imported inside the stub window (the bridge pulls them in below) they
# capture the stub for good: httpx.Client/Response vanish and httpx.RequestError becomes
# bare Exception.  That is what made test_resilience fail with AttributeError and
# is_transient_network() return True for everything.
try:
    import examlops.resilience.http  # noqa: F401
    import examlops.resilience.retry  # noqa: F401
except ImportError:  # examlops not installed in this environment
    pass
httpx_stub = types.ModuleType("httpx")
httpx_stub.HTTPError = Exception


class _HTTPStatusError(Exception):
    """Minimal HTTPStatusError stub for raise_for_status() tests."""

    def __init__(self, message="HTTP error", response=None):
        super().__init__(message)
        self.response = response


httpx_stub.HTTPStatusError = _HTTPStatusError
httpx_stub.AsyncClient = MagicMock()
httpx_stub.Timeout = MagicMock()  # used by the shared examlops.resilience timeout helper
# RequestError/ConnectError must stay the REAL classes when httpx is installed.
# examlops.resilience.retry captures `httpx.RequestError` into _HTTPX_REQUEST_ERROR at
# import time, and if that capture happens while this stub is installed, aliasing it to
# bare `Exception` makes is_transient_network() true for every exception for the rest of
# the session — which is what broke test_resilience::test_classifiers.  HTTPError stays
# aliased to Exception on purpose: test_call_inference_http_error raises a plain
# Exception and expects the bridge's `except httpx.HTTPError` to catch it.
httpx_stub.RequestError = getattr(_real_httpx, "RequestError", Exception)
httpx_stub.ConnectError = getattr(_real_httpx, "ConnectError", Exception)
sys.modules["httpx"] = httpx_stub

# seanerbus_msgs — define concrete stub classes so isinstance checks work
msgs_stub = types.ModuleType("seanerbus_msgs")


class _VectorReqV1:
    def __init__(self, values):
        self.values = values


class _VectorResV1:
    def __init__(self, results):
        self.results = results


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


msgs_stub.VectorReqV1 = _VectorReqV1
msgs_stub.VectorResV1 = _VectorResV1
msgs_stub.HpcJobV1 = _HpcJobV1
msgs_stub.HpcInferenceResV1 = _HpcInferenceResV1
msgs_stub.RetrainReqV1 = _RetrainReqV1
msgs_stub.RetrainResV1 = _RetrainResV1
sys.modules["seanerbus_msgs"] = msgs_stub

# model_schema_registry — lightweight stub (always overwrite)
model_schema_stub = types.ModuleType("model_schema_registry")


class _MockRegistry:
    def __init__(self, *a, **kw):
        pass

    def build_features(self, model_name, msg):
        return {"embedding": list(getattr(msg, "embedding", []))}

    def validate_features(self, model_name, features):
        pass


model_schema_stub.ModelSchemaRegistry = _MockRegistry
sys.modules["model_schema_registry"] = model_schema_stub

# ── 2. Import the bridge ──────────────────────────────────────────────────────

import seanerbus_bridge as bridge  # noqa: E402

# Fix-up: if another test file (e.g. test_drift_tracker.py) already imported the
# bridge with MagicMock stubs for the message classes, the bridge module's
# HpcInferenceResV1/VectorResV1/etc. will be MagicMock callables.  A full
# importlib.reload() would fix this but also re-registers Prometheus metrics,
# which raises ValueError.
#
# Simpler targeted fix: directly rebind the bridge's message-class attributes to
# our concrete stub classes.  This is safe because these attributes are only used
# as constructors — the bridge never stores the class objects in a registry or
# does isinstance() checks.
bridge.HpcInferenceResV1 = _HpcInferenceResV1  # type: ignore[attr-defined]
bridge.VectorResV1 = _VectorResV1  # type: ignore[attr-defined]
bridge.HpcJobV1 = _HpcJobV1  # type: ignore[attr-defined]
bridge.RetrainResV1 = _RetrainResV1  # type: ignore[attr-defined]

# Also patch the schema registry instance to our mock (in case the bridge was
# previously loaded with a real or different registry).
bridge._schema_registry = _MockRegistry()  # type: ignore[attr-defined]

# ── 3. Pop model_schema_registry so it doesn't shadow the real one for later tests
sys.modules.pop("model_schema_registry", None)

# Same reasoning for httpx: the bridge did `import httpx` above, so `bridge.httpx`
# already holds the stub and every `patch("seanerbus_bridge.httpx.AsyncClient")` in
# this file keeps working.  Nothing else in the suite should inherit it, so put the
# real module back.  examlops.resilience.timeouts imports httpx lazily inside
# httpx_timeout(), so it picks the restored module up on the next call.
if _real_httpx is not None:
    sys.modules["httpx"] = _real_httpx
else:
    sys.modules.pop("httpx", None)
# (examlops.resilience is left alone: timeouts.py imports httpx lazily inside
# httpx_timeout(), so restoring sys.modules above is enough.)


@pytest.fixture
def mock_http_client():
    """Async httpx.AsyncClient mock that returns a configurable response."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_handle_vector_success(mock_http_client):
    """_handle_vector returns VectorResV1 with prediction on Ray Serve success."""
    req = _VectorReqV1(values=[0.1, 0.2, 0.3])

    mock_response = MagicMock()
    mock_response.json.return_value = {"prediction": 42.0, "run_id": "abc", "model_version": "3"}
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200

    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        result = await bridge._handle_vector(req)

    assert isinstance(result, _VectorResV1)
    assert result.results == [42.0]


@pytest.mark.asyncio
async def test_handle_vector_ray_error_returns_empty(mock_http_client):
    """_handle_vector returns VectorResV1(results=[]) on Ray Serve HTTP error."""
    req = _VectorReqV1(values=[0.5])

    mock_http_client.post = AsyncMock(side_effect=Exception("connection refused"))

    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        result = await bridge._handle_vector(req)

    assert isinstance(result, _VectorResV1)
    assert result.results == []


@pytest.mark.asyncio
async def test_handle_vector_increments_stat(mock_http_client, monkeypatch):
    """_handle_vector increments _bridge_stats['vectors_total'] on each call."""
    monkeypatch.setitem(bridge._bridge_stats, "vectors_total", 0)
    req = _VectorReqV1(values=[1.0])

    mock_response = MagicMock()
    mock_response.json.return_value = {"prediction": 7.0}
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200

    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        await bridge._handle_vector(req)

    assert bridge._bridge_stats["vectors_total"] == 1


@pytest.mark.asyncio
async def test_call_pipeline_uses_schema_registry(mock_http_client):
    """_call_pipeline must spread features from the schema registry into the request body."""
    job = _HpcJobV1()
    job.job_id = "test-123"
    job.model_name = "JPCP"
    job.alias = "Production"
    job.embedding = [0.5] * 384
    job.num_nodes = 4
    job.user_id = 42

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "prediction": 55.0,
        "run_id": "run-abc",
        "model_version": "2",
    }
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_http_client.post = AsyncMock(return_value=mock_response)

    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        prediction, run_id, version = await bridge._call_pipeline(job)

    assert prediction == 55.0
    call_kwargs = mock_http_client.post.call_args
    body = call_kwargs.kwargs.get("json") or call_kwargs.args[1]
    assert "embedding" in body
    assert body["embedding"] == [0.5] * 384
    assert body["num_nodes"] == 4


# ── Required tests from the task spec ─────────────────────────────────────────


def _make_hpc_job(
    job_id: str = "job-abc12345",
    model_name: str = "JPCP",
    alias: str = "Production",
    num_nodes: int = 4,
    user_id: int = 7,
    embedding: list | None = None,
) -> _HpcJobV1:
    """Build a synthetic HpcJobV1 stub."""
    job = _HpcJobV1()
    job.job_id = job_id
    job.user_id = user_id
    job.num_nodes = num_nodes
    job.alias = alias
    job.model_name = model_name
    job.embedding = embedding if embedding is not None else [0.1] * 10
    return job


@pytest.mark.asyncio
async def test_call_pipeline_success(mock_http_client):
    """_call_pipeline returns (prediction, run_id, version) on a successful POST to Ray Serve."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "prediction": 91.5,
        "run_id": "abc123",
        "model_version": "18",
    }
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_http_client.post = AsyncMock(return_value=mock_response)

    job = _make_hpc_job()
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        prediction, run_id, version = await bridge._call_pipeline(job)

    assert prediction == 91.5
    assert run_id == "abc123"
    assert version == "18"


@pytest.mark.asyncio
async def test_call_pipeline_logs_res_line(mock_http_client, caplog):
    """_call_pipeline emits a '→ RES' log line on success."""
    import logging

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "prediction": 91.5,
        "run_id": "abc123",
        "model_version": "18",
    }
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_http_client.post = AsyncMock(return_value=mock_response)

    job = _make_hpc_job()
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        with caplog.at_level(logging.INFO, logger="seanerbus_bridge"):
            await bridge._call_pipeline(job)

    res_lines = [r.message for r in caplog.records if "→ RES" in r.message]
    assert res_lines, "Expected a '→ RES' log line after a successful pipeline call"


@pytest.mark.asyncio
async def test_call_pipeline_ray_serve_error_propagates(mock_http_client):
    """_call_pipeline propagates HTTPStatusError raised by resp.raise_for_status()."""
    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.json.return_value = {"detail": "upstream down"}  # no "error" key → transport
    mock_response.raise_for_status = MagicMock(
        side_effect=_HTTPStatusError("503 Service Unavailable")
    )
    mock_http_client.post = AsyncMock(return_value=mock_response)

    job = _make_hpc_job()
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        with pytest.raises(_HTTPStatusError):
            await bridge._call_pipeline(job)


@pytest.mark.asyncio
async def test_call_inference_success(mock_http_client):
    """_call_inference returns HpcInferenceResV1 with prediction, model_name, and run_id set."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "prediction": 91.5,
        "run_id": "abc123",
        "model_version": "18",
    }
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_http_client.post = AsyncMock(return_value=mock_response)

    job = _make_hpc_job(job_id="job-xtest001", model_name="JPCP", alias="Production")
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        result = await bridge._call_inference(job)

    assert isinstance(result, _HpcInferenceResV1)
    assert result.prediction == 91.5
    assert result.model_name == "JPCP"
    assert result.run_id == "abc123"
    # On success, error_msg must be absent or empty
    assert getattr(result, "error_msg", "") == ""


@pytest.mark.asyncio
async def test_call_inference_ray_serve_error_returns_error_msg(mock_http_client):
    """On HTTPError, _call_inference returns HpcInferenceResV1 with error_msg set (does not raise)."""
    mock_http_client.post = AsyncMock(
        side_effect=Exception("connection refused")  # HTTPError is aliased to Exception in stub
    )

    job = _make_hpc_job(model_name="JPCP")
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        result = await bridge._call_inference(job)

    assert isinstance(result, _HpcInferenceResV1)
    assert result.error_msg != "", "error_msg must be non-empty when Ray Serve returns an error"
    assert result.model_name == "JPCP"
    # On the error path the bridge sets prediction to 0.0 (real HpcInferenceResV1 default)
    # or omits it — either way prediction must be falsy / absent (not a real value)
    assert getattr(result, "prediction", 0.0) == 0.0


@pytest.mark.asyncio
async def test_make_inference_handler_binds_model_name(mock_http_client):
    """Handler from _make_inference_handler POSTs to Ray Serve with the bound model_name."""
    captured: list[dict] = []

    async def _fake_post(url, json=None, **kwargs):
        captured.append(json or {})
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "prediction": 55.0,
            "run_id": "run999",
            "model_version": "7",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_resp.status_code = 200
        return mock_resp

    mock_http_client.post = _fake_post

    # Job carries empty model_name — the handler should override with "MACK"
    job = _make_hpc_job(model_name="", alias="Production")
    handler = bridge._make_inference_handler("MACK")

    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        result = await handler(job)

    assert captured, "Expected at least one POST call to Ray Serve"
    assert captured[0].get("model_name") == "MACK", (
        f"Expected model_name='MACK' in POST body, got {captured[0].get('model_name')!r}"
    )
    assert isinstance(result, _HpcInferenceResV1)
    assert result.model_name == "MACK"


# ─── Phase 1 fault-injection: transport errors must NOT feed the drift tracker ──
# Regression guard for the spurious-retrain bug: a Ray Serve outage used to be
# recorded as model drift, which could trip the drift threshold and fire a retrain.


@pytest.mark.asyncio
async def test_transport_error_does_not_feed_drift(mock_http_client):
    """A transport failure in _call_inference must leave the drift bucket unchanged."""
    model = "JPCP"
    before = list(bridge._drift_tracker._results.get(model, []))

    mock_http_client.post = AsyncMock(side_effect=Exception("connection refused"))
    job = _make_hpc_job(model_name=model)
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        result = await bridge._call_inference(job)

    after = list(bridge._drift_tracker._results.get(model, []))
    assert after == before, "transport errors must not be recorded as model drift"
    assert result.error_msg != ""


@pytest.mark.asyncio
async def test_successful_inference_records_drift_success(mock_http_client):
    """A real prediction records a True (success) sample in the drift tracker."""
    model = "JPCP"
    before = len(bridge._drift_tracker._results.get(model, []))

    mock_response = MagicMock()
    mock_response.json.return_value = {"prediction": 42.0, "run_id": "r", "model_version": "1"}
    mock_response.raise_for_status = MagicMock()
    mock_response.status_code = 200
    mock_http_client.post = AsyncMock(return_value=mock_response)

    job = _make_hpc_job(model_name=model)
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        await bridge._call_inference(job)

    bucket = bridge._drift_tracker._results.get(model, [])
    assert len(bucket) == before + 1
    assert bucket[-1] is True


# ── QW10: per-inference telemetry writes are bundled + offloaded to a worker thread ──


def test_persist_inference_telemetry_writes_all_three(monkeypatch):
    """The bundled helper performs the drift, input-embedding and audit writes with correct args."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(bridge, "write_drift_snapshot", lambda *a: calls.__setitem__("drift", a))
    monkeypatch.setattr(bridge, "write_input_snapshot", lambda *a: calls.__setitem__("input", a))
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: calls.__setitem__("audit", a))

    bridge._persist_inference_telemetry("JPCP", "Production", 42.0, [3.0, 4.0], "job-1")

    assert calls["drift"] == ("JPCP", "Production", 42.0, "job-1")
    # embedding [3, 4] ⇒ norm=5.0, mean=3.5, std=0.5
    _, _, norm, mean, std, jid = calls["input"]  # type: ignore[misc]
    assert round(norm, 6) == 5.0 and mean == 3.5 and std == 0.5 and jid == "job-1"
    assert calls["audit"][0] == "bridge" and calls["audit"][3] == "JPCP"  # type: ignore[index]


def test_persist_skips_input_snapshot_without_embedding(monkeypatch):
    seen = {"input": 0}
    monkeypatch.setattr(bridge, "write_drift_snapshot", lambda *a: None)
    monkeypatch.setattr(
        bridge, "write_input_snapshot", lambda *a: seen.__setitem__("input", seen["input"] + 1)
    )
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: None)

    bridge._persist_inference_telemetry("JPCP", "Production", None, None, "job-2")
    assert seen["input"] == 0  # no embedding ⇒ no input snapshot


# ── the input-drift panels had nothing to draw ───────────────────────────────
#
# The bridge computed norm/mean/std on every inference to write `input_snapshots`, but never
# exported them, so three Grafana panels queried metrics that no exporter published and drew
# "No data" from the day they shipped — which looks like a calm system, not a missing metric.


def _gauge(name: str, model: str) -> float | None:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(name, {"model": model})


def test_embedding_stats_are_exported_not_only_written_to_sqlite(monkeypatch):
    monkeypatch.setattr(bridge, "write_drift_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_input_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "get_input_baseline", lambda *a, **k: None)
    bridge._baseline_seen_at.clear()

    bridge._persist_inference_telemetry("EMBX", "Production", 1.0, [3.0, 4.0], "job-3")

    assert _gauge("seanerbus_embedding_norm", "EMBX") == 5.0
    assert _gauge("seanerbus_embedding_mean", "EMBX") == 3.5
    assert _gauge("seanerbus_embedding_std", "EMBX") == 0.5


def test_baseline_gauges_are_published_from_the_recorded_baseline(monkeypatch):
    monkeypatch.setattr(bridge, "write_drift_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_input_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(
        bridge,
        "get_input_baseline",
        lambda model: {"norm_mean": 7.0, "mean_mean": 0.25, "std_mean": 0.5, "n": 100.0},
    )
    bridge._baseline_seen_at.clear()

    bridge._persist_inference_telemetry("EMBY", "Production", 1.0, [1.0, 1.0], "job-4")

    assert _gauge("seanerbus_embedding_norm_baseline", "EMBY") == 7.0
    assert _gauge("seanerbus_embedding_mean_baseline", "EMBY") == 0.25
    assert _gauge("seanerbus_embedding_std_baseline", "EMBY") == 0.5


def test_a_missing_baseline_leaves_the_gauge_unset_rather_than_zero(monkeypatch):
    """Publishing 0.0 would draw a floor on the panel and read as a real measurement."""
    monkeypatch.setattr(bridge, "write_drift_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_input_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "get_input_baseline", lambda model: None)
    bridge._baseline_seen_at.clear()

    bridge._persist_inference_telemetry("EMBZ", "Production", 1.0, [1.0, 1.0], "job-5")

    assert _gauge("seanerbus_embedding_norm_baseline", "EMBZ") is None


def test_the_baseline_is_not_read_from_sqlite_on_every_single_inference(monkeypatch):
    """A baseline changes only when an operator sets one; re-reading it per request is waste."""
    monkeypatch.setattr(bridge, "write_drift_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_input_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: None)
    reads = {"n": 0}

    def _counting(model):
        reads["n"] += 1
        return {"norm_mean": 1.0, "mean_mean": 1.0, "std_mean": 1.0}

    monkeypatch.setattr(bridge, "get_input_baseline", _counting)
    bridge._baseline_seen_at.clear()

    for _ in range(5):
        bridge._persist_inference_telemetry("EMBW", "Production", 1.0, [1.0, 1.0], "job-6")

    assert reads["n"] == 1


def test_a_broken_baseline_read_never_breaks_the_inference_path(monkeypatch):
    monkeypatch.setattr(bridge, "write_drift_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_input_snapshot", lambda *a: None)
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: None)

    def _boom(model):
        raise RuntimeError("platform.db is locked")

    monkeypatch.setattr(bridge, "get_input_baseline", _boom)
    bridge._baseline_seen_at.clear()

    bridge._persist_inference_telemetry("EMBV", "Production", 1.0, [3.0, 4.0], "job-7")

    assert _gauge("seanerbus_embedding_norm", "EMBV") == 5.0


# ── every door that can start a retrain must leave a trace ───────────────────
#
# A retrain is the platform's most consequential action — it can end in a production
# promotion — and the bridge owns the two doors with no human on the other side: a drift
# trigger fired from live error rates, and a retrain requested over the bus. Both were
# silent. The control plane, the one place *every* caller passes through, cannot record it:
# it runs without access to the shared platform.db (only `control_plane_data:/data`), so the
# trace has to be written by the caller.


@pytest.mark.asyncio
async def test_drift_trigger_writes_an_audit_event(mock_http_client, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: seen.append(a))

    resp = MagicMock()
    resp.status_code = 202
    mock_http_client.post = AsyncMock(return_value=resp)
    monkeypatch.setattr(bridge, "_RETRAINS", MagicMock())

    tracker = bridge.DriftTracker.__new__(bridge.DriftTracker)
    tracker._last_retrain = {}
    tracker.cooldown = 0

    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        await tracker._maybe_trigger("JPCP", 0.5)

    assert len(seen) == 1, "a drift-driven retrain must be audited"
    source, actor, action, target, details = seen[0]
    assert (source, action, target) == ("bridge", "retrain_triggered", "JPCP")
    assert details["reason"] == "drift"
    assert details["error_rate"] == 0.5


@pytest.mark.asyncio
async def test_bus_retrain_request_writes_an_audit_event(mock_http_client, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: seen.append(a))

    resp = MagicMock()
    resp.json.return_value = {"flow_run_id": "fr-77"}
    resp.raise_for_status = MagicMock()
    mock_http_client.post = AsyncMock(return_value=resp)

    req = _RetrainReqV1()
    req.model_name = "MACK"
    req.dataset_name = "FDataDataset"
    req.backend_name = "dataplane"
    req.is_dummy = False

    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        res = await bridge._handle_retrain(req)

    assert res.flow_run_id == "fr-77"
    assert len(seen) == 1, "a bus-requested retrain must be audited"
    source, actor, action, target, details = seen[0]
    assert (source, action, target) == ("bridge", "retrain_triggered", "MACK")
    assert details["reason"] == "bus_request"
    assert details["flow_run_id"] == "fr-77"


@pytest.mark.asyncio
async def test_failed_retrain_is_not_audited_as_triggered(mock_http_client, monkeypatch):
    """The other direction: a request that never reached the control plane is not a retrain."""
    seen: list[tuple] = []
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: seen.append(a))

    mock_http_client.post = AsyncMock(side_effect=bridge.httpx.HTTPError("boom"))

    req = _RetrainReqV1()
    req.model_name = "MACK"
    req.dataset_name = "FDataDataset"
    req.backend_name = "dataplane"
    req.is_dummy = False

    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        res = await bridge._handle_retrain(req)

    assert getattr(res, "error_msg", "")
    assert seen == [], "a failed trigger must not be recorded as a retrain"


# ─── S4: model failures ARE drift signals; transport failures are not ─────────


@pytest.mark.asyncio
async def test_ingress_inference_failed_feeds_drift_tracker(mock_http_client):
    """An ingress 500 {"error": "inference_failed"} is the MODEL failing → recorded False."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.json.return_value = {"error": "inference_failed", "detail": "predict blew up"}
    mock_http_client.post = AsyncMock(return_value=mock_response)

    bridge._drift_tracker._results.pop("JPCP", None)
    req = _make_hpc_job()
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        res = await bridge._call_inference(req)

    assert getattr(res, "error_msg", "")
    bucket = bridge._drift_tracker._results.get("JPCP", [])
    assert bucket == [False], "a model failure must reach the drift tracker"


@pytest.mark.asyncio
async def test_transport_500_still_excluded_from_drift(mock_http_client):
    """A 5xx without the inference_failed marker stays a transport error (no drift record)."""
    mock_response = MagicMock()
    mock_response.status_code = 502
    mock_response.json.return_value = {"detail": "bad gateway"}
    mock_response.raise_for_status = MagicMock(side_effect=_HTTPStatusError("502"))
    mock_http_client.post = AsyncMock(return_value=mock_response)

    bridge._drift_tracker._results.pop("JPCP", None)
    req = _make_hpc_job()
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        res = await bridge._call_inference(req)

    assert getattr(res, "error_msg", "")
    assert bridge._drift_tracker._results.get("JPCP", []) == []


# ─── S7: a rejected drift-retrain trigger is not a retrain ────────────────────


@pytest.mark.asyncio
async def test_rejected_drift_trigger_not_counted_and_cooldown_released(
    mock_http_client, monkeypatch
):
    seen: list[tuple] = []
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: seen.append(a))
    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_http_client.post = AsyncMock(return_value=mock_response)

    tracker = bridge.DriftTracker(window=4, threshold=0.5, cooldown=300)
    before = bridge._bridge_stats["retrains_total"]
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        await tracker._maybe_trigger("JPCP", 0.75)

    assert bridge._bridge_stats["retrains_total"] == before
    assert "JPCP" not in tracker._last_retrain, "cooldown must be released on rejection"
    actions = [a[2] for a in seen]
    assert actions == ["retrain_trigger_failed"]


@pytest.mark.asyncio
async def test_accepted_drift_trigger_counts_and_audits(mock_http_client, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(bridge, "write_audit_event", lambda *a, **k: seen.append(a))
    mock_response = MagicMock()
    mock_response.status_code = 202
    mock_http_client.post = AsyncMock(return_value=mock_response)

    tracker = bridge.DriftTracker(window=4, threshold=0.5, cooldown=300)
    before = bridge._bridge_stats["retrains_total"]
    with patch("seanerbus_bridge.httpx.AsyncClient", return_value=mock_http_client):
        await tracker._maybe_trigger("JPCP", 0.75)

    assert bridge._bridge_stats["retrains_total"] == before + 1
    assert "JPCP" in tracker._last_retrain
    actions = [a[2] for a in seen]
    assert actions == ["retrain_triggered"]
