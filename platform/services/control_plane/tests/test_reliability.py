"""Tests for the 20 control plane reliability improvements (2 rounds)."""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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


# ─── Round 1: Improvement 1 — Registry TTL cache ──────────────────────────────


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


# ─── Round 1: Improvement 2 & 3 — WAL mode + indices ────────────────────────


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


# ─── Round 1: Improvement 5 — Concurrent /status ─────────────────────────────


def test_status_runs_concurrently(cp, monkeypatch):
    """All service pings should be fanned out; result must include all services."""
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


# ─── Round 1: Improvement 6 — Retrain deduplication ─────────────────────────


def test_retrain_dedup_returns_409_on_duplicate(cp):
    """A second /retrain for the same model+dataset while one is in-flight returns 409."""
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

    with cp._RETRAIN_LOCK:
        cp._inflight_retrains.discard(key)


def test_retrain_key_inflight_cleared_on_prefect_error(cp, monkeypatch):
    """In-flight key must be removed even when Prefect raises an error."""
    key = cp._retrain_key("JPCP", "PM100Dataset")

    def _failing_find(*_a, **_kw):
        raise Exception("prefect down")

    monkeypatch.setattr(cp._get_gateway(), "find_deployment_id", _failing_find)

    client = TestClient(cp.app, raise_server_exceptions=False)
    resp = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers=_auth(),
    )
    assert resp.status_code in (500, 502)

    with cp._RETRAIN_LOCK:
        assert key not in cp._inflight_retrains


# ─── Round 1: Improvement 7 — Rate limiting ──────────────────────────────────


def test_rate_limiter_allows_under_limit(cp):
    """Requests within the burst capacity should all succeed."""
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


# ─── Round 1: Improvement 8 — Poller health ──────────────────────────────────


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


# ─── Round 1: Improvement 9 — Startup checks ──────────────────────────────────


def test_startup_checks_present_in_health(cp):
    """GET /health must include startup_checks with at least db and registry keys."""
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


# ─── Round 1: Improvement 4 — Prefect retry counter ──────────────────────────


def test_record_prefect_retry_increments_counter(cp):
    """record_prefect_retry should increment the Prometheus counter without error."""
    import metrics as m
    before = m.prefect_retries.labels(method="GET")._value.get()
    m.record_prefect_retry("GET")
    after = m.prefect_retries.labels(method="GET")._value.get()
    assert after == before + 1


# ─── Round 2: Improvement 11 — Structured JSON logging ───────────────────────


def test_json_formatter_emits_valid_json(cp):
    """_JsonFormatter must produce parseable JSON for every record."""
    import logging
    formatter = cp._JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="hello %s", args=("world",), exc_info=None,
    )
    line = formatter.format(record)
    parsed = json.loads(line)
    assert parsed["msg"] == "hello world"
    assert parsed["level"] == "INFO"
    assert "ts" in parsed


def test_json_formatter_includes_request_id(cp):
    """When a request_id is set in ContextVar, it should appear in JSON log."""
    import logging
    cp._request_id_var.set("abc123")
    try:
        formatter = cp._JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        line = formatter.format(record)
        parsed = json.loads(line)
        assert parsed.get("request_id") == "abc123"
    finally:
        cp._request_id_var.set("")


# ─── Round 2: Improvement 12 — Circuit breaker ───────────────────────────────


def test_circuit_breaker_opens_after_fail_max(cp):
    """After PREFECT_CB_FAIL_MAX failures the breaker must be in OPEN state."""
    cb = cp._CircuitBreaker(fail_max=3, reset_timeout=9999)
    for _ in range(3):
        try:
            cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
        except Exception:
            pass
    assert cb.state == "open"


def test_circuit_breaker_raises_503_when_open(cp):
    """A call while OPEN must raise HTTPException(503) without calling fn."""
    from fastapi import HTTPException
    cb = cp._CircuitBreaker(fail_max=1, reset_timeout=9999)
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass
    assert cb.state == "open"
    with pytest.raises(HTTPException) as exc_info:
        cb.call(lambda: "should not run")
    assert exc_info.value.status_code == 503


def test_circuit_breaker_half_open_after_reset_timeout(cp):
    """After reset_timeout, the breaker allows one trial call (HALF-OPEN state)."""
    cb = cp._CircuitBreaker(fail_max=1, reset_timeout=0.05)
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass
    assert cb.state == "open"
    time.sleep(0.1)  # past reset_timeout
    cb.call(lambda: None)  # successful probe
    assert cb.state == "closed"


