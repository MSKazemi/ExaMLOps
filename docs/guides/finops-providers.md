# Pluggable carbon & FinOps providers

ExaMLOps computes energy and carbon (and cost) through **pluggable providers** — swappable calculation
strategies. You can change *which formula* and *which coefficients* the platform uses **without editing
its source code**, three ways:

1. **Override coefficients** in a config file (keep the built-in formula).
2. **Author a formula** declaratively in YAML (change the math itself — no Python).
3. **Write a Python plugin** and `pip install` it (full power — a custom methodology, external data, etc.).

All three sit behind one interface, so the CLI, the dashboard, and the training pipeline all honour your
choice. The default reproduces the platform's original methodology exactly, so **doing nothing changes
nothing**.

> Design rationale and the technology survey behind this are in ADR 0074.

---

## Quick start

List what's available:

```bash
exa finops carbon providers
```

```
Carbon providers
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━┓
┃ Name                        ┃ Kind    ┃ Uncertainty ┃ Status ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━┩
│ ccf-like                    │ builtin │ ±30%        │ ok     │
│ codecarbon-like             │ builtin │ ±25%        │ ok     │
│ green-ai-default  (default) │ builtin │ ±30%        │ ok     │
└─────────────────────────────┴─────────┴─────────────┴────────┘
```

Use one for a single estimate:

```bash
exa finops carbon estimate --gpu-hours 12 --provider ccf-like --pue 1.3
```

`--json` / `-o json` works on every command (`exa -o json finops carbon providers`).

## The built-in carbon providers

| Provider | Formula | CPU-only work? | Uncertainty |
|---|---|---|---|
| **`green-ai-default`** (default) | `kWh = (gpu_hours × gpu_tdp + cpu_hours × cpu_tdp)/1000 × pue`; `co2e_g = kWh × grid_intensity` | **yes** | ±30% |
| **`codecarbon-like`** | component energy: `gpu_hours × (gpu_tdp + cpu_tdp + ram_gb × ram_w_per_gb)/1000 × pue`, after CodeCarbon | no | ±25% |
| **`ccf-like`** | `kWh = gpu_hours × energy_coeff_kwh_per_gpu_hour × pue`, after Cloud Carbon Footprint | no | ±30% |
| **`grid-live`** | `green-ai-default` formula, but `grid_intensity` is fetched **live** from a configured endpoint (degrades to the static default offline) | **yes** | ±20% |

Coefficients (with their defaults): `gpu_tdp_watts` 400, `pue` 1.5, `grid_intensity_g_per_kwh` 300,
`cpu_tdp_watts` 120, `ram_gb` 32, `ram_watts_per_gb` 0.3725, `energy_coeff_kwh_per_gpu_hour` 0.4.

### CPU-only runs are not zero-carbon

`--cpu-hours` is CPU-core-hours, and it is the other half of what the scheduler already reports for
every job — `exa models cost --record` reads `(gpu_hours, cpu_hours)` from Flux and stores both.
Counting only GPU-hours makes a run on a cluster without accelerators come out at exactly
`0.000 kWh`, which is the best possible number and never the true one:

```bash
exa finops carbon estimate --cpu-hours 32              # 5.760 kWh · 1728 gCO2e
exa finops carbon record JPCP --cpu-hours 32 --run-id <mlflow_run_id>
```

Two refusals guard the figure rather than shrinking it:

- **A provider with no `cpu_hours` term rejects the call.** `codecarbon-like` charges CPU and RAM
  over *GPU*-hours (a whole-node model) and `ccf-like` has a per-GPU-hour coefficient; neither can
  price CPU-only work, and extending them would mean inventing a methodology rather than applying
  theirs. Handed `--cpu-hours` they exit 1 and name a provider that can, instead of silently
  returning the smaller GPU-only number.
