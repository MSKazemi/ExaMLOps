"""Retention/TTL prune for unbounded per-inference telemetry (enterprise-readiness QW9).

`purge_telemetry` deletes old rows from the high-volume drift/input snapshot tables while never
touching the tamper-evident audit log or FinOps cost history. Dry-run reports counts without
changing anything.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.platform_db as pdb

    pdb.init_db()
    return pdb


def _seed(pdb, *, old_days: int, fresh: int, old: int):
    with pdb.get_db() as c:
        for i in range(fresh):
            c.execute(
                "INSERT INTO drift_snapshots (ts, model, alias, prediction) "
                "VALUES (datetime('now'), 'JPCP', 'Production', ?)",
                (float(i),),
            )
        for i in range(old):
            c.execute(
                "INSERT INTO drift_snapshots (ts, model, alias, prediction) "
                "VALUES (datetime('now', ?), 'JPCP', 'Production', ?)",
                (f"-{old_days} days", float(i)),
            )
            c.execute(
                "INSERT INTO input_snapshots (ts, model, alias, emb_norm, emb_mean, emb_std) "
                "VALUES (datetime('now', ?), 'JPCP', 'Production', 1.0, 0.0, 1.0)",
                (f"-{old_days} days",),
            )


def _count(pdb, table: str) -> int:
    with pdb.get_db() as c:
        return c.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def test_dry_run_changes_nothing(db):
    _seed(db, old_days=200, fresh=3, old=5)
    counts = db.purge_telemetry(90, dry_run=True)
    assert counts["drift_snapshots"] == 5
    assert counts["input_snapshots"] == 5
    assert _count(db, "drift_snapshots") == 8  # nothing deleted
    assert _count(db, "input_snapshots") == 5


def test_prune_deletes_only_old_rows(db):
    _seed(db, old_days=200, fresh=3, old=5)
    counts = db.purge_telemetry(90)
    assert counts["drift_snapshots"] == 5
    assert _count(db, "drift_snapshots") == 3  # the 3 fresh rows survive
    assert _count(db, "input_snapshots") == 0


def test_prune_never_touches_audit_chain(db):
    db.write_audit_event("test", "a", "one", "t1")
    db.write_audit_event("test", "a", "two", "t2")
    # Even with an aggressive 0-day retention, the audit log is not in the prunable set.
    db.purge_telemetry(0)
    assert _count(db, "audit_events") == 2
    assert db.verify_audit_chain()["ok"] is True


def test_negative_retention_rejected(db):
    with pytest.raises(ValueError):
        db.purge_telemetry(-1)


def test_vacuum_runs_only_on_sqlite_backend(db, monkeypatch):
    """C5: under a non-sqlite backend the file-level VACUUM is skipped (and noted)."""
    import examlops.data.data_assets as da

    _seed(db, old_days=200, fresh=1, old=2)
    monkeypatch.setattr(da, "_is_sqlite_backend", lambda: False)

    class _NoDirectOpen:
        @staticmethod
        def connect(*a, **k):
            raise AssertionError("VACUUM must not open the local DB file under postgres")

    # Patch only data_assets' module-level `_rdb` reference — get_db() (used by the prune
    # itself) resolves the real resilience.db through platform_db and must keep working.
    monkeypatch.setattr(da, "_rdb", _NoDirectOpen())
    counts = da.purge_telemetry(90, vacuum=True)
    assert counts["vacuum_skipped"] == 1
    assert counts["drift_snapshots"] == 2  # the prune itself still ran via the seam


def test_vacuum_backend_selector_reads_env(db, monkeypatch):
    """C5: the sqlite/postgres decision follows EXAMLOPS_DB_BACKEND (same as get_backend)."""
    import examlops.data.data_assets as da

    monkeypatch.delenv("EXAMLOPS_DB_BACKEND", raising=False)
    assert da._is_sqlite_backend() is True
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "postgres")
    assert da._is_sqlite_backend() is False
    monkeypatch.setenv("EXAMLOPS_DB_BACKEND", "SQLite")
    assert da._is_sqlite_backend() is True


def test_vacuum_still_runs_on_sqlite(db):
    """The default (sqlite) path keeps vacuuming — no skip note in the summary."""
    _seed(db, old_days=200, fresh=1, old=2)
    counts = db.purge_telemetry(90, vacuum=True)
    assert "vacuum_skipped" not in counts
    assert counts["drift_snapshots"] == 2
