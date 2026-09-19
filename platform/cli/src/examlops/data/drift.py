"""examlops.data.drift — Drift detection.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.platform_db import (  # noqa: F401
    begin_immediate,
    get_db,
    init_db,
    install_write_retry,
    write_retry,
)

__all__ = [
    "claim_drift_trigger",
    "get_corruption_baseline",
    "get_drift_auto_retrain",
    "get_drift_baseline",
    "get_input_baseline",
    "latest_drift_event",
    "list_drift_auto_retrain",
    "list_drift_events",
    "record_drift_event",
    "record_drift_trigger",
    "set_corruption_baseline",
    "set_drift_auto_retrain",
    "set_drift_baseline",
    "set_input_baseline",
    "write_drift_snapshot",
    "write_input_snapshot",
    "drift_models",
    "recent_drift_predictions",
    "record_drift_statuses",
]


def claim_drift_trigger(model: str, cooldown_s: float) -> bool:
    """Atomically claim a retrain slot for ``model``, honoring its cooldown (Phase 0 item 0.12).

    Returns ``True`` (and stamps ``last_triggered = now``) iff the cooldown has elapsed since the
    last trigger — or there was none. Returns ``False`` when the model is still cooling down or has
    no auto-retrain config. The check and the stamp are **one conditional UPDATE**, so two
    overlapping autopilot/drift cycles cannot both claim the same model and double-fire a retrain
    (closes the read-check-then-stamp TOCTOU). ``cooldown_s <= 0`` always claims when a config row
    exists. Note: because the stamp happens *before* the retrain call, a subsequently-failed
    retrain still consumes the cooldown — deliberately conservative, to avoid retry storms.
    """

    def _claim() -> bool:
        with get_db() as conn:
            cur = conn.execute(
                """UPDATE drift_auto_retrain
                       SET last_triggered = CURRENT_TIMESTAMP
                     WHERE model = ?
                       AND (last_triggered IS NULL
                            OR (julianday(CURRENT_TIMESTAMP) - julianday(last_triggered)) * 86400.0
                               >= ?)""",
                (model, cooldown_s),
            )
            return cur.rowcount == 1

    return write_retry(_claim)


def get_corruption_baseline(model: str) -> dict[str, float] | None:
    """The zero-rate/spread reference a corruption signal is judged against (ADR 0114)."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT stats FROM corruption_baselines WHERE model=?", (model,)
        ).fetchone()
    return json.loads(row["stats"]) if row else None


def set_corruption_baseline(model: str, stats: dict[str, float]) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO corruption_baselines (model, stats) VALUES (?,?)",
            (model, json.dumps(stats)),
        )


