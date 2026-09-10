# Vector store & embedding store (B5)

An engine-agnostic **`VectorStore`** used by B3 (semantic cache), B4 (RAG), and A3
(feature/embedding store). A collection declares a fixed dimensionality, a distance metric and an
ANN index. It supports dense, sparse (BM25) and **hybrid** top-k search with metadata filters, and
it is isolated per tenant/project (D6).

Design: ADR 0020 · spec `design/vision/specs/B5-vector-db.md` · the algorithms behind hybrid
search and the indexes: [Hybrid retrieval](../algorithms/hybrid-retrieval.md).

## Backends

Selected by `EXAMLOPS_VECTOR_BACKEND`. An unknown value is an error, never a silent fallback:

- **`sqlite`** (default) is a persistent, dependency-free store in `platform_db`. Every search is
  an exact scan in Python, so recall is 1.0. Collections, dim enforcement, filters, hybrid
  search, tenant isolation, reindex and drop all work with **no external service**. Past about
  10⁵ items per collection, move to pgvector.
- **`pgvector`** (production) is Postgres with the pgvector extension. It needs
  `EXAMLOPS_PGVECTOR_DSN` and `pip install 'examlops[vector]'`. Each collection is its own table:
  a typed `vector(dim)` column carries the ANN index, and a generated `tsvector` column with a
  GIN index carries the lexical channel. It creates the extension on first use if the role may
  do so; otherwise run `CREATE EXTENSION vector` as a superuser, or use the `pgvector/pgvector`
  image.

| variable | default | purpose |
|---|---|---|
| `EXAMLOPS_VECTOR_BACKEND` | `sqlite` | `sqlite` or `pgvector` |
| `EXAMLOPS_PGVECTOR_DSN` | unset | Postgres DSN for the pgvector backend |
| `EXAMLOPS_PGVECTOR_SCHEMA` | unset (`public`) | schema for the registry and collection tables — lets two instances share one server |
| `EXAMLOPS_PGVECTOR_STATEMENT_TIMEOUT_MS` | `10000` | per-search statement timeout, so a runaway scan cannot hold a connection |
| `EXAMLOPS_PGVECTOR_CONNECT_TIMEOUT` | `5` | seconds to wait for the server |
| `EXAMLOPS_PGVECTOR_POOL_MAX` | `10` | connection-pool size per process (needs `psycopg-pool`) |

## Indexes

```bash
exa vector create docs --dim 384 --index hnsw --m 16 --ef-construction 64 --ef-search 40
exa vector create logs --dim 384 --index ivfflat --lists 200 --probes 14
exa vector create small --dim 384                      # flat: exact scan
```

| index | answers a query by | when to use |
|---|---|---|
| `flat` | scanning every vector (recall 1.0) | small collections; required above 2000 dimensions |
| `hnsw` | walking a proximity graph; raise `--ef-search` for recall | the default for interactive search |
| `ivfflat` | scanning the `--probes` nearest of `--lists` clusters | large, mostly static collections |

Parameters are validated at `create` against pgvector's limits: `m` 2–100, `ef_construction`
≥ 2·m, `ef_search` 1–1000, `lists` 1–32768, `probes` ≤ `lists`, at most 2000 dimensions for an
ANN index. A bad value fails before any index build starts. The SQLite store records the index
and answers every query exactly; the index takes effect when the collection moves to pgvector.

IVFFlat trains its clusters on the rows present when the index is built, so it is built by
`reindex` after you load the collection. Until then `exa vector stats` says
`exact (index declared but not built — run: exa vector reindex)`.

## Search

```bash
exa vector search docs --vector '[0.1, ...]' -k 5 --filter '{"lang":"en"}'          # dense
exa vector search docs --text 'JPCP-4711' --mode sparse                              # BM25
exa vector search docs --vector '[0.1, ...]' --text 'why did JPCP-4711 fail' \
    --mode hybrid -k 5                                                               # both, fused
exa vector search docs --vector '[0.1, ...]' --text '...' --mode hybrid \
    --fusion convex --alpha 0.7                                                      # tuned blend
```

- **Dense** ranks by the collection metric (cosine, L2 or inner product).
- **Sparse** ranks by BM25 over each item's text: `--text` on `upsert`, or a string
  `metadata["text"]`, where RAG keeps its chunks.
- **Hybrid** runs both, takes `max(4k, 50)` candidates from each (`--candidates` overrides),
  and fuses them by Reciprocal Rank Fusion. Use `--fusion convex --alpha` once you have relevance
  judgements to tune the weight. Each hybrid hit reports its dense and sparse rank, so you can
  see which channel carried it.

Filters are metadata equality and are applied before ranking. On pgvector, a filtered HNSW or
IVFFlat search uses an iterative index scan (pgvector ≥ 0.8), so a selective filter still
returns the matching rows.

