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
    dataplane bridge) gets WAL + ``synchronous=NORMAL`` + a ``busy_timeout`` that
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
            CREATE TABLE IF NOT EXISTS hpc_clusters (
                name            TEXT PRIMARY KEY,
                scheduler       TEXT NOT NULL,        -- flux|slurm|unmanaged|mock
                transport       TEXT NOT NULL DEFAULT 'ssh',  -- ssh|local
                host            TEXT,
                ssh_user        TEXT,
                ssh_port        INTEGER DEFAULT 22,
                ssh_key         TEXT,
                key_fingerprint TEXT,
                state           TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|ACTIVE|REJECTED
                capabilities    TEXT,                 -- JSON (discovery ClusterCaps)
                requested_by    TEXT,
                approved_by     TEXT,
                reason          TEXT,
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

            -- ExaMLOps Projects (RHOAI-style resource envelopes for Docker)
            CREATE TABLE IF NOT EXISTS projects (
                name            TEXT PRIMARY KEY,
                description     TEXT,
                cpu_limit       REAL NOT NULL DEFAULT 4.0,
                memory_limit_gb REAL NOT NULL DEFAULT 8.0,
                storage_gb      REAL NOT NULL DEFAULT 50.0,
                gpu_limit       INTEGER NOT NULL DEFAULT 0,
                network_name    TEXT,
                status          TEXT NOT NULL DEFAULT 'ACTIVE',
                created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by      TEXT,
                updated_at      DATETIME
            );
            CREATE TABLE IF NOT EXISTS project_models (
                project     TEXT NOT NULL,
                model       TEXT NOT NULL,
                assigned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, model)
            );

            -- Self-driving autopilot (ADR 0085): closed-loop detect→retrain→validate→promote
            CREATE TABLE IF NOT EXISTS autopilot_runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                triggered_by        TEXT NOT NULL DEFAULT 'manual',
                model_filter        TEXT,
                dry_run             INTEGER NOT NULL DEFAULT 0,
                enabled_state       TEXT NOT NULL DEFAULT 'enabled',
                retrains_triggered  INTEGER NOT NULL DEFAULT 0,
                promotions_made     INTEGER NOT NULL DEFAULT 0,
                policy_blocks       INTEGER NOT NULL DEFAULT 0,
                human_required      INTEGER NOT NULL DEFAULT 0,
                skipped             INTEGER NOT NULL DEFAULT 0,
                summary             TEXT
            );
            CREATE TABLE IF NOT EXISTS autopilot_config (
                key         TEXT PRIMARY KEY,
                value       TEXT NOT NULL,
                updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · D3 — model signing + AI-BOM (ADR 0013).
            CREATE TABLE IF NOT EXISTS model_signatures (
                model       TEXT NOT NULL,
                version     TEXT NOT NULL,
                digest      TEXT NOT NULL,
                algo        TEXT NOT NULL DEFAULT 'hmac-sha256',
                signature   TEXT NOT NULL,
                cert        TEXT,
                signed_by   TEXT,
                signed_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (model, version)
            );
            CREATE TABLE IF NOT EXISTS model_boms (
                model       TEXT NOT NULL,
                version     TEXT NOT NULL,
                bom_json    TEXT NOT NULL,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (model, version)
            );
            -- Next-Gen 40 · D6 — relationship-based authz (ADR 0014). Default-deny;
            -- relations owner⊇editor⊇viewer between a subject and an object.
            CREATE TABLE IF NOT EXISTS authz_relations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                subject     TEXT NOT NULL,
                relation    TEXT NOT NULL,
                object      TEXT NOT NULL,
                actor       TEXT,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (subject, relation, object)
            );
            -- Next-Gen 40 · D7 — local encrypted secrets store (ADR 0011). Fallback
            -- when OpenBao/SOPS is not deployed; values are Fernet-encrypted at rest.
            CREATE TABLE IF NOT EXISTS secrets_store (
                path        TEXT NOT NULL,
                tenant      TEXT NOT NULL DEFAULT 'default',
                ciphertext  TEXT NOT NULL,
                version     INTEGER NOT NULL DEFAULT 1,
                updated_by  TEXT,
                updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (path, tenant)
            );
            -- Next-Gen 40 · B1 — prompt registry (ADR 0009). Versions are immutable;
            -- labels are moving pointers (dev/staging/prod) to a specific version.
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                version     INTEGER NOT NULL,
                template    TEXT NOT NULL,
                variables   TEXT,
                tags        TEXT,
                actor       TEXT,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (name, version)
            );
            CREATE TABLE IF NOT EXISTS prompt_labels (
                name        TEXT NOT NULL,
                label       TEXT NOT NULL,
                version     INTEGER NOT NULL,
                updated_by  TEXT,
                updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (name, label)
            );
            -- Next-Gen 40 · A1 — immutable, run-pinned dataset revisions (ADR 0003).
            -- Idempotent on (backend, dataset, revision_id): re-recording a revision is a no-op.
            CREATE TABLE IF NOT EXISTS dataset_revisions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                backend       TEXT NOT NULL,
                dataset       TEXT NOT NULL,
                revision_id   TEXT NOT NULL,
                kind          TEXT NOT NULL DEFAULT 'content',
                uri           TEXT,
                schema_hash   TEXT,
                mlflow_run_id TEXT,
                row_count     INTEGER,
                byte_count    INTEGER,
                actor         TEXT,
                created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (backend, dataset, revision_id)
            );
            -- Next-Gen 40 · A2 — OpenLineage run events + I/O nodes (ADR 0004).
            -- platform_db is the operational source of truth; Marquez (if configured)
            -- holds the queryable graph. emit_lineage() dual-writes both.
            CREATE TABLE IF NOT EXISTS lineage_events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id           TEXT NOT NULL,
                job              TEXT NOT NULL,
                event_type       TEXT NOT NULL,       -- START | COMPLETE | FAIL
                dataset_revision TEXT,
                mlflow_run_id    TEXT,
                model            TEXT,
                model_version    TEXT,
                trace_id         TEXT,
                facets_json      TEXT,
                ts               DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS lineage_io (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                direction   TEXT NOT NULL,             -- input | output
                node_type   TEXT NOT NULL,             -- dataset | model | deployment
                node_name   TEXT NOT NULL,             -- namespaced, e.g. examlops://model/jpcp/18
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (run_id, direction, node_name)
            );
            -- Next-Gen 40 · C2 — continuous-eval suite results (ADR 0007). One row per
            -- (suite, model version, run, metric); idempotent via the UNIQUE constraint.
            CREATE TABLE IF NOT EXISTS eval_suite_results (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                suite                 TEXT NOT NULL,
                model                 TEXT NOT NULL,
                model_version         TEXT,
                alias                 TEXT,
                metric                TEXT NOT NULL,
                score                 REAL NOT NULL,
                sample_size           INTEGER NOT NULL DEFAULT 0,
                judge_model           TEXT,
                judge_prompt_version  TEXT,
                dataset_revision      TEXT,
                run_id                TEXT NOT NULL,
                ts                    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (suite, model_version, run_id, metric)
            );
            -- Next-Gen 40 · C3 — eval regression gate config + reports (ADR 0008).
            CREATE TABLE IF NOT EXISTS eval_gates (
                model          TEXT PRIMARY KEY,
                suite          TEXT NOT NULL,
                baseline_alias TEXT NOT NULL DEFAULT 'Production',
                metrics_json   TEXT NOT NULL,           -- [{name,min?,max_drop?}]
                mode           TEXT NOT NULL DEFAULT 'block',   -- block | warn
                updated_by     TEXT,
                updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS gate_reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                model         TEXT NOT NULL,
                candidate     TEXT,
                baseline      TEXT,
                passed        INTEGER NOT NULL,
                mode          TEXT NOT NULL,
                report_json   TEXT NOT NULL,
                ts            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · B2 — model gateway virtual keys + per-call cost (ADR 0010).
            -- Only the key hash is stored (never the raw key). budget_usd NULL = unlimited.
            CREATE TABLE IF NOT EXISTS virtual_keys (
                key_hash    TEXT PRIMARY KEY,
                tenant      TEXT NOT NULL DEFAULT 'default',
                project     TEXT NOT NULL DEFAULT 'default',
                models_json TEXT NOT NULL DEFAULT '[]',   -- allow-list; [] = all models
                budget_usd  REAL,
                spent_usd   REAL NOT NULL DEFAULT 0,
                created_by  TEXT,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                revoked     INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS gateway_calls (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash          TEXT,
                model             TEXT NOT NULL,
                backend           TEXT,
                cost_usd          REAL NOT NULL DEFAULT 0,
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                ts                DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    # A5 data contracts (ADR 0005): revision/stage/score facets on the quality table.
    "data_quality_checks": {
        "revision": "TEXT",
        "stage": "TEXT NOT NULL DEFAULT 'train'",
        "score": "REAL",
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
            rows = conn.execute("SELECT * FROM hpc_nodes ORDER BY cluster ASC, node ASC").fetchall()
    return [dict(r) for r in rows]


def upsert_cluster(
    name: str,
    scheduler: str,
    *,
    transport: str = "ssh",
    host: str | None = None,
    ssh_user: str | None = None,
    ssh_port: int | None = 22,
    ssh_key: str | None = None,
    key_fingerprint: str | None = None,
    capabilities: dict[str, Any] | None = None,
    requested_by: str | None = None,
) -> None:
    """Insert or update a cluster *definition* — state is never changed here.

    A brand-new cluster starts ``PENDING`` (the table default). Re-running discovery on an
    already-approved (or already-rejected) cluster refreshes its definition + capabilities
    but leaves its ``state``/``approved_by`` intact, so re-probing can never silently
    authorize or de-authorize a cluster. State transitions go through
    :func:`set_cluster_state`.
    """
    caps = json.dumps(capabilities) if capabilities else None
    with get_db() as conn:
        conn.execute(
            """INSERT INTO hpc_clusters
                   (name, scheduler, transport, host, ssh_user, ssh_port, ssh_key,
                    key_fingerprint, capabilities, requested_by)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                   scheduler=excluded.scheduler,
                   transport=excluded.transport,
                   host=excluded.host,
                   ssh_user=excluded.ssh_user,
                   ssh_port=excluded.ssh_port,
                   ssh_key=excluded.ssh_key,
                   key_fingerprint=excluded.key_fingerprint,
                   capabilities=excluded.capabilities,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                name,
                scheduler,
                transport,
                host,
                ssh_user,
                ssh_port,
                ssh_key,
                key_fingerprint,
                caps,
                requested_by,
            ),
        )


def set_cluster_state(
    name: str,
    state: str,
    *,
    approved_by: str | None = None,
    reason: str | None = None,
) -> bool:
    """Transition a cluster's state (PENDING|ACTIVE|REJECTED). Returns False if unknown."""
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE hpc_clusters
                   SET state=?, approved_by=COALESCE(?, approved_by),
                       reason=COALESCE(?, reason), updated_at=CURRENT_TIMESTAMP
                 WHERE name=?""",
            (state, approved_by, reason, name),
        )
        return cur.rowcount > 0


def get_cluster(name: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM hpc_clusters WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def get_clusters(state: str | None = None) -> list[dict[str, Any]]:
    with get_db() as conn:
        if state:
            rows = conn.execute(
                "SELECT * FROM hpc_clusters WHERE state=? ORDER BY name ASC", (state,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM hpc_clusters ORDER BY name ASC").fetchall()
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


# ── ExaMLOps Projects (RHOAI-style resource envelopes for Docker) ─────────────


def create_project(
    name: str,
    *,
    description: str | None = None,
    cpu_limit: float = 4.0,
    memory_limit_gb: float = 8.0,
    storage_gb: float = 50.0,
    gpu_limit: int = 0,
    created_by: str | None = None,
) -> None:
    """Create a new project with resource quotas."""
    init_db()
    network_name = f"examlops-{name}"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO projects
               (name, description, cpu_limit, memory_limit_gb, storage_gb, gpu_limit,
                network_name, created_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                name,
                description,
                cpu_limit,
                memory_limit_gb,
                storage_gb,
                gpu_limit,
                network_name,
                created_by,
            ),
        )


def get_project(name: str) -> dict[str, Any] | None:
    """Return project row as dict, or None if not found."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def list_projects(status: str | None = None) -> list[dict[str, Any]]:
    """Return all projects, optionally filtered by status (ACTIVE/ARCHIVED)."""
    init_db()
    with get_db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status=? ORDER BY name", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def update_project_quota(
    name: str,
    *,
    cpu_limit: float | None = None,
    memory_limit_gb: float | None = None,
    storage_gb: float | None = None,
    gpu_limit: int | None = None,
    description: str | None = None,
) -> bool:
    """Update quota fields for a project. Returns True if found and updated."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        if cpu_limit is not None:
            conn.execute(
                "UPDATE projects SET cpu_limit=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (cpu_limit, name),
            )
        if memory_limit_gb is not None:
            conn.execute(
                "UPDATE projects SET memory_limit_gb=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (memory_limit_gb, name),
            )
        if storage_gb is not None:
            conn.execute(
                "UPDATE projects SET storage_gb=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (storage_gb, name),
            )
        if gpu_limit is not None:
            conn.execute(
                "UPDATE projects SET gpu_limit=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (gpu_limit, name),
            )
        if description is not None:
            conn.execute(
                "UPDATE projects SET description=?, updated_at=CURRENT_TIMESTAMP WHERE name=?",
                (description, name),
            )
    return True


def archive_project(name: str) -> bool:
    """Set project status to ARCHIVED. Returns True if found."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE projects SET status='ARCHIVED', updated_at=CURRENT_TIMESTAMP WHERE name=?",
            (name,),
        )
    return True


def delete_project(name: str) -> bool:
    """Delete a project and its model assignments. Returns True if found."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            return False
        conn.execute("DELETE FROM project_models WHERE project=?", (name,))
        conn.execute("DELETE FROM projects WHERE name=?", (name,))
    return True


def assign_model_to_project(project: str, model: str) -> bool:
    """Assign a model to a project. Returns False if project not found."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT name FROM projects WHERE name=?", (project,)).fetchone()
        if not row:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO project_models (project, model) VALUES (?,?)",
            (project, model),
        )
    return True


def list_project_models(project: str) -> list[str]:
    """Return model names assigned to a project."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT model FROM project_models WHERE project=? ORDER BY model", (project,)
        ).fetchall()
    return [r["model"] for r in rows]


# ── Autopilot (ADR 0085): self-driving closed-loop detect→retrain→promote ────


def get_autopilot_config(key: str) -> str | None:
    """Return a value from autopilot_config, or None if not set."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT value FROM autopilot_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_autopilot_config(key: str, value: str) -> None:
    """Upsert a key/value pair in autopilot_config."""
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO autopilot_config (key, value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )


def create_autopilot_run(
    triggered_by: str = "manual",
    model_filter: str | None = None,
    dry_run: bool = False,
    enabled_state: str = "enabled",
) -> int:
    """Insert a new autopilot_runs row and return its id."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO autopilot_runs
               (triggered_by, model_filter, dry_run, enabled_state)
               VALUES (?,?,?,?)""",
            (triggered_by, model_filter, int(dry_run), enabled_state),
        )
        return cur.lastrowid  # type: ignore[return-value]


