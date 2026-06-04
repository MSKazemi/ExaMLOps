from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", str(Path(__file__).parents[4] / "platform.db"))


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                source   TEXT NOT NULL,
                actor    TEXT,
                action   TEXT NOT NULL,
                target   TEXT,
                details  TEXT
            );
            CREATE TABLE IF NOT EXISTS drift_snapshots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model      TEXT NOT NULL,
                alias      TEXT NOT NULL,
                prediction REAL NOT NULL,
                job_id     TEXT
            );
            CREATE TABLE IF NOT EXISTS drift_baselines (
                model  TEXT PRIMARY KEY,
                stats  TEXT NOT NULL,
                set_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS traffic_rules (
                model      TEXT PRIMARY KEY,
                rules      TEXT NOT NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT
            );
            CREATE TABLE IF NOT EXISTS promotion_rules (
                model      TEXT PRIMARY KEY,
                metric     TEXT NOT NULL,
                operator   TEXT NOT NULL,
                threshold  REAL NOT NULL,
                from_alias TEXT NOT NULL DEFAULT 'Staging',
                to_alias   TEXT NOT NULL DEFAULT 'Production',
                enabled    INTEGER NOT NULL DEFAULT 1,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS drift_auto_retrain (
                model          TEXT PRIMARY KEY,
                enabled        INTEGER NOT NULL DEFAULT 1,
                min_z_score    REAL NOT NULL DEFAULT 3.0,
                dataset_name   TEXT NOT NULL DEFAULT 'PM100Dataset',
                cooldown_s     INTEGER NOT NULL DEFAULT 3600,
                last_triggered DATETIME
            );
            CREATE TABLE IF NOT EXISTS input_snapshots (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model    TEXT NOT NULL,
                alias    TEXT NOT NULL,
                emb_norm REAL NOT NULL,
                emb_mean REAL NOT NULL,
                emb_std  REAL NOT NULL,
                job_id   TEXT
            );
            CREATE TABLE IF NOT EXISTS input_baselines (
                model     TEXT PRIMARY KEY,
                stats     TEXT NOT NULL,
                set_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)


def write_audit_event(
    source: str,
    actor: str | None,
    action: str,
    target: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO audit_events (source, actor, action, target, details) VALUES (?,?,?,?,?)",
            (source, actor, action, target, json.dumps(details) if details else None),
        )


def write_drift_snapshot(model: str, alias: str, prediction: float, job_id: str | None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO drift_snapshots (model, alias, prediction, job_id) VALUES (?,?,?,?)",
            (model, alias, prediction, job_id),
        )


def set_traffic_rules(model: str, rules: dict[str, int], updated_by: str | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO traffic_rules (model, rules, updated_by) VALUES (?,?,?)",
            (model, json.dumps(rules), updated_by),
        )


def get_traffic_rules(model: str) -> dict[str, int] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT rules FROM traffic_rules WHERE model=?", (model,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["rules"])


def set_promotion_rule(
    model: str,
    metric: str,
    operator: str,
    threshold: float,
    from_alias: str = "Staging",
    to_alias: str = "Production",
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO promotion_rules
               (model, metric, operator, threshold, from_alias, to_alias)
               VALUES (?,?,?,?,?,?)""",
            (model, metric, operator, threshold, from_alias, to_alias),
        )


def get_promotion_rule(model: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM promotion_rules WHERE model=?", (model,)
        ).fetchone()
    return dict(row) if row else None


def set_drift_baseline(model: str, stats: dict[str, float]) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drift_baselines (model, stats) VALUES (?,?)",
            (model, json.dumps(stats)),
        )


def get_drift_baseline(model: str) -> dict[str, float] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT stats FROM drift_baselines WHERE model=?", (model,)
        ).fetchone()
    return json.loads(row["stats"]) if row else None


def write_input_snapshot(
    model: str, alias: str, emb_norm: float, emb_mean: float, emb_std: float, job_id: str | None
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO input_snapshots (model, alias, emb_norm, emb_mean, emb_std, job_id) "
            "VALUES (?,?,?,?,?,?)",
            (model, alias, emb_norm, emb_mean, emb_std, job_id),
        )


def set_input_baseline(model: str, stats: dict[str, Any]) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO input_baselines (model, stats) VALUES (?,?)",
            (model, json.dumps(stats)),
        )


def get_input_baseline(model: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT stats FROM input_baselines WHERE model=?", (model,)
        ).fetchone()
    return json.loads(row["stats"]) if row else None


def set_drift_auto_retrain(
    model: str,
    enabled: bool,
    min_z_score: float = 3.0,
    dataset_name: str = "PM100Dataset",
    cooldown_s: int = 3600,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO drift_auto_retrain
               (model, enabled, min_z_score, dataset_name, cooldown_s)
               VALUES (?,?,?,?,?)""",
            (model, int(enabled), min_z_score, dataset_name, cooldown_s),
        )


def get_drift_auto_retrain(model: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM drift_auto_retrain WHERE model=?", (model,)
        ).fetchone()
    return dict(row) if row else None


def list_drift_auto_retrain() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM drift_auto_retrain").fetchall()
    return [dict(r) for r in rows]


def record_drift_trigger(model: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE drift_auto_retrain SET last_triggered=CURRENT_TIMESTAMP WHERE model=?",
            (model,),
        )


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
