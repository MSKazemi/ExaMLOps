"""Software-defined asset views (ADR 0036 clause 5).

Read-only views of the asset DAG the platform keeps in ``platform.db`` (`exa assets`): every asset
with its kind, version, upstream dependencies, dependents, and **freshness** — whether it is stale
and why (never materialized, an upstream changed, an upstream is itself stale) — computed by the
same ``examlops.assets.asset_status`` the CLI uses, so the page and ``exa assets status`` cannot
disagree.

No materialize action here, deliberately. Materializing runs an asset's production function or
submits it to the HPC scheduler; the dashboard process holds none of those functions, and starting
cluster jobs from a web click is a governance decision of its own (D5 policy, approval), not a
side effect of adding a view. ``exa assets materialize`` remains the way to rebuild.
"""

from __future__ import annotations

import asyncio
from typing import Any

from auth import require_role
from fastapi import APIRouter, Depends, HTTPException, status

router = APIRouter(prefix="/assets", tags=["assets"])
_viewer = require_role("viewer")


def _examlops_assets() -> Any:
    try:
        from examlops import assets as _a

        return _a
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "asset views require the examlops package (not available in this deployment)",
        ) from exc


def _graph() -> list[dict[str, Any]]:
    a = _examlops_assets()
    from examlops.data.data_assets import list_assets

    rows = list_assets()
    declared = {r["name"] for r in rows}
    dependents: dict[str, list[str]] = {r["name"]: [] for r in rows}
    for r in rows:
        for dep in r["deps"]:
            dependents.setdefault(dep, []).append(r["name"])
    out: list[dict[str, Any]] = []
    for r in rows:
        fresh = a.asset_status(r["name"])
        out.append(
            {
                "name": r["name"],
                "kind": r.get("kind") or "model",
                "description": r.get("description"),
                "version": int(r.get("current_version") or 0),
                "lastMaterializedAt": r.get("last_materialized_at"),
                "deps": list(r["deps"]),
                "dependents": sorted(dependents.get(r["name"], [])),
                "fresh": fresh.fresh,
                "reasons": fresh.reasons,
                # An upstream named in `deps` that was never declared is a dangling edge — shown,
                # not dropped, because a DAG that hides a missing input looks complete when it isn't.
                "undeclaredDeps": [d for d in r["deps"] if d not in declared],
            }
        )
    return out


@router.get("")
async def list_asset_graph(_=Depends(_viewer)) -> dict[str, Any]:
    """The asset DAG with freshness: ``{assets: [...], counts: {...}}``."""
    assets = await asyncio.to_thread(_graph)
    stale = sum(1 for x in assets if not x["fresh"])
    return {
        "assets": assets,
        "counts": {"total": len(assets), "stale": stale, "fresh": len(assets) - stale},
    }


@router.get("/{name}")
async def asset_detail(name: str, _=Depends(_viewer)) -> dict[str, Any]:
    """One asset with its freshness and its direct upstreams/downstreams."""
    for item in await asyncio.to_thread(_graph):
        if item["name"] == name:
            return item
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"asset '{name}' is not declared")
