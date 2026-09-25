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
| **`concept`** | realized-error test (z-test, DDM, ADWIN or Evidently) — and a label-confirmed performance estimate | input→target relationship changed |
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

The labelled stream is read in **arrival order** (prediction id), newest 10 000 pairs at most,
so "recent" really is the latest window.

### Choosing the detector

`--detector` (or `EXAMLOPS_DRIFT_CONCEPT_DETECTOR`) picks the test. The event's
`detail.detector` records which one actually ran.

| Detector | What it does | Needs |
|---|---|---|
| `builtin` (default) | one-sided mean-shift z-test of recent vs baseline error | nothing |
| `ddm` | Drift Detection Method (Gama et al., 2004) over the error stream, pure Python | nothing |
| `river-ddm` | River's `DDM`, same thresholds (warm start 30, warn 2σ, drift 3σ) | `examlops[drift-advanced]` |
| `river-adwin` | River's `ADWIN` over the error stream | `examlops[drift-advanced]` |
| `evidently` | Evidently `ValueDrift` two-sample test (K-S p-value for small samples) of the recent error distribution vs the baseline one | `examlops[drift-advanced]` |

DDM works on failures, not magnitudes: an error above the baseline's 75th percentile counts as a
failure, which keeps the test scale-free for regression models. The streaming and Evidently
detectors decide *whether* the relationship changed; the z-score is still recorded as the score.
They reach `CRITICAL` only when they signal drift inside the recent window **and** the recent error
is higher (a model that got better has not suffered concept drift); otherwise the result is at most
`WARN`.

```bash
exa drift concept JPCP --detector ddm
pip install 'examlops[drift-advanced]'
exa drift concept JPCP --detector evidently
EXAMLOPS_DRIFT_CONCEPT_DETECTOR=river-ddm exa drift run-advanced --once
```

A detector whose library is missing, cannot be imported, or fails on the data falls back to
`builtin` and says so in `detail.detector_fallback` (for example
`river-ddm unavailable: river.drift.binary is not installed`).

### Auto-retrain wiring

`exa drift trigger` acts on two concept signals, subject to the same cooldown as prediction
drift:

- **realized error** — a `CRITICAL` from the concept detector above;
- **estimated performance** — a label-free estimate whose drop realized labels confirmed (see the
  next section). An estimate that labels have not confirmed never triggers a retrain, whatever
  severity a stored row claims.

The trigger looks at the newest event from each source, so a newer estimate `WARN` no longer hides
an older realized-error `CRITICAL`. `--json` output and the `drift_auto_retrain_triggered` audit
event carry `signal` (`realized_error` or `estimated_performance`), `severity` and the
`drift_event_id` that fired it.

```bash
exa drift auto-retrain enable JPCP --dataset PM100Dataset
exa drift concept JPCP                  # records CRITICAL if the relationship broke
exa drift trigger --dry-run             # concept-CRITICAL models appear here
exa drift trigger                       # fires POST /retrain (cooldown-aware)
```

## Label-free performance estimation (`exa drift estimate`)

Before labels arrive, estimate model performance. The estimator is selectable with
`--estimator` or `EXAMLOPS_DRIFT_PERF_ESTIMATOR`:

| Estimator | Method recorded | What it does |
|---|---|---|
| `builtin` (default) | `cbpe-like` / `stability-proxy` | expected accuracy is `mean(max(p, 1-p))` for probabilistic outputs; regression uses a spread-based proxy |
| `nannyml` (separate install, see below) | `nannyml-cbpe` / `nannyml-dle` | NannyML CBPE fitted on the labelled history (binary 0/1 labels, both classes present) for probabilistic classifiers; DLE for regression, learning from the numeric input features recorded with each prediction, reported as `1 / (1 + MAE)` |

NannyML needs at least 50 labelled reference rows; with fewer, or without the library, the
builtin runs and `estimator_fallback` says why.

Severity:

- `WARN` — the estimate fell by at least 10 % against `--baseline`. A warning only.
- `CRITICAL` — the same drop **and** the realized score over the most recent `--window` labelled
  predictions (at least 10 of them) fell by at least 10 % too. The event carries
  `confirmed_by_labels: true` and is what `exa drift trigger` acts on.

The realized drop is measured against something in the same units, recorded as `confirm_basis`:

- `realized_reference` — when at least 10 labelled predictions older than the window exist, the
  realized score of that older history (a relative drop, so it means the same for accuracy and for
  a regressor's `1 / (1 + MAE)`);
- `baseline` — otherwise, for probabilistic outputs only, `--baseline` itself (both are accuracies).

A regressor with no labelled history older than the window cannot be confirmed and stays a `WARN`:
its estimate baseline is the spread proxy, which is not comparable with a realized `1 / (1 + MAE)`.
When the estimate recovers, an `OK` estimate event is written straight away (even inside the
scheduler's cooldown), so a cleared `CRITICAL` stops being a retrain signal.

Because `exa drift estimate` and `exa drift concept` write the events that start (or, with an `OK`,
clear) an autonomous retrain, and take their thresholds from the caller, the dashboard CLI console
runs both at the **admin** tier, not the viewer one.

```bash
exa drift estimate Clf --baseline 0.95                       # warns if estimated accuracy dropped ≥10%
exa drift estimate Clf --baseline 0.95 --estimator nannyml   # NannyML CBPE
exa drift estimate JPCP --window 500                         # regression: stability proxy / DLE
```

Estimated and realized values are stored side by side in `perf_estimates`. The dashboard's
**Concept & Quality** tab shows them in an *Estimated vs realized performance* table
(`GET /api/drift/perf-estimates`), with the gap, and "awaiting labels" where none have arrived.

## Data-quality profiling (`exa drift profile`)

Profiles recent inference inputs — per-field null fraction, min/max range, and
cardinality — and folds in the requests the inference ingress **rejected** against the A5
contract. A null-fraction spike escalates severity and records a `drift_kind=data_quality` event.

Every `422` the inference-pipeline ingress returns for a contract violation is counted in
`inference_rejections` (one row per model, UTC minute and reason — `missing_field`,
`embedding_dim` or `invalid`), off the event loop and best-effort: a replica that cannot reach the
platform store still answers the `422`. A `model_name` outside `[A-Za-z0-9_.-]{1,128}` is counted
as `<invalid>`. A well-formed but made-up name still gets its own row, so buckets older than seven
days are deleted as new ones arrive, the scheduler sweeps at most the 100 most-rejected models, and
the ingress waits at most 250 ms for the counter before answering. Rejections count as
fully-null inputs, so a model whose traffic is mostly refused is not reported healthy because the
few rows that got through were clean.

```bash
exa drift profile JPCP                          # last 200 inputs + the last hour's rejections
exa drift profile JPCP --last-n 500 --bad-payloads 12   # override the rejection count
exa drift profile JPCP --profiler whylogs
```

`--profiler` (or `EXAMLOPS_DRIFT_QUALITY_PROFILER`) picks `builtin` (default) or `whylogs`. whylogs
1.6.4 references `numpy.unicode_`, which NumPy 2 removed, so it only imports in an environment
pinned to NumPy < 2 and is not part of the `drift-advanced` extra; elsewhere the builtin runs and
`profiler_fallback` says why. whylogs profiles a table, so a key absent from a row counts as a null
in that column, where the builtin only counts the keys a row carries.

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
scheduler sweeps every model with recorded predictions — and every model with contract
rejections in the lookback window, since a model whose every request was refused has no predictions
— and writes `drift_events` with `drift_kind`, which `exa drift trigger` consumes (a CRITICAL
concept event starts the cooldown-aware retrain; a label-free estimate only does once labels
confirm it).

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
- **Rejections**: the data-quality check folds in the contract rejections of the last
  `EXAMLOPS_DRIFT_ADVANCED_REJECTION_WINDOW` seconds (default 3600).
- The detector, estimator and profiler are the ones selected by their environment variables above.
- A model with too few labels or no recorded inputs is `skipped`; a crashing detector is `failed`
  and never stops the sweep.

The dashboard's Drift page has a **Concept & Quality** tab over `GET /api/drift/events`, naming a
label-free estimate as `estimate` or `estimate (confirmed)`, and the estimated-versus-realized table
over `GET /api/drift/perf-estimates`.

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

All detectors are pure-Python by default. River and Evidently come with
`pip install 'examlops[drift-advanced]'`. NannyML and whylogs are **not** in the extra, because
neither can share one dependency resolution with the rest of the package: nannyml 0.13.1 supports
Python < 3.13 only and its `s3fs` dependency brings fsspec/botocore bounds the `dataplane` and `backup` extras exclude, and
whylogs 1.6.4 cannot import beside NumPy 2. Their adapters run when you install them into a
separate environment (`pip install nannyml statsmodels` — nannyml 0.13.1 imports `statsmodels`
without declaring it; `pip install whylogs 'numpy<2'`). Each library is imported lazily, and a
missing or failing one falls back to the builtin with the reason recorded on the event.

The adapters are tested two ways: hermetically, against stand-ins with the exact return shapes of
river 0.26.1, evidently 0.7.23, nannyml 0.13.1 and whylogs 1.6.4 (always run), and against the real
libraries when they are installed (`tests/unit/test_drift_advanced_adapters.py`, the `real_` tests —
they skip, not pass, on a bare install).

## See also

- [AgentOps (C4)](agentops.md) — agent-side analytics.
- [Evaluation (C2/C3)](evaluation.md) — quality gates the same signals feed.
- `exa eval feedback` — the ground-truth loop that supplies delayed labels.