def update_autopilot_run(
    run_id: int,
    *,
    retrains_triggered: int = 0,
    promotions_made: int = 0,
    policy_blocks: int = 0,
    human_required: int = 0,
    skipped: int = 0,
    summary: dict[str, Any] | None = None,
) -> None:
    """Update counts and summary for a completed autopilot run."""
    with get_db() as conn:
        conn.execute(
            """UPDATE autopilot_runs
               SET retrains_triggered=?, promotions_made=?, policy_blocks=?,
                   human_required=?, skipped=?, summary=?
               WHERE id=?""",
            (
                retrains_triggered,
                promotions_made,
                policy_blocks,
                human_required,
                skipped,
                json.dumps(summary) if summary else None,
                run_id,
            ),
        )


def list_autopilot_runs(last_n: int = 10) -> list[dict[str, Any]]:
    """Return the last N autopilot run records, newest first."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM autopilot_runs ORDER BY id DESC LIMIT ?", (last_n,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Next-Gen 40 · A1 — dataset revisions (ADR 0003) --------------------------
# ``rev`` is any object exposing the DatasetRevision fields (backend, dataset,
# revision_id, kind, uri, schema_hash). Kept duck-typed so this platform-layer
# module never imports the pipelines package (avoids a layering cycle).


def record_dataset_revision(
    rev: Any,
    *,
    mlflow_run_id: str | None = None,
    row_count: int | None = None,
    byte_count: int | None = None,
    actor: str | None = None,
) -> None:
    """Record a resolved dataset revision.

    Idempotent on ``(backend, dataset, revision_id)`` (spec R5): re-recording the
    same revision is a no-op and never raises a UNIQUE-constraint error.
    """
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO dataset_revisions
                   (backend, dataset, revision_id, kind, uri, schema_hash,
                    mlflow_run_id, row_count, byte_count, actor)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(backend, dataset, revision_id) DO NOTHING""",
            (
                rev.backend,
                rev.dataset,
                rev.revision_id,
                getattr(rev, "kind", "content"),
                getattr(rev, "uri", None),
                getattr(rev, "schema_hash", None),
                mlflow_run_id,
                row_count,
                byte_count,
                actor,
            ),
        )


