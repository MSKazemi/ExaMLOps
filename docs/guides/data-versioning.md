# Data versioning & reproducibility

ExaMLOps pins the **data** a training run used, the same way it already pins code
(git SHA) and models (MLflow versions). Every run resolves an immutable
*dataset revision* before training, records it, and tags the MLflow run — so a
re-run can reproduce the exact data, rollback is possible, and EU AI Act Art. 10
data-governance evidence is available.

See ADR 0003 for the design decision and `design/vision/specs/A1-data-versioning.md`
for the normative spec.

## How a revision is resolved

A `DatasetRevision` is resolved by one of two strategies, behind one value object:

| Strategy | When | `revision_id` | `kind` |
|---|---|---|---|
| **lakeFS** (preferred) | `EXAMLOPS_LAKEFS_ENDPOINT` is set | lakeFS commit id | `lakefs` |
| **content-hash** (fallback) | otherwise | `sha256(sorted file digests ‖ schema)` | `content` |

The content hash is **deterministic** across runs on identical data and
**order-independent** across the file set. Resolution is **fail-open**: if a
revision can't be computed, the run still completes and records
`revision_id="unknown"` with a warning — availability is never sacrificed for
provenance.

Revisions are stored in the shared `platform.db` `dataset_revisions` table
(idempotent on `(backend, dataset, revision_id)`) and the creating MLflow run is
tagged `dataset_revision`, `dataset_backend`, `dataset_uri`.

## CLI

```bash
# Record the current state of a dataset as a revision.
exa data snapshot FData --backend minio --path ./data/FData

# List recorded revisions, newest first, with the runs that produced them.
exa data list FData
exa --json data list FData          # machine-readable

# Compare two revisions: row-count delta, schema change, size delta.
exa data diff FData <revA> <revB>

# Verify local data matches a pinned revision (exit code 1 if it doesn't).
exa data checkout FData <rev> --path ./data/FData
```

## Pinning a training run

```bash
# Default run — resolves "latest" and records the resulting revision automatically.
exa pipeline run --model JPCP --dataset FData --backend minio

# Reproduce an exact past run by pinning its recorded revision.
exa pipeline run --model JPCP --dataset FData --dataset-revision <rev>
```

`--dataset-revision` requires `--dataset` and exits non-zero if the revision was
never recorded (it can't be materialised). When pinned, the revision id is passed
to the pipeline via `EXAMLOPS_DATASET_REVISION` and tagged on the MLflow run.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `EXAMLOPS_LAKEFS_ENDPOINT` | unset | lakeFS API endpoint; when set, revisions are lakeFS commit ids |
| `EXAMLOPS_LAKEFS_REPO` | dataset name | lakeFS repository to read commits from |
| `EXAMLOPS_LAKEFS_REF` | `main` | lakeFS branch/ref to resolve the latest commit on |
| `EXAMLOPS_DATASET_REVISION` | unset | Pin a pipeline run to this revision (set automatically by `--dataset-revision`) |

With lakeFS absent (laptop, CI, tests) the content-hash fallback keeps the whole
feature working with **no configuration**.
