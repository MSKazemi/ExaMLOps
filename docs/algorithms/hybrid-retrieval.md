# Hybrid retrieval: BM25 + dense vectors, fused by rank

The vector store answers a query through two channels and fuses the results. The **dense
channel** compares embeddings, so it captures meaning. The **sparse channel** scores words with
BM25, so it catches exact tokens. This page gives the algorithms, their parameters, and the
guarantees the implementation makes. For the commands, see the
[Vector store guide](../guides/vector-store.md).

!!! abstract "At a glance"
    | | |
    |---|---|
    | **Dense channel** | cosine / L2 / inner product over the collection's embeddings; exact scan or ANN index (HNSW, IVFFlat) |
    | **Sparse channel** | Okapi BM25 over each item's text (`k1 = 1.2`, `b = 0.75`); Postgres full-text ranking on pgvector |
    | **Fusion** | Reciprocal Rank Fusion (`k = 60`, default) or a convex combination of normalised scores |
    | **Candidate depth** | each channel contributes `max(4k, 50)` results before fusion |
    | **Determinism** | ties break by item id: the same inputs always give the same ranking |
    | **Code** | `examlops.vector_store` (`sparse.py`, `fusion.py`, `index.py`, `pgvector.py`) · ADR 0020 |

## Why two channels

An embedding model maps a chunk to a point in a space where *similar meaning* is *nearby*. That
is what makes "why did the training job run out of memory" retrieve a chunk that says "the job
was killed by the OOM killer". The same geometry blurs **identifiers**. `JPCP-4711`,
`PM100Dataset`, an error code or a node name are rare strings with no neighbourhood: the
embedding of a chunk that mentions one sits close to every other chunk about failed jobs.

A lexical scorer has the opposite profile. It knows nothing about meaning. But it rewards a rare
exact token heavily, because rarity is exactly what inverse document frequency measures.

On an operations platform, questions name identifiers constantly. Hybrid search runs both
channels and lets each promote what the other misses:

```text
                ┌───────── dense channel ─────────┐
 query vector ──► ANN / exact scan (metric)       ├──► top-N ids ─┐
                └─────────────────────────────────┘               │    ┌──────────┐
                                                                  ├───►│  fusion  ├──► top-k hits
                ┌───────── sparse channel ────────┐               │    │rrf/convex│    (+ per-channel
 query text  ───► BM25 over item texts            ├──► top-N ids ─┘    └──────────┘     ranks)
                └─────────────────────────────────┘
          both channels apply the metadata filter and tenant scope before ranking
```

## The sparse channel: Okapi BM25

For a query \(Q\) and a document \(D\), BM25 (Robertson & Zaragoza, 2009) scores

$$
\operatorname{BM25}(D, Q) \;=\; \sum_{q \in Q} \operatorname{idf}(q)\cdot
\frac{f(q, D)\,(k_1 + 1)}{f(q, D) + k_1\left(1 - b + b\,\dfrac{|D|}{\operatorname{avgdl}}\right)}
$$

where \(f(q, D)\) is the frequency of term \(q\) in \(D\), \(|D|\) is the document length in
tokens, and \(\operatorname{avgdl}\) is the mean document length in the collection. Two
parameters shape the score:

- \(k_1 = 1.2\) sets how quickly repeated occurrences of a term stop adding score (term-frequency
  saturation).
- \(b = 0.75\) sets how strongly long documents are penalised (length normalisation).

Both are the standard defaults (Manning, Raghavan & Schütze, 2008, §11.4.3).

The inverse document frequency is the form Lucene has used since version 6:

$$
\operatorname{idf}(q) \;=\; \ln\!\left(1 + \frac{N - n(q) + 0.5}{n(q) + 0.5}\right)
$$

Here \(N\) is the number of documents and \(n(q)\) is the number that contain \(q\). The
`1 +` inside the logarithm matters. The textbook Robertson–Spärck Jones weight
\(\ln\frac{N - n + 0.5}{n + 0.5}\) is **negative** for a term in more than half the corpus, and a
negative weight would rank a document *lower* for containing a query term. The unit tests pin
this down (`test_bm25_idf_stays_positive_for_a_term_in_every_document`).

**Tokenisation** is lower-cased Unicode word characters, with no stemming and no stop words.
Stemmers are language-specific, the platform's corpora mix English, Italian and identifiers, and
a stemmer would mangle the identifiers that make this channel worth having.

**Corpus statistics are exact.** On the SQLite store, \(N\), \(n(q)\) and
\(\operatorname{avgdl}\) are computed over the whole filtered collection at query time, so a
filtered search gives BM25 scores relative to the filtered set.

