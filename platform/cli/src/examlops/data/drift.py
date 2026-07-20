"""examlops.data.drift — Drift detection.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
from typing import Any  # noqa: F401

from examlops.platform_db import get_db, init_db, install_write_retry, write_retry  # noqa: F401

__all__ = [
    "claim_drift_trigger",
    "get_drift_auto_retrain",
    "get_drift_baseline",
    "get_input_baseline",
    "latest_drift_event",
    "list_drift_auto_retrain",
    "list_drift_events",
    "record_drift_event",
    "record_drift_trigger",
    "set_drift_auto_retrain",
    "set_drift_baseline",
    "set_input_baseline",
    "write_drift_snapshot",
    "write_input_snapshot",
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


def get_drift_auto_retrain(model: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM drift_auto_retrain WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


def get_drift_baseline(model: str) -> dict[str, float] | None:
    with get_db() as conn:
        row = conn.execute("SELECT stats FROM drift_baselines WHERE model=?", (model,)).fetchone()
    return json.loads(row["stats"]) if row else None


def get_input_baseline(model: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT stats FROM input_baselines WHERE model=?", (model,)).fetchone()
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
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO drift_auto_retrain
               (model, enabled, min_z_score, dataset_name, cooldown_s)
               VALUES (?,?,?,?,?)""",
            (model, int(enabled), min_z_score, dataset_name, cooldown_s),
        )


def set_drift_baseline(model: str, stats: dict[str, float]) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drift_baselines (model, stats) VALUES (?,?)",
            (model, json.dumps(stats)),
        )


def set_input_baseline(model: str, stats: dict[str, Any]) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO input_baselines (model, stats) VALUES (?,?)",
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


install_write_retry(__name__)