- **Supplying neither GPU- nor CPU-hours is refused.** A stored record of `0 kWh` is a claim that
  the run consumed no energy, not a note that nobody counted it. For the same reason
  `exa report` prints `not measured` rather than `0.000 kg CO2e` when there are no carbon records.

With `--cpu-hours` left at 0 every figure is arithmetically identical to what these providers have
always returned, so nothing changes for a GPU site.

### Live grid intensity (`grid-live`)

`grid-live` tracks how clean the grid is *right now* instead of using a fixed factor. Point it at a
grid-intensity endpoint (ElectricityMaps / WattTime / a national-grid API) and it fetches the current
gCO2/kWh (cached ~5 min):

```bash
export EXAMLOPS_GRID_INTENSITY_URL="https://api.example/carbon-intensity?fmt=json"   # may contain {zone}
export EXAMLOPS_GRID_INTENSITY_ZONE="FR"          # optional: substituted for {zone} or appended as ?zone=
export EXAMLOPS_GRID_INTENSITY_TOKEN="…"          # optional bearer token
exa finops carbon estimate --gpu-hours 12 --provider grid-live
```

The response body is parsed endpoint-agnostically (common keys: `carbonIntensity` / `intensity` /
`value`, nested one level). **Graceful degradation is guaranteed:** with no URL set, an unreachable
endpoint, or an unparseable/non-positive reading, `grid-live` falls back to the static
`grid_intensity_g_per_kwh` default — so carbon accounting never breaks offline. An explicit
`grid_intensity_g_per_kwh` input always overrides the live signal.

---

## 1. Override coefficients (keep the formula)

Create `~/.config/examlops/finops.yaml`:

```yaml
finops:
  carbon:
    provider: green-ai-default          # keep the default formula
    coefficients:
      gpu_tdp_watts: 700                 # e.g. an H100 board
      pue: 1.3                           # your datacentre
      grid_intensity_g_per_kwh: 250      # your grid region
```

Now every carbon calculation uses those numbers. A per-call flag still wins:
`exa finops carbon estimate --gpu-hours 12 --pue 1.1`.

## 2. Author a formula in YAML (change the math — no code)

Set `provider: expression` and supply `formulas` (and optional `coefficients` / `metadata`). Formulas are
evaluated **safely** (a sandboxed expression evaluator — no imports, no attribute access, no I/O) and **in
order**, so a later formula can use an earlier output:

```yaml
finops:
  carbon:
    provider: expression
    coefficients: {gpu_tdp_watts: 700, pue: 1.3, grid_intensity_g_per_kwh: 250}
    formulas:
      kwh:    "gpu_hours * (gpu_tdp_watts / 1000) * pue"
      co2e_g: "kwh * grid_intensity_g_per_kwh"       # 'kwh' is available here
    metadata:
      uncertainty: 0.25
      methodology: "H100 @ PUE 1.3, DE grid 250 gCO2e/kWh"
```

Available in formulas: your `coefficients`, the call inputs (`gpu_hours`, and any `--pue`/`--gpu-tdp`/
`--grid-intensity` you pass), earlier outputs, and a math allow-list (`min`, `max`, `abs`, `round`, `pow`,
`log`, `log10`, `log2`, `exp`, `sqrt`, `floor`, `ceil`, `sum`).

> The expression provider needs the optional dependency: `pip install "examlops[finops]"`. Without it, the
> built-in Python providers still work and the CLI tells you how to enable expressions.

## 3. Write a Python plugin (full power)

For a methodology that needs real logic or external data, ship a small package that exposes a `Provider`
under the `exa.providers.carbon` entry-point group.

```python
# my_carbon_plugin/provider.py
from examlops.providers import Provider, ProviderMeta

class MyGridAwareProvider(Provider):
    name = "my-grid-aware"

    def metadata(self) -> ProviderMeta:
        return ProviderMeta(
            methodology="Live grid intensity × measured GPU energy.",
            uncertainty=0.15,
            outputs=("kwh", "co2e_g"),
            source="internal",
        )

    def compute(self, inputs):
        gpu_hours = float(inputs["gpu_hours"])
        kwh = gpu_hours * (float(inputs.get("gpu_tdp_watts", 400)) / 1000) * 1.2
        grid = float(inputs.get("grid_intensity_g_per_kwh", 300))
        return {"kwh": kwh, "co2e_g": kwh * grid}
```

