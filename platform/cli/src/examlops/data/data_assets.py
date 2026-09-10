"""examlops.data.data_assets — Datasets/assets/features/distributed/repro.

Owns these helpers (bodies physically live here) — per-domain split (item 4.5), implementation relocated.
Shared primitives are imported from ``platform_db``; cross-domain calls route via ``_pdb`` (resolved at
call time → no import cycle). ``install_write_retry(__name__)`` re-applies the item-0.4 auto-wrapping.
``platform_db`` re-exports these names for backward compatibility.
"""

from __future__ import annotations

import json
import os
from typing import Any  # noqa: F401

from examlops.data._rowid import last_insert_id
from examlops.platform_db import (  # noqa: F401
    _PRUNABLE_TELEMETRY,
    _UNSET,
    _db_path,
    get_db,
    init_db,
    install_write_retry,
)
from examlops.resilience import db as _rdb

__all__ = [
    "bump_asset_version",
    "create_distributed_run",
    "create_reindex_job",
    "get_adapter",
    "get_asset",
    "get_collection",
    "latest_vector_metrics",
    "get_data_quality_checks",
    "get_dataset_revision",
    "get_dataset_revisions",
    "get_distributed_run",
    "get_encoder",
    "get_feature_view",
    "get_offline_features_asof",
    "get_online_feature",
    "get_repro_bundle",
    "get_synthetic_dataset",
    "is_synthetic_only",
    "last_materialization",
    "list_adapters",
    "list_assets",
    "list_distributed_runs",
    "list_encoders",
    "list_feature_views",
    "list_reindex_jobs",
    "list_repro_bundles",
    "list_synthetic_datasets",
    "materialize_online",
    "purge_telemetry",
    "record_data_quality_check",
    "record_dataset_revision",
    "record_synthetic_dataset",
    "register_adapter",
    "register_asset",
    "register_encoder_row",
    "set_adapter_promoted",
    "store_repro_bundle",
    "synthetic_proportion",
    "update_distributed_run",
    "update_reindex_job",
    "upsert_collection",
    "upsert_feature_view",
    "write_feature_record",
]


def bump_asset_version(
    name: str,
    built_from: dict[str, int],
    *,
    run_id: str | None = None,
    actor: str | None = None,
) -> int:
    """Record a new materialized version of an asset + the upstream versions it built from (R3)."""
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT current_version FROM assets WHERE name=?", (name,)).fetchone()
        new_version = (row["current_version"] if row else 0) + 1
        conn.execute(
            """UPDATE assets SET current_version=?, built_from_json=?,
                   last_materialized_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
               WHERE name=?""",
            (new_version, json.dumps(built_from), name),
        )
        conn.execute(
            """INSERT INTO asset_materializations (name, version, built_from_json, run_id, actor)
               VALUES (?,?,?,?,?)""",
            (name, new_version, json.dumps(built_from), run_id, actor),
        )
    return new_version


def create_distributed_run(
    run_id: str,
    model: str,
    *,
    nodes: int = 1,
    gpus_per_node: int = 1,
    strategy: str = "fsdp",
    dataset_revision: str | None = None,
    checkpoint_every: str | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO distributed_runs
                   (run_id, model, nodes, gpus_per_node, strategy, status,
                    dataset_revision, checkpoint_every, updated_at)
               VALUES (?,?,?,?,?, 'running', ?,?, CURRENT_TIMESTAMP)""",
            (run_id, model, nodes, gpus_per_node, strategy, dataset_revision, checkpoint_every),
        )


def create_reindex_job(
    collection: str, tenant: str, from_encoder: str | None, to_encoder: str
) -> int:
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO reindex_jobs (collection, tenant, from_encoder, to_encoder, status)
               VALUES (?,?,?,?, 'building')""",
            (collection, tenant, from_encoder, to_encoder),
        )
        return last_insert_id(cur)


def get_adapter(adapter_id: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM lora_adapters WHERE adapter_id=?", (adapter_id,)
        ).fetchone()
    return dict(row) if row else None


def get_asset(name: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM assets WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["deps"] = json.loads(d.pop("deps_json"))
    d["built_from"] = json.loads(d["built_from_json"]) if d.get("built_from_json") else {}
    d.pop("built_from_json", None)
    return d


def get_collection(collection: str, tenant: str = "default") -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM embedding_collections WHERE collection=? AND tenant=?",
            (collection, tenant),
        ).fetchone()
    return dict(row) if row else None


