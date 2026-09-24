"""Qdrant store — the scale-out option of ADR 0020 clause 1.

pgvector is the default and is right up to a few million vectors on the Postgres the platform
already runs. Past that the index stops fitting the box it shares with platform state, and clause
1 names **Qdrant or Milvus** as the way out: a dedicated, horizontally shardable vector service
behind the same :class:`~examlops.vector_store.VectorStore` seam, so nothing above it changes.
This is the Qdrant half. Milvus is deliberately not built — one scale-out engine satisfies the
clause, and a second unused adapter would be a maintenance cost with no caller.

**Layout.** One Qdrant collection per (tenant, logical collection), named
``<namespace>_<sha256(tenant, name)[:24]>`` so no user string ever becomes a server-side
identifier and two ExaMLOps instances can share one Qdrant (``EXAMLOPS_QDRANT_NAMESPACE``).
The schema registry — dim, metric, encoder stamp, index configuration — lives in Qdrant too, as
one point per collection in ``<namespace>_collections``: Qdrant is the system of record for its
own data, exactly as Postgres is for the pgvector store, so a vector deployment does not depend
on ``platform.db`` being reachable to answer a query.

**Point ids.** Qdrant accepts only an unsigned integer or a UUID as a point id, and ExaMLOps item
ids are arbitrary strings (``emb-<sha256…>``, a RAG chunk key, an entity id). Each item is stored
under ``uuid5(item_id)`` — deterministic, so an upsert of the same id still replaces rather than
duplicates — with the original string kept in the payload (``eid``) and returned in every hit.

**Payload.** ``eid`` (the real id), ``text`` (the lexical channel, full-text indexed), ``ts`` (a
monotonic write stamp, float-indexed — what orders :meth:`scan` and bounds :meth:`trim`), and
``meta`` holding the caller's metadata. A metadata filter is an equality match on ``meta.<key>``,
which keeps user keys from ever colliding with the three reserved ones.

**Scores** follow the same convention as the other two stores, so a caller cannot tell the
backends apart by sign or direction: cosine → the cosine similarity Qdrant returns, dot → the
inner product, l2 → ``−distance``. Note that on a **cosine** collection Qdrant normalises vectors
on write, so :meth:`scan` returns unit vectors: direction (and therefore every cosine question)
is preserved, magnitude is not. Use ``l2``/``dot`` for a collection whose magnitudes matter.

**Index.** ``flat`` is ``m = 0`` (Qdrant's own way to disable the HNSW graph, giving an exact
scan) and ``hnsw`` maps onto Qdrant's ``m``/``ef_construct``/``hnsw_ef``. ``ivfflat`` is
**refused**: Qdrant has no IVF index, and quietly serving an HNSW under a collection that asked
for IVF would misreport what answers its queries. :meth:`reindex` changes the graph parameters
and lets Qdrant's optimizers rebuild — online, the old segments keep serving, which is the same
guarantee pgvector gets from ``CREATE INDEX CONCURRENTLY``.

Declaring ``hnsw`` is not the same as having one. Qdrant builds the graph per segment only once
that segment passes ``optimizers_config.indexing_threshold`` (20 MB of vectors by default) and
answers by exact scan until then — deliberately, because below that size a scan is the faster
answer. :meth:`stats` therefore reports ``exact`` with the reason, and how many vectors are in
fact indexed, rather than echoing the declaration back. ``EXAMLOPS_QDRANT_INDEXING_THRESHOLD_KB``
lowers the threshold for a collection that must be served by the graph regardless of size.

**Lexical channel.** Qdrant has no BM25 scorer. The full-text payload index answers *which*
documents contain a query term (server-side, indexed, bounded by ``limit``) and
:func:`~examlops.vector_store.sparse.bm25_scores` ranks that candidate set. Corpus statistics are
therefore computed over the candidates rather than the whole collection, so absolute sparse
scores differ from the SQLite fallback's — as pgvector's ``ts_rank_cd`` scores do. Fusion uses
ranks (RRF) or min-max-normalised scores, so no scorer's scale reaches the fused result.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import re
import time
import uuid
from typing import Any

from examlops.vector_store import (
    FUSIONS,
    RRF_K,
    CollectionNotFound,
    DimensionMismatch,
    Hit,
    IndexConfig,
    IndexConfigError,
    SchemaConflict,
    SqliteVectorStore,
    VecItem,
    _fused_hits,
    _item_text,
    hybrid_candidates,
)
from examlops.vector_store.sparse import bm25_scores, tokenize

# metric → (Qdrant distance name, Qdrant score → our score)
_METRICS: dict[str, str] = {"cosine": "Cosine", "l2": "Euclid", "dot": "Dot"}

# Collection names are derived, never typed, but the namespace comes from the environment and
# becomes part of a server-side identifier — so it is held to a plain identifier.
_NAMESPACE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")

_POINT_NS = uuid.UUID("6f1f0d5e-4d3c-5b2a-9e8f-0a1b2c3d4e5f")
_UPSERT_BATCH = 256
# The lexical channel prefilters with the full-text index; this caps how many of those candidates
# are pulled back for BM25. Well above the `max(4k, 50)` per-channel budget, small enough that a
# one-word query against a million-document collection cannot drag the whole corpus into memory.
_SPARSE_CANDIDATE_CAP = 2000

_counter = itertools.count()


class QdrantUnavailable(RuntimeError):
    """The Qdrant backend was selected but cannot be used, with what to do about it."""


def _now() -> float:
    """A strictly increasing write stamp.

    ``time.time()`` alone repeats inside one clock tick, and two items sharing a stamp cannot be
    ordered — which would make :meth:`trim` evict an arbitrary one of them. The counter breaks
    ties within a process; the float keeps the value meaningful as a timestamp.
    """
    return time.time() + (next(_counter) % 1000) * 1e-9


def _collection_name(namespace: str, name: str, tenant: str) -> str:
    digest = hashlib.sha256(f"{tenant}\x00{name}".encode()).hexdigest()[:24]
    return f"{namespace}_{digest}"


def _point_id(item_id: str) -> str:
    return str(uuid.uuid5(_POINT_NS, item_id))


def _record_metric(
    coll: str, tenant: str, operation: str, latency_ms: float, item_count: int
) -> None:
    """Operations metrics land in ``platform.db``, the same table every backend writes.

    ``exa slo export-metrics`` then sees all three backends through one exporter. A metrics write
    must never fail a vector operation, so every failure here is swallowed.
    """
    try:
        from examlops.data import get_db, init_db

        init_db()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO vector_metrics (collection, tenant, operation, latency_ms, item_count)
                   VALUES (?,?,?,?,?)""",
                (coll, tenant, operation, latency_ms, item_count),
            )
    except Exception:  # noqa: BLE001 - telemetry must not break the operation it measures
        pass


