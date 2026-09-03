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


# ── what "unchained" means, and what it does not ─────────────────────────────


def test_verify_counts_rows_it_could_not_check_instead_of_skipping_them(tmp_path, monkeypatch):
    """`verify_audit_chain` selects `WHERE hash IS NOT NULL`. Until 2026-09-02 that meant an
    unchained row was invisible: `ok: True` with a `count` that silently excluded it. A verifier
    that ignores what it cannot check reports success over a log it has only partly read.
    """
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    from examlops.data.audit import verify_audit_chain, write_audit_event
    from examlops.platform_db import get_db, init_db

    init_db()
    write_audit_event("cli", "m", "chained", "t", None)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
            ("legacy", "m", "unchained", "t", None),
        )

    result = verify_audit_chain()
    assert result["ok"] is True, "the chain that exists is intact — crying wolf would be wrong"
    assert result["count"] == 1
    assert result["unchained"] == 1, "an unverifiable row was skipped rather than counted"
    assert "warning" in result


def test_a_clean_chain_reports_no_warning(tmp_path, monkeypatch):
    """The warning must mean something. If it appeared on every run it would be ignored."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    from examlops.data.audit import verify_audit_chain, write_audit_event
    from examlops.platform_db import init_db

    init_db()
    write_audit_event("cli", "m", "a", "t", None)
    result = verify_audit_chain()
    assert result["unchained"] == 0
    assert "warning" not in result and "chain_begins_at" not in result


def test_the_chain_start_is_reported_so_the_two_causes_are_separable(tmp_path, monkeypatch):
    """Unchained rows have two causes: history written before the chain columns existed
    (expected, ages out, must NOT be back-filled because that would rewrite the log), and a
    writer still bypassing `write_audit_event` (a bug). Only the timestamp tells them apart —
    and mistaking the first for the second is exactly the error this test exists to prevent.
    """
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "p.db"))
    from examlops.data.audit import verify_audit_chain, write_audit_event
    from examlops.platform_db import get_db, init_db

    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
            ("legacy", "m", "pre_migration", "t", None),
        )
    write_audit_event("cli", "m", "first_chained", "t", None)

    result = verify_audit_chain()
    assert result["chain_begins_at"], "no boundary reported, so the causes cannot be told apart"
