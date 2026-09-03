# Continuous evaluation & the regression gate (C2 · C3)

ExaMLOps evaluates model/prompt versions with **eval suites** (deterministic + LLM-as-judge
evaluators) and blocks promotion when a version **regresses** against the incumbent or
violates an absolute floor.

Design: ADR 0007 (C2) + ADR 0008 (C3) · specs `design/vision/specs/C2-continuous-eval.md`
and `C3-eval-regression-gate.md`.

## C2 — eval suites

An **evaluator** scores one item → `{metric, score, detail}`. A **suite** bundles
evaluators and runs them over items, persisting the mean per-metric score to
`platform_db.eval_suite_results` (idempotent per suite × version × run × metric).

Deterministic evaluators: `ExactMatch`, `Regex`, `NumericTolerance`, `JSONValid`.
LLM-as-judge: `LLMJudge(judge_fn, rubric, judge_model, prompt_version)` — runs at
temperature 0 and records the judge model + prompt version per result (governance).

```bash
# items.jsonl: one {"output": "...", "reference": "...", "prompt": "..."} per line
exa eval run smoke --model JPCP --items ./eval/items.jsonl --version 18
exa eval run smoke --model JPCP --items ./eval/items.jsonl --sample 20   # sample by request_hash
```

`exa eval run` exits non-zero only on **execution error** — never on low scores (that's
the gate's job, C3). Aggregate scores feed A2's `eval_score` facet, C6 SLOs, D1 evidence,
and the dashboard Evaluation page.

### Judge calibration

`judge_calibration(judge_scores, human_labels)` computes judge↔human agreement (+ MAE)
over a labelled set, so a judge's reliability is measured before it gates anything.

## C3 — regression gate

A gate is per-model config:
`{suite, baseline_alias, metrics:[{name, min?, max?, max_drop?, higher_is_better?}],
mode ∈ {block, warn}, aggregate ∈ {all, majority}}`.
A metric **fails** if it regresses beyond `max_drop` versus the baseline alias, falls below
`min`, or rises above `max`. Error metrics (rmse) use `--lower-is-better`.

```bash
exa eval gate set JPCP --suite smoke \
    --metric accuracy:min=0.8:max_drop=0.01 --metric groundedness:min=0.8 --mode block
exa eval gate show JPCP
exa eval gate run  JPCP 18            # exit 1 in block mode on failure — CI-safe
```

### How the metrics decide together

By default (`--aggregate all`) **any** failing metric blocks. With `--aggregate majority` a
*regression* blocks only when more than half the configured metrics regressed, so one noisy
metric cannot alone veto a genuine improvement.

```bash
exa eval gate set JPCP --suite smoke --aggregate majority \
    --metric accuracy:max_drop=0.01 --metric f1:max_drop=0.01 --metric recall:max_drop=0.01
```

Three things this policy deliberately does **not** do:

- **It does not change what existing gates do.** `all` is the default, and an unrecognised
  value falls back to `all` — a typo fails closed. A gate configured before this option existed
  blocks exactly as it did.
- **It never softens an absolute bound.** A `min` floor, a `max` ceiling, or a **missing**
  candidate score blocks on its own under `majority` too. Sampling noise lives in a `max_drop`
  comparison against a baseline; an absolute bound is a statement about the candidate alone. A
  safety cap that unrelated metrics can outvote is not a cap, and "nothing was measured" is not
  noise to be outvoted.
- **It does not hide the failure.** An outvoted metric is still recorded in the report as
  failed, and the report records which policy decided it — so a pass under `majority` is never
  mistaken for a clean run.

### Gates over metrics that point both ways

`--higher-is-better` / `--lower-is-better` sets the direction for the *whole* gate, and any
single metric may override it with `higher_is_better=false`. That override is not a nicety.
The agent suites store both directions in one scores dict — `answer_rate` and `pass_rate`
rise, `unsafe_rate`, `latency_p50` and `latency_p95` fall — so under one flag half of such a
gate is read backwards, and the half read backwards is the safety half. Prefer `max` for a
cap: it is a plain ceiling and means the same thing whichever way the gate leans, where a cap
written as a `min` under the wrong direction silently never fires.

```bash
exa eval gate set SkipperProd --suite agent-safety \
    --metric answer_rate:min=0.95 \
    --metric unsafe_rate:max=0.05:higher_is_better=false \
    --metric latency_p95:max=30 --mode block
```

Run against a candidate that answers everything but executes every mutating request and takes
five minutes to do it, that gate reports `answer_rate ok`, `unsafe_rate FAIL`,
`latency_p95 FAIL`, and exits 1.

### Token and cost budgets

Every agent suite also records what the run *consumed*, read from the `usage` block the agent
bridge already returns. Correctness and latency say whether an answer is right and in time;
neither distinguishes an agent that answers correctly on 4 000 tokens a question from one that
needs 40 000, so a prompt change that triples the spend at unchanged accuracy was previously
invisible in the stored history.

| Metric | Meaning |
|---|---|
| `tokens_total` | Tokens across the answers that reported usage |
| `prompt_tokens_total` / `completion_tokens_total` | The same split by direction |
| `tokens_per_answer` | The budget number — stable when the suite size changes |
| `usage_reported_rate` | How much of the run the totals above actually cover |
| `cost_usd` / `cost_per_answer` | Only when the model has a **known** price (see below) |

A budget is an ordinary ceiling — no new gate machinery:

```bash
exa eval gate set SkipperProd --suite agent-safety \
    --metric tokens_per_answer:max=6000:higher_is_better=false \
    --metric cost_per_answer:max=0.02:higher_is_better=false --mode block
```

Two rules keep the numbers honest, and both matter because a budget is a *ceiling* — the
direction in which a broken measurement passes:

- **No usage reported ⇒ no token scores at all.** A bridge that stops returning `usage` would
  otherwise sum to zero, and a zero passes every ceiling precisely when measurement has broken.
  Partial coverage is visible in `usage_reported_rate`; gate on it if the totals must be
  trusted (`--metric usage_reported_rate:min=0.95`).
- **A price is reported only when a price is known.** Cost appears when an operator has
  selected an `llm_cost` provider (ADR 0083) or the model is one the built-in rate table
  actually prices. Skipper's normal backend is a locally served model, and charging it a
  generic default rate would invent a dollar figure for self-hosted inference — on an
  HPC-sovereign platform the honest answer is usually *no such number*, and GPU-seconds
  (`exa finops`) is the road that answers it. Tokens are observed, so they are always recorded.

Spend is attributed to the backend that actually answered (`model_version`, e.g.
`ollama:llama3.1:8b`), never to the free-text `--agent-model` label — otherwise two backends'
costs land in one series.

### Enforcement in `promote`

`exa pipeline promote` runs the gate before moving an alias. In `block` mode a failure
**refuses** the promotion and audits `promotion_blocked_by_gate`; `--force` overrides and
audits `eval_gate_override` with the failing metrics (D4).

```bash
exa pipeline promote jpcp --if-rmse-lt 5.0            # gate-checked
exa pipeline promote jpcp --if-rmse-lt 5.0 --force    # override a failing gate (audited)
```

Every gate run persists a structured report to `platform_db.gate_reports`. (ADR 0008 clause 4
also calls for a dashboard Promotion page and a blocking CI job before an alias move; neither
is built — the reports are readable from the table and from `exa eval gate run`.)

### Enforcement in `validate-model`

`exa pipeline validate-model` runs the eval gate **alongside** the latency smoke-test, so one
command answers both "does it serve" and "did it regress". It resolves the alias to a version
through MLflow and exits non-zero on a `block`-mode failure — safe as a CI gate before
promotion.

```bash
exa pipeline validate-model JPCP --alias Staging      # latency SLA + eval gate
```

The report carries an `eval_gate` column that is `PASS`, `FAIL` or **`SKIP` with a reason** —
no gate configured, an alias that could not be resolved in MLflow, or a gate error. The skip is
never silent: a CI log showing only a green latency check, when the eval gate did not run,
reads as "validated". A `warn`-mode or outvoted pass is labelled `not blocking` for the same
reason.

### Enforcement in the autopilot

There are two roads to Production and the gate guards both. `exa autopilot run` promotes the
same model to the same alias, so a failing `block`-mode gate stops the cycle and is audited as
`autopilot_promote_blocked`; `warn` is advisory here exactly as it is in `promote`; an
unconfigured gate is a no-op. There is no `--force` on this road — an autopilot that can
override its own gate is not gated.

The autopilot's *promotion rule* (`exa pipeline promote` writes it) is an absolute threshold on
one metric. The C3 gate is the only check that compares a candidate against the **baseline
alias**, which is why the closed loop needs it: a model can clear `rmse < 5.0` while having
regressed from 2.0. A gate that is configured but cannot be evaluated — the Staging version does
not resolve — blocks rather than promotes, because promoting because the check could not run is
the failure the gate exists to prevent.

### Which direction a gate uses

Precedence is **per-metric > the gate's own > the caller's**.

Declare the gate's direction when you set it. Left undeclared, the gate takes whatever the caller
passes, and both promotion roads derive that from the promotion *rule's* operator — a threshold on
one MLflow metric that says nothing about the suite metrics the gate names. A model promoted on
`--if-rmse-lt 5.0` then defaults every eval metric to lower-is-better, and an `accuracy` regression
is read backwards. Worse, the same gate judges differently depending on who ran it.

```bash
exa eval gate set JPCP --suite smoke --metric accuracy:max_drop=0.01 --higher-is-better
exa eval gate show JPCP     # the Direction column says which, or "undeclared"
```

*Undeclared* is not the same as *lower-is-better*: gates written before this existed keep the
caller fallback and behave exactly as they did, and `exa eval gate run` prints a warning naming
the direction it borrowed rather than choosing silently.
