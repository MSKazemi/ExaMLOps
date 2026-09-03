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

`training_flow` validates the A1-pinned data against its contract between data extraction and
job submission. An `error`-severity violation **fails closed** — the run raises
`DataContractViolation` and never trains; `warn` violations are recorded and continue. Passing
runs write a `data_quality_checks` row (dataset, pass/fail, per-check results, quality score).

It validates the data **the revision resolver pinned for this run**, not whatever happens to be
on disk, so the gate and the recorded provenance describe the same rows.

```bash
EXAMLOPS_DATA_CONTRACT_GATE=enforce   # default — fail closed
EXAMLOPS_DATA_CONTRACT_GATE=warn      # record the violation, keep training
EXAMLOPS_DATA_CONTRACT_GATE=off       # skip entirely
```

An unrecognised value falls back to `enforce`: a typo must not quietly disable a gate whose
whole point is failing closed.

### Three things that are not violations

The gate reports each with a reason instead of failing, because **a gate that records nothing
when it could not run is indistinguishable from one that passed**:

| Situation | Why it is not a violation |
|---|---|
| The dataset has no contract | Most do not. Absence of a contract is not a breach of one. |
| The pinned location is not readable here | The resolver may return a remote or unmaterialised URI; that is an environment fact, not a data defect. |
| `--dummy` run | Synthetic rows were never meant to satisfy a production contract. |

## The inference gate

`InferencePipelineIngress` validates every request through
`validate_request(payload, required=..., embedding_field=..., embedding_dim=...)`. It **never
raises** — a malformed payload returns `(False, errors)` so the ingress answers 4xx rather than
5xx.

Set the embedding width to have it checked:

```bash
EXAMLOPS_INFERENCE_EMBEDDING_DIM=384
```

Unset, no width check runs. It is **never defaulted**: an embedding width is a fact about a use
case, not a platform constant, and a wrong guess would reject every legitimate request.

Two deliberate limits:

- **The request contract is not derived from the dataset contract.** A `DataContract` describes
  training *columns*, and a request is not a row of the training table — deriving one from the
  other would be a guess wearing a contract's name.
- **A replica that cannot import the contract package degrades** to a required-field presence
  check rather than refusing everything. Rejecting every request because a *validator* is
  missing is a worse failure than the one the gate prevents.

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
