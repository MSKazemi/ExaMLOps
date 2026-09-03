# Shadow Deployment & Champion-Challenger (C7)

> Next-Gen 40 · feature **C7** · ADR 0024 · spec `design/vision/specs/C7-shadow-champion-challenger.md`

C7 lets you run a **challenger** model alongside the production **champion**: production
traffic is mirrored to the challenger in isolation, the two are scored against each other
as labels arrive, and a promotion is proposed (through the C3 gate) only when the
challenger wins with statistical significance and no SLO regression.

This builds on the existing `exa serve shadow` traffic-mirroring and the phase-24 A/B
statistics engine (`analysis/ab_stats`).

## Where mirroring happens

The **Ray Serve router** mirrors. Enable it per model and it starts on the next request:

```bash
exa serve shadow enable JPCP --shadow-alias Staging
exa serve shadow log JPCP        # the recorded comparisons
```

Every successful prediction is mirrored to the shadow alias on a background pool, the shadow
prediction is compared to the champion's, and the pair is written to `shadow_results` with a
percentage difference. Only the **success** path mirrors — a request that 4xx'd or timed out has no
champion value to compare against, so mirroring it would add a row with nothing on the other side.

The router is the boundary that mirrors because it is the one that produces a numeric prediction:
`shadow_results` stores `production_pred`/`shadow_pred`/`diff_pct` as REALs. The LLM gateway (B2)
does not mirror.

Three knobs, all with safe defaults:

| Variable | Default | Why it exists |
|---|---|---|
| `RAY_SHADOW_WORKERS` | `2` | A pool of its own — never the prediction pool, so a shadow slower than the champion cannot take threads from the traffic it is shadowing. |
| `RAY_SHADOW_MAX_INFLIGHT` | `16` | Excess is **dropped and counted**, never queued. An unbounded queue turns a merely-slow shadow into unbounded memory growth on a serving replica. |
| `RAY_SHADOW_CONFIG_TTL` | `30` | The config is consulted per request; a SQLite read per prediction would put the shadow's cost on the production path. |

Watch `examlops_shadow_total{status="recorded|error|dropped"}`. **Drops are counted deliberately**:
a scoreboard built silently from only the requests that happened to fit would misrepresent the
comparison it exists to make, so a gap in the data is visible rather than assumed away.

## Isolation guarantees

| Requirement | Mechanism |
|---|---|
| **No user impact (R1)** | The challenger response is recorded, never returned — the caller always gets the champion. |
| **Isolation (R2)** | `run_shadow(fn)` runs shadow inference and **swallows any exception** — a shadow crash or latency spike can't affect the production path. |
| **Side-effect-free (R3)** | Inside `shadow_context()` a thread-local flag is set; `guard_write()` raises `ShadowWriteError` if a shadow tries to write, so side effects are prevented/flagged. |

```python
from examlops.champion_challenger import run_shadow, guard_write

result, err = run_shadow(lambda: challenger.predict(x))   # never raises
# In any write path the model might reach:
guard_write("db-write")   # raises ShadowWriteError if called under shadow
```

## The dashboard console

`/serve/challenger` lists every configured challenger and, per model, the scoreboard the promotion
decision rests on: sample count, champion and challenger error, Δ, p-value, significance, the C6 SLO
verdict and whether the promotion policy is met.

Two behaviours worth knowing:

- **A refused promotion is shown, not hidden.** "Not significant", "too few samples", "SLO
  regression" is the useful half of the answer; the console displays the reason and the numbers
  behind it rather than an error.
- **No new permission.** Disabling a challenger takes `traffic.manage` — the capability that already
  governs enabling shadow traffic — and promoting takes `model.promote`, which is already a step-up
  capability. Viewers see everything and can change nothing.

Every decision is computed by `examlops.champion_challenger`, the same code path `exa serve
challenger` uses, so the console and the CLI cannot disagree about whether a promotion is warranted.

## Enabling a challenger

