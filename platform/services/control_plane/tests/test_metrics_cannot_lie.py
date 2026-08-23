"""The /metrics scrape must not report a broken or unknown state as a healthy one.

Three alerts are built on the two approval gauges — `ApprovalsStale`, `ApprovalsStaleUrgent` and
`PendingApprovalQueueLarge`. An alert can only fire if the series it selects carries a truthful
value at the moment it matters, and for these two "the moment it matters" is precisely a moment
when *nothing is happening*: nobody is approving, or the store cannot be read. Both gauges used to
report zero in exactly those moments, and zero is this platform's encoding for "all clear" — so
each alert was guaranteed silent in the one state it exists to catch.
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


def _sample(body: str, name: str) -> float | None:
    for line in body.splitlines():
        if line.startswith("#"):
            continue
        metric, _, value = line.partition(" ")
        if metric == name:
            return float(value)
    return None


def _seed_pending(cp, n: int) -> None:
    """Put *n* long-pending approvals in the store without going through the API.

    They are seeded directly because the point is a queue this process never watched accumulate —
    the state a restarted control plane inherits.
    """
    conn = cp._get_db()  # also creates the schema
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


def _boom():
    raise sqlite3.OperationalError("store is unreadable")


def test_pending_queue_gauge_survives_a_restart(cp):
    """The queue gauge must come from the store, not only from events this process observed.

    It was only ever `set()` from the create/approve/reject handlers, so a control plane that
    restarted with a full queue published `0` until somebody happened to file the next approval —
    and a queue nobody is touching is the whole condition `PendingApprovalQueueLarge` exists to
    detect.
    """
    _seed_pending(cp, 12)
    body = TestClient(cp.app).get("/metrics").text
    assert _sample(body, "examlops_approvals_pending") == 12.0


def test_an_unreadable_store_does_not_overwrite_what_was_last_true(cp):
    """A failed read must report the failure, not publish the value that means 'all clear'.

    The handler used to call `update_age(None)` in its `except`, and that branch sets the age to 0 —
    which the gauge's own help text defines as "none pending". A store that cannot be read was
    therefore indistinguishable, to every alert, from an empty queue.
    """
    _seed_pending(cp, 12)
    client = TestClient(cp.app)

    body = client.get("/metrics").text
    assert _sample(body, "examlops_approvals_pending") == 12.0
    age_before = _sample(body, "examlops_approval_age_oldest_seconds")
    assert age_before is not None and age_before > 86400.0
    errors_before = _sample(body, "examlops_metrics_scrape_errors_total") or 0.0

    cp._get_db = _boom
    body = client.get("/metrics").text

    assert _sample(body, "examlops_metrics_scrape_errors_total") == errors_before + 1.0
    assert _sample(body, "examlops_approvals_pending") == 12.0
    assert _sample(body, "examlops_approval_age_oldest_seconds") == age_before


def test_the_scrape_error_counter_exists_so_an_alert_can_select_it(cp):
    """A counter nothing ever exports cannot be selected by a rule; the rule would never evaluate."""
    body = TestClient(cp.app).get("/metrics").text
    assert "examlops_metrics_scrape_errors_total" in body