**On pgvector** the lexical channel uses Postgres full-text search. It runs over a generated
`tsvector` column with a GIN index, and is ranked by `ts_rank_cd` (cover density). The query is
an OR of the query's tokens, not an AND, because this is a recall channel. A chunk that contains
the rare identifier but not every other word of a long question must still be a candidate. The
absolute scores differ from BM25's; fusion is designed so that does not matter (next section).

## Fusion

A cosine similarity lives in \([-1, 1]\). A BM25 score is unbounded and grows with query length.
Adding the two raw scores would let whichever happens to be larger decide the ranking. Both
fusion methods avoid that.

### Reciprocal Rank Fusion (default)

$$
\operatorname{RRF}(d) \;=\; \sum_{c \,\in\, \{\text{dense},\,\text{sparse}\}}
\frac{1}{k + \operatorname{rank}_c(d)}, \qquad k = 60
$$

\(\operatorname{rank}_c(d)\) is the 1-based position of \(d\) in channel \(c\). A document absent
from a channel gets no term from it. RRF uses **ranks only**, so the scale problem disappears
entirely. The constant \(k = 60\) is from the paper that introduced it (Cormack, Clarke &
Büttcher, SIGIR 2009); it damps the influence of the top few positions. RRF needs no tuning and
no labelled data, which is why it is the default.

Two rows from a worked example. The embedding ranks the incident report 5th, and BM25 ranks it
1st because it is the only chunk containing `JPCP-4711`:

| chunk | dense rank | sparse rank | RRF |
|---|---|---|---|
| guide  | 1 | 2 | \(\tfrac{1}{61} + \tfrac{1}{62} = 0.03252\) |
| incident | 5 | 1 | \(\tfrac{1}{65} + \tfrac{1}{61} = 0.03178\) |

Fused, the incident report scores within 3% of the best semantic match (0.03178 against
0.03252). Dense search alone ranked it 5th, outside a top-3 cut.

### Convex combination

$$
\operatorname{score}(d) \;=\; \alpha\,\hat{s}_{\text{dense}}(d) + (1 - \alpha)\,\hat{s}_{\text{sparse}}(d)
$$

\(\hat{s}\) is the channel score min-max-normalised over that channel's candidates, and a
document missing from a channel contributes 0 from it. Bruch, Gai & Ingber (ACM TOIS, 2023) found
that a **tuned** convex combination beats RRF both in and out of domain, and needs only a small
labelled sample to tune \(\alpha\). An *untuned* \(\alpha\) is a guess, though. Use convex fusion
once you have relevance judgements to choose \(\alpha\) from; until then, use RRF.

Two edge cases are defined explicitly:

- A channel with a single candidate, or with every candidate tied, normalises to 1.0, not 0.0.
  Otherwise a lone lexical match would contribute nothing.
- \(\alpha = 1\) reproduces the dense ranking exactly, and \(\alpha = 0\) ranks by the sparse channel alone.

### Candidate depth

Fusion can only promote what a channel returned. A chunk ranked 12th by the embedding and 1st by
BM25 is exactly the hit hybrid search exists for, and cutting each channel at \(k\) would lose
it. Each channel therefore contributes \(\max(4k, 50)\) candidates (override with
`--candidates`), in line with the common practice of fusing top-50 to top-100 lists.

## The dense channel and its index

A collection declares its index when it is created, and can change it on `reindex`:

| index | how a query is answered | recall | build | best for |
|---|---|---|---|---|
| `flat` | exact scan of every vector | 1.0 | none | small collections; the SQLite store always answers this way |
| `hnsw` | greedy search over a layered proximity graph | tunable, typically > 0.95 | slower, more memory | the default choice for interactive search |
| `ivfflat` | scan the `probes` nearest of `lists` clusters | tunable | fast, small | large, mostly static collections |

**HNSW** (Malkov & Yashunin, IEEE TPAMI 2020) keeps `m` links per node and uses a candidate list
of `ef_construction` while building. At query time it explores `ef_search` candidates; raising
`ef_search` buys recall at the cost of latency, and it is the knob to turn first. pgvector
requires \(\text{ef\_construction} \ge 2m\), and the store checks this at `create`, before any
index build starts.

**IVFFlat** (Jégou, Douze & Schmid, IEEE TPAMI 2011) clusters the vectors into `lists`
centroids and scans the `probes` closest lists per query. The centroids are trained on the rows
present **when the index is built**, so an IVFFlat index built on an empty table is meaningless.
The store therefore builds it on `reindex`, after loading, and until then `stats` reports
`exact (index declared but not built …)`. Rules of thumb from pgvector: `lists ≈ rows/1000` up to
1M rows, and `probes ≈ √lists`.

