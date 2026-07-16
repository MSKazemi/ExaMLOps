# Fairness & Subgroup Performance Monitoring (C8)

> Next-Gen 40 · feature **C8** · ADR 0025 · spec `design/vision/specs/C8-fairness-subgroup-monitoring.md`

C8 slices model performance by declared attributes, computes standard group-fairness
disparities, and can **gate promotion** (C3) or **alert** (C6) when a disparity exceeds a
threshold. The numbers also feed A6 model cards and D1 EU-AI-Act reports.

## Declaring slices

Declare the attributes to slice by, the disparity threshold, and (optionally) the
promotion gate:

```bash
exa fairness config JPCP --attr region --attr tier --threshold 0.1 --min-samples 30 --gate
```

- `--attr` (repeatable) — the categorical/binned attributes to slice by (protected
  attributes where applicable, handled under D6/D7/D8).
- `--threshold` — the maximum allowed disparity (default 0.1).
- `--min-samples` — the **noise guard**: slices with fewer samples are excluded from
  disparity and alerting (R3).
- `--gate` — block promotion when disparity exceeds the threshold.

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

Fairlearn's `MetricFrame` is used when installed; otherwise a pure-Python implementation
computes the same per-slice performance and the standard fairness metrics against
`platform_db.fairness_samples`. No external service is required.

## See also

- [Evaluation gate (C3)](evaluation.md) — the promotion mechanism fairness feeds.
- [SLOs (C6)](slos.md) — where a fairness SLI/alert can live.
- Model cards (A6) and the EU-AI-Act report (D1) consume these subgroup numbers.
