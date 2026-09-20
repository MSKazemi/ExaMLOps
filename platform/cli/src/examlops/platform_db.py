from __future__ import annotations

import functools
import inspect
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from examlops.resilience import db as _rdb


def _db_path() -> str:
    if explicit := os.getenv("PLATFORM_DB"):
        return explicit
    # ADR 0128: with a data root the datastore lives there, not beside the code.
    if root := os.getenv("EXAMLOPS_DATA_DIR", "").strip():
        return str(Path(root).expanduser() / "platform.db")
    return _legacy_db_default()


@functools.cache
def _legacy_db_default() -> str:
    """The datastore location when neither ``PLATFORM_DB`` nor a data root is set.

    A source checkout keeps its historical ``<repo>/platform.db``. An installed wheel has no repo:
    ``parents[4]`` is then the venv prefix, so the datastore would land inside the software it is
    meant to outlive — it goes to the user data dir (``$XDG_DATA_HOME/examlops``) instead.
    """
    repo = Path(__file__).parents[4]
    if (repo / "platform" / "cli" / "pyproject.toml").is_file():
        return str(repo / "platform.db")
    xdg = os.getenv("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return str(base / "examlops" / "platform.db")


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Open a hardened connection to the shared platform SQLite DB.

    Uses the shared :mod:`examlops.resilience.db` helper so every one of the ~170
    call sites (and the 3 concurrent long-lived writers: CLI, agent service,
    dataplane-bus bridge) gets WAL + ``synchronous=NORMAL`` + a ``busy_timeout`` that
    waits out lock contention instead of raising ``database is locked`` immediately,
    plus ``check_same_thread=False`` for the threaded services.
    """
    conn = _open_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _open_conn() -> Any:
    """The datastore engine for this process (enterprise-readiness item 0.1 follow-on).

    ``EXAMLOPS_DB_BACKEND=sqlite`` (the default) is the unchanged direct path — same call, same
    connection object, no seam in the way. ``postgres`` returns a translating connection
    (:mod:`examlops.storage.pg`) that speaks the same SQLite-shaped API, which is why none of the
    ~252 helpers above needed to change.
    """
    if os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() != "postgres":
        return _rdb.connect(_db_path())
    from examlops.storage import get_backend

    return get_backend().connect()


def write_retry[T](fn: Callable[[], T]) -> T:
    """Run a DB write, retrying the whole transaction if it loses a lock race.

    Belt-and-suspenders on top of the connection ``busy_timeout`` for the highest-
    frequency writers (e.g. the per-inference bridge snapshots). Example::

        write_retry(lambda: write_drift_snapshot(model, alias, pred, job_id))
    """
    return _rdb.write_retry(fn)


# --- Central write-retry coverage (enterprise-readiness Phase 0, item 0.4) -------------------
# Every mutating helper in this module must survive a transient ``database is locked`` past the
# busy_timeout, and must never let a lost write vanish inside a fire-and-forget caller. Rather than
# hand-decorate ~80 helpers (churn + drift as new ones land), :func:`_install_write_retry` runs once
# at import and transparently wraps every public helper whose source performs a write, skipping the
# two that already call :func:`write_retry` internally (the audit hash-chain + atomic drift claim).
# The wrap is idempotent-safe: these helpers open a fresh ``get_db()`` transaction, so a lock error
# means nothing committed and re-running the whole function cannot double-write.

_WRITE_MARKERS = ("INSERT ", "UPDATE ", "DELETE ", " REPLACE", "OR REPLACE", "BEGIN IMMEDIATE")


def _wrap_mutating[T](fn: Callable[..., T]) -> Callable[..., T]:
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        return _rdb.write_retry(lambda: fn(*args, **kwargs))

    wrapper._wr_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def install_write_retry(module_name: str) -> None:
    """Wrap all mutating public helpers **in ``module_name``** with :func:`write_retry` (item 0.4).

    Auto-discovers writers by source inspection so coverage is self-maintaining: a new mutating
    helper is protected the moment it lands, no allow-list to update. Degrades to a no-op if source
    is unavailable (frozen/zipimport). Generalized (parametric on the module) so a relocated
    per-domain module (item 4.5 body relocation) can call ``install_write_retry(__name__)`` to
    protect its own mutating helpers exactly as ``platform_db`` does — the auto-wrapper is no longer
    hard-coupled to this one module."""
    mod = sys.modules[module_name]
    for name, obj in list(vars(mod).items()):
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        if getattr(obj, "__module__", None) != module_name or getattr(obj, "_wr_wrapped", False):
            continue
        try:
            src = inspect.getsource(obj)
        except (OSError, TypeError):  # pragma: no cover - frozen/zipimport fallback
            continue
        if not any(marker in src for marker in _WRITE_MARKERS):
            continue  # pure reader — nothing to protect
        if "write_retry(" in src:
            continue  # already self-retries (write_audit_event, claim_drift_trigger)
        setattr(mod, name, _wrap_mutating(obj))


def _install_write_retry() -> None:  # back-compat wrapper for this module
    install_write_retry(__name__)


def begin_immediate(scope: str | None = None) -> str:
    """The ``BEGIN IMMEDIATE`` statement for a write lock, optionally scoped (see ``lock_key``)."""
    if scope is None:
        return "BEGIN IMMEDIATE"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", scope):
        raise ValueError(f"invalid lock scope {scope!r}")
    return f"BEGIN IMMEDIATE /* lock:{scope} */"


@contextmanager
def _immediate_write(scope: str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """A hardened connection holding an IMMEDIATE (RESERVED) write lock for the whole txn.

    Use this for read-modify-write sequences that must be atomic against other *writer
    processes* — chiefly the audit hash-chain, where reading the current head and appending
    the next link must not interleave with another writer (or the chain forks: two rows chain
    off the same parent and :func:`verify_audit_chain` reports a prev_hash mismatch). The plain
    :func:`get_db` opens a *deferred* transaction, so its head-read runs before any lock is
    held; ``BEGIN IMMEDIATE`` takes the RESERVED lock up front instead. Pair with
    :func:`write_retry` so a lost lock race (``database is locked`` after busy_timeout) retries
    the whole transaction rather than corrupting or dropping the write.
    """
    conn = _open_conn()  # same engine as get_db(): the audit chain must not bypass the backend
    conn.isolation_level = None  # drive BEGIN/COMMIT explicitly (no implicit deferred txn)
    try:
        # Postgres: a transaction-scoped advisory lock, per scope (plan P1.3). SQLite: RESERVED.
        conn.execute(begin_immediate(scope))
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - engine-specific; a failed rollback must not mask the cause
            pass
        raise
    finally:
        conn.close()


# Process-level sentinel of DB paths whose schema has been created this process, so the full
# CREATE-TABLE-IF-NOT-EXISTS script + column migrations run once — not on every one of the ~165
# defensive `init_db()` calls, which otherwise churned a write lock on hot read paths (item 0.5/QW8).
_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.RLock()  # re-entrant: a bootstrap step may open the database


def _init_key() -> str:
    """What "the database" means for the schema-once cache.

    On SQLite that is the file path. On Postgres the file path is meaningless — the DSN plus the
    schema is the database — so tests that point ``PLATFORM_DB`` at a fresh tmp file must not each
    re-run the 127-table DDL against the same Postgres schema.
    """
    if os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() != "postgres":
        return _db_path()
    return f"pg:{os.getenv('EXAMLOPS_POSTGRES_DSN', '')}:{os.getenv('EXAMLOPS_POSTGRES_SCHEMA', 'public')}"


def init_db(*, force: bool = False) -> None:
    """Create the platform schema. Idempotent, and near-free after the first call per DB path.

    The 100+-table DDL and additive column migrations run once per process per ``PLATFORM_DB``
    path (guarded by :data:`_INITIALIZED_PATHS`); subsequent calls short-circuit. Pass
    ``force=True`` to re-run regardless — e.g. after intentionally dropping tables in a test.
    In-memory DBs are never cached, since each new connection is a distinct database.
    """
    path = _init_key()
    cacheable = path not in (":memory:", "") and not path.startswith("file::memory:")
    if not force and cacheable and path in _INITIALIZED_PATHS:
        return
    # One bootstrap per process at a time. Threads of one process opening a new database together
    # otherwise ran it concurrently, and one met the schema the other was changing ("database
    # schema has changed"): tests/unit/test_schema_bootstrap_race.py failed 3 runs in 15 without
    # this. The waiter finds the path initialised and returns.
    with _INIT_LOCK:
        if not force and cacheable and path in _INITIALIZED_PATHS:
            return
        _bootstrap_schema(path, cacheable)


def _executescript_retrying(conn: Any, script: str, attempts: int = 8) -> None:
    """Run the schema script, again if another process changed the schema under it.

    Several processes opening a new SQLite database at once each run the script. A statement can
    then fail with "database schema has changed" (SQLITE_SCHEMA) when another process committed DDL
    between its preparation and its execution; the per-process lock above cannot prevent that
    across processes. Every statement is ``IF NOT EXISTS``, so running the script again is safe:
    tests/unit/test_schema_bootstrap_race.py failed 2 of 18 runs of 8 processes under load without
    this. Postgres serialises the bootstrap with its ``schema`` lock and raises other errors.
    """
    for attempt in range(attempts):
        try:
            conn.executescript(script)
            return
        except sqlite3.OperationalError as exc:
            if "schema has changed" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def _bootstrap_schema(path: str, cacheable: bool) -> None:
    with get_db() as conn:
        if path.startswith("pg:"):
            # Concurrent first boots on an empty Postgres (control plane + dashboard + agent
            # replicas) raced `CREATE TABLE IF NOT EXISTS`: IF NOT EXISTS is not atomic there, and
            # the loser failed with a duplicate key on pg_type. The whole bootstrap is one
            # transaction, so a transaction-scoped advisory lock serialises it and the second
            # process finds every table already there. SQLite's file lock already does this.
            conn.execute(begin_immediate("schema"))
        _executescript_retrying(conn, """
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
            -- One row per inference; every reader filters `WHERE model=? ORDER BY ts DESC`.
            -- Index it so drift status doesn't full-scan the hottest table (C6).
            CREATE INDEX IF NOT EXISTS ix_drift_snapshots_model_ts
                ON drift_snapshots (model, ts DESC);
            -- The last drift status recorded per model, so a change is announced once
            -- (drift.status_changed, plan P2.4b) and not on every evaluation.
            CREATE TABLE IF NOT EXISTS drift_status_state (
                model        TEXT PRIMARY KEY,
                status       TEXT NOT NULL,
                z_score      REAL,
                changed_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                evaluated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
            -- Same access pattern as drift_snapshots: per-inference rows read newest-first
            -- per model (C6).
            CREATE INDEX IF NOT EXISTS ix_input_snapshots_model_ts
                ON input_snapshots (model, ts DESC);
            CREATE TABLE IF NOT EXISTS input_baselines (
                model     TEXT PRIMARY KEY,
                stats     TEXT NOT NULL,
                set_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- ADR 0114: the zero-rate/spread reference a corruption signal is judged
            -- against. Separate from drift_baselines because it answers a different
            -- question — "is this machine lying?" rather than "has the data moved?".
            -- ADR 0117: the portability gate's measured divergence, recorded whatever the
            -- verdict. A gate that stores only pass/fail hides drift toward its own tolerance
            -- boundary until the moment it crosses it.
            CREATE TABLE IF NOT EXISTS parity_checks (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                 DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                model              TEXT NOT NULL,
                source_version     TEXT,
                target_version     TEXT,
                verdict            TEXT NOT NULL,
                max_abs_divergence REAL,
                max_rel_divergence REAL,
                tolerance          REAL,
                n_fixtures         INTEGER NOT NULL DEFAULT 0,
                transformed        INTEGER NOT NULL DEFAULT 0,
                reason             TEXT
            );
            CREATE TABLE IF NOT EXISTS corruption_baselines (
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
            -- Capacity/availability queries filter by (cluster, state); index it so fleet-scale
            -- node snapshots don't force a full scan (Phase 0 bonus win).
            CREATE INDEX IF NOT EXISTS ix_hpc_nodes_cluster_state
                ON hpc_nodes (cluster, state);
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
            -- Phase 1 item 1.2 — externalized coordination primitives (distributed lock,
            -- idempotency dedup, fixed-window rate limit). DB-backed so they work cross-PROCESS
            -- today (CLI + control-plane + agent share platform.db); the same Coordinator seam
            -- swaps to Redis for cross-HOST HA with no caller change.
            CREATE TABLE IF NOT EXISTS coord_locks (
                key        TEXT PRIMARY KEY,
                holder     TEXT NOT NULL,
                expires_at DATETIME NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coord_idempotency (
                key        TEXT PRIMARY KEY,
                first_seen DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at DATETIME NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coord_rate (
                bucket       TEXT PRIMARY KEY,
                window_start DATETIME NOT NULL,
                count        INTEGER NOT NULL DEFAULT 0
            );
            -- ADR 0147 decision 4 — result-carrying idempotency keys for agent-callable
            -- mutating tools (`examlops.idempotency`). Epoch-second REAL timestamps keep the
            -- expiry arithmetic dialect-neutral (sqlite and the Postgres translation layer).
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                key          TEXT PRIMARY KEY,
                scope        TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                state        TEXT NOT NULL DEFAULT 'pending',  -- pending | done
                result_json  TEXT,
                created_at   REAL NOT NULL,
                expires_at   REAL NOT NULL
            );
            -- ADR 0147 decision 2 — plan/apply for agent principals (`examlops.plans`). One row per
            -- plan_hash (sha256 of tool+args+preconditions); `state` is planned | applying |
            -- applied | failed | expired | rejected. Epoch-second REAL timestamps, like
            -- idempotency_keys, keep the expiry arithmetic dialect-neutral.
            CREATE TABLE IF NOT EXISTS agent_plans (
                plan_hash     TEXT PRIMARY KEY,
                tool          TEXT NOT NULL,
                plan_json     TEXT NOT NULL,
                state         TEXT NOT NULL DEFAULT 'planned',
                actor         TEXT,
                created_at    REAL NOT NULL,
                expires_at    REAL NOT NULL,
                approval_hash TEXT,
                approved_by   TEXT,
                applied_at    REAL,
                applied_by    TEXT,
                result_json   TEXT
            );
            -- ADR 0035 clause 2 — gateway reasoning budgets (`examlops.structured`). A cap per
            -- (scope, ref, tenant), scope in key|project|model; the tightest applicable one wins.
            CREATE TABLE IF NOT EXISTS reasoning_budgets (
                scope               TEXT NOT NULL,
                ref                 TEXT NOT NULL,
                tenant              TEXT NOT NULL DEFAULT 'default',
                max_thinking_tokens INTEGER NOT NULL,
                updated_at          REAL NOT NULL,
                PRIMARY KEY (scope, ref, tenant)
            );
            -- What the gateway observed against a budget: within|exceeded|unknown|refused.
            CREATE TABLE IF NOT EXISTS reasoning_budget_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL NOT NULL,
                model           TEXT NOT NULL,
                tenant          TEXT NOT NULL DEFAULT 'default',
                key_hash        TEXT,
                project         TEXT,
                outcome         TEXT NOT NULL,
                budget_tokens   INTEGER,
                observed_tokens INTEGER,
                source          TEXT,
                request_id      TEXT
            );
            -- ADR 0117 decision 2 — paired (TTFT, TPOT) serving SLOs (`examlops.slo.pairs`). One row
            -- per (model, name, tenant); `tight` names which dimension is the binding one.
            CREATE TABLE IF NOT EXISTS slo_pairs (
                model       TEXT NOT NULL,
                name        TEXT NOT NULL,
                tenant      TEXT NOT NULL DEFAULT 'default',
                ttft_ms     REAL NOT NULL,
                tpot_ms     REAL NOT NULL,
                percentile  REAL NOT NULL DEFAULT 99,
                tight       TEXT NOT NULL,
                slo_class   TEXT NOT NULL DEFAULT 'interactive',
                updated_at  REAL NOT NULL,
                PRIMARY KEY (model, name, tenant)
            );
            -- Phase 1 item 1.5 — durable admission-control queue between every trigger
            -- (drift/autopilot/API/webhook) and Prefect. Per-tenant fair-share + a global
            -- concurrency cap stop one tenant (or a fleet-wide drift event) from starving the
            -- cluster. Survives a restart; a crashed worker's 'running' item is reclaimable by TTL.
            CREATE TABLE IF NOT EXISTS admission_queue (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant       TEXT NOT NULL DEFAULT 'default',
                project      TEXT,
                kind         TEXT NOT NULL,           -- retrain | pipeline | ...
                payload      TEXT NOT NULL,           -- JSON
                priority     INTEGER NOT NULL DEFAULT 0,   -- higher runs first within a tenant
                state        TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|rejected|failed
                enqueued_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at   DATETIME,
                finished_at  DATETIME,
                reason       TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_admission_state_tenant
                ON admission_queue (state, tenant, priority, id);
            -- Phase 1 item 1.3 — transactional outbox for the NovaFabric event backbone. A domain
            -- write and its event enqueue commit together (same DB txn); a relay then publishes each
            -- row at least once to the broker and stamps published_at. Consumers deduplicate using
            -- the stable outbox row ID carried in every broker envelope.
            -- Replaces O(models×replicas) polling + the in-process realtime singleton.
            CREATE TABLE IF NOT EXISTS event_outbox (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                topic        TEXT NOT NULL,
                payload      TEXT NOT NULL,           -- JSON
                created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                published_at DATETIME,                -- NULL until relayed
                claimed_at   DATETIME,                -- set when a relay claims it (visibility lease)
                attempts     INTEGER NOT NULL DEFAULT 0,
                last_error   TEXT,
                actor        TEXT NOT NULL DEFAULT 'system',
                tenant       TEXT NOT NULL DEFAULT 'default',
                traceparent  TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_event_outbox_unpublished
                ON event_outbox (published_at, id);
            -- ADR 0124 — consumer inbox: one row per (consumer, event) handled, so a redelivered
            -- event (at-least-once) never repeats its effect.
            CREATE TABLE IF NOT EXISTS event_inbox (
                consumer     TEXT NOT NULL,
                event_id     TEXT NOT NULL,
                handled_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (consumer, event_id)
            );
            -- Phase 0 item 0.12 — single-row distributed cycle lease so overlapping cron
            -- runs (or multiple agent replicas) don't scan/act concurrently. TTL-based so a
            -- crashed holder's lease auto-expires. Correctness of no-double-retrain is already
            -- guaranteed by claim_drift_trigger; this is the coarser cycle-level guard.
            CREATE TABLE IF NOT EXISTS autopilot_lease (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                holder      TEXT NOT NULL,
                acquired_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at  DATETIME NOT NULL
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
                key_id      TEXT,                   -- KEK the ciphertext is wrapped under (2.3)
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
            -- ADR 0130 — dataplane source registry + pull history. Snapshots live in the dataset
            -- store (self-describing manifests); these rows are an index `exa dataplane
            -- catalog-rebuild` can recreate. Column names avoid SQL keywords (Postgres backend).
            CREATE TABLE IF NOT EXISTS dataplane_sources (
                project      TEXT NOT NULL DEFAULT '',
                name         TEXT NOT NULL,
                connector    TEXT NOT NULL,
                connection   TEXT,
                spec_json    TEXT NOT NULL DEFAULT '{}',
                schedule     TEXT,
                limits_json  TEXT NOT NULL DEFAULT '{}',
                contract     TEXT,
                enabled      INTEGER NOT NULL DEFAULT 1,
                created_by   TEXT,
                created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, name)
            );
            CREATE TABLE IF NOT EXISTS dataplane_pulls (
                id              TEXT PRIMARY KEY,
                project         TEXT NOT NULL DEFAULT '',
                source          TEXT NOT NULL,
                status          TEXT NOT NULL,
                trigger_kind    TEXT NOT NULL DEFAULT 'manual',
                actor           TEXT,
                started_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at     DATETIME,
                revision        TEXT,
                parent_revision TEXT,
                row_count       INTEGER,
                byte_count      INTEGER,
                watermark_json  TEXT,
                error           TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dataplane_pulls_source
                ON dataplane_pulls(project, source, id);
            -- Plan 2 (ADR 0130/0131) task A6 — stream catalog: named inbound-connector
            -- bindings (Kafka/HTTP push/Dataplane bus req-res) that route a live request to a
            -- project/model/alias. PK (project, name) mirrors dataplane_sources; project
            -- ''  (displayed '_global') is the unscoped default, same convention as the
            -- source registry above. options_json/limits_json mirror spec_json/limits_json
            -- on dataplane_sources (serialised StreamBinding.options / StreamLimits).
            CREATE TABLE IF NOT EXISTS dataplane_streams (
                project        TEXT NOT NULL DEFAULT '',
                name           TEXT NOT NULL,
                connector      TEXT NOT NULL,
                model          TEXT NOT NULL,
                alias          TEXT NOT NULL DEFAULT 'Production',
                address        TEXT NOT NULL DEFAULT '',
                connection     TEXT,
                options_json   TEXT NOT NULL DEFAULT '{}',
                limits_json    TEXT NOT NULL DEFAULT '{}',
                state          TEXT NOT NULL DEFAULT 'enabled',
                state_reason   TEXT,
                origin         TEXT NOT NULL DEFAULT 'api',
                schema_version INTEGER NOT NULL DEFAULT 1,
                created_by     TEXT,
                created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, name)
            );
            CREATE INDEX IF NOT EXISTS idx_dataplane_streams_model
                ON dataplane_streams(project, model);
            -- Plan 2 (ADR 0131 section 5) task A7b - stream dead letters: messages an
            -- asynchronous stream connector (Kafka) could not process. Metadata always; the
            -- payload only when the binding opts in (options.dlq_store_payload), at most
            -- 256 KiB, UTF-8 only, redacted (secrets + PII) before it is written - sha256 and
            -- size describe the ORIGINAL bytes. error is redacted, at most 1024 chars. The
            -- UNIQUE origin key makes a redelivered dead letter update its row, not add one
            -- (ruling R15); NULL origin columns (a source with no offset) never conflict, since
            -- NULLs are distinct in a UNIQUE constraint on SQLite and Postgres alike.
            -- replay_claim/replay_claimed_at hold one replay at a time. project '' is the
            -- unscoped default, as on dataplane_streams; every read filters on project.
            CREATE TABLE IF NOT EXISTS dataplane_stream_dead_letters (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                project           TEXT NOT NULL DEFAULT '',
                stream            TEXT NOT NULL,
                reason            TEXT NOT NULL,
                error             TEXT NOT NULL DEFAULT '',
                attempts          INTEGER NOT NULL DEFAULT 0,
                sha256            TEXT,
                size              INTEGER NOT NULL DEFAULT 0,
                origin_json       TEXT NOT NULL DEFAULT '{}',
                origin_topic      TEXT,
                origin_partition  INTEGER,
                origin_offset     INTEGER,
                payload           TEXT,
                payload_encoding  TEXT,
                payload_truncated INTEGER NOT NULL DEFAULT 0,
                created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                replayed_at       DATETIME,
                replayed_by       TEXT,
                replay_claim      TEXT,
                replay_claimed_at DATETIME,
                UNIQUE (project, stream, origin_topic, origin_partition, origin_offset)
            );
            CREATE INDEX IF NOT EXISTS idx_dataplane_stream_dead_letters_stream
                ON dataplane_stream_dead_letters(project, stream, id);
            CREATE INDEX IF NOT EXISTS idx_dataplane_stream_dead_letters_created
                ON dataplane_stream_dead_letters(created_at);
            -- Next-Gen 40 · A7 — synthetic dataset gate record (ADR 0042). One row per
            -- generated synthetic revision: the generator config + fidelity/privacy scores
            -- and whether it passed the release gate. The `synthetic=1` flag lives on the
            -- dataset_revisions row (spec R4); this table holds the quality/privacy provenance.
            CREATE TABLE IF NOT EXISTS synthetic_datasets (
                revision_id     TEXT PRIMARY KEY,
                dataset         TEXT NOT NULL,
                source_revision TEXT,
                method          TEXT NOT NULL,
                params_json     TEXT,
                n_rows          INTEGER,
                fidelity_score  REAL,
                privacy_score   REAL,
                released        INTEGER NOT NULL DEFAULT 0,
                reasons_json    TEXT,
                actor           TEXT,
                created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
                node_type   TEXT NOT NULL,             -- dataset | model | prompt | deployment
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
            -- ADR 0111 · judge calibration (MVVP). One row per measurement of a judge;
            -- calibration_id is the provenance handle every eval result carries (G7.3), so a
            -- score can always be resolved back to the judge's kappa and bias AT THAT TIME.
            CREATE TABLE IF NOT EXISTS judge_calibrations (
                calibration_id TEXT PRIMARY KEY,
                judge          TEXT NOT NULL,
                version        TEXT NOT NULL DEFAULT 'v1',
                kappa          REAL NOT NULL DEFAULT 0,
                kappa_lo       REAL,
                kappa_hi       REAL,
                position_bias  REAL NOT NULL DEFAULT 0,
                test_retest    REAL NOT NULL DEFAULT 0,
                benchmarks     TEXT NOT NULL DEFAULT '[]',   -- json list of benchmark names
                families       TEXT NOT NULL DEFAULT '[]',   -- json list: preference|correctness
                replications   INTEGER NOT NULL DEFAULT 0,
                paradox_flag   INTEGER NOT NULL DEFAULT 0,
                sensitivity    REAL NOT NULL DEFAULT 0,
                specificity    REAL NOT NULL DEFAULT 0,
                n              INTEGER NOT NULL DEFAULT 0,
                at             TEXT NOT NULL,
                ts             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_judge_cal_judge ON judge_calibrations (judge, ts DESC);
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
            -- Next-Gen 40 · B3 — semantic-cache hit/miss + measured savings (ADR 0018).
            CREATE TABLE IF NOT EXISTS cache_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant        TEXT NOT NULL DEFAULT 'default',
                model         TEXT NOT NULL,
                hit           INTEGER NOT NULL,
                similarity    REAL,
                tokens_saved  INTEGER NOT NULL DEFAULT 0,
                cost_saved    REAL NOT NULL DEFAULT 0,
                ts            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · B5 — vector store (pgvector-default; SQLite fallback) (ADR 0020).
            CREATE TABLE IF NOT EXISTS vector_collections (
                name       TEXT NOT NULL,
                tenant     TEXT NOT NULL DEFAULT 'default',
                dim        INTEGER NOT NULL,
                metric     TEXT NOT NULL DEFAULT 'cosine',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (name, tenant)
            );
            CREATE TABLE IF NOT EXISTS vector_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                collection    TEXT NOT NULL,
                tenant        TEXT NOT NULL DEFAULT 'default',
                item_id       TEXT NOT NULL,
                vector_json   TEXT NOT NULL,
                metadata_json TEXT,
                ts            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (collection, tenant, item_id)
            );
            CREATE TABLE IF NOT EXISTS vector_metrics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                collection  TEXT NOT NULL,
                tenant      TEXT NOT NULL DEFAULT 'default',
                operation   TEXT NOT NULL,
                latency_ms  REAL NOT NULL DEFAULT 0,
                item_count  INTEGER NOT NULL DEFAULT 0,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · B4 — RAG knowledge-base versioning (ADR 0019). One row per
            -- (kb, tenant); ingest bumps chunk_count + records the A1 source revision.
            CREATE TABLE IF NOT EXISTS rag_kbs (
                kb             TEXT NOT NULL,
                tenant         TEXT NOT NULL DEFAULT 'default',
                source_revision TEXT,
                encoder        TEXT,
                chunk_count    INTEGER NOT NULL DEFAULT 0,
                updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (kb, tenant)
            );
            -- Next-Gen 40 · D8 — guardrail violations (metrics + audit trail) (ADR 0026).
            CREATE TABLE IF NOT EXISTS guardrail_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant     TEXT NOT NULL DEFAULT 'default',
                direction  TEXT NOT NULL,      -- input | output | tool
                action     TEXT NOT NULL,      -- allow | redact | block
                rule       TEXT NOT NULL,
                mode       TEXT NOT NULL DEFAULT 'enforce',
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Unified Project workspace (ADR 0086): generic resource membership.
            -- ``kind`` ∈ model | pipeline | serving_endpoint | connection | dataset | storage.
            CREATE TABLE IF NOT EXISTS project_resources (
                project   TEXT NOT NULL,
                kind      TEXT NOT NULL,
                ref       TEXT NOT NULL,
                added_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                added_by  TEXT,
                PRIMARY KEY (project, kind, ref)
            );
            -- Project Anatomy · P6 — per-project storage location (ADR 0091).
            CREATE TABLE IF NOT EXISTS project_storage (
                project        TEXT PRIMARY KEY,
                bucket         TEXT NOT NULL,
                prefix         TEXT NOT NULL,       -- '<project>/'
                connection_ref TEXT,                -- optional P2 s3 connection name
                quota_gb       REAL,
                used_bytes     INTEGER NOT NULL DEFAULT 0,
                updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Project Anatomy · P7 — the project's two pipeline surfaces (ADR 0092).
            CREATE TABLE IF NOT EXISTS project_pipelines (
                project      TEXT NOT NULL,
                kind         TEXT NOT NULL,         -- 'prefect' | 'rayserve'
                ref          TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'unknown',
                schedule     TEXT,
                last_run_at  DATETIME,
                updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project, kind)
            );
            -- Next-Gen 40 · C4 — AgentOps: per-session agent traces (ADR 0021).
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id  TEXT PRIMARY KEY,
                tenant      TEXT NOT NULL DEFAULT 'default',
                agent       TEXT,               -- logical agent name (e.g. skipper)
                model       TEXT,
                steps       INTEGER NOT NULL DEFAULT 0,
                tool_calls  INTEGER NOT NULL DEFAULT 0,
                errors      INTEGER NOT NULL DEFAULT 0,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd    REAL NOT NULL DEFAULT 0,
                status      TEXT NOT NULL DEFAULT 'ok',  -- ok | anomaly | error
                anomalies   TEXT,               -- JSON list of detected anomaly codes
                started_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at    DATETIME
            );
            -- Next-Gen 40 · C4 — per tool-call analytics (success rate, loops, latency).
            CREATE TABLE IF NOT EXISTS agent_tool_calls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                tenant      TEXT NOT NULL DEFAULT 'default',
                step        INTEGER NOT NULL DEFAULT 0,
                tool        TEXT NOT NULL,
                args_digest TEXT,               -- redacted (D8) hash of args for loop detection
                ok          INTEGER NOT NULL DEFAULT 1,
                error       TEXT,
                latency_ms  REAL,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · C5 — unified advanced drift events (ADR 0022).
            -- drift_kind ∈ feature | prediction | input_embedding | concept | data_quality.
            CREATE TABLE IF NOT EXISTS drift_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                model      TEXT NOT NULL,
                drift_kind TEXT NOT NULL,
                severity   TEXT NOT NULL DEFAULT 'OK',   -- OK | WARN | CRITICAL
                score      REAL,                          -- test statistic (kind-specific)
                metric     TEXT,                          -- realized metric name (concept)
                detail     TEXT,                          -- JSON: profile / test params
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · C5 — label-free performance estimates vs realized (ADR 0022).
            CREATE TABLE IF NOT EXISTS perf_estimates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                model      TEXT NOT NULL,
                metric     TEXT NOT NULL,
                estimated  REAL,                          -- CBPE/DLE pre-label estimate
                realized   REAL,                          -- filled once labels arrive
                baseline   REAL,
                method     TEXT NOT NULL DEFAULT 'cbpe-like',
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · C6 — model-quality SLO specs (OpenSLO-style) (ADR 0023).
            CREATE TABLE IF NOT EXISTS slo_specs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model       TEXT NOT NULL,
                tenant      TEXT NOT NULL DEFAULT 'default',
                name        TEXT NOT NULL,      -- SLO name (e.g. latency-p99, groundedness)
                sli_source  TEXT NOT NULL,      -- c1 | c2 | c5 | availability | prometheus
                sli_query   TEXT,               -- PromQL / SLI expression
                target      REAL NOT NULL,      -- objective ratio (0..1), e.g. 0.99
                window      TEXT NOT NULL DEFAULT '30d',
                higher_is_better INTEGER NOT NULL DEFAULT 1,
                version     INTEGER NOT NULL DEFAULT 1,
                gate_promotion INTEGER NOT NULL DEFAULT 0,
                updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(model, tenant, name)
            );
            -- Next-Gen 40 · C6 — SLI good/total samples feeding budget + burn rate.
            CREATE TABLE IF NOT EXISTS slo_samples (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                model   TEXT NOT NULL,
                tenant  TEXT NOT NULL DEFAULT 'default',
                name    TEXT NOT NULL,
                good    REAL NOT NULL DEFAULT 0,   -- events meeting the SLI this interval
                total   REAL NOT NULL DEFAULT 0,   -- total events this interval
                ts      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · C7 — champion-challenger config (ADR 0024).
            CREATE TABLE IF NOT EXISTS challenger_config (
                model               TEXT PRIMARY KEY,
                tenant              TEXT NOT NULL DEFAULT 'default',
                challenger_version  TEXT NOT NULL,
                mirror_pct          INTEGER NOT NULL DEFAULT 100,
                min_delta           REAL NOT NULL DEFAULT 0.0,
                alpha               REAL NOT NULL DEFAULT 0.05,
                min_samples         INTEGER NOT NULL DEFAULT 100,
                auto_promote        INTEGER NOT NULL DEFAULT 0,
                enabled             INTEGER NOT NULL DEFAULT 1,
                updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by          TEXT
            );
            -- Next-Gen 40 · C7 — per-request champion vs challenger scored samples.
            CREATE TABLE IF NOT EXISTS challenger_samples (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                model           TEXT NOT NULL,
                tenant          TEXT NOT NULL DEFAULT 'default',
                request_hash    TEXT,
                champion_pred   REAL,
                challenger_pred REAL,
                label           REAL,          -- filled as ground truth / C2 judge arrives
                ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · C8 — fairness / subgroup monitoring config (ADR 0025).
            CREATE TABLE IF NOT EXISTS fairness_config (
                model          TEXT PRIMARY KEY,
                tenant         TEXT NOT NULL DEFAULT 'default',
                slice_attrs    TEXT NOT NULL DEFAULT '[]',  -- JSON list of slicing attributes
                threshold      REAL NOT NULL DEFAULT 0.1,   -- max allowed disparity
                min_samples    INTEGER NOT NULL DEFAULT 30, -- noise guard per slice
                gate_promotion INTEGER NOT NULL DEFAULT 0,
                enabled        INTEGER NOT NULL DEFAULT 1,
                updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · C8 — per-request fairness samples (slice attr + pred + label).
            CREATE TABLE IF NOT EXISTS fairness_samples (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model       TEXT NOT NULL,
                tenant      TEXT NOT NULL DEFAULT 'default',
                slice_attr  TEXT NOT NULL,
                slice_value TEXT NOT NULL,
                prediction  REAL,
                label       REAL,
                ts          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · D1 — EU AI Act compliance: risk classification + conformity (ADR 0012).
            CREATE TABLE IF NOT EXISTS compliance_systems (
                model               TEXT PRIMARY KEY,
                tenant              TEXT NOT NULL DEFAULT 'default',
                in_scope            INTEGER NOT NULL DEFAULT 1,
                risk_tier           TEXT,       -- prohibited | high | limited | minimal
                intended_purpose    TEXT,
                deployment_context  TEXT,
                conformity_state    TEXT NOT NULL DEFAULT 'draft',  -- draft|documented|assessed|declared
                updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_by          TEXT
            );
            -- Next-Gen 40 · D1 — versioned generated technical files (Annex IV).
            CREATE TABLE IF NOT EXISTS technical_files (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                model        TEXT NOT NULL,
                tenant       TEXT NOT NULL DEFAULT 'default',
                version      INTEGER NOT NULL DEFAULT 1,
                gaps         INTEGER NOT NULL DEFAULT 0,   -- count of flagged missing sections
                content      TEXT NOT NULL,
                generated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                generated_by TEXT
            );
            -- Next-Gen 40 · D4 — signed checkpoints over the hash-chained audit trail (ADR 0028).
            CREATE TABLE IF NOT EXISTS audit_checkpoints (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                head_id    INTEGER NOT NULL,   -- audit_events.id at the chain head
                head_hash  TEXT NOT NULL,      -- hash of the head event
                signature  TEXT NOT NULL,      -- detached signature over head_hash (D7 key)
                key_id     TEXT,
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- ADR 0108 / AIDC W3 — examlops.identity: agent principals, scoped grants,
            -- JIT single-target leases. AUTONOMOUS vs DELEGATED is a different credential,
            -- never a flag on one; expiry is evaluated at check time (no sweeps).
            CREATE TABLE IF NOT EXISTS agent_principals (
                agent_id          TEXT PRIMARY KEY,
                name              TEXT NOT NULL,
                owner             TEXT NOT NULL,
                purpose           TEXT NOT NULL,
                parent            TEXT,
                state             TEXT NOT NULL DEFAULT 'ACTIVE',
                issuer            TEXT NOT NULL DEFAULT 'local',
                created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decommissioned_at DATETIME
            );
            CREATE TABLE IF NOT EXISTS identity_grants (
                grant_id        TEXT PRIMARY KEY,
                principal       TEXT NOT NULL,
                resource_scope  TEXT NOT NULL,  -- json {"kind","ids"}
                data_scope      TEXT NOT NULL,  -- json {"sensitivity_max","collections"}
                operation_scope TEXT NOT NULL,  -- json ["read","write","export","admin"]
                mode            TEXT NOT NULL,  -- AUTONOMOUS | DELEGATED
                on_behalf_of    TEXT,           -- required for DELEGATED, forbidden for AUTONOMOUS
                issued_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at      DATETIME NOT NULL,
                revoked_at      DATETIME
            );
            CREATE TABLE IF NOT EXISTS identity_leases (
                lease_id      TEXT PRIMARY KEY,
                grant_id      TEXT NOT NULL,
                target        TEXT NOT NULL,
                issued_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at    DATETIME NOT NULL,
                revoked_at    DATETIME,
                revoke_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_identity_leases_grant ON identity_leases (grant_id);
            -- Next-Gen 40 · D4 — append-only enforcement: block UPDATE/DELETE at the DB level (R3).
            CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                BEFORE UPDATE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit_events is append-only (D4)'); END;
            CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                BEFORE DELETE ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit_events is append-only (D4)'); END;
            -- Next-Gen 40 · A6 — Croissant dataset cards + structured model cards (ADR 0037).
            CREATE TABLE IF NOT EXISTS dataset_cards (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset        TEXT NOT NULL,
                revision       TEXT,
                version        INTEGER NOT NULL DEFAULT 1,
                croissant_json TEXT NOT NULL,
                created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS model_card_records (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                model        TEXT NOT NULL,
                tenant       TEXT NOT NULL DEFAULT 'default',
                version      INTEGER NOT NULL DEFAULT 1,
                completeness REAL NOT NULL DEFAULT 0,
                card_json    TEXT NOT NULL,
                created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_by   TEXT
            );
            -- Next-Gen 40 · E3 — fractional GPU allocations (ADR 0030).
            CREATE TABLE IF NOT EXISTS gpu_allocations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                model      TEXT NOT NULL,
                tenant     TEXT NOT NULL DEFAULT 'default',
                mechanism  TEXT NOT NULL,      -- mig | timeslice | whole
                fraction   REAL NOT NULL,
                isolation  TEXT NOT NULL,      -- hardware | soft | exclusive
                gpu_index  INTEGER,
                note       TEXT,
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · E5 — per-model autoscaling scale events (ADR 0031).
            -- (autoscale_config predates this; new policy columns are added via
            --  _COLUMN_MIGRATIONS below to preserve the Phase-24 stub table.)
            CREATE TABLE IF NOT EXISTS scale_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                model         TEXT NOT NULL,
                tenant        TEXT NOT NULL DEFAULT 'default',
                from_replicas INTEGER NOT NULL,
                to_replicas   INTEGER NOT NULL,
                reason        TEXT,
                metric_value  REAL,
                cold_start_s  REAL,
                ts            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · A3 — feature store (single train/serve definition; ADR 0017).
            -- Feast-compatible semantics with a pure-Python fallback (no Feast/Redis required).
            CREATE TABLE IF NOT EXISTS feature_views (
                name             TEXT PRIMARY KEY,
                entity           TEXT NOT NULL,
                features_json    TEXT NOT NULL,      -- ["embedding","pclass",...]
                source           TEXT,               -- offline source hint (parquet/table)
                ttl_seconds      INTEGER NOT NULL DEFAULT 0,
                dataset_revision TEXT,               -- A1 revision this view was applied against
                created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Offline store: append-only event log (point-in-time source of truth).
            CREATE TABLE IF NOT EXISTS feature_records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                view        TEXT NOT NULL,
                entity_id   TEXT NOT NULL,
                event_ts    DATETIME NOT NULL,
                values_json TEXT NOT NULL,
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_feature_records_lookup
                ON feature_records (view, entity_id, event_ts);
            -- Online store: latest materialized snapshot per entity (low-latency read).
            CREATE TABLE IF NOT EXISTS online_features (
                view            TEXT NOT NULL,
                entity_id       TEXT NOT NULL,
                event_ts        DATETIME NOT NULL,
                values_json     TEXT NOT NULL,
                materialized_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (view, entity_id)
            );
            -- A3 materialization runs (freshness monitoring → C5). Distinct from the
            -- Phase-24 `feature_materializations` stub (different schema).
            CREATE TABLE IF NOT EXISTS feature_view_materializations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                view            TEXT NOT NULL,
                start_ts        DATETIME,
                end_ts          DATETIME,
                rows            INTEGER NOT NULL DEFAULT 0,
                materialized_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · A4 — declarative asset-centric pipelines (ADR 0036).
            -- Asset registry + freshness state; the DAG coincides with the A2 lineage graph.
            CREATE TABLE IF NOT EXISTS assets (
                name             TEXT PRIMARY KEY,
                kind             TEXT NOT NULL DEFAULT 'model',  -- dataset | feature | model
                deps_json        TEXT NOT NULL DEFAULT '[]',
                description      TEXT,
                current_version  INTEGER NOT NULL DEFAULT 0,
                built_from_json  TEXT,                           -- {upstream: version_at_build}
                last_materialized_at DATETIME,
                created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS asset_materializations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                version         INTEGER NOT NULL,
                built_from_json TEXT,
                run_id          TEXT,
                actor           TEXT,
                ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · E7 — federated & privacy-preserving training (ADR 0040).
            CREATE TABLE IF NOT EXISTS federated_runs (
                run_id           TEXT PRIMARY KEY,
                strategy         TEXT NOT NULL DEFAULT 'fedavg',
                dp_enabled       INTEGER NOT NULL DEFAULT 0,
                secure_agg       INTEGER NOT NULL DEFAULT 0,
                epsilon          REAL NOT NULL DEFAULT 0,
                delta            REAL NOT NULL DEFAULT 0,
                epsilon_per_round REAL NOT NULL DEFAULT 0,
                rounds_completed INTEGER NOT NULL DEFAULT 0,
                status           TEXT NOT NULL DEFAULT 'initialized',
                created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS federated_sites (
                run_id     TEXT NOT NULL,
                site       TEXT NOT NULL,
                authorized INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (run_id, site)
            );
            CREATE TABLE IF NOT EXISTS federated_rounds (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id             TEXT NOT NULL,
                round_num          INTEGER NOT NULL,
                global_metric      REAL,
                sites_participated INTEGER NOT NULL DEFAULT 0,
                epsilon            REAL,
                created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · E8 — heterogeneous hardware & hybrid HPC↔cloud (ADR 0041).
            CREATE TABLE IF NOT EXISTS device_pools (
                name              TEXT PRIMARY KEY,
                target            TEXT NOT NULL DEFAULT 'hpc',      -- hpc|cloud
                accelerator       TEXT NOT NULL DEFAULT 'nvidia',   -- nvidia|amd|intel-gaudi|tpu|cpu
                capabilities      TEXT,                             -- JSON list of capability tags
                count             INTEGER NOT NULL DEFAULT 0,
                region            TEXT,
                cost_per_hour     REAL NOT NULL DEFAULT 0,
                carbon_factor     REAL NOT NULL DEFAULT 0,          -- gCO2e per device-hour
                supports_fractions INTEGER NOT NULL DEFAULT 0,
                status            TEXT NOT NULL DEFAULT 'active',
                created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS placement_decisions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                workload              TEXT NOT NULL,
                accelerator_requested TEXT,
                device_chosen         TEXT,
                pool                  TEXT,
                target                TEXT,
                region                TEXT,
                decision              TEXT NOT NULL,   -- placed|fallback|rejected
                fraction_honored      INTEGER NOT NULL DEFAULT 1,
                reason                TEXT,
                created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS burst_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                workload      TEXT NOT NULL,
                from_pool     TEXT,
                to_pool       TEXT,
                residency     TEXT,
                allowed       INTEGER NOT NULL DEFAULT 0,
                reason        TEXT,
                created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · E4 — inference gateway & KV-cache-aware routing (ADR 0039).
            CREATE TABLE IF NOT EXISTS inference_gateway_config (
                model         TEXT NOT NULL,
                tenant        TEXT NOT NULL DEFAULT 'default',
                mode          TEXT NOT NULL DEFAULT 'round_robin',  -- round_robin|cache_aware
                slo_latency_ms REAL,
                disaggregate  INTEGER NOT NULL DEFAULT 0,
                prefill_pool  TEXT,
                decode_pool   TEXT,
                updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (model, tenant)
            );
            CREATE TABLE IF NOT EXISTS routing_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                model      TEXT NOT NULL,
                tenant     TEXT NOT NULL DEFAULT 'default',
                prefix_key TEXT,
                replica    TEXT,
                decision   TEXT NOT NULL,   -- affinity|load_aware|round_robin
                hit        INTEGER NOT NULL DEFAULT 0,
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · B8 — structured output & reasoning ops (ADR 0035).
            CREATE TABLE IF NOT EXISTS reasoning_usage (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                model           TEXT NOT NULL,
                tenant          TEXT NOT NULL DEFAULT 'default',
                request_id      TEXT,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens   INTEGER NOT NULL DEFAULT 0,
                reasoning_cost  REAL NOT NULL DEFAULT 0,
                output_cost     REAL NOT NULL DEFAULT 0,
                ts              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS reasoning_traces (
                request_id     TEXT PRIMARY KEY,
                tenant         TEXT NOT NULL DEFAULT 'default',
                redacted_trace TEXT NOT NULL,
                expires_at     REAL,
                ts             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS structured_output_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                model      TEXT,
                tenant     TEXT NOT NULL DEFAULT 'default',
                outcome    TEXT NOT NULL,   -- valid | repaired | failed
                ts         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · B6 — embedding lifecycle & reindexing (ADR 0043).
            CREATE TABLE IF NOT EXISTS encoders (
                encoder_id    TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                version       TEXT NOT NULL,
                dim           INTEGER NOT NULL,
                metric        TEXT NOT NULL DEFAULT 'cosine',
                normalization TEXT NOT NULL DEFAULT 'l2',
                created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- B6 encoder-lifecycle state per collection (distinct from the B5
            -- `vector_collections` store table, which has a different schema).
            CREATE TABLE IF NOT EXISTS embedding_collections (
                collection         TEXT NOT NULL,
                tenant             TEXT NOT NULL DEFAULT 'default',
                active_encoder_id  TEXT,
                staging_encoder_id TEXT,
                status             TEXT NOT NULL DEFAULT 'active',  -- active|building|switching
                updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (collection, tenant)
            );
            CREATE TABLE IF NOT EXISTS reindex_jobs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                collection    TEXT NOT NULL,
                tenant        TEXT NOT NULL DEFAULT 'default',
                from_encoder  TEXT,
                to_encoder    TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'building',  -- building|verified|switched|aborted
                recall        REAL,
                docs_reindexed INTEGER NOT NULL DEFAULT 0,
                created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            -- Next-Gen 40 · E6 — distributed & fault-tolerant training (ADR 0032).
            CREATE TABLE IF NOT EXISTS distributed_runs (
                run_id           TEXT PRIMARY KEY,
                model            TEXT NOT NULL,
                nodes            INTEGER NOT NULL DEFAULT 1,
                gpus_per_node    INTEGER NOT NULL DEFAULT 1,
                strategy         TEXT NOT NULL DEFAULT 'fsdp',   -- fsdp | zero | megatron
                status           TEXT NOT NULL DEFAULT 'running', -- running|failed|resumed|complete
                dataset_revision TEXT,
                checkpoint_every TEXT,
                cost_gpu_hours   REAL,
                resumes          INTEGER NOT NULL DEFAULT 0,
                created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS training_checkpoints (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id         TEXT NOT NULL,
                step           INTEGER NOT NULL,
                epoch          INTEGER NOT NULL,
                shard_count    INTEGER NOT NULL DEFAULT 1,
                uri            TEXT,
                state_json     TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                mlflow_run_id  TEXT,
                created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_training_checkpoints_run
                ON training_checkpoints (run_id, step);
            -- Next-Gen 40 · D5 — signed, versioned policy bundles (ADR 0029).
            CREATE TABLE IF NOT EXISTS policy_bundles (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant       TEXT NOT NULL DEFAULT 'default',
                version      INTEGER NOT NULL DEFAULT 1,
                content_hash TEXT NOT NULL,
                content      TEXT NOT NULL,
                signature    TEXT,
                algo         TEXT,
                signed_by    TEXT,
                created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_policy_bundles_tenant ON policy_bundles (tenant, version);
            -- Next-Gen 40 · B7 — PEFT/LoRA adapter registry (ADR 0044).
            CREATE TABLE IF NOT EXISTS lora_adapters (
                adapter_id       TEXT PRIMARY KEY,
                base_ref         TEXT NOT NULL,
                method           TEXT NOT NULL DEFAULT 'lora',   -- lora | qlora | full
                rank             INTEGER,
                target_modules   TEXT,
                dataset_revision TEXT,
                eval_score       REAL,
                eval_floor       REAL,
                promoted         INTEGER NOT NULL DEFAULT 0,
                signature        TEXT,
                signed_by        TEXT,
                cost_gpu_hours   REAL,
                created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_lora_adapters_base ON lora_adapters (base_ref);
            -- Next-Gen 40 · A8 — signed reproducibility bundles (ADR 0038).
            CREATE TABLE IF NOT EXISTS repro_bundles (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                model         TEXT NOT NULL,
                version       TEXT NOT NULL,
                bundle_version INTEGER NOT NULL DEFAULT 1,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                signature     TEXT,             -- NULL when no signing key (degraded)
                algo          TEXT,
                signed_by     TEXT,
                created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_repro_bundles_mv
                ON repro_bundles (model, version, bundle_version);

            -- ADR 0127 / plan P4.2 — the serving snapshot: everything a serving replica needs to
            -- know (alias -> version -> artifact, traffic splits, shadow targets), compiled once
            -- and versioned by a monotonic generation. Replicas read the newest row instead of
            -- each polling MLflow and the config tables.
            CREATE TABLE IF NOT EXISTS serving_snapshots (
                generation INTEGER PRIMARY KEY AUTOINCREMENT,
                digest     TEXT NOT NULL,
                body       TEXT NOT NULL,             -- JSON
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            -- ADR 0123 decision 3 — per-tenant request quotas the serving gateway enforces. The
            -- control plane compiles them into the serving snapshot; the gateway never reads this
            -- table on the request path (rpm = 0 means "unlimited for this tenant").
            CREATE TABLE IF NOT EXISTS serving_quotas (
                tenant     TEXT PRIMARY KEY,
                rpm        INTEGER NOT NULL CHECK (rpm >= 0),
                updated_by TEXT,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        if not path.startswith("pg:"):
            # The column migrations check PRAGMA table_info and then ALTER TABLE: two statements.
            # In autocommit, two processes opening a new database both saw a column missing and the
            # second ALTER failed ("duplicate column name"). Holding SQLite's write lock makes the
            # check and the change one step; Postgres is already serialised by the lock above.
            conn.execute(begin_immediate("schema"))
        _migrate_columns(conn)
        # Which outbox topics changed since a watermark (the serving-snapshot projector's trigger).
        # Outside the script and behind a column check: a failed statement aborts a Postgres
        # bootstrap transaction, and an outbox created by something else may lack `topic`.
        if "topic" in {r[1] for r in conn.execute("PRAGMA table_info(event_outbox)").fetchall()}:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_event_outbox_topic ON event_outbox (topic, id)")
        # ADR 0128: stamp the data format, refuse data a newer release made unreadable to this
        # one (IncompatibleDataError propagates — and the path stays uncached, so every later
        # call refuses too), and apply pending online migrations.
        from examlops.lifecycle.dataformat import on_schema_ready

        on_schema_ready(conn)
    if cacheable:
        _INITIALIZED_PATHS.add(path)


# Idempotent additive column migrations for tables that predate a feature.
# ``ALTER TABLE ADD COLUMN`` errors if the column already exists, so we gate on
# PRAGMA table_info. Keep entries here forever — they are cheap and self-skipping.
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    # ADR 0023 clause 3 `c1`: a gateway call's measured latency and whether it failed, so a
    # latency or error SLI can be derived. NULL on older rows reads as *unmeasured*, never as 0.
    "gateway_calls": {
        "latency_ms": "REAL",
        "error": "INTEGER",
    },
    # ADR 0023 clause 3: the high-water mark of the events an `exa slo ingest` counted
    # (`<table>:<id>`), so the next ingest counts only newer events. Without it every ingest
    # re-recorded its whole window and the summed SLI counted the same events again and again.
    "slo_samples": {
        "watermark": "TEXT",
    },
    # A6 reindex orchestration (ADR 0043 clause 4): where the job ran and how long it took.
    # `cost_usd` is deliberately absent — a monetary figure needs device-hours this path does not
    # know, and an invented one is worse than none (the rule the C1 carbon facet already follows).
    "reindex_jobs": {
        "orchestrator": "TEXT",
        "hpc_job_id": "TEXT",
        "duration_s": "REAL",
    },
    # C7 judge scoring (ADR 0024 clause 2). Deliberately NOT written into `label`: a judge's
    # opinion is not ground truth, and a judged sample that is indistinguishable from a measured
    # one makes the whole scoreboard a mixture nobody can separate afterwards.
    "challenger_samples": {
        "champion_judge": "REAL",
        "challenger_judge": "REAL",
        "judge_model": "TEXT",
    },
    # D1: which document a row holds. NULL reads as the Annex-IV technical file, which is what
    # every row written before the Declaration of Conformity existed is.
    "technical_files": {
        "kind": "TEXT",
    },
    # C3 eval gate: the gate's own metric direction. NULL means "not declared", which is not the
    # same as False — an undeclared gate falls back to whatever the caller passes, so every gate
    # configured before this column existed behaves exactly as it did.
    "eval_gates": {
        "higher_is_better": "INTEGER",
        # ADR 0008 clause 5 — the aggregate policy ("all" | "majority"). NULL reads as "all",
        # so every gate configured before this column existed keeps blocking on any failure.
        "aggregate": "TEXT",
    },
    # A6 embedding lifecycle (ADR 0043 clause 1): which encoder produced this collection's
    # vectors. NULL means "unstamped", which is not the same as "compatible" — a collection
    # written before this column existed cannot be checked, and the guard says so rather than
    # assuming. Stamping is what gives `guard_compatible` something to guard.
    "vector_collections": {
        "encoder_id": "TEXT",
        # ADR 0020 clause 2: the collection's ANN index (flat | hnsw | ivfflat) and its
        # parameters as JSON. NULL reads as flat — which is what every collection created before
        # these columns existed actually is on this store (an exact scan).
        "index_type": "TEXT",
        "index_params": "TEXT",
    },
    # ADR 0020 clause 2: the text the sparse (BM25) channel of hybrid search indexes. NULL falls
    # back to a string metadata["text"], where B4 RAG has always kept its chunk text.
    "vector_items": {
        "text": "TEXT",
    },
    # ADR 0020 clause 4 (A3 ingestion): which declared feature of a view holds an embedding. When
    # set, materialization also indexes it into the `features.<view>` vector collection. NULL = the
    # view has no embedding, and materialization behaves exactly as before.
    "feature_views": {
        "embedding_feature": "TEXT",
    },
    # D4 immutable audit trail (ADR 0028): hash-chain columns on the existing audit log.
    "audit_events": {
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
        "prev_hash": "TEXT",
        "hash": "TEXT",
        # ADR 0110 decision 3: the causal edges. Without them the chain records events but not
        # causation, so "who did this, on whose behalf, and how would it be undone" cannot be
        # answered from the chain alone — which is the W2 gate.
        "correlation_id": "TEXT",
        "parent_correlation_id": "TEXT",
        "mode": "TEXT",
        "on_behalf_of": "TEXT",
        "rollback_ref": "TEXT",
    },
    # E5 autoscaling (ADR 0031): richer policy columns on the Phase-24 autoscale_config stub.
    "autoscale_config": {
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
        "target_metric": "TEXT NOT NULL DEFAULT 'queue_depth'",
        "target_value": "REAL NOT NULL DEFAULT 10",
        "scale_to_zero_after_s": "INTEGER NOT NULL DEFAULT 0",
        "warm_pool": "INTEGER NOT NULL DEFAULT 0",
        "stabilization_s": "INTEGER NOT NULL DEFAULT 30",
        "cooldown_s": "INTEGER NOT NULL DEFAULT 60",
        "gpu_fraction": "REAL NOT NULL DEFAULT 1.0",
        "enabled": "INTEGER NOT NULL DEFAULT 1",
    },
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
    # ADR 0112 decision 6 adds what *kind* of signal produced it — a stored intensity without
    # its method is exactly what lets an average figure be read as if it could justify a
    # scheduling decision.
    "carbon_records": {
        "provider": "TEXT",
        "signal_type": "TEXT",
        "signal_method": "TEXT",
    },
    # ADR 0089: the alert state of a project's budget, so a breach is announced on the transition
    # rather than once per look (and a recovery is announced at all).
    "project_budgets": {
        "alert_state": "TEXT",
        "alert_breaches_json": "TEXT",
        "alerted_at": "DATETIME",
    },
    # ADR 0035 clause 1: did the schema constrain the decoder, or only judge the answer?
    "structured_output_events": {"constrained": "INTEGER NOT NULL DEFAULT 0"},
    # A5 data contracts (ADR 0005): revision/stage/score facets on the quality table.
    "data_quality_checks": {
        "revision": "TEXT",
        "stage": "TEXT NOT NULL DEFAULT 'train'",
        "score": "REAL",
        # Which contract engine judged — pandera or python (ADR 0005 clause 1). NULL on rows
        # written before it was recorded.
        "engine": "TEXT",
    },
    # Unified Project workspace (ADR 0086): per-project cost attribution anchor.
    "model_costs": {
        "project": "TEXT",
        # The scheduler reports (gpu_hours, cpu_hours) per job and the cost provider prices both;
        # only the GPU half was ever stored, so a CPU-only site kept no record of the work it did.
        # NULL = recorded before this column existed, and is not the same as 0.
        "cpu_hours": "REAL",
    },
    # ADR 0111 no uncalibrated judge may gate: evaluator provenance (G7.3) + the uncertainty
    # interval every score must carry (G7.4). NULL = recorded before the ADR landed.
    "eval_suite_results": {
        "calibration_id": "TEXT",
        "score_lo": "REAL",
        "score_hi": "REAL",
    },
    # D7/2.3 envelope encryption: which KEK (key_id) each secret is wrapped under, so keys can be
    # rotated online and old ciphertext rewrapped. NULL = the legacy single-key era.
    "secrets_store": {
        "key_id": "TEXT",
    },
    # A7 synthetic data (ADR 0042): flag a dataset revision as synthetic so it can never pass as
    # real (spec R4), and anchor its provenance to the real source revision + generator method.
    "dataset_revisions": {
        "synthetic": "INTEGER NOT NULL DEFAULT 0",
        "source_revision": "TEXT",
        "generator": "TEXT",
    },
    # P1/P3 durable command events carry their trusted actor and tenant through the outbox. Keep
    # existing installations additive while matching the canonical control-plane declaration.
    "event_outbox": {
        "actor": "TEXT NOT NULL DEFAULT 'system'",
        "tenant": "TEXT NOT NULL DEFAULT 'default'",
        # W3C trace context of the transaction that wrote the event (plan P2.5).
        "traceparent": "TEXT",
    },
    # Track V / ADR 0107: a *running* vLLM endpoint, not just its declared shape. The
    # pre-existing columns describe what to serve; these describe where it is, who started
    # it and on what substrate, so the table can back `exa serve llm` and the F10 console.
    "llm_endpoints": {
        "base_url": "TEXT",
        "state": "TEXT NOT NULL DEFAULT 'PENDING'",  # PENDING|STARTING|READY|FAILED|STOPPED
        "launcher": "TEXT NOT NULL DEFAULT 'external'",
        "job_id": "TEXT",  # scheduler job id when launcher is slurm/flux
        "cluster": "TEXT",
        "project": "TEXT",
        "modality": "TEXT NOT NULL DEFAULT 'text'",
        "served_model_name": "TEXT",
        "engine_config": "TEXT",  # JSON snapshot of the resolved engine block
        "gpus": "INTEGER",
        "nodes": "INTEGER",
        "last_health": "TEXT",
        "created_at": "TEXT",
    },
    # Track V: distinguish a long-running *serving* job from a training job. The poller
    # (hpc_poll.poll_until_complete) assumes termination, so a serve job must never enter it.
    "hpc_jobs": {
        "kind": "TEXT NOT NULL DEFAULT 'train'",  # train | serve
        "endpoint_url": "TEXT",
    },
    # ADR 0131 ruling R14: why a pack-removal sweep disabled a stream, so a re-added entry can
    # tell the sweep's own disable from a human's. Added to the DDL after the table already
    # existed on sites running a pre-release build of the stream ingress, where every state
    # change (pause, resume, disable, the sweep itself) then failed with "no such column".
    # NULL = a row whose state was never explained, which is what every earlier row is.
    "dataplane_streams": {
        "state_reason": "TEXT",
    },
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _COLUMN_MIGRATIONS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _audit_canonical(
    source: str,
    actor: str | None,
    action: str,
    target: str | None,
    details_json: str | None,
    tenant: str,
    ts: str,
    correlation: dict[str, Any] | None = None,
) -> str:
    """Deterministic serialization of an audit event for the D4 hash chain (R1).

    ``correlation`` (ADR 0110) is folded in **only when it carries something**, so an event
    written outside any correlation context canonicalises byte-identically to what this function
    produced before those fields existed — which is what lets every historical row keep
    verifying. It is inside the hash rather than beside it because a causal edge an attacker
    could rewrite without breaking the chain would be evidence of nothing.
    """
    payload: dict[str, Any] = {
        "source": source,
        "actor": actor,
        "action": action,
        "target": target,
        "details": details_json,
        "tenant": tenant,
        "ts": ts,
    }
    if correlation and any(correlation.values()):
        payload["correlation"] = {k: v for k, v in sorted(correlation.items()) if v}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _audit_hash(prev_hash: str, canonical: str) -> str:
    import hashlib

    return hashlib.sha256(f"{prev_hash}‖{canonical}".encode()).hexdigest()






# Serving traffic/promotion helpers now LIVE in examlops.data.serving (item 4.5 body
# relocation); re-exported for back-compat (data.serving imports get_db/install_write_retry).
from examlops.data.serving import (count_scale_events, delete_llm_endpoint, disable_challenger, get_autoscale_config, get_challenger_config, get_challenger_samples, set_challenger_judge_scores, get_device_pools, get_llm_endpoint, get_promotion_rule, get_shadow_config, get_traffic_rules, serving_model_key, set_shadow_config, list_autoscale_configs, list_challenger_configs, list_llm_endpoints, list_scale_events, record_challenger_sample, record_scale_event, set_autoscale_config, set_challenger_config, set_llm_endpoint_state, set_promotion_rule, set_traffic_rules, upsert_llm_endpoint)  # noqa: E402, E501, F401, I001
























# High-volume, append-only, per-inference telemetry tables that grow unbounded and are safe to
# TTL-prune. Deliberately EXCLUDES audit_events (tamper-evident hash chain — deleting rows breaks
# verify_audit_chain) and model_costs / carbon_records (FinOps history must be retained). Item QW9.
_PRUNABLE_TELEMETRY: tuple[str, ...] = ("drift_snapshots", "input_snapshots")








# Coordination primitives (item 1.2) now LIVE in examlops.data.coordination (item 4.5 body
# relocation); re-exported here for backward compatibility. data.coordination imports the shared
# get_db/_immediate_write/write_retry defined above, so there is no import cycle.
# noqa: E402, I001 — mid-module re-export must stay here (data.coordination needs get_db/etc.
# defined above → no import cycle).
from examlops.data.coordination import coord_check_and_set_idempotent, coord_rate_allow, coord_try_lock, coord_unlock  # noqa: E402, E501, F401, I001
# admission helpers now LIVE in examlops.data.admission (item 4.5 body relocation); re-exported
# for back-compat (data.admission imports get_db/etc. defined above → no cycle).
from examlops.data.admission import admission_stats, claim_next_admission, complete_admission, enqueue_admission  # noqa: E402, E501, F401, I001
# events helpers now LIVE in examlops.data.events (item 4.5 body relocation); re-exported
# for back-compat (data.events imports get_db/etc. defined above → no cycle).
from examlops.data.events import (claim_outbox_batch, create_federated_run, enqueue_event, get_federated_run, get_federated_sites, get_reasoning_trace, lineage_graph, lineage_impact, list_burst_events, list_federated_rounds, mark_event_failed, mark_event_published, outbox_oldest_pending_age, outbox_stats, reasoning_usage_summary, record_burst_event, record_cache_event, record_federated_round, record_lineage_event, record_reasoning_usage, record_routing_event, record_structured_output_event, register_federated_site, routing_stats, store_reasoning_trace, structured_output_stats)  # noqa: E402, E501, F401, I001






























# ======================================================================
# Next-generation feature helpers (Phase 0). Each mirrors the set_/get_/
# write_ conventions above so callers stay uniform across features.
# ======================================================================


# ---- #3 Elastic autoscaling --------------------------------------------------
# E5 (ADR 0031): set_autoscale_config / get_autoscale_config are defined near the end of
# this module with the full policy schema (min/max/target/scale-to-zero/warm-pool/etc.).


# ---- #9 Ground-truth feedback loop -------------------------------------------










# ---- #18 Fairness gates ------------------------------------------------------




# ---- #20 FinOps budgets ------------------------------------------------------












def _actor() -> str:
    return os.getenv("EXAMLOPS_ACTOR") or os.getenv("USER") or "unknown"


# ── ExaMLOps Projects (RHOAI-style resource envelopes for Docker) ─────────────


















# ── Unified Project workspace: generic resource membership (ADR 0086) ─────────


_RESOURCE_KINDS = {"model", "pipeline", "serving_endpoint", "connection", "dataset", "storage"}










# ── People membership + permissions via D6 authz (ADR 0086, no new ACL table) ─










# ── Autopilot (ADR 0085): self-driving closed-loop detect→retrain→promote ────












# --- Next-Gen 40 · A1 — dataset revisions (ADR 0003) --------------------------
# ``rev`` is any object exposing the DatasetRevision fields (backend, dataset,
# revision_id, kind, uri, schema_hash). Kept duck-typed so this platform-layer
# module never imports the pipelines package (avoids a layering cycle).








# --- Next-Gen 40 · A5 — data contracts / quality gates (ADR 0005) ------------






# --- Next-Gen 40 · B1 — prompt registry (ADR 0009) ---------------------------
















# --- Next-Gen 40 · D7 — local encrypted secrets store (ADR 0011) --------------
# Stores/returns opaque ciphertext only; encryption/decryption lives in the
# examlops.secrets client so this DB layer never sees a plaintext secret.












# --- Next-Gen 40 · D6 — authz relations (ADR 0014) ---------------------------












# --- Next-Gen 40 · D3 — model signing + AI-BOM (ADR 0013) --------------------










# ── A2 — OpenLineage dual-write + graph/impact queries (ADR 0004) ─────────────








# ── C2 — continuous-eval suite results (ADR 0007) ─────────────────────────────






# ── C3 — eval regression gate config + reports (ADR 0008) ─────────────────────










# ── B2 — model-gateway virtual keys + per-call cost (ADR 0010) ────────────────
















# ── B3 — semantic-cache savings (ADR 0018) ────────────────────────────────────






# ---------------------------------------------------------------------------
# Next-Gen 40 · C4 — AgentOps: agent trace & tool-call analytics (ADR 0021).
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Next-Gen 40 · C5 — advanced drift: concept / label-free perf / data quality (ADR 0022).
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Next-Gen 40 · C6 — model-quality SLOs / SLIs & burn-rate (ADR 0023).
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Next-Gen 40 · C7 — champion-challenger / shadow scoreboard (ADR 0024).
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Next-Gen 40 · C8 — fairness & subgroup performance monitoring (ADR 0025).
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Next-Gen 40 · D1 — EU AI Act compliance (ADR 0012).
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Next-Gen 40 · D4 — immutable, tamper-evident audit trail (ADR 0028).
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Next-Gen 40 · A6 — Croissant dataset cards + structured model cards (ADR 0037).
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Next-Gen 40 · E5 — autoscaling & scale-to-zero (ADR 0031).
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# Next-Gen 40 · A3 — feature store (single train/serve definition; ADR 0017).
# ---------------------------------------------------------------------------
















# ---------------------------------------------------------------------------
# Next-Gen 40 · A4 — declarative asset-centric pipelines (ADR 0036).
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Next-Gen 40 · A8 — signed reproducibility bundles (ADR 0038).
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Next-Gen 40 · B7 — PEFT/LoRA adapter registry (ADR 0044).
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Next-Gen 40 · D5 — signed, versioned policy bundles (ADR 0029).
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Next-Gen 40 · E6 — distributed & fault-tolerant training (ADR 0032).
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Next-Gen 40 · B6 — embedding lifecycle & reindexing (ADR 0043).
# ---------------------------------------------------------------------------








_UNSET = object()  # sentinel: distinguish "don't change" from "set to NULL"










# ---------------------------------------------------------------------------
# Next-Gen 40 · B8 — structured output & reasoning ops (ADR 0035).
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Next-Gen 40 · E4 — inference gateway & KV-cache-aware routing (ADR 0039).
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Next-Gen 40 · E7 — federated & privacy-preserving training (ADR 0040).
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Next-Gen 40 · E8 — heterogeneous hardware & hybrid HPC↔cloud (ADR 0041).
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Project Anatomy · P6 — per-project storage location (ADR 0091).
# ---------------------------------------------------------------------------














# ---------------------------------------------------------------------------
# Project Anatomy · P7 — the project's two pipeline surfaces (ADR 0092).
# ---------------------------------------------------------------------------
_PIPELINE_KINDS = ("prefect", "rayserve")






# Install central write-retry coverage once the whole module (all helpers) is defined.


# ── Per-domain body relocation (item 4.5): helpers below LIVE in examlops.data.*; re-exported
# for back-compat (at END so every primitive/constant + install_write_retry is defined first).
from examlops.data.agent import (get_agent_session_trace, list_agent_sessions, record_agent_session, record_agent_tool_call, tool_success_rate)  # noqa: E402, E501, F401, I001
from examlops.data.audit import (audit_chain_head, autonomous_actions, correlation_chain, export_audit_events, list_audit_checkpoints, list_training_checkpoints, sign_audit_checkpoint, verify_audit_chain, write_audit_event, write_training_checkpoint, audit_stream_enabled, verify_audit_stream)  # noqa: E402, E501, F401, I001
from examlops.data.autopilot import (claim_autopilot_lease, create_autopilot_run, get_autopilot_config, list_autopilot_runs, release_autopilot_lease, set_autopilot_config, update_autopilot_run)  # noqa: E402, E501, F401, I001
from examlops.data.data_assets import (latest_vector_metrics, bump_asset_version, create_distributed_run, create_reindex_job, get_adapter, get_asset, get_collection, get_data_quality_checks, get_dataset_revision, get_dataset_revisions, get_distributed_run, get_encoder, get_feature_view, get_offline_features_asof, get_online_feature, get_repro_bundle, get_synthetic_dataset, is_synthetic_only, last_materialization, link_dataset_revision_run, list_adapters, list_assets, list_distributed_runs, list_encoders, list_feature_views, list_reindex_jobs, list_repro_bundles, list_synthetic_datasets, materialize_online, purge_telemetry, record_data_quality_check, record_dataset_revision, record_synthetic_dataset, register_adapter, register_asset, register_encoder_row, set_adapter_promoted, store_repro_bundle, synthetic_proportion, update_distributed_run, update_reindex_job, upsert_collection, upsert_feature_view, write_feature_record)  # noqa: E402, E501, F401, I001
from examlops.data.drift import (claim_drift_trigger, get_corruption_baseline, get_drift_auto_retrain, get_drift_baseline, get_input_baseline, latest_drift_event, list_drift_auto_retrain, list_drift_events, record_drift_event, record_drift_trigger, set_corruption_baseline, set_drift_auto_retrain, set_drift_baseline, set_input_baseline, write_drift_snapshot, write_input_snapshot, drift_models, recent_drift_predictions, record_drift_statuses)  # noqa: E402, E501, F401, I001
from examlops.data.evaluation import (get_calibration_by_id, get_eval_gate, get_eval_results, get_gate_reports, get_judge_calibration, list_judge_calibrations, list_perf_estimates, record_eval_result, record_gate_report, record_judge_calibration, record_perf_estimate, set_eval_gate)  # noqa: E402, E501, F401, I001
from examlops.data.finops import (add_key_spend, aggregate_model_costs, get_carbon_records, get_fairness_gates, get_live_metrics, get_model_costs, join_predictions_with_truth, record_model_cost, set_fairness_gate, total_gateway_cost, write_carbon_record, write_ground_truth, write_live_metric, write_prediction)  # noqa: E402, E501, F401, I001
from examlops.data.gateway import (cache_stats, create_virtual_key, get_gateway_config, get_virtual_key, list_virtual_keys, record_gateway_call, set_gateway_config)  # noqa: E402, E501, F401, I001
from examlops.data.governance import (ANNEX_IV, DECLARATION, get_compliance_system, get_fairness_config, get_fairness_samples, get_policy_bundle, get_relations_for, get_slo_spec, grant_relation, list_compliance_systems, list_objects_for, list_policy_bundles, list_relations, list_slo_specs, list_technical_files, record_fairness_sample, record_slo_sample, revoke_relation, revoke_virtual_key, save_technical_file, set_compliance_system, set_fairness_config, slo_sli_ratio, store_policy_bundle, upsert_slo_spec)  # noqa: E402, E501, F401, I001
from examlops.data.hpc import (aggregate_node_capacity, get_cluster, get_clusters, get_hpc_jobs, get_node_snapshot, list_placement_decisions, record_hpc_job, record_node_snapshot, record_placement_decision, set_cluster_state, update_hpc_job, upsert_cluster)  # noqa: E402, E501, F401, I001
from examlops.data.projects import (add_project_member, archive_project, assign_model_to_project, assign_resource_to_project, bind_project_connection, create_project, delete_project, ensure_project_storage, get_project, get_project_budget, get_project_consumption, get_project_for_model, get_project_full, get_project_pipelines, get_project_storage, list_project_budgets, list_project_members, list_project_models, list_project_resources, list_projects, project_experiment, projects_bucket, refresh_project_usage, remove_project_member, remove_project_resource, set_project_budget, set_project_usage, update_project_quota, upsert_project_pipeline)  # noqa: E402, E501, F401, I001
from examlops.data.prompts import (create_prompt_version, get_prompt_by_label, get_prompt_version, list_prompt_labels, list_prompt_names, list_prompt_versions, set_prompt_label)  # noqa: E402, E501, F401, I001
from examlops.data.registry import (get_dataset_card, get_model_bom, get_model_card, get_model_signature, register_device_pool, save_dataset_card, save_model_card, store_model_bom, store_model_signature)  # noqa: E402, E501, F401, I001
from examlops.data.secrets import (all_secret_records, get_secret_ciphertext, get_secret_record, list_secret_paths, put_secret_ciphertext)  # noqa: E402, E501, F401, I001

_install_write_retry()  # wrap platform_db's own remaining mutating helpers (if any)
