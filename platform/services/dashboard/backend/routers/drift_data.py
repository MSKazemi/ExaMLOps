"""Drift monitoring data — reads from shared platform.db drift tables."""
from __future__ import annotations

import json
import math
import os
import sqlite3

from auth import require_role
from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/drift", tags=["drift"])
_viewer = require_role("viewer")

_SNAPSHOT_WINDOW = 100
_WARN_Z = 2.0
_CRIT_Z = 3.0


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", "/repo/platform.db")


def _compute_stats(values: list) -> dict:
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return {"mean": mean, "std": math.sqrt(variance), "n": n}


@router.get("/status")
async def drift_status(
    _=Depends(_viewer),
    model: str | None = Query(None),
) -> list[dict]:
    """Prediction drift status for all models (or one model)."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        if model:
            models_list = [model]
        else:
            rows = conn.execute("SELECT DISTINCT model FROM drift_snapshots").fetchall()
            models_list = [r["model"] for r in rows]
        results = []
        for m in models_list:
            snap_rows = conn.execute(
                "SELECT prediction FROM drift_snapshots WHERE model=? "
                "ORDER BY ts DESC, rowid DESC LIMIT ?",
                (m, _SNAPSHOT_WINDOW),
            ).fetchall()
            preds = [r["prediction"] for r in snap_rows]
            if not preds:
                continue
            live = _compute_stats(preds)
            bl_row = conn.execute(
                "SELECT stats FROM drift_baselines WHERE model=?", (m,)
            ).fetchone()
            baseline = json.loads(bl_row["stats"]) if bl_row else None
            if baseline is None or baseline.get("std", 0) == 0:
                z = 0.0
                status = "OK (no baseline)"
            else:
                z = abs(live["mean"] - baseline["mean"]) / baseline["std"]
                status = "CRITICAL" if z >= _CRIT_Z else ("WARNING" if z >= _WARN_Z else "OK")
            results.append({
                "model": m,
                "live_mean": round(live["mean"], 3),
                "live_std": round(live["std"], 3),
                "baseline_mean": round(baseline["mean"], 3) if baseline else None,
                "z_score": round(z, 2),
                "status": status,
                "n_snapshots": len(preds),
            })
        conn.close()
        return results
    except Exception:
        return []


@router.get("/auto-retrain")
async def drift_auto_retrain(_=Depends(_viewer)) -> list[dict]:
    """Auto-retrain configuration for all models."""
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM drift_auto_retrain").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


@router.get("/input-status")
async def input_drift_status(
    _=Depends(_viewer),
    model: str | None = Query(None),
) -> list[dict]:
    """Input embedding distribution drift status."""
    INPUT_WINDOW = 200
    try:
        conn = sqlite3.connect(_db_path())
        conn.row_factory = sqlite3.Row
        if model:
            models_list = [model]
        else:
            rows = conn.execute("SELECT DISTINCT model FROM input_snapshots").fetchall()
            models_list = [r["model"] for r in rows]
        results = []
        for m in models_list:
            snap_rows = conn.execute(
                "SELECT emb_norm, emb_mean, emb_std FROM input_snapshots "
                "WHERE model=? ORDER BY ts DESC LIMIT ?", (m, INPUT_WINDOW)
            ).fetchall()
            if not snap_rows:
                continue
            norms = [r["emb_norm"] for r in snap_rows]
            means = [r["emb_mean"] for r in snap_rows]
            stds = [r["emb_std"] for r in snap_rows]
            live = {
                "norm_mean": sum(norms)/len(norms),
                "mean_mean": sum(means)/len(means),
                "std_mean": sum(stds)/len(stds),
            }
            bl_row = conn.execute(
                "SELECT stats FROM input_baselines WHERE model=?", (m,)
            ).fetchone()
            baseline = json.loads(bl_row["stats"]) if bl_row else None
            if baseline is None:
                max_z, status = 0.0, "OK (no baseline)"
            else:
                zs = []
                for metric in ("norm_mean", "mean_mean", "std_mean"):
                    bstd = baseline.get(f"{metric}_std", 0.0)
                    if bstd > 0:
                        zs.append(abs(live[metric] - baseline[metric]) / bstd)
                max_z = max(zs) if zs else 0.0
                status = "CRITICAL" if max_z >= _CRIT_Z else ("WARNING" if max_z >= _WARN_Z else "OK")
            results.append({
                "model": m, "live_norm_mean": round(live["norm_mean"], 3),
                "live_emb_mean": round(live["mean_mean"], 4),
                "live_emb_std": round(live["std_mean"], 4),
                "max_z": round(max_z, 2), "status": status, "n_snapshots": len(snap_rows),
            })
        conn.close()
        return results
    except Exception:
        return []
