# RAG pipeline & retrieval ops (B4)

A first-class **Ingest + Query** RAG pipeline over the B5 vector store, with gateway-routed
generation (B2) using a versioned B1 prompt, a RETRIEVER span (C1), pluggable reranking,
retrieval-quality eval (C2), per-tenant isolation (D6), KB versioning against A1 source
revisions, and D8 guardrails over untrusted retrieved content.

Design: ADR 0019 · spec `design/vision/specs/B4-rag-pipeline.md`.

## Encoder compatibility

A `RagPipeline` names the encoder behind its `embed_fn` with `encoder_id` — `token-hash` for the
built-in embedding. `ingest` stamps the KB's vector collection with it (`encoder="…"` overrides the
stamp), and `query` asks the store under the pipeline's own `encoder_id`, so a query embedded by a
different encoder than the one that built the KB is refused rather than answered:

```python
RagPipeline(embed_fn=minilm, encoder_id="minilm@v1").ingest("runbooks", docs)
RagPipeline(embed_fn=e5, encoder_id="e5@v2").query("runbooks", "…")   # EncoderMismatch
```

Until 2026-09-10 `query` asked under the KB's *recorded* encoder instead — the collection compared
with itself — so the check could not fire. A pipeline with a custom `embed_fn` and no
`encoder_id` still falls back to that record, since it has no identity of its own to claim.

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

- **Retrieval mode:** `--retrieval dense` (the default) searches by embedding only.
  `--retrieval hybrid` adds a BM25 channel over the chunk text and fuses the two rankings
  (`--fusion rrf`, the default, or `convex`). Use hybrid when questions name exact identifiers,
  such as a job id, an error code or a model name. An embedding blurs those, and BM25 ranks them
  first. Knowledge bases ingested before hybrid search existed work as they are, because the
  chunk text was always stored with each chunk. The method is on
  [Hybrid retrieval](../algorithms/hybrid-retrieval.md).

  ```bash
  exa rag query handbook --question "why did JPCP-4711 fail?" --retrieval hybrid
  ```

- **Rerank (R2/GWT-3):** over-retrieves then reranks. The default `lexical_reranker` orders
  by question↔chunk overlap; swap in a cross-encoder by passing `reranker=` to `RagPipeline`.
- **RETRIEVER span (R4):** each query emits a C1 span carrying the query, retrieved doc ids,
  and scores.
- **Citations (R8):** the answer cites the exact chunks used (`doc_id#chunk`).
- **Guardrails (R7/D8):** retrieved content is untrusted — injection patterns are flagged and
  neutralized before the context reaches the model, so a poisoned document cannot hijack the
  prompt. Install a richer guardrail with `examlops.rag.set_guardrail(fn)`.

## Chunking framework (native · LlamaIndex)

Which library cuts a document into chunks is a swappable seam (`examlops.rag.frameworks`):

| `--framework` | Chunker | Needs |
|---|---|---|
| `native` (default) | overlapping word window — every KB ingested before the seam existed | nothing |
| `llamaindex` | LlamaIndex `SentenceSplitter`: packs whole sentences, never cuts mid-sentence | `pip install 'examlops[rag-llamaindex]'` |
| `auto` | `llamaindex` when installed, else `native` | — |

```bash
exa rag ingest handbook --docs ./docs.jsonl --framework llamaindex
EXAMLOPS_RAG_FRAMEWORK=auto exa rag ingest handbook --docs ./docs.jsonl
```

`chunk_size`/`chunk_overlap` are in **words** for both, and the LlamaIndex splitter is given a word
tokenizer and a regex sentence splitter, so nothing (tiktoken encodings, NLTK data) is downloaded at
ingest time — it works on an air-gapped login node. An explicit `--framework llamaindex` on a host
without the library **fails** instead of silently chunking differently: two ingests of one corpus
must not produce different chunk ids. Only `auto` degrades. The framework used is recorded in the
`rag_ingest` audit event. Only chunking goes through LlamaIndex; embedding, indexing, retrieval and
generation stay on the platform's own B5/B2 seams.

## Structured, grounded answers (B8)

```bash
exa rag query handbook --question "how does promotion work?" --structured
```

`--structured` (`query(..., structured=True)`) asks the model for
`{"answer", "citations": [chunk numbers], "insufficient_context"}` and produces it through B8
`generate_structured`: validated against the schema, repaired once if invalid, and metered as
valid/repaired/failed in `structured_output_events`. Then the RAG-specific check: every cited
number must name a chunk that was actually retrieved. Out-of-range, duplicate or non-integer
citations are dropped and listed in `dropped_citations`; `grounded` is true only when a valid
citation survives, or when the model says the context was insufficient. A prose answer (a model
that ignored the instruction, or the offline echo generator) is repaired to `citations: []` and
reported **ungrounded** — visible, not an error.

## Retrieval-quality eval (C2, Ragas)

```bash
# qa.jsonl: {"question": "...", "relevant_ids": ["runbook-7", "faq-2#0"]} per line
exa rag eval handbook --items ./qa.jsonl -k 5 --alias Production     # record the baseline
exa rag ingest handbook --docs ./docs-v2.jsonl --source-revision rev2
exa rag eval handbook --items ./qa.jsonl -k 5                          # candidate = rev2
exa eval gate set rag:handbook --suite rag-retrieval \
    --metric context_recall:max_drop=0.05 --higher-is-better
exa eval gate run rag:handbook rev2                                    # exit 1 on a regression
```

