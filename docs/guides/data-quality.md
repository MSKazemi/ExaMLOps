# Data contracts & quality gates

A **data contract** is a versioned, machine-checked declaration of a dataset's
expected schema and semantics. ExaMLOps enforces contracts at two gates — before
training (fail-closed) and before serving (reject malformed requests) — so bad data
can't silently degrade a model or crash the inference server.

Design: ADR 0005 · spec `design/vision/specs/A5-data-contracts.md`.

## Defining a contract

Contracts are Python, versioned under `pipelines/contracts/<dataset>.py`, exporting a
`CONTRACT` object. Each check carries a severity — `error` (fails the gate) or `warn`
(recorded, non-blocking):

```python
from pipelines.contracts import (
    DataContract, column_present, categorical, in_range, not_null, embedding_dim, min_rows,
)

CONTRACT = DataContract(dataset="FData", version="1", checks=[
    column_present("pclass", dtype="object"),
    categorical("pclass", ["memory-bound", "compute-bound"]),
    in_range("mbwidth", low=0.0),
    not_null("mbwidth", max_null_rate=0.05, severity="warn"),
    embedding_dim("embedding", 384),
    min_rows(1),
])
```

Supported checks: column presence + dtype, nullability (max null-rate), numeric range,
categorical domain, exact embedding dimensionality, minimum row count.

## The training gate

Before training, validate the A1-pinned data against its contract. An `error`-severity
violation **fails closed** (non-zero exit) and the run does not train; `warn` violations
are recorded but don't block. Every run writes a `data_quality_checks` row (dataset,
revision, pass/fail, per-check results, quality score, actor).

## The inference gate

`validate_request(payload, required=..., embedding_field=..., embedding_dim=...)` checks
each request payload. It **never raises** — a malformed payload returns `(False, errors)`
so the ingress responds 4xx instead of 5xx, and bad payloads are counted for C5 monitoring.

## CLI

```bash
exa data validate FData --path ./data/FData                 # exit 1 on error violations
exa data validate FData --path ./data/FData --revision <r>  # tie the check to an A1 revision
exa --json data validate FData --path ./data/FData          # machine-readable per-check results
```

`exa data validate` is safe to wire as a **blocking GitLab CI step** before promotion:
a corrupted dataset makes it exit non-zero.

## Quality score & SLOs

Each validation yields a quality score (fraction of checks passed), persisted for C6
model-quality SLOs and available as EU AI Act Art. 10 data-governance evidence.
