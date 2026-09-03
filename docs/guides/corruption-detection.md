# Corruption detection — is this the data, or the machine?

`exa drift trigger` used to fire an automated retrain on two gates: a z-score above a threshold,
and a cooldown that had elapsed. Silent data corruption perturbs *exactly the statistic that
z-score is computed from*, so a hardware fault produced a high z-score, an autonomous retrain, and
a model trained on corrupt data. Nothing in the path asked whether the anomaly was the data or the
machine.

ExaMLOps now classifies the anomaly before it remediates it. **The remediation is a function of the
class, never of the z-score**, and only one class — `data_drift` — permits an autonomous retrain.

This changes when auto-retrain fires. See [Migration](#migration).

## Why a NaN/Inf guard is not corruption detection

Gate-level fault injection on a production datacenter GPU (arXiv:2605.04213, *The Anatomy of Silent
Data Corruption*; 63 CUDA benchmarks, >3M simulator hours) measured special values (NaN/±INF) at
**1.01%** of silent data corruption, and states the consequence directly: *"Software detection
targeting NaN/±INF captures minimal SDCs."* The same abstract verifies that **single-bit flips are
under 40%** of bit-flip events — so the majority are multi-bit — and that corruption addresses
exhibit periodicity.

This is an operating condition, not an exception. Meta observes SDC on roughly 1 machine in 1,000;
SDC caused **1.4%** of Llama-3 training interruptions; Alibaba measures 3.61‱ across >1M processors.

A guard that catches ~1% of a phenomenon must never be logged, described or reported as
"corruption checked". `detect_corruption()` therefore also computes an **unexpected-zero rate**
against a per-model baseline, and reports a spread-shift statistic alongside it.

## The second axis was already being collected

`input_snapshots` / `input_baselines` record embedding statistics per inference, independently of
the prediction distribution. That gives a second axis, and the two together separate cases that
prediction drift alone cannot:

| Scenario | Input drift | Prediction drift | Correct action |
|---|---|---|---|
| Real data drift | ↑ | ↑ | retrain |
| Hardware corruption / SDC | ≈ 0 | ↑ | **quarantine the node, do not retrain** |
| Model or serving regression | ≈ 0 | ↑ | roll back the deployment |

**Prediction drift without input drift is evidence *against* data drift, not for it.**

## The four classes

`classify_anomaly()` returns exactly one of these, with the remediation it implies:

| Class | Means | Remediation | Autonomous? |
|---|---|---|---|
| `data_drift` | both axes moved, corruption negative | `retrain` | **yes** |
| `suspected_hardware` | corruption signal positive | `quarantine_node` | no |
| `suspected_regression` | outputs moved, inputs held, corruption negative | `rollback_deployment` | no |
| `undetermined` | the signals do not separate | `none` | no |

`undetermined` is a **first-class outcome**, not an error. When there is no corruption baseline, no
input evidence, or the axes conflict, the platform says so, raises an operator event, and takes no
autonomous action. Guessing here costs GPU-hours and ships a model trained on corrupt data.

## Commands

```bash
# The corruption signal per model — NaN/Inf *and* unexpected zeros, never one alone
exa drift corruption status
exa drift corruption status JPCP

# Record what "normal" looks like for this model. Zero rates differ legitimately between
# models, so like `exa drift baseline` this is an explicit, audited act.
exa drift corruption baseline JPCP --reason "post-deploy reference"

# Name the anomaly behind a drift breach
exa drift corruption classify
exa drift corruption classify JPCP

# Measure the detector against injected corruption and publish the rate
exa drift corruption selftest JPCP
```

A suppressed retrain shows up in three places: the command's own `Suppressed` table, a
`drift_events` row of kind `corruption`, and an audit event
(`drift_retrain_suppressed` / `autopilot_retrain_suppressed`) carrying the class and the reason. A
retrain that does not happen leaves no trace of its own, so these are it — and they are also the
denominator that makes "failures per human intervention" mean something.

## What the detector covers — and what it does not

A detector may not be credited with corruption classes it was never tested against. `exa drift
corruption selftest` injects each class into the model's own recent predictions and reports what
was caught:

| Class | Detected by | Sets `suspected_sdc` |
|---|---|---|
| `nullification` (silent zeros) | zero rate vs baseline | **yes** |
| `special_values` (NaN/±Inf) | NaN/Inf rate | **yes** |
| `mantissa_flip` (non-special multi-bit) | `distribution_shift` (reported only) | **no** |

The third row is deliberate. A spread change is not specific enough to corruption to justify
blocking a legitimate retrain, so it is *reported* and never gating. `selftest` will show a
detection rate near zero for it — that is the honest number, and printing it is the point. Coverage
is bounded by what was injected, not by the assumed distribution of the phenomenon.

## Migration

The behaviour change operators notice first: **prediction drift alone no longer fires an
autonomous retrain.** A model with drift snapshots but no input snapshots classifies as
`undetermined`, and `exa drift trigger` reports it under `Suppressed` instead of retraining.

To restore autonomous retraining for a model:

1. Ensure the bridge is running so `input_snapshots` are collected for it.
2. Set the input baseline: `exa drift input baseline <MODEL>`.
3. Set the corruption baseline: `exa drift corruption baseline <MODEL>`.
4. Confirm with `exa drift corruption classify <MODEL>` — a genuinely drifting model should read
   `data_drift`, and `Auto? yes`.

Expect `undetermined` to be common in week one, until baselines settle. That is the intended
trade: a missed retrain costs latency, while a wrong retrain costs GPU-hours *and* ships a model
trained on corrupt data.

## Design

- ADR 0114 — *Detect corruption before diagnosing drift* (requirements G4.2 · G4.3)
- `examlops.corruption` — the statistic, the classifier and the injection harness
- Related: [Judge calibration](judge-calibration.md), which applies the same principle (every
  governance instrument must itself be measured) to LLM judges
