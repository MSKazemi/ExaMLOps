"""B5 — Vector DB & embedding store behind a VectorStore seam (ADR 0020).

Engine-agnostic vector store consumed by B3 (cache), B4 (RAG), A3 (feature/embedding
store). The production default is **pgvector** (Postgres, :mod:`.pgvector`), with Qdrant/Milvus
for scale; the **fallback** is a persistent SQLite-backed store (`SqliteVectorStore`) that
computes distances in Python — so collections, dim-enforcement, filtered search, hybrid search,
tenant isolation, and reindex all work with no external service.

Collections declare a fixed dimensionality + distance metric + ANN index configuration
(:mod:`.index`); upserts with the wrong dim are rejected (R2). Search is dense, sparse
(BM25, :mod:`.sparse`) or **hybrid** — both channels fused by rank (:mod:`.fusion`). Collections
are isolated per tenant/project (D6, R4). Index build cost + search latency are recorded to
`platform_db.vector_metrics` (R7).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from examlops.data import get_db, init_db
from examlops.vector_store.fusion import FUSIONS, RRF_K, fuse, ranked
from examlops.vector_store.index import INDEX_TYPES, IndexConfig, IndexConfigError
from examlops.vector_store.sparse import bm25_scores

_METRICS = ("cosine", "l2", "dot")
SEARCH_MODES = ("dense", "sparse", "hybrid")

__all__ = [
    "FUSIONS",
    "INDEX_TYPES",
    "SEARCH_MODES",
    "CollectionNotFound",
    "DimensionMismatch",
    "EncoderMismatch",
    "Hit",
    "IndexConfig",
    "IndexConfigError",
    "PgVectorStore",
    "QdrantVectorStore",
    "SchemaConflict",
    "SqliteVectorStore",
    "VecItem",
    "VectorStore",
    "hybrid_candidates",
    "select_store",
]


class DimensionMismatch(ValueError):
    """An upserted/queried vector does not match the collection dimensionality (R2)."""


class EncoderMismatch(ValueError):
    """A vector produced by one encoder met a collection built by another (ADR 0043 clause 2).

    Separate from :class:`DimensionMismatch` because the two failures are not alike. A wrong
    dimension cannot be scored at all, so it announces itself. Two encoders of the *same*
    dimension produce vectors that score perfectly happily against each other and mean nothing —
    the search returns confident, ranked, wrong results, and nothing downstream can tell. That
    silent corruption is the failure ADR 0043 exists to prevent.
    """


class SchemaConflict(ValueError):
    """Re-declaring a non-empty collection with a different dim, metric or encoder.

    ``create_collection`` used to be ``INSERT OR REPLACE``: re-running ``exa vector create`` with
    another ``--dim`` rewrote the schema under the stored vectors, and ``zip`` then scored every
    old vector against the new query over a truncated prefix — ranked, plausible, and wrong. The
    same statement also erased the encoder stamp whenever a later call named no encoder, which
    switched the ADR 0043 guard off without a trace. A schema change on data is a reindex, never
    a quiet overwrite.
    """


class CollectionNotFound(KeyError):
    """No such collection for this tenant."""

    def __str__(self) -> str:  # KeyError's str() is repr(): it would print the message quoted
        return str(self.args[0]) if self.args else "collection not found"


@dataclass
class VecItem:
    id: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)
    # The text the sparse channel indexes. Optional: when absent, a string ``metadata["text"]`` is
    # used, which is where B4 RAG has always kept its chunk text — so every knowledge base ingested
    # before this field existed is hybrid-searchable without re-ingestion.
    text: str | None = None


@dataclass
class Hit:
    id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    # Per-channel evidence for a hybrid hit: ``dense``/``sparse`` raw scores and ``*_rank``. Empty
    # for a plain dense search. Kept so an operator can see *why* a chunk ranked where it did — a
    # fused score alone cannot say whether the embedding or the keyword carried it.
    channels: dict[str, float] = field(default_factory=dict)


def _item_text(item: VecItem) -> str | None:
    if item.text is not None:
        return item.text
    candidate = item.metadata.get("text") if item.metadata else None
    return candidate if isinstance(candidate, str) else None


def hybrid_candidates(k: int, candidates: int | None = None) -> int:
    """How many results each channel contributes before fusion.

    Fusion can only promote what a channel returned, so each channel must over-fetch: a document
    ranked 12th by the embedding and 1st by BM25 is exactly the hit hybrid search exists for, and
    a per-channel cut at ``k`` would drop it. ``max(4k, 50)`` follows the common practice of
    fusing top-50..100 lists; an explicit ``candidates`` wins, but never below ``k``.
    """
    if candidates is not None:
        return max(int(candidates), k)
    return max(4 * k, 50)


# ── distance ──────────────────────────────────────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _l2(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _score(metric: str, q: list[float], v: list[float]) -> float:
    if metric == "l2":
        return -_l2(q, v)  # smaller distance = higher score → negate for a max-sort
    if metric == "dot":
        return _dot(q, v)
    return _cosine(q, v)


@runtime_checkable
class VectorStore(Protocol):
    name: str

    def create_collection(
        self,
        name: str,
        dim: int,
        metric: str,
        tenant: str,
        encoder_id: str | None = ...,
        index: IndexConfig | None = ...,
    ) -> None: ...
    def upsert(
        self, coll: str, items: list[VecItem], tenant: str, encoder_id: str | None = ...
    ) -> None: ...
    def search(
        self,
        coll: str,
        vector: list[float],
        k: int,
        flt: dict | None,
        tenant: str,
        encoder_id: str | None = ...,
    ) -> list[Hit]: ...
    def sparse_search(
        self, coll: str, text: str, k: int, flt: dict | None, tenant: str
    ) -> list[Hit]: ...
    def hybrid_search(
        self,
        coll: str,
        vector: list[float],
        text: str,
        k: int,
        flt: dict | None,
        tenant: str,
        encoder_id: str | None = ...,
        *,
        fusion: str = ...,
        alpha: float = ...,
        rrf_k: int = ...,
        candidates: int | None = ...,
    ) -> list[Hit]: ...
    def reindex(self, coll: str, tenant: str, index: IndexConfig | None = ...) -> None: ...
    def drop_collection(self, coll: str, tenant: str) -> int: ...
    def trim(self, coll: str, tenant: str, keep: int) -> int: ...
    def scan(self, coll: str, tenant: str, limit: int) -> list[VecItem]: ...
    def stats(self, coll: str, tenant: str) -> dict[str, Any]: ...


def _fused_hits(
    dense: list[Hit],
    sparse: list[Hit],
    k: int,
    fusion: str,
    alpha: float,
    rrf_k: int,
) -> list[Hit]:
    """Fuse two channel result lists into one ranked hit list. Shared by every store.

    Lives outside the stores so the SQLite fallback and pgvector cannot drift apart on the one
    part of hybrid search that has a right answer: given the same two channel rankings, both
    backends return the same fused order.
    """
    if fusion not in FUSIONS:
        raise ValueError(f"fusion '{fusion}' not in {FUSIONS}")
    d_scores = {h.id: h.score for h in dense}
    s_scores = {h.id: h.score for h in sparse}
    fused = fuse(d_scores, s_scores, method=fusion, alpha=alpha, rrf_k=rrf_k)
    meta = {h.id: h.metadata for h in sparse}
    meta.update({h.id: h.metadata for h in dense})
    d_rank = {d: i for i, d in enumerate(ranked(d_scores), start=1)}
    s_rank = {d: i for i, d in enumerate(ranked(s_scores), start=1)}
    out: list[Hit] = []
    for doc_id in ranked(fused)[:k]:
        channels: dict[str, float] = {}
        if doc_id in d_scores:
            channels["dense"] = d_scores[doc_id]
            channels["dense_rank"] = float(d_rank[doc_id])
        if doc_id in s_scores:
            channels["sparse"] = s_scores[doc_id]
            channels["sparse_rank"] = float(s_rank[doc_id])
        out.append(Hit(doc_id, fused[doc_id], meta.get(doc_id, {}), channels))
    return out


# ── SQLite-backed store (default fallback) ────────────────────────────────────


class SqliteVectorStore:
    """Persistent, dependency-free vector store (the pgvector-less fallback).

    Every search is an exact scan in Python, so recall is 1.0 whatever index the collection
    declares — the declared index is recorded (and honoured the day the collection moves to
    pgvector) but builds nothing here. Cost is O(N·d) per query; use pgvector past ~10⁵ items.
    """

    name = "sqlite"

    def __init__(self) -> None:
        init_db()

    def _collection(self, name: str, tenant: str) -> dict[str, Any]:
        with get_db() as conn:
            row = conn.execute(
                "SELECT * FROM vector_collections WHERE name=? AND tenant=?", (name, tenant)
            ).fetchone()
        if row is None:
            raise CollectionNotFound(f"collection '{name}' not found for tenant '{tenant}'")
        return dict(row)

    @staticmethod
    def _index_of(meta: dict[str, Any]) -> IndexConfig:
        return IndexConfig.from_row(meta.get("index_type"), meta.get("index_params"))

    def create_collection(
        self,
        name: str,
        dim: int,
        metric: str = "cosine",
        tenant: str = "default",
        encoder_id: str | None = None,
        index: IndexConfig | None = None,
    ) -> None:
        """Declare a collection. Idempotent for an identical schema.

        ``encoder_id`` stamps which encoder produced its vectors; ``index`` is its ANN index
        configuration (flat when omitted). Re-declaring an existing collection keeps its stamp
        and index unless new ones are given, and refuses a dim/metric/encoder change while it
        holds items (:class:`SchemaConflict`) — see that class for why.
        """
        if metric not in _METRICS:
            raise ValueError(f"metric '{metric}' not in {_METRICS}")
        if int(dim) < 1:
            raise ValueError(f"dim must be >= 1, got {dim}")
        if index is not None:
            index._validate(int(dim))
        try:
            existing: dict[str, Any] | None = self._collection(name, tenant)
        except CollectionNotFound:
            existing = None
        if existing is not None:
            stamped = existing.get("encoder_id")
            changed = []
            if int(existing["dim"]) != int(dim):
                changed.append(f"dim {existing['dim']} → {dim}")
            if existing["metric"] != metric:
                changed.append(f"metric {existing['metric']} → {metric}")
            if encoder_id and stamped and stamped != encoder_id:
                changed.append(f"encoder {stamped} → {encoder_id}")
            if changed and self.count(name, tenant):
                raise SchemaConflict(
                    f"collection '{name}' (tenant '{tenant}') holds {self.count(name, tenant)} "
                    f"items; refusing to change {', '.join(changed)} under them. Drop it "
                    f"(exa vector drop {name}) or, for an encoder change, reindex it "
                    f"(exa embedding reindex {name} <encoder>)."
                )
            encoder_id = encoder_id or stamped
            if index is None:
                index = self._index_of(existing)
        cfg = index or IndexConfig()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO vector_collections
                       (name, tenant, dim, metric, encoder_id, index_type, index_params)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(name, tenant) DO UPDATE SET
                       dim=excluded.dim, metric=excluded.metric, encoder_id=excluded.encoder_id,
                       index_type=excluded.index_type, index_params=excluded.index_params""",
                (name, tenant, int(dim), metric, encoder_id, cfg.type, cfg.to_json()),
            )

    @staticmethod
    def _check_encoder(meta: dict, encoder_id: str | None, coll: str) -> None:
        """Refuse a cross-encoder operation (ADR 0043 clause 2). Never guesses.

        Both "the collection is unstamped" and "the caller named no encoder" pass, and neither is
        a statement that the vectors are compatible — it is the absence of one. This mirrors the
        `measured` flag on SLOs and `usage_reported_rate` on eval runs: unverified must not be
        recorded, or reported, as verified. A stamped collection meeting a named encoder is the
        one case where a real comparison exists, and that is the case this refuses.

        Delegates to `examlops.embeddings.guard_compatible`, which the ADR found had "nothing to
        guard" — it was written for exactly this check and reached by no caller.
        """
        stamped = meta.get("encoder_id")
        if not stamped or not encoder_id:
            return
        if stamped != encoder_id:
            # B5's hook (ADR 0043 clause 4): leave a trail the operator can act on. It records a
            # recommendation and never starts a reindex — a search that quietly re-embedded a
            # large corpus would turn one query into an unbounded job nobody asked for.
            try:
                from examlops.embeddings import recommend_reindex

                recommend_reindex(
                    coll,
                    str(meta.get("tenant") or "default"),
                    from_encoder=str(stamped),
                    to_encoder=str(encoder_id),
                )
            except Exception:  # noqa: BLE001 - the refusal below is what matters
                pass
            try:
                from examlops.embeddings import guard_compatible

                guard_compatible(str(stamped), str(encoder_id))
            except Exception as exc:
                raise EncoderMismatch(
                    f"collection '{coll}' was built with encoder {stamped!r} but the vector "
                    f"comes from {encoder_id!r} — same-dimension vectors from different encoders "
                    "score against each other and mean nothing. Reindex it: "
                    f"exa embedding reindex {coll} {encoder_id}"
                ) from exc

    def upsert(
        self,
        coll: str,
        items: list[VecItem],
        tenant: str = "default",
        encoder_id: str | None = None,
    ) -> None:
        meta = self._collection(coll, tenant)
        dim = int(meta["dim"])
        self._check_encoder(meta, encoder_id, coll)
        t0 = time.time()
        for it in items:
            if len(it.vector) != dim:
                raise DimensionMismatch(
                    f"vector '{it.id}' has dim {len(it.vector)}, collection expects {dim}"
                )
            if not all(math.isfinite(float(x)) for x in it.vector):
                # NaN poisons every score it touches (NaN comparisons are all False, so a NaN
                # item sorts arbitrarily) and pgvector refuses it outright — refuse it here too,
                # so the fallback and the production store accept the same data.
                raise ValueError(f"vector '{it.id}' contains NaN or infinity")
        with get_db() as conn:
            for it in items:
                conn.execute(
                    """INSERT INTO vector_items
                           (collection, tenant, item_id, vector_json, metadata_json, text)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(collection, tenant, item_id) DO UPDATE SET
                           vector_json=excluded.vector_json, metadata_json=excluded.metadata_json,
                           text=excluded.text""",
                    (
                        coll,
                        tenant,
                        it.id,
                        json.dumps(it.vector),
                        json.dumps(it.metadata),
                        _item_text(it),
                    ),
                )
        self._metric(coll, tenant, "upsert", (time.time() - t0) * 1000, len(items))

    def _rows(self, coll: str, tenant: str, flt: dict | None) -> list[tuple[str, Any, dict, Any]]:
        """``(item_id, vector_json, metadata, text)`` for every item passing the filter.

        The filter is applied before scoring (pre-filtering), so a filtered top-k is exact: it is
        the best k of the matching items, never "the best k overall, minus the ones filtered out".
        """
        with get_db() as conn:
            rows = conn.execute(
                "SELECT item_id, vector_json, metadata_json, text FROM vector_items "
                "WHERE collection=? AND tenant=?",
                (coll, tenant),
            ).fetchall()
        out = []
        for r in rows:
            md = json.loads(r["metadata_json"] or "{}")
            if flt and not all(md.get(fk) == fv for fk, fv in flt.items()):
                continue  # metadata filter (R3)
            text = r["text"]
            if text is None and isinstance(md.get("text"), str):
                text = md["text"]  # rows written before the `text` column existed
            out.append((r["item_id"], r["vector_json"], md, text))
        return out

    def _dense(
        self, meta: dict[str, Any], vector: list[float], rows: list[tuple[str, Any, dict, Any]]
    ) -> list[Hit]:
        dim, metric = int(meta["dim"]), meta["metric"]
        if len(vector) != dim:
            raise DimensionMismatch(f"query vector dim {len(vector)} != collection dim {dim}")
        hits = [
            Hit(item_id, _score(metric, vector, json.loads(v)), md) for item_id, v, md, _ in rows
        ]
        hits.sort(key=lambda h: (-h.score, h.id))  # ties by id: same inputs, same order
        return hits

    def search(
        self,
        coll: str,
        vector: list[float],
        k: int = 5,
        flt: dict | None = None,
        tenant: str = "default",
        encoder_id: str | None = None,
    ) -> list[Hit]:
        meta = self._collection(coll, tenant)
        if len(vector) != int(meta["dim"]):
            raise DimensionMismatch(
                f"query vector dim {len(vector)} != collection dim {int(meta['dim'])}"
            )
        # Queried with the wrong encoder, this returns confident, ranked, meaningless results —
        # the failure mode a dimension check cannot see.
        self._check_encoder(meta, encoder_id, coll)
        t0 = time.time()
        hits = self._dense(meta, vector, self._rows(coll, tenant, flt))
        self._metric(coll, tenant, "search", (time.time() - t0) * 1000, len(hits))
        return hits[:k]

    def sparse_search(
        self,
        coll: str,
        text: str,
        k: int = 5,
        flt: dict | None = None,
        tenant: str = "default",
    ) -> list[Hit]:
        """Lexical top-k by BM25 over the item texts. No vector, so no encoder check applies."""
        self._collection(coll, tenant)  # CollectionNotFound for a missing/foreign collection
        t0 = time.time()
        rows = self._rows(coll, tenant, flt)
        scores = bm25_scores(text, ((item_id, t) for item_id, _, _, t in rows))
        md = {item_id: m for item_id, _, m, _ in rows}
        hits = [Hit(d, scores[d], md[d]) for d in ranked(scores)]
        self._metric(coll, tenant, "sparse_search", (time.time() - t0) * 1000, len(hits))
        return hits[:k]

    def hybrid_search(
        self,
        coll: str,
        vector: list[float],
        text: str,
        k: int = 5,
        flt: dict | None = None,
        tenant: str = "default",
        encoder_id: str | None = None,
        *,
        fusion: str = "rrf",
        alpha: float = 0.5,
        rrf_k: int = RRF_K,
        candidates: int | None = None,
    ) -> list[Hit]:
        """Dense + sparse (BM25) search, fused by rank (RRF, default) or convex combination.

        With an empty ``text`` the sparse channel is empty and the result is the dense ranking —
        so a caller can always ask for hybrid, and degrade to dense when it has no query text.
        """
        meta = self._collection(coll, tenant)
        self._check_encoder(meta, encoder_id, coll)
        if fusion not in FUSIONS:
            raise ValueError(f"fusion '{fusion}' not in {FUSIONS}")
        t0 = time.time()
        n = hybrid_candidates(k, candidates)
        rows = self._rows(coll, tenant, flt)  # one read serves both channels
        dense = self._dense(meta, vector, rows)[:n]
        scores = bm25_scores(text, ((item_id, t) for item_id, _, _, t in rows))
        md = {item_id: m for item_id, _, m, _ in rows}
        sparse = [Hit(d, scores[d], md[d]) for d in ranked(scores)][:n]
        hits = _fused_hits(dense, sparse, k, fusion, alpha, rrf_k)
        self._metric(coll, tenant, "hybrid_search", (time.time() - t0) * 1000, len(hits))
        return hits

    def reindex(self, coll: str, tenant: str = "default", index: IndexConfig | None = None) -> None:
        """Rebuild the index; with ``index``, switch the collection to that configuration.

        Blue-green is a no-op for this store — the items are the index and every search is an
        exact scan, so recall is preserved trivially. A new ``index`` is recorded so the
        collection carries it to pgvector.
        """
        meta = self._collection(coll, tenant)
        t0 = time.time()
        if index is not None:
            index._validate(int(meta["dim"]))
            with get_db() as conn:
                conn.execute(
                    "UPDATE vector_collections SET index_type=?, index_params=? "
                    "WHERE name=? AND tenant=?",
                    (index.type, index.to_json(), coll, tenant),
                )
        self._metric(coll, tenant, "reindex", (time.time() - t0) * 1000, self.count(coll, tenant))

    def drop_collection(self, coll: str, tenant: str = "default") -> int:
        """Delete a collection and every item in it. Returns the number of items removed.

        The ``vector_metrics`` history is kept: it is an append-only operations log, and the
        record that a collection existed and what it cost is exactly what an audit of a deletion
        needs afterwards.
        """
        self._collection(coll, tenant)
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM vector_items WHERE collection=? AND tenant=?", (coll, tenant)
            )
            removed = int(cur.rowcount or 0)
            conn.execute("DELETE FROM vector_collections WHERE name=? AND tenant=?", (coll, tenant))
        self._metric(coll, tenant, "drop", 0.0, removed)
        return removed

    def trim(self, coll: str, tenant: str = "default", keep: int = 1000) -> int:
        """Keep only the ``keep`` most recently *inserted* items; return how many were evicted.

        The ring-buffer primitive for collections that must stay bounded (drift embedding
        samples, ADR 0020 clause 4). Oldest first by insertion order.
        """
        self._collection(coll, tenant)
        keep = max(0, int(keep))
        with get_db() as conn:
            cur = conn.execute(
                "DELETE FROM vector_items WHERE collection=? AND tenant=? AND id NOT IN ("
                "SELECT id FROM vector_items WHERE collection=? AND tenant=? "
                "ORDER BY id DESC LIMIT ?)",
                (coll, tenant, coll, tenant, keep),
            )
            evicted = int(cur.rowcount or 0)
        if evicted:
            self._metric(coll, tenant, "trim", 0.0, evicted)
        return evicted

    def scan(self, coll: str, tenant: str = "default", limit: int = 1000) -> list[VecItem]:
        """Up to ``limit`` items, newest insertion first (for centroid/baseline maths)."""
        self._collection(coll, tenant)
        with get_db() as conn:
            rows = conn.execute(
                "SELECT item_id, vector_json, metadata_json, text FROM vector_items "
                "WHERE collection=? AND tenant=? ORDER BY id DESC LIMIT ?",
                (coll, tenant, max(0, int(limit))),
            ).fetchall()
        return [
            VecItem(
                r["item_id"],
                json.loads(r["vector_json"]),
                json.loads(r["metadata_json"] or "{}"),
                r["text"],
            )
            for r in rows
        ]

    def count(self, coll: str, tenant: str = "default") -> int:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM vector_items WHERE collection=? AND tenant=?",
                (coll, tenant),
            ).fetchone()
        return int(row["n"])

    def stats(self, coll: str, tenant: str = "default") -> dict[str, Any]:
        meta = self._collection(coll, tenant)
        idx = self._index_of(meta)
        return {
            "collection": coll,
            "tenant": tenant,
            "dim": int(meta["dim"]),
            "metric": meta["metric"],
            "count": self.count(coll, tenant),
            "encoder_id": meta.get("encoder_id"),
            "index": {"type": idx.type, **idx.params},
            # What actually answers a query on this backend, whatever was declared. Reporting the
            # declared HNSW as if it were serving would overstate latency *and* understate recall.
            "search": "exact",
            "backend": self.name,
        }

    def _metric(
        self, coll: str, tenant: str, operation: str, latency_ms: float, item_count: int
    ) -> None:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO vector_metrics (collection, tenant, operation, latency_ms, item_count)
                   VALUES (?,?,?,?,?)""",
                (coll, tenant, operation, latency_ms, item_count),
            )


# ── pgvector store (production default) + qdrant (scale-out) ──────────────────

from examlops.vector_store.pgvector import PgVectorStore  # noqa: E402  (imports the above)
from examlops.vector_store.qdrant import QdrantVectorStore  # noqa: E402  (imports the above)

_STORES: dict[str, Any] = {
    "sqlite": SqliteVectorStore,
    "pgvector": PgVectorStore,
    "qdrant": QdrantVectorStore,
}


def select_store(name: str | None = None) -> VectorStore:
    """Select the vector store from the arg or ``EXAMLOPS_VECTOR_BACKEND`` (default sqlite).

    An unknown name is an error. It used to fall back to SQLite, so a typo such as
    ``EXAMLOPS_VECTOR_BACKEND=pgvektor`` sent production writes into ``platform.db`` with nothing
    to say so — the one failure a backend selector must never have.
    """
    import os

    chosen = (name or os.getenv("EXAMLOPS_VECTOR_BACKEND") or "sqlite").strip().lower()
    cls = _STORES.get(chosen)
    if cls is None:
        raise ValueError(
            f"unknown vector backend '{chosen}' (EXAMLOPS_VECTOR_BACKEND); choose one of "
            f"{sorted(_STORES)}"
        )
    return cls()
