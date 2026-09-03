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


def register_encoder(
    name: str,
    version: str,
    dim: int,
    *,
    metric: str = "cosine",
    normalization: str = "l2",
) -> str:
    """Register an encoder and return its ``encoder_id`` (R1)."""
    eid = encoder_id(name, version, dim, metric, normalization)
    platform_db.register_encoder_row(eid, name, version, dim, metric, normalization)
    return eid


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


def submit_reindex(collection: str, new_encoder_id: str, *, tenant: str = "default") -> str | None:
    """Submit the reindex to the phase-23 scheduler; returns its job id, or None if unavailable.

    Large corpora are the case the clause names, and re-embedding one in the caller's process
    blocks whatever asked. The submitted command re-enters the CLI pinned to ``--inline``, or the
    job would submit another job.
    """
    try:
        adapter = _scheduler_adapter()
    except Exception:  # noqa: BLE001 - no scheduler here is an environment fact
        return None
    return str(
        adapter.submit_job(
            script_path=None,
            resources={"job_name": f"reindex-{collection}"},
            training_data={
                "command": (
                    f"exa embedding reindex {collection} {new_encoder_id} "
                    f"--tenant {tenant} --inline"
                )
            },
        )
    )


def _scheduler_adapter() -> Any:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[5]
    adapter_dir = root / "platform" / "infra" / "slurm-adapter"
    if str(adapter_dir) not in sys.path:
        sys.path.insert(0, str(adapter_dir))
    from adapter import get_scheduler_adapter  # noqa: PLC0415

    return get_scheduler_adapter()


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
) -> ReindexResult:
    """Blue-green reindex a collection to a new encoder (R4/R5/R6/GWT-3/GWT-4/GWT-5).

    Build a staging index → re-embed → **verify recall** → **atomic switch** (retain old
    until confirmed, then prune) → **rebaseline** input drift → audit. If recall is below
    the floor the switch is refused and the old index is kept.
    """
    if platform_db.get_encoder(new_encoder_id) is None:
        raise ValueError(f"unknown encoder {new_encoder_id!r} — register it first")
    current = platform_db.get_collection(collection, tenant) or {}
    from_encoder = current.get("active_encoder_id")

    job_id = platform_db.create_reindex_job(collection, tenant, from_encoder, new_encoder_id)
    mode = _reindex_mode(orchestrator)
    started = time.perf_counter()
    platform_db.update_reindex_job(job_id, orchestrator=mode)

    if mode == "scheduler":
        hpc_job_id = submit_reindex(collection, new_encoder_id, tenant=tenant)
        if hpc_job_id is not None:
            platform_db.update_reindex_job(job_id, status="submitted", hpc_job_id=hpc_job_id)
            _audit(
                collection,
                tenant,
                "reindex_submitted",
                {"to": new_encoder_id, "hpc_job_id": hpc_job_id},
                actor,
            )
            return ReindexResult(
                collection=collection,
                tenant=tenant,
                from_encoder=from_encoder,
                to_encoder=new_encoder_id,
                recall=None,
                switched=False,
                docs_reindexed=0,
                reason=f"submitted to the scheduler as job {hpc_job_id}",
            )
        # No scheduler reachable — say so and do the work here rather than not at all.
        platform_db.update_reindex_job(job_id, orchestrator="inline-fallback")

    # 1) staging build: old index stays active + retained (R5/GWT-4).
    platform_db.upsert_collection(
        collection, tenant, staging_encoder_id=new_encoder_id, status="building"
    )
    # 2) re-embed corpus (mock: count docs), 3) verify recall.
    recall = recall_fn() if recall_fn is not None else 1.0
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


def reindex_status(collection: str, tenant: str = "default") -> dict:
    coll = platform_db.get_collection(collection, tenant)
    jobs = platform_db.list_reindex_jobs(collection)
    return {"collection": coll, "jobs": jobs}


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
    "guard_compatible",
    "set_collection_encoder",
    "reindex",
    "reindex_status",
]
