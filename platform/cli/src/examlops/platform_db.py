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
            CREATE TABLE IF NOT EXISTS hpc_jobs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id        TEXT NOT NULL,
                scheduler     TEXT NOT NULL,          -- 'flux' | 'slurm' | 'mock'
                flow_run_id   TEXT,
                model         TEXT NOT NULL,
                dataset       TEXT NOT NULL,
                state         TEXT NOT NULL DEFAULT 'SUBMITTED',
                submit_time   TEXT,
                start_time    TEXT,
                end_time      TEXT,
                queue_seconds REAL,
                run_seconds   REAL,
                nodes         INTEGER,
                gpus          INTEGER,
                cpus          INTEGER,
                exit_code     INTEGER,
                mlflow_run_id TEXT,
                created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scheduler, job_id)
            );
            CREATE TABLE IF NOT EXISTS hpc_nodes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster     TEXT NOT NULL,          -- registry cluster name (or 'default')
                scheduler   TEXT NOT NULL,          -- 'flux' | 'slurm' | 'unmanaged'
                node        TEXT NOT NULL,
                cpus        INTEGER,
                memory_mb   INTEGER,
                gpus        INTEGER NOT NULL DEFAULT 0,
                gpu_model   TEXT,
                state       TEXT,                   -- idle|allocated|mixed|down|drain|unknown
                partition   TEXT,                   -- Slurm partition / Flux queue
                captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

            -- ==================================================================
            -- Next-generation feature substrate (Phase 0 migration pass).
            -- One table per feature area; helper fns follow the set_/get_ /
            -- write_ conventions used above. All additive; existing paths
            -- untouched until each feature's EXAMLOPS_* env flag is enabled.
            -- ==================================================================

            -- #3 Elastic autoscaling & scale-to-zero
            CREATE TABLE IF NOT EXISTS autoscale_config (
                model           TEXT PRIMARY KEY,
                min_replicas    INTEGER NOT NULL DEFAULT 1,
                max_replicas    INTEGER NOT NULL DEFAULT 4,
                target_ongoing  INTEGER NOT NULL DEFAULT 8,
                updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by      TEXT
            );

            -- #1 LLM/GenAI serving endpoints
            CREATE TABLE IF NOT EXISTS llm_endpoints (
                model                TEXT PRIMARY KEY,
                engine               TEXT NOT NULL DEFAULT 'vllm',
                hf_model_id          TEXT NOT NULL,
                max_model_len        INTEGER,
                tensor_parallel_size INTEGER NOT NULL DEFAULT 1,
                dtype                TEXT NOT NULL DEFAULT 'auto',
                enabled              INTEGER NOT NULL DEFAULT 1,
                updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by           TEXT
            );

            -- #5 Online feature/embedding store materializations
            CREATE TABLE IF NOT EXISTS feature_materializations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model        TEXT NOT NULL,
                name         TEXT NOT NULL DEFAULT 'default',
                version      INTEGER NOT NULL DEFAULT 1,
                n_entities   INTEGER,
                online_store TEXT,
                ttl_seconds  INTEGER,
                actor        TEXT
            );

            -- #6 Data & artifact versioning
            CREATE TABLE IF NOT EXISTS data_versions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model         TEXT NOT NULL,
                dataset       TEXT NOT NULL,
                dvc_rev       TEXT,
                storage_url   TEXT,
                content_hash  TEXT,
                zenodo_record TEXT,
                actor         TEXT
            );

            -- #7 Data validation & contracts
            CREATE TABLE IF NOT EXISTS data_contracts (
                model       TEXT PRIMARY KEY,
                schema_path TEXT,
                schema_hash TEXT NOT NULL,
                updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by  TEXT
            );

            -- #9 Ground-truth feedback loop
            CREATE TABLE IF NOT EXISTS predictions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model        TEXT NOT NULL,
                alias        TEXT NOT NULL DEFAULT 'Production',
                request_hash TEXT NOT NULL,
                prediction   REAL NOT NULL,
                features_json TEXT,
                job_id       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_predictions_hash ON predictions (request_hash);
            CREATE TABLE IF NOT EXISTS ground_truth (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                request_hash TEXT NOT NULL,
                label        REAL NOT NULL,
                source       TEXT NOT NULL DEFAULT 'manual'
            );
            CREATE INDEX IF NOT EXISTS idx_ground_truth_hash ON ground_truth (request_hash);
            CREATE TABLE IF NOT EXISTS live_metrics (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model        TEXT NOT NULL,
                alias        TEXT NOT NULL DEFAULT 'Production',
                metric       TEXT NOT NULL,
                value        REAL NOT NULL,
                n            INTEGER NOT NULL DEFAULT 0,
                window_start DATETIME,
                window_end   DATETIME
            );

            -- #10 Data labeling & active learning
            CREATE TABLE IF NOT EXISTS label_queue (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model         TEXT NOT NULL,
                request_hash  TEXT,
                features_json TEXT,
                uncertainty   REAL,
                status        TEXT NOT NULL DEFAULT 'pending',
                assigned_to   TEXT,
                label         REAL
            );

            -- #13 Statistically-rigorous A/B assignment
            CREATE TABLE IF NOT EXISTS ab_assignments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                test_id     INTEGER NOT NULL,
                request_key TEXT NOT NULL,
                variant     TEXT NOT NULL
            );

            -- #11 Automated progressive delivery (canary + auto-rollback)
            CREATE TABLE IF NOT EXISTS canary_runs (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at          DATETIME,
                model             TEXT NOT NULL,
                candidate_version INTEGER,
                status            TEXT NOT NULL DEFAULT 'running',
                actor             TEXT
            );
            CREATE TABLE IF NOT EXISTS canary_steps (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                canary_run_id INTEGER NOT NULL,
                pct          INTEGER NOT NULL,
                decision     TEXT NOT NULL,
                metric_value REAL
            );

            -- #14 Continuous evaluation + LLM eval harness
            CREATE TABLE IF NOT EXISTS eval_runs (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model    TEXT NOT NULL,
                suite    TEXT NOT NULL,
                status   TEXT NOT NULL DEFAULT 'running',
                actor    TEXT
            );
            CREATE TABLE IF NOT EXISTS eval_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                eval_run_id INTEGER NOT NULL,
                metric      TEXT NOT NULL,
                value       REAL NOT NULL,
                baseline    REAL,
                passed      INTEGER NOT NULL DEFAULT 1
            );

            -- #15 Model optimization/compilation
            CREATE TABLE IF NOT EXISTS model_optimizations (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model          TEXT NOT NULL,
                base_version   INTEGER,
                opt_version    INTEGER,
                method         TEXT NOT NULL,
                size_bytes     INTEGER,
                latency_ms     REAL,
                accuracy_delta REAL
            );

            -- #17 Explainability
            CREATE TABLE IF NOT EXISTS explanations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model               TEXT NOT NULL,
                run_id              TEXT,
                method              TEXT NOT NULL DEFAULT 'shap',
                feature_importances TEXT NOT NULL,
                scope               TEXT NOT NULL DEFAULT 'global'
            );

            -- #18 Fairness & bias detection
            CREATE TABLE IF NOT EXISTS fairness_reports (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model            TEXT NOT NULL,
                run_id           TEXT,
                sensitive_feature TEXT NOT NULL,
                metrics_json     TEXT NOT NULL,
                passed           INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS fairness_gates (
                model             TEXT NOT NULL,
                sensitive_feature TEXT NOT NULL,
                metric            TEXT NOT NULL,
                max_disparity     REAL NOT NULL,
                PRIMARY KEY (model, sensitive_feature, metric)
            );

            -- #19 Supply-chain security & provenance
            CREATE TABLE IF NOT EXISTS attestations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model           TEXT NOT NULL,
                version         INTEGER NOT NULL,
                artifact_digest TEXT NOT NULL,
                signature       TEXT,
                sbom_path       TEXT,
                slsa_level      INTEGER,
                trivy_summary   TEXT,
                verified        INTEGER NOT NULL DEFAULT 0
            );

            -- #20 FinOps + Green-AI carbon accounting
            CREATE TABLE IF NOT EXISTS project_budgets (
                project          TEXT PRIMARY KEY,
                gpu_hours_budget REAL,
                cost_budget      REAL,
                period           TEXT NOT NULL DEFAULT 'monthly',
                updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by       TEXT
            );
            CREATE TABLE IF NOT EXISTS carbon_records (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                run_id         TEXT,
                model          TEXT NOT NULL,
                kwh            REAL,
                co2e_g         REAL,
                grid_intensity REAL,
                provider       TEXT
            );
            CREATE TABLE IF NOT EXISTS inference_energy (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model    TEXT NOT NULL,
                requests INTEGER NOT NULL DEFAULT 0,
                kwh      REAL,
                co2e_g   REAL
            );

            -- #16 EU AI Act / GDPR compliance
            CREATE TABLE IF NOT EXISTS compliance_records (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model           TEXT NOT NULL,
                version         INTEGER,
                risk_class      TEXT,
                annex_iv_path   TEXT,
                provenance_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS data_retention (
                dataset         TEXT PRIMARY KEY,
                subject_id_field TEXT,
                retention_days  INTEGER NOT NULL DEFAULT 365,
                purge_after     DATETIME
            );
        """)
        _migrate_columns(conn)


# Idempotent additive column migrations for tables that predate a feature.
# ``ALTER TABLE ADD COLUMN`` errors if the column already exists, so we gate on
# PRAGMA table_info. Keep entries here forever — they are cheap and self-skipping.
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    # #8 Real HPO/AutoML: Optuna study bookkeeping on the existing hpo tables.
    "hpo_studies": {
        "study_name": "TEXT",
        "sampler": "TEXT",
        "pruner": "TEXT",
        "state": "TEXT NOT NULL DEFAULT 'running'",
    },
    "hpo_trials": {
        "state": "TEXT NOT NULL DEFAULT 'complete'",
        "pruned": "INTEGER NOT NULL DEFAULT 0",
    },
    # FinOps pluggable providers (ADR 0074): which carbon provider produced each record.
    "carbon_records": {
        "provider": "TEXT",
    },
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _COLUMN_MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


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


def record_hpc_job(
    job_id: str,
    scheduler: str,
    flow_run_id: str | None,
    model: str,
    dataset: str,
    nodes: int | None = None,
    gpus: int | None = None,
    cpus: int | None = None,
    submit_time: str | None = None,
    mlflow_run_id: str | None = None,
) -> None:
    """Insert (or upsert) an HPC job tracking row.

    Idempotent on ``(scheduler, job_id)`` so a Prefect task retry that resubmits does
    not create duplicate rows.
    """
    import datetime

    submit_time = submit_time or datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO hpc_jobs
                   (job_id, scheduler, flow_run_id, model, dataset, state,
                    submit_time, nodes, gpus, cpus, mlflow_run_id)
               VALUES (?,?,?,?,?,'SUBMITTED',?,?,?,?,?)
               ON CONFLICT(scheduler, job_id) DO UPDATE SET
                   flow_run_id=excluded.flow_run_id,
                   model=excluded.model,
                   dataset=excluded.dataset,
                   nodes=excluded.nodes,
                   gpus=excluded.gpus,
                   cpus=excluded.cpus,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                job_id,
                scheduler,
                flow_run_id,
                model,
                dataset,
                submit_time,
                nodes,
                gpus,
                cpus,
                mlflow_run_id,
            ),
        )


def update_hpc_job(
    job_id: str,
    scheduler: str,
    *,
    state: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    exit_code: int | None = None,
    queue_seconds: float | None = None,
    run_seconds: float | None = None,
    mlflow_run_id: str | None = None,
) -> None:
    """Update mutable fields of an existing hpc_jobs row (no-op if none provided)."""
    fields = {
        "state": state,
        "start_time": start_time,
        "end_time": end_time,
        "exit_code": exit_code,
        "queue_seconds": queue_seconds,
        "run_seconds": run_seconds,
        "mlflow_run_id": mlflow_run_id,
    }
    sets = {k: v for k, v in fields.items() if v is not None}
    if not sets:
        return
    assignments = ", ".join(f"{k}=?" for k in sets)
    with get_db() as conn:
        conn.execute(
            f"UPDATE hpc_jobs SET {assignments}, updated_at=CURRENT_TIMESTAMP "
            "WHERE scheduler=? AND job_id=?",
            (*sets.values(), scheduler, job_id),
        )


def get_hpc_jobs(model: str | None = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        if model:
            rows = conn.execute(
                "SELECT * FROM hpc_jobs WHERE model=? ORDER BY id DESC", (model,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM hpc_jobs ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def record_node_snapshot(cluster: str, scheduler: str, nodes: list[dict[str, Any]]) -> int:
    """Replace the stored node inventory for ``cluster`` with a fresh snapshot.

    Snapshot semantics (latest wins): existing rows for the cluster are deleted and the
    current ``nodes`` (dicts shaped like ``discovery.NodeInfo.to_dict()``) are inserted.
    Returns the number of node rows written.
    """
    with get_db() as conn:
        conn.execute("DELETE FROM hpc_nodes WHERE cluster=?", (cluster,))
        conn.executemany(
            """INSERT INTO hpc_nodes
                   (cluster, scheduler, node, cpus, memory_mb, gpus, gpu_model, state, partition)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (
                    cluster,
                    scheduler,
                    n.get("name"),
                    n.get("cpus"),
                    n.get("memory_mb"),
                    n.get("gpus", 0) or 0,
                    n.get("gpu_model"),
                    n.get("state"),
                    n.get("partition"),
                )
                for n in nodes
            ],
        )
    return len(nodes)


