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

## Drift status changes as events

The control plane scores every model's prediction drift every `CONTROL_PLANE_DRIFT_EVAL_SECONDS`
(60) and, when a model's status changes, emits `drift.status_changed`, plus `alert.drift` when the
new status is `WARNING` or `CRITICAL`. It records the last status per model (`drift_status_state`),
so each change is announced once. A model first seen healthy is recorded quietly; a model first
seen already drifting is announced with `previous` null. The record and the events commit
together, and the comparison runs under a write lock, so two control-plane replicas never both
announce the same change. Consumers subscribe on the [event backbone](event-backbone.md) instead
of polling `exa drift status`.

`exa drift status`, `exa drift trigger`, the autopilot and this evaluator share one computation
(`examlops.drift_status`): the last 100 predictions, scored by the site's drift provider
(`exa providers list --domain drift`). Before, the autopilot had its own copy: 50 predictions and
fixed 2.0/3.0 thresholds that ignored a configured provider, so it could disagree with
`exa drift trigger` about the same model.

`DriftEvaluationFailing` alerts when the evaluator keeps failing
([runbook](../runbooks/control-plane.md#driftevaluationfailing)); `exa drift status` still works then,
because it scores on demand.

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

## Scheduling the detectors (`exa drift run-advanced`)

Until this command nothing ran the three detectors: they fired when someone typed them. The
scheduler sweeps every model with recorded predictions and writes `drift_events` with `drift_kind`,
which `exa drift trigger` and the autopilot already consume (a CRITICAL concept event still starts
the cooldown-aware retrain; a label-free estimate still only warns).

```bash
exa drift run-advanced --once --dry-run          # preview a sweep; writes nothing, no switch needed
EXAMLOPS_DRIFT_ADVANCED_ENABLED=1 exa drift run-advanced --once
EXAMLOPS_DRIFT_ADVANCED_ENABLED=1 exa drift run-advanced --interval 600   # loop until stopped
```

- **Kill-switch**: `EXAMLOPS_DRIFT_ADVANCED_ENABLED` (default off). A refused real run is audited
  as `drift_advanced_skipped` and exits 1.
- **Lease**: one scheduler acts at a time (`drift-advanced` lock through `examlops.coordination`,
  cross-host with the Redis coordinator); TTL `EXAMLOPS_DRIFT_ADVANCED_LEASE_TTL`.
- **Cooldown / dedupe**: an event is written when it is the first for its (model, kind, metric),
  when its severity changed, or when an unchanged non-OK event is older than
  `EXAMLOPS_DRIFT_ADVANCED_COOLDOWN` (default 3600 s). A repeating OK is not news. An OK label-free
  estimate is stored at most once per cooldown.
- **Audited**: each real cycle writes `drift_advanced_cycle`.
- A model with too few labels or no recorded inputs is `skipped`; a crashing detector is `failed`
  and never stops the sweep.

The dashboard's Drift page has a **Concept & Quality** tab over `GET /api/drift/events`.

### Detector seam and the River adapter

The concept detector is selectable with `--detector` on `exa drift concept` or
`EXAMLOPS_DRIFT_CONCEPT_DETECTOR`: `builtin` (default, the mean-shift z-test) or `river-adwin`,
which runs River's ADWIN over the error stream and reaches CRITICAL only when ADWIN signals inside
the recent window **and** the error rose. River is imported lazily and is not a dependency; when it
is missing the builtin runs and the event's `detail.detector_fallback` says why. This adapter is
tested against a stand-in `river.drift.ADWIN`, not against River itself. **Evidently, NannyML and
whylogs are not adapted**: the estimate and profile detectors are the pure-Python ones above.

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
