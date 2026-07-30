"""Drift monitoring data — reads/writes the shared platform.db drift tables.

Reads (viewer): prediction/input drift status + auto-retrain config. Writes (admin + the
``drift.baseline`` capability, audited): set a prediction or input baseline, clear snapshots, and
enable/disable drift-triggered auto-retrain — the same operations as
``exa drift baseline|reset|auto-retrain|input baseline|input reset``, reusing the
``examlops.data.drift`` code paths so the dashboard can't drift from the CLI.
"""

from __future__ import annotations

import json
import math
import os

from auth import require_role
from capabilities import DRIFT_BASELINE, can, deny_reason
from dbconn import connect
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

router = APIRouter(prefix="/drift", tags=["drift"])
_viewer = require_role("viewer")
_admin = require_role("admin")

_SNAPSHOT_WINDOW = 100
_BASELINE_WINDOW = 100
_MIN_BASELINE_SNAPSHOTS = 10
# Input-drift baseline: mirror the CLI (`exa drift input baseline`) window + minimum exactly, so a
# baseline set from the dashboard is byte-identical to one set from the CLI.
_INPUT_BASELINE_WINDOW = 1000
_MIN_INPUT_SNAPSHOTS = 10
_WARN_Z = 2.0
_CRIT_Z = 3.0


def _require_manage(principal: dict) -> None:
    """Enforce the ``drift.baseline`` capability (F15), matching the connections/projects routers.

    The route already depends on the admin role; this adds the named-capability gate so the check
    is the single, consistent enforcement point and future finer-grained roles work unchanged.
    """
    role = principal.get("role", "")
    if not can(role, DRIFT_BASELINE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, deny_reason(role, DRIFT_BASELINE))


def _audit(conn, actor: str, action: str, target: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
        ("dashboard", actor, action, target, json.dumps(details)),
    )


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
        conn = connect(_db_path())
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
            results.append(
                {
                    "model": m,
                    "live_mean": round(live["mean"], 3),
                    "live_std": round(live["std"], 3),
                    "baseline_mean": round(baseline["mean"], 3) if baseline else None,
                    "z_score": round(z, 2),
                    "status": status,
                    "n_snapshots": len(preds),
                }
            )
        conn.close()
        return results
    except Exception:
        return []


@router.get("/auto-retrain")
async def drift_auto_retrain(_=Depends(_viewer)) -> list[dict]:
    """Auto-retrain configuration for all models."""
    try:
        conn = connect(_db_path())
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
        conn = connect(_db_path())
        if model:
            models_list = [model]
        else:
            rows = conn.execute("SELECT DISTINCT model FROM input_snapshots").fetchall()
            models_list = [r["model"] for r in rows]
        results = []
        for m in models_list:
            snap_rows = conn.execute(
                "SELECT emb_norm, emb_mean, emb_std FROM input_snapshots "
                "WHERE model=? ORDER BY ts DESC LIMIT ?",
                (m, INPUT_WINDOW),
            ).fetchall()
            if not snap_rows:
                continue
            norms = [r["emb_norm"] for r in snap_rows]
            means = [r["emb_mean"] for r in snap_rows]
            stds = [r["emb_std"] for r in snap_rows]
            live = {
                "norm_mean": sum(norms) / len(norms),
                "mean_mean": sum(means) / len(means),
                "std_mean": sum(stds) / len(stds),
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
                status = (
                    "CRITICAL" if max_z >= _CRIT_Z else ("WARNING" if max_z >= _WARN_Z else "OK")
                )
            results.append(
                {
                    "model": m,
                    "live_norm_mean": round(live["norm_mean"], 3),
                    "live_emb_mean": round(live["mean_mean"], 4),
                    "live_emb_std": round(live["std_mean"], 4),
                    "max_z": round(max_z, 2),
                    "status": status,
                    "n_snapshots": len(snap_rows),
                }
            )
        conn.close()
        return results
    except Exception:
        return []


# ── writes (admin, audited) ───────────────────────────────────────────────────


def _examlops_drift():
    """Lazy, guarded import of the shared CLI drift code path (503 if unavailable)."""
    try:
        from examlops.data import drift as _d  # type: ignore

        return _d
    except ImportError as exc:  # pragma: no cover - only when examlops is not installed
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "drift writes require the examlops package (not available in this deployment)",
        ) from exc


@router.post("/baseline/{model}")
async def set_baseline(
    model: str,
    dry_run: bool = Query(False),
    principal: dict = Depends(_admin),
) -> dict:
    """Store the current rolling stats as the drift baseline for a model (admin; audited).

    Mirrors ``exa drift baseline``: needs ≥10 recent snapshots. ``?dry_run=true`` previews the
    baseline without writing it.
    """
    _require_manage(principal)
    conn = connect(_db_path())
    rows = conn.execute(
        "SELECT prediction FROM drift_snapshots WHERE model=? ORDER BY ts DESC, rowid DESC LIMIT ?",
        (model, _BASELINE_WINDOW),
    ).fetchall()
    preds = [r["prediction"] for r in rows]
    if len(preds) < _MIN_BASELINE_SNAPSHOTS:
        conn.close()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Need at least {_MIN_BASELINE_SNAPSHOTS} snapshots, have {len(preds)}. "
            "Run the bridge to collect predictions first.",
        )
    stats = _compute_stats(preds)
    if dry_run:
        conn.close()
        return {
            "dryRun": True,
            "model": model,
            "wouldSet": {k: round(v, 4) for k, v in stats.items()},
        }
    conn.close()
    drift = _examlops_drift()
    drift.set_drift_baseline(model, stats)
    conn = connect(_db_path())
    _audit(conn, principal.get("sub", "?"), "drift_baseline_set", model, stats)
    conn.commit()
    conn.close()
    return {"model": model, "baseline": {k: round(v, 4) for k, v in stats.items()}}


