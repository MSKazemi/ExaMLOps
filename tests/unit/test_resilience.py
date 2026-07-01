"""Unit tests for the shared examlops.resilience foundation (Phase 0).

Covers the circuit breaker state machine, retry/backoff + classifiers, the
sync/async resilient HTTP helpers, and the hardened SQLite connection —
including concurrent-writer contention that used to raise ``database is locked``.
"""

from __future__ import annotations

import sqlite3
import threading

import httpx
import pytest

from examlops.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    is_locked_error,
    is_transient_network,
    request_json,
    retry_call,
)
from examlops.resilience import db as rdb
from examlops.resilience.http import arequest_json

# ─── Circuit breaker ─────────────────────────────────────────────────────────


def _boom():
    raise RuntimeError("upstream down")


def test_breaker_opens_after_fail_max():
    cb = CircuitBreaker(fail_max=3, reset_timeout=9999)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            cb.call(_boom)
    assert cb.state == CircuitBreaker.OPEN


def test_breaker_fast_fails_when_open():
    cb = CircuitBreaker(fail_max=1, reset_timeout=9999)
    with pytest.raises(RuntimeError):
        cb.call(_boom)
    # Now OPEN — fn must NOT run; CircuitOpenError raised instead.
    ran = {"v": False}

    def _should_not_run():
        ran["v"] = True
        return "nope"

    with pytest.raises(CircuitOpenError):
        cb.call(_should_not_run)
    assert ran["v"] is False


def test_breaker_half_open_then_close_on_success():
    cb = CircuitBreaker(fail_max=1, reset_timeout=0.0)
    with pytest.raises(RuntimeError):
        cb.call(_boom)
    # reset_timeout=0 → immediately eligible for HALF-OPEN probe.
    assert cb.call(lambda: "ok") == "ok"
    assert cb.state == CircuitBreaker.CLOSED


def test_breaker_on_open_hook_fires_once():
    calls = {"n": 0}
    cb = CircuitBreaker(fail_max=1, reset_timeout=9999, on_open=lambda: calls.__setitem__("n", calls["n"] + 1))
    with pytest.raises(RuntimeError):
        cb.call(_boom)
    assert calls["n"] == 1


def test_breaker_is_failure_predicate_ignores_client_errors():
    # A predicate that treats ValueError as non-failure must not trip the breaker.
    cb = CircuitBreaker(fail_max=1, reset_timeout=9999, is_failure=lambda e: not isinstance(e, ValueError))
    with pytest.raises(ValueError):
        cb.call(lambda: (_ for _ in ()).throw(ValueError("client")))
    assert cb.state == CircuitBreaker.CLOSED


# ─── Retry + classifiers ─────────────────────────────────────────────────────


def test_retry_succeeds_after_transient_failures():
    attempts = {"n": 0}

    def _flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("blip")
        return "done"

    result = retry_call(_flaky, retries=3, sleep=lambda _s: None)
    assert result == "done"
    assert attempts["n"] == 3


def test_retry_does_not_retry_non_matching():
    attempts = {"n": 0}

    def _bad():
        attempts["n"] += 1
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        retry_call(_bad, retries=3, sleep=lambda _s: None)
    assert attempts["n"] == 1  # no retries for non-transient


def test_retry_reraises_after_exhaustion():
    with pytest.raises(TimeoutError):
        retry_call(lambda: (_ for _ in ()).throw(TimeoutError("nope")), retries=2, sleep=lambda _s: None)


def test_classifiers():
    assert is_transient_network(ConnectionError())
    assert is_transient_network(TimeoutError())
    assert is_transient_network(httpx.ConnectError("x"))
    assert not is_transient_network(ValueError())
    assert is_locked_error(sqlite3.OperationalError("database is locked"))
    assert is_locked_error(sqlite3.OperationalError("database is busy"))
    assert not is_locked_error(sqlite3.OperationalError("no such table"))
    assert not is_locked_error(ValueError())


# ─── Resilient HTTP (sync) ───────────────────────────────────────────────────


def test_request_json_success(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    orig = httpx.Client

    def _client(**kwargs):
        kwargs["transport"] = transport
        return orig(**kwargs)

    monkeypatch.setattr(httpx, "Client", _client)
    data, err = request_json("svc", "GET", "http://x/health")
    assert err is None
    assert data == {"ok": True}


def test_request_json_retries_transient_then_fails(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    orig = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: orig(transport=transport, **kw))
    data, err = request_json("svc", "GET", "http://x/health", retries=2, base_delay=0)
    assert data is None
    assert "cannot reach svc" in err
    assert calls["n"] == 3  # 1 + 2 retries


def test_request_json_http_error_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    orig = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: orig(transport=transport, **kw))
    data, err = request_json("svc", "GET", "http://x", retries=3, base_delay=0)
    assert data is None
    assert "returned 500" in err
    assert calls["n"] == 1  # 5xx is definitive — no retry


# ─── Resilient HTTP (async) ──────────────────────────────────────────────────


async def test_arequest_json_success():
    def handler(request):
        return httpx.Response(200, json={"pong": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data, err = await arequest_json("svc", "GET", "http://x", client=client)
    await client.aclose()
    assert err is None
    assert data == {"pong": 1}


async def test_arequest_json_breaker_opens_and_fast_fails():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cb = CircuitBreaker(name="ray", fail_max=2, reset_timeout=9999)
    # First call: 1 attempt + 2 retries = 3 transport failures → trips breaker (fail_max=2).
    data, err = await arequest_json("ray", "GET", "http://x", breaker=cb, retries=2, base_delay=0)
    assert data is None and err is not None
    before = calls["n"]
    assert cb.state == CircuitBreaker.OPEN
    # Second call must fast-fail without hitting the transport again.
    data2, err2 = await arequest_json("ray", "GET", "http://x", breaker=cb, retries=2, base_delay=0)
    assert data2 is None and "circuit open" in err2
    assert calls["n"] == before  # no new transport calls
    await client.aclose()


# ─── Hardened SQLite ─────────────────────────────────────────────────────────


def test_db_connect_sets_pragmas(tmp_path):
    conn = rdb.connect(str(tmp_path / "t.db"))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 1
    conn.close()


def test_db_write_retry_recovers_from_locked():
    attempts = {"n": 0}

    def _flaky_write():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise sqlite3.OperationalError("database is locked")
        return "written"

    assert rdb.write_retry(_flaky_write, base_delay=0) == "written"
    assert attempts["n"] == 2


def test_concurrent_writers_no_lock_error(tmp_path):
    """Many threads writing a busy-timeout'd DB must not raise 'database is locked'."""
    path = str(tmp_path / "concurrent.db")
    setup = rdb.connect(path)
    setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    setup.commit()
    setup.close()

    errors: list[Exception] = []

    def _worker(n: int):
        try:
            for i in range(20):
                conn = rdb.connect(path)
                conn.execute("INSERT INTO t (v) VALUES (?)", (f"{n}-{i}",))
                conn.commit()
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"unexpected DB errors under concurrency: {errors[:3]}"
    check = rdb.connect(path)
    count = check.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    check.close()
    assert count == 8 * 20