def get_dataset_revisions(dataset: str, backend: str | None = None) -> list[dict[str, Any]]:
    """Return recorded revisions for ``dataset`` newest-first (spec R9).

    When ``backend`` is given, restrict to that backend.
    """
    init_db()
    with get_db() as conn:
        if backend:
            rows = conn.execute(
                "SELECT * FROM dataset_revisions WHERE dataset=? AND backend=? ORDER BY id DESC",
                (dataset, backend),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM dataset_revisions WHERE dataset=? ORDER BY id DESC",
                (dataset,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_dataset_revision(
    dataset: str, revision_id: str, backend: str | None = None
) -> dict[str, Any] | None:
    """Return a single recorded revision by id, or None if absent."""
    for row in get_dataset_revisions(dataset, backend):
        if row["revision_id"] == revision_id:
            return row
    return None


# --- Next-Gen 40 · A5 — data contracts / quality gates (ADR 0005) ------------


def record_data_quality_check(
    dataset: str,
    result: Any,
    *,
    revision: str | None = None,
    stage: str = "train",
    model: str = "-",
    actor: str | None = None,
) -> None:
    """Record a contract-validation outcome (spec R7).

    ``result`` is a QualityResult-like object exposing ``passed``, ``score``, and
    ``checks`` (kept duck-typed so this layer never imports the pipelines package).
    """
    init_db()
    checks = list(getattr(result, "checks", []))
    passed_n = sum(1 for c in checks if c.get("passed"))
    failed_n = len(checks) - passed_n
    status = "PASS" if getattr(result, "passed", False) else "FAIL"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO data_quality_checks
                   (model, dataset, status, passed, failed, details_json, actor,
                    revision, stage, score)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                model,
                dataset,
                status,
                passed_n,
                failed_n,
                json.dumps(checks),
                actor,
                revision,
                stage,
                float(getattr(result, "score", 0.0)),
            ),
        )


