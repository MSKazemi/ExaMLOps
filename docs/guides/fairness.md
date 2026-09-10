# Fairness & Subgroup Performance Monitoring (C8)

> Next-Gen 40 · feature **C8** · ADR 0025 · spec `design/vision/specs/C8-fairness-subgroup-monitoring.md`

C8 slices model performance by declared attributes, computes standard group-fairness
disparities, and can **gate promotion** (C3) or **alert** (C6) when a disparity exceeds a
threshold. The numbers also feed A6 model cards and D1 EU-AI-Act reports.

## Declaring slices

There are two ways to declare a slice registry, and the difference matters.

### In the model YAML (the declaration)

```yaml
# usecases/<pack>/models/<model>.yaml
fairness:
  slices: [region, tier]     # categorical/binned attributes; protected ones where applicable
  threshold: 0.1             # maximum allowed disparity
  min_samples: 30            # noise guard — smaller slices are excluded from disparity
  gate_promotion: true       # block promotion when disparity exceeds the threshold
```

This is the form ADR 0025 decides on: it is version-controlled, reviewed with the model, and
deployed with it, so a fresh database does not lose it. **It is in force immediately** — there is
no apply step between declaring a registry and the promotion gate honouring it.

A malformed block fails CI (`test_yaml_fairness_block_is_valid`). An unknown key is an error
rather than being ignored: `slice:` for `slices:` would parse into a registry declaring nothing,
and a fairness gate over zero attributes passes every model.

### At runtime (the override)

```bash
exa fairness config JPCP --attr region --attr tier --threshold 0.1 --min-samples 30 --gate
```

- `--attr` (repeatable) — the categorical/binned attributes to slice by (protected
  attributes where applicable, handled under D6/D7/D8).
- `--threshold` — the maximum allowed disparity (default 0.1).
- `--min-samples` — the **noise guard**: slices with fewer samples are excluded from
  disparity and alerting (R3).
- `--gate` — block promotion when disparity exceeds the threshold.

### Which one is in force

A runtime row **wins** over the YAML declaration: writing one is a deliberate act by an operator
on a live system, and having the file silently override it would make the CLI and the dashboard's
fairness console look broken.

So ask, rather than assume:

```bash
exa fairness show JPCP        # the registry in force, its source (yaml | db), and any drift
exa fairness apply JPCP       # make the YAML declaration authoritative again (audited)
```

When both exist and disagree, `show` names every field where they differ. The disagreement is
**reported, not resolved silently** — quietly preferring one is how a reviewed declaration and a
live gate come to differ with nobody able to see it.

## Per-slice performance

```bash
exa fairness slice JPCP region
```

| Column | Meaning |
|---|---|
| `Accuracy` | fraction correct (binary classification slices) |
| `Error` | mean absolute error (regression slices) |
| `Sel.Rate` | fraction predicted positive |
| `TPR` / `FPR` | true/false positive rate |
| `Guard` | `low-n` if below the min-sample threshold (excluded from disparity) |

Below the table, the disparities are printed: demographic-parity difference, equalized-odds
difference, selection-rate range, and accuracy range — with a warning if any exceeds the
threshold.

## Full report

```bash
exa fairness report JPCP          # every declared slice attribute
exa --json fairness report JPCP   # machine-readable, for cards/reports/dashboards
```

## Fairness metrics

| Metric | Definition |
|---|---|
| **Demographic-parity difference** | max − min selection rate across eligible slices |
| **Equalized-odds difference** | max of the TPR range and the FPR range |
| **Selection-rate range** | spread of positive-prediction rates |
| **Accuracy range** | spread of per-slice accuracy |

`disparity_exceeded` is true if any of DP-diff / EO-diff / accuracy-range exceeds the
configured threshold.

## As an SLO (C6)

A model's fairness can back an error budget like any other quality signal:

```bash
exa slo set JPCP fair --target 0.99 --source c8 --gate
exa slo ingest JPCP
```

`good` is the declared slice attributes within threshold; `total` is the ones with enough samples
to measure. Attributes below the min-sample guard are **excluded rather than counted as good** —
otherwise a model with no fairness data would score a perfect fairness SLI. See
[SLOs](slos.md#feeding-the-sli).

## Promotion gating (C3)

When `EXAMLOPS_FAIRNESS_GATE_ENABLED` is set and a **gate-flagged** model exceeds its
disparity threshold, `exa pipeline promote` refuses to move the alias:

```bash
export EXAMLOPS_FAIRNESS_GATE_ENABLED=1
exa pipeline promote jpcp --if-rmse-lt 5.0
# → error: Fairness disparity exceeds threshold for JPCP. Use --force to override (audited).
```

`--force` overrides and writes a `fairness_gate_override` audit event (D4); a block writes
`promotion_blocked_by_fairness`.

## Programmatic use

```python
from examlops.fairness import slice_metrics, fairness_disparity, fairness_gate

res = slice_metrics("JPCP", "region")
for s in res.slices:
    print(s.slice_value, s.n, s.accuracy, s.below_min)
print(res.demographic_parity_diff, res.disparity_exceeded)

disparities = fairness_disparity("JPCP", "region")   # dict, for cards/reports
if fairness_gate("JPCP"):                             # used by the C3 gate
    ...
```

## Graceful degradation

With `pip install 'examlops[fairness]'`, Fairlearn's `MetricFrame` computes the per-slice
metrics (accuracy, selection rate, TPR, FPR, and MAE for regression slices). Without it, a
pure-Python implementation computes **the same numbers**: a parity test holds the two engines to
agreement at 1e-12 across randomised slices, including a slice with no positives (TPR is *none*,
not 0) and rows whose label has not arrived yet. Every result records which engine produced it
(`"engine": "fairlearn" | "pure-python"`). No external service is required.

**How samples are scored.** Each sample keeps its prediction and its label together:

- Accuracy, TPR, FPR and MAE use only the samples whose label has arrived. A prediction still
  waiting for ground truth is not a miss.
- Selection rate uses every prediction, because it needs no label.
- A label that arrived for a request with no recorded prediction pairs with nothing, and is not
  scored.

## See also

- [Evaluation gate (C3)](evaluation.md) — the promotion mechanism fairness feeds.
- [SLOs (C6)](slos.md) — where a fairness SLI/alert can live.
- Model cards (A6) and the EU-AI-Act report (D1) consume these subgroup numbers.
