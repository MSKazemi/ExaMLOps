# Authored providers — edit calculation code from a notebook

ExaMLOps calculations (FinOps **cost** and **carbon**, plus `drift`, `promotion`, `llm_*`, …)
resolve through the pluggable **provider** substrate (ADR 0074). Beyond the built-in providers,
entry-point plugins, and YAML formulas, you can **author a provider's Python directly** — from a
Jupyter notebook (workbench) or the CLI — and the platform picks it up. Providers are **per-project**
and run inside an **AST sandbox**.

## Write a provider

A provider is a small `Provider` subclass with a `compute(inputs) -> dict`. `Provider`,
`ProviderMeta`, `math`, and `Mapping` are **pre-injected** — you do not (and cannot) `import`
anything:

```python
class MyCost(Provider):
    name = "my-cost"
    version = "1.0"

    def metadata(self):
        return ProviderMeta(
            methodology="cost_usd = gpu_hours × 0.85 + cpu_hours × 0.05",
            units={"cost_usd": "USD"}, outputs=("cost_usd",),
        )

    def compute(self, inputs):
        return {"cost_usd": inputs.get("gpu_hours", 0) * 0.85 + inputs.get("cpu_hours", 0) * 0.05}
```

## From a Jupyter notebook (workbench)

A project's workbench has `examlops` on the path. Iterate live, then persist:

```python
from examlops.providers import register_from_source, save_provider

code = open("my_cost.py").read()          # or a triple-quoted string in the cell
register_from_source("cost", "my-cost", code)                 # test in THIS session
save_provider("cost", "my-cost", code, project="research")    # persist for the platform
```

`register_from_source` registers the provider in the current process only (fast iteration).
`save_provider` writes it to the project's provider directory so every `exa`/pipeline/serving
process that computes this project's cost can resolve it.

## From the CLI

```bash
exa providers validate --file my_cost.py                              # gate-check only (CI-safe)
exa providers author cost my-cost --project research --file my_cost.py # save + register (audited)
exa providers authored --project research                            # list a project's providers
exa providers show cost my-cost --project research                   # print stored source
exa providers activate cost my-cost --project research               # make it the domain default
exa providers rm cost my-cost --project research                     # delete (audited)
```

## From the dashboard

Open a project (**Projects → a project**) and use the **Providers** card: **New provider** opens a
code editor (domain + name + Python) with a **Validate** button (runs the AST gate) and a **Save**
that persists it; per-row **Edit** / **Activate** / **Delete**. All writes require the
`project.manage` capability and are audited; viewers see the list read-only. The editor is the GUI
half of this feature — it calls the same `examlops.providers` code path as the notebook/CLI.

## Active provider

Each `(project, domain)` can have one **active** provider — the one used when a calculation is run
with no explicit `--provider`. Set it with `exa providers activate` (or the dashboard **Activate**
button / the New-provider "make active" checkbox). `estimate_cost_via_provider(..., project=...)` and
`estimate_carbon_via_provider(..., project=...)` honour it.

## Use it

```bash
exa models cost jpcp --record --provider my-cost --project research
```

The FinOps `cost`/`carbon` entrypoints take an optional `project=` that loads that project's
authored providers before resolving `--provider`.

## Security (AST sandbox)

Authored source is statically gated before it runs (`examlops.providers.sandbox`). Rejected:
`import`/`from … import`, `eval`/`exec`/`compile`/`__import__`, `open`, `getattr`/`setattr`,
`globals()`/`locals()`, dunder attribute access (`x.__class__`), and `global`/`nonlocal`. Execution
uses a restricted `__builtins__`. This blocks the common escape hatches for authenticated internal
authors writing pure math; it is **not** a hardened jail against an unbounded adversary — pair it
with the approval tier for higher-risk code. A provider that fails the gate never reaches disk and
is skipped at load time (never fatal).

## Storage & config

| Setting | Default | Purpose |
|---|---|---|
| `EXAMLOPS_PROVIDERS_DIR` | `~/.config/examlops/providers` | Root of authored providers. Files live at `<root>/<project>/<domain>/<name>.py`. Point all processes (CLI, dashboard, serving) at one shared path (e.g. an NFS dir) so authored providers resolve everywhere. |