```toml
# my_carbon_plugin/pyproject.toml
[project.entry-points."exa.providers.carbon"]
my-grid-aware = "my_carbon_plugin.provider:MyGridAwareProvider"
```

```bash
pip install ./my_carbon_plugin
exa finops carbon providers          # → 'my-grid-aware' now listed (kind: entrypoint)
exa finops carbon estimate --gpu-hours 12 --provider my-grid-aware
```

A `Provider` implements `compute(inputs) -> {"kwh": ..., "co2e_g": ...}` and, ideally, `metadata()`. A
factory *callable* `def factory(config) -> Provider` is also accepted (it receives the config block).

You can also point at an un-packaged local class without an entry point:
`--provider "my_module:MyProvider"` (a dotted import path).

---

## How a provider is chosen

Highest priority first:

1. `--provider <name>` on the command
2. `EXAMLOPS_CARBON_PROVIDER` environment variable
3. `provider:` in `~/.config/examlops/finops.yaml`
4. the registered **default** (`green-ai-default`)

Coefficients merge as: **built-in defaults → config `coefficients` → per-call flags** (later wins).

### When a provider you configured fails

*A specific case of [honest degradation](honest-degradation.md) — the platform-wide rule.*

If a requested provider cannot be resolved (a typo, a missing plugin, bad YAML) or raises while
computing, the calculation **falls back to the built-in default rather than failing** — a broken
plugin must not stop a cost report or block a promotion.

**It now says so.** Until 2026-09-13 that fallback was silent and indistinguishable from the ordinary
case of having configured no provider at all: a site that had deliberately installed a stricter
promotion gate, its own placement score or a different carbon coefficient simply received the
platform's answer, with nothing anywhere to say which one it was. You get a warning naming the
provider and the cause, and stating plainly that this is *not* the same as configuring none.

Promotion goes further, because it is a **gate** rather than a calculation: the cause is appended to
the verdict's own reason, so the promotion record reads

```
0.0420 < 0.05 [built-in threshold used: configured provider failed — ValueError: coefficient table is empty]
```

and whoever reviews that decision can see the gate they configured was not the one that ran, without
going to the logs. The same applies across `drift`, `placement`, `carbon` and `cost`, which log it.

If you would rather a broken provider stopped the work than fell back, that is a policy this layer
deliberately does not take: the calculation continuing is the invariant. Pin the provider you want
with `--provider` and watch for the warning.

## Security & trust

- A **Python provider** (built-in or plugin) runs arbitrary code — the trust boundary is "who can install
  packages / place files on the host," identical to any dependency. Only install plugins you trust.
- A **YAML expression provider** is **sandboxed** (`simpleeval`): it cannot import modules, access
  attributes, or do I/O. This is the safe path for less-trusted authors (e.g. a sysadmin editing config).

## Reusable beyond carbon — cost providers

The same mechanism (`examlops.providers`) is domain-agnostic, and **cost is already a second consumer**.
HPC cost (GPU/CPU-hours → USD) is computed by a pluggable **rate card** under the `cost` domain:

```bash
exa finops cost providers            # → flat-rate (default), tiered-example, + your plugins
```

| Provider | Formula |
|---|---|
| **`flat-rate`** (default) | `cost_usd = gpu_hours × gpu_rate + cpu_hours × cpu_rate` (defaults `GPU_COST_PER_HOUR` 2.50, `CPU_COST_PER_HOUR` 0.05) |
| **`tiered-example`** | volume discount: GPU-hours above `tier_threshold` billed at `gpu_rate × (1 − tier_discount)` |

