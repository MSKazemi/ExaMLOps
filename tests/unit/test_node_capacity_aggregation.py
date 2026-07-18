"""SQL-side node-capacity aggregation + TTL cache (enterprise-readiness Phase 1, item 1.9).

At fleet scale (thousands of nodes) loading every `hpc_nodes` row into Python to sum it is
memory-heavy. `aggregate_node_capacity` does a `GROUP BY cluster, state` over the
`ix_hpc_nodes_cluster_state` index instead. This test proves the SQL rollup equals the
canonical Python `node_capacity` for the same data, that it's per-cluster, and that the TTL
cache collapses repeated reads while a fresh snapshot invalidates it.
"""

from __future__ import annotations

import pytest

from examlops.hpc_placement import node_capacity


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_DB", str(tmp_path / "platform.db"))
    import examlops.hpc_capacity as cap
    import examlops.platform_db as pdb

    pdb.init_db()
    cap.invalidate_capacity_cache()
    return pdb


def _nodes(spec):
    return [
        {"name": f"n{i}", "cpus": c, "memory_mb": 1000, "gpus": g, "state": s}
        for i, (s, g, c) in enumerate(spec)
    ]


def test_sql_aggregation_matches_python(db):
    nodes = _nodes([("idle", 4, 32), ("allocated", 4, 32), ("idle", 2, 16), ("down", 8, 64)])
    db.record_node_snapshot("lxp", "flux", nodes)

    agg = db.aggregate_node_capacity("lxp")["lxp"]
    py = node_capacity(nodes)
    assert agg["total_nodes"] == py["total_nodes"] == 4
    assert agg["total_gpus"] == py["total_gpus"] == 18
    assert agg["idle_gpus"] == py["idle_gpus"] == 6
    assert agg["idle_nodes"] == py["idle_nodes"] == 2
    assert agg["by_state"] == {"idle": 2, "allocated": 1, "down": 1}


def test_aggregation_is_per_cluster(db):
    db.record_node_snapshot("a", "flux", _nodes([("idle", 2, 8)]))
    db.record_node_snapshot("b", "slurm", _nodes([("allocated", 4, 16), ("idle", 1, 8)]))
    allc = db.aggregate_node_capacity()
    assert set(allc) == {"a", "b"}
    assert allc["a"]["total_gpus"] == 2
    assert allc["b"]["total_gpus"] == 5
    assert allc["b"]["idle_gpus"] == 1


def test_ttl_cache_serves_stale_within_window_then_invalidates_on_snapshot(db):
    import examlops.hpc_capacity as cap

    db.record_node_snapshot("lxp", "flux", _nodes([("idle", 4, 32)]))
    first = cap.capacity_summary("lxp", ttl=100, _now=1000.0)
    assert first["lxp"]["total_gpus"] == 4

    # Directly mutate the table WITHOUT going through record_node_snapshot → cache still serves old.
    with db.get_db() as conn:
        conn.execute("UPDATE hpc_nodes SET gpus = 8 WHERE cluster='lxp'")
    cached = cap.capacity_summary("lxp", ttl=100, _now=1050.0)  # within TTL
    assert cached["lxp"]["total_gpus"] == 4  # served from cache

    # A real snapshot invalidates the cache → fresh value.
    db.record_node_snapshot("lxp", "flux", _nodes([("idle", 8, 32)]))
    fresh = cap.capacity_summary("lxp", ttl=100, _now=1051.0)
    assert fresh["lxp"]["total_gpus"] == 8


def test_ttl_zero_disables_cache(db):
    import examlops.hpc_capacity as cap

    db.record_node_snapshot("lxp", "flux", _nodes([("idle", 4, 32)]))
    cap.capacity_summary("lxp", ttl=0)
    with db.get_db() as conn:
        conn.execute("UPDATE hpc_nodes SET gpus = 9 WHERE cluster='lxp'")
    # ttl=0 → no cache → reflects the live table immediately.
    assert cap.capacity_summary("lxp", ttl=0)["lxp"]["total_gpus"] == 9
