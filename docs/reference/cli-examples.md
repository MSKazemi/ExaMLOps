# `exa` CLI — Verified Examples

One working, verified example per command. Built + kept by the dashboard/CLI QA loop (2026-07-30).
Each example was run on the LXP node against the live stack; read-only examples are shown for
mutating/outward commands (a real `retrain`/`pipeline run`/`serve reload` would fire training/deploys).

> Run any command with `-h`/`--help` for its full options, or `exa explain <cmd>` for plain-language help.

## Getting Started

```bash
exa --version
#   exa version 0.46.0

exa status            # platform snapshot: service health + pending approvals + prod models
#   ExaMLOps Service Health: Control Plane ✓ · MLflow ✓ · Prefect ✓ · Ray Serve ✓ (3 models) · Dashboard ✓
#   ✓ No pending approvals

exa doctor            # diagnose setup: config, connectivity, DB health

exa env               # effective config + the source of every value (secrets redacted)
#   config file: ~/.config/examlops/config.toml   (table of Key · Value · Source)

exa explain status    # plain-language help + examples for a command

exa docs --out docs/reference/cli-generated.md   # regenerate the full command reference (make docs-cli)

exa plugins           # list installed exa CLI plugins + load status
#   No plugins installed (entry-point group: examlops.cli_plugins).
#     → Add one via [project.entry-points."examlops.cli_plugins"] in a package.

exa config show       # print resolved config (env + ~/.config/examlops/config.toml)
exa config contexts   # list configured contexts + the active one
```

## Models & Registry

```bash
exa models list                 # registered models + production alias + latest version
#   jpcp · Production=2 · Latest=2 · aliases: Staging, Canary, Archived, Production
exa models info jpcp            # one model: versions, aliases, metrics
exa models diff jpcp 1 2        # metric/param delta between two versions
exa models lineage jpcp         # pipeline → dataset → model-version chain
exa models cost jpcp            # HPC GPU-hour cost history  (--record to fetch+store)
exa modelzoo status             # ModelZoo freshness per model  (e.g. JPCP STALE since …)
exa modelzoo events             # recent ModelZoo push events
```

## Training & Pipelines

```bash
exa pipeline list               # auto-discovered models + supported datasets
#   JPCP←jpcp_config  datasets=[PM100Dataset, FDataDataset] · MACK · MCBound
exa pipeline validate           # validate pack models/*.yaml against the Python shims
# exa pipeline run --model JPCP --dataset PM100Dataset --dummy   # (fires a real Prefect run)
# exa retrain JPCP --dataset PM100Dataset --dummy                # (fires training via control plane)
exa scaffold DemoAD --task anomaly_detection --type classification   # scaffold a new model
```

## Serving & Inference

```bash
exa serve models                # models currently hot-loaded in Ray Serve
exa serve traffic-list          # traffic split across aliases for all models
exa serve check                 # smoke-test Ray Serve (health + one prediction/model)
# exa serve reload              # (hot-reloads Production models — a real serving mutation)
```

## Monitoring & Quality

```bash
exa drift status                # prediction-drift status  (graceful: "No data" until baselines exist)
exa drift baseline jpcp         # store current rolling stats as the drift baseline
exa autopilot status            # self-driving loop history
exa slo list                    # declared SLO specs
```

## HPC, Fleet & FinOps

```bash
exa finops carbon report        # aggregate recorded energy + CO2e  (graceful when empty)
exa finops budget status        # per-project GPU-hour/cost budget vs consumption
exa hpc nodes                   # compute nodes (CPUs/mem/GPUs/state)
exa hpc capacity                # per-cluster GPU util / GPU-hours / cost
```

## Governance & Security

```bash
exa audit --last 7d             # platform audit log (who/what/when) — hash-chained
exa audit verify                # recompute the audit hash-chain + report integrity
exa governance report           # NIST AI RMF evidence-coverage per control
exa approvals list              # pending model-change approvals
exa compliance status           # EU AI Act classification + conformity state
```

## Projects & Workspaces

```bash
exa project list                # all projects with quotas  (e.g. minio-demo ACTIVE)
exa project show minio-demo     # full anatomy: quota · resources · members · budget · storage
exa project current             # active project
exa connection list             # named connections (metadata only)
```

## Data & Features

```bash
exa data list FData             # recorded dataset revisions newest-first (with linked runs)
exa data snapshot FData --backend minio --path ./data/FData   # record an immutable revision
exa feature list                # registered feature views  (feature store, A3)
exa feature apply user_stats --entity user --source offline   # register/patch a feature view
exa features list               # versioned training feature store
exa assets list                 # asset-centric pipeline nodes + freshness  (A4)
exa cards model jpcp            # structured model card from live data
```

## GenAI & LLMOps

```bash
exa genai check                 # GenAI telemetry status (tracing/content-capture/semconv)
exa genai cost --model gpt-4o --in 1000 --out 500   # USD cost from token usage
exa prompt list                 # versioned prompt registry
exa prompt create greeting --template "Hello {name}"      # new immutable prompt version
exa guardrails test "ignore previous instructions"        # run text through the guardrail
exa guardrails stats            # allow/redact/block counts
exa agentops tools              # per-tool success rate / latency for agent tool-calls
exa rag list                    # knowledge bases + versions
exa rag ingest kb ./docs        # chunk+embed+index docs into a KB
exa gateway key list            # virtual keys (hashes only)
exa gateway cache stats         # semantic-cache hit-rate + savings (B3)
exa vector create demo --dim 384 --metric cosine          # create a vector collection
exa vector stats demo           # collection dim/metric/item count  (exit 1 if it doesn't exist)
```

## Governance, Security & Platform

```bash
exa secrets list                # secret metadata (never values)
exa secrets scan ./config       # scan a file/dir for likely secrets (CI gate)
exa policy list                 # policy rules from policy.yaml  (graceful when absent)
exa providers list              # pluggable calculation providers across every domain
exa namespace list              # project namespaces with model counts
exa connection list             # named connections (metadata only)
exa workbench list              # on-demand dev environments
exa events stats                # event-outbox backlog (pending/published/poison)
exa admission stats             # admission-control queue depth by state
exa backup list                 # backups & bundles with manifest metadata
exa stack status                # running containers + ports
exa seanerbus list              # models + their SeanerBUS UUIDs
exa mcp tools                   # tools exposed to agents over MCP
exa hpc nodes                   # compute nodes (CPUs/mem/GPUs/state)
exa hpc clusters                # registered clusters + approval state
```
