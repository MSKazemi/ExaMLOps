"""MLOps-console aggregators (F9 / ADR 0060).

Pure, view-shaped read helpers over the shared ``platform.db`` that back the MLOps console
endpoints in :mod:`routers.mlops`. They surface the already-shipped MLOps backend (drift,
cost, traffic, promotion policy) as registry rows, per-model detail tabs, and a guided
promotion check — without re-querying MLflow at request time, so they stay fast and testable.

**Central name-casing (F9 R2).** ``MODEL_REGISTRY`` uses uppercase display names (``JPCP``)
while the MLflow registry is lowercase (``jpcp``). The mapping lives in ONE place here
(:func:`display_name` / :func:`mlflow_name`) so no caller has to special-case it.
"""

from __future__ import annotations

import sqlite3
from typing import Any


# ── central name-casing (F9 R2) ──────────────────────────────────────────────


def display_name(name: str) -> str:
    """Registry/display casing (uppercase, e.g. ``JPCP``)."""
    return name.upper()


def mlflow_name(name: str) -> str:
    """MLflow-registry casing (lowercase, e.g. ``jpcp``)."""
    return name.lower()


# ── db access ────────────────────────────────────────────────────────────────


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ── health derivation ────────────────────────────────────────────────────────


def _health_token(has_drift: bool, has_promotion: bool) -> str:
    """Colour-blind-safe status token (F3): ``ok`` / ``warn`` / ``unknown``.

    A model tracked for drift but with no promotion policy is ``warn`` (ungoverned);
    fully wired models are ``ok``; models we only know about by name are ``unknown``.
    """
    if has_drift and has_promotion:
        return "ok"
    if has_drift or has_promotion:
        return "warn"
    return "unknown"


# ── registry grid (F9 R1) ────────────────────────────────────────────────────


def registry_rows(db_path: str) -> list[dict[str, Any]]:
    """Registry grid rows — one per model known to the platform DB.

    Composes the union of model names seen across ``drift_snapshots``, ``model_costs``,
    ``traffic_rules`` and ``promotion_rules`` into ModelRow-shaped dicts with central
    name-casing, a derived health token, latest version, alias/stage and freshness.
    """
    conn = _connect(db_path)
    try:
        names: set[str] = set()
        latest_alias: dict[str, str] = {}
        latest_ts: dict[str, str] = {}
        latest_version: dict[str, int] = {}
        drift_models: set[str] = set()
        promo_models: set[str] = set()

        if _table_exists(conn, "drift_snapshots"):
            for r in conn.execute(
                "SELECT model, alias, MAX(ts) AS ts FROM drift_snapshots GROUP BY model"
            ):
                m = r["model"]
                names.add(m)
                drift_models.add(m)
                if r["alias"]:
                    latest_alias[m] = r["alias"]
                if r["ts"]:
                    latest_ts[m] = max(latest_ts.get(m, ""), r["ts"])

        if _table_exists(conn, "model_costs"):
            for r in conn.execute(
                "SELECT model_name, MAX(version) AS v, MAX(recorded_at) AS ts "
                "FROM model_costs GROUP BY model_name"
            ):
                m = r["model_name"]
                names.add(m)
                if r["v"] is not None:
                    latest_version[m] = r["v"]
                if r["ts"]:
                    latest_ts[m] = max(latest_ts.get(m, ""), r["ts"])

        if _table_exists(conn, "traffic_rules"):
            for r in conn.execute("SELECT model, updated_at FROM traffic_rules"):
                names.add(r["model"])
                if r["updated_at"]:
                    latest_ts[r["model"]] = max(latest_ts.get(r["model"], ""), r["updated_at"])

        if _table_exists(conn, "promotion_rules"):
            for r in conn.execute("SELECT model, enabled FROM promotion_rules"):
                names.add(r["model"])
                if r["enabled"]:
                    promo_models.add(r["model"])

        rows: list[dict[str, Any]] = []
        for name in sorted(names):
            rows.append(
                {
                    "name": display_name(name),
                    "mlflowName": mlflow_name(name),
                    "version": latest_version.get(name),
                    "stage": latest_alias.get(name, "—"),
                    "health": _health_token(name in drift_models, name in promo_models),
                    "freshness": latest_ts.get(name),
                    "governed": name in promo_models,
                }
            )
        return rows
    finally:
        conn.close()


