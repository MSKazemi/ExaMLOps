# ExaMLOps

**End-to-end MLOps platform for HPC workload management in large European research
projects.** ExaMLOps runs the full model lifecycle — train, register, promote, serve,
observe, retrain — on the schedulers European HPC centres actually run (Slurm, Flux, or
none at all), without requiring a Kubernetes cluster.

It is operated from one place: the **`exa` CLI**, with a React + FastAPI dashboard and a
LangGraph agent (**Skipper**) over the same code paths.

---

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

Everything above is reachable from the CLI: see the
[full command guide](reference/cli-commands-guide.md) for every command, what it is for,
and a runnable example.

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
