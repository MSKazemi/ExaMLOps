"""FleetScape — server-side tile aggregation for the 3D/NOC fleet view (Phase 5 item 5.4).

The bundled WebGL rack/GPU heatmap (and the big-screen NOC wall) need the fleet reduced to a compact,
positioned grid of tiles the browser can paint — computing that per-frame from thousands of raw nodes
in the client would melt it. This module does the aggregation **server-side**: it bins the fleet
registry into a deterministic grid, computes a 0–1 health score per tile for the heatmap colour, and
rolls up per-cluster + fleet summaries. The 3D view renders this; where WebGL is unavailable it
degrades to the existing SVG grid over the same tiles.

Pure over injected nodes → testable; the live wrapper reads the registry.
"""

from __future__ import annotations

import math
from typing import Any

# State → health score (1.0 = ideal, 0 = down). Drives the heatmap colour ramp.
_STATE_HEALTH = {
    "idle": 1.0,
    "mixed": 0.7,
    "allocated": 0.6,
    "drain": 0.3,
    "down": 0.0,
    "unknown": 0.5,
}


def _health(node: dict[str, Any]) -> float:
    return _STATE_HEALTH.get((node.get("state") or "unknown").lower(), 0.5)


def tile_grid(nodes: list[dict[str, Any]], *, cols: int | None = None) -> dict[str, Any]:
    """Bin ``nodes`` into a positioned tile grid for the 3D/NOC heatmap (item 5.4).

    Nodes are grouped by cluster then laid out row-major into a near-square grid (``cols`` overrides
    the width). Each tile carries its (row, col) position, node/cluster id, GPUs, state, and a 0–1
    ``health`` for colouring. Returns ``{tiles, dims: {rows, cols}, clusters: {...}, summary: {...}}``.
    """
    ordered = sorted(
        nodes, key=lambda n: (n.get("cluster", ""), n.get("node") or n.get("name") or "")
    )
    n = len(ordered)
    width = cols or max(1, math.ceil(math.sqrt(n))) if n else 1
    tiles: list[dict[str, Any]] = []
    clusters: dict[str, dict[str, Any]] = {}
    total_gpus = down = 0
    for i, node in enumerate(ordered):
        cluster = node.get("cluster", "default")
        gpus = node.get("gpus") or 0
        health = _health(node)
        tiles.append(
            {
                "row": i // width,
                "col": i % width,
                "node": node.get("node") or node.get("name"),
                "cluster": cluster,
                "gpus": gpus,
                "state": node.get("state") or "unknown",
                "health": round(health, 3),
            }
        )
        c = clusters.setdefault(cluster, {"nodes": 0, "gpus": 0, "health_sum": 0.0})
        c["nodes"] += 1
        c["gpus"] += gpus
        c["health_sum"] += health
        total_gpus += gpus
        down += 1 if health == 0.0 else 0
    for c in clusters.values():
        c["avg_health"] = round(c["health_sum"] / c["nodes"], 3) if c["nodes"] else 0.0
        del c["health_sum"]
    rows = (n + width - 1) // width if n else 0
    fleet_health = round(sum(t["health"] for t in tiles) / n, 3) if n else 1.0
    return {
        "tiles": tiles,
        "dims": {"rows": rows, "cols": width},
        "clusters": clusters,
        "summary": {
            "nodes": n,
            "total_gpus": total_gpus,
            "down_nodes": down,
            "fleet_health": fleet_health,
        },
    }


def fleet_heatmap(cluster: str | None = None, *, cols: int | None = None) -> dict[str, Any]:
    """Read the registry's node snapshot and build the tile grid (live wrapper)."""
    from examlops.data import init_db
    from examlops.data.hpc import get_node_snapshot

    init_db()
    return tile_grid(get_node_snapshot(cluster), cols=cols)
