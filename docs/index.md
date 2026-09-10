---
title: ExaMLOps
description: Train, serve and govern machine-learning models on HPC as one loop — from drift to retraining, on the schedulers HPC centres already run.
hide:
  - navigation
  - toc
---

<div class="xm-hero" markdown>

# Train, serve and govern models on HPC as one loop

<p class="xm-lede">ExaMLOps runs the whole model lifecycle — train, register, promote, serve,
observe, retrain — on the schedulers European HPC centres actually run: Slurm, Flux, or none at
all. You operate it from the <code>exa</code> CLI, a web dashboard, or in plain English through
the Skipper agent, all over the same code paths.</p>

<div class="xm-cta" markdown>
[Explore the platform](explore/index.md){ .md-button .md-button--primary }
[Start in five minutes](guides/quickstart.md){ .md-button }
</div>

<div class="xm-player xm-hero-map" data-scene="system" data-mode="ambient"></div>

</div>

Every kind of work travels its own line through shared stations: a **prediction** on the
[data line](explore/prediction.md), a **retrain** on the [control line](explore/retrain.md),
a **decision** on the [decision line](explore/decisions.md), a **cluster job** on the
[compute line](explore/hpc.md), and **telemetry and evidence** on the
[signal line](explore/signals.md). Open any station on the map for what it does.

## Start here

| If you want to… | Go to |
|---|---|
| Get a stack running and train a model | [Quick Start](guides/quickstart.md) |
| Understand how the pieces fit | [Architecture](guides/architecture.md) |
| Find the command for a task | [exa CLI — Command Guide](reference/cli-commands-guide.md) |
| Drive the platform from a browser | [Dashboard Usage Guide](dashboard/usage-guide.md) |
| Talk to the platform in plain English | [Management Agent](guides/agent.md) |
| Add your own model | [Add a New Model](guides/add-a-new-model.md) |

```bash
make bootstrap                                   # dev stack + all dependencies
exa status                                       # what is running, what is in production
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
```

---

## What it covers

- **Training pipelines** — Prefect flows that auto-discover every registered model ×
  dataset, with YAML-driven per-environment overlays.
- **HPC orchestration** — a scheduler abstraction over Slurm, Flux and a mock backend,
  plus a fleet layer that discovers, approves, places and accounts for clusters.
- **Model lifecycle** — MLflow registry with multi-stage aliases, metric-gated promotion,
  lineage, diffing, and signed artifacts.
- **Serving** — Ray Serve multi-model routing with version-selectable inference, traffic
  splits and champion–challenger.
- **GenAI / LLMOps** — an OpenAI-compatible gateway, prompt registry, RAG, vector store,
  guardrails, and evaluation with [calibrated judges](guides/judge-calibration.md).
- **Observability** — Prometheus, Grafana, Loki, Tempo tracing, drift detection (output
  *and* input-embedding), and a closed-loop autopilot.
- **Governance** — a hash-chained audit trail, policy-as-code, secrets, supply-chain
  signing, and [EU AI Act](guides/eu-ai-act-compliance.md) / [NIST RMF](guides/governance-nist-rmf.md)
  mappings.
- **FinOps & Green AI** — GPU-hour cost attribution and carbon accounting, both with
  swappable calculation providers.

Everything above is reachable from the CLI. [Every capability](explore/capabilities.md) lists
all of it by lifecycle area, searchable; the [command guide](reference/cli-commands-guide.md)
gives each command's purpose and a runnable example; the [roadmap](explore/roadmap.md) shows
what is shipped and what is under construction.

---

## Interfaces

ExaMLOps deliberately exposes the same capabilities four ways, over shared code paths:

| Surface | Entry point |
|---|---|
| Command line | `exa` — [command guide](reference/cli-commands-guide.md) |
| Web | [Dashboard](dashboard/index.md) |
| Conversational | `exa chat` — [Skipper agent](guides/agent.md) |
| Agent-callable | `exa mcp serve` — MCP tools, resources and prompts |

See [All Interfaces](guides/interfaces.md) for how they relate.
