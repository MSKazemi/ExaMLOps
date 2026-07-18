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
