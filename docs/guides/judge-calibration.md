# Judge calibration — no uncalibrated judge may gate

An LLM judge decides which model reaches production. If nobody has measured that judge, the
promotion gate is a confident guess wearing the clothes of a measurement.

ExaMLOps therefore refuses to let an unmeasured judge gate anything. A judge must first pass the
**Minimum Viable Validation Protocol (MVVP)**; until it does, `exa eval gate`,
`exa pipeline promote` and `exa autopilot` all refuse and name the failed check.

This is deliberately disruptive on first rollout — see [Migration](#migration).

## Why

The largest systematic study of LLM judges to date (21 judges, 9 providers, 3 benchmarks,
118 runs, ≈541 000 judgments) found:

| Finding | Consequence for a gate |
|---|---|
| Raw agreement overstates chance-corrected κ by **33.8–41.3 pp** | A "90 % agreement" judge can be a κ ≈ 0.6 judge |
| **11 of 21 judges shift ≥4 rank positions** across benchmarks (one moves 15) | A judge validated on one benchmark family tells you nothing about another |
| Test–retest **0.992** with position bias **0.192** | A judge can be almost perfectly *reproducible* and almost perfectly *wrong* |
| Position bias spans **0.002 → 0.192** (~100×) | Which judge you picked matters more than the model you are gating |

The last row is the one that motivates the hard failure: reproducibility is the property people
instinctively check, and on its own it is not evidence of anything.

## The protocol

A calibration measures five things. All must pass for the judge to be gate-eligible.

| Check | Rule | Requirement |
|---|---|---|
| Chance-corrected agreement | Cohen's κ **with a 95 % interval** — never raw agreement | G7.1 |
| Position bias | Paired **AB + BA** presentation; `\|P(first position wins) − 0.5\| ≤ 0.10` | G7.2 |
| Test–retest | **≥3** replications, temperature 0, response caching **off** | G7.2 |
| Benchmark coverage | **≥2 families**, spanning *preference*-based and *correctness*-based labels | G7.2 |
| Consistency–bias paradox | test–retest > 0.95 **and** position bias > 0.10 ⇒ hard failure | G7.2 |

Two further rules apply to everything downstream:

- **Provenance (G7.3).** Every evaluation result carries a `calibration_id` resolving to the
  judge's κ and bias *at the time of that evaluation*. Re-measuring a judge never rewrites the
  provenance of an evaluation that already ran.
- **Uncertainty (G7.4).** Proportion scores are stored with a Wilson interval
  (`score_lo` / `score_hi`), not as point values.

Replications must run with caching **off**: three replications served from a cache measure the
cache, not the judge.

## Calibrating a judge

Collect the judgments into a JSON file — a batch job, reviewable and committable:

```json
{
  "benchmarks": [
    {
      "name": "mt-bench-sample",
      "family": "preference",
      "items": [
        {"human_label": 1, "judge_scores": [1, 1, 1], "ab_first_wins": [true, false]},
        {"human_label": 0, "judge_scores": [0, 0, 0], "ab_first_wins": [false, true]}
      ]
    },
    {
      "name": "gsm8k-sample",
      "family": "correctness",
      "items": [{"human_label": 1, "judge_scores": [1, 1, 1]}]
    }
  ]
}
```

- `judge_scores` — one score per replication, in `[0, 1]`. The recorded replication count is the
  **minimum** across items: an item scored once cannot support a three-replication claim.
- `ab_first_wins` — outcomes of the paired AB + BA probe: did the option in the *first* slot win?
  An order-blind judge produces one `true` and one `false` per pair.
- `human_label` — ground truth, binarized at 0.5.

Then measure and record it:

```bash
exa eval calibrate gpt-judge --from ./eval/judge-calibration.json
exa eval calibrate gpt-judge --from ./eval/judge-calibration.json --require-eligible   # CI gate
exa eval calibration show gpt-judge
exa eval calibration list
```

Recording a *failing* calibration is not an error — the measurement is the point. Use
`--require-eligible` when a CI job should fail on a judge that may not gate.

To calibrate against a live judge from Python, use the same seam `LLMJudge` uses:

```python
from examlops.evaluation.calibration import CalibrationBenchmark, CalibrationItem, calibrate
from examlops.data.evaluation import record_judge_calibration

cal = calibrate(judge_fn, benchmarks, judge="gpt-judge", replications=3)
record_judge_calibration(cal)
```

## What refuses, and what it looks like

| Surface | Behaviour when the judge is not eligible |
|---|---|
| `exa eval gate run` | Gate fails; a `judge_calibration` verdict names the failed checks |
| `exa pipeline promote` | Alias is not moved; blocked promotion is audited (`--force` still overrides, and is audited) |
| `exa autopilot run` | Promotion is blocked and an `autopilot_promote_blocked` audit event is written |

A judge nobody has measured fails with `no_calibration`. **Absence of calibration is not
eligibility** — that is the specific failure mode this exists to prevent.

The refusal applies in `warn` mode too. `warn` says *metric regressions* are advisory; it was
never a licence to let an unmeasured instrument decide what reaches production.

A suite made only of deterministic evaluators (exact match, regex, JSON validity) has no judge and
is unaffected.

## Correcting a score for judge error

A judge's raw pass-rate is an *apparent* rate measured with an imperfect instrument. The
Rogan–Gladen correction converts it into an estimate of the true rate using the judge's own
sensitivity and specificity, both recorded by the calibration:

```python
from examlops.evaluation.calibration import rogan_gladen
rogan_gladen(apparent=0.80, sensitivity=0.90, specificity=0.90)   # -> 0.875
```

It returns `None` when `sensitivity + specificity ≤ 1`: a judge no better than chance carries no
information, and a corrected number there would be an invention.

## Migration

Gates that pass today will start refusing as soon as their suite uses a judge. That is the
intended behaviour. The path forward is:

1. `exa eval calibration list` — see which judges are measured (empty means none may gate).
2. Assemble a labelled benchmark for each judge in use, spanning both families.
3. `exa eval calibrate <judge> --from <file>` and read the failed checks.
4. Fix the judge (prompt, model, or presentation order) and re-measure.

There is no flag to skip the requirement. If calibration cannot run because no labelled benchmark
exists, the judge is not gate-eligible — a deliberate hard failure, not an oversight.

## Reference

- ADR 0111 — *No uncalibrated judge may gate* (requirements G7.1–G7.4)
- Module: `examlops.evaluation.calibration` · gate wiring: `examlops.evaluation.gate`
- Table: `judge_calibrations`; provenance columns `eval_suite_results.calibration_id`,
  `score_lo`, `score_hi`
- Related: [Evaluation & regression gates](evaluation.md)
