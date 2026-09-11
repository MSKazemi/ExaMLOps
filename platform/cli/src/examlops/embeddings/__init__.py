"""Next-Gen 40 · B6 — embedding lifecycle & reindexing (ADR 0043).

Governs the *embeddings* used by the RAG cluster (B3 cache / B4 RAG / B5 vector store):

- **Versioned encoders (R1):** each encoder (name, version, dim, metric, normalization) is
  registered with a stable ``encoder_id``.
- **Stamping (R2):** stored vectors + cache entries carry their ``encoder_id`` (this module
  provides the id; B3/B5 stamp with it).
- **Compatibility guard (R3/GWT-2):** any similarity comparison across different
  ``encoder_id``s is **refused**, never silently computed in an incompatible space.
- **Blue-green reindex (R4/R5/GWT-3/GWT-4):** on an encoder change a new index is built in
  the background, the corpus re-embedded, **recall verified** before an **atomic switch**;
  the old index is retained until the switch is confirmed, then pruned.
- **Rebaseline (R6/GWT-5):** input-embedding drift baselines (C5) are reset after a reindex,
  since the embedding space changed.
- **Governance:** encoder changes are audited (D4) and per-tenant collections reindex
  independently (D6).

Pure-Python and testable — no encoder model, GPU, or vector DB required.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from examlops import data as platform_db


class EncoderMismatchError(RuntimeError):
    """Raised when vectors from different encoders are compared (R3/GWT-2)."""


class ReindexAbortedError(RuntimeError):
    """Raised when a reindex fails recall verification and is aborted (R4)."""


class ReindexSubmissionError(RuntimeError):
    """The scheduler refused the reindex job; the reindex row is marked ``failed``."""


@dataclass
class ReindexResult:
    collection: str
    tenant: str
    from_encoder: str | None
    to_encoder: str
    #: None when the reindex was **submitted** rather than run here — no recall has been measured
    #: yet, and 0.0 would read as "verified and terrible".
    recall: float | None
    switched: bool
    docs_reindexed: int
    reason: str


def encoder_id(name: str, version: str, dim: int, metric: str, normalization: str) -> str:
    """Deterministic, content-addressed encoder id (R1)."""
    payload = f"{name}|{version}|{dim}|{metric}|{normalization}"
    return f"{name}-{version}-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


ENCODER_REGISTRIES = ("local", "mlflow")


def encoder_registry() -> str:
    """Where encoders are recorded: ``EXAMLOPS_ENCODER_REGISTRY`` (default ``local``).

    ``mlflow`` makes the MLflow encoder registry the record (ADR 0043 clause 1, see
    :mod:`examlops.embeddings.mlflow_registry`) with ``platform.db`` as its index. An unknown
    value is an error: a typo must not quietly keep the registry of record somewhere else.
    """
    import os

    name = (os.getenv("EXAMLOPS_ENCODER_REGISTRY") or "local").strip().lower()
    if name not in ENCODER_REGISTRIES:
        raise ValueError(
            f"EXAMLOPS_ENCODER_REGISTRY={name!r} is not one of {', '.join(ENCODER_REGISTRIES)}"
        )
    return name


def register_encoder(
    name: str,
    version: str,
    dim: int,
    *,
    metric: str = "cosine",
    normalization: str = "l2",
    actor: str | None = None,
) -> str:
    """Register an encoder and return its ``encoder_id`` (R1). Idempotent.

    Under the MLflow registry the encoder is published there **first** and indexed in
    ``platform.db`` only afterwards, so a failed publish registers nothing and the two never
    disagree. Re-registering an encoder that is only local publishes it.
    """
    eid = encoder_id(name, version, dim, metric, normalization)
    if encoder_registry() == "mlflow":
        from examlops.embeddings import mlflow_registry

        card = {
            "encoder_id": eid,
            "name": name,
            "version": version,
            "dim": int(dim),
            "metric": metric,
            "normalization": normalization,
        }
        mlflow_registry.publish(card, actor=actor)
    platform_db.register_encoder_row(eid, name, version, dim, metric, normalization)
    return eid


def get_encoder(enc_id: str) -> dict[str, Any] | None:
    """An encoder's record — from the local index, or (MLflow registry) pulled from MLflow.

    An encoder published by another instance that shares this MLflow is indexed here on first
    use, so a reindex or a guard never refuses an encoder the registry of record knows.
    """
    row = platform_db.get_encoder(enc_id)
    if row is not None or encoder_registry() != "mlflow":
        return row
    from examlops.embeddings import mlflow_registry

    card = mlflow_registry.fetch(enc_id)
    if card is None:
        return None
    platform_db.register_encoder_row(
        enc_id, card["name"], card["version"], card["dim"], card["metric"], card["normalization"]
    )
    return platform_db.get_encoder(enc_id)


def list_encoders() -> list[dict[str, Any]]:
    """Every registered encoder, newest first — from the registry of record.

    Under MLflow the listing is MLflow's, and any encoder missing from the local index is added
    to it. Each row carries ``registry`` (``local`` / ``mlflow``) so a listing says where it came
    from.
    """
    if encoder_registry() != "mlflow":
        return [{**r, "registry": "local"} for r in platform_db.list_encoders()]
    from examlops.embeddings import mlflow_registry

    rows: list[dict[str, Any]] = []
    for card in mlflow_registry.fetch_all():
        if platform_db.get_encoder(card["encoder_id"]) is None:
            platform_db.register_encoder_row(
                card["encoder_id"],
                card["name"],
                card["version"],
                card["dim"],
                card["metric"],
                card["normalization"],
            )
        rows.append({**card, "registry": "mlflow"})
    return sorted(rows, key=lambda r: r.get("created_at") or 0, reverse=True)


def migrate_encoders(*, dry_run: bool = False, actor: str | None = None) -> dict[str, Any]:
    """Publish every locally registered encoder to the MLflow registry (additive, idempotent).

    Encoder ids are content-addressed, so an encoder already in MLflow is **skipped** — it is the
    same record by construction. Runs regardless of ``EXAMLOPS_ENCODER_REGISTRY`` so a deployment
    can publish before switching over; it needs ``MLFLOW_TRACKING_URI``.
    """
    from examlops.embeddings import mlflow_registry

    published: list[str] = []
    skipped: list[str] = []
    for row in platform_db.list_encoders():
        eid = row["encoder_id"]
        if mlflow_registry.fetch(eid) is not None:
            skipped.append(eid)
            continue
        if not dry_run:
            mlflow_registry.publish(
                {
                    "encoder_id": eid,
                    "name": row["name"],
                    "version": row["version"],
                    "dim": int(row["dim"]),
                    "metric": row["metric"],
                    "normalization": row["normalization"],
                },
                actor=actor,
            )
        published.append(eid)
    return {"dry_run": dry_run, "published": published, "skipped": skipped}


def guard_compatible(a_encoder_id: str, b_encoder_id: str) -> None:
    """Refuse a cross-encoder comparison (R3/GWT-2)."""
    if a_encoder_id != b_encoder_id:
        raise EncoderMismatchError(
            f"cannot compare vectors across encoders: {a_encoder_id!r} vs {b_encoder_id!r} — "
            "reindex the corpus to a single encoder first"
        )


def set_collection_encoder(collection: str, enc_id: str, *, tenant: str = "default") -> None:
    """Set the active encoder for a collection (initial bootstrap) (R2)."""
    platform_db.upsert_collection(collection, tenant, active_encoder_id=enc_id, status="active")


# ── Reindex orchestration (ADR 0043 clause 4) ─────────────────────────────────
#
# "reindex runs as a scheduler job (large corpora), progress/cost tracked, invoked by B5's hook."
# It ran inline in the calling process, was invoked only by the CLI, and recorded no timing.


def _reindex_mode(mode: str | None = None) -> str:
    """``inline`` (default) or ``scheduler``. An unrecognised value is ``inline``.

    Inline stays the default because a reindex that silently became a cluster submission on
    upgrade would strand every existing caller waiting for a result that now arrives elsewhere.
    """
    import os

    chosen = (mode or os.getenv("EXAMLOPS_REINDEX_ORCHESTRATOR") or "inline").strip().lower()
    return chosen if chosen in ("inline", "scheduler") else "inline"


def submit_reindex(
    collection: str,
    new_encoder_id: str,
    *,
    tenant: str = "default",
    job_id: int | None = None,
    corpus_size: int = 0,
    recall: float | None = None,
    recall_floor: float = 0.9,
    actor: str | None = None,
) -> str | None:
    """Run reindex ``job_id`` as a scheduler job; returns the scheduler's job id, or ``None`` when
    there is no scheduler here (the caller then runs it inline).

    The job is ``python -m examlops.embeddings.job --job-id N`` with the operator's recall inputs:
    it **continues the same ``reindex_jobs`` row** — ``submitted`` → ``switched`` / ``aborted`` /
    ``failed`` — rather than re-entering ``exa embedding reindex``, which opened a second row and
    left the first ``submitted`` forever, and ran without ``--recall`` / ``--recall-floor``, so the
    recall gate the operator set was never applied. Only the job id and numbers reach the script;
    collection and encoder names are read back from the row. The job does the bookkeeping, so it
    needs the same datastore as this process (shared ``platform.db`` or the Postgres backend).

    On the mock, which executes a job only when waited on, this waits — the reindex is done when
    it returns. On Slurm / Flux it returns once the job is queued.
    """
    try:
        adapter = _scheduler_adapter()
    except Exception:  # noqa: BLE001 - no scheduler here is an environment fact
        return None
    from examlops import scheduler_jobs as jobs

    if job_id is None:  # a direct caller: open the row the job will continue
        current = platform_db.get_collection(collection, tenant) or {}
        job_id = platform_db.create_reindex_job(
            collection, tenant, current.get("active_encoder_id"), new_encoder_id
        )
        platform_db.update_reindex_job(job_id, orchestrator="scheduler")
    argv = [
        jobs.job_python(),
        "-m",
        "examlops.embeddings.job",
        "--job-id",
        str(job_id),
        "--corpus-size",
        str(int(corpus_size)),
        "--recall-floor",
        repr(float(recall_floor)),
    ]
    if recall is not None:
        argv += ["--recall", repr(float(recall))]
    if actor:
        argv += ["--actor", actor]
    script, job_key = jobs.write_script(
        "reindex", jobs.script_text(argv, title=f"embedding reindex job {job_id} (ADR 0043)")
    )
    scheduler = jobs.scheduler_name()
    resources = {"job_name": f"reindex-{collection}"}
    # `submitted` is written *before* the job exists: the job runs only a `submitted` row, and an
    # idle cluster can start it before `submit_job` has even returned here. Afterwards only the
    # job id is recorded — by then the job may already have moved the row on.
    platform_db.update_reindex_job(job_id, status="submitted")
    try:
        hpc_job_id = jobs.submit(adapter, script, job_key, resources)
    except Exception as exc:  # noqa: BLE001
        platform_db.update_reindex_job(job_id, status="failed")
        raise ReindexSubmissionError(
            f"reindex of {collection!r}: the {scheduler} scheduler refused the job ({exc})"
        ) from exc
    platform_db.update_reindex_job(job_id, hpc_job_id=hpc_job_id)
    jobs.record_job(hpc_job_id, scheduler, f"reindex:{collection}", resources)
    _audit(
        collection,
        tenant,
        "reindex_submitted",
        {"to": new_encoder_id, "hpc_job_id": hpc_job_id, "reindex_job": job_id},
        actor,
    )
    if jobs.executes_only_when_waited(scheduler):
        try:
            adapter.wait_until_complete(hpc_job_id)
            status = adapter.get_job_status(hpc_job_id)
        except Exception as exc:  # noqa: BLE001
            status = {"state": f"UNKNOWN ({exc})"}
        jobs.finish_job(hpc_job_id, scheduler, status)
        if _job_row(job_id).get("status") == "submitted":
            # The job ended without reaching the reindex (it could not start or import).
            platform_db.update_reindex_job(job_id, status="failed")
    return hpc_job_id


def _job_row(job_id: int) -> dict[str, Any]:
    return next((r for r in platform_db.list_reindex_jobs() if r.get("id") == job_id), {})


def _scheduler_adapter() -> Any:
    from examlops.scheduler_jobs import scheduler_adapter

    return scheduler_adapter()


def recommend_reindex(
    collection: str, tenant: str, *, from_encoder: str | None, to_encoder: str
) -> None:
    """B5's hook: record that a collection needs reindexing (clause 4).

    Called when the vector store meets a vector from a different encoder. It **records a
    recommendation and does not start one**: a search that quietly triggered the re-embedding of
    a large corpus would turn one query into an unbounded, unbudgeted job, and the caller asked
    for a search. The mismatch still raises — see ``guard_compatible`` — and now it also leaves a
    trail an operator can act on.

    Idempotent per (collection, tenant, to_encoder): a mismatched collection is usually queried
    many times, and one recommendation per query would bury the signal it exists to give.
    """
    try:
        for job in platform_db.list_reindex_jobs(collection):
            if job.get("status") == "recommended" and job.get("to_encoder") == to_encoder:
                return
        job_id = platform_db.create_reindex_job(collection, tenant, from_encoder, to_encoder)
        platform_db.update_reindex_job(job_id, status="recommended")
        _audit(
            collection,
            tenant,
            "reindex_recommended",
            {"from": from_encoder, "to": to_encoder},
            None,
        )
    except Exception:  # noqa: BLE001 - a recommendation must never break the caller's operation
        pass


def reindex(
    collection: str,
    new_encoder_id: str,
    *,
    tenant: str = "default",
    corpus_size: int = 0,
    recall_fn: Callable[[], float] | None = None,
    recall_floor: float = 0.9,
    actor: str | None = None,
    orchestrator: str | None = None,
    recall: float | None = None,
    _resume_job_id: int | None = None,
) -> ReindexResult:
    """Blue-green reindex a collection to a new encoder (R4/R5/R6/GWT-3/GWT-4/GWT-5).

    Build a staging index → re-embed → **verify recall** → **atomic switch** (retain old
    until confirmed, then prune) → **rebaseline** input drift → audit. If recall is below
    the floor the switch is refused and the old index is kept.

    Recall is ``recall_fn()`` when given, else ``recall``, else 1.0. On the scheduler path only a
    *value* can travel to the job: a ``recall_fn`` lives in this process, so that reindex runs
    here and is recorded ``inline-fallback`` (the rule asset closures follow). ``_resume_job_id``
    is how the job continues the row this call opened.
    """
    if get_encoder(new_encoder_id) is None:
        raise ValueError(f"unknown encoder {new_encoder_id!r} — register it first")
    current = platform_db.get_collection(collection, tenant) or {}
    from_encoder = current.get("active_encoder_id")

    started = time.perf_counter()
    if _resume_job_id is not None:
        job_id, mode = _resume_job_id, "inline"  # the row keeps orchestrator=scheduler
    else:
        job_id = platform_db.create_reindex_job(collection, tenant, from_encoder, new_encoder_id)
        mode = _reindex_mode(orchestrator)
        platform_db.update_reindex_job(job_id, orchestrator=mode)

    if mode == "scheduler" and recall_fn is not None:
        platform_db.update_reindex_job(job_id, orchestrator="inline-fallback")
        _audit(
            collection,
            tenant,
            "reindex_inline_fallback",
            {"to": new_encoder_id, "reason": "a recall function cannot cross into a job"},
            actor,
        )
    elif mode == "scheduler":
        hpc_job_id = submit_reindex(
            collection,
            new_encoder_id,
            tenant=tenant,
            job_id=job_id,
            corpus_size=corpus_size,
            recall=recall,
            recall_floor=recall_floor,
            actor=actor,
        )
        if hpc_job_id is not None:
            return _result_from_row(job_id, collection, tenant, from_encoder, new_encoder_id)
        # No scheduler reachable — say so and do the work here rather than not at all.
        platform_db.update_reindex_job(job_id, orchestrator="inline-fallback")

    # 1) staging build: old index stays active + retained (R5/GWT-4).
    platform_db.upsert_collection(
        collection, tenant, staging_encoder_id=new_encoder_id, status="building"
    )
    # 2) re-embed corpus (mock: count docs), 3) verify recall.
    if recall_fn is not None:
        recall = recall_fn()
    elif recall is None:
        recall = 1.0
    platform_db.update_reindex_job(job_id, recall=recall, docs_reindexed=corpus_size)

    if recall < recall_floor:
        # Abort: keep the old index active, drop the staging index.
        platform_db.upsert_collection(collection, tenant, staging_encoder_id=None, status="active")
        platform_db.update_reindex_job(
            job_id, status="aborted", duration_s=time.perf_counter() - started
        )
        _audit(
            collection,
            tenant,
            "reindex_aborted",
            {"to": new_encoder_id, "recall": recall, "floor": recall_floor},
            actor,
        )
        return ReindexResult(
            collection=collection,
            tenant=tenant,
            from_encoder=from_encoder,
            to_encoder=new_encoder_id,
            recall=recall,
            switched=False,
            docs_reindexed=corpus_size,
            reason=f"recall {recall:.3f} < floor {recall_floor:.3f} — kept old index",
        )

    # 4) atomic switch, then prune the old (staging cleared).
    platform_db.update_reindex_job(job_id, status="verified")
    platform_db.upsert_collection(
        collection,
        tenant,
        active_encoder_id=new_encoder_id,
        staging_encoder_id=None,
        status="active",
    )
    platform_db.update_reindex_job(
        job_id, status="switched", duration_s=time.perf_counter() - started
    )
    # 5) rebaseline input-embedding drift (C5) — the space changed (R6/GWT-5).
    _rebaseline_input_drift(collection, new_encoder_id)
    _audit(
        collection,
        tenant,
        "reindex_switched",
        {"from": from_encoder, "to": new_encoder_id, "recall": recall},
        actor,
    )
    return ReindexResult(
        collection=collection,
        tenant=tenant,
        from_encoder=from_encoder,
        to_encoder=new_encoder_id,
        recall=recall,
        switched=True,
        docs_reindexed=corpus_size,
        reason="switched atomically; old index pruned; drift rebaselined",
    )


def _result_from_row(
    job_id: int, collection: str, tenant: str, from_encoder: str | None, to_encoder: str
) -> ReindexResult:
    """What a scheduler reindex has reached: ``submitted`` while queued, else its outcome."""
    row = _job_row(job_id)
    status = row.get("status") or "submitted"
    reasons = {
        "submitted": f"submitted to the scheduler as job {row.get('hpc_job_id')}",
        "switched": "switched atomically by the scheduler job; drift rebaselined",
        "aborted": "the scheduler job kept the old index — recall below the floor",
        "failed": f"the scheduler job {row.get('hpc_job_id')} failed; the old index is kept",
    }
    return ReindexResult(
        collection=collection,
        tenant=tenant,
        from_encoder=from_encoder,
        to_encoder=to_encoder,
        recall=row.get("recall") if status != "submitted" else None,
        switched=status == "switched",
        docs_reindexed=int(row.get("docs_reindexed") or 0) if status != "submitted" else 0,
        reason=reasons.get(status, status),
    )


def reindex_status(collection: str, tenant: str = "default", *, reconcile: bool = True) -> dict:
    """A collection's encoders and reindex history — reconciled with the scheduler first.

    ``reconcile`` (default) settles any row still ``submitted`` whose scheduler job has ended
    (see :func:`reconcile_submitted`), so a status never reports a dead job as queued.
    """
    if reconcile:
        reconcile_submitted(collection, tenant)
    coll = platform_db.get_collection(collection, tenant)
    jobs = platform_db.list_reindex_jobs(collection)
    return {"collection": coll, "jobs": jobs}


_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT")


def reconcile_submitted(collection: str, tenant: str = "default") -> list[int]:
    """Mark ``failed`` every ``submitted`` reindex whose scheduler job ended without settling it.

    On Slurm / Flux a reindex is fire-and-forget: the job moves its own row on. A job that dies
    before its interpreter runs — node failure, a cancelled or timed-out allocation, a broken
    environment — never does, and the row would read ``submitted`` forever.

    **The order is what makes it safe.** The scheduler is asked first; only a terminal state
    leads to re-reading the row. By then the job's process is gone, so any write it was going to
    make has been made — a row still ``submitted`` is genuinely orphaned, including when the job
    ``COMPLETED`` without reaching the reindex. Reading the row first would race a job finishing
    its bookkeeping. Anything short of a clear answer — no scheduler here, a job it cannot see, a
    state that is not terminal — leaves the row alone: reconciliation may only settle, never
    guess. Returns the reindex job ids it marked ``failed``.
    """
    pending = [
        r
        for r in platform_db.list_reindex_jobs(collection)
        if r.get("status") == "submitted" and r.get("hpc_job_id") and r.get("tenant") == tenant
    ]
    if not pending:
        return []
    try:
        adapter = _scheduler_adapter()
    except Exception:  # noqa: BLE001 - no scheduler here: nothing can be decided
        return []
    from examlops import scheduler_jobs as jobs

    settled: list[int] = []
    for row in pending:
        try:
            status = adapter.get_job_status(str(row["hpc_job_id"]))
        except Exception:  # noqa: BLE001 - a job the scheduler cannot see is not a verdict
            continue
        state = str(status.get("state") or "UNKNOWN").upper()
        if state not in _TERMINAL:
            continue
        if _job_row(int(row["id"])).get("status") != "submitted":
            continue  # the job settled it after all
        platform_db.update_reindex_job(int(row["id"]), status="failed")
        jobs.finish_job(str(row["hpc_job_id"]), jobs.scheduler_name(), status)
        _audit(
            collection,
            tenant,
            "reindex_reconciled_failed",
            {"reindex_job": row["id"], "hpc_job_id": row["hpc_job_id"], "scheduler_state": state},
            None,
        )
        settled.append(int(row["id"]))
    return settled


def _rebaseline_input_drift(collection: str, enc_id: str) -> None:
    try:
        platform_db.set_input_baseline(
            collection, {"rebaselined_for_encoder": enc_id, "emb_norm": None}
        )
    except Exception:
        pass


def _audit(collection: str, tenant: str, action: str, extra: dict, actor: str | None) -> None:
    try:
        from examlops.data.audit import write_audit_event

        write_audit_event("exa-embedding", actor, action, collection, extra, tenant=tenant)
    except Exception:
        pass


__all__ = [
    "EncoderMismatchError",
    "ReindexAbortedError",
    "ReindexResult",
    "encoder_id",
    "register_encoder",
    "encoder_registry",
    "get_encoder",
    "list_encoders",
    "migrate_encoders",
    "guard_compatible",
    "set_collection_encoder",
    "reindex",
    "reindex_status",
    "reconcile_submitted",
]