`exa rag eval` retrieves (no generation is paid for) for every labelled question and scores
`context_precision` / `context_recall` as C2 evaluators (`examlops.evaluation.rag_metrics`),
persisting them to `eval_suite_results` as model `rag:<kb>` (`rag:<tenant>/<kb>` outside the
default tenant) with the KB's A1 source revision as the version — so the C3 gate can block a
re-ingest whose retrieval got worse. `--backend`:

- `ragas` — Ragas' `IDBasedContextPrecision`/`IDBasedContextRecall`
  (`pip install 'examlops[rag-eval]'`, in an environment without the `dataplane-files` extra —
  ragas' `datasets` dependency caps fsspec below the dataplane floor). These are Ragas'
  **non-LLM** metrics: no judge model, no paid API.
- `native` — the built-in set-membership math (`examlops.rag.context_precision/recall`, routed
  through the `rag_quality` provider when one is configured).
- `auto` (default, `EXAMLOPS_RAG_EVAL_BACKEND`) — Ragas when installed. Both backends give the same
  number on every non-empty input, so installing Ragas moves where a score is computed, not the
  score. On an empty input Ragas returns NaN; it is recorded as `0.0` with `undefined` in the detail.

Relevance may be labelled per document (`runbook-7`) or per chunk (`runbook-7#0`); with document
labels a document retrieved as several chunks counts once. A question set is capped at 2000 items.
Re-running the same set on an unchanged KB is idempotent (same `run_id`).

The plain functions stay available:

```python
from examlops.rag import context_precision, context_recall

context_precision(retrieved_ids, relevant_ids)   # fraction retrieved that are relevant
context_recall(retrieved_ids, relevant_ids)       # fraction relevant that were retrieved
```

## Serving endpoint

`serving/rag_pipeline/app.py` hosts the pipeline over HTTP (`pip install 'examlops[rag-service]'`):

```bash
export EXAMLOPS_RAG_TOKENS="*:$(openssl rand -hex 24),acme:$(openssl rand -hex 24)"
uvicorn serving.rag_pipeline.app:app --host 127.0.0.1 --port 18011        # source checkout
uvicorn --factory examlops.rag.service:create_app --port 18011            # installed wheel
```

| Route | Purpose |
|---|---|
| `POST /v1/rag/query` | `{kb, question, k≤50, retrieval, fusion, structured, prompt_label, tenant?}` → answer, citations, `guardrail_flagged`, and `structured`/`grounded` when asked |
| `POST /v1/rag/ingest` | `{kb, docs[≤1000], source_revision?, framework?, tenant?}` — audited `rag_ingest` |
| `GET /v1/rag/kbs` | the caller's tenant's KBs (tenant filtered in SQL, `limit` ≤ 500) |
| `GET /healthz` · `/readyz` · `/metrics` | liveness · tokens + vector store · Prometheus |

- **Auth fails closed.** `EXAMLOPS_RAG_TOKENS` is `tenant:token,…` (tenant `*` may act for any
  tenant); `EXAMLOPS_RAG_TOKEN` is shorthand for `*:<token>`. Placeholder or <16-character tokens
  are discarded at startup; with none usable every call is 503 `rag_service_unconfigured` and
  `/readyz` is not ready. A tenant-bound token asking for another tenant gets 403 and a
  `rag_tenant_denied` audit event.
- **D8 on three surfaces.** The question goes through the gateway guardrail
  (`EXAMLOPS_GUARDRAIL_MODE`, monitor by default; enforce blocks injection → 400). Retrieved content
  is untrusted: it is scanned per tenant in **enforce** mode by default
  (`EXAMLOPS_RAG_CONTEXT_GUARD=monitor` records instead, and injection text is still neutralised).
  The answer is scanned on the way out (toxic → 422, PII redacted in enforce).
- **Bounded.** `EXAMLOPS_RAG_MAX_BODY` (8 MiB → 413), `EXAMLOPS_RAG_MAX_CONCURRENT` (8 → 429; a
  slot is held until the worker thread finishes, not until the caller gives up) and
  `EXAMLOPS_RAG_TIMEOUT` (30 s → 504).
- **Errors** are `{"error": {"code", "message"}}`: `kb_not_found` 404, `encoder_mismatch` 409,
  `framework_unavailable` 501, `structured_output_failed` 502.
- **Metrics:** `examlops_rag_requests_total{endpoint,outcome}`, `examlops_rag_request_seconds`,
  `examlops_rag_guardrail_flags_total{stage}`, `examlops_rag_retrieved_chunks`.

Not yet shipped: a Compose service / Helm chart for the endpoint and a KServe (E1) rendering of it
— run it with uvicorn, or behind the platform's own reverse proxy.

## Programmatic

```python
from examlops.rag import RagPipeline

p = RagPipeline()
p.ingest("handbook", [{"id": "d1", "text": "..."}], tenant="acme", source_revision="abc123")
ans = p.query("handbook", "how does promotion work?", tenant="acme", k=3)
# hybrid retrieval: RagPipeline(retrieval="hybrid", fusion="rrf")
# ans.answer, ans.citations[i].doc_id/score, ans.guardrail_flagged, ans.retrieval_span_id
```

KBs are re-indexable on encoder change via B6 (`exa vector reindex <kb>`), preserving recall.
