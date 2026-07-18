"""FleetScape tile aggregation (enterprise-readiness Phase 5, item 5.4).

Proves the server-side aggregation feeding the 3D/NOC heatmap: nodes bin into a positioned grid with
a 0–1 health per tile (down=0, idle=1), per-cluster + fleet rollups, and stable dimensions.
"""

from __future__ import annotations

from examlops.fleetscape import tile_grid


def _node(cluster, name, gpus, state):
    return {"cluster": cluster, "node": name, "gpus": gpus, "state": state}


def test_grid_positions_and_dims():
    nodes = [_node("a", f"n{i}", 8, "idle") for i in range(4)]
    grid = tile_grid(nodes, cols=2)
    assert grid["dims"] == {"rows": 2, "cols": 2}
    positions = {(t["row"], t["col"]) for t in grid["tiles"]}
    assert positions == {(0, 0), (0, 1), (1, 0), (1, 1)}


def test_health_reflects_state():
    grid = tile_grid([_node("a", "idle0", 8, "idle"), _node("a", "down0", 8, "down")])
    health = {t["node"]: t["health"] for t in grid["tiles"]}
    assert health["idle0"] == 1.0 and health["down0"] == 0.0


def test_cluster_and_fleet_rollups():
    nodes = [_node("a", "n0", 8, "idle"), _node("a", "n1", 8, "down"), _node("b", "n2", 4, "idle")]
    grid = tile_grid(nodes)
    assert grid["clusters"]["a"]["gpus"] == 16 and grid["clusters"]["a"]["nodes"] == 2
    assert grid["clusters"]["a"]["avg_health"] == 0.5  # (1.0 + 0.0)/2
    assert grid["summary"]["total_gpus"] == 20
    assert grid["summary"]["down_nodes"] == 1
    assert 0.0 <= grid["summary"]["fleet_health"] <= 1.0


def test_near_square_default_layout():
    grid = tile_grid([_node("a", f"n{i}", 1, "idle") for i in range(9)])
    assert grid["dims"] == {"rows": 3, "cols": 3}


def test_empty_fleet():
    grid = tile_grid([])
    assert grid["tiles"] == [] and grid["summary"]["nodes"] == 0
    assert grid["summary"]["fleet_health"] == 1.0
