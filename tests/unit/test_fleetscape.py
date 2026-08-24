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
    # Not 1.0. A fleet with no nodes has no health to report, and this assertion used to pin the
    # opposite — see the block below for why that mattered on a wall-mounted heatmap.
    assert grid["summary"]["fleet_health"] is None


# ── an empty fleet is not a healthy fleet (T88) ─────────────────────────────────────────────
#
# `fleet_health` averages a 0–1 per-node score and drives the heatmap colour ramp on the NOC wall.
# With no nodes there is nothing to average, and the old code answered 1.0 — the value reserved for
# a fleet where every node is idle and well. The states that produce an empty snapshot are exactly
# the ones worth seeing: discovery never ran, the probe failed, the registry was wiped, or a
# `--cluster` filter matched nothing. Each of them painted the display its most reassuring colour.


def test_a_fleet_with_no_nodes_reports_no_health_rather_than_perfect_health():
    summary = tile_grid([])["summary"]
    assert summary["fleet_health"] is None
    assert summary["nodes"] == 0


def test_a_cluster_filter_that_matches_nothing_does_not_report_perfect_health():
    """The same empty snapshot `exa fleet heatmap --cluster typo` produces."""
    assert tile_grid([])["summary"]["fleet_health"] is None


def test_a_fleet_whose_nodes_are_all_down_still_reports_zero_not_none():
    """The control: 0.0 means measured-and-terrible and must stay distinguishable from None."""
    grid = tile_grid([_node("a", "n0", 8, "down"), _node("a", "n1", 8, "down")])
    assert grid["summary"]["fleet_health"] == 0.0
    assert grid["summary"]["down_nodes"] == 2


def test_a_healthy_fleet_still_reports_one():
    """The other control: the reading that used to be ambiguous still means what it says."""
    grid = tile_grid([_node("a", f"n{i}", 8, "idle") for i in range(3)])
    assert grid["summary"]["fleet_health"] == 1.0
