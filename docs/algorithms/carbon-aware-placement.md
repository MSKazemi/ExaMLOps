# Carbon-aware placement: must beat the simple baseline, and keep beating it

A placement policy that weighs grid carbon intensity makes a claim: that its extra complexity
saves carbon. The platform does not take that claim on trust. A carbon-aware policy is **measured**
against simple baselines on the same workload and intensity trace. It may place jobs on carbon
only if it wins by a declared margin, and only while that measurement is current and shows the
capability still pays. Otherwise the simple policy runs, or carbon is taken out of placement
entirely.

This is ADR 0112's V16 amendment (R-ec and R-ed), and it rests on one measured finding. Sukprasert
et al. (EuroSys '24) studied carbon-intensity data from 123 regions and found that *"simple
scheduling policies often yield most of these reductions, with more sophisticated techniques
yielding little additional benefit"*, and that the benefit *"will decrease as the energy supply
becomes 'greener'"*.

!!! abstract "At a glance"
    | | |
    |---|---|
    | **Measured against** | carbon-agnostic placement (the reference), two simple baselines, and a perfect-foresight oracle (the headroom) |
    | **Ships** | the candidate only if it beats the best simple baseline by ≥ **5 pp** of carbon-agnostic emissions |
    | **Retired** | when the shipped policy saves < **2 %** — carbon is taken out of placement |
    | **Re-tested** | every **90 days**; an older measurement no longer licenses the policy |
    | **Evidence** | each evaluation is an event in the hash-chained audit log |
    | **Code** | `examlops.finops.carbon_policy` · gate in `examlops.hpc_placement_providers` · ADR 0112 |

## The model

A job \(j\) needs \(d_j\) whole hours and \(E_j\) kWh, spread evenly over those hours. It may start
at any hour in \([s_j,\ s_j + \text{slack}_j]\) and may run in any of its allowed regions. Given
hourly intensities \(I_r(h)\) in gCO₂e/kWh, running \(j\) in region \(r\) from hour \(t\) emits

$$
C(j, r, t) \;=\; \frac{E_j}{d_j}\sum_{h=t}^{t+d_j-1} I_r(h).
$$

A policy maps each job to a \((r, t)\). Its total is \(C_\pi = \sum_j C(j, \pi(j))\), and its
**reduction** is measured against carbon-agnostic placement (the home region, starting at
submission):

$$
\text{reduction}(\pi) \;=\; 100\left(1 - \frac{C_\pi}{C_{\text{agnostic}}}\right)\ \%.
$$

The model assumes **no capacity limits and no migration cost**, the idealisation Sukprasert et al.
use for upper bounds. That flatters shifting, which is the conservative direction for this test:
a policy that cannot beat a simple baseline with free, unlimited capacity will not beat it in
production.

## The policies compared

| policy | role | rule |
|---|---|---|
| `carbon-agnostic` | reference | home region, start at submission |
| `lowest-average-region` | simple (spatial) | the allowed region with the lowest *mean* intensity; no waiting |
| `threshold-shift` | simple (temporal) | in the home region, the first start in the slack window whose run-window mean is ≤ the region's 30th percentile; otherwise start at once |
| candidate | under test | any placement provider (e.g. `carbon-aware`), or the built-in `forecast-greedy` |
| `oracle` | headroom (not deployable) | the lowest-emission \((r, t)\) in the window, found with perfect foresight |

`forecast-greedy` is a realistic "sophisticated" policy. It runs the oracle's search, but scores
each future hour with a **24-hour persistence forecast**: the most recent same-hour value observed
before submission. It uses nothing a real scheduler would not know at submit time. A test pins
this down: if yesterday's dip moves by three hours today, forecast-greedy picks yesterday's hour
while the oracle picks today's.

A **placement provider** is evaluated the way it behaves in production. At submission it scores
every allowed region as a cluster, identical except for that region's *current* intensity, and
the job runs in the best-scoring region at once.

## The decision (R-ec)

Let \(\pi^\star\) be the better of the two simple baselines. The candidate's advantage is

$$
\Delta \;=\; \text{reduction}(\text{candidate}) - \text{reduction}(\pi^\star)\quad\text{(percentage points)}.
$$

The candidate **ships** if \(\Delta \ge m\). Otherwise \(\pi^\star\) ships. The margin \(m\) is
declared, not implied: 5 pp by default (`EXAMLOPS_CARBON_POLICY_MARGIN_PP`). That is a policy
choice: sophistication must buy at least five points of the carbon-agnostic emissions over the
best simple rule before its complexity is worth running. The oracle's reduction is reported
alongside as the headroom, so a reader can see how much any policy could possibly have saved.

## Retirement and re-testing (R-ed)

The benefit of carbon-aware placement shrinks as the grid decarbonises. It shrinks fastest on the
European grids this platform targets, so a capability whose value falls across its own build
horizon needs a stated end:

- **Retirement.** If the shipped policy's reduction is below \(r\) (2 % by default,
  `EXAMLOPS_CARBON_POLICY_RETIRE_BELOW_PCT`), the capability is **retired**: placement stops
  using carbon at all.
- **Cadence.** A measurement older than 90 days (`EXAMLOPS_CARBON_POLICY_RETEST_DAYS`, chosen to
  catch seasonal changes in the grid mix) no longer licenses a policy. It must be re-evaluated
  against the current grid.

## The runtime gate

