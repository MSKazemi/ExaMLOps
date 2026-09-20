"""Per-inference input-embedding samples in the vector store (ADR 0020 clause 4, drift half).

``input_snapshots`` keeps three scalars per inference (norm, mean, std) — enough to notice that
the input distribution moved, never enough to say *how*, because a 384-dim vector cannot be
rebuilt from them. This module keeps a **bounded, sampled** set of the real vectors in the
:class:`examlops.vector_store.VectorStore` seam so drift can be asked nearest-neighbour questions
against a baseline snapshot.

Bounds, all deliberate:

* **Sampling.** Off by default (``EXAMLOPS_DRIFT_EMBEDDING_SAMPLE_RATE=0``): a raw input embedding
  can encode what the input said, so storing them is an operator decision, not a side effect. The
  decision is a hash of ``(model, job_id)`` — deterministic, so a redelivered event makes the same
  choice and the tests need no RNG.
* **Ring buffer.** At most ``EXAMLOPS_DRIFT_EMBEDDING_CAP`` (default 500) vectors per model; the
  oldest are evicted after each write. Evictions are counted, never silent.
* **Content-addressed id.** ``sha256(model, alias, canonical vector)``: the same input to the same
  model alias is one row, however often it is redelivered.

Nothing here is on the reply path: the Dataplane bus bridge calls it from the telemetry worker
thread (or the ``serving.inference_telemetry`` consumer), and a failure raises to *that* caller,
which counts it. Loss counters for the consumer side live in :func:`stats`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import time
from typing import Any

from examlops.vector_store import CollectionNotFound, VecItem, VectorStore, select_store

logger = logging.getLogger(__name__)

RATE_ENV = "EXAMLOPS_DRIFT_EMBEDDING_SAMPLE_RATE"
CAP_ENV = "EXAMLOPS_DRIFT_EMBEDDING_CAP"
DEFAULT_CAP = 500
MAX_CAP = 100_000
DEFAULT_MIN_SIMILARITY = 0.8
BASELINE_ID = "baseline"
_TENANT = "default"

_lock = threading.Lock()
_stats = {"stored": 0, "evicted": 0, "failed": 0}


def stats() -> dict[str, int]:
    """Process-local counters: samples stored, ring evictions, failed writes."""
    with _lock:
        return dict(_stats)


def _bump(key: str, n: int = 1) -> None:
    with _lock:
        _stats[key] += n


def note_failure() -> None:
    """Count a lost sample. Called by whoever caught the exception (bridge, consumer)."""
    _bump("failed")


def sample_rate() -> float:
    """Fraction of inferences sampled, in [0, 1]. Unparseable or non-positive ⇒ 0 (off); above 1 ⇒ 1."""
    raw = os.getenv(RATE_ENV, "").strip()
    if not raw:
        return 0.0
    try:
        rate = float(raw)
    except ValueError:
        return 0.0
    return rate if 0.0 < rate <= 1.0 else (1.0 if rate > 1.0 else 0.0)


def ring_cap() -> int:
    """Per-model vector cap; clamped to ``[1, MAX_CAP]``, default :data:`DEFAULT_CAP`."""
    raw = os.getenv(CAP_ENV, "").strip()
    try:
        cap = int(raw) if raw else DEFAULT_CAP
    except ValueError:
        cap = DEFAULT_CAP
    return max(1, min(cap, MAX_CAP))


def samples_collection(model: str) -> str:
    return f"drift.embeddings.{model}"


def baseline_collection(model: str) -> str:
    return f"drift.baseline.{model}"


def should_sample(model: str, job_id: str | None, rate: float | None = None) -> bool:
    """Deterministic keep/skip for one inference: a hash of ``(model, job_id)`` below ``rate``."""
    r = sample_rate() if rate is None else rate
    if r <= 0.0:
        return False
    if r >= 1.0:
        return True
    digest = hashlib.sha256(f"{model}\x00{job_id or ''}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < r


def _clean(vector: Any) -> list[float]:
    vec = [float(x) for x in vector]
    if not vec:
        raise ValueError("empty embedding")
    if not all(math.isfinite(x) for x in vec):
        raise ValueError("embedding contains NaN or infinity")
    return vec


def sample_id(model: str, alias: str, vector: list[float]) -> str:
    """Content-addressed id: the same vector for the same model alias is the same row."""
    canon = json.dumps([model, alias, [repr(x) for x in vector]], separators=(",", ":"))
    return "emb-" + hashlib.sha256(canon.encode()).hexdigest()[:32]


def _ensure(store: VectorStore, coll: str, dim: int) -> None:
    store.create_collection(coll, dim, "cosine", _TENANT)


def record_sample(
    model: str,
    alias: str,
    version: str | None,
    job_id: str | None,
    vector: Any,
    *,
    ts: float | None = None,
    store: VectorStore | None = None,
) -> str:
    """Upsert one embedding into the model's ring buffer and trim it to the cap.

    Does **not** sample — the caller decides (:func:`should_sample`) so a value that already
    crossed the event bus is stored, not re-rolled. Raises on any failure (dimension change,
    store unreachable); the caller counts it. Returns the content-addressed id.
    """
    vec = _clean(vector)
    st = store or select_store()
    coll = samples_collection(model)
    _ensure(st, coll, len(vec))
    sid = sample_id(model, alias, vec)
    meta = {
        "model": model,
        "alias": alias,
        "version": version,
        "job_id": job_id,
        "ts": float(ts if ts is not None else time.time()),
    }
    st.upsert(coll, [VecItem(sid, vec, meta)], _TENANT)
    evicted = st.trim(coll, _TENANT, ring_cap())
    _bump("stored")
    if evicted:
        _bump("evicted", evicted)
    return sid


def maybe_sample(
    model: str,
    alias: str,
    version: str | None,
    job_id: str | None,
    vector: Any,
) -> dict[str, Any] | None:
    """The sampling decision plus the payload that crosses the event bus, or ``None`` to skip.

    Cheap and store-free, so the bridge can run it before deciding *where* the write happens.
    """
    if not vector or not should_sample(model, job_id):
        return None
    return {"vector": _clean(vector), "version": version, "ts": time.time()}


def set_baseline(model: str, *, store: VectorStore | None = None) -> dict[str, Any]:
    """Freeze the current samples' centroid as the baseline snapshot for ``model``.

    Raises ``ValueError`` if there are no samples (nothing to baseline).
    """
    st = store or select_store()
    try:
        items = st.scan(samples_collection(model), _TENANT, ring_cap())
    except CollectionNotFound as exc:
        raise ValueError(f"no embedding samples recorded for {model}") from exc
    if not items:
        raise ValueError(f"no embedding samples recorded for {model}")
    dim = len(items[0].vector)
    centroid = [sum(it.vector[i] for it in items) / len(items) for i in range(dim)]
    if not any(centroid):
        raise ValueError("samples average to the zero vector; no direction to baseline")
    coll = baseline_collection(model)
    _ensure(st, coll, dim)
    st.upsert(
        coll,
        [VecItem(BASELINE_ID, centroid, {"model": model, "n": len(items), "set_at": time.time()})],
        _TENANT,
    )
    return {"model": model, "n": len(items), "dim": dim}


def embedding_drift(
    model: str,
    *,
    k: int = 5,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    store: VectorStore | None = None,
) -> dict[str, Any]:
    """Nearest-neighbour drift of the sampled embeddings against the baseline snapshot.

    Every sample is scored (cosine) against the baseline centroid through the store's ``search``.
    ``recent_mean_similarity`` is the mean over the newer half of the samples by timestamp;
    ``drifted`` means it fell below ``min_similarity`` (an operator-tuned threshold, not a
    calibrated one). ``nearest``/``farthest`` list the ``k`` samples closest to / furthest from
    the baseline so an operator can inspect the outliers by job id.
    """
    st = store or select_store()
    out: dict[str, Any] = {
        "model": model,
        "status": "ok",
        "min_similarity": min_similarity,
        "samples": 0,
    }
    try:
        base = st.scan(baseline_collection(model), _TENANT, 1)
    except CollectionNotFound:
        base = []
    if not base:
        return {**out, "status": "no_baseline"}
    try:
        n = int(st.stats(samples_collection(model), _TENANT)["count"])
    except CollectionNotFound:
        n = 0
    if n == 0:
        return {**out, "status": "no_samples", "baseline_n": base[0].metadata.get("n")}
    hits = st.search(samples_collection(model), base[0].vector, n, None, _TENANT)
    sims = [h.score for h in hits]
    by_ts = sorted(hits, key=lambda h: float(h.metadata.get("ts") or 0.0))
    recent = by_ts[len(by_ts) // 2 :] or by_ts
    recent_mean = sum(h.score for h in recent) / len(recent)

    def _row(h: Any) -> dict[str, Any]:
        m = h.metadata
        return {
            "id": h.id,
            "similarity": round(h.score, 6),
            "job_id": m.get("job_id"),
            "version": m.get("version"),
            "ts": m.get("ts"),
        }

    out.update(
        samples=len(hits),
        baseline_n=base[0].metadata.get("n"),
        mean_similarity=round(sum(sims) / len(sims), 6),
        min_sample_similarity=round(min(sims), 6),
        recent_mean_similarity=round(recent_mean, 6),
        drifted=recent_mean < min_similarity,
        nearest=[_row(h) for h in hits[: max(0, k)]],
        farthest=[_row(h) for h in reversed(hits[-max(0, k) :])] if k > 0 else [],
    )
    return out