## What feeds the store

- **RAG knowledge bases** (`exa rag ingest`): chunks with their text, so they are
  hybrid-searchable.
- **Feature-store embeddings** (`exa feature apply … --embedding F`, then
  `exa feature materialize`): each entity's online embedding goes into `features.<view>`, queried
  with `exa feature similar`. See [Feature store](feature-store.md#embedding-features-nearest-neighbours).
- Any caller through the `VectorStore` interface (below).

## Lifecycle

```bash
exa vector upsert docs --id a1 --vector '[0.1, ...]' --text 'JPCP job 4711 failed' --meta '{"lang":"en"}'
exa vector reindex docs                                  # blue-green rebuild; search stays up
exa vector reindex docs --index hnsw --m 32              # retune: switch index and rebuild
exa vector stats docs                                    # dim · metric · index · how search is answered
exa --yes vector drop docs                               # delete the collection (audited)
```

All commands accept `--tenant <name>` for D6 isolation.

## Guarantees

- **Dim enforcement (R2):** a collection fixes its dimensionality. An upsert or query with a
  different-dim vector is rejected (`DimensionMismatch`), and so is a vector containing NaN or
  infinity.
- **Schema safety:** re-declaring a collection that holds items with another dim, metric or
  encoder is refused (`SchemaConflict`). Drop it, or reindex it for an encoder change. An
  identical re-declare is a no-op and keeps the encoder stamp and index.
- **Metadata filter (R3):** `search(..., flt={"k": "v"})` returns only hits whose metadata
  matches.
- **Tenant isolation (R4):** a collection lives in one tenant namespace. Another tenant cannot
  read it (`CollectionNotFound`).
- **Reindex (R5):** `reindex` rebuilds blue-green. On pgvector the new index is built
  `CONCURRENTLY` while the old one keeps serving. A second concurrent reindex of the same
  collection is refused. B6 (embedding lifecycle) triggers a reindex on encoder change.
- **Backend parity:** for the same data and query, pgvector returns the same ranking as the
  SQLite store, for every metric, with and without a filter. A live test suite checks this
  (`tests/unit/test_pgvector_store.py`, `-m live`).
- **Determinism:** ties break by item id, so a ranking never reshuffles between runs.
- **Metrics (R7):** index build cost and search latency (dense, sparse, hybrid) are recorded to
  `platform_db.vector_metrics` for both backends and exported by `exa slo export-metrics`.
- **Drop is audited:** `exa vector drop` writes a `vector_collection_dropped` event to the audit
  chain. The operations history in `vector_metrics` is kept.

## Encoder compatibility (ADR 0043)

A collection can record which encoder produced its vectors, and mismatches are then refused:

```python
store.create_collection("docs", dim=384, encoder_id="minilm@v1")
store.upsert("docs", items, encoder_id="minilm@v1")
store.search("docs", q, encoder_id="e5@v2")     # EncoderMismatch
```

**This is not the dimension check, and the difference is the whole point.** A wrong dimension
cannot be scored at all, so it announces itself. Two encoders of the *same* dimension produce
vectors that score against each other perfectly happily and mean nothing — the search returns
confident, ranked, wrong results, and nothing downstream can tell. `EncoderMismatch` is a separate
error from `DimensionMismatch` because an operator needs to know which of the two happened: one is
unscoreable, the other is scoreable and wrong.

**Unverified is not verified.** An unstamped collection, or a call that names no encoder, passes —
you cannot mismatch an identity nobody asserted, and refusing would break every corpus written
before the column existed. Treat that as the absence of a check rather than a clean bill of health:
stamp your collections if you want the guarantee.

The B3 semantic cache and B4 RAG carry the encoder too — see
[Semantic caching](model-gateway.md#semantic-caching-b3) and [RAG](rag.md).

## Interface

```python
from examlops.vector_store import IndexConfig, VecItem, select_store

store = select_store()                       # sqlite | pgvector via env
store.create_collection("docs", dim=384, metric="cosine", tenant="acme",
                        index=IndexConfig.build("hnsw", m=16, ef_search=64))
store.upsert("docs", [VecItem("a1", embedding, {"lang": "en"}, text="JPCP job 4711")], tenant="acme")
hits = store.search("docs", query_vec, k=5, flt={"lang": "en"}, tenant="acme")
hits = store.sparse_search("docs", "JPCP-4711", k=5, tenant="acme")
hits = store.hybrid_search("docs", query_vec, "why did JPCP-4711 fail", k=5, tenant="acme",
                           fusion="rrf")           # hits[i].channels → dense/sparse score + rank
store.reindex("docs", "acme", index=IndexConfig.build("ivfflat", lists=200))
store.drop_collection("docs", "acme")
```
