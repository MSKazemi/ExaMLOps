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
exa slo set JPCP latency-p99 --target 0.99 --window 30d --source c1
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
quality), `c5` (drift/data-quality), `availability`, or raw `prometheus`.

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