def test_circuit_breaker_closes_on_success_in_half_open(cp):
    """A successful call in HALF-OPEN must close the breaker."""
    cb = cp._CircuitBreaker(fail_max=1, reset_timeout=0.0)
    try:
        cb.call(lambda: (_ for _ in ()).throw(Exception("fail")))
    except Exception:
        pass
    time.sleep(0.01)
    result = cb.call(lambda: "ok")
    assert result == "ok"
    assert cb.state == "closed"


def test_health_includes_circuit_breaker_state(cp):
    """GET /health must include circuit_breaker.state."""
    client = TestClient(cp.app)
    resp = client.get("/health")
    data = resp.json()
    assert "circuit_breaker" in data
    assert data["circuit_breaker"]["state"] in ("closed", "open", "half-open")


# ─── Round 2: Improvement 13 — Retrain metrics ───────────────────────────────


def test_record_retrain_increments_counter(cp):
    """record_retrain must increment the retrain_requests counter."""
    import metrics as m
    before = m.retrain_requests.labels(
        model_name="JPCP", dataset_name="PM100Dataset", outcome="success"
    )._value.get()
    m.record_retrain("JPCP", "PM100Dataset", "success")
    after = m.retrain_requests.labels(
        model_name="JPCP", dataset_name="PM100Dataset", outcome="success"
    )._value.get()
    assert after == before + 1


def test_observe_retrain_duration_records(cp):
    """observe_retrain_duration must not raise and should update the histogram sum."""
    import metrics as m
    h = m.retrain_duration.labels(model_name="JPCP", dataset_name="PM100Dataset")
    before_sum = h._sum.get()
    m.observe_retrain_duration("JPCP", "PM100Dataset", 0.42)
    after_sum = m.retrain_duration.labels(
        model_name="JPCP", dataset_name="PM100Dataset"
    )._sum.get()
    assert after_sum > before_sum


# ─── Round 2: Improvement 14 — Request-ID middleware ─────────────────────────


def test_request_id_header_present_in_response(cp):
    """Every response must carry an X-Request-ID header."""
    client = TestClient(cp.app)
    resp = client.get("/health")
    assert "x-request-id" in resp.headers


def test_request_id_echoes_client_provided_value(cp):
    """If the client sends X-Request-ID, the same value must be echoed back."""
    client = TestClient(cp.app)
    resp = client.get("/health", headers={"X-Request-ID": "my-trace-123"})
    assert resp.headers.get("x-request-id") == "my-trace-123"


def test_request_id_generated_when_absent(cp):
    """If the client omits X-Request-ID, the server should generate one."""
    client = TestClient(cp.app)
    resp = client.get("/health")
    req_id = resp.headers.get("x-request-id", "")
    assert len(req_id) > 0


# ─── Round 2: Improvement 15 — Liveness / readiness split ────────────────────


def test_ready_endpoint_always_200(cp):
    """GET /ready must always return 200 with status=alive."""
    client = TestClient(cp.app)
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "alive"


def test_ready_independent_of_startup_checks(tmp_path, monkeypatch):
    """GET /ready must return 200 even when token is missing (degraded mode)."""
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "ready_test.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app
    importlib.reload(cp_app)
    client = TestClient(cp_app.app)
    resp = client.get("/ready")
    assert resp.status_code == 200


# ─── Round 2: Improvement 16 — Approval expiry ───────────────────────────────


def test_expire_old_approvals_marks_stale_entries(cp, monkeypatch):
    """_expire_old_approvals must update old pending entries to 'expired'."""
    monkeypatch.setattr(cp, "APPROVAL_EXPIRY_HOURS", 1)

    old_ts = "2020-01-01T00:00:00"  # far in the past
    with cp._DB_LOCK:
        conn = cp._get_db()
        try:
            conn.execute(
                "INSERT INTO pending_approvals (id, model_id, status, requested_at) "
                "VALUES ('exp-1', 'JPCP', 'pending', ?)",
                (old_ts,),
            )
            conn.commit()
        finally:
            conn.close()

    expired_count = cp._expire_old_approvals()
    assert expired_count >= 1

    conn = cp._get_db()
    row = conn.execute(
        "SELECT status FROM pending_approvals WHERE id='exp-1'"
    ).fetchone()
    conn.close()
    assert row[0] == "expired"


