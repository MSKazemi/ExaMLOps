"""B5 — Vector DB & embedding store behind a VectorStore seam (ADR 0020).

Engine-agnostic vector store consumed by B3 (cache), B4 (RAG), A3 (feature/embedding
store). The production default is **pgvector** (Postgres), with Qdrant/Milvus for scale;
the **fallback** is a persistent SQLite-backed store (`SqliteVectorStore`) that computes
distances in Python — so collections, dim-enforcement, filtered search, tenant isolation,
and reindex all work with no external service.

Collections declare a fixed dimensionality + distance metric; upserts with the wrong dim
are rejected (R2). Collections are isolated per tenant/project (D6, R4). Index build cost +
search latency are recorded to `platform_db.vector_metrics` (R7).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from examlops.data import get_db, init_db

_METRICS = ("cosine", "l2", "dot")


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


class CollectionNotFound(KeyError):
    """No such collection for this tenant."""


@dataclass
class VecItem:
    id: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


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
    def reindex(self, coll: str, tenant: str) -> None: ...


# ── SQLite-backed store (default fallback) ────────────────────────────────────


class SqliteVectorStore:
    """Persistent, dependency-free vector store (the pgvector-less fallback)."""

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

    def create_collection(
        self,
        name: str,
        dim: int,
        metric: str = "cosine",
        tenant: str = "default",
        encoder_id: str | None = None,
    ) -> None:
        """Declare a collection. ``encoder_id`` stamps which encoder produced its vectors."""
        if metric not in _METRICS:
            raise ValueError(f"metric '{metric}' not in {_METRICS}")
        with get_db() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO vector_collections (name, tenant, dim, metric, encoder_id)
                   VALUES (?,?,?,?,?)""",
                (name, tenant, dim, metric, encoder_id),
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
        with get_db() as conn:
            for it in items:
                conn.execute(
                    """INSERT INTO vector_items (collection, tenant, item_id, vector_json, metadata_json)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(collection, tenant, item_id) DO UPDATE SET
                           vector_json=excluded.vector_json, metadata_json=excluded.metadata_json""",
                    (coll, tenant, it.id, json.dumps(it.vector), json.dumps(it.metadata)),
                )
        self._metric(coll, tenant, "upsert", (time.time() - t0) * 1000, len(items))

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
        dim, metric = int(meta["dim"]), meta["metric"]
        if len(vector) != dim:
            raise DimensionMismatch(f"query vector dim {len(vector)} != collection dim {dim}")
        # Queried with the wrong encoder, this returns confident, ranked, meaningless results —
        # the failure mode a dimension check cannot see.
        self._check_encoder(meta, encoder_id, coll)
        t0 = time.time()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT item_id, vector_json, metadata_json FROM vector_items "
                "WHERE collection=? AND tenant=?",
                (coll, tenant),
            ).fetchall()
        hits: list[Hit] = []
        for r in rows:
            md = json.loads(r["metadata_json"] or "{}")
            if flt and not all(md.get(fk) == fv for fk, fv in flt.items()):
                continue  # metadata filter (R3)
            v = json.loads(r["vector_json"])
            hits.append(Hit(r["item_id"], _score(metric, vector, v), md))
        hits.sort(key=lambda h: h.score, reverse=True)
        self._metric(coll, tenant, "search", (time.time() - t0) * 1000, len(hits))
        return hits[:k]

    def reindex(self, coll: str, tenant: str = "default") -> None:
        # Blue-green no-op for the SQLite store: items are the index, so recall is preserved.
        meta = self._collection(coll, tenant)
        self._metric(coll, tenant, "reindex", 0.0, self.count(coll, tenant))
        _ = meta

    def count(self, coll: str, tenant: str = "default") -> int:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM vector_items WHERE collection=? AND tenant=?",
                (coll, tenant),
            ).fetchone()
        return int(row["n"])

    def stats(self, coll: str, tenant: str = "default") -> dict[str, Any]:
        meta = self._collection(coll, tenant)
        return {
            "collection": coll,
            "tenant": tenant,
            "dim": int(meta["dim"]),
            "metric": meta["metric"],
            "count": self.count(coll, tenant),
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


# ── pgvector store (lazy; production default) ─────────────────────────────────


class PgVectorStore:  # pragma: no cover - needs Postgres+pgvector
    """Postgres + pgvector store — lazily connects; raises a clear error if unavailable."""

    name = "pgvector"

    def __init__(self, dsn: str | None = None) -> None:
        import os

        self.dsn = dsn or os.getenv("EXAMLOPS_PGVECTOR_DSN")
        if not self.dsn:
            raise RuntimeError(
                "pgvector store needs EXAMLOPS_PGVECTOR_DSN; use the 'sqlite' backend for local dev"
            )

    def _conn(self):
        try:
            import psycopg  # type: ignore
        except Exception as exc:
            raise RuntimeError("psycopg not installed; pip install examlops[vector]") from exc
        return psycopg.connect(self.dsn)

    # `encoder_id` is carried here too, unused, so the two stores stay one interface. A seam
    # whose implementations take different arguments is not a seam — the day pgvector is
    # implemented, a caller that stamps its encoder would silently stop being checked.
    def create_collection(self, name, dim, metric="cosine", tenant="default", encoder_id=None):
        raise NotImplementedError("PgVectorStore is provisioned via the Helm chart / migrations")

    def upsert(self, coll, items, tenant="default", encoder_id=None):
        raise NotImplementedError

    def search(self, coll, vector, k=5, flt=None, tenant="default", encoder_id=None):
        raise NotImplementedError

    def reindex(self, coll, tenant="default"):
        raise NotImplementedError


_STORES: dict[str, Any] = {"sqlite": SqliteVectorStore, "pgvector": PgVectorStore}


def select_store(name: str | None = None) -> VectorStore:
    """Select the vector store from the arg or ``EXAMLOPS_VECTOR_BACKEND`` (default sqlite)."""
    import os

    chosen = (name or os.getenv("EXAMLOPS_VECTOR_BACKEND") or "sqlite").lower()
    cls = _STORES.get(chosen, SqliteVectorStore)
    return cls()
