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

### Where a reindex runs

Re-embedding a large corpus in the calling process blocks whatever asked. Submit it instead:

```bash
exa embedding reindex docs <new-id> --recall 0.97 --scheduler
# submitted to the scheduler as job 41273
exa embedding status docs          # the same reindex row: submitted → switched / aborted / failed

export EXAMLOPS_REINDEX_ORCHESTRATOR=scheduler   # or make it the default
```

`inline` remains the default. A reindex that silently became a cluster submission on upgrade
would strand every existing caller waiting for a result that now arrives elsewhere.

**How the job works.** The reindex runs as a real job on mock, Slurm or Flux. Its generated
script runs `python -m examlops.embeddings.job --job-id N`, which **continues the reindex row
this command opened**, so there is one row per reindex from `submitted` to its outcome. It carries
your `--recall`, `--recall-floor` and `--corpus-size`, so **the recall gate is applied where the
work runs**. Only the job id and those numbers reach the script; the collection and encoder are
read back from the row.

- The job runs only a `submitted` row. A replayed job, or one aimed at a finished reindex, does
  nothing rather than switching an index twice.
- The job does the bookkeeping, so it needs the same datastore as the submitter: a shared
  `platform.db`, or the Postgres backend.
- On Slurm and Flux the command returns once the job is queued. The mock runs jobs only when
  waited on, so there it returns the outcome.

| Situation | Outcome |
|---|---|
| The scheduler refuses the job | The command fails, and the row is `failed` |
| The job cannot start its interpreter (mock) | The row is `failed`, not left `submitted` |
| No scheduler in this environment | Runs here and records `inline-fallback` (not reindexing at all is the worse answer) |
| A library caller passes a `recall_fn` | Runs here and records `inline-fallback`, because a function cannot travel to a job; pass `recall=` to submit |

Job scripts follow the same rules as asset jobs: mode 0700, shell-quoted, no environment
values, kept under `EXAMLOPS_JOB_SCRIPT_DIR` and never inside the repository. The job is listed
in `exa hpc jobs` as `reindex:<collection>`.

Each job records where it ran, the scheduler job id, how many documents it re-embedded and a
measured `duration_s` — on the aborted path too, since the time was spent either way. A
*submitted* job records **no recall** rather than 0.0: nothing has been measured yet, and 0.0
reads as "verified and terrible". **Monetary cost is not recorded**, because it needs device-hours
this path does not know, and an invented figure is worse than none.

### When the vector store finds a mismatch

A cross-encoder operation still **refuses** — that is what prevents the silent corruption the
compatibility guard exists for. It now also leaves a trail:

```
EncoderMismatch: collection 'docs' was built with encoder 'enc-old' but the vector comes from
'enc-new' … Reindex it: exa embedding reindex docs enc-new
```

and records a **reindex recommendation** visible in `exa embedding status`. It deliberately does
**not** start one: a search that quietly re-embedded a large corpus would turn one query into an
unbounded, unbudgeted job, and the caller asked for a search. Recommendations are idempotent per
collection and target encoder — a mismatched collection is queried many times, and one row per
query would bury the signal.

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
