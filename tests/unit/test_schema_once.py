"""Schema bootstrap runs once per process per DB path (Phase 0 item 0.5 / QW8).

`init_db()` is called defensively by ~165 helpers; re-running the 100+-table DDL on every call
churned a write lock on hot read paths. A process-level sentinel now short-circuits after the first
call per `PLATFORM_DB` path, while `force=True` still re-runs and a *different* path re-initializes.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pdb(monkeypatch):
    import examlops.platform_db as _pdb

    return _pdb


def _has_table(pdb, name: str) -> bool:
    with pdb.get_db() as c:
        return (
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            is not None
        )


def test_first_call_creates_schema_and_caches_path(pdb, tmp_path, monkeypatch):
    p = tmp_path / "a.db"
    monkeypatch.setenv("PLATFORM_DB", str(p))
    pdb.init_db()
    assert str(p) in pdb._INITIALIZED_PATHS
    assert _has_table(pdb, "audit_events")
    # A second call short-circuits without error and the schema is still present.
    pdb.init_db()
    assert _has_table(pdb, "audit_events")


def test_distinct_path_reinitializes(pdb, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "a.db"))
    pdb.init_db()
    p2 = tmp_path / "b.db"
    monkeypatch.setenv("PLATFORM_DB", str(p2))
    pdb.init_db()
    assert str(p2) in pdb._INITIALIZED_PATHS
    assert _has_table(pdb, "audit_events")


def test_hpc_nodes_cluster_state_index_exists(pdb, tmp_path, monkeypatch):
    """Fleet capacity queries filter hpc_nodes by (cluster, state) — the index must be created."""
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "idx.db"))
    pdb.init_db()
    with pdb.get_db() as c:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_hpc_nodes_cluster_state'"
        ).fetchone()
    assert row is not None


def test_force_recreates_after_drop(pdb, tmp_path, monkeypatch):
    p = tmp_path / "c.db"
    monkeypatch.setenv("PLATFORM_DB", str(p))
    pdb.init_db()
    with pdb.get_db() as c:
        c.execute("DROP TABLE audit_events")
    # Cached ⇒ a plain init_db() does NOT recreate (documented behavior).
    pdb.init_db()
    assert not _has_table(pdb, "audit_events")
    # force=True re-runs the DDL and restores it.
    pdb.init_db(force=True)
    assert _has_table(pdb, "audit_events")