def get_data_quality_checks(dataset: str, last_n: int = 20) -> list[dict[str, Any]]:
    """Return recent quality-check rows for ``dataset``, newest first."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM data_quality_checks WHERE dataset=? ORDER BY id DESC LIMIT ?",
            (dataset, last_n),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Next-Gen 40 · B1 — prompt registry (ADR 0009) ---------------------------


def create_prompt_version(
    name: str,
    template: str,
    *,
    variables: list[str] | None = None,
    tags: dict[str, Any] | None = None,
    actor: str | None = None,
) -> int:
    """Create a new immutable prompt version (spec R1). Returns the new version number."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM prompt_versions WHERE name=?", (name,)
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            """INSERT INTO prompt_versions (name, version, template, variables, tags, actor)
               VALUES (?,?,?,?,?,?)""",
            (
                name,
                version,
                template,
                json.dumps(variables or []),
                json.dumps(tags or {}),
                actor,
            ),
        )
    return version


def get_prompt_version(name: str, version: int) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM prompt_versions WHERE name=? AND version=?", (name, version)
        ).fetchone()
    return dict(row) if row else None


def list_prompt_versions(name: str) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_versions WHERE name=? ORDER BY version DESC", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_prompt_names() -> list[str]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT DISTINCT name FROM prompt_versions ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def set_prompt_label(name: str, label: str, version: int) -> None:
    """Point a label at a version (spec R8). Caller writes the audit event (R9)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO prompt_labels (name, label, version, updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(name, label) DO UPDATE SET
                   version=excluded.version, updated_at=CURRENT_TIMESTAMP""",
            (name, label, version),
        )