def get_node_snapshot(cluster: str | None = None) -> list[dict[str, Any]]:
    """Return the latest stored node inventory, optionally filtered to one cluster."""
    with get_db() as conn:
        if cluster:
            rows = conn.execute(
                "SELECT * FROM hpc_nodes WHERE cluster=? ORDER BY node ASC", (cluster,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM hpc_nodes ORDER BY cluster ASC, node ASC"
            ).fetchall()
    return [dict(r) for r in rows]


# ======================================================================
# Next-generation feature helpers (Phase 0). Each mirrors the set_/get_/
# write_ conventions above so callers stay uniform across features.
# ======================================================================


# ---- #3 Elastic autoscaling --------------------------------------------------
def set_autoscale_config(
    model: str,
    min_replicas: int,
    max_replicas: int,
    target_ongoing: int = 8,
    updated_by: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO autoscale_config
               (model, min_replicas, max_replicas, target_ongoing, updated_by)
               VALUES (?,?,?,?,?)""",
            (model, min_replicas, max_replicas, target_ongoing, updated_by),
        )


def get_autoscale_config(model: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM autoscale_config WHERE model=?", (model,)).fetchone()
    return dict(row) if row else None


# ---- #9 Ground-truth feedback loop -------------------------------------------
def write_prediction(
    model: str,
    alias: str,
    request_hash: str,
    prediction: float,
    features_json: str | None = None,
    job_id: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO predictions
               (model, alias, request_hash, prediction, features_json, job_id)
               VALUES (?,?,?,?,?,?)""",
            (model, alias, request_hash, prediction, features_json, job_id),
        )


def write_ground_truth(request_hash: str, label: float, source: str = "manual") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO ground_truth (request_hash, label, source) VALUES (?,?,?)",
            (request_hash, label, source),
        )


