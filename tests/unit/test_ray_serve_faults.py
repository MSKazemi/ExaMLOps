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
from concurrent.futures import ThreadPoolExecutor
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


# ─── reload keeps last-known-good on artifact-store failure (S5) ─────────────


def test_reload_keeps_last_known_good_when_load_fails(monkeypatch):
    """A reload during an artifact-store outage must not evict healthy in-memory models."""
    server = _make_server()
    good_entry = {"model": MagicMock(), "version": "3", "run_id": "r1"}
    server._hot[("jpcp", "Production")] = good_entry

    fake_client = MagicMock()
    fake_client.search_registered_models.return_value = [SimpleNamespace(name="jpcp")]
    fake_client.get_model_version_by_alias.return_value = SimpleNamespace(version="4")
    monkeypatch.setattr(rs_app.mlflow, "MlflowClient", lambda: fake_client)
    monkeypatch.setattr(rs_app, "_get_serve_aliases_for", lambda name: ["Production"])
    server._load_by_flavour = MagicMock(side_effect=RuntimeError("MinIO down"))

    server._load_hot_aliases()

    assert server._hot[("jpcp", "Production")] is good_entry, (
        "reload evicted a healthy model because the artifact store was down"
    )


def test_reload_replaces_entry_when_load_succeeds(monkeypatch):
    server = _make_server()
    server._hot[("jpcp", "Production")] = {"model": MagicMock(), "version": "3", "run_id": "r1"}

    new_model = MagicMock()
    new_model.metadata.run_id = "r2"
    fake_client = MagicMock()
    fake_client.search_registered_models.return_value = [SimpleNamespace(name="jpcp")]
    fake_client.get_model_version_by_alias.return_value = SimpleNamespace(version="4")
    monkeypatch.setattr(rs_app.mlflow, "MlflowClient", lambda: fake_client)
    monkeypatch.setattr(rs_app, "_get_serve_aliases_for", lambda name: ["Production"])
    server._load_by_flavour = MagicMock(return_value=new_model)

    server._load_hot_aliases()

    assert server._hot[("jpcp", "Production")]["version"] == "4"


# ─── cold-load single-flight (S9) ────────────────────────────────────────────


def test_cold_alias_load_is_single_flight(monkeypatch):
    """N concurrent requests for a cold alias trigger exactly one artifact load."""
    server = _make_server()
    loads = []
    load_started = threading.Event()

    def slow_load(name, alias, mv):
        loads.append((name, alias))
        load_started.set()
        time.sleep(0.2)
        m = MagicMock()
        m.metadata.run_id = "r9"
        return m

    fake_client = MagicMock()
    fake_client.get_model_version_by_alias.return_value = SimpleNamespace(version="7")
    monkeypatch.setattr(rs_app.mlflow, "MlflowClient", lambda: fake_client)
    server._load_by_flavour = slow_load

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(server._resolve("jpcp", "Canary", None)))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 6
    assert all(r["version"] == "7" for r in results)
    assert len(loads) == 1, f"expected one single-flight load, got {len(loads)}"


# ─── S6: poisoned predict pool recycles instead of 504ing forever ─────────────


def test_pool_recycles_after_full_poisoning():
    """When every worker is hung, the pool is swapped so new predicts get fresh threads."""
    server = _make_server()
    server._pool_workers = 2
    server._pool_lock = threading.Lock()
    server._leaked_predicts = 0
    server._pool_recycles = 0
    server._predict_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="predict")
    original_pool = server._predict_pool

    release = threading.Event()

    def hang():
        release.wait(10)

    # Two hung predicts = every worker consumed.
    f1 = server._predict_pool.submit(hang)
    f2 = server._predict_pool.submit(hang)
    time.sleep(0.05)  # let both start so cancel() fails (truly running)
    server._note_predict_timeout(f1)
    assert server._pool_recycles == 0  # one hung worker is not poisoning yet
    server._note_predict_timeout(f2)

    assert server._pool_recycles == 1, "fully poisoned pool must recycle"
    assert server._predict_pool is not original_pool
    # The fresh pool actually serves work.
    assert server._predict_pool.submit(lambda: 42).result(timeout=5) == 42
    release.set()


def test_queued_timeout_does_not_count_as_leak():
    """A timeout while still queued (cancel() succeeds) is congestion, not a hung thread."""
    server = _make_server()
    server._pool_workers = 1
    server._pool_lock = threading.Lock()
    server._leaked_predicts = 0
    server._pool_recycles = 0
    server._predict_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="predict")

    release = threading.Event()
    running = server._predict_pool.submit(release.wait, 10)
    time.sleep(0.05)
    queued = server._predict_pool.submit(lambda: 1)  # sits in the queue behind the hang

    server._note_predict_timeout(queued)  # cancel() succeeds → no leak counted
    assert server._leaked_predicts == 0
    assert server._pool_recycles == 0
    release.set()
    running.result(timeout=5)


def test_slow_but_finishing_predict_returns_its_slot():
    """A merely-slow call that completes decrements the leak count via its done-callback."""
    server = _make_server()
    server._pool_workers = 4
    server._pool_lock = threading.Lock()
    server._leaked_predicts = 0
    server._pool_recycles = 0
    server._predict_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="predict")

    release = threading.Event()
    f = server._predict_pool.submit(release.wait, 10)
    time.sleep(0.05)
    server._note_predict_timeout(f)
    assert server._leaked_predicts == 1
    release.set()
    f.result(timeout=5)
    time.sleep(0.05)  # done-callback runs
    assert server._leaked_predicts == 0
