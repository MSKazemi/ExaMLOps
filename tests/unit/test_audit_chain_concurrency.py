"""Audit hash-chain integrity under concurrent writers (enterprise-readiness Phase 0, item 0.3).

The tamper-evident audit chain reads the current head hash and appends the next link. If that
read-modify-write is not atomic across writers, two threads can chain off the same parent and fork
the chain — ``verify_audit_chain`` then reports a prev_hash mismatch. ``write_audit_event`` now holds
an IMMEDIATE (RESERVED) lock across the head-read + append and retries on lock loss, so the chain
stays contiguous no matter how many writers race. These tests prove that.
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def test_chain_intact_under_parallel_writers(db):
    """8 threads × 12 events, barrier-synchronized, must yield one contiguous, valid chain."""
    n_threads, per_thread = 8, 12
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            barrier.wait()  # maximise contention: all threads append at once
            for i in range(per_thread):
                db.write_audit_event(
                    "test", f"writer-{tid}", "concurrent_write", f"target-{tid}-{i}"
                )
        except BaseException as exc:  # noqa: BLE001 - surface in the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"writers raised: {errors}"

    result = db.verify_audit_chain()
    assert result["ok"] is True, result
    # Every event landed and every link verifies — no forks, no drops.
    assert result["count"] == n_threads * per_thread


def test_chain_head_advances_monotonically(db):
    """Sequential writes keep a single advancing head (sanity for the IMMEDIATE-lock path)."""
    db.write_audit_event("test", "a", "one", "t1")
    head1 = db.audit_chain_head()
    db.write_audit_event("test", "a", "two", "t2")
    head2 = db.audit_chain_head()
    assert head1 is not None and head2 is not None
    assert head2["id"] > head1["id"]
    assert head2["hash"] != head1["hash"]
    assert db.verify_audit_chain()["ok"] is True
