"""Fault-injection tests for the Phase 1 Ray Serve resilience hardening.

Proves: MLflow-unreachable is surfaced as 503 (not 404); the health/ready split
(ready always 200, health 503 when the hot set is empty); the reachability
classifier; and the predict hard-timeout (504).
"""

from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "modelzoo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from serving.ray_serving import app as rs_app  # noqa: E402


def _make_server() -> rs_app.MultiModelServer:
    cls = rs_app.MultiModelServer.func_or_class
    server = object.__new__(cls)
    server._cache_lock = threading.RLock()
    server._hot = {}
    server._version_cache = OrderedDict()
    server._version_cache_size = 8
    server._preload_aliases = list(rs_app.PRELOAD_ALIASES)
    server._poll_task = None
    server._poller_alive = True
    server._replica_id = "test"
    for attr in (
        "_req_counter",
        "_latency_hist",
        "_pred_value_hist",
        "_models_gauge",
        "_version_gauge",
        "_reload_counter",
    ):
        setattr(server, attr, MagicMock())
    return server


# ─── reachability classifier ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc,expected",
    [
        (ConnectionError("refused"), True),
        (TimeoutError("slow"), True),
        (Exception("HTTPConnectionPool: Max retries exceeded"), True),
        (Exception("Connection refused"), True),
        (Exception("not found"), False),
        (Exception("RESOURCE_DOES_NOT_EXIST"), False),
    ],
)
def test_is_mlflow_unreachable(exc, expected):
    assert rs_app._is_mlflow_unreachable(exc) is expected


def test_unreachable_walks_cause_chain():
    root = ConnectionError("connection refused")
    wrapper = Exception("load failed")
    wrapper.__cause__ = root
    assert rs_app._is_mlflow_unreachable(wrapper) is True


# ─── 503 vs 404 in _resolve ──────────────────────────────────────────────────


def test_resolve_alias_mlflow_unreachable_returns_503():
    server = _make_server()
    with patch.object(rs_app.mlflow, "MlflowClient") as MC:
        MC.return_value.get_model_version_by_alias.side_effect = ConnectionError(
            "connection refused"
        )
        with pytest.raises(rs_app.HTTPException) as exc_info:
            server._resolve("M", alias="Production", version=None)
    assert exc_info.value.status_code == 503


def test_resolve_alias_genuine_missing_returns_404():
    server = _make_server()
    with patch.object(rs_app.mlflow, "MlflowClient") as MC:
        MC.return_value.get_model_version_by_alias.side_effect = Exception("not found")
        with pytest.raises(rs_app.HTTPException) as exc_info:
            server._resolve("M", alias="Nope", version=None)
    assert exc_info.value.status_code == 404


# ─── health / ready split ────────────────────────────────────────────────────


def test_health_503_when_hot_empty():
    server = _make_server()
    resp = SimpleNamespace(status_code=200)
    body = server.health(resp)
    assert resp.status_code == 503
    assert body["status"] == "degraded"
    assert body["models_loaded"] == 0


def test_health_200_when_models_loaded():
    server = _make_server()
    server._hot = {("M", "Production"): {"version": "3", "run_id": "r"}}
    resp = SimpleNamespace(status_code=200)
    body = server.health(resp)
    assert resp.status_code == 200
    assert body["status"] == "ok"
    assert body["models_loaded"] == 1


def test_ready_always_alive():
    server = _make_server()
    assert server.ready()["status"] == "alive"
    server._hot = {}  # even fully degraded
    assert server.ready()["status"] == "alive"


# ─── predict hard timeout ────────────────────────────────────────────────────


def test_predict_times_out_returns_504():
    server = _make_server()

    class _HangModel:
        def predict(self, _x):
            time.sleep(5)  # far longer than the 0.1s timeout
            return [1.0]

    server._hot = {("M", "Production"): {"model": _HangModel(), "version": "1", "run_id": "r"}}
    from concurrent.futures import ThreadPoolExecutor

    server._predict_pool = ThreadPoolExecutor(max_workers=1)
    server._predict_timeout = 0.1

    req = rs_app.PredictRequest(features={"x": 1.0}, alias="Production", version=None)
    with pytest.raises(rs_app.HTTPException) as exc_info:
        server.predict("M", req)
    assert exc_info.value.status_code == 504