def join_predictions_with_truth(model: str, alias: str | None = None) -> list[dict[str, Any]]:
    """Return prediction/label pairs for a model (optionally one alias).

    The join key is ``request_hash`` — a stable digest of the inference input the
    serving path already computes for drift logging. Feeds live-accuracy metrics
    (#9), the A/B stats engine (#13), canary analysis (#11) and eval (#14).
    """
    sql = (
        "SELECT p.model, p.alias, p.request_hash, p.prediction, g.label, g.source "
        "FROM predictions p JOIN ground_truth g ON p.request_hash = g.request_hash "
        "WHERE p.model=?"
    )
    params: list[Any] = [model]
    if alias is not None:
        sql += " AND p.alias=?"
        params.append(alias)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def write_live_metric(
    model: str,
    alias: str,
    metric: str,
    value: float,
    n: int = 0,
    window_start: str | None = None,
    window_end: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO live_metrics
               (model, alias, metric, value, n, window_start, window_end)
               VALUES (?,?,?,?,?,?,?)""",
            (model, alias, metric, value, n, window_start, window_end),
        )


def get_live_metrics(model: str, alias: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM live_metrics WHERE model=?"
    params: list[Any] = [model]
    if alias is not None:
        sql += " AND alias=?"
        params.append(alias)
    sql += " ORDER BY id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---- #18 Fairness gates ------------------------------------------------------
def set_fairness_gate(
    model: str, sensitive_feature: str, metric: str, max_disparity: float
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO fairness_gates
               (model, sensitive_feature, metric, max_disparity) VALUES (?,?,?,?)""",
            (model, sensitive_feature, metric, max_disparity),
        )