class QdrantVectorStore:
    """Qdrant-backed vector store. Needs ``EXAMLOPS_QDRANT_URL`` and ``examlops[qdrant]``.

    ``client``/``models`` exist so a test can drive the adapter against an in-process double and
    so a caller that already holds a configured ``QdrantClient`` (an embedded one, a client with
    custom auth) can hand it over rather than have a second one built from the environment.
    """

    name = "qdrant"

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        *,
        namespace: str | None = None,
        client: Any | None = None,
        models: Any | None = None,
    ) -> None:
        self.namespace = namespace or os.getenv("EXAMLOPS_QDRANT_NAMESPACE") or "exv"
        if not _NAMESPACE_RE.match(self.namespace):
            raise ValueError(
                f"EXAMLOPS_QDRANT_NAMESPACE {self.namespace!r} is not a plain identifier "
                "(letters, digits, underscore; at most 32 characters)"
            )
        self.url = url or os.getenv("EXAMLOPS_QDRANT_URL")
        if client is None:
            if not self.url:
                raise QdrantUnavailable(
                    "the qdrant backend needs EXAMLOPS_QDRANT_URL (e.g. http://localhost:6333); "
                    "use EXAMLOPS_VECTOR_BACKEND=sqlite for local dev, or pgvector for a "
                    "Postgres-sized collection"
                )
            try:
                from qdrant_client import QdrantClient
                from qdrant_client import models as qmodels
            except ImportError as exc:
                raise QdrantUnavailable(
                    "the qdrant backend needs the qdrant-client package: "
                    "pip install 'examlops[qdrant]'"
                ) from exc
            self._client: Any = QdrantClient(
                url=self.url,
                api_key=api_key or os.getenv("EXAMLOPS_QDRANT_API_KEY") or None,
                timeout=int(os.getenv("EXAMLOPS_QDRANT_TIMEOUT", "30")),
            )
            self._m: Any = models if models is not None else qmodels
        else:
            self._client = client
            if models is None:
                try:
                    from qdrant_client import models as qmodels
                except ImportError as exc:  # pragma: no cover - an injected client without models
                    raise QdrantUnavailable(
                        "an injected qdrant client needs either qdrant-client installed or an "
                        "explicit `models=` namespace"
                    ) from exc
                models = qmodels
            self._m = models
        self._registry = f"{self.namespace}_collections"
        self._registry_ready = False

    # ── registry ─────────────────────────────────────────────────────────────

    def _ensure_registry(self) -> None:
        if self._registry_ready:
            return
        if not self._client.collection_exists(self._registry):
            m = self._m
            # A one-dimensional dummy vector: Qdrant has no vector-less collection, and the
            # registry is a key/value lookup by point id — no similarity search ever runs on it.
            self._client.create_collection(
                collection_name=self._registry,
                vectors_config=m.VectorParams(size=1, distance=m.Distance.DOT),
                hnsw_config=m.HnswConfigDiff(m=0),
            )
        self._registry_ready = True

    @staticmethod
    def _registry_id(name: str, tenant: str) -> str:
        return str(uuid.uuid5(_POINT_NS, f"registry\x00{tenant}\x00{name}"))

    def _write_registry(self, meta: dict[str, Any]) -> None:
        self._ensure_registry()
        m = self._m
        self._client.upsert(
            collection_name=self._registry,
            points=[
                m.PointStruct(
                    id=self._registry_id(meta["name"], meta["tenant"]),
                    vector=[0.0],
                    payload=dict(meta),
                )
            ],
        )

    def _read_registry(self, name: str, tenant: str) -> dict[str, Any] | None:
        self._ensure_registry()
        got = self._client.retrieve(
            collection_name=self._registry,
            ids=[self._registry_id(name, tenant)],
            with_payload=True,
        )
        if not got:
            return None
        payload = getattr(got[0], "payload", None) or {}
        return dict(payload)

    def _collection(self, name: str, tenant: str) -> dict[str, Any]:
        meta = self._read_registry(name, tenant)
        if meta is None:
            raise CollectionNotFound(f"collection '{name}' not found for tenant '{tenant}'")
        return meta

    @staticmethod
    def _index_of(meta: dict[str, Any]) -> IndexConfig:
        return IndexConfig.from_row(meta.get("index_type"), meta.get("index_params"))

    def _hnsw(self, cfg: IndexConfig) -> Any:
        """Qdrant's HNSW knobs for a validated :class:`IndexConfig`. ``flat`` is ``m = 0``."""
        m = self._m
        if cfg.type == "hnsw":
            return m.HnswConfigDiff(m=int(cfg.m or 16), ef_construct=int(cfg.ef_construction or 64))
        return m.HnswConfigDiff(m=0)

    def _optimizers(self) -> Any:
        """``indexing_threshold`` override, or ``None`` to keep Qdrant's default.

        Qdrant will not build an HNSW graph for a segment below this size (20 MB of vectors by
        default) — an operator who needs the graph on a smaller collection sets
        ``EXAMLOPS_QDRANT_INDEXING_THRESHOLD_KB``. Unset means "whatever Qdrant decides", which is
        the right default: below the threshold an exact scan is genuinely the faster answer.
        """
        raw = os.getenv("EXAMLOPS_QDRANT_INDEXING_THRESHOLD_KB")
        if not raw:
            return None
        return self._m.OptimizersConfigDiff(indexing_threshold=int(raw))

    @staticmethod
    def _check_index(cfg: IndexConfig | None) -> None:
        if cfg is not None and cfg.type == "ivfflat":
            raise IndexConfigError(
                "Qdrant has no IVFFlat index — it serves either an HNSW graph or an exact scan. "
                "Use --index hnsw (or --index flat) on this backend, or keep the collection on "
                "pgvector, which does have IVFFlat."
            )

    # ── lifecycle ────────────────────────────────────────────────────────────

    def create_collection(
        self,
        name: str,
        dim: int,
        metric: str = "cosine",
        tenant: str = "default",
        encoder_id: str | None = None,
        index: IndexConfig | None = None,
    ) -> None:
        """Declare a collection (same contract as the other stores, including SchemaConflict)."""
        if metric not in _METRICS:
            raise ValueError(f"metric '{metric}' not in {tuple(_METRICS)}")
        if int(dim) < 1:
            raise ValueError(f"dim must be >= 1, got {dim}")
        if index is not None:
            index._validate(int(dim))
        self._check_index(index)
        existing = self._read_registry(name, tenant)
        physical = _collection_name(self.namespace, name, tenant)
        if existing is not None:
            stamped = existing.get("encoder_id")
            changed = []
            if int(existing["dim"]) != int(dim):
                changed.append(f"dim {existing['dim']} → {dim}")
            if existing["metric"] != metric:
                changed.append(f"metric {existing['metric']} → {metric}")
            if encoder_id and stamped and stamped != encoder_id:
                changed.append(f"encoder {stamped} → {encoder_id}")
            n = self._count(physical)
            if changed and n:
                raise SchemaConflict(
                    f"collection '{name}' (tenant '{tenant}') holds {n} items; refusing to "
                    f"change {', '.join(changed)} under them. Drop it (exa vector drop {name}) "
                    f"or, for an encoder change, reindex it (exa embedding reindex {name} <encoder>)."
                )
            cfg = index or self._index_of(existing)
            self._check_index(cfg)
            if int(existing["dim"]) != int(dim) or existing["metric"] != metric:
                # Empty collection: the vector shape itself changes, so rebuild it.
                self._client.delete_collection(collection_name=physical)
                self._create_physical(physical, int(dim), metric, cfg)
            elif cfg != self._index_of(existing):
                self._client.update_collection(
                    collection_name=physical,
                    hnsw_config=self._hnsw(cfg),
                    optimizers_config=self._optimizers(),
                )
            self._write_registry(
                {
                    "name": name,
                    "tenant": tenant,
                    "dim": int(dim),
                    "metric": metric,
                    "encoder_id": encoder_id or stamped,
                    "index_type": cfg.type,
                    "index_params": cfg.to_json(),
                    "collection": physical,
                }
            )
            return
        cfg = index or IndexConfig()
        self._create_physical(physical, int(dim), metric, cfg)
        self._write_registry(
            {
                "name": name,
                "tenant": tenant,
                "dim": int(dim),
                "metric": metric,
                "encoder_id": encoder_id,
                "index_type": cfg.type,
                "index_params": cfg.to_json(),
                "collection": physical,
            }
        )

    def _create_physical(self, physical: str, dim: int, metric: str, cfg: IndexConfig) -> None:
        m = self._m
        if not self._client.collection_exists(physical):
            self._client.create_collection(
                collection_name=physical,
                vectors_config=m.VectorParams(
                    size=int(dim), distance=getattr(m.Distance, _METRICS[metric].upper())
                ),
                hnsw_config=self._hnsw(cfg),
                optimizers_config=self._optimizers(),
            )
        # The lexical channel's prefilter. Without a full-text index on `text`, a MatchText
        # condition is a server-side error, not a slow query.
        self._client.create_payload_index(
            collection_name=physical,
            field_name="text",
            field_schema=m.TextIndexParams(
                type=m.TextIndexType.TEXT, lowercase=True, min_token_len=1
            ),
        )
        # `ts` orders `scan` and bounds `trim`; Qdrant's order_by requires the field to be indexed.
        self._client.create_payload_index(
            collection_name=physical, field_name="ts", field_schema=m.PayloadSchemaType.FLOAT
        )

    def drop_collection(self, coll: str, tenant: str = "default") -> int:
        meta = self._collection(coll, tenant)
        n = self._count(meta["collection"])
        self._client.delete_collection(collection_name=meta["collection"])
        self._client.delete(
            collection_name=self._registry,
            points_selector=self._m.PointIdsList(points=[self._registry_id(coll, tenant)]),
        )
        _record_metric(coll, tenant, "drop", 0.0, n)
        return n

    def reindex(self, coll: str, tenant: str = "default", index: IndexConfig | None = None) -> None:
        """Retune the HNSW graph and let Qdrant's optimizers rebuild it online.

        Blue-green is native here: the optimizer builds new segments while the existing ones keep
        answering, so search stays up throughout — the same guarantee the pgvector store buys with
        ``CREATE INDEX CONCURRENTLY``. Without ``index`` this is a no-op rebuild request, which is
        what B6 calls after an encoder change.
        """
        meta = self._collection(coll, tenant)
        if index is not None:
            index._validate(int(meta["dim"]))
            self._check_index(index)
            meta = {**meta, "index_type": index.type, "index_params": index.to_json()}
            self._write_registry(meta)
        cfg = self._index_of(meta)
        t0 = time.time()
        self._client.update_collection(
            collection_name=meta["collection"],
            hnsw_config=self._hnsw(cfg),
            optimizers_config=self._optimizers(),
        )
        _record_metric(
            coll, tenant, "reindex", (time.time() - t0) * 1000, self._count(meta["collection"])
        )

    # ── data ─────────────────────────────────────────────────────────────────

    def upsert(
        self,
        coll: str,
        items: list[VecItem],
        tenant: str = "default",
        encoder_id: str | None = None,
    ) -> None:
        meta = self._collection(coll, tenant)
        dim = int(meta["dim"])
        SqliteVectorStore._check_encoder(meta, encoder_id, coll)
        for it in items:
            if len(it.vector) != dim:
                raise DimensionMismatch(
                    f"vector '{it.id}' has dim {len(it.vector)}, collection expects {dim}"
                )
            if not all(math.isfinite(float(x)) for x in it.vector):
                raise ValueError(f"vector '{it.id}' contains NaN or infinity")
        if not items:
            return
        m = self._m
        t0 = time.time()
        points = [
            m.PointStruct(
                id=_point_id(it.id),
                vector=[float(x) for x in it.vector],
                payload={
                    "eid": it.id,
                    "text": _item_text(it),
                    "ts": _now(),
                    "meta": dict(it.metadata or {}),
                },
            )
            for it in items
        ]
        for start in range(0, len(points), _UPSERT_BATCH):
            self._client.upsert(
                collection_name=meta["collection"], points=points[start : start + _UPSERT_BATCH]
            )
        _record_metric(coll, tenant, "upsert", (time.time() - t0) * 1000, len(items))

    def _filter(self, flt: dict | None, *, terms: list[str] | None = None) -> Any:
        """A Qdrant filter: metadata equality in ``must``, query terms OR-ed in ``should``."""
        m = self._m
        must = []
        for key, value in (flt or {}).items():
            if not isinstance(value, str | int | bool):
                raise ValueError(
                    f"the qdrant backend filters on string, integer and boolean metadata; "
                    f"key {key!r} has {type(value).__name__}"
                )
            must.append(m.FieldCondition(key=f"meta.{key}", match=m.MatchValue(value=value)))
        should = [m.FieldCondition(key="text", match=m.MatchText(text=t)) for t in (terms or [])]
        if not must and not should:
            return None
        return m.Filter(must=must or None, should=should or None)

    @staticmethod
    def _hit(point: Any, to_score: Any) -> Hit:
        payload = dict(getattr(point, "payload", None) or {})
        return Hit(
            str(payload.get("eid") or point.id),
            float(to_score(float(getattr(point, "score", 0.0)))),
            dict(payload.get("meta") or {}),
        )

    def _dense(
        self, meta: dict[str, Any], vector: list[float], n: int, flt: dict | None
    ) -> list[Hit]:
        m = self._m
        cfg = self._index_of(meta)
        params = m.SearchParams(
            hnsw_ef=int(cfg.ef_search or 40) if cfg.type == "hnsw" else None,
            exact=cfg.type == "flat",
        )
        response = self._client.query_points(
            collection_name=meta["collection"],
            query=[float(x) for x in vector],
            limit=max(1, int(n)),
            query_filter=self._filter(flt),
            search_params=params,
            with_payload=True,
        )
        # Qdrant reports a *distance* for Euclid and a *similarity* for Cosine/Dot; the platform's
        # convention is "higher is better" for every metric (see `_score` in the package root).
        negate = meta["metric"] == "l2"
        hits = [self._hit(p, (lambda d: -d) if negate else (lambda d: d)) for p in response.points]
        hits.sort(key=lambda h: (-h.score, h.id))  # ties by id: same inputs, same order
        return hits

    def _scroll(self, physical: str, flt: Any, limit: int, *, vectors: bool = False) -> list[Any]:
        points, _ = self._client.scroll(
            collection_name=physical,
            scroll_filter=flt,
            limit=max(1, int(limit)),
            with_payload=True,
            with_vectors=vectors,
        )
        return list(points)

    def _sparse(self, meta: dict[str, Any], text: str, n: int, flt: dict | None) -> list[Hit]:
        terms = list(dict.fromkeys(tokenize(text)))
        if not terms:
            return []
        candidates = self._scroll(
            meta["collection"],
            self._filter(flt, terms=terms),
            min(_SPARSE_CANDIDATE_CAP, max(int(n) * 10, 200)),
        )
        payloads = {}
        for point in candidates:
            payload = dict(getattr(point, "payload", None) or {})
            payloads[str(payload.get("eid") or point.id)] = payload
        scores = bm25_scores(text, ((k, v.get("text")) for k, v in payloads.items()))
        hits = [
            Hit(doc_id, score, dict(payloads[doc_id].get("meta") or {}))
            for doc_id, score in scores.items()
        ]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[: max(1, int(n))]

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
        SqliteVectorStore._check_encoder(meta, encoder_id, coll)
        t0 = time.time()
        hits = self._dense(meta, vector, k, flt)
        _record_metric(coll, tenant, "search", (time.time() - t0) * 1000, len(hits))
        return hits

    def sparse_search(
        self, coll: str, text: str, k: int = 5, flt: dict | None = None, tenant: str = "default"
    ) -> list[Hit]:
        """Lexical top-k. No vector, so no encoder check applies."""
        meta = self._collection(coll, tenant)
        t0 = time.time()
        hits = self._sparse(meta, text, k, flt)
        _record_metric(coll, tenant, "sparse_search", (time.time() - t0) * 1000, len(hits))
        return hits

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
        meta = self._collection(coll, tenant)
        if len(vector) != int(meta["dim"]):
            raise DimensionMismatch(
                f"query vector dim {len(vector)} != collection dim {int(meta['dim'])}"
            )
        SqliteVectorStore._check_encoder(meta, encoder_id, coll)
        if fusion not in FUSIONS:
            raise ValueError(f"fusion '{fusion}' not in {FUSIONS}")
        n = hybrid_candidates(k, candidates)
        t0 = time.time()
        dense = self._dense(meta, vector, n, flt)
        sparse = self._sparse(meta, text, n, flt)
        hits = _fused_hits(dense, sparse, k, fusion, alpha, rrf_k)
        _record_metric(coll, tenant, "hybrid_search", (time.time() - t0) * 1000, len(hits))
        return hits

    # ── introspection ────────────────────────────────────────────────────────

    def _count(self, physical: str) -> int:
        if not self._client.collection_exists(physical):
            return 0
        return int(self._client.count(collection_name=physical, exact=True).count)

    def count(self, coll: str, tenant: str = "default") -> int:
        return self._count(self._collection(coll, tenant)["collection"])

    def _ordered(self, physical: str, limit: int, *, vectors: bool) -> list[Any]:
        m = self._m
        points, _ = self._client.scroll(
            collection_name=physical,
            limit=max(1, int(limit)),
            order_by=m.OrderBy(key="ts", direction=m.Direction.DESC),
            with_payload=True,
            with_vectors=vectors,
        )
        return list(points)

    def trim(self, coll: str, tenant: str = "default", keep: int = 1000) -> int:
        """Keep the ``keep`` most recently written items; return how many were evicted.

        The eviction is a single server-side delete-by-range on ``ts`` — the boundary stamp is
        read from the ``keep``-th newest point, so the cost is ``keep``, not the collection size.
        That matters for the ring buffers this primitive exists for (ADR 0020 clause 4): a
        scale-out collection is exactly the one you cannot afford to enumerate.
        """
        meta = self._collection(coll, tenant)
        physical = meta["collection"]
        keep = max(0, int(keep))
        total = self._count(physical)
        if total <= keep:
            return 0
        m = self._m
        if keep == 0:
            selector = m.FilterSelector(filter=m.Filter(must=[]))
        else:
            newest = self._ordered(physical, keep, vectors=False)
            cutoff = float((dict(getattr(newest[-1], "payload", None) or {})).get("ts") or 0.0)
            selector = m.FilterSelector(
                filter=m.Filter(must=[m.FieldCondition(key="ts", range=m.Range(lt=cutoff))])
            )
        self._client.delete(collection_name=physical, points_selector=selector)
        evicted = total - self._count(physical)
        if evicted:
            _record_metric(coll, tenant, "trim", 0.0, evicted)
        return evicted

    def scan(self, coll: str, tenant: str = "default", limit: int = 1000) -> list[VecItem]:
        """Up to ``limit`` items, most recently written first.

        On a **cosine** collection the vectors come back L2-normalised — Qdrant normalises on
        write — so directions are exact and magnitudes are not. Every cosine question (similarity,
        centroid direction) is unaffected; a caller that needs the original magnitude should use
        an ``l2`` or ``dot`` collection.
        """
        meta = self._collection(coll, tenant)
        if int(limit) <= 0:
            return []
        out: list[VecItem] = []
        for point in self._ordered(meta["collection"], int(limit), vectors=True):
            payload = dict(getattr(point, "payload", None) or {})
            vector = getattr(point, "vector", None)
            if isinstance(vector, dict):  # a named-vector collection, not one of ours
                vector = next(iter(vector.values()), [])
            out.append(
                VecItem(
                    str(payload.get("eid") or point.id),
                    [float(x) for x in (vector or [])],
                    dict(payload.get("meta") or {}),
                    payload.get("text"),
                )
            )
        return out

    def stats(self, coll: str, tenant: str = "default") -> dict[str, Any]:
        meta = self._collection(coll, tenant)
        physical = meta["collection"]
        n = self._count(physical)
        idx = self._index_of(meta)
        mode = "exact"
        indexed: int | None = None
        if idx.type == "hnsw":
            info = self._client.get_collection(collection_name=physical)
            indexed = int(getattr(info, "indexed_vectors_count", None) or 0)
            if n and not indexed:
                # A declaration is not an index. Qdrant leaves a segment unindexed until it passes
                # `indexing_threshold`, and indexes asynchronously after that; it answers by exact
                # scan throughout. Saying "ann" here would overstate latency *and* understate
                # recall, exactly as it would on pgvector with an IVFFlat that was never built.
                mode = (
                    "exact (HNSW declared but not built — below Qdrant's indexing_threshold, or "
                    "still building; see EXAMLOPS_QDRANT_INDEXING_THRESHOLD_KB)"
                )
            else:
                mode = "ann"
        return {
            "collection": coll,
            "tenant": tenant,
            "dim": int(meta["dim"]),
            "metric": meta["metric"],
            "count": n,
            "encoder_id": meta.get("encoder_id"),
            "index": {"type": idx.type, **idx.params},
            "search": mode,
            "backend": self.name,
            "qdrant_collection": physical,
            # How many of `count` vectors the HNSW graph actually covers. Qdrant indexes segment
            # by segment, so a partly-indexed collection is a real, normal state worth seeing.
            "indexed_vectors": indexed,
        }
