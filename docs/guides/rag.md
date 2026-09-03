# RAG pipeline & retrieval ops (B4)

A first-class **Ingest + Query** RAG pipeline over the B5 vector store, with gateway-routed
generation (B2) using a versioned B1 prompt, a RETRIEVER span (C1), pluggable reranking,
retrieval-quality eval (C2), per-tenant isolation (D6), KB versioning against A1 source
revisions, and D8 guardrails over untrusted retrieved content.

Design: ADR 0019 · spec `design/vision/specs/B4-rag-pipeline.md`.

## Encoder compatibility

`ingest(..., encoder="…")` stamps the KB's vector collection with the encoder that produced its
embeddings, and `query` looks that value up and asks the store under it. A query embedded by a
different encoder is refused rather than answered.

Retrieval is where cross-encoder scoring is most convincing and least detectable: every hit still
arrives with a plausible score and a real citation attached, so nothing about the answer looks
wrong. A KB ingested before the stamp existed records no encoder and still queries — that is the
absence of a check, not a guarantee (ADR 0043).


## Graceful degrade

Every dependency has a fallback, so the full round-trip runs with **no external service**:
no embedding service → deterministic token-hash embedding; no gateway → local echo
generator; vector store → the B5 SQLite fallback.

## Ingest

`load → chunk → embed → index` into a per-tenant B5 collection, versioned against an A1
source revision (R1):

```bash
# docs.jsonl: one {"id": "...", "text": "..."} per line
exa rag ingest handbook --docs ./docs.jsonl --source-revision abc123 --tenant acme
exa rag list
```

## Query

`embed → retrieve → rerank → assemble-context → generate`, citing the retrieved chunks:

```bash
exa rag query handbook --question "how does promotion work?" -k 3 --tenant acme
```

- **Rerank (R2/GWT-3):** over-retrieves then reranks. The default `lexical_reranker` orders
  by question↔chunk overlap; swap in a cross-encoder by passing `reranker=` to `RagPipeline`.
- **RETRIEVER span (R4):** each query emits a C1 span carrying the query, retrieved doc ids,
  and scores.
- **Citations (R8):** the answer cites the exact chunks used (`doc_id#chunk`).
- **Guardrails (R7/D8):** retrieved content is untrusted — injection patterns are flagged and
  neutralized before the context reaches the model, so a poisoned document cannot hijack the
  prompt. Install a richer guardrail with `examlops.rag.set_guardrail(fn)`.

## Retrieval-quality eval (C2)

```python
from examlops.rag import context_precision, context_recall

context_precision(retrieved_ids, relevant_ids)   # fraction retrieved that are relevant
context_recall(retrieved_ids, relevant_ids)       # fraction relevant that were retrieved
```

Wire these into a C2 suite (Ragas-style) to gate RAG changes with C3.

## Programmatic

```python
from examlops.rag import RagPipeline

p = RagPipeline()
p.ingest("handbook", [{"id": "d1", "text": "..."}], tenant="acme", source_revision="abc123")
ans = p.query("handbook", "how does promotion work?", tenant="acme", k=3)
# ans.answer, ans.citations[i].doc_id/score, ans.guardrail_flagged, ans.retrieval_span_id
```

KBs are re-indexable on encoder change via B6 (`exa vector reindex <kb>`), preserving recall.
