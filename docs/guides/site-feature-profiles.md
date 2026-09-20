# Site feature profiles

Different centres run different parts of ExaMLOps. One has GPU clusters and wants the HPC fleet.
Another has no LLM endpoint and should not offer the agent. A third only trains and serves
models. A **site profile** records that choice once, and every surface of the platform honours
it.

## Modules

A module is a coarse slice of the platform:

| Module | What it covers |
|---|---|
| `core` | Status, config, the model registry, projects, approvals, audit, secrets, policy, backup, upgrades, this switch. **Always on.** |
| `training` | Prefect pipelines, retraining, scaffolding, reproducibility, dataset versioning, feature store, asset pipelines |
| `serving` | Ray Serve, predictions, production verification, traffic, shadow / A-B / challenger, batch, autoscaling, rollback |
| `quality` | Drift, continuous evaluation, judge calibration, SLOs, fairness |
| `governance` | Compliance evidence, governance reports, model/dataset cards |
| `genai` | LLM gateway, prompt registry, guardrails, vector store, RAG, embeddings, fine-tuning |
| `llm-serving` | A GPU vLLM engine behind the gateway (requires `genai`) |
| `agent` | The Skipper agent (ask/chat, dashboard copilot), agent ops, the MCP/A2A surface |
| `autopilot` | The closed detect → retrain → validate → promote loop (requires `quality`, `training`, `serving`) |
| `hpc` | Scheduler discovery and approval, placement, capacity, hardware portability, fleet twin, federated training |
| `finops` | Budgets, cost and carbon accounting, reports |
| `workbenches` | Per-project JupyterHub notebooks |
| `observability` | Prometheus, Alertmanager, Grafana, Loki, Tempo |
| `integrations` | Dataplane bus bridge, ModelZoo sync, signed Exchange packages (requires `serving`) |

`exa modules show <module>` lists everything a module owns: its commands, dashboard flags and
API routes, Compose services and profiles, Helm switches, related environment gates, and
prerequisites.

## Presets

| Preset | Modules |
|---|---|
| `full` | Every module. This is what an install with no profile runs, so nothing changes until you choose. |
| `standard` | core, training, serving, quality, governance, observability, workbenches, finops |
| `minimal` | core, training, serving |
| `hpc-center` | `standard` + hpc, autopilot |
| `genai` | `standard` + genai, llm-serving, agent |

## Choosing modules

```bash
exa modules presets
exa modules preset standard --site-name centre-a
exa modules enable hpc          # dependencies come with it
exa modules disable agent       # modules that need it go off too
exa modules list                # on/off and *why* for each module
```

The profile lives in `site.toml`. It is created in the [instance-data root](three-layer-architecture.md)
(`$EXAMLOPS_DATA_DIR/site.toml`), otherwise in the site configuration directory. As instance data
it is backed up with everything else and kept across upgrades:

```toml
[site]
name = "centre-a"

[features]
preset = "standard"
enable = ["hpc"]
disable = ["finops"]
```

For a single process, or for a deployment with no shared file, `EXAMLOPS_FEATURES` overlays the
file:

```bash
EXAMLOPS_FEATURES=preset:standard,+hpc,-finops    # comma or space separated; bare name = +name
```

### Resolution

Each layer overrides the one before it:

1. **Preset.** Taken from the profile. The default is `full`.
2. **Site profile.** Its `enable` and `disable` lists.
3. **`EXAMLOPS_FEATURES`.** Its `preset:`, `+module` and `-module` tokens.
4. **`core` forced on.**
5. **Dependencies closed.** An enabled module pulls in what it requires unless that was
   explicitly disabled. In that case the dependent goes off too, and `exa modules list` says why.

Unknown module or preset names never crash anything. They appear as warnings in
`exa modules list`, `exa instance check` and `exa upgrade plan`.

## What "disabled" means on each surface

| Surface | Effect |
|---|---|
| `exa` CLI | The module's commands disappear from `exa --help`, `exa docs` and the dashboard's CLI Console catalogue. Invoking one names the module and how to enable it, and exits **3**. That code is distinct from 1, so a script can tell "switched off here" from "failed". Third-party plugin commands are never gated. |
| Dashboard API | Every route the module owns answers `404` with `{"code": "module_disabled", "module": …}`. This is checked server-side, not just hidden in the UI. |
| Dashboard flags | A flag that belongs to a disabled module evaluates **false**, even with an admin override. `GET /api/v1/modules` returns the profile for the UI. |
| Dashboard navigation | The module's consoles leave the sidebar and the ⌘K palette. The server lists them (`disabled_pages` in `GET /api/v1/modules`, from each module's `dashboard_pages` in the catalog), so the UI keeps no module map of its own. |
| Agents (MCP) | The module's MCP tools are not offered: `exa mcp serve`, `exa mcp tools` and the A2A agent card list only tools of enabled modules (each module declares the tool tags of its domain as `mcp_tags`). |
| Docker Compose | `exa modules render --target compose` prints `COMPOSE_PROFILES` for the enabled modules. It also writes an override that parks disabled always-on services in an inactive profile and makes their dependents optional. |
| Kubernetes | `exa modules render --target helm` writes values: `site.features` (injected into every pod as `EXAMLOPS_FEATURES`) and `agent.enabled`. |

The CLI applies a profile change on its next command. The dashboard picks it up within a few
seconds. Services that start or stop need the deployment re-rendered.

### Docker Compose

```bash
exa modules render --target compose --out platform/infra/docker-compose/docker-compose.site.yml
# in platform/infra/docker-compose/.env:
#   COMPOSE_FILE=docker-compose.yml:docker-compose.site.yml
#   COMPOSE_PROFILES=<the value render printed>
#   EXAMLOPS_FEATURES=<the value render printed>
exa stack up
```

The override uses `depends_on.required: false`, which needs Docker Compose ≥ 2.20.

### Kubernetes (Helm)

```bash
exa modules render --target helm --out site-values.yaml
helm upgrade --install examlops platform/infra/helm/examlops \
  --set global.imageRegistry=registry.example.org/ -f values-prod.yaml -f site-values.yaml
```

Pass a features string on the command line with `--set-string` and escape the commas:
`--set-string 'site.features=preset:standard\,+hpc'`.

## Relation to dashboard feature flags

[Dashboard feature flags](dashboard-feature-flags.md) stage a UI change: roll a console out to
50 % of users, or give admins a beta. Modules decide what a centre runs at all. The module gate
sits above the flag: a flag can hide a console that a module provides, but no flag can turn on a
console whose module is disabled.

## Related

- [Core · deployment · instance data](three-layer-architecture.md)
- [Upgrades & compatibility](upgrade-and-compatibility.md)
- [Environment variables — instance data, upgrades & site modules](../reference/env-vars.md)