Cost estimation itself runs inside `exa models cost --record`. Select a rate card the same three ways as
carbon — coefficient override, an inline `provider: expression` formula, or a plugin under
`exa.providers.cost` — via the `[finops.cost]` block of `finops.yaml` or `EXAMLOPS_COST_PROVIDER`:

```yaml
finops:
  cost:
    provider: tiered-example
    coefficients: {gpu_rate: 2.5, tier_threshold: 100, tier_discount: 0.2}
```

The default reproduces the platform's original cost arithmetic exactly, so `exa models cost` is unchanged
until you opt in. Drift-score and promotion-policy domains can follow the same pattern. See ADR 0074.

## LLMOps calculations — token cost, cache savings, routing, RAG quality (ADR 0083)

Four LLMOps figures are computed through the same provider substrate, one domain each:

| Domain | Default | Other built-ins | Live call site |
|---|---|---|---|
| `llm_cost` | `token-rate` | — | gateway accounting (`GatewayClient`, the LLM gateway service), eval usage |
| `llm_cache` | `hit-savings` | — | `exa gateway cache stats`, the dashboard caching panel |
| `llm_routing` | `least-cost` | `cost-latency` | the gateway's `cost_aware` routing strategy |
| `rag_quality` | `retrieval-lite` | — | `exa rag` context precision / recall |

**Where the config lives.** Each domain has a flat block in `~/.config/examlops/providers.yaml`
(the same file `placement`, `drift` and `promotion` use). A block under `finops:` in
`finops.yaml` is still read, but only when `providers.yaml` has no block for that domain.
The order of precedence is the usual one: `--provider`, then `EXAMLOPS_<DOMAIN>_PROVIDER` (for example
`EXAMLOPS_LLM_COST_PROVIDER`), then the block's `provider:`, then the default.

```yaml
# ~/.config/examlops/providers.yaml
llm_cost:                       # a contract price card, USD per 1k tokens
  provider: token-rate
  input_price_per_1k: 0.0005
  output_price_per_1k: 0.0015   # reasoning tokens are billed at the output rate
llm_routing:                    # trade price against observed time-to-first-token
  provider: cost-latency
  latency_weight: 0.00001       # cost units per ms of TTFT; see the note below
```

`latency_weight` converts milliseconds into whatever unit `cost_usd` is in. On the gateway's
`cost_aware` path, `cost_usd` is the deployment's `price_per_1k` (USD per 1k tokens) or the
locality marker, so the weight is in USD-per-1k-tokens per millisecond. The built-in default,
`0.001`, makes 100 ms of TTFT weigh as much as $0.10 per 1k tokens, which is far above typical
token prices. With the default, `cost-latency` therefore ranks almost purely by latency. Pick
the weight from the trade-off you want: `0.00001` makes 100 ms equal to $0.001 per 1k tokens.

`llm_cost`, `llm_cache` and `rag_quality` stay on the caller's own arithmetic until an operator
selects a provider. A locally served model therefore keeps its zero marginal cost instead of
picking up a generic price. `llm_routing` always runs, because choosing a deployment is the entire
job of the `cost_aware` strategy. A selected provider that fails to load is logged as a warning.
The caller then falls back to its own arithmetic, and a broken provider never stops a request.

**Per-deployment prices for `cost_aware`.** A deployment in `gateway.yaml` may declare
`price_per_1k` (USD per 1k tokens, `>= 0`). `cost_aware` ranks by that price when it is set.
Otherwise it falls back to locality, where local and site deployments are free and external ones
cost a flat marker. The deployment's observed TTFT is always passed in as `latency_ms`, so
selecting `cost-latency` changes the ranking without touching any code:

```yaml
models:
  chat:
    strategy: cost_aware
    deployments:
      - {provider: n1, model: qwen3:8b, price_per_1k: 0.0004}     # amortised on-prem GPU
      - {provider: omni, model: auto, external_ok: true, price_per_1k: 0.002}
```