def get_prompt_by_label(name: str, label: str) -> dict[str, Any] | None:
    """Resolve ``name@label`` to its pinned prompt version (spec R3)."""
    init_db()
    with get_db() as conn:
        lab = conn.execute(
            "SELECT version FROM prompt_labels WHERE name=? AND label=?", (name, label)
        ).fetchone()
    if lab is None:
        return None
    return get_prompt_version(name, int(lab["version"]))


def list_prompt_labels(name: str) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM prompt_labels WHERE name=? ORDER BY label", (name,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Next-Gen 40 · D7 — local encrypted secrets store (ADR 0011) --------------
# Stores/returns opaque ciphertext only; encryption/decryption lives in the
# examlops.secrets client so this DB layer never sees a plaintext secret.


def put_secret_ciphertext(
    path: str, tenant: str, ciphertext: str, *, updated_by: str | None = None
) -> int:
    """Upsert an encrypted secret, bumping its version. Returns the new version."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT version FROM secrets_store WHERE path=? AND tenant=?", (path, tenant)
        ).fetchone()
        version = (int(row["version"]) + 1) if row else 1
        conn.execute(
            """INSERT INTO secrets_store (path, tenant, ciphertext, version, updated_by, updated_at)
               VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(path, tenant) DO UPDATE SET
                   ciphertext=excluded.ciphertext, version=excluded.version,
                   updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP""",
            (path, tenant, ciphertext, version, updated_by),
        )
    return version


def get_secret_ciphertext(path: str, tenant: str) -> str | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT ciphertext FROM secrets_store WHERE path=? AND tenant=?", (path, tenant)
        ).fetchone()
    return row["ciphertext"] if row else None


def list_secret_paths(tenant: str | None = None) -> list[dict[str, Any]]:
    """List secret metadata (path/tenant/version/updated_at) — never values."""
    init_db()
    with get_db() as conn:
        if tenant:
            rows = conn.execute(
                "SELECT path, tenant, version, updated_by, updated_at FROM secrets_store "
                "WHERE tenant=? ORDER BY path",
                (tenant,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT path, tenant, version, updated_by, updated_at FROM secrets_store "
                "ORDER BY tenant, path"
            ).fetchall()
    return [dict(r) for r in rows]


# --- Next-Gen 40 · D6 — authz relations (ADR 0014) ---------------------------


def grant_relation(subject: str, relation: str, obj: str, *, actor: str | None = None) -> None:
    """Grant ``subject`` a ``relation`` on ``obj`` (idempotent)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO authz_relations (subject, relation, object, actor)
               VALUES (?,?,?,?)
               ON CONFLICT(subject, relation, object) DO NOTHING""",
            (subject, relation, obj, actor),
        )


def revoke_relation(subject: str, relation: str, obj: str) -> int:
    """Revoke a relation. Returns rows deleted (0 if none)."""
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            "DELETE FROM authz_relations WHERE subject=? AND relation=? AND object=?",
            (subject, relation, obj),
        )
        return cur.rowcount


def get_relations_for(subject: str, obj: str) -> list[str]:
    """Return the relations ``subject`` holds directly on ``obj``."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT relation FROM authz_relations WHERE subject=? AND object=?", (subject, obj)
        ).fetchall()
    return [r["relation"] for r in rows]


