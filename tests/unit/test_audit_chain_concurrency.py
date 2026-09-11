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


# ─── C1: the conn= path must serialize the head read on Postgres ──────────────


class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class PgConnection:  # noqa: N801 — name is the contract _lock_chain_head keys on
    """Recorder standing in for examlops.storage.pg.PgConnection."""

    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if sql.startswith("PRAGMA table_info"):
            names = [
                "id",
                "source",
                "actor",
                "action",
                "target",
                "details",
                "tenant",
                "prev_hash",
                "hash",
                "ts",
                "correlation_id",
                "parent_correlation_id",
                "mode",
                "on_behalf_of",
                "rollback_ref",
            ]
            return _Cursor([_Row(name=n) for n in names])
        if "CURRENT_TIMESTAMP" in sql:
            return _Cursor([_Row(t="2026-09-04 00:00:00")])
        if sql.startswith("SELECT hash FROM audit_events"):
            return _Cursor([])
        return _Cursor([])


def test_pg_conn_path_takes_advisory_lock_before_head_read():
    from examlops.data.audit import _append_on

    conn = PgConnection()
    _append_on(conn, "test", None, "unit_test", "t", None, "default")

    assert "BEGIN IMMEDIATE" in conn.executed, "Pg conn= path must take the advisory lock"
    lock_at = conn.executed.index("BEGIN IMMEDIATE")
    head_at = next(
        i for i, s in enumerate(conn.executed) if s.startswith("SELECT hash FROM audit_events")
    )
    assert lock_at < head_at, "the lock must precede the chain-head read"


def test_sqlite_conn_path_takes_no_extra_lock():
    """On SQLite the caller already holds RESERVED; no BEGIN inside their transaction."""

    class SqliteishConn(PgConnection):
        pass

    conn = SqliteishConn()
    from examlops.data.audit import _append_on

    _append_on(conn, "test", None, "unit_test", "t", None, "default")
    assert "BEGIN IMMEDIATE" not in conn.executed


def test_chain_intact_when_callers_pass_an_idle_connection(db):
    """The ``conn=`` path must not trust the caller to already hold the write lock.

    ``append_audit_event`` documented that its caller must be inside a write transaction, and 24
    dashboard mutation routes were not: the ``examlops.*`` helper committed the change on its own
    connection, then the router opened a *fresh* connection only to audit it. That connection is
    idle, so head-read and append ran unlocked and concurrent routes forked the chain —
    reproduced live as five rows sharing one ``prev_hash``. ``get_db()`` hands out exactly such an
    idle connection, so it is the faithful reproduction.
    """
    n_threads, per_thread = 8, 6
    barrier = threading.Barrier(n_threads)
    errors: list[BaseException] = []

    def worker(tid: int) -> None:
        try:
            barrier.wait()
            for i in range(per_thread):
                with db.get_db() as conn:  # idle: nothing written on it yet
                    db.write_audit_event("test", f"w{tid}", "idle_conn", f"t{tid}-{i}", conn=conn)
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
    assert result["count"] == n_threads * per_thread


class _Abandon(Exception):
    pass


def test_a_caller_holding_its_own_write_keeps_atomicity(db):
    """A caller already mid-transaction keeps its semantics: the audit rolls back with it."""
    with pytest.raises(_Abandon):
        with db.get_db() as conn:
            db.write_audit_event("test", "a", "first", "t", conn=conn)  # opens the caller's txn
            db.write_audit_event("test", "a", "second", "t", conn=conn)  # joins it
            raise _Abandon  # get_db() does not commit on the way out → both roll back
    assert db.verify_audit_chain()["count"] == 0


def test_an_idle_connection_that_loses_the_lock_race_retries_instead_of_failing(db):
    """The idle-connection lock is retried, as every other platform write is.

    ``busy_timeout`` waits once; a saturated host can still outlast it — seen as
    ``database is locked`` from the concurrent-writer test under the full parallel suite. Nothing
    is open on an idle connection when ``BEGIN IMMEDIATE`` loses, so re-issuing it is safe.
    Deterministic here: the holder releases after the waiter's first attempt has already failed.
    """
    db.init_db()
    with db.get_db() as holder, db.get_db() as waiter:
        holder.execute("BEGIN IMMEDIATE")  # another writer holds the lock
        waiter.execute("PRAGMA busy_timeout=0")  # so the first attempt fails at once
        release = threading.Timer(0.15, holder.commit)
        release.start()
        try:
            db.write_audit_event("test", "w", "raced", "t", conn=waiter)
            waiter.commit()
        finally:
            release.join()
    assert db.verify_audit_chain()["count"] == 1