def get_data_quality_checks(dataset: str, last_n: int = 20) -> list[dict[str, Any]]:
    """Return recent quality-check rows for ``dataset``, newest first."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM data_quality_checks WHERE dataset=? ORDER BY id DESC LIMIT ?",
            (dataset, last_n),
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


def get_distributed_run(run_id: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM distributed_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_encoder(encoder_id: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM encoders WHERE encoder_id=?", (encoder_id,)).fetchone()
    return dict(row) if row else None


def get_feature_view(name: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM feature_views WHERE name=?", (name,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["features"] = json.loads(d.pop("features_json"))
    return d


def get_offline_features_asof(view: str, entity_id: str, asof_ts: str) -> dict[str, Any] | None:
    """Latest offline feature values for an entity **as of** ``asof_ts`` (R4/R5, no leakage)."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            """SELECT values_json FROM feature_records
               WHERE view=? AND entity_id=? AND event_ts<=?
               ORDER BY event_ts DESC, id DESC LIMIT 1""",
            (view, entity_id, asof_ts),
        ).fetchone()
    return json.loads(row["values_json"]) if row else None


def get_online_feature(view: str, entity_id: str) -> dict[str, Any] | None:
    """Low-latency online read of the materialized feature vector (R3)."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT values_json FROM online_features WHERE view=? AND entity_id=?",
            (view, entity_id),
        ).fetchone()
    return json.loads(row["values_json"]) if row else None


def get_repro_bundle(model: str, version: str) -> dict[str, Any] | None:
    """Return the latest bundle for a model version (with parsed manifest), or None."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM repro_bundles WHERE model=? AND version=?
               ORDER BY bundle_version DESC LIMIT 1""",
            (model, version),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["manifest"] = json.loads(d["manifest_json"])
    return d


def last_materialization(view: str) -> dict[str, Any] | None:
    init_db()
    with get_db() as conn:
        row = conn.execute(
            """SELECT * FROM feature_view_materializations WHERE view=?
               ORDER BY id DESC LIMIT 1""",
            (view,),
        ).fetchone()
    return dict(row) if row else None