**Seeing which formula ran.** `exa providers list --domain llm_routing` lists what is installed.
The dashboard's LLMOps console has a *How the numbers are computed* section. For each domain it
shows the provider an operator chose, or the routing default, with that provider's own
methodology text. For `llm_cost`, `llm_cache` and `rag_quality` with nothing chosen it says
*built-in arithmetic* and describes the caller's own math, because the registered default
provider does not run in that case. A provider that failed to load is shown there with its error
rather than being dropped. The section is resolved in the dashboard's own process, so it matches
the gateway only when both read the same config directory and `EXAMLOPS_<DOMAIN>_PROVIDER` env.

## Unit economics per workload kind (ADR 0148 d4)

`exa finops economics [--kind predictive|generative|agentic] [--days N]` reports one unit cost per
workload kind from ledgers that already exist: **per prediction**, **per 1k tokens** (and per
successful call) and **per agent task**. The kinds are shown side by side and never summed.

A number is only printed when it is supported: no rows gives `no_data`, fewer than
`EXAMLOPS_ECONOMICS_MIN_SAMPLES` outcomes gives `insufficient_samples`, and a missing cost gives
`not_metered`. Predictive inference has no metered USD cost, so its USD unit is blank. Agent
per-task cost covers model calls only (`complete: false`, a **lower bound**); sandbox-seconds,
idle-state GB-hours and hot-pool standby are not metered by the session ledger and are listed as
such. The per-task ledger below meters them, and `economics` reports its totals beside the lower
bound (`task_ledger`) without adding the two together.

## Per-task cost ledger (ADR 0148 d4)

An agent task costs:

    Σ model calls + Σ tool calls + sandbox-seconds + idle-state GB-hours + hot-pool standby share

`examlops.finops.task_ledger` holds all five components in one ledger (the
`task_cost_entries` table, created on first use). A task's total is computed from its entries,
so it always equals their sum.

```bash
exa finops task-cost record T1 --project research --component model_call --cost 0.02
exa finops task-cost record T1 --project research --component sandbox_seconds --quantity 120 --rate 0.0001
exa finops task-cost record T1 --project research --component idle_state_gb_hours --quantity 28 --hours 0.5 --rate 0.01
exa finops task-cost apportion gpu-pool-a --cost 4.8 --project research --task T1=3 --task T2=1 --rule weighted --period 2026-09-25
exa finops task-cost show T1
```

- **Owned by a project.** An entry without a project is refused. This covers standby, which is
  never left as unowned overhead.
- **No invented prices.** Sandbox and idle state are priced by `--rate`, or by
  `EXAMLOPS_SANDBOX_USD_PER_SECOND` / `EXAMLOPS_IDLE_USD_PER_GB_HOUR`. With no rate the command
  refuses; it never records 0.
- **Declared apportioning rule.** `equal` or `weighted` (for example by GPU-seconds). The shares
  sum exactly to the pool cost (largest remainder, in micro-dollars). The rule is recorded in
  each entry's `method` and in a `hot_pool_standby_apportioned` audit event. There is one
  apportioning per `(pool, --period)`. An identical re-run records nothing. A re-run with a
  different cost or task set is refused as a whole, so it can't half-merge into the first split.
  Use a new `--period` for a new apportioning.
- **Idempotent writes.** `--entry-id` makes a retried metering write a no-op. The id is scoped
  to its tenant.
- **Totals cover every entry.** `task-cost show` sums in SQL over all of a task's entries.
  `rows` is a bounded page, and `rows_truncated` says when it was cut.
- **Incomplete by default.** `complete` stays `false`, and `unmetered` names the missing
  components, until all five are metered. For a task that made no tool calls, record a
  zero-cost `tool_call` entry.
- Entries are telemetry and are not audited one by one (ADR 0148 d7). An apportioning is a
  decision, so it is audited.