```bash
exa serve challenger enable JPCP --version 18 --mirror 50 \
    --min-delta 0.05 --alpha 0.05 --min-samples 100
# --auto-promote to promote automatically once the policy is met
```

This declares the **promotion policy**: the challenger must beat the champion by at least
`--min-delta` (error reduction), with `p < --alpha`, over at least `--min-samples`
labelled samples, and with no C6 SLO budget exhausted.

## The scoreboard

Champion and challenger predictions are logged per request into `challenger_samples`.
As ground-truth labels (or a C2 judge) arrive, `exa serve challenger status` scores both
with Welch's t-test on per-sample error:

```bash
exa serve challenger status JPCP
```

| Field | Meaning |
|---|---|
| `champion_error` / `challenger_error` | mean absolute error over labelled samples |
| `delta` | `champion_error − challenger_error` (positive = challenger better) |
| `p_value` | Welch's t-test two-sided p-value |
| `significant` | `p < alpha` |
| `slo_ok` | no C6 SLO budget exhausted for the model |
| `evidence` | what the scores rest on: `labels` / `judge` / `mixed` / `none` |
| `judge` / `judge_eligible` | the judge that scored it, and whether it may gate (ADR 0111) |
| `policy_met` | all conditions satisfied → ready to promote |

### When ground truth never arrives

Some deployments never get labels. Score the unlabelled samples with a C2 judge instead:

```bash
exa serve challenger judge JPCP --judge-model gpt-4o
exa serve challenger status JPCP        # evidence: judge
```

Four things this deliberately does:

- **Judge scores never go into `label`.** They have their own columns. A judged sample that is
  indistinguishable from a measured one turns the scoreboard into a mixture nobody can separate
  afterwards, and the promotion would rest on evidence of unknown provenance.
- **Ground truth wins wherever it exists.** Only unlabelled samples are judged — the judge is the
  fallback the ADR describes, not a second opinion that would replace a measurement with an
  estimate.
- **A judge error leaves the sample unscored**, not scored 0. A judge failure is not a bad
  prediction, and recording it as one would move the decision.
- **An uncalibrated judge cannot carry a promotion.** A scoreboard resting on a judge reports
  `policy_met: false` unless that judge is MVVP-eligible — a challenger promotion is the same
  decision `exa pipeline promote` makes by another road, and [judge
  calibration](judge-calibration.md) governs both. Calibrate with `exa eval calibrate`.

## Promotion

```bash
exa serve challenger promote JPCP
```

If the policy is met and there is **no SLO regression** (R6), a `PromotionProposal` is
returned and a `challenger_promotion_proposed` audit event is written (D4). The actual
alias move still goes through the C3 gate:

```bash
exa pipeline promote jpcp --if-rmse-lt 5.0     # C3 (+ C6 SLO gate if enabled) apply here
```

If the policy is not met, `promote` exits non-zero and explains what's missing.

## Programmatic use

```python
from examlops.champion_challenger import (
    enable_shadow, challenger_status, maybe_promote,
)

enable_shadow("JPCP", "18", mirror_pct=50, min_delta=0.05, min_samples=100)
# ... serving records champion/challenger samples + labels ...

st = challenger_status("JPCP")
if st.policy_met:
    proposal = maybe_promote("JPCP")   # None if SLO regression or policy unmet
```

## Governance

- **Audited (D4):** `challenger_enable`, `challenger_disable`, `challenger_promotion_proposed`.
- **Tenant-scoped (D6):** every config and sample carries a `tenant`.

## Graceful degradation

Scoring uses `analysis/ab_stats.welch_t_test` (SciPy/NumPy) when available and falls back
to a pure-Python normal-approximation z-test otherwise. The whole feature works against
`platform_db` with no external service.

## See also

- `exa serve shadow` — the underlying traffic mirroring.
- [SLOs (C6)](slos.md) — the regression guard on promotion.
- [Evaluation gate (C3)](evaluation.md) — the promotion mechanism.
