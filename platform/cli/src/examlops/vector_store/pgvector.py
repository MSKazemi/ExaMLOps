"""Postgres + pgvector store — the production default of ADR 0020.

**Layout.** One registry table, ``examlops_vector_collections``, plus one table per collection.
A table per collection is what lets each collection have a typed ``vector(dim)`` column — pgvector
can only build an ANN index over a column of one fixed dimension — and it makes tenant erasure a
``DROP TABLE`` rather than a scan-and-delete over a shared table. Table names are derived from a
hash of ``(tenant, name)``, so no user string ever becomes an identifier.

Each collection table carries:

- ``embedding vector(dim)`` with the declared ANN index (HNSW, IVFFlat, or none);
- ``metadata jsonb`` with a ``jsonb_path_ops`` GIN index, serving the equality filter as
  ``metadata @> filter`` (containment is equality for scalar values);
- ``text`` and a generated ``tsvector`` column with its own GIN index — the lexical channel of
  hybrid search. Postgres ranks it with ``ts_rank_cd`` (cover density), not BM25; fusion uses
  ranks only (RRF) or min-max-normalised scores (convex), so the scorer's scale never leaks into
  the fused result, but absolute sparse scores differ from the SQLite fallback's BM25.

**Scores** match the SQLite store's convention, so a caller cannot tell the backends apart by
sign or direction: cosine → ``1 − (a <=> b)``, l2 → ``−(a <-> b)``, dot → ``−(a <#> b)``
(pgvector's ``<#>`` is the *negative* inner product).

**Filtered ANN search.** An HNSW scan returns ``ef_search`` candidates and the filter runs after,
so a selective filter used to return fewer than ``k`` rows. pgvector ≥ 0.8 has iterative scans;
this store enables ``hnsw.iterative_scan = relaxed_order`` whenever a filter is present, and
re-sorts the (slightly out-of-order) result in Python.

**Blue-green reindex.** ``reindex`` builds the new ANN index ``CONCURRENTLY`` under a temporary
name while the old one keeps serving, then drops the old one and takes over its name. Writes and
reads continue throughout.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from examlops.vector_store import (
    FUSIONS,
    RRF_K,
    CollectionNotFound,
    DimensionMismatch,
    Hit,
    IndexConfig,
    SchemaConflict,
    SqliteVectorStore,
    VecItem,
    _fused_hits,
    _item_text,
    hybrid_candidates,
)
from examlops.vector_store.sparse import tsquery_or

_METRICS = {
    # metric: (operator, operator class, distance → score)
    "cosine": ("<=>", "vector_cosine_ops", lambda d: 1.0 - d),
    "l2": ("<->", "vector_l2_ops", lambda d: -d),
    "dot": ("<#>", "vector_ip_ops", lambda d: -d),
}
_REGISTRY = "examlops_vector_collections"
# The schema reaches libpq inside an options string (`-c search_path=...`), where a space or a
# `-c` would smuggle in other settings. Only a plain unquoted identifier is accepted.
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

_POOLS: dict[tuple[str, str | None], Any] = {}
_POOLS_LOCK = threading.Lock()
_BOOTSTRAPPED: dict[tuple[str, str | None], str] = {}  # (dsn, schema) -> pgvector extversion


def _sql():
    from psycopg import sql

    return sql


def _vec_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _table_for(name: str, tenant: str) -> str:
    digest = hashlib.sha256(f"{tenant}\x00{name}".encode()).hexdigest()[:24]
    return f"evec_{digest}"


def _version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out)


class PgVectorStore:
    """Postgres + pgvector store. Needs ``EXAMLOPS_PGVECTOR_DSN`` and ``examlops[vector]``."""

    name = "pgvector"

    def __init__(self, dsn: str | None = None, schema: str | None = None) -> None:
        self.dsn = dsn or os.getenv("EXAMLOPS_PGVECTOR_DSN")
        if not self.dsn:
            raise RuntimeError(
                "pgvector store needs EXAMLOPS_PGVECTOR_DSN; use the 'sqlite' backend for local dev"
            )
        self.schema = schema or os.getenv("EXAMLOPS_PGVECTOR_SCHEMA") or None
        if self.schema is not None and not _SCHEMA_RE.match(self.schema):
            raise ValueError(
                f"EXAMLOPS_PGVECTOR_SCHEMA {self.schema!r} is not a plain identifier "
                "(letters, digits, underscore; at most 63 characters)"
            )
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "the pgvector backend needs psycopg: pip install 'examlops[vector]'"
            ) from exc
        self._statement_timeout_ms = int(
            os.getenv("EXAMLOPS_PGVECTOR_STATEMENT_TIMEOUT_MS", "10000")
        )
        self._ext_version = self._bootstrap()

    # ── connections ──────────────────────────────────────────────────────────

    def _connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "connect_timeout": int(os.getenv("EXAMLOPS_PGVECTOR_CONNECT_TIMEOUT", "5"))
        }
        if self.schema:
            # `public` stays on the path because that is where the `vector` type lives.
            kwargs["options"] = f"-c search_path={self.schema},public"
        return kwargs

    def _pool(self) -> Any | None:
        key = (str(self.dsn), self.schema)
        with _POOLS_LOCK:
            if key in _POOLS:
                return _POOLS[key]
            try:
                from psycopg_pool import ConnectionPool
            except ImportError:
                _POOLS[key] = None  # unpooled: slower, still correct
                return None
            pool = ConnectionPool(
                str(self.dsn),
                min_size=1,
                max_size=int(os.getenv("EXAMLOPS_PGVECTOR_POOL_MAX", "10")),
                kwargs=self._connect_kwargs(),
                open=True,
                name=f"examlops-vector-{key[1] or 'public'}",
            )
            _POOLS[key] = pool
            return pool

    @contextmanager
    def _tx(self) -> Iterator[Any]:
        """One transaction: commit on success, roll back on any exception."""
        pool = self._pool()
        if pool is not None:
            with pool.connection() as conn:  # the pool commits / rolls back on exit
                yield conn
            return
        import psycopg

        with psycopg.connect(str(self.dsn), **self._connect_kwargs()) as conn:
            yield conn

    @contextmanager
    def _autocommit(self) -> Iterator[Any]:
        """A dedicated autocommit connection, for ``CREATE/DROP INDEX CONCURRENTLY``.

        Never a pooled connection: CONCURRENTLY refuses to run inside a transaction block, and
        flipping a pooled connection's autocommit mode would leak into the next borrower.
        """
        import psycopg

        conn = psycopg.connect(str(self.dsn), autocommit=True, **self._connect_kwargs())
        try:
            yield conn
        finally:
            conn.close()

    def _bootstrap(self) -> str:
        key = (str(self.dsn), self.schema)
        if key in _BOOTSTRAPPED:
            return _BOOTSTRAPPED[key]
        sql = _sql()
        with self._tx() as conn:
            # Serialise the one-time DDL across processes: two replicas starting together would
            # otherwise race on CREATE EXTENSION / CREATE TABLE and one would die on a unique
            # violation in the catalog.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('examlops-vector-bootstrap'))")
            try:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
            except Exception as exc:
                raise RuntimeError(
                    "the pgvector extension is not installed on this server and this role may "
                    "not create it — run `CREATE EXTENSION vector` as a superuser (or use the "
                    "pgvector/pgvector image)"
                ) from exc
            if self.schema:
                conn.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema))
                )
            conn.execute(
                sql.SQL(
                    """CREATE TABLE IF NOT EXISTS {} (
                           name         text NOT NULL,
                           tenant       text NOT NULL DEFAULT 'default',
                           dim          integer NOT NULL,
                           metric       text NOT NULL,
                           encoder_id   text,
                           index_type   text NOT NULL DEFAULT 'flat',
                           index_params jsonb NOT NULL DEFAULT '{{}}',
                           table_name   text NOT NULL UNIQUE,
                           created_at   timestamptz NOT NULL DEFAULT now(),
                           PRIMARY KEY (name, tenant)
                       )"""
                ).format(sql.Identifier(_REGISTRY))
            )
            row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
        version = str(row[0]) if row else "0"
        _BOOTSTRAPPED[key] = version
        return version

    # ── registry ─────────────────────────────────────────────────────────────

    def _collection(self, name: str, tenant: str, conn: Any | None = None) -> dict[str, Any]:
        sql = _sql()
        q = sql.SQL(
            "SELECT name, tenant, dim, metric, encoder_id, index_type, index_params, table_name "
            "FROM {} WHERE name = %s AND tenant = %s"
        ).format(sql.Identifier(_REGISTRY))
        if conn is None:
            with self._tx() as c:
                row = c.execute(q, (name, tenant)).fetchone()
        else:
            row = conn.execute(q, (name, tenant)).fetchone()
        if row is None:
            raise CollectionNotFound(f"collection '{name}' not found for tenant '{tenant}'")
        keys = (
            "name",
            "tenant",
            "dim",
            "metric",
            "encoder_id",
            "index_type",
            "index_params",
            "table_name",
        )
        meta = dict(zip(keys, row))
        if not isinstance(meta["index_params"], str):
            meta["index_params"] = json.dumps(meta["index_params"] or {})
        return meta

    @staticmethod
    def _index_of(meta: dict[str, Any]) -> IndexConfig:
        return IndexConfig.from_row(meta.get("index_type"), meta.get("index_params"))

    def _ann_ddl(
        self, table: str, metric: str, cfg: IndexConfig, index_name: str, concurrently: bool
    ) -> Any:
        sql = _sql()
        opclass = _METRICS[metric][1]
        if cfg.type == "hnsw":
            with_ = sql.SQL("WITH (m = {}, ef_construction = {})").format(
                sql.Literal(int(cfg.m or 16)), sql.Literal(int(cfg.ef_construction or 64))
            )
            method = sql.SQL("hnsw")
        else:
            with_ = sql.SQL("WITH (lists = {})").format(sql.Literal(int(cfg.lists or 100)))
            method = sql.SQL("ivfflat")
        return sql.SQL(
            "CREATE INDEX {conc} {idx} ON {tbl} USING {method} (embedding {ops}) {w}"
        ).format(
            conc=sql.SQL("CONCURRENTLY") if concurrently else sql.SQL(""),
            idx=sql.Identifier(index_name),
            tbl=sql.Identifier(table),
            method=method,
            ops=sql.SQL(opclass),
            w=with_,
        )

    def _create_table(self, conn: Any, table: str, dim: int, metric: str, cfg: IndexConfig) -> None:
        sql = _sql()
        conn.execute(
            sql.SQL(
                """CREATE TABLE {tbl} (
                       item_id    text PRIMARY KEY,
                       embedding  vector({dim}) NOT NULL,
                       metadata   jsonb NOT NULL DEFAULT '{{}}',
                       text       text,
                       tsv        tsvector GENERATED ALWAYS AS
                                  (to_tsvector('simple', coalesce(text, ''))) STORED,
                       updated_at timestamptz NOT NULL DEFAULT now()
                   )"""
            ).format(tbl=sql.Identifier(table), dim=sql.Literal(int(dim)))
        )
        conn.execute(
            sql.SQL("CREATE INDEX {} ON {} USING gin (tsv)").format(
                sql.Identifier(f"{table}_tsv"), sql.Identifier(table)
            )
        )
        conn.execute(
            sql.SQL("CREATE INDEX {} ON {} USING gin (metadata jsonb_path_ops)").format(
                sql.Identifier(f"{table}_meta"), sql.Identifier(table)
            )
        )
        # HNSW builds incrementally, so it can exist from the first row. IVFFlat trains its
        # centroids on the rows present at build time — built on an empty table its lists mean
        # nothing — so it is built by `reindex`, after loading.
        if cfg.type == "hnsw":
            conn.execute(self._ann_ddl(table, metric, cfg, f"{table}_ann", concurrently=False))

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
        """Declare a collection (same contract as the SQLite store, including SchemaConflict)."""
        if metric not in _METRICS:
            raise ValueError(f"metric '{metric}' not in {tuple(_METRICS)}")
        if int(dim) < 1:
            raise ValueError(f"dim must be >= 1, got {dim}")
        if index is not None:
            index._validate(int(dim))
        sql = _sql()
        rebuild_ann: IndexConfig | None = None
        with self._tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"evec:{tenant}:{name}",))
            try:
                existing: dict[str, Any] | None = self._collection(name, tenant, conn)
            except CollectionNotFound:
                existing = None
            table = _table_for(name, tenant)
            if existing is None:
                cfg = index or IndexConfig()
                self._create_table(conn, table, dim, metric, cfg)
                conn.execute(
                    sql.SQL(
                        "INSERT INTO {} (name, tenant, dim, metric, encoder_id, index_type, "
                        "index_params, table_name) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)"
                    ).format(sql.Identifier(_REGISTRY)),
                    (name, tenant, int(dim), metric, encoder_id, cfg.type, cfg.to_json(), table),
                )
                return
            stamped = existing.get("encoder_id")
            changed = []
            if int(existing["dim"]) != int(dim):
                changed.append(f"dim {existing['dim']} → {dim}")
            if existing["metric"] != metric:
                changed.append(f"metric {existing['metric']} → {metric}")
            if encoder_id and stamped and stamped != encoder_id:
                changed.append(f"encoder {stamped} → {encoder_id}")
            n = self._count(conn, existing["table_name"])
            if changed and n:
                raise SchemaConflict(
                    f"collection '{name}' (tenant '{tenant}') holds {n} items; refusing to "
                    f"change {', '.join(changed)} under them. Drop it (exa vector drop {name}) "
                    f"or, for an encoder change, reindex it (exa embedding reindex {name} <encoder>)."
                )
            old_cfg = self._index_of(existing)
            cfg = index or old_cfg
            if int(existing["dim"]) != int(dim) or existing["metric"] != metric:
                # Empty collection: the vector column type / opclass changes, so rebuild it.
                conn.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}").format(
                        sql.Identifier(existing["table_name"])
                    )
                )
                self._create_table(conn, existing["table_name"], dim, metric, cfg)
            elif cfg != old_cfg:
                rebuild_ann = cfg
            conn.execute(
                sql.SQL(
                    "UPDATE {} SET dim=%s, metric=%s, encoder_id=%s, index_type=%s, "
                    "index_params=%s::jsonb WHERE name=%s AND tenant=%s"
                ).format(sql.Identifier(_REGISTRY)),
                (int(dim), metric, encoder_id or stamped, cfg.type, cfg.to_json(), name, tenant),
            )
        if rebuild_ann is not None:
            self.reindex(name, tenant)

    def drop_collection(self, coll: str, tenant: str = "default") -> int:
        sql = _sql()
        with self._tx() as conn:
            meta = self._collection(coll, tenant, conn)
            n = self._count(conn, meta["table_name"])
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(meta["table_name"]))
            )
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE name=%s AND tenant=%s").format(
                    sql.Identifier(_REGISTRY)
                ),
                (coll, tenant),
            )
        self._metric(coll, tenant, "drop", 0.0, n)
        return n

    def reindex(self, coll: str, tenant: str = "default", index: IndexConfig | None = None) -> None:
        """Blue-green rebuild of the ANN index; with ``index``, switch to that configuration."""
        sql = _sql()
        meta = self._collection(coll, tenant)
        if index is not None:
            index._validate(int(meta["dim"]))
            with self._tx() as conn:
                conn.execute(
                    sql.SQL(
                        "UPDATE {} SET index_type=%s, index_params=%s::jsonb "
                        "WHERE name=%s AND tenant=%s"
                    ).format(sql.Identifier(_REGISTRY)),
                    (index.type, index.to_json(), coll, tenant),
                )
            meta = self._collection(coll, tenant)
        cfg = self._index_of(meta)
        table = meta["table_name"]
        live, staging = f"{table}_ann", f"{table}_ann_new"
        t0 = time.time()
        with self._autocommit() as conn:
            got = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))", (f"evec-reindex:{table}",)
            ).fetchone()[0]
            if not got:
                raise RuntimeError(f"a reindex of '{coll}' is already running")
            try:
                # A crashed earlier attempt leaves an INVALID staging index behind; it would make
                # the CREATE below fail on the name. Clear it first.
                conn.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(staging))
                )
                if cfg.type == "flat":
                    conn.execute(
                        sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(live))
                    )
                else:
                    conn.execute(
                        self._ann_ddl(table, meta["metric"], cfg, staging, concurrently=True)
                    )
                    conn.execute(
                        sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}").format(sql.Identifier(live))
                    )
                    conn.execute(
                        sql.SQL("ALTER INDEX {} RENAME TO {}").format(
                            sql.Identifier(staging), sql.Identifier(live)
                        )
                    )
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"evec-reindex:{table}",))
        with self._tx() as conn:
            n = self._count(conn, table)
        self._metric(coll, tenant, "reindex", (time.time() - t0) * 1000, n)

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
        sql = _sql()
        t0 = time.time()
        with self._tx() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    sql.SQL(
                        "INSERT INTO {} (item_id, embedding, metadata, text) "
                        "VALUES (%s, %s::vector, %s::jsonb, %s) "
                        "ON CONFLICT (item_id) DO UPDATE SET embedding = excluded.embedding, "
                        "metadata = excluded.metadata, text = excluded.text, updated_at = now()"
                    ).format(sql.Identifier(meta["table_name"])),
                    [
                        (it.id, _vec_literal(it.vector), json.dumps(it.metadata), _item_text(it))
                        for it in items
                    ],
                )
        self._metric(coll, tenant, "upsert", (time.time() - t0) * 1000, len(items))

    def _session_settings(self, conn: Any, cfg: IndexConfig, filtered: bool) -> None:
        sql = _sql()
        conn.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(
                sql.Literal(int(self._statement_timeout_ms))
            )
        )
        if cfg.type == "hnsw":
            conn.execute(
                sql.SQL("SET LOCAL hnsw.ef_search = {}").format(
                    sql.Literal(int(cfg.ef_search or 40))
                )
            )
            if filtered and _version_tuple(self._ext_version) >= (0, 8, 0):
                conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        elif cfg.type == "ivfflat":
            conn.execute(
                sql.SQL("SET LOCAL ivfflat.probes = {}").format(sql.Literal(int(cfg.probes or 1)))
            )
            if filtered and _version_tuple(self._ext_version) >= (0, 8, 0):
                conn.execute("SET LOCAL ivfflat.iterative_scan = relaxed_order")

    def _dense(
        self, conn: Any, meta: dict[str, Any], vector: list[float], n: int, flt: dict | None
    ) -> list[Hit]:
        sql = _sql()
        op, _, to_score = _METRICS[meta["metric"]]
        where = sql.SQL("WHERE metadata @> %s::jsonb") if flt else sql.SQL("")
        q = sql.SQL(
            "SELECT item_id, metadata, embedding {op} %s::vector AS dist FROM {tbl} {where} "
            "ORDER BY embedding {op} %s::vector LIMIT %s"
        ).format(op=sql.SQL(op), tbl=sql.Identifier(meta["table_name"]), where=where)
        lit = _vec_literal(vector)
        params: tuple = (lit, json.dumps(flt), lit, n) if flt else (lit, lit, n)
        rows = conn.execute(q, params).fetchall()
        hits = [Hit(str(r[0]), float(to_score(float(r[2]))), dict(r[1] or {})) for r in rows]
        # relaxed_order iterative scans may return slightly out-of-order rows; the order a caller
        # sees must not depend on which scan strategy the planner picked.
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits

    def _sparse(
        self, conn: Any, meta: dict[str, Any], text: str, n: int, flt: dict | None
    ) -> list[Hit]:
        query = tsquery_or(text)
        if not query:
            return []
        sql = _sql()
        where = sql.SQL("AND metadata @> %s::jsonb") if flt else sql.SQL("")
        q = sql.SQL(
            "SELECT item_id, metadata, ts_rank_cd(tsv, q) AS score "
            "FROM {tbl}, to_tsquery('simple', %s) AS q WHERE tsv @@ q {where} "
            "ORDER BY score DESC, item_id LIMIT %s"
        ).format(tbl=sql.Identifier(meta["table_name"]), where=where)
        params: tuple = (query, json.dumps(flt), n) if flt else (query, n)
        return [Hit(str(r[0]), float(r[2]), dict(r[1] or {})) for r in conn.execute(q, params)]

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
        with self._tx() as conn:
            self._session_settings(conn, self._index_of(meta), bool(flt))
            hits = self._dense(conn, meta, vector, k, flt)
        self._metric(coll, tenant, "search", (time.time() - t0) * 1000, len(hits))
        return hits

    def sparse_search(
        self, coll: str, text: str, k: int = 5, flt: dict | None = None, tenant: str = "default"
    ) -> list[Hit]:
        meta = self._collection(coll, tenant)
        t0 = time.time()
        with self._tx() as conn:
            self._session_settings(conn, IndexConfig(), bool(flt))
            hits = self._sparse(conn, meta, text, k, flt)
        self._metric(coll, tenant, "sparse_search", (time.time() - t0) * 1000, len(hits))
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
        with self._tx() as conn:  # one snapshot for both channels
            self._session_settings(conn, self._index_of(meta), bool(flt))
            dense = self._dense(conn, meta, vector, n, flt)
            sparse = self._sparse(conn, meta, text, n, flt)
        hits = _fused_hits(dense, sparse, k, fusion, alpha, rrf_k)
        self._metric(coll, tenant, "hybrid_search", (time.time() - t0) * 1000, len(hits))
        return hits

    # ── introspection ────────────────────────────────────────────────────────

    @staticmethod
    def _count(conn: Any, table: str) -> int:
        sql = _sql()
        return int(
            conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
        )

    def trim(self, coll: str, tenant: str = "default", keep: int = 1000) -> int:
        """Keep the ``keep`` most recently written items (by ``updated_at``); evict the rest."""
        sql = _sql()
        with self._tx() as conn:
            table = self._collection(coll, tenant, conn)["table_name"]
            cur = conn.execute(
                sql.SQL(
                    "DELETE FROM {t} WHERE item_id IN (SELECT item_id FROM {t} "
                    "ORDER BY updated_at DESC, item_id DESC OFFSET %s)"
                ).format(t=sql.Identifier(table)),
                (max(0, int(keep)),),
            )
            evicted = int(cur.rowcount or 0)
        if evicted:
            self._metric(coll, tenant, "trim", 0.0, evicted)
        return evicted

    def scan(self, coll: str, tenant: str = "default", limit: int = 1000) -> list[VecItem]:
        """Up to ``limit`` items, most recently written first."""
        sql = _sql()
        with self._tx() as conn:
            table = self._collection(coll, tenant, conn)["table_name"]
            rows = conn.execute(
                sql.SQL(
                    "SELECT item_id, embedding::text, metadata, text FROM {} "
                    "ORDER BY updated_at DESC, item_id DESC LIMIT %s"
                ).format(sql.Identifier(table)),
                (max(0, int(limit)),),
            ).fetchall()
        out = []
        for item_id, emb, md, text in rows:
            vec = [float(x) for x in str(emb).strip("[]").split(",") if x != ""]
            meta = md if isinstance(md, dict) else json.loads(md or "{}")
            out.append(VecItem(item_id, vec, meta, text))
        return out

    def count(self, coll: str, tenant: str = "default") -> int:
        with self._tx() as conn:
            return self._count(conn, self._collection(coll, tenant, conn)["table_name"])

    def stats(self, coll: str, tenant: str = "default") -> dict[str, Any]:
        with self._tx() as conn:
            meta = self._collection(coll, tenant, conn)
            n = self._count(conn, meta["table_name"])
            # Resolved through *this* connection's schema. Matching on `pg_class.relname` alone
            # found a same-named index in another schema — collection tables are named by
            # (tenant, name) only, so two instances sharing a server (EXAMLOPS_PGVECTOR_SCHEMA)
            # reported each other's index as built.
            built = conn.execute(
                "SELECT i.indisvalid FROM pg_index i "
                "WHERE i.indexrelid = to_regclass(quote_ident(current_schema()) || '.' || %s)",
                (f"{meta['table_name']}_ann",),
            ).fetchone()
        idx = self._index_of(meta)
        ann_ready = bool(built and built[0])
        if idx.type == "flat":
            mode = "exact"
        elif ann_ready:
            mode = "ann"
        else:
            # Declared but not (yet) built — e.g. IVFFlat before its first reindex. Say so: the
            # collection is answering by exact scan, and an operator sizing latency needs to know.
            mode = "exact (index declared but not built — run: exa vector reindex)"
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
            "pgvector": self._ext_version,
        }

    def _metric(
        self, coll: str, tenant: str, operation: str, latency_ms: float, item_count: int
    ) -> None:
        # Operations metrics land in platform.db, the same `vector_metrics` table the SQLite
        # store writes, so `exa slo export-metrics` sees every backend through one exporter.
        try:
            from examlops.data import get_db, init_db

            init_db()
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO vector_metrics
                           (collection, tenant, operation, latency_ms, item_count)
                       VALUES (?,?,?,?,?)""",
                    (coll, tenant, operation, latency_ms, item_count),
                )
        except Exception:  # noqa: BLE001 - a metrics write must never fail a vector operation
            pass
