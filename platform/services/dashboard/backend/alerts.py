"""Alert aggregation (F12 / ADR 0062).

A unified alert inbox derived from the platform's own signals — prediction drift (`drift_snapshots`
vs `drift_baselines`), budget overspend (`project_budgets` vs `model_costs`), and eval regressions
(`eval_results`). Alerts are severity-coded (F3) and ack-able; an ack is audited to `platform_db`
(D4) and published on the F8 ``alert.*`` channel so open dashboards update live.

The full center (Alertmanager merge, incident correlation, on-call/escalation, runbooks, SLO board)
layers on top; this is the always-available, self-derived core.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from dbconn import connect

# Severity ordering for sorting the inbox (most severe first).
_SEVERITY_RANK = {"critical": 0, "error": 1, "warn": 2, "info": 3}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _alert(
    source: str, key: str, severity: str, title: str, labels: dict[str, str]
) -> dict[str, Any]:
    return {
        "id": f"{source}:{key}",
        "source": source,
        "severity": severity,
        "title": title,
        "labels": labels,
        "state": "firing",
    }


# ── source: prediction drift (drift_snapshots vs drift_baselines) ─────────────


def _drift_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not (_table_exists(conn, "drift_snapshots") and _table_exists(conn, "drift_baselines")):
        return []
    out: list[dict[str, Any]] = []
    baselines = {
        r["model"]: r["stats"] for r in conn.execute("SELECT model, stats FROM drift_baselines")
    }
    latest = conn.execute(
        "SELECT model, AVG(prediction) AS mean, COUNT(*) AS n FROM drift_snapshots GROUP BY model"
    ).fetchall()
    for r in latest:
        raw = baselines.get(r["model"])
        if not raw:
            continue
        try:
            stats = json.loads(raw)
        except (ValueError, TypeError):
            continue
        base_mean = stats.get("mean")
        base_std = stats.get("std")
        if base_mean is None or not base_std:
            continue
        z = abs(r["mean"] - base_mean) / base_std
        if z >= 3:
            sev = "critical"
        elif z >= 2:
            sev = "warn"
        else:
            continue
        out.append(_drift_alert_row(r["model"], z, sev))
    return out


def _drift_alert_row(model: str, z: float, sev: str) -> dict[str, Any]:
    return _alert(
        "drift",
        model,
        sev,
        f"Prediction drift on {model} ({z:.1f}σ from baseline)",
        {"model": model, "zscore": f"{z:.2f}"},
    )


# ── source: budget overspend (project_budgets vs model_costs) ─────────────────


def _budget_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "project_budgets"):
        return []
    consumed = 0.0
    if _table_exists(conn, "model_costs"):
        consumed = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS c FROM model_costs"
        ).fetchone()["c"]
    out: list[dict[str, Any]] = []
    for b in conn.execute("SELECT project, cost_budget FROM project_budgets"):
        budget = b["cost_budget"]
        if budget and consumed > budget:
            out.append(
                _alert(
                    "budget",
                    b["project"],
                    "error",
                    f"Project {b['project']} over budget (${consumed:.0f} / ${budget:.0f})",
                    {"project": b["project"]},
                )
            )
    return out


# ── source: eval regressions (eval_results with a failed metric) ──────────────


def _eval_alerts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not (_table_exists(conn, "eval_runs") and _table_exists(conn, "eval_results")):
        return []
    latest = conn.execute(
        "SELECT r.model AS model, r.id AS run_id FROM eval_runs r "
        "JOIN (SELECT model, MAX(id) AS mid FROM eval_runs GROUP BY model) l ON r.id = l.mid"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in latest:
        failed = conn.execute(
            "SELECT metric FROM eval_results WHERE eval_run_id = ? AND passed = 0", (r["run_id"],)
        ).fetchall()
        if failed:
            metrics = ", ".join(f["metric"] for f in failed)
            out.append(
                _alert(
                    "eval",
                    r["model"],
                    "warn",
                    f"Eval regression on {r['model']}: {metrics}",
                    {"model": r["model"], "failed": metrics},
                )
            )
    return out


# ── public API ────────────────────────────────────────────────────────────────


def active_alerts(db_path: str) -> dict[str, Any]:
    """All firing alerts across sources, most-severe first, with per-severity counts (F12 R1)."""
    conn = _connect(db_path)
    try:
        alerts = _drift_alerts(conn) + _budget_alerts(conn) + _eval_alerts(conn)
    finally:
        conn.close()
    alerts.sort(key=lambda a: (_SEVERITY_RANK.get(a["severity"], 9), a["id"]))
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a["severity"]] = counts.get(a["severity"], 0) + 1
    return {"alerts": alerts, "count": len(alerts), "counts": counts}


def acknowledge(db_path: str, alert_id: str, actor: str) -> bool:
    """Audit an alert acknowledgement to ``audit_events`` (F12 R3 / D4). Best-effort."""
    try:
        conn = connect(db_path)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_events'"
            ).fetchone():
                return False
            conn.execute(
                "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
                ("dashboard-alerts", actor, "alert_ack", alert_id, ""),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:  # pragma: no cover
        return False