def get_fairness_gates(model: str) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM fairness_gates WHERE model=?", (model,)).fetchall()
    return [dict(r) for r in rows]


# ---- #20 FinOps budgets ------------------------------------------------------
def set_project_budget(
    project: str,
    gpu_hours_budget: float | None,
    cost_budget: float | None,
    period: str = "monthly",
    updated_by: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO project_budgets
               (project, gpu_hours_budget, cost_budget, period, updated_by)
               VALUES (?,?,?,?,?)""",
            (project, gpu_hours_budget, cost_budget, period, updated_by),
        )


def get_project_budget(project: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM project_budgets WHERE project=?", (project,)).fetchone()
    return dict(row) if row else None


def write_carbon_record(
    model: str,
    run_id: str | None,
    kwh: float | None,
    co2e_g: float | None,
    grid_intensity: float | None = None,
    provider: str | None = None,
) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO carbon_records (run_id, model, kwh, co2e_g, grid_intensity, provider)
               VALUES (?,?,?,?,?,?)""",
            (run_id, model, kwh, co2e_g, grid_intensity, provider),
        )


def get_carbon_records(model: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM carbon_records"
    params: list[Any] = []
    if model is not None:
        sql += " WHERE model=?"
        params.append(model)
    sql += " ORDER BY id DESC"
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_project_budgets() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM project_budgets ORDER BY project").fetchall()
    return [dict(r) for r in rows]


def get_project_consumption(project: str) -> dict[str, float]:
    """Sum recorded GPU-hours and cost for all models in a namespace (= project).

    Joins ``namespace_models`` → ``model_costs`` so budgets enforce against the real
    training spend already tracked by ``exa models cost``.
    """
    with get_db() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(c.gpu_hours), 0) AS gpu_hours,
                      COALESCE(SUM(c.cost_usd), 0)  AS cost_usd
               FROM namespace_models nm
               JOIN model_costs c ON c.model_name = nm.model
               WHERE nm.namespace = ?""",
            (project,),
        ).fetchone()
    return {"gpu_hours": float(row["gpu_hours"]), "cost_usd": float(row["cost_usd"])}


def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"