def list_relations(subject: str | None = None, obj: str | None = None) -> list[dict[str, Any]]:
    init_db()
    clauses, params = [], []
    if subject:
        clauses.append("subject=?")
        params.append(subject)
    if obj:
        clauses.append("object=?")
        params.append(obj)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM authz_relations{where} ORDER BY object, subject", params
        ).fetchall()
    return [dict(r) for r in rows]


def list_objects_for(subject: str) -> list[dict[str, Any]]:
    """All (object, relation) pairs granted to ``subject``."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT object, relation FROM authz_relations WHERE subject=? ORDER BY object",
            (subject,),
        ).fetchall()
    return [dict(r) for r in rows]


# --- Next-Gen 40 · D3 — model signing + AI-BOM (ADR 0013) --------------------


def store_model_signature(
    model: str,
    version: str,
    digest: str,
    signature: str,
    *,
    algo: str = "hmac-sha256",
    cert: str | None = None,
    signed_by: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO model_signatures
                   (model, version, digest, algo, signature, cert, signed_by, signed_at)
               VALUES (?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(model, version) DO UPDATE SET
                   digest=excluded.digest, algo=excluded.algo, signature=excluded.signature,
                   cert=excluded.cert, signed_by=excluded.signed_by, signed_at=CURRENT_TIMESTAMP""",
            (model, version, digest, algo, signature, cert, signed_by),
        )


