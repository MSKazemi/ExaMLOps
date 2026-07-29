# Synthetic Data Generation (A7)

> Next-Gen 40 · feature **A7** · ADR 0042 · spec `design/vision/specs/A7-synthetic-data-generation.md`

Real HPC telemetry is often imbalanced (rare failure/edge classes), privacy-constrained, and
scarce for new scenarios. A7 lets you generate **synthetic** datasets from a real dataset
revision, gate them on **fidelity + privacy**, and flag their provenance so synthetic data can
**never pass as real**. Use them for class balancing, rare-event augmentation, richer eval sets,
and privacy-safer sharing.

## Quick start

```bash
# 1. Fit + generate + gate + record, writing the released parquet to a directory
exa data synth generate FData --path ./data/FData --rows 1000 --out ./data/FData_synth

# 2. Score an existing synthetic set against the real data (exits non-zero if the gate fails)
exa data synth evaluate FData --real ./data/FData --synthetic ./data/FData_synth

# 3. Just fit and report what the generator learned (smoke-check)
exa data synth fit FData --path ./data/FData
```

`exa data synth generate` runs the whole flow: fit → generate `--rows` records → evaluate
fidelity + privacy → **gate** → if released, write the parquet (when `--out` is given) and record
it as a synthetic A1 revision with A2 lineage. If the gate blocks it, the command exits non-zero
and records the blocked attempt for auditability (nothing is released).

## SDV optional, fallback always works

The heavy generator library (**SDV** — Gaussian Copula / CTGAN / TVAE) is an **optional extra**:

```bash
pip install "examlops[synth]"     # enables SDV-backed synthesizers
```

Without it, a dependency-free **Gaussian-copula fallback** is used automatically: empirical
marginals + a rank-correlation copula for numeric columns, empirical frequencies for categoricals,
and bootstrap resampling for list/embedding columns. This keeps every subcommand — **including the
fidelity/privacy release gate** — fully functional offline (laptop, CI, tests), the same
graceful-degradation pattern as A1 data versioning (lakeFS → content-hash). `exa data synth fit`
reports which backend is active (`sdv` or `fallback`).

## The fidelity + privacy gate

A synthetic dataset is releasable only when it clears **both** floors (spec R3). Both metrics are
computed even in the fallback path, so the gate is never a silent no-op.

| Metric | What it measures | Score |
|---|---|---|
| **Fidelity** | Per-column distribution match (KS for numerics, total-variation for categoricals) + correlation-structure preservation | 1.0 = matches real |
| **Privacy** | Distance-to-closest-record (are synthetic rows as far from real rows as real rows are from each other?) + exact-match memorisation signal | 1.0 = safe, 0.0 = memorised |

- A **low-fidelity** set (noise unrelated to the real data) fails the fidelity floor and is blocked.
- A **memorising** generator (emitting near-copies of real records) scores ~0 privacy and is
  flagged and blocked — this is the core governance hazard the gate exists to prevent.

Tune the floors per use-case:

```bash
exa data synth generate FData --path ./data/FData --rows 500 --min-fidelity 0.7 --min-privacy 0.6
```

The fidelity-vs-privacy trade-off is real (memorised data has perfect fidelity but terrible
privacy; noise has good privacy but terrible fidelity) — the gate requires **both**.

## Provenance & governance

Every released synthetic dataset is:

- an **A1 dataset revision** flagged `synthetic=true` (it can never be silently used as real),
- linked by an **A2 lineage edge** to its source real revision + the generator config,
- recorded with its fidelity/privacy scores in the `synthetic_datasets` gate-record table,
- **audited** (`synth_generated` / `synth_blocked` / `synth_evaluate` events).

Two primitives support policy control of synthetic data in training:

- `synthetic_proportion(revision_ids)` — the fraction of a training run's revisions that are synthetic.
- `is_synthetic_only(revision_ids)` — `True` when a model was trained on synthetic data alone.

A D5 policy can call `is_synthetic_only` to **forbid promoting a synthetic-only model**, and model
quality should always be reported on a **real holdout** (spec R5).

### Synthetic-only promotion gate

`examlops.promotion_gates` closes this loop end-to-end. It resolves a model's training dataset
revisions from its A2 lineage and reports whether they are synthetic-only:

- **Manual promotion** — set `EXAMLOPS_SYNTHETIC_ONLY_GATE=1` and `exa pipeline promote` refuses a
  synthetic-only model (audited; `--force` overrides, also audited), mirroring the SLO/fairness gates.
- **Autopilot** — the self-driving loop passes `synthetic_only` into the `autopilot_promote` policy
  context, so a D5 policy rule forbids auto-promotion:

  ```yaml
  policies:
    - name: no-synthetic-only-promotion
      action: autopilot_promote
      when: "synthetic_only == True"
      effect: deny
  ```

The gate **fails open** on missing lineage — a model with no recorded training provenance is never
blocked, so the gate only ever acts on positive evidence of synthetic-only training.

## Determinism

Generation is fully seeded: the same real data + method + `--seed` yields byte-identical synthetic
records and the same content revision id, so a run can be reproduced exactly.

## Notes & non-goals

- Embedding/list columns are preserved by bootstrap resampling; embedding *drift* is out of scope
  for A7 (see C5 profiling / B6 embedding lifecycle).
- Data-contract validation (A5) and profiling (C5) still apply to synthetic output — run
  `exa data validate` on the generated parquet to enforce the schema contract.
