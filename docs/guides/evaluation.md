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
LLM-as-judge: `LLMJudge(judge_fn, rubric, judge_model, prompt_version)` — records the judge
model + prompt version per result (governance).

```bash
# items.jsonl: one {"output": "...", "reference": "...", "prompt": "...", "contexts": [...]} per line
exa eval run smoke --model JPCP --items ./eval/items.jsonl --version 18
exa eval run smoke --model JPCP --items ./eval/items.jsonl --sample 20   # sample by request_hash
exa eval run live --model JPCP --items ./eval/items.jsonl -e abs_error -e numeric_match:0.5
exa eval run rag --model chat --items ./eval/rag.jsonl -e judge:faithfulness --judge-model llama3.1:8b
```

`exa eval run` exits non-zero only on **execution error** — never on low scores (that's
the gate's job, C3). Aggregate scores feed A2's `eval_score` facet, C6 SLOs, D1 evidence,
and the dashboard Evaluation page.

### Evaluator specs and engines

`--evaluator/-e` takes a spec; `exa eval evaluators` lists them with the engine that runs
each on this host.

| Spec | Metric recorded | Engine |
|---|---|---|
| `exact_match`, `json_valid`, `numeric_match[:TOL]` | same name | native |
| `abs_error` | `mae` — the suite mean of `\|output − reference\|`, the same MAE `exa eval feedback accuracy` reports; carries a unit, so no Wilson interval | native |
| `string_presence`, `string_similarity` | same name | Ragas if importable → rapidfuzz (`examlops[eval-metrics]`) → pure Python; all three compute the identical number |
| `rouge_l` | `rouge_l` (Porter-stemmed rougeL F1) via Ragas or rouge-score; **`rouge_l_unstemmed`** with neither — a different number gets a different series | Ragas → rouge-score → pure Python |
| `judge:correctness\|relevancy\|faithfulness\|helpfulness` | `judge_<rubric>`, prompt version `<rubric>-v1` | B2 gateway judge (`--judge-model`) |
| `deepeval:answer_relevancy\|faithfulness\|contextual_precision\|contextual_recall\|hallucination` | `deepeval_<metric>` | DeepEval, with the gateway judge as its model |

**Why Ragas and DeepEval are not extras.** Both are wrapped (ADR 0007 decision 1), and
both activate when importable — but neither can join this workspace's single lock today:
deepeval 4.2.6 pins `click<8.4` while `examlops` needs `click>=8.5`, and ragas 0.4.3 needs
`datasets`, every release of which caps `fsspec<=2026.6` while the dataplane extras need
`fsspec>=2026.7`. Run them from a separate evaluator image, or use the
`examlops[eval-metrics]` extra, which installs the two libraries Ragas's text metrics
delegate to so the numbers match Ragas's. A `deepeval:` spec without DeepEval is **refused**
(`EvaluatorUnavailable`), never silently replaced by a rubric judge — `judge:relevancy` is
the explicit alternative. Both libraries' telemetry is switched off before import
(`DEEPEVAL_TELEMETRY_OPT_OUT`, `RAGAS_DO_NOT_TRACK`).

### Judges run at temperature 0 — enforced

The harness calls every judge through one invoker. A judge seam that accepts a
`temperature` keyword is **set** to 0 (the gateway judge, `gateway_judge(model)`, does and
also bypasses the semantic cache with `no_cache`); an `LLMJudge(temperature=…)` other than 0,
or a callable that declares a non-zero `temperature` attribute, is refused
(`JudgeTemperatureError`) — including inside a tolerant online suite and inside
`exa eval calibrate`'s replications. A bare `fn(prompt)` callable cannot be told its
temperature, so its scores carry `temperature_enforced: false` rather than a claim nobody
checked. A judge answer with no score in `[0, 1]` (or a ratio such as `4/5`) raises
`JudgeOutputError`; a bare `7` is refused, not rescaled.

## Online evaluation of live traffic (ADR 0007 decisions 2–3)

`exa eval run` measures a fixed item set when someone runs it. **Online evaluation** scores a
sample of real traffic on a schedule:

```bash
# predictive model — labelled predictions from platform_db (the #9 feedback loop)
exa eval online enable JPCP --suite live-accuracy -e abs_error -e numeric_match:0.5

# generative model — GenAI spans from Tempo, judged by a local model through the gateway
export EXAMLOPS_EVAL_TEMPO_URL=http://tempo:3200
exa eval online enable chat --suite live-quality --source tempo \
    -e judge:relevancy -e json_valid --judge-model llama3.1:8b --sample 25

exa eval online status
exa eval online run --once --dry-run                     # preview, writes nothing
EXAMLOPS_EVAL_ONLINE_ENABLED=1 exa eval online run --once  # one real cycle
EXAMLOPS_EVAL_ONLINE_ENABLED=1 exa eval online run --interval 300   # the scheduled loop
```

Each cycle, per enabled schedule: pull the window's traffic → sample `--sample` items by
`request_hash` (deterministic) → score → persist to `eval_suite_results` under
`run_id = online:<alias>:<window>:<window-start>` → audit. The same table feeds the C3 gate,
so an online suite on `Production` is a live baseline.

| Source | What it reads | Notes |
|---|---|---|
| `predictions` | `platform_db.predictions` joined to the newest `ground_truth` label per `request_hash` | Only labelled predictions (label-free estimation is `exa drift run-advanced`). The table has no tenant column, so any tenant but `default` is refused. |
| `tempo` | Tempo `/api/search` TraceQL over C1 GenAI spans: `gen_ai.request.model`, `examlops.tenant`, a present `examlops.request_hash` | Model and tenant are filtered **in the query**, before Tempo's limit. Prompt/completion exist on a span only if `EXAMLOPS_GENAI_CAPTURE_CONTENT` was on (always D8-redacted); spans without content are counted as `no_content` and the cycle says why. Names are validated before they reach TraceQL. |

Safety and bounds, the house pattern of `exa drift run-advanced`:

- **Kill-switch** `EXAMLOPS_EVAL_ONLINE_ENABLED` (default off). `--dry-run` needs neither it nor the lease.
- **Lease** `eval-online` via the shared coordinator (`EXAMLOPS_EVAL_ONLINE_LEASE_TTL`, 1800 s).
- **Idempotent per window** — a window already recorded is `deduped`, never scored twice.
- **Bounded** — a pull is capped at 5000 rows/spans; `--sample` ≤ 1000; `--window` ≥ 60 s; the
  Tempo call has a timeout (`EXAMLOPS_EVAL_TEMPO_TIMEOUT`, 10 s).
- **Tolerant per item, strict on governance** — a judge that answers no number is counted in
  `errors` and skipped; a temperature violation aborts the model's cycle.
- **Audited** — `eval_online_enabled` / `_disabled` / `_cycle` / `_skipped`.

Tempo auth: `EXAMLOPS_EVAL_TEMPO_TOKEN` (Bearer) and `EXAMLOPS_EVAL_TEMPO_ORG`
(`X-Scope-OrgID` for a multi-tenant Tempo).

### Prometheus

`exa slo export-metrics` (and, after each online cycle, the textfile at
`EXAMLOPS_EVAL_METRICS_TEXTFILE`, written atomically for node_exporter's textfile collector)
publishes the latest result per `(tenant, suite, model, alias, metric)` — scoped to one tenant
per export (`--tenant`, default `default`; the scheduler's textfile uses the tenant it ran for),
so one textfile never carries another tenant's scores:

| Series | Meaning |
|---|---|
| `examlops_eval_score` | latest score |
| `examlops_eval_score_lower` / `_upper` | its Wilson interval (proportions only) |
| `examlops_eval_sample_size` | items it was computed over |
| `examlops_eval_last_run_timestamp_seconds` | when it was recorded — alert on `time() - … > window`, so a suite that stopped running is not mistaken for a healthy one |

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

There are three roads to Production and the gate guards each (the training flow's own is
below). `exa autopilot run` promotes the
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

### Enforcement in the training flow

The training flow promotes a freshly trained version itself. Its lifecycle rules (Staging →
Canary → Production) set every alias whose metric threshold the version clears. That was the one
road the gate did not guard: a `block`-mode gate stopped `exa pipeline promote` and the autopilot,
while the flow auto-promoted past it on an `rmse` threshold alone.

Now every alias **past Staging** answers to the gate. The gate runs once per version, before the
first such alias is set and before the move is announced on the event backbone. On a refusal:
- the flow stops, and the version keeps only the aliases it had already reached (normally
  Staging);
- the refusal is audited as `promotion_blocked_by_gate`, and the gate report is persisted as
  usual;
- a configured gate that cannot run refuses too, audited as `promotion_gate_error`.

`warn` mode and an unconfigured gate change nothing. Staging itself is not gated: it is the
candidate stage the suite evaluates, and gating it would stop a fresh version from ever being
scored. The flow's fallback direction is its lifecycle rule's, so declare the gate's direction
(below).

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
