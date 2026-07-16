# Embedding Lifecycle & Reindexing (B6)

> Next-Gen 40 · feature **B6** · ADR 0043 · spec `design/vision/specs/B6-embedding-lifecycle.md`

Vector search only works if every vector compared was produced by the **same encoder** in
the **same space**. B6 governs the embeddings the RAG cluster (B3 cache / B4 RAG / B5 vector
store) relies on: it versions encoders, stamps vectors with an `encoder_id`, **refuses**
cross-encoder comparisons, and performs a **verified blue-green reindex** when the encoder
changes — so upgrading an embedding model never silently corrupts recall.

Everything is pure Python and testable — no encoder model, GPU, or vector DB required to
register encoders, enforce the guard, or exercise the reindex/switch/abort logic.

## Versioned encoders

```bash
exa embedding register nomic-embed-text v1.5 --dim 768 --metric cosine --norm l2
# Registered encoder nomic-embed-text-v1.5-9f2c8a1b4e6d (nomic-embed-text v1.5, dim 768, cosine)
exa embedding list
```

The `encoder_id` is **content-addressed** over name/version/dim/metric/normalization, so the
same encoder always resolves to the same id and any change produces a new one.

## Compatibility guard — no silent cross-encoder compares

```python
from examlops.embeddings import guard_compatible, EncoderMismatchError

guard_compatible(vec_a.encoder_id, vec_b.encoder_id)   # raises if they differ
```

A comparison between vectors from different encoders is **refused** (`EncoderMismatchError`),
never computed as if the spaces were compatible (R3 / GWT-2). B3/B5 stamp each stored
vector/cache entry with its `encoder_id`; this guard is the gate before any similarity op.

## Blue-green reindex

When you upgrade the encoder, reindex the collection — the switch is verified and atomic:

```bash
# Bootstrap the collection's current encoder:
exa embedding set-encoder docs nomic-embed-text-v1.5-9f2c8a1b4e6d

# Reindex to a new encoder, verifying recall before switching:
exa embedding reindex docs nomic-embed-text-v2.0-<id> --corpus-size 10000 \
    --recall 0.97 --recall-floor 0.9
# Reindexed docs → …v2.0… (recall 0.970) — switched atomically; old index pruned; drift rebaselined
```

The lifecycle:

1. **Build** a staging index with the new encoder — the old index stays **active and
   retained** (R5 / GWT-4).
2. **Re-embed** the corpus and **verify recall** against the floor.
3. If recall ≥ floor → **atomic switch** to the new encoder, then **prune** the old index.
4. If recall < floor → **abort**: keep the old index active, drop the staging index.
5. **Rebaseline** input-embedding drift (C5) — the space changed (R6 / GWT-5).

```bash
exa embedding reindex docs <new-id> --recall 0.5 --recall-floor 0.9
# Reindex NOT switched: recall 0.500 < floor 0.900 — kept old index
```

## Governance

- **Audited (D4):** switches and aborts write `reindex_switched` / `reindex_aborted` events.
- **Per-tenant (D6):** each tenant's copy of a collection reindexes independently; the
  active/staging encoder is tracked per `(collection, tenant)`.

```bash
exa embedding status docs
# collection      docs
# active_encoder  nomic-embed-text-v2.0-…
# staging_encoder —
# status          active
```

## Related

- **B3 / B4 / B5** — the cache / RAG / vector store whose embeddings this governs.
- **C5** input-embedding drift — rebaselined after a reindex.
- **D4** audit / **D6** tenancy — encoder changes are audited and tenant-scoped.
