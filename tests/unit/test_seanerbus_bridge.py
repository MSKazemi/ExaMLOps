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

# httpx — always overwrite so our stub is in place when bridge is reloaded
httpx_stub = types.ModuleType("httpx")
httpx_stub.HTTPError = Exception


class _HTTPStatusError(Exception):
    """Minimal HTTPStatusError stub for raise_for_status() tests."""

    def __init__(self, message="HTTP error", response=None):
        super().__init__(message)
        self.response = response


httpx_stub.HTTPStatusError = _HTTPStatusError
httpx_stub.AsyncClient = MagicMock()
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
