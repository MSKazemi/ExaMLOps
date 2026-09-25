# Model-Quality SLOs & Burn-Rate Alerting (C6)

> Next-Gen 40 · feature **C6** · ADR 0023 · spec `design/vision/specs/C6-model-quality-slos.md`

C6 adds a **declarative model-quality SLO layer** on top of the existing
Prometheus/Alertmanager stack. You declare Service Level Objectives (SLOs) per model in
an OpenSLO-style form; ExaMLOps generates promtool-valid recording + multi-window
burn-rate alerting rules, tracks the **error budget** from live samples, and can gate
promotion (C3) when a budget is exhausted.

## Concepts

| Term | Meaning |
|---|---|
| **SLI** | A measured indicator — the *good/total* event ratio (e.g. p99 latency under 1s, groundedness ≥ target). |
| **SLO** | A target for an SLI over a window (e.g. 99% over 30 days). |
| **Error budget** | The allowed failure fraction = `1 − target`. |
| **Burn rate** | Observed error / allowed error. > 1 means you're consuming budget faster than sustainable. |

## Declaring SLOs

```bash
# One SLO inline
exa slo set LLM  latency-800ms --target 0.99 --window 30d --source c1 --query 'latency_ms<=800'
exa slo set LLM  groundedness --target 0.95 --source c2 --gate   # --gate: block promotion on exhaustion

# Or a batch from OpenSLO-style YAML
exa slo apply slos.yaml
exa slo list --model JPCP
```

`slos.yaml`:

```yaml
slos:
  - model: JPCP
    name: latency-p99
    target: 0.99
    window: 30d
    sli_source: c1
    sli_query: 'sum(rate(good[5m])) / sum(rate(total[5m]))'
  - model: LLM
    name: groundedness
    target: 0.95
    sli_source: c2
    gate_promotion: true
```