def test_expire_old_approvals_skips_recent_entries(cp, monkeypatch):
    """_expire_old_approvals must NOT touch recently created pending entries."""
    monkeypatch.setattr(cp, "APPROVAL_EXPIRY_HOURS", 72)
    from datetime import datetime
    recent_ts = datetime.utcnow().isoformat()

    with cp._DB_LOCK:
        conn = cp._get_db()
        try:
            conn.execute(
                "INSERT INTO pending_approvals (id, model_id, status, requested_at) "
                "VALUES ('rec-1', 'JPCP', 'pending', ?)",
                (recent_ts,),
            )
            conn.commit()
        finally:
            conn.close()

    cp._expire_old_approvals()

    conn = cp._get_db()
    row = conn.execute(
        "SELECT status FROM pending_approvals WHERE id='rec-1'"
    ).fetchone()
    conn.close()
    assert row[0] == "pending"


# ─── Round 2: Improvement 17 — Idempotency keys ──────────────────────────────


def test_idempotency_cache_stores_and_retrieves(cp):
    """_store_idempotency should be retrievable by _check_idempotency within TTL."""
    cp._store_idempotency("key-abc", {"flow_run_id": "run-123"})
    cached = cp._check_idempotency("key-abc")
    assert cached is not None
    assert cached["flow_run_id"] == "run-123"


def test_idempotency_cache_expires_after_ttl(cp, monkeypatch):
    """Entries should expire after IDEMPOTENCY_TTL_SECONDS."""
    monkeypatch.setattr(cp, "IDEMPOTENCY_TTL_SECONDS", 0.05)
    cp._store_idempotency("key-expire", {"flow_run_id": "run-99"})
    time.sleep(0.1)
    result = cp._check_idempotency("key-expire")
    assert result is None


def test_idempotency_header_returns_cached_response(cp, monkeypatch):
    """POST /retrain with a repeated X-Idempotency-Key should return the same response."""
    cached_payload = {
        "flow_run_id": "cached-run-id",
        "deployment": "examlops_scheduled_training/nightly",
        "status_url": "/retrain/cached-run-id",
        "parameters": {},
    }
    cp._store_idempotency("idem-key-001", cached_payload)

    # Patch the registry so the request isn't blocked at validation
    monkeypatch.setattr(cp, "_get_registry", lambda: {"JPCP": ["PM100Dataset"]})

    client = TestClient(cp.app)
    resp = client.post(
        "/retrain",
        json={"model_name": "JPCP", "dataset_name": "PM100Dataset"},
        headers={**_auth(), "X-Idempotency-Key": "idem-key-001"},
    )
    assert resp.status_code == 200
    assert resp.json()["flow_run_id"] == "cached-run-id"


# ─── Round 2: Improvement 18 — Security headers ──────────────────────────────


def test_security_headers_present(cp):
    """Responses must include standard security headers."""
    client = TestClient(cp.app)
    resp = client.get("/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "no-store" in resp.headers.get("cache-control", "")


# ─── Round 2: Improvement 19 — Config hot-reload ─────────────────────────────


def test_admin_reload_invalidates_registry_cache(cp):
    """POST /admin/reload must clear the registry cache and re-populate it."""
    cp._get_registry()  # prime the cache
    assert cp._registry_cache is not None

    client = TestClient(cp.app)
    resp = client.post("/admin/reload", headers=_auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["registry_reloaded"] is True
    assert "startup_checks" in data
    assert cp._registry_cache is not None  # re-populated after reload


def test_admin_reload_requires_auth(cp):
    """POST /admin/reload without bearer token must return 401 or 403."""
    client = TestClient(cp.app)
    resp = client.post("/admin/reload")
    assert resp.status_code in (401, 403)


# ─── Round 2: Improvement 20 — DB NFS retry ──────────────────────────────────


def test_db_retry_on_operational_error(cp, monkeypatch):
    """_get_db should retry up to 3 times on sqlite3.OperationalError."""
    import sqlite3
    call_count = {"n": 0}
    real_connect = __import__("sqlite3").connect

    def _flaky_connect(path, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect(path, **kwargs)

    monkeypatch.setattr("sqlite3.connect", _flaky_connect)
    conn = cp._get_db()
    conn.close()
    assert call_count["n"] == 3


def test_db_retry_raises_after_all_attempts_exhausted(cp, monkeypatch):
    """_get_db should raise after all 3 retry attempts fail."""
    import sqlite3

    def _always_fail(path, **kwargs):
        raise sqlite3.OperationalError("NFS mount unavailable")

    monkeypatch.setattr("sqlite3.connect", _always_fail)
    with pytest.raises(sqlite3.OperationalError, match="DB unavailable"):
        cp._get_db()