def get_drift_auto_retrain(model: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM drift_auto_retrain WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


def get_drift_baseline(model: str) -> dict[str, float] | None:
    with get_db() as conn:
        row = conn.execute("SELECT stats FROM drift_baselines WHERE model=?", (model,)).fetchone()
    return json.loads(row["stats"]) if row else None


def get_input_baseline(model: str, *, conn: Any = None) -> dict[str, Any] | None:
    """A model's input-drift baseline, or ``None``.

    ``conn`` lets a caller that is already inside a connection reuse it. Without it, a caller
    looping over models paid a connection per model — free on SQLite, a pool checkout and a round
    trip on Postgres, which is what made `input_drift_rows` open 101 connections for 50 models.
    """
    if conn is not None:
        row = conn.execute("SELECT stats FROM input_baselines WHERE model=?", (model,)).fetchone()
        return json.loads(row["stats"]) if row else None
    with get_db() as own:
        row = own.execute("SELECT stats FROM input_baselines WHERE model=?", (model,)).fetchone()
    return json.loads(row["stats"]) if row else None


def latest_drift_event(model: str, drift_kind: str) -> dict[str, Any] | None:
    """Most recent event of a given kind for a model (auto-retrain consumers)."""
    events = list_drift_events(model=model, drift_kind=drift_kind, last_n=1)
    return events[0] if events else None


def list_drift_auto_retrain() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM drift_auto_retrain").fetchall()
    return [dict(r) for r in rows]


def list_drift_events(
    *, model: str | None = None, drift_kind: str | None = None, last_n: int = 50
) -> list[dict[str, Any]]:
    """Recent unified drift events, newest first — filterable by model/kind (R6)."""
    init_db()
    clauses, params = [], []
    if model:
        clauses.append("model=?")
        params.append(model)
    if drift_kind:
        clauses.append("drift_kind=?")
        params.append(drift_kind)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM drift_events {where} ORDER BY ts DESC, id DESC LIMIT ?",
            (*params, last_n),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        out.append(d)
    return out


def record_drift_event(
    model: str,
    drift_kind: str,
    *,
    severity: str = "OK",
    score: float | None = None,
    metric: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Write one unified drift event (R6). ``drift_kind`` is the discriminator."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO drift_events (model, drift_kind, severity, score, metric, detail)
               VALUES (?,?,?,?,?,?)""",
            (model, drift_kind, severity, score, metric, json.dumps(detail) if detail else None),
        )


def record_drift_trigger(model: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE drift_auto_retrain SET last_triggered=CURRENT_TIMESTAMP WHERE model=?",
            (model,),
        )


def set_drift_auto_retrain(
    model: str,
    enabled: bool,
    min_z_score: float = 3.0,
    dataset_name: str = "",  # no hardcoded dataset (ADR 0094) — caller supplies it
    cooldown_s: int = 3600,
) -> None:
    # Explicit upsert, not INSERT OR REPLACE: REPLACE deletes+reinserts on SQLite (resetting
    # unlisted columns like last_triggered to NULL — instantly re-eligible to retrain) while
    # Postgres' translation preserves them. The two engines must agree; the chosen semantics
    # is PRESERVE the cooldown history across reconfiguration.
    with get_db() as conn:
        conn.execute(
            """INSERT INTO drift_auto_retrain
               (model, enabled, min_z_score, dataset_name, cooldown_s)
               VALUES (?,?,?,?,?)
               ON CONFLICT(model) DO UPDATE SET
                 enabled=excluded.enabled,
                 min_z_score=excluded.min_z_score,
                 dataset_name=excluded.dataset_name,
                 cooldown_s=excluded.cooldown_s""",
            (model, int(enabled), min_z_score, dataset_name, cooldown_s),
        )


def set_drift_baseline(model: str, stats: dict[str, float]) -> None:
    # Explicit upsert with a refreshed set_at — a re-baselined model IS newly baselined,
    # and both engines must agree (see set_drift_auto_retrain).
    with get_db() as conn:
        conn.execute(
            """INSERT INTO drift_baselines (model, stats) VALUES (?,?)
               ON CONFLICT(model) DO UPDATE SET
                 stats=excluded.stats, set_at=CURRENT_TIMESTAMP""",
            (model, json.dumps(stats)),
        )


def set_input_baseline(model: str, stats: dict[str, Any]) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO input_baselines (model, stats) VALUES (?,?)
               ON CONFLICT(model) DO UPDATE SET
                 stats=excluded.stats, set_at=CURRENT_TIMESTAMP""",
            (model, json.dumps(stats)),
        )


def write_drift_snapshot(model: str, alias: str, prediction: float, job_id: str | None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO drift_snapshots (model, alias, prediction, job_id) VALUES (?,?,?,?)",
            (model, alias, prediction, job_id),
        )


def write_input_snapshot(
    model: str, alias: str, emb_norm: float, emb_mean: float, emb_std: float, job_id: str | None
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO input_snapshots (model, alias, emb_norm, emb_mean, emb_std, job_id) "
            "VALUES (?,?,?,?,?,?)",
            (model, alias, emb_norm, emb_mean, emb_std, job_id),
        )


def handle_inference_telemetry_event(event: dict[str, Any]) -> None:
    """The consumer side of ``serving.inference_telemetry`` (ADR 0123 decision 4).

    The serving plane (`platform/clients/seanerbus_bridge.py`, ``EXAMLOPS_TELEMETRY_VIA_EVENTBUS``)
    publishes instead of writing here directly; this performs the same
    ``write_drift_snapshot``/``write_input_snapshot`` calls it used to make inline, on whichever
    process runs the consumer (``exa drift consume-telemetry``) — which is where platform.db
    connectivity now needs to live, not on the request path.
    """
    data = event.get("data") or {}
    model = data.get("model")
    alias = data.get("alias")
    job_id = data.get("job_id")
    if not model or not alias:
        return  # malformed event; nothing to record against
    prediction = data.get("prediction")
    if prediction is not None:
        write_drift_snapshot(model, alias, float(prediction), job_id)
    stats = data.get("embedding_stats")
    if stats:
        write_input_snapshot(
            model, alias, float(stats["norm"]), float(stats["mean"]), float(stats["std"]), job_id
        )


def drift_models() -> list[str]:
    """Every model that has drift snapshots."""
    init_db()
    with get_db() as conn:
        return [r["model"] for r in conn.execute("SELECT DISTINCT model FROM drift_snapshots")]


def recent_drift_predictions(model: str, limit: int) -> list[float]:
    """The model's most recent ``limit`` predictions, newest first."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT prediction FROM drift_snapshots WHERE model=? "
            "ORDER BY ts DESC, rowid DESC LIMIT ?",
            (model, limit),
        ).fetchall()
    return [r["prediction"] for r in rows]


def record_drift_statuses(
    rows: list[dict[str, Any]], announce: Any, *, actor: str = "control-plane"
) -> list[dict[str, Any]]:
    """Record each model's drift status; for each that changed, call ``announce(conn, change)``.

    One transaction under the ``drift`` write lock: the comparison with the last recorded status,
    the new record, and whatever ``announce`` writes (the events) commit together, and a second
    evaluator running at the same time sees the new record, not the old one (plan P2.4b).
    ``announce`` returns False for a change it chose not to announce; it is recorded all the same.
    """
    changes: list[dict[str, Any]] = []
    if not rows:
        return changes
    init_db()
    with get_db() as conn:
        conn.execute(begin_immediate("drift"))
        for row in rows:
            status = str(row["status"])
            before = conn.execute(
                "SELECT status FROM drift_status_state WHERE model=?", (row["model"],)
            ).fetchone()
            previous = before["status"] if before else None
            if previous == status:
                conn.execute(
                    "UPDATE drift_status_state SET z_score=?, evaluated_at=CURRENT_TIMESTAMP "
                    "WHERE model=?",
                    (row["z_score"], row["model"]),
                )
                continue
            conn.execute(
                "INSERT INTO drift_status_state (model, status, z_score, changed_at, evaluated_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                "ON CONFLICT(model) DO UPDATE SET status=excluded.status, "
                "z_score=excluded.z_score, changed_at=excluded.changed_at, "
                "evaluated_at=excluded.evaluated_at",
                (row["model"], status, row["z_score"]),
            )
            change = {
                "model": row["model"],
                "previous": previous,
                "status": status,
                "z_score": row["z_score"],
                "live_mean": row["live_mean"],
                "baseline_mean": row["baseline_mean"],
                "n_snapshots": row["n_snapshots"],
            }
            if announce(conn, change, actor):
                changes.append(change)
    return changes


install_write_retry(__name__)
