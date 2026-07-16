# Advanced Drift — Concept, Label-Free Performance & Data Quality (C5)

> Next-Gen 40 · feature **C5** · ADR 0022 · spec `design/vision/specs/C5-concept-drift.md`

ExaMLOps already tracks **feature**, **prediction**, and **input-embedding** drift
(`exa drift status`, `exa drift input status`). C5 adds three more detectors and
unifies them all under a single `drift_kind` discriminator so every kind is queryable
and exportable the same way — and concept drift feeds the existing auto-retrain loop.

| `drift_kind` | Detector | Signal |
|---|---|---|
| `feature` | (existing) feature-distribution shift | input feature stats drift |
| `prediction` | (existing) prediction z-score | output distribution drift |
| `input_embedding` | (existing) embedding norm/mean/std | semantic input drift |
| **`concept`** | realized-error mean-shift test | input→target relationship changed |
| **`data_quality`** | schema / null / range / cardinality profile | bad or malformed inputs |

## Concept drift (`exa drift concept`)

As **delayed labels** arrive (via the ground-truth feedback loop, `exa eval feedback`),
the realized error per prediction is tracked over time. A recent window is compared
against a baseline window with a one-sided mean-shift z-test — a significant *increase*
in error means the learned input→target relationship no longer holds.

```bash
exa drift concept JPCP                 # test with the default 50-sample window
exa drift concept JPCP --window 100    # larger recent window
exa --json drift concept JPCP
```

Severity: `OK` (z < 2), `WARN` (2 ≤ z < 3), `CRITICAL` (z ≥ 3). Each run records a
`drift_kind=concept` event.

### Auto-retrain wiring

A **concept-CRITICAL** detection is consumable by the existing auto-retrain trigger,
subject to the same cooldown as prediction drift:

```bash
exa drift auto-retrain enable JPCP --dataset PM100Dataset
exa drift concept JPCP                  # records CRITICAL if the relationship broke
exa drift trigger --dry-run             # concept-CRITICAL models appear here
exa drift trigger                       # fires POST /retrain (cooldown-aware)
```

## Label-free performance estimation (`exa drift estimate`)

Before labels arrive, estimate model performance from prediction **confidence**
(a CBPE-like estimate: for probabilistic outputs, expected accuracy is
`mean(max(p, 1-p))`). A large drop versus a baseline **warns** — it never forces a
retrain, because unconfirmed estimates should not act on their own.

```bash
exa drift estimate Clf --baseline 0.95        # warns if estimated accuracy dropped ≥10%
exa drift estimate JPCP --window 500          # regression models use a stability proxy
```

Estimated and realized values are stored side by side in `perf_estimates` for the
estimated-vs-realized dashboard panel.

## Data-quality profiling (`exa drift profile`)

Profiles recent inference inputs — per-field null fraction, min/max range, and
cardinality — and folds in A5 bad-payload counters. A null-fraction spike escalates
severity and records a `drift_kind=data_quality` event.

```bash
exa drift profile JPCP                          # profile the last 200 inputs
exa drift profile JPCP --last-n 500 --bad-payloads 12
```

Severity from the combined null+bad-payload fraction: `OK` (< 20%), `WARN` (≥ 20%),
`CRITICAL` (≥ 50%).

## Unified events (`exa drift events`)

Every drift kind lands in one table, queryable by kind:

```bash
exa drift events                                # all kinds, newest first
exa drift events --kind concept
exa drift events --model JPCP --kind data_quality
exa --json drift events
```

## Programmatic use

```python
from examlops.drift_advanced import (
    detect_concept_drift, estimate_performance, profile_inference,
)

res = detect_concept_drift("JPCP", window=50)     # DriftResult(drift_kind='concept', ...)
if res.is_critical:
    ...  # elevate / trigger retrain

est = estimate_performance("Clf", baseline=0.95)  # {'estimated': ..., 'warn': True, ...}

prof = profile_inference("JPCP", batch, bad_payloads=0)  # QualityProfile
```

## Graceful degradation

All detectors are pure-Python by default. Evidently, River, NannyML, and whylogs are
optional accelerants — absent them, the built-in mean-shift test, confidence-based
estimate, and null/range profile provide the same signals against `platform_db`.

## See also

- [AgentOps (C4)](agentops.md) — agent-side analytics.
- [Evaluation (C2/C3)](evaluation.md) — quality gates the same signals feed.
- `exa eval feedback` — the ground-truth loop that supplies delayed labels.
