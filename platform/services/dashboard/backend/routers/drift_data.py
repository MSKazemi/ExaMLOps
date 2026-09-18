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

import audit_write
from auth import require_role
from capabilities import DRIFT_BASELINE, can, deny_reason, require_capability
from dbconn import connect, platform_db_path, postgres_configured
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from readfail import readable

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
    audit_write.audit(actor, action, target, details, conn=conn)


def _db_path() -> str:
    return platform_db_path()


#: One statement that ranks every model's rows by recency. `ts` has whole-second resolution, so
#: `rowid` breaks ties the way the per-model `LIMIT` used to; both are portable (SQLite has had
#: window functions since 3.25, and the Postgres layer translates `rowid`).
_WINDOWED = (
    "SELECT model, {columns} FROM ("
    " SELECT model, {columns},"
    " ROW_NUMBER() OVER (PARTITION BY model ORDER BY ts DESC, rowid DESC) AS rn"
    " FROM {table}"
    ") ranked WHERE rn <= ?"
)

#: The same window, one model at a time, straight down the `(model, ts)` index.
_PER_MODEL = "SELECT {columns} FROM {table} WHERE model=? ORDER BY ts DESC, rowid DESC LIMIT ?"


def _latest_per_model(conn, table: str, columns: str, models: list[str], window: int) -> dict:
    """The newest `window` rows for each of `models`, as `{model: [row, …]}`.

    **Which query is cheaper depends on the engine, and by a factor either way.** Measured over
    250 000 snapshots across 50 models:

    | | one statement per model | one windowed statement |
    |---|---|---|
    | SQLite | **47 ms** | 129 ms |
    | Postgres | 134–148 ms | **70 ms** |

    A statement is an in-process call on SQLite, so the per-model form wins there: each one walks
    the `(model, ts)` index and stops after `window` rows, while the window function has to rank
    every row in the table before discarding all but the newest few. On Postgres each statement is
    a network round trip, and 50 of them cost more than one query the planner can parallelise —
    which is the reverse.

    So this dispatches on the engine. It is the one place in the dashboard where that is true, and
    it is about *cost*, never about dialect: the SQL both branches send is the same SQLite-shaped
    SQL the translation layer handles everywhere else.
    """
    if postgres_configured():
        sql = _WINDOWED.format(columns=columns, table=table)
        wanted = set(models)
        out: dict[str, list] = {}
        for row in conn.execute(sql, (window,)).fetchall():
            if row["model"] in wanted:
                out.setdefault(row["model"], []).append(row)
        return out
    sql = _PER_MODEL.format(columns=columns, table=table)
    return {m: conn.execute(sql, (m, window)).fetchall() for m in models}


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
    with readable("the drift register"):
        conn = connect(_db_path())
        try:
            if model:
                models_list = [model]
            else:
                rows = conn.execute("SELECT DISTINCT model FROM drift_snapshots").fetchall()
                models_list = [r["model"] for r in rows]
            # Two statements for every model, not two per model. This was a query for each
            # model's window plus a query for each model's baseline — 101 statements for 50
            # models, each a network round trip on Postgres. The window function is portable:
            # SQLite has had it since 3.25, and `rowid` is translated for Postgres.
            rows_by_model = _latest_per_model(
                conn, "drift_snapshots", "prediction", models_list, _SNAPSHOT_WINDOW
            )
            windows = {m: [r["prediction"] for r in rs] for m, rs in rows_by_model.items()}
            baselines = {
                r["model"]: r["stats"]
                for r in conn.execute("SELECT model, stats FROM drift_baselines").fetchall()
            }
            results = []
            for m in models_list:
                preds = windows.get(m, [])
                if not preds:
                    continue
                live = _compute_stats(preds)
                raw = baselines.get(m)
                baseline = json.loads(raw) if raw else None
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
        finally:
            conn.close()
    return results


@router.get("/auto-retrain")
async def drift_auto_retrain(_=Depends(_viewer)) -> list[dict]:
    """Auto-retrain configuration for all models."""
    with readable("the auto-retrain register"):
        conn = connect(_db_path())
        try:
            rows = conn.execute("SELECT * FROM drift_auto_retrain").fetchall()
            configs = [dict(r) for r in rows]
        finally:
            conn.close()
    return configs


