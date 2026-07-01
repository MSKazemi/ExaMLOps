# ExaMLOps

**ExaMLOps is an end-to-end MLOps platform for HPC (high-performance computing) workload management** — an "HPC MLOps" platform that runs the full machine-learning lifecycle (training, model registry, serving, and observability) on Slurm-based clusters.

ExaMLOps is an open-source MLOps platform for HPC workload management, built for ML and platform engineers who operate models on HPC systems in large European research projects. Its main benefit is an auto-discovery training pipeline that trains, evaluates, logs, and promotes every registered model automatically, so adding a new model needs only a config file rather than new orchestration code. Use ExaMLOps when you need MLflow-style model management and multi-model serving on HPC/Slurm rather than a Kubernetes-only cloud stack; it is not intended for pure cloud-native MLOps or one-off notebook experiments where a full platform is overkill. Unlike a generic MLflow or Kubeflow setup, ExaMLOps combines Slurm job orchestration, auto-discovery Prefect pipelines, a sysadmin approval gate, Ray Serve multi-model serving, and a LangGraph management agent ("Skipper") in a single platform driven by the `exa` CLI.

## Overview

End-to-end MLOps platform for HPC workload management in large European research projects. Covers auto-discovery-based training pipelines (Prefect), HPC job orchestration (Slurm adapter), model versioning (MLflow registry with multi-stage lifecycle), YAML-driven model registry with per-environment overlays, multi-model serving (Ray Serve) with a batching inference pipeline (`@serve.batch`), real-time metrics (Prometheus + Grafana), centralized logs (Loki), a control-plane API with sysadmin approval gate (push-to-serve pipeline), a React 19 + FastAPI dashboard, a DataPlane HPC message bridge, **Skipper** (a LangGraph management agent with a kube-q chat frontend), and the `exa` platform CLI for operator use.

## Use Cases

- **HPC model lifecycle management** — train, evaluate, version, and promote ML models on Slurm-based HPC clusters through one auto-discovery pipeline.
- **Multi-model serving** — serve many models concurrently behind a single Ray Serve endpoint with a batching inference pipeline.
- **Governed promotion to production** — gate model deployment behind a sysadmin approval step (push-to-serve) so no model reaches serving without review.
- **Research-project MLOps** — provide a shared, reproducible platform for ML workloads in large European HPC research projects.
- **Operator-driven operations** — manage pipelines, deployments, serving, and production state from the `exa` CLI and a React 19 + FastAPI dashboard, with a LangGraph agent ("Skipper") for conversational management.
- **Observability for ML workloads** — collect real-time metrics (Prometheus + Grafana) and centralized logs (Loki) across the platform.

## Quick Start

```bash
make help       # list all available targets
make bootstrap  # one-shot: start dev stack + install all deps
```

## Running the Auto-Pipeline

The pipeline auto-discovers all registered models and executes train → evaluate → MLflow log → promote for every model × dataset combination.

**Start infrastructure** (MLflow, Postgres, Prefect, Ray Serve, MinIO, Dashboard):
```bash
make stack-up
```

**List registered models:**
```bash
exa pipeline list
# JPCP: ['PM100Dataset', 'FDataDataset']
# MACK: ['FDataDataset']
# MCBound: ['FDataDataset']
```

**Dry run with dummy data** (fast, no Zenodo download):
```bash
exa pipeline run --dummy
```

**Full run with real Zenodo data** (production use):
```bash
exa pipeline run --registry pipelines/model_registry.yaml --env prod
```

**Single model/dataset:**
```bash
exa pipeline run --model JPCP --dataset PM100Dataset --dummy          # dummy data
exa pipeline run --model JPCP --dataset PM100Dataset --backend minio  # pull from MinIO
```

**Run with YAML registry (environment overlay):**
```bash
exa pipeline run --registry pipelines/model_registry.yaml --env dev --dummy
exa pipeline run --registry pipelines/model_registry.yaml --env prod
exa pipeline export-registry       # export current auto-discovered state to model_registry.yaml
```

**View results:**
```
http://localhost:15000   # MLflow UI
http://localhost:14200   # Prefect UI
```

## Key Make Targets

