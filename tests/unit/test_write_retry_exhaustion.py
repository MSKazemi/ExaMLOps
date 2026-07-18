"""Central write-retry + retry-exhaustion observability (enterprise-readiness Phase 0, item 0.4).

Two guarantees are proven here:

1. **No silent drops.** When a mutating write loses the lock race past ``busy_timeout`` AND
   exhausts every retry, the failure is loud: an ``on_exhausted`` hook fires, a WARNING is logged,
   and a process-level counter increments — instead of the write vanishing.
2. **Central coverage.** Every mutating ``platform_db`` helper is transparently wrapped in
   ``write_retry`` at import (auto-discovered), so a transient lock auto-retries rather than
   surfacing to fire-and-forget callers that would swallow it. Helpers that already self-retry
   (the audit hash-chain, the atomic drift claim) are left untouched.
"""

from __future__ import annotations

import sqlite3

import pytest

from examlops.resilience import db as rdb
from examlops.resilience.retry import retry_call


def test_retry_call_invokes_on_exhausted_before_reraise():
    calls: list[tuple[BaseException, int]] = []

    def always_locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        retry_call(
            always_locked,
            retries=2,
            base_delay=0,
            retry_on=lambda e: True,
            sleep=lambda _s: None,
            on_exhausted=lambda exc, attempts: calls.append((exc, attempts)),
        )

    assert len(calls) == 1
    exc, attempts = calls[0]
    assert isinstance(exc, sqlite3.OperationalError)
    assert attempts == 3  # retries=2 → 3 total attempts


def test_retry_call_no_exhaustion_hook_on_success():
    calls: list[object] = []
    result = retry_call(
        lambda: 42,
        retries=2,
        base_delay=0,
        retry_on=lambda e: True,
        on_exhausted=lambda exc, attempts: calls.append(exc),
    )
    assert result == 42
    assert calls == []


def test_write_retry_counts_and_logs_exhaustion(caplog):
    before = rdb.write_retry_exhaustions()

    def always_locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    import logging

    with caplog.at_level(logging.WARNING), pytest.raises(sqlite3.OperationalError):
        rdb.write_retry(always_locked, retries=1, base_delay=0)

    assert rdb.write_retry_exhaustions() == before + 1
    assert any("exhaust" in r.message.lower() for r in caplog.records)


def test_write_retry_reraises_non_locked_immediately():
    """A genuine error (not lock contention) must not be retried or counted as exhaustion."""
    before = rdb.write_retry_exhaustions()
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise ValueError("not a lock error")

    with pytest.raises(ValueError):
        rdb.write_retry(boom, retries=3, base_delay=0)

    assert calls["n"] == 1  # no retries for a non-transient error
    assert rdb.write_retry_exhaustions() == before  # not an exhaustion


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def test_mutating_helpers_are_wrapped(db):
    """The central installer must have wrapped representative writers, but not reads/self-retriers."""
    assert getattr(db.write_drift_snapshot, "_wr_wrapped", False) is True
    assert getattr(db.set_traffic_rules, "_wr_wrapped", False) is True
    assert getattr(db.record_model_cost, "_wr_wrapped", False) is True
    # Reads stay unwrapped.
    assert getattr(db.get_traffic_rules, "_wr_wrapped", False) is False
    # Self-retrying helpers are left alone (they wrap write_retry internally).
    assert getattr(db.write_audit_event, "_wr_wrapped", False) is False
    assert getattr(db.claim_drift_trigger, "_wr_wrapped", False) is False


def test_wrapped_helper_retries_then_succeeds(db, monkeypatch):
    """A wrapped helper that hits one lock then succeeds must complete (not raise)."""
    real_connect = rdb.connect
    state = {"fail_once": True}

    def flaky_connect(*args, **kwargs):
        if state["fail_once"]:
            state["fail_once"] = False
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(rdb, "connect", flaky_connect)
    # write_drift_snapshot is wrapped → the first locked attempt retries and the second succeeds.
    db.write_drift_snapshot("JPCP", "Production", 1.23, "job-1")

    monkeypatch.setattr(rdb, "connect", real_connect)
    # Verify the row actually landed (the retry succeeded, not swallowed).
    import examlops.platform_db as pdb

    with pdb.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM drift_snapshots WHERE model='JPCP'").fetchone()[
            "c"
        ]
    assert n == 1