@router.post("/reset/{model}")
async def reset_snapshots(
    model: str,
    dry_run: bool = Query(False),
    principal: dict = Depends(_admin),
) -> dict:
    """Clear all drift snapshots for a model — keeps the baseline (admin; audited).

    Mirrors ``exa drift reset``. ``?dry_run=true`` reports the count without deleting.
    """
    _require_manage(principal)
    conn = connect(_db_path())
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM drift_snapshots WHERE model=?", (model,)
    ).fetchone()["c"]
    if dry_run:
        conn.close()
        return {"dryRun": True, "model": model, "wouldClear": n}
    if n:
        conn.execute("DELETE FROM drift_snapshots WHERE model=?", (model,))
        _audit(conn, principal.get("sub", "?"), "drift_reset", model, {"cleared": n})
        conn.commit()
    conn.close()
    return {"model": model, "cleared": n}


@router.post("/auto-retrain/{model}")
async def configure_auto_retrain(
    model: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
) -> dict:
    """Enable/disable drift-triggered auto-retrain for a model (admin; audited).

    Body: ``{enabled: bool, dataset?: str, minZ?: float, cooldown?: int}``. Mirrors
    ``exa drift auto-retrain enable|disable`` via ``examlops.data.drift.set_drift_auto_retrain``.
    A dataset is required when enabling.
    """
    _require_manage(principal)
    enabled = bool(payload.get("enabled"))
    dataset = (payload.get("dataset") or "").strip()
    min_z = float(payload.get("minZ", 3.0))
    cooldown = int(payload.get("cooldown", 3600))
    if enabled and not dataset:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "dataset is required to enable auto-retrain"
        )
    drift = _examlops_drift()
    drift.set_drift_auto_retrain(
        model, enabled=enabled, min_z_score=min_z, dataset_name=dataset, cooldown_s=cooldown
    )
    conn = connect(_db_path())
    _audit(
        conn,
        principal.get("sub", "?"),
        "drift_auto_retrain_configured",
        model,
        {"enabled": enabled, "dataset": dataset, "minZ": min_z, "cooldown": cooldown},
    )
    conn.commit()
    conn.close()
    return {
        "model": model,
        "enabled": enabled,
        "dataset": dataset,
        "minZ": min_z,
        "cooldown": cooldown,
    }


def _pop_mean_std(values: list[float]) -> tuple[float, float]:
    """Population mean + std — byte-identical to the CLI ``input baseline`` computation."""
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / n)
    return mean, std


@router.post("/input-baseline/{model}")
async def set_input_baseline_view(
    model: str,
    dry_run: bool = Query(False),
    principal: dict = Depends(_admin),
) -> dict:
    """Store the current rolling embedding stats as the input-drift baseline (admin; audited).

    Mirrors ``exa drift input baseline``: needs ≥10 recent input snapshots, uses the same 1000-row
    window and the same stat shape, and persists via ``examlops.data.drift.set_input_baseline`` so
    the baseline is identical to the CLI's. ``?dry_run=true`` previews without writing.
    """
    _require_manage(principal)
    conn = connect(_db_path())
    rows = conn.execute(
        "SELECT emb_norm, emb_mean, emb_std FROM input_snapshots WHERE model=? "
        "ORDER BY ts DESC LIMIT ?",
        (model, _INPUT_BASELINE_WINDOW),
    ).fetchall()
    conn.close()
    if len(rows) < _MIN_INPUT_SNAPSHOTS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Need at least {_MIN_INPUT_SNAPSHOTS} input snapshots, have {len(rows)}. "
            "Run the bridge to collect embeddings first.",
        )
    norm_mean, norm_std = _pop_mean_std([r["emb_norm"] for r in rows])
    mean_mean, mean_std = _pop_mean_std([r["emb_mean"] for r in rows])
    std_mean, std_std = _pop_mean_std([r["emb_std"] for r in rows])
    stats = {
        "norm_mean": norm_mean,
        "norm_mean_std": norm_std,
        "mean_mean": mean_mean,
        "mean_mean_std": mean_std,
        "std_mean": std_mean,
        "std_mean_std": std_std,
        "n": float(len(rows)),
    }
    if dry_run:
        return {
            "dryRun": True,
            "model": model,
            "wouldSet": {k: round(v, 4) for k, v in stats.items()},
        }
    drift = _examlops_drift()
    drift.set_input_baseline(model, stats)
    conn = connect(_db_path())
    _audit(conn, principal.get("sub", "?"), "input_baseline_set", model, {"n": len(rows)})
    conn.commit()
    conn.close()
    return {"model": model, "baseline": {k: round(v, 4) for k, v in stats.items()}}


@router.post("/input-reset/{model}")
async def reset_input_snapshots(
    model: str,
    dry_run: bool = Query(False),
    principal: dict = Depends(_admin),
) -> dict:
    """Clear all input embedding snapshots for a model — keeps the baseline (admin; audited).

    Mirrors ``exa drift input reset``. ``?dry_run=true`` reports the count without deleting.
    """
    _require_manage(principal)
    conn = connect(_db_path())
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM input_snapshots WHERE model=?", (model,)
    ).fetchone()["c"]
    if dry_run:
        conn.close()
        return {"dryRun": True, "model": model, "wouldClear": n}
    if n:
        conn.execute("DELETE FROM input_snapshots WHERE model=?", (model,))
        _audit(conn, principal.get("sub", "?"), "input_reset", model, {"cleared": n})
        conn.commit()
    conn.close()
    return {"model": model, "cleared": n}