| parameter | range | default | effect |
|---|---|---|---|
| `m` | 2–100 | 16 | links per node: recall ↑, memory ↑ |
| `ef_construction` | 4–1000, ≥ 2·m | 64 | build quality ↑, build time ↑ |
| `ef_search` | 1–1000 | 40 | query recall ↑, latency ↑ |
| `lists` | 1–32768 | 100 | number of clusters |
| `probes` | 1–`lists` | 1 | clusters scanned per query: recall ↑, latency ↑ |

ANN indexes are limited to 2000 dimensions on pgvector's `vector` type. A larger collection must
stay `flat` (or have its dimension reduced), and `create` refuses the combination up front.

### Filtered ANN search

An HNSW scan returns about `ef_search` candidates, and a `WHERE` filter runs after the scan. So
before pgvector 0.8, a selective filter returned fewer than `k` rows. When a filter is present,
the store enables pgvector's **iterative scan** (`hnsw.iterative_scan = relaxed_order`, and the
IVFFlat equivalent), which keeps scanning until enough rows pass the filter. Relaxed ordering can
return rows slightly out of order, so the store re-sorts them. On the SQLite store the filter runs
before scoring, so filtered results are exact.

### Reindexing without downtime

`reindex` on pgvector is blue-green:

1. `CREATE INDEX CONCURRENTLY` builds the new index under a staging name. Reads and writes
   continue, and the old index keeps serving.
2. `DROP INDEX CONCURRENTLY` removes the old index.
3. The new index takes over the old name.

A leftover invalid staging index from a crashed attempt is cleared first. A session advisory
lock refuses a second concurrent reindex of the same collection.

## Guarantees and their tests

| guarantee | test |
|---|---|
| BM25 equals a hand-computed value | `test_bm25_matches_hand_computed_value` |
| RRF equals the formula; ties break by id | `test_rrf_matches_the_formula`, `test_ranked_breaks_ties_by_id_deterministically` |
| hybrid finds the identifier that dense search misses | `test_hybrid_finds_the_identifier_dense_search_misses` |
| hybrid with no query text equals dense search | `test_hybrid_with_no_query_text_degrades_to_the_dense_order` |
| pgvector ranks exactly like the SQLite fallback (every metric, with and without a filter) | `test_dense_ranking_matches_the_sqlite_fallback` (live) |
| a filtered HNSW search still returns the matching rows | `test_ann_index_serves_and_is_reported` (live) |
| reindex switches the index type and preserves recall | `test_blue_green_reindex_switches_index_type_and_keeps_recall` (live) |

The live tests run against a real Postgres with pgvector; see the module docstring of
`tests/unit/test_pgvector_store.py` for the one-line container command.

## Complexity

| operation | SQLite store | pgvector |
|---|---|---|
| dense search | \(O(N\,d)\) exact scan | HNSW ≈ \(O(\log N)\) graph hops · IVFFlat \(O(\tfrac{\text{probes}}{\text{lists}}\,N\,d)\) |
| sparse search | \(O(\sum_D \lvert D\rvert)\) tokenisation of the filtered set | GIN index lookup + ranking of the matches |
| fusion | \(O(n \log n)\) over the \(2n\) candidates | same, in Python |

The SQLite store is the dependency-free fallback. Past roughly \(10^5\) items per collection,
move the collection to pgvector.

## References

- S. Robertson and H. Zaragoza. *The Probabilistic Relevance Framework: BM25 and Beyond.*
  Foundations and Trends in Information Retrieval 3(4), 2009.
- C. D. Manning, P. Raghavan and H. Schütze. *Introduction to Information Retrieval*, §11.4.3.
  Cambridge University Press, 2008.
- G. V. Cormack, C. L. A. Clarke and S. Büttcher. *Reciprocal Rank Fusion Outperforms Condorcet
  and Individual Rank Learning Methods.* SIGIR 2009.
- S. Bruch, S. Gai and A. Ingber. *An Analysis of Fusion Functions for Hybrid Retrieval.* ACM
  Transactions on Information Systems 42(1), 2023.
- Y. A. Malkov and D. A. Yashunin. *Efficient and Robust Approximate Nearest Neighbor Search
  Using Hierarchical Navigable Small World Graphs.* IEEE TPAMI 42(4), 2020.
- H. Jégou, M. Douze and C. Schmid. *Product Quantization for Nearest Neighbor Search.* IEEE
  TPAMI 33(1), 2011 (the inverted-file structure IVFFlat uses).
- pgvector documentation: HNSW, IVFFlat, iterative index scans (v0.8.0 and later).