SLOs are **versioned** (re-applying bumps the version) and **per-tenant** (D6). SLI
sources map to the other Next-Gen tracks: `c1` (latency/cost/error), `c2` (eval
quality), `c4` (agent tool-call / session success — see
[AgentOps](agentops.md#agent-slos-error-budgets-and-burn-rate-alerts)), `c5` (drift verdicts),
`c8` (fairness disparity), `availability`, or raw `prometheus`.

## Feeding the SLI

```bash
exa slo ingest JPCP        # pull samples from the platform's own telemetry
exa slo record JPCP latency-p99 995 1000    # or push one interval by hand
```

Until `ingest` existed every SLI arrived by hand, so an SLO measured whatever someone remembered
to type — while the spec's `sli_source` was stored and read by nothing.

**Each event is counted once, however often you ingest.** An SLO's status sums its samples, and
the ingesters used to record their whole window as a new sample on every run. A daily ingest
therefore counted the same events once per day, and a fresh outage was diluted by recounting a
good month: 120 real calls read as 620, and the SLI read 0.948 against a true 0.817.

The event sources (`c1`, `c2`, `c4`, `c5`) now record a watermark with each sample and count only
events past it. Run `exa slo ingest` as often as you like: a run with nothing new records nothing
and reports `up to date`. The first ingest of a `c2` spec starts from the newest result rather
than the whole history. `c8` is different: it measures the current state (how many declared
attributes are within threshold now), so each ingest is one sample of it, and the SLI weights
those samples by how often you ingest.

**`c2` (eval quality) is ingested today.** An `eval_suite_results` row is already a proportion over
a known sample size, which is exactly the shape an SLI needs; `sli_query` picks the metric —
`pass_rate`, or `agent-safety:answer_rate` to pin one suite. A metric that is not a ratio is
refused rather than coerced: an SLI is good/total, and rounding a latency into a count would
invent a denominator.

**`c5` (drift) counts recorded verdicts.** `good` is the drift evaluations that came back OK,
`total` is the evaluations in the window; `--query` pins one `drift_kind`.

```bash
exa slo set JPCP not-drifting --target 0.95 --source c5 --query concept
```

It reads `drift_events` rather than raw snapshots on purpose: one recorded verdict is one
evaluation the real detector already made, so there is no second copy of the threshold rule to
diverge from `exa drift status` when a drift provider is swapped. **It does not cover prediction
drift** — `exa drift status` computes that live and persists no verdict, so a model with only
prediction drift reports the skip reason rather than an empty SLI.

**`c8` (fairness) counts declared slice attributes.** `good` is the attributes whose disparity is
within threshold, `total` is the ones that could actually be **measured**.

```bash
exa slo set JPCP fair --target 0.99 --source c8 --gate
```

An attribute whose slices are all below the min-sample guard is **excluded, not counted as
good** — counting it would let a model with no data score a perfect fairness SLI. The registry
comes from the same `effective_fairness_config` the promotion gate uses, so the SLI and the gate
can never disagree about which attributes a model declares.

**`c1` (the model gateway) counts measured calls.** Every gateway call records how long the
caller waited (guardrail scanning included) and whether it failed. A request that failed on
every backend is recorded as an error, where it used to leave no row at all, making any error
rate computed from the table zero by construction. A `c1` SLO needs `--query`:

```bash
exa slo set LLM latency-800ms --target 0.99 --source c1 --query 'latency_ms<=800'
exa slo set LLM errors        --target 0.999 --source c1 --query errors
```

`latency_ms<=800` is the share of **successful** calls answered within 800 ms; a failed call
belongs to the error SLI, the usual SRE split. `errors` is the share of calls that did not fail. A
failover that eventually succeeded is one successful call, timed end to end. Only calls in the
spec's own `--window` count, and only calls that carry a measurement: rows written before this
existed have no latency and read as *unmeasured*, never as fast.

**`availability` probes the model itself.** Each ingest asks the platform's own Ray Serve the Open
Inference Protocol readiness question, `GET /v2/models/{model}/ready`, and records one sample:
good on 200, bad on anything else. A server that doesn't answer is a bad sample too, because a
model nobody can reach is exactly what this SLO counts. Probes accumulate, so run the ingest on a
schedule and the SLI becomes the share of probes the model was ready for:

```bash
exa slo set JPCP up --target 0.999 --source availability          # the served model
exa slo set JPCP up-v17 --target 0.99 --source availability --query version:17
* * * * * exa slo ingest JPCP     # e.g. a cron entry: one probe a minute
```

The host is always the configured `ray_serve_url` (`RAY_SERVE_URL`), and a spec can only pin a
version. An SLO spec that could name a URL for the platform to fetch would be a server-side
request forgery primitive. Redirects are not followed either. This is black-box availability:
does the model answer. The share of *real* requests that succeeded is request-based
availability, which lives in Prometheus.

**`prometheus` reads the SLO's PromQL ratio back.** Each ingest evaluates the spec's `--query` (the
good-events *ratio*, the same contract `exa slo generate` builds rules from; without one, the
recorded `examlops:sli_ratio` series) as an instant query against `PROMETHEUS_URL`. It records one
sample of the answer:

```bash
exa slo set JPCP p99-ok --target 0.99 --source prometheus \
  --query 'sum(rate(http_requests_total{model="JPCP",code!~"5.."}[5m])) / sum(rate(http_requests_total{model="JPCP"}[5m]))'
```

- **The SLI is time-weighted.** Prometheus hands back a ratio, so each ingest records
  `good = ratio, total = 1`. The SLI is the average of those samples, not an event-weighted count
  like `c1`/`c2`/`c5`; turning a ratio into counts would invent a denominator. Run the ingest on a
  schedule.
- **Only one ratio is accepted.** A query that returns more than one series, none, NaN, or a value
  outside [0, 1] is refused with the reason. It is never averaged or clipped. Aggregate it in the
  query itself.
- **A Prometheus that can't be reached is *unmeasured*.** This is the opposite rule to the
  availability probe: a monitoring outage is not a service outage. The host comes from
  `PROMETHEUS_URL`, never from the spec.
- **A self-diluting ratio is warned about.** If the numerator selects *some* of a counter's label
  values and the denominator takes *all* of them, every other value sits in the denominator and
  lowers the measured rate:

  | | events | bad | measured rate |
  |---|---|---|---|
  | real traffic | 100 | 5 | **5.0%** |
  | plus 900 events the numerator never selects | 1000 | 5 | **0.5%** |

  So unrelated traffic can hold a burn-rate alert below its threshold during a real incident.

  **Every surface that writes a spec says so**, because the query is judged in `apply_spec` rather
  than at one call site: `exa slo set` prints it, `exa slo apply` prints it per spec (in a file of
  twenty, "one of these dilutes itself" is not actionable), and `POST /api/slo` returns it in a
  `warnings` array. It **only warns** — "what share of *all* events were X" is a legitimate SLI that
  looks identical, and only the author knows which was meant, so the spec is written either way.

  This is not hypothetical advice: the platform made the same mistake four times in its own alert
  rules (see [Serving runbooks](../runbooks/serving.md) and
  [Control plane](control-plane.md#retrain-outcomes-and-what-a-success-rate-divides-by)), where
  refused and throttled requests were diluting the rates that decide whether anyone is paged.

**Every source now has an ingester.** An SLO whose ingest cannot produce a sample, for a missing
query, no data or an unreachable Prometheus, reports why and records nothing, because silence
would be indistinguishable downstream from a healthy service nobody asked about. That is the trap
the `measured` flag exists to close.

A **misspelled** source is reported differently again — `unrecognised sli_source '<x>' — expected
one of [...]`. A typo and a deliberately-unbuilt source read identically until 2026-09-02, which
meant a spec written straight from ADR 0023 (`--source c5`) was told the source did not exist.

## When an SLO breaks

A breach is audited. Every sample — hand-typed or ingested — goes through one path, and the moment
an error budget goes from intact to spent it writes a D4 `slo_breached` event with the SLI, target,
remaining budget, burn rate and sample count:

```bash
exa audit --last 7d | grep slo_breached
```

Only the **transition** is audited. A sustained breach re-recording itself every interval would
produce an audit trail that grows without new information, and recovery is not audited at all — a
budget that refills is a rolling-window artefact, not a decision anyone made.

## Publishing the SLIs Prometheus cannot see

`exa slo generate` emits burn-rate **alert** rules that range over a Prometheus series. For an SLI
the platform ingests itself — `c1`, `c2`, `c4`, `c5`, `c8`, `availability` — that series does not
exist unless you publish it, so those alerts can never fire. For those sources the generated rules
record the SLI from `examlops_slo_sli{model,slo,tenant}`, and each burn-rate alert takes its short-
and long-window error ratio from the `examlops_slo_good_total` / `examlops_slo_events_total`
counters (`1 - increase(good[5m]) / increase(events[5m])`): the SLI gauge is the ratio over the
SLO's whole window and cannot show a burn that started an hour ago. All three are series this
command publishes; the
spec's own `--query` (`errors`, `pass_rate`, `tool_success` …) tells the ingester what to count and
is not PromQL, so it never reaches the rules. Only a `prometheus` spec's query is used as PromQL.
Publish the series with:

```bash
exa slo export-metrics --out /var/lib/node_exporter/textfile/examlops.prom
# then: node_exporter --collector.textfile.directory=/var/lib/node_exporter/textfile
```

Run it on a timer (cron, a systemd timer, a Prefect schedule) — it is a pure read, so re-running
it costs a query and rewrites one file.

It publishes `examlops_slo_sli`, `_target`, `_budget_remaining`, `_burn_rate`, `_samples`,
`_measured` and the `_good_total` / `_events_total` counters, plus vector-store latency and item gauges.

**An unmeasured SLO exports `measured=0` and no SLI at all.** Its placeholder 1.0 would put a
perfect ratio on a dashboard for something nobody has measured — and a burn-rate alert cannot fire
on a perfect ratio, which is exactly the silence this whole section exists to break. Alert on
`examlops_slo_measured == 0` if you want to know an SLO has gone unfed.

## Generating Prometheus rules

```bash
exa slo generate JPCP latency-p99 --out slo_rules.yml
promtool check rules slo_rules.yml     # valid by construction
```

The generated file contains:

- **recording rules** — `examlops:slo:<model>:<name>:sli_ratio`, `:error_ratio`, and
  `:error_budget`;
- **burn-rate alerts** — four multi-window pairs (Google SRE workbook defaults):

  | Windows | Burn factor | Severity | Fires after |
  |---|---|---|---|
  | 5m / 1h | 14.4× | critical (page) | 2m |
  | 30m / 6h | 6× | critical (page) | 15m |
  | 2h / 1d | 3× | warning (ticket) | 1h |
  | 6h / 3d | 1× | warning (ticket) | 3h |

  A **fast burn pages**; a single-sample blip does not, because each alert requires the
  *recorded* error ratio to exceed its threshold over **both** windows:

  ```promql
  (avg_over_time(examlops:slo:JPCP:latency_p99:error_ratio[5m]) > 0.144)
  and (avg_over_time(examlops:slo:JPCP:latency_p99:error_ratio[1h]) > 0.144)
  ```

  The long window is what makes the short one safe to page on. The alerts range over the
  recorded `:error_ratio` series rather than over your `sli_query` directly because PromQL
  can only subscript a selector — that is what the recording rules are for.

## Tracking the budget

Feed SLI measurements (or let the serving path do it), then inspect status:

```bash
exa slo record JPCP latency-p99 98 100     # 98 good of 100 total this interval
exa slo status JPCP                        # SLI, budget left, burn rate, OK/BREACH
exa slo burn JPCP                          # only SLOs actively burning budget
```

`exa slo status` shows, per SLO: its **window**, target, observed SLI, **budget left** (100% =
untouched, negative = exhausted), **burn rate**, and OK/BREACH.

**Every number in the row is measured over the spec's own `--window`.** A `30d` SLO answers "how
did the last thirty days go"; a `24h` one answers about today, and the two can disagree about the
same model at the same moment — that is the point of declaring a window. Samples older than it are
not counted, so a breach that ended a month ago no longer holds the budget down, no longer burns,
and no longer blocks a promotion; and an SLO whose window contains no samples reads **NO DATA**
rather than a perfect score. A spec with no window, or one nobody can parse (`last month`), is
measured over **30d** and says so in the `Window` column, rather than silently measuring something
else.

A row cap still bounds how much is read (the newest 1000 samples in the window), so a very busy SLO
measures a representative sample of its window rather than all of it. It is a bound on the read,
not the definition of the window — that distinction is what this used to get wrong: the status
summed the newest 1000 samples *whatever their age*, which is an hour on a busy SLO and half a year
on a quiet one.

## Promotion gating (C3)

When `EXAMLOPS_SLO_GATE_ENABLED` is set and a **gate-flagged** SLO
(`--gate` / `gate_promotion: true`) has an exhausted budget, `exa pipeline promote`
refuses to move the alias:

```bash
export EXAMLOPS_SLO_GATE_ENABLED=1
exa pipeline promote jpcp --if-rmse-lt 5.0
# → error: SLO budget exhausted for JPCP: groundedness. Use --force to override (audited).
```

`--force` overrides and writes an `slo_gate_override` audit event (D4); a block writes
`promotion_blocked_by_slo`.

## Programmatic use

```python
from examlops.slo import generate_rules, slo_status, budget_exhausted, apply_spec

apply_spec({"model": "JPCP", "name": "latency-p99", "target": 0.99})
rules = generate_rules({"model": "JPCP", "name": "latency-p99", "target": 0.99})
open("rules.yml", "w").write(rules.to_yaml())

for s in slo_status("JPCP"):
    print(s.name, s.budget_remaining, s.burn_rate)

if budget_exhausted("JPCP", "latency-p99"):
    ...  # block promotion
```

## Paired (TTFT, TPOT) serving SLOs

Generative latency is a pair: **TTFT** (queue + prefill) and **TPOT** (per-token decode). They trade
against each other, so a single `--max-latency` cannot say which side is tight (ADR 0117 decision 2).

```bash
exa slo pair-set qwen chat --ttft-ms 300 --tpot-ms 40 --percentile 99 --tight ttft --class interactive
exa slo pair-list --model qwen
exa slo pair-check qwen chat --samples bench.json   # [[ttft_ms, tpot_ms], ...]; exit 1 unless met
```

Semantics:

- **Both dimensions must hold.** `met` requires the nearest-rank percentile of TTFT <= `--ttft-ms`
  *and* of TPOT <= `--tpot-ms`. Exactly at a threshold passes; one dimension failing is `violated`.
- **Attainment** is the share of requests with TTFT *and* TPOT both within threshold;
  `burn_rate = (1 - attainment) / (1 - percentile/100)` (1.0 = budget spent exactly as allowed).
- **Absent is not pass.** Fewer than `EXAMLOPS_SLO_PAIR_MIN_SAMPLES` (default 20) valid samples, or
  an undeclared pair, is `no_verdict` (exit 1). Malformed samples (missing, negative, NaN) are counted
  as `rejected` and excluded.
- `--tight` records which side binds, for the topology/routing policy to read; it does not loosen the
  other dimension.

Library: `examlops.slo.pairs.check_pair(model, name, samples)` returns a `PairVerdict`
(`.passed` is True only for `met`). Limits today: samples are supplied by the caller (gateway calls do
not yet record TTFT/TPOT) and the verdict is not yet wired into `exa pipeline promote`.

## SLOSpec by kind and the `slo` promotion gate (ADR 0148 decision 3)

One SLOSpec per `(servable or agent, kind, tenant)`; the objective shape is fixed by the kind, and
every threshold is inclusive (exactly at the threshold passes).

| Kind | Objectives (`exa slo spec set`) | Burn signal |
|---|---|---|
| `predictive` | `--latency-p99-ms` (max), `--error-rate` (max, `[0,1)`), `--availability` (min); at least one | multi-window burn rate |
| `generative` | `--pair` (a declared **p99** TTFT/TPOT pair), `--goodput-target` (min) | goodput below target; TTFT includes queue wait |
| `agentic` | `--task-success` (min, Wilson **lower bound**) with `--judge`; `--jct-p50-s`/`--jct-p95-s`, `--intervention-rate`, `--cost-per-task-p95-usd` (max) | success lower bound below target; JCT burn |

The generative kind *references* the pair record instead of copying it, so the TTFT/TPOT thresholds and
`tight` have one home (`exa slo pair-set`); the spec adds the goodput target. Editing a spec bumps its
`version`; setting identical objectives is a no-op.

`exa slo spec check` returns `met`, `violated` or `no_verdict` and exits 1 unless `met`. **Absence is
not a pass**: too few valid observations, a judge that has no passing calibration (ADR 0111), a
removed pair, or a kind whose telemetry the platform does not keep is `no_verdict` with the reason.
A spec is `met` only when every declared objective is; any violation makes it `violated`.

What the platform can observe by itself, and what it cannot:

- **predictive**: `gateway_calls` rows that carry a measured latency (default tenant only, that
  table has no tenant column) and the model's C6 `availability` SLI samples.
- **agentic**: ended `agent_sessions` give job completion time and cost. Task success and
  intervention are **not** recorded (a session's `status` is not a judged outcome), so they need
  `--samples`.
- **generative**: TTFT/TPOT are not persisted, so they need `--samples`.

`--samples file.json` replaces the ledgers (`[[ttft_ms, tpot_ms], ...]` for generative,
`[{"success": true, "jct_s": 12, "intervened": false, "cost_usd": 0.05}, ...]` for agentic, an object
of `latency_ms[]`, `requests`, `errors`, `availability_good`, `availability_total` for predictive);
`--record` stores the verdict.

### Refusing a promotion without an SLO

An opt-in gate, off by default and armed like `supply_chain`/`model_card`:

```bash
EXAMLOPS_POLICY_GATES=slo=enforce        # or  gates: {slo: enforce}  in policy.yaml
```

- `off` (default): never consulted; behaviour and audit trail are byte-identical to before.
- `monitor`: evaluated and audited as `policy_gate_monitor:slo`; a would-deny does not block.
- `enforce`: `exa pipeline promote` **and** the autopilot's promote refuse when the servable has no
  SLOSpec, or any of its specs is `violated` or `no_verdict`. `--force` does not override it (that flag
  only bypasses the metric gates).

The gate takes live evidence first; when that cannot decide it accepts the latest verdict recorded
with `exa slo spec check --record` if it is for the *current* spec version and younger than
`EXAMLOPS_SLO_SPEC_VERDICT_MAX_AGE_HOURS`. On the agent-version road (`exa agent alias set <agent>
Production <ref>`, ADR 0146) an armed gate additionally requires a declared `agentic` spec to be `met` and
stores the verdict in the alias-move evidence; an agent with no agentic spec is not refused there.

Limits: the kind of a servable is what its spec declares, so an armed gate refuses a model with no
spec of any kind rather than guessing its kind; the paired-SLO evaluator is unchanged.

## Benchmark results carry their conditions (ADR 0143 decision 5)

A TTFT, TPOT or goodput number is stored only together with the conditions it was measured under:

```bash
exa slo pair-set chat-llm interactive --ttft-ms 300 --tpot-ms 40
exa slo benchmark record chat-llm --file bench.json
exa slo benchmark results chat-llm
```

`bench.json` holds `{"conditions": {...}, "samples": [[ttft_ms, tpot_ms], ...]}`. The write is
refused, and exits 1, unless the conditions state all of the following:

- `model`, `quantization`, `hardware`, `engine_version`
- `dataset`, `length_distribution`, a positive `concurrency`
- `slo`, which must name a pair already declared for the servable
- `ttft_includes_queue_wait`

A sample may also be `{ttft_ms, tpot_ms, queue_wait_ms}`. When the run says TTFT excludes queue
wait, the queue wait is added, so every stored TTFT on a queued (HPC) substrate is the one the
user saw (ADR 0143 d6). Malformed samples are counted as `rejected`. With fewer than
`EXAMLOPS_SLO_PAIR_MIN_SAMPLES` valid samples the verdict is `no_verdict`, never `met`.
Recording the same run twice stores it once. Each new result writes one
`slo_benchmark_recorded` audit event, and results are listed per tenant.

## Graceful degradation

Rule generation is pure dict/string assembly — no Prometheus needed to produce or test
the rules. `slo_status`/`budget_exhausted` read `platform_db` samples only. PyYAML is
used for YAML I/O; pass dict specs directly if it's absent.

## See also

- [Evaluation gate (C3)](evaluation.md) — the promotion gate SLO exhaustion feeds.
- [Advanced drift (C5)](drift-advanced.md) / [AgentOps (C4)](agentops.md) — SLI sources.
- `platform/infra/docker-compose/alert_rules.yml` — the hand-authored baseline alerts.