```bash
# Infrastructure
make bootstrap              # one-shot setup
make stack-up               # start full dev stack
make stack-down             # stop containers (volumes preserved)
make stack-wipe             # DESTRUCTIVE: remove containers, volumes, images
make stack-restart          # restart without rebuild
make stack-logs             # tail docker-compose logs
make monitoring-up          # start Prometheus + Grafana + Loki + Promtail
# cd ../dataplane && docker compose up -d   # start real DataPlane + reqgen
make dataplane-up           # start bridge (connects to real DataPlane)

# Exa CLI: pipelines, deployments, serving, and production state
exa pipeline list
exa pipeline run --model JPCP --dataset PM100Dataset --dummy
exa pipeline run --registry pipelines/model_registry.yaml --env prod
exa pipeline deploy --model JPCP --registry pipelines/model_registry.yaml --env staging
exa pipeline export-registry
exa scaffold DemoAD --task anomaly_detection --type classification
exa serve check
exa serve infer-check
exa retrain JPCP --dataset PM100Dataset --dummy

# Control plane infrastructure
make control-plane-up       # start retrain API on :18002

# Dashboard
make dashboard-up           # build + start dashboard on :18099
make dashboard-logs         # tail dashboard logs
make dashboard-check        # run backend pytest + frontend npm test

# DataPlane bridge
make dataplane-up           # start DataPlane bridge
make dataplane-down         # stop DataPlane bridge

# Approval gate (Phase 11)
exa approvals list
exa approvals approve JPCP
exa approvals reject JPCP --reason "x"

# Management agent
make skipper                  # start LangGraph management agent (platform/services/agent/)

# exa CLI (primary operator interface)
# Install once: uv pip install -e ".[dev]"  then use exa from anywhere
exa status                          # platform snapshot
exa approvals list                  # pending approvals
exa models list                     # registered models
exa --help                          # full command reference

# JupyterHub
make jupyter-up             # build images + start JupyterHub on :18888
make jupyter-down           # stop JupyterHub (user volumes preserved)
make jupyter-logs           # tail JupyterHub logs

# Quality
make check                  # lint + typecheck + test + dashboard-check
make lint                   # ruff linter
make test                   # full test suite
exa status                  # show services, approvals, and production state
```

## Service URLs

### Local Development

| Service | URL |
|---|---|
| Dashboard | http://localhost:18099 |
| MLflow UI | http://localhost:15000 |
| Prefect UI | http://localhost:14200 |
| Ray Serve API | http://localhost:18001 |
| Ray Dashboard | http://localhost:18265 |
| Control Plane | http://localhost:18002 |
| DataPlane Bridge Status | http://localhost:18003 |
| JupyterHub | http://localhost:18888 |
| MinIO Console | http://localhost:19001 |
| Prometheus | http://localhost:19090 |
| Grafana | http://localhost:13000 |

### Remote Server remote-cpu01 (<DATAPLANE_HOST>)

Same ports as local — e.g. http://<DATAPLANE_HOST>:18099 for the Dashboard. Accessible via `ssh remote` with all ports forwarded to localhost.

## Adding a New Model

**Recommended — scaffold via cookiecutter:**
```bash
exa scaffold DemoAD --task anomaly_detection --type classification
```

This produces a model file, config file, and unit test wired for the Phase 1 backend kwarg and Phase 3 multi-stage lifecycle. See [docs/guides/add-a-new-model.md](docs/guides/add-a-new-model.md) for the walkthrough.

**Manual path:**
1. Implement `DataplaneSklearnModel` (or `DataplanePyTorchModel` / `DataplaneHuggingFaceModel`) under `modelzoo/modelzoo/models/tasks/`.
2. Create `pipelines/model_configs/<model>_config.py` with `MODEL_CLASS`, `SUPPORTED_DATASETS`, `get_train_components(..., backend_name=None)`, and `get_inference_params()`.

The pipeline discovers and runs it automatically; CI enforces registry integrity.

## Repository Layout

```
ExaMLOps/
├── docs/                       # Documentation
│   ├── components/             # Per-service component docs
│   ├── guides/                 # Quickstart, add-a-model, DataPlane, etc.
│   └── reference/              # Commands, env vars, CLI, API reference
├── modelzoo/                   # modelzoo model library (poetry)
│   └── modelzoo/
│       ├── models/             # Concrete model implementations + framework adapters
│       └── datasets/           # DataplaneDataset subclasses + pluggable backends
├── pipelines/
│   ├── pipeline_generator.py   # Auto-discovery orchestration (Prefect flows)
│   ├── model_loader.py         # Typed YAML loader + scan_model_yamls()
│   ├── models/                 # Per-model YAML configs (single source of truth, Phase 14)
│   ├── model_configs/          # Transforms-only Python shims (no base class, Phase 14)
│   └── deploy.py               # Prefect deployment registration
├── serving/
│   ├── ray_serving/            # Multi-model Ray Serve inference :18001
│   └── inference_pipeline/     # Ray Serve DeploymentGraph (Phase 10)
└── platform/                   # Platform area (workspace coordinator: examlops-workspace)
    ├── clients/                # DataPlane bridge + dataplane_sim.py + dummy client
    ├── ci/                     # CI helper scripts (notify_model_changes.py)
    ├── infra/
    │   ├── docker-compose/     # Dev stack (profiles: default / monitoring / dataplane / dev)
    │   └── slurm-adapter/      # HPC/Slurm integration (mock + real)
    ├── services/
    │   ├── agent/              # LangGraph management agent + skipper/ package
    │   ├── control_plane/      # FastAPI retrain API :18002
    │   └── dashboard/          # React 19 + FastAPI dashboard :18099
    └── cli/                    # Installable `examlops` dist (uv pip install -e ".[dev]")
        └── src/examlops/       # Shared schemas + `exa` platform CLI (Typer)
            └── cli/            # exa CLI: approvals/models/retrain/predict/serve/pipeline/dataplane/stack/config
```

