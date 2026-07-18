"""Distributed autopilot cycle lease (enterprise-readiness Phase 0, item 0.12 follow-up).

Proves the coarse cycle-level guard: only one autopilot cycle can hold the lease at a time,
a crashed holder's lease auto-expires (TTL), and the running cycle releases it. This is
defense-in-depth on top of the atomic `claim_drift_trigger` (which already guarantees no model
is retrained twice); the lease stops two overlapping cron cycles from scanning concurrently.
"""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def test_second_holder_blocked_while_first_holds(db):
    assert db.claim_autopilot_lease("host-a:1", ttl_s=60) is True
    assert db.claim_autopilot_lease("host-b:2", ttl_s=60) is False  # a still holds it


def test_same_holder_reacquire_extends(db):
    assert db.claim_autopilot_lease("host-a:1", ttl_s=60) is True
    assert db.claim_autopilot_lease("host-a:1", ttl_s=60) is True  # re-entrant, idempotent


def test_release_frees_the_lease(db):
    assert db.claim_autopilot_lease("host-a:1", ttl_s=60) is True
    db.release_autopilot_lease("host-a:1")
    assert db.claim_autopilot_lease("host-b:2", ttl_s=60) is True  # now free


def test_release_does_not_steal_another_holders_lease(db):
    assert db.claim_autopilot_lease("host-a:1", ttl_s=60) is True
    db.release_autopilot_lease("host-b:2")  # b never held it — no-op
    assert db.claim_autopilot_lease("host-b:2", ttl_s=60) is False  # a still holds it


def test_expired_lease_is_reclaimable(db):
    assert db.claim_autopilot_lease("host-a:1", ttl_s=1) is True
    time.sleep(1.2)  # let it expire
    assert db.claim_autopilot_lease("host-b:2", ttl_s=60) is True  # a's lease expired → b claims


def test_exactly_one_winner_under_concurrency(db):
    wins: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()
        got = db.claim_autopilot_lease(f"host-{i}", ttl_s=60)
        with lock:
            wins.append(got)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for w in wins if w) == 1  # exactly one racer acquired the lease


def test_run_cycle_skips_when_lease_held(db, monkeypatch):
    """A second live cycle observes the lease and skips (no double scan)."""
    from examlops.cli.commands import autopilot_cmd

    monkeypatch.setenv("EXAMLOPS_AUTOPILOT_ENABLED", "1")
    # Pretend another process already holds the cycle lease.
    assert db.claim_autopilot_lease("other-host:999", ttl_s=60) is True

    result = autopilot_cmd.run_cycle(dry_run=False)
    assert result.get("skipped") is True
    assert "lease" in result["reason"].lower()
