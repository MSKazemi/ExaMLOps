# Offline (batch) inference

`exa offline` runs a registered **predictive** model version over a dataset without going through
an online endpoint (ADR 0149). It loads the model the way a serving replica does, feeds rows through
the same Open Inference Protocol v2 code as `/v2/models/<m>/infer`, and writes the predictions as a
content-addressed dataset with a manifest. It is resumable after a crash and idempotent per key.

```bash
exa offline run --model JPCP --version 7 \
    --input ./jobs.parquet --output ./scores --key score-2026-09
exa offline status off-3f9a…          # also: exa ops status off-3f9a…
exa offline list
exa offline cancel off-3f9a…
```

`--key` is required. It is the job's identity (with `--tenant`): run the same command again and you
get the stored result (`replayed: true`, nothing recomputed) if it finished, or a **resume** if it
did not. The same key with a different model version, input revision or batch size is refused
(`idempotency_conflict`).

## The spec

`--spec file.json` replaces the flags. It is versioned (`schema_version: 1`) and strict: an unknown
key or a wrong type is an error that lists every problem.

```json
{
  "schema_version": 1,
  "kind": "predictive",
  "model": "JPCP",
  "alias": "Production",
  "input":  {"type": "dataplane", "source": "pm100", "table": "jobs", "revision": "latest"},
  "output": {"type": "dataplane", "source": "jpcp-scores"},
  "batch_size": 1000,
  "resources": {"cpus": 4, "gpus": 0, "memory_gb": 0},
  "idempotency_key": "nightly-2026-09-21"
}
```

* **Servable**: exactly one of `version` or `alias`. An alias is resolved once, at run time, to an
  immutable version; the job loads that version, never the alias.
* **Input**: `local` (a Parquet file or directory) or `dataplane` (a table of a snapshot of a
  [dataplane](dataplane.md) source; `latest` is pinned to the exact 64-hex revision and recorded).
* **Output**: `local` (a directory that receives `<revision>/`) or `dataplane` (published as a new
  snapshot of the named source, which a later job or a training run can pin).
* **Columns**: rows are matched to the model's signature by column name; extra columns (ids) are
  ignored. An input missing a signature column is refused up front. Without a signature every column
  is a feature.

## What comes out

`<output>/<revision>/predictions/part-NNNNN.parquet` (one part per batch) plus `_manifest.json`.
Columns: `row_id` (position in the concatenated input), `prediction`, `error` (null when the row
scored). `<revision>` is the same content hash a dataplane snapshot uses over the parts, so identical
predictions have identical revisions. The manifest records the input revision, model name and
version, the engine (`mlflow-pyfunc-oip`, MLflow and ExaMLOps versions), the batch size, the counts and
a per-batch tally (rows, errors, sha256).

A bad row costs one row: a batch that fails as a whole is retried row by row, and only rows that fail
carry an `error` (their `prediction` is null). The run still completes; `--fail-on-errors` turns row
errors into exit code 1.

## Resume, cancel, crash

Each batch is committed (part, then sidecar, both atomically) before the next starts. After a crash
or a failure, the same command skips every batch whose part still hashes to its sidecar and redoes
the rest. A running job holds a lease renewed after every batch (`EXAMLOPS_OFFLINE_LEASE_TTL`); a
second runner is refused (`idempotency_in_progress`) until it lapses, and `exa offline status` shows
`stalled` for a job whose runner died. `exa offline cancel` stops a live run after the batch in
flight (committed batches are kept; re-running resumes); a job nobody is running is cancelled at
once. `exa ops status|wait|cancel` accept an offline job id (`off-…`).

## Cost, lineage, audit

* **Cost**: with `--cpus`/`--gpus` declared, the attempt's declared hours x wall clock are booked in
  the ledger `exa models cost` reads (priced by the cost provider). They are *declared*, not
  metered (`metered: false`); with nothing declared the run says "no resources declared; nothing
  recorded". Per-item cost is in the result when priced.
* **Lineage**: a `COMPLETE` event links the input dataset revision and the model version to the
  output revision (`exa models lineage --impact <input revision>` finds it).
* **Audit**: `offline_completed`, `offline_failed`, `offline_cancel_requested` events.

## What is not built

| Not built | What happens |
|---|---|
| `generative` and `agentic` kinds | Valid in the spec, refused with `kind_not_supported_offline`. They need an offline engine executor (vLLM offline / Ray Data LLM with OpenAI-Batch JSONL) and an agent batch worker. |
| Submission through admission / a scheduler | The run is inline in the calling process (the local path). The spec carries declared resources; nothing reserves quota or places the job. |
| `flexibility_s` > 0 (carbon-aware later start) | Refused. |
| Shard-level resume of a *single* huge batch, work stealing | The unit of resume is a batch. |

`exa serve batch submit` is unrelated and unchanged: it loops the online endpoint.

## Environment

`EXAMLOPS_OFFLINE_LEASE_TTL`, `EXAMLOPS_OFFLINE_WORKDIR` - see [environment variables](../reference/env-vars.md).