def get_model_signature(model: str, version: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM model_signatures WHERE model=? AND version=?", (model, version)
        ).fetchone()
    return dict(row) if row else None


def store_model_bom(model: str, version: str, bom: dict[str, Any]) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO model_boms (model, version, bom_json, created_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(model, version) DO UPDATE SET
                   bom_json=excluded.bom_json, created_at=CURRENT_TIMESTAMP""",
            (model, version, json.dumps(bom)),
        )


def get_model_bom(model: str, version: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT bom_json FROM model_boms WHERE model=? AND version=?", (model, version)
        ).fetchone()
    return json.loads(row["bom_json"]) if row else None


# ── A2 — OpenLineage dual-write + graph/impact queries (ADR 0004) ─────────────


def record_lineage_event(
    run_id: str,
    job: str,
    event_type: str,
    *,
    inputs: list[dict[str, str]] | None = None,
    outputs: list[dict[str, str]] | None = None,
    dataset_revision: str | None = None,
    mlflow_run_id: str | None = None,
    model: str | None = None,
    model_version: str | None = None,
    trace_id: str | None = None,
    facets: dict[str, Any] | None = None,
) -> None:
    """Upsert a lineage run event + its I/O nodes (the operational source of truth)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO lineage_events
                   (run_id, job, event_type, dataset_revision, mlflow_run_id,
                    model, model_version, trace_id, facets_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                job,
                event_type,
                dataset_revision,
                mlflow_run_id,
                model,
                model_version,
                trace_id,
                json.dumps(facets) if facets else None,
            ),
        )
        for direction, nodes in (("input", inputs or []), ("output", outputs or [])):
            for node in nodes:
                conn.execute(
                    """INSERT OR IGNORE INTO lineage_io
                           (run_id, direction, node_type, node_name)
                       VALUES (?,?,?,?)""",
                    (run_id, direction, node.get("type", "dataset"), node["name"]),
                )


def lineage_graph(model: str) -> dict[str, Any]:
    """Return upstream (datasets/runs) + downstream (deployments) nodes for a model."""
    init_db()
    with get_db() as conn:
        runs = conn.execute(
            "SELECT * FROM lineage_events WHERE model=? ORDER BY ts DESC", (model,)
        ).fetchall()
        run_ids = [r["run_id"] for r in runs]
        io_rows: list[dict[str, Any]] = []
        for rid in run_ids:
            io_rows.extend(
                dict(r)
                for r in conn.execute("SELECT * FROM lineage_io WHERE run_id=?", (rid,)).fetchall()
            )
    return {
        "model": model,
        "runs": [dict(r) for r in runs],
        "upstream": [r for r in io_rows if r["direction"] == "input"],
        "downstream": [r for r in io_rows if r["direction"] == "output"],
    }


