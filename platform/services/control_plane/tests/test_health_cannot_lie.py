"""An unreadable approval store must not be published as an empty one.

`/health` and `/status` both answered a failed store read with ``pending_approvals: 0``. Zero is
this platform's encoding for "nothing is waiting", and every reader above treats it that way:
`exa status` prints its approval line only ``if pending_count:``, so the fabricated zero renders
as *silence* — byte-for-byte the output of a genuinely empty queue — and `exa production` reported
the same zero from inside a production-readiness check. `/status` reached it through a bare
``except Exception: pass``, so the failure left no trace anywhere.

This is the same correction `metrics.py` already carries for the Prometheus path, where a fallback
age of 0 meant "none pending" and kept `ApprovalsStale` silent in the one state it exists to catch.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def cp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN", "test-token")
    monkeypatch.setenv("CONTROL_PLANE_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("MODELZOO_POLL_SECONDS", "0")
    import app as cp_app

    importlib.reload(cp_app)
    return cp_app


def _boom():
    raise sqlite3.OperationalError("store is unreadable")


def _seed_pending(cp, n: int) -> None:
    conn = cp._get_db()
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO pending_approvals (id, model_id, status, requested_at) "
                "VALUES (?, ?, 'pending', ?)",
                (f"id-{i}", f"M{i}", "2020-01-01T00:00:00"),
            )
        conn.commit()
    finally:
        conn.close()


def test_a_readable_store_still_reports_its_real_count(cp):
    """The guard below is only meaningful if the healthy path is untouched."""
    _seed_pending(cp, 3)
    client = TestClient(cp.app)
    assert client.get("/health").json()["pending_approvals"] == 3
    assert client.get("/status").json()["pending_approvals"] == 3


def test_an_empty_store_reports_zero_and_not_unknown(cp):
    """Zero must keep meaning zero — the fix distinguishes unknown from empty, it does not merge
    them."""
    client = TestClient(cp.app)
    assert client.get("/health").json()["pending_approvals"] == 0
    assert client.get("/status").json()["pending_approvals"] == 0


def test_health_reports_an_unreadable_store_as_unknown_not_empty(cp):
    _seed_pending(cp, 5)
    client = TestClient(cp.app)
    assert client.get("/health").json()["pending_approvals"] == 5

    cp._get_db = _boom
    body = client.get("/health").json()
    assert body["pending_approvals"] is None, "0 is the value that means 'nothing is waiting'"


def test_status_reports_an_unreadable_store_as_unknown_not_empty(cp):
    _seed_pending(cp, 5)
    client = TestClient(cp.app)
    assert client.get("/status").json()["pending_approvals"] == 5

    cp._get_db = _boom
    assert client.get("/status").json()["pending_approvals"] is None


def test_a_control_plane_that_cannot_read_its_queue_is_not_ok(cp):
    """`exa production` gates on `status == "ok"`. A queue nobody can see is not a passing platform."""
    client = TestClient(cp.app)
    cp._get_db = _boom
    assert client.get("/health").json()["status"] == "degraded"
