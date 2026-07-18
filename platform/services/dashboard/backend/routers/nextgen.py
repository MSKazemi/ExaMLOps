"""Next-Gen 40 read surface — federated (E7), heterogeneous hardware (E8), autoscaling
(E5), distributed training (E6), inference gateway (E4), and feature views (A3) from the
shared platform.db.

Read-only, viewer-gated, and fail-open (a missing table / absent DB yields an empty result
rather than a 500) — the same contract as the other platform-data routers, so the dashboard
degrades gracefully when a feature has never been exercised.
"""

from __future__ import annotations

import json
import os

from auth import require_role
from dbconn import connect
from fastapi import APIRouter, Depends

router = APIRouter(prefix="/nextgen", tags=["nextgen-40"])
_viewer = require_role("viewer")


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read query, returning [] on any error (fail-open)."""
    try:
        conn = connect(_db_path())
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── E7 federated & privacy-preserving training ───────────────────────────────
@router.get("/federated/runs")
async def federated_runs(_=Depends(_viewer)) -> list[dict]:
    """All federated runs with their DP/secure-agg config + progress."""
    return _query(
        "SELECT run_id, strategy, dp_enabled, secure_agg, epsilon, delta, "
        "rounds_completed, status FROM federated_runs ORDER BY created_at DESC"
    )


@router.get("/federated/runs/{run_id}")
async def federated_run_detail(run_id: str, _=Depends(_viewer)) -> dict:
    """One federated run: config, sites (authorization), and completed rounds."""
    runs = _query("SELECT * FROM federated_runs WHERE run_id=?", (run_id,))
    return {
        "run": runs[0] if runs else None,
        "sites": _query(
            "SELECT site, authorized FROM federated_sites WHERE run_id=? ORDER BY site", (run_id,)
        ),
        "rounds": _query(
            "SELECT round_num, global_metric, sites_participated, epsilon "
            "FROM federated_rounds WHERE run_id=? ORDER BY round_num",
            (run_id,),
        ),
    }


# ── E8 heterogeneous hardware & hybrid HPC↔cloud ─────────────────────────────
@router.get("/hardware/pools")
async def device_pools(_=Depends(_viewer)) -> list[dict]:
    """Registered device pools (HPC + cloud, any accelerator)."""
    rows = _query(
        "SELECT name, target, accelerator, capabilities, count, region, cost_per_hour, "
        "carbon_factor, supports_fractions, status FROM device_pools ORDER BY cost_per_hour, name"
    )
    for r in rows:
        r["capabilities"] = json.loads(r["capabilities"]) if r.get("capabilities") else []
        r["supports_fractions"] = bool(r["supports_fractions"])
    return rows


@router.get("/hardware/placements")
async def placement_decisions(limit: int = 50, _=Depends(_viewer)) -> list[dict]:
    """Recent placement decisions (placed / fallback / rejected)."""
    return _query(
        "SELECT workload, accelerator_requested, device_chosen, pool, target, region, "
        "decision, fraction_honored, reason, created_at "
        "FROM placement_decisions ORDER BY id DESC LIMIT ?",
        (limit,),
    )


@router.get("/hardware/bursts")
async def burst_events(limit: int = 50, _=Depends(_viewer)) -> list[dict]:
    """Recent HPC→cloud burst attempts (allowed / blocked-by-residency)."""
    return _query(
        "SELECT workload, from_pool, to_pool, residency, allowed, reason, created_at "
        "FROM burst_events ORDER BY id DESC LIMIT ?",
        (limit,),
    )


# ── E5 autoscaling & scale-to-zero ───────────────────────────────────────────
@router.get("/autoscale/config")
async def autoscale_config(_=Depends(_viewer)) -> list[dict]:
    """Per-model autoscaling policies."""
    return _query("SELECT * FROM autoscale_config ORDER BY model")


@router.get("/autoscale/events")
async def scale_events(limit: int = 50, _=Depends(_viewer)) -> list[dict]:
    """Recent scale up/down/to-zero events."""
    return _query("SELECT * FROM scale_events ORDER BY id DESC LIMIT ?", (limit,))


# ── E6 distributed & fault-tolerant training ─────────────────────────────────
@router.get("/distributed/runs")
async def distributed_runs(_=Depends(_viewer)) -> list[dict]:
    """Distributed training runs + status."""
    return _query("SELECT * FROM distributed_runs ORDER BY created_at DESC")


# ── E4 inference gateway & KV routing ────────────────────────────────────────
@router.get("/gateway/config")
async def gateway_config(_=Depends(_viewer)) -> list[dict]:
    """Per-model inference-routing config (round-robin / cache-aware)."""
    return _query("SELECT * FROM inference_gateway_config ORDER BY model, tenant")


# ── A3 feature store ─────────────────────────────────────────────────────────
@router.get("/features/views")
async def feature_views(_=Depends(_viewer)) -> list[dict]:
    """Registered feature views (one train/serve definition)."""
    return _query("SELECT * FROM feature_views ORDER BY name")


# ── roll-up ──────────────────────────────────────────────────────────────────
@router.get("/summary")
async def summary(_=Depends(_viewer)) -> dict:
    """Counts across the Next-Gen 40 surfaces for a console overview tile."""

    def _count(table: str) -> int:
        rows = _query(f"SELECT COUNT(*) AS n FROM {table}")
        return rows[0]["n"] if rows else 0

    return {
        "federated_runs": _count("federated_runs"),
        "device_pools": _count("device_pools"),
        "placements": _count("placement_decisions"),
        "autoscale_configs": _count("autoscale_config"),
        "distributed_runs": _count("distributed_runs"),
        "feature_views": _count("feature_views"),
    }
