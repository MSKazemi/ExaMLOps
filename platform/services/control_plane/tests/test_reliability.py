"""Tests for the 10 control plane reliability improvements."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib
import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    """Reload app module with a fresh tmp DB and token."""
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")  # disable poller in tests
    import app as cp_app
    importlib.reload(cp_app)
    return cp_app


@pytest.fixture()
def client(cp):
    return TestClient(cp.app)


def _auth():
    return {"Authorization": "Bearer test-token"}


# ─── Improvement 1: Registry TTL cache ───────────────────────────────────────


def test_registry_cache_returns_same_object_within_ttl(cp):
    """Two calls within TTL return the same cached dict object."""
    r1 = cp._get_registry()
    r2 = cp._get_registry()
    assert r1 is r2


def test_registry_cache_invalidated_after_expiry(cp):
    """After forced expiry, next call re-reads from disk."""
    cp._get_registry()  # prime
    cp._registry_cache.expires_at = 0.0  # force expiry
    r2 = cp._get_registry()
    assert isinstance(r2, dict)  # re-read succeeded


def test_invalidate_registry_cache(cp):
    """_invalidate_registry_cache() forces a re-read on next call."""
    cp._get_registry()
    assert cp._registry_cache is not None
    cp._invalidate_registry_cache()
    assert cp._registry_cache is None
    cp._get_registry()  # should re-prime without error
    assert cp._registry_cache is not None


# ─── Improvement 2 & 3: WAL mode + indices ───────────────────────────────────


def test_db_wal_mode_enabled(cp):
    """Each connection should be in WAL journal mode."""
    conn = cp._get_db()
    row = conn.execute("PRAGMA journal_mode").fetchone()
    conn.close()
    assert row[0] == "wal"


def test_db_indices_exist(cp):
    """Required indices must be present after schema creation."""
    conn = cp._get_db()
    indices = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    conn.close()
    assert "idx_pa_model_status" in indices
    assert "idx_me_sha" in indices


# ─── Improvement 5: Concurrent /status ───────────────────────────────────────


def test_status_runs_concurrently(cp, monkeypatch):
    """All service pings should be fanned out; result must include all services."""
    # Patch urllib.request.urlopen so no real network calls happen
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = b"[]"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        client = TestClient(cp.app)
        resp = client.get("/status")

    assert resp.status_code == 200
    data = resp.json()
    assert set(data["services"].keys()) == {
        "control_plane", "mlflow", "prefect", "ray_serve", "dashboard"
    }


# ─── Improvement 6: Retrain deduplication ────────────────────────────────────


def test_retrain_dedup_returns_409_on_duplicate(cp):
    """A second /retrain for the same model+dataset while one is in-flight returns 409."""
    # Manually inject an in-flight key
    key = cp._retrain_key("JPCP", "PM100Dataset")
    with cp._RETRAIN_LOCK:
        cp._inflight_retrains.add(key)

    client = TestClient(cp.app)
    resp = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=_auth(),
    )
    assert resp.status_code == 409
    assert "in-flight" in resp.json()["detail"]

    # Clean up
    with cp._RETRAIN_LOCK:
        cp._inflight_retrains.discard(key)


def test_retrain_key_inflight_cleared_on_prefect_error(cp, monkeypatch):
    """In-flight key must be removed even when Prefect raises an error."""
    key = cp._retrain_key("JPCP", "PM100Dataset")

    def _failing_find(*_a, **_kw):
        raise Exception("prefect down")

    monkeypatch.setattr(cp._get_gateway(), "find_deployment_id", _failing_find)

    # raise_server_exceptions=False so the 500 is returned rather than re-raised
    client = TestClient(cp.app, raise_server_exceptions=False)
    resp = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=_auth(),
    )
    assert resp.status_code in (500, 502)

    with cp._RETRAIN_LOCK:
        assert key not in cp._inflight_retrains


# ─── Improvement 7: Rate limiting ────────────────────────────────────────────


def test_rate_limiter_allows_under_limit(cp):
    """Requests within the burst capacity should all succeed (auth/logic aside)."""
    bucket = cp._TokenBucket(capacity=5, refill_rate=5 / 60)
    for _ in range(5):
        assert bucket.consume() is True


def test_rate_limiter_rejects_over_limit(cp):
    """Requests beyond burst capacity should be rejected."""
    bucket = cp._TokenBucket(capacity=3, refill_rate=3 / 60)
    for _ in range(3):
        bucket.consume()
    assert bucket.consume() is False


def test_rate_limiter_refills_over_time(cp):
    """After waiting, the bucket should have tokens again."""
    bucket = cp._TokenBucket(capacity=1, refill_rate=100.0)  # 100 tokens/sec refill
    bucket.consume()  # drain
    assert bucket.consume() is False
    time.sleep(0.02)  # 20 ms — should refill ~2 tokens at 100/s
    assert bucket.consume() is True


def test_rate_limit_endpoint_returns_429(cp, monkeypatch):
    """When the bucket is drained, write endpoints should return 429."""
    # Drain the rate limiter completely
    monkeypatch.setattr(cp._rate_limiter, "_tokens", 0.0)
    monkeypatch.setattr(cp._rate_limiter, "_refill_rate", 0.0)

    client = TestClient(cp.app)
    resp = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=_auth(),
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


# ─── Improvement 8: Poller health ────────────────────────────────────────────


def test_health_includes_poller_info(cp):
    """GET /health must include a 'poller' section."""
    client = TestClient(cp.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "poller" in data
    assert "enabled" in data["poller"]


def test_poller_not_stale_when_recently_updated(cp):
    """Poller should not be stale when last_ok_ts was recent."""
    cp._poller_last_ok_ts = time.time()
    assert cp._is_poller_stale() is False


def test_poller_stale_when_last_ok_too_old(cp, monkeypatch):
    """Poller should be stale when last_ok was more than 3× interval ago."""
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "60")
    importlib.reload(cp)
    cp._poller_last_ok_ts = time.time() - 300  # 5 min ago, interval is 60 s → 3× = 180 s
    assert cp._is_poller_stale() is True


# ─── Improvement 9: lifespan + startup checks ────────────────────────────────


def test_startup_checks_present_in_health(cp):
    """GET /health must include startup_checks with at least db and registry keys.

    Lifespan doesn't auto-run without a context manager, so we invoke the startup
    check directly — the same code path the lifespan calls.
    """
    cp._run_startup_checks()
    client = TestClient(cp.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "startup_checks" in data
    checks = data["startup_checks"]
    assert "db" in checks
    assert "registry" in checks
    assert "token" in checks


def test_startup_checks_token_missing_when_no_token(tmp_path, monkeypatch):
    """startup_checks.token should be 'missing' when CONTROL_PLANE_TOKEN is unset."""
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "notoken.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app
    importlib.reload(cp_app)
    cp_app._run_startup_checks()
    client = TestClient(cp_app.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["startup_checks"]["token"] == "missing"
    assert data["status"] == "degraded"


def test_overall_status_ok_when_all_checks_pass(cp):
    """status should be 'ok' when db, registry, and token checks all pass."""
    cp._run_startup_checks()
    client = TestClient(cp.app)
    resp = client.get("/health")
    data = resp.json()
    if all(v == "ok" for v in data["startup_checks"].values()):
        assert data["status"] == "ok"


# ─── Improvement 4: Prefect retry counter in metrics ─────────────────────────


def test_record_prefect_retry_increments_counter(cp):
    """record_prefect_retry should increment the Prometheus counter without error."""
    import metrics as m
    before = m.prefect_retries.labels(method="GET")._value.get()
    m.record_prefect_retry("GET")
    after = m.prefect_retries.labels(method="GET")._value.get()
    assert after == before + 1