def list_adapters(base_ref: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        if base_ref:
            rows = conn.execute(
                "SELECT * FROM lora_adapters WHERE base_ref=? ORDER BY created_at DESC",
                (base_ref,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM lora_adapters ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def list_assets() -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM assets ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["deps"] = json.loads(d.pop("deps_json"))
        d["built_from"] = json.loads(d["built_from_json"]) if d.get("built_from_json") else {}
        d.pop("built_from_json", None)
        out.append(d)
    return out


def list_distributed_runs(model: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        if model:
            rows = conn.execute(
                "SELECT * FROM distributed_runs WHERE model=? ORDER BY created_at DESC", (model,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM distributed_runs ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def list_encoders() -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM encoders ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def list_online_features(view: str) -> list[dict[str, Any]]:
    """Every materialized online row of a view: ``{entity_id, event_ts, values}``."""
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT entity_id, event_ts, values_json FROM online_features WHERE view=? "
            "ORDER BY entity_id",
            (view,),
        ).fetchall()
    return [
        {
            "entity_id": r["entity_id"],
            "event_ts": r["event_ts"],
            "values": json.loads(r["values_json"] or "{}"),
        }
        for r in rows
    ]


def list_feature_views() -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM feature_views ORDER BY name").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["features"] = json.loads(d.pop("features_json"))
        out.append(d)
    return out


def list_reindex_jobs(collection: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        if collection:
            rows = conn.execute(
                "SELECT * FROM reindex_jobs WHERE collection=? ORDER BY id DESC", (collection,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reindex_jobs ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


# The newest bundle per (model, version). Selecting bare columns alongside MAX() is a SQLite
# extension — every other engine rejects it — so the latest row is picked by a correlated
# subquery, which means the same thing everywhere.
_LATEST_BUNDLES = """
    SELECT model, version, bundle_version, manifest_hash, signature, created_at
      FROM repro_bundles r
     WHERE bundle_version = (SELECT MAX(bundle_version) FROM repro_bundles x
                              WHERE x.model = r.model AND x.version = r.version)
"""


def list_repro_bundles(model: str | None = None) -> list[dict[str, Any]]:
    init_db()
    with get_db() as conn:
        if model:
            rows = conn.execute(
                _LATEST_BUNDLES + " AND r.model=? ORDER BY created_at DESC", (model,)
            ).fetchall()
        else:
            rows = conn.execute(_LATEST_BUNDLES + " ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def materialize_online(view: str, *, start_ts: str | None = None, end_ts: str | None = None) -> int:
    """Copy the latest offline value per entity into the online store (R6). Returns row count."""
    init_db()
    with get_db() as conn:
        clauses = ["view=?"]
        params: list[Any] = [view]
        if start_ts:
            clauses.append("event_ts>=?")
            params.append(start_ts)
        if end_ts:
            clauses.append("event_ts<=?")
            params.append(end_ts)
        where = " AND ".join(clauses)
        # Latest row per entity within the window.
        rows = conn.execute(
            f"""SELECT fr.entity_id, fr.event_ts, fr.values_json
                FROM feature_records fr
                JOIN (SELECT entity_id, MAX(event_ts) AS mx FROM feature_records
                      WHERE {where} GROUP BY entity_id) g
                  ON fr.entity_id=g.entity_id AND fr.event_ts=g.mx
                WHERE fr.view=?""",
            (*params, view),
        ).fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO online_features (view, entity_id, event_ts, values_json,
                                                materialized_at)
                   VALUES (?,?,?,?, CURRENT_TIMESTAMP)
                   ON CONFLICT(view, entity_id) DO UPDATE SET
                       event_ts=excluded.event_ts, values_json=excluded.values_json,
                       materialized_at=CURRENT_TIMESTAMP""",
                (view, r["entity_id"], r["event_ts"], r["values_json"]),
            )
        conn.execute(
            """INSERT INTO feature_view_materializations (view, start_ts, end_ts, rows)
               VALUES (?,?,?,?)""",
            (view, start_ts, end_ts, len(rows)),
        )
    return len(rows)


def purge_telemetry(
    retention_days: int = 90, *, dry_run: bool = False, vacuum: bool = False
) -> dict[str, int]:
    """Prune per-inference telemetry older than ``retention_days``; returns ``{table: rows}``.

    In ``dry_run`` mode nothing is deleted — the returned counts are what *would* be removed. Never
    touches the tamper-evident audit log or FinOps cost history. When ``vacuum`` is set (and not a
    dry run and something was deleted), reclaims freed pages afterwards.
    """
    if retention_days < 0:
        raise ValueError("retention_days must be >= 0")
    init_db()
    cutoff = f"-{int(retention_days)} days"
    result: dict[str, int] = {}
    with get_db() as conn:
        for table in _PRUNABLE_TELEMETRY:
            n = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE ts < datetime('now', ?)", (cutoff,)
            ).fetchone()["n"]
            result[table] = int(n)
            if not dry_run and n:
                conn.execute(f"DELETE FROM {table} WHERE ts < datetime('now', ?)", (cutoff,))
    if vacuum and not dry_run and any(result.values()):
        if _is_sqlite_backend():
            conn2 = _rdb.connect(_db_path())
            conn2.isolation_level = None  # VACUUM cannot run inside a transaction
            try:
                conn2.execute("VACUUM")
            finally:
                conn2.close()
        else:
            # C5: the file-level VACUUM opens PLATFORM_DB directly; under
            # EXAMLOPS_DB_BACKEND=postgres that would hit (and may create) a stray local
            # platform.db while the real data lives in Postgres. Skip it and say so —
            # Postgres reclaims space via autovacuum on its own schedule.
            result["vacuum_skipped"] = 1
    return result


def _is_sqlite_backend() -> bool:
    """Is the active datastore the SQLite file? Same selector ``storage.get_backend`` reads."""
    return os.getenv("EXAMLOPS_DB_BACKEND", "sqlite").strip().lower() != "postgres"


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


def record_dataset_revision(
    rev: Any,
    *,
    mlflow_run_id: str | None = None,
    row_count: int | None = None,
    byte_count: int | None = None,
    actor: str | None = None,
    synthetic: bool = False,
    source_revision: str | None = None,
    generator: str | None = None,
) -> None:
    """Record a resolved dataset revision.

    Idempotent on ``(backend, dataset, revision_id)`` (spec R5): re-recording the
    same revision is a no-op and never raises a UNIQUE-constraint error.

    A7 (ADR 0042): ``synthetic=True`` hard-flags the revision so it can never pass as
    real (spec R4); ``source_revision``/``generator`` anchor its provenance to the real
    revision it was derived from and the generator method used.
    """
    init_db()
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO dataset_revisions
                   (backend, dataset, revision_id, kind, uri, schema_hash,
                    mlflow_run_id, row_count, byte_count, actor,
                    synthetic, source_revision, generator)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                1 if synthetic else 0,
                source_revision,
                generator,
            ),
        )
        inserted = cur.rowcount > 0

    if inserted:
        _declare_dataset_asset(rev.dataset, actor=actor)


def _declare_dataset_asset(dataset: str, *, actor: str | None = None) -> None:
    """Advance the dataset's A4 asset so downstream models go stale (ADR 0036).

    `mark_source_changed` was written for precisely this — its docstring reads "e.g. an A1 dataset
    revision landed" — and nothing in the platform called it. The ADR's finding was that the asset
    DAG "is empty until an operator types it in, which is the opposite of the declarative substrate
    the ADR describes"; a dataset revision is the platform's most common source event, so this is
    where the DAG starts building itself.

    **Only on a real insert.** `record_dataset_revision` is idempotent on
    `(backend, dataset, revision_id)`, and re-recording the same revision must not bump the
    version: that would report the dataset as changed when nothing changed, and every downstream
    model would go spuriously stale — a freshness signal that cries wolf is one nobody acts on.

    Best-effort. A dataset revision is the durable fact here; the asset graph is a derived view of
    it, and failing to update the view must never lose the fact.
    """
    try:
        from examlops.assets import mark_source_changed

        mark_source_changed(dataset, actor=actor)
    except Exception:
        pass


def record_synthetic_dataset(
    revision_id: str,
    dataset: str,
    *,
    source_revision: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    n_rows: int | None = None,
    fidelity_score: float | None = None,
    privacy_score: float | None = None,
    released: bool = False,
    reasons: list[str] | None = None,
    actor: str | None = None,
) -> None:
    """Record the fidelity/privacy gate outcome for a synthetic revision (A7, spec R2/R3).

    Idempotent on ``revision_id``: re-evaluating a revision overwrites its scores/gate
    verdict so the latest evaluation is authoritative.
    """
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO synthetic_datasets
                   (revision_id, dataset, source_revision, method, params_json, n_rows,
                    fidelity_score, privacy_score, released, reasons_json, actor)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(revision_id) DO UPDATE SET
                   dataset=excluded.dataset, source_revision=excluded.source_revision,
                   method=excluded.method, params_json=excluded.params_json,
                   n_rows=excluded.n_rows, fidelity_score=excluded.fidelity_score,
                   privacy_score=excluded.privacy_score, released=excluded.released,
                   reasons_json=excluded.reasons_json, actor=excluded.actor""",
            (
                revision_id,
                dataset,
                source_revision,
                method,
                json.dumps(params or {}),
                n_rows,
                fidelity_score,
                privacy_score,
                1 if released else 0,
                json.dumps(reasons or []),
                actor,
            ),
        )


def get_synthetic_dataset(revision_id: str) -> dict[str, Any] | None:
    """Return the synthetic-dataset gate record for a revision, or None."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM synthetic_datasets WHERE revision_id=?", (revision_id,)
        ).fetchone()
    if row is None:
        return None
    rec = dict(row)
    rec["params"] = json.loads(rec.pop("params_json", None) or "{}")
    rec["reasons"] = json.loads(rec.pop("reasons_json", None) or "[]")
    rec["released"] = bool(rec.get("released"))
    return rec


def list_synthetic_datasets(dataset: str | None = None) -> list[dict[str, Any]]:
    """List synthetic-dataset gate records, newest first (optionally filtered by dataset)."""
    init_db()
    with get_db() as conn:
        if dataset is not None:
            rows = conn.execute(
                "SELECT * FROM synthetic_datasets WHERE dataset=? ORDER BY created_at DESC",
                (dataset,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM synthetic_datasets ORDER BY created_at DESC"
            ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        rec["params"] = json.loads(rec.pop("params_json", None) or "{}")
        rec["reasons"] = json.loads(rec.pop("reasons_json", None) or "[]")
        rec["released"] = bool(rec.get("released"))
        out.append(rec)
    return out


def synthetic_proportion(revision_ids: list[str]) -> float:
    """Fraction of the given dataset revisions flagged synthetic (A7, spec R5).

    Unknown revision ids count as real (conservative). Returns 0.0 for an empty set.
    """
    ids = [r for r in revision_ids if r]
    if not ids:
        return 0.0
    placeholders = ",".join("?" for _ in ids)
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT revision_id, synthetic FROM dataset_revisions WHERE revision_id IN ({placeholders})",  # noqa: S608 - placeholders are bound params, ids are values
            ids,
        ).fetchall()
    flags = {r["revision_id"]: bool(r["synthetic"]) for r in rows}
    synthetic_n = sum(1 for rid in ids if flags.get(rid, False))
    return synthetic_n / len(ids)


def is_synthetic_only(revision_ids: list[str]) -> bool:
    """True iff every given revision is flagged synthetic (A7, spec R5/GWT-5).

    The D5 policy layer calls this to forbid promoting a model trained on synthetic
    data alone. An empty set is not synthetic-only (returns False).
    """
    ids = [r for r in revision_ids if r]
    if not ids:
        return False
    return synthetic_proportion(ids) >= 1.0


def register_adapter(
    adapter_id: str,
    base_ref: str,
    *,
    method: str = "lora",
    rank: int | None = None,
    target_modules: str | None = None,
    dataset_revision: str | None = None,
    eval_score: float | None = None,
    eval_floor: float | None = None,
    signature: str | None = None,
    signed_by: str | None = None,
    cost_gpu_hours: float | None = None,
) -> None:
    """Register/patch a LoRA adapter as a first-class artifact (R3)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO lora_adapters
                   (adapter_id, base_ref, method, rank, target_modules, dataset_revision,
                    eval_score, eval_floor, signature, signed_by, cost_gpu_hours, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(adapter_id) DO UPDATE SET
                   base_ref=excluded.base_ref, method=excluded.method, rank=excluded.rank,
                   target_modules=excluded.target_modules,
                   dataset_revision=excluded.dataset_revision, eval_score=excluded.eval_score,
                   eval_floor=excluded.eval_floor, signature=excluded.signature,
                   signed_by=excluded.signed_by, cost_gpu_hours=excluded.cost_gpu_hours,
                   updated_at=CURRENT_TIMESTAMP""",
            (
                adapter_id,
                base_ref,
                method,
                rank,
                target_modules,
                dataset_revision,
                eval_score,
                eval_floor,
                signature,
                signed_by,
                cost_gpu_hours,
            ),
        )


def register_asset(
    name: str,
    kind: str = "model",
    deps: list[str] | None = None,
    *,
    description: str | None = None,
) -> None:
    """Register/patch an asset declaration (R1). Preserves version + freshness state."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO assets (name, kind, deps_json, description, updated_at)
               VALUES (?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(name) DO UPDATE SET
                   kind=excluded.kind, deps_json=excluded.deps_json,
                   description=excluded.description, updated_at=CURRENT_TIMESTAMP""",
            (name, kind, json.dumps(deps or []), description),
        )


def register_encoder_row(
    encoder_id: str, name: str, version: str, dim: int, metric: str, normalization: str
) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO encoders
                   (encoder_id, name, version, dim, metric, normalization)
               VALUES (?,?,?,?,?,?)""",
            (encoder_id, name, version, dim, metric, normalization),
        )


def set_adapter_promoted(adapter_id: str, promoted: bool) -> None:
    init_db()
    with get_db() as conn:
        conn.execute(
            "UPDATE lora_adapters SET promoted=?, updated_at=CURRENT_TIMESTAMP WHERE adapter_id=?",
            (1 if promoted else 0, adapter_id),
        )


def store_repro_bundle(
    model: str,
    version: str,
    manifest: dict[str, Any],
    manifest_hash: str,
    *,
    signature: str | None = None,
    algo: str | None = None,
    signed_by: str | None = None,
) -> int:
    """Persist a reproducibility bundle manifest (versioned; audited by the caller) (R2)."""
    init_db()
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(bundle_version) AS mx FROM repro_bundles WHERE model=? AND version=?",
            (model, version),
        ).fetchone()
        bundle_version = (row["mx"] or 0) + 1
        conn.execute(
            """INSERT INTO repro_bundles
                   (model, version, bundle_version, manifest_json, manifest_hash,
                    signature, algo, signed_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                model,
                version,
                bundle_version,
                json.dumps(manifest),
                manifest_hash,
                signature,
                algo,
                signed_by,
            ),
        )
    return bundle_version


def update_distributed_run(
    run_id: str,
    *,
    status: str | None = None,
    cost_gpu_hours: float | None = None,
    bump_resumes: bool = False,
) -> None:
    init_db()
    with get_db() as conn:
        if status is not None:
            conn.execute(
                "UPDATE distributed_runs SET status=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (status, run_id),
            )
        if cost_gpu_hours is not None:
            conn.execute(
                "UPDATE distributed_runs SET cost_gpu_hours=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE run_id=?",
                (cost_gpu_hours, run_id),
            )
        if bump_resumes:
            conn.execute(
                "UPDATE distributed_runs SET resumes=resumes+1, updated_at=CURRENT_TIMESTAMP "
                "WHERE run_id=?",
                (run_id,),
            )


def latest_vector_metrics(tenant: str | None = None) -> list[dict[str, Any]]:
    """The most recent row per (collection, tenant, operation) from ``vector_metrics``.

    The **latest**, not an average: the table is an append-only log, and a gauge averaging a
    collection's whole history moves less and less as the log grows — the opposite of what an
    operator watching a reindex needs.
    """
    init_db()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT collection, tenant, operation, latency_ms, item_count FROM vector_metrics "
            "WHERE id IN ("
            "  SELECT MAX(id) FROM vector_metrics GROUP BY collection, tenant, operation"
            ")"
        ).fetchall()
    out = [dict(r) for r in rows]
    return [r for r in out if tenant is None or r["tenant"] == tenant]


def update_reindex_job(
    job_id: int,
    *,
    status: str | None = None,
    recall: float | None = None,
    docs_reindexed: int | None = None,
    orchestrator: str | None = None,
    hpc_job_id: str | None = None,
    duration_s: float | None = None,
) -> None:
    init_db()
    with get_db() as conn:
        for column, value in (
            ("orchestrator", orchestrator),
            ("hpc_job_id", hpc_job_id),
            ("duration_s", duration_s),
        ):
            if value is not None:
                conn.execute(
                    f"UPDATE reindex_jobs SET {column}=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (value, job_id),
                )
        if status is not None:
            conn.execute(
                "UPDATE reindex_jobs SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, job_id),
            )
        if recall is not None:
            conn.execute(
                "UPDATE reindex_jobs SET recall=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (recall, job_id),
            )
        if docs_reindexed is not None:
            conn.execute(
                "UPDATE reindex_jobs SET docs_reindexed=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (docs_reindexed, job_id),
            )


def upsert_collection(
    collection: str,
    tenant: str = "default",
    *,
    active_encoder_id: Any = _UNSET,
    staging_encoder_id: Any = _UNSET,
    status: str | None = None,
) -> None:
    init_db()
    existing = get_collection(collection, tenant) or {}
    active = existing.get("active_encoder_id") if active_encoder_id is _UNSET else active_encoder_id
    staging = (
        existing.get("staging_encoder_id") if staging_encoder_id is _UNSET else staging_encoder_id
    )
    st = status if status is not None else existing.get("status", "active")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO embedding_collections
                   (collection, tenant, active_encoder_id, staging_encoder_id, status, updated_at)
               VALUES (?,?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(collection, tenant) DO UPDATE SET
                   active_encoder_id=excluded.active_encoder_id,
                   staging_encoder_id=excluded.staging_encoder_id,
                   status=excluded.status, updated_at=CURRENT_TIMESTAMP""",
            (collection, tenant, active, staging, st),
        )


def upsert_feature_view(
    name: str,
    entity: str,
    features: list[str],
    *,
    source: str | None = None,
    ttl_seconds: int = 0,
    dataset_revision: str | None = None,
    embedding_feature: str | None = None,
) -> None:
    """Register/patch a feature view (single definition for train + serve) (R1)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO feature_views
                   (name, entity, features_json, source, ttl_seconds, dataset_revision,
                    embedding_feature, updated_at)
               VALUES (?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
               ON CONFLICT(name) DO UPDATE SET
                   entity=excluded.entity, features_json=excluded.features_json,
                   source=excluded.source, ttl_seconds=excluded.ttl_seconds,
                   dataset_revision=excluded.dataset_revision,
                   embedding_feature=excluded.embedding_feature, updated_at=CURRENT_TIMESTAMP""",
            (
                name,
                entity,
                json.dumps(features),
                source,
                ttl_seconds,
                dataset_revision,
                embedding_feature,
            ),
        )


def write_feature_record(view: str, entity_id: str, event_ts: str, values: dict[str, Any]) -> None:
    """Append an offline feature observation (point-in-time source of truth)."""
    init_db()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO feature_records (view, entity_id, event_ts, values_json)
               VALUES (?,?,?,?)""",
            (view, entity_id, event_ts, json.dumps(values)),
        )


install_write_retry(__name__)