# ── promotion check (F9 R4 — guided, denies with reasons) ─────────────────────


def promotion_check(db_path: str, name: str) -> dict[str, Any]:
    """Guided-promotion gate for one model: policy + eval + approval, with reasons.

    Mirrors the ``exa pipeline promote`` policy: a promotion is *allowed* only when an
    **enabled** promotion policy exists. Missing/disabled policy denies with an explicit
    reason (F9 R4 — "denied with reasons when any gate fails"). Approval is always flagged
    as required (phase-11 sysadmin gate) so the UI shows the human step inline.
    """
    key = mlflow_name(name)
    conn = _connect(db_path)
    try:
        policy_row = None
        if _table_exists(conn, "promotion_rules"):
            policy_row = conn.execute(
                "SELECT metric, operator, threshold, from_alias, to_alias, enabled "
                "FROM promotion_rules WHERE lower(model)=?",
                (key,),
            ).fetchone()

        reasons: list[str] = []
        if policy_row is None:
            reasons.append("no promotion policy configured")
            policy_allow = False
            policy: dict[str, Any] = {}
        elif not policy_row["enabled"]:
            reasons.append("promotion policy is disabled")
            policy_allow = False
            policy = _policy_dict(policy_row)
        else:
            policy_allow = True
            policy = _policy_dict(policy_row)

        return {
            "model": display_name(name),
            "mlflowName": key,
            "policy": {"allow": policy_allow, "reasons": reasons, **policy},
            "eval": {"pass": policy_allow, "metrics": {}},
            "approval": {"required": True, "state": "pending"},
            "allowed": policy_allow,
        }
    finally:
        conn.close()


def _policy_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "metric": row["metric"],
        "operator": row["operator"],
        "threshold": row["threshold"],
        "fromAlias": row["from_alias"],
        "toAlias": row["to_alias"],
    }


# ── model detail 2.0 tabs (F9 R2) ────────────────────────────────────────────


def model_detail(db_path: str, name: str) -> dict[str, Any]:
    """Compose the model-detail tabs (cost / drift / traffic / promotion) for one model.

    Each tab is a small view-shaped payload the console renders as a card. MLflow name
    casing is resolved once, centrally, so a caller may pass either casing.
    """
    key = mlflow_name(name)
    conn = _connect(db_path)
    try:
        cost = _cost_tab(conn, key)
        drift = _drift_tab(conn, key)
        traffic = _traffic_tab(conn, key)
    finally:
        conn.close()
    return {
        "name": display_name(name),
        "mlflowName": key,
        "cost": cost,
        "drift": drift,
        "traffic": traffic,
        "promotion": promotion_check(db_path, name),
    }


def _cost_tab(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    if not _table_exists(conn, "model_costs"):
        return {"runs": 0, "gpu_hours": 0.0, "cost_usd": 0.0}
    r = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(gpu_hours),0) AS gh, COALESCE(SUM(cost_usd),0) AS c "
        "FROM model_costs WHERE lower(model_name)=?",
        (key,),
    ).fetchone()
    return {"runs": r["n"], "gpu_hours": round(r["gh"], 3), "cost_usd": round(r["c"], 2)}


def _drift_tab(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    if not _table_exists(conn, "drift_snapshots"):
        return {"samples": 0, "latest": None}
    r = conn.execute(
        "SELECT COUNT(*) AS n, AVG(prediction) AS avg, MAX(ts) AS ts "
        "FROM drift_snapshots WHERE lower(model)=?",
        (key,),
    ).fetchone()
    return {
        "samples": r["n"],
        "mean_prediction": round(r["avg"], 4) if r["avg"] is not None else None,
        "latest": r["ts"],
    }


def _traffic_tab(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    if not _table_exists(conn, "traffic_rules"):
        return {"configured": False}
    r = conn.execute(
        "SELECT rules, updated_at FROM traffic_rules WHERE lower(model)=?", (key,)
    ).fetchone()
    if r is None:
        return {"configured": False}
    return {"configured": True, "rules": r["rules"], "updated_at": r["updated_at"]}