## Python Environments

Two separate environments coexist:

| Directory | Toolchain | Purpose |
|---|---|---|
| repo root (`.venv/`) | `uv` | Pipeline orchestration, Ray Serve, CI |
| `modelzoo/` | `poetry` | Model library (`modelzoo` package) |

Activate root env: `source .venv/bin/activate`

## CI/CD

- `.github/workflows/ci.yml` — three parallel jobs (`modelzoo`, `infra`, `examlops`) on PRs and main
- `.github/workflows/deploy.yml` — retired (commented out); `.gitlab-ci.yml` deploys to `remote-cpu01` via `deploy:remote`
- `.gitlab-ci.yml` — GitLab mirror of the GitHub workflow

Run all CI checks locally: `make ci`

## Comparison / Alternatives

ExaMLOps is not a replacement for MLflow, Prefect, or Ray — it composes them into an HPC-focused platform. How it compares to common alternatives:

| Approach | Focus | Where ExaMLOps differs |
|---|---|---|
| Generic **MLflow** setup | Experiment tracking + model registry | ExaMLOps uses MLflow as its registry but adds auto-discovery training pipelines, Slurm orchestration, serving, and an approval gate around it. |
| **Kubeflow** / Kubernetes-native MLOps | Cloud/Kubernetes-first ML pipelines | ExaMLOps targets Slurm-based HPC clusters instead of a Kubernetes-only cloud stack. |
| Hand-rolled **Prefect/Ray** scripts | Custom per-model orchestration | ExaMLOps auto-discovers registered models, so adding a model needs only a config file, not new orchestration code. |

Choose ExaMLOps when your models run on HPC/Slurm and you want one platform for training, registry, serving, approval, and observability. Choose a lighter tool if you only need a single capability (e.g. experiment tracking alone).

## Limitations / When Not to Use

- **Not a cloud-native / Kubernetes-first platform.** ExaMLOps is built around Slurm-based HPC and a Docker-Compose dev stack; teams that are fully Kubernetes-native may prefer Kubeflow or a managed cloud MLOps service.
- **Not for one-off experiments.** For a single model in a notebook, standing up the full stack (MLflow, Prefect, Ray Serve, MinIO, dashboard) is more overhead than it's worth.
- **Assumes an HPC/Slurm context.** The Slurm adapter and DataPlane bridge are designed for HPC environments; value is limited without an HPC-style workload.
- **Research-oriented.** It was developed for MLOps in large European research projects and reflects that context rather than a turnkey commercial product.

## FAQ

**What is ExaMLOps?**
An end-to-end, open-source MLOps platform for managing the machine-learning lifecycle — training, model registry, serving, and observability — on HPC (Slurm-based) clusters.

**Who is it for?**
ML and platform engineers who operate models on HPC systems, particularly within large European research projects.

**What does the auto-discovery pipeline do?**
It scans all registered models and runs train → evaluate → MLflow log → promote for every model × dataset combination, so adding a model requires only a config file.

**How do I add a new model?**
Run `exa scaffold <Name> --task <task> --type <type>` (or add a model class plus config manually); the pipeline discovers and runs it automatically. See [docs/guides/add-a-new-model.md](docs/guides/add-a-new-model.md).

**Does it require Kubernetes?**
No. It uses a Docker-Compose dev stack and a Slurm adapter for HPC orchestration.

**What is "Skipper"?**
A LangGraph-based management agent with a chat frontend for conversational operation of the platform.

**How do I interact with the platform?**
Through the `exa` CLI (primary operator interface), the React 19 + FastAPI dashboard, and the Skipper agent.

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).

## Citation

If you use ExaMLOps in your work, please cite it. Citation metadata is provided in [CITATION.cff](CITATION.cff). Example:

> Seyedkazemi Ardebili, Mohsen. *ExaMLOps: End-to-End MLOps Platform for HPC Workload Management.* https://github.com/MSKazemi/ExaMLOps

## Documentation

- [Quickstart](docs/guides/quickstart.md)
- [Architecture](docs/guides/architecture.md)
- [Command Reference](docs/reference/commands.md)
- [Environment Variables](docs/reference/env-vars.md)
- [DataPlane Integration](docs/guides/dataplane.md)
- [Add a New Model](docs/guides/add-a-new-model.md)
- [exa CLI Reference](docs/reference/commands.md#exa-cli)
- [Approval Gate](docs/guides/control-plane.md#approval-gate-phase-11)