Whenever a placement policy is resolved (`exa hpc place`, `exa fleet`, the SDK, MCP tools,
`exa pipeline run --cluster auto`), the resolver first asks one question: **does this policy's
score depend on carbon intensity?** It answers by probing. It scores two clusters that differ only
in `carbon_intensity`; if the scores differ, the policy weighs carbon. That covers built-ins, YAML
formulas and pip plugins alike, with no reliance on what a plugin chooses to declare.

A carbon-weighing policy then goes through the gate:

| latest real evaluation of the policy | what placement does |
|---|---|
| none (or only synthetic ones) | carbon-first policy (`carbon-aware`) → replaced by `carbon-simple`; any other → runs with its carbon input withheld |
| older than the cadence | same as none (re-test overdue) |
| simple baseline won | same as none (the simple policy ships) |
| retired | carbon input withheld (for `carbon-simple` too) |
| candidate won, current | runs as requested |

`carbon-simple` is the simple spatial baseline in placement form. Among clusters that fit, it picks
the lowest declared `carbon_intensity` outright and lets headroom break ties. A cluster with no
intensity data ranks **last**: unknown is not green.

**Withholding** neutralises the carbon input rather than deleting it: every cluster is scored as
if it had the same intensity. Carbon then cannot change the ranking, but the policy's other terms
still do. `cost-aware` still optimises cost, and a YAML formula that names `carbon_intensity` keeps
working instead of failing and falling back to bare headroom.

Every placement reports what happened: the placement reason says
`[placement policy: carbon-aware → carbon-simple: no R-ec evaluation …]`, and
`objectives.placement_policy` carries the requested and effective policy, the action, the reason
and the audit event of the evaluation relied on. A silent substitution would read as the requested
policy.

Two further rules:

- **Synthetic evaluations never gate.** `exa finops carbon policy sample` writes a demonstration
  trace, and evaluations over it are recorded as synthetic: they prove nothing about a real grid.
- **Absence of evidence is not eligibility**, the same rule ADR 0111 applies to LLM judges. A gate
  that cannot read the evaluations fails closed.

For a planned transition, `EXAMLOPS_CARBON_POLICY_GATE=warn` lets the requested policy run and
records what `enforce` would have done.

## On the synthetic trace

`exa finops carbon policy sample` generates three regions with a daily solar dip (one cleaner on
average) and 60 jobs of mixed flexibility. On it, the numbers reproduce the paper's shape:

| policy | reduction vs agnostic |
|---|---|
| `threshold-shift` (simple) | 9.7 % |
| `lowest-average-region` (simple) | 60.4 % |
| `carbon-aware` (candidate) | 60.4 % — **no advantage**, the simple policy ships |
| `forecast-greedy` (candidate) | 67.4 % — **+6.9 pp**, beats the 5 pp margin |
| `oracle` (headroom) | 67.6 % |

Following the live signal spatially buys nothing over knowing which region is cleaner on average.
Adding a forecast and temporal flexibility recovers almost all of the remaining headroom. These
are synthetic numbers that demonstrate the method; the gate only acts on evaluations over real
traces.

## Running an evaluation

```bash
exa finops carbon policy evaluate carbon-aware --trace grid.json            # dry run: prints the verdict
exa finops carbon policy evaluate carbon-aware --trace grid.json --record   # chains it; placement follows
exa finops carbon policy status carbon-aware                                # what placement will do now
exa finops carbon policy list                                               # recorded evaluations
```

The trace is JSON: `{"method": "<carbon signal method>", "regions": {"<name>": [gCO₂e/kWh per hour, …]},
"jobs": [{"id", "submit", "duration", "energy_kwh", "slack", "regions"?, "home"?}]}`. Hourly
historical intensities per bidding zone are available, for example, from ENTSO-E generation data
or from Electricity Maps. Record the `method` truthfully: ADR 0112 decides on **marginal**
(decision) signals, and an evaluation over an average-mix trace is labelled as an
accounting-basis figure.

## Guarantees and their tests

`tests/unit/test_carbon_policy.py` (26 tests; 8 of 8 deliberate mutations caught):

| guarantee | test |
|---|---|
| \(C(j,r,t)\) equals the hand-computed value | `test_emissions_match_the_hand_computed_formula` |
| the oracle is a lower bound on every policy | `test_the_oracle_is_a_lower_bound_on_every_policy` |
| forecast-greedy never reads the future | `test_forecast_greedy_uses_only_what_it_could_know` |
| matching the simple baseline is not enough to ship | `test_a_policy_that_only_matches_the_simple_baseline_does_not_ship` |
| retirement below the threshold | `test_retirement_when_the_shipped_policy_no_longer_pays` |
| every gate outcome, including synthetic and overdue evidence | `test_gate_decisions` |
| unmeasured `carbon-aware` places with `carbon-simple` | `test_unmeasured_carbon_aware_placement_runs_the_simple_baseline` |
| only a recorded winning evaluation licenses carbon placement | `test_a_recorded_winning_evaluation_lets_the_policy_place_on_carbon` |
| withholding keeps a policy's other objectives | `test_a_non_primary_policy_keeps_its_other_objectives_when_carbon_is_withheld` |
| a withheld YAML formula keeps working | `test_a_withheld_formula_keeps_its_other_terms` |

## References

- T. Sukprasert, A. Souza, N. Bashir, D. Irwin and P. Shenoy. *On the Limitations of Carbon-Aware
  Temporal and Spatial Workload Shifting in the Cloud.* EuroSys 2024.
- ADR 0111 — no uncalibrated judge may gate (the same principle, applied to an evaluator).
- ADR 0112 — carbon signals are typed: accounting vs decision.
