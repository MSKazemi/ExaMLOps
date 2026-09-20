---
title: FAQ
description: Frequently asked questions about ExaMLOps — what it is, who it is for, whether you need a supercomputer to try it, supported schedulers, how models reach production, AI-agent access, licensing and how to contribute.
---

# Frequently asked questions

## What is ExaMLOps?

ExaMLOps is an open-source MLOps platform for HPC (high-performance computing) systems and
supercomputers. It runs the whole life of a machine-learning model on a cluster: training runs
are submitted as scheduler jobs, every trained version is stored with its metrics in an MLflow
registry, promotion to production passes through metric and approval gates, promoted versions
are served by Ray Serve, and drift, cost, carbon and quality are watched afterwards. It is
driven by the `exa` command-line tool, a web dashboard, or an AI agent.

## Who is ExaMLOps for?

ML engineers, platform engineers and HPC operators who run models on batch-scheduled clusters —
typically in research computing centres and large research projects — and want the MLOps
practices of cloud platforms (a model registry, gated promotion, versioned serving, monitoring,
an audit trail) without moving their workloads to Kubernetes.

## Do I need a supercomputer or Slurm to try it?

No. The scheduler defaults to `mock` mode, which trains in-process, so the full local stack and
the whole unit-test suite run on a laptop:

```bash
make bootstrap                 # start the local stack and install dependencies
exa pipeline run --dummy       # train every registered model on dummy data
```

The [Quick Start](guides/quickstart.md) walks through training, promoting and serving a model
locally.

## Which HPC schedulers does it support?

**Slurm** and **Flux**, plus a `mock` scheduler for development. The scheduler is selected with
`EXAMLOPS_HPC_SCHEDULER=mock|slurm|flux`; remote clusters are reached over SSH, with host-key
verification on by default. Resources are described in scheduler-neutral terms (GPUs, account,
QoS). The [HPC fleet](guides/hpc-fleet.md) guide covers discovering clusters, admitting them
and placing jobs.

## How is ExaMLOps different from Kubeflow or a plain MLflow server?

Kubeflow is built on Kubernetes; ExaMLOps targets **batch-scheduled HPC clusters** (Slurm, Flux)
first, and also generates Kubernetes serving manifests for sites that have both. A plain MLflow
server tracks experiments and stores models; ExaMLOps **uses** MLflow as its registry and adds
what surrounds it — auto-discovery training pipelines (Prefect), scheduler job submission,
promotion gates, multi-model serving, drift detection with optional automatic retraining,
FinOps and carbon accounting, a hash-chained audit trail, a dashboard and an agent — as one
platform. The [comparison page](compare.md) covers Ray on Slurm, ClearML and Metaflow too, with
sources, and says when to choose something else.

## How does a model reach production?

1. You describe the model in one YAML file (or generate it with `exa scaffold`).
2. The training pipeline runs it as a scheduler job and records the result in MLflow.
3. Promotion moves the `Production` alias only when a condition holds — for example
   `exa pipeline promote jpcp --if-rmse-lt 5.0` — and evaluation gates can block it.
4. Ray Serve picks up the new alias and serves it; traffic can be split between
   production and a canary.

Model changes that arrive through CI wait in an **approval queue** until an operator approves
them. Retrains triggered by drift are governed by cooldowns, rate limits and — for the
autopilot — autonomy levels rather than by that queue. The [Who decides](explore/decisions.md)
page lists every human gate.

## Can AI agents operate ExaMLOps?

Yes. `exa mcp serve` exposes platform capabilities as tools over the **Model Context Protocol**
(stdio or HTTP), so MCP clients such as Claude Code or Claude Desktop can query status, models,
drift and audit data. Write tools are only registered when the server is started with
`--allow-writes`. ExaMLOps also ships its own management agent, **Skipper**, which asks for
confirmation before every change it proposes, and `exa ask` routes plain-English questions to it.

## Does it support LLMs and generative AI?

Yes. Alongside classical models there is a versioned prompt registry, an OpenAI-compatible
model gateway with virtual keys, retrieval-augmented generation with citations, a vector store,
guardrails, LLM-judge evaluation with calibration, and pluggable inference engines. Optional
dependencies degrade to a local fallback when they are not installed. See the
[LLMOps guides](guides/prompt-management.md).

## Does it need a paid AI API?

No. With no API key configured, the Skipper agent uses a locally hosted model through Ollama,
and its long-term memory uses local embeddings. If you prefer a hosted model, setting an API
key switches it to the Claude API or an Azure OpenAI endpoint. The rest of the platform needs
no AI API at all.

## What does it cost, and what is the license?

ExaMLOps is free and open source under the **Apache License 2.0**, which permits commercial
use, modification and redistribution.

## How do I cite ExaMLOps?

Use the metadata in [`CITATION.cff`](https://github.com/MSKazemi/ExaMLOps/blob/main/CITATION.cff);
the **Cite this repository** button on GitHub turns it into APA or BibTeX.

## How can I contribute?

Start with [Get involved](community/index.md) and the
[contributing guide](https://github.com/MSKazemi/ExaMLOps/blob/main/.github/CONTRIBUTING.md).
Documentation fixes, tests, field reports from your cluster and plugins are all welcome — and
every page of this site has an edit button.
