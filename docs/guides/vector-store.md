# Vector store & embedding store (B5)

An engine-agnostic **`VectorStore`** used by B3 (semantic cache), B4 (RAG), and A3
(feature/embedding store). Collections declare a fixed dimensionality + distance metric,
support metadata-filtered top-k search, and are isolated per tenant/project (D6).

Design: ADR 0020 · spec `design/vision/specs/B5-vector-db.md`.

## Backends (graceful degrade)

Selected by `EXAMLOPS_VECTOR_BACKEND`:

- **`sqlite`** (default fallback) — a persistent, dependency-free store backed by
  `platform_db` that computes distances in Python. Collections, dim-enforcement, filtered
  search, tenant isolation, and reindex all work with **no external service**.
- **`pgvector`** (production default) — Postgres + pgvector, provisioned via the Helm chart;
  needs `EXAMLOPS_PGVECTOR_DSN`. Qdrant/Milvus are the scale-out options behind the same
  interface (R1/R6 engine parity).

## CLI

```bash
exa vector create docs --dim 384 --metric cosine        # cosine | l2 | dot
exa vector upsert docs --id a1 --vector '[0.1, ...]' --meta '{"lang":"en"}'
exa vector search docs --vector '[0.1, ...]' -k 5 --filter '{"lang":"en"}'
exa vector reindex docs                                  # blue-green; invoked by B6 on encoder change
exa vector stats docs
```

All ops accept `--tenant <name>` for D6 isolation.

## Guarantees

- **Dim enforcement (R2):** a collection fixes its dimensionality; an upsert or query with a
  different-dim vector is rejected (`DimensionMismatch`).
- **Metadata filter (R3):** `search(..., flt={"k": "v"})` returns only hits whose metadata
  matches; hybrid dense+sparse search is a pgvector extension.
- **Tenant isolation (R4):** a collection lives in one tenant namespace; another tenant
  cannot read it (`CollectionNotFound`).
- **Reindex (R5):** `reindex` rebuilds the index blue-green — recall is preserved and search
  stays available. B6 (embedding lifecycle) triggers this on encoder change.
- **Metrics (R7):** index build cost + search latency are recorded to
  `platform_db.vector_metrics` (and Prometheus in production).

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
from examlops.vector_store import VecItem, select_store

store = select_store()                       # sqlite | pgvector via env
store.create_collection("docs", dim=384, metric="cosine", tenant="acme")
store.upsert("docs", [VecItem("a1", embedding, {"lang": "en"})], tenant="acme")
hits = store.search("docs", query_vec, k=5, flt={"lang": "en"}, tenant="acme")
```
