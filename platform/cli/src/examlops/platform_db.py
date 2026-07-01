from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from examlops.resilience import db as _rdb


def _db_path() -> str:
    return os.getenv("PLATFORM_DB", str(Path(__file__).parents[4] / "platform.db"))


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Open a hardened connection to the shared platform SQLite DB.

    Uses the shared :mod:`examlops.resilience.db` helper so every one of the ~170
    call sites (and the 3 concurrent long-lived writers: CLI, agent service,
    seanerbus bridge) gets WAL + ``synchronous=NORMAL`` + a ``busy_timeout`` that
    waits out lock contention instead of raising ``database is locked`` immediately,
    plus ``check_same_thread=False`` for the threaded services.
    """
    conn = _rdb.connect(_db_path())
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def write_retry[T](fn: Callable[[], T]) -> T:
    """Run a DB write, retrying the whole transaction if it loses a lock race.

    Belt-and-suspenders on top of the connection ``busy_timeout`` for the highest-
    frequency writers (e.g. the per-inference bridge snapshots). Example::

        write_retry(lambda: write_drift_snapshot(model, alias, pred, job_id))
    """
    return _rdb.write_retry(fn)


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
            CREATE TABLE IF NOT EXISTS model_costs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model_name  TEXT NOT NULL,
                version     INTEGER NOT NULL,
                run_id      TEXT,
                job_id      TEXT,
                gpu_hours   REAL,
                cost_usd    REAL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS explain_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model      TEXT NOT NULL,
                alias      TEXT NOT NULL DEFAULT 'Production',
                input_hash TEXT,
                top_n      INTEGER,
                status     TEXT NOT NULL DEFAULT 'ok',
                error      TEXT
            );
            CREATE TABLE IF NOT EXISTS model_rollbacks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model        TEXT NOT NULL,
                from_version INTEGER,
                to_version   INTEGER NOT NULL,
                alias        TEXT NOT NULL DEFAULT 'Production',
                actor        TEXT,
                reason       TEXT
            );
            CREATE TABLE IF NOT EXISTS data_quality_checks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model        TEXT NOT NULL,
                dataset      TEXT NOT NULL,
                status       TEXT NOT NULL,
                passed       INTEGER NOT NULL DEFAULT 0,
                failed       INTEGER NOT NULL DEFAULT 0,
                details_json TEXT,
                actor        TEXT
            );
            CREATE TABLE IF NOT EXISTS shadow_config (
                model        TEXT PRIMARY KEY,
                shadow_alias TEXT NOT NULL DEFAULT 'Staging',
                enabled      INTEGER NOT NULL DEFAULT 1,
                updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by   TEXT
            );
            CREATE TABLE IF NOT EXISTS shadow_results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model           TEXT NOT NULL,
                production_pred REAL,
                shadow_pred     REAL,
                diff_pct        REAL,
                job_id          TEXT
            );
            CREATE TABLE IF NOT EXISTS ab_tests (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                model      TEXT NOT NULL,
                name       TEXT,
                variant_a  TEXT NOT NULL DEFAULT 'Production',
                variant_b  TEXT NOT NULL DEFAULT 'Canary',
                split_pct  INTEGER NOT NULL DEFAULT 50,
                status     TEXT NOT NULL DEFAULT 'running',
                started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at   DATETIME,
                created_by TEXT
            );
            CREATE TABLE IF NOT EXISTS ab_results (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                test_id INTEGER NOT NULL,
                ts      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                variant TEXT NOT NULL,
                value   REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model       TEXT NOT NULL,
                alias       TEXT NOT NULL DEFAULT 'Production',
                input_path  TEXT,
                output_path TEXT,
                n_inputs    INTEGER,
                n_success   INTEGER,
                n_errors    INTEGER,
                elapsed_s   REAL,
                actor       TEXT
            );
            CREATE TABLE IF NOT EXISTS hpo_studies (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model            TEXT NOT NULL,
                dataset          TEXT,
                n_trials         INTEGER NOT NULL DEFAULT 20,
                metric           TEXT NOT NULL DEFAULT 'rmse',
                status           TEXT NOT NULL DEFAULT 'pending',
                flow_run_id      TEXT,
                best_params_json TEXT,
                best_value       REAL,
                actor            TEXT
            );
            CREATE TABLE IF NOT EXISTS hpo_trials (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                study_id    INTEGER NOT NULL,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                trial_num   INTEGER NOT NULL,
                params_json TEXT NOT NULL,
                value       REAL
            );
            CREATE TABLE IF NOT EXISTS model_cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model       TEXT NOT NULL,
                output_path TEXT,
                actor       TEXT
            );
            CREATE TABLE IF NOT EXISTS feature_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model       TEXT NOT NULL,
                name        TEXT NOT NULL DEFAULT 'default',
                version     INTEGER NOT NULL DEFAULT 1,
                local_path  TEXT,
                size_bytes  INTEGER,
                schema_json TEXT,
                actor       TEXT
            );
            CREATE TABLE IF NOT EXISTS namespaces (
                name        TEXT PRIMARY KEY,
                description TEXT,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by  TEXT
            );
            CREATE TABLE IF NOT EXISTS namespace_models (
                model       TEXT NOT NULL,
                namespace   TEXT NOT NULL DEFAULT 'default',
                assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (model, namespace)
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
        row = conn.execute("SELECT rules FROM traffic_rules WHERE model=?", (model,)).fetchone()
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
        row = conn.execute("SELECT * FROM promotion_rules WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


def set_drift_baseline(model: str, stats: dict[str, float]) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO drift_baselines (model, stats) VALUES (?,?)",
            (model, json.dumps(stats)),
        )


def get_drift_baseline(model: str) -> dict[str, float] | None:
    with get_db() as conn:
        row = conn.execute("SELECT stats FROM drift_baselines WHERE model=?", (model,)).fetchone()
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
        row = conn.execute("SELECT stats FROM input_baselines WHERE model=?", (model,)).fetchone()
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
        row = conn.execute("SELECT * FROM drift_auto_retrain WHERE model=?", (model,)).fetchone()
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


def record_model_cost(
    model_name: str,
    version: int,
    run_id: str | None,
    job_id: str | None,
    gpu_hours: float | None,
    cost_usd: float | None,
) -> None:
    import datetime

    recorded_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO model_costs
               (model_name, version, run_id, job_id, gpu_hours, cost_usd, recorded_at)
               VALUES (?,?,?,?,?,?,?)""",
            (model_name, version, run_id, job_id, gpu_hours, cost_usd, recorded_at),
        )


def get_model_costs(model_name: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM model_costs WHERE model_name=? ORDER BY version ASC, id ASC",
            (model_name,),
        ).fetchall()
    return [dict(r) for r in rows]


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