def lineage_impact(dataset_revision: str) -> list[dict[str, Any]]:
    """List every model version derived (transitively) from a dataset revision."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT model, model_version, run_id, mlflow_run_id
               FROM lineage_events
               WHERE dataset_revision=? AND model IS NOT NULL
               ORDER BY model, model_version""",
            (dataset_revision,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── C2 — continuous-eval suite results (ADR 0007) ─────────────────────────────


def record_eval_result(
    suite: str,
    model: str,
    scores: dict[str, float],
    *,
    run_id: str,
    model_version: str | None = None,
    alias: str | None = None,
    sample_size: int = 0,
    judge: dict[str, str] | None = None,
    dataset_revision: str | None = None,
) -> None:
    """Persist per-metric suite scores; idempotent per (suite, version, run, metric) (R7/R8)."""
    init_db()
    judge_model = (judge or {}).get("model")
    judge_prompt = (judge or {}).get("prompt_version")
    with get_db() as conn:
        for metric, score in scores.items():
            conn.execute(
                """INSERT OR IGNORE INTO eval_suite_results
                       (suite, model, model_version, alias, metric, score, sample_size,
                        judge_model, judge_prompt_version, dataset_revision, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    suite,
                    model,
                    model_version,
                    alias,
                    metric,
                    float(score),
                    sample_size,
                    judge_model,
                    judge_prompt,
                    dataset_revision,
                    run_id,
                ),
            )


def get_eval_results(
    model: str, suite: str | None = None, *, alias: str | None = None
) -> list[dict[str, Any]]:
    init_db()
    q = "SELECT * FROM eval_suite_results WHERE model=?"
    params: list[Any] = [model]
    if suite:
        q += " AND suite=?"
        params.append(suite)
    if alias:
        q += " AND alias=?"
        params.append(alias)
    q += " ORDER BY ts DESC"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(q, params).fetchall()]


# ── C3 — eval regression gate config + reports (ADR 0008) ─────────────────────


def set_eval_gate(
    model: str,
    suite: str,
    metrics: list[dict[str, Any]],
    *,
    baseline_alias: str = "Production",
    mode: str = "block",
    updated_by: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO eval_gates (model, suite, baseline_alias, metrics_json, mode, updated_by)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(model) DO UPDATE SET
                   suite=excluded.suite, baseline_alias=excluded.baseline_alias,
                   metrics_json=excluded.metrics_json, mode=excluded.mode,
                   updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP""",
            (model, suite, baseline_alias, json.dumps(metrics), mode, updated_by),
        )


def get_eval_gate(model: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM eval_gates WHERE model=?", (model,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["metrics"] = json.loads(d.pop("metrics_json"))
    return d


def record_gate_report(
    model: str,
    passed: bool,
    mode: str,
    report: dict[str, Any],
    *,
    candidate: str | None = None,
    baseline: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO gate_reports (model, candidate, baseline, passed, mode, report_json)
               VALUES (?,?,?,?,?,?)""",
            (model, candidate, baseline, 1 if passed else 0, mode, json.dumps(report)),
        )


def get_gate_reports(model: str, limit: int = 20) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM gate_reports WHERE model=? ORDER BY ts DESC LIMIT ?", (model, limit)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["report"] = json.loads(d.pop("report_json"))
        out.append(d)
    return out


# ── B2 — model-gateway virtual keys + per-call cost (ADR 0010) ────────────────


def create_virtual_key(
    key_hash: str,
    *,
    tenant: str = "default",
    project: str = "default",
    models: list[str] | None = None,
    budget_usd: float | None = None,
    created_by: str | None = None,
) -> None:
    """Store a virtual key (only its hash — never the raw key)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO virtual_keys
                   (key_hash, tenant, project, models_json, budget_usd, spent_usd, created_by)
               VALUES (?,?,?,?,?,
                   COALESCE((SELECT spent_usd FROM virtual_keys WHERE key_hash=?), 0), ?)""",
            (
                key_hash,
                tenant,
                project,
                json.dumps(models or []),
                budget_usd,
                key_hash,
                created_by,
            ),
        )


def get_virtual_key(key_hash: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM virtual_keys WHERE key_hash=?", (key_hash,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["models"] = json.loads(d.pop("models_json"))
    return d


def list_virtual_keys() -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM virtual_keys ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["models"] = json.loads(d.pop("models_json"))
        out.append(d)
    return out


def add_key_spend(key_hash: str, cost_usd: float) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE virtual_keys SET spent_usd = spent_usd + ? WHERE key_hash=?",
            (cost_usd, key_hash),
        )


def revoke_virtual_key(key_hash: str) -> None:
    init_db()
    with get_db() as conn:
        conn.execute("UPDATE virtual_keys SET revoked=1 WHERE key_hash=?", (key_hash,))


def record_gateway_call(
    key_hash: str | None,
    model: str,
    *,
    backend: str | None = None,
    cost_usd: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO gateway_calls
                   (key_hash, model, backend, cost_usd, prompt_tokens, completion_tokens)
               VALUES (?,?,?,?,?,?)""",
            (key_hash, model, backend, cost_usd, prompt_tokens, completion_tokens),
        )


def total_gateway_cost(key_hash: str | None = None) -> float:
    init_db()
    with get_db() as conn:
        if key_hash:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS t FROM gateway_calls WHERE key_hash=?",
                (key_hash,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS t FROM gateway_calls"
            ).fetchone()
    return float(row["t"])
