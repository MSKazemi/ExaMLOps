# Shadow Deployment & Champion-Challenger (C7)

> Next-Gen 40 · feature **C7** · ADR 0024 · spec `design/vision/specs/C7-shadow-champion-challenger.md`

C7 lets you run a **challenger** model alongside the production **champion**: production
traffic is mirrored to the challenger in isolation, the two are scored against each other
as labels arrive, and a promotion is proposed (through the C3 gate) only when the
challenger wins with statistical significance and no SLO regression.

This builds on the existing `exa serve shadow` traffic-mirroring and the phase-24 A/B
statistics engine (`analysis/ab_stats`).

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
| `policy_met` | all conditions satisfied → ready to promote |

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
