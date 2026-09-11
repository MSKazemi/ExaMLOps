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
quality), `c5` (drift verdicts), `c8` (fairness disparity), `availability`, or raw
`prometheus`.

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

The event sources (`c1`, `c2`, `c5`) now record a watermark with each sample and count only
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

**The remaining sources are reported as un-ingested, with the reason:** `availability` because
no serving probe is persisted; `prometheus` because Prometheus evaluates its own rules
(use `exa slo generate`). This is not an oversight to tidy away: a source that silently records
nothing is indistinguishable downstream from a healthy service nobody asked about, which is the
trap the `measured` flag already exists to close.

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
the platform ingests itself — `c1`, `c2`, `c5`, `c8` — that series does not exist unless you publish it,
so those alerts can never fire:

```bash
exa slo export-metrics --out /var/lib/node_exporter/textfile/examlops.prom
# then: node_exporter --collector.textfile.directory=/var/lib/node_exporter/textfile
```

Run it on a timer (cron, a systemd timer, a Prefect schedule) — it is a pure read, so re-running
it costs a query and rewrites one file.

It publishes `examlops_slo_sli`, `_target`, `_budget_remaining`, `_burn_rate`, `_samples` and
`_measured`, plus vector-store latency and item gauges.

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

`exa slo status` shows, per SLO: target, observed SLI, **budget left** (100% = untouched,
negative = exhausted), **burn rate**, and OK/BREACH.

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

## Graceful degradation

Rule generation is pure dict/string assembly — no Prometheus needed to produce or test
the rules. `slo_status`/`budget_exhausted` read `platform_db` samples only. PyYAML is
used for YAML I/O; pass dict specs directly if it's absent.

## See also

- [Evaluation gate (C3)](evaluation.md) — the promotion gate SLO exhaustion feeds.
- [Advanced drift (C5)](drift-advanced.md) / [AgentOps (C4)](agentops.md) — SLI sources.
- `platform/infra/docker-compose/alert_rules.yml` — the hand-authored baseline alerts.