@router.get("/input-status")
async def input_drift_status(
    _=Depends(_viewer),
    model: str | None = Query(None),
) -> list[dict]:
    """Input embedding distribution drift status."""
    INPUT_WINDOW = 200
    with readable("the input-drift register"):
        conn = connect(_db_path())
        try:
            if model:
                models_list = [model]
            else:
                rows = conn.execute("SELECT DISTINCT model FROM input_snapshots").fetchall()
                models_list = [r["model"] for r in rows]
            # As in `drift_status` above: one windowed read and one baseline read for every
            # model, rather than two queries per model.
            windows = _latest_per_model(
                conn, "input_snapshots", "emb_norm, emb_mean, emb_std", models_list, INPUT_WINDOW
            )
            baselines = {
                r["model"]: r["stats"]
                for r in conn.execute("SELECT model, stats FROM input_baselines").fetchall()
            }
            results = []
            for m in models_list:
                snap_rows = windows.get(m, [])
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
                raw = baselines.get(m)
                baseline = json.loads(raw) if raw else None
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
                        "CRITICAL"
                        if max_z >= _CRIT_Z
                        else ("WARNING" if max_z >= _WARN_Z else "OK")
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
        finally:
            conn.close()
    return results


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
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(DRIFT_BASELINE)),
) -> dict:
    """Store the current rolling stats as the drift baseline for a model (admin; audited).

    Mirrors ``exa drift baseline``: needs ≥10 recent snapshots. ``?dry_run=true`` previews the
    baseline without writing it.
    """
    _require_manage(principal)
    conn = connect(_db_path())
    try:
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
    finally:
        conn.close()
    conn = connect(_db_path())
    try:
        _audit(conn, principal.get("sub", "?"), "drift_baseline_set", model, stats)
        conn.commit()
        conn.close()
        return {"model": model, "baseline": {k: round(v, 4) for k, v in stats.items()}}
    finally:
        conn.close()


@router.post("/reset/{model}")
async def reset_snapshots(
    model: str,
    dry_run: bool = Query(False),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(DRIFT_BASELINE)),
) -> dict:
    """Clear all drift snapshots for a model — keeps the baseline (admin; audited).

    Mirrors ``exa drift reset``. ``?dry_run=true`` reports the count without deleting.
    """
    _require_manage(principal)
    conn = connect(_db_path())
    try:
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
    finally:
        conn.close()


@router.post("/auto-retrain/{model}")
async def configure_auto_retrain(
    model: str,
    payload: dict = Body(...),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(DRIFT_BASELINE)),
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
    try:
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
    finally:
        conn.close()


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
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(DRIFT_BASELINE)),
) -> dict:
    """Store the current rolling embedding stats as the input-drift baseline (admin; audited).

    Mirrors ``exa drift input baseline``: needs ≥10 recent input snapshots, uses the same 1000-row
    window and the same stat shape, and persists via ``examlops.data.drift.set_input_baseline`` so
    the baseline is identical to the CLI's. ``?dry_run=true`` previews without writing.
    """
    _require_manage(principal)
    conn = connect(_db_path())
    try:
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
    finally:
        conn.close()
    conn = connect(_db_path())
    try:
        _audit(conn, principal.get("sub", "?"), "input_baseline_set", model, {"n": len(rows)})
        conn.commit()
        conn.close()
        return {"model": model, "baseline": {k: round(v, 4) for k, v in stats.items()}}
    finally:
        conn.close()


@router.post("/input-reset/{model}")
async def reset_input_snapshots(
    model: str,
    dry_run: bool = Query(False),
    principal: dict = Depends(_admin),
    # Through the enforcing dependency as well, so the centre's PDP is asked about this
    # *capability* and not only the coarse `api.write` that `require_role` sends. Added
    # alongside the existing role dependency, never in place of it: `require_capability`
    # admits operators, and widening who may act is not this change's business.
    _gate: dict = Depends(require_capability(DRIFT_BASELINE)),
) -> dict:
    """Clear all input embedding snapshots for a model — keeps the baseline (admin; audited).

    Mirrors ``exa drift input reset``. ``?dry_run=true`` reports the count without deleting.
    """
    _require_manage(principal)
    conn = connect(_db_path())
    try:
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
    finally:
        conn.close()
