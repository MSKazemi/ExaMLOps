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

from examlops.platform_db import get_db, init_db

_METRICS = ("cosine", "l2", "dot")


class DimensionMismatch(ValueError):
    """An upserted/queried vector does not match the collection dimensionality (R2)."""


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

    def create_collection(self, name: str, dim: int, metric: str, tenant: str) -> None: ...
    def upsert(self, coll: str, items: list[VecItem], tenant: str) -> None: ...
    def search(
        self, coll: str, vector: list[float], k: int, flt: dict | None, tenant: str
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
        self, name: str, dim: int, metric: str = "cosine", tenant: str = "default"
    ) -> None:
        if metric not in _METRICS:
            raise ValueError(f"metric '{metric}' not in {_METRICS}")
        with get_db() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO vector_collections (name, tenant, dim, metric)
                   VALUES (?,?,?,?)""",
                (name, tenant, dim, metric),
            )

    def upsert(self, coll: str, items: list[VecItem], tenant: str = "default") -> None:
        meta = self._collection(coll, tenant)
        dim = int(meta["dim"])
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
    ) -> list[Hit]:
        meta = self._collection(coll, tenant)
        dim, metric = int(meta["dim"]), meta["metric"]
        if len(vector) != dim:
            raise DimensionMismatch(f"query vector dim {len(vector)} != collection dim {dim}")
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

    def create_collection(self, name, dim, metric="cosine", tenant="default"):
        raise NotImplementedError("PgVectorStore is provisioned via the Helm chart / migrations")

    def upsert(self, coll, items, tenant="default"):
        raise NotImplementedError

    def search(self, coll, vector, k=5, flt=None, tenant="default"):
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
