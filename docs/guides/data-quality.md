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
    fresh_within,
)

CONTRACT = DataContract(dataset="FData", version="1", checks=[
    column_present("pclass", dtype="string"),
    categorical("pclass", ["memory-bound", "compute-bound"]),
    in_range("mbwidth", low=0.0),
    not_null("mbwidth", max_null_rate=0.05, severity="warn"),
    embedding_dim("embedding", 384),
    min_rows(1),
])
```

| Check | Passes when | Notes |
|---|---|---|
| `column_present(col, dtype=None)` | the column exists (and its dtype matches `dtype`) | `dtype="string"` is a *family*: `object` holding only `str`, `string`, or pandas 3's `str` — nulls ignored, categoricals not strings. Any other value matches as a substring of the dtype's name: `dtype="float"` accepts `float32` and `float64` |
| `not_null(col, max_null_rate=0.0)` | the share of nulls is at most the ceiling | an empty column has no nulls; emptiness is `min_rows`'s question |
| `in_range(col, low=None, high=None)` | every non-null value is within `[low, high]` | both ends inclusive; either may be open |
| `categorical(col, allowed)` | every non-null value is in `allowed` | |
| `embedding_dim(col, dim)` | **every** non-null value is a sequence of exactly `dim` numbers | a column with no non-null values fails |
| `min_rows(n)` | the table has at least `n` rows | on a bounded dataplane sample, the table's real row count is judged |
| `fresh_within(col, max_age)` | the newest timestamp is no older than `max_age` (`"24h"`, `"7d"`, a `timedelta`) | naive timestamps are UTC; a value that is not a timestamp fails the check rather than being dropped |

!!! warning "Declare a string column as the string family, not as object"
    pandas 2 reads a parquet string column as `object`; pandas 3 reads it as `str`. A contract that
    matched the spelling `"object"` fails every run on pandas 3 — the FData contract did, until its
    version 2. `"object"` also matches lists and mixed values, which a string column must not hold.

A check that is not on the list can be written by hand — `Check(name, severity, fn)` where `fn`
takes the dataframe and returns `(passed, observed)`.

!!! note "Embedding width is judged on every row"
    Earlier releases' `embedding_dim` read the first row only, so a column whose first embedding had
    the right width and whose others did not passed. Such a ragged column fails later and further
    from its cause, in collate or at the model's input layer. It is now judged row by row, and
    a contract that passed on ragged data will now fail — which is the point of it.

## How a contract is checked

**Pandera is the engine when it is installed** (it is a dependency of the pipelines package), and
the pandas-only engine is the fallback. They are two engines with one verdict: installing Pandera
changes how precisely a failure is reported, never whether data passes.

Every check built with the functions above carries a declarative description as well as a
predicate. The Pandera engine compiles a contract's descriptions into one
`pandera.DataFrameSchema` and validates it *lazily*, so every failure is collected in one pass
instead of stopping at the first:

| Contract check | Compiles to |
|---|---|
| `column_present` | a required `Column`; a dtype through the check's predicate |
| `not_null(…, 0.0)` | a row-level not-null mask |
| `not_null(…, r > 0)` | a table-level check over the null-rate predicate |
| `in_range` | `Check.ge(low)` and/or `Check.le(high)` |
| `categorical` | `Check.isin(allowed)` |
| `embedding_dim` | an element-wise width test, plus "at least one value" |
| `min_rows`, `fresh_within` | a table-level check over the predicate |
| a hand-written `Check` | not compiled — its own `fn` runs |

Pandera returns its failures as a table of *failure cases*, each naming the column, the position
of the check within that column, and — for a row-level check — the row and its value. Each case
is mapped back to the contract check that compiled to it, so the result has the same shape either
way: one entry per contract check, in contract order.

What the Pandera engine adds is **which rows failed**:

```text
range:x          error  ✗  2 row(s) outside [0.0, 10.0] — row 1: -1.0, row 3: -3.0
embedding_dim:e  error  ✗  1 row(s) not 384-dim — row 4: dim 383
not_null:x       error  ✗  4 row(s) null — rows 0, 2, 3, …
```

The count is always exact; at most three rows are quoted. The pandas-only engine reports the same
failures as a summary (`x < 0.0 present (min -3.0)`).

**Why the verdicts cannot drift apart.** Table-level facts have no row to point at, so each is a
Pandera check wrapping the *same* predicate the fallback runs. Row-level checks are Pandera's own,
and `tests/unit/test_data_contract_engines.py` holds them to the fallback check for check — on
every edge case listed there (missing columns, NaN, infinity, both range bounds, non-numeric
values, ragged and scalar embeddings, naive and unparseable timestamps, empty tables) and on 300
seeded random frames. If Pandera itself raises — as opposed to reporting a failing check — the
contract is re-run on the fallback with a warning: an engine fault is not a data verdict, and it
must not become a skipped gate either.

Every verdict records its engine: `exa data validate` prints it, `--json` carries `engine`, and
each `data_quality_checks` row stores it (`pandera`, `python`, or `pandera+python` when one table
of a multi-table snapshot fell back).

```bash
EXAMLOPS_CONTRACT_ENGINE=auto     # default — Pandera when importable
EXAMLOPS_CONTRACT_ENGINE=python   # force the pandas-only engine
```

There is deliberately no setting that *requires* Pandera: the two engines agree, so a missing
library costs precision in a failure report and nothing else.

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

`exa data validate` exits non-zero on an error-severity violation, so it can gate a CI job or a
promotion script as it stands:

```yaml
# in your site's pipeline, before training or promotion
- run: exa data validate FData --path ./data/FData --revision "$DATASET_REVISION"
```

## Contract tests

A contract is code, and code nothing runs drifts. Each contract declares, beside it, the rows it
must **accept** and rows it must **reject**:

```python
from pipelines.contracts import ContractExamples

EXAMPLES = ContractExamples(
    valid=[{"pclass": "memory-bound", "mbwidth": 12.5, "embedding": [0.1] * 384}],
    invalid=[
        ([{"pclass": "io-bound", "mbwidth": 1.0, "embedding": [0.1] * 384}], "categorical:pclass"),
        ([{"pclass": "memory-bound", "mbwidth": -1.0, "embedding": [0.1] * 384}], "range:mbwidth"),
    ],
)
```

Each `invalid` entry names the check that must catch it, so an example cannot pass by failing for
an unrelated reason.

`tests/unit/test_data_contract_examples.py` runs them in CI — the blocking step ADR 0005 clause 4
asks for. It runs every contract's examples through both engines, and then end to end through the
real `exa data validate` on Parquet: exit 0 for the accepted rows, exit 1 for the rejected ones,
and a `data_quality_checks` row with the score either way. Every contract must ship examples; one
that does not fails the guard.

**What this can and cannot prove.** It proves a contract still accepts the data it describes and
still rejects what it must — the failure that a green suite would otherwise hide, and exactly how
the `pclass` dtype check would have refused every row on pandas 3. It cannot say anything about
*your* data: that is what `exa data validate` in your own pipeline is for, and what the training
gate does on every run.

An example of a **mixed** column (a number among the strings) is checked against the contract
directly rather than through the CLI: Parquet gives a column one type, so such a frame exists only
in memory — which a dataplane frame can be.

## Quality score & SLOs

Each validation yields a quality score (fraction of checks passed), persisted for C6
model-quality SLOs and available as EU AI Act Art. 10 data-governance evidence.
