---
description: "Extend ExaMLOps without forking: swap how cost, carbon, placement, drift and promotion are calculated with provider plugins, declarative formulas and policy-as-code."
---

# Programmable MLOps — extending ExaMLOps without forking

ExaMLOps is **programmable**: you can change how the platform calculates and decides things — cost,
carbon, and now **fleet placement** — without editing core code. Every such calculation is a
**provider**: a swappable strategy behind one stable interface. You pick or author a provider three
ways, in precedence order:

1. **`--provider` / env / config** selects a built-in or installed provider.
2. **A declarative formula** in `~/.config/examlops/providers.yaml` (no code).
3. **A plugin** you `pip install` under the `exa.providers.<domain>` entry-point group.

If you configure nothing, the built-in **default** is used — and defaults reproduce the platform's
prior behaviour exactly. A broken plugin or bad formula never breaks a calculation; it falls back to
the default and is shown with its error.

> This guide covers the **placement** domain (new). For **cost** and **carbon**, see
> [`finops-providers.md`](finops-providers.md) — same mechanism, same trust model.

## See what's installed

```bash
exa providers list                      # every domain: carbon, cost, placement
exa providers list --domain placement   # just placement
exa --json providers list               # machine-readable (agents/scripts)
```

Each row shows the provider **name**, where it came from (**builtin** / **entrypoint** / **config**),
its **methodology**, and whether it **loaded** (`ok` / `ERROR: …`).

## Placement: change how the fleet places jobs

`exa hpc place` (and `exa pipeline run --cluster auto`) choose which ACTIVE cluster runs a job. The
default policy is **least-loaded** (most idle GPU/node headroom). You can swap the scoring policy.

### Option A — a declarative formula (no code)

Create `~/.config/examlops/providers.yaml`:

```yaml
placement:
  provider: expression
  formulas:
    # higher score = preferred cluster. Reference any capacity field the fleet exposes:
    #   idle_gpus, total_gpus, idle_nodes, total_nodes, idle_cpus,
    #   ask_gpus, ask_nodes, and any scalar you declare in a cluster's capabilities
    #   (e.g. carbon_intensity, cost_per_gpu_hour).
    score: "idle_gpus * 100 + idle_nodes - carbon_intensity"   # carbon-aware placement
```

Now placement prefers greener clusters:

```bash
exa hpc place --gpus 4
exa hpc place --gpus 4 --placement-provider expression   # explicit
EXAMLOPS_PLACEMENT_PROVIDER=expression exa hpc place --gpus 4
```

The formula is evaluated **safely** (sandboxed arithmetic — no imports, no attribute access, no I/O).

!!! note "A formula that weighs carbon has to earn it"
    A score that depends on `carbon_intensity` is a carbon-aware policy, so ADR 0112's R-ec gate
    applies. Until a recorded evaluation shows it beats the simple baselines
    (`exa finops carbon policy evaluate expression --trace grid.json --record`), it runs with
    carbon neutralised: every cluster scores at the same intensity, and the rest of the formula
    still decides. `exa hpc place` says so in its reason. See
    [Carbon-aware placement](../algorithms/carbon-aware-placement.md).

### Option B — a plugin (Python)

Ship a package exposing a `Provider` under the `exa.providers.placement` entry-point group:

```toml
# your_pkg/pyproject.toml
[project.entry-points."exa.providers.placement"]
fair-share = "your_pkg.placement:FairShareProvider"
```

```python
# your_pkg/placement.py
from examlops.providers import Provider, ProviderMeta

class FairShareProvider(Provider):
    name = "fair-share"
    def metadata(self) -> ProviderMeta:
        return ProviderMeta(methodology="fair-share by project quota", outputs=("score",))
    def compute(self, inputs) -> dict:
        # inputs carries idle_gpus, idle_nodes, ask_gpus, ask_nodes, + declared cluster scalars
        return {"score": ...}
```

`pip install` it and it appears in `exa providers list --domain placement`, selectable via
`--placement-provider fair-share`.

## Exposing cluster metadata to formulas

A placement formula can reference any **scalar** field you declare in a cluster's `capabilities`
(e.g. `carbon_intensity`, `cost_per_gpu_hour`) — they pass through to the scorer automatically, so
carbon- and cost-aware placement need no core change.

## Trust model (important)

| Path | Trust | What it can do |
|---|---|---|
| Built-in / plugin (Python) | **Trusted** — arbitrary code | Same as any dependency you `pip install`; an OS-level decision |
| `providers.yaml` formula | **Sandboxed** | Arithmetic over exposed inputs only — no imports, attributes, or I/O |

Prefer the formula path for less-trusted authors (sysadmins). Use a plugin only when a formula can't
express your logic — never widen the sandbox to run richer code.

## Where this is going

Placement is the first non-finops domain to become programmable. The same mechanism is being extended
to **drift** and **promotion** scoring, plus **policy-as-code** governance and a stable Python **SDK**
— see the design in `design/adr/0076`–`0082`. The north-star (a policy-governed
self-driving MLOps loop on sovereign HPC) is in `design/vision/futures/`.
